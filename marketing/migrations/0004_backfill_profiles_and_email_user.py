"""Data migration: backfill rows that existed before the new schema landed.

Two jobs:

1. Every auth.User needs a Profile. The post_save signal in signals.py only
   fires for users created *after* it was registered, so the accounts already
   in the database need one written by hand.
2. EmailOutreach gained a `user` column. Existing rows can be attributed by
   following the lead they were sent to.

Both operations are written with a reverse function so the migration can be
rolled back, and both use the historical model classes supplied by `apps`
rather than importing from marketing.models directly -- that is what keeps a
data migration correct even after the real models change again later.
"""

from django.db import migrations


def create_missing_profiles(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    Profile = apps.get_model('marketing', 'Profile')

    existing = set(Profile.objects.values_list('user_id', flat=True))
    Profile.objects.bulk_create([
        Profile(
            user_id=user_id,
            # The first account created is treated as the workspace owner;
            # everyone else starts as a manager.
            role='owner' if is_superuser else 'manager',
            theme='light',
            avatar_icon='fa-circle-user',
            avatar_color='#4f46e5',
        )
        for user_id, is_superuser in User.objects.values_list('id', 'is_superuser')
        if user_id not in existing
    ])


def drop_profiles(apps, schema_editor):
    apps.get_model('marketing', 'Profile').objects.all().delete()


def backfill_email_owner(apps, schema_editor):
    EmailOutreach = apps.get_model('marketing', 'EmailOutreach')
    for email in EmailOutreach.objects.filter(user__isnull=True).select_related('lead'):
        if email.lead_id and email.lead.user_id:
            EmailOutreach.objects.filter(pk=email.pk).update(user_id=email.lead.user_id)


def clear_email_owner(apps, schema_editor):
    apps.get_model('marketing', 'EmailOutreach').objects.update(user=None)


class Migration(migrations.Migration):

    dependencies = [
        ('marketing', '0003_workforce_platform'),
    ]

    operations = [
        migrations.RunPython(create_missing_profiles, drop_profiles),
        migrations.RunPython(backfill_email_owner, clear_email_owner),
    ]
