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

# The operational records the workforce produces: recruitment, engineering,
# support, marketing and knowledge. Written as model names so the four roles
# below can be given add/change/delete over the whole set without listing
# sixty permission strings, and so a model added later is covered by adding
# one name here.
OPERATIONAL_MODELS = [
    # Recruitment and people
    'jobopening', 'candidate', 'candidateevaluation', 'interview',
    'employee', 'onboardingtask', 'performancereview', 'hrannouncement',
    # Engineering
    'project', 'sprint', 'workitem', 'workitemcomment', 'sprintreport',
    'codeartifact', 'codereview',
    # Customer support
    'customer', 'supportticket', 'ticketmessage', 'supportreport', 'tickettag',
    # Marketing and content
    'campaignbrief', 'contentpiece', 'contentcalendarentry', 'marketingemail',
    'audiencesegment', 'marketingcampaign', 'socialpost', 'emailoutreach', 'lead',
    # Knowledge
    'knowledgesource', 'knowledgedocument', 'documentchunk',
    'researchreport', 'researchcitation',
]

# Read-only access to every part of the workspace.
#
# A wildcard rather than a list of eighty strings. `sync_groups` expands
# 'marketing.view_*' to every view permission the app has, which means a model
# added to the platform is readable by a Viewer without anyone remembering to
# come back here. Read access is the one place where that default is the safe
# one: this is a single-organisation workspace and the whole point of the audit
# trail is that people can see what happened.
VIEWER_PERMS = [
    'marketing.view_*',
]

# Talking to employees, and doing the day-to-day operational work.
ANALYST_PERMS = VIEWER_PERMS + [
    'marketing.add_conversation',
    'marketing.change_conversation',
    'marketing.delete_conversation',
    'marketing.add_chatmessage',
    'marketing.add_approvalrequest',
    'marketing.add_proposedaction',
    'marketing.add_agenttask',
    'marketing.change_agenttask',
    'marketing.add_agentmemory',
    'marketing.change_agentmemory',
] + [f'marketing.add_{model}' for model in OPERATIONAL_MODELS] \
  + [f'marketing.change_{model}' for model in OPERATIONAL_MODELS]

# Deciding what actually happens, and shaping the workforce.
#
# The approval permission is the important one in the whole file: it is the
# difference between somebody who can ask an employee to draft a message and
# somebody who can let it leave the company.
MANAGER_PERMS = ANALYST_PERMS + [
    'marketing.approve_approvalrequest',
    'marketing.change_approvalrequest',
    'marketing.approve_proposedaction',
    'marketing.change_proposedaction',
    'marketing.delete_proposedaction',
    'marketing.add_aiagent',
    'marketing.change_aiagent',
    'marketing.delete_aiagent',
    'marketing.change_agentcapability',
    'marketing.add_mcpserver',
    'marketing.change_mcpserver',
    'marketing.delete_mcpserver',
    'marketing.test_mcpserver',
    'marketing.add_mcptool',
    'marketing.change_mcptool',
    'marketing.delete_mcptool',
    'marketing.test_llmprovider',
    'marketing.test_integration',
    'marketing.delete_agentmemory',
] + [f'marketing.delete_{model}' for model in OPERATIONAL_MODELS]

# Everything, including credentials, connected applications and the people who
# use the platform.
ADMIN_PERMS = MANAGER_PERMS + [
    'marketing.change_llmprovider',
    'marketing.add_llmmodel',
    'marketing.change_llmmodel',
    'marketing.delete_llmmodel',
    'marketing.configure_integration',
    'marketing.change_integration',
    'marketing.add_integration',
    'marketing.change_systemsetting',
    'marketing.add_systemsetting',
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

# The proposed-action queue, which is where every action that would reach
# outside the company waits. Deciding one of those is a separate capability
# from deciding a piece of drafted text in the older approval queue.
CAN_DECIDE_ACTION = 'marketing.approve_proposedaction'
CAN_EDIT_ACTION = 'marketing.approve_proposedaction'
CAN_VIEW_ACTIONS = 'marketing.view_proposedaction'
CAN_VIEW_AUDIT = 'marketing.view_auditevent'
CAN_CONFIGURE_INTEGRATION = 'marketing.configure_integration'
CAN_TEST_INTEGRATION = 'marketing.test_integration'
CAN_CHANGE_SETTINGS = 'marketing.change_systemsetting'
CAN_MANAGE_KNOWLEDGE = 'marketing.change_knowledgedocument'
CAN_ADD_KNOWLEDGE = 'marketing.add_knowledgedocument'
CAN_MANAGE_OPERATIONS = 'marketing.change_jobopening'
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
        seen = set()
        for entry in codenames:
            app_label, codename = entry.split('.', 1)

            # A trailing '*' means every permission with that prefix in the
            # app, which is how 'marketing.view_*' grants read access to
            # models that did not exist when this file was written.
            if codename.endswith('*'):
                matches = Permission.objects.filter(
                    content_type__app_label=app_label,
                    codename__startswith=codename[:-1])
            else:
                matches = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename)

            for permission in matches:
                if permission.pk not in seen:
                    seen.add(permission.pk)
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
