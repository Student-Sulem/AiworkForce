"""URL configuration for the marketing application.

Included from marketpulse/urls.py, so every path below is mounted at the site
root. Each path() maps one URL pattern to exactly one view in views.py and
gives it a name; templates then link with {% url 'name' %} rather than a
hard-coded path, so a route can be changed in one place.

NOTE: this module deliberately does NOT define `app_name`. Adding a namespace
would turn every {% url 'dashboard' %} in the templates into
{% url 'marketing:dashboard' %}, and every existing tag would raise
NoReverseMatch until all of them were rewritten.
"""

from django.urls import path

from . import views

urlpatterns = [
    # ------------------------------------------------------------------
    # Public and authentication
    # ------------------------------------------------------------------
    path('', views.landing_page, name='landing'),
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),

    # ------------------------------------------------------------------
    # The six workspace pages, plus the create/edit/delete routes that go
    # with them. All require authentication; the ones that change something
    # also require a permission, enforced in the view itself.
    #
    # Every delete is POST-only, so no link, prefetch or crawler can ever
    # trigger one.
    # ------------------------------------------------------------------
    path('workspace/', views.dashboard_view, name='dashboard'),

    # The AI Employees page is a chat screen. With no conversation in the URL
    # it opens the most recent thread; the second pattern addresses one thread
    # directly, which is what the conversation rail links to.
    path('agents/', views.agents_view, name='agents'),
    path('agents/c/<int:conversation_pk>/', views.agents_view, name='conversation'),
    path('agents/new/', views.agent_create_view, name='agent_create'),
    path('agents/<int:pk>/delete/', views.agent_delete_view, name='agent_delete'),

    path('approvals/', views.approvals_view, name='approvals'),
    path('approvals/<int:pk>/', views.approval_detail_view, name='approval_detail'),

    path('users/', views.users_view, name='users'),
    path('users/new/', views.user_create_view, name='user_create'),
    path('users/<int:pk>/edit/', views.user_edit_view, name='user_edit'),
    path('users/<int:pk>/delete/', views.user_delete_view, name='user_delete'),

    path('configurations/', views.configurations_view, name='configurations'),

    path('mcp-tools/', views.mcp_tools_view, name='mcp_tools'),
    path('mcp-tools/new/', views.mcp_server_create_view, name='mcp_server_create'),
    path('mcp-tools/<int:pk>/', views.mcp_server_detail_view, name='mcp_server_detail'),
    path('mcp-tools/<int:pk>/delete/', views.mcp_server_delete_view, name='mcp_server_delete'),
    path('mcp-tools/tool/<int:pk>/delete/', views.mcp_tool_delete_view, name='mcp_tool_delete'),

    # ------------------------------------------------------------------
    # JSON endpoints. POST only, login required, CSRF enforced.
    # Consumed by fetch() from the modules in static/js/.
    # ------------------------------------------------------------------
    path('api/edit-agent/', views.api_edit_agent, name='api_edit_agent'),

    # Chat
    path('api/chat/send/', views.api_send_message, name='api_send_message'),
    path('api/chat/new/', views.api_new_conversation, name='api_new_conversation'),
    path('api/chat/rename/', views.api_rename_conversation, name='api_rename_conversation'),
    path('api/chat/delete/', views.api_delete_conversation, name='api_delete_conversation'),
    path('api/chat/submit/', views.api_submit_for_approval, name='api_submit_for_approval'),
    path('api/chat/send-now/', views.api_send_now, name='api_send_now'),

    path('api/approvals/decide/', views.api_approval_decision, name='api_approval_decision'),
    path('api/approvals/bulk/', views.api_bulk_approval, name='api_bulk_approval'),

    path('api/providers/test/', views.api_test_provider, name='api_test_provider'),
    path('api/providers/fetch-models/', views.api_fetch_models, name='api_fetch_models'),
    path('api/providers/models/', views.api_provider_models, name='api_provider_models'),

    path('api/email/test/', views.api_test_email, name='api_test_email'),
    path('api/email/send-test/', views.api_send_test_email, name='api_send_test_email'),

    path('api/mcp/test/', views.api_test_mcp_server, name='api_test_mcp_server'),
    path('api/mcp/toggle/', views.api_toggle_mcp_server, name='api_toggle_mcp_server'),

    path('api/users/toggle-active/', views.api_toggle_user_active, name='api_toggle_user_active'),
    path('api/users/set-staff/', views.api_set_user_staff, name='api_set_user_staff'),

    path('api/preferences/theme/', views.api_set_theme, name='api_set_theme'),
]
