"""Jira: the project board the AI employees actually move.

WHAT THIS DOES FOR THE WORKFORCE
--------------------------------
Jira is where planned work lives, so this is the integration that lets an AI
employee do the unglamorous half of project management: file the ticket with
the right type and priority, move it across the board when the state genuinely
changed, put it in the right sprint, and answer "what is in progress" with the
board rather than with a guess. Because a transition is a real state change on
a real board, the platform routes it through the approval queue first; this
connector is only ever the hands, never the decision.

WHICH EMPLOYEES USE IT
----------------------
``Engineering``   creates and updates issues, transitions them, and reads the
                  sprint to answer questions about capacity and progress.
``Support``       raises a bug from a customer ticket with the reproduction
                  steps already written, and links it back to the ticket.
``Operations``    plans sprints, adds issues to them, and reports on status
                  spread across the project.

THE CREDENTIAL YOU NEED
-----------------------
Jira Cloud uses HTTP Basic authentication with an email address and an API
token -- not your password.

  1. Sign in to Jira and go to
     id.atlassian.com/manage-profile/security/api-tokens.
  2. Create an API token and copy it. It is shown once.
  3. On the Integrations page fill in Site URL
     (https://yourcompany.atlassian.net), Account email (the address that owns
     the token) and API token.
  4. Set the default project key (for example ENG) so an employee does not
     have to name the project on every call, and the board id if you want
     sprint operations -- the board id is the number in the board URL after
     ``rapidView=``.

The token inherits the permissions of that account, so an account that cannot
transition an issue by hand cannot transition it through here either. That is
deliberate.

THE ATLASSIAN DOCUMENT FORMAT TRAP
----------------------------------
Jira REST v3 does not accept plain text in a description or a comment. It
expects Atlassian Document Format, a small JSON document tree. Sending a
string returns a 400 with a message that does not mention ADF, which is the
single most common reason a Jira integration appears broken. ``_adf`` wraps
plain text into a valid document with one paragraph per line, and
``_adf_to_text`` unwraps it again on the way back, so the rest of the platform
only ever handles strings.

WHAT DEMO MODE DOES INSTEAD
---------------------------
Without a site, email and token every operation is simulated against a
deterministic project ``ENG``: ten issues, ENG-101 to ENG-110, spread across
To Do, In Progress, In Review and Done, one active sprint with a goal and
dates, and a realistic transition list. Keys are stable, so simulating a
create and then simulating a transition of the returned key agree with each
other. Every simulated result is flagged and labelled as such.
"""

import base64
import hashlib
import urllib.error

from . import register
from .base import CallResult, ConfigField, Connector


# ===========================================================================
# Atlassian Document Format
# ===========================================================================

def _adf(text):
    """Wrap plain text into a valid ADF document, one paragraph per line.

    Jira v3 rejects a plain string in a description or comment field. An empty
    paragraph is used for a blank line so that the shape of a written
    reproduction ("steps", blank line, "expected") survives the trip.
    """
    lines = str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    content = []
    for line in lines:
        if line.strip():
            content.append({'type': 'paragraph',
                            'content': [{'type': 'text', 'text': line}]})
        else:
            content.append({'type': 'paragraph', 'content': []})
    if not content:
        content = [{'type': 'paragraph', 'content': []}]
    return {'type': 'doc', 'version': 1, 'content': content}


def _adf_to_text(node):
    """Flatten an ADF document back to plain text.

    Read paths get ADF too, and the knowledge base and the chat window both
    want prose, so the reverse trip matters as much as the forward one.
    """
    if node in (None, '', {}):
        return ''
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return '\n'.join(part for part in (_adf_to_text(item) for item in node) if part)

    kind = node.get('type', '')
    if kind == 'text':
        return node.get('text', '')
    if kind == 'hardBreak':
        return '\n'
    if kind == 'mention':
        return '@' + ((node.get('attrs') or {}).get('text', '') or 'someone')
    if kind == 'emoji':
        return (node.get('attrs') or {}).get('shortName', '')

    children = node.get('content') or []
    if kind in ('paragraph', 'heading', 'blockquote', 'codeBlock', 'panel'):
        inner = ''.join(_adf_to_text(child) for child in children)
        if kind == 'heading':
            level = int((node.get('attrs') or {}).get('level', 1))
            return f'{"#" * level} {inner}'
        return inner
    if kind == 'listItem':
        return '- ' + ''.join(_adf_to_text(child) for child in children)
    if kind == 'doc':
        # Blank lines are kept here, because the shape of a written
        # reproduction -- steps, blank line, expected -- is part of its meaning.
        # Runs of them are collapsed so a heavily spaced description does not
        # come back as mostly whitespace.
        lines, blank = [], False
        for child in children:
            part = _adf_to_text(child)
            if part.strip():
                lines.append(part)
                blank = False
            elif not blank and lines:
                lines.append('')
                blank = True
        return '\n'.join(lines).strip()
    if kind in ('bulletList', 'orderedList', 'tableCell', 'tableRow', 'table'):
        return '\n'.join(part for part in (_adf_to_text(child) for child in children) if part)
    return '\n'.join(part for part in (_adf_to_text(child) for child in children) if part)


def _stable(text, low, high):
    digest = hashlib.sha256(str(text).encode('utf-8')).hexdigest()
    return low + int(digest[:8], 16) % max(1, (high - low + 1))


# ===========================================================================
# Deterministic fixture project used by every demo half
# ===========================================================================

DEMO_PROJECT = 'ENG'

DEMO_ISSUES = (
    {'key': 'ENG-101', 'summary': 'Export times out for accounts over 50k rows',
     'type': 'Bug', 'status': 'In Progress', 'priority': 'Highest',
     'assignee': 'Priya Nair', 'reporter': 'Sara Lombardi', 'points': 5,
     'labels': ['performance', 'customer-reported'], 'sprint': 'ENG Sprint 24',
     'created': '2026-08-14T09:12:00.000+1000',
     'updated': '2026-09-02T11:40:00.000+1000',
     'description': ('Three enterprise workspaces report the CSV export never '
                     'finishes.\n\nSteps: open a campaign table with more than '
                     '50,000 rows and choose Export.\n\nExpected: a file '
                     'downloads.\n\nActual: the request holds a worker for sixty '
                     'seconds and returns 504.')},
    {'key': 'ENG-102', 'summary': 'Add SAML single sign-on for enterprise plans',
     'type': 'Story', 'status': 'In Progress', 'priority': 'High',
     'assignee': 'Marcus Tan', 'reporter': 'Dana Okoye', 'points': 13,
     'labels': ['enterprise', 'security'], 'sprint': 'ENG Sprint 24',
     'created': '2026-07-30T14:02:00.000+1000',
     'updated': '2026-09-04T08:15:00.000+1000',
     'description': ('Four of six enterprise deals require SAML.\n\nIn scope: '
                     'service-provider initiated login, metadata exchange by '
                     'URL, group to role mapping.\n\nOut of scope: SCIM '
                     'provisioning, tracked as ENG-110.')},
    {'key': 'ENG-103', 'summary': 'Webhook retries duplicate deliveries on 502',
     'type': 'Bug', 'status': 'In Review', 'priority': 'High',
     'assignee': 'Dan Kovac', 'reporter': 'Priya Nair', 'points': 3,
     'labels': ['integrations'], 'sprint': 'ENG Sprint 24',
     'created': '2026-08-21T16:45:00.000+1000',
     'updated': '2026-09-01T10:05:00.000+1000',
     'description': ('The delivery worker retries on any non-2xx without '
                     'recording that the body was accepted, so a receiver '
                     'returning 502 after processing gets the event twice. Fix '
                     'is an idempotency key on the delivery record.')},
    {'key': 'ENG-104', 'summary': 'Upgrade to Django 6.1 and drop the psycopg patch',
     'type': 'Task', 'status': 'Done', 'priority': 'Medium',
     'assignee': 'Dan Kovac', 'reporter': 'Priya Nair', 'points': 2,
     'labels': ['dependencies'], 'sprint': 'ENG Sprint 24',
     'created': '2026-08-02T08:30:00.000+1000',
     'updated': '2026-08-28T13:22:00.000+1000',
     'description': ('Two minor versions behind and carrying a local psycopg '
                     'patch that upstream fixed in 3.2.4. Landed in '
                     'commit 35ce70d.')},
    {'key': 'ENG-105', 'summary': 'Bulk edit for campaign records',
     'type': 'Story', 'status': 'To Do', 'priority': 'Medium',
     'assignee': 'Sara Lombardi', 'reporter': 'Dana Okoye', 'points': 8,
     'labels': ['ux'], 'sprint': 'ENG Sprint 24',
     'created': '2026-08-19T11:00:00.000+1000',
     'updated': '2026-09-03T15:48:00.000+1000',
     'description': ('Marketing operations edit the same three fields across '
                     'dozens of campaigns one row at a time. Needs an undo '
                     'window: a mistaken bulk edit costs far more than a '
                     'mistaken single edit.')},
    {'key': 'ENG-106', 'summary': 'Scheduling screen shows UTC not workspace timezone',
     'type': 'Bug', 'status': 'In Review', 'priority': 'Low',
     'assignee': 'Sara Lombardi', 'reporter': 'Tomas Reyes', 'points': 1,
     'labels': ['ux', 'quick-win'], 'sprint': 'ENG Sprint 24',
     'created': '2026-08-25T07:20:00.000+1000',
     'updated': '2026-08-30T09:10:00.000+1000',
     'description': ('The scheduling panel renders the stored UTC value with no '
                     'conversion. Stored data is correct; the template is '
                     'missing the localtime filter.')},
    {'key': 'ENG-107', 'summary': 'Split settings into per-environment modules',
     'type': 'Task', 'status': 'To Do', 'priority': 'Medium',
     'assignee': 'Priya Nair', 'reporter': 'Priya Nair', 'points': 5,
     'labels': ['tech-debt'], 'sprint': '',
     'created': '2026-07-24T10:40:00.000+1000',
     'updated': '2026-08-27T12:00:00.000+1000',
     'description': ('One 600 line settings file with nested conditionals '
                     'decides behaviour for three environments. Every '
                     'deployment incident this quarter touched it.')},
    {'key': 'ENG-108', 'summary': 'Weekly digest email for workspace owners',
     'type': 'Story', 'status': 'To Do', 'priority': 'Low',
     'assignee': '', 'reporter': 'Dana Okoye', 'points': 5,
     'labels': ['notifications'], 'sprint': '',
     'created': '2026-09-01T09:05:00.000+1000',
     'updated': '2026-09-05T08:55:00.000+1000',
     'description': ('One email on Monday summarising campaigns published, '
                     'approvals pending and anything that failed. Must be '
                     'switchable off per user and must never carry customer '
                     'personal data.')},
    {'key': 'ENG-109', 'summary': 'Rotate the staging database credentials',
     'type': 'Task', 'status': 'Done', 'priority': 'High',
     'assignee': 'Dan Kovac', 'reporter': 'Marcus Tan', 'points': 1,
     'labels': ['security', 'operations'], 'sprint': 'ENG Sprint 23',
     'created': '2026-07-18T13:10:00.000+1000',
     'updated': '2026-08-06T15:25:00.000+1000',
     'description': ('Quarterly rotation. Completed and recorded in the '
                     'secrets register.')},
    {'key': 'ENG-110', 'summary': 'SCIM provisioning for enterprise directories',
     'type': 'Epic', 'status': 'To Do', 'priority': 'Medium',
     'assignee': 'Marcus Tan', 'reporter': 'Dana Okoye', 'points': 21,
     'labels': ['enterprise'], 'sprint': '',
     'created': '2026-08-05T09:00:00.000+1000',
     'updated': '2026-09-04T10:30:00.000+1000',
     'description': ('Follows SAML. Automatic user creation, update and '
                     'deactivation from the customer directory. Deliberately '
                     'kept out of the SAML story so the first release can '
                     'ship.')},
)

DEMO_TRANSITIONS = (
    {'id': '11', 'name': 'To Do', 'to': 'To Do'},
    {'id': '21', 'name': 'In Progress', 'to': 'In Progress'},
    {'id': '31', 'name': 'In Review', 'to': 'In Review'},
    {'id': '41', 'name': 'Done', 'to': 'Done'},
    {'id': '51', 'name': 'Blocked', 'to': 'Blocked'},
)

DEMO_SPRINT = {
    'id': 224, 'name': 'ENG Sprint 24', 'state': 'active',
    'startDate': '2026-08-25T09:00:00.000+1000',
    'endDate': '2026-09-08T17:00:00.000+1000',
    'goal': ('Ship the streaming export and land the first SAML slice behind a '
             'flag.'),
}

DEMO_PROJECTS = (
    {'key': 'ENG', 'name': 'Engineering', 'type': 'software', 'lead': 'Priya Nair'},
    {'key': 'OPS', 'name': 'Operations', 'type': 'business', 'lead': 'Dan Kovac'},
    {'key': 'SUP', 'name': 'Customer Support', 'type': 'service_desk',
     'lead': 'Tomas Reyes'},
)


@register
class JiraConnector(Connector):
    """Issues, transitions, sprints and projects over the Jira Cloud REST API."""

    key = 'jira'
    name = 'Jira'
    description = ('Creates, updates and transitions Jira issues, manages '
                   'sprints and reports on project status.')
    category = 'development'
    icon = 'fa-jira'
    color = '#0052cc'
    docs_url = 'https://developer.atlassian.com/cloud/jira/platform/rest/v3/'

    config_fields = (
        ConfigField(
            'site_url', 'Site URL',
            help_text='e.g. https://yourcompany.atlassian.net',
            field_type='url', required=True,
            placeholder='https://yourcompany.atlassian.net'),
        ConfigField(
            'email', 'Account email',
            help_text=('The Atlassian account that owns the API token. Its '
                       'permissions are the permissions this integration has.'),
            required=True, placeholder='you@yourcompany.com'),
        ConfigField(
            'api_token', 'API token',
            help_text=('Create one at '
                       'id.atlassian.com/manage-profile/security/api-tokens. '
                       'Not your account password.'),
            field_type='password', required=True, secret=True),
        ConfigField(
            'default_project_key', 'Default project key',
            help_text='e.g. ENG', placeholder='ENG'),
        ConfigField(
            'default_issue_type', 'Default issue type',
            help_text=('Used when an employee does not say. Must exist in the '
                       'project, or Jira rejects the create.'),
            default='Task'),
        ConfigField(
            'board_id', 'Board id',
            help_text=('Needed for sprints. Find it in the board URL, after '
                       'rapidView= or /boards/.'),
            placeholder='42'),
    )

    operations = (
        'create_issue', 'update_issue', 'transition_issue', 'assign_issue',
        'comment_issue', 'list_issues', 'get_issue', 'list_projects',
        'list_transitions', 'create_sprint', 'list_sprints', 'add_to_sprint',
    )

    # -- plumbing ----------------------------------------------------------

    def _site(self):
        site = str(self.setting('site_url', '') or '').strip().rstrip('/')
        # A site pasted with /wiki or /jira on the end is a common paste error.
        for suffix in ('/wiki', '/jira', '/browse'):
            if site.endswith(suffix):
                site = site[: -len(suffix)]
        return site

    def _headers(self):
        email = str(self.setting('email', '') or '')
        token = str(self.setting('api_token', '') or '')
        pair = base64.b64encode(f'{email}:{token}'.encode()).decode('ascii')
        return {'Authorization': f'Basic {pair}',
                'Accept': 'application/json'}

    def _project(self, project):
        return (str(project or '').strip().upper()
                or str(self.setting('default_project_key', '') or '').strip().upper())

    def _no_project(self):
        return self.failure(
            'No project was given and no default project key is set. Open the '
            'Integrations page, choose Jira, and set the default project key '
            '(for example ENG), or pass project explicitly.')

    def _board(self, board_id):
        return (str(board_id or '').strip()
                or str(self.setting('board_id', '') or '').strip())

    def _api(self, path, *, method='GET', payload=None, params=None):
        url = f'{self._site()}{path}'
        try:
            status, data = self.request_json(
                url, method=method, headers=self._headers(),
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
        if code == 400:
            return ('Jira rejected the request (400). The usual causes are an '
                    'issue type that does not exist in the project, a field the '
                    'project does not have on its create screen, or plain text '
                    'sent where Atlassian Document Format is required. '
                    f'Jira said: {body}')
        if code == 401:
            return ('Jira rejected the credentials (401). Check the account '
                    'email matches the account that created the API token, and '
                    'that the token has not been revoked at '
                    'id.atlassian.com/manage-profile/security/api-tokens.')
        if code == 403:
            return ('Jira refused the action (403). The credentials are valid '
                    'but the account lacks the permission -- typically Create '
                    'Issues, Transition Issues or Assign Issues on this '
                    f'project. Ask a Jira administrator to grant it. {body}')
        if code == 404:
            return (f'Jira returned 404 for {path}. Either the issue key or '
                    'project does not exist, or the account cannot see it. '
                    'Confirm the Site URL has no trailing path and that the key '
                    'is spelled correctly, including the project prefix.')
        if code == 429:
            return ('Jira is rate limiting this account (429). Wait a minute '
                    'and try again; nothing was changed.')
        return f'Jira returned HTTP {code} {exc.reason}. {body}'.strip()

    def _browse(self, key):
        return f'{self._site()}/browse/{key}'

    def _issue_row(self, item):
        fields = item.get('fields') or {}
        status = ((fields.get('status') or {}).get('name', ''))
        assignee = (fields.get('assignee') or {})
        reporter = (fields.get('reporter') or {})
        return {
            'key': item.get('key', ''),
            'id': item.get('id', ''),
            'summary': fields.get('summary', ''),
            'status': status,
            'status_category': (((fields.get('status') or {}).get('statusCategory')
                                 or {}).get('name', '')),
            'type': ((fields.get('issuetype') or {}).get('name', '')),
            'priority': ((fields.get('priority') or {}).get('name', '')),
            'assignee': assignee.get('displayName', ''),
            'assignee_id': assignee.get('accountId', ''),
            'reporter': reporter.get('displayName', ''),
            'labels': fields.get('labels') or [],
            'due_date': fields.get('duedate') or '',
            'created': fields.get('created', ''),
            'updated': fields.get('updated', ''),
            'description': _adf_to_text(fields.get('description'))[:4000],
            'url': self._browse(item.get('key', '')),
        }

    # =======================================================================
    # Issues -- live
    # =======================================================================

    def live_create_issue(self, project='', summary='', description='', issue_type='',
                          priority='', assignee='', labels=None, parent=''):
        project = self._project(project)
        if not project:
            return self._no_project()
        if not str(summary).strip():
            return self.failure('A Jira issue needs a summary.')

        kind = (str(issue_type).strip()
                or str(self.setting('default_issue_type', 'Task')))
        fields = {
            'project': {'key': project},
            'summary': str(summary).strip()[:255],
            'issuetype': {'name': kind},
            # ADF, not a string. A plain string here is a 400 every time.
            'description': _adf(description),
        }
        if str(priority).strip():
            fields['priority'] = {'name': str(priority).strip()}
        if labels:
            fields['labels'] = [str(item).strip().replace(' ', '-')
                                for item in labels if str(item).strip()]
        if str(parent).strip():
            fields['parent'] = {'key': str(parent).strip().upper()}
        if str(assignee).strip():
            account = self._resolve_account(assignee)
            if account:
                fields['assignee'] = {'accountId': account}

        _status, data = self._api('/rest/api/3/issue', method='POST',
                                  payload={'fields': fields})
        key = data.get('key', '')
        return self.ok(
            f'Created {key} in {project}: {str(summary).strip()}. {self._browse(key)}',
            {'key': key, 'id': data.get('id', ''), 'url': self._browse(key),
             'project': project, 'issue_type': kind,
             'summary': str(summary).strip()})

    def live_update_issue(self, key='', summary='', description='', priority='',
                          labels=None, due_date=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')

        fields = {}
        if str(summary).strip():
            fields['summary'] = str(summary).strip()[:255]
        if str(description).strip():
            fields['description'] = _adf(description)
        if str(priority).strip():
            fields['priority'] = {'name': str(priority).strip()}
        if labels is not None:
            fields['labels'] = [str(item).strip().replace(' ', '-')
                                for item in (labels or []) if str(item).strip()]
        if str(due_date).strip():
            fields['duedate'] = str(due_date).strip()[:10]

        if not fields:
            return self.failure(
                'Nothing to change. Pass at least one of summary, description, '
                'priority, labels or due_date.')

        self._api(f'/rest/api/3/issue/{key}', method='PUT', payload={'fields': fields})
        return self.ok(
            f'Updated {key} ({", ".join(sorted(fields))}). {self._browse(key)}',
            {'key': key, 'changed': sorted(fields), 'url': self._browse(key)})

    def live_transition_issue(self, key='', status=''):
        """Move an issue by finding the transition that reaches the wanted state.

        Jira transitions are named per workflow, so there is no fixed id for
        "In Progress". The available transitions are read first, matched on the
        target status name and on the transition's own name, and only then
        posted. When nothing matches, the useful answer is the list of states
        this issue can actually reach, so that is what comes back.
        """
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        if not str(status).strip():
            return self.failure(
                'Which state should it move to? Pass status, e.g. '
                "status='In Progress'.")

        wanted = _normalise(status)
        _code, data = self._api(f'/rest/api/3/issue/{key}/transitions')
        available = data.get('transitions') or []

        chosen = None
        for item in available:
            target = ((item.get('to') or {}).get('name', ''))
            if _normalise(target) == wanted or _normalise(item.get('name', '')) == wanted:
                chosen = item
                break
        if chosen is None:
            for item in available:
                target = ((item.get('to') or {}).get('name', ''))
                if wanted in _normalise(target) or _normalise(target) in wanted:
                    chosen = item
                    break

        if chosen is None:
            names = [((item.get('to') or {}).get('name') or item.get('name', ''))
                     for item in available]
            listed = ', '.join(names) or ('none at all, which usually means the '
                                          'account lacks Transition Issues '
                                          'permission on this project')
            return self.failure(
                (f'{key} cannot move to {status!r} from where it is now. The '
                 f'transitions available on this issue are: {listed}.'),
                {'key': key, 'requested': str(status),
                 'available': names, 'url': self._browse(key)})

        self._api(f'/rest/api/3/issue/{key}/transitions', method='POST',
                  payload={'transition': {'id': chosen.get('id')}})
        target = ((chosen.get('to') or {}).get('name') or chosen.get('name', ''))
        return self.ok(
            f'Moved {key} to {target}. {self._browse(key)}',
            {'key': key, 'status': target, 'transition_id': chosen.get('id'),
             'transition_name': chosen.get('name', ''), 'url': self._browse(key)})

    def live_assign_issue(self, key='', assignee=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        if not str(assignee).strip():
            return self.failure(
                'Who should it go to? Pass assignee as an email address or a '
                "display name, or 'unassigned' to clear it.")

        if str(assignee).strip().lower() in ('none', 'unassigned', 'nobody'):
            self._api(f'/rest/api/3/issue/{key}/assignee', method='PUT',
                      payload={'accountId': None})
            return self.ok(f'Cleared the assignee on {key}.',
                           {'key': key, 'assignee': '', 'url': self._browse(key)})

        account = self._resolve_account(assignee)
        if not account:
            return self.failure(
                f'No Jira account matches {assignee!r}. Jira needs an accountId, '
                'and the user search found nobody. Check the spelling, or use '
                'the exact email address of the account. Note that accounts '
                'with a private profile do not appear in the search at all, in '
                'which case the accountId has to be supplied directly.')

        self._api(f'/rest/api/3/issue/{key}/assignee', method='PUT',
                  payload={'accountId': account})
        return self.ok(f'Assigned {key} to {assignee}. {self._browse(key)}',
                       {'key': key, 'assignee': str(assignee),
                        'account_id': account, 'url': self._browse(key)})

    def _resolve_account(self, who):
        """An email address or display name turned into a Jira accountId."""
        query = str(who or '').strip()
        if not query:
            return ''
        # An accountId is an opaque token with no '@' and no spaces. When one is
        # handed straight in, there is nothing to look up.
        if '@' not in query and ' ' not in query and len(query) >= 20:
            return query
        try:
            _code, data = self._api('/rest/api/3/user/search',
                                    params={'query': query, 'maxResults': 10})
        except RuntimeError:
            return ''
        people = data if isinstance(data, list) else []
        lowered = query.lower()
        for person in people:
            if (person.get('emailAddress') or '').lower() == lowered:
                return person.get('accountId', '')
        for person in people:
            if (person.get('displayName') or '').lower() == lowered:
                return person.get('accountId', '')
        return people[0].get('accountId', '') if people else ''

    def live_comment_issue(self, key='', body=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        if not str(body).strip():
            return self.failure('A comment needs a body.')

        _code, data = self._api(f'/rest/api/3/issue/{key}/comment', method='POST',
                                payload={'body': _adf(body)})
        return self.ok(
            f'Commented on {key}. {self._browse(key)}',
            {'key': key, 'comment_id': data.get('id', ''),
             'author': ((data.get('author') or {}).get('displayName', '')),
             'created': data.get('created', ''), 'url': self._browse(key)})

    def live_list_issues(self, project='', jql='', limit=20, status=''):
        expression = str(jql or '').strip()
        project = self._project(project)
        if not expression:
            if not project:
                return self._no_project()
            clauses = [f'project = {project}']
            if str(status).strip():
                clauses.append(f'status = "{_escape_jql(status)}"')
            expression = ' AND '.join(clauses) + ' ORDER BY updated DESC'

        payload = {
            'jql': expression,
            'maxResults': min(int(limit or 20), 100),
            'fields': ['summary', 'status', 'issuetype', 'priority', 'assignee',
                       'reporter', 'labels', 'duedate', 'created', 'updated',
                       'description'],
        }
        _code, data = self._api('/rest/api/3/search/jql', method='POST', payload=payload)
        rows = [self._issue_row(item) for item in (data.get('issues') or [])]
        spread = {}
        for row in rows:
            spread[row['status']] = spread.get(row['status'], 0) + 1
        shape = ', '.join(f'{count} {name}' for name, count in sorted(spread.items()))
        return self.ok(
            f'{len(rows)} issue(s) for {expression}. {shape or "no results"}.',
            {'jql': expression, 'project': project, 'count': len(rows),
             'by_status': spread, 'issues': rows})

    def live_get_issue(self, key=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        _code, data = self._api(f'/rest/api/3/issue/{key}')
        row = self._issue_row(data)
        return self.ok(
            f'{row["key"]} [{row["status"]}] {row["summary"]} '
            f'({row["type"]}, {row["priority"] or "no priority"}, '
            f'{row["assignee"] or "unassigned"}).',
            {'issue': row})

    def live_list_projects(self):
        _code, data = self._api('/rest/api/3/project/search',
                                params={'maxResults': 50})
        found = data if isinstance(data, list) else (data.get('values') or [])
        rows = []
        for item in found:
            rows.append({'key': item.get('key', ''), 'name': item.get('name', ''),
                         'type': item.get('projectTypeKey', ''),
                         'lead': ((item.get('lead') or {}).get('displayName', '')),
                         'url': f'{self._site()}/browse/{item.get("key", "")}'})
        return self.ok(f'{len(rows)} Jira project(s) visible to this account.',
                       {'count': len(rows), 'projects': rows,
                        'default': self._project('')})

    def live_list_transitions(self, key=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        _code, data = self._api(f'/rest/api/3/issue/{key}/transitions')
        rows = [{'id': item.get('id', ''), 'name': item.get('name', ''),
                 'to': ((item.get('to') or {}).get('name', ''))}
                for item in (data.get('transitions') or [])]
        return self.ok(
            f'{key} can move to: {", ".join(row["to"] or row["name"] for row in rows) or "nowhere"}.',
            {'key': key, 'count': len(rows), 'transitions': rows})

    # =======================================================================
    # Sprints -- live
    # =======================================================================

    def live_create_sprint(self, name='', start='', end='', board_id='', goal=''):
        board = self._board(board_id)
        if not board:
            return self.failure(
                'A sprint needs a board id, and none is set. Open the board in '
                'Jira and copy the number from the URL (after rapidView= or '
                '/boards/), then put it in the Board id field on the '
                'Integrations page.')
        if not str(name).strip():
            return self.failure('A sprint needs a name.')

        payload = {'name': str(name).strip(), 'originBoardId': int(board)}
        if str(start).strip():
            payload['startDate'] = str(start).strip()
        if str(end).strip():
            payload['endDate'] = str(end).strip()
        if str(goal).strip():
            payload['goal'] = str(goal).strip()

        _code, data = self._api('/rest/agile/1.0/sprint', method='POST', payload=payload)
        return self.ok(
            f'Created sprint {data.get("name", name)} (id {data.get("id")}) on board {board}.',
            {'id': data.get('id'), 'name': data.get('name', str(name)),
             'state': data.get('state', 'future'), 'board_id': board,
             'start': data.get('startDate', ''), 'end': data.get('endDate', ''),
             'goal': data.get('goal', str(goal))})

    def live_list_sprints(self, board_id='', state='active'):
        board = self._board(board_id)
        if not board:
            return self.failure(
                'Sprints need a board id, and none is set. Put the number from '
                'the board URL into the Board id field on the Integrations page.')
        params = {'maxResults': 50}
        if str(state).strip():
            params['state'] = str(state).strip().lower()
        _code, data = self._api(f'/rest/agile/1.0/board/{board}/sprint', params=params)
        rows = [{'id': item.get('id'), 'name': item.get('name', ''),
                 'state': item.get('state', ''), 'goal': item.get('goal', ''),
                 'start': item.get('startDate', ''), 'end': item.get('endDate', '')}
                for item in (data.get('values') or [])]
        return self.ok(
            f'{len(rows)} {state} sprint(s) on board {board}: '
            f'{", ".join(row["name"] for row in rows) or "none"}.',
            {'board_id': board, 'state': state, 'count': len(rows), 'sprints': rows})

    def live_add_to_sprint(self, sprint_id='', issue_keys=None):
        if not str(sprint_id).strip():
            return self.failure('Which sprint? Pass sprint_id.')
        keys = [str(item).strip().upper() for item in (issue_keys or []) if str(item).strip()]
        if not keys:
            return self.failure('Which issues? Pass issue_keys as a list, e.g. '
                                "issue_keys=['ENG-101','ENG-105'].")
        self._api(f'/rest/agile/1.0/sprint/{str(sprint_id).strip()}/issue',
                  method='POST', payload={'issues': keys})
        return self.ok(
            f'Moved {len(keys)} issue(s) into sprint {sprint_id}: {", ".join(keys)}.',
            {'sprint_id': str(sprint_id), 'issues': keys, 'count': len(keys)})

    # -- connection check --------------------------------------------------

    def live_probe(self):
        _code, me = self._api('/rest/api/3/myself')
        project = self._project('')
        board = self._board('')
        return self.ok(
            (f'Connected to {self._site()} as '
             f'{me.get("displayName", "unknown")} '
             f'({me.get("emailAddress") or "email hidden by profile settings"}). '
             f'Default project: {project or "not set"}. '
             f'Board: {board or "not set, so sprint operations are unavailable"}.'),
            {'account_id': me.get('accountId', ''),
             'display_name': me.get('displayName', ''),
             'site': self._site(), 'default_project': project, 'board_id': board})

    # =======================================================================
    # Demo halves -- the ENG fixture project
    # =======================================================================

    def _demo_site(self):
        return self._site() or 'https://acme.atlassian.net'

    def _demo_browse(self, key):
        return f'{self._demo_site()}/browse/{key}'

    def _demo_key(self, project, summary):
        """A stable key for something that does not exist yet.

        Derived from a hash so that the same create request always returns the
        same key, which is what makes a simulated create followed by a
        simulated transition of that key coherent.
        """
        return f'{project}-{_stable(f"{project}:{summary}", 201, 299)}'

    def demo_create_issue(self, project='', summary='', description='', issue_type='',
                          priority='', assignee='', labels=None, parent=''):
        project = self._project(project) or DEMO_PROJECT
        if not str(summary).strip():
            return self.failure('A Jira issue needs a summary.')
        kind = str(issue_type).strip() or str(self.setting('default_issue_type', 'Task'))
        key = self._demo_key(project, summary)
        detail = f'{kind}'
        if str(priority).strip():
            detail += f', priority {str(priority).strip()}'
        if str(assignee).strip():
            detail += f', assigned to {str(assignee).strip()}'
        return self.simulated(
            (f'Would create {key} in {project}: {str(summary).strip()!r} '
             f'({detail}). Nothing was created in Jira.'),
            {'key': key, 'id': str(_stable(key, 10000, 99999)),
             'url': self._demo_browse(key), 'project': project,
             'issue_type': kind, 'summary': str(summary).strip(),
             'priority': str(priority), 'assignee': str(assignee),
             'labels': list(labels or []), 'parent': str(parent),
             'description': str(description or '')[:2000],
             'description_format': 'plain text here; a live call sends ADF',
             'simulated': True})

    def demo_update_issue(self, key='', summary='', description='', priority='',
                          labels=None, due_date=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        changed = [name for name, value in (
            ('summary', str(summary).strip()), ('description', str(description).strip()),
            ('priority', str(priority).strip()), ('due_date', str(due_date).strip()),
        ) if value]
        if labels is not None:
            changed.append('labels')
        if not changed:
            return self.failure(
                'Nothing to change. Pass at least one of summary, description, '
                'priority, labels or due_date.')
        return self.simulated(
            f'Would update {key}, changing {", ".join(sorted(changed))}. '
            'Nothing was changed in Jira.',
            {'key': key, 'changed': sorted(changed),
             'url': self._demo_browse(key), 'simulated': True})

    def demo_transition_issue(self, key='', status=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        if not str(status).strip():
            return self.failure(
                "Which state should it move to? Pass status, e.g. status='In Progress'.")

        wanted = _normalise(status)
        chosen = next((item for item in DEMO_TRANSITIONS
                       if _normalise(item['to']) == wanted
                       or _normalise(item['name']) == wanted), None)
        if chosen is None:
            return self.failure(
                (f'{key} cannot move to {status!r} in the demo workflow. The '
                 f'states available are: '
                 f'{", ".join(item["to"] for item in DEMO_TRANSITIONS)}.'),
                {'key': key, 'requested': str(status),
                 'available': [item['to'] for item in DEMO_TRANSITIONS],
                 'simulated': True})

        current = next((item['status'] for item in DEMO_ISSUES if item['key'] == key),
                       'To Do')
        return self.simulated(
            (f'Would move {key} from {current} to {chosen["to"]} using the '
             f'{chosen["name"]!r} transition. The board was not changed.'),
            {'key': key, 'from_status': current, 'status': chosen['to'],
             'transition_id': chosen['id'], 'transition_name': chosen['name'],
             'url': self._demo_browse(key), 'simulated': True})

    def demo_assign_issue(self, key='', assignee=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        if not str(assignee).strip():
            return self.failure('Who should it go to? Pass assignee.')
        return self.simulated(
            f'Would assign {key} to {str(assignee).strip()}. '
            'Nothing was changed in Jira.',
            {'key': key, 'assignee': str(assignee).strip(),
             'account_id': f'demo:{_stable(assignee, 100000, 999999)}',
             'url': self._demo_browse(key), 'simulated': True})

    def demo_comment_issue(self, key='', body=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        if not str(body).strip():
            return self.failure('A comment needs a body.')
        preview = str(body).strip().replace('\n', ' ')
        if len(preview) > 90:
            preview = preview[:87] + '...'
        return self.simulated(
            f'Would comment on {key}: {preview!r}. Nothing was posted to Jira.',
            {'key': key, 'comment_id': str(_stable(f'{key}:{body}', 10000, 99999)),
             'body': str(body), 'url': self._demo_browse(key), 'simulated': True})

    def demo_list_issues(self, project='', jql='', limit=20, status=''):
        project = self._project(project) or DEMO_PROJECT
        rows = []
        for item in DEMO_ISSUES:
            if str(status).strip() and _normalise(item['status']) != _normalise(status):
                continue
            rows.append(_demo_issue_row(self._demo_site(), item))
        rows = rows[:max(1, int(limit or 20))]
        spread = {}
        for row in rows:
            spread[row['status']] = spread.get(row['status'], 0) + 1
        shape = ', '.join(f'{count} {name}' for name, count in sorted(spread.items()))
        expression = str(jql).strip() or f'project = {project} ORDER BY updated DESC'
        return self.simulated(
            (f'Simulated board for {project}: {len(rows)} issue(s) -- '
             f'{shape or "none matching"}. Fixture data, not your Jira site. '
             f'A live call would run: {expression}'),
            {'jql': expression, 'project': project, 'count': len(rows),
             'by_status': spread, 'issues': rows, 'simulated': True})

    def demo_get_issue(self, key=''):
        key = str(key or '').strip().upper()
        if not key:
            return self.failure('Which issue? Pass key, e.g. key=ENG-101.')
        match = next((item for item in DEMO_ISSUES if item['key'] == key), None)
        if match is None:
            return self.simulated(
                (f'{key} is not in the demo fixture, which holds '
                 f'{DEMO_ISSUES[0]["key"]} to {DEMO_ISSUES[-1]["key"]}. '
                 'Nothing was read from Jira.'),
                {'key': key, 'found': False,
                 'available': [item['key'] for item in DEMO_ISSUES],
                 'simulated': True})
        row = _demo_issue_row(self._demo_site(), match)
        return self.simulated(
            (f'Simulated {row["key"]} [{row["status"]}] {row["summary"]} '
             f'({row["type"]}, {row["priority"]}, '
             f'{row["assignee"] or "unassigned"}). Fixture data.'),
            {'issue': row, 'simulated': True})

    def demo_list_projects(self):
        rows = [{'key': item['key'], 'name': item['name'], 'type': item['type'],
                 'lead': item['lead'],
                 'url': f'{self._demo_site()}/browse/{item["key"]}'}
                for item in DEMO_PROJECTS]
        listed = ', '.join('{0} ({1})'.format(row['key'], row['name']) for row in rows)
        return self.simulated(
            f'Simulated {len(rows)} project(s): {listed}. Fixture data.',
            {'count': len(rows), 'projects': rows,
             'default': self._project('') or DEMO_PROJECT, 'simulated': True})

    def demo_list_transitions(self, key=''):
        key = str(key or '').strip().upper() or f'{DEMO_PROJECT}-101'
        rows = [dict(item) for item in DEMO_TRANSITIONS]
        return self.simulated(
            f'In the demo workflow {key} can move to: '
            f'{", ".join(item["to"] for item in rows)}. Fixture workflow, not '
            'your project\'s.',
            {'key': key, 'count': len(rows), 'transitions': rows,
             'simulated': True})

    def demo_create_sprint(self, name='', start='', end='', board_id='', goal=''):
        if not str(name).strip():
            return self.failure('A sprint needs a name.')
        board = self._board(board_id) or '42'
        sprint_id = _stable(f'sprint:{name}', 300, 399)
        return self.simulated(
            (f'Would create sprint {str(name).strip()!r} (id {sprint_id}) on '
             f'board {board}'
             f'{", running " + str(start) + " to " + str(end) if str(start).strip() else ""}. '
             'No sprint was created in Jira.'),
            {'id': sprint_id, 'name': str(name).strip(), 'state': 'future',
             'board_id': board, 'start': str(start), 'end': str(end),
             'goal': str(goal), 'simulated': True})

    def demo_list_sprints(self, board_id='', state='active'):
        board = self._board(board_id) or '42'
        wanted = str(state or 'active').strip().lower()
        rows = []
        if wanted in ('active', '', 'all'):
            rows.append(dict(DEMO_SPRINT))
        if wanted in ('closed', 'all'):
            rows.append({'id': 223, 'name': 'ENG Sprint 23', 'state': 'closed',
                         'goal': 'Rotate credentials and clear the webhook backlog.',
                         'startDate': '2026-08-11T09:00:00.000+1000',
                         'endDate': '2026-08-25T17:00:00.000+1000'})
        if wanted in ('future', 'all'):
            rows.append({'id': 225, 'name': 'ENG Sprint 25', 'state': 'future',
                         'goal': 'Bulk edit with an undo window.',
                         'startDate': '', 'endDate': ''})
        normalised = [{'id': row['id'], 'name': row['name'], 'state': row['state'],
                       'goal': row.get('goal', ''),
                       'start': row.get('startDate', ''), 'end': row.get('endDate', '')}
                      for row in rows]
        return self.simulated(
            (f'Simulated {len(normalised)} {wanted} sprint(s) on board {board}: '
             f'{", ".join(row["name"] for row in normalised) or "none"}. '
             'Fixture data.'),
            {'board_id': board, 'state': wanted, 'count': len(normalised),
             'sprints': normalised, 'simulated': True})

    def demo_add_to_sprint(self, sprint_id='', issue_keys=None):
        keys = [str(item).strip().upper() for item in (issue_keys or []) if str(item).strip()]
        if not keys:
            return self.failure(
                "Which issues? Pass issue_keys as a list, e.g. "
                "issue_keys=['ENG-101','ENG-105'].")
        sprint = str(sprint_id).strip() or str(DEMO_SPRINT['id'])
        return self.simulated(
            (f'Would move {len(keys)} issue(s) into sprint {sprint}: '
             f'{", ".join(keys)}. The board was not changed.'),
            {'sprint_id': sprint, 'issues': keys, 'count': len(keys),
             'simulated': True})


# ===========================================================================
# Small helpers
# ===========================================================================

def _normalise(text):
    """Lowercase, single-spaced, so 'in progress' matches 'In  Progress'."""
    return ' '.join(str(text or '').lower().replace('_', ' ').replace('-', ' ').split())


def _escape_jql(text):
    """Make a value safe inside a double-quoted JQL string."""
    return str(text or '').replace('\\', '\\\\').replace('"', '\\"')


def _demo_issue_row(site, item):
    return {
        'key': item['key'], 'id': str(_stable(item['key'], 10000, 99999)),
        'summary': item['summary'], 'status': item['status'],
        'status_category': _category(item['status']),
        'type': item['type'], 'priority': item['priority'],
        'assignee': item['assignee'], 'reporter': item['reporter'],
        'labels': list(item['labels']), 'story_points': item['points'],
        'sprint': item['sprint'], 'due_date': '',
        'created': item['created'], 'updated': item['updated'],
        'description': item['description'],
        'url': f'{site}/browse/{item["key"]}',
    }


def _category(status):
    if _normalise(status) in ('done', 'closed', 'resolved'):
        return 'Done'
    if _normalise(status) in ('to do', 'backlog', 'open'):
        return 'To Do'
    return 'In Progress'
