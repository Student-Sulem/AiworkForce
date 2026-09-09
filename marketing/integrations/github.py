"""GitHub: where the Engineering employee files, reads and closes real work.

WHAT THIS DOES FOR THE WORKFORCE
--------------------------------
GitHub is the system of record for engineering work, so it is the integration
that turns a conversation into something a developer will actually see. When
the Support employee triages a customer report and the Engineering employee
agrees it is a defect, the outcome is a numbered issue in a repository with
labels and an assignee -- not a note in a chat window that nobody reads on
Monday. When someone asks "what is in flight", the answer is the real open
issue and pull request list, including the pull request that is large enough
to be risky and the one whose checks are failing.

WHICH EMPLOYEES USE IT
----------------------
``Engineering``  files and updates issues, comments, closes them, reviews the
                 open pull requests and reads files out of the repository to
                 answer questions about how something is implemented.
``Support``      escalates a reproducible customer fault into an issue and
                 links the issue back onto the ticket.
``Research``     indexes the repository's markdown documentation, so the
                 architecture note and the deployment runbook are searchable
                 alongside Notion and Confluence pages.

THE CREDENTIAL YOU NEED
-----------------------
One personal access token. Sign in to GitHub, open
github.com/settings/tokens, and create a *fine-grained* token scoped to the
repositories this workspace should touch. Grant it:

    Issues            read and write   (filing, commenting, closing)
    Pull requests     read             (review and risk summaries)
    Contents          read             (reading files and markdown docs)
    Metadata          read             (granted automatically)

Paste it into the Personal access token field on the Integrations page and set
the default repository to ``owner/name``. Nothing is written into the code and
no environment variable is involved: the token lives in the Integration row
and is never rendered back to the browser.

WHAT DEMO MODE DOES INSTEAD
---------------------------
Without a token every operation is simulated against a deterministic fixture
repository, ``acme/platform``: eight open issues across bug, feature and
chore, three open pull requests (one large and risky, one trivial, one with a
failing check), ten commits, and four markdown documents in ``docs/`` that
contain genuine prose so the Research employee's indexing run has something
worth reading. Ids and numbers are derived from stable hashes, so the same
request always produces the same answer and a simulated create followed by a
simulated comment agree about the issue number. Every simulated result comes
back with ``demo`` set, and the interface says so.

A LIMIT WORTH KNOWING
---------------------
GitHub's REST API has no wiki endpoint. Wiki pages live in a separate
``.wiki.git`` repository that the API does not expose. ``list_wiki``
therefore returns the repository's markdown documentation -- ``docs/`` plus
any top-level ``*.md`` -- and says plainly in its summary that this is what it
did. Pretending otherwise would put invented pages into the knowledge base.
"""

import base64
import hashlib
import urllib.error

from . import register
from .base import CallResult, ConfigField, Connector

API_VERSION_HEADER = 'application/vnd.github+json'


# ===========================================================================
# Deterministic fixture repository used by every demo half
# ===========================================================================

DEMO_REPO = 'acme/platform'

DEMO_ISSUES = (
    {'number': 201, 'title': 'Export times out for accounts with more than 50k rows',
     'kind': 'bug', 'labels': ['bug', 'performance', 'reported-by-customer'],
     'assignees': ['priya-n'], 'comments': 7, 'created_at': '2026-08-14T09:12:00Z',
     'updated_at': '2026-09-02T11:40:00Z',
     'body': ('Three enterprise workspaces report that the CSV export never '
              'finishes. The request holds a worker for the full sixty second '
              'gateway timeout and then returns a 504. Reproduced on staging '
              'with a 62,000 row campaign table. The export builds the whole '
              'file in memory before writing a byte, so the fix is almost '
              'certainly to stream it.')},
    {'number': 203, 'title': 'Add SAML single sign-on for enterprise plans',
     'kind': 'feature', 'labels': ['feature', 'enterprise', 'security'],
     'assignees': ['marcus-t'], 'comments': 12, 'created_at': '2026-07-30T14:02:00Z',
     'updated_at': '2026-09-04T08:15:00Z',
     'body': ('Four of the six deals in the enterprise pipeline have asked for '
              'SAML. Scope for the first release: service-provider initiated '
              'login, metadata exchange by URL, group to role mapping, and no '
              'just-in-time provisioning yet. SCIM is explicitly out of scope '
              'and tracked separately.')},
    {'number': 205, 'title': 'Webhook retries duplicate deliveries on 502 responses',
     'kind': 'bug', 'labels': ['bug', 'integrations'],
     'assignees': ['dan-k'], 'comments': 4, 'created_at': '2026-08-21T16:45:00Z',
     'updated_at': '2026-09-01T10:05:00Z',
     'body': ('The delivery worker treats any non-2xx as retryable and does not '
              'record that the body was already accepted. A receiver that '
              'returns 502 after processing therefore gets the same event '
              'again. Needs an idempotency key on the delivery record.')},
    {'number': 206, 'title': 'Upgrade to Django 6.1 and drop the pinned psycopg patch',
     'kind': 'chore', 'labels': ['chore', 'dependencies'],
     'assignees': [], 'comments': 2, 'created_at': '2026-08-02T08:30:00Z',
     'updated_at': '2026-08-28T13:22:00Z',
     'body': ('We are two minor versions behind and carrying a local patch to '
              'psycopg that upstream fixed in 3.2.4. Upgrading removes the '
              'patch and unblocks the async view work.')},
    {'number': 208, 'title': 'Bulk edit for campaign records',
     'kind': 'feature', 'labels': ['feature', 'ux'],
     'assignees': ['sara-l'], 'comments': 9, 'created_at': '2026-08-19T11:00:00Z',
     'updated_at': '2026-09-03T15:48:00Z',
     'body': ('Marketing operations edit the same three fields across dozens of '
              'campaigns every month, one row at a time. Select rows, choose a '
              'field, apply once. Needs an undo window because a mistaken bulk '
              'edit is far more expensive than a mistaken single edit.')},
    {'number': 209, 'title': 'Scheduling screen shows UTC instead of the workspace timezone',
     'kind': 'bug', 'labels': ['bug', 'ux', 'good-first-issue'],
     'assignees': [], 'comments': 3, 'created_at': '2026-08-25T07:20:00Z',
     'updated_at': '2026-08-30T09:10:00Z',
     'body': ('The scheduling panel renders the stored UTC value with no '
              'conversion, so a post scheduled for 09:00 Melbourne time reads '
              'as 23:00 the previous day. The template is missing the '
              'localtime filter; the stored data is correct.')},
    {'number': 211, 'title': 'Split the settings module into per-environment modules',
     'kind': 'chore', 'labels': ['chore', 'tech-debt'],
     'assignees': ['priya-n'], 'comments': 5, 'created_at': '2026-07-24T10:40:00Z',
     'updated_at': '2026-08-27T12:00:00Z',
     'body': ('One 600 line settings file with nested conditionals decides '
              'behaviour for local, staging and production. Every deployment '
              'incident this quarter touched it. Split into a base module plus '
              'one module per environment.')},
    {'number': 212, 'title': 'Weekly digest email for workspace owners',
     'kind': 'feature', 'labels': ['feature', 'notifications'],
     'assignees': [], 'comments': 1, 'created_at': '2026-09-01T09:05:00Z',
     'updated_at': '2026-09-05T08:55:00Z',
     'body': ('Owners want one email on Monday morning summarising the week: '
              'campaigns published, approvals still pending, and anything that '
              'failed. Must be switchable off per user and must never include '
              'customer personal data in the body.')},
)

DEMO_PULLS = (
    {'number': 214, 'title': 'Stream the export pipeline instead of buffering it',
     'author': 'priya-n', 'head': 'export-streaming', 'base': 'main',
     'changed_files': 24, 'additions': 1842, 'deletions': 613,
     'mergeable_state': 'dirty', 'draft': False, 'review_state': 'changes_requested',
     'checks': 'passing', 'comments': 18,
     'created_at': '2026-08-26T10:15:00Z', 'updated_at': '2026-09-05T16:30:00Z',
     'body': ('Closes #201. Replaces the in-memory writer with a generator that '
              'streams rows straight to the response, and moves the header '
              'logic out of the view. Large and touching shared serialisers, so '
              'it wants two reviewers rather than one. Currently conflicting '
              'with main after the serialiser rename landed.')},
    {'number': 215, 'title': 'Render scheduling times in the workspace timezone',
     'author': 'sara-l', 'head': 'fix-schedule-timezone', 'base': 'main',
     'changed_files': 2, 'additions': 14, 'deletions': 6,
     'mergeable_state': 'clean', 'draft': False, 'review_state': 'approved',
     'checks': 'passing', 'comments': 2,
     'created_at': '2026-09-03T08:40:00Z', 'updated_at': '2026-09-04T09:05:00Z',
     'body': 'Closes #209. Adds the localtime filter and a regression test.'},
    {'number': 216, 'title': 'Add the SAML metadata endpoint',
     'author': 'marcus-t', 'head': 'saml-metadata', 'base': 'main',
     'changed_files': 7, 'additions': 402, 'deletions': 31,
     'mergeable_state': 'unstable', 'draft': False, 'review_state': 'pending',
     'checks': 'failing', 'comments': 6,
     'created_at': '2026-09-02T13:20:00Z', 'updated_at': '2026-09-05T11:10:00Z',
     'body': ('First slice of #203: publishes service-provider metadata and '
              'validates an identity provider descriptor. The certificate '
              'fixture test fails on CI because the fixture expired; a new '
              'fixture is needed before this can merge.')},
)

DEMO_PULL_FILES = {
    214: (
        ('marketing/exports/pipeline.py', 'modified', 318, 204),
        ('marketing/exports/streaming.py', 'added', 246, 0),
        ('marketing/exports/writers.py', 'modified', 141, 96),
        ('marketing/serialisers/campaign.py', 'modified', 88, 61),
        ('marketing/views_export.py', 'modified', 74, 122),
        ('marketing/tests/test_exports.py', 'modified', 402, 38),
        ('marketing/tests/test_streaming.py', 'added', 311, 0),
        ('docs/architecture.md', 'modified', 42, 9),
    ),
    215: (
        ('templates/scheduling/panel.html', 'modified', 6, 4),
        ('marketing/tests/test_scheduling.py', 'modified', 8, 2),
    ),
    216: (
        ('marketing/auth/saml.py', 'added', 188, 0),
        ('marketing/auth/metadata.py', 'added', 96, 0),
        ('marketing/urls.py', 'modified', 6, 1),
        ('marketing/tests/test_saml.py', 'added', 112, 0),
    ),
}

DEMO_COMMITS = (
    ('9f1c0a7', 'priya-n', '2026-09-05T16:28:00Z',
     'Stream export rows instead of buffering the whole file'),
    ('7b34e12', 'sara-l', '2026-09-04T09:02:00Z',
     'Render scheduling times in the workspace timezone'),
    ('4d90aa3', 'marcus-t', '2026-09-03T13:18:00Z',
     'Publish service-provider SAML metadata'),
    ('1e57bc9', 'dan-k', '2026-09-02T15:44:00Z',
     'Record an idempotency key on every webhook delivery'),
    ('c02df55', 'priya-n', '2026-09-01T11:30:00Z',
     'Rename the campaign serialiser fields for consistency'),
    ('8a6b331', 'sara-l', '2026-08-29T10:12:00Z',
     'Add the undo window to bulk edits behind a flag'),
    ('35ce70d', 'dan-k', '2026-08-28T14:05:00Z',
     'Upgrade Django to 6.1 and remove the psycopg patch'),
    ('b71f2e8', 'marcus-t', '2026-08-27T09:48:00Z',
     'Move deployment steps into the runbook'),
    ('6c48d90', 'priya-n', '2026-08-26T16:22:00Z',
     'Split settings into base plus one module per environment'),
    ('2fa9017', 'sara-l', '2026-08-25T08:35:00Z',
     'Document the API error envelope'),
)

DEMO_DOCS = {
    'docs/architecture.md': (
        '# Platform architecture\n'
        '\n'
        'The platform is one Django project with a single application boundary '
        'per business domain and one shared database. We chose a modular '
        'monolith rather than services because the whole engineering team is '
        'seven people: the cost of a network hop between two modules that '
        'change together is paid every day, whereas the cost of a shared '
        'deployment is paid only when we release.\n'
        '\n'
        '## Request path\n'
        '\n'
        'A request arrives at the load balancer, terminates TLS there, and is '
        'passed to one of four application containers. Views are thin: they '
        'validate input, call into a service module, and render. Business '
        'rules live in the service modules so that the same rule is enforced '
        'whether the caller is a browser, the public API, or a background '
        'worker.\n'
        '\n'
        '## Data\n'
        '\n'
        'Postgres is the only durable store. Redis holds the cache and the task '
        'queue, and is treated as disposable: nothing in Redis is the only copy '
        'of anything. Long running work is handed to workers through the queue, '
        'and every task must be safe to run twice, because at-least-once '
        'delivery is the guarantee the queue actually gives us.\n'
        '\n'
        '## Boundaries that matter\n'
        '\n'
        'Outbound calls to third parties are funnelled through one connector '
        'layer. That is what makes it possible to record every external effect, '
        'to simulate it when a credential is absent, and to rate limit it in '
        'one place. Nothing else in the codebase is permitted to open a socket '
        'to a vendor.\n'
        '\n'
        '## What we would change\n'
        '\n'
        'The settings module is the weakest part of the design and is being '
        'split per environment. The export pipeline buffers whole files in '
        'memory and is being rewritten to stream.\n'
    ),
    'docs/deployment.md': (
        '# Deployment runbook\n'
        '\n'
        'Releases go out on Tuesday and Thursday afternoons, never on a Friday '
        'and never after 16:00 local time. The rule exists because every '
        'incident that took more than an hour to resolve in the last two years '
        'began with a release nobody was awake to watch.\n'
        '\n'
        '## Before you start\n'
        '\n'
        'Confirm that main is green, that no migration in the release is '
        'destructive, and that the release notes name a person who will watch '
        'the dashboards for thirty minutes afterwards.\n'
        '\n'
        '## Steps\n'
        '\n'
        '1. Tag the release from main and let the build produce the image.\n'
        '2. Apply migrations first, on their own. Migrations in this project '
        'must be backward compatible with the currently running code, so this '
        'step is always safe to do before the new code is live.\n'
        '3. Deploy to one container and watch error rate and p95 latency for '
        'five minutes.\n'
        '4. Roll the remaining containers.\n'
        '5. Post the release note in the engineering channel with the tag and '
        'the migration list.\n'
        '\n'
        '## Rolling back\n'
        '\n'
        'Redeploy the previous tag. Do not reverse a migration as part of a '
        'rollback: because migrations are backward compatible, the old code '
        'runs correctly against the new schema. Reversing a migration under '
        'pressure is how data is lost.\n'
        '\n'
        '## If the deployment goes wrong\n'
        '\n'
        'Declare an incident in the channel before investigating, so that '
        'everyone joining knows what is happening. One person coordinates, one '
        'person investigates, and nobody changes production without saying so '
        'out loud first.\n'
    ),
    'docs/api-reference.md': (
        '# Public API reference\n'
        '\n'
        'The API is REST over HTTPS, versioned in the path, and returns JSON. '
        'The current version is v2. Version v1 is frozen and will be withdrawn '
        'twelve months after v2 became general availability.\n'
        '\n'
        '## Authentication\n'
        '\n'
        'Send an API key as a bearer token. Keys are scoped to a workspace and '
        'carry one of three roles: read, write, or admin. A key is shown once '
        'at creation and stored as a hash, so a lost key is replaced rather '
        'than recovered.\n'
        '\n'
        '## Conventions\n'
        '\n'
        'Collections are paginated with a cursor, not an offset, because '
        'offsets skip and repeat rows when the underlying data changes between '
        'pages. Every list response carries `next_cursor`, which is null on the '
        'final page. Timestamps are ISO 8601 in UTC with an explicit Z. Money '
        'is an integer of minor units plus a currency code; there are no '
        'floating point amounts anywhere in the API.\n'
        '\n'
        '## Errors\n'
        '\n'
        'Errors return the appropriate status plus an envelope containing '
        '`code`, `message` and, for validation failures, a `fields` object '
        'keyed by field name. `code` is stable and safe to branch on; '
        '`message` is written for a human and may change.\n'
        '\n'
        '## Rate limits\n'
        '\n'
        'Six hundred requests per minute per key, burst of one hundred. A '
        'throttled request returns 429 with a Retry-After header. Clients are '
        'expected to back off exponentially with jitter rather than retrying on '
        'a fixed interval.\n'
    ),
    'docs/contributing.md': (
        '# Contributing\n'
        '\n'
        'The aim of this guide is that a change is easy to review. Reviewing is '
        'the bottleneck on this team, not writing, so the work of making a '
        'change reviewable belongs to the author.\n'
        '\n'
        '## Branches and commits\n'
        '\n'
        'Branch from main, one topic per branch. Write commit subjects in the '
        'imperative present: "Stream export rows", not "Streamed" or '
        '"Streaming". Explain in the body why the change is being made; the '
        'diff already shows what changed.\n'
        '\n'
        '## Pull requests\n'
        '\n'
        'Keep a pull request under roughly four hundred changed lines. Above '
        'that, review quality falls off sharply and approvals start meaning '
        '"looks plausible" rather than "I understand this". If a change is '
        'genuinely large, split it into a mechanical part and a behavioural '
        'part and land the mechanical part first.\n'
        '\n'
        'Every pull request needs a test that fails without the change. A pull '
        'request that touches the connector layer or authentication needs two '
        'reviewers.\n'
        '\n'
        '## Style\n'
        '\n'
        'The formatter decides layout and is not up for discussion in review. '
        'Name things for what they are in the business, not for their type. '
        'Comments explain why; if a comment has to explain what, rewrite the '
        'code instead.\n'
        '\n'
        '## Tests\n'
        '\n'
        'Unit tests for rules, integration tests for boundaries, and no test '
        'that depends on the wall clock or on a live third-party service. '
        'External services are exercised through the connector layer in '
        'simulated mode.\n'
    ),
}

DEMO_README = (
    '# acme/platform\n'
    '\n'
    'The Acme marketing and operations platform. See `docs/architecture.md` for '
    'the design, `docs/deployment.md` for how a release goes out, and '
    '`docs/contributing.md` before opening a pull request.\n'
)


def _stable(text, low, high):
    """A repeatable pseudo-number from a string, so demo ids never drift."""
    digest = hashlib.sha256(str(text).encode('utf-8')).hexdigest()
    return low + int(digest[:8], 16) % max(1, (high - low + 1))


@register
class GitHubConnector(Connector):
    """Issues, pull requests, files and commits over the GitHub REST API."""

    key = 'github'
    name = 'GitHub'
    description = ('Files and updates issues, reviews pull requests and reads '
                   'repository files and markdown documentation.')
    category = 'development'
    icon = 'fa-github'
    color = '#24292f'
    docs_url = 'https://docs.github.com/en/rest'

    config_fields = (
        ConfigField(
            'access_token', 'Personal access token',
            help_text=('Create a fine-grained token at github.com/settings/tokens '
                       'with Issues read and write and Contents read on the '
                       'repositories you want.'),
            field_type='password', required=True, secret=True,
            placeholder='github_pat_...'),
        ConfigField(
            'default_repository', 'Default repository',
            help_text='As owner/name, e.g. acme/platform.',
            placeholder='acme/platform'),
        ConfigField(
            'api_base', 'API base URL',
            help_text=('Leave as it is for github.com. Change it only for '
                       'GitHub Enterprise, e.g. https://ghe.example.com/api/v3.'),
            default='https://api.github.com'),
        ConfigField(
            'default_labels', 'Labels added to every issue',
            help_text=('Comma separated, e.g. ai-workforce,triage. Applied on '
                       'top of whatever the employee asks for, so filed work is '
                       'always identifiable.'),
            placeholder='ai-workforce'),
    )

    operations = (
        'create_issue', 'update_issue', 'comment_issue', 'close_issue',
        'list_issues', 'get_issue', 'list_pull_requests', 'get_pull_request',
        'list_pull_request_files', 'get_repo', 'read_file', 'search_code',
        'list_wiki', 'list_commits',
    )

    # -- plumbing ----------------------------------------------------------

    def _base(self):
        return str(self.setting('api_base', 'https://api.github.com')).rstrip('/')

    def _headers(self):
        return {
            'Authorization': f'Bearer {self.setting("access_token")}',
            'Accept': API_VERSION_HEADER,
            'X-GitHub-Api-Version': '2022-11-28',
        }

    def _repo(self, repo):
        """The repository to act on, or '' when nothing is configured."""
        return (str(repo or '').strip()
                or str(self.setting('default_repository', '') or '').strip())

    def _no_repo(self):
        return self.failure(
            'No repository was given and no default repository is set. Open the '
            'Integrations page, choose GitHub, and set the default repository to '
            'owner/name (for example acme/platform), or pass repo explicitly.')

    def _labels(self, labels):
        """The caller's labels plus the always-on ones, de-duplicated."""
        wanted = []
        for item in (labels or []):
            text = str(item).strip()
            if text and text not in wanted:
                wanted.append(text)
        configured = str(self.setting('default_labels', '') or '')
        for item in configured.replace(';', ',').split(','):
            text = item.strip()
            if text and text not in wanted:
                wanted.append(text)
        return wanted

    def _api(self, path, *, method='GET', payload=None, params=None):
        """One GitHub call, with its error body translated into advice.

        GitHub's failures are unusually specific, and repeating that
        specificity back to the person who has to fix it saves a support
        conversation. A 404 on a private repository, for instance, almost
        never means the repository is missing.
        """
        url = path if path.startswith('http') else f'{self._base()}{path}'
        try:
            status, data = self.request_json(
                url, method=method, headers=self._headers(),
                payload=payload, params=params)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(self._advise(exc, path)) from exc
        return status, data

    def _advise(self, exc, path):
        try:
            body = exc.read().decode('utf-8', errors='replace')[:600]
        except Exception:  # noqa: BLE001
            body = ''
        code = exc.code
        if code == 401:
            return ('GitHub rejected the personal access token (401). It has '
                    'expired or been revoked. Create a new fine-grained token at '
                    'github.com/settings/tokens and paste it into the GitHub '
                    'integration.')
        if code == 403 and 'rate limit' in body.lower():
            return ('GitHub rate limit reached (403). The token is valid; wait '
                    'for the window to reset, usually within the hour, and try '
                    'again.')
        if code == 403:
            return ('GitHub refused the request (403). The token is valid but '
                    'lacks permission for this action. Check that the '
                    'fine-grained token lists this repository and grants Issues '
                    f'read and write and Contents read. Detail: {body}')
        if code == 404:
            return (f'GitHub returned 404 for {path}. Either the repository, '
                    'issue or path does not exist, or the token has not been '
                    'granted access to it -- a private repository missing from '
                    'the token\'s repository list looks exactly like a missing '
                    'one. Confirm the owner/name spelling and the token scope.')
        if code == 410:
            return ('GitHub returned 410: issues are disabled on this '
                    'repository. Enable them in the repository settings, or file '
                    'the work in Jira instead.')
        if code == 422:
            return ('GitHub rejected the content (422). Usually a label or '
                    'assignee that does not exist on the repository, or an empty '
                    f'title. Detail: {body}')
        return f'GitHub returned HTTP {code} {exc.reason}. {body}'.strip()

    def _issue_row(self, item):
        return {
            'number': item.get('number'),
            'title': item.get('title', ''),
            'state': item.get('state', ''),
            'url': item.get('html_url', ''),
            'api_url': item.get('url', ''),
            'labels': [label.get('name') if isinstance(label, dict) else str(label)
                       for label in (item.get('labels') or [])],
            'assignees': [who.get('login', '') for who in (item.get('assignees') or [])],
            'author': (item.get('user') or {}).get('login', ''),
            'comments': item.get('comments', 0),
            'created_at': item.get('created_at', ''),
            'updated_at': item.get('updated_at', ''),
            'body': (item.get('body') or '')[:4000],
        }

    # =======================================================================
    # Issues -- live
    # =======================================================================

    def live_create_issue(self, repo='', title='', body='', labels=None, assignees=None):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()
        if not str(title).strip():
            return self.failure('An issue needs a title. GitHub rejects an empty one.')

        payload = {'title': str(title).strip(), 'body': str(body or '')}
        resolved = self._labels(labels)
        if resolved:
            payload['labels'] = resolved
        if assignees:
            payload['assignees'] = [str(who).lstrip('@') for who in assignees]

        _status, data = self._api(f'/repos/{repo}/issues', method='POST', payload=payload)
        row = self._issue_row(data)
        return self.ok(
            f'Filed {repo}#{row["number"]}: {row["title"]}. {row["url"]}',
            {'number': row['number'], 'url': row['url'], 'api_url': row['api_url'],
             'state': row['state'], 'title': row['title'], 'repository': repo,
             'labels': row['labels'], 'assignees': row['assignees']})

    def live_update_issue(self, repo='', number=None, title='', body='',
                          state='', labels=None, assignees=None):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()
        if not number:
            return self.failure('Which issue? Pass number, e.g. number=201.')

        payload = {}
        if str(title).strip():
            payload['title'] = str(title).strip()
        if str(body).strip():
            payload['body'] = str(body)
        if str(state).strip():
            wanted = str(state).strip().lower()
            if wanted not in ('open', 'closed'):
                return self.failure(
                    f"GitHub only understands 'open' or 'closed' for an issue "
                    f"state, not {state!r}.")
            payload['state'] = wanted
        if labels is not None:
            payload['labels'] = self._labels(labels)
        if assignees is not None:
            payload['assignees'] = [str(who).lstrip('@') for who in assignees]

        if not payload:
            return self.failure(
                'Nothing to change. Pass at least one of title, body, state, '
                'labels or assignees.')

        _status, data = self._api(f'/repos/{repo}/issues/{number}',
                                  method='PATCH', payload=payload)
        row = self._issue_row(data)
        return self.ok(
            f'Updated {repo}#{number} ({", ".join(sorted(payload))}). {row["url"]}',
            {'number': row['number'], 'url': row['url'], 'state': row['state'],
             'title': row['title'], 'changed': sorted(payload), 'repository': repo})

    def live_comment_issue(self, repo='', number=None, body=''):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()
        if not number:
            return self.failure('Which issue? Pass number, e.g. number=201.')
        if not str(body).strip():
            return self.failure('A comment needs a body.')

        _status, data = self._api(f'/repos/{repo}/issues/{number}/comments',
                                  method='POST', payload={'body': str(body)})
        return self.ok(
            f'Commented on {repo}#{number}. {data.get("html_url", "")}',
            {'comment_id': data.get('id'), 'url': data.get('html_url', ''),
             'number': number, 'repository': repo,
             'author': (data.get('user') or {}).get('login', '')})

    def live_close_issue(self, repo='', number=None, reason='completed'):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()
        if not number:
            return self.failure('Which issue? Pass number, e.g. number=201.')

        why = str(reason or 'completed').strip().lower().replace(' ', '_')
        if why not in ('completed', 'not_planned', 'reopened'):
            why = 'completed'
        _status, data = self._api(
            f'/repos/{repo}/issues/{number}', method='PATCH',
            payload={'state': 'closed', 'state_reason': why})
        row = self._issue_row(data)
        return self.ok(
            f'Closed {repo}#{number} as {why.replace("_", " ")}. {row["url"]}',
            {'number': row['number'], 'state': row['state'], 'reason': why,
             'url': row['url'], 'title': row['title'], 'repository': repo})

    def live_list_issues(self, repo='', state='open', limit=20, labels=''):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()

        params = {'state': str(state or 'open'), 'per_page': min(int(limit or 20), 100)}
        if str(labels).strip():
            params['labels'] = str(labels).strip()
        _status, data = self._api(f'/repos/{repo}/issues', params=params)

        # GitHub returns pull requests in the issues list. An entry carrying a
        # 'pull_request' key is a pull request wearing an issue number, and
        # counting it as an issue is the classic way to over-report the backlog.
        rows, skipped = [], 0
        for item in (data if isinstance(data, list) else []):
            if 'pull_request' in item:
                skipped += 1
                continue
            rows.append(self._issue_row(item))

        note = f' {skipped} pull request(s) in the response were excluded.' if skipped else ''
        return self.ok(
            f'{len(rows)} {state} issue(s) in {repo}.{note}',
            {'repository': repo, 'state': state, 'count': len(rows),
             'pull_requests_excluded': skipped, 'issues': rows})

    def live_get_issue(self, repo='', number=None):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()
        if not number:
            return self.failure('Which issue? Pass number, e.g. number=201.')

        _status, data = self._api(f'/repos/{repo}/issues/{number}')
        row = self._issue_row(data)
        row['is_pull_request'] = 'pull_request' in data
        return self.ok(
            f'{repo}#{number} [{row["state"]}] {row["title"]}',
            {'repository': repo, 'issue': row})

    # =======================================================================
    # Pull requests -- live
    # =======================================================================

    def live_list_pull_requests(self, repo='', state='open', limit=20):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()

        _status, data = self._api(
            f'/repos/{repo}/pulls',
            params={'state': str(state or 'open'), 'per_page': min(int(limit or 20), 100)})
        rows = []
        for item in (data if isinstance(data, list) else []):
            rows.append({
                'number': item.get('number'),
                'title': item.get('title', ''),
                'state': item.get('state', ''),
                'draft': bool(item.get('draft')),
                'author': (item.get('user') or {}).get('login', ''),
                'head': (item.get('head') or {}).get('ref', ''),
                'base': (item.get('base') or {}).get('ref', ''),
                'url': item.get('html_url', ''),
                'created_at': item.get('created_at', ''),
                'updated_at': item.get('updated_at', ''),
                'labels': [label.get('name', '') for label in (item.get('labels') or [])],
            })
        return self.ok(f'{len(rows)} {state} pull request(s) in {repo}.',
                       {'repository': repo, 'state': state, 'count': len(rows),
                        'pull_requests': rows})

    def live_get_pull_request(self, repo='', number=None):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()
        if not number:
            return self.failure('Which pull request? Pass number, e.g. number=214.')

        _status, data = self._api(f'/repos/{repo}/pulls/{number}')
        row = {
            'number': data.get('number'),
            'title': data.get('title', ''),
            'state': data.get('state', ''),
            'draft': bool(data.get('draft')),
            'author': (data.get('user') or {}).get('login', ''),
            'body': (data.get('body') or '')[:4000],
            'changed_files': data.get('changed_files', 0),
            'additions': data.get('additions', 0),
            'deletions': data.get('deletions', 0),
            'commits': data.get('commits', 0),
            'mergeable': data.get('mergeable'),
            'mergeable_state': data.get('mergeable_state', ''),
            'head_branch': (data.get('head') or {}).get('ref', ''),
            'base_branch': (data.get('base') or {}).get('ref', ''),
            'head_sha': (data.get('head') or {}).get('sha', '')[:7],
            'review_comments': data.get('review_comments', 0),
            'url': data.get('html_url', ''),
            'created_at': data.get('created_at', ''),
            'updated_at': data.get('updated_at', ''),
        }
        row['size_warning'] = _size_warning(row['changed_files'],
                                            row['additions'] + row['deletions'])
        return self.ok(
            (f'{repo}#{number} {row["title"]}: {row["changed_files"]} file(s), '
             f'+{row["additions"]}/-{row["deletions"]}, {row["head_branch"]} into '
             f'{row["base_branch"]}, merge state {row["mergeable_state"] or "unknown"}.'),
            {'repository': repo, 'pull_request': row})

    def live_list_pull_request_files(self, repo='', number=None, limit=100):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()
        if not number:
            return self.failure('Which pull request? Pass number, e.g. number=214.')

        _status, data = self._api(
            f'/repos/{repo}/pulls/{number}/files',
            params={'per_page': min(int(limit or 100), 100)})
        rows, additions, deletions = [], 0, 0
        for item in (data if isinstance(data, list) else []):
            patch = item.get('patch') or ''
            additions += item.get('additions', 0)
            deletions += item.get('deletions', 0)
            rows.append({
                'filename': item.get('filename', ''),
                'status': item.get('status', ''),
                'additions': item.get('additions', 0),
                'deletions': item.get('deletions', 0),
                'patch': patch[:1500],
                'patch_truncated': len(patch) > 1500,
            })
        return self.ok(
            f'{len(rows)} file(s) changed in {repo}#{number}, '
            f'+{additions}/-{deletions}.',
            {'repository': repo, 'number': number, 'count': len(rows),
             'additions': additions, 'deletions': deletions, 'files': rows})

    # =======================================================================
    # Repository, files, search -- live
    # =======================================================================

    def live_get_repo(self, repo=''):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()

        _status, data = self._api(f'/repos/{repo}')
        row = {
            'full_name': data.get('full_name', repo),
            'description': data.get('description') or '',
            'language': data.get('language') or '',
            'open_issues': data.get('open_issues_count', 0),
            'default_branch': data.get('default_branch', ''),
            'stars': data.get('stargazers_count', 0),
            'forks': data.get('forks_count', 0),
            'private': bool(data.get('private')),
            'url': data.get('html_url', ''),
            'pushed_at': data.get('pushed_at', ''),
            'topics': data.get('topics') or [],
        }
        return self.ok(
            (f'{row["full_name"]}: {row["description"] or "no description"}. '
             f'{row["language"] or "language not detected"}, '
             f'{row["open_issues"]} open issue(s) including pull requests, '
             f'default branch {row["default_branch"]}, {row["stars"]} star(s).'),
            {'repository': row})

    def live_read_file(self, repo='', path='', ref=''):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()
        if not str(path).strip():
            return self.failure('Which file? Pass path, e.g. path=docs/architecture.md.')

        params = {'ref': str(ref).strip()} if str(ref).strip() else None
        _status, data = self._api(f'/repos/{repo}/contents/{str(path).lstrip("/")}',
                                  params=params)
        if isinstance(data, list):
            names = [item.get('name', '') for item in data]
            return self.ok(
                f'{path} in {repo} is a directory holding {len(names)} entr(ies).',
                {'repository': repo, 'path': path, 'is_directory': True,
                 'entries': names})

        raw = base64.b64decode((data.get('content') or '').encode('ascii', 'ignore'))
        text, is_binary = _decode(raw)
        if is_binary:
            return self.ok(
                (f'{path} in {repo} is a binary file of {data.get("size", len(raw))} '
                 'byte(s), so there is no text to return. Nothing readable was '
                 'invented in its place.'),
                {'repository': repo, 'path': path, 'is_binary': True,
                 'size': data.get('size', len(raw)), 'content': '',
                 'url': data.get('html_url', '')})

        return self.ok(
            f'Read {path} from {repo} ({len(text)} character(s)).',
            {'repository': repo, 'path': path, 'is_binary': False,
             'size': data.get('size', len(raw)), 'sha': data.get('sha', ''),
             'ref': str(ref) or 'default branch', 'content': text,
             'url': data.get('html_url', '')})

    def live_search_code(self, repo='', query='', limit=20):
        repo = self._repo(repo)
        if not str(query).strip():
            return self.failure('What should be searched for? Pass query.')

        expression = str(query).strip()
        if repo:
            expression = f'{expression} repo:{repo}'
        _status, data = self._api(
            '/search/code',
            params={'q': expression, 'per_page': min(int(limit or 20), 100)})

        rows = []
        for item in (data.get('items') or []):
            rows.append({
                'path': item.get('path', ''),
                'name': item.get('name', ''),
                'repository': (item.get('repository') or {}).get('full_name', ''),
                'url': item.get('html_url', ''),
                'score': item.get('score', 0),
            })
        return self.ok(
            f'{data.get("total_count", len(rows))} code match(es) for {query!r}'
            f'{f" in {repo}" if repo else ""}; showing {len(rows)}.',
            {'query': expression, 'repository': repo, 'count': len(rows),
             'total': data.get('total_count', len(rows)), 'matches': rows})

    def live_list_wiki(self, repo=''):
        """The repository's markdown documentation, because there is no wiki API.

        GitHub keeps wiki pages in a separate ``<repo>.wiki.git`` repository
        that the REST API does not expose at all. Rather than invent pages, this
        reads ``docs/`` and any top-level ``*.md`` and reports them as the
        documentation set, saying so in the summary. The Research employee
        indexes whatever comes back, so the summary has to be true.
        """
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()

        found = []

        def collect(entries, folder):
            for item in entries:
                name = item.get('name', '')
                if item.get('type') != 'file' or not name.lower().endswith('.md'):
                    continue
                found.append({'path': item.get('path', f'{folder}/{name}'),
                              'name': name, 'size': item.get('size', 0),
                              'url': item.get('html_url', ''), 'folder': folder})

        try:
            _status, docs = self._api(f'/repos/{repo}/contents/docs')
            if isinstance(docs, list):
                collect(docs, 'docs')
        except RuntimeError as exc:
            if 'returned 404' not in str(exc):
                raise

        _status, root = self._api(f'/repos/{repo}/contents/')
        if isinstance(root, list):
            collect(root, '')

        # Fetch the text so the indexer gets prose rather than a file listing.
        for item in found[:25]:
            try:
                _status, blob = self._api(
                    f'/repos/{repo}/contents/{item["path"]}')
                raw = base64.b64decode((blob.get('content') or '').encode('ascii', 'ignore'))
                text, is_binary = _decode(raw)
                item['content'] = '' if is_binary else text
            except RuntimeError:
                item['content'] = ''

        return self.ok(
            (f'GitHub\'s API has no wiki endpoint -- wiki pages live in a '
             f'separate .wiki.git repository it does not expose -- so the '
             f'repository\'s markdown documentation is returned instead: '
             f'{len(found)} document(s) from {repo} (docs/ plus top-level '
             f'*.md files).'),
            {'repository': repo, 'source': 'repository markdown',
             'wiki_api_available': False, 'count': len(found),
             'documents': found})

    def live_list_commits(self, repo='', limit=20, path=''):
        repo = self._repo(repo)
        if not repo:
            return self._no_repo()

        params = {'per_page': min(int(limit or 20), 100)}
        if str(path).strip():
            params['path'] = str(path).strip()
        _status, data = self._api(f'/repos/{repo}/commits', params=params)

        rows = []
        for item in (data if isinstance(data, list) else []):
            commit = item.get('commit') or {}
            author = commit.get('author') or {}
            rows.append({
                'sha': (item.get('sha') or '')[:7],
                'full_sha': item.get('sha', ''),
                'message': (commit.get('message') or '').split('\n')[0][:200],
                'author': author.get('name', '') or (item.get('author') or {}).get('login', ''),
                'date': author.get('date', ''),
                'url': item.get('html_url', ''),
            })
        where = f' touching {path}' if str(path).strip() else ''
        return self.ok(f'{len(rows)} commit(s) in {repo}{where}.',
                       {'repository': repo, 'path': path, 'count': len(rows),
                        'commits': rows})

    # -- connection check --------------------------------------------------

    def live_probe(self):
        _status, user = self._api('/user')
        remaining, limit = '?', '?'
        try:
            _status, rate = self._api('/rate_limit')
            core = ((rate.get('resources') or {}).get('core')
                    or rate.get('rate') or {})
            remaining = core.get('remaining', '?')
            limit = core.get('limit', '?')
        except RuntimeError:
            pass
        repo = self._repo('')
        return self.ok(
            (f'Connected to GitHub as {user.get("login", "unknown")} '
             f'({user.get("name") or "no display name"}). '
             f'{remaining} of {limit} API calls left in this window. '
             f'Default repository: {repo or "not set"}.'),
            {'login': user.get('login', ''), 'rate_remaining': remaining,
             'rate_limit': limit, 'default_repository': repo})

    # =======================================================================
    # Demo halves -- the acme/platform fixture
    # =======================================================================

    def _demo_repo(self, repo):
        return self._repo(repo) or DEMO_REPO

    def demo_create_issue(self, repo='', title='', body='', labels=None, assignees=None):
        repo = self._demo_repo(repo)
        if not str(title).strip():
            return self.failure('An issue needs a title. GitHub rejects an empty one.')
        number = _stable(f'{repo}:{title}', 220, 299)
        url = f'https://github.com/{repo}/issues/{number}'
        resolved = self._labels(labels)
        who = [str(name).lstrip('@') for name in (assignees or [])]
        extras = ''
        if resolved:
            extras += ' with labels ' + ', '.join(resolved)
        if who:
            extras += ' assigned to ' + ', '.join(who)
        return self.simulated(
            (f'Would create issue in {repo}: {str(title).strip()!r}{extras}. '
             'Nothing was filed on GitHub.'),
            {'number': number, 'url': url,
             'api_url': f'https://api.github.com/repos/{repo}/issues/{number}',
             'state': 'open', 'title': str(title).strip(), 'repository': repo,
             'labels': resolved, 'assignees': who, 'body': str(body or '')[:2000],
             'simulated': True})

    def demo_update_issue(self, repo='', number=None, title='', body='',
                          state='', labels=None, assignees=None):
        repo = self._demo_repo(repo)
        if not number:
            return self.failure('Which issue? Pass number, e.g. number=201.')
        changed = []
        if str(title).strip():
            changed.append('title')
        if str(body).strip():
            changed.append('body')
        if str(state).strip():
            changed.append('state')
        if labels is not None:
            changed.append('labels')
        if assignees is not None:
            changed.append('assignees')
        if not changed:
            return self.failure(
                'Nothing to change. Pass at least one of title, body, state, '
                'labels or assignees.')
        return self.simulated(
            (f'Would update {repo}#{number}, changing '
             f'{", ".join(changed)}. Nothing was changed on GitHub.'),
            {'number': number, 'repository': repo, 'changed': changed,
             'state': str(state) or 'open',
             'title': str(title) or _demo_issue_title(number),
             'url': f'https://github.com/{repo}/issues/{number}',
             'labels': self._labels(labels) if labels is not None else None,
             'simulated': True})

    def demo_comment_issue(self, repo='', number=None, body=''):
        repo = self._demo_repo(repo)
        if not number:
            return self.failure('Which issue? Pass number, e.g. number=201.')
        if not str(body).strip():
            return self.failure('A comment needs a body.')
        comment_id = _stable(f'{repo}:{number}:{body}', 900000, 999999)
        preview = str(body).strip().replace('\n', ' ')
        if len(preview) > 90:
            preview = preview[:87] + '...'
        return self.simulated(
            (f'Would comment on {repo}#{number}: {preview!r}. '
             'Nothing was posted on GitHub.'),
            {'comment_id': comment_id, 'number': number, 'repository': repo,
             'url': f'https://github.com/{repo}/issues/{number}#issuecomment-{comment_id}',
             'body': str(body), 'simulated': True})

    def demo_close_issue(self, repo='', number=None, reason='completed'):
        repo = self._demo_repo(repo)
        if not number:
            return self.failure('Which issue? Pass number, e.g. number=201.')
        why = str(reason or 'completed').strip().lower().replace(' ', '_')
        if why not in ('completed', 'not_planned'):
            why = 'completed'
        return self.simulated(
            (f'Would close {repo}#{number} as {why.replace("_", " ")}. '
             'The issue is still open on GitHub.'),
            {'number': number, 'repository': repo, 'state': 'closed',
             'reason': why, 'title': _demo_issue_title(number),
             'url': f'https://github.com/{repo}/issues/{number}',
             'simulated': True})

    def demo_list_issues(self, repo='', state='open', limit=20, labels=''):
        repo = self._demo_repo(repo)
        wanted = str(state or 'open').lower()
        rows = []
        for item in DEMO_ISSUES:
            if str(labels).strip():
                asked = {name.strip().lower()
                         for name in str(labels).replace(';', ',').split(',')
                         if name.strip()}
                if not asked & {name.lower() for name in item['labels']}:
                    continue
            rows.append(_demo_issue_row(repo, item))
        if wanted == 'closed':
            rows = []
        rows = rows[:max(1, int(limit or 20))]
        counts = {}
        for item in rows:
            counts[item['kind']] = counts.get(item['kind'], 0) + 1
        shape = ', '.join(f'{count} {kind}' for kind, count in sorted(counts.items()))
        return self.simulated(
            (f'Simulated backlog for {repo}: {len(rows)} {wanted} issue(s)'
             f'{f" ({shape})" if shape else ""}. Pull requests are excluded, as '
             'they would be against the real API. This is fixture data, not '
             f'{repo} as it stands today.'),
            {'repository': repo, 'state': wanted, 'count': len(rows),
             'pull_requests_excluded': 3, 'issues': rows, 'simulated': True})

    def demo_get_issue(self, repo='', number=None):
        repo = self._demo_repo(repo)
        if not number:
            return self.failure('Which issue? Pass number, e.g. number=201.')
        match = next((item for item in DEMO_ISSUES if item['number'] == int(number)), None)
        if match is None:
            return self.simulated(
                (f'{repo}#{number} is not in the demo fixture, which holds issues '
                 f'{", ".join(str(item["number"]) for item in DEMO_ISSUES)}. '
                 'Nothing was read from GitHub.'),
                {'repository': repo, 'number': int(number), 'found': False,
                 'available': [item['number'] for item in DEMO_ISSUES],
                 'simulated': True})
        row = _demo_issue_row(repo, match)
        return self.simulated(
            f'Simulated {repo}#{number} [open] {row["title"]}. Fixture data.',
            {'repository': repo, 'issue': row, 'simulated': True})

    def demo_list_pull_requests(self, repo='', state='open', limit=20):
        repo = self._demo_repo(repo)
        if str(state or 'open').lower() == 'closed':
            rows = []
        else:
            rows = [_demo_pull_row(repo, item) for item in DEMO_PULLS]
        rows = rows[:max(1, int(limit or 20))]
        return self.simulated(
            (f'Simulated {len(rows)} open pull request(s) in {repo}: one large '
             'change with 24 files and a conflict, one two-file fix already '
             'approved, and one with a failing check. Fixture data, not the real '
             'repository.'),
            {'repository': repo, 'state': str(state or 'open'), 'count': len(rows),
             'pull_requests': rows, 'simulated': True})

    def demo_get_pull_request(self, repo='', number=None):
        repo = self._demo_repo(repo)
        if not number:
            return self.failure('Which pull request? Pass number, e.g. number=214.')
        match = next((item for item in DEMO_PULLS if item['number'] == int(number)), None)
        if match is None:
            return self.simulated(
                (f'{repo}#{number} is not in the demo fixture, which holds pull '
                 f'requests {", ".join(str(item["number"]) for item in DEMO_PULLS)}. '
                 'Nothing was read from GitHub.'),
                {'repository': repo, 'number': int(number), 'found': False,
                 'available': [item['number'] for item in DEMO_PULLS],
                 'simulated': True})
        row = _demo_pull_row(repo, match, detailed=True)
        return self.simulated(
            (f'Simulated {repo}#{number} {row["title"]}: {row["changed_files"]} '
             f'file(s), +{row["additions"]}/-{row["deletions"]}, '
             f'{row["head_branch"]} into {row["base_branch"]}, merge state '
             f'{row["mergeable_state"]}, checks {row["checks"]}. '
             f'{row["size_warning"]} Fixture data.'),
            {'repository': repo, 'pull_request': row, 'simulated': True})

    def demo_list_pull_request_files(self, repo='', number=None, limit=100):
        repo = self._demo_repo(repo)
        if not number:
            return self.failure('Which pull request? Pass number, e.g. number=214.')
        entries = DEMO_PULL_FILES.get(int(number))
        if not entries:
            return self.simulated(
                (f'No file list in the demo fixture for {repo}#{number}. The '
                 f'fixture covers {", ".join(str(key) for key in sorted(DEMO_PULL_FILES))}.'),
                {'repository': repo, 'number': int(number), 'files': [],
                 'found': False, 'simulated': True})
        rows, additions, deletions = [], 0, 0
        for filename, status, plus, minus in entries[:max(1, int(limit or 100))]:
            additions += plus
            deletions += minus
            rows.append({
                'filename': filename, 'status': status,
                'additions': plus, 'deletions': minus,
                'patch': (f'@@ demo patch for {filename} @@\n'
                          f'+{plus} line(s) added, -{minus} line(s) removed. '
                          'The full patch is not part of the fixture.'),
                'patch_truncated': True,
            })
        return self.simulated(
            (f'Simulated {len(rows)} changed file(s) in {repo}#{number}, '
             f'+{additions}/-{deletions}. Patches are placeholders, not real '
             'diffs.'),
            {'repository': repo, 'number': int(number), 'count': len(rows),
             'additions': additions, 'deletions': deletions, 'files': rows,
             'simulated': True})

    def demo_get_repo(self, repo=''):
        repo = self._demo_repo(repo)
        return self.simulated(
            (f'Simulated {repo}: the Acme marketing and operations platform. '
             'Python, 8 open issues and 3 open pull requests, default branch '
             'main, 142 stars. Fixture data, not the real repository.'),
            {'repository': {
                'full_name': repo,
                'description': 'The Acme marketing and operations platform.',
                'language': 'Python', 'open_issues': 11, 'issues_only': 8,
                'open_pull_requests': 3, 'default_branch': 'main',
                'stars': 142, 'forks': 17, 'private': True,
                'url': f'https://github.com/{repo}',
                'pushed_at': '2026-09-05T16:28:00Z',
                'topics': ['django', 'marketing', 'automation']},
             'simulated': True})

    def demo_read_file(self, repo='', path='', ref=''):
        repo = self._demo_repo(repo)
        wanted = str(path or '').strip().lstrip('/')
        if not wanted:
            return self.failure('Which file? Pass path, e.g. path=docs/architecture.md.')

        if wanted.lower() in ('readme.md', 'readme'):
            body = DEMO_README
        elif wanted in DEMO_DOCS:
            body = DEMO_DOCS[wanted]
        elif f'docs/{wanted}' in DEMO_DOCS:
            wanted = f'docs/{wanted}'
            body = DEMO_DOCS[wanted]
        elif wanted in ('docs', 'docs/'):
            return self.simulated(
                f'docs/ in {repo} holds {len(DEMO_DOCS)} markdown document(s). '
                'Fixture data.',
                {'repository': repo, 'path': 'docs', 'is_directory': True,
                 'entries': [name.split('/')[-1] for name in sorted(DEMO_DOCS)],
                 'simulated': True})
        elif wanted.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip',
                                      '.woff', '.woff2', '.ico')):
            return self.simulated(
                (f'{wanted} would be a binary file, so there is no text to '
                 'return. Nothing readable was invented in its place.'),
                {'repository': repo, 'path': wanted, 'is_binary': True,
                 'content': '', 'simulated': True})
        else:
            return self.simulated(
                (f'{wanted} is not in the demo fixture. It holds README.md and '
                 f'{", ".join(sorted(DEMO_DOCS))}. Nothing was read from GitHub.'),
                {'repository': repo, 'path': wanted, 'found': False,
                 'available': ['README.md'] + sorted(DEMO_DOCS),
                 'simulated': True})

        return self.simulated(
            (f'Simulated read of {wanted} from {repo} '
             f'({len(body)} character(s) of fixture prose, not the real file).'),
            {'repository': repo, 'path': wanted, 'is_binary': False,
             'size': len(body), 'sha': f'{_stable(wanted, 0, 0xffffff):06x}',
             'ref': str(ref) or 'main', 'content': body,
             'url': f'https://github.com/{repo}/blob/main/{wanted}',
             'simulated': True})

    def demo_search_code(self, repo='', query='', limit=20):
        repo = self._demo_repo(repo)
        if not str(query).strip():
            return self.failure('What should be searched for? Pass query.')
        needle = str(query).strip().lower()
        rows = []
        for name, body in sorted(DEMO_DOCS.items()):
            if needle in body.lower() or needle in name.lower():
                line = next((line for line in body.split('\n')
                             if needle in line.lower()), '')
                rows.append({'path': name, 'name': name.split('/')[-1],
                             'repository': repo,
                             'url': f'https://github.com/{repo}/blob/main/{name}',
                             'match': line.strip()[:200],
                             'score': round(1.0 - 0.1 * len(rows), 2)})
        rows = rows[:max(1, int(limit or 20))]
        return self.simulated(
            (f'Simulated code search for {query!r} in {repo}: {len(rows)} '
             'match(es), searched across the fixture documentation only. A live '
             'search would cover the whole repository index.'),
            {'query': f'{query} repo:{repo}', 'repository': repo,
             'count': len(rows), 'total': len(rows), 'matches': rows,
             'simulated': True})

    def demo_list_wiki(self, repo=''):
        repo = self._demo_repo(repo)
        documents = []
        for name in sorted(DEMO_DOCS):
            body = DEMO_DOCS[name]
            documents.append({
                'path': name, 'name': name.split('/')[-1],
                'title': body.split('\n')[0].lstrip('# ').strip(),
                'folder': 'docs', 'size': len(body),
                'url': f'https://github.com/{repo}/blob/main/{name}',
                'content': body,
                'words': len(body.split()),
            })
        return self.simulated(
            (f'GitHub\'s API has no wiki endpoint, so the repository\'s markdown '
             f'documentation stands in for it. Simulated {len(documents)} '
             f'document(s) from {repo}/docs: '
             f'{", ".join(item["title"] for item in documents)}. '
             'Fixture prose, safe to index but not the real repository.'),
            {'repository': repo, 'source': 'repository markdown',
             'wiki_api_available': False, 'count': len(documents),
             'documents': documents, 'simulated': True})

    def demo_list_commits(self, repo='', limit=20, path=''):
        repo = self._demo_repo(repo)
        rows = []
        for sha, author, date, message in DEMO_COMMITS:
            rows.append({'sha': sha, 'full_sha': f'{sha}{"0" * 33}',
                         'message': message, 'author': author, 'date': date,
                         'url': f'https://github.com/{repo}/commit/{sha}'})
        rows = rows[:max(1, int(limit or 20))]
        where = f' (a live call would filter to {path})' if str(path).strip() else ''
        return self.simulated(
            f'Simulated {len(rows)} recent commit(s) in {repo}{where}. Fixture data.',
            {'repository': repo, 'path': path, 'count': len(rows),
             'commits': rows, 'simulated': True})


# ===========================================================================
# Small helpers
# ===========================================================================

def _decode(raw):
    """(text, is_binary). Honest about binary rather than returning rubbish."""
    if b'\x00' in raw[:8192]:
        return '', True
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError:
        return '', True
    return raw.decode('utf-8', errors='replace'), False


def _size_warning(files, lines):
    if files >= 20 or lines >= 800:
        return ('Large enough to be risky: review quality falls off sharply '
                'above roughly twenty files, so this wants two reviewers or a '
                'split.')
    if files >= 8 or lines >= 300:
        return 'Moderate size. One careful reviewer is enough.'
    return 'Small and quick to review.'


def _demo_issue_title(number):
    match = next((item for item in DEMO_ISSUES if item['number'] == int(number)), None)
    return match['title'] if match else 'Issue not in the demo fixture'


def _demo_issue_row(repo, item):
    return {
        'number': item['number'], 'title': item['title'], 'state': 'open',
        'kind': item['kind'], 'labels': list(item['labels']),
        'assignees': list(item['assignees']),
        'author': 'sara-l', 'comments': item['comments'],
        'created_at': item['created_at'], 'updated_at': item['updated_at'],
        'body': item['body'],
        'url': f'https://github.com/{repo}/issues/{item["number"]}',
        'api_url': f'https://api.github.com/repos/{repo}/issues/{item["number"]}',
    }


def _demo_pull_row(repo, item, detailed=False):
    row = {
        'number': item['number'], 'title': item['title'], 'state': 'open',
        'draft': item['draft'], 'author': item['author'],
        'head': item['head'], 'base': item['base'],
        'head_branch': item['head'], 'base_branch': item['base'],
        'changed_files': item['changed_files'], 'additions': item['additions'],
        'deletions': item['deletions'], 'mergeable_state': item['mergeable_state'],
        'checks': item['checks'], 'review_state': item['review_state'],
        'created_at': item['created_at'], 'updated_at': item['updated_at'],
        'url': f'https://github.com/{repo}/pull/{item["number"]}',
        'labels': [],
    }
    if detailed:
        row['body'] = item['body']
        row['comments'] = item['comments']
        row['review_comments'] = item['comments']
        row['mergeable'] = item['mergeable_state'] == 'clean'
        row['head_sha'] = f'{_stable(item["head"], 0, 0xfffffff):07x}'
        row['size_warning'] = _size_warning(
            item['changed_files'], item['additions'] + item['deletions'])
    return row
