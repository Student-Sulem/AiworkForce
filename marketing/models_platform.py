"""Platform layer: integrations, capabilities, tasks, approvals, memory, audit.

This module holds the machinery that turns six chat personas into six AI
employees. Nothing here is domain specific; the domain models live in
models_hr, models_eng, models_support, models_knowledge and models_content.

WHY THESE MODELS EXIST
----------------------
``Integration``      one connected company application (Gmail, Slack, Jira...).
                     Everything about a connection -- endpoint, credential,
                     behaviour -- is a database row, so an integration can be
                     configured from the web interface *or* by an AI employee
                     calling a configuration tool. Nothing needs a code edit
                     and nothing needs an environment variable.

``AgentCapability``  one named thing an employee can do, linked to the tool
                     that does it. Rows are generated from the tool registry,
                     so the roster page is never out of step with reality.

``AgentTask``        a unit of work an employee is doing. Chat is one way a
                     task starts; the orchestrator and delegation are others.

``AgentDelegation``  one employee asking another for help, with the answer.

``ProposedAction``   the centre of the governance rule. Any tool that would
                     touch the outside world stops here as a pending row with
                     an editable payload. A person approves, edits or rejects
                     it; only approval executes it.

``OutboundMessage``  ``CalendarEvent``  ``ExternalIssue``
                     records of what execution actually did, whether live or
                     simulated, so the audit trail points at something real.

``AgentMemory``      what an employee remembers between conversations.

``AuditEvent``       the append-only history of everything that happened.

``SystemSetting``    global configuration as data, editable from the UI and by
                     the platform-administration tools.

TENANCY
-------
As with the rest of the application the workspace is shared: one organisation,
one roster, one approval queue. ``created_by`` columns record provenance, not
an access boundary. What a person may do is decided by their role; see
marketing/roles.py.
"""

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .models import AIAgent, ChatMessage, Conversation


# ===========================================================================
# 1. INTEGRATIONS -- the connected company applications
# ===========================================================================

class Integration(models.Model):
    """One company application the workforce can act through.

    A connector class in marketing/integrations/ supplies the behaviour; this
    row supplies the configuration. The split matters: the connector is code
    and ships with the project, the configuration is data and is entered by a
    person on the Integrations page or by an AI employee calling
    ``platform.configure_integration``.

    ``config`` holds settings that are safe to display (a Slack channel, a
    GitHub repository, a Drive folder id). ``secrets`` holds credentials and is
    never rendered back to the browser -- the serialiser in views_platform
    replaces every value with a masked placeholder.
    """

    CATEGORY_CHOICES = [
        ('communication', 'Communication'),
        ('calendar', 'Calendar'),
        ('documents', 'Documents & Knowledge'),
        ('development', 'Development'),
        ('social', 'Social & Marketing'),
        ('internal', 'Internal Platform'),
    ]

    MODE_CHOICES = [
        ('auto', 'Automatic -- live when configured, demo when not'),
        ('live', 'Live only -- fail rather than simulate'),
        ('demo', 'Demo only -- always simulate'),
    ]

    STATUS_CHOICES = [
        ('unknown', 'Never Checked'),
        ('connected', 'Connected'),
        ('demo', 'Demo Mode'),
        ('degraded', 'Degraded'),
        ('failed', 'Connection Failed'),
        ('disabled', 'Disabled'),
    ]

    provider_key = models.SlugField(
        max_length=40, unique=True,
        help_text="Matches a connector in marketing/integrations, e.g. 'gmail'.")
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='internal')
    icon = models.CharField(max_length=50, default='fa-plug')
    color = models.CharField(max_length=20, default='#4f46e5')

    is_enabled = models.BooleanField(default=True)
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default='auto')

    config = models.JSONField(
        default=dict, blank=True,
        help_text='Non-secret settings, shown in the interface.')
    secrets = models.JSONField(
        default=dict, blank=True,
        help_text='Credentials. Never sent back to the browser in clear text.')

    connection_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unknown')
    last_status_message = models.CharField(max_length=400, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    call_count = models.PositiveIntegerField(default=0)
    live_call_count = models.PositiveIntegerField(default=0)
    demo_call_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)

    configured_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='configured_integrations')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'name']
        verbose_name = 'Integration'
        verbose_name_plural = 'Integrations'
        permissions = [
            ('configure_integration', 'Can configure a connected application'),
            ('test_integration', 'Can run the connection test on an integration'),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_connection_status_display()})"

    # -- connector access --------------------------------------------------

    @property
    def connector(self):
        """The connector object that gives this row its behaviour, or None."""
        from .integrations import get_connector
        return get_connector(self)

    @property
    def is_configured(self):
        connector = self.connector
        return bool(connector and connector.is_configured())

    @property
    def runs_live(self):
        """Whether a call right now would reach the real service."""
        if not self.is_enabled or self.mode == 'demo':
            return False
        return self.is_configured

    @property
    def effective_mode(self):
        if not self.is_enabled:
            return 'disabled'
        if self.mode == 'demo':
            return 'demo'
        if self.mode == 'live':
            return 'live' if self.is_configured else 'unavailable'
        return 'live' if self.is_configured else 'demo'

    @property
    def missing_settings(self):
        """Names of the required fields still empty, for the interface."""
        connector = self.connector
        return list(connector.missing_settings()) if connector else []

    def record_call(self, *, demo):
        """Bump the usage counters. Called by the integration base class."""
        Integration.objects.filter(pk=self.pk).update(
            call_count=models.F('call_count') + 1,
            live_call_count=models.F('live_call_count') + (0 if demo else 1),
            demo_call_count=models.F('demo_call_count') + (1 if demo else 0),
            last_used_at=timezone.now(),
        )


# ===========================================================================
# 2. CAPABILITIES -- what each employee can do
# ===========================================================================

class AgentCapability(models.Model):
    """One named capability belonging to one AI employee.

    Rows are provisioned from the tool registry rather than written by hand,
    which is what keeps the roster page honest: a capability appears on the
    page because a tool exists to perform it.
    """

    agent = models.ForeignKey(AIAgent, on_delete=models.CASCADE, related_name='capabilities')
    group = models.CharField(max_length=60, help_text="Heading, e.g. 'Recruitment'.")
    name = models.CharField(max_length=140)
    slug = models.SlugField(max_length=140)
    description = models.TextField(blank=True)
    tool_name = models.CharField(
        max_length=80, blank=True,
        help_text="Registry name of the tool that performs it, e.g. 'hr.create_job_opening'.")
    integration_key = models.CharField(max_length=40, blank=True)
    requires_approval = models.BooleanField(default=False)
    is_enabled = models.BooleanField(default=True)
    usage_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['agent', 'display_order', 'group', 'name']
        verbose_name = 'Agent Capability'
        verbose_name_plural = 'Agent Capabilities'
        constraints = [
            models.UniqueConstraint(fields=['agent', 'slug'], name='uniq_capability_per_agent'),
        ]

    def __str__(self):
        return f"{self.agent.name}: {self.name}"


# ===========================================================================
# 3. TASKS AND DELEGATION -- how work moves
# ===========================================================================

class AgentTask(models.Model):
    """A unit of work an AI employee is doing or has done.

    Every tool call happens inside a task, which is what makes the audit trail
    answerable: not just 'an email was drafted' but 'an email was drafted while
    working on the task that came from this request'.
    """

    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('running', 'In Progress'),
        ('waiting_approval', 'Waiting for Approval'),
        ('blocked', 'Blocked'),
        ('done', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    ORIGIN_CHOICES = [
        ('chat', 'Chat Request'),
        ('orchestrator', 'Routed by the Orchestrator'),
        ('delegation', 'Delegated by Another Employee'),
        ('manual', 'Created by Hand'),
        ('approval', 'Follow-up to an Approval'),
    ]
    PRIORITY_CHOICES = [('low', 'Low'), ('normal', 'Normal'),
                        ('high', 'High'), ('urgent', 'Urgent')]

    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='tasks')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    origin = models.CharField(max_length=20, choices=ORIGIN_CHOICES, default='chat')
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='normal')

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='agent_tasks')
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='tasks')
    source_message = models.ForeignKey(ChatMessage, on_delete=models.SET_NULL, null=True,
                                       blank=True, related_name='tasks')
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='subtasks')

    steps = models.JSONField(
        default=list, blank=True,
        help_text='Ordered record of what the employee did, one entry per tool call.')
    result_summary = models.TextField(blank=True)
    error_message = models.TextField(blank=True)

    tool_calls = models.PositiveIntegerField(default=0)
    tokens_used = models.PositiveIntegerField(default=0)

    due_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Agent Task'
        verbose_name_plural = 'Agent Tasks'
        indexes = [
            models.Index(fields=['status', '-created_at'], name='task_status_created_idx'),
        ]

    def __str__(self):
        return f"[{self.get_status_display()}] {self.title}"

    @property
    def is_open(self):
        return self.status in ('queued', 'running', 'waiting_approval', 'blocked')

    def add_step(self, label, detail='', outcome='ok'):
        """Append one line to the visible working record."""
        self.steps = list(self.steps or []) + [{
            'at': timezone.now().isoformat(timespec='seconds'),
            'label': label,
            'detail': str(detail)[:600],
            'outcome': outcome,
        }]
        self.save(update_fields=['steps'])

    def finish(self, summary='', status='done'):
        self.status = status
        self.result_summary = summary or self.result_summary
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'result_summary', 'completed_at'])


class AgentDelegation(models.Model):
    """One employee asking another for help.

    The Marketing employee needing product facts does not guess them: it asks
    the Research employee, which searches the knowledge base and answers with
    citations. This row is that exchange, kept so the provenance of a claim in
    a published post can be traced back to the document it came from.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('answered', 'Answered'),
        ('failed', 'Failed'),
    ]

    from_agent = models.ForeignKey(AIAgent, on_delete=models.CASCADE,
                                   related_name='delegations_made')
    to_agent = models.ForeignKey(AIAgent, on_delete=models.CASCADE,
                                 related_name='delegations_received')
    task = models.ForeignKey(AgentTask, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='delegations')
    request = models.TextField()
    response = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    sources = models.JSONField(default=list, blank=True)
    tokens_used = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Agent Delegation'
        verbose_name_plural = 'Agent Delegations'

    def __str__(self):
        return f"{self.from_agent.name} -> {self.to_agent.name}"


class OrchestratorDecision(models.Model):
    """Which employee the orchestrator chose for a request, and why.

    Recorded because routing is a decision the system makes on the user's
    behalf, and a decision made on someone's behalf should be inspectable.
    """

    request_text = models.TextField()
    chosen_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='routed_requests')
    reasoning = models.TextField(blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=0)
    method = models.CharField(
        max_length=20, default='rules',
        help_text="'rules' for keyword scoring, 'llm' when a model decided.")
    candidates = models.JSONField(default=list, blank=True)
    task = models.ForeignKey(AgentTask, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='routing')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='routing_decisions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Orchestrator Decision'
        verbose_name_plural = 'Orchestrator Decisions'

    def __str__(self):
        name = self.chosen_agent.name if self.chosen_agent else 'nobody'
        return f"routed to {name}"


# ===========================================================================
# 4. GOVERNANCE -- the proposed action queue
# ===========================================================================

class ProposedAction(models.Model):
    """An action an AI employee wants to take, waiting for a person.

    THE RULE THIS MODEL ENFORCES
    ----------------------------
    A tool marked ``requires_approval`` never performs its action. It writes
    one of these rows and stops. The payload is the complete argument set the
    execution will use, stored as JSON so a reviewer can edit any field before
    approving. Editing does not approve: the row stays pending, the edit is
    recorded, and a second deliberate act releases it.

    ``original_payload`` keeps the employee's own version, so the difference
    between what was proposed and what was sent is always recoverable.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('executing', 'Executing'),
        ('executed', 'Executed'),
        ('failed', 'Execution Failed'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]
    RISK_CHOICES = [
        ('low', 'Low -- internal only'),
        ('medium', 'Medium -- changes a company system'),
        ('high', 'High -- reaches someone outside the company'),
    ]

    action_type = models.CharField(
        max_length=80,
        help_text="Registry name of the tool that will execute it, e.g. 'gmail.send_email'.")
    title = models.CharField(max_length=250)
    summary = models.TextField(blank=True)

    payload = models.JSONField(
        default=dict, help_text='The arguments execution will use. Editable by a reviewer.')
    original_payload = models.JSONField(
        default=dict, blank=True, help_text="The employee's own version, kept for comparison.")
    editable_fields = models.JSONField(
        default=list, blank=True,
        help_text='Payload keys a reviewer may change, with their form types.')

    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='proposed_actions')
    integration = models.ForeignKey(Integration, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='proposed_actions')
    task = models.ForeignKey(AgentTask, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='proposed_actions')
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='proposed_actions')
    source_message = models.ForeignKey(ChatMessage, on_delete=models.SET_NULL, null=True,
                                       blank=True, related_name='proposed_actions')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='proposed_actions')

    # A soft reference to the domain object this action concerns, written as
    # 'app_label.modelname' plus a primary key. Deliberately not a
    # GenericForeignKey: an action can outlive its subject, and a dangling
    # label that still reads correctly in the audit trail is better than a
    # cascade that erases the history of what was approved.
    subject_label = models.CharField(max_length=60, blank=True)
    subject_id = models.PositiveIntegerField(null=True, blank=True)
    subject_display = models.CharField(max_length=200, blank=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='pending')
    risk = models.CharField(max_length=10, choices=RISK_CHOICES, default='medium')
    edit_count = models.PositiveIntegerField(default=0)

    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='action_decisions')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)

    executed_at = models.DateTimeField(null=True, blank=True)
    execution_summary = models.TextField(blank=True)
    execution_result = models.JSONField(default=dict, blank=True)
    executed_in_demo = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Proposed Action'
        verbose_name_plural = 'Proposed Actions'
        indexes = [
            models.Index(fields=['status', '-created_at'], name='action_status_created_idx'),
            models.Index(fields=['action_type'], name='action_type_idx'),
        ]
        permissions = [
            ('approve_proposedaction', 'Can approve, edit or reject a proposed action'),
        ]

    def __str__(self):
        return f"[{self.get_status_display()}] {self.title}"

    @property
    def is_pending(self):
        return self.status == 'pending'

    @property
    def was_edited(self):
        return self.edit_count > 0

    @property
    def tool(self):
        from .tools import get_tool
        return get_tool(self.action_type)

    @property
    def target_app(self):
        return self.integration.name if self.integration else 'AI Workforce'

    @property
    def icon(self):
        tool = self.tool
        return tool.icon if tool else 'fa-bolt'

    @property
    def age_hours(self):
        return round((timezone.now() - self.created_at).total_seconds() / 3600, 1)

    @property
    def decision_summary(self):
        if self.status == 'pending':
            return 'Awaiting review'
        who = self.decided_by.get_username() if self.decided_by else 'system'
        when = self.decided_at.strftime('%d %b %Y at %H:%M') if self.decided_at else 'unknown time'
        return f"{self.get_status_display()} by {who} on {when}"

    @property
    def payload_diff(self):
        """Fields a reviewer changed, as (key, proposed, current) triples."""
        original = self.original_payload or {}
        rows = []
        for key, value in (self.payload or {}).items():
            was = original.get(key)
            if was != value:
                rows.append((key, was, value))
        return rows


class ActionAuditTrail(models.Model):
    """Everything that happened to one proposed action, in order."""

    ACTION_CHOICES = [
        ('proposed', 'Proposed by an AI Employee'),
        ('edited', 'Edited by a Reviewer'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
        ('executed', 'Executed'),
        ('failed', 'Execution Failed'),
    ]

    action = models.ForeignKey(ProposedAction, on_delete=models.CASCADE,
                               related_name='trail')
    event = models.CharField(max_length=12, choices=ACTION_CHOICES)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='action_trail_entries')
    note = models.TextField(blank=True)
    changes = models.JSONField(default=dict, blank=True)
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Action Trail Entry'
        verbose_name_plural = 'Action Trail'

    def __str__(self):
        return f"{self.get_event_display()} on #{self.action_id}"


# ===========================================================================
# 5. EXECUTION RECORDS -- what the approved action actually did
# ===========================================================================

class OutboundMessage(models.Model):
    """A message the platform sent out, live or simulated.

    One table for every channel because the question a reviewer asks is
    'what did we send, to whom, and did it leave the building' -- and that
    question does not change between email and Slack.
    """

    CHANNEL_CHOICES = [
        ('email', 'Email'),
        ('slack', 'Slack'),
        ('linkedin', 'LinkedIn'),
        ('instagram', 'Instagram'),
        ('sms', 'SMS'),
    ]
    STATUS_CHOICES = [
        ('sent', 'Sent'),
        ('simulated', 'Simulated (Demo)'),
        ('failed', 'Failed'),
    ]

    channel = models.CharField(max_length=15, choices=CHANNEL_CHOICES)
    recipient = models.CharField(max_length=400, blank=True)
    subject = models.CharField(max_length=300, blank=True)
    body = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='simulated')
    external_id = models.CharField(max_length=200, blank=True)
    external_url = models.CharField(max_length=400, blank=True)
    error_message = models.TextField(blank=True)

    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='outbound_messages')
    action = models.ForeignKey(ProposedAction, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='messages')
    integration = models.ForeignKey(Integration, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='messages')
    metadata = models.JSONField(default=dict, blank=True)
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-sent_at']
        verbose_name = 'Outbound Message'
        verbose_name_plural = 'Outbound Messages'

    def __str__(self):
        return f"{self.get_channel_display()} to {self.recipient or 'unknown'}"


class CalendarEvent(models.Model):
    """A meeting the platform put on a calendar, live or simulated."""

    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('simulated', 'Simulated (Demo)'),
        ('rescheduled', 'Rescheduled'),
        ('cancelled', 'Cancelled'),
    ]

    title = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(default=45)
    attendees = models.JSONField(default=list, blank=True)
    location = models.CharField(max_length=300, blank=True)
    meeting_link = models.CharField(max_length=400, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='simulated')
    external_id = models.CharField(max_length=200, blank=True)

    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='calendar_events')
    action = models.ForeignKey(ProposedAction, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='calendar_events')
    subject_label = models.CharField(max_length=60, blank=True)
    subject_id = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start_at']
        verbose_name = 'Calendar Event'
        verbose_name_plural = 'Calendar Events'

    def __str__(self):
        return f"{self.title} at {self.start_at:%d %b %H:%M}"


class ExternalIssue(models.Model):
    """A GitHub or Jira item the platform created or updated."""

    SYSTEM_CHOICES = [('github', 'GitHub'), ('jira', 'Jira')]
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('simulated', 'Simulated (Demo)'),
        ('in_progress', 'In Progress'),
        ('closed', 'Closed'),
    ]

    system = models.CharField(max_length=10, choices=SYSTEM_CHOICES)
    container = models.CharField(
        max_length=200, blank=True,
        help_text='Repository for GitHub, project key for Jira.')
    reference = models.CharField(max_length=60, blank=True,
                                 help_text="'#42' or 'ENG-118'.")
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True)
    issue_status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='simulated')
    assignee = models.CharField(max_length=120, blank=True)
    priority = models.CharField(max_length=30, blank=True)
    labels = models.JSONField(default=list, blank=True)
    url = models.CharField(max_length=400, blank=True)
    external_id = models.CharField(max_length=200, blank=True)

    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='external_issues')
    action = models.ForeignKey(ProposedAction, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='external_issues')
    subject_label = models.CharField(max_length=60, blank=True)
    subject_id = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'External Issue'
        verbose_name_plural = 'External Issues'

    def __str__(self):
        return f"{self.get_system_display()} {self.reference or ''} {self.title}".strip()


# ===========================================================================
# 6. MEMORY -- what an employee carries between conversations
# ===========================================================================

class AgentMemory(models.Model):
    """One durable thing an employee knows.

    Without this an employee is a stateless chat window. With it, the HR
    employee still knows next quarter's headcount plan in a conversation
    started a week later, and the Support employee still knows that a named
    customer has an open escalation.
    """

    KIND_CHOICES = [
        ('fact', 'Fact'),
        ('preference', 'Preference'),
        ('entity', 'Entity'),
        ('summary', 'Conversation Summary'),
        ('policy', 'Working Rule'),
    ]
    SCOPE_CHOICES = [
        ('agent', 'This Employee Only'),
        ('shared', 'Shared Across the Workforce'),
    ]

    agent = models.ForeignKey(AIAgent, on_delete=models.CASCADE, null=True, blank=True,
                              related_name='memories',
                              help_text='Null for a memory shared by the whole workforce.')
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, default='agent')
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default='fact')
    key = models.CharField(max_length=160)
    content = models.TextField()
    importance = models.PositiveSmallIntegerField(default=5,
                                                  help_text='1 is trivia, 10 is essential.')
    source = models.CharField(max_length=200, blank=True)
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='memories')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='agent_memories')
    recall_count = models.PositiveIntegerField(default=0)
    last_recalled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-importance', '-updated_at']
        verbose_name = 'Agent Memory'
        verbose_name_plural = 'Agent Memory'
        constraints = [
            models.UniqueConstraint(fields=['agent', 'key'], name='uniq_memory_key_per_agent'),
        ]

    def __str__(self):
        return f"{self.key}"


# ===========================================================================
# 7. AUDIT -- the append-only history of the whole platform
# ===========================================================================

class AuditEvent(models.Model):
    """One thing that happened, recorded so it can be answered for.

    Written by marketing/audit.py rather than by callers directly, so the
    shape of an entry is decided in one place.
    """

    CATEGORY_CHOICES = [
        ('agent', 'AI Employee'),
        ('tool', 'Tool Call'),
        ('approval', 'Approval'),
        ('execution', 'Execution'),
        ('integration', 'Integration'),
        ('knowledge', 'Knowledge Access'),
        ('data', 'Record Change'),
        ('auth', 'Access'),
        ('system', 'System'),
    ]
    STATUS_CHOICES = [
        ('ok', 'Succeeded'),
        ('demo', 'Simulated'),
        ('failed', 'Failed'),
        ('denied', 'Denied'),
        ('pending', 'Pending'),
    ]

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='audit_events')
    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='audit_events')
    category = models.CharField(max_length=15, choices=CATEGORY_CHOICES, default='system')
    action = models.CharField(max_length=90,
                              help_text="Dotted event name, e.g. 'action.approved'.")
    message = models.CharField(max_length=400, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='ok')

    target_app = models.CharField(max_length=60, blank=True)
    object_label = models.CharField(max_length=200, blank=True)
    object_ref = models.CharField(max_length=60, blank=True)

    task = models.ForeignKey(AgentTask, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='audit_events')
    proposed_action = models.ForeignKey(ProposedAction, on_delete=models.SET_NULL, null=True,
                                        blank=True, related_name='audit_events')
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='audit_events')
    integration = models.ForeignKey(Integration, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='audit_events')

    detail = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Audit Event'
        verbose_name_plural = 'Audit Log'
        indexes = [
            models.Index(fields=['category', '-created_at'], name='audit_cat_created_idx'),
            models.Index(fields=['action'], name='audit_action_idx'),
        ]

    def __str__(self):
        return f"{self.action} ({self.get_status_display()})"

    @property
    def actor_label(self):
        if self.agent_id:
            return self.agent.name
        if self.actor_id:
            return self.actor.get_username()
        return 'system'


# ===========================================================================
# 8. SYSTEM SETTINGS -- global configuration as data
# ===========================================================================

class SystemSetting(models.Model):
    """One global setting, editable in the interface and by the agents.

    The point of this table is the project's promise that nothing requires a
    code edit or an environment variable. Company name, default demo
    behaviour, working hours, signature block -- all of it lives here.
    """

    GROUP_CHOICES = [
        ('company', 'Company Profile'),
        ('behaviour', 'Workforce Behaviour'),
        ('approval', 'Approval Policy'),
        ('knowledge', 'Knowledge Base'),
        ('notification', 'Notifications'),
    ]
    TYPE_CHOICES = [
        ('text', 'Text'),
        ('longtext', 'Long Text'),
        ('number', 'Number'),
        ('boolean', 'Yes / No'),
        ('choice', 'Choice'),
        ('json', 'JSON'),
    ]

    key = models.SlugField(max_length=60, unique=True)
    label = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    group = models.CharField(max_length=15, choices=GROUP_CHOICES, default='company')
    value_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='text')
    value = models.JSONField(default=dict, blank=True)
    choices = models.JSONField(default=list, blank=True)
    is_secret = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=0)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='setting_changes')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['group', 'display_order', 'label']
        verbose_name = 'System Setting'
        verbose_name_plural = 'System Settings'

    def __str__(self):
        return f"{self.label}"

    @property
    def resolved(self):
        """The stored value, unwrapped from its JSON envelope."""
        if isinstance(self.value, dict) and 'v' in self.value:
            return self.value['v']
        return self.value
