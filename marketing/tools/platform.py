"""Self-administration: the tools by which the platform configures itself.

WHY THIS MODULE IS THE HEADLINE REQUIREMENT
-------------------------------------------
Every other tool module is about the work: writing a job description, breaking
down a requirement, drafting a reply. This one is about the platform. It is
what lets somebody say to any employee

    "connect our Google Drive, the folder id is 1a2b3c, here is the token"

and have the connection actually exist afterwards, with the credential in the
Integration row's ``secrets`` and nothing whatsoever changed in the source
tree. No environment file, no settings edit, no migration, no deployment. The
same is true of the company name, the working hours, the signature block and
the approval reminder window: configuration is data, and data can be written by
a tool.

Take this module away and the promise collapses. An employee could still send
an email, but only if a developer had first pasted a token into a file, which
is precisely the manual code change the brief rules out.

WHAT ELSE LIVES HERE, AND WHY IT IS SHARED
------------------------------------------
Four capabilities belong to every employee rather than to one of them:

``Memory``          an employee that forgets between conversations is a chat
                    window, not a colleague.
``Collaboration``   an employee that does not know who its colleagues are
                    cannot delegate, so it refuses instead of asking.
``Self-Knowledge``  an employee that cannot enumerate its own tools has no
                    recovery path when a language model guesses a tool name
                    that does not exist.
``Work Tracking``   an employee that cannot see the approval queue cannot
                    answer the most common question put to it: "did that
                    actually go out?"

THE ONE RULE THAT SHAPES EVERY FUNCTION BELOW
---------------------------------------------
No tool in this module ever returns a secret value, in any field, ever. Not in
``text``, not in ``data``, not in a proposal summary. A tool that echoed an API
token back would have written it into the chat transcript, the audit detail and
quite possibly a language-model provider's request logs, all at once and
irreversibly. Secrets are reported as '(set)' or '(not set)', and a proposal
describes a credential by its length rather than its content.

Everything here is deterministic. Nothing in this module calls a language
model, because the platform's own configuration should not depend on a
probabilistic answer.
"""

import json
from datetime import timedelta

from . import base as tool_base
from .base import Proposal, ToolContext, ToolResult, editable, executor, tool


# ===========================================================================
# Default configuration
# ===========================================================================

# The settings the platform assumes exist. ``platform.ensure_defaults``
# applies this list, which is why a fresh database still has a company profile
# and an approval policy without anybody running a data migration.
DEFAULT_SETTINGS = [
    {
        'key': 'company_name',
        'label': 'Company name',
        'description': 'Used in signatures, documents and anything an employee '
                       'writes on the company behalf.',
        'group': 'company',
        'value_type': 'text',
        'value': 'Our Company',
        'choices': [],
        'display_order': 1,
        'is_secret': False,
    },
    {
        'key': 'company_industry',
        'label': 'Industry',
        'description': 'Shapes the vocabulary an employee reaches for. A '
                       'healthcare provider and a games studio do not write '
                       'the same rejection email.',
        'group': 'company',
        'value_type': 'text',
        'value': '',
        'choices': [],
        'display_order': 2,
        'is_secret': False,
    },
    {
        'key': 'company_website',
        'label': 'Website',
        'description': 'Quoted in outbound messages and marketing copy.',
        'group': 'company',
        'value_type': 'text',
        'value': '',
        'choices': [],
        'display_order': 3,
        'is_secret': False,
    },
    {
        'key': 'company_timezone',
        'label': 'Timezone',
        'description': 'Every time an employee writes into an invitation or a '
                       'deadline is stated in this zone. An unstated timezone '
                       'is the most common cause of a missed interview.',
        'group': 'company',
        'value_type': 'text',
        'value': 'Australia/Melbourne',
        'choices': [],
        'display_order': 4,
        'is_secret': False,
    },
    {
        'key': 'working_hours',
        'label': 'Working hours',
        'description': 'When the company is contactable. Used when proposing '
                       'meeting times, so an employee does not offer a '
                       'candidate a Sunday slot.',
        'group': 'company',
        'value_type': 'json',
        'value': {'start': '09:00', 'end': '17:00',
                  'days': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']},
        'choices': [],
        'display_order': 5,
        'is_secret': False,
    },
    {
        'key': 'email_signature',
        'label': 'Email signature',
        'description': 'Appended to outbound email. Kept here rather than in a '
                       'template so a person can change it without a code edit.',
        'group': 'company',
        'value_type': 'longtext',
        'value': '',
        'choices': [],
        'display_order': 6,
        'is_secret': False,
    },
    {
        'key': 'brand_tone',
        'label': 'Brand tone',
        'description': 'The voice every employee writes in. Marketing copy and '
                       'a support apology should sound like the same company.',
        'group': 'company',
        'value_type': 'text',
        'value': 'Warm but authoritative',
        'choices': [],
        'display_order': 7,
        'is_secret': False,
    },
    {
        'key': 'default_integration_mode',
        'label': 'Default integration mode',
        'description': "Mode applied to a newly created integration. 'auto' is "
                       'live once a credential is present and simulated until '
                       'then, which is what lets the platform be demonstrated '
                       'before any account exists.',
        'group': 'behaviour',
        'value_type': 'choice',
        'value': 'auto',
        'choices': ['auto', 'live', 'demo'],
        'display_order': 1,
        'is_secret': False,
    },
    {
        'key': 'max_tool_calls_per_turn',
        'label': 'Maximum tool calls per turn',
        'description': 'How many tools an employee may chain inside one reply. '
                       'A ceiling exists so a confused model cannot spend an '
                       'unbounded number of calls on one request.',
        'group': 'behaviour',
        'value_type': 'number',
        'value': 6,
        'choices': [],
        'display_order': 2,
        'is_secret': False,
    },
    {
        'key': 'agent_memory_enabled',
        'label': 'Employee memory enabled',
        'description': 'Whether an employee may record durable memories. Turn '
                       'it off and every conversation starts from nothing.',
        'group': 'behaviour',
        'value_type': 'boolean',
        'value': True,
        'choices': [],
        'display_order': 3,
        'is_secret': False,
    },
    {
        'key': 'demo_banner',
        'label': 'Label simulated actions',
        'description': 'Label every simulated action in the interface. Leave '
                       'this on: a simulated action presented as a real one is '
                       'the single most damaging thing this platform could do.',
        'group': 'behaviour',
        'value_type': 'boolean',
        'value': True,
        'choices': [],
        'display_order': 4,
        'is_secret': False,
    },
    {
        'key': 'require_approval_for_external',
        'label': 'Require approval for external actions',
        'description': 'Turning this off is not supported. The platform queues '
                       'every action that leaves the company regardless of this '
                       'value, because the approval step is enforced in the tool '
                       'layer rather than by configuration. The setting exists '
                       'to be reported, not to be relied on as a switch.',
        'group': 'approval',
        'value_type': 'boolean',
        'value': True,
        'choices': [],
        'display_order': 1,
        'is_secret': False,
    },
    {
        'key': 'approval_reminder_hours',
        'label': 'Approval reminder after (hours)',
        'description': 'How long a proposed action may sit unreviewed before it '
                       'is reported as ageing. Work that nobody is reminded '
                       'about is work that quietly never happens.',
        'group': 'approval',
        'value_type': 'number',
        'value': 24,
        'choices': [],
        'display_order': 2,
        'is_secret': False,
    },
    {
        'key': 'knowledge_min_relevance',
        'label': 'Minimum relevance to cite (percentage)',
        'description': 'A percentage. A search result scoring below it is not '
                       'quoted as a source, which keeps a weak keyword match '
                       'from being presented as a citation.',
        'group': 'knowledge',
        'value_type': 'number',
        'value': 5,
        'choices': [],
        'display_order': 1,
        'is_secret': False,
    },
    {
        'key': 'citation_required',
        'label': 'Citations required',
        'description': 'Whether a factual claim must name the document it came '
                       'from. This is what stops a marketing post repeating an '
                       'invented figure.',
        'group': 'knowledge',
        'value_type': 'boolean',
        'value': True,
        'choices': [],
        'display_order': 2,
        'is_secret': False,
    },
]


MODE_EXPLANATION = (
    "auto -- live when the credential is present, simulated when it is not. "
    "live -- live only, so a missing credential is reported as an error rather "
    "than quietly simulated. "
    "demo -- always simulated, even with a working credential."
)


# ===========================================================================
# Lazy module access
# ===========================================================================
# Model and connector imports happen inside functions, matching base.py. The
# tool registry is imported while the app registry is still populating, so a
# module-level model import here would be a circular import waiting to happen.

def _platform_models():
    from .. import models_platform
    return models_platform


def _integrations():
    from .. import integrations
    return integrations


def _audit():
    from .. import audit
    return audit


def _agents():
    from ..models import AIAgent
    return AIAgent


# ===========================================================================
# Small shared helpers
# ===========================================================================

_STOPWORDS = {
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'you', 'your',
    'our', 'are', 'was', 'were', 'can', 'could', 'would', 'should', 'about',
    'what', 'which', 'who', 'whom', 'how', 'why', 'when', 'where', 'please',
    'need', 'want', 'have', 'has', 'had', 'get', 'got', 'any', 'all', 'some',
    'out', 'not', 'but', 'also', 'there', 'their', 'they', 'them', 'his', 'her',
    'its', 'been', 'being', 'does', 'did', 'doing', 'will', 'shall', 'may',
    'now', 'one', 'yet', 'own', 'via', 'per', 'too', 'just', 'very', 'more',
    'most', 'than', 'then', 'each', 'both', 'here', 'over', 'only', 'same',
}

_TRUE_WORDS = {'true', 'yes', 'on', 'y', 't', '1', 'enabled', 'enable'}
_FALSE_WORDS = {'false', 'no', 'off', 'n', 'f', '0', 'disabled', 'disable'}

# Substrings that mark a key as holding a credential even when no connector
# field declares it. Deliberately conservative: 'key' on its own is not here,
# because 'provider_key' and 'folder_key' are not secrets and masking them
# would make the output useless.
_SENSITIVE_HINTS = ('token', 'secret', 'password', 'passwd', 'credential',
                    'api_key', 'apikey', 'private', 'access_key', 'refresh',
                    'client_secret', 'signature', 'cookie')


def _stem(word):
    """Fold the common English plural so a search is not defeated by an 's'.

    Nothing linguistic is intended here. It exists because "policy" has to
    find a tool whose description says "policies", and "memories" has to find
    "memory". Both sides of every comparison are folded the same way, so the
    only thing that matters is that the folding is consistent.
    """
    if len(word) > 4 and word.endswith('ies'):
        return word[:-3] + 'y'
    if len(word) > 4 and word.endswith('es'):
        return word[:-2]
    if len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
        return word[:-1]
    return word


def _terms(text):
    """The searchable words in a phrase, lowercased, folded, without noise."""
    cleaned = ''.join(character if character.isalnum() else ' '
                      for character in str(text or '').lower())
    return {_stem(word) for word in cleaned.split()
            if len(word) >= 3 and word not in _STOPWORDS}


def _as_bool(value):
    """Parse the many ways a language model writes a boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(
        f'"{value}" is not a yes or no value. Use true or false.')


def _as_number(value):
    """Parse a number, keeping it an integer when it is one."""
    if isinstance(value, bool):
        raise ValueError('a number was expected, not a yes or no value.')
    if isinstance(value, (int, float)):
        number = value
    else:
        number = float(str(value).strip().replace(',', ''))
    if isinstance(number, float) and number.is_integer():
        return int(number)
    return number


def _looks_secret(key):
    lowered = str(key).lower()
    return any(hint in lowered for hint in _SENSITIVE_HINTS)


def _short(value, limit=120):
    text = str(value).replace('\n', ' ').strip()
    if not text:
        return '(not set)'
    return text if len(text) <= limit else text[:limit - 3] + '...'


def _yes_no(value):
    return 'yes' if value else 'no'


def _require_agent(ctx):
    """A readable refusal when a tool that needs an employee has none.

    Most of these tools are about *this* employee -- its memory, its tasks, its
    colleagues. Running one without an employee in the context is a programming
    error somewhere above, and saying so beats an AttributeError in a chat
    window.
    """
    if ctx.agent is None:
        return ToolResult(
            ok=False, error='No employee in context.',
            text='This tool has to be run by one of the AI employees, and no '
                 'employee is attached to this request.')
    return None


def _setting_value(key, default=None):
    """One SystemSetting value, unwrapped, without raising if it is absent."""
    models = _platform_models()
    row = models.SystemSetting.objects.filter(key=key).first()
    if row is None:
        for spec in DEFAULT_SETTINGS:
            if spec['key'] == key:
                return spec['value']
        return default
    resolved = row.resolved
    return default if resolved in (None, {}, '') and default is not None else resolved


def _integration_or_error(provider_key, *, verb='inspect'):
    """Resolve a provider key to (integration, connector) or a refusal.

    Returns a three-tuple, the last member being a ToolResult when the lookup
    failed. Every integration tool starts with this, because an unknown key
    arriving from a language model is routine rather than exceptional, and it
    should produce a list of the valid keys rather than a traceback.
    """
    integrations = _integrations()
    models = _platform_models()

    key = str(provider_key or '').strip().lower().replace(' ', '_').replace('-', '_')
    if not key:
        available = ', '.join(sorted(
            models.Integration.objects.values_list('provider_key', flat=True)))
        return None, None, ToolResult(
            ok=False, error='No provider key given.',
            text=f'Name the integration to {verb}. Available keys: '
                 f'{available or "none yet -- run platform.ensure_defaults"}.')

    integration = integrations.get_integration(key)
    if integration is None:
        available = sorted(models.Integration.objects.values_list('provider_key', flat=True))
        registered = sorted(cls.key for cls in integrations.connector_classes())
        return None, None, ToolResult(
            ok=False, error=f'Unknown integration "{key}".',
            text=f'There is no integration with the key "{key}". Configured '
                 f'keys are: {", ".join(available) or "none"}. Connectors the '
                 f'platform ships with: {", ".join(registered) or "none"}. Run '
                 f'platform.ensure_defaults to create any missing row.')

    connector = integration.connector
    if connector is None:
        return integration, None, ToolResult(
            ok=False, error=f'No connector installed for {key}.',
            text=f'The {integration.name} row exists but no connector is '
                 f'installed for "{key}", so it cannot be configured or tested. '
                 f'The row can be removed on the Integrations page.')

    return integration, connector, None


def _field_map(connector):
    return {spec.key: spec for spec in connector.config_fields}


def _operation_rows(connector):
    """The advertised operations, whatever shape the connector declared them in.

    ``operations`` is documentation rather than dispatch, so connectors write it
    the way that reads best: a tuple of names, of pairs, or of dicts. Reading
    all three here means a connector author never has to care.
    """
    rows = []
    for entry in getattr(connector, 'operations', ()) or ():
        if isinstance(entry, dict):
            rows.append((str(entry.get('name') or entry.get('key') or ''),
                         str(entry.get('description') or entry.get('label') or '')))
        elif isinstance(entry, (tuple, list)) and entry:
            name = str(entry[0])
            description = str(entry[1]) if len(entry) > 1 else ''
            rows.append((name, description))
        else:
            rows.append((str(entry), ''))
    return [row for row in rows if row[0]]


def _mask_setting(row):
    """A SystemSetting value fit to print."""
    if row.is_secret:
        return '(set)' if row.resolved not in (None, '', {}, []) else '(not set)'
    value = row.resolved
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return _yes_no(value)
    return '(not set)' if value in (None, '', {}) else str(value)


def _safe_payload_value(key, value, secret_keys=()):
    """One payload value, masked if there is any chance it is a credential."""
    if key in set(secret_keys or ()) or _looks_secret(key):
        return '(hidden)' if value not in (None, '', {}, []) else '(not set)'
    if isinstance(value, dict):
        return '{' + ', '.join(
            f'{inner}: {_safe_payload_value(inner, item, secret_keys)}'
            for inner, item in list(value.items())[:12]) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_short(item, 40) for item in list(value)[:8]) + ']'
    return _short(value, 160)


def _agent_by_type(agent_type):
    AIAgent = _agents()
    key = str(agent_type or '').strip().lower().replace(' ', '_').replace('-', '_')
    if not key:
        return None
    found = AIAgent.objects.filter(agent_type=key).first()
    if found is not None:
        return found
    # Tolerate a role name or a person-facing name, because that is what a
    # language model reaches for when it has read list_colleagues output.
    for candidate in AIAgent.objects.all():
        if key in (candidate.name.lower().replace(' ', '_'),
                   candidate.role.lower().replace(' ', '_')):
            return candidate
    return None


def _roster_line(agent):
    return f'{agent.agent_type} -- {agent.name} ({agent.role})'


def _score_tool(entry, terms):
    """How well one tool matches a described need.

    Name and title carry more weight than description because a tool's name is
    chosen to say what it does, whereas its description also explains why it
    exists and therefore contains far more incidental vocabulary.

    Partial hits count, and have to. Nobody searching for a "policy" should
    miss a tool whose description says "policies", and an exact-word match
    alone would miss it. So a substring hit either way earns a smaller score
    than a whole-word one, which keeps singular and plural, and verb and noun,
    from being treated as unrelated words.
    """
    if not terms:
        return 0
    name_terms = _terms(entry.name.replace('.', ' ').replace('_', ' '))
    title_terms = _terms(entry.title)
    group_terms = _terms(entry.group)
    description_terms = _terms(entry.description)
    score = 0
    for term in terms:
        if term in name_terms:
            score += 6
        if term in title_terms:
            score += 4
        if term in group_terms:
            score += 2
        if term in description_terms:
            score += 1
        if any(term in word or word in term
               for word in name_terms | title_terms):
            score += 2
        elif any(term in word or word in term for word in description_terms):
            score += 1
    return score


def _argument_summary(entry):
    """The arguments one tool takes, as a readable phrase."""
    properties = entry.parameters.get('properties') or {}
    required = set(entry.required_arguments)
    if not properties:
        return 'no arguments'
    parts = []
    for key, spec in properties.items():
        kind = spec.get('type', 'string')
        parts.append(f'{key} ({kind}{"" if key in required else ", optional"})')
    return ', '.join(parts)


# ===========================================================================
# 1. INTEGRATIONS -- connecting a company application without a code change
# ===========================================================================

@tool(
    name='platform.list_integrations',
    title='List connected applications',
    description='List every integration with its category, whether it is '
                'enabled, its effective mode (live, demo, unavailable or '
                'disabled), its connection status and any required setting '
                'still missing. Secret values are never included. Optionally '
                'narrow to one category: communication, calendar, documents, '
                'development, social, internal.',
    group='Integrations', shared=True, reads_only=True, icon='fa-plug',
    capability='See which company applications are connected',
    parameters={
        'type': 'object',
        'properties': {
            'category': {
                'type': 'string',
                'description': 'Optional category filter: communication, '
                               'calendar, documents, development, social, '
                               'internal.',
            },
        },
    },
)
def list_integrations(ctx, category=''):
    """The roster of connected applications, as an employee needs to see it.

    An employee asked to email a candidate has to know whether Gmail will
    actually send or merely simulate, and if it will simulate, what is missing.
    Answering that from one call is what keeps an employee from promising
    something the platform cannot presently do.
    """
    models = _platform_models()
    rows = models.Integration.objects.all()

    wanted = str(category or '').strip().lower()
    if wanted:
        valid = [choice for choice, _label in models.Integration.CATEGORY_CHOICES]
        if wanted not in valid:
            return ToolResult(
                ok=False, error=f'Unknown category "{category}".',
                text=f'"{category}" is not a category. Valid categories are: '
                     f'{", ".join(valid)}.')
        rows = rows.filter(category=wanted)

    rows = list(rows)
    if not rows:
        return ToolResult(
            ok=True,
            text='No integrations exist yet. Run platform.ensure_defaults to '
                 'create a row for every connector the platform ships with.')

    lines = []
    data = []
    counts = {}
    for integration in rows:
        missing = integration.missing_settings
        line = (f'{integration.provider_key} -- {integration.name} '
                f'[{integration.get_category_display()}] '
                f'enabled: {_yes_no(integration.is_enabled)}, '
                f'mode setting: {integration.mode}, '
                f'effective mode: {integration.effective_mode}, '
                f'status: {integration.get_connection_status_display()}')
        if missing:
            line += f'. Still missing: {", ".join(missing)}'
        else:
            line += '. Nothing missing'
        lines.append(line)
        counts[integration.effective_mode] = counts.get(
            integration.effective_mode, 0) + 1
        data.append({
            'provider_key': integration.provider_key,
            'name': integration.name,
            'category': integration.category,
            'is_enabled': integration.is_enabled,
            'mode': integration.mode,
            'effective_mode': integration.effective_mode,
            'connection_status': integration.connection_status,
            'status_message': integration.last_status_message,
            'missing_settings': missing,
        })

    tally = ', '.join(f'{count} {mode}' for mode, count in sorted(counts.items()))

    return ToolResult(
        ok=True,
        text=(f'{len(rows)} integration(s) ({tally}).\n' + '\n'.join(lines) +
              '\n\nUse platform.describe_integration for the settings one of '
              'them needs, and platform.configure_integration to supply them.'),
        data={'integrations': data, 'by_effective_mode': counts})


@tool(
    name='platform.describe_integration',
    title='Describe an integration',
    description='The full configuration schema for one integration: every '
                'setting with its label, type, whether it is required, whether '
                'it is a credential, the help text saying where to obtain it, '
                'and whether it is currently set. Also lists the operations the '
                'connector advertises. Call this before configure_integration '
                'so you can tell a person exactly what to supply.',
    group='Integrations', shared=True, reads_only=True, icon='fa-circle-info',
    capability='Find out what an integration needs to be connected',
    parameters={
        'type': 'object',
        'properties': {
            'provider_key': {
                'type': 'string',
                'description': "The integration key, for example 'google_drive'.",
            },
        },
        'required': ['provider_key'],
    },
)
def describe_integration(ctx, provider_key):
    """What a person has to hand over before this connection can go live.

    This is the tool that makes the platform self-describing. An employee told
    "connect our Drive" does not have to know anything about Google: it reads
    the schema, sees that a folder id and an access token are required, sees the
    help text saying where the token comes from, and asks for exactly those two
    things. Without it the employee would have to guess field names, and a
    guessed field name is configuration that is silently ignored.
    """
    integration, connector, failure = _integration_or_error(
        provider_key, verb='describe')
    if failure is not None:
        return failure

    fields = []
    lines = []
    for spec in connector.config_fields:
        if spec.is_secret:
            state = '(set)' if connector.has(spec.key) else '(not set)'
        else:
            value = connector.setting(spec.key)
            state = _short(value) if value not in (None, '', [], {}) else '(not set)'
        kind = 'password' if spec.is_secret else spec.field_type
        line = (f'- {spec.key} ({spec.label}) type: {kind}, '
                f'{"required" if spec.required else "optional"}, '
                f'{"credential -- never shown back" if spec.is_secret else "plain setting"}. '
                f'Current: {state}')
        if spec.choices:
            line += f'. Choices: {", ".join(str(choice) for choice in spec.choices)}'
        if spec.help_text:
            line += f'\n    Where to get it: {spec.help_text}'
        lines.append(line)
        fields.append({
            'key': spec.key,
            'label': spec.label,
            'type': kind,
            'required': spec.required,
            'secret': spec.is_secret,
            'help_text': spec.help_text,
            'choices': list(spec.choices),
            'state': state,
        })

    operations = _operation_rows(connector)
    operation_lines = [f'- {name}: {description}' if description else f'- {name}'
                       for name, description in operations]
    missing = integration.missing_settings
    valid_keys = ', '.join(spec['key'] for spec in fields) or 'none'

    text = (
        f'{integration.name} ({integration.provider_key}) -- '
        f'{integration.get_category_display()}\n'
        f'{connector.description or integration.description}\n'
        f'Enabled: {_yes_no(integration.is_enabled)}. Mode setting: '
        f'{integration.mode}. Effective mode: {integration.effective_mode}. '
        f'Status: {integration.get_connection_status_display()}.\n'
        f'{integration.last_status_message}\n\n'
        f'SETTINGS ({len(fields)}):\n' +
        ('\n'.join(lines) if lines
         else '- none; this connector needs no configuration') +
        f'\n\nOPERATIONS ({len(operations)}):\n' +
        ('\n'.join(operation_lines) if operation_lines else '- none declared') +
        '\n\n' +
        (f'STILL MISSING: {", ".join(missing)}. Ask a person for exactly these, '
         f'then call platform.configure_integration with a settings object '
         f'whose keys come from: {valid_keys}.'
         if missing else
         'Everything required is present. platform.test_integration will check '
         'the connection.')
    )
    if connector.docs_url:
        text += f'\nProvider documentation: {connector.docs_url}'

    return ToolResult(ok=True, text=text, data={
        'provider_key': integration.provider_key,
        'name': integration.name,
        'fields': fields,
        'operations': [name for name, _description in operations],
        'missing_settings': missing,
        'effective_mode': integration.effective_mode,
    })


@tool(
    name='platform.configure_integration',
    title='Configure an integration',
    description='Supply the settings and credentials that connect a company '
                'application, so it goes live with no code change and no '
                'environment file. Pass settings as an object of field key to '
                'value, using the exact keys from describe_integration. '
                'Credentials are stored in the integration secrets column and '
                'are never displayed again. Optionally enable the integration '
                'and set its mode in the same change.',
    group='Integrations', shared=True, requires_approval=True, risk='high',
    icon='fa-key', capability='Connect a company application',
    parameters={
        'type': 'object',
        'properties': {
            'provider_key': {
                'type': 'string',
                'description': "The integration key, for example 'google_drive'.",
            },
            'settings': {
                'type': 'object',
                'description': 'Field key to value, using the keys from '
                               'platform.describe_integration. Unknown keys are '
                               'rejected by name.',
                'additionalProperties': True,
            },
            'enable': {
                'type': 'boolean',
                'description': 'Optionally switch the integration on or off as '
                               'part of the same change.',
            },
            'mode': {
                'type': 'string',
                'enum': ['auto', 'live', 'demo'],
                'description': 'Optionally set the mode. ' + MODE_EXPLANATION,
            },
        },
        'required': ['provider_key', 'settings'],
    },
)
def configure_integration(ctx, provider_key, settings, enable=None, mode=''):
    """The tool that delivers the project's headline promise.

    Somebody says "connect our Google Drive, the folder id is 1a2b3c, here is
    the access token" and this writes it. The folder id lands in
    ``Integration.config`` where the interface can show it; the token lands in
    ``Integration.secrets`` where nothing renders it. Nobody edits a file,
    nobody restarts anything, and the next Drive call is live.

    WHY IT STILL NEEDS APPROVAL. A credential arriving through a chat window
    could have come from anywhere, including from text an employee read in a
    document. Configuring an integration is the act that decides whether the
    platform's actions really leave the company, so it is proposed and a person
    releases it.

    WHY THE SUMMARY NEVER CONTAINS THE CREDENTIAL. The summary is rendered on
    the review page, stored on the action row and copied into the audit
    message. A token in it would be a token in all three. So a secret is
    described by its length -- enough for a reviewer to confirm they pasted the
    right thing, useless to anybody else. The real values sit in the payload,
    which only a reviewer who can already see the Integrations page ever loads,
    and the secret keys are named in ``_secret_keys`` so the review page knows
    which inputs to mask.
    """
    integration, connector, failure = _integration_or_error(
        provider_key, verb='configure')
    if failure is not None:
        return failure

    if not isinstance(settings, dict):
        return ToolResult(
            ok=False, error='settings must be an object.',
            text='settings must be an object of field key to value, for example '
                 '{"folder_id": "1a2b3c", "access_token": "..."}. Call '
                 f'platform.describe_integration on '
                 f'"{integration.provider_key}" for the field keys.')

    fields = _field_map(connector)
    if not fields:
        return ToolResult(
            ok=False, error=f'{integration.name} takes no configuration.',
            text=f'{integration.name} declares no configuration fields, so '
                 f'there is nothing to set. Use platform.enable_integration or '
                 f'platform.set_integration_mode instead.')

    unknown = [key for key in settings if key not in fields]
    if unknown:
        return ToolResult(
            ok=False, error=f'Unknown settings: {", ".join(unknown)}.',
            text=f'{integration.name} has no setting called '
                 f'{", ".join(unknown)}. Its valid setting keys are: '
                 f'{", ".join(fields)}. Call platform.describe_integration for '
                 f'the type and help text of each one.')

    if not settings:
        return ToolResult(
            ok=False, error='No settings given.',
            text=f'No settings were supplied. {integration.name} accepts: '
                 f'{", ".join(fields)}.')

    coerced = {}
    for key, raw in settings.items():
        spec = fields[key]
        try:
            if spec.field_type == 'number':
                coerced[key] = _as_number(raw)
            elif spec.field_type == 'boolean':
                coerced[key] = _as_bool(raw)
            elif isinstance(raw, (dict, list)):
                coerced[key] = raw
            else:
                coerced[key] = str(raw)
        except ValueError as exc:
            return ToolResult(
                ok=False, error=f'Bad value for {key}.',
                text=f'{key} ({spec.label}) expects a {spec.field_type} value: '
                     f'{exc}')
        if spec.choices and coerced[key] not in list(spec.choices):
            return ToolResult(
                ok=False, error=f'Bad value for {key}.',
                text=f'{key} ({spec.label}) must be one of: '
                     f'{", ".join(str(choice) for choice in spec.choices)}.')

    if enable is not None:
        try:
            enable = _as_bool(enable)
        except ValueError as exc:
            return ToolResult(ok=False, error='Bad enable value.',
                              text=f'enable expects true or false: {exc}')

    wanted_mode = str(mode or '').strip().lower()
    if wanted_mode and wanted_mode not in ('auto', 'live', 'demo'):
        return ToolResult(
            ok=False, error=f'Unknown mode "{mode}".',
            text=f'"{mode}" is not a mode. Use auto, live or demo. '
                 f'{MODE_EXPLANATION}')

    secret_keys = [key for key in coerced if fields[key].is_secret]

    change_lines = []
    for key, value in coerced.items():
        if fields[key].is_secret:
            change_lines.append(
                f'{key}: (a {len(str(value))}-character value will be stored)')
        else:
            previous = (integration.config or {}).get(key)
            change_lines.append(f'{key}: {_short(previous)} -> {_short(value)}')

    if enable is not None:
        change_lines.append(
            f'enabled: {_yes_no(integration.is_enabled)} -> {_yes_no(enable)}')
    if wanted_mode:
        change_lines.append(f'mode: {integration.mode} -> {wanted_mode}')

    still_missing = [spec.label for spec in connector.required_fields()
                     if not connector.has(spec.key) and spec.key not in coerced]

    summary = (
        f'Configure {integration.name} ({integration.provider_key}).\n'
        f'{len(coerced)} setting(s), of which {len(secret_keys)} are '
        f'credentials stored without ever being displayed again:\n' +
        '\n'.join(f'  {line}' for line in change_lines) +
        '\n\nOn approval the plain settings are written to the integration '
        'configuration, the credentials to its secrets column, and the '
        'connection is tested immediately.' +
        (f'\nStill missing after this change: {", ".join(still_missing)}.'
         if still_missing
         else '\nThis completes everything the connector requires.')
    )

    return Proposal(
        title=f'Configure {integration.name}',
        summary=summary,
        payload={
            'provider_key': integration.provider_key,
            'settings': coerced,
            'enable': enable,
            'mode': wanted_mode,
            '_secret_keys': secret_keys,
        },
        editable_fields=[
            editable('mode', 'Integration mode', 'text',
                     'auto, live or demo. ' + MODE_EXPLANATION),
        ],
        risk='high',
        integration_key=integration.provider_key,
        subject_label='marketing.integration',
        subject_id=integration.pk,
        subject_display=integration.name,
    )


@executor('platform.configure_integration')
def execute_configure_integration(action):
    """Write the configuration, then immediately check whether it worked.

    Probing here rather than leaving it to the interface matters: the person
    who has just approved a credential wants to know in the same breath whether
    the credential is any good, and the employee that proposed it needs that
    answer recorded on the action.
    """
    integrations = _integrations()
    payload = dict(action.payload or {})
    key = payload.get('provider_key', '')

    integration = integrations.get_integration(key)
    if integration is None:
        return ToolResult(
            ok=False, error=f'Integration {key} no longer exists.',
            text=f'The {key} integration row no longer exists, so nothing was '
                 f'configured.')

    settings = payload.get('settings') or {}
    secret_keys = set(payload.get('_secret_keys') or [])

    config = dict(integration.config or {})
    secrets = dict(integration.secrets or {})
    for field_key, value in settings.items():
        if field_key in secret_keys:
            secrets[field_key] = value
            config.pop(field_key, None)
        else:
            config[field_key] = value

    integration.config = config
    integration.secrets = secrets

    enable = payload.get('enable')
    if enable is not None:
        integration.is_enabled = bool(enable)
    mode = payload.get('mode') or ''
    if mode in ('auto', 'live', 'demo'):
        integration.mode = mode
    if action.decided_by_id:
        integration.configured_by = action.decided_by
    integration.save()

    verdict = integrations.probe(integration.provider_key)
    plain = [name for name in settings if name not in secret_keys]
    missing = verdict.get('missing') or []

    text = (
        f'{integration.name} configured: '
        f'{len(plain)} setting(s) ({", ".join(plain) or "none"}) and '
        f'{len(secret_keys)} credential(s) '
        f'({", ".join(sorted(secret_keys)) or "none"}, values not displayed).\n'
        f'Connection check: {verdict.get("status")} -- {verdict.get("message")}\n'
        f'Effective mode is now {verdict.get("effective_mode")}.'
    )
    if missing:
        text += f'\nStill missing: {", ".join(missing)}.'

    # ok reports whether the configuration was written, not whether the
    # credential turned out to work. A rejected token is a true and useful
    # outcome of a successful configuration change, and marking the action
    # failed would invite a retry that rewrites the same settings.
    return ToolResult(
        ok=True, text=text,
        data={'provider_key': integration.provider_key,
              'probe_ok': bool(verdict.get('ok')),
              'status': verdict.get('status'),
              'effective_mode': verdict.get('effective_mode'),
              'missing': missing,
              'settings_written': plain,
              'credentials_written': sorted(secret_keys)})


@tool(
    name='platform.set_integration_mode',
    title='Set an integration mode',
    description='Choose whether an integration really reaches the outside '
                'world. auto -- live when the credential is present, simulated '
                'when it is not. live -- live only, so a missing credential is '
                'reported as an error rather than quietly simulated. demo -- '
                'always simulated, even when a working credential is present, '
                'which is how the platform is demonstrated safely. This is the '
                'switch that decides whether an approved action leaves the '
                'company, so it goes to a person.',
    group='Integrations', shared=True, requires_approval=True, risk='medium',
    icon='fa-toggle-on', capability='Choose live or simulated behaviour',
    parameters={
        'type': 'object',
        'properties': {
            'provider_key': {'type': 'string',
                             'description': 'The integration key.'},
            'mode': {'type': 'string', 'enum': ['auto', 'live', 'demo'],
                     'description': 'auto, live or demo.'},
        },
        'required': ['provider_key', 'mode'],
    },
)
def set_integration_mode(ctx, provider_key, mode):
    """The single most consequential setting on an integration row.

    Moving Gmail from demo to live is the difference between a simulated
    message in a table and an email in a candidate's inbox. Nobody should be
    able to cross that line as a side effect of a chat message, which is why
    this is proposed rather than applied, even though it touches only one
    column of one row.
    """
    integration, _connector, failure = _integration_or_error(
        provider_key, verb='switch')
    if integration is None:
        return failure

    wanted = str(mode or '').strip().lower()
    if wanted not in ('auto', 'live', 'demo'):
        return ToolResult(
            ok=False, error=f'Unknown mode "{mode}".',
            text=f'"{mode}" is not a mode. Use auto, live or demo. '
                 f'{MODE_EXPLANATION}')

    if integration.mode == wanted:
        return ToolResult(
            ok=True,
            text=f'{integration.name} is already in {wanted} mode, so there is '
                 f'nothing to change. Its effective mode is '
                 f'{integration.effective_mode}.')

    consequence = {
        'auto': ('Actions will be live once every required credential is '
                 'present, and clearly labelled simulations until then.'),
        'live': ('Actions will reach the real service. A missing credential '
                 'will be reported as a failure rather than simulated.'),
        'demo': ('Every action will be simulated and labelled as such, even if '
                 'the credential works. Nothing will leave the company.'),
    }[wanted]

    missing = integration.missing_settings
    summary = (
        f'Change {integration.name} from {integration.mode} mode to {wanted} '
        f'mode.\n{consequence}\n'
        f'Currently configured: {_yes_no(not missing)}.'
        + (f' Missing: {", ".join(missing)}.' if missing else '')
    )

    return Proposal(
        title=f'Set {integration.name} to {wanted} mode',
        summary=summary,
        payload={'provider_key': integration.provider_key, 'mode': wanted},
        editable_fields=[editable('mode', 'Mode', 'text',
                                  'auto, live or demo.')],
        risk='medium',
        integration_key=integration.provider_key,
        subject_label='marketing.integration',
        subject_id=integration.pk,
        subject_display=integration.name,
    )


@executor('platform.set_integration_mode')
def execute_set_integration_mode(action):
    """Apply the mode and report what the integration will now actually do."""
    integrations = _integrations()
    payload = dict(action.payload or {})
    key = payload.get('provider_key', '')
    mode = str(payload.get('mode') or '').strip().lower()

    integration = integrations.get_integration(key)
    if integration is None:
        return ToolResult(ok=False, error=f'Integration {key} no longer exists.',
                          text=f'The {key} integration row no longer exists.')
    if mode not in ('auto', 'live', 'demo'):
        return ToolResult(ok=False, error=f'Bad mode "{mode}".',
                          text=f'"{mode}" is not a mode, so nothing changed.')

    previous = integration.mode
    integration.mode = mode
    if action.decided_by_id:
        integration.configured_by = action.decided_by
    integration.save(update_fields=['mode', 'configured_by', 'updated_at'])

    verdict = integrations.probe(integration.provider_key)
    return ToolResult(
        ok=True,
        text=(f'{integration.name} moved from {previous} to {mode} mode. '
              f'Effective mode is now {verdict.get("effective_mode")}. '
              f'{verdict.get("message")}'),
        data={'provider_key': integration.provider_key, 'mode': mode,
              'previous_mode': previous,
              'effective_mode': verdict.get('effective_mode')})


@tool(
    name='platform.enable_integration',
    title='Enable an integration',
    description='Switch a connected application back on so its tools work '
                'again. A disabled integration refuses every call rather than '
                'simulating it, so enabling one changes what the workforce can '
                'do and goes to a person.',
    group='Integrations', shared=True, requires_approval=True, risk='medium',
    icon='fa-power-off', capability='Switch a connected application on',
    parameters={
        'type': 'object',
        'properties': {
            'provider_key': {'type': 'string',
                             'description': 'The integration key.'},
        },
        'required': ['provider_key'],
    },
)
def enable_integration(ctx, provider_key):
    """Restore a switched-off integration.

    Kept separate from ``configure_integration`` because enabling is a decision
    on its own: a reviewer asked to approve "switch Slack back on" should not
    have to read a credential change to work out what they are agreeing to.
    """
    integration, _connector, failure = _integration_or_error(
        provider_key, verb='enable')
    if integration is None:
        return failure

    if integration.is_enabled:
        return ToolResult(
            ok=True,
            text=f'{integration.name} is already enabled. Effective mode: '
                 f'{integration.effective_mode}.')

    missing = integration.missing_settings
    return Proposal(
        title=f'Enable {integration.name}',
        summary=(
            f'Switch {integration.name} ({integration.provider_key}) back on. '
            f'Its mode setting is {integration.mode}, so once enabled it will '
            f'run '
            + ('live where configured and simulated where not.'
               if integration.mode == 'auto'
               else f'in {integration.mode} mode.')
            + (f'\nStill missing: {", ".join(missing)}.' if missing
               else '\nEverything required is configured.')),
        payload={'provider_key': integration.provider_key, 'enable': True},
        editable_fields=[],
        risk='medium',
        integration_key=integration.provider_key,
        subject_label='marketing.integration',
        subject_id=integration.pk,
        subject_display=integration.name,
    )


@tool(
    name='platform.disable_integration',
    title='Disable an integration',
    description='Switch a connected application off. Every call to it then '
                'fails with a clear message rather than being simulated, '
                'because switching something off should stop it, not quietly '
                'replace it with a pretend version. Use this to take an '
                'application out of service.',
    group='Integrations', shared=True, requires_approval=True, risk='medium',
    icon='fa-plug-circle-xmark',
    capability='Take a connected application out of service',
    parameters={
        'type': 'object',
        'properties': {
            'provider_key': {'type': 'string',
                             'description': 'The integration key.'},
        },
        'required': ['provider_key'],
    },
)
def disable_integration(ctx, provider_key):
    """Take an application out of service without losing its configuration.

    Disabling keeps the credential in place, so the integration can be restored
    without anybody hunting for the token again. That is deliberate: the usual
    reason to disable something is an incident, and an incident is the worst
    moment to also lose your configuration.
    """
    integration, _connector, failure = _integration_or_error(
        provider_key, verb='disable')
    if integration is None:
        return failure

    if not integration.is_enabled:
        return ToolResult(
            ok=True,
            text=f'{integration.name} is already disabled, so nothing to do.')

    tool_count = len([entry for entry in tool_base.all_tools()
                      if entry.integration == integration.provider_key])
    return Proposal(
        title=f'Disable {integration.name}',
        summary=(
            f'Switch {integration.name} ({integration.provider_key}) off. '
            f'{tool_count} tool(s) reach it, and each will fail with a clear '
            f'message rather than simulating. The configuration and the stored '
            f'credentials are kept, so it can be enabled again without '
            f're-entering anything.'),
        payload={'provider_key': integration.provider_key, 'enable': False},
        editable_fields=[],
        risk='medium',
        integration_key=integration.provider_key,
        subject_label='marketing.integration',
        subject_id=integration.pk,
        subject_display=integration.name,
    )


def _apply_enabled(action):
    """Shared execution for enable and disable: one column, one probe."""
    integrations = _integrations()
    payload = dict(action.payload or {})
    key = payload.get('provider_key', '')
    wanted = bool(payload.get('enable'))

    integration = integrations.get_integration(key)
    if integration is None:
        return ToolResult(ok=False, error=f'Integration {key} no longer exists.',
                          text=f'The {key} integration row no longer exists.')

    integration.is_enabled = wanted
    if action.decided_by_id:
        integration.configured_by = action.decided_by
    integration.save(update_fields=['is_enabled', 'configured_by', 'updated_at'])

    verdict = integrations.probe(integration.provider_key)
    word = 'enabled' if wanted else 'disabled'
    return ToolResult(
        ok=True,
        text=(f'{integration.name} is now {word}. Effective mode: '
              f'{verdict.get("effective_mode")}. {verdict.get("message")}'),
        data={'provider_key': integration.provider_key,
              'is_enabled': wanted,
              'effective_mode': verdict.get('effective_mode')})


executor('platform.enable_integration')(_apply_enabled)
executor('platform.disable_integration')(_apply_enabled)


@tool(
    name='platform.test_integration',
    title='Test a connection',
    description='Check one integration now and report its status, the reason '
                'for that status, its effective mode and anything still '
                'missing. A connection check reads only, so it happens '
                'immediately rather than going to the approval queue.',
    group='Integrations', shared=True, reads_only=True, icon='fa-vial',
    capability='Check whether a connected application works',
    parameters={
        'type': 'object',
        'properties': {
            'provider_key': {'type': 'string',
                             'description': 'The integration key.'},
        },
        'required': ['provider_key'],
    },
)
def test_integration(ctx, provider_key):
    """Confirm a connection before promising anything through it.

    This is immediate rather than approved because a probe changes nothing
    outside the platform: it asks the connector whether it could work, and
    stores the verdict on the row. Requiring approval for a diagnostic would
    make diagnosing anything a two-person job.
    """
    integration, _connector, failure = _integration_or_error(
        provider_key, verb='test')
    if integration is None:
        return failure

    integrations = _integrations()
    verdict = integrations.probe(integration.provider_key)
    missing = verdict.get('missing') or []

    text = (f'{integration.name} ({integration.provider_key}): '
            f'{verdict.get("status_display") or verdict.get("status")}\n'
            f'{verdict.get("message")}\n'
            f'Effective mode: {verdict.get("effective_mode")}. '
            f'Checked {verdict.get("checked_at")}.')
    if missing:
        text += (f'\nStill missing: {", ".join(missing)}. Ask a person for '
                 f'these and call platform.configure_integration.')

    return ToolResult(ok=bool(verdict.get('ok')), text=text, data=verdict,
                      subject_label='marketing.integration',
                      subject_id=integration.pk)


@tool(
    name='platform.test_all_integrations',
    title='Test every connection',
    description='Check every integration at once and return a table of status, '
                'effective mode and missing settings. Use this to answer "what '
                'is actually connected" in one call.',
    group='Integrations', shared=True, reads_only=True, icon='fa-list-check',
    capability='Check every connected application',
    parameters={'type': 'object', 'properties': {}},
)
def test_all_integrations(ctx):
    """The whole connection picture, which is what a person usually wants.

    Asked "is anything actually hooked up?", checking one integration at a time
    is six calls and a confused answer. One table settles it.
    """
    models = _platform_models()
    integrations = _integrations()

    rows = list(models.Integration.objects.all())
    if not rows:
        return ToolResult(
            ok=True,
            text='There are no integrations to test. Run '
                 'platform.ensure_defaults first.')

    lines = []
    data = {}
    tally = {}
    for integration in rows:
        verdict = integrations.probe(integration.provider_key)
        status = verdict.get('status', 'unknown')
        tally[status] = tally.get(status, 0) + 1
        missing = verdict.get('missing') or []
        lines.append(
            f'{integration.provider_key:<18} {status:<10} '
            f'mode {verdict.get("effective_mode"):<12} '
            f'{("missing: " + ", ".join(missing)) if missing else "ready"} '
            f'-- {_short(verdict.get("message"), 90)}')
        data[integration.provider_key] = verdict

    headline = ', '.join(f'{count} {status}'
                         for status, count in sorted(tally.items()))
    return ToolResult(
        ok=True,
        text=(f'Checked {len(rows)} integration(s): {headline}.\n' +
              '\n'.join(lines)),
        data={'results': data, 'by_status': tally})


@tool(
    name='platform.list_integration_operations',
    title='List an integration operations',
    description='The operations one connector advertises, so you can tell what '
                'is possible through an application before proposing something '
                'it cannot do.',
    group='Integrations', shared=True, reads_only=True, icon='fa-diagram-next',
    capability='See what an application can be asked to do',
    parameters={
        'type': 'object',
        'properties': {
            'provider_key': {'type': 'string',
                             'description': 'The integration key.'},
        },
        'required': ['provider_key'],
    },
)
def list_integration_operations(ctx, provider_key):
    """What this application can actually be asked to do.

    An employee that proposes "post this to the Slack canvas" when the
    connector only knows how to post a message has wasted a reviewer's time
    with an action that cannot execute. Reading the operation list first turns
    that into a straight answer about what is possible.
    """
    integration, connector, failure = _integration_or_error(
        provider_key, verb='inspect')
    if failure is not None:
        return failure

    operations = _operation_rows(connector)
    tools_reaching = [entry for entry in tool_base.all_tools()
                      if entry.integration == integration.provider_key]

    lines = [f'- {name}: {description}' if description else f'- {name}'
             for name, description in operations]
    tool_lines = [
        f'- {entry.name} ({entry.title})'
        + (' -- needs approval' if entry.requires_approval else ' -- immediate')
        for entry in tools_reaching]

    return ToolResult(
        ok=True,
        text=(f'{integration.name} advertises {len(operations)} operation(s):\n'
              + ('\n'.join(lines) if lines else '- none declared') +
              f'\n\nWorkforce tools that reach {integration.name} '
              f'({len(tool_lines)}):\n'
              + ('\n'.join(tool_lines) if tool_lines
                 else '- none; nothing calls this integration yet') +
              f'\n\nEffective mode: {integration.effective_mode}.'),
        data={'provider_key': integration.provider_key,
              'operations': [{'name': name, 'description': description}
                             for name, description in operations],
              'tools': [entry.name for entry in tools_reaching]})


@tool(
    name='platform.integration_usage',
    title='Integration usage',
    description='How much an integration has been used: total calls split into '
                'live and simulated, when it was last used, and the recent '
                'audit entries for it. Omit provider_key for every '
                'integration. Use this to answer whether work really went out '
                'or was only simulated.',
    group='Integrations', shared=True, reads_only=True, icon='fa-chart-simple',
    capability='See how much a connected application has been used',
    parameters={
        'type': 'object',
        'properties': {
            'provider_key': {'type': 'string',
                             'description': 'Optional. One integration key; '
                                            'omit for all of them.'},
            'days': {'type': 'integer',
                     'description': 'How many days of audit history to include. '
                                    'Default 30.'},
        },
    },
)
def integration_usage(ctx, provider_key='', days=30):
    """The live-versus-simulated split, which is the honest usage question.

    "Slack was called forty times" means nothing on its own. Forty live calls is
    forty messages in a channel; forty simulated calls is a demonstration.
    Keeping the two apart in every report is what stops a simulated action
    being read later as a real one.
    """
    from django.utils import timezone

    models = _platform_models()

    try:
        window = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        window = 30
    since = timezone.now() - timedelta(days=window)

    if provider_key:
        integration, _connector, failure = _integration_or_error(
            provider_key, verb='report on')
        if integration is None:
            return failure
        rows = [integration]
    else:
        rows = list(models.Integration.objects.all())

    if not rows:
        return ToolResult(
            ok=True, text='No integrations exist yet, so there is no usage to '
                          'report. Run platform.ensure_defaults.')

    lines = []
    data = []
    for integration in rows:
        last_used = (integration.last_used_at.strftime('%d %b %Y at %H:%M')
                     if integration.last_used_at else 'never')
        lines.append(
            f'{integration.provider_key} -- {integration.call_count} call(s): '
            f'{integration.live_call_count} live, '
            f'{integration.demo_call_count} simulated. '
            f'Last used: {last_used}. Effective mode: '
            f'{integration.effective_mode}.')
        data.append({
            'provider_key': integration.provider_key,
            'calls': integration.call_count,
            'live_calls': integration.live_call_count,
            'demo_calls': integration.demo_call_count,
            'last_used_at': last_used,
            'effective_mode': integration.effective_mode,
        })

    events = models.AuditEvent.objects.filter(
        integration__in=rows, created_at__gte=since).select_related(
            'integration', 'agent')[:20]
    event_lines = [
        f'- {event.created_at.strftime("%d %b %H:%M")} '
        f'{event.integration.provider_key if event.integration_id else "-"} '
        f'{event.action} [{event.get_status_display()}] by {event.actor_label}: '
        f'{_short(event.message, 110)}'
        for event in events]

    return ToolResult(
        ok=True,
        text=('\n'.join(lines) +
              f'\n\nAudit entries in the last {window} day(s) '
              f'({len(event_lines)} shown):\n' +
              ('\n'.join(event_lines) if event_lines
               else '- none recorded in this window')),
        data={'integrations': data, 'days': window,
              'audit_events': len(event_lines)})


# ===========================================================================
# 2. PLATFORM SETTINGS -- configuration as data
# ===========================================================================

@tool(
    name='platform.list_settings',
    title='List platform settings',
    description='Every platform setting with its label, group, type and current '
                'value. Secret settings show as (set) or (not set) and never '
                'reveal their value. Optionally narrow to one group: company, '
                'behaviour, approval, knowledge, notification.',
    group='Platform Settings', shared=True, reads_only=True, icon='fa-sliders',
    capability='See how the platform is configured',
    parameters={
        'type': 'object',
        'properties': {
            'group': {'type': 'string',
                      'description': 'Optional group filter: company, '
                                     'behaviour, approval, knowledge, '
                                     'notification.'},
        },
    },
)
def list_settings(ctx, group=''):
    """Everything about how the platform behaves, in one readable list.

    These rows exist so that none of it lives in a source file. An employee can
    read them and, with approval, change them, which is the whole of the claim
    that the platform is configurable through the agents as well as through the
    interface.
    """
    models = _platform_models()
    rows = models.SystemSetting.objects.all()

    wanted = str(group or '').strip().lower()
    if wanted:
        valid = [choice for choice, _label in models.SystemSetting.GROUP_CHOICES]
        if wanted not in valid:
            return ToolResult(
                ok=False, error=f'Unknown group "{group}".',
                text=f'"{group}" is not a settings group. Valid groups are: '
                     f'{", ".join(valid)}.')
        rows = rows.filter(group=wanted)

    rows = list(rows)
    if not rows:
        return ToolResult(
            ok=True,
            text=('No settings exist yet. Run platform.ensure_defaults to '
                  'create the defaults.' if not wanted else
                  f'No settings in the {wanted} group.'))

    lines = []
    data = []
    for row in rows:
        display = _mask_setting(row)
        lines.append(f'{row.key} ({row.get_group_display()}) -- {row.label} '
                     f'[{row.get_value_type_display()}]: {_short(display, 160)}'
                     + (f'. Choices: {", ".join(str(c) for c in row.choices)}'
                        if row.choices else ''))
        data.append({'key': row.key, 'label': row.label, 'group': row.group,
                     'value_type': row.value_type, 'display': display,
                     'is_secret': row.is_secret,
                     'choices': list(row.choices or [])})

    return ToolResult(
        ok=True,
        text=(f'{len(rows)} setting(s).\n' + '\n'.join(lines) +
              '\n\nUse platform.get_setting for one setting with its full '
              'description, and platform.update_setting to change one.'),
        data={'settings': data})


@tool(
    name='platform.get_setting',
    title='Get one platform setting',
    description='One setting with its label, group, type, description, allowed '
                'choices and current value. A secret setting reports only '
                'whether it is set.',
    group='Platform Settings', shared=True, reads_only=True, icon='fa-gear',
    capability='Read one platform setting',
    parameters={
        'type': 'object',
        'properties': {
            'key': {'type': 'string',
                    'description': "The setting key, for example "
                                   "'company_timezone'."},
        },
        'required': ['key'],
    },
)
def get_setting(ctx, key):
    """One setting, with the description that says what it is for.

    The description matters more than the value. An employee reading
    ``require_approval_for_external`` needs to know that turning it off is not
    supported, and that sentence lives on the row rather than in anybody's
    memory.
    """
    models = _platform_models()
    wanted = str(key or '').strip()
    row = models.SystemSetting.objects.filter(key=wanted).first()
    if row is None:
        available = ', '.join(
            models.SystemSetting.objects.values_list('key', flat=True))
        return ToolResult(
            ok=False, error=f'No setting called "{wanted}".',
            text=f'There is no setting called "{wanted}". Existing keys are: '
                 f'{available or "none -- run platform.ensure_defaults"}.')

    display = _mask_setting(row)
    text = (f'{row.key} -- {row.label}\n'
            f'Group: {row.get_group_display()}. Type: '
            f'{row.get_value_type_display()}.\n'
            f'Current value: {display}\n'
            f'{row.description}')
    if row.choices:
        text += f'\nAllowed values: {", ".join(str(c) for c in row.choices)}'
    if row.updated_by_id:
        text += (f'\nLast changed by {row.updated_by.get_username()} on '
                 f'{row.updated_at:%d %b %Y at %H:%M}.')

    return ToolResult(ok=True, text=text, data={
        'key': row.key, 'label': row.label, 'group': row.group,
        'value_type': row.value_type, 'display': display,
        'description': row.description, 'is_secret': row.is_secret,
        'choices': list(row.choices or [])})


@tool(
    name='platform.update_setting',
    title='Update a platform setting',
    description='Change one platform setting -- company name, timezone, working '
                'hours, signature, brand tone, approval policy. The value is '
                'validated against the setting type before it is proposed, so a '
                'bad value is reported now rather than stored. Goes to a person '
                'because it changes how every employee behaves.',
    group='Platform Settings', shared=True, requires_approval=True,
    risk='medium', icon='fa-pen-to-square',
    capability='Change how the platform behaves',
    parameters={
        'type': 'object',
        'properties': {
            'key': {'type': 'string', 'description': 'The setting key.'},
            'value': {'description': 'The new value. A number for a number '
                                     'setting, true or false for a yes/no '
                                     'setting, one of the listed choices for a '
                                     'choice setting, valid JSON for a JSON '
                                     'setting.'},
        },
        'required': ['key', 'value'],
    },
)
def update_setting(ctx, key, value):
    """Change the platform's own behaviour, with the type checked first.

    Validation happens here rather than in the executor on purpose. A reviewer
    should never be shown a proposal that cannot succeed, and the employee that
    typed ``working_hours = "nine to five"`` should be told immediately that a
    JSON object was expected, while it still has the conversation in front of it
    to ask a better question.
    """
    models = _platform_models()
    wanted = str(key or '').strip()
    row = models.SystemSetting.objects.filter(key=wanted).first()
    if row is None:
        available = ', '.join(
            models.SystemSetting.objects.values_list('key', flat=True))
        return ToolResult(
            ok=False, error=f'No setting called "{wanted}".',
            text=f'There is no setting called "{wanted}". Existing keys are: '
                 f'{available or "none -- run platform.ensure_defaults"}.')

    if row.value_type == 'number':
        try:
            cleaned = _as_number(value)
        except (TypeError, ValueError) as exc:
            return ToolResult(
                ok=False, error='Bad number.',
                text=f'{row.label} ({row.key}) expects a number: {exc} '
                     f'You supplied "{_short(value, 60)}".')
    elif row.value_type == 'boolean':
        try:
            cleaned = _as_bool(value)
        except (TypeError, ValueError) as exc:
            return ToolResult(
                ok=False, error='Bad yes/no value.',
                text=f'{row.label} ({row.key}) expects true or false: {exc}')
    elif row.value_type == 'choice':
        allowed = [choice[0] if isinstance(choice, (list, tuple)) else choice
                   for choice in (row.choices or [])]
        cleaned = str(value).strip()
        if allowed and cleaned not in [str(item) for item in allowed]:
            return ToolResult(
                ok=False, error='Not an allowed choice.',
                text=f'{row.label} ({row.key}) must be one of: '
                     f'{", ".join(str(item) for item in allowed)}. You supplied '
                     f'"{cleaned}".')
    elif row.value_type == 'json':
        if isinstance(value, (dict, list)):
            cleaned = value
        else:
            try:
                cleaned = json.loads(str(value))
            except (TypeError, ValueError) as exc:
                return ToolResult(
                    ok=False, error='Bad JSON.',
                    text=f'{row.label} ({row.key}) expects valid JSON and could '
                         f'not parse what you supplied ({exc}). Example for '
                         f'working_hours: {{"start": "09:00", "end": "17:00", '
                         f'"days": ["Mon", "Tue"]}}.')
    else:
        cleaned = str(value)

    previous_display = _mask_setting(row)
    if row.is_secret:
        new_display = f'(a {len(str(cleaned))}-character value will be stored)'
    elif isinstance(cleaned, bool):
        new_display = _yes_no(cleaned)
    elif isinstance(cleaned, (dict, list)):
        new_display = json.dumps(cleaned)
    else:
        new_display = str(cleaned)

    if not row.is_secret and str(previous_display) == str(new_display):
        return ToolResult(
            ok=True,
            text=f'{row.label} ({row.key}) is already {new_display}, so there '
                 f'is nothing to change.')

    field_type = 'longtext' if row.value_type in ('longtext', 'json') else 'text'

    return Proposal(
        title=f'Set {row.label} to {_short(new_display, 60)}',
        summary=(f'{row.label} ({row.key}), group '
                 f'{row.get_group_display()}, type '
                 f'{row.get_value_type_display()}.\n'
                 f'{row.description}\n\n'
                 f'Current: {_short(previous_display, 300)}\n'
                 f'Proposed: {_short(new_display, 300)}'),
        payload={'key': row.key, 'value': cleaned,
                 'value_type': row.value_type,
                 '_secret_keys': ['value'] if row.is_secret else []},
        # A secret setting gets no editable field: rendering one would put the
        # stored value back into a form, which is exactly what this module
        # exists to avoid. A reviewer who needs to change a credential rejects
        # the action and sets it themselves.
        editable_fields=([] if row.is_secret else
                         [editable('value', row.label, field_type,
                                   row.description)]),
        risk='medium',
        subject_label='marketing.systemsetting',
        subject_id=row.pk,
        subject_display=row.label,
    )


@executor('platform.update_setting')
def execute_update_setting(action):
    """Write the value in the envelope the model's ``resolved`` expects.

    ``SystemSetting.value`` is a JSONField, and a bare string or number is not
    reliably a JSON document on every backend. Wrapping every value as
    ``{'v': value}`` gives one shape for all six types, which is exactly what
    ``resolved`` unwraps.
    """
    models = _platform_models()
    payload = dict(action.payload or {})
    key = payload.get('key', '')

    row = models.SystemSetting.objects.filter(key=key).first()
    if row is None:
        return ToolResult(
            ok=False, error=f'Setting {key} no longer exists.',
            text=f'The setting "{key}" no longer exists, so nothing changed.')

    previous_display = _mask_setting(row)
    row.value = {'v': payload.get('value')}
    if action.decided_by_id:
        row.updated_by = action.decided_by
    row.save(update_fields=['value', 'updated_by', 'updated_at'])

    new_display = _mask_setting(row)
    return ToolResult(
        ok=True,
        text=(f'{row.label} ({row.key}) changed from '
              f'{_short(previous_display, 120)} to {_short(new_display, 120)}. '
              f'Every employee reads the new value on its next call.'),
        data={'key': row.key, 'display': new_display},
        subject_label='marketing.systemsetting', subject_id=row.pk)


@tool(
    name='platform.company_profile',
    title='Company profile',
    description='Who this workforce works for: company name, industry, '
                'website, timezone, working hours, brand tone and email '
                'signature. Call this before writing anything on the company '
                'behalf so the name, the tone and the timezone are right.',
    group='Platform Settings', shared=True, reads_only=True,
    icon='fa-building', capability='Know which company this workforce serves',
    parameters={'type': 'object', 'properties': {}},
)
def company_profile(ctx):
    """The facts every employee needs and none of them should invent.

    This is shared for a plain reason: the Support employee apologising to a
    customer, the HR employee inviting a candidate and the Marketing employee
    writing a post all have to name the same company in the same voice and
    quote the same timezone. One source, six readers, no guessing.
    """
    keys = ['company_name', 'company_industry', 'company_website',
            'company_timezone', 'working_hours', 'brand_tone',
            'email_signature']
    models = _platform_models()
    rows = {row.key: row for row in
            models.SystemSetting.objects.filter(key__in=keys)}

    missing_rows = [key for key in keys if key not in rows]
    profile = {}
    for key in keys:
        row = rows.get(key)
        profile[key] = row.resolved if row is not None else _setting_value(key)

    hours = profile.get('working_hours') or {}
    if isinstance(hours, dict):
        hours_text = (f'{hours.get("start", "?")} to {hours.get("end", "?")}, '
                      f'{", ".join(hours.get("days") or []) or "days not set"}')
    else:
        hours_text = _short(hours)

    text = (
        f'Company: {profile.get("company_name") or "(not set)"}\n'
        f'Industry: {profile.get("company_industry") or "(not set)"}\n'
        f'Website: {profile.get("company_website") or "(not set)"}\n'
        f'Timezone: {profile.get("company_timezone") or "(not set)"} '
        f'-- state this in any time you write\n'
        f'Working hours: {hours_text}\n'
        f'Brand tone: {profile.get("brand_tone") or "(not set)"}\n'
        f'Email signature:\n{profile.get("email_signature") or "(not set)"}'
    )
    if missing_rows:
        text += (f'\n\nThese settings have no row yet: '
                 f'{", ".join(missing_rows)}. Run platform.ensure_defaults, '
                 f'then platform.update_setting to fill them in.')

    return ToolResult(ok=True, text=text, data={'profile': profile,
                                                'working_hours': hours,
                                                'missing': missing_rows})


@tool(
    name='platform.ensure_defaults',
    title='Create missing defaults',
    description='Create any missing default platform setting and any missing '
                'integration row, then report what was added. Safe to run at '
                'any time: it only creates rows the platform already assumes '
                'exist and never overwrites a configured value.',
    group='Platform Settings', shared=True, icon='fa-wand-magic-sparkles',
    capability='Provision the platform defaults',
    parameters={'type': 'object', 'properties': {}},
)
def ensure_defaults(ctx):
    """Make the platform's own assumptions true.

    Code all over the project reads ``company_timezone`` and
    ``agent_memory_enabled`` and assumes a row exists. Rather than scatter
    defaults through every reader, this creates the rows once. It is immediate
    rather than approved because it only adds what is already assumed, and it
    never touches a value somebody has set -- so running it twice is the same
    as running it once, and running it after a change loses nothing.
    """
    models = _platform_models()
    integrations = _integrations()

    added_settings = []
    refreshed = []
    for spec in DEFAULT_SETTINGS:
        row, created = models.SystemSetting.objects.get_or_create(
            key=spec['key'],
            defaults={
                'label': spec['label'],
                'description': spec['description'],
                'group': spec['group'],
                'value_type': spec['value_type'],
                'value': {'v': spec['value']},
                'choices': list(spec['choices']),
                'display_order': spec['display_order'],
                'is_secret': spec['is_secret'],
            },
        )
        if created:
            added_settings.append(row.key)
            continue
        # Keep the explanatory metadata in step with this module, but never
        # touch ``value``: that belongs to whoever set it.
        changed = []
        for attribute, expected in (('label', spec['label']),
                                    ('description', spec['description']),
                                    ('group', spec['group']),
                                    ('value_type', spec['value_type']),
                                    ('choices', list(spec['choices'])),
                                    ('display_order', spec['display_order'])):
            if getattr(row, attribute) != expected:
                setattr(row, attribute, expected)
                changed.append(attribute)
        if changed:
            row.save(update_fields=changed + ['updated_at'])
            refreshed.append(row.key)

    created_integrations = integrations.ensure_integrations(
        owner=ctx.user if getattr(ctx.user, 'pk', None) else None)
    integration_keys = [row.provider_key for row in created_integrations]

    lines = [
        f'Settings created ({len(added_settings)}): '
        f'{", ".join(added_settings) or "none, all present"}',
        f'Setting descriptions refreshed ({len(refreshed)}): '
        f'{", ".join(refreshed) or "none"}',
        f'Integration rows created ({len(integration_keys)}): '
        f'{", ".join(integration_keys) or "none, all present"}',
        f'Totals now: '
        f'{models.SystemSetting.objects.count()} setting(s), '
        f'{models.Integration.objects.count()} integration(s).',
    ]

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'settings_created': added_settings,
              'settings_refreshed': refreshed,
              'integrations_created': integration_keys})


# ===========================================================================
# 3. MEMORY -- what an employee carries between conversations
# ===========================================================================

@tool(
    name='platform.remember',
    title='Remember something',
    description='Record something durable so it survives this conversation: a '
                'fact, a preference, an entity, a summary or a working rule. '
                "Use scope 'agent' to keep it to yourself and 'shared' to give "
                'it to the whole workforce. Importance runs 1 (trivia) to 10 '
                '(essential) and decides what surfaces first on recall. '
                'Recording the same key again updates it rather than '
                'duplicating it.',
    group='Memory', shared=True, icon='fa-brain',
    capability='Remember something between conversations',
    parameters={
        'type': 'object',
        'properties': {
            'key': {'type': 'string',
                    'description': 'A short handle, for example '
                                   '"q3_headcount_plan".'},
            'content': {'type': 'string',
                        'description': 'What to remember, written so it still '
                                       'makes sense in a month.'},
            'kind': {'type': 'string',
                     'enum': ['fact', 'preference', 'entity', 'summary',
                              'policy'],
                     'description': 'Default fact.'},
            'importance': {'type': 'integer',
                           'description': '1 to 10. Default 5.'},
            'scope': {'type': 'string', 'enum': ['agent', 'shared'],
                      'description': "Default 'agent'. Use 'shared' for "
                                     'something every employee should know.'},
        },
        'required': ['key', 'content'],
    },
)
def remember(ctx, key, content, kind='fact', importance=5, scope='agent'):
    """The difference between a colleague and a chat window.

    Without this an employee re-learns everything every morning: it asks again
    which timezone the interviews are in, forgets that a named customer has an
    open escalation, and cannot be told a working rule that sticks. A memory row
    is small and cheap, and it is the only reason a second conversation is
    better informed than the first.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    models = _platform_models()

    if not _setting_value('agent_memory_enabled', True):
        return ToolResult(
            ok=False, error='Memory is switched off.',
            text='Employee memory is switched off in the platform settings '
                 '(agent_memory_enabled), so nothing was recorded. A person can '
                 'turn it back on from Settings, or you can propose the change '
                 'with platform.update_setting.')

    handle = str(key or '').strip()[:160]
    if not handle:
        return ToolResult(ok=False, error='No key given.',
                          text='A memory needs a short key to be recalled by, '
                               'for example "refund_policy_owner".')
    body = str(content or '').strip()
    if not body:
        return ToolResult(ok=False, error='No content given.',
                          text=f'Nothing was recorded for "{handle}" because '
                               f'the content was empty.')

    wanted_kind = str(kind or 'fact').strip().lower()
    valid_kinds = [choice for choice, _label in models.AgentMemory.KIND_CHOICES]
    if wanted_kind not in valid_kinds:
        return ToolResult(
            ok=False, error=f'Unknown kind "{kind}".',
            text=f'"{kind}" is not a memory kind. Use one of: '
                 f'{", ".join(valid_kinds)}.')

    wanted_scope = str(scope or 'agent').strip().lower()
    if wanted_scope not in ('agent', 'shared'):
        return ToolResult(
            ok=False, error=f'Unknown scope "{scope}".',
            text=f'"{scope}" is not a scope. Use agent (yours alone) or shared '
                 f'(the whole workforce).')

    try:
        weight = max(1, min(int(importance), 10))
    except (TypeError, ValueError):
        weight = 5

    owner = None if wanted_scope == 'shared' else ctx.agent

    row, created = models.AgentMemory.objects.update_or_create(
        agent=owner, key=handle,
        defaults={
            'scope': wanted_scope,
            'kind': wanted_kind,
            'content': body,
            'importance': weight,
            'conversation': ctx.conversation,
            'created_by': ctx.user if getattr(ctx.user, 'pk', None) else None,
            'source': (f'{ctx.agent.name} while working on '
                       f'{ctx.task.title}' if ctx.task else ctx.agent.name)[:200],
        },
    )

    where = ('shared with the whole workforce' if wanted_scope == 'shared'
             else f'kept by {ctx.agent.name}')
    return ToolResult(
        ok=True,
        text=(f'{"Recorded" if created else "Updated"} memory #{row.pk} '
              f'"{row.key}" ({row.get_kind_display()}, importance {weight}, '
              f'{where}). Recall it with platform.recall.'),
        data={'memory_id': row.pk, 'key': row.key, 'kind': row.kind,
              'scope': row.scope, 'importance': weight, 'created': created},
        subject_label='marketing.agentmemory', subject_id=row.pk)


@tool(
    name='platform.recall',
    title='Recall memories',
    description='Search what you remember, plus everything shared across the '
                'workforce, by term overlap on the key and the content. Omit '
                'the query to get your most important memories. Optionally '
                'narrow by kind: fact, preference, entity, summary, policy.',
    group='Memory', shared=True, reads_only=True, icon='fa-lightbulb',
    capability='Recall what was learned earlier',
    parameters={
        'type': 'object',
        'properties': {
            'query': {'type': 'string',
                      'description': 'What you are trying to remember. Omit for '
                                     'your most important memories.'},
            'limit': {'type': 'integer', 'description': 'Default 8.'},
            'kind': {'type': 'string',
                     'enum': ['fact', 'preference', 'entity', 'summary',
                              'policy'],
                     'description': 'Optional kind filter.'},
        },
    },
)
def recall(ctx, query='', limit=8, kind=''):
    """Look before asking, so an employee does not ask twice.

    Shared memories are searched alongside the employee's own because a working
    rule set for the workforce is useless if only the employee that recorded it
    can find it. Scoring is term overlap rather than anything clever: it is
    deterministic, it needs no model call, and it is easy to explain when it
    misses.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    from django.db import models as django_models
    from django.utils import timezone

    models = _platform_models()

    try:
        count = max(1, min(int(limit), 40))
    except (TypeError, ValueError):
        count = 8

    rows = models.AgentMemory.objects.filter(
        django_models.Q(agent=ctx.agent) | django_models.Q(agent__isnull=True)
        | django_models.Q(scope='shared'))

    wanted_kind = str(kind or '').strip().lower()
    if wanted_kind:
        valid_kinds = [choice for choice, _label
                       in models.AgentMemory.KIND_CHOICES]
        if wanted_kind not in valid_kinds:
            return ToolResult(
                ok=False, error=f'Unknown kind "{kind}".',
                text=f'"{kind}" is not a memory kind. Use one of: '
                     f'{", ".join(valid_kinds)}.')
        rows = rows.filter(kind=wanted_kind)

    rows = list(rows.distinct())
    if not rows:
        return ToolResult(
            ok=True,
            text=f'{ctx.agent.name} has no memories recorded yet'
                 + (f' of kind {wanted_kind}.' if wanted_kind else '.')
                 + ' Record one with platform.remember.')

    terms = _terms(query)
    scored = []
    for row in rows:
        if terms:
            key_terms = _terms(row.key.replace('_', ' '))
            content_terms = _terms(row.content)
            score = sum(3 for term in terms if term in key_terms)
            score += sum(1 for term in terms if term in content_terms)
            score += sum(2 for term in terms
                         if any(term in word or word in term
                                for word in key_terms))
            if score <= 0:
                continue
        else:
            score = 0
        scored.append((score, row.importance, row))

    if not scored:
        return ToolResult(
            ok=True,
            text=f'Nothing recalled for "{query}". {len(rows)} memory row(s) '
                 f'were searched (yours plus shared) and none mentioned those '
                 f'terms. Say plainly that this is not something you were told, '
                 f'rather than guessing.',
            data={'searched': len(rows), 'matches': 0})

    scored.sort(key=lambda entry: (-entry[0], -entry[1],
                                   entry[2].updated_at), reverse=False)
    chosen = scored[:count]

    lines = []
    data = []
    for score, _weight, row in chosen:
        owner = 'shared' if row.agent_id is None else 'yours'
        lines.append(
            f'#{row.pk} {row.key} [{row.get_kind_display()}, importance '
            f'{row.importance}, {owner}, recalled {row.recall_count} time(s)]: '
            f'{row.content}')
        data.append({'memory_id': row.pk, 'key': row.key, 'kind': row.kind,
                     'scope': row.scope, 'importance': row.importance,
                     'content': row.content, 'score': score})

    models.AgentMemory.objects.filter(
        pk__in=[row.pk for _score, _weight, row in chosen]).update(
            recall_count=django_models.F('recall_count') + 1,
            last_recalled_at=timezone.now())

    return ToolResult(
        ok=True,
        text=(f'{len(chosen)} memory row(s) recalled'
              + (f' for "{query}"' if query else ' by importance') + ':\n'
              + '\n'.join(lines)),
        data={'memories': data, 'searched': len(rows)})


@tool(
    name='platform.forget',
    title='Forget a memory',
    description='Delete one of your own memories by its key. Use this when '
                'something you recorded has turned out to be wrong or out of '
                'date -- a stale memory is worse than none, because it is '
                'quoted with confidence. Shared memories cannot be deleted '
                'this way.',
    group='Memory', shared=True, icon='fa-eraser',
    capability='Forget something that is no longer true',
    parameters={
        'type': 'object',
        'properties': {
            'key': {'type': 'string',
                    'description': 'The key of the memory to delete.'},
        },
        'required': ['key'],
    },
)
def forget(ctx, key):
    """Remove a memory that has gone stale, and record that it happened.

    Deletion is immediate because a wrong memory keeps being repeated for as
    long as it exists, and waiting for approval to stop repeating a wrong fact
    would be the wrong trade. It is audited, though: the run wrapper writes an
    audit entry for every call, and this one also names what was removed, so a
    memory cannot vanish without trace.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    models = _platform_models()
    audit = _audit()

    handle = str(key or '').strip()
    row = models.AgentMemory.objects.filter(agent=ctx.agent, key=handle).first()
    if row is None:
        shared = models.AgentMemory.objects.filter(
            agent__isnull=True, key=handle).first()
        if shared is not None:
            return ToolResult(
                ok=False, error='That memory is shared.',
                text=f'"{handle}" is a shared memory belonging to the whole '
                     f'workforce, not to {ctx.agent.name}, so it cannot be '
                     f'deleted here. Ask a person to remove it from the '
                     f'interface if it is wrong.')
        own = list(models.AgentMemory.objects.filter(
            agent=ctx.agent).values_list('key', flat=True)[:25])
        return ToolResult(
            ok=False, error=f'No memory called "{handle}".',
            text=f'{ctx.agent.name} has no memory with the key "{handle}". '
                 f'Its keys are: {", ".join(own) or "none"}.')

    detail = {'key': row.key, 'kind': row.kind,
              'importance': row.importance, 'content': row.content[:400]}
    memory_id = row.pk
    row.delete()

    audit.log('memory.forgotten', category='agent', status='ok',
              actor=ctx.user, agent=ctx.agent,
              message=f'Deleted memory "{handle}".',
              object_label=handle, object_ref=f'agentmemory#{memory_id}',
              task=ctx.task, conversation=ctx.conversation, detail=detail)

    return ToolResult(
        ok=True,
        text=f'Deleted memory #{memory_id} "{handle}". {ctx.agent.name} will no '
             f'longer recall it, and the deletion is in the audit log.',
        data={'memory_id': memory_id, 'key': handle})


@tool(
    name='platform.list_memories',
    title='List memories',
    description='Everything you remember, newest and most important first, '
                "with keys so they can be recalled or forgotten. Scope 'agent' "
                "for your own, 'shared' for workforce-wide, blank for both.",
    group='Memory', shared=True, reads_only=True, icon='fa-list',
    capability='List what is remembered',
    parameters={
        'type': 'object',
        'properties': {
            'scope': {'type': 'string', 'enum': ['agent', 'shared'],
                      'description': 'Optional. Blank for both.'},
            'limit': {'type': 'integer', 'description': 'Default 30.'},
        },
    },
)
def list_memories(ctx, scope='', limit=30):
    """An inventory, so an employee can audit what it thinks it knows.

    Recall answers a question; this answers "what have I been told at all",
    which is what somebody needs before they can correct it.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    from django.db import models as django_models

    models = _platform_models()

    try:
        count = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        count = 30

    wanted = str(scope or '').strip().lower()
    if wanted and wanted not in ('agent', 'shared'):
        return ToolResult(
            ok=False, error=f'Unknown scope "{scope}".',
            text=f'"{scope}" is not a scope. Use agent, shared, or leave it '
                 f'blank for both.')

    rows = models.AgentMemory.objects.all()
    if wanted == 'agent':
        rows = rows.filter(agent=ctx.agent)
    elif wanted == 'shared':
        rows = rows.filter(django_models.Q(agent__isnull=True)
                           | django_models.Q(scope='shared'))
    else:
        rows = rows.filter(django_models.Q(agent=ctx.agent)
                           | django_models.Q(agent__isnull=True)
                           | django_models.Q(scope='shared'))

    rows = list(rows.distinct()[:count])
    if not rows:
        return ToolResult(
            ok=True,
            text=f'{ctx.agent.name} has nothing recorded'
                 + (f' in the {wanted} scope.' if wanted else ' yet.')
                 + ' Use platform.remember to record something.')

    lines = [
        f'#{row.pk} {row.key} [{row.get_kind_display()}, importance '
        f'{row.importance}, {"shared" if row.agent_id is None else "yours"}]: '
        f'{_short(row.content, 200)}'
        for row in rows]

    return ToolResult(
        ok=True,
        text=f'{len(rows)} memory row(s):\n' + '\n'.join(lines),
        data={'memories': [{'memory_id': row.pk, 'key': row.key,
                            'kind': row.kind, 'scope': row.scope,
                            'importance': row.importance} for row in rows]})


# ===========================================================================
# 4. COLLABORATION -- one employee asking another
# ===========================================================================

# Argument names that plainly take a question or a phrase to search for. A
# delegated request is only ever passed into one of these, which is what keeps
# delegation from filling in a field it does not understand.
_REQUEST_PARAMETERS = ('query', 'question', 'text', 'topic', 'term', 'terms',
                       'keywords', 'search', 'q', 'request', 'prompt', 'need')

# Tool names delegation must never choose, whatever it scores. The first is
# recursion; the rest write or delete something, and a colleague answering a
# question should not be able to change the platform as a side effect.
_DELEGATION_BLOCKED = {
    'platform.delegate_to', 'platform.remember', 'platform.forget',
    'platform.cancel_action', 'platform.ensure_defaults',
}

# Name fragments that mark a tool as changing something rather than reading it.
# Approval-requiring tools are already excluded; this catches the immediate
# writers, which are the dangerous ones here.
_WRITE_FRAGMENTS = ('create', 'add', 'delete', 'remove', 'update', 'set_',
                    'assign', 'configure', 'enable', 'disable', 'ensure',
                    'close', 'escalate', 'publish', 'send', 'post', 'schedule',
                    'file_', 'cancel', 'forget', 'import', 'sync', 'upload',
                    'write', 'draft', 'compose', 'generate', 'record', 'save',
                    'notify', 'invite', 'book', 'approve', 'reject', 'delegate',
                    'reassign', 'plan', 'break', 'index', 'apply')

_READ_FRAGMENTS = ('list', 'get', 'search', 'find', 'describe', 'recall',
                   'report', 'status', 'summar', 'analyse', 'analyze',
                   'review', 'check', 'score', 'read', 'overview', 'profile',
                   'answer', 'explain', 'lookup')


def _capability_groups(agent_type):
    """The headings under which one employee's tools sit, most-populated first."""
    counts = {}
    for entry in tool_base.tools_for(agent_type):
        counts[entry.group] = counts.get(entry.group, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


@tool(
    name='platform.list_colleagues',
    title='List colleagues',
    description='The other AI employees, with their role, what they do and the '
                'headings their capabilities sit under. Read this before '
                'refusing a request that is somebody else job -- delegate it '
                'with platform.delegate_to instead.',
    group='Collaboration', shared=True, reads_only=True, icon='fa-users',
    capability='Know who the other AI employees are',
    parameters={'type': 'object', 'properties': {}},
)
def list_colleagues(ctx):
    """What makes delegation possible at all.

    An employee cannot ask the right colleague if it does not know who exists.
    Without this the Marketing employee needing a product fact either invents
    one or tells the user to go and ask somebody, and both outcomes are the
    failure this platform is built to avoid.
    """
    AIAgent = _agents()
    rows = [agent for agent in AIAgent.objects.all()
            if ctx.agent is None or agent.pk != ctx.agent.pk]

    if not rows:
        return ToolResult(
            ok=True,
            text='There are no other AI employees on the roster, so there is '
                 'nobody to delegate to. Everything has to be done with your '
                 'own tools.')

    lines = []
    data = []
    for agent in rows:
        groups = _capability_groups(agent.agent_type)
        headline = ', '.join(f'{name} ({count})' for name, count in groups[:5])
        tool_count = len(tool_base.tools_for(agent.agent_type))
        lines.append(
            f'{agent.agent_type} -- {agent.name} ({agent.role}), '
            f'{tool_count} tool(s)\n'
            f'    {agent.persona_description}\n'
            f'    Capability groups: {headline or "none"}')
        data.append({'agent_type': agent.agent_type, 'name': agent.name,
                     'role': agent.role, 'tools': tool_count,
                     'groups': [name for name, _count in groups]})

    return ToolResult(
        ok=True,
        text=(f'{len(rows)} colleague(s):\n' + '\n'.join(lines) +
              '\n\nDelegate with platform.delegate_to using the agent_type on '
              'the left, or check exactly what one can do with '
              'platform.ask_colleague_capabilities.'),
        data={'colleagues': data})


def _delegation_candidates(agent_type, terms, require_match=True):
    """Rank the colleague's runnable tools against a request.

    Only tools that read are considered: anything requiring approval, anything
    on the blocked list and anything whose name says it writes is dropped
    before scoring. The ranking is deliberately plain -- term overlap on name,
    title, group and description, then a bonus for a reading verb and a penalty
    for a tool every employee shares -- so the choice is reproducible and can be
    explained after the fact. A delegation that cannot be explained is worse
    than one that admits defeat.

    ``require_match`` is what stops the flood. The bonuses are only ever added
    to a tool that already matched the request, because a tool that scores
    solely on the strength of being read-only would answer an unrelated
    question with an unrelated list -- and that answer, attributed to a named
    colleague, would read as sourced. With no match at all the list comes back
    empty and the caller says so. Pass False only to enumerate what a colleague
    could in principle be asked.
    """
    ranked = []
    for entry in tool_base.tools_for(agent_type):
        if entry.requires_approval or entry.name in _DELEGATION_BLOCKED:
            continue
        short_name = entry.name.split('.')[-1]
        if any(fragment in short_name for fragment in _WRITE_FRAGMENTS):
            continue
        score = _score_tool(entry, terms)
        if require_match and score <= 0:
            continue
        if any(fragment in short_name for fragment in _READ_FRAGMENTS):
            score += 3
        if entry.reads_only:
            score += 2
        if entry.shared:
            # A colleague's own speciality should win over a tool everybody has.
            score -= 4
        ranked.append((score, entry))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [entry for _score, entry in ranked]


def _delegation_arguments(entry, request):
    """Arguments for one candidate tool, or None if they cannot be honoured.

    The request text is only ever placed in a parameter that plainly takes a
    question. Anything else required and not defaulted means this tool is not
    the right one, and returning None sends the caller to the next candidate --
    far better than guessing a value for ``candidate_id``.
    """
    properties = entry.parameters.get('properties') or {}
    arguments = {}
    placed = False

    for name in _REQUEST_PARAMETERS:
        if name in properties:
            arguments[name] = request
            placed = True
            break

    for name in entry.required_arguments:
        if name in arguments:
            continue
        if name in _REQUEST_PARAMETERS:
            arguments[name] = request
            placed = True
            continue
        spec = properties.get(name) or {}
        if 'default' in spec:
            arguments[name] = spec['default']
            continue
        return None

    if not placed and entry.required_arguments:
        # Every required argument was satisfied from a default, but the request
        # itself went nowhere, so the answer would not be about the question.
        return None
    return arguments


@tool(
    name='platform.delegate_to',
    title='Delegate to a colleague',
    description='Ask another AI employee something that is their job rather '
                'than yours, and get their answer back. The colleague runs one '
                'of its own read-only tools and the exchange is recorded as a '
                'delegation and a task, so the provenance of the answer can be '
                'traced. Use this instead of guessing or refusing.',
    group='Collaboration', shared=True, icon='fa-people-arrows',
    capability='Ask another AI employee for help',
    parameters={
        'type': 'object',
        'properties': {
            'agent_type': {
                'type': 'string',
                'description': 'The colleague to ask, from '
                               'platform.list_colleagues, for example '
                               "'research' or 'hr'.",
            },
            'request': {
                'type': 'string',
                'description': 'What you need, written as a clear question. It '
                               'is passed verbatim into the colleague tool, so '
                               'the wording matters.',
            },
            'context': {
                'type': 'string',
                'description': 'Optional. Why you are asking, recorded on the '
                               'task for whoever reads it later.',
            },
        },
        'required': ['agent_type', 'request'],
    },
)
def delegate_to(ctx, agent_type, request, context=''):
    """One employee genuinely asking another, with no language model involved.

    HOW THE ANSWER IS PRODUCED
    A delegation here is not a message dropped in a queue for somebody else to
    process later, and it is not a second model call. It is executed inside
    this call:

      1. The target employee is resolved from ``agent_type``.
      2. An ``AgentDelegation`` records the request, and an ``AgentTask`` with
         origin 'delegation' gives the colleague a unit of work whose parent is
         the caller's own task -- so the audit trail shows one request
         branching, not two unrelated ones.
      3. The colleague's tools are ranked against the request. Only tools that
         require no approval and read rather than write are eligible.
      4. The best candidate whose required arguments can be honestly satisfied
         is run through ``tool_base.run`` under the colleague's own context, so
         it is audited as the colleague's work and counts against the
         colleague's capability usage.
      5. Its result becomes the delegation's response and is returned
         attributed to the colleague.

    WHY IT NEVER INVENTS AN ANSWER
    If no eligible tool can be run, this says so and points at the colleague.
    A fabricated answer attributed to a named colleague would be the most
    damaging thing this tool could produce: it would look sourced.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    from django.utils import timezone

    models = _platform_models()
    AIAgent = _agents()

    question = str(request or '').strip()
    if not question:
        return ToolResult(
            ok=False, error='No request given.',
            text='Say what you need from the colleague. The request text is '
                 'passed straight into their tool, so an empty request cannot '
                 'be answered.')

    target = _agent_by_type(agent_type)
    if target is None:
        roster = [_roster_line(agent) for agent in AIAgent.objects.all()]
        return ToolResult(
            ok=False, error=f'No colleague of type "{agent_type}".',
            text=f'There is no employee with the type "{agent_type}". The '
                 f'roster is:\n' + '\n'.join(roster))
    if target.pk == ctx.agent.pk:
        return ToolResult(
            ok=False, error='Cannot delegate to yourself.',
            text=f'{ctx.agent.name} cannot delegate to itself. Use your own '
                 f'tools, or platform.who_can to find which colleague owns '
                 f'this.')

    task = models.AgentTask.objects.create(
        agent=target,
        title=_short(question, 190),
        description=(f'Delegated by {ctx.agent.name}.\n'
                     f'Request: {question}\n'
                     + (f'Context: {context}\n' if context else '')),
        status='running',
        origin='delegation',
        created_by=ctx.user if getattr(ctx.user, 'pk', None) else None,
        conversation=ctx.conversation,
        source_message=ctx.message,
        parent=ctx.task,
        started_at=timezone.now(),
    )
    delegation = models.AgentDelegation.objects.create(
        from_agent=ctx.agent, to_agent=target,
        task=ctx.task or task,
        request=question + (f'\n\nContext: {context}' if context else ''),
        status='pending',
    )

    terms = _terms(f'{question} {context}')
    candidates = _delegation_candidates(target.agent_type, terms)

    colleague_ctx = ToolContext(user=ctx.user, agent=target, task=task,
                               conversation=ctx.conversation)

    attempted = []
    for entry in candidates[:6]:
        arguments = _delegation_arguments(entry, question)
        if arguments is None:
            attempted.append(f'{entry.name} (arguments could not be satisfied)')
            continue
        result = tool_base.run(entry.name, colleague_ctx, arguments)
        attempted.append(f'{entry.name} ({"answered" if result.ok else "failed"})')
        if not result.ok:
            continue

        answer = result.text or '(the tool returned no text)'
        delegation.response = answer
        delegation.status = 'answered'
        delegation.answered_at = timezone.now()
        delegation.sources = [{'tool': entry.name, 'title': entry.title,
                               'agent': target.agent_type}]
        delegation.save(update_fields=['response', 'status', 'answered_at',
                                       'sources'])
        task.finish(summary=f'Answered {ctx.agent.name} using {entry.title}: '
                            f'{_short(answer, 300)}')

        return ToolResult(
            ok=True,
            text=(f'{target.name} ({target.role}) answered, using its own '
                  f'{entry.title} tool [{entry.name}]:\n\n{answer}\n\n'
                  f'Recorded as delegation #{delegation.pk} and task '
                  f'#{task.pk}. Attribute this to {target.name} rather than '
                  f'presenting it as your own finding.'),
            data={'delegation_id': delegation.pk, 'task_id': task.pk,
                  'colleague': target.agent_type, 'tool': entry.name,
                  'answer': answer, 'demo': result.demo},
            demo=result.demo,
            subject_label='marketing.agentdelegation',
            subject_id=delegation.pk)

    delegation.response = (
        'No read-only tool belonging to this employee could answer the request. '
        'Nothing was invented.')
    delegation.status = 'failed'
    delegation.answered_at = timezone.now()
    delegation.save(update_fields=['response', 'status', 'answered_at'])
    task.finish(summary='Could not be answered with a read-only tool.',
                status='failed')

    tried = '; '.join(attempted) or 'none were eligible'
    return ToolResult(
        ok=False,
        error='No suitable colleague tool.',
        text=(f'{target.name} has no read-only tool that can answer '
              f'"{_short(question, 120)}". Tools considered: {tried}. '
              f'Do not answer this yourself: say plainly that it needs '
              f'{target.name} ({target.role}) and suggest the person ask that '
              f'employee directly in its own chat. Recorded as delegation '
              f'#{delegation.pk}.'),
        data={'delegation_id': delegation.pk, 'task_id': task.pk,
              'colleague': target.agent_type, 'considered': attempted})


@tool(
    name='platform.list_delegations',
    title='List delegations',
    description='Recent delegations in both directions: what you asked '
                'colleagues, what colleagues asked you, and the answers. Use '
                'this before asking the same colleague the same thing twice.',
    group='Collaboration', shared=True, reads_only=True,
    icon='fa-right-left', capability='See what was asked of whom',
    parameters={
        'type': 'object',
        'properties': {
            'limit': {'type': 'integer', 'description': 'Default 20.'},
        },
    },
)
def list_delegations(ctx, limit=20):
    """The record of who asked whom, which is where a claim's provenance starts.

    When a figure appears in a published post, the chain back to the document
    it came from runs through these rows. Being able to read them is what makes
    that chain checkable rather than theoretical.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    from django.db import models as django_models

    models = _platform_models()

    try:
        count = max(1, min(int(limit), 60))
    except (TypeError, ValueError):
        count = 20

    rows = list(models.AgentDelegation.objects.filter(
        django_models.Q(from_agent=ctx.agent) | django_models.Q(to_agent=ctx.agent)
    ).select_related('from_agent', 'to_agent')[:count])

    if not rows:
        return ToolResult(
            ok=True,
            text=f'{ctx.agent.name} has neither asked nor been asked anything '
                 f'yet. platform.list_colleagues shows who is available.')

    lines = []
    for row in rows:
        direction = ('you asked' if row.from_agent_id == ctx.agent.pk
                     else 'asked of you by')
        other = (row.to_agent if row.from_agent_id == ctx.agent.pk
                 else row.from_agent)
        lines.append(
            f'#{row.pk} {row.created_at:%d %b %H:%M} {direction} {other.name} '
            f'[{row.get_status_display()}]\n'
            f'    Request: {_short(row.request, 180)}\n'
            f'    Answer: {_short(row.response, 240)}')

    return ToolResult(
        ok=True,
        text=f'{len(rows)} delegation(s):\n' + '\n'.join(lines),
        data={'delegations': [
            {'id': row.pk, 'from': row.from_agent.agent_type,
             'to': row.to_agent.agent_type, 'status': row.status}
            for row in rows]})


@tool(
    name='platform.ask_colleague_capabilities',
    title='Ask what a colleague can do',
    description='Exactly what one named colleague can do, from the tool '
                'registry: every capability grouped by heading, which need '
                'approval, and which can answer a delegated question. Check '
                'this before delegating something impossible.',
    group='Collaboration', shared=True, reads_only=True,
    icon='fa-circle-question', capability='Find out what a colleague can do',
    parameters={
        'type': 'object',
        'properties': {
            'agent_type': {'type': 'string',
                           'description': "The colleague type, for example "
                                          "'research'."},
        },
        'required': ['agent_type'],
    },
)
def ask_colleague_capabilities(ctx, agent_type):
    """The registry answer rather than an assumption about a colleague.

    An employee that assumes what a colleague can do delegates work that comes
    back empty. The registry is the only honest source, and it is the same
    source the colleague's own profile page renders.
    """
    AIAgent = _agents()
    target = _agent_by_type(agent_type)
    if target is None:
        roster = [_roster_line(agent) for agent in AIAgent.objects.all()]
        return ToolResult(
            ok=False, error=f'No colleague of type "{agent_type}".',
            text=f'There is no employee with the type "{agent_type}". The '
                 f'roster is:\n' + '\n'.join(roster))

    grouped = tool_base.groups_for(target.agent_type)
    if not grouped:
        return ToolResult(
            ok=True,
            text=f'{target.name} has no tools registered, so it cannot be '
                 f'delegated anything yet.')

    lines = []
    delegatable = []
    for group in sorted(grouped):
        lines.append(f'{group}:')
        for entry in grouped[group]:
            marker = ('needs human approval' if entry.requires_approval
                      else 'immediate')
            reach = f', reaches {entry.integration}' if entry.integration else ''
            lines.append(f'  - {entry.name} ({entry.title}) -- {marker}{reach}')
    for entry in _delegation_candidates(target.agent_type, set(),
                                        require_match=False)[:8]:
        delegatable.append(entry.name)

    total = sum(len(entries) for entries in grouped.values())
    return ToolResult(
        ok=True,
        text=(f'{target.name} ({target.role}) -- {total} capabilities in '
              f'{len(grouped)} group(s).\n{target.persona_description}\n\n'
              + '\n'.join(lines) +
              '\n\nTools that could answer a delegated question: '
              + (', '.join(delegatable) if delegatable
                 else 'none; this colleague only performs actions') + '.'),
        data={'agent_type': target.agent_type, 'name': target.name,
              'total': total, 'groups': sorted(grouped),
              'delegatable': delegatable})


# ===========================================================================
# 5. SELF-KNOWLEDGE -- an employee inspecting its own capabilities
# ===========================================================================

@tool(
    name='platform.my_capabilities',
    title='List my capabilities',
    description='Every tool you can call, grouped by heading, marked with '
                'whether it needs human approval and which connected '
                'application it reaches, with a total count. Call this when you '
                'are unsure whether you can do something.',
    group='Self-Knowledge', shared=True, reads_only=True, icon='fa-toolbox',
    capability='Know what I can do',
    parameters={'type': 'object', 'properties': {}},
)
def my_capabilities(ctx):
    """An employee reading its own job description from the registry.

    The registry is the only honest answer to "can you do this". A model
    reasoning from its system prompt will either claim a capability it does not
    have or refuse one it does, and both mistakes are visible to the user. This
    replaces the guess with a list.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    grouped = tool_base.groups_for(ctx.agent.agent_type)
    if not grouped:
        return ToolResult(
            ok=True,
            text=f'{ctx.agent.name} has no tools registered, which is a '
                 f'provisioning problem rather than a limitation of the job. '
                 f'Report it rather than working around it.')

    lines = []
    approval_count = 0
    integrations_used = set()
    total = 0
    for group in sorted(grouped):
        lines.append(f'{group}:')
        for entry in grouped[group]:
            total += 1
            if entry.requires_approval:
                approval_count += 1
            if entry.integration:
                integrations_used.add(entry.integration)
            marker = ('goes to the human approval queue'
                      if entry.requires_approval else 'acts immediately')
            reach = f', reaches {entry.integration}' if entry.integration else ''
            lines.append(f'  - {entry.name} ({entry.title}) -- {marker}{reach}')

    return ToolResult(
        ok=True,
        text=(f'{ctx.agent.name} ({ctx.agent.role}) has {total} tool(s) in '
              f'{len(grouped)} group(s). {approval_count} of them prepare an '
              f'action for human approval; the other {total - approval_count} '
              f'act immediately. Connected applications reached: '
              f'{", ".join(sorted(integrations_used)) or "none"}.\n\n'
              + '\n'.join(lines)),
        data={'total': total, 'requires_approval': approval_count,
              'groups': sorted(grouped),
              'integrations': sorted(integrations_used)})


@tool(
    name='platform.my_tools_for',
    title='Find my tool for a need',
    description='Which of your own tools match a described need, ranked best '
                'first, each with the arguments it takes and whether it needs '
                'approval. Use this the moment you are unsure which tool to '
                'call, instead of guessing a tool name.',
    group='Self-Knowledge', shared=True, reads_only=True, icon='fa-compass',
    capability='Work out which of my tools fits a task',
    parameters={
        'type': 'object',
        'properties': {
            'topic': {'type': 'string',
                      'description': 'What you are trying to do, in plain '
                                     'words, for example "send an interview '
                                     'invitation".'},
        },
        'required': ['topic'],
    },
)
def my_tools_for(ctx, topic):
    """The recovery path when a model cannot work out which tool to call.

    A model that guesses a tool name gets a "no such tool" reply and often
    gives up or, worse, describes what it would have done. Giving it a ranked
    shortlist with the exact argument names turns a dead end into the next
    call, which is why this returns argument lists rather than prose.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    terms = _terms(topic)
    if not terms:
        return ToolResult(
            ok=False, error='No topic given.',
            text='Describe the need in a few words, for example "search our '
                 'refund policy" or "book an interview".')

    ranked = []
    for entry in tool_base.tools_for(ctx.agent.agent_type):
        score = _score_tool(entry, terms)
        if score > 0:
            ranked.append((score, entry))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].name))

    if not ranked:
        return ToolResult(
            ok=True,
            text=(f'None of {ctx.agent.name} tools match "{topic}". This is '
                  f'probably somebody else job: call platform.who_can with the '
                  f'same words to find the colleague who owns it, then '
                  f'platform.delegate_to. Do not attempt it without a tool.'),
            data={'matches': 0})

    lines = []
    data = []
    for score, entry in ranked[:8]:
        marker = ('needs human approval' if entry.requires_approval
                  else 'immediate')
        lines.append(
            f'{entry.name} ({entry.title}) -- score {score}, {marker}\n'
            f'    Arguments: {_argument_summary(entry)}\n'
            f'    Required: {", ".join(entry.required_arguments) or "none"}\n'
            f'    {_short(entry.description, 240)}')
        data.append({'tool': entry.name, 'title': entry.title, 'score': score,
                     'requires_approval': entry.requires_approval,
                     'required_arguments': entry.required_arguments})

    return ToolResult(
        ok=True,
        text=(f'{len(ranked)} of {ctx.agent.name} tools match "{topic}". Best '
              f'{len(lines)}:\n' + '\n'.join(lines) +
              '\n\nCall the top one whose required arguments you can supply. '
              'If a required detail is missing, ask one short question rather '
              'than inventing it.'),
        data={'matches': len(ranked), 'tools': data})


@tool(
    name='platform.who_can',
    title='Find who can do something',
    description='Which of the AI employees has a tool for a described need. Use '
                'this to route or delegate a request rather than refusing it. '
                'Reports the colleague, the matching tool and whether it needs '
                'approval.',
    group='Self-Knowledge', shared=True, reads_only=True,
    icon='fa-magnifying-glass-location',
    capability='Find which employee owns a task',
    parameters={
        'type': 'object',
        'properties': {
            'topic': {'type': 'string',
                      'description': 'The need, in plain words.'},
        },
        'required': ['topic'],
    },
)
def who_can(ctx, topic):
    """Turn "I cannot do that" into "that is the Research employee's job".

    A refusal is almost always wrong in this platform: six employees between
    them cover a great deal, and the request usually belongs to one of them.
    Answering with a name and a tool makes routing possible; answering with a
    refusal ends the conversation.
    """
    AIAgent = _agents()
    terms = _terms(topic)
    if not terms:
        return ToolResult(
            ok=False, error='No topic given.',
            text='Describe the need in a few words so it can be matched '
                 'against the roster.')

    own_lines = []
    shared_lines = []
    data = []
    for agent in AIAgent.objects.all():
        best = []
        for entry in tool_base.tools_for(agent.agent_type):
            score = _score_tool(entry, terms)
            if score <= 0:
                continue
            if entry.shared:
                continue
            best.append((score, entry))
        best.sort(key=lambda pair: (-pair[0], pair[1].name))
        if not best:
            continue
        top = ', '.join(
            f'{entry.name}'
            + (' (needs approval)' if entry.requires_approval else '')
            for _score, entry in best[:3])
        mine = ' -- this is you' if ctx.agent and agent.pk == ctx.agent.pk else ''
        own_lines.append(
            f'{agent.agent_type} -- {agent.name} ({agent.role}){mine}: {top}')
        data.append({'agent_type': agent.agent_type, 'name': agent.name,
                     'tools': [entry.name for _score, entry in best[:3]],
                     'score': best[0][0]})

    for entry in tool_base.all_tools():
        if not entry.shared:
            continue
        if _score_tool(entry, terms) > 0:
            shared_lines.append(
                f'{entry.name} ({entry.title})'
                + (' -- needs approval' if entry.requires_approval else ''))

    if not own_lines and not shared_lines:
        return ToolResult(
            ok=True,
            text=(f'No employee has a tool matching "{topic}". Say plainly that '
                  f'the workforce cannot do this yet rather than attempting it '
                  f'by hand, and suggest what capability would be needed.'),
            data={'matches': 0})

    text = ''
    if own_lines:
        text += ('Employees with a tool for this:\n' + '\n'.join(own_lines))
    if shared_lines:
        text += (('\n\n' if text else '') +
                 'Available to every employee, including you:\n' +
                 '\n'.join(f'- {line}' for line in shared_lines[:8]))
    text += ('\n\nDelegate with platform.delegate_to using the type on the '
             'left. If it is your own job, use the tool directly.')

    return ToolResult(ok=True, text=text,
                      data={'employees': data,
                            'shared_tools': shared_lines[:8],
                            'matches': len(data) + len(shared_lines)})


@tool(
    name='platform.platform_overview',
    title='Platform overview',
    description='The state of the whole system in one block: the employees, '
                'the capability count, integrations by effective mode, pending '
                'approvals, documents in the knowledge base, open tasks and '
                'audit events today. Use this to answer "how is the platform '
                'set up" or "what is outstanding".',
    group='Self-Knowledge', shared=True, reads_only=True,
    icon='fa-gauge-high', capability='See the state of the whole platform',
    parameters={'type': 'object', 'properties': {}},
)
def platform_overview(ctx):
    """One readable picture of the platform, assembled from the real rows.

    Asked "is this thing actually working", nobody wants six separate calls and
    a synthesis. This counts what exists: employees on the roster, capabilities
    in the registry, integrations by what they would really do if called,
    approvals nobody has looked at, documents that can be cited, tasks still
    open, and how much happened today.
    """
    from django.utils import timezone

    models = _platform_models()
    AIAgent = _agents()

    agents = list(AIAgent.objects.all())
    roster = [
        f'  - {agent.agent_type} -- {agent.name} ({agent.role}), '
        f'{len(tool_base.tools_for(agent.agent_type))} tool(s), '
        f'{agent.tasks_completed} task(s) completed'
        for agent in agents]

    by_mode = {}
    integration_rows = list(models.Integration.objects.all())
    for integration in integration_rows:
        by_mode.setdefault(integration.effective_mode, []).append(
            integration.provider_key)

    pending = list(models.ProposedAction.objects.filter(
        status='pending').select_related('agent')[:200])
    reminder_hours = _setting_value('approval_reminder_hours', 24)
    try:
        threshold = float(reminder_hours)
    except (TypeError, ValueError):
        threshold = 24.0
    ageing = [action for action in pending if action.age_hours >= threshold]

    open_tasks = models.AgentTask.objects.filter(
        status__in=('queued', 'running', 'waiting_approval', 'blocked')).count()

    try:
        from ..models_knowledge import KnowledgeDocument
        document_count = KnowledgeDocument.objects.filter(is_active=True).count()
    except Exception:  # noqa: BLE001 -- the knowledge app is optional here
        document_count = 0

    today = timezone.localdate()
    events_today = models.AuditEvent.objects.filter(created_at__date=today).count()

    all_tools = tool_base.all_tools()
    approval_tools = [entry for entry in all_tools if entry.requires_approval]

    mode_lines = [f'  - {mode}: {len(keys)} ({", ".join(sorted(keys))})'
                  for mode, keys in sorted(by_mode.items())]

    text = (
        f'AI WORKFORCE OS -- platform state\n\n'
        f'EMPLOYEES ({len(agents)}):\n' + ('\n'.join(roster) or '  - none') +
        f'\n\nCAPABILITIES: {len(all_tools)} tool(s) registered, '
        f'{len(approval_tools)} of which require human approval.\n\n'
        f'INTEGRATIONS ({len(integration_rows)}) by effective mode:\n'
        + ('\n'.join(mode_lines) or '  - none; run platform.ensure_defaults') +
        f'\n\nAPPROVALS: {len(pending)} pending'
        + (f', {len(ageing)} of them older than {threshold:g} hour(s)'
           if ageing else '') + '.\n'
        f'KNOWLEDGE BASE: {document_count} active document(s) available to '
        f'cite.\n'
        f'TASKS: {open_tasks} still open.\n'
        f'AUDIT: {events_today} event(s) recorded today ({today:%d %b %Y}).\n\n'
        f'Every action that leaves the company is queued for a person; that is '
        f'enforced in the tool layer and cannot be switched off in settings.'
    )

    return ToolResult(ok=True, text=text, data={
        'employees': [agent.agent_type for agent in agents],
        'tools': len(all_tools),
        'approval_tools': len(approval_tools),
        'integrations_by_mode': {mode: sorted(keys)
                                 for mode, keys in by_mode.items()},
        'pending_approvals': len(pending),
        'ageing_approvals': len(ageing),
        'documents': document_count,
        'open_tasks': open_tasks,
        'audit_events_today': events_today})


# ===========================================================================
# 6. WORK TRACKING -- the approval queue and this employee's own work
# ===========================================================================

@tool(
    name='platform.list_pending_approvals',
    title='List pending approvals',
    description='The human approval queue: action id, title, which application '
                'it would reach, its risk, how long it has waited and which '
                'employee proposed it. Use this to answer "what is waiting on '
                'a person" and to point at an action by number.',
    group='Work Tracking', shared=True, reads_only=True, icon='fa-clipboard-check',
    capability='See what is waiting for human approval',
    parameters={
        'type': 'object',
        'properties': {
            'limit': {'type': 'integer', 'description': 'Default 15.'},
        },
    },
)
def list_pending_approvals(ctx, limit=15):
    """What has been prepared and not yet released.

    Every employee needs this, because the most common follow-up question put
    to one is "did that go out?" and the honest answer is a queue position, not
    a reassurance. Entries proposed by the calling employee are marked, so it
    can talk about its own work without claiming somebody else's.
    """
    models = _platform_models()

    try:
        count = max(1, min(int(limit), 60))
    except (TypeError, ValueError):
        count = 15

    rows = list(models.ProposedAction.objects.filter(
        status='pending').select_related('agent', 'integration')[:count])
    if not rows:
        return ToolResult(
            ok=True,
            text='The approval queue is empty. Nothing is waiting on a person.')

    reminder = _setting_value('approval_reminder_hours', 24)
    try:
        threshold = float(reminder)
    except (TypeError, ValueError):
        threshold = 24.0

    lines = []
    mine = 0
    for action in rows:
        owner = action.agent.name if action.agent_id else 'unattributed'
        if ctx.agent is not None and action.agent_id == ctx.agent.pk:
            owner += ' (yours)'
            mine += 1
        flag = ' -- OVERDUE for review' if action.age_hours >= threshold else ''
        lines.append(
            f'#{action.pk} {action.title}\n'
            f'    target: {action.target_app}, risk: '
            f'{action.get_risk_display()}, waiting {action.age_hours} hour(s)'
            f'{flag}, proposed by {owner}')

    return ToolResult(
        ok=True,
        text=(f'{len(rows)} action(s) pending approval'
              + (f', {mine} of them yours' if mine else '') + ':\n'
              + '\n'.join(lines) +
              '\n\nNothing here has happened yet. Use '
              'platform.check_action_status for one of them, and '
              'platform.cancel_action to withdraw one of your own.'),
        data={'pending': [
            {'id': action.pk, 'title': action.title,
             'target_app': action.target_app, 'risk': action.risk,
             'age_hours': action.age_hours,
             'agent': action.agent.agent_type if action.agent_id else ''}
            for action in rows], 'mine': mine})


@tool(
    name='platform.check_action_status',
    title='Check an action status',
    description='The full history of one proposed action: its status, who '
                'decided it and when, whether a reviewer edited it and which '
                'fields changed, and what execution actually did, including '
                'whether it was live or simulated. Credential values are never '
                'shown. Use this to answer "did that actually go out?"',
    group='Work Tracking', shared=True, reads_only=True, icon='fa-magnifying-glass',
    capability='Check what happened to a proposed action',
    parameters={
        'type': 'object',
        'properties': {
            'action_id': {'type': 'integer',
                          'description': 'The action number the proposing tool '
                                         'returned.'},
        },
        'required': ['action_id'],
    },
)
def check_action_status(ctx, action_id):
    """The honest answer to the question every user asks next.

    An employee that says "the email has been sent" when it is queued has lied,
    and an employee that says "I cannot know" when the row is right there is
    useless. This reads the row: pending, approved, executed, failed, edited by
    a reviewer, simulated rather than live. The reviewer's edits matter most --
    what went out may not be what was proposed, and that is a legitimate
    outcome that should be reported rather than hidden.
    """
    models = _platform_models()

    try:
        number = int(action_id)
    except (TypeError, ValueError):
        return ToolResult(
            ok=False, error='action_id must be a number.',
            text=f'"{action_id}" is not an action number. '
                 f'platform.list_pending_approvals lists the numbers.')

    action = models.ProposedAction.objects.filter(pk=number).select_related(
        'agent', 'integration', 'decided_by').first()
    if action is None:
        recent = list(models.ProposedAction.objects.values_list(
            'pk', flat=True)[:10])
        return ToolResult(
            ok=False, error=f'No action #{number}.',
            text=f'There is no proposed action #{number}. Recent action '
                 f'numbers are: '
                 f'{", ".join(f"#{item}" for item in recent) or "none yet"}.')

    secret_keys = list((action.payload or {}).get('_secret_keys') or [])
    diff = [(key, was, now) for key, was, now in action.payload_diff
            if key != '_secret_keys']

    lines = [
        f'Action #{action.pk}: {action.title}',
        f'Status: {action.get_status_display()}. Risk: '
        f'{action.get_risk_display()}. Target: {action.target_app}.',
        f'Tool: {action.action_type}. Proposed by '
        f'{action.agent.name if action.agent_id else "an employee"} on '
        f'{action.created_at:%d %b %Y at %H:%M} '
        f'({action.age_hours} hour(s) ago).',
        f'Decision: {action.decision_summary}.',
    ]
    if action.decision_reason:
        lines.append(f'Reason given: {action.decision_reason}')

    if action.was_edited or diff:
        lines.append(f'A reviewer edited this {action.edit_count} time(s). '
                     f'Fields changed:')
        for key, was, now in diff:
            lines.append(
                f'  - {key}: {_safe_payload_value(key, was, secret_keys)} -> '
                f'{_safe_payload_value(key, now, secret_keys)}')
        if not diff:
            lines.append('  - no field differs from the original payload now')
    else:
        lines.append('No reviewer edits: this is exactly as it was proposed.')

    if action.status in ('executed', 'failed'):
        when = (action.executed_at.strftime('%d %b %Y at %H:%M')
                if action.executed_at else 'unknown time')
        lines.append(
            f'Execution at {when}: '
            f'{"SIMULATED (demo mode)" if action.executed_in_demo else "live"}.')
        if action.execution_summary:
            lines.append(f'Outcome: {_short(action.execution_summary, 400)}')
    elif action.status == 'pending':
        lines.append('Nothing has happened yet. It is still waiting on a '
                     'person, so do not describe it as done.')
    elif action.status in ('rejected', 'cancelled'):
        lines.append('This will not be carried out.')

    trail = list(action.trail.all()[:12])
    if trail:
        lines.append('History:')
        for entry in trail:
            who = entry.actor.get_username() if entry.actor_id else 'system'
            lines.append(
                f'  - {entry.created_at:%d %b %H:%M} '
                f'{entry.get_event_display()} by {who}'
                + (f': {_short(entry.note, 140)}' if entry.note else ''))

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'action_id': action.pk, 'status': action.status,
              'risk': action.risk, 'target_app': action.target_app,
              'edit_count': action.edit_count,
              'changed_fields': [key for key, _was, _now in diff],
              'executed_in_demo': action.executed_in_demo,
              'execution_summary': action.execution_summary},
        pending_action_id=action.pk if action.status == 'pending' else 0)


@tool(
    name='platform.cancel_action',
    title='Cancel my proposed action',
    description='Withdraw one of your own pending proposals, for example after '
                'learning that a fact in it was wrong. Only works while the '
                'action is still pending and only for an action you proposed. '
                'Withdrawing is immediate and recorded.',
    group='Work Tracking', shared=True, icon='fa-ban',
    capability='Withdraw a proposal I made',
    parameters={
        'type': 'object',
        'properties': {
            'action_id': {'type': 'integer',
                          'description': 'The action number to withdraw.'},
            'reason': {'type': 'string',
                       'description': 'Why it is being withdrawn. Recorded for '
                                      'the reviewer who would have read it.'},
        },
        'required': ['action_id'],
    },
)
def cancel_action(ctx, action_id, reason=''):
    """An employee correcting itself before a person acts on the mistake.

    Withdrawing is immediate on purpose. The alternative -- an employee that
    knows a queued email quotes a wrong figure but has to wait for approval to
    stop it -- is plainly worse than one that can pull its own work back. The
    limits are what make that safe: only pending, and only its own.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    from django.utils import timezone

    models = _platform_models()
    audit = _audit()

    try:
        number = int(action_id)
    except (TypeError, ValueError):
        return ToolResult(
            ok=False, error='action_id must be a number.',
            text=f'"{action_id}" is not an action number.')

    action = models.ProposedAction.objects.filter(pk=number).select_related(
        'agent', 'integration').first()
    if action is None:
        return ToolResult(
            ok=False, error=f'No action #{number}.',
            text=f'There is no proposed action #{number}.')

    if action.agent_id != ctx.agent.pk:
        owner = action.agent.name if action.agent_id else 'nobody in particular'
        return ToolResult(
            ok=False, error='Not your action.',
            text=f'Action #{number} was proposed by {owner}, not by '
                 f'{ctx.agent.name}, so it cannot be withdrawn here. Only the '
                 f'employee that proposed an action may withdraw it; a person '
                 f'can reject any of them from the approval queue.')

    if action.status != 'pending':
        return ToolResult(
            ok=False, error=f'Action #{number} is {action.status}.',
            text=f'Action #{number} is {action.get_status_display().lower()}, '
                 f'not pending, so it cannot be withdrawn. '
                 f'{action.decision_summary}.'
                 + (' It has already been carried out, so it would have to be '
                    'corrected rather than withdrawn.'
                    if action.status == 'executed' else ''))

    note = str(reason or '').strip() or 'Withdrawn by the employee that proposed it.'
    previous = action.status

    action.status = 'cancelled'
    action.decision_reason = note[:2000]
    action.decided_at = timezone.now()
    action.save(update_fields=['status', 'decision_reason', 'decided_at'])

    models.ActionAuditTrail.objects.create(
        action=action, event='cancelled',
        actor=ctx.user if getattr(ctx.user, 'pk', None) else None,
        note=f'{ctx.agent.name} withdrew this proposal. {note}',
        from_status=previous, to_status='cancelled')

    audit.log('action.cancelled', category='approval', status='ok',
              actor=ctx.user, agent=ctx.agent, proposed_action=action,
              integration=action.integration, target_app=action.target_app,
              object_label=action.title,
              message=f'{ctx.agent.name} withdrew action #{action.pk}: {note}',
              task=ctx.task, conversation=ctx.conversation)

    return ToolResult(
        ok=True,
        text=(f'Withdrew action #{action.pk} "{action.title}". It will not be '
              f'carried out and no longer appears in the approval queue. '
              f'Reason recorded: {note}'),
        data={'action_id': action.pk, 'status': 'cancelled', 'reason': note})


@tool(
    name='platform.list_my_tasks',
    title='List my tasks',
    description='The units of work assigned to you, newest first, with status '
                'and how many tool calls each took. Optionally filter by '
                'status: queued, running, waiting_approval, blocked, done, '
                'failed, cancelled.',
    group='Work Tracking', shared=True, reads_only=True, icon='fa-list-ol',
    capability='See my own work',
    parameters={
        'type': 'object',
        'properties': {
            'status': {'type': 'string',
                       'enum': ['queued', 'running', 'waiting_approval',
                                'blocked', 'done', 'failed', 'cancelled'],
                       'description': 'Optional status filter.'},
            'limit': {'type': 'integer', 'description': 'Default 20.'},
        },
    },
)
def list_my_tasks(ctx, status='', limit=20):
    """What this employee has been asked to do, and where each request stands.

    A task row is the only place a request that spanned several tool calls is
    still one thing. Reading them is how an employee answers "where did we get
    to with that" without re-doing the work.
    """
    refusal = _require_agent(ctx)
    if refusal is not None:
        return refusal

    models = _platform_models()

    try:
        count = max(1, min(int(limit), 60))
    except (TypeError, ValueError):
        count = 20

    rows = models.AgentTask.objects.filter(agent=ctx.agent)
    wanted = str(status or '').strip().lower()
    if wanted:
        valid = [choice for choice, _label in models.AgentTask.STATUS_CHOICES]
        if wanted not in valid:
            return ToolResult(
                ok=False, error=f'Unknown status "{status}".',
                text=f'"{status}" is not a task status. Valid values are: '
                     f'{", ".join(valid)}.')
        rows = rows.filter(status=wanted)

    rows = list(rows[:count])
    if not rows:
        return ToolResult(
            ok=True,
            text=f'{ctx.agent.name} has no tasks'
                 + (f' with status {wanted}.' if wanted else ' recorded yet.'))

    lines = [
        f'#{task.pk} [{task.get_status_display()}] {task.title}\n'
        f'    origin: {task.get_origin_display()}, '
        f'{task.tool_calls} tool call(s), created '
        f'{task.created_at:%d %b %H:%M}'
        + (f', result: {_short(task.result_summary, 160)}'
           if task.result_summary else '')
        for task in rows]

    open_count = len([task for task in rows if task.is_open])
    return ToolResult(
        ok=True,
        text=(f'{len(rows)} task(s) for {ctx.agent.name}, {open_count} still '
              f'open:\n' + '\n'.join(lines) +
              '\n\nplatform.get_task shows the step-by-step working record of '
              'one of them.'),
        data={'tasks': [{'id': task.pk, 'title': task.title,
                         'status': task.status, 'origin': task.origin,
                         'tool_calls': task.tool_calls} for task in rows],
              'open': open_count})


@tool(
    name='platform.get_task',
    title='Get one task',
    description='One task in full, including the ordered working record of '
                'every step taken on it, any proposed actions it produced and '
                'any delegations it involved.',
    group='Work Tracking', shared=True, reads_only=True, icon='fa-clipboard-list',
    capability='Read the working record of one task',
    parameters={
        'type': 'object',
        'properties': {
            'task_id': {'type': 'integer', 'description': 'The task number.'},
        },
        'required': ['task_id'],
    },
)
def get_task(ctx, task_id):
    """The step record, which is what makes a task answerable rather than opaque.

    ``steps`` is written by the tool runner on every call, so it is not a
    summary anybody composed after the fact. Reading it back is how an employee
    resumes work it had already partly done, and how a person sees what was
    actually attempted.
    """
    models = _platform_models()

    try:
        number = int(task_id)
    except (TypeError, ValueError):
        return ToolResult(
            ok=False, error='task_id must be a number.',
            text=f'"{task_id}" is not a task number.')

    task = models.AgentTask.objects.filter(pk=number).select_related(
        'agent', 'parent').first()
    if task is None:
        recent = list(models.AgentTask.objects.values_list('pk', flat=True)[:10])
        return ToolResult(
            ok=False, error=f'No task #{number}.',
            text=f'There is no task #{number}. Recent task numbers are: '
                 f'{", ".join(f"#{item}" for item in recent) or "none yet"}.')

    lines = [
        f'Task #{task.pk}: {task.title}',
        f'Status: {task.get_status_display()}. Origin: '
        f'{task.get_origin_display()}. Priority: '
        f'{task.get_priority_display()}.',
        f'Assigned to: {task.agent.name if task.agent_id else "nobody"}. '
        f'Created {task.created_at:%d %b %Y at %H:%M}. '
        f'{task.tool_calls} tool call(s).',
    ]
    if task.parent_id:
        lines.append(f'Part of task #{task.parent_id}: {task.parent.title}')
    if task.description:
        lines.append(f'Description: {_short(task.description, 400)}')

    steps = list(task.steps or [])
    lines.append(f'Working record ({len(steps)} step(s)):')
    for index, step in enumerate(steps[:40], start=1):
        lines.append(
            f'  {index}. [{step.get("outcome", "ok")}] '
            f'{step.get("label", "step")} -- '
            f'{_short(step.get("detail", ""), 200)}')
    if not steps:
        lines.append('  - nothing recorded yet')

    actions = list(task.proposed_actions.all()[:10])
    if actions:
        lines.append('Proposed actions from this task:')
        for action in actions:
            lines.append(f'  - #{action.pk} [{action.get_status_display()}] '
                         f'{action.title}')

    delegations = list(task.delegations.select_related(
        'from_agent', 'to_agent')[:10])
    if delegations:
        lines.append('Delegations on this task:')
        for row in delegations:
            lines.append(f'  - #{row.pk} {row.from_agent.name} -> '
                         f'{row.to_agent.name} [{row.get_status_display()}]')

    if task.result_summary:
        lines.append(f'Result: {task.result_summary}')
    if task.error_message:
        lines.append(f'Error: {_short(task.error_message, 300)}')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'task_id': task.pk, 'status': task.status,
              'steps': len(steps), 'tool_calls': task.tool_calls,
              'proposed_actions': [action.pk for action in actions]},
        subject_label='marketing.agenttask', subject_id=task.pk)


@tool(
    name='platform.recent_activity',
    title='Recent platform activity',
    description='Recent audit entries in readable form: what happened, who or '
                'which employee did it, against which application, and whether '
                'it succeeded, was simulated, failed or was denied. Optionally '
                'filter by category: agent, tool, approval, execution, '
                'integration, knowledge, data, auth, system.',
    group='Work Tracking', shared=True, reads_only=True, icon='fa-clock-rotate-left',
    capability='See what has been happening on the platform',
    parameters={
        'type': 'object',
        'properties': {
            'limit': {'type': 'integer', 'description': 'Default 20.'},
            'category': {'type': 'string',
                         'description': 'Optional category filter.'},
        },
    },
)
def recent_activity(ctx, limit=20, category=''):
    """The append-only history, read back in sentences.

    An audit trail nobody can read is a compliance ornament. Being able to say
    "Gmail was called four times today, three of them simulated" is what makes
    it a working record, and it is also how an employee notices that its own
    last attempt failed.
    """
    models = _platform_models()

    try:
        count = max(1, min(int(limit), 60))
    except (TypeError, ValueError):
        count = 20

    rows = models.AuditEvent.objects.select_related(
        'actor', 'agent', 'integration', 'proposed_action')

    wanted = str(category or '').strip().lower()
    if wanted:
        valid = [choice for choice, _label in models.AuditEvent.CATEGORY_CHOICES]
        if wanted not in valid:
            return ToolResult(
                ok=False, error=f'Unknown category "{category}".',
                text=f'"{category}" is not an audit category. Valid values '
                     f'are: {", ".join(valid)}.')
        rows = rows.filter(category=wanted)

    rows = list(rows[:count])
    if not rows:
        return ToolResult(
            ok=True,
            text='No audit entries match'
                 + (f' the {wanted} category.' if wanted else ' yet.'))

    lines = [
        f'{event.created_at:%d %b %H:%M} [{event.get_status_display()}] '
        f'{event.actor_label} -- {event.action}'
        + (f' on {event.target_app}' if event.target_app else '')
        + (f' ({event.object_label})' if event.object_label else '')
        + (f': {_short(event.message, 160)}' if event.message else '')
        for event in rows]

    statuses = {}
    for event in rows:
        statuses[event.status] = statuses.get(event.status, 0) + 1
    tally = ', '.join(f'{count_value} {status}'
                      for status, count_value in sorted(statuses.items()))

    return ToolResult(
        ok=True,
        text=f'{len(rows)} recent audit entry(s) ({tally}):\n' + '\n'.join(lines),
        data={'events': len(rows), 'by_status': statuses})
