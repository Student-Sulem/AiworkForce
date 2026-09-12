"""Plain communication tools: write to an address, not to a record.

WHY THIS MODULE HAD TO BE ADDED
-------------------------------
Every other email tool in this platform is attached to a record. The HR
employee sends an interview invitation to a *candidate id*, the Support
employee replies to a *ticket id*. That is the right shape for those jobs: it
means the message is filed against the person it concerns, the candidate's
status moves when the rejection goes out, and nobody has to retype an address
that is already on file.

It is the wrong shape for the most ordinary request anybody makes of an
assistant, which is "email this address and say this". Faced with that, an
employee holding only record-shaped tools does the only thing it can: it goes
looking for a candidate or an employee row matching the address, fails to find
one, and either asks who that person is or offers to create a record for them.
The person asking wanted an email sent. They did not ask to be enrolled in a
recruitment pipeline.

So these tools take a recipient, a subject and a body, and nothing else. No
lookup, no record, no side effect on anybody's status.

WHAT HAS NOT CHANGED
--------------------
The message still goes to the approval queue, because that is the rule the
whole platform is built on and a general-purpose tool is precisely the wrong
place to make an exception to it: a tool that can write to any address with no
record behind it is the one most worth reading before it leaves.
"""

import re

from ..models_platform import OutboundMessage
from .base import Proposal, ToolResult, editable, executor, tool

ADDRESS_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _call(provider_key, operation, **kwargs):
    from .. import integrations
    return integrations.call(provider_key, operation, **kwargs)


def _addresses(value):
    """Split a recipient argument into clean addresses, however it arrived.

    A language model supplies this as a string, a comma-separated string, a
    semicolon-separated string or a list, depending on the model and the day.
    Accepting all four is cheaper than a tool that fails on a technicality
    after the employee has already written the message.
    """
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r'[;,]', str(value))
    out = []
    for item in items:
        text = str(item).strip().strip('<>')
        # Tolerate "Ada Lovelace <ada@example.com>".
        match = re.search(r'<([^>]+)>', str(item))
        if match:
            text = match.group(1).strip()
        if text:
            out.append(text)
    return out


def _invalid(addresses):
    return [a for a in addresses if not ADDRESS_RE.match(a)]


@tool(name='comms.send_email',
      title='Send an email to an address',
      description=(
          'Send an email to any address, given the address itself. Use this '
          'whenever somebody names a recipient directly -- "email '
          'alex@example.com about the delay" -- rather than naming a candidate, '
          'an employee or a ticket. It does NOT look the recipient up in any '
          'record and does not create one. Where the request does concern a '
          'candidate, an employee or a support ticket, prefer the tool for that '
          'record instead, so the message is filed against the right person. '
          'The email is prepared and placed in the approval queue; it is not '
          'sent until somebody approves it.'),
      group='Communication', shared=True, integration='gmail',
      requires_approval=True, risk='high', icon='fa-paper-plane',
      parameters={
          'type': 'object',
          'properties': {
              'to': {'type': 'string',
                     'description': 'The recipient address. Several may be '
                                    'separated by commas.'},
              'subject': {'type': 'string', 'description': 'The subject line.'},
              'body': {'type': 'string', 'description': 'The message body.'},
              'cc': {'type': 'string', 'description': 'Addresses to copy.'},
              'bcc': {'type': 'string', 'description': 'Addresses to blind copy.'},
          },
          'required': ['to', 'subject', 'body'],
      })
def send_email(ctx, to, subject, body, cc='', bcc=''):
    """Prepare an email to a named address. Sending it needs a person."""
    recipients = _addresses(to)
    if not recipients:
        return ToolResult(
            ok=False, error='no recipient',
            text='No recipient address was given. Supply the address to write to.')

    bad = _invalid(recipients) + _invalid(_addresses(cc)) + _invalid(_addresses(bcc))
    if bad:
        return ToolResult(
            ok=False, error='invalid address',
            text=(f'These do not look like email addresses: {", ".join(bad)}. '
                  f'Check them, or ask for the correct address rather than '
                  f'guessing one.'))

    if not str(body).strip():
        return ToolResult(
            ok=False, error='empty body',
            text='The message body is empty. Write the message, then send it.')

    shown = ', '.join(recipients)
    return Proposal(
        title=f'Email to {shown[:120]}: {str(subject)[:90]}',
        summary=(f'Would send to {len(recipients)} recipient'
                 f'{"s" if len(recipients) != 1 else ""} ({shown}). '
                 f'{len(str(body))} characters. Not attached to any record.'),
        payload={'to': ', '.join(recipients), 'subject': str(subject),
                 'body': str(body),
                 'cc': ', '.join(_addresses(cc)),
                 'bcc': ', '.join(_addresses(bcc))},
        editable_fields=[
            editable('to', 'To'),
            editable('subject', 'Subject'),
            editable('body', 'Message', 'longtext', rows=14),
            editable('cc', 'Cc'),
            editable('bcc', 'Bcc'),
        ],
        risk='high', integration_key='gmail',
        confirmation=(
            f'Wrote the email to {shown} and placed it in the approval queue. '
            f'It has not been sent. Open Approvals to read it, edit it if you '
            f'want, and approve it -- that is what sends it.'))


@executor('comms.send_email')
def _execute_send_email(action):
    payload = action.payload or {}
    result = _call('gmail', 'send_email',
                   to=payload.get('to', ''),
                   subject=payload.get('subject', ''),
                   body=payload.get('body', ''),
                   cc=payload.get('cc') or None,
                   bcc=payload.get('bcc') or None)

    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The email was not sent: {result.error}')

    message = OutboundMessage.objects.create(
        channel='email', recipient=payload.get('to', '')[:400],
        subject=payload.get('subject', '')[:300], body=payload.get('body', ''),
        status='simulated' if result.demo else 'sent',
        external_id=str((result.data or {}).get('message_id', '')),
        agent=action.agent, action=action, integration=action.integration,
        metadata={'cc': payload.get('cc', ''), 'bcc': payload.get('bcc', '')})

    return ToolResult(
        ok=True, demo=result.demo,
        text=(result.summary or f'Sent to {payload.get("to")}.')
             + f' Recorded as outbound message #{message.pk}.',
        data={'message_id': message.pk, 'simulated': result.demo},
        subject_label='marketing.outboundmessage', subject_id=message.pk)


@tool(name='comms.send_slack_message',
      title='Post a Slack message',
      description=(
          'Post a message to a Slack channel by name. Use this when somebody '
          'names a channel directly rather than asking to notify a team about a '
          'particular record. Prepared for approval; it is not posted until '
          'somebody approves it.'),
      group='Communication', shared=True, integration='slack',
      requires_approval=True, risk='high', icon='fa-slack',
      parameters={
          'type': 'object',
          'properties': {
              'text': {'type': 'string', 'description': 'The message.'},
              'channel': {'type': 'string',
                          'description': 'The channel, e.g. #general. Uses the '
                                         'configured default when empty.'},
          },
          'required': ['text'],
      })
def send_slack_message(ctx, text, channel=''):
    """Prepare a Slack message to a named channel. Posting it needs a person."""
    if not str(text).strip():
        return ToolResult(ok=False, error='empty message',
                          text='The message is empty. Write it, then send it.')

    where = str(channel).strip() or 'the default channel'
    return Proposal(
        title=f'Slack {where}: {str(text).strip()[:80]}',
        summary=f'Would post {len(str(text))} characters to {where}.',
        payload={'channel': str(channel).strip(), 'text': str(text)},
        editable_fields=[
            editable('text', 'Message', 'longtext', rows=10),
            editable('channel', 'Channel'),
        ],
        risk='high', integration_key='slack',
        confirmation=(f'Wrote the message for {where} and placed it in the '
                      f'approval queue. It has not been posted.'))


@executor('comms.send_slack_message')
def _execute_send_slack_message(action):
    payload = action.payload or {}
    result = _call('slack', 'post_message',
                   channel=payload.get('channel', ''), text=payload.get('text', ''))
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The message was not posted: {result.error}')

    where = (result.data or {}).get('channel') or payload.get('channel') or 'default'
    message = OutboundMessage.objects.create(
        channel='slack', recipient=str(where)[:400],
        subject=action.title[:300], body=payload.get('text', ''),
        status='simulated' if result.demo else 'sent',
        external_id=str((result.data or {}).get('ts', '')),
        agent=action.agent, action=action, integration=action.integration)

    return ToolResult(
        ok=True, demo=result.demo,
        text=(result.summary or f'Posted to {where}.')
             + f' Recorded as outbound message #{message.pk}.',
        data={'message_id': message.pk, 'simulated': result.demo})
