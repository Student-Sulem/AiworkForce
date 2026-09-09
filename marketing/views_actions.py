"""Governance views: the human approval queue, and the orchestrator console.

WHY THIS MODULE IS SEPARATE FROM views.py
-----------------------------------------
views.py owns the original four-agent marketing tool, including the older
text-approval queue (``ApprovalRequest``): a person reviews a *draft* there and
approving it publishes a database row. This module owns the newer and far more
consequential queue: ``ProposedAction``, where approving something makes it
leave the company. The two screens deliberately look like relatives, because a
reviewer should recognise the shape of the page, but they are not the same act
and they do not share a permission.

THE THREE PAGES
---------------
    actions_view          everything waiting on a person, pending first
    action_detail_view    one action in full: payload, comparison, trail
    orchestrator_view     "describe what you need", routed to an employee

THE FIVE ENDPOINTS
------------------
    api_action_decide     approve (and execute) / reject / cancel one action
    api_action_edit       save a reviewer's payload edits WITHOUT approving
    api_action_bulk       one decision applied to a selection
    api_action_retry      run a failed execution again
    api_orchestrate       route a request, dispatch it, suggest a workflow

WHY THE PIPELINE IS IMPORTED DEFENSIVELY
----------------------------------------
``marketing/approvals.py`` and ``marketing/orchestrator.py`` hold the behaviour
these screens drive, and they are written independently of this module. If
either is absent the pages must still render and the endpoints must still
answer with a readable explanation, because a 500 on the approval queue tells
a reviewer nothing and leaves them unable to see what is waiting. So both are
imported inside a try block, every read-only helper has a local fallback that
uses nothing but the ORM, and every write endpoint refuses with a plain
sentence instead of a traceback.

The same reasoning shapes ``_invoke``. Those two modules are the authority on
their own signatures, so rather than assume an argument order this module
inspects what a function actually declares and passes only that. A pipeline
that names its reviewer ``user`` instead of ``actor`` works either way, and a
pipeline that requires something this module cannot supply produces a clear
message rather than a TypeError in a stack trace.

ACCESS CONTROL
--------------
Reading the queue needs ``roles.CAN_VIEW_ACTIONS``. Deciding, editing or
retrying needs ``roles.CAN_DECIDE_ACTION`` -- the one permission in the whole
platform that separates somebody who may ask an employee to draft a message
from somebody who may let it leave the building. Asking the orchestrator to do
something needs ``roles.CAN_CHAT``. Every check is made in the view as well as
in the template: the template check decides what a reviewer is *offered*, the
view check decides what actually happens.
"""

import inspect
import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, Q, Value, When
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from . import roles, workforce
from .models import AIAgent
from .models_platform import (Integration, OrchestratorDecision, ProposedAction)

# The pipeline and the router. Written in parallel with this module; see the
# module docstring for why neither is allowed to break these pages.
try:
    from . import approvals
except ImportError:                                   # pragma: no cover
    approvals = None

try:
    from . import orchestrator
except ImportError:                                   # pragma: no cover
    orchestrator = None


# ===========================================================================
# Constants
# ===========================================================================

PAGE_SIZE = 25

# How long a pending action may sit before the queue calls it overdue. A queue
# nobody reads is the failure mode of an approval workflow, so the page says so
# out loud rather than letting old rows sink quietly down the list.
OVERDUE_HOURS = 24

# Rendered in place of any payload value named in the payload's own
# ``_secret_keys`` list. A credential an employee needed in order to prepare an
# action is still a credential, and the review page has no reason to show it.
MASK = '********'

# Payload keys beginning with an underscore are metadata for the pipeline, not
# arguments a reviewer edits, so they never appear as a field.
FIELD_TYPES = ('text', 'longtext', 'number', 'boolean', 'choice', 'json')

# Which tone a risk level is drawn in. High risk means the action reaches
# somebody outside the company.
RISK_TONE = {'low': 'info', 'medium': 'warning', 'high': 'danger'}

# The status tabs, in the order a reviewer works through them.
STATUS_TABS = [
    ('pending', 'Pending', 'fa-clock'),
    ('approved', 'Approved', 'fa-circle-check'),
    ('executed', 'Executed', 'fa-paper-plane'),
    ('failed', 'Failed', 'fa-triangle-exclamation'),
    ('rejected', 'Rejected', 'fa-circle-xmark'),
    ('all', 'All', 'fa-layer-group'),
]

# One example request per employee, shown on the orchestrator console as a
# clickable chip. An empty text box is not usable by somebody who has never
# seen the platform before; a sentence they can click is.
EXAMPLE_REQUESTS = {
    'hr': 'Write a job description for a senior Django developer and invite the '
          'strongest candidate to a first interview.',
    'engineering_manager': 'Break the new billing export requirement into work '
                           'items with estimates and sequence them for the next sprint.',
    'developer': 'Review the failing test in the invoice importer and propose a fix.',
    'research': 'What is our refund policy for annual subscriptions, and where is '
                'it documented?',
    'marketing': 'Draft a LinkedIn post announcing our new reporting dashboard.',
    'support': 'A customer says their export has been stuck for two days. Draft a '
               'reply and file an engineering issue.',
}


# ===========================================================================
# Helpers: JSON plumbing, copied from views.py so the two modules answer alike
# ===========================================================================

def _json_body(request):
    """Parse a JSON request body, tolerating an empty or malformed one."""
    try:
        return json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _error(message, status=400):
    return JsonResponse({'status': 'error', 'message': message}, status=status)


def _forbidden(perm):
    """Refusal for a JSON endpoint the caller lacks the permission for."""
    return _error(f'Your role does not allow this action ({perm}).', status=403)


def _requires(request, perm):
    """True when the caller may proceed. Superusers always may."""
    return request.user.has_perm(perm)


def _unavailable(module_name):
    """Refusal when the module that does the work is not installed yet.

    503 rather than 500: nothing is broken, a part of the platform is simply
    not present, and the reviewer needs to be told that in words.
    """
    return _error(
        f'The {module_name} module is not available in this build, so this '
        f'cannot be carried out yet. Nothing has been changed.', status=503)


def _pending_count():
    """Pending actions across the shared workspace, matching the sidebar."""
    return ProposedAction.objects.filter(status='pending').count()


# ===========================================================================
# Helpers: calling the pipeline without assuming its signature
# ===========================================================================

def _invoke(fn, values):
    """Call ``fn`` with only the arguments it actually declares.

    ``values`` maps every plausible parameter name to a value, including
    synonyms, so a pipeline function that calls its reviewer ``user`` and one
    that calls it ``actor`` are both satisfied. A required parameter this
    module cannot supply raises TypeError with a sentence naming it, which the
    endpoints turn into a readable refusal.
    """
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):                    # pragma: no cover
        return fn()

    kwargs = {}
    missing = []
    for name, parameter in parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if name in values:
            kwargs[name] = values[name]
        elif parameter.default is parameter.empty:
            missing.append(name)

    if missing:
        raise TypeError(
            f"{getattr(fn, '__name__', 'the pipeline')} expects "
            f"{', '.join(missing)}, which this page cannot supply.")

    return fn(**kwargs)


def _values(action=None, actor=None, reason='', changes=None, note='',
            decision='', action_ids=None):
    """Every name the pipeline might use for the arguments we hold."""
    return {
        'action': action, 'proposed_action': action, 'obj': action,
        'actor': actor, 'user': actor, 'by': actor, 'reviewer': actor,
        'reason': reason or note, 'comment': reason,
        'changes': changes or {}, 'edits': changes or {},
        'note': note or reason,
        'decision': decision, 'outcome': decision,
        'action_ids': action_ids or [], 'ids': action_ids or [],
        'pks': action_ids or [],
    }


def _pipeline(name):
    """The named callable from approvals.py, or None when it is not there."""
    if approvals is None:
        return None
    fn = getattr(approvals, name, None)
    return fn if callable(fn) else None


def _router(name):
    """The named callable from orchestrator.py, or None."""
    if orchestrator is None:
        return None
    fn = getattr(orchestrator, name, None)
    return fn if callable(fn) else None


# ===========================================================================
# Helpers: statistics and reminders, each with an ORM-only fallback
# ===========================================================================

def _median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


# Canonical statistic name -> the names the pipeline might have used for it.
# approvals.statistics() reports live and demo executions separately and gives
# the per-status breakdown as a nested dict, so the aliases here cover its
# actual key names as well as the plainer ones a caller might expect.
STAT_ALIASES = {
    'pending': ('pending', 'pending_count', 'awaiting'),
    'approved_today': ('approved_today', 'approved_today_count', 'today_approved'),
    'executed': ('live_executions', 'executed', 'executed_count', 'executed_live'),
    'simulated': ('demo_executions', 'simulated', 'simulated_count', 'executed_demo'),
    'rejected': ('rejected', 'rejected_count'),
    'median_decision_hours': ('median_decision_hours', 'median_hours_to_decision',
                              'median_hours', 'median_decision', 'median'),
}

# Extra figures approvals.statistics() supplies that the local fallback cannot,
# passed through to the page when they are there. 'edited_before_approval' is
# worth reading twice: it says whether the employees are proposing work people
# accept as written.
STAT_EXTRAS = ('edited_before_approval', 'oldest_pending_hours', 'total', 'failed')


def _local_statistics():
    """The six headline figures, computed from nothing but the queue itself."""
    queryset = ProposedAction.objects.all()
    today = timezone.localdate()

    decided = queryset.filter(decided_at__isnull=False).values_list(
        'created_at', 'decided_at')
    hours = [(decided_at - created_at).total_seconds() / 3600
             for created_at, decided_at in decided]

    return {
        'pending': queryset.filter(status='pending').count(),
        'approved_today': queryset.filter(
            decided_at__date=today,
            status__in=('approved', 'executing', 'executed', 'failed')).count(),
        'executed': queryset.filter(status='executed', executed_in_demo=False).count(),
        'simulated': queryset.filter(status='executed', executed_in_demo=True).count(),
        'rejected': queryset.filter(status='rejected').count(),
        'median_decision_hours': _median(hours),
    }


def _statistics():
    """``approvals.statistics()`` where available, normalised and backfilled.

    The pipeline's own numbers win, because it knows things this module does
    not, but any figure it omits is filled in locally so a KPI tile is never
    blank.
    """
    stats = _local_statistics()

    fn = _pipeline('statistics')
    if fn is None:
        return stats

    try:
        supplied = _invoke(fn, _values()) or {}
    except Exception:                                  # noqa: BLE001
        # A read-only page decoration must never be the thing that stops a
        # reviewer seeing the queue. See the module docstring.
        return stats

    if not isinstance(supplied, dict):
        return stats

    for canonical, names in STAT_ALIASES.items():
        for name in names:
            if name in supplied and supplied[name] is not None:
                stats[canonical] = supplied[name]
                break

    # The per-status breakdown, when it is given as one nested dictionary.
    by_status = supplied.get('by_status')
    if isinstance(by_status, dict):
        for canonical, key in (('rejected', 'rejected'), ('pending', 'pending')):
            if key in by_status:
                stats[canonical] = by_status[key]

    for name in STAT_EXTRAS:
        if supplied.get(name) is not None:
            stats[name] = supplied[name]

    return stats


def _reminder_row(entry):
    """One overdue item, normalised whatever shape the pipeline returned it in."""
    if isinstance(entry, ProposedAction):
        return {
            'pk': entry.pk,
            'title': entry.title,
            'risk': entry.risk,
            'risk_label': entry.get_risk_display(),
            'age_hours': entry.age_hours,
            'agent': entry.agent.name if entry.agent else 'the system',
            'url': reverse('action_detail', args=[entry.pk]),
        }

    if isinstance(entry, dict):
        action = entry.get('action')
        pk = entry.get('pk') or entry.get('id') or entry.get('action_id')
        if isinstance(action, ProposedAction):
            row = _reminder_row(action)
            row['message'] = entry.get('message') or entry.get('note') or ''
            return row
        return {
            'pk': pk,
            'title': entry.get('title') or 'An action is overdue',
            'risk': entry.get('risk') or '',
            'risk_label': (entry.get('risk') or '').title(),
            'age_hours': entry.get('age_hours') or entry.get('age') or 0,
            'agent': entry.get('agent') or '',
            'message': entry.get('message') or entry.get('note') or '',
            'url': reverse('action_detail', args=[pk]) if pk else '',
        }

    return {'pk': None, 'title': str(entry), 'risk': '', 'risk_label': '',
            'age_hours': 0, 'agent': '', 'message': '', 'url': ''}


def _overdue_hours():
    """How long is too long, as the pipeline defines it.

    The interval is a policy decision belonging to the organisation, so the
    pipeline reads it from a setting. The page quotes the same number rather
    than a constant of its own, or the panel heading would disagree with the
    rows underneath it.
    """
    if approvals is not None:
        value = getattr(approvals, 'DEFAULT_REMINDER_HOURS', None)
        getter = getattr(approvals, 'setting_value', None)
        if callable(getter):
            try:
                value = getter('approval_reminder_hours',
                               value or OVERDUE_HOURS)
            except Exception:                          # noqa: BLE001
                pass
        try:
            return round(float(value))
        except (TypeError, ValueError):
            pass
    return OVERDUE_HOURS


def _reminders():
    """Overdue pending actions, from the pipeline or worked out locally."""
    fn = _pipeline('reminders')
    if fn is not None:
        try:
            supplied = _invoke(fn, _values())
        except Exception:                              # noqa: BLE001
            supplied = None
        if supplied is not None:
            try:
                return [_reminder_row(entry) for entry in supplied][:20]
            except TypeError:                          # not iterable
                pass

    cutoff = timezone.now() - timedelta(hours=OVERDUE_HOURS)
    overdue = (ProposedAction.objects.filter(status='pending', created_at__lt=cutoff)
               .select_related('agent').order_by('created_at')[:20])
    return [_reminder_row(action) for action in overdue]


# ===========================================================================
# Helpers: the payload, and never showing a secret
# ===========================================================================

def _secret_keys(payload):
    """Payload keys whose values must never reach the browser.

    A tool that had to put a credential in the argument set names it here, and
    this module honours that everywhere a value could be rendered: the queue
    preview, the review form, the before-and-after comparison, and the JSON
    responses.
    """
    declared = (payload or {}).get('_secret_keys')
    if isinstance(declared, (list, tuple, set)):
        return {str(key) for key in declared}
    if isinstance(declared, str):
        return {part.strip() for part in declared.split(',') if part.strip()}
    return set()


def _readable(value):
    """A payload value as text a reviewer can read."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, indent=2, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _label_for(key):
    return str(key).replace('_', ' ').strip().capitalize()


def _choices_for(entry):
    """Normalise a choice field's options to (value, label) pairs."""
    raw = entry.get('choices') or entry.get('options') or []
    pairs = []
    if isinstance(raw, dict):
        raw = list(raw.items())
    for option in raw:
        if isinstance(option, (list, tuple)) and len(option) >= 2:
            pairs.append((str(option[0]), str(option[1])))
        else:
            pairs.append((str(option), str(option)))
    return pairs


def _field_row(entry, payload, secrets, *, editable):
    """One control on the review form, or one read-only row."""
    key = str(entry.get('key'))
    value = (payload or {}).get(key)

    field_type = str(entry.get('field_type') or 'text').lower()
    if field_type in ('textarea', 'long', 'text_area'):
        field_type = 'longtext'
    if field_type in ('bool', 'checkbox'):
        field_type = 'boolean'
    if field_type in ('select', 'options'):
        field_type = 'choice'
    if field_type in ('int', 'integer', 'float', 'decimal'):
        field_type = 'number'
    if field_type not in FIELD_TYPES:
        field_type = 'text'

    # A structured value cannot be edited safely in a text box, so it is shown
    # as formatted JSON and left alone.
    if isinstance(value, (dict, list, tuple)):
        field_type = 'json'
        editable = False

    try:
        rows = max(2, min(30, int(entry.get('rows') or 6)))
    except (TypeError, ValueError):
        rows = 6

    is_secret = key in secrets

    return {
        'key': key,
        'dom_id': f'payloadField-{key}',
        'label': entry.get('label') or _label_for(key),
        'help_text': entry.get('help_text') or entry.get('hint') or '',
        'field_type': field_type,
        'rows': rows,
        'choices': _choices_for(entry),
        'is_secret': is_secret,
        'editable': bool(editable) and not is_secret,
        'value': MASK if is_secret else _readable(value),
        'checked': bool(value) if field_type == 'boolean' else False,
    }


def _payload_fields(action):
    """The review form, split into what may be changed and what may not.

    Everything the tool declared in ``editable_fields`` becomes a control.
    Everything else in the payload is still shown, read-only, because a
    reviewer deciding whether an action should happen needs to see the whole
    argument set -- and must not be able to change something the tool never
    intended to expose.
    """
    payload = action.payload or {}
    secrets = _secret_keys(payload)

    editable_rows = []
    claimed = set()
    for entry in (action.editable_fields or []):
        if not isinstance(entry, dict):
            continue
        key = entry.get('key')
        if not key or str(key).startswith('_') or key in claimed:
            continue
        claimed.add(key)
        editable_rows.append(_field_row(entry, payload, secrets, editable=True))

    readonly_rows = []
    for key in payload:
        if key in claimed or str(key).startswith('_'):
            continue
        readonly_rows.append(
            _field_row({'key': key}, payload, secrets, editable=False))

    return editable_rows, readonly_rows


def _diff_rows(action):
    """What the employee wrote beside what the payload now says.

    This is the accountability the whole design turns on. If a reviewer changed
    an employee's words before approving, the difference between the two is
    recoverable for as long as the row exists.
    """
    secrets = _secret_keys(action.payload or {})
    rows = []
    for key, proposed, current in action.payload_diff:
        if str(key).startswith('_'):
            continue
        masked = key in secrets
        rows.append({
            'key': key,
            'label': _label_for(key),
            'proposed': MASK if masked else _readable(proposed),
            'current': MASK if masked else _readable(current),
        })
    return rows


def _local_preview(action):
    """A one-line answer to "what will happen, and to whom".

    Keyed on the payload's own field names rather than on the action type, so a
    tool added later gets a sensible preview without this function knowing
    anything about it.
    """
    payload = action.payload or {}
    secrets = _secret_keys(payload)

    def first(*keys):
        for key in keys:
            if key in secrets:
                continue
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                value = ', '.join(str(item) for item in value if item)
            if value not in (None, '', [], {}, ()):
                return str(value)
        return ''

    who = first('to', 'recipient', 'recipients', 'email', 'address', 'channel',
                'attendees', 'assignee', 'container', 'repository', 'repo',
                'project', 'board')
    when = first('start_at', 'start', 'when', 'scheduled_for', 'starts_at', 'date')
    what = first('subject', 'title', 'headline', 'name')
    body = first('body', 'message', 'text', 'content', 'description', 'caption',
                 'agenda', 'note', 'comment')

    parts = [part for part in (who, when, what) if part]
    headline = ' -- '.join(parts) if parts else action.title

    detail = ' '.join(body.split())[:220] if body else ''
    return {'headline': headline[:220], 'detail': detail}


def _preview_from_rows(rows):
    """Build the queue preview from ``approvals.summarise_payload()``.

    That function returns ``[(label, value, field_type)]`` in the order the
    tool declared its fields, and the tool knows which field a reviewer reads
    first. So the short fields become the headline in that order, and the first
    long field becomes the detail line. Nothing is masked here because the
    pipeline has already masked its own secrets.
    """
    headline = []
    detail = ''
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        label, value = str(row[0]), str(row[1] or '')
        field_type = str(row[2]) if len(row) > 2 else 'text'
        if not value:
            continue
        if field_type == 'longtext' or len(value) > 90:
            if not detail:
                detail = ' '.join(value.split())[:220]
            continue
        if len(headline) < 3:
            headline.append(f'{label}: {value}')

    return {'headline': ' -- '.join(headline)[:220], 'detail': detail}


def _preview(action):
    """The queue row's preview, preferring the pipeline's own summary."""
    fn = _pipeline('summarise_payload')
    if fn is not None:
        try:
            summary = _invoke(fn, _values(action=action))
        except Exception:                              # noqa: BLE001
            summary = None

        if isinstance(summary, (list, tuple)) and summary:
            built = _preview_from_rows(summary)
            if built['headline'] or built['detail']:
                if not built['headline']:
                    built['headline'] = action.title
                return built
        elif isinstance(summary, dict):
            return {'headline': str(summary.get('headline') or action.title)[:220],
                    'detail': str(summary.get('detail') or '')[:220]}
        elif isinstance(summary, str) and summary:
            return {'headline': summary[:220],
                    'detail': _local_preview(action)['detail']}

    return _local_preview(action)


# ===========================================================================
# Helpers: the integration's mode, said out loud
# ===========================================================================

def _mode_warning(action):
    """What approving this action will really do, when that is not obvious.

    An integration in demo mode simulates rather than sends. A reviewer who
    approves without knowing that has been misled by the interface, so the
    queue and the review page both say it before the button, not after. This
    honesty is the whole design.
    """
    integration = action.integration
    if integration is None:
        return None

    try:
        mode = integration.effective_mode
    except Exception:                                  # noqa: BLE001
        # effective_mode asks the connector whether it is configured, which
        # reaches into settings this page has no business failing over.
        mode = 'unknown'

    if mode == 'demo':
        return {
            'tone': 'warning',
            'icon': 'fa-flask',
            'label': 'Demo mode',
            'message': (f'{integration.name} is in demo mode. Approving this will '
                        f'simulate the action and record it, and nothing will '
                        f'actually leave the company.'),
        }
    if mode == 'unavailable':
        return {
            'tone': 'danger',
            'icon': 'fa-plug-circle-xmark',
            'label': 'Not connected',
            'message': (f'{integration.name} is set to live only but is not '
                        f'configured, so execution will fail rather than '
                        f'simulate. Connect it first.'),
        }
    if mode == 'disabled':
        return {
            'tone': 'danger',
            'icon': 'fa-ban',
            'label': 'Disabled',
            'message': (f'{integration.name} is switched off. Approving this will '
                        f'not carry it out.'),
        }
    return None


def _decorate(action):
    """Attach to one action everything the queue row needs to be read at a glance.

    Done here rather than in the template because working out whether an
    integration would simulate involves asking a connector, and a template is
    the wrong place for that.
    """
    action.preview = _preview(action)
    action.mode_warning = _mode_warning(action)
    action.risk_tone = RISK_TONE.get(action.risk, 'neutral')
    action.is_overdue = (action.status == 'pending'
                         and action.age_hours >= _overdue_hours())
    return action


# ===========================================================================
# Helpers: execution outcome, and the JSON shape returned after a decision
# ===========================================================================

def _outcome(action):
    """What execution actually did, once it has run.

    The external id or URL is looked for in three places: the result JSON the
    executor stored, and then the two record tables that execution writes. A
    reviewer wants the link to the thing that now exists, wherever it was put.
    """
    if action.status not in ('executed', 'failed', 'executing'):
        return None

    result = action.execution_result if isinstance(action.execution_result, dict) else {}
    external_id = str(result.get('external_id') or result.get('id') or '')
    external_url = str(result.get('external_url') or result.get('url') or
                       result.get('link') or '')

    message = action.messages.first()
    if message is not None:
        external_id = external_id or message.external_id
        external_url = external_url or message.external_url
    issue = action.external_issues.first()
    if issue is not None:
        external_id = external_id or issue.external_id or issue.reference
        external_url = external_url or issue.url
    event = action.calendar_events.first()
    if event is not None:
        external_id = external_id or event.external_id
        external_url = external_url or event.meeting_link

    return {
        'summary': action.execution_summary,
        'demo': action.executed_in_demo,
        'failed': action.status == 'failed',
        'executed_at': action.executed_at,
        'external_id': external_id,
        'external_url': external_url if external_url.startswith('http') else '',
        'message': message,
        'issue': issue,
        'event': event,
    }


def _serialise(action):
    """One action in the shape static/js/actions.js updates a row from."""
    outcome = _outcome(action) or {}
    return {
        'id': action.pk,
        'title': action.title,
        'status': action.status,
        'status_label': action.get_status_display(),
        'risk': action.risk,
        'risk_label': action.get_risk_display(),
        'risk_tone': RISK_TONE.get(action.risk, 'neutral'),
        'was_edited': action.was_edited,
        'edit_count': action.edit_count,
        'decision_summary': action.decision_summary,
        'execution_summary': action.execution_summary,
        'executed_in_demo': action.executed_in_demo,
        'external_url': outcome.get('external_url', ''),
        'external_id': outcome.get('external_id', ''),
        'url': reverse('action_detail', args=[action.pk]),
    }


def _reload(action):
    """Re-read the row so a response reports what the pipeline actually wrote."""
    return ProposedAction.objects.select_related(
        'agent', 'integration', 'decided_by').get(pk=action.pk)


# ===========================================================================
# Page 1: the action queue
# ===========================================================================

def _status_tabs(queryset):
    """The status tabs with their counts, ready to loop over."""
    total = queryset.count()
    tabs = []
    for value, label, icon in STATUS_TABS:
        count = total if value == 'all' else queryset.filter(status=value).count()
        tabs.append({'value': value, 'label': label, 'icon': icon, 'count': count})
    return tabs

@login_required(login_url='login')
@permission_required(roles.CAN_VIEW_ACTIONS, raise_exception=True)
def actions_view(request):
    """Everything waiting on a person, pending first and then newest first.

    WHY PENDING SORTS ABOVE EVERYTHING ELSE
    Ordering purely by date would push a still-undecided action from Tuesday
    below a decided one from this morning. The queue exists to answer one
    question -- what needs me? -- so an undecided row is never below a settled
    one, whatever the filter.

    Every filter is a GET parameter and every one of them survives pagination,
    because a reviewer who has narrowed the queue to high-risk email and then
    turns the page has not asked to see everything again.
    """
    queryset = ProposedAction.objects.select_related(
        'agent', 'integration', 'decided_by', 'task', 'conversation')

    status_filter = (request.GET.get('status') or 'pending').strip()
    risk_filter = (request.GET.get('risk') or 'all').strip()
    agent_filter = (request.GET.get('agent') or 'all').strip()
    app_filter = (request.GET.get('app') or 'all').strip()
    type_filter = (request.GET.get('type') or 'all').strip()
    search_query = (request.GET.get('q') or '').strip()

    visible = queryset
    if status_filter == 'decided':
        visible = visible.exclude(status='pending')
    elif status_filter != 'all':
        visible = visible.filter(status=status_filter)
    if risk_filter != 'all':
        visible = visible.filter(risk=risk_filter)
    if agent_filter != 'all':
        visible = visible.filter(agent__agent_type=agent_filter)
    if app_filter != 'all':
        visible = visible.filter(integration__provider_key=app_filter)
    if type_filter != 'all':
        visible = visible.filter(action_type=type_filter)
    if search_query:
        visible = visible.filter(
            Q(title__icontains=search_query)
            | Q(summary__icontains=search_query)
            | Q(subject_display__icontains=search_query)
            | Q(action_type__icontains=search_query))

    visible = visible.annotate(
        queue_rank=Case(When(status='pending', then=Value(0)),
                        default=Value(1), output_field=IntegerField()),
    ).order_by('queue_rank', '-created_at')

    paginator = Paginator(visible, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))
    actions = [_decorate(action) for action in page_obj.object_list]

    # The filters as a querystring, so partials/_pagination.html can carry
    # them into the next page without knowing what they are.
    carried = {key: value for key, value in request.GET.items()
               if key != 'page' and value}

    action_types = sorted(
        set(queryset.exclude(action_type='')
            .values_list('action_type', flat=True).distinct()))

    context = {
        'page_obj': page_obj,
        'actions': actions,
        'querystring': urlencode(carried),
        'status_filter': status_filter,
        'risk_filter': risk_filter,
        'agent_filter': agent_filter,
        'app_filter': app_filter,
        'type_filter': type_filter,
        'search_query': search_query,
        'has_filters': bool(search_query) or any(
            value != 'all' for value in
            (risk_filter, agent_filter, app_filter, type_filter)),
        # The tabs are assembled here with their counts already in them. A
        # Django template cannot look a dictionary up by a loop variable, so
        # doing this in the view is what keeps the template readable.
        'status_tabs': _status_tabs(queryset),
        'total_count': queryset.count(),
        'risk_choices': ProposedAction.RISK_CHOICES,
        'agent_choices': AIAgent.AGENT_TYPE_CHOICES,
        'integrations': Integration.objects.order_by('name'),
        'action_types': action_types,
        'stats': _statistics(),
        'reminders': _reminders(),
        'overdue_hours': _overdue_hours(),
        'can_decide': _requires(request, roles.CAN_DECIDE_ACTION),
        'pipeline_ready': approvals is not None,
    }
    return render(request, 'actions.html', context)


# ===========================================================================
# Page 2: one action, in full
# ===========================================================================

@login_required(login_url='login')
@permission_required(roles.CAN_VIEW_ACTIONS, raise_exception=True)
def action_detail_view(request, pk):
    """The review screen: everything needed to decide, and nothing else.

    Three separate acts are offered, deliberately never combined into one
    button. Saving edits does not approve; approving executes; rejecting
    requires a reason. A screen where the only obvious button both changes an
    employee's words and sends them is not a review screen.
    """
    action = get_object_or_404(
        ProposedAction.objects.select_related(
            'agent', 'integration', 'task', 'conversation', 'source_message',
            'requested_by', 'decided_by'),
        pk=pk)

    _decorate(action)
    editable_rows, readonly_rows = _payload_fields(action)

    task = action.task
    steps = list(task.steps or []) if task else []

    context = {
        'action': action,
        'editable_rows': editable_rows,
        'readonly_rows': readonly_rows,
        'diff_rows': _diff_rows(action),
        'trail': action.trail.select_related('actor'),
        'outcome': _outcome(action),
        'task': task,
        'task_steps': steps,
        'can_decide': _requires(request, roles.CAN_DECIDE_ACTION),
        'pipeline_ready': approvals is not None,
        'employee_url': (reverse('employee_profile', args=[action.agent.agent_type])
                         if action.agent else ''),
        'integration_url': (reverse('integration_detail',
                                    args=[action.integration.provider_key])
                            if action.integration else ''),
    }
    return render(request, 'action_detail.html', context)


# ===========================================================================
# Page 3: the orchestrator console
# ===========================================================================

@login_required(login_url='login')
@permission_required(roles.CAN_VIEW_ACTIONS, raise_exception=True)
def orchestrator_view(request):
    """One input box, and an honest account of what it did with what you typed.

    Routing is a decision the platform makes on somebody's behalf, and a
    decision made on your behalf should be inspectable. So the console shows
    the confidence, the reasoning, and the scores of the employees that were
    not chosen, rather than presenting the choice as magic.
    """
    employees = []
    for row in workforce.AGENT_BLUEPRINT:
        agent_type = row['agent_type']
        employees.append({
            'agent_type': agent_type,
            'name': row['name'],
            'role': row['role'],
            'icon': row['avatar_icon'],
            'color': row['avatar_color'],
            'description': row['persona_description'],
            'example': EXAMPLE_REQUESTS.get(agent_type, ''),
            'url': reverse('employee_profile', args=[agent_type]),
        })

    context = {
        'employees': employees,
        'decisions': (OrchestratorDecision.objects
                      .select_related('chosen_agent', 'requested_by', 'task')[:15]),
        'can_chat': _requires(request, roles.CAN_CHAT),
        'router_ready': orchestrator is not None,
    }
    return render(request, 'orchestrator.html', context)


# ===========================================================================
# Endpoint: decide one action
# ===========================================================================

@login_required(login_url='login')
@require_POST
def api_action_decide(request):
    """Approve, reject or cancel one proposed action.

    Approving is two pipeline steps, not one: ``approve`` records the decision,
    ``execute`` carries it out. They are called in sequence here because a
    reviewer pressing "Approve and execute" means both, but they stay separate
    functions so a pipeline that defers execution to a worker is not forced to
    run it inline.
    """
    if not _requires(request, roles.CAN_DECIDE_ACTION):
        return _forbidden(roles.CAN_DECIDE_ACTION)
    if approvals is None:
        return _unavailable('approval pipeline')

    data = _json_body(request)
    decision = (data.get('decision') or '').strip()
    reason = (data.get('reason') or '').strip()

    if decision not in ('approved', 'rejected', 'cancelled'):
        return _error('The decision must be "approved", "rejected" or "cancelled".')
    if decision in ('rejected', 'cancelled') and not reason:
        return _error('A reason is required when rejecting or cancelling an action.')

    action = get_object_or_404(ProposedAction, pk=data.get('action_id'))
    if not action.is_pending:
        return _error(
            f'That action is already {action.get_status_display().lower()}.', status=409)

    name = {'approved': 'approve', 'rejected': 'reject', 'cancelled': 'cancel'}[decision]
    fn = _pipeline(name)
    if fn is None:
        return _error(f'The approval pipeline has no {name}() to call.', status=503)

    try:
        _invoke(fn, _values(action=action, actor=request.user, reason=reason))
    except TypeError as exc:
        return _error(str(exc), status=503)
    except ValueError as exc:
        return _error(str(exc))

    action = _reload(action)

    # Approving without executing would leave a reviewer believing the message
    # had gone. If approve() has not already run it, run it now.
    if decision == 'approved' and action.status == 'approved':
        execute = _pipeline('execute')
        if execute is not None:
            try:
                _invoke(execute, _values(action=action, actor=request.user))
            except TypeError as exc:
                return _error(
                    f'The action was approved but could not be executed: {exc}',
                    status=503)
            except ValueError as exc:
                return _error(f'The action was approved but could not be executed: {exc}')
            action = _reload(action)

    return JsonResponse({
        'status': 'success',
        'message': _decision_message(action, decision),
        'action': _serialise(action),
        'pending_count': _pending_count(),
    })


def _decision_message(action, decision):
    """The sentence the toast shows. It says what really happened."""
    if decision == 'rejected':
        return f'"{action.title}" was rejected. Nothing was sent.'
    if decision == 'cancelled':
        return f'"{action.title}" was cancelled.'
    if action.status == 'executed':
        how = 'simulated in demo mode' if action.executed_in_demo else 'carried out'
        return f'"{action.title}" was approved and {how}.'
    if action.status == 'failed':
        return f'"{action.title}" was approved but execution failed.'
    return f'"{action.title}" was approved.'


# ===========================================================================
# Endpoint: save a reviewer's edits, WITHOUT approving
# ===========================================================================

@login_required(login_url='login')
@require_POST
def api_action_edit(request):
    """Store payload changes and leave the action pending.

    Editing is not approving, and this endpoint is the reason that is true
    rather than merely intended: it writes the payload, records the edit in the
    trail through the pipeline, and returns with the status untouched. A second,
    separate, deliberate act releases it.

    Only keys the tool declared in ``editable_fields`` are accepted, and a key
    named in ``_secret_keys`` is refused even if it was declared editable. A
    reviewer cannot reach a field the tool did not offer, whatever the request
    body says.
    """
    if not _requires(request, roles.CAN_DECIDE_ACTION):
        return _forbidden(roles.CAN_DECIDE_ACTION)
    if approvals is None:
        return _unavailable('approval pipeline')

    data = _json_body(request)
    action = get_object_or_404(ProposedAction, pk=data.get('action_id'))
    if not action.is_pending:
        return _error('Only a pending action can be edited.', status=409)

    submitted = data.get('changes')
    if not isinstance(submitted, dict) or not submitted:
        return _error('No changes were supplied.')

    secrets = _secret_keys(action.payload or {})
    allowed = {}
    for entry in (action.editable_fields or []):
        if isinstance(entry, dict) and entry.get('key'):
            allowed[str(entry['key'])] = entry

    changes = {}
    refused = []
    for key, value in submitted.items():
        key = str(key)
        if key not in allowed or key in secrets or key.startswith('_'):
            refused.append(key)
            continue
        coerced, problem = _coerce(value, allowed[key])
        if problem:
            return _error(f'{allowed[key].get("label") or _label_for(key)}: {problem}')
        if coerced != (action.payload or {}).get(key):
            changes[key] = coerced

    if refused:
        return _error(
            'These fields are not editable on this action: ' + ', '.join(sorted(refused)))
    if not changes:
        return _error('Nothing was changed.')

    note = (data.get('note') or '').strip() or 'Payload edited before review.'

    fn = _pipeline('edit')
    if fn is None:
        return _error('The approval pipeline has no edit() to call.', status=503)

    try:
        _invoke(fn, _values(action=action, actor=request.user,
                            changes=changes, note=note))
    except TypeError as exc:
        return _error(str(exc), status=503)
    except ValueError as exc:
        return _error(str(exc))

    action = _reload(action)
    changed = ', '.join(sorted(changes))

    return JsonResponse({
        'status': 'success',
        'message': (f'Saved {changed}. The action is still pending: '
                    f'approving it is a separate step.'),
        'action': _serialise(action),
        'changed': sorted(changes),
        'pending_count': _pending_count(),
    })


def _coerce(value, entry):
    """Bring one submitted value to the type its field declared.

    Returns (value, problem). The browser sends strings; the payload is JSON and
    a number that arrives as "12" must not be stored as text, or the executor
    receives an argument of the wrong type.
    """
    field_type = str(entry.get('field_type') or 'text').lower()

    if field_type in ('boolean', 'bool', 'checkbox'):
        if isinstance(value, bool):
            return value, ''
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on'), ''

    if field_type in ('number', 'int', 'integer', 'float', 'decimal'):
        if isinstance(value, bool):
            return None, 'a number is required.'
        if isinstance(value, (int, float)):
            return value, ''
        text = str(value).strip()
        if not text:
            return None, 'a number is required.'
        try:
            return int(text), ''
        except ValueError:
            try:
                return float(text), ''
            except ValueError:
                return None, f'"{text[:40]}" is not a number.'

    if field_type in ('choice', 'select', 'options'):
        options = [option for option, _ in _choices_for(entry)]
        text = str(value)
        if options and text not in options:
            return None, f'"{text[:40]}" is not one of the permitted choices.'
        return text, ''

    if isinstance(value, (dict, list)):
        return None, 'a structured value cannot be edited here.'

    return str(value), ''


# ===========================================================================
# Endpoint: one decision applied to a selection
# ===========================================================================

@login_required(login_url='login')
@require_POST
def api_action_bulk(request):
    """Apply the same decision to several actions at once.

    Bulk approval is offered because a queue of forty low-risk internal notes
    is otherwise unworkable. It is still the pipeline that decides each one, so
    a row that has already been settled between the page loading and the button
    being pressed is skipped rather than decided twice.
    """
    if not _requires(request, roles.CAN_DECIDE_ACTION):
        return _forbidden(roles.CAN_DECIDE_ACTION)
    if approvals is None:
        return _unavailable('approval pipeline')

    data = _json_body(request)
    decision = (data.get('decision') or '').strip()
    reason = (data.get('reason') or '').strip()

    if decision not in ('approved', 'rejected'):
        return _error('The decision must be either "approved" or "rejected".')
    if decision == 'rejected' and not reason:
        return _error('A reason is required when rejecting actions.')

    raw_ids = data.get('action_ids') or []
    if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
        return _error('Select at least one action first.')

    action_ids = []
    for value in raw_ids:
        try:
            action_ids.append(int(value))
        except (TypeError, ValueError):
            return _error('One of the selected identifiers was not a number.')

    pending_ids = list(ProposedAction.objects.filter(
        pk__in=action_ids, status='pending').values_list('pk', flat=True))
    if not pending_ids:
        return _error('None of the selected actions are still pending.', status=409)

    fn = _pipeline('bulk_decide')
    try:
        if fn is not None:
            outcome = _invoke(fn, _values(actor=request.user, reason=reason,
                                          decision=decision, action_ids=pending_ids))
            updated = _count_from(outcome, len(pending_ids))
        else:
            # No bulk_decide: fall back to deciding them one at a time, which
            # is slower but identical in effect and in what it records.
            updated = _one_at_a_time(pending_ids, request.user, decision, reason)
    except TypeError as exc:
        return _error(str(exc), status=503)
    except ValueError as exc:
        return _error(str(exc))

    return JsonResponse({
        'status': 'success',
        'message': (f'{updated} action{"" if updated == 1 else "s"} '
                    f'{"approved" if decision == "approved" else "rejected"}.'),
        'updated': updated,
        'action_ids': pending_ids,
        'pending_count': _pending_count(),
    })


def _count_from(outcome, fallback):
    """How many rows a bulk call reports having changed, however it says so."""
    if isinstance(outcome, int):
        return outcome
    if isinstance(outcome, dict):
        for key in ('updated', 'count', 'decided', 'changed'):
            if isinstance(outcome.get(key), int):
                return outcome[key]
    if isinstance(outcome, (list, tuple)):
        return len(outcome)
    return fallback


def _one_at_a_time(pending_ids, actor, decision, reason):
    name = 'approve' if decision == 'approved' else 'reject'
    fn = _pipeline(name)
    if fn is None:
        raise TypeError(f'The approval pipeline has no {name}() to call.')
    execute = _pipeline('execute') if decision == 'approved' else None

    updated = 0
    for action in ProposedAction.objects.filter(pk__in=pending_ids, status='pending'):
        _invoke(fn, _values(action=action, actor=actor, reason=reason))
        if execute is not None:
            fresh = _reload(action)
            if fresh.status == 'approved':
                _invoke(execute, _values(action=fresh, actor=actor))
        updated += 1
    return updated


# ===========================================================================
# Endpoint: run a failed execution again
# ===========================================================================

@login_required(login_url='login')
@require_POST
def api_action_retry(request):
    """Try a failed execution again.

    Only a failed action may be retried. Retrying an executed one would send
    the same message twice, and there is no interface convenience worth that.
    """
    if not _requires(request, roles.CAN_DECIDE_ACTION):
        return _forbidden(roles.CAN_DECIDE_ACTION)
    if approvals is None:
        return _unavailable('approval pipeline')

    action = get_object_or_404(ProposedAction, pk=_json_body(request).get('action_id'))
    if action.status != 'failed':
        return _error('Only a failed action can be retried.', status=409)

    fn = _pipeline('retry')
    if fn is None:
        return _error('The approval pipeline has no retry() to call.', status=503)

    try:
        _invoke(fn, _values(action=action, actor=request.user))
    except TypeError as exc:
        return _error(str(exc), status=503)
    except ValueError as exc:
        return _error(str(exc))

    action = _reload(action)
    return JsonResponse({
        'status': 'success',
        'message': (f'"{action.title}" was retried and is now '
                    f'{action.get_status_display().lower()}.'),
        'action': _serialise(action),
        'pending_count': _pending_count(),
    })


# ===========================================================================
# Endpoint: route a request, dispatch it, suggest a workflow
# ===========================================================================

@login_required(login_url='login')
@require_POST
def api_orchestrate(request):
    """Choose an employee for a request, have it do the work, and explain both.

    Three things come back, and the order matters. First the routing decision,
    with the runners-up, so the choice can be disagreed with. Then what the
    chosen employee actually produced, including any actions it queued for
    approval. Then, only when the request plainly needs more than one employee,
    a suggested sequence.

    Dispatch is allowed to fail without taking the routing with it. Knowing who
    was chosen and why is useful even when the work itself did not complete.
    """
    if not _requires(request, roles.CAN_CHAT):
        return _forbidden(roles.CAN_CHAT)
    if orchestrator is None:
        return _unavailable('orchestrator')

    data = _json_body(request)
    request_text = (data.get('request_text') or data.get('text') or '').strip()
    prefer = (data.get('prefer_agent_type') or '').strip()

    if not request_text:
        return _error('Describe what you need first.')
    if len(request_text) > 4000:
        return _error('That request is too long. Keep it under 4000 characters.')

    dispatch = _router('dispatch')
    route = _router('route')
    if dispatch is None and route is None:
        return _error('The orchestrator has neither dispatch() nor route() to call.',
                      status=503)

    # dispatch() routes the request itself and returns both halves, so it is
    # called INSTEAD of route() rather than after it. Calling both would route
    # twice and record two OrchestratorDecision rows for one request.
    routing = None
    outcome = None
    failure = ''

    if dispatch is not None:
        try:
            outcome = _call_router(dispatch, request_text, request.user, prefer)
            routing = _attr(outcome, 'routing')
        except Exception as exc:                       # noqa: BLE001
            failure = f'The employee was chosen but the work did not complete: {exc}'
            outcome = None

    if routing is None and route is not None:
        try:
            routing = _call_router(route, request_text, request.user, prefer)
        except Exception as exc:                       # noqa: BLE001
            return _error(f'The request could not be routed: {exc}', status=502)

    if routing is None:
        return _error('The orchestrator returned no routing decision.', status=502)

    result = _dispatch_dict(outcome)
    if failure:
        result['error'] = failure

    payload = {
        'status': 'success',
        'decision': _decision_dict(routing, _attr(outcome, 'explanation', default='')),
        'result': result,
        'workflow': [],
    }

    suggest = _router('suggest_workflow')
    if suggest is not None:
        try:
            payload['workflow'] = _workflow_list(_invoke(
                suggest, {'request_text': request_text, 'text': request_text,
                          'request': request_text}))
        except Exception:                              # noqa: BLE001
            payload['workflow'] = []

    chosen = payload['decision'].get('agent_name') or 'nobody'
    payload['message'] = f'Routed to {chosen}.'
    return JsonResponse(payload)


def _call_router(fn, request_text, user, prefer):
    """Call route() or dispatch() with whichever of those arguments it declares."""
    return _invoke(fn, {
        'request_text': request_text, 'text': request_text, 'request': request_text,
        'user': user, 'actor': user,
        'prefer_agent_type': prefer,
    })


def _attr(source, *names, default=None):
    """Read a value from either a dict or an object, whichever came back."""
    for name in names:
        if isinstance(source, dict):
            if name in source and source[name] is not None:
                return source[name]
        else:
            value = getattr(source, name, None)
            if value is not None:
                return value
    return default


def _decision_dict(routing, explanation=''):
    """The routing decision, with the runners-up, ready for the console.

    ``orchestrator.route()`` returns a dictionary carrying the chosen AIAgent,
    the confidence as a fraction, the reasoning, the candidate scores and the
    OrchestratorDecision row it wrote. The row is what route_explanation()
    reads, so the paragraph shown to the reader is the one that was actually
    recorded at the time rather than something recomputed now.
    """
    if routing is None:
        return {'agent_name': '', 'agent_role': '', 'confidence': 0,
                'reasoning': '', 'method': '', 'candidates': []}

    agent = _attr(routing, 'agent', 'chosen_agent')
    chosen_type = str(_attr(routing, 'agent_type', default='') or
                      getattr(agent, 'agent_type', '') or '')

    try:
        confidence = float(_attr(routing, 'confidence', default=0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    # route() expresses confidence as a fraction; a percentage is tolerated.
    percent = max(0, min(100, round(confidence * 100 if confidence <= 1 else confidence)))

    reasoning = str(explanation or '')
    if not reasoning:
        row = _attr(routing, 'decision')
        explain = _router('route_explanation')
        if explain is not None and row is not None:
            try:
                reasoning = str(_invoke(explain, {'decision': row}) or '')
            except Exception:                          # noqa: BLE001
                reasoning = ''
    if not reasoning:
        reasoning = str(_attr(routing, 'reasoning', default='') or '')

    return {
        'agent_name': getattr(agent, 'name', '') or 'No employee was chosen',
        'agent_role': getattr(agent, 'role', ''),
        'agent_type': chosen_type,
        'agent_icon': getattr(agent, 'avatar_icon', '') or 'fa-robot',
        'agent_color': getattr(agent, 'avatar_color', ''),
        'agent_url': (reverse('employee_profile', args=[agent.agent_type])
                      if getattr(agent, 'agent_type', '') else ''),
        'confidence': percent,
        'reasoning': reasoning,
        'method': str(_attr(routing, 'method', default='') or ''),
        'candidates': _candidate_list(_attr(routing, 'candidates', default=[]),
                                      chosen_type),
    }


def _candidate_list(raw, chosen_type=''):
    """The employees that were NOT chosen, with their scores.

    Shown so the choice is inspectable rather than magic. The chosen employee
    is dropped, since it is already named above the table. route() reports the
    terms each employee matched, which is the most useful thing to put in the
    "why it scored" column: a reader can see the choice turned on the word
    "sprint" and judge whether that was right.
    """
    rows = []
    if isinstance(raw, dict):
        raw = [{'agent_type': key, 'score': value} for key, value in raw.items()]
    if not isinstance(raw, (list, tuple)):
        return rows

    for entry in raw[:8]:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            entry = {'agent_type': entry[0], 'score': entry[1]}
        if not isinstance(entry, dict):
            continue

        agent_type = str(entry.get('agent_type') or entry.get('type') or '')
        if agent_type and agent_type == chosen_type:
            continue

        blueprint = workforce.blueprint_for(agent_type) or {}
        try:
            score = float(entry.get('score') or entry.get('confidence') or 0)
        except (TypeError, ValueError):
            score = 0.0

        matched = entry.get('matched') or []
        if isinstance(matched, (list, tuple)) and matched:
            reason = 'matched ' + ', '.join(f'"{term}"' for term in matched[:5])
        else:
            reason = str(entry.get('reason') or entry.get('why') or '')

        rows.append({
            'agent_type': agent_type,
            'name': entry.get('name') or blueprint.get('name') or agent_type,
            'score': round(score, 2),
            'reason': reason,
        })
    return rows


def _dispatch_dict(outcome):
    """What the chosen employee produced, normalised for the console.

    ``orchestrator.dispatch()`` returns ``{'ok', 'routing', 'result', 'agent',
    'explanation'}``, and the reply, the tool calls and the queued action ids
    are all inside ``result``. So this unwraps one level first, and falls back
    to reading the outer object when a caller hands it the inner one directly.
    """
    if outcome is None:
        return {'reply': '', 'tools': [], 'actions': [], 'error': '',
                'task_title': '', 'task_status': '', 'notice': ''}

    result = _attr(outcome, 'result')
    if result is None:
        result = outcome

    reply = _attr(result, 'reply', 'text', 'content', 'answer', default='')

    tools = _attr(result, 'tool_calls', 'tools', 'tools_used', 'tools_consulted',
                  default=[]) or []
    if not isinstance(tools, (list, tuple)):
        tools = []
    tool_names = []
    for tool in tools[:12]:
        if isinstance(tool, str):
            tool_names.append(tool)
        else:
            name = _attr(tool, 'name', 'tool', default='')
            if name:
                tool_names.append(str(name))

    raw_actions = _attr(result, 'pending_actions', 'actions', 'proposed_actions',
                        'action_ids', default=[]) or []
    if not isinstance(raw_actions, (list, tuple)):
        raw_actions = [raw_actions]

    action_ids = []
    for entry in raw_actions:
        if isinstance(entry, ProposedAction):
            action_ids.append(entry.pk)
        elif isinstance(entry, dict):
            value = entry.get('id') or entry.get('pk') or entry.get('action_id')
            if value:
                action_ids.append(value)
        else:
            try:
                action_ids.append(int(entry))
            except (TypeError, ValueError):
                continue

    actions = [{
        'id': action.pk,
        'title': action.title,
        'risk': action.risk,
        'risk_label': action.get_risk_display(),
        'risk_tone': RISK_TONE.get(action.risk, 'neutral'),
        'target_app': action.target_app,
        'icon': action.icon,
        'url': reverse('action_detail', args=[action.pk]),
    } for action in ProposedAction.objects.select_related('integration')
        .filter(pk__in=action_ids)]

    task = _attr(result, 'task')
    return {
        'reply': str(reply or ''),
        'tools': tool_names,
        'actions': actions,
        'task_title': getattr(task, 'title', ''),
        'task_status': (task.get_status_display() if task is not None else ''),
        'notice': str(_attr(result, 'notice', default='') or ''),
        'error': str(_attr(result, 'error', default='') or ''),
    }


def _workflow_list(raw):
    """A suggested multi-employee sequence, as ordered steps.

    ``orchestrator.suggest_workflow()`` already returns an empty list for a
    request one employee covers, and this keeps that rule rather than dressing
    a single step up as a workflow: a one-step sequence is only the routing
    decision restated, and repeating it would make the page look busier without
    telling anybody anything.
    """
    steps = _attr(raw, 'steps', default=raw) or []
    if not isinstance(steps, (list, tuple)):
        return []

    rows = []
    for index, entry in enumerate(steps[:8], start=1):
        if isinstance(entry, str):
            entry = {'description': entry}
        if not isinstance(entry, dict):
            continue
        agent_type = str(entry.get('agent_type') or entry.get('agent') or '')
        blueprint = workforce.blueprint_for(agent_type) or {}
        rows.append({
            'order': entry.get('step') or entry.get('order') or index,
            'agent_type': agent_type,
            'name': entry.get('name') or blueprint.get('name') or 'An employee',
            'icon': blueprint.get('avatar_icon') or 'fa-robot',
            'description': str(entry.get('why') or entry.get('description')
                               or entry.get('task') or ''),
            'reason': str(entry.get('reason') or ''),
            'url': (reverse('employee_profile', args=[agent_type])
                    if agent_type and blueprint else ''),
        })

    return rows if len(rows) > 1 else []
