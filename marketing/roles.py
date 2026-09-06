"""Role-based access control for the AI Workforce application.

HOW THIS WORKS
--------------
This module does not invent a permission system. It uses the one Django
already ships:

    Role  ->  auth.Group  ->  auth.Permission  ->  user.has_perm(...)

Each of the four roles is an `auth.Group`. Each group is granted a set of
`auth.Permission` rows, most of them the add/change/delete/view permissions
Django creates automatically for every model, plus a handful of custom ones
declared in the models' `Meta.permissions`.

Because the checks go through `has_perm`, three things come for free:

  * views can use Django's own `@permission_required` decorator
  * templates can use `{% if perms.marketing.approve_approvalrequest %}`
  * the Django admin shows and edits the whole matrix with no extra code

`Profile.role` stays the human-facing label. A signal keeps the user's group
membership in step with it, so changing a role in one place changes what that
person can actually do everywhere.

THE FOUR ROLES
--------------
    Viewer         Reads everything. Changes nothing.
    Analyst        Viewer, plus talking to AI employees and submitting their
                   output for approval.
    Manager        Analyst, plus approving or rejecting queued work, and
                   managing the AI employees and MCP tool servers.
    Administrator  Manager, plus configuring LLM providers and managing the
                   people who use the platform.

A superuser bypasses all of this: Django's ModelBackend answers True to every
`has_perm` call for `is_superuser`, which is why the seeded `admin` account can
do everything regardless of its group.
"""

ROLE_VIEWER = 'viewer'
ROLE_ANALYST = 'analyst'
ROLE_MANAGER = 'manager'
ROLE_ADMIN = 'owner'

# The group name shown in the Django admin for each role.
GROUP_NAMES = {
    ROLE_VIEWER: 'Viewer',
    ROLE_ANALYST: 'Analyst',
    ROLE_MANAGER: 'Manager',
    ROLE_ADMIN: 'Administrator',
}

# --------------------------------------------------------------------------
# Capability sets, written as "app_label.codename" strings.
#
# Each role includes everything the role above it in this file already had, so
# the sets below are additive and the final matrix is assembled in ROLE_PERMS.
# --------------------------------------------------------------------------

# Read-only access to every part of the workspace.
VIEWER_PERMS = [
    'marketing.view_aiagent',
    'marketing.view_approvalrequest',
    'marketing.view_approvalauditlog',
    'marketing.view_chatmessage',
    'marketing.view_conversation',
    'marketing.view_llmmodel',
    'marketing.view_llmprovider',
    'marketing.view_marketingcampaign',
    'marketing.view_lead',
    'marketing.view_mcpcalllog',
    'marketing.view_mcpserver',
    'marketing.view_mcptool',
    'marketing.view_profile',
    'marketing.view_socialpost',
    'marketing.view_emailoutreach',
    'marketing.view_analyticsmetric',
]

# Talking to employees, and putting their output forward for review.
ANALYST_PERMS = VIEWER_PERMS + [
    'marketing.add_conversation',
    'marketing.change_conversation',
    'marketing.delete_conversation',
    'marketing.add_chatmessage',
    'marketing.add_approvalrequest',
    'marketing.add_marketingcampaign',
    'marketing.change_marketingcampaign',
]

# Deciding what actually gets published, and shaping the workforce.
MANAGER_PERMS = ANALYST_PERMS + [
    'marketing.approve_approvalrequest',
    'marketing.change_approvalrequest',
    'marketing.add_aiagent',
    'marketing.change_aiagent',
    'marketing.delete_aiagent',
    'marketing.add_mcpserver',
    'marketing.change_mcpserver',
    'marketing.delete_mcpserver',
    'marketing.test_mcpserver',
    'marketing.add_mcptool',
    'marketing.change_mcptool',
    'marketing.delete_mcptool',
    'marketing.test_llmprovider',
    'marketing.change_socialpost',
    'marketing.change_emailoutreach',
    'marketing.change_lead',
    'marketing.delete_marketingcampaign',
]

# Everything, including credentials and the people who use the platform.
ADMIN_PERMS = MANAGER_PERMS + [
    'marketing.change_llmprovider',
    'marketing.add_llmmodel',
    'marketing.change_llmmodel',
    'marketing.delete_llmmodel',
    'marketing.assign_role',
    'marketing.change_profile',
    'auth.view_user',
    'auth.add_user',
    'auth.change_user',
    'auth.delete_user',
]

ROLE_PERMS = {
    ROLE_VIEWER: VIEWER_PERMS,
    ROLE_ANALYST: ANALYST_PERMS,
    ROLE_MANAGER: MANAGER_PERMS,
    ROLE_ADMIN: ADMIN_PERMS,
}


# --------------------------------------------------------------------------
# Named capabilities.
#
# Views and templates refer to these constants rather than repeating permission
# strings, so a capability can be renamed or re-scoped in exactly one place.
# --------------------------------------------------------------------------

CAN_CHAT = 'marketing.add_conversation'
CAN_SUBMIT = 'marketing.add_approvalrequest'
CAN_APPROVE = 'marketing.approve_approvalrequest'
CAN_EDIT_AGENT = 'marketing.change_aiagent'
CAN_ADD_AGENT = 'marketing.add_aiagent'
CAN_DELETE_AGENT = 'marketing.delete_aiagent'
CAN_MANAGE_MCP = 'marketing.change_mcpserver'
CAN_ADD_MCP = 'marketing.add_mcpserver'
CAN_DELETE_MCP = 'marketing.delete_mcpserver'
CAN_TEST_MCP = 'marketing.test_mcpserver'
CAN_TEST_PROVIDER = 'marketing.test_llmprovider'
CAN_CONFIGURE_PROVIDER = 'marketing.change_llmprovider'
CAN_MANAGE_USERS = 'auth.change_user'
CAN_ADD_USER = 'auth.add_user'
CAN_DELETE_USER = 'auth.delete_user'
CAN_ASSIGN_ROLE = 'marketing.assign_role'


def sync_groups(apps=None):
    """Create the four groups and (re)apply their permission sets.

    Safe to run repeatedly: permissions are set, not appended, so removing an
    entry from the lists above and running this again withdraws it.

    `apps` lets a migration pass its historical model registry in. Called
    without it, the live models are used.
    """
    if apps is None:
        from django.contrib.auth.models import Group, Permission
    else:
        Group = apps.get_model('auth', 'Group')
        Permission = apps.get_model('auth', 'Permission')

    for role, codenames in ROLE_PERMS.items():
        group, _ = Group.objects.get_or_create(name=GROUP_NAMES[role])
        wanted = []
        for entry in codenames:
            app_label, codename = entry.split('.', 1)
            permission = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename).first()
            if permission is not None:
                wanted.append(permission)
        group.permissions.set(wanted)


def apply_role(user, role):
    """Put a user in exactly the group matching their role.

    Exactly one group, not several: the roles are cumulative by construction,
    so belonging to two of them would only ever be a way to get the matrix
    subtly wrong.
    """
    from django.contrib.auth.models import Group

    names = set(GROUP_NAMES.values())
    user.groups.remove(*user.groups.filter(name__in=names))

    target = GROUP_NAMES.get(role)
    if target:
        group, _ = Group.objects.get_or_create(name=target)
        user.groups.add(group)
