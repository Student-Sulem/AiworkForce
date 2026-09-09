"""The Customer Support employee's tools: intake, triage, replies, escalation.

WHAT THIS MODULE IS FOR
-----------------------
The Customer Support employee reads what customers send, decides what it is,
decides how badly it hurts, grounds an answer in company policy, and hands
anything it should not touch to a person. Every one of those steps is a tool
here, and every tool returns text a language model can act on: a reference, an
id, a rule that fired, a document it quoted.

WHY THE TRIAGE IS PLAIN PYTHON AND NOT A LANGUAGE MODEL
-------------------------------------------------------
Categorisation, prioritisation, sentiment, duplicate detection and the reports
are all deterministic keyword and set arithmetic. That is not a limitation, it
is the point. A priority is a promise about whose ticket waits: the customer
below it in the queue is entitled to know why. A rule set answers that -- "urgent
because the message contains 'charged me twice' and 'our whole team cannot
invoice', which is the double-charge rule" -- and a model's judgement does not.
The same reasoning applies to a duplicate: closing somebody's ticket as a
duplicate on an unexplainable similarity score is worse than leaving it open.

So the rules live in the tables below, the phrases that fired are stored on the
ticket in ``urgency_signals``, and every triage tool reports its evidence.

PRIORITY COMES FROM IMPACT, NOT FROM TONE
-----------------------------------------
The single most common failure in automated support triage is answering the
loudest message first. A calm note saying "our export has silently dropped
three months of records" outranks a message in capitals about a slow reply, and
the rules below are ordered to make that happen. Profanity and shouting are
detected, reported, and deliberately given no weight at all.

THE HARD STOP
-------------
``SupportTicket.needs_human`` marks a ticket that must never receive an
AI-drafted reply: a data breach, a legal threat, a safety issue, a vulnerable
customer, a media enquiry, anything involving self-harm. ``flag_for_human``
sets it, ``draft_customer_reply`` refuses to write while it is set, and
``send_customer_reply`` refuses even to place a proposal in the approval queue.
A refusal at the proposal stage matters more than it looks: an approval queue
invites a tired reviewer to press Approve, so the safest thing to do with that
class of message is to never put a plausible reply in front of them.

SENDING IS ALWAYS SOMEBODY ELSE'S DECISION
------------------------------------------
Everything that reaches a customer or an external system -- email, Slack, Jira,
a calendar invitation -- returns a ``Proposal`` and is carried out by the
matching ``@executor`` only when a person approves it. Drafting and sending are
therefore separate tools on purpose: the draft is a record on the ticket, and
the send is a governed action.
"""

import re
import statistics
from datetime import timedelta

from django.utils import timezone

from .. import integrations
from .base import Proposal, ToolResult, editable, executor, tool

# ===========================================================================
# THE RULE TABLES
# ===========================================================================

# Categorisation. Ordered from most specific intent to least, which is also the
# tie-break order: a message that scores equally as a refund and as a complaint
# is a refund, because 'refund' says what to do and 'complaint' does not.
CATEGORY_RULES = (
    ('refund', (
        'refund', 'refunded', 'money back', 'my money back', 'reimburse',
        'reimbursement', 'chargeback', 'charge back', 'credit note',
        'return my money', 'cancel and refund')),
    ('feature_request', (
        'feature request', 'would be great if', 'it would help if', 'please add',
        'any plans to', 'suggestion', 'enhancement', 'on the roadmap',
        'feature we need', 'wish list')),
    ('billing', (
        'invoice', 'invoiced', 'billing', 'billed', 'bill', 'charge', 'charged',
        'subscription', 'payment', 'card declined', 'overcharg', 'double charge',
        'direct debit', 'receipt', 'price increase', 'renewal', 'vat', 'gst',
        'purchase order', 'quote')),
    ('delivery', (
        'delivery', 'delivered', 'not delivered', 'shipping', 'shipment',
        'courier', 'tracking number', 'tracking', 'dispatch', 'dispatched',
        'parcel', 'package', 'post office', 'in transit', 'lost in the post')),
    ('order', (
        'my order', 'order number', 'order status', 'placed an order', 'checkout',
        'shopping cart', 'my purchase', 'order confirmation', 'order')),
    ('technical', (
        'error', 'error message', 'bug', 'crash', 'crashed', 'crashing',
        'not working', 'does not work', "doesn't work", 'broken', 'timeout',
        'timed out', 'exception', 'stack trace', 'http 500', 'http 502',
        'http 503', 'api', 'integration', 'sync', 'syncing', 'export', 'import',
        'glitch', 'frozen', 'freezes', 'blank screen', 'will not load')),
    ('account', (
        'my account', 'account', 'password', 'reset my password', 'log in',
        'login', 'cannot log in', 'sign in', 'username', 'two-factor', '2fa',
        'locked out', 'permission', 'permissions', 'user seat', 'add a user',
        'deactivate', 'close my account', 'delete my account')),
    ('complaint', (
        'complaint', 'formal complaint', 'unacceptable', 'disappointed',
        'terrible service', 'appalling', 'the worst', 'poor service', 'let down',
        'no one has replied', 'nobody has replied', 'third time i have')),
)

PRIORITY_RANK = {'low': 0, 'normal': 1, 'high': 2, 'urgent': 3}
PRIORITY_ORDER = ('low', 'normal', 'high', 'urgent')

# Prioritisation, keyed on impact. Ordered by severity of consequence rather
# than by how the message sounds. Each entry is
# (rule name, priority it sets, phrases, why that priority is defensible).
PRIORITY_RULES = (
    ('data-loss', 'urgent', (
        'data loss', 'lost all my data', 'lost my data', 'lost our data',
        'data is gone', 'files are gone', 'records are gone', 'deleted everything',
        'wiped', 'nothing was saved', 'corrupted', 'cannot recover',
        'missing three months', 'history has disappeared'),
     'customer data may have been lost, which cannot be undone by replying later'),
    ('security', 'urgent', (
        'data breach', 'breach of data', 'hacked', 'leaked', 'leak of',
        'unauthorised access', 'unauthorized access', 'someone else logged in',
        'someone has my', 'details were exposed', 'exposed our data', 'phishing',
        'compromised', 'security issue', 'security vulnerability'),
     'a security or privacy exposure is alleged and the clock is legal, not commercial'),
    ('legal', 'urgent', (
        'my lawyer', 'lawyer', 'solicitor', 'legal action', 'take legal',
        'take this further legally', 'sue', 'suing', 'court', 'tribunal',
        'ombudsman', 'fair trading', 'consumer law', 'accc', 'gdpr',
        'privacy act', 'the regulator', 'breach of contract', 'small claims'),
     'a legal or regulatory route has been named'),
    ('welfare', 'urgent', (
        'self-harm', 'self harm', 'suicide', 'suicidal', 'kill myself',
        'harm myself', 'end my life', 'mental health crisis', 'in hospital',
        'palliative', 'dementia', 'my carer', 'disability support',
        'my elderly mother', 'my elderly father'),
     'a welfare or vulnerable-person signal is present'),
    ('safety', 'urgent', (
        'injured', 'injury', 'burnt', 'burned me', 'electric shock', 'caught fire',
        'smoke', 'unsafe', 'dangerous', 'poisoned', 'allergic reaction',
        'child was hurt'),
     'physical safety is involved'),
    ('service-down', 'urgent', (
        'service is down', 'site is down', 'system is down', 'completely down',
        'total outage', 'outage', 'nothing loads', 'cannot access anything',
        'everything is broken', 'down since', 'all users are affected'),
     'the service appears unavailable, so every minute multiplies the impact'),
    ('double-charge', 'high', (
        'charged twice', 'charged me twice', 'double charged', 'billed twice',
        'duplicate charge', 'duplicate payment', 'overcharged', 'taken twice',
        'charged the wrong amount', 'unauthorised payment', 'taken money i did not'),
     'money has left the customer twice and is sitting in the wrong account'),
    ('blocked', 'high', (
        'cannot work', 'can not work', 'unable to work', 'blocked from',
        'stopping us from', 'cannot do my job', 'whole team cannot',
        'team cannot', 'production is', 'business is stopped', 'cannot invoice',
        'cannot serve our customers', 'cannot trade', 'at a standstill'),
     'the customer is blocked from doing their own work'),
    ('deadline', 'high', (
        'deadline', 'by tomorrow', 'by monday', 'by tuesday', 'by wednesday',
        'by thursday', 'by friday', 'end of day', 'by close of business',
        'due today', 'before the end of the month', 'audit on', 'launch on',
        'going live', 'go live', 'court date', 'settlement date'),
     'the customer named a fixed date that a slow reply would miss'),
    ('money-at-risk', 'high', (
        'payment failed', 'payments are failing', 'cannot take payments',
        'cannot pay', 'invoice is overdue', 'losing money', 'losing revenue',
        'customers cannot pay us'),
     'revenue is actively at risk'),
)

# Tone markers. Detected so they can be reported and then explicitly ignored.
# They never raise a priority: doing so would mean the loudest customer is
# served first, which is exactly the behaviour this module refuses.
TONE_MARKERS = (
    'ridiculous', 'outrageous', 'disgusted', 'furious', 'fed up', 'sick of',
    'joke', 'a joke', 'pathetic', 'useless', 'rubbish', 'nonsense', 'damn',
    'bloody', 'crap', 'wtf', 'never again', 'shocking',
)

# The hard stop. A hit here means no AI-drafted reply, at all.
HUMAN_RULES = (
    ('data breach', (
        'data breach', 'breach of data', 'hacked', 'leaked', 'unauthorised access',
        'unauthorized access', 'details were exposed', 'exposed our data',
        'compromised', 'someone else logged in')),
    ('legal threat', (
        'my lawyer', 'lawyer', 'solicitor', 'legal action', 'take legal', 'sue',
        'suing', 'court', 'tribunal', 'ombudsman', 'defamation', 'accc',
        'fair trading', 'the regulator', 'breach of contract')),
    ('safety issue', (
        'injured', 'injury', 'caught fire', 'electric shock', 'burnt', 'burned me',
        'unsafe', 'dangerous', 'poisoned', 'allergic reaction', 'child was hurt')),
    ('vulnerable customer or self-harm', (
        'self-harm', 'self harm', 'suicide', 'suicidal', 'kill myself',
        'harm myself', 'end my life', 'mental health crisis', 'dementia',
        'palliative', 'my carer')),
    ('media enquiry', (
        'journalist', 'reporter', 'press enquiry', 'press inquiry', 'media enquiry',
        'media inquiry', 'for publication', 'on the record', 'writing an article',
        'our newsroom')),
)

SENTIMENT_RULES = (
    ('angry', (
        'ridiculous', 'outrageous', 'disgusted', 'furious', 'fed up', 'sick of',
        'appalling', 'unacceptable', 'the worst', 'pathetic', 'useless',
        'never again', 'scam', 'fraud', 'a disgrace', 'shocking', 'damn',
        'bloody', 'crap', 'wtf', 'demand', 'i want someone to explain')),
    ('negative', (
        'disappointed', 'frustrated', 'frustrating', 'not happy', 'unhappy',
        'still waiting', 'no response', 'no reply', 'annoying', 'annoyed',
        'poor', 'let down', 'concerned', 'worried', 'problem', 'issue',
        'broken', 'not working', 'does not work', "doesn't work", 'failed',
        'delay', 'delayed', 'chasing')),
    ('positive', (
        'thank you', 'thanks', 'thank', 'appreciate', 'appreciated', 'great',
        'love', 'excellent', 'brilliant', 'happy with', 'helpful', 'well done',
        'grateful', 'perfect', 'no rush')),
)

# The response targets used when the knowledge base has no SLA document.
# Stated here so ``check_sla`` always has something defensible to report, and
# always says which of the two it used.
SLA_DEFAULT_HOURS = {'urgent': 4, 'high': 8, 'normal': 24, 'low': 72}

# Words too common to identify anything, removed before any similarity or
# recurring-signature arithmetic. Without this every ticket looks like every
# other ticket because they all say 'please' and 'account'.
STOPWORDS = frozenset("""
about above after again against all also always am and another any are around
as ask asked at back be because been before being below best both but by came
can cannot come could day days did does doing done down during each even ever
every few for from further get gets getting give given had has have having
help here hers him his how however into its just keep kept know known last
least like liked long made make many may me might more most much must my need
needed needs never new next no nor not now off often once one only onto or
other others our ours out over own per please put ran rather really regard
regards said same say says see seem seen send sent set she should since so
some someone something soon still such sure take taken tell than that the
their them then there these they thing things this those though through time
times to today too took two under until up upon us use used using very want
wanted was way we week weeks well went were what when where whether which
while who whom why will with within without word words work would yet you
your yours
""".split())

# Patterns that look like a reference a customer would quote back. Used both to
# tie duplicates together and to notice when the one detail needed to act is
# missing.
REFERENCE_PATTERNS = (
    re.compile(r'\b[A-Z]{2,6}-\d{2,8}\b'),          # ORD-10422, SUP-118
    re.compile(r'#\s?(\d{4,10})\b'),                 # #100422
    re.compile(r'\b(?:order|invoice|inv|ref|reference|account)\s*'
               r'(?:no\.?|number|#|:)?\s*([A-Z0-9\-]{4,20})\b', re.I),
    re.compile(r'\b\d{6,12}\b'),                     # a bare long number
)

EMAIL_PATTERN = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')

MAX_DRAFT_QUOTES = 3
DUPLICATE_STRONG = 0.55
POLICY_DOC_TYPES = ('policy', 'faq', 'troubleshooting')


# ===========================================================================
# SCHEMA HELPERS -- small, so the tool declarations stay readable
# ===========================================================================

def _schema(properties=None, required=()):
    return {'type': 'object', 'properties': dict(properties or {}),
            'required': list(required)}


def _text(description, enum=None):
    field = {'type': 'string', 'description': description}
    if enum:
        field['enum'] = list(enum)
    return field


def _number(description):
    return {'type': 'integer', 'description': description}


def _flag(description):
    return {'type': 'boolean', 'description': description}


TICKET_ID = _number('Numeric id of the ticket. Use support.list_tickets to find it.')


# ===========================================================================
# LOOKUP HELPERS -- a missing id is a readable result, never an exception
# ===========================================================================

def _ticket(ticket_id):
    """One ticket by id or by reference, or None. Tolerant of 'TKT-14' and '14'."""
    from ..models_support import SupportTicket

    if ticket_id in (None, '', 0):
        return None
    raw = str(ticket_id).strip().upper()
    if raw.startswith('TKT-'):
        raw = raw[4:]
    try:
        return SupportTicket.objects.filter(pk=int(raw)).first()
    except (TypeError, ValueError):
        return SupportTicket.objects.filter(reference=str(ticket_id).strip()).first()


def _no_ticket(ticket_id):
    return ToolResult(
        ok=False, error=f'No ticket {ticket_id}.',
        text=(f'There is no ticket with id {ticket_id}. Call support.list_tickets to see '
              f'what exists, or support.create_ticket if this request has not been '
              f'recorded yet. Do not guess a ticket id.'))


def _customer(customer_id):
    from ..models_support import Customer

    if customer_id in (None, '', 0):
        return None
    try:
        return Customer.objects.filter(pk=int(customer_id)).first()
    except (TypeError, ValueError):
        return None


def _no_customer(customer_id):
    return ToolResult(
        ok=False, error=f'No customer {customer_id}.',
        text=(f'There is no customer with id {customer_id}. Call support.list_customers to '
              f'find them, or support.create_customer to add them.'))


def _agent_label(ctx):
    return getattr(getattr(ctx, 'agent', None), 'name', '') or 'Customer Support'


def _company_name(default='our team'):
    """The company name from SystemSetting, so drafts are not signed 'Company'."""
    try:
        from ..models_platform import SystemSetting
        row = (SystemSetting.objects.filter(key='company-name').first()
               or SystemSetting.objects.filter(key='company_name').first())
    except Exception:  # noqa: BLE001 -- a missing table must not stop a draft
        return default
    if row is None:
        return default
    value = row.resolved
    return str(value).strip() if value else default


# ===========================================================================
# TRIAGE -- deterministic, explainable, evidence-reporting
# ===========================================================================

def _normalise(text):
    """Lowercase with collapsed whitespace, so a phrase match is not defeated
    by a line break in the middle of 'charged  twice'."""
    return ' '.join(str(text or '').lower().split())


def _match_phrases(haystack, phrases):
    """Every phrase from the table present in the text, in table order."""
    return [phrase for phrase in phrases if phrase in haystack]


def _combined_text(ticket=None, text=''):
    """The text triage runs over: the subject, the body and the customer's own
    messages. Inbound thread replies are included because a ticket that turned
    urgent on its third message is still urgent."""
    parts = []
    if ticket is not None:
        parts.append(ticket.subject or '')
        parts.append(ticket.body or '')
        for entry in ticket.messages.filter(direction='inbound')[:20]:
            parts.append(entry.body or '')
    if text:
        parts.append(str(text))
    return '\n'.join(part for part in parts if part)


def categorise(text):
    """Classify text into the ticket category set. Returns (category, evidence, ranking).

    Evidence is the list of matched phrases. A multi-word phrase scores two and
    a single word scores one, because 'money back' is far better evidence of a
    refund request than the word 'back'.
    """
    low = _normalise(text)
    ranking = []
    for index, (name, phrases) in enumerate(CATEGORY_RULES):
        hits = _match_phrases(low, phrases)
        if hits:
            score = sum(2 if ' ' in phrase else 1 for phrase in hits)
            ranking.append((score, -index, name, hits))
    if not ranking:
        return 'other', [], []
    ranking.sort(reverse=True)
    readable = [(name, score) for score, _order, name, _hits in ranking]
    return ranking[0][2], ranking[0][3], readable


def tone_signals(text):
    """Markers of anger and shouting. Reported, then given no weight."""
    low = _normalise(text)
    found = list(_match_phrases(low, TONE_MARKERS))
    raw = str(text or '')
    shouted = [word for word in re.findall(r'\b[A-Z]{4,}\b', raw)
               if word not in ('ASAP', 'HELP', 'URGENT')]
    if len(shouted) >= 2:
        found.append(f'{len(shouted)} words in capitals')
    if '!!' in raw:
        found.append('repeated exclamation marks')
    if 'urgent' in low or 'asap' in low:
        found.append("the word 'urgent' or 'asap' with no stated consequence")
    return found


def derive_priority(text, customer=None, category=''):
    """Work out a priority from impact and say exactly which rule decided it.

    Returns a dict with ``priority``, ``signals`` (the customer's own phrases),
    ``rule`` (a sentence naming the rule and why it is defensible), ``tone``
    (markers found and ignored) and ``fired`` (every rule that matched).
    """
    low = _normalise(text)
    fired = []
    for name, priority, phrases, why in PRIORITY_RULES:
        hits = _match_phrases(low, phrases)
        if hits:
            fired.append({'rule': name, 'priority': priority,
                          'signals': hits, 'why': why})

    tone = tone_signals(text)

    if fired:
        fired.sort(key=lambda entry: (PRIORITY_RANK[entry['priority']],
                                      len(entry['signals'])), reverse=True)
        winner = fired[0]
        priority = winner['priority']
        signals = list(winner['signals'])
        quoted = ', '.join(f'"{phrase}"' for phrase in winner['signals'][:4])
        rule = f"the {winner['rule']} rule fired on {quoted} -- {winner['why']}"
    elif category == 'feature_request':
        priority, signals = 'low', []
        rule = ('no impact signal and the request is an enhancement, so it is low: '
                'nothing is broken for the customer today')
    else:
        priority, signals = 'normal', []
        rule = ('no impact signal was found, so the default of normal applies. '
                'Nothing in the message names data loss, a security exposure, a '
                'double charge, an outage, a block on the customer working, a '
                'named deadline or a legal route')

    if customer is not None and getattr(customer, 'is_priority', False):
        tier = customer.get_tier_display()
        if PRIORITY_RANK[priority] < PRIORITY_RANK['high']:
            was = priority
            priority = PRIORITY_ORDER[min(PRIORITY_RANK[priority] + 1, 3)]
            article = 'an' if tier[:1].upper() in 'AEIOU' else 'a'
            rule += (f'. Then raised from {was} to {priority} because this is '
                     f'{article} {tier} customer, which is a contractual fact '
                     f'rather than a matter of tone')

    return {'priority': priority, 'signals': signals, 'rule': rule,
            'tone': tone, 'fired': fired}


def analyse_sentiment_text(text):
    """Sentiment with the phrases that decided it. Returns (label, phrases)."""
    low = _normalise(text)
    scores = {}
    evidence = {}
    for label, phrases in SENTIMENT_RULES:
        hits = _match_phrases(low, phrases)
        if hits:
            scores[label] = len(hits)
            evidence[label] = list(hits)

    shouting = [marker for marker in tone_signals(text)
                if 'capitals' in marker or 'exclamation' in marker]
    if shouting:
        scores['angry'] = scores.get('angry', 0) + 1
        evidence.setdefault('angry', []).extend(shouting)

    if not scores:
        return 'neutral', []
    if scores.get('angry'):
        return 'angry', evidence['angry']
    negative = scores.get('negative', 0)
    positive = scores.get('positive', 0)
    if negative > positive:
        return 'negative', evidence['negative']
    if positive > negative:
        return 'positive', evidence['positive']
    return 'neutral', evidence.get('negative', []) + evidence.get('positive', [])


def needs_human_check(text):
    """Whether this message must never receive an AI-drafted reply.

    Returns (needed, reason, phrases). Kept separate from prioritisation
    because the two answer different questions: prioritisation asks how soon,
    this asks whether an AI should touch it at all.
    """
    low = _normalise(text)
    for reason, phrases in HUMAN_RULES:
        hits = _match_phrases(low, phrases)
        if hits:
            return True, reason, hits
    return False, '', []


def triage(ticket, save=True):
    """Run the whole triage over one ticket and write the result onto it.

    Called by ``create_ticket`` and ``import_email_as_ticket`` so no ticket is
    ever created untriaged. An untriaged ticket is invisible to
    ``detect_urgent_complaints`` and to every report, which is the quiet way a
    genuine emergency sits in a queue overnight.
    """
    text = _combined_text(ticket)
    category, evidence, _ranking = categorise(text)
    verdict = derive_priority(text, customer=ticket.customer, category=category)
    sentiment, sentiment_phrases = analyse_sentiment_text(text)
    flagged, reason, human_phrases = needs_human_check(text)

    ticket.category = category
    ticket.priority = verdict['priority']
    ticket.sentiment = sentiment
    ticket.urgency_signals = list(verdict['signals'])
    if flagged:
        ticket.needs_human = True
        joined = ', '.join(human_phrases[:4])
        ticket.escalation_reason = (
            ticket.escalation_reason
            or f'Flagged automatically at intake: {reason} ({joined}).')
    if not ticket.sla_due_at:
        hours = SLA_DEFAULT_HOURS.get(ticket.priority, 24)
        ticket.sla_due_at = (ticket.created_at or timezone.now()) + timedelta(hours=hours)

    if save:
        ticket.save(update_fields=[
            'category', 'priority', 'sentiment', 'urgency_signals', 'needs_human',
            'escalation_reason', 'sla_due_at', 'updated_at'])

    return {
        'category': category, 'category_evidence': evidence,
        'priority': verdict['priority'], 'priority_rule': verdict['rule'],
        'signals': verdict['signals'], 'tone': verdict['tone'],
        'sentiment': sentiment, 'sentiment_phrases': sentiment_phrases,
        'needs_human': flagged, 'needs_human_reason': reason,
        'needs_human_phrases': human_phrases,
    }


# ===========================================================================
# SIMILARITY -- for duplicates and for recurring problems
# ===========================================================================

def _terms(text):
    """The distinctive words in a piece of text, as a set."""
    words = re.findall(r"[a-z][a-z0-9'\-]{2,}", str(text or '').lower())
    return {word for word in words if word not in STOPWORDS and len(word) > 3}


def _signatures(text):
    """Adjacent pairs of distinctive words, e.g. 'export timeout'.

    A pair identifies a problem where a single word does not: 'export' appears
    in half the tickets, 'export timeout' appears in the seven that are the
    same bug.
    """
    words = re.findall(r'[a-z][a-z0-9\-]{2,}', str(text or '').lower())
    kept = [word for word in words if word not in STOPWORDS and len(word) > 3]
    return {f'{first} {second}' for first, second in zip(kept, kept[1:])}


def _references(text):
    """Order, invoice and account references found in the text."""
    found = set()
    raw = str(text or '')
    for pattern in REFERENCE_PATTERNS:
        for match in pattern.finditer(raw):
            value = match.group(1) if match.groups() else match.group(0)
            value = str(value).strip().upper()
            if len(value) >= 4:
                found.add(value)
    return found


def similarity(first, second):
    """How alike two tickets are, and why. Returns (score, explanation dict).

    The score is the mean of the Jaccard overlap and the containment of the
    smaller term set, then raised for the two facts that actually make a
    duplicate: the same customer, or the same order reference. Fewer than two
    shared distinctive terms scores zero, because one shared word is a
    coincidence and closing somebody's ticket on a coincidence is worse than
    leaving it open.
    """
    text_a = f'{first.subject} {first.body}'
    text_b = f'{second.subject} {second.body}'
    terms_a, terms_b = _terms(text_a), _terms(text_b)
    shared = terms_a & terms_b

    if len(shared) < 2 or not terms_a or not terms_b:
        return 0.0, {'shared_terms': sorted(shared), 'same_customer': False,
                     'shared_references': []}

    union = terms_a | terms_b
    jaccard = len(shared) / len(union)
    containment = len(shared) / min(len(terms_a), len(terms_b))
    score = (jaccard + containment) / 2

    same_customer = bool(first.customer_id and first.customer_id == second.customer_id)
    if not same_customer:
        address_a = (first.contact_email or '').lower()
        address_b = (second.contact_email or '').lower()
        same_customer = bool(address_a and address_a == address_b)
    if same_customer:
        score += 0.25

    shared_refs = sorted(_references(text_a) & _references(text_b))
    if shared_refs:
        score += 0.35

    return round(min(score, 1.0), 2), {
        'shared_terms': sorted(shared)[:12],
        'same_customer': same_customer,
        'shared_references': shared_refs,
    }


def _median(values):
    numbers = [value for value in values if value is not None]
    return round(statistics.median(numbers), 1) if numbers else None


# ===========================================================================
# KNOWLEDGE -- grounding, and the honest failure when there is no ground
# ===========================================================================

def knowledge_hits(query, limit=5, doc_types=()):
    """Search the knowledge base. Returns (hits, problem).

    ``hits`` is None when the knowledge base is unavailable, which is a
    different answer from an empty list: "I could not look" and "I looked and
    there is nothing" lead to different replies to the customer, and conflating
    them is how an improvised policy gets sent.

    The import is inside the function and guarded because the knowledge module
    is owned by another part of the platform; a support tool must degrade to
    "no policy found, escalate" rather than fail to load.
    """
    try:
        from .. import knowledge
    except ImportError:
        return None, 'the knowledge base module is not installed'

    search = getattr(knowledge, 'search', None)
    if search is None:
        return None, 'the knowledge base offers no search function'

    wanted = max(int(limit or 5), 1)
    try:
        raw = list(search(query, limit=wanted * 3 if doc_types else wanted) or [])
    except Exception as exc:  # noqa: BLE001 -- a search failure is a result
        return None, f'the knowledge base search failed ({exc})'

    if doc_types:
        raw = [hit for hit in raw
               if str(getattr(getattr(hit, 'document', None), 'doc_type', '')).lower()
               in doc_types]
    return raw[:wanted], ''


def _hit_fields(hit):
    """One knowledge hit flattened, tolerating a missing document."""
    document = getattr(hit, 'document', None)
    return {
        'title': str(getattr(document, 'title', '') or 'an untitled document'),
        'url': str(getattr(document, 'url', '') or ''),
        'doc_type': str(getattr(document, 'doc_type', '') or ''),
        'snippet': ' '.join(str(getattr(hit, 'snippet', '') or '').split()),
        'score': getattr(hit, 'score', 0),
    }


def _quote_lines(hits, limit=MAX_DRAFT_QUOTES):
    """Knowledge hits as readable quoted lines, for a tool result or a draft."""
    lines = []
    for hit in (hits or [])[:limit]:
        fields = _hit_fields(hit)
        where = f" ({fields['url']})" if fields['url'] else ''
        lines.append(f'- "{fields["snippet"][:280]}" -- {fields["title"]}{where}')
    return lines


# ===========================================================================
# INTEGRATION PAYLOAD NORMALISERS
# ===========================================================================
# The connectors are configured data rather than a fixed contract, and a demo
# payload does not have to use the same keys as a live one. These readers
# accept every reasonable shape so a tool never fails on a key name.

def _first_value(source, *keys):
    for key in keys:
        value = source.get(key)
        if value not in (None, '', [], {}):
            return value
    return ''


def _listing(data, *keys):
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _message_fields(item):
    """One email listing entry, normalised."""
    if not isinstance(item, dict):
        text = str(item)
        return {'id': '', 'from': '', 'subject': '', 'snippet': text[:300],
                'body': text, 'received': ''}
    return {
        'id': str(_first_value(item, 'id', 'message_id', 'messageId', 'uid')),
        'from': str(_first_value(item, 'from', 'sender', 'from_address',
                                 'from_email', 'address')),
        'subject': str(_first_value(item, 'subject', 'title')),
        'snippet': str(_first_value(item, 'snippet', 'preview', 'summary')),
        'body': str(_first_value(item, 'body', 'text', 'body_text', 'plain',
                                 'content', 'message')),
        'received': str(_first_value(item, 'date', 'received_at', 'timestamp')),
    }


def _address_of(raw):
    """The bare email address out of 'Ada Lovelace <ada@example.com>'."""
    match = EMAIL_PATTERN.search(str(raw or ''))
    return match.group(0).lower() if match else ''


def _display_name(raw):
    """The human name out of an email From header, or the local part."""
    text = str(raw or '').strip()
    if '<' in text:
        name = text.split('<', 1)[0].strip().strip('"')
        if name:
            return name
    address = _address_of(text)
    if address:
        local = address.split('@', 1)[0]
        return local.replace('.', ' ').replace('_', ' ').title()
    return ''


def _file_fields(item):
    """One Drive file entry, normalised."""
    if not isinstance(item, dict):
        return {'id': '', 'name': str(item), 'url': '', 'snippet': ''}
    return {
        'id': str(_first_value(item, 'id', 'file_id', 'fileId')),
        'name': str(_first_value(item, 'name', 'title', 'filename')),
        'url': str(_first_value(item, 'url', 'link', 'webViewLink')),
        'snippet': str(_first_value(item, 'snippet', 'summary', 'description')),
    }


# ===========================================================================
# DRAFTING HELPERS
# ===========================================================================
# Drafts are composed from the ticket, the policy quotes and a small set of
# shapes. No language model is called: a reply that quotes a policy it actually
# found is worth more than a fluent one that improvised the concession.

TONE_OPENERS = {
    'warm': 'Thanks for getting in touch, and sorry for the trouble.',
    'neutral': 'Thanks for getting in touch.',
    'formal': 'Thank you for contacting us regarding this matter.',
    'apologetic': 'I am sorry this has happened, and thank you for telling us.',
    'brief': 'Thanks for the note.',
}

# What a draft in each category must not promise. Stated per category because
# the improvisation that causes real damage is category-specific: a made-up
# refund window, a delivery date nobody controls, a root cause nobody has found.
MUST_NOT_PROMISE = {
    'refund': ('a refund amount, a refund window or a processing time that the '
               'refund policy does not state'),
    'billing': ('a credit, a waiver or a price that is not in the pricing or '
                'billing policy'),
    'order': 'an order change or cancellation that the order policy does not allow',
    'delivery': ('a delivery date, because the courier controls it and a promised '
                 'date becomes the second complaint'),
    'technical': 'a fix date or a root cause, until engineering has confirmed both',
    'account': ('access, a data export or a deletion outside the account and '
                'privacy policy'),
    'feature_request': 'that the feature will be built, or when',
    'complaint': 'compensation, or that a named person will be disciplined',
    'other': 'anything the knowledge base does not support',
}

# The one detail that most often has to be asked for before anything can be
# done, per category, with the question that gets it.
UNBLOCKING_QUESTION = {
    'refund': ('the order or invoice reference the refund relates to',
               'Which order or invoice reference should we refund against?'),
    'billing': ('the invoice number, or the date and amount of the charge',
                'Which invoice number, or what date and amount was the charge?'),
    'order': ('the order reference', 'What is the order reference?'),
    'delivery': ('the order or tracking reference',
                 'What is the order or tracking reference?'),
    'technical': ('the exact error text and when it started',
                  'What exactly does the error say, and when did it start?'),
    'account': ('the email address on the account',
                'Which email address is the account under?'),
    'feature_request': ('what the customer is trying to achieve',
                        'What are you trying to achieve, so we can look at how it '
                        'is done today?'),
    'complaint': ('what outcome the customer wants',
                  'What would you like us to do to put this right?'),
    'other': ('what specifically the customer wants done',
              'What would you like us to do next?'),
}


def _greeting(ticket):
    """A greeting that uses a name when there is a real one to use."""
    if ticket.customer_id and ticket.customer and ticket.customer.name:
        first = ticket.customer.name.split()[0]
        return f'Hi {first},'
    name = _display_name(ticket.contact_email)
    if name and ' ' in name:
        return f'Hi {name.split()[0]},'
    return 'Hello,'


def _signature(ctx):
    company = _company_name()
    return f'Kind regards,\n{_agent_label(ctx)}\n{company}'


def _next_step_line(ticket):
    """When the customer will next hear something, taken from the SLA rather
    than invented, because a date in a reply is a promise."""
    if ticket.sla_due_at and ticket.sla_due_at > timezone.now():
        return (f'You will hear from us by '
                f'{ticket.sla_due_at:%d %b %Y at %H:%M} at the latest.')
    hours = SLA_DEFAULT_HOURS.get(ticket.priority, 24)
    return f'We will come back to you within {hours} hours.'


def _save_draft(ctx, ticket, body, direction='outbound'):
    """Write a draft onto the ticket thread and return the row.

    The draft is stored before it is returned so it exists as a record whether
    or not the conversation that produced it goes anywhere. A reply that lives
    only in a chat transcript cannot be reviewed on the ticket.
    """
    from ..models_support import TicketMessage

    return TicketMessage.objects.create(
        ticket=ticket, direction=direction, author_label=_agent_label(ctx),
        body=body, is_ai_draft=True)


def _latest_draft(ticket):
    """The most recent unsent AI draft on this ticket, or None."""
    return (ticket.messages.filter(direction='outbound', is_ai_draft=True,
                                   sent_message__isnull=True)
            .order_by('-created_at').first())


def _human_only_result(ticket, verb='draft a reply to'):
    """The refusal used everywhere ``needs_human`` is set."""
    reason = ticket.escalation_reason or 'it was flagged as needing a person.'
    return ToolResult(
        ok=False, error='Ticket requires a human.',
        text=(f'{ticket.reference} is flagged needs_human, so I will not {verb} it. '
              f'Reason on file: {reason} A person must handle this ticket directly. '
              f'What you can still do: add an internal note with '
              f'support.add_internal_note, tell the team with '
              f'support.notify_support_team, or file a tracked record with '
              f'support.escalate_to_jira. Do not write customer-facing wording for '
              f'this ticket, and do not offer suggested wording in your reply '
              f'either -- the risk is precisely that a good draft gets approved.'),
        data={'ticket_id': ticket.pk, 'reference': ticket.reference,
              'needs_human': True},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


def _policy_block(hits, problem, category):
    """The policy section of a draft, and the honest version when there is none.

    Returns (lines, grounded). ``grounded`` is False when nothing was found,
    which every caller uses to refuse to make a commitment rather than to
    invent one.
    """
    if hits:
        lines = ['What our policy says:']
        for hit in hits[:MAX_DRAFT_QUOTES]:
            fields = _hit_fields(hit)
            lines.append(f'- {fields["snippet"][:280]} ({fields["title"]})')
        return lines, True
    missing = problem or 'no policy document matched this request'
    return ([f'No policy was quoted here because {missing}.'], False)


# ===========================================================================
# EXECUTION RECORD HELPERS -- used only by the executors
# ===========================================================================

def _outcome_status(result):
    """The execution-record status matching what the connector actually did."""
    if not result.ok:
        return 'failed'
    return 'simulated' if result.demo else 'sent'


def _record_message(action, result, *, channel, recipient, subject, body,
                    metadata=None):
    """One OutboundMessage row describing what execution did.

    The live or simulated distinction comes straight from ``CallResult.demo``,
    so a simulated send is never recorded as a real one.
    """
    from ..models_platform import OutboundMessage

    data = result.data or {}
    return OutboundMessage.objects.create(
        channel=channel,
        recipient=str(recipient or '')[:400],
        subject=str(subject or '')[:300],
        body=body or '',
        status=_outcome_status(result),
        external_id=str(_first_value(data, 'id', 'message_id', 'ts',
                                     'thread_ts'))[:200],
        external_url=str(_first_value(data, 'url', 'link', 'permalink'))[:400],
        error_message=result.error or '',
        agent=action.agent,
        action=action,
        integration=action.integration,
        metadata=dict(metadata or {}),
    )


def _execution_text(result, what):
    """One line saying what execution did, never overstating a simulation."""
    if not result.ok:
        return f'{what} failed: {result.error}'
    if result.demo:
        return (f'{what} was simulated in demo mode and is recorded as simulated, '
                f'so nothing left the company. {result.summary}')
    return f'{what} completed. {result.summary}'


def _action_ticket(action):
    """The ticket an approved action concerns, or None."""
    return _ticket((action.payload or {}).get('ticket_id'))


# ===========================================================================
# GROUP: INTAKE
# ===========================================================================

def _triage_summary(ticket, verdict):
    """The triage result as the sentence a model should read back."""
    lines = [
        f'Category: {ticket.get_category_display()} '
        f'({", ".join(verdict["category_evidence"][:5]) or "no keyword matched, so it is Other"}).',
        f'Priority: {ticket.get_priority_display()} because {verdict["priority_rule"]}.',
        f'Sentiment: {ticket.get_sentiment_display()}'
        + (f' from {", ".join(verdict["sentiment_phrases"][:4])}.'
           if verdict['sentiment_phrases'] else ' (nothing signalled either way).'),
    ]
    if verdict['tone']:
        lines.append(
            f'Tone markers present and deliberately ignored for priority: '
            f'{", ".join(verdict["tone"][:4])}. Priority follows impact, not volume.')
    if verdict['needs_human']:
        lines.append(
            f'NEEDS A HUMAN: {verdict["needs_human_reason"]} '
            f'({", ".join(verdict["needs_human_phrases"][:4])}). Do not draft or send '
            f'a customer reply for this ticket.')
    if ticket.sla_due_at:
        lines.append(f'Response target: {ticket.sla_due_at:%d %b %Y at %H:%M}.')
    return lines


@tool(name='support.create_ticket',
      title='Create a support ticket',
      group='Intake', agent_types=('support',), icon='fa-ticket',
      capability='Record a customer request as a triaged ticket',
      parameters=_schema({
          'subject': _text('One line saying what the request is about.'),
          'body': _text("The customer's message, as fully as it is available."),
          'contact_email': _text('The address it came from, when no Customer row exists.'),
          'customer_id': _number('Existing customer id, if this is a known customer.'),
          'channel': _text('How it arrived.',
                           ('email', 'chat', 'phone', 'web', 'social')),
          'category': _text('Leave empty to have it classified from the text.'),
          'priority': _text('Leave empty to have it derived from impact.'),
          'source_message_id': _text('The Gmail message id, when imported from email.'),
      }, required=('subject', 'body')))
def create_ticket(ctx, subject, body, contact_email='', customer_id=None,
                  channel='email', category='', priority='', source_message_id=''):
    """Record a customer request as a ticket and triage it in the same step.

    Triage is not optional here, and that is the point. A ticket created
    without a category, a priority and a sentiment is invisible to
    detect_urgent_complaints, to check_sla and to every report, which is the
    quiet way a real emergency sits in a queue overnight. So this tool runs the
    same classification, prioritisation, sentiment analysis and needs-a-human
    check that the individual triage tools expose, and reports what fired.

    Give it the customer's own words in ``body``. Triage reads that text, so a
    paraphrase weakens every decision made from it. Pass ``category`` or
    ``priority`` only when a person has told you what they should be.
    """
    from ..models_support import SupportTicket, TicketMessage

    customer = None
    if customer_id not in (None, '', 0):
        customer = _customer(customer_id)
        if customer is None:
            return _no_customer(customer_id)

    address = _address_of(contact_email) or (contact_email or '').strip()
    if not address and customer is not None:
        address = customer.email

    ticket = SupportTicket.objects.create(
        customer=customer,
        contact_email=address,
        subject=str(subject)[:300],
        body=str(body or ''),
        channel=channel if channel in dict(SupportTicket.CHANNEL_CHOICES) else 'email',
        source_message_id=str(source_message_id or '')[:200],
        created_by_agent=getattr(ctx, 'agent', None),
        status='new',
    )
    TicketMessage.objects.create(
        ticket=ticket, direction='inbound',
        author_label=(str(customer) if customer else address or 'the customer'),
        body=str(body or ''))

    verdict = triage(ticket)

    # An explicit instruction from a person outranks the rules, but the rules'
    # verdict is still reported so the override is visible.
    overrides = []
    if category and category in dict(SupportTicket.CATEGORY_CHOICES):
        if category != ticket.category:
            overrides.append(f'category set to {category} as instructed, '
                             f'over the classified {ticket.category}')
        ticket.category = category
    if priority and priority in PRIORITY_RANK:
        if priority != ticket.priority:
            overrides.append(f'priority set to {priority} as instructed, '
                             f'over the derived {ticket.priority}')
        ticket.priority = priority
    if overrides:
        ticket.save(update_fields=['category', 'priority', 'updated_at'])

    lines = [f'Created {ticket.reference} (id {ticket.pk}): {ticket.subject}',
             f'From: {ticket.customer_label}']
    lines += _triage_summary(ticket, verdict)
    lines += [f'Override: {entry}.' for entry in overrides]
    if not verdict['needs_human']:
        lines.append('Next: support.suggest_resolution for a policy-grounded answer, '
                     'or support.identify_customer_issue if the request is unclear.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'reference': ticket.reference,
              'category': ticket.category, 'priority': ticket.priority,
              'sentiment': ticket.sentiment, 'needs_human': ticket.needs_human,
              'urgency_signals': ticket.urgency_signals},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.read_customer_requests',
      title='Read waiting customer requests',
      group='Intake', agent_types=('support',), integration='gmail',
      icon='fa-inbox', reads_only=True,
      capability='List customer email waiting to be handled',
      parameters=_schema({
          'query': _text("Gmail search, e.g. 'is:unread' or 'from:acme.com'."),
          'limit': _number('How many to list. Default 15.'),
      }))
def read_customer_requests(ctx, query='is:unread', limit=15):
    """List the customer email that is waiting, without creating anything.

    Reading and recording are separate on purpose. Importing every unread
    message as a ticket would fill the queue with newsletters, bounces and
    replies to tickets that already exist. So this tool only reports what is
    there, with each message id, and you decide which ones are real requests
    and import those with support.import_email_as_ticket.

    Report back what is waiting and which of them look like genuine support
    requests, then import those.
    """
    try:
        count = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        count = 15

    result = integrations.call('gmail', 'list_messages', query=query, limit=count)
    if not result.ok:
        return ToolResult(
            ok=False, error=result.error, demo=result.demo,
            text=(f'Could not read the mailbox: {result.error} '
                  f'Check the Gmail integration on the Integrations page.'))

    items = _listing(result.data or {}, 'messages', 'items', 'results', 'emails')
    if not items:
        return ToolResult(
            ok=True, demo=result.demo,
            text=f'Nothing matches "{query}" in the mailbox right now.',
            data={'query': query, 'count': 0})

    lines = [f'{len(items)} message(s) matching "{query}":']
    rows = []
    for item in items:
        fields = _message_fields(item)
        snippet = fields['snippet'] or fields['body'][:200]
        lines.append(
            f"- [{fields['id'] or 'no id'}] from {fields['from'] or 'unknown'} "
            f"-- {fields['subject'] or '(no subject)'}"
            + (f"\n  {' '.join(snippet.split())[:200]}" if snippet else ''))
        rows.append(fields)

    lines.append('No tickets were created. Import the genuine requests with '
                 'support.import_email_as_ticket using the id in brackets.')

    return ToolResult(ok=True, demo=result.demo, text='\n'.join(lines),
                      data={'query': query, 'count': len(rows), 'messages': rows})


@tool(name='support.import_email_as_ticket',
      title='Import an email as a ticket',
      group='Intake', agent_types=('support',), integration='gmail',
      icon='fa-file-import',
      capability='Turn one customer email into a triaged ticket',
      parameters=_schema({
          'message_id': _text('The message id from support.read_customer_requests.'),
      }, required=('message_id',)))
def import_email_as_ticket(ctx, message_id):
    """Read one email and record it as a fully triaged ticket.

    The whole message body is read rather than the snippet, because triage
    decides priority from phrases like 'charged me twice' and 'our team cannot
    invoice' and those are rarely in the first line. The message id is stored on
    the ticket so the thread can be traced back to the mailbox.

    If a Customer already exists with that email address it is linked, which is
    what lets tier raise the priority and lets duplicate detection see that two
    tickets came from the same person.
    """
    from ..models_support import Customer, SupportTicket, TicketMessage

    result = integrations.call('gmail', 'read_message', message_id=message_id)
    if not result.ok:
        return ToolResult(
            ok=False, error=result.error, demo=result.demo,
            text=(f'Could not read message {message_id}: {result.error} '
                  f'Call support.read_customer_requests to get a current message id.'))

    data = result.data or {}
    payload = data.get('message') if isinstance(data.get('message'), dict) else data
    fields = _message_fields(payload)
    body = fields['body'] or fields['snippet']
    if not fields['subject'] and not body:
        return ToolResult(
            ok=False, demo=result.demo, error='Empty message.',
            text=(f'Message {message_id} came back with no subject and no body, so '
                  f'there is nothing to triage. It may have been deleted.'))

    address = _address_of(fields['from'])
    existing = SupportTicket.objects.filter(
        source_message_id=str(message_id)).first() if message_id else None
    if existing is not None:
        return ToolResult(
            ok=True, demo=result.demo,
            text=(f'That email is already {existing.reference} (id {existing.pk}), '
                  f'status {existing.get_status_display()}. Nothing was created again. '
                  f'Use support.get_ticket to read it.'),
            data={'ticket_id': existing.pk, 'reference': existing.reference,
                  'already_imported': True},
            subject_label='marketing.supportticket', subject_id=existing.pk)

    customer = Customer.objects.filter(email__iexact=address).first() if address else None

    ticket = SupportTicket.objects.create(
        customer=customer,
        contact_email=address,
        subject=(fields['subject'] or 'Customer request')[:300],
        body=body,
        channel='email',
        source_message_id=str(message_id)[:200],
        created_by_agent=getattr(ctx, 'agent', None),
        status='new',
    )
    TicketMessage.objects.create(
        ticket=ticket, direction='inbound',
        author_label=fields['from'] or address or 'the customer', body=body)

    verdict = triage(ticket)

    lines = [f'Imported message {message_id} as {ticket.reference} (id {ticket.pk}).',
             f'Subject: {ticket.subject}',
             f'From: {fields["from"] or address or "unknown sender"}'
             + (f' -- linked to customer #{customer.pk} '
                f'({customer.get_tier_display()})' if customer else
                ' -- no Customer row matched this address')]
    lines += _triage_summary(ticket, verdict)
    return ToolResult(
        ok=True, demo=result.demo, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'reference': ticket.reference,
              'category': ticket.category, 'priority': ticket.priority,
              'sentiment': ticket.sentiment, 'needs_human': ticket.needs_human},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.list_tickets',
      title='List support tickets',
      group='Intake', agent_types=('support',), icon='fa-list-check',
      reads_only=True, capability='List and filter support tickets',
      parameters=_schema({
          'status': _text("Filter by status, or 'open' for every open status."),
          'priority': _text('Filter by priority.', PRIORITY_ORDER),
          'category': _text('Filter by category.'),
          'customer_id': _number('Only this customer.'),
          'limit': _number('How many to return. Default 25.'),
      }))
def list_tickets(ctx, status='', priority='', category='', customer_id=None, limit=25):
    """List tickets, newest first, with the id and reference for every one.

    Use this before any tool that takes a ticket_id. Guessing an id is the
    single most common way a support action lands on the wrong customer.
    """
    from ..models_support import OPEN_STATUSES, SupportTicket

    try:
        count = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        count = 25

    queryset = SupportTicket.objects.all().select_related('customer')
    applied = []
    if status:
        if status == 'open':
            queryset = queryset.filter(status__in=OPEN_STATUSES)
            applied.append('status open')
        else:
            queryset = queryset.filter(status=status)
            applied.append(f'status {status}')
    if priority:
        queryset = queryset.filter(priority=priority)
        applied.append(f'priority {priority}')
    if category:
        queryset = queryset.filter(category=category)
        applied.append(f'category {category}')
    if customer_id not in (None, '', 0):
        customer = _customer(customer_id)
        if customer is None:
            return _no_customer(customer_id)
        queryset = queryset.filter(customer=customer)
        applied.append(f'customer {customer}')

    total = queryset.count()
    rows = list(queryset[:count])
    if not rows:
        return ToolResult(
            ok=True,
            text=('No tickets match ' + (', '.join(applied) if applied else 'the whole table')
                  + '. Widen the filter, or create the ticket with support.create_ticket.'),
            data={'count': 0, 'filters': applied})

    lines = [f'{len(rows)} of {total} ticket(s)'
             + (f' matching {", ".join(applied)}' if applied else '') + ':']
    for ticket in rows:
        marks = []
        if ticket.needs_human:
            marks.append('NEEDS A HUMAN')
        if ticket.is_breaching_sla:
            marks.append('SLA BREACHED')
        if ticket.duplicate_of_id:
            marks.append(f'duplicate of #{ticket.duplicate_of_id}')
        lines.append(
            f'- id {ticket.pk} {ticket.reference} [{ticket.get_priority_display()}] '
            f'{ticket.get_status_display()} / {ticket.get_category_display()} -- '
            f'{ticket.subject[:90]} ({ticket.customer_label}, open {ticket.age_label})'
            + (f' *{"; ".join(marks)}*' if marks else ''))

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'count': len(rows), 'total': total, 'filters': applied,
              'tickets': [{'id': row.pk, 'reference': row.reference,
                           'subject': row.subject, 'status': row.status,
                           'priority': row.priority, 'category': row.category,
                           'needs_human': row.needs_human} for row in rows]})


@tool(name='support.get_ticket',
      title='Read one ticket',
      group='Intake', agent_types=('support',), icon='fa-file-lines',
      reads_only=True, capability='Read a ticket with its thread and customer',
      parameters=_schema({'ticket_id': TICKET_ID}, required=('ticket_id',)))
def get_ticket(ctx, ticket_id):
    """Read one ticket in full: the request, the customer, and the whole thread.

    Read this before drafting anything. Half of the replies that go wrong are
    replies to the first message of a thread whose third message already
    answered the question, or withdrew it.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    lines = [
        f'{ticket.reference} (id {ticket.pk}) -- {ticket.subject}',
        f'Status {ticket.get_status_display()}, priority '
        f'{ticket.get_priority_display()}, category {ticket.get_category_display()}, '
        f'sentiment {ticket.get_sentiment_display()}.',
        f'Channel {ticket.get_channel_display()}, open {ticket.age_label}, '
        f'{ticket.message_count} message(s).',
    ]

    if ticket.customer_id and ticket.customer:
        customer = ticket.customer
        lines.append(
            f'Customer: {customer} (id {customer.pk}), {customer.get_tier_display()} tier, '
            f'{customer.email or "no email on file"}, timezone '
            f'{customer.timezone_name or "unknown"}, '
            f'{customer.open_ticket_count} open ticket(s).')
        if customer.notes:
            lines.append(f'Customer notes: {customer.notes[:300]}')
    else:
        lines.append(f'Customer: no record. Contact address '
                     f'{ticket.contact_email or "unknown"}.')

    if ticket.urgency_signals:
        lines.append(f'Urgency signals on file: '
                     f'{", ".join(str(s) for s in ticket.urgency_signals[:6])}.')
    if ticket.sla_due_at:
        state = 'BREACHED' if ticket.is_breaching_sla else 'due'
        lines.append(f'Response target {state} {ticket.sla_due_at:%d %b %Y at %H:%M}.')
    if ticket.needs_human:
        lines.append(f'NEEDS A HUMAN. {ticket.escalation_reason or "Flagged for a person."} '
                     f'Do not draft or send a customer reply.')
    if ticket.assignee_id:
        lines.append(f'Assigned to {ticket.assignee.get_username()}.')
    if ticket.external_reference:
        lines.append(f'Filed externally as {ticket.external_reference} '
                     f'{ticket.external_url}'.strip())
    if ticket.duplicate_of_id:
        lines.append(f'Marked a duplicate of ticket #{ticket.duplicate_of_id}.')
    if ticket.resolution:
        lines.append(f'Resolution recorded: {ticket.resolution[:400]}')
    tags = list(ticket.tags.all())
    if tags:
        lines.append(f'Tags: {", ".join(tag.name for tag in tags)}.')

    lines.append('')
    lines.append('Original request:')
    lines.append(ticket.body[:2000] or '(no body was recorded)')

    thread = list(ticket.messages.all()[:40])
    if thread:
        lines.append('')
        lines.append('Thread:')
        for entry in thread:
            marker = ' [UNSENT DRAFT]' if entry.is_ai_draft and not entry.was_sent else ''
            marker = ' [sent]' if entry.was_sent else marker
            lines.append(f'- {entry.created_at:%d %b %H:%M} '
                         f'{entry.get_direction_display()}{marker} '
                         f'{entry.author_label or ""}: {entry.preview}')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'reference': ticket.reference,
              'status': ticket.status, 'priority': ticket.priority,
              'category': ticket.category, 'needs_human': ticket.needs_human,
              'message_count': ticket.message_count},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.list_customers',
      title='List customers',
      group='Intake', agent_types=('support',), icon='fa-users',
      reads_only=True, capability='Find customers',
      parameters=_schema({
          'tier': _text('Only this tier.', ('free', 'standard', 'premium', 'enterprise')),
          'query': _text('Match on name, email, company or account reference.'),
          'limit': _number('How many to return. Default 25.'),
      }))
def list_customers(ctx, tier='', query='', limit=25):
    """Find customers, with their id, tier and open ticket count.

    Use this to get a customer_id before creating a ticket for a known
    customer, and to check the tier before you promise a response time.
    """
    from django.db.models import Q

    from ..models_support import Customer

    try:
        count = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        count = 25

    queryset = Customer.objects.all()
    if tier:
        queryset = queryset.filter(tier=tier)
    if query:
        queryset = queryset.filter(
            Q(name__icontains=query) | Q(email__icontains=query)
            | Q(company__icontains=query) | Q(account_reference__icontains=query))

    total = queryset.count()
    rows = list(queryset[:count])
    if not rows:
        return ToolResult(
            ok=True,
            text=(f'No customer matches {query or tier or "that"}. '
                  f'Add them with support.create_customer, or record the ticket with '
                  f'contact_email only.'),
            data={'count': 0})

    lines = [f'{len(rows)} of {total} customer(s):']
    for customer in rows:
        lines.append(
            f'- id {customer.pk} {customer} [{customer.get_tier_display()}] '
            f'{customer.email or "no email"} -- {customer.open_ticket_count} open ticket(s)'
            + (' (priority customer)' if customer.is_priority else ''))
    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'count': len(rows), 'total': total,
                            'customers': [{'id': row.pk, 'name': row.name,
                                           'email': row.email, 'tier': row.tier}
                                          for row in rows]})


@tool(name='support.create_customer',
      title='Create a customer',
      group='Intake', agent_types=('support',), icon='fa-user-plus',
      capability='Add a customer record',
      parameters=_schema({
          'name': _text('The person or organisation name.'),
          'email': _text('Their email address.'),
          'company': _text('Their company, if different from the name.'),
          'tier': _text('Support tier.', ('free', 'standard', 'premium', 'enterprise')),
          'phone': _text('Contact number.'),
      }, required=('name', 'email')))
def create_customer(ctx, name, email, company='', tier='standard', phone=''):
    """Add a customer record so their tickets can be linked together.

    Linking matters for two reasons beyond tidiness: tier is the only thing
    allowed to raise a priority without an impact signal, and duplicate
    detection scores two tickets from the same customer far higher than two
    similar tickets from strangers.

    Set the tier from something you were told or read, never from how important
    the customer sounds.
    """
    from ..models_support import Customer

    address = _address_of(email) or str(email or '').strip()
    existing = Customer.objects.filter(email__iexact=address).first() if address else None
    if existing is not None:
        return ToolResult(
            ok=True,
            text=(f'{existing} already exists as customer id {existing.pk} '
                  f'({existing.get_tier_display()} tier). Nothing was created. '
                  f'Use that id.'),
            data={'customer_id': existing.pk, 'created': False},
            subject_label='marketing.customer', subject_id=existing.pk)

    valid_tiers = dict(Customer.TIER_CHOICES)
    customer = Customer.objects.create(
        name=str(name)[:160], email=address, company=str(company or '')[:160],
        tier=tier if tier in valid_tiers else 'standard',
        phone=str(phone or '')[:40])

    return ToolResult(
        ok=True,
        text=(f'Created customer id {customer.pk}: {customer}, '
              f'{customer.get_tier_display()} tier, {customer.email}.'
              + (' Priority customer, so their tickets are raised one level when no '
                 'impact signal is present.' if customer.is_priority else '')),
        data={'customer_id': customer.pk, 'created': True, 'tier': customer.tier},
        subject_label='marketing.customer', subject_id=customer.pk)


@tool(name='support.get_customer',
      title='Read one customer',
      group='Intake', agent_types=('support',), icon='fa-address-card',
      reads_only=True, capability='Read a customer with their ticket history',
      parameters=_schema({
          'customer_id': _number('Numeric customer id from support.list_customers.'),
      }, required=('customer_id',)))
def get_customer(ctx, customer_id):
    """Read one customer with their whole ticket history.

    The history is the part that changes a reply. Somebody on their fourth
    ticket about the same thing should not be told what they were told three
    times already, and the only way to know that is to look.
    """
    customer = _customer(customer_id)
    if customer is None:
        return _no_customer(customer_id)

    tickets = list(customer.tickets.all()[:30])
    lines = [
        f'Customer id {customer.pk}: {customer}',
        f'{customer.get_tier_display()} tier'
        + (' (priority)' if customer.is_priority else '')
        + f', {customer.email or "no email"}'
        + (f', {customer.phone}' if customer.phone else '')
        + f', timezone {customer.timezone_name or "unknown"}.',
        f'Account reference: {customer.account_reference or "none on file"}.',
        f'{len(tickets)} ticket(s) on record, {customer.open_ticket_count} open.',
    ]
    if customer.notes:
        lines.append(f'Notes: {customer.notes[:500]}')
    if tickets:
        lines.append('')
        lines.append('Ticket history, newest first:')
        for ticket in tickets:
            lines.append(
                f'- id {ticket.pk} {ticket.reference} '
                f'[{ticket.get_priority_display()}] {ticket.get_status_display()} '
                f'/ {ticket.get_category_display()} -- {ticket.subject[:80]} '
                f'({ticket.created_at:%d %b %Y})'
                + (' NEEDS A HUMAN' if ticket.needs_human else ''))
        repeats = {}
        for ticket in tickets:
            repeats[ticket.category] = repeats.get(ticket.category, 0) + 1
        worst = max(repeats.items(), key=lambda pair: pair[1])
        if worst[1] >= 3:
            lines.append(f'Pattern: {worst[1]} of their tickets are {worst[0]}. '
                         f'Treat a repeat as an unresolved problem, not a new one.')
    else:
        lines.append('No tickets on record for this customer yet.')

    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'customer_id': customer.pk, 'tier': customer.tier,
                            'ticket_count': len(tickets),
                            'open_tickets': customer.open_ticket_count},
                      subject_label='marketing.customer', subject_id=customer.pk)


# ===========================================================================
# GROUP: TRIAGE
# ===========================================================================

@tool(name='support.categorise_ticket',
      title='Categorise a ticket',
      group='Triage', agent_types=('support',), icon='fa-tags',
      capability='Classify a request into the support category set',
      parameters=_schema({
          'ticket_id': _number('Ticket to classify and update.'),
          'text': _text('Classify this text instead, without touching a ticket.'),
      }))
def categorise_ticket(ctx, ticket_id=None, text=''):
    """Classify a request into one category, and report the words that decided it.

    Pass a ticket_id to classify and save, or text to classify without
    recording anything. The classification is keyword based and the matched
    phrases come back with it, so a wrong category can be argued with rather
    than merely disagreed with.

    Multi-word phrases count double. 'money back' is strong evidence of a
    refund request; the word 'back' on its own is not evidence of anything.
    """
    from ..models_support import SupportTicket

    ticket = None
    if ticket_id not in (None, '', 0):
        ticket = _ticket(ticket_id)
        if ticket is None:
            return _no_ticket(ticket_id)
        source = _combined_text(ticket)
    elif text:
        source = str(text)
    else:
        return ToolResult(
            ok=False, error='Nothing to classify.',
            text='Give either a ticket_id or some text to classify.')

    category, evidence, ranking = categorise(source)
    labels = dict(SupportTicket.CATEGORY_CHOICES)

    lines = [f'Category: {labels.get(category, category)}.']
    if evidence:
        lines.append(f'Evidence: {", ".join(repr(item) for item in evidence[:8])}.')
    else:
        lines.append('No category keyword matched, so it falls to Other. '
                     'Read the request yourself and say what it is actually about.')
    if len(ranking) > 1:
        runners = ', '.join(f'{labels.get(name, name)} ({score})'
                            for name, score in ranking[1:4])
        lines.append(f'Also matched, with lower scores: {runners}.')

    if ticket is not None:
        was = ticket.category
        ticket.category = category
        ticket.save(update_fields=['category', 'updated_at'])
        lines.insert(0, f'{ticket.reference} (id {ticket.pk}) categorised.')
        if was != category:
            lines.append(f'Changed from {labels.get(was, was)}.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'category': category, 'evidence': evidence, 'ranking': ranking,
              'ticket_id': getattr(ticket, 'pk', None)},
        subject_label='marketing.supportticket' if ticket else '',
        subject_id=getattr(ticket, 'pk', 0) or 0)


@tool(name='support.set_ticket_priority',
      title='Set or derive a ticket priority',
      group='Triage', agent_types=('support',), icon='fa-arrow-up-right-dots',
      capability='Prioritise a ticket from impact rather than tone',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'priority': _text('Leave empty to derive it from the message.',
                            PRIORITY_ORDER),
          'reason': _text('Why, when you are setting it by hand.'),
      }, required=('ticket_id',)))
def set_ticket_priority(ctx, ticket_id, priority='', reason=''):
    """Prioritise a ticket from impact, and record the phrases that decided it.

    Leave ``priority`` empty and the rules decide. They are ordered by
    consequence, not by volume: data loss, a security or privacy exposure, a
    named legal or regulatory route, a welfare signal, a safety issue and a
    full outage are urgent; a double charge, a customer blocked from working, a
    named deadline and revenue at risk are high. A premium or enterprise
    contract raises a ticket one level, because that is a contractual fact.

    Profanity, capital letters and the word 'urgent' with no stated consequence
    are detected, reported and given no weight at all. A calm message about
    lost data outranks an angry one about a slow reply, and answering the
    loudest customer first is the failure this tool exists to prevent.

    The driving phrases are written to the ticket's urgency_signals, so 'why is
    this urgent' can always be answered in the customer's own words.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    source = _combined_text(ticket)
    verdict = derive_priority(source, customer=ticket.customer,
                              category=ticket.category)

    if priority:
        if priority not in PRIORITY_RANK:
            return ToolResult(
                ok=False, error='Unknown priority.',
                text=f'Priority must be one of {", ".join(PRIORITY_ORDER)}.')
        chosen = priority
        rule = (f'set by instruction to {priority}'
                + (f': {reason}' if reason else '')
                + f'. The rules would have said {verdict["priority"]} because '
                  f'{verdict["rule"]}')
    else:
        chosen = verdict['priority']
        rule = verdict['rule']

    ticket.priority = chosen
    ticket.urgency_signals = list(verdict['signals'])
    hours = SLA_DEFAULT_HOURS.get(chosen, 24)
    ticket.sla_due_at = (ticket.created_at or timezone.now()) + timedelta(hours=hours)
    ticket.save(update_fields=['priority', 'urgency_signals', 'sla_due_at',
                               'updated_at'])

    lines = [f'{ticket.reference} (id {ticket.pk}) is now '
             f'{ticket.get_priority_display()}.',
             f'Why: {rule}.']
    if verdict['signals']:
        lines.append(f'Recorded in urgency_signals: '
                     f'{", ".join(repr(item) for item in verdict["signals"])}.')
    else:
        lines.append('urgency_signals is empty because no impact phrase was found.')
    if verdict['tone']:
        lines.append(f'Tone markers found and given no weight: '
                     f'{", ".join(verdict["tone"][:5])}. Priority follows impact.')
    if len(verdict['fired']) > 1:
        others = ', '.join(f'{entry["rule"]} ({entry["priority"]})'
                           for entry in verdict['fired'][1:4])
        lines.append(f'Other rules that also fired: {others}.')
    lines.append(f'Response target moved to {ticket.sla_due_at:%d %b %Y at %H:%M} '
                 f'({hours} hours from arrival).')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'priority': chosen,
              'signals': ticket.urgency_signals, 'rule': rule,
              'tone_ignored': verdict['tone']},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.analyse_sentiment',
      title='Analyse customer sentiment',
      group='Triage', agent_types=('support',), icon='fa-face-frown',
      capability='Judge how a customer feels, with the evidence',
      parameters=_schema({
          'ticket_id': _number('Ticket to analyse and update.'),
          'text': _text('Analyse this text instead, without touching a ticket.'),
      }))
def analyse_sentiment(ctx, ticket_id=None, text=''):
    """Judge the customer's tone as positive, neutral, negative or angry.

    Sentiment changes how a reply is written. It does not change the priority,
    and this tool does not touch the priority, because tone and impact are
    different questions and mixing them is how a swearing customer with a
    trivial problem overtakes a polite one with a serious one.

    The deciding phrases come back so the judgement can be checked.
    """
    from ..models_support import SupportTicket

    ticket = None
    if ticket_id not in (None, '', 0):
        ticket = _ticket(ticket_id)
        if ticket is None:
            return _no_ticket(ticket_id)
        source = _combined_text(ticket)
    elif text:
        source = str(text)
    else:
        return ToolResult(
            ok=False, error='Nothing to analyse.',
            text='Give either a ticket_id or some text to analyse.')

    label, phrases = analyse_sentiment_text(source)
    labels = dict(SupportTicket.SENTIMENT_CHOICES)

    lines = [f'Sentiment: {labels.get(label, label)}.']
    if phrases:
        lines.append(f'Decided by: {", ".join(repr(item) for item in phrases[:8])}.')
    else:
        lines.append('Nothing signalled either way, so it reads as neutral.')
    if label == 'angry':
        lines.append('Write plainly, apologise once and specifically, and give a '
                     'concrete next step with a time. Do not match the tone and do '
                     'not raise the priority for it.')
    elif label == 'positive':
        lines.append('The customer is not upset. Do not open with an apology for '
                     'something they did not complain about.')

    if ticket is not None:
        ticket.sentiment = label
        ticket.save(update_fields=['sentiment', 'updated_at'])
        lines.insert(0, f'{ticket.reference} (id {ticket.pk}) sentiment recorded.')
        lines.append(f'Priority is unchanged at {ticket.get_priority_display()}; '
                     f'use support.set_ticket_priority if the impact has changed.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'sentiment': label, 'phrases': phrases,
              'ticket_id': getattr(ticket, 'pk', None)},
        subject_label='marketing.supportticket' if ticket else '',
        subject_id=getattr(ticket, 'pk', 0) or 0)


@tool(name='support.detect_urgent_complaints',
      title='Detect urgent complaints',
      group='Triage', agent_types=('support',), icon='fa-triangle-exclamation',
      reads_only=True, capability='Find the open tickets that genuinely cannot wait',
      parameters=_schema({'limit': _number('How many to report. Default 15.')}))
def detect_urgent_complaints(ctx, limit=15):
    """Find the open tickets that genuinely cannot wait, and say what makes each urgent.

    Genuine is the operative word. This reports tickets whose stored urgency
    signals name a real consequence, tickets flagged for a human, and tickets
    whose response target has already passed. It does not report tickets that
    merely sound cross, and it says so when a ticket sounds cross but has no
    impact signal, because that is a judgement somebody may want to overturn
    with information the message did not contain.
    """
    from ..models_support import OPEN_STATUSES, SupportTicket

    try:
        count = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        count = 15

    open_tickets = list(
        SupportTicket.objects.filter(status__in=OPEN_STATUSES)
        .select_related('customer')[:200])

    scored = []
    loud_only = []
    for ticket in open_tickets:
        reasons = []
        weight = PRIORITY_RANK.get(ticket.priority, 1)
        if ticket.needs_human:
            reasons.append(f'flagged as needing a person: '
                           f'{ticket.escalation_reason or "no reason recorded"}')
            weight += 10
        if ticket.urgency_signals:
            reasons.append('impact signals '
                           + ', '.join(repr(str(item))
                                       for item in ticket.urgency_signals[:4]))
            weight += 4
        if ticket.is_breaching_sla:
            reasons.append(f'response target passed '
                           f'{ticket.sla_due_at:%d %b %H:%M}')
            weight += 3
        if ticket.priority in ('urgent', 'high'):
            reasons.append(f'priority {ticket.get_priority_display()}')
        if ticket.customer_id and ticket.customer and ticket.customer.is_priority:
            reasons.append(f'{ticket.customer.get_tier_display()} customer')
            weight += 1
        if ticket.reopened_count:
            reasons.append(f'reopened {ticket.reopened_count} time(s)')
            weight += 2

        # Stored signals are evidence for the reasons list, not a qualifier on
        # their own: every triaged ticket carries the phrases that decided its
        # priority, including the ones that decided it was routine. Treating
        # their presence as urgency reported the whole queue as urgent.
        if (ticket.needs_human or ticket.is_breaching_sla
                or ticket.priority in ('urgent', 'high')):
            scored.append((weight, ticket, reasons))
        elif ticket.sentiment == 'angry':
            loud_only.append(ticket)

    scored.sort(key=lambda entry: entry[0], reverse=True)
    top = scored[:count]

    if not top:
        lines = [f'Nothing urgent among {len(open_tickets)} open ticket(s). '
                 f'No impact signals, no breached targets, nothing flagged for a person.']
    else:
        lines = [f'{len(top)} of {len(open_tickets)} open ticket(s) cannot wait (urgent or high priority, past the response target, or flagged for a person):']
        for _weight, ticket, reasons in top:
            lines.append(
                f'- id {ticket.pk} {ticket.reference} '
                f'[{ticket.get_priority_display()}] {ticket.subject[:80]} '
                f'({ticket.customer_label}, open {ticket.age_label})')
            lines.append(f'  Urgent because: {"; ".join(reasons)}.')

    if loud_only:
        lines.append('')
        lines.append(
            f'{len(loud_only)} angry ticket(s) are NOT in the list above because '
            f'nothing in them names a consequence: '
            + ', '.join(f'#{ticket.pk} {ticket.reference}'
                        for ticket in loud_only[:8])
            + '. They still deserve a good reply. If one of them describes real '
              'impact in words the rules missed, raise it with '
              'support.set_ticket_priority and say why.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'urgent_count': len(top), 'open_count': len(open_tickets),
              'angry_without_impact': [ticket.pk for ticket in loud_only],
              'tickets': [{'id': ticket.pk, 'reference': ticket.reference,
                           'priority': ticket.priority, 'reasons': reasons}
                          for _weight, ticket, reasons in top]})


@tool(name='support.flag_for_human',
      title='Flag a ticket for a human',
      group='Triage', agent_types=('support',), icon='fa-hand',
      risk='medium', capability='Mark a ticket that no AI may reply to',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'reason': _text('What makes this a person\'s job. Be specific.'),
      }, required=('ticket_id', 'reason')))
def flag_for_human(ctx, ticket_id, reason):
    """Mark a ticket that no AI may reply to, and escalate it.

    USE THIS IMMEDIATELY, BEFORE ANYTHING ELSE, for a data breach or any
    suspected exposure of personal data, a legal threat or any mention of a
    lawyer, a court, a tribunal, a regulator or an ombudsman, a safety issue or
    an injury, a vulnerable customer, a media or press enquiry, and anything
    involving self-harm or suicide.

    In those cases DO NOT draft a customer reply, do not suggest wording for
    one, and do not offer to write one. A fluent reply is not a safe reply
    here: the whole risk is that it reads well enough to be approved. Flag it,
    tell the team with support.notify_support_team, and stop.

    This sets needs_human on the ticket and moves it to escalated. Once set,
    support.draft_customer_reply refuses to write and support.send_customer_reply
    refuses even to prepare a proposal, so the flag holds after this
    conversation ends.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    detected, detected_reason, phrases = needs_human_check(_combined_text(ticket))

    ticket.needs_human = True
    ticket.status = 'escalated'
    ticket.escalation_reason = str(reason)
    if not ticket.escalated_to:
        ticket.escalated_to = 'a human support colleague'
    ticket.save(update_fields=['needs_human', 'status', 'escalation_reason',
                               'escalated_to', 'updated_at'])

    from ..models_support import TicketMessage
    TicketMessage.objects.create(
        ticket=ticket, direction='internal', author_label=_agent_label(ctx),
        body=f'Flagged for a human. Reason: {reason}')

    lines = [
        f'{ticket.reference} (id {ticket.pk}) is flagged needs_human and moved to '
        f'Escalated.',
        f'Reason recorded: {reason}',
        'No AI-drafted reply can be written or sent for this ticket from now on. '
        'support.draft_customer_reply will refuse, and support.send_customer_reply '
        'will refuse to prepare a proposal.',
    ]
    if detected:
        lines.append(f'The rules independently agree: {detected_reason} '
                     f'({", ".join(phrases[:4])}).')
    lines.append('Next: support.notify_support_team so a person picks it up, and '
                 'support.escalate_to_jira if it needs a tracked record. Do not '
                 'write customer-facing wording.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'needs_human': True,
              'reason': str(reason), 'rules_agree': detected},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.identify_customer_issue',
      title='Identify the real issue',
      group='Triage', agent_types=('support',), icon='fa-magnifying-glass',
      reads_only=True,
      capability='Separate what the customer wants from what they said',
      parameters=_schema({'ticket_id': TICKET_ID}, required=('ticket_id',)))
def identify_customer_issue(ctx, ticket_id):
    """Work out what the customer actually wants, as distinct from what they wrote.

    The two come apart constantly. "Cancel my subscription" is often "stop
    charging me for a thing that has never worked", and answering the sentence
    rather than the want is how a cancellation becomes a complaint. This tool
    reports the stated request, the underlying want implied by the category and
    the impact signals, and the single missing detail that is blocking a
    resolution -- with the one question that would get it.

    Ask that one question. Asking three questions at once reads as a
    stalling tactic and usually gets one answer.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    source = _combined_text(ticket)
    references = _references(source)
    category = ticket.category

    stated = ' '.join((ticket.subject or '').split())
    first_line = next((' '.join(line.split())
                       for line in (ticket.body or '').splitlines()
                       if line.strip()), '')

    wants = {
        'refund': 'their money returned, and to stop being charged again',
        'billing': 'the charge corrected and an explanation of how it happened',
        'order': 'to know where their order is, or to change it',
        'delivery': 'the goods, or a date they can rely on',
        'technical': 'the thing to work, today, without a workaround they have to remember',
        'account': 'access, or control over their own data',
        'feature_request': 'to achieve something the product does not do yet',
        'complaint': 'to be taken seriously and told what will change',
        'other': 'something the category rules could not identify',
    }

    missing_label, question = UNBLOCKING_QUESTION.get(
        category, UNBLOCKING_QUESTION['other'])
    reference_categories = ('refund', 'order', 'delivery', 'billing')
    have_reference = bool(references)
    blocked = not have_reference if category in reference_categories else False

    if category == 'technical':
        blocked = not re.search(r'error|exception|code\s*\d|\b\d{3}\b', source, re.I)
    if category == 'account':
        blocked = not EMAIL_PATTERN.search(source)

    lines = [
        f'{ticket.reference} (id {ticket.pk})',
        f'What they said: "{stated}"'
        + (f' -- opening line: "{first_line[:180]}"' if first_line else ''),
        f'What they want: {wants.get(category, wants["other"])}.',
        f'Category {ticket.get_category_display()}, priority '
        f'{ticket.get_priority_display()}, sentiment '
        f'{ticket.get_sentiment_display()}.',
    ]
    if ticket.urgency_signals:
        lines.append(f'What is at stake, in their words: '
                     f'{", ".join(repr(str(s)) for s in ticket.urgency_signals[:4])}.')
    if references:
        lines.append(f'References they supplied: {", ".join(sorted(references)[:6])}.')

    if ticket.customer_id and ticket.customer:
        history = ticket.customer.tickets.filter(category=category).exclude(pk=ticket.pk)
        repeat = history.count()
        if repeat:
            lines.append(f'This is their {repeat + 1}th {ticket.get_category_display()} '
                         f'ticket. Treat it as an unresolved problem rather than a '
                         f'new one, and do not repeat advice they have already had.')

    if blocked:
        lines.append('')
        lines.append(f'BLOCKED ON: {missing_label}.')
        lines.append(f'The one question to ask: "{question}"')
        lines.append('Ask only that. Everything else can wait until it is answered.')
    else:
        lines.append('')
        lines.append('Nothing essential is missing, so this can be resolved without '
                     'going back to the customer. Call support.suggest_resolution '
                     'for the policy-grounded answer.')

    if ticket.needs_human:
        lines.append('NEEDS A HUMAN. Do not draft a reply whatever this analysis says.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'category': category,
              'references': sorted(references), 'blocked': blocked,
              'question': question if blocked else '',
              'needs_human': ticket.needs_human},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


def _sla_targets():
    """Response targets, from the knowledge base if it has them.

    Returns (targets, source_note). The knowledge base wins because an SLA is a
    commitment somebody wrote down; the constants are a fallback that is always
    labelled as such, so a report never implies a contractual number it made up.
    """
    hits, problem = knowledge_hits('service level agreement response time SLA target',
                                   limit=4, doc_types=('policy',))
    if not hits:
        note = (f'No SLA document was available ({problem or "nothing matched"}), so '
                f'the platform defaults are used and are not a contractual commitment')
        return dict(SLA_DEFAULT_HOURS), note

    targets = {}
    source = ''
    for hit in hits:
        fields = _hit_fields(hit)
        text = fields['snippet'].lower()
        for level in PRIORITY_ORDER:
            if level in targets:
                continue
            match = re.search(
                level + r'[^.\n]{0,80}?(\d+)\s*(hour|hours|business hour|'
                        r'business hours|day|days)', text)
            if match:
                value = int(match.group(1))
                if 'day' in match.group(2):
                    value *= 24
                targets[level] = value
                source = fields['title']
    if not targets:
        note = (f'The SLA document "{_hit_fields(hits[0])["title"]}" was found but no '
                f'per-priority hour figure could be read from it, so the platform '
                f'defaults are used and are not a contractual commitment')
        return dict(SLA_DEFAULT_HOURS), note

    merged = dict(SLA_DEFAULT_HOURS)
    merged.update(targets)
    named = ', '.join(f'{level} {merged[level]}h' for level in PRIORITY_ORDER)
    return merged, f'Targets read from "{source}": {named}'


@tool(name='support.check_sla',
      title='Check SLA compliance',
      group='Triage', agent_types=('support',), icon='fa-stopwatch',
      reads_only=True, capability='Report which tickets are breaching their response target',
      parameters=_schema({
          'ticket_id': _number('One ticket, or leave empty for every open ticket.'),
      }))
def check_sla(ctx, ticket_id=None):
    """Report which tickets have breached their response target, or are about to.

    The targets come from the knowledge base's SLA policy document where one
    exists, and from platform defaults where it does not. The result always
    says which of the two it used, because quoting an invented response time
    back to a customer as though it were a commitment is a promise the company
    then has to keep.

    A ticket with no target on file gets one computed here, so the next check
    has something to measure against.
    """
    from ..models_support import OPEN_STATUSES, SupportTicket

    targets, note = _sla_targets()

    if ticket_id not in (None, '', 0):
        ticket = _ticket(ticket_id)
        if ticket is None:
            return _no_ticket(ticket_id)
        tickets = [ticket]
    else:
        tickets = list(SupportTicket.objects.filter(status__in=OPEN_STATUSES)
                       .select_related('customer')[:200])

    now = timezone.now()
    breaching, close, fine = [], [], []
    for ticket in tickets:
        hours = targets.get(ticket.priority, 24)
        due = ticket.sla_due_at or ((ticket.created_at or now) + timedelta(hours=hours))
        if not ticket.sla_due_at:
            ticket.sla_due_at = due
            ticket.save(update_fields=['sla_due_at', 'updated_at'])
        remaining = round((due - now).total_seconds() / 3600, 1)
        entry = (ticket, due, remaining, hours)
        if ticket.status in ('resolved', 'closed'):
            fine.append(entry)
        elif remaining < 0:
            breaching.append(entry)
        elif remaining <= max(1.0, hours * 0.25):
            close.append(entry)
        else:
            fine.append(entry)

    breaching.sort(key=lambda entry: entry[2])
    close.sort(key=lambda entry: entry[2])

    lines = [f'SLA check over {len(tickets)} ticket(s). {note}.']
    if breaching:
        lines.append('')
        lines.append(f'BREACHED ({len(breaching)}):')
        for ticket, due, remaining, hours in breaching[:20]:
            lines.append(
                f'- id {ticket.pk} {ticket.reference} '
                f'[{ticket.get_priority_display()}, {hours}h target] '
                f'{abs(remaining)}h over, due {due:%d %b %H:%M} -- '
                f'{ticket.subject[:70]} ({ticket.customer_label})')
    if close:
        lines.append('')
        lines.append(f'DUE SOON ({len(close)}):')
        for ticket, due, remaining, hours in close[:20]:
            lines.append(
                f'- id {ticket.pk} {ticket.reference} '
                f'[{ticket.get_priority_display()}] {remaining}h left, due '
                f'{due:%d %b %H:%M} -- {ticket.subject[:70]}')
    if not breaching and not close:
        lines.append('Nothing is breaching and nothing is inside the final quarter '
                     'of its window.')
    else:
        lines.append('')
        lines.append('Deal with the breached ones first, oldest breach first. A first '
                     'response counts, so support.send_customer_reply on a breached '
                     'ticket is worth more than a perfect answer tomorrow.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'targets': targets, 'source': note,
              'breached': [ticket.pk for ticket, _d, _r, _h in breaching],
              'due_soon': [ticket.pk for ticket, _d, _r, _h in close],
              'checked': len(tickets)})


# ===========================================================================
# GROUP: CUSTOMER REPLIES
# ===========================================================================

@tool(name='support.suggest_resolution',
      title='Suggest a resolution',
      group='Customer Replies', agent_types=('support',), icon='fa-lightbulb',
      integration='knowledge_base', reads_only=True,
      capability='Propose a resolution grounded in company policy',
      parameters=_schema({'ticket_id': TICKET_ID}, required=('ticket_id',)))
def suggest_resolution(ctx, ticket_id):
    """Propose how to resolve a ticket, grounded in a policy it actually found.

    This tool searches the knowledge base first and reports the documents it
    rests on. That order is not a formality. A resolution improvised from
    general knowledge sounds identical to one taken from the refund policy, and
    the difference only becomes visible when a customer quotes it back and the
    company has to honour a window nobody agreed to.

    When no policy covers the request the tool says so and recommends
    escalation. Take that recommendation. "No policy covers this, so a person
    should decide" is a correct answer and an invented concession is not.

    The result also lists what the reply must not promise, which is the part
    most worth reading twice.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    query = ' '.join(filter(None, [
        ticket.get_category_display(), ticket.subject,
        ' '.join(sorted(_terms(ticket.body))[:8])]))
    hits, problem = knowledge_hits(query, limit=5)

    forbidden = MUST_NOT_PROMISE.get(ticket.category, MUST_NOT_PROMISE['other'])
    lines = [f'{ticket.reference} (id {ticket.pk}) -- {ticket.subject}',
             f'Category {ticket.get_category_display()}, priority '
             f'{ticket.get_priority_display()}.']

    if ticket.needs_human:
        return _human_only_result(ticket, verb='suggest a customer-facing resolution for')

    if not hits:
        reason = problem or 'nothing in the knowledge base matched this request'
        lines += [
            '',
            f'NO POLICY FOUND: {reason}.',
            f'I searched for: "{query[:200]}".',
            'Recommendation: do not improvise. Escalate this instead, with '
            'support.escalate_to_jira for a tracked record or '
            'support.notify_support_team to get a person to decide. If a policy '
            'exists but is not in the knowledge base, say which document should '
            'be added -- that is more useful than a guess.',
            f'What must not be promised in any case: {forbidden}.',
        ]
        return ToolResult(
            ok=True, text='\n'.join(lines),
            data={'ticket_id': ticket.pk, 'grounded': False, 'query': query,
                  'recommend': 'escalate'},
            subject_label='marketing.supportticket', subject_id=ticket.pk)

    quotes = _quote_lines(hits, limit=MAX_DRAFT_QUOTES)
    top = _hit_fields(hits[0])

    steps = {
        'refund': ('Confirm the order or invoice, check it against the refund '
                   'window in the policy quoted below, then either process the '
                   'refund or explain plainly why the policy does not cover it.'),
        'billing': ('Identify the charge, compare it with what the billing policy '
                    'says should have happened, correct the difference and say how '
                    'it occurred.'),
        'order': ('Look up the order, tell them its actual state and the next '
                  'thing that will happen to it.'),
        'delivery': ('Give them the tracking state and the courier, and the date '
                     'the courier is committing to rather than one of ours.'),
        'technical': ('Give the workaround from the troubleshooting document if '
                      'there is one, then file the defect so it is fixed rather '
                      'than only worked around.'),
        'account': ('Verify they are the account holder by the method the account '
                    'policy specifies, then make the change.'),
        'feature_request': ('Say how the outcome is achieved today, record the '
                            'request, and do not imply it is planned.'),
        'complaint': ('Say what happened, what is being changed, and when they '
                      'will see it. One specific apology.'),
        'other': 'Answer from the document below, and only from it.',
    }

    lines += [
        '',
        f'Suggested resolution: {steps.get(ticket.category, steps["other"])}',
        '',
        f'The policy this rests on -- {top["title"]}'
        + (f' ({top["url"]})' if top['url'] else '') + ':',
    ]
    lines += quotes
    lines += [
        '',
        f'MUST NOT PROMISE: {forbidden}. Anything beyond what the quotes above '
        f'actually say needs a person to decide it, not a better sentence.',
        '',
        'Next: support.draft_customer_reply to write it, which quotes this policy '
        'again, then support.send_customer_reply to put it in the approval queue.',
    ]

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'grounded': True, 'query': query,
              'policy_title': top['title'], 'policy_url': top['url'],
              'must_not_promise': forbidden,
              'sources': [_hit_fields(hit) for hit in hits]},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.search_policies',
      title='Search policies and FAQs',
      group='Customer Replies', agent_types=('support',), icon='fa-book',
      integration='knowledge_base', reads_only=True,
      capability='Find the policy that governs a customer question',
      parameters=_schema({
          'query': _text('What to look for, in the words the policy would use.'),
          'limit': _number('How many results. Default 5.'),
      }, required=('query',)))
def search_policies(ctx, query, limit=5):
    """Search only the policy, FAQ and troubleshooting documents, and quote them.

    Narrowed to those three document types on purpose. A marketing page and a
    meeting note both mention refunds, and neither is a policy; quoting one to
    a customer as though it were is how the company acquires a commitment it
    never made.

    Quote what comes back rather than paraphrasing it. The exact window, the
    exact processing time and the exact exclusion are the parts that matter.
    """
    hits, problem = knowledge_hits(query, limit=limit, doc_types=POLICY_DOC_TYPES)

    if hits is None:
        return ToolResult(
            ok=False, error=problem,
            text=(f'The policy search could not run because {problem}. Do not '
                  f'answer the customer from memory. Escalate with '
                  f'support.notify_support_team, or ask the Knowledge and Research '
                  f'employee.'))
    if not hits:
        return ToolResult(
            ok=True,
            text=(f'No policy, FAQ or troubleshooting document matches "{query}". '
                  f'That is a real answer: say the company has no published policy '
                  f'on this and escalate rather than improvising one. It is also '
                  f'worth naming the document that should exist.'),
            data={'query': query, 'count': 0, 'grounded': False})

    lines = [f'{len(hits)} policy result(s) for "{query}":']
    for hit in hits:
        fields = _hit_fields(hit)
        lines.append(f'- {fields["title"]} [{fields["doc_type"] or "document"}]'
                     + (f' {fields["url"]}' if fields['url'] else ''))
        lines.append(f'  "{fields["snippet"][:300]}"')
    lines.append('Quote these words, not a paraphrase of them.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'query': query, 'count': len(hits), 'grounded': True,
              'results': [_hit_fields(hit) for hit in hits]})


@tool(name='support.answer_faq',
      title='Answer a common question',
      group='Customer Replies', agent_types=('support',), icon='fa-circle-question',
      integration='knowledge_base', reads_only=True,
      capability='Answer a question from the knowledge base, with the source named',
      parameters=_schema({
          'question': _text("The customer's question."),
      }, required=('question',)))
def answer_faq(ctx, question):
    """Answer a question from the knowledge base and name the document it came from.

    The naming is the useful part. An answer with a source can be checked,
    corrected and reused; an answer without one has to be trusted, and support
    answers get repeated hundreds of times before anybody notices they were
    wrong.

    If nothing matches, this says so. Pass that on to the customer honestly
    rather than filling the gap.
    """
    hits, problem = knowledge_hits(question, limit=4,
                                   doc_types=POLICY_DOC_TYPES)
    if hits is None:
        return ToolResult(
            ok=False, error=problem,
            text=(f'I could not search the knowledge base because {problem}, so I '
                  f'have no grounded answer to "{question}". Say that plainly '
                  f'rather than answering from memory.'))
    if not hits:
        wider, _problem = knowledge_hits(question, limit=4)
        if wider:
            lines = [f'No policy or FAQ answers "{question}" directly, but these '
                     f'documents are related:']
            lines += _quote_lines(wider, limit=3)
            lines.append('These are not policy documents, so do not quote them to a '
                         'customer as though they were. Use them to work out who to ask.')
            return ToolResult(ok=True, text='\n'.join(lines),
                              data={'question': question, 'grounded': False,
                                    'related': [_hit_fields(hit) for hit in wider]})
        return ToolResult(
            ok=True,
            text=(f'The knowledge base has nothing on "{question}". Tell the '
                  f'customer we will confirm and come back to them, escalate with '
                  f'support.notify_support_team, and name the document that ought '
                  f'to exist. Do not compose an answer.'),
            data={'question': question, 'grounded': False})

    top = _hit_fields(hits[0])
    lines = [f'Answer to "{question}", from {top["title"]}'
             + (f' ({top["url"]})' if top['url'] else '') + ':',
             f'"{top["snippet"][:600]}"']
    if len(hits) > 1:
        lines.append('')
        lines.append('Also relevant:')
        lines += _quote_lines(hits[1:], limit=2)
    lines.append('')
    lines.append(f'Name {top["title"]} when you use this with a customer, so they '
                 f'can read it themselves.')

    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'question': question, 'grounded': True,
                            'source': top['title'], 'source_url': top['url'],
                            'results': [_hit_fields(hit) for hit in hits]})


@tool(name='support.draft_customer_reply',
      title='Draft a customer reply',
      group='Customer Replies', agent_types=('support',), icon='fa-pen',
      capability='Write a reply to the customer, for review',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'tone': _text('How to pitch it.',
                        ('warm', 'neutral', 'formal', 'apologetic', 'brief')),
          'include_policy': _flag('Quote the governing policy. Keep this true.'),
          'notes': _text('Facts to include that are not on the ticket.'),
      }, required=('ticket_id',)))
def draft_customer_reply(ctx, ticket_id, tone='warm', include_policy=True, notes=''):
    """Write a reply to the customer and save it on the ticket as a draft.

    This does NOT send anything. The draft is recorded as a TicketMessage
    marked is_ai_draft, so it can be read, corrected and quoted before it is
    anything else. Sending is support.send_customer_reply, which is a separate
    approval-required tool, and that separation is deliberate: writing is work,
    sending is a decision.

    The draft quotes the policy it found when include_policy is true. Leave it
    true unless a person has told you the reply needs no policy, because a
    reply with a quoted source is one a colleague can check in ten seconds.

    This tool REFUSES when the ticket is flagged needs_human -- a data breach, a
    legal threat, a safety issue, a vulnerable customer, a media enquiry, or
    anything involving self-harm. In those cases do not write wording of any
    kind, not even as a suggestion in chat.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)
    if ticket.needs_human:
        return _human_only_result(ticket)

    opener = TONE_OPENERS.get(tone, TONE_OPENERS['warm'])
    if ticket.sentiment == 'positive' and tone == 'warm':
        opener = TONE_OPENERS['neutral']

    hits, problem = ([], '')
    grounded = False
    policy_lines = []
    if include_policy:
        query = f'{ticket.get_category_display()} {ticket.subject}'
        hits, problem = knowledge_hits(query, limit=4, doc_types=POLICY_DOC_TYPES)
        policy_lines, grounded = _policy_block(hits, problem, ticket.category)

    body_parts = [_greeting(ticket), '', opener, '']

    summary = ' '.join((ticket.subject or '').split())
    body_parts.append(
        f'About your message regarding {summary.lower() or "your request"}: '
        f'here is where it stands.')
    if notes:
        body_parts += ['', str(notes).strip()]
    if include_policy and grounded:
        body_parts += ['']
        body_parts += [line for line in policy_lines]
    body_parts += ['', _next_step_line(ticket)]
    body_parts += ['', f'Your reference is {ticket.reference} if you need to reply.']
    body_parts += ['', _signature(ctx)]

    body = '\n'.join(body_parts)
    draft = _save_draft(ctx, ticket, body)

    forbidden = MUST_NOT_PROMISE.get(ticket.category, MUST_NOT_PROMISE['other'])
    lines = [
        f'Drafted a reply for {ticket.reference} (id {ticket.pk}) and saved it on '
        f'the ticket as draft message #{draft.pk}. Nothing has been sent.',
        '',
        body,
        '',
        f'Grounded in policy: {"yes" if grounded else "NO"}.',
    ]
    if include_policy and not grounded:
        lines.append(f'No policy was quoted because {problem or "nothing matched"}. '
                     f'Do not add a commitment to fill the gap. Either call '
                     f'support.search_policies with better words, or escalate.')
    lines += [
        f'This draft must not promise {forbidden}. Read it again against that.',
        'To send it, call support.send_customer_reply with this ticket_id and leave '
        'the body empty -- it will pick up this draft and put it in the approval '
        'queue for a person.',
    ]

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'draft_id': draft.pk, 'body': body,
              'grounded': grounded, 'tone': tone,
              'must_not_promise': forbidden},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.draft_apology',
      title='Draft an apology',
      group='Customer Replies', agent_types=('support',), icon='fa-heart-crack',
      capability='Apologise once, specifically, without grovelling',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'what_went_wrong': _text('The specific thing that went wrong.'),
          'remedy': _text('What is being done about it, if anything is agreed.'),
      }, required=('ticket_id',)))
def draft_apology(ctx, ticket_id, what_went_wrong='', remedy=''):
    """Draft an apology: once, specifically, no grovelling, no blame.

    Those three constraints are the whole craft of it. Apologising repeatedly
    reads as a script rather than as regret. Apologising vaguely ("for any
    inconvenience caused") tells the customer nobody has read their message.
    Apologising while explaining that the customer misunderstood is not an
    apology. And an apology that promises compensation nobody has authorised
    creates a second problem on top of the first.

    So: one sentence naming the actual failure, one sentence on what is being
    done, one on when they will hear next. The draft is saved on the ticket and
    nothing is sent.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)
    if ticket.needs_human:
        return _human_only_result(ticket, verb='draft an apology for')

    failure = str(what_went_wrong or '').strip()
    if not failure:
        failure = (f'we did not handle your {ticket.get_category_display().lower()} '
                   f'request the way we should have')
    failure = failure.rstrip('.')

    body_parts = [
        _greeting(ticket), '',
        f'I am sorry that {failure[0].lower() + failure[1:] if failure else failure}. '
        f'That is not the standard we hold ourselves to, and you were right to '
        f'tell us.', '',
    ]
    if remedy:
        body_parts += [f'What we are doing about it: {str(remedy).strip()}', '']
    else:
        body_parts += ['I am looking into exactly what happened so that I can tell '
                       'you what is being changed rather than just that it will be.', '']
    body_parts += [_next_step_line(ticket), '',
                   f'Your reference is {ticket.reference}.', '', _signature(ctx)]

    body = '\n'.join(body_parts)
    draft = _save_draft(ctx, ticket, body)

    return ToolResult(
        ok=True,
        text='\n'.join([
            f'Drafted an apology for {ticket.reference} (id {ticket.pk}), saved as '
            f'draft message #{draft.pk}. Nothing has been sent.',
            '', body, '',
            'Checked against the three rules: it apologises once, it names the '
            'specific failure, and it does not blame the customer.',
            'It also promises no compensation. Do not add any -- that needs a '
            'person, and support.notify_support_team is how you ask.',
            'To send it, call support.send_customer_reply with this ticket_id.']),
        data={'ticket_id': ticket.pk, 'draft_id': draft.pk, 'body': body},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


def _refund_window(hits):
    """The refund window and processing time actually stated in the policy.

    Read out of the document rather than assumed, because '14 days' and '30
    days' are both common and getting it wrong in writing is the company's
    problem rather than the customer's.
    """
    window = ''
    processing = ''
    for hit in (hits or []):
        text = _hit_fields(hit)['snippet']
        low = text.lower()
        if not window:
            match = re.search(
                r'(\d+)\s*(calendar |business |working )?days?'
                r'[^.]{0,40}(?:of|from|after|since)?[^.]{0,30}'
                r'(?:purchase|delivery|receipt|order|invoice)', low)
            if match:
                window = match.group(0).strip()
        if not processing:
            match = re.search(
                r'(?:process|processed|refunded|returned|credited)[^.]{0,40}?'
                r'(\d+)\s*(?:to\s*\d+\s*)?(calendar |business |working )?days?', low)
            if match:
                processing = match.group(0).strip()
    return window, processing


@tool(name='support.draft_refund_response',
      title='Draft a refund response',
      group='Customer Replies', agent_types=('support',), icon='fa-money-bill-transfer',
      integration='knowledge_base',
      capability='Answer a refund request from the refund policy',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'decision': _text('approve, decline, or leave empty to let the policy decide.',
                            ('approve', 'decline', 'partial')),
          'amount': _text('The amount, if one has been agreed.'),
          'currency': _text('Currency code. Default AUD.'),
      }, required=('ticket_id',)))
def draft_refund_response(ctx, ticket_id, decision='', amount='', currency='AUD'):
    """Draft a refund answer that quotes the actual refund policy.

    Refunds are where improvisation costs money. This tool searches the
    knowledge base for the refund policy and quotes the window and the
    processing time it finds there, verbatim. It does not assume fourteen days,
    or thirty, or any figure at all.

    When the policy does not support the refund, the draft says so plainly.
    That is the intended behaviour, not a limitation to work around: inventing a
    goodwill credit to soften a decline commits the company to something nobody
    approved, and the customer will quote it. If a concession is warranted, a
    person can add it in the approval queue where it is their decision.

    When no refund policy can be found at all, no draft is written.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)
    if ticket.needs_human:
        return _human_only_result(ticket, verb='draft a refund response for')

    hits, problem = knowledge_hits(
        'refund policy return window processing time eligibility',
        limit=5, doc_types=POLICY_DOC_TYPES)

    if not hits:
        reason = problem or 'no refund policy is in the knowledge base'
        return ToolResult(
            ok=False, error='No refund policy found.',
            text=(f'I will not draft a refund response for {ticket.reference} '
                  f'because {reason}. A refund answer that does not quote a policy '
                  f'is an invented commitment, and the customer will hold the '
                  f'company to it. Escalate with support.notify_support_team or '
                  f'support.escalate_to_jira so a person decides, and say which '
                  f'document should be added to the knowledge base.'),
            data={'ticket_id': ticket.pk, 'grounded': False},
            subject_label='marketing.supportticket', subject_id=ticket.pk)

    top = _hit_fields(hits[0])
    window, processing = _refund_window(hits)
    references = sorted(_references(_combined_text(ticket)))

    verdict = str(decision or '').strip().lower()
    if verdict not in ('approve', 'decline', 'partial'):
        verdict = 'undecided'

    body_parts = [_greeting(ticket), '',
                  'Thanks for getting in touch about a refund.', '']

    if references:
        body_parts += [f'I can see this relates to {", ".join(references[:3])}.', '']

    body_parts += [f'What our refund policy says, from {top["title"]}:']
    for hit in hits[:MAX_DRAFT_QUOTES]:
        body_parts.append(f'- {_hit_fields(hit)["snippet"][:280]}')
    body_parts.append('')

    money = f'{currency or "AUD"} {amount}'.strip() if amount else ''

    if verdict == 'approve':
        body_parts.append(
            f'Your request is within that policy, so we are refunding'
            + (f' {money}' if money else ' the amount you paid') + '.')
        if processing:
            body_parts.append(f'The policy states that refunds are {processing}, so '
                              f'that is the timeframe to expect.')
        else:
            body_parts.append('I have not quoted a processing time because our '
                              'policy does not state one; I will confirm it with '
                              'you rather than guess.')
    elif verdict == 'partial':
        body_parts.append(
            f'Under that policy we can refund'
            + (f' {money}' if money else ' part of what you paid')
            + ', and I have set out above the part of the policy that decides it.')
    elif verdict == 'decline':
        body_parts.append(
            'On that policy your request falls outside what we can refund, and I '
            'would rather tell you that plainly than leave it unclear.')
        body_parts.append(
            'I have not offered an alternative arrangement because that is not '
            'mine to offer. If you would like it reviewed, say so and I will put '
            'it to the team with everything you have told me.')
    else:
        body_parts.append(
            'I am checking your order against that policy now and will come back '
            'to you with a decision rather than an estimate.')
        if window:
            body_parts.append(f'The relevant window is {window}.')

    body_parts += ['', _next_step_line(ticket), '',
                   f'Your reference is {ticket.reference}.', '', _signature(ctx)]

    body = '\n'.join(body_parts)
    draft = _save_draft(ctx, ticket, body)

    lines = [
        f'Drafted a refund response for {ticket.reference} (id {ticket.pk}), saved '
        f'as draft message #{draft.pk}. Nothing has been sent.',
        '', body, '',
        f'Policy quoted: {top["title"]}'
        + (f' ({top["url"]})' if top['url'] else '') + '.',
        f'Refund window found in the policy: {window or "none stated, so none quoted"}.',
        f'Processing time found in the policy: '
        f'{processing or "none stated, so none quoted"}.',
        f'Decision: {verdict}.',
        'The draft promises no amount, window or processing time beyond the quotes '
        'above. If a concession is warranted, a person adds it in the approval '
        'queue where it is their decision.',
    ]

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'draft_id': draft.pk, 'body': body,
              'grounded': True, 'decision': verdict, 'window': window,
              'processing_time': processing, 'policy_title': top['title']},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.draft_order_status_response',
      title='Draft an order status response',
      group='Customer Replies', agent_types=('support',), icon='fa-box',
      capability='Tell a customer where their order is',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'order_reference': _text('The order reference. Leave empty to read it '
                                   'from the ticket.'),
          'status': _text('Where the order actually is.'),
          'expected_date': _text('The date the courier or warehouse is committing to.'),
      }, required=('ticket_id',)))
def draft_order_status_response(ctx, ticket_id, order_reference='', status='',
                                expected_date=''):
    """Draft an answer about where an order is.

    Only facts that were given get into the draft. If no status was supplied
    the draft says the order is being checked rather than inventing a stage,
    and if no date was supplied it promises no date at all. A delivery date
    that came from nowhere is the most common cause of a second, angrier
    ticket, and the courier does not know it was promised.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)
    if ticket.needs_human:
        return _human_only_result(ticket, verb='draft an order update for')

    reference = str(order_reference or '').strip()
    found = sorted(_references(_combined_text(ticket)))
    if not reference and found:
        reference = found[0]

    body_parts = [_greeting(ticket), '',
                  'Thanks for checking on your order.', '']

    if reference:
        body_parts.append(f'Order {reference}:')
    else:
        body_parts.append('I could not find an order reference in your message, so '
                          'I have not looked up the wrong order by guessing.')

    if status:
        body_parts.append(f'It is currently {str(status).strip()}.')
    else:
        body_parts.append('I am confirming its current stage with our warehouse now '
                          'rather than telling you something that may not be right.')

    if expected_date:
        body_parts.append(f'The date we have from the courier is '
                          f'{str(expected_date).strip()}. If that changes I will '
                          f'tell you rather than let you find out.')
    else:
        body_parts.append('I have deliberately not given you a delivery date, '
                          'because I do not have one I can stand behind yet.')

    if not reference:
        label, question = UNBLOCKING_QUESTION['order']
        body_parts += ['', question]

    body_parts += ['', _next_step_line(ticket), '',
                   f'Your reference is {ticket.reference}.', '', _signature(ctx)]

    body = '\n'.join(body_parts)
    draft = _save_draft(ctx, ticket, body)

    return ToolResult(
        ok=True,
        text='\n'.join([
            f'Drafted an order update for {ticket.reference} (id {ticket.pk}), saved '
            f'as draft message #{draft.pk}. Nothing has been sent.',
            '', body, '',
            f'Order reference used: {reference or "none -- the draft asks for it"}.',
            f'Status stated: {status or "none, so the draft says it is being checked"}.',
            f'Date promised: {expected_date or "none, deliberately"}.',
            'To send it, call support.send_customer_reply with this ticket_id.']),
        data={'ticket_id': ticket.pk, 'draft_id': draft.pk, 'body': body,
              'order_reference': reference},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


# ===========================================================================
# GROUP: TICKET MANAGEMENT
# ===========================================================================

@tool(name='support.assign_ticket',
      title='Assign a ticket',
      group='Ticket Management', agent_types=('support',), icon='fa-user-check',
      capability='Give a ticket an owner',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'assignee_username': _text('The username of the person who should own it.'),
          'assigned_agent_type': _text('The AI employee that should own it.',
                                       ('hr', 'engineering_manager', 'developer',
                                        'research', 'marketing', 'support')),
      }, required=('ticket_id',)))
def assign_ticket(ctx, ticket_id, assignee_username='', assigned_agent_type=''):
    """Give a ticket an owner, human or AI.

    An unassigned ticket is everybody's and therefore nobody's, which is how a
    ticket ages past its response target with three people having read it. Give
    it a name.
    """
    from django.contrib.auth.models import User

    from ..models import AIAgent

    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    changes = []
    fields = []

    if assignee_username:
        person = User.objects.filter(username=assignee_username).first()
        if person is None:
            available = ', '.join(
                User.objects.values_list('username', flat=True)[:15]) or 'none'
            return ToolResult(
                ok=False, error=f'No user "{assignee_username}".',
                text=(f'There is no user called "{assignee_username}". Known '
                      f'usernames: {available}. Nothing was changed.'))
        ticket.assignee = person
        fields.append('assignee')
        changes.append(f'assigned to {person.get_username()}')

    if assigned_agent_type:
        agent = AIAgent.objects.filter(agent_type=assigned_agent_type).first()
        if agent is None:
            return ToolResult(
                ok=False, error=f'No {assigned_agent_type} employee.',
                text=(f'There is no {assigned_agent_type} employee on the roster, '
                      f'so nothing was changed.'))
        ticket.assigned_agent = agent
        fields.append('assigned_agent')
        changes.append(f'handled by {agent.name}')

    if not changes:
        return ToolResult(
            ok=False, error='Nothing to assign.',
            text=('Give either assignee_username for a person or '
                  'assigned_agent_type for an AI employee.'))

    if ticket.status == 'new':
        ticket.status = 'open'
        fields.append('status')
        changes.append('status moved from New to Open')

    fields.append('updated_at')
    ticket.save(update_fields=fields)

    from ..models_support import TicketMessage
    TicketMessage.objects.create(
        ticket=ticket, direction='internal', author_label=_agent_label(ctx),
        body=f'Assignment changed: {"; ".join(changes)}.')

    note = ''
    if ticket.needs_human:
        note = (' This ticket is flagged needs_human, so the owner must write any '
                'customer reply themselves.')

    return ToolResult(
        ok=True,
        text=(f'{ticket.reference} (id {ticket.pk}): {"; ".join(changes)}.{note}'),
        data={'ticket_id': ticket.pk, 'changes': changes,
              'assignee': assignee_username, 'agent_type': assigned_agent_type},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.update_ticket_status',
      title='Update a ticket status',
      group='Ticket Management', agent_types=('support',), icon='fa-arrows-rotate',
      capability='Move a ticket through its workflow',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'status': _text('The new status.',
                          ('new', 'open', 'pending', 'waiting_customer',
                           'escalated', 'resolved', 'closed', 'reopened')),
          'note': _text('Why it moved. Recorded as an internal note.'),
      }, required=('ticket_id', 'status')))
def update_ticket_status(ctx, ticket_id, status, note=''):
    """Move a ticket through its workflow and keep the timestamps honest.

    The timestamps are the part that matters, because every report is computed
    from them. Moving to Waiting on the Customer or Resolved implies the
    customer has heard from us, so first_response_at is stamped if it is not
    already set. Resolved and Closed stamp resolved_at. Reopened clears
    resolved_at and increments reopened_count, so the reopen rate in a report
    is a real number rather than an impression.

    Do not use Resolved to mean "I have replied". Resolved means the customer's
    problem is over.
    """
    from ..models_support import SupportTicket, TicketMessage

    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    valid = dict(SupportTicket.STATUS_CHOICES)
    if status not in valid:
        return ToolResult(
            ok=False, error='Unknown status.',
            text=f'"{status}" is not a status. Use one of: {", ".join(valid)}.')

    was = ticket.status
    if was == status:
        return ToolResult(
            ok=True,
            text=(f'{ticket.reference} (id {ticket.pk}) is already '
                  f'{valid[status]}, so nothing changed.'),
            data={'ticket_id': ticket.pk, 'status': status, 'changed': False},
            subject_label='marketing.supportticket', subject_id=ticket.pk)

    now = timezone.now()
    ticket.status = status
    fields = ['status', 'updated_at']
    stamped = []

    if status in ('waiting_customer', 'resolved', 'closed') and not ticket.first_response_at:
        ticket.first_response_at = now
        fields.append('first_response_at')
        stamped.append('first_response_at set, because the customer has now heard '
                       'from us')

    if status in ('resolved', 'closed'):
        if not ticket.resolved_at:
            ticket.resolved_at = now
            fields.append('resolved_at')
            stamped.append(f'resolved_at set, {ticket.hours_open} hours after it '
                           f'arrived')
    elif status == 'reopened':
        ticket.reopened_count = (ticket.reopened_count or 0) + 1
        ticket.resolved_at = None
        fields += ['reopened_count', 'resolved_at']
        stamped.append(f'reopened_count is now {ticket.reopened_count} and '
                       f'resolved_at cleared')

    ticket.save(update_fields=fields)

    if note:
        TicketMessage.objects.create(
            ticket=ticket, direction='internal', author_label=_agent_label(ctx),
            body=f'Status {valid.get(was, was)} to {valid[status]}: {note}')

    lines = [f'{ticket.reference} (id {ticket.pk}) moved from '
             f'{valid.get(was, was)} to {valid[status]}.']
    lines += [f'Timestamp: {entry}.' for entry in stamped]
    if note:
        lines.append(f'Note recorded: {note}')
    if status in ('resolved', 'closed') and not ticket.resolution:
        lines.append('No resolution text is on file. Use support.close_ticket with '
                     'a resolution instead, so the next person reading this ticket '
                     'knows what was actually done.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'status': status, 'previous': was,
              'changed': True, 'reopened_count': ticket.reopened_count},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.close_ticket',
      title='Close a ticket',
      group='Ticket Management', agent_types=('support',), icon='fa-circle-check',
      capability='Close a ticket with a recorded resolution',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'resolution': _text('What was actually done, in enough detail to be '
                              'useful to whoever reads it next.'),
      }, required=('ticket_id', 'resolution')))
def close_ticket(ctx, ticket_id, resolution):
    """Close a ticket, recording what was actually done about it.

    The resolution is compulsory here for a reason: a ticket closed with no
    resolution is a ticket that will be reopened and answered from scratch, and
    the customer will have to explain it all again. Write what was done, not
    that it was handled.
    """
    from ..models_support import TicketMessage

    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    text = str(resolution or '').strip()
    if len(text) < 10:
        return ToolResult(
            ok=False, error='Resolution too thin.',
            text=(f'"{text}" is not a resolution anybody can use later. Say what '
                  f'was done, what the customer was told and anything still '
                  f'outstanding. {ticket.reference} was not closed.'))

    now = timezone.now()
    ticket.status = 'closed'
    ticket.resolution = text
    if not ticket.resolved_at:
        ticket.resolved_at = now
    if not ticket.first_response_at:
        ticket.first_response_at = now
    ticket.save(update_fields=['status', 'resolution', 'resolved_at',
                               'first_response_at', 'updated_at'])

    TicketMessage.objects.create(
        ticket=ticket, direction='internal', author_label=_agent_label(ctx),
        body=f'Closed. Resolution: {text}')

    warning = ''
    if ticket.needs_human:
        warning = (' This ticket was flagged needs_human. Confirm a person '
                   'actually handled it before treating the closure as sound.')

    return ToolResult(
        ok=True,
        text=(f'{ticket.reference} (id {ticket.pk}) closed after '
              f'{ticket.hours_open} hours.\nResolution recorded: {text}'
              f'{warning}'),
        data={'ticket_id': ticket.pk, 'status': 'closed',
              'hours_open': ticket.hours_open},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.reopen_ticket',
      title='Reopen a ticket',
      group='Ticket Management', agent_types=('support',), icon='fa-rotate-left',
      capability='Reopen a ticket that was not actually resolved',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'reason': _text('Why it is being reopened.'),
      }, required=('ticket_id', 'reason')))
def reopen_ticket(ctx, ticket_id, reason):
    """Reopen a ticket that was closed before the customer's problem was over.

    Reopening increments reopened_count, which is deliberately visible in
    reports. A rising reopen rate means tickets are being closed on a reply
    rather than on a resolution, and that is a management signal worth having
    rather than a statistic worth hiding.
    """
    from ..models_support import TicketMessage

    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    was = ticket.get_status_display()
    ticket.status = 'reopened'
    ticket.reopened_count = (ticket.reopened_count or 0) + 1
    ticket.resolved_at = None
    hours = SLA_DEFAULT_HOURS.get(ticket.priority, 24)
    ticket.sla_due_at = timezone.now() + timedelta(hours=hours)
    ticket.save(update_fields=['status', 'reopened_count', 'resolved_at',
                               'sla_due_at', 'updated_at'])

    TicketMessage.objects.create(
        ticket=ticket, direction='internal', author_label=_agent_label(ctx),
        body=f'Reopened from {was}. Reason: {reason}')

    return ToolResult(
        ok=True,
        text=(f'{ticket.reference} (id {ticket.pk}) reopened from {was}. '
              f'Reason: {reason}\n'
              f'This is reopen number {ticket.reopened_count}. A new response '
              f'target is set for {ticket.sla_due_at:%d %b %Y at %H:%M}.\n'
              f'Read the whole thread with support.get_ticket before replying, so '
              f'the customer is not told the same thing twice.'),
        data={'ticket_id': ticket.pk, 'status': 'reopened',
              'reopened_count': ticket.reopened_count},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.summarise_ticket',
      title='Summarise a ticket',
      group='Ticket Management', agent_types=('support',), icon='fa-align-left',
      reads_only=True, capability='Summarise a whole ticket in five lines',
      parameters=_schema({'ticket_id': TICKET_ID}, required=('ticket_id',)))
def summarise_ticket(ctx, ticket_id):
    """Summarise the whole thread in five lines: asked, done, standing, owed, by when.

    Five lines because that is what somebody picking the ticket up actually
    needs, and a longer summary gets skimmed instead of read. The fourth line
    is the one that prevents the common failure: what is owed to the customer
    that has not yet been given to them.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    messages = list(ticket.messages.all())
    inbound = [entry for entry in messages if entry.direction == 'inbound']
    outbound = [entry for entry in messages if entry.direction == 'outbound']
    sent = [entry for entry in outbound if entry.was_sent]
    drafts = [entry for entry in outbound if entry.is_ai_draft and not entry.was_sent]
    notes = [entry for entry in messages if entry.direction == 'internal']

    asked = ' '.join((ticket.subject or 'nothing recorded').split())
    if inbound:
        asked += f' -- "{inbound[0].preview}"'

    if sent:
        done = (f'{len(sent)} reply/replies sent, the last on '
                f'{sent[-1].created_at:%d %b at %H:%M}')
    elif drafts:
        done = (f'nothing sent yet; {len(drafts)} draft(s) are waiting, the newest '
                f'is message #{drafts[-1].pk}')
    else:
        done = 'nothing has been sent and nothing has been drafted'
    if notes:
        done += f'; {len(notes)} internal note(s) on file'
    if ticket.external_reference:
        done += f'; filed as {ticket.external_reference}'

    standing = (f'{ticket.get_status_display()}, priority '
                f'{ticket.get_priority_display()}, open {ticket.age_label}')
    if ticket.reopened_count:
        standing += f', reopened {ticket.reopened_count} time(s)'
    if ticket.duplicate_of_id:
        standing += f', duplicate of #{ticket.duplicate_of_id}'

    if ticket.needs_human:
        owed = ('a reply from a person. This ticket is flagged needs_human, so no '
                'AI-drafted reply may be sent')
    elif not ticket.first_response_at:
        owed = 'a first response -- the customer has not heard from us at all'
    elif drafts:
        owed = ('a decision on the waiting draft, then the reply itself; use '
                'support.send_customer_reply')
    elif ticket.status == 'waiting_customer':
        owed = 'nothing right now; we are waiting on the customer'
    elif ticket.status in ('resolved', 'closed'):
        owed = 'nothing; ' + (ticket.resolution[:160] if ticket.resolution
                              else 'though no resolution text was recorded')
    else:
        owed = 'the substantive answer'

    if ticket.status in ('resolved', 'closed'):
        by_when = f'closed {ticket.resolved_at:%d %b at %H:%M}' \
            if ticket.resolved_at else 'closed, no timestamp recorded'
    elif ticket.sla_due_at:
        by_when = (f'{"OVERDUE since" if ticket.is_breaching_sla else "due"} '
                   f'{ticket.sla_due_at:%d %b at %H:%M}')
    else:
        by_when = 'no response target on file; run support.check_sla'

    lines = [
        f'{ticket.reference} (id {ticket.pk}) -- {ticket.customer_label}',
        f'1. They asked: {asked}',
        f'2. We did: {done}',
        f'3. It stands: {standing}',
        f'4. We owe: {owed}',
        f'5. By when: {by_when}',
    ]

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'status': ticket.status,
              'owed': owed, 'by_when': by_when,
              'sent_count': len(sent), 'draft_count': len(drafts)},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.summarise_conversation',
      title='Summarise a conversation',
      group='Ticket Management', agent_types=('support',), icon='fa-comments',
      reads_only=True, capability='Condense a long customer thread',
      parameters=_schema({
          'ticket_id': _number('Summarise this ticket\'s thread.'),
          'text': _text('Or summarise this pasted conversation instead.'),
      }))
def summarise_conversation(ctx, ticket_id=None, text=''):
    """Condense a long customer thread into what a colleague needs to know.

    Reports who said what and in what order, what has changed since the first
    message, and the open questions nobody has answered. Long threads bury the
    important turn in the middle, and this pulls it out.
    """
    if ticket_id not in (None, '', 0):
        ticket = _ticket(ticket_id)
        if ticket is None:
            return _no_ticket(ticket_id)
        entries = list(ticket.messages.all())
        header = (f'{ticket.reference} (id {ticket.pk}), {len(entries)} message(s), '
                  f'{ticket.customer_label}')
        turns = [(entry.get_direction_display(),
                  entry.author_label or '', entry.preview,
                  f'{entry.created_at:%d %b %H:%M}') for entry in entries]
        source_text = _combined_text(ticket)
    elif text:
        ticket = None
        blocks = [' '.join(block.split()) for block in str(text).split('\n\n')
                  if block.strip()]
        header = f'Pasted conversation, {len(blocks)} block(s)'
        turns = [('block', '', block[:160], '') for block in blocks]
        source_text = str(text)
    else:
        return ToolResult(
            ok=False, error='Nothing to summarise.',
            text='Give either a ticket_id or the conversation text.')

    if not turns:
        return ToolResult(
            ok=True, text=f'{header}. There is nothing in the thread to summarise.',
            data={'turns': 0})

    questions = [' '.join(part.split())
                 for part in re.split(r'(?<=[.?!])\s+', source_text)
                 if part.strip().endswith('?')]
    references = sorted(_references(source_text))
    sentiment, phrases = analyse_sentiment_text(source_text)

    lines = [header, '', 'How it went:']
    for direction, author, preview, when in turns[:15]:
        lines.append(f'- {when} {direction} {author}: {preview}'.replace('  ', ' '))
    if len(turns) > 15:
        lines.append(f'- ... and {len(turns) - 15} more')

    lines += ['', f'Tone across the thread: {sentiment}'
                  + (f' ({", ".join(phrases[:4])})' if phrases else '') + '.']
    if references:
        lines.append(f'References mentioned: {", ".join(references[:6])}.')
    if questions:
        lines.append('')
        lines.append('Questions asked in the thread -- check each has an answer:')
        for question in questions[:6]:
            lines.append(f'- {question[:200]}')
    else:
        lines.append('No direct questions were asked in the thread.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'turns': len(turns), 'questions': questions[:6],
              'references': references[:6], 'sentiment': sentiment,
              'ticket_id': getattr(ticket, 'pk', None)},
        subject_label='marketing.supportticket' if ticket else '',
        subject_id=getattr(ticket, 'pk', 0) or 0)


@tool(name='support.add_internal_note',
      title='Add an internal note',
      group='Ticket Management', agent_types=('support',), icon='fa-note-sticky',
      capability='Record something on the ticket that the customer never sees',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'body': _text('The note. Written for a colleague, not the customer.'),
      }, required=('ticket_id', 'body')))
def add_internal_note(ctx, ticket_id, body):
    """Record something on the ticket that the customer never sees.

    This is where the reasoning goes: what was checked, what was ruled out, who
    was asked, why the priority was set the way it was. Without it the next
    person repeats the investigation, and the customer is asked the same
    question twice.

    Internal notes are never sent anywhere, so they are also the right place to
    record a concern about a ticket that needs a person.
    """
    from ..models_support import TicketMessage

    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    text = str(body or '').strip()
    if not text:
        return ToolResult(ok=False, error='Empty note.',
                          text='The note was empty, so nothing was recorded.')

    note = TicketMessage.objects.create(
        ticket=ticket, direction='internal', author_label=_agent_label(ctx),
        body=text)

    return ToolResult(
        ok=True,
        text=(f'Internal note #{note.pk} added to {ticket.reference} '
              f'(id {ticket.pk}). It is not visible to the customer and will not '
              f'be sent anywhere.\n{text[:400]}'),
        data={'ticket_id': ticket.pk, 'note_id': note.pk},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.tag_ticket',
      title='Tag a ticket',
      group='Ticket Management', agent_types=('support',), icon='fa-tag',
      capability='Label a ticket so it can be counted with others like it',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'tags': _text('Comma separated labels, e.g. "export-timeout, csv".'),
      }, required=('ticket_id', 'tags')))
def tag_ticket(ctx, ticket_id, tags):
    """Label a ticket so it can be counted alongside others like it.

    Tags are rows rather than free text, so the same label means the same thing
    everywhere. That is what turns seven tickets into one product signal
    instead of seven separately worded complaints. Use lowercase hyphenated
    labels and reuse an existing one wherever it fits, since a near-duplicate
    tag splits the count and hides the pattern.
    """
    from django.utils.text import slugify

    from ..models_support import TicketTag

    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    if isinstance(tags, (list, tuple)):
        wanted = [str(entry) for entry in tags]
    else:
        wanted = str(tags or '').replace(';', ',').split(',')

    created, attached, skipped = [], [], []
    for raw in wanted:
        name = slugify(str(raw).strip())[:60]
        if not name:
            skipped.append(str(raw).strip())
            continue
        tag, was_created = TicketTag.objects.get_or_create(name=name)
        ticket.tags.add(tag)
        attached.append(name)
        if was_created:
            created.append(name)

    if not attached:
        return ToolResult(
            ok=False, error='No usable tag.',
            text=(f'None of "{tags}" produced a usable tag name. Use lowercase '
                  f'words with hyphens, for example "export-timeout".'))

    existing = [name for name in attached if name not in created]
    lines = [f'{ticket.reference} (id {ticket.pk}) tagged: {", ".join(attached)}.']
    if created:
        lines.append(f'New tag(s) created: {", ".join(created)}.')
    if existing:
        lines.append(f'Reused existing tag(s): {", ".join(existing)}, so the counts '
                     f'stay together.')
    if skipped:
        lines.append(f'Skipped: {", ".join(skipped)}.')
    lines.append(f'All tags on this ticket: '
                 f'{", ".join(tag.name for tag in ticket.tags.all())}.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'ticket_id': ticket.pk, 'tags': attached, 'created': created},
        subject_label='marketing.supportticket', subject_id=ticket.pk)


@tool(name='support.detect_duplicate_tickets',
      title='Detect duplicate tickets',
      group='Ticket Management', agent_types=('support',), icon='fa-clone',
      capability='Find tickets that are really the same request',
      parameters=_schema({
          'ticket_id': _number('Compare this ticket against the others. Omit to '
                               'scan the open queue for pairs.'),
          'limit': _number('How many matches to report. Default 10.'),
      }))
def detect_duplicate_tickets(ctx, ticket_id=None, limit=10):
    """Find tickets that are genuinely the same request, and say how sure it is.

    Similarity here means shared distinctive terms, plus the same customer or
    the same order reference. Common words are stripped first, and fewer than
    two shared distinctive terms scores zero, because two tickets that both say
    "please help with my account" are not duplicates and merging them loses one
    customer's problem.

    A strong match writes duplicate_of onto the newer ticket. The score and the
    shared terms are always reported, so a wrong merge can be argued with. When
    the score is middling the tool says so and leaves both tickets alone --
    that is the safe default, since a missed duplicate costs a little duplicated
    work and a wrong merge costs a customer.
    """
    from ..models_support import OPEN_STATUSES, SupportTicket

    try:
        count = max(1, min(int(limit), 40))
    except (TypeError, ValueError):
        count = 10

    pool = list(SupportTicket.objects.filter(
        status__in=OPEN_STATUSES).select_related('customer')[:300])

    if ticket_id not in (None, '', 0):
        target = _ticket(ticket_id)
        if target is None:
            return _no_ticket(ticket_id)
        candidates = [row for row in
                      list(SupportTicket.objects.exclude(pk=target.pk)
                           .select_related('customer')[:300])]
        pairs = []
        for other in candidates:
            score, why = similarity(target, other)
            if score > 0:
                pairs.append((score, target, other, why))
        header = f'Duplicate check for {target.reference} (id {target.pk})'
    else:
        target = None
        pairs = []
        for index, first in enumerate(pool):
            for second in pool[index + 1:]:
                score, why = similarity(first, second)
                if score > 0:
                    pairs.append((score, first, second, why))
        header = f'Duplicate scan across {len(pool)} open ticket(s)'

    pairs.sort(key=lambda entry: entry[0], reverse=True)
    top = pairs[:count]

    if not top:
        return ToolResult(
            ok=True,
            text=(f'{header}: nothing shares enough distinctive wording to be a '
                  f'duplicate. Fewer than two shared distinctive terms scores '
                  f'zero, so near-identical boilerplate is correctly ignored.'),
            data={'matches': 0})

    lines = [f'{header}: {len(top)} candidate(s), strongest first.']
    marked = []
    for score, first, second, why in top:
        newer, older = ((first, second) if (first.created_at or timezone.now())
                        >= (second.created_at or timezone.now()) else (second, first))
        reasons = [f'shared terms {", ".join(why["shared_terms"][:6])}']
        if why['same_customer']:
            reasons.append('same customer')
        if why['shared_references']:
            reasons.append(f'same reference {", ".join(why["shared_references"])}')

        verdict = 'STRONG' if score >= DUPLICATE_STRONG else 'possible'
        lines.append(
            f'- {verdict} {score}: id {newer.pk} {newer.reference} '
            f'"{newer.subject[:60]}" looks like id {older.pk} {older.reference} '
            f'"{older.subject[:60]}" -- {"; ".join(reasons)}')

        if score >= DUPLICATE_STRONG and not newer.duplicate_of_id \
                and newer.pk != older.pk:
            newer.duplicate_of = older
            newer.save(update_fields=['duplicate_of', 'updated_at'])
            marked.append((newer.pk, older.pk, score))
            lines.append(f'  Marked id {newer.pk} as duplicate_of id {older.pk} '
                         f'(score {score}, threshold {DUPLICATE_STRONG}).')

    if not marked:
        lines.append('')
        lines.append(f'Nothing reached the {DUPLICATE_STRONG} threshold, so no '
                     f'ticket was marked. Leaving both open is the safe default: a '
                     f'missed duplicate costs a little repeated work, a wrong merge '
                     f'costs a customer.')
    else:
        lines.append('')
        lines.append('Reply on the older ticket and tell the customer the other '
                     'reference is being handled with it, rather than closing one '
                     'silently.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'matches': len(top), 'marked': marked,
              'threshold': DUPLICATE_STRONG,
              'pairs': [{'score': score, 'a': first.pk, 'b': second.pk,
                         'shared_terms': why['shared_terms']}
                        for score, first, second, why in top]},
        subject_label='marketing.supportticket' if target else '',
        subject_id=getattr(target, 'pk', 0) or 0)


# ===========================================================================
# GROUP: ANALYSIS
# ===========================================================================

def _signature_counts(tickets):
    """How many tickets each two-word signature appears in.

    Counted per ticket rather than per occurrence, so one customer writing
    'export timeout' nine times in one message does not become nine tickets.
    """
    counts = {}
    owners = {}
    for ticket in tickets:
        for signature in _signatures(f'{ticket.subject} {ticket.body}'):
            counts[signature] = counts.get(signature, 0) + 1
            owners.setdefault(signature, []).append(ticket)
    return counts, owners


@tool(name='support.find_recurring_problems',
      title='Find recurring problems',
      group='Analysis', agent_types=('support',), icon='fa-repeat',
      reads_only=True, capability='Report the same problem arriving repeatedly',
      parameters=_schema({
          'days': _number('How far back to look. Default 30.'),
          'limit': _number('How many patterns to report. Default 10.'),
      }))
def find_recurring_problems(ctx, days=30, limit=10):
    """Report the problems arriving repeatedly, with counts, as a product signal.

    Reporting this is part of the job rather than an extra. Support sees the
    same defect forty times before engineering sees it once, and a count is the
    only form in which that observation travels: "7 tickets in 30 days name
    'export timeout'" gets prioritised, "customers seem unhappy with exports"
    does not.

    Signatures are pairs of distinctive words, counted once per ticket, with
    common words stripped. Take the top pattern to the Engineering employee or
    file it with support.escalate_to_jira -- answering the tickets one at a time
    while the cause stays in place is the expensive option.
    """
    from ..models_support import OPEN_STATUSES, SupportTicket

    try:
        window = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        window = 30
    try:
        count = max(1, min(int(limit), 30))
    except (TypeError, ValueError):
        count = 10

    since = timezone.now() - timedelta(days=window)
    from django.db.models import Q
    tickets = list(SupportTicket.objects.filter(
        Q(created_at__gte=since) | Q(status__in=OPEN_STATUSES))
        .select_related('customer')[:500])

    if not tickets:
        return ToolResult(
            ok=True,
            text=f'No tickets in the last {window} days and none open, so there is '
                 f'no pattern to report.',
            data={'days': window, 'patterns': []})

    counts, owners = _signature_counts(tickets)
    repeated = sorted(((total, signature) for signature, total in counts.items()
                       if total >= 2), reverse=True)[:count]

    lines = [f'Recurring problems across {len(tickets)} ticket(s) in the last '
             f'{window} days:']
    patterns = []
    if not repeated:
        lines.append('No two-word signature appears in more than one ticket. Every '
                     'request is currently distinct, which is itself worth saying: '
                     'there is no product pattern to chase.')
    else:
        for total, signature in repeated:
            rows = owners[signature][:8]
            categories = {}
            for row in rows:
                categories[row.category] = categories.get(row.category, 0) + 1
            top_category = max(categories.items(), key=lambda pair: pair[1])[0]
            references = ', '.join(f'#{row.pk} {row.reference}' for row in rows[:5])
            lines.append(
                f'- {total} tickets in {window} days name "{signature}" '
                f'(mostly {top_category}): {references}'
                + (' and more' if len(owners[signature]) > 5 else ''))
            patterns.append({'signature': signature, 'count': total,
                             'category': top_category,
                             'ticket_ids': [row.pk for row in owners[signature]]})

        worst = patterns[0]
        lines.append('')
        lines.append(
            f'The signal worth acting on: {worst["count"]} tickets in {window} days '
            f'name "{worst["signature"]}". Answering them individually costs '
            f'{worst["count"]} replies and leaves the cause in place. File it with '
            f'support.escalate_to_jira, or hand it to the Engineering employee.')

    reopened = [ticket for ticket in tickets if (ticket.reopened_count or 0) > 0]
    if reopened:
        lines.append(f'Also worth noting: {len(reopened)} ticket(s) were reopened, '
                     f'which usually means a reply was mistaken for a resolution.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'days': window, 'ticket_count': len(tickets),
              'patterns': patterns[:count]})


@tool(name='support.generate_support_report',
      title='Generate a support report',
      group='Analysis', agent_types=('support',), icon='fa-chart-column',
      capability='Compute a support report from the ticket table',
      parameters=_schema({
          'kind': _text('What the report is about.',
                        ('volume', 'resolution', 'recurring', 'sentiment', 'sla')),
          'days': _number('The period, in days. Default 30.'),
      }))
def generate_support_report(ctx, kind='volume', days=30):
    """Compute a support report from the ticket table and record it.

    Every figure here is computed from real rows: volume by category and
    priority, median hours to first response and to resolution, SLA breaches,
    the reopen rate, the sentiment split and the top recurring signatures. None
    of it is estimated, and the report is stored with its metrics in a JSON
    field so the next one can be compared against it rather than merely read
    alongside it.

    A median rather than a mean, deliberately. One ticket left over a long
    weekend drags a mean response time into fiction, and the number is used to
    decide staffing.
    """
    from ..models_support import CLOSED_STATUSES, OPEN_STATUSES, SupportReport, SupportTicket

    valid_kinds = dict(SupportReport.KIND_CHOICES)
    if kind not in valid_kinds:
        return ToolResult(
            ok=False, error='Unknown report kind.',
            text=f'"{kind}" is not a report kind. Use one of: '
                 f'{", ".join(valid_kinds)}.')

    try:
        window = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        window = 30

    now = timezone.now()
    since = now - timedelta(days=window)
    tickets = list(SupportTicket.objects.filter(created_at__gte=since)
                   .select_related('customer')[:2000])

    if not tickets:
        return ToolResult(
            ok=True,
            text=(f'No tickets arrived in the last {window} days, so there is '
                  f'nothing to report. No report row was created, because an empty '
                  f'report is worse than none: it gets quoted as a zero.'),
            data={'days': window, 'ticket_count': 0})

    by_category, by_priority, by_status, by_sentiment, by_channel = {}, {}, {}, {}, {}
    first_response_hours, resolution_hours = [], []
    breaches, reopened, needing_human, unanswered = [], [], [], []

    for ticket in tickets:
        by_category[ticket.get_category_display()] = \
            by_category.get(ticket.get_category_display(), 0) + 1
        by_priority[ticket.get_priority_display()] = \
            by_priority.get(ticket.get_priority_display(), 0) + 1
        by_status[ticket.get_status_display()] = \
            by_status.get(ticket.get_status_display(), 0) + 1
        by_sentiment[ticket.get_sentiment_display()] = \
            by_sentiment.get(ticket.get_sentiment_display(), 0) + 1
        by_channel[ticket.get_channel_display()] = \
            by_channel.get(ticket.get_channel_display(), 0) + 1

        if ticket.first_response_at and ticket.created_at:
            first_response_hours.append(
                (ticket.first_response_at - ticket.created_at).total_seconds() / 3600)
        else:
            unanswered.append(ticket)
        if ticket.resolved_at and ticket.created_at:
            resolution_hours.append(
                (ticket.resolved_at - ticket.created_at).total_seconds() / 3600)
        if ticket.sla_due_at and ticket.status not in CLOSED_STATUSES \
                and ticket.sla_due_at < now:
            breaches.append(ticket)
        if ticket.reopened_count:
            reopened.append(ticket)
        if ticket.needs_human:
            needing_human.append(ticket)

    counts, owners = _signature_counts(tickets)
    top_signatures = sorted(((total, signature) for signature, total in counts.items()
                             if total >= 2), reverse=True)[:8]

    resolved_count = len([t for t in tickets if t.status in CLOSED_STATUSES])
    open_count = len([t for t in tickets if t.status in OPEN_STATUSES])
    median_first = _median(first_response_hours)
    median_resolution = _median(resolution_hours)
    reopen_rate = round(100.0 * len(reopened) / len(tickets), 1)
    breach_rate = round(100.0 * len(breaches) / len(tickets), 1)

    metrics = {
        'period_days': window,
        'ticket_count': len(tickets),
        'open_count': open_count,
        'resolved_count': resolved_count,
        'by_category': by_category,
        'by_priority': by_priority,
        'by_status': by_status,
        'by_sentiment': by_sentiment,
        'by_channel': by_channel,
        'median_hours_to_first_response': median_first,
        'median_hours_to_resolution': median_resolution,
        'sla_breaches': len(breaches),
        'sla_breach_rate_percent': breach_rate,
        'reopened_count': len(reopened),
        'reopen_rate_percent': reopen_rate,
        'never_answered': len(unanswered),
        'needs_human_count': len(needing_human),
        'top_signatures': [{'signature': signature, 'count': total}
                           for total, signature in top_signatures],
    }

    def tally(mapping):
        return ', '.join(f'{name} {value}' for name, value
                         in sorted(mapping.items(), key=lambda pair: -pair[1]))

    body_lines = [
        f'{valid_kinds[kind]} report, {since:%d %b %Y} to {now:%d %b %Y} '
        f'({window} days).',
        '',
        f'Volume: {len(tickets)} ticket(s). {open_count} still open, '
        f'{resolved_count} resolved or closed.',
        f'By category: {tally(by_category)}.',
        f'By priority: {tally(by_priority)}.',
        f'By channel: {tally(by_channel)}.',
        '',
        f'Median time to first response: '
        + (f'{median_first} hours' if median_first is not None
           else 'not measurable, no ticket has a first response recorded')
        + f'. {len(unanswered)} ticket(s) have never been answered at all.',
        f'Median time to resolution: '
        + (f'{median_resolution} hours' if median_resolution is not None
           else 'not measurable, nothing has been resolved in the period') + '.',
        'Medians rather than means, because one ticket left over a long weekend '
        'makes a mean useless for staffing.',
        '',
        f'SLA: {len(breaches)} breach(es), {breach_rate}% of the period.',
        f'Reopened: {len(reopened)} ticket(s), a reopen rate of {reopen_rate}%. '
        + ('A rate above about 10% usually means tickets are being closed on a '
           'reply rather than on a resolution.' if reopen_rate > 10
           else 'That is within a reasonable range.'),
        f'Flagged for a person: {len(needing_human)} ticket(s).',
        '',
        f'Sentiment split: {tally(by_sentiment)}.',
    ]

    if top_signatures:
        body_lines += ['', 'Recurring signatures, counted once per ticket:']
        for total, signature in top_signatures:
            body_lines.append(f'- {total} tickets name "{signature}"')
        worst = top_signatures[0]
        body_lines.append(
            f'{worst[0]} tickets in {window} days name "{worst[1]}". That is a '
            f'product signal, not a support workload.')
    else:
        body_lines += ['', 'No two-word signature appears in more than one ticket, '
                           'so there is no recurring pattern this period.']

    if breaches:
        body_lines += ['', 'Breached tickets to deal with first:']
        for ticket in sorted(breaches, key=lambda row: row.sla_due_at)[:10]:
            body_lines.append(
                f'- id {ticket.pk} {ticket.reference} '
                f'[{ticket.get_priority_display()}] due '
                f'{ticket.sla_due_at:%d %b %H:%M} -- {ticket.subject[:60]}')

    body = '\n'.join(body_lines)

    report = SupportReport.objects.create(
        title=f'{valid_kinds[kind]} report, {window} days to {now:%d %b %Y}',
        kind=kind, body=body, metrics=metrics,
        period_start=since, period_end=now,
        created_by_agent=getattr(ctx, 'agent', None))

    return ToolResult(
        ok=True,
        text=(f'Created support report #{report.pk}: {report.title}\n\n{body}'),
        data={'report_id': report.pk, 'kind': kind, 'metrics': metrics},
        subject_label='marketing.supportreport', subject_id=report.pk)


@tool(name='support.read_support_document',
      title='Read a support document',
      group='Analysis', agent_types=('support',), integration='google_drive',
      icon='fa-file-lines', reads_only=True,
      capability='Read a support document from Drive',
      parameters=_schema({
          'file_id': _text('The Drive file id, from support.search_support_docs.'),
      }, required=('file_id',)))
def read_support_document(ctx, file_id):
    """Read one support document out of Drive.

    Use this for the working material that is not in the knowledge base:
    runbooks, escalation matrices, a courier's cut-off times. What you quote to
    a customer as policy should still come from support.search_policies, since
    a document in a shared folder is not automatically a policy.
    """
    result = integrations.call('google_drive', 'read_file', file_id=file_id)
    if not result.ok:
        return ToolResult(
            ok=False, error=result.error, demo=result.demo,
            text=(f'Could not read Drive file {file_id}: {result.error} '
                  f'Call support.search_support_docs to get a current file id.'))

    data = result.data or {}
    content = str(_first_value(data, 'content', 'text', 'body', 'plain') or '')
    name = str(_first_value(data, 'name', 'title', 'filename') or file_id)

    if not content:
        return ToolResult(
            ok=True, demo=result.demo,
            text=(f'"{name}" ({file_id}) came back with no readable text. It may be '
                  f'a spreadsheet, an image or a format the connector cannot read.'),
            data={'file_id': file_id, 'name': name, 'characters': 0})

    return ToolResult(
        ok=True, demo=result.demo,
        text=(f'"{name}" ({file_id}), {len(content)} characters:\n\n'
              f'{content[:6000]}'
              + ('\n\n[truncated]' if len(content) > 6000 else '')),
        data={'file_id': file_id, 'name': name, 'characters': len(content),
              'content': content[:6000]})


@tool(name='support.search_support_docs',
      title='Search support documents',
      group='Analysis', agent_types=('support',), integration='google_drive',
      icon='fa-folder-open', reads_only=True,
      capability='Find support documents in Drive',
      parameters=_schema({
          'query': _text('What to search for.'),
          'limit': _number('How many results. Default 6.'),
      }, required=('query',)))
def search_support_docs(ctx, query, limit=6):
    """Search Drive for support working documents, and report each file id.

    The file id is the part you need: support.read_support_document takes it,
    and guessing one reads the wrong document without saying so.
    """
    try:
        count = max(1, min(int(limit), 25))
    except (TypeError, ValueError):
        count = 6

    result = integrations.call('google_drive', 'search_files', query=query,
                               limit=count)
    if not result.ok:
        return ToolResult(
            ok=False, error=result.error, demo=result.demo,
            text=(f'Could not search Drive: {result.error} Check the Google Drive '
                  f'integration on the Integrations page.'))

    items = _listing(result.data or {}, 'files', 'items', 'results', 'documents')
    if not items:
        return ToolResult(
            ok=True, demo=result.demo,
            text=(f'No Drive file matches "{query}". Try different words, or '
                  f'search the knowledge base with support.search_policies, which '
                  f'covers policies, FAQs and troubleshooting notes.'),
            data={'query': query, 'count': 0})

    rows = [_file_fields(item) for item in items]
    lines = [f'{len(rows)} Drive file(s) matching "{query}":']
    for fields in rows:
        lines.append(f'- [{fields["id"] or "no id"}] {fields["name"]}'
                     + (f' {fields["url"]}' if fields['url'] else '')
                     + (f'\n  {fields["snippet"][:160]}' if fields['snippet'] else ''))
    lines.append('Read one with support.read_support_document using the id in '
                 'brackets.')

    return ToolResult(ok=True, demo=result.demo, text='\n'.join(lines),
                      data={'query': query, 'count': len(rows), 'files': rows})


# ===========================================================================
# GROUP: CUSTOMER REPLIES -- the sending half, which needs a person
# ===========================================================================

def _reply_subject(ticket, subject=''):
    if subject:
        return str(subject)[:280]
    base = ' '.join((ticket.subject or 'Your support request').split())
    prefix = '' if base.lower().startswith('re:') else 'Re: '
    return f'{prefix}{base} [{ticket.reference}]'[:280]


def _attach_sent_message(ticket, message, body, author):
    """Link the OutboundMessage to the thread entry it came from.

    An existing unsent draft is reused where the text matches, so the ticket
    shows one entry that was drafted and then sent rather than two entries that
    look like two replies.
    """
    from ..models_support import TicketMessage

    draft = _latest_draft(ticket)
    if draft is not None and ' '.join(draft.body.split()) == ' '.join(body.split()):
        draft.sent_message = message
        draft.save(update_fields=['sent_message'])
        return draft
    return TicketMessage.objects.create(
        ticket=ticket, direction='outbound', author_label=author,
        body=body, is_ai_draft=True, sent_message=message)


def _advance_after_reply(ticket, body):
    """Move the ticket on after a reply actually left the building.

    A reply containing a question moves the ticket to Waiting on the Customer,
    because the next move is theirs and a queue that says Open for a ticket
    nobody can progress is a queue that gets ignored.
    """
    fields = []
    now = timezone.now()
    if not ticket.first_response_at:
        ticket.first_response_at = now
        fields.append('first_response_at')
    asked = '?' in (body or '')
    if ticket.status in ('new', 'open', 'reopened'):
        ticket.status = 'waiting_customer' if asked else 'open'
        fields.append('status')
    if fields:
        fields.append('updated_at')
        ticket.save(update_fields=fields)
    return ticket.status, asked


@tool(name='support.send_customer_reply',
      title='Send a reply to the customer',
      group='Customer Replies', agent_types=('support',), integration='gmail',
      requires_approval=True, risk='high', icon='fa-paper-plane',
      capability='Send a reply to a customer, after approval',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'subject': _text('Leave empty to reply on the ticket subject.'),
          'body': _text('Leave empty to use the latest saved draft.'),
          'to': _text('Leave empty to use the address on the ticket.'),
      }, required=('ticket_id',)))
def send_customer_reply(ctx, ticket_id, subject='', body='', to=''):
    """Prepare the reply that goes to the customer, for a person to approve.

    Leave ``body`` empty and the latest saved draft is used, which is the
    normal path: draft with support.draft_customer_reply, read it, then send.

    This tool REFUSES OUTRIGHT when the ticket is flagged needs_human. It does
    not prepare a proposal a reviewer could approve by reflex, because on a data
    breach, a legal threat, a safety issue, a vulnerable customer or a media
    enquiry the danger is exactly that the reply reads well. Those tickets go to
    a person with no draft attached.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)
    if ticket.needs_human:
        return _human_only_result(ticket, verb='prepare a reply to')

    text = str(body or '').strip()
    source = 'given directly'
    if not text:
        draft = _latest_draft(ticket)
        if draft is None:
            return ToolResult(
                ok=False, error='No draft to send.',
                text=(f'{ticket.reference} has no saved draft and no body was '
                      f'given, so there is nothing to send. Call '
                      f'support.draft_customer_reply first, read what it wrote, '
                      f'then call this again.'))
        text = draft.body
        source = f'draft message #{draft.pk}'

    recipient = _address_of(to) or (to or '').strip() or ticket.contact_address
    if not recipient:
        return ToolResult(
            ok=False, error='No address to reply to.',
            text=(f'{ticket.reference} has no contact address and none was given. '
                  f'Add one to the customer record with support.create_customer, '
                  f'or pass "to" explicitly.'))

    line = _reply_subject(ticket, subject)
    return Proposal(
        title=f'Email {recipient} about {ticket.reference}',
        summary=(f'Reply to {ticket.customer_label} on {ticket.reference} '
                 f'({ticket.get_category_display()}, '
                 f'{ticket.get_priority_display()} priority, open '
                 f'{ticket.age_label}).\nBody taken from {source}.\n'
                 f'On approval the platform will send it, record the send on the '
                 f'ticket thread, set the first response time if it is not '
                 f'already set, and move the ticket on.\n\nSubject: {line}\n\n'
                 f'{text}'),
        payload={'ticket_id': ticket.pk, 'to': recipient, 'subject': line,
                 'body': text},
        editable_fields=[
            editable('to', 'To', 'text', 'The recipient address.'),
            editable('subject', 'Subject', 'text'),
            editable('body', 'Message', 'longtext',
                     'Edit freely. Editing does not approve it.', rows=16),
        ],
        risk='high', integration_key='gmail',
        subject_label='marketing.supportticket', subject_id=ticket.pk,
        subject_display=f'{ticket.reference}: {ticket.subject[:120]}',
        confirmation=(f'Prepared the reply to {recipient} for {ticket.reference} '
                      f'and put it in the approval queue. Nothing has been sent '
                      f'yet, and nothing will be until somebody approves it.'))


@executor('support.send_customer_reply')
def execute_send_customer_reply(action):
    """Send the approved reply, record it, and move the ticket on."""
    payload = dict(action.payload or {})
    ticket = _action_ticket(action)
    body = str(payload.get('body') or '')
    recipient = str(payload.get('to') or '')
    subject = str(payload.get('subject') or '')

    if ticket is not None and ticket.needs_human:
        return ToolResult(
            ok=False, error='Ticket requires a human.',
            text=(f'{ticket.reference} was flagged needs_human after this action '
                  f'was prepared, so nothing was sent. A person must handle it.'))

    result = integrations.call('gmail', 'send_email', to=recipient,
                              subject=subject, body=body)
    message = _record_message(action, result, channel='email', recipient=recipient,
                              subject=subject, body=body,
                              metadata={'ticket_id': payload.get('ticket_id'),
                                        'reference': getattr(ticket, 'reference', '')})

    lines = [_execution_text(result, f'Reply to {recipient}')]
    if result.ok and ticket is not None:
        _attach_sent_message(ticket, message, body,
                             action.agent.name if action.agent else 'Customer Support')
        status, asked = _advance_after_reply(ticket, body)
        lines.append(f'Recorded on {ticket.reference} as outbound message linked to '
                     f'OutboundMessage #{message.pk}.')
        lines.append(f'First response time: '
                     f'{ticket.first_response_at:%d %b %Y at %H:%M}.'
                     if ticket.first_response_at else 'No first response time set.')
        lines.append(f'Status is now {ticket.get_status_display()}'
                     + (', because the reply asks the customer a question.'
                        if asked else '.'))

    return ToolResult(
        ok=result.ok, demo=result.demo, error=result.error, text='\n'.join(lines),
        data={'message_id': message.pk, 'ticket_id': payload.get('ticket_id'),
              'status': getattr(ticket, 'status', ''), 'demo': result.demo},
        subject_label='marketing.supportticket',
        subject_id=getattr(ticket, 'pk', 0) or 0)


@tool(name='support.send_follow_up',
      title='Send a follow-up',
      group='Customer Replies', agent_types=('support',), integration='gmail',
      requires_approval=True, risk='high', icon='fa-clock-rotate-left',
      capability='Chase a customer who has gone quiet, after approval',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'subject': _text('Leave empty to follow up on the ticket subject.'),
          'body': _text('Leave empty for a short follow-up composed from the ticket.'),
          'days_since': _number('How long they have been quiet. Default 3.'),
      }, required=('ticket_id',)))
def send_follow_up(ctx, ticket_id, subject='', body='', days_since=3):
    """Prepare a follow-up to a customer who has gone quiet, for approval.

    A follow-up should give them something rather than only ask for something.
    The composed default restates the one outstanding question and says what
    will happen if they do not reply, so the message is useful even if they
    never answer it. Chasing without either is what makes a follow-up feel like
    pressure.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)
    if ticket.needs_human:
        return _human_only_result(ticket, verb='prepare a follow-up for')

    try:
        quiet = max(1, min(int(days_since), 90))
    except (TypeError, ValueError):
        quiet = 3

    text = str(body or '').strip()
    if not text:
        label, question = UNBLOCKING_QUESTION.get(
            ticket.category, UNBLOCKING_QUESTION['other'])
        text = '\n'.join([
            _greeting(ticket), '',
            f'I am following up on {ticket.reference}, which we last discussed '
            f'about {quiet} day(s) ago.', '',
            f'The one thing I still need is {label}.',
            question, '',
            'If it is easier, reply with whatever you have and I will work from '
            'that. If I do not hear from you I will leave the ticket open rather '
            'than closing it, so nothing is lost.', '',
            _signature(ctx)])

    recipient = ticket.contact_address
    if not recipient:
        return ToolResult(
            ok=False, error='No address to follow up to.',
            text=f'{ticket.reference} has no contact address on file, so no '
                 f'follow-up can be prepared.')

    line = _reply_subject(ticket, subject or f'Following up: {ticket.subject}')
    return Proposal(
        title=f'Follow up with {recipient} on {ticket.reference}',
        summary=(f'Follow-up after {quiet} day(s) of silence on {ticket.reference} '
                 f'({ticket.get_status_display()}).\n\nSubject: {line}\n\n{text}'),
        payload={'ticket_id': ticket.pk, 'to': recipient, 'subject': line,
                 'body': text, 'days_since': quiet},
        editable_fields=[
            editable('to', 'To', 'text'),
            editable('subject', 'Subject', 'text'),
            editable('body', 'Message', 'longtext', rows=14),
        ],
        risk='high', integration_key='gmail',
        subject_label='marketing.supportticket', subject_id=ticket.pk,
        subject_display=f'{ticket.reference}: {ticket.subject[:120]}')


@executor('support.send_follow_up')
def execute_send_follow_up(action):
    """Send the approved follow-up and record it on the ticket."""
    payload = dict(action.payload or {})
    ticket = _action_ticket(action)
    body = str(payload.get('body') or '')
    recipient = str(payload.get('to') or '')
    subject = str(payload.get('subject') or '')

    if ticket is not None and ticket.needs_human:
        return ToolResult(
            ok=False, error='Ticket requires a human.',
            text=(f'{ticket.reference} was flagged needs_human after this was '
                  f'prepared, so no follow-up was sent.'))

    result = integrations.call('gmail', 'send_email', to=recipient,
                              subject=subject, body=body)
    message = _record_message(action, result, channel='email', recipient=recipient,
                              subject=subject, body=body,
                              metadata={'ticket_id': payload.get('ticket_id'),
                                        'follow_up': True,
                                        'days_since': payload.get('days_since')})

    lines = [_execution_text(result, f'Follow-up to {recipient}')]
    if result.ok and ticket is not None:
        _attach_sent_message(ticket, message, body,
                             action.agent.name if action.agent else 'Customer Support')
        if ticket.status in ('new', 'open', 'reopened'):
            ticket.status = 'waiting_customer'
            if not ticket.first_response_at:
                ticket.first_response_at = timezone.now()
            ticket.save(update_fields=['status', 'first_response_at', 'updated_at'])
        lines.append(f'{ticket.reference} is now '
                     f'{ticket.get_status_display()}, recorded against '
                     f'OutboundMessage #{message.pk}.')

    return ToolResult(
        ok=result.ok, demo=result.demo, error=result.error, text='\n'.join(lines),
        data={'message_id': message.pk, 'ticket_id': payload.get('ticket_id')},
        subject_label='marketing.supportticket',
        subject_id=getattr(ticket, 'pk', 0) or 0)


@tool(name='support.send_resolution_confirmation',
      title='Send a resolution confirmation',
      group='Customer Replies', agent_types=('support',), integration='gmail',
      requires_approval=True, risk='high', icon='fa-circle-check',
      capability='Confirm to a customer that their issue is resolved, after approval',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'subject': _text('Leave empty to use the ticket subject.'),
          'body': _text('Leave empty for a confirmation composed from the '
                        'recorded resolution.'),
      }, required=('ticket_id',)))
def send_resolution_confirmation(ctx, ticket_id, subject='', body=''):
    """Prepare the message that tells the customer their issue is resolved.

    Composed from the resolution recorded on the ticket, which is why the
    resolution has to be written down rather than remembered. The message says
    what was done and how to reopen it, because a confirmation that leaves no
    route back turns a small residual problem into a new ticket with no history.

    On approval the ticket moves to Resolved and the resolution time is stamped,
    so the reports measure something real.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)
    if ticket.needs_human:
        return _human_only_result(ticket, verb='prepare a resolution notice for')

    text = str(body or '').strip()
    if not text:
        if not ticket.resolution:
            return ToolResult(
                ok=False, error='No resolution recorded.',
                text=(f'{ticket.reference} has no resolution recorded, so there is '
                      f'nothing to confirm and I will not compose one. Record what '
                      f'was actually done with support.close_ticket, or pass the '
                      f'body explicitly.'))
        text = '\n'.join([
            _greeting(ticket), '',
            f'Your request {ticket.reference} is resolved. Here is what was done:',
            '', ticket.resolution.strip(), '',
            'If anything about it is still not right, reply to this message and it '
            'will reopen the same ticket with all of its history, so you will not '
            'have to explain it again.', '',
            _signature(ctx)])

    recipient = ticket.contact_address
    if not recipient:
        return ToolResult(
            ok=False, error='No address to confirm to.',
            text=f'{ticket.reference} has no contact address on file.')

    line = _reply_subject(ticket, subject or f'Resolved: {ticket.subject}')
    return Proposal(
        title=f'Confirm resolution of {ticket.reference} to {recipient}',
        summary=(f'Tell {ticket.customer_label} that {ticket.reference} is '
                 f'resolved, after {ticket.hours_open} hours open.\n'
                 f'On approval the ticket moves to Resolved and the resolution '
                 f'time is recorded.\n\nSubject: {line}\n\n{text}'),
        payload={'ticket_id': ticket.pk, 'to': recipient, 'subject': line,
                 'body': text},
        editable_fields=[
            editable('to', 'To', 'text'),
            editable('subject', 'Subject', 'text'),
            editable('body', 'Message', 'longtext', rows=14),
        ],
        risk='high', integration_key='gmail',
        subject_label='marketing.supportticket', subject_id=ticket.pk,
        subject_display=f'{ticket.reference}: {ticket.subject[:120]}')


@executor('support.send_resolution_confirmation')
def execute_send_resolution_confirmation(action):
    """Send the approved confirmation and mark the ticket resolved."""
    payload = dict(action.payload or {})
    ticket = _action_ticket(action)
    body = str(payload.get('body') or '')
    recipient = str(payload.get('to') or '')
    subject = str(payload.get('subject') or '')

    result = integrations.call('gmail', 'send_email', to=recipient,
                              subject=subject, body=body)
    message = _record_message(action, result, channel='email', recipient=recipient,
                              subject=subject, body=body,
                              metadata={'ticket_id': payload.get('ticket_id'),
                                        'resolution_confirmation': True})

    lines = [_execution_text(result, f'Resolution confirmation to {recipient}')]
    if result.ok and ticket is not None:
        _attach_sent_message(ticket, message, body,
                             action.agent.name if action.agent else 'Customer Support')
        now = timezone.now()
        ticket.status = 'resolved'
        if not ticket.first_response_at:
            ticket.first_response_at = now
        ticket.resolved_at = now
        ticket.save(update_fields=['status', 'first_response_at', 'resolved_at',
                                   'updated_at'])
        lines.append(f'{ticket.reference} is now Resolved, {ticket.hours_open} '
                     f'hours after it arrived, recorded against OutboundMessage '
                     f'#{message.pk}.')

    return ToolResult(
        ok=result.ok, demo=result.demo, error=result.error, text='\n'.join(lines),
        data={'message_id': message.pk, 'ticket_id': payload.get('ticket_id'),
              'status': getattr(ticket, 'status', '')},
        subject_label='marketing.supportticket',
        subject_id=getattr(ticket, 'pk', 0) or 0)


# ===========================================================================
# GROUP: ESCALATION
# ===========================================================================

def _default_channel(fallback='#support'):
    """The Slack channel configured on the integration, or a sensible default."""
    integration = integrations.get_integration('slack')
    if integration is not None:
        value = (integration.config or {}).get('default_channel')
        if value:
            return str(value)
    return fallback


def _ticket_brief(ticket):
    """The ticket as a few lines a colleague can act on without opening it."""
    lines = [
        f'{ticket.reference} (id {ticket.pk}) -- {ticket.subject}',
        f'{ticket.customer_label}, {ticket.get_category_display()}, '
        f'{ticket.get_priority_display()} priority, open {ticket.age_label}.',
    ]
    if ticket.urgency_signals:
        lines.append('Urgency signals: '
                     + ', '.join(f'"{signal}"'
                                 for signal in ticket.urgency_signals[:4]) + '.')
    if ticket.is_breaching_sla:
        lines.append(f'Response target breached at {ticket.sla_due_at:%d %b %H:%M}.')
    if ticket.needs_human:
        lines.append(f'FLAGGED FOR A PERSON: '
                     f'{ticket.escalation_reason or "no reason recorded"}')
    lines.append('Request as received: '
                 + ' '.join((ticket.body or '')[:600].split()))
    return '\n'.join(lines)


@tool(name='support.escalate_to_jira',
      title='File a Jira issue for a ticket',
      group='Escalation', agent_types=('support',), integration='jira',
      requires_approval=True, risk='medium', icon='fa-bug',
      capability='File a support ticket as a tracked Jira issue, after approval',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'project_key': _text('The Jira project key. Leave empty for the '
                               'configured default.'),
          'summary': _text('One line. Leave empty to use the ticket subject.'),
          'description': _text('Leave empty to compose it from the ticket.'),
          'issue_type': _text('Bug, Task, Story or Incident. Default Bug.'),
          'priority': _text('Leave empty to carry the ticket priority across.'),
      }, required=('ticket_id',)))
def escalate_to_jira(ctx, ticket_id, project_key='', summary='', description='',
                     issue_type='Bug', priority=''):
    """Prepare a Jira issue for a ticket engineering has to fix, for approval.

    Escalate a defect rather than a symptom. The composed description carries
    the customer's own words, the urgency signals that set the priority and the
    ticket reference, because a Jira issue that says "customer reports export
    problem" will be closed as unreproducible and the ticket will come back.

    Check support.find_recurring_problems first. Filing one issue that names
    seven tickets gets fixed; filing seven issues gets triaged.

    On approval the Jira key and URL are written back onto the ticket and it
    moves to Escalated, so the two records stay joined.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    integration = integrations.get_integration('jira')
    default_project = ''
    if integration is not None:
        default_project = str((integration.config or {}).get('project_key') or '')
    project = str(project_key or default_project or 'SUP').strip().upper()

    line = str(summary or f'{ticket.subject}').strip()[:240]
    if not line:
        line = f'Support escalation {ticket.reference}'

    text = str(description or '').strip()
    if not text:
        text = '\n'.join([
            _ticket_brief(ticket), '',
            f'Raised from support ticket {ticket.reference}. '
            f'Reply to the customer goes through support, not through this issue.'])

    mapped = {'urgent': 'Highest', 'high': 'High', 'normal': 'Medium',
              'low': 'Low'}
    level = str(priority or mapped.get(ticket.priority, 'Medium'))

    return Proposal(
        title=f'File {project} issue for {ticket.reference}',
        summary=(f'Create a {issue_type} in {project} at {level} priority for '
                 f'{ticket.reference}.\nOn approval the issue key and URL are '
                 f'written back onto the ticket and it moves to Escalated.\n\n'
                 f'Summary: {line}\n\n{text}'),
        payload={'ticket_id': ticket.pk, 'project': project, 'summary': line,
                 'description': text, 'issue_type': issue_type or 'Bug',
                 'priority': level, 'assignee': '',
                 'labels': ['support', ticket.category]},
        editable_fields=[
            editable('project', 'Project key', 'text'),
            editable('summary', 'Summary', 'text'),
            editable('description', 'Description', 'longtext', rows=14),
            editable('issue_type', 'Issue type', 'text'),
            editable('priority', 'Priority', 'text'),
            editable('assignee', 'Assignee', 'text', 'Leave empty for unassigned.'),
        ],
        risk='medium', integration_key='jira',
        subject_label='marketing.supportticket', subject_id=ticket.pk,
        subject_display=f'{ticket.reference}: {ticket.subject[:120]}')


@executor('support.escalate_to_jira')
def execute_escalate_to_jira(action):
    """Create the Jira issue and join it to the ticket."""
    from ..models_platform import ExternalIssue

    payload = dict(action.payload or {})
    ticket = _action_ticket(action)

    result = integrations.call(
        'jira', 'create_issue',
        project=payload.get('project', ''),
        summary=payload.get('summary', ''),
        description=payload.get('description', ''),
        issue_type=payload.get('issue_type', 'Bug'),
        priority=payload.get('priority', ''),
        assignee=payload.get('assignee', ''),
        labels=payload.get('labels') or [])

    data = result.data or {}
    reference = str(_first_value(data, 'key', 'reference', 'id', 'issue_key'))
    url = str(_first_value(data, 'url', 'link', 'self', 'browse_url'))

    issue = ExternalIssue.objects.create(
        system='jira',
        container=str(payload.get('project') or '')[:200],
        reference=reference[:60],
        title=str(payload.get('summary') or '')[:300],
        body=str(payload.get('description') or ''),
        issue_status='simulated' if result.demo else ('open' if result.ok else 'open'),
        assignee=str(payload.get('assignee') or '')[:120],
        priority=str(payload.get('priority') or '')[:30],
        labels=list(payload.get('labels') or []),
        url=url[:400],
        external_id=reference[:200],
        agent=action.agent,
        action=action,
        subject_label='marketing.supportticket',
        subject_id=getattr(ticket, 'pk', None))

    lines = [_execution_text(result, f'Jira issue in {payload.get("project")}')]
    if result.ok and ticket is not None:
        ticket.external_reference = reference[:60]
        ticket.external_url = url[:400]
        ticket.status = 'escalated'
        if not ticket.escalated_to:
            ticket.escalated_to = f'Jira {payload.get("project")}'
        ticket.save(update_fields=['external_reference', 'external_url', 'status',
                                   'escalated_to', 'updated_at'])
        lines.append(f'{ticket.reference} is now Escalated and carries '
                     f'{reference or "the new issue"} '
                     f'{url}'.strip())
        lines.append('The customer has not been told anything by this. Reply to '
                     'them separately with support.send_customer_reply.')

    return ToolResult(
        ok=result.ok, demo=result.demo, error=result.error, text='\n'.join(lines),
        data={'issue_id': issue.pk, 'reference': reference, 'url': url,
              'ticket_id': payload.get('ticket_id')},
        subject_label='marketing.supportticket',
        subject_id=getattr(ticket, 'pk', 0) or 0)


@tool(name='support.update_jira_issue',
      title='Update a Jira issue',
      group='Escalation', agent_types=('support',), integration='jira',
      requires_approval=True, risk='medium', icon='fa-pen-to-square',
      capability='Move or comment on a Jira issue, after approval',
      parameters=_schema({
          'issue_key': _text("The Jira key, e.g. 'SUP-118'."),
          'status': _text('The status to move it to. Leave empty to only comment.'),
          'comment': _text('A comment to add. Leave empty to only transition.'),
      }, required=('issue_key',)))
def update_jira_issue(ctx, issue_key, status='', comment=''):
    """Prepare a change to a Jira issue: a transition, a comment, or both.

    Add what the issue does not already know. "Two more customers hit this
    today, both on the enterprise plan" changes how it is prioritised; "any
    update?" does not, and a comment that adds nothing trains the team to
    ignore support comments.
    """
    key = str(issue_key or '').strip().upper()
    if not key:
        return ToolResult(ok=False, error='No issue key.',
                          text='Give the Jira issue key, for example SUP-118.')
    if not status and not comment:
        return ToolResult(
            ok=False, error='Nothing to change.',
            text=(f'Give a status to move {key} to, a comment to add, or both. '
                  f'Nothing was prepared.'))

    parts = []
    if status:
        parts.append(f'move it to {status}')
    if comment:
        parts.append('add a comment')

    return Proposal(
        title=f'Update Jira {key}',
        summary=(f'On {key}: {" and ".join(parts)}.'
                 + (f'\n\nComment:\n{comment}' if comment else '')),
        payload={'issue_key': key, 'status': str(status or ''),
                 'comment': str(comment or '')},
        editable_fields=[
            editable('issue_key', 'Issue key', 'text'),
            editable('status', 'Move to status', 'text'),
            editable('comment', 'Comment', 'longtext', rows=8),
        ],
        risk='medium', integration_key='jira')


@executor('support.update_jira_issue')
def execute_update_jira_issue(action):
    """Apply the approved transition and comment, reporting each separately."""
    from ..models_platform import ExternalIssue

    payload = dict(action.payload or {})
    key = str(payload.get('issue_key') or '')
    status = str(payload.get('status') or '')
    comment = str(payload.get('comment') or '')

    lines = []
    demo = False
    ok = True
    errors = []

    if status:
        moved = integrations.call('jira', 'transition_issue', key=key,
                                  status=status)
        demo = demo or moved.demo
        ok = ok and moved.ok
        if not moved.ok:
            errors.append(moved.error)
        lines.append(_execution_text(moved, f'Moving {key} to {status}'))

    if comment:
        commented = integrations.call('jira', 'comment_issue', key=key,
                                      body=comment)
        demo = demo or commented.demo
        ok = ok and commented.ok
        if not commented.ok:
            errors.append(commented.error)
        lines.append(_execution_text(commented, f'Comment on {key}'))

    issue = ExternalIssue.objects.filter(system='jira', reference=key).first()
    if issue is not None:
        if status:
            issue.issue_status = ('simulated' if demo else
                                  ('closed' if status.lower() in
                                   ('done', 'closed', 'resolved') else 'in_progress'))
        if comment:
            issue.body = f'{issue.body}\n\n[support comment]\n{comment}'.strip()
        issue.save(update_fields=['issue_status', 'body', 'updated_at'])
        lines.append(f'ExternalIssue #{issue.pk} updated to match.')
    else:
        issue = ExternalIssue.objects.create(
            system='jira', reference=key[:60], title=f'Jira {key}',
            body=comment, issue_status='simulated' if demo else 'in_progress',
            agent=action.agent, action=action)
        lines.append(f'No existing record for {key}, so ExternalIssue #{issue.pk} '
                     f'was created to keep the trail complete.')

    return ToolResult(ok=ok, demo=demo, error='; '.join(errors),
                      text='\n'.join(lines),
                      data={'issue_key': key, 'issue_id': issue.pk,
                            'status': status, 'commented': bool(comment)})


@tool(name='support.notify_support_team',
      title='Tell the support team',
      group='Escalation', agent_types=('support',), integration='slack',
      requires_approval=True, risk='medium', icon='fa-bell',
      capability='Post a message to the support team, after approval',
      parameters=_schema({
          'message': _text('What the team needs to know.'),
          'channel': _text('Slack channel. Leave empty for the configured default.'),
          'ticket_id': _number('Attach the ticket summary, if this is about one.'),
      }, required=('message',)))
def notify_support_team(ctx, message, channel='', ticket_id=None):
    """Prepare a message to the support team, for approval.

    Use this the moment a ticket is flagged for a person. A needs_human flag
    that nobody is told about is a flag that sits in a database while the
    customer waits, and the flag was set precisely because waiting is the
    expensive outcome.

    Say what is needed and by when. "Someone should look at this" gets read and
    not acted on.
    """
    ticket = None
    if ticket_id not in (None, '', 0):
        ticket = _ticket(ticket_id)
        if ticket is None:
            return _no_ticket(ticket_id)

    target = str(channel or '').strip() or _default_channel('#support')
    text = str(message).strip()
    if ticket is not None:
        text = f'{text}\n\n{_ticket_brief(ticket)}'

    return Proposal(
        title=f'Post to {target}'
              + (f' about {ticket.reference}' if ticket else ''),
        summary=f'Message to the support team in {target}.\n\n{text}',
        payload={'channel': target, 'text': text,
                 'ticket_id': getattr(ticket, 'pk', None)},
        editable_fields=[
            editable('channel', 'Channel', 'text'),
            editable('text', 'Message', 'longtext', rows=10),
        ],
        risk='medium', integration_key='slack',
        subject_label='marketing.supportticket' if ticket else '',
        subject_id=getattr(ticket, 'pk', 0) or 0,
        subject_display=(f'{ticket.reference}: {ticket.subject[:120]}'
                         if ticket else ''))


@executor('support.notify_support_team')
def execute_notify_support_team(action):
    """Post the approved message and record it."""
    payload = dict(action.payload or {})
    ticket = _action_ticket(action)
    channel = str(payload.get('channel') or '')
    text = str(payload.get('text') or '')

    result = integrations.call('slack', 'post_message', channel=channel, text=text)
    message = _record_message(action, result, channel='slack', recipient=channel,
                              subject='Support team notification', body=text,
                              metadata={'ticket_id': payload.get('ticket_id')})

    lines = [_execution_text(result, f'Message to {channel}')]
    if result.ok and ticket is not None:
        from ..models_support import TicketMessage
        TicketMessage.objects.create(
            ticket=ticket, direction='internal',
            author_label=action.agent.name if action.agent else 'Customer Support',
            body=f'Told the support team in {channel}:\n{text}',
            sent_message=message)
        lines.append(f'Recorded as an internal note on {ticket.reference}.')

    return ToolResult(ok=result.ok, demo=result.demo, error=result.error,
                      text='\n'.join(lines),
                      data={'message_id': message.pk, 'channel': channel,
                            'ticket_id': payload.get('ticket_id')})


@tool(name='support.escalate_to_slack',
      title='Escalate a ticket in Slack',
      group='Escalation', agent_types=('support',), integration='slack',
      requires_approval=True, risk='high', icon='fa-tower-broadcast',
      capability='Raise a ticket urgently with the team, after approval',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'channel': _text('Slack channel. Leave empty for the configured default.'),
          'message': _text('Leave empty to compose it from the ticket.'),
      }, required=('ticket_id',)))
def escalate_to_slack(ctx, ticket_id, channel='', message=''):
    """Prepare an urgent escalation of one ticket to the team, for approval.

    High risk deliberately. An escalation interrupts people, and a channel that
    gets interrupted for ordinary tickets stops being read, which costs the
    company exactly when the next real emergency arrives. Escalate when the
    stored urgency signals name a genuine consequence, when a response target
    has been missed, or when the ticket is flagged for a person.

    The composed message leads with what is at stake and what is needed from
    whoever picks it up.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    target = str(channel or '').strip() or _default_channel('#support')
    text = str(message or '').strip()
    if not text:
        need = ('A person must take this over; no AI reply may be sent.'
                if ticket.needs_human
                else 'Someone needs to own this and reply to the customer.')
        text = '\n'.join([
            f'Escalating {ticket.reference} '
            f'({ticket.get_priority_display()} priority).',
            need, '', _ticket_brief(ticket)])

    justified = bool(ticket.urgency_signals or ticket.needs_human
                     or ticket.is_breaching_sla or ticket.priority == 'urgent')
    caution = ('' if justified else
               '\n\nNote for the reviewer: this ticket has no recorded urgency '
               'signal, no missed target and is not flagged for a person, so the '
               'escalation may not be warranted.')

    return Proposal(
        title=f'Escalate {ticket.reference} in {target}',
        summary=(f'Urgent escalation of {ticket.reference} to {target}.'
                 f'{caution}\n\n{text}'),
        payload={'ticket_id': ticket.pk, 'channel': target, 'text': text},
        editable_fields=[
            editable('channel', 'Channel', 'text'),
            editable('text', 'Message', 'longtext', rows=12),
        ],
        risk='high', integration_key='slack',
        subject_label='marketing.supportticket', subject_id=ticket.pk,
        subject_display=f'{ticket.reference}: {ticket.subject[:120]}')


@executor('support.escalate_to_slack')
def execute_escalate_to_slack(action):
    """Post the approved escalation and mark the ticket escalated."""
    payload = dict(action.payload or {})
    ticket = _action_ticket(action)
    channel = str(payload.get('channel') or '')
    text = str(payload.get('text') or '')

    result = integrations.call('slack', 'post_message', channel=channel, text=text)
    message = _record_message(action, result, channel='slack', recipient=channel,
                              subject='Ticket escalation', body=text,
                              metadata={'ticket_id': payload.get('ticket_id'),
                                        'escalation': True})

    lines = [_execution_text(result, f'Escalation to {channel}')]
    if result.ok and ticket is not None:
        from ..models_support import TicketMessage
        ticket.status = 'escalated'
        ticket.escalated_to = channel[:140]
        if not ticket.escalation_reason:
            ticket.escalation_reason = f'Escalated to {channel} by support.'
        ticket.save(update_fields=['status', 'escalated_to', 'escalation_reason',
                                   'updated_at'])
        TicketMessage.objects.create(
            ticket=ticket, direction='internal',
            author_label=action.agent.name if action.agent else 'Customer Support',
            body=f'Escalated in {channel}:\n{text}', sent_message=message)
        lines.append(f'{ticket.reference} is now Escalated to {channel}.')

    return ToolResult(ok=result.ok, demo=result.demo, error=result.error,
                      text='\n'.join(lines),
                      data={'message_id': message.pk, 'channel': channel,
                            'ticket_id': payload.get('ticket_id')},
                      subject_label='marketing.supportticket',
                      subject_id=getattr(ticket, 'pk', 0) or 0)


@tool(name='support.notify_technical_team',
      title='Tell the technical team',
      group='Escalation', agent_types=('support',), integration='slack',
      requires_approval=True, risk='medium', icon='fa-screwdriver-wrench',
      capability='Report a technical fault to engineering, after approval',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'channel': _text('Slack channel. Leave empty for the configured default.'),
          'message': _text('Leave empty to compose it from the ticket.'),
      }, required=('ticket_id',)))
def notify_technical_team(ctx, ticket_id, channel='', message=''):
    """Prepare a technical fault report for engineering, for approval.

    Engineering needs what support usually leaves out: the exact error text,
    how many customers have hit it, and whether it is getting worse. The
    composed message carries the customer's own wording and, where the same
    signature appears on other tickets, the count -- because a fault reported
    once is a support request and a fault reported with a count is a priority.
    """
    from ..models_support import OPEN_STATUSES, SupportTicket

    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    target = str(channel or '').strip() or _default_channel('#engineering')
    text = str(message or '').strip()

    related = []
    if not text:
        signatures = _signatures(f'{ticket.subject} {ticket.body}')
        if signatures:
            for other in SupportTicket.objects.filter(
                    status__in=OPEN_STATUSES).exclude(pk=ticket.pk)[:200]:
                if signatures & _signatures(f'{other.subject} {other.body}'):
                    related.append(other)
        scale = (f'{len(related) + 1} open tickets share wording with this one: '
                 + ', '.join(f'{row.reference}' for row in related[:6])
                 if related else
                 'No other open ticket shares wording with this one, so it may be '
                 'isolated.')
        text = '\n'.join([
            f'Technical fault reported by a customer on {ticket.reference}.',
            scale, '', _ticket_brief(ticket), '',
            'What would help: whether this is known, and whether there is a '
            'workaround we can give the customer today.'])

    return Proposal(
        title=f'Report {ticket.reference} to {target}',
        summary=(f'Technical fault report to {target} for {ticket.reference}.'
                 f'\n\n{text}'),
        payload={'ticket_id': ticket.pk, 'channel': target, 'text': text,
                 'related': [row.pk for row in related]},
        editable_fields=[
            editable('channel', 'Channel', 'text'),
            editable('text', 'Message', 'longtext', rows=12),
        ],
        risk='medium', integration_key='slack',
        subject_label='marketing.supportticket', subject_id=ticket.pk,
        subject_display=f'{ticket.reference}: {ticket.subject[:120]}')


@executor('support.notify_technical_team')
def execute_notify_technical_team(action):
    """Post the approved fault report and note it on the ticket."""
    payload = dict(action.payload or {})
    ticket = _action_ticket(action)
    channel = str(payload.get('channel') or '')
    text = str(payload.get('text') or '')

    result = integrations.call('slack', 'post_message', channel=channel, text=text)
    message = _record_message(action, result, channel='slack', recipient=channel,
                              subject='Technical fault report', body=text,
                              metadata={'ticket_id': payload.get('ticket_id'),
                                        'related': payload.get('related') or []})

    lines = [_execution_text(result, f'Fault report to {channel}')]
    if result.ok and ticket is not None:
        from ..models_support import TicketMessage
        TicketMessage.objects.create(
            ticket=ticket, direction='internal',
            author_label=action.agent.name if action.agent else 'Customer Support',
            body=f'Reported to the technical team in {channel}:\n{text}',
            sent_message=message)
        if ticket.status == 'new':
            ticket.status = 'pending'
            ticket.save(update_fields=['status', 'updated_at'])
        lines.append(f'Noted on {ticket.reference}, now '
                     f'{ticket.get_status_display()}.')
        lines.append('The customer still has not heard anything. Reply to them '
                     'with support.send_customer_reply.')

    return ToolResult(ok=result.ok, demo=result.demo, error=result.error,
                      text='\n'.join(lines),
                      data={'message_id': message.pk, 'channel': channel,
                            'ticket_id': payload.get('ticket_id')},
                      subject_label='marketing.supportticket',
                      subject_id=getattr(ticket, 'pk', 0) or 0)


def _parse_start(value):
    """A datetime out of whatever the model supplied, or None.

    Returns None rather than guessing. A meeting booked at the wrong time is
    worse than one that was not booked, because somebody will attend it.
    """
    from django.utils.dateparse import parse_date, parse_datetime

    text = str(value or '').strip().replace('/', '-')
    if not text:
        return None
    parsed = parse_datetime(text)
    if parsed is None:
        day = parse_date(text[:10])
        if day is None:
            return None
        from datetime import datetime, time
        parsed = datetime.combine(day, time(hour=10))
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


@tool(name='support.schedule_customer_call',
      title='Schedule a call with a customer',
      group='Escalation', agent_types=('support',), integration='google_calendar',
      requires_approval=True, risk='high', icon='fa-phone',
      capability='Book a call with a customer, after approval',
      parameters=_schema({
          'ticket_id': TICKET_ID,
          'start_at': _text("When it starts, as '2026-09-12 14:30' or an ISO "
                            'timestamp.'),
          'duration_minutes': _number('How long. Default 30.'),
          'attendees': {'type': 'array', 'items': {'type': 'string'},
                        'description': 'Email addresses. The customer is added '
                                       'automatically.'},
          'title': _text('Leave empty to compose it from the ticket.'),
      }, required=('ticket_id', 'start_at')))
def schedule_customer_call(ctx, ticket_id, start_at, duration_minutes=30,
                           attendees=None, title=''):
    """Prepare a call with a customer, for approval.

    A call is the right answer when the thread has stopped making progress, or
    when what has to be said should not be said in writing. It is the wrong
    answer when the customer wants a decision and a call is a way of delaying
    it.

    State the customer's timezone in the invitation. A time with no timezone is
    the most common reason a support call is missed, and missing a call the
    customer asked for is worse than never offering one.
    """
    ticket = _ticket(ticket_id)
    if ticket is None:
        return _no_ticket(ticket_id)

    start = _parse_start(start_at)
    if start is None:
        return ToolResult(
            ok=False, error='Unreadable start time.',
            text=(f'I could not read "{start_at}" as a date and time, and I will '
                  f'not guess one. Give it as 2026-09-12 14:30 or as an ISO '
                  f'timestamp.'))
    if start < timezone.now():
        return ToolResult(
            ok=False, error='Start time is in the past.',
            text=(f'{start:%d %b %Y at %H:%M} is in the past, so nothing was '
                  f'prepared. Give a future time.'))

    try:
        minutes = max(10, min(int(duration_minutes), 240))
    except (TypeError, ValueError):
        minutes = 30

    guests = [str(entry).strip() for entry in (attendees or []) if str(entry).strip()]
    if ticket.contact_address and ticket.contact_address not in guests:
        guests.append(ticket.contact_address)

    zone = ''
    if ticket.customer_id and ticket.customer and ticket.customer.timezone_name:
        zone = ticket.customer.timezone_name

    heading = str(title or '').strip() or \
        f'Support call about {ticket.reference}: {ticket.subject[:80]}'
    description = '\n'.join([
        f'Call about support ticket {ticket.reference}.',
        f'Customer timezone: {zone or "not recorded -- confirm it with them"}.',
        '', _ticket_brief(ticket)])

    return Proposal(
        title=f'Book a {minutes} minute call for {ticket.reference}',
        summary=(f'{heading}\n{start:%d %b %Y at %H:%M} for {minutes} minutes.\n'
                 f'Attendees: {", ".join(guests) or "none listed"}.\n'
                 f'Customer timezone: '
                 f'{zone or "not recorded, so confirm it before this is approved"}.'
                 f'\n\n{description}'),
        payload={'ticket_id': ticket.pk, 'title': heading,
                 'start': start.isoformat(), 'duration_minutes': minutes,
                 'attendees': guests, 'description': description,
                 'location': 'Phone'},
        editable_fields=[
            editable('title', 'Title', 'text'),
            editable('start', 'Start', 'text', 'ISO timestamp.'),
            editable('duration_minutes', 'Duration in minutes', 'number'),
            editable('location', 'Location', 'text'),
            editable('description', 'Description', 'longtext', rows=10),
        ],
        risk='high', integration_key='google_calendar',
        subject_label='marketing.supportticket', subject_id=ticket.pk,
        subject_display=f'{ticket.reference}: {ticket.subject[:120]}')


@executor('support.schedule_customer_call')
def execute_schedule_customer_call(action):
    """Book the approved call and record the event."""
    from ..models_platform import CalendarEvent

    payload = dict(action.payload or {})
    ticket = _action_ticket(action)
    start = _parse_start(payload.get('start')) or timezone.now()
    try:
        minutes = max(10, min(int(payload.get('duration_minutes') or 30), 240))
    except (TypeError, ValueError):
        minutes = 30
    guests = list(payload.get('attendees') or [])

    result = integrations.call(
        'google_calendar', 'create_event',
        title=payload.get('title', ''),
        start=start.isoformat(),
        duration_minutes=minutes,
        attendees=guests,
        description=payload.get('description', ''),
        location=payload.get('location', 'Phone'))

    data = result.data or {}
    event = CalendarEvent.objects.create(
        title=str(payload.get('title') or 'Support call')[:250],
        description=str(payload.get('description') or ''),
        start_at=start,
        end_at=start + timedelta(minutes=minutes),
        duration_minutes=minutes,
        attendees=guests,
        location=str(payload.get('location') or '')[:300],
        meeting_link=str(_first_value(data, 'meeting_link', 'hangoutLink',
                                      'url', 'link'))[:400],
        status='simulated' if result.demo else ('scheduled' if result.ok
                                                else 'cancelled'),
        external_id=str(_first_value(data, 'id', 'event_id'))[:200],
        agent=action.agent,
        action=action,
        subject_label='marketing.supportticket',
        subject_id=getattr(ticket, 'pk', None))

    lines = [_execution_text(result, f'Call on {start:%d %b %Y at %H:%M}'),
             f'CalendarEvent #{event.pk} recorded for {minutes} minutes with '
             f'{len(guests)} attendee(s).']
    if result.ok and ticket is not None:
        from ..models_support import TicketMessage
        TicketMessage.objects.create(
            ticket=ticket, direction='internal',
            author_label=action.agent.name if action.agent else 'Customer Support',
            body=(f'Call booked for {start:%d %b %Y at %H:%M} '
                  f'({minutes} minutes) with {", ".join(guests) or "no attendees"}.'))
        if ticket.status == 'new':
            ticket.status = 'open'
            ticket.save(update_fields=['status', 'updated_at'])
        lines.append(f'Noted on {ticket.reference}.')

    return ToolResult(ok=result.ok, demo=result.demo, error=result.error,
                      text='\n'.join(lines),
                      data={'event_id': event.pk, 'start': start.isoformat(),
                            'duration_minutes': minutes,
                            'ticket_id': payload.get('ticket_id')},
                      subject_label='marketing.supportticket',
                      subject_id=getattr(ticket, 'pk', 0) or 0)
