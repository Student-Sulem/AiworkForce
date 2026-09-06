"""Data migration: create the four role groups and place existing users in them.

The permission matrix itself lives in marketing/roles.py and is applied by the
post_migrate receiver in marketing/signals.py, not here. That ordering matters:
Django creates its Permission rows in its own post_migrate receiver, so a
migration that tried to assign them would find none and leave every group
empty -- which only shows up on a fresh database, and locks everybody out when
it does.

So this migration does two smaller things: it creates the four groups, and it
puts the users that already exist into the one matching their profile role.
"""

from django.db import migrations

from marketing import roles


def create_groups(apps, schema_editor):
    # The groups are created here, but their PERMISSIONS are not: Django has
    # not created the Permission rows yet at migration time, so anything
    # assigned here would silently be nothing. The marketing app's
    # post_migrate receiver in signals.py applies the matrix once those rows
    # exist. This migration only has to place existing users in the right
    # group.
    Group = apps.get_model('auth', 'Group')
    for name in roles.GROUP_NAMES.values():
        Group.objects.get_or_create(name=name)
    Profile = apps.get_model('marketing', 'Profile')

    groups_by_name = {group.name: group for group in Group.objects.all()}

    for profile in Profile.objects.select_related('user'):
        target = groups_by_name.get(roles.GROUP_NAMES.get(profile.role, ''))
        if target is None:
            continue
        # roles.apply_role() cannot be used here: it imports the live Group
        # model, which a migration must not touch.
        profile.user.groups.remove(
            *profile.user.groups.filter(name__in=roles.GROUP_NAMES.values()))
        profile.user.groups.add(target)


def remove_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name__in=roles.GROUP_NAMES.values()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('marketing', '0007_rbac_and_shared_workspace'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(create_groups, remove_groups),
    ]
