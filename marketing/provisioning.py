"""Bringing the workspace into existence, and keeping it in step.

WHY THIS FILE EXISTS
--------------------
A platform that needs a person to run six commands before it works is a
platform that does not work. Everything in here is idempotent and is called on
start-up and on every login, so the answer to "what do I have to set up first"
is: nothing.

It is also how the project keeps its promise that no capability is maintained
by hand. The employee roster comes from marketing/workforce.py, the capability
catalogue is generated from the tool registry, and the integration list is
generated from the connector register. Add a connector file and it appears on
the Integrations page. Add a tool and it appears on an employee's profile. No
migration, no seed edit, no settings change.

ORDER MATTERS
-------------
    profiles -> providers -> agents -> integrations -> settings
             -> capabilities -> knowledge

Capabilities need both the agents and the tool registry. The knowledge base
seed needs its source row. Everything else is independent.
"""

from django.db import transaction

from . import workforce
from .models import AIAgent, LLMModel, LLMProvider, Profile


# ===========================================================================
# Identity
# ===========================================================================

def ensure_profile(user):
    profile, _created = Profile.objects.get_or_create(user=user)
    return profile


# ===========================================================================
# Language model providers
# ===========================================================================

# The three providers the project supports, with a small starter catalogue
# each. A provider row exists whether or not a key has been entered for it,
# because the Language models page has to be able to show you what you could
# connect. `base_url` is left blank so the model's own DEFAULT_BASE_URLS
# applies, which keeps the default in one place.
#
# The first model listed for each provider is the one a new employee is given.
# All three first choices are free to use, which matters: the platform should
# be fully usable by somebody who has not paid for anything.
PROVIDER_BLUEPRINT = [
    {
        'provider_key': 'openrouter',
        'display_name': 'OpenRouter',
        'models': [
            ('deepseek/deepseek-chat-v3.1:free', 'DeepSeek V3.1 (free)'),
            ('meta-llama/llama-3.3-70b-instruct:free', 'Llama 3.3 70B (free)'),
            ('qwen/qwen-2.5-72b-instruct:free', 'Qwen 2.5 72B (free)'),
        ],
    },
    {
        'provider_key': 'nvidia',
        'display_name': 'NVIDIA NIM',
        'models': [
            ('meta/llama-3.3-70b-instruct', 'Llama 3.3 70B Instruct'),
            ('mistralai/mistral-small-24b-instruct', 'Mistral Small 24B'),
        ],
    },
    {
        'provider_key': 'ollama',
        'display_name': 'Ollama (local)',
        'models': [
            ('llama3.1', 'Llama 3.1 8B'),
            ('qwen2.5', 'Qwen 2.5 7B'),
        ],
    },
]


def ensure_providers(owner=None):
    """Create the three provider rows and their starter model catalogues."""
    created = []
    for row in PROVIDER_BLUEPRINT:
        provider, was_created = LLMProvider.objects.get_or_create(
            provider_key=row['provider_key'],
            defaults={
                'display_name': row['display_name'],
                'user': owner,
            },
        )
        if was_created:
            created.append(provider)
        for model_id, label in row['models']:
            LLMModel.objects.get_or_create(
                provider=provider, model_id=model_id,
                defaults={'display_name': label, 'source': 'fallback'},
            )
    return created


# The model a new employee is given, in order of preference. Named here rather
# than inferred, because "the first row the database happens to return" is not
# a choice, and an employee's default model is worth choosing.
PREFERRED_MODEL_IDS = (
    'deepseek/deepseek-chat-v3.1:free',
    'meta-llama/llama-3.3-70b-instruct:free',
    'meta/llama-3.3-70b-instruct',
    'llama3.1',
)


def default_model():
    """The model a new employee is given, if any is usable.

    Preference order: a named preferred model on a provider that actually has
    a credential, then any model on a configured provider, then a preferred
    model anywhere, then anything at all.

    That last fallback is deliberate rather than sloppy. An employee holding a
    model it cannot currently reach still shows on its profile what it *would*
    think with, and the readiness checklist on the dashboard says the provider
    needs a key. An empty field would say neither of those things.
    """
    usable = list(
        LLMModel.objects.filter(is_enabled=True, provider__is_enabled=True)
        .select_related('provider'))
    if not usable:
        return None

    configured = [m for m in usable if m.provider.is_configured]

    for pool in (configured, usable):
        if not pool:
            continue
        by_id = {m.model_id: m for m in pool}
        for wanted in PREFERRED_MODEL_IDS:
            if wanted in by_id:
                return by_id[wanted]
        return pool[0]
    return None


# ===========================================================================
# The employee roster
# ===========================================================================

def ensure_agents(owner=None):
    """Create the six AI employees. Keyed on agent_type, which is unique.

    Only the fields that describe the *job* are refreshed on an existing row.
    The language model, temperature and token budget are left alone, because
    those are choices somebody made on the employee's profile page and
    overwriting them on every login would be a bug that looks like magic.
    """
    model = default_model()
    created = []

    for row in workforce.AGENT_BLUEPRINT:
        agent, was_created = AIAgent.objects.get_or_create(
            agent_type=row['agent_type'],
            defaults={
                'name': row['name'],
                'role': row['role'],
                'avatar_icon': row['avatar_icon'],
                'avatar_color': row['avatar_color'],
                'persona_description': row['persona_description'],
                'system_prompt': workforce.full_prompt(row),
                'llm_model': model,
                'max_tokens': 1200,
                'user': owner,
            },
        )
        if was_created:
            created.append(agent)
            continue

        changed = []
        for field, value in (
            ('name', row['name']),
            ('role', row['role']),
            ('avatar_icon', row['avatar_icon']),
            ('avatar_color', row['avatar_color']),
            ('persona_description', row['persona_description']),
        ):
            if getattr(agent, field) != value:
                setattr(agent, field, value)
                changed.append(field)
        if agent.llm_model_id is None and model is not None:
            agent.llm_model = model
            changed.append('llm_model')
        if changed:
            agent.save(update_fields=changed)

    return created


def refresh_system_prompts():
    """Reapply the instructions from workforce.py to every employee.

    Separate from ensure_agents because it OVERWRITES a prompt somebody may
    have edited. It runs from the sync_workforce management command, never on
    login.
    """
    updated = 0
    for row in workforce.AGENT_BLUEPRINT:
        agent = AIAgent.objects.filter(agent_type=row['agent_type']).first()
        if agent is None:
            continue
        wanted = workforce.full_prompt(row)
        if agent.system_prompt != wanted:
            agent.system_prompt = wanted
            agent.save(update_fields=['system_prompt'])
            updated += 1
    return updated


# ===========================================================================
# Integrations, settings, capabilities, knowledge
# ===========================================================================

def ensure_integrations(owner=None):
    """One Integration row per registered connector."""
    from .integrations import ensure_integrations as ensure
    return ensure(owner=owner)


def ensure_settings(owner=None):
    """The global settings the platform assumes exist.

    The defaults live beside the tools that read and write them, in
    marketing/tools/platform.py, so the list is maintained in one place by
    whoever adds a setting.
    """
    try:
        from .tools import platform as platform_tools
    except ImportError:
        return 0

    # The tool module owns the default list, but the callable that applies it
    # has been spelled two ways during development. Try both names rather than
    # failing silently: an ImportError here used to mean the platform started
    # with no settings at all and the company profile page was empty, which is
    # a hard failure to diagnose from the symptom.
    for name in ('ensure_default_settings', 'apply_default_settings',
                 'ensure_defaults_now', 'seed_default_settings'):
        writer = getattr(platform_tools, name, None)
        if callable(writer):
            try:
                return writer(owner=owner) or 0
            except TypeError:
                return writer() or 0

    return _settings_from_blueprint(platform_tools, owner)


def _settings_from_blueprint(platform_tools, owner):
    """Apply DEFAULT_SETTINGS directly when no writer function is exported.

    The tool layer's own `platform.ensure_defaults` is a Tool, which needs a
    ToolContext and an audit trail. Provisioning runs on every login and must
    not depend on that, so it writes the same blueprint itself.
    """
    from .models_platform import SystemSetting

    blueprint = getattr(platform_tools, 'DEFAULT_SETTINGS', None)
    if not blueprint:
        return 0

    written = 0
    for row in blueprint:
        key = row.get('key')
        if not key:
            continue
        value = row.get('value')
        _setting, created = SystemSetting.objects.get_or_create(
            key=key,
            defaults={
                'label': row.get('label', key.replace('_', ' ').capitalize()),
                'description': row.get('description', ''),
                'group': row.get('group', 'company'),
                'value_type': row.get('value_type', 'text'),
                # The model unwraps {'v': ...} in its `resolved` property, so
                # the envelope is what the rest of the platform expects.
                'value': value if isinstance(value, dict) and 'v' in value else {'v': value},
                'choices': row.get('choices', []),
                'is_secret': row.get('is_secret', False),
                'display_order': row.get('display_order', 0),
                'updated_by': owner if getattr(owner, 'pk', None) else None,
            },
        )
        written += int(created)
    return written


def ensure_capabilities():
    """Generate the capability catalogue from the tool registry."""
    from . import tools
    return tools.ensure_capabilities()


def ensure_knowledge(owner=None):
    """Seed the company knowledge base so the platform is usable at once.

    A knowledge base that starts empty makes five of the six employees look
    broken: they are instructed not to invent company facts, so with nothing
    indexed they can only say they do not know. The seed set is real prose,
    and a person replaces or extends it from the Knowledge page.
    """
    try:
        from . import knowledge
    except ImportError:
        return 0
    return knowledge.ensure_seed_documents(owner=owner)


# ===========================================================================
# The one entry point
# ===========================================================================

@transaction.atomic
def ensure_workspace(owner=None):
    """Bring the whole workspace up. Safe to run as often as you like."""
    report = {}
    report['providers'] = len(ensure_providers(owner))
    report['agents'] = len(ensure_agents(owner))
    report['integrations'] = len(ensure_integrations(owner))
    report['settings'] = ensure_settings(owner)
    report['capabilities'] = ensure_capabilities()
    report['documents'] = ensure_knowledge(owner)
    return report


def ensure_workspace_for_user(user):
    """Called on login. Gives the person a profile and the workspace a roster."""
    ensure_profile(user)
    owner = user if getattr(user, 'pk', None) else None
    if not AIAgent.objects.exists():
        return ensure_workspace(owner)

    # The common case: everything exists already. Only the generated things
    # are refreshed, because those are the ones that go stale when the code
    # changes underneath a running database.
    ensure_integrations(owner)
    ensure_settings(owner)
    ensure_capabilities()
    return {}
