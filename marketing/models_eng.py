"""Engineering domain: projects, sprints, work items and developer artefacts.

WHY THIS LAYER EXISTS SEPARATELY
--------------------------------
The platform layer (models_platform) knows about integrations, approvals and
audit. It deliberately knows nothing about software delivery. This module is
the delivery domain: the things the Engineering Manager employee plans with
and the things the Developer employee produces.

The split matters for one practical reason. A work item here is the company's
own record of a piece of work, and it exists whether or not anybody ever files
it in Jira. Filing it externally is a separate, approved act which writes an
``ExternalIssue`` row and stamps ``external_reference`` back onto the item. So
the internal plan is never hostage to a connected application being configured,
and the platform can say honestly which items have left the building and which
have not.

WHERE THE SAFETY BOUNDARY SITS
------------------------------
``CodeArtifact`` is the boundary for the Developer employee, and it is worth
being blunt about it: the Developer has no tool that touches a working tree.
Everything it writes -- code, tests, documentation, fixes -- becomes a row in
that table, as text, for a human developer to judge. See the model's own
docstring.

TENANCY
-------
As with the rest of the application there is one organisation and one shared
workspace. ``created_by`` and ``created_by_agent`` record provenance, not an
access boundary.
"""

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .models import AIAgent
from .models_hr import Employee


# ===========================================================================
# 1. PROJECTS -- the container everything else hangs from
# ===========================================================================

class Project(models.Model):
    """One body of engineering work with a name, an owner and a deadline.

    ``key`` is the short prefix that gives every work item a human-quotable
    reference ('ENG-42' rather than 'work item 42'). It is derived from the
    name when nobody supplies one, and kept unique, because a reference that
    collides is worse than no reference at all -- two people would read the
    same string as two different items.

    ``repository`` and ``jira_project_key`` are where this project lives
    externally. They are stored rather than guessed so that a tool filing an
    issue for an item knows the destination without being told again.
    """

    STATUS_CHOICES = [
        ('planning', 'Planning'),
        ('active', 'Active'),
        ('on_hold', 'On Hold'),
        ('complete', 'Complete'),
        ('cancelled', 'Cancelled'),
    ]

    name = models.CharField(max_length=200)
    key = models.CharField(
        max_length=12, unique=True, blank=True,
        help_text="Short prefix for work item references, e.g. 'ENG'.")
    description = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='planning')

    owner = models.CharField(max_length=120, blank=True,
                             help_text='Person accountable for delivery.')
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)

    repository = models.CharField(
        max_length=200, blank=True,
        help_text="GitHub repository as 'owner/name'.")
    jira_project_key = models.CharField(max_length=20, blank=True)
    tech_stack = models.JSONField(default=list, blank=True,
                                  help_text='List of technologies in use.')

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='eng_projects')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='eng_projects')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['status', 'name']
        verbose_name = 'Project'
        verbose_name_plural = 'Projects'
        indexes = [
            models.Index(fields=['status', 'name'], name='eng_project_status_idx'),
        ]

    def __str__(self):
        return f"{self.key or 'PRJ'}: {self.name}"

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = self._derive_key()
        self.key = self.key.upper()[:12]
        super().save(*args, **kwargs)

    def _derive_key(self):
        """A short, unique prefix from the project name.

        Initials of the first words when the name has several, otherwise the
        first letters of the single word. A numeric suffix breaks a tie rather
        than raising, because a project being created by an employee mid
        conversation should not fail on a name clash.
        """
        words = [w for w in ''.join(
            c if (c.isalnum() or c.isspace()) else ' ' for c in self.name).split() if w]
        if len(words) >= 2:
            base = ''.join(w[0] for w in words[:4]).upper()
        elif words:
            base = words[0][:4].upper()
        else:
            base = 'PRJ'
        base = base[:8] or 'PRJ'
        candidate, counter = base, 1
        while Project.objects.filter(key=candidate).exclude(pk=self.pk).exists():
            counter += 1
            candidate = f'{base}{counter}'[:12]
        return candidate

    # -- progress ----------------------------------------------------------

    @property
    def open_items(self):
        """Work items still to be finished. Cancelled work is not open."""
        return self.work_items.exclude(status__in=('done', 'cancelled')).count()

    @property
    def done_items(self):
        return self.work_items.filter(status='done').count()

    @property
    def progress_percent(self):
        """Completed share of the live work, cancelled items excluded.

        Cancelled work is left out of both halves deliberately: descoping
        something should not read as progress, and it should not read as debt
        either.
        """
        live = self.work_items.exclude(status='cancelled').count()
        if not live:
            return 0
        return int(round(self.done_items * 100.0 / live))

    @property
    def is_open(self):
        return self.status in ('planning', 'active', 'on_hold')


# ===========================================================================
# 2. SPRINTS -- a time box with a capacity
# ===========================================================================

class Sprint(models.Model):
    """One iteration: a goal, two dates and a point capacity.

    ``capacity_points`` is stored so that planning can be checked rather than
    asserted. A sprint plan that quietly exceeds the team's capacity is the
    normal way a sprint fails, so the planning tool compares committed points
    against this number and reports the overflow instead of hiding it.
    """

    STATUS_CHOICES = [
        ('planned', 'Planned'),
        ('active', 'Active'),
        ('closed', 'Closed'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='sprints')
    name = models.CharField(max_length=120)
    goal = models.TextField(blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='planned')
    capacity_points = models.PositiveIntegerField(
        default=0, help_text='Points the team believes it can finish. 0 means unstated.')

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='sprints')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-start_date', 'name']
        verbose_name = 'Sprint'
        verbose_name_plural = 'Sprints'

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    @property
    def committed_points(self):
        total = self.work_items.aggregate(total=models.Sum('estimate_points'))['total']
        return int(total or 0)

    @property
    def completed_points(self):
        total = self.work_items.filter(status='done').aggregate(
            total=models.Sum('estimate_points'))['total']
        return int(total or 0)

    @property
    def remaining_points(self):
        return max(self.committed_points - self.completed_points, 0)

    @property
    def is_current(self):
        """Whether today falls inside this sprint and it is running."""
        if self.status != 'active':
            return False
        today = timezone.localdate()
        if self.start_date and today < self.start_date:
            return False
        if self.end_date and today > self.end_date:
            return False
        return True

    @property
    def over_capacity_by(self):
        if not self.capacity_points:
            return 0
        return max(self.committed_points - self.capacity_points, 0)


# ===========================================================================
# 3. WORK ITEMS -- the unit of engineering work
# ===========================================================================

class WorkItem(models.Model):
    """One piece of work a developer could pick up and start on.

    THREE THINGS WORTH EXPLAINING
    -----------------------------
    ``reference`` is assigned in ``save`` as '<PROJECT KEY>-<pk>' because a
    work item needs a name a person can say out loud in a stand-up, and the
    primary key alone is not that. It is derived rather than entered so it can
    never disagree with the project it belongs to.

    ``depends_on`` is a non-symmetrical self relation: A depending on B does
    not mean B depends on A, and the reverse accessor is called ``blocks``,
    which is the way the question is actually asked ('what is this holding
    up?'). Sequencing and blocker detection both read this field.

    ``assignee`` and ``assignee_name`` exist together on purpose. An item can
    be assigned to a person who is not in the Employee table yet -- a
    contractor, a new starter, a name mentioned in a conversation -- and losing
    that information because the foreign key had nowhere to point would be
    worse than storing the string.
    """

    TYPE_CHOICES = [
        ('epic', 'Epic'),
        ('story', 'Story'),
        ('task', 'Task'),
        ('subtask', 'Subtask'),
        ('bug', 'Bug'),
        ('spike', 'Spike'),
        ('chore', 'Chore'),
    ]
    STATUS_CHOICES = [
        ('backlog', 'Backlog'),
        ('todo', 'To Do'),
        ('in_progress', 'In Progress'),
        ('in_review', 'In Review'),
        ('blocked', 'Blocked'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
    ]
    PRIORITY_CHOICES = [
        ('lowest', 'Lowest'),
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]
    COMPLEXITY_CHOICES = [
        ('trivial', 'Trivial'),
        ('small', 'Small'),
        ('moderate', 'Moderate'),
        ('large', 'Large'),
        ('unknown', 'Unknown -- needs a spike'),
    ]

    OPEN_STATUSES = ('backlog', 'todo', 'in_progress', 'in_review', 'blocked')
    PRIORITY_WEIGHT = {'critical': 5, 'high': 4, 'medium': 3, 'low': 2, 'lowest': 1}

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='work_items')
    sprint = models.ForeignKey(Sprint, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='work_items')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True,
                               related_name='children')

    reference = models.CharField(
        max_length=30, blank=True, db_index=True,
        help_text="Assigned automatically as '<PROJECT KEY>-<id>'.")
    title = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    acceptance_criteria = models.JSONField(
        default=list, blank=True,
        help_text='List of checkable statements that decide when this is done.')

    item_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='task')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='backlog')
    priority = models.CharField(max_length=8, choices=PRIORITY_CHOICES, default='medium')
    complexity = models.CharField(max_length=10, choices=COMPLEXITY_CHOICES, default='unknown')

    assignee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='work_items')
    assignee_name = models.CharField(
        max_length=140, blank=True,
        help_text='Used when the assignee is not an Employee record.')

    estimate_points = models.PositiveIntegerField(null=True, blank=True)
    estimate_hours = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    labels = models.JSONField(default=list, blank=True)
    sequence_hint = models.PositiveIntegerField(
        default=25,
        help_text='Suggested build order. Lower is earlier; data before API before UI.')

    depends_on = models.ManyToManyField('self', symmetrical=False, blank=True,
                                        related_name='blocks')
    blocked_reason = models.TextField(blank=True)

    external_reference = models.CharField(
        max_length=60, blank=True,
        help_text="Jira key or GitHub number, once this item has been filed.")
    external_url = models.CharField(max_length=400, blank=True)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='work_items')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['sequence_hint', '-priority', 'created_at']
        verbose_name = 'Work Item'
        verbose_name_plural = 'Work Items'
        indexes = [
            models.Index(fields=['project', 'status'], name='workitem_project_status_idx'),
            models.Index(fields=['status', 'due_date'], name='workitem_status_due_idx'),
        ]

    def __str__(self):
        return f"{self.reference or 'NEW'} {self.title}".strip()

    def save(self, *args, **kwargs):
        """Save, then stamp the reference once a primary key exists.

        The reference contains the primary key, so it cannot be built before
        the first insert. The second write is a targeted queryset update
        rather than another ``save`` so that it cannot recurse and cannot
        disturb ``updated_at``.
        """
        super().save(*args, **kwargs)
        prefix = (self.project.key if self.project_id else '') or 'ITEM'
        expected = f'{prefix}-{self.pk}'
        if self.reference != expected:
            WorkItem.objects.filter(pk=self.pk).update(reference=expected)
            self.reference = expected

    # -- state -------------------------------------------------------------

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_finished(self):
        return self.status in ('done', 'cancelled')

    @property
    def is_overdue(self):
        if not self.due_date or self.is_finished:
            return False
        return timezone.localdate() > self.due_date

    @property
    def days_overdue(self):
        if not self.is_overdue:
            return 0
        return (timezone.localdate() - self.due_date).days

    @property
    def assignee_label(self):
        """Who holds this item, whichever way it was recorded."""
        if self.assignee_id:
            return self.assignee.full_name
        return self.assignee_name or 'unassigned'

    @property
    def depth(self):
        """How many parents sit above this item.

        The walk is capped rather than trusted. A parent cycle should not be
        possible, but a property used by every listing page is the wrong place
        to discover that it is.
        """
        level, node, seen = 0, self, {self.pk}
        while node.parent_id and level < 10:
            node = node.parent
            if node.pk in seen:
                break
            seen.add(node.pk)
            level += 1
        return level

    @property
    def priority_weight(self):
        return self.PRIORITY_WEIGHT.get(self.priority, 3)

    @property
    def unmet_dependencies(self):
        """Dependencies that are not finished, and therefore still blocking."""
        return list(self.depends_on.exclude(status__in=('done', 'cancelled')))

    @property
    def is_filed_externally(self):
        return bool(self.external_reference)


class WorkItemComment(models.Model):
    """One note against a work item, from a person or an AI employee.

    Both authors are optional foreign keys rather than one text column,
    because the useful question later is 'did a person say this or did an
    employee say this', and a name string cannot answer it.
    """

    work_item = models.ForeignKey(WorkItem, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='work_item_comments')
    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='work_item_comments')
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Work Item Comment'
        verbose_name_plural = 'Work Item Comments'

    def __str__(self):
        return f"comment on {self.work_item_id} by {self.author_label}"

    @property
    def author_label(self):
        if self.agent_id:
            return self.agent.name
        if self.author_id:
            return self.author.get_username()
        return 'unknown'


# ===========================================================================
# 4. REPORTS -- what was true at a point in time
# ===========================================================================

class SprintReport(models.Model):
    """A status account, kept rather than only spoken.

    ``metrics`` holds the figures the body was written from, so a report can
    be checked against its own numbers months later. A report whose prose no
    longer matches the live data is not wrong -- it is a record of what was
    true then -- and keeping the metrics is what makes that distinction
    recoverable.
    """

    KIND_CHOICES = [
        ('sprint', 'Sprint Report'),
        ('project', 'Project Status'),
        ('weekly', 'Weekly Summary'),
        ('blockers', 'Blocker Report'),
    ]

    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='sprint')
    title = models.CharField(max_length=250)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='reports')
    sprint = models.ForeignKey(Sprint, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='reports')
    body = models.TextField()
    metrics = models.JSONField(default=dict, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='sprint_reports')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Engineering Report'
        verbose_name_plural = 'Engineering Reports'

    def __str__(self):
        return f"{self.get_kind_display()}: {self.title}"

    @property
    def line_count(self):
        return len([line for line in (self.body or '').splitlines() if line.strip()])


# ===========================================================================
# 5. DEVELOPER OUTPUT -- and the boundary it enforces
# ===========================================================================

class CodeArtifact(models.Model):
    """Something the Developer employee wrote, saved as text for a person.

    THIS MODEL IS THE SAFETY BOUNDARY
    ---------------------------------
    The Developer employee has no tool that writes to a working tree, runs a
    command, stages a change, commits, merges, pushes or deploys. There is no
    such tool to call, so there is no code path by which generated code can
    reach the project. Every generation tool -- code, models, endpoints,
    queries, tests, documentation, fixes, explanations, plans, commit messages
    -- ends by writing one of these rows and returning its text and its id.

    What that means in practice: an artefact is a proposal about code, not a
    change to code. A human developer reads it, judges it, and applies it by
    hand if they agree. ``status`` records which of those happened -- draft,
    reviewed, applied or discarded -- and ``suggested_path`` records where the
    employee thought it should go, as advice rather than a destination.

    The reason for building it this way rather than trusting a prompt is that
    an instruction not to modify code is a request, while an absent tool is a
    guarantee. Only the second one survives a model that misunderstands its
    brief.
    """

    KIND_CHOICES = [
        ('code', 'Code'),
        ('model', 'Django Model'),
        ('api', 'API Endpoint'),
        ('query', 'Database Query'),
        ('test', 'Tests'),
        ('doc', 'Documentation'),
        ('readme', 'README Section'),
        ('review', 'Code Review'),
        ('fix', 'Suggested Fix'),
        ('explanation', 'Code Explanation'),
        ('plan', 'Implementation Plan'),
        ('commit', 'Commit Message'),
        ('pr', 'Pull Request Description'),
        ('debug', 'Debug Analysis'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft -- not reviewed'),
        ('reviewed', 'Reviewed by a Developer'),
        ('applied', 'Applied by a Developer'),
        ('discarded', 'Discarded'),
    ]

    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default='code')
    title = models.CharField(max_length=250)
    language = models.CharField(max_length=30, default='python')
    content = models.TextField()
    explanation = models.TextField(
        blank=True,
        help_text='What was assumed, and the most likely thing to be wrong.')
    suggested_path = models.CharField(
        max_length=400, blank=True,
        help_text='Where this might belong. Advice only -- nothing is written there.')
    repository = models.CharField(max_length=200, blank=True)

    work_item = models.ForeignKey(WorkItem, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='artifacts')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='code_artifacts')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    metadata = models.JSONField(default=dict, blank=True)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='code_artifacts')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Code Artefact'
        verbose_name_plural = 'Code Artefacts'
        indexes = [
            models.Index(fields=['kind', '-created_at'], name='artifact_kind_created_idx'),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.title}"

    @property
    def line_count(self):
        return len((self.content or '').splitlines())

    @property
    def is_open(self):
        return self.status == 'draft'


class CodeReview(models.Model):
    """A review of code or of a pull request, with its findings kept.

    ``findings`` is a list of dictionaries shaped
    ``{"severity", "file", "line", "note", "suggestion"}``. It is stored
    structured rather than as prose so that ``blocking_count`` is a fact about
    the review rather than a re-reading of it, and so a later question -- what
    did we flag on this pull request -- has an answer that does not require
    parsing English.

    A review here is an opinion, not a gate. Nothing in this platform can
    approve, merge or block a pull request; ``verdict`` is what the employee
    would say, for a human reviewer to weigh.
    """

    VERDICT_CHOICES = [
        ('approve', 'Nothing blocking'),
        ('comment', 'Comments, none blocking'),
        ('request_changes', 'Changes needed'),
    ]

    BLOCKING_SEVERITIES = ('critical', 'high', 'blocker')

    repository = models.CharField(max_length=200, blank=True)
    pull_request_number = models.PositiveIntegerField(null=True, blank=True)
    title = models.CharField(max_length=250)
    summary = models.TextField(blank=True)
    findings = models.JSONField(
        default=list, blank=True,
        help_text='List of {severity, file, line, note, suggestion}.')
    verdict = models.CharField(max_length=16, choices=VERDICT_CHOICES, default='comment')

    work_item = models.ForeignKey(WorkItem, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='code_reviews')

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='code_reviews')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Code Review'
        verbose_name_plural = 'Code Reviews'

    def __str__(self):
        where = f'{self.repository}#{self.pull_request_number}' if self.pull_request_number \
            else self.title
        return f"review of {where}"

    @property
    def blocking_count(self):
        """Findings serious enough to hold a merge."""
        return len([f for f in (self.findings or [])
                    if str(f.get('severity', '')).lower() in self.BLOCKING_SEVERITIES])

    @property
    def finding_count(self):
        return len(self.findings or [])
