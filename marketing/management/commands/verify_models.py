"""Find out which language models this account can actually call.

WHY THIS COMMAND EXISTS
-----------------------
A provider's catalogue endpoint answers "what models exist", which is not the
question that matters. The question that matters is "what can this key call",
and the two answers differ enormously: on the machine this was written for,
NVIDIA listed 68 chat models and served 6.

That gap caused a real failure. Two AI employees were assigned a model the
catalogue advertised and the provider had retired. Every turn they took fell
back to running a tool directly, and the only visible symptom was a reply
beginning "No language model is configured" -- while the Language models page
reported the provider online, because the provider was online. Nothing on the
page was wrong, and nothing on the page was useful.

So this command asks each model the only question that settles it: it sends a
two-word prompt and sees what comes back. A model that answers is kept. A model
that is retired or unavailable is switched off, and any employee holding it is
moved to one that works.

    python manage.py verify_models                 every enabled provider
    python manage.py verify_models --provider nvidia
    python manage.py verify_models --tools         also check tool calling
    python manage.py verify_models --dry-run       report, change nothing

WHY --tools MATTERS
-------------------
An employee needs a model that can hold a tool-calling conversation, which is
a stronger requirement than being able to chat. A model that ignores the tools
parameter writes a confident paragraph about what it would have done and
changes nothing, which is the most misleading failure this platform has -- it
looks like success. `--tools` sends a real function definition and reports
which models actually asked to use it.
"""

from django.core.management.base import BaseCommand

from marketing import llm_client, llm_tools, provisioning
from marketing.models import AIAgent, LLMModel, LLMProvider

# Models that are not for conversation at all. Probing an embedding or a
# safety-classifier endpoint with a chat prompt produces a confusing failure
# that says nothing about whether the account is healthy.
NON_CHAT_MARKERS = (
    'embed', 'rerank', 'guard', 'safety', 'topic-control', 'ocr', 'parse',
    'vision', 'vl-', 'clip', 'asr', 'tts', 'riva', 'diffusion', 'image',
)

PROBE = [
    {'role': 'system', 'content': 'Answer in one word.'},
    {'role': 'user', 'content': 'Ready?'},
]

TOOL_PROBE = [{
    'type': 'function',
    'function': {
        'name': 'company__lookup',
        'description': 'Look a fact up in the company knowledge base.',
        'parameters': {
            'type': 'object',
            'properties': {'query': {'type': 'string',
                                     'description': 'What to look for.'}},
            'required': ['query'],
        },
    },
}]


class Command(BaseCommand):
    help = 'Probe every configured model and switch off the ones that cannot be called.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--provider', default='',
            help='Check one provider only, by key: openrouter, nvidia or ollama.')
        parser.add_argument(
            '--tools', action='store_true',
            help='Also check whether each live model can call a tool.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without changing anything.')
        parser.add_argument(
            '--include-disabled', action='store_true',
            help='Re-probe models that are already switched off, in case a '
                 'provider has restored one.')

    def handle(self, *args, **options):
        providers = LLMProvider.objects.filter(is_enabled=True)
        if options['provider']:
            providers = providers.filter(provider_key=options['provider'])
        if not providers.exists():
            self.stdout.write(self.style.ERROR(
                'No enabled provider matches that. Run manage.py workforce_status.'))
            return

        total_live = total_dead = total_disabled = 0

        for provider in providers:
            self.stdout.write('')
            self.stdout.write(self.style.MIGRATE_HEADING(
                f'{provider.label} ({provider.provider_key})'))

            if provider.requires_api_key and not llm_client.resolve_api_key(provider):
                self.stdout.write(self.style.WARNING(
                    '  No API key configured, so nothing can be probed. '
                    'Add one on the Language models page.'))
                continue

            models = LLMModel.objects.filter(provider=provider)
            if not options['include_disabled']:
                models = models.filter(is_enabled=True)

            candidates = [m for m in models
                          if not any(marker in m.model_id.lower()
                                     for marker in NON_CHAT_MARKERS)]
            skipped = models.count() - len(candidates)

            if not candidates:
                self.stdout.write('  Nothing to probe. Fetch the catalogue first '
                                  'on the Language models page.')
                continue

            self.stdout.write(f'  Probing {len(candidates)} models'
                              + (f', skipping {skipped} that are not for chat.'
                                 if skipped else '.'))

            live, dead = [], []
            for model in candidates:
                result = llm_tools.chat_with_tools(
                    provider, model.model_id, PROBE, tools=None, max_tokens=8)

                if result['ok']:
                    live.append(model)
                    note = ''
                    if options['tools']:
                        note = self._tool_note(provider, model)
                    self.stdout.write(self.style.SUCCESS(
                        f'    live  {model.model_id}{note}'))
                    if not model.is_enabled and not options['dry_run']:
                        model.is_enabled = True
                        model.save(update_fields=['is_enabled'])
                        self.stdout.write('          switched back on.')
                    continue

                message = result.get('message') or 'no reply'
                if provisioning.model_is_gone(message):
                    dead.append((model, message))
                    self.stdout.write(
                        f'    dead  {model.model_id}  ({_short(message)})')
                else:
                    # A timeout or a 503 says nothing about whether the model
                    # exists, so it is reported and deliberately left enabled.
                    self.stdout.write(self.style.WARNING(
                        f'    ?     {model.model_id}  ({_short(message)}) '
                        f'-- left enabled, this looks temporary'))

            total_live += len(live)
            total_dead += len(dead)

            if dead and not options['dry_run']:
                for model, message in dead:
                    total_disabled += provisioning.disable_model(model, reason=message)

            self.stdout.write(
                f'  {len(live)} callable, {len(dead)} unavailable'
                + (' (nothing changed, this was a dry run).' if options['dry_run']
                   else '.'))

        if not options['dry_run']:
            rehoused = provisioning.repair_agent_models()
            if rehoused:
                self.stdout.write('')
                self.stdout.write(self.style.WARNING(
                    f'Moved {rehoused} AI employee(s) onto a working model.'))

        self._report_roster()

        self.stdout.write('')
        if total_live:
            self.stdout.write(self.style.SUCCESS(
                f'{total_live} model(s) answered. {total_dead} could not be called'
                + (f'; {total_disabled} employee assignment(s) were moved.'
                   if total_disabled else '.')))
        else:
            self.stdout.write(self.style.ERROR(
                'No model answered on any provider. The employees will still run '
                'their tools and produce real records, but they cannot write '
                'prose. Check the key on the Language models page.'))

    def _tool_note(self, provider, model):
        """Whether this model actually asks to use a tool when offered one."""
        result = llm_tools.chat_with_tools(
            provider, model.model_id,
            [{'role': 'system', 'content': 'Use your tools when they apply.'},
             {'role': 'user', 'content': 'What is our refund policy?'}],
            tools=TOOL_PROBE, max_tokens=160)
        if not result['ok']:
            return '  [tools: call failed]'
        if result.get('tool_calls'):
            return '  [tools: yes]'
        return ('  [tools: NO -- it answered from memory, so it would invent '
                'company facts]')

    def _report_roster(self):
        """What each employee ended up with, which is the point of the exercise."""
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('AI employees'))
        for agent in AIAgent.objects.select_related('llm_model', 'llm_model__provider'):
            if agent.has_live_llm:
                self.stdout.write(self.style.SUCCESS(
                    f'  {agent.name:30} {agent.llm_model.model_id}'))
            elif agent.llm_model is None:
                self.stdout.write(self.style.ERROR(
                    f'  {agent.name:30} no model assigned -- tools only'))
            else:
                self.stdout.write(self.style.WARNING(
                    f'  {agent.name:30} {agent.llm_model.model_id} '
                    f'(provider not usable)'))


def _short(message, limit=58):
    text = ' '.join(str(message).split())
    return text if len(text) <= limit else text[:limit - 3] + '...'
