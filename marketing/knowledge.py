"""The retrieval engine: how the workforce finds out what the company knows.

WHY THIS IS DELIBERATE, DETERMINISTIC PYTHON AND NOT A MODEL CALL
-----------------------------------------------------------------
Every other part of an AI employee's behaviour is produced by a language
model. Retrieval is not, and that is the point. A citation produced by search
is verifiable: the passage either contains the words or it does not, the
document either says the figure or it does not, and anybody can check in ten
seconds. A citation produced by a model is a plausible-looking string, and the
failure mode of a plausible-looking string is that nobody checks it until it
is already in a published post.

So there is no embedding service here, no third-party ranker, and nothing that
needs a network. The scoring is BM25 -- the same family of ranking function
that ran production search for twenty years before dense vectors -- computed
over document passages, with a small number of boosts that are written down
below rather than tuned into opacity.

WHAT BM25 IS DOING, IN ONE PARAGRAPH
-----------------------------------
A passage scores well for a query term when the term appears often in that
passage (term frequency), rarely across the corpus (inverse document
frequency), and the passage is not long enough for the frequency to be an
accident of size (length normalisation, tuned by ``b``). ``k1`` caps the
reward for repetition, because a passage saying "refund" nine times is not
nine times more about refunds than one saying it once. The consequence that
matters for this platform: a specific paragraph in a specific policy beats a
long handbook that mentions the word in passing, which is exactly the ranking
a person would choose by hand.

THE BOOSTS, AND WHY EACH ONE IS HERE
------------------------------------
    heading match   a term in the passage heading is strong evidence the
                    passage is *about* the term rather than mentioning it.
    title match     the same argument one level up.
    exact phrase    a passage containing the whole query verbatim is almost
                    always the answer, and BM25 alone cannot see word order.
    recency         between two passages that say the same thing, the one from
                    the more recently updated document is the better citation.

Each boost is bounded, so no boost can carry a passage that BM25 scored at
nothing. Scores are then normalised so the best hit is 1.0, because a raw
BM25 number is meaningless to a reader and a relative one is not.

THE HARD CONTRACT
-----------------
Three other employees' tool modules import this module by name. The public
API below is therefore fixed, and callers may rely on it:

    index_document  reindex  reindex_all  chunk_text
    search  search_documents  get_document  list_documents  document_stats
    detect_conflicts  summarise  compare_documents  citations_for
    keywords_of  ensure_seed_documents  SearchHit
"""

import hashlib
import re
from dataclasses import dataclass, field

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from .models_knowledge import DocumentChunk, KnowledgeDocument, KnowledgeSource

# ===========================================================================
# Tuning constants, in one place so they can be argued with
# ===========================================================================

BM25_K1 = 1.5           # how quickly the reward for repetition saturates
BM25_B = 0.75           # how strongly passage length is penalised
HEADING_BOOST = 0.14    # per distinct query term found in the passage heading
HEADING_BOOST_CAP = 0.42
TITLE_BOOST = 0.10      # per distinct query term found in the document title
TITLE_BOOST_CAP = 0.30
PHRASE_BOOST = 0.50     # the whole query appears verbatim in the passage
KEYWORD_BOOST = 0.06    # per query term among the passage's distinctive terms
KEYWORD_BOOST_CAP = 0.18
RECENCY_BOOST_CAP = 0.10  # a document updated today over one updated last year
RECENCY_HALF_LIFE_DAYS = 180

CANDIDATE_LIMIT = 800   # how many passages one query will score at most

# Words that carry no retrieval signal. Kept short on purpose: an aggressive
# stopword list removes the very terms a policy question turns on ("notice",
# "before", "within"), so only genuinely empty words are here.
STOPWORDS = frozenset("""
a an and are as at be been being but by can cannot could did do does doing done
for from had has have having he her hers him his how i if in into is it its
me my no nor not of off on once one only or other our ours out over own she
should so some such than that the their theirs them then there these they this
those to too us was we were what when where which while who whom why will with
would you your yours am been being it's i'm don't just also very get got make
made much many any each every both few more most such same too own s t just
about give given tell know want please thing things something anything
someone anyone
""".split())

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*", re.IGNORECASE)
_SENTENCE = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"\'(])')
_UNDERLINE = re.compile(r'^\s*[=\-~_]{3,}\s*$')
_MD_HEADING = re.compile(r'^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$')

# Measurements a conflict can hide in. Two documents stating a different
# number of business days for the same thing is the single most common real
# inconsistency in a company knowledge base, and the one most likely to be
# quoted to a customer.
_UNIT_WORDS = (
    'business days', 'working days', 'calendar days', 'days', 'business hours',
    'hours', 'minutes', 'weeks', 'months', 'years', 'percent', '%',
)
_MEASUREMENT = re.compile(
    r'(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>' +
    '|'.join(re.escape(word) for word in _UNIT_WORDS) + r')\b',
    re.IGNORECASE)
_MONEY = re.compile(r'(?P<symbol>[$£€])\s?(?P<value>\d[\d,]*(?:\.\d{1,2})?)')


# ===========================================================================
# The value type every caller receives
# ===========================================================================

@dataclass
class SearchHit:
    """One retrieved passage, with everything needed to cite it.

    ``why`` exists because a search result that cannot explain itself is a
    search result nobody trusts. It names the terms that actually matched, the
    section they matched in and the document -- which is also, conveniently,
    exactly the sentence a research answer wants to put beside its citation.
    """

    document: object = None
    chunk: object = None
    score: float = 0.0
    snippet: str = ''
    matched_terms: list = field(default_factory=list)
    why: str = ''

    @property
    def document_id(self):
        return getattr(self.document, 'pk', None)

    @property
    def title(self):
        return getattr(self.document, 'title', '')

    @property
    def heading(self):
        return getattr(self.chunk, 'heading', '') or ''

    @property
    def citation_label(self):
        label = getattr(self.document, 'citation_label', '') or self.title
        return f'{label}, {self.heading}' if self.heading else label

    def as_dict(self):
        return {
            'document_id': self.document_id,
            'title': self.title,
            'doc_type': getattr(self.document, 'doc_type', ''),
            'heading': self.heading,
            'score': round(self.score, 3),
            'snippet': self.snippet,
            'matched_terms': list(self.matched_terms),
            'why': self.why,
            'citation_label': self.citation_label,
            'url': getattr(self.document, 'url', '') or '',
        }


# ===========================================================================
# 1. TOKENISING -- the one place text becomes terms
# ===========================================================================

def tokenise(text):
    """Lower-cased word tokens, punctuation discarded."""
    return [match.group(0).lower().strip("'-")
            for match in _WORD.finditer(text or '')
            if match.group(0).strip("'-")]


def content_terms(text):
    """Tokens worth scoring: no stopwords, nothing shorter than two letters."""
    return [token for token in tokenise(text)
            if token not in STOPWORDS and len(token) > 1]


def _stem(token):
    """A deliberately crude suffix trim.

    Not linguistics -- just enough that "refunds" finds the "refund" policy
    and "escalating" finds "escalation". A real stemmer would be a dependency,
    and the failure mode of this one (occasionally conflating two words) is
    far less damaging than the failure mode of exact matching (a question
    phrased in the plural finding nothing at all).
    """
    for suffix, replacement in (('ies', 'y'), ('sses', 'ss'), ('ing', ''),
                                ('ies', 'y'), ('es', ''), ('s', '')):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[:len(token) - len(suffix)] + replacement
    return token


def _term_matches(token, term):
    """Whether one passage token counts as an occurrence of one query term."""
    if token == term:
        return True
    stemmed_token, stemmed_term = _stem(token), _stem(term)
    if stemmed_token == stemmed_term:
        return True
    if len(stemmed_term) >= 5 and stemmed_token.startswith(stemmed_term):
        return True
    return False


def keywords_of(text, limit=12):
    """The distinctive terms of one piece of text.

    Scored by frequency and by length, on the working assumption that in
    company documents the longer word is usually the more specific one
    ("reimbursement" characterises a passage, "form" does not). Used at index
    time to fill ``DocumentChunk.keywords`` and at read time to describe a
    document without opening it.
    """
    counts = {}
    for token in content_terms(text):
        counts[token] = counts.get(token, 0) + 1
    scored = [(count * (1.0 + min(len(token), 14) / 14.0), token)
              for token, count in counts.items()]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [token for _score, token in scored[:max(1, limit)]]


# ===========================================================================
# 2. CHUNKING -- turning a document into retrievable passages
# ===========================================================================

def _looks_like_heading(line, next_line, previous_line=None):
    """Whether one line is a section heading.

    Company documents arrive from Drive, Notion, Confluence and a textarea,
    so there is no single heading convention to rely on. Three are
    recognised: markdown hashes, an underlined line, and a short unpunctuated
    line in title case or capitals that stands alone after a blank line.

    TWO RULES THAT LOOK FUSSY AND ARE NOT
    -------------------------------------
    A line ending in a colon is never a heading here, even a short one in
    title case. Real documents are full of lead-in sentences -- "Notice
    requirements scale with the length of the absence:" -- and treating one as
    a heading does not merely mislabel a passage: it starts a new section,
    which orphans the real heading above it and loses it entirely. The
    citation then names a sentence instead of a section.

    A bare line must also follow a blank line. Without that rule a
    soft-wrapped line of prose whose fragment happens to end without
    punctuation becomes a heading, and the passage beneath it is filed under
    half a sentence.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return False
    if _MD_HEADING.match(line):
        return True
    if next_line is not None and _UNDERLINE.match(next_line):
        return True

    # Bare-line heuristics from here down, and they are deliberately strict.
    if previous_line is not None and previous_line.strip():
        return False
    if stripped.endswith(('.', ',', ';', '?', '!', ':')):
        return False
    if '. ' in stripped:
        return False
    first = stripped[0]
    if not (first.isdigit() or (first.isalpha() and first.isupper())):
        return False

    words = stripped.split()
    if len(words) > 10:
        return False

    letters = [char for char in stripped if char.isalpha()]
    if letters and all(char.isupper() for char in letters):
        return True
    if len(words) == 1:
        return len(stripped) >= 3
    capitalised = sum(1 for word in words if word[:1].isupper())
    return capitalised >= max(2, len(words) - 1)


def _heading_text(line):
    match = _MD_HEADING.match(line)
    if match:
        return match.group(2).strip()
    return line.strip().rstrip(':').strip()


def _sections(content):
    """[(heading, [paragraph, ...]), ...] in document order."""
    lines = (content or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    sections = [('', [])]
    buffer = []

    def flush():
        if buffer:
            paragraph = ' '.join(part.strip() for part in buffer if part.strip())
            if paragraph:
                sections[-1][1].append(paragraph)
            buffer.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else None
        preceding = lines[index - 1] if index else None

        if _UNDERLINE.match(line):
            index += 1
            continue
        if not line.strip():
            flush()
            index += 1
            continue
        if _looks_like_heading(line, following, preceding):
            flush()
            sections.append((_heading_text(line), []))
            if following is not None and _UNDERLINE.match(following):
                index += 1
            index += 1
            continue

        buffer.append(line)
        index += 1

    flush()
    return [(heading, paragraphs) for heading, paragraphs in sections if paragraphs]


def chunk_text(content, target_words=180, overlap_words=30):
    """Split a document into overlapping, heading-aware passages.

    Sections first, paragraphs second. A chunk never spans two headings,
    because a citation that names the wrong section is worse than one that
    names none. Within a section, paragraphs are packed up to
    ``target_words`` and each chunk after the first is prefixed with the tail
    of its predecessor -- the overlap that keeps a sentence straddling a
    boundary findable from either side.

    Returns a list of dicts, one per passage:
    ``{'ordinal', 'text', 'heading', 'keywords', 'word_count'}``.
    """
    target_words = max(60, int(target_words or 180))
    overlap_words = max(0, min(int(overlap_words or 0), target_words // 2))

    raw = []
    for heading, paragraphs in _sections(content):
        current = []
        current_words = 0
        previous_tail = ''

        for paragraph in paragraphs:
            words = paragraph.split()
            if current_words and current_words + len(words) > target_words:
                text = ' '.join(current)
                raw.append({'heading': heading,
                            'text': (previous_tail + ' ' + text).strip()})
                previous_tail = ' '.join(text.split()[-overlap_words:]) if overlap_words else ''
                current, current_words = [], 0
            current.append(paragraph)
            current_words += len(words)

        if current:
            text = ' '.join(current)
            raw.append({'heading': heading,
                        'text': (previous_tail + ' ' + text).strip()})

    if not raw:
        flat = ' '.join((content or '').split())
        if not flat:
            return []
        raw = [{'heading': '', 'text': flat}]

    # A passage of a dozen words is not worth retrieving on its own; fold it
    # forward so a one-line section still lands inside a citable passage.
    merged = []
    for entry in raw:
        if merged and len(entry['text'].split()) < 25 and not entry['heading']:
            merged[-1]['text'] = f"{merged[-1]['text']} {entry['text']}".strip()
            continue
        if merged and len(merged[-1]['text'].split()) < 25:
            entry['heading'] = merged[-1]['heading'] or entry['heading']
            entry['text'] = f"{merged.pop()['text']} {entry['text']}".strip()
        merged.append(entry)

    chunks = []
    for ordinal, entry in enumerate(merged):
        text = entry['text'].strip()
        chunks.append({
            'ordinal': ordinal,
            'text': text,
            'heading': entry['heading'][:250],
            'keywords': keywords_of(f"{entry['heading']} {text}", limit=14),
            'word_count': len(text.split()),
        })
    return chunks


# ===========================================================================
# 3. INDEXING
# ===========================================================================

def checksum_of(content):
    return hashlib.sha256((content or '').encode('utf-8')).hexdigest()


def reindex(document):
    """Rebuild one document's passages. Returns the passage count.

    Deletes and rewrites rather than diffing. Chunk boundaries move when the
    text changes, so a diff would produce passages that are half old and half
    new -- and a citation into such a passage would quote text that never
    appeared together in the document.
    """
    if document is None:
        return 0
    document.chunks.all().delete()
    rows = [
        DocumentChunk(document=document, ordinal=entry['ordinal'], text=entry['text'],
                      heading=entry['heading'], keywords=entry['keywords'],
                      word_count=entry['word_count'])
        for entry in chunk_text(document.content)
    ]
    if rows:
        DocumentChunk.objects.bulk_create(rows)
    return len(rows)


def reindex_all():
    """Rebuild every passage in the knowledge base."""
    documents = 0
    chunks = 0
    for document in KnowledgeDocument.objects.all():
        documents += 1
        chunks += reindex(document)
    return {'documents': documents, 'chunks': chunks}


def index_document(*, title, content, doc_type='other', source=None, external_id='',
                   url='', tags=None, department='', author='', version='1',
                   created_by=None, created_by_agent=None):
    """Add or update one document and build its passages.

    Identity is ``(source, external_id)`` when the originating system gave us
    an identifier, and the exact title otherwise. That ordering matters for
    synchronisation: a Drive file whose name was changed is the same document
    and must be updated, whereas two documents entered by hand with the same
    title are the same document by the only handle a person gave us.

    Idempotent. Re-indexing byte-identical content touches ``indexed_at`` and
    nothing else -- no rewritten row, no rebuilt passages -- which is what
    makes a nightly synchronisation of fifty unchanged files nearly free.
    """
    title = (title or '').strip()[:250]
    content = content or ''
    if not title:
        title = ' '.join(content.split()[:8])[:250] or 'Untitled document'

    external_id = (external_id or '').strip()[:200]
    digest = checksum_of(content)

    existing = None
    if external_id:
        existing = KnowledgeDocument.objects.filter(
            source=source, external_id=external_id).first()
    if existing is None:
        existing = KnowledgeDocument.objects.filter(title=title).first()

    fields = {
        'title': title,
        'doc_type': doc_type or 'other',
        'url': (url or '')[:500],
        'department': (department or '')[:80],
        'author': (author or '')[:140],
        'version': str(version or '1')[:30],
        'tags': list(tags or []),
        'is_active': True,
    }

    if existing is not None:
        unchanged = existing.checksum == digest and existing.chunks.exists()
        existing.content = content
        existing.checksum = digest
        existing.word_count = len(content.split())
        existing.indexed_at = timezone.now()
        if source is not None:
            existing.source = source
        if external_id:
            existing.external_id = external_id
        for key, value in fields.items():
            # Never blank a stored value because the caller passed nothing.
            if value in ('', [], None) and getattr(existing, key) not in ('', [], None):
                continue
            setattr(existing, key, value)
        existing.save()
        if not unchanged:
            reindex(existing)
        _refresh_source_count(existing.source)
        return existing

    document = KnowledgeDocument.objects.create(
        source=source, external_id=external_id, content=content, checksum=digest,
        word_count=len(content.split()), indexed_at=timezone.now(),
        created_by=created_by if getattr(created_by, 'pk', None) else None,
        created_by_agent=created_by_agent, **fields)
    reindex(document)
    _refresh_source_count(source)
    return document


def _refresh_source_count(source):
    if source is None or not getattr(source, 'pk', None):
        return
    KnowledgeSource.objects.filter(pk=source.pk).update(
        document_count=KnowledgeDocument.objects.filter(source=source).count())


# ===========================================================================
# 4. SEARCH -- BM25 over passages
# ===========================================================================

def _query_terms(query):
    """The scoring terms of a query, de-duplicated, in order."""
    seen = []
    for term in content_terms(query):
        if term not in seen:
            seen.append(term)
    return seen


def _candidate_chunks(terms, doc_type='', department='', source_kind=''):
    """The passages worth scoring for one query.

    A coarse database filter first, BM25 in Python second. Scoring every
    passage in the corpus would also work and would be slower for no gain: a
    passage containing none of the query terms and belonging to a document
    whose title contains none of them scores exactly zero, and there is no
    boost that can rescue it.
    """
    clause = Q()
    for term in terms:
        clause |= Q(text__icontains=term)
        clause |= Q(document__title__icontains=term)
        clause |= Q(heading__icontains=term)

    query = DocumentChunk.objects.select_related('document', 'document__source').filter(
        document__is_active=True)
    if doc_type:
        query = query.filter(document__doc_type=doc_type)
    if department:
        query = query.filter(document__department__iexact=department)
    if source_kind:
        query = query.filter(document__source__kind=source_kind)

    return list(query.filter(clause)[:CANDIDATE_LIMIT])


def _document_frequencies(terms, doc_type='', department='', source_kind=''):
    """How many passages in the corpus contain each term, plus corpus size.

    Computed against the same filtered corpus the search runs over, so that
    narrowing to one document type does not leave the inverse document
    frequencies describing a different collection than the one being ranked.
    """
    query = DocumentChunk.objects.filter(document__is_active=True)
    if doc_type:
        query = query.filter(document__doc_type=doc_type)
    if department:
        query = query.filter(document__department__iexact=department)
    if source_kind:
        query = query.filter(document__source__kind=source_kind)

    total = query.count()
    average = query.aggregate(value=Avg('word_count'))['value'] or 1.0
    frequencies = {term: query.filter(text__icontains=term).count() for term in terms}
    return total, float(average), frequencies


def _idf(total, frequency):
    """Robertson-Sparck-Jones inverse document frequency, floored at zero.

    The ``1 +`` inside the logarithm is what keeps a term appearing in most of
    the corpus from scoring negatively and thereby *penalising* a passage for
    containing a word the user asked for.
    """
    import math
    return math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))


def _recency_factor(document):
    """Up to ``RECENCY_BOOST_CAP``, halving every ``RECENCY_HALF_LIFE_DAYS``."""
    stamp = getattr(document, 'updated_at', None) or getattr(document, 'created_at', None)
    if stamp is None:
        return 0.0
    age_days = max(0.0, (timezone.now() - stamp).total_seconds() / 86400.0)
    return RECENCY_BOOST_CAP * (0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))


def _window(text, terms, width=300):
    """A readable extract centred on the first matched term.

    Query terms are never elided from the extract. A snippet that omits the
    word the reader searched for looks like a wrong result even when the
    ranking was right.
    """
    flat = ' '.join((text or '').split())
    if len(flat) <= width:
        return flat

    lowered = flat.lower()
    position = -1
    for term in terms:
        found = lowered.find(term)
        if found != -1 and (position == -1 or found < position):
            position = found
    if position == -1:
        position = 0

    start = max(0, position - width // 3)
    end = min(len(flat), start + width)
    start = max(0, end - width)

    if start > 0:
        space = flat.find(' ', start)
        start = space + 1 if 0 <= space < start + 25 else start
    if end < len(flat):
        space = flat.rfind(' ', start, end)
        end = space if space > start + width - 40 else end

    extract = flat[start:end].strip()
    return f"{'...' if start > 0 else ''}{extract}{'...' if end < len(flat) else ''}"


def _why(matched, heading, title, phrase_hit):
    """One sentence naming the actual reason this passage came back."""
    quoted = ', '.join(f"'{term}'" for term in matched[:4]) or 'no distinctive term'
    where = f"the '{heading}' section of {title}" if heading else title
    if phrase_hit:
        return f'contains the exact phrase searched for, in {where}'
    return f'matched {quoted} in {where}'


def search(query, *, limit=8, doc_type='', department='', source_kind='',
           min_score=0.0):
    """Rank passages against a query. The function everything else leans on.

    An empty result is a real answer here. A query of nothing but stopwords
    ("what about the one for them") returns ``[]`` rather than the whole
    corpus, because handing back every document in response to a question
    that contained no searchable term teaches the caller to trust results it
    should not.
    """
    terms = _query_terms(query)
    if not terms:
        return []

    candidates = _candidate_chunks(terms, doc_type, department, source_kind)
    if not candidates:
        return []

    total, average_length, frequencies = _document_frequencies(
        terms, doc_type, department, source_kind)
    total = max(total, 1)
    idf = {term: _idf(total, frequencies.get(term, 0)) for term in terms}

    phrase = ' '.join(tokenise(query))
    scored = []

    for chunk in candidates:
        tokens = tokenise(chunk.text)
        if not tokens:
            continue
        length = len(tokens)
        heading_tokens = tokenise(chunk.heading)
        title_tokens = tokenise(chunk.document.title)
        chunk_keywords = [str(word).lower() for word in (chunk.keywords or [])]

        base = 0.0
        matched = []
        for term in terms:
            frequency = sum(1 for token in tokens if _term_matches(token, term))
            if not frequency:
                # A term present only in the heading or title still counts as
                # a match for the purpose of explaining the result, and picks
                # up its boost below.
                if any(_term_matches(token, term) for token in heading_tokens + title_tokens):
                    matched.append(term)
                continue
            matched.append(term)
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * (length / max(average_length, 1.0)))
            base += idf[term] * (frequency * (BM25_K1 + 1)) / denominator

        if base <= 0 and not matched:
            continue

        heading_hits = sum(1 for term in terms
                           if any(_term_matches(token, term) for token in heading_tokens))
        title_hits = sum(1 for term in terms
                         if any(_term_matches(token, term) for token in title_tokens))
        keyword_hits = sum(1 for term in terms
                           if any(_term_matches(word, term) for word in chunk_keywords))
        phrase_hit = bool(phrase) and phrase in ' '.join(tokens)

        multiplier = 1.0
        multiplier += min(heading_hits * HEADING_BOOST, HEADING_BOOST_CAP)
        multiplier += min(title_hits * TITLE_BOOST, TITLE_BOOST_CAP)
        multiplier += min(keyword_hits * KEYWORD_BOOST, KEYWORD_BOOST_CAP)
        multiplier += PHRASE_BOOST if phrase_hit else 0.0
        multiplier += _recency_factor(chunk.document)

        # A passage that matched only through its title carries no BM25 mass
        # of its own, so give it a small floor rather than dropping it: the
        # document is plainly on topic even if this passage is not.
        final = (base or 0.05 * len(matched)) * multiplier
        if final <= 0:
            continue

        scored.append((final, chunk, matched, phrase_hit))

    if not scored:
        return []

    top = max(entry[0] for entry in scored)
    scored.sort(key=lambda entry: (-entry[0], entry[1].document_id, entry[1].ordinal))

    hits = []
    for raw_score, chunk, matched, phrase_hit in scored:
        normalised = round(raw_score / top, 4) if top else 0.0
        if normalised < min_score:
            continue
        hits.append(SearchHit(
            document=chunk.document,
            chunk=chunk,
            score=normalised,
            snippet=_window(chunk.text, terms),
            matched_terms=list(matched),
            why=_why(matched, chunk.heading, chunk.document.title, phrase_hit),
        ))
        if len(hits) >= max(1, int(limit or 8)):
            break
    return hits


def search_documents(query, **kwargs):
    """Alias for ``search``, kept because other tool modules call it by name."""
    return search(query, **kwargs)


def term_coverage(query):
    """Which of a query's terms the corpus contains at all, and how commonly.

    WHY A RANKING IS NOT ENOUGH
    ---------------------------
    Scores are normalised, so the best passage for any query scores 1.0 --
    including the best passage for a question the knowledge base knows nothing
    about. Asked "what is our policy on cryptocurrency payments" the ranker
    dutifully returns the refund policy, because "policy" and "payment" are
    both in it, and a caller that trusted the score alone would answer a
    question about cryptocurrency out of a document that has never heard of
    it. That is the exact failure this whole subsystem exists to prevent.

    So this function asks a different question: does the corpus contain these
    words *anywhere*. A term with a document frequency of zero is a subject
    the company has not written down, and saying so is the answer.

    ``subject`` holds the rarest present terms -- the words that make the
    question specific rather than generic. A passage that matched only
    "policy" and "within" is not an answer to anything.

    Returns ``{'terms', 'present', 'absent', 'substantive', 'frequencies',
    'subject', 'corpus'}``.
    """
    terms = _query_terms(query)
    corpus = DocumentChunk.objects.filter(document__is_active=True).count()

    frequencies = {}
    for term in terms:
        frequencies[term] = DocumentChunk.objects.filter(
            document__is_active=True).filter(
            Q(text__icontains=term) | Q(document__title__icontains=term)).count()

    present = [term for term in terms if frequencies[term] > 0]
    absent = [term for term in terms if frequencies[term] == 0]

    # A four-letter minimum and no bare numbers. A year or a plan code missing
    # from the corpus says nothing about whether the subject is covered, and
    # treating "2027" as an uncovered subject would refuse to answer perfectly
    # answerable questions.
    substantive = [term for term in terms
                   if len(term) >= 4 and not term.isdigit()]

    subject = []
    if present:
        ranked = sorted(present, key=lambda term: (frequencies[term], term))
        floor = frequencies[ranked[0]]
        subject = [term for term in ranked
                   if frequencies[term] <= max(floor * 2, floor + 1)][:3]

    return {
        'terms': terms,
        'present': present,
        'absent': absent,
        'substantive': substantive,
        'frequencies': frequencies,
        'subject': subject,
        'corpus': corpus,
    }


# ===========================================================================
# 5. READING THE KNOWLEDGE BASE
# ===========================================================================

def get_document(document_id):
    """One document by primary key, or None. Never raises on a bad id."""
    try:
        key = int(document_id)
    except (TypeError, ValueError):
        return None
    return KnowledgeDocument.objects.select_related('source').filter(pk=key).first()


def list_documents(doc_type='', department='', source=None, limit=50, query=''):
    """Documents matching a coarse filter, newest first within the title order."""
    rows = KnowledgeDocument.objects.select_related('source').filter(is_active=True)
    if doc_type:
        rows = rows.filter(doc_type=doc_type)
    if department:
        rows = rows.filter(department__iexact=department)
    if source is not None:
        rows = rows.filter(source=source)
    if query:
        rows = rows.filter(Q(title__icontains=query) | Q(content__icontains=query))
    return list(rows[:max(1, int(limit or 50))])


def document_stats():
    """A one-glance picture of what the company actually has written down."""
    documents = KnowledgeDocument.objects.filter(is_active=True)
    totals = documents.aggregate(words=Sum('word_count'))
    by_type = {}
    for row in documents.values('doc_type').annotate(count=Count('pk')):
        by_type[row['doc_type']] = row['count']
    latest = documents.order_by('-indexed_at').values_list('indexed_at', flat=True).first()
    return {
        'documents': documents.count(),
        'chunks': DocumentChunk.objects.filter(document__is_active=True).count(),
        'words': int(totals['words'] or 0),
        'by_type': by_type,
        'sources': KnowledgeSource.objects.count(),
        'last_indexed': latest,
    }


# ===========================================================================
# 6. ANALYSIS -- conflicts, summaries, comparisons, citations
# ===========================================================================

# Words that must not count towards "these two figures are about the same
# thing". Without this list every pair of figures measured in business days
# looks related, because the unit words themselves sit in each other's
# context window: a refund processed in 5 business days would be reported as
# conflicting with a support target of 10 business days on the strength of
# sharing the words "business" and "days". Excluding measurement vocabulary
# and connective filler leaves only the words that name the subject.
_CONTEXT_IGNORE_WORDS = (
    'business', 'working', 'calendar', 'day', 'days', 'hour', 'hours',
    'minute', 'minutes', 'week', 'weeks', 'month', 'months', 'year', 'years',
    'percent', 'cent', 'within', 'further', 'least', 'maximum', 'minimum',
    'per', 'take', 'takes', 'taken', 'show', 'shown', 'following', 'first',
    'second', 'third', 'next', 'last', 'available', 'applicable', 'after',
    'before', 'between', 'target', 'targets', 'time', 'times', 'up', 'still',
    'may', 'must', 'shall', 'within', 'least',
)
_CONTEXT_IGNORE = frozenset(_stem(word) for word in _CONTEXT_IGNORE_WORDS)

# How much subject vocabulary two figures must share before they are called a
# conflict, and how many conflicts one call will report. The cap exists
# because a list of forty near-duplicate findings is read as noise and
# therefore not read at all.
CONFLICT_MIN_SHARED = 2
CONFLICT_LIMIT = 12


def _measurements(text):
    """Every quantity in a passage, with the words around it.

    The surrounding words are the whole trick. "5 business days" on its own
    conflicts with nothing; "refunds are processed within 5 business days"
    conflicts with "refunds are processed within 7 business days" and with
    nothing else in the document.
    """
    flat = ' '.join((text or '').split())
    found = []

    for match in _MEASUREMENT.finditer(flat):
        found.append({
            'value': match.group('value'),
            'unit': match.group('unit').lower().replace('percent', '%'),
            'phrase': match.group(0),
            'context': _context_words(flat, match.start(), match.end()),
            'quote': _quote_around(flat, match.start(), match.end()),
        })

    for match in _MONEY.finditer(flat):
        found.append({
            'value': match.group('value').replace(',', ''),
            'unit': match.group('symbol'),
            'phrase': match.group(0),
            'context': _context_words(flat, match.start(), match.end()),
            'quote': _quote_around(flat, match.start(), match.end()),
        })
    return found


def _context_words(flat, start, end, span=9):
    """The subject vocabulary immediately around one figure."""
    before = content_terms(flat[max(0, start - 140):start])[-span:]
    after = content_terms(flat[end:end + 140])[:span]
    words = {_stem(word) for word in before + after if not word.isdigit()}
    return words - _CONTEXT_IGNORE


def _quote_around(flat, start, end, width=160):
    left = max(0, start - width // 2)
    right = min(len(flat), end + width // 2)
    return flat[left:right].strip()


def detect_conflicts(hits):
    """Pairs of retrieved passages that state different values for one thing.

    WHY THIS REPORTS RATHER THAN RESOLVES
    -------------------------------------
    When the refund policy says five business days and the troubleshooting
    guide says seven, the tempting behaviour is to pick the one from the more
    authoritative document and answer confidently. That behaviour is wrong,
    and expensively so: the guide is what the support team reads, so a
    customer has already been told seven, and the real finding is not "the
    answer is five" but "these two documents disagree and one needs fixing".

    So this function only ever reports. Each entry is
    ``{'topic', 'a', 'b', 'note'}``, and it is the caller's job to put both
    sides in front of a person.
    """
    entries = []
    seen = set()

    prepared = []
    for hit in hits or []:
        document = getattr(hit, 'document', None)
        if document is None:
            continue
        text = getattr(getattr(hit, 'chunk', None), 'text', None) or getattr(
            document, 'content', '')
        for measurement in _measurements(text):
            prepared.append((hit, document, measurement))

    for index, (hit_a, document_a, first) in enumerate(prepared):
        for hit_b, document_b, second in prepared[index + 1:]:
            if document_a.pk == document_b.pk:
                continue
            if first['unit'] != second['unit']:
                continue
            if _same_number(first['value'], second['value']):
                continue

            shared = first['context'] & second['context']
            if len(shared) < CONFLICT_MIN_SHARED:
                continue

            topic_words = sorted(shared)[:4]
            topic = ' / '.join(topic_words)

            # Deduplicated on the finding, not on the wording of the finding.
            # Two figures often sit inside each other's context window twice
            # over, which produces the same disagreement described with a
            # slightly different topic phrase; reporting it twice makes a real
            # finding look like a list of them.
            key = (first['unit'],
                   *sorted([str(document_a.pk), str(document_b.pk)]),
                   *sorted([first['value'], second['value']]))
            if key in seen:
                continue
            seen.add(key)

            entries.append({
                'topic': f"{topic} ({first['unit']})",
                'a': {
                    'document_id': document_a.pk,
                    'document': document_a.title,
                    'version': document_a.version,
                    'value': first['phrase'],
                    'quote': first['quote'],
                    'updated': _stamp(document_a),
                },
                'b': {
                    'document_id': document_b.pk,
                    'document': document_b.title,
                    'version': document_b.version,
                    'value': second['phrase'],
                    'quote': second['quote'],
                    'updated': _stamp(document_b),
                },
                'note': (f"{document_a.title} states {first['phrase']} while "
                         f"{document_b.title} states {second['phrase']} for the same "
                         f"subject ({topic}). Both are reported; neither has been "
                         f"chosen."),
            })
            if len(entries) >= CONFLICT_LIMIT:
                return entries
    return entries


def _same_number(left, right):
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return str(left) == str(right)


def _stamp(document):
    value = getattr(document, 'updated_at', None) or getattr(document, 'created_at', None)
    return value.strftime('%d %b %Y') if value else ''


def prose_only(text):
    """The text with heading and underline lines removed, flattened to one line.

    Public because sentence-level work outside this module needs it too. A
    heading is not a sentence, and a sentence splitter fed raw markdown glues
    each heading to the paragraph below it -- which turns "Escalate on any P1
    open for more than 4 hours." into a two-hundred-word run-on that no
    per-sentence rule can classify correctly.
    """
    kept = []
    lines = (text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    for index, line in enumerate(lines):
        if not line.strip() or _UNDERLINE.match(line):
            continue
        following = lines[index + 1] if index + 1 < len(lines) else None
        preceding = lines[index - 1] if index else None
        if _looks_like_heading(line, following, preceding):
            continue
        kept.append(line.strip())
    return ' '.join(' '.join(kept).split())


def summarise(text, max_sentences=5):
    """An extractive summary: the document's own best sentences, in order.

    Extractive rather than generated, for the same reason the whole module is
    deterministic. Every sentence in the output appeared verbatim in the
    input, so a summary cannot introduce a claim the document did not make --
    which is the one failure a summary of a company policy must not have.

    Sentences are scored by the density of distinctive terms they carry, with
    a bonus for coming early, since company documents state their subject in
    the opening lines. The chosen sentences are then returned in their
    original order, because a summary reordered by score reads as nonsense.

    Heading lines are dropped before scoring. A heading is not a sentence, and
    left in it glues itself to the paragraph beneath -- producing a "summary"
    that opens "# Customer Refund Policy This policy is the authoritative
    statement", which is a formatting accident presented as prose.
    """
    flat = prose_only(text)
    if not flat:
        return ''

    sentences = [part.strip() for part in _SENTENCE.split(flat) if part.strip()]
    if not sentences:
        return ''
    wanted = max(1, int(max_sentences or 5))
    if len(sentences) <= wanted:
        return ' '.join(sentences)

    frequency = {}
    for token in content_terms(flat):
        frequency[token] = frequency.get(token, 0) + 1
    peak = max(frequency.values()) if frequency else 1

    scored = []
    for position, sentence in enumerate(sentences):
        tokens = content_terms(sentence)
        if not tokens:
            scored.append((0.0, position))
            continue
        density = sum(frequency.get(token, 0) / peak for token in tokens) / len(tokens)
        length_penalty = 0.75 if len(tokens) < 5 else 1.0
        position_bonus = 0.35 / (1 + position * 0.6)
        scored.append(((density + position_bonus) * length_penalty, position))

    scored.sort(key=lambda pair: -pair[0])
    keep = sorted(position for _score, position in scored[:wanted])
    return ' '.join(sentences[position] for position in keep)


def compare_documents(doc_a, doc_b):
    """What two documents agree on, cover alone, and disagree about.

    ``differences`` is the part worth reading. Two overlapping documents --
    a policy and the guide that paraphrases it -- are exactly where a company
    accumulates contradictions, and finding them is cheaper than discovering
    one through a customer.
    """
    if doc_a is None or doc_b is None:
        return {'shared': [], 'only_a': [], 'only_b': [], 'differences': []}

    terms_a = set(keywords_of(f'{doc_a.title} {doc_a.content}', limit=45))
    terms_b = set(keywords_of(f'{doc_b.title} {doc_b.content}', limit=45))

    hits = [
        SearchHit(document=doc_a, chunk=None, score=1.0, snippet=doc_a.snippet),
        SearchHit(document=doc_b, chunk=None, score=1.0, snippet=doc_b.snippet),
    ]

    return {
        'shared': sorted(terms_a & terms_b),
        'only_a': sorted(terms_a - terms_b),
        'only_b': sorted(terms_b - terms_a),
        'differences': detect_conflicts(hits),
    }


def citations_for(hits):
    """Turn ranked passages into citation records, one per document.

    De-duplicated by document and kept in rank order: three passages from the
    same handbook are one citation, quoting the best of the three. A reader
    following up wants the document once, at the place that answered them.
    """
    seen = {}
    ordered = []
    for hit in hits or []:
        document = getattr(hit, 'document', None)
        key = getattr(document, 'pk', None)
        if key is None or key in seen:
            continue
        seen[key] = True
        ordered.append({
            'label': getattr(hit, 'citation_label', '') or getattr(document, 'title', ''),
            'url': getattr(document, 'url', '') or '',
            'quote': (getattr(hit, 'snippet', '') or '')[:600],
            'relevance': round(float(getattr(hit, 'score', 0.0) or 0.0), 3),
            'document_id': key,
        })
    return ordered


# ===========================================================================
# 7. THE STARTER KNOWLEDGE BASE
# ===========================================================================

def ensure_seed_documents(owner=None):
    """Create the starter knowledge base if it is not already there.

    WHY THE PLATFORM SHIPS WITH CONTENT
    -----------------------------------
    An empty knowledge base makes five of the six employees useless and the
    sixth pointless. The HR employee asked about carry-over has nothing to
    cite, the Support employee asked about a refund has nothing to quote, and
    the honest answer -- "the knowledge base does not cover this" -- is
    correct behaviour that looks like a broken product. So the documents below
    exist: real policies, with real numbers, written the way a company writes
    them, indexed on first run with nothing to upload.

    ONE DELIBERATE INCONSISTENCY
    ----------------------------
    The refund policy states that an approved refund is processed within 5
    business days. The troubleshooting guide, written by a different team at a
    different time, says 7. That is not an oversight. Every real knowledge
    base contains contradictions of exactly that shape, a platform that only
    ever demonstrates agreement has not shown the hard part, and
    ``detect_conflicts`` needs something true to find. Reporting the
    disagreement is the feature.

    Returns the number of documents created, so a caller can tell a first run
    from a repeat one.
    """
    source, _created = KnowledgeSource.objects.get_or_create(
        name='Company Handbook',
        defaults={
            'kind': 'manual',
            'sync_mode': 'manual',
            'last_sync_message': 'Starter knowledge base shipped with the platform.',
        },
    )

    created = 0
    for entry in _SEED_DOCUMENTS:
        title = entry['title']
        if KnowledgeDocument.objects.filter(title=title).exists():
            continue
        index_document(
            title=title,
            content=entry['content'].strip(),
            doc_type=entry['doc_type'],
            department=entry.get('department', ''),
            tags=entry.get('tags', []),
            author=entry.get('author', 'Company Operations'),
            version=entry.get('version', '1'),
            source=source,
            created_by=owner if getattr(owner, 'pk', None) else None,
        )
        created += 1

    if created:
        KnowledgeSource.objects.filter(pk=source.pk).update(
            last_synced_at=timezone.now(),
            document_count=KnowledgeDocument.objects.filter(source=source).count(),
            last_sync_message=f'Indexed {created} starter documents.')
    return created


# The starter corpus. Written as markdown so the heading detector can carry a
# real section name onto every passage, which is what lets a citation say
# "Notice and approval, Leave and Time Off Policy" rather than just naming a
# forty-paragraph document and leaving the reader to hunt.
_SEED_DOCUMENTS = [
    {
        'title': 'Employee Handbook',
        'doc_type': 'handbook',
        'department': 'People Operations',
        'version': '4.1',
        'tags': ['handbook', 'conduct', 'working hours', 'remote work', 'probation'],
        'content': """
# Employee Handbook

This handbook sets out how we work together. It applies to every employee,
contractor and intern, and it is the document referred to whenever a question
about conduct or working arrangements arises. Where this handbook and an
individual employment contract differ, the contract prevails.

## Working hours

Standard full-time hours are 38 hours per week, ordinarily worked between
9.00am and 5.30pm Monday to Friday, with a 30 minute unpaid lunch break.
Core hours, during which everyone is expected to be contactable, are 10.00am
to 4.00pm in the employee's local time. Outside core hours people arrange
their own schedule.

Flexible start and finish times need no approval provided core hours are
covered and commitments to colleagues and customers are met. A permanent
change to contracted hours, including a move to part-time, is a change to the
employment contract and requires written agreement from the employee's manager
and People Operations.

Overtime is not expected. Where additional hours are genuinely necessary,
employees on an hourly agreement are paid at the applicable rate and salaried
employees take equivalent time in lieu within the following month.

## Remote and hybrid work

The default arrangement is hybrid: three days in the office and two days
remote each week, with the in-office days agreed within the team rather than
mandated centrally. Fully remote arrangements are available by agreement and
are reviewed at each performance cycle.

Employees working remotely are expected to have a working internet connection
adequate for video calls, a quiet space for customer conversations, and the
company-issued laptop. Personal devices are not used for company data. Working
from a location outside the country of employment for more than 14 consecutive
days requires prior approval, because it has tax and insurance consequences.

## Conduct

We expect professional, courteous behaviour towards colleagues, customers,
candidates and suppliers. Harassment, discrimination, bullying and retaliation
are grounds for disciplinary action up to and including termination. Concerns
may be raised with a manager, with People Operations, or anonymously through
the reporting form; every report is acknowledged within two business days.

Confidential information stays inside the company. Customer data, financial
information, unreleased product plans and personal information about
colleagues are not discussed outside the company, published, or moved to
personal accounts or storage. The Data Privacy and Security Policy sets out
the classifications in detail.

Conflicts of interest are declared rather than judged privately. Outside
employment, board positions, investments in competitors and family
relationships with suppliers are disclosed to People Operations in writing.

## Probation

New employees serve a probation period of six months. Managers hold a
documented check-in at the end of month one, month three and month five.
Probation may be extended once, by up to three months, where the manager and
People Operations agree that more time would give a fair assessment; an
extension is confirmed in writing with specific objectives.

During probation the notice period is two weeks on either side. After
confirmation it becomes four weeks, or as stated in the employment contract if
longer.

## Equipment and expenses

Every employee receives a laptop, a monitor, a keyboard, a mouse and a
headset. A home-office allowance of $500 is available in the first three
months and $250 every two years thereafter. Expenses are submitted within 30
days of being incurred, with a receipt, and are approved by the employee's
manager before payment.
""",
    },
    {
        'title': 'Leave and Time Off Policy',
        'doc_type': 'policy',
        'department': 'People Operations',
        'version': '3.0',
        'tags': ['leave', 'annual leave', 'parental leave', 'accrual', 'carry-over',
                 'notice', 'sick leave'],
        'content': """
# Leave and Time Off Policy

This policy states the leave every employee is entitled to, how it accrues,
how much notice is required, and what happens to leave that is not taken. It
is the authoritative source for leave questions and figures quoted from it may
be relied upon.

## Annual leave

Full-time employees accrue 20 days of paid annual leave per year, accruing at
1.667 days for each completed month of service. Part-time employees accrue on
the same basis pro rata to their contracted hours. Accrual begins on the first
day of employment, including during probation.

Annual leave may be taken before it has fully accrued, up to a maximum of five
days in advance, with the manager's written agreement. Leave taken in advance
and not subsequently accrued is deducted from the final payment on
termination.

## Personal and carer's leave

Employees receive 10 days of paid personal leave per year for their own
illness or injury, or to care for an immediate family or household member.
Personal leave accrues progressively and any unused balance carries over
without limit.

A medical certificate is required for any absence of three or more consecutive
days, and for any single day of personal leave taken immediately before or
after a public holiday or a period of annual leave. Two days of unpaid carer's
leave per occasion are available once the paid balance is exhausted.

Compassionate leave of two days per occasion is paid and separate from
personal leave. It applies on the death or life-threatening illness of an
immediate family or household member.

## Parental leave

Employees with 12 months of continuous service are entitled to paid parental
leave: 14 weeks at full pay for the primary carer and 4 weeks at full pay for
the secondary carer. This sits alongside, and is not reduced by, any
government-funded parental leave payment.

Unpaid parental leave of up to 12 months is available, with a right to request
a further 12 months. A request for the extension is answered in writing within
21 days and may only be refused on reasonable business grounds, with those
grounds stated. Employees returning from parental leave return to their
pre-leave position, or to a comparable position at the same classification and
pay where the pre-leave position no longer exists.

Keeping-in-touch days are permitted: up to 10 paid days during unpaid parental
leave, at the employee's initiative.

## Notice and approval

Notice requirements scale with the length of the absence:

- One or two days: 48 hours' notice.
- Three to five days: two weeks' notice.
- More than five days: four weeks' notice.
- More than 15 consecutive days: eight weeks' notice.

Requests are submitted in the leave system and answered by the manager within
five business days. A request may be declined only where the absence would
leave the team unable to meet a committed deadline, and the manager must offer
an alternative period when declining. Personal leave requires no advance
notice; the employee notifies their manager as early as practicable on the
first day of absence.

## Carry-over and cash-out

Up to 5 days of unused annual leave carry over into the following leave year
and must be taken by 31 March. Any balance above 5 days at 31 December is
forfeited unless the manager and People Operations agreed in writing before 30
November that it could not reasonably be taken.

An employee with an accrued balance above 20 days may request to cash out up
to 5 days per year, provided at least 20 days remain accrued after the
cash-out. Cash-out is at the employee's initiative only and is never a
condition of anything.

## Public holidays and long service leave

Employees receive 11 public holidays per year according to the location of
employment. Where an employee is required to work a public holiday, they
receive an alternative day off within the following month.

Long service leave of 8.667 weeks becomes available after 10 years of
continuous service, and pro rata thereafter at 4.333 weeks for each further
five years.
""",
    },
    {
        'title': 'Recruitment and Hiring Policy',
        'doc_type': 'policy',
        'department': 'People Operations',
        'version': '2.3',
        'tags': ['recruitment', 'hiring', 'interview', 'scoring', 'referral',
                 'non-discrimination'],
        'content': """
# Recruitment and Hiring Policy

Hiring is the highest-leverage decision the company makes, and the most
exposed to bias. This policy exists to make the process consistent, defensible
and reviewable rather than to make it fast.

## Opening a role

A role is opened only against approved headcount. The hiring manager writes a
job description containing the title, the level, the reporting line, a summary
a candidate outside the industry would understand, between five and eight
responsibilities, and requirements separated into essential and desirable.

Every requirement must be checkable. "Strong communication skills" is not a
requirement; "can explain a technical trade-off in writing to a non-technical
reader" is. Vague requirements are the main cause of inconsistent
shortlisting, because each reviewer silently substitutes their own definition.

The description is reviewed by People Operations before publication for
inclusive language, for salary-range disclosure, and for requirements that
would exclude candidates without being genuinely necessary.

## Stages

Every role follows the same five stages:

1. Application review, against the essential requirements only.
2. Screening call of 30 minutes with People Operations.
3. Technical or functional assessment, timeboxed to two hours.
4. Panel interview of 60 minutes with at least two interviewers.
5. Final conversation with the hiring manager, plus reference checks.

Stages may be removed for a role with the agreement of People Operations, but
not added, and never for one candidate and not another in the same process.

## Scoring

Candidates are scored against the published requirements, one at a time, on a
four-point scale: 1 no evidence, 2 partial evidence, 3 meets, 4 exceeds. Each
score carries a written note naming the evidence from the application,
assessment or interview that supports it. Where the evidence is absent the
score is 1 and the note records a gap; an interviewer may not infer a score
from impression.

Scores are recorded before the panel discusses the candidate. Discussion after
scores are visible produces convergence rather than assessment.

## Non-discrimination

Candidates are assessed solely on the stated requirements of the role. Age,
gender, gender identity, sexual orientation, race, national or ethnic origin,
religion, disability, pregnancy, marital or family status, and political
opinion are irrelevant to every decision and are never recorded, discussed or
inferred.

Reasonable adjustments to any stage are offered proactively and provided
without the candidate having to justify the request. Interview notes are
written on the assumption that the candidate may one day read them.

Unsuccessful candidates are told within five business days of the decision.
Rejections are brief and honest, and do not imply the decision was close when
it was not.

## Referrals

Employees may refer candidates for any open role. A referral bonus of $2,000
is paid once the referred employee completes three months of service, and
$1,500 for a referral into a graduate or internship role. The referring
employee takes no part in the assessment of that candidate and is not shown
the scores.

## Records

Applications, scores, interview notes and the decision rationale are retained
for 12 months after the role closes, and then deleted. Candidates may request
a copy of the information held about them at any time.
""",
    },
    {
        'title': 'Onboarding Process',
        'doc_type': 'process',
        'department': 'People Operations',
        'version': '2.0',
        'tags': ['onboarding', 'induction', 'first week', 'buddy', 'day one'],
        'content': """
# Onboarding Process

A new employee's first month decides how long they stay and how quickly they
become useful. This process names who owns each step, because the common
failure is not that a step is unknown but that everybody assumed somebody else
had it.

## Day minus three: before they arrive

People Operations confirms the start date, sends the welcome pack, and
confirms the first-day agenda. IT provisions the laptop, the email account,
the single sign-on profile and the tool accounts for the role, and ships the
hardware to arrive at least one day before the start date. The hiring manager
writes the 30-day objectives and names a buddy from within the team who is not
the manager.

## Day one

The manager meets the new employee first, in person or on a call, before
anything administrative happens. People Operations runs a 45-minute induction
covering the handbook, leave, expenses, security and how to raise a concern.
IT runs a 30-minute session on access, the password manager and device
encryption.

The buddy takes the new employee to lunch or holds an informal call, and is
their default question channel for the first month. Nothing technical is
expected on day one and no meeting is scheduled after 3.00pm.

## Week one

The manager holds a 30-minute one-to-one on day two and day five. The new
employee reads the team's documentation, sits in on customer or standup calls
as an observer, and completes one genuinely small, genuinely real task by the
end of the week. Shipping something in week one matters more than its size.

People Operations checks on day three that every account works, because access
problems discovered in week three have usually been silently worked around
since week one.

## Week two

The new employee takes on their first properly scoped piece of work with the
buddy reviewing. The manager arranges introductions to the three people
outside the team the role depends on most. Role-specific training is completed:
security awareness for everyone, the brand guidelines for
customer-facing roles, and the engineering standards for engineers.

## Week three

The new employee works at a normal cadence with review rather than
supervision. The manager and the employee review the 30-day objectives at the
midpoint and adjust them, because objectives written before the person arrived
are always slightly wrong.

## Week four

The manager holds the 30-day review: what has gone well, what is still
unclear, what support is needed, and whether the objectives were the right
ones. People Operations holds a separate 20-minute conversation without the
manager present, and asks one question that matters: was anything different
from what you were told during hiring? Discrepancies are fed back into the
recruitment process rather than filed.

The buddy relationship formally ends at week four and usually continues
informally.

## Ownership summary

- People Operations: paperwork, induction, account audit, 30-day check-in.
- IT: hardware, accounts, access, security session.
- Hiring manager: objectives, one-to-ones, introductions, 30-day review.
- Buddy: questions, context, the things nobody writes down.
""",
    },
    {
        'title': 'Performance Review Framework',
        'doc_type': 'process',
        'department': 'People Operations',
        'version': '1.6',
        'tags': ['performance', 'review', 'ratings', 'calibration', 'promotion',
                 'objectives'],
        'content': """
# Performance Review Framework

Performance review exists to give people an accurate picture of how they are
doing and what would change that. It is not a ranking exercise and it is not
the mechanism for managing someone out; both of those uses destroy the honesty
the process depends on.

## Cycles

There are two formal cycles per year:

- The mid-year review, held in June, is developmental. It produces no rating
  and no pay decision.
- The end-of-year review, held in December, produces a rating and feeds the
  pay and promotion decisions taken in February.

Objectives are set in January and revisited at the mid-year review. Between
cycles, feedback happens in the weekly one-to-one; a review that surprises the
employee is a failure of the manager, not a finding of the process.

## What is assessed

Each employee is assessed on two axes, weighted equally. Delivery covers the
objectives agreed for the period, adjusted for changes outside the employee's
control. Behaviour covers how the work was done: collaboration, ownership,
communication and judgement, assessed against the expectations published for
the employee's level.

An employee who delivered everything by working in a way that cost the team
more than it gained has not had a strong year, and the framework is built to
say so.

## Ratings

Five ratings are used:

1. Not meeting expectations at this level.
2. Developing towards the expectations of this level.
3. Consistently meeting the expectations of this level.
4. Exceeding expectations at this level.
5. Performing at the level above.

There is no forced distribution and no quota for any rating. Most people at any
healthy company are rated 3, which is the definition of doing the job well and
is not a disappointing outcome.

## Self-assessment and feedback

Employees write a self-assessment against their objectives, no longer than one
page, and nominate three to five colleagues for peer feedback. Managers request
feedback from those nominees and may add up to two of their own. Peer feedback
is shared with the employee in summarised form; the nominee's name is attached
only with their agreement.

## Calibration

Before ratings are released, managers within a function meet to calibrate.
Each manager presents their proposed ratings with evidence, and the group
tests whether the same evidence would earn the same rating in another team.
Calibration adjusts ratings for consistency of standard; it does not adjust
them to fit a distribution.

Calibration notes are recorded, and a rating changed in calibration is
explained to the employee by their manager with the reason.

## Promotion

Promotion follows a sustained pattern of working at the level above, usually
demonstrated over two cycles, and requires the existence of the role at that
level. A rating of 5 is evidence for promotion, not an entitlement to it, and a
promotion declined for structural reasons is explained plainly rather than
attributed to performance.

## Underperformance

Where performance is not meeting expectations, the manager sets out the gap in
writing, agrees specific measurable improvements, and reviews progress at 30,
60 and 90 days with People Operations involved from the start. Support is
offered before consequences are discussed.
""",
    },
    {
        'title': 'Customer Refund Policy',
        'doc_type': 'policy',
        'department': 'Customer Operations',
        'version': '3.2',
        'tags': ['refund', 'returns', 'refund window', 'exclusions',
                 'processing time', 'payment'],
        'content': """
# Customer Refund Policy

This policy is the authoritative statement of when a customer is entitled to a
refund and how long it takes. Support staff and AI employees answer refund
questions from this document and nothing else; an improvised concession
becomes the company's obligation the moment it is sent to a customer.

## Refund window

Customers may request a refund within 30 calendar days of the delivery date
for physical goods, or within 30 calendar days of the purchase date for
subscriptions and digital licences. The window is counted from delivery, not
from dispatch, and a request submitted on day 30 is in time.

Requests outside the window are declined by default. A support representative
may not grant an out-of-window refund; the Support Lead may approve one where
the delay was caused by the company, by a shipping carrier, or by a documented
illness or bereavement.

## Conditions

An in-window refund is approved where all of the following hold:

- The customer can provide an order number or the email address used to
  purchase.
- Physical goods are returned in a resaleable condition with all accessories
  and, where applicable, the original packaging.
- The account has no outstanding balance or unresolved chargeback.
- For a subscription, the refund covers the current billing period only;
  earlier periods that were used in full are not refunded.

A faulty item is refunded or replaced at the customer's choice regardless of
condition, and the return postage is paid by the company.

## Exclusions

The following are not refundable:

- Digital downloads and licence keys once the download has been accessed or
  the key revealed.
- Personalised, engraved or made-to-order items.
- Gift cards and account credit.
- Items marked "final sale" at the time of purchase.
- Shipping charges, except where the item was faulty, wrongly supplied or
  damaged in transit.

Opened hardware returned in resaleable condition but without its original
packaging is subject to a restocking fee of 10 per cent of the item price.

## Processing time

Once a refund is approved, it is processed back to the original payment method
within 5 business days. The customer's bank or card issuer may take a further
3 to 5 business days to show the credit, and support replies should say so
rather than promising the money will appear immediately.

Refunds are always returned to the original payment method. Where that method
has expired or been closed, the refund is issued as account credit, or by bank
transfer once the customer has confirmed their details through the secure form
rather than by email.

## Partial refunds

A partial refund is appropriate where a customer received part of an order,
where a subscription was unused for a complete billing period following a
service outage, or where a shipment arrived materially late against a
guaranteed delivery date. Partial refunds are calculated pro rata and the
calculation is stated to the customer.

## What is recorded

Every refund records the order reference, the reason code, the amount, who
approved it, and the policy clause relied upon. Reason codes are reviewed
monthly; a rising code is a product signal and is reported to the product
team rather than absorbed by support.
""",
    },
    {
        'title': 'Support SLA and Escalation Policy',
        'doc_type': 'policy',
        'department': 'Customer Operations',
        'version': '2.5',
        'tags': ['sla', 'escalation', 'priority', 'response time',
                 'resolution time', 'support hours'],
        'content': """
# Support SLA and Escalation Policy

This policy states what a customer is promised, what a support representative
is measured against, and exactly when a ticket stops being theirs. Figures in
this document are contractual for customers on a paid plan and may be quoted
directly.

## Support hours

Standard support hours are 9.00am to 6.00pm AEST, Monday to Friday, excluding
public holidays. Priority 1 incidents are handled 24 hours a day, every day of
the year, including public holidays.

A business hour falls inside standard support hours. A ticket raised at 5.30pm
on a Friday with a target of 2 business hours is due at 10.30am on the
following Monday, not at 7.30pm on the Friday.

## Priority definitions

Priority is set from the actual impact on the customer, not from the tone of
the message. A calm report of lost data outranks an angry message about a slow
reply.

- Priority 1, Critical. The service is unavailable, data is being lost or
  corrupted, or a security breach is suspected. Affects all users of an
  account or any user's data integrity. No workaround exists.
- Priority 2, High. A core function is unavailable or substantially degraded
  for multiple users. A workaround exists but is not sustainable.
- Priority 3, Normal. A non-core function is broken, or a core function is
  broken for a single user. A reasonable workaround exists.
- Priority 4, Low. A cosmetic defect, a documentation error, a question, or a
  feature request.

Priority is agreed with the customer where possible and may be raised at any
time. It is lowered only with the customer's explicit agreement, recorded on
the ticket.

## Response and resolution targets

| Priority | First response | Update interval | Resolution target |
|----------|----------------|-----------------|-------------------|
| P1 Critical | 30 minutes, 24/7 | Every 60 minutes | 4 hours |
| P2 High | 2 business hours | Every 4 business hours | 1 business day |
| P3 Normal | 8 business hours | Every business day | 3 business days |
| P4 Low | 2 business days | Every 5 business days | 10 business days |

First response means a human reply from a representative who has read the
ticket. An automated acknowledgement is not a first response. Resolution means
the customer's problem is solved or a permanent workaround is in place and the
customer has confirmed it; a ticket closed without customer confirmation does
not count as resolved.

The clock pauses only while the ticket is genuinely waiting on the customer,
and the pause is recorded with the question that was asked.

## Escalation ladder

A ticket escalates one level at a time, and each escalation is recorded on the
ticket with the reason.

1. Support Representative. Owns every ticket at first contact.
2. Senior Support Representative. Escalate on the first missed target, when
   the issue needs product knowledge beyond the representative, or after two
   customer replies without progress.
3. Support Lead. Escalate on a second missed target, on any customer request
   for a manager, on any out-of-window refund request, and on any P2 open for
   more than one business day.
4. Head of Customer Operations. Escalate on any P1 open for more than 2
   hours, any P2 open for more than 2 business days, any threatened legal
   action, and any complaint naming a named employee.
5. Engineering on-call and the CTO. Escalate on any P1 open for more than 4
   hours, and immediately on any suspected data breach or data loss.

## Immediate handover to a person

The following are handed to a human immediately and never answered by an AI
employee: a suspected data breach, a safety issue, a legal threat, a media
enquiry, a bereavement, and any indication that the customer is vulnerable or
in distress. No draft reply is prepared for these.

## Reporting

Attainment against every target is reported weekly by priority, with each
breach listed individually and its cause named. A breach is a finding to
understand, not a number to improve by reclassifying the ticket.
""",
    },
    {
        'title': 'Product Overview: AI Workforce OS',
        'doc_type': 'product',
        'department': 'Product',
        'version': '1.3',
        'tags': ['product', 'ai workforce os', 'employees', 'approval',
                 'integrations', 'governance'],
        'content': """
# Product Overview: AI Workforce OS

AI Workforce OS is a platform that gives a company a roster of AI employees
rather than a chatbot. Each employee holds a defined job, reaches real company
applications through connected integrations, and works inside a governance
model where anything that leaves the company is approved by a person first.

## What it is, and what it is not

It is not a chat interface with a friendly name. An employee in this platform
is identified by the function it performs, has a system prompt that describes
that job, owns a set of tools that perform real work, keeps memory between
conversations, and produces a task record of what it did.

It is also not an autonomous agent. No employee can send an email, publish a
post, book a meeting or file an issue on its own. Those capabilities exist, and
they stop at a human approval queue.

## The six employees

- People Operations, covering HR and recruitment: job descriptions, candidate
  screening and scoring, interview arrangements, onboarding plans and policy
  answers from the handbook.
- Engineering Delivery, an engineering manager: turning requirements into work
  items, estimating and sequencing them, sprint planning, and status reporting
  that names what is late.
- Software Development, a developer: code, tests and documentation written as
  artefacts for a human developer to review, error and stack-trace analysis,
  and pull-request review.
- Knowledge and Research: searching company documents, comparing sources,
  writing reports with citations, and answering questions put by the other
  employees.
- Marketing and Communications: social posts, blog and advertisement copy,
  campaigns, content calendars and newsletters, with publishing held behind
  approval.
- Customer Support: reading customer requests, drafting replies grounded in
  company policy, triaging and prioritising tickets, and escalating what needs
  a person.

## The approval workflow

Tools come in two kinds. A tool that reads or records company data acts
immediately: creating a job opening, scoring a candidate, searching the
knowledge base, drafting a document. A tool whose effect leaves the company
prepares the action instead of performing it.

A prepared action becomes a pending row carrying its full content and its
complete argument set. A reviewer reads it, edits any field, and approves,
edits or rejects it. Editing does not approve: the row stays pending and a
second deliberate act releases it. The employee's original version is kept
alongside the edited one, so the difference between what was proposed and what
was sent is always recoverable.

## Integrations and demo mode

The platform connects to Gmail, Google Calendar, Google Drive, Slack, Jira,
GitHub, Notion, Confluence, LinkedIn and Instagram. Every connection is
configured through the interface as data; nothing requires a code change or an
environment variable.

Each integration implements every operation twice: once against the real
service and once as a faithful simulation. Where a credential is absent the
simulation runs, and the result is labelled as simulated at every layer -- in
the tool result, the execution record, the audit entry and the interface. A
simulated action is never presented as a real one.

## Grounding and citations

No employee invents a company fact. Product details, policies and figures come
from the knowledge base, retrieved by keyword search over document passages
and returned with the document they came from. Retrieval is deterministic
rather than generated, which is what makes a citation checkable: the passage
either contains the claim or it does not.

## The record

Every tool call writes an audit entry naming the employee, the tool, the
arguments, the outcome and the elapsed time. Every task keeps an ordered list
of the steps taken. Every approval keeps its own trail of who proposed,
edited, approved and executed it.
""",
    },
    {
        'title': 'Brand and Tone of Voice Guidelines',
        'doc_type': 'brand',
        'department': 'Marketing',
        'version': '2.1',
        'tags': ['brand', 'tone of voice', 'writing', 'hashtags', 'claims',
                 'banned words'],
        'content': """
# Brand and Tone of Voice Guidelines

Everything the company publishes sounds like it came from the same
organisation, because it did. These guidelines describe that voice concretely
enough to be checked rather than admired.

## The voice

We are warm but authoritative. Warm means we write to a person, in the second
person, in plain sentences, and we do not perform enthusiasm. Authoritative
means we say what is true, we say what we do not know, and we do not hedge a
clear statement into vagueness to avoid being held to it.

Three tests for any piece of copy:

- Would a competent person in the reader's job find this useful, or only
  flattering?
- Is every claim in it substantiated somewhere we could produce on request?
- Would we be comfortable if a customer quoted this sentence back to us in
  twelve months?

## How we write

Short sentences. Active voice. Specific nouns. One idea per paragraph. British
and Australian spelling throughout: organise, summarise, behaviour, licence as
a noun, licenses as a verb.

Open with something concrete rather than a windup. "Support tickets went from
a four-hour first response to forty minutes" earns the reader's next sentence;
"In today's fast-paced business environment" does not. Close by giving the
reader something to do.

Numbers are written with their source and their basis. "Three times faster"
without saying faster than what is not a claim, it is a decoration.

## Words we do not use

Never: synergy, revolutionary, game-changing, unlock, leverage as a verb,
seamless, effortless, disrupt, ninja, rockstar, cutting-edge, best-in-class,
world-class, delve, elevate.

Avoid: solution when a plainer word exists, utilise for use, in order to for
to, at this point in time for now, reach out for contact or ask.

Never claim a result we have not measured, never name a customer without
written permission, and never describe a feature that is not released as
though it were available.

## Claim substantiation

Any performance figure, comparison, customer result or market statistic must
trace to a document in the knowledge base or to a written customer approval.
Marketing copy is the most likely place in the company for an invented number
to reach the public, so the rule is absolute: if the fact cannot be sourced,
the piece is written without it rather than approximated.

Comparisons naming a competitor require a factual basis, a date, and legal
review before publication.

## Channel notes

- LinkedIn: 120 to 200 words, one point, no more than three hashtags, first
  line must stand alone because the rest is truncated.
- Instagram: 60 to 120 words, concrete opening line, up to five hashtags, no
  hashtag stuffing in the first line.
- Blog: 800 to 1,400 words, one argument, subheadings every 200 words,
  sources linked inline.
- Email: subject line under 50 characters, one call to action, no image-only
  content, plain-text fallback always written.

Five hashtags is the maximum on any channel. Hashtags are words a reader might
search, not a description of the post's mood.

## Visual identity

Primary palette is deep indigo and warm amber on off-white. Body text is set
at 16 pixels minimum with a contrast ratio of at least 4.5 to 1. The logo is
never rotated, recoloured, outlined or set on a busy photograph. Screenshots
are real, never mocked up, and any customer data in them is replaced with
obvious placeholders rather than blurred.
""",
    },
    {
        'title': 'Engineering Standards and Definition of Done',
        'doc_type': 'technical',
        'department': 'Engineering',
        'version': '3.4',
        'tags': ['engineering', 'branching', 'code review', 'testing', 'deploy',
                 'definition of done'],
        'content': """
# Engineering Standards and Definition of Done

These standards describe how code reaches production. They are deliberately
few and deliberately enforced; a long list of aspirational rules that nobody
checks is worse than a short list that gates the merge.

## Branching

Trunk-based development on `main`. Every change is a short-lived branch named
`type/short-description`, where type is one of `feat`, `fix`, `chore`, `docs`
or `refactor`. A branch older than three days is a signal that the change is
too large and should be split.

`main` is always releasable. Direct pushes to `main` are blocked. Merges are
squash merges, and the squash message is the pull-request title, so the commit
history reads as a list of changes rather than a list of keystrokes.

Commit messages use the imperative mood and say why rather than what: "Reject
refund requests outside the 30 day window" rather than "updated views.py".

## Code review

Every change requires one approving review from someone who did not write it,
and two for anything touching authentication, payments, personal data or a
database migration.

A reviewer reports correctness problems first, then genuine simplifications.
Each finding names the file, the line and the concrete failure it causes.
Reviews are not padded with style preferences -- formatting is the formatter's
job -- and findings are not invented to appear thorough. "Nothing blocking" is
a legitimate and useful review outcome.

Review turnaround target is one business day. A review waiting longer than
that is raised in the team channel rather than waited on silently.

## Testing

Every behavioural change ships with tests covering the ordinary path, the
boundaries and the failure cases. A bug fix ships with a test that fails
before the fix and passes after it; without that test the fix is a claim
rather than a change.

Minimum line coverage on changed files is 80 per cent, measured on the diff
rather than the repository, because a repository-wide number can be held up by
code nobody is touching. Tests do not reach the network, do not depend on
wall-clock time, and do not depend on each other's order.

The full suite must pass locally before a pull request is opened. A red build
on `main` is the highest priority work for whoever caused it.

## Definition of done

A work item is done when all of the following are true:

- The acceptance criteria are met and demonstrated.
- Tests are written and the full suite passes.
- The change has been reviewed and approved.
- Documentation affected by the change has been updated in the same pull
  request.
- Any database migration has been reviewed and is reversible, or its
  irreversibility is stated and accepted.
- Logging and error handling exist for the failure paths.
- The change is deployed to staging and verified there.
- The work item is closed with a note of what was actually done, including
  anything deferred.

Anything less is in progress, and reporting it as done is the single most
expensive habit an engineering team can acquire.

## Deployment

Deployment to staging is automatic on merge to `main`. Deployment to
production is a deliberate act, run by a named person during business hours,
never on a Friday afternoon and never during a customer incident.

Every release carries a rollback plan written before the deploy, not
discovered during it. Database migrations are deployed separately from and
ahead of the code that depends on them, so that a rollback of the code does
not require a rollback of the schema.

## Dependencies and secrets

A new third-party dependency requires a note in the pull request covering
licence, maintenance activity and what it saves. Secrets are never committed,
never logged and never placed in a URL; they live in the configuration store
and are read at runtime.
""",
    },
    {
        'title': 'Data Privacy and Security Policy',
        'doc_type': 'policy',
        'department': 'Security',
        'version': '4.0',
        'tags': ['privacy', 'security', 'data classification', 'retention',
                 'breach', 'access'],
        'content': """
# Data Privacy and Security Policy

This policy states how company and customer information is classified,
handled, retained and disposed of, and what happens when something goes
wrong. It applies to every employee and contractor and to every device that
touches company data.

## Data classification

Four classifications, and every piece of information has exactly one:

- Public. Published material: the website, released documentation, press
  statements. No handling restriction.
- Internal. Ordinary company information: plans, drafts, meeting notes,
  metrics. Shared inside the company freely, never outside it without
  approval.
- Confidential. Customer data, employee personal information, financial
  records, contracts, candidate applications, security findings. Accessible
  only to those who need it for their role, and never copied to personal
  accounts, personal devices or unapproved tools.
- Restricted. Authentication credentials, payment card data, encryption keys,
  government identifiers and health information. Access is individually
  granted, individually logged, and reviewed quarterly.

When the classification is unclear, treat the information as Confidential and
ask.

## Access

Access follows least privilege: a role receives the minimum access it needs,
and access is granted to roles rather than to people. Every account uses
single sign-on with multi-factor authentication; shared accounts are
prohibited. Production database access is read-only by default and any
write access is time-limited, justified in writing and logged.

Access is reviewed quarterly, and removed the same day someone changes role or
leaves. IT confirms revocation in writing to People Operations.

## Handling rules

Company data is stored in approved systems only. Personal cloud storage,
personal email, unapproved AI tools and personal messaging apps are not
approved systems. Customer personal information is never pasted into a tool
that has not been assessed.

Devices are encrypted at rest, locked after five minutes idle, and patched
within 14 days of a security update being released, or within 48 hours for a
critical vulnerability. Lost or stolen devices are reported to IT
immediately, at any hour.

Personal information is never placed in a URL, a query string or a log line,
and is never used as a test fixture. Test environments use synthetic data.

## Retention

- Customer account and transaction records: 7 years after the account closes,
  as required for tax and accounting.
- Support tickets and their attachments: 3 years after closure.
- Candidate applications, scores and interview notes: 12 months after the
  role closes.
- Employee records: 7 years after employment ends.
- Server and application logs: 90 days, then deleted.
- Marketing contact records: until consent is withdrawn, and reviewed every 24
  months for continued relevance.

Deletion at the end of a retention period is automatic and verified annually.
A customer request for deletion is actioned within 30 calendar days, and the
data that must be kept for a legal obligation is named to the customer rather
than left unexplained.

## Breach process

A suspected breach is reported to the security lead immediately, before any
attempt to investigate or remediate it. Nobody is penalised for reporting
something that turns out to be nothing, and a report that arrives late because
somebody hoped it was nothing is the outcome this rule exists to prevent.

1. Report immediately, in the security channel or by phone.
2. The security lead confirms and contains within 1 hour of the report.
3. Assessment of what data, whose data and how much, within 24 hours.
4. Notification to the regulator within 72 hours of becoming aware, where the
   breach is likely to result in serious harm.
5. Notification to affected individuals as soon as practicable, in plain
   language, saying what happened, what it means for them and what to do.
6. A written post-incident review within 10 business days, naming the cause
   and the systemic change, not the person.

No employee and no AI employee drafts a customer communication about a
suspected breach. That work belongs to the security lead and legal counsel.
""",
    },
    {
        'title': 'Troubleshooting Guide: Common Customer Issues',
        'doc_type': 'troubleshooting',
        'department': 'Customer Operations',
        'version': '1.4',
        'tags': ['troubleshooting', 'common issues', 'login', 'refund', 'billing',
                 'sync', 'export'],
        'content': """
# Troubleshooting Guide: Common Customer Issues

The six issues below account for roughly three quarters of inbound tickets.
Each entry gives the symptom as the customer describes it, the cause we
usually find, and the resolution. Where a policy applies, the policy governs;
this guide is a working aid and not a substitute for it.

## Symptom: cannot sign in, password reset email never arrives

Cause. In most cases the address entered at sign-in is not the address on the
account -- typically a work address where the account was created with a
personal one, or the reverse. The second most common cause is the reset email
being filtered into a spam or quarantine folder by the customer's mail
provider.

Resolution. Confirm the exact address on the account by order reference rather
than by asking the customer to guess again. Ask them to check spam and, on
corporate mail, the quarantine digest. Trigger a fresh reset while they are on
the ticket, since the link expires after 60 minutes. If nothing arrives within
15 minutes, check the delivery log for a bounce and escalate to Senior Support
if the address is on a suppression list.

## Symptom: charged twice for the same subscription period

Cause. Almost always a card retry after an initial decline, where the first
attempt eventually settled. Occasionally a genuine duplicate caused by the
customer resubscribing before the earlier cancellation processed.

Resolution. Check the billing history for two charges of the same amount
within 72 hours. Where a retry settled twice, refund the later charge in full
under the Customer Refund Policy and confirm the next billing date. Where the
customer holds two active subscriptions, cancel the newer one and refund it in
full regardless of the refund window, because the duplicate is our fault.

## Symptom: refund approved but the money has not arrived

Cause. The customer is counting calendar days, or is watching a different card
from the one used to pay. Where the payment method has expired, the refund
will have failed silently at the processor.

Resolution. Confirm the approval date and the last four digits of the original
card. Explain that the refund is processed to the original payment method
within 7 business days, and that the card issuer may take a further 3 to 5
business days to display the credit. Where the method has expired, arrange
account credit or a bank transfer through the secure form -- never collect bank
details over email. If the processor shows the refund as failed, escalate to
Senior Support the same day.

## Symptom: integration stopped syncing, no error shown

Cause. An expired or revoked OAuth token, usually because the person who
connected the integration changed their password, left the customer's
organisation, or had their access removed.

Resolution. Check the connection status in the customer's admin panel; a token
error appears there even when the dashboard looks normal. Ask the customer to
reconnect the integration from an account that will remain active, and
recommend a service account rather than an individual's. Data queued during
the outage backfills within one hour of reconnection; anything older than 30
days must be re-imported manually.

## Symptom: export is empty or truncated

Cause. A filter left applied from the previous session, or an export requested
across a date range with no records in it. Truncation at exactly 10,000 rows
is the export limit rather than a fault.

Resolution. Ask for a screenshot of the filter bar before assuming a defect.
Where the row count is exactly 10,000, explain the limit and show how to split
the export by month. Where the file is genuinely empty for a range with known
records, capture the range, the filters and the account id, and raise a P3
with engineering.

## Symptom: slow performance for one user only

Cause. Nearly always local: an outdated browser, an extension injecting into
the page, or a corporate proxy adding latency. A single affected user is the
strongest evidence against a platform fault.

Resolution. Confirm the browser version, ask the customer to reproduce in a
private window with extensions disabled, and compare against a colleague on
the same account. If the problem persists across browsers and machines on the
same account, it is no longer a single-user issue: raise a P2 with the account
id and the affected endpoints.
""",
    },
]


__all__ = [
    'SearchHit', 'index_document', 'reindex', 'reindex_all', 'chunk_text',
    'search', 'search_documents', 'term_coverage', 'get_document', 'list_documents',
    'document_stats', 'detect_conflicts', 'summarise', 'compare_documents',
    'citations_for', 'keywords_of', 'ensure_seed_documents', 'tokenise',
    'prose_only',
    'content_terms', 'checksum_of',
]
