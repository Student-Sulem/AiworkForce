"""Google Drive: the company's documents, as the workforce sees them.

WHAT IT IS USED FOR
-------------------
Drive is where the employees get their facts. The Research employee syncs the
company documents folder into the knowledge base so that "what is our refund
policy" is answered with a citation instead of a guess. The HR employee reads
the resumes folder, parses each candidate and scores them against a job
opening. The Engineering Manager and the Developer read project
specifications. The Support employee reads the troubleshooting guide before
drafting a reply.

Reading is therefore the important half. Writing exists so an employee can put
a generated document -- a job description, a sprint summary, a customer reply
-- somewhere a person will find it.

THE CREDENTIAL, AND WHERE A PERSON GETS IT
------------------------------------------
The same OAuth shape as Google Calendar, and usually the same client:

  * An access token for the short path. Mint one at
    developers.google.com/oauthplayground with the
    ``https://www.googleapis.com/auth/drive`` scope, or the read-only
    ``drive.readonly`` scope if the workforce should never write. It lasts
    about an hour.

  * A refresh token with its client id and secret for the durable path. Create
    an OAuth 2.0 Client ID at console.cloud.google.com/apis/credentials, enable
    the Google Drive API for the project at
    console.cloud.google.com/apis/library, authorise once and keep the refresh
    token. This connector exchanges it for an access token when it needs one,
    caches that on the instance, and never writes it back to the database.

The three folder ids all come from a Drive URL: opening a folder gives an
address ending ``/folders/1AbC...``, and that trailing part is the id.

WHAT THE STANDARD LIBRARY CANNOT DO
-----------------------------------
A Google Doc, Sheet or Slide deck can be exported as text, and this connector
does exactly that. A PDF, a Word file or a spreadsheet uploaded as .xlsx
cannot: parsing those needs a third-party library the project deliberately does
not have. ``read_file`` therefore returns the metadata and says plainly that
the content could not be read and what the file is, rather than decoding the
bytes as UTF-8 and passing the resulting nonsense off as content. A model given
mojibake will confidently summarise it, which is far worse than being told the
file needs converting to a Google Doc first.

WHAT DEMO MODE DOES INSTEAD
---------------------------
A stand-in Drive of twelve files with ids of the form ``demo-drive-<slug>``, so
a listing followed by a read of one of the ids it returned works. The content
is real prose rather than lorem ipsum, because the rest of the platform has to
work with it: three full resumes for the HR employee to parse and score, four
company policy documents for the knowledge sync to index and cite, two project
specifications, a support troubleshooting guide, a spreadsheet that is listed
and honestly reported as unreadable, and a folder.
"""

import hashlib
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request

from . import register
from .base import CallResult, ConfigField, Connector

API = 'https://www.googleapis.com/drive/v3'
UPLOAD_API = 'https://www.googleapis.com/upload/drive/v3/files'
TOKEN_URL = 'https://oauth2.googleapis.com/token'

FILE_FIELDS = 'files(id,name,mimeType,modifiedTime,size,webViewLink,parents)'
ONE_FILE_FIELDS = ('id,name,mimeType,modifiedTime,createdTime,size,webViewLink,'
                   'parents,owners(displayName,emailAddress),description')

FOLDER_MIME = 'application/vnd.google-apps.folder'

# Google's own formats have no bytes to download; they have to be exported.
GOOGLE_EXPORTS = {
    'application/vnd.google-apps.document': 'text/plain',
    'application/vnd.google-apps.spreadsheet': 'text/csv',
    'application/vnd.google-apps.presentation': 'text/plain',
    'application/vnd.google-apps.script': 'application/vnd.google-apps.script+json',
}

# Formats that are text on the wire and text once decoded.
READABLE_TEXT = (
    'text/', 'application/json', 'application/xml', 'application/x-yaml',
    'application/javascript', 'application/sql',
)

# Formats whose bytes are meaningless without a parser the project does not
# have. Named individually so the message can say what the file actually is.
BINARY_NOTES = {
    'application/pdf': 'a PDF',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        'a Word document (.docx)',
    'application/msword': 'a Word document (.doc)',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':
        'an Excel spreadsheet (.xlsx)',
    'application/vnd.ms-excel': 'an Excel spreadsheet (.xls)',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation':
        'a PowerPoint deck (.pptx)',
    'application/zip': 'a zip archive',
    'image/png': 'a PNG image',
    'image/jpeg': 'a JPEG image',
    'video/mp4': 'an MP4 video',
}

DEFAULT_MAX_CHARS = 200000


# ===========================================================================
# The stand-in Drive
# ===========================================================================

def _demo_file(slug, name, mime, modified, size, content='', parent='documents',
               readable=True):
    return {'file_id': f'demo-drive-{slug}', 'name': name, 'mime_type': mime,
            'modified': modified, 'size': size,
            'url': f'https://drive.google.com/file/d/demo-drive-{slug}/view',
            'parent': parent, 'content': content, 'readable': readable}


_RESUME_ARJUN = """ARJUN MEHTA
Melbourne VIC 3000 | arjun.mehta@example.com | 0400 000 111

SUMMARY
Backend engineer with six years building Django services for logistics and
payments teams. Owns services end to end, from schema design through to
on-call. Strongest on PostgreSQL, queueing and making slow endpoints fast.

SKILLS
Python, Django, Django REST Framework, PostgreSQL, Redis, Celery, Docker,
GitHub Actions, AWS (ECS, RDS, S3), pytest, Terraform basics.

EXPERIENCE
Senior Backend Engineer, Freightline Systems, Melbourne
March 2023 - present
  Rebuilt the consignment pricing service, cutting median quote time from
  900ms to 120ms by removing an N+1 across three tables.
  Introduced Celery for label generation, taking a synchronous 14-second
  request out of the checkout path.
  Mentored two graduate engineers and ran the fortnightly review clinic.

Backend Engineer, Paylane Australia, Melbourne
July 2020 - February 2023
  Built the reconciliation pipeline matching settlement files against 40,000
  daily transactions.
  Took the test suite from 26 minutes to 7 by isolating fixtures per module.

Junior Developer, Corville Digital, Geelong
February 2019 - June 2020
  Maintained three client Django sites and their deployment scripts.

EDUCATION
BSc Computer Science, University of Melbourne, 2018.

AVAILABILITY
Four weeks notice. Happy to complete a technical exercise.
"""

_RESUME_SOFIA = """SOFIA ROSSI
Carlton VIC 3053 | sofia.rossi@example.com | 0400 000 222

SUMMARY
Full-stack developer, four years, half of it in a two-person team where she
owned the front end and the deployment pipeline. Comfortable being the only
person who knows how something works, and better at writing it down than most.

SKILLS
Python, Django, JavaScript, TypeScript, React, PostgreSQL, Docker, Nginx,
GitHub Actions, Playwright, accessibility testing.

EXPERIENCE
Developer, Marlowe Health, Melbourne
August 2023 - present
  Built the patient intake portal used by eleven clinics, including the
  offline-tolerant form flow that cut abandoned submissions by a third.
  Brought the front end to WCAG 2.1 AA and wrote the audit that proved it.

Junior Developer, Halden Labs, Melbourne
February 2022 - July 2023
  Two-person team on a laboratory scheduling product. Owned React front end,
  Django API and the deployment pipeline.
  Introduced Playwright end-to-end tests, which caught a booking bug that had
  reached production twice.

EDUCATION
BSc Information Technology, RMIT University, 2021.
Graduate certificate in human-computer interaction, RMIT, 2022.

AVAILABILITY
Two weeks notice. Prefers three days in the office.
"""

_RESUME_DAVID = """DAVID OYELARAN
Footscray VIC 3011 | david.oyelaran@example.com | 0400 000 333

SUMMARY
Platform engineer, nine years, most recently responsible for the deployment
and observability of thirty-one services. Interested in the boring problems:
build times, alert fatigue, and the difference between a runbook and a wiki
page nobody reads.

SKILLS
Python, Go, Kubernetes, Terraform, AWS, Prometheus, Grafana, PostgreSQL,
GitHub Actions, Django (working knowledge), incident command.

EXPERIENCE
Staff Platform Engineer, Kestrel Freight, Melbourne
January 2021 - present
  Consolidated fourteen bespoke deployment scripts into one Terraform module,
  taking a release from forty minutes of manual work to six of pipeline.
  Cut paging alerts by 71 per cent in a quarter by deleting the ones nobody
  had ever acted on and rewriting the four that mattered.
  Ran incident command for the 2024 regional outage, and wrote the review.

Senior DevOps Engineer, Ambry Digital, Sydney
March 2017 - December 2020
  Migrated a monolith to ECS across eight months with no customer-visible
  downtime.

EDUCATION
BEng Software Engineering, University of Lagos, 2015.

AVAILABILITY
Six weeks notice. Open to a lead role.
"""

_LEAVE_POLICY = """ANNUAL AND PERSONAL LEAVE POLICY
Version 4.2, effective 1 July 2026

1. SCOPE
This policy covers every permanent employee, full time and part time. Casual
staff accrue no paid leave and are covered by clause 9 instead.

2. ANNUAL LEAVE
Full-time employees accrue twenty working days of paid annual leave a year,
accruing progressively through the year. Part-time employees accrue the same
entitlement pro rata to their ordinary hours.

Leave must be requested at least two weeks in advance for absences of a week
or less, and at least six weeks in advance for anything longer. Requests are
approved by the direct manager, who must respond within five working days.

Up to ten days may be carried into the following year. Anything beyond ten
days lapses on 31 July unless the manager has agreed in writing to carry it,
which is expected where the company asked the employee to defer leave.

3. PERSONAL AND CARER'S LEAVE
Ten days of paid personal leave a year, which may be used for the employee's
own illness or to care for an immediate family member. Personal leave
accumulates from year to year without limit.

A medical certificate is required for any absence of three consecutive days or
more, and for any absence adjoining a public holiday or a period of annual
leave.

4. PARENTAL LEAVE
Primary carers receive sixteen weeks at full pay, in addition to the government
paid parental leave scheme. Secondary carers receive four weeks at full pay.
Both are available after twelve months of continuous service, and may be taken
flexibly within twenty-four months of the birth or placement.

5. COMPASSIONATE LEAVE
Three days of paid leave per occasion on the death or life-threatening illness
of an immediate family member or household member. No certificate is required,
though the company may ask for evidence where the pattern gives it reason to.

6. UNPAID LEAVE
Unpaid leave may be granted at the company's discretion once paid entitlements
are exhausted. Service continues to accrue during approved unpaid leave of up
to three months.

7. PUBLIC HOLIDAYS
Victorian public holidays are observed. An employee required to work one is
paid at the applicable penalty rate or, by agreement, given a day in lieu.

8. LEAVE AT TERMINATION
Accrued and untaken annual leave is paid out on termination. Accrued personal
leave is not.

9. CASUAL STAFF
Casual employees receive a loading in place of paid leave. They are entitled to
two days of unpaid carer's leave per occasion and to unpaid compassionate
leave on the same terms as clause 5.

10. QUESTIONS
Anything this policy does not answer goes to the People team, who will answer
within two working days and update this document if the gap is a real one.
"""

_REFUND_POLICY = """CUSTOMER REFUND AND RETURNS POLICY
Version 6.0, effective 12 August 2026

1. THE PROMISE
Any item may be returned within thirty days of delivery for a full refund,
provided it is in a condition we can reasonably resell or the fault is ours.
This sits on top of the Australian Consumer Law, which we do not attempt to
limit and which continues to apply after these thirty days.

2. FAULTY OR DAMAGED GOODS
Report a fault or damage within thirty days and we refund in full, including
the original delivery charge, and cover return postage. We do not require the
original packaging. Photographs are enough evidence for anything under $200 and
we will not ask the customer to post a broken item back at their own expense.

For a major failure -- the item is unsafe, unfit for purpose, or significantly
different from its description -- the customer chooses between a refund, a
replacement and a repair. That choice is theirs, not ours, and it does not
expire at thirty days.

3. CHANGE OF MIND
Within thirty days, unused and in original condition: full refund of the item
price. Return postage is the customer's, unless the listing was misleading.
Items excluded from change-of-mind returns are named on the product page and
are limited to perishables, personalised items and opened hygiene products.

4. HOW LONG A REFUND TAKES
Refunds are issued to the original payment method within two business days of
the return being received or the fault being accepted. The customer's bank
then takes three to five business days, which is outside our control. Support
staff must quote the whole range and not only our part of it.

5. PARTIAL REFUNDS
A partial refund may be offered where an item is returned used but resellable,
or where the customer prefers to keep a slightly damaged item at a reduced
price. Anything above 30 per cent of the order value needs a team lead.

6. FAILED DELIVERY
Where a courier has failed to deliver after two attempts, the customer may have
a refund or a resend, at their choice, without returning anything. Repeated
failures by the same courier are logged and reviewed monthly.

7. AUTHORITY
Support staff may approve any refund up to $500 without escalation. Between
$500 and $2,000 needs a team lead. Above $2,000 needs the Head of Support.

8. WHAT WE DO NOT DO
We do not require a receipt where we can find the order ourselves. We do not
charge a restocking fee. We do not refuse a refund because the customer was
rude.
"""

_BRAND_GUIDELINES = """BRAND AND TONE GUIDELINES
Version 3.1, effective 3 March 2026

WHO WE SOUND LIKE
Warm but authoritative. We know what we are talking about and we do not need
to shout about it. The reader should finish a paragraph better informed and
slightly reassured.

THE FOUR RULES
1. Say the thing. Lead with the answer, then explain it. Never make a reader
   scroll to find out whether they are getting their money back.
2. Write like a person. Short sentences. Ordinary words. "We will refund you
   today", not "a refund will be processed in due course".
3. Never blame the customer. If a process confused somebody, the process is
   the problem.
4. No false urgency, no invented scarcity, no exclamation marks in support
   replies. Enthusiasm belongs in a product launch, not in an apology.

WORDS WE USE
Customer, not user. Colleague, not resource. Problem, not issue. Sorry, when we
are. Refund, not credit, unless it really is a credit.

WORDS WE DO NOT USE
Leverage, synergy, reach out, circle back, revert, kindly, as per, please be
advised, unfortunately at this time. "Unfortunately" is allowed once per reply
and only when something genuinely is.

VISUAL IDENTITY
Primary indigo #4f46e5. Ink #111827 for body text. Off-white #f8fafc for
backgrounds. One accent per layout, never two. Inter for interface text,
system serif for long-form. Minimum 16px body text, minimum 4.5:1 contrast, no
text over a busy photograph.

SOCIAL POSTS
LinkedIn: one idea per post, first line under twelve words, no more than three
hashtags, and never a hook that withholds the point. Instagram: the caption
carries the meaning, because the image will be seen without it half the time.
Alternative text is not optional on either.

APPROVALS
Anything published externally is approved by a person first. That includes
posts drafted by the AI workforce, which is why the drafts arrive in the
approval queue rather than on the timeline.
"""

_ENGINEERING_STANDARDS = """ENGINEERING STANDARDS
Version 2.4, effective 1 June 2026

CODE REVIEW
Every change is reviewed by one other engineer before merge. A review is
expected within one working day; if that cannot happen, say so rather than
letting the branch age. Reviewers comment on correctness, naming and tests,
and leave formatting to the formatter.

A review comment says what is wrong and why it matters. "This will break when
the list is empty" is a review. "I would not do it this way" is not.

BRANCHES AND COMMITS
Short-lived feature branches from main, named ``type/short-description``.
Commit messages explain why, since the diff already shows what. Rebase before
merge, squash only when the intermediate commits say nothing.

TESTS
A bug fix arrives with the test that would have caught it. New behaviour
arrives with tests for the ordinary case and for the boundary. Coverage is
watched but not targeted; a suite at 90 per cent that tests only getters is
worth less than one at 70 that tests decisions.

The suite must run in under five minutes locally. When it stops doing that,
fixing it becomes somebody's actual work rather than a background complaint.

DATABASE CHANGES
Migrations are reviewed as carefully as code, and separately from the change
that needs them. No migration drops a column in the same release that stops
writing to it. Anything that rewrites a table on a large database is
rehearsed against a copy first.

DEPENDENCIES
A new dependency needs a reason in the pull request description covering what
it does, what it would take to write instead, and who maintains it. The
standard library first, always.

ON-CALL AND INCIDENTS
One engineer on call per week, handing over on Monday morning. Every incident
gets a written review within five working days, blameless, published
internally, with actions assigned to named people and dates.

DOCUMENTATION
A service has a README covering what it does, how to run it locally, and who
to wake. A runbook covers the three things most likely to go wrong. Anything
else is optional and probably stale.
"""

_SPEC_PORTAL = """PROJECT SPECIFICATION: CUSTOMER SELF-SERVICE PORTAL
Status: approved for build. Target: Q4 2026.

PROBLEM
Sixty-one per cent of support tickets last quarter were one of three questions:
where is my order, how do I return this, and can I change my address. Each one
costs roughly nine minutes of a support colleague's time and gives the customer
an answer slower than a web page would.

GOALS
1. Let a customer see order status, start a return and change their address
   without contacting anybody.
2. Reduce those three ticket categories by half within two months of launch.
3. Do not make the experience worse for the customers who still want a person.

OUT OF SCOPE
Live chat. Loyalty points. Anything touching payment details, which stays with
the payment provider.

USER STORIES
As a customer I can sign in with a link sent to my order email, so I do not
have to remember another password.
As a customer I can see every order from the last twenty-four months with its
current courier status.
As a customer I can start a return, choose refund or replacement, and print a
label.
As a customer I can update my delivery address on an order that has not yet
been dispatched.
As a support colleague I can see everything the customer did in the portal on
the ticket, so I am not asking them to repeat themselves.

CONSTRAINTS
Courier tracking comes from three different carrier APIs, one of which has no
sandbox. Address changes must reach the warehouse system within fifteen
minutes or they are useless. Sign-in links expire after thirty minutes.

ACCEPTANCE
Median time to see order status under four seconds. Return flow completable on
a phone in under ninety seconds. WCAG 2.1 AA. No regression in the existing
ticket response time.

RISKS
The carrier without a sandbox is the schedule risk. The address-change window
is the correctness risk, and it needs a reconciliation job rather than
optimism.
"""

_SPEC_KNOWLEDGE = """PROJECT SPECIFICATION: INTERNAL KNOWLEDGE SEARCH
Status: in build. Target: Q3 2026.

PROBLEM
The company's documented knowledge is spread across Drive, Notion, Confluence
and four people's heads. New colleagues ask a question in Slack that is
answered in a document they had no way of finding, which wastes their time and
teaches them that asking is faster than looking.

GOALS
1. One search box that covers Drive, Notion and Confluence.
2. Every answer carries a citation to the document and the section it came
   from, so the reader can check it.
3. A document that changes is re-indexed within an hour.

OUT OF SCOPE
Anything that answers from a model's own memory without a citation. If the
knowledge base does not know, it says so.

APPROACH
Documents are chunked on heading boundaries, embedded, and stored with the
source id, the heading path and the last-modified time. A query retrieves
chunks, and the answer is composed only from what was retrieved. A retrieval
that returns nothing above the relevance floor produces "I could not find
this", not an invention.

ACCESS
The index respects the source system's permissions. A document nobody can read
is a document the search must not quote, which means permissions are checked at
query time and not only at index time.

ACCEPTANCE
Ninety per cent of a fifty-question evaluation set answered correctly with a
correct citation. No answer without a citation. Re-index within one hour of a
change.

RISKS
Stale content confidently cited is worse than no answer, so the age of a
document is shown beside every citation.
"""

_TROUBLESHOOTING = """SUPPORT TROUBLESHOOTING GUIDE
Version 5.3. Read this before drafting a reply.

HOW TO USE IT
Find the symptom, follow the checks in order, and use the wording given. The
wording is not a suggestion: it has been through legal and it avoids promising
things we cannot do.

1. "MY ORDER HAS NOT ARRIVED"
Check the tracking record first, not the customer's account.
  Marked delivered but not received: ask them to check with neighbours and any
  safe place named at checkout, then lodge a carrier investigation. Do not
  tell the customer to contact the carrier themselves.
  Stuck at the same depot for more than 72 hours: treat it as lost. Offer a
  resend or a refund immediately, at the customer's choice.
  Repeated "customer not available" with no card left: this is a courier
  failure, not a customer failure. Apologise for the courier, arrange a resend
  to a collection point or a refund, and log the courier reference so the
  monthly review sees it.
Never ask a customer to wait a further five business days more than once.

2. "THE ITEM ARRIVED DAMAGED"
Ask for one photograph. Under $200, refund or replace on the photograph alone
and do not ask for the item back. Over $200, arrange a prepaid return label.
Never ask for the original packaging.

3. "I WAS CHARGED TWICE"
Check for an authorisation hold, which looks like a second charge and clears
in three to five business days. If it is a genuine duplicate, refund the
duplicate the same day and say so plainly, including that their bank takes a
further three to five days.

4. "I CANNOT SIGN IN"
Reset link not arriving: check the address for a typo, then check whether the
account exists at all. Do not confirm or deny an account's existence to
someone who has not proved they own it.
Link expired: links last thirty minutes. Send a new one.

5. "I WANT TO CANCEL"
Not yet dispatched: cancel and refund in full immediately.
Dispatched: it becomes a return under the refund policy. Say that clearly, and
offer to arrange the label rather than making them do it.

6. WHEN TO ESCALATE
Any legal threat, any injury or safety claim, any media or public review
mention, any refund above your authority, any customer who has contacted us
more than three times about the same problem. Escalate to the support channel
with the ticket reference and the customer's own words.

7. TONE
One apology, meant. No blaming the courier by name to the customer. No
"unfortunately at this time". Say what happens next and when, then do it.
"""

_DEMO_DRIVE = (
    _demo_file('resume-arjun-mehta', 'Resume - Arjun Mehta (Backend Engineer).txt',
               'text/plain', '2026-09-07T18:31:00Z', 2280, _RESUME_ARJUN, 'resumes'),
    _demo_file('resume-sofia-rossi', 'Resume - Sofia Rossi (Full Stack).txt',
               'text/plain', '2026-09-06T09:12:00Z', 1740, _RESUME_SOFIA, 'resumes'),
    _demo_file('resume-david-oyelaran', 'Resume - David Oyelaran (Platform).txt',
               'text/plain', '2026-09-04T14:45:00Z', 1980, _RESUME_DAVID, 'resumes'),
    _demo_file('policy-annual-leave', 'Annual and Personal Leave Policy',
               'application/vnd.google-apps.document', '2026-07-01T00:30:00Z',
               0, _LEAVE_POLICY, 'documents'),
    _demo_file('policy-refunds', 'Customer Refund and Returns Policy',
               'application/vnd.google-apps.document', '2026-08-12T03:10:00Z',
               0, _REFUND_POLICY, 'documents'),
    _demo_file('brand-guidelines', 'Brand and Tone Guidelines',
               'application/vnd.google-apps.document', '2026-03-03T22:05:00Z',
               0, _BRAND_GUIDELINES, 'documents'),
    _demo_file('engineering-standards', 'Engineering Standards',
               'application/vnd.google-apps.document', '2026-06-01T01:20:00Z',
               0, _ENGINEERING_STANDARDS, 'documents'),
    _demo_file('spec-self-service-portal',
               'Spec - Customer Self-Service Portal.md', 'text/markdown',
               '2026-08-29T05:40:00Z', 2410, _SPEC_PORTAL, 'documents'),
    _demo_file('spec-knowledge-search', 'Spec - Internal Knowledge Search.md',
               'text/markdown', '2026-08-22T23:15:00Z', 2190, _SPEC_KNOWLEDGE,
               'documents'),
    _demo_file('support-troubleshooting', 'Support Troubleshooting Guide',
               'application/vnd.google-apps.document', '2026-09-02T04:00:00Z',
               0, _TROUBLESHOOTING, 'documents'),
    _demo_file('headcount-plan-fy27', 'Headcount Plan FY27.xlsx',
               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
               '2026-08-30T06:25:00Z', 48219, '', 'documents', readable=False),
    _demo_file('resumes-folder', 'Resumes', FOLDER_MIME,
               '2026-09-07T18:31:00Z', 0, '', 'root'),
)


# ===========================================================================
# The connector
# ===========================================================================

@register
class GoogleDriveConnector(Connector):
    """List, search, read and write files in one Google Drive."""

    key = 'google_drive'
    name = 'Google Drive'
    description = ('Reads company documents and candidate resumes, and files '
                   'the documents the workforce produces where people expect '
                   'to find them.')
    category = 'documents'
    icon = 'fa-folder-open'
    color = '#1a73e8'
    docs_url = 'https://developers.google.com/drive/api/v3/reference'

    config_fields = (
        ConfigField('access_token', 'OAuth access token', field_type='password',
                    secret=True,
                    help_text=('The quick path. Mint one at '
                               'developers.google.com/oauthplayground with the '
                               'drive scope. It expires after about an hour.')),
        ConfigField('refresh_token', 'OAuth refresh token', field_type='password',
                    secret=True,
                    help_text=('The durable path, used with the client id and '
                               'secret below to obtain access tokens as needed.')),
        ConfigField('client_id', 'OAuth client id',
                    help_text=('From an OAuth 2.0 Client ID at '
                               'console.cloud.google.com/apis/credentials, with '
                               'the Drive API enabled for that project.')),
        ConfigField('client_secret', 'OAuth client secret', field_type='password',
                    secret=True),
        ConfigField('root_folder_id', 'Default folder id',
                    help_text=('The folder id from the Drive URL. Uploads and '
                               'listings default to it.')),
        ConfigField('resumes_folder_id', 'Resumes folder id',
                    help_text='Where the HR employee looks for resumes.'),
        ConfigField('documents_folder_id', 'Company documents folder id',
                    help_text='Indexed into the knowledge base.'),
        ConfigField('export_google_docs_as', 'Export Google Docs as',
                    field_type='choice', default='text/plain',
                    choices=(('text/plain', 'Plain text'),
                             ('text/markdown', 'Markdown')),
                    help_text=('Google Docs have no bytes to download, so they '
                               'are exported. Markdown keeps the headings, '
                               'which helps the knowledge base chunk a '
                               'document sensibly.')),
    )

    operations = ('list_files', 'search_files', 'read_file', 'upload_file',
                  'create_folder', 'delete_file', 'get_file_metadata')

    def __init__(self, integration):
        super().__init__(integration)
        # One exchange per connector object, held in memory only.
        self._token_cache = ''

    # -- configuration -----------------------------------------------------

    def is_configured(self):
        """A credential that could actually be used, and nothing less.

        No field here is marked required, because either credential shape is
        acceptable and marking all four required would misdescribe both. The
        judgement therefore lives in this method rather than in the field
        definitions.
        """
        if self.has('access_token'):
            return True
        return all(self.has(key) for key in
                   ('refresh_token', 'client_id', 'client_secret'))

    def missing_settings(self):
        if self.is_configured():
            return []
        return ['OAuth access token, or a refresh token with its client id '
                'and secret']

    def _folder(self, folder_id=''):
        """Which folder a call means: what it asked for, else the default."""
        return str(folder_id or self.setting('root_folder_id') or '').strip()

    # -- tokens ------------------------------------------------------------

    def _access_token(self):
        """A usable bearer token, exchanging the refresh token if need be."""
        stored = str(self.setting('access_token') or '').strip()
        if stored:
            return stored
        if self._token_cache:
            return self._token_cache

        refresh = str(self.setting('refresh_token') or '').strip()
        client_id = str(self.setting('client_id') or '').strip()
        client_secret = str(self.setting('client_secret') or '').strip()
        if not (refresh and client_id and client_secret):
            raise RuntimeError(
                'No usable Google credential. Add an OAuth access token, or a '
                'refresh token with its client id and secret, on the '
                'Integrations page.')

        try:
            _status, body = self.request_json(
                TOKEN_URL, method='POST', form=True,
                payload={'client_id': client_id, 'client_secret': client_secret,
                         'refresh_token': refresh, 'grant_type': 'refresh_token'})
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_token_error(exc)) from exc

        token = (body or {}).get('access_token', '')
        if not token:
            raise RuntimeError(
                'Google accepted the refresh request but returned no access '
                'token. Check that the Drive API is enabled for this OAuth '
                'client and that the refresh token has not been revoked.')
        self._token_cache = token
        return token

    def _headers(self, extra=None):
        head = {'Authorization': f'Bearer {self._access_token()}'}
        head.update(extra or {})
        return head

    def _call(self, url, *, method='GET', payload=None, params=None):
        try:
            return self.request_json(url, method=method, headers=self._headers(),
                                     payload=payload, params=params)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_api_error(exc)) from exc

    def _download(self, url, params=None):
        """Fetch raw bytes rather than JSON.

        ``request_json`` on the base class is the right tool for the metadata
        calls and the wrong one here, because a document's body is not JSON and
        must not be parsed as if it were.
        """
        target = f'{url}?{urllib.parse.urlencode(params)}' if params else url
        request = urllib.request.Request(target, headers=self._headers(), method='GET')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_api_error(exc)) from exc

    # -- listing -----------------------------------------------------------

    def live_list_files(self, folder_id='', query='', limit=25, mime_type=''):
        folder = self._folder(folder_id)
        clauses = ['trashed = false']
        if folder:
            clauses.append(f"'{_escape(folder)}' in parents")
        if query:
            clauses.append(f"name contains '{_escape(query)}'")
        if mime_type:
            clauses.append(f"mimeType = '{_escape(mime_type)}'")

        _status, body = self._call(f'{API}/files', params={
            'q': ' and '.join(clauses),
            'fields': f'nextPageToken,{FILE_FIELDS}',
            'pageSize': _clamp(limit, 1, 200),
            'orderBy': 'modifiedTime desc',
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
        })

        files = [_shape_file(row) for row in (body or {}).get('files') or []]
        where = f'folder {folder}' if folder else 'the whole Drive'
        return self.ok(
            f'{len(files)} file{"" if len(files) == 1 else "s"} in {where}'
            + (f' matching "{query}"' if query else '') + '.',
            {'files': files, 'count': len(files), 'folder_id': folder,
             'query': query, 'mime_type': mime_type})

    def demo_list_files(self, folder_id='', query='', limit=25, mime_type=''):
        wanted = self._demo_parent(folder_id)
        rows = []
        for entry in _DEMO_DRIVE:
            if wanted and entry['parent'] != wanted:
                continue
            if query and query.lower() not in entry['name'].lower():
                continue
            if mime_type and entry['mime_type'] != mime_type:
                continue
            rows.append(_public(entry))
        rows = rows[:_clamp(limit, 1, 200)]
        return self.simulated(
            f'{len(rows)} file{"" if len(rows) == 1 else "s"} in the stand-in '
            f'Drive'
            + (f' under {wanted}' if wanted else '')
            + (f' matching "{query}"' if query else '')
            + '. These are simulated files, but their content is real enough to '
              'read, index and score.',
            {'files': rows, 'count': len(rows),
             'folder_id': str(folder_id or ''), 'query': query,
             'mime_type': mime_type, 'simulated': True})

    def _demo_parent(self, folder_id):
        """Map a configured folder id onto a section of the stand-in Drive."""
        raw = str(folder_id or '').strip()
        if not raw:
            return ''
        if raw == str(self.setting('resumes_folder_id') or '') or 'resume' in raw.lower():
            return 'resumes'
        if raw == str(self.setting('documents_folder_id') or '') or 'doc' in raw.lower():
            return 'documents'
        return ''

    # -- search ------------------------------------------------------------

    def live_search_files(self, query, limit=25):
        """Full-text search across the Drive the token can see."""
        if not str(query or '').strip():
            return self.failure('Give me something to search for.')

        _status, body = self._call(f'{API}/files', params={
            'q': f"fullText contains '{_escape(query)}' and trashed = false",
            'fields': f'nextPageToken,{FILE_FIELDS}',
            'pageSize': _clamp(limit, 1, 200),
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
        })

        files = [_shape_file(row) for row in (body or {}).get('files') or []]
        return self.ok(
            f'{len(files)} file{"" if len(files) == 1 else "s"} contain '
            f'"{query}".',
            {'files': files, 'count': len(files), 'query': query})

    def demo_search_files(self, query, limit=25):
        needle = str(query or '').strip().lower()
        if not needle:
            return self.failure('Give me something to search for.')
        rows = [_public(entry) for entry in _DEMO_DRIVE
                if needle in entry['name'].lower()
                or needle in (entry['content'] or '').lower()]
        rows = rows[:_clamp(limit, 1, 200)]
        return self.simulated(
            f'{len(rows)} of the {len(_DEMO_DRIVE)} files in the stand-in Drive '
            f'contain "{query}". Nothing real was searched.',
            {'files': rows, 'count': len(rows), 'query': query,
             'simulated': True})

    # -- read --------------------------------------------------------------

    def live_read_file(self, file_id, max_chars=DEFAULT_MAX_CHARS):
        """The text of one file, exporting or downloading as appropriate."""
        limit = _clamp(max_chars, 1000, 2000000)
        _status, meta = self._call(f'{API}/files/{urllib.parse.quote(str(file_id))}',
                                   params={'fields': ONE_FILE_FIELDS,
                                           'supportsAllDrives': 'true'})
        meta = meta or {}
        name = meta.get('name', '')
        mime = meta.get('mimeType', '')

        if mime == FOLDER_MIME:
            return self.ok(
                f'"{name}" is a folder, not a file. Use list_files with its id '
                'to see what is inside it.',
                {'file_id': file_id, 'name': name, 'mime_type': mime,
                 'content': '', 'truncated': False, 'is_folder': True})

        if mime in GOOGLE_EXPORTS:
            export_as = self._export_mime(mime)
            raw = self._download(
                f'{API}/files/{urllib.parse.quote(str(file_id))}/export',
                {'mimeType': export_as})
            text = raw.decode('utf-8', errors='replace')
            truncated = len(text) > limit
            return self.ok(
                f'Exported "{name}" as {export_as}, {len(text)} characters'
                + (f', truncated to {limit}.' if truncated else '.'),
                {'file_id': file_id, 'name': name, 'mime_type': mime,
                 'exported_as': export_as, 'content': text[:limit],
                 'truncated': truncated, 'url': meta.get('webViewLink', '')})

        if not _is_text(mime, name):
            note = BINARY_NOTES.get(mime, f'a {mime or "binary"} file')
            return self.ok(
                f'"{name}" is {note}. Its content cannot be read here: parsing '
                'that format needs a library this project deliberately does not '
                'have, and decoding the bytes as text would produce nonsense '
                'rather than content. Convert it to a Google Doc in Drive '
                '(File, Save as Google Docs) and read that instead.',
                {'file_id': file_id, 'name': name, 'mime_type': mime,
                 'content': '', 'truncated': False, 'readable': False,
                 'reason': f'{note} cannot be parsed with the standard library',
                 'size': meta.get('size', ''), 'url': meta.get('webViewLink', '')})

        raw = self._download(f'{API}/files/{urllib.parse.quote(str(file_id))}',
                             {'alt': 'media', 'supportsAllDrives': 'true'})
        text = raw.decode('utf-8', errors='replace')
        truncated = len(text) > limit
        return self.ok(
            f'Read "{name}", {len(text)} characters'
            + (f', truncated to {limit}.' if truncated else '.'),
            {'file_id': file_id, 'name': name, 'mime_type': mime,
             'content': text[:limit], 'truncated': truncated,
             'url': meta.get('webViewLink', '')})

    def _export_mime(self, mime):
        """Which text format a Google-native file is exported as."""
        if mime == 'application/vnd.google-apps.document':
            return str(self.setting('export_google_docs_as') or 'text/plain')
        return GOOGLE_EXPORTS.get(mime, 'text/plain')

    def demo_read_file(self, file_id, max_chars=DEFAULT_MAX_CHARS):
        limit = _clamp(max_chars, 1000, 2000000)
        entry = _find_demo(file_id)
        if entry is None:
            return CallResult(
                ok=False, demo=True, provider=self.key,
                error=(f'No file with id {file_id} in the stand-in Drive. Ids '
                       'look like demo-drive-policy-refunds and come from '
                       'list_files or search_files.'),
                data={'file_id': file_id, 'simulated': True})

        if entry['mime_type'] == FOLDER_MIME:
            return self.simulated(
                f'"{entry["name"]}" is a folder in the stand-in Drive. Use '
                'list_files with its id to see what is inside it.',
                dict(_public(entry), content='', truncated=False,
                     is_folder=True, simulated=True))

        if not entry['readable']:
            note = BINARY_NOTES.get(entry['mime_type'], 'a binary file')
            return self.simulated(
                f'"{entry["name"]}" is {note}. Even in the simulation this is '
                'reported as unreadable, because it would be unreadable live: '
                'parsing that format needs a library this project does not '
                'have. Convert it to a Google Sheet or export it as CSV.',
                dict(_public(entry), content='', truncated=False,
                     readable=False,
                     reason=f'{note} cannot be parsed with the standard library',
                     simulated=True))

        text = entry['content']
        exported = (self._export_mime(entry['mime_type'])
                    if entry['mime_type'] in GOOGLE_EXPORTS else '')
        return self.simulated(
            f'Read "{entry["name"]}" from the stand-in Drive, {len(text)} '
            f'characters'
            + (f', exported as {exported}' if exported else '')
            + (f', truncated to {limit}.' if len(text) > limit else '.'),
            dict(_public(entry), content=text[:limit],
                 truncated=len(text) > limit, exported_as=exported,
                 simulated=True))

    # -- write -------------------------------------------------------------

    def live_upload_file(self, name, content, folder_id='', mime_type='text/plain'):
        """A multipart upload, assembled by hand.

        Drive's multipart format is a JSON metadata part followed by the file
        content, separated by a boundary. There is no third-party library here
        to build that, and ``email.mime`` would fold and re-encode the body, so
        the bytes are assembled directly. It is thirty lines and it is exact.
        """
        folder = self._folder(folder_id)
        metadata = {'name': name or 'Untitled'}
        if folder:
            metadata['parents'] = [folder]

        payload = content if isinstance(content, bytes) else str(content).encode('utf-8')
        boundary = _boundary(name, len(payload))
        mime = mime_type or _guess_mime(name)

        body = b''.join([
            f'--{boundary}\r\n'.encode(),
            b'Content-Type: application/json; charset=UTF-8\r\n\r\n',
            json.dumps(metadata).encode('utf-8'), b'\r\n',
            f'--{boundary}\r\n'.encode(),
            f'Content-Type: {mime}\r\n\r\n'.encode(),
            payload, b'\r\n',
            f'--{boundary}--\r\n'.encode(),
        ])

        request = urllib.request.Request(
            f'{UPLOAD_API}?uploadType=multipart&supportsAllDrives=true'
            '&fields=id,name,mimeType,webViewLink,parents,modifiedTime,size',
            data=body, method='POST',
            headers=self._headers({
                'Content-Type': f'multipart/related; boundary={boundary}',
                'Content-Length': str(len(body)),
            }))

        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_api_error(exc)) from exc

        try:
            created = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            created = {}

        shaped = _shape_file(created)
        return self.ok(
            f'Uploaded "{shaped["name"] or name}" ({len(payload)} bytes, {mime})'
            + (f' into folder {folder}.' if folder else ' to the Drive root.'),
            dict(shaped, bytes_written=len(payload), folder_id=folder))

    def demo_upload_file(self, name, content, folder_id='', mime_type='text/plain'):
        payload = content if isinstance(content, bytes) else str(content).encode('utf-8')
        slug = _slug(name)
        folder = self._folder(folder_id)
        return self.simulated(
            f'Would upload "{name}" ({len(payload)} bytes, '
            f'{mime_type or _guess_mime(name)})'
            + (f' into folder {folder}' if folder else ' to the Drive root')
            + '. Nothing was written to a real Drive.',
            {'file_id': f'demo-drive-{slug}', 'name': name,
             'mime_type': mime_type or _guess_mime(name),
             'url': f'https://drive.google.com/file/d/demo-drive-{slug}/view',
             'folder_id': folder, 'bytes_written': len(payload),
             'preview': (payload[:200].decode('utf-8', errors='replace')),
             'simulated': True})

    def live_create_folder(self, name, parent_id=''):
        parent = self._folder(parent_id)
        metadata = {'name': name or 'Untitled folder', 'mimeType': FOLDER_MIME}
        if parent:
            metadata['parents'] = [parent]

        _status, body = self._call(
            f'{API}/files', method='POST', payload=metadata,
            params={'fields': 'id,name,mimeType,webViewLink,parents',
                    'supportsAllDrives': 'true'})
        shaped = _shape_file(body)
        return self.ok(
            f'Created the folder "{shaped["name"] or name}"'
            + (f' inside {parent}.' if parent else ' in the Drive root.'),
            dict(shaped, parent_id=parent))

    def demo_create_folder(self, name, parent_id=''):
        slug = _slug(name)
        parent = self._folder(parent_id)
        return self.simulated(
            f'Would create the folder "{name}"'
            + (f' inside {parent}' if parent else ' in the Drive root')
            + '. Nothing was created in a real Drive.',
            {'file_id': f'demo-drive-{slug}', 'name': name,
             'mime_type': FOLDER_MIME, 'parent_id': parent,
             'url': f'https://drive.google.com/drive/folders/demo-drive-{slug}',
             'simulated': True})

    def live_delete_file(self, file_id):
        """Move the file to the Drive trash rather than destroying it.

        ``files.delete`` is permanent and irreversible. Trashing is what a
        person doing this by hand would do, it is undoable from the Drive
        interface, and an AI employee should not be able to do worse than a
        person could undo.
        """
        _status, _body = self._call(
            f'{API}/files/{urllib.parse.quote(str(file_id))}', method='PATCH',
            payload={'trashed': True},
            params={'fields': 'id,name', 'supportsAllDrives': 'true'})
        return self.ok(
            f'Moved {file_id} to the Drive trash, where it can be restored for '
            '30 days.',
            {'file_id': file_id, 'trashed': True, 'permanently_deleted': False})

    def demo_delete_file(self, file_id):
        entry = _find_demo(file_id)
        name = entry['name'] if entry else str(file_id)
        return self.simulated(
            f'Would move "{name}" to the Drive trash, recoverable for 30 days. '
            'Nothing was removed from a real Drive.',
            {'file_id': file_id, 'name': name, 'trashed': True,
             'permanently_deleted': False, 'simulated': True})

    # -- metadata ----------------------------------------------------------

    def live_get_file_metadata(self, file_id):
        _status, body = self._call(
            f'{API}/files/{urllib.parse.quote(str(file_id))}',
            params={'fields': ONE_FILE_FIELDS, 'supportsAllDrives': 'true'})
        body = body or {}
        shaped = _shape_file(body)
        owners = [(row.get('displayName') or row.get('emailAddress') or '')
                  for row in body.get('owners') or []]
        return self.ok(
            f'"{shaped["name"]}" is {shaped["mime_type"]}, last modified '
            f'{shaped["modified"] or "at an unknown time"}'
            + (f', owned by {owners[0]}.' if owners else '.'),
            dict(shaped, owners=owners, created=body.get('createdTime', ''),
                 description=body.get('description', ''),
                 readable=bool(shaped['mime_type'] in GOOGLE_EXPORTS
                               or _is_text(shaped['mime_type'], shaped['name']))))

    def demo_get_file_metadata(self, file_id):
        entry = _find_demo(file_id)
        if entry is None:
            return CallResult(
                ok=False, demo=True, provider=self.key,
                error=(f'No file with id {file_id} in the stand-in Drive. The '
                       'ids it holds are: '
                       + ', '.join(row['file_id'] for row in _DEMO_DRIVE) + '.'),
                data={'file_id': file_id, 'simulated': True})

        return self.simulated(
            f'"{entry["name"]}" is {entry["mime_type"]}, last modified '
            f'{entry["modified"]}, {entry["size"]} bytes. From the stand-in '
            'Drive.',
            dict(_public(entry), owners=['stand-in Drive'],
                 characters=len(entry['content'] or ''), simulated=True))

    # -- probe -------------------------------------------------------------

    def live_probe(self):
        """Who the token belongs to, and how much room is left."""
        try:
            _status, body = self._call(f'{API}/about',
                                       params={'fields': 'user,storageQuota'})
        except RuntimeError as exc:
            return self.failure(str(exc))

        body = body or {}
        user = body.get('user') or {}
        quota = body.get('storageQuota') or {}
        used = _bytes_label(quota.get('usage'))
        total = _bytes_label(quota.get('limit'))
        folders = [label for label, key in
                   (('default', 'root_folder_id'), ('resumes', 'resumes_folder_id'),
                    ('documents', 'documents_folder_id')) if self.has(key)]

        return self.ok(
            f'Connected as {user.get("emailAddress") or user.get("displayName") or "the token owner"}, '
            f'using {used} of {total}. '
            + (f'Configured folders: {", ".join(folders)}.' if folders
               else 'No folder ids are configured, so listings cover the whole '
                    'Drive and uploads go to its root.'),
            {'account': user.get('emailAddress', ''),
             'display_name': user.get('displayName', ''),
             'storage_used': used, 'storage_limit': total,
             'configured_folders': folders})


# ===========================================================================
# helpers
# ===========================================================================

def _escape(value):
    """Escape a term for Drive's query language.

    Drive's ``q`` parameter wraps values in single quotes, so a backslash and
    an apostrophe both have to be escaped or the query becomes a syntax error
    -- and a name with an apostrophe in it is not exotic.
    """
    return str(value).replace('\\', '\\\\').replace("'", "\\'")


def _shape_file(row):
    """One Drive file, reduced to this connector's contract."""
    row = row or {}
    return {
        'file_id': row.get('id', ''),
        'name': row.get('name', ''),
        'mime_type': row.get('mimeType', ''),
        'modified': row.get('modifiedTime', ''),
        'size': row.get('size', ''),
        'url': row.get('webViewLink', ''),
        'parents': row.get('parents') or [],
    }


def _public(entry):
    """A stand-in file without its body, in the listing shape."""
    return {'file_id': entry['file_id'], 'name': entry['name'],
            'mime_type': entry['mime_type'], 'modified': entry['modified'],
            'size': entry['size'] or len(entry['content'] or ''),
            'url': entry['url'], 'parents': [entry['parent']]}


def _find_demo(file_id):
    """A stand-in file by id, or by name, or by the tail of its id."""
    wanted = str(file_id or '').strip().lower()
    if not wanted:
        return None
    for entry in _DEMO_DRIVE:
        if wanted in (entry['file_id'].lower(), entry['name'].lower()):
            return entry
    # Tolerate an id quoted without its prefix, which is what happens when a
    # model repeats the slug rather than the whole id.
    for entry in _DEMO_DRIVE:
        if entry['file_id'].lower().endswith(wanted):
            return entry
    return None


def _is_text(mime, name=''):
    """Whether these bytes will still mean something once decoded."""
    lowered = str(mime or '').lower()
    if lowered in BINARY_NOTES:
        return False
    if any(lowered.startswith(prefix) for prefix in READABLE_TEXT):
        return True
    if not lowered:
        # No mime type at all: fall back to the extension, which is all Drive
        # has given us to go on.
        guessed = (mimetypes.guess_type(name or '')[0] or '').lower()
        return any(guessed.startswith(prefix) for prefix in READABLE_TEXT)
    return False


def _guess_mime(name):
    return mimetypes.guess_type(str(name or ''))[0] or 'text/plain'


def _slug(name):
    """A stable, readable slug for a simulated file id."""
    cleaned = ''.join(character if character.isalnum() else '-'
                      for character in str(name or 'file').lower())
    trimmed = '-'.join(part for part in cleaned.split('-') if part)[:40]
    digest = hashlib.sha1(str(name or '').encode('utf-8')).hexdigest()[:6]
    return f'{trimmed or "file"}-{digest}'


def _boundary(name, size):
    """A deterministic multipart boundary.

    Deterministic rather than random so that the same upload produces the same
    request bytes, which makes a failure reproducible. The value only has to
    be absent from the body, and a hex digest is.
    """
    digest = hashlib.sha1(f'{name}|{size}'.encode('utf-8')).hexdigest()
    return f'aiworkforce{digest[:20]}'


def _bytes_label(value):
    """'1.4 GB', or 'unlimited' when Google reports no limit."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 'an unreported amount'
    for unit in ('bytes', 'KB', 'MB', 'GB', 'TB'):
        if number < 1024 or unit == 'TB':
            return f'{number:.1f} {unit}' if unit != 'bytes' else f'{number} bytes'
        number /= 1024
    return f'{number:.1f} TB'


def _clamp(value, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(number, high))


def _api_error(exc):
    """Google's JSON error body, turned into one readable sentence."""
    try:
        raw = exc.read().decode('utf-8', errors='replace')
    except Exception:                                              # noqa: BLE001
        raw = ''
    message = ''
    try:
        detail = json.loads(raw) if raw else {}
        if isinstance(detail.get('error'), dict):
            message = detail['error'].get('message', '') or ''
    except json.JSONDecodeError:
        message = raw[:200]

    if exc.code == 401:
        return ('Google rejected the credential (401). An access token from the '
                'OAuth playground lasts about an hour; add a refresh token with '
                f'its client id and secret for something durable. {message}').strip()
    if exc.code == 403:
        if 'insufficient' in message.lower() or 'scope' in message.lower():
            return ('The token does not carry the Drive scope this call needs '
                    '(403). Writing needs the drive scope rather than '
                    f'drive.readonly. {message}').strip()
        return ('Google refused the request (403). Either the Drive API is not '
                'enabled for this OAuth client, or the account cannot reach '
                f'that file. {message}').strip()
    if exc.code == 404:
        return ('Google could not find that file (404). Check the id -- it is '
                'the part of the Drive URL after /d/ or /folders/ -- and that '
                f'the account behind the token has been given access. {message}').strip()
    if exc.code == 429:
        return 'Google is rate limiting this client (429). Try again shortly.'
    return f'Google Drive returned HTTP {exc.code}. {message}'.strip()


def _token_error(exc):
    """The same courtesy for a failed refresh-token exchange."""
    try:
        raw = exc.read().decode('utf-8', errors='replace')
    except Exception:                                              # noqa: BLE001
        raw = ''
    code = ''
    try:
        code = (json.loads(raw) if raw else {}).get('error', '')
    except json.JSONDecodeError:
        code = raw[:200]

    if code == 'invalid_grant':
        return ('Google rejected the refresh token (invalid_grant). It has been '
                'revoked, or it belongs to a different OAuth client than the '
                'client id and secret configured here. Authorise once more and '
                'store the new refresh token.')
    if code == 'invalid_client':
        return ('Google rejected the OAuth client id or secret '
                '(invalid_client). Copy both again from '
                'console.cloud.google.com/apis/credentials.')
    return f'The token exchange failed: HTTP {exc.code} {code}'.strip()
