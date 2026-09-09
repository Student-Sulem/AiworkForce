"""The People Operations domain: hiring, and the employees hiring produces.

WHY THIS IS A SEPARATE MODULE
-----------------------------
models_platform holds the machinery that makes six chat personas into six AI
employees -- tasks, approvals, audit, memory. None of it knows what a candidate
is. This module holds the other half for one employee: the records the People
Operations employee reads, writes and reasons over. Keeping them apart means a
change to the approval queue cannot break shortlisting, and a change to the
scoring model cannot break the audit trail.

The app label is inferred from the package, so these tables live in the
``marketing`` app alongside everything else and the migration for them is
written centrally.

THE SHAPE OF THE HIRING RECORD, AND WHY IT IS THIS SHAPE
--------------------------------------------------------
``JobOpening.requirements`` is a list of dictionaries rather than a block of
prose, and that single decision is what makes the rest of the employee's job
possible. A requirement stored as ``{"text", "weight", "must_have"}`` can be
scored one at a time, weighted, and reported on with evidence. A requirement
stored as a paragraph can only be eyeballed. Vague, unscoreable requirements
are the usual reason a shortlist cannot be defended later, so the schema
refuses to accept them in that form: ``requirement_lines`` will read a plain
string back for tolerance, but everything written by a tool is normalised to
the scoreable shape.

``CandidateEvaluation`` is an append-only history rather than a column on
Candidate. A score is a judgement made at a moment against a particular
requirement set; if the opening's requirements are later sharpened, the earlier
judgement must still be readable as it stood. ``Candidate.score`` is a cached
copy of the most recent evaluation, kept only so a list of twenty candidates
does not need twenty joins.

``Interview.calendar_event`` points at the platform's own CalendarEvent record
rather than storing a Google event id directly. The interview is a hiring fact
and exists whether or not anybody has booked a room; the calendar entry is a
consequence of somebody approving a booking. Conflating the two would mean an
interview could not be planned before it was scheduled, which is backwards.

``Employee.from_candidate`` keeps the join between the two halves of the story.
Without it, the moment an offer is accepted the record of why the person was
hired becomes unreachable from the person.

WHAT IS DELIBERATELY ABSENT
---------------------------
There is no date of birth, gender, nationality, marital status, photograph or
health field anywhere in this module, and no free-text field is intended to
carry one. Every scoring tool works from ``JobOpening.requirements`` and the
resume text alone. A column that cannot be recorded cannot be weighed by
accident, which is a stronger guarantee than a policy note in a prompt.
"""

from django.contrib.auth.models import User
from django.db import models

from .models import AIAgent


# ===========================================================================
# 1. RECRUITMENT -- the opening and the people who applied to it
# ===========================================================================

class JobOpening(models.Model):
    """One role the company is hiring for, described well enough to score against.

    The description fields exist to be published; the ``requirements`` field
    exists to be computed with. Both matter, and they are not the same text.
    A job advertisement written for a reader ("you will thrive in a fast-moving
    team") tells a screening tool nothing, so the scoreable requirement set is
    stored separately and every screening decision refers to it by index.
    """

    EMPLOYMENT_TYPE_CHOICES = [
        ('full_time', 'Full Time'),
        ('part_time', 'Part Time'),
        ('contract', 'Contract'),
        ('internship', 'Internship'),
    ]
    SENIORITY_CHOICES = [
        ('intern', 'Intern'),
        ('junior', 'Junior'),
        ('mid', 'Mid-level'),
        ('senior', 'Senior'),
        ('lead', 'Lead'),
        ('principal', 'Principal'),
        ('manager', 'Manager'),
        ('director', 'Director'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('open', 'Open'),
        ('on_hold', 'On Hold'),
        ('filled', 'Filled'),
        ('closed', 'Closed'),
    ]

    title = models.CharField(max_length=160)
    department = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=140, blank=True)
    employment_type = models.CharField(max_length=15, choices=EMPLOYMENT_TYPE_CHOICES,
                                       default='full_time')
    seniority = models.CharField(max_length=12, choices=SENIORITY_CHOICES, default='mid')
    is_remote = models.BooleanField(default=False)

    summary = models.TextField(
        blank=True, help_text='Two or three sentences anybody in the company would understand.')
    description = models.TextField(blank=True, help_text='The full advertisement text.')
    responsibilities = models.JSONField(
        default=list, blank=True, help_text='List of plain strings.')
    requirements = models.JSONField(
        default=list, blank=True,
        help_text=('Scoreable requirement set. Each entry is '
                   '{"text": str, "weight": int, "must_have": bool}.'))
    nice_to_have = models.JSONField(default=list, blank=True)
    benefits = models.JSONField(default=list, blank=True)

    salary_min = models.PositiveIntegerField(null=True, blank=True)
    salary_max = models.PositiveIntegerField(null=True, blank=True)
    salary_currency = models.CharField(max_length=8, default='AUD')

    openings = models.PositiveSmallIntegerField(default=1)
    hiring_manager = models.CharField(max_length=140, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    target_start_date = models.DateField(null=True, blank=True)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='job_openings')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='job_openings')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Job Opening'
        verbose_name_plural = 'Job Openings'
        indexes = [
            models.Index(fields=['status', '-created_at'], name='hr_opening_status_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    # -- counts, for list pages that must not run a query per row ----------

    @property
    def candidate_count(self):
        return self.candidates.count()

    @property
    def shortlisted_count(self):
        return self.candidates.filter(
            status__in=('shortlisted', 'interviewing', 'offer', 'hired')).count()

    @property
    def salary_range(self):
        """A readable band, or an empty string when no figures were given.

        Returned as text rather than a tuple because every caller is either
        printing it or putting it in a tool result for a language model to
        read back, and 'AUD 120,000 - 150,000' needs no further handling.
        """
        currency = self.salary_currency or ''
        if self.salary_min and self.salary_max:
            return f"{currency} {self.salary_min:,} - {self.salary_max:,}".strip()
        if self.salary_min:
            return f"{currency} from {self.salary_min:,}".strip()
        if self.salary_max:
            return f"{currency} up to {self.salary_max:,}".strip()
        return ''

    @property
    def requirement_lines(self):
        """The requirement texts as plain strings.

        Tolerates a bare string in the list as well as the normalised
        dictionary shape. Requirements can arrive from a person pasting a job
        advertisement into the admin, and a screening run that crashed on a
        string would be a worse outcome than one that reads it as weight 3.
        """
        lines = []
        for entry in (self.requirements or []):
            if isinstance(entry, dict):
                text = str(entry.get('text') or '').strip()
            else:
                text = str(entry or '').strip()
            if text:
                lines.append(text)
        return lines

    @property
    def must_have_lines(self):
        """Only the requirements marked as non-negotiable."""
        lines = []
        for entry in (self.requirements or []):
            if isinstance(entry, dict) and entry.get('must_have'):
                text = str(entry.get('text') or '').strip()
                if text:
                    lines.append(text)
        return lines

    @property
    def is_accepting_candidates(self):
        return self.status in ('draft', 'open')


class Candidate(models.Model):
    """One person who applied for, or was sourced for, an opening.

    ``resume_text`` is the only substantive input to screening. It is stored as
    text rather than a file because every scoring tool in this project is
    deterministic Python over words: it needs the words, not a PDF. Where a
    resume arrived as a document, whatever extracted the text records where it
    came from in ``resume_source`` so a low score can be traced back to a bad
    extraction rather than a bad candidate.
    """

    SOURCE_CHOICES = [
        ('application', 'Direct Application'),
        ('referral', 'Employee Referral'),
        ('sourced', 'Sourced'),
        ('agency', 'Agency'),
        ('drive', 'Imported from Drive'),
    ]
    STATUS_CHOICES = [
        ('new', 'New'),
        ('screening', 'Screening'),
        ('shortlisted', 'Shortlisted'),
        ('interviewing', 'Interviewing'),
        ('offer', 'Offer Made'),
        ('hired', 'Hired'),
        ('rejected', 'Rejected'),
        ('withdrawn', 'Withdrawn'),
    ]

    full_name = models.CharField(max_length=160)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    location = models.CharField(max_length=140, blank=True)
    linkedin_url = models.URLField(blank=True)

    job_opening = models.ForeignKey(JobOpening, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='candidates')
    current_title = models.CharField(max_length=160, blank=True)
    current_company = models.CharField(max_length=160, blank=True)
    years_experience = models.DecimalField(max_digits=4, decimal_places=1,
                                           null=True, blank=True)
    skills = models.JSONField(default=list, blank=True)

    resume_text = models.TextField(blank=True)
    resume_source = models.CharField(
        max_length=200, blank=True,
        help_text='Where the text came from, so a bad extraction is traceable.')
    source = models.CharField(max_length=12, choices=SOURCE_CHOICES, default='application')

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='new')
    score = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Cached copy of the most recent evaluation, 0 to 100.')
    notes = models.TextField(blank=True)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='candidates')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-score', '-created_at']
        verbose_name = 'Candidate'
        verbose_name_plural = 'Candidates'
        indexes = [
            models.Index(fields=['status', '-score'], name='hr_cand_status_score_idx'),
            models.Index(fields=['job_opening', '-score'], name='hr_cand_opening_idx'),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.get_status_display()})"

    @property
    def initials(self):
        parts = [p for p in (self.full_name or '').split() if p]
        if not parts:
            return '??'
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def score_band(self):
        """A word for the score, so nothing has to re-invent the thresholds.

        Bands rather than raw numbers because a shortlist conversation is
        about 'strong' and 'possible', and having one definition of those keeps
        the tool text, the interface and the audit trail agreeing.
        """
        if self.score is None:
            return 'unscored'
        value = float(self.score)
        if value >= 80:
            return 'strong'
        if value >= 60:
            return 'possible'
        return 'weak'

    @property
    def latest_evaluation(self):
        """The most recent screening judgement, or None if never screened."""
        return self.evaluations.first()

    @property
    def is_open(self):
        return self.status not in ('hired', 'rejected', 'withdrawn')


class CandidateEvaluation(models.Model):
    """One screening judgement, kept as it stood.

    Append-only on purpose. A score is only defensible alongside the
    requirement set it was measured against and the evidence quoted for each
    line; overwriting it when the opening changes would leave a number nobody
    could explain. ``score_breakdown`` is therefore the substance of this row
    and the total is a derived convenience.
    """

    RECOMMENDATION_CHOICES = [
        ('advance', 'Advance'),
        ('hold', 'Hold'),
        ('reject', 'Do Not Advance'),
    ]

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE,
                                  related_name='evaluations')
    job_opening = models.ForeignKey(JobOpening, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='evaluations')
    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='candidate_evaluations')

    score = models.DecimalField(max_digits=5, decimal_places=2, default=0,
                                help_text='0 to 100, weighted across the requirements.')
    score_breakdown = models.JSONField(
        default=list, blank=True,
        help_text=('One entry per requirement: '
                   '{"requirement", "score", "max", "evidence"}.'))
    strengths = models.JSONField(default=list, blank=True)
    gaps = models.JSONField(default=list, blank=True)
    recommendation = models.CharField(max_length=8, choices=RECOMMENDATION_CHOICES,
                                      default='hold')
    rationale = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Candidate Evaluation'
        verbose_name_plural = 'Candidate Evaluations'
        indexes = [
            models.Index(fields=['candidate', '-created_at'], name='hr_eval_candidate_idx'),
        ]

    def __str__(self):
        return f"{self.candidate.full_name}: {self.score} ({self.recommendation})"

    @property
    def band(self):
        value = float(self.score or 0)
        if value >= 80:
            return 'strong'
        if value >= 60:
            return 'possible'
        return 'weak'

    @property
    def strongest(self):
        """The requirement this candidate covered best, as a breakdown entry."""
        rows = [r for r in (self.score_breakdown or []) if isinstance(r, dict)]
        return max(rows, key=lambda r: _ratio(r), default=None)

    @property
    def weakest(self):
        rows = [r for r in (self.score_breakdown or []) if isinstance(r, dict)]
        return min(rows, key=lambda r: _ratio(r), default=None)


def _ratio(row):
    """Proportion of a breakdown row's available points that were earned."""
    try:
        maximum = float(row.get('max') or 0)
        return (float(row.get('score') or 0) / maximum) if maximum else 0.0
    except (TypeError, ValueError):
        return 0.0


# ===========================================================================
# 2. INTERVIEWING -- planned first, booked second
# ===========================================================================

class Interview(models.Model):
    """One interview round for one candidate.

    A row exists as soon as the round is planned, with ``status='proposed'``
    and no ``scheduled_at``. That is the point of the model: the questions, the
    competencies and the evaluation form are hiring work the employee can do
    immediately, while putting the meeting on somebody's calendar reaches
    outside the company and has to wait for a person to approve it. If booking
    and planning shared one step, the interviewers would receive an invitation
    before anybody had read the questions.

    ``calendar_event`` is a string reference because CalendarEvent lives in
    models_platform; both are in the ``marketing`` app, and a lazy reference
    keeps this module importable without pulling the platform layer in.
    """

    MODE_CHOICES = [
        ('video', 'Video Call'),
        ('phone', 'Phone'),
        ('onsite', 'On Site'),
    ]
    STATUS_CHOICES = [
        ('proposed', 'Proposed'),
        ('scheduled', 'Scheduled'),
        ('rescheduled', 'Rescheduled'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('no_show', 'No Show'),
    ]

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE,
                                  related_name='interviews')
    job_opening = models.ForeignKey(JobOpening, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='interviews')

    round_name = models.CharField(max_length=120, default='First Interview')
    round_number = models.PositiveSmallIntegerField(default=1)
    scheduled_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Empty until a booking has been approved and executed.')
    duration_minutes = models.PositiveSmallIntegerField(default=45)
    mode = models.CharField(max_length=8, choices=MODE_CHOICES, default='video')
    interviewers = models.JSONField(default=list, blank=True,
                                    help_text='Names or email addresses.')
    location_or_link = models.CharField(max_length=400, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='proposed')

    questions = models.JSONField(
        default=list, blank=True,
        help_text='{"question", "competency", "what_to_look_for"} per entry.')
    evaluation_form = models.JSONField(
        default=list, blank=True,
        help_text='{"criterion", "scale", "guidance"} per entry.')
    feedback = models.TextField(blank=True)
    outcome_score = models.DecimalField(max_digits=5, decimal_places=2,
                                        null=True, blank=True)

    calendar_event = models.ForeignKey('CalendarEvent', on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='interviews')
    reschedule_reason = models.TextField(blank=True)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='interviews')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Interview'
        verbose_name_plural = 'Interviews'
        indexes = [
            models.Index(fields=['status', '-created_at'], name='hr_interview_status_idx'),
        ]

    def __str__(self):
        return f"{self.round_name} with {self.candidate.full_name}"

    @property
    def is_booked(self):
        return bool(self.scheduled_at) and self.status in ('scheduled', 'rescheduled')

    @property
    def when_readable(self):
        if not self.scheduled_at:
            return 'not yet scheduled'
        return self.scheduled_at.strftime('%A %d %B %Y at %H:%M')

    @property
    def interviewer_list(self):
        return [str(name) for name in (self.interviewers or []) if str(name).strip()]

    @property
    def question_count(self):
        return len(self.questions or [])


# ===========================================================================
# 3. EMPLOYEES -- what a completed hire becomes
# ===========================================================================

class Employee(models.Model):
    """A person on the staff, whether or not they have a login.

    ``account`` is nullable and separate from the record on purpose. Somebody
    is an employee from the day their contract is signed, which is usually
    before IT has created anything; an employee record that could not exist
    without a user account would make it impossible to run onboarding, which
    is exactly the period the record is most needed for.
    """

    STATUS_CHOICES = [
        ('onboarding', 'Onboarding'),
        ('active', 'Active'),
        ('leave', 'On Leave'),
        ('notice', 'Serving Notice'),
        ('exited', 'Exited'),
    ]

    full_name = models.CharField(max_length=160)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    job_title = models.CharField(max_length=160, blank=True)
    department = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=140, blank=True)

    manager = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='reports')
    account = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='employee_record')

    employment_status = models.CharField(max_length=12, choices=STATUS_CHOICES,
                                         default='onboarding')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    from_candidate = models.ForeignKey(Candidate, on_delete=models.SET_NULL, null=True,
                                       blank=True, related_name='employee_records')

    leave_balance_days = models.DecimalField(max_digits=5, decimal_places=1, default=20)
    skills = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['full_name']
        verbose_name = 'Employee'
        verbose_name_plural = 'Employees'
        indexes = [
            models.Index(fields=['employment_status', 'full_name'],
                         name='hr_employee_status_idx'),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.job_title or 'no title recorded'})"

    @property
    def initials(self):
        parts = [p for p in (self.full_name or '').split() if p]
        if not parts:
            return '??'
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def open_onboarding_tasks(self):
        """The onboarding tasks still outstanding, newest ordering preserved."""
        return self.onboarding_tasks.exclude(status__in=('done', 'skipped'))

    @property
    def open_onboarding_count(self):
        return self.open_onboarding_tasks.count()

    @property
    def onboarding_progress(self):
        """Percentage of onboarding tasks closed out, 0 when there are none."""
        total = self.onboarding_tasks.count()
        if not total:
            return 0
        done = self.onboarding_tasks.filter(status__in=('done', 'skipped')).count()
        return round(done * 100 / total)


class OnboardingTask(models.Model):
    """One thing that has to happen for a new starter.

    ``due_offset_days`` is stored beside ``due_date`` rather than instead of
    it. The offset is the reusable part of the checklist -- a laptop is ordered
    two days before the start, a compliance module is done in the second week
    -- and it stays correct when a start date moves. The absolute date is the
    resolved instance of that rule, recomputed rather than trusted, so a
    delayed start does not silently leave a task overdue on the old date.
    """

    CATEGORY_CHOICES = [
        ('paperwork', 'Paperwork'),
        ('access', 'Systems Access'),
        ('equipment', 'Equipment'),
        ('training', 'Training'),
        ('introduction', 'Introductions'),
        ('compliance', 'Compliance'),
    ]
    STATUS_CHOICES = [
        ('todo', 'To Do'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('blocked', 'Blocked'),
        ('skipped', 'Skipped'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE,
                                 related_name='onboarding_tasks')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=14, choices=CATEGORY_CHOICES,
                                default='paperwork')
    owner_role = models.CharField(
        max_length=120, blank=True,
        help_text="Who does it, by role rather than name, e.g. 'IT Support'.")
    due_offset_days = models.SmallIntegerField(
        default=0,
        help_text='Days relative to the start date. Negative is before day one.')
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='todo')
    display_order = models.PositiveSmallIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='onboarding_tasks')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'due_offset_days', 'id']
        verbose_name = 'Onboarding Task'
        verbose_name_plural = 'Onboarding Tasks'
        indexes = [
            models.Index(fields=['employee', 'display_order'], name='hr_onboard_emp_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    @property
    def is_open(self):
        return self.status in ('todo', 'in_progress', 'blocked')

    @property
    def timing_label(self):
        """The offset said in words, because '-2' means nothing on a page."""
        offset = self.due_offset_days or 0
        if offset < -1:
            return f'{abs(offset)} days before start'
        if offset == -1:
            return 'the day before start'
        if offset == 0:
            return 'day one'
        if offset == 1:
            return 'day two'
        if offset <= 5:
            return f'first week (day {offset + 1})'
        if offset <= 10:
            return f'second week (day {offset + 1})'
        return f'day {offset + 1}'


class PerformanceReview(models.Model):
    """A review cycle for one employee, as a template and then as answers.

    ``sections`` is the form and ``responses`` is what was written into it.
    They are separate because the form is generated and the answers are human;
    regenerating a template must never be able to erase what somebody already
    wrote.
    """

    STATUS_CHOICES = [
        ('template', 'Template Only'),
        ('in_progress', 'In Progress'),
        ('submitted', 'Submitted'),
        ('closed', 'Closed'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE,
                                related_name='reviews')
    period = models.CharField(max_length=80, help_text="e.g. 'H1 2026' or 'Probation'.")
    reviewer = models.CharField(max_length=140, blank=True)
    sections = models.JSONField(
        default=list, blank=True,
        help_text='{"title", "prompt", "scale"} per entry -- the blank form.')
    responses = models.JSONField(
        default=dict, blank=True,
        help_text='Section key to answer -- what was actually written.')
    summary = models.TextField(blank=True)
    overall_rating = models.DecimalField(max_digits=4, decimal_places=2,
                                         null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='template')

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='performance_reviews')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Performance Review'
        verbose_name_plural = 'Performance Reviews'

    def __str__(self):
        return f"{self.employee.full_name}: {self.period}"

    @property
    def section_count(self):
        return len(self.sections or [])

    @property
    def is_complete(self):
        keys = {str(s.get('key') or s.get('title'))
                for s in (self.sections or []) if isinstance(s, dict)}
        answered = {k for k, v in (self.responses or {}).items() if str(v).strip()}
        return bool(keys) and keys.issubset(answered)


class HRAnnouncement(models.Model):
    """Something People Operations told the company.

    Kept as a record even though the message itself went out through Slack or
    email, because 'what were staff actually told, and when' is a question that
    gets asked months later and the answer should not live only in a channel
    somebody may have left.
    """

    CHANNEL_CHOICES = [
        ('slack', 'Slack'),
        ('email', 'Email'),
        ('both', 'Slack and Email'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending', 'Awaiting Approval'),
        ('published', 'Published'),
        ('rejected', 'Rejected'),
    ]

    title = models.CharField(max_length=200)
    body = models.TextField()
    channel = models.CharField(max_length=6, choices=CHANNEL_CHOICES, default='slack')
    audience = models.CharField(max_length=140, default='All staff')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    published_at = models.DateTimeField(null=True, blank=True)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='hr_announcements')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'HR Announcement'
        verbose_name_plural = 'HR Announcements'

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    @property
    def is_live(self):
        return self.status == 'published'
