"""Signal receivers for the marketing application.

Registered from MarketingConfig.ready() in apps.py.

Three receivers:

1. Every auth.User must have a Profile. The base template reads
   `user.profile.theme`, so a missing profile would raise
   RelatedObjectDoesNotExist and blank the whole page. Belt and braces are used
   deliberately: this signal covers users created from now on, migration 0004
   backfilled the ones that already existed, and agent_engine.ensure_profile()
   covers anything that slips past both.

2. A Profile's role must stay in step with the user's Django group. `role` is
   the human-facing label; the group is what actually carries the permissions.
   Letting them drift would mean the Users page showed one thing while the
   application enforced another.

3. The role groups must be given their permissions AFTER Django has created
   the Permission rows. Django creates those in its own post_migrate receiver,
   so a data migration cannot do it: on a fresh database the migration runs
   first and finds nothing to assign, leaving every group empty. Hooking
   post_migrate is the only ordering that is correct on both a fresh install
   and an existing one.
"""

from django.contrib.auth.models import User
from django.db.models.signals import post_migrate, post_save
from django.dispatch import receiver

from . import roles
from .models import Profile


@receiver(post_save, sender=User, dispatch_uid='marketing_create_user_profile')
def create_user_profile(sender, instance, created, raw=False, **kwargs):
    """Create the matching Profile row whenever a User is created.

    `raw` is True during loaddata, when related tables may not exist yet, so
    the receiver stands down in that case.
    """
    if raw:
        return
    if created:
        Profile.objects.get_or_create(user=instance)


@receiver(post_save, sender=Profile, dispatch_uid='marketing_sync_role_group')
def sync_role_group(sender, instance, raw=False, **kwargs):
    """Move the user into the Django group that matches their role.

    This is what turns `Profile.role` from a label into an enforceable
    permission set: changing the role on the Users page, in the Django admin,
    or from a shell all take effect immediately and identically, because they
    all end in a Profile save.
    """
    if raw:
        return
    roles.apply_role(instance.user, instance.role)


@receiver(post_migrate, dispatch_uid='marketing_sync_role_groups')
def sync_role_groups(sender, **kwargs):
    """Create the role groups and apply their permissions after every migrate.

    Only reacts to this application's own post_migrate, so it runs once rather
    than once per installed app. By this point Django's own receiver has
    created every Permission row, including the custom ones declared in the
    models' Meta.permissions, so the matrix in roles.py can be applied in full.
    """
    if getattr(sender, 'name', None) != 'marketing':
        return
    roles.sync_groups()
