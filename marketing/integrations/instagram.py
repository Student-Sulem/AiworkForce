"""Instagram: visual publishing for the Marketing employee, via the Graph API.

WHAT THIS DOES FOR THE WORKFORCE
--------------------------------
Instagram is the workforce's visual channel. The Marketing employee drafts a
caption, a person approves it along with the image, and this connector
publishes it to the company's business account. As with LinkedIn, publishing
is never autonomous: the draft becomes a proposed action carrying the exact
caption and image URL, and only an approval releases it. Reading matters as
much as writing -- the reach and engagement figures on previous posts are what
turn "post something" into "post the kind of thing that worked".

WHICH EMPLOYEES USE IT
----------------------
``Marketing``  publishes approved posts and reads reach, impressions and
               engagement to decide what to publish next.
``Research``   reads the account and recent media for context before a
               campaign brief is written.

THE CREDENTIAL YOU NEED
-----------------------
Instagram's API is part of the Facebook Graph API, and it only works for a
business or creator account connected to a Facebook Page. A personal account
cannot publish through the API at all, no matter what token you hold.

  1. Convert the Instagram account to a Business or Creator account and
     connect it to a Facebook Page.
  2. Create an app at developers.facebook.com and add Instagram Graph API.
  3. Generate a Page access token with ``instagram_basic``,
     ``instagram_content_publish``, ``pages_read_engagement`` and
     ``pages_show_list``. A long-lived Page token is the one worth storing; a
     short-lived user token expires in about an hour.
  4. Find the Instagram business account id -- it is on the Page's
     ``instagram_business_account`` field -- and put it, the token, and the
     Graph version into the Integrations page.

TWO STEPS TO PUBLISH, AND ONE HARD REQUIREMENT
----------------------------------------------
Publishing is deliberately two calls: create a media container from the image
URL and the caption, then publish that container. The hard requirement is the
image. Instagram fetches the image itself, from a URL that must be publicly
reachable over HTTPS -- there is no file upload, and a local path, a signed
URL that expires, or anything behind authentication will fail. ``publish_post``
therefore refuses immediately when no image URL is supplied, and says what is
needed, rather than sending a request that cannot succeed.

Two content limits are enforced before anything is sent: 2200 characters of
caption and 30 hashtags. Both are Instagram's, both are rejected server-side,
and checking first means the draft can be fixed rather than half-published.

WHAT DEMO MODE DOES INSTEAD
---------------------------
Without a token and an account id every operation is simulated: stable media
ids and permalinks derived from a hash of the caption, an account summary, six
previous media entries with like, comment, reach and impression figures, and
insight totals. The caption and hashtag limits and the image URL requirement
are all enforced in demo mode too, so a draft that passes in rehearsal will
pass when the credential is added. Every simulated result is flagged.
"""

import hashlib
import re
import urllib.error

from . import register
from .base import CallResult, ConfigField, Connector

CAPTION_LIMIT = 2200
HASHTAG_LIMIT = 30
HASHTAG_PATTERN = re.compile(r'#\w+', re.UNICODE)


def _stable(text, low, high):
    digest = hashlib.sha256(str(text).encode('utf-8')).hexdigest()
    return low + int(digest[:12], 16) % max(1, (high - low + 1))


def _shortcode(text):
    """A stable, Instagram-shaped eleven character shortcode."""
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-'
    digest = hashlib.sha256(f'ig:{text}'.encode()).digest()
    return ''.join(alphabet[byte % len(alphabet)] for byte in digest[:11])


DEMO_ACCOUNT = {
    'id': '17841400000000001',
    'username': 'acmeplatform',
    'name': 'Acme Platform',
    'followers': 12480,
    'follows': 312,
    'media_count': 214,
    'biography': ('Marketing and operations software for teams that answer for '
                  'their work. Melbourne built.'),
    'website': 'https://acme.example.com',
    'profile_views_last_week': 1840,
}

DEMO_MEDIA = (
    {'slug': 'behind-the-release', 'type': 'IMAGE',
     'timestamp': '2026-09-04T23:15:00+0000',
     'likes': 642, 'comments': 38, 'reach': 9120, 'impressions': 11470,
     'saved': 74,
     'caption': ('Release day. Migrations first, one container, five minutes of '
                 'watching the dashboard, then the rest. Boring on purpose. '
                 '#buildinpublic #devops #melbourne')},
    {'slug': 'team-offsite', 'type': 'CAROUSEL_ALBUM',
     'timestamp': '2026-08-29T02:40:00+0000',
     'likes': 1184, 'comments': 91, 'reach': 15330, 'impressions': 19880,
     'saved': 46,
     'caption': ('Two days, one whiteboard, no laptops after four. The roadmap '
                 'came out shorter than it went in, which is the point. '
                 '#teamculture #startuplife')},
    {'slug': 'hiring-engineers', 'type': 'IMAGE',
     'timestamp': '2026-08-21T01:10:00+0000',
     'likes': 428, 'comments': 55, 'reach': 7260, 'impressions': 8940,
     'saved': 132,
     'caption': ('Two engineering roles open in Melbourne. Ninety minutes '
                 'pairing on a real ticket, paid, no whiteboard algorithms. '
                 'Link in bio. #hiring #engineeringjobs #melbournejobs')},
    {'slug': 'customer-story-northgate', 'type': 'VIDEO',
     'timestamp': '2026-08-14T04:05:00+0000',
     'likes': 906, 'comments': 27, 'reach': 21440, 'impressions': 26910,
     'saved': 88,
     'caption': ('Northgate cut their monthly campaign approval cycle from nine '
                 'days to two. Their operations lead explains how, in ninety '
                 'seconds. #customerstory #marketingops')},
    {'slug': 'no-friday-releases', 'type': 'IMAGE',
     'timestamp': '2026-08-06T22:50:00+0000',
     'likes': 1502, 'comments': 143, 'reach': 28710, 'impressions': 36240,
     'saved': 211,
     'caption': ('We do not release on Fridays. Every incident that took us '
                 'more than an hour started with a release nobody was awake to '
                 'watch. #engineering #devops #hottake')},
    {'slug': 'design-system-refresh', 'type': 'CAROUSEL_ALBUM',
     'timestamp': '2026-07-30T03:25:00+0000',
     'likes': 733, 'comments': 34, 'reach': 10850, 'impressions': 13120,
     'saved': 159,
     'caption': ('New colour tokens, one type scale, and every component now '
                 'legible in dark mode. Swipe for the before. #designsystem '
                 '#productdesign')},
)

DEMO_INSIGHTS = {
    'reach': 41230,
    'impressions': 58940,
    'profile_views': 1840,
    'follower_count': DEMO_ACCOUNT['followers'],
    'website_clicks': 412,
    'accounts_engaged': 6180,
}


@register
class InstagramConnector(Connector):
    """Publishing and insights for an Instagram business account."""

    key = 'instagram'
    name = 'Instagram'
    description = ('Publishes approved posts to the company Instagram business '
                   'account and reads reach and engagement.')
    category = 'social'
    icon = 'fa-instagram'
    color = '#e1306c'
    docs_url = 'https://developers.facebook.com/docs/instagram-api'

    config_fields = (
        ConfigField(
            'access_token', 'Page access token',
            help_text=('A long-lived Facebook Page token with '
                       'instagram_basic, instagram_content_publish and '
                       'pages_read_engagement. A short-lived user token '
                       'expires within the hour and is not worth storing.'),
            field_type='password', required=True, secret=True),
        ConfigField(
            'ig_user_id', 'Instagram business account id',
            help_text=('The numeric id on the connected Facebook Page\'s '
                       'instagram_business_account field. A personal Instagram '
                       'account cannot publish through the API at all.'),
            required=True, placeholder='17841400000000001'),
        ConfigField(
            'api_version', 'Graph API version',
            help_text=('Facebook retires a Graph version roughly every two '
                       'years. Raise this only after reading their changelog.'),
            default='v21.0'),
        ConfigField(
            'graph_base', 'Graph API base URL',
            help_text='Leave as it is unless you are proxying Graph calls.',
            default='https://graph.facebook.com'),
    )

    operations = ('publish_post', 'list_media', 'get_insights', 'get_account')

    # -- plumbing ----------------------------------------------------------

    def _base(self):
        root = str(self.setting('graph_base', 'https://graph.facebook.com')).rstrip('/')
        version = str(self.setting('api_version', 'v21.0')).strip('/')
        return f'{root}/{version}'

    def _account(self):
        return str(self.setting('ig_user_id', '') or '').strip()

    def _api(self, path, *, method='GET', params=None, payload=None):
        query = dict(params or {})
        query['access_token'] = str(self.setting('access_token', '') or '')
        try:
            status, data = self.request_json(
                f'{self._base()}{path}', method=method, params=query,
                payload=payload)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(self._advise(exc, path)) from exc
        return status, data

    def _advise(self, exc, path):
        try:
            body = exc.read().decode('utf-8', errors='replace')[:700]
        except Exception:  # noqa: BLE001
            body = ''
        lowered = body.lower()
        code = exc.code

        if 'media_url' in lowered or 'unable to fetch' in lowered or 'uri' in lowered:
            return ('Instagram could not fetch the image. It downloads the file '
                    'itself, so the URL must be publicly reachable over HTTPS, '
                    'must return an image content type, and must not require '
                    'authentication or redirect through a login. A signed URL '
                    'that has expired fails the same way. Host the image '
                    f'somewhere public and try again. Instagram said: {body}')
        if code in (190, 401) or 'access token' in lowered:
            return ('Instagram rejected the access token. Page tokens expire, '
                    'and a token also stops working when the authorising person '
                    'loses admin rights on the Page or changes their password. '
                    'Generate a new long-lived Page token at '
                    'developers.facebook.com and paste it in.')
        if code == 403 or 'permission' in lowered:
            return ('Instagram refused the request. The token lacks a '
                    'permission -- publishing needs instagram_content_publish, '
                    'and reading insights needs pages_read_engagement. Both '
                    'must be granted on the app and approved for a live app. '
                    f'Instagram said: {body}')
        if 'not a business' in lowered or 'business account' in lowered:
            return ('This Instagram account is not a Business or Creator '
                    'account connected to a Facebook Page, so the API cannot '
                    'publish for it. Convert the account in the Instagram app '
                    'and connect it to a Page first.')
        if code == 400:
            return ('Instagram rejected the request (400). Check the account id '
                    'is the Instagram business account id and not the Facebook '
                    f'Page id, which is a common mix-up. Instagram said: {body}')
        if code == 429 or 'rate limit' in lowered:
            return ('Instagram is rate limiting the account. Publishing is '
                    'capped at twenty-five posts per twenty-four hours. Wait '
                    'and try again.')
        return f'Instagram returned HTTP {code} {exc.reason}. {body}'.strip()

    def _check_caption(self, caption):
        """Both limits, checked before anything is sent. Returns a failure or None."""
        text = str(caption or '')
        if len(text) > CAPTION_LIMIT:
            return self.failure(
                (f'The caption is {len(text)} characters and Instagram rejects '
                 f'anything over {CAPTION_LIMIT}. Trim '
                 f'{len(text) - CAPTION_LIMIT} character(s). Nothing was sent.'),
                {'length': len(text), 'limit': CAPTION_LIMIT,
                 'over_by': len(text) - CAPTION_LIMIT})
        tags = HASHTAG_PATTERN.findall(text)
        if len(tags) > HASHTAG_LIMIT:
            return self.failure(
                (f'The caption has {len(tags)} hashtags and Instagram allows '
                 f'{HASHTAG_LIMIT}. Remove {len(tags) - HASHTAG_LIMIT}. '
                 'Instagram silently suppresses reach on over-tagged posts as '
                 'well as rejecting them, so fewer is usually better than the '
                 'maximum. Nothing was sent.'),
                {'hashtags': len(tags), 'limit': HASHTAG_LIMIT,
                 'tags': tags[:40]})
        return None

    # =======================================================================
    # Live
    # =======================================================================

    def live_publish_post(self, caption='', image_url='', media_type='IMAGE'):
        account = self._account()
        if not account:
            return self.failure(
                'No Instagram business account id is set. Find it on the '
                'connected Facebook Page\'s instagram_business_account field '
                'and put it on the Integrations page.')

        problem = self._check_caption(caption)
        if problem is not None:
            return problem

        if not str(image_url).strip():
            return self.failure(
                ('Instagram cannot publish without a publicly reachable image '
                 'URL. It fetches the file itself over HTTPS -- there is no '
                 'upload endpoint -- so the URL must be public, must not '
                 'require authentication, and must still be valid at the moment '
                 'of publishing. Host the image somewhere public and pass '
                 'image_url. Demo mode can simulate the whole publish if you '
                 'only need to rehearse the caption.'),
                {'image_url_required': True, 'caption_length': len(str(caption or '')),
                 'caption': str(caption or '')})

        kind = str(media_type or 'IMAGE').strip().upper()
        container = {'caption': str(caption or '')}
        if kind in ('VIDEO', 'REELS'):
            container['media_type'] = 'REELS' if kind == 'REELS' else 'VIDEO'
            container['video_url'] = str(image_url).strip()
        else:
            container['image_url'] = str(image_url).strip()

        # Step one: the container.
        _code, created = self._api(f'/{account}/media', method='POST',
                                   params=container)
        container_id = created.get('id', '')
        if not container_id:
            return self.failure(
                'Instagram accepted the container request but returned no '
                'container id, so there is nothing to publish. Nothing was '
                'posted.', {'response': created})

        # Step two: publish it.
        _code, published = self._api(f'/{account}/media_publish', method='POST',
                                     params={'creation_id': container_id})
        media_id = published.get('id', '')

        permalink = ''
        try:
            _code, detail = self._api(f'/{media_id}', params={'fields': 'permalink'})
            permalink = detail.get('permalink', '')
        except RuntimeError:
            permalink = ''

        return self.ok(
            (f'Published to Instagram as @{account}: '
             f'{len(str(caption or ""))} character caption, {kind}. '
             f'{permalink or "permalink unavailable"}'),
            {'media_id': media_id, 'container_id': container_id,
             'permalink': permalink, 'account_id': account,
             'media_type': kind, 'image_url': str(image_url).strip(),
             'caption': str(caption or ''),
             'hashtags': HASHTAG_PATTERN.findall(str(caption or ''))})

    def live_list_media(self, limit=10):
        account = self._account()
        if not account:
            return self.failure('No Instagram business account id is set.')
        fields = ('id,caption,media_type,media_url,permalink,timestamp,'
                  'like_count,comments_count')
        _code, data = self._api(f'/{account}/media',
                                params={'fields': fields,
                                        'limit': min(int(limit or 10), 50)})
        rows = []
        for item in (data.get('data') or []):
            rows.append({
                'media_id': item.get('id', ''),
                'caption': (item.get('caption') or '')[:2200],
                'media_type': item.get('media_type', ''),
                'permalink': item.get('permalink', ''),
                'timestamp': item.get('timestamp', ''),
                'likes': item.get('like_count', 0),
                'comments': item.get('comments_count', 0),
            })
        engagement = sum(row['likes'] + row['comments'] for row in rows)
        return self.ok(
            f'{len(rows)} recent Instagram post(s), {engagement} like(s) and '
            f'comment(s) between them.',
            {'account_id': account, 'count': len(rows), 'media': rows,
             'total_engagement': engagement})

    def live_get_insights(self, metric='', period='day'):
        account = self._account()
        if not account:
            return self.failure('No Instagram business account id is set.')
        wanted = (str(metric).strip()
                  or 'reach,impressions,profile_views,follower_count')
        _code, data = self._api(f'/{account}/insights',
                                params={'metric': wanted,
                                        'period': str(period or 'day')})
        values = {}
        for item in (data.get('data') or []):
            series = item.get('values') or []
            latest = series[-1].get('value') if series else None
            values[item.get('name', 'unknown')] = latest
        if not values:
            return self.ok(
                ('Instagram returned no insight values. Insights need at least '
                 'one hundred followers on the account and a metric that the '
                 'chosen period supports -- follower_count, for instance, is '
                 'not available for the lifetime period.'),
                {'account_id': account, 'metric': wanted,
                 'period': str(period), 'values': {}})
        readable = ', '.join(f'{name} {value}' for name, value in values.items())
        return self.ok(f'Instagram insights ({period}): {readable}.',
                       {'account_id': account, 'metric': wanted,
                        'period': str(period), 'values': values})

    def live_get_account(self):
        account = self._account()
        if not account:
            return self.failure('No Instagram business account id is set.')
        fields = ('id,username,name,biography,website,followers_count,'
                  'follows_count,media_count,profile_picture_url')
        _code, data = self._api(f'/{account}', params={'fields': fields})
        return self.ok(
            (f'@{data.get("username", "unknown")} ({data.get("name", "")}): '
             f'{data.get("followers_count", 0)} follower(s), '
             f'{data.get("media_count", 0)} post(s).'),
            {'account_id': data.get('id', account),
             'username': data.get('username', ''),
             'name': data.get('name', ''),
             'biography': data.get('biography', ''),
             'website': data.get('website', ''),
             'followers': data.get('followers_count', 0),
             'follows': data.get('follows_count', 0),
             'media_count': data.get('media_count', 0)})

    # -- connection check --------------------------------------------------

    def live_probe(self):
        _code, data = self._api(f'/{self._account()}',
                                params={'fields': 'username,name,followers_count,media_count'})
        return self.ok(
            (f'Connected to Instagram as @{data.get("username", "unknown")} '
             f'({data.get("name", "no display name")}), '
             f'{data.get("followers_count", 0)} follower(s), '
             f'{data.get("media_count", 0)} post(s). Publishing requires a '
             'publicly reachable image URL, because Instagram fetches the file '
             'itself.'),
            {'username': data.get('username', ''),
             'followers': data.get('followers_count', 0),
             'media_count': data.get('media_count', 0),
             'account_id': self._account(),
             'requires_public_image_url': True})

    # =======================================================================
    # Demo halves
    # =======================================================================

    def _demo_account(self):
        return self._account() or DEMO_ACCOUNT['id']

    def demo_publish_post(self, caption='', image_url='', media_type='IMAGE'):
        # Both limits and the image requirement are enforced here as well.
        # A rehearsal that accepts a draft the live call would reject is worse
        # than no rehearsal, because it moves the failure to publication day.
        problem = self._check_caption(caption)
        if problem is not None:
            problem.data['simulated'] = True
            return problem

        text = str(caption or '')
        kind = str(media_type or 'IMAGE').strip().upper()
        tags = HASHTAG_PATTERN.findall(text)
        media_id = str(_stable(f'media:{text}', 17900000000000000, 17999999999999999))
        code = _shortcode(text)
        image = str(image_url).strip()

        if not image:
            return self.simulated(
                ('Would publish to Instagram, but no image URL was given. A '
                 'live call fails outright at this point: Instagram fetches the '
                 'file itself from a public HTTPS URL and has no upload '
                 'endpoint. The caption below passed both content limits, so '
                 'only the image is outstanding. Nothing was posted.'),
                {'media_id': media_id,
                 'permalink': f'https://www.instagram.com/p/{code}/',
                 'account_id': self._demo_account(), 'media_type': kind,
                 'caption': text, 'caption_length': len(text),
                 'hashtags': tags, 'image_url': '',
                 'image_url_required': True,
                 'would_fail_live': True, 'simulated': True})

        preview = ' '.join(text.split())
        if len(preview) > 90:
            preview = preview[:87] + '...'
        return self.simulated(
            (f'Would publish to Instagram as @{DEMO_ACCOUNT["username"]}: '
             f'{preview!r} ({len(text)} characters, {len(tags)} hashtag(s), '
             f'{kind}) using the image at {image}. Two live calls would be made '
             '-- create the container, then publish it. Nothing was posted.'),
            {'media_id': media_id,
             'container_id': str(_stable(f'container:{text}', 1, 9999999999)),
             'permalink': f'https://www.instagram.com/p/{code}/',
             'account_id': self._demo_account(), 'media_type': kind,
             'caption': text, 'caption_length': len(text),
             'hashtags': tags, 'image_url': image, 'simulated': True})

    def demo_list_media(self, limit=10):
        rows = []
        for item in DEMO_MEDIA[:max(1, int(limit or 10))]:
            code = _shortcode(item['slug'])
            rows.append({
                'media_id': str(_stable(item['slug'],
                                        17900000000000000, 17999999999999999)),
                'caption': item['caption'], 'media_type': item['type'],
                'permalink': f'https://www.instagram.com/p/{code}/',
                'timestamp': item['timestamp'],
                'likes': item['likes'], 'comments': item['comments'],
                'reach': item['reach'], 'impressions': item['impressions'],
                'saved': item['saved'],
                'engagement_rate': round(
                    100 * (item['likes'] + item['comments'] + item['saved'])
                    / item['reach'], 2),
                'hashtags': HASHTAG_PATTERN.findall(item['caption']),
            })
        engagement = sum(row['likes'] + row['comments'] for row in rows)
        best = max(rows, key=lambda row: row['engagement_rate']) if rows else None
        return self.simulated(
            (f'Simulated {len(rows)} recent post(s) for '
             f'@{DEMO_ACCOUNT["username"]}, {engagement} like(s) and '
             f'comment(s) between them. Best engagement rate: '
             f'{best["engagement_rate"] if best else 0}%. Fixture figures, not '
             'your account.'),
            {'account_id': self._demo_account(), 'count': len(rows),
             'media': rows, 'total_engagement': engagement,
             'simulated': True})

    def demo_get_insights(self, metric='', period='day'):
        asked = [name.strip() for name in str(metric or '').split(',') if name.strip()]
        values = ({name: DEMO_INSIGHTS.get(name) for name in asked}
                  if asked else dict(DEMO_INSIGHTS))
        missing = [name for name, value in values.items() if value is None]
        readable = ', '.join(f'{name} {value}' for name, value in values.items()
                             if value is not None)
        note = ''
        if missing:
            note = (f' No fixture value exists for {", ".join(missing)}; the '
                    'demo covers reach, impressions, profile_views, '
                    'follower_count, website_clicks and accounts_engaged.')
        return self.simulated(
            (f'Simulated Instagram insights ({period}): {readable or "nothing"}.'
             f'{note} Fixture figures, not your account.'),
            {'account_id': self._demo_account(),
             'metric': str(metric) or ','.join(DEMO_INSIGHTS),
             'period': str(period or 'day'),
             'values': {name: value for name, value in values.items()
                        if value is not None},
             'unavailable': missing, 'simulated': True})

    def demo_get_account(self):
        return self.simulated(
            (f'Simulated account @{DEMO_ACCOUNT["username"]} '
             f'({DEMO_ACCOUNT["name"]}): {DEMO_ACCOUNT["followers"]} '
             f'follower(s), {DEMO_ACCOUNT["media_count"]} post(s), '
             f'{DEMO_ACCOUNT["profile_views_last_week"]} profile view(s) last '
             'week. Fixture data, not your Instagram account.'),
            {'account_id': self._demo_account(),
             'username': DEMO_ACCOUNT['username'],
             'name': DEMO_ACCOUNT['name'],
             'biography': DEMO_ACCOUNT['biography'],
             'website': DEMO_ACCOUNT['website'],
             'followers': DEMO_ACCOUNT['followers'],
             'follows': DEMO_ACCOUNT['follows'],
             'media_count': DEMO_ACCOUNT['media_count'],
             'profile_views_last_week': DEMO_ACCOUNT['profile_views_last_week'],
             'simulated': True})
