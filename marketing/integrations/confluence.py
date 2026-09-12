"""Confluence: the formal documentation space, read as prose.

WHAT THIS DOES FOR THE WORKFORCE
--------------------------------
Where Notion holds a company's informal writing, Confluence usually holds its
formal writing: the architecture page procurement asks about, the runbook the
on-call engineer follows, the decision record that explains why the system is
shaped the way it is. This integration exists so that when an AI employee
answers a question about how something works, the answer comes from the page
somebody wrote and is cited back to it.

The awkward part of Confluence is its body format. A page is stored as
"storage format", which is XHTML with Atlassian's own macro elements mixed in.
Stripping tags with a regular expression produces nonsense the moment a page
contains a table of contents macro or a structured layout, and nonsense in the
knowledge base is worse than nothing. ``_html_to_text`` therefore uses
``html.parser.HTMLParser`` from the standard library, keeps block structure by
emitting a newline for paragraphs, line breaks, list items and headings, drops
the content of script, style and macro-parameter elements, and unescapes
entities. The result reads like the page.

WHICH EMPLOYEES USE IT
----------------------
``Research``     searches with CQL, reads pages, and indexes them with a
                 citation to the page URL and version.
``Engineering``  reads the runbook and the release process before proposing an
                 operational change, and writes a decision record after one.
``Support``      reads the customer-facing standards pages so that what it
                 tells a customer matches published policy.
``Operations``   publishes and updates process pages.

THE CREDENTIAL YOU NEED
-----------------------
The same kind of credential as Jira, because it is the same account system:
HTTP Basic with an email address and an Atlassian API token.

  1. Create a token at id.atlassian.com/manage-profile/security/api-tokens.
  2. On the Integrations page set Site URL to your wiki root, including the
     ``/wiki`` path -- https://yourcompany.atlassian.net/wiki -- plus the
     account email and the token.
  3. Set the default space key (for example DOCS) so an employee need not name
     the space on every call.

The token carries that account's permissions. A space the account cannot read
by hand is not readable here either.

VERSIONS, AND WHY UPDATE READS FIRST
------------------------------------
Confluence uses optimistic locking on page updates: an update must state the
version number it is producing, and it must be exactly one higher than the
current one. Sending anything else is a 409. ``update_page`` therefore always
reads the current version first and increments it, which also means a page
edited by a person between the read and the write fails loudly rather than
silently overwriting their work.

WHAT DEMO MODE DOES INSTEAD
---------------------------
Without a site, email and token every operation is simulated against eight
fixture pages in two spaces, DOCS and ENG -- a system architecture page, a
runbook, the release process, data retention, API standards, a testing
strategy, a glossary and a decision record -- each written as real technical
prose with stable ids. Every simulated result is flagged.
"""

import base64
import urllib.parse
import hashlib
import html
import urllib.error
from html.parser import HTMLParser

from . import register
from .base import CallResult, ConfigField, Connector

# Elements whose text content is markup plumbing rather than page content.
SKIP_CONTENT = {'script', 'style', 'ac:parameter', 'ri:attachment', 'ri:page'}
BLOCK_TAGS = {'p', 'br', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
              'blockquote', 'pre', 'table', 'ul', 'ol', 'section', 'hr',
              'ac:structured-macro', 'ac:rich-text-body'}
# Cells are separated by a space rather than a newline, so a short table still
# reads as a row.
CELL_TAGS = {'td', 'th'}
HEADING_TAGS = {'h1': '#', 'h2': '##', 'h3': '###',
                'h4': '####', 'h5': '#####', 'h6': '######'}


class _StorageFormatReader(HTMLParser):
    """Turn Confluence storage format into readable plain text.

    A regular expression cannot do this job. A page with a table of contents
    macro, a layout section or an info panel carries nested Atlassian elements
    whose attributes contain text; strip the angle brackets and the output is
    a soup of macro parameters. Parsing properly means the macro plumbing can
    be skipped deliberately and the prose kept.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0
        self._pending_heading = ''

    def handle_starttag(self, tag, attrs):
        name = tag.lower()
        if name in SKIP_CONTENT:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if name in HEADING_TAGS:
            self._newline()
            self._pending_heading = HEADING_TAGS[name]
            self._parts.append(f'{self._pending_heading} ')
        elif name == 'li':
            self._newline()
            self._parts.append('- ')
        elif name in CELL_TAGS:
            # A row must not read as one run-together word, and a newline per
            # cell would turn a small table into a column of fragments.
            self._space()
        elif name in BLOCK_TAGS:
            self._newline()

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == 'br' and not self._skip_depth:
            self._newline()

    def handle_endtag(self, tag):
        name = tag.lower()
        if name in SKIP_CONTENT:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if name in HEADING_TAGS:
            self._pending_heading = ''
            self._newline()
        elif name in CELL_TAGS:
            self._space()
        elif name in BLOCK_TAGS:
            self._newline()

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.replace('\xa0', ' ')
        if not text.strip():
            self._space()
            return
        # Whitespace at the edges of a data chunk is what separates text either
        # side of an inline tag: 'the person <strong>on call</strong>' arrives as
        # two chunks, and collapsing the trailing space would join the words.
        if text[:1].isspace():
            self._space()
        self._parts.append(' '.join(text.split()))
        if text[-1:].isspace():
            self._space()

    def _space(self):
        if self._parts and not self._parts[-1].endswith((' ', '\n')):
            self._parts.append(' ')

    def _newline(self):
        if self._parts and self._parts[-1] != '\n':
            self._parts.append('\n')

    def text(self):
        joined = ''.join(self._parts)
        lines = [' '.join(line.split()) for line in joined.split('\n')]
        cleaned, blank = [], False
        for line in lines:
            if line:
                cleaned.append(line)
                blank = False
            elif not blank and cleaned:
                cleaned.append('')
                blank = True
        return '\n'.join(cleaned).strip()


def _html_to_text(markup):
    """Confluence storage format as plain text, block structure preserved."""
    if not markup:
        return ''
    reader = _StorageFormatReader()
    try:
        reader.feed(str(markup))
        reader.close()
    except Exception:  # noqa: BLE001 -- a malformed page must still yield something
        return html.unescape(str(markup))[:20000]
    return reader.text()


def _text_to_storage(body):
    """Plain text as simple storage-format markup.

    Deliberately plain: paragraphs and nothing else. Generating richer markup
    from text means guessing, and a guess that produces invalid storage format
    is rejected by Confluence with an unhelpful message.
    """
    lines = [line.strip() for line in str(body or '').replace('\r\n', '\n').split('\n')]
    parts = []
    for line in lines:
        if line:
            parts.append(f'<p>{html.escape(line)}</p>')
    return ''.join(parts) or '<p></p>'


def _escape_cql(text):
    """Escape a value for use inside a double-quoted CQL string.

    Backslashes first, then quotes, or the escaping escapes itself.
    """
    return str(text or '').replace('\\', '\\\\').replace('"', '\\"')


def _demo_id(slug):
    digest = hashlib.sha256(f'confluence:{slug}'.encode()).hexdigest()
    return str(3000000 + int(digest[:6], 16) % 900000)


# ===========================================================================
# The demo wiki -- eight pages of technical prose in two spaces
# ===========================================================================

DEMO_SPACES = (
    {'key': 'DOCS', 'name': 'Product Documentation', 'type': 'global'},
    {'key': 'ENG', 'name': 'Engineering', 'type': 'global'},
)

DEMO_PAGES = (
    {'slug': 'system-architecture', 'space': 'ENG', 'version': 14,
     'title': 'System architecture',
     'author': 'Priya Nair', 'created': '2026-02-18T09:00:00.000+1000',
     'updated': '2026-08-27T15:10:00.000+1000',
     'content': (
         '# System architecture\n'
         'The platform is a single Django project with one application module '
         'per business domain and one Postgres database. It is a modular '
         'monolith by choice. With seven engineers, the daily cost of a network '
         'hop between modules that change together outweighs the occasional '
         'cost of a shared deployment.\n'
         '## Layers\n'
         'Views validate input and render. Service modules hold business rules '
         'and are the only place a rule is enforced, so the same rule applies '
         'whether the caller is a browser, the public API or a worker. Models '
         'hold persistence and nothing else.\n'
         '## Outbound traffic\n'
         'Every call to a third party goes through the connector layer. Nothing '
         'else in the codebase may open a socket to a vendor. That single '
         'chokepoint is what makes it possible to record every external effect, '
         'to simulate one when a credential is missing, and to rate limit in one '
         'place rather than seven.\n'
         '## State\n'
         'Postgres is the only durable store. Redis holds cache and queue and is '
         'treated as disposable: nothing in Redis is the only copy of anything. '
         'Every queued task must be safe to run twice, because at-least-once is '
         'the guarantee the queue actually offers.\n'
         '## Known weaknesses\n'
         'The settings module concentrates environment differences in nested '
         'conditionals and has been implicated in every deployment incident this '
         'year. The export path buffers whole files in memory. Both are being '
         'addressed.\n')},
    {'slug': 'production-runbook', 'space': 'ENG', 'version': 22,
     'title': 'Production runbook',
     'author': 'Dan Kovac', 'created': '2026-01-22T08:30:00.000+1000',
     'updated': '2026-09-01T11:45:00.000+1000',
     'content': (
         '# Production runbook\n'
         'For the person on call. Read the whole of the relevant section before '
         'typing anything.\n'
         '## Declare first, investigate second\n'
         'Say in the incident channel what you are seeing and that you are '
         'looking at it. The cost of declaring an incident that turns out to be '
         'nothing is a message; the cost of not declaring one is that nobody '
         'else knows to help.\n'
         '## High error rate\n'
         'Check the deployment history first: most error spikes follow a '
         'release by minutes. If a release is implicated, redeploy the previous '
         'tag rather than diagnosing under pressure. Migrations are backward '
         'compatible by policy, so the previous code runs correctly against the '
         'current schema.\n'
         '## Database saturation\n'
         'Look for a long-running query before assuming load. A single '
         'unindexed report query holding a connection pool is far more common '
         'than genuine capacity exhaustion. Cancel the query, note it, and open '
         'a ticket rather than raising instance size.\n'
         '## Queue backlog\n'
         'A growing queue with healthy workers usually means one task type is '
         'failing and retrying. Find the failing type before adding workers; '
         'more workers on a poison message simply fails faster.\n'
         '## Never do these under pressure\n'
         'Do not reverse a migration. Do not edit data by hand without a second '
         'person watching. Do not disable alerting to stop the noise.\n')},
    {'slug': 'release-process', 'space': 'ENG', 'version': 9,
     'title': 'Release process',
     'author': 'Priya Nair', 'created': '2026-03-04T10:00:00.000+1000',
     'updated': '2026-08-14T09:20:00.000+1000',
     'content': (
         '# Release process\n'
         'Releases go out Tuesday and Thursday afternoons, never Friday and '
         'never after 16:00 local time. Every incident in the last two years '
         'that took more than an hour to resolve began with a release nobody '
         'was awake to watch.\n'
         '## Preconditions\n'
         'Main is green. No destructive migration is included. The release note '
         'names a person who will watch dashboards for thirty minutes '
         'afterwards.\n'
         '## Sequence\n'
         'Tag from main. Apply migrations on their own first, which is safe '
         'because migrations must be backward compatible with the running code. '
         'Deploy one container and watch error rate and p95 latency for five '
         'minutes. Roll the rest. Post the tag and the migration list in the '
         'engineering channel.\n'
         '## Rollback\n'
         'Redeploy the previous tag and leave the schema alone.\n'
         '## Exceptions\n'
         'A security fix may go out at any time, with two people present. '
         'Nothing else is an exception, and "the customer is waiting" has never '
         'once justified a Friday release.\n')},
    {'slug': 'data-retention', 'space': 'DOCS', 'version': 7,
     'title': 'Data retention and deletion',
     'author': 'Dana Okoye', 'created': '2026-04-02T09:15:00.000+1000',
     'updated': '2026-08-20T14:00:00.000+1000',
     'content': (
         '# Data retention and deletion\n'
         'What we keep, for how long, and what happens when a customer asks us '
         'to delete it. This page is quoted in security reviews, so it must '
         'describe what the system actually does.\n'
         '## Retention periods\n'
         'Customer content is kept for the life of the contract and for thirty '
         'days afterwards, which is the window for an accidental cancellation. '
         'Audit records are kept for seven years because they are the evidence '
         'that an approval happened. Application logs are kept for thirty days, '
         'and contain no customer content by policy. Backups are kept for '
         'thirty-five days on a rolling schedule.\n'
         '## Deletion on request\n'
         'A verified deletion request removes customer content from the live '
         'database within seven days and from backups as those backups expire, '
         'within thirty-five days. We do not surgically edit backups; the '
         'honest answer to a customer is the expiry window, not a claim of '
         'immediate erasure.\n'
         '## What survives deletion\n'
         'Audit records of actions taken, with the content redacted but the '
         'fact retained, and financial records required by law. Both are stated '
         'in the contract.\n'
         '## Sub-processors\n'
         'Deletion is propagated to sub-processors within the same window. The '
         'current list is maintained on the trust page and reviewed quarterly.\n')},
    {'slug': 'api-standards', 'space': 'DOCS', 'version': 11,
     'title': 'API design standards',
     'author': 'Marcus Tan', 'created': '2026-02-27T13:40:00.000+1000',
     'updated': '2026-08-31T10:05:00.000+1000',
     'content': (
         '# API design standards\n'
         'Rules for anything we expose publicly. They exist so that a client '
         'written against one endpoint is not surprised by the next.\n'
         '## Versioning\n'
         'The major version is in the path. A breaking change requires a new '
         'major version and twelve months of overlap. Adding an optional field '
         'is not a breaking change; changing the meaning of an existing one is, '
         'even when the type is unchanged.\n'
         '## Pagination\n'
         'Cursors, never offsets. An offset skips and repeats rows when the '
         'underlying data changes between pages, and a client cannot tell that '
         'it happened. Every list response carries next_cursor, null on the '
         'last page.\n'
         '## Types\n'
         'Timestamps are ISO 8601 in UTC with an explicit Z. Money is an '
         'integer of minor units plus a currency code; there are no floating '
         'point amounts anywhere in the API. Enumerations are lowercase '
         'underscored strings and new members may be added at any time, so '
         'clients must tolerate an unknown one.\n'
         '## Errors\n'
         'The correct status code, plus an envelope with code, message and for '
         'validation failures a fields object. Code is stable and safe to '
         'branch on. Message is for a human and may change without notice.\n'
         '## Idempotency\n'
         'Every unsafe endpoint accepts an idempotency key and returns the '
         'original response for a repeat within twenty-four hours.\n')},
    {'slug': 'testing-strategy', 'space': 'ENG', 'version': 6,
     'title': 'Testing strategy',
     'author': 'Sara Lombardi', 'created': '2026-05-13T11:25:00.000+1000',
     'updated': '2026-08-09T16:30:00.000+1000',
     'content': (
         '# Testing strategy\n'
         'The purpose of a test here is to make a change safe to make, not to '
         'raise a coverage number. A test that has to be rewritten every time '
         'the implementation changes is a liability.\n'
         '## What we test where\n'
         'Business rules get unit tests against the service modules, which is '
         'where the rules live. Boundaries -- HTTP, the database, the connector '
         'layer -- get integration tests. The user journeys that would embarrass '
         'us if broken get a small number of end-to-end tests, currently eleven '
         'of them.\n'
         '## Rules\n'
         'No test depends on the wall clock; time is injected. No test reaches a '
         'live third-party service; external calls go through the connector '
         'layer in simulated mode. No test depends on the order tests run in. A '
         'flaky test is deleted or fixed within a week, never muted, because a '
         'muted test teaches the team to ignore red.\n'
         '## Pull requests\n'
         'Every change carries a test that fails without it. The exception is a '
         'pure refactor, where the existing tests passing unchanged is the '
         'point.\n'
         '## Performance\n'
         'The suite must finish in under six minutes. When it does not, the fix '
         'is to make the tests faster, not to run fewer of them.\n')},
    {'slug': 'glossary', 'space': 'DOCS', 'version': 19,
     'title': 'Glossary of platform terms',
     'author': 'Tomas Reyes', 'created': '2026-01-09T08:00:00.000+1000',
     'updated': '2026-09-02T09:35:00.000+1000',
     'content': (
         '# Glossary of platform terms\n'
         'Terms used precisely. A document that uses one of these differently '
         'is wrong and should be corrected.\n'
         '## Workspace\n'
         'One customer\'s isolated container of data, users and settings. '
         'Billing is per workspace, and a customer may hold several.\n'
         '## Seat\n'
         'One named user with a login. Not a concurrent session and not a role.\n'
         '## Approval\n'
         'A recorded human decision permitting an action that reaches outside '
         'the platform.\n'
         '## Proposed action\n'
         'An action an automated employee intends to take, held with its full '
         'argument set until a person approves, edits or rejects it. Editing '
         'does not approve.\n'
         '## Simulated action\n'
         'An action produced without contacting the external service, because a '
         'credential is absent or simulation was chosen. Always labelled, and '
         'never described as having happened.\n'
         '## Connector\n'
         'The code that speaks to one external service. Its configuration lives '
         'in the database, never in code and never in an environment variable.\n'
         '## First response\n'
         'Time from a customer message arriving to a human reply that engages '
         'with its content. An automatic acknowledgement does not count.\n')},
    {'slug': 'adr-0014-modular-monolith', 'space': 'ENG', 'version': 3,
     'title': 'ADR 0014: keep the modular monolith',
     'author': 'Priya Nair', 'created': '2026-06-11T14:00:00.000+1000',
     'updated': '2026-06-19T09:10:00.000+1000',
     'content': (
         '# ADR 0014: keep the modular monolith\n'
         'Status: accepted, 19 June 2026. Deciders: Priya Nair, Marcus Tan, '
         'Dana Okoye.\n'
         '## Context\n'
         'Two proposals argued for extracting services: the export pipeline, '
         'because it is resource hungry, and the notification sender, because it '
         'is chatty. Both are real problems.\n'
         '## Decision\n'
         'Neither is extracted. The export pipeline is rewritten to stream '
         'within the monolith, and notifications remain a queued task with a '
         'dedicated worker pool.\n'
         '## Reasoning\n'
         'Extraction would buy independent scaling and cost independent '
         'deployment, versioned contracts, distributed tracing and a second '
         'on-call surface. With seven engineers the second bill is larger than '
         'the first. Streaming solves the export resource problem outright, and '
         'a dedicated worker pool solves the notification problem without a '
         'network boundary.\n'
         '## Consequences\n'
         'We accept that a single deployment couples release timing across '
         'domains. We will revisit if the team passes roughly fifteen engineers, '
         'or if one domain develops genuinely different availability '
         'requirements from the rest.\n'
         '## Alternatives rejected\n'
         'Full microservices, rejected as disproportionate. Extracting only '
         'notifications, rejected because it establishes the distributed '
         'tooling cost without solving the pressing problem.\n')},
)


@register
class ConfluenceConnector(Connector):
    """Search, read, create and update Confluence Cloud pages."""

    key = 'confluence'
    name = 'Confluence'
    description = ('Searches and reads Confluence pages as plain prose for the '
                   'knowledge base, and publishes or updates documentation.')
    category = 'documents'
    icon = 'fa-confluence'
    color = '#172b4d'
    docs_url = 'https://developer.atlassian.com/cloud/confluence/rest/v1/'

    config_fields = (
        ConfigField(
            'site_url', 'Site URL',
            help_text='e.g. https://yourcompany.atlassian.net/wiki',
            field_type='url', required=True,
            placeholder='https://yourcompany.atlassian.net/wiki'),
        ConfigField(
            'email', 'Account email',
            help_text=('The Atlassian account that owns the API token. Its '
                       'space permissions are this integration\'s permissions.'),
            required=True, placeholder='you@yourcompany.com'),
        ConfigField(
            'api_token', 'API token',
            help_text=('Create one at '
                       'id.atlassian.com/manage-profile/security/api-tokens. '
                       'The same token works for Jira and Confluence.'),
            field_type='password', required=True, secret=True),
        ConfigField(
            'default_space_key', 'Default space key',
            help_text='e.g. DOCS', placeholder='DOCS'),
    )

    operations = ('search', 'get_page', 'list_spaces', 'list_pages',
                  'create_page', 'update_page')

    # -- plumbing ----------------------------------------------------------

    def _site(self):
        """The Atlassian site origin, however the URL was pasted.

        People copy this out of the browser address bar, which means it
        routinely arrives carrying the whole session query string:

            https://acme.atlassian.net?continue=%2Fwelcome&atlOrigin=eyJpIjoi...

        Appending a REST path to that produces a URL where the path lands
        after the query and the call fails in a way that looks like a
        credential problem rather than a typing one. So everything after the
        host is discarded, along with the trailing path fragments people
        habitually include.
        """
        raw = str(self.setting('site_url', '') or '').strip()
        if not raw:
            return ''

        parsed = urllib.parse.urlsplit(raw if '//' in raw else f'https://{raw}')
        site = f'{parsed.scheme or "https"}://{parsed.netloc}'.rstrip('/')

        # Confluence lives under /wiki, so it is added back rather than
        # stripped -- but only once, however the person typed it.
        return site + '/wiki'

    def _headers(self):
        email = str(self.setting('email', '') or '')
        token = str(self.setting('api_token', '') or '')
        pair = base64.b64encode(f'{email}:{token}'.encode()).decode('ascii')
        return {'Authorization': f'Basic {pair}', 'Accept': 'application/json'}

    def _space(self, space):
        return (str(space or '').strip().upper()
                or str(self.setting('default_space_key', '') or '').strip().upper())

    def _api(self, path, *, method='GET', payload=None, params=None):
        try:
            status, data = self.request_json(
                f'{self._site()}{path}', method=method, headers=self._headers(),
                payload=payload, params=params)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(self._advise(exc, path)) from exc
        return status, data

    def _advise(self, exc, path):
        try:
            body = exc.read().decode('utf-8', errors='replace')[:700]
        except Exception:  # noqa: BLE001
            body = ''
        code = exc.code
        if code == 401:
            # Confluence answers 401 for two entirely different situations, and
            # telling them apart matters more than the status code does.
            #
            # A genuinely bad credential comes back as JSON. A site that simply
            # has no Confluence on it comes back as an HTML sign-in page,
            # because Atlassian redirects to login rather than answering the
            # API at all. Reporting the second as "check your token" sends
            # somebody to re-make a token that was never the problem -- which
            # is exactly what happened here, with the same token returning 200
            # from Jira on the same site.
            looks_like_login = ('<html' in body.lower()
                                or 'text/html' in str(
                                    exc.headers.get('Content-Type', '')).lower())
            if looks_like_login:
                return ('Confluence answered with a sign-in page rather than the '
                        'API (401), which almost always means Confluence is not '
                        'activated on this Atlassian site. If Jira works with the '
                        'same token, that is the cause. Add Confluence to the site '
                        'from admin.atlassian.com, or the app switcher in Jira -- '
                        'the free tier is enough -- then try again.')
            return ('Confluence rejected the credentials (401). Check the '
                    'account email matches the account that created the API '
                    'token, and that the token has not been revoked.')
        if code == 403:
            return ('Confluence refused the action (403). The account can sign '
                    'in but lacks permission on this space -- typically Add '
                    'Pages or Space View. Ask a space administrator to grant '
                    'it.')
        if code == 404:
            return (f'Confluence returned 404 for {path}. Either the page or '
                    'space does not exist, or the Site URL is missing the /wiki '
                    'path. A Cloud wiki root looks like '
                    'https://yourcompany.atlassian.net/wiki.')
        if code == 409:
            return ('Confluence rejected the update as a version conflict '
                    '(409). Somebody edited the page between reading it and '
                    'writing it. Nothing was overwritten. Read the page again '
                    'and reapply the change.')
        if code == 400:
            return ('Confluence rejected the request (400). Usually invalid '
                    'storage-format markup in the body, or a malformed CQL '
                    f'query. Confluence said: {body}')
        return f'Confluence returned HTTP {code} {exc.reason}. {body}'.strip()

    def _page_url(self, item):
        links = item.get('_links') or {}
        webui = links.get('webui', '')
        if webui:
            return f'{self._site()}{webui}'
        return f'{self._site()}/pages/{item.get("id", "")}'

    def _page_row(self, item, *, with_body=False):
        version = (item.get('version') or {})
        space = (item.get('space') or {})
        row = {
            'id': str(item.get('id', '')),
            'title': item.get('title', ''),
            'type': item.get('type', 'page'),
            'space': space.get('key', ''),
            'space_name': space.get('name', ''),
            'version': version.get('number', 0),
            'author': ((version.get('by') or {}).get('displayName', '')),
            'updated': version.get('when', ''),
            'url': self._page_url(item),
        }
        if with_body:
            storage = (((item.get('body') or {}).get('storage') or {}).get('value', ''))
            row['content'] = _html_to_text(storage)
            row['words'] = len(row['content'].split())
        return row

    # =======================================================================
    # Live
    # =======================================================================

    def live_search(self, query='', limit=15, space=''):
        if not str(query).strip():
            return self.failure('What should be searched for? Pass query.')

        clauses = [f'text ~ "{_escape_cql(query)}"']
        scope = self._space(space) if str(space).strip() else ''
        if scope:
            clauses.append(f'space = "{_escape_cql(scope)}"')
        clauses.append('type = page')
        cql = ' AND '.join(clauses) + ' ORDER BY lastmodified DESC'

        _code, data = self._api('/rest/api/content/search',
                                params={'cql': cql,
                                        'limit': min(int(limit or 15), 100),
                                        'expand': 'space,version'})
        rows = [self._page_row(item) for item in (data.get('results') or [])]
        return self.ok(
            f'{len(rows)} Confluence page(s) match {str(query).strip()!r}'
            f'{f" in {scope}" if scope else ""}.',
            {'query': str(query), 'cql': cql, 'space': scope,
             'count': len(rows), 'total': data.get('totalSize', len(rows)),
             'results': rows})

    def live_get_page(self, page_id=''):
        page_id = str(page_id or '').strip()
        if not page_id:
            return self.failure('Which page? Pass page_id.')
        _code, data = self._api(f'/rest/api/content/{page_id}',
                                params={'expand': 'body.storage,version,space'})
        row = self._page_row(data, with_body=True)
        return self.ok(
            (f'{row["title"]} in {row["space"] or "an unknown space"}, version '
             f'{row["version"]}, {row["words"]} word(s) of text. {row["url"]}'),
            {'page': row})

    def live_list_spaces(self):
        _code, data = self._api('/rest/api/space',
                                params={'limit': 100, 'expand': 'description.plain'})
        rows = []
        for item in (data.get('results') or []):
            rows.append({
                'key': item.get('key', ''), 'name': item.get('name', ''),
                'type': item.get('type', ''),
                'description': ((((item.get('description') or {}).get('plain') or {})
                                 .get('value', ''))),
                'url': f'{self._site()}/spaces/{item.get("key", "")}',
            })
        return self.ok(
            f'{len(rows)} Confluence space(s) visible to this account: '
            f'{", ".join(row["key"] for row in rows) or "none"}.',
            {'count': len(rows), 'spaces': rows, 'default': self._space('')})

    def live_list_pages(self, space='', limit=25):
        scope = self._space(space)
        if not scope:
            return self.failure(
                'No space was given and no default space key is set. Set it on '
                'the Integrations page (for example DOCS), or pass space.')
        _code, data = self._api('/rest/api/content',
                                params={'spaceKey': scope, 'type': 'page',
                                        'limit': min(int(limit or 25), 100),
                                        'expand': 'version,space'})
        rows = [self._page_row(item) for item in (data.get('results') or [])]
        return self.ok(f'{len(rows)} page(s) in Confluence space {scope}.',
                       {'space': scope, 'count': len(rows), 'pages': rows})

    def live_create_page(self, space='', title='', body='', parent_id=''):
        scope = self._space(space)
        if not scope:
            return self.failure(
                'A new page needs a space. Set the default space key on the '
                'Integrations page, or pass space.')
        if not str(title).strip():
            return self.failure('A page needs a title.')

        payload = {
            'type': 'page',
            'title': str(title).strip()[:255],
            'space': {'key': scope},
            'body': {'storage': {'value': _text_to_storage(body),
                                 'representation': 'storage'}},
        }
        if str(parent_id).strip():
            payload['ancestors'] = [{'id': str(parent_id).strip()}]

        _code, data = self._api('/rest/api/content', method='POST', payload=payload)
        row = self._page_row(data)
        return self.ok(
            f'Published {row["title"]!r} in {scope} as version '
            f'{row["version"] or 1}. {row["url"]}',
            {'page_id': row['id'], 'title': row['title'], 'space': scope,
             'version': row['version'] or 1, 'url': row['url'],
             'parent_id': str(parent_id)})

    def live_update_page(self, page_id='', title='', body=''):
        """Update a page, reading its version first because Confluence insists.

        Confluence uses optimistic locking: the update must carry the version
        number it produces, exactly one above the current one. Reading first is
        not politeness, it is the only way the call succeeds -- and it means a
        concurrent human edit produces a clean 409 rather than silently
        discarding their work.
        """
        page_id = str(page_id or '').strip()
        if not page_id:
            return self.failure('Which page? Pass page_id.')
        if not str(title).strip() and not str(body).strip():
            return self.failure('Nothing to change. Pass title, body or both.')

        _code, current = self._api(f'/rest/api/content/{page_id}',
                                   params={'expand': 'version,space,body.storage'})
        version_now = ((current.get('version') or {}).get('number', 0)) or 0
        space = ((current.get('space') or {}).get('key', ''))
        existing_body = (((current.get('body') or {}).get('storage') or {})
                         .get('value', ''))

        payload = {
            'id': page_id,
            'type': current.get('type', 'page'),
            'title': str(title).strip()[:255] or current.get('title', ''),
            'version': {'number': version_now + 1},
            'body': {'storage': {
                'value': _text_to_storage(body) if str(body).strip() else existing_body,
                'representation': 'storage'}},
        }
        if space:
            payload['space'] = {'key': space}

        _code, data = self._api(f'/rest/api/content/{page_id}',
                                method='PUT', payload=payload)
        row = self._page_row(data)
        return self.ok(
            (f'Updated {row["title"]!r} from version {version_now} to '
             f'{row["version"] or version_now + 1}. {row["url"]}'),
            {'page_id': page_id, 'title': row['title'], 'space': space,
             'previous_version': version_now,
             'version': row['version'] or version_now + 1, 'url': row['url']})

    # -- connection check --------------------------------------------------

    def live_probe(self):
        _code, me = self._api('/rest/api/user/current')
        spaces = 0
        try:
            _code, data = self._api('/rest/api/space', params={'limit': 100})
            spaces = len(data.get('results') or [])
        except RuntimeError:
            pass
        default = self._space('')
        return self.ok(
            (f'Connected to {self._site()} as '
             f'{me.get("displayName", "unknown")}. {spaces} space(s) readable. '
             f'Default space: {default or "not set"}.'),
            {'account_id': me.get('accountId', ''),
             'display_name': me.get('displayName', ''),
             'site': self._site(), 'spaces_visible': spaces,
             'default_space': default})

    # =======================================================================
    # Demo halves -- eight fixture pages in DOCS and ENG
    # =======================================================================

    def _demo_site(self):
        return self._site() or 'https://acme.atlassian.net/wiki'

    def demo_search(self, query='', limit=15, space=''):
        if not str(query).strip():
            return self.failure('What should be searched for? Pass query.')
        needle = str(query).strip().lower()
        scope = self._space(space) if str(space).strip() else ''

        rows = []
        for page in DEMO_PAGES:
            if scope and page['space'] != scope:
                continue
            haystack = f'{page["title"]} {page["content"]}'.lower()
            if needle not in haystack:
                continue
            rows.append(_demo_row(self._demo_site(), page))
        rows = rows[:max(1, int(limit or 15))]
        cql = f'text ~ "{_escape_cql(query)}" AND type = page'
        if scope:
            cql = f'text ~ "{_escape_cql(query)}" AND space = "{scope}" AND type = page'
        return self.simulated(
            (f'Simulated Confluence search for {str(query).strip()!r}: '
             f'{len(rows)} match(es) across eight fixture pages in DOCS and '
             f'ENG. A live call would run the CQL {cql}. Nothing was read from '
             'your wiki.'),
            {'query': str(query), 'cql': cql, 'space': scope,
             'count': len(rows), 'total': len(rows), 'results': rows,
             'simulated': True})

    def demo_get_page(self, page_id=''):
        page = _find_demo_page(page_id)
        if page is None:
            return self.simulated(
                (f'No fixture page matches {page_id!r}. The demo wiki holds: '
                 f'{", ".join(item["title"] for item in DEMO_PAGES)}.'),
                {'page_id': str(page_id), 'found': False,
                 'available': [{'id': _demo_id(item['slug']), 'title': item['title'],
                                'space': item['space']} for item in DEMO_PAGES],
                 'simulated': True})
        row = _demo_row(self._demo_site(), page, with_body=True)
        return self.simulated(
            (f'Simulated {row["title"]} in {row["space"]}, version '
             f'{row["version"]}, {row["words"]} word(s) of fixture prose. '
             'Invented content, coherent enough to index, but not from your '
             'wiki.'),
            {'page': row, 'simulated': True})

    def demo_list_spaces(self):
        rows = []
        for item in DEMO_SPACES:
            held = sum(1 for page in DEMO_PAGES if page['space'] == item['key'])
            rows.append({'key': item['key'], 'name': item['name'],
                         'type': item['type'], 'pages': held,
                         'description': f'{held} fixture page(s).',
                         'url': f'{self._demo_site()}/spaces/{item["key"]}'})
        return self.simulated(
            f'Simulated {len(rows)} space(s): '
            f'{", ".join(row["key"] for row in rows)}. Fixture data.',
            {'count': len(rows), 'spaces': rows,
             'default': self._space('') or 'DOCS', 'simulated': True})

    def demo_list_pages(self, space='', limit=25):
        scope = self._space(space) or 'DOCS'
        rows = [_demo_row(self._demo_site(), page) for page in DEMO_PAGES
                if page['space'] == scope]
        rows = rows[:max(1, int(limit or 25))]
        return self.simulated(
            (f'Simulated {len(rows)} page(s) in space {scope}: '
             f'{", ".join(row["title"] for row in rows) or "none"}. '
             'Fixture data.'),
            {'space': scope, 'count': len(rows), 'pages': rows,
             'simulated': True})

    def demo_create_page(self, space='', title='', body='', parent_id=''):
        scope = self._space(space) or 'DOCS'
        if not str(title).strip():
            return self.failure('A page needs a title.')
        new_id = _demo_id(f'new:{scope}:{title}')
        return self.simulated(
            (f'Would publish {str(title).strip()!r} in space {scope} as version '
             f'1, with {len(str(body or "").split())} word(s) of body. Nothing '
             'was published to Confluence.'),
            {'page_id': new_id, 'title': str(title).strip(), 'space': scope,
             'version': 1, 'parent_id': str(parent_id),
             'url': f'{self._demo_site()}/spaces/{scope}/pages/{new_id}',
             'body_preview': str(body or '')[:600], 'simulated': True})

    def demo_update_page(self, page_id='', title='', body=''):
        if not str(title).strip() and not str(body).strip():
            return self.failure('Nothing to change. Pass title, body or both.')
        page = _find_demo_page(page_id)
        if page is None:
            return self.simulated(
                f'No fixture page matches {page_id!r}, so there is nothing to '
                'update. Nothing was written to Confluence.',
                {'page_id': str(page_id), 'found': False, 'simulated': True})
        return self.simulated(
            (f'Would update {page["title"]!r} in {page["space"]} from version '
             f'{page["version"]} to {page["version"] + 1}. A live call reads '
             'the current version first, because Confluence rejects an update '
             'that does not carry exactly the next number. Nothing was written '
             'to Confluence.'),
            {'page_id': _demo_id(page['slug']), 'space': page['space'],
             'title': str(title).strip() or page['title'],
             'previous_version': page['version'], 'version': page['version'] + 1,
             'url': f'{self._demo_site()}/spaces/{page["space"]}/pages/'
                    f'{_demo_id(page["slug"])}',
             'body_preview': str(body or '')[:600], 'simulated': True})


# ===========================================================================
# Demo helpers
# ===========================================================================

def _find_demo_page(page_id):
    wanted = str(page_id or '').strip().lower()
    if not wanted:
        return None
    for page in DEMO_PAGES:
        if _demo_id(page['slug']) == wanted or page['slug'] == wanted:
            return page
    for page in DEMO_PAGES:
        if wanted in page['title'].lower():
            return page
    return None


def _demo_row(site, page, with_body=False):
    identifier = _demo_id(page['slug'])
    row = {
        'id': identifier, 'title': page['title'], 'type': 'page',
        'space': page['space'],
        'space_name': next((item['name'] for item in DEMO_SPACES
                            if item['key'] == page['space']), page['space']),
        'version': page['version'], 'author': page['author'],
        'created': page['created'], 'updated': page['updated'],
        'url': f'{site}/spaces/{page["space"]}/pages/{identifier}',
        'excerpt': ' '.join(page['content'].split())[:280],
        'simulated': True,
    }
    if with_body:
        row['content'] = page['content']
        row['words'] = len(page['content'].split())
    else:
        row['words'] = len(page['content'].split())
    return row
