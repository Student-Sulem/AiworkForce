"""Make a real call against one connected application, and say what came back.

WHY THIS IS NOT THE SAME AS THE CONNECTION TEST
-----------------------------------------------
The Test connection button on the Integrations page answers "does this
credential authenticate". That is worth knowing and it is not the question
that matters, because a token can authenticate perfectly and still be unable
to do the one thing you need: the scope may be missing, the repository may not
be in the token's selected set, the calendar may not be shared with the
account. All of those pass a connection test and fail the first real call.

So this command runs an actual read -- list the issues, list the files, list
the channels -- and prints what came back. It only ever reads. Nothing here
creates, sends or publishes anything, so it is safe to run against a live
credential at any time.

    python manage.py try_integration github
    python manage.py try_integration --all
    python manage.py try_integration slack --operation list_channels
    python manage.py try_integration github --operation list_issues --arg repo=owner/name

WHAT "SIMULATED" IN THE OUTPUT MEANS
-----------------------------------
The integration has no credential, or is set to demo, so the connector
answered from its own fixture rather than the service. That is a working
state, not a failure -- it is how the whole platform stays demonstrable
without accounts. The line says so explicitly rather than letting a plausible
result be mistaken for a live one.
"""

from django.core.management.base import BaseCommand

from marketing import integrations
from marketing.models_platform import Integration

# One read per connector, chosen because it proves the credential can reach
# the thing the employees actually use, not merely that it authenticates.
PROBES = {
    'github': ('list_issues', {'state': 'open', 'limit': 5}),
    'jira': ('list_projects', {}),
    'slack': ('list_channels', {'limit': 10}),
    'gmail': ('list_messages', {'limit': 5}),
    'google_calendar': ('find_availability', {'duration_minutes': 45,
                                              'days_ahead': 5}),
    'google_drive': ('list_files', {'limit': 10}),
    'notion': ('search', {'query': '', 'limit': 5}),
    'confluence': ('list_spaces', {}),
    'linkedin': ('get_profile', {}),
    'instagram': ('get_account', {}),
    'knowledge_base': ('stats', {}),
}


class Command(BaseCommand):
    help = 'Run a real read against one connected application and show the result.'

    def add_arguments(self, parser):
        parser.add_argument(
            'provider', nargs='?', default='',
            help='Which integration, e.g. github. Omit with --all.')
        parser.add_argument('--all', action='store_true',
                            help='Try every enabled integration in turn.')
        parser.add_argument('--operation', default='',
                            help='Override the read operation to call.')
        parser.add_argument(
            '--arg', action='append', default=[], metavar='KEY=VALUE',
            help='An argument for the operation. Repeatable.')

    def handle(self, *args, **options):
        if options['all']:
            targets = list(Integration.objects.filter(is_enabled=True)
                           .values_list('provider_key', flat=True))
        elif options['provider']:
            targets = [options['provider']]
        else:
            self.stdout.write(self.style.ERROR(
                'Name an integration, or pass --all.'))
            self.stdout.write('  Available: ' + ', '.join(sorted(PROBES)))
            return

        extra = {}
        for pair in options['arg']:
            if '=' not in pair:
                self.stdout.write(self.style.ERROR(
                    f'--arg needs KEY=VALUE, not "{pair}".'))
                return
            key, value = pair.split('=', 1)
            extra[key.strip()] = value.strip()

        live = simulated = failed = 0

        for key in targets:
            row = Integration.objects.filter(provider_key=key).first()
            if row is None:
                self.stdout.write(self.style.ERROR(f'{key}: not registered.'))
                failed += 1
                continue

            operation, defaults = PROBES.get(key, ('', {}))
            operation = options['operation'] or operation
            if not operation:
                self.stdout.write(self.style.WARNING(
                    f'{row.name}: no read operation known. '
                    f'Pass --operation.'))
                continue

            arguments = dict(defaults)
            arguments.update(extra)

            self.stdout.write('')
            self.stdout.write(self.style.MIGRATE_HEADING(
                f'{row.name}  ({key}.{operation})'))
            self.stdout.write(f'  mode: {row.mode}, effective: {row.effective_mode}'
                              + (f', missing: {", ".join(row.missing_settings)}'
                                 if row.missing_settings else ''))

            result = integrations.call(key, operation, **arguments)

            if not result.ok:
                failed += 1
                self.stdout.write(self.style.ERROR(f'  FAILED  {result.error}'))
                self.stdout.write('  ' + self._advice(key, result.error))
                continue

            if result.demo:
                simulated += 1
                self.stdout.write(self.style.WARNING(
                    '  SIMULATED -- this answer came from the connector\'s own '
                    'fixture, not the service.'))
            else:
                live += 1
                self.stdout.write(self.style.SUCCESS('  LIVE -- this came from the '
                                                     'real service.'))

            self.stdout.write(f'  {result.summary[:300]}')
            for line in self._preview(result.data):
                self.stdout.write(f'    {line}')

        self.stdout.write('')
        self.stdout.write(f'{live} live, {simulated} simulated, {failed} failed.')
        if failed:
            self.stdout.write(self.style.WARNING(
                'A failure here is the useful kind: the credential exists but '
                'cannot do this particular thing. The advice above names what to '
                'change.'))

    def _preview(self, data, limit=6):
        """A few rows of whatever came back, so the result is visibly real."""
        if not isinstance(data, dict):
            return [str(data)[:160]]

        for key, value in data.items():
            if isinstance(value, list) and value:
                out = [f'{key}: {len(value)} item(s)']
                for item in value[:limit]:
                    if isinstance(item, dict):
                        label = (item.get('title') or item.get('name')
                                 or item.get('subject') or item.get('label')
                                 or item.get('key') or item.get('id') or '')
                        out.append(f'  - {str(label)[:110]}')
                    else:
                        out.append(f'  - {str(item)[:110]}')
                return out

        return [f'{k}: {str(v)[:90]}' for k, v in list(data.items())[:limit]]

    def _advice(self, key, error):
        """What to actually change, for the failures that really happen."""
        lowered = str(error).lower()

        if '404' in lowered and key == 'github':
            return ('The token authenticated but cannot see that repository. A '
                    'fine-grained token only reaches repositories selected when '
                    'it was created.')
        if '403' in lowered and 'github' in key:
            return ('Authenticated, but the token lacks the permission for this. '
                    'Issues needs Read and write; reading files needs Contents '
                    'Read.')
        if '401' in lowered or 'unauthor' in lowered:
            return ('The credential was rejected. If this is Google, an OAuth '
                    'Playground access token expires after about an hour.')
        if '403' in lowered and 'google' in key:
            return ('Enable the API for the Cloud project the credential belongs '
                    'to, then try again.')
        if 'not_in_channel' in lowered or 'channel_not_found' in lowered:
            return ('Slack authenticated but the bot is not in that channel. '
                    'Invite it with /invite @your-app.')
        if 'missing' in lowered:
            return ('Add the missing settings on the Integrations page, or ask an '
                    'employee to configure it.')
        return ('Read the message above -- it is the service\'s own words, not a '
                'wrapper around them.')
