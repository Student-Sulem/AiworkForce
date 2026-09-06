"""Actually sending the outreach email an AI employee drafted.

WHERE THIS SITS IN THE WORKFLOW
-------------------------------
Aria drafts. A person approves. Only then does anything leave the server:

    chat reply -> submit_message_for_approval() -> ApprovalRequest (pending)
               -> a person presses Approve
               -> apply_approval() -> send_outreach()  <- this module

Nothing here is reachable without that approval, which is the whole point of
the queue.

CREDENTIALS
-----------
Read from the environment by marketpulse/settings.py: EMAIL_HOST_USER and
EMAIL_HOST_PASSWORD. They are never stored in the database or in settings.py.
With neither set, Django falls back to the console backend and an "sent" email
is printed to the terminal instead, so the project still runs on a machine with
no mail account.

EVERY FUNCTION IS TOTAL
-----------------------
Nothing here raises into a view. A refused login, a rejected recipient or a
dead network becomes a recorded failure on the EmailOutreach row, because a
delivery problem should not lose the approval decision that preceded it.
"""

import smtplib
import socket
import ssl

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.utils import timezone


def is_configured():
    """True when real credentials are present, rather than the console fallback."""
    return bool(settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD)


def backend_label():
    """How delivery is currently happening, for the Configurations page."""
    if is_configured():
        return f'SMTP via {settings.EMAIL_HOST}:{settings.EMAIL_PORT}'
    return 'Console (printed to the terminal, nothing is sent)'


def _describe(exc):
    """Turn an exception into something a person can act on."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ('The mail server rejected the credentials. For Gmail this must be '
                'an App Password, with two-step verification switched on.')
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'The mail server refused the recipient address.'
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return 'The mail server refused the sender address.'
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return 'The mail server closed the connection unexpectedly.'
    if isinstance(exc, ssl.SSLError):
        return 'TLS negotiation with the mail server failed.'
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f'The mail server did not respond within {settings.EMAIL_TIMEOUT} seconds.'
    if isinstance(exc, (socket.gaierror, OSError)):
        return f'Could not reach {settings.EMAIL_HOST}. Check the network.'
    return f'{exc.__class__.__name__}: {exc}'


def check_connection():
    """Authenticate against the mail server without sending anything.

    Returns {'ok', 'message', 'backend', 'configured'}. Used by the Test
    button on the Configurations page, so a credential can be proven before
    anyone approves an email that depends on it.
    """
    if not is_configured():
        return {
            'ok': False,
            'configured': False,
            'backend': backend_label(),
            'message': ('No mail credentials are set. Approved emails are printed to '
                        'the terminal instead of being sent. Set EMAIL_HOST_USER and '
                        'EMAIL_HOST_PASSWORD in the environment to send for real.'),
        }

    connection = get_connection(fail_silently=False)
    try:
        connection.open()
        connection.close()
    except Exception as exc:                                   # noqa: BLE001
        return {'ok': False, 'configured': True, 'backend': backend_label(),
                'message': _describe(exc)}

    return {
        'ok': True,
        'configured': True,
        'backend': backend_label(),
        'message': (f'Connected to {settings.EMAIL_HOST} and signed in as '
                    f'{settings.EMAIL_HOST_USER}. Nothing was sent.'),
    }


def send_outreach(email):
    """Send one drafted EmailOutreach, and record what happened.

    Returns {'ok', 'message'}. The outcome is written to the row either way, so
    the Approvals page can show whether the message really went out rather than
    only that somebody approved it.
    """
    recipient = email.recipient
    if not recipient:
        message = 'This email has no recipient address.'
        _record(email, error=message)
        return {'ok': False, 'message': message}

    body = email.body
    signature = (f'\n\n--\nDrafted by {email.agent.name} and approved by a person '
                 f'before sending.' if email.agent_id else '')

    message = EmailMultiAlternatives(
        subject=email.subject,
        body=body + signature,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
        reply_to=[settings.DEFAULT_FROM_EMAIL],
    )

    try:
        delivered = message.send(fail_silently=False)
    except Exception as exc:                                   # noqa: BLE001
        detail = _describe(exc)
        _record(email, error=detail)
        return {'ok': False, 'message': f'Could not send to {recipient}. {detail}'}

    if not delivered:
        detail = 'The mail server accepted the connection but delivered nothing.'
        _record(email, error=detail)
        return {'ok': False, 'message': detail}

    _record(email, error='')
    if not is_configured():
        return {'ok': True,
                'message': (f'Printed to the terminal for {recipient}. Set mail '
                            f'credentials to send it for real.')}
    return {'ok': True, 'message': f'Sent to {recipient}.'}


def _record(email, error):
    """Store the delivery outcome on the row."""
    email.delivery_error = error[:2000]
    email.delivered_at = None if error else timezone.now()
    email.status = 'bounced' if error else 'sent'
    email.save(update_fields=['delivery_error', 'delivered_at', 'status'])


def send_test_message(recipient):
    """Send a short message to prove the whole path end to end.

    Deliberately separate from send_outreach: this touches no prospect, no
    approval and no stored row, so it is safe to press twice.
    """
    if not is_configured():
        return {'ok': False,
                'message': 'No mail credentials are set, so there is nothing to test.'}

    try:
        sent = EmailMultiAlternatives(
            subject='AI Workforce: mail delivery is working',
            body=('This is a test from your AI Workforce installation.\n\n'
                  'If you are reading it, approved outreach emails will now be '
                  'delivered rather than printed to the terminal.\n\n'
                  'Nothing was sent to any prospect.'),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        ).send(fail_silently=False)
    except Exception as exc:                                   # noqa: BLE001
        return {'ok': False, 'message': _describe(exc)}

    if not sent:
        return {'ok': False, 'message': 'The mail server delivered nothing.'}
    return {'ok': True, 'message': f'Test message sent to {recipient}.'}
