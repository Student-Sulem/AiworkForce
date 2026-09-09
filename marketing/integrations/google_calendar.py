"""Google Calendar: putting a meeting in someone's day.

WHAT IT IS USED FOR
-------------------
The HR employee is the heaviest user. Asked to arrange a first interview, it
does not simply propose a time out of the air: it reads the calendar, finds the
gaps inside working hours, offers three of them, and once a person approves,
books the winner with the candidate as an attendee and a meeting link attached.
The Engineering Manager books sprint planning and retrospectives from the same
connector, the Support employee books customer callbacks, and the Marketing
employee blocks out content review slots.

``find_availability`` is the operation that matters most, and it is the one
deliberately built to work with nothing configured at all -- see below.

THE CREDENTIAL, AND WHERE A PERSON GETS IT
------------------------------------------
Google Calendar has no app-password equivalent, so this needs OAuth. Two
credentials are accepted:

  * An access token, for the short path. Anyone can mint one in a couple of
    minutes at developers.google.com/oauthplayground: press the gear, tick
    "Use your own OAuth credentials" if you have them, select the
    ``https://www.googleapis.com/auth/calendar`` scope, authorise, and
    exchange the code. It expires in an hour, which is fine for a
    demonstration and useless for anything else.

  * A refresh token with its client id and secret, for the durable path.
    Create an OAuth 2.0 Client ID of type "Desktop app" at
    console.cloud.google.com/apis/credentials, enable the Google Calendar API
    for that project under console.cloud.google.com/apis/library, then
    authorise once and keep the refresh token. This connector exchanges it for
    an access token whenever it needs one.

The exchanged access token is cached on the connector instance for the life of
one request and is never written back to the database. A connector that quietly
persisted a refreshed secret would be writing credentials nobody asked it to
write, and the Integration row is meant to be edited by people, not by code.

WHAT DEMO MODE DOES INSTEAD
---------------------------
Nothing reaches a real calendar. Event ids are ``demo-evt-<hash>`` derived from
the title and the start time, so the same booking simulated twice has the same
id, and a simulated update can find what it is updating. A small set of
existing meetings stands in for a busy week, which gives ``find_availability``
something to work around, so "propose three times for an interview on
Wednesday" is demonstrable with an entirely empty configuration. Every
simulated summary states the parsed local time in full, because the most
common mistake in this whole area is a time that was understood differently
from the way it was meant.
"""

import hashlib
import json
import urllib.error
import urllib.parse
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from . import register
from .base import CallResult, ConfigField, Connector

API = 'https://www.googleapis.com/calendar/v3'
TOKEN_URL = 'https://oauth2.googleapis.com/token'

# The formats ``_parse_when`` accepts, quoted verbatim in the error it raises
# so that a caller who got it wrong is told what right looks like.
ACCEPTED_FORMATS = (
    "ISO 8601 with an offset ('2026-09-15T10:00:00+10:00')",
    "ISO 8601 without one ('2026-09-15T10:00:00'), read in the configured timezone",
    "'YYYY-MM-DD HH:MM'",
    "'YYYY-MM-DD HH:MM:SS'",
    "a bare date ('2026-09-15'), which starts at the beginning of working hours",
)

_NAKED_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S',
                  '%Y-%m-%dT%H:%M', '%d/%m/%Y %H:%M', '%d %b %Y %H:%M')


# ===========================================================================
# The stand-in calendar
# ===========================================================================

# A plausible week for a small company, expressed as weekday plus local time so
# the simulation lands in whatever week is being looked at rather than going
# stale in 2026. Monday is 0.
_DEMO_BUSY = (
    (0, '09:30', 60, 'Weekly leadership stand-up'),
    (0, '14:00', 90, 'Sprint planning'),
    (1, '11:00', 45, 'Customer escalation review'),
    (1, '15:30', 30, 'One to one: Priya Raman'),
    (2, '09:00', 120, 'Product roadmap workshop'),
    (3, '10:00', 45, 'Support triage'),
    (3, '13:00', 60, 'Marketing content review'),
    (4, '11:30', 30, 'Retrospective'),
)


@register
class GoogleCalendarConnector(Connector):
    """Read and write one Google Calendar over the v3 REST API."""

    key = 'google_calendar'
    name = 'Google Calendar'
    description = ('Books interviews, reviews and callbacks, and works out '
                   'which times are actually free before proposing any of them.')
    category = 'calendar'
    icon = 'fa-calendar-days'
    color = '#4285f4'
    docs_url = 'https://developers.google.com/calendar/api/v3/reference'

    config_fields = (
        ConfigField('calendar_id', 'Calendar id', required=True, default='primary',
                    help_text=("'primary' is the account's own calendar. A shared "
                               "calendar's id is on its Settings and sharing page, "
                               'and usually ends in @group.calendar.google.com.')),
        ConfigField('access_token', 'OAuth access token', field_type='password',
                    secret=True,
                    help_text=('The quick path. Mint one at '
                               'developers.google.com/oauthplayground with the '
                               'calendar scope. It expires after about an hour.')),
        ConfigField('refresh_token', 'OAuth refresh token', field_type='password',
                    secret=True,
                    help_text=('The durable path. Used with the client id and '
                               'secret below to obtain a fresh access token '
                               'whenever one is needed.')),
        ConfigField('client_id', 'OAuth client id',
                    help_text=('From an OAuth 2.0 Client ID at '
                               'console.cloud.google.com/apis/credentials.')),
        ConfigField('client_secret', 'OAuth client secret', field_type='password',
                    secret=True),
        ConfigField('timezone_name', 'Timezone', default='Australia/Melbourne',
                    help_text=('An IANA name. Every time given without an offset '
                               'is read in this zone, and every time reported '
                               'back is stated in it.')),
        ConfigField('default_duration_minutes', 'Default meeting length',
                    field_type='number', default=45,
                    help_text='Used when a booking gives a start but no end.'),
        ConfigField('working_hours_start', 'Working hours start', default='09:00',
                    help_text='24-hour local time. Availability never proposes '
                              'anything earlier.'),
        ConfigField('working_hours_end', 'Working hours end', default='17:00',
                    help_text='24-hour local time. Availability never proposes '
                              'anything that would finish later.'),
        ConfigField('default_conference', 'Add a meeting link',
                    field_type='choice', default='google_meet',
                    choices=(('none', 'No meeting link'),
                             ('google_meet', 'Attach a Google Meet link')),
                    help_text='Whether new events get a video link by default.'),
    )

    operations = ('create_event', 'update_event', 'cancel_event', 'list_events',
                  'get_event', 'find_availability')

    def __init__(self, integration):
        super().__init__(integration)
        # Cached for the life of this connector object only -- one request, one
        # exchange. Never written back to the Integration row.
        self._token_cache = ''

    # -- configuration -----------------------------------------------------

    def is_configured(self):
        """A calendar id plus a credential that could actually be used.

        The base implementation would be satisfied by the calendar id alone,
        which defaults to 'primary' and is therefore always present. That would
        make every installation claim to be live and then fail on the first
        call, so the test is tightened here to ask for a token as well.
        """
        if not self.has('calendar_id'):
            return False
        if self.has('access_token'):
            return True
        return all(self.has(key) for key in
                   ('refresh_token', 'client_id', 'client_secret'))

    def missing_settings(self):
        if self.is_configured():
            return []
        missing = []
        if not self.has('calendar_id'):
            missing.append('Calendar id')
        missing.append('OAuth access token, or a refresh token with its '
                       'client id and secret')
        return missing

    def _calendar_id(self):
        return str(self.setting('calendar_id') or 'primary')

    def _tzinfo(self):
        """The zone every naked time is read in and every result is stated in."""
        name = str(self.setting('timezone_name') or '').strip()
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                # A mistyped zone should not stop a booking; Django's own
                # current zone is the sane fallback and the summary still names
                # what was used.
                pass
        return timezone.get_current_timezone()

    def _zone_label(self):
        return str(self.setting('timezone_name') or timezone.get_current_timezone())

    def _duration(self, minutes=None):
        for candidate in (minutes, self.setting('default_duration_minutes')):
            try:
                value = int(candidate)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 45

    def _working_hours(self, earliest='', latest=''):
        """The window availability is allowed to propose inside."""
        start = _parse_clock(earliest) or _parse_clock(self.setting('working_hours_start'))
        end = _parse_clock(latest) or _parse_clock(self.setting('working_hours_end'))
        start = start or time(9, 0)
        end = end or time(17, 0)
        if end <= start:
            end = time(23, 59)
        return start, end

    # -- time parsing ------------------------------------------------------

    def _parse_when(self, value):
        """Turn whatever a caller passed into an aware datetime.

        A time arrives here from three kinds of source: a person typing into a
        form, a language model writing ISO 8601, and another tool passing a
        datetime straight through. All three have to work, and a time that was
        misread is worse than a time that was rejected -- so anything not
        recognised raises a ValueError naming every format that is.
        """
        if value in (None, ''):
            raise ValueError(
                'No date and time was given. Accepted formats: '
                + '; '.join(ACCEPTED_FORMATS) + '.')

        zone = self._tzinfo()

        if isinstance(value, datetime):
            return value if timezone.is_aware(value) else value.replace(tzinfo=zone)

        text = str(value).strip()

        # A bare date is legitimate and common: "book it on the fifteenth". It
        # has to be caught before the ISO parser, because fromisoformat happily
        # reads '2026-09-16' as midnight -- and midnight is never what anybody
        # meant by a bare date. It starts at the beginning of working hours
        # instead, and the summary always states the time that was chosen.
        for pattern in ('%Y-%m-%d', '%d/%m/%Y', '%d %b %Y', '%d %B %Y'):
            try:
                day = datetime.strptime(text, pattern)
            except ValueError:
                continue
            opens, _closes = self._working_hours()
            return day.replace(hour=opens.hour, minute=opens.minute, tzinfo=zone)

        # ISO 8601, including the trailing Z that Google itself returns.
        candidate = text[:-1] + '+00:00' if text.endswith(('Z', 'z')) else text
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parsed = None

        if parsed is None:
            for pattern in _NAKED_FORMATS:
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue

        if parsed is None:
            raise ValueError(
                f'"{text}" is not a date and time I can read. Accepted formats: '
                + '; '.join(ACCEPTED_FORMATS) + '.')

        return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)

    def _local(self, moment):
        return moment.astimezone(self._tzinfo())

    def _spoken(self, start, end):
        """'Tue 15 Sep 2026 10:00-10:45 (Australia/Melbourne)'."""
        local_start, local_end = self._local(start), self._local(end)
        return (f'{local_start.strftime("%a %d %b %Y %H:%M")}-'
                f'{local_end.strftime("%H:%M")} ({self._zone_label()})')

    # -- tokens ------------------------------------------------------------

    def _access_token(self):
        """A usable bearer token, exchanging the refresh token if need be.

        The stored access token wins when there is one, because an installation
        that pasted a short-lived token from the playground means it to be
        used. Otherwise the refresh token is exchanged, once, and the result is
        held on this instance.
        """
        stored = str(self.setting('access_token') or '').strip()
        if stored:
            return stored

        if self._token_cache:
            return self._token_cache

        refresh = str(self.setting('refresh_token') or '').strip()
        client_id = str(self.setting('client_id') or '').strip()
        client_secret = str(self.setting('client_secret') or '').strip()
        if not (refresh and client_id and client_secret):
            raise RuntimeError(
                'No usable Google credential. Add an OAuth access token, or a '
                'refresh token with its client id and secret, on the '
                'Integrations page.')

        try:
            _status, body = self.request_json(
                TOKEN_URL, method='POST', form=True,
                payload={'client_id': client_id, 'client_secret': client_secret,
                         'refresh_token': refresh, 'grant_type': 'refresh_token'})
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_token_error(exc)) from exc

        token = (body or {}).get('access_token', '')
        if not token:
            raise RuntimeError(
                'Google accepted the refresh request but returned no access '
                'token. Check that the Calendar API is enabled for this OAuth '
                'client and that the refresh token has not been revoked.')
        self._token_cache = token
        return token

    def _headers(self):
        return {'Authorization': f'Bearer {self._access_token()}'}

    def _events_url(self, event_id=''):
        base = f'{API}/calendars/{urllib.parse.quote(self._calendar_id(), safe="")}/events'
        return f'{base}/{urllib.parse.quote(str(event_id), safe="")}' if event_id else base

    def _call(self, url, *, method='GET', payload=None, params=None):
        """One Calendar API call, with Google's error body made readable."""
        try:
            return self.request_json(url, method=method, headers=self._headers(),
                                     payload=payload, params=params)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_api_error(exc)) from exc

    # -- create ------------------------------------------------------------

    def _wants_conference(self, add_conference=None):
        if add_conference is None:
            return str(self.setting('default_conference') or '') == 'google_meet'
        if isinstance(add_conference, str):
            return add_conference.strip().lower() in ('1', 'true', 'yes', 'google_meet', 'meet')
        return bool(add_conference)

    def _window(self, start, end=None, duration_minutes=None):
        """Start and end as aware datetimes, whichever of the two was given."""
        begins = self._parse_when(start)
        if end:
            finishes = self._parse_when(end)
            if finishes <= begins:
                raise ValueError('The end of the event is not after its start.')
            return begins, finishes
        return begins, begins + timedelta(minutes=self._duration(duration_minutes))

    def live_create_event(self, title, start, end=None, duration_minutes=None,
                          attendees=None, description='', location='',
                          add_conference=None):
        begins, finishes = self._window(start, end, duration_minutes)
        guests = _emails(attendees)
        conference = self._wants_conference(add_conference)

        payload = {
            'summary': title or 'Untitled meeting',
            'description': description or '',
            'start': {'dateTime': begins.isoformat(), 'timeZone': self._zone_label()},
            'end': {'dateTime': finishes.isoformat(), 'timeZone': self._zone_label()},
        }
        if location:
            payload['location'] = location
        if guests:
            payload['attendees'] = [{'email': address} for address in guests]
        if conference:
            payload['conferenceData'] = {'createRequest': {
                'requestId': _request_id(title, begins),
                'conferenceSolutionKey': {'type': 'hangoutsMeet'},
            }}

        params = {'sendUpdates': 'all' if guests else 'none'}
        if conference:
            # Google ignores conferenceData entirely without this parameter,
            # and reports no error while doing so.
            params['conferenceDataVersion'] = 1

        _status, body = self._call(self._events_url(), method='POST',
                                   payload=payload, params=params)
        return self.ok(
            f'Booked "{payload["summary"]}" for {self._spoken(begins, finishes)}'
            + (f' with {len(guests)} attendee{"" if len(guests) == 1 else "s"}.'
               if guests else '.'),
            _shape_event(body, guests, self._local))

    def demo_create_event(self, title, start, end=None, duration_minutes=None,
                          attendees=None, description='', location='',
                          add_conference=None):
        begins, finishes = self._window(start, end, duration_minutes)
        guests = _emails(attendees)
        event_id = _demo_event_id(title, begins)
        conference = self._wants_conference(add_conference)
        return self.simulated(
            f'Would book "{title}" for {self._spoken(begins, finishes)}'
            + (f' with {len(guests)} attendee{"" if len(guests) == 1 else "s"}'
               if guests else '')
            + '. Nothing was added to a real calendar.',
            {'event_id': event_id,
             'html_link': f'https://calendar.google.com/calendar/event?eid={event_id}',
             'meeting_link': (f'https://meet.google.com/{_meet_code(event_id)}'
                              if conference else ''),
             'title': title,
             'start': begins.isoformat(),
             'end': finishes.isoformat(),
             'start_display': self._local(begins).strftime('%a %d %b %Y %H:%M'),
             'timezone': self._zone_label(),
             'attendees': guests,
             'description': description,
             'location': location,
             'calendar_id': self._calendar_id(),
             'simulated': True})

    # -- update ------------------------------------------------------------

    def live_update_event(self, event_id, **fields):
        """PATCH the fields that were named, and only those.

        A PATCH rather than a PUT because a reschedule should not silently drop
        the description, the attendees or the meeting link that somebody else
        put on the event.
        """
        payload, changed = self._patch_body(event_id, fields)
        if not payload:
            return self.failure(
                'Nothing to change. update_event accepts start, end, '
                'duration_minutes, title, description, location and attendees.')

        _status, body = self._call(self._events_url(event_id), method='PATCH',
                                   payload=payload,
                                   params={'sendUpdates': 'all'})
        shaped = _shape_event(body, _emails(fields.get('attendees')), self._local)
        return self.ok(f'Updated {", ".join(changed)} on "{shaped["title"]}".',
                       shaped)

    def _patch_body(self, event_id, fields):
        """The PATCH body, plus a readable list of what is being changed."""
        payload, changed = {}, []

        if fields.get('title'):
            payload['summary'] = fields['title']
            changed.append('the title')
        if 'description' in fields and fields['description'] is not None:
            payload['description'] = fields['description']
            changed.append('the description')
        if 'location' in fields and fields['location'] is not None:
            payload['location'] = fields['location']
            changed.append('the location')
        if fields.get('attendees') is not None:
            payload['attendees'] = [{'email': address}
                                    for address in _emails(fields['attendees'])]
            changed.append('the attendees')

        start, end = fields.get('start'), fields.get('end')
        minutes = fields.get('duration_minutes')

        if start:
            begins = self._parse_when(start)
            payload['start'] = {'dateTime': begins.isoformat(),
                                'timeZone': self._zone_label()}
            if end:
                finishes = self._parse_when(end)
            else:
                finishes = begins + timedelta(minutes=self._duration(minutes))
            payload['end'] = {'dateTime': finishes.isoformat(),
                              'timeZone': self._zone_label()}
            changed.append(f'the time to {self._spoken(begins, finishes)}')
        elif end:
            finishes = self._parse_when(end)
            payload['end'] = {'dateTime': finishes.isoformat(),
                              'timeZone': self._zone_label()}
            changed.append('the end time')
        elif minutes:
            # Only a new length was given, so the existing start has to be read
            # back before the new end can be worked out.
            existing = self._existing_start(event_id)
            if existing is not None:
                finishes = existing + timedelta(minutes=self._duration(minutes))
                payload['end'] = {'dateTime': finishes.isoformat(),
                                  'timeZone': self._zone_label()}
                changed.append(f'the length to {self._duration(minutes)} minutes')

        return payload, changed

    def _existing_start(self, event_id):
        _status, body = self._call(self._events_url(event_id))
        raw = ((body or {}).get('start') or {}).get('dateTime') or ''
        if not raw:
            return None
        try:
            return self._parse_when(raw)
        except ValueError:
            return None

    def demo_update_event(self, event_id, **fields):
        described = []
        start = fields.get('start')
        if start:
            begins = self._parse_when(start)
            finishes = (self._parse_when(fields['end']) if fields.get('end')
                        else begins + timedelta(
                            minutes=self._duration(fields.get('duration_minutes'))))
            described.append(f'the time to {self._spoken(begins, finishes)}')
            fields = dict(fields, start=begins.isoformat(), end=finishes.isoformat())
        for key in ('title', 'description', 'location', 'attendees'):
            if fields.get(key) is not None:
                described.append(f'the {key}')

        return self.simulated(
            f'Would change {", ".join(described) or "nothing"} on event '
            f'{event_id}. No real calendar was touched.',
            {'event_id': event_id, 'changes': {key: value for key, value
                                               in fields.items() if value is not None},
             'html_link': f'https://calendar.google.com/calendar/event?eid={event_id}',
             'simulated': True})

    # -- cancel ------------------------------------------------------------

    def live_cancel_event(self, event_id):
        """Delete the event, notifying the attendees.

        Google returns 204 with an empty body, so there is nothing to report
        back beyond the id that no longer exists.
        """
        self._call(self._events_url(event_id), method='DELETE',
                   params={'sendUpdates': 'all'})
        return self.ok(
            f'Cancelled event {event_id} on {self._calendar_id()}. '
            'Attendees were notified.',
            {'event_id': event_id, 'calendar_id': self._calendar_id(),
             'cancelled': True})

    def demo_cancel_event(self, event_id):
        return self.simulated(
            f'Would cancel event {event_id} and notify its attendees. '
            'Nothing was removed from a real calendar.',
            {'event_id': event_id, 'calendar_id': self._calendar_id(),
             'cancelled': True, 'simulated': True})

    # -- read --------------------------------------------------------------

    def live_list_events(self, start=None, end=None, limit=20):
        window_start, window_end = self._listing_window(start, end)
        _status, body = self._call(self._events_url(), params={
            'timeMin': window_start.isoformat(),
            'timeMax': window_end.isoformat(),
            'singleEvents': 'true',
            'orderBy': 'startTime',
            'maxResults': _clamp(limit, 1, 250),
        })

        events = [_shape_event(row, [], self._local)
                  for row in (body or {}).get('items') or []]
        return self.ok(
            f'{len(events)} event{"" if len(events) == 1 else "s"} on '
            f'{(body or {}).get("summary") or self._calendar_id()} between '
            f'{self._local(window_start).strftime("%d %b %H:%M")} and '
            f'{self._local(window_end).strftime("%d %b %H:%M")} '
            f'({self._zone_label()}).',
            {'events': events, 'count': len(events),
             'calendar_id': self._calendar_id(),
             'from': window_start.isoformat(), 'to': window_end.isoformat()})

    def _listing_window(self, start=None, end=None):
        begins = self._parse_when(start) if start else timezone.now()
        if end:
            finishes = self._parse_when(end)
        else:
            finishes = begins + timedelta(days=7)
        if finishes <= begins:
            finishes = begins + timedelta(days=1)
        return begins, finishes

    def demo_list_events(self, start=None, end=None, limit=20):
        window_start, window_end = self._listing_window(start, end)
        events = self._demo_events(window_start, window_end)[:_clamp(limit, 1, 250)]
        return self.simulated(
            f'{len(events)} event{"" if len(events) == 1 else "s"} in the '
            f'stand-in calendar between '
            f'{self._local(window_start).strftime("%d %b %H:%M")} and '
            f'{self._local(window_end).strftime("%d %b %H:%M")} '
            f'({self._zone_label()}). This is not a real calendar.',
            {'events': events, 'count': len(events),
             'calendar_id': self._calendar_id(),
             'from': window_start.isoformat(), 'to': window_end.isoformat(),
             'simulated': True})

    def live_get_event(self, event_id):
        _status, body = self._call(self._events_url(event_id))
        shaped = _shape_event(body, [], self._local)
        return self.ok(
            f'"{shaped["title"]}" on {shaped["start_display"]} '
            f'({self._zone_label()})'
            + (f', {len(shaped["attendees"])} attendees.'
               if shaped['attendees'] else '.'),
            shaped)

    def demo_get_event(self, event_id):
        window = timezone.now()
        for event in self._demo_events(window - timedelta(days=7),
                                       window + timedelta(days=21)):
            if event['event_id'] == str(event_id):
                return self.simulated(
                    f'Simulated event "{event["title"]}" on '
                    f'{event["start_display"]} ({self._zone_label()}).',
                    dict(event, simulated=True))

        # A demo failure rather than a cheerful empty answer, and flagged as a
        # demo so nothing above mistakes it for the real calendar refusing.
        return CallResult(
            ok=False, demo=True, provider=self.key,
            error=(f'No simulated event with id {event_id} in the stand-in '
                   'calendar. Ids returned by list_events can be read back; an '
                   'id from a simulated booking is not stored anywhere, because '
                   'a simulation deliberately keeps no state.'),
            data={'event_id': event_id, 'simulated': True})

    # -- availability ------------------------------------------------------

    def live_find_availability(self, duration_minutes=None, days_ahead=7,
                               earliest='', latest=''):
        """Free slots inside working hours, worked out from real events."""
        minutes = self._duration(duration_minutes)
        days = _clamp(days_ahead, 1, 60)
        window_start = timezone.now()
        window_end = window_start + timedelta(days=days)

        _status, body = self._call(self._events_url(), params={
            'timeMin': window_start.isoformat(),
            'timeMax': window_end.isoformat(),
            'singleEvents': 'true',
            'orderBy': 'startTime',
            'maxResults': 250,
        })

        busy = []
        for row in (body or {}).get('items') or []:
            if (row.get('status') or '') == 'cancelled':
                continue
            begins = _google_time(row.get('start') or {})
            finishes = _google_time(row.get('end') or {})
            if begins and finishes:
                busy.append((begins, finishes))

        slots = self._slots(busy, minutes, days, earliest, latest)
        opens, closes = self._working_hours(earliest, latest)
        return self.ok(
            f'{len(slots)} free slot{"" if len(slots) == 1 else "s"} of '
            f'{minutes} minutes in the next {days} days, inside '
            f'{opens.strftime("%H:%M")}-{closes.strftime("%H:%M")} '
            f'{self._zone_label()} on weekdays, around '
            f'{len(busy)} existing event{"" if len(busy) == 1 else "s"}.',
            {'slots': slots, 'count': len(slots), 'duration_minutes': minutes,
             'timezone': self._zone_label(), 'busy_events': len(busy),
             'working_hours': f'{opens.strftime("%H:%M")}-{closes.strftime("%H:%M")}'})

    def demo_find_availability(self, duration_minutes=None, days_ahead=7,
                               earliest='', latest=''):
        """The same arithmetic, against the stand-in week.

        This is the one demo half in the project that carries real weight. It
        needs no credential, no network and no configuration beyond the working
        hours, which is what makes "propose three times for an interview"
        answerable on a fresh installation.
        """
        minutes = self._duration(duration_minutes)
        days = _clamp(days_ahead, 1, 60)
        window_start = timezone.now()
        busy = [(begins, finishes) for begins, finishes, _title
                in self._demo_busy(window_start, window_start + timedelta(days=days))]

        slots = self._slots(busy, minutes, days, earliest, latest)
        opens, closes = self._working_hours(earliest, latest)
        return self.simulated(
            f'{len(slots)} free slot{"" if len(slots) == 1 else "s"} of '
            f'{minutes} minutes in the next {days} days, inside '
            f'{opens.strftime("%H:%M")}-{closes.strftime("%H:%M")} '
            f'{self._zone_label()} on weekdays, worked out around '
            f'{len(busy)} meetings in the stand-in calendar. The working hours '
            'are real configuration; the meetings are not.',
            {'slots': slots, 'count': len(slots), 'duration_minutes': minutes,
             'timezone': self._zone_label(), 'busy_events': len(busy),
             'working_hours': f'{opens.strftime("%H:%M")}-{closes.strftime("%H:%M")}',
             'simulated': True})

    def _slots(self, busy, minutes, days, earliest='', latest=''):
        """Gaps of at least ``minutes`` inside working hours, weekdays only.

        Shared by both halves of ``find_availability`` deliberately: the demo
        must not be a different algorithm from the live path, or the free times
        it proposes would stop being believable.
        """
        zone = self._tzinfo()
        opens, closes = self._working_hours(earliest, latest)
        now = timezone.now().astimezone(zone)
        step = timedelta(minutes=minutes)

        # Normalise the busy list into the local zone once, then sort.
        occupied = sorted((begins.astimezone(zone), finishes.astimezone(zone))
                          for begins, finishes in busy)

        slots = []
        for offset in range(days + 1):
            day = (now + timedelta(days=offset)).date()
            if day.weekday() >= 5:                       # Saturday and Sunday
                continue

            cursor = datetime.combine(day, opens, tzinfo=zone)
            day_end = datetime.combine(day, closes, tzinfo=zone)
            if cursor < now:
                cursor = _round_up(now, minutes, zone)

            while cursor + step <= day_end:
                finish = cursor + step
                clash = next(((begins, ends) for begins, ends in occupied
                              if begins < finish and ends > cursor), None)
                if clash is None:
                    slots.append({
                        'start': cursor.isoformat(),
                        'end': finish.isoformat(),
                        'label': (f'{cursor.strftime("%a %d %b")} '
                                  f'{cursor.strftime("%H:%M")}-'
                                  f'{finish.strftime("%H:%M")}'),
                    })
                    cursor = finish
                else:
                    # Jump to the end of whatever is in the way rather than
                    # crawling forward in one-slot steps.
                    cursor = max(clash[1], cursor + timedelta(minutes=15))
                if len(slots) >= 24:
                    return slots
        return slots

    # -- the stand-in calendar --------------------------------------------

    def _demo_busy(self, window_start, window_end):
        """The simulated meetings that fall inside a window."""
        zone = self._tzinfo()
        first = window_start.astimezone(zone).date() - timedelta(
            days=window_start.astimezone(zone).weekday())
        out = []
        for week in range(0, 8):
            monday = first + timedelta(weeks=week)
            for weekday, clock, minutes, title in _DEMO_BUSY:
                day = monday + timedelta(days=weekday)
                begins = datetime.combine(day, _parse_clock(clock) or time(9, 0),
                                          tzinfo=zone)
                finishes = begins + timedelta(minutes=minutes)
                if finishes <= window_start or begins >= window_end:
                    continue
                out.append((begins, finishes, title))
        return sorted(out)

    def _demo_events(self, window_start, window_end):
        """The same meetings, in the shape ``list_events`` returns."""
        events = []
        for begins, finishes, title in self._demo_busy(window_start, window_end):
            event_id = _demo_event_id(title, begins)
            events.append({
                'event_id': event_id,
                'title': title,
                'start': begins.isoformat(),
                'end': finishes.isoformat(),
                'start_display': begins.strftime('%a %d %b %Y %H:%M'),
                'end_display': finishes.strftime('%H:%M'),
                'timezone': self._zone_label(),
                'attendees': [],
                'location': '',
                'description': '',
                'status': 'confirmed',
                'html_link': f'https://calendar.google.com/calendar/event?eid={event_id}',
                'meeting_link': f'https://meet.google.com/{_meet_code(event_id)}',
                'organiser': 'stand-in calendar',
            })
        return events

    # -- probe -------------------------------------------------------------

    def live_probe(self):
        """Read the calendar's own metadata: cheapest proof the token works."""
        try:
            _status, body = self._call(
                f'{API}/calendars/'
                f'{urllib.parse.quote(self._calendar_id(), safe="")}')
        except RuntimeError as exc:
            return self.failure(str(exc))

        summary = (body or {}).get('summary') or self._calendar_id()
        zone = (body or {}).get('timeZone') or 'no timezone reported'
        configured = self._zone_label()
        note = ''
        if zone and configured and zone != configured:
            note = (f' The calendar runs in {zone} while this integration is '
                    f'configured for {configured}; times are read and reported '
                    f'in {configured}.')
        return self.ok(
            f'Connected to the calendar "{summary}" ({zone}). Default meeting '
            f'length {self._duration()} minutes.{note}',
            {'calendar_id': self._calendar_id(), 'summary': summary,
             'calendar_timezone': zone, 'configured_timezone': configured})


# ===========================================================================
# helpers
# ===========================================================================

def _parse_clock(value):
    """'09:00' or '9' or '17:30' as a time, or None."""
    text = str(value or '').strip()
    if not text:
        return None
    for pattern in ('%H:%M', '%H:%M:%S', '%H', '%I%p', '%I:%M%p'):
        try:
            return datetime.strptime(text.upper().replace(' ', ''), pattern).time()
        except ValueError:
            continue
    return None


def _round_up(moment, minutes, zone):
    """The next clean boundary at or after ``moment``.

    Availability that starts at 10:07 is technically free and practically
    useless, so slots begin on a sensible multiple of the meeting length.
    """
    local = moment.astimezone(zone)
    block = max(15, min(int(minutes), 60))
    surplus = (local.minute % block)
    bumped = local.replace(second=0, microsecond=0)
    if surplus or local.second or local.microsecond:
        bumped += timedelta(minutes=block - surplus)
    return bumped


def _emails(value):
    """Attendees as a plain list of addresses.

    Callers pass a comma-separated string, a list of strings, or a list of
    ``{'email': ...}`` dictionaries depending on where the call came from.
    """
    if not value:
        return []
    if isinstance(value, dict):
        address = value.get('email') or value.get('address') or ''
        return [address.strip()] if address else []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_emails(item))
        return out
    return [part.strip() for part in str(value).replace(';', ',').split(',')
            if part.strip()]


def _google_time(block):
    """A Google start/end block as an aware datetime, or None.

    An all-day event has ``date`` rather than ``dateTime``; it still occupies
    the day, so it is treated as busy from midnight to midnight.
    """
    raw = (block or {}).get('dateTime') or ''
    if raw:
        candidate = raw[:-1] + '+00:00' if raw.endswith(('Z', 'z')) else raw
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else timezone.make_aware(parsed)

    day = (block or {}).get('date') or ''
    if day:
        try:
            parsed = datetime.strptime(day, '%Y-%m-%d')
        except ValueError:
            return None
        return timezone.make_aware(parsed)
    return None


def _shape_event(body, fallback_attendees, localise):
    """One Google event, reduced to this connector's contract."""
    body = body or {}
    begins = _google_time(body.get('start') or {})
    finishes = _google_time(body.get('end') or {})
    attendees = [row.get('email', '') for row in body.get('attendees') or []
                 if row.get('email')]

    meeting_link = body.get('hangoutLink') or ''
    if not meeting_link:
        for entry in ((body.get('conferenceData') or {}).get('entryPoints') or []):
            if entry.get('entryPointType') == 'video' and entry.get('uri'):
                meeting_link = entry['uri']
                break

    return {
        'event_id': body.get('id', ''),
        'title': body.get('summary', '') or '(no title)',
        'html_link': body.get('htmlLink', ''),
        'meeting_link': meeting_link,
        'start': begins.isoformat() if begins else '',
        'end': finishes.isoformat() if finishes else '',
        'start_display': (localise(begins).strftime('%a %d %b %Y %H:%M')
                          if begins else ''),
        'end_display': localise(finishes).strftime('%H:%M') if finishes else '',
        'attendees': attendees or list(fallback_attendees or []),
        'location': body.get('location', ''),
        'description': body.get('description', ''),
        'status': body.get('status', ''),
        'organiser': (body.get('organizer') or {}).get('email', ''),
    }


def _request_id(title, begins):
    """A stable conferenceData request id.

    Google uses it to make the request idempotent, so deriving it from the
    booking means retrying a create does not produce two Meet links.
    """
    digest = hashlib.sha1(f'{title}|{begins.isoformat()}'.encode('utf-8'))
    return f'aiwo-{digest.hexdigest()[:16]}'


def _demo_event_id(title, begins):
    digest = hashlib.sha1(f'{title}|{begins.isoformat()}'.encode('utf-8'))
    return f'demo-evt-{digest.hexdigest()[:10]}'


def _meet_code(seed):
    """A Meet-shaped code, xxx-xxxx-xxx, derived from the event id."""
    letters = 'abcdefghijkmnopqrstuvwxyz'
    digest = hashlib.sha1(str(seed).encode('utf-8')).hexdigest()
    picked = [letters[int(digest[index * 2:index * 2 + 2], 16) % len(letters)]
              for index in range(10)]
    body = ''.join(picked)
    return f'{body[:3]}-{body[3:7]}-{body[7:10]}'


def _clamp(value, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(number, high))


def _api_error(exc):
    """Google's JSON error body, turned into one readable sentence."""
    try:
        raw = exc.read().decode('utf-8', errors='replace')
    except Exception:                                              # noqa: BLE001
        raw = ''
    message = ''
    try:
        detail = json.loads(raw) if raw else {}
        message = ((detail.get('error') or {}).get('message')
                   if isinstance(detail.get('error'), dict) else '') or ''
    except json.JSONDecodeError:
        message = raw[:200]

    if exc.code == 401:
        return ('Google rejected the credential (401). An access token from the '
                'OAuth playground lasts about an hour; add a refresh token with '
                f'its client id and secret for something durable. {message}').strip()
    if exc.code == 403:
        return ('Google refused the request (403). Either the Calendar API is '
                'not enabled for this OAuth client, or the token was granted '
                f'without the calendar scope. {message}').strip()
    if exc.code == 404:
        return ('Google could not find that calendar or event (404). Check the '
                'calendar id, and that the account behind the token can see '
                f'it. {message}').strip()
    if exc.code == 429:
        return 'Google is rate limiting this client (429). Try again shortly.'
    return f'Google Calendar returned HTTP {exc.code}. {message}'.strip()


def _token_error(exc):
    """The same courtesy for a failed refresh-token exchange."""
    try:
        raw = exc.read().decode('utf-8', errors='replace')
    except Exception:                                              # noqa: BLE001
        raw = ''
    code = ''
    try:
        code = (json.loads(raw) if raw else {}).get('error', '')
    except json.JSONDecodeError:
        code = raw[:200]

    if code == 'invalid_grant':
        return ('Google rejected the refresh token (invalid_grant). It has been '
                'revoked, or it belongs to a different OAuth client than the '
                'client id and secret configured here. Authorise once more and '
                'store the new refresh token.')
    if code == 'invalid_client':
        return ('Google rejected the OAuth client id or secret '
                '(invalid_client). Copy both again from '
                'console.cloud.google.com/apis/credentials.')
    return f'The token exchange failed: HTTP {exc.code} {code}'.strip()
