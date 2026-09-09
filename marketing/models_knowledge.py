"""The company knowledge base, and the record of what research answered.

WHY THIS MODULE IS THE LOAD-BEARING ONE
---------------------------------------
Five of the six AI employees are forbidden from inventing a company fact. The
HR employee may not improvise a leave entitlement, the Support employee may
not improvise a refund window, and the Marketing employee may not improvise a
product claim, because an improvised policy quoted back to an employee -- or
an improvised number printed in a published post -- is a real problem with a
real cost. The prohibition is only credible if there is somewhere for those
employees to get the fact instead. This module is that somewhere.

WHY RETRIEVAL IS CHUNK-LEVEL AND NOT DOCUMENT-LEVEL
---------------------------------------------------
A forty-page employee handbook is the wrong answer to "how much notice do I
give for annual leave". It is a *correct* answer, in that the sentence is
somewhere inside it, and it is a *useless* one, because the reader still has
to do the work. Worse, when the retrieved unit is a whole document the
language model receives forty pages of context, and the paragraph that
actually answers the question competes for attention with thirty-nine pages
that do not.

So ``KnowledgeDocument`` is the unit of provenance and ``DocumentChunk`` is
the unit of retrieval. A document is indexed once and split into overlapping
passages, each carrying the nearest heading above it and the distinctive terms
inside it. Search scores passages; an answer quotes the passage; the citation
names the document. The overlap exists because a sentence that straddles a
passage boundary would otherwise be findable from neither side of it.

WHY A CITATION IS A ROW AND NOT A LINE OF PROSE
-----------------------------------------------
It is easy to write an answer that mentions its sources in the body text, and
that is exactly the arrangement that fails quietly. Prose citations cannot be
counted, cannot be listed on a page, cannot be followed to the passage they
came from, and cannot be checked against anything. A claim whose source cannot
be produced on demand is indistinguishable from a claim that was invented --
and it is precisely the kind of claim that ends up in a published marketing
post, where the company then owns it.

``ResearchCitation`` therefore stores the pointer, not the sentence: which
document, which passage, what was quoted, and how relevant it was judged to
be. The report body carries inline markers such as ``[1]`` and ``[2]``; those
markers resolve against the citation rows by ``ordinal``. The consequence is
that "show me the source for this sentence" is a database query rather than an
act of faith, and a report whose citations were deleted alongside their
documents still says truthfully that the source is gone rather than presenting
an orphaned assertion as fact.

WHAT LIVES HERE
---------------
``KnowledgeSource``    where documents come from: a Drive folder, a Notion
                       database, a Confluence space, or manual entry. The
                       connection details are a row rather than code, in
                       keeping with the rest of the platform.

``KnowledgeDocument``  one document, with its content, its provenance and a
                       checksum so re-indexing unchanged content is free.

``DocumentChunk``      one retrievable passage of one document.

``ResearchReport``     an answer the Research employee produced, kept because
                       an answer that vanishes when the chat window closes
                       cannot be audited later.

``ResearchCitation``   one source behind one report, as described above.
"""

from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q

from .models import AIAgent


# ===========================================================================
# 1. SOURCES -- where the knowledge comes from
# ===========================================================================

class KnowledgeSource(models.Model):
    """One origin for company documents.

    ``integration_key`` is a plain CharField rather than a foreign key to
    ``Integration`` deliberately. A source describes where documents came
    from, which stays true whether or not the matching integration row still
    exists, and a knowledge base that loses the provenance of its documents
    because somebody deleted a connector row would be worse than one that
    keeps a string it can no longer resolve.
    """

    KIND_CHOICES = [
        ('drive', 'Google Drive Folder'),
        ('notion', 'Notion Database'),
        ('confluence', 'Confluence Space'),
        ('github_wiki', 'GitHub Wiki'),
        ('manual', 'Entered by Hand'),
        ('upload', 'Uploaded Files'),
        ('web', 'Public Web Page'),
    ]
    SYNC_CHOICES = [
        ('manual', 'Manual -- only when somebody asks'),
        ('on_demand', 'On demand -- refreshed when an employee needs it'),
    ]

    name = models.CharField(max_length=140)
    kind = models.CharField(max_length=15, choices=KIND_CHOICES, default='manual')
    integration_key = models.CharField(
        max_length=40, blank=True,
        help_text="Provider key of the integration that reads it, e.g. 'google_drive'.")
    root_reference = models.CharField(
        max_length=300, blank=True,
        help_text='Drive folder id, Confluence space key, Notion database id, repository name.')

    is_enabled = models.BooleanField(default=True)
    sync_mode = models.CharField(max_length=12, choices=SYNC_CHOICES, default='manual')

    last_synced_at = models.DateTimeField(null=True, blank=True)
    document_count = models.PositiveIntegerField(default=0)
    last_sync_message = models.CharField(max_length=400, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Knowledge Source'
        verbose_name_plural = 'Knowledge Sources'

    def __str__(self):
        return self.label

    @property
    def label(self):
        """A one-line description fit for a citation or a select box."""
        return f'{self.name} ({self.get_kind_display()})'


# ===========================================================================
# 2. DOCUMENTS -- the unit of provenance
# ===========================================================================

class KnowledgeDocument(models.Model):
    """One company document the workforce may cite.

    ``checksum`` is the sha256 of the content. It exists so that re-indexing
    is idempotent: a synchronisation that pulls the same fifty Drive files
    every morning must not rewrite fifty rows and rebuild several hundred
    chunks for nothing, and the cheapest honest way to know nothing changed is
    to hash what arrived and compare.

    ``source`` is SET_NULL rather than CASCADE. Removing a Drive folder from
    the configuration should stop new documents arriving from it; it should not
    silently delete the policy that three answers already cite.
    """

    DOC_TYPE_CHOICES = [
        ('policy', 'Policy'),
        ('handbook', 'Handbook'),
        ('faq', 'FAQ'),
        ('product', 'Product Documentation'),
        ('technical', 'Technical Documentation'),
        ('brand', 'Brand & Tone'),
        ('troubleshooting', 'Troubleshooting Guide'),
        ('process', 'Process & Procedure'),
        ('report', 'Report'),
        ('other', 'Other'),
    ]

    source = models.ForeignKey(KnowledgeSource, on_delete=models.SET_NULL, null=True,
                               blank=True, related_name='documents')
    title = models.CharField(max_length=250)
    doc_type = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES, default='other')

    external_id = models.CharField(
        max_length=200, blank=True,
        help_text='Identifier in the originating system, blank for a document entered here.')
    url = models.CharField(max_length=500, blank=True)

    content = models.TextField()
    summary = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    department = models.CharField(max_length=80, blank=True)
    version = models.CharField(max_length=30, default='1')
    author = models.CharField(max_length=140, blank=True)

    checksum = models.CharField(
        max_length=64, blank=True,
        help_text='sha256 of the content, so re-indexing unchanged text is a no-op.')
    word_count = models.PositiveIntegerField(default=0)
    indexed_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='knowledge_documents')
    created_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='knowledge_documents')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title']
        verbose_name = 'Knowledge Document'
        verbose_name_plural = 'Knowledge Documents'
        indexes = [
            models.Index(fields=['doc_type'], name='knowledge_doc_type_idx'),
        ]
        constraints = [
            # Two documents from the same source cannot share an external id,
            # which is what makes a repeated synchronisation an update rather
            # than a duplicate. The condition matters: documents entered by
            # hand have no external id, and an unconditional constraint would
            # allow exactly one of them per source.
            models.UniqueConstraint(
                fields=['source', 'external_id'],
                condition=~Q(external_id=''),
                name='uniq_document_per_source_external_id'),
        ]

    def __str__(self):
        return self.title

    @property
    def snippet(self):
        """The opening of the document, on one line, for a list row."""
        flat = ' '.join((self.content or '').split())
        return flat[:220]

    @property
    def citation_label(self):
        """How this document is named in a citation.

        The version is included because a policy answer is only checkable
        against the version of the policy that was read.
        """
        parts = [self.title]
        if self.version and self.version != '1':
            parts.append(f'v{self.version}')
        if self.source_id and self.source is not None:
            parts.append(self.source.name)
        return ' -- '.join(parts)

    @property
    def chunk_count(self):
        return self.chunks.count()


class DocumentChunk(models.Model):
    """One retrievable passage of one document.

    THIS IS THE UNIT SEARCH ACTUALLY SCORES
    ---------------------------------------
    Asking "what is the notice period for annual leave" of a forty-page
    handbook should return the paragraph headed 'Notice and approval', not the
    handbook. Scoring passages rather than documents is what makes that
    possible, and carrying ``heading`` is what lets the answer say where in
    the document the passage came from -- which is the difference between a
    citation a reader can verify in ten seconds and one they cannot verify at
    all.

    ``keywords`` holds the distinctive terms of the passage, computed at index
    time. It is a small deliberate redundancy: it lets the interface show why
    a passage exists without loading its text, and it gives the scorer a cheap
    signal for terms that characterise this passage rather than the document.

    CASCADE here, unlike on the document's source, is correct: a chunk has no
    meaning without its document, and a stale chunk pointing at nothing would
    be retrievable and uncitable at the same time.
    """

    document = models.ForeignKey(KnowledgeDocument, on_delete=models.CASCADE,
                                 related_name='chunks')
    ordinal = models.PositiveIntegerField(default=0)
    text = models.TextField()
    heading = models.CharField(max_length=250, blank=True)
    keywords = models.JSONField(default=list, blank=True)
    word_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['document', 'ordinal']
        verbose_name = 'Document Chunk'
        verbose_name_plural = 'Document Chunks'
        constraints = [
            models.UniqueConstraint(fields=['document', 'ordinal'],
                                    name='uniq_chunk_ordinal_per_document'),
        ]

    def __str__(self):
        where = self.heading or f'passage {self.ordinal + 1}'
        return f'{self.document_id}: {where}'


# ===========================================================================
# 3. REPORTS AND CITATIONS -- what research answered, and on what basis
# ===========================================================================

class ResearchReport(models.Model):
    """One answer the Research employee produced, kept.

    An answer given in a chat window and then forgotten cannot be audited, and
    an unauditable answer is the thing this whole subsystem exists to avoid.
    Keeping the report means the question "where did the figure in that post
    come from" has an answer months later: this report, these citations, these
    documents, at these versions.

    ``confidence`` is derived from the search itself -- how strongly the top
    passages matched and how far ahead of the rest they were -- not from a
    language model's opinion of its own answer. ``coverage_note`` records what
    was searched and what was not found, because "the knowledge base does not
    cover this" is a useful finding and needs somewhere to live.
    """

    KIND_CHOICES = [
        ('answer', 'Direct Answer'),
        ('summary', 'Summary'),
        ('comparison', 'Comparison'),
        ('report', 'Research Report'),
        ('executive_summary', 'Executive Summary'),
    ]

    title = models.CharField(max_length=250)
    question = models.TextField(blank=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='answer')

    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='research_reports')
    summary = models.TextField(blank=True)
    body = models.TextField(blank=True)
    confidence = models.DecimalField(
        max_digits=4, decimal_places=3, default=0,
        help_text='0 to 1, computed from the retrieval scores rather than asserted.')
    conflicts = models.JSONField(
        default=list, blank=True,
        help_text='Disagreements found between sources, reported rather than resolved.')
    coverage_note = models.TextField(
        blank=True, help_text='What was searched, and what was not found.')

    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='research_reports')
    requested_by_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True,
                                           blank=True, related_name='research_requests')
    delegation = models.ForeignKey('AgentDelegation', on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='reports')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Research Report'
        verbose_name_plural = 'Research Reports'

    def __str__(self):
        return self.title

    @property
    def citation_count(self):
        return self.citations.count()


class ResearchCitation(models.Model):
    """One source behind one report.

    ``source_label``, ``url`` and ``quote`` are copied onto the row rather
    than read through the foreign key every time. That looks like duplication
    and is not: a citation has to remain readable after the document it points
    at has been re-synchronised, re-versioned or removed. What the report
    actually relied upon was the text as it stood when the answer was written,
    so that text is what is stored.
    """

    report = models.ForeignKey(ResearchReport, on_delete=models.CASCADE,
                               related_name='citations')
    document = models.ForeignKey(KnowledgeDocument, on_delete=models.SET_NULL, null=True,
                                 blank=True, related_name='citations')
    source_label = models.CharField(max_length=300, blank=True)
    url = models.CharField(max_length=500, blank=True)
    quote = models.TextField(blank=True)
    relevance = models.DecimalField(max_digits=4, decimal_places=3, default=0)
    ordinal = models.PositiveIntegerField(
        default=1, help_text='Matches the inline marker in the report body, e.g. [1].')

    class Meta:
        ordering = ['ordinal']
        verbose_name = 'Research Citation'
        verbose_name_plural = 'Research Citations'

    def __str__(self):
        return f'[{self.ordinal}] {self.source_label or "unknown source"}'
