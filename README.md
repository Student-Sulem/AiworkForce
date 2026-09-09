# AI Workforce OS

Six AI employees with real capabilities, eleven connected company applications,
and one rule that governs all of it: **an employee does the work and proposes
the action; a person releases it.**

This is a Django application. It is not six chat windows with different system
prompts. Each employee has a job, a set of registered tools that reach company
systems, a memory, a task history and an audit trail. Anything an employee does
that stays inside the platform -- writing a job description, scoring a
candidate, breaking a requirement into work items, searching the knowledge
base -- happens at once. Anything that would leave the company -- an email, a
Slack message, a calendar invitation, a Jira or GitHub issue, a published post
-- stops in an approval queue with its full content attached, where a person
can edit it, approve it or reject it. Only approval executes it, and every step
is recorded.

Three mechanisms make that true rather than aspirational:

- **A tool registry.** A capability exists because a registered function
  exists. The employee's profile page and the list of tools offered to the
  language model are both generated from that one registry, so neither can
  drift from what the platform can actually do.
- **A proposed-action queue that holds the complete payload.** A tool marked as
  requiring approval never acts. It returns a proposal, and the framework turns
  that into a pending row. The proposing function has nothing to send with, so
  there is no code path that bypasses review.
- **Integrations as data.** Every connected application reads its endpoint and
  credential from a database row, entered on the Integrations page or by asking
  an employee to set it up. Nothing needs a code edit or an environment file.

---

## Running it

```bash
run.bat
```

That builds the virtual environment if there is not one, applies migrations,
provisions the six employees and every integration, seeds the knowledge base
and a demonstration company, and starts the server at <http://127.0.0.1:8000/>.
Pass a port to use a different one: `run.bat 8123`.

By hand:

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe manage.py migrate
venv\Scripts\python.exe manage.py seed_workforce
venv\Scripts\python.exe manage.py runserver
```

Sign in with one of the accounts `seed_workforce` prints:

| Username | Password   | Role          | Can                                                   |
|----------|------------|---------------|-------------------------------------------------------|
| admin    | admin123   | Administrator | everything, including configuring connected applications |
| manager  | demo1234   | Manager       | approve, edit and reject queued actions               |
| analyst  | demo1234   | Analyst       | talk to employees and do operational work             |
| viewer   | demo1234   | Viewer        | read everything, change nothing                       |

An `admin` account that already existed keeps its password.

### If you see "Couldn't import Django"

Django is installed inside this project's `venv`, not system-wide, so a plain
`python manage.py runserver` uses the system interpreter and cannot find it.
Either use `run.bat`, activate the environment first
(`.\venv\Scripts\Activate.ps1`), or name the interpreter every time
(`venv\Scripts\python.exe manage.py runserver`). On a fresh clone there is no
`venv` at all -- it is in `.gitignore` -- and `run.bat` creates it.

---

## The first ten minutes

Start on the **Dashboard**. Its readiness checklist says what is set up and
what is not, and links to the page that fixes each item. On a fresh install it
will say that every application is in demo mode and, unless you have added a
key, that no language model is connected. Neither stops you.

1. **Ask the Research employee something the company knows.** Open
   *AI Employees*, choose Knowledge & Research, and type
   *"What is our refund window, and how long does a refund take?"*
   The answer quotes the Customer Refund Policy and names the section. Ask a
   follow-up: *"Does any document disagree about the processing time?"* It will
   report that the refund policy says five business days and the troubleshooting
   guide says seven, and that it has chosen neither. That inconsistency is
   planted so you can see the behaviour.

2. **Ask it something the company has not documented.**
   *"What is our policy on cryptocurrency payments?"* It says the knowledge
   base does not cover it and names what it searched for, rather than inventing
   a policy.

3. **Have HR do real recruitment work.** Choose People Operations and type
   *"Score the candidates for the Senior Backend Engineer opening and shortlist
   the strongest."* Then open *Recruitment* and click a candidate: the score is
   broken down requirement by requirement, with the resume evidence beside each
   line.

4. **Watch the approval loop.** Still with People Operations:
   *"Invite the top candidate to a first interview next Tuesday at 10."*
   The reply says the invitation is prepared and waiting, and gives the action
   number. Open *Approvals*. The full email is there. Change a sentence and save
   -- it is still pending, and the page says so. Approve it. Open *Audit log*:
   proposed, edited, approved, executed, each with who and when. Because Gmail
   starts in demo mode the execution is labelled *simulated*.

5. **Try the Orchestrator.** Open *Orchestrator* and type
   *"A customer says they were charged twice; find out what our policy is and
   draft a reply."* It routes the request, shows why, and where a request needs
   more than one employee it proposes the sequence.

6. **Ask an employee to configure an application.** Any employee:
   *"Describe what is needed to connect Slack."* It lists every setting, which
   ones are credentials, and where to get them. Then
   *"Set the Slack default channel to #general."* The change queues for
   approval like anything else; a credential passed the same way is stored
   masked and never echoed back.

---

## The six employees

No employee has a personal name. Each is identified by its function, because a
department is something you hand a task to and hold to an outcome, and a
friendly first name invites the reader to treat the same object as a chat
companion -- the wrong expectation for something that files Jira issues.

| Employee | Type | What it does | Reaches |
|---|---|---|---|
| **People Operations** | `hr` | Job descriptions and scoreable requirements; resume analysis and requirement-by-requirement scoring; shortlisting; interview questions and evaluation forms; scheduling; candidate email; employee records, onboarding checklists, review templates; policy questions answered from the handbook | Gmail, Google Calendar, Google Drive, Slack, knowledge base |
| **Engineering Delivery** | `engineering_manager` | Projects and sprints; breaking requirements into work items and subtasks with acceptance criteria; estimates; dependency detection and build order; sprint planning against capacity; overdue and blocker reports; sprint, project and weekly reports; team notifications | Jira, GitHub, Slack, Google Calendar, Google Drive |
| **Software Development** | `developer` | Code, model, endpoint, query and test scaffolds; traceback analysis and fix suggestions; static bug checks; code and pull-request review; documentation, README sections, commit messages and PR descriptions; GitHub and Jira issues | GitHub, Jira, Slack, Google Drive |
| **Knowledge & Research** | `research` | Chunk-level search with citations; answers, summaries, comparisons and reports; conflict detection between documents; indexing documents by hand or by syncing a source; answering the other five employees | Google Drive, Notion, Confluence, GitHub, knowledge base |
| **Marketing & Communications** | `marketing` | Posts, captions, ads, blog copy, announcements and variants; campaigns, briefs, strategies and content calendars; marketing email and newsletters; brand-compliance checks; publishing held behind approval | LinkedIn, Instagram, Gmail, Slack, Google Drive, Google Calendar, knowledge base |
| **Customer Support** | `support` | Ticket intake and import from email; category, priority and sentiment triage with the evidence recorded; policy-grounded replies, apologies and refund responses; duplicates and recurring problems; escalation to Jira, Slack or a person; reports | Gmail, Jira, Slack, Google Drive, Google Calendar, knowledge base |

Every employee also has the shared platform tools: search the knowledge base,
ask the Research employee, delegate to a colleague, remember and recall,
describe and configure integrations, list its own capabilities and pending
approvals. Open an employee's profile page (`/employees/<type>/`) for the
generated capability list, the applications it reaches with each one's real
mode, its tasks with their working record, its memories and its instructions.

The Developer employee has no tool that writes to a working tree, commits,
merges or deploys. Everything it produces is a `CodeArtifact` row for a human
developer to read and apply by hand. The Support employee refuses to draft a
reply to any ticket marked `needs_human`, and refuses even to queue one.

---

## Connected applications

Eleven integrations, each a connector class plus a database row. The
Integrations page shows every one with its status, its effective mode, what is
still missing, and which employees and capabilities depend on it.

| Integration | Used for | Credential | Where to get it |
|---|---|---|---|
| Gmail | sending mail, reading the support inbox | mailbox address and an app password | myaccount.google.com/apppasswords, with two-step verification on |
| Slack | team notifications, alerts, escalation | bot user OAuth token (`xoxb-`) | api.slack.com/apps, scopes `chat:write`, `channels:read`, `users:read` |
| Google Calendar | interviews, meetings, customer calls | OAuth access token, or refresh token with client id and secret | console.cloud.google.com; the OAuth playground for a quick token |
| Google Drive | resumes, documents, uploads, knowledge sync | same as Calendar | same |
| GitHub | issues, pull requests, files, code search | personal access token | github.com/settings/tokens |
| Jira | issues, transitions, sprints | site URL, account email, API token | id.atlassian.com/manage-profile/security/api-tokens |
| Notion | internal wiki search and pages | internal integration token | notion.so/my-integrations, then share the pages with it |
| Confluence | documentation search and pages | site URL, account email, API token | as Jira |
| LinkedIn | publishing company posts | access token and author URN | a LinkedIn developer application |
| Instagram | publishing to a business account | page access token and account id | Meta for Developers |
| Company Knowledge Base | the internal retrieval engine | none | built in |

### Demo mode, stated plainly

An integration without a credential is not broken. Every connector implements
each operation twice -- a live path that speaks to the real service and a
simulated path that produces a deterministic, plausible result -- and reports
which one ran. That flag travels all the way to the screen: the tool result
says simulated, the execution record is marked simulated, the audit entry has
status `demo`, and the interface labels it. A simulated action is never shown
as a real one.

The integration's mode decides the choice:

| Mode | Behaviour |
|---|---|
| `auto` (default) | live when the credential is present, simulated when it is not |
| `live` | live only; a missing credential is an error, not a simulation |
| `demo` | always simulated, even with a working credential |

Running the test suite forces every connector into its simulated path
regardless of mode, because the connectors bypass Django's test email backend.

### What the real APIs cannot do

These are limits of the services, and the connectors say so rather than hide
them: GitHub's API has no wiki endpoint, so the repository's markdown
documentation is returned instead; LinkedIn's API cannot schedule a post, so a
scheduled post is held by the platform; Instagram cannot publish without a
publicly reachable image URL; LinkedIn people search has no public endpoint.

---

## Configuring it without touching code

Two ways, both writing the same database row.

**On the Integrations page.** Press Configure on a card. The form is generated
from the connector's declared settings, with help text saying where each
credential comes from. A secret shows as *(set)* or *(not set)*; its value is
never rendered back.

**By asking an employee.** `platform.describe_integration` explains what a
connection needs; `platform.configure_integration` proposes the change, which
queues for approval with credentials masked in the review. On approval the
values are written -- plain settings to `config`, credentials to `secrets` --
and the connection is tested. Credentials passed through a chat are redacted
before the tool call is written to the audit log, so a token typed once cannot
leak into a table every signed-in person can read.

Company-wide settings -- name, timezone, working hours, signature, tone,
approval reminder period -- live on the *Company settings* page and are read by
every employee before it writes anything.

---

## The approval queue

The lifecycle of every external action:

```
employee calls a gated tool
  -> Proposal (title, summary, complete payload, editable fields)
  -> ProposedAction, status pending
  -> a person edits (still pending), approves or rejects
  -> approve: status approved, then executing, then executed or failed
  -> execution record (message, calendar event, external issue), marked live or simulated
  -> audit trail throughout
```

Editing does not approve. The employee's own version is kept in
`original_payload`, so the difference between what was proposed and what was
sent is always recoverable and is shown on the review page. A high-risk action
-- one that reaches somebody outside the company -- asks for confirmation. A
failed execution can be retried.

Gated actions: sending any email, posting to Slack, creating or changing a
calendar event, creating or updating a GitHub or Jira issue, publishing or
scheduling a social post, uploading a file to Drive, publishing to Notion, and
changing an integration's configuration, mode or enabled state.

---

## The pages

| Sidebar | Page | What it shows |
|---|---|---|
| Workspace | Dashboard | readiness checklist, what needs a person, the roster, figures per area, recent activity |
| | AI Employees | chat with any employee; the employee's profile is one click away |
| | Orchestrator | describe what you need; see who was chosen, why, and what it did |
| Governance | Approvals | the proposed-action queue with filters, bulk decisions and the review page |
| | Drafts | the older text-approval queue for chat replies submitted as posts or emails |
| | Audit log | every event, filterable by category, status, employee, application and text |
| Operations | Recruitment | openings, the candidate pipeline board, interviews; candidate pages with the scoring evidence |
| | People | the employee directory and onboarding progress |
| | Engineering | projects, the active sprint, the work-item board, overdue and blocked items |
| | Support | the ticket queue with SLA and needs-a-person markers; ticket threads |
| | Content | campaigns and briefs, the copy with channel limits and sources, the calendar, email |
| | Knowledge | live search with cited passages, documents, sources with sync, add a document |
| Platform | Integrations | every connected application, its mode, configuration and dependants |
| | Company settings | the settings every employee reads |
| | Language models | providers, keys and model catalogues |
| | MCP servers | the Model Context Protocol server layer |
| | Users | accounts and roles |

---

## Roles

Four roles, implemented as Django groups whose permission sets are declared in
`marketing/roles.py` and applied by `sync_groups`. The Viewer set is a
wildcard, `marketing.view_*`, expanded at sync time so a model added later is
readable without anyone editing the list.

| Role | Adds |
|---|---|
| Viewer | read every page |
| Analyst | talk to employees; add and change operational records |
| Manager | approve, edit, reject and retry proposed actions; manage employees and MCP servers; test integrations |
| Administrator | configure integrations and company settings; language-model keys; user management |

---

## The language model

The platform works without one. An employee with no reachable model chooses
the tool that best matches the request, runs it, and answers from the real
result -- so records are still created and lookups still cited. What it cannot
do is write prose. Connect a provider on *Language models*: OpenRouter (free
models exist), NVIDIA NIM (free developer tier) or a local Ollama (no key). The
provider client speaks the OpenAI-style tools protocol and also recognises the
text-embedded tool calls that smaller open models emit.

---

## Commands

| Command | Does |
|---|---|
| `manage.py seed_workforce [--reset] [--minimal] [--owner USER] [--noinput]` | provision everything and seed the demonstration company; idempotent |
| `manage.py sync_workforce [--prompts] [--reindex] [--probe]` | regenerate roles, integrations, settings and capabilities from the code |
| `manage.py workforce_status` | one-screen health report with numbered next steps |
| `manage.py seed_data` | the original marketing demonstration data |
| `manage.py sync_roles` | reapply the permission matrix |
| `manage.py test marketing` | the test suite; every connector simulates while it runs |

---

## Safety notes

**If Gmail has real credentials, approving an email really sends it.** That is
the correct behaviour for the finished product and the reason every address in
the demonstration data is on a reserved domain (`example.com`) that cannot
receive mail. Anyone replacing the seed with their own data should keep that
property. During development the Gmail integration was left in `demo` mode;
switch it to `auto` on the Integrations page when you want it live.

The Developer employee cannot change code, and the Support employee will not
draft a reply for a ticket flagged for a person. Neither is a prompt
instruction; both are the absence of a tool.

---

## Troubleshooting

Run `manage.py workforce_status` first. Then:

- **Pages are empty.** `manage.py seed_workforce` provisions and seeds. It is
  safe to run again.
- **A migration error on start.** `manage.py migrate`, then `seed_workforce`.
- **An employee answers without doing anything.** No model is reachable and
  the request matched no tool. The reply lists the employee's capabilities;
  phrase the request in those terms, or connect a model.
- **Provider returns 401 or 503.** The key is wrong or the service is down.
  Test it on *Language models*; the employees keep working in fallback mode.
- **An integration says failed rather than demo.** A credential is present but
  rejected. Fix it, or set the mode to `demo` to simulate meanwhile.
- **The knowledge base is empty.** `manage.py seed_workforce --minimal` seeds
  the twelve starter documents; the Knowledge page adds more.

---

## What is deliberately not built

No background worker, so a post or email scheduled for later is held and
surfaced rather than sent at that time. No OAuth flow in the interface, so a
Google token is pasted rather than granted. Retrieval is BM25 over passages,
not embeddings -- deterministic, explainable, and dependent on shared
vocabulary. Credentials are stored in the database without encryption at
rest, which is acceptable for coursework and not for production. See
`docs/ARCHITECTURE.md` for how the pieces fit and how to extend them.
