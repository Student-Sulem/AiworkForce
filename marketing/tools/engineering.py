"""Tools for the Engineering Delivery employee (agent_type 'engineering_manager').

WHAT THIS EMPLOYEE ACTUALLY DOES
--------------------------------
It turns written requirements into work items, sizes and sequences them, fills
sprints, tracks what is late or blocked, and reports the truth about all of it.
Everything in the first half of this module acts immediately, because none of
it leaves the platform: a work item is the company's own record.

The second half is the part that reaches other systems -- filing a Jira issue,
opening a GitHub issue, telling a developer on Slack, booking a planning
meeting, uploading a document. Every one of those returns a ``Proposal`` and
stops. A person reads it, edits it if they wish, and approves it; only then
does the registered ``@executor`` run and write the ``ExternalIssue``,
``OutboundMessage`` or ``CalendarEvent`` row that records what happened.

THE BREAKDOWN AND ESTIMATION ENGINES ARE PLAIN PYTHON
-----------------------------------------------------
No tool in this module calls a language model. ``break_down_requirement``
splits real text on bullets, sentences and conjunctions, drops filler openers,
derives acceptance criteria from the vocabulary it actually finds, and assigns
a build-order hint by detecting which layer each clause talks about.
``estimate_complexity`` scores length, criteria count, dependency count and a
table of known cost signals. Both are deterministic, which is what makes them
worth trusting: the same requirement produces the same plan twice, and the
reasoning can be read in this file rather than guessed at.

The employee's chat reply supplies the prose. These tools supply the record.
"""

from datetime import date, datetime, timedelta

from django.utils import timezone

from ..models_eng import Project, Sprint, SprintReport, WorkItem, WorkItemComment
from ..models_platform import CalendarEvent, ExternalIssue, OutboundMessage
from .base import Proposal, ToolResult, editable, executor, tool

# ===========================================================================
# Schema shorthand
# ===========================================================================
# The tool registry wants a JSON schema per tool. Written out longhand, forty
# of them would bury the logic they describe, so these five helpers build the
# same structures in one line each.


def _obj(required=(), **props):
    return {'type': 'object', 'properties': props, 'required': list(required)}


def _s(description):
    return {'type': 'string', 'description': description}


def _i(description):
    return {'type': 'integer', 'description': description}


def _b(description):
    return {'type': 'boolean', 'description': description}


def _a(description):
    return {'type': 'array', 'items': {'type': 'string'}, 'description': description}


def _ai(description):
    return {'type': 'array', 'items': {'type': 'integer'}, 'description': description}


def _enum(description, values):
    return {'type': 'string', 'enum': list(values), 'description': description}


# ===========================================================================
# Small conversions
# ===========================================================================

_DATE_FORMATS = ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d %B %Y', '%d %b %Y',
                 '%Y/%m/%d', '%B %d %Y', '%b %d %Y')

_DATETIME_FORMATS = ('%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S',
                     '%d/%m/%Y %H:%M', '%d %b %Y %H:%M', '%Y-%m-%dT%H:%M:%S')


def _as_date(value):
    """A date from whatever the model passed, or None. Day-first for slashes."""
    if value in (None, '', 'null'):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:40]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _as_datetime(value):
    if value in (None, '', 'null'):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()[:40].replace('Z', '')
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    only_date = _as_date(text)
    return datetime(only_date.year, only_date.month, only_date.day, 10, 0) \
        if only_date else None


def _aware(moment):
    """Make a naive datetime aware, and never fail over a timezone setting."""
    if moment is None:
        return None
    try:
        if timezone.is_naive(moment):
            return timezone.make_aware(moment)
    except Exception:  # noqa: BLE001 -- an ambiguous local time is not worth a crash
        return moment
    return moment


def _as_list(value):
    """A clean list of strings from a list, a comma string or nothing."""
    if value in (None, ''):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).replace(';', ',').split(',')
            if part.strip()]


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _agent_of(ctx):
    return ctx.agent if getattr(ctx, 'agent', None) else None


def _user_of(ctx):
    user = getattr(ctx, 'user', None)
    return user if getattr(user, 'pk', None) else None


def _call(provider_key, operation, **kwargs):
    """Reach one integration.

    The import is local rather than at module level because the integration
    register imports every connector on first use, and a tool module should
    not force that to happen simply by being imported.
    """
    from .. import integrations
    return integrations.call(provider_key, operation, **kwargs)


# ===========================================================================
# Guarded lookups
# ===========================================================================
# A missing id is a result the model can act on, never an exception. Each of
# these says which list tool to call next, because that is the reply that gets
# the conversation unstuck.


def _no_project(project_id):
    message = (f'There is no project with id {project_id}. '
               'Call eng.list_projects to see the projects that exist, or '
               'eng.create_project to start one.')
    return ToolResult(ok=False, error=message, text=message)


def _no_item(work_item_id):
    message = (f'There is no work item with id {work_item_id}. '
               'Call eng.list_work_items to see the items that exist.')
    return ToolResult(ok=False, error=message, text=message)


def _no_sprint(sprint_id):
    message = (f'There is no sprint with id {sprint_id}. '
               'Call eng.get_project to see a project and its sprints, or '
               'eng.create_sprint to start one.')
    return ToolResult(ok=False, error=message, text=message)


def _find_project(project_id):
    return Project.objects.filter(pk=_as_int(project_id)).first()


def _find_item(work_item_id):
    return WorkItem.objects.filter(pk=_as_int(work_item_id)).first()


def _find_sprint(sprint_id):
    return Sprint.objects.filter(pk=_as_int(sprint_id)).first()


def _find_employee(employee_id):
    """An Employee row, tolerating the HR module not being importable yet."""
    number = _as_int(employee_id)
    if not number:
        return None
    try:
        from ..models_hr import Employee
    except ImportError:  # pragma: no cover -- HR module absent
        return None
    return Employee.objects.filter(pk=number).first()


# ===========================================================================
# The breakdown engine
# ===========================================================================
# Splitting a requirement is done with text, not with a model. The order below
# is the order a person reads in: bullet lines first, because somebody who
# bulleted their requirement has already done the splitting; then sentences;
# then conjunctions, but only where both halves are substantial enough to be
# separate pieces of work. Splitting "terms and conditions" into two tasks is
# the failure mode that last rule guards against.

_BULLET_MARKS = ('-', '*', '•', '·', '–', '—', '>')

_FILLER_OPENERS = (
    'as a user, i want to', 'as a user i want to', 'as an admin i want to',
    'the application should be able to', 'the system should be able to',
    'the system needs to be able to', 'we need to be able to',
    'the application should', 'the application must', 'the system should',
    'the system must', 'the system needs to', 'the app should', 'the app must',
    'we also need to', 'we need to', 'we should also', 'we should', 'we must',
    'we want to', 'we would like to', 'i need to', 'i want to', 'i would like',
    'there should also be', 'there should be', 'there needs to be',
    'make sure that', 'make sure', 'ensure that', 'ensure', 'it should also',
    'it should', 'it must', 'it needs to', 'please also', 'please',
    'in addition,', 'in addition', 'additionally,', 'additionally',
    'also,', 'also', 'basically,', 'basically', 'obviously,', 'obviously',
    'and then', 'and also', 'and', 'then', 'finally,', 'finally',
)

_CONJUNCTIONS = (' and then ', ' and also ', ' as well as ', ' after that ',
                 ' followed by ', ' and ', ' then ', ' plus ')

# Which layer of the system a clause is about, and therefore roughly when it
# can be built. Lower runs earlier. The point is not precision -- it is that a
# plan which puts the interface before the model it renders is obviously
# wrong, and a plan that gets that order right is one a developer can start on.
_LAYERS = (
    (10, 'data model', (
        'model', 'models', 'schema', 'migration', 'migrations', 'database', 'db',
        'table', 'tables', 'column', 'columns', 'field', 'fields', 'entity',
        'orm', 'persist', 'persistence', 'storage', 'store', 'record', 'records')),
    (20, 'api and backend', (
        'api', 'apis', 'endpoint', 'endpoints', 'route', 'routes', 'backend',
        'service', 'services', 'serializer', 'serialiser', 'view', 'views',
        'controller', 'webhook', 'auth', 'authentication', 'authorisation',
        'authorization', 'login', 'permission', 'permissions', 'validation',
        'integration', 'queue', 'worker', 'job')),
    (30, 'user interface', (
        'ui', 'ux', 'screen', 'screens', 'page', 'pages', 'form', 'forms',
        'template', 'templates', 'frontend', 'css', 'button', 'buttons',
        'dashboard', 'layout', 'display', 'render', 'chart', 'modal',
        'navigation', 'menu')),
    (40, 'tests', (
        'test', 'tests', 'testing', 'coverage', 'qa', 'regression', 'e2e')),
    (50, 'documentation', (
        'document', 'documentation', 'documented', 'readme', 'docs', 'guide',
        'changelog', 'runbook', 'handover')),
)

# Acceptance criteria a person would have written anyway, keyed to the words
# that make them relevant. Three at most are added to any one item, because a
# list of eight generic criteria is read by nobody.
_CRITERIA_HINTS = (
    (('valid', 'validate', 'validation', 'required', 'mandatory'),
     'Invalid input is rejected with a message that names the field at fault.'),
    (('login', 'sign in', 'signin', 'auth', 'password', 'token', 'session',
      'credential'),
     'A wrong credential is refused without revealing which half was wrong.'),
    (('permission', 'role', 'access', 'admin', 'authorise', 'authorize'),
     'A user without the permission is refused the action, not merely hidden from it.'),
    (('upload', 'file', 'image', 'attachment'),
     'An oversized or wrong-typed file is refused before anything is stored.'),
    (('email', 'notify', 'notification', 'message', 'alert'),
     'A delivery failure is surfaced and recorded rather than silently swallowed.'),
    (('search', 'filter', 'list', 'listing', 'report', 'query'),
     'An empty result set renders as an empty state, not as an error.'),
    (('delete', 'remove', 'archive'),
     'The removal is confirmed first and leaves no orphaned rows behind.'),
    (('payment', 'invoice', 'billing', 'price', 'refund', 'charge'),
     'Amounts are calculated server side, once, and rounded to two decimals.'),
    (('import', 'export', 'csv', 'sync', 'bulk'),
     'A malformed row fails that row alone and reports its line number.'),
    (('performance', 'fast', 'slow', 'cache', 'load', 'scale', 'concurrent'),
     'The stated response time holds at a representative data volume.'),
    (('api', 'endpoint', 'route'),
     'The endpoint returns the documented status code for success and for each failure.'),
    (('migration', 'schema', 'model', 'field'),
     'Existing rows survive the change, and the migration is reversible.'),
)

_STOPWORDS = {
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'their', 'have',
    'has', 'been', 'will', 'should', 'must', 'need', 'needs', 'able', 'when',
    'then', 'they', 'them', 'our', 'your', 'user', 'users', 'system', 'systems',
    'application', 'app', 'also', 'each', 'every', 'some', 'any', 'all', 'new',
    'make', 'made', 'sure', 'want', 'wants', 'would', 'could', 'shall', 'page',
    'thing', 'things', 'work', 'using', 'used', 'via', 'about', 'over', 'after',
    'before', 'where', 'which', 'while', 'there', 'here', 'more', 'most', 'less',
    'like', 'just', 'only', 'both', 'than', 'once', 'part', 'item', 'items',
}


def _clean_line(text):
    """Strip a bullet mark or a leading number from one line."""
    stripped = str(text or '').strip()
    while stripped[:1] in _BULLET_MARKS:
        stripped = stripped[1:].strip()
    digits = 0
    while digits < len(stripped) and stripped[digits].isdigit():
        digits += 1
    if digits and stripped[digits:digits + 1] in ('.', ')', ':'):
        stripped = stripped[digits + 1:].strip()
    return stripped


def _sentences(text):
    """Split one line into sentences, leaving decimal numbers intact."""
    pieces, current = [], []
    characters = list(str(text or ''))
    for index, character in enumerate(characters):
        current.append(character)
        if character in '.;!?':
            following = characters[index + 1] if index + 1 < len(characters) else ' '
            previous = characters[index - 1] if index else ' '
            if character == '.' and previous.isdigit() and following.isdigit():
                continue
            pieces.append(''.join(current))
            current = []
    if current:
        pieces.append(''.join(current))
    return [piece.strip(' .;!?\t') for piece in pieces if piece.strip(' .;!?\t')]


def _split_conjunctions(clause):
    """Split on conjunctions, but only where both halves are real work.

    Four words is the threshold. Below it the fragment is almost always part
    of a phrase rather than a second task, and producing 'Conditions' as a
    work item is worse than producing one slightly long one.
    """
    parts = [clause]
    for conjunction in _CONJUNCTIONS:
        result = []
        for part in parts:
            lowered = part.lower()
            if conjunction not in lowered:
                result.append(part)
                continue
            position = lowered.find(conjunction)
            left = part[:position].strip()
            right = part[position + len(conjunction):].strip()
            if len(left.split()) >= 4 and len(right.split()) >= 4:
                result.extend([left, right])
            else:
                result.append(part)
        parts = result
    return [part for part in parts if part.strip()]


def _strip_openers(text):
    """Remove the filler that starts most written requirements."""
    working = text.strip()
    changed = True
    while changed:
        changed = False
        lowered = working.lower()
        for opener in _FILLER_OPENERS:
            if lowered.startswith(opener) and len(working) > len(opener) + 3:
                following = working[len(opener):len(opener) + 1]
                if following in (' ', ',', ':'):
                    working = working[len(opener):].lstrip(' ,:').strip()
                    changed = True
                    break
    return working


def _truncate_words(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(' ', 1)[0]
    return (cut or text[:limit]).rstrip(' ,;-') + '...'


def _title_from_clause(clause):
    text = _strip_openers(_clean_line(clause)).rstrip(' .;,:')
    if not text:
        return ''
    text = text[0].upper() + text[1:]
    return _truncate_words(text, 120)


def _layer_for(text):
    """(sequence hint, layer name) for a piece of text.

    The layer with the most keyword hits wins; an equal score goes to the
    earlier layer, because building the lower layer first is never the more
    expensive mistake.
    """
    lowered = ' ' + str(text or '').lower().replace(',', ' ').replace('.', ' ') + ' '
    best_hint, best_name, best_score = 25, 'general', 0
    for hint, name, keywords in _LAYERS:
        score = sum(1 for word in keywords if f' {word} ' in lowered)
        if score > best_score:
            best_hint, best_name, best_score = hint, name, score
    return best_hint, best_name


def _criteria_for(title, clause):
    """Acceptance criteria derived from the words actually present."""
    lowered = f'{title} {clause}'.lower()
    criteria = [f'{title.rstrip(".")} behaves as described for the ordinary case.']
    for keywords, statement in _CRITERIA_HINTS:
        if len(criteria) >= 3:
            break
        if any(word in lowered for word in keywords) and statement not in criteria:
            criteria.append(statement)
    hint, _name = _layer_for(lowered)
    if hint < 40:
        criteria.append('At least one automated test covers this behaviour.')
    return criteria


def _break_text(text, max_items):
    """Split a written requirement into candidate work items.

    Returns a list of dictionaries, ordered so the list is buildable: data
    model before API before interface before tests before documentation, and
    original order within a layer.
    """
    clauses = []
    for line in str(text or '').replace('\r', '').split('\n'):
        cleaned = _clean_line(line)
        if not cleaned:
            continue
        for sentence in _sentences(cleaned):
            for part in _split_conjunctions(sentence):
                if len(part.split()) >= 2:
                    clauses.append(part.strip())

    candidates, seen = [], set()
    for position, clause in enumerate(clauses):
        title = _title_from_clause(clause)
        if not title or len(title) < 4:
            continue
        fingerprint = title.lower()[:70]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        hint, layer = _layer_for(clause)
        candidates.append({
            'title': title,
            'description': clause.strip(),
            'criteria': _criteria_for(title, clause),
            'sequence_hint': hint,
            'layer': layer,
            'position': position,
        })

    candidates.sort(key=lambda row: (row['sequence_hint'], row['position']))
    limit = max(1, min(_as_int(max_items) or 8, 20))
    return candidates[:limit]


# ===========================================================================
# The estimation engine
# ===========================================================================
# Every signal below is stated rather than hidden, which is the only reason an
# estimate from a machine is worth quoting. These are the things that have
# historically cost more than they looked like they would.

_COST_SIGNALS = (
    (('migration', 'migrate', 'schema change', 'backfill'), 3,
     'a schema migration over existing rows'),
    (('integration', 'third party', 'third-party', 'external api', 'webhook'), 3,
     'an integration with a system we do not control'),
    (('refactor', 'rewrite', 'restructure', 'rearchitect'), 3,
     'changing code that already works'),
    (('unknown', 'unclear', 'not sure', 'investigate', 'research', 'spike',
      'explore', 'find out'), 4,
     'work that is stated as not yet understood'),
    (('legacy', 'old system', 'undocumented'), 3,
     'legacy or undocumented code'),
    (('concurrency', 'race condition', 'locking', 'transaction', 'idempotent'), 4,
     'concurrency, which is rarely right first time'),
    (('payment', 'billing', 'invoice', 'refund', 'money'), 3,
     'money handling, where a mistake is expensive'),
    (('security', 'vulnerability', 'encryption', 'gdpr', 'privacy'), 2,
     'a security or privacy obligation'),
    (('auth', 'authentication', 'permission', 'sso', 'oauth'), 2,
     'authentication or permissions'),
    (('performance', 'optimise', 'optimize', 'scale', 'cache', 'slow'), 2,
     'a performance requirement that has to be measured'),
    (('real time', 'real-time', 'websocket', 'streaming'), 3,
     'real-time delivery'),
    (('report', 'analytics', 'dashboard', 'aggregate'), 2,
     'aggregation, which usually needs its own query work'),
    (('import', 'export', 'bulk', 'csv'), 1,
     'bulk data handling and its malformed rows'),
    (('email', 'notification', 'sms'), 1,
     'outbound delivery and its failure paths'),
)

_UNKNOWN_WORDS = ('unknown', 'unclear', 'not sure', 'investigate', 'research',
                  'spike', 'explore', 'find out', 'tbd', 'to be decided')

# Score band -> (complexity, points, hours). Fibonacci points, because the
# spacing is the honest part: 3 and 5 are distinguishable, 3 and 4 are not.
_BANDS = (
    (2.0, 'trivial', 1, 2.0),
    (4.0, 'small', 2, 5.0),
    (7.0, 'moderate', 3, 10.0),
    (11.0, 'large', 5, 20.0),
    (9999.0, 'large', 8, 32.0),
)


def _estimate(text, criteria_count=0, dependency_count=0, child_count=0):
    """Size a piece of work from its own text. Deterministic.

    Returns the verdict and, importantly, the drivers and the assumptions. An
    estimate without its assumptions gets quoted back as a commitment, which
    is how a reasonable guess becomes a broken promise.
    """
    body = str(text or '')
    words = len(body.split())
    score = min(words / 25.0, 4.0)
    drivers = []
    if words >= 60:
        drivers.append(f'{words} words of requirement, which usually covers '
                       f'more than one concern')

    criteria = max(0, min(_as_int(criteria_count) or 0, 8))
    score += criteria * 0.6
    if criteria >= 4:
        drivers.append(f'{criteria} acceptance criteria to satisfy')

    dependencies = max(0, _as_int(dependency_count) or 0)
    score += dependencies * 1.5
    if dependencies:
        drivers.append(f'{dependencies} dependency(ies) that must land first')

    children = max(0, _as_int(child_count) or 0)
    score += children * 0.8
    if children:
        drivers.append(f'{children} child item(s) already identified')

    lowered = body.lower()
    matched = []
    for keywords, weight, reason in _COST_SIGNALS:
        if any(word in lowered for word in keywords):
            score += weight
            matched.append(reason)
    drivers.extend(matched[:4])

    complexity, points, hours = 'moderate', 3, 10.0
    for ceiling, name, point_value, hour_value in _BANDS:
        if score < ceiling:
            complexity, points, hours = name, point_value, hour_value
            break

    genuinely_unknown = any(word in lowered for word in _UNKNOWN_WORDS)
    if genuinely_unknown and score >= 6:
        complexity = 'unknown'

    assumptions = [
        'The requirement as written is complete; anything discovered later is new scope.',
        'One developer familiar with this codebase, working without interruption.',
        'Code review and the automated tests named in the criteria are included.',
    ]
    if dependencies:
        assumptions.append('The dependencies are finished before this item starts.')
    if genuinely_unknown:
        assumptions.append('Something here is not yet understood -- raise a spike to '
                           'find out rather than treating this number as a commitment.')
    if not matched:
        assumptions.append('No migration, integration or refactor is involved; if one '
                           'is, this estimate is low.')

    return {
        'complexity': complexity,
        'points': points,
        'hours': round(hours, 1),
        'score': round(score, 2),
        'drivers': drivers or ['nothing unusual -- length and criteria count only'],
        'assumptions': assumptions,
        'needs_spike': complexity == 'unknown',
    }


def _estimate_sentence(verdict):
    return (f"{verdict['complexity']}, {verdict['points']} point(s), about "
            f"{verdict['hours']} hours. Driven by: "
            f"{'; '.join(verdict['drivers'][:3])}.")


# ===========================================================================
# Ordering, dependencies and load
# ===========================================================================

def _order_by_dependency(items):
    """Kahn's algorithm over ``depends_on``, priority first among the ready.

    Returns (ordered, unresolved). The unresolved list is a cycle -- items
    that can never become ready. It is reported rather than broken, because a
    dependency cycle is a planning mistake somebody has to fix, and an order
    invented around it would hide that mistake behind a plausible plan.
    """
    pool = {item.pk: item for item in items}
    incoming = {pk: set() for pk in pool}
    outgoing = {pk: set() for pk in pool}
    for item in items:
        for dependency in item.depends_on.all():
            if dependency.pk in pool and dependency.pk != item.pk:
                incoming[item.pk].add(dependency.pk)
                outgoing[dependency.pk].add(item.pk)

    ready = [pk for pk, waiting in incoming.items() if not waiting]
    ordered, placed = [], set()
    while ready:
        ready.sort(key=lambda pk: (-pool[pk].priority_weight, pool[pk].sequence_hint, pk))
        current = ready.pop(0)
        if current in placed:
            continue
        ordered.append(pool[current])
        placed.add(current)
        for follower in sorted(outgoing[current]):
            incoming[follower].discard(current)
            if not incoming[follower] and follower not in placed:
                ready.append(follower)

    unresolved = [pool[pk] for pk in pool if pk not in placed]
    return ordered, unresolved


def _blocking_reach(item, seen=None):
    """How many unfinished items this one holds up, directly and onwards."""
    seen = seen if seen is not None else set()
    total = 0
    for follower in item.blocks.all():
        if follower.pk in seen or follower.is_finished:
            continue
        seen.add(follower.pk)
        total += 1 + _blocking_reach(follower, seen)
    return total


def _tokens(text):
    """Significant words, crudely singularised, for evidence matching."""
    cleaned = ''.join(c.lower() if (c.isalnum() or c.isspace()) else ' '
                      for c in str(text or ''))
    words = set()
    for word in cleaned.split():
        if len(word) < 4 or word in _STOPWORDS or word.isdigit():
            continue
        words.add(word[:-1] if word.endswith('s') and len(word) > 4 else word)
    return words


def _open_items(project=None, sprint=None):
    query = WorkItem.objects.filter(status__in=WorkItem.OPEN_STATUSES)
    if project is not None:
        query = query.filter(project=project)
    if sprint is not None:
        query = query.filter(sprint=sprint)
    return query


def _status_counts(queryset):
    counts = {key: 0 for key, _label in WorkItem.STATUS_CHOICES}
    for row in queryset.values('status'):
        counts[row['status']] = counts.get(row['status'], 0) + 1
    return counts


def _counts_sentence(counts):
    parts = [f'{value} {key.replace("_", " ")}' for key, value in counts.items() if value]
    return ', '.join(parts) or 'no work items yet'


def _item_row(item):
    """One work item as a small dictionary, for a tool's data payload."""
    return {
        'id': item.pk,
        'reference': item.reference,
        'title': item.title,
        'type': item.item_type,
        'status': item.status,
        'priority': item.priority,
        'complexity': item.complexity,
        'points': item.estimate_points,
        'assignee': item.assignee_label,
        'due_date': item.due_date.isoformat() if item.due_date else None,
        'days_overdue': item.days_overdue,
        'sequence_hint': item.sequence_hint,
        'sprint': item.sprint.name if item.sprint_id else None,
        'external_reference': item.external_reference,
    }


def _tree_lines(items):
    return [f'{"  " * min(item.depth, 4)}{item.reference} [{item.item_type}] {item.title}'
            for item in items]


# ===========================================================================
# GROUP: Projects
# ===========================================================================

@tool(name='eng.create_project', title='Create a project',
      description=('Start a project: a name, a description, an owner, a target date, '
                   'the repository and Jira project it lives in, and its technology '
                   'stack. Gives every later work item a home and a reference prefix.'),
      group='Projects', agent_types=('engineering_manager',), icon='fa-diagram-project',
      capability='Create an engineering project',
      parameters=_obj(
          ('name',),
          name=_s('Project name.'),
          description=_s('What the project is for.'),
          key=_s("Short reference prefix, e.g. 'ENG'. Derived from the name if omitted."),
          repository=_s("GitHub repository as 'owner/name'."),
          jira_project_key=_s('Jira project key issues should be filed under.'),
          owner=_s('Person accountable for delivery.'),
          target_date=_s('Target completion date, e.g. 2026-03-31.'),
          tech_stack=_a('Technologies in use.')))
def create_project(ctx, name, description='', key='', repository='',
                   jira_project_key='', owner='', target_date=None, tech_stack=None):
    """Record a project.

    The key is derived from the name when nobody supplies one, because every
    work item reference is built from it and an item called 'None-14' helps
    nobody.
    """
    if not str(name or '').strip():
        message = 'A project needs a name. Ask what the project is called.'
        return ToolResult(ok=False, error=message, text=message)

    project = Project.objects.create(
        name=str(name).strip()[:200],
        description=str(description or '').strip(),
        key=str(key or '').strip(),
        repository=str(repository or '').strip()[:200],
        jira_project_key=str(jira_project_key or '').strip()[:20],
        owner=str(owner or '').strip()[:120],
        target_date=_as_date(target_date),
        start_date=timezone.localdate(),
        tech_stack=_as_list(tech_stack),
        status='planning',
        created_by_agent=_agent_of(ctx),
        created_by=_user_of(ctx),
    )

    detail = [f'Created project {project.key} "{project.name}" as id {project.pk}.']
    if project.target_date:
        detail.append(f'Target date {project.target_date:%d %b %Y}.')
    if project.repository:
        detail.append(f'Repository {project.repository}.')
    if project.jira_project_key:
        detail.append(f'Jira project {project.jira_project_key}.')
    detail.append(f'Work items will be referenced {project.key}-<id>. '
                  f'Next: break a requirement down with eng.break_down_requirement '
                  f'using project_id {project.pk}.')

    return ToolResult(
        ok=True, text=' '.join(detail),
        data={'project_id': project.pk, 'key': project.key, 'name': project.name,
              'target_date': project.target_date.isoformat() if project.target_date else None,
              'tech_stack': project.tech_stack},
        subject_label='marketing.project', subject_id=project.pk)


@tool(name='eng.list_projects', title='List projects',
      description='List the projects, optionally filtered by status, with progress.',
      group='Projects', agent_types=('engineering_manager',), icon='fa-list',
      reads_only=True, capability='List engineering projects',
      parameters=_obj(
          (),
          status=_enum('Restrict to one status.',
                       ('planning', 'active', 'on_hold', 'complete', 'cancelled')),
          limit=_i('Most projects to return. Default 20.')))
def list_projects(ctx, status='', limit=20):
    """Every project, with open and done counts so the reply is usable at once."""
    query = Project.objects.all()
    if status:
        query = query.filter(status=status)
    rows = list(query[:max(1, min(_as_int(limit) or 20, 100))])

    if not rows:
        where = f' with status {status}' if status else ''
        return ToolResult(
            ok=True, text=f'There are no projects{where}. eng.create_project starts one.',
            data={'projects': [], 'count': 0})

    lines, payload = [], []
    for project in rows:
        lines.append(f'{project.key} (id {project.pk}) {project.name} -- '
                     f'{project.get_status_display()}, {project.progress_percent}% done, '
                     f'{project.open_items} open item(s)')
        payload.append({'id': project.pk, 'key': project.key, 'name': project.name,
                        'status': project.status, 'owner': project.owner,
                        'progress_percent': project.progress_percent,
                        'open_items': project.open_items,
                        'done_items': project.done_items,
                        'target_date': project.target_date.isoformat()
                        if project.target_date else None})

    return ToolResult(ok=True,
                      text=f'{len(rows)} project(s):\n' + '\n'.join(lines),
                      data={'projects': payload, 'count': len(rows)})


@tool(name='eng.get_project', title='Get a project',
      description=('One project in full: its details, its sprints, and a count of its '
                   'work items by status.'),
      group='Projects', agent_types=('engineering_manager',), icon='fa-folder-open',
      reads_only=True, capability='Read an engineering project',
      parameters=_obj(('project_id',), project_id=_i('Project id.')))
def get_project(ctx, project_id):
    """One project, including the work-item summary a status question needs."""
    project = _find_project(project_id)
    if project is None:
        return _no_project(project_id)

    counts = _status_counts(project.work_items.all())
    sprints = list(project.sprints.all()[:10])
    overdue = [item for item in project.work_items.all() if item.is_overdue]

    lines = [
        f'{project.key} (id {project.pk}) {project.name}',
        f'Status: {project.get_status_display()}. Owner: {project.owner or "unassigned"}.',
        f'Progress: {project.progress_percent}% -- {project.done_items} done, '
        f'{project.open_items} open.',
        f'Work items by status: {_counts_sentence(counts)}.',
    ]
    if project.target_date:
        lines.append(f'Target date: {project.target_date:%d %b %Y}.')
    if project.repository or project.jira_project_key:
        lines.append(f'External: repository {project.repository or "none"}, '
                     f'Jira project {project.jira_project_key or "none"}.')
    if project.tech_stack:
        lines.append(f'Stack: {", ".join(project.tech_stack)}.')
    if overdue:
        lines.append(f'{len(overdue)} item(s) are overdue -- see eng.find_overdue_items.')
    if sprints:
        lines.append('Sprints: ' + '; '.join(
            f'{sprint.name} (id {sprint.pk}, {sprint.get_status_display()}, '
            f'{sprint.committed_points}/{sprint.capacity_points or "unstated"} points)'
            for sprint in sprints))
    else:
        lines.append('No sprints yet -- eng.create_sprint starts one.')
    if project.description:
        lines.append(f'Description: {project.description[:400]}')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'project': {'id': project.pk, 'key': project.key, 'name': project.name,
                          'status': project.status, 'owner': project.owner,
                          'progress_percent': project.progress_percent,
                          'repository': project.repository,
                          'jira_project_key': project.jira_project_key,
                          'tech_stack': project.tech_stack},
              'status_counts': counts, 'overdue_count': len(overdue),
              'sprints': [{'id': s.pk, 'name': s.name, 'status': s.status,
                           'committed_points': s.committed_points,
                           'capacity_points': s.capacity_points} for s in sprints]},
        subject_label='marketing.project', subject_id=project.pk)


# ===========================================================================
# GROUP: Task Breakdown
# ===========================================================================

@tool(name='eng.break_down_requirement', title='Break a requirement into work items',
      description=('Split a written requirement into work items with titles, '
                   'descriptions, acceptance criteria and a build-order hint that puts '
                   'the data model before the API before the interface before the '
                   'tests. Returns the tree it created.'),
      group='Task Breakdown', agent_types=('engineering_manager',), icon='fa-sitemap',
      capability='Break a requirement into work items',
      parameters=_obj(
          ('project_id', 'requirement'),
          project_id=_i('Project the items belong to.'),
          requirement=_s('The requirement text, in whatever form it was written.'),
          item_type=_enum('Type for the items created.',
                          ('epic', 'story', 'task', 'subtask', 'bug', 'spike', 'chore')),
          max_items=_i('Most items to create. Default 8, capped at 20.'),
          parent_id=_i('Existing work item to hang the new items under.')))
def break_down_requirement(ctx, project_id, requirement, item_type='story',
                           max_items=8, parent_id=None):
    """Turn a paragraph into work items a developer could start on.

    The split is real text handling, described in the breakdown engine above:
    bullet lines, then sentences, then conjunctions where both halves are
    substantial. Each item gets acceptance criteria derived from its own
    vocabulary and a sequence hint from the layer it talks about, so the list
    comes back in an order that can actually be built.
    """
    project = _find_project(project_id)
    if project is None:
        return _no_project(project_id)

    if not str(requirement or '').strip():
        message = ('There is no requirement text to break down. Paste the requirement, '
                   'or ask for it in one short question.')
        return ToolResult(ok=False, error=message, text=message)

    parent = None
    if parent_id:
        parent = _find_item(parent_id)
        if parent is None:
            return _no_item(parent_id)

    candidates = _break_text(requirement, max_items)
    if not candidates:
        message = ('That requirement did not contain anything separable -- it reads as '
                   'a single sentence. Create one work item with eng.create_work_item, '
                   'or supply the requirement with its points on separate lines.')
        return ToolResult(ok=False, error='nothing to split', text=message)

    kind = item_type if item_type in dict(WorkItem.TYPE_CHOICES) else 'story'
    agent = _agent_of(ctx)
    created = []
    for candidate in candidates:
        verdict = _estimate(f"{candidate['title']} {candidate['description']}",
                            criteria_count=len(candidate['criteria']))
        created.append(WorkItem.objects.create(
            project=project, parent=parent, item_type=kind, status='backlog',
            title=candidate['title'][:250],
            description=candidate['description'],
            acceptance_criteria=candidate['criteria'],
            sequence_hint=candidate['sequence_hint'],
            complexity=verdict['complexity'],
            estimate_points=verdict['points'],
            estimate_hours=verdict['hours'],
            labels=[candidate['layer']] if candidate['layer'] != 'general' else [],
            created_by_agent=agent))

    # Chain the layers: an item in a later layer depends on the last item of
    # the previous layer. This is a suggestion recorded as data, and
    # eng.unlink_dependency removes any of it a person disagrees with.
    by_layer, links = {}, []
    for item, candidate in zip(created, candidates):
        by_layer.setdefault(candidate['sequence_hint'], []).append(item)
    layers = sorted(by_layer)
    for earlier, later in zip(layers, layers[1:]):
        anchor = by_layer[earlier][-1]
        for follower in by_layer[later]:
            follower.depends_on.add(anchor)
            links.append(f'{follower.reference} depends on {anchor.reference}')

    references = ', '.join(item.reference for item in created)
    lines = [f'Broke the requirement into {len(created)} work item(s) on '
             f'{project.key}: {references}.']
    lines.append('Build order:')
    lines.extend(f'  {index}. {item.reference} {item.title} '
                 f'[{item.complexity}, {item.estimate_points} pt, '
                 f'{len(item.acceptance_criteria)} criteria]'
                 for index, item in enumerate(created, start=1))
    if links:
        lines.append('Recorded dependencies: ' + '; '.join(links[:8]) + '.')
    if parent is not None:
        lines.append(f'All of them sit under {parent.reference}.')
    lines.append('Estimates are heuristic -- call eng.estimate_complexity on any item '
                 'to see what its number assumes.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'project_id': project.pk, 'created': [_item_row(i) for i in created],
              'dependencies': links, 'count': len(created)},
        subject_label='marketing.project', subject_id=project.pk)


@tool(name='eng.break_down_task', title='Break a work item into subtasks',
      description=('Split one work item one level down into child items, using the '
                   'same text handling as the requirement breakdown.'),
      group='Task Breakdown', agent_types=('engineering_manager',), icon='fa-code-branch',
      capability='Break a work item into subtasks',
      parameters=_obj(
          ('work_item_id',),
          work_item_id=_i('Work item to split.'),
          max_subtasks=_i('Most children to create. Default 6.')))
def break_down_task(ctx, work_item_id, max_subtasks=6):
    """One level down: children of a single item.

    The source text is the item's description plus its acceptance criteria,
    because the criteria are usually where the separable pieces of work were
    written down.
    """
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)

    source_parts = [item.description or item.title]
    source_parts.extend(str(line) for line in (item.acceptance_criteria or []))
    candidates = _break_text('\n'.join(source_parts), max_subtasks)

    if len(candidates) < 2:
        message = (f'{item.reference} does not contain separable pieces -- it is already '
                   f'one outcome. Leave it as it is, or add detail to its description '
                   f'first with eng.update_work_item.')
        return ToolResult(ok=True, text=message,
                          data={'work_item_id': item.pk, 'created': [], 'count': 0})

    child_type = 'subtask' if item.item_type in ('story', 'task', 'subtask') else 'story'
    agent = _agent_of(ctx)
    created = []
    for candidate in candidates:
        verdict = _estimate(f"{candidate['title']} {candidate['description']}",
                            criteria_count=len(candidate['criteria']))
        created.append(WorkItem.objects.create(
            project=item.project, parent=item, sprint=item.sprint,
            item_type=child_type, status='backlog', priority=item.priority,
            title=candidate['title'][:250],
            description=candidate['description'],
            acceptance_criteria=candidate['criteria'],
            sequence_hint=candidate['sequence_hint'],
            complexity=verdict['complexity'],
            estimate_points=verdict['points'],
            estimate_hours=verdict['hours'],
            created_by_agent=agent))

    total_points = sum(child.estimate_points or 0 for child in created)
    lines = [f'Split {item.reference} into {len(created)} subtask(s): '
             + ', '.join(child.reference for child in created) + '.']
    lines.extend(f'  {child.reference} {child.title} '
                 f'[{child.complexity}, {child.estimate_points} pt]'
                 for child in created)
    lines.append(f'The children total {total_points} point(s) against '
                 f'{item.estimate_points or 0} on the parent. If those disagree, the '
                 f'parent estimate is the one to revisit.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'parent': _item_row(item), 'created': [_item_row(c) for c in created],
              'child_points': total_points, 'count': len(created)},
        subject_label='marketing.workitem', subject_id=item.pk)


@tool(name='eng.create_work_item', title='Create a work item',
      description=('Record one work item: title, description, type, priority, '
                   'acceptance criteria, estimate, due date, sprint and parent.'),
      group='Task Breakdown', agent_types=('engineering_manager',), icon='fa-plus',
      capability='Create a work item',
      parameters=_obj(
          ('project_id', 'title'),
          project_id=_i('Project the item belongs to.'),
          title=_s('One outcome, stated plainly.'),
          description=_s('What the work involves.'),
          item_type=_enum('Item type.',
                          ('epic', 'story', 'task', 'subtask', 'bug', 'spike', 'chore')),
          priority=_enum('Priority.', ('lowest', 'low', 'medium', 'high', 'critical')),
          parent_id=_i('Parent work item, for a child of an epic or story.'),
          sprint_id=_i('Sprint to put it in immediately.'),
          acceptance_criteria=_a('Checkable statements that decide when it is done.'),
          estimate_points=_i('Story points, if already agreed.'),
          due_date=_s('Due date, e.g. 2026-02-14.'),
          labels=_a('Labels.')))
def create_work_item(ctx, project_id, title, description='', item_type='task',
                     priority='medium', parent_id=None, sprint_id=None,
                     acceptance_criteria=None, estimate_points=None,
                     due_date=None, labels=None):
    """One work item, estimated and sequenced if nobody said otherwise."""
    project = _find_project(project_id)
    if project is None:
        return _no_project(project_id)
    if not str(title or '').strip():
        message = 'A work item needs a title naming a single outcome.'
        return ToolResult(ok=False, error=message, text=message)

    parent = None
    if parent_id:
        parent = _find_item(parent_id)
        if parent is None:
            return _no_item(parent_id)

    sprint = None
    if sprint_id:
        sprint = _find_sprint(sprint_id)
        if sprint is None:
            return _no_sprint(sprint_id)

    criteria = _as_list(acceptance_criteria)
    body = f'{title} {description}'
    verdict = _estimate(body, criteria_count=len(criteria))
    hint, layer = _layer_for(body)
    points = _as_int(estimate_points)

    item = WorkItem.objects.create(
        project=project, parent=parent, sprint=sprint,
        title=str(title).strip()[:250],
        description=str(description or '').strip(),
        acceptance_criteria=criteria,
        item_type=item_type if item_type in dict(WorkItem.TYPE_CHOICES) else 'task',
        priority=priority if priority in dict(WorkItem.PRIORITY_CHOICES) else 'medium',
        complexity=verdict['complexity'],
        estimate_points=points if points is not None else verdict['points'],
        estimate_hours=verdict['hours'],
        due_date=_as_date(due_date),
        labels=_as_list(labels),
        sequence_hint=hint,
        status='backlog',
        created_by_agent=_agent_of(ctx))

    lines = [f'Created {item.reference} (id {item.pk}) "{item.title}" '
             f'as a {item.get_item_type_display().lower()} on {project.key}, '
             f'priority {item.priority}.']
    if points is None:
        lines.append(f'Estimated {verdict["complexity"]} at {verdict["points"]} point(s) '
                     f'because of: {"; ".join(verdict["drivers"][:2])}.')
    if criteria:
        lines.append(f'{len(criteria)} acceptance criteria recorded.')
    else:
        lines.append('No acceptance criteria yet -- an item without them is hard to '
                     'call done. eng.update_work_item can add them.')
    if item.due_date:
        lines.append(f'Due {item.due_date:%d %b %Y}.')
    if sprint is not None:
        lines.append(f'Placed in sprint {sprint.name}.')
    lines.append(f'Build-order layer: {layer}.')

    return ToolResult(ok=True, text=' '.join(lines), data={'work_item': _item_row(item)},
                      subject_label='marketing.workitem', subject_id=item.pk)


@tool(name='eng.update_work_item', title='Update a work item',
      description=('Change a work item: title, description, status, priority, estimate, '
                   'due date, sprint, labels or blocked reason. Setting the status to '
                   'done stamps the completion time.'),
      group='Task Breakdown', agent_types=('engineering_manager',), icon='fa-pen',
      capability='Update a work item',
      parameters=_obj(
          ('work_item_id',),
          work_item_id=_i('Work item to change.'),
          title=_s('New title.'),
          description=_s('New description.'),
          status=_enum('New status.', ('backlog', 'todo', 'in_progress', 'in_review',
                                       'blocked', 'done', 'cancelled')),
          priority=_enum('New priority.',
                         ('lowest', 'low', 'medium', 'high', 'critical')),
          estimate_points=_i('New point estimate.'),
          due_date=_s('New due date.'),
          sprint_id=_i('Sprint to move it to. 0 removes it from its sprint.'),
          labels=_a('Replacement labels.'),
          acceptance_criteria=_a('Replacement acceptance criteria.'),
          blocked_reason=_s('What it is waiting on, when the status is blocked.')))
def update_work_item(ctx, work_item_id, title='', description='', status='',
                     priority='', estimate_points=None, due_date=None,
                     sprint_id=None, labels=None, acceptance_criteria=None,
                     blocked_reason=''):
    """Change one item and report exactly what changed.

    Listing the changes back matters more than it looks: the employee's next
    sentence is written from this text, and 'updated the item' tells a reader
    nothing about what is now true.
    """
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)

    changes, fields = [], []

    if str(title or '').strip():
        changes.append(f'title to "{str(title).strip()[:80]}"')
        item.title = str(title).strip()[:250]
        fields.append('title')

    if str(description or '').strip():
        changes.append('description')
        item.description = str(description).strip()
        fields.append('description')

    if status:
        if status not in dict(WorkItem.STATUS_CHOICES):
            message = (f'"{status}" is not a work item status. Use one of: '
                       f'{", ".join(dict(WorkItem.STATUS_CHOICES))}.')
            return ToolResult(ok=False, error=message, text=message)
        if status != item.status:
            changes.append(f'status from {item.status} to {status}')
            item.status = status
            fields.append('status')
            if status == 'done':
                item.completed_at = timezone.now()
                fields.append('completed_at')
            elif item.completed_at:
                item.completed_at = None
                fields.append('completed_at')

    if priority:
        if priority not in dict(WorkItem.PRIORITY_CHOICES):
            message = (f'"{priority}" is not a priority. Use lowest, low, medium, high '
                       f'or critical.')
            return ToolResult(ok=False, error=message, text=message)
        changes.append(f'priority from {item.priority} to {priority}')
        item.priority = priority
        fields.append('priority')

    points = _as_int(estimate_points)
    if points is not None:
        changes.append(f'estimate to {points} point(s)')
        item.estimate_points = max(0, points)
        fields.append('estimate_points')

    if due_date not in (None, ''):
        parsed = _as_date(due_date)
        if parsed is None:
            message = (f'"{due_date}" is not a date I can read. Use a form like '
                       f'2026-03-31 or 31/03/2026.')
            return ToolResult(ok=False, error=message, text=message)
        changes.append(f'due date to {parsed:%d %b %Y}')
        item.due_date = parsed
        fields.append('due_date')

    if sprint_id is not None:
        number = _as_int(sprint_id) or 0
        if number:
            sprint = _find_sprint(number)
            if sprint is None:
                return _no_sprint(sprint_id)
            changes.append(f'sprint to {sprint.name}')
            item.sprint = sprint
        else:
            changes.append('removed it from its sprint')
            item.sprint = None
        fields.append('sprint')

    if labels is not None:
        item.labels = _as_list(labels)
        changes.append(f'labels to {", ".join(item.labels) or "none"}')
        fields.append('labels')

    if acceptance_criteria is not None:
        item.acceptance_criteria = _as_list(acceptance_criteria)
        changes.append(f'{len(item.acceptance_criteria)} acceptance criteria')
        fields.append('acceptance_criteria')

    if str(blocked_reason or '').strip():
        item.blocked_reason = str(blocked_reason).strip()
        changes.append('blocked reason')
        fields.append('blocked_reason')
        if item.status not in ('blocked', 'done', 'cancelled'):
            item.status = 'blocked'
            fields.append('status')
            changes.append('status to blocked, since a reason was given')

    if not changes:
        message = (f'Nothing was changed on {item.reference}: no new values were passed. '
                   f'Say which field to change.')
        return ToolResult(ok=False, error='no changes', text=message)

    item.save()
    return ToolResult(
        ok=True,
        text=f'Updated {item.reference} "{item.title}": ' + '; '.join(changes) + '.',
        data={'work_item': _item_row(item), 'changed': fields},
        subject_label='marketing.workitem', subject_id=item.pk)


@tool(name='eng.assign_work_item', title='Assign a work item',
      description=('Record who holds a work item, by employee id or by name. This is '
                   'the internal record only -- telling the person is '
                   'eng.notify_developer, which needs approval.'),
      group='Task Breakdown', agent_types=('engineering_manager',), icon='fa-user-check',
      capability='Assign a work item',
      parameters=_obj(
          ('work_item_id',),
          work_item_id=_i('Work item to assign.'),
          employee_id=_i('Employee record to assign it to.'),
          assignee_name=_s('Name, when the person has no employee record.')))
def assign_work_item(ctx, work_item_id, employee_id=None, assignee_name=''):
    """Assign an item internally.

    Deliberately separate from notifying anybody. Assignment is a record and
    happens at once; a message to a person leaves the platform and therefore
    goes through approval. Collapsing the two would mean a plan could not be
    drafted without messaging people about it.
    """
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)

    if employee_id:
        employee = _find_employee(employee_id)
        if employee is None:
            message = (f'There is no employee with id {employee_id}. Call '
                       f'hr.list_employees to find the right id, or pass '
                       f'assignee_name instead.')
            return ToolResult(ok=False, error=message, text=message)
        item.assignee = employee
        item.assignee_name = ''
        label = employee.full_name
    elif str(assignee_name or '').strip():
        item.assignee = None
        item.assignee_name = str(assignee_name).strip()[:140]
        label = item.assignee_name
    else:
        item.assignee = None
        item.assignee_name = ''
        label = 'nobody'

    if item.status == 'backlog' and label != 'nobody':
        item.status = 'todo'
    item.save()

    load = _open_items().filter(assignee=item.assignee).count() if item.assignee_id else \
        _open_items().filter(assignee_name=item.assignee_name).count()
    lines = [f'{item.reference} "{item.title}" is now assigned to {label}.']
    if label != 'nobody':
        lines.append(f'{label} now holds {load} open item(s).')
        lines.append('They have not been told -- eng.notify_developer prepares that '
                     'message for approval.')

    return ToolResult(ok=True, text=' '.join(lines),
                      data={'work_item': _item_row(item), 'assignee': label,
                            'open_items_held': load},
                      subject_label='marketing.workitem', subject_id=item.pk)


@tool(name='eng.set_priority', title='Set a work item priority',
      description='Set one work item to lowest, low, medium, high or critical.',
      group='Task Breakdown', agent_types=('engineering_manager',), icon='fa-flag',
      capability='Set a work item priority',
      parameters=_obj(('work_item_id', 'priority'),
                      work_item_id=_i('Work item.'),
                      priority=_enum('New priority.',
                                     ('lowest', 'low', 'medium', 'high', 'critical'))))
def set_priority(ctx, work_item_id, priority):
    """Change a priority, and say what else is at that level."""
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)
    if priority not in dict(WorkItem.PRIORITY_CHOICES):
        message = ('Priority must be lowest, low, medium, high or critical. '
                   f'"{priority}" is not one of those.')
        return ToolResult(ok=False, error=message, text=message)

    was = item.priority
    item.priority = priority
    item.save(update_fields=['priority', 'updated_at'])

    peers = _open_items(project=item.project).filter(priority=priority).count()
    text = (f'{item.reference} "{item.title}" moved from {was} to {priority} priority. '
            f'{peers} open item(s) on {item.project.key} now sit at {priority}.')
    if priority == 'critical' and peers > 3:
        text += (' More than three critical items at once usually means the label has '
                 'stopped meaning anything -- worth re-ranking with eng.suggest_priority.')

    return ToolResult(ok=True, text=text,
                      data={'work_item': _item_row(item), 'was': was,
                            'peers_at_priority': peers},
                      subject_label='marketing.workitem', subject_id=item.pk)


@tool(name='eng.set_deadline', title='Set a work item deadline',
      description='Set the due date on one work item and say how long that leaves.',
      group='Task Breakdown', agent_types=('engineering_manager',),
      icon='fa-calendar-day', capability='Set a work item deadline',
      parameters=_obj(('work_item_id', 'due_date'),
                      work_item_id=_i('Work item.'),
                      due_date=_s('Due date, e.g. 2026-03-31 or 31/03/2026.')))
def set_deadline(ctx, work_item_id, due_date):
    """Set a due date, and compare it against the estimate rather than accept it blindly."""
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)

    parsed = _as_date(due_date)
    if parsed is None:
        message = (f'"{due_date}" is not a date I can read. Use 2026-03-31, '
                   f'31/03/2026 or 31 March 2026.')
        return ToolResult(ok=False, error=message, text=message)

    was = item.due_date
    item.due_date = parsed
    item.save(update_fields=['due_date', 'updated_at'])

    days = (parsed - timezone.localdate()).days
    lines = [f'{item.reference} "{item.title}" is now due {parsed:%d %b %Y}'
             + (f' (was {was:%d %b %Y})' if was else '') + '.']
    if days < 0:
        lines.append(f'That date is {abs(days)} day(s) in the past, so the item is '
                     f'overdue the moment it is set.')
    else:
        lines.append(f'{days} day(s) from today.')
        hours = float(item.estimate_hours or 0)
        if hours and days * 6 < hours:
            lines.append(f'The estimate is {hours} hours, which does not fit '
                         f'{days} working day(s) at any reasonable load. Either the '
                         f'estimate or the date needs to move.')
    if item.project.target_date and parsed > item.project.target_date:
        lines.append(f'This is after the project target date '
                     f'({item.project.target_date:%d %b %Y}).')

    return ToolResult(ok=True, text=' '.join(lines),
                      data={'work_item': _item_row(item),
                            'days_from_today': days},
                      subject_label='marketing.workitem', subject_id=item.pk)


@tool(name='eng.list_work_items', title='List work items',
      description=('List work items, filtered by project, status, sprint or assignee. '
                   'The way to find an id before acting on an item.'),
      group='Tracking', agent_types=('engineering_manager',), icon='fa-list-check',
      reads_only=True, capability='List work items',
      parameters=_obj(
          (),
          project_id=_i('Restrict to one project.'),
          status=_enum('Restrict to one status.',
                       ('backlog', 'todo', 'in_progress', 'in_review', 'blocked',
                        'done', 'cancelled')),
          sprint_id=_i('Restrict to one sprint.'),
          assignee_id=_i('Restrict to one employee.'),
          limit=_i('Most items to return. Default 30.')))
def list_work_items(ctx, project_id=None, status='', sprint_id=None,
                    assignee_id=None, limit=30):
    """Work items matching the filters, with the references a person can quote."""
    query = WorkItem.objects.all()
    described = []

    if project_id:
        project = _find_project(project_id)
        if project is None:
            return _no_project(project_id)
        query = query.filter(project=project)
        described.append(f'project {project.key}')
    if status:
        query = query.filter(status=status)
        described.append(f'status {status}')
    if sprint_id:
        sprint = _find_sprint(sprint_id)
        if sprint is None:
            return _no_sprint(sprint_id)
        query = query.filter(sprint=sprint)
        described.append(f'sprint {sprint.name}')
    if assignee_id:
        employee = _find_employee(assignee_id)
        if employee is None:
            message = (f'There is no employee with id {assignee_id}. '
                       f'Call hr.list_employees to find the right id.')
            return ToolResult(ok=False, error=message, text=message)
        query = query.filter(assignee=employee)
        described.append(f'assignee {employee.full_name}')

    rows = list(query.select_related('project', 'sprint')[
        :max(1, min(_as_int(limit) or 30, 100))])
    where = ' with ' + ', '.join(described) if described else ''

    if not rows:
        return ToolResult(ok=True, text=f'No work items{where}.',
                          data={'work_items': [], 'count': 0})

    lines = [f'{item.reference} [{item.status}/{item.priority}] {item.title} -- '
             f'{item.assignee_label}'
             + (f', due {item.due_date:%d %b}' if item.due_date else '')
             + (f', {item.days_overdue} day(s) overdue' if item.is_overdue else '')
             for item in rows]

    return ToolResult(ok=True,
                      text=f'{len(rows)} work item(s){where}:\n' + '\n'.join(lines),
                      data={'work_items': [_item_row(item) for item in rows],
                            'count': len(rows)})


@tool(name='eng.get_work_item', title='Get a work item',
      description=('One work item in full: its criteria, its children, what it depends '
                   'on, what it blocks, and its comments.'),
      group='Tracking', agent_types=('engineering_manager',), icon='fa-file-lines',
      reads_only=True, capability='Read a work item',
      parameters=_obj(('work_item_id',), work_item_id=_i('Work item id.')))
def get_work_item(ctx, work_item_id):
    """Everything about one item, in the order somebody asking about it needs it."""
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)

    children = list(item.children.all())
    dependencies = list(item.depends_on.all())
    blocking = list(item.blocks.all())
    comments = list(item.comments.all()[:10])

    lines = [
        f'{item.reference} (id {item.pk}) {item.title}',
        f'{item.get_item_type_display()} on {item.project.key}, '
        f'status {item.get_status_display()}, priority {item.priority}, '
        f'complexity {item.complexity}, {item.estimate_points or 0} point(s).',
        f'Assignee: {item.assignee_label}.'
        + (f' Sprint: {item.sprint.name}.' if item.sprint_id else ' Not in a sprint.'),
    ]
    if item.due_date:
        lines.append(f'Due {item.due_date:%d %b %Y}'
                     + (f' -- {item.days_overdue} day(s) overdue.' if item.is_overdue
                        else '.'))
    if item.description:
        lines.append(f'Description: {item.description[:600]}')
    if item.acceptance_criteria:
        lines.append('Acceptance criteria:')
        lines.extend(f'  - {line}' for line in item.acceptance_criteria)
    else:
        lines.append('No acceptance criteria recorded.')
    if item.blocked_reason:
        lines.append(f'Blocked on: {item.blocked_reason}')
    if dependencies:
        lines.append('Depends on: ' + ', '.join(
            f'{dep.reference} ({dep.status})' for dep in dependencies))
    if blocking:
        lines.append('Blocks: ' + ', '.join(
            f'{other.reference} ({other.status})' for other in blocking))
    if children:
        lines.append(f'{len(children)} child item(s):')
        lines.extend(f'  {child.reference} [{child.status}] {child.title}'
                     for child in children)
    if item.external_reference:
        lines.append(f'Filed externally as {item.external_reference} '
                     f'{item.external_url}'.strip())
    if item.labels:
        lines.append(f'Labels: {", ".join(item.labels)}.')
    if comments:
        lines.append('Recent comments:')
        lines.extend(f'  {c.created_at:%d %b %H:%M} {c.author_label}: {c.body[:200]}'
                     for c in comments)

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'work_item': _item_row(item),
              'acceptance_criteria': item.acceptance_criteria,
              'children': [_item_row(c) for c in children],
              'depends_on': [_item_row(d) for d in dependencies],
              'blocks': [_item_row(b) for b in blocking],
              'comments': [{'author': c.author_label, 'body': c.body,
                            'at': c.created_at.isoformat()} for c in comments]},
        subject_label='marketing.workitem', subject_id=item.pk)


@tool(name='eng.comment_on_work_item', title='Comment on a work item',
      description=('Add a note to a work item, attributed to this employee. Internal '
                   'only -- it does not notify anybody.'),
      group='Tracking', agent_types=('engineering_manager',), icon='fa-comment',
      capability='Comment on a work item',
      parameters=_obj(('work_item_id', 'body'),
                      work_item_id=_i('Work item to comment on.'),
                      body=_s('The note.')))
def comment_on_work_item(ctx, work_item_id, body):
    """Leave a note on an item. Attribution is to the employee, not to a person."""
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)
    if not str(body or '').strip():
        message = 'A comment needs some text.'
        return ToolResult(ok=False, error=message, text=message)

    comment = WorkItemComment.objects.create(
        work_item=item, body=str(body).strip(),
        agent=_agent_of(ctx), author=_user_of(ctx))
    total = item.comments.count()

    return ToolResult(
        ok=True,
        text=(f'Added comment #{comment.pk} to {item.reference} "{item.title}" '
              f'({total} comment(s) in total). It is an internal note -- nobody has '
              f'been notified.'),
        data={'comment_id': comment.pk, 'work_item_id': item.pk, 'comments': total},
        subject_label='marketing.workitem', subject_id=item.pk)


# ===========================================================================
# GROUP: Planning & Estimation
# ===========================================================================

@tool(name='eng.estimate_complexity', title='Estimate complexity',
      description=('Size a work item or a written description: complexity, points, '
                   'hours, what drove the number and what it assumes. Saves the '
                   'verdict onto the item when one is given.'),
      group='Planning & Estimation', agent_types=('engineering_manager',),
      icon='fa-scale-balanced', capability='Estimate complexity',
      parameters=_obj(
          (),
          work_item_id=_i('Work item to size and update.'),
          description=_s('Text to size, when there is no item yet.')))
def estimate_complexity(ctx, work_item_id=None, description=''):
    """Size work from its own text, and state the assumptions.

    The assumptions are the point. A number without them is repeated as a
    commitment by whoever reads it next, and the estimate then fails for
    reasons nobody wrote down.
    """
    item = None
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)

    if item is None and not str(description or '').strip():
        message = ('Nothing to estimate. Pass a work_item_id, or a description of the '
                   'work.')
        return ToolResult(ok=False, error=message, text=message)

    if item is not None:
        body = ' '.join([item.title, item.description or '',
                         ' '.join(str(c) for c in (item.acceptance_criteria or [])),
                         str(description or '')])
        verdict = _estimate(body,
                            criteria_count=len(item.acceptance_criteria or []),
                            dependency_count=item.depends_on.count(),
                            child_count=item.children.count())
        item.complexity = verdict['complexity']
        item.estimate_points = verdict['points']
        item.estimate_hours = verdict['hours']
        if verdict['needs_spike'] and item.item_type not in ('spike', 'epic'):
            item.item_type = 'spike'
        item.save(update_fields=['complexity', 'estimate_points', 'estimate_hours',
                                 'item_type', 'updated_at'])
        subject = f'{item.reference} "{item.title}"'
    else:
        verdict = _estimate(description)
        subject = 'that description'

    lines = [f'Estimated {subject}: {verdict["complexity"]}, {verdict["points"]} '
             f'point(s), roughly {verdict["hours"]} hours.']
    lines.append('What drove it: ' + '; '.join(verdict['drivers']) + '.')
    lines.append('What it assumes:')
    lines.extend(f'  - {line}' for line in verdict['assumptions'])
    if verdict['needs_spike']:
        lines.append('Part of this is genuinely not understood yet. It is recorded as a '
                     'spike: the work is to find out, and the real estimate comes after.')
    if item is not None:
        lines.append(f'Saved onto {item.reference}.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'estimate': verdict,
              'work_item': _item_row(item) if item is not None else None},
        subject_label='marketing.workitem' if item is not None else '',
        subject_id=item.pk if item is not None else 0)


@tool(name='eng.suggest_priority', title='Suggest a backlog order by priority',
      description=('Rank the open backlog by what it blocks, how overdue it is, how '
                   'much depends on it and its stated priority, explaining each place '
                   'in the ranking.'),
      group='Planning & Estimation', agent_types=('engineering_manager',),
      icon='fa-ranking-star', reads_only=True, capability='Suggest backlog priority',
      parameters=_obj(('project_id',),
                      project_id=_i('Project whose backlog to rank.'),
                      limit=_i('How many to return. Default 10.')))
def suggest_priority(ctx, project_id, limit=10):
    """Rank the backlog, and show the arithmetic.

    A ranking a person cannot argue with is a ranking they cannot use, so
    every row carries the reasons that put it where it is.
    """
    project = _find_project(project_id)
    if project is None:
        return _no_project(project_id)

    items = list(_open_items(project=project).select_related('project'))
    if not items:
        return ToolResult(ok=True,
                          text=f'{project.key} has no open work items to rank.',
                          data={'ranking': [], 'count': 0})

    scored = []
    for item in items:
        reach = _blocking_reach(item)
        overdue = min(item.days_overdue, 30)
        weight = item.priority_weight
        unmet = len(item.unmet_dependencies)
        score = weight * 2 + reach * 3 + overdue * 0.4 - unmet * 1.5
        reasons = [f'stated priority {item.priority} ({weight * 2:.0f} pts)']
        if reach:
            reasons.append(f'holds up {reach} other item(s) ({reach * 3} pts)')
        if overdue:
            reasons.append(f'{item.days_overdue} day(s) overdue ({overdue * 0.4:.1f} pts)')
        if unmet:
            reasons.append(f'waiting on {unmet} unfinished dependency(ies) '
                           f'(-{unmet * 1.5:.1f} pts, it cannot start yet)')
        if item.status == 'blocked':
            score -= 2
            reasons.append('marked blocked (-2 pts)')
        scored.append((score, item, reasons, reach))

    scored.sort(key=lambda row: (-row[0], row[1].sequence_hint, row[1].pk))
    top = scored[:max(1, min(_as_int(limit) or 10, 40))]

    lines = [f'Suggested order for the {len(items)} open item(s) on {project.key} '
             f'-- top {len(top)}:']
    payload = []
    for position, (score, item, reasons, reach) in enumerate(top, start=1):
        lines.append(f'{position}. {item.reference} {item.title} (score {score:.1f})')
        lines.append(f'   Because: {"; ".join(reasons)}.')
        payload.append({'rank': position, 'score': round(score, 1),
                        'reasons': reasons, 'blocking_reach': reach,
                        **_item_row(item)})

    blocked_first = [row for row in top if row[1].unmet_dependencies]
    if blocked_first:
        lines.append(f'{len(blocked_first)} of these cannot start yet -- they are ranked '
                     f'high on importance but they are waiting on something. Clear the '
                     f'dependency first or the ranking is academic.')
    lines.append('This is a suggestion computed from the recorded data, not a decision. '
                 'eng.set_priority changes the stored priority when you agree with it.')

    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'ranking': payload, 'open_count': len(items)},
                      subject_label='marketing.project', subject_id=project.pk)


@tool(name='eng.suggest_sequence', title='Suggest a build order',
      description=('A build order that respects the recorded dependencies, produced by '
                   'a topological sort. Reports a dependency cycle honestly rather than '
                   'ordering around it.'),
      group='Planning & Estimation', agent_types=('engineering_manager',),
      icon='fa-arrow-down-1-9', reads_only=True, capability='Suggest a build order',
      parameters=_obj(('project_id',),
                      project_id=_i('Project to sequence.'),
                      parent_id=_i('Sequence only the children of this item.')))
def suggest_sequence(ctx, project_id, parent_id=None):
    """A buildable order, or an honest account of why there is not one.

    The cycle case matters more than the happy case. Any sort can produce an
    order; only one that refuses to invent an order through a cycle tells the
    reader that their dependency graph is wrong.
    """
    project = _find_project(project_id)
    if project is None:
        return _no_project(project_id)

    query = _open_items(project=project)
    scope = f'the open items on {project.key}'
    if parent_id:
        parent = _find_item(parent_id)
        if parent is None:
            return _no_item(parent_id)
        query = query.filter(parent=parent)
        scope = f'the open children of {parent.reference}'

    items = list(query.prefetch_related('depends_on'))
    if not items:
        return ToolResult(ok=True, text=f'There are no open items in {scope}.',
                          data={'sequence': [], 'cycle': []})

    ordered, unresolved = _order_by_dependency(items)

    lines = [f'Build order for {scope} ({len(ordered)} of {len(items)} placed):']
    payload = []
    for position, item in enumerate(ordered, start=1):
        waiting = [dep.reference for dep in item.depends_on.all()
                   if dep in ordered or dep.is_finished]
        lines.append(f'{position}. {item.reference} {item.title} '
                     f'[{item.priority}, {item.estimate_points or 0} pt]'
                     + (f' -- after {", ".join(waiting)}' if waiting else ''))
        payload.append({'position': position, 'after': waiting, **_item_row(item)})

    if unresolved:
        names = ', '.join(item.reference for item in unresolved)
        lines.append('')
        lines.append(f'These {len(unresolved)} item(s) could not be placed: {names}. '
                     f'They form a dependency cycle -- each is waiting, directly or '
                     f'through others, on something that is waiting on it. No build '
                     f'order exists until that is broken. Use eng.unlink_dependency to '
                     f'remove whichever link is wrong; I will not invent an order '
                     f'around it, because that would hide the problem.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'sequence': payload,
              'cycle': [_item_row(item) for item in unresolved],
              'has_cycle': bool(unresolved)},
        subject_label='marketing.project', subject_id=project.pk)


@tool(name='eng.identify_dependencies', title='Identify likely dependencies',
      description=('Infer dependencies between the open items of a project from their '
                   'titles and descriptions, record them, and report the evidence for '
                   'each so a person can reject the wrong ones.'),
      group='Planning & Estimation', agent_types=('engineering_manager',),
      icon='fa-link', capability='Identify dependencies',
      parameters=_obj(('project_id',), project_id=_i('Project to analyse.')))
def identify_dependencies(ctx, project_id):
    """Infer dependencies, and show the evidence for every one.

    Two signals are used, both textual. The first is layering: an item about
    the interface that shares vocabulary with an item about the data model
    almost certainly needs it first. The second is explicit language --
    'after', 'depends on', 'requires', 'once' -- naming a word from another
    item.

    Each inference is reported with the words it was drawn from, because an
    inferred dependency that nobody can check is just a constraint appearing
    out of nowhere.
    """
    project = _find_project(project_id)
    if project is None:
        return _no_project(project_id)

    items = list(_open_items(project=project).prefetch_related('depends_on'))
    if len(items) < 2:
        return ToolResult(
            ok=True,
            text=(f'{project.key} has fewer than two open items, so there is nothing '
                  f'to infer dependencies between.'),
            data={'inferred': [], 'count': 0})

    profiles = {}
    for item in items:
        text = f'{item.title} {item.description}'
        profiles[item.pk] = {
            'item': item,
            'tokens': _tokens(text),
            'title_tokens': _tokens(item.title),
            'lowered': text.lower(),
            'existing': {dep.pk for dep in item.depends_on.all()},
        }

    inferred, evidence_lines = [], []
    for later in items:
        mine = profiles[later.pk]
        for earlier in items:
            if earlier.pk == later.pk or len(inferred) >= 25:
                continue
            theirs = profiles[earlier.pk]
            if earlier.pk in mine['existing']:
                continue
            if later.pk in theirs['existing']:
                continue  # the reverse link exists; adding this would make a cycle

            shared = sorted(mine['tokens'] & theirs['title_tokens'])
            if not shared:
                continue

            explicit = [word for word in ('depends on', 'after', 'requires', 'once',
                                          'based on', 'needs')
                        if word in mine['lowered']]
            layered = later.sequence_hint > earlier.sequence_hint

            if layered:
                reason = (f'"{shared[0]}" appears in both, and {earlier.reference} is '
                          f'the earlier layer (hint {earlier.sequence_hint}) that '
                          f'{later.reference} (hint {later.sequence_hint}) builds on')
            elif explicit:
                reason = (f'{later.reference} says "{explicit[0]}" and shares the word '
                          f'"{shared[0]}" with {earlier.reference}')
            else:
                continue

            later.depends_on.add(earlier)
            mine['existing'].add(earlier.pk)
            inferred.append({'work_item': later.reference, 'work_item_id': later.pk,
                             'depends_on': earlier.reference,
                             'depends_on_id': earlier.pk, 'evidence': reason})
            evidence_lines.append(f'  {later.reference} depends on {earlier.reference} '
                                  f'-- {reason}')

    if not inferred:
        return ToolResult(
            ok=True,
            text=(f'No dependencies could be inferred between the {len(items)} open '
                  f'items on {project.key}. Their titles do not share vocabulary across '
                  f'layers, and none of them names another. That is a real answer, not a '
                  f'failure -- record any real dependency with eng.link_dependency.'),
            data={'inferred': [], 'count': 0})

    lines = [f'Inferred and recorded {len(inferred)} dependency(ies) on {project.key}, '
             f'each from text evidence:']
    lines.extend(evidence_lines)
    lines.append('')
    lines.append('These are inferences from wording, not facts. Read each one and '
                 'remove any that is wrong with eng.unlink_dependency; then '
                 'eng.suggest_sequence will give a build order that respects the rest.')

    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'inferred': inferred, 'count': len(inferred)},
                      subject_label='marketing.project', subject_id=project.pk)


@tool(name='eng.link_dependency', title='Link a dependency',
      description='Record that one work item cannot start until another is finished.',
      group='Planning & Estimation', agent_types=('engineering_manager',),
      icon='fa-link', capability='Link a dependency',
      parameters=_obj(('work_item_id', 'depends_on_id'),
                      work_item_id=_i('The item that has to wait.'),
                      depends_on_id=_i('The item it is waiting for.')))
def link_dependency(ctx, work_item_id, depends_on_id):
    """Record a dependency, refusing the two links that make no sense."""
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)
    other = _find_item(depends_on_id)
    if other is None:
        return _no_item(depends_on_id)

    if item.pk == other.pk:
        message = 'An item cannot depend on itself.'
        return ToolResult(ok=False, error=message, text=message)

    if other.depends_on.filter(pk=item.pk).exists():
        message = (f'{other.reference} already depends on {item.reference}. Adding the '
                   f'reverse would create a cycle, and neither item could ever start. '
                   f'Decide which way round it really is.')
        return ToolResult(ok=False, error='would create a cycle', text=message)

    already = item.depends_on.filter(pk=other.pk).exists()
    item.depends_on.add(other)

    text = (f'{item.reference} "{item.title}" '
            + ('already depended' if already else 'now depends')
            + f' on {other.reference} "{other.title}" (currently {other.status}).')
    if other.is_finished:
        text += ' That dependency is already finished, so it does not block anything.'
    else:
        text += (f' {item.reference} cannot start until {other.reference} is done.')
    if item.due_date and other.due_date and other.due_date > item.due_date:
        text += (f' Note the dates disagree: {other.reference} is due '
                 f'{other.due_date:%d %b} but {item.reference} is due '
                 f'{item.due_date:%d %b}.')

    return ToolResult(ok=True, text=text,
                      data={'work_item_id': item.pk, 'depends_on_id': other.pk,
                            'already_linked': already},
                      subject_label='marketing.workitem', subject_id=item.pk)


@tool(name='eng.unlink_dependency', title='Remove a dependency',
      description='Remove a recorded dependency between two work items.',
      group='Planning & Estimation', agent_types=('engineering_manager',),
      icon='fa-link-slash', capability='Remove a dependency',
      parameters=_obj(('work_item_id', 'depends_on_id'),
                      work_item_id=_i('The item that was waiting.'),
                      depends_on_id=_i('The item it was waiting for.')))
def unlink_dependency(ctx, work_item_id, depends_on_id):
    """Remove a dependency, including one this employee inferred wrongly."""
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)
    other = _find_item(depends_on_id)
    if other is None:
        return _no_item(depends_on_id)

    if not item.depends_on.filter(pk=other.pk).exists():
        message = (f'{item.reference} does not depend on {other.reference}, so there is '
                   f'nothing to remove. eng.get_work_item shows what it does depend on.')
        return ToolResult(ok=False, error='not linked', text=message)

    item.depends_on.remove(other)
    remaining = item.depends_on.count()
    text = (f'Removed the dependency: {item.reference} no longer waits on '
            f'{other.reference}. It has {remaining} dependency(ies) left.')
    if item.status == 'blocked' and not item.unmet_dependencies:
        text += (' Nothing unfinished blocks it now, so its blocked status may be out '
                 'of date -- eng.update_work_item can move it to todo.')

    return ToolResult(ok=True, text=text,
                      data={'work_item_id': item.pk, 'depends_on_id': other.pk,
                            'remaining_dependencies': remaining},
                      subject_label='marketing.workitem', subject_id=item.pk)


@tool(name='eng.create_sprint', title='Create a sprint',
      description='Start a sprint on a project: a name, a goal, dates and a capacity.',
      group='Planning & Estimation', agent_types=('engineering_manager',),
      icon='fa-person-running', capability='Create a sprint',
      parameters=_obj(
          ('project_id', 'name'),
          project_id=_i('Project the sprint belongs to.'),
          name=_s("Sprint name, e.g. 'Sprint 4'."),
          goal=_s('The one outcome this sprint is for.'),
          start_date=_s('Start date. Defaults to today.'),
          end_date=_s('End date. Defaults to two weeks after the start.'),
          capacity_points=_i('Points the team can finish. 0 means unstated.')))
def create_sprint(ctx, project_id, name, goal='', start_date=None, end_date=None,
                  capacity_points=0):
    """Create a sprint, defaulting to a fortnight when nobody says otherwise."""
    project = _find_project(project_id)
    if project is None:
        return _no_project(project_id)
    if not str(name or '').strip():
        message = 'A sprint needs a name.'
        return ToolResult(ok=False, error=message, text=message)

    starts = _as_date(start_date) or timezone.localdate()
    ends = _as_date(end_date) or (starts + timedelta(days=13))
    if ends < starts:
        message = (f'The end date ({ends:%d %b %Y}) is before the start date '
                   f'({starts:%d %b %Y}). Check which way round they go.')
        return ToolResult(ok=False, error='dates reversed', text=message)

    capacity = max(0, _as_int(capacity_points) or 0)
    sprint = Sprint.objects.create(
        project=project, name=str(name).strip()[:120],
        goal=str(goal or '').strip(), start_date=starts, end_date=ends,
        capacity_points=capacity,
        status='active' if starts <= timezone.localdate() <= ends else 'planned',
        created_by_agent=_agent_of(ctx))

    days = (ends - starts).days + 1
    lines = [f'Created sprint "{sprint.name}" (id {sprint.pk}) on {project.key}, '
             f'{starts:%d %b} to {ends:%d %b %Y} -- {days} days, status '
             f'{sprint.get_status_display().lower()}.']
    if capacity:
        lines.append(f'Capacity {capacity} point(s).')
    else:
        lines.append('No capacity stated, so eng.plan_sprint cannot warn about '
                     'overcommitment. Worth setting.')
    if sprint.goal:
        lines.append(f'Goal: {sprint.goal}')
    else:
        lines.append('No goal recorded. A sprint without one is a list, not a sprint.')
    lines.append(f'Fill it with eng.plan_sprint using sprint_id {sprint.pk}.')

    return ToolResult(ok=True, text=' '.join(lines),
                      data={'sprint_id': sprint.pk, 'name': sprint.name,
                            'start_date': starts.isoformat(),
                            'end_date': ends.isoformat(),
                            'capacity_points': capacity},
                      subject_label='marketing.sprint', subject_id=sprint.pk)


@tool(name='eng.plan_sprint', title='Plan a sprint',
      description=('Fill a sprint from the backlog in priority and dependency order up '
                   'to its capacity, and report exactly what went in, what did not, '
                   'and why.'),
      group='Planning & Estimation', agent_types=('engineering_manager',),
      icon='fa-clipboard-list', capability='Plan a sprint',
      parameters=_obj(
          ('sprint_id',),
          sprint_id=_i('Sprint to fill.'),
          work_item_ids=_ai('Specific items to consider. Defaults to the backlog.'),
          respect_capacity=_b('Stop at the capacity. Default true.')))
def plan_sprint(ctx, sprint_id, work_item_ids=None, respect_capacity=True):
    """Fill a sprint, and account for every item it did not take.

    The exclusions are the useful half of the output. A plan that lists only
    what went in reads as a complete plan, and the conversation that should
    have happened -- about the four items that did not fit -- never happens.
    """
    sprint = _find_sprint(sprint_id)
    if sprint is None:
        return _no_sprint(sprint_id)

    project = sprint.project
    if work_item_ids:
        wanted = [_as_int(value) for value in _as_list(work_item_ids)] \
            if not isinstance(work_item_ids, (list, tuple)) else \
            [_as_int(value) for value in work_item_ids]
        candidates = list(WorkItem.objects.filter(
            pk__in=[pk for pk in wanted if pk], project=project
        ).exclude(status__in=('done', 'cancelled')).prefetch_related('depends_on'))
        missing = [pk for pk in wanted if pk and not any(c.pk == pk for c in candidates)]
    else:
        candidates = list(_open_items(project=project).filter(
            sprint__isnull=True, status__in=('backlog', 'todo')
        ).prefetch_related('depends_on'))
        missing = []

    if not candidates:
        return ToolResult(
            ok=True,
            text=(f'There is nothing available to put in "{sprint.name}": no open, '
                  f'unassigned-to-a-sprint backlog items on {project.key}. '
                  f'eng.break_down_requirement or eng.create_work_item first.'),
            data={'included': [], 'excluded': [], 'sprint_id': sprint.pk})

    ordered, unresolved = _order_by_dependency(candidates)
    capacity = sprint.capacity_points
    honour_capacity = bool(respect_capacity) and capacity > 0
    committed = sprint.committed_points

    included, excluded = [], []
    included_ids = set()

    for item in ordered:
        points = item.estimate_points or 0
        unmet = [dep for dep in item.depends_on.all()
                 if not dep.is_finished and dep.pk not in included_ids
                 and dep.sprint_id != sprint.pk]
        if unmet:
            excluded.append((item, f'waits on {", ".join(d.reference for d in unmet)}, '
                                   f'which is neither finished nor in this sprint'))
            continue
        if honour_capacity and committed + points > capacity:
            excluded.append((item, f'{points} point(s) would take the sprint to '
                                   f'{committed + points} against a capacity of '
                                   f'{capacity}'))
            continue
        item.sprint = sprint
        if item.status == 'backlog':
            item.status = 'todo'
        item.save(update_fields=['sprint', 'status', 'updated_at'])
        included.append(item)
        included_ids.add(item.pk)
        committed += points

    for item in unresolved:
        excluded.append((item, 'is part of a dependency cycle, so it has no valid '
                               'position in the order'))

    lines = [f'Planned "{sprint.name}" on {project.key}: {len(included)} item(s) in, '
             f'{len(excluded)} left out. Committed {committed} point(s)'
             + (f' against a capacity of {capacity}.' if capacity else
                ' (no capacity stated).')]
    if included:
        lines.append('In the sprint, in build order:')
        lines.extend(f'  {position}. {item.reference} {item.title} '
                     f'[{item.priority}, {item.estimate_points or 0} pt, '
                     f'{item.assignee_label}]'
                     for position, item in enumerate(included, start=1))
    if excluded:
        lines.append('Left out, and why:')
        lines.extend(f'  {item.reference} {item.title} -- {reason}'
                     for item, reason in excluded)
    if capacity and committed > capacity:
        lines.append(f'The sprint is over capacity by {committed - capacity} point(s) '
                     f'because items were named explicitly. That is a real '
                     f'overcommitment, not a rounding.')
    if capacity and committed < capacity * 0.6 and not excluded:
        lines.append(f'Only {committed} of {capacity} point(s) are committed. There is '
                     f'room for more, if the backlog has anything ready.')
    if missing:
        lines.append(f'These ids were not usable: {", ".join(str(pk) for pk in missing)} '
                     f'-- they are not open items on {project.key}.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'sprint_id': sprint.pk, 'committed_points': committed,
              'capacity_points': capacity,
              'included': [_item_row(item) for item in included],
              'excluded': [{'reason': reason, **_item_row(item)}
                           for item, reason in excluded]},
        subject_label='marketing.sprint', subject_id=sprint.pk)


@tool(name='eng.list_sprint_backlog', title='List a sprint backlog',
      description='Everything in one sprint, with points, status and assignee.',
      group='Planning & Estimation', agent_types=('engineering_manager',),
      icon='fa-layer-group', reads_only=True, capability='List a sprint backlog',
      parameters=_obj(('sprint_id',), sprint_id=_i('Sprint id.')))
def list_sprint_backlog(ctx, sprint_id):
    """What is in a sprint, and how much of it is finished."""
    sprint = _find_sprint(sprint_id)
    if sprint is None:
        return _no_sprint(sprint_id)

    items = list(sprint.work_items.select_related('project', 'assignee'))
    counts = _status_counts(sprint.work_items.all())

    header = (f'"{sprint.name}" on {sprint.project.key} -- '
              f'{sprint.get_status_display().lower()}, '
              f'{sprint.start_date:%d %b} to {sprint.end_date:%d %b %Y}'
              if sprint.start_date and sprint.end_date else
              f'"{sprint.name}" on {sprint.project.key}')

    if not items:
        return ToolResult(
            ok=True,
            text=f'{header}. Nothing is in it yet -- eng.plan_sprint fills it.',
            data={'sprint_id': sprint.pk, 'work_items': [], 'count': 0})

    lines = [f'{header}.',
             f'{len(items)} item(s): {_counts_sentence(counts)}.',
             f'{sprint.completed_points} of {sprint.committed_points} committed '
             f'point(s) done'
             + (f', capacity {sprint.capacity_points}.' if sprint.capacity_points
                else '.')]
    lines.extend(f'  {item.reference} [{item.status}] {item.title} -- '
                 f'{item.assignee_label}, {item.estimate_points or 0} pt'
                 + (f', {item.days_overdue} day(s) overdue' if item.is_overdue else '')
                 for item in items)
    if sprint.over_capacity_by:
        lines.append(f'Over capacity by {sprint.over_capacity_by} point(s).')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'sprint_id': sprint.pk, 'status_counts': counts,
              'committed_points': sprint.committed_points,
              'completed_points': sprint.completed_points,
              'capacity_points': sprint.capacity_points,
              'work_items': [_item_row(item) for item in items], 'count': len(items)},
        subject_label='marketing.sprint', subject_id=sprint.pk)


# ===========================================================================
# GROUP: Tracking
# ===========================================================================

@tool(name='eng.track_progress', title='Track progress',
      description=('Counts by status, points done against points committed, and the '
                   'percentage, for a project or a sprint.'),
      group='Tracking', agent_types=('engineering_manager',), icon='fa-chart-line',
      reads_only=True, capability='Track progress',
      parameters=_obj((), project_id=_i('Project to measure.'),
                      sprint_id=_i('Sprint to measure.')))
def track_progress(ctx, project_id=None, sprint_id=None):
    """Where the work actually stands. Counted, not estimated."""
    project = sprint = None
    if sprint_id:
        sprint = _find_sprint(sprint_id)
        if sprint is None:
            return _no_sprint(sprint_id)
        project = sprint.project
    elif project_id:
        project = _find_project(project_id)
        if project is None:
            return _no_project(project_id)
    else:
        message = ('Say what to measure: pass a project_id or a sprint_id. '
                   'eng.list_projects lists the projects.')
        return ToolResult(ok=False, error=message, text=message)

    query = sprint.work_items.all() if sprint is not None else project.work_items.all()
    counts = _status_counts(query)
    total = sum(counts.values())
    live = total - counts.get('cancelled', 0)
    done = counts.get('done', 0)
    percent = int(round(done * 100.0 / live)) if live else 0

    committed = sum(item.estimate_points or 0 for item in query)
    finished_points = sum(item.estimate_points or 0
                          for item in query.filter(status='done'))
    overdue = [item for item in query if item.is_overdue]
    blocked = counts.get('blocked', 0)

    scope = f'sprint "{sprint.name}"' if sprint is not None else f'project {project.key}'
    lines = [f'{scope}: {percent}% complete -- {done} of {live} live item(s) done.',
             f'By status: {_counts_sentence(counts)}.',
             f'Points: {finished_points} done of {committed} committed.']
    if sprint is not None and sprint.capacity_points:
        lines.append(f'Capacity {sprint.capacity_points} point(s); committed '
                     f'{committed}'
                     + (f', over by {sprint.over_capacity_by}.'
                        if sprint.over_capacity_by else '.'))
    if sprint is not None and sprint.end_date:
        left = (sprint.end_date - timezone.localdate()).days
        remaining = committed - finished_points
        if left >= 0:
            lines.append(f'{left} day(s) left, {remaining} point(s) outstanding.')
            if left and remaining and remaining / max(left, 1) > 2:
                lines.append(f'That is {remaining / max(left, 1):.1f} points a day, '
                             f'which is not a plan. Something has to come out.')
        else:
            lines.append(f'The sprint ended {abs(left)} day(s) ago with {remaining} '
                         f'point(s) outstanding.')
    if blocked:
        lines.append(f'{blocked} item(s) are blocked -- eng.identify_blockers says on what.')
    if overdue:
        lines.append(f'{len(overdue)} item(s) are overdue, the worst by '
                     f'{max(item.days_overdue for item in overdue)} day(s).')
    if not overdue and not blocked:
        lines.append('Nothing is overdue and nothing is blocked.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'scope': scope, 'status_counts': counts, 'total': total,
              'percent_complete': percent, 'committed_points': committed,
              'completed_points': finished_points, 'blocked': blocked,
              'overdue': len(overdue)},
        subject_label='marketing.sprint' if sprint is not None else 'marketing.project',
        subject_id=sprint.pk if sprint is not None else project.pk)


@tool(name='eng.find_overdue_items', title='Find overdue items',
      description='Every work item past its due date, with days overdue and who holds it.',
      group='Tracking', agent_types=('engineering_manager',),
      icon='fa-triangle-exclamation', reads_only=True, capability='Find overdue items',
      parameters=_obj((), project_id=_i('Restrict to one project.'),
                      limit=_i('Most items to return. Default 20.')))
def find_overdue_items(ctx, project_id=None, limit=20):
    """What is late, by how long, and whose it is. No softening."""
    project = None
    if project_id:
        project = _find_project(project_id)
        if project is None:
            return _no_project(project_id)

    today = timezone.localdate()
    query = _open_items(project=project).filter(
        due_date__isnull=False, due_date__lt=today).select_related('project', 'assignee')
    items = sorted(query, key=lambda item: -item.days_overdue)
    items = items[:max(1, min(_as_int(limit) or 20, 100))]

    where = f' on {project.key}' if project is not None else ''
    if not items:
        return ToolResult(ok=True, text=f'Nothing is overdue{where}.',
                          data={'overdue': [], 'count': 0})

    by_person = {}
    for item in items:
        by_person[item.assignee_label] = by_person.get(item.assignee_label, 0) + 1

    lines = [f'{len(items)} overdue item(s){where}, worst first:']
    lines.extend(f'  {item.reference} {item.title} -- {item.days_overdue} day(s) late, '
                 f'due {item.due_date:%d %b}, {item.assignee_label}, '
                 f'status {item.status}, priority {item.priority}'
                 for item in items)
    worst = ', '.join(f'{name}: {count}' for name, count
                      in sorted(by_person.items(), key=lambda row: -row[1]))
    lines.append(f'By holder -- {worst}.')
    unassigned = by_person.get('unassigned', 0)
    if unassigned:
        lines.append(f'{unassigned} of them are unassigned, which is why they are late.')

    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'overdue': [_item_row(item) for item in items],
                            'count': len(items), 'by_holder': by_person})


@tool(name='eng.identify_blockers', title='Identify blockers',
      description=('Every item marked blocked, plus every item waiting on an unfinished '
                   'dependency, and what each one is waiting for.'),
      group='Tracking', agent_types=('engineering_manager',), icon='fa-ban',
      reads_only=True, capability='Identify blockers',
      parameters=_obj((), project_id=_i('Restrict to one project.')))
def identify_blockers(ctx, project_id=None):
    """What is stuck, and on what.

    Both kinds are reported. An item somebody marked blocked is the obvious
    one; an item quietly waiting on an unfinished dependency nobody has
    noticed is the one that costs a sprint.
    """
    project = None
    if project_id:
        project = _find_project(project_id)
        if project is None:
            return _no_project(project_id)

    items = list(_open_items(project=project).prefetch_related('depends_on')
                 .select_related('project', 'assignee'))
    declared, implied = [], []
    for item in items:
        unmet = item.unmet_dependencies
        if item.status == 'blocked':
            declared.append((item, item.blocked_reason
                             or (f'waiting on {", ".join(d.reference for d in unmet)}'
                                 if unmet else 'no reason recorded')))
        elif unmet:
            implied.append((item, unmet))

    where = f' on {project.key}' if project is not None else ''
    if not declared and not implied:
        return ToolResult(
            ok=True,
            text=(f'Nothing is blocked{where}: no item is marked blocked, and no open '
                  f'item is waiting on an unfinished dependency.'),
            data={'declared': [], 'implied': [], 'count': 0})

    lines = []
    if declared:
        lines.append(f'{len(declared)} item(s) marked blocked{where}:')
        for item, reason in declared:
            reach = _blocking_reach(item)
            lines.append(f'  {item.reference} {item.title} ({item.assignee_label}) '
                         f'-- {reason}'
                         + (f'; it holds up {reach} other item(s)' if reach else ''))
    if implied:
        lines.append(f'{len(implied)} item(s) not marked blocked but waiting on '
                     f'unfinished work:')
        for item, unmet in implied:
            detail = ', '.join(f'{dep.reference} ({dep.status})' for dep in unmet)
            lines.append(f'  {item.reference} {item.title} ({item.assignee_label}) '
                         f'-- waiting on {detail}')
        lines.append('Those are the expensive ones: nobody has flagged them, so nobody '
                     'is chasing the dependency.')
    if declared:
        lines.append('eng.notify_team_of_blocker prepares a message about any of these '
                     'for approval.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'declared': [{'reason': reason, **_item_row(item)}
                           for item, reason in declared],
              'implied': [{'waiting_on': [d.reference for d in unmet], **_item_row(item)}
                          for item, unmet in implied],
              'count': len(declared) + len(implied)})


@tool(name='eng.suggest_reassignment', title='Suggest reassignment',
      description=('Find where one person holds too much of the overdue or blocking '
                   'work, with the load figures, and suggest where it could move.'),
      group='Tracking', agent_types=('engineering_manager',), icon='fa-people-arrows',
      reads_only=True, capability='Suggest reassignment',
      parameters=_obj((), project_id=_i('Restrict to one project.')))
def suggest_reassignment(ctx, project_id=None):
    """Compare the load people are actually carrying.

    The figures come first and the suggestion second, in that order on
    purpose. 'Move two items off Priya' is an instruction about a person;
    'Priya holds 14 of the 30 open points and 4 of the 5 overdue items' is a
    fact they can answer.
    """
    project = None
    if project_id:
        project = _find_project(project_id)
        if project is None:
            return _no_project(project_id)

    items = list(_open_items(project=project).select_related('assignee', 'project'))
    if not items:
        where = f' on {project.key}' if project is not None else ''
        return ToolResult(ok=True, text=f'There is no open work{where} to balance.',
                          data={'load': [], 'suggestions': []})

    load = {}
    for item in items:
        entry = load.setdefault(item.assignee_label, {
            'items': 0, 'points': 0, 'overdue': 0, 'blocking': 0, 'critical': 0,
            'worst': []})
        entry['items'] += 1
        entry['points'] += item.estimate_points or 0
        if item.is_overdue:
            entry['overdue'] += 1
            entry['worst'].append(item)
        if item.priority in ('high', 'critical'):
            entry['critical'] += 1
        entry['blocking'] += 1 if item.blocks.exclude(
            status__in=('done', 'cancelled')).exists() else 0

    total_points = sum(entry['points'] for entry in load.values()) or 1
    total_overdue = sum(entry['overdue'] for entry in load.values())
    ranked = sorted(load.items(), key=lambda row: -row[1]['points'])

    lines = [f'Open load across {len(load)} holder(s), {len(items)} item(s), '
             f'{total_points} point(s):']
    for name, entry in ranked:
        share = int(round(entry['points'] * 100.0 / total_points))
        lines.append(f'  {name}: {entry["items"]} item(s), {entry["points"]} point(s) '
                     f'({share}%), {entry["overdue"]} overdue, '
                     f'{entry["critical"]} high or critical, '
                     f'{entry["blocking"]} blocking others')

    suggestions = []
    lightest = [name for name, entry in ranked[::-1] if name != 'unassigned']
    for name, entry in ranked:
        if name == 'unassigned':
            if entry['items']:
                suggestions.append(
                    f'{entry["items"]} item(s) ({entry["points"]} points) are '
                    f'unassigned, {entry["overdue"]} of them already overdue. Nobody '
                    f'is going to pick those up by themselves.')
            continue
        share = entry['points'] * 100.0 / total_points
        reasons = []
        if share > 40 and len(load) > 1:
            reasons.append(f'holds {int(round(share))}% of the open points')
        if entry['overdue'] >= 2 and entry['overdue'] >= total_overdue * 0.5:
            reasons.append(f'holds {entry["overdue"]} of the {total_overdue} overdue '
                           f'item(s)')
        if entry['critical'] >= 3:
            reasons.append(f'holds {entry["critical"]} high or critical item(s)')
        if reasons:
            target = next((other for other in lightest if other != name), None)
            worst = sorted(entry['worst'], key=lambda item: -item.days_overdue)[:2]
            move = ', '.join(f'{item.reference} ({item.days_overdue} days late)'
                             for item in worst) or 'their least urgent item'
            suggestions.append(
                f'{name} {" and ".join(reasons)}. Consider moving {move}'
                + (f' to {target}, who holds {load[target]["points"]} point(s).'
                   if target else ', or descoping it.'))

    if suggestions:
        lines.append('')
        lines.append('Where the load is uneven:')
        lines.extend(f'  - {line}' for line in suggestions)
        lines.append('Reassigning has a cost: the new holder has to pick the work up '
                     'from cold. Only worth it where the item is genuinely stuck.')
    else:
        lines.append('The load is reasonably even. No reassignment is worth its cost.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'load': {name: {k: v for k, v in entry.items() if k != 'worst'}
                       for name, entry in load.items()},
              'suggestions': suggestions})


# ===========================================================================
# GROUP: Reporting
# ===========================================================================
# Each report writes a SprintReport row and returns its body. Storing it is
# what makes a status claim answerable later: the metrics travel with the
# prose, so a report can be checked against its own numbers rather than
# re-argued from memory.


@tool(name='eng.generate_sprint_report', title='Generate a sprint report',
      description=('Write and store a sprint report: items by status, points done '
                   'against committed, what is late, what is blocked, and what should '
                   'carry over.'),
      group='Reporting', agent_types=('engineering_manager',), icon='fa-file-chart-column',
      capability='Generate a sprint report',
      parameters=_obj(('sprint_id',), sprint_id=_i('Sprint to report on.')))
def generate_sprint_report(ctx, sprint_id):
    """A sprint report computed from the rows, not from recollection."""
    sprint = _find_sprint(sprint_id)
    if sprint is None:
        return _no_sprint(sprint_id)

    items = list(sprint.work_items.select_related('assignee', 'project')
                 .prefetch_related('depends_on'))
    counts = _status_counts(sprint.work_items.all())
    done = [item for item in items if item.status == 'done']
    carry = [item for item in items if item.is_open]
    overdue = [item for item in items if item.is_overdue]
    blocked = [item for item in items if item.status == 'blocked']

    committed = sprint.committed_points
    completed = sprint.completed_points
    percent = int(round(completed * 100.0 / committed)) if committed else 0

    metrics = {
        'items': len(items), 'done': len(done), 'carry_over': len(carry),
        'overdue': len(overdue), 'blocked': len(blocked),
        'committed_points': committed, 'completed_points': completed,
        'capacity_points': sprint.capacity_points,
        'percent_points_complete': percent, 'status_counts': counts,
    }

    body = [
        f'Sprint report: {sprint.name} ({sprint.project.key} -- {sprint.project.name})',
        f'Period: '
        + (f'{sprint.start_date:%d %b %Y} to {sprint.end_date:%d %b %Y}'
           if sprint.start_date and sprint.end_date else 'dates not set'),
        '',
        'DELIVERY',
        f'{len(done)} of {len(items)} item(s) finished. {completed} of {committed} '
        f'committed point(s) done ({percent}%).',
    ]
    if sprint.capacity_points:
        body.append(f'Capacity was {sprint.capacity_points} point(s); the sprint '
                    f'committed {committed}'
                    + (f', {sprint.over_capacity_by} over.' if sprint.over_capacity_by
                       else ', within capacity.'))
    body.append(f'Status spread: {_counts_sentence(counts)}.')

    if done:
        body.extend(['', 'FINISHED'])
        body.extend(f'  {item.reference} {item.title} ({item.estimate_points or 0} pt, '
                    f'{item.assignee_label})' for item in done)
    else:
        body.extend(['', 'FINISHED', '  Nothing was finished in this sprint.'])

    if carry:
        body.extend(['', 'NOT FINISHED -- WOULD CARRY OVER'])
        body.extend(f'  {item.reference} {item.title} -- {item.status}, '
                    f'{item.estimate_points or 0} pt, {item.assignee_label}'
                    for item in carry)
        body.append(f'  Total carry-over: '
                    f'{sum(item.estimate_points or 0 for item in carry)} point(s).')

    if blocked:
        body.extend(['', 'BLOCKED'])
        body.extend(f'  {item.reference} {item.title} -- '
                    f'{item.blocked_reason or "no reason recorded"}'
                    for item in blocked)

    if overdue:
        body.extend(['', 'OVERDUE'])
        body.extend(f'  {item.reference} {item.title} -- {item.days_overdue} day(s) '
                    f'past {item.due_date:%d %b}' for item in overdue)

    body.extend(['', 'READING OF IT'])
    if committed and percent >= 90:
        body.append('The sprint delivered what it committed to.')
    elif committed and percent >= 60:
        body.append(f'The sprint delivered most of its commitment. The {len(carry)} '
                    f'carry-over item(s) are the thing to discuss: were they too big, '
                    f'or was the capacity wrong?')
    elif committed:
        body.append(f'Only {percent}% of the committed points landed. That is a '
                    f'planning miss rather than a bad fortnight -- either the capacity '
                    f'figure or the estimates need revisiting before the next sprint.')
    else:
        body.append('No points were committed, so there is nothing to measure delivery '
                    'against. Estimate the items before the next sprint.')
    if blocked:
        body.append(f'{len(blocked)} item(s) sat blocked. Whatever they were waiting on '
                    f'is the first thing to fix in the next sprint.')

    text = '\n'.join(body)
    report = SprintReport.objects.create(
        kind='sprint', title=f'Sprint report: {sprint.name}',
        project=sprint.project, sprint=sprint, body=text, metrics=metrics,
        period_start=sprint.start_date, period_end=sprint.end_date,
        created_by_agent=_agent_of(ctx))

    return ToolResult(
        ok=True,
        text=(f'Wrote sprint report #{report.pk} for "{sprint.name}": {len(done)} of '
              f'{len(items)} item(s) done, {completed}/{committed} point(s) '
              f'({percent}%), {len(carry)} carrying over, {len(blocked)} blocked.\n\n'
              + text),
        data={'report_id': report.pk, 'metrics': metrics},
        subject_label='marketing.sprintreport', subject_id=report.pk)


@tool(name='eng.generate_project_status_report', title='Generate a project status report',
      description=('Write and store a project status report: progress, sprint history, '
                   'what is late, what is blocked, and whether the target date is '
                   'still credible.'),
      group='Reporting', agent_types=('engineering_manager',), icon='fa-clipboard-check',
      capability='Generate a project status report',
      parameters=_obj(('project_id',), project_id=_i('Project to report on.')))
def generate_project_status_report(ctx, project_id):
    """A project status account, including whether the date still holds.

    The date question is the one a status report usually dodges. It is
    answered here from the recorded points and the observed completion rate,
    so the answer is checkable even when it is unwelcome.
    """
    project = _find_project(project_id)
    if project is None:
        return _no_project(project_id)

    items = list(project.work_items.select_related('assignee', 'sprint')
                 .prefetch_related('depends_on'))
    counts = _status_counts(project.work_items.all())
    overdue = [item for item in items if item.is_overdue]
    blocked = [item for item in items if item.status == 'blocked'
               or item.unmet_dependencies]
    unestimated = [item for item in items
                   if item.is_open and not item.estimate_points]
    sprints = list(project.sprints.all())
    closed_sprints = [s for s in sprints if s.status == 'closed']

    open_points = sum(item.estimate_points or 0 for item in items if item.is_open)
    done_points = sum(item.estimate_points or 0 for item in items
                      if item.status == 'done')
    velocity = (sum(s.completed_points for s in closed_sprints) / len(closed_sprints)) \
        if closed_sprints else 0

    metrics = {
        'items': len(items), 'open_items': project.open_items,
        'done_items': project.done_items, 'progress_percent': project.progress_percent,
        'open_points': open_points, 'done_points': done_points,
        'overdue': len(overdue), 'blocked': len(blocked),
        'unestimated': len(unestimated), 'sprints': len(sprints),
        'average_velocity': round(velocity, 1), 'status_counts': counts,
    }

    body = [
        f'Project status: {project.key} -- {project.name}',
        f'Status {project.get_status_display()}. '
        f'Owner {project.owner or "unassigned"}.',
        f'Reported {timezone.localdate():%d %b %Y}.',
        '',
        'WHERE IT STANDS',
        f'{project.progress_percent}% of the live work is done '
        f'({project.done_items} done, {project.open_items} open).',
        f'Points: {done_points} finished, {open_points} outstanding.',
        f'Status spread: {_counts_sentence(counts)}.',
    ]

    if sprints:
        body.extend(['', 'SPRINTS'])
        body.extend(f'  {s.name} -- {s.get_status_display()}, '
                    f'{s.completed_points}/{s.committed_points} point(s)'
                    for s in sprints)
        if velocity:
            body.append(f'  Average velocity over {len(closed_sprints)} closed '
                        f'sprint(s): {velocity:.1f} point(s).')

    if overdue:
        body.extend(['', 'LATE'])
        body.extend(f'  {item.reference} {item.title} -- {item.days_overdue} day(s) '
                    f'late, {item.assignee_label}'
                    for item in sorted(overdue, key=lambda i: -i.days_overdue)[:10])
    if blocked:
        body.extend(['', 'BLOCKED OR WAITING'])
        body.extend(f'  {item.reference} {item.title} -- '
                    f'{item.blocked_reason or "waiting on " + ", ".join(d.reference for d in item.unmet_dependencies)}'
                    for item in blocked[:10])

    body.extend(['', 'THE DATE'])
    if not project.target_date:
        body.append('No target date is recorded, so nothing can be said about whether '
                    'it will be met.')
    else:
        days_left = (project.target_date - timezone.localdate()).days
        body.append(f'Target date {project.target_date:%d %b %Y} -- '
                    + (f'{days_left} day(s) away.' if days_left >= 0
                       else f'{abs(days_left)} day(s) ago, already passed.'))
        if velocity and open_points:
            fortnights = open_points / velocity
            needed_days = int(round(fortnights * 14))
            body.append(f'At the observed velocity of {velocity:.1f} point(s) a sprint, '
                        f'{open_points} outstanding point(s) is about '
                        f'{fortnights:.1f} more sprint(s) -- roughly {needed_days} days.')
            if days_left >= 0 and needed_days > days_left:
                body.append(f'That is {needed_days - days_left} day(s) beyond the target '
                            f'date. The date is not currently credible; either scope '
                            f'comes out or the date moves.')
            elif days_left >= 0:
                body.append('The target date is achievable at the current rate, '
                            'provided nothing new is added.')
        elif open_points:
            body.append('There is no closed sprint yet, so there is no velocity to '
                        'forecast from. Any date given now is a guess.')

    if unestimated:
        body.extend(['', 'GAPS IN THE DATA',
                     f'{len(unestimated)} open item(s) carry no estimate, so the '
                     f'outstanding point total is understated. '
                     f'eng.estimate_complexity sizes them.'])

    text = '\n'.join(body)
    report = SprintReport.objects.create(
        kind='project', title=f'Project status: {project.name}',
        project=project, body=text, metrics=metrics,
        period_start=project.start_date, period_end=timezone.localdate(),
        created_by_agent=_agent_of(ctx))

    return ToolResult(
        ok=True,
        text=(f'Wrote project status report #{report.pk} for {project.key}: '
              f'{project.progress_percent}% done, {open_points} point(s) outstanding, '
              f'{len(overdue)} late, {len(blocked)} blocked.\n\n' + text),
        data={'report_id': report.pk, 'metrics': metrics},
        subject_label='marketing.sprintreport', subject_id=report.pk)


@tool(name='eng.generate_weekly_summary', title='Generate a weekly summary',
      description=('Write and store a summary of the last few days: what was finished, '
                   'what started, what is late and what is stuck.'),
      group='Reporting', agent_types=('engineering_manager',), icon='fa-calendar-week',
      capability='Generate a weekly summary',
      parameters=_obj((), project_id=_i('Restrict to one project.'),
                      days=_i('How many days back. Default 7.')))
def generate_weekly_summary(ctx, project_id=None, days=7):
    """What actually moved in the last week, counted from the timestamps."""
    project = None
    if project_id:
        project = _find_project(project_id)
        if project is None:
            return _no_project(project_id)

    window = max(1, min(_as_int(days) or 7, 90))
    since = timezone.now() - timedelta(days=window)
    start_date = since.date()

    scope = WorkItem.objects.filter(project=project) if project is not None \
        else WorkItem.objects.all()

    finished = list(scope.filter(status='done', completed_at__gte=since)
                    .select_related('project', 'assignee'))
    created = list(scope.filter(created_at__gte=since).select_related('project'))
    started = list(scope.filter(status__in=('in_progress', 'in_review'))
                   .select_related('project', 'assignee'))
    blocked = list(scope.filter(status='blocked').select_related('project', 'assignee'))
    overdue = [item for item in scope.filter(
        status__in=WorkItem.OPEN_STATUSES, due_date__isnull=False,
        due_date__lt=timezone.localdate()).select_related('project', 'assignee')]

    points_done = sum(item.estimate_points or 0 for item in finished)
    metrics = {
        'window_days': window, 'finished': len(finished), 'created': len(created),
        'in_flight': len(started), 'blocked': len(blocked), 'overdue': len(overdue),
        'points_finished': points_done,
    }

    where = f'{project.key} -- {project.name}' if project is not None \
        else 'all projects'
    body = [
        f'Weekly summary: {where}',
        f'Last {window} day(s), {start_date:%d %b} to {timezone.localdate():%d %b %Y}.',
        '',
        'FINISHED',
    ]
    if finished:
        body.extend(f'  {item.reference} {item.title} ({item.estimate_points or 0} pt, '
                    f'{item.assignee_label})' for item in finished)
        body.append(f'  {len(finished)} item(s), {points_done} point(s).')
    else:
        body.append(f'  Nothing was completed in the last {window} day(s).')

    body.extend(['', 'IN FLIGHT'])
    if started:
        body.extend(f'  {item.reference} {item.title} -- {item.status}, '
                    f'{item.assignee_label}' for item in started[:15])
    else:
        body.append('  Nothing is in progress or in review.')

    body.extend(['', 'NEW WORK RECORDED'])
    body.append(f'  {len(created)} item(s) were created in the window'
                + (f', totalling '
                   f'{sum(item.estimate_points or 0 for item in created)} point(s).'
                   if created else '.'))
    if created and finished and len(created) > len(finished) * 2:
        body.append(f'  More than twice as much work arrived as left. At that rate the '
                    f'backlog grows regardless of how hard anybody works.')

    if blocked:
        body.extend(['', 'BLOCKED'])
        body.extend(f'  {item.reference} {item.title} -- '
                    f'{item.blocked_reason or "no reason recorded"}'
                    for item in blocked[:10])
    if overdue:
        body.extend(['', 'LATE'])
        body.extend(f'  {item.reference} {item.title} -- {item.days_overdue} day(s), '
                    f'{item.assignee_label}'
                    for item in sorted(overdue, key=lambda i: -i.days_overdue)[:10])
    if not blocked and not overdue:
        body.extend(['', 'Nothing is blocked and nothing is late.'])

    text = '\n'.join(body)
    report = SprintReport.objects.create(
        kind='weekly', title=f'Weekly summary: {where}',
        project=project, body=text, metrics=metrics,
        period_start=start_date, period_end=timezone.localdate(),
        created_by_agent=_agent_of(ctx))

    return ToolResult(
        ok=True,
        text=(f'Wrote weekly summary #{report.pk} covering {window} day(s) for {where}: '
              f'{len(finished)} finished ({points_done} points), {len(started)} in '
              f'flight, {len(blocked)} blocked, {len(overdue)} late.\n\n' + text),
        data={'report_id': report.pk, 'metrics': metrics},
        subject_label='marketing.sprintreport', subject_id=report.pk)


# The technical requirements a feature needs whatever it is, in the order they
# have to be decided. Each entry is (category, template).
_TECHNICAL_REQUIREMENTS = (
    ('Data', 'Define the tables, columns and constraints {feature} needs, with a '
             'reversible migration and a plan for existing rows.'),
    ('API', 'Expose {feature} through explicit endpoints with documented request and '
            'response shapes, and a status code for every failure, not only for success.'),
    ('Authentication and access', 'Decide who may use {feature}. Enforce it server '
                                  'side; hiding a control in the interface is not a '
                                  'permission.'),
    ('Validation', 'Validate every input to {feature} at the boundary, and reject bad '
                   'input with a message naming the field at fault.'),
    ('Error handling', 'Decide what {feature} does when a dependency is unavailable: '
                       'fail visibly, retry, or degrade. Never swallow the error.'),
    ('Performance', 'State the acceptable response time for {feature} at a realistic '
                    'data volume, and the query count it may use to get there.'),
    ('Observability', 'Log the actions {feature} takes with enough context to answer '
                      '"what happened to this record" without a debugger.'),
    ('Testing', 'Cover {feature} with tests for the ordinary path, the boundaries and '
                'the failures, including the error paths above.'),
    ('Data protection', 'Identify any personal data {feature} touches, and decide '
                        'retention, access and deletion before it is collected.'),
    ('Documentation', 'Write down how {feature} works and how to operate it, including '
                      'what to do when it fails at three in the morning.'),
    ('Migration and rollout', 'Decide how {feature} reaches production: behind a flag, '
                              'in stages, or all at once, and how it is rolled back.'),
    ('Dependencies', 'List what {feature} depends on that this team does not control, '
                     'and what happens when each is unavailable.'),
)


@tool(name='eng.generate_technical_requirements', title='Generate technical requirements',
      description=('Produce the technical and non-functional requirements a feature '
                   'needs: data, API, access, validation, error handling, performance, '
                   'observability, testing, data protection and rollout.'),
      group='Reporting', agent_types=('engineering_manager',), icon='fa-list-ol',
      capability='Generate technical requirements',
      parameters=_obj((), project_id=_i('Project for context.'),
                      feature=_s('The feature the requirements are for.'),
                      count=_i('How many to return. Default 6, maximum 12.')))
def generate_technical_requirements(ctx, project_id=None, feature='', count=6):
    """The requirements a feature needs whether or not anybody wrote them down.

    These are the non-functional obligations that get discovered late: access
    control decided after launch, error handling added after an outage, a
    performance number invented in the incident review. Stating them at
    breakdown time costs one paragraph each.
    """
    project = None
    if project_id:
        project = _find_project(project_id)
        if project is None:
            return _no_project(project_id)

    subject = str(feature or '').strip() or (
        f'{project.name}' if project is not None else 'this feature')
    wanted = max(1, min(_as_int(count) or 6, len(_TECHNICAL_REQUIREMENTS)))

    lowered = f'{subject} {project.description if project is not None else ""}'.lower()
    ordered = list(_TECHNICAL_REQUIREMENTS)
    # Move the categories the text already talks about to the front, so the
    # list leads with what this particular feature has raised.
    def relevance(entry):
        category = entry[0].lower().split()[0]
        return 0 if category in lowered else 1
    ordered.sort(key=relevance)

    rows, lines = [], [f'Technical and non-functional requirements for {subject}'
                       + (f' ({project.key})' if project is not None else '') + ':']
    for position, (category, template) in enumerate(ordered[:wanted], start=1):
        statement = template.format(feature=subject)
        rows.append({'number': position, 'category': category,
                     'requirement': statement})
        lines.append(f'{position}. {category}: {statement}')

    if project is not None and project.tech_stack:
        lines.append(f'Stack in use: {", ".join(project.tech_stack)} -- the answers '
                     f'above should be the idiomatic ones for that stack rather than '
                     f'invented afresh.')
    lines.append('These are obligations, not suggestions. Each one can be recorded as a '
                 'work item with eng.create_work_item so it is tracked rather than '
                 'assumed.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'feature': subject, 'requirements': rows, 'count': len(rows),
              'project_id': project.pk if project is not None else None})


# ===========================================================================
# GROUP: Team Communication -- everything below needs a person
# ===========================================================================
# Each tool here returns a Proposal and stops. The matching executor is the
# only code that can reach the outside world, and marketing/approvals.py is
# the only caller of it. The proposing function has nothing to send with, so
# there is no path by which a message escapes review.


def _first(data, *keys, default=''):
    """The first present value among several likely keys.

    Connectors are written independently, so one returns 'key' and another
    'issue_key' for the same thing. Reading defensively here is cheaper than
    demanding a single shape from every connector.
    """
    for key in keys:
        value = (data or {}).get(key)
        if value not in (None, '', [], {}):
            return value
    return default


def _subject_of(item):
    if item is None:
        return '', 0, ''
    return 'marketing.workitem', item.pk, f'{item.reference} {item.title}'[:200]


def _item_brief(item):
    """A work item as text suitable for an external issue body."""
    lines = [item.description or item.title]
    if item.acceptance_criteria:
        lines.append('')
        lines.append('Acceptance criteria:')
        lines.extend(f'- {line}' for line in item.acceptance_criteria)
    lines.append('')
    lines.append(f'Internal reference {item.reference} on project '
                 f'{item.project.key}. Priority {item.priority}, complexity '
                 f'{item.complexity}, estimate {item.estimate_points or 0} point(s).')
    if item.due_date:
        lines.append(f'Due {item.due_date:%d %b %Y}.')
    return '\n'.join(lines)


def _record_message(action, result, *, recipient, body, subject=''):
    """Write the OutboundMessage row that says what execution did."""
    return OutboundMessage.objects.create(
        channel='slack', recipient=recipient or 'default channel',
        subject=subject[:300], body=body,
        status='simulated' if result.demo else ('sent' if result.ok else 'failed'),
        external_id=str(_first(result.data, 'ts', 'message_id', 'id')),
        external_url=str(_first(result.data, 'permalink', 'url')),
        error_message=result.error or '',
        agent=action.agent, action=action, integration=action.integration,
        metadata={'operation': result.operation, 'provider': result.provider})


def _slack_proposal(*, title, summary, channel, text, work_item=None, extra=None,
                    confirmation=''):
    """One shape for every Slack message this employee prepares."""
    payload = {'channel': str(channel or '').strip(), 'text': text}
    if work_item is not None:
        payload['work_item_id'] = work_item.pk
    payload.update(extra or {})
    label, subject_id, display = _subject_of(work_item)
    return Proposal(
        title=title[:250], summary=summary, payload=payload,
        editable_fields=[
            editable('channel', 'Slack channel', 'text',
                     'Leave blank to use the default channel on the integration.'),
            editable('text', 'Message', 'longtext', 'Exactly what will be posted.'),
        ],
        integration_key='slack', subject_label=label, subject_id=subject_id,
        subject_display=display, confirmation=confirmation)


@tool(name='eng.create_jira_issue', title='File a Jira issue',
      description=('Prepare a Jira issue from a work item or from given text. Goes to '
                   'the approval queue; on approval the Jira key is written back onto '
                   'the work item.'),
      group='Team Communication', agent_types=('engineering_manager',),
      integration='jira', requires_approval=True, risk='medium', icon='fa-jira',
      capability='File a Jira issue',
      parameters=_obj(
          (),
          work_item_id=_i('Work item to file. Fills the summary and description.'),
          project_key=_s('Jira project key. Defaults to the project setting.'),
          summary=_s('Issue summary, when no work item is given.'),
          description=_s('Issue description.'),
          issue_type=_s("Jira issue type, e.g. Task, Story, Bug."),
          priority=_s('Jira priority name.'),
          assignee=_s('Jira account to assign it to.'),
          labels=_a('Labels.')))
def create_jira_issue(ctx, work_item_id=None, project_key='', summary='',
                      description='', issue_type='Task', priority='', assignee='',
                      labels=None):
    """Prepare a Jira issue. Nothing is filed until somebody approves it."""
    item = None
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)

    resolved_key = (str(project_key or '').strip()
                    or (item.project.jira_project_key if item is not None else ''))
    resolved_summary = str(summary or '').strip() or (item.title if item else '')
    resolved_body = str(description or '').strip() or (_item_brief(item) if item else '')

    if not resolved_summary:
        message = ('There is nothing to file: pass a work_item_id or a summary. '
                   'eng.list_work_items shows the items that exist.')
        return ToolResult(ok=False, error=message, text=message)
    if not resolved_key:
        message = ('No Jira project key is known. Pass project_key, or record one on '
                   'the project so future issues know where to go.')
        return ToolResult(ok=False, error=message, text=message)

    jira_priority = str(priority or '').strip() or (
        {'critical': 'Highest', 'high': 'High', 'medium': 'Medium',
         'low': 'Low', 'lowest': 'Lowest'}.get(item.priority, '') if item else '')
    tags = _as_list(labels) or (item.labels if item else [])

    label, subject_id, display = _subject_of(item)
    return Proposal(
        title=f'Jira issue in {resolved_key}: {resolved_summary[:120]}',
        summary=(f'Files a {issue_type} in Jira project {resolved_key}'
                 + (f' for {item.reference}. On approval the Jira key is written '
                    f'back onto the work item.' if item is not None
                    else ' from the text given.')),
        payload={'work_item_id': item.pk if item else None,
                 'project_key': resolved_key, 'summary': resolved_summary,
                 'description': resolved_body, 'issue_type': issue_type or 'Task',
                 'priority': jira_priority, 'assignee': str(assignee or '').strip(),
                 'labels': tags},
        editable_fields=[
            editable('project_key', 'Jira project', 'text'),
            editable('summary', 'Summary', 'text'),
            editable('description', 'Description', 'longtext'),
            editable('issue_type', 'Issue type', 'text'),
            editable('priority', 'Priority', 'text'),
            editable('assignee', 'Assignee', 'text'),
        ],
        risk='medium', integration_key='jira',
        subject_label=label, subject_id=subject_id, subject_display=display)


@executor('eng.create_jira_issue')
def execute_create_jira_issue(action):
    """File the approved Jira issue and record what came back."""
    payload = dict(action.payload or {})
    item = _find_item(payload.get('work_item_id'))

    result = _call('jira', 'create_issue',
                   project=payload.get('project_key', ''),
                   summary=payload.get('summary', ''),
                   description=payload.get('description', ''),
                   issue_type=payload.get('issue_type', 'Task'),
                   priority=payload.get('priority', ''),
                   assignee=payload.get('assignee', ''),
                   labels=payload.get('labels') or [])

    key = str(_first(result.data, 'key', 'issue_key', 'reference', 'id'))
    url = str(_first(result.data, 'url', 'browse_url', 'html_url', 'self'))

    ExternalIssue.objects.create(
        system='jira', container=payload.get('project_key', ''),
        reference=key[:60], title=payload.get('summary', '')[:300],
        body=payload.get('description', ''),
        issue_status='simulated' if result.demo else ('open' if result.ok else 'open'),
        assignee=str(payload.get('assignee') or '')[:120],
        priority=str(payload.get('priority') or '')[:30],
        labels=payload.get('labels') or [], url=url[:400],
        external_id=str(_first(result.data, 'id', 'key')),
        agent=action.agent, action=action,
        subject_label='marketing.workitem' if item else '',
        subject_id=item.pk if item else None)

    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Jira would not accept the issue: {result.error}')

    if item is not None and key:
        item.external_reference = key[:60]
        item.external_url = url[:400]
        item.save(update_fields=['external_reference', 'external_url', 'updated_at'])

    where = 'simulated' if result.demo else 'filed'
    return ToolResult(
        ok=True, demo=result.demo,
        text=(f'{where.capitalize()} Jira issue {key or "(no key returned)"} in '
              f'{payload.get("project_key")}: {payload.get("summary")}.'
              + (f' Written back onto {item.reference}.' if item and key else '')),
        data={'key': key, 'url': url, 'demo': result.demo})


@tool(name='eng.update_jira_issue', title='Update a Jira issue',
      description=('Prepare a change to an existing Jira issue -- summary, '
                   'description, status, priority or assignee.'),
      group='Team Communication', agent_types=('engineering_manager',),
      integration='jira', requires_approval=True, risk='medium', icon='fa-jira',
      capability='Update a Jira issue',
      parameters=_obj(
          ('issue_key',),
          issue_key=_s("The Jira key, e.g. ENG-118."),
          summary=_s('New summary.'),
          description=_s('New description.'),
          status=_s('Status to move it to.'),
          priority=_s('New priority.'),
          assignee=_s('New assignee.')))
def update_jira_issue(ctx, issue_key, summary='', description='', status='',
                      priority='', assignee=''):
    """Prepare a Jira update. Only the fields given will be sent."""
    key = str(issue_key or '').strip()
    if not key:
        message = 'Which Jira issue? Pass its key, e.g. ENG-118.'
        return ToolResult(ok=False, error=message, text=message)

    fields = {name: str(value).strip() for name, value in (
        ('summary', summary), ('description', description), ('status', status),
        ('priority', priority), ('assignee', assignee)) if str(value or '').strip()}

    if not fields:
        message = (f'Nothing was given to change on {key}. Name at least one of '
                   f'summary, description, status, priority or assignee.')
        return ToolResult(ok=False, error=message, text=message)

    return Proposal(
        title=f'Update Jira {key}: {", ".join(fields)}',
        summary=(f'Changes {", ".join(fields)} on {key}. Fields not listed are left '
                 f'as they are.'),
        payload={'key': key, **fields},
        editable_fields=[editable(name, name.capitalize(),
                                  'longtext' if name == 'description' else 'text')
                         for name in fields],
        risk='medium', integration_key='jira',
        subject_display=key)


@executor('eng.update_jira_issue')
def execute_update_jira_issue(action):
    """Apply the approved Jira update."""
    payload = dict(action.payload or {})
    key = payload.pop('key', '')
    result = _call('jira', 'update_issue', key=key, **payload)

    ExternalIssue.objects.create(
        system='jira', reference=str(key)[:60],
        title=payload.get('summary') or f'Update to {key}',
        body='\n'.join(f'{name}: {value}' for name, value in payload.items()),
        issue_status='simulated' if result.demo else 'in_progress',
        assignee=str(payload.get('assignee') or '')[:120],
        priority=str(payload.get('priority') or '')[:30],
        url=str(_first(result.data, 'url', 'html_url'))[:400],
        agent=action.agent, action=action)

    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Jira would not accept the update to {key}: '
                               f'{result.error}')
    return ToolResult(
        ok=True, demo=result.demo,
        text=(('Simulated update to ' if result.demo else 'Updated ')
              + f'Jira {key}: ' + ', '.join(f'{name} set' for name in payload) + '.'),
        data={'key': key, 'fields': list(payload), 'demo': result.demo})


@tool(name='eng.create_github_issue', title='Open a GitHub issue',
      description=('Prepare a GitHub issue from a work item or from given text. Goes '
                   'to the approval queue; on approval the issue number is written '
                   'back onto the work item.'),
      group='Team Communication', agent_types=('engineering_manager',),
      integration='github', requires_approval=True, risk='medium', icon='fa-github',
      capability='Open a GitHub issue',
      parameters=_obj(
          (),
          work_item_id=_i('Work item to file.'),
          repo=_s("Repository as 'owner/name'. Defaults to the project setting."),
          title=_s('Issue title, when no work item is given.'),
          body=_s('Issue body.'),
          labels=_a('Labels.'),
          assignees=_a('GitHub usernames to assign.')))
def create_github_issue(ctx, work_item_id=None, repo='', title='', body='',
                        labels=None, assignees=None):
    """Prepare a GitHub issue. Nothing is opened until somebody approves it."""
    item = None
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)

    repository = (str(repo or '').strip()
                  or (item.project.repository if item is not None else ''))
    heading = str(title or '').strip() or (item.title if item else '')
    text = str(body or '').strip() or (_item_brief(item) if item else '')

    if not heading:
        message = ('There is nothing to file: pass a work_item_id or a title.')
        return ToolResult(ok=False, error=message, text=message)
    if not repository or '/' not in repository:
        message = ("No repository is known. Pass repo as 'owner/name', or record one "
                   "on the project.")
        return ToolResult(ok=False, error=message, text=message)

    label, subject_id, display = _subject_of(item)
    return Proposal(
        title=f'GitHub issue in {repository}: {heading[:120]}',
        summary=(f'Opens an issue on {repository}'
                 + (f' for {item.reference}. On approval the issue number is written '
                    f'back onto the work item.' if item else '.')),
        payload={'work_item_id': item.pk if item else None, 'repo': repository,
                 'title': heading, 'body': text,
                 'labels': _as_list(labels) or (item.labels if item else []),
                 'assignees': _as_list(assignees)},
        editable_fields=[
            editable('repo', 'Repository', 'text'),
            editable('title', 'Title', 'text'),
            editable('body', 'Body', 'longtext'),
        ],
        risk='medium', integration_key='github',
        subject_label=label, subject_id=subject_id, subject_display=display)


@executor('eng.create_github_issue')
def execute_create_github_issue(action):
    """Open the approved GitHub issue and record it."""
    payload = dict(action.payload or {})
    item = _find_item(payload.get('work_item_id'))

    result = _call('github', 'create_issue',
                   repo=payload.get('repo', ''), title=payload.get('title', ''),
                   body=payload.get('body', ''), labels=payload.get('labels') or [],
                   assignees=payload.get('assignees') or [])

    number = _first(result.data, 'number', 'issue_number', 'id')
    reference = f'#{number}' if number else ''
    url = str(_first(result.data, 'html_url', 'url'))

    ExternalIssue.objects.create(
        system='github', container=payload.get('repo', ''), reference=reference[:60],
        title=payload.get('title', '')[:300], body=payload.get('body', ''),
        issue_status='simulated' if result.demo else 'open',
        assignee=', '.join(payload.get('assignees') or [])[:120],
        labels=payload.get('labels') or [], url=url[:400],
        external_id=str(number), agent=action.agent, action=action,
        subject_label='marketing.workitem' if item else '',
        subject_id=item.pk if item else None)

    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'GitHub would not accept the issue: {result.error}')

    if item is not None and reference:
        item.external_reference = reference[:60]
        item.external_url = url[:400]
        item.save(update_fields=['external_reference', 'external_url', 'updated_at'])

    return ToolResult(
        ok=True, demo=result.demo,
        text=(('Simulated opening ' if result.demo else 'Opened ')
              + f'GitHub issue {reference or "(no number returned)"} on '
              + f'{payload.get("repo")}: {payload.get("title")}.'
              + (f' Written back onto {item.reference}.' if item and reference else '')),
        data={'number': number, 'url': url, 'demo': result.demo})


@tool(name='eng.notify_developer', title='Notify a developer',
      description=('Prepare a Slack message to a developer about a work item. Short '
                   'and specific: what, by when, and why it matters now.'),
      group='Team Communication', agent_types=('engineering_manager',),
      integration='slack', requires_approval=True, icon='fa-slack',
      capability='Notify a developer',
      parameters=_obj(
          ('message',),
          message=_s('What to tell them.'),
          channel=_s('Slack channel or user. Defaults to the integration setting.'),
          work_item_id=_i('Work item this is about. Its reference is included.'),
          employee_id=_i('Employee this is for, for the record.')))
def notify_developer(ctx, message, channel='', work_item_id=None, employee_id=None):
    """Prepare a message to a developer. It is not sent until approved."""
    if not str(message or '').strip():
        message_text = 'There is no message to send. Say what the developer needs to know.'
        return ToolResult(ok=False, error=message_text, text=message_text)

    item = None
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)

    employee = _find_employee(employee_id) if employee_id else None
    who = employee.full_name if employee is not None else (
        item.assignee_label if item is not None else 'the team')

    lines = [str(message).strip()]
    if item is not None:
        detail = f'{item.reference}: {item.title} ({item.priority} priority'
        detail += f', due {item.due_date:%d %b}' if item.due_date else ''
        detail += ')'
        lines.append(detail)
        if item.acceptance_criteria:
            lines.append('Done when: ' + '; '.join(
                str(line) for line in item.acceptance_criteria[:3]))

    return _slack_proposal(
        title=f'Slack message to {who}'
              + (f' about {item.reference}' if item is not None else ''),
        summary=f'Tells {who} about '
                + (item.reference if item is not None else 'engineering work')
                + '. Nothing is sent until this is approved.',
        channel=channel, text='\n'.join(lines), work_item=item,
        extra={'employee_id': employee.pk if employee is not None else None,
               'recipient_label': who})


@executor('eng.notify_developer')
def execute_notify_developer(action):
    """Post the approved developer notification."""
    payload = dict(action.payload or {})
    result = _call('slack', 'post_message', channel=payload.get('channel', ''),
                   text=payload.get('text', ''))
    _record_message(action, result, recipient=payload.get('channel', ''),
                    body=payload.get('text', ''),
                    subject=f'Notification to {payload.get("recipient_label", "a developer")}')
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Slack would not accept the message: {result.error}')
    return ToolResult(
        ok=True, demo=result.demo,
        text=(('Simulated a Slack message to ' if result.demo else 'Sent a Slack message to ')
              + (payload.get('channel') or 'the default channel')
              + f' for {payload.get("recipient_label", "a developer")}.'),
        data={'channel': payload.get('channel'), 'demo': result.demo})


@tool(name='eng.send_deadline_reminder', title='Send a deadline reminder',
      description=('Prepare a Slack reminder about a work item deadline, with the date, '
                   'the days remaining and who holds it.'),
      group='Team Communication', agent_types=('engineering_manager',),
      integration='slack', requires_approval=True, icon='fa-bell',
      capability='Send a deadline reminder',
      parameters=_obj(
          ('work_item_id',),
          work_item_id=_i('Work item the reminder is about.'),
          channel=_s('Slack channel. Defaults to the integration setting.'),
          message=_s('Extra wording to include.')))
def send_deadline_reminder(ctx, work_item_id, channel='', message=''):
    """Prepare a deadline reminder that states the actual position.

    A reminder that says 'this is due soon' when the item is four days late
    trains people to ignore reminders, so the wording is generated from the
    dates rather than left to a template.
    """
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)

    if not item.due_date:
        text = (f'{item.reference} has no due date, so there is no deadline to remind '
                f'anybody about. eng.set_deadline sets one first.')
        return ToolResult(ok=False, error='no due date', text=text)

    days = (item.due_date - timezone.localdate()).days
    if days < 0:
        opening = (f'{item.reference} was due {item.due_date:%d %b} and is '
                   f'{abs(days)} day(s) overdue.')
    elif days == 0:
        opening = f'{item.reference} is due today ({item.due_date:%d %b}).'
    else:
        opening = f'{item.reference} is due in {days} day(s), on {item.due_date:%d %b}.'

    lines = [opening, f'{item.title} -- currently {item.status}, '
                      f'{item.assignee_label}, {item.estimate_points or 0} point(s).']
    if str(message or '').strip():
        lines.append(str(message).strip())
    if item.status in ('backlog', 'todo') and days <= 2:
        lines.append('It has not been started yet, which is the reason for the reminder.')
    lines.append('If the date is no longer realistic, say so now rather than on the day.')

    return _slack_proposal(
        title=f'Deadline reminder for {item.reference}',
        summary=f'Reminds the holder of {item.reference} about its '
                f'{item.due_date:%d %b} deadline.',
        channel=channel, text='\n'.join(lines), work_item=item,
        extra={'recipient_label': item.assignee_label})


@executor('eng.send_deadline_reminder')
def execute_send_deadline_reminder(action):
    """Post the approved deadline reminder."""
    payload = dict(action.payload or {})
    result = _call('slack', 'post_message', channel=payload.get('channel', ''),
                   text=payload.get('text', ''))
    _record_message(action, result, recipient=payload.get('channel', ''),
                    body=payload.get('text', ''), subject='Deadline reminder')
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Slack would not accept the reminder: {result.error}')
    return ToolResult(ok=True, demo=result.demo,
                      text=(('Simulated a deadline reminder to ' if result.demo
                             else 'Sent a deadline reminder to ')
                            + (payload.get('channel') or 'the default channel') + '.'),
                      data={'demo': result.demo})


@tool(name='eng.notify_team_of_blocker', title='Tell the team about a blocker',
      description=('Prepare a Slack message about a blocked work item: what is stuck, '
                   'what it is waiting on, and what it holds up.'),
      group='Team Communication', agent_types=('engineering_manager',),
      integration='slack', requires_approval=True, icon='fa-ban',
      capability='Tell the team about a blocker',
      parameters=_obj(
          ('work_item_id',),
          work_item_id=_i('The blocked work item.'),
          channel=_s('Slack channel.'),
          message=_s('Extra wording to include.')))
def notify_team_of_blocker(ctx, work_item_id, channel='', message=''):
    """Prepare a blocker notice, including the cost of the blockage."""
    item = _find_item(work_item_id)
    if item is None:
        return _no_item(work_item_id)

    unmet = item.unmet_dependencies
    reach = _blocking_reach(item)
    reason = item.blocked_reason or (
        'waiting on ' + ', '.join(f'{dep.reference} ({dep.status})' for dep in unmet)
        if unmet else 'no reason recorded')

    lines = [f'Blocked: {item.reference} {item.title}',
             f'Waiting on: {reason}.',
             f'Holder: {item.assignee_label}. Priority {item.priority}.']
    if reach:
        lines.append(f'This holds up {reach} other item(s), so it is not only its own '
                     f'delay.')
    if item.due_date:
        lines.append(f'It is due {item.due_date:%d %b}'
                     + (f' and already {item.days_overdue} day(s) late.'
                        if item.is_overdue else '.'))
    if str(message or '').strip():
        lines.append(str(message).strip())
    lines.append('What is needed: whoever owns the dependency to say when it will land, '
                 'or agreement to work around it.')

    return _slack_proposal(
        title=f'Blocker notice for {item.reference}',
        summary=f'Tells the team that {item.reference} is blocked on {reason[:80]}.',
        channel=channel, text='\n'.join(lines), work_item=item,
        extra={'blocking_reach': reach})


@executor('eng.notify_team_of_blocker')
def execute_notify_team_of_blocker(action):
    """Post the approved blocker notice."""
    payload = dict(action.payload or {})
    result = _call('slack', 'post_message', channel=payload.get('channel', ''),
                   text=payload.get('text', ''))
    _record_message(action, result, recipient=payload.get('channel', ''),
                    body=payload.get('text', ''), subject='Blocker notice')
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Slack would not accept the notice: {result.error}')
    return ToolResult(ok=True, demo=result.demo,
                      text=(('Simulated a blocker notice to ' if result.demo
                             else 'Posted a blocker notice to ')
                            + (payload.get('channel') or 'the default channel') + '.'),
                      data={'demo': result.demo})


@tool(name='eng.send_sprint_update', title='Send a sprint update',
      description=('Prepare a Slack update on a sprint: points done against committed, '
                   'days left, and what is blocked.'),
      group='Team Communication', agent_types=('engineering_manager',),
      integration='slack', requires_approval=True, icon='fa-person-running',
      capability='Send a sprint update',
      parameters=_obj(
          ('sprint_id',),
          sprint_id=_i('Sprint to report on.'),
          channel=_s('Slack channel.'),
          message=_s('Extra wording to include.')))
def send_sprint_update(ctx, sprint_id, channel='', message=''):
    """Prepare a sprint update built from the current figures."""
    sprint = _find_sprint(sprint_id)
    if sprint is None:
        return _no_sprint(sprint_id)

    counts = _status_counts(sprint.work_items.all())
    blocked = [item for item in sprint.work_items.all() if item.status == 'blocked']
    days_left = ((sprint.end_date - timezone.localdate()).days
                 if sprint.end_date else None)

    lines = [f'{sprint.name} update ({sprint.project.key})',
             f'{sprint.completed_points} of {sprint.committed_points} committed '
             f'point(s) done.',
             f'Items: {_counts_sentence(counts)}.']
    if days_left is not None:
        lines.append(f'{days_left} day(s) left.' if days_left >= 0
                     else f'The sprint ended {abs(days_left)} day(s) ago.')
        remaining = sprint.committed_points - sprint.completed_points
        if days_left and days_left > 0 and remaining:
            lines.append(f'{remaining} point(s) outstanding -- '
                         f'{remaining / days_left:.1f} a day to finish on time.')
    if blocked:
        lines.append('Blocked: ' + '; '.join(
            f'{item.reference} ({item.blocked_reason or "reason not recorded"})'
            for item in blocked))
    if str(message or '').strip():
        lines.append(str(message).strip())

    return _slack_proposal(
        title=f'Sprint update: {sprint.name}',
        summary=f'Posts the current figures for {sprint.name} on '
                f'{sprint.project.key}.',
        channel=channel, text='\n'.join(lines),
        extra={'sprint_id': sprint.pk})


@executor('eng.send_sprint_update')
def execute_send_sprint_update(action):
    """Post the approved sprint update."""
    payload = dict(action.payload or {})
    result = _call('slack', 'post_message', channel=payload.get('channel', ''),
                   text=payload.get('text', ''))
    _record_message(action, result, recipient=payload.get('channel', ''),
                    body=payload.get('text', ''), subject='Sprint update')
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Slack would not accept the update: {result.error}')
    return ToolResult(ok=True, demo=result.demo,
                      text=(('Simulated a sprint update to ' if result.demo
                             else 'Posted a sprint update to ')
                            + (payload.get('channel') or 'the default channel') + '.'),
                      data={'demo': result.demo})


@tool(name='eng.post_engineering_announcement', title='Post an engineering announcement',
      description='Prepare a Slack announcement to the engineering team.',
      group='Team Communication', agent_types=('engineering_manager',),
      integration='slack', requires_approval=True, icon='fa-bullhorn',
      capability='Post an engineering announcement',
      parameters=_obj(('title', 'body'),
                      title=_s('Announcement heading.'),
                      body=_s('The announcement itself.'),
                      channel=_s('Slack channel.')))
def post_engineering_announcement(ctx, title, body, channel=''):
    """Prepare a team announcement. It reaches nobody until approved."""
    if not str(title or '').strip() or not str(body or '').strip():
        message = 'An announcement needs both a heading and a body.'
        return ToolResult(ok=False, error=message, text=message)

    text = f'*{str(title).strip()}*\n{str(body).strip()}'
    return _slack_proposal(
        title=f'Engineering announcement: {str(title).strip()[:120]}',
        summary='Posts an announcement to the engineering channel.',
        channel=channel, text=text, extra={'heading': str(title).strip()[:200]})


@executor('eng.post_engineering_announcement')
def execute_post_engineering_announcement(action):
    """Post the approved announcement."""
    payload = dict(action.payload or {})
    result = _call('slack', 'post_message', channel=payload.get('channel', ''),
                   text=payload.get('text', ''))
    _record_message(action, result, recipient=payload.get('channel', ''),
                    body=payload.get('text', ''),
                    subject=payload.get('heading', 'Engineering announcement'))
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Slack would not accept the announcement: '
                               f'{result.error}')
    return ToolResult(ok=True, demo=result.demo,
                      text=(('Simulated an announcement to ' if result.demo
                             else 'Posted an announcement to ')
                            + (payload.get('channel') or 'the default channel') + '.'),
                      data={'demo': result.demo})


# The agenda a meeting of each kind actually needs. Written out because a
# meeting with no agenda becomes a status round-robin, which is the most
# expensive way for a team to exchange information.
_MEETING_AGENDAS = {
    'sprint_planning': (
        'Capacity for the sprint, honestly stated',
        'Backlog in priority order, with the estimates and what they assume',
        'Dependencies that must land first, and who owns each',
        'What is being left out, and the consequence of leaving it out',
        'The commitment, in points and in items',
    ),
    'review': (
        'What was finished, demonstrated rather than described',
        'What was not finished, and why',
        'What carries over, and whether it should be re-estimated',
        'Feedback that changes the next sprint',
    ),
    'retro': (
        'What worked, specifically enough to repeat',
        'What did not, without blaming a person for a process',
        'The one change we will make next sprint, with an owner',
    ),
    'general': (
        'Purpose of the meeting in one sentence',
        'Decisions that need to be made today',
        'Who owns each action, and by when',
    ),
}


@tool(name='eng.schedule_engineering_meeting', title='Schedule an engineering meeting',
      description=('Prepare a calendar invitation for a planning, review, retro or '
                   'general engineering meeting, with an agenda shaped to its kind.'),
      group='Team Communication', agent_types=('engineering_manager',),
      integration='google_calendar', requires_approval=True, icon='fa-calendar-plus',
      capability='Schedule an engineering meeting',
      parameters=_obj(
          ('title', 'start_at'),
          title=_s('Meeting title.'),
          start_at=_s('Start, e.g. 2026-02-14 10:00.'),
          duration_minutes=_i('Length in minutes. Default 60.'),
          attendees=_a('Email addresses to invite.'),
          agenda=_s('Agenda. Generated from the kind when left blank.'),
          kind=_enum('Meeting kind.',
                     ('general', 'sprint_planning', 'review', 'retro'))))
def schedule_engineering_meeting(ctx, title, start_at, duration_minutes=60,
                                 attendees=None, agenda='', kind='general'):
    """Prepare a meeting invitation with a real agenda."""
    if not str(title or '').strip():
        message = 'The meeting needs a title.'
        return ToolResult(ok=False, error=message, text=message)

    moment = _as_datetime(start_at)
    if moment is None:
        message = (f'"{start_at}" is not a time I can read. Use a form like '
                   f'2026-02-14 10:00.')
        return ToolResult(ok=False, error=message, text=message)

    shape = kind if kind in _MEETING_AGENDAS else 'general'
    minutes = max(15, min(_as_int(duration_minutes) or 60, 480))
    invitees = _as_list(attendees)

    if str(agenda or '').strip():
        agenda_text = str(agenda).strip()
    else:
        agenda_text = '\n'.join(f'{position}. {line}' for position, line
                                in enumerate(_MEETING_AGENDAS[shape], start=1))

    return Proposal(
        title=f'{str(title).strip()[:120]} -- {moment:%d %b %Y %H:%M}',
        summary=(f'A {shape.replace("_", " ")} meeting of {minutes} minutes with '
                 f'{len(invitees)} invitee(s). The invitation is not sent until this '
                 f'is approved.'),
        payload={'title': str(title).strip()[:250],
                 'start_at': moment.isoformat(timespec='minutes'),
                 'duration_minutes': minutes, 'attendees': invitees,
                 'agenda': agenda_text, 'kind': shape},
        editable_fields=[
            editable('title', 'Title', 'text'),
            editable('start_at', 'Start', 'text'),
            editable('duration_minutes', 'Duration in minutes', 'number'),
            editable('agenda', 'Agenda', 'longtext'),
        ],
        integration_key='google_calendar')


@executor('eng.schedule_engineering_meeting')
def execute_schedule_engineering_meeting(action):
    """Create the approved calendar event and record it."""
    payload = dict(action.payload or {})
    moment = _as_datetime(payload.get('start_at')) or datetime.now()
    minutes = max(15, _as_int(payload.get('duration_minutes')) or 60)
    finish = moment + timedelta(minutes=minutes)

    result = _call('google_calendar', 'create_event',
                   title=payload.get('title', ''),
                   description=payload.get('agenda', ''),
                   start_at=moment.isoformat(timespec='minutes'),
                   end_at=finish.isoformat(timespec='minutes'),
                   attendees=payload.get('attendees') or [])

    event = CalendarEvent.objects.create(
        title=payload.get('title', '')[:250], description=payload.get('agenda', ''),
        start_at=_aware(moment), end_at=_aware(finish), duration_minutes=minutes,
        attendees=payload.get('attendees') or [],
        meeting_link=str(_first(result.data, 'meeting_link', 'hangout_link', 'url'))[:400],
        status='simulated' if result.demo else ('scheduled' if result.ok else 'cancelled'),
        external_id=str(_first(result.data, 'id', 'event_id')),
        agent=action.agent, action=action)

    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The calendar would not accept the meeting: '
                               f'{result.error}')
    return ToolResult(
        ok=True, demo=result.demo,
        text=(('Simulated booking ' if result.demo else 'Booked ')
              + f'"{event.title}" for {moment:%d %b %Y %H:%M}, {minutes} minutes, '
              + f'{len(event.attendees)} invitee(s). Calendar record #{event.pk}.'),
        data={'event_id': event.pk, 'external_id': event.external_id,
              'demo': result.demo})


@tool(name='eng.upload_project_document', title='Upload a project document',
      description=('Prepare a document upload to the shared drive -- a plan, a report '
                   'or a specification.'),
      group='Team Communication', agent_types=('engineering_manager',),
      integration='google_drive', requires_approval=True, risk='medium',
      icon='fa-file-arrow-up', capability='Upload a project document',
      parameters=_obj(('name', 'content'),
                      name=_s('File name.'),
                      content=_s('The document text.'),
                      folder_id=_s('Drive folder id. Defaults to the integration setting.')))
def upload_project_document(ctx, name, content, folder_id=''):
    """Prepare a document upload. Nothing is written to the drive until approved."""
    if not str(name or '').strip() or not str(content or '').strip():
        message = 'An upload needs both a file name and some content.'
        return ToolResult(ok=False, error=message, text=message)

    body = str(content)
    return Proposal(
        title=f'Upload "{str(name).strip()[:120]}" to the shared drive',
        summary=(f'{len(body.splitlines())} line(s), {len(body)} characters. '
                 f'The document is not uploaded until this is approved.'),
        payload={'name': str(name).strip()[:200], 'content': body,
                 'folder_id': str(folder_id or '').strip()},
        editable_fields=[
            editable('name', 'File name', 'text'),
            editable('content', 'Content', 'longtext'),
            editable('folder_id', 'Folder id', 'text'),
        ],
        risk='medium', integration_key='google_drive')


@executor('eng.upload_project_document')
def execute_upload_project_document(action):
    """Upload the approved document."""
    payload = dict(action.payload or {})
    result = _call('google_drive', 'upload_file',
                   name=payload.get('name', ''), content=payload.get('content', ''),
                   folder_id=payload.get('folder_id', ''))
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The drive would not accept the document: '
                               f'{result.error}')
    file_id = str(_first(result.data, 'id', 'file_id'))
    url = str(_first(result.data, 'url', 'web_view_link', 'html_url'))
    return ToolResult(
        ok=True, demo=result.demo,
        text=(('Simulated uploading ' if result.demo else 'Uploaded ')
              + f'"{payload.get("name")}" to the shared drive'
              + (f' as file {file_id}' if file_id else '')
              + (f' -- {url}' if url else '') + '.'),
        data={'file_id': file_id, 'url': url, 'demo': result.demo})


# ===========================================================================
# GROUP: Tracking -- reads that reach out
# ===========================================================================
# These call a connected application but only read from it, so they act at
# once. Approval exists to protect the outside world from us; reading changes
# nothing out there, and making a manager wait for approval to look at the
# issue list would make the queue meaningless.


def _read_failure(what, result):
    return ToolResult(
        ok=False, error=result.error,
        text=(f'Could not read {what}: {result.error} '
              f'Check the integration on the Integrations page.'))


def default_repository():
    """The repository configured on the GitHub integration, or ''.

    WHY A TOOL NEEDS THIS

    A connector already falls back to its configured default when a tool
    passes an empty repository, so execution worked. What did not work was
    everything a person sees before execution: a read tool refused with
    "Which repository?" while a perfectly good default sat in the settings,
    and an issue proposal showed a reviewer a blank repository field, so the
    one question they most need answered -- where is this going -- had no
    answer on the approval card.

    So the default is resolved here, in the tool, rather than being left to
    the connector.
    """
    try:
        from ..integrations import get_integration
    except Exception:  # noqa: BLE001 -- a missing registry is not a failed tool
        return ''
    row = get_integration('github')
    if row is None:
        return ''
    return str((row.config or {}).get('default_repository') or '').strip()


def resolve_repository(repo='', work_item=None):
    """Which repository a GitHub call should use, in order of specificity.

    An explicit argument wins, then the project the work item belongs to,
    then the integration's own default.
    """
    named = str(repo or '').strip()
    if named:
        return named
    project = getattr(work_item, 'project', None)
    if project is not None and getattr(project, 'repository', ''):
        return str(project.repository).strip()
    return default_repository()


@tool(name='eng.read_repository_issues', title='Read repository issues',
      description='Read the issues on a GitHub repository. Read-only.',
      group='Tracking', agent_types=('engineering_manager',), integration='github',
      reads_only=True, icon='fa-github', capability='Read repository issues',
      parameters=_obj(('repo',), repo=_s("Repository as 'owner/name'."),
                      state=_enum('Which issues.', ('open', 'closed', 'all')),
                      limit=_i('Most issues to return. Default 20.')))
def read_repository_issues(ctx, repo, state='open', limit=20):
    """What GitHub currently holds, so a plan can be reconciled against it."""
    repo = resolve_repository(repo)
    if not repo:
        message = ("Which repository? Pass repo as 'owner/name', or set a default "
                   "repository on the GitHub integration so it does not have to be "
                   "named every time.")
        return ToolResult(ok=False, error=message, text=message)

    count = max(1, min(_as_int(limit) or 20, 100))
    result = _call('github', 'list_issues', repo=str(repo).strip(),
                   state=state or 'open', limit=count)
    if not result.ok:
        return _read_failure(f'the issues on {repo}', result)

    issues = result.data.get('issues') or result.data.get('items') or []
    if not issues:
        return ToolResult(ok=True, demo=result.demo,
                          text=f'{repo} has no {state} issues.',
                          data={'issues': [], 'count': 0})

    lines = []
    for issue in issues[:count]:
        if isinstance(issue, dict):
            lines.append(f'  #{_first(issue, "number", "id", default="?")} '
                         f'{_first(issue, "title", default="(no title)")} '
                         f'[{_first(issue, "state", default=state)}]')
        else:
            lines.append(f'  {issue}')

    return ToolResult(
        ok=True, demo=result.demo,
        text=f'{len(lines)} {state} issue(s) on {repo}:\n' + '\n'.join(lines),
        data={'issues': issues[:count], 'count': len(lines), 'demo': result.demo})


@tool(name='eng.read_pull_requests', title='Read pull requests',
      description='Read the pull requests on a GitHub repository. Read-only.',
      group='Tracking', agent_types=('engineering_manager',), integration='github',
      reads_only=True, icon='fa-code-pull-request', capability='Read pull requests',
      parameters=_obj(('repo',), repo=_s("Repository as 'owner/name'."),
                      state=_enum('Which pull requests.', ('open', 'closed', 'all')),
                      limit=_i('Most to return. Default 20.')))
def read_pull_requests(ctx, repo, state='open', limit=20):
    """Open pull requests, which are the work that is nearly done."""
    repo = resolve_repository(repo)
    if not repo:
        message = ("Which repository? Pass repo as 'owner/name', or set a default "
                   "repository on the GitHub integration so it does not have to be "
                   "named every time.")
        return ToolResult(ok=False, error=message, text=message)

    count = max(1, min(_as_int(limit) or 20, 100))
    result = _call('github', 'list_pull_requests', repo=str(repo).strip(),
                   state=state or 'open', limit=count)
    if not result.ok:
        return _read_failure(f'the pull requests on {repo}', result)

    requests = (result.data.get('pull_requests') or result.data.get('items')
                or result.data.get('pulls') or [])
    if not requests:
        return ToolResult(ok=True, demo=result.demo,
                          text=f'{repo} has no {state} pull requests.',
                          data={'pull_requests': [], 'count': 0})

    lines = []
    for entry in requests[:count]:
        if isinstance(entry, dict):
            lines.append(f'  #{_first(entry, "number", "id", default="?")} '
                         f'{_first(entry, "title", default="(no title)")} '
                         f'by {_first(entry, "user", "author", default="unknown")} '
                         f'[{_first(entry, "state", default=state)}]')
        else:
            lines.append(f'  {entry}')

    return ToolResult(
        ok=True, demo=result.demo,
        text=f'{len(lines)} {state} pull request(s) on {repo}:\n' + '\n'.join(lines),
        data={'pull_requests': requests[:count], 'count': len(lines),
              'demo': result.demo})


@tool(name='eng.read_jira_issues', title='Read Jira issues',
      description='Read issues from Jira by project or by JQL. Read-only.',
      group='Tracking', agent_types=('engineering_manager',), integration='jira',
      reads_only=True, icon='fa-jira', capability='Read Jira issues',
      parameters=_obj((), project=_s('Jira project key.'),
                      jql=_s('A JQL query, when a project key is not enough.'),
                      limit=_i('Most issues to return. Default 20.')))
def read_jira_issues(ctx, project='', jql='', limit=20):
    """What Jira holds, so the internal plan can be checked against it."""
    if not str(project or '').strip() and not str(jql or '').strip():
        message = ('Pass a Jira project key or a JQL query. eng.get_project shows the '
                   'project key recorded for a project.')
        return ToolResult(ok=False, error=message, text=message)

    count = max(1, min(_as_int(limit) or 20, 100))
    result = _call('jira', 'list_issues', project=str(project or '').strip(),
                   jql=str(jql or '').strip(), limit=count)
    if not result.ok:
        return _read_failure('the Jira issues', result)

    issues = result.data.get('issues') or result.data.get('items') or []
    scope = f'project {project}' if project else f'JQL "{jql}"'
    if not issues:
        return ToolResult(ok=True, demo=result.demo,
                          text=f'Jira returned no issues for {scope}.',
                          data={'issues': [], 'count': 0})

    lines = []
    for issue in issues[:count]:
        if isinstance(issue, dict):
            lines.append(f'  {_first(issue, "key", "id", default="?")} '
                         f'{_first(issue, "summary", "title", default="(no summary)")} '
                         f'[{_first(issue, "status", default="unknown")}]')
        else:
            lines.append(f'  {issue}')

    return ToolResult(
        ok=True, demo=result.demo,
        text=f'{len(lines)} Jira issue(s) for {scope}:\n' + '\n'.join(lines),
        data={'issues': issues[:count], 'count': len(lines), 'demo': result.demo})


@tool(name='eng.read_project_document', title='Read a project document',
      description='Read a document from the shared drive by file id. Read-only.',
      group='Tracking', agent_types=('engineering_manager',),
      integration='google_drive', reads_only=True, icon='fa-file-lines',
      capability='Read a project document',
      parameters=_obj(('file_id',), file_id=_s('Drive file id.')))
def read_project_document(ctx, file_id):
    """Read a specification or plan from the drive rather than guessing at it."""
    if not str(file_id or '').strip():
        message = 'Which document? Pass the drive file id.'
        return ToolResult(ok=False, error=message, text=message)

    result = _call('google_drive', 'read_file', file_id=str(file_id).strip())
    if not result.ok:
        return _read_failure(f'drive file {file_id}', result)

    content = str(_first(result.data, 'content', 'text', 'body', default=''))
    name = str(_first(result.data, 'name', 'title', default=file_id))
    if not content:
        return ToolResult(ok=True, demo=result.demo,
                          text=f'"{name}" was read but contained no text.',
                          data={'name': name, 'content': ''})

    return ToolResult(
        ok=True, demo=result.demo,
        text=(f'Read "{name}" ({len(content.splitlines())} lines, '
              f'{len(content)} characters):\n\n{content[:4000]}'
              + ('\n\n[truncated]' if len(content) > 4000 else '')),
        data={'name': name, 'content': content[:20000], 'demo': result.demo})
