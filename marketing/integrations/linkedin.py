"""LinkedIn: the Marketing employee's professional publishing channel.

WHAT THIS DOES FOR THE WORKFORCE
--------------------------------
LinkedIn is where a company's professional voice lives, which makes it the
highest-consequence integration in the platform: a post cannot be unseen, and
the audience includes customers, candidates and competitors. For that reason
publishing is never something an AI employee does on its own. The Marketing
employee drafts, the draft becomes a proposed action carrying the exact text,
a person reads it, edits it if they wish, and approves it. Only then does this
connector post. The connector is the hands; the judgement stays human.

WHICH EMPLOYEES USE IT
----------------------
``Marketing``  drafts and publishes company posts, and reads impressions and
               reactions on previous posts to decide what to write next.
``HR``         publishes role announcements from an approved job opening.
``Research``   reads the company page and previous posts for context, so a
               draft does not contradict what has already been said.

THE CREDENTIAL YOU NEED
-----------------------
An OAuth access token from a LinkedIn developer application, plus the URN of
whoever is posting.

  1. Create an application at linkedin.com/developers and associate it with
     the company page you intend to post from.
  2. Request the ``w_member_social`` product, and for a company page the
     Community Management API. Both go through LinkedIn's review, which takes
     days rather than minutes -- this delay is the honest reason demo mode
     exists.
  3. Complete the OAuth flow and copy the access token into the Access token
     field on the Integrations page.
  4. Set the Author URN: ``urn:li:person:XXXX`` to post as a profile, or
     ``urn:li:organization:12345`` to post as a company page. The
     organisation id is the number in the page's admin URL.

Access tokens are typically valid for sixty days, so expect to repeat step
three periodically. When the token expires every call returns 401 and the
Integrations page will say so.

WHAT LINKEDIN'S API CANNOT DO
-----------------------------
Two limits are real and are reported rather than papered over.

``schedule_post``  The UGC Posts API has no future publish time. A post is
                   created published or created as a draft, and nothing in the
                   API will publish a draft later. The live path therefore
                   returns a clear failure explaining that the platform will
                   hold the text and prompt at the requested time instead.
                   Demo mode simulates that hold.

``search_people``  There is no general people-search API. It exists only
                   inside partner programmes such as Talent Solutions, and is
                   not available to a normal developer application. The live
                   path says so rather than inventing plausible strangers,
                   which would be a fabrication about real people.

WHAT DEMO MODE DOES INSTEAD
---------------------------
Without a token and an author URN every operation is simulated: a post urn
derived from a stable hash of the text (so the same draft always yields the
same urn), a plausible permalink, a small company profile, and five previous
posts with impression, reaction, comment and click figures so the Marketing
employee's insight tools have something to read. Every simulated result is
flagged, and the 3000 character limit is enforced in demo mode too -- a draft
that would be rejected live should be rejected in rehearsal.
"""

import hashlib
import urllib.error

from . import register
from .base import CallResult, ConfigField, Connector

API_BASE = 'https://api.linkedin.com'
UGC_LIMIT = 3000


def _stable(text, low, high):
    digest = hashlib.sha256(str(text).encode('utf-8')).hexdigest()
    return low + int(digest[:12], 16) % max(1, (high - low + 1))


DEMO_PROFILE = {
    'name': 'Acme Platform',
    'headline': 'Marketing and operations software for teams that answer for their work.',
    'urn': 'urn:li:organization:70418362',
    'followers': 4812,
    'employees': 74,
    'industry': 'Software Development',
    'location': 'Melbourne, Victoria, Australia',
    'website': 'https://acme.example.com',
    'vanity': 'acme-platform',
}

DEMO_POSTS = (
    {'slug': 'streaming-exports', 'published': '2026-09-01T09:15:00Z',
     'impressions': 8420, 'reactions': 214, 'comments': 19, 'shares': 11,
     'clicks': 386,
     'text': ('Exports used to build the whole file in memory before writing a '
              'byte. That works until a customer has fifty thousand rows, and '
              'then it fails in the least helpful way possible: a gateway '
              'timeout with no error message. We rewrote it to stream. Here is '
              'what we learned about finding the failure mode your own tests '
              'never reach.')},
    {'slug': 'audit-trail-argument', 'published': '2026-08-25T08:30:00Z',
     'impressions': 12960, 'reactions': 431, 'comments': 47, 'shares': 38,
     'clicks': 902,
     'text': ('"Who approved this?" is the question that decides whether '
              'automation is allowed near anything that matters. If the answer '
              'is a name, a timestamp and the exact text they saw, the '
              'conversation is short. If it is "the system did it", the '
              'conversation is over.')},
    {'slug': 'hiring-two-engineers', 'published': '2026-08-18T10:00:00Z',
     'impressions': 6180, 'reactions': 152, 'comments': 24, 'shares': 9,
     'clicks': 1140,
     'text': ('We are hiring two engineers in Melbourne. No take-home '
              'exercise, no whiteboard algorithms. You will pair with someone '
              'on a real ticket from our real backlog for ninety minutes, and '
              'we pay you for your time.')},
    {'slug': 'postmortem-culture', 'published': '2026-08-11T09:45:00Z',
     'impressions': 9740, 'reactions': 287, 'comments': 33, 'shares': 26,
     'clicks': 441,
     'text': ('Our alerting did not catch last month\'s outage. A customer '
              'told us. The uncomfortable finding was not the bug -- it was '
              'that gateway errors never reached the alert we had been trusting '
              'for two years. Publishing the postmortem internally within a '
              'week is the only reason we found that out.')},
    {'slug': 'no-friday-releases', 'published': '2026-08-04T08:00:00Z',
     'impressions': 15230, 'reactions': 604, 'comments': 88, 'shares': 71,
     'clicks': 512,
     'text': ('We do not release on Fridays. Not because Friday code is worse, '
              'but because every incident we have had that took more than an '
              'hour to resolve began with a release nobody was awake to '
              'watch.')},
)


@register
class LinkedInConnector(Connector):
    """Publishing and reading on LinkedIn through the UGC Posts API."""

    key = 'linkedin'
    name = 'LinkedIn'
    description = ('Publishes approved company posts and reads the performance '
                   'of previous ones.')
    category = 'social'
    icon = 'fa-linkedin'
    color = '#0a66c2'
    docs_url = 'https://learn.microsoft.com/en-us/linkedin/marketing/'

    config_fields = (
        ConfigField(
            'access_token', 'Access token',
            help_text=('A member or organisation token with w_member_social. '
                       'Get it from a LinkedIn developer app; the review '
                       'process is why demo mode exists.'),
            field_type='password', required=True, secret=True),
        ConfigField(
            'author_urn', 'Author URN',
            help_text=('urn:li:person:XXXX for a profile, or '
                       'urn:li:organization:12345 for a company page.'),
            required=True, placeholder='urn:li:organization:12345'),
        ConfigField(
            'default_visibility', 'Default visibility',
            help_text=('PUBLIC is visible to anyone including search engines. '
                       'CONNECTIONS restricts it to the author\'s network and '
                       'is meaningless for a company page.'),
            field_type='choice', choices=('PUBLIC', 'CONNECTIONS'),
            default='PUBLIC'),
    )

    operations = ('publish_post', 'schedule_post', 'get_profile', 'list_posts',
                  'search_people', 'get_company_page')

    # -- plumbing ----------------------------------------------------------

    def _headers(self):
        return {
            'Authorization': f'Bearer {self.setting("access_token")}',
            'X-Restli-Protocol-Version': '2.0.0',
            'LinkedIn-Version': '202405',
            'Accept': 'application/json',
        }

    def _author(self):
        return str(self.setting('author_urn', '') or '').strip()

    def _visibility(self, visibility):
        wanted = (str(visibility or '').strip().upper()
                  or str(self.setting('default_visibility', 'PUBLIC')).upper())
        return wanted if wanted in ('PUBLIC', 'CONNECTIONS') else 'PUBLIC'

    def _api(self, path, *, method='GET', payload=None, params=None):
        try:
            status, data = self.request_json(
                f'{API_BASE}{path}', method=method, headers=self._headers(),
                payload=payload, params=params)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(self._advise(exc, path)) from exc
        return status, data

    def _advise(self, exc, path):
        try:
            body = exc.read().decode('utf-8', errors='replace')[:700]
        except Exception:  # noqa: BLE001
            body = ''
        code = exc.code
        if code == 401:
            return ('LinkedIn rejected the access token (401). LinkedIn tokens '
                    'expire after about sixty days, so the usual cause is age '
                    'rather than a mistake. Run the OAuth flow in your '
                    'developer application again and paste the new token into '
                    'the LinkedIn integration.')
        if code == 403:
            return ('LinkedIn refused the request (403). The token is valid but '
                    'lacks the permission. Posting needs w_member_social, and '
                    'posting as a company page also needs the Community '
                    'Management API approved on the application and the '
                    'authorising member to be an administrator of that page. '
                    f'LinkedIn said: {body}')
        if code == 404:
            return (f'LinkedIn returned 404 for {path}. Check the Author URN: '
                    'it must be the full urn, either urn:li:person:XXXX or '
                    'urn:li:organization:12345, not a bare id and not a profile '
                    'URL.')
        if code == 422:
            return ('LinkedIn rejected the post content (422). This usually '
                    'means the author urn does not match the token, or the '
                    f'share body is malformed. LinkedIn said: {body}')
        if code == 429:
            return ('LinkedIn is throttling this application (429). Daily post '
                    'quotas are low. Wait and try again; nothing was '
                    'published.')
        return f'LinkedIn returned HTTP {code} {exc.reason}. {body}'.strip()

    def _permalink(self, urn):
        identifier = str(urn or '').split(':')[-1]
        return f'https://www.linkedin.com/feed/update/urn:li:share:{identifier}/'

    # =======================================================================
    # Live
    # =======================================================================

    def live_publish_post(self, text='', visibility='', media=None):
        body = str(text or '').strip()
        if not body:
            return self.failure('A LinkedIn post needs some text.')
        if len(body) > UGC_LIMIT:
            return self.failure(
                (f'The post is {len(body)} characters and LinkedIn rejects '
                 f'anything over {UGC_LIMIT}. Trim {len(body) - UGC_LIMIT} '
                 'character(s) before publishing. Nothing was sent, so no '
                 'partial post exists.'),
                {'length': len(body), 'limit': UGC_LIMIT,
                 'over_by': len(body) - UGC_LIMIT})

        author = self._author()
        if not author.startswith('urn:li:'):
            return self.failure(
                'The Author URN is not a LinkedIn urn. It must look like '
                'urn:li:person:XXXX or urn:li:organization:12345. Set it on the '
                'Integrations page.')
        if media:
            return self.failure(
                'Publishing with media is not implemented here. LinkedIn '
                'requires a three step upload -- register the upload, PUT the '
                'binary to the returned URL, then reference the returned asset '
                'urn in the share -- and doing that badly produces a post with '
                'a broken image. Publish the text, or attach the image by hand '
                'in LinkedIn.')

        payload = {
            'author': author,
            'lifecycleState': 'PUBLISHED',
            'specificContent': {
                'com.linkedin.ugc.ShareContent': {
                    'shareCommentary': {'text': body},
                    'shareMediaCategory': 'NONE',
                },
            },
            'visibility': {
                'com.linkedin.ugc.MemberNetworkVisibility': self._visibility(visibility),
            },
        }
        _code, data = self._api('/v2/ugcPosts', method='POST', payload=payload)
        urn = data.get('id') or data.get('urn') or ''
        return self.ok(
            (f'Published to LinkedIn as {author}: {len(body)} characters, '
             f'{self._visibility(visibility)}. {self._permalink(urn)}'),
            {'post_urn': urn, 'id': urn, 'url': self._permalink(urn),
             'author': author, 'visibility': self._visibility(visibility),
             'length': len(body), 'text': body})

    def live_schedule_post(self, text='', publish_at='', visibility=''):
        """Honest refusal: the UGC Posts API has no future publish time.

        There is no field for it. A UGC post is created either published or as
        a draft, and no endpoint publishes a draft later. Silently publishing
        now would be worse than failing, because the person who asked for
        Tuesday morning would learn about it from the reactions.
        """
        body = str(text or '').strip()
        when = str(publish_at or '').strip()
        return self.failure(
            ('LinkedIn\'s API cannot schedule a post. The UGC Posts API accepts '
             'no future publish time -- a post is created published, or created '
             'as a draft that nothing in the API will publish later. The '
             'platform will hold this text as a pending action and prompt at '
             f'{when or "the requested time"} so a person can approve it then. '
             'Nothing was published and nothing was lost.'),
            {'held_by_platform': True, 'requested_at': when,
             'length': len(body), 'text': body,
             'visibility': self._visibility(visibility),
             'api_supports_scheduling': False,
             'why': ('the UGC Posts API has no publish-at field; scheduling in '
                     'the LinkedIn interface is a client-side feature, not an '
                     'API one')})

    def live_get_profile(self):
        _code, data = self._api('/v2/userinfo')
        email = data.get('email') or ''
        shown = f' ({email})' if email else ''
        return self.ok(
            (f'LinkedIn token belongs to '
             f'{data.get("name", "an unnamed member")}{shown}. '
             f'Posting as {self._author() or "nobody -- set the Author URN"}.'),
            {'name': data.get('name', ''), 'given_name': data.get('given_name', ''),
             'family_name': data.get('family_name', ''),
             'subject': data.get('sub', ''), 'locale': data.get('locale', ''),
             'author_urn': self._author()})

    def live_list_posts(self, limit=10):
        author = self._author()
        if not author:
            return self.failure(
                'Reading previous posts needs the Author URN. Set it on the '
                'Integrations page.')
        _code, data = self._api(
            '/v2/ugcPosts',
            params={'q': 'authors', 'authors': f'List({author})',
                    'count': min(int(limit or 10), 50),
                    'sortBy': 'LAST_MODIFIED'})
        rows = []
        for item in (data.get('elements') or []):
            share = (((item.get('specificContent') or {})
                      .get('com.linkedin.ugc.ShareContent') or {}))
            commentary = ((share.get('shareCommentary') or {}).get('text', ''))
            urn = item.get('id', '')
            rows.append({
                'post_urn': urn, 'url': self._permalink(urn),
                'text': commentary[:1200],
                'created_at': ((item.get('created') or {}).get('time', '')),
                'state': item.get('lifecycleState', ''),
                'visibility': ((item.get('visibility') or {})
                               .get('com.linkedin.ugc.MemberNetworkVisibility', '')),
            })
        return self.ok(
            (f'{len(rows)} recent LinkedIn post(s) for {author}. Impression and '
             'reaction figures are not part of this endpoint; they need the '
             'organizationalEntityShareStatistics endpoint, which requires the '
             'Community Management API on the application.'),
            {'author': author, 'count': len(rows), 'posts': rows,
             'metrics_available': False})

    def live_search_people(self, query='', limit=10):
        """Honest refusal: LinkedIn has no general people search API."""
        return self.failure(
            ('LinkedIn does not offer people search to ordinary applications. '
             'The only people-search endpoints live inside partner programmes '
             'such as Talent Solutions and Sales Navigator, and access is '
             'granted per company after a commercial agreement. No public '
             'endpoint will answer this. Search in the LinkedIn interface '
             'instead, or use a sourcing tool the company already licenses. '
             'Inventing plausible people here would mean fabricating '
             'information about real individuals, which this connector will not '
             'do.'),
            {'query': str(query), 'api_supports_people_search': False,
             'alternatives': ['the LinkedIn interface',
                              'a licensed Talent Solutions seat',
                              'the company\'s existing sourcing tool']})

    def live_get_company_page(self, organisation_id=''):
        identifier = str(organisation_id or '').strip()
        if not identifier and self._author().startswith('urn:li:organization:'):
            identifier = self._author().split(':')[-1]
        if not identifier:
            return self.failure(
                'Which company page? Pass organisation_id, or set the Author '
                'URN to urn:li:organization:12345 so it can be taken from '
                'there.')

        _code, data = self._api(f'/v2/organizations/{identifier}')
        name = data.get('localizedName') or ''
        followers = None
        try:
            _code, stats = self._api(
                '/v2/networkSizes/urn:li:organization:' + identifier,
                params={'edgeType': 'COMPANY_FOLLOWED_BY_MEMBER'})
            followers = stats.get('firstDegreeSize')
        except RuntimeError:
            followers = None
        return self.ok(
            (f'{name or "Company page " + identifier}: '
             f'{followers if followers is not None else "follower count unavailable"}'
             f'{" follower(s)" if followers is not None else ""}. '
             f'urn:li:organization:{identifier}'),
            {'organisation_id': identifier, 'name': name,
             'urn': f'urn:li:organization:{identifier}',
             'vanity_name': data.get('vanityName', ''),
             'followers': followers,
             'website': (((data.get('localizedWebsite') or ''))),
             'url': f'https://www.linkedin.com/company/{data.get("vanityName") or identifier}/'})

    # -- connection check --------------------------------------------------

    def live_probe(self):
        author = self._author()
        try:
            _code, data = self._api('/v2/userinfo')
            who = data.get('name', '') or data.get('sub', '')
        except RuntimeError:
            _code, data = self._api('/v2/me')
            who = ' '.join(filter(None, [
                str(data.get('localizedFirstName', '')),
                str(data.get('localizedLastName', ''))])) or data.get('id', '')
        owner = who or 'an unnamed member'
        return self.ok(
            (f'Connected to LinkedIn. The token belongs to {owner}. Posting as '
             f'{author or "nobody -- set the Author URN"}. Scheduling is not '
             'available through LinkedIn\'s API, so scheduled posts are held by '
             'this platform and prompted at the requested time.'),
            {'token_owner': who, 'author_urn': author,
             'scheduling_supported': False,
             'people_search_supported': False})

    # =======================================================================
    # Demo halves
    # =======================================================================

    def _demo_urn(self, text):
        return f'urn:li:share:{_stable(text, 7100000000000000000, 7199999999999999999)}'

    def _demo_author(self):
        return self._author() or DEMO_PROFILE['urn']

    def demo_publish_post(self, text='', visibility='', media=None):
        body = str(text or '').strip()
        if not body:
            return self.failure('A LinkedIn post needs some text.')
        if len(body) > UGC_LIMIT:
            # Enforced in demo too: a draft that would be rejected live must be
            # rejected in rehearsal, or the rehearsal is misleading.
            return self.failure(
                (f'The post is {len(body)} characters and LinkedIn rejects '
                 f'anything over {UGC_LIMIT}. Trim '
                 f'{len(body) - UGC_LIMIT} character(s). This limit is enforced '
                 'in demo mode as well, so a draft that passes here will pass '
                 'when the credential is added.'),
                {'length': len(body), 'limit': UGC_LIMIT,
                 'over_by': len(body) - UGC_LIMIT, 'simulated': True})

        urn = self._demo_urn(body)
        preview = ' '.join(body.split())
        if len(preview) > 100:
            preview = preview[:97] + '...'
        return self.simulated(
            (f'Would publish to LinkedIn as {self._demo_author()}: {preview!r} '
             f'({len(body)} characters, {self._visibility(visibility)}). '
             'Nothing was posted on LinkedIn.'),
            {'post_urn': urn, 'id': urn, 'url': self._permalink(urn),
             'author': self._demo_author(),
             'visibility': self._visibility(visibility),
             'length': len(body), 'text': body,
             'media_ignored': bool(media), 'simulated': True})

    def demo_schedule_post(self, text='', publish_at='', visibility=''):
        body = str(text or '').strip()
        if not body:
            return self.failure('A scheduled post still needs some text.')
        if len(body) > UGC_LIMIT:
            return self.failure(
                f'The post is {len(body)} characters and LinkedIn rejects '
                f'anything over {UGC_LIMIT}. Trim it before scheduling.',
                {'length': len(body), 'limit': UGC_LIMIT, 'simulated': True})
        when = str(publish_at or '').strip()
        urn = self._demo_urn(f'scheduled:{body}')
        return self.simulated(
            (f'Would hold this post until {when or "a time you specify"} and '
             'prompt for approval then. LinkedIn\'s API has no publish-at '
             'field, so the platform does the waiting rather than LinkedIn. '
             'Nothing was posted and nothing was queued at LinkedIn.'),
            {'held_by_platform': True, 'requested_at': when,
             'draft_urn': urn, 'length': len(body), 'text': body,
             'visibility': self._visibility(visibility),
             'api_supports_scheduling': False, 'simulated': True})

    def demo_get_profile(self):
        return self.simulated(
            (f'Simulated LinkedIn profile: {DEMO_PROFILE["name"]}, '
             f'{DEMO_PROFILE["followers"]} followers, '
             f'{DEMO_PROFILE["industry"]}, {DEMO_PROFILE["location"]}. '
             'Fixture data, not your LinkedIn account.'),
            {'name': DEMO_PROFILE['name'],
             'headline': DEMO_PROFILE['headline'],
             'author_urn': self._demo_author(),
             'followers': DEMO_PROFILE['followers'],
             'employees': DEMO_PROFILE['employees'],
             'industry': DEMO_PROFILE['industry'],
             'location': DEMO_PROFILE['location'],
             'website': DEMO_PROFILE['website'], 'simulated': True})

    def demo_list_posts(self, limit=10):
        rows = []
        for item in DEMO_POSTS[:max(1, int(limit or 10))]:
            urn = self._demo_urn(item['slug'])
            engagement = item['reactions'] + item['comments'] + item['shares']
            rows.append({
                'post_urn': urn, 'url': self._permalink(urn),
                'text': item['text'], 'created_at': item['published'],
                'state': 'PUBLISHED', 'visibility': 'PUBLIC',
                'impressions': item['impressions'],
                'reactions': item['reactions'], 'comments': item['comments'],
                'shares': item['shares'], 'clicks': item['clicks'],
                'engagement_rate': round(100 * engagement / item['impressions'], 2),
            })
        best = max(rows, key=lambda row: row['engagement_rate']) if rows else None
        total = sum(row['impressions'] for row in rows)
        return self.simulated(
            (f'Simulated {len(rows)} previous post(s), {total} total '
             f'impression(s). Best engagement: '
             f'{best["engagement_rate"] if best else 0}% on the post published '
             f'{best["created_at"] if best else "never"}. Fixture figures, not '
             'your account -- a live call would need the Community Management '
             'API for metrics.'),
            {'author': self._demo_author(), 'count': len(rows), 'posts': rows,
             'total_impressions': total, 'metrics_available': True,
             'simulated': True})

    def demo_search_people(self, query='', limit=10):
        """Refused in demo too, because the limit is real and permanent."""
        return self.failure(
            ('LinkedIn does not offer people search to ordinary applications, '
             'so there is nothing to simulate. Fabricating plausible people '
             'would mean inventing information about real individuals, and a '
             'simulation that cannot ever become live is a misleading '
             'rehearsal. Search in the LinkedIn interface instead.'),
            {'query': str(query), 'api_supports_people_search': False,
             'simulated': True})

    def demo_get_company_page(self, organisation_id=''):
        identifier = (str(organisation_id or '').strip()
                      or DEMO_PROFILE['urn'].split(':')[-1])
        return self.simulated(
            (f'Simulated company page {DEMO_PROFILE["name"]} '
             f'(urn:li:organization:{identifier}): '
             f'{DEMO_PROFILE["followers"]} followers, '
             f'{DEMO_PROFILE["employees"]} employees, '
             f'{DEMO_PROFILE["industry"]}. Fixture data.'),
            {'organisation_id': identifier, 'name': DEMO_PROFILE['name'],
             'urn': f'urn:li:organization:{identifier}',
             'vanity_name': DEMO_PROFILE['vanity'],
             'followers': DEMO_PROFILE['followers'],
             'employees': DEMO_PROFILE['employees'],
             'industry': DEMO_PROFILE['industry'],
             'location': DEMO_PROFILE['location'],
             'website': DEMO_PROFILE['website'],
             'url': f'https://www.linkedin.com/company/{DEMO_PROFILE["vanity"]}/',
             'simulated': True})
