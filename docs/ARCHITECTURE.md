# Architecture

How AI Workforce OS fits together, why it is shaped this way, and how to extend
it. The README says what it does; this says how.

## The layers

```
  person types a request
          |
          v
  +-------------------+     keyword scoring over each employee's vocabulary;
  |   orchestrator    |---- a model breaks a close tie when one is reachable
  +-------------------+     (marketing/orchestrator.py)
          |
          v
  +-------------------+     builds the system prompt, advertises the employee's
  |  agent runtime    |---- tools, runs the model/tool loop, blocks repeats,
  +-------------------+     falls back to running a tool when no model answers
          |                 (marketing/agent_runtime.py, llm_tools.py)
          v
  +-------------------+     the only way a tool is invoked; audits every call;
  |   tool registry   |---- turns a Proposal into a pending ProposedAction
  +-------------------+     (marketing/tools/)
          |
          |  immediate tools write company records and return
          |
          |  gated tools stop here  ==>  +------------------+
          |                              |  approval queue  |  edit / approve / reject
          |                              +------------------+  (marketing/approvals.py)
          |                                       |  approve
          v                                       v
  +-------------------+     one door to the outside; decides live or simulated;
  | integration layer |---- reports which ran in CallResult.demo
  +-------------------+     (marketing/integrations/)
          |
          v
  +-------------------+     live_<op> speaks to the service,
  |     connector     |---- demo_<op> produces a deterministic simulation
  +-------------------+     (marketing/integrations/<name>.py)
          |
          v
    external service
```

Two properties hold at every layer. Nothing external happens without a person,
because a gated tool has no code path that reaches a connector. Every call is
recorded, because `tools.run` is the only entry point and it writes the audit
entry before returning.

## The tool contract

A tool is a function decorated with `@tool` in `marketing/tools/`. The
decorator records its name, title, description, owning employee types, JSON
argument schema, the integration it reaches and whether it needs approval.

```python
@tool(name='hr.shortlist_candidate', title='Shortlist a candidate',
      description='Mark a candidate as shortlisted for their opening.',
      group='Screening', agent_types=('hr',),
      parameters={'type': 'object',
                  'properties': {'candidate_id': {'type': 'integer'},
                                 'reason': {'type': 'string'}},
                  'required': ['candidate_id']})
def shortlist_candidate(ctx, candidate_id, reason=''):
    candidate = Candidate.objects.filter(pk=candidate_id).first()
    if candidate is None:
        return ToolResult(ok=False, text='No candidate with that id. Call hr.list_candidates.')
    candidate.status = 'shortlisted'
    candidate.save(update_fields=['status'])
    return ToolResult(ok=True, text=f'Shortlisted {candidate.full_name}.',
                      data={'candidate_id': candidate.pk})
```

An immediate tool returns a `ToolResult`. Its `text` is what the language model
reads next, so it is written as a sentence with the ids in it.

A gated tool returns a `Proposal` instead, and registers a separate executor:

```python
@tool(name='hr.send_rejection_email', title='Send a rejection',
      description='Prepare a rejection email for approval.',
      group='Candidate Communication', agent_types=('hr',),
      integration='gmail', requires_approval=True, risk='high',
      parameters=...)
def send_rejection_email(ctx, candidate_id, subject='', body=''):
    candidate = ...
    return Proposal(title=f'Rejection to {candidate.full_name}',
                    payload={'to': candidate.email, 'subject': subject, 'body': body},
                    editable_fields=[editable('subject', 'Subject'),
                                     editable('body', 'Body', 'longtext')],
                    subject_label='marketing.candidate', subject_id=candidate.pk)

@executor('hr.send_rejection_email')
def execute_rejection(action):
    result = integrations.call('gmail', 'send_email', **action.payload)
    return ToolResult(ok=result.ok, demo=result.demo, text=result.summary)
```

The proposing function and the executing function are different functions on
purpose. A single function with an `approved=True` flag would be one forgotten
argument away from sending; two functions means the proposer literally has no
way to reach a connector. `tools.run` turns the `Proposal` into a
`ProposedAction` row; `approvals.approve` is the only caller of an executor.

`tools.run` also validates required arguments, translates the model's
tool-name spelling (`hr__shortlist_candidate` for `hr.shortlist_candidate`),
refuses a tool the employee does not own, redacts credential-shaped arguments
before writing the audit entry, and appends a step to the current task.

## The connector contract

A connector subclasses `Connector` in `marketing/integrations/base.py` and
registers itself with `@register`. It declares `config_fields` -- the settings
it needs, which drive the Integrations form, the configuration tool's
validation and the "what is missing" list -- and implements each operation
twice:

```python
@register
class SlackConnector(Connector):
    key = 'slack'
    name = 'Slack'
    category = 'communication'
    config_fields = (
        ConfigField('bot_token', 'Bot user OAuth token', secret=True, required=True),
        ConfigField('default_channel', 'Default channel', default='#general'),
    )
    operations = ('post_message',)

    def live_post_message(self, channel='', text='', **kwargs):
        status, data = self.request_json('https://slack.com/api/chat.postMessage',
                                         method='POST',
                                         headers={'Authorization': f'Bearer {self.setting("bot_token")}'},
                                         payload={'channel': channel or self.setting('default_channel'),
                                                  'text': text})
        if not data.get('ok'):
            return self.failure(f'Slack said {data.get("error")}.')
        return self.ok(f'Posted to {channel}.', {'ts': data.get('ts')})

    def demo_post_message(self, channel='', text='', **kwargs):
        return self.simulated(f'Would post to {channel or "#general"}. Nothing was sent.',
                              {'ts': 'demo-1', 'simulated': True})
```

`Connector.call` chooses the path: disabled is an error; `demo` mode simulates;
a missing credential simulates in `auto` and fails in `live`; the test suite
forces simulation for everyone. The result's `demo` flag is carried by the tool
result, the execution record's status, the audit entry's status and the
interface. Every setting is read with `self.setting(key)` from the Integration
row; a connector never reads an environment variable.

## The turn loop

`agent_runtime.run_turn` records the user's message, builds a system prompt
from the employee's instructions plus a compact live context (date and
timezone, company profile, its integrations and their modes, its pending
approvals, its recalled memories), and loops: call the model with the
employee's tool specifications, run each requested tool through `tools.run`,
feed the results back, repeat until the model answers in prose.

Guards: a call identical to an earlier one in the same turn is answered with
the earlier result and an instruction not to retry, so a model cannot file the
same issue four times; rounds are capped by the `max_tool_calls_per_turn`
setting and total calls by a hard ceiling, after which the model is asked once
more with no tools so it must write. Unparseable arguments are reported back
rather than dropped.

When no model is reachable the turn still does work. The fallback scores the
request against the employee's tools, keeps those whose required arguments can
be filled from the text, runs the best one and answers from its real result,
labelled as produced without a model. With no match it returns the capability
list with an example per group, which is more useful than an apology.

## Retrieval

`marketing/knowledge.py` splits each document into overlapping passages of
about 180 words carrying their nearest heading, and ranks passages with BM25
plus small boosts for a term in the heading, a term in the title, an exact
phrase match and recency. Scores are normalised so the best hit is 1.0.

That normalisation is why `term_coverage` exists. The best passage for a
question nobody documented also scores 1.0, so the answering tools check the
document frequency of each query term and refuse when a distinctive term
appears in no document at all. `detect_conflicts` compares numbers and
durations near shared key terms across hits and reports both sides without
choosing. Every answer carries citations that point at real `DocumentChunk`
rows, which is what makes a claim in a published post traceable to a document.

## The data model

Sixty models in six modules, all in the `marketing` app.

- **Platform** (`models_platform.py`): `Integration`, `AgentCapability`,
  `AgentTask`, `AgentDelegation`, `OrchestratorDecision`, `ProposedAction`,
  `ActionAuditTrail`, `OutboundMessage`, `CalendarEvent`, `ExternalIssue`,
  `AgentMemory`, `AuditEvent`, `SystemSetting`.
- **HR** (`models_hr.py`): `JobOpening`, `Candidate`, `CandidateEvaluation`,
  `Interview`, `Employee`, `OnboardingTask`, `PerformanceReview`,
  `HRAnnouncement`.
- **Engineering** (`models_eng.py`): `Project`, `Sprint`, `WorkItem` (one
  self-referencing tree for epics, stories, tasks and subtasks), `WorkItemComment`,
  `SprintReport`, `CodeArtifact` (the Developer's safety boundary), `CodeReview`.
- **Support** (`models_support.py`): `Customer`, `SupportTicket`,
  `TicketMessage`, `TicketTag`, `SupportReport`.
- **Content** (`models_content.py`): `CampaignBrief`, `ContentPiece`,
  `ContentCalendarEntry`, `MarketingEmail`, `AudienceSegment`, linking to the
  original `MarketingCampaign`.
- **Knowledge** (`models_knowledge.py`): `KnowledgeSource`, `KnowledgeDocument`,
  `DocumentChunk`, `ResearchReport`, `ResearchCitation`.

`ProposedAction` points at its subject with a soft `subject_label` and
`subject_id` rather than a `GenericForeignKey`. An action can outlive its
subject, and a label that still reads correctly in the audit trail is better
than a cascade that erases the record of what was approved.

## Provisioning

`marketing/provisioning.py` is idempotent end to end and runs on start-up and on
login: providers, the six employees from `workforce.py`, an `Integration` row
for every registered connector, default settings, the capability catalogue
generated from the tool registry, and the seeded knowledge base.
`demo_data.py` adds the demonstration company, keyed on natural values and
tracked in a manifest so `clear_demo_data` removes exactly what it wrote.

## Extending it

**A new tool.** Add a decorated function to the relevant module under
`marketing/tools/`. It appears in the employee's profile, in the model's tool
list and in the fallback matcher with no other change. If it reaches outside,
return a `Proposal` and register an executor.

**A new connector.** Add a module under `marketing/integrations/` with a
registered `Connector` subclass and import it at the bottom of
`integrations/__init__.py`. An `Integration` row is created on the next start
and the card appears on the Integrations page with a form built from its
`config_fields`.

**A new setting.** Add an entry to `DEFAULT_SETTINGS` in
`marketing/tools/platform.py`. Provisioning writes it; the Company settings
page renders it; `platform.get_setting` reads it.

**A new employee.** Add a blueprint to `workforce.AGENT_BLUEPRINT`, a choice to
`AIAgent.AGENT_TYPE_CHOICES`, and give some tools its `agent_type`.

## Seams

A background worker would attach at `approvals.reminders` and at the
`scheduled_for` fields on content and email; nothing currently fires at a
time. A real OAuth flow would replace the pasted-token fields on the Google
connectors with a redirect that writes the same `secrets` keys. Embedding-based
retrieval would replace the scoring inside `knowledge.search` while keeping
`SearchHit`, so every caller stays unchanged.
