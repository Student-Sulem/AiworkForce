"""Managing a mailbox by typing at an employee.

WHAT THIS MODULE IS
-------------------
The bridge between what a person types in the chat and the real mailbox. It
answers one question per turn: *is this message about the mailbox, and if so,
which tool should run?*

    "any new mail?"                -> gmail.list_messages   (unread)
    "show my last 5 emails"        -> gmail.list_messages
    "anything from NVIDIA?"        -> gmail.search_messages
    "open 2" / "read the third"    -> gmail.read_message
    "reply to 2 saying thanks"     -> gmail.read_message, then Aria drafts
    "email sam@x.com about pricing" -> Aria drafts, reviewer sends
    "save that as a draft"         -> gmail.create_draft

WHY A ROUTER RATHER THAN LETTING THE MODEL DECIDE
-------------------------------------------------
Asking the language model to choose the tool would be one more round trip, and
would make the mailbox stop working whenever the model was slow, rate-limited
or absent. Since the whole application is built to keep working offline, the
routing is done here, in Python, where it is deterministic and testable. The
model is still what *writes* -- it phrases the answer and composes the emails --
but it is never what decides whether to open the mailbox.

The practical benefit is that "check my inbox" behaves identically on a machine
with no API key at all.

HOW A LISTING IS REMEMBERED
---------------------------
"open the second one" needs to know what was listed. The listing is stored on
the assistant's own ChatMessage in `metadata['mail']`, so the reference is
resolved against the last thing actually shown in this thread. Nothing is held
in a session or a global, which means two people in two tabs cannot collide.
"""

import re

from . import gmail_client, mcp_client
from .models import ChatMessage, MCPTool

# How many messages a listing shows unless the person asks for a number.
DEFAULT_LISTING = 5
MAX_LISTING = 25

EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')

# Ordinals people actually type when pointing at a listing.
ORDINALS = {
    'first': 1, '1st': 1, 'one': 1,
    'second': 2, '2nd': 2, 'two': 2,
    'third': 3, '3rd': 3, 'three': 3,
    'fourth': 4, '4th': 4, 'four': 4,
    'fifth': 5, '5th': 5, 'five': 5,
    'sixth': 6, '6th': 6, 'six': 6,
    'seventh': 7, '7th': 7, 'seven': 7,
    'eighth': 8, '8th': 8, 'eight': 8,
    'ninth': 9, '9th': 9, 'nine': 9,
    'tenth': 10, '10th': 10, 'ten': 10,
    'last': -1, 'latest': 1, 'newest': 1, 'top': 1,
}

# --------------------------------------------------------------------------
# intent patterns
#
# Ordered: the first match wins, so the more specific patterns come first.
# Each is anchored on a verb a person would actually use, not on a keyword
# that might appear in the middle of an unrelated sentence.
# --------------------------------------------------------------------------

_UNREAD_RE = re.compile(
    r'\b(?:any\s+)?(?:new|unread|unseen)\s+(?:mail|email|emails|messages)\b'
    r'|\bany(?:thing)?\s+new\b'
    r'|\bunread\b',
    re.IGNORECASE)

_LIST_RE = re.compile(
    r'\b(?:check|show|list|see|open|read|what.s\s+in|catch\s+up\s+on)\b[^.?!]*'
    r'\b(?:inbox|mail|mailbox|email|emails|messages)\b'
    r'|\b(?:my|recent|latest)\s+(?:mail|email|emails|messages)\b',
    re.IGNORECASE)

_SEARCH_RE = re.compile(
    r'\b(?:search|find|look\s+for|any(?:thing)?\s+from|emails?\s+from|mail\s+from)\b\s*'
    r'(?:for\s+|about\s+)?(?P<query>[^.?!]+)',
    re.IGNORECASE)

# Filler between the verb and the reference: "show *me the* third *one*".
_FILLER = r'(?:the|me|us|my|message|messages|email|emails|mail|number|no\.?|item|#)'

_OPEN_RE = re.compile(
    r'\b(?:open|read|show|view|expand|pull\s+up)\b\s*'
    r'(?:' + _FILLER + r'\s*)*'
    r'(?P<which>\d+|' + '|'.join(ORDINALS) + r')\b'
    r'(?P<rest>.*)',
    re.IGNORECASE)

_REPLY_RE = re.compile(
    r'\b(?:reply|respond|answer)\b\s*'
    r'(?:to\s+)?(?:' + _FILLER + r'\s*)*'
    r'(?P<which>\d+|' + '|'.join(ORDINALS) + r')\b'
    r'(?P<rest>.*)',
    re.IGNORECASE)

_DRAFT_RE = re.compile(
    r'\b(?:save|store|keep)\b[^.?!]*\bdraft\b'
    r'|\bdraft\s+(?:it|that|this)\b',
    re.IGNORECASE)

_COUNT_RE = re.compile(r'\b(?:last|latest|recent|top|first)?\s*(\d{1,2})\b')

# "mail"/"inbox" anywhere at all -- the weakest signal, used only to decide
# whether an unrecognised sentence is even about email.
_MAILISH_RE = re.compile(r'\b(?:mail|mailbox|inbox|email|emails|message|messages)\b',
                         re.IGNORECASE)


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def detect(text, conversation=None):
    """Work out which mailbox action a message is asking for.

    Returns a dict with at least `action`, or None when the message is not
    about the mailbox and should be handled as ordinary conversation.
    """
    said = (text or '').strip()
    if not said:
        return None

    # An explicit address means composing, which the existing drafting path
    # already handles. Do not hijack it into a search.
    addresses = EMAIL_RE.findall(said)

    reply = _REPLY_RE.search(said)
    if reply:
        index = _resolve_index(reply.group('which'))
        if index is not None:
            return {'action': 'reply', 'index': index,
                    'instruction': _clean_instruction(reply.group('rest'))}

    if _DRAFT_RE.search(said):
        return {'action': 'draft'}

    if addresses:
        return None

    if _UNREAD_RE.search(said):
        return {'action': 'list', 'unread_only': True, 'limit': _count_in(said)}

    open_match = _OPEN_RE.search(said)
    if open_match and _looks_like_a_reference(open_match):
        index = _resolve_index(open_match.group('which'))
        if index is not None:
            return {'action': 'open', 'index': index}

    search = _SEARCH_RE.search(said)
    if search:
        query = _clean_query(search.group('query'))
        if query:
            return {'action': 'search', 'query': query, 'limit': _count_in(said)}

    if _LIST_RE.search(said):
        return {'action': 'list', 'unread_only': False, 'limit': _count_in(said)}

    return None


# What may legitimately follow a reference: "open the 2nd *one please*".
_TRAILING_FILLER_RE = re.compile(
    r'^(?:\s|one|message|messages|email|emails|mail|item|please|for\s+me|'
    r'in\s+full|properly|now|thanks|[.,!?])*$',
    re.IGNORECASE)


def _looks_like_a_reference(match):
    """Guard "open 2" against "read 2 articles about marketing".

    Both start with a verb and a number. The difference is everything after it:
    a reference to a listing is finished once the number is given, whereas the
    second sentence carries the noun the number was counting. So the test is on
    the tail, not on the length of the sentence.
    """
    return bool(_TRAILING_FILLER_RE.match(match.group('rest') or ''))


def _clean_instruction(raw):
    """What the reply should say, with the pointing words removed.

    "reply to the first one" carries no instruction at all; the trailing "one"
    belongs to the reference, not to the message being asked for.
    """
    text = (raw or '').strip(' ,:-')
    text = re.sub(r'^(?:one|message|email|mail|item)\b', '', text,
                  flags=re.IGNORECASE).strip(' ,:-')
    text = re.sub(r'^(?:and|saying|to\s+say|with|that)\s+', '', text,
                  flags=re.IGNORECASE)
    return text.strip(' ,:-')


def _resolve_index(token):
    """Turn "3", "third" or "last" into a 1-based position, or -1 for last."""
    token = (token or '').strip().lower()
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= MAX_LISTING else None
    return ORDINALS.get(token)


def _count_in(said):
    """"show my last 5 emails" -> 5."""
    match = _COUNT_RE.search(said or '')
    if not match:
        return DEFAULT_LISTING
    return max(1, min(int(match.group(1)), MAX_LISTING))


def _clean_query(raw):
    """Trim the filler words off a search phrase."""
    query = (raw or '').strip(' ,.?!:;')

    # Repeatedly, because "find mail about the timetable" has two layers of it
    # and a single pass left the query as "about the timetable".
    leading = re.compile(
        r'^(?:for|about|any|anything|all|some|the|my|emails?|mails?|messages?|'
        r'related\s+to|regarding)\s+', re.IGNORECASE)
    while True:
        trimmed = leading.sub('', query, count=1)
        if trimmed == query:
            break
        query = trimmed

    query = re.sub(r'\s+(?:in\s+my\s+)?(?:inbox|mailbox|mail|emails?)$', '', query,
                   flags=re.IGNORECASE)
    return query.strip(' ,.?!:;')


# --------------------------------------------------------------------------
# the listing a reference points at
# --------------------------------------------------------------------------

def last_listing(conversation):
    """The most recent mailbox listing shown in this thread."""
    recent = (ChatMessage.objects
              .filter(conversation=conversation, role='assistant')
              .order_by('-created_at')[:12])
    for message in recent:
        listing = message.mail_listing
        if listing:
            return listing
    return []


def pick(listing, index):
    """Resolve a 1-based position, or -1 for the last, against a listing."""
    if not listing:
        return None
    if index == -1:
        return listing[-1]
    if 1 <= index <= len(listing):
        return listing[index - 1]
    return None


# --------------------------------------------------------------------------
# execution -- every call goes through the MCP tool record
# --------------------------------------------------------------------------

def _tool(agent, qualified_name):
    """The MCPTool row for a capability, if this employee actually has it.

    Going through the tool record rather than calling gmail_client directly is
    what makes the MCP Tools page real: every mailbox action shows up there as
    a call, with its arguments, outcome and duration.
    """
    server_key, _, tool_name = qualified_name.partition('.')
    return MCPTool.objects.filter(
        server__server_key=server_key,
        tool_name=tool_name,
        agent_links__agent=agent,
        agent_links__is_enabled=True,
        is_enabled=True,
        server__is_enabled=True,
    ).select_related('server').first()


def _record(agent, qualified_name, arguments, result):
    tool = _tool(agent, qualified_name)
    if tool is None:
        return None
    summary = result.get('message') or result.get('summary') or ''
    mcp_client.record_tool_call(
        agent, tool, arguments=arguments, summary=summary,
        outcome='ok' if result.get('ok') else 'error')
    return tool.qualified_name


def withheld(agent, qualified_name):
    """Why this employee may not use a tool right now, or '' if it may.

    The tool record is the authority, not this module. Switching the Gmail
    server off on the MCP Tools page, disabling one tool, or detaching it from
    an employee all take the capability away immediately -- which is the point
    of modelling MCP at all, and is worth demonstrating.
    """
    if _tool(agent, qualified_name) is not None:
        return ''

    exists = MCPTool.objects.filter(
        server__server_key=qualified_name.split('.')[0],
        tool_name=qualified_name.split('.')[-1]).first()

    if exists is None:
        return f'MAILBOX: the {qualified_name} tool is not installed.'
    if not exists.server.is_enabled:
        return (f'MAILBOX: the {exists.server.name} MCP server is switched off, so the '
                f'mailbox cannot be opened. Tell the person to enable it on the MCP '
                f'Tools page. Do not attempt anything else.')
    if not exists.is_enabled:
        return (f'MAILBOX: the {qualified_name} tool is disabled on the MCP Tools page. '
                f'Tell the person that, in one sentence.')
    return (f'MAILBOX: {agent.name} does not have the {qualified_name} tool attached. '
            f'Tell the person it can be attached from the employee configuration panel.')

def run(agent, intent, conversation):
    """Carry out a detected intent against the real mailbox.

    Returns a dict:

        facts     compact text handed to the language model so it can phrase
                  the answer in its own voice. Bounded on purpose -- a 6,000
                  character email body would swamp the context window.
        plain     the same information written out as a finished reply, used
                  when there is no live model. Every feature of this project
                  has to work offline, and the mailbox is no exception.
        metadata  structured data stored on the reply, which the template turns
                  into cards and buttons.
        tools     qualified tool names to show under the reply.
    """
    action = intent.get('action')
    if action == 'list':
        return _run_list(agent, intent)
    if action == 'search':
        return _run_search(agent, intent)
    if action in ('open', 'reply'):
        return _run_open(agent, intent, conversation)
    if action == 'draft':
        return _run_draft(agent, conversation)
    return _result()


def _result(facts='', plain='', metadata=None, tools=()):
    return {'facts': facts, 'plain': plain or facts,
            'metadata': metadata or {}, 'tools': [t for t in tools if t]}


def _denied(message):
    """A refusal reads the same whether or not a model phrased it."""
    spoken = message.replace('MAILBOX: ', '')
    return _result(facts=message, plain=spoken)


def _run_list(agent, intent):
    withheld_reason = withheld(agent, 'gmail.list_messages')
    if withheld_reason:
        return _denied(withheld_reason)

    unread_only = bool(intent.get('unread_only'))
    limit = intent.get('limit') or DEFAULT_LISTING
    result = gmail_client.list_messages(
        limit=limit, unread_only=unread_only, with_body=True)
    tools = [_record(agent, 'gmail.list_messages',
                     {'unread_only': unread_only, 'limit': limit}, result)]
    return _shape(result, tools, empty='Nothing in the mailbox matches that.')


def _run_search(agent, intent):
    withheld_reason = withheld(agent, 'gmail.search_messages')
    if withheld_reason:
        return _denied(withheld_reason)

    query = intent.get('query', '')
    limit = intent.get('limit') or DEFAULT_LISTING
    result = gmail_client.search_messages(query, limit=limit, with_body=True)
    tools = [_record(agent, 'gmail.search_messages',
                     {'query': query, 'limit': limit}, result)]
    return _shape(result, tools, empty=f'Nothing in the mailbox matches "{query}".')


def _run_open(agent, intent, conversation):
    withheld_reason = withheld(agent, 'gmail.read_message')
    if withheld_reason:
        return _denied(withheld_reason)

    listing = last_listing(conversation)
    chosen = pick(listing, intent.get('index'))

    if chosen is None:
        spoken = ('I have not shown you a list of messages in this thread yet, so '
                  'there is nothing to open. Ask me to check your inbox first.')
        return _result(facts='MAILBOX: ' + spoken, plain=spoken)

    result = gmail_client.get_message(chosen['uid'])
    tools = [_record(agent, 'gmail.read_message', {'uid': chosen['uid']}, result)]

    if not result.get('ok'):
        return _result(facts=f'MAILBOX ERROR: {result.get("message")}',
                       plain=result.get('message', 'The mailbox could not be opened.'),
                       tools=tools)

    full = result['mail']
    metadata = {'mail': [full], 'opened': full['uid']}

    facts = ('MAILBOX: the person asked to open a message. Here it is in full.\n'
             'Tell them what it says, in two or three sentences. Lead with what '
             'it wants from them, if anything. Do not ask whether they would '
             'like a summary -- they already asked, so give it.\n\n'
             f'From: {full["sender"]}\n'
             f'Date: {full["date_display"]}\n'
             f'Subject: {full["subject"]}\n\n'
             f'{full["body"][:2500]}')

    plain = (f'**{full["subject"]}**\n\n'
             f'From {full["sender"]} on {full["date_display"]}.\n\n'
             f'{full["body"][:1200]}')

    if intent.get('action') == 'reply':
        instruction = intent.get('instruction') or 'a brief, courteous reply'
        metadata['draft'] = {'recipient': full['sender_email'],
                             'in_reply_to': full['uid']}
        facts += (f'\n\nWrite a reply to {full["sender_email"]}. '
                  f'The person wants it to say: {instruction}.\n'
                  f'Answer with the reply only, starting '
                  f'"Subject: Re: {full["subject"]}".')
        plain = (f'Subject: Re: {full["subject"]}\n\n'
                 f'Hello,\n\n{instruction.capitalize()}.\n\n'
                 f'Best regards')

    return _result(facts=facts, plain=plain, metadata=metadata, tools=tools)


def _run_draft(agent, conversation):
    """Save the employee's most recent draft into the real Drafts folder."""
    withheld_reason = withheld(agent, 'gmail.create_draft')
    if withheld_reason:
        return _denied(withheld_reason)

    from .agent_engine import split_subject

    previous = (ChatMessage.objects
                .filter(conversation=conversation, role='assistant')
                .order_by('-created_at')[:6])

    for message in previous:
        recipient = message.draft_recipient
        if not recipient:
            continue
        subject, body = split_subject(message.content, fallback='Draft')
        result = gmail_client.save_draft(subject, body, recipient)
        tools = [_record(agent, 'gmail.create_draft',
                         {'recipient': recipient, 'subject': subject}, result)]
        return _result(
            facts=(f'MAILBOX: {result["message"]} Tell the person plainly, in one '
                   f'sentence, that the draft is saved in Gmail.'),
            plain=result['message'],
            tools=tools)

    spoken = ('There is no email in this thread to save yet. Tell me who to write '
              'to and what to say, and I will draft it.')
    return _result(facts='MAILBOX: ' + spoken, plain=spoken)


def _shape(result, tools, empty):
    """Turn a mailbox result into facts, a plain reply, metadata and tools."""
    if not result.get('ok'):
        return _result(facts=f'MAILBOX ERROR: {result.get("message")}',
                       plain=result.get('message', 'The mailbox could not be opened.'),
                       tools=tools)

    found = result.get('messages') or []
    if not found:
        return _result(facts=f'MAILBOX: {empty}', plain=empty, tools=tools)

    facts = [f'MAILBOX: {result.get("message", "")}',
             'The listing is already shown to the person as cards, so do not '
             'repeat it line by line. Introduce it in one or two sentences and '
             'say what stands out. Refer to messages by their number.', '']
    for position, item in enumerate(found, start=1):
        facts.append(
            f'{position}. {item["sender"]} - "{item["subject"]}" '
            f'({item["date_display"]}{", unread" if item["is_unread"] else ""})\n'
            f'   {item["snippet"]}')

    # The cards carry the detail, so the offline reply only has to introduce
    # them. Repeating the list here would show everything twice.
    plain = result.get('message', f'{len(found)} messages.')

    return _result(facts='\n'.join(facts), plain=plain,
                   metadata={'mail': found}, tools=tools)
