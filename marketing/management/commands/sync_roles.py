"""Re-apply the role permission matrix defined in marketing/roles.py.

Run this after editing the permission lists in roles.py:

    python manage.py sync_roles

It is idempotent, and it *sets* rather than appends, so a permission removed
from a role in roles.py is withdrawn from that group here.
"""

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from marketing import roles


class Command(BaseCommand):
    help = 'Create the role groups and re-apply their permissions from roles.py.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--resync-users', action='store_true',
            help="Also put every user back in the group matching their profile role.")

    def handle(self, *args, **options):
        roles.sync_groups()
        self.stdout.write(self.style.SUCCESS('Role groups synchronised.'))

        for role, name in roles.GROUP_NAMES.items():
            group = Group.objects.get(name=name)
            self.stdout.write(
                f'  {name:15s} {group.permissions.count():3d} permissions  '
                f'{group.user_set.count():2d} users')

        if options['resync_users']:
            moved = 0
            for user in User.objects.select_related('profile'):
                profile = getattr(user, 'profile', None)
                if profile is not None:
                    roles.apply_role(user, profile.role)
                    moved += 1
            self.stdout.write(self.style.SUCCESS(
                f'\n{moved} users placed in the group matching their role.'))
