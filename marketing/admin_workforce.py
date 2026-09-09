"""Django admin registrations for the workforce, domain and platform models.

WHY THIS FILE IS SEPARATE FROM admin.py
---------------------------------------
marketing/admin.py registers the original seventeen models and carries the
flagship ``AIAgentAdmin``. The platform then grew to sixty models across six
sibling model modules, and putting all sixty registrations in one file would
produce fifteen hundred lines that nobody can navigate. The split follows the
one already made in the model layer: models.py holds the original schema and
models_platform, models_hr, models_eng, models_support, models_knowledge and
models_content hold the rest. This module is the admin half of that same
division, and admin.py imports it at the very bottom.

WHAT THE ADMIN IS FOR HERE
--------------------------
The Django admin is the safety net for the end user. Whatever a page in the
application does not yet let somebody edit, the admin must -- so every model
is registered, every list is scannable without opening a row, and every
governed operation goes through the same pipeline the application itself uses.
That last point matters most on ``ProposedActionAdmin``: approving a row is
not setting ``status = 'approved'``, it is calling ``approvals.approve``,
because the pipeline is what executes the action, writes the audit trail and
records the outcome. An admin action that wrote the column directly would
leave an approved row that never happened.

HOUSE STYLE, FOLLOWED FROM admin.py
-----------------------------------
``@admin.register`` decorators; ``list_display`` built from ``@admin.display``
methods that render through ``format_html``; ``list_filter`` and
``search_fields`` on everything; ``autocomplete_fields`` for foreign keys
whose target admin declares ``search_fields``; ``readonly_fields`` for
computed summaries rather than stored columns; ``date_hierarchy`` where there
is a date worth navigating; ``get_queryset`` with ``select_related`` wherever
a column reaches through a relation; and inlines that report rather than edit
where the underlying record is append-only.

TWO RULES THAT ARE NOT NEGOTIABLE
---------------------------------
No secret is ever rendered. ``Integration.secrets`` is excluded from the edit
form and summarised as ``(set)`` or ``(not set)``, and a ``SystemSetting``
marked ``is_secret`` is masked the same way, including inside a read-only
display method.

No JSON column is dumped into a list column. A JSONField in ``list_display``
is summarised as a count -- "8 requirements", "3 findings" -- because a raw
dictionary in a table cell is unreadable and tells a reader nothing they came
to the page for.
"""

import json

from django.contrib import admin, messages
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .models import (ActionAuditTrail, AgentCapability, AgentDelegation,
                     AgentMemory, AgentTask, AudienceSegment, AuditEvent,
                     CalendarEvent, CampaignBrief, Candidate,
                     CandidateEvaluation, CodeArtifact, CodeReview,
                     ContentCalendarEntry, ContentPiece, Customer,
                     DocumentChunk, Employee, ExternalIssue, HRAnnouncement,
                     Integration, Interview, JobOpening, KnowledgeDocument,
                     KnowledgeSource, MarketingEmail, OnboardingTask,
                     OrchestratorDecision, OutboundMessage, PerformanceReview,
                     Project,
                     ProposedAction, ResearchCitation, ResearchReport, Sprint,
                     SprintReport, SupportReport, SupportTicket, SystemSetting,
                     TicketMessage, TicketTag, WorkItem, WorkItemComment)
from .models_content import CHANNEL_LIMITS

# ===========================================================================
# Rendering helpers
#
# Every fragment below goes through format_html, so a value that came from a
# person or from a language model is escaped on the way in. Nothing in this
# module concatenates markup and nothing marks user-supplied text safe --
# mark_safe is used only for constant fragments, which is the same allowance
# admin.py already makes for itself.
# ===========================================================================

_GREY = '#64748b'
_FAINT = '#94a3b8'


def _badge(label, colour):
    """A filled status badge, for the column a reader scans first."""
    return format_html(
        '<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        'background:{};color:#fff;font-size:11px;font-weight:600;'
        'white-space:nowrap;">{}</span>', colour, label)


def _pill(label, colour):
    """An outlined pill, used where a filled badge would shout."""
    return format_html(
        '<span style="display:inline-block;padding:1px 7px;border-radius:9px;'
        'border:1px solid {};color:{};font-size:11px;font-weight:600;'
        'white-space:nowrap;">{}</span>', colour, colour, label)


def _icon(name, colour):
    """An icon name shown in its own colour, with a matching dot.

    The admin does not load the icon font the application uses, so the name is
    rendered as text beside a coloured dot rather than as a glyph that would
    come out as an empty box.
    """
    return format_html(
        '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;'
        'background:{};vertical-align:middle;"></span> '
        '<code style="color:{};font-size:11px;">{}</code>',
        colour, colour, name or 'none')


def _avatar(initials, colour):
    """A coloured monogram, so a list of people is scannable at a glance."""
    return format_html(
        '<span style="display:inline-block;width:28px;height:28px;border-radius:50%;'
        'background:{};color:#fff;text-align:center;line-height:28px;'
        'font-weight:700;font-size:11px;">{}</span>', colour, initials)


def _colour(mapping, key, default=_GREY):
    return mapping.get(key or '', default)


def _plain(value, limit=200):
    """One value said in plain text, short enough for a table cell."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, indent=None, default=str, ensure_ascii=False)
    else:
        text = str(value)
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + '...'


def _count_label(value, singular, plural=None):
    """A JSON collection said as a count, never dumped into a list column."""
    plural = plural or f'{singular}s'
    if isinstance(value, (dict, list, tuple)):
        total = len(value)
    elif value in (None, ''):
        total = 0
    else:
        total = 1
    return f"{total} {singular if total == 1 else plural}"


def _json_block(value, empty='Nothing was recorded.'):
    """A JSON structure pretty-printed into a read-only panel."""
    if value in (None, '', {}, []):
        return empty
    try:
        text = json.dumps(value, indent=2, default=str, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    return format_html(
        '<pre style="white-space:pre-wrap;max-width:760px;max-height:420px;'
        'overflow:auto;background:#f8fafc;padding:10px;border-radius:6px;'
        'border:1px solid #e2e8f0;font-size:12px;">{}</pre>', text)


def _key_value_table(rows, empty='Nothing was recorded.'):
    """Rows of (label, value) as a compact read-only table."""
    rows = [(str(key), _plain(value, 600)) for key, value in rows]
    if not rows:
        return empty
    body = format_html_join(
        '',
        '<tr><th style="text-align:left;padding:3px 12px 3px 0;vertical-align:top;'
        'font-weight:600;color:#334155;white-space:nowrap;">{}</th>'
        '<td style="padding:3px 0;vertical-align:top;color:#0f172a;">{}</td></tr>',
        rows)
    return format_html(
        '<table style="border-collapse:collapse;font-size:12px;max-width:760px;">'
        '{}</table>', body)


def _text_panel(text, empty='Nothing was recorded.', limit=4000):
    """A block of prose in a bordered panel, escaped and wrapped."""
    text = (text or '').strip()
    if not text:
        return empty
    clipped = text[:limit] + ('...' if len(text) > limit else '')
    return format_html(
        '<pre style="white-space:pre-wrap;max-width:760px;max-height:420px;'
        'overflow:auto;background:#f8fafc;padding:10px;border-radius:6px;'
        'border:1px solid #e2e8f0;font-size:12px;">{}</pre>', clipped)


def _user_link(user):
    """A link into the auth admin, or a dash when the row has no user."""
    if user is None:
        return mark_safe('<span style="color:#94a3b8;">nobody</span>')
    url = reverse('admin:auth_user_change', args=[user.pk])
    return format_html('<a href="{}">{}</a>', url, user.get_username())


# ===========================================================================
# Defensive imports of the operational modules
#
# The admin actions below drive the same pipelines the application uses. Those
# modules import connectors, the tool registry and the language-model client,
# any of which can fail to import in a partly configured checkout -- and an
# admin site that will not start is a worse outcome than an admin action that
# reports it cannot run. So each one is imported at call time inside a try,
# and the failure is reported to the person who pressed the button.
# ===========================================================================

_PIPELINE_UNAVAILABLE = (
    'The approval pipeline (marketing/approvals.py) could not be imported, so '
    'nothing was decided. Approving a row by hand is deliberately not offered: '
    'the pipeline is what executes the action and writes the audit trail.')

_INTEGRATIONS_UNAVAILABLE = (
    'The integration layer (marketing/integrations) could not be imported, so '
    'no connection was tested.')

_KNOWLEDGE_UNAVAILABLE = (
    'The knowledge indexer (marketing/knowledge.py) could not be imported, so '
    'nothing was re-indexed.')


def _approvals():
    """The approval pipeline, or None when it cannot be imported."""
    try:
        from . import approvals
    except Exception:  # noqa: BLE001 -- any import failure must not break the admin
        return None
    return approvals


def _integrations():
    """The integration layer, or None when it cannot be imported."""
    try:
        from . import integrations
    except Exception:  # noqa: BLE001
        return None
    return integrations


def _knowledge():
    """The knowledge indexer, or None when it cannot be imported."""
    try:
        from . import knowledge
    except Exception:  # noqa: BLE001
        return None
    return knowledge


# ===========================================================================
# Colour vocabularies
#
# One mapping per set of choices, so the colour a status is shown in is decided
# once. A reader who learns that amber means "waiting on a person" on the
# approval queue reads the same amber the same way on the ticket list.
# ===========================================================================

ACTION_STATUS_COLOURS = {
    'pending': '#b45309', 'approved': '#0369a1', 'executing': '#7c3aed',
    'executed': '#047857', 'failed': '#b91c1c', 'rejected': '#be123c',
    'cancelled': _GREY,
}
RISK_COLOURS = {'low': '#047857', 'medium': '#b45309', 'high': '#b91c1c'}

INTEGRATION_STATUS_COLOURS = {
    'unknown': _FAINT, 'connected': '#047857', 'demo': '#7c3aed',
    'degraded': '#b45309', 'failed': '#b91c1c', 'disabled': _GREY,
}
EFFECTIVE_MODE_COLOURS = {
    'live': '#047857', 'demo': '#7c3aed', 'unavailable': '#b91c1c',
    'disabled': _GREY,
}

AUDIT_STATUS_COLOURS = {
    'ok': '#047857', 'demo': '#7c3aed', 'failed': '#b91c1c',
    'denied': '#be123c', 'pending': '#b45309',
}

TASK_STATUS_COLOURS = {
    'queued': _FAINT, 'running': '#0369a1', 'waiting_approval': '#b45309',
    'blocked': '#be123c', 'done': '#047857', 'failed': '#b91c1c',
    'cancelled': _GREY,
}
PRIORITY_COLOURS = {
    'lowest': _FAINT, 'low': _FAINT, 'normal': _GREY, 'medium': _GREY,
    'high': '#b45309', 'urgent': '#b91c1c', 'critical': '#b91c1c',
}

TICKET_STATUS_COLOURS = {
    'new': '#0369a1', 'open': '#0369a1', 'pending': '#b45309',
    'waiting_customer': '#7c3aed', 'escalated': '#b91c1c',
    'resolved': '#047857', 'closed': _GREY, 'reopened': '#be123c',
}
SENTIMENT_COLOURS = {
    'positive': '#047857', 'neutral': _GREY, 'negative': '#b45309',
    'angry': '#b91c1c',
}

CANDIDATE_STATUS_COLOURS = {
    'new': '#0369a1', 'screening': '#0369a1', 'screened': '#7c3aed',
    'shortlisted': '#047857', 'interviewing': '#7c3aed', 'offered': '#047857',
    'hired': '#047857', 'rejected': '#be123c', 'withdrawn': _GREY,
    'on_hold': '#b45309',
}
BAND_COLOURS = {
    'strong': '#047857', 'possible': '#b45309', 'weak': '#b91c1c',
    'unscored': _FAINT,
}

OPENING_STATUS_COLOURS = {
    'draft': _FAINT, 'open': '#047857', 'paused': '#b45309',
    'closed': _GREY, 'filled': '#0369a1',
}

WORK_STATUS_COLOURS = {
    'backlog': _FAINT, 'todo': '#0369a1', 'in_progress': '#7c3aed',
    'in_review': '#b45309', 'blocked': '#b91c1c', 'done': '#047857',
    'cancelled': _GREY,
}
WORK_TYPE_ICONS = {
    'epic': ('fa-layer-group', '#7c3aed'),
    'story': ('fa-bookmark', '#0369a1'),
    'task': ('fa-circle-check', _GREY),
    'subtask': ('fa-list-check', _FAINT),
    'bug': ('fa-bug', '#b91c1c'),
    'spike': ('fa-flask', '#b45309'),
    'chore': ('fa-broom', _GREY),
}

CONTENT_STATUS_COLOURS = {
    'draft': _FAINT, 'pending': '#b45309', 'approved': '#0369a1',
    'scheduled': '#7c3aed', 'published': '#047857', 'rejected': '#be123c',
}
CHANNEL_ICONS = {
    'linkedin': ('fa-linkedin', '#0a66c2'),
    'instagram': ('fa-instagram', '#c13584'),
    'twitter': ('fa-x-twitter', '#0f172a'),
    'facebook': ('fa-facebook', '#1877f2'),
    'blog': ('fa-pen-nib', '#7c3aed'),
    'email': ('fa-envelope', '#b45309'),
    'ad': ('fa-rectangle-ad', '#be123c'),
    'press': ('fa-newspaper', _GREY),
    'website': ('fa-globe', '#0369a1'),
}

GENERIC_STATUS_COLOURS = {
    'draft': _FAINT, 'template': _FAINT, 'planned': _FAINT,
    'planning': _FAINT, 'proposed': _FAINT, 'todo': '#0369a1',
    'queued': _FAINT, 'in_progress': '#7c3aed', 'in_collection': '#7c3aed',
    'active': '#047857', 'live': '#047857', 'open': '#047857',
    'ready': '#0369a1', 'approved': '#0369a1', 'scheduled': '#7c3aed',
    'booked': '#0369a1', 'sent': '#047857', 'sending': '#7c3aed',
    'published': '#047857', 'completed': '#047857', 'complete': '#047857',
    'done': '#047857', 'closed': _GREY, 'archived': _GREY,
    'skipped': _GREY, 'cancelled': _GREY, 'blocked': '#b91c1c',
    'paused': '#b45309', 'on_hold': '#b45309', 'pending': '#b45309',
    'shared': '#047857', 'failed': '#b91c1c', 'rejected': '#be123c',
    'declined': '#be123c', 'withdrawn': _GREY, 'simulated': '#7c3aed',
    'no_show': '#b91c1c', 'filled': '#0369a1', 'merged': '#047857',
    'reviewed': '#0369a1', 'discarded': _GREY,
}


def _generic_badge(obj, field='status'):
    """A status badge for a model whose statuses need no special vocabulary."""
    value = getattr(obj, field, '') or ''
    getter = getattr(obj, f'get_{field}_display', None)
    label = getter() if callable(getter) else value
    return _badge(label or 'unset', _colour(GENERIC_STATUS_COLOURS, value))


# ===========================================================================
# Shared inlines
# ===========================================================================

class ActionAuditTrailInline(admin.TabularInline):
    """The whole life of one proposed action, in order and uneditable.

    Append-only by design: the trail is the evidence that an action was
    proposed, edited and approved in that order, and an editable trail is not
    evidence of anything.
    """

    model = ActionAuditTrail
    extra = 0
    max_num = 0
    can_delete = False
    fields = ('created_at', 'event', 'actor', 'from_status', 'to_status',
              'note', 'change_summary')
    readonly_fields = fields
    verbose_name_plural = 'Audit trail'

    @admin.display(description='Changes')
    def change_summary(self, obj):
        return _count_label(obj.changes, 'field changed', 'fields changed')

    def has_add_permission(self, request, obj=None):
        return False


class TicketMessageInline(admin.TabularInline):
    """The conversation with the customer, oldest first."""

    model = TicketMessage
    extra = 0
    fields = ('created_at', 'direction', 'author_label', 'body', 'is_ai_draft',
              'sent_message')
    readonly_fields = ('created_at',)
    autocomplete_fields = ('sent_message',)
    verbose_name_plural = 'Thread'


class CandidateEvaluationInline(admin.TabularInline):
    """Every screening judgement made about this candidate, unedited.

    A score is a judgement made at a moment against a particular requirement
    set. Editing one later would make the earlier shortlist indefensible, so
    this inline reports and the tools write.
    """

    model = CandidateEvaluation
    extra = 0
    max_num = 0
    can_delete = False
    show_change_link = True
    fields = ('created_at', 'job_opening', 'agent', 'score', 'recommendation',
              'rationale')
    readonly_fields = fields
    verbose_name_plural = 'Screening evaluations'

    def has_add_permission(self, request, obj=None):
        return False


class InterviewInline(admin.TabularInline):
    """Interviews planned or held for this candidate."""

    model = Interview
    extra = 0
    fields = ('round_number', 'round_name', 'scheduled_at', 'mode', 'status',
              'outcome_score')
    show_change_link = True
    verbose_name_plural = 'Interviews'


class CandidateOnOpeningInline(admin.TabularInline):
    """Who has applied to this opening, and how they scored.

    Read-only because a candidate is a record in its own right with a
    screening history attached; editing one from inside the opening would
    invite changes that the evaluation trail cannot explain.
    """

    model = Candidate
    extra = 0
    max_num = 0
    can_delete = False
    show_change_link = True
    fields = ('full_name', 'current_title', 'status', 'score', 'source',
              'created_at')
    readonly_fields = fields
    verbose_name_plural = 'Candidates'

    def has_add_permission(self, request, obj=None):
        return False


class WorkItemCommentInline(admin.TabularInline):
    """Notes on this work item, from people and from AI employees."""

    model = WorkItemComment
    extra = 0
    fields = ('created_at', 'author', 'agent', 'body')
    readonly_fields = ('created_at',)
    autocomplete_fields = ('author', 'agent')
    verbose_name_plural = 'Comments'


class DocumentChunkInline(admin.TabularInline):
    """The passages this document was split into, as the indexer wrote them.

    Read-only because the passages are derived: editing one would make a
    citation quote text that never appeared in the document. Re-index instead,
    with the action on the document list.
    """

    model = DocumentChunk
    extra = 0
    max_num = 0
    can_delete = False
    fields = ('ordinal', 'heading', 'word_count', 'keyword_summary', 'text')
    readonly_fields = fields
    verbose_name_plural = 'Indexed passages'

    @admin.display(description='Keywords')
    def keyword_summary(self, obj):
        return _count_label(obj.keywords, 'keyword')

    def has_add_permission(self, request, obj=None):
        return False


# ===========================================================================
# FLAGSHIP 1: the approval queue
# ===========================================================================

@admin.register(ProposedAction)
class ProposedActionAdmin(admin.ModelAdmin):
    """The approval queue -- the one admin page with real authority.

    WHAT THIS PAGE IS FOR
    ---------------------
    A tool that would touch the outside world never performs its action. It
    writes a ProposedAction row with the complete argument set and stops. This
    page is where a person reads that argument set, sees what the employee
    originally proposed against what it says now, and decides.

    WHY THE ACTIONS GO THROUGH marketing/approvals.py
    -------------------------------------------------
    Nothing here sets ``status`` directly. ``approvals.approve`` stamps the
    decision in a short transaction, commits it, executes the action through
    the tool registry, records the outcome and writes the audit trail --
    in that order and for good reasons documented in that module. Writing the
    column from the admin would produce a row that says "executed" with
    nothing behind it, which is precisely the failure the governance rule
    exists to prevent. The pipeline reports per row, so a bulk approval that
    half succeeds says which half.

    The payload, its difference from the original, the execution result and
    the trail are all read-only here. A reviewer who needs to change a payload
    field does it on the Approvals page, which knows which fields the tool
    declared editable; the admin does not, and offering a free-text JSON edit
    of an argument set that is about to be sent to Slack would be worse than
    offering nothing.
    """

    # --- Change list -------------------------------------------------------
    list_display = ('status_badge', 'title', 'action_type', 'target_app_label',
                    'risk_pill', 'agent', 'edited_flag', 'age_column',
                    'requester')
    list_display_links = ('status_badge', 'title')
    list_filter = ('status', 'risk', 'action_type', 'integration', 'agent',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('title', 'summary', 'action_type')
    date_hierarchy = 'created_at'
    list_per_page = 25
    save_on_top = True
    empty_value_display = 'Not set'

    # --- Change form -------------------------------------------------------
    autocomplete_fields = ('agent', 'integration', 'task', 'conversation',
                           'source_message', 'requested_by', 'decided_by')
    readonly_fields = ('payload_table', 'payload_changes', 'original_payload',
                       'execution_block', 'trail_block', 'created_at',
                       'decided_at', 'executed_at', 'edit_count')
    inlines = [ActionAuditTrailInline]
    actions = ['action_approve', 'action_reject', 'action_retry']

    fieldsets = (
        ('What is being proposed', {
            'fields': ('title', 'action_type', 'summary', ('risk', 'status'),
                       'agent', 'integration'),
        }),
        ('The arguments execution will use', {
            'description': 'Read-only here. Payload fields are edited on the '
                           'Approvals page, which knows which keys the tool '
                           'declared editable.',
            'fields': ('payload_table', 'payload_changes', 'original_payload',
                       'edit_count'),
        }),
        ('Provenance', {
            'classes': ('collapse',),
            'fields': ('task', 'conversation', 'source_message', 'requested_by',
                       ('subject_label', 'subject_id', 'subject_display'),
                       'created_at'),
        }),
        ('Decision and execution', {
            'fields': (('decided_by', 'decided_at'), 'decision_reason',
                       ('executed_at', 'executed_in_demo'), 'execution_summary',
                       'execution_block'),
        }),
        ('Audit trail', {
            'classes': ('collapse',),
            'fields': ('trail_block',),
        }),
    )

    # --- Custom columns ----------------------------------------------------

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _badge(obj.get_status_display(),
                      _colour(ACTION_STATUS_COLOURS, obj.status))

    @admin.display(description='Risk', ordering='risk')
    def risk_pill(self, obj):
        # The stored display text carries a long explanation; the pill wants
        # only the level, so the first word is taken.
        level = obj.get_risk_display().split(' --')[0]
        return _pill(level, _colour(RISK_COLOURS, obj.risk))

    @admin.display(description='Target application', ordering='integration__name')
    def target_app_label(self, obj):
        return obj.target_app

    @admin.display(description='Edited', boolean=True, ordering='edit_count')
    def edited_flag(self, obj):
        return obj.was_edited

    @admin.display(description='Age', ordering='created_at')
    def age_column(self, obj):
        hours = obj.age_hours
        colour = '#b91c1c' if hours >= 48 else '#b45309' if hours >= 8 else _GREY
        return _pill(f'{hours} h', colour)

    @admin.display(description='Requested by', ordering='requested_by__username')
    def requester(self, obj):
        return _user_link(obj.requested_by)

    # --- Computed read-only fields ----------------------------------------

    @admin.display(description='Payload')
    def payload_table(self, obj):
        return _key_value_table(sorted((obj.payload or {}).items()),
                                empty='This action carries no arguments.')

    @admin.display(description='What a reviewer changed')
    def payload_changes(self, obj):
        rows = obj.payload_diff
        if not rows:
            return 'Nothing was changed. The payload is the employee\'s own version.'
        return format_html(
            '<table style="border-collapse:collapse;font-size:12px;max-width:760px;">'
            '<tr><th style="text-align:left;padding:3px 12px 3px 0;">Field</th>'
            '<th style="text-align:left;padding:3px 12px 3px 0;">Proposed</th>'
            '<th style="text-align:left;padding:3px 0;">Now</th></tr>{}</table>',
            format_html_join(
                '',
                '<tr><th style="text-align:left;padding:3px 12px 3px 0;'
                'vertical-align:top;font-weight:600;">{}</th>'
                '<td style="padding:3px 12px 3px 0;vertical-align:top;'
                'color:#b91c1c;text-decoration:line-through;">{}</td>'
                '<td style="padding:3px 0;vertical-align:top;color:#047857;">{}</td></tr>',
                ((key, _plain(was, 300), _plain(now, 300)) for key, was, now in rows)))

    @admin.display(description='Execution result')
    def execution_block(self, obj):
        return _json_block(obj.execution_result,
                           empty='This action has not been executed.')

    @admin.display(description='Audit trail')
    def trail_block(self, obj):
        entries = list(obj.trail.select_related('actor'))
        if not entries:
            return 'No trail entries were written.'
        return format_html_join(
            mark_safe('<br>'),
            '<code>{}</code> &middot; <strong>{}</strong> by {} &mdash; {}',
            ((entry.created_at.strftime('%d %b %Y %H:%M'),
              entry.get_event_display(),
              entry.actor.get_username() if entry.actor_id else 'system',
              _plain(entry.note, 160) or 'no note')
             for entry in entries))

    # --- Overrides ---------------------------------------------------------

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('agent', 'integration', 'requested_by',
                                'decided_by', 'task'))

    # --- Bulk actions ------------------------------------------------------

    def _run_pipeline(self, request, queryset, verb, call):
        """Apply one pipeline call per row and report each outcome separately.

        Deliberately not atomic across the set: approving twelve actions means
        twelve separate things reaching the outside world, and rolling back the
        eleven that worked because the twelfth failed would be both impossible
        and wrong.
        """
        pipeline = _approvals()
        if pipeline is None:
            self.message_user(request, _PIPELINE_UNAVAILABLE, level=messages.ERROR)
            return

        succeeded = 0
        for action in queryset:
            try:
                result = call(pipeline, action)
            except Exception as error:  # noqa: BLE001 -- report, never 500
                self.message_user(
                    request,
                    f'"{action.title}" could not be {verb}: {error}',
                    level=messages.ERROR)
                continue
            note = (result or {}).get('message') or 'No outcome was reported.'
            if (result or {}).get('ok'):
                succeeded += 1
                self.message_user(request, f'"{action.title}": {note}',
                                  level=messages.SUCCESS)
            else:
                self.message_user(request, f'"{action.title}": {note}',
                                  level=messages.WARNING)

        total = queryset.count()
        self.message_user(
            request,
            f'{succeeded} of {total} selected actions were {verb} successfully.',
            level=messages.SUCCESS if succeeded == total else messages.WARNING)

    @admin.action(description='Approve and execute the selected actions')
    def action_approve(self, request, queryset):
        self._run_pipeline(
            request, queryset, 'approved',
            lambda pipeline, action: pipeline.approve(
                action, request.user,
                reason='Approved from the Django admin.'))

    @admin.action(description='Reject the selected actions')
    def action_reject(self, request, queryset):
        self._run_pipeline(
            request, queryset, 'rejected',
            lambda pipeline, action: pipeline.reject(
                action, request.user,
                reason='Rejected from the Django admin.'))

    @admin.action(description='Retry the failed selected actions')
    def action_retry(self, request, queryset):
        self._run_pipeline(
            request, queryset.filter(status__in=('failed', 'executing')),
            'retried',
            lambda pipeline, action: pipeline.retry(action, request.user))


# ===========================================================================
# FLAGSHIP 2: connected applications
# ===========================================================================

@admin.register(Integration)
class IntegrationAdmin(admin.ModelAdmin):
    """One connected company application per row, configuration and all.

    WHAT THIS PAGE IS FOR
    ---------------------
    A connector in marketing/integrations supplies behaviour; this row supplies
    configuration. Nothing about a connection needs a code edit or an
    environment variable, so this page is where an administrator sees which
    applications are live, which are simulating, and which are enabled but
    missing a setting.

    WHY THE CREDENTIALS ARE NOT ON THIS FORM
    ----------------------------------------
    ``secrets`` is excluded from the edit form entirely and no display method
    renders a value from it. A credential is entered on the Integrations page
    or by an AI employee calling ``platform.configure_integration``; both
    write into the column without ever rendering it back, and the serialiser
    that feeds the browser replaces every value with a masked placeholder. The
    configuration summary below therefore reports only whether each secret is
    ``(set)`` or ``(not set)``, which is the whole of what somebody diagnosing
    a connection needs to know. Clearing a credential is done the same way it
    was set, on the Integrations page.
    """

    # --- Change list -------------------------------------------------------
    list_display = ('icon_swatch', 'name', 'category', 'status_badge',
                    'mode_pill', 'configured_flag', 'live_call_count',
                    'demo_call_count', 'last_used_at')
    list_display_links = ('icon_swatch', 'name')
    list_filter = ('category', 'mode', 'is_enabled', 'connection_status')
    search_fields = ('name', 'provider_key', 'description')
    list_per_page = 40
    save_on_top = True
    empty_value_display = 'Never'

    # --- Change form -------------------------------------------------------
    exclude = ('secrets',)
    autocomplete_fields = ('configured_by',)
    readonly_fields = ('configuration_summary', 'credential_summary',
                       'missing_summary', 'call_count', 'live_call_count',
                       'demo_call_count', 'last_used_at', 'last_checked_at',
                       'last_status_message', 'created_at', 'updated_at')
    actions = ['action_test_connections']

    fieldsets = (
        ('Application', {
            'fields': ('provider_key', 'name', 'description', 'category',
                       ('icon', 'color')),
        }),
        ('Behaviour', {
            'fields': ('is_enabled', 'mode'),
        }),
        ('Configuration', {
            'description': 'Credentials are entered on the Integrations page or '
                           'by an AI employee calling the configuration tool. '
                           'They are never shown here.',
            'fields': ('config', 'configuration_summary', 'credential_summary',
                       'missing_summary', 'configured_by'),
        }),
        ('Connection and usage', {
            'classes': ('collapse',),
            'fields': ('connection_status', 'last_status_message',
                       'last_checked_at',
                       ('call_count', 'live_call_count', 'demo_call_count'),
                       'last_used_at', ('created_at', 'updated_at')),
        }),
    )

    # --- Custom columns ----------------------------------------------------

    @admin.display(description='Icon', ordering='icon')
    def icon_swatch(self, obj):
        return _icon(obj.icon, obj.color)

    @admin.display(description='Connection', ordering='connection_status')
    def status_badge(self, obj):
        return _badge(obj.get_connection_status_display(),
                      _colour(INTEGRATION_STATUS_COLOURS, obj.connection_status))

    @admin.display(description='Effective mode')
    def mode_pill(self, obj):
        mode = obj.effective_mode
        return _pill(mode, _colour(EFFECTIVE_MODE_COLOURS, mode))

    @admin.display(description='Configured', boolean=True)
    def configured_flag(self, obj):
        return obj.is_configured

    # --- Computed read-only fields ----------------------------------------

    @admin.display(description='Settings in use')
    def configuration_summary(self, obj):
        """Every non-secret setting, as stored.

        ``config`` holds only what is safe to display: a Slack channel, a
        GitHub repository, a Drive folder id. Credentials live in ``secrets``
        and are reported by ``credential_summary`` as presence alone.
        """
        return _key_value_table(sorted((obj.config or {}).items()),
                                empty='No settings have been entered.')

    @admin.display(description='Credentials')
    def credential_summary(self, obj):
        """Which credentials exist. No value from ``secrets`` is ever read out."""
        stored = obj.secrets or {}
        if not stored:
            return 'No credential has been entered for this application.'
        rows = [(key, '(set)' if stored.get(key) not in (None, '', [], {}) else '(not set)')
                for key in sorted(stored)]
        return _key_value_table(rows)

    @admin.display(description='Still required')
    def missing_summary(self, obj):
        missing = obj.missing_settings
        if not missing:
            return 'Nothing is outstanding.'
        return format_html_join(mark_safe(', '), '<code>{}</code>',
                                ((name,) for name in missing))

    # --- Bulk actions ------------------------------------------------------

    @admin.action(description='Test the selected connections')
    def action_test_connections(self, request, queryset):
        layer = _integrations()
        if layer is None:
            self.message_user(request, _INTEGRATIONS_UNAVAILABLE,
                              level=messages.ERROR)
            return
        for integration in queryset:
            try:
                result = layer.probe(integration.provider_key)
            except Exception as error:  # noqa: BLE001
                self.message_user(request,
                                  f'{integration.name} could not be tested: {error}',
                                  level=messages.ERROR)
                continue
            note = f"{integration.name}: {result.get('message', 'no message')}"
            self.message_user(
                request, note,
                level=messages.SUCCESS if result.get('ok') else messages.WARNING)


# ===========================================================================
# FLAGSHIP 3: the audit log
# ===========================================================================

@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """The append-only history of everything the platform did.

    WHY EVERY WRITE PATH IS CLOSED
    ------------------------------
    ``has_add_permission`` and ``has_change_permission`` both return False, on
    purpose and permanently: an audit log somebody can edit is not an audit
    log. Entries are written by marketing/audit.py, which is the single place
    that decides the shape of an entry, and the only thing this page offers is
    reading -- with a date hierarchy, filters by category and outcome, and a
    search across the action name, the message and the object label, because
    "what happened to that candidate record on Tuesday" is the question this
    table exists to answer.

    Django's view permission still applies, so a staff member with it gets a
    read-only change form including the full ``detail`` payload.
    """

    list_display = ('created_at', 'actor_column', 'category', 'action',
                    'target_app', 'status_badge', 'message')
    list_filter = ('category', 'status', 'agent', 'integration',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('action', 'message', 'object_label')
    date_hierarchy = 'created_at'
    list_per_page = 50
    empty_value_display = '--'

    readonly_fields = ('created_at', 'actor', 'agent', 'category', 'action',
                       'message', 'status', 'target_app', 'object_label',
                       'object_ref', 'task', 'proposed_action', 'conversation',
                       'integration', 'detail_block', 'duration_ms')
    fieldsets = (
        ('What happened', {
            'fields': ('created_at', 'category', 'action', 'status', 'message',
                       'duration_ms'),
        }),
        ('Who did it', {
            'fields': ('actor', 'agent'),
        }),
        ('What it touched', {
            'fields': ('target_app', 'object_label', 'object_ref', 'integration',
                       'task', 'proposed_action', 'conversation'),
        }),
        ('Detail', {
            'fields': ('detail_block',),
        }),
    )

    @admin.display(description='Actor')
    def actor_column(self, obj):
        return obj.actor_label

    @admin.display(description='Outcome', ordering='status')
    def status_badge(self, obj):
        return _badge(obj.get_status_display(),
                      _colour(AUDIT_STATUS_COLOURS, obj.status))

    @admin.display(description='Recorded detail')
    def detail_block(self, obj):
        return _json_block(obj.detail, empty='No detail was recorded.')

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('actor', 'agent', 'integration', 'task',
                                'proposed_action'))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ===========================================================================
# FLAGSHIP 4: agent tasks
# ===========================================================================

@admin.register(AgentTask)
class AgentTaskAdmin(admin.ModelAdmin):
    """One unit of work an AI employee is doing, with its working record.

    WHAT THIS PAGE IS FOR
    ---------------------
    Every tool call happens inside a task, which is what makes the audit trail
    answerable: not "an email was drafted" but "an email was drafted while
    working on the task that came from this request". When somebody asks why
    an employee did something, the ``steps`` timeline below is the answer --
    one entry per tool call, in order, with the outcome of each.

    ``steps`` is read-only. It is a record of what happened, and a record that
    can be rewritten afterwards is not a record.
    """

    list_display = ('status_badge', 'title', 'agent', 'origin', 'priority_pill',
                    'tool_calls', 'duration_column')
    list_display_links = ('status_badge', 'title')
    list_filter = ('status', 'origin', 'priority', 'agent',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('title', 'description', 'result_summary', 'error_message',
                     'agent__name')
    date_hierarchy = 'created_at'
    list_per_page = 30
    save_on_top = True
    empty_value_display = '--'

    autocomplete_fields = ('agent', 'created_by', 'conversation',
                           'source_message', 'parent')
    readonly_fields = ('steps_timeline', 'tool_calls', 'tokens_used',
                       'created_at', 'started_at', 'completed_at')

    fieldsets = (
        ('The work', {
            'fields': ('title', 'description', ('status', 'priority'), 'origin',
                       'agent', 'parent'),
        }),
        ('Working record', {
            'description': 'Written by the runtime, one entry per tool call.',
            'fields': ('steps_timeline', ('tool_calls', 'tokens_used')),
        }),
        ('Outcome', {
            'fields': ('result_summary', 'error_message'),
        }),
        ('Provenance and timing', {
            'classes': ('collapse',),
            'fields': ('created_by', 'conversation', 'source_message',
                       ('due_at', 'started_at'), ('completed_at', 'created_at')),
        }),
    )

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _badge(obj.get_status_display(),
                      _colour(TASK_STATUS_COLOURS, obj.status))

    @admin.display(description='Priority', ordering='priority')
    def priority_pill(self, obj):
        return _pill(obj.get_priority_display(),
                     _colour(PRIORITY_COLOURS, obj.priority))

    @admin.display(description='Took', ordering='completed_at')
    def duration_column(self, obj):
        start = obj.started_at or obj.created_at
        end = obj.completed_at
        if not (start and end):
            return _pill('still open' if obj.is_open else 'not timed', _FAINT)
        seconds = max((end - start).total_seconds(), 0)
        if seconds < 60:
            return f'{round(seconds)} s'
        if seconds < 3600:
            return f'{round(seconds / 60, 1)} min'
        return f'{round(seconds / 3600, 1)} h'

    @admin.display(description='What the employee did')
    def steps_timeline(self, obj):
        steps = [entry for entry in (obj.steps or []) if isinstance(entry, dict)]
        if not steps:
            return 'No steps were recorded for this task.'
        return format_html_join(
            mark_safe('<br>'),
            '<code>{}</code> <strong>{}</strong> [{}] &mdash; {}',
            ((entry.get('at', ''), entry.get('label', 'step'),
              entry.get('outcome', 'ok'), _plain(entry.get('detail'), 300))
             for entry in steps))

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('agent', 'created_by', 'conversation', 'parent'))


# ===========================================================================
# FLAGSHIP 5: support tickets
# ===========================================================================

@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    """One customer request, from arrival to resolution.

    WHAT THIS PAGE IS FOR
    ---------------------
    The queue an on-call person works from. Status and priority are editable
    straight from the list, because reprioritising six tickets should not mean
    opening six pages, and the thread is on the change form so a reply can be
    read in context.

    THE TWO COLUMNS THAT MATTER MOST
    --------------------------------
    ``needs_human`` is the hard stop: when it is set, no tool will draft or
    propose a customer reply whatever it is asked to do, so the column shouts
    rather than whispers -- a ticket sitting in that state unnoticed is the
    one failure mode this whole subsystem is built to avoid. The SLA column
    reads ``is_breaching_sla`` rather than comparing dates here, so the page
    and the tools agree on what "breached" means.
    """

    list_display = ('reference', 'subject', 'customer_column', 'category',
                    'priority_pill', 'status_badge', 'sentiment_pill',
                    'human_flag', 'sla_flag', 'priority', 'status')
    list_display_links = ('reference', 'subject')
    list_editable = ('status', 'priority')
    list_filter = ('status', 'priority', 'category', 'needs_human', 'channel',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('reference', 'subject', 'body', 'contact_email',
                     'customer__name', 'customer__company')
    date_hierarchy = 'created_at'
    list_per_page = 30
    save_on_top = True
    empty_value_display = '--'

    autocomplete_fields = ('customer', 'assignee', 'assigned_agent',
                           'duplicate_of', 'created_by_agent')
    filter_horizontal = ('tags',)
    readonly_fields = ('reference', 'signal_summary', 'age_column',
                       'created_at', 'updated_at', 'first_response_at',
                       'resolved_at', 'reopened_count')
    inlines = [TicketMessageInline]
    actions = ['action_flag_for_human', 'action_mark_resolved']

    fieldsets = (
        ('The request', {
            'fields': ('reference', 'subject', 'body', 'channel',
                       ('customer', 'contact_email')),
        }),
        ('Triage', {
            'description': 'Category, priority and sentiment are set by keyword '
                           'rules; the signals below are the phrases that fired.',
            'fields': (('category', 'priority'), ('status', 'sentiment'),
                       'signal_summary', 'tags'),
        }),
        ('Who holds it', {
            'fields': ('assignee', 'assigned_agent', 'needs_human',
                       ('escalated_to', 'escalation_reason'), 'duplicate_of'),
        }),
        ('Resolution and service level', {
            'fields': ('resolution', 'sla_due_at',
                       ('first_response_at', 'resolved_at'),
                       ('age_column', 'reopened_count')),
        }),
        ('Elsewhere', {
            'classes': ('collapse',),
            'fields': (('external_reference', 'external_url'),
                       'source_message_id', 'created_by_agent',
                       ('created_at', 'updated_at')),
        }),
    )

    # --- Custom columns ----------------------------------------------------

    @admin.display(description='Customer', ordering='customer__name')
    def customer_column(self, obj):
        return obj.customer_label

    @admin.display(description='Urgency', ordering='priority')
    def priority_pill(self, obj):
        return _pill(obj.get_priority_display(),
                     _colour(PRIORITY_COLOURS, obj.priority))

    @admin.display(description='State', ordering='status')
    def status_badge(self, obj):
        return _badge(obj.get_status_display(),
                      _colour(TICKET_STATUS_COLOURS, obj.status))

    @admin.display(description='Mood', ordering='sentiment')
    def sentiment_pill(self, obj):
        return _pill(obj.get_sentiment_display(),
                     _colour(SENTIMENT_COLOURS, obj.sentiment))

    @admin.display(description='Handling', ordering='needs_human')
    def human_flag(self, obj):
        if obj.needs_human:
            return _badge('NEEDS A PERSON', '#be123c')
        return _pill('agent may reply', _FAINT)

    @admin.display(description='SLA', ordering='sla_due_at')
    def sla_flag(self, obj):
        if obj.is_breaching_sla:
            return _badge('BREACHED', '#b91c1c')
        if obj.sla_due_at:
            return _pill('on target', '#047857')
        return _pill('no target set', _FAINT)

    @admin.display(description='Open for')
    def age_column(self, obj):
        return obj.age_label

    @admin.display(description='Urgency signals')
    def signal_summary(self, obj):
        signals = [str(entry) for entry in (obj.urgency_signals or []) if entry]
        if not signals:
            return 'No urgency phrases were detected.'
        return format_html_join(mark_safe(' '), '{}',
                                ((_pill(text, '#b45309'),) for text in signals))

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('customer', 'assignee', 'assigned_agent',
                                'duplicate_of'))

    # --- Bulk actions ------------------------------------------------------

    @admin.action(description='Flag the selected tickets for a person')
    def action_flag_for_human(self, request, queryset):
        updated = queryset.update(needs_human=True)
        self.message_user(
            request,
            f'{updated} tickets are now flagged for a person. No tool will '
            f'draft or send a reply on them.',
            level=messages.WARNING)

    @admin.action(description='Mark the selected tickets resolved')
    def action_mark_resolved(self, request, queryset):
        from django.utils import timezone

        updated = queryset.exclude(status__in=('resolved', 'closed')).update(
            status='resolved', resolved_at=timezone.now())
        self.message_user(request, f'{updated} tickets were marked resolved.',
                          level=messages.SUCCESS)


# ===========================================================================
# FLAGSHIP 6: candidates
# ===========================================================================

@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    """One applicant, with every screening judgement ever made about them.

    WHAT THIS PAGE IS FOR
    ---------------------
    Working a shortlist. Status is editable from the list so twelve candidates
    can be moved through screening without opening twelve pages, and the score
    is shown in its band colour because a shortlist conversation is about
    "strong" and "possible" rather than about 74.5.

    The score column reads ``score_band``, which is where the thresholds are
    defined, so the colour on this page cannot drift away from the wording the
    tools and the application use.

    The evaluations inline is read-only. A score is a judgement made at a
    moment against a particular requirement set; if the opening's requirements
    are later sharpened, the earlier judgement must still be readable as it
    stood, and an editable history destroys exactly that.
    """

    list_display = ('avatar', 'full_name', 'job_opening', 'status_badge',
                    'score_column', 'current_title', 'source', 'status')
    list_display_links = ('avatar', 'full_name')
    list_editable = ('status',)
    list_filter = ('status', 'source', 'job_opening',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('full_name', 'email', 'skills', 'resume_text',
                     'current_company')
    date_hierarchy = 'created_at'
    list_per_page = 30
    save_on_top = True
    empty_value_display = '--'

    autocomplete_fields = ('job_opening', 'created_by_agent')
    readonly_fields = ('skill_summary', 'score', 'band_column', 'created_at',
                       'updated_at')
    inlines = [CandidateEvaluationInline, InterviewInline]
    actions = ['action_shortlist']

    fieldsets = (
        ('The person', {
            'fields': ('full_name', ('email', 'phone'), 'location',
                       'linkedin_url'),
        }),
        ('Where they are now', {
            'fields': (('current_title', 'current_company'), 'years_experience',
                       'skills', 'skill_summary'),
        }),
        ('This application', {
            'fields': ('job_opening', ('source', 'status'),
                       ('score', 'band_column'), 'notes'),
        }),
        ('Resume', {
            'classes': ('collapse',),
            'fields': ('resume_source', 'resume_text'),
        }),
        ('Provenance', {
            'classes': ('collapse',),
            'fields': ('created_by_agent', ('created_at', 'updated_at')),
        }),
    )

    @admin.display(description='')
    def avatar(self, obj):
        return _avatar(obj.initials, _colour(BAND_COLOURS, obj.score_band))

    @admin.display(description='Stage', ordering='status')
    def status_badge(self, obj):
        return _badge(obj.get_status_display(),
                      _colour(CANDIDATE_STATUS_COLOURS, obj.status))

    @admin.display(description='Score', ordering='score')
    def score_column(self, obj):
        band = obj.score_band
        if band == 'unscored':
            return _pill('not screened', _FAINT)
        return _badge(f'{obj.score} {band}', _colour(BAND_COLOURS, band))

    @admin.display(description='Band')
    def band_column(self, obj):
        return obj.score_band

    @admin.display(description='Skills recorded')
    def skill_summary(self, obj):
        return _count_label(obj.skills, 'skill')

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('job_opening', 'created_by_agent'))

    @admin.action(description='Shortlist the selected candidates')
    def action_shortlist(self, request, queryset):
        updated = queryset.exclude(
            status__in=('hired', 'rejected', 'withdrawn')).update(
            status='shortlisted')
        skipped = queryset.count() - updated
        self.message_user(request, f'{updated} candidates were shortlisted.',
                          level=messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f'{skipped} were left alone because they are already hired, '
                f'rejected or withdrawn.',
                level=messages.WARNING)


# ===========================================================================
# FLAGSHIP 7: job openings
# ===========================================================================

@admin.register(JobOpening)
class JobOpeningAdmin(admin.ModelAdmin):
    """One role being recruited for, with its scoreable requirements.

    WHAT THIS PAGE IS FOR
    ---------------------
    ``requirements`` is a list of ``{"text", "weight", "must_have"}``
    dictionaries rather than a block of prose, and that single decision is
    what makes screening defensible: a requirement stored that way can be
    scored one at a time, weighted, and reported on with evidence, while a
    requirement stored as a paragraph can only be eyeballed. This page is
    where that structure is edited, so it is where an unscoreable requirement
    gets fixed before it produces a shortlist nobody can defend.

    The candidate counts read ``candidate_count`` and ``shortlisted_count`` so
    the numbers here are the same numbers the application shows.
    """

    list_display = ('title', 'department', 'status_badge', 'seniority',
                    'candidate_column', 'shortlisted_column', 'salary_column',
                    'requirement_summary')
    list_filter = ('status', 'seniority', 'employment_type', 'is_remote',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('title', 'department', 'description', 'summary',
                     'hiring_manager')
    date_hierarchy = 'created_at'
    save_on_top = True
    empty_value_display = '--'

    autocomplete_fields = ('created_by_agent', 'created_by')
    readonly_fields = ('requirement_table', 'candidate_column',
                       'shortlisted_column', 'created_at', 'updated_at')
    inlines = [CandidateOnOpeningInline]

    fieldsets = (
        ('The role', {
            'fields': ('title', 'department', 'location', 'is_remote',
                       ('employment_type', 'seniority'), 'openings',
                       'hiring_manager', 'status', 'target_start_date'),
        }),
        ('The advertisement', {
            'fields': ('summary', 'description', 'responsibilities', 'benefits'),
        }),
        ('Requirements, as scoreable entries', {
            'description': 'Each entry needs text, a weight and whether it is a '
                           'must-have. A requirement written as prose cannot be '
                           'scored, and a shortlist built on one cannot be '
                           'defended later.',
            'fields': ('requirements', 'nice_to_have', 'requirement_table'),
        }),
        ('Money', {
            'fields': (('salary_min', 'salary_max'), 'salary_currency'),
        }),
        ('Pipeline and provenance', {
            'classes': ('collapse',),
            'fields': (('candidate_column', 'shortlisted_column'),
                       'created_by_agent', 'created_by',
                       ('created_at', 'updated_at')),
        }),
    )

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _badge(obj.get_status_display(),
                      _colour(OPENING_STATUS_COLOURS, obj.status))

    @admin.display(description='Candidates')
    def candidate_column(self, obj):
        return obj.candidate_count

    @admin.display(description='Shortlisted')
    def shortlisted_column(self, obj):
        count = obj.shortlisted_count
        return _pill(str(count), '#047857' if count else _FAINT)

    @admin.display(description='Salary', ordering='salary_min')
    def salary_column(self, obj):
        return obj.salary_range

    @admin.display(description='Requirements')
    def requirement_summary(self, obj):
        return _count_label(obj.requirements, 'requirement')

    @admin.display(description='Requirements as they will be scored')
    def requirement_table(self, obj):
        lines = obj.requirement_lines
        if not lines:
            return 'No requirements have been recorded, so nothing can be scored.'
        rows = []
        for entry in lines:
            if isinstance(entry, dict):
                rows.append((entry.get('text', ''),
                             f"weight {entry.get('weight', 1)}"
                             f"{', must have' if entry.get('must_have') else ''}"))
            else:
                rows.append((str(entry), 'not scoreable -- stored as prose'))
        return _key_value_table(rows)


# ===========================================================================
# FLAGSHIP 8: work items
# ===========================================================================

@admin.register(WorkItem)
class WorkItemAdmin(admin.ModelAdmin):
    """One piece of work a developer could pick up and start on.

    WHAT THIS PAGE IS FOR
    ---------------------
    The backlog. Status and priority are editable from the list because a
    sprint planning session reprioritises twenty items in ten minutes, and the
    overdue marker reads ``is_overdue`` and ``days_overdue`` so it agrees with
    the sprint reports rather than re-deriving the comparison here.

    ``depends_on`` gets a horizontal filter rather than a plain multiple
    select: it is a non-symmetrical self relation whose reverse accessor is
    ``blocks``, and sequencing an epic means moving several dependencies at
    once. ``assignee_label`` is used for the holder column because an item can
    be assigned to a name that is not in the Employee table yet -- a
    contractor, a new starter -- and that string is worth showing.
    """

    list_display = ('reference', 'title', 'type_icon', 'status_badge',
                    'priority_pill', 'holder', 'estimate_column', 'due_column',
                    'sprint', 'status', 'priority')
    list_display_links = ('reference', 'title')
    list_editable = ('status', 'priority')
    list_filter = ('status', 'priority', 'item_type', 'project', 'sprint',
                   'complexity', ('due_date', admin.DateFieldListFilter))
    search_fields = ('reference', 'title', 'description', 'assignee_name',
                     'external_reference')
    date_hierarchy = 'created_at'
    list_per_page = 40
    save_on_top = True
    empty_value_display = '--'

    autocomplete_fields = ('project', 'sprint', 'parent', 'assignee',
                           'created_by_agent')
    filter_horizontal = ('depends_on',)
    readonly_fields = ('reference', 'criteria_table', 'blocker_summary',
                       'created_at', 'updated_at', 'completed_at')
    inlines = [WorkItemCommentInline]

    fieldsets = (
        ('The work', {
            'fields': ('reference', 'title', 'description', 'project',
                       ('item_type', 'complexity')),
        }),
        ('Acceptance criteria', {
            'fields': ('acceptance_criteria', 'criteria_table'),
        }),
        ('Scheduling', {
            'fields': (('status', 'priority'), 'sprint', 'parent',
                       ('estimate_points', 'estimate_hours'), 'due_date',
                       'sequence_hint'),
        }),
        ('Who holds it', {
            'fields': ('assignee', 'assignee_name', 'labels'),
        }),
        ('Dependencies', {
            'description': 'What this item waits on. The reverse of the relation '
                           'answers "what is this holding up?".',
            'fields': ('depends_on', 'blocker_summary', 'blocked_reason'),
        }),
        ('Elsewhere and provenance', {
            'classes': ('collapse',),
            'fields': (('external_reference', 'external_url'),
                       'created_by_agent',
                       ('created_at', 'updated_at', 'completed_at')),
        }),
    )

    @admin.display(description='Type', ordering='item_type')
    def type_icon(self, obj):
        name, colour = WORK_TYPE_ICONS.get(obj.item_type, ('fa-circle', _GREY))
        return _icon(name, colour)

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _badge(obj.get_status_display(),
                      _colour(WORK_STATUS_COLOURS, obj.status))

    @admin.display(description='Priority', ordering='priority')
    def priority_pill(self, obj):
        return _pill(obj.get_priority_display(),
                     _colour(PRIORITY_COLOURS, obj.priority))

    @admin.display(description='Holder', ordering='assignee__full_name')
    def holder(self, obj):
        return obj.assignee_label

    @admin.display(description='Estimate', ordering='estimate_points')
    def estimate_column(self, obj):
        if obj.estimate_points:
            return f'{obj.estimate_points} pts'
        if obj.estimate_hours:
            return f'{obj.estimate_hours} h'
        return _pill('unestimated', _FAINT)

    @admin.display(description='Due', ordering='due_date')
    def due_column(self, obj):
        if not obj.due_date:
            return _pill('no date', _FAINT)
        stamp = obj.due_date.strftime('%d %b %Y')
        if obj.is_overdue:
            days = obj.days_overdue
            return format_html('{} {}', stamp,
                               _badge(f'{days} d overdue', '#b91c1c'))
        return stamp

    @admin.display(description='Acceptance criteria')
    def criteria_table(self, obj):
        criteria = [str(entry) for entry in (obj.acceptance_criteria or []) if entry]
        if not criteria:
            return 'No acceptance criteria were recorded.'
        return format_html_join(mark_safe('<br>'), '&bull; {}',
                                ((text,) for text in criteria))

    @admin.display(description='Still blocking')
    def blocker_summary(self, obj):
        if not obj.pk:
            return 'Save the item first, then add dependencies.'
        unmet = obj.unmet_dependencies
        if not unmet:
            return 'Nothing this item depends on is outstanding.'
        return format_html_join(
            mark_safe('<br>'), '<code>{}</code> {} ({})',
            ((item.reference, item.title, item.get_status_display())
             for item in unmet))

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('project', 'sprint', 'assignee', 'parent'))


# ===========================================================================
# FLAGSHIP 9: knowledge documents
# ===========================================================================

@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    """One company document the workforce may cite, with its passages.

    WHAT THIS PAGE IS FOR
    ---------------------
    Provenance. Every answer the Research employee gives cites a document at a
    version, so this page is where somebody checks that the policy being cited
    is the current one, that it is still active, and that it has actually been
    indexed -- an unindexed document has no passages and can therefore never
    be found, however good its content is.

    ``indexed_at`` empty with a non-zero word count is the state to look for,
    and the re-index action fixes it. Re-indexing deletes and rewrites the
    passages rather than diffing them, because chunk boundaries move when the
    text changes and a half-updated passage would let a citation quote text
    that never appeared together in the document.
    """

    list_display = ('title', 'doc_type', 'source', 'department', 'word_count',
                    'chunk_column', 'indexed_at', 'is_active')
    list_filter = ('doc_type', 'department', 'source', 'is_active',
                   ('updated_at', admin.DateFieldListFilter))
    search_fields = ('title', 'content', 'tags', 'author')
    date_hierarchy = 'updated_at'
    list_per_page = 30
    save_on_top = True
    empty_value_display = 'Never'

    autocomplete_fields = ('source', 'created_by', 'created_by_agent')
    readonly_fields = ('checksum', 'word_count', 'chunk_column', 'indexed_at',
                       'tag_summary', 'created_at', 'updated_at')
    inlines = [DocumentChunkInline]
    actions = ['action_reindex']

    fieldsets = (
        ('The document', {
            'fields': ('title', ('doc_type', 'department'), 'summary', 'content'),
        }),
        ('Provenance', {
            'fields': ('source', ('external_id', 'url'), ('version', 'author'),
                       'is_active'),
        }),
        ('Indexing', {
            'description': 'Passages are derived from the content. Use the '
                           're-index action after editing the text.',
            'fields': ('tags', 'tag_summary',
                       ('word_count', 'chunk_column'),
                       ('checksum', 'indexed_at')),
        }),
        ('Who added it', {
            'classes': ('collapse',),
            'fields': ('created_by', 'created_by_agent',
                       ('created_at', 'updated_at')),
        }),
    )

    @admin.display(description='Passages', ordering='_chunk_total')
    def chunk_column(self, obj):
        # Reads the annotation from get_queryset, so a list of thirty documents
        # costs one query rather than thirty.
        count = getattr(obj, '_chunk_total', None)
        if count is None:
            count = obj.chunk_count
        return _pill(str(count), '#047857' if count else '#b91c1c')

    @admin.display(description='Tags')
    def tag_summary(self, obj):
        return _count_label(obj.tags, 'tag')

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('source', 'created_by', 'created_by_agent')
                .annotate(_chunk_total=Count('chunks', distinct=True)))

    @admin.action(description='Re-index the selected documents')
    def action_reindex(self, request, queryset):
        indexer = _knowledge()
        if indexer is None:
            self.message_user(request, _KNOWLEDGE_UNAVAILABLE,
                              level=messages.ERROR)
            return
        documents = 0
        passages = 0
        for document in queryset:
            try:
                passages += indexer.reindex(document)
            except Exception as error:  # noqa: BLE001
                self.message_user(request,
                                  f'"{document.title}" could not be indexed: {error}',
                                  level=messages.ERROR)
                continue
            documents += 1
        self.message_user(
            request,
            f'{documents} documents were re-indexed into {passages} passages.',
            level=messages.SUCCESS if documents else messages.WARNING)


# ===========================================================================
# FLAGSHIP 10: content pieces
# ===========================================================================

@admin.register(ContentPiece)
class ContentPieceAdmin(admin.ModelAdmin):
    """One piece of marketing copy, on one channel, for one purpose.

    WHAT THIS PAGE IS FOR
    ---------------------
    Checking copy before it goes out. Two checks matter, and both are columns
    rather than something a reader has to work out.

    The length column reads ``fits_channel``, which returns the platform's own
    limit and a message saying what to cut. A piece that is over is marked, not
    merely reported, because "412 characters over LinkedIn's 3000" is
    actionable and a green tick on an unpublishable post is not.

    The sources panel records the facts the copy was built from, with the
    documents they came from. A figure in the body that appears nowhere in
    ``sources`` was either typed by somebody who knows something the record
    does not, or invented -- and both deserve a person's attention before
    publication. The body and the sources are read-only here so this page
    reviews rather than rewrites; editing copy belongs on the content page,
    which recounts the length as it is typed.
    """

    list_display = ('title', 'channel_icon', 'kind', 'status_badge',
                    'length_column', 'scheduled_for', 'published_at')
    list_filter = ('channel', 'status', 'kind', 'campaign',
                   ('scheduled_for', admin.DateFieldListFilter))
    search_fields = ('title', 'body', 'audience', 'call_to_action')
    date_hierarchy = 'created_at'
    list_per_page = 30
    save_on_top = True
    empty_value_display = '--'

    autocomplete_fields = ('campaign', 'brief', 'variant_of', 'created_by_agent')
    readonly_fields = ('body_panel', 'source_panel', 'length_verdict',
                       'word_count', 'character_count', 'created_at',
                       'updated_at')

    fieldsets = (
        ('The piece', {
            'fields': ('title', ('channel', 'kind'), 'campaign', 'brief',
                       ('audience', 'tone'), 'call_to_action', 'hashtags'),
        }),
        ('The copy', {
            'description': 'Read-only here. Copy is edited on the content page, '
                           'which recounts the length as it is typed.',
            'fields': ('body_panel', 'length_verdict',
                       ('word_count', 'character_count')),
        }),
        ('Evidence', {
            'description': 'A figure in the copy that appears nowhere below was '
                           'not supplied. That is worth a person checking.',
            'fields': ('source_panel',),
        }),
        ('Publication', {
            'fields': ('status', ('variant_of', 'variant_label'),
                       ('scheduled_for', 'published_at'),
                       ('external_reference', 'external_url')),
        }),
        ('Provenance', {
            'classes': ('collapse',),
            'fields': ('created_by_agent', ('created_at', 'updated_at')),
        }),
    )

    @admin.display(description='Channel', ordering='channel')
    def channel_icon(self, obj):
        name, colour = CHANNEL_ICONS.get(obj.channel, ('fa-share-nodes', _GREY))
        return format_html('{} {}', _icon(name, colour),
                           obj.get_channel_display())

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _badge(obj.get_status_display(),
                      _colour(CONTENT_STATUS_COLOURS, obj.status))

    @admin.display(description='Length', ordering='character_count')
    def length_column(self, obj):
        fits, _message = obj.fits_channel
        allowance = (CHANNEL_LIMITS.get(obj.channel or '') or {}).get('characters') or 0
        label = f'{obj.character_count}'
        if allowance:
            label = f'{label} / {allowance}'
        if fits:
            return _pill(label, '#047857')
        return _badge(f'{label} OVER LIMIT', '#b91c1c')

    @admin.display(description='Channel limits')
    def length_verdict(self, obj):
        fits, message = obj.fits_channel
        return _pill(message, '#047857' if fits else '#b91c1c')

    @admin.display(description='Copy')
    def body_panel(self, obj):
        return _text_panel(obj.full_text, empty='No copy has been written yet.')

    @admin.display(description='Facts the copy was built from')
    def source_panel(self, obj):
        entries = [entry for entry in (obj.sources or []) if entry]
        if not entries:
            return ('No sources were recorded, so nothing in the copy can be '
                    'traced back to a document.')
        rows = []
        for entry in entries:
            if isinstance(entry, dict):
                rows.append((entry.get('document') or 'unattributed',
                             entry.get('fact') or entry.get('text') or entry))
            else:
                rows.append(('unattributed', entry))
        return _key_value_table(rows)

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('campaign', 'brief', 'variant_of',
                                'created_by_agent'))


# ===========================================================================
# FLAGSHIP 11: system settings
# ===========================================================================

@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    """Global configuration as data, which is the point of the table.

    WHAT THIS PAGE IS FOR
    ---------------------
    The project's promise is that nothing requires a code edit or an
    environment variable. Company name, default demo behaviour, working hours,
    the signature block -- all of it is a row here, editable by a person and by
    an AI employee calling the settings tool. This page is the fallback for
    anything the settings screens do not yet expose.

    A setting marked ``is_secret`` never shows its value on this page, in the
    list or in a read-only display method. The resolved column reports
    ``(set)`` or ``(not set)`` and nothing else, for the same reason
    ``Integration.secrets`` is excluded from its form: a credential that can be
    read back out of an administration page is a credential that has leaked.
    """

    list_display = ('label', 'group', 'value_type', 'resolved_column',
                    'is_secret', 'display_order', 'updated_at')
    list_filter = ('group', 'value_type', 'is_secret')
    search_fields = ('key', 'label', 'description')
    ordering = ('group', 'display_order', 'label')
    list_per_page = 60
    save_on_top = True
    empty_value_display = '--'

    autocomplete_fields = ('updated_by',)
    readonly_fields = ('resolved_column', 'choice_summary', 'updated_at')

    fieldsets = (
        ('The setting', {
            'fields': ('key', 'label', 'description',
                       ('group', 'display_order')),
        }),
        ('Its value', {
            'fields': ('value_type', 'value', 'resolved_column', 'choices',
                       'choice_summary', 'is_secret'),
        }),
        ('Last change', {
            'fields': ('updated_by', 'updated_at'),
        }),
    )

    @admin.display(description='Resolved value')
    def resolved_column(self, obj):
        """The stored value, or its presence alone when the setting is secret."""
        if obj.is_secret:
            present = obj.resolved not in (None, '', {}, [])
            return _pill('(set)' if present else '(not set)', '#b45309')
        value = obj.resolved
        if isinstance(value, (dict, list, tuple)):
            return _pill(_count_label(value, 'entry', 'entries'), _GREY)
        if isinstance(value, bool):
            return _pill('yes' if value else 'no',
                         '#047857' if value else _GREY)
        if value in (None, ''):
            return _pill('empty', _FAINT)
        return format_html('<code>{}</code>', _plain(value, 160))

    @admin.display(description='Allowed choices')
    def choice_summary(self, obj):
        if not obj.choices:
            return 'This setting is not restricted to a list.'
        return _count_label(obj.choices, 'choice')


# ===========================================================================
# Platform: the rest
# ===========================================================================

@admin.register(AgentCapability)
class AgentCapabilityAdmin(admin.ModelAdmin):
    list_display = ('name', 'agent', 'group', 'tool_name', 'integration_key',
                    'requires_approval', 'is_enabled', 'usage_count',
                    'last_used_at')
    list_filter = ('group', 'requires_approval', 'is_enabled', 'agent')
    search_fields = ('name', 'slug', 'description', 'tool_name', 'agent__name')
    list_editable = ('is_enabled',)
    autocomplete_fields = ('agent',)
    readonly_fields = ('usage_count', 'last_used_at')
    empty_value_display = '--'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('agent')


@admin.register(AgentDelegation)
class AgentDelegationAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'from_agent', 'to_agent', 'status_badge',
                    'request_preview', 'source_summary', 'tokens_used',
                    'answered_at')
    list_filter = ('status', 'from_agent', 'to_agent',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('request', 'response', 'from_agent__name', 'to_agent__name')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('from_agent', 'to_agent', 'task')
    readonly_fields = ('created_at', 'answered_at', 'tokens_used')
    empty_value_display = '--'

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Asked')
    def request_preview(self, obj):
        return _plain(obj.request, 90)

    @admin.display(description='Sources')
    def source_summary(self, obj):
        return _count_label(obj.sources, 'source')

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('from_agent', 'to_agent', 'task'))


@admin.register(OrchestratorDecision)
class OrchestratorDecisionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'request_preview', 'chosen_agent', 'method',
                    'confidence', 'candidate_summary', 'requester')
    list_filter = ('method', 'chosen_agent',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('request_text', 'reasoning', 'chosen_agent__name')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('chosen_agent', 'task', 'requested_by')
    readonly_fields = ('created_at',)
    empty_value_display = '--'

    @admin.display(description='Request')
    def request_preview(self, obj):
        return _plain(obj.request_text, 100)

    @admin.display(description='Candidates considered')
    def candidate_summary(self, obj):
        return _count_label(obj.candidates, 'candidate')

    @admin.display(description='Asked by', ordering='requested_by__username')
    def requester(self, obj):
        return _user_link(obj.requested_by)

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('chosen_agent', 'task', 'requested_by'))


@admin.register(ActionAuditTrail)
class ActionAuditTrailAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action', 'event', 'actor_column',
                    'from_status', 'to_status', 'change_summary')
    list_filter = ('event', ('created_at', admin.DateFieldListFilter))
    search_fields = ('note', 'action__title', 'actor__username')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('action', 'actor')
    readonly_fields = ('created_at',)
    empty_value_display = '--'

    @admin.display(description='Actor', ordering='actor__username')
    def actor_column(self, obj):
        return _user_link(obj.actor)

    @admin.display(description='Changes')
    def change_summary(self, obj):
        return _count_label(obj.changes, 'field changed', 'fields changed')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('action', 'actor')


@admin.register(OutboundMessage)
class OutboundMessageAdmin(admin.ModelAdmin):
    list_display = ('sent_at', 'channel', 'recipient', 'subject',
                    'status_badge', 'agent', 'integration', 'external_id')
    list_filter = ('channel', 'status', 'integration', 'agent',
                   ('sent_at', admin.DateFieldListFilter))
    search_fields = ('recipient', 'subject', 'body', 'external_id',
                     'error_message')
    date_hierarchy = 'sent_at'
    autocomplete_fields = ('agent', 'action', 'integration')
    readonly_fields = ('sent_at',)
    empty_value_display = '--'

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('agent', 'action', 'integration'))


@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display = ('start_at', 'title', 'duration_minutes', 'status_badge',
                    'attendee_summary', 'location', 'agent')
    list_filter = ('status', 'agent', ('start_at', admin.DateFieldListFilter))
    search_fields = ('title', 'description', 'location', 'external_id')
    date_hierarchy = 'start_at'
    autocomplete_fields = ('agent', 'action')
    readonly_fields = ('created_at',)
    empty_value_display = '--'

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Attendees')
    def attendee_summary(self, obj):
        return _count_label(obj.attendees, 'attendee')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('agent', 'action')


@admin.register(ExternalIssue)
class ExternalIssueAdmin(admin.ModelAdmin):
    list_display = ('reference', 'title', 'system', 'container',
                    'status_badge', 'assignee', 'label_summary', 'created_at')
    list_display_links = ('reference', 'title')
    list_filter = ('system', 'issue_status', 'agent',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('reference', 'title', 'body', 'container', 'external_id')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('agent', 'action')
    readonly_fields = ('created_at', 'updated_at')
    empty_value_display = '--'

    @admin.display(description='Status', ordering='issue_status')
    def status_badge(self, obj):
        return _generic_badge(obj, 'issue_status')

    @admin.display(description='Labels')
    def label_summary(self, obj):
        return _count_label(obj.labels, 'label')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('agent', 'action')


@admin.register(AgentMemory)
class AgentMemoryAdmin(admin.ModelAdmin):
    list_display = ('key', 'agent', 'scope', 'kind', 'importance_pill',
                    'content_preview', 'recall_count', 'updated_at')
    list_filter = ('scope', 'kind', 'agent',
                   ('updated_at', admin.DateFieldListFilter))
    search_fields = ('key', 'content', 'source', 'agent__name')
    date_hierarchy = 'updated_at'
    autocomplete_fields = ('agent', 'conversation', 'created_by')
    readonly_fields = ('recall_count', 'last_recalled_at', 'created_at',
                       'updated_at')
    empty_value_display = '--'

    @admin.display(description='Importance', ordering='importance')
    def importance_pill(self, obj):
        value = obj.importance or 0
        colour = '#b91c1c' if value >= 8 else '#b45309' if value >= 5 else _FAINT
        return _pill(f'{value}/10', colour)

    @admin.display(description='Remembers')
    def content_preview(self, obj):
        return _plain(obj.content, 110)

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('agent', 'conversation', 'created_by'))


# ===========================================================================
# People Operations: the rest
# ===========================================================================

@admin.register(CandidateEvaluation)
class CandidateEvaluationAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'candidate', 'job_opening', 'score_column',
                    'recommendation', 'strength_summary', 'gap_summary',
                    'agent')
    list_filter = ('recommendation', 'job_opening', 'agent',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('candidate__full_name', 'rationale',
                     'job_opening__title')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('candidate', 'job_opening', 'agent')
    readonly_fields = ('created_at', 'breakdown_table')
    empty_value_display = '--'

    @admin.display(description='Score', ordering='score')
    def score_column(self, obj):
        return _badge(f'{obj.score} {obj.band}', _colour(BAND_COLOURS, obj.band))

    @admin.display(description='Strengths')
    def strength_summary(self, obj):
        return _count_label(obj.strengths, 'strength')

    @admin.display(description='Gaps')
    def gap_summary(self, obj):
        return _count_label(obj.gaps, 'gap')

    @admin.display(description='Score against each requirement')
    def breakdown_table(self, obj):
        rows = [row for row in (obj.score_breakdown or []) if isinstance(row, dict)]
        if not rows:
            return 'No breakdown was recorded.'
        return _key_value_table(
            [(row.get('requirement') or row.get('text') or 'requirement',
              f"{row.get('score', 0)} of {row.get('max', 0)}") for row in rows])

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('candidate', 'job_opening', 'agent'))


@admin.register(Interview)
class InterviewAdmin(admin.ModelAdmin):
    list_display = ('scheduled_at', 'candidate', 'round_name', 'round_number',
                    'mode', 'status_badge', 'interviewer_summary',
                    'question_count', 'outcome_score', 'status')
    list_filter = ('status', 'mode', 'job_opening',
                   ('scheduled_at', admin.DateFieldListFilter))
    search_fields = ('candidate__full_name', 'round_name', 'feedback',
                     'location_or_link')
    date_hierarchy = 'scheduled_at'
    list_editable = ('status',)
    autocomplete_fields = ('candidate', 'job_opening', 'calendar_event',
                           'created_by_agent')
    readonly_fields = ('created_at', 'updated_at', 'question_count')
    empty_value_display = 'Not booked'

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Interviewers')
    def interviewer_summary(self, obj):
        return _count_label(obj.interviewers, 'interviewer')

    @admin.display(description='Questions')
    def question_count(self, obj):
        return obj.question_count

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('candidate', 'job_opening', 'calendar_event'))


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('avatar', 'full_name', 'job_title', 'department', 'manager',
                    'status_badge', 'start_date', 'onboarding_column',
                    'leave_balance_days')
    list_display_links = ('avatar', 'full_name')
    list_filter = ('employment_status', 'department',
                   ('start_date', admin.DateFieldListFilter))
    # search_fields is required here: manager is a self-reference that
    # autocompletes against this very admin, and Employee is autocompleted by
    # WorkItem, OnboardingTask and PerformanceReview.
    search_fields = ('full_name', 'email', 'job_title', 'department', 'skills')
    date_hierarchy = 'start_date'
    autocomplete_fields = ('manager', 'account', 'from_candidate')
    readonly_fields = ('onboarding_column', 'created_at', 'updated_at')
    empty_value_display = '--'

    @admin.display(description='')
    def avatar(self, obj):
        return _avatar(obj.initials, '#0369a1')

    @admin.display(description='Status', ordering='employment_status')
    def status_badge(self, obj):
        return _generic_badge(obj, 'employment_status')

    @admin.display(description='Onboarding')
    def onboarding_column(self, obj):
        percent = obj.onboarding_progress
        open_count = obj.open_onboarding_count
        colour = '#047857' if percent >= 100 else '#b45309' if percent else _FAINT
        return _pill(f'{percent}% -- {open_count} open', colour)

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('manager', 'account', 'from_candidate'))


@admin.register(OnboardingTask)
class OnboardingTaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'employee', 'category', 'owner_role',
                    'status_badge', 'timing_column', 'due_date',
                    'completed_at', 'status')
    list_filter = ('status', 'category', 'owner_role',
                   ('due_date', admin.DateFieldListFilter))
    search_fields = ('title', 'description', 'employee__full_name')
    date_hierarchy = 'due_date'
    list_editable = ('status',)
    autocomplete_fields = ('employee', 'created_by_agent')
    readonly_fields = ('completed_at', 'created_at', 'timing_column')
    empty_value_display = '--'

    @admin.display(description='State', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='When', ordering='due_offset_days')
    def timing_column(self, obj):
        return obj.timing_label

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('employee')


@admin.register(PerformanceReview)
class PerformanceReviewAdmin(admin.ModelAdmin):
    list_display = ('employee', 'period', 'reviewer', 'status_badge',
                    'section_column', 'overall_rating', 'complete_flag',
                    'created_at')
    list_filter = ('status', ('created_at', admin.DateFieldListFilter))
    search_fields = ('employee__full_name', 'period', 'reviewer', 'summary')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('employee', 'created_by_agent')
    readonly_fields = ('created_at', 'section_column')
    empty_value_display = '--'

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Sections')
    def section_column(self, obj):
        return _count_label(obj.sections, 'section')

    @admin.display(description='Complete', boolean=True)
    def complete_flag(self, obj):
        return obj.is_complete

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('employee')


@admin.register(HRAnnouncement)
class HRAnnouncementAdmin(admin.ModelAdmin):
    list_display = ('title', 'channel', 'audience', 'status_badge',
                    'published_at', 'created_by_agent', 'created_at', 'status')
    list_filter = ('channel', 'status',
                   ('published_at', admin.DateFieldListFilter))
    search_fields = ('title', 'body', 'audience')
    date_hierarchy = 'created_at'
    list_editable = ('status',)
    autocomplete_fields = ('created_by_agent',)
    readonly_fields = ('created_at',)
    empty_value_display = 'Not published'

    @admin.display(description='State', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('created_by_agent')


# ===========================================================================
# Engineering: the rest
# ===========================================================================

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('key', 'name', 'status_badge', 'owner', 'progress_column',
                    'stack_summary', 'target_date')
    list_display_links = ('key', 'name')
    list_filter = ('status', ('target_date', admin.DateFieldListFilter))
    search_fields = ('name', 'key', 'description', 'repository', 'owner')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('created_by_agent', 'created_by')
    readonly_fields = ('progress_column', 'created_at', 'updated_at')
    empty_value_display = '--'

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Progress')
    def progress_column(self, obj):
        percent = obj.progress_percent
        colour = '#047857' if percent >= 80 else '#b45309' if percent >= 30 else _FAINT
        return _pill(f'{percent}%', colour)

    @admin.display(description='Stack')
    def stack_summary(self, obj):
        return _count_label(obj.tech_stack, 'technology', 'technologies')

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('created_by_agent', 'created_by'))


@admin.register(Sprint)
class SprintAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'status_badge', 'start_date',
                    'end_date', 'capacity_points', 'load_column',
                    'current_flag')
    list_filter = ('status', 'project', ('start_date', admin.DateFieldListFilter))
    # search_fields is required here because WorkItem and SprintReport
    # autocomplete this model.
    search_fields = ('name', 'goal', 'project__name')
    date_hierarchy = 'start_date'
    autocomplete_fields = ('project', 'created_by_agent')
    readonly_fields = ('load_column', 'created_at')
    empty_value_display = '--'

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Committed')
    def load_column(self, obj):
        over = obj.over_capacity_by
        label = f'{obj.completed_points} of {obj.committed_points} pts done'
        if over:
            return _badge(f'{label} -- {over} over capacity', '#b91c1c')
        return _pill(label, '#047857')

    @admin.display(description='Current', boolean=True)
    def current_flag(self, obj):
        return obj.is_current

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('project')


@admin.register(WorkItemComment)
class WorkItemCommentAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'work_item', 'author_column',
                    'body_preview')
    list_filter = ('agent', ('created_at', admin.DateFieldListFilter))
    search_fields = ('body', 'work_item__reference', 'work_item__title',
                     'author__username')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('work_item', 'author', 'agent')
    readonly_fields = ('created_at',)
    empty_value_display = '--'

    @admin.display(description='Author')
    def author_column(self, obj):
        return obj.author_label

    @admin.display(description='Comment')
    def body_preview(self, obj):
        return _plain(obj.body, 140)

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('work_item', 'author', 'agent'))


@admin.register(SprintReport)
class SprintReportAdmin(admin.ModelAdmin):
    list_display = ('title', 'kind', 'project', 'sprint', 'metric_summary',
                    'period_start', 'period_end', 'created_at')
    list_filter = ('kind', 'project', ('created_at', admin.DateFieldListFilter))
    search_fields = ('title', 'body', 'project__name', 'sprint__name')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('project', 'sprint', 'created_by_agent')
    readonly_fields = ('created_at', 'metric_block')
    empty_value_display = '--'

    @admin.display(description='Metrics')
    def metric_summary(self, obj):
        return _count_label(obj.metrics, 'metric')

    @admin.display(description='Recorded metrics')
    def metric_block(self, obj):
        return _key_value_table(sorted((obj.metrics or {}).items()),
                                empty='No metrics were recorded.')

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('project', 'sprint', 'created_by_agent'))


@admin.register(CodeArtifact)
class CodeArtifactAdmin(admin.ModelAdmin):
    list_display = ('title', 'kind', 'language', 'status_badge', 'work_item',
                    'project', 'line_column', 'created_at', 'status')
    list_filter = ('kind', 'language', 'status', 'project',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('title', 'content', 'explanation', 'suggested_path',
                     'repository')
    date_hierarchy = 'created_at'
    list_editable = ('status',)
    autocomplete_fields = ('work_item', 'project', 'created_by_agent')
    readonly_fields = ('created_at', 'line_column')
    empty_value_display = '--'

    @admin.display(description='State', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Lines')
    def line_column(self, obj):
        return obj.line_count

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('work_item', 'project', 'created_by_agent'))


@admin.register(CodeReview)
class CodeReviewAdmin(admin.ModelAdmin):
    list_display = ('title', 'repository', 'pull_request_number',
                    'verdict_badge', 'finding_summary', 'blocking_column',
                    'work_item', 'created_at')
    list_filter = ('verdict', 'repository',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('title', 'summary', 'repository')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('work_item', 'created_by_agent')
    readonly_fields = ('created_at', 'finding_table')
    empty_value_display = '--'

    @admin.display(description='Verdict', ordering='verdict')
    def verdict_badge(self, obj):
        colour = {'approve': '#047857', 'request_changes': '#b91c1c',
                  'comment': _GREY}.get(obj.verdict, _GREY)
        return _badge(obj.get_verdict_display(), colour)

    @admin.display(description='Findings')
    def finding_summary(self, obj):
        return _count_label(obj.findings, 'finding')

    @admin.display(description='Blocking')
    def blocking_column(self, obj):
        count = obj.blocking_count
        return _pill(str(count), '#b91c1c' if count else '#047857')

    @admin.display(description='What the review found')
    def finding_table(self, obj):
        rows = [row for row in (obj.findings or []) if isinstance(row, dict)]
        if not rows:
            return 'No findings were recorded.'
        return _key_value_table(
            [(f"{row.get('severity', 'note')} -- {row.get('file', 'unknown file')}",
              row.get('comment') or row.get('text') or '') for row in rows])

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('work_item', 'created_by_agent'))


# ===========================================================================
# Customer Support: the rest
# ===========================================================================

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('avatar', 'name', 'company', 'email', 'tier_pill',
                    'open_ticket_column', 'account_reference', 'created_at')
    list_display_links = ('avatar', 'name')
    list_filter = ('tier', ('created_at', admin.DateFieldListFilter))
    search_fields = ('name', 'email', 'company', 'account_reference', 'phone')
    date_hierarchy = 'created_at'
    readonly_fields = ('open_ticket_column', 'created_at', 'updated_at')
    empty_value_display = '--'

    @admin.display(description='')
    def avatar(self, obj):
        return _avatar(obj.initials, '#7c3aed' if obj.is_priority else _GREY)

    @admin.display(description='Tier', ordering='tier')
    def tier_pill(self, obj):
        colour = '#7c3aed' if obj.is_priority else _GREY
        return _pill(obj.get_tier_display(), colour)

    @admin.display(description='Open tickets')
    def open_ticket_column(self, obj):
        count = obj.open_ticket_count
        return _pill(str(count), '#b45309' if count else '#047857')


@admin.register(TicketMessage)
class TicketMessageAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'ticket', 'direction', 'author_label',
                    'preview_column', 'is_ai_draft', 'sent_flag')
    list_filter = ('direction', 'is_ai_draft',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('body', 'author_label', 'ticket__reference',
                     'ticket__subject')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('ticket', 'sent_message')
    readonly_fields = ('created_at',)
    empty_value_display = '--'

    @admin.display(description='Message')
    def preview_column(self, obj):
        return obj.preview

    @admin.display(description='Sent', boolean=True)
    def sent_flag(self, obj):
        return obj.was_sent

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('ticket', 'sent_message'))


@admin.register(SupportReport)
class SupportReportAdmin(admin.ModelAdmin):
    list_display = ('title', 'kind', 'period_column', 'metric_summary',
                    'created_by_agent', 'created_at')
    list_filter = ('kind', ('created_at', admin.DateFieldListFilter))
    search_fields = ('title', 'body')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('created_by_agent',)
    readonly_fields = ('created_at', 'metric_block')
    empty_value_display = '--'

    @admin.display(description='Period', ordering='period_start')
    def period_column(self, obj):
        return obj.period_label

    @admin.display(description='Metrics')
    def metric_summary(self, obj):
        return _count_label(obj.metrics, 'metric')

    @admin.display(description='Recorded metrics')
    def metric_block(self, obj):
        return _key_value_table(sorted((obj.metrics or {}).items()),
                                empty='No metrics were recorded.')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('created_by_agent')


@admin.register(TicketTag)
class TicketTagAdmin(admin.ModelAdmin):
    list_display = ('tag_swatch', 'name', 'description', 'ticket_column')
    list_display_links = ('tag_swatch', 'name')
    search_fields = ('name', 'description')
    readonly_fields = ('ticket_column',)
    empty_value_display = '--'

    @admin.display(description='')
    def tag_swatch(self, obj):
        return _pill(obj.name, obj.colour or _GREY)

    @admin.display(description='Tickets')
    def ticket_column(self, obj):
        return obj.ticket_count


# ===========================================================================
# Knowledge: the rest
# ===========================================================================

@admin.register(KnowledgeSource)
class KnowledgeSourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'kind', 'integration_key', 'sync_mode',
                    'is_enabled', 'document_count', 'last_synced_at')
    list_filter = ('kind', 'sync_mode', 'is_enabled')
    # search_fields is required here because KnowledgeDocument autocompletes it.
    search_fields = ('name', 'root_reference', 'integration_key')
    list_editable = ('is_enabled',)
    readonly_fields = ('document_count', 'last_synced_at', 'last_sync_message',
                       'created_at')
    empty_value_display = 'Never'


@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display = ('document', 'ordinal', 'heading', 'word_count',
                    'keyword_summary', 'text_preview')
    list_filter = ('document__doc_type', 'document__department')
    search_fields = ('text', 'heading', 'document__title')
    autocomplete_fields = ('document',)
    empty_value_display = '--'

    @admin.display(description='Keywords')
    def keyword_summary(self, obj):
        return _count_label(obj.keywords, 'keyword')

    @admin.display(description='Passage')
    def text_preview(self, obj):
        return _plain(obj.text, 140)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('document')


@admin.register(ResearchReport)
class ResearchReportAdmin(admin.ModelAdmin):
    list_display = ('title', 'kind', 'agent', 'confidence_pill',
                    'citation_column', 'conflict_summary', 'created_at')
    list_filter = ('kind', 'agent', ('created_at', admin.DateFieldListFilter))
    # search_fields is required here because ResearchCitation autocompletes it.
    search_fields = ('title', 'question', 'summary', 'body')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('agent', 'requested_by', 'requested_by_agent',
                           'delegation')
    readonly_fields = ('created_at', 'citation_column', 'coverage_note')
    empty_value_display = '--'

    @admin.display(description='Confidence', ordering='confidence')
    def confidence_pill(self, obj):
        value = float(obj.confidence or 0)
        colour = '#047857' if value >= 0.7 else '#b45309' if value >= 0.4 else '#b91c1c'
        return _pill(f'{value:.2f}', colour)

    @admin.display(description='Citations')
    def citation_column(self, obj):
        count = obj.citation_count
        return _pill(str(count), '#047857' if count else '#b91c1c')

    @admin.display(description='Conflicts')
    def conflict_summary(self, obj):
        return _count_label(obj.conflicts, 'conflict')

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('agent', 'requested_by', 'requested_by_agent',
                                'delegation'))


@admin.register(ResearchCitation)
class ResearchCitationAdmin(admin.ModelAdmin):
    list_display = ('report', 'ordinal', 'source_column', 'relevance_pill',
                    'quote_preview')
    list_filter = ('document__doc_type',)
    search_fields = ('source_label', 'quote', 'url', 'report__title',
                     'document__title')
    autocomplete_fields = ('report', 'document')
    empty_value_display = '--'

    @admin.display(description='Source', ordering='document__title')
    def source_column(self, obj):
        if obj.document_id:
            return obj.document.title
        return obj.source_label or obj.url or 'unattributed'

    @admin.display(description='Relevance', ordering='relevance')
    def relevance_pill(self, obj):
        value = float(obj.relevance or 0)
        colour = '#047857' if value >= 0.6 else '#b45309' if value >= 0.3 else _FAINT
        return _pill(f'{value:.2f}', colour)

    @admin.display(description='Quote')
    def quote_preview(self, obj):
        return _plain(obj.quote, 140)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('report', 'document')


# ===========================================================================
# Marketing content: the rest
# ===========================================================================

@admin.register(CampaignBrief)
class CampaignBriefAdmin(admin.ModelAdmin):
    list_display = ('campaign', 'audience', 'status_badge', 'channel_summary',
                    'proof_summary', 'grounded_flag', 'start_date', 'end_date')
    list_filter = ('status', ('start_date', admin.DateFieldListFilter))
    # search_fields is required here because ContentPiece autocompletes it.
    search_fields = ('objective', 'audience', 'key_message',
                     'campaign__name')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('campaign', 'created_by_agent')
    readonly_fields = ('created_at', 'updated_at', 'unsourced_panel')
    empty_value_display = '--'

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Channels')
    def channel_summary(self, obj):
        return _count_label(obj.channels, 'channel')

    @admin.display(description='Proof points')
    def proof_summary(self, obj):
        return _count_label(obj.proof_points, 'proof point')

    @admin.display(description='Grounded', boolean=True)
    def grounded_flag(self, obj):
        return obj.is_grounded

    @admin.display(description='Claims with no source behind them')
    def unsourced_panel(self, obj):
        claims = obj.unsourced_claims
        if not claims:
            return 'Every claim in this brief has a proof point behind it.'
        return format_html_join(mark_safe('<br>'), '&bull; {}',
                                ((_plain(claim, 200),) for claim in claims))

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('campaign', 'created_by_agent'))


@admin.register(ContentCalendarEntry)
class ContentCalendarEntryAdmin(admin.ModelAdmin):
    list_display = ('date', 'slot', 'channel_column', 'title', 'owner',
                    'status_badge', 'draft_flag', 'campaign', 'status')
    list_filter = ('channel', 'status', 'slot',
                   ('date', admin.DateFieldListFilter))
    search_fields = ('title', 'notes', 'owner', 'campaign__name')
    date_hierarchy = 'date'
    list_editable = ('status',)
    autocomplete_fields = ('content', 'campaign', 'created_by_agent')
    readonly_fields = ('created_at',)
    empty_value_display = '--'

    @admin.display(description='Channel', ordering='channel')
    def channel_column(self, obj):
        name, colour = CHANNEL_ICONS.get(obj.channel, ('fa-share-nodes', _GREY))
        return format_html('{} {}', _icon(name, colour), obj.channel_display)

    @admin.display(description='State', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Draft ready', boolean=True)
    def draft_flag(self, obj):
        return obj.has_draft

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('content', 'campaign', 'created_by_agent'))


@admin.register(MarketingEmail)
class MarketingEmailAdmin(admin.ModelAdmin):
    list_display = ('name', 'kind', 'subject_column', 'status_badge',
                    'audience_column', 'recipient_count', 'open_rate',
                    'click_rate', 'sent_at')
    list_filter = ('kind', 'status', 'campaign',
                   ('sent_at', admin.DateFieldListFilter))
    search_fields = ('name', 'subject', 'preheader', 'body',
                     'audience_segment')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('campaign', 'created_by_agent')
    readonly_fields = ('created_at', 'updated_at', 'sent_at', 'recipient_count')
    empty_value_display = '--'

    @admin.display(description='Subject', ordering='subject')
    def subject_column(self, obj):
        length = obj.subject_length
        if obj.subject_length_ok:
            return format_html('{} {}', obj.subject or '(none)',
                               _pill(f'{length} chars', '#047857'))
        return format_html('{} {}', obj.subject or '(none)',
                           _badge(f'{length} chars -- will truncate', '#b45309'))

    @admin.display(description='Status', ordering='status')
    def status_badge(self, obj):
        return _generic_badge(obj)

    @admin.display(description='Audience')
    def audience_column(self, obj):
        return obj.audience_summary

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('campaign', 'created_by_agent'))


@admin.register(AudienceSegment)
class AudienceSegmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'criteria_column', 'estimated_size',
                    'channel_summary', 'created_by_agent', 'created_at')
    list_filter = (('created_at', admin.DateFieldListFilter),)
    search_fields = ('name', 'description')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('created_by_agent', 'created_by')
    readonly_fields = ('created_at', 'criteria_block')
    empty_value_display = '--'

    @admin.display(description='Criteria')
    def criteria_column(self, obj):
        return obj.criteria_summary

    @admin.display(description='Channel fit')
    def channel_summary(self, obj):
        return _count_label(obj.channel_fit, 'channel')

    @admin.display(description='Criteria in full')
    def criteria_block(self, obj):
        return _json_block(obj.criteria, empty='No criteria were recorded.')

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('created_by_agent', 'created_by'))
