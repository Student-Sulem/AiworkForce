"""The Customer Support domain: customers, tickets, threads and reports.

WHY THIS IS A SEPARATE MODULE
-----------------------------
models_platform holds the machinery shared by all six employees: tasks, the
approval queue, execution records, audit. None of it knows what a ticket is.
This module holds the other half for one employee -- the records the Customer
Support employee reads, writes and reasons over. Keeping them apart means a
change to the approval queue cannot break triage, and a change to triage cannot
break the audit trail.

The app label is inferred from the package, so these tables live in the
``marketing`` app alongside everything else and the migration for them is
written centrally.

THE FIELD THAT MATTERS MOST: ``SupportTicket.needs_human``
----------------------------------------------------------
A support system whose only safeguard is "the AI writes well" is not a
safeguard at all. Fluency is not judgement, and the messages where the two come
apart are exactly the ones that matter: a data breach, a threat of legal
action, an injury, a customer in a mental-health crisis, a journalist asking
for comment. For those, the correct output is not a better-worded reply. It is
no reply, and a person.

``needs_human`` records that decision as data rather than trusting a paragraph
in a prompt to hold. The difference is enforceability. A prompt instruction can
be talked around by the next message in the conversation; a boolean column is
read by ``support.draft_customer_reply``, which refuses to write, and by
``support.send_customer_reply``, which refuses to even propose. The flag also
survives the conversation that set it, so a ticket marked for a person at
09:00 is still marked at 17:00 when a different task picks it up. Nothing about
the model's wording can clear it -- only a person changing the row can.

``urgency_signals`` exists for the same reason in a smaller way. Priority here
is derived from impact rather than tone, and the phrases that drove the
decision are stored alongside it, so "why is this urgent" has an answer made of
the customer's own words. A priority you cannot explain is a priority you
cannot defend to the customer whose ticket sits below it.

WHY THE THREAD IS ITS OWN TABLE
-------------------------------
``TicketMessage`` keeps inbound, outbound and internal notes in one ordered
thread, with ``is_ai_draft`` marking text an employee wrote but nobody has
approved. A draft is therefore visible, reviewable and quotable *before* it is
anything else, and the ``sent_message`` link to ``OutboundMessage`` records the
moment a draft stopped being a draft. Storing the draft on the ticket instead
would lose the distinction between what was proposed and what was sent, which
is the one distinction the whole platform is built around.

WHY ``contact_email`` SITS BESIDE ``customer``
----------------------------------------------
Most support contact arrives from somebody who is not yet a row in the
database. Requiring a Customer before a ticket can exist would mean either
losing the request or inventing a customer record from an email signature. The
ticket therefore carries the address it came from, and the Customer link is
filled in when there is a real one to link to.
"""

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .models import AIAgent

# Statuses that mean somebody still owes the customer something. Declared once
# here because three properties, several list filters and every report count
# depend on agreeing about it.
OPEN_STATUSES = ('new', 'open', 'pending', 'waiting_customer', 'escalated', 'reopened')

# Statuses that mean the work is finished.
CLOSED_STATUSES = ('resolved', 'closed')


# ===========================================================================
# 1. THE CUSTOMER
# ===========================================================================

class Customer(models.Model):
    """One person or organisation the company supports.

    Deliberately thin. This is not a CRM: it holds what support needs in order
    to answer well -- who they are, how to reach them, what they are entitled
    to, and what timezone they will read the reply in. Anything richer belongs
    in the company's real customer system, and duplicating it here would only
    create a second version of the truth for an employee to quote from.

    ``tier`` is the one field that changes behaviour rather than presentation:
    it is a contractual fact, so it can raise a priority without anybody
    accusing the triage of responding to tone.
    """

    TIER_CHOICES = [
        ('free', 'Free'),
        ('standard', 'Standard'),
        ('premium', 'Premium'),
        ('enterprise', 'Enterprise'),
    ]

    name = models.CharField(max_length=160)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    company = models.CharField(max_length=160, blank=True)
    tier = models.CharField(max_length=12, choices=TIER_CHOICES, default='standard')
    account_reference = models.CharField(
        max_length=60, blank=True,
        help_text='The identifier this customer is known by in the billing system.')
    timezone_name = models.CharField(
        max_length=60, blank=True, default='Australia/Sydney',
        help_text='Used when a reply promises a time, so the promise means what it says.')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Customer'
        verbose_name_plural = 'Customers'
        indexes = [
            models.Index(fields=['email'], name='support_customer_email_idx'),
        ]

    def __str__(self):
        if self.company and self.company != self.name:
            return f"{self.name} ({self.company})"
        return self.name

    @property
    def initials(self):
        """Two letters for an avatar, without needing an image."""
        parts = [part for part in (self.name or '').replace('-', ' ').split() if part]
        if not parts:
            return (self.email or '?')[:1].upper()
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][:1] + parts[-1][:1]).upper()

    @property
    def open_ticket_count(self):
        return self.tickets.filter(status__in=OPEN_STATUSES).count()

    @property
    def is_priority(self):
        """Whether this customer's contract earns a faster response."""
        return self.tier in ('premium', 'enterprise')


# ===========================================================================
# 2. THE TICKET
# ===========================================================================

class SupportTicket(models.Model):
    """One customer request, from arrival to resolution.

    ``reference`` is assigned after the first save, because a reference a
    customer will quote back at you should be the primary key rather than a
    counter that can drift. 'TKT-14' is therefore always ticket 14, in the
    database, in the reply, and in the audit trail.

    THE TRIAGE COLUMNS
    ------------------
    ``category``, ``priority`` and ``sentiment`` are all set by deterministic
    keyword rules rather than by a language model, and ``urgency_signals``
    stores the phrases that fired. That combination is the only way the
    priority can be explained later: "urgent because the message says 'charged
    me twice' and 'our whole team cannot invoice'" is answerable, and "the
    model felt it was urgent" is not.

    ``needs_human`` is the hard stop described in the module docstring. When it
    is true no tool will draft or propose a customer reply, whatever it is
    asked to do.
    """

    CHANNEL_CHOICES = [
        ('email', 'Email'),
        ('chat', 'Live Chat'),
        ('phone', 'Phone'),
        ('web', 'Web Form'),
        ('social', 'Social Media'),
    ]
    CATEGORY_CHOICES = [
        ('billing', 'Billing'),
        ('refund', 'Refund'),
        ('order', 'Order'),
        ('technical', 'Technical'),
        ('account', 'Account'),
        ('delivery', 'Delivery'),
        ('feature_request', 'Feature Request'),
        ('complaint', 'Complaint'),
        ('other', 'Other'),
    ]
    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('normal', 'Normal'),
        ('high', 'High'),
        ('urgent', 'Urgent'),
    ]
    STATUS_CHOICES = [
        ('new', 'New'),
        ('open', 'Open'),
        ('pending', 'Pending Internally'),
        ('waiting_customer', 'Waiting on the Customer'),
        ('escalated', 'Escalated'),
        ('resolved', 'Resolved'),
        ('closed', 'Closed'),
        ('reopened', 'Reopened'),
    ]
    SENTIMENT_CHOICES = [
        ('positive', 'Positive'),
        ('neutral', 'Neutral'),
        ('negative', 'Negative'),
        ('angry', 'Angry'),
    ]

    reference = models.CharField(
        max_length=20, blank=True, db_index=True,
        help_text="Assigned on first save as 'TKT-<id>'. What the customer quotes back.")

    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='tickets')
    contact_email = models.EmailField(
        blank=True,
        help_text='The address the request came from, used when no Customer row exists yet.')

    subject = models.CharField(max_length=300)
    body = models.TextField(blank=True)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default='email')
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other')
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='normal')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    sentiment = models.CharField(max_length=10, choices=SENTIMENT_CHOICES, default='neutral')

    urgency_signals = models.JSONField(
        default=list, blank=True,
        help_text=('The phrases from the customer that drove the priority, so the '
                   'decision can be explained in the customer\'s own words.'))

    assignee = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='support_tickets')
    assigned_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='support_tickets')

    duplicate_of = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='duplicates')

    escalated_to = models.CharField(
        max_length=140, blank=True,
        help_text='Where it went: a team, a channel, a named person.')
    escalation_reason = models.TextField(blank=True)

    needs_human = models.BooleanField(
        default=False,
        help_text=('True when this ticket must not receive an AI-drafted reply at all: '
                   'a data breach, a legal threat, a safety issue, a vulnerable customer, '
                   'a media enquiry, anything involving self-harm. Read by the drafting '
                   'and sending tools, which refuse to act while it is set.'))

    external_reference = models.CharField(
        max_length=60, blank=True, help_text="The Jira key once escalated, e.g. 'SUP-118'.")
    external_url = models.CharField(max_length=400, blank=True)
    source_message_id = models.CharField(
        max_length=200, blank=True,
        help_text='The Gmail message id this ticket was imported from, if any.')

    resolution = models.TextField(blank=True)

    sla_due_at = models.DateTimeField(null=True, blank=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    reopened_count = models.PositiveIntegerField(default=0)

    tags = models.ManyToManyField('TicketTag', blank=True, related_name='tickets')

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='created_tickets')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Support Ticket'
        verbose_name_plural = 'Support Tickets'
        indexes = [
            models.Index(fields=['status'], name='support_ticket_status_idx'),
            models.Index(fields=['priority'], name='support_ticket_priority_idx'),
        ]

    def __str__(self):
        return f"{self.reference or 'TKT-?'}: {self.subject}"

    def save(self, *args, **kwargs):
        """Save, then stamp the reference from the primary key on first write.

        Two saves rather than one because the reference is derived from the id,
        and the id does not exist until the row does. The second write touches
        one column, so the cost is a single UPDATE on creation only.
        """
        super().save(*args, **kwargs)
        if not self.reference and self.pk:
            self.reference = f'TKT-{self.pk}'
            super().save(update_fields=['reference'])

    # -- state -------------------------------------------------------------

    @property
    def is_open(self):
        return self.status in OPEN_STATUSES

    @property
    def is_breaching_sla(self):
        """Whether the response target has already passed with nothing resolved."""
        if not self.sla_due_at or self.status in CLOSED_STATUSES:
            return False
        return self.sla_due_at < timezone.now()

    @property
    def hours_open(self):
        """Hours from arrival to resolution, or to now while still open."""
        if not self.created_at:
            return 0.0
        end = self.resolved_at if self.status in CLOSED_STATUSES and self.resolved_at \
            else timezone.now()
        return round((end - self.created_at).total_seconds() / 3600, 1)

    @property
    def age_label(self):
        """The age in words, because '73.4' in a reply to a customer is no use."""
        hours = self.hours_open
        if hours < 1:
            return 'under an hour'
        if hours < 24:
            whole = int(round(hours))
            return f"{whole} hour{'s' if whole != 1 else ''}"
        days = int(hours // 24)
        return f"{days} day{'s' if days != 1 else ''}"

    @property
    def message_count(self):
        return self.messages.count()

    @property
    def contact_address(self):
        """The best address to reply to: the Customer row, else the ticket's own."""
        if self.customer_id and self.customer and self.customer.email:
            return self.customer.email
        return self.contact_email

    @property
    def customer_label(self):
        if self.customer_id and self.customer:
            return str(self.customer)
        return self.contact_email or 'an unidentified customer'


# ===========================================================================
# 3. THE THREAD
# ===========================================================================

class TicketMessage(models.Model):
    """One entry in a ticket's conversation: theirs, ours, or a note to ourselves.

    ``is_ai_draft`` is what makes the approval workflow legible on the ticket
    itself. An employee's proposed reply is written here first, marked as a
    draft, and only acquires ``sent_message`` when a person approved it and the
    platform sent it. A reader of the thread can therefore always tell what the
    customer has actually received from what was merely written.
    """

    DIRECTION_CHOICES = [
        ('inbound', 'From the Customer'),
        ('outbound', 'To the Customer'),
        ('internal', 'Internal Note'),
    ]

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE,
                               related_name='messages')
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default='inbound')
    author_label = models.CharField(
        max_length=140, blank=True,
        help_text='Who wrote it, as it should be shown: a customer, an employee, an agent.')
    body = models.TextField(blank=True)
    is_ai_draft = models.BooleanField(
        default=False,
        help_text='Written by an AI employee and not yet approved or sent.')
    sent_message = models.ForeignKey(
        'OutboundMessage', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ticket_messages',
        help_text='The execution record, present once this draft was actually sent.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Ticket Message'
        verbose_name_plural = 'Ticket Messages'
        indexes = [
            models.Index(fields=['ticket', 'created_at'], name='support_msg_thread_idx'),
        ]

    def __str__(self):
        marker = ' (draft)' if self.is_ai_draft and not self.sent_message_id else ''
        return f"{self.get_direction_display()}{marker} on {self.ticket_id}"

    @property
    def was_sent(self):
        return self.sent_message_id is not None

    @property
    def preview(self):
        text = ' '.join((self.body or '').split())
        return text if len(text) <= 160 else text[:157] + '...'


# ===========================================================================
# 4. REPORTING
# ===========================================================================

class SupportReport(models.Model):
    """One computed account of what support looked like over a period.

    ``metrics`` holds the figures and ``body`` holds the reading of them. Both
    are kept because a report is quoted twice: once by a person skimming the
    prose, and once by a tool that wants last month's median response time
    without recomputing it. A report whose numbers are only inside its prose
    cannot be compared with the next one.
    """

    KIND_CHOICES = [
        ('volume', 'Volume'),
        ('resolution', 'Resolution'),
        ('recurring', 'Recurring Problems'),
        ('sentiment', 'Sentiment'),
        ('sla', 'SLA Performance'),
    ]

    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default='volume')
    body = models.TextField(blank=True)
    metrics = models.JSONField(
        default=dict, blank=True,
        help_text='The computed figures, kept separately so reports can be compared.')
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='support_reports')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Support Report'
        verbose_name_plural = 'Support Reports'

    def __str__(self):
        return f"{self.title} ({self.get_kind_display()})"

    @property
    def period_label(self):
        if self.period_start and self.period_end:
            return f"{self.period_start:%d %b %Y} to {self.period_end:%d %b %Y}"
        return 'unspecified period'


# ===========================================================================
# 5. TAGS
# ===========================================================================

class TicketTag(models.Model):
    """One label used to group tickets by something the status set cannot say.

    A tag is a row rather than a string on the ticket so that the same label
    means the same thing across every ticket carrying it. 'export-timeout'
    spelled three ways is three product signals nobody counts; spelled once, it
    is a number in a recurring-problems report.
    """

    name = models.SlugField(
        max_length=60, unique=True,
        help_text="Lowercase and hyphenated, e.g. 'export-timeout'.")
    description = models.CharField(max_length=250, blank=True)
    colour = models.CharField(max_length=20, default='#64748b')

    class Meta:
        ordering = ['name']
        verbose_name = 'Ticket Tag'
        verbose_name_plural = 'Ticket Tags'

    def __str__(self):
        return self.name

    @property
    def ticket_count(self):
        return self.tickets.count()
