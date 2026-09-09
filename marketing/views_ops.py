"""Operational views: the pages that show the work the six employees produce.

WHY THIS MODULE EXISTS SEPARATELY FROM views.py
-----------------------------------------------
views.py owns the original workspace -- dashboard, chat, approvals, users,
configurations, MCP. views_actions.py owns governance and views_platform.py
owns administration. This module owns the fifth thing: the operational record.
Recruitment, people, engineering, support, content and knowledge are the output
of the workforce, and without these screens the platform is a chat window with
a queue attached rather than a company operating system.

Keeping them apart is not tidiness. A change to the pipeline board must not be
able to break the approval queue, and a single views.py holding all of it would
be four thousand lines nobody could navigate.

WHAT EVERY PAGE HERE HAS IN COMMON
----------------------------------
    read       any signed-in user may read an operational page, because the
               workspace is shared and the whole value of the audit trail is
               that people can see what happened
    write      every control that changes a record is gated on
               roles.CAN_MANAGE_OPERATIONS, in the view AND in the template,
               and the JSON endpoint behind it re-checks the same permission
    queries    every list uses select_related/prefetch_related, because a board
               of sixty work items rendered naively is sixty queries
    empty      every page has an empty state that names the employee to ask,
               since a reader who sees nothing needs to know how work arrives

DEFENSIVE IMPORTS
-----------------
`marketing.tools` and a few `marketing.knowledge` helpers are imported
defensively. The tool registry imports every tool module on import, and those
modules are still being written; a page that lists capabilities should degrade
to the stored AgentCapability rows rather than 500 the whole site.
"""

import json

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import audit, roles, workforce
from .models import AIAgent, Conversation, MarketingCampaign
from .models_content import (CHANNEL_CHOICES, CHANNEL_LIMITS, EMAIL_SUBJECT_LIMIT,
                             AudienceSegment, CampaignBrief, ContentCalendarEntry,
                             ContentPiece, MarketingEmail, channel_label)
from .models_eng import CodeArtifact, Project, Sprint, SprintReport, WorkItem
from .models_hr import (Candidate, Employee, HRAnnouncement, Interview, JobOpening,
                        OnboardingTask, PerformanceReview)
from .models_knowledge import KnowledgeDocument, KnowledgeSource, ResearchReport
from .models_platform import (AgentMemory, Integration, OutboundMessage,
                              ProposedAction)
from .models_support import (CLOSED_STATUSES as TICKET_CLOSED_STATUSES,
                             OPEN_STATUSES as TICKET_OPEN_STATUSES,
                             Customer, SupportReport, SupportTicket, TicketTag)

try:  # The tool registry imports every tool module; treat it as optional.
    from . import tools as tool_registry
except Exception:  # noqa: BLE001 -- a half-written tool module must not 500 a page
    tool_registry = None

try:
    from . import knowledge
except Exception:  # noqa: BLE001
    knowledge = None


# ===========================================================================
# Helpers -- the same shapes views.py uses, so the two modules read alike
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


def _querystring(request, *drop):
    """The current query parameters minus `page` and anything named in `drop`.

    Handed to partials/_pagination.html so that paging keeps the filters. Built
    here rather than in the template because a template cannot delete a key
    from request.GET.
    """
    removed = {'page'} | set(drop)
    parts = []
    for key in request.GET:
        if key in removed:
            continue
        for value in request.GET.getlist(key):
            if value not in ('', None):
                parts.append(f'{key}={value}')
    return '&'.join(parts)


def _employee(agent_type):
    """The AIAgent row for one of the six employees, or None.

    Ordered so an active row wins over a deactivated one with the same type,
    because a page naming "ask People Operations" should link to the employee
    somebody can actually talk to.
    """
    return (AIAgent.objects
            .select_related('llm_model', 'llm_model__provider')
            .filter(agent_type=agent_type)
            .order_by('-is_active', 'pk')
            .first())


def _employee_name(agent_type):
    """The employee's display name, from the blueprint so it works with no rows.

    Empty states name the employee to ask. That sentence must still be right on
    a fresh database where no AIAgent row has been provisioned yet, which is
    exactly when a reader most needs telling how work arrives.
    """
    blueprint = workforce.blueprint_for(agent_type)
    if blueprint:
        return blueprint['name']
    return 'an AI employee'


def _ops_base(agent_type):
    """Context every operational page shares."""
    return {
        'employee': _employee(agent_type),
        'employee_name': _employee_name(agent_type),
        'employee_type': agent_type,
        'can_manage': False,          # replaced by the caller
    }


def _actions_for(subject_label, subject_id):
    """Proposed actions concerning one domain record, newest first.

    ProposedAction points at its subject with a soft label rather than a
    generic foreign key, so this is a filter on two columns rather than a join.
    """
    return list(ProposedAction.objects
                .select_related('agent', 'integration', 'decided_by')
                .filter(subject_label=subject_label, subject_id=subject_id)[:20])


# ===========================================================================
# 1. RECRUITMENT
# ===========================================================================

# The pipeline board's columns, in the order a candidate moves through them.
# 'withdrawn' is deliberately absent: it is an outcome rather than a stage, and
# a column for it would be permanently near-empty while stealing width from the
# stages somebody is actually working.
CANDIDATE_STAGES = ('new', 'screening', 'shortlisted', 'interviewing',
                    'offer', 'hired', 'rejected')


@login_required(login_url='login')
def recruitment_view(request):
    """The hiring pipeline: openings, candidates by stage, interviews ahead.

    The board is grouped in Python from one query rather than one query per
    column. Seven columns of ten candidates is otherwise seven queries for a
    page that could have been one.
    """
    can_manage = _requires(request, roles.CAN_MANAGE_OPERATIONS)

    status_filter = (request.GET.get('status') or 'all').strip()
    job_filter = (request.GET.get('job') or 'all').strip()
    search_query = (request.GET.get('q') or '').strip()

    openings = (JobOpening.objects
                .select_related('created_by_agent')
                .annotate(candidates_total=Count('candidates', distinct=True))
                .order_by('status', '-created_at'))

    candidates = (Candidate.objects
                  .select_related('job_opening')
                  .prefetch_related('evaluations'))

    if status_filter != 'all' and status_filter in dict(Candidate.STATUS_CHOICES):
        candidates = candidates.filter(status=status_filter)
    if job_filter not in ('all', ''):
        try:
            candidates = candidates.filter(job_opening_id=int(job_filter))
        except (TypeError, ValueError):
            job_filter = 'all'
    if search_query:
        candidates = candidates.filter(
            Q(full_name__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(current_title__icontains=search_query)
            | Q(current_company__icontains=search_query))

    # One pass over the filtered rows, bucketed by stage.
    buckets = {stage: [] for stage in CANDIDATE_STAGES}
    other = []
    for candidate in candidates[:400]:
        buckets.get(candidate.status, other).append(candidate)

    stage_labels = dict(Candidate.STATUS_CHOICES)
    board = [{'value': stage,
              'label': stage_labels.get(stage, stage.title()),
              'candidates': buckets[stage][:40],
              'count': len(buckets[stage]),
              'overflow': max(len(buckets[stage]) - 40, 0)}
             for stage in CANDIDATE_STAGES]

    now = timezone.now()
    week_ahead = now + timezone.timedelta(days=7)
    upcoming = (Interview.objects
                .select_related('candidate', 'job_opening', 'calendar_event')
                .filter(scheduled_at__gte=now,
                        status__in=('proposed', 'scheduled', 'rescheduled'))
                .order_by('scheduled_at')[:20])

    context = _ops_base('hr')
    context.update({
        'can_manage': can_manage,
        'openings': list(openings[:60]),
        'board': board,
        'other_candidates': other[:20],
        'upcoming_interviews': list(upcoming),
        'candidate_total': sum(len(rows) for rows in buckets.values()) + len(other),
        'open_roles': JobOpening.objects.filter(status='open').count(),
        'in_play': Candidate.objects.filter(
            status__in=('new', 'screening', 'shortlisted', 'interviewing', 'offer')).count(),
        'shortlisted': Candidate.objects.filter(
            status__in=('shortlisted', 'interviewing', 'offer')).count(),
        'interviews_this_week': Interview.objects.filter(
            scheduled_at__gte=now, scheduled_at__lte=week_ahead,
            status__in=('proposed', 'scheduled', 'rescheduled')).count(),
        'status_choices': Candidate.STATUS_CHOICES,
        'job_choices': list(JobOpening.objects.values_list('pk', 'title')),
        'status_filter': status_filter,
        'job_filter': job_filter,
        'search_query': search_query,
        'has_filters': bool(search_query) or status_filter != 'all' or job_filter != 'all',
    })
    return render(request, 'ops_recruitment.html', context)


@login_required(login_url='login')
def job_detail_view(request, pk):
    """One opening in full, with its ranked candidates and its requirements.

    The requirement weights are shown rather than hidden because they are what
    the scores were computed from. A ranking whose weighting is invisible is a
    ranking nobody can argue with, which is the opposite of useful.
    """
    opening = get_object_or_404(
        JobOpening.objects.select_related('created_by_agent', 'created_by'), pk=pk)

    candidates = list(Candidate.objects
                      .select_related('job_opening')
                      .prefetch_related('evaluations')
                      .filter(job_opening=opening)
                      .order_by('-score', 'full_name'))

    # requirements is a list of dicts; normalise for display without changing it.
    requirements = []
    for entry in (opening.requirements or []):
        if isinstance(entry, dict):
            requirements.append({
                'text': str(entry.get('text') or '').strip(),
                'weight': entry.get('weight'),
                'must_have': bool(entry.get('must_have')),
            })
        else:
            text = str(entry or '').strip()
            if text:
                requirements.append({'text': text, 'weight': None, 'must_have': False})

    context = _ops_base('hr')
    context.update({
        'can_manage': _requires(request, roles.CAN_MANAGE_OPERATIONS),
        'opening': opening,
        'requirements': requirements,
        'candidates': candidates,
        'interviews': list(Interview.objects
                           .select_related('candidate', 'calendar_event')
                           .filter(job_opening=opening)
                           .order_by('-scheduled_at', '-created_at')[:30]),
        'actions': _actions_for('marketing.jobopening', opening.pk),
        'status_choices': JobOpening.STATUS_CHOICES,
    })
    return render(request, 'ops_job_detail.html', context)


@login_required(login_url='login')
def candidate_detail_view(request, pk):
    """One candidate, and above all the evaluation that scored them.

    THE SCORE BREAKDOWN IS THE POINT OF THIS PAGE. A shortlist defended with a
    bare number is not defended at all; a table of requirement, score and the
    evidence quoted from the resume is. So the breakdown is rendered in full,
    row by row, rather than summarised into a rating.

    The emails panel reads both sides of the boundary this platform is built
    on: OutboundMessage rows are what was actually sent, ProposedAction rows
    are what is written and waiting for a person.
    """
    candidate = get_object_or_404(
        Candidate.objects.select_related('job_opening', 'created_by_agent'), pk=pk)

    evaluations = list(candidate.evaluations
                       .select_related('agent', 'job_opening')
                       .all()[:10])
    latest = evaluations[0] if evaluations else None

    breakdown = []
    if latest:
        for row in (latest.score_breakdown or []):
            if not isinstance(row, dict):
                breakdown.append({'requirement': str(row), 'score': None,
                                  'max': None, 'evidence': '', 'percent': 0})
                continue
            try:
                scored = float(row.get('score') or 0)
                maximum = float(row.get('max') or 0)
            except (TypeError, ValueError):
                scored, maximum = 0.0, 0.0
            breakdown.append({
                'requirement': row.get('requirement') or row.get('text') or 'Unnamed requirement',
                'score': row.get('score'),
                'max': row.get('max'),
                'evidence': row.get('evidence') or '',
                'percent': int(round(scored * 100 / maximum)) if maximum else 0,
            })

    # Anything sent or queued about this person. Matched on the subject
    # reference the tools stamp, and on the person's own name or address as
    # well, because an early draft can predate the subject columns and a
    # panel that silently omits a sent email is worse than no panel.
    match_sent = Q(action__subject_label='marketing.candidate',
                   action__subject_id=candidate.pk)
    match_queued = Q(subject_label='marketing.candidate', subject_id=candidate.pk)
    if candidate.email:
        match_sent |= Q(recipient__icontains=candidate.email)
    if candidate.full_name:
        match_sent |= Q(subject__icontains=candidate.full_name)
        match_queued |= Q(title__icontains=candidate.full_name)

    sent = list(OutboundMessage.objects
                .select_related('agent', 'integration', 'action')
                .filter(match_sent).distinct()[:20])
    queued = list(ProposedAction.objects
                  .select_related('agent', 'integration', 'decided_by')
                  .filter(match_queued).distinct()[:20])

    context = _ops_base('hr')
    context.update({
        'can_manage': _requires(request, roles.CAN_MANAGE_OPERATIONS),
        'candidate': candidate,
        'evaluations': evaluations,
        'evaluation': latest,
        'breakdown': breakdown,
        'interviews': list(candidate.interviews
                           .select_related('job_opening', 'calendar_event')
                           .order_by('round_number', '-created_at')),
        'sent_messages': sent,
        'queued_actions': queued,
        'status_choices': Candidate.STATUS_CHOICES,
        'skills': [str(skill) for skill in (candidate.skills or []) if str(skill).strip()],
    })
    return render(request, 'ops_candidate_detail.html', context)


# ===========================================================================
# 2. PEOPLE
# ===========================================================================

@login_required(login_url='login')
def people_view(request):
    """The employee directory, and everybody still working through onboarding.

    The onboarding panel is the operational half of this page. A checklist
    nobody can see the state of is a checklist that quietly stops being done,
    so the progress bar and the per-task status control are the point.
    """
    can_manage = _requires(request, roles.CAN_MANAGE_OPERATIONS)

    search_query = (request.GET.get('q') or '').strip()
    department_filter = (request.GET.get('department') or 'all').strip()
    status_filter = (request.GET.get('status') or 'all').strip()

    people = (Employee.objects
              .select_related('manager', 'account', 'from_candidate')
              .annotate(open_tasks=Count(
                  'onboarding_tasks',
                  filter=~Q(onboarding_tasks__status__in=('done', 'skipped')),
                  distinct=True)))

    if search_query:
        people = people.filter(
            Q(full_name__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(job_title__icontains=search_query))
    if department_filter != 'all':
        people = people.filter(department__iexact=department_filter)
    if status_filter != 'all' and status_filter in dict(Employee.STATUS_CHOICES):
        people = people.filter(employment_status=status_filter)

    paginator = Paginator(people, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    onboarding = list(Employee.objects
                      .filter(employment_status='onboarding')
                      .prefetch_related(Prefetch(
                          'onboarding_tasks',
                          queryset=OnboardingTask.objects.select_related('created_by_agent')))
                      .order_by('start_date', 'full_name')[:12])

    departments = sorted({row for row in Employee.objects
                          .exclude(department='')
                          .values_list('department', flat=True).distinct()})

    context = _ops_base('hr')
    context.update({
        'can_manage': can_manage,
        'page_obj': page_obj,
        'people': page_obj.object_list,
        'querystring': _querystring(request),
        'onboarding_people': onboarding,
        'departments': departments,
        'status_choices': Employee.STATUS_CHOICES,
        'task_status_choices': OnboardingTask.STATUS_CHOICES,
        'search_query': search_query,
        'department_filter': department_filter,
        'status_filter': status_filter,
        'has_filters': bool(search_query) or department_filter != 'all' or status_filter != 'all',
        'total_people': Employee.objects.count(),
        'active_people': Employee.objects.filter(employment_status='active').count(),
        'onboarding_count': Employee.objects.filter(employment_status='onboarding').count(),
        'open_task_count': OnboardingTask.objects.exclude(
            status__in=('done', 'skipped')).count(),
        'reviews': list(PerformanceReview.objects
                        .select_related('employee', 'created_by_agent')[:6]),
        'announcements': list(HRAnnouncement.objects
                              .select_related('created_by_agent')[:6]),
    })
    return render(request, 'ops_people.html', context)


# ===========================================================================
# 3. ENGINEERING
# ===========================================================================

# Board columns in delivery order. 'cancelled' is excluded for the same reason
# 'withdrawn' is excluded from the candidate board: descoped work is not a
# stage, and giving it a column would push the live stages off the screen.
WORK_ITEM_STAGES = ('backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done')

WORK_ITEM_ICONS = {
    'epic': 'fa-layer-group',
    'story': 'fa-book-open',
    'task': 'fa-square-check',
    'subtask': 'fa-turn-down',
    'bug': 'fa-bug',
    'spike': 'fa-flask',
    'chore': 'fa-broom',
}


def _decorate_items(items):
    """Attach the display extras a board cell needs, once per item.

    Done here rather than in the template because a template filter cannot
    reach a dictionary by a variable key, and doing it in the view keeps the
    icon vocabulary in one place.
    """
    for item in items:
        item.type_icon = WORK_ITEM_ICONS.get(item.item_type, 'fa-square-check')
    return items


@login_required(login_url='login')
def engineering_view(request):
    """Projects, the running sprint, and the work-item board.

    Overdue work is marked rather than reported politely. An item three days
    late shown as "in progress" is the normal way a slipping sprint stays
    invisible until the review, so the board says how many days.
    """
    can_manage = _requires(request, roles.CAN_MANAGE_OPERATIONS)

    project_filter = (request.GET.get('project') or 'all').strip()
    sprint_filter = (request.GET.get('sprint') or 'all').strip()
    status_filter = (request.GET.get('status') or 'all').strip()
    assignee_filter = (request.GET.get('assignee') or 'all').strip()
    search_query = (request.GET.get('q') or '').strip()

    items = (WorkItem.objects
             .select_related('project', 'sprint', 'assignee', 'created_by_agent')
             .prefetch_related('depends_on'))

    if project_filter != 'all':
        try:
            items = items.filter(project_id=int(project_filter))
        except (TypeError, ValueError):
            project_filter = 'all'
    if sprint_filter != 'all':
        try:
            items = items.filter(sprint_id=int(sprint_filter))
        except (TypeError, ValueError):
            sprint_filter = 'all'
    if status_filter != 'all' and status_filter in dict(WorkItem.STATUS_CHOICES):
        items = items.filter(status=status_filter)
    if assignee_filter != 'all':
        try:
            items = items.filter(assignee_id=int(assignee_filter))
        except (TypeError, ValueError):
            assignee_filter = 'all'
    if search_query:
        items = items.filter(
            Q(title__icontains=search_query)
            | Q(reference__icontains=search_query)
            | Q(description__icontains=search_query))

    buckets = {stage: [] for stage in WORK_ITEM_STAGES}
    for item in _decorate_items(list(items[:400])):
        if item.status in buckets:
            buckets[item.status].append(item)

    stage_labels = dict(WorkItem.STATUS_CHOICES)
    board = [{'value': stage,
              'label': stage_labels.get(stage, stage.title()),
              'items': buckets[stage][:40],
              'count': len(buckets[stage]),
              'overflow': max(len(buckets[stage]) - 40, 0),
              'points': sum(item.estimate_points or 0 for item in buckets[stage])}
             for stage in WORK_ITEM_STAGES]

    today = timezone.localdate()
    overdue = _decorate_items(list(
        WorkItem.objects
        .select_related('project', 'assignee')
        .filter(due_date__lt=today)
        .exclude(status__in=('done', 'cancelled'))
        .order_by('due_date')[:20]))

    blocked = _decorate_items(list(
        WorkItem.objects
        .select_related('project', 'assignee')
        .prefetch_related('depends_on')
        .filter(status='blocked')
        .order_by('-priority', 'due_date')[:20]))

    projects = list(Project.objects
                    .annotate(item_total=Count('work_items', distinct=True))
                    .order_by('status', 'name')[:24])

    active_sprints = list(Sprint.objects
                          .select_related('project')
                          .filter(status='active')
                          .order_by('end_date')[:6])

    context = _ops_base('engineering_manager')
    context.update({
        'can_manage': can_manage,
        'projects': projects,
        'active_sprints': active_sprints,
        'board': board,
        'overdue_items': overdue,
        'blocked_items': blocked,
        'active_projects': Project.objects.filter(status='active').count(),
        'open_items': WorkItem.objects.filter(
            status__in=WorkItem.OPEN_STATUSES).count(),
        'overdue_count': WorkItem.objects.filter(due_date__lt=today).exclude(
            status__in=('done', 'cancelled')).count(),
        'blocked_count': WorkItem.objects.filter(status='blocked').count(),
        'project_choices': list(Project.objects.values_list('pk', 'name')),
        'sprint_choices': list(Sprint.objects.values_list('pk', 'name')),
        'status_choices': WorkItem.STATUS_CHOICES,
        'assignee_choices': list(Employee.objects.values_list('pk', 'full_name')),
        'project_filter': project_filter,
        'sprint_filter': sprint_filter,
        'status_filter': status_filter,
        'assignee_filter': assignee_filter,
        'search_query': search_query,
        'has_filters': any([search_query, project_filter != 'all', sprint_filter != 'all',
                            status_filter != 'all', assignee_filter != 'all']),
        'reports': list(SprintReport.objects
                        .select_related('project', 'sprint', 'created_by_agent')[:6]),
        'developer_name': _employee_name('developer'),
    })
    return render(request, 'ops_engineering.html', context)


@login_required(login_url='login')
def project_detail_view(request, pk):
    """One project: its sprints, its work-item tree, its reports and artefacts.

    The tree is assembled in Python from a single query. Rendering parents and
    then querying children per parent is the obvious approach and also the one
    that turns a forty-item project into forty-one queries.
    """
    project = get_object_or_404(
        Project.objects.select_related('created_by_agent', 'created_by'), pk=pk)

    items = _decorate_items(list(
        WorkItem.objects
        .select_related('sprint', 'assignee', 'parent')
        .prefetch_related('depends_on')
        .filter(project=project)
        .order_by('sequence_hint', 'reference')))

    by_parent = {}
    roots = []
    for item in items:
        if item.parent_id:
            by_parent.setdefault(item.parent_id, []).append(item)
        else:
            roots.append(item)

    tree = []
    for root in roots:
        tree.append({'item': root, 'children': by_parent.get(root.pk, [])})
    # An item whose parent sits outside this project would otherwise vanish.
    orphans = [item for item in items
               if item.parent_id and item.parent_id not in {row['item'].pk for row in tree}]

    context = _ops_base('engineering_manager')
    context.update({
        'can_manage': _requires(request, roles.CAN_MANAGE_OPERATIONS),
        'project': project,
        'tech_stack': [str(entry) for entry in (project.tech_stack or []) if str(entry).strip()],
        'sprints': list(project.sprints.all()[:20]),
        'tree': tree,
        'orphans': orphans,
        'item_count': len(items),
        'reports': list(project.reports.select_related('sprint', 'created_by_agent')[:10]),
        'artifacts': list(CodeArtifact.objects
                          .select_related('work_item', 'created_by_agent')
                          .filter(project=project)[:20]),
        'status_choices': WorkItem.STATUS_CHOICES,
        'developer_name': _employee_name('developer'),
    })
    return render(request, 'ops_project_detail.html', context)


@login_required(login_url='login')
def work_item_detail_view(request, pk):
    """One work item in full, including both directions of its dependencies.

    `depends_on` and its reverse `blocks` are both shown because the two
    questions a stand-up actually asks are "what is holding this up" and "what
    is this holding up", and only one of them is answered by a single column.
    """
    item = get_object_or_404(
        WorkItem.objects.select_related(
            'project', 'sprint', 'assignee', 'parent', 'created_by_agent'), pk=pk)
    item.type_icon = WORK_ITEM_ICONS.get(item.item_type, 'fa-square-check')

    criteria = [str(line).strip() for line in (item.acceptance_criteria or [])
                if str(line).strip()]

    context = _ops_base('engineering_manager')
    context.update({
        'can_manage': _requires(request, roles.CAN_MANAGE_OPERATIONS),
        'item': item,
        'criteria': criteria,
        'labels': [str(label) for label in (item.labels or []) if str(label).strip()],
        'depends_on': _decorate_items(list(
            item.depends_on.select_related('project', 'assignee').all())),
        'blocks': _decorate_items(list(
            item.blocks.select_related('project', 'assignee').all())),
        'children': _decorate_items(list(
            item.children.select_related('assignee').order_by('sequence_hint'))),
        'comments': list(item.comments.select_related('author', 'agent').all()),
        'artifacts': list(item.artifacts.select_related('created_by_agent').all()[:20]),
        'reviews': list(item.code_reviews.select_related('created_by_agent').all()[:10]),
        'actions': _actions_for('marketing.workitem', item.pk),
        'status_choices': WorkItem.STATUS_CHOICES,
        'priority_choices': WorkItem.PRIORITY_CHOICES,
        'assignee_choices': list(Employee.objects.values_list('pk', 'full_name')),
        'developer_name': _employee_name('developer'),
    })
    return render(request, 'ops_work_item_detail.html', context)


# ===========================================================================
# 4. SUPPORT
# ===========================================================================

@login_required(login_url='login')
def support_view(request):
    """The ticket queue, with the tickets a person must see marked loudly.

    `needs_human` is the one field on this page that changes what anybody is
    allowed to do, so it is rendered as its own banner row rather than as
    another pill in a row of pills. A flag that has to be hunted for is a flag
    that will be missed, and the tickets carrying it are the ones where being
    missed matters most.
    """
    can_manage = _requires(request, roles.CAN_MANAGE_OPERATIONS)

    status_filter = (request.GET.get('status') or 'open').strip()
    priority_filter = (request.GET.get('priority') or 'all').strip()
    category_filter = (request.GET.get('category') or 'all').strip()
    customer_filter = (request.GET.get('customer') or 'all').strip()
    search_query = (request.GET.get('q') or '').strip()

    tickets = (SupportTicket.objects
               .select_related('customer', 'assignee', 'assigned_agent', 'duplicate_of')
               .prefetch_related('tags'))

    if status_filter == 'open':
        tickets = tickets.filter(status__in=TICKET_OPEN_STATUSES)
    elif status_filter == 'closed':
        tickets = tickets.filter(status__in=TICKET_CLOSED_STATUSES)
    elif status_filter != 'all' and status_filter in dict(SupportTicket.STATUS_CHOICES):
        tickets = tickets.filter(status=status_filter)

    if priority_filter != 'all' and priority_filter in dict(SupportTicket.PRIORITY_CHOICES):
        tickets = tickets.filter(priority=priority_filter)
    if category_filter != 'all' and category_filter in dict(SupportTicket.CATEGORY_CHOICES):
        tickets = tickets.filter(category=category_filter)
    if customer_filter != 'all':
        try:
            tickets = tickets.filter(customer_id=int(customer_filter))
        except (TypeError, ValueError):
            customer_filter = 'all'
    if search_query:
        tickets = tickets.filter(
            Q(subject__icontains=search_query)
            | Q(body__icontains=search_query)
            | Q(reference__icontains=search_query)
            | Q(contact_email__icontains=search_query))

    paginator = Paginator(tickets, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    now = timezone.now()
    week_ago = now - timezone.timedelta(days=7)
    open_queryset = SupportTicket.objects.filter(status__in=TICKET_OPEN_STATUSES)

    urgent_complaints = list(SupportTicket.objects
                             .select_related('customer')
                             .filter(status__in=TICKET_OPEN_STATUSES)
                             .filter(Q(priority='urgent') | Q(sentiment='angry')
                                     | Q(category='complaint'))
                             .order_by('-needs_human', '-created_at')[:12])

    # A recurring problem is a tag carried by more than one open ticket. Read
    # from the tag table rather than the subject line, because the same fault
    # described three ways is three signals nobody counts.
    recurring = list(TicketTag.objects
                     .annotate(open_count=Count(
                         'tickets', filter=Q(tickets__status__in=TICKET_OPEN_STATUSES),
                         distinct=True))
                     .filter(open_count__gte=2)
                     .order_by('-open_count')[:10])

    context = _ops_base('support')
    context.update({
        'can_manage': can_manage,
        'page_obj': page_obj,
        'tickets': page_obj.object_list,
        'querystring': _querystring(request),
        'open_tickets': open_queryset.count(),
        'urgent_tickets': open_queryset.filter(priority='urgent').count(),
        'needs_human_count': open_queryset.filter(needs_human=True).count(),
        'breaching_count': open_queryset.filter(sla_due_at__lt=now).count(),
        'resolved_this_week': SupportTicket.objects.filter(
            resolved_at__gte=week_ago).count(),
        'urgent_complaints': urgent_complaints,
        'recurring_tags': recurring,
        'reports': list(SupportReport.objects.select_related('created_by_agent')[:6]),
        'status_choices': SupportTicket.STATUS_CHOICES,
        'priority_choices': SupportTicket.PRIORITY_CHOICES,
        'category_choices': SupportTicket.CATEGORY_CHOICES,
        'customer_choices': list(Customer.objects.values_list('pk', 'name')[:200]),
        'status_filter': status_filter,
        'priority_filter': priority_filter,
        'category_filter': category_filter,
        'customer_filter': customer_filter,
        'search_query': search_query,
        'has_filters': any([search_query, status_filter != 'open', priority_filter != 'all',
                            category_filter != 'all', customer_filter != 'all']),
    })
    return render(request, 'ops_support.html', context)


@login_required(login_url='login')
def ticket_detail_view(request, pk):
    """One ticket: the whole thread, the triage record, and the resolution.

    The thread distinguishes an AI draft from a sent message, because that
    distinction is the entire approval workflow made visible on the record it
    concerns. A draft is text somebody wrote; a sent message is text the
    customer has read, and a page that renders them the same way loses the only
    thing worth knowing about either.
    """
    ticket = get_object_or_404(
        SupportTicket.objects.select_related(
            'customer', 'assignee', 'assigned_agent', 'duplicate_of', 'created_by_agent')
        .prefetch_related('tags'), pk=pk)

    thread = list(ticket.messages
                  .select_related('sent_message', 'sent_message__integration')
                  .all())

    other_tickets = []
    if ticket.customer_id:
        other_tickets = list(SupportTicket.objects
                             .filter(customer_id=ticket.customer_id)
                             .exclude(pk=ticket.pk)
                             .order_by('-created_at')[:10])

    context = _ops_base('support')
    context.update({
        'can_manage': _requires(request, roles.CAN_MANAGE_OPERATIONS),
        'ticket': ticket,
        'thread': thread,
        'draft_count': len([row for row in thread if row.is_ai_draft and not row.was_sent]),
        'urgency_signals': [str(signal) for signal in (ticket.urgency_signals or [])
                            if str(signal).strip()],
        'other_tickets': other_tickets,
        'duplicates': list(ticket.duplicates.all()[:10]),
        'actions': _actions_for('marketing.supportticket', ticket.pk),
        'status_choices': SupportTicket.STATUS_CHOICES,
        'priority_choices': SupportTicket.PRIORITY_CHOICES,
        'assignee_choices': list(User.objects.filter(is_active=True)
                                 .values_list('pk', 'username')[:200]),
    })
    return render(request, 'ops_ticket_detail.html', context)


# ===========================================================================
# 5. CONTENT
# ===========================================================================

def _calendar_weeks(entries, weeks=4):
    """A four-week grid of calendar entries, starting from this week's Monday.

    Returned as rows of seven day dictionaries so the template renders a real
    grid rather than a list pretending to be one. Entries are bucketed by date
    in one pass; a lookup per cell would be twenty-eight queries.
    """
    today = timezone.localdate()
    start = today - timezone.timedelta(days=today.weekday())

    by_date = {}
    for entry in entries:
        by_date.setdefault(entry.date, []).append(entry)

    rows = []
    for week in range(weeks):
        days = []
        for offset in range(7):
            day = start + timezone.timedelta(days=week * 7 + offset)
            days.append({
                'date': day,
                'is_today': day == today,
                'is_past': day < today,
                'entries': by_date.get(day, []),
            })
        rows.append({'start': days[0]['date'], 'days': days})
    return rows


@login_required(login_url='login')
def content_view(request):
    """Campaigns, copy, the calendar, the mailings and the audiences.

    Two things on this page are checks rather than decoration. `unsourced_claims`
    names the proof points nobody grounded in a document, and the character
    count is compared against the channel's own limit. Marketing copy is the
    one place in this platform where an invented figure reaches people who never
    asked the company anything, so provenance and length are shown on the card
    rather than buried in a detail view.
    """
    can_manage = _requires(request, roles.CAN_MANAGE_OPERATIONS)

    channel_filter = (request.GET.get('channel') or 'all').strip()
    status_filter = (request.GET.get('status') or 'all').strip()
    campaign_filter = (request.GET.get('campaign') or 'all').strip()

    pieces = (ContentPiece.objects
              .select_related('campaign', 'brief', 'created_by_agent', 'variant_of'))

    if channel_filter != 'all' and channel_filter in dict(CHANNEL_CHOICES):
        pieces = pieces.filter(channel=channel_filter)
    if status_filter != 'all' and status_filter in dict(ContentPiece.STATUS_CHOICES):
        pieces = pieces.filter(status=status_filter)
    if campaign_filter != 'all':
        try:
            pieces = pieces.filter(campaign_id=int(campaign_filter))
        except (TypeError, ValueError):
            campaign_filter = 'all'

    paginator = Paginator(pieces, 24)
    page_obj = paginator.get_page(request.GET.get('page'))

    # fits_channel returns (bool, message); unpack once here so the template
    # does not have to index a tuple, which Django's language cannot do.
    rows = []
    for piece in page_obj.object_list:
        fits, note = piece.fits_channel
        limits = CHANNEL_LIMITS.get(piece.channel or '') or {}
        rows.append({
            'piece': piece,
            'fits': fits,
            'note': note,
            'limit': limits.get('characters') or 0,
            'channel_label': channel_label(piece.channel),
        })

    campaigns = list(MarketingCampaign.objects
                     .select_related('assigned_agent')
                     .prefetch_related(Prefetch(
                         'briefs', queryset=CampaignBrief.objects.select_related(
                             'created_by_agent')))
                     .annotate(piece_count=Count('content_pieces', distinct=True))[:12])

    calendar_entries = list(ContentCalendarEntry.objects
                            .select_related('content', 'campaign')
                            .order_by('date', 'slot')[:400])

    context = _ops_base('marketing')
    context.update({
        'can_manage': can_manage,
        'campaigns': campaigns,
        'page_obj': page_obj,
        'rows': rows,
        'querystring': _querystring(request),
        'calendar_weeks': _calendar_weeks(calendar_entries),
        'emails': list(MarketingEmail.objects.select_related(
            'campaign', 'created_by_agent')[:15]),
        'segments': list(AudienceSegment.objects.select_related(
            'created_by_agent', 'created_by')[:12]),
        'email_subject_limit': EMAIL_SUBJECT_LIMIT,
        'channel_choices': CHANNEL_CHOICES,
        'status_choices': ContentPiece.STATUS_CHOICES,
        'campaign_choices': list(MarketingCampaign.objects.values_list('pk', 'name')),
        'channel_filter': channel_filter,
        'status_filter': status_filter,
        'campaign_filter': campaign_filter,
        'has_filters': any([channel_filter != 'all', status_filter != 'all',
                            campaign_filter != 'all']),
        'total_pieces': ContentPiece.objects.count(),
        'published_pieces': ContentPiece.objects.filter(status='published').count(),
        'pending_pieces': ContentPiece.objects.filter(status='pending').count(),
        'planned_entries': ContentCalendarEntry.objects.filter(status='planned').count(),
    })
    return render(request, 'ops_content.html', context)


# ===========================================================================
# 6. KNOWLEDGE
# ===========================================================================

@login_required(login_url='login')
def knowledge_view(request):
    """The company knowledge base, built to be used rather than admired.

    Search is live because that is how a knowledge base is actually consulted:
    somebody has a question now. The results carry the score and the `why`
    explanation from marketing/knowledge.py, since a search result that cannot
    say why it matched is a search result nobody trusts twice.
    """
    doc_type_filter = (request.GET.get('doc_type') or 'all').strip()
    department_filter = (request.GET.get('department') or 'all').strip()
    search_query = (request.GET.get('q') or '').strip()

    documents = (KnowledgeDocument.objects
                 .select_related('source', 'created_by', 'created_by_agent')
                 .annotate(chunks_total=Count('chunks', distinct=True),
                           citation_total=Count('citations', distinct=True))
                 .filter(is_active=True))

    if doc_type_filter != 'all' and doc_type_filter in dict(KnowledgeDocument.DOC_TYPE_CHOICES):
        documents = documents.filter(doc_type=doc_type_filter)
    if department_filter != 'all':
        documents = documents.filter(department__iexact=department_filter)
    if search_query:
        documents = documents.filter(
            Q(title__icontains=search_query) | Q(content__icontains=search_query))

    paginator = Paginator(documents, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    stats = {}
    if knowledge is not None and hasattr(knowledge, 'document_stats'):
        try:
            stats = knowledge.document_stats() or {}
        except Exception:  # noqa: BLE001 -- a stats panel must not break the page
            stats = {}

    type_labels = dict(KnowledgeDocument.DOC_TYPE_CHOICES)
    by_type = [{'value': key, 'label': type_labels.get(key, key.title()), 'count': count}
               for key, count in sorted((stats.get('by_type') or {}).items(),
                                        key=lambda pair: -pair[1])]

    departments = sorted({row for row in KnowledgeDocument.objects
                          .exclude(department='')
                          .values_list('department', flat=True).distinct()})

    context = _ops_base('research')
    context.update({
        'can_manage': _requires(request, roles.CAN_MANAGE_KNOWLEDGE),
        'can_add': _requires(request, roles.CAN_ADD_KNOWLEDGE),
        'page_obj': page_obj,
        'documents': page_obj.object_list,
        'querystring': _querystring(request),
        'stats': stats,
        'by_type': by_type,
        'sources': list(KnowledgeSource.objects
                        .annotate(indexed_documents=Count('documents', distinct=True))
                        .order_by('name')),
        'reports': list(ResearchReport.objects
                        .select_related('agent', 'requested_by')
                        .annotate(citation_total=Count('citations', distinct=True))[:10]),
        'doc_type_choices': KnowledgeDocument.DOC_TYPE_CHOICES,
        'departments': departments,
        'doc_type_filter': doc_type_filter,
        'department_filter': department_filter,
        'search_query': search_query,
        'has_filters': bool(search_query) or doc_type_filter != 'all' \
            or department_filter != 'all',
    })
    return render(request, 'ops_knowledge.html', context)


@login_required(login_url='login')
def document_detail_view(request, pk):
    """One document, with its chunk boundaries visible.

    The boundaries are shown because they explain the search. A reader who
    finds a passage returned for a query they thought was about something else
    can see that the passage is one indexed unit, with its own heading and its
    own keywords -- and that is a far better answer than "the ranking said so".
    """
    document = get_object_or_404(
        KnowledgeDocument.objects.select_related('source', 'created_by', 'created_by_agent'),
        pk=pk)

    chunks = list(document.chunks.order_by('ordinal'))
    citing = list(ResearchReport.objects
                  .select_related('agent')
                  .filter(citations__document=document)
                  .distinct()[:20])

    context = _ops_base('research')
    context.update({
        'can_manage': _requires(request, roles.CAN_MANAGE_KNOWLEDGE),
        'document': document,
        'chunks': chunks,
        'tags': [str(tag) for tag in (document.tags or []) if str(tag).strip()],
        'citing_reports': citing,
        'citations': list(document.citations.select_related('report')[:20]),
    })
    return render(request, 'ops_document_detail.html', context)


# ===========================================================================
# 7. EMPLOYEE PROFILE
# ===========================================================================

@login_required(login_url='login')
def employee_profile_view(request, agent_type):
    """One AI employee in full: what it is, what it can do, what it has done.

    GENERATED, NOT WRITTEN. The capability list comes from the tool registry,
    so an employee cannot advertise something the platform cannot do and cannot
    hide something it can. Where the registry will not import -- a tool module
    mid-edit -- the stored AgentCapability rows are used instead, which are
    written from the same registry by the provisioner.

    The connected-applications panel shows each integration's effective mode
    beside the capabilities that reach it, because "can this employee actually
    send that email" is the question a reader has, and the honest answer is
    sometimes "it will simulate it".
    """
    valid = dict(AIAgent.AGENT_TYPE_CHOICES)
    blueprint = workforce.blueprint_for(agent_type)
    if agent_type not in valid and blueprint is None:
        # A 404 rather than a redirect: there is no such employee, and silently
        # showing a different one would be worse than saying so.
        raise Http404(f'There is no employee of type "{agent_type}".')

    agent = _employee(agent_type)

    # -- capabilities --------------------------------------------------------
    groups = []
    capability_count = 0
    integration_keys = set()

    registry_groups = None
    if tool_registry is not None and hasattr(tool_registry, 'groups_for'):
        try:
            registry_groups = tool_registry.groups_for(agent_type)
        except Exception:  # noqa: BLE001
            registry_groups = None

    if registry_groups:
        for heading in sorted(registry_groups):
            entries = []
            for entry in registry_groups[heading]:
                entries.append({
                    'name': getattr(entry, 'capability', '') or entry.title,
                    'title': entry.title,
                    'tool_name': entry.name,
                    'description': entry.description,
                    'requires_approval': entry.requires_approval,
                    'integration_key': entry.integration or '',
                    'icon': getattr(entry, 'icon', 'fa-bolt'),
                    'reads_only': getattr(entry, 'reads_only', False),
                })
                if entry.integration:
                    integration_keys.add(entry.integration)
            capability_count += len(entries)
            groups.append({'heading': heading, 'entries': entries})
    elif agent is not None:
        stored = {}
        for row in agent.capabilities.order_by('group', 'display_order', 'name'):
            stored.setdefault(row.group or 'General', []).append({
                'name': row.name,
                'title': row.name,
                'tool_name': row.tool_name,
                'description': row.description,
                'requires_approval': row.requires_approval,
                'integration_key': row.integration_key or '',
                'icon': 'fa-bolt',
                'reads_only': False,
            })
            if row.integration_key:
                integration_keys.add(row.integration_key)
            capability_count += 1
        groups = [{'heading': heading, 'entries': entries}
                  for heading, entries in sorted(stored.items())]

    # The blueprint list is a display convenience; the registry is the truth,
    # so it is only consulted for keys the tools did not already name.
    integration_keys |= set(workforce.AGENT_INTEGRATIONS.get(agent_type, ()))
    integrations = list(Integration.objects
                        .filter(provider_key__in=sorted(integration_keys))
                        .order_by('category', 'name'))
    known = {row.provider_key for row in integrations}
    unknown = sorted(key for key in integration_keys
                     if key not in known and key != 'knowledge_base')

    # -- what it has done ----------------------------------------------------
    tasks, actions, memories, made, received, conversations = [], [], [], [], [], []
    if agent is not None:
        tasks = list(agent.tasks
                     .select_related('created_by', 'conversation')
                     .order_by('-created_at')[:12])
        actions = list(agent.proposed_actions
                       .select_related('integration', 'decided_by')[:12])
        memories = list(AgentMemory.objects
                        .select_related('conversation', 'created_by')
                        .filter(Q(agent=agent) | Q(scope='shared'))
                        .order_by('-importance', '-updated_at')[:15])
        made = list(agent.delegations_made.select_related('to_agent', 'task')[:10])
        received = list(agent.delegations_received.select_related('from_agent', 'task')[:10])
        conversations = list(Conversation.objects
                             .select_related('agent')
                             .filter(agent=agent, user=request.user)
                             .order_by('-updated_at')[:8])

    context = {
        'agent': agent,
        'agent_type': agent_type,
        'blueprint': blueprint,
        'employee_name': (agent.name if agent else _employee_name(agent_type)),
        'role_label': valid.get(agent_type, ''),
        'groups': groups,
        'capability_count': capability_count,
        'approval_count': sum(1 for group in groups for entry in group['entries']
                              if entry['requires_approval']),
        'integrations': integrations,
        'unknown_integrations': unknown,
        'uses_knowledge_base': 'knowledge_base' in integration_keys,
        'tasks': tasks,
        'actions': actions,
        'memories': memories,
        'delegations_made': made,
        'delegations_received': received,
        'conversations': conversations,
        'system_prompt': (agent.effective_system_prompt if agent
                          else (workforce.full_prompt(blueprint) if blueprint else '')),
        'can_view_actions': _requires(request, roles.CAN_VIEW_ACTIONS),
    }
    return render(request, 'employee_profile.html', context)


# ===========================================================================
# 8. JSON ENDPOINTS
#
# POST only, login required, CSRF enforced by Django's middleware, and every
# one that writes re-checks the permission the template used to decide whether
# to draw the control. Hiding a button is a convenience; this is the boundary.
# ===========================================================================

@login_required(login_url='login')
@require_POST
def api_knowledge_search(request):
    """Rank passages against a query and return them with their explanations.

    Read-only, so gated on CAN_CHAT rather than a write permission: a Viewer
    may read the knowledge base, which is the point of having one.
    """
    if knowledge is None or not hasattr(knowledge, 'search'):
        return _error('The knowledge index is unavailable on this deployment.', status=503)

    data = _json_body(request)
    query = (data.get('query') or '').strip()
    if not query:
        return _error('Enter something to search for.')

    doc_type = (data.get('doc_type') or '').strip()
    department = (data.get('department') or '').strip()
    if doc_type and doc_type not in dict(KnowledgeDocument.DOC_TYPE_CHOICES):
        doc_type = ''

    try:
        limit = min(max(int(data.get('limit') or 8), 1), 25)
    except (TypeError, ValueError):
        limit = 8

    try:
        hits = knowledge.search(query, limit=limit, doc_type=doc_type,
                                department=department)
    except Exception as error:  # noqa: BLE001 -- report, do not 500 the page
        return _error(f'The search could not be run: {error}', status=500)

    type_labels = dict(KnowledgeDocument.DOC_TYPE_CHOICES)
    best = max((hit.score for hit in hits), default=0) or 1

    results = []
    for hit in hits:
        document = hit.document
        results.append({
            'document_id': hit.document_id,
            'title': hit.title,
            'doc_type': getattr(document, 'doc_type', ''),
            'doc_type_label': type_labels.get(getattr(document, 'doc_type', ''), 'Other'),
            'department': getattr(document, 'department', '') or '',
            'heading': hit.heading,
            'snippet': hit.snippet,
            'matched_terms': list(hit.matched_terms or []),
            'why': hit.why,
            'score': round(hit.score, 3),
            'bar': int(round(min(hit.score / best, 1.0) * 100)),
            'url': (f'/operations/knowledge/{hit.document_id}/'
                    if hit.document_id else ''),
            'external_url': getattr(document, 'url', '') or '',
        })

    audit.log('knowledge.searched', category='knowledge', actor=request.user,
              message=f'Searched the knowledge base for "{query[:120]}"',
              detail={'query': query, 'results': len(results)})

    return JsonResponse({
        'status': 'success',
        'message': (f'{len(results)} passage{"s" if len(results) != 1 else ""} matched.'
                    if results else 'Nothing in the knowledge base matched that.'),
        'query': query,
        'results': results,
    })


@login_required(login_url='login')
@require_POST
def api_knowledge_index(request):
    """Add or replace one document and rebuild its passages.

    This is the route by which a person pastes in a policy. It goes through
    knowledge.index_document rather than creating the row directly, so the
    checksum, the word count and the chunking are done the same way whether a
    person or a synchronisation put the text there.
    """
    if not _requires(request, roles.CAN_ADD_KNOWLEDGE):
        return _forbidden(roles.CAN_ADD_KNOWLEDGE)
    if knowledge is None or not hasattr(knowledge, 'index_document'):
        return _error('The knowledge index is unavailable on this deployment.', status=503)

    data = _json_body(request)
    title = (data.get('title') or '').strip()
    content = (data.get('content') or '').strip()
    doc_type = (data.get('doc_type') or 'other').strip()
    department = (data.get('department') or '').strip()[:80]

    if not title:
        return _error('Give the document a title.')
    if len(content.split()) < 10:
        return _error('The document needs at least ten words to be worth indexing.')
    if doc_type not in dict(KnowledgeDocument.DOC_TYPE_CHOICES):
        return _error('That is not a document type this knowledge base recognises.')

    raw_tags = data.get('tags') or ''
    if isinstance(raw_tags, str):
        tags = [part.strip() for part in raw_tags.replace('\n', ',').split(',')
                if part.strip()]
    else:
        tags = [str(part).strip() for part in raw_tags if str(part).strip()]

    source = KnowledgeSource.objects.filter(kind='manual').order_by('pk').first()

    try:
        document = knowledge.index_document(
            title=title, content=content, doc_type=doc_type, department=department,
            tags=tags[:20], source=source, created_by=request.user)
    except Exception as error:  # noqa: BLE001
        return _error(f'The document could not be indexed: {error}', status=500)

    audit.log_record('knowledge.document_indexed', document, category='knowledge',
                     actor=request.user,
                     message=f'Indexed "{document.title}" ({document.word_count} words)')

    return JsonResponse({
        'status': 'success',
        'message': f'"{document.title}" was indexed into {document.chunk_count} passages.',
        'document_id': document.pk,
        'url': f'/operations/knowledge/{document.pk}/',
        'chunks': document.chunk_count,
        'word_count': document.word_count,
    })


@login_required(login_url='login')
@require_POST
def api_knowledge_sync(request):
    """Refresh one source: re-chunk what it holds and stamp the result.

    WHAT THIS DOES AND DOES NOT DO, since the button says "Sync now". Where the
    source's connected application is configured, the connector pulls; where it
    is not, there is nothing external to pull from and the honest outcome is to
    rebuild the passages of the documents already stored and say so. Reporting
    a successful synchronisation of a folder nobody has connected would be a
    lie the interface then repeats every day.
    """
    if not _requires(request, roles.CAN_MANAGE_KNOWLEDGE):
        return _forbidden(roles.CAN_MANAGE_KNOWLEDGE)

    data = _json_body(request)
    source = KnowledgeSource.objects.filter(pk=data.get('source_id')).first()
    if source is None:
        return _error('That knowledge source no longer exists.', status=404)
    if not source.is_enabled:
        return _error(f'{source.name} is switched off. Enable it before syncing.')

    integration = None
    if source.integration_key:
        integration = Integration.objects.filter(
            provider_key=source.integration_key).first()

    documents = list(KnowledgeDocument.objects.filter(source=source, is_active=True))
    rebuilt = 0
    if knowledge is not None and hasattr(knowledge, 'reindex'):
        for document in documents:
            try:
                knowledge.reindex(document)
                rebuilt += 1
            except Exception:  # noqa: BLE001 -- one bad document must not stop the run
                continue

    if integration is None and source.kind not in ('manual', 'upload'):
        message = (f'No connected application is registered for {source.get_kind_display()}, '
                   f'so nothing could be pulled. Rebuilt {rebuilt} stored document'
                   f'{"s" if rebuilt != 1 else ""}.')
    elif integration is not None and not integration.runs_live:
        message = (f'{integration.name} is in {integration.effective_mode} mode, so nothing '
                   f'was pulled from it. Rebuilt {rebuilt} stored document'
                   f'{"s" if rebuilt != 1 else ""}.')
    else:
        message = (f'Rebuilt the passages of {rebuilt} document'
                   f'{"s" if rebuilt != 1 else ""} in {source.name}.')

    source.document_count = len(documents)
    source.last_synced_at = timezone.now()
    source.last_sync_message = message[:400]
    source.save(update_fields=['document_count', 'last_synced_at', 'last_sync_message'])

    audit.log_record('knowledge.source_synced', source, category='knowledge',
                     actor=request.user, integration=integration, message=message)

    return JsonResponse({
        'status': 'success',
        'message': message,
        'source_id': source.pk,
        'document_count': source.document_count,
        'last_synced': timezone.localtime(source.last_synced_at).strftime('%d %b %Y at %H:%M'),
    })


# --------------------------------------------------------------------------
# The generic inline update
#
# WHY THE ALLOW-LIST IS WRITTEN OUT BY NAME
# One endpoint serves seven models because the alternative is seven endpoints
# that differ by one line. The safety of doing that rests entirely on never
# taking a field name from the request and handing it to setattr: a caller who
# could name the field could name `is_superuser` on a model that has one, or
# blank a column nobody meant to expose. So the model, the field and in the
# case of a choice field the value are all checked against a table declared
# here, and anything not in it is refused by name.
# --------------------------------------------------------------------------

_OPS_MODELS = {
    'candidate': Candidate,
    'job': JobOpening,
    'interview': Interview,
    'onboarding_task': OnboardingTask,
    'work_item': WorkItem,
    'ticket': SupportTicket,
    'content': ContentPiece,
}

_OPS_FIELDS = {
    'candidate': ('status',),
    'job': ('status',),
    'interview': ('status',),
    'onboarding_task': ('status',),
    'work_item': ('status', 'priority', 'assignee'),
    'ticket': ('status', 'priority', 'assignee'),
    'content': ('status',),
}

# Which table an assignee comes from, per model. Absent means the model has no
# assignee this endpoint will touch.
_OPS_ASSIGNEE = {
    'work_item': 'employee',
    'ticket': 'user',
}

_OPS_LABELS = {
    'candidate': 'Candidate', 'job': 'Job opening', 'interview': 'Interview',
    'onboarding_task': 'Onboarding task', 'work_item': 'Work item',
    'ticket': 'Ticket', 'content': 'Content piece',
}


def _apply_status_side_effects(key, obj):
    """Keep the timestamp columns honest when a status moves.

    A work item marked done with no `completed_at` breaks every velocity
    figure that reads it, and a resolved ticket with no `resolved_at` breaks
    the SLA report. Both are cheap to keep right at the moment of the change
    and expensive to reconstruct afterwards.
    """
    changed = []
    now = timezone.now()
    if key == 'work_item':
        if obj.status == 'done' and not obj.completed_at:
            obj.completed_at = now
            changed.append('completed_at')
        elif obj.status != 'done' and obj.completed_at:
            obj.completed_at = None
            changed.append('completed_at')
    elif key == 'onboarding_task':
        if obj.status in ('done', 'skipped') and not obj.completed_at:
            obj.completed_at = now
            changed.append('completed_at')
        elif obj.status not in ('done', 'skipped') and obj.completed_at:
            obj.completed_at = None
            changed.append('completed_at')
    elif key == 'ticket':
        if obj.status in TICKET_CLOSED_STATUSES and not obj.resolved_at:
            obj.resolved_at = now
            changed.append('resolved_at')
    return changed


@login_required(login_url='login')
@require_POST
def api_ops_update(request):
    """Change one allow-listed field on one operational record.

    Serves every inline control on the operational pages. Returns the new
    display value so the cell can be replaced in place rather than the page
    reloaded, which matters on a board where a reload loses the scroll
    position of six columns.
    """
    if not _requires(request, roles.CAN_MANAGE_OPERATIONS):
        return _forbidden(roles.CAN_MANAGE_OPERATIONS)

    data = _json_body(request)
    key = (data.get('model') or '').strip()
    field = (data.get('field') or '').strip()

    model = _OPS_MODELS.get(key)
    if model is None:
        return _error(f'"{key or "nothing"}" is not a record this endpoint may change.')
    if field not in _OPS_FIELDS[key]:
        return _error(f'The field "{field or "nothing"}" may not be changed on a '
                      f'{_OPS_LABELS[key].lower()}.')

    try:
        record_id = int(data.get('id'))
    except (TypeError, ValueError):
        return _error('A record id is required.')

    obj = model.objects.filter(pk=record_id).first()
    if obj is None:
        return _error(f'That {_OPS_LABELS[key].lower()} no longer exists.', status=404)

    value = data.get('value')
    updated = []

    if field == 'status':
        choices = dict(model.STATUS_CHOICES)
        if value not in choices:
            return _error(f'"{value}" is not a status a {_OPS_LABELS[key].lower()} may hold.')
        was = obj.get_status_display()
        obj.status = value
        updated = ['status'] + _apply_status_side_effects(key, obj)
        obj.save(update_fields=updated)
        display = obj.get_status_display()
        note = f'Status changed from {was} to {display}'

    elif field == 'priority':
        choices = dict(model.PRIORITY_CHOICES)
        if value not in choices:
            return _error(f'"{value}" is not a priority a {_OPS_LABELS[key].lower()} may hold.')
        was = obj.get_priority_display()
        obj.priority = value
        obj.save(update_fields=['priority'])
        display = obj.get_priority_display()
        note = f'Priority changed from {was} to {display}'

    else:  # field == 'assignee', the only remaining allow-listed name
        table = _OPS_ASSIGNEE.get(key)
        if table is None:
            return _error(f'A {_OPS_LABELS[key].lower()} has no assignee.')

        if value in ('', None, 'none'):
            obj.assignee = None
            obj.save(update_fields=['assignee'])
            display = 'Unassigned'
        else:
            try:
                target_id = int(value)
            except (TypeError, ValueError):
                return _error('Choose a person, or clear the assignment.')
            if table == 'employee':
                target = Employee.objects.filter(pk=target_id).first()
                if target is None:
                    return _error('That employee is not on file.', status=404)
                obj.assignee = target
                obj.save(update_fields=['assignee'])
                display = target.full_name
            else:
                target = User.objects.filter(pk=target_id, is_active=True).first()
                if target is None:
                    return _error('That account is not active.', status=404)
                obj.assignee = target
                obj.save(update_fields=['assignee'])
                display = target.get_username()
        note = f'Assigned to {display}'

    audit.log_record(f'ops.{key}_{field}_changed', obj, category='data',
                     actor=request.user, message=note,
                     detail={'model': key, 'field': field, 'value': value,
                             'fields_written': updated or [field]})

    return JsonResponse({
        'status': 'success',
        'message': f'{_OPS_LABELS[key]} {obj.pk}: {note.lower()}.',
        'model': key,
        'id': obj.pk,
        'field': field,
        'value': value,
        'display': display,
    })
