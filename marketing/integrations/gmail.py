"""Gmail: the mailbox the AI workforce reads, triages and answers from.

WHAT IT IS USED FOR
-------------------
Email is the one channel every employee on the roster touches. The Support
employee imports a customer complaint as a ticket and drafts the reply. The HR
employee reads job applications, pulls the resume out of the body and sends the
interview invitation. The Marketing employee sends approved outreach. All of
that arrives through this connector, which is the single door onto the mailbox.

WHY IMAP AND SMTP RATHER THAN THE GMAIL API
-------------------------------------------
The Gmail REST API needs an OAuth application, a consent screen and a Google
verification review before it will touch a real mailbox. An app password needs
two-step verification and about a minute. Since the project already had working
IMAP reading (``marketing/gmail_client.py``) and working SMTP sending
(``marketing/mailer.py``), this connector is built on those rather than on a
second, half-finished credential path. It reuses the decoding, searching and
fetching helpers in ``gmail_client`` instead of writing a third mailbox parser.

That makes Gmail the one connector in the project whose live path works out of
the box on a laptop, which is why it is worth the compromise: everything below
this docstring can be demonstrated for real.

THE CREDENTIAL, AND WHERE A PERSON GETS IT
------------------------------------------
An app password, not the account password:

    1. Switch two-step verification on at myaccount.google.com/security.
    2. Visit myaccount.google.com/apppasswords and create one, naming it
       something like "AI Workforce".
    3. Paste the sixteen characters into the App password field on the
       Integrations page. Spaces are ignored.
    4. Check that IMAP is enabled in Gmail under Settings, Forwarding and
       POP/IMAP.

THE ONE EXCEPTION TO THE NO-ENVIRONMENT RULE
--------------------------------------------
Every other connector in this project reads its credential from its
Integration row and from nowhere else. This one also accepts the mail
credentials the project already had in its environment (EMAIL_HOST_USER and
EMAIL_HOST_PASSWORD, surfaced through ``marketing/mailer.py``), and uses them
only when the Integration row supplies nothing. The reason is compatibility
rather than convenience: an installation that already sends approved outreach
must not stop sending because the Integrations page has not been filled in yet.
The row always wins when it is filled in.

WHAT DEMO MODE DOES INSTEAD
---------------------------
Nothing leaves the machine, and a deterministic six-message mailbox stands in
for the real one: a refund request, an angry delivery complaint, a job
application with a full resume in the body, an interview availability reply, a
vendor invoice and a newsletter. Those six exist so the demonstrations that
matter have something real to chew on -- the Support employee's ticket import
and the HR employee's resume parsing both run against this data. Message ids
are derived from a hash of the subject, so listing the mailbox and then reading
one of the messages it returned works exactly as it does live.
"""

import contextlib
import email.utils
import hashlib
import imaplib
import shlex
import smtplib
import socket
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage

from . import register
from .base import CallResult, ConfigField, Connector

# The mailbox this was built against held over ten thousand messages, so every
# listing is bounded. gmail_client owns those bounds; this connector borrows
# them rather than inventing a second set.
LISTING_DEFAULT = 20


# ===========================================================================
# The stand-in mailbox
# ===========================================================================

# Six messages that between them cover the two flows demonstrated most often:
# customer support triage and recruitment. The bodies are deliberately full
# rather than suggestive -- the resume below is meant to be parsed and scored,
# not glanced at.
_DEMO_MAILBOX = (
    {
        'from': 'Meera Sundaram <meera.sundaram@example.com>',
        'to': 'support@example.com',
        'subject': 'Refund request for order 48219',
        'date': '2026-09-08T09:14:00+10:00',
        'unread': True,
        'body': (
            'Hello,\n\n'
            'I ordered the Northwind desk lamp on 29 August (order 48219) and it '
            'arrived with the shade cracked along the seam. I have not used it. '
            'I would like a refund rather than a replacement, because I have '
            'since bought a different lamp.\n\n'
            'Your website says refunds are available within 30 days of delivery, '
            'so I believe this is inside the window. I have photographs of the '
            'damage and of the packaging if you need them.\n\n'
            'Could you confirm how long the refund takes to reach my card?\n\n'
            'Thank you,\n'
            'Meera Sundaram\n'
            'Order 48219'
        ),
    },
    {
        'from': 'Daniel Okafor <daniel.okafor@example.com>',
        'to': 'support@example.com',
        'subject': 'Third failed delivery attempt -- this is unacceptable',
        'date': '2026-09-08T07:52:00+10:00',
        'unread': True,
        'body': (
            'This is the third time your courier has claimed nobody was home.\n\n'
            'I work from home. I have not left the house on any of those three '
            'days. Nobody rang the bell, nobody left a card, and the tracking page '
            'updated itself to "customer not available" at 04:40 in the morning, '
            'which I find hard to believe.\n\n'
            'I have now wasted three days waiting in for a parcel that has been '
            'sitting in a depot eleven kilometres away since 1 September. Order '
            '47905, tracking NB4471902AU.\n\n'
            'Either it is delivered tomorrow or I want the whole order cancelled '
            'and refunded in full. I would also like to know what you intend to do '
            'about the courier, because I am not the only person complaining about '
            'them in your reviews.\n\n'
            'Daniel Okafor'
        ),
    },
    {
        'from': 'Arjun Mehta <arjun.mehta@example.com>',
        'to': 'careers@example.com',
        'subject': 'Application: Backend Engineer (Django) -- Arjun Mehta',
        'date': '2026-09-07T18:31:00+10:00',
        'unread': True,
        'body': (
            'Dear Hiring Team,\n\n'
            'I am applying for the Backend Engineer (Django) role advertised on '
            'your careers page. My resume is below as plain text, since your '
            'posting asked applicants not to attach files.\n\n'
            '-----------------------------------------\n'
            'ARJUN MEHTA\n'
            'Melbourne VIC 3000 | arjun.mehta@example.com | 0400 000 111\n\n'
            'SUMMARY\n'
            'Backend engineer with six years building Django services for '
            'logistics and payments teams. Comfortable owning a service from '
            'schema design through to on-call. Strong on PostgreSQL, queueing '
            'and the unglamorous work of making a slow endpoint fast.\n\n'
            'SKILLS\n'
            'Python, Django, Django REST Framework, PostgreSQL, Redis, Celery, '
            'Docker, GitHub Actions, AWS (ECS, RDS, S3), pytest, Terraform basics.\n\n'
            'EXPERIENCE\n'
            'Senior Backend Engineer, Freightline Systems, Melbourne\n'
            'March 2023 - present\n'
            '  Rebuilt the consignment pricing service, cutting median quote time '
            'from 900ms to 120ms by removing an N+1 across three tables.\n'
            '  Introduced Celery for label generation, which took a synchronous '
            '14-second request out of the checkout path.\n'
            '  Mentored two graduate engineers and ran the fortnightly code '
            'review clinic.\n\n'
            'Backend Engineer, Paylane Australia, Melbourne\n'
            'July 2020 - February 2023\n'
            '  Built the reconciliation pipeline that matched settlement files '
            'against 40,000 daily transactions.\n'
            '  Moved the test suite from 26 minutes to 7 by isolating database '
            'fixtures per module.\n\n'
            'Junior Developer, Corville Digital, Geelong\n'
            'February 2019 - June 2020\n'
            '  Maintained three client Django sites and their deployment scripts.\n\n'
            'EDUCATION\n'
            'BSc Computer Science, University of Melbourne, 2018.\n'
            '-----------------------------------------\n\n'
            'I can start with four weeks notice and I am happy to do a technical '
            'exercise. Thank you for your time.\n\n'
            'Arjun Mehta'
        ),
    },
    {
        'from': 'Sofia Rossi <sofia.rossi@example.com>',
        'to': 'careers@example.com',
        'subject': 'Re: First interview -- availability',
        'date': '2026-09-07T11:05:00+10:00',
        'unread': False,
        'body': (
            'Hi,\n\n'
            'Thank you for coming back to me so quickly. Any of these suit me:\n\n'
            '  Tuesday 15 September, any time after 10:00\n'
            '  Wednesday 16 September, before 12:00\n'
            '  Friday 18 September, 14:00 to 16:00\n\n'
            'I am in Melbourne, so all of those are local time. A video call is '
            'fine, and I can come into the office if you would rather meet in '
            'person.\n\n'
            'Kind regards,\n'
            'Sofia Rossi'
        ),
    },
    {
        'from': 'Northbridge Cloud Billing <billing@example.net>',
        'to': 'accounts@example.com',
        'subject': 'Invoice INV-2026-0451 is ready',
        'date': '2026-09-06T02:00:00+10:00',
        'unread': False,
        'body': (
            'Your invoice for the August billing period is ready.\n\n'
            'Invoice INV-2026-0451\n'
            'Period: 1 August 2026 to 31 August 2026\n'
            'Amount due: AUD 1,284.60\n'
            'Due date: 20 September 2026\n\n'
            'Compute 742.10, managed database 388.00, object storage 96.50, '
            'egress 58.00.\n\n'
            'Payment terms are 14 days. Reply to this address with any query '
            'about a line item.\n\n'
            'Northbridge Cloud'
        ),
    },
    {
        'from': 'The Django Weekly <hello@example.org>',
        'to': 'team@example.com',
        'subject': 'The Django Weekly -- issue 218',
        'date': '2026-09-05T20:15:00+10:00',
        'unread': False,
        'body': (
            'This week: what changed in the 6.1 release, three patterns for '
            'keeping migrations reviewable, and a long read on database '
            'connection pooling that is worth your commute.\n\n'
            'Also in this issue: a maintainer interview, six jobs, and the usual '
            'pile of packages nobody asked for.\n\n'
            'You are receiving this because somebody on your team subscribed. '
            'Unsubscribe at the foot of this message.'
        ),
    },
)

SNIPPET_CHARS = 160


def _demo_id(subject):
    """A stable, readable message id for one simulated message.

    Derived from the subject rather than from a counter or a clock, so a
    listing and a later read of the same message agree -- which is the whole
    point of a deterministic simulation.
    """
    digest = hashlib.sha1((subject or '').encode('utf-8')).hexdigest()
    return f'demo-msg-{digest[:8]}'


def _snippet(body):
    flat = ' '.join((body or '').split())
    return flat[:SNIPPET_CHARS] + ('...' if len(flat) > SNIPPET_CHARS else '')


def _demo_record(entry, with_body=False):
    """One simulated message in the same shape the live path returns."""
    record = {
        'message_id': _demo_id(entry['subject']),
        'from': entry['from'],
        'to': entry['to'],
        'subject': entry['subject'],
        'date': entry['date'],
        'date_display': _demo_display_date(entry['date']),
        'snippet': _snippet(entry['body']),
        'unread': entry['unread'],
        'mailbox': 'INBOX',
        'simulated': True,
    }
    if with_body:
        record['body'] = entry['body']
    return record


def _demo_display_date(iso):
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return parsed.strftime('%d %b %Y at %H:%M')


# ===========================================================================
# Query translation
# ===========================================================================

_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def _imap_date(value):
    """'2026-09-01' -> '01-Sep-2026', the only date format IMAP SEARCH takes."""
    parsed = datetime.strptime(value, '%Y-%m-%d')
    return f'{parsed.day:02d}-{_MONTHS[parsed.month - 1]}-{parsed.year}'


def _quote(value):
    """Quote a search term for IMAP, which has no escape character worth using."""
    return '"' + str(value).replace('"', '').replace('\\', '') + '"'


def _translate_query(query):
    """Turn a Gmail-style query into IMAP SEARCH criteria.

    Returns ``(criteria, description)``. The description is not decoration:
    IMAP understands a much smaller language than Gmail's search box, so every
    operation that accepts a query says in its summary what the query was
    actually reduced to. A person who searched for ``has:attachment`` and got
    everything back deserves to be told why.
    """
    raw = (query or '').strip()
    if not raw:
        return '', 'every message, newest first'

    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()

    criteria, described, words, ignored = [], [], [], []

    for token in tokens:
        lowered = token.lower()
        prefix, _, value = token.partition(':')
        prefix = prefix.lower()
        value = value.strip().strip('"\'')

        if lowered in ('is:unread', 'label:unread', 'in:unread'):
            criteria.append('UNSEEN')
            described.append('UNSEEN')
        elif lowered == 'is:read':
            criteria.append('SEEN')
            described.append('SEEN')
        elif lowered in ('is:starred', 'is:flagged'):
            criteria.append('FLAGGED')
            described.append('FLAGGED')
        elif prefix in ('from', 'to', 'cc', 'bcc', 'subject') and value:
            keyword = prefix.upper()
            criteria.append(f'{keyword} {_quote(value)}')
            described.append(f'{keyword} {_quote(value)}')
        elif prefix in ('after', 'newer') and value:
            try:
                criteria.append(f'SINCE {_imap_date(value)}')
                described.append(f'SINCE {_imap_date(value)}')
            except ValueError:
                ignored.append(token)
        elif prefix in ('before', 'older') and value:
            try:
                criteria.append(f'BEFORE {_imap_date(value)}')
                described.append(f'BEFORE {_imap_date(value)}')
            except ValueError:
                ignored.append(token)
        elif prefix == 'newer_than' and value.endswith('d') and value[:-1].isdigit():
            since = datetime.now() - timedelta(days=int(value[:-1]))
            stamp = _imap_date(since.strftime('%Y-%m-%d'))
            criteria.append(f'SINCE {stamp}')
            described.append(f'SINCE {stamp}')
        elif ':' in token and prefix in ('has', 'label', 'in', 'category', 'filename'):
            # Nothing in plain IMAP corresponds to these. Say so rather than
            # silently widening the search.
            ignored.append(token)
        else:
            words.append(token)

    if words:
        phrase = ' '.join(words)
        criteria.append(f'TEXT {_quote(phrase)}')
        described.append(f'TEXT {_quote(phrase)}')

    description = ' '.join(described) if described else 'ALL'
    if ignored:
        description += (f' (IMAP cannot filter on {", ".join(ignored)}, '
                        'so that part was dropped)')
    return ' '.join(criteria), description


def _matches_demo(entry, query):
    """Apply the same query, as far as it goes, to the stand-in mailbox."""
    raw = (query or '').strip()
    if not raw:
        return True

    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()

    haystack = ' '.join([entry['from'], entry['to'], entry['subject'],
                         entry['body']]).lower()
    words = []

    for token in tokens:
        lowered = token.lower()
        prefix, _, value = token.partition(':')
        prefix, value = prefix.lower(), value.strip().strip('"\'').lower()

        if lowered in ('is:unread', 'label:unread', 'in:unread'):
            if not entry['unread']:
                return False
        elif lowered == 'is:read':
            if entry['unread']:
                return False
        elif prefix == 'from' and value:
            if value not in entry['from'].lower():
                return False
        elif prefix == 'to' and value:
            if value not in entry['to'].lower():
                return False
        elif prefix == 'subject' and value:
            if value not in entry['subject'].lower():
                return False
        elif ':' in token:
            continue
        else:
            words.append(lowered)

    return all(word in haystack for word in words)


def _describe_smtp(exc, host, port):
    """Turn a delivery failure into something a person can act on.

    The vocabulary is deliberately the same as ``marketing/mailer.py`` uses,
    because a refused app password should read identically whether it was the
    approval queue or an AI employee that tripped over it.
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ('The mail server rejected the credentials. For Gmail this must be '
                'an app password from myaccount.google.com/apppasswords, with '
                'two-step verification switched on.')
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'The mail server refused every recipient address.'
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return ('The mail server refused the sender address. It must match the '
                'mailbox the app password belongs to.')
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return 'The mail server closed the connection unexpectedly.'
    if isinstance(exc, ssl.SSLError):
        return 'TLS negotiation with the mail server failed.'
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f'{host}:{port} did not respond in time.'
    if isinstance(exc, (socket.gaierror, OSError)):
        return f'Could not reach {host}:{port}. Check the network and the host name.'
    return f'{type(exc).__name__}: {exc}'


def _describe_imap(exc, host, port):
    """The same courtesy for a reading failure."""
    if isinstance(exc, imaplib.IMAP4.error):
        detail = str(exc).strip("b'\" ")
        if 'AUTHENTICATIONFAILED' in detail.upper() or 'Invalid credentials' in detail:
            return ('The mail server rejected the credentials. For Gmail this must '
                    'be an app password, and IMAP must be switched on in the '
                    "account's Forwarding and POP/IMAP settings.")
        return f'The mail server refused the request: {detail}'
    if isinstance(exc, ssl.SSLError):
        return 'The secure connection to the mail server failed.'
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f'{host}:{port} did not answer in time.'
    if isinstance(exc, (socket.gaierror, OSError)):
        return f'Could not reach {host}:{port}.'
    return f'{type(exc).__name__}: {exc}'


# ===========================================================================
# The connector
# ===========================================================================

@register
class GmailConnector(Connector):
    """Read and write one Gmail mailbox over IMAP and SMTP."""

    key = 'gmail'
    name = 'Gmail'
    description = ('Reads, searches, drafts and sends email from one mailbox '
                   'over IMAP and SMTP, using an app password rather than OAuth.')
    category = 'communication'
    icon = 'fa-envelope'
    color = '#ea4335'
    docs_url = 'https://support.google.com/accounts/answer/185833'

    config_fields = (
        ConfigField('email_address', 'Mailbox address', required=True,
                    placeholder='workforce@yourcompany.com',
                    help_text='The Gmail address the workforce sends and reads as.'),
        ConfigField('app_password', 'App password', field_type='password',
                    required=True, secret=True,
                    help_text=('Not the account password. Create one at '
                               'myaccount.google.com/apppasswords with two-step '
                               'verification switched on. Spaces are ignored.')),
        ConfigField('display_name', 'Sender name', default='AI Workforce',
                    help_text='The name recipients see beside the address.'),
        ConfigField('signature', 'Signature block', field_type='longtext',
                    help_text='Appended to every outgoing message.'),
        ConfigField('smtp_host', 'SMTP host', default='smtp.gmail.com'),
        ConfigField('smtp_port', 'SMTP port', field_type='number', default=587,
                    help_text='587 for STARTTLS, 465 for implicit TLS.'),
        ConfigField('imap_host', 'IMAP host', default='imap.gmail.com'),
        ConfigField('imap_port', 'IMAP port', field_type='number', default=993),
        ConfigField('reply_to', 'Reply-to address',
                    help_text='Optional. Where replies should go if not the mailbox itself.'),
    )

    operations = ('send_email', 'create_draft', 'list_messages', 'search_messages',
                  'read_message', 'get_profile')

    # -- credentials -------------------------------------------------------

    def _mail_address(self):
        """The mailbox address: the Integration row first, the environment second."""
        configured = self.setting('email_address')
        if configured:
            return str(configured).strip()
        from django.conf import settings
        return (getattr(settings, 'EMAIL_HOST_USER', '') or '').strip()

    def _mail_password(self):
        """The app password, with spaces removed.

        Google displays app passwords in groups of four and people paste them
        that way. Stripping the spaces here saves an unexplained
        authentication failure later.
        """
        configured = self.setting('app_password')
        if configured:
            return str(configured).replace(' ', '')
        from django.conf import settings
        return (getattr(settings, 'EMAIL_HOST_PASSWORD', '') or '').replace(' ', '')

    def is_configured(self):
        """True when there is a usable address and password from either source."""
        return bool(self._mail_address() and self._mail_password())

    def missing_settings(self):
        missing = []
        if not self._mail_address():
            missing.append('Mailbox address')
        if not self._mail_password():
            missing.append('App password')
        return missing

    # -- connections -------------------------------------------------------

    @contextlib.contextmanager
    def _mailbox(self, mailbox='INBOX', readonly=True):
        """An IMAP connection that always closes itself.

        Opened read-only by default and read with BODY.PEEK downstream, so
        looking at a message through an AI employee does not mark it read in
        Gmail. A tool that quietly alters your inbox as a side effect of
        reading it is a tool people stop trusting.
        """
        from .. import gmail_client

        host = str(self.setting('imap_host'))
        port = int(self.setting('imap_port'))
        folder = gmail_client.resolve_folder(mailbox)

        try:
            connection = imaplib.IMAP4_SSL(host, port,
                                           timeout=gmail_client.IMAP_TIMEOUT)
            connection.login(self._mail_address(), self._mail_password())
            typ, _data = connection.select(folder, readonly=readonly)
            if typ != 'OK':
                raise imaplib.IMAP4.error(f'Cannot open the folder "{folder}".')
        except Exception as exc:                                   # noqa: BLE001
            raise RuntimeError(_describe_imap(exc, host, port)) from exc

        try:
            yield connection, folder
        finally:
            # close() fails when no folder is selected and logout() fails when
            # the socket has already gone. Neither should mask the real error.
            with contextlib.suppress(Exception):
                connection.close()
            with contextlib.suppress(Exception):
                connection.logout()

    def _shape(self, record, with_body=False):
        """One gmail_client record, reduced to this connector's contract.

        ``message_id`` is the IMAP UID rather than the RFC 2822 Message-ID,
        because the UID is what ``read_message`` can address. The header value
        is kept alongside it as ``rfc_message_id`` for anything that needs to
        thread a reply.
        """
        shaped = {
            'message_id': record.get('uid', ''),
            'from': record.get('sender', ''),
            'from_email': record.get('sender_email', ''),
            'to': record.get('to', ''),
            'subject': record.get('subject', ''),
            'date': record.get('date', ''),
            'date_display': record.get('date_display', ''),
            'snippet': record.get('snippet', ''),
            'unread': record.get('is_unread', False),
            'rfc_message_id': record.get('message_id', ''),
        }
        if with_body:
            shaped['body'] = record.get('body', '')
        return shaped

    # -- send --------------------------------------------------------------

    def _compose(self, to, subject, body, cc=None, bcc=None, reply_to=None,
                 html=False):
        """Build the outgoing message, signature and all."""
        sender = self._mail_address()
        display = self.setting('display_name')
        signature = (self.setting('signature') or '').strip()

        text = body or ''
        if signature and signature not in text:
            text = f'{text.rstrip()}\n\n{signature}\n'

        message = EmailMessage()
        message['From'] = (email.utils.formataddr((str(display), sender))
                           if display else sender)
        message['To'] = _as_address_list(to)
        message['Subject'] = subject or ''
        message['Date'] = email.utils.formatdate(localtime=True)
        message_id = email.utils.make_msgid(domain=sender.split('@')[-1] or None)
        message['Message-ID'] = message_id

        if cc:
            message['Cc'] = _as_address_list(cc)
        if bcc:
            message['Bcc'] = _as_address_list(bcc)

        answer_to = reply_to or self.setting('reply_to')
        if answer_to:
            message['Reply-To'] = _as_address_list(answer_to)

        if html:
            message.set_content(text, subtype='html')
        else:
            message.set_content(text)

        return message, message_id, text

    def live_send_email(self, to, subject, body, cc=None, bcc=None,
                        reply_to=None, html=False):
        """Send one message over SMTP.

        The narrow try/except here is deliberate. The base class would report
        the raw smtplib exception, and 'SMTPAuthenticationError(535, ...)' is
        not something a person can act on, whereas 'this must be an app
        password' is.
        """
        recipients = _recipient_list(to) + _recipient_list(cc) + _recipient_list(bcc)
        if not recipients:
            return self.failure('No recipient address was given, so nothing was sent.')

        host = str(self.setting('smtp_host'))
        port = int(self.setting('smtp_port'))
        message, message_id, text = self._compose(
            to, subject, body, cc=cc, bcc=bcc, reply_to=reply_to, html=html)

        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
            try:
                server.ehlo()
                if port != 465:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(self._mail_address(), self._mail_password())
                refused = server.send_message(message)
            finally:
                with contextlib.suppress(Exception):
                    server.quit()
        except Exception as exc:                                   # noqa: BLE001
            return self.failure(_describe_smtp(exc, host, port))

        if refused and len(refused) >= len(recipients):
            return self.failure(
                f'The mail server refused every recipient: {", ".join(refused)}.')

        delivered = [address for address in recipients if address not in (refused or {})]
        note = (f' {len(refused)} address(es) were refused.' if refused else '')
        return self.ok(
            f'Sent "{subject}" to {", ".join(delivered)} from {self._mail_address()}.{note}',
            {
                'message_id': message_id,
                'to': _recipient_list(to),
                'cc': _recipient_list(cc),
                'bcc': _recipient_list(bcc),
                'subject': subject,
                'from': self._mail_address(),
                'refused': sorted(refused or {}),
                'characters': len(text),
                'html': bool(html),
                'transport': f'{host}:{port}',
            })

    def demo_send_email(self, to, subject, body='', cc=None, bcc=None,
                        reply_to=None, html=False):
        recipients = _recipient_list(to)
        signature = (self.setting('signature') or '').strip()
        digest = hashlib.sha1(f'{to}|{subject}'.encode('utf-8')).hexdigest()
        return self.simulated(
            f'Would send to {", ".join(recipients) or "nobody"} with subject '
            f'"{subject}". No mail left the platform.',
            {
                'message_id': f'demo-{digest[:6]}',
                'to': recipients,
                'cc': _recipient_list(cc),
                'bcc': _recipient_list(bcc),
                'subject': subject,
                'from': self._mail_address() or 'workforce@example.com',
                'body_preview': _snippet(body),
                'signature_appended': bool(signature),
                'html': bool(html),
                'simulated': True,
            })

    # -- drafts ------------------------------------------------------------

    def live_create_draft(self, to, subject, body, cc=None, bcc=None):
        """Append the message to Gmail's Drafts folder.

        There is no portable IMAP way to create a draft; appending to
        ``[Gmail]/Drafts`` with the \\Draft flag is the approach Gmail itself
        documents, and it is the only write this connector performs against
        the mailbox. If the folder is named differently on the account the
        append fails and the base class reports that honestly rather than
        pretending a draft exists.
        """
        from .. import gmail_client

        message, message_id, _text = self._compose(to, subject, body, cc=cc, bcc=bcc)
        folder = gmail_client.resolve_folder('drafts')
        host = str(self.setting('imap_host'))
        port = int(self.setting('imap_port'))

        try:
            connection = imaplib.IMAP4_SSL(host, port,
                                           timeout=gmail_client.IMAP_TIMEOUT)
            try:
                connection.login(self._mail_address(), self._mail_password())
                typ, _data = connection.append(
                    folder, '\\Draft',
                    imaplib.Time2Internaldate(datetime.now()),
                    message.as_bytes())
            finally:
                with contextlib.suppress(Exception):
                    connection.logout()
        except Exception as exc:                                   # noqa: BLE001
            return self.failure(_describe_imap(exc, host, port))

        if typ != 'OK':
            return self.failure(
                f'The mail server would not save the draft to {folder}. '
                'Check that the folder exists on this account.')

        return self.ok(
            f'Draft "{subject}" saved to {folder} for '
            f'{", ".join(_recipient_list(to)) or "no recipient"}. Nothing was sent.',
            {'message_id': message_id, 'folder': folder,
             'to': _recipient_list(to), 'cc': _recipient_list(cc),
             'subject': subject})

    def demo_create_draft(self, to, subject, body='', cc=None, bcc=None):
        digest = hashlib.sha1(f'draft|{to}|{subject}'.encode('utf-8')).hexdigest()
        return self.simulated(
            f'Would save a draft of "{subject}" for '
            f'{", ".join(_recipient_list(to)) or "no recipient"} in [Gmail]/Drafts. '
            'Nothing was written to a real mailbox.',
            {'message_id': f'demo-draft-{digest[:6]}',
             'folder': '[Gmail]/Drafts',
             'to': _recipient_list(to), 'cc': _recipient_list(cc),
             'subject': subject, 'body_preview': _snippet(body),
             'simulated': True})

    # -- reading -----------------------------------------------------------

    def live_list_messages(self, query='', limit=LISTING_DEFAULT, mailbox='INBOX'):
        """The most recent messages in a folder, newest first."""
        from .. import gmail_client

        count = gmail_client._clamp(limit)
        criteria, described = _translate_query(query)

        with self._mailbox(mailbox) as (connection, folder):
            uids = gmail_client._search(connection, criteria or 'ALL')
            total = len(uids)
            found = gmail_client._fetch_summaries(connection, uids[:count],
                                                  with_body=True)

        messages = [self._shape(record) for record in found]
        return self.ok(
            f'{total} message{"" if total == 1 else "s"} in {folder} matching '
            f'{described}; showing {len(messages)}.',
            {'messages': messages, 'count': len(messages), 'total': total,
             'mailbox': folder, 'query': query, 'imap_criteria': criteria or 'ALL'})

    def demo_list_messages(self, query='', limit=LISTING_DEFAULT, mailbox='INBOX'):
        entries = [entry for entry in _DEMO_MAILBOX if _matches_demo(entry, query)]
        messages = [_demo_record(entry) for entry in entries[:max(1, int(limit or 1))]]
        unread = sum(1 for entry in entries if entry['unread'])
        return self.simulated(
            f'{len(entries)} message{"" if len(entries) == 1 else "s"} in the '
            f'stand-in {mailbox} ({unread} unread); showing {len(messages)}. '
            'This is the simulated mailbox, not a real one.',
            {'messages': messages, 'count': len(messages), 'total': len(entries),
             'unread': unread, 'mailbox': mailbox, 'query': query,
             'simulated': True})

    def live_search_messages(self, query, limit=LISTING_DEFAULT, mailbox='INBOX'):
        """Search the mailbox with IMAP SEARCH, saying what the query became."""
        from .. import gmail_client

        if not (query or '').strip():
            return self.failure('Give me something to search for.')

        count = gmail_client._clamp(limit)
        criteria, described = _translate_query(query)

        with self._mailbox(mailbox) as (connection, folder):
            uids = gmail_client._search(connection, criteria or 'ALL')
            total = len(uids)
            found = gmail_client._fetch_summaries(connection, uids[:count],
                                                  with_body=True)

        messages = [self._shape(record) for record in found]
        return self.ok(
            f'"{query}" was translated to IMAP {described} and matched {total} '
            f'message{"" if total == 1 else "s"} in {folder}; showing {len(messages)}.',
            {'messages': messages, 'count': len(messages), 'total': total,
             'mailbox': folder, 'query': query, 'imap_criteria': criteria or 'ALL',
             'translated_to': described})

    def demo_search_messages(self, query, limit=LISTING_DEFAULT, mailbox='INBOX'):
        _criteria, described = _translate_query(query)
        entries = [entry for entry in _DEMO_MAILBOX if _matches_demo(entry, query)]
        messages = [_demo_record(entry) for entry in entries[:max(1, int(limit or 1))]]
        return self.simulated(
            f'"{query}" reads as IMAP {described} and matches '
            f'{len(entries)} of the {len(_DEMO_MAILBOX)} messages in the '
            'stand-in mailbox. Nothing real was searched.',
            {'messages': messages, 'count': len(messages), 'total': len(entries),
             'mailbox': mailbox, 'query': query, 'translated_to': described,
             'simulated': True})

    def live_read_message(self, message_id, mailbox='INBOX'):
        """One message with its full body, addressed by IMAP UID."""
        from .. import gmail_client

        with self._mailbox(mailbox) as (connection, folder):
            found = gmail_client._fetch_summaries(
                connection, [str(message_id).encode()], with_body=True)

        if not found:
            return self.failure(
                f'No message with id {message_id} in {mailbox}. Ids come from '
                'list_messages or search_messages and are IMAP UIDs.')

        record = self._shape(found[0], with_body=True)
        return self.ok(
            f'"{record["subject"]}" from {record["from"]}, '
            f'{record["date_display"] or "unknown date"} '
            f'({len(record["body"])} characters).',
            {'message': record, 'mailbox': mailbox})

    def demo_read_message(self, message_id, mailbox='INBOX'):
        wanted = str(message_id or '').strip().lower()
        for entry in _DEMO_MAILBOX:
            if wanted in (_demo_id(entry['subject']).lower(),
                          entry['subject'].lower()):
                record = _demo_record(entry, with_body=True)
                return self.simulated(
                    f'Simulated message "{record["subject"]}" from '
                    f'{record["from"]}, {record["date_display"]} '
                    f'({len(record["body"])} characters). This came from the '
                    'stand-in mailbox.',
                    {'message': record, 'mailbox': mailbox, 'simulated': True})

        known = ', '.join(_demo_id(entry['subject']) for entry in _DEMO_MAILBOX)
        return CallResult(
            ok=False, demo=True, provider=self.key,
            error=(f'No simulated message with id {message_id}. '
                   f'The stand-in mailbox holds: {known}.'))

    # -- profile and probe -------------------------------------------------

    def live_get_profile(self):
        """The configured address and what is actually in the mailbox."""
        from .. import gmail_client, mailer

        counts = {}
        with self._mailbox('INBOX') as (connection, _folder):
            for label in ('INBOX', '[Gmail]/Drafts', '[Gmail]/Sent Mail'):
                try:
                    typ, data = connection.status(label, '(MESSAGES UNSEEN)')
                except Exception:                                  # noqa: BLE001
                    continue
                if typ != 'OK' or not data:
                    continue
                detail = data[0].decode('utf-8', 'replace')
                total, unread = gmail_client._parse_status(detail)
                counts[label] = {'total': total, 'unread': unread}

        inbox = counts.get('INBOX', {'total': 0, 'unread': 0})
        source = ('the Integrations page' if self.setting('email_address')
                  else 'the mail settings this installation already had')
        return self.ok(
            f'Connected to {self.setting("imap_host")} as {self._mail_address()}. '
            f'INBOX holds {inbox["total"]} messages, {inbox["unread"]} unread. '
            f'Credentials came from {source}. Sending goes out through '
            f'{mailer.backend_label()}.',
            {'email_address': self._mail_address(),
             'display_name': self.setting('display_name'),
             'imap_host': self.setting('imap_host'),
             'smtp_host': f'{self.setting("smtp_host")}:{self.setting("smtp_port")}',
             'reply_to': self.setting('reply_to') or self._mail_address(),
             'folders': counts,
             'inbox_total': inbox['total'],
             'inbox_unread': inbox['unread']})

    def demo_get_profile(self):
        unread = sum(1 for entry in _DEMO_MAILBOX if entry['unread'])
        return self.simulated(
            f'The stand-in mailbox holds {len(_DEMO_MAILBOX)} messages, '
            f'{unread} unread, addressed as '
            f'{self._mail_address() or "workforce@example.com"}. Add a mailbox '
            'address and an app password on the Integrations page to read the '
            'real one.',
            {'email_address': self._mail_address() or 'workforce@example.com',
             'display_name': self.setting('display_name'),
             'inbox_total': len(_DEMO_MAILBOX),
             'inbox_unread': unread,
             'subjects': [entry['subject'] for entry in _DEMO_MAILBOX],
             'simulated': True})

    def live_probe(self):
        """Prove the mailbox opens, without reading a message from it."""
        return self.live_get_profile()


def _as_address_list(value):
    """Anything a tool might pass as recipients, as one header value."""
    return ', '.join(_recipient_list(value))


def _recipient_list(value):
    """Normalise a recipient argument into a list of addresses.

    Tools hand this a string, a comma-separated string or a list, depending on
    whether a person, a model or another tool assembled the call.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            parts.extend(_recipient_list(item))
        return parts
    return [part.strip() for part in str(value).replace(';', ',').split(',')
            if part.strip()]
