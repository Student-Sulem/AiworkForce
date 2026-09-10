"""The conversation loop that turns a chat window into an employee.

WHAT THIS MODULE IS FOR
-----------------------
``marketing/agent_engine.py`` holds the original chat path: a message goes to
a language model with a system prompt, and text comes back. That is a chat
product. An employee is different in exactly one respect -- it is offered its
tools on every call, and when it asks for one, the tool runs and its real
result goes back into the transcript before the model writes the reply. This
module is that loop, and nothing else in the project performs it.

The engine is left untouched. Both paths write the same ``ChatMessage`` rows,
use the same ``generation_source`` vocabulary and return the same shapes, so a
view can call either.

THE FOUR THINGS THIS LOOP HAS TO GET RIGHT
------------------------------------------
1. GROUNDING. The system prompt carries a small live context block: the date,
   the company profile, which of this employee's integrations are live and
   which will be simulated, how many approvals it is waiting on, and what it
   remembers. Without it a model invents the company, and an invented company
   detail in a candidate email is a real problem.

2. NOT REPEATING ITSELF. A model that cannot see any progress from a tool
   call will call it again. Filing the same Jira issue four times is the
   failure mode that matters here, so an identical call -- same tool, same
   arguments -- is answered with the previous result and a plain instruction
   not to retry, rather than being run a second time.

3. NOT RUNNING FOREVER. Rounds are capped, total calls are capped, and when
   the cap is reached the model is asked once more with no tools at all, so
   its only remaining move is to write the answer.

4. WORKING WITHOUT A MODEL. Most readers of this project will run it with no
   API key. ``fallback_turn`` therefore does real work: it picks the tool that
   best matches the request, fills what it can from the text, runs it, and
   answers from the genuine result. An apology would be easier and would make
   the platform look broken.

WHAT IS RECORDED
----------------
Every turn creates an ``AgentTask``, every tool call is audited by
``tools.run``, every proposal becomes a pending ``ProposedAction``, and the
assistant ``ChatMessage`` carries the whole account of the turn in its
``metadata``: which tools ran with which arguments and outcomes, which
approvals were queued, the model, the token count, and whether it was live.
"""

import json
import re

from django.db import transaction
from django.utils import timezone

from . import audit, llm_tools
from .models import DEFAULT_CONVERSATION_TITLE, ChatMessage

# How many times the model may be asked again after tools have run. Six is
# enough for "search, read, draft, propose" with room to recover from a
# mistake, and small enough that a confused model cannot burn an API budget.
MAX_TOOL_ROUNDS = 6

# A hard ceiling on tool calls per turn, independent of rounds, because a
# single round can request several calls at once.
MAX_TOOL_CALLS_PER_TURN = 12

# Turns of history replayed on each request. Matches agent_engine.CONTEXT_TURNS
# deliberately: the two paths should behave the same when they can.
CONTEXT_TURNS = 12

# Ceiling on the live context block, in characters. Roughly 1,200 tokens, which
# leaves the job description and the conversation the rest of the window.
MAX_CONTEXT_CHARS = 4200

# How many tools may be advertised on one call, before the most relevant are
# chosen. See select_specs for why a cap is necessary rather than tidy.
MAX_TOOLS_ADVERTISED = 32


# ===========================================================================
# The tool registry, reached defensively
# ===========================================================================
#
# ``marketing/tools/__init__.py`` imports every capability module for its side
# effects. While the platform is being built those modules arrive one at a
# time, and a half-populated package must not stop the application from
# starting -- an employee with no tools is a working employee that can only
# talk, which is a far better outcome than an import error on the login page.
#
# So the registry is fetched lazily, once, and its absence is a condition this
# module reports rather than an exception it propagates.

_REGISTRY = None
_REGISTRY_LOADED = False
_REGISTRY_ERROR = ''


def tool_registry():
    """The tool registry module, or None if it could not be imported."""
    global _REGISTRY, _REGISTRY_LOADED, _REGISTRY_ERROR

    if not _REGISTRY_LOADED:
        _REGISTRY_LOADED = True
        try:
            from . import tools
        except Exception as exc:  # noqa: BLE001 -- see the note above
            _REGISTRY = None
            _REGISTRY_ERROR = str(exc)
        else:
            _REGISTRY = tools
    return _REGISTRY


def registry_error():
    """Why the registry is unavailable, for an interface that wants to say so."""
    tool_registry()
    return _REGISTRY_ERROR


class _Unavailable:
    """Stands in for a ToolResult when there is no registry to produce one.

    Shaped like the real thing so the loop and the fallback path do not need a
    second code path for a situation that is temporary by construction.
    """

    ok = False
    demo = False
    text = ''
    data = {}
    pending_action_id = 0
    subject_label = ''
    subject_id = 0

    def __init__(self, text):
        self.text = text
        self.error = text
        self.data = {}

    @property
    def awaiting_approval(self):
        return False


def available_tools(agent_type):
    """Every registered tool this kind of employee may call."""
    registry = tool_registry()
    if registry is None:
        return []
    try:
        return registry.tools_for(agent_type)
    except Exception:  # noqa: BLE001 -- a broken registry is not a broken chat
        return []


def tool_specs(agent_type):
    """The same tools as language-model function definitions."""
    registry = tool_registry()
    if registry is None:
        return []
    try:
        return registry.llm_specs(agent_type)
    except Exception:  # noqa: BLE001
        return []


def select_specs(agent_type, request_text='', keep=()):
    """The tools to advertise for one request, and how many were left out.

    WHY THIS IS NOT SIMPLY EVERY TOOL

    The registry gives the Engineering Delivery employee eighty tools, which
    is about 12,000 tokens of function definitions. Sent on all six rounds of
    a turn that is 70,000 tokens spent describing capabilities rather than
    doing the work, and on a local model with a 4,096-token window it does not
    merely cost too much -- the request cannot be made at all, and the employee
    appears broken on exactly the free, local setup this project is most likely
    to be run on.

    So when an employee owns more tools than the cap, the ones most relevant to
    this particular request are offered and the model is told plainly that the
    list is a subset. Under the cap, every tool is offered and this function
    does nothing at all, which is the case for four of the six employees.

    ``keep`` names tools that must be offered whatever they score -- the ones
    already used this turn, so a model can never lose access to a tool it is
    part-way through using.
    """
    entries = available_tools(agent_type)
    cap = _positive_int(setting_value('max_tools_per_call', MAX_TOOLS_ADVERTISED),
                        MAX_TOOLS_ADVERTISED)

    if len(entries) <= cap:
        return [entry.llm_spec() for entry in entries], 0

    terms = _terms(request_text)
    protected = set(keep or ())

    def rank(entry):
        score = _score_tool(entry, terms, request_text)
        if entry.name in protected:
            score += 1000
        if getattr(entry, 'reads_only', False):
            # A reading tool earns its place cheaply: the model has to be able
            # to look something up before it can sensibly act on it.
            score += 0.5
        if not getattr(entry, 'shared', False):
            # On a vague request nothing scores, and the tie must not be broken
            # alphabetically -- that would fill the list with the shared
            # platform tools every employee has and crowd out the ones that
            # make this employee the one being spoken to.
            score += 0.75
        return -score, entry.name

    chosen = sorted(entries, key=rank)[:cap]
    chosen.sort(key=lambda entry: (entry.group, entry.title))
    return [entry.llm_spec() for entry in chosen], len(entries) - len(chosen)


def find_tool(name):
    registry = tool_registry()
    if registry is None:
        return None
    try:
        return registry.get_tool(registry.resolve_llm_name(name))
    except Exception:  # noqa: BLE001
        return None


def resolve_tool_name(name):
    """The registry name for whatever the model called it.

    Function names in the tool-calling protocol cannot contain a dot, so
    ``hr.list_candidates`` is advertised as ``hr__list_candidates``. Records
    and repeat detection use the registry name, so one turn cannot be recorded
    as having used two different tools because the model spelled it both ways.
    """
    registry = tool_registry()
    if registry is None:
        return name
    try:
        return registry.resolve_llm_name(name)
    except Exception:  # noqa: BLE001
        return name


def call_tool(name, ctx, arguments=None):
    """Run one tool through the registry, or report that there is none."""
    registry = tool_registry()
    if registry is None or ctx is None:
        return _Unavailable(
            'No tools are registered on this installation yet, so nothing could '
            'be carried out.')
    return registry.run(name, ctx, arguments or {})


def build_context(user=None, agent=None, conversation=None, task=None,
                  message=None, auto_approve=False):
    """A ToolContext, or None when there is no registry to define one."""
    registry = tool_registry()
    if registry is None:
        return None
    return registry.ToolContext(user=user, agent=agent, conversation=conversation,
                                task=task, message=message,
                                auto_approve=auto_approve)


# ===========================================================================
# Optional database work
# ===========================================================================

def _quietly(operation, default=None):
    """Run a database operation that is allowed to fail, inside a savepoint.

    Several things this module reads are enhancements rather than
    prerequisites: the company profile, the integration modes, the memories,
    the task record. A workspace part-way through provisioning may not have
    those tables yet, and a chat window must not stop working because the
    memory table is missing.

    The savepoint is the important part. A failed query inside an enclosing
    ``atomic`` block marks the whole transaction for rollback, so catching the
    exception without one would turn a skipped enhancement into a request that
    can no longer write anything -- a far more confusing failure than the one
    being handled.
    """
    try:
        with transaction.atomic():
            return operation()
    except Exception:  # noqa: BLE001 -- deliberately total, see the docstring
        return default


# ===========================================================================
# Settings, read as data
# ===========================================================================

def setting_value(key, default=None):
    """One SystemSetting value, with a default when it has not been seeded.

    Public because ``marketing/approvals.py`` reads its reminder interval the
    same way, and two readers of the same table should not disagree about what
    a missing row means.

    Settings are rows rather than constants because the project's promise is
    that nothing needs a code edit. That promise cuts both ways: the row may
    not exist yet, so every read here has a default and a missing table is
    treated as "not configured" rather than as an error.
    """
    def read():
        from .models_platform import SystemSetting
        return SystemSetting.objects.filter(key=key).first()

    row = _quietly(read)
    if row is None:
        return default
    value = row.resolved
    return default if value in (None, '', {}, []) else value


def _positive_int(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def max_tool_rounds():
    """Rounds allowed this turn, from the ``max_tool_calls_per_turn`` setting."""
    return _positive_int(setting_value('max_tool_calls_per_turn', MAX_TOOL_ROUNDS),
                         MAX_TOOL_ROUNDS)


def live_llm_active(agent):
    """Whether this turn should call a real language model.

    The test suite must be hermetic. A live model configured in ``.env`` would
    otherwise make every end-to-end chat test hit the provider over the network,
    which is slow and non-deterministic. Under ``TESTING`` the employee answers
    from its tools and templates exactly as it does when no model is configured,
    so the suite runs identically on a machine with an API key and one without.
    """
    from django.conf import settings
    if getattr(settings, 'TESTING', False):
        return False
    return agent.has_live_llm


# ===========================================================================
# The system prompt
# ===========================================================================

def build_system_prompt(agent, user=None):
    """The employee's instructions plus a compact block of live context.

    The job description is the employee's own ``effective_system_prompt`` and
    is never rewritten here. What follows it is small and factual, and every
    line of it exists because a model without that line gets something wrong:

        the date              a model asked for "next Tuesday" needs today
        the company           otherwise it invents a company
        integration modes     so it says "prepared, and Slack is in demo mode"
                              rather than implying a message will really send
        pending approvals     so it does not re-propose work already queued
        memory                so an employee is not amnesiac between threads

    Kept under roughly 1,500 tokens by construction and truncated as a
    backstop, because context that crowds out the conversation is worse than
    no context at all.
    """
    parts = [agent.effective_system_prompt.strip()]

    lines = []
    now = timezone.localtime()
    zone = setting_value('company_timezone') or str(timezone.get_current_timezone())
    lines.append(f"Today is {now:%A %d %B %Y}, {now:%H:%M} ({zone}).")

    company = _company_profile()
    if company:
        lines.append('')
        lines.append('COMPANY')
        lines.extend(f"  {label}: {value}" for label, value in company)

    integrations = _integration_summary(agent)
    if integrations:
        lines.append('')
        lines.append('YOUR CONNECTED APPLICATIONS')
        lines.extend(f"  {row}" for row in integrations)
        lines.append('  A demo-mode application records a realistic result and '
                     'does not reach the real service. Say so when it matters.')

    waiting = _pending_count(agent)
    if waiting:
        lines.append('')
        lines.append(f"You have {waiting} action(s) already waiting in the approval "
                     f"queue. Do not propose the same thing twice.")

    memories = recall(agent)
    if memories:
        lines.append('')
        lines.append('WHAT YOU REMEMBER')
        lines.extend(f"  - {row.key}: {_shorten(row.content, 200)}" for row in memories)

    if user is not None and getattr(user, 'pk', None):
        name = (user.get_full_name() or user.get_username()).strip()
        lines.append('')
        lines.append(f"You are speaking with {name}.")

    context = '\n'.join(lines).strip()
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS].rsplit('\n', 1)[0] + '\n  ...'

    if context:
        parts.append('\n\nCURRENT CONTEXT\n' + context)

    return ''.join(parts)


def _company_profile():
    """(label, value) pairs from the company group of SystemSetting."""
    def read():
        from .models_platform import SystemSetting
        return list(SystemSetting.objects.filter(group='company', is_secret=False)
                    .order_by('display_order', 'label')[:10])

    rows = _quietly(read, [])

    profile = []
    for row in rows:
        value = row.resolved
        if value in (None, '', {}, []):
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value)[:200]
        profile.append((row.label, _shorten(str(value), 220)))
    return profile


def _integration_summary(agent):
    """One line per connected application this employee's tools reach.

    Derived from the tools themselves rather than from a hand-written list, so
    it cannot advertise a connection no tool actually uses. Falls back to the
    display list in workforce.py while the tool modules are still arriving.
    """
    keys = []
    for entry in available_tools(agent.agent_type):
        key = getattr(entry, 'integration', '')
        if key and key not in keys:
            keys.append(key)

    if not keys:
        from .workforce import AGENT_INTEGRATIONS
        keys = list(AGENT_INTEGRATIONS.get(agent.agent_type, ()))

    if not keys:
        return []

    def read():
        from .models_platform import Integration
        return {row.provider_key: row
                for row in Integration.objects.filter(provider_key__in=keys)}

    rows = _quietly(read, {})

    summary = []
    for key in keys[:12]:
        row = rows.get(key)
        if row is None:
            continue
        summary.append(f"{row.name}: {row.effective_mode}")
    return summary


def _pending_count(agent):
    def read():
        from .models_platform import ProposedAction
        return ProposedAction.objects.filter(agent=agent, status='pending').count()

    return _quietly(read, 0)


def recall(agent, limit=6):
    """The memories this employee should carry into the prompt.

    Ordered by importance then recency, which is the ordering the model itself
    declared when it wrote them. Recall is counted, so the memory page can show
    which memories are actually earning their place rather than only which ones
    exist.
    """
    def read():
        from django.db.models import F, Q

        from .models_platform import AgentMemory
        rows = list(AgentMemory.objects
                    .filter(Q(agent=agent) | Q(scope='shared'))
                    .order_by('-importance', '-updated_at')[:limit])
        if rows:
            AgentMemory.objects.filter(pk__in=[row.pk for row in rows]).update(
                recall_count=F('recall_count') + 1, last_recalled_at=timezone.now())
        return rows

    return _quietly(read, [])


def _shorten(text, limit):
    flat = ' '.join(str(text or '').split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + '...'


# ===========================================================================
# History
# ===========================================================================

def _history_for(conversation):
    """The recent turns, oldest first, as chat-completion messages.

    Same bounded slice-and-reverse as agent_engine._history_for. Tool messages
    are not stored as ChatMessage rows and so never appear here: a previous
    turn's tool traffic is settled history, and replaying it would spend the
    context window on work already reported in the reply.
    """
    recent = list(
        conversation.messages.filter(is_error=False)
        .exclude(role='system')
        .order_by('-created_at')[:CONTEXT_TURNS]
    )
    recent.reverse()
    return [{'role': message.role, 'content': message.content}
            for message in recent if message.content]


# ===========================================================================
# One turn
# ===========================================================================

def run_turn(conversation, user_text, *, user=None, task=None, auto_approve=False):
    """Record a person's message, do the work, and record the reply.

    Returns ``{'ok', 'reply', 'message', 'user_message', 'task', 'tool_calls',
    'pending_actions', 'source', 'model', 'tokens', 'notice'}``. It never raises: a provider
    failure becomes a fallback turn, a tool failure becomes a result the model
    reads, and both are reported in the returned dictionary.
    """
    agent = conversation.agent
    text = (user_text or '').strip()

    if not text:
        return {'ok': False, 'reply': '', 'message': None, 'user_message': None,
                'task': task, 'tool_calls': [], 'pending_actions': [],
                'source': 'fallback', 'model': '', 'tokens': 0,
                'notice': 'There was nothing to answer.'}

    if user is None:
        user = conversation.user

    owned_task = task is None
    if owned_task:
        task = _start_task(agent, text, user=user, conversation=conversation)

    user_message = ChatMessage.objects.create(
        conversation=conversation, role='user', content=text,
        generation_source='manual')

    if owned_task and task is not None:
        task.source_message = user_message
        task.save(update_fields=['source_message'])

    # Name the thread after its opening question, exactly as the chat engine
    # does, and only while the title is still the untouched default.
    if (conversation.title == DEFAULT_CONVERSATION_TITLE
            and conversation.messages.filter(role='user').count() == 1):
        conversation.title = conversation.title_from_first_message()

    ctx = build_context(user=user, agent=agent, conversation=conversation,
                        task=task, message=user_message, auto_approve=auto_approve)

    messages = [{'role': 'system', 'content': build_system_prompt(agent, user=user)}]
    messages.extend(_history_for(conversation))

    if live_llm_active(agent):
        outcome = _conduct(agent, messages, ctx, agent_type=agent.agent_type)
    else:
        outcome = fallback_turn(agent, conversation, text, ctx)
        outcome.setdefault(
            'notice',
            'No language model is assigned to this employee. Assign one on the '
            'Configurations page for a written reply.')

    if not outcome.get('reply'):
        outcome = _merge_fallback(outcome,
                                  fallback_turn(agent, conversation, text, ctx))

    assistant_message = ChatMessage.objects.create(
        conversation=conversation,
        role='assistant',
        content=outcome['reply'],
        generation_source=outcome.get('source', 'fallback'),
        llm_model_used=outcome.get('model', '') or '',
        tokens_used=outcome.get('tokens', 0) or 0,
        tools_consulted=[record['name'] for record in outcome.get('tool_calls', [])],
        metadata={'runtime': _turn_record(outcome, task)},
    )

    _finish_task(task, outcome)

    # A plain save so auto_now lifts the thread to the top of the rail.
    conversation.save()
    agent.tasks_completed += 1
    agent.save(update_fields=['tasks_completed'])

    audit.log('agent.turn', category='agent',
              status='ok' if outcome.get('ok') else 'failed',
              actor=user, agent=agent, conversation=conversation, task=task,
              object_label=task.title if task else '',
              message=_shorten(outcome['reply'], 300),
              detail={'source': outcome.get('source'),
                      'tools': len(outcome.get('tool_calls', [])),
                      'pending': outcome.get('pending_actions', [])})

    return {
        'ok': bool(outcome.get('ok', True)),
        'reply': outcome['reply'],
        'message': assistant_message,
        # The person's own turn as well, so a caller that has to return both
        # rows -- agent_engine.send_message does -- is not left re-reading the
        # conversation to find the message it just caused to be written.
        'user_message': user_message,
        'task': task,
        'tool_calls': outcome.get('tool_calls', []),
        'pending_actions': outcome.get('pending_actions', []),
        'source': outcome.get('source', 'fallback'),
        'model': outcome.get('model', ''),
        'tokens': outcome.get('tokens', 0),
        'notice': outcome.get('notice', ''),
    }


def run_agent_task(agent, instruction, *, user=None, parent_task=None):
    """A turn with no conversation, for the orchestrator and for delegation.

    Same loop, same tools, same approval queue; the only differences are that
    nothing is written to a chat thread and the task records where the request
    came from. Returns the same dictionary as ``run_turn`` with ``message``
    set to None.
    """
    text = (instruction or '').strip()
    if not text:
        return {'ok': False, 'reply': '', 'message': None, 'user_message': None,
                'task': None, 'tool_calls': [], 'pending_actions': [],
                'source': 'fallback', 'model': '', 'tokens': 0,
                'notice': 'No instruction was given.'}

    task = _start_task(agent, text, user=user, parent=parent_task,
                       origin='delegation' if parent_task else 'orchestrator')

    ctx = build_context(user=user, agent=agent, task=task)

    messages = [
        {'role': 'system', 'content': build_system_prompt(agent, user=user)},
        {'role': 'user', 'content': text},
    ]

    if live_llm_active(agent):
        outcome = _conduct(agent, messages, ctx, agent_type=agent.agent_type)
    else:
        outcome = fallback_turn(agent, None, text, ctx)
        outcome.setdefault(
            'notice',
            'No language model is assigned to this employee, so the answer comes '
            'from running its best-matching tool directly.')

    if not outcome.get('reply'):
        outcome = _merge_fallback(outcome, fallback_turn(agent, None, text, ctx))

    _finish_task(task, outcome)

    audit.log('agent.task', category='agent',
              status='ok' if outcome.get('ok') else 'failed',
              actor=user, agent=agent, task=task,
              object_label=task.title if task is not None else '',
              message=_shorten(outcome['reply'], 300),
              detail={'source': outcome.get('source'),
                      'tools': len(outcome.get('tool_calls', []))})

    return {
        'ok': bool(outcome.get('ok', True)),
        'reply': outcome['reply'],
        'message': None,
        'user_message': None,
        'task': task,
        'tool_calls': outcome.get('tool_calls', []),
        'pending_actions': outcome.get('pending_actions', []),
        'source': outcome.get('source', 'fallback'),
        'model': outcome.get('model', ''),
        'tokens': outcome.get('tokens', 0),
        'notice': outcome.get('notice', ''),
    }


# ===========================================================================
# The loop itself
# ===========================================================================

def _conduct(agent, messages, ctx, *, agent_type):
    """Ask, run whatever tools were asked for, and ask again until answered.

    ``messages`` is mutated as the transcript grows, which is the point: at any
    moment it is the exact list that will be sent next, so what the model sees
    is what this function holds.

    THE TWO GUARDS, AND WHY THEY ARE SHAPED THIS WAY

    A repeated call -- same tool, same arguments, in the same turn -- is not
    run again. It is answered with the result of the first call and told not to
    retry. Simply refusing would leave the model with nothing, and it would
    either try a third time or claim the work is done; handing back the earlier
    result lets it move on. This is the guard that stops one request becoming
    four identical Jira issues.

    Rounds and total calls are capped separately, because a single round can
    request five calls at once. When either cap is reached the model is asked
    one final time with no tools offered, so writing prose is its only
    available move.
    """
    provider = agent.llm_model.provider
    model_id = agent.llm_model.model_id
    temperature = float(agent.temperature)
    max_tokens = max(int(agent.max_tokens or 800), 600)

    specs, withheld = select_specs(agent_type, _last_user_text(messages))
    rounds_allowed = max_tool_rounds()

    if withheld:
        messages.append({
            'role': 'system',
            'content': (f'{len(specs)} of your tools are listed for this request, '
                        f'the ones most relevant to it. You have {withheld} more. If '
                        f'the capability you need is not listed, say which one you '
                        f'want rather than guessing at a name.'),
        })

    records = []
    pending = []
    seen = {}
    tokens = 0
    repeats_blocked = 0
    notice = ''
    source = 'live'
    reply = ''
    rounds_used = 0
    tools_offered = bool(specs)

    while rounds_used < rounds_allowed:
        rounds_used += 1
        result = llm_tools.chat_with_tools(
            provider, model_id, messages,
            tools=specs if tools_offered else None,
            temperature=temperature, max_tokens=max_tokens)
        tokens += result.get('tokens', 0)

        if not result['ok']:
            # A provider that will not accept the tools parameter fails on the
            # first round with a 4xx. That is worth one retry without tools:
            # a talking employee beats an error message, and the model may
            # still be perfectly able to answer.
            if tools_offered and rounds_used == 1:
                tools_offered = False
                notice = (f"{result['message']} Retried without tool support.")
                rounds_used -= 1
                continue
            if records:
                # Tools already did real work this turn, so report that rather
                # than discarding it because the summary call failed.
                return _from_tool_work(records, pending, result['message'],
                                       model_id, tokens)
            return {'ok': False, 'reply': '', 'tool_calls': records,
                    'pending_actions': pending, 'source': 'fallback',
                    'model': model_id, 'tokens': tokens,
                    'notice': result['message']}

        calls = result.get('tool_calls') or []

        if not calls:
            reply = result['text']
            break

        if len(records) >= MAX_TOOL_CALLS_PER_TURN:
            reply, final_tokens = _final_answer(
                provider, model_id, messages, temperature, max_tokens,
                reason=(f'You have used the {MAX_TOOL_CALLS_PER_TURN} tool calls '
                        f'allowed for this turn.'))
            tokens += final_tokens
            notice = notice or ('The tool-call limit for one turn was reached, so '
                                'the answer was written from the results already '
                                'gathered.')
            break

        messages.append(llm_tools.assistant_tool_call_message(
            result.get('raw_message'), calls))

        for call in calls[:MAX_TOOL_CALLS_PER_TURN - len(records)]:
            content, record = _perform(call, ctx, seen)
            if record.get('repeat'):
                repeats_blocked += 1
            records.append(record)
            if record.get('pending_action_id'):
                pending.append(record['pending_action_id'])
            messages.append(llm_tools.tool_result_message(
                call.get('id'), call.get('name', ''), content, provider=provider))
    else:
        # The rounds ran out with the model still asking for tools.
        reply, final_tokens = _final_answer(
            provider, model_id, messages, temperature, max_tokens,
            reason=(f'You have used the {rounds_allowed} tool rounds allowed for '
                    f'this turn.'))
        tokens += final_tokens
        notice = notice or ('The tool-round limit for one turn was reached, so the '
                            'answer was written from the results already gathered.')

    if not reply and records:
        return _from_tool_work(records, pending,
                               'The model stopped without writing a reply.',
                               model_id, tokens)

    return {'ok': bool(reply), 'reply': reply, 'tool_calls': records,
            'pending_actions': pending, 'source': source if reply else 'fallback',
            'model': model_id, 'tokens': tokens, 'notice': notice,
            'rounds': rounds_used, 'repeats_blocked': repeats_blocked,
            'tools_offered': len(specs), 'tools_withheld': withheld}


def _last_user_text(messages):
    """The most recent thing the person actually asked.

    Used to choose which tools to advertise, so the selection follows the
    request rather than the whole thread: an employee asked about a candidate
    should be offered the recruitment tools even in a conversation that opened
    with something else.
    """
    for message in reversed(messages or []):
        if message.get('role') == 'user' and message.get('content'):
            return message['content']
    return ''


def _perform(call, ctx, seen):
    """Run one requested call, or answer it without running it.

    Returns ``(content_for_the_model, record_for_the_transcript)``.
    """
    name = call.get('name', '')
    registry_name = resolve_tool_name(name)
    arguments = call.get('arguments') or {}

    if '_raw' in arguments:
        # The model sent something that was not JSON. Telling it so is far more
        # useful than guessing what it meant, and it can correct itself on the
        # next round.
        content = (f'The arguments for {name} were not valid JSON and could not be '
                   f'read: {_shorten(arguments["_raw"], 200)}. Send them again as a '
                   f'JSON object.')
        return content, {'name': registry_name, 'arguments': {}, 'ok': False,
                         'result': content, 'pending_action_id': 0, 'demo': False,
                         'repeat': False, 'error': 'unparseable arguments'}

    key = _call_key(registry_name, arguments)
    if key in seen:
        content = (f'You have already called {name} with exactly these arguments in '
                   f'this turn. The result was: {seen[key]} Do not call it again. '
                   f'Use that result, call a different tool, or write your answer.')
        return content, {'name': registry_name, 'arguments': _plain(arguments),
                         'ok': True, 'result': content, 'pending_action_id': 0,
                         'demo': False, 'repeat': True, 'error': ''}

    result = call_tool(name, ctx, arguments)
    content = (result.text or getattr(result, 'error', '')
               or ('Done.' if result.ok else 'That did not work.'))
    seen[key] = _shorten(content, 600)

    return content, {
        'name': registry_name,
        'arguments': _plain(arguments),
        'ok': bool(result.ok),
        'result': _shorten(content, 600),
        'pending_action_id': getattr(result, 'pending_action_id', 0) or 0,
        'demo': bool(getattr(result, 'demo', False)),
        'repeat': False,
        'error': getattr(result, 'error', '') or '',
    }


def _call_key(name, arguments):
    """A stable identity for one call, so a repeat can be recognised.

    Sorted keys, so the same arguments in a different order are the same call;
    that matters because a model rarely emits its arguments in a stable order.
    """
    try:
        blob = json.dumps(_plain(arguments), sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(arguments)
    return f'{name}|{blob}'


def _plain(value, _depth=0):
    """Reduce arguments to something a JSONField will certainly store."""
    if _depth > 4:
        return '...'
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k)[:80]: _plain(v, _depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v, _depth + 1) for v in list(value)[:40]]
    return str(value)[:600]


def _final_answer(provider, model_id, messages, temperature, max_tokens, *, reason):
    """Ask once more with no tools, so prose is the only possible answer."""
    messages.append({
        'role': 'system',
        'content': (f'{reason} Write your final answer now from what the tools have '
                    f'already returned. Do not request another tool. If something is '
                    f'still missing, say what it is.'),
    })
    result = llm_tools.chat_with_tools(
        provider, model_id, messages, tools=None,
        temperature=temperature, max_tokens=max_tokens)
    return (result['text'] if result['ok'] else ''), result.get('tokens', 0)


def _from_tool_work(records, pending, why, model_id, tokens):
    """Report real tool work when the model could not write the summary.

    The work happened, it is audited, and any proposal is sitting in the
    approval queue. Losing the account of it because the summarising call
    failed would be the worst of both outcomes.
    """
    lines = ['The language model did not return a reply, so here is what was '
             'actually done.', '']
    for record in records:
        if record.get('repeat'):
            continue
        marker = '-' if record['ok'] else '!'
        lines.append(f"{marker} {record['name']}: {record['result']}")
    if pending:
        lines.append('')
        lines.append('Waiting for approval: '
                     + ', '.join(f'#{action_id}' for action_id in pending))
    return {'ok': True, 'reply': '\n'.join(lines).strip(), 'tool_calls': records,
            'pending_actions': pending, 'source': 'fallback', 'model': model_id,
            'tokens': tokens, 'notice': why}


def _merge_fallback(outcome, fallback):
    """Keep the live account of a failed turn, take the fallback's reply."""
    merged = dict(fallback)
    merged['tool_calls'] = list(outcome.get('tool_calls', [])) + \
        list(fallback.get('tool_calls', []))
    merged['pending_actions'] = list(outcome.get('pending_actions', [])) + \
        list(fallback.get('pending_actions', []))
    merged['tokens'] = outcome.get('tokens', 0) + fallback.get('tokens', 0)
    merged['model'] = outcome.get('model', '') or fallback.get('model', '')
    notices = [outcome.get('notice', ''), fallback.get('notice', '')]
    merged['notice'] = ' '.join(part for part in notices if part).strip()
    return merged


# ===========================================================================
# Tasks
# ===========================================================================

def _start_task(agent, text, *, user=None, conversation=None, parent=None,
                origin='chat'):
    """Open the AgentTask this turn's work belongs to, or carry on without one.

    Every tool call happens inside a task, which is what makes the audit trail
    answerable. But the task is the *record* of the turn rather than a
    precondition for it, and ``ToolContext`` already treats a missing task as
    "nothing to append to", so a platform layer that has not been migrated yet
    costs the reader their working record and not their conversation.
    """
    title = _shorten((text.splitlines() or [''])[0], 190) or 'Untitled request'

    def create():
        from .models_platform import AgentTask
        return AgentTask.objects.create(
            agent=agent, title=title, description=text[:4000], status='running',
            origin=origin, conversation=conversation, parent=parent,
            created_by=user if getattr(user, 'pk', None) else None,
            started_at=timezone.now())

    return _quietly(create)


def _finish_task(task, outcome):
    """Close the task, or leave it open when it is waiting on a person.

    ``waiting_approval`` rather than ``done`` whenever the turn queued
    something: the employee's work is finished but the task is not, because
    the outcome it was created for has not happened yet.
    """
    if task is None:
        return

    task.tokens_used = (task.tokens_used or 0) + (outcome.get('tokens', 0) or 0)
    task.save(update_fields=['tokens_used'])

    pending = outcome.get('pending_actions') or []
    summary = _shorten(outcome.get('reply', ''), 600)

    if pending:
        # Deliberately not task.finish(): that stamps completed_at, and this
        # task is not complete. The employee's part is done and a person's
        # part has not started, which is exactly what waiting_approval means.
        queued = ', '.join(f'#{action_id}' for action_id in pending)
        task.status = 'waiting_approval'
        task.result_summary = f'{summary} Waiting for approval: {queued}.'.strip()
        task.save(update_fields=['status', 'result_summary'])
    else:
        task.finish(summary, status='done' if outcome.get('ok', True) else 'failed')


def _turn_record(outcome, task):
    """What goes in the assistant message's metadata.

    The interface reads this to show which tools ran and which approvals were
    queued, and it is the record anybody auditing the turn reads first, so it
    holds the arguments and the outcomes rather than only the names.
    """
    return {
        'tools': [
            {'name': record['name'], 'arguments': record['arguments'],
             'ok': record['ok'], 'result': record['result'],
             'demo': record.get('demo', False),
             'repeat': record.get('repeat', False),
             'pending_action_id': record.get('pending_action_id', 0)}
            for record in outcome.get('tool_calls', [])
        ],
        'pending_actions': outcome.get('pending_actions', []),
        'model': outcome.get('model', ''),
        'tokens': outcome.get('tokens', 0),
        'source': outcome.get('source', 'fallback'),
        'rounds': outcome.get('rounds', 0),
        'repeats_blocked': outcome.get('repeats_blocked', 0),
        'tools_offered': outcome.get('tools_offered', 0),
        'tools_withheld': outcome.get('tools_withheld', 0),
        'notice': outcome.get('notice', ''),
        'task_id': task.pk if task is not None else None,
    }


# ===========================================================================
# The fallback path: an employee with no language model
# ===========================================================================
#
# This is not a courtesy. Most people running this project will have no API
# key, and an employee that answers "I cannot help without a language model"
# has demonstrated nothing at all. An employee that answers "no model is
# configured, so I ran List Candidates for you -- here are the eleven
# candidates" has demonstrated the entire architecture: the tools are real, the
# data is real, and the language model is a way of choosing between them rather
# than the thing doing the work.

# Parameters that plainly want the request itself. Ordered by how specific they
# are, so a tool with both `query` and `description` gets the text in `query`.
_TEXT_PARAMETERS = ('query', 'question', 'text', 'description', 'topic',
                    'requirement', 'error_text', 'search', 'term', 'prompt',
                    'instruction', 'content', 'body', 'message', 'title')

_ID_RE = re.compile(r'#?\b(\d{1,9})\b')

_STOPWORDS = frozenset("""
a an and are as at be but by can could do does for from get give go had has have
how i if in into is it its just let make me my no not of on or our out please so
some tell that the their them then there these they this to us use want was we
were what when where which who will with would you your
""".split())


def fallback_turn(agent, conversation, user_text, ctx):
    """Do useful work with no language model at all.

    Scores every tool this employee owns against the words of the request,
    keeps only those whose required arguments can be filled from the text, runs
    the best one, and answers from its real result. When nothing matches, the
    reply is this employee's actual capability list with an example request per
    group, which is genuinely more useful than an apology because it tells the
    reader what to type next.
    """
    text = (user_text or '').strip()
    entries = available_tools(agent.agent_type)

    ranked = _rank_tools(entries, text)
    if ranked:
        entry, arguments = ranked[0]
        result = call_tool(entry.name, ctx, arguments)
        body = (result.text or getattr(result, 'error', '')
                or 'The tool returned nothing.')
        reply = (f'No language model is configured, so I ran {entry.title} directly '
                 f'and this is its real result.\n\n{body}')
        if getattr(result, 'demo', False):
            reply += ('\n\nThat application is in demo mode, so the result was '
                      'simulated rather than sent to the real service.')
        pending_id = getattr(result, 'pending_action_id', 0) or 0
        if pending_id:
            reply += (f'\n\nNothing has happened yet: action #{pending_id} is waiting '
                      f'in the approval queue.')
        return {
            'ok': bool(result.ok),
            'reply': reply,
            'tool_calls': [{
                'name': entry.name, 'arguments': _plain(arguments),
                'ok': bool(result.ok), 'result': _shorten(body, 600),
                'pending_action_id': pending_id,
                'demo': bool(getattr(result, 'demo', False)),
                'repeat': False, 'error': getattr(result, 'error', '') or '',
            }],
            'pending_actions': [pending_id] if pending_id else [],
            'source': 'fallback',
            'model': '',
            'tokens': 0,
            'notice': _no_model_notice(),
        }

    return {'ok': True, 'reply': capability_reply(agent, text), 'tool_calls': [],
            'pending_actions': [], 'source': 'fallback', 'model': '', 'tokens': 0,
            'notice': _no_model_notice()}


def _no_model_notice():
    return ('No language model is reachable, so this employee answered by running '
            'its own tools. Assign a provider and model on the Configurations page '
            'for a written reply.')


def _rank_tools(entries, text):
    """[(tool, arguments)] for the tools that both match and can be run.

    Two filters, and the second is the one that makes this useful. A tool that
    matches the words but needs a candidate id nobody mentioned cannot be run,
    so it is discarded here rather than run and failed -- the reader would
    otherwise be shown "score_candidate needs candidate_id", which is the
    platform explaining its own plumbing instead of doing the job.
    """
    terms = _terms(text)
    if not entries:
        return []

    scored = []
    for entry in entries:
        score = _score_tool(entry, terms, text)
        if score <= 0:
            continue
        arguments = _fill_arguments(entry, text)
        if arguments is None:
            continue
        # A reading tool is a far safer guess than one that queues an action,
        # because guessing wrong on the second kind puts a wrong draft in front
        # of a reviewer.
        if getattr(entry, 'requires_approval', False):
            score -= 2.5
        elif getattr(entry, 'reads_only', False):
            score += 0.75
        if score <= 0:
            continue
        scored.append((score, entry.name, entry, arguments))

    scored.sort(key=lambda row: (-row[0], row[1]))
    return [(entry, arguments) for _score, _name, entry, arguments in scored]


def _terms(text):
    words = re.findall(r"[a-z][a-z0-9_'-]{2,}", (text or '').lower())
    return [word for word in dict.fromkeys(words) if word not in _STOPWORDS]


def _score_tool(entry, terms, text):
    """How well one tool matches the request.

    The name and title are worth more than the description because a
    description is prose and matches almost anything; a title match is close to
    the person having named the capability.
    """
    name = (entry.name or '').replace('.', ' ').replace('_', ' ').lower()
    title = (entry.title or '').lower()
    group = (entry.group or '').lower()
    description = (entry.description or '').lower()

    score = 0.0
    for term in terms:
        if term in title.split() or term in name.split():
            score += 2.0
        elif term in title or term in name:
            score += 1.25
        elif term in group:
            score += 0.75
        elif term in description:
            score += 0.4

    lowered = (text or '').lower()
    if title and title in lowered:
        score += 3.0
    return score


def _fill_arguments(entry, text):
    """Arguments for one tool from the request text, or None if impossible.

    Deliberately modest: the whole request goes into the one obviously textual
    parameter, and an integer in the text fills an id. Anything else is left
    empty, and a tool that requires something this cannot supply is reported as
    unrunnable rather than called with an invented value.
    """
    properties = (entry.parameters or {}).get('properties') or {}
    required = list(entry.required_arguments)
    arguments = {}

    text_key = next((key for key in _TEXT_PARAMETERS if key in properties), '')
    if text_key and text:
        arguments[text_key] = text

    numbers = _ID_RE.findall(text or '')
    identifier = int(numbers[0]) if numbers else None

    for key in required:
        if key in arguments:
            continue
        spec = properties.get(key) or {}
        kind = spec.get('type', 'string')
        if (key.endswith('_id') or key == 'id') and identifier is not None:
            arguments[key] = identifier
        elif kind in ('integer', 'number') and identifier is not None:
            arguments[key] = identifier
        elif kind == 'string' and spec.get('enum'):
            match = next((option for option in spec['enum']
                          if str(option).lower() in (text or '').lower()), None)
            if match is None:
                return None
            arguments[key] = match
        elif kind == 'string' and text:
            arguments[key] = text
        else:
            return None

    return arguments


def capability_reply(agent, text=''):
    """This employee's real capabilities, with an example request per group.

    Generated from the registry, so it can never advertise something the
    platform cannot do -- which is the same guarantee the roster page makes.
    """
    entries = available_tools(agent.agent_type)
    if not entries:
        return (f'{agent.name} has no tools registered on this installation yet, and '
                f'no language model is reachable either, so there is nothing it can '
                f'do for "{_shorten(text, 80)}" right now. Assign a model on the '
                f'Configurations page, or check that the tool modules are installed.')

    grouped = {}
    for entry in entries:
        grouped.setdefault(entry.group or 'General', []).append(entry)

    lines = [f'No language model is configured, so I cannot write a reply to '
             f'"{_shorten(text, 80)}". Here is what I can actually do, and how to '
             f'ask for it.', '']

    # Capped at both levels. An employee owns up to eighty tools, and a wall of
    # eighty lines is a worse answer than a readable sample of what it does.
    shown = list(grouped.items())[:8]

    for group, members in shown:
        lines.append(group.upper())
        for entry in members[:6]:
            marker = ' (needs approval)' if entry.requires_approval else ''
            lines.append(f'  - {entry.title}{marker}')
        if len(members) > 6:
            lines.append(f'  ... and {len(members) - 6} more in this group')
        lines.append(f'  Example: "{_example_request(members[0])}"')
        lines.append('')

    if len(grouped) > len(shown):
        lines.append(f'There are {len(grouped) - len(shown)} further groups of '
                     f'capabilities; the employee profile page lists all of them.')
        lines.append('')

    lines.append('Naming one of those directly will run it, even with no model '
                 'assigned.')
    return '\n'.join(lines).strip()


def _example_request(entry):
    """One plausible sentence that would run this tool."""
    properties = (entry.parameters or {}).get('properties') or {}
    text_key = next((key for key in _TEXT_PARAMETERS if key in properties), '')
    title = (entry.title or entry.name).lower()
    if text_key:
        return f'{title} for <{text_key.replace("_", " ")}>'
    identifier = next((key for key in entry.required_arguments
                       if key.endswith('_id')), '')
    if identifier:
        return f'{title} 12'
    return title


# ===========================================================================
# Carrying something forward
# ===========================================================================

def summarise_for_memory(conversation):
    """Write an extractive summary of a long conversation into memory.

    Extractive rather than generated on purpose. This runs whether or not a
    language model is available, and a summary assembled from sentences the
    participants actually wrote cannot introduce a fact neither of them said --
    which matters, because the next conversation will read it as true.

    Returns the summary text, or None when the thread is too short to be worth
    remembering.
    """
    if conversation is None:
        return None

    messages = _quietly(
        lambda: list(conversation.messages.filter(is_error=False)
                     .exclude(role='system').order_by('created_at')), [])

    if len(messages) < 6:
        return None

    asked = [message.content for message in messages if message.role == 'user']
    answered = [message for message in messages if message.role == 'assistant']

    lines = [f'Thread "{conversation.title}" with {len(asked)} requests.']
    for content in asked[:5]:
        lines.append(f'Asked: {_shorten(_first_sentence(content), 160)}')

    queued = []
    for message in answered:
        queued.extend((message.metadata or {}).get('runtime', {})
                      .get('pending_actions', []))
    if queued:
        lines.append('Queued for approval: '
                     + ', '.join(f'#{action_id}' for action_id in queued[:8]))

    if answered:
        lines.append(f'Last answer: {_shorten(_first_sentence(answered[-1].content), 200)}')

    content = '\n'.join(lines)

    def write():
        from .models_platform import AgentMemory
        return AgentMemory.objects.update_or_create(
            agent=conversation.agent,
            key=f'conversation-{conversation.pk}',
            defaults={
                'scope': 'agent',
                'kind': 'summary',
                'content': content,
                'importance': 4,
                'source': f'Conversation #{conversation.pk}',
                'conversation': conversation,
                'created_by': conversation.user,
            })

    if _quietly(write) is None:
        return None

    return content


def _first_sentence(text):
    flat = ' '.join((text or '').split())
    for stop in ('. ', '? ', '! '):
        index = flat.find(stop)
        if 0 < index < 220:
            return flat[:index + 1]
    return flat
