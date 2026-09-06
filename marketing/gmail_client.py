"""Reading a real mailbox over IMAP, using only the standard library.

WHY THIS EXISTS
---------------
Sending already worked: approving an outreach email hands it to Django's SMTP
backend. But an employee that can only send is a typewriter, not an assistant.
Aria was asked to "fully manage mails", which means she has to be able to look
at the mailbox as well.

WHY NOT A LIBRARY
-----------------
`imaplib` and `email` are both in the standard library and do the whole job.
The project deliberately has no third-party dependencies beyond Django, and
adding one to read a mailbox would not be a good trade.

THE THREE RULES THIS MODULE FOLLOWS
-----------------------------------
1. **Never fetch the whole mailbox.** The account this was built against holds
   10,669 messages. Every function here takes a limit, fetches a bounded slice
   of the most recent UIDs, and asks for headers only unless a body was
   explicitly requested.

2. **Never change what it reads.** The mailbox is opened `readonly=True` and
   bodies are fetched with `BODY.PEEK[]` rather than `BODY[]`. Reading a
   message through Aria does not mark it as read in Gmail, because a tool that
   silently alters your inbox as a side effect of looking at it is a tool you
   stop trusting.

3. **Never raise into a view.** Every public function returns a dictionary with
   an `ok` key. A mailbox that is offline, misconfigured or refusing the
   password produces a readable message, not a 500.

CREDENTIALS
-----------
The same ones SMTP uses -- EMAIL_HOST_USER and EMAIL_HOST_PASSWORD from .env.
For Gmail that is an App Password, and IMAP must be enabled in the account's
settings. Nothing extra needs configuring.
"""

import email
import email.header
import email.utils
import html
import imaplib
import re
import socket
import ssl
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

# Gmail's IMAP endpoint. Overridable for a different provider, and derived from
# EMAIL_HOST when that clearly names one.
DEFAULT_IMAP_HOST = 'imap.gmail.com'
DEFAULT_IMAP_PORT = 993

# A slice this size keeps a listing to one screen and one quick fetch. The
# mailbox behind this was 10,669 messages; an unbounded FETCH would hang the
# request and then run the process out of memory.
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

# Long enough for a slow mailbox, short enough that a wedged connection does
# not hold a worker open indefinitely.
IMAP_TIMEOUT = 20

SNIPPET_CHARS = 160
MAX_BODY_CHARS = 8000

_HTML_TAG_RE = re.compile(r'<[^>]+>')
# Bulk senders pad subjects and bodies with zero-width and soft-hyphen
# characters to defeat spam grouping. They render as blank boxes.
_INVISIBLE_RE = re.compile(r'[­​-‏⁠⁡-⁤﻿]')
_WHITESPACE_RE = re.compile(r'[ \t\r\f\v]+')
_BLANK_RUN_RE = re.compile(r'\n{3,}')

# Folders Gmail names with a bracketed prefix, mapped to the words a person
# would actually type.
FOLDER_ALIASES = {
    'inbox': 'INBOX',
    'sent': '[Gmail]/Sent Mail',
    'drafts': '[Gmail]/Drafts',
    'spam': '[Gmail]/Spam',
    'trash': '[Gmail]/Trash',
    'starred': '[Gmail]/Starred',
    'important': '[Gmail]/Important',
    'all': '[Gmail]/All Mail',
    'archive': '[Gmail]/All Mail',
}


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def imap_host():
    configured = getattr(settings, 'IMAP_HOST', '') or ''
    if configured:
        return configured
    smtp_host = (getattr(settings, 'EMAIL_HOST', '') or '').lower()
    if 'gmail' in smtp_host:
        return DEFAULT_IMAP_HOST
    if smtp_host.startswith('smtp.'):
        return 'imap.' + smtp_host[len('smtp.'):]
    return DEFAULT_IMAP_HOST


def imap_port():
    return int(getattr(settings, 'IMAP_PORT', DEFAULT_IMAP_PORT) or DEFAULT_IMAP_PORT)


def is_configured():
    """Reading needs exactly the credentials sending needs."""
    return bool(settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD)


def account():
    return settings.EMAIL_HOST_USER or ''


def _describe(exc):
    """Turn an exception into something a person can act on."""
    if isinstance(exc, imaplib.IMAP4.error):
        detail = str(exc).strip("b'\" ")
        if 'AUTHENTICATIONFAILED' in detail.upper() or 'Invalid credentials' in detail:
            return ('The mail server rejected the credentials. For Gmail this must be '
                    'an App Password, and IMAP must be switched on in the account.')
        return f'The mail server refused the request: {detail}'
    if isinstance(exc, ssl.SSLError):
        return 'The secure connection to the mail server failed.'
    if isinstance(exc, socket.timeout):
        return f'The mail server did not answer within {IMAP_TIMEOUT} seconds.'
    if isinstance(exc, (socket.gaierror, OSError)):
        return f'Could not reach {imap_host()}:{imap_port()}.'
    return f'Unexpected mailbox error: {type(exc).__name__}.'


def _failure(message):
    return {'ok': False, 'message': message, 'messages': [], 'count': 0}


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

class _Mailbox:
    """A connection that always closes itself, even when something goes wrong."""

    def __init__(self, folder='INBOX', readonly=True):
        self.folder = resolve_folder(folder)
        self.readonly = readonly
        self.connection = None

    def __enter__(self):
        self.connection = imaplib.IMAP4_SSL(imap_host(), imap_port(), timeout=IMAP_TIMEOUT)
        self.connection.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
        typ, data = self.connection.select(self.folder, readonly=self.readonly)
        if typ != 'OK':
            raise imaplib.IMAP4.error(f'Cannot open the folder "{self.folder}".')
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        if self.connection is not None:
            # close() fails if no folder is selected, and logout() fails if the
            # socket is already gone. Neither should mask the real error.
            try:
                self.connection.close()
            except Exception:
                pass
            try:
                self.connection.logout()
            except Exception:
                pass
        return False


def resolve_folder(name):
    """Map what a person types onto what IMAP expects."""
    cleaned = (name or 'inbox').strip()
    return FOLDER_ALIASES.get(cleaned.lower(), cleaned)


def check_connection():
    """Prove the mailbox is reachable, without reading anything from it."""
    if not is_configured():
        return {'ok': False, 'configured': False,
                'message': 'No mail credentials are set, so the mailbox cannot be opened.'}

    try:
        with _Mailbox('INBOX') as connection:
            typ, data = connection.status('INBOX', '(MESSAGES UNSEEN)')
            detail = data[0].decode('utf-8', 'replace') if typ == 'OK' and data else ''
    except Exception as exc:
        return {'ok': False, 'configured': True, 'message': _describe(exc)}

    total, unread = _parse_status(detail)
    return {
        'ok': True,
        'configured': True,
        'total': total,
        'unread': unread,
        'message': (f'Connected to {imap_host()} as {account()}. '
                    f'{total} messages, {unread} unread.'),
    }


def _parse_status(detail):
    total = re.search(r'MESSAGES\s+(\d+)', detail or '')
    unread = re.search(r'UNSEEN\s+(\d+)', detail or '')
    return (int(total.group(1)) if total else 0,
            int(unread.group(1)) if unread else 0)


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------

def _decode_header(raw):
    """Decode an RFC 2047 header into plain text.

    Subjects arrive as '=?UTF-8?B?...?=' more often than not, and a mailbox
    listing full of those is unreadable.
    """
    if not raw:
        return ''
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        return str(raw)

    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or 'utf-8', 'replace'))
            except (LookupError, TypeError):
                out.append(text.decode('utf-8', 'replace'))
        else:
            out.append(text)
    return _WHITESPACE_RE.sub(' ', _INVISIBLE_RE.sub('', ''.join(out))).strip()


def _address_only(raw):
    """"Aria <aria@example.com>" -> "aria@example.com"."""
    _name, address = email.utils.parseaddr(raw or '')
    return address


def _display_sender(raw):
    """Prefer the human name, but never lose the address entirely."""
    decoded = _decode_header(raw)
    name, address = email.utils.parseaddr(decoded)
    if name and address:
        return f'{name} <{address}>'
    return address or decoded


def _parse_date(raw):
    """Return an aware datetime, or None when the header is unusable."""
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _html_to_text(markup):
    text = re.sub(r'(?is)<(script|style).*?</\1>', ' ', markup or '')
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</p\s*>', '\n\n', text)
    text = _HTML_TAG_RE.sub('', text)
    # html.unescape covers the whole entity table. Doing it by hand left
    # marketing mail reading "&#8199;&#847;&shy;" in every snippet.
    return html.unescape(text)


def _body_text(message):
    """The readable body: text/plain if offered, otherwise stripped HTML.

    Attachments are skipped -- an inline base64 PDF is not something to put in
    a chat bubble or a model's context window.
    """
    plain, html = [], []

    for part in message.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        disposition = str(part.get('Content-Disposition') or '')
        if 'attachment' in disposition.lower():
            continue

        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue

        charset = part.get_content_charset() or 'utf-8'
        try:
            decoded = payload.decode(charset, 'replace')
        except LookupError:
            decoded = payload.decode('utf-8', 'replace')

        if part.get_content_type() == 'text/plain':
            plain.append(decoded)
        elif part.get_content_type() == 'text/html':
            html.append(decoded)

    text = '\n'.join(plain) if plain else _html_to_text('\n'.join(html))
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = _BLANK_RUN_RE.sub('\n\n', text).strip()
    return text[:MAX_BODY_CHARS]


def _snippet(text):
    flat = _WHITESPACE_RE.sub(' ', (text or '').replace('\n', ' ')).strip()
    return flat[:SNIPPET_CHARS] + ('...' if len(flat) > SNIPPET_CHARS else '')


def _summarise(message, uid, flags, with_body=False):
    """One mailbox message, reduced to what a chat bubble needs."""
    sent_at = _parse_date(message.get('Date'))
    body = _body_text(message) if with_body else ''

    record = {
        'uid': str(uid),
        'sender': _display_sender(message.get('From')),
        'sender_email': _address_only(_decode_header(message.get('From'))),
        'to': _decode_header(message.get('To')),
        'subject': _decode_header(message.get('Subject')) or '(no subject)',
        'message_id': (message.get('Message-ID') or '').strip(),
        # ISO text, not a datetime: this record is stored on ChatMessage.metadata,
        # which is a JSONField, and json.dumps cannot serialise a datetime.
        # IMAP already returns newest-first, so nothing here needs to sort.
        'date': sent_at.isoformat() if sent_at else '',
        'date_display': (timezone.localtime(sent_at).strftime('%d %b %Y at %H:%M')
                         if sent_at else 'unknown date'),
        'is_unread': '\\Seen' not in (flags or ''),
        'snippet': '',
        'body': body,
    }
    record['snippet'] = _snippet(body) if with_body else ''
    return record


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _clamp(limit):
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _fetch_summaries(connection, uids, with_body):
    """Fetch a bounded set of UIDs, newest first.

    Headers only unless a body was asked for, and always with BODY.PEEK so the
    unread flag survives being looked at.
    """
    part = 'BODY.PEEK[]' if with_body else 'BODY.PEEK[HEADER]'
    out = []

    for uid in uids:
        typ, data = connection.uid('FETCH', uid, f'(FLAGS {part})')
        if typ != 'OK' or not data or not isinstance(data[0], tuple):
            continue

        envelope = data[0][0].decode('utf-8', 'replace')
        flags = envelope[envelope.find('FLAGS'):] if 'FLAGS' in envelope else ''
        try:
            parsed = email.message_from_bytes(data[0][1])
        except Exception:
            continue

        out.append(_summarise(parsed, uid.decode() if isinstance(uid, bytes) else uid,
                              flags, with_body=with_body))
    return out


def _search(connection, criteria):
    """Run a search and return UIDs newest-first."""
    if isinstance(criteria, str):
        typ, data = connection.uid('SEARCH', None, criteria)
    else:
        typ, data = connection.uid('SEARCH', *criteria)
    if typ != 'OK' or not data or not data[0]:
        return []
    return list(reversed(data[0].split()))


def list_messages(folder='inbox', limit=DEFAULT_LIMIT, unread_only=False,
                  with_body=False):
    """The most recent messages in a folder, newest first."""
    if not is_configured():
        return _failure('No mail credentials are set, so the mailbox cannot be opened.')

    limit = _clamp(limit)
    try:
        with _Mailbox(folder) as connection:
            uids = _search(connection, 'UNSEEN' if unread_only else 'ALL')
            total = len(uids)
            found = _fetch_summaries(connection, uids[:limit], with_body)
    except Exception as exc:
        return _failure(_describe(exc))

    scope = 'unread message' if unread_only else 'message'
    return {
        'ok': True,
        'messages': found,
        'count': len(found),
        'total': total,
        'folder': resolve_folder(folder),
        'message': (f'{total} {scope}{"" if total == 1 else "s"} in '
                    f'{resolve_folder(folder)}; showing {len(found)}.'),
    }


def search_messages(query, folder='inbox', limit=DEFAULT_LIMIT, with_body=False):
    """Search the mailbox.

    Gmail's own search syntax is used when the server offers X-GM-RAW, so
    "from:university has:attachment" works exactly as it does in the web
    interface. Any other server falls back to a standard IMAP TEXT search.
    """
    if not is_configured():
        return _failure('No mail credentials are set, so the mailbox cannot be opened.')

    cleaned = (query or '').strip()
    if not cleaned:
        return _failure('Give me something to search for.')

    limit = _clamp(limit)
    try:
        with _Mailbox(folder) as connection:
            uids, syntax = [], 'imap'
            if 'X-GM-EXT-1' in str(connection.capabilities):
                try:
                    uids = _search(connection, ('X-GM-RAW', f'"{cleaned}"'))
                    syntax = 'gmail'
                except imaplib.IMAP4.error:
                    uids = []
            if not uids:
                uids = _search(connection, ('TEXT', f'"{cleaned}"'))
                syntax = 'imap'
            total = len(uids)
            found = _fetch_summaries(connection, uids[:limit], with_body)
    except Exception as exc:
        return _failure(_describe(exc))

    return {
        'ok': True,
        'messages': found,
        'count': len(found),
        'total': total,
        'query': cleaned,
        'syntax': syntax,
        'message': (f'{total} message{"" if total == 1 else "s"} match "{cleaned}"; '
                    f'showing {len(found)}.'),
    }


def get_message(uid, folder='inbox'):
    """One message, with its body, addressed by UID."""
    if not is_configured():
        return _failure('No mail credentials are set, so the mailbox cannot be opened.')

    try:
        with _Mailbox(folder) as connection:
            found = _fetch_summaries(connection, [str(uid).encode()], with_body=True)
    except Exception as exc:
        return _failure(_describe(exc))

    if not found:
        return _failure(f'No message with id {uid} in {resolve_folder(folder)}.')

    # `message` is the human-readable status line everywhere in this module, so
    # the record itself goes under `mail`. Returning the dict as `message` once
    # broke every caller that logged result['message'][:300].
    return {'ok': True, 'messages': found, 'count': 1, 'mail': found[0],
            'message': f'Opened "{found[0]["subject"]}" from {found[0]["sender"]}.'}


def folders():
    """Every folder the account exposes, as typeable names."""
    if not is_configured():
        return _failure('No mail credentials are set, so the mailbox cannot be opened.')

    try:
        connection = imaplib.IMAP4_SSL(imap_host(), imap_port(), timeout=IMAP_TIMEOUT)
        try:
            connection.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
            typ, data = connection.list()
        finally:
            try:
                connection.logout()
            except Exception:
                pass
    except Exception as exc:
        return _failure(_describe(exc))

    if typ != 'OK':
        return _failure('The mail server would not list the folders.')

    names = []
    for row in data or []:
        text = row.decode('utf-8', 'replace') if isinstance(row, bytes) else str(row)
        match = re.search(r'"([^"]+)"$|([^ ]+)$', text)
        if match:
            names.append(match.group(1) or match.group(2))

    return {'ok': True, 'folders': names, 'count': len(names),
            'message': f'{len(names)} folders.'}


def save_draft(subject, body, recipient):
    """Append a draft to the mailbox, so it appears in Gmail's Drafts.

    This is the one function here that writes, and it only ever adds: it
    appends a new message and never modifies or removes an existing one.
    """
    if not is_configured():
        return {'ok': False, 'message': 'No mail credentials are set, so no draft can be saved.'}

    from email.message import EmailMessage

    draft = EmailMessage()
    draft['From'] = account()
    draft['To'] = recipient
    draft['Subject'] = subject
    draft['Date'] = email.utils.formatdate(localtime=True)
    draft.set_content(body)

    folder = resolve_folder('drafts')
    try:
        connection = imaplib.IMAP4_SSL(imap_host(), imap_port(), timeout=IMAP_TIMEOUT)
        try:
            connection.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
            typ, _data = connection.append(
                folder, '\\Draft', imaplib.Time2Internaldate(datetime.now()),
                draft.as_bytes())
        finally:
            try:
                connection.logout()
            except Exception:
                pass
    except Exception as exc:
        return {'ok': False, 'message': _describe(exc)}

    if typ != 'OK':
        return {'ok': False, 'message': f'The mail server would not save the draft to {folder}.'}

    return {'ok': True, 'folder': folder,
            'message': f'Draft "{subject}" saved to {folder} for {recipient}.'}
