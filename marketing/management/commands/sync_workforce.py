"""Refresh everything the platform generates from code.

    python manage.py sync_workforce [--prompts] [--reindex] [--probe]

Always: the role permission matrix, the Integration rows for every registered
connector, the default settings, and the capability catalogue generated from
the tool registry. Those four go stale whenever the code changes underneath an
existing database, and this is the command that brings them back into step.

--prompts   reapply the employee instructions from marketing/workforce.py.
            This OVERWRITES a prompt somebody edited by hand, so it is opt-in.
--reindex   rebuild every knowledge document's search chunks.
--probe     check every connected application and print a status table.
"""

from django.core.management.base import BaseCommand

from marketing import provisioning, roles


class Command(BaseCommand):
    help = 'Regenerate roles, integrations, settings and capabilities from the code.'

    def add_arguments(self, parser):
        parser.add_argument('--prompts', action='store_true',
                            help='Reapply the system prompts from workforce.py (overwrites edits).')
        parser.add_argument('--reindex', action='store_true',
                            help='Rebuild the knowledge base search index.')
        parser.add_argument('--probe', action='store_true',
                            help='Check every integration connection.')

    def handle(self, *args, **options):
        roles.sync_groups()
        self.stdout.write('Role permission matrix reapplied.')

        created = provisioning.ensure_integrations()
        self.stdout.write(f'Integrations: {len(created)} new row(s) registered.')

        settings_written = provisioning.ensure_settings()
        self.stdout.write(f'Settings: {settings_written} default(s) created.')

        capabilities = provisioning.ensure_capabilities()
        self.stdout.write(f'Capabilities: {capabilities} row(s) written from the registry.')

        if options['prompts']:
            updated = provisioning.refresh_system_prompts()
            self.stdout.write(self.style.WARNING(
                f'Prompts: {updated} employee instruction(s) overwritten from workforce.py.'))

        if options['reindex']:
            try:
                from marketing import knowledge
                report = knowledge.reindex_all()
                self.stdout.write(f'Knowledge: reindexed {report}.')
            except Exception as exc:  # noqa: BLE001 -- report, do not crash
                self.stdout.write(self.style.ERROR(f'Knowledge reindex failed: {exc}'))

        if options['probe']:
            from marketing import integrations
            self.stdout.write('\nCONNECTIONS')
            self.stdout.write(f'  {"INTEGRATION":<18} {"STATUS":<10} {"MODE":<12} MESSAGE')
            self.stdout.write('  ' + '-' * 76)
            for key, result in integrations.probe_all().items():
                style = (self.style.SUCCESS if result['status'] == 'connected'
                         else self.style.WARNING if result['status'] == 'demo'
                         else self.style.ERROR)
                self.stdout.write(style(
                    f'  {key:<18} {result["status"]:<10} {result["effective_mode"]:<12} '
                    f'{result["message"][:60]}'))

        self.stdout.write(self.style.SUCCESS('\nSync complete.'))
