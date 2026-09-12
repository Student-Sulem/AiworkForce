"""Live language-model provider client built on the Python standard library.

WHY urllib AND NOT requests
---------------------------
The project's virtual environment contains Django and nothing else. Using
`urllib.request` keeps it that way, so the application runs anywhere Python and
Django are installed with no `pip install` step.

SUPPORTED PROVIDERS
-------------------
    OpenRouter   GET  {base}/models              POST {base}/chat/completions
    NVIDIA NIM   GET  {base}/models              POST {base}/chat/completions
    Ollama       GET  {base}/api/tags            POST {base}/api/chat

OpenRouter and NVIDIA authenticate with `Authorization: Bearer <key>`. Ollama
runs on the local machine and needs no credential.

EVERY PUBLIC FUNCTION IS TOTAL
------------------------------
No function in this module raises into a view. Each one returns a result
dictionary describing either success or the failure, and `fetch_models` always
returns a non-empty list by falling back to FALLBACK_CATALOG. That property is
what allows the application to be demonstrated with no internet connection.

API KEY STORAGE
---------------
Keys are held in LLMProvider.api_key as plain text. This is an academic build
running with DEBUG=True against a local SQLite file. Encrypting the column with
a key stored in the same settings.py would provide obfuscation, not security,
and would make the code harder to explain. Production options, in ascending
order of rigour, are: (1) read keys from environment variables at start-up,
(2) use a secrets manager such as AWS KMS or HashiCorp Vault, (3) never store
user keys server-side at all. The trade-off accepted here is convenience and
explainability over confidentiality at rest.

Keys are never rendered back to the browser in full (see
LLMProvider.masked_key), never written to a log, and never placed in a URL
query string.
"""

import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request

from django.conf import settings

# Two separate budgets. Listing models or probing a connection is a cheap
# metadata call and should fail fast. Generating a reply is not: a serverless
# endpoint that has scaled to zero can take 15-20 seconds to answer the first
# request, so a short timeout there would silently fall back to templates and
# make a perfectly good API key look broken.
DEFAULT_TIMEOUT = 8
DEFAULT_CHAT_TIMEOUT = 120  # see the note above: a research turn that has
                            # read several documents routinely needs more
                            # than a minute, and timing out discards work
                            # that was almost complete.
USER_AGENT = 'AIWorkforce/1.0 (Django coursework project)'

# OpenRouter asks callers to identify themselves. Both headers are optional but
# reduce the chance of a 4xx response.
OPENROUTER_REFERER = 'http://localhost:8000'
OPENROUTER_TITLE = 'AI Workforce'

# Environment variables consulted when a provider has no key stored.
ENV_KEY_NAMES = {
    'openrouter': 'OPENROUTER_API_KEY',
    'nvidia': 'NVIDIA_API_KEY',
}


class LLMClientError(Exception):
    """Raised internally. Always caught before it can reach a view."""


# ---------------------------------------------------------------------------
# Curated fallback catalogue
# ---------------------------------------------------------------------------
# Used whenever a live model list cannot be fetched, and to seed the database
# so the model dropdown is never empty on a fresh install.

FALLBACK_CATALOG = {
    'openrouter': [
        {'model_id': 'openai/gpt-4o-mini', 'display_name': 'GPT-4o mini',
         'context_length': 128000, 'description': 'Fast and inexpensive general-purpose model.'},
        {'model_id': 'openai/gpt-4o', 'display_name': 'GPT-4o',
         'context_length': 128000, 'description': 'High-capability multimodal model.'},
        {'model_id': 'anthropic/claude-3.5-sonnet', 'display_name': 'Claude 3.5 Sonnet',
         'context_length': 200000, 'description': 'Strong long-form writing and reasoning.'},
        {'model_id': 'anthropic/claude-3.5-haiku', 'display_name': 'Claude 3.5 Haiku',
         'context_length': 200000, 'description': 'Low-latency model for short tasks.'},
        {'model_id': 'google/gemini-flash-1.5', 'display_name': 'Gemini 1.5 Flash',
         'context_length': 1000000, 'description': 'Very large context window.'},
        {'model_id': 'meta-llama/llama-3.1-70b-instruct', 'display_name': 'Llama 3.1 70B Instruct',
         'context_length': 131072, 'description': 'Open-weight instruction-tuned model.'},
        {'model_id': 'mistralai/mistral-large', 'display_name': 'Mistral Large',
         'context_length': 128000, 'description': 'European flagship model.'},
        {'model_id': 'deepseek/deepseek-chat', 'display_name': 'DeepSeek Chat',
         'context_length': 64000, 'description': 'Cost-effective conversational model.'},
    ],
    'nvidia': [
        # Verified against a live NVIDIA NIM account. The /models endpoint lists
        # the entire catalogue, but only a subset is provisioned per account, so
        # these are the ones that actually answer. Use "Refresh models" on the
        # Configurations page to pull the full live list for your own key.
        {'model_id': 'nvidia/nemotron-3-super-120b-a12b',
         'display_name': 'Nemotron 3 Super 120B',
         'context_length': 131072,
         'description': 'Fast and follows a system prompt closely. Recommended.'},
        {'model_id': 'meta/llama-3.2-11b-vision-instruct',
         'display_name': 'Llama 3.2 11B Vision Instruct',
         'context_length': 131072,
         'description': 'Low latency, clean instruction following.'},
        {'model_id': 'nvidia/nemotron-3.5-lightning-30b-a3b',
         'display_name': 'Nemotron 3.5 Lightning 30B',
         'context_length': 131072,
         'description': 'Reasoning model; its visible working is stripped automatically.'},
        {'model_id': 'nvidia/nemotron-3-ultra-550b-a55b',
         'display_name': 'Nemotron 3 Ultra 550B',
         'context_length': 131072, 'description': 'Largest Nemotron variant.'},
        {'model_id': 'mistralai/mistral-large',
         'display_name': 'Mistral Large',
         'context_length': 128000, 'description': 'European flagship model.'},
        {'model_id': 'nvidia/nemotron-4-340b-instruct',
         'display_name': 'Nemotron 4 340B Instruct',
         'context_length': 4096, 'description': 'Instruction-tuned NVIDIA model.'},
    ],
    'ollama': [
        {'model_id': 'llama3.2', 'display_name': 'Llama 3.2',
         'context_length': 131072, 'description': 'Runs locally through Ollama.'},
        {'model_id': 'llama3.1:8b', 'display_name': 'Llama 3.1 8B',
         'context_length': 131072, 'description': 'Local 8-billion-parameter model.'},
        {'model_id': 'mistral', 'display_name': 'Mistral 7B',
         'context_length': 32768, 'description': 'Compact local model.'},
        {'model_id': 'phi3', 'display_name': 'Phi-3',
         'context_length': 4096, 'description': 'Small local model from Microsoft.'},
        {'model_id': 'qwen2.5', 'display_name': 'Qwen 2.5',
         'context_length': 32768, 'description': 'Multilingual local model.'},
        {'model_id': 'gemma2', 'display_name': 'Gemma 2',
         'context_length': 8192, 'description': 'Local Gemma model.'},
        {'model_id': 'deepseek-coder-v2', 'display_name': 'DeepSeek Coder V2',
         'context_length': 128000, 'description': 'Code-focused local model.'},
    ],
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _timeout():
    """Budget for metadata calls: connection tests and model listings."""
    return getattr(settings, 'LLM_HTTP_TIMEOUT', DEFAULT_TIMEOUT)


def _chat_timeout():
    """Budget for generation, which is far slower than a metadata call."""
    return getattr(settings, 'LLM_CHAT_TIMEOUT', DEFAULT_CHAT_TIMEOUT)


def resolve_api_key(provider):
    """The key to use for this provider, database first, environment second.

    Reading from the environment is the approach this module's docstring
    recommends, and it means a real credential never has to be typed into a
    form, written to db.sqlite3, or committed to version control. Set it as:

        OPENROUTER_API_KEY   for OpenRouter
        NVIDIA_API_KEY       for NVIDIA NIM

    A key saved through the Configurations page still wins, so the interface
    keeps working for anyone who prefers it.
    """
    if provider.api_key:
        return provider.api_key
    return os.environ.get(ENV_KEY_NAMES.get(provider.provider_key, ''), '')


def _build_headers(provider):
    """Authorization and identification headers for one provider."""
    headers = {}
    key = resolve_api_key(provider)
    if provider.requires_api_key and key:
        headers['Authorization'] = f"Bearer {key}"
    if provider.provider_key == 'openrouter':
        headers['HTTP-Referer'] = OPENROUTER_REFERER
        headers['X-Title'] = OPENROUTER_TITLE
    return headers


def _http_json(url, *, method='GET', headers=None, payload=None, timeout=None):
    """The single network primitive. Returns (status_code, parsed_body)."""
    timeout = timeout or _timeout()
    body = json.dumps(payload).encode('utf-8') if payload is not None else None

    request = urllib.request.Request(url, data=body, method=method)
    request.add_header('User-Agent', USER_AGENT)
    request.add_header('Accept', 'application/json')
    if body is not None:
        request.add_header('Content-Type', 'application/json')
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode('utf-8', errors='replace')
        return response.status, (json.loads(raw) if raw.strip() else {})


def _classify_error(exc, provider_key='', timeout=None):
    """Turn any exception into (connection_status, human-readable message).

    HTTPError is checked before URLError because it is a subclass of it; the
    other order would swallow every status code into the generic branch.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return 'unauthorized', f'The provider rejected the API key (HTTP {exc.code}).'
        if exc.code == 429:
            return 'error', 'Rate limited by the provider. Please try again shortly.'
        if exc.code == 404:
            # NVIDIA NIM lists its whole catalogue from /models but only serves
            # the models provisioned for the account, so a 404 here almost
            # always means the model, not a mistyped base URL.
            return 'error', ('Not found (HTTP 404). That model is probably not '
                             'available to your account; try another from the list.')
        if exc.code == 410:
            return 'error', 'That model has been retired by the provider.'
        return 'error', f'The provider returned HTTP {exc.code}.'

    if isinstance(exc, ssl.SSLError):
        return 'error', 'TLS verification failed. Check the network or proxy settings.'

    if isinstance(exc, (socket.timeout, TimeoutError)):
        # The caller passes the budget it actually used. Without that this
        # reported the metadata timeout for every failure, so a generation call
        # that waited a full minute said "no response within 8 seconds" -- which
        # sends whoever is diagnosing it looking for a network fault that is not
        # there.
        return 'error', f'No response within {timeout or _timeout()} seconds.'

    if isinstance(exc, urllib.error.URLError):
        message = f'Could not reach the provider: {exc.reason}.'
        if provider_key == 'ollama':
            message += ' Is "ollama serve" running on port 11434?'
        return 'error', message

    if isinstance(exc, json.JSONDecodeError):
        return 'error', 'The provider returned a response that was not valid JSON.'

    return 'error', f'Unexpected error: {exc.__class__.__name__}.'


# ---------------------------------------------------------------------------
# Per-provider response parsers
# ---------------------------------------------------------------------------

def _parse_openai_style(data):
    """OpenRouter and NVIDIA both return {"data": [{"id": ..., ...}]}."""
    entries = []
    for item in (data or {}).get('data', []):
        model_id = item.get('id')
        if not model_id:
            continue
        entries.append({
            'model_id': model_id,
            'display_name': item.get('name') or model_id,
            'context_length': item.get('context_length') or None,
            'description': (item.get('description') or '')[:300],
        })
    return entries


def _parse_ollama(data):
    """Ollama returns {"models": [{"name": "llama3.2:latest", ...}]}."""
    entries = []
    for item in (data or {}).get('models', []):
        model_id = item.get('name') or item.get('model')
        if not model_id:
            continue
        details = item.get('details') or {}
        parameter_size = details.get('parameter_size', '')
        entries.append({
            'model_id': model_id,
            'display_name': model_id.split(':')[0],
            'context_length': None,
            'description': f"Local model. {parameter_size}".strip()[:300],
        })
    return entries


_MODEL_PARSERS = {
    'openrouter': _parse_openai_style,
    'nvidia': _parse_openai_style,
    'ollama': _parse_ollama,
}


def _models_url(provider):
    base = provider.effective_base_url
    if provider.provider_key == 'ollama':
        return f"{base}/api/tags"
    return f"{base}/models"


def _chat_url(provider):
    base = provider.effective_base_url
    if provider.provider_key == 'ollama':
        return f"{base}/api/chat"
    return f"{base}/chat/completions"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def test_connection(provider):
    """Probe the provider's model endpoint and report what happened."""
    if not provider.is_enabled:
        return {'ok': False, 'connection_status': 'error',
                'message': 'This provider is disabled. Enable it before testing.',
                'latency_ms': 0}

    if provider.requires_api_key and not resolve_api_key(provider):
        return {'ok': False, 'connection_status': 'unauthorized',
                'message': f'No API key is configured for {provider.label}.',
                'latency_ms': 0}

    started = time.monotonic()
    try:
        status, data = _http_json(_models_url(provider), headers=_build_headers(provider))
        latency = int((time.monotonic() - started) * 1000)
        count = len(_MODEL_PARSERS[provider.provider_key](data))
        return {
            'ok': True,
            'connection_status': 'online',
            'message': f'Connected successfully. {count} models are available.',
            'latency_ms': latency,
        }
    except Exception as exc:  # noqa: BLE001 -- deliberately total
        latency = int((time.monotonic() - started) * 1000)
        connection_status, message = _classify_error(exc, provider.provider_key)
        return {'ok': False, 'connection_status': connection_status,
                'message': message, 'latency_ms': latency}


def fetch_models(provider):
    """Return the provider's model list, never empty.

    On any failure the curated FALLBACK_CATALOG is returned instead, so the
    dropdown on the Configurations page always has something in it.
    """
    fallback = [dict(entry) for entry in FALLBACK_CATALOG.get(provider.provider_key, [])]

    if provider.requires_api_key and not resolve_api_key(provider):
        return {'ok': False, 'source': 'fallback',
                'message': f'No API key configured. Showing {len(fallback)} curated models.',
                'models': fallback}

    try:
        status, data = _http_json(_models_url(provider), headers=_build_headers(provider))
        parsed = _MODEL_PARSERS[provider.provider_key](data)
        if not parsed:
            raise LLMClientError('The provider returned an empty model list.')
        return {'ok': True, 'source': 'live',
                'message': f'Fetched {len(parsed)} models from {provider.label}.',
                'models': parsed}
    except Exception as exc:  # noqa: BLE001 -- deliberately total
        _status, message = _classify_error(exc, provider.provider_key)
        return {'ok': False, 'source': 'fallback',
                'message': f'{message} Loaded {len(fallback)} curated models instead.',
                'models': fallback}


def sync_models_for_provider(provider):
    """Persist the provider's model list into the LLMModel table."""
    from django.utils import timezone

    from .models import LLMModel

    result = fetch_models(provider)
    now = timezone.now()
    created = updated = 0

    for entry in result['models']:
        _obj, was_created = LLMModel.objects.update_or_create(
            provider=provider,
            model_id=entry['model_id'],
            defaults={
                'display_name': entry.get('display_name') or entry['model_id'],
                'context_length': entry.get('context_length'),
                'description': entry.get('description', ''),
                'source': result['source'],
                'fetched_at': now,
                'is_enabled': True,
            },
        )
        created += 1 if was_created else 0
        updated += 0 if was_created else 1

    provider.models_synced_at = now
    provider.save(update_fields=['models_synced_at'])

    return {'created': created, 'updated': updated,
            'source': result['source'], 'message': result['message'],
            'total': created + updated}


def chat_completion(provider, model_id, system_prompt, user_prompt,
                    temperature=0.7, max_tokens=800):
    """Single-turn convenience wrapper around chat_conversation()."""
    return chat_conversation(
        provider, model_id, system_prompt,
        [{'role': 'user', 'content': user_prompt}],
        temperature=temperature, max_tokens=max_tokens)


# Some reasoning models show their working before the answer. Two forms are
# seen in practice: a <think> block, and a prose heading such as "Here's a
# thinking process:". Neither belongs in a social post, so both are removed
# before the reply is stored.
_THINK_TAG_RE = re.compile(r'<think>.*?</think>\s*', re.DOTALL | re.IGNORECASE)

_THINK_HEADING_RE = re.compile(
    r"^\s*(?:here(?:'s| is) (?:a |my )?(?:thinking|thought) process"
    r"|let me think(?: through this)?"
    r"|reasoning|thinking)\s*:",
    re.IGNORECASE)

_BLANK_LINE_RE = re.compile(r'\n\s*\n')

# A third form, and the one seen most often in practice: no marker at all, just
# the model narrating its own deliberation. Two giveaways, neither of which a
# reply addressed TO someone ever contains -- talking about "the user" in the
# third person, and reasoning aloud about its own instructions.
#
# The verbs matter. Bare "the user" is left alone deliberately, because a
# strategy employee may quite properly write about user acquisition or the user
# journey; "the user is asking" is not something it would ever write.
_MONOLOGUE_RE = re.compile(
    # Talking about the person it is talking to, in the third person.
    r"\bthe user (?:is|was|says|said|asked|asks|wants|meant|means|might"
    r"|seems|expects|didn't|did not|probably)\b"
    # Reasoning aloud about its own instructions.
    r"|\bmy (?:role|instructions|brief|constraints|capabilities) (?:as|is|are|say)\b"
    r"|\bper (?:my|the) (?:role|rules|brief|instructions)\b"
    r"|\bthe (?:rules|brief|instructions) (?:say|says|forbid|state)\b"
    r"|\bre-?reading the (?:constraints|rules|brief)\b"
    # Planning its own next move rather than making it. A finished email or
    # social post never narrates what it is about to do.
    r"|\bso I(?:'ll| will| should)\b"
    # The first-person-plural planning voice. Seen in practice from the
    # Nemotron reasoning models, which open with a whole paragraph of
    # "We need to write a short LinkedIn post about..." before producing the
    # post. Deliberately narrow: it matches the verbs of composing something,
    # not "we need to" in general, because a genuine piece of marketing copy
    # may well contain that phrase in its body.
    r"|\bwe (?:need to|must|should) (?:write|produce|generate|create|output"
    r"|draft|compose|answer|reply|respond|call|use)\b"
    r"|\bI need to (?:write|produce|generate|create|output|draft|compose)\b"
    r"|\bmy planned (?:response|reply|answer)\b"
    r"|\b(?:best|safer) (?:path|approach|option)\s*:"
    r"|^\s*(?:alternative|solution|wait|hmm|ah)\b\s*[:!,.]"
    r"|\bdouble-?checking\b"
    # "Let me parse the 5 documents:" -- the model narrating its own next
    # step before taking it. Restricted to verbs of working rather than "let
    # me know", which is ordinary polite prose and must survive.
    r"|\blet me (?:unpack|re-?read|parse|read|check|look|see|go through"
    r"|work through|start by|think|analyse|analyze|summarise|summarize)\b"
    # NOTE: there was a rule here matching a line beginning "Step 1", "First,"
    # or "Now I". It was removed after it stripped a perfectly good answer --
    # "Step 1 of onboarding is the contract, which HR sends on day minus
    # three" -- down to nothing. An onboarding checklist, a runbook and a set
    # of reproduction steps all legitimately open that way, so the rule
    # destroyed more real answers than it caught monologues. A pattern here
    # has to be specific to the act of narrating, not to the shape of a list.
    r"|\bbefore I (?:answer|write|reply|respond)\b",
    re.IGNORECASE | re.MULTILINE)

# Below this, a trailing block is more likely to be a stray closing remark than
# the actual answer, so the earlier split is preferred instead.
_MIN_ANSWER_CHARS = 40

# What is left after the working must also be a fair share of the whole reply.
# A tail worth 4% of the output is where a model ran out of tokens mid-thought,
# not where it answered.
_MIN_ANSWER_SHARE = 0.15


def strip_reasoning(text):
    """Remove a reasoning model's visible working from its answer.

    Three forms are handled, in decreasing order of certainty:

        <think>...</think>          an unambiguous marker; always cut
        "Here's my thinking:"       the reply announces its own reasoning
        "Okay, the user wants..."   no marker at all, just deliberation

    Returning '' is a valid and deliberate outcome: it means the model produced
    working and no answer. chat_completion() treats that as a failed call, so
    the employee replies from its template and the chat is labelled "Template
    reply". Publishing the reasoning, or an empty bubble, would both be worse.
    """
    # U+FFFD carries no meaning and only ever appears when something upstream
    # mangled a byte, so it is dropped rather than published.
    cleaned = _THINK_TAG_RE.sub('', text or '').replace('�', '').strip()

    blocks = [block.strip() for block in _BLANK_LINE_RE.split(cleaned) if block.strip()]

    if _THINK_HEADING_RE.match(cleaned):
        if len(blocks) < 2:
            return cleaned
        if len(blocks[-1]) >= _MIN_ANSWER_CHARS:
            return blocks[-1]
        return '\n\n'.join(blocks[1:]).strip()

    return _strip_monologue(blocks, cleaned)


def _strip_monologue(blocks, cleaned):
    """Handle a reply that opens by deliberating instead of answering.

    An answer never begins with the model thinking about the request, so an
    opening block of monologue means everything up to the last such block is
    working, and whatever follows is the candidate answer.

    That candidate has to earn the name twice over. It must be long enough to
    be an answer at all, and it must be a fair share of what the model wrote:
    a model that spends 95% of its budget deliberating and stops mid-sentence
    has left a fragment, not a reply. The leak this was written for ended with
    154 characters after 3,300 of working -- long enough to pass a length test
    on its own, and still not an answer.

    Returning '' is the honest outcome there, because the caller then falls
    back to the template and the chat says "Template reply".
    """
    if not blocks or not _MONOLOGUE_RE.search(blocks[0]):
        return cleaned

    last_monologue = max(index for index, block in enumerate(blocks)
                         if _MONOLOGUE_RE.search(block))
    answer = blocks[last_monologue + 1:]

    if not answer:
        return ''

    joined = '\n\n'.join(answer).strip()
    if len(joined) < _MIN_ANSWER_CHARS:
        return ''
    if len(joined) < len(cleaned) * _MIN_ANSWER_SHARE:
        return ''
    return joined


def chat_conversation(provider, model_id, system_prompt, history,
                      temperature=0.7, max_tokens=800):
    """Continue a multi-turn conversation. Never raises.

    `history` is a list of {'role': 'user'|'assistant', 'content': str} in
    chronological order, which is the shape all three providers expect. The
    system prompt is prepended here so callers never have to remember to.

    Returns {'ok', 'text', 'source', 'model', 'tokens', 'message'}. A caller
    that gets ok=False should fall back to its own template, which is exactly
    what agent_engine._generate_reply does.
    """
    failure = {'ok': False, 'text': '', 'source': 'fallback',
               'model': model_id, 'tokens': 0, 'message': ''}

    if provider.requires_api_key and not resolve_api_key(provider):
        failure['message'] = f'No API key configured for {provider.label}.'
        return failure

    messages = [{'role': 'system', 'content': system_prompt}]
    messages.extend(
        {'role': turn['role'], 'content': turn['content']}
        for turn in (history or [])
        if turn.get('content')
    )

    if len(messages) == 1:
        failure['message'] = 'There was nothing to send to the provider.'
        return failure

    if provider.provider_key == 'ollama':
        # Ollama streams newline-delimited JSON unless stream is false, and
        # json.loads cannot parse a multi-object body.
        payload = {
            'model': model_id,
            'messages': messages,
            'stream': False,
            'options': {'temperature': float(temperature), 'num_predict': int(max_tokens)},
        }
    else:
        payload = {
            'model': model_id,
            'messages': messages,
            'temperature': float(temperature),
            'max_tokens': int(max_tokens),
        }

    try:
        _status, data = _http_json(_chat_url(provider), method='POST',
                                   headers=_build_headers(provider), payload=payload,
                                   timeout=_chat_timeout())
    except Exception as exc:  # noqa: BLE001 -- deliberately total
        _connection_status, message = _classify_error(exc, provider.provider_key)
        failure['message'] = message
        return failure

    try:
        if provider.provider_key == 'ollama':
            # Ollama's shape is {"message": {"content": ...}}, not choices[].
            text = (data.get('message') or {}).get('content', '')
            tokens = int(data.get('eval_count') or 0)
        else:
            choices = data.get('choices') or []
            text = (choices[0].get('message') or {}).get('content', '') if choices else ''
            tokens = int((data.get('usage') or {}).get('total_tokens') or 0)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        failure['message'] = 'The provider returned a response in an unexpected shape.'
        return failure

    text = strip_reasoning(text)
    if not text:
        failure['message'] = 'The provider returned an empty completion.'
        return failure

    return {'ok': True, 'text': text, 'source': 'live',
            'model': model_id, 'tokens': tokens,
            'message': f'Generated {len(text)} characters using {model_id}.'}
