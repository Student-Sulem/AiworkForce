"""The Knowledge and Research employee, and the lookup tools every colleague shares.

WHY THREE OF THESE TOOLS ARE SHARED
-----------------------------------
Five of the six employees are told never to invent a company fact. A rule like
that is only honest if the alternative is available at the moment the fact is
needed: telling the Marketing employee "do not guess the refund window" and
then giving it no way to find the refund window produces exactly the behaviour
the rule was written to prevent, because the model will fill the gap rather
than stop.

So ``knowledge.search``, ``knowledge.get_document`` and
``knowledge.ask_research`` carry ``shared=True`` and appear in every
employee's toolbox. The Support employee drafting a refund reply can quote the
policy. The Marketing employee writing a launch post can check the product
claim. Neither has to guess, and neither has to route through a person.

WHY ``ask_research`` EXISTS ALONGSIDE ``search``
------------------------------------------------
Search returns passages; a colleague usually wants an answer. ``ask_research``
is the agent-to-agent path: it records an ``AgentDelegation`` row, runs the
retrieval, writes a ``ResearchReport`` with citation rows, marks the
delegation answered, and hands back the answer with its sources. The whole
exchange is a database record afterwards, which is what makes the provenance
of a sentence in a published post traceable to the document it came from.

It performs no language-model call. The answer is assembled from the retrieved
passages themselves, which means it is sometimes blunter than a generated one
and always checkable -- and when the knowledge base does not cover the
question it says so and names what it looked for, rather than producing a
fluent paragraph about nothing.

WHY NOTHING HERE GENERATES TEXT
-------------------------------
Everything in this module is deterministic. Summaries are extractive, so every
sentence appeared verbatim in the source. Answers are assembled from retrieved
passages, so every claim has a passage behind it. Conflicts are reported and
never resolved, because choosing between two documents that disagree is a
decision with a cost and belongs to a person.
"""

import math
import re

from django.utils import timezone

from .. import integrations, knowledge
from ..models import AIAgent
from ..models_knowledge import (KnowledgeDocument, KnowledgeSource,
                                ResearchCitation, ResearchReport)
from ..models_platform import AgentDelegation
from .base import Proposal, ToolResult, editable, executor, tool

# ===========================================================================
# Small shared helpers
# ===========================================================================

DEPTH_LIMITS = {'brief': 4, 'normal': 8, 'deep': 14}

# A passage scoring below this fraction of the best hit is padding rather than
# evidence, and padding in an answer is worse than a shorter answer: it reads
# as corroboration.
RELEVANCE_FLOOR = 0.30

# Coordination matching: the share of the best passage's matched terms that
# another passage must also match before it is treated as evidence rather
# than as a coincidence of vocabulary. At 0.8 this means all of them for a
# short question and all but one or two for a long one, which is strict on
# purpose -- an extra paragraph in an answer costs more than a missing one,
# because it reads as a second source agreeing.
COORDINATION_RATIO = 0.8

# Below this confidence an answer is not offered at all. The alternative is
# offering one, and a fluent paragraph assembled from passages that do not
# address the question is precisely the output this platform must not produce.
ANSWER_CONFIDENCE_FLOOR = 0.50

# Patterns used by extract_key_information and find_sources. Kept local
# because they answer a different question from the ones in knowledge.py:
# there the job is to spot a disagreement, here it is to list what a document
# commits the company to.
_FIGURE = re.compile(
    r'(?:[$£€]\s?\d[\d,]*(?:\.\d{1,2})?'
    r'|\d+(?:\.\d+)?\s*(?:per cent|percent|%)'
    r'|\d+(?:\.\d+)?\s*(?:business|working|calendar)?\s*'
    r'(?:days?|hours?|minutes?|weeks?|months?|years?)'
    r'|\b\d[\d,]{2,}\b)', re.IGNORECASE)
_DATE = re.compile(
    r'(?:\d{4}-\d{2}-\d{2}'
    r'|\d{1,2}/\d{1,2}/\d{2,4}'
    r'|\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)(?:\s+\d{4})?'
    r'|\b(?:January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+\d{1,2}(?:,?\s+\d{4})?)', re.IGNORECASE)
_OBLIGATION = re.compile(
    r'\b(?:must|must not|shall|shall not|may not|is required|are required|'
    r'is not permitted|are not permitted|never|always|is prohibited|'
    r'are prohibited|is expected|are expected|within)\b', re.IGNORECASE)
_DEFINITION = re.compile(
    r'\b(?:means|is defined as|are defined as|refers to|is counted from|'
    r'for the purposes of this)\b', re.IGNORECASE)
_ENTITY = re.compile(r'\b(?:[A-Z][a-z]{2,}|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]{2,}|[A-Z]{2,}|of|and|for))*\b')
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"\'(])')

# A short local measurement pattern for classifying a claim against a
# document. See find_sources.
_CLAIM_NUMBER = re.compile(
    r'(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>business days|working days|'
    r'calendar days|days|business hours|hours|minutes|weeks|months|years|'
    r'per cent|percent|%)', re.IGNORECASE)

# Unit vocabulary never counts as evidence that two statements are about the
# same subject. Without this, a refund processed in 5 business days is
# reported as contradicting a support resolution target of 10 business days,
# on the strength of both containing the words "business" and "days".
_UNIT_VOCAB = frozenset({
    'business', 'working', 'calendar', 'day', 'days', 'hour', 'hours',
    'minute', 'minutes', 'week', 'weeks', 'month', 'months', 'year', 'years',
    'percent', 'cent', 'within', 'target', 'targets',
})


def _research_agent():
    """The Knowledge and Research employee row, or None if not provisioned."""
    return AIAgent.objects.filter(agent_type='research').first()


def _sentences(text):
    """Sentences, with heading lines removed first.

    The removal matters: a sentence splitter fed raw markdown treats a
    heading and the paragraph beneath it as one sentence, and every
    per-sentence rule below -- obligations, definitions, entities -- then
    reports one enormous match instead of the dozen real ones.
    """
    flat = knowledge.prose_only(text)
    return [part.strip() for part in _SENTENCE_SPLIT.split(flat) if part.strip()]


def _report_prose(report):
    """A research report's narrative, without its apparatus.

    Source lists, gap notes and the confidence line are metadata about the
    answer rather than part of it. Summarising them alongside the findings
    produces an "executive summary" made largely of citation labels, which is
    the opposite of the point.
    """
    body = report.body or report.summary or ''
    skipping = False
    kept = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(('Sources', 'Gaps', 'Coverage', 'Conflicts',
                            'Confidence', 'GAP:', 'Question:', 'Neither side',
                            'Nowhere in the knowledge base', 'Not matched by')):
            skipping = True
            continue
        if line.startswith(('Findings', 'Answer', 'Research report')):
            skipping = False
            continue
        if skipping or line.startswith('['):
            continue
        kept.append(line.lstrip('- ').strip())
    return ' '.join(' '.join(kept).split()) or body


def _hit_lines(hits, *, numbered=True):
    """Retrieved passages rendered for a language model to read.

    Document ids are included on every line on purpose. The next thing a model
    does with a search result is ask for one of the documents in full, and a
    result that named its documents only by title forces it to invent an id or
    run a second search.
    """
    lines = []
    for index, hit in enumerate(hits, start=1):
        marker = f'[{index}] ' if numbered else '- '
        where = f" -- section '{hit.heading}'" if hit.heading else ''
        lines.append(
            f'{marker}{hit.title}{where} (document #{hit.document_id}, '
            f'{hit.document.get_doc_type_display()}, relevance '
            f'{hit.score:.2f})\n    "{hit.snippet}"\n    Why: {hit.why}')
    return '\n'.join(lines)


def _section_note(hit):
    """" (Section name)", or nothing when the heading only repeats the title.

    The opening passage of a document carries the document's own title as its
    heading, and "[1] (Customer Refund Policy)" beside a citation that already
    says Customer Refund Policy is noise dressed as precision.
    """
    heading = (hit.heading or '').strip()
    if not heading or heading.lower() == (hit.title or '').strip().lower():
        return ''
    return f' ({heading})'


def _unmatched_terms(query, hits):
    """Query terms that no retrieved passage matched.

    Reported rather than hidden, because "we have nothing written down about
    carry-over" is the single most actionable finding a search can produce.
    """
    terms = [term for term in knowledge.content_terms(query)]
    seen = set()
    for hit in hits:
        seen.update(hit.matched_terms or [])
    ordered = []
    for term in terms:
        if term not in seen and term not in ordered:
            ordered.append(term)
    return ordered


def _focus(query, hits):
    """Drop the passages that are not about the subject of the question.

    WHY FILTERING BEATS TRUSTING THE RANK
    -------------------------------------
    Asked how long an approved refund takes, the ranker will happily return,
    in fourth place, a paragraph from the onboarding process -- because it
    contains "how long" and "process". It is a correct ranking and a wrong
    answer, since an answer built from four paragraphs reads as four sources
    agreeing when only two of them were about refunds.

    Two filters therefore run before an answer is assembled. The first drops
    passages scoring far below the best as padding. The second is coordination
    matching: a passage must match roughly as many of the query's distinct
    terms as the best passage did. The onboarding paragraph matched two terms
    out of five where the refund policy matched four, and that ratio -- not
    its BM25 score -- is what identifies it as a coincidence of vocabulary.

    Neither filter is allowed to empty the list, because a weak answer that
    says it is weak beats no answer with no explanation. The confidence figure
    and the gap warning carry that weakness to the reader.

    Returns ``(kept_hits, coverage)`` where coverage is the term_coverage map,
    so the caller does not pay for it twice.
    """
    coverage = knowledge.term_coverage(query)
    kept = [hit for hit in hits if hit.score >= RELEVANCE_FLOOR] or list(hits[:1])

    best = max((len(set(hit.matched_terms or [])) for hit in kept), default=0)
    if best >= 2:
        need = max(2, math.ceil(best * COORDINATION_RATIO))
        strong = [hit for hit in kept
                  if len(set(hit.matched_terms or [])) >= need]
        if strong:
            kept = strong
    return kept, coverage


def _confidence(query, hits, coverage=None):
    """A confidence figure derived from the retrieval, not asserted.

    Three ingredients, because any one of them alone is misleading. Term
    coverage says whether the passages addressed the whole question or only
    part of it. The mean of the top scores says whether the corpus answered
    strongly or scraped a match. Corroboration says whether more than one
    document agrees.

    Then the whole thing is multiplied by the square of the proportion of the
    question's substantive words the corpus contains at all. That factor is
    the one that matters, and it is squared deliberately: a question whose
    subject word appears in no document must not come back with a confident
    answer assembled out of its other words. The result is capped below 1.0,
    because retrieval can show that a document says something and never that
    the document is right.
    """
    if not hits:
        return 0.0
    terms = set(knowledge.content_terms(query))
    if not terms:
        return 0.0

    matched = set()
    for hit in hits:
        matched.update(hit.matched_terms or [])
    term_ratio = len(matched & terms) / len(terms)
    window = hits[:3]
    mean_top = sum(hit.score for hit in window) / len(window)
    documents = {hit.document_id for hit in hits}
    corroboration = min(len(documents), 3) / 3.0
    value = 0.50 * term_ratio + 0.35 * mean_top + 0.15 * corroboration

    coverage = coverage if coverage is not None else knowledge.term_coverage(query)
    substantive = coverage.get('substantive') or []
    if substantive:
        absent = set(coverage.get('absent') or [])
        present = [term for term in substantive if term not in absent]
        value *= (len(present) / len(substantive)) ** 2

    return round(min(value, 0.95), 3)


def _gap_warning(coverage):
    """A leading warning naming the words no document contains, or ''."""
    absent = [term for term in (coverage.get('absent') or [])
              if term in (coverage.get('substantive') or [])]
    if not absent:
        return ''
    present = [term for term in (coverage.get('substantive') or [])
               if term not in absent]
    note = (f'GAP: no document in the knowledge base contains '
            f'{", ".join(absent)}.')
    if present:
        note += (f' What follows was matched on {", ".join(present)} only, so it '
                 f'may not address the part of the question that matters.')
    return note


def _coverage_note(query, hits, *, doc_type='', department=''):
    """What was searched and what was not found, as prose."""
    scope = []
    if doc_type:
        scope.append(f'document type "{doc_type}"')
    if department:
        scope.append(f'department "{department}"')
    where = f' restricted to {" and ".join(scope)}' if scope else ' across the whole knowledge base'

    parts = [f'Searched for "{query}"{where}.']
    if hits:
        documents = []
        for hit in hits:
            if hit.title not in documents:
                documents.append(hit.title)
        parts.append(
            f'{len(hits)} passage(s) returned from {len(documents)} document(s): '
            f'{", ".join(documents)}.')
    else:
        parts.append('No passage matched.')

    missing = _unmatched_terms(query, hits)
    if missing:
        parts.append(
            'No document mentioned: ' + ', '.join(missing) +
            '. That is a gap in the knowledge base rather than an answer.')
    return ' '.join(parts)


def _save_report(*, title, question, kind, body, summary, hits, confidence,
                 conflicts=None, coverage_note='', ctx=None, requested_by_agent=None,
                 delegation=None):
    """Write the report and its citation rows.

    The citations are written as rows even though the body already carries
    ``[1]`` markers, because a marker in prose cannot be followed, counted or
    checked. Storing the quote on the row is deliberate duplication: what the
    report relied on was the passage as it read when the answer was written,
    and that is what a reviewer months later needs to see.
    """
    report = ResearchReport.objects.create(
        title=title[:250],
        question=question,
        kind=kind,
        agent=getattr(ctx, 'agent', None) if ctx else None,
        summary=summary,
        body=body,
        confidence=confidence,
        conflicts=list(conflicts or []),
        coverage_note=coverage_note,
        requested_by=(ctx.user if ctx and getattr(getattr(ctx, 'user', None), 'pk', None)
                      else None),
        requested_by_agent=requested_by_agent,
        delegation=delegation,
    )
    for ordinal, citation in enumerate(knowledge.citations_for(hits), start=1):
        ResearchCitation.objects.create(
            report=report,
            document_id=citation['document_id'],
            source_label=citation['label'][:300],
            url=citation['url'][:500],
            quote=citation['quote'],
            relevance=citation['relevance'],
            ordinal=ordinal,
        )
    return report


def _assemble_answer(question, hits, *, sentences_per_source=2):
    """Build an answer out of the retrieved passages themselves.

    Each paragraph is the most informative sentences of one passage, followed
    by the citation marker for the document it came from. That is a blunter
    answer than a model would write and it has a property a written one does
    not: every sentence in it exists, verbatim, in a company document. The
    marker is not decoration -- it resolves against a ResearchCitation row.
    """
    citations = knowledge.citations_for(hits)
    order = {entry['document_id']: index for index, entry in enumerate(citations, start=1)}

    # One paragraph per document, taking that document's best passage. Three
    # passages from the same handbook are one point made three times, and a
    # reader counting paragraphs would read it as three sources agreeing.
    used = set()
    paragraphs = []
    for hit in hits:
        marker = order.get(hit.document_id)
        if marker is None or hit.document_id in used:
            continue
        used.add(hit.document_id)
        passage = knowledge.summarise(getattr(hit.chunk, 'text', '') or hit.snippet,
                                      max_sentences=sentences_per_source)
        paragraphs.append(f'{passage} [{marker}]{_section_note(hit)}')

    if not paragraphs:
        return '', citations

    lines = [f'Question: {question}', '', 'Answer, from the company knowledge base:', '']
    lines.extend(paragraphs)
    lines.extend(['', 'Sources:'])
    for index, citation in enumerate(citations, start=1):
        suffix = f' -- {citation["url"]}' if citation['url'] else ''
        lines.append(f'[{index}] {citation["label"]} '
                     f'(document #{citation["document_id"]}, relevance '
                     f'{citation["relevance"]}){suffix}')
    return '\n'.join(lines), citations


def _nothing_found(question, tried, *, scope=''):
    """The honest empty answer, with the search terms named.

    Naming the terms is the whole value of this branch. "I could not find
    anything" tells the reader nothing; "I searched for refund, carry, over
    and found no document mentioning carry-over" tells them precisely which
    document is missing.
    """
    words = ', '.join(tried) if tried else 'no distinctive search terms'
    where = scope or 'the whole knowledge base'
    return (f'The knowledge base does not cover this. I searched {where} for: '
            f'{words}. Nothing relevant was returned, so there is no company '
            f'document behind an answer to "{question}". Do not answer it from '
            f'general knowledge -- either ask for the document to be added, or '
            f'say plainly that it is not documented.')


def _not_covered(question, coverage, confidence=None):
    """The refusal an answering tool gives, with the evidence for refusing.

    "I could not find anything" is useless; this says which words were
    searched, which of them no document contains, and what the retrieval
    confidence was. That turns a refusal into a work item -- somebody now
    knows exactly which document is missing.
    """
    substantive = coverage.get('substantive') or []
    absent = [term for term in (coverage.get('absent') or []) if term in substantive]
    tried = coverage.get('terms') or []

    parts = [f'The knowledge base does not cover "{question}".',
             f'Searched the whole knowledge base for: '
             f'{", ".join(tried) or "no distinctive terms"}.']
    if absent:
        parts.append(f'No document contains: {", ".join(absent)}.')
    if confidence is not None:
        parts.append(
            f'The passages that did come back reach a retrieval confidence of only '
            f'{confidence}, below the {ANSWER_CONFIDENCE_FLOOR} floor at which an '
            f'answer is offered, so none is offered -- assembling one from those '
            f'passages would read as an answer without being one.')
    parts.append('Do not answer this from general knowledge. Either ask for the '
                 'document to be added, or say plainly that it is not documented.')
    return ' '.join(parts)


# ===========================================================================
# 1. SHARED LOOKUP -- the tools every employee gets
# ===========================================================================

@tool(name='knowledge.search',
      title='Search the company knowledge base',
      description=('Search company documents, policies, product and technical '
                   'material by keyword and return the passages that match, each '
                   'with its document title, an extract and a citation label. Use '
                   'this instead of stating a company fact from memory.'),
      group='Knowledge Search', agent_types=('research',), shared=True,
      reads_only=True, icon='fa-magnifying-glass', integration='knowledge_base',
      capability='Knowledge base search',
      parameters={
          'type': 'object',
          'properties': {
              'query': {'type': 'string',
                        'description': 'What to look for, in the words a person would use.'},
              'limit': {'type': 'integer', 'description': 'Passages to return, default 6.'},
              'doc_type': {'type': 'string',
                           'description': ('Optional filter: policy, handbook, faq, product, '
                                           'technical, brand, troubleshooting, process, report.')},
          },
          'required': ['query'],
      })
def shared_search(ctx, query, limit=6, doc_type=''):
    """Keyword search over document passages, returned with citations."""
    hits = knowledge.search(query, limit=int(limit or 6), doc_type=doc_type or '')
    if not hits:
        tried = knowledge.content_terms(query)
        return ToolResult(
            ok=True, text=_nothing_found(query, tried), data={'hits': [], 'query': query})

    text = (f'{len(hits)} passage(s) matched "{query}".\n\n' + _hit_lines(hits) +
            '\n\nCite the document title when you use any of this. Call '
            'knowledge.get_document with a document id to read one in full.')
    return ToolResult(ok=True, text=text,
                      data={'query': query, 'hits': [hit.as_dict() for hit in hits],
                            'citations': knowledge.citations_for(hits)})


@tool(name='knowledge.get_document',
      title='Read a knowledge base document',
      description=('Read one company document in full by its id, together with its '
                   'type, version, department and source. Use it after a search when '
                   'an extract is not enough.'),
      group='Knowledge Search', agent_types=('research',), shared=True,
      reads_only=True, icon='fa-file-lines', integration='knowledge_base',
      capability='Read a knowledge document',
      parameters={
          'type': 'object',
          'properties': {
              'document_id': {'type': 'integer',
                              'description': 'Id from a knowledge.search result.'},
          },
          'required': ['document_id'],
      })
def shared_get_document(ctx, document_id):
    """Return one document in full, or advice on how to find the right id."""
    document = knowledge.get_document(document_id)
    if document is None:
        return ToolResult(
            ok=False, error=f'No document #{document_id}.',
            text=(f'There is no knowledge base document with id {document_id}. Run '
                  f'knowledge.search first and use a document id from the results, '
                  f'or research.list_documents to see what exists.'))

    header = (f'Document #{document.pk}: {document.title}\n'
              f'Type: {document.get_doc_type_display()} | Version: {document.version} | '
              f'Department: {document.department or "unspecified"} | '
              f'Words: {document.word_count} | '
              f'Source: {document.source.label if document.source else "entered here"}\n'
              f'Citation label: {document.citation_label}')
    if document.url:
        header += f'\nURL: {document.url}'

    return ToolResult(
        ok=True, text=f'{header}\n\n{document.content}',
        subject_label='marketing.knowledgedocument', subject_id=document.pk,
        data={'document_id': document.pk, 'title': document.title,
              'doc_type': document.doc_type, 'version': document.version,
              'word_count': document.word_count, 'tags': document.tags,
              'citation_label': document.citation_label})


@tool(name='knowledge.ask_research',
      title='Ask the research employee a company question',
      description=('Put a question about company facts, policies or product detail to '
                   'the Knowledge and Research employee and receive an answer with its '
                   'sources. Use this rather than stating a company fact you are not '
                   'certain of. The exchange is recorded as a delegation.'),
      group='Agent Support', agent_types=('research',), shared=True,
      reads_only=True, icon='fa-people-arrows', integration='knowledge_base',
      capability='Ask research for a sourced answer',
      parameters={
          'type': 'object',
          'properties': {
              'question': {'type': 'string',
                           'description': 'The question, phrased as you would ask a colleague.'},
              'context': {'type': 'string',
                          'description': 'Optional: why you need it, which sharpens the search.'},
          },
          'required': ['question'],
      })
def ask_research(ctx, question, context=''):
    """Delegate a company question to research and get back a cited answer.

    THE COLLABORATION PATH, WITHOUT A MODEL CALL
    --------------------------------------------
    The employee that asks is not the employee that answers, and the answer is
    not generated: it is assembled from the passages retrieval returned, with
    the citation rows written alongside it. That is what makes the exchange
    worth recording. Six months later the question "where did the refund
    figure in that post come from" resolves to this delegation, this report
    and these documents.
    """
    researcher = _research_agent()
    asker = getattr(ctx, 'agent', None)

    # Both ends of a delegation are required columns, so an exchange is only
    # recorded when there really are two employees. A tool called from a
    # script or from the research employee itself still answers.
    delegation = None
    if (researcher is not None and asker is not None
            and getattr(asker, 'pk', None) and asker.pk != researcher.pk):
        delegation = AgentDelegation.objects.create(
            from_agent=asker, to_agent=researcher,
            task=getattr(ctx, 'task', None),
            request=f'{question}\n\nContext: {context}'.strip(),
            status='pending')

    search_text = f'{question} {context}'.strip()
    hits, coverage_map = _focus(search_text, knowledge.search(search_text, limit=6))
    confidence = _confidence(search_text, hits, coverage_map)

    if not hits or confidence < ANSWER_CONFIDENCE_FLOOR:
        answer = _not_covered(question, coverage_map,
                              confidence if hits else None)
        if delegation is not None:
            delegation.response = answer
            delegation.status = 'answered'
            delegation.answered_at = timezone.now()
            delegation.save(update_fields=['response', 'status', 'answered_at'])
        return ToolResult(ok=True, text=answer,
                          data={'answered': False, 'question': question,
                                'terms_tried': coverage_map.get('terms', []),
                                'absent_terms': coverage_map.get('absent', []),
                                'confidence': confidence,
                                'delegation_id': delegation.pk if delegation else None})

    body, citations = _assemble_answer(question, hits)
    conflicts = knowledge.detect_conflicts(hits)
    coverage = _coverage_note(search_text, hits)

    warning = _gap_warning(coverage_map)
    if warning:
        body = f'{warning}\n\n{body}'

    if conflicts:
        body += ('\n\nDisagreement between sources, reported and not resolved:\n' +
                 '\n'.join(f'- {entry["note"]}' for entry in conflicts))

    report = _save_report(
        title=f'Answer: {question}'[:250], question=question, kind='answer',
        body=body, summary=knowledge.summarise(body, max_sentences=3),
        hits=hits, confidence=confidence, conflicts=conflicts,
        coverage_note=coverage, ctx=ctx, requested_by_agent=asker,
        delegation=delegation)

    if delegation is not None:
        delegation.response = body
        delegation.status = 'answered'
        delegation.answered_at = timezone.now()
        delegation.sources = citations
        delegation.save(update_fields=['response', 'status', 'answered_at', 'sources'])

    who = f'{researcher.name} answered' if researcher else 'Research answered'
    text = (f'{who} from {len(citations)} source(s), confidence {confidence}.\n\n'
            f'{body}\n\nRecorded as research report #{report.pk}'
            + (f' and delegation #{delegation.pk}' if delegation else '') +
            '. Use the source labels above when you repeat any of this.')

    return ToolResult(ok=True, text=text,
                      subject_label='marketing.researchreport', subject_id=report.pk,
                      data={'answered': True, 'report_id': report.pk,
                            'delegation_id': delegation.pk if delegation else None,
                            'confidence': confidence, 'citations': citations,
                            'conflicts': conflicts})


# ===========================================================================
# 2. KNOWLEDGE SEARCH -- the research employee's own search tools
# ===========================================================================

def _typed_search(ctx, query, limit, doc_type, label):
    """The shared body of the narrowed search tools.

    Narrowed searches exist because the corpus is mixed. A question about the
    refund window searched across everything competes with the brand
    guidelines and the engineering standards for the same words; restricted to
    policies it does not. The narrowing is a filter on the corpus, applied
    before scoring, so the inverse document frequencies describe the
    collection actually being ranked.
    """
    hits = knowledge.search(query, limit=int(limit or 6), doc_type=doc_type)
    if not hits:
        tried = knowledge.content_terms(query)
        return ToolResult(
            ok=True, data={'hits': [], 'doc_type': doc_type},
            text=_nothing_found(query, tried, scope=f'{label} only'))
    return ToolResult(
        ok=True,
        text=(f'{len(hits)} passage(s) in {label} matched "{query}".\n\n'
              + _hit_lines(hits)),
        data={'query': query, 'doc_type': doc_type,
              'hits': [hit.as_dict() for hit in hits],
              'citations': knowledge.citations_for(hits)})


@tool(name='research.search_knowledge',
      title='Search all company knowledge',
      description=('Full keyword search across every company document, optionally '
                   'narrowed by document type or department, returning the matching '
                   'passages with citation labels and relevance scores.'),
      group='Knowledge Search', agent_types=('research',), reads_only=True,
      icon='fa-magnifying-glass-chart', integration='knowledge_base',
      capability='Search all company knowledge',
      parameters={
          'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'What to look for.'},
              'limit': {'type': 'integer', 'description': 'Passages to return, default 8.'},
              'doc_type': {'type': 'string', 'description': 'Optional document type filter.'},
              'department': {'type': 'string', 'description': 'Optional department filter.'},
          },
          'required': ['query'],
      })
def search_knowledge(ctx, query, limit=8, doc_type='', department=''):
    """Search everything, with optional type and department filters."""
    hits = knowledge.search(query, limit=int(limit or 8), doc_type=doc_type or '',
                            department=department or '')
    if not hits:
        return ToolResult(
            ok=True, data={'hits': []},
            text=_nothing_found(query, knowledge.content_terms(query)))

    documents = []
    for hit in hits:
        if hit.title not in documents:
            documents.append(hit.title)
    text = (f'{len(hits)} passage(s) from {len(documents)} document(s) matched '
            f'"{query}".\n\n{_hit_lines(hits)}\n\n'
            f'Documents involved: {", ".join(documents)}.')
    missing = _unmatched_terms(query, hits)
    if missing:
        text += f'\nNo document mentioned: {", ".join(missing)}.'
    return ToolResult(ok=True, text=text,
                      data={'query': query, 'hits': [hit.as_dict() for hit in hits],
                            'citations': knowledge.citations_for(hits),
                            'unmatched_terms': missing})


@tool(name='research.search_policies',
      title='Search company policies',
      description=('Search only documents classified as policies. Use this for leave, '
                   'refunds, privacy, hiring, SLA and any other question where the '
                   'company has a formal position.'),
      group='Knowledge Search', agent_types=('research',), reads_only=True,
      icon='fa-scale-balanced', integration='knowledge_base',
      capability='Search company policies',
      parameters={
          'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'The policy question.'},
              'limit': {'type': 'integer', 'description': 'Passages to return, default 6.'},
          },
          'required': ['query'],
      })
def search_policies(ctx, query, limit=6):
    """Search the policy documents only."""
    return _typed_search(ctx, query, limit, 'policy', 'company policies')


@tool(name='research.search_product_docs',
      title='Search product documentation',
      description=('Search only product documentation. Use this before making any '
                   'claim about what the product is or does.'),
      group='Knowledge Search', agent_types=('research',), reads_only=True,
      icon='fa-box-open', integration='knowledge_base',
      capability='Search product documentation',
      parameters={
          'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'The product question.'},
              'limit': {'type': 'integer', 'description': 'Passages to return, default 6.'},
          },
          'required': ['query'],
      })
def search_product_docs(ctx, query, limit=6):
    """Search product documentation only."""
    return _typed_search(ctx, query, limit, 'product', 'product documentation')


@tool(name='research.search_technical_docs',
      title='Search technical documentation',
      description=('Search only technical and engineering documentation: standards, '
                   'architecture, definition of done, deployment procedure.'),
      group='Knowledge Search', agent_types=('research',), reads_only=True,
      icon='fa-screwdriver-wrench', integration='knowledge_base',
      capability='Search technical documentation',
      parameters={
          'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'The technical question.'},
              'limit': {'type': 'integer', 'description': 'Passages to return, default 6.'},
          },
          'required': ['query'],
      })
def search_technical_docs(ctx, query, limit=6):
    """Search technical documentation only."""
    return _typed_search(ctx, query, limit, 'technical', 'technical documentation')


@tool(name='research.search_faqs',
      title='Search FAQs and troubleshooting guides',
      description=('Search the FAQ and troubleshooting material, which is where the '
                   'symptom-cause-resolution answers for common customer problems live.'),
      group='Knowledge Search', agent_types=('research',), reads_only=True,
      icon='fa-circle-question', integration='knowledge_base',
      capability='Search FAQs and troubleshooting',
      parameters={
          'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'The symptom or question.'},
              'limit': {'type': 'integer', 'description': 'Passages to return, default 6.'},
          },
          'required': ['query'],
      })
def search_faqs(ctx, query, limit=6):
    """Search FAQ and troubleshooting documents.

    Both types are searched because the distinction between them is editorial
    rather than real: the same customer question is filed as an FAQ in one
    company and a troubleshooting entry in another, and a caller asking about
    a symptom should not have to know which.
    """
    wanted = int(limit or 6)
    hits = (knowledge.search(query, limit=wanted, doc_type='faq') +
            knowledge.search(query, limit=wanted, doc_type='troubleshooting'))
    hits.sort(key=lambda hit: -hit.score)
    hits = hits[:wanted]
    if not hits:
        return ToolResult(
            ok=True, data={'hits': []},
            text=_nothing_found(query, knowledge.content_terms(query),
                                scope='FAQs and troubleshooting guides only'))
    return ToolResult(
        ok=True,
        text=(f'{len(hits)} passage(s) in FAQs and troubleshooting guides matched '
              f'"{query}".\n\n{_hit_lines(hits)}'),
        data={'query': query, 'hits': [hit.as_dict() for hit in hits],
              'citations': knowledge.citations_for(hits)})


@tool(name='research.search_employee_docs',
      title='Search employee and HR documents',
      description=('Search the handbook, HR policies and people processes: working '
                   'hours, leave, conduct, probation, onboarding, performance review.'),
      group='Knowledge Search', agent_types=('research',), reads_only=True,
      icon='fa-user-tie', integration='knowledge_base',
      capability='Search employee documents',
      parameters={
          'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'The employee or HR question.'},
              'limit': {'type': 'integer', 'description': 'Passages to return, default 6.'},
          },
          'required': ['query'],
      })
def search_employee_docs(ctx, query, limit=6):
    """Search the handbook and people documents.

    Scoped by department rather than by type, because an employee question is
    answered by a handbook, a policy and a process document in roughly equal
    measure and narrowing to one type would lose two thirds of the answer.
    """
    wanted = int(limit or 6)
    hits = knowledge.search(query, limit=wanted, department='People Operations')
    if not hits:
        hits = [hit for hit in knowledge.search(query, limit=wanted * 2)
                if hit.document.doc_type in ('handbook', 'policy', 'process')][:wanted]
    if not hits:
        return ToolResult(
            ok=True, data={'hits': []},
            text=_nothing_found(query, knowledge.content_terms(query),
                                scope='employee and HR documents only'))
    return ToolResult(
        ok=True,
        text=(f'{len(hits)} passage(s) in employee and HR documents matched '
              f'"{query}".\n\n{_hit_lines(hits)}'),
        data={'query': query, 'hits': [hit.as_dict() for hit in hits],
              'citations': knowledge.citations_for(hits)})


@tool(name='research.list_documents',
      title='List knowledge base documents',
      description=('List the documents in the knowledge base with their ids, types, '
                   'departments and sizes, so a document can be read or compared by id.'),
      group='Knowledge Search', agent_types=('research',), reads_only=True,
      icon='fa-list', integration='knowledge_base',
      capability='List knowledge documents',
      parameters={
          'type': 'object',
          'properties': {
              'doc_type': {'type': 'string', 'description': 'Optional document type filter.'},
              'department': {'type': 'string', 'description': 'Optional department filter.'},
              'limit': {'type': 'integer', 'description': 'Maximum rows, default 40.'},
          },
      })
def list_documents_tool(ctx, doc_type='', department='', limit=40):
    """Every document the workforce can cite, with its id."""
    rows = knowledge.list_documents(doc_type=doc_type or '', department=department or '',
                                    limit=int(limit or 40))
    if not rows:
        scope = doc_type or department or 'the knowledge base'
        return ToolResult(
            ok=True, data={'documents': []},
            text=(f'No documents in {scope}. research.index_document adds one by hand, '
                  f'and research.sync_source pulls documents in from Drive, Notion, '
                  f'Confluence or a GitHub wiki.'))

    lines = [
        f'#{row.pk} {row.title} -- {row.get_doc_type_display()}'
        f'{", " + row.department if row.department else ""}, v{row.version}, '
        f'{row.word_count} words, {row.chunk_count} passage(s)'
        for row in rows
    ]
    return ToolResult(
        ok=True,
        text=f'{len(rows)} document(s) in the knowledge base:\n' + '\n'.join(lines),
        data={'documents': [{'id': row.pk, 'title': row.title,
                             'doc_type': row.doc_type, 'department': row.department,
                             'version': row.version, 'word_count': row.word_count}
                            for row in rows]})


@tool(name='research.knowledge_stats',
      title='Knowledge base statistics',
      description=('Report how many documents, passages and words the knowledge base '
                   'holds, broken down by document type, with the last indexing time.'),
      group='Knowledge Search', agent_types=('research',), reads_only=True,
      icon='fa-chart-simple', integration='knowledge_base',
      capability='Knowledge base statistics',
      parameters={'type': 'object', 'properties': {}})
def knowledge_stats(ctx):
    """What the company has actually written down, counted."""
    stats = knowledge.document_stats()
    if not stats['documents']:
        return ToolResult(
            ok=True, data=stats,
            text=('The knowledge base is empty, so no employee can cite anything. '
                  'Add a document with research.index_document or connect a source '
                  'with research.create_source and research.sync_source.'))

    display = dict(KnowledgeDocument.DOC_TYPE_CHOICES)
    breakdown = ', '.join(f'{display.get(key, key)}: {count}'
                          for key, count in sorted(stats['by_type'].items()))
    last = (stats['last_indexed'].strftime('%d %b %Y at %H:%M')
            if stats['last_indexed'] else 'never')
    return ToolResult(
        ok=True,
        text=(f'Knowledge base: {stats["documents"]} document(s), '
              f'{stats["chunks"]} retrievable passage(s), '
              f'{stats["words"]:,} words, from {stats["sources"]} source(s). '
              f'By type -- {breakdown}. Last indexed {last}.'),
        data=stats)


# ===========================================================================
# 3. ANALYSIS
# ===========================================================================

@tool(name='research.answer_company_question',
      title='Answer a company question with citations',
      description=('Search the knowledge base and answer a question about the company '
                   'from what was found, with inline citation markers, a confidence '
                   'figure derived from the retrieval and a note on what was not '
                   'found. Saves a research report with citation rows. When nothing '
                   'relevant exists it says so and names the terms tried.'),
      group='Analysis', agent_types=('research',), reads_only=True,
      icon='fa-comment-dots', integration='knowledge_base',
      capability='Answer a company question with citations',
      parameters={
          'type': 'object',
          'properties': {
              'question': {'type': 'string', 'description': 'The question to answer.'},
              'depth': {'type': 'string', 'enum': ['brief', 'normal', 'deep'],
                        'description': 'How widely to search. Default normal.'},
          },
          'required': ['question'],
      })
def answer_company_question(ctx, question, depth='normal'):
    """Answer a company question strictly from retrieved passages.

    THE FLAGSHIP, AND WHY IT LOOKS LIKE THIS
    ----------------------------------------
    The answer is built out of the passages, not written about them. Every
    sentence in the body appeared in a company document; every claim carries a
    marker that resolves to a citation row; the confidence figure comes from
    the retrieval scores rather than from anybody's impression; and the
    coverage note records the terms no document mentioned.

    The consequence is that this tool can be wrong in only one interesting
    way -- the knowledge base can be wrong -- and cannot be wrong in the way
    that matters most, which is confidently stating something the company
    never wrote down.
    """
    limit = DEPTH_LIMITS.get(str(depth or 'normal').lower(), 8)
    hits, coverage_map = _focus(question, knowledge.search(question, limit=limit))
    confidence = _confidence(question, hits, coverage_map)

    if not hits or confidence < ANSWER_CONFIDENCE_FLOOR:
        answer = _not_covered(question, coverage_map, confidence if hits else None)
        report = _save_report(
            title=f'Not covered: {question}'[:250], question=question, kind='answer',
            body=answer, summary='The knowledge base does not cover this question.',
            hits=[], confidence=0.0, conflicts=[],
            coverage_note=_coverage_note(question, hits), ctx=ctx)
        return ToolResult(
            ok=True, text=f'{answer}\n\nRecorded as research report #{report.pk}.',
            subject_label='marketing.researchreport', subject_id=report.pk,
            data={'answered': False, 'report_id': report.pk,
                  'terms_tried': coverage_map.get('terms', []),
                  'absent_terms': coverage_map.get('absent', []),
                  'confidence': confidence})

    sentences = 3 if str(depth).lower() == 'deep' else 2
    body, citations = _assemble_answer(question, hits, sentences_per_source=sentences)
    conflicts = knowledge.detect_conflicts(hits)
    coverage = _coverage_note(question, hits)

    warning = _gap_warning(coverage_map)
    if warning:
        body = f'{warning}\n\n{body}'

    if conflicts:
        body += ('\n\nSources disagree. Both sides are reported and neither has been '
                 'chosen:\n' +
                 '\n'.join(f'- {entry["note"]}' for entry in conflicts))
    body += f'\n\nCoverage: {coverage}'

    report = _save_report(
        title=f'Answer: {question}'[:250], question=question, kind='answer',
        body=body, summary=knowledge.summarise(body, max_sentences=3), hits=hits,
        confidence=confidence, conflicts=conflicts, coverage_note=coverage, ctx=ctx)

    verdict = ('strongly supported' if confidence >= 0.7
               else 'partially supported' if confidence >= 0.4
               else 'weakly supported -- treat with care')
    text = (f'{body}\n\nConfidence {confidence} ({verdict}), from '
            f'{len(citations)} source(s) and {len(hits)} passage(s). '
            f'Saved as research report #{report.pk} with {len(citations)} citation(s).')

    return ToolResult(
        ok=True, text=text,
        subject_label='marketing.researchreport', subject_id=report.pk,
        data={'answered': True, 'report_id': report.pk, 'confidence': confidence,
              'citations': citations, 'conflicts': conflicts,
              'coverage_note': coverage,
              'hits': [hit.as_dict() for hit in hits]})


@tool(name='research.summarise_document',
      title='Summarise a document',
      description=('Produce an extractive summary of one knowledge base document and '
                   'save it onto the document, so later searches and listings show it.'),
      group='Analysis', agent_types=('research',), reads_only=False,
      icon='fa-compress', integration='knowledge_base',
      capability='Summarise a document',
      parameters={
          'type': 'object',
          'properties': {
              'document_id': {'type': 'integer', 'description': 'Document to summarise.'},
              'max_sentences': {'type': 'integer',
                                'description': 'Sentences to keep, default 6.'},
          },
          'required': ['document_id'],
      })
def summarise_document(ctx, document_id, max_sentences=6):
    """Summarise a document extractively and store the summary.

    Extractive, so the summary cannot introduce a claim the document did not
    make. Stored on the document because the same summary is wanted by the
    document list, the search result card and the next three answers that
    cite it, and recomputing it each time would be work done repeatedly for an
    identical result.
    """
    document = knowledge.get_document(document_id)
    if document is None:
        return ToolResult(
            ok=False, error=f'No document #{document_id}.',
            text=(f'There is no document with id {document_id}. Use '
                  f'research.list_documents to see the available ids.'))

    summary = knowledge.summarise(document.content, max_sentences=int(max_sentences or 6))
    if not summary:
        return ToolResult(
            ok=False, error='Empty document.',
            text=f'Document #{document.pk} "{document.title}" has no readable text to summarise.')

    document.summary = summary
    document.save(update_fields=['summary', 'updated_at'])

    return ToolResult(
        ok=True,
        text=(f'Summary of #{document.pk} "{document.title}" '
              f'({document.get_doc_type_display()}, v{document.version}), saved onto '
              f'the document. Every sentence below appears verbatim in the source.\n\n'
              f'{summary}\n\nKey terms: '
              f'{", ".join(knowledge.keywords_of(document.content, limit=10))}'),
        subject_label='marketing.knowledgedocument', subject_id=document.pk,
        data={'document_id': document.pk, 'title': document.title,
              'summary': summary,
              'keywords': knowledge.keywords_of(document.content, limit=10)})


@tool(name='research.compare_documents',
      title='Compare two documents',
      description=('Compare two knowledge base documents: what they both cover, what '
                   'each covers alone, and where they state different figures for the '
                   'same thing.'),
      group='Analysis', agent_types=('research',), reads_only=True,
      icon='fa-code-compare', integration='knowledge_base',
      capability='Compare two documents',
      parameters={
          'type': 'object',
          'properties': {
              'document_id_a': {'type': 'integer', 'description': 'First document id.'},
              'document_id_b': {'type': 'integer', 'description': 'Second document id.'},
          },
          'required': ['document_id_a', 'document_id_b'],
      })
def compare_documents_tool(ctx, document_id_a, document_id_b):
    """Compare two documents' coverage and find where they disagree."""
    first = knowledge.get_document(document_id_a)
    second = knowledge.get_document(document_id_b)

    missing = [str(key) for key, value in ((document_id_a, first), (document_id_b, second))
               if value is None]
    if missing:
        return ToolResult(
            ok=False, error=f'Unknown document id(s): {", ".join(missing)}.',
            text=(f'No document with id {", ".join(missing)}. Use '
                  f'research.list_documents to see the available ids, then compare '
                  f'two of them.'))
    if first.pk == second.pk:
        return ToolResult(
            ok=False, error='Same document twice.',
            text=(f'Document #{first.pk} cannot be compared with itself. Supply two '
                  f'different document ids.'))

    result = knowledge.compare_documents(first, second)
    lines = [
        f'Comparing #{first.pk} "{first.title}" (v{first.version}, '
        f'{first.get_doc_type_display()}) with #{second.pk} "{second.title}" '
        f'(v{second.version}, {second.get_doc_type_display()}).',
        '',
        f'Covered by both ({len(result["shared"])} terms): '
        f'{", ".join(result["shared"][:25]) or "nothing in common"}',
        '',
        f'Only in "{first.title}": {", ".join(result["only_a"][:20]) or "nothing distinctive"}',
        f'Only in "{second.title}": {", ".join(result["only_b"][:20]) or "nothing distinctive"}',
    ]

    if result['differences']:
        lines.append('')
        lines.append(f'{len(result["differences"])} figure(s) stated differently for the '
                     f'same subject. Neither has been chosen:')
        for entry in result['differences']:
            lines.append(f'- {entry["topic"]}: {entry["a"]["document"]} says '
                         f'{entry["a"]["value"]}, {entry["b"]["document"]} says '
                         f'{entry["b"]["value"]}.')
        lines.append('This needs a decision from a person and an edit to one of the '
                     'two documents.')
    else:
        lines.append('')
        lines.append('No contradictory figures found between the two.')

    return ToolResult(ok=True, text='\n'.join(lines), data=result)


@tool(name='research.extract_key_information',
      title='Extract key information',
      description=('Pull the figures, dates, obligations, named entities and defined '
                   'terms out of a document or a block of text, optionally narrowed to '
                   'a focus term.'),
      group='Analysis', agent_types=('research',), reads_only=True,
      icon='fa-highlighter', integration='knowledge_base',
      capability='Extract key information',
      parameters={
          'type': 'object',
          'properties': {
              'document_id': {'type': 'integer',
                              'description': 'Document to read, or omit and pass text.'},
              'text': {'type': 'string', 'description': 'Text to read instead of a document.'},
              'focus': {'type': 'string',
                        'description': 'Optional: keep only sentences mentioning this.'},
          },
      })
def extract_key_information(ctx, document_id=None, text='', focus=''):
    """List what a document commits the company to, in five categories.

    Obligations are the category that earns this tool. A policy answer is
    usually a search away, but the question "what does this document actually
    bind us to" is answered by the sentences containing must, shall, never and
    within -- and those are cheap to find exactly and expensive to find by
    reading forty paragraphs.
    """
    document = knowledge.get_document(document_id) if document_id else None
    if document_id and document is None:
        return ToolResult(
            ok=False, error=f'No document #{document_id}.',
            text=(f'There is no document with id {document_id}. Pass a valid '
                  f'document_id, or pass the text directly in the text argument.'))

    body = document.content if document is not None else (text or '')
    if not body.strip():
        return ToolResult(
            ok=False, error='Nothing to read.',
            text=('Give me either a document_id from research.list_documents or the '
                  'text to read in the text argument.'))

    sentences = _sentences(body)
    if focus:
        needle = focus.lower()
        sentences = [line for line in sentences if needle in line.lower()] or sentences

    scope = ' '.join(sentences)

    figures = []
    for match in _FIGURE.finditer(scope):
        value = match.group(0).strip()
        if value not in figures:
            figures.append(value)

    dates = []
    for match in _DATE.finditer(scope):
        value = match.group(0).strip()
        if value not in dates:
            dates.append(value)

    obligations = [line for line in sentences if _OBLIGATION.search(line)]
    definitions = [line for line in sentences if _DEFINITION.search(line)]

    # Named entities, crudely: capitalised runs that are not simply the first
    # word of a sentence. Crude is the right level here -- the aim is to
    # surface the systems, teams and roles a document names, and a false
    # positive costs a reader one glance.
    entities = []
    for line in sentences:
        for match in _ENTITY.finditer(line):
            candidate = match.group(0).strip()
            if match.start() == 0 and ' ' not in candidate:
                continue
            if len(candidate) < 4 or candidate.lower() in knowledge.STOPWORDS:
                continue
            if candidate not in entities:
                entities.append(candidate)

    where = (f'#{document.pk} "{document.title}"' if document is not None
             else 'the supplied text')
    focus_note = f' focused on "{focus}"' if focus else ''

    def block(label, values, limit):
        if not values:
            return f'{label}: none found.'
        shown = values[:limit]
        more = f' (+{len(values) - len(shown)} more)' if len(values) > len(shown) else ''
        return f'{label} ({len(values)}){more}:\n' + '\n'.join(f'  - {v}' for v in shown)

    text_out = '\n\n'.join([
        f'Key information extracted from {where}{focus_note}.',
        block('Figures and amounts', figures, 25),
        block('Dates and deadlines', dates, 15),
        block('Obligations and requirements', obligations, 15),
        block('Defined terms', definitions, 8),
        block('Named entities', entities, 20),
    ])

    return ToolResult(
        ok=True, text=text_out,
        subject_label='marketing.knowledgedocument' if document else '',
        subject_id=document.pk if document else 0,
        data={'document_id': document.pk if document else None,
              'figures': figures, 'dates': dates,
              'obligations': obligations[:30], 'definitions': definitions[:15],
              'entities': entities[:40]})


@tool(name='research.find_sources',
      title='Find sources for a claim',
      description=('Check a specific claim against the knowledge base and say, for each '
                   'document found, whether it supports the claim, contradicts it, or '
                   'is silent on it. Use this before repeating a claim publicly.'),
      group='Analysis', agent_types=('research',), reads_only=True,
      icon='fa-clipboard-check', integration='knowledge_base',
      capability='Find sources for a claim',
      parameters={
          'type': 'object',
          'properties': {
              'claim': {'type': 'string',
                        'description': 'The claim to verify, as a single statement.'},
              'limit': {'type': 'integer', 'description': 'Documents to check, default 6.'},
          },
          'required': ['claim'],
      })
def find_sources(ctx, claim, limit=6):
    """Classify each retrieved document as supporting, contradicting or silent.

    WHY "SILENT" IS A VERDICT
    -------------------------
    A document that matched the words of a claim without addressing its
    substance is the most dangerous kind of result, because it looks like
    corroboration. Reporting it as silent rather than as support is the
    difference between "three documents back this" and the truth, which is
    often "one document backs this and two use the same vocabulary".
    """
    hits = knowledge.search(claim, limit=int(limit or 6))
    if not hits:
        return ToolResult(
            ok=True, data={'support': [], 'contradict': [], 'silent': []},
            text=(f'No document in the knowledge base mentions this claim. Searched '
                  f'for: {", ".join(knowledge.content_terms(claim)) or "no distinctive terms"}. '
                  f'The claim is unsourced, so it must not be repeated as a company '
                  f'fact.'))

    claim_terms = set(knowledge.content_terms(claim))
    claim_numbers = [(match.group('value'), match.group('unit').lower())
                     for match in _CLAIM_NUMBER.finditer(claim)]

    support, contradict, silent = [], [], []

    for hit in hits:
        passage = getattr(hit.chunk, 'text', '') or hit.snippet
        passage_terms = set(knowledge.content_terms(passage))
        overlap = (claim_terms & passage_terms) - _UNIT_VOCAB

        # A contradiction is a passage that states a different value for a
        # unit the claim also quantifies, while sharing enough vocabulary that
        # the two are plainly about the same thing.
        passage_numbers = [(match.group('value'), match.group('unit').lower())
                           for match in _CLAIM_NUMBER.finditer(passage)]

        disagrees = None
        agrees = False
        for value, unit in claim_numbers:
            for found_value, found_unit in passage_numbers:
                if found_unit != unit:
                    continue
                if found_value == value:
                    agrees = True
                    continue
                if len(overlap) < 2:
                    continue
                disagrees = (f'states {found_value} {found_unit} where the claim '
                             f'says {value} {unit}')
            if disagrees:
                break

        entry = {'document_id': hit.document_id, 'document': hit.title,
                 'version': hit.document.version, 'heading': hit.heading,
                 'quote': hit.snippet, 'relevance': hit.score,
                 'shared_terms': sorted(overlap)[:8]}

        # A quantified claim is only supported by a passage that states the
        # same quantity. Counting a passage that merely shares the vocabulary
        # is how "one document says this" becomes "three documents agree",
        # which is the specific way a citation list stops meaning anything.
        if disagrees:
            entry['detail'] = disagrees
            contradict.append(entry)
        elif claim_numbers and not agrees:
            entry['detail'] = 'shares the wording but states no matching figure'
            silent.append(entry)
        elif hit.score >= 0.45 and len(overlap) >= 2:
            support.append(entry)
        else:
            silent.append(entry)

    def render(label, rows):
        if not rows:
            return f'{label}: none.'
        lines = [f'{label} ({len(rows)}):']
        for row in rows:
            where = f", section '{row['heading']}'" if row['heading'] else ''
            detail = f" -- {row['detail']}" if row.get('detail') else ''
            lines.append(f'  - #{row["document_id"]} {row["document"]} '
                         f'(v{row["version"]}{where}), relevance '
                         f'{row["relevance"]:.2f}{detail}\n      "{row["quote"]}"')
        return '\n'.join(lines)

    verdict = ('CONTRADICTED by at least one document -- do not repeat the claim as '
               'stated' if contradict else
               'SUPPORTED' if support else
               'UNSOURCED -- the words appear but no document states the claim')

    text = '\n\n'.join([
        f'Claim: "{claim}"\nVerdict: {verdict}.',
        render('Supports the claim', support),
        render('Contradicts the claim', contradict),
        render('Mentions the terms but does not state the claim', silent),
    ])
    if contradict:
        text += ('\n\nBoth readings are reported. Choosing between them is a decision '
                 'for a person, and one of the documents needs correcting.')

    return ToolResult(ok=True, text=text,
                      data={'claim': claim, 'support': support,
                            'contradict': contradict, 'silent': silent,
                            'citations': knowledge.citations_for(hits)})


@tool(name='research.identify_conflicts',
      title='Identify conflicting information',
      description=('Search a topic and report where two documents state different '
                   'figures for the same thing, giving both sides with their versions '
                   'and dates. Never picks a side.'),
      group='Analysis', agent_types=('research',), reads_only=True,
      icon='fa-triangle-exclamation', integration='knowledge_base',
      capability='Identify conflicting information',
      parameters={
          'type': 'object',
          'properties': {
              'query': {'type': 'string', 'description': 'The topic to check for conflicts.'},
              'limit': {'type': 'integer', 'description': 'Passages to examine, default 10.'},
          },
          'required': ['query'],
      })
def identify_conflicts(ctx, query, limit=10):
    """Report disagreements between documents, without resolving them.

    The temptation is to pick the more authoritative document and answer
    confidently. That is wrong: if the troubleshooting guide says seven days
    and the policy says five, customers have already been told seven, and the
    finding is not "the answer is five" but "one of these documents needs
    fixing today". So both sides are reported, with whatever evidence of
    recency and authority exists, and the decision stays with a person.
    """
    hits = knowledge.search(query, limit=int(limit or 10))
    if not hits:
        return ToolResult(
            ok=True, data={'conflicts': []},
            text=(f'Nothing in the knowledge base matched "{query}", so there is '
                  f'nothing to compare. Searched for: '
                  f'{", ".join(knowledge.content_terms(query)) or "no distinctive terms"}.'))

    conflicts = knowledge.detect_conflicts(hits)
    documents = []
    for hit in hits:
        if hit.title not in documents:
            documents.append(hit.title)

    if not conflicts:
        return ToolResult(
            ok=True,
            text=(f'No conflicting figures found on "{query}" across '
                  f'{len(documents)} document(s): {", ".join(documents)}. '
                  f'That is consistency in the passages examined, not a guarantee '
                  f'about documents the search did not reach.'),
            data={'conflicts': [], 'documents_examined': documents})

    lines = [f'{len(conflicts)} conflict(s) found on "{query}" across '
             f'{len(documents)} document(s).', '']
    for index, entry in enumerate(conflicts, start=1):
        side_a, side_b = entry['a'], entry['b']
        lines.append(f'Conflict {index}: {entry["topic"]}')
        lines.append(f'  Side A -- #{side_a["document_id"]} {side_a["document"]} '
                     f'(v{side_a["version"]}, updated {side_a["updated"] or "unknown"}) '
                     f'states {side_a["value"]}.')
        lines.append(f'          "{side_a["quote"]}"')
        lines.append(f'  Side B -- #{side_b["document_id"]} {side_b["document"]} '
                     f'(v{side_b["version"]}, updated {side_b["updated"] or "unknown"}) '
                     f'states {side_b["value"]}.')
        lines.append(f'          "{side_b["quote"]}"')
        lines.append(f'  Which to believe: {_authority_note(side_a, side_b)}')
        lines.append('')

    lines.append('Neither side has been chosen. Report both to the reader, and raise '
                 'the correction with whoever owns the documents.')

    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'conflicts': conflicts, 'documents_examined': documents})


def _authority_note(side_a, side_b):
    """What evidence there is about which side to prefer, stated as evidence.

    Deliberately stops short of a recommendation. Version numbers and update
    dates are the only signals available here, and neither establishes which
    figure is correct -- only which was written most recently, which is a
    different thing and is worth saying as such.
    """
    reasons = []
    if side_a['updated'] and side_b['updated'] and side_a['updated'] != side_b['updated']:
        newer = side_a if side_a['updated'] > side_b['updated'] else side_b
        reasons.append(f'{newer["document"]} was updated more recently '
                       f'({newer["updated"]})')
    if side_a['version'] != side_b['version']:
        reasons.append(f'versions differ ({side_a["document"]} v{side_a["version"]} '
                       f'against {side_b["document"]} v{side_b["version"]})')
    if not reasons:
        return ('no evidence either way from version or date. A person who owns '
                'these documents must decide.')
    return (' and '.join(reasons) +
            '. That indicates recency, not correctness; a person must confirm which '
            'figure is right.')


# ===========================================================================
# 4. REPORTS
# ===========================================================================

@tool(name='research.generate_research_report',
      title='Generate a research report',
      description=('Produce a multi-source report on a topic with a findings section, '
                   'a numbered sources section and an explicit gaps section, saved with '
                   'citation rows.'),
      group='Reports', agent_types=('research',), reads_only=False,
      icon='fa-file-contract', integration='knowledge_base',
      capability='Generate a research report',
      parameters={
          'type': 'object',
          'properties': {
              'topic': {'type': 'string', 'description': 'The subject of the report.'},
              'question': {'type': 'string',
                           'description': 'Optional specific question the report answers.'},
              'limit': {'type': 'integer', 'description': 'Passages to draw on, default 10.'},
          },
          'required': ['topic'],
      })
def generate_research_report(ctx, topic, question='', limit=10):
    """A report with findings, sources and -- crucially -- gaps.

    THE GAPS SECTION IS THE POINT
    -----------------------------
    A report that lists only what was found reads as complete whether or not
    it is, and the reader has no way to tell the difference. Naming the terms
    no document mentioned turns an ordinary report into a statement about the
    knowledge base as well as the topic, and it is usually the gaps that
    change what somebody does next.
    """
    query = f'{topic} {question}'.strip()
    raw = knowledge.search(query, limit=int(limit or 10))
    coverage_map = knowledge.term_coverage(query)

    # A report is deliberately broader than an answer, so only the padding
    # filter applies here and not the subject filter: a passage that touches
    # the topic from an unexpected angle is worth a line in a report even
    # though it would be a poor basis for a direct answer.
    hits = [hit for hit in raw if hit.score >= RELEVANCE_FLOOR] or raw[:3]

    if not hits:
        tried = knowledge.content_terms(query)
        body = (f'Topic: {topic}\n\nFindings\nNone. The knowledge base holds no '
                f'document on this topic.\n\nSources\nNone.\n\nGaps\nSearched for: '
                f'{", ".join(tried) or "no distinctive terms"}. Every one of those '
                f'terms is a gap. Nothing in this report may be treated as a company '
                f'fact.')
        report = _save_report(
            title=f'Research report: {topic}'[:250], question=question or topic,
            kind='report', body=body,
            summary='No company document covers this topic.', hits=[],
            confidence=0.0, coverage_note=_coverage_note(query, []), ctx=ctx)
        return ToolResult(
            ok=True, text=f'{body}\n\nSaved as research report #{report.pk}.',
            subject_label='marketing.researchreport', subject_id=report.pk,
            data={'report_id': report.pk, 'findings': 0, 'terms_tried': tried})

    citations = knowledge.citations_for(hits)
    order = {entry['document_id']: index for index, entry in enumerate(citations, start=1)}
    conflicts = knowledge.detect_conflicts(hits)
    confidence = _confidence(query, hits, coverage_map)
    gaps = _unmatched_terms(query, hits)
    never_written = [term for term in (coverage_map.get('absent') or [])
                     if term in (coverage_map.get('substantive') or [])]

    findings = []
    for hit in hits:
        marker = order.get(hit.document_id, 0)
        passage = knowledge.summarise(getattr(hit.chunk, 'text', '') or hit.snippet,
                                      max_sentences=2)
        findings.append(f'- {passage} [{marker}]{_section_note(hit)}')

    source_lines = []
    for index, entry in enumerate(citations, start=1):
        document = knowledge.get_document(entry['document_id'])
        detail = (f'{document.get_doc_type_display()}, v{document.version}, '
                  f'{document.department or "no department"}, '
                  f'{document.word_count} words' if document else 'document removed')
        suffix = f'\n    {entry["url"]}' if entry['url'] else ''
        source_lines.append(f'[{index}] {entry["label"]} -- {detail} '
                            f'(document #{entry["document_id"]}, relevance '
                            f'{entry["relevance"]}){suffix}')

    parts = [
        f'Research report: {topic}',
        f'Question: {question}' if question else '',
        '',
        f'Findings ({len(findings)} passage(s) from {len(citations)} document(s))',
        '\n'.join(findings),
        '',
        'Sources',
        '\n'.join(source_lines),
    ]

    if conflicts:
        parts.extend([
            '',
            f'Conflicts ({len(conflicts)})',
            '\n'.join(f'- {entry["note"]}' for entry in conflicts),
            'Neither side has been chosen.',
        ])

    gap_lines = []
    if never_written:
        gap_lines.append(
            f'Nowhere in the knowledge base: {", ".join(never_written)}. No document '
            f'contains these words at all, so nothing in this report speaks to them.')
    if gaps:
        gap_lines.append(
            f'Not matched by any passage returned: {", ".join(gaps)}.')
    if not gap_lines:
        gap_lines.append(
            'Every distinctive term of the topic was matched by at least one '
            'document. That does not mean the coverage is complete, only that '
            'nothing searched for was entirely absent.')
    gap_lines.append('Gaps are gaps in what the company has written down. They are '
                     'not conclusions and must not be filled in from general '
                     'knowledge.')

    parts.extend([
        '',
        'Gaps',
        '\n'.join(gap_lines),
        '',
        f'Confidence: {confidence}, computed from term coverage, retrieval scores '
        f'and the number of corroborating documents.',
    ])

    body = '\n'.join(parts).strip()

    report = _save_report(
        title=f'Research report: {topic}'[:250], question=question or topic,
        kind='report', body=body,
        summary=knowledge.summarise(body, max_sentences=4), hits=hits,
        confidence=confidence, conflicts=conflicts,
        coverage_note=_coverage_note(query, hits), ctx=ctx)

    return ToolResult(
        ok=True,
        text=(f'{body}\n\nSaved as research report #{report.pk} with '
              f'{report.citation_count} citation(s).'),
        subject_label='marketing.researchreport', subject_id=report.pk,
        data={'report_id': report.pk, 'confidence': confidence,
              'citations': citations, 'conflicts': conflicts, 'gaps': gaps})


@tool(name='research.generate_executive_summary',
      title='Generate an executive summary',
      description=('Condense a saved research report, a document, or a block of text '
                   'into a short executive summary made only of sentences that appear '
                   'in the source.'),
      group='Reports', agent_types=('research',), reads_only=False,
      icon='fa-file-invoice', integration='knowledge_base',
      capability='Generate an executive summary',
      parameters={
          'type': 'object',
          'properties': {
              'report_id': {'type': 'integer', 'description': 'A saved research report id.'},
              'text': {'type': 'string', 'description': 'Text to summarise instead.'},
              'document_id': {'type': 'integer', 'description': 'A document id to summarise.'},
          },
      })
def generate_executive_summary(ctx, report_id=None, text='', document_id=None):
    """Condense a report, a document or a passage of text, extractively.

    Three input routes because the request arrives in three shapes -- summarise
    that report, summarise that policy, summarise this -- and forcing a caller
    to save text as a document first in order to summarise it would be
    ceremony with no benefit.
    """
    source_report = None
    origin = ''
    body = ''
    hits = []

    if report_id:
        source_report = ResearchReport.objects.filter(pk=report_id).first()
        if source_report is None:
            return ToolResult(
                ok=False, error=f'No research report #{report_id}.',
                text=(f'There is no research report with id {report_id}. Reports are '
                      f'created by research.answer_company_question and '
                      f'research.generate_research_report.'))
        body = _report_prose(source_report)
        origin = f'research report #{source_report.pk} "{source_report.title}"'
    elif document_id:
        document = knowledge.get_document(document_id)
        if document is None:
            return ToolResult(
                ok=False, error=f'No document #{document_id}.',
                text=(f'There is no document with id {document_id}. Use '
                      f'research.list_documents to see the available ids.'))
        body = document.content
        origin = f'document #{document.pk} "{document.title}"'
        hits = knowledge.search(document.title, limit=3)
    else:
        body = text or ''
        origin = 'the supplied text'

    if not body.strip():
        return ToolResult(
            ok=False, error='Nothing to summarise.',
            text=('Pass a report_id, a document_id, or the text to summarise. All '
                  'three were empty.'))

    summary = knowledge.summarise(body, max_sentences=5)
    terms = knowledge.keywords_of(body, limit=10)

    lines = [f'Executive summary of {origin}.', '', summary, '',
             f'Key terms: {", ".join(terms)}.']
    if source_report is not None:
        lines.append(f'Underlying confidence: {source_report.confidence}, '
                     f'{source_report.citation_count} citation(s).')
        if source_report.conflicts:
            lines.append(f'Note: the source report records '
                         f'{len(source_report.conflicts)} unresolved conflict(s) '
                         f'between documents.')
    lines.append('Every sentence above appears verbatim in the source; nothing has '
                 'been paraphrased or added.')

    output = '\n'.join(lines)

    saved = _save_report(
        title=f'Executive summary: {origin}'[:250],
        question=getattr(source_report, 'question', '') or '',
        kind='executive_summary', body=output, summary=summary, hits=hits,
        confidence=source_report.confidence if source_report else 0.0,
        conflicts=list(getattr(source_report, 'conflicts', []) or []),
        coverage_note=f'Condensed from {origin}.', ctx=ctx)

    return ToolResult(
        ok=True, text=f'{output}\n\nSaved as research report #{saved.pk}.',
        subject_label='marketing.researchreport', subject_id=saved.pk,
        data={'report_id': saved.pk, 'source_report_id': report_id,
              'summary': summary, 'keywords': terms})


# ===========================================================================
# 5. SOURCES -- adding documents and pulling them in
# ===========================================================================

@tool(name='research.index_document',
      title='Add a document to the knowledge base',
      description=('Add or update a company document by hand: title, content, type, '
                   'tags, department and an optional URL. Splits it into retrievable '
                   'passages immediately so it can be searched and cited.'),
      group='Sources', agent_types=('research',), reads_only=False,
      icon='fa-file-circle-plus', integration='knowledge_base',
      capability='Add a document to the knowledge base',
      parameters={
          'type': 'object',
          'properties': {
              'title': {'type': 'string', 'description': 'The document title.'},
              'content': {'type': 'string', 'description': 'The full text.'},
              'doc_type': {'type': 'string',
                           'description': ('policy, handbook, faq, product, technical, '
                                           'brand, troubleshooting, process, report or '
                                           'other. Default other.')},
              'tags': {'type': 'array', 'items': {'type': 'string'},
                       'description': 'Optional tags.'},
              'department': {'type': 'string', 'description': 'Optional owning department.'},
              'url': {'type': 'string', 'description': 'Optional link to the original.'},
          },
          'required': ['title', 'content'],
      })
def index_document_tool(ctx, title, content, doc_type='other', tags=None,
                        department='', url=''):
    """Add or update one document and build its passages.

    Identity is the exact title, so calling this twice with the same title
    updates rather than duplicates. That is the behaviour a person expects
    from "add the leave policy" said twice, and a knowledge base with two
    slightly different copies of one policy is worse than one with none.
    """
    valid = {key for key, _label in KnowledgeDocument.DOC_TYPE_CHOICES}
    chosen = (doc_type or 'other').lower()
    if chosen not in valid:
        return ToolResult(
            ok=False, error=f'Unknown document type "{doc_type}".',
            text=(f'"{doc_type}" is not a document type. Choose one of: '
                  f'{", ".join(sorted(valid))}.'))
    if not (content or '').strip():
        return ToolResult(
            ok=False, error='Empty content.',
            text=(f'The document "{title}" has no content, so there would be nothing '
                  f'to retrieve or cite. Supply the text.'))

    existing = KnowledgeDocument.objects.filter(title=(title or '').strip()[:250]).first()
    document = knowledge.index_document(
        title=title, content=content, doc_type=chosen,
        tags=list(tags or []), department=department or '', url=url or '',
        created_by=getattr(ctx, 'user', None),
        created_by_agent=getattr(ctx, 'agent', None))

    verb = 'Updated' if existing is not None else 'Added'
    return ToolResult(
        ok=True,
        text=(f'{verb} document #{document.pk} "{document.title}" '
              f'({document.get_doc_type_display()}, {document.word_count} words) and '
              f'split it into {document.chunk_count} retrievable passage(s). It is '
              f'searchable and citable now. Key terms: '
              f'{", ".join(knowledge.keywords_of(document.content, limit=8))}.'),
        subject_label='marketing.knowledgedocument', subject_id=document.pk,
        data={'document_id': document.pk, 'title': document.title,
              'doc_type': document.doc_type, 'word_count': document.word_count,
              'chunks': document.chunk_count, 'updated': existing is not None})


@tool(name='research.list_sources',
      title='List knowledge sources',
      description=('List the configured knowledge sources with their kinds, '
                   'integrations, document counts and last synchronisation.'),
      group='Sources', agent_types=('research',), reads_only=True,
      icon='fa-folder-tree', integration='knowledge_base',
      capability='List knowledge sources',
      parameters={'type': 'object', 'properties': {}})
def list_sources(ctx):
    """Every configured source, with its id for synchronising."""
    rows = list(KnowledgeSource.objects.all())
    if not rows:
        return ToolResult(
            ok=True, data={'sources': []},
            text=('No knowledge sources are configured. research.create_source adds '
                  'one -- for example a Google Drive folder, a Notion database, a '
                  'Confluence space or a GitHub wiki.'))

    lines = []
    for row in rows:
        synced = (row.last_synced_at.strftime('%d %b %Y at %H:%M')
                  if row.last_synced_at else 'never')
        state = 'enabled' if row.is_enabled else 'disabled'
        reference = f', reference "{row.root_reference}"' if row.root_reference else ''
        lines.append(
            f'#{row.pk} {row.name} -- {row.get_kind_display()}, {state}, '
            f'integration "{row.integration_key or "none"}"{reference}, '
            f'{row.document_count} document(s), last synced {synced}.'
            + (f' Last message: {row.last_sync_message}' if row.last_sync_message else ''))

    return ToolResult(
        ok=True, text=f'{len(rows)} knowledge source(s):\n' + '\n'.join(lines),
        data={'sources': [{'id': row.pk, 'name': row.name, 'kind': row.kind,
                           'integration_key': row.integration_key,
                           'root_reference': row.root_reference,
                           'document_count': row.document_count,
                           'is_enabled': row.is_enabled} for row in rows]})


@tool(name='research.create_source',
      title='Create a knowledge source',
      description=('Register a place documents come from: a Drive folder, a Notion '
                   'database, a Confluence space, a GitHub wiki, or manual entry. '
                   'Creating it does not read anything; research.sync_source does that.'),
      group='Sources', agent_types=('research',), reads_only=False,
      icon='fa-folder-plus', integration='knowledge_base',
      capability='Create a knowledge source',
      parameters={
          'type': 'object',
          'properties': {
              'name': {'type': 'string', 'description': 'A name a person would recognise.'},
              'kind': {'type': 'string',
                       'description': ('drive, notion, confluence, github_wiki, manual, '
                                       'upload or web.')},
              'integration_key': {'type': 'string',
                                  'description': ("Provider key that reads it, e.g. "
                                                  "'google_drive'. Inferred from kind "
                                                  "when omitted.")},
              'root_reference': {'type': 'string',
                                 'description': ('Drive folder id, Notion database id, '
                                                 'Confluence space key or repository name.')},
          },
          'required': ['name', 'kind'],
      })
def create_source(ctx, name, kind, integration_key='', root_reference=''):
    """Register a source. Reading it is a separate, deliberate act.

    Creating and synchronising are separate tools because they fail for
    different reasons and a caller needs to tell them apart: a source that was
    registered with the wrong folder id is a configuration mistake, whereas a
    synchronisation that returned nothing may simply mean the folder is empty.
    """
    valid = {key for key, _label in KnowledgeSource.KIND_CHOICES}
    chosen = (kind or '').lower().strip()
    if chosen not in valid:
        return ToolResult(
            ok=False, error=f'Unknown source kind "{kind}".',
            text=f'"{kind}" is not a source kind. Choose one of: {", ".join(sorted(valid))}.')

    inferred = {'drive': 'google_drive', 'notion': 'notion', 'confluence': 'confluence',
                'github_wiki': 'github'}
    provider = (integration_key or inferred.get(chosen, '')).strip()

    existing = KnowledgeSource.objects.filter(name=name.strip()[:140]).first()
    if existing is not None:
        return ToolResult(
            ok=True,
            text=(f'A source named "{existing.name}" already exists as #{existing.pk} '
                  f'({existing.get_kind_display()}). Synchronise it with '
                  f'research.sync_source(source_id={existing.pk}) rather than creating '
                  f'a second one.'),
            subject_label='marketing.knowledgesource', subject_id=existing.pk,
            data={'source_id': existing.pk, 'created': False})

    source = KnowledgeSource.objects.create(
        name=name.strip()[:140], kind=chosen, integration_key=provider[:40],
        root_reference=(root_reference or '')[:300], sync_mode='manual')

    advice = ('Nothing has been read yet. Call '
              f'research.sync_source(source_id={source.pk}) to pull the documents in '
              f'and index them.')
    if chosen in inferred and not root_reference:
        advice += (' No root reference was given, so the synchronisation will use the '
                   'integration default -- set root_reference to target a specific '
                   'folder, database, space or repository.')

    return ToolResult(
        ok=True,
        text=(f'Created knowledge source #{source.pk} "{source.name}" '
              f'({source.get_kind_display()}, integration '
              f'"{source.integration_key or "none"}"). {advice}'),
        subject_label='marketing.knowledgesource', subject_id=source.pk,
        data={'source_id': source.pk, 'name': source.name, 'kind': source.kind,
              'integration_key': source.integration_key, 'created': True})


# How each source kind is read: the integration, the operation that lists what
# is there, the argument that carries the root reference, and the operation
# that reads one item.
_SYNC_PLAN = {
    'drive': {'provider': 'google_drive', 'list': 'list_files',
              'root_arg': 'folder_id', 'read': 'read_file', 'id_arg': 'file_id'},
    # Notion's search takes no scope argument, so the root reference becomes
    # the search term instead. get_page_content rather than get_page: the
    # former returns the prose, the latter returns the page's metadata.
    'notion': {'provider': 'notion', 'list': 'search',
               'root_arg': '', 'read': 'get_page_content', 'id_arg': 'page_id'},
    'confluence': {'provider': 'confluence', 'list': 'search',
                   'root_arg': 'space', 'read': 'get_page', 'id_arg': 'page_id'},
    'github_wiki': {'provider': 'github', 'list': 'list_wiki',
                    'root_arg': 'repo', 'read': 'read_file', 'id_arg': 'path'},
}

# Keys a connector might nest its payload under. Confluence returns
# {'page': {...}}, Drive returns the fields at the top level, and a live
# service may do either -- so a reader that only understood one shape would
# work against half the integrations and silently index nothing from the rest.
_WRAPPER_KEYS = ('page', 'file', 'document', 'item', 'result', 'node', 'data',
                 'content')

_LIST_KEYS = ('files', 'items', 'results', 'pages', 'documents', 'entries',
              'records', 'children', 'rows')
_CONTENT_KEYS = ('content', 'text', 'body', 'markdown', 'plain_text', 'value')
_ID_KEYS = ('id', 'file_id', 'page_id', 'path', 'key', 'slug', 'external_id')
_TITLE_KEYS = ('title', 'name', 'filename', 'path', 'subject')
_URL_KEYS = ('url', 'link', 'web_url', 'webViewLink', 'html_url')


def _first_value(mapping, keys, default=''):
    """The first non-empty value among ``keys``, looking one level down too.

    One level of nesting is unwrapped because connectors disagree about
    shape: Drive puts ``content`` at the top of its payload, Confluence puts
    the whole page under ``page``. Insisting on one convention would mean a
    synchronisation that reported success and indexed nothing.
    """
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        value = mapping.get(key)
        if value not in (None, '', [], {}) and not isinstance(value, (dict, list)):
            return value
    for wrapper in _WRAPPER_KEYS:
        nested = mapping.get(wrapper)
        if isinstance(nested, dict):
            for key in keys:
                value = nested.get(key)
                if value not in (None, '', [], {}) and not isinstance(value, (dict, list)):
                    return value
    return default


def _listing(data):
    """Find the list of items in a CallResult payload, whatever it is called.

    Connectors are written by different hands and a simulation is free to
    shape its own payload, so this looks for the list rather than insisting on
    one key. Being permissive here is what stops a synchronisation failing
    because a connector called its array "pages" instead of "items".
    """
    if isinstance(data, list):
        return [row for row in data if isinstance(row, (dict, str))]
    if not isinstance(data, dict):
        return []
    for key in _LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list) and value:
            return [row for row in value if isinstance(row, (dict, str))]
    for value in data.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


@tool(name='research.sync_source',
      title='Synchronise a knowledge source',
      description=('Pull documents in from a configured source through its integration '
                   'and index them for search. Reports how many were added, updated and '
                   'skipped, and says plainly when the content was simulated.'),
      group='Sources', agent_types=('research',), reads_only=False,
      icon='fa-rotate', integration='knowledge_base',
      capability='Synchronise a knowledge source',
      parameters={
          'type': 'object',
          'properties': {
              'source_id': {'type': 'integer',
                            'description': 'Source id from research.list_sources.'},
              'limit': {'type': 'integer', 'description': 'Maximum documents, default 25.'},
          },
          'required': ['source_id'],
      })
def sync_source(ctx, source_id, limit=25):
    """Read a source through its integration and index what came back.

    DEMO MODE IS REPORTED, NOT HIDDEN
    ---------------------------------
    Where an integration has no credential the connector returns a faithful
    simulation with ``demo`` set. Those documents are still indexed -- a
    demonstration with an empty knowledge base shows nothing -- but the tool
    result says the content was simulated, and so does every layer above it. A
    simulated policy that a colleague later cites as fact is exactly the
    failure this platform exists to prevent, so the label travels with the
    data.
    """
    source = KnowledgeSource.objects.filter(pk=_as_int(source_id)).first()
    if source is None:
        return ToolResult(
            ok=False, error=f'No knowledge source #{source_id}.',
            text=(f'There is no knowledge source with id {source_id}. Use '
                  f'research.list_sources to see the configured sources, or '
                  f'research.create_source to add one.'))
    if not source.is_enabled:
        return ToolResult(
            ok=False, error='Source disabled.',
            text=(f'Source #{source.pk} "{source.name}" is disabled, so it will not be '
                  f'read. Enable it on the Knowledge page first.'))

    plan = _SYNC_PLAN.get(source.kind)
    if plan is None:
        return ToolResult(
            ok=False, error=f'Source kind "{source.kind}" is not synchronisable.',
            text=(f'Source #{source.pk} "{source.name}" is of kind '
                  f'{source.get_kind_display()}, which has no integration to read from. '
                  f'Documents for it are added with research.index_document.'))

    provider = source.integration_key or plan['provider']
    wanted = max(1, min(int(limit or 25), 100))

    # A listing operation is either a browse (Drive, a wiki) that takes a
    # container id, or a search that takes a term. The root reference means
    # the container in the first case and the term in the second, so it is
    # passed differently rather than passed blindly and rejected.
    list_kwargs = {}
    root_arg = plan.get('root_arg') or ''
    if plan['list'] == 'search':
        if root_arg and source.root_reference:
            list_kwargs[root_arg] = source.root_reference
            list_kwargs['query'] = source.name
        else:
            list_kwargs['query'] = source.root_reference or source.name
    elif root_arg and source.root_reference:
        list_kwargs[root_arg] = source.root_reference

    listing = integrations.call(provider, plan['list'], **list_kwargs)
    if not listing.ok:
        source.last_sync_message = listing.error[:400]
        source.save(update_fields=['last_sync_message'])
        return ToolResult(
            ok=False, error=listing.error,
            text=(f'Could not list documents in "{source.name}" through {provider}: '
                  f'{listing.error} Nothing was indexed. Check the integration on the '
                  f'Integrations page, or set the source mode to demo to work with a '
                  f'simulation.'),
            data={'source_id': source.pk})

    items = _listing(listing.data)[:wanted]
    simulated = bool(listing.demo)
    added, updated, skipped = [], [], []

    for item in items:
        if isinstance(item, str):
            item = {'id': item, 'title': item}

        external_id = str(_first_value(item, _ID_KEYS, ''))[:200]
        title = str(_first_value(item, _TITLE_KEYS, external_id or 'Untitled'))[:250]
        url = str(_first_value(item, _URL_KEYS, ''))[:500]
        content = str(_first_value(item, _CONTENT_KEYS, ''))

        if not content and external_id:
            read_kwargs = {plan['id_arg']: external_id}
            if plan['read'] == 'read_file' and source.kind == 'github_wiki':
                read_kwargs['repo'] = source.root_reference
            reading = integrations.call(provider, plan['read'], **read_kwargs)
            simulated = simulated or bool(reading.demo)
            if reading.ok:
                content = str(_first_value(reading.data, _CONTENT_KEYS,
                                           reading.summary or ''))
            else:
                skipped.append(f'{title} ({reading.error})')
                continue

        if not (content or '').strip():
            skipped.append(f'{title} (no readable content)')
            continue

        existing = None
        if external_id:
            existing = KnowledgeDocument.objects.filter(
                source=source, external_id=external_id).first()
        if existing is None:
            existing = KnowledgeDocument.objects.filter(title=title).first()

        document = knowledge.index_document(
            title=title, content=content, doc_type=_guess_type(title, content),
            source=source, external_id=external_id, url=url,
            created_by=getattr(ctx, 'user', None),
            created_by_agent=getattr(ctx, 'agent', None))

        (updated if existing is not None else added).append(
            f'#{document.pk} {document.title} ({document.chunk_count} passages)')

    message = (f'{len(added)} added, {len(updated)} updated, {len(skipped)} skipped'
               + (' -- content was simulated' if simulated else ''))
    source.last_synced_at = timezone.now()
    source.last_sync_message = message[:400]
    source.document_count = KnowledgeDocument.objects.filter(source=source).count()
    source.save(update_fields=['last_synced_at', 'last_sync_message', 'document_count'])

    lines = [f'Synchronised source #{source.pk} "{source.name}" '
             f'({source.get_kind_display()}) through {provider}: {message}.']
    if simulated:
        lines.append('IMPORTANT: the integration is not configured with a live '
                     'credential, so the content above is a simulation. It has been '
                     'indexed so the platform is usable, but it must not be quoted to '
                     'a customer or treated as a company fact.')
    if added:
        lines.append('Added:\n' + '\n'.join(f'  - {row}' for row in added))
    if updated:
        lines.append('Updated:\n' + '\n'.join(f'  - {row}' for row in updated))
    if skipped:
        lines.append('Skipped:\n' + '\n'.join(f'  - {row}' for row in skipped))
    if not items:
        lines.append(f'{provider} returned no items for this source. Check the root '
                     f'reference ("{source.root_reference or "not set"}").')
    lines.append(f'The source now holds {source.document_count} document(s).')

    return ToolResult(
        ok=True, demo=simulated, text='\n\n'.join(lines),
        subject_label='marketing.knowledgesource', subject_id=source.pk,
        data={'source_id': source.pk, 'added': len(added), 'updated': len(updated),
              'skipped': len(skipped), 'simulated': simulated,
              'documents_added': added, 'documents_updated': updated,
              'documents_skipped': skipped})


def _safe_filename(value):
    """A file name a person would accept in a shared folder.

    Report titles carry the question they answered, quotation marks and all,
    and a Drive folder full of files called ``Answer: "how long..."`` is a
    folder nobody browses.
    """
    flat = ' '.join(str(value or '').split())
    for bad in ('"', "'", '/', '\\', ':', '*', '?', '<', '>', '|'):
        flat = flat.replace(bad, '')
    return (flat.strip(' .-') or 'Research report')[:150]


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _guess_type(title, content):
    """A document type from the title, falling back to the content.

    A guess, and labelled as one. Classifying an incoming document as
    'other' would work and would make the narrowed searches useless for
    everything ever synchronised, which is a worse outcome than an
    occasional misfiling a person can correct.
    """
    haystack = f'{title} {content[:600]}'.lower()
    for needle, kind in (
            ('troubleshoot', 'troubleshooting'), ('faq', 'faq'),
            ('frequently asked', 'faq'), ('handbook', 'handbook'),
            ('policy', 'policy'), ('policies', 'policy'),
            ('brand', 'brand'), ('tone of voice', 'brand'),
            ('runbook', 'technical'), ('architecture', 'technical'),
            ('api', 'technical'), ('engineering', 'technical'),
            ('process', 'process'), ('procedure', 'process'),
            ('onboarding', 'process'), ('product', 'product'),
            ('release notes', 'product'), ('report', 'report')):
        if needle in haystack:
            return kind
    return 'other'


@tool(name='research.search_repository_docs',
      title='Search documentation in a code repository',
      description=('Search a GitHub repository for documentation and code matching a '
                   'query, for questions the knowledge base does not answer because the '
                   'answer lives in the codebase.'),
      group='Sources', agent_types=('research',), reads_only=True,
      icon='fa-code', integration='github',
      capability='Search repository documentation',
      parameters={
          'type': 'object',
          'properties': {
              'repo': {'type': 'string', 'description': "Repository, e.g. 'acme/platform'."},
              'query': {'type': 'string', 'description': 'What to look for.'},
          },
          'required': ['repo', 'query'],
      })
def search_repository_docs(ctx, repo, query):
    """Search a repository through the GitHub integration.

    Reading only. A repository is a source of truth about how something works
    and a poor source of truth about what the company has decided, so results
    from here are reported as code findings rather than folded into the
    knowledge base as policy.
    """
    result = integrations.call('github', 'search_code', repo=repo, query=query)
    if not result.ok:
        return ToolResult(
            ok=False, error=result.error,
            text=(f'Could not search {repo} through GitHub: {result.error} Check the '
                  f'GitHub integration on the Integrations page.'))

    items = _listing(result.data)
    if not items:
        note = ' (simulated)' if result.demo else ''
        return ToolResult(
            ok=True, demo=result.demo,
            text=(f'No matches for "{query}" in {repo}{note}. '
                  f'{result.summary or ""}').strip(),
            data={'repo': repo, 'query': query, 'matches': []})

    lines = []
    for item in items[:20]:
        if isinstance(item, str):
            lines.append(f'  - {item}')
            continue
        path = _first_value(item, ('path', 'name', 'file', 'title'), 'unknown path')
        url = _first_value(item, _URL_KEYS, '')
        excerpt = str(_first_value(item, ('snippet', 'excerpt', 'text', 'content'), ''))[:200]
        lines.append(f'  - {path}{f" -- {url}" if url else ""}'
                     + (f'\n      "{excerpt}"' if excerpt else ''))

    prefix = 'Simulated GitHub search' if result.demo else 'GitHub search'
    return ToolResult(
        ok=True, demo=result.demo,
        text=(f'{prefix}: {len(items)} match(es) for "{query}" in {repo}.\n'
              + '\n'.join(lines)
              + ('\n\nThis content was simulated because the GitHub integration has no '
                 'credential. Do not cite it as fact.' if result.demo else '')),
        data={'repo': repo, 'query': query, 'matches': items[:20],
              'simulated': result.demo})


# ===========================================================================
# 6. AGENT SUPPORT -- research answering a named colleague
# ===========================================================================

@tool(name='research.provide_research_to_agent',
      title='Provide research to a colleague',
      description=('Answer a question on behalf of a named colleague employee, with '
                   'citations, and record the exchange as a delegation so the colleague '
                   'can cite the source rather than repeat an unattributed claim.'),
      group='Agent Support', agent_types=('research',), reads_only=False,
      icon='fa-share-nodes', integration='knowledge_base',
      capability='Provide research to a colleague',
      parameters={
          'type': 'object',
          'properties': {
              'agent_type': {'type': 'string',
                             'description': ('Which colleague: hr, engineering_manager, '
                                             'developer, marketing or support.')},
              'question': {'type': 'string', 'description': 'The question to answer for them.'},
          },
          'required': ['agent_type', 'question'],
      })
def provide_research_to_agent(ctx, agent_type, question):
    """Answer a colleague's question and record the exchange.

    THE OTHER DIRECTION
    -------------------
    ``knowledge.ask_research`` is a colleague pulling an answer. This is
    research pushing one: the Support employee is about to draft a refund
    reply, and the refund figures are handed over with their sources before
    the draft is written rather than after somebody notices it was invented.
    The delegation row is written either way, so the collaboration is visible
    from both ends.
    """
    colleague = AIAgent.objects.filter(agent_type=(agent_type or '').strip()).first()
    if colleague is None:
        available = ', '.join(sorted(
            row.agent_type for row in AIAgent.objects.all())) or 'none provisioned'
        return ToolResult(
            ok=False, error=f'No employee of type "{agent_type}".',
            text=(f'There is no employee with agent_type "{agent_type}". The '
                  f'provisioned types are: {available}.'))

    researcher = getattr(ctx, 'agent', None) or _research_agent()
    hits, coverage_map = _focus(question, knowledge.search(question, limit=6))
    confidence = _confidence(question, hits, coverage_map)

    delegation = None
    if (researcher is not None and getattr(researcher, 'pk', None)
            and researcher.pk != colleague.pk):
        delegation = AgentDelegation.objects.create(
            from_agent=researcher, to_agent=colleague, task=getattr(ctx, 'task', None),
            request=question, status='pending')

    if not hits or confidence < ANSWER_CONFIDENCE_FLOOR:
        answer = _not_covered(question, coverage_map, confidence if hits else None)
        if delegation is not None:
            delegation.response = answer
            delegation.status = 'answered'
            delegation.answered_at = timezone.now()
            delegation.save(update_fields=['response', 'status', 'answered_at'])
        return ToolResult(
            ok=True,
            text=(f'Told {colleague.name} that the knowledge base does not cover '
                  f'"{question}".\n\n{answer}'),
            data={'agent': colleague.agent_type, 'answered': False,
                  'confidence': confidence,
                  'absent_terms': coverage_map.get('absent', []),
                  'delegation_id': delegation.pk if delegation else None})

    body, citations = _assemble_answer(question, hits)
    warning = _gap_warning(coverage_map)
    if warning:
        body = f'{warning}\n\n{body}'
    conflicts = knowledge.detect_conflicts(hits)
    if conflicts:
        body += ('\n\nSources disagree; both are reported:\n' +
                 '\n'.join(f'- {entry["note"]}' for entry in conflicts))

    report = _save_report(
        title=f'For {colleague.name}: {question}'[:250], question=question,
        kind='answer', body=body, summary=knowledge.summarise(body, max_sentences=3),
        hits=hits, confidence=confidence, conflicts=conflicts,
        coverage_note=_coverage_note(question, hits), ctx=ctx,
        requested_by_agent=colleague, delegation=delegation)

    if delegation is not None:
        delegation.response = body
        delegation.status = 'answered'
        delegation.answered_at = timezone.now()
        delegation.sources = citations
        delegation.save(update_fields=['response', 'status', 'answered_at', 'sources'])

    return ToolResult(
        ok=True,
        text=(f'Provided {colleague.name} ({colleague.role}) with a sourced answer, '
              f'confidence {confidence}, {len(citations)} citation(s).\n\n{body}\n\n'
              f'Recorded as research report #{report.pk}'
              + (f' and delegation #{delegation.pk}' if delegation else '') +
              f'. {colleague.name} must cite these source labels rather than '
              f'restating the facts unattributed.'),
        subject_label='marketing.researchreport', subject_id=report.pk,
        data={'agent': colleague.agent_type, 'answered': True,
              'report_id': report.pk, 'confidence': confidence,
              'citations': citations, 'conflicts': conflicts,
              'delegation_id': delegation.pk if delegation else None})


# ===========================================================================
# 7. ACTIONS THAT LEAVE THE PLATFORM -- proposed, never performed
# ===========================================================================

@tool(name='research.upload_report_to_drive',
      title='Upload a report to Google Drive',
      description=('Prepare a research report for upload to Google Drive as a document '
                   'in a shared folder.'),
      group='Reports', agent_types=('research',), integration='google_drive',
      requires_approval=True, risk='medium', icon='fa-cloud-arrow-up',
      capability='Upload a report to Drive',
      parameters={
          'type': 'object',
          'properties': {
              'report_id': {'type': 'integer',
                            'description': 'A saved research report to upload.'},
              'name': {'type': 'string', 'description': 'File name. Defaults to the title.'},
              'content': {'type': 'string',
                          'description': 'Content to upload instead of a report.'},
              'folder_id': {'type': 'string', 'description': 'Destination folder id.'},
          },
      })
def upload_report_to_drive(ctx, report_id=None, name='', content='', folder_id=''):
    """Prepare a Drive upload for approval. Nothing is written to Drive here."""
    report = None
    if report_id:
        report = ResearchReport.objects.filter(pk=_as_int(report_id)).first()
        if report is None:
            return ToolResult(
                ok=False, error=f'No research report #{report_id}.',
                text=(f'There is no research report with id {report_id}. Create one '
                      f'with research.generate_research_report, or pass the content '
                      f'directly.'))

    body = content or (report.body if report else '')
    if not body.strip():
        return ToolResult(
            ok=False, error='Nothing to upload.',
            text=('Pass a report_id or the content to upload. Both were empty, and '
                  'an empty file in a shared Drive folder is worse than no file.'))

    filename = _safe_filename(name or (report.title if report else 'Research report'))
    if not filename.lower().endswith(('.md', '.txt', '.doc', '.docx')):
        filename = f'{filename}.md'

    citation_note = (f' It carries {report.citation_count} citation(s) and a '
                     f'confidence of {report.confidence}.' if report else '')

    return Proposal(
        title=f'Upload "{filename}" to Google Drive',
        summary=(f'Upload a {len(body.split())}-word research document to '
                 f'{"folder " + folder_id if folder_id else "the default Drive folder"}.'
                 f'{citation_note} Anybody with folder access will be able to read it, '
                 f'so the citations and the coverage note should be checked before '
                 f'approval.'),
        payload={'name': filename, 'content': body, 'folder_id': folder_id or '',
                 'report_id': report.pk if report else None},
        editable_fields=[
            editable('name', 'File name'),
            editable('folder_id', 'Destination folder id'),
            editable('content', 'Document content', 'longtext',
                     'The full text that will be uploaded.', rows=20),
        ],
        risk='medium', integration_key='google_drive',
        subject_label='marketing.researchreport' if report else '',
        subject_id=report.pk if report else 0,
        subject_display=(report.title if report else filename)[:200])


def _executed_text(action_label, result):
    """One line describing what an approved action actually did.

    Three outcomes, not two. A call that failed must not be reported with the
    word "Uploaded" in front of the error: the approval queue is the one place
    in this platform where the difference between done, simulated and failed
    has to be unmistakable.
    """
    if not result.ok:
        return f'{action_label} failed. {result.error or "No reason was given."}'
    verb = 'was simulated' if result.demo else 'completed'
    detail = result.summary or ''
    return f'{action_label} {verb}. {detail}'.strip()


@executor('research.upload_report_to_drive')
def execute_upload_report_to_drive(action):
    """Carry out an approved Drive upload."""
    payload = action.payload or {}
    result = integrations.call(
        'google_drive', 'upload_file',
        name=payload.get('name', 'Research report.md'),
        content=payload.get('content', ''),
        folder_id=payload.get('folder_id', ''))
    label = f'Upload of "{payload.get("name", "")}" to Google Drive'
    return ToolResult(ok=result.ok, demo=result.demo, error=result.error,
                      text=_executed_text(label, result), data=result.as_dict())


@tool(name='research.share_findings',
      title='Share findings on Slack',
      description=('Prepare a Slack message sharing research findings with a channel, '
                   'optionally attaching a saved report and its citations.'),
      group='Reports', agent_types=('research',), integration='slack',
      requires_approval=True, risk='medium', icon='fa-comments',
      capability='Share findings on Slack',
      parameters={
          'type': 'object',
          'properties': {
              'message': {'type': 'string', 'description': 'What to say.'},
              'channel': {'type': 'string',
                          'description': "Channel, e.g. '#research'. Defaults to the "
                                         'integration default.'},
              'report_id': {'type': 'integer',
                            'description': 'Optional report whose sources are appended.'},
          },
          'required': ['message'],
      })
def share_findings(ctx, message, channel='', report_id=None):
    """Prepare a Slack message for approval. Nothing is posted here.

    The citations are appended to the message body rather than left in the
    report, because a finding shared without its sources is repeated without
    its sources, and the person who reads it in a channel has no way back to
    the document.
    """
    if not (message or '').strip():
        return ToolResult(
            ok=False, error='Empty message.',
            text='There is nothing to share. Supply the message text.')

    report = None
    body = message.strip()
    if report_id:
        report = ResearchReport.objects.filter(pk=_as_int(report_id)).first()
        if report is None:
            return ToolResult(
                ok=False, error=f'No research report #{report_id}.',
                text=(f'There is no research report with id {report_id}. Post the '
                      f'message without report_id, or create a report first.'))
        citations = list(report.citations.all())
        if citations:
            body += '\n\nSources:\n' + '\n'.join(
                f'[{row.ordinal}] {row.source_label}'
                + (f' -- {row.url}' if row.url else '')
                for row in citations)
        body += (f'\n\nResearch report #{report.pk}, confidence '
                 f'{report.confidence}.')
        if report.conflicts:
            body += (f' Note: {len(report.conflicts)} unresolved conflict(s) between '
                     f'documents are recorded on the report.')

    return Proposal(
        title=f'Slack message to {channel or "the default channel"}',
        summary=(f'Share research findings with {channel or "the default Slack channel"}. '
                 f'{len(body.split())} words'
                 + (f', {report.citation_count} citation(s) appended.' if report
                    else ', no citations attached -- consider whether the claims in it '
                         'need sources.')),
        payload={'channel': channel or '', 'text': body,
                 'report_id': report.pk if report else None},
        editable_fields=[
            editable('channel', 'Channel'),
            editable('text', 'Message', 'longtext',
                     'Exactly what will be posted, including the sources.', rows=14),
        ],
        integration_key='slack',
        subject_label='marketing.researchreport' if report else '',
        subject_id=report.pk if report else 0,
        subject_display=(report.title if report else body[:80])[:200])


@executor('research.share_findings')
def execute_share_findings(action):
    """Carry out an approved Slack post."""
    payload = action.payload or {}
    result = integrations.call('slack', 'post_message',
                               channel=payload.get('channel', ''),
                               text=payload.get('text', ''))
    label = (f'Slack post to '
             f'{payload.get("channel") or "the default Slack channel"}')
    return ToolResult(ok=result.ok, demo=result.demo, error=result.error,
                      text=_executed_text(label, result), data=result.as_dict())


@tool(name='research.publish_to_notion',
      title='Publish a page to Notion',
      description=('Prepare a Notion page containing research findings or a document, '
                   'for publication into a workspace database or parent page.'),
      group='Reports', agent_types=('research',), integration='notion',
      requires_approval=True, risk='medium', icon='fa-book',
      capability='Publish a page to Notion',
      parameters={
          'type': 'object',
          'properties': {
              'title': {'type': 'string', 'description': 'The page title.'},
              'content': {'type': 'string', 'description': 'The page body.'},
              'parent_id': {'type': 'string',
                            'description': 'Parent page or database id. Defaults to the '
                                           'integration default.'},
          },
          'required': ['title', 'content'],
      })
def publish_to_notion(ctx, title, content, parent_id=''):
    """Prepare a Notion page for approval. Nothing is published here.

    Publishing into Notion is treated as medium risk rather than low because a
    page in the company workspace becomes a source others will cite. An
    unsourced research page published there is how an invented figure acquires
    the appearance of provenance.
    """
    if not (content or '').strip():
        return ToolResult(
            ok=False, error='Empty content.',
            text=f'The page "{title}" has no body. Supply the content to publish.')

    return Proposal(
        title=f'Publish "{title}" to Notion',
        summary=(f'Create a Notion page of {len(content.split())} words '
                 f'{"under " + parent_id if parent_id else "in the default location"}. '
                 f'Once published, colleagues will cite it, so check that every claim '
                 f'in it names its source before approving.'),
        payload={'title': title.strip()[:200], 'content': content,
                 'parent_id': parent_id or ''},
        editable_fields=[
            editable('title', 'Page title'),
            editable('parent_id', 'Parent page or database id'),
            editable('content', 'Page content', 'longtext',
                     'Exactly what will be published.', rows=20),
        ],
        risk='medium', integration_key='notion',
        subject_display=title.strip()[:200])


@executor('research.publish_to_notion')
def execute_publish_to_notion(action):
    """Carry out an approved Notion publication."""
    payload = action.payload or {}
    result = integrations.call('notion', 'create_page',
                               parent_id=payload.get('parent_id', ''),
                               title=payload.get('title', ''),
                               content=payload.get('content', ''))
    label = f'Notion page "{payload.get("title", "")}"'
    return ToolResult(ok=result.ok, demo=result.demo, error=result.error,
                      text=_executed_text(label, result), data=result.as_dict())
