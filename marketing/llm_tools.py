"""Tool calling for the three providers, built on the existing HTTP layer.

WHY THIS MODULE EXISTS AT ALL
-----------------------------
``llm_client.chat_conversation`` is the project's original generation call. It
takes a system prompt and a list of user/assistant turns, and it returns text.
That is everything a chat window needs and nothing an *employee* needs: there
is no way to advertise a tool, no way to read back a request to call one, and
no way to put a tool's result into the transcript so the model can carry on.

Rather than widen that function -- and risk changing the behaviour of the chat
path that already works -- the tool-calling protocol lives here. The two
modules share one HTTP primitive, one header builder, one error classifier and
one reasoning stripper, imported below, so there is exactly one place where
this application talks to a provider over the network.

THE CALLER OWNS THE TRANSCRIPT
------------------------------
``chat_with_tools`` takes the complete message list, system message and
tool-result messages included, and returns without remembering anything. The
conversation loop in ``agent_runtime`` therefore holds the whole state of a
turn in one local list, which is what makes the repeat guard and the round cap
in that module possible to write and to read.

THREE SHAPES OF THE SAME IDEA
-----------------------------
    OpenRouter, NVIDIA   ``tools`` on the request, ``choices[0].message
                         .tool_calls`` on the response, arguments as a JSON
                         *string*.
    Ollama (0.4+)        ``tools`` on the request, ``message.tool_calls`` on
                         the response, arguments as a JSON *object*, and a
                         tool result identified by tool name rather than by a
                         call id.

``parse_tool_calls`` and ``tool_result_message`` absorb that difference so the
loop above never asks which provider it is talking to.

AND A FOURTH SHAPE, WHICH IS WHY ``extract_text_tool_calls`` EXISTS
-------------------------------------------------------------------
Smaller open-weight models -- exactly the free ones a reader of this project is
most likely to configure -- frequently ignore the tools parameter and write the
call out as text instead:

    <tool_call>{"name": "hr.list_candidates", "arguments": {}}</tool_call>

If that is treated as prose, the platform answers "here is what I would do" and
does nothing, which is indistinguishable from being broken. Recognising the two
common text forms turns those models from useless into usable, so the parsing
is a feature rather than a workaround.

EVERY PUBLIC FUNCTION IS TOTAL
------------------------------
Nothing here raises. A refused key, a dead endpoint, a body in an unexpected
shape and a malformed argument blob are all outcomes reported in the returned
dictionary, because the caller's job is to keep a conversation going rather
than to handle exceptions.
"""

import json
import re

from .llm_client import (_build_headers, _chat_timeout, _chat_url,
                         _classify_error, _http_json, resolve_api_key,
                         strip_reasoning)

# Providers that speak the OpenAI tools protocol. Ollama is included because
# it has served ``tools`` since 0.4; an older daemon simply ignores the
# parameter and answers in prose, which the text-form parser below then
# recovers. Anything not listed here is reported as unsupported rather than
# optimistically tried, so a future provider cannot fail silently.
_OPENAI_STYLE = ('openrouter', 'nvidia')
_TOOL_CAPABLE = _OPENAI_STYLE + ('ollama',)

# Nothing in a tool result needs to be longer than this. A listing tool that
# returns eighty rows would otherwise push the system prompt out of the
# context window on the next round.
MAX_TOOL_RESULT_CHARS = 4000


# ===========================================================================
# Capability reporting
# ===========================================================================

def supports_tools(provider):
    """Whether this provider can be sent a ``tools`` parameter.

    Reported per ``provider_key`` and honestly: the caller uses this to decide
    whether to advertise tools at all, and an over-optimistic answer here
    becomes a 400 from the provider halfway through a conversation.
    """
    return getattr(provider, 'provider_key', '') in _TOOL_CAPABLE


def _is_ollama(provider):
    return getattr(provider, 'provider_key', '') == 'ollama'


# ===========================================================================
# Message shapes
# ===========================================================================

def tool_result_message(call_id, name, content, provider=None):
    """One ``role='tool'`` message carrying what a tool returned.

    ``provider`` is optional so a caller that only ever talks to one provider
    can leave it out, but passing it matters: an OpenAI-style provider matches
    the result to the request by ``tool_call_id`` and rejects a result whose id
    it does not recognise, whereas Ollama matches by tool name and has no id at
    all. Sending the wrong one produces a 400 on the round after the tool ran,
    which is the most confusing possible moment for it to happen.
    """
    text = str(content if content is not None else '')
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '\n... (result truncated)'

    if provider is not None and _is_ollama(provider):
        return {'role': 'tool', 'tool_name': name, 'content': text}

    return {'role': 'tool', 'tool_call_id': call_id or name,
            'name': name, 'content': text}


def assistant_tool_call_message(raw_message, tool_calls):
    """The assistant turn that asked for the calls, ready to replay.

    Replayed verbatim when the provider gave us one, because a provider is
    entitled to require its own message back unchanged -- some validate that
    the ids in the following tool messages match ids they issued themselves.
    Only two things are normalised: a null ``content``, which several providers
    return and at least one then refuses to accept, and the reasoning field
    some models attach, which is large, private working and must not be fed
    back into the transcript.

    Reconstructed when there is no usable original, which is the case for every
    call recovered by ``extract_text_tool_calls``: the model wrote the call as
    prose, so a protocol-shaped assistant turn has to be built for it. Doing
    that keeps the transcript honest -- the model sees its own request followed
    by the result, exactly as if it had used the protocol properly.
    """
    if isinstance(raw_message, dict) and raw_message.get('tool_calls'):
        replay = {key: value for key, value in raw_message.items()
                  if key not in ('reasoning', 'reasoning_content', 'refusal',
                                 'annotations', 'audio')}
        replay['role'] = 'assistant'
        replay['content'] = replay.get('content') or ''
        return replay

    content = ''
    if isinstance(raw_message, dict):
        content = raw_message.get('content') or ''
    elif isinstance(raw_message, str):
        content = raw_message

    return {
        'role': 'assistant',
        'content': content,
        'tool_calls': [
            {
                'id': call.get('id') or f"call_{index + 1}",
                'type': 'function',
                'function': {
                    'name': call.get('name', ''),
                    'arguments': json.dumps(call.get('arguments') or {}),
                },
            }
            for index, call in enumerate(tool_calls or [])
        ],
    }


def _prepare_messages(provider, messages):
    """Copy the transcript into the shape this provider accepts.

    A shallow copy per message, never a mutation: the caller keeps its own list
    across rounds and a rewritten message would corrupt the next request.
    """
    ollama = _is_ollama(provider)
    prepared = []

    for message in messages or []:
        if not isinstance(message, dict) or not message.get('role'):
            continue
        entry = dict(message)
        entry['content'] = entry.get('content') or ''
        if ollama:
            # Ollama has no notion of a call id and ignores unknown keys, but
            # dropping them keeps the request minimal and readable in a log.
            entry.pop('tool_call_id', None)
        else:
            entry.pop('tool_name', None)
        prepared.append(entry)

    return prepared


# ===========================================================================
# Reading tool calls out of a response
# ===========================================================================

def parse_tool_calls(data, provider_key):
    """The tool calls in one response body, in one shape.

    Returns a list of ``{'id', 'name', 'arguments'}`` where ``arguments`` is
    always a dictionary. OpenAI-style providers send it as a JSON string and
    Ollama sends it as an object, so one of the two is decoded here.

    A blob that will not parse is NOT dropped. It comes back as
    ``{'_raw': '<the string>'}`` so the loop can answer the model with "your
    arguments were not valid JSON, send them again" -- which it can act on.
    Dropping the call instead would leave the model waiting for a result that
    never arrives, and it would then either repeat itself or invent an outcome.
    """
    message = _response_message(data, provider_key)
    raw_calls = (message or {}).get('tool_calls') or []
    if not isinstance(raw_calls, list):
        return []

    calls = []
    for index, entry in enumerate(raw_calls):
        if not isinstance(entry, dict):
            continue
        function = entry.get('function') if isinstance(entry.get('function'), dict) else entry
        name = function.get('name') or entry.get('name') or ''
        if not name:
            continue
        calls.append({
            'id': entry.get('id') or function.get('id') or f"call_{index + 1}",
            'name': name,
            'arguments': _coerce_arguments(function.get('arguments',
                                                         function.get('parameters'))),
        })
    return calls


def _coerce_arguments(value):
    """Whatever the provider sent for the arguments, as a dictionary."""
    if isinstance(value, dict):
        return value
    if value in (None, ''):
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {'_raw': value[:2000]}
        return parsed if isinstance(parsed, dict) else {'value': parsed}
    return {'value': value}


def _response_message(data, provider_key):
    """The assistant message from either response shape."""
    if not isinstance(data, dict):
        return {}
    if provider_key == 'ollama':
        message = data.get('message')
        return message if isinstance(message, dict) else {}
    choices = data.get('choices') or []
    if not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get('message')
    return message if isinstance(message, dict) else {}


# ===========================================================================
# Tool calls written as text by a model that ignored the protocol
# ===========================================================================

# An explicit tag. Several open-weight families use one of these, and the tag
# itself is unambiguous, so any name inside it is accepted.
_TAGGED_CALL_RE = re.compile(
    r'<\s*(tool_call|toolcall|function_call|tool)\s*>(.*?)<\s*/\s*\1\s*>',
    re.DOTALL | re.IGNORECASE)

# The same tag left unterminated, which happens when the model runs out of
# tokens or simply forgets the closing tag.
_OPEN_CALL_RE = re.compile(
    r'<\s*(?:tool_call|toolcall|function_call)\s*>(.*)$',
    re.DOTALL | re.IGNORECASE)

# A fenced block. Far more likely than a tag to be an innocent piece of JSON
# in an answer, so the name inside has to look like a registry name before it
# is treated as a call. See _looks_like_tool_name.
_FENCED_JSON_RE = re.compile(
    r'```(?:json|tool|tool_code|python)?\s*(\{.*?\}|\[.*?\])\s*```',
    re.DOTALL | re.IGNORECASE)

# A registry name is dotted ('hr.list_candidates') or, once advertised to the
# model, double-underscored ('hr__list_candidates'). Requiring one of those
# separators is what keeps an ordinary JSON example in a developer's answer
# from being mistaken for an instruction to act.
_TOOL_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*(?:\.|__)[a-z0-9_.]+$', re.IGNORECASE)

_NAME_KEYS = ('name', 'tool', 'tool_name', 'function', 'action', 'recipient_name')
_ARGUMENT_KEYS = ('arguments', 'parameters', 'args', 'input', 'tool_input')


def extract_text_tool_calls(text):
    """Recover tool calls a model wrote into its prose.

    Returns ``(visible_text, calls)``. The recognised blocks are removed from
    the visible text, because a reader must never see the machinery, and the
    calls come back in the same shape ``parse_tool_calls`` produces.

    Two forms are recognised. A tagged form is trusted outright: nothing but a
    model attempting a tool call ever writes ``<tool_call>``. A fenced JSON
    block is only trusted when the name inside looks like a registry name,
    since the Software Development employee legitimately answers with JSON and
    must not have its example executed.
    """
    original = text or ''
    if not original.strip():
        return original, []

    calls = []
    remaining = original

    def _harvest(blob, trusted):
        for name, arguments in _calls_in_blob(blob, trusted):
            calls.append({
                'id': f"text_call_{len(calls) + 1}",
                'name': name,
                'arguments': arguments,
            })

    for match in list(_TAGGED_CALL_RE.finditer(remaining)):
        _harvest(match.group(2), trusted=True)
    remaining = _TAGGED_CALL_RE.sub('', remaining)

    open_tag = _OPEN_CALL_RE.search(remaining)
    if open_tag:
        before = len(calls)
        _harvest(open_tag.group(1), trusted=True)
        if len(calls) > before:
            remaining = remaining[:open_tag.start()]

    for match in list(_FENCED_JSON_RE.finditer(remaining)):
        before = len(calls)
        _harvest(match.group(1), trusted=False)
        if len(calls) > before:
            remaining = remaining.replace(match.group(0), '')

    if not calls:
        # A reply that is nothing but one JSON object, which is what a model
        # does when it has been told to answer in JSON and has no tool support.
        stripped = remaining.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            before = len(calls)
            _harvest(stripped, trusted=False)
            if len(calls) > before:
                remaining = ''

    return _tidy(remaining), calls


def _calls_in_blob(blob, trusted):
    """(name, arguments) pairs found in one candidate JSON blob."""
    for candidate in _json_objects(blob):
        entries = candidate if isinstance(candidate, list) else [candidate]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = _name_in(entry)
            if not name:
                continue
            if not trusted and not _looks_like_tool_name(name):
                continue
            yield name, _arguments_in(entry)


def _json_objects(blob):
    """Parse a blob that is probably JSON, tolerating what surrounds it."""
    text = (blob or '').strip()
    if not text:
        return []

    for candidate in (text, _first_braced(text)):
        if not candidate:
            continue
        try:
            return [json.loads(candidate)]
        except (json.JSONDecodeError, ValueError):
            continue
    return []


def _first_braced(text):
    """The first balanced ``{...}`` span, for a blob wrapped in commentary."""
    start = text.find('{')
    if start < 0:
        return ''
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ''


def _name_in(entry):
    for key in _NAME_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict) and isinstance(value.get('name'), str):
            return value['name'].strip()
    return ''


def _arguments_in(entry):
    for key in _ARGUMENT_KEYS:
        value = entry.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            return _coerce_arguments(value)
    nested = entry.get('function')
    if isinstance(nested, dict):
        return _arguments_in(nested)
    # Everything that is not a name key is presumed to be an argument, which
    # is how a model writes a flat call: {"tool": "x", "query": "y"}.
    leftovers = {key: value for key, value in entry.items()
                 if key not in _NAME_KEYS and key not in _ARGUMENT_KEYS}
    return leftovers


def _looks_like_tool_name(name):
    return bool(_TOOL_NAME_RE.match(name or ''))


_BLANK_RUN_RE = re.compile(r'\n{3,}')


def _tidy(text):
    return _BLANK_RUN_RE.sub('\n\n', (text or '').strip())


# ===========================================================================
# The call itself
# ===========================================================================

def _testing():
    """Whether the test suite is running, so no provider may be reached.

    Read lazily rather than captured at import time: this module is imported
    while the app registry is loading, and reading a setting then is a way to
    get a half-configured answer.
    """
    from django.conf import settings
    return bool(getattr(settings, 'TESTING', False))


def chat_with_tools(provider, model_id, messages, tools=None, temperature=0.7,
                    max_tokens=1200, timeout=None):
    """One generation round, with tools offered and tool calls read back.

    ``messages`` is the entire transcript, system message first, including any
    ``role='tool'`` messages from earlier rounds. Nothing is prepended and
    nothing is remembered between calls.

    Returns a dictionary in every case:

        ok             a usable answer came back (text, tool calls, or both)
        text           what a person should see, reasoning already stripped
        tool_calls     [{'id', 'name', 'arguments'}], arguments always a dict
        finish_reason  the provider's own word for why it stopped
        tokens         total tokens if the provider reported any, else 0
        model          the model asked
        source         'live' when the provider answered, 'fallback' when not
        message        a sentence a person could be shown
        raw_message    the assistant message as received, for exact replay

    A refusal, a timeout, an empty completion and a body in an unexpected
    shape all return ``ok=False`` with a readable ``message``, because the
    conversation loop above has a fallback path and needs to be told to take
    it rather than to handle an exception.
    """
    provider_key = getattr(provider, 'provider_key', '')
    failure = {'ok': False, 'text': '', 'tool_calls': [], 'finish_reason': 'error',
               'tokens': 0, 'model': model_id, 'source': 'fallback',
               'message': '', 'raw_message': {}}

    if provider is None:
        failure['message'] = 'No language-model provider was supplied.'
        return failure

    # THE TEST SUITE NEVER REACHES A PROVIDER.
    #
    # This mirrors what marketing/integrations/base.py does for connectors, and
    # for the same reason. A developer machine with a real key in .env would
    # otherwise make a genuine, billed, non-deterministic network call on every
    # chat turn a test happens to drive, so the suite's runtime and its
    # outcomes would depend on whose machine ran it.
    #
    # Refusing here rather than in the caller is deliberate: this is the single
    # function through which both the conversation loop and the orchestrator's
    # tie-break reach a model, so one guard covers both and cannot be
    # forgotten by a third caller added later. The loop reads ok=False and
    # takes its fallback path, which runs a real tool and answers from the
    # result -- so a test still exercises the employee doing work.
    #
    # A test that wants to exercise the live loop should patch this function.
    if _testing():
        failure['message'] = ('No language model is reached while the test suite is '
                              'running, so this employee answered by running its '
                              'own tools.')
        return failure

    if getattr(provider, 'requires_api_key', False) and not resolve_api_key(provider):
        failure['message'] = (
            f'No API key is configured for {getattr(provider, "label", provider_key)}.')
        return failure

    prepared = _prepare_messages(provider, messages)
    if not prepared:
        failure['message'] = 'There was nothing to send to the provider.'
        return failure

    offer_tools = bool(tools) and supports_tools(provider)

    if provider_key == 'ollama':
        payload = {
            'model': model_id,
            'messages': prepared,
            'stream': False,
            'options': {'temperature': float(temperature),
                        'num_predict': int(max_tokens)},
        }
        if offer_tools:
            payload['tools'] = list(tools)
    else:
        payload = {
            'model': model_id,
            'messages': prepared,
            'temperature': float(temperature),
            'max_tokens': int(max_tokens),
        }
        if offer_tools:
            payload['tools'] = list(tools)
            payload['tool_choice'] = 'auto'

    try:
        _status, data = _http_json(
            _chat_url(provider), method='POST', headers=_build_headers(provider),
            payload=payload, timeout=timeout or _chat_timeout())
    except Exception as exc:  # noqa: BLE001 -- deliberately total
        _connection_status, message = _classify_error(exc, provider_key)
        failure['message'] = message
        return failure

    try:
        message = _response_message(data, provider_key)
        raw_text = message.get('content') or ''
        tool_calls = parse_tool_calls(data, provider_key)
        finish_reason = _finish_reason(data, provider_key)
        tokens = _tokens(data, provider_key)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        failure['message'] = 'The provider returned a response in an unexpected shape.'
        return failure

    # Only look for text-form calls when the protocol produced none. A model
    # that used the protocol properly and also happened to quote JSON must not
    # have the quotation executed as well.
    if not tool_calls:
        raw_text, text_calls = extract_text_tool_calls(raw_text)
        if text_calls:
            tool_calls = text_calls
            finish_reason = 'tool_calls_in_text'

    text = strip_reasoning(raw_text) if raw_text else ''

    if not text and not tool_calls:
        failure['message'] = 'The provider returned an empty completion.'
        failure['finish_reason'] = finish_reason or 'empty'
        failure['tokens'] = tokens
        return failure

    if tool_calls:
        summary = (f'{len(tool_calls)} tool call'
                   f'{"s" if len(tool_calls) != 1 else ""} requested by {model_id}.')
    else:
        summary = f'Generated {len(text)} characters using {model_id}.'

    return {
        'ok': True,
        'text': text,
        'tool_calls': tool_calls,
        'finish_reason': finish_reason or 'stop',
        'tokens': tokens,
        'model': model_id,
        'source': 'live',
        'message': summary,
        'raw_message': message if isinstance(message, dict) else {},
    }


def _finish_reason(data, provider_key):
    if provider_key == 'ollama':
        return str(data.get('done_reason') or '')
    choices = data.get('choices') or []
    if choices and isinstance(choices[0], dict):
        return str(choices[0].get('finish_reason') or '')
    return ''


def _tokens(data, provider_key):
    """Total tokens, as far as this provider reports them.

    Ollama reports the two halves separately rather than a total, so they are
    added; a provider that reports nothing contributes zero rather than an
    invented figure, because the task record is read as fact.
    """
    try:
        if provider_key == 'ollama':
            return int(data.get('eval_count') or 0) + int(data.get('prompt_eval_count') or 0)
        return int((data.get('usage') or {}).get('total_tokens') or 0)
    except (TypeError, ValueError):
        return 0
