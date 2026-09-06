"""Template context processors for the marketing application.

Registered in marketpulse/settings.py under TEMPLATES -> OPTIONS ->
context_processors, which means the values returned here are available in
*every* template without any view having to pass them.

This is how the sidebar shows a live "pending approvals" count on all six pages
without repeating the same query in six view functions.
"""

from .models import ApprovalRequest, MCPServer, Profile


def workspace_chrome(request):
    """Supply the shared page furniture: theme, profile and sidebar counters."""
    user = getattr(request, 'user', None)

    if user is None or not user.is_authenticated:
        return {'ui_theme': 'light', 'user_profile': None, 'nav_pending_count': 0}

    # get_or_create rather than a plain read: a user created before migration
    # 0004 ran, or through a path that bypassed the signal, still gets a
    # profile instead of crashing every template on the site.
    profile, _ = Profile.objects.get_or_create(user=user)

    return {
        'ui_theme': profile.theme,
        'user_profile': profile,
        'nav_pending_count': ApprovalRequest.objects.filter(
            user=user, status='pending').count(),
        'nav_mcp_online_count': MCPServer.objects.filter(
            user=user, is_enabled=True, connection_status='connected').count(),
    }
