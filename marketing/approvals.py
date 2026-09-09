"""The approval pipeline: the one place a proposal becomes a fact.

THE RULE, AND WHERE IT IS ENFORCED
----------------------------------
An AI employee never reaches outside the company. A tool declaring
``requires_approval`` returns a ``Proposal``, and ``tools/base.py`` turns that
into a pending ``ProposedAction`` with the complete argument set attached.
Nothing has happened at that point, and nothing can: the proposing function has
no way to act, because the code that acts is a separate function registered
with ``@executor`` and reachable only from ``execute`` below.

So this module is the gate. Every route out of the platform passes through it,
and each of the five decisions a person can take -- edit, approve, reject,
cancel, retry -- is a function here with an audit entry attached.

WHY EDITING IS NOT APPROVING
----------------------------
``edit`` leaves the status at ``pending``. A reviewer who corrects a recipient
address and closes the page has changed nothing about whether the message goes
out; a second, deliberate act does that. Anything else turns a proofreading
pass into an accidental send. ``original_payload`` is never touched either, so
the difference between what the employee wrote and what was actually sent stays
recoverable for as long as the row exists.

WHY STAMPING AND EXECUTING ARE SEPARATE
---------------------------------------
``approve`` stamps the decision inside a transaction and commits, and only then
calls ``execute``. Holding a database transaction open across an HTTP call to
Slack would keep a write lock for the length of somebody else's network
problem, and on SQLite that blocks the entire application. The two-step also
gives a clean retry: the decision is durable, so a failed execution can be run
again without asking the reviewer to approve a second time.

WHY A MISSING EXECUTOR IS A RECORDED OUTCOME
--------------------------------------------
A tool that requires approval and has no ``@executor`` is a real mistake
somebody can make, and it only shows up at the moment a person presses
Approve. Crashing there loses the decision. Instead the action is marked
``failed`` with a message naming the action type, which tells whoever reads the
queue exactly which tool is missing its execution half.

EVERY FUNCTION RETURNS A DICTIONARY
-----------------------------------
Refusals are outcomes, not exceptions: a stale page submitting a decision on an
action somebody else already approved gets ``ok=False`` and a sentence
explaining why, which is what the interface needs to show.
"""

import json
import statistics as _statistics

from django.db import transaction
from django.utils import timezone

from . import audit
from .agent_runtime import find_tool, setting_value

# How long a pending action may sit before it is worth chasing. A queue nobody
# is reminded about silently becomes a list of things that never happened.
DEFAULT_REMINDER_HOURS = 24

# Statuses from which execution may legitimately be attempted. 'executing' is
# included so a run interrupted halfway -- a restarted server, a killed worker
# -- can be retried rather than being stuck for ever.
RUNNABLE = ('approved', 'executing')

_DECISIONS = ('approve', 'reject', 'cancel')

# Past tense for the bulk summary. Written out rather than derived, because
# "cancel" plus "d" is not a word and the summary is read by people.
_DECIDED = {'approve': 'Approved', 'reject': 'Rejected', 'cancel': 'Cancelled'}


# ===========================================================================
# Editing a proposal
# ===========================================================================

def edit(action, actor, changes, note=''):
    """Apply a reviewer's edits to the payload, without approving anything.

    Returns ``{'ok', 'action', 'applied', 'rejected', 'editable', 'message'}``.

    Only keys named in ``editable_fields`` may change. A key outside that set
    is refused by name and the editable ones are listed back, because the most
    likely cause is a form built against an older version of the action, and
    "field X cannot be edited; you may edit Y and Z" is the only message that
    helps somebody fix it.

    The row is re-read and locked before anything is written, and the fresh row
    is what comes back in ``action``. The instance a caller is holding may have
    been loaded before somebody else rejected or approved the same action, and
    editing the payload of an action already on its way out would silently
    change what was sent.
    """
    from .models_platform import ProposedAction

    with transaction.atomic():
        row = (ProposedAction.objects.select_for_update()
               .filter(pk=action.pk).first())
        if row is None:
            return {'ok': False, 'action': action, 'applied': {}, 'rejected': {},
                    'editable': [],
                    'message': f'Action #{action.pk} no longer exists.'}

        editable = _editable_keys(row)

        if row.status != 'pending':
            return {'ok': False, 'action': row, 'applied': {}, 'rejected': {},
                    'editable': editable,
                    'message': (f'Action #{row.pk} is {row.get_status_display()} '
                                f'and can no longer be edited.')}

        if not editable:
            return {'ok': False, 'action': row, 'applied': {}, 'rejected': {},
                    'editable': [],
                    'message': (f'Nothing in action #{row.pk} is marked editable, so '
                                f'it must be approved or rejected as proposed.')}

        payload = dict(row.payload or {})
        applied = {}
        rejected = {}

        for key, value in (changes or {}).items():
            if key not in editable:
                rejected[key] = 'not editable'
                continue
            coerced = _coerce(row, key, value)
            if payload.get(key) == coerced:
                continue
            applied[key] = {'from': payload.get(key), 'to': coerced}
            payload[key] = coerced

        if rejected and not applied:
            return {'ok': False, 'action': row, 'applied': {}, 'rejected': rejected,
                    'editable': editable,
                    'message': (f'{", ".join(rejected)} cannot be edited on this '
                                f'action. The editable fields are: '
                                f'{", ".join(editable)}.')}

        if not applied:
            return {'ok': True, 'action': row, 'applied': {}, 'rejected': rejected,
                    'editable': editable,
                    'message': 'Nothing changed, so nothing was recorded.'}

        # original_payload is deliberately untouched, so what the employee
        # proposed stays recoverable however many times this is edited.
        row.payload = payload
        row.edit_count = (row.edit_count or 0) + 1
        row.save(update_fields=['payload', 'edit_count'])

        _trail(row, 'edited', actor,
               note=note or f'{_who(actor)} edited {", ".join(applied)}.',
               changes=applied, to_status='pending')

    audit.log('action.edited', category='approval', status='pending', actor=actor,
              agent=row.agent, proposed_action=row,
              integration=row.integration, target_app=row.target_app,
              object_label=row.title,
              message=f'Edited {", ".join(applied)} before approval.',
              detail={'applied': _plain(applied), 'rejected': rejected})

    message = f'Updated {", ".join(applied)}. This action is still pending approval.'
    if rejected:
        message += f' Ignored {", ".join(rejected)}, which cannot be edited.'

    return {'ok': True, 'action': row, 'applied': applied, 'rejected': rejected,
            'editable': editable, 'message': message}


def _editable_keys(action):
    keys = []
    for entry in (action.editable_fields or []):
        key = entry.get('key') if isinstance(entry, dict) else str(entry)
        if key and key not in keys:
            keys.append(key)
    return keys


def _field_spec(action, key):
    for entry in (action.editable_fields or []):
        if isinstance(entry, dict) and entry.get('key') == key:
            return entry
    return {}


def _coerce(action, key, value):
    """Bring a submitted value back to the type the payload already holds.

    Everything from a web form arrives as a string, so a number edited in the
    browser would otherwise be written back as text and reach the executor as
    the wrong type. The declared ``field_type`` is trusted first, then the
    existing value's own type.
    """
    field_type = (_field_spec(action, key).get('field_type') or '').lower()
    existing = (action.payload or {}).get(key)

    # Booleans are tested before numbers, because bool is a subclass of int
    # and a True checked the other way round would be written back as 1.
    if field_type == 'boolean' or isinstance(existing, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')

    numeric = isinstance(existing, (int, float)) and not isinstance(existing, bool)
    if field_type == 'number' or numeric:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                return value

    if isinstance(existing, (list, dict)) and isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value

    return value.strip() if isinstance(value, str) else value


# ===========================================================================
# The four decisions
# ===========================================================================

def approve(action, actor, reason=''):
    """Approve the action, then carry it out.

    The stamping happens in a short transaction of its own and is committed
    before anything is executed, for the reasons in the module docstring:
    an HTTP call to Slack must not be made with a write lock held, and a
    durable decision means a failed execution can be retried without a second
    approval.
    """
    with transaction.atomic():
        from .models_platform import ProposedAction

        row = (ProposedAction.objects.select_for_update()
               .filter(pk=action.pk).first())
        if row is None:
            return {'ok': False, 'action': action, 'execution': None,
                    'message': f'Action #{action.pk} no longer exists.'}
        if row.status != 'pending':
            return {'ok': False, 'action': row, 'execution': None,
                    'message': (f'Action #{row.pk} is already '
                                f'{row.get_status_display()}. '
                                f'{row.decision_summary}.')}

        row.status = 'approved'
        row.decided_by = actor if getattr(actor, 'pk', None) else None
        row.decided_at = timezone.now()
        row.decision_reason = reason or ''
        row.save(update_fields=['status', 'decided_by', 'decided_at',
                                'decision_reason'])

        _trail(row, 'approved', actor, note=reason, from_status='pending',
               to_status='approved')

    audit.log('action.approved', category='approval', status='ok', actor=actor,
              agent=row.agent, proposed_action=row, integration=row.integration,
              target_app=row.target_app, object_label=row.title,
              message=reason or f'{_who(actor)} approved "{row.title}".',
              task=row.task, conversation=row.conversation)

    execution = execute(row, actor=actor)
    row.refresh_from_db()

    return {'ok': bool(execution.get('ok')), 'action': row, 'execution': execution,
            'message': execution.get('message', 'Approved.')}


def reject(action, actor, reason=''):
    """Decline the action. Nothing is executed, and the payload is left intact."""
    return _decline(action, actor, reason, status='rejected', event='rejected')


def cancel(action, actor, reason=''):
    """Withdraw the action, typically because it is no longer wanted.

    Kept separate from rejection because they mean different things to whoever
    reads the queue later: a rejection is a judgement on the content, a
    cancellation is a change of circumstances.
    """
    return _decline(action, actor, reason, status='cancelled', event='cancelled')


def _decline(action, actor, reason, *, status, event):
    from .models_platform import ProposedAction

    with transaction.atomic():
        row = (ProposedAction.objects.select_for_update()
               .filter(pk=action.pk).first())
        if row is None:
            return {'ok': False, 'action': action,
                    'message': f'Action #{action.pk} no longer exists.'}
        if row.status not in ('pending', 'approved'):
            return {'ok': False, 'action': row,
                    'message': (f'Action #{row.pk} is {row.get_status_display()} '
                                f'and cannot be {status}.')}

        previous = row.status
        row.status = status
        row.decided_by = actor if getattr(actor, 'pk', None) else None
        row.decided_at = timezone.now()
        row.decision_reason = reason or ''
        row.save(update_fields=['status', 'decided_by', 'decided_at',
                                'decision_reason'])

        _trail(row, event, actor, note=reason, from_status=previous,
               to_status=status)

    audit.log(f'action.{status}', category='approval', status='denied', actor=actor,
              agent=row.agent, proposed_action=row, integration=row.integration,
              target_app=row.target_app, object_label=row.title,
              message=reason or f'{_who(actor)} {status} "{row.title}".',
              task=row.task, conversation=row.conversation)

    _settle_task(row)

    return {'ok': True, 'action': row,
            'message': f'Action #{row.pk} was {status} and will not be carried out.'}


# ===========================================================================
# Execution
# ===========================================================================

def execute(action, actor=None):
    """Carry out one approved action through its registered executor.

    Returns ``{'ok', 'action', 'summary', 'demo', 'result', 'message'}``.

    The status is moved to ``executing`` inside a transaction that re-reads the
    row with ``select_for_update`` first. That is the double-submit guard: two
    people pressing Approve on the same action, or a page submitted twice, both
    arrive here, and only the one that finds the row still runnable proceeds.
    Without it the second request would send the same email again, which is the
    one mistake this whole module exists to prevent.
    """
    from .models_platform import ProposedAction

    with transaction.atomic():
        row = (ProposedAction.objects.select_for_update()
               .filter(pk=action.pk).first())
        if row is None:
            return {'ok': False, 'action': action, 'summary': '', 'demo': False,
                    'result': {}, 'message': f'Action #{action.pk} no longer exists.'}
        if row.status not in RUNNABLE:
            return {'ok': False, 'action': row, 'summary': '', 'demo': False,
                    'result': {},
                    'message': (f'Action #{row.pk} is {row.get_status_display()} and '
                                f'was not executed. Only an approved action runs.')}
        row.status = 'executing'
        row.save(update_fields=['status'])

    tool = find_tool(row.action_type)

    if tool is None:
        return _failed(row, actor,
                       f'No tool named "{row.action_type}" is registered, so this '
                       f'action cannot be carried out. It was probably proposed by '
                       f'a tool that has since been removed or renamed.')

    if getattr(tool, 'executor', None) is None:
        # A registered tool that requires approval and has no execution half.
        # Naming the action type is the whole value of this message: it is a
        # developer error, and this is where it becomes visible.
        return _failed(row, actor,
                       f'The tool "{row.action_type}" requires approval but has no '
                       f'registered executor, so there is nothing to run. Whoever '
                       f'added the tool needs to add its @executor function.')

    started = timezone.now()
    try:
        outcome = tool.executor(row)
    except Exception as exc:  # noqa: BLE001 -- a failed send is a result, not a crash
        return _failed(row, actor, f'{tool.title} failed: {exc}')

    ok, summary, data, demo, error = _read_outcome(outcome, tool)

    if not ok:
        return _failed(row, actor, error or summary or f'{tool.title} did not succeed.',
                       result=data)

    row.status = 'executed'
    row.executed_at = timezone.now()
    row.execution_summary = summary or f'{tool.title} completed.'
    row.execution_result = _plain(data)
    row.executed_in_demo = bool(demo)
    row.save(update_fields=['status', 'executed_at', 'execution_summary',
                            'execution_result', 'executed_in_demo'])

    _trail(row, 'executed', actor,
           note=row.execution_summary, from_status='executing', to_status='executed')

    audit.log('action.executed', category='execution',
              status='demo' if demo else 'ok', actor=actor, agent=row.agent,
              proposed_action=row, integration=row.integration,
              target_app=row.target_app, object_label=row.title,
              message=row.execution_summary[:400], task=row.task,
              conversation=row.conversation,
              duration_ms=int((timezone.now() - started).total_seconds() * 1000))

    _settle_task(row, ok=True)

    return {'ok': True, 'action': row, 'summary': row.execution_summary,
            'demo': bool(demo), 'result': row.execution_result,
            'message': row.execution_summary}


def _failed(row, actor, message, result=None):
    """Record a failed execution in a state it can be retried from."""
    row.status = 'failed'
    row.execution_summary = message[:4000]
    row.execution_result = _plain(result or {})
    row.save(update_fields=['status', 'execution_summary', 'execution_result'])

    _trail(row, 'failed', actor, note=message, from_status='executing',
           to_status='failed')

    audit.log('action.failed', category='execution', status='failed', actor=actor,
              agent=row.agent, proposed_action=row, integration=row.integration,
              target_app=row.target_app, object_label=row.title,
              message=message[:400], task=row.task, conversation=row.conversation)

    _settle_task(row, ok=False)

    return {'ok': False, 'action': row, 'summary': message, 'demo': False,
            'result': row.execution_result,
            'message': f'{message} The action can be retried once that is fixed.'}


def _read_outcome(outcome, tool):
    """Read whatever the executor returned, in any of its reasonable shapes.

    An executor may return a ``ToolResult``, an integration ``CallResult``, a
    plain dictionary, a string or nothing at all. Accepting all of them keeps
    the tool modules free to be written the obvious way, and the ``demo`` flag
    is picked up wherever it appears because the difference between "sent" and
    "would have been sent" has to reach the screen.
    """
    if outcome is None:
        return True, f'{tool.title} completed.', {}, False, ''

    if isinstance(outcome, str):
        return True, outcome, {}, False, ''

    if isinstance(outcome, bool):
        return outcome, f'{tool.title} completed.', {}, False, (
            '' if outcome else f'{tool.title} reported failure.')

    if isinstance(outcome, dict):
        ok = bool(outcome.get('ok', True))
        summary = str(outcome.get('summary') or outcome.get('text')
                      or outcome.get('result') or '')
        data = outcome.get('data') if isinstance(outcome.get('data'), dict) else outcome
        return ok, summary, data, bool(outcome.get('demo')), str(outcome.get('error') or '')

    ok = bool(getattr(outcome, 'ok', True))
    summary = str(getattr(outcome, 'text', '') or getattr(outcome, 'summary', '') or '')
    data = getattr(outcome, 'data', {})
    return (ok, summary, data if isinstance(data, dict) else {'value': _plain(data)},
            bool(getattr(outcome, 'demo', False)), str(getattr(outcome, 'error', '')))


def retry(action, actor):
    """Run a failed action again, without asking for a second approval.

    The approval decision is already recorded and has not been withdrawn, so
    re-approving would be theatre. What changed is the world outside -- a
    credential added, a service back up -- and that is not something the
    reviewer needs to re-affirm.
    """
    if action.status not in ('failed', 'executing'):
        return {'ok': False, 'action': action,
                'message': (f'Action #{action.pk} is {action.get_status_display()} '
                            f'and there is nothing to retry.')}

    action.status = 'approved'
    action.save(update_fields=['status'])

    audit.log('action.retried', category='execution', status='pending', actor=actor,
              agent=action.agent, proposed_action=action, target_app=action.target_app,
              object_label=action.title,
              message=f'{_who(actor)} retried a failed execution.')

    return execute(action, actor=actor)


# ===========================================================================
# Deciding several at once
# ===========================================================================

def bulk_decide(action_ids, actor, decision, reason=''):
    """Apply one decision to several actions, reporting each outcome separately.

    Deliberately not atomic across the set. Approving twelve actions means
    twelve separate things reaching the outside world, and rolling back the
    eleven that worked because the twelfth failed would be both impossible and
    wrong. Each row is decided on its own and the summary says what happened.
    """
    if decision not in _DECISIONS:
        return {'ok': False, 'decision': decision, 'results': [], 'succeeded': 0,
                'failed': 0,
                'message': (f'"{decision}" is not a decision. Use one of: '
                            f'{", ".join(_DECISIONS)}.')}

    from .models_platform import ProposedAction

    handler = {'approve': approve, 'reject': reject, 'cancel': cancel}[decision]
    rows = {row.pk: row for row in
            ProposedAction.objects.filter(pk__in=list(action_ids or []))}

    results = []
    succeeded = failed = 0

    for action_id in list(action_ids or []):
        row = rows.get(int(action_id)) if str(action_id).isdigit() else None
        if row is None:
            results.append({'id': action_id, 'ok': False,
                            'message': f'Action #{action_id} does not exist.'})
            failed += 1
            continue
        outcome = handler(row, actor, reason)
        results.append({'id': row.pk, 'ok': bool(outcome.get('ok')),
                        'message': outcome.get('message', '')})
        succeeded += 1 if outcome.get('ok') else 0
        failed += 0 if outcome.get('ok') else 1

    audit.log(f'action.bulk_{decision}', category='approval',
              status='ok' if failed == 0 else 'failed', actor=actor,
              message=f'{succeeded} succeeded, {failed} did not.',
              detail={'ids': [str(action_id) for action_id in (action_ids or [])]})

    return {'ok': failed == 0, 'decision': decision, 'results': results,
            'succeeded': succeeded, 'failed': failed,
            'message': (f'{_DECIDED[decision]} {succeeded} action(s)'
                        + (f'; {failed} could not be processed.' if failed else '.'))}


# ===========================================================================
# Reading the queue
# ===========================================================================

def pending(limit=None, **filters):
    """The pending queue, newest first, optionally narrowed.

    Returns a queryset rather than a list so a caller can count it, page it or
    narrow it further without a second function here for every combination.
    """
    from .models_platform import ProposedAction

    query = (ProposedAction.objects
             .filter(status='pending', **filters)
             .select_related('agent', 'integration', 'task', 'requested_by',
                             'conversation'))
    return query[:limit] if limit else query


def reminders(hours=None):
    """Pending actions old enough to chase, oldest first.

    The interval is the ``approval_reminder_hours`` setting, because how long
    is too long is a policy decision belonging to the organisation rather than
    a constant belonging to the code.
    """
    from datetime import timedelta

    threshold = hours if hours is not None else setting_value(
        'approval_reminder_hours', DEFAULT_REMINDER_HOURS)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = DEFAULT_REMINDER_HOURS

    cutoff = timezone.now() - timedelta(hours=threshold)
    return list(pending().filter(created_at__lt=cutoff).order_by('created_at'))


def statistics():
    """What the approval queue looks like as a whole.

    The two figures worth reading twice are the median time to a decision,
    which says whether the queue is a workflow or a bottleneck, and the count
    of actions edited before approval, which says whether the employees are
    proposing work people accept as written.
    """
    from .models_platform import ProposedAction

    blank = {'total': 0, 'by_status': {}, 'by_risk': {}, 'by_application': {},
             'pending': 0, 'median_decision_hours': None, 'edited_before_approval': 0,
             'live_executions': 0, 'demo_executions': 0, 'failed': 0,
             'oldest_pending_hours': None}

    try:
        # The savepoint keeps a missing table from marking an enclosing
        # transaction for rollback: a page that cannot show statistics must
        # still be able to write.
        with transaction.atomic():
            rows = list(ProposedAction.objects.select_related('integration')
                        .values('status', 'risk', 'edit_count', 'executed_in_demo',
                                'created_at', 'decided_at', 'integration__name'))
    except Exception:  # noqa: BLE001 -- an unmigrated database reports nothing
        return blank

    if not rows:
        return blank

    by_status = {}
    by_risk = {}
    by_application = {}
    decision_hours = []
    edited = 0
    live = demo = 0

    for row in rows:
        by_status[row['status']] = by_status.get(row['status'], 0) + 1
        by_risk[row['risk']] = by_risk.get(row['risk'], 0) + 1
        app = row['integration__name'] or 'AI Workforce'
        by_application[app] = by_application.get(app, 0) + 1

        if row['decided_at'] and row['created_at']:
            decision_hours.append(
                (row['decided_at'] - row['created_at']).total_seconds() / 3600)

        if (row['edit_count'] or 0) > 0 and row['status'] in (
                'approved', 'executing', 'executed', 'failed'):
            edited += 1

        if row['status'] == 'executed':
            if row['executed_in_demo']:
                demo += 1
            else:
                live += 1

    pending_rows = [row['created_at'] for row in rows if row['status'] == 'pending']
    oldest = None
    if pending_rows:
        oldest = round(
            (timezone.now() - min(pending_rows)).total_seconds() / 3600, 1)

    return {
        'total': len(rows),
        'by_status': by_status,
        'by_risk': by_risk,
        'by_application': dict(sorted(by_application.items(),
                                      key=lambda item: -item[1])),
        'pending': by_status.get('pending', 0),
        'median_decision_hours': (round(_statistics.median(decision_hours), 2)
                                  if decision_hours else None),
        'edited_before_approval': edited,
        'live_executions': live,
        'demo_executions': demo,
        'failed': by_status.get('failed', 0),
        'oldest_pending_hours': oldest,
    }


# ===========================================================================
# Showing one action to a reviewer
# ===========================================================================

def summarise_payload(action):
    """The payload as ``[(label, value, field_type)]`` for the review page.

    Ordered and labelled by ``editable_fields``, because the tool that wrote
    the proposal knows which field a reviewer reads first and what to call it.
    Keys the tool did not declare are appended afterwards rather than hidden:
    a reviewer approving an action is entitled to see every argument it will
    run with.

    Any key named in the payload's ``_secret_keys`` is masked. A tool that puts
    a credential in a payload -- configuring an integration, for instance --
    must be reviewable without the credential being rendered into a page and
    then into a browser cache.
    """
    payload = dict(action.payload or {})
    secrets = {str(key) for key in (payload.get('_secret_keys') or [])}
    rows = []
    shown = set()

    for entry in (action.editable_fields or []):
        if not isinstance(entry, dict):
            continue
        key = entry.get('key')
        if not key or key not in payload:
            continue
        shown.add(key)
        rows.append((entry.get('label') or _humanise(key),
                     _display(payload[key], key in secrets),
                     entry.get('field_type') or 'text'))

    for key, value in payload.items():
        if key in shown or key.startswith('_'):
            continue
        rows.append((_humanise(key), _display(value, key in secrets),
                     'longtext' if len(str(value)) > 160 else 'text'))

    return rows


def _display(value, masked):
    if masked:
        return '********'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, indent=2)[:2000]
        except (TypeError, ValueError):
            return str(value)[:2000]
    return '' if value is None else str(value)


def _humanise(key):
    return str(key).replace('_', ' ').strip().capitalize()


# ===========================================================================
# Shared bookkeeping
# ===========================================================================

def _trail(action, event, actor, *, note='', changes=None, from_status='',
           to_status=''):
    """One ActionAuditTrail entry. Never raises: see audit.log's docstring."""
    try:
        with transaction.atomic():
            from .models_platform import ActionAuditTrail
            return ActionAuditTrail.objects.create(
                action=action, event=event,
                actor=actor if getattr(actor, 'pk', None) else None,
                note=str(note or '')[:4000], changes=_plain(changes or {}),
                from_status=from_status, to_status=to_status)
    except Exception:  # noqa: BLE001
        return None


def _settle_task(action, ok=True):
    """Close the originating task once nothing of its work is outstanding.

    A task sits at ``waiting_approval`` from the moment its employee queued
    something. It is only finished here, and only when no sibling action is
    still pending, approved or executing -- one turn can propose three emails,
    and the task is not done until all three have been decided.
    """
    task = action.task
    if task is None or not task.is_open:
        return

    try:
        with transaction.atomic():
            from .models_platform import ProposedAction
            outstanding = (ProposedAction.objects
                           .filter(task=task,
                                   status__in=('pending', 'approved', 'executing'))
                           .exclude(pk=action.pk)
                           .count())
    except Exception:  # noqa: BLE001
        return

    if outstanding:
        return

    summary = (f'{task.result_summary or ""} Final action #{action.pk}: '
               f'{action.get_status_display()}.').strip()
    task.finish(summary[:4000], status='done' if ok else 'failed')


def _who(actor):
    if actor is None or not getattr(actor, 'pk', None):
        return 'Somebody'
    return actor.get_full_name() or actor.get_username()


def _plain(value, _depth=0):
    """Reduce a value to something a JSONField will certainly accept."""
    if _depth > 5:
        return '...'
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k)[:80]: _plain(v, _depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v, _depth + 1) for v in list(value)[:60]]
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)[:1000]
