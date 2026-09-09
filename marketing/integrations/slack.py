"""Slack: where the workforce tells people what it has done.

WHAT IT IS USED FOR
-------------------
Slack is the internal voice of the platform. The HR employee announces a new
job opening in the people channel and posts the interview shortlist there. The
Engineering Manager posts the sprint summary and the list of blocked items. The
Support employee raises an escalation in the support channel so a human sees it
before the customer does. The Marketing employee circulates a draft caption for
comment before anything is published anywhere public.

None of that is conversation with a customer, which is why Slack is the channel
the platform is most willing to write to: the audience is the team that owns
the AI workforce, and a wrong message there is embarrassing rather than costly.

THE CREDENTIAL, AND WHERE A PERSON GETS IT
------------------------------------------
A bot user OAuth token, which begins ``xoxb-``:

    1. Create an app at api.slack.com/apps, choosing "From scratch" and the
       workspace it should live in.
    2. Under OAuth and Permissions, add the bot token scopes ``chat:write``,
       ``channels:read`` and ``users:read``. Add ``channels:history`` as well
       if the workforce should be able to read a channel back, and ``im:write``
       for direct messages.
    3. Press Install to Workspace and approve it.
    4. Copy the Bot User OAuth Token and paste it into the Bot user OAuth
       token field on the Integrations page.
    5. Invite the bot into each channel it should post in, with
       ``/invite @your-app``. A bot that has not been invited cannot post,
       and Slack reports that as ``not_in_channel``.

CHANNELS BY PURPOSE
-------------------
Tools should not have to know channel names. They pass a purpose -- 'hr',
'engineering', 'support', 'marketing', 'alerts' -- and ``channel_for`` maps it
onto whatever this workspace calls that channel, falling back to the default.
Renaming a channel is then a configuration change rather than a code change,
which is the whole point of the Integration row.

HOW SLACK REPORTS FAILURE
-------------------------
Slack answers almost everything with HTTP 200 and puts the verdict in the body
as ``{"ok": false, "error": "channel_not_found"}``. A connector that only
checked the status code would report every failure as a success, so every
method here checks the body and translates the error code into advice a person
can act on.

WHAT DEMO MODE DOES INSTEAD
---------------------------
Nothing is posted. The simulated result echoes the channel the purpose or name
resolved to and the text that would have been sent, with a deterministic
message timestamp derived from the channel and the text, so the same simulated
post has the same id twice. The channel and user listings are fabricated but
include the five channels this integration is configured with, so a tool that
looks up a channel before posting to it finds one.
"""

import hashlib
import json
import urllib.error

from . import register
from .base import CallResult, ConfigField, Connector

API = 'https://slack.com/api'

# Slack's error codes, turned into something a person can act on. The bare
# code is kept in the result data for anyone debugging against Slack's own
# documentation.
_ERRORS = {
    'channel_not_found': ('No channel by that name exists in this workspace. '
                          'Check the spelling, and remember a private channel '
                          'has to have the bot invited into it before the API '
                          'can even see it.'),
    'not_in_channel': ('The bot is not a member of that channel. Invite it '
                       'with /invite @your-app in the channel itself.'),
    'is_archived': 'That channel is archived, so nothing can be posted to it.',
    'invalid_auth': ('Slack rejected the token. Confirm it is the Bot User '
                     'OAuth Token (it starts with xoxb-) and that the app is '
                     'still installed in this workspace.'),
    'not_authed': 'No token was sent. Add the bot token on the Integrations page.',
    'token_revoked': ('The token has been revoked. Reinstall the app at '
                      'api.slack.com/apps and copy the new bot token.'),
    'account_inactive': 'The bot user has been deactivated in this workspace.',
    'missing_scope': ('The token does not carry the scope this call needs. Add '
                      'it under OAuth and Permissions, then reinstall the app '
                      '-- a scope added without reinstalling does not take '
                      'effect.'),
    'not_allowed_token_type': ('This call needs a bot token rather than a user '
                               'token.'),
    'user_not_found': 'No user by that id or handle exists in this workspace.',
    'users_not_found': 'No user by that id or handle exists in this workspace.',
    'cannot_dm_bot': 'Slack does not allow a direct message to another bot.',
    'message_not_found': ('No message with that timestamp in that channel, so '
                          'there was nothing to update.'),
    'cant_update_message': ('The bot may not edit that message. A bot can only '
                            'edit messages it posted itself.'),
    'msg_too_long': 'The message is longer than Slack will accept.',
    'no_text': 'The message had no text, and Slack will not post an empty one.',
    'rate_limited': ('Slack is rate limiting this app. Wait a moment before '
                     'trying again.'),
    'invalid_blocks': ('Slack rejected the blocks payload. It must be a list of '
                       'Block Kit blocks.'),
}

# Which configured channel each purpose maps onto.
_PURPOSES = {
    'hr': 'hr_channel',
    'people': 'hr_channel',
    'recruitment': 'hr_channel',
    'engineering': 'engineering_channel',
    'eng': 'engineering_channel',
    'development': 'engineering_channel',
    'support': 'support_channel',
    'customer': 'support_channel',
    'marketing': 'marketing_channel',
    'content': 'marketing_channel',
    'social': 'marketing_channel',
    'alerts': 'alerts_channel',
    'alert': 'alerts_channel',
    'escalation': 'alerts_channel',
}


def _ts(*parts):
    """A deterministic Slack-shaped message timestamp.

    Slack ids a message by the epoch second and a six-digit counter. A demo
    result needs the same shape so anything that stores or displays a ts keeps
    working, and it needs to be stable for the same input so that a simulated
    update can find the simulated message it is updating.
    """
    digest = hashlib.sha1('|'.join(str(part) for part in parts).encode('utf-8'))
    number = int(digest.hexdigest()[:12], 16)
    # A fixed epoch base keeps the value plausible without reading the clock,
    # which is what makes the simulation reproducible.
    seconds = 1757000000 + (number % 2592000)
    return f'{seconds}.{number % 1000000:06d}'


@register
class SlackConnector(Connector):
    """Post to, read from and look around one Slack workspace."""

    key = 'slack'
    name = 'Slack'
    description = ('Posts announcements, summaries and escalations into the '
                   'team workspace, and reads a channel back when an employee '
                   'needs the context.')
    category = 'communication'
    icon = 'fa-slack'
    color = '#4a154b'
    docs_url = 'https://api.slack.com/authentication/basics'

    config_fields = (
        ConfigField('bot_token', 'Bot user OAuth token', field_type='password',
                    required=True, secret=True, placeholder='xoxb-...',
                    help_text=('Starts with xoxb-. Create an app at '
                               'api.slack.com/apps, add the chat:write, '
                               'channels:read and users:read scopes, install it '
                               'to the workspace and copy the bot token.')),
        ConfigField('default_channel', 'Default channel', default='#general',
                    help_text='Used whenever a tool does not name one.'),
        ConfigField('hr_channel', 'People channel', default='#people',
                    help_text='Job openings, shortlists and onboarding notices.'),
        ConfigField('engineering_channel', 'Engineering channel',
                    default='#engineering',
                    help_text='Sprint summaries, blockers and release notes.'),
        ConfigField('support_channel', 'Support channel', default='#support',
                    help_text='Ticket activity and customer escalations.'),
        ConfigField('marketing_channel', 'Marketing channel', default='#marketing',
                    help_text='Draft content circulated for comment.'),
        ConfigField('alerts_channel', 'Alerts channel', default='#alerts',
                    help_text='Anything that needs a person to look now.'),
        ConfigField('workspace_name', 'Workspace name',
                    help_text='Optional. Shown in summaries and audit entries.'),
        ConfigField('username_override', 'Post as', default='AI Workforce',
                    help_text=('The display name on posts. Slack honours this '
                               'only when the app has the chat:write.customize '
                               'scope.')),
    )

    operations = ('post_message', 'post_dm', 'list_channels', 'list_users',
                  'get_channel_history', 'update_message')

    # -- channels ----------------------------------------------------------

    def channel_for(self, purpose=''):
        """The channel one kind of message belongs in.

        Tools pass a purpose rather than a channel name, so that moving the
        HR announcements from #people to #hr-announcements is a change on the
        Integrations page and nowhere else.
        """
        field = _PURPOSES.get(str(purpose or '').strip().lower().lstrip('#'))
        if field:
            configured = self.setting(field)
            if configured:
                return _normalise_channel(configured)
        return _normalise_channel(self.setting('default_channel'))

    def _resolve(self, channel):
        """What a tool passed, turned into something Slack will accept.

        Three kinds of thing arrive here: nothing at all, a purpose such as
        'hr', and a real channel name or id. All three have to end up as a
        channel Slack recognises.
        """
        raw = str(channel or '').strip()
        if not raw:
            return _normalise_channel(self.setting('default_channel'))
        if raw.lower().lstrip('#') in _PURPOSES:
            return self.channel_for(raw)
        return _normalise_channel(raw)

    def _known_channels(self):
        """The five configured channels plus the default, de-duplicated."""
        names, seen = [], set()
        for field in ('default_channel', 'hr_channel', 'engineering_channel',
                      'support_channel', 'marketing_channel', 'alerts_channel'):
            name = _normalise_channel(self.setting(field))
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    # -- HTTP --------------------------------------------------------------

    def _post(self, method, payload):
        """One Slack write call, with the body-level ok check Slack requires."""
        token = str(self.setting('bot_token') or '').strip()
        _status, body = self.request_json(
            f'{API}/{method}', method='POST',
            headers={'Authorization': f'Bearer {token}'},
            payload=payload)
        return body or {}

    def _get(self, method, params=None):
        token = str(self.setting('bot_token') or '').strip()
        _status, body = self.request_json(
            f'{API}/{method}',
            headers={'Authorization': f'Bearer {token}'},
            params=params or {})
        return body or {}

    def _slack_failure(self, method, body):
        """Turn ``{"ok": false, "error": "..."}`` into a readable failure."""
        code = str((body or {}).get('error') or 'unknown_error')
        advice = _ERRORS.get(code)
        if advice is None:
            advice = (f'Slack refused the call with "{code}". The code is '
                      'documented on the api.slack.com page for this method.')
        detail = (body or {}).get('response_metadata') or {}
        messages = detail.get('messages') or []
        if messages:
            advice += ' Slack added: ' + '; '.join(str(item) for item in messages[:3])
        return self.failure(f'{method} failed: {advice}',
                            {'slack_error': code, 'method': method})

    # -- post_message ------------------------------------------------------

    def live_post_message(self, channel='', text='', thread_ts=None, blocks=None):
        target = self._resolve(channel)
        rich = _blocks_from(blocks)
        if not (text or rich):
            return self.failure('There was no text to post, so nothing was sent.')

        payload = {'channel': target, 'text': text or ''}
        if thread_ts:
            payload['thread_ts'] = str(thread_ts)
        if rich:
            payload['blocks'] = rich
        username = self.setting('username_override')
        if username:
            # Honoured only when the app carries chat:write.customize. Slack
            # ignores it silently otherwise, which is the behaviour wanted:
            # a missing cosmetic scope must not stop the message.
            payload['username'] = str(username)

        body = self._post('chat.postMessage', payload)
        if not body.get('ok'):
            return self._slack_failure('chat.postMessage', body)

        ts = body.get('ts', '')
        where = body.get('channel', target)
        return self.ok(
            f'Posted to {target} in '
            f'{self.setting("workspace_name") or "the workspace"}'
            f'{" as a threaded reply" if thread_ts else ""}.',
            {'ts': ts, 'channel': where, 'channel_name': target,
             'text': text, 'thread_ts': thread_ts or '',
             'permalink': _permalink(self.setting('workspace_name'), where, ts)})

    def demo_post_message(self, channel='', text='', thread_ts=None, blocks=None):
        target = self._resolve(channel)
        ts = _ts('post', target, text)
        return self.simulated(
            f'Would post to {target}: "{_short(text)}". Nothing was sent to '
            'Slack.',
            {'ts': ts, 'channel': target, 'channel_name': target,
             'text': text, 'thread_ts': thread_ts or '',
             'blocks': len(_blocks_from(blocks)),
             'resolved_from': str(channel or '(default channel)'),
             'simulated': True})

    # -- post_dm -----------------------------------------------------------

    def live_post_dm(self, user, text):
        """Open a direct-message conversation, then post into it.

        Two calls rather than one: ``chat.postMessage`` needs a channel id, and
        for a direct message that id only exists once ``conversations.open``
        has created the conversation.
        """
        handle = str(user or '').strip().lstrip('@')
        if not handle:
            return self.failure('No user was named, so no direct message was sent.')

        opened = self._post('conversations.open', {'users': handle})
        if not opened.get('ok'):
            return self._slack_failure('conversations.open', opened)

        channel_id = (opened.get('channel') or {}).get('id', '')
        payload = {'channel': channel_id, 'text': text or ''}
        body = self._post('chat.postMessage', payload)
        if not body.get('ok'):
            return self._slack_failure('chat.postMessage', body)

        return self.ok(
            f'Direct message sent to {handle}.',
            {'ts': body.get('ts', ''), 'channel': channel_id, 'user': handle,
             'text': text})

    def demo_post_dm(self, user, text):
        handle = str(user or '').strip().lstrip('@')
        digest = hashlib.sha1(handle.encode('utf-8')).hexdigest()[:9].upper()
        return self.simulated(
            f'Would send {handle or "nobody"} a direct message: "{_short(text)}". '
            'Nothing was sent to Slack.',
            {'ts': _ts('dm', handle, text), 'channel': f'D{digest}',
             'user': handle, 'text': text, 'simulated': True})

    # -- listings ----------------------------------------------------------

    def live_list_channels(self, limit=100):
        body = self._get('conversations.list',
                         {'limit': _clamp(limit, 1, 1000),
                          'exclude_archived': 'true',
                          'types': 'public_channel,private_channel'})
        if not body.get('ok'):
            return self._slack_failure('conversations.list', body)

        channels = [{
            'id': row.get('id', ''),
            'name': '#' + row.get('name', ''),
            'is_private': bool(row.get('is_private')),
            'members': row.get('num_members', 0),
            'topic': ((row.get('topic') or {}).get('value') or '')[:200],
            'is_member': bool(row.get('is_member')),
        } for row in body.get('channels') or []]

        joined = sum(1 for row in channels if row['is_member'])
        return self.ok(
            f'{len(channels)} channels in '
            f'{self.setting("workspace_name") or "the workspace"}; the bot is a '
            f'member of {joined}.',
            {'channels': channels, 'count': len(channels), 'bot_member_of': joined})

    def demo_list_channels(self, limit=100):
        configured = self._known_channels()
        extras = ['#random', '#announcements', '#product']
        rows, seen = [], set()
        for name in configured + extras:
            if name in seen:
                continue
            seen.add(name)
            digest = hashlib.sha1(name.encode('utf-8')).hexdigest()[:8].upper()
            rows.append({
                'id': f'C{digest}',
                'name': name,
                'is_private': False,
                'members': 4 + (int(digest, 16) % 40),
                'topic': ('Configured on the Integrations page'
                          if name in configured else 'Part of the stand-in workspace'),
                'is_member': name in configured,
            })
        rows = rows[:_clamp(limit, 1, 1000)]
        return self.simulated(
            f'{len(rows)} channels in the stand-in workspace, including the '
            f'{len(configured)} this integration is configured with '
            f'({", ".join(configured)}). Nothing was read from Slack.',
            {'channels': rows, 'count': len(rows),
             'configured': configured, 'simulated': True})

    def live_list_users(self, limit=100):
        body = self._get('users.list', {'limit': _clamp(limit, 1, 1000)})
        if not body.get('ok'):
            return self._slack_failure('users.list', body)

        users = []
        for row in body.get('members') or []:
            if row.get('deleted'):
                continue
            profile = row.get('profile') or {}
            users.append({
                'id': row.get('id', ''),
                'handle': '@' + row.get('name', ''),
                'real_name': profile.get('real_name') or row.get('real_name', ''),
                'title': profile.get('title', ''),
                'is_bot': bool(row.get('is_bot')),
                'is_admin': bool(row.get('is_admin')),
            })

        people = sum(1 for row in users if not row['is_bot'])
        return self.ok(
            f'{len(users)} active accounts, {people} of them people.',
            {'users': users, 'count': len(users), 'people': people})

    def demo_list_users(self, limit=100):
        # Fictional, and deliberately shaped like a small company: the roster
        # the AI employees would actually be mentioning by name.
        roster = (
            ('claire.hong', 'Claire Hong', 'Head of People', False, True),
            ('tomas.silva', 'Tomas Silva', 'Engineering Manager', False, False),
            ('priya.raman', 'Priya Raman', 'Senior Developer', False, False),
            ('ben.iwu', 'Ben Iwu', 'Support Lead', False, False),
            ('nadia.karim', 'Nadia Karim', 'Marketing Manager', False, False),
            ('sam.doyle', 'Sam Doyle', 'Operations', False, False),
            ('ai-workforce', 'AI Workforce', 'Platform bot', True, False),
        )
        users = []
        for handle, real_name, title, is_bot, is_admin in roster:
            digest = hashlib.sha1(handle.encode('utf-8')).hexdigest()[:8].upper()
            users.append({'id': f'U{digest}', 'handle': '@' + handle,
                          'real_name': real_name, 'title': title,
                          'is_bot': is_bot, 'is_admin': is_admin})
        users = users[:_clamp(limit, 1, 1000)]
        return self.simulated(
            f'{len(users)} accounts in the stand-in workspace. Nothing was read '
            'from Slack.',
            {'users': users, 'count': len(users),
             'people': sum(1 for row in users if not row['is_bot']),
             'simulated': True})

    # -- history -----------------------------------------------------------

    def live_get_channel_history(self, channel='', limit=20):
        target = self._resolve(channel)
        body = self._get('conversations.history',
                         {'channel': target, 'limit': _clamp(limit, 1, 200)})
        if not body.get('ok'):
            return self._slack_failure('conversations.history', body)

        messages = [{
            'ts': row.get('ts', ''),
            'user': row.get('user') or row.get('bot_id') or '',
            'text': row.get('text', ''),
            'thread_replies': row.get('reply_count', 0),
            'is_bot': bool(row.get('bot_id')),
        } for row in body.get('messages') or []]

        return self.ok(
            f'{len(messages)} recent messages from {target}, newest first.',
            {'messages': messages, 'count': len(messages), 'channel': target,
             'has_more': bool(body.get('has_more'))})

    def demo_get_channel_history(self, channel='', limit=20):
        target = self._resolve(channel)
        # A short, plausible conversation, seeded from the channel name so the
        # same channel reads the same way twice.
        script = (
            ('@claire.hong', 'Shortlist for the backend role is ready when you are.'),
            ('@ai-workforce', 'Three candidates scored above 70. Posting the '
                              'summary here shortly.'),
            ('@tomas.silva', 'Can we get the interviews inside next week? '
                             'Sprint planning is the Monday after.'),
            ('@ai-workforce', 'Proposing Tuesday 10:00, Wednesday 11:00 and '
                              'Friday 14:00. Waiting on approval before anything '
                              'is sent to candidates.'),
            ('@claire.hong', 'Approved. Send them.'),
        )
        messages = []
        for index, (user, text) in enumerate(script[:_clamp(limit, 1, 200)]):
            messages.append({
                'ts': _ts('history', target, index),
                'user': user,
                'text': text,
                'thread_replies': 0,
                'is_bot': user == '@ai-workforce',
            })
        messages.reverse()
        return self.simulated(
            f'{len(messages)} messages from the stand-in {target}, newest first. '
            'Nothing was read from Slack.',
            {'messages': messages, 'count': len(messages), 'channel': target,
             'has_more': False, 'simulated': True})

    # -- update ------------------------------------------------------------

    def live_update_message(self, channel, ts, text):
        target = self._resolve(channel)
        body = self._post('chat.update',
                          {'channel': target, 'ts': str(ts), 'text': text or ''})
        if not body.get('ok'):
            return self._slack_failure('chat.update', body)

        return self.ok(
            f'Edited the message at {ts} in {target}.',
            {'ts': body.get('ts', str(ts)), 'channel': body.get('channel', target),
             'text': text})

    def demo_update_message(self, channel, ts, text):
        target = self._resolve(channel)
        return self.simulated(
            f'Would edit the message at {ts} in {target} to read '
            f'"{_short(text)}". Nothing was changed in Slack.',
            {'ts': str(ts), 'channel': target, 'text': text, 'simulated': True})

    # -- probe -------------------------------------------------------------

    def live_probe(self):
        """``auth.test``: who the token belongs to, and where.

        Cheap, needs no scope beyond the token itself, and answers the only
        question worth asking of a Slack credential.
        """
        try:
            body = self._get('auth.test')
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')[:200]
            return self.failure(f'Slack returned HTTP {exc.code}. {detail}'.strip())

        if not body.get('ok'):
            return self._slack_failure('auth.test', body)

        team = body.get('team', '')
        configured = self.setting('workspace_name')
        mismatch = ''
        if configured and team and configured.strip().lower() != team.strip().lower():
            mismatch = (f' The workspace name is configured as "{configured}", '
                        f'which does not match.')

        return self.ok(
            f'Signed in to the {team} workspace as {body.get("user", "the bot")}. '
            f'Default channel {self.channel_for()}.{mismatch}',
            {'team': team, 'team_id': body.get('team_id', ''),
             'bot_user': body.get('user', ''), 'user_id': body.get('user_id', ''),
             'url': body.get('url', ''),
             'channels': self._known_channels()})


# ===========================================================================
# helpers
# ===========================================================================

def _normalise_channel(value):
    """Channel names keep their hash; channel and user ids do not gain one.

    Slack accepts either, but a name without the hash reads wrongly in a
    summary, and an id with one is rejected outright.
    """
    raw = str(value or '').strip()
    if not raw:
        return ''
    if raw.startswith('#'):
        return raw
    # C..., G..., D... are conversation ids; anything else is a name.
    if len(raw) >= 9 and raw[0] in 'CGD' and raw.upper() == raw:
        return raw
    return '#' + raw.lstrip('#')


def _permalink(workspace, channel, ts):
    """A best-effort link to the message, or an empty string.

    Slack's own permalink needs another API call. This builds the predictable
    form when the workspace name is configured and says nothing when it is not,
    rather than inventing a URL that would not open.
    """
    if not (workspace and channel and ts):
        return ''
    slug = ''.join(character for character in str(workspace).lower()
                   if character.isalnum() or character == '-')
    if not slug:
        return ''
    return f'https://{slug}.slack.com/archives/{channel}/p{str(ts).replace(".", "")}'


def _clamp(value, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(number, high))


def _short(text, limit=120):
    flat = ' '.join(str(text or '').split())
    return flat if len(flat) <= limit else flat[:limit - 3] + '...'


def _blocks_from(value):
    """Accept Block Kit blocks as a list or as a JSON string.

    A tool assembling blocks in Python passes a list; a language model asked
    for blocks passes a JSON string more often than not. Both should work, and
    anything unparseable becomes no blocks rather than an error, because the
    plain text always accompanies them.
    """
    if not value:
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return list(value) if isinstance(value, (list, tuple)) else []
