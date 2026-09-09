"""Template context processors for the marketing application.

Registered in marketpulse/settings.py under TEMPLATES -> OPTIONS ->
context_processors, which means the values returned here are available in
*every* template without any view having to pass them.

This is how the sidebar shows live counters on every page without repeating
the same four queries in twenty view functions.

A NOTE ON COST
--------------
Anything added here runs on every single request, including the JSON
endpoints. So the counters below are deliberately `.count()` calls against
indexed columns and nothing more. A processor that assembled a rich summary
would slow down every keystroke in the chat window, which is a strange price
to pay for a number in the margin.
"""

from .models import ApprovalRequest, MCPServer, Profile


def workspace_chrome(request):
    """Supply the shared page furniture: theme, profile and sidebar counters."""
    user = getattr(request, 'user', None)

    if user is None or not user.is_authenticated:
        return {
            'ui_theme': 'light',
            'user_profile': None,
            'nav_pending_count': 0,
            'nav_action_count': 0,
        }

    # get_or_create rather than a plain read: a user created before migration
    # 0004 ran, or through a path that bypassed the signal, still gets a
    # profile instead of crashing every template on the site.
    profile, _ = Profile.objects.get_or_create(user=user)

    return {
        'ui_theme': profile.theme,
        'user_profile': profile,

        # The older text-approval queue. Scoped to this person, as it always
        # was: those rows record who submitted the draft.
        'nav_pending_count': ApprovalRequest.objects.filter(
            user=user, status='pending').count(),

        # The proposed-action queue. NOT scoped to a person: an action that
        # would send mail to a customer is the organisation's business, and a
        # queue only its submitter can see is not an approval workflow.
        'nav_action_count': _pending_actions(),
        'nav_urgent_ticket_count': _urgent_tickets(),
        'nav_integration_live_count': _live_integrations(),

        'nav_mcp_online_count': MCPServer.objects.filter(
            is_enabled=True, connection_status='connected').count(),
    }


def _pending_actions():
    """How many proposed actions are waiting for somebody."""
    from .models_platform import ProposedAction
    return ProposedAction.objects.filter(status='pending').count()


def _urgent_tickets():
    """Open support tickets that need a person, or are marked urgent.

    Both conditions in one number because both mean the same thing to the
    person reading the sidebar: go and look at Support.
    """
    from django.db.models import Q

    from .models_support import SupportTicket
    return SupportTicket.objects.filter(
        Q(needs_human=True) | Q(priority='urgent'),
    ).exclude(status__in=('resolved', 'closed')).count()


def _live_integrations():
    """Connected applications whose last check said they really connect.

    Read from the stored status rather than probed, because probing eleven
    services on every page load would make the whole site as slow as the
    slowest one of them.
    """
    from .models_platform import Integration
    return Integration.objects.filter(
        is_enabled=True, connection_status='connected').count()
