"""The Marketing & Communications domain: briefs, copy, calendars and email.

WHY THIS IS A SEPARATE MODULE
-----------------------------
models_platform holds the machinery shared by all six employees -- tasks,
approvals, execution records, audit. models.py holds the original marketing
records this project started from, including ``MarketingCampaign``. This module
holds the third thing: the records the Marketing & Communications employee
reads, writes and reasons over when it produces copy.

``MarketingCampaign`` is deliberately reused rather than replaced. A campaign
already exists in this application, is already on the dashboard, and already
carries an objective, an audience, a budget and dates. Defining a second
campaign model would have split every existing report in two. So everything
here links to that model instead: a ``CampaignBrief`` explains a campaign,
``ContentPiece`` rows fill it, ``ContentCalendarEntry`` rows schedule it, and
``MarketingEmail`` rows send it.

WHY ``sources`` AND ``proof_points`` ARE COLUMNS AND NOT ADVICE
---------------------------------------------------------------
Marketing copy is the single most likely place in the whole platform for an
invented figure to reach the public. Every other employee's mistakes stay
inside the company: an improvised leave entitlement is quoted back to one
employee, an improvised refund window is quoted back to one customer, and both
are recoverable. A published post is different. It is read by people who never
asked the company anything, it is screenshotted, and the company owns the
number in it from the moment it goes out. "Uptime improved by 40 per cent" in a
LinkedIn post is a public claim whether or not anybody measured it.

A prompt can discourage that. It cannot make it visible. So the schema carries
the provenance instead:

``ContentPiece.sources``      the documents and facts the copy was built from.
                             Empty means the copy asserts nothing sourced --
                             which is fine for an invitation and alarming for a
                             performance claim, and either way it is a query
                             rather than a judgement about a prompt.

``CampaignBrief.proof_points`` each entry shaped ``{"claim", "source",
                             "document_id"}``, so a claim with an empty source
                             is structurally visible. ``unsourced_claims``
                             returns exactly those rows, which is what lets a
                             review page, a compliance tool and a person all
                             ask the same question and get the same answer.

The consequence is the one that matters: ``mkt.check_brand_compliance`` can
take a figure out of the finished copy, look for it in that piece's own
``sources``, and report it when it is not there. That check is only possible
because the provenance is stored beside the text rather than remembered in a
conversation that has since scrolled away.

CHANNEL LIMITS LIVE HERE
------------------------
``CHANNEL_LIMITS`` holds the platforms' own hard limits and ``HOUSE_HASHTAG_LIMITS``
holds the company's stylistic ones. They are separate because they fail
differently: exceeding LinkedIn's 3000 characters means the post is rejected by
LinkedIn, while exceeding five hashtags means the post is merely worse. The
tool layer imports both from here so that a model, a compliance check and a
generator can never disagree about what the limit is.
"""

from django.contrib.auth.models import User
from django.db import models

from .models import AIAgent, MarketingCampaign


# ===========================================================================
# Channel facts, shared by the models and the tools
# ===========================================================================

# The platforms' own limits. ``characters`` of 0 means the platform imposes no
# limit worth checking, not that the limit is zero. ``hashtags`` of 0 likewise:
# LinkedIn does not cap hashtags, Instagram caps them at 30.
CHANNEL_LIMITS = {
    'linkedin': {'label': 'LinkedIn', 'characters': 3000, 'hashtags': 0},
    'instagram': {'label': 'Instagram', 'characters': 2200, 'hashtags': 30},
    'twitter': {'label': 'Twitter/X', 'characters': 280, 'hashtags': 0},
    'facebook': {'label': 'Facebook', 'characters': 63206, 'hashtags': 0},
    'blog': {'label': 'Blog', 'characters': 0, 'hashtags': 0},
    'email': {'label': 'Email', 'characters': 0, 'hashtags': 0,
              'subject_characters': 60},
    'ad': {'label': 'Advertisement', 'characters': 600, 'hashtags': 0},
    'press': {'label': 'Press release', 'characters': 0, 'hashtags': 0},
    'website': {'label': 'Website', 'characters': 0, 'hashtags': 0},
}

# The house style, which is stricter than the platforms and for different
# reasons. Five hashtags on LinkedIn is a readability decision, not an API one.
HOUSE_HASHTAG_LIMITS = {
    'linkedin': 5,
    'instagram': 12,
    'twitter': 3,
    'facebook': 5,
    'ad': 3,
    'blog': 5,
    'email': 0,
    'press': 0,
    'website': 0,
}

# An email subject longer than this is truncated in most mail clients, so the
# limit is behavioural rather than enforced. Kept beside the others so one
# import answers every "what is the limit" question.
EMAIL_SUBJECT_LIMIT = 60


CHANNEL_CHOICES = [
    ('linkedin', 'LinkedIn'),
    ('instagram', 'Instagram'),
    ('twitter', 'Twitter / X'),
    ('facebook', 'Facebook'),
    ('blog', 'Blog'),
    ('email', 'Email'),
    ('ad', 'Advertisement'),
    ('press', 'Press Release'),
    ('website', 'Website'),
]


def channel_label(channel):
    """The display name of one channel, tolerant of an unknown key."""
    entry = CHANNEL_LIMITS.get(channel or '')
    if entry:
        return entry['label']
    return (channel or 'unspecified channel').replace('_', ' ').title()


# ===========================================================================
# 1. THE BRIEF -- what a campaign is trying to do, and on what evidence
# ===========================================================================

class CampaignBrief(models.Model):
    """The argument behind a campaign, written before any copy is produced.

    A brief exists because the alternative is a campaign that is only a pile of
    posts. Asked "who is this for and what would count as it having worked",
    a pile of posts cannot answer; a brief can, and the answer is checkable
    afterwards against ``success_measures``.

    ``proof_points`` is the load-bearing field. Each entry is shaped
    ``{"claim", "source", "document_id"}`` rather than being a paragraph of
    supporting prose, because a claim in a list can be checked one at a time
    and a claim in a paragraph can only be read sympathetically. An entry whose
    source and document_id are both empty is an assertion nobody has grounded,
    and ``unsourced_claims`` surfaces it before it is repeated in a post.
    """

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('active', 'Active'),
        ('complete', 'Complete'),
    ]

    campaign = models.ForeignKey(MarketingCampaign, on_delete=models.CASCADE,
                                 related_name='briefs')

    objective = models.TextField(
        blank=True,
        help_text='What this campaign is for, in terms somebody could later judge.')
    audience = models.CharField(max_length=250, blank=True)
    key_message = models.TextField(
        blank=True,
        help_text='The single thing a reader should carry away. One message, not three.')
    channels = models.JSONField(
        default=list, blank=True,
        help_text="Channel keys this campaign runs on, e.g. ['linkedin', 'email'].")
    tone = models.CharField(max_length=60, blank=True)

    proof_points = models.JSONField(
        default=list, blank=True,
        help_text='Claims with their provenance, each {"claim", "source", '
                  '"document_id"}. An entry with neither a source nor a document '
                  'id is an unsourced claim and is reported as one.')
    success_measures = models.JSONField(
        default=list, blank=True,
        help_text='What would count as this campaign having worked.')
    constraints = models.TextField(
        blank=True,
        help_text='What the campaign may not do: claims to avoid, legal limits, timing.')

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='campaign_briefs')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Campaign Brief'
        verbose_name_plural = 'Campaign Briefs'

    def __str__(self):
        return f"Brief for {self.campaign.name}"

    @property
    def unsourced_claims(self):
        """Proof points nobody has grounded in a document.

        Returned as the entries themselves rather than as a count, because the
        useful thing to show a reviewer is the sentence that has no source, not
        the fact that three of them do not.
        """
        rows = []
        for entry in self.proof_points or []:
            if not isinstance(entry, dict):
                text = str(entry).strip()
                if text:
                    rows.append({'claim': text, 'source': '', 'document_id': None})
                continue
            if not (str(entry.get('source') or '').strip() or entry.get('document_id')):
                rows.append(entry)
        return rows

    @property
    def channel_labels(self):
        return [channel_label(key) for key in (self.channels or [])]

    @property
    def is_grounded(self):
        """True when every proof point names where it came from."""
        return bool(self.proof_points) and not self.unsourced_claims


# ===========================================================================
# 2. THE COPY -- one piece of content, with its provenance
# ===========================================================================

class ContentPiece(models.Model):
    """One piece of marketing copy, on one channel, for one purpose.

    WHY ``sources`` IS HERE AND NOT IN A CONVERSATION
    -------------------------------------------------
    The copy in ``body`` is produced deterministically from a topic and a list
    of supplied facts, and this column records those facts with the documents
    they came from. That is what makes the compliance check possible: a figure
    in the body that does not appear anywhere in ``sources`` was not supplied,
    which means it was either typed by a person who knows something the record
    does not, or invented. Both deserve a person's attention before the piece
    is published, and neither is discoverable from the text alone.

    WHY ``variant_of`` AND NOT A SEPARATE VARIANT TABLE
    ---------------------------------------------------
    A variant of a post is a post. It has a channel, a body, a status and a
    publication history exactly like its parent, and it may itself be published
    while the parent is not. A self-reference keeps A/B alternatives in the same
    list, the same status workflow and the same approval queue as everything
    else, and ``variant_label`` records what was varied so choosing between them
    means something.

    ``published_at`` and ``external_reference`` are written by an executor after
    a person approves publication, never by the tool that wrote the copy. A
    piece with a body and no ``published_at`` has not been published, and that
    is a fact about the row rather than a promise about the code.
    """

    KIND_CHOICES = [
        ('post', 'Social Post'),
        ('caption', 'Caption'),
        ('article', 'Article'),
        ('ad', 'Advertisement'),
        ('announcement', 'Announcement'),
        ('product_description', 'Product Description'),
        ('subject_line', 'Email Subject Line'),
        ('newsletter', 'Newsletter'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('scheduled', 'Scheduled'),
        ('published', 'Published'),
        ('rejected', 'Rejected'),
    ]

    campaign = models.ForeignKey(MarketingCampaign, on_delete=models.SET_NULL, null=True,
                                 blank=True, related_name='content_pieces')
    brief = models.ForeignKey(CampaignBrief, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='content_pieces')

    channel = models.CharField(max_length=15, choices=CHANNEL_CHOICES, default='linkedin')
    kind = models.CharField(max_length=25, choices=KIND_CHOICES, default='post')

    title = models.CharField(
        max_length=250, blank=True,
        help_text='An internal name for a social post; the real headline for an article or ad.')
    body = models.TextField(blank=True)
    hashtags = models.JSONField(default=list, blank=True)
    call_to_action = models.CharField(max_length=250, blank=True)
    audience = models.CharField(max_length=250, blank=True)
    tone = models.CharField(max_length=60, blank=True)

    variant_of = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='variants')
    variant_label = models.CharField(
        max_length=120, blank=True,
        help_text='What was varied, so an A/B choice is between named alternatives.')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    scheduled_for = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Written by an executor after approval. Never set at drafting time.')
    external_reference = models.CharField(max_length=200, blank=True)
    external_url = models.CharField(max_length=500, blank=True)

    word_count = models.PositiveIntegerField(default=0)
    character_count = models.PositiveIntegerField(default=0)

    sources = models.JSONField(
        default=list, blank=True,
        help_text='The facts this copy was built from and the documents they came '
                  'from, each {"fact", "document", "document_id", "url"}. A figure '
                  'in the body that is not represented here was not supplied.')

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='content_pieces')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Content Piece'
        verbose_name_plural = 'Content Pieces'
        indexes = [
            models.Index(fields=['status'], name='content_piece_status_idx'),
        ]

    def __str__(self):
        return f"[{channel_label(self.channel)}] {self.title or self.preview}"

    # -- derived reading ---------------------------------------------------

    @property
    def is_published(self):
        return self.status == 'published' and self.published_at is not None

    @property
    def hashtag_line(self):
        """The hashtags as they would actually appear at the foot of a post."""
        tags = []
        for tag in self.hashtags or []:
            text = str(tag).strip().lstrip('#')
            if text:
                tags.append(f'#{text}')
        return ' '.join(tags)

    @property
    def full_text(self):
        """Body plus hashtag line: what would genuinely be published."""
        line = self.hashtag_line
        return f'{self.body}\n\n{line}'.strip() if line else (self.body or '')

    @property
    def fits_channel(self):
        """Whether this piece is within its platform's limits.

        Returns ``(bool, message)`` rather than a bare boolean because the
        interesting part is always the message: "412 characters over LinkedIn's
        3000" tells a writer what to cut, and False does not.
        """
        limits = CHANNEL_LIMITS.get(self.channel or '')
        if limits is None:
            return (True, f'No limits are recorded for {channel_label(self.channel)}, '
                          'so nothing was checked.')

        label = limits['label']
        problems = []

        # An email subject line is the one case where the limit applies to the
        # short field rather than the body.
        if self.kind == 'subject_line':
            allowance = limits.get('subject_characters') or EMAIL_SUBJECT_LIMIT
            subject = (self.title or self.body or '').strip()
            if len(subject) > allowance:
                problems.append(
                    f'the subject is {len(subject)} characters, '
                    f'{len(subject) - allowance} over the {allowance} that survive '
                    'in most mail clients')
            if problems:
                return (False, f'{label}: ' + '; '.join(problems) + '.')
            return (True, f'{label}: subject is {len(subject)} of {allowance} characters.')

        text = self.full_text
        allowance = limits.get('characters') or 0
        if allowance and len(text) > allowance:
            problems.append(f'the copy is {len(text)} characters, '
                            f'{len(text) - allowance} over {label}\'s {allowance}')

        tag_allowance = limits.get('hashtags') or 0
        tag_count = len([t for t in (self.hashtags or []) if str(t).strip()])
        if tag_allowance and tag_count > tag_allowance:
            problems.append(f'there are {tag_count} hashtags, '
                            f'{tag_count - tag_allowance} over {label}\'s {tag_allowance}')

        if problems:
            return (False, f'{label}: ' + '; '.join(problems) + '.')

        if allowance:
            return (True, f'{label}: {len(text)} of {allowance} characters, '
                          f'{tag_count} hashtags.')
        return (True, f'{label} sets no length limit worth checking. '
                      f'The copy is {len(text)} characters with {tag_count} hashtags.')

    @property
    def preview(self):
        """The opening of the copy on one line, for a list row."""
        flat = ' '.join((self.body or '').split())
        return flat[:160] if len(flat) <= 160 else flat[:157] + '...'

    @property
    def source_documents(self):
        """The distinct document names this copy drew on."""
        names = []
        for entry in self.sources or []:
            name = (entry.get('document') if isinstance(entry, dict) else str(entry)) or ''
            name = str(name).strip()
            if name and name not in names:
                names.append(name)
        return names

    def recount(self):
        """Refresh the cached word and character counts from the body.

        Cached rather than computed because a calendar or a campaign page lists
        many pieces at once and neither wants to load every body to show a
        length.
        """
        text = self.full_text
        self.word_count = len(text.split())
        self.character_count = len(text)
        return self


# ===========================================================================
# 3. THE PLAN -- what goes out, when, on what channel
# ===========================================================================

class ContentCalendarEntry(models.Model):
    """One planned slot on the marketing calendar.

    The entry is separate from the ``ContentPiece`` on purpose, and nullable in
    both directions of use. A calendar is normally built before any copy
    exists: "LinkedIn, Tuesday morning, week two, about rostering" is a real
    plan and a useful one, and forcing it to point at a draft would mean the
    plan could not be made until the work was already done. Equally a piece can
    exist without ever being calendared. So the two are joined when both exist
    and stand alone when they do not.
    """

    STATUS_CHOICES = [
        ('planned', 'Planned'),
        ('drafted', 'Drafted'),
        ('scheduled', 'Scheduled'),
        ('published', 'Published'),
        ('skipped', 'Skipped'),
    ]

    date = models.DateField()
    slot = models.CharField(
        max_length=30, default='morning',
        help_text="Time of day in words, e.g. 'morning'. Deliberately not a time: "
                  'a plan made four weeks out does not know the hour.')
    channel = models.CharField(max_length=15, choices=CHANNEL_CHOICES, default='linkedin')

    content = models.ForeignKey(ContentPiece, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='calendar_entries')
    title = models.CharField(max_length=250, blank=True)
    owner = models.CharField(max_length=140, blank=True)
    campaign = models.ForeignKey(MarketingCampaign, on_delete=models.SET_NULL, null=True,
                                 blank=True, related_name='calendar_entries')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='planned')
    notes = models.TextField(blank=True)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='calendar_plan_entries')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'slot']
        verbose_name = 'Content Calendar Entry'
        verbose_name_plural = 'Content Calendar'
        indexes = [
            models.Index(fields=['date'], name='calendar_entry_date_idx'),
        ]

    def __str__(self):
        return f"{self.date:%d %b} {self.slot} -- {channel_label(self.channel)}"

    @property
    def channel_display(self):
        return channel_label(self.channel)

    @property
    def has_draft(self):
        return self.content_id is not None


# ===========================================================================
# 4. MARKETING EMAIL -- the campaign mailing, distinct from outreach
# ===========================================================================

class MarketingEmail(models.Model):
    """One marketing mailing: a campaign send, a newsletter, an announcement.

    This is not ``EmailOutreach``. Outreach is one message to one prospect in a
    sequence, and its unit of interest is the individual relationship. A
    marketing email is one piece of copy sent to a segment, and its unit of
    interest is the segment and the rates. Merging them would mean either a
    newsletter pretending to be addressed to a single lead or a prospecting
    sequence carrying open-rate columns it never fills, so they stay separate.

    ``recipients`` is a list rather than a related table because the recipients
    of a mailing are a snapshot of a segment at send time. Recorded as a list,
    the record still says who received the January newsletter after the segment
    has been redefined twice; joined to a live segment, it would quietly start
    lying.
    """

    KIND_CHOICES = [
        ('campaign', 'Campaign Email'),
        ('newsletter', 'Newsletter'),
        ('announcement', 'Announcement'),
        ('nurture', 'Nurture Email'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('scheduled', 'Scheduled'),
        ('sent', 'Sent'),
        ('rejected', 'Rejected'),
    ]

    campaign = models.ForeignKey(MarketingCampaign, on_delete=models.SET_NULL, null=True,
                                 blank=True, related_name='emails')
    kind = models.CharField(max_length=15, choices=KIND_CHOICES, default='campaign')

    name = models.CharField(max_length=200, blank=True,
                            help_text='Internal name, so the mailing can be found later.')
    subject = models.CharField(max_length=250, blank=True)
    preheader = models.CharField(
        max_length=200, blank=True,
        help_text='The line the inbox shows after the subject. Left blank, the '
                  'client shows the opening of the body instead, which is usually worse.')
    body = models.TextField(blank=True)

    audience_segment = models.CharField(max_length=200, blank=True)
    recipients = models.JSONField(
        default=list, blank=True,
        help_text='The addresses this mailing goes to, as a snapshot taken at send time.')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    scheduled_for = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    recipient_count = models.PositiveIntegerField(default=0)
    open_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    click_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='marketing_emails')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Marketing Email'
        verbose_name_plural = 'Marketing Emails'
        indexes = [
            models.Index(fields=['status'], name='marketing_email_status_idx'),
        ]

    def __str__(self):
        return f"[{self.get_kind_display()}] {self.subject or self.name or 'Untitled'}"

    @property
    def subject_length_ok(self):
        """Whether the subject survives the inbox.

        Sixty characters is where most desktop and mobile clients truncate. A
        longer subject is not rejected by anything; it is simply read as a
        fragment, which is worse than a shorter subject that reads whole.
        """
        return len((self.subject or '').strip()) <= EMAIL_SUBJECT_LIMIT

    @property
    def subject_length(self):
        return len((self.subject or '').strip())

    @property
    def was_sent(self):
        return self.status == 'sent' and self.sent_at is not None

    @property
    def audience_summary(self):
        listed = len(self.recipients or [])
        if listed:
            return f'{listed} listed recipient{"s" if listed != 1 else ""}'
        if self.audience_segment:
            return f'segment: {self.audience_segment}'
        return 'no audience recorded yet'


# ===========================================================================
# 5. AUDIENCES -- who the work is for
# ===========================================================================

class AudienceSegment(models.Model):
    """One group the marketing work is aimed at, with the rule that defines it.

    ``criteria`` is a dictionary rather than a description because a segment
    described in prose cannot be counted, exported or reproduced. Stored as
    ``{"industry": "aged care", "role": "operations manager"}`` it can be
    handed to whatever system actually holds the contacts.

    ``estimated_size`` is an estimate and is only ever useful alongside the
    basis for it, which is why the tool that writes these rows states the basis
    in ``description``. A bare number here would be read as a measurement.
    """

    name = models.CharField(max_length=160)
    description = models.TextField(
        blank=True,
        help_text='What this segment is, and where any size estimate came from.')
    criteria = models.JSONField(
        default=dict, blank=True,
        help_text='The rule that defines membership, as field/value pairs.')
    estimated_size = models.PositiveIntegerField(
        default=0, help_text='An estimate. The basis for it belongs in description.')
    channel_fit = models.JSONField(
        default=list, blank=True,
        help_text='Channel keys where this segment is actually reachable.')

    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='audience_segments')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='audience_segments')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Audience Segment'
        verbose_name_plural = 'Audience Segments'

    def __str__(self):
        return self.name

    @property
    def criteria_summary(self):
        pairs = [f'{key.replace("_", " ")}: {value}'
                 for key, value in (self.criteria or {}).items()]
        return '; '.join(pairs) or 'no criteria recorded'

    @property
    def channel_labels(self):
        return [channel_label(key) for key in (self.channel_fit or [])]
