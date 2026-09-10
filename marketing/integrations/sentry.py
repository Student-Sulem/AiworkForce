"""Sentry: error monitoring integration for the Developer employee.

WHAT THIS DOES FOR THE WORKFORCE
--------------------------------
Sentry gives the Developer employee real error data: recent exceptions,
affected users, frequency, and stack traces. The connector reads error
summaries and issue details so the Developer can debug from live data
rather than from speculation.
"""

from .base import CallResult, ConfigField, Connector
from . import register


@register
class SentryConnector(Connector):
    key = 'sentry'
    name = 'Sentry'
    description = 'Error monitoring: read exceptions, stack traces and issue frequency.'
    category = 'development'
    icon = 'fa-bug'
    color = '#362d59'

    config_fields = (
        ConfigField('auth_token', 'Auth token', secret=True, required=True,
                    help_text='Sentry auth token from https://sentry.io/settings/api/'),
        ConfigField('org_slug', 'Organisation slug', required=True,
                    help_text='Your Sentry organisation slug (e.g. "mycompany")'),
        ConfigField('project_id', 'Project ID', required=False,
                    help_text='Optional: limit queries to one project'),
        ConfigField('base_url', 'Base URL', required=False,
                    default='https://sentry.io',
                    help_text='Self-hosted Sentry URL, if applicable'),
    )

    operations = (
        'list_issues', 'get_issue', 'list_events',
    )

    def live_list_issues(self, project_id=None, query=None, limit=10):
        org = self.setting('org_slug')
        token = self.setting('auth_token')
        base = self.setting('base_url', 'https://sentry.io')
        status, data = self.request_json(
            f'{base}/api/0/projects/{org}/{project_id or ""}/issues/',
            headers={'Authorization': f'Bearer {token}'},
            params={'query': query, 'limit': min(limit, 50)} if query else {'limit': min(limit, 50)})
        results = data if isinstance(data, list) else data.get('data', [])
        return self.ok(f'Found {len(results)} recent Sentry issues.',
                       {'issues': [{'id': r['id'], 'title': r['title'],
                                    'level': r.get('level', 'error'),
                                    'count': r.get('count', 0),
                                    'first_seen': r.get('firstSeen', '')}
                                   for r in results[:limit]]})

    def demo_list_issues(self, project_id=None, query=None, limit=10):
        return self.simulated(
            f'Retrieved {limit} simulated Sentry issues.',
            {'issues': [
                {'id': '1001', 'title': 'TypeError: Cannot read properties of null',
                 'level': 'error', 'count': 47, 'first_seen': '2026-09-01T10:00:00Z'},
                {'id': '1002', 'title': 'ConnectionTimeoutError: database pool exhausted',
                 'level': 'error', 'count': 23, 'first_seen': '2026-09-02T14:30:00Z'},
                {'id': '1003', 'title': 'ValueError: invalid literal for int()',
                 'level': 'warning', 'count': 12, 'first_seen': '2026-09-03T08:15:00Z'},
            ]})

    def live_get_issue(self, issue_id):
        org = self.setting('org_slug')
        token = self.setting('auth_token')
        base = self.setting('base_url', 'https://sentry.io')
        status, data = self.request_json(
            f'{base}/api/0/issues/{issue_id}/',
            headers={'Authorization': f'Bearer {token}'})
        return self.ok(f'Retrieved issue {issue_id}: {data.get("title", "")}',
                       {'id': data.get('id'), 'title': data.get('title'),
                        'level': data.get('level'), 'count': data.get('count', 0),
                        'status': data.get('status')})

    def demo_get_issue(self, issue_id):
        return self.simulated(f'Retrieved simulated issue #{issue_id}.',
                              {'id': issue_id, 'title': 'Sample error',
                               'level': 'error', 'count': 15, 'status': 'unresolved'})

    def live_list_events(self, issue_id, limit=10):
        org = self.setting('org_slug')
        token = self.setting('auth_token')
        base = self.setting('base_url', 'https://sentry.io')
        status, data = self.request_json(
            f'{base}/api/0/issues/{issue_id}/events/',
            headers={'Authorization': f'Bearer {token}'},
            params={'limit': min(limit, 50)})
        events = data if isinstance(data, list) else data.get('data', [])
        return self.ok(f'Retrieved {len(events)} events for issue {issue_id}.',
                       {'events': [{'id': e['id'], 'message': e.get('message', '')[:200],
                                    'date': e.get('dateCreated', '')}
                                   for e in events[:limit]]})

    def demo_list_events(self, issue_id, limit=10):
        return self.simulated(f'Retrieved simulated events for issue {issue_id}.',
                              {'events': [
                                  {'id': 'evt-1', 'message': 'Error in view.py:72', 'date': '2026-09-01T10:00:00Z'},
                                  {'id': 'evt-2', 'message': 'Error in serializers.py:34', 'date': '2026-09-01T10:00:01Z'},
                              ]})