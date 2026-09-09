"""The Marketing & Communications employee's tools.

WHY NO TOOL HERE CALLS A LANGUAGE MODEL
---------------------------------------
Every piece of copy these tools produce is assembled deterministically from
the arguments given to them and from the ``facts`` list passed in. Not one of
them asks a model to write anything. That is not a limitation of the build; it
is the whole point of the division.

The employee itself thinks with a language model -- that is where judgement,
phrasing and the reply in the chat window come from. But the moment a claim is
written into a ``ContentPiece`` that a person may later approve for
publication, the question stops being "does this read well" and becomes "where
did this number come from". A deterministic composer can answer that: every
sentence in the body traces to a topic, an audience, a tone or an entry in
``facts``, and ``facts`` traces to the documents recorded in
``ContentPiece.sources``. A composer that asked a model to "write a post about
our uptime" could not answer it, because the model's recollection is not a
source and cannot be produced on demand.

So the flow the employee is expected to follow is: call
``mkt.get_product_facts`` to obtain the facts with their document titles, pass
them into the generator, and let the generator build copy from them. The copy
is duller than a model would write. It is also defensible, which is the
property that matters once it is public.

WHAT REQUIRES A PERSON
----------------------
Everything that leaves the platform: publishing to LinkedIn or Instagram,
scheduling a post, sending a marketing email or newsletter, posting to Slack,
booking a calendar entry, uploading an asset to Drive. Those tools return a
``Proposal`` and never act. The matching ``@executor`` runs only from
marketing/approvals.py after somebody presses Approve, and it is the executor
-- not the proposing tool -- that calls ``integrations.call``, writes the
``OutboundMessage`` or ``CalendarEvent`` record, and moves the ``ContentPiece``
or ``MarketingEmail`` to published or sent.
"""

import re
from datetime import date as date_cls
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .. import integrations
from ..models import MarketingCampaign
from ..models_content import (CHANNEL_LIMITS, EMAIL_SUBJECT_LIMIT,
                              HOUSE_HASHTAG_LIMITS, AudienceSegment,
                              CampaignBrief, ContentCalendarEntry, ContentPiece,
                              MarketingEmail, channel_label)
from .base import Proposal, ToolResult, editable, executor, tool

AGENTS = ('marketing',)


# ===========================================================================
# House rules, expressed once
# ===========================================================================

# The four words the brief forbids, with their inflections, because
# 'unlocking' is the same failure as 'unlock'.
BANNED_WORDS = (
    'synergy', 'synergies', 'synergistic',
    'revolutionary', 'revolutionise', 'revolutionize',
    'game-changing', 'game changing', 'gamechanging',
    'unlock', 'unlocks', 'unlocking', 'unlocked',
)

# Substitutions applied to text a caller supplied, so a banned word arriving in
# a topic does not end up in the composed copy.
_SOFTEN = (
    ('game-changing', 'significant'),
    ('game changing', 'significant'),
    ('gamechanging', 'significant'),
    ('revolutionary', 'new'),
    ('revolutionise', 'change'),
    ('revolutionize', 'change'),
    ('synergistic', 'shared'),
    ('synergies', 'shared effort'),
    ('synergy', 'shared effort'),
    ('unlocking', 'opening up'),
    ('unlocked', 'opened up'),
    ('unlocks', 'opens up'),
    ('unlock', 'open up'),
)

# Claims that assert a ranking or a promise nobody has measured. Flagged rather
# than substituted: 'the fastest' may be true and provable, and the point of
# the check is to make somebody produce the proof.
SUPERLATIVES = ('best', 'fastest', 'number one', '#1', 'guaranteed')

_STOPWORDS = frozenset((
    'a an the and or but for with to of in on at by from our your their we you '
    'is are was were be been being as that this these those it its how why what '
    'when where who new using use about into over under more most less than then '
    'so if not no all any can will would should could do does did have has had'
).split())


# ===========================================================================
# Parameter-schema shorthands, so forty-odd declarations stay readable
# ===========================================================================

def _text(description):
    return {'type': 'string', 'description': description}


def _integer(description):
    return {'type': 'integer', 'description': description}


def _boolean(description):
    return {'type': 'boolean', 'description': description}


def _array(description):
    return {'type': 'array', 'items': {'type': 'string'}, 'description': description}


def _enum(description, values):
    return {'type': 'string', 'enum': list(values), 'description': description}


def _schema(properties, required=()):
    return {'type': 'object', 'properties': dict(properties), 'required': list(required)}


_FACTS_ARGUMENT = _array(
    'Facts to build the copy from, each ideally ending with its document in '
    'square brackets, exactly as mkt.get_product_facts returns them. Anything '
    'not in this list will not appear as a claim in the copy.')


# ===========================================================================
# Text helpers
# ===========================================================================

def _flatten(text):
    return ' '.join(str(text or '').split())


def _cap(text):
    flat = _flatten(text)
    return (flat[0].upper() + flat[1:]) if flat else flat


def _lower(text):
    """Lowercase the first letter, unless it opens an acronym."""
    flat = _flatten(text)
    if not flat:
        return flat
    first = flat.split()[0]
    if len(first) > 1 and first.isupper():
        return flat
    return flat[0].lower() + flat[1:]


def _sentence(text):
    flat = _flatten(text)
    if not flat:
        return ''
    if flat[-1] not in '.!?:':
        flat += '.'
    return flat[0].upper() + flat[1:]


def _truncate(text, limit):
    flat = _flatten(text)
    if len(flat) <= limit:
        return flat
    cut = flat[:max(0, limit - 3)].rstrip()
    return cut + '...'


def soften(text):
    """Replace any banned word in supplied text, and say what was replaced.

    Returns ``(text, substitutions)``. Applied to topics, offers and product
    names rather than to the composed copy, because the composer's own
    vocabulary is fixed and clean; the risk is a caller handing in 'our
    game-changing rostering engine' and it being repeated verbatim.
    """
    result = str(text or '')
    changes = []
    for word, replacement in _SOFTEN:
        pattern = re.compile(re.escape(word), re.IGNORECASE)
        if pattern.search(result):
            result = pattern.sub(replacement, result)
            changes.append(f'"{word}" replaced with "{replacement}"')
    return result, changes


def banned_words_in(text):
    """Every forbidden word present in a piece of text, in order of appearance."""
    lowered = str(text or '').lower()
    found = []
    for word in BANNED_WORDS:
        if word in lowered and word not in found:
            found.append(word)
    return found


# 'Best regards' is a sign-off, not a claim to be the best at anything. Without
# this exclusion every email the platform writes reports itself as making an
# unsubstantiated superlative claim, and a check that cries wolf on every run
# is a check nobody reads.
_SUPERLATIVE_EXCLUSIONS = re.compile(
    r'\bbest\s+(?:regards|wishes)\b|\ball\s+the\s+best\b', re.IGNORECASE)


def superlatives_in(text):
    """Unsubstantiated ranking words present in a piece of text."""
    lowered = _SUPERLATIVE_EXCLUSIONS.sub(' ', str(text or '')).lower()
    found = []
    for word in SUPERLATIVES:
        if word == '#1':
            if '#1' in lowered:
                found.append('#1')
            continue
        if re.search(rf'\b{re.escape(word)}\b', lowered):
            found.append(word)
    return found


_FIGURE_PATTERN = re.compile(
    r'(?:[$£€]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:%|per cent|percent|'
    r'x|k|m|bn|million|billion|hours?|days?|weeks?|months?|years?)?)',
    re.IGNORECASE)


def figures_in(text):
    """Every number-shaped token in a piece of text.

    Deliberately greedy. A false positive costs a reviewer one glance; a missed
    figure costs the company a public claim it cannot support.
    """
    found = []
    for match in _FIGURE_PATTERN.finditer(str(text or '')):
        token = match.group(0).strip()
        # A bare digit inside a word, or a lone punctuation match, is noise.
        if not any(character.isdigit() for character in token):
            continue
        if token not in found:
            found.append(token)
    return found


def _keywords(text, limit=6):
    words = re.findall(r"[A-Za-z][A-Za-z0-9'&+-]*", str(text or ''))
    chosen = []
    seen = set()
    for word in words:
        low = word.lower()
        if low in _STOPWORDS or len(low) < 3 or low in seen:
            continue
        seen.add(low)
        chosen.append(word)
        if len(chosen) >= limit:
            break
    return chosen


def _camel(words):
    parts = []
    for word in words:
        clean = re.sub(r'[^A-Za-z0-9]', '', str(word))
        if clean:
            parts.append(clean[0].upper() + clean[1:])
    return ''.join(parts)


def _company_name(default='the company'):
    """The company's own name, from settings, without requiring it to exist.

    Read here rather than hard-coded because the platform's promise is that
    nothing needs a code edit. A missing setting is not an error: the copy
    simply says 'the company' and the branded hashtag falls back.
    """
    try:
        from ..models_platform import SystemSetting
        row = SystemSetting.objects.filter(key='company_name').first()
    except Exception:  # noqa: BLE001 -- a settings table that is not there yet
        row = None
    if row is not None:
        value = row.resolved
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


# ===========================================================================
# Facts and provenance
# ===========================================================================

_SOURCE_SUFFIX = re.compile(
    r'\s*[\[(]\s*(?:source\s*[:\-]\s*)?([^\])]{2,140})\s*[\])]\s*$', re.IGNORECASE)


def normalise_facts(facts):
    """Turn whatever the model passed as ``facts`` into (text, source) pairs.

    Three shapes are accepted, because a language model will produce all
    three: a plain sentence, a sentence ending in its document in brackets
    (which is what mkt.get_product_facts returns), and a dictionary. Anything
    that yields no text is dropped rather than becoming an empty bullet.
    """
    rows = []
    if isinstance(facts, (str, bytes)):
        facts = [facts]
    for entry in list(facts or []):
        if isinstance(entry, dict):
            text = _flatten(entry.get('text') or entry.get('fact')
                            or entry.get('claim') or '')
            source = _flatten(entry.get('source') or entry.get('document') or '')
            document_id = entry.get('document_id')
            url = _flatten(entry.get('url') or '')
        else:
            text = _flatten(entry)
            source = ''
            document_id = None
            url = ''
            match = _SOURCE_SUFFIX.search(text)
            if match:
                source = _flatten(match.group(1))
                text = _flatten(text[:match.start()])
        if not text:
            continue
        cleaned, _changes = soften(text)
        rows.append({'fact': _flatten(cleaned), 'source': source,
                     'document_id': document_id, 'url': url})
    return rows


def sources_payload(rows):
    """The ``ContentPiece.sources`` value for a set of normalised facts."""
    return [{'fact': row['fact'], 'document': row['source'],
             'document_id': row.get('document_id'), 'url': row.get('url', '')}
            for row in rows]


def unsourced_facts(rows):
    """Facts that arrived without a document attached."""
    return [row['fact'] for row in rows if not row['source'] and not row.get('document_id')]


def _fact_texts(rows, limit=None):
    texts = [row['fact'] for row in rows]
    return texts[:limit] if limit else texts


def _facts_note(rows):
    """The line every generator adds about the evidence behind its copy."""
    if not rows:
        return ('No facts were supplied, so the copy states no figures and no '
                'product claims. Call mkt.get_product_facts and regenerate '
                'before this is published as anything other than an opinion.')
    missing = unsourced_facts(rows)
    documents = sorted({row['source'] for row in rows if row['source']})
    parts = [f'Built from {len(rows)} supplied fact{"s" if len(rows) != 1 else ""}.']
    if documents:
        parts.append('Sources: ' + ', '.join(documents) + '.')
    if missing:
        parts.append(f'{len(missing)} of them name no document: '
                     + '; '.join(f'"{text}"' for text in missing[:3])
                     + '. Those are unsourced claims and are recorded as such.')
    return ' '.join(parts)


# ===========================================================================
# Tone, hooks and hashtags
# ===========================================================================

_TONE_BRIDGE = {
    'professional': 'What that means in practice:',
    'friendly': 'Here is what it looks like day to day:',
    'confident': 'Why it matters:',
    'plain': 'In plain terms:',
    'urgent': 'Why it matters now:',
    'informative': 'The detail behind it:',
    'warm': 'What we have seen:',
    'technical': 'The specifics:',
}

_TONE_CTA = {
    'professional': 'If this is a problem you recognise, reply and we will walk '
                    'you through how we handle it.',
    'friendly': 'If you have run into this, tell us how you dealt with it. '
                'We read every reply.',
    'confident': 'Ask us for the detail and we will send it. No form, no call.',
    'plain': 'Reply if you want the detail and we will send it across.',
    'urgent': 'Reply this week and we will get you the detail before the month closes.',
    'informative': 'Ask for the write-up and we will send it across.',
    'warm': 'Tell us what you are working on and we will point you at the useful part.',
    'technical': 'Ask for the technical notes and we will send them.',
}

_TONE_KEYS = tuple(_TONE_BRIDGE)


def _bridge(tone):
    return _TONE_BRIDGE.get(_flatten(tone).lower(), _TONE_BRIDGE['professional'])


def _cta_line(tone, given=''):
    if _flatten(given):
        return _sentence(given)
    return _TONE_CTA.get(_flatten(tone).lower(), _TONE_CTA['professional'])


HOOK_STYLES = ('direct', 'statistic', 'question', 'contrast')

HOOK_REASONS = {
    'direct': 'states the point in the first line with no set-up',
    'statistic': 'opens on the strongest supplied fact',
    'question': 'opens on the question the post answers',
    'contrast': 'opens on the framing, then narrows to the point',
}


def _opening(style, subject, audience, facts):
    who = _flatten(audience) or 'the teams we work with'
    if style == 'statistic' and facts:
        return _sentence(facts[0]['fact'])
    if style == 'question':
        return _sentence(f'How much of your week goes on {_lower(subject)}')[:-1] + '?'
    if style == 'contrast':
        return _sentence(f'{_cap(subject)} looks like a small thing until it is '
                         'the thing holding everything else up')
    return _sentence(f'{_cap(subject)} comes up in almost every conversation we '
                     f'have with {who}')


_BROAD_TAGS = {
    'linkedin': ('Operations', 'Leadership'),
    'instagram': ('BehindTheScenes', 'OurTeam'),
    'twitter': ('Product', 'Build'),
    'facebook': ('Community', 'Updates'),
    'ad': ('Offer',),
    'blog': ('Insights',),
}


def build_hashtags(topic, channel='linkedin', count=5):
    """Hashtags with the reason each was chosen, deterministically.

    A mix of three kinds, because a set of five niche tags reaches nobody new
    and a set of five broad ones reaches nobody relevant:

      broad     the general conversation the post belongs in
      niche     the specific subject, from the topic's own words
      branded   the company's own tag, so the post is findable later
    """
    house = HOUSE_HASHTAG_LIMITS.get(channel, 5)
    try:
        wanted = int(count)
    except (TypeError, ValueError):
        wanted = house or 5
    wanted = max(0, min(wanted, house if house else wanted))

    words = _keywords(topic, limit=5)
    rows = []

    for tag in _BROAD_TAGS.get(channel, ('Marketing',)):
        rows.append({'tag': tag, 'kind': 'broad',
                     'reason': f'the general {channel_label(channel)} conversation this '
                               'post belongs in, so it reaches beyond existing followers'})

    if words:
        rows.append({'tag': _camel(words[:2]), 'kind': 'niche',
                     'reason': 'the subject itself, taken from the topic wording, so '
                               'the people already searching for it find the post'})
    if len(words) >= 3:
        rows.append({'tag': _camel([words[2]]), 'kind': 'niche',
                     'reason': 'a narrower cut of the same subject, for readers who '
                               'follow the specific term rather than the category'})
    if len(words) >= 4:
        rows.append({'tag': _camel([words[3]]), 'kind': 'niche',
                     'reason': 'a secondary term from the topic, included so the set '
                               'is not one idea repeated'})

    # Pairwise combinations, so a channel that allows twelve tags gets twelve
    # that mean something rather than the same five padded out.
    for first, second in zip(words, words[1:]):
        rows.append({'tag': _camel([first, second]), 'kind': 'niche',
                     'reason': f'"{first}" and "{second}" together, which is how '
                               'the subject is usually written where it is '
                               'already being discussed'})
    for word in words:
        rows.append({'tag': _camel([word]), 'kind': 'niche',
                     'reason': f'"{word}" on its own, for readers following the '
                               'single term rather than the phrase'})

    brand = _camel(_keywords(_company_name('Our Company'), limit=3))
    if brand:
        rows.append({'tag': brand, 'kind': 'branded',
                     'reason': 'the company tag, so everything published on this '
                               'subject can be found together afterwards'})
        if words:
            rows.append({'tag': _camel([brand, words[0]]), 'kind': 'branded',
                         'reason': 'the company tag joined to the subject, so this '
                                   'campaign is separable from everything else the '
                                   'company publishes'})

    # Deduplicate, then interleave the three kinds. Order matters because the
    # list is truncated to the channel's limit: taken in the order they were
    # built, five tags on LinkedIn would be four niche terms and a broad one,
    # which reaches only people already searching for the exact phrase.
    buckets = {'broad': [], 'niche': [], 'branded': []}
    seen = set()
    for row in rows:
        key = row['tag'].lower()
        if not row['tag'] or key in seen:
            continue
        seen.add(key)
        buckets[row['kind']].append(row)

    order = ('niche', 'broad', 'niche', 'branded', 'niche', 'broad',
             'niche', 'branded')
    chosen = []
    cursors = {kind: 0 for kind in buckets}
    for kind in order:
        if len(chosen) >= wanted:
            break
        if cursors[kind] < len(buckets[kind]):
            chosen.append(buckets[kind][cursors[kind]])
            cursors[kind] += 1

    # Whatever is left over, in the order it was built, fills the remainder.
    for kind in ('niche', 'broad', 'branded'):
        for row in buckets[kind][cursors[kind]:]:
            if len(chosen) >= wanted:
                break
            chosen.append(row)
    return chosen[:wanted]


def _tag_list(rows):
    return [row['tag'] for row in rows]


# ===========================================================================
# The composers -- every word of published copy comes from here
# ===========================================================================

_LENGTH_FACTS = {'short': 2, 'medium': 4, 'long': 6}


def compose_long_form(subject, audience, tone, facts, hook='direct', cta='',
                      length='medium'):
    """The LinkedIn and Facebook shape: open concretely, one point, then a close."""
    allowance = _LENGTH_FACTS.get(_flatten(length).lower(), 4)
    lines = [_opening(hook, subject, audience, facts), '']

    # A fact already used as the opening must not reappear as a bullet: the
    # same sentence twice reads as a mistake, which it is.
    start = 1 if (hook == 'statistic' and facts) else 0
    chosen = facts[start:start + allowance]

    if chosen:
        lines.append(_bridge(tone))
        for row in chosen:
            lines.append(f'- {_sentence(row["fact"])}')
    else:
        lines.append(_sentence(
            f'We treat {_lower(subject)} as an operational habit rather than a '
            'one-off project, which is a duller answer than most people want '
            'and a more useful one'))

    lines.extend(['', _cta_line(tone, cta)])
    return '\n'.join(lines).strip()


def compose_caption(subject, audience, tone, facts, hook='direct', cta=''):
    """The Instagram shape: shorter lines, no bullets, a close that invites a reply."""
    lines = [_opening(hook, subject, audience, facts), '']
    start = 1 if (hook == 'statistic' and facts) else 0
    chosen = facts[start:start + 3]
    for row in chosen:
        lines.append(_sentence(row['fact']))
    if not chosen:
        lines.append(_sentence(f'A short note on how we actually handle '
                               f'{_lower(subject)}'))
    lines.extend(['', _cta_line(tone, cta)])
    return '\n'.join(lines).strip()


def compose_short_post(subject, audience, tone, facts, hashtags=(), cta=''):
    """The Twitter/X shape, built to fit 280 characters including the hashtags."""
    tag_line = ' '.join(f'#{tag}' for tag in hashtags)
    budget = CHANNEL_LIMITS['twitter']['characters']
    if tag_line:
        budget -= len(tag_line) + 2

    core = _sentence(facts[0]['fact']) if facts else _opening('direct', subject,
                                                              audience, facts)
    tail = _flatten(cta) or 'Ask us for the detail.'
    candidate = f'{core} {tail}'
    if len(candidate) <= budget:
        return candidate
    if len(core) <= budget:
        return core
    return _truncate(core, max(20, budget))


def compose_article(title, outline, facts, audience='', tone='', word_target=700):
    """A structured article with real headings, not a wall of text.

    Headings come from the supplied outline when there is one, because an
    outline a person wrote is better than one derived from a title. Without an
    outline the structure is the one an explanatory piece almost always wants:
    what the problem is, what we do about it, what it costs, and what to do next.
    """
    heading_source = [
        _flatten(line).lstrip('-*0123456789. ')
        for line in re.split(r'[\n;]|(?<=[a-z]),\s(?=[A-Z])', str(outline or ''))
        if _flatten(line)
    ]
    if not heading_source:
        heading_source = ['The problem, stated plainly',
                          'How we approach it',
                          'What it actually takes',
                          'Where it goes wrong']

    try:
        target = max(200, int(word_target))
    except (TypeError, ValueError):
        target = 700
    per_section = max(60, target // (len(heading_source) + 2))

    who = _flatten(audience) or 'the teams we work with'
    parts = [f'# {_cap(title)}', '']
    parts.append(_sentence(
        f'This is written for {who}. It covers {_lower(title)} and nothing else, '
        f'and it is roughly a {max(2, target // 200)}-minute read'))
    if facts:
        parts.append('')
        parts.append(_sentence(facts[0]['fact']))

    remaining = list(facts[1:]) if facts else []
    for index, heading in enumerate(heading_source):
        parts.extend(['', f'## {_cap(heading)}', ''])
        parts.append(_sentence(
            f'{_cap(heading)} is where most of the effort goes, so it is worth '
            f'being specific about it rather than general'))
        share = remaining[index::len(heading_source)] if remaining else []
        for row in share[:2]:
            parts.append('')
            parts.append(_sentence(row['fact']))
        parts.append('')
        parts.append(_sentence(
            f'Roughly {per_section} words belong here. Expand it with detail you '
            f'can source, and leave it short if you cannot'))

    parts.extend(['', '## What to do next', '', _cta_line(tone)])
    return '\n'.join(parts).strip()


def compose_email_body(purpose, audience, offer, facts, tone='', cta=''):
    """The marketing email shape: a greeting, one point, the offer, one action."""
    who = _flatten(audience) or 'there'
    lines = [f'Hi {who},', '']
    lines.append(_sentence(f'A short note about {_lower(purpose)}'))
    lines.append('')
    if facts:
        lines.append(_bridge(tone))
        for row in facts[:4]:
            lines.append(f'- {_sentence(row["fact"])}')
        lines.append('')
    if _flatten(offer):
        lines.append(_sentence(offer))
        lines.append('')
    lines.append(_cta_line(tone, cta))
    lines.extend(['', 'Best regards,', _company_name('The team')])
    return '\n'.join(lines).strip()


def compose_description(product, length, audience, facts):
    """A product description at one of three lengths."""
    sentences = {'short': 1, 'medium': 3, 'long': 5}.get(_flatten(length).lower(), 3)
    who = _flatten(audience) or 'the teams that use it'
    body = [_sentence(f'{_cap(product)} is built for {who}')]
    body.extend(_sentence(row['fact']) for row in facts[:sentences])

    # A description shorter than the requested length is padded with exactly one
    # closing line and no more. Repeating filler to hit a sentence count would
    # produce a longer description that says less, which is the opposite of what
    # the length argument was asking for.
    if len(body) < sentences:
        body.append(_sentence(
            f'It is used day to day rather than at a launch, which is the only '
            f'test of {_lower(product)} that matters'))
    return ' '.join(body[:max(1, sentences)])


# ===========================================================================
# Record helpers
# ===========================================================================

def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fail(text):
    return ToolResult(ok=False, error=_flatten(text)[:400], text=text)


def find_campaign(campaign_id):
    """One campaign by id, or (None, failure) with advice when it is missing."""
    key = _int_or_none(campaign_id)
    if key is None:
        return (None, None)
    campaign = MarketingCampaign.objects.filter(pk=key).first()
    if campaign is None:
        names = list(MarketingCampaign.objects.order_by('-created_at')
                     .values_list('pk', 'name')[:8])
        listing = ', '.join(f'#{pk} {name}' for pk, name in names) or 'none recorded'
        return (None, _fail(f'There is no campaign #{key}. Existing campaigns: '
                            f'{listing}. Use mkt.create_campaign to make a new one.'))
    return (campaign, None)


def find_piece(content_id):
    key = _int_or_none(content_id)
    if key is None:
        return (None, _fail('A content id is needed and none was given. '
                            'Call mkt.list_content to find the piece first.'))
    piece = ContentPiece.objects.filter(pk=key).first()
    if piece is None:
        recent = list(ContentPiece.objects.values_list('pk', 'channel', 'title')[:8])
        listing = ', '.join(f'#{pk} {channel} {title or "untitled"}'
                            for pk, channel, title in recent) or 'none recorded'
        return (None, _fail(f'There is no content piece #{key}. Recent pieces: '
                            f'{listing}. Call mkt.list_content for the full list.'))
    return (piece, None)


def find_email(email_id):
    key = _int_or_none(email_id)
    if key is None:
        return (None, _fail('A marketing email id is needed and none was given. '
                            'Call mkt.list_emails to find it first.'))
    row = MarketingEmail.objects.filter(pk=key).first()
    if row is None:
        recent = list(MarketingEmail.objects.values_list('pk', 'subject')[:8])
        listing = ', '.join(f'#{pk} {subject or "no subject"}'
                            for pk, subject in recent) or 'none recorded'
        return (None, _fail(f'There is no marketing email #{key}. Recent mailings: '
                            f'{listing}.'))
    return (row, None)


def _parse_date(value, default=None):
    if isinstance(value, date_cls) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = _flatten(value)
    if not text:
        return default
    parsed = parse_date(text)
    if parsed is not None:
        return parsed
    moment = parse_datetime(text)
    return moment.date() if moment is not None else default


def _parse_moment(value):
    """A timezone-aware datetime from whatever the model wrote, or None."""
    if isinstance(value, datetime):
        moment = value
    else:
        text = _flatten(value)
        if not text:
            return None
        moment = parse_datetime(text)
        if moment is None:
            day = parse_date(text)
            if day is None:
                return None
            moment = datetime(day.year, day.month, day.day, 9, 0)
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_current_timezone())
    return moment


def create_piece(ctx, *, channel, kind, body, title='', hashtags=(), cta='',
                 audience='', tone='', campaign=None, brief=None, facts=(),
                 variant_of=None, variant_label='', status='draft'):
    """Write one ContentPiece, with its counts and its provenance."""
    piece = ContentPiece(
        campaign=campaign, brief=brief, channel=channel, kind=kind,
        title=_flatten(title)[:250], body=body,
        hashtags=[str(tag).lstrip('#') for tag in hashtags if str(tag).strip()],
        call_to_action=_flatten(cta)[:250], audience=_flatten(audience)[:250],
        tone=_flatten(tone)[:60], variant_of=variant_of,
        variant_label=_flatten(variant_label)[:120], status=status,
        sources=sources_payload(list(facts)), created_by_agent=ctx.agent)
    piece.recount()
    piece.save()
    return piece


def piece_result(piece, facts=(), notes=(), extra_data=None):
    """The standard result for a generator: the copy, the id, and the limits."""
    fits, message = piece.fits_channel
    lines = [
        f'{channel_label(piece.channel)} {piece.get_kind_display().lower()} saved '
        f'as content piece #{piece.pk}, status {piece.get_status_display().lower()}. '
        f'It is not published; publishing is a separate tool and needs approval.',
        '',
        '--- copy ---',
        piece.full_text,
        '--- end of copy ---',
        '',
        ('Within limits. ' if fits else 'OVER THE LIMIT. ') + message,
        _facts_note(list(facts)),
    ]
    banned = banned_words_in(piece.full_text)
    if banned:
        lines.append('House-rule breach: ' + ', '.join(banned) + ' appear in the copy.')
    for note in notes:
        if _flatten(note):
            lines.append(_flatten(note))

    data = {
        'content_id': piece.pk,
        'channel': piece.channel,
        'kind': piece.kind,
        'status': piece.status,
        'characters': piece.character_count,
        'words': piece.word_count,
        'hashtags': piece.hashtags,
        'fits_channel': fits,
        'limit_message': message,
        'sources': piece.sources,
    }
    data.update(extra_data or {})
    return ToolResult(ok=True, text='\n'.join(lines), data=data,
                      subject_label='marketing.contentpiece', subject_id=piece.pk)


# ===========================================================================
# Knowledge-base access, guarded
# ===========================================================================

def knowledge_search(query, limit=3):
    """Search the company knowledge base, tolerating its absence.

    Returns ``(hits, note)``. The note is what a caller reports when there are
    no hits, because "the knowledge base has nothing on brand tone" and "the
    knowledge base is not installed" lead to completely different next steps
    and both are better than silence.
    """
    try:
        from .. import knowledge
    except ImportError:
        return ([], 'The knowledge base module is not available in this build, '
                    'so nothing could be looked up there.')
    try:
        hits = list(knowledge.search(query, limit=limit) or [])
    except Exception as exc:  # noqa: BLE001 -- a search failure is a result
        return ([], f'The knowledge base search failed: {exc}')
    if not hits:
        return ([], f'The knowledge base has nothing matching "{query}".')
    return (hits, '')


def _hit_fields(hit):
    """Title, url, snippet, score and document id from one search hit."""
    document = getattr(hit, 'document', None)
    return {
        'title': _flatten(getattr(document, 'title', '') or 'Untitled document'),
        'url': _flatten(getattr(document, 'url', '') or ''),
        'document_id': getattr(document, 'pk', None),
        'snippet': _flatten(getattr(hit, 'snippet', '') or ''),
        'score': getattr(hit, 'score', 0),
    }


# ===========================================================================
# GROUP: Content Creation
# ===========================================================================

@tool(name='mkt.generate_linkedin_post', title='Write a LinkedIn post',
      description=(
          'Write a LinkedIn post and save it as a content piece. The copy is '
          'built from the topic and from the facts you pass in, so pass facts: '
          'anything not in that list will not appear as a claim. Opens '
          'concretely, makes one point, closes with something the reader can do, '
          'and adds at most five hashtags. Nothing is published -- use '
          'mkt.publish_linkedin_post afterwards, which needs approval.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-linkedin',
      capability='LinkedIn post writing',
      parameters=_schema({
          'topic': _text('What the post is about.'),
          'audience': _text('Who it is written for, e.g. "operations managers".'),
          'tone': _enum('Register of the copy.', _TONE_KEYS),
          'include_hashtags': _boolean('Add hashtags. Five at most on LinkedIn.'),
          'campaign_id': _integer('Campaign this belongs to, if any.'),
          'facts': _FACTS_ARGUMENT,
      }, required=('topic',)))
def generate_linkedin_post(ctx, topic, audience='', tone='professional',
                           include_hashtags=True, campaign_id=None, facts=None):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure

    subject, changes = soften(topic)
    rows = normalise_facts(facts)
    tags = (_tag_list(build_hashtags(subject, 'linkedin',
                                     HOUSE_HASHTAG_LIMITS['linkedin']))
            if include_hashtags else [])

    body = compose_long_form(subject, audience, tone, rows,
                             hook='statistic' if rows else 'direct')
    piece = create_piece(
        ctx, channel='linkedin', kind='post', body=body,
        title=f'LinkedIn: {_truncate(subject, 60)}', hashtags=tags,
        cta=_cta_line(tone), audience=audience, tone=tone,
        campaign=campaign, facts=rows)

    notes = []
    if changes:
        notes.append('Banned wording in the topic was replaced: '
                     + '; '.join(changes) + '.')
    if campaign is not None:
        notes.append(f'Linked to campaign #{campaign.pk} "{campaign.name}".')
    return piece_result(piece, rows, notes)


@tool(name='mkt.generate_instagram_caption', title='Write an Instagram caption',
      description=(
          'Write an Instagram caption and save it as a content piece. Shorter '
          'lines than LinkedIn, no bullet points, and up to twelve hashtags. '
          'Instagram allows 2200 characters and 30 hashtags; the house limit is '
          'twelve because a wall of tags reads as spam. Pass facts for anything '
          'the caption claims. Publishing is a separate, approved step.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-instagram',
      capability='Instagram caption writing',
      parameters=_schema({
          'topic': _text('What the caption is about.'),
          'tone': _enum('Register of the copy.', _TONE_KEYS),
          'include_hashtags': _boolean('Add hashtags. Twelve at most.'),
          'campaign_id': _integer('Campaign this belongs to, if any.'),
          'facts': _FACTS_ARGUMENT,
      }, required=('topic',)))
def generate_instagram_caption(ctx, topic, tone='friendly', include_hashtags=True,
                               campaign_id=None, facts=None):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure

    subject, changes = soften(topic)
    rows = normalise_facts(facts)
    tags = (_tag_list(build_hashtags(subject, 'instagram',
                                     HOUSE_HASHTAG_LIMITS['instagram']))
            if include_hashtags else [])

    body = compose_caption(subject, '', tone, rows,
                           hook='statistic' if rows else 'direct')
    piece = create_piece(
        ctx, channel='instagram', kind='caption', body=body,
        title=f'Instagram: {_truncate(subject, 60)}', hashtags=tags,
        cta=_cta_line(tone), tone=tone, campaign=campaign, facts=rows)

    notes = []
    if changes:
        notes.append('Banned wording in the topic was replaced: '
                     + '; '.join(changes) + '.')
    return piece_result(piece, rows, notes)


@tool(name='mkt.generate_social_post', title='Write a post for any channel',
      description=(
          'Write a social post for a named channel and save it as a content '
          "piece, respecting that channel's own limits: LinkedIn 3000 "
          'characters, Instagram 2200 and 30 hashtags, Twitter/X 280. Use this '
          'when the channel is not LinkedIn or Instagram, or when the channel '
          'comes from a calendar entry. Pass facts for anything it claims.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-share-nodes',
      capability='Multi-channel post writing',
      parameters=_schema({
          'channel': _enum('Channel key.', tuple(CHANNEL_LIMITS)),
          'topic': _text('What the post is about.'),
          'tone': _enum('Register of the copy.', _TONE_KEYS),
          'audience': _text('Who it is written for.'),
          'campaign_id': _integer('Campaign this belongs to, if any.'),
          'facts': _FACTS_ARGUMENT,
      }, required=('channel', 'topic')))
def generate_social_post(ctx, channel, topic, tone='', audience='',
                         campaign_id=None, facts=None):
    key = _flatten(channel).lower().replace('/', '').replace(' ', '')
    key = {'x': 'twitter', 'twitterx': 'twitter'}.get(key, key)
    if key not in CHANNEL_LIMITS:
        return _fail(f'"{channel}" is not a channel this platform writes for. '
                     f'Choose one of: {", ".join(CHANNEL_LIMITS)}.')

    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure

    subject, changes = soften(topic)
    rows = normalise_facts(facts)
    tone = tone or ('friendly' if key == 'instagram' else 'professional')
    tags = _tag_list(build_hashtags(subject, key, HOUSE_HASHTAG_LIMITS.get(key, 5)))

    if key == 'twitter':
        kind = 'post'
        body = compose_short_post(subject, audience, tone, rows, tags)
    elif key == 'instagram':
        kind = 'caption'
        body = compose_caption(subject, audience, tone, rows)
    elif key == 'blog':
        kind, tags = 'article', []
        body = compose_article(subject, '', rows, audience, tone)
    elif key == 'email':
        kind, tags = 'newsletter', []
        body = compose_email_body(subject, audience, '', rows, tone)
    elif key in ('press', 'website'):
        kind, tags = 'announcement', []
        body = compose_long_form(subject, audience, tone, rows, length='long')
    else:
        kind = 'post'
        body = compose_long_form(subject, audience, tone, rows)

    piece = create_piece(
        ctx, channel=key, kind=kind, body=body,
        title=f'{channel_label(key)}: {_truncate(subject, 60)}', hashtags=tags,
        cta=_cta_line(tone), audience=audience, tone=tone,
        campaign=campaign, facts=rows)

    notes = []
    if changes:
        notes.append('Banned wording in the topic was replaced: '
                     + '; '.join(changes) + '.')
    return piece_result(piece, rows, notes)


@tool(name='mkt.generate_hashtags', title='Suggest hashtags',
      description=(
          'Suggest hashtags for a topic on one channel, as a mix of broad, '
          'niche and branded tags, each with the reason it was chosen. Nothing '
          'is saved; use the tags with a generator or with mkt.update_content. '
          'Instagram allows 30 tags but the house limit is twelve, and five '
          'everywhere else.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-hashtag',
      reads_only=True, capability='Hashtag research',
      parameters=_schema({
          'topic': _text('Subject the tags are for.'),
          'channel': _enum('Channel the tags will be used on.', tuple(CHANNEL_LIMITS)),
          'count': _integer('How many tags to return.'),
      }, required=('topic',)))
def generate_hashtags(ctx, topic, channel='linkedin', count=5):
    key = _flatten(channel).lower()
    if key not in CHANNEL_LIMITS:
        key = 'linkedin'
    subject, _changes = soften(topic)
    rows = build_hashtags(subject, key, count)
    if not rows:
        return _fail(f'No hashtags could be derived from "{topic}". Give a topic '
                     'with at least one distinctive word in it.')

    house = HOUSE_HASHTAG_LIMITS.get(key, 5)
    platform = CHANNEL_LIMITS[key].get('hashtags') or 0
    lines = [f'{len(rows)} hashtags for {channel_label(key)} on '
             f'"{_flatten(subject)}":', '']
    for row in rows:
        lines.append(f'#{row["tag"]} ({row["kind"]}) -- {row["reason"]}')
    lines.extend(['', ' '.join(f'#{tag}' for tag in _tag_list(rows)), ''])
    lines.append(f'House limit on {channel_label(key)} is {house}; the platform '
                 f'itself allows {platform or "no stated maximum"}.')
    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'channel': key, 'hashtags': _tag_list(rows),
                            'detail': rows, 'house_limit': house})


@tool(name='mkt.generate_product_description', title='Write a product description',
      description=(
          'Write a product description at a chosen length and save it as a '
          'content piece. Short is one sentence, medium three, long five. Every '
          'specific attribute in the description comes from the facts you pass '
          'in, so a description with no facts describes who the product is for '
          'and nothing more.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-box-open',
      capability='Product description writing',
      parameters=_schema({
          'product': _text('Product name.'),
          'length': _enum('How long.', ('short', 'medium', 'long')),
          'audience': _text('Who it is written for.'),
          'facts': _FACTS_ARGUMENT,
      }, required=('product',)))
def generate_product_description(ctx, product, length='medium', audience='',
                                 facts=None):
    name, changes = soften(product)
    rows = normalise_facts(facts)
    body = compose_description(name, length, audience, rows)
    piece = create_piece(
        ctx, channel='website', kind='product_description', body=body,
        title=_truncate(name, 120), audience=audience, facts=rows)
    notes = [f'Length requested: {_flatten(length) or "medium"}.']
    if changes:
        notes.append('Banned wording in the product name was replaced: '
                     + '; '.join(changes) + '.')
    return piece_result(piece, rows, notes)


@tool(name='mkt.generate_advertisement', title='Write an advertisement',
      description=(
          'Write an advertisement -- headline, body and call to action -- and '
          'save it as a content piece, together with the character limits it '
          'was written to. Ad copy is where a superlative does the most damage, '
          'so anything specific in it must come from the facts you pass in.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-rectangle-ad',
      capability='Advertisement copywriting',
      parameters=_schema({
          'product': _text('What is being advertised.'),
          'platform': _enum('Where the ad runs.', tuple(CHANNEL_LIMITS)),
          'objective': _enum('What the ad is for.',
                             ('awareness', 'consideration', 'conversion', 'retention')),
          'facts': _FACTS_ARGUMENT,
      }, required=('product',)))
def generate_advertisement(ctx, product, platform='linkedin', objective='awareness',
                           facts=None):
    key = _flatten(platform).lower()
    limits = CHANNEL_LIMITS.get(key, CHANNEL_LIMITS['ad'])
    name, changes = soften(product)
    rows = normalise_facts(facts)

    headline_limit = 70
    body_limit = min(limits.get('characters') or 600, 600)

    lead = (_sentence(rows[0]['fact']) if rows
            else _sentence(f'{_cap(name)}, explained in one line'))
    headline = _truncate(lead.rstrip('.'), headline_limit)
    action = {'awareness': 'Read the one-page overview.',
              'consideration': 'Compare it against what you run today.',
              'conversion': 'Start a trial and use it on real work this week.',
              'retention': 'See what changed in the latest release.'}.get(
                  _flatten(objective).lower(), 'Read the one-page overview.')

    body_lines = [_sentence(f'{_cap(name)} is used for real work, '
                            'not demonstrations')]
    for row in rows[:3]:
        body_lines.append(_sentence(row['fact']))
    body = _truncate(' '.join(body_lines), body_limit)

    copy = '\n'.join([f'Headline: {headline}', '', body, '',
                      f'Call to action: {action}'])
    piece = create_piece(
        ctx, channel=key if key in CHANNEL_LIMITS else 'ad', kind='ad', body=copy,
        title=_truncate(headline, 120), cta=action, facts=rows)

    notes = [
        f'Written to a {headline_limit}-character headline and a '
        f'{body_limit}-character body. The headline is {len(headline)} '
        f'characters and the body is {len(body)}.',
        f'Objective: {_flatten(objective) or "awareness"}.',
    ]
    if changes:
        notes.append('Banned wording was replaced: ' + '; '.join(changes) + '.')
    superlatives = superlatives_in(copy)
    if superlatives:
        notes.append('Unsubstantiated superlatives present: '
                     + ', '.join(superlatives)
                     + '. Either source them or take them out before publishing.')
    return piece_result(piece, rows, notes,
                        {'headline': headline, 'headline_limit': headline_limit,
                         'body_limit': body_limit,
                         'objective': _flatten(objective)})


@tool(name='mkt.generate_promotional_content', title='Write promotional copy',
      description=(
          'Write promotional copy for an offer on one channel, at a chosen '
          'urgency, and save it as a content piece. Urgency changes the close, '
          'not the claims: a deadline you were not given is never invented, so '
          'pass the real dates in as facts if the copy needs them.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-tags',
      capability='Promotional copywriting',
      parameters=_schema({
          'offer': _text('What is being offered.'),
          'channel': _enum('Where it will run.', tuple(CHANNEL_LIMITS)),
          'urgency': _enum('How urgent the close should read.',
                           ('low', 'normal', 'high')),
          'facts': _FACTS_ARGUMENT,
      }, required=('offer',)))
def generate_promotional_content(ctx, offer, channel='email', urgency='normal',
                                 facts=None):
    key = _flatten(channel).lower()
    if key not in CHANNEL_LIMITS:
        return _fail(f'"{channel}" is not a channel this platform writes for. '
                     f'Choose one of: {", ".join(CHANNEL_LIMITS)}.')

    subject, changes = soften(offer)
    rows = normalise_facts(facts)
    level = _flatten(urgency).lower()
    tone = {'low': 'plain', 'normal': 'professional', 'high': 'urgent'}.get(
        level, 'professional')
    close = {
        'low': 'Have a look when it suits you.',
        'normal': 'Reply if you want it and we will set it up.',
        'high': 'Reply this week if you want it in place before the month closes.',
    }.get(level, 'Reply if you want it and we will set it up.')

    tags = (_tag_list(build_hashtags(subject, key, HOUSE_HASHTAG_LIMITS.get(key, 5)))
            if key in ('linkedin', 'instagram', 'facebook', 'twitter') else [])

    if key == 'email':
        kind = 'announcement'
        body = compose_email_body(subject, '', subject, rows, tone, close)
    elif key == 'twitter':
        kind = 'post'
        body = compose_short_post(subject, '', tone, rows, tags, close)
    else:
        kind = 'announcement'
        body = compose_long_form(subject, '', tone, rows, cta=close)

    piece = create_piece(
        ctx, channel=key, kind=kind, body=body,
        title=f'Promotion: {_truncate(subject, 60)}', hashtags=tags,
        cta=close, tone=tone, facts=rows)

    notes = [f'Urgency "{level or "normal"}" changed the close only. No deadline '
             'was invented; if there is one, pass it in as a fact.']
    if changes:
        notes.append('Banned wording in the offer was replaced: '
                     + '; '.join(changes) + '.')
    return piece_result(piece, rows, notes)


@tool(name='mkt.generate_blog_ideas', title='Suggest blog ideas',
      description=(
          'Suggest blog article ideas for a topic. Each idea names its angle, '
          'the reader it serves and a working title, because an idea without a '
          'reader is a title nobody commissioned. Nothing is saved -- pass a '
          'working title to mkt.generate_blog_content to draft one.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-lightbulb',
      reads_only=True, capability='Blog ideation',
      parameters=_schema({
          'topic': _text('Subject area to generate ideas within.'),
          'count': _integer('How many ideas. Eight by default.'),
          'audience': _text('Who the blog is written for.'),
      }, required=('topic',)))
def generate_blog_ideas(ctx, topic, count=8, audience=''):
    subject, _changes = soften(topic)
    flat = _lower(subject)
    who = _flatten(audience) or 'operations and marketing leads'
    wanted = max(1, min(_int_or_none(count) or 8, 20))

    angles = [
        ('The beginner explanation',
         f'somebody who has just been handed {flat} and has no context',
         f'What {_cap(flat)} Actually Involves'),
        ('The mistake list',
         f'a team already doing {flat} badly and not sure why',
         f'Five Ways {_cap(flat)} Goes Wrong'),
        ('The comparison',
         'a buyer choosing between two approaches',
         f'{_cap(flat)}: Two Approaches, Compared Honestly'),
        ('The cost breakdown',
         'a manager who has to justify the spend',
         f'What {_cap(flat)} Costs, Line by Line'),
        ('The case study',
         'a sceptic who wants evidence rather than argument',
         f'How One Team Changed Their {_cap(flat)}'),
        ('The checklist',
         'somebody about to start and wanting a plan',
         f'A Working Checklist for {_cap(flat)}'),
        ('The contrarian take',
         'a reader tired of the standard advice',
         f'Most Advice About {_cap(flat)} Assumes You Have Time'),
        ('The process walk-through', who,
         f'{_cap(flat)}, From First Step to Last'),
        ('The metrics piece',
         'a reader who has to report on it',
         f'What to Measure in {_cap(flat)}, and What to Ignore'),
        ('The tooling review',
         'a team choosing what to buy',
         f'Choosing Tools for {_cap(flat)}'),
    ]

    lines = [f'{min(wanted, len(angles))} blog ideas on "{_flatten(subject)}", '
             f'written for {who}:', '']
    payload = []
    for index, (angle, reader, working_title) in enumerate(angles[:wanted], start=1):
        lines.append(f'{index}. {working_title}')
        lines.append(f'   Angle: {angle}.')
        lines.append(f'   Reader it serves: {reader}.')
        lines.append('')
        payload.append({'title': working_title, 'angle': angle, 'reader': reader})
    lines.append('None of these are saved. Call mkt.generate_blog_content with a '
                 'working title to draft one, and get the facts first.')
    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'topic': _flatten(subject), 'ideas': payload})


@tool(name='mkt.generate_blog_content', title='Draft a blog article',
      description=(
          'Draft a structured blog article with real headings and save it as a '
          'content piece. Supply an outline and those become the headings; '
          'without one the structure is the problem, the approach, the effort '
          'it takes and where it goes wrong. Each section states roughly how '
          'many words belong there so a writer can expand it with detail they '
          'can source.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-file-lines',
      capability='Blog article drafting',
      parameters=_schema({
          'title': _text('Working title of the article.'),
          'outline': _text('Section headings, one per line or separated by semicolons.'),
          'word_target': _integer('Roughly how long the finished piece should be.'),
          'facts': _FACTS_ARGUMENT,
      }, required=('title',)))
def generate_blog_content(ctx, title, outline='', word_target=700, facts=None):
    heading, changes = soften(title)
    rows = normalise_facts(facts)
    body = compose_article(heading, outline, rows, word_target=word_target)
    piece = create_piece(ctx, channel='blog', kind='article', body=body,
                         title=_truncate(heading, 200), facts=rows)

    target = _int_or_none(word_target) or 700
    notes = [f'Drafted at {piece.word_count} words against a target of {target}. '
             'The section notes say where to expand.']
    if changes:
        notes.append('Banned wording in the title was replaced: '
                     + '; '.join(changes) + '.')
    return piece_result(piece, rows, notes, {'word_target': target})


@tool(name='mkt.rewrite_for_audience', title='Rewrite copy for an audience',
      description=(
          'Rewrite an existing content piece for a different audience or tone '
          'and save the result as a linked variant, so the original survives '
          'and the two can be compared. Pass a content_id to rewrite a saved '
          'piece, or text to rewrite copy you were given. The facts recorded '
          'on the original carry across to the variant.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-pen-to-square',
      capability='Audience rewriting',
      parameters=_schema({
          'content_id': _integer('The piece to rewrite.'),
          'text': _text('Copy to rewrite, when it is not a saved piece.'),
          'audience': _text('Who the rewrite is for.'),
          'tone': _enum('Register of the rewrite.', _TONE_KEYS),
      }))
def rewrite_for_audience(ctx, content_id=None, text='', audience='', tone=''):
    original = None
    if _int_or_none(content_id) is not None:
        original, failure = find_piece(content_id)
        if failure is not None:
            return failure

    source_text = _flatten(text) or (original.body if original is not None else '')
    if not source_text:
        return _fail('Nothing to rewrite. Give a content_id of a saved piece, or '
                     'pass the copy itself as text.')

    rows = normalise_facts(original.sources) if original is not None else []
    if not rows:
        # No provenance to inherit, so the source sentences become the facts and
        # are recorded as unsourced, which is exactly what they are.
        rows = normalise_facts([line for line in source_text.split('\n')
                                if _flatten(line) and not _flatten(line).startswith('#')][:4])

    channel = original.channel if original is not None else 'linkedin'
    tone = tone or (original.tone if original is not None else 'professional')
    subject = (original.title if original is not None and original.title
               else _truncate(source_text, 60))

    if channel == 'instagram':
        body = compose_caption(subject, audience, tone, rows)
    elif channel == 'twitter':
        body = compose_short_post(subject, audience, tone, rows,
                                  original.hashtags if original is not None else ())
    elif channel == 'email':
        body = compose_email_body(subject, audience, '', rows, tone)
    else:
        body = compose_long_form(subject, audience, tone, rows)

    label = f'audience: {_flatten(audience) or "unchanged"}'
    if _flatten(tone):
        label += f'; tone: {_flatten(tone)}'

    piece = create_piece(
        ctx, channel=channel,
        kind=original.kind if original is not None else 'post',
        body=body, title=f'Rewrite: {_truncate(subject, 50)}',
        hashtags=original.hashtags if original is not None else (),
        cta=_cta_line(tone), audience=audience, tone=tone,
        campaign=original.campaign if original is not None else None,
        brief=original.brief if original is not None else None,
        facts=rows, variant_of=original, variant_label=label)

    notes = []
    if original is not None:
        notes.append(f'Saved as a variant of content piece #{original.pk}, which '
                     'is unchanged. Compare the two before choosing.')
    else:
        notes.append('There was no saved original, so this is a standalone piece '
                     'rather than a variant.')
    return piece_result(piece, rows, notes,
                        {'variant_of': original.pk if original is not None else None})


@tool(name='mkt.generate_variants', title='Generate A/B variants',
      description=(
          'Produce several genuinely different versions of a saved content '
          'piece, each recorded as a linked variant with variant_label saying '
          'what was varied. Vary the hook, the call to action, the length or '
          'the tone. Labelling the difference is the point: an A/B test '
          'between two versions nobody can describe teaches nothing.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-clone',
      capability='A/B variant generation',
      parameters=_schema({
          'content_id': _integer('The piece to build variants from.'),
          'count': _integer('How many variants. Three by default.'),
          'vary': _enum('What to change between them.',
                        ('hook', 'cta', 'length', 'tone')),
      }, required=('content_id',)))
def generate_variants(ctx, content_id, count=3, vary='hook'):
    original, failure = find_piece(content_id)
    if failure is not None:
        return failure

    wanted = max(1, min(_int_or_none(count) or 3, 6))
    dimension = _flatten(vary).lower() or 'hook'
    rows = normalise_facts(original.sources)
    subject = original.title or _truncate(original.body, 60)

    if dimension == 'hook':
        settings = [(style, f'hook: {style} -- {HOOK_REASONS[style]}')
                    for style in HOOK_STYLES]
    elif dimension == 'cta':
        settings = [(key, f'cta: {key} close -- "{_TONE_CTA[key]}"')
                    for key in ('professional', 'friendly', 'confident', 'urgent')]
    elif dimension == 'length':
        settings = [(size, f'length: {size} -- '
                           f'{_LENGTH_FACTS[size]} supporting points')
                    for size in ('short', 'medium', 'long')]
    elif dimension == 'tone':
        settings = [(key, f'tone: {key}') for key in
                    ('professional', 'friendly', 'plain', 'confident')]
    else:
        return _fail(f'"{vary}" is not something this tool can vary. Choose hook, '
                     'cta, length or tone.')

    created = []
    for value, label in settings[:wanted]:
        hook = value if dimension == 'hook' else 'direct'
        tone = value if dimension == 'tone' else (original.tone or 'professional')
        cta = _TONE_CTA[value] if dimension == 'cta' else ''
        length = value if dimension == 'length' else 'medium'

        if original.channel == 'instagram':
            body = compose_caption(subject, original.audience, tone, rows,
                                   hook=hook, cta=cta)
        elif original.channel == 'twitter':
            body = compose_short_post(subject, original.audience, tone, rows,
                                      original.hashtags, cta)
        else:
            body = compose_long_form(subject, original.audience, tone, rows,
                                     hook=hook, cta=cta, length=length)

        created.append(create_piece(
            ctx, channel=original.channel, kind=original.kind, body=body,
            title=f'{original.title or "Variant"} [{value}]',
            hashtags=original.hashtags, cta=cta or _cta_line(tone),
            audience=original.audience, tone=tone, campaign=original.campaign,
            brief=original.brief, facts=rows, variant_of=original,
            variant_label=label))

    lines = [f'{len(created)} variants of content piece #{original.pk}, varying '
             f'the {dimension}. The original is unchanged.', '']
    for piece in created:
        fits, message = piece.fits_channel
        lines.append(f'--- variant #{piece.pk}: {piece.variant_label} ---')
        lines.append(piece.full_text)
        lines.append(('Within limits. ' if fits else 'OVER THE LIMIT. ') + message)
        lines.append('')
    lines.append(_facts_note(rows))
    lines.append('Choose one and publish it with the relevant publish tool, which '
                 'needs approval.')
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'original_id': original.pk, 'varied': dimension,
              'variants': [{'content_id': p.pk, 'label': p.variant_label,
                            'characters': p.character_count} for p in created]},
        subject_label='marketing.contentpiece', subject_id=original.pk)


@tool(name='mkt.generate_product_announcement', title='Announce a product',
      description=(
          'Write a product announcement as one content piece per channel, each '
          'written for that channel rather than the same text pasted four '
          'times. An announcement is the piece most likely to carry a claim '
          'about what the product now does, so pass what is new and the facts '
          'behind it. Nothing is published.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-bullhorn',
      capability='Product announcements',
      parameters=_schema({
          'product': _text('Product being announced.'),
          'what_is_new': _text('What actually changed.'),
          'audience': _text('Who the announcement is for.'),
          'channels': _array('Channel keys to write for. LinkedIn and email by default.'),
          'facts': _FACTS_ARGUMENT,
      }, required=('product',)))
def generate_product_announcement(ctx, product, what_is_new='', audience='',
                                  channels=None, facts=None):
    name, changes = soften(product)
    detail, more_changes = soften(what_is_new)
    changes = changes + more_changes
    rows = normalise_facts(facts)

    wanted = [_flatten(key).lower() for key in (channels or ['linkedin', 'email'])
              if _flatten(key)]
    unknown = [key for key in wanted if key not in CHANNEL_LIMITS]
    if unknown:
        return _fail(f'These are not channels this platform writes for: '
                     f'{", ".join(unknown)}. Choose from: {", ".join(CHANNEL_LIMITS)}.')
    if not wanted:
        wanted = ['linkedin', 'email']

    subject = f'{_cap(name)}: {_lower(detail)}' if _flatten(detail) else _cap(name)
    created = []
    for key in wanted:
        tone = 'friendly' if key == 'instagram' else 'professional'
        tags = (_tag_list(build_hashtags(name, key, HOUSE_HASHTAG_LIMITS.get(key, 5)))
                if key in ('linkedin', 'instagram', 'facebook', 'twitter') else [])
        if key == 'instagram':
            body = compose_caption(subject, audience, tone, rows)
        elif key == 'twitter':
            body = compose_short_post(subject, audience, tone, rows, tags)
        elif key == 'email':
            body = compose_email_body(f'what is new in {_lower(name)}', audience,
                                      detail, rows, tone)
        elif key == 'blog':
            body = compose_article(subject, '', rows, audience, tone)
        else:
            body = compose_long_form(subject, audience, tone, rows, length='long')
        created.append(create_piece(
            ctx, channel=key, kind='announcement', body=body,
            title=f'{channel_label(key)} announcement: {_truncate(name, 50)}',
            hashtags=tags, cta=_cta_line(tone), audience=audience, tone=tone,
            facts=rows))

    lines = [f'{len(created)} announcement pieces for {_cap(name)}, one per '
             'channel, each written for that channel. None is published.', '']
    for piece in created:
        fits, message = piece.fits_channel
        lines.append(f'--- {channel_label(piece.channel)} (piece #{piece.pk}) ---')
        lines.append(piece.full_text)
        lines.append(('Within limits. ' if fits else 'OVER THE LIMIT. ') + message)
        lines.append('')
    lines.append(_facts_note(rows))
    if changes:
        lines.append('Banned wording was replaced: ' + '; '.join(changes) + '.')
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'product': _flatten(name),
              'pieces': [{'content_id': p.pk, 'channel': p.channel,
                          'characters': p.character_count} for p in created]})


@tool(name='mkt.list_content', title='List content pieces',
      description=(
          'List saved content pieces, newest first, optionally filtered by '
          'channel, status or campaign. Use this before quoting a content id: '
          'an invented id is the most common reason a publish tool fails.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-list',
      reads_only=True, capability='Content library listing',
      parameters=_schema({
          'channel': _enum('Only this channel.', tuple(CHANNEL_LIMITS)),
          'status': _enum('Only this status.',
                          ('draft', 'pending', 'approved', 'scheduled',
                           'published', 'rejected')),
          'campaign_id': _integer('Only pieces on this campaign.'),
          'limit': _integer('How many rows. Twenty-five by default.'),
      }))
def list_content(ctx, channel='', status='', campaign_id=None, limit=25):
    query = ContentPiece.objects.all().select_related('campaign')
    described = []

    if _flatten(channel):
        key = _flatten(channel).lower()
        if key not in CHANNEL_LIMITS:
            return _fail(f'"{channel}" is not a channel. Choose from: '
                         f'{", ".join(CHANNEL_LIMITS)}.')
        query = query.filter(channel=key)
        described.append(channel_label(key))

    if _flatten(status):
        key = _flatten(status).lower()
        valid = [value for value, _label in ContentPiece.STATUS_CHOICES]
        if key not in valid:
            return _fail(f'"{status}" is not a content status. Choose from: '
                         f'{", ".join(valid)}.')
        query = query.filter(status=key)
        described.append(key)

    if _int_or_none(campaign_id) is not None:
        campaign, failure = find_campaign(campaign_id)
        if failure is not None:
            return failure
        query = query.filter(campaign=campaign)
        described.append(f'campaign #{campaign.pk}')

    rows = list(query[:max(1, min(_int_or_none(limit) or 25, 100))])
    scope = ' and '.join(described) or 'all channels and statuses'
    if not rows:
        return ToolResult(ok=True, data={'count': 0, 'pieces': []},
                          text=f'No content pieces match {scope}. Use one of the '
                               'generate tools to write something.')

    lines = [f'{len(rows)} content pieces ({scope}), newest first:', '']
    for piece in rows:
        fits, _message = piece.fits_channel
        flag = '' if fits else '  [over the channel limit]'
        campaign = f' campaign #{piece.campaign_id}' if piece.campaign_id else ''
        variant = f' variant of #{piece.variant_of_id}' if piece.variant_of_id else ''
        lines.append(f'#{piece.pk} [{channel_label(piece.channel)}/{piece.kind}] '
                     f'{piece.get_status_display()}{campaign}{variant} -- '
                     f'{piece.character_count} chars{flag}')
        lines.append(f'    {piece.preview}')
    lines.append('')
    lines.append('Call mkt.get_content with an id for the full copy.')
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'count': len(rows),
              'pieces': [{'content_id': p.pk, 'channel': p.channel,
                          'kind': p.kind, 'status': p.status,
                          'title': p.title, 'characters': p.character_count}
                         for p in rows]})


@tool(name='mkt.get_content', title='Read one content piece',
      description=(
          'Read one saved content piece in full: the copy, the hashtags, the '
          'channel limits, the documents its facts came from, and any variants '
          'of it. Read a piece before publishing it, because the copy may have '
          'been edited since it was written.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-file-lines',
      reads_only=True, capability='Content retrieval',
      parameters=_schema({'content_id': _integer('The piece to read.')},
                         required=('content_id',)))
def get_content(ctx, content_id):
    piece, failure = find_piece(content_id)
    if failure is not None:
        return failure

    fits, message = piece.fits_channel
    lines = [
        f'Content piece #{piece.pk} -- {channel_label(piece.channel)} '
        f'{piece.get_kind_display().lower()}, {piece.get_status_display().lower()}.',
        f'Title: {piece.title or "(none)"}',
        f'Audience: {piece.audience or "not recorded"}   '
        f'Tone: {piece.tone or "not recorded"}',
    ]
    if piece.campaign_id:
        lines.append(f'Campaign: #{piece.campaign_id} {piece.campaign.name}')
    if piece.variant_of_id:
        lines.append(f'Variant of #{piece.variant_of_id}: {piece.variant_label}')
    if piece.scheduled_for:
        lines.append(f'Scheduled for {piece.scheduled_for:%d %b %Y at %H:%M}.')
    if piece.is_published:
        lines.append(f'Published {piece.published_at:%d %b %Y at %H:%M}'
                     + (f', reference {piece.external_reference}'
                        if piece.external_reference else '')
                     + (f', {piece.external_url}' if piece.external_url else '') + '.')

    lines.extend(['', '--- copy ---', piece.full_text, '--- end of copy ---', ''])
    lines.append(('Within limits. ' if fits else 'OVER THE LIMIT. ') + message)

    documents = piece.source_documents
    if piece.sources:
        lines.append(f'Built from {len(piece.sources)} recorded facts.'
                     + (' Documents: ' + ', '.join(documents) + '.' if documents else
                        ' None of them names a document, so every claim in this '
                        'copy is unsourced.'))
    else:
        lines.append('No sources are recorded, so this copy asserts nothing that '
                     'was supplied to it. Check it for figures before publishing.')

    variants = list(piece.variants.all()[:10])
    if variants:
        lines.append('')
        lines.append('Variants: ' + '; '.join(
            f'#{v.pk} ({v.variant_label or "unlabelled"})' for v in variants))

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'content_id': piece.pk, 'channel': piece.channel,
              'kind': piece.kind, 'status': piece.status, 'body': piece.body,
              'hashtags': piece.hashtags, 'sources': piece.sources,
              'fits_channel': fits, 'limit_message': message,
              'variants': [v.pk for v in variants]},
        subject_label='marketing.contentpiece', subject_id=piece.pk)


@tool(name='mkt.update_content', title='Edit a content piece',
      description=(
          'Edit a saved content piece: its body, its title, its hashtags or its '
          'status. Only the arguments you pass are changed. Editing the body '
          'does not change the recorded sources, so run '
          'mkt.check_brand_compliance afterwards if you added a figure.'),
      group='Content Creation', agent_types=AGENTS, icon='fa-pen',
      capability='Content editing',
      parameters=_schema({
          'content_id': _integer('The piece to edit.'),
          'body': _text('Replacement copy.'),
          'title': _text('Replacement title.'),
          'hashtags': _array('Replacement hashtags, with or without the hash.'),
          'status': _enum('New status.',
                          ('draft', 'pending', 'approved', 'rejected')),
      }, required=('content_id',)))
def update_content(ctx, content_id, body='', title='', hashtags=None, status=''):
    piece, failure = find_piece(content_id)
    if failure is not None:
        return failure

    if piece.is_published:
        return _fail(f'Content piece #{piece.pk} is already published, so editing '
                     'it here would make the record disagree with what is public. '
                     'Write a new piece instead.')

    changed = []
    if _flatten(body):
        piece.body = str(body)
        changed.append('body')
    if _flatten(title):
        piece.title = _flatten(title)[:250]
        changed.append('title')
    if hashtags is not None:
        piece.hashtags = [str(tag).lstrip('#') for tag in hashtags
                          if str(tag).strip()]
        changed.append('hashtags')
    if _flatten(status):
        key = _flatten(status).lower()
        allowed = ('draft', 'pending', 'approved', 'rejected')
        if key not in allowed:
            return _fail(f'"{status}" cannot be set here. Choose from '
                         f'{", ".join(allowed)}. Scheduled, published and sent '
                         'are set by an executor after approval, never by hand.')
        piece.status = key
        changed.append('status')

    if not changed:
        return _fail(f'Nothing was changed on content piece #{piece.pk}: no body, '
                     'title, hashtags or status was given.')

    piece.recount()
    piece.save()

    notes = [f'Changed: {", ".join(changed)}.']
    if 'body' in changed and piece.sources:
        notes.append('The recorded sources were left as they were. Run '
                     'mkt.check_brand_compliance if the new copy adds a figure.')
    return piece_result(piece, normalise_facts(piece.sources), notes)


# ===========================================================================
# GROUP: Campaigns
# ===========================================================================

def latest_brief(campaign):
    """The most recent brief on a campaign, or None."""
    return campaign.briefs.order_by('-created_at').first() if campaign else None


@tool(name='mkt.create_campaign', title='Create a campaign',
      description=(
          'Create a marketing campaign and its brief. The campaign carries the '
          'name, objective, audience, budget and dates; the brief carries the '
          'key message, the tone, the channels and the proof points. A campaign '
          'without a brief cannot answer who it is for or what would count as '
          'it having worked, so both are written together.'),
      group='Campaigns', agent_types=AGENTS, icon='fa-flag',
      capability='Campaign creation',
      parameters=_schema({
          'name': _text('Campaign name.'),
          'objective': _text('What the campaign is for, in judgeable terms.'),
          'audience': _text('Who it is aimed at.'),
          'channels': _array('Channel keys it will run on.'),
          'start_date': _text('Start date, YYYY-MM-DD.'),
          'end_date': _text('End date, YYYY-MM-DD.'),
          'budget': _text('Budget as a number.'),
          'key_message': _text('The single thing a reader should carry away.'),
          'tone': _enum('Register for the whole campaign.', _TONE_KEYS),
      }, required=('name',)))
def create_campaign(ctx, name, objective='', audience='', channels=None,
                    start_date=None, end_date=None, budget=None, key_message='',
                    tone=''):
    title = _flatten(name)
    if not title:
        return _fail('A campaign needs a name. Give one that names the thing '
                     'being promoted rather than the quarter.')

    wanted = [_flatten(key).lower() for key in (channels or []) if _flatten(key)]
    unknown = [key for key in wanted if key not in CHANNEL_LIMITS]
    if unknown:
        return _fail(f'These are not channels this platform writes for: '
                     f'{", ".join(unknown)}. Choose from: {", ".join(CHANNEL_LIMITS)}.')
    if not wanted:
        wanted = ['linkedin', 'email']

    starts = _parse_date(start_date, timezone.localdate())
    ends = _parse_date(end_date)
    if ends is not None and starts is not None and ends < starts:
        return _fail(f'The end date {ends:%d %b %Y} is before the start date '
                     f'{starts:%d %b %Y}. Give dates the campaign could actually run on.')

    amount = None
    if budget not in (None, ''):
        try:
            amount = Decimal(str(budget).replace(',', '').replace('$', '').strip())
        except (InvalidOperation, ValueError):
            return _fail(f'"{budget}" is not a number this platform can store as a '
                         'budget. Give it as a plain figure, e.g. 5000.')

    campaign = MarketingCampaign(
        name=title[:200],
        objective=_flatten(objective) or f'Promote {title}.',
        target_audience=_flatten(audience)[:200] or 'not yet defined',
        status='draft', assigned_agent=ctx.agent,
        start_date=starts, end_date=ends)
    if amount is not None:
        campaign.budget = amount
    if getattr(ctx.user, 'pk', None):
        campaign.user = ctx.user
    campaign.save()

    brief = CampaignBrief.objects.create(
        campaign=campaign,
        objective=_flatten(objective),
        audience=_flatten(audience)[:250],
        key_message=_flatten(key_message),
        channels=wanted,
        tone=_flatten(tone)[:60] or 'professional',
        proof_points=[],
        success_measures=[],
        start_date=starts, end_date=ends,
        status='draft', created_by_agent=ctx.agent)

    lines = [
        f'Campaign #{campaign.pk} "{campaign.name}" created as a draft, with '
        f'brief #{brief.pk}.',
        f'Objective: {campaign.objective}',
        f'Audience: {campaign.target_audience}',
        f'Channels: {", ".join(brief.channel_labels)}',
        f'Runs from {starts:%d %b %Y}'
        + (f' to {ends:%d %b %Y}.' if ends else ', with no end date set.'),
        f'Budget: {campaign.budget}',
    ]
    if brief.key_message:
        lines.append(f'Key message: {brief.key_message}')
    else:
        lines.append('No key message is recorded yet. A campaign with three '
                     'messages has none, so set one before any copy is written.')
    lines.append('')
    lines.append('The brief has no proof points yet, which means every claim the '
                 'campaign makes is currently unsourced. Call '
                 'mkt.get_product_facts and mkt.generate_campaign_strategy next.')
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'campaign_id': campaign.pk, 'brief_id': brief.pk,
              'channels': wanted, 'start_date': str(starts),
              'end_date': str(ends) if ends else None},
        subject_label='marketing.marketingcampaign', subject_id=campaign.pk)


@tool(name='mkt.generate_campaign_strategy', title='Write a campaign strategy',
      description=(
          'Write the strategy for a campaign: the positioning, the message per '
          'channel, the sequence it runs in, and what would count as it having '
          'worked. Saved onto the campaign brief when a campaign_id is given, '
          'so later work can be checked against it rather than against '
          'somebody\'s memory of a conversation.'),
      group='Campaigns', agent_types=AGENTS, icon='fa-chess',
      capability='Campaign strategy',
      parameters=_schema({
          'campaign_id': _integer('Campaign to write the strategy for.'),
          'objective': _text('Objective, when there is no campaign yet.'),
          'audience': _text('Audience, when there is no campaign yet.'),
          'channels': _array('Channel keys the campaign will run on.'),
      }))
def generate_campaign_strategy(ctx, campaign_id=None, objective='', audience='',
                               channels=None):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure

    brief = latest_brief(campaign)
    goal = (_flatten(objective) or (brief.objective if brief else '')
            or (campaign.objective if campaign else ''))
    who = (_flatten(audience) or (brief.audience if brief else '')
           or (campaign.target_audience if campaign else ''))
    if not goal:
        return _fail('A strategy needs an objective. Give one, or pass a '
                     'campaign_id whose brief already has one.')

    wanted = [_flatten(key).lower() for key in (channels or []) if _flatten(key)]
    if not wanted and brief is not None:
        wanted = list(brief.channels or [])
    unknown = [key for key in wanted if key not in CHANNEL_LIMITS]
    if unknown:
        return _fail(f'These are not channels this platform writes for: '
                     f'{", ".join(unknown)}.')
    if not wanted:
        wanted = ['linkedin', 'email']

    who = who or 'the teams we work with'
    positioning = _sentence(
        f'For {who}, the campaign argues one thing: {_lower(goal)}. Everything '
        'published under it either supports that or does not run')

    channel_messages = []
    for key in wanted:
        angle = {
            'linkedin': 'the operational case, aimed at the person who owns the '
                        'problem and has to justify fixing it',
            'instagram': 'the human side, aimed at people who will recognise '
                         'themselves in it rather than evaluate it',
            'twitter': 'one line that survives being read at speed',
            'facebook': 'the community angle, aimed at existing customers first',
            'email': 'the detailed case, aimed at somebody who already knows who '
                     'we are and wants the specifics',
            'blog': 'the long explanation people can send to a colleague',
            'ad': 'the single benefit, stated inside the character limit',
            'press': 'the factual account, written so a journalist can quote it',
            'website': 'the reference version everything else links back to',
        }.get(key, 'the general case')
        channel_messages.append((key, angle))

    sequence = [
        'Week 1 -- publish the reference version first (website or blog), so '
        'every later post has somewhere to point.',
        'Week 2 -- the operational case on the primary social channel, then the '
        'detailed email to people who already know us.',
        'Week 3 -- the human and community angles, which land better once the '
        'argument is already in circulation.',
        'Week 4 -- one piece of proof: a result, a customer account, a number '
        'you can source. If there is none, do not invent one; report that '
        'instead and shorten the campaign.',
    ]

    measures = [
        f'Reach on the primary channel, compared against the previous {len(wanted)} '
        'comparable posts rather than against zero.',
        'Replies and direct messages, which are the only engagement that turns '
        'into a conversation.',
        'Email open and click rates against the segment average.',
        'Number of published pieces whose claims are all sourced. A campaign that '
        'hit its reach target on unsourced claims has not worked.',
    ]

    lines = ['POSITIONING', positioning, '', 'MESSAGE PER CHANNEL']
    for key, angle in channel_messages:
        lines.append(f'{channel_label(key)}: {angle}.')
    lines.extend(['', 'SEQUENCE'] + sequence)
    lines.extend(['', 'WHAT WOULD COUNT AS THIS HAVING WORKED'])
    lines.extend(f'- {measure}' for measure in measures)

    if brief is not None:
        brief.objective = goal
        brief.audience = who[:250]
        brief.channels = wanted
        brief.key_message = brief.key_message or positioning
        brief.success_measures = measures
        brief.constraints = (brief.constraints
                             or 'No superlative may be published without a source. '
                                'Five hashtags at most, twelve on Instagram.')
        brief.save()
        lines.extend(['', f'Saved onto brief #{brief.pk} for campaign '
                          f'#{campaign.pk}. The success measures are now on the '
                          'record and can be checked against later.'])
        if brief.unsourced_claims:
            lines.append(f'{len(brief.unsourced_claims)} proof points on this brief '
                         'name no document. They are unsourced claims until they do.')
        elif not brief.proof_points:
            lines.append('The brief still has no proof points. Call '
                         'mkt.get_product_facts and record what you find before '
                         'any copy makes a claim.')
    else:
        lines.extend(['', 'No campaign_id was given, so nothing was saved. Create '
                          'the campaign with mkt.create_campaign and call this '
                          'again to put the strategy on the record.'])

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'campaign_id': campaign.pk if campaign else None,
              'brief_id': brief.pk if brief else None,
              'channels': wanted, 'measures': measures,
              'positioning': positioning},
        subject_label='marketing.campaignbrief' if brief else '',
        subject_id=brief.pk if brief else 0)


@tool(name='mkt.generate_campaign_tasks', title='List the work a campaign needs',
      description=(
          'List the work a campaign actually needs -- asset production, review, '
          'scheduling and reporting -- with an owner and a day offset for each. '
          'Returned as a list rather than filed anywhere: putting work on an '
          'engineering board is the Engineering Delivery employee\'s job, so '
          'hand these to that employee if they need tracking.'),
      group='Campaigns', agent_types=AGENTS, icon='fa-list-check',
      reads_only=True, capability='Campaign work planning',
      parameters=_schema({
          'campaign_id': _integer('Campaign the work belongs to.'),
          'count': _integer('How many items. Eight by default.'),
      }, required=('campaign_id',)))
def generate_campaign_tasks(ctx, campaign_id, count=8):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure
    if campaign is None:
        return _fail('A campaign_id is needed. Call mkt.create_campaign first.')

    brief = latest_brief(campaign)
    anchor = campaign.start_date or timezone.localdate()
    wanted = max(1, min(_int_or_none(count) or 8, 20))

    template = [
        ('Confirm the brief and source every proof point', 'Marketing', -10,
         'A claim without a document is the one thing that cannot be fixed after '
         'publication.'),
        ('Get the product facts from the knowledge base', 'Research', -9,
         'The generators only state what is passed into them, so this gates '
         'everything else.'),
        ('Produce the channel assets', 'Marketing', -7,
         'One piece per channel, written for that channel.'),
        ('Review the copy against the brand guidelines', 'Marketing', -5,
         'Banned words, hashtag counts, channel limits and unsourced figures.'),
        ('Human review and approval of each piece', 'Marketing lead', -4,
         'Nothing publishes without this; the approval queue enforces it.'),
        ('Build the content calendar and schedule the posts', 'Marketing', -3,
         'Scheduling is a commitment, so it goes through approval as well.'),
        ('Prepare the campaign email and its segment', 'Marketing', -2,
         'Check the subject against the 60-character inbox limit.'),
        ('Brief the team in Slack', 'Marketing', -1,
         'So support and sales are not surprised by what customers ask about.'),
        ('Publish the reference piece', 'Marketing', 0,
         'Everything else points at it, so it goes first.'),
        ('Mid-campaign check on reach and replies', 'Marketing', 7,
         'Against comparable previous posts, not against zero.'),
        ('Report results against the brief\'s success measures', 'Marketing', 21,
         'Including how many published pieces had every claim sourced.'),
        ('Archive the assets to Drive', 'Marketing', 24,
         'So the next campaign starts from what was already written.'),
    ]

    lines = [f'{min(wanted, len(template))} pieces of work for campaign '
             f'#{campaign.pk} "{campaign.name}", offset from the start date '
             f'{anchor:%d %b %Y}:', '']
    payload = []
    for index, (title, owner, offset, why) in enumerate(template[:wanted], start=1):
        when = anchor + timedelta(days=offset)
        sign = f'{offset:+d}'
        lines.append(f'{index}. {title}')
        lines.append(f'   Owner: {owner}. Day {sign} -- {when:%a %d %b %Y}.')
        lines.append(f'   Why: {why}')
        lines.append('')
        payload.append({'title': title, 'owner': owner, 'day_offset': offset,
                        'date': str(when), 'rationale': why})

    if brief is not None and brief.unsourced_claims:
        lines.append(f'Note: brief #{brief.pk} has {len(brief.unsourced_claims)} '
                     'unsourced proof points, so the first item is not optional.')
    lines.append('None of this was filed. This employee does not put work on an '
                 'engineering board -- ask the Engineering Delivery employee to '
                 'create the work items if they need tracking.')
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'campaign_id': campaign.pk, 'anchor_date': str(anchor),
              'tasks': payload},
        subject_label='marketing.marketingcampaign', subject_id=campaign.pk)


@tool(name='mkt.suggest_target_audiences', title='Suggest audience segments',
      description=(
          'Suggest audience segments for a product or campaign and save them as '
          'records, each with the criteria that define membership, the channels '
          'it is reachable on, and an estimated size. Every estimate states the '
          'basis for it, because a bare number in a segment record is read as a '
          'measurement and these are not measurements.'),
      group='Campaigns', agent_types=AGENTS, icon='fa-users',
      capability='Audience segmentation',
      parameters=_schema({
          'product': _text('Product or offer the segments are for.'),
          'campaign_id': _integer('Campaign the segments are for.'),
          'count': _integer('How many segments. Three by default.'),
      }))
def suggest_target_audiences(ctx, product='', campaign_id=None, count=3):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure

    brief = latest_brief(campaign)
    subject, _changes = soften(product)
    subject = _flatten(subject) or (campaign.name if campaign else '')
    if not subject:
        return _fail('A product or a campaign_id is needed, so the segments have '
                     'something to be segments of.')

    stated = (brief.audience if brief else '') or (campaign.target_audience
                                                   if campaign else '')
    wanted = max(1, min(_int_or_none(count) or 3, 6))

    blueprints = [
        {
            'name': f'{_cap(subject)} -- decision owners',
            'criteria': {'seniority': 'manager or above',
                         'responsibility': f'owns {_lower(subject)} day to day',
                         'company_size': '50-500 staff'},
            'channels': ['linkedin', 'email'],
            'basis': 'the narrowest of the three by construction, since it '
                     'requires both seniority and direct ownership. Sized at a '
                     'nominal 400 as a placeholder, not a count.',
            'size': 400,
        },
        {
            'name': f'{_cap(subject)} -- practitioners',
            'criteria': {'seniority': 'individual contributor',
                         'responsibility': f'does the {_lower(subject)} work itself',
                         'company_size': 'any'},
            'channels': ['linkedin', 'instagram', 'blog'],
            'basis': 'broader than the decision owners because every owner has '
                     'several practitioners under them. Sized at a nominal 1600, '
                     'four times the first, on that reasoning alone.',
            'size': 1600,
        },
        {
            'name': f'{_cap(subject)} -- existing customers',
            'criteria': {'relationship': 'current customer',
                         'usage': f'already using {_lower(subject)}'},
            'channels': ['email', 'facebook'],
            'basis': 'this one can be counted properly from the customer list, so '
                     'the placeholder of 0 is deliberate: replace it with the real '
                     'figure rather than an estimate.',
            'size': 0,
        },
        {
            'name': f'{_cap(subject)} -- lapsed enquiries',
            'criteria': {'relationship': 'enquired, did not buy',
                         'recency': 'within 18 months'},
            'channels': ['email'],
            'basis': 'countable from the CRM. The placeholder of 0 says so rather '
                     'than guessing.',
            'size': 0,
        },
        {
            'name': f'{_cap(subject)} -- adjacent industries',
            'criteria': {'industry': 'adjacent to the current customer base',
                         'problem': f'has the {_lower(subject)} problem in a '
                                    'different form'},
            'channels': ['linkedin', 'blog'],
            'basis': 'the least defensible of the set, because whether these '
                     'people have the problem at all is an assumption. Sized at a '
                     'nominal 800 and flagged as speculative.',
            'size': 800,
        },
    ]

    created = []
    for entry in blueprints[:wanted]:
        description = entry['basis']
        if stated:
            description = f'Derived from the stated audience "{stated}". ' + description
        created.append(AudienceSegment.objects.create(
            name=entry['name'][:160], description=description,
            criteria=entry['criteria'], estimated_size=entry['size'],
            channel_fit=entry['channels'], created_by_agent=ctx.agent,
            created_by=ctx.user if getattr(ctx.user, 'pk', None) else None))

    lines = [f'{len(created)} audience segments saved for "{_flatten(subject)}":', '']
    for segment in created:
        lines.append(f'#{segment.pk} {segment.name}')
        lines.append(f'   Criteria: {segment.criteria_summary}')
        lines.append(f'   Reachable on: {", ".join(segment.channel_labels)}')
        lines.append(f'   Estimated size: {segment.estimated_size} -- '
                     f'{segment.description}')
        lines.append('')
    lines.append('Every size above is an estimate or a placeholder, and the basis '
                 'is recorded with it. None of them came from a count of real '
                 'contacts, so do not quote them as figures.')
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'segments': [{'segment_id': s.pk, 'name': s.name,
                            'criteria': s.criteria,
                            'estimated_size': s.estimated_size,
                            'channel_fit': s.channel_fit} for s in created]})


@tool(name='mkt.generate_campaign_ideas', title='Suggest campaign ideas',
      description=(
          'Suggest campaign ideas for a product and objective. Each idea names '
          'the angle, who it is aimed at, the channels it suits and what would '
          'have to be true for it to work -- that last part is the useful one, '
          'because it is where an idea that needs evidence nobody has becomes '
          'obvious before any copy is written.'),
      group='Campaigns', agent_types=AGENTS, icon='fa-lightbulb',
      reads_only=True, capability='Campaign ideation',
      parameters=_schema({
          'product': _text('Product or service the campaign is for.'),
          'objective': _text('What the campaign should achieve.'),
          'count': _integer('How many ideas. Five by default.'),
      }))
def generate_campaign_ideas(ctx, product='', objective='', count=5):
    subject, _changes = soften(product)
    subject = _flatten(subject)
    if not subject:
        return _fail('A product or service is needed, so the ideas have something '
                     'to be about.')
    goal = _flatten(objective) or 'awareness among people who have the problem'
    wanted = max(1, min(_int_or_none(count) or 5, 10))

    ideas = [
        ('The problem series',
         f'one post per week on a single failure mode of {_lower(subject)}',
         'people already living with the problem',
         ['linkedin', 'blog'],
         'you can describe four distinct failure modes without inventing any'),
        ('The customer account',
         'one customer, one before and after, told in their words',
         'sceptics who want evidence rather than argument',
         ['linkedin', 'email', 'blog'],
         'a customer agrees to be named and the numbers can be sourced'),
        ('The teardown',
         f'take {_lower(subject)} apart in public and show how it works',
         'practitioners who distrust marketing',
         ['blog', 'linkedin'],
         'somebody technical has half a day to check the detail'),
        ('The comparison',
         'compare the two honest approaches, including where ours loses',
         'buyers actively choosing',
         ['blog', 'email'],
         'you are willing to publish the case where the alternative wins'),
        ('The behind the scenes',
         'how the work actually gets done, unpolished',
         'people who buy from teams rather than from products',
         ['instagram', 'facebook'],
         'the team is willing to be photographed and quoted'),
        ('The seasonal hook',
         f'tie {_lower(subject)} to the point in the year it actually bites',
         'anyone with the annual version of the problem',
         ['email', 'linkedin'],
         'the timing is real rather than manufactured'),
        ('The tool giveaway',
         'publish a checklist or template that is useful on its own',
         'people not yet ready to buy',
         ['blog', 'email', 'linkedin'],
         'the giveaway is genuinely useful without the product'),
    ]

    lines = [f'{min(wanted, len(ideas))} campaign ideas for "{subject}", '
             f'against the objective: {goal}.', '']
    payload = []
    for index, (name, angle, who, channels, condition) in enumerate(
            ideas[:wanted], start=1):
        lines.append(f'{index}. {name}')
        lines.append(f'   Angle: {angle}.')
        lines.append(f'   Aimed at: {who}.')
        lines.append(f'   Channels: {", ".join(channel_label(c) for c in channels)}.')
        lines.append(f'   Only works if: {condition}.')
        lines.append('')
        payload.append({'name': name, 'angle': angle, 'audience': who,
                        'channels': channels, 'precondition': condition})
    lines.append('Nothing was saved. Pick one and call mkt.create_campaign, which '
                 'writes the campaign and its brief together.')
    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'product': subject, 'objective': goal,
                            'ideas': payload})


@tool(name='mkt.generate_promotional_plan', title='Plan a promotion',
      description=(
          'Plan a promotion week by week across the channels it will run on: '
          'what goes out, in what order, and what has to be ready before each '
          'step. Returned as readable text and not saved; use '
          'mkt.create_content_calendar to turn a plan into real calendar rows.'),
      group='Campaigns', agent_types=AGENTS, icon='fa-calendar-week',
      reads_only=True, capability='Promotion planning',
      parameters=_schema({
          'offer': _text('What is being promoted.'),
          'weeks': _integer('How many weeks it runs. Two by default.'),
          'channels': _array('Channel keys it runs on.'),
      }, required=('offer',)))
def generate_promotional_plan(ctx, offer, weeks=2, channels=None):
    subject, changes = soften(offer)
    subject = _flatten(subject)
    if not subject:
        return _fail('An offer is needed, so the plan has something to promote.')

    span = max(1, min(_int_or_none(weeks) or 2, 12))
    wanted = [_flatten(key).lower() for key in (channels or []) if _flatten(key)]
    unknown = [key for key in wanted if key not in CHANNEL_LIMITS]
    if unknown:
        return _fail(f'These are not channels this platform writes for: '
                     f'{", ".join(unknown)}.')
    if not wanted:
        wanted = ['email', 'linkedin']

    beats = [
        ('Announce it', 'state the offer and its real terms once, plainly'),
        ('Explain it', 'answer the question the announcement raised'),
        ('Show it', 'one concrete example of somebody using it'),
        ('Remind once', 'a single reminder, not a sequence of them'),
        ('Close it', 'say it has ended, on the day it ended'),
    ]

    lines = [f'A {span}-week plan for "{subject}" across '
             f'{", ".join(channel_label(k) for k in wanted)}:', '']
    payload = []
    for week in range(1, span + 1):
        beat, detail = beats[min(week - 1, len(beats) - 1)]
        channel = wanted[(week - 1) % len(wanted)]
        lines.append(f'Week {week} -- {beat} on {channel_label(channel)}')
        lines.append(f'   {_cap(detail)}.')
        lines.append(f'   Ready before it goes: the copy written and approved, and '
                     f'every figure in it sourced.')
        lines.append('')
        payload.append({'week': week, 'beat': beat, 'channel': channel,
                        'detail': detail})

    lines.append('Terms and dates are not invented anywhere in this plan. Pass the '
                 'real ones in as facts when the copy is written.')
    if changes:
        lines.append('Banned wording in the offer was replaced: '
                     + '; '.join(changes) + '.')
    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'offer': subject, 'weeks': span,
                            'channels': wanted, 'plan': payload})


# ===========================================================================
# GROUP: Planning
# ===========================================================================

_SLOTS = ('morning', 'midday', 'afternoon')

# Weekday offsets from the Monday of each planned week, in the order they are
# taken up. Three posts a week therefore land Monday, Wednesday, Friday rather
# than on three consecutive days, and the first five slots never fall on a
# weekend, which is where marketing reach on a business channel goes to die.
_CADENCE_OFFSETS = (0, 2, 4, 1, 3, 5, 6)


def _planning_monday(anchor):
    """The Monday the calendar grid starts on.

    Aligned to a week boundary so the plan reads as weeks rather than as an
    arbitrary rolling seven days, and never earlier than the requested start
    date, so nothing is planned in the past.
    """
    monday = anchor - timedelta(days=anchor.weekday())
    return monday if monday >= anchor else monday + timedelta(days=7)

_CALENDAR_THEMES = (
    'the problem, stated plainly',
    'how we actually handle it',
    'one concrete example',
    'a question we get asked',
    'what it costs in practice',
    'where it goes wrong',
    'a short update',
    'something a reader can use on their own',
)


@tool(name='mkt.create_content_calendar', title='Build a content calendar',
      description=(
          'Build a real content calendar across a period: one row per planned '
          'slot, with a date, a time of day, a channel, a working title and an '
          'owner. Channels rotate so the same channel does not carry every '
          'slot. The rows are saved, so the plan survives the conversation and '
          'can be listed with mkt.list_calendar.'),
      group='Planning', agent_types=AGENTS, icon='fa-calendar-days',
      capability='Content calendar planning',
      parameters=_schema({
          'campaign_id': _integer('Campaign the calendar belongs to.'),
          'start_date': _text('First date, YYYY-MM-DD. Today by default.'),
          'weeks': _integer('How many weeks. Four by default.'),
          'channels': _array('Channel keys to rotate through.'),
          'cadence_per_week': _integer('Posts per week. Three by default.'),
      }))
def create_content_calendar(ctx, campaign_id=None, start_date=None, weeks=4,
                            channels=None, cadence_per_week=3):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure

    brief = latest_brief(campaign)
    wanted = [_flatten(key).lower() for key in (channels or []) if _flatten(key)]
    if not wanted and brief is not None:
        wanted = list(brief.channels or [])
    unknown = [key for key in wanted if key not in CHANNEL_LIMITS]
    if unknown:
        return _fail(f'These are not channels this platform writes for: '
                     f'{", ".join(unknown)}.')
    if not wanted:
        wanted = ['linkedin', 'email', 'instagram']

    span = max(1, min(_int_or_none(weeks) or 4, 26))
    cadence = max(1, min(_int_or_none(cadence_per_week) or 3, 7))
    requested = _parse_date(start_date, (campaign.start_date if campaign
                                         else None) or timezone.localdate())
    anchor = _planning_monday(requested)

    subject = (brief.key_message if brief and brief.key_message
               else (campaign.name if campaign else 'the marketing programme'))
    owner = 'Marketing'

    created = []
    position = 0
    for week in range(span):
        for index in range(cadence):
            when = anchor + timedelta(days=week * 7 + _CADENCE_OFFSETS[index])
            channel = wanted[position % len(wanted)]
            slot = _SLOTS[position % len(_SLOTS)]
            theme = _CALENDAR_THEMES[position % len(_CALENDAR_THEMES)]
            created.append(ContentCalendarEntry.objects.create(
                date=when, slot=slot, channel=channel,
                title=_truncate(f'{channel_label(channel)}: {theme}', 250),
                owner=owner, campaign=campaign, status='planned',
                notes=f'Week {week + 1} of {span}. Theme: {theme}. '
                      f'Subject of the campaign: {_truncate(subject, 120)}. '
                      'No copy exists yet; the generators write it and every '
                      'claim in it needs a source.',
                created_by_agent=ctx.agent))
            position += 1

    lines = [
        f'{len(created)} calendar entries created over {span} weeks at {cadence} '
        f'a week, rotating {", ".join(channel_label(k) for k in wanted)}, '
        f'starting Monday {anchor:%d %b %Y}.'
        + (f' Campaign #{campaign.pk} "{campaign.name}".' if campaign else
           ' No campaign is attached.'),
    ]
    if anchor != requested:
        lines.append(f'The requested start {requested:%a %d %b %Y} was moved to '
                     f'the following Monday so the plan falls on week boundaries '
                     'and nothing lands in the past.')
    lines.append('')
    current_week = None
    for entry in created:
        week_number = ((entry.date - anchor).days // 7) + 1
        if week_number != current_week:
            current_week = week_number
            lines.append(f'Week {week_number}')
        lines.append(f'  #{entry.pk} {entry.date:%a %d %b} {entry.slot:<10} '
                     f'{channel_label(entry.channel):<12} {entry.title}')
    lines.extend(['', 'Every entry is planned and has no draft. Write each one '
                      'with a generator, then schedule it -- scheduling is a '
                      'commitment and needs approval.'])
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'campaign_id': campaign.pk if campaign else None,
              'count': len(created), 'start_date': str(anchor),
              'requested_start': str(requested),
              'weeks': span, 'cadence_per_week': cadence, 'channels': wanted,
              'entries': [{'entry_id': e.pk, 'date': str(e.date),
                           'slot': e.slot, 'channel': e.channel,
                           'title': e.title} for e in created]})


@tool(name='mkt.list_calendar', title='Read the content calendar',
      description=(
          'Read the content calendar over a date range, optionally for one '
          'channel. Says for each entry whether a draft exists yet, which is '
          'the question a planning conversation actually turns on.'),
      group='Planning', agent_types=AGENTS, icon='fa-calendar',
      reads_only=True, capability='Calendar listing',
      parameters=_schema({
          'start_date': _text('From this date, YYYY-MM-DD. Today by default.'),
          'end_date': _text('To this date, YYYY-MM-DD.'),
          'channel': _enum('Only this channel.', tuple(CHANNEL_LIMITS)),
          'limit': _integer('How many rows. Sixty by default.'),
      }))
def list_calendar(ctx, start_date=None, end_date=None, channel='', limit=60):
    begins = _parse_date(start_date, timezone.localdate())
    ends = _parse_date(end_date)
    if ends is not None and begins is not None and ends < begins:
        return _fail(f'The end date {ends:%d %b %Y} is before the start date '
                     f'{begins:%d %b %Y}.')

    query = ContentCalendarEntry.objects.filter(date__gte=begins)
    if ends is not None:
        query = query.filter(date__lte=ends)
    if _flatten(channel):
        key = _flatten(channel).lower()
        if key not in CHANNEL_LIMITS:
            return _fail(f'"{channel}" is not a channel. Choose from: '
                         f'{", ".join(CHANNEL_LIMITS)}.')
        query = query.filter(channel=key)

    rows = list(query.select_related('content', 'campaign')
                [:max(1, min(_int_or_none(limit) or 60, 200))])
    window = (f'{begins:%d %b %Y}'
              + (f' to {ends:%d %b %Y}' if ends else ' onwards'))
    if not rows:
        return ToolResult(ok=True, data={'count': 0, 'entries': []},
                          text=f'The calendar has nothing between {window}. Use '
                               'mkt.create_content_calendar to plan the period.')

    drafted = sum(1 for entry in rows if entry.has_draft)
    lines = [f'{len(rows)} calendar entries, {window}. {drafted} of them have a '
             f'draft; {len(rows) - drafted} do not.', '']
    for entry in rows:
        draft = (f'draft #{entry.content_id}' if entry.has_draft else 'no draft yet')
        campaign = f' campaign #{entry.campaign_id}' if entry.campaign_id else ''
        lines.append(f'#{entry.pk} {entry.date:%a %d %b} {entry.slot:<10} '
                     f'{channel_label(entry.channel):<12} '
                     f'[{entry.get_status_display()}] {draft}{campaign}')
        lines.append(f'    {entry.title or "(no working title)"}')
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'count': len(rows), 'drafted': drafted,
              'entries': [{'entry_id': e.pk, 'date': str(e.date), 'slot': e.slot,
                           'channel': e.channel, 'status': e.status,
                           'title': e.title, 'content_id': e.content_id}
                          for e in rows]})


# ===========================================================================
# GROUP: Email Marketing
# ===========================================================================

def email_result(row, facts=(), notes=()):
    """The standard result for a mailing: the copy, the id, the subject length."""
    lines = [
        f'Marketing email #{row.pk} saved as a '
        f'{row.get_kind_display().lower()}, status '
        f'{row.get_status_display().lower()}. It has not been sent; sending is a '
        'separate tool and needs approval.',
        '',
        f'Subject ({row.subject_length} characters): {row.subject}',
        f'Preheader: {row.preheader or "(none set)"}',
        '',
        '--- body ---',
        row.body,
        '--- end of body ---',
        '',
    ]
    if row.subject_length_ok:
        lines.append(f'The subject fits the {EMAIL_SUBJECT_LIMIT}-character window '
                     'most inboxes show.')
    else:
        lines.append(f'OVER THE LIMIT: the subject is {row.subject_length} '
                     f'characters, {row.subject_length - EMAIL_SUBJECT_LIMIT} over '
                     f'the {EMAIL_SUBJECT_LIMIT} most inboxes show. It will be '
                     'read as a fragment. Call mkt.generate_subject_lines for '
                     'shorter options.')
    lines.append(f'Audience: {row.audience_summary}.')
    lines.append(_facts_note(list(facts)))
    banned = banned_words_in(f'{row.subject} {row.body}')
    if banned:
        lines.append('House-rule breach: ' + ', '.join(banned) + ' appear in the copy.')
    for note in notes:
        if _flatten(note):
            lines.append(_flatten(note))

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'email_id': row.pk, 'kind': row.kind, 'status': row.status,
              'subject': row.subject, 'subject_length': row.subject_length,
              'subject_ok': row.subject_length_ok,
              'recipient_count': row.recipient_count},
        subject_label='marketing.marketingemail', subject_id=row.pk)


@tool(name='mkt.generate_marketing_email', title='Write a marketing email',
      description=(
          'Write a marketing email -- subject, preheader and body -- and save it '
          'as a mailing. The subject is written inside the 60 characters most '
          'inboxes show, and the body states only what the facts you pass in '
          'support. Nothing is sent; mkt.send_marketing_email does that and '
          'needs approval.'),
      group='Email Marketing', agent_types=AGENTS, icon='fa-envelope-open-text',
      capability='Marketing email writing',
      parameters=_schema({
          'purpose': _text('What the email is for.'),
          'audience': _text('Who it is written for.'),
          'offer': _text('The offer or the ask, if there is one.'),
          'campaign_id': _integer('Campaign this belongs to, if any.'),
          'facts': _FACTS_ARGUMENT,
      }, required=('purpose',)))
def generate_marketing_email(ctx, purpose, audience='', offer='', campaign_id=None,
                             facts=None):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure

    reason, changes = soften(purpose)
    deal, more_changes = soften(offer)
    changes = changes + more_changes
    rows = normalise_facts(facts)

    subject = _truncate(_cap(reason).rstrip('.'), EMAIL_SUBJECT_LIMIT)
    preheader = _truncate(
        _sentence(rows[0]['fact']) if rows
        else f'A short note about {_lower(reason)}', 200)
    body = compose_email_body(reason, audience, deal, rows, 'professional')

    row = MarketingEmail.objects.create(
        campaign=campaign, kind='campaign',
        name=_truncate(f'Campaign email: {reason}', 200),
        subject=subject, preheader=preheader, body=body,
        audience_segment=_flatten(audience)[:200], recipients=[],
        status='draft', created_by_agent=ctx.agent)

    notes = []
    if changes:
        notes.append('Banned wording was replaced: ' + '; '.join(changes) + '.')
    if campaign is not None:
        notes.append(f'Linked to campaign #{campaign.pk} "{campaign.name}".')
    notes.append('No recipients are recorded yet. Pass them to '
                 'mkt.send_marketing_email, or set them with '
                 'mkt.create_email_campaign.')
    return email_result(row, rows, notes)


@tool(name='mkt.generate_newsletter', title='Write a newsletter',
      description=(
          'Write a newsletter edition with the sections you name, and save it as '
          'a mailing. Sections become real headings; without any, the structure '
          'is what happened, what is coming and one thing worth reading. Every '
          'claim in it comes from the facts you pass in.'),
      group='Email Marketing', agent_types=AGENTS, icon='fa-newspaper',
      capability='Newsletter writing',
      parameters=_schema({
          'edition': _text('Edition name, e.g. "March" or "Issue 12".'),
          'sections': _array('Section headings, in order.'),
          'campaign_id': _integer('Campaign this belongs to, if any.'),
          'facts': _FACTS_ARGUMENT,
      }))
def generate_newsletter(ctx, edition='', sections=None, campaign_id=None, facts=None):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure

    label, changes = soften(edition)
    label = _flatten(label) or timezone.localdate().strftime('%B %Y')
    rows = normalise_facts(facts)

    headings = [_flatten(entry) for entry in (sections or []) if _flatten(entry)]
    if not headings:
        headings = ['What happened', 'What is coming', 'One thing worth reading']

    company = _company_name('The team')
    parts = [f'{company} newsletter -- {label}', '',
             _sentence(f'Three short sections, and nothing that needs a reply '
                       'unless you want to send one'), '']
    for index, heading in enumerate(headings):
        parts.append(f'## {_cap(heading)}')
        share = rows[index::len(headings)] if rows else []
        if share:
            for entry in share[:2]:
                parts.append(_sentence(entry['fact']))
        else:
            parts.append(_sentence(
                f'{_cap(heading)} -- add the detail here. Nothing was supplied '
                'for this section, so nothing is claimed in it'))
        parts.append('')
    parts.extend([_cta_line('friendly'), '', 'Best regards,', company])

    subject = _truncate(f'{company} newsletter: {label}', EMAIL_SUBJECT_LIMIT)
    row = MarketingEmail.objects.create(
        campaign=campaign, kind='newsletter',
        name=_truncate(f'Newsletter {label}', 200), subject=subject,
        preheader=_truncate(f'{len(headings)} short sections from {company}.', 200),
        body='\n'.join(parts).strip(), recipients=[], status='draft',
        created_by_agent=ctx.agent)

    notes = [f'Sections: {", ".join(headings)}.']
    if changes:
        notes.append('Banned wording in the edition name was replaced: '
                     + '; '.join(changes) + '.')
    empty = [heading for index, heading in enumerate(headings)
             if not (rows[index::len(headings)] if rows else [])]
    if empty:
        notes.append('These sections have no supplied facts and say so in the copy '
                     'rather than filling the space: ' + ', '.join(empty) + '.')
    return email_result(row, rows, notes)


@tool(name='mkt.generate_subject_lines', title='Suggest email subject lines',
      description=(
          'Suggest email subject lines for a topic, each with its character '
          'count and the reason it might work. Anything over 60 characters is '
          'flagged, because most inboxes cut it there and a subject read as a '
          'fragment is worse than a shorter one read whole. Nothing is saved.'),
      group='Email Marketing', agent_types=AGENTS, icon='fa-heading',
      reads_only=True, capability='Subject line writing',
      parameters=_schema({
          'topic': _text('What the email is about.'),
          'count': _integer('How many lines. Six by default.'),
          'style': _enum('Register of the lines.',
                         ('plain', 'direct', 'question', 'curious', 'urgent')),
      }, required=('topic',)))
def generate_subject_lines(ctx, topic, count=6, style=''):
    subject, changes = soften(topic)
    subject = _flatten(subject)
    if not subject:
        return _fail('A topic is needed, so the subject lines have something to '
                     'be about.')
    wanted = max(1, min(_int_or_none(count) or 6, 15))
    short = _truncate(_lower(subject), 34)

    candidates = [
        (_cap(short), 'plain',
         'says exactly what the email is about, which beats cleverness on a '
         'list that already knows you'),
        (f'About {short}', 'plain',
         'lowest-effort framing, useful when the list opens on sender rather '
         'than subject'),
        (f'{_cap(short)}: the short version', 'direct',
         'promises brevity, which is the one promise a subject can keep'),
        (f'Have you looked at {short}?', 'question',
         'a question invites an answer, but only works if the body answers it'),
        (f'What we changed about {short}', 'curious',
         'implies news without claiming a result nobody measured'),
        (f'{_cap(short)} -- one thing to check', 'direct',
         'specific and small, so it reads as useful rather than promotional'),
        (f'Two minutes on {short}', 'plain',
         'sets the cost of reading, which lifts opens among busy readers'),
        (f'Before the month ends: {short}', 'urgent',
         'only use this when a real deadline exists; a manufactured one trains '
         'the list to ignore you'),
        (f'The {short} question we keep getting', 'curious',
         'grounded in something real, so it does not read as invented intrigue'),
    ]

    preferred = _flatten(style).lower()
    if preferred:
        ordered = ([row for row in candidates if row[1] == preferred]
                   + [row for row in candidates if row[1] != preferred])
    else:
        ordered = candidates

    lines = [f'{min(wanted, len(ordered))} subject lines for "{subject}"'
             + (f', {preferred} first' if preferred else '') + ':', '']
    payload = []
    over = 0
    for index, (line, kind, why) in enumerate(ordered[:wanted], start=1):
        length = len(line)
        flag = ''
        if length > EMAIL_SUBJECT_LIMIT:
            over += 1
            flag = f'  OVER THE LIMIT by {length - EMAIL_SUBJECT_LIMIT}'
        lines.append(f'{index}. {line}')
        lines.append(f'   {length} characters ({kind}){flag}')
        lines.append(f'   Why it might work: {why}.')
        lines.append('')
        payload.append({'subject': line, 'characters': length, 'style': kind,
                        'over_limit': length > EMAIL_SUBJECT_LIMIT,
                        'rationale': why})
    if over:
        lines.append(f'{over} of these are over the {EMAIL_SUBJECT_LIMIT}-character '
                     'inbox window and will be truncated.')
    else:
        lines.append(f'All of these fit the {EMAIL_SUBJECT_LIMIT}-character '
                     'inbox window.')
    if changes:
        lines.append('Banned wording in the topic was replaced: '
                     + '; '.join(changes) + '.')
    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'topic': subject, 'limit': EMAIL_SUBJECT_LIMIT,
                            'subject_lines': payload})


@tool(name='mkt.create_email_campaign', title='Record an email campaign',
      description=(
          'Record a marketing email you already have the copy for, with its '
          'segment and its recipient list. Use this when the subject and body '
          'are given rather than generated. The recipient list is stored as a '
          'snapshot, so the record still says who it went to after the segment '
          'is redefined. Nothing is sent.'),
      group='Email Marketing', agent_types=AGENTS, icon='fa-paper-plane',
      capability='Email campaign setup',
      parameters=_schema({
          'name': _text('Internal name for the mailing.'),
          'subject': _text('Subject line.'),
          'body': _text('Body of the email.'),
          'audience_segment': _text('Segment name this goes to.'),
          'recipients': _array('Email addresses, as a snapshot of the segment.'),
          'kind': _enum('What kind of mailing this is.',
                        ('campaign', 'newsletter', 'announcement', 'nurture')),
      }, required=('name', 'subject', 'body')))
def create_email_campaign(ctx, name, subject, body, audience_segment='',
                          recipients=None, kind='campaign'):
    label = _flatten(name)
    line = _flatten(subject)
    if not label or not line or not _flatten(body):
        return _fail('A name, a subject and a body are all needed. An email '
                     'without one of them cannot be reviewed, let alone sent.')

    valid = [value for value, _text_label in MarketingEmail.KIND_CHOICES]
    key = _flatten(kind).lower() or 'campaign'
    if key not in valid:
        return _fail(f'"{kind}" is not a kind of mailing. Choose from: '
                     f'{", ".join(valid)}.')

    addresses = []
    for entry in (recipients or []):
        address = _flatten(entry)
        if address and address not in addresses:
            addresses.append(address)

    row = MarketingEmail.objects.create(
        kind=key, name=label[:200], subject=line[:250],
        preheader=_truncate(_flatten(body), 200),
        body=str(body), audience_segment=_flatten(audience_segment)[:200],
        recipients=addresses, recipient_count=len(addresses),
        status='draft', created_by_agent=ctx.agent)

    notes = []
    if addresses:
        notes.append(f'{len(addresses)} recipients recorded as a snapshot.')
    else:
        notes.append('No recipients were given, so nothing can be sent yet. '
                     'Pass them here or to mkt.send_marketing_email.')
    return email_result(row, (), notes)


@tool(name='mkt.list_emails', title='List marketing emails',
      description=(
          'List saved marketing emails, newest first, optionally by status or '
          'kind. Flags any whose subject is over the 60-character inbox window. '
          'Use this before quoting an email id to a send tool.'),
      group='Email Marketing', agent_types=AGENTS, icon='fa-list',
      reads_only=True, capability='Mailing listing',
      parameters=_schema({
          'status': _enum('Only this status.',
                          ('draft', 'pending', 'approved', 'scheduled',
                           'sent', 'rejected')),
          'kind': _enum('Only this kind.',
                        ('campaign', 'newsletter', 'announcement', 'nurture')),
          'limit': _integer('How many rows. Twenty-five by default.'),
      }))
def list_emails(ctx, status='', kind='', limit=25):
    query = MarketingEmail.objects.all().select_related('campaign')
    described = []

    if _flatten(status):
        key = _flatten(status).lower()
        valid = [value for value, _label in MarketingEmail.STATUS_CHOICES]
        if key not in valid:
            return _fail(f'"{status}" is not a mailing status. Choose from: '
                         f'{", ".join(valid)}.')
        query = query.filter(status=key)
        described.append(key)

    if _flatten(kind):
        key = _flatten(kind).lower()
        valid = [value for value, _label in MarketingEmail.KIND_CHOICES]
        if key not in valid:
            return _fail(f'"{kind}" is not a kind of mailing. Choose from: '
                         f'{", ".join(valid)}.')
        query = query.filter(kind=key)
        described.append(key)

    rows = list(query[:max(1, min(_int_or_none(limit) or 25, 100))])
    scope = ' and '.join(described) or 'all statuses and kinds'
    if not rows:
        return ToolResult(ok=True, data={'count': 0, 'emails': []},
                          text=f'No marketing emails match {scope}. Use '
                               'mkt.generate_marketing_email to write one.')

    long_subjects = [row for row in rows if not row.subject_length_ok]
    lines = [f'{len(rows)} marketing emails ({scope}), newest first:', '']
    for row in rows:
        flag = '' if row.subject_length_ok else '  [subject over the limit]'
        campaign = f' campaign #{row.campaign_id}' if row.campaign_id else ''
        when = (f' sent {row.sent_at:%d %b %Y}' if row.sent_at else
                (f' scheduled {row.scheduled_for:%d %b %Y at %H:%M}'
                 if row.scheduled_for else ''))
        lines.append(f'#{row.pk} [{row.get_kind_display()}] '
                     f'{row.get_status_display()}{campaign}{when}')
        lines.append(f'    Subject ({row.subject_length}): '
                     f'{row.subject or "(none)"}{flag}')
        lines.append(f'    {row.audience_summary}')
    if long_subjects:
        lines.extend(['', f'{len(long_subjects)} of these have a subject over the '
                          f'{EMAIL_SUBJECT_LIMIT}-character inbox window: '
                          + ', '.join(f'#{row.pk}' for row in long_subjects) + '.'])
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'count': len(rows),
              'emails': [{'email_id': r.pk, 'kind': r.kind, 'status': r.status,
                          'subject': r.subject,
                          'subject_length': r.subject_length,
                          'recipient_count': r.recipient_count} for r in rows]})


# ===========================================================================
# GROUP: Research -- facts before copy, and copy checked afterwards
# ===========================================================================

@tool(name='mkt.get_product_facts', title='Get product facts from the knowledge base',
      description=(
          'Look facts up in the company knowledge base and return them with the '
          'document each came from, formatted so they can be passed straight '
          'into any generator\'s facts argument. CALL THIS BEFORE WRITING '
          'ANYTHING THAT STATES A FACT ABOUT THE COMPANY OR THE PRODUCT. The '
          'generators only assert what is passed into them, so a figure that '
          'did not come through here will not appear in the copy -- and a figure '
          'you type from memory has no source and cannot be defended once the '
          'post is public.'),
      group='Research', agent_types=AGENTS, icon='fa-magnifying-glass',
      integration='knowledge_base', reads_only=True,
      capability='Product fact lookup',
      parameters=_schema({
          'query': _text('What to look up, e.g. "rostering time saved".'),
          'limit': _integer('How many facts. Five by default.'),
      }, required=('query',)))
def get_product_facts(ctx, query, limit=5):
    question = _flatten(query)
    if not question:
        return _fail('A query is needed. Say what fact you are looking for.')

    wanted = max(1, min(_int_or_none(limit) or 5, 15))
    hits, note = knowledge_search(question, limit=wanted)
    if not hits:
        return ToolResult(
            ok=True, data={'query': question, 'facts': [], 'count': 0},
            text=f'{note} Write the piece without the fact rather than '
                 'approximating it, or ask the Knowledge & Research employee '
                 'whether the document exists under another name. Do not supply '
                 'the figure from memory.')

    facts = []
    lines = [f'{len(hits)} facts for "{question}", each with its document. Pass '
             'these strings straight into a generator\'s facts argument:', '']
    for hit in hits:
        fields = _hit_fields(hit)
        text = fields['snippet'] or fields['title']
        formatted = f'{_flatten(text)} [{fields["title"]}]'
        facts.append(formatted)
        lines.append(f'- {formatted}')
        if fields['url']:
            lines.append(f'    {fields["url"]}')
        lines.append(f'    relevance {fields["score"]}')
    lines.extend(['', 'Anything not in this list must not appear as a claim in the '
                      'copy. If the fact you need is missing, say it is unknown.'])
    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'query': question, 'count': len(facts), 'facts': facts,
              'detail': [_hit_fields(hit) for hit in hits]})


@tool(name='mkt.check_brand_compliance', title='Check copy against the brand rules',
      description=(
          'Check a content piece, or a block of text, against the brand '
          'guidelines in the knowledge base and against the hard house rules: '
          'banned words, hashtag counts, channel length, unsubstantiated '
          'superlatives, and any figure in the copy that is not in the piece\'s '
          'recorded sources. Every finding quotes the offending text. Run this '
          'before requesting approval to publish.'),
      group='Research', agent_types=AGENTS, icon='fa-shield-halved',
      integration='knowledge_base', reads_only=True,
      capability='Brand compliance checking',
      parameters=_schema({
          'content_id': _integer('The saved piece to check.'),
          'text': _text('Copy to check, when it is not a saved piece.'),
      }))
def check_brand_compliance(ctx, content_id=None, text=''):
    piece = None
    if _int_or_none(content_id) is not None:
        piece, failure = find_piece(content_id)
        if failure is not None:
            return failure

    copy = piece.full_text if piece is not None else str(text or '')
    if not _flatten(copy):
        return _fail('Nothing to check. Give a content_id of a saved piece, or '
                     'pass the copy itself as text.')

    findings = []

    # -- the guidelines in the knowledge base -----------------------------
    hits, note = knowledge_search('brand tone of voice guidelines', limit=3)
    guideline_lines = []
    if hits:
        for hit in hits:
            fields = _hit_fields(hit)
            guideline_lines.append(f'- {fields["title"]}'
                                   + (f' ({fields["url"]})' if fields['url'] else ''))
            if fields['snippet']:
                guideline_lines.append(f'    {_truncate(fields["snippet"], 300)}')
    else:
        guideline_lines.append(f'- {note} The hard rules below were still applied.')

    # -- banned words ------------------------------------------------------
    for word in banned_words_in(copy):
        for match in re.finditer(re.escape(word), copy, re.IGNORECASE):
            start = max(0, match.start() - 40)
            findings.append({
                'rule': 'banned word',
                'severity': 'must fix',
                'detail': f'"{word}" is on the banned list.',
                'quote': _flatten(copy[start:match.end() + 40]),
            })
            break

    # -- unsubstantiated superlatives -------------------------------------
    recorded = ' '.join(entry.get('fact', '') for entry in (piece.sources or [])) \
        if piece is not None else ''
    for word in superlatives_in(copy):
        match = re.search(re.escape(word), copy, re.IGNORECASE)
        start = max(0, match.start() - 40) if match else 0
        end = (match.end() + 40) if match else 80
        supported = bool(recorded) and word.lower() in recorded.lower()
        findings.append({
            'rule': 'unsubstantiated superlative',
            'severity': 'check' if supported else 'must fix',
            'detail': (f'"{word}" is a ranking claim. It does appear in the '
                       'recorded sources, so check the source actually says it.'
                       if supported else
                       f'"{word}" is a ranking claim and nothing in the recorded '
                       'sources supports it.'),
            'quote': _flatten(copy[start:end]),
        })

    # -- figures not present in the recorded sources ----------------------
    if piece is not None:
        for figure in figures_in(piece.body):
            if figure.lower() in recorded.lower():
                continue
            match = re.search(re.escape(figure), piece.body)
            start = max(0, match.start() - 40) if match else 0
            end = (match.end() + 40) if match else 80
            findings.append({
                'rule': 'unsourced figure',
                'severity': 'must fix',
                'detail': (f'The figure "{figure}" appears in the copy but is in '
                           'none of the recorded sources on this piece. Either '
                           'pass the fact in through mkt.get_product_facts and '
                           'regenerate, or take the figure out.'),
                'quote': _flatten(piece.body[start:end]),
            })
    else:
        loose = figures_in(copy)
        if loose:
            findings.append({
                'rule': 'unsourced figure',
                'severity': 'check',
                'detail': ('This copy is not a saved piece, so it has no recorded '
                           'sources to check against. Figures present: '
                           + ', '.join(loose[:8]) + '.'),
                'quote': _truncate(copy, 120),
            })

    # -- hashtags and channel length --------------------------------------
    if piece is not None:
        house = HOUSE_HASHTAG_LIMITS.get(piece.channel, 5)
        count = len(piece.hashtags or [])
        if house and count > house:
            findings.append({
                'rule': 'hashtag count',
                'severity': 'must fix',
                'detail': (f'{count} hashtags on {channel_label(piece.channel)}, '
                           f'{count - house} over the house limit of {house}.'),
                'quote': piece.hashtag_line,
            })
        fits, message = piece.fits_channel
        if not fits:
            findings.append({'rule': 'channel limit', 'severity': 'must fix',
                             'detail': message, 'quote': _truncate(piece.full_text, 120)})

    subject_of = (f'content piece #{piece.pk} ({channel_label(piece.channel)})'
                  if piece is not None else 'the supplied text')
    lines = [f'Brand compliance check on {subject_of}.', '',
             'GUIDELINES CONSULTED'] + guideline_lines + ['', 'FINDINGS']

    if not findings:
        lines.append('None. No banned words, no unsubstantiated superlative, no '
                     'figure outside the recorded sources, and within the channel '
                     'and hashtag limits.')
    else:
        must_fix = sum(1 for row in findings if row['severity'] == 'must fix')
        lines.append(f'{len(findings)} findings, {must_fix} of which must be fixed '
                     'before this is published.')
        lines.append('')
        for index, row in enumerate(findings, start=1):
            lines.append(f'{index}. [{row["severity"]}] {row["rule"]}')
            lines.append(f'   {row["detail"]}')
            lines.append(f'   Offending text: "{row["quote"]}"')
            lines.append('')

    if piece is not None and not piece.sources:
        lines.append('This piece has no recorded sources at all, so every claim in '
                     'it is unsourced by definition. Call mkt.get_product_facts '
                     'and regenerate rather than editing figures in by hand.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'content_id': piece.pk if piece is not None else None,
              'finding_count': len(findings),
              'must_fix': sum(1 for row in findings
                              if row['severity'] == 'must fix'),
              'findings': findings,
              'guidelines_found': bool(hits)},
        subject_label='marketing.contentpiece' if piece is not None else '',
        subject_id=piece.pk if piece is not None else 0)


@tool(name='mkt.read_brand_guidelines', title='Read the brand guidelines',
      description=(
          'Find and read the brand guidelines from Google Drive, falling back to '
          'the knowledge base when Drive has nothing. A read, so it happens '
          'immediately. Read these before writing for a channel the company has '
          'not used before, rather than inferring the house style from a '
          'previous post.'),
      group='Research', agent_types=AGENTS, icon='fa-book',
      integration='google_drive', reads_only=True,
      capability='Brand guideline retrieval',
      parameters=_schema({}))
def read_brand_guidelines(ctx):
    search = integrations.call('google_drive', 'search_files',
                              query='brand guidelines', limit=5)
    files = []
    if search.ok:
        payload = search.data or {}
        listing = (payload.get('files') or payload.get('results')
                   or payload.get('items') or [])
        for entry in listing if isinstance(listing, list) else []:
            if not isinstance(entry, dict):
                continue
            identifier = (entry.get('id') or entry.get('file_id')
                          or entry.get('external_id') or '')
            name = entry.get('name') or entry.get('title') or 'Untitled'
            if identifier:
                files.append({'file_id': str(identifier), 'name': _flatten(name)})

    lines = []
    if files:
        first = files[0]
        read = integrations.call('google_drive', 'read_file', file_id=first['file_id'])
        lines.append(f'Google Drive holds {len(files)} matching files: '
                     + '; '.join(f'{row["name"]} ({row["file_id"]})'
                                 for row in files) + '.')
        lines.append('')
        if read.ok:
            content = (read.data or {}).get('content') or read.summary
            lines.append(f'--- {first["name"]} ---')
            lines.append(_truncate(str(content), 4000))
            lines.append('--- end ---')
            if read.demo:
                lines.append('')
                lines.append('This was SIMULATED. Google Drive is not connected, '
                             'so the content above is a demonstration and must '
                             'not be treated as the real guidelines.')
            return ToolResult(
                ok=True, demo=read.demo, text='\n'.join(lines),
                data={'source': 'google_drive', 'files': files,
                      'file_id': first['file_id']})
        lines.append(f'The file could not be read: {read.error}')
    else:
        lines.append('Google Drive returned no brand guideline files'
                     + (f' ({search.error})' if not search.ok else '') + '.')

    hits, note = knowledge_search('brand tone of voice guidelines', limit=3)
    lines.append('')
    if not hits:
        lines.append(f'Falling back to the knowledge base: {note}')
        lines.append('There are no recorded guidelines. Apply the house rules '
                     'that are hard-coded instead: no synergy, revolutionary, '
                     'game-changing or unlock; five hashtags at most, twelve on '
                     'Instagram; no superlative without a source.')
        return ToolResult(ok=True, text='\n'.join(lines),
                          data={'source': 'none', 'files': files})

    lines.append(f'Falling back to the knowledge base, which has {len(hits)} '
                 'relevant documents:')
    for hit in hits:
        fields = _hit_fields(hit)
        lines.append('')
        lines.append(f'--- {fields["title"]} ---')
        lines.append(_truncate(fields['snippet'], 1200))
        if fields['url']:
            lines.append(fields['url'])
    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'source': 'knowledge_base', 'files': files,
                            'documents': [_hit_fields(hit) for hit in hits]})


@tool(name='mkt.read_marketing_document', title='Read a marketing document',
      description=(
          'Read one document from Google Drive by its file id -- a brief, a '
          'previous campaign write-up, a fact sheet. A read, so it happens '
          'immediately. Get the file id from mkt.read_brand_guidelines or from '
          'whoever gave you the document; do not guess one.'),
      group='Research', agent_types=AGENTS, icon='fa-file-arrow-down',
      integration='google_drive', reads_only=True,
      capability='Marketing document retrieval',
      parameters=_schema({'file_id': _text('Drive file id.')},
                         required=('file_id',)))
def read_marketing_document(ctx, file_id):
    identifier = _flatten(file_id)
    if not identifier:
        return _fail('A file id is needed. Call mkt.read_brand_guidelines to see '
                     'what Drive holds, rather than guessing an id.')

    read = integrations.call('google_drive', 'read_file', file_id=identifier)
    if not read.ok:
        return _fail(f'Google Drive could not read file {identifier}: {read.error}')

    content = (read.data or {}).get('content') or read.summary
    name = (read.data or {}).get('name') or identifier
    lines = [f'--- {name} ({identifier}) ---',
             _truncate(str(content), 6000), '--- end ---']
    if read.demo:
        lines.extend(['', 'This was SIMULATED. Google Drive is not connected, so '
                          'nothing above is a real company document and none of '
                          'it may be used as a source for published copy.'])
    return ToolResult(ok=True, demo=read.demo, text='\n'.join(lines),
                      data={'file_id': identifier, 'name': name,
                            'characters': len(str(content))})


# ===========================================================================
# GROUP: Social Media -- reads
# ===========================================================================

@tool(name='mkt.get_social_insights', title='Read social media insights',
      description=(
          'Read the figures a social platform reports back: Instagram insights, '
          'or the LinkedIn profile and its follower numbers. A read, so it '
          'happens immediately and needs no approval. Use these figures rather '
          'than estimating reach, and note whether the result says it was '
          'simulated before quoting anything from it.'),
      group='Social Media', agent_types=AGENTS, icon='fa-chart-line',
      integration='instagram', reads_only=True,
      capability='Social insight reading',
      parameters=_schema({
          'platform': _enum('Which platform to read.', ('instagram', 'linkedin')),
          'metric': _text('A particular metric, if only one is wanted.'),
      }))
def get_social_insights(ctx, platform='instagram', metric=''):
    key = _flatten(platform).lower() or 'instagram'
    if key not in ('instagram', 'linkedin'):
        return _fail(f'"{platform}" is not a platform this employee can read. '
                     'Choose instagram or linkedin.')

    if key == 'instagram':
        arguments = {'metric': _flatten(metric)} if _flatten(metric) else {}
        result = integrations.call('instagram', 'get_insights', **arguments)
    else:
        result = integrations.call('linkedin', 'get_profile')

    if not result.ok:
        return _fail(f'{key.capitalize()} could not be read: {result.error}')

    payload = result.data or {}
    lines = [f'{key.capitalize()}: {result.summary or "read completed"}.']
    if payload:
        lines.append('')
        for name, value in list(payload.items())[:25]:
            lines.append(f'{str(name).replace("_", " ")}: {value}')
    if _flatten(metric) and key == 'linkedin':
        lines.append('')
        lines.append(f'A specific metric ("{_flatten(metric)}") was asked for, but '
                     'the LinkedIn read returns the profile rather than named '
                     'metrics. Everything available is above.')
    if result.demo:
        lines.extend(['', 'These figures are SIMULATED. The integration is not '
                          'connected, so nothing above is a real measurement and '
                          'none of it may be quoted in published copy.'])
    return ToolResult(ok=True, demo=result.demo, text='\n'.join(lines),
                      data={'platform': key, 'metric': _flatten(metric),
                            'insights': payload})


# ===========================================================================
# EXECUTION HELPERS
#
# Everything below the line runs only from marketing/approvals.py, after a
# person has approved a ProposedAction. The proposing tools above it have no
# access to integrations.call and therefore no way to reach the outside world,
# which is the guarantee rather than the convention.
# ===========================================================================

_REFERENCE_KEYS = ('external_id', 'post_id', 'message_id', 'event_id', 'file_id',
                   'id', 'urn', 'ts', 'thread_id')
_URL_KEYS = ('permalink', 'external_url', 'html_url', 'web_link', 'url', 'link')


def _reference_from(data):
    """Whatever identifier the service handed back, whichever key it used."""
    payload = data or {}
    for key in _REFERENCE_KEYS:
        value = payload.get(key)
        if value not in (None, '', [], {}):
            return str(value)
    return ''


def _url_from(data):
    payload = data or {}
    for key in _URL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _record_outbound(action, *, channel, recipient, subject, body, result):
    """One OutboundMessage row for one thing that actually left, or did not.

    ``status`` comes from the CallResult's own ``demo`` flag, never from an
    assumption. A simulated send is recorded as simulated, so the audit trail
    never presents a demonstration as a delivery.
    """
    from ..models_platform import OutboundMessage

    if result.ok:
        status = 'simulated' if result.demo else 'sent'
    else:
        status = 'failed'
    return OutboundMessage.objects.create(
        channel=channel, recipient=_flatten(recipient)[:400],
        subject=_flatten(subject)[:300], body=str(body or ''), status=status,
        external_id=_reference_from(result.data)[:200],
        external_url=_url_from(result.data)[:400],
        error_message=result.error or '',
        agent=action.agent, action=action, integration=action.integration,
        metadata={'operation': result.operation, 'provider': result.provider,
                  'summary': result.summary, 'demo': result.demo})


def _record_calendar(action, *, title, start_at, duration_minutes=60,
                     attendees=(), description='', result=None,
                     subject_label='', subject_id=None):
    """One CalendarEvent row, whether a calendar service was involved or not."""
    from ..models_platform import CalendarEvent

    if result is None:
        status = 'scheduled'
        external = ''
    elif not result.ok:
        status = 'cancelled'
        external = ''
    else:
        status = 'simulated' if result.demo else 'scheduled'
        external = _reference_from(result.data)[:200]

    minutes = _int_or_none(duration_minutes) or 60
    return CalendarEvent.objects.create(
        title=_flatten(title)[:250], description=str(description or ''),
        start_at=start_at, end_at=start_at + timedelta(minutes=minutes),
        duration_minutes=minutes,
        attendees=[_flatten(person) for person in attendees if _flatten(person)],
        status=status, external_id=external,
        agent=action.agent, action=action,
        subject_label=subject_label, subject_id=subject_id)


def _piece_from_action(action):
    key = _int_or_none((action.payload or {}).get('content_id'))
    return ContentPiece.objects.filter(pk=key).first() if key is not None else None


def _email_from_action(action):
    key = _int_or_none((action.payload or {}).get('email_id'))
    return MarketingEmail.objects.filter(pk=key).first() if key is not None else None


def _mark_published(piece, result):
    """Move a piece to published, with whatever the platform returned."""
    if piece is None:
        return
    piece.status = 'published'
    piece.published_at = timezone.now()
    piece.external_reference = _reference_from(result.data)[:200]
    piece.external_url = _url_from(result.data)[:500]
    piece.save(update_fields=['status', 'published_at', 'external_reference',
                              'external_url', 'updated_at'])


def _failed(result, what):
    return ToolResult(ok=False, error=result.error,
                      text=f'{what} failed and nothing was published or sent: '
                           f'{result.error}')


# ===========================================================================
# GROUP: Social Media -- publishing, which always needs a person
# ===========================================================================

@tool(name='mkt.publish_linkedin_post', title='Publish a LinkedIn post',
      description=(
          'Publish a post to the company LinkedIn page. Pass a content_id of a '
          'saved piece, or the text directly. This prepares the publication and '
          'places it in the human approval queue with the full copy attached; it '
          'does not post. Run mkt.check_brand_compliance first, because after '
          'approval the copy is public and the company owns every claim in it.'),
      group='Social Media', agent_types=AGENTS, icon='fa-linkedin',
      integration='linkedin', requires_approval=True, risk='high',
      capability='LinkedIn publishing',
      parameters=_schema({
          'content_id': _integer('The saved piece to publish.'),
          'text': _text('Copy to publish, when it is not a saved piece.'),
          'visibility': _enum('Who can see it.', ('PUBLIC', 'CONNECTIONS')),
      }))
def publish_linkedin_post(ctx, content_id=None, text='', visibility='PUBLIC'):
    piece = None
    if _int_or_none(content_id) is not None:
        piece, failure = find_piece(content_id)
        if failure is not None:
            return failure
        if piece.is_published:
            return _fail(f'Content piece #{piece.pk} was already published on '
                         f'{piece.published_at:%d %b %Y at %H:%M}. Publishing it '
                         'again would duplicate it; write a new piece instead.')

    copy = piece.full_text if piece is not None else _flatten(text)
    if not copy:
        return _fail('Nothing to publish. Give a content_id of a saved piece, or '
                     'pass the copy itself as text.')

    limit = CHANNEL_LIMITS['linkedin']['characters']
    warnings = []
    if len(copy) > limit:
        warnings.append(f'The copy is {len(copy)} characters, '
                        f'{len(copy) - limit} over LinkedIn\'s {limit}. LinkedIn '
                        'will reject it.')
    banned = banned_words_in(copy)
    if banned:
        warnings.append('Banned words present: ' + ', '.join(banned) + '.')
    superlatives = superlatives_in(copy)
    if superlatives:
        warnings.append('Unsubstantiated superlatives present: '
                        + ', '.join(superlatives) + '.')
    if piece is not None and not piece.sources:
        warnings.append('This piece has no recorded sources, so every claim in it '
                        'is unsourced.')

    summary = [f'{len(copy)} characters, visibility {_flatten(visibility) or "PUBLIC"}.']
    if piece is not None:
        summary.append(f'Content piece #{piece.pk}.')
        if piece.source_documents:
            summary.append('Sources: ' + ', '.join(piece.source_documents) + '.')
    summary.extend(warnings)
    summary.extend(['', 'Copy as it would be published:', '', copy])

    return Proposal(
        title=f'Publish to LinkedIn: {_truncate(copy, 90)}',
        summary='\n'.join(summary),
        payload={'content_id': piece.pk if piece is not None else None,
                 'text': copy, 'visibility': _flatten(visibility) or 'PUBLIC'},
        editable_fields=[editable('text', 'Post copy', 'longtext', rows=14),
                         editable('visibility', 'Visibility')],
        risk='high', integration_key='linkedin',
        subject_label='marketing.contentpiece' if piece is not None else '',
        subject_id=piece.pk if piece is not None else 0,
        subject_display=_truncate(copy, 180),
        confirmation=(
            f'Prepared the LinkedIn post and placed it in the approval queue. It '
            f'is {len(copy)} characters. '
            + (' '.join(warnings) + ' ' if warnings else '')
            + 'Nothing is public until somebody approves it.'))


@executor('mkt.publish_linkedin_post')
def _execute_publish_linkedin_post(action):
    payload = action.payload or {}
    copy = str(payload.get('text') or '')
    result = integrations.call('linkedin', 'publish_post', text=copy,
                               visibility=payload.get('visibility') or 'PUBLIC')

    message = _record_outbound(action, channel='linkedin', recipient='LinkedIn page',
                               subject='LinkedIn post', body=copy, result=result)
    if not result.ok:
        return _failed(result, 'The LinkedIn publication')

    piece = _piece_from_action(action)
    _mark_published(piece, result)

    where = 'simulated (LinkedIn is not connected)' if result.demo else 'published live'
    reference = _reference_from(result.data)
    lines = [f'The LinkedIn post was {where}. Outbound message #{message.pk} '
             'records it.']
    if reference:
        lines.append(f'Platform reference: {reference}.')
    if _url_from(result.data):
        lines.append(_url_from(result.data))
    if piece is not None:
        lines.append(f'Content piece #{piece.pk} is now marked published.')
    return ToolResult(ok=True, demo=result.demo, text=' '.join(lines),
                      data={'message_id': message.pk,
                            'content_id': piece.pk if piece else None,
                            'external_reference': reference,
                            'external_url': _url_from(result.data)})


@tool(name='mkt.publish_instagram_post', title='Publish an Instagram post',
      description=(
          'Publish a caption and image to Instagram. Pass a content_id of a '
          'saved caption, or the caption directly, plus the image URL. This '
          'prepares the publication for the human approval queue; it does not '
          'post. Instagram will not accept a post with no media, so an empty '
          'image_url is reported rather than attempted.'),
      group='Social Media', agent_types=AGENTS, icon='fa-instagram',
      integration='instagram', requires_approval=True, risk='high',
      capability='Instagram publishing',
      parameters=_schema({
          'content_id': _integer('The saved caption to publish.'),
          'caption': _text('Caption to publish, when it is not a saved piece.'),
          'image_url': _text('URL of the image to post.'),
      }))
def publish_instagram_post(ctx, content_id=None, caption='', image_url=''):
    piece = None
    if _int_or_none(content_id) is not None:
        piece, failure = find_piece(content_id)
        if failure is not None:
            return failure
        if piece.is_published:
            return _fail(f'Content piece #{piece.pk} was already published on '
                         f'{piece.published_at:%d %b %Y at %H:%M}.')

    copy = piece.full_text if piece is not None else _flatten(caption)
    if not copy:
        return _fail('Nothing to publish. Give a content_id of a saved caption, '
                     'or pass the caption itself.')

    limits = CHANNEL_LIMITS['instagram']
    warnings = []
    if len(copy) > limits['characters']:
        warnings.append(f'The caption is {len(copy)} characters, '
                        f'{len(copy) - limits["characters"]} over Instagram\'s '
                        f'{limits["characters"]}.')
    tags = len(piece.hashtags or []) if piece is not None else copy.count('#')
    if tags > limits['hashtags']:
        warnings.append(f'{tags} hashtags, over Instagram\'s '
                        f'{limits["hashtags"]}.')
    if not _flatten(image_url):
        warnings.append('No image URL was given. Instagram does not accept a post '
                        'without media, so this will fail unless a reviewer adds '
                        'one before approving.')
    banned = banned_words_in(copy)
    if banned:
        warnings.append('Banned words present: ' + ', '.join(banned) + '.')

    summary = [f'{len(copy)} characters, {tags} hashtags.']
    if piece is not None:
        summary.append(f'Content piece #{piece.pk}.')
    summary.extend(warnings)
    summary.extend(['', 'Caption as it would be published:', '', copy])

    return Proposal(
        title=f'Publish to Instagram: {_truncate(copy, 90)}',
        summary='\n'.join(summary),
        payload={'content_id': piece.pk if piece is not None else None,
                 'caption': copy, 'image_url': _flatten(image_url),
                 'media_type': 'IMAGE'},
        editable_fields=[editable('caption', 'Caption', 'longtext', rows=12),
                         editable('image_url', 'Image URL',
                                  help_text='Instagram will not post without media.')],
        risk='high', integration_key='instagram',
        subject_label='marketing.contentpiece' if piece is not None else '',
        subject_id=piece.pk if piece is not None else 0,
        subject_display=_truncate(copy, 180),
        confirmation=(
            'Prepared the Instagram post and placed it in the approval queue. '
            + (' '.join(warnings) + ' ' if warnings else '')
            + 'Nothing is public until somebody approves it.'))


@executor('mkt.publish_instagram_post')
def _execute_publish_instagram_post(action):
    payload = action.payload or {}
    copy = str(payload.get('caption') or '')
    result = integrations.call('instagram', 'publish_post', caption=copy,
                               image_url=payload.get('image_url') or '',
                               media_type=payload.get('media_type') or 'IMAGE')

    message = _record_outbound(action, channel='instagram',
                               recipient='Instagram account',
                               subject='Instagram post', body=copy, result=result)
    if not result.ok:
        return _failed(result, 'The Instagram publication')

    piece = _piece_from_action(action)
    _mark_published(piece, result)

    where = ('simulated (Instagram is not connected)' if result.demo
             else 'published live')
    lines = [f'The Instagram post was {where}. Outbound message #{message.pk} '
             'records it.']
    reference = _reference_from(result.data)
    if reference:
        lines.append(f'Platform reference: {reference}.')
    if piece is not None:
        lines.append(f'Content piece #{piece.pk} is now marked published.')
    return ToolResult(ok=True, demo=result.demo, text=' '.join(lines),
                      data={'message_id': message.pk,
                            'content_id': piece.pk if piece else None,
                            'external_reference': reference})


@tool(name='mkt.schedule_social_post', title='Schedule a social post',
      description=(
          'Schedule a saved content piece to go out at a given time. LinkedIn '
          'accepts a scheduled post directly; on every other channel the '
          'platform records the schedule and reports that a person will have to '
          'publish at that time, because the connector cannot do it for them. '
          'Scheduling is a commitment, so it goes to the approval queue.'),
      group='Social Media', agent_types=AGENTS, icon='fa-clock',
      integration='linkedin', requires_approval=True, risk='medium',
      capability='Social post scheduling',
      parameters=_schema({
          'content_id': _integer('The saved piece to schedule.'),
          'publish_at': _text('When to publish, e.g. 2026-10-01T09:00.'),
          'channel': _enum('Override the channel on the piece.',
                           tuple(CHANNEL_LIMITS)),
      }, required=('content_id', 'publish_at')))
def schedule_social_post(ctx, content_id, publish_at, channel=''):
    piece, failure = find_piece(content_id)
    if failure is not None:
        return failure
    if piece.is_published:
        return _fail(f'Content piece #{piece.pk} is already published, so there is '
                     'nothing to schedule.')

    when = _parse_moment(publish_at)
    if when is None:
        return _fail(f'"{publish_at}" could not be read as a date and time. Give '
                     'it as YYYY-MM-DD HH:MM.')
    if when <= timezone.now():
        return _fail(f'{when:%d %b %Y at %H:%M} is not in the future, so nothing '
                     'could be scheduled for it. Give a later time.')

    key = _flatten(channel).lower() or piece.channel
    if key not in CHANNEL_LIMITS:
        return _fail(f'"{channel}" is not a channel. Choose from: '
                     f'{", ".join(CHANNEL_LIMITS)}.')

    fits, message = piece.fits_channel
    handled = key == 'linkedin'
    summary = [
        f'Content piece #{piece.pk} on {channel_label(key)}, for '
        f'{when:%A %d %B %Y at %H:%M}.',
        message if not fits else f'Within limits. {message}',
    ]
    if handled:
        summary.append('LinkedIn accepts a scheduled post, so on approval the '
                       'platform hands it to LinkedIn to publish at that time.')
    else:
        summary.append(f'{channel_label(key)} cannot be scheduled through this '
                       'platform. On approval the schedule is recorded and a '
                       'calendar entry is created, and a person will have to '
                       'publish at that time.')
    summary.extend(['', 'Copy as it would go out:', '', piece.full_text])

    return Proposal(
        title=f'Schedule {channel_label(key)} post #{piece.pk} for '
              f'{when:%d %b %H:%M}',
        summary='\n'.join(summary),
        payload={'content_id': piece.pk, 'publish_at': when.isoformat(),
                 'channel': key, 'text': piece.full_text},
        editable_fields=[editable('publish_at', 'Publish at'),
                         editable('text', 'Copy', 'longtext', rows=12)],
        risk='medium', integration_key='linkedin' if handled else '',
        subject_label='marketing.contentpiece', subject_id=piece.pk,
        subject_display=_truncate(piece.full_text, 180),
        confirmation=(
            f'Prepared the schedule for content piece #{piece.pk} at '
            f'{when:%d %b %Y %H:%M} and placed it in the approval queue. '
            + ('' if handled else
               f'{channel_label(key)} cannot be scheduled automatically, so a '
               'person will need to publish at that time. ')
            + 'Nothing is scheduled until somebody approves it.'))


@executor('mkt.schedule_social_post')
def _execute_schedule_social_post(action):
    payload = action.payload or {}
    piece = _piece_from_action(action)
    when = _parse_moment(payload.get('publish_at'))
    if when is None:
        return ToolResult(ok=False, error='unreadable publish time',
                          text='The scheduled time on this action could not be '
                               'read, so nothing was scheduled.')

    key = payload.get('channel') or (piece.channel if piece else 'linkedin')
    copy = str(payload.get('text') or (piece.full_text if piece else ''))

    if key == 'linkedin':
        result = integrations.call('linkedin', 'schedule_post', text=copy,
                                    publish_at=when.isoformat())
        message = _record_outbound(action, channel='linkedin',
                                   recipient='LinkedIn page',
                                   subject=f'Scheduled for {when:%d %b %H:%M}',
                                   body=copy, result=result)
        if not result.ok:
            return _failed(result, 'The LinkedIn scheduling')
        note = ('simulated (LinkedIn is not connected)' if result.demo
                else 'accepted by LinkedIn')
        detail = (f'Scheduled for {when:%d %b %Y at %H:%M} and {note}. Outbound '
                  f'message #{message.pk} records it.')
        demo = result.demo
        record_id = message.pk
    else:
        event = _record_calendar(
            action, title=f'Publish {channel_label(key)} post'
                          + (f' #{piece.pk}' if piece else ''),
            start_at=when, duration_minutes=15,
            description=('This platform cannot publish to '
                         f'{channel_label(key)} on a schedule, so a person has '
                         'to publish at this time. The approved copy:\n\n' + copy),
            subject_label='marketing.contentpiece',
            subject_id=piece.pk if piece else None)
        detail = (f'{channel_label(key)} cannot be scheduled through this '
                  f'platform, so calendar event #{event.pk} was created for '
                  f'{when:%d %b %Y at %H:%M} and a person will have to publish '
                  'at that time.')
        demo = False
        record_id = event.pk

    if piece is not None:
        piece.status = 'scheduled'
        piece.scheduled_for = when
        piece.save(update_fields=['status', 'scheduled_for', 'updated_at'])
        detail += f' Content piece #{piece.pk} is now marked scheduled.'

    return ToolResult(ok=True, demo=demo, text=detail,
                      data={'record_id': record_id, 'channel': key,
                            'scheduled_for': when.isoformat(),
                            'content_id': piece.pk if piece else None})


# ===========================================================================
# GROUP: Email Marketing -- sending, which always needs a person
# ===========================================================================

# A ceiling on one approved send, so a mistaken segment of ten thousand
# addresses cannot be executed by a single click. Hitting it is reported rather
# than silently truncated.
_SEND_CEILING = 50


def _dispatch_mailing(action, *, recipients, subject, body, email_id=None):
    """Send one mailing to a recipient list, one message record per address."""
    delivered = []
    simulated = []
    failed = []
    messages = []

    for address in recipients[:_SEND_CEILING]:
        result = integrations.call('gmail', 'send_email', to=address,
                                   subject=subject, body=body)
        messages.append(_record_outbound(action, channel='email',
                                         recipient=address, subject=subject,
                                         body=body, result=result))
        if not result.ok:
            failed.append((address, result.error))
        elif result.demo:
            simulated.append(address)
        else:
            delivered.append(address)

    row = MarketingEmail.objects.filter(pk=_int_or_none(email_id)).first() \
        if email_id is not None else None

    sent_count = len(delivered) + len(simulated)
    if row is not None and sent_count:
        row.status = 'sent'
        row.sent_at = timezone.now()
        row.recipient_count = sent_count
        row.recipients = list(recipients[:_SEND_CEILING])
        row.save(update_fields=['status', 'sent_at', 'recipient_count',
                                'recipients', 'updated_at'])

    lines = []
    if delivered:
        lines.append(f'{len(delivered)} sent live.')
    if simulated:
        lines.append(f'{len(simulated)} SIMULATED because Gmail is not connected; '
                     'those did not leave the building.')
    if failed:
        lines.append(f'{len(failed)} failed: '
                     + '; '.join(f'{address} ({error})'
                                 for address, error in failed[:5]) + '.')
    if len(recipients) > _SEND_CEILING:
        lines.append(f'The list held {len(recipients)} addresses and only the '
                     f'first {_SEND_CEILING} were attempted. One approval does '
                     'not release an unbounded send; approve a further action for '
                     'the rest.')
    if row is not None and sent_count:
        lines.append(f'Marketing email #{row.pk} is now marked sent.')
    if not sent_count and not failed:
        lines.append('There were no recipients, so nothing was sent.')

    return ToolResult(
        ok=bool(sent_count) or not recipients,
        demo=bool(simulated) and not delivered,
        text=' '.join(lines),
        data={'email_id': row.pk if row is not None else None,
              'delivered': len(delivered), 'simulated': len(simulated),
              'failed': len(failed),
              'message_ids': [message.pk for message in messages]},
        error='; '.join(error for _address, error in failed) if failed and not sent_count
              else '')


def _recipient_list(row, given=''):
    """The addresses a mailing goes to: the explicit one, else the snapshot."""
    addresses = []
    for entry in ([given] if _flatten(given) else []) + list(
            (row.recipients or []) if row is not None else []):
        for part in re.split(r'[,;\s]+', _flatten(entry)):
            address = part.strip()
            if address and address not in addresses:
                addresses.append(address)
    return addresses


@tool(name='mkt.send_marketing_email', title='Send a marketing email',
      description=(
          'Send a marketing email. Pass an email_id of a saved mailing, or the '
          'recipient, subject and body directly. A saved mailing\'s recipient '
          'list is used when no explicit recipient is given. This prepares the '
          'send for the human approval queue with the full copy and the '
          'recipient count attached; it does not send.'),
      group='Email Marketing', agent_types=AGENTS, icon='fa-paper-plane',
      integration='gmail', requires_approval=True, risk='high',
      capability='Marketing email sending',
      parameters=_schema({
          'email_id': _integer('The saved mailing to send.'),
          'to': _text('Recipient address, or several separated by commas.'),
          'subject': _text('Subject line, when it is not a saved mailing.'),
          'body': _text('Body, when it is not a saved mailing.'),
      }))
def send_marketing_email(ctx, email_id=None, to='', subject='', body=''):
    row = None
    if _int_or_none(email_id) is not None:
        row, failure = find_email(email_id)
        if failure is not None:
            return failure
        if row.was_sent:
            return _fail(f'Marketing email #{row.pk} was already sent on '
                         f'{row.sent_at:%d %b %Y at %H:%M}. Sending it again would '
                         'mail the same people twice.')

    line = _flatten(subject) or (row.subject if row is not None else '')
    copy = str(body or '') or (row.body if row is not None else '')
    addresses = _recipient_list(row, to)

    if not line or not _flatten(copy):
        return _fail('A subject and a body are both needed. Pass an email_id of a '
                     'saved mailing, or give the subject and body directly.')
    if not addresses:
        return _fail('There are no recipients. Pass them in "to", or record them '
                     'on the mailing with mkt.create_email_campaign.')

    warnings = []
    if len(line) > EMAIL_SUBJECT_LIMIT:
        warnings.append(f'The subject is {len(line)} characters, '
                        f'{len(line) - EMAIL_SUBJECT_LIMIT} over the '
                        f'{EMAIL_SUBJECT_LIMIT} most inboxes show.')
    banned = banned_words_in(f'{line} {copy}')
    if banned:
        warnings.append('Banned words present: ' + ', '.join(banned) + '.')
    superlatives = superlatives_in(f'{line} {copy}')
    if superlatives:
        warnings.append('Unsubstantiated superlatives present: '
                        + ', '.join(superlatives) + '.')
    if len(addresses) > _SEND_CEILING:
        warnings.append(f'The list holds {len(addresses)} addresses and one '
                        f'approval releases at most {_SEND_CEILING} of them.')

    count = len(addresses)
    summary = [
        f'{count} recipient{"s" if count != 1 else ""}: '
        + ', '.join(addresses[:10]) + ('...' if count > 10 else ''),
        f'Subject ({len(line)} characters): {line}',
    ]
    if row is not None:
        summary.append(f'Marketing email #{row.pk}, '
                       f'{row.get_kind_display().lower()}.')
    summary.extend(warnings)
    summary.extend(['', 'Body as it would be sent:', '', copy])

    return Proposal(
        title=f'Send marketing email to {count} '
              f'recipient{"s" if count != 1 else ""}: {_truncate(line, 70)}',
        summary='\n'.join(summary),
        payload={'email_id': row.pk if row is not None else None,
                 'recipients': addresses, 'subject': line, 'body': copy},
        editable_fields=[editable('subject', 'Subject'),
                         editable('body', 'Body', 'longtext', rows=16)],
        risk='high', integration_key='gmail',
        subject_label='marketing.marketingemail' if row is not None else '',
        subject_id=row.pk if row is not None else 0,
        subject_display=_truncate(line, 180),
        confirmation=(
            f'Prepared the mailing to {count} '
            f'recipient{"s" if count != 1 else ""} and placed it in the approval '
            f'queue. '
            + (' '.join(warnings) + ' ' if warnings else '')
            + 'Nothing is sent until somebody approves it.'))


@executor('mkt.send_marketing_email')
def _execute_send_marketing_email(action):
    payload = action.payload or {}
    recipients = _recipient_list(_email_from_action(action),
                                 ', '.join(payload.get('recipients') or []))
    return _dispatch_mailing(action, recipients=recipients,
                             subject=str(payload.get('subject') or ''),
                             body=str(payload.get('body') or ''),
                             email_id=payload.get('email_id'))


@tool(name='mkt.send_newsletter', title='Send a newsletter',
      description=(
          'Send a saved newsletter to its recorded recipient list, or to an '
          'address you give. This prepares the send for the human approval '
          'queue with the full edition attached; it does not send. A newsletter '
          'reaches people who chose to hear from the company, so the copy is '
          'worth reading once more before approving it.'),
      group='Email Marketing', agent_types=AGENTS, icon='fa-newspaper',
      integration='gmail', requires_approval=True, risk='high',
      capability='Newsletter sending',
      parameters=_schema({
          'email_id': _integer('The saved newsletter to send.'),
          'to': _text('Recipient address, or several separated by commas.'),
      }, required=('email_id',)))
def send_newsletter(ctx, email_id, to=''):
    row, failure = find_email(email_id)
    if failure is not None:
        return failure
    if row.was_sent:
        return _fail(f'Marketing email #{row.pk} was already sent on '
                     f'{row.sent_at:%d %b %Y at %H:%M}.')
    if not _flatten(row.body):
        return _fail(f'Marketing email #{row.pk} has no body, so there is nothing '
                     'to send. Write it with mkt.generate_newsletter first.')

    addresses = _recipient_list(row, to)
    if not addresses:
        return _fail(f'Marketing email #{row.pk} has no recipients. Pass them in '
                     '"to", or record them on the mailing.')

    warnings = []
    if not row.subject_length_ok:
        warnings.append(f'The subject is {row.subject_length} characters, over the '
                        f'{EMAIL_SUBJECT_LIMIT} most inboxes show.')
    banned = banned_words_in(f'{row.subject} {row.body}')
    if banned:
        warnings.append('Banned words present: ' + ', '.join(banned) + '.')

    count = len(addresses)
    summary = [f'Newsletter #{row.pk} "{row.name or row.subject}" to {count} '
               f'recipient{"s" if count != 1 else ""}.',
               f'Subject ({row.subject_length} characters): {row.subject}',
               f'Preheader: {row.preheader or "(none)"}']
    summary.extend(warnings)
    summary.extend(['', 'Edition as it would be sent:', '', row.body])

    return Proposal(
        title=f'Send newsletter #{row.pk} to {count} '
              f'recipient{"s" if count != 1 else ""}: {_truncate(row.subject, 60)}',
        summary='\n'.join(summary),
        payload={'email_id': row.pk, 'recipients': addresses,
                 'subject': row.subject, 'body': row.body},
        editable_fields=[editable('subject', 'Subject'),
                         editable('body', 'Edition body', 'longtext', rows=18)],
        risk='high', integration_key='gmail',
        subject_label='marketing.marketingemail', subject_id=row.pk,
        subject_display=_truncate(row.subject, 180),
        confirmation=(
            f'Prepared newsletter #{row.pk} for {count} '
            f'recipient{"s" if count != 1 else ""} and placed it in the approval '
            'queue. Nothing is sent until somebody approves it.'))


@executor('mkt.send_newsletter')
def _execute_send_newsletter(action):
    payload = action.payload or {}
    recipients = _recipient_list(_email_from_action(action),
                                 ', '.join(payload.get('recipients') or []))
    return _dispatch_mailing(action, recipients=recipients,
                             subject=str(payload.get('subject') or ''),
                             body=str(payload.get('body') or ''),
                             email_id=payload.get('email_id'))


@tool(name='mkt.schedule_marketing_email', title='Schedule a marketing email',
      description=(
          'Schedule a saved mailing to go out at a given time. The platform '
          'records the schedule and creates a calendar entry for it; the send '
          'itself is a separate approved action at that time. Scheduling is a '
          'commitment to mail a segment, so it goes to the approval queue.'),
      group='Email Marketing', agent_types=AGENTS, icon='fa-clock',
      requires_approval=True, risk='medium',
      capability='Marketing email scheduling',
      parameters=_schema({
          'email_id': _integer('The saved mailing to schedule.'),
          'send_at': _text('When to send, e.g. 2026-10-01T09:00.'),
      }, required=('email_id', 'send_at')))
def schedule_marketing_email(ctx, email_id, send_at):
    row, failure = find_email(email_id)
    if failure is not None:
        return failure
    if row.was_sent:
        return _fail(f'Marketing email #{row.pk} was already sent on '
                     f'{row.sent_at:%d %b %Y at %H:%M}, so there is nothing to '
                     'schedule.')

    when = _parse_moment(send_at)
    if when is None:
        return _fail(f'"{send_at}" could not be read as a date and time. Give it '
                     'as YYYY-MM-DD HH:MM.')
    if when <= timezone.now():
        return _fail(f'{when:%d %b %Y at %H:%M} is not in the future. Give a '
                     'later time.')

    count = len(row.recipients or [])
    summary = [
        f'Marketing email #{row.pk} "{row.name or row.subject}" for '
        f'{when:%A %d %B %Y at %H:%M}.',
        f'Subject ({row.subject_length} characters): {row.subject}',
        f'Audience: {row.audience_summary}.',
        'On approval the mailing is marked scheduled and a calendar entry is '
        'created. The send itself is a separate approved action, so scheduling '
        'never becomes sending without a second decision.',
    ]
    if not row.subject_length_ok:
        summary.append(f'The subject is over the {EMAIL_SUBJECT_LIMIT}-character '
                       'inbox window and will be truncated.')

    return Proposal(
        title=f'Schedule mailing #{row.pk} for {when:%d %b %H:%M} '
              f'({count} recipients)',
        summary='\n'.join(summary),
        payload={'email_id': row.pk, 'send_at': when.isoformat(),
                 'subject': row.subject},
        editable_fields=[editable('send_at', 'Send at')],
        risk='medium',
        subject_label='marketing.marketingemail', subject_id=row.pk,
        subject_display=_truncate(row.subject, 180),
        confirmation=(
            f'Prepared the schedule for mailing #{row.pk} at '
            f'{when:%d %b %Y %H:%M} and placed it in the approval queue. '
            'Nothing is scheduled or sent until somebody approves it.'))


@executor('mkt.schedule_marketing_email')
def _execute_schedule_marketing_email(action):
    payload = action.payload or {}
    row = _email_from_action(action)
    when = _parse_moment(payload.get('send_at'))
    if when is None or row is None:
        return ToolResult(
            ok=False, error='missing mailing or unreadable time',
            text='The mailing or the scheduled time on this action could not be '
                 'read, so nothing was scheduled.')

    row.status = 'scheduled'
    row.scheduled_for = when
    row.save(update_fields=['status', 'scheduled_for', 'updated_at'])

    event = _record_calendar(
        action, title=f'Send marketing email #{row.pk}: '
                      f'{_truncate(row.subject, 90)}',
        start_at=when, duration_minutes=15,
        description=(f'Scheduled mailing to {row.audience_summary}. The send is a '
                     'separate approved action; this entry is the reminder that '
                     'it is due.\n\nSubject: ' + row.subject),
        subject_label='marketing.marketingemail', subject_id=row.pk)

    return ToolResult(
        ok=True, text=f'Marketing email #{row.pk} is now marked scheduled for '
                      f'{when:%d %b %Y at %H:%M}, and calendar event '
                      f'#{event.pk} records it. Sending is still a separate '
                      'approved action.',
        data={'email_id': row.pk, 'event_id': event.pk,
              'scheduled_for': when.isoformat()})


# ===========================================================================
# GROUP: Planning -- notifying people, and booking their time
# ===========================================================================

@tool(name='mkt.notify_marketing_team', title='Notify the marketing team',
      description=(
          'Post a message to the marketing team in Slack, optionally attaching a '
          'content piece so the team can read the copy. This prepares the '
          'message for the human approval queue; it does not post. Leave the '
          'channel blank to use the configured default.'),
      group='Planning', agent_types=AGENTS, icon='fa-slack',
      integration='slack', requires_approval=True, risk='medium',
      capability='Team notification',
      parameters=_schema({
          'message': _text('What to tell the team.'),
          'channel': _text('Slack channel, e.g. #marketing. Blank uses the default.'),
          'content_id': _integer('A content piece to include.'),
      }, required=('message',)))
def notify_marketing_team(ctx, message, channel='', content_id=None):
    note = _flatten(message)
    if not note:
        return _fail('A message is needed. Say what the team is being told.')

    piece = None
    if _int_or_none(content_id) is not None:
        piece, failure = find_piece(content_id)
        if failure is not None:
            return failure

    body = note
    if piece is not None:
        body = (f'{note}\n\nContent piece #{piece.pk} '
                f'({channel_label(piece.channel)}, '
                f'{piece.get_status_display().lower()}):\n\n{piece.full_text}')

    where = _flatten(channel) or 'the configured default channel'
    return Proposal(
        title=f'Slack the marketing team: {_truncate(note, 80)}',
        summary=f'To {where}.\n\n{body}',
        payload={'channel': _flatten(channel), 'text': body,
                 'content_id': piece.pk if piece is not None else None},
        editable_fields=[editable('channel', 'Slack channel'),
                         editable('text', 'Message', 'longtext', rows=12)],
        risk='medium', integration_key='slack',
        subject_label='marketing.contentpiece' if piece is not None else '',
        subject_id=piece.pk if piece is not None else 0,
        subject_display=_truncate(note, 180),
        confirmation=(f'Prepared the Slack message to {where} and placed it in the '
                      'approval queue. Nothing is posted until somebody approves it.'))


@executor('mkt.notify_marketing_team')
def _execute_notify_marketing_team(action):
    payload = action.payload or {}
    where = payload.get('channel') or ''
    body = str(payload.get('text') or '')
    result = integrations.call('slack', 'post_message', channel=where, text=body)
    record = _record_outbound(action, channel='slack',
                              recipient=where or 'default channel',
                              subject='Marketing team notification', body=body,
                              result=result)
    if not result.ok:
        return _failed(result, 'The Slack message')
    how = 'simulated (Slack is not connected)' if result.demo else 'posted live'
    return ToolResult(ok=True, demo=result.demo,
                      text=f'The Slack message was {how} to '
                           f'{where or "the default channel"}. Outbound message '
                           f'#{record.pk} records it.',
                      data={'message_id': record.pk, 'channel': where})


@tool(name='mkt.share_campaign_update', title='Share a campaign update',
      description=(
          'Post a campaign update to Slack: where the campaign stands, what has '
          'been published and what is still unsourced. Prepared for the human '
          'approval queue; it does not post. Sharing the unsourced-claim count '
          'is deliberate, because it is the number a campaign review most often '
          'skips over.'),
      group='Planning', agent_types=AGENTS, icon='fa-slack',
      integration='slack', requires_approval=True, risk='medium',
      capability='Campaign update sharing',
      parameters=_schema({
          'campaign_id': _integer('Campaign the update is about.'),
          'message': _text('Anything to add to the generated summary.'),
          'channel': _text('Slack channel. Blank uses the default.'),
      }, required=('campaign_id',)))
def share_campaign_update(ctx, campaign_id, message='', channel=''):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure
    if campaign is None:
        return _fail('A campaign_id is needed. Call mkt.list_content or '
                     'mkt.create_campaign first.')

    brief = latest_brief(campaign)
    pieces = list(campaign.content_pieces.all())
    published = [piece for piece in pieces if piece.is_published]
    unsourced = [piece for piece in pieces if not piece.sources]
    mailings = list(campaign.emails.all())
    sent = [row for row in mailings if row.was_sent]

    lines = [f'Campaign update: {campaign.name} (#{campaign.pk}), '
             f'{campaign.get_status_display().lower()}.',
             f'Objective: {campaign.objective}',
             f'Audience: {campaign.target_audience}']
    if brief is not None:
        lines.append(f'Channels: {", ".join(brief.channel_labels) or "none set"}')
    lines.append(f'Content: {len(pieces)} pieces, {len(published)} published.')
    lines.append(f'Email: {len(mailings)} mailings, {len(sent)} sent.')
    if unsourced:
        lines.append(f'{len(unsourced)} pieces have no recorded sources, so every '
                     'claim in them is unsourced: '
                     + ', '.join(f'#{piece.pk}' for piece in unsourced[:8]) + '.')
    if brief is not None and brief.unsourced_claims:
        lines.append(f'{len(brief.unsourced_claims)} proof points on the brief name '
                     'no document.')
    if brief is not None and brief.success_measures:
        lines.append('Success measures on the brief: '
                     + '; '.join(brief.success_measures[:4]) + '.')
    if _flatten(message):
        lines.extend(['', _flatten(message)])

    body = '\n'.join(lines)
    where = _flatten(channel) or 'the configured default channel'
    return Proposal(
        title=f'Slack campaign update: {_truncate(campaign.name, 80)}',
        summary=f'To {where}.\n\n{body}',
        payload={'campaign_id': campaign.pk, 'channel': _flatten(channel),
                 'text': body},
        editable_fields=[editable('channel', 'Slack channel'),
                         editable('text', 'Update', 'longtext', rows=14)],
        risk='medium', integration_key='slack',
        subject_label='marketing.marketingcampaign', subject_id=campaign.pk,
        subject_display=_truncate(campaign.name, 180),
        confirmation=(f'Prepared the campaign update for {where} and placed it in '
                      'the approval queue. Nothing is posted until somebody '
                      'approves it.'))


@executor('mkt.share_campaign_update')
def _execute_share_campaign_update(action):
    payload = action.payload or {}
    where = payload.get('channel') or ''
    body = str(payload.get('text') or '')
    result = integrations.call('slack', 'post_message', channel=where, text=body)
    record = _record_outbound(action, channel='slack',
                              recipient=where or 'default channel',
                              subject='Campaign update', body=body, result=result)
    if not result.ok:
        return _failed(result, 'The Slack campaign update')
    how = 'simulated (Slack is not connected)' if result.demo else 'posted live'
    return ToolResult(ok=True, demo=result.demo,
                      text=f'The campaign update was {how} to '
                           f'{where or "the default channel"}. Outbound message '
                           f'#{record.pk} records it.',
                      data={'message_id': record.pk,
                            'campaign_id': payload.get('campaign_id'),
                            'channel': where})


@tool(name='mkt.request_content_approval', title='Request content approval',
      description=(
          'Post a review request to Slack asking a colleague to read a content '
          'piece before it is published. This notifies humans in Slack and is '
          'itself queued for approval, which is not circular: the Slack message '
          'is an outbound action like any other, and the approval releasing it '
          'is the approval of a message, not of the content. The content still '
          'needs its own publishing approval afterwards.'),
      group='Planning', agent_types=AGENTS, icon='fa-clipboard-check',
      integration='slack', requires_approval=True, risk='medium',
      capability='Content review requests',
      parameters=_schema({
          'content_id': _integer('The piece to be reviewed.'),
          'channel': _text('Slack channel. Blank uses the default.'),
          'message': _text('What the reviewer should look at in particular.'),
      }, required=('content_id',)))
def request_content_approval(ctx, content_id, channel='', message=''):
    piece, failure = find_piece(content_id)
    if failure is not None:
        return failure
    if piece.is_published:
        return _fail(f'Content piece #{piece.pk} is already published, so a review '
                     'request would arrive too late to matter.')

    fits, limit_message = piece.fits_channel
    lines = [f'Review request: content piece #{piece.pk} for '
             f'{channel_label(piece.channel)}.',
             f'Status: {piece.get_status_display()}. '
             f'{piece.character_count} characters, '
             f'{len(piece.hashtags or [])} hashtags.']
    if not fits:
        lines.append(f'Over the channel limit: {limit_message}')
    documents = piece.source_documents
    lines.append('Sources: ' + (', '.join(documents) if documents else
                                'none recorded, so every claim in it is unsourced'))
    findings = banned_words_in(piece.full_text) + superlatives_in(piece.full_text)
    if findings:
        lines.append('Words worth a second look: ' + ', '.join(findings) + '.')
    if _flatten(message):
        lines.append('')
        lines.append(_flatten(message))
    lines.extend(['', piece.full_text])

    body = '\n'.join(lines)
    where = _flatten(channel) or 'the configured default channel'
    return Proposal(
        title=f'Request review of content piece #{piece.pk} '
              f'({channel_label(piece.channel)})',
        summary=f'To {where}.\n\n{body}',
        payload={'content_id': piece.pk, 'channel': _flatten(channel),
                 'text': body},
        editable_fields=[editable('channel', 'Slack channel'),
                         editable('text', 'Review request', 'longtext', rows=16)],
        risk='medium', integration_key='slack',
        subject_label='marketing.contentpiece', subject_id=piece.pk,
        subject_display=_truncate(piece.full_text, 180),
        confirmation=(
            f'Prepared the review request for {where} and placed it in the '
            'approval queue. Approving it posts the request; publishing the '
            'content is still a separate approval.'))


@executor('mkt.request_content_approval')
def _execute_request_content_approval(action):
    payload = action.payload or {}
    where = payload.get('channel') or ''
    body = str(payload.get('text') or '')
    result = integrations.call('slack', 'post_message', channel=where, text=body)
    record = _record_outbound(action, channel='slack',
                              recipient=where or 'default channel',
                              subject='Content review request', body=body,
                              result=result)
    if not result.ok:
        return _failed(result, 'The Slack review request')

    piece = _piece_from_action(action)
    detail = ''
    if piece is not None and piece.status == 'draft':
        piece.status = 'pending'
        piece.save(update_fields=['status', 'updated_at'])
        detail = f' Content piece #{piece.pk} is now marked pending review.'

    how = 'simulated (Slack is not connected)' if result.demo else 'posted live'
    return ToolResult(ok=True, demo=result.demo,
                      text=f'The review request was {how} to '
                           f'{where or "the default channel"}. Outbound message '
                           f'#{record.pk} records it.' + detail,
                      data={'message_id': record.pk,
                            'content_id': piece.pk if piece else None,
                            'channel': where})


@tool(name='mkt.schedule_campaign_activity', title='Book a campaign meeting',
      description=(
          'Book a campaign activity on the shared calendar: a review, a launch '
          'checkpoint, a retrospective. Prepared for the human approval queue; '
          'nothing is booked and no invitation reaches anybody until it is '
          'approved. Give an agenda -- a campaign meeting with no agenda is the '
          'one most likely to be a status recital.'),
      group='Planning', agent_types=AGENTS, icon='fa-calendar-plus',
      integration='google_calendar', requires_approval=True, risk='medium',
      capability='Campaign meeting scheduling',
      parameters=_schema({
          'campaign_id': _integer('Campaign the activity belongs to.'),
          'title': _text('What the meeting is.'),
          'start_at': _text('When it starts, e.g. 2026-10-01T14:00.'),
          'duration_minutes': _integer('How long. Sixty by default.'),
          'attendees': _array('Email addresses to invite.'),
          'agenda': _text('What will be decided or reviewed.'),
      }, required=('campaign_id', 'title', 'start_at')))
def schedule_campaign_activity(ctx, campaign_id, title, start_at,
                               duration_minutes=60, attendees=None, agenda=''):
    campaign, failure = find_campaign(campaign_id)
    if failure is not None:
        return failure
    if campaign is None:
        return _fail('A campaign_id is needed, so the activity belongs to '
                     'something.')

    name = _flatten(title)
    if not name:
        return _fail('A title is needed. Name what the meeting is for.')

    when = _parse_moment(start_at)
    if when is None:
        return _fail(f'"{start_at}" could not be read as a date and time. Give it '
                     'as YYYY-MM-DD HH:MM.')
    if when <= timezone.now():
        return _fail(f'{when:%d %b %Y at %H:%M} is in the past. Give a future time.')

    minutes = max(15, min(_int_or_none(duration_minutes) or 60, 480))
    people = []
    for entry in (attendees or []):
        address = _flatten(entry)
        if address and address not in people:
            people.append(address)

    plan = _flatten(agenda) or (
        f'No agenda was given. At a minimum: what has been published on '
        f'{campaign.name}, what is still unsourced, and what the next two weeks '
        'commit to.')

    summary = [
        f'{name} for campaign #{campaign.pk} "{campaign.name}".',
        f'{when:%A %d %B %Y at %H:%M}, {minutes} minutes.',
        f'Attendees: {", ".join(people) if people else "none listed"}.',
        '', 'Agenda:', plan,
    ]

    return Proposal(
        title=f'Book "{_truncate(name, 70)}" on {when:%d %b at %H:%M}',
        summary='\n'.join(summary),
        payload={'campaign_id': campaign.pk, 'title': name,
                 'start_at': when.isoformat(), 'duration_minutes': minutes,
                 'attendees': people, 'agenda': plan},
        editable_fields=[editable('title', 'Meeting title'),
                         editable('start_at', 'Start at'),
                         editable('duration_minutes', 'Duration in minutes'),
                         editable('agenda', 'Agenda', 'longtext', rows=8)],
        risk='medium', integration_key='google_calendar',
        subject_label='marketing.marketingcampaign', subject_id=campaign.pk,
        subject_display=_truncate(name, 180),
        confirmation=(
            f'Prepared the booking for "{name}" on {when:%d %b %Y at %H:%M} and '
            'placed it in the approval queue. Nothing is on anybody\'s calendar '
            'until somebody approves it.'))


@executor('mkt.schedule_campaign_activity')
def _execute_schedule_campaign_activity(action):
    payload = action.payload or {}
    when = _parse_moment(payload.get('start_at'))
    if when is None:
        return ToolResult(ok=False, error='unreadable start time',
                          text='The start time on this action could not be read, '
                               'so nothing was booked.')

    minutes = _int_or_none(payload.get('duration_minutes')) or 60
    people = [_flatten(entry) for entry in (payload.get('attendees') or [])
              if _flatten(entry)]
    name = str(payload.get('title') or 'Campaign activity')
    plan = str(payload.get('agenda') or '')

    # The connector's argument is ``start``, not ``start_at``. The payload key
    # stays ``start_at`` because that is what the reviewer sees on the approval
    # page and what every other scheduling tool here calls it.
    result = integrations.call(
        'google_calendar', 'create_event', title=name,
        start=when.isoformat(), duration_minutes=minutes,
        attendees=people, description=plan)

    event = _record_calendar(
        action, title=name, start_at=when, duration_minutes=minutes,
        attendees=people, description=plan, result=result,
        subject_label='marketing.marketingcampaign',
        subject_id=_int_or_none(payload.get('campaign_id')))

    if not result.ok:
        return _failed(result, 'The calendar booking')

    how = ('simulated (Google Calendar is not connected)' if result.demo
           else 'booked live')
    return ToolResult(
        ok=True, demo=result.demo,
        text=f'"{name}" was {how} for {when:%d %b %Y at %H:%M}, {minutes} '
             f'minutes, with {len(people)} attendees. Calendar event '
             f'#{event.pk} records it.',
        data={'event_id': event.pk, 'start_at': when.isoformat(),
              'attendees': people,
              'external_reference': _reference_from(result.data)})


@tool(name='mkt.upload_marketing_asset', title='Upload a marketing asset',
      description=(
          'Upload a marketing asset to Google Drive: a finished piece of copy, a '
          'brief, a campaign write-up. Prepared for the human approval queue, '
          'because a file in a shared Drive is visible to everyone who has the '
          'folder. Nothing is uploaded until it is approved.'),
      group='Planning', agent_types=AGENTS, icon='fa-cloud-arrow-up',
      integration='google_drive', requires_approval=True, risk='medium',
      capability='Asset upload',
      parameters=_schema({
          'name': _text('File name.'),
          'content': _text('The file content.'),
          'folder_id': _text('Drive folder id. Blank uses the configured default.'),
      }, required=('name', 'content')))
def upload_marketing_asset(ctx, name, content, folder_id=''):
    filename = _flatten(name)
    if not filename:
        return _fail('A file name is needed.')
    if not _flatten(content):
        return _fail('There is no content to upload. An empty file in a shared '
                     'Drive is worse than no file.')

    body = str(content)
    where = _flatten(folder_id) or 'the configured default folder'
    return Proposal(
        title=f'Upload "{_truncate(filename, 80)}" to Google Drive',
        summary=(f'{len(body)} characters, {len(body.split())} words, into '
                 f'{where}.\n\n' + _truncate(body, 2000)),
        payload={'name': filename, 'content': body,
                 'folder_id': _flatten(folder_id)},
        editable_fields=[editable('name', 'File name'),
                         editable('folder_id', 'Folder id'),
                         editable('content', 'File content', 'longtext', rows=18)],
        risk='medium', integration_key='google_drive',
        subject_display=_truncate(filename, 180),
        confirmation=(f'Prepared the upload of "{filename}" to {where} and placed '
                      'it in the approval queue. Nothing is in Drive until '
                      'somebody approves it.'))


@executor('mkt.upload_marketing_asset')
def _execute_upload_marketing_asset(action):
    payload = action.payload or {}
    filename = str(payload.get('name') or 'marketing-asset.txt')
    body = str(payload.get('content') or '')
    result = integrations.call('google_drive', 'upload_file', name=filename,
                               content=body,
                               folder_id=payload.get('folder_id') or '')
    if not result.ok:
        return _failed(result, 'The Drive upload')

    # There is no OutboundMessage or CalendarEvent that honestly describes a
    # file upload -- it is neither a message nor a meeting -- so the record of
    # this execution is the ProposedAction itself plus the audit entry that
    # approvals.py writes. The reference below is what makes it findable.
    how = ('simulated (Google Drive is not connected)' if result.demo
           else 'uploaded live')
    reference = _reference_from(result.data)
    url = _url_from(result.data)
    lines = [f'"{filename}" was {how}, {len(body)} characters.']
    if reference:
        lines.append(f'Drive reference: {reference}.')
    if url:
        lines.append(url)
    return ToolResult(ok=True, demo=result.demo, text=' '.join(lines),
                      data={'name': filename, 'characters': len(body),
                            'file_id': reference, 'url': url})

