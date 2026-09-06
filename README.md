# AI Workforce

A Django application in which you hold a conversation with four AI employees,
and everything they draft — social posts, outreach email, strategy insights —
waits in an approval queue until a person releases it.

The governing rule of the whole system: **agents propose, people dispose.**

---

## Running it

```bash
venv\Scripts\python.exe manage.py migrate
venv\Scripts\python.exe manage.py seed_data --fix-orphans
venv\Scripts\python.exe manage.py runserver
```

### Sign-in details

One account per role, so the permission system can be demonstrated by signing
in as each in turn.

| Username | Password | Role | What they can do |
|---|---|---|---|
| `admin` | `admin123` | Administrator | Everything. Also a Django superuser, so it bypasses permission checks entirely and can open `/admin/`. |
| `manager` | `Manager12345` | Manager | Approve and reject work; add, edit and remove AI employees and MCP servers. Cannot touch API keys or accounts. |
| `analyst` | `Analyst12345` | Analyst | Talk to employees and submit their output for approval. Cannot approve their own work. |
| `viewer` | `Viewer12345` | Viewer | Read everything. Cannot chat, submit or approve. |
| `demo` | `demo12345` | Manager | A second Manager, useful for showing two people in the same shared workspace. |

Run the tests with `venv\Scripts\python.exe manage.py test marketing` (193 tests).

### Configuration: the `.env` file

Credentials live in a file called `.env` at the project root. It is read on
every start-up by `marketpulse/env.py`, which `settings.py` calls before any
setting is evaluated, so the configuration survives closing the terminal.

```
cp .env.example .env
```

Then fill in the values you have. Every one is optional; the application runs
with an empty file and says plainly which features are offline.

| Variable | Effect when set |
|---|---|
| `NVIDIA_API_KEY` | Employees generate real replies through NVIDIA NIM |
| `OPENROUTER_API_KEY` | The same, through OpenRouter |
| `EMAIL_HOST_USER` | The mailbox approved outreach is sent from |
| `EMAIL_HOST_PASSWORD` | Its password — for Gmail, an App Password |
| `EMAIL_HOST`, `EMAIL_PORT` | Only for a provider other than Gmail |

Three properties of this arrangement are worth stating, because a marker will
ask about each:

- **`.env` is listed in `.gitignore`,** so a real password is never committed.
  `.env.example` is the file that is shared: same variable names, no values.
- **A real environment variable overrides the file.** The file is a default,
  not an override, so a server or a CI runner can inject its own values without
  editing anything:
  `$env:NVIDIA_API_KEY = "nvapi-..."` still wins for that one terminal.
- **Nothing is written to `db.sqlite3`.** `LLMProvider.key_source` reports
  `environment`, `database` or `none`, and the Configurations page shows which
  applies to each provider, with the key itself masked.

`marketpulse/env.py` is about thirty lines of standard library. Adding
`python-dotenv` to read `KEY=value` would not have been a good trade, and the
project deliberately has no third-party dependencies beyond Django.

### Getting live replies

Without an API key the employees answer from built-in templates, and every
reply is labelled "Template reply" so nobody is misled. Put `NVIDIA_API_KEY`
in `.env` and restart the server to get real generation.

A key saved through the Configurations page is stored in the database and takes
precedence over the environment; the page shows which of the two each provider
is using. Ollama runs locally and needs no key.

### Managing the mailbox by typing

Aria does not only write email; she can read the mailbox too. Everything below
is typed into the chat on the AI Employees page:

| What you type | What happens |
|---|---|
| `any new mail?` | lists your unread messages as cards |
| `check my inbox`, `show my last 5 emails` | lists recent messages |
| `anything from nvidia?`, `find mail about the timetable` | searches, using Gmail's own query syntax |
| `open 2`, `read the third one` | opens that message in full |
| `reply to 2 saying thanks` | opens it and drafts a reply to the sender |
| `email sam@example.com about pricing` | drafts an email, ready to send in one click |
| `save that as a draft` | saves it to your Gmail Drafts folder |

Reading uses `imaplib` from the standard library (`marketing/gmail_client.py`)
with the same credentials SMTP uses, so nothing extra needs configuring. Three
rules that module keeps:

- **It never fetches the whole mailbox.** The account this was built against
  holds 10,669 messages. Every call is bounded and asks for headers only unless
  a body was requested.
- **It never changes what it reads.** The mailbox is opened read-only and
  bodies are fetched with `BODY.PEEK[]`, so looking at a message through Aria
  does not mark it read in Gmail.
- **It never raises into a view.** A mailbox that is offline or refusing the
  password produces a readable sentence, not a 500.

**Which tool runs is decided in Python, not by the model.** `marketing/mail_agent.py`
matches what you typed against a small set of patterns and calls the tool
itself. Asking the language model to choose would be an extra round trip and
would make the mailbox stop working whenever the model was slow or absent —
and this project is built to work offline. The model still writes: it phrases
the answer and composes the emails. It is simply never what decides whether to
open your mailbox.

**The MCP tool record is what grants access.** Every mailbox action is gated on
an enabled `MCPTool`, attached to that employee, on an enabled `MCPServer`.
Switching Gmail off on the MCP Tools page genuinely takes the capability away,
and each call is written to `MCPCallLog` with its arguments, outcome and
duration. That log now contains only calls that actually ran.

### Sending, in one click

When Aria writes an email and the address is already known — because you named
it, or because it is a reply to a message she opened — the reply carries an
**Approve and send to …** button.

Nothing is skipped. An `ApprovalRequest` is still created, the decision is
still recorded against the person who made it, and the audit log still gets
both entries, so the Approvals queue remains a complete record of everything
ever sent. What is removed is the dialog asking for an address you gave a
moment ago, and the trip to another page. The two-step route is unchanged and
still there.

The button needs both `add_approvalrequest` and `approve_approvalrequest`,
because it performs both actions: an Analyst may submit, but not decide.

### Sending real email

Approving an outreach email in the queue is what actually sends it. The mail
credentials come only from `.env`, and unlike the LLM keys there is deliberately
**no way to enter them through the interface** — a mailbox password should not
be typed into a form or written to `db.sqlite3`.

```
EMAIL_HOST_USER=you@gmail.com
EMAIL_HOST_PASSWORD=your-app-password
```

For Gmail this must be an [App Password](https://myaccount.google.com/apppasswords)
with two-step verification enabled, not the account password. Google shows it
in four groups of four; the spaces are presentation only and `settings.py`
strips them, so the value can be pasted exactly as displayed.

**Without those two variables nothing breaks.** Django falls back to the console
backend, so an approved email is printed to the terminal instead of sent, and
the Configurations page says so plainly rather than pretending it went out.

Then open **Configurations**, press **Refresh models** to pull the live
catalogue, and assign a model to an employee from the configuration panel on
the chat screen.

**A note on NVIDIA NIM.** Its `/models` endpoint lists the entire public
catalogue — 81 entries on a typical account — but only a subset is actually
provisioned for any given key. Calling one of the others returns HTTP 404, and
the client reports that as *"That model is probably not available to your
account"* rather than blaming the base URL. `nvidia/nemotron-3-super-120b-a12b`
was verified working and follows a system prompt closely; it is the recommended
default.

---

## The six pages

| Page | URL | View | Template |
|---|---|---|---|
| Dashboard | `/workspace/` | `dashboard_view` | `dashboard.html` |
| AI Employees | `/agents/`, `/agents/c/<id>/` | `agents_view` | `agents.html` |
| Approvals | `/approvals/` | `approvals_view` | `approvals.html` |
| Users | `/users/` | `users_view` | `users.html` |
| Configurations | `/configurations/` | `configurations_view` | `configurations.html` |
| MCP Tools | `/mcp-tools/` | `mcp_tools_view` | `mcp_tools.html` |

Plus two detail pages (`approval_detail`, `mcp_server_detail`), the public
landing page, and sign in / sign up.

---

## Access control

**One shared workspace.** The application serves a single organisation, so
campaigns, prospects, AI employees, providers, MCP servers and the approval
queue are visible to everyone who signs in. The `user` foreign key on those
models records who *created* a row; it is not an access boundary. Chat threads
are the one exception: a conversation belongs to whoever started it, and
opening someone else's returns 404.

**Roles are Django Groups.** Nothing here reinvents permissions:

    Role  ->  auth.Group  ->  auth.Permission  ->  user.has_perm(...)

The matrix lives in `marketing/roles.py`. Four custom permissions are declared
in the models' `Meta.permissions` (`approve_approvalrequest`, `test_mcpserver`,
`test_llmprovider`, `assign_role`); the rest are the add/change/delete/view
permissions Django creates for every model.

| | Viewer | Analyst | Manager | Administrator |
|---|:---:|:---:|:---:|:---:|
| Read every page | ✓ | ✓ | ✓ | ✓ |
| Chat with employees | | ✓ | ✓ | ✓ |
| Submit work for approval | | ✓ | ✓ | ✓ |
| Approve or reject | | | ✓ | ✓ |
| Add, edit, remove employees | | | ✓ | ✓ |
| Manage MCP servers and tools | | | ✓ | ✓ |
| Test a provider connection | | | ✓ | ✓ |
| Change API keys and endpoints | | | | ✓ |
| Create, edit, delete accounts | | | | ✓ |

`Profile.role` is the human-facing label. A `post_save` signal moves the user
into the matching group whenever it changes, so editing a role on the Users
page, in the Django admin, or from a shell all take effect identically.

**The same rule hides a control and refuses a request.** Templates gate with
`{% if perms.marketing.approve_approvalrequest %}`; views gate with
`@permission_required` or `has_perm`; JSON endpoints return 403. A Viewer who
guesses a URL gets the same refusal as a Viewer who cannot see the button.

**One ordering subtlety worth knowing.** Django creates its `Permission` rows
in a `post_migrate` receiver, which runs *after* every migration. A data
migration that tried to assign permissions to groups would therefore find none
and leave every group empty — invisible on an existing database, and a total
lockout on a fresh one. So migration 0008 only creates the groups, and the
`post_migrate` receiver in `marketing/signals.py` applies the matrix. Running
`manage.py sync_roles` re-applies it after editing `roles.py`.

---

## How the application works

**AI Employees is a chat screen.** A conversation rail on the left, the active
thread in the middle, and a slide-out panel on the right holding that
employee's system prompt, language model and MCP tools. You talk to an
employee; you do not trigger it.

**Nothing an employee writes leaves the application on its own.** Every reply
carries a "Send for approval" action. Pressing it creates a draft record — a
`SocialPost`, an `EmailOutreach`, or nothing at all for a text-only insight —
and puts it in the queue. Only approving it there publishes anything.

**There is no "run" any more.** An earlier version had a Run button per
employee that fired a scripted task. That is gone entirely: the four
`run_*_agent` functions, the `/api/trigger-agent/` endpoint, the run terminal
modal and the separate employee detail page have all been removed. Two tests
(`test_the_run_endpoint_is_gone` and `test_deleted_pages_are_really_gone`)
assert that those routes no longer reverse, so the removal cannot silently
regress.

---

## Answering the assessment criteria

### Models — `marketing/models.py`

Seventeen models in six layers:

1. **Identity** — `Profile` extends `auth.User` through a `OneToOneField`. A
   custom `AUTH_USER_MODEL` was rejected because the user model cannot be
   swapped after the first migration is applied, and this project already had
   live data.
2. **Intelligence** — `LLMProvider` (OpenRouter, NVIDIA NIM, Ollama) and
   `LLMModel`, the catalogue behind the cascading dropdown.
3. **Capability** — `MCPServer`, `MCPTool`, `AgentToolLink`, `MCPCallLog`.
4. **Workforce** — `AIAgent`, `MarketingCampaign`, `Lead`, `SocialPost`,
   `EmailOutreach`, `AnalyticsMetric`.
5. **Conversation** — `Conversation` and `ChatMessage`.
6. **Governance** — `ApprovalRequest` and `ApprovalAuditLog`.

Points worth being able to explain:

- **`AgentToolLink` is a `through` model, not a plain `ManyToManyField`.** A
  plain M2M could not record *when* a tool was attached, *by whom*, whether it
  is enabled for that particular employee, or how often it was used.
- **`ChatMessage.role` uses the same vocabulary as the chat completion APIs**
  (`user`, `assistant`, `system`), so a stored conversation replays straight
  into a request body with no translation step.
- **`ApprovalRequest` uses three nullable foreign keys, not a
  `GenericForeignKey`.** Concrete keys give real referential integrity, let
  `select_related` fetch the queue in one query instead of N+1, and let
  `list_filter` reach through to `social_post__platform`. A database
  `CheckConstraint` guarantees at most one of the three is populated.
- **`ApprovalRequest.source_message` is provenance, not a target.** It records
  which chat reply a reviewer submitted, and sits deliberately outside that
  check constraint, which governs only the three target links.
- **`CheckConstraint` takes `condition=`,** not the `check=` keyword that was
  removed in Django 6.0.
- **Four custom permissions** are declared in `Meta.permissions`, which is how
  a capability that is not simply add/change/delete/view gets a real Django
  `Permission` row to hang off.
- **The unique constraints are workspace-wide**, not per user: one AI employee
  of each type, one row per provider, one per MCP server. Migration 0006
  merged the per-user duplicates that the old design had created, repointing
  every foreign key before deleting anything.

### Views — `marketing/views.py`

Eight page views and sixteen JSON endpoints. Every page view is decorated with
`@login_required`; every JSON endpoint additionally carries `@require_POST` and
scopes its lookups with `user=request.user`.

The previous version had four `@csrf_exempt` endpoints with no authentication
at all. Those decorators are gone: the browser now sends the CSRF token in an
`X-CSRFToken` header, read from the hidden `{% csrf_token %}` that `base.html`
renders on every page.

`agents_view` serves two URL patterns. Without a conversation id it opens the
most recent thread, and creates one if the account has none, so the page is
never empty.

### Create, edit and delete

| Record | Add | Edit | Delete | Who |
|---|---|---|---|---|
| AI employee | `/agents/new/` | configuration panel in the chat | `/agents/<id>/delete/` | Manager |
| MCP server | `/mcp-tools/new/` | `/mcp-tools/<id>/` | `/mcp-tools/<id>/delete/` | Manager |
| MCP tool | on the server page | on the server page | `/mcp-tools/tool/<id>/delete/` | Manager |
| Person | `/users/new/` | `/users/<id>/edit/` | `/users/<id>/delete/` | Administrator |
| Conversation | chat rail | rename in the header | header, or the API | Analyst |
| Campaign | dashboard modal | — | — | Analyst |

Every delete is **POST only**, so no link, prefetch or crawler can trigger one,
and each is behind a confirmation. Two guards exist because they would
otherwise be easy to trip over during a demonstration: nobody can delete or
demote their own account, and the last remaining Administrator cannot be
removed.

### URLs — `marketing/urls.py`

Every path maps to exactly one view and carries a `name`, so templates link
with `{% url 'approvals' %}` rather than a hard-coded path. The module
deliberately does **not** define `app_name`; adding a namespace would require
rewriting every `{% url %}` tag in the project.

### Templates — `templates/`

Two shells (`base.html` for signed-in pages, `base_public.html` for public
ones), ten page templates, and twelve partials.

The project convention, stated in a comment at the top of `base.html`:

- **Blocks** compose through inheritance and may wrap arbitrary markup, so they
  carry per-page regions: title, subtitle, header actions, content, modals.
- **Includes** receive a copy of the context but cannot wrap child markup, so
  they carry repeated data-driven fragments: a KPI tile, a status badge, a form
  field, a chat message.
- A block declared *inside* an included template can never be filled by the
  page that triggered the include. That is why the page heading is a block in
  `base.html` rather than one inside `partials/_topbar.html`.

`partials/_chat_message.html` is worth pointing at: `static/js/chat.js` builds
the same markup client-side when a reply arrives, so a message looks identical
whether Django rendered it or JavaScript did.

### Admin — `marketing/admin.py`

**`AIAgentAdmin` is the flagship customised admin.** `AIAgent` backs the AI
Employees page and carries a foreign key, a `through` many-to-many, two reverse
relations, numeric performance fields and colour fields, so it exercises every
technique without contrivance:

- `fieldsets` with collapsible sections and descriptions
- Three `inlines`: `AgentToolLink` (editable), `Conversation` and
  `ApprovalRequest` (both read-only history)
- `list_display` with custom `@admin.display` methods rendering HTML — a colour
  avatar, a link to the owner, and an inline success-rate bar
- `list_editable`, `list_filter` (including a relation traversal and a date
  filter), `date_hierarchy`, `search_fields`, `autocomplete_fields`
- `readonly_fields` that are computed, not stored
- Four bulk `actions`
- `get_queryset` (`select_related` + `annotate`, and per-user scoping),
  `formfield_for_foreignkey`, `save_model` and `save_formset` overrides

`ConversationAdmin` nests `ChatMessageInline`, so a whole thread can be read
inside the admin.

---

## The service layer

### `marketing/llm_client.py` — live provider calls, standard library only

Real HTTP calls to OpenRouter, NVIDIA NIM and Ollama using `urllib.request`,
so the virtual environment needs nothing beyond Django.

`chat_conversation()` is the multi-turn entry point: it prepends the system
prompt to a list of turns and posts them, which is the shape all three
providers expect. `chat_completion()` is a single-turn wrapper around it.

**Every public function is total** — none of them raises into a view.
`fetch_models()` never returns an empty list: on any failure it falls back to a
curated catalogue. `chat_conversation()` returns `ok: False` and the employee
answers from a template instead. **A conversation therefore always gets a
reply, with or without a network connection**, which is what makes the
application safe to demonstrate offline.

Two details that would otherwise break a demonstration are handled explicitly:
Ollama's `/api/chat` streams newline-delimited JSON unless `"stream": false` is
sent, and `HTTPError` must be caught before `URLError` because it is a subclass.

**API keys** resolve database first, environment second, through
`resolve_api_key()`. Setting `NVIDIA_API_KEY` or `OPENROUTER_API_KEY` in the
environment means a real credential never has to be typed into a form, written
to `db.sqlite3`, or committed. `LLMProvider.is_configured` consults the
environment too, which matters because `AIAgent.has_live_llm` depends on it: an
employee whose provider is configured that way must count as having a live
model. A key is never rendered back to the browser in full; `masked_key` shows
only the first six and last four characters.

**Two timeout budgets, not one.** Listing models or testing a connection is a
cheap metadata call and fails fast at 8 seconds. Generating a reply gets 60.
This is not tidiness: a serverless model endpoint that has scaled to zero can
take 15-20 seconds to answer its first request, and a live reply measured at
12.8 seconds during testing would have silently fallen back to a template under
a shared 8-second budget — making a perfectly valid API key look broken.

**Reasoning models are sanitised.** Some models show their working before the
answer, either in a `<think>` block or under a heading such as "Here's a
thinking process:". `strip_reasoning()` removes both, so a chain of thought
never ends up in a published social post.

### `marketing/mailer.py` — the only path that sends email

Aria drafts; a person approves; `apply_approval` calls `send_outreach`. There
is no other route by which this application sends mail, which is what makes the
approval queue meaningful rather than decorative.

Two behaviours are worth being able to explain:

- **A delivery failure never discards the decision.** If the mail server
  refuses the credentials, the approval still stands, the `EmailOutreach` row
  records `status='bounced'` and why, the prospect is *not* marked contacted,
  and the reason is written into the audit trail. Losing an approval because a
  network blipped would be the worse failure.
- **The prospect's status only advances on success.** Marking someone
  "contacted" when the message bounced would corrupt the pipeline.

`send_test_message` is deliberately separate: it touches no prospect, no
approval and no stored row, so it is safe to press twice.

### `marketing/mcp_client.py` — MCP modelled in Django

A real MCP client launches a server process and speaks JSON-RPC over it. This
project stores, validates and reports on each server's configuration, and
`simulate_handshake` reproduces what an `initialize` exchange would conclude —
`connected`, `degraded`, `failed` or `disabled` — without launching anything.
The docstring says so plainly rather than overclaiming.

`run_agent_tools` is called once per chat turn, so each reply records which
capabilities were in play when it was written.

### `marketing/agent_engine.py` — provisioning and conversation

`ensure_workspace_for_user` is idempotent and runs on every login, giving each
account four AI employees, three providers with seeded catalogues, and seven
MCP servers with 22 tools.

`send_message` is the heart of the chat: it stores the person's turn, replays a
bounded window of history (`CONTEXT_TURNS = 12`) to the employee's assigned
model, records the MCP tools consulted, and stores the reply. A thread names
itself from its opening question — but only while its title is still the
untouched default, so a deliberate rename is never overwritten.

`submit_message_for_approval` is the **only** route by which chat output can
reach the outside world, and a person always chooses to take it.

`apply_approval` is the single place in the codebase where an approval decision
changes the world. Approving publishes the underlying record; rejecting leaves
it inert. Either way an audit entry is written.

---

## Front end

No CSS framework, no build step, no JavaScript dependencies. Six stylesheets
loaded in a significant order (`tokens` → `base` → `layout` → `components` →
`pages` → `utilities`) and eleven small JavaScript modules.

**Theme.** Light by default with a dark toggle. An inline script in
`partials/_head.html` applies the saved theme before the first paint, so the
page never flashes the wrong colours. The choice is stored in `localStorage`
and on the user's profile.

**Responsive.** Mobile-first with three breakpoints: an off-canvas navigation
drawer below 768px, a permanent icon rail from 768px, the full sidebar from
1024px, and a capped content column from 1440px. The chat's conversation rail
is a second drawer of its own below 1024px. Data tables become stacked cards
below 768px using the `data-label` attribute on each cell.

**Progressive enhancement.** The employee configuration panel and the approve
and reject actions are ordinary `<form method="post">` elements, so they still
work with JavaScript disabled. The approval tabs and the conversation rail are
server-rendered links, not client-side panels. Only the chat composer itself
genuinely needs JavaScript, which is unavoidable for a chat interface.

**Untrusted output.** A model's reply is written into the page with
`textContent`, never `innerHTML`, both in `partials/_chat_message.html` and in
`static/js/chat.js`. `white-space: pre-wrap` preserves its paragraph breaks
without the text ever being parsed as markup.

---

## Suggested demonstration order

1. **RBAC first, because it frames everything else.** Sign in as `viewer`
   (`Viewer12345`). The chat composer is replaced by a read-only notice, the
   approval queue says "Awaiting a Manager", and there are no Add or Configure
   buttons anywhere. Then try a URL directly — `/agents/new/` — and show the
   403. Hiding the button and refusing the request are the same rule.
2. Sign in as `analyst` (`Analyst12345`). Now you can chat and submit for
   approval, but the approve controls are still absent.
3. Sign in as `manager` (`Manager12345`). Approve and reject appear, along with
   employee and MCP management, but the Configurations page still says only an
   Administrator may change provider settings.
4. Sign in as `admin` (`admin123`) for the rest of the walkthrough, and open the
   Users page to show the role matrix and the account controls.
5. **Landing page** and sign-up — mention that the workspace is shared, so a new
   account joins the existing one rather than getting a private copy.
6. **Dashboard** — the headline figures and the employee list.
7. **AI Employees** — the centrepiece. Pick a thread from the rail, type a
   message, and watch the reply arrive. Point out the source badge: "Template
   reply" when no model is assigned, "Live model" when one is. Open the
   configuration panel and change the system prompt, the model or the attached
   MCP tools without leaving the conversation. Start a new thread and show that
   it names itself after the opening question.
8. **Send for approval** — on any reply, press "Send for approval", choose
   "Outreach email", pick a prospect and submit. The button becomes a link into
   the queue and the sidebar badge increments.
9. **Approvals** — the item that just arrived. Approve it, then reject
   something and show that a blank reason is refused. Open the detail page for
   the audit trail, which records the conversation it came from.
   If mail credentials are set, approving an *outreach email* genuinely sends
   it: point out that the draft reached nobody until that click.
10. **Configurations** — the cascading provider-to-model dropdown, then press
   "Test connection" and show that a failure degrades to the curated catalogue.
11. **MCP Tools** — toggle a server, test Gmail (degraded, no credential) and
   Sequential Thinking (connected).
12. **Users** — the role matrix, then add a person, change their role, and
    show that the account they created can immediately do more or less.
13. **Admin** at `/admin/marketing/aiagent/` — the customised list, the three
    inlines and the collapsible fieldsets. Also `/admin/auth/group/`, where the
    four roles and their permissions are visible with no extra code.
14. **Responsive** — narrow the window to show both drawers, and toggle dark
    mode.
