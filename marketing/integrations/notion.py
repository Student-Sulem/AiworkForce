"""Notion: the internal wiki the Research employee reads and writes.

WHAT THIS DOES FOR THE WORKFORCE
--------------------------------
Most of what a company actually knows is written down somewhere informal: a
page of team norms, the notes from the meeting where a decision was taken, a
postmortem nobody has re-read since. Notion is where that lives. This
integration exists so the workforce can answer a question with what the
company has already written rather than with something plausible.

The important operation is not ``search`` but ``get_page_content``. Notion
returns a page as a tree of typed blocks, and a block tree is useless to an
indexer. ``_blocks_to_text`` flattens the block types that carry text into
readable prose with markdown-ish headings, so a page arrives in the knowledge
base as something a person would recognise as the page.

WHICH EMPLOYEES USE IT
----------------------
``Research``   searches for a page, reads it, and indexes it into the company
               knowledge base with a citation back to the Notion URL.
``HR``         reads the onboarding checklist and the team norms so that what
               it tells a new starter matches what is written down.
``Marketing``  reads the roadmap note, the pricing page and the competitor
               note before it drafts anything public.
``Support``    reads the postmortem for an incident a customer is asking
               about, so the answer matches the internal account of it.

THE CREDENTIAL YOU NEED
-----------------------
An internal integration token.

  1. Go to notion.so/my-integrations and create a new internal integration.
  2. Copy the internal integration secret. It begins with ``ntn_`` or
     ``secret_``.
  3. Paste it into the Internal integration token field on the Integrations
     page.
  4. Share the pages and databases you want reachable WITH that integration,
     from each page's own share menu. This step is the one everybody misses:
     a valid token that has not been shared anything returns an empty search
     and no error, which looks exactly like a broken connection.

Optionally set the default parent page id, which is where a new page is
created when nobody says where, and the knowledge database id if your wiki is
organised as a database rather than as nested pages. A page id is the
thirty-two character hex string in the page URL.

WHAT DEMO MODE DOES INSTEAD
---------------------------
Without a token every operation is simulated against a small internal wiki of
eight pages with real prose -- team norms, meeting notes, a roadmap note, an
incident postmortem, an onboarding checklist, a pricing page, a competitor
note and a glossary -- each a few hundred words, with stable ids derived from
the page slug. The content is invented but coherent, which is what the
Research employee's indexing run needs in order to be worth demonstrating.
Every simulated result is flagged.
"""

import hashlib
import urllib.error

from . import register
from .base import CallResult, ConfigField, Connector

API_BASE = 'https://api.notion.com/v1'
DEFAULT_NOTION_VERSION = '2022-06-28'

TEXT_BLOCKS = (
    'paragraph', 'heading_1', 'heading_2', 'heading_3',
    'bulleted_list_item', 'numbered_list_item', 'to_do',
    'quote', 'code', 'callout', 'toggle',
)


# ===========================================================================
# Block flattening -- a page as prose, not as a tree
# ===========================================================================

def _rich_text(items):
    """The plain text of a Notion rich-text array."""
    parts = []
    for item in (items or []):
        if isinstance(item, dict):
            parts.append(item.get('plain_text')
                         or ((item.get('text') or {}).get('content', '')))
        else:
            parts.append(str(item))
    return ''.join(parts)


def _blocks_to_text(blocks):
    """Flatten Notion blocks into readable prose.

    Only the block types that actually carry text are rendered. Images,
    dividers, embeds and child-database references are skipped rather than
    turned into placeholder noise, because this text is what the knowledge base
    indexes and a page full of '[unsupported block]' is worse than a page that
    is slightly shorter than the original.
    """
    lines = []
    for block in (blocks or []):
        if not isinstance(block, dict):
            continue
        kind = block.get('type', '')
        body = block.get(kind) or {}
        text = _rich_text(body.get('rich_text'))

        if kind == 'heading_1':
            lines.append('')
            lines.append(f'# {text}')
        elif kind == 'heading_2':
            lines.append('')
            lines.append(f'## {text}')
        elif kind == 'heading_3':
            lines.append('')
            lines.append(f'### {text}')
        elif kind == 'bulleted_list_item':
            lines.append(f'- {text}')
        elif kind == 'numbered_list_item':
            lines.append(f'1. {text}')
        elif kind == 'to_do':
            mark = 'x' if body.get('checked') else ' '
            lines.append(f'- [{mark}] {text}')
        elif kind == 'quote':
            lines.append(f'> {text}')
        elif kind == 'code':
            language = body.get('language', '')
            lines.append(f'```{language}')
            lines.append(text)
            lines.append('```')
        elif kind == 'callout':
            lines.append(f'> {text}')
        elif kind in ('paragraph', 'toggle'):
            if text.strip():
                lines.append(text)
            else:
                lines.append('')
        else:
            continue

    # Collapse runs of blank lines so the result reads like a document.
    cleaned, blank = [], False
    for line in lines:
        if line.strip():
            cleaned.append(line)
            blank = False
        elif not blank and cleaned:
            cleaned.append('')
            blank = True
    return '\n'.join(cleaned).strip()


def _page_title(page):
    """The title of a page, wherever Notion decided to keep it.

    In a plain page the title property is called 'title'. In a database row it
    is called whatever the database's title column is called -- 'Name', 'Doc',
    'Policy'. The only reliable marker is the property's own type, so that is
    what is matched on.
    """
    properties = page.get('properties') or {}
    for name, prop in properties.items():
        if isinstance(prop, dict) and prop.get('type') == 'title':
            text = _rich_text(prop.get('title'))
            if text.strip():
                return text.strip()
    for key in ('title', 'Name', 'name'):
        prop = properties.get(key)
        if isinstance(prop, dict):
            text = _rich_text(prop.get('title') or prop.get('rich_text'))
            if text.strip():
                return text.strip()
    if isinstance(page.get('title'), list):
        text = _rich_text(page.get('title'))
        if text.strip():
            return text.strip()
    return 'Untitled'


def _plain_properties(page):
    """Every property of a page reduced to something printable."""
    out = {}
    for name, prop in (page.get('properties') or {}).items():
        if not isinstance(prop, dict):
            continue
        kind = prop.get('type', '')
        if kind == 'title':
            out[name] = _rich_text(prop.get('title'))
        elif kind == 'rich_text':
            out[name] = _rich_text(prop.get('rich_text'))
        elif kind == 'select':
            out[name] = ((prop.get('select') or {}).get('name', ''))
        elif kind == 'multi_select':
            out[name] = [item.get('name', '') for item in (prop.get('multi_select') or [])]
        elif kind == 'status':
            out[name] = ((prop.get('status') or {}).get('name', ''))
        elif kind == 'date':
            date = prop.get('date') or {}
            out[name] = date.get('start', '')
        elif kind in ('number', 'checkbox', 'url', 'email', 'phone_number'):
            out[name] = prop.get(kind)
        elif kind == 'people':
            out[name] = [person.get('name', '') for person in (prop.get('people') or [])]
        elif kind in ('created_time', 'last_edited_time'):
            out[name] = prop.get(kind, '')
    return out


def _paragraphs(content, limit=100):
    """Plain text split into Notion paragraph blocks.

    Notion caps a single rich-text run at 2000 characters, so long lines are
    chunked rather than rejected by the API.
    """
    blocks = []
    for line in str(content or '').replace('\r\n', '\n').split('\n'):
        if not line.strip():
            continue
        for start in range(0, len(line), 1900):
            blocks.append({
                'object': 'block', 'type': 'paragraph',
                'paragraph': {'rich_text': [
                    {'type': 'text', 'text': {'content': line[start:start + 1900]}}]},
            })
            if len(blocks) >= limit:
                return blocks
    return blocks


def _demo_id(slug):
    """A stable, Notion-shaped 32 hex character id from a slug."""
    return hashlib.sha256(f'notion:{slug}'.encode()).hexdigest()[:32]


# ===========================================================================
# The demo wiki -- eight pages of genuine prose
# ===========================================================================

DEMO_PAGES = (
    {'slug': 'team-norms', 'title': 'How this team works',
     'kind': 'policy', 'owner': 'Dana Okoye',
     'created': '2026-03-11T09:00:00.000Z', 'edited': '2026-08-19T14:20:00.000Z',
     'content': (
         '# How this team works\n'
         '\n'
         'These are norms, not rules. A norm is something we do because it has '
         'been shown to work here, and anyone may argue that it has stopped '
         'working.\n'
         '\n'
         '## Writing beats meeting\n'
         '\n'
         'A proposal is written down before it is discussed. The writing is not '
         'ceremony: it forces the author to find the weak part of the argument '
         'before eight people spend an hour finding it for them. A meeting '
         'without a document attached is a conversation, and conversations do '
         'not need a calendar invitation.\n'
         '\n'
         '## Decisions have an owner and a date\n'
         '\n'
         'Every decision is recorded with who made it, when, and what was '
         'expected to happen as a result. Six months later the question is '
         'never "who is to blame" but "was the reasoning sound given what we '
         'knew", and that question cannot be answered without the record.\n'
         '\n'
         '## Disagree in the open\n'
         '\n'
         'Objections belong in the thread, not in a private message afterwards. '
         'A concern raised privately protects the person raising it and costs '
         'everybody else the information.\n'
         '\n'
         '## Nobody is on call alone\n'
         '\n'
         'The rotation always has a second name. The second person is not '
         'expected to work; they are expected to answer the phone if the first '
         'person needs help. Being alone with a production incident at two in '
         'the morning is how people leave.\n'
         '\n'
         '## Deep work is protected\n'
         '\n'
         'Tuesday and Thursday mornings carry no internal meetings. If '
         'something genuinely cannot wait until the afternoon, it is an '
         'incident and should be declared as one.\n')},
    {'slug': 'weekly-leadership-notes-2026-09-02',
     'title': 'Leadership notes, 2 September 2026',
     'kind': 'meeting_notes', 'owner': 'Dana Okoye',
     'created': '2026-09-02T10:05:00.000Z', 'edited': '2026-09-02T11:40:00.000Z',
     'content': (
         '# Leadership notes, 2 September 2026\n'
         '\n'
         'Present: Dana Okoye, Priya Nair, Tomas Reyes, Marcus Tan. '
         'Apologies: Sara Lombardi.\n'
         '\n'
         '## Enterprise pipeline\n'
         '\n'
         'Four of six open enterprise deals now list SAML as a requirement '
         'rather than a preference. Marcus reported the first slice is behind a '
         'flag and blocked on an expired certificate fixture in the test suite. '
         'Agreed: SAML ships in this quarter, SCIM does not, and sales will '
         'stop implying otherwise in proposals.\n'
         '\n'
         '## Export failures\n'
         '\n'
         'Three enterprise workspaces cannot export at all above roughly fifty '
         'thousand rows. Priya described the cause as buffering whole files in '
         'memory and the fix as streaming. The rewrite is large -- twenty-four '
         'files -- and currently conflicting with main. Agreed: two reviewers, '
         'and it does not go out on a Thursday afternoon.\n'
         '\n'
         '## Support load\n'
         '\n'
         'Tomas reported first-response time has risen from four hours to '
         'eleven over six weeks, driven almost entirely by export and timezone '
         'complaints. Both have fixes in flight. Agreed: no new headcount yet, '
         'reviewed again in three weeks.\n'
         '\n'
         '## Decisions\n'
         '\n'
         '- SAML in scope this quarter, SCIM out of scope. Owner: Marcus.\n'
         '- Streaming export requires two reviewers. Owner: Priya.\n'
         '- Support headcount revisited on 23 September. Owner: Dana.\n'
         '\n'
         '## Next meeting\n'
         '\n'
         '9 September. Dana to bring the revised quarterly forecast.\n')},
    {'slug': 'product-roadmap-note', 'title': 'Roadmap note: the next two quarters',
     'kind': 'roadmap', 'owner': 'Dana Okoye',
     'created': '2026-07-08T08:30:00.000Z', 'edited': '2026-09-04T09:15:00.000Z',
     'content': (
         '# Roadmap note: the next two quarters\n'
         '\n'
         'This is a note, not a commitment. It records what we currently intend '
         'and why, so that when the intention changes the change is visible.\n'
         '\n'
         '## The theme\n'
         '\n'
         'Make the platform credible for companies with more than two hundred '
         'staff. Everything below follows from that. We are not adding '
         'features for smaller customers this half, and that is a deliberate '
         'choice rather than an oversight.\n'
         '\n'
         '## This quarter\n'
         '\n'
         'SAML single sign-on, because it is the first question every '
         'enterprise security review asks. Streaming exports, because the '
         'current implementation fails outright at enterprise data volumes and '
         'a product that cannot export its own data is not trusted with it. '
         'Bulk edit, because marketing operations teams at that size do the '
         'same edit forty times a month.\n'
         '\n'
         '## Next quarter\n'
         '\n'
         'SCIM provisioning, which follows SAML naturally and removes the '
         'manual account administration that large customers dislike. An audit '
         'export, so a compliance team can answer their own questions without '
         'asking us. Role granularity beyond the current three roles.\n'
         '\n'
         '## Explicitly not doing\n'
         '\n'
         'A mobile application. A public plugin marketplace. Anything that adds '
         'a second durable data store. Each of these has been asked for more '
         'than once and each would take the whole team for a quarter.\n'
         '\n'
         '## How we will know it worked\n'
         '\n'
         'Enterprise security reviews stop stalling on authentication, export '
         'complaints fall to zero, and at least two of the six open enterprise '
         'deals close.\n')},
    {'slug': 'incident-2026-08-14-export-outage',
     'title': 'Postmortem: export outage, 14 August 2026',
     'kind': 'postmortem', 'owner': 'Priya Nair',
     'created': '2026-08-15T09:20:00.000Z', 'edited': '2026-08-22T16:05:00.000Z',
     'content': (
         '# Postmortem: export outage, 14 August 2026\n'
         '\n'
         'Blameless. The purpose is to change the system, not to find the '
         'person.\n'
         '\n'
         '## What happened\n'
         '\n'
         'Between 09:12 and 13:40 on 14 August, CSV export failed for every '
         'workspace with more than roughly fifty thousand rows in the table '
         'being exported. Smaller exports were unaffected. Three enterprise '
         'customers reported it; an unknown number did not.\n'
         '\n'
         '## Why\n'
         '\n'
         'The export builds the entire file in memory before writing any of it '
         'to the response. A data migration that morning roughly doubled the '
         'row count for the largest workspaces, which pushed those requests '
         'past the sixty second gateway timeout. The code had always had this '
         'shape; the migration only revealed it.\n'
         '\n'
         '## Detection\n'
         '\n'
         'A customer told us. Our own alerting did not fire, because a 504 '
         'returned by the gateway never reaches the application error rate that '
         'the alert watches. This is the most important finding in the '
         'document.\n'
         '\n'
         '## Response\n'
         '\n'
         'The immediate mitigation was to raise the gateway timeout to one '
         'hundred and eighty seconds, which restored exports for the affected '
         'workspaces at the cost of holding workers for longer. The real fix, '
         'streaming the response, is in review.\n'
         '\n'
         '## What we are changing\n'
         '\n'
         '- Alert on gateway 5xx as well as application errors. Owner: Dan. '
         'Done.\n'
         '- Rewrite the export to stream. Owner: Priya. In review.\n'
         '- Add a load test at ten times the largest current workspace to CI. '
         'Owner: Priya. Not started.\n'
         '\n'
         '## What went well\n'
         '\n'
         'The mitigation was found and applied within forty minutes of the '
         'first report, and the customer communication went out before the fix '
         'rather than after it.\n')},
    {'slug': 'onboarding-checklist', 'title': 'New starter checklist',
     'kind': 'checklist', 'owner': 'Dana Okoye',
     'created': '2026-02-02T08:00:00.000Z', 'edited': '2026-08-28T10:10:00.000Z',
     'content': (
         '# New starter checklist\n'
         '\n'
         'The aim of a first week is not productivity. It is that by Friday the '
         'new starter knows who to ask, has shipped something small, and does '
         'not feel they are imposing by asking a question.\n'
         '\n'
         '## Before day one\n'
         '\n'
         '- [x] Laptop ordered and delivered to the address they gave.\n'
         '- [x] Accounts created: email, Slack, Jira, GitHub, Notion.\n'
         '- [x] Buddy assigned, and the buddy told, with time set aside.\n'
         '- [x] First week calendar drafted, with the afternoons left empty.\n'
         '\n'
         '## Day one\n'
         '\n'
         '- [ ] Welcome conversation with their manager, thirty minutes, no '
         'agenda.\n'
         '- [ ] Read How this team works and the security basics page.\n'
         '- [ ] Local environment running, with the buddy sitting alongside.\n'
         '- [ ] Lunch with the team.\n'
         '\n'
         '## First week\n'
         '\n'
         '- [ ] Ship one small change to production, however trivial.\n'
         '- [ ] Meet one person from support and one from sales.\n'
         '- [ ] Attend one customer call as an observer.\n'
         '- [ ] Write down three things that were confusing. These are a '
         'defect report on our onboarding, not on the new starter.\n'
         '\n'
         '## First month\n'
         '\n'
         '- [ ] Own one piece of work end to end.\n'
         '- [ ] Take one on-call shift as the second name on the rotation.\n'
         '- [ ] Thirty day conversation with their manager, focused on whether '
         'the role matches what was described in hiring.\n')},
    {'slug': 'pricing-and-packaging', 'title': 'Pricing and packaging',
     'kind': 'reference', 'owner': 'Dana Okoye',
     'created': '2026-04-19T11:00:00.000Z', 'edited': '2026-09-01T08:45:00.000Z',
     'content': (
         '# Pricing and packaging\n'
         '\n'
         'Internal reference. The public page is generated from this, so a '
         'change here is a change customers see.\n'
         '\n'
         '## Tiers\n'
         '\n'
         'Starter is nineteen dollars per seat per month, billed annually, with '
         'a five seat minimum. It includes the core platform, two integrations '
         'and email support.\n'
         '\n'
         'Growth is forty-nine dollars per seat per month with a ten seat '
         'minimum. It adds unlimited integrations, approval workflows, the API, '
         'and a four hour first-response target during business hours.\n'
         '\n'
         'Enterprise is quoted, with a floor of thirty thousand dollars a year. '
         'It adds single sign-on, audit export, a named contact, and a '
         'contractual uptime commitment.\n'
         '\n'
         '## Discounting\n'
         '\n'
         'Up to fifteen per cent is at the account executive\'s discretion. '
         'Above that needs the head of revenue, and above thirty per cent needs '
         'a written reason recorded on the opportunity. We do not discount the '
         'first year and then raise the price in the second: it produces a '
         'renewal argument that costs more than the discount saved.\n'
         '\n'
         '## What is not negotiable\n'
         '\n'
         'The seat minimums, the annual billing term below Enterprise, and the '
         'uptime commitment. Payment terms beyond thirty days require finance '
         'approval.\n'
         '\n'
         '## Common questions\n'
         '\n'
         'Seats are named users, not concurrent sessions. Adding a seat '
         'mid-term is prorated; removing one takes effect at renewal. Nonprofit '
         'and education pricing is forty per cent off list, with '
         'documentation.\n')},
    {'slug': 'competitor-note-northwind',
     'title': 'Competitor note: Northwind Suite',
     'kind': 'competitor', 'owner': 'Marcus Tan',
     'created': '2026-06-24T13:15:00.000Z', 'edited': '2026-08-30T15:50:00.000Z',
     'content': (
         '# Competitor note: Northwind Suite\n'
         '\n'
         'Written from public material, customer conversations and two lost '
         'deals. Treat the numbers as approximate and do not quote them to '
         'customers.\n'
         '\n'
         '## Where they are strong\n'
         '\n'
         'Their reporting is better than ours and they know it: a prospect who '
         'has seen a Northwind demonstration will ask for scheduled reports '
         'within the first ten minutes. They are also cheaper at the bottom of '
         'the market, roughly twelve dollars per seat, and they have a mobile '
         'application.\n'
         '\n'
         '## Where they are weak\n'
         '\n'
         'Their approval workflow is a single approver with no audit trail, '
         'which fails most procurement reviews above a couple of hundred staff. '
         'They have no API worth the name -- read-only, and rate limited to '
         'sixty requests a minute. Their support is email only with a '
         'twenty-four hour target.\n'
         '\n'
         '## How the two lost deals were lost\n'
         '\n'
         'Both were price. In both cases the buyer was an operations manager '
         'without a security review in the process, which is exactly the '
         'segment where our advantages do not count.\n'
         '\n'
         '## How to position against them\n'
         '\n'
         'Do not compete on reporting; acknowledge it and move. Ask who signs '
         'off a change and what evidence they need afterwards -- the audit '
         'trail is the argument, and it is an argument Northwind cannot answer '
         'without rebuilding their workflow engine. If the buyer has no '
         'compliance requirement and a small team, we will lose on price and '
         'should qualify out early rather than discount.\n')},
    {'slug': 'glossary', 'title': 'Glossary',
     'kind': 'glossary', 'owner': 'Tomas Reyes',
     'created': '2026-01-15T09:00:00.000Z', 'edited': '2026-09-03T12:30:00.000Z',
     'content': (
         '# Glossary\n'
         '\n'
         'Words we use in a particular way. If a term here is used differently '
         'in a document, that document is wrong.\n'
         '\n'
         '**Workspace** -- one customer\'s isolated container of data, users '
         'and settings. A customer may have several; billing is per workspace.\n'
         '\n'
         '**Seat** -- one named user with a login. Not a concurrent session and '
         'not a role.\n'
         '\n'
         '**Approval** -- a recorded human decision to allow an action that '
         'reaches outside the platform. An action that never reaches outside '
         'does not need one.\n'
         '\n'
         '**Proposed action** -- an action an automated employee wants to take, '
         'held with its full argument set until a person approves, edits or '
         'rejects it.\n'
         '\n'
         '**Demo mode** -- a simulated action, produced when a credential is '
         'absent or when simulation is deliberately chosen. Always labelled. A '
         'simulated action is never described as having happened.\n'
         '\n'
         '**Connector** -- the code that knows how to speak to one external '
         'service. Configuration for it lives in the database, never in the '
         'code.\n'
         '\n'
         '**First response** -- the time from a customer message arriving to a '
         'human reply that engages with the content. An automatic '
         'acknowledgement is not a first response.\n'
         '\n'
         '**Incident** -- any event where customers are affected and the fix is '
         'not already known. Declaring one is free; not declaring one is '
         'expensive.\n')},
)

DEMO_DATABASES = (
    {'slug': 'knowledge-base-db', 'title': 'Company knowledge base',
     'description': 'Every internal document, tagged by department and owner.',
     'properties': ['Name', 'Type', 'Department', 'Owner', 'Last reviewed']},
    {'slug': 'decision-log-db', 'title': 'Decision log',
     'description': 'One row per decision, with the owner and the expected outcome.',
     'properties': ['Decision', 'Owner', 'Date', 'Status', 'Reasoning']},
)


@register
class NotionConnector(Connector):
    """Search, read and write Notion pages over the official API."""

    key = 'notion'
    name = 'Notion'
    description = ('Searches the internal wiki, reads pages as prose for the '
                   'knowledge base, and writes new pages and notes.')
    category = 'documents'
    icon = 'fa-n'
    color = '#000000'
    docs_url = 'https://developers.notion.com/reference'

    config_fields = (
        ConfigField(
            'integration_token', 'Internal integration token',
            help_text=('Create an integration at notion.so/my-integrations and '
                       'share the pages you want with it, or nothing will be '
                       'visible.'),
            field_type='password', required=True, secret=True,
            placeholder='ntn_...'),
        ConfigField(
            'notion_version', 'API version',
            help_text=('Notion pins behaviour to a dated version. Leave this '
                       'alone unless you have read their changelog.'),
            default=DEFAULT_NOTION_VERSION),
        ConfigField(
            'default_parent_page_id', 'Default parent page id',
            help_text=('Where a new page goes when nobody says. The 32 '
                       'character hex string in the page URL.'),
            placeholder='1f2e3d4c5b6a7988...'),
        ConfigField(
            'knowledge_database_id', 'Knowledge database id',
            help_text=('Optional. Set this if the wiki is organised as a '
                       'database, so queries can be run against it directly.')),
    )

    operations = (
        'search', 'get_page', 'get_page_content', 'list_databases',
        'query_database', 'create_page', 'append_block',
    )

    # -- plumbing ----------------------------------------------------------

    def _headers(self):
        return {
            'Authorization': f'Bearer {self.setting("integration_token")}',
            'Notion-Version': str(self.setting('notion_version',
                                               DEFAULT_NOTION_VERSION)),
            'Accept': 'application/json',
        }

    def _api(self, path, *, method='GET', payload=None, params=None):
        try:
            status, data = self.request_json(
                f'{API_BASE}{path}', method=method, headers=self._headers(),
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
            return ('Notion rejected the token (401). Regenerate the internal '
                    'integration secret at notion.so/my-integrations and paste '
                    'the new value into the Notion integration.')
        if code == 403:
            return ('Notion refused the request (403). The integration exists '
                    'but has not been given this capability -- check that it has '
                    'read, and if you are writing, insert and update content '
                    'permission in its capability settings.')
        if code == 404:
            return (f'Notion returned 404 for {path}. In practice this almost '
                    'always means the page or database exists but has not been '
                    'shared with the integration. Open the page in Notion, use '
                    'its share menu, and add the integration by name. A page '
                    'the integration cannot see is indistinguishable from one '
                    'that does not exist.')
        if code == 400:
            return ('Notion rejected the request body (400). Common causes are '
                    'an id with the wrong shape, or a block that exceeds the '
                    f'2000 character rich-text limit. Notion said: {body}')
        if code == 429:
            return ('Notion is rate limiting this integration (429), which '
                    'allows roughly three requests a second. Wait a moment and '
                    'try again.')
        return f'Notion returned HTTP {code} {exc.reason}. {body}'.strip()

    def _page_row(self, page):
        return {
            'id': page.get('id', ''),
            'title': _page_title(page),
            'object': page.get('object', 'page'),
            'url': page.get('url', ''),
            'created_time': page.get('created_time', ''),
            'last_edited_time': page.get('last_edited_time', ''),
            'archived': bool(page.get('archived')),
            'parent': ((page.get('parent') or {}).get('type', '')),
            'properties': _plain_properties(page),
        }

    # =======================================================================
    # Live
    # =======================================================================

    def live_search(self, query='', limit=15, filter_type=''):
        payload = {'page_size': min(int(limit or 15), 100)}
        if str(query).strip():
            payload['query'] = str(query).strip()
        if str(filter_type).strip().lower() in ('page', 'database'):
            payload['filter'] = {'property': 'object',
                                 'value': str(filter_type).strip().lower()}

        _code, data = self._api('/search', method='POST', payload=payload)
        rows = []
        for item in (data.get('results') or []):
            if item.get('object') == 'database':
                rows.append({'id': item.get('id', ''),
                             'title': _rich_text(item.get('title')) or 'Untitled',
                             'object': 'database', 'url': item.get('url', ''),
                             'last_edited_time': item.get('last_edited_time', '')})
            else:
                rows.append(self._page_row(item))

        if not rows:
            return self.ok(
                (f'Notion returned nothing for {query or "an empty query"!r}. '
                 'If that is unexpected, the usual cause is sharing rather than '
                 'search: the integration only sees pages that have been shared '
                 'with it explicitly from each page\'s share menu.'),
                {'query': str(query), 'count': 0, 'results': [],
                 'hint': 'share the pages with the integration'})

        return self.ok(
            f'{len(rows)} Notion result(s) for {query or "everything shared"!r}.',
            {'query': str(query), 'count': len(rows), 'results': rows,
             'has_more': bool(data.get('has_more'))})

    def live_get_page(self, page_id=''):
        page_id = str(page_id or '').strip()
        if not page_id:
            return self.failure('Which page? Pass page_id.')
        _code, data = self._api(f'/pages/{page_id}')
        row = self._page_row(data)
        return self.ok(
            f'{row["title"]} (last edited {row["last_edited_time"] or "unknown"}). '
            f'{row["url"]}',
            {'page': row})

    def live_get_page_content(self, page_id=''):
        """A page as readable prose, with its blocks flattened and paged through."""
        page_id = str(page_id or '').strip()
        if not page_id:
            return self.failure('Which page? Pass page_id.')

        title = ''
        try:
            _code, page = self._api(f'/pages/{page_id}')
            title = _page_title(page)
            url = page.get('url', '')
        except RuntimeError:
            url = ''

        blocks, cursor, pages = [], None, 0
        while pages < 10:
            params = {'page_size': 100}
            if cursor:
                params['start_cursor'] = cursor
            _code, data = self._api(f'/blocks/{page_id}/children', params=params)
            blocks.extend(data.get('results') or [])
            pages += 1
            if not data.get('has_more'):
                break
            cursor = data.get('next_cursor')

        text = _blocks_to_text(blocks)
        carried = sum(1 for block in blocks if block.get('type') in TEXT_BLOCKS)
        return self.ok(
            (f'Read {title or page_id} as {len(text.split())} word(s) of prose '
             f'from {carried} text block(s) of {len(blocks)}.'),
            {'page_id': page_id, 'title': title, 'url': url,
             'content': text, 'words': len(text.split()),
             'blocks_total': len(blocks), 'blocks_with_text': carried})

    def live_list_databases(self):
        _code, data = self._api('/search', method='POST',
                                payload={'filter': {'property': 'object',
                                                    'value': 'database'},
                                         'page_size': 100})
        rows = []
        for item in (data.get('results') or []):
            rows.append({
                'id': item.get('id', ''),
                'title': _rich_text(item.get('title')) or 'Untitled',
                'description': _rich_text(item.get('description')),
                'url': item.get('url', ''),
                'properties': sorted((item.get('properties') or {}).keys()),
                'last_edited_time': item.get('last_edited_time', ''),
            })
        return self.ok(
            f'{len(rows)} Notion database(s) shared with this integration.',
            {'count': len(rows), 'databases': rows,
             'configured': str(self.setting('knowledge_database_id', '') or '')})

    def live_query_database(self, database_id='', filter=None, limit=20, sorts=None):  # noqa: A002
        database_id = (str(database_id or '').strip()
                       or str(self.setting('knowledge_database_id', '') or '').strip())
        if not database_id:
            return self.failure(
                'No database was given and no knowledge database id is set. Set '
                'it on the Integrations page, or pass database_id.')

        payload = {'page_size': min(int(limit or 20), 100)}
        if filter:
            payload['filter'] = filter
        if sorts:
            payload['sorts'] = sorts
        _code, data = self._api(f'/databases/{database_id}/query',
                                method='POST', payload=payload)
        rows = [self._page_row(item) for item in (data.get('results') or [])]
        return self.ok(
            f'{len(rows)} row(s) from Notion database {database_id}.',
            {'database_id': database_id, 'count': len(rows), 'rows': rows,
             'has_more': bool(data.get('has_more'))})

    def live_create_page(self, parent_id='', title='', content=''):
        parent_id = (str(parent_id or '').strip()
                     or str(self.setting('default_parent_page_id', '') or '').strip())
        if not parent_id:
            return self.failure(
                'A new page needs a parent. Set the default parent page id on '
                'the Integrations page, or pass parent_id -- the 32 character '
                'hex string from the parent page URL.')
        if not str(title).strip():
            return self.failure('A page needs a title.')

        payload = {
            'parent': {'page_id': parent_id},
            'properties': {'title': {'title': [
                {'type': 'text', 'text': {'content': str(title).strip()[:2000]}}]}},
            'children': _paragraphs(content),
        }
        _code, data = self._api('/pages', method='POST', payload=payload)
        return self.ok(
            f'Created the Notion page {str(title).strip()!r}. {data.get("url", "")}',
            {'page_id': data.get('id', ''), 'url': data.get('url', ''),
             'title': str(title).strip(), 'parent_id': parent_id,
             'blocks': len(payload['children'])})

    def live_append_block(self, page_id='', content=''):
        page_id = str(page_id or '').strip()
        if not page_id:
            return self.failure('Which page? Pass page_id.')
        blocks = _paragraphs(content)
        if not blocks:
            return self.failure('Nothing to append. Pass content.')
        _code, data = self._api(f'/blocks/{page_id}/children', method='PATCH',
                                payload={'children': blocks})
        return self.ok(
            f'Appended {len(blocks)} paragraph(s) to Notion page {page_id}.',
            {'page_id': page_id, 'blocks_added': len(blocks),
             'results': len(data.get('results') or [])})

    # -- connection check --------------------------------------------------

    def live_probe(self):
        _code, me = self._api('/users/me')
        bot = (me.get('bot') or {})
        owner = ((bot.get('owner') or {}).get('type', ''))
        _code, shared = self._api('/search', method='POST', payload={'page_size': 5})
        visible = len(shared.get('results') or [])
        note = ('' if visible else
                ' No pages are shared with it yet, so searches will come back '
                'empty until you share pages from their share menu.')
        return self.ok(
            (f'Connected to Notion as the integration '
             f'{me.get("name") or "unnamed"} (owner type: {owner or "unknown"}). '
             f'At least {visible} item(s) shared with it.{note}'),
            {'bot_name': me.get('name', ''), 'bot_id': me.get('id', ''),
             'owner_type': owner, 'shared_sample': visible})

    # =======================================================================
    # Demo halves -- the eight page fixture wiki
    # =======================================================================

    def demo_search(self, query='', limit=15, filter_type=''):
        needle = str(query or '').strip().lower()
        wanted = str(filter_type or '').strip().lower()

        rows = []
        if wanted != 'database':
            for page in DEMO_PAGES:
                haystack = f'{page["title"]} {page["content"]}'.lower()
                if needle and needle not in haystack:
                    continue
                rows.append(_demo_page_row(page))
        if wanted in ('', 'database'):
            for database in DEMO_DATABASES:
                if needle and needle not in database['title'].lower():
                    continue
                rows.append(_demo_database_row(database))

        rows = rows[:max(1, int(limit or 15))]
        return self.simulated(
            (f'Simulated Notion search for {query or "everything"!r}: '
             f'{len(rows)} result(s) from an eight page fixture wiki. Nothing '
             'was read from your Notion workspace.'),
            {'query': str(query), 'count': len(rows), 'results': rows,
             'has_more': False, 'simulated': True})

    def demo_get_page(self, page_id=''):
        page = _find_demo_page(page_id)
        if page is None:
            return self.simulated(
                (f'No fixture page matches {page_id!r}. The demo wiki holds: '
                 f'{", ".join(item["title"] for item in DEMO_PAGES)}.'),
                {'page_id': str(page_id), 'found': False,
                 'available': [{'id': _demo_id(item['slug']), 'title': item['title']}
                               for item in DEMO_PAGES],
                 'simulated': True})
        row = _demo_page_row(page)
        return self.simulated(
            (f'Simulated page {row["title"]} (owner {page["owner"]}, last edited '
             f'{page["edited"]}). Fixture data.'),
            {'page': row, 'simulated': True})

    def demo_get_page_content(self, page_id=''):
        page = _find_demo_page(page_id)
        if page is None:
            return self.simulated(
                (f'No fixture page matches {page_id!r}, so there is no content '
                 f'to return. The demo wiki holds: '
                 f'{", ".join(item["title"] for item in DEMO_PAGES)}.'),
                {'page_id': str(page_id), 'found': False, 'content': '',
                 'simulated': True})
        body = page['content']
        return self.simulated(
            (f'Simulated read of {page["title"]}: {len(body.split())} word(s) of '
             'fixture prose. Invented content, coherent enough to index, but not '
             'from your Notion workspace.'),
            {'page_id': _demo_id(page['slug']), 'title': page['title'],
             'url': f'https://www.notion.so/{_demo_id(page["slug"])}',
             'content': body, 'words': len(body.split()),
             'blocks_total': body.count('\n\n') + 1,
             'blocks_with_text': body.count('\n\n') + 1,
             'doc_type': page['kind'], 'owner': page['owner'],
             'simulated': True})

    def demo_list_databases(self):
        rows = [_demo_database_row(item) for item in DEMO_DATABASES]
        return self.simulated(
            f'Simulated {len(rows)} Notion database(s): '
            f'{", ".join(row["title"] for row in rows)}. Fixture data.',
            {'count': len(rows), 'databases': rows, 'simulated': True})

    def demo_query_database(self, database_id='', filter=None, limit=20, sorts=None):  # noqa: A002
        target = (str(database_id or '').strip()
                  or _demo_id(DEMO_DATABASES[0]['slug']))
        rows = [_demo_page_row(page) for page in DEMO_PAGES][:max(1, int(limit or 20))]
        applied = 'no filter' if not filter else 'the supplied filter (not applied in demo)'
        return self.simulated(
            (f'Simulated query of Notion database {target}: {len(rows)} row(s) '
             f'with {applied}. Fixture data.'),
            {'database_id': target, 'count': len(rows), 'rows': rows,
             'filter_applied': bool(filter), 'has_more': False,
             'simulated': True})

    def demo_create_page(self, parent_id='', title='', content=''):
        if not str(title).strip():
            return self.failure('A page needs a title.')
        parent = (str(parent_id or '').strip()
                  or str(self.setting('default_parent_page_id', '') or '').strip()
                  or _demo_id('team-norms'))
        new_id = _demo_id(f'new:{title}')
        blocks = len([line for line in str(content or '').split('\n') if line.strip()])
        return self.simulated(
            (f'Would create the Notion page {str(title).strip()!r} under '
             f'{parent} with {blocks} paragraph(s). Nothing was written to '
             'Notion.'),
            {'page_id': new_id, 'url': f'https://www.notion.so/{new_id}',
             'title': str(title).strip(), 'parent_id': parent, 'blocks': blocks,
             'content': str(content or '')[:2000], 'simulated': True})

    def demo_append_block(self, page_id='', content=''):
        if not str(content or '').strip():
            return self.failure('Nothing to append. Pass content.')
        target = str(page_id or '').strip() or _demo_id('team-norms')
        blocks = len([line for line in str(content).split('\n') if line.strip()])
        page = _find_demo_page(target)
        where = page['title'] if page else target
        return self.simulated(
            (f'Would append {blocks} paragraph(s) to {where}. Nothing was '
             'written to Notion.'),
            {'page_id': target, 'blocks_added': blocks,
             'content': str(content)[:2000], 'simulated': True})


# ===========================================================================
# Demo helpers
# ===========================================================================

def _find_demo_page(page_id):
    given = str(page_id or '').strip().lower()
    if not given:
        return None
    # A Notion id may arrive dashed or undashed, and a caller that has read a
    # previous demo result may pass the slug instead.
    wanted = given.replace('-', '')
    for page in DEMO_PAGES:
        if wanted in (_demo_id(page['slug']), page['slug'].replace('-', '')):
            return page
    for page in DEMO_PAGES:
        if wanted in page['title'].lower().replace(' ', ''):
            return page
    return None


def _demo_page_row(page):
    identifier = _demo_id(page['slug'])
    body = page['content']
    return {
        'id': identifier, 'title': page['title'], 'object': 'page',
        'url': f'https://www.notion.so/{identifier}',
        'created_time': page['created'], 'last_edited_time': page['edited'],
        'archived': False, 'parent': 'page_id',
        'properties': {'Name': page['title'], 'Type': page['kind'],
                       'Owner': page['owner']},
        'excerpt': ' '.join(body.split())[:280],
        'words': len(body.split()),
        'simulated': True,
    }


def _demo_database_row(database):
    identifier = _demo_id(database['slug'])
    return {
        'id': identifier, 'title': database['title'], 'object': 'database',
        'description': database['description'],
        'url': f'https://www.notion.so/{identifier}',
        'properties': list(database['properties']),
        'last_edited_time': '2026-09-03T12:30:00.000Z',
        'simulated': True,
    }
