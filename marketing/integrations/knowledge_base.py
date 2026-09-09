"""The Company Knowledge Base: the one integration that is always live.

WHY THIS IS AN INTEGRATION AT ALL
---------------------------------
There is no external service behind this connector. It wraps the platform's
own retrieval engine, ``marketing/knowledge.py``, over the documents the
company has indexed -- Notion pages, Confluence pages, GitHub markdown,
uploaded files and anything an employee has written back. It is modelled as an
integration anyway so that the workforce reaches its own memory through
exactly the same door it reaches Gmail or Jira: one ``call``, one
``CallResult``, one audit entry, one place where usage is counted.

WHY IT HAS NO DEMO HALF
-----------------------
Demo mode exists because most services will not answer without a credential
that takes days to obtain. That reasoning does not apply here: the data is
ours, the engine is in this repository, and a search either finds the
company's documents or honestly finds nothing. Simulating a search over our own
documents would mean inventing citations, which is precisely the failure this
integration exists to prevent. Every result from this connector therefore
carries ``demo=False``, in demo mode as much as in live mode, and ``call`` is
overridden to say so rather than quietly falling through to a simulation.

CITATIONS ARE THE POINT
-----------------------
Every result's ``data`` carries a ``citations`` list. An answer that cites the
document it came from can be checked; one that cannot is a guess wearing a
confident tone. When ``require_citations`` is on and a search returns nothing,
that is reported as nothing found rather than dressed up, because the correct
behaviour for an employee with no source is to say it does not know.

WHICH EMPLOYEES USE IT
----------------------
``Research``     the primary user: searches, reads, indexes new documents and
                 reports conflicts between them.
``Every other``  through delegation. The Marketing employee asking for product
                 facts, the Support employee checking policy and the HR
                 employee quoting a handbook all end up here, which is why the
                 citations travel back with the answer.

CONFIGURATION
-------------
Nothing needs configuring for this to work, so ``is_configured`` is always
true. The settings are behavioural rather than credentials: how many results a
search returns, how weak a match may be before it is dropped, whether an
answer must carry a citation, and whether an uploaded file is indexed
automatically.

WHAT HAPPENS IF THE ENGINE IS MISSING
-------------------------------------
``marketing/knowledge.py`` is imported inside each method rather than at
module level. These connector modules are imported while Django is still
loading its applications, so a top-level import of anything that touches the
models would break start-up. The import is also guarded, so a missing or
broken engine produces a clean failure result on the one call that needed it
instead of preventing the whole platform from starting.
"""

from . import register
from .base import CallResult, ConfigField, Connector


@register
class KnowledgeBaseConnector(Connector):
    """Search, read and index the company's own documents."""

    key = 'knowledge_base'
    name = 'Company Knowledge Base'
    description = ('Searches and indexes the company\'s own documents, and '
                   'returns a citation with every answer.')
    category = 'internal'
    icon = 'fa-book-open'
    color = '#7c3aed'

    config_fields = (
        ConfigField(
            'result_limit', 'Results per search',
            help_text=('How many passages a search returns by default. Eight '
                       'is enough to answer a question and few enough to read.'),
            field_type='number', default=8),
        ConfigField(
            'min_score', 'Minimum relevance',
            help_text=('Matches weaker than this are dropped. Raise it if '
                       'searches return loosely related passages; lower it if '
                       'they return nothing.'),
            field_type='number', default=0.05),
        ConfigField(
            'require_citations', 'Require a citation for every answer',
            help_text=('When on, an employee that finds no source says so '
                       'rather than answering from its own general knowledge.'),
            field_type='boolean', default=True),
        ConfigField(
            'auto_index_uploads', 'Index uploaded files automatically',
            help_text=('When on, a document uploaded anywhere in the platform '
                       'is indexed and becomes searchable immediately.'),
            field_type='boolean', default=True),
    )

    operations = ('search', 'get_document', 'list_documents', 'index_document',
                  'stats', 'detect_conflicts', 'reindex')

    # -- the engine --------------------------------------------------------

    def _engine(self):
        """The retrieval engine, imported late and deliberately.

        Late because this module is imported while Django is still loading
        applications, and the engine reaches the models.
        """
        from .. import knowledge
        return knowledge

    # -- settings ----------------------------------------------------------

    def is_configured(self):
        """Always true. There is nothing to configure for this to work."""
        return True

    def _limit(self, given=None):
        if given:
            try:
                return max(1, min(int(given), 50))
            except (TypeError, ValueError):
                pass
        try:
            return max(1, min(int(self.setting('result_limit', 8)), 50))
        except (TypeError, ValueError):
            return 8

    def _min_score(self, given=None):
        value = given if given not in (None, '') else self.setting('min_score', 0.05)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.05

    def _requires_citations(self):
        value = self.setting('require_citations', True)
        if isinstance(value, str):
            return value.strip().lower() not in ('false', '0', 'no', 'off', '')
        return bool(value)

    # -- dispatch ----------------------------------------------------------

    def call(self, operation, **kwargs):
        """Always live, because the data is ours.

        The base class would fall back to a simulation when an integration is
        in demo mode or when an operation has no live half. Neither is right
        here: there is no credential to be missing, and a simulated search over
        our own documents would mean inventing citations. A disabled
        integration is still an error rather than a simulation, exactly as it is
        everywhere else.
        """
        if not self.integration.is_enabled:
            return self._finish(CallResult(
                ok=False, provider=self.key,
                error=('The Company Knowledge Base is switched off in '
                       'Integrations. Nothing in the platform can search '
                       'company documents until it is switched back on.')),
                operation)

        handler = getattr(self, f'live_{operation}', None)
        if handler is None:
            return self._finish(self.failure(
                f'The knowledge base has no operation named {operation!r}. '
                f'Available operations: {", ".join(self.operations)}.'),
                operation)

        try:
            result = handler(**kwargs)
        except ImportError as exc:
            result = self.failure(
                'The retrieval engine (marketing/knowledge.py) could not be '
                f'imported, so nothing can be searched or indexed: {exc}. This '
                'is a code fault rather than a configuration one.')
        except Exception as exc:  # noqa: BLE001 -- one bad call must not stop the platform
            result = self.failure(
                f'Knowledge base call failed: {exc or type(exc).__name__}')
        return self._finish(result, operation)

    def probe(self):
        """Report the real document and chunk counts.

        There is no connection to test, so the useful check is whether there is
        anything in the index at all. An empty knowledge base is not a failure,
        but it is worth saying plainly, because an employee with nothing to cite
        will decline to answer and that looks like a fault.
        """
        if not self.integration.is_enabled:
            return ('disabled', 'The Company Knowledge Base is switched off.')
        try:
            stats = self._engine().document_stats() or {}
        except ImportError as exc:
            return ('failed', f'The retrieval engine could not be imported: {exc}')
        except Exception as exc:  # noqa: BLE001
            return ('failed', f'The knowledge base could not be read: {exc}')

        documents = _first(stats, ('documents', 'document_count', 'total_documents'), 0)
        chunks = _first(stats, ('chunks', 'chunk_count', 'total_chunks'), 0)
        if not documents:
            return ('degraded',
                    ('The knowledge base is working but empty: 0 documents and '
                     '0 passages indexed. Index a document, or run a Notion, '
                     'Confluence or GitHub sync, and the employees will have '
                     'something to cite.'))
        return ('connected',
                (f'{documents} document(s) and {chunks} passage(s) indexed, '
                 'searchable now. Every answer carries a citation.'))

    # =======================================================================
    # Operations -- all live, because the data is ours
    # =======================================================================

    def live_search(self, query='', limit=None, doc_type='', department='',
                    source_kind='', min_score=None):
        if not str(query).strip():
            return self.failure(
                'What should be searched for? Pass query -- a question or a few '
                'words from the document you are looking for.')

        engine = self._engine()
        wanted = self._limit(limit)
        floor = self._min_score(min_score)
        hits = engine.search(
            str(query).strip(), limit=wanted, doc_type=str(doc_type or ''),
            department=str(department or ''), source_kind=str(source_kind or ''),
            min_score=floor) or []

        citations = _citations(engine, hits)
        rows = [_hit_row(hit) for hit in hits]

        if not rows:
            advice = ('Nothing in the company knowledge base matches '
                      f'{str(query).strip()!r} above a relevance of {floor}. '
                      'That is a real answer, not an error: an employee with no '
                      'source should say it does not know.')
            if self._requires_citations():
                advice += (' Citations are required, so this question should be '
                           'answered by saying the company has nothing written '
                           'about it.')
            return self.ok(advice, {
                'query': str(query).strip(), 'count': 0, 'results': [],
                'citations': [], 'require_citations': self._requires_citations(),
                'min_score': floor,
                'filters': {'doc_type': str(doc_type or ''),
                            'department': str(department or ''),
                            'source_kind': str(source_kind or '')}})

        titles = []
        for row in rows:
            if row['title'] and row['title'] not in titles:
                titles.append(row['title'])
        return self.ok(
            (f'{len(rows)} passage(s) for {str(query).strip()!r} from '
             f'{len(titles)} document(s): {", ".join(titles[:5])}'
             f'{" and others" if len(titles) > 5 else ""}. '
             f'Best match scored {rows[0]["score"]}.'),
            {'query': str(query).strip(), 'count': len(rows), 'results': rows,
             'citations': citations, 'documents': titles,
             'top_score': rows[0]['score'], 'min_score': floor,
             'require_citations': self._requires_citations(),
             'filters': {'doc_type': str(doc_type or ''),
                         'department': str(department or ''),
                         'source_kind': str(source_kind or '')}})

    def live_get_document(self, document_id=None):
        if document_id in (None, '', 0):
            return self.failure('Which document? Pass document_id.')
        engine = self._engine()
        document = engine.get_document(document_id)
        if document is None:
            return self.failure(
                f'No document with id {document_id!r} is in the knowledge base. '
                'Use list_documents or search to find the right id.',
                {'document_id': document_id, 'citations': []})

        row = _document_row(document)
        content = getattr(document, 'content', '') or ''
        row['content'] = content
        row['words'] = len(str(content).split())
        return self.ok(
            (f'{row["title"]} ({row["doc_type"] or "no type"}, '
             f'{row["words"]} word(s))'
             f'{" -- " + row["url"] if row["url"] else ""}'),
            {'document': row, 'citations': [_citation(document)]})

    def live_list_documents(self, doc_type='', department='', source=None,
                            limit=50, query=''):
        engine = self._engine()
        try:
            capped = max(1, min(int(limit or 50), 200))
        except (TypeError, ValueError):
            capped = 50
        documents = engine.list_documents(
            doc_type=str(doc_type or ''), department=str(department or ''),
            source=source, limit=capped, query=str(query or '')) or []

        rows = [_document_row(item) for item in documents]
        kinds = {}
        for row in rows:
            kinds[row['doc_type'] or 'unclassified'] = (
                kinds.get(row['doc_type'] or 'unclassified', 0) + 1)
        shape = ', '.join(f'{count} {name}' for name, count in sorted(kinds.items()))
        return self.ok(
            f'{len(rows)} document(s) in the knowledge base'
            f'{f" ({shape})" if shape else ""}.',
            {'count': len(rows), 'documents': rows, 'by_type': kinds,
             'citations': [_citation(item) for item in documents],
             'filters': {'doc_type': str(doc_type or ''),
                         'department': str(department or ''),
                         'query': str(query or '')}})

    def live_index_document(self, title='', content='', doc_type='other',
                            source=None, external_id='', url='', tags=None,
                            department='', author='', version='1',
                            created_by=None, created_by_agent=None):
        if not str(title).strip():
            return self.failure('A document needs a title to be findable.')
        if not str(content or '').strip():
            return self.failure(
                'A document needs content. Indexing an empty document adds a '
                'title that can be found and then cited for nothing.')

        engine = self._engine()
        document = engine.index_document(
            title=str(title).strip(), content=str(content),
            doc_type=str(doc_type or 'other'), source=source,
            external_id=str(external_id or ''), url=str(url or ''),
            tags=list(tags or []), department=str(department or ''),
            author=str(author or ''), version=str(version or '1'),
            created_by=created_by, created_by_agent=created_by_agent)

        row = _document_row(document)
        chunks = getattr(document, 'chunk_count', None)
        if chunks is None:
            chunks = len(getattr(document, 'chunks', []) or [])
        return self.ok(
            (f'Indexed {row["title"]!r} as {row["doc_type"] or "other"}, '
             f'{len(str(content).split())} word(s) in '
             f'{chunks or "an unreported number of"} passage(s). It is '
             'searchable now and can be cited.'),
            {'document': row, 'chunks': chunks,
             'words': len(str(content).split()),
             'citations': [_citation(document)]})

    def live_stats(self):
        engine = self._engine()
        stats = engine.document_stats() or {}
        documents = _first(stats, ('documents', 'document_count', 'total_documents'), 0)
        chunks = _first(stats, ('chunks', 'chunk_count', 'total_chunks'), 0)
        if not documents:
            return self.ok(
                ('The knowledge base is empty: 0 documents and 0 passages. Run '
                 'a Notion, Confluence or GitHub sync, or index a document '
                 'directly, and the employees will have something to cite.'),
                {'documents': 0, 'chunks': 0, 'stats': _plain(stats),
                 'citations': []})
        # _plain, because document_stats carries a datetime and this dict ends
        # up in a JSONField on the audit trail.
        return self.ok(
            (f'{documents} document(s) and {chunks} passage(s) indexed. '
             'Every answer drawn from them carries a citation.'),
            {'documents': documents, 'chunks': chunks, 'stats': _plain(stats),
             'require_citations': self._requires_citations(),
             'citations': []})

    def live_detect_conflicts(self, query='', hits=None, limit=None):
        """Find passages that disagree with each other.

        Two documents both saying what the retention period is, differently, is
        the single most damaging thing a knowledge base can contain, because
        both answers look sourced. Either the caller passes hits already in
        hand, or a search is run first so there is something to compare.
        """
        engine = self._engine()
        found = hits
        if not found:
            if not str(query).strip():
                return self.failure(
                    'Pass either query, so a search can be run first, or hits '
                    'from an earlier search to examine.')
            found = engine.search(str(query).strip(), limit=self._limit(limit),
                                  min_score=self._min_score()) or []

        conflicts = engine.detect_conflicts(found) or []
        citations = _citations(engine, found)
        if not conflicts:
            return self.ok(
                (f'No contradictions found across {len(found)} passage(s)'
                 f'{f" for {str(query).strip()!r}" if str(query).strip() else ""}. '
                 'The sources agree, or are about different enough things not to '
                 'disagree.'),
                {'query': str(query or ''), 'examined': len(found),
                 'conflicts': [], 'count': 0, 'citations': citations})
        return self.ok(
            (f'{len(conflicts)} possible contradiction(s) across '
             f'{len(found)} passage(s). Documents that disagree with each other '
             'should be reconciled before either is quoted to anyone.'),
            {'query': str(query or ''), 'examined': len(found),
             'conflicts': _plain(conflicts), 'count': len(conflicts),
             'citations': citations})

    def live_reindex(self):
        engine = self._engine()
        outcome = engine.reindex_all()
        if isinstance(outcome, dict):
            documents = _first(outcome, ('documents', 'document_count',
                                         'reindexed', 'total_documents'), 0)
            chunks = _first(outcome, ('chunks', 'chunk_count', 'total_chunks'), 0)
            detail = dict(outcome)
        else:
            documents = outcome if isinstance(outcome, int) else 0
            chunks = 0
            detail = {'result': _plain(outcome)}
        detail = _plain(detail)
        return self.ok(
            (f'Rebuilt the search index over {documents or "every"} document(s)'
             f'{f", {chunks} passage(s)" if chunks else ""}. Anything indexed '
             'before is searchable under the current settings.'),
            {'documents': documents, 'chunks': chunks, 'detail': detail,
             'citations': []})


# ===========================================================================
# Shaping engine objects into plain data
# ===========================================================================

def _first(mapping, keys, default=None):
    """The first present key of several, since stats keys vary by engine build."""
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return default


def _document_row(document):
    return {
        'id': getattr(document, 'id', None),
        'title': getattr(document, 'title', '') or 'Untitled',
        'doc_type': getattr(document, 'doc_type', '') or '',
        'url': getattr(document, 'url', '') or '',
        'summary': getattr(document, 'summary', '') or '',
        'department': getattr(document, 'department', '') or '',
        'author': getattr(document, 'author', '') or '',
        'version': str(getattr(document, 'version', '') or ''),
        'external_id': getattr(document, 'external_id', '') or '',
        'tags': list(getattr(document, 'tags', None) or []),
    }


def _citation(document):
    """One traceable reference: what it is and where to read it."""
    return {
        'document_id': getattr(document, 'id', None),
        'title': getattr(document, 'title', '') or 'Untitled',
        'doc_type': getattr(document, 'doc_type', '') or '',
        'url': getattr(document, 'url', '') or '',
        'summary': getattr(document, 'summary', '') or '',
    }


def _hit_row(hit):
    """One retrieved passage as plain data.

    ``SearchHit`` already knows how to describe itself, and its own version
    carries the citation label and the heading, so that is preferred. The
    manual shaping below is the fallback for anything that does not.
    """
    if hasattr(hit, 'as_dict'):
        try:
            row = _plain(hit.as_dict())
            row.setdefault('score', round(float(getattr(hit, 'score', 0) or 0), 4))
            row.setdefault('title', 'Untitled')
            return row
        except Exception:  # noqa: BLE001 -- fall back to reading the fields
            pass
    document = getattr(hit, 'document', None)
    row = {
        'score': round(float(getattr(hit, 'score', 0) or 0), 4),
        'snippet': getattr(hit, 'snippet', '') or '',
        'matched_terms': list(getattr(hit, 'matched_terms', None) or []),
        'why': getattr(hit, 'why', '') or '',
        'chunk': _plain(getattr(hit, 'chunk', '')),
        'document_id': getattr(document, 'id', None) if document else None,
        'title': (getattr(document, 'title', '') or 'Untitled') if document else 'Untitled',
        'doc_type': (getattr(document, 'doc_type', '') or '') if document else '',
        'url': (getattr(document, 'url', '') or '') if document else '',
        'summary': (getattr(document, 'summary', '') or '') if document else '',
    }
    return row


def _citations(engine, hits):
    """Citations for a set of hits, preferring the engine's own formatter."""
    if not hits:
        return []
    try:
        produced = engine.citations_for(hits)
    except Exception:  # noqa: BLE001 -- a citation helper must never break a search
        produced = None
    if produced:
        return _plain(produced)
    seen, out = set(), []
    for hit in hits:
        document = getattr(hit, 'document', None)
        if document is None:
            continue
        identifier = getattr(document, 'id', None)
        if identifier in seen:
            continue
        seen.add(identifier)
        out.append(_citation(document))
    return out


def _plain(value, depth=0):
    """Anything the engine returns, reduced to JSON-safe data.

    A CallResult travels into a JSONField on the audit trail, so a model
    instance or a dataclass in ``data`` would fail to serialise. Depth is
    capped because an engine object may hold a reference back to something that
    holds it.
    """
    if depth > 4:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item, depth + 1) for item in value]
    for attribute in ('text', 'content', 'title'):
        if hasattr(value, attribute):
            return str(getattr(value, attribute) or '')
    return str(value)
