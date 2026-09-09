"""The tool contract: what an AI employee can actually do.

A tool is the unit of capability. It has a name the language model calls, a
JSON schema describing its arguments, an owning set of employees, and a
handler that does the work. ``run`` is the only way one is invoked, which is
what makes the two guarantees below hold everywhere rather than in most places.

GUARANTEE ONE: NOTHING EXTERNAL HAPPENS WITHOUT A PERSON
--------------------------------------------------------
A tool declares ``requires_approval=True`` when its effect leaves the
platform: an email, a Slack message, a calendar invitation, a Jira issue, a
published post. Such a tool's handler does not act. It returns a ``Proposal``
-- a title, a summary and the complete argument set -- and ``run`` turns that
into a pending ``ProposedAction``. The work is done, reviewable and editable,
and nothing has happened yet.

Execution is a separate function, registered with ``@executor``, called only
from ``marketing/approvals.py`` when somebody presses Approve.

    @tool(name='gmail.send_email', requires_approval=True, ...)
    def propose_email(ctx, to, subject, body):
        return Proposal(title=f'Email to {to}', payload={...})

    @executor('gmail.send_email')
    def send_email(action):
        return integrations.call('gmail', 'send_email', **action.payload)

The split is deliberate. There is no code path in which the proposing function
can send anything, because it has nothing to send with.

GUARANTEE TWO: EVERY CALL IS RECORDED
-------------------------------------
``run`` writes an audit entry for the call, appends a step to the task, and
counts the capability, whatever the tool did. A handler cannot opt out.
"""

import time
from dataclasses import dataclass, field

from django.utils import timezone


# ===========================================================================
# The three value types
# ===========================================================================

@dataclass
class ToolContext:
    """Who is running this tool, on whose behalf, as part of what."""

    user = None
    agent = None
    conversation = None
    task = None
    message = None
    auto_approve: bool = False

    def __init__(self, user=None, agent=None, conversation=None, task=None,
                 message=None, auto_approve=False):
        self.user = user
        self.agent = agent
        self.conversation = conversation
        self.task = task
        self.message = message
        self.auto_approve = auto_approve

    def note(self, label, detail='', outcome='ok'):
        if self.task is not None:
            self.task.add_step(label, detail, outcome)


@dataclass
class ToolResult:
    """What a tool did, in a shape the language model can read back.

    ``text`` is what the model sees. It has to be a plain readable account,
    because the model's next turn is written from it: "Created job opening
    #4, Senior Django Developer" leads somewhere, "ok: true" does not.
    """

    ok: bool = True
    text: str = ''
    data: dict = field(default_factory=dict)
    demo: bool = False
    error: str = ''
    pending_action_id: int = 0
    subject_label: str = ''
    subject_id: int = 0

    @property
    def awaiting_approval(self):
        return bool(self.pending_action_id)

    def as_dict(self):
        return {
            'ok': self.ok,
            'result': self.text,
            'data': _trim(self.data),
            'demo': self.demo,
            'error': self.error,
            'pending_approval_id': self.pending_action_id or None,
        }


@dataclass
class Proposal:
    """An action an employee wants taken, not yet taken.

    Returned instead of a ToolResult by any tool that requires approval.
    ``editable_fields`` tells the review page which payload keys a person may
    change and how to render each one, so the reviewer edits an email body in
    a textarea and a recipient in a text input without the page knowing
    anything about email.
    """

    title: str
    summary: str = ''
    payload: dict = field(default_factory=dict)
    editable_fields: list = field(default_factory=list)
    risk: str = ''
    subject_label: str = ''
    subject_id: int = 0
    subject_display: str = ''
    integration_key: str = ''
    confirmation: str = ''


def editable(key, label, field_type='text', help_text='', rows=6):
    """One entry for ``Proposal.editable_fields``."""
    return {'key': key, 'label': label, 'field_type': field_type,
            'help_text': help_text, 'rows': rows}


# ===========================================================================
# The tool object and the register
# ===========================================================================

class Tool:
    """One callable capability."""

    def __init__(self, *, name, title, description, handler, group='General',
                 agent_types=(), parameters=None, integration='',
                 requires_approval=False, risk='low', icon='fa-bolt',
                 capability='', reads_only=False, shared=False):
        self.name = name
        self.title = title
        self.description = description
        self.handler = handler
        self.group = group
        self.agent_types = tuple(agent_types)
        self.parameters = parameters or {'type': 'object', 'properties': {}}
        self.integration = integration
        self.requires_approval = requires_approval
        self.risk = risk or ('high' if requires_approval else 'low')
        self.icon = icon
        self.capability = capability or title
        self.reads_only = reads_only
        self.shared = shared
        self.executor = None

    def __repr__(self):
        return f'<Tool {self.name}>'

    @property
    def required_arguments(self):
        return list(self.parameters.get('required') or [])

    def available_to(self, agent_type):
        return self.shared or agent_type in self.agent_types

    def llm_spec(self):
        """The tool as an OpenAI-style function description."""
        description = self.description
        if self.requires_approval:
            description += (' This prepares the action and sends it to the human '
                            'approval queue; it does not carry it out.')
        return {
            'type': 'function',
            'function': {
                'name': self.name.replace('.', '__'),
                'description': description[:1000],
                'parameters': self.parameters,
            },
        }


_TOOLS = {}


def tool(**options):
    """Decorator that registers a function as a tool.

        @tool(name='hr.create_job_opening', title='Create a job opening',
              description='...', group='Recruitment', agent_types=('hr',),
              parameters={...})
        def create_job_opening(ctx, title, department=''):
            ...
    """
    def decorate(function):
        options.setdefault('title', function.__name__.replace('_', ' ').title())
        options.setdefault('description', (function.__doc__ or '').strip())
        registered = Tool(handler=function, **options)
        _TOOLS[registered.name] = registered
        function.tool = registered
        return function
    return decorate


def executor(name):
    """Decorator that registers the function which carries out an approved action.

        @executor('gmail.send_email')
        def send(action):
            return ToolResult(...)
    """
    def decorate(function):
        target = _TOOLS.get(name)
        if target is None:
            raise KeyError(f'No tool named {name} to attach an executor to.')
        target.executor = function
        return function
    return decorate


def get_tool(name):
    return _TOOLS.get(name)


def all_tools():
    return sorted(_TOOLS.values(), key=lambda t: (t.group, t.title))


def tools_for(agent_type):
    """Every tool one kind of employee may call."""
    return [t for t in all_tools() if t.available_to(agent_type)]


def llm_specs(agent_type):
    return [t.llm_spec() for t in tools_for(agent_type)]


def resolve_llm_name(name):
    """Turn the name a model called back into a registry name.

    Function names in the tool-calling protocol cannot contain a dot, so
    ``hr.create_job_opening`` is advertised as ``hr__create_job_opening``.
    """
    if name in _TOOLS:
        return name
    dotted = name.replace('__', '.')
    if dotted in _TOOLS:
        return dotted
    lowered = dotted.lower()
    for key in _TOOLS:
        if key.lower() == lowered:
            return key
    return name


def groups_for(agent_type):
    """Tools for one employee, grouped under their headings, for display."""
    grouped = {}
    for entry in tools_for(agent_type):
        grouped.setdefault(entry.group, []).append(entry)
    return grouped


# ===========================================================================
# Running a tool
# ===========================================================================

def run(name, ctx, arguments=None):
    """Invoke one tool. The only way a tool is ever called.

    Returns a ToolResult in every case. A missing tool, a bad argument, a
    handler that raises -- all become a result the language model can read and
    respond to, because a stack trace in a chat window helps nobody.
    """
    from .. import audit

    arguments = dict(arguments or {})
    registry_name = resolve_llm_name(name)
    entry = _TOOLS.get(registry_name)

    if entry is None:
        return ToolResult(
            ok=False, error=f'No such tool: {name}.',
            text=f'The tool "{name}" does not exist. Available tools are listed '
                 f'in the employee profile.')

    if ctx.agent is not None and not entry.available_to(ctx.agent.agent_type):
        message = (f'{ctx.agent.name} is not permitted to use {entry.title}. '
                   f'Ask a colleague who owns that capability, or delegate the task.')
        audit.log('tool.denied', category='tool', status='denied', actor=ctx.user,
                  agent=ctx.agent, message=message, object_label=entry.name,
                  task=ctx.task, conversation=ctx.conversation)
        return ToolResult(ok=False, error=message, text=message)

    missing = [key for key in entry.required_arguments if key not in arguments]
    if missing:
        message = (f'{entry.title} needs {", ".join(missing)}. '
                   f'Ask for the missing detail, or supply a sensible default.')
        return ToolResult(ok=False, error=message, text=message)

    started = time.monotonic()
    try:
        outcome = entry.handler(ctx, **arguments)
    except TypeError as exc:
        # Almost always the model inventing an argument name.
        message = (f'{entry.title} was called with arguments it does not accept '
                   f'({exc}). Its arguments are: '
                   f'{", ".join(entry.parameters.get("properties", {})) or "none"}.')
        return ToolResult(ok=False, error=str(exc), text=message)
    except Exception as exc:  # noqa: BLE001 -- a tool failure is a result, not a crash
        elapsed = int((time.monotonic() - started) * 1000)
        audit.log(f'tool.{registry_name}', category='tool', status='failed',
                  actor=ctx.user, agent=ctx.agent, message=str(exc)[:400],
                  object_label=entry.name, task=ctx.task, conversation=ctx.conversation,
                  detail={'arguments': redact(arguments)}, duration_ms=elapsed)
        ctx.note(f'{entry.title} failed', str(exc), outcome='failed')
        return ToolResult(
            ok=False, error=str(exc),
            text=f'{entry.title} could not be completed: {exc}')

    elapsed = int((time.monotonic() - started) * 1000)

    if isinstance(outcome, Proposal):
        result = _queue(entry, ctx, arguments, outcome)
    elif isinstance(outcome, ToolResult):
        result = outcome
    elif isinstance(outcome, str):
        result = ToolResult(ok=True, text=outcome)
    elif isinstance(outcome, dict):
        result = ToolResult(ok=True, text=outcome.get('text', ''), data=outcome)
    else:
        result = ToolResult(ok=True, text=str(outcome))

    _count_capability(ctx.agent, entry)

    audit.log(
        f'tool.{registry_name}', category='tool',
        status='demo' if result.demo else ('ok' if result.ok else 'failed'),
        actor=ctx.user, agent=ctx.agent,
        message=(result.text or result.error)[:400],
        target_app=entry.integration or 'AI Workforce',
        object_label=entry.title, task=ctx.task, conversation=ctx.conversation,
        detail={'arguments': redact(arguments), 'tool': registry_name},
        duration_ms=elapsed,
    )

    if ctx.task is not None:
        ctx.task.tool_calls = (ctx.task.tool_calls or 0) + 1
        ctx.task.save(update_fields=['tool_calls'])
        ctx.note(entry.title, result.text or result.error,
                 outcome='pending' if result.awaiting_approval
                         else ('ok' if result.ok else 'failed'))

    return result


def _queue(entry, ctx, arguments, proposal):
    """Turn a Proposal into a pending ProposedAction row."""
    from .. import audit
    from ..integrations import get_integration
    from ..models_platform import ActionAuditTrail, ProposedAction

    integration = None
    key = proposal.integration_key or entry.integration
    if key:
        integration = get_integration(key)

    payload = dict(proposal.payload or arguments)

    action = ProposedAction.objects.create(
        action_type=entry.name,
        title=proposal.title[:250],
        summary=proposal.summary,
        payload=payload,
        original_payload=dict(payload),
        editable_fields=proposal.editable_fields or _guess_editable(payload),
        agent=ctx.agent,
        integration=integration,
        task=ctx.task,
        conversation=ctx.conversation,
        source_message=ctx.message,
        requested_by=ctx.user if getattr(ctx.user, 'pk', None) else None,
        subject_label=proposal.subject_label,
        subject_id=proposal.subject_id or None,
        subject_display=proposal.subject_display[:200],
        risk=proposal.risk or entry.risk,
        status='pending',
    )

    ActionAuditTrail.objects.create(
        action=action, event='proposed',
        actor=ctx.user if getattr(ctx.user, 'pk', None) else None,
        note=f'{ctx.agent.name if ctx.agent else "An employee"} prepared this action.',
        to_status='pending')

    audit.log('action.proposed', category='approval', status='pending',
              actor=ctx.user, agent=ctx.agent, proposed_action=action,
              integration=integration, target_app=action.target_app,
              object_label=action.title,
              message=f'{entry.title} queued for approval.',
              task=ctx.task, conversation=ctx.conversation)

    if ctx.task is not None and ctx.task.status == 'running':
        ctx.task.status = 'waiting_approval'
        ctx.task.save(update_fields=['status'])

    where = f' through {integration.name}' if integration else ''
    text = (proposal.confirmation
            or f'Prepared "{proposal.title}"{where} and placed it in the approval queue '
               f'as action #{action.pk}. It will not happen until somebody approves it.')

    return ToolResult(ok=True, text=text, pending_action_id=action.pk,
                      data={'action_id': action.pk, 'status': 'pending',
                            'title': action.title},
                      subject_label=proposal.subject_label,
                      subject_id=proposal.subject_id or 0)


# ===========================================================================
# Redaction
# ===========================================================================

# Argument names that carry a credential. Matched as substrings against a
# lower-cased key, so 'app_password', 'bot_token' and 'client_secret' are all
# caught without listing every connector's field names here.
_SECRET_KEY_FRAGMENTS = (
    'password', 'passwd', 'secret', 'token', 'api_key', 'apikey', 'auth',
    'credential', 'private_key', 'bearer', 'signature', 'access_key',
    'session', 'cookie', 'otp', 'pin',
)

# 'key' on its own is too broad -- 'provider_key', 'setting_key' and
# 'jira_project_key' are all ordinary identifiers -- so it is matched only as
# a whole word.
_SECRET_EXACT_KEYS = ('key', 'keys', 'pw', 'pass')

REDACTED = '[redacted]'


def is_secret_name(name):
    """Whether an argument name looks like it carries a credential."""
    lowered = str(name).lower()
    if lowered in _SECRET_EXACT_KEYS:
        return True
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def redact(value, _depth=0):
    """Replace every credential-shaped value with a placeholder.

    WHY THIS EXISTS
    ---------------
    Tool arguments are written into ``AuditEvent.detail``, which is rendered on
    the Audit Log page and in the Django admin. One tool --
    ``platform.configure_integration`` -- takes a connected application's
    credential as an argument, because the whole point of it is that an AI
    employee can be told "here is the Slack bot token, set it up".

    Without redaction that token would be written in clear text into a table
    that is deliberately readable by every signed-in person, including a
    Viewer, and would stay there permanently because the audit log is
    append-only. A credential entered once through a chat window would leak to
    everyone in the organisation.

    So the audit trail records WHICH settings were supplied and never their
    values. The values still reach the ``ProposedAction`` payload, where a
    reviewer with the approval permission can see them in order to make the
    decision, and the review page masks the keys the tool listed under
    ``_secret_keys``.

    Matching is by name rather than by content, because there is no reliable
    way to look at a string and tell whether it is a token. A name-based rule
    over-redacts occasionally, which costs a reader a little detail; a
    content-based rule under-redacts occasionally, which costs a credential.
    """
    if _depth > 6:
        return '...'

    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if is_secret_name(key):
                out[key] = _describe_secret(inner)
            else:
                out[key] = redact(inner, _depth + 1)
        return out

    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]

    return value


def _describe_secret(value):
    """Say enough about a credential to be useful, and no more.

    A bare placeholder leaves a reviewer unable to tell a real mistake -- an
    empty token, a value pasted twice -- from a working credential, so the
    length is reported. A length alone does not narrow a secret usefully.
    """
    if value in (None, '', [], {}):
        return '[not set]'
    if isinstance(value, bool):
        return value
    text = str(value)
    return f'{REDACTED} ({len(text)} characters)'


_LONG_KEYS = ('body', 'content', 'text', 'message', 'description', 'summary',
              'caption', 'post', 'note', 'agenda')


def _guess_editable(payload):
    """A reasonable set of editable fields when a tool did not declare any."""
    fields = []
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            continue
        # A key beginning with an underscore is metadata for the review page
        # itself, not an argument, and a credential must never be rendered
        # into a form field a browser will echo back.
        if key.startswith('_') or is_secret_name(key):
            continue
        kind = 'longtext' if (key in _LONG_KEYS or len(str(value)) > 160) else 'text'
        fields.append(editable(key, key.replace('_', ' ').capitalize(), kind))
    return fields


def _count_capability(agent, entry):
    """Bump the usage counter on the capability row this tool backs."""
    if agent is None:
        return
    from django.db.models import F

    from ..models_platform import AgentCapability
    AgentCapability.objects.filter(agent=agent, tool_name=entry.name).update(
        usage_count=F('usage_count') + 1, last_used_at=timezone.now())


def _trim(value, _depth=0):
    """Keep a tool's data payload small enough to put in a chat transcript."""
    if _depth > 4:
        return '...'
    if isinstance(value, dict):
        return {k: _trim(v, _depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        return [_trim(v, _depth + 1) for v in list(value)[:30]]
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:2000] + '...'
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]
