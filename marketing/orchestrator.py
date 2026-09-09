"""The orchestrator: which employee should take this request, and why.

THE PROBLEM THIS SOLVES
-----------------------
Six employees, one input box. A person typing "the checkout page throws a 500
after the last deploy" should not first have to decide whether that is a
Developer matter or an Engineering Delivery matter. So the request is routed,
and because routing is a decision the platform makes on somebody's behalf, it
is recorded and explainable: every call writes an ``OrchestratorDecision`` row
holding the request, the winner, the runners-up, the reasoning and the method.

RULES FIRST, MODEL ONLY TO BREAK A TIE
--------------------------------------
Keyword scoring against ``workforce.ROUTING_KEYWORDS`` decides almost every
request, and it has three properties a language model does not: it costs
nothing, it takes no time, and it gives the same answer twice. It is also
inspectable, which is why the matched terms travel in the decision row.

A language model is consulted in one situation only -- the top two candidates
are within a small margin of each other, and a model is actually reachable. It
is asked a single short question with a metadata-length timeout rather than a
generation-length one, because a routing decision that takes twenty seconds has
already failed at its job even if it answers correctly.

WHEN NOTHING MATCHES
--------------------
An unclassifiable request goes to Knowledge & Research, and the decision says
so plainly. That is not a shrug: a question that matches none of the six
vocabularies is most often a question about the company itself, which is
precisely the Research employee's job, and it is the one employee whose honest
answer to "the knowledge base does not cover this" is useful on its own.

MORE THAN ONE EMPLOYEE
----------------------
"Launch the new release" is not one employee's work. ``suggest_workflow``
reports the ordered sequence -- research the facts, plan the work, then
communicate it -- so the console can offer a real multi-employee sequence
instead of pretending the top scorer covers the whole request.
"""

import re

from django.utils import timezone

from . import agent_runtime, audit, llm_tools
from .llm_client import _timeout as _metadata_timeout
from .models import AIAgent
from .workforce import ROUTING_KEYWORDS, blueprint_for

# A phrase is much stronger evidence than a word: "job description" names the
# work, whereas "description" appears in every second sentence.
PHRASE_WEIGHT = 3.0
WORD_WEIGHT = 1.5

# The subject of a request is nearly always in its first sentence; a term that
# turns up later is more often background than intent.
FIRST_SENTENCE_BONUS = 1.5

# A tool whose own name or title matches a term is corroborating evidence, so
# it is worth a nudge and never more. Capped, because an employee with forty
# tools would otherwise win every request by sheer surface area.
TOOL_MATCH_WEIGHT = 0.5
TOOL_MATCH_CAP = 1.5

# Below this the two leaders are effectively tied, and a language model is
# better at "is this about the code or about the schedule" than a word count.
TIE_MARGIN = 1.5

# Where an unclassifiable request goes, and why. See the module docstring.
DEFAULT_AGENT_TYPE = 'research'

# For suggest_workflow: research establishes facts, the doers act on them, and
# the communicating employees speak once there is something to say. A sequence
# in any other order produces a post about a feature nobody has planned yet.
WORKFLOW_ORDER = ('research', 'engineering_manager', 'developer', 'hr',
                  'support', 'marketing')

WORKFLOW_ROLE = {
    'research': 'establish the facts and the sources first',
    'engineering_manager': 'plan and sequence the work',
    'developer': 'write and review the code',
    'hr': 'handle the people and recruitment side',
    'support': 'handle the customer contact',
    'marketing': 'write the external communication last, once the facts are settled',
}

WORKFLOW_THRESHOLD = 1.5


# ===========================================================================
# Scoring
# ===========================================================================

def _first_sentence(text):
    flat = ' '.join((text or '').split())
    match = re.search(r'[.!?]\s', flat)
    return flat[:match.start() + 1] if match else flat


def _score_agent_types(request_text):
    """Every employee's score for one request, with the terms that earned it.

    Returns ``{agent_type: {'score': float, 'matched': [term, ...]}}``.
    """
    lowered = ' '.join((request_text or '').lower().split())
    opening = _first_sentence(lowered).lower()
    scores = {}

    for agent_type, keywords in ROUTING_KEYWORDS.items():
        score = 0.0
        matched = []
        for keyword in keywords:
            if ' ' in keyword:
                hit = keyword in lowered
                weight = PHRASE_WEIGHT
            else:
                hit = re.search(rf'\b{re.escape(keyword)}\b', lowered) is not None
                weight = WORD_WEIGHT
            if not hit:
                continue
            if keyword in opening:
                weight *= FIRST_SENTENCE_BONUS
            score += weight
            matched.append(keyword)

        bonus = _tool_bonus(agent_type, lowered)
        scores[agent_type] = {'score': round(score + bonus, 3), 'matched': matched,
                              'tool_bonus': round(bonus, 3)}

    return scores


def _tool_bonus(agent_type, lowered):
    """A small boost when this employee owns a tool the request names.

    Reads the live registry, so an employee that gains a capability starts
    attracting the requests that capability answers with no routing edit. An
    empty registry contributes nothing rather than failing.
    """
    bonus = 0.0
    for entry in agent_runtime.available_tools(agent_type):
        title = (entry.title or '').lower()
        name = (entry.name or '').split('.')[-1].replace('_', ' ').lower()
        if (title and title in lowered) or (name and name in lowered):
            bonus += TOOL_MATCH_WEIGHT
            if bonus >= TOOL_MATCH_CAP:
                return TOOL_MATCH_CAP
    return bonus


def _confidence(top, second, total):
    """A score turned into a number between 0 and 1.

    Two things make a routing decision confident and they are not the same:
    how much evidence there is at all (a single weak word match should never
    read as certain), and how much of that evidence points at one employee
    rather than being spread across several. Both are weighted equally.
    """
    if top <= 0:
        return 0.2
    strength = min(1.0, top / 6.0)
    share = top / total if total > 0 else 1.0
    if second > 0:
        share = min(share, top / (top + second))
    return round(max(0.2, min(0.95, 0.5 * strength + 0.5 * share)), 3)


def _agent_for(agent_type):
    agent = AIAgent.objects.filter(agent_type=agent_type).first()
    if agent is not None:
        return agent
    # A roster that has not been provisioned yet, or an employee somebody
    # deleted. Any employee is better than dropping the request.
    return AIAgent.objects.filter(agent_type=DEFAULT_AGENT_TYPE).first() \
        or AIAgent.objects.first()


def _label(agent_type):
    row = blueprint_for(agent_type)
    return row['name'] if row else agent_type.replace('_', ' ').title()


# ===========================================================================
# Routing
# ===========================================================================

def route(request_text, *, user=None, prefer_agent_type=''):
    """Choose the employee for one request and record the decision.

    Returns ``{'agent', 'agent_type', 'confidence', 'reasoning', 'method',
    'candidates', 'decision'}``. Never raises: an empty roster returns
    ``agent=None`` with the reason in ``reasoning``.
    """
    text = (request_text or '').strip()
    scores = _score_agent_types(text)

    ordered = sorted(scores.items(), key=lambda row: (-row[1]['score'], row[0]))
    candidates = [{'agent_type': agent_type, 'name': _label(agent_type),
                   'score': entry['score'], 'matched': entry['matched']}
                  for agent_type, entry in ordered]

    top_score = ordered[0][1]['score'] if ordered else 0.0
    second_score = ordered[1][1]['score'] if len(ordered) > 1 else 0.0
    total = sum(entry['score'] for _key, entry in ordered) or 0.0

    method = 'rules'
    chosen_type = ordered[0][0] if ordered else DEFAULT_AGENT_TYPE
    reasoning = ''

    if prefer_agent_type and prefer_agent_type in ROUTING_KEYWORDS:
        chosen_type = prefer_agent_type
        method = 'requested'
        reasoning = (f'{_label(chosen_type)} was asked for directly, so no routing '
                     f'was needed.')
        confidence = 1.0

    elif top_score <= 0:
        chosen_type = DEFAULT_AGENT_TYPE
        confidence = 0.2
        reasoning = ('Nothing in the request matched any employee\'s vocabulary. An '
                     'unclassifiable request is most often a question about the '
                     'company itself, so it goes to '
                     f'{_label(DEFAULT_AGENT_TYPE)}, which can search the knowledge '
                     'base and say plainly if the answer is not there.')

    else:
        confidence = _confidence(top_score, second_score, total)
        tied = (len(ordered) > 1
                and second_score > 0
                and (top_score - second_score) < TIE_MARGIN)

        if tied:
            picked, why = _ask_model_to_choose(text, ordered[:3])
            if picked:
                chosen_type = picked
                method = 'llm'
                reasoning = why
            else:
                reasoning = _rules_reasoning(chosen_type, scores[chosen_type],
                                             ordered, tied=True, note=why)
        else:
            reasoning = _rules_reasoning(chosen_type, scores[chosen_type], ordered,
                                         tied=False)

    agent = _agent_for(chosen_type)
    if agent is None:
        reasoning = (f'{reasoning} No employee of that kind exists in this workspace '
                     f'yet, so nothing could be assigned.').strip()
    elif agent.agent_type != chosen_type:
        # A roster that has not been fully provisioned. Saying so matters: the
        # reader would otherwise see a decision naming one employee and an
        # answer written by another, with nothing to explain the difference.
        reasoning = (f'{reasoning} There is no {_label(chosen_type)} employee in this '
                     f'workspace, so {agent.name} took it instead.').strip()

    decision = _record(text, agent, reasoning, confidence, method, candidates, user)

    return {'agent': agent, 'agent_type': chosen_type, 'confidence': confidence,
            'reasoning': reasoning, 'method': method, 'candidates': candidates,
            'decision': decision}


def _rules_reasoning(chosen_type, entry, ordered, *, tied, note=''):
    matched = entry.get('matched') or []
    terms = ', '.join(f'"{term}"' for term in matched[:5]) or 'no specific terms'
    parts = [f'{_label(chosen_type)} scored highest ({entry["score"]}) on {terms}.']
    if entry.get('tool_bonus'):
        parts.append('It also owns a tool the request names by name.')
    if len(ordered) > 1 and ordered[1][1]['score'] > 0:
        parts.append(f'The nearest alternative was {_label(ordered[1][0])} '
                     f'({ordered[1][1]["score"]}).')
    if tied:
        parts.append('The two were close enough to be a tie.')
    if note:
        parts.append(note)
    return ' '.join(parts)


def _record(text, agent, reasoning, confidence, method, candidates, user):
    """Write the OrchestratorDecision row. Returns it, or None if it failed."""
    try:
        from .models_platform import OrchestratorDecision
        decision = OrchestratorDecision.objects.create(
            request_text=text,
            chosen_agent=agent,
            reasoning=reasoning[:4000],
            confidence=confidence,
            method=method,
            candidates=candidates,
            requested_by=user if getattr(user, 'pk', None) else None,
        )
    except Exception:  # noqa: BLE001 -- routing must survive an unmigrated database
        return None

    audit.log('orchestrator.routed', category='agent', status='ok', actor=user,
              agent=agent, object_label=agent.name if agent else 'nobody',
              message=f'{method}: {reasoning[:300]}',
              detail={'confidence': float(confidence),
                      'candidates': candidates[:3]})
    return decision


# ===========================================================================
# The tie-break
# ===========================================================================

def _available_model():
    """A provider and model to ask a cheap question with, or (None, None).

    Reuses whatever an employee is already configured with rather than
    introducing a second place where a model is chosen, so a workspace with one
    working key routes with that key and a workspace with none routes by rules.
    """
    for agent in AIAgent.objects.select_related('llm_model__provider').all():
        if agent.has_live_llm:
            return agent.llm_model.provider, agent.llm_model.model_id
    return None, None


def _ask_model_to_choose(text, top_entries):
    """Ask a model to break a tie between two or three candidates.

    Returns ``(agent_type, reasoning)``, or ``('', note)`` when no model is
    reachable or the answer was not one of the candidates offered. Constrained
    hard: the model chooses from a list it is given, the reply budget is a few
    tokens, and the timeout is the metadata budget rather than the generation
    one, so the worst case is a short wait and a rules decision.
    """
    provider, model_id = _available_model()
    if provider is None:
        return '', 'No language model was reachable, so the keyword score decided.'

    options = []
    for agent_type, _entry in top_entries:
        row = blueprint_for(agent_type)
        summary = row['persona_description'] if row else ''
        options.append(f'{agent_type}: {summary[:220]}')

    allowed = [agent_type for agent_type, _entry in top_entries]

    result = llm_tools.chat_with_tools(
        provider, model_id,
        [
            {'role': 'system',
             'content': ('You route one request to one AI employee. Reply with '
                         'exactly one of the given identifiers and nothing else.')},
            {'role': 'user',
             'content': ('Candidates:\n' + '\n'.join(options)
                         + f'\n\nRequest: {text[:600]}\n\nIdentifier:')},
        ],
        tools=None, temperature=0.0, max_tokens=24,
        timeout=_metadata_timeout())

    if not result['ok']:
        return '', (f'A language model was asked to break the tie but did not '
                    f'answer ({result["message"]}), so the keyword score decided.')

    answer = (result['text'] or '').strip().lower()
    for agent_type in allowed:
        if agent_type in answer:
            names = ' and '.join(_label(option) for option in allowed)
            return agent_type, (f'{names} scored within a point of each other, so '
                                f'{model_id} was asked to choose and picked '
                                f'{_label(agent_type)}.')

    return '', (f'A language model was asked to break the tie but answered '
                f'"{answer[:40]}", which is not one of the candidates, so the '
                f'keyword score decided.')


# ===========================================================================
# Dispatch
# ===========================================================================

def dispatch(request_text, *, user=None, conversation=None, prefer_agent_type=''):
    """Route the request, then actually run it on the chosen employee.

    Returns ``{'ok', 'routing', 'result', 'agent', 'explanation'}`` so an
    interface can say both halves of what happened: "routed to Customer Support
    because ..., and then it did ...". Nothing else in the platform joins those
    two facts together.

    A conversation is used only when it belongs to the chosen employee. Running
    one employee's turn inside another employee's thread would leave a chat
    history that reads as if the wrong employee said it, so the headless path is
    used instead and the thread is left alone.
    """
    routing = route(request_text, user=user, prefer_agent_type=prefer_agent_type)
    agent = routing['agent']

    if agent is None:
        return {'ok': False, 'routing': routing, 'result': None, 'agent': None,
                'explanation': routing['reasoning']}

    if conversation is not None and conversation.agent_id == agent.pk:
        result = agent_runtime.run_turn(conversation, request_text, user=user)
    else:
        result = agent_runtime.run_agent_task(agent, request_text, user=user)

    decision = routing.get('decision')
    if decision is not None and result.get('task') is not None:
        try:
            decision.task = result['task']
            decision.save(update_fields=['task'])
        except Exception:  # noqa: BLE001 -- the link is a convenience, not the record
            pass

    return {'ok': bool(result.get('ok')), 'routing': routing, 'result': result,
            'agent': agent, 'explanation': route_explanation(decision) or
            routing['reasoning']}


# ===========================================================================
# Employee to employee
# ===========================================================================

def collaborate(from_agent, to_agent_type, request, *, task=None, user=None):
    """One employee asking another for help, recorded as an AgentDelegation.

    This is the path that keeps the Marketing employee from inventing a product
    fact: it asks Research, Research searches and answers with citations, and
    the exchange is kept so the provenance of a claim in a published post can
    be traced back to the document it came from.

    Returns the AgentDelegation row in every case, including failure, because
    the record of a question that could not be answered is worth as much as the
    record of one that could.
    """
    from .models_platform import AgentDelegation

    target = AIAgent.objects.filter(agent_type=to_agent_type).first()

    if target is None:
        # ``to_agent`` cannot be null, so an unanswerable request is recorded
        # against the asker with the reason in the response. A row that says
        # "this was asked and nobody could answer it" is worth keeping; a
        # silently dropped question is not.
        delegation = AgentDelegation.objects.create(
            from_agent=from_agent, to_agent=from_agent, task=task,
            request=(request or '')[:4000],
            response=(f'No {_label(to_agent_type)} employee exists in this '
                      f'workspace, so the question could not be asked.'),
            status='failed', answered_at=timezone.now())
        return delegation

    delegation = AgentDelegation.objects.create(
        from_agent=from_agent, to_agent=target, task=task,
        request=(request or '')[:4000], status='pending')

    result = agent_runtime.run_agent_task(
        target, request, user=user, parent_task=task)

    delegation.response = result.get('reply', '')
    delegation.sources = _sources_from(result)
    delegation.tokens_used = result.get('tokens', 0) or 0
    delegation.status = 'answered' if result.get('reply') else 'failed'
    delegation.answered_at = timezone.now()
    delegation.save(update_fields=['response', 'sources', 'tokens_used', 'status',
                                   'answered_at'])

    audit.log('agent.delegated', category='agent',
              status='ok' if delegation.status == 'answered' else 'failed',
              actor=user, agent=from_agent, task=task,
              object_label=f'{from_agent.name} -> {target.name}',
              message=(result.get('reply') or 'No answer')[:300],
              detail={'to': to_agent_type, 'tools': len(result.get('tool_calls', []))})

    return delegation


def _sources_from(result):
    """Whatever the answering employee cited, for the delegation record.

    Tool results carry their own provenance, so the sources are collected from
    the calls that ran rather than parsed back out of the prose. A claim whose
    source cannot be named is better recorded as unsourced than given a source
    invented from the sentence around it.
    """
    sources = []
    for record in result.get('tool_calls') or []:
        if not record.get('ok') or record.get('repeat'):
            continue
        sources.append({'tool': record.get('name', ''),
                        'summary': record.get('result', '')[:300]})
    for action_id in result.get('pending_actions') or []:
        sources.append({'proposed_action': action_id})
    return sources[:12]


# ===========================================================================
# Work that needs more than one employee
# ===========================================================================

def suggest_workflow(request_text):
    """The ordered sequence of employees a broad request actually needs.

    Returns ``[]`` for a request one employee covers, which is the common case
    and must not be dressed up as a workflow. A request that genuinely spans
    two or more vocabularies -- "launch the new release" reaches Research,
    Engineering Delivery and Marketing -- returns them in working order, so the
    console can offer the sequence rather than quietly handing the whole thing
    to the top scorer.
    """
    scores = _score_agent_types(request_text)
    if not scores:
        return []

    top = max(entry['score'] for entry in scores.values()) or 0.0
    if top <= 0:
        return []

    qualifying = [agent_type for agent_type, entry in scores.items()
                  if entry['score'] >= WORKFLOW_THRESHOLD
                  and entry['score'] >= top * 0.35]

    if len(qualifying) < 2:
        return []

    sequence = []
    for agent_type in WORKFLOW_ORDER:
        if agent_type not in qualifying:
            continue
        entry = scores[agent_type]
        terms = ', '.join(f'"{term}"' for term in (entry['matched'] or [])[:3])
        sequence.append({
            'agent_type': agent_type,
            'name': _label(agent_type),
            'step': len(sequence) + 1,
            'why': (f'{WORKFLOW_ROLE.get(agent_type, "contribute its part")}; '
                    f'matched {terms or "the general subject"}'),
        })

    return sequence


# ===========================================================================
# Saying it in words
# ===========================================================================

def route_explanation(decision):
    """Why this employee was chosen, as a paragraph for the interface.

    A routing decision the reader cannot interrogate is indistinguishable from
    a guess, so this reads the stored row rather than recomputing anything: what
    it says is what was actually recorded at the time.
    """
    if decision is None:
        return ''

    name = decision.chosen_agent.name if decision.chosen_agent else 'no employee'
    confidence = float(decision.confidence or 0)
    method = {
        'rules': 'by matching the request against each employee\'s subject vocabulary',
        'llm': 'by keyword scoring, with a language model asked to break a close tie',
        'requested': 'because that employee was asked for directly',
    }.get(decision.method, f'by the {decision.method} method')

    parts = [f'This request went to {name} {method}, with a confidence of '
             f'{confidence:.0%}.']

    if decision.reasoning:
        parts.append(decision.reasoning)

    runners = [row for row in (decision.candidates or [])[1:4]
               if (row.get('score') or 0) > 0]
    if runners:
        listed = ', '.join(f'{row.get("name") or row.get("agent_type")} '
                           f'({row.get("score")})' for row in runners)
        parts.append(f'The employees considered next were {listed}.')

    if confidence < 0.4:
        parts.append('The confidence is low, so it is worth checking that this is '
                     'the employee you wanted before acting on the answer.')

    return ' '.join(parts)
