"""Django admin configuration for the AI Workforce application.

AIAgentAdmin is the flagship customised admin required by the brief. AIAgent is
the model behind the AI Employees page, and after the redesign it carries a
foreign key (llm_model), a many-to-many through a custom model (mcp_tools via
AgentToolLink), a reverse relation (approval_requests), numeric performance
fields, and colour and icon fields. That makes it the one model that can
demonstrate every admin technique without contrivance:

    fieldsets            grouped and collapsible edit form
    inlines              AgentToolLink (editable) and ApprovalRequest (read-only)
    list_display         with custom @admin.display methods rendering HTML
    list_editable        change status and is_active straight from the list
    list_filter          including a relation traversal and a date filter
    search_fields        across the model and its owner
    autocomplete_fields  for the user and LLM foreign keys
    readonly_fields      computed summaries, not stored columns
    actions              four bulk operations
    get_queryset         select_related + annotate, and per-user scoping
    save_model           assigns the owner and generates a default prompt
    save_formset         stamps who attached each tool

The remaining models get lighter, conventional registrations.
"""

from django.contrib import admin, messages
from django.db.models import Count, Q
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .models import (AgentToolLink, AIAgent, AnalyticsMetric, ApprovalAuditLog,
                     ApprovalRequest, ChatMessage, Conversation, EmailOutreach,
                     Lead, LLMModel, LLMProvider, MarketingCampaign, MCPCallLog,
                     MCPServer, MCPTool, Profile, SocialPost)

admin.site.site_header = 'AI Workforce Administration'
admin.site.site_title = 'AI Workforce Admin'
admin.site.index_title = 'Autonomous Marketing Control Centre'


# ===========================================================================
# Inlines used by the flagship admin
# ===========================================================================

class AgentToolLinkInline(admin.TabularInline):
    """Edit which MCP tools an employee may use, without leaving its page."""

    model = AgentToolLink
    extra = 1
    autocomplete_fields = ('tool',)
    fields = ('tool', 'is_enabled', 'usage_count', 'attached_by', 'attached_at', 'notes')
    readonly_fields = ('usage_count', 'attached_by', 'attached_at')
    verbose_name = 'Attached MCP tool'
    verbose_name_plural = 'Attached MCP tools'


class ApprovalRequestInline(admin.TabularInline):
    """Read-only history of what this employee has submitted for review.

    max_num=0 removes the "add another" row, and every field is read-only, so
    this inline reports rather than edits.
    """

    model = ApprovalRequest
    fk_name = 'agent'
    extra = 0
    max_num = 0
    can_delete = False
    show_change_link = True
    fields = ('title', 'item_type', 'status', 'generation_source', 'requested_at', 'decided_by')
    readonly_fields = fields
    verbose_name_plural = 'Recent output submitted for approval'


class ConversationInline(admin.TabularInline):
    """Read-only list of the chat threads held with this employee."""

    model = Conversation
    fk_name = 'agent'
    extra = 0
    max_num = 0
    can_delete = False
    show_change_link = True
    fields = ('title', 'user', 'message_count', 'updated_at')
    readonly_fields = fields
    verbose_name_plural = 'Conversations'

    @admin.display(description='Messages')
    def message_count(self, obj):
        return obj.message_count


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    max_num = 0
    can_delete = False
    fields = ('created_at', 'role', 'content', 'generation_source', 'llm_model_used')
    readonly_fields = fields


class MCPToolInline(admin.TabularInline):
    model = MCPTool
    extra = 1
    fields = ('tool_name', 'display_name', 'is_enabled', 'is_destructive', 'call_count')
    readonly_fields = ('call_count',)


class LLMModelInline(admin.TabularInline):
    model = LLMModel
    extra = 0
    fields = ('model_id', 'display_name', 'context_length', 'source', 'is_enabled')
    readonly_fields = ('source',)


class ApprovalAuditLogInline(admin.TabularInline):
    model = ApprovalAuditLog
    extra = 0
    max_num = 0
    can_delete = False
    fields = ('created_at', 'actor', 'action', 'from_status', 'to_status', 'note')
    readonly_fields = fields
    verbose_name_plural = 'Audit trail'


# ===========================================================================
# FLAGSHIP: the AI Employee admin
# ===========================================================================

@admin.register(AIAgent)
class AIAgentAdmin(admin.ModelAdmin):
    """Fully customised admin for the AI Employee model."""

    # --- Change list -------------------------------------------------------
    list_display = ('avatar_swatch', 'name', 'role', 'agent_type', 'owner_link',
                    'llm_badge', 'tool_count', 'performance_bar', 'tasks_completed',
                    'status', 'is_active')
    list_display_links = ('avatar_swatch', 'name')
    list_editable = ('status', 'is_active')
    list_filter = ('agent_type', 'status', 'is_active',
                   'llm_model__provider__provider_key',
                   ('created_at', admin.DateFieldListFilter))
    search_fields = ('name', 'role', 'persona_description', 'system_prompt',
                     'user__username', 'user__email')
    date_hierarchy = 'created_at'
    list_per_page = 25
    list_select_related = ('user', 'llm_model')
    save_on_top = True
    empty_value_display = 'Not set'

    # --- Change form -------------------------------------------------------
    autocomplete_fields = ('user', 'llm_model')
    readonly_fields = ('created_at', 'updated_at', 'tasks_completed',
                       'tool_summary', 'prompt_preview', 'approval_stats')
    inlines = [AgentToolLinkInline, ConversationInline, ApprovalRequestInline]
    actions = ['action_activate', 'action_set_idle', 'action_reset_counters',
               'action_regenerate_prompts']

    fieldsets = (
        ('Identity', {
            'fields': ('user', 'name', 'role', 'agent_type',
                       ('avatar_icon', 'avatar_color'), ('is_active', 'status')),
        }),
        ('Persona and system prompt', {
            'classes': ('collapse',),
            'description': 'The system prompt is sent verbatim as the "system" role '
                           'on every language-model call this employee makes.',
            'fields': ('persona_description', 'system_prompt', 'prompt_preview'),
        }),
        ('Language model configuration', {
            'fields': ('llm_model', ('temperature', 'max_tokens')),
        }),
        ('MCP capability', {
            'classes': ('collapse',),
            'description': 'Edit the attachments in the table below the form.',
            'fields': ('tool_summary',),
        }),
        ('Performance and audit', {
            'classes': ('collapse',),
            'fields': (('tasks_completed', 'success_rate'), 'approval_stats',
                       ('created_at', 'updated_at')),
        }),
    )

    # --- Custom columns ----------------------------------------------------

    @admin.display(description='')
    def avatar_swatch(self, obj):
        """A coloured initial, so the list is scannable at a glance."""
        return format_html(
            '<span style="display:inline-block;width:28px;height:28px;border-radius:50%;'
            'background:{};color:#fff;text-align:center;line-height:28px;'
            'font-weight:700;font-size:12px;">{}</span>',
            obj.avatar_color, obj.name[:1].upper())

    @admin.display(description='Owner', ordering='user__username')
    def owner_link(self, obj):
        if not obj.user_id:
            return 'Unassigned'
        url = reverse('admin:auth_user_change', args=[obj.user_id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description='Assigned LLM', ordering='llm_model__display_name')
    def llm_badge(self, obj):
        if not obj.llm_model_id:
            # format_html() requires at least one argument, so a constant
            # fragment goes through mark_safe instead.
            return mark_safe('<span style="color:#94a3b8;">template fallback</span>')
        return format_html('<code>{}</code><br><small>{}</small>',
                           obj.llm_model.model_id,
                           obj.llm_model.provider.get_provider_key_display())

    @admin.display(description='MCP tools', ordering='_tool_count')
    def tool_count(self, obj):
        # Reads the annotation added in get_queryset, so this costs no query.
        return getattr(obj, '_tool_count', 0)

    @admin.display(description='Success rate', ordering='success_rate')
    def performance_bar(self, obj):
        percent = float(obj.success_rate)
        colour = '#047857' if percent >= 95 else '#b45309' if percent >= 85 else '#b91c1c'
        return format_html(
            '<div style="background:#e5e7eb;width:90px;height:8px;border-radius:4px;">'
            '<div style="background:{};width:{}px;height:8px;border-radius:4px;"></div>'
            '</div><small>{}%</small>',
            colour, round(percent * 0.9), percent)

    # --- Computed read-only fields ----------------------------------------

    @admin.display(description='Attached tools')
    def tool_summary(self, obj):
        links = list(obj.tool_links.select_related('tool', 'tool__server'))
        if not links:
            return 'No MCP tools are attached to this employee.'
        return format_html_join(
            mark_safe('<br>'), '<strong>{}</strong> &rarr; {} ({})',
            ((link.tool.server.name, link.tool.tool_name,
              'enabled' if link.is_enabled else 'disabled') for link in links))

    @admin.display(description='System prompt preview')
    def prompt_preview(self, obj):
        text = obj.effective_system_prompt
        clipped = text[:600] + ('...' if len(text) > 600 else '')
        return format_html(
            '<pre style="white-space:pre-wrap;max-width:640px;background:#f8fafc;'
            'padding:10px;border-radius:6px;border:1px solid #e2e8f0;">{}</pre>', clipped)

    @admin.display(description='Approval outcomes')
    def approval_stats(self, obj):
        totals = obj.approval_requests.aggregate(
            pending=Count('id', filter=Q(status='pending')),
            approved=Count('id', filter=Q(status='approved')),
            rejected=Count('id', filter=Q(status='rejected')))
        return format_html('Pending {} &middot; Approved {} &middot; Rejected {}',
                           totals['pending'], totals['approved'], totals['rejected'])

    # --- Overrides ---------------------------------------------------------

    def get_queryset(self, request):
        """Fetch related rows up front and annotate the tool count.

        Non-superuser staff see only the employees they own, which mirrors the
        per-user isolation the front-end views enforce.
        """
        queryset = (super().get_queryset(request)
                    .select_related('user', 'llm_model', 'llm_model__provider')
                    .annotate(_tool_count=Count('mcp_tools', distinct=True)))
        if not request.user.is_superuser:
            queryset = queryset.filter(user=request.user)
        return queryset

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Never offer another user's language models in the dropdown."""
        if db_field.name == 'llm_model' and not request.user.is_superuser:
            kwargs['queryset'] = LLMModel.objects.filter(provider__user=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        """Assign an owner on creation and guarantee a usable system prompt."""
        if obj.user_id is None:
            obj.user = request.user
        if not obj.system_prompt.strip():
            obj.system_prompt = (f"You are {obj.name}, a {obj.role}. "
                                 f"{obj.persona_description}")
            self.message_user(
                request, f'A default system prompt was generated for {obj.name}.',
                level=messages.INFO)
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        """Record who attached each MCP tool."""
        instances = formset.save(commit=False)
        for instance in instances:
            if isinstance(instance, AgentToolLink) and instance.attached_by_id is None:
                instance.attached_by = request.user
            instance.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()

    # --- Bulk actions ------------------------------------------------------

    @admin.action(description='Mark selected employees as active')
    def action_activate(self, request, queryset):
        updated = queryset.update(status='active', is_active=True)
        self.message_user(request, f'{updated} employees are now active.',
                          level=messages.SUCCESS)

    @admin.action(description='Mark selected employees as idle')
    def action_set_idle(self, request, queryset):
        updated = queryset.update(status='idle')
        self.message_user(request, f'{updated} employees were set to idle.')

    @admin.action(description='Reset task counters to zero')
    def action_reset_counters(self, request, queryset):
        updated = queryset.update(tasks_completed=0)
        self.message_user(request, f'Task counters were reset for {updated} employees.',
                          level=messages.WARNING)

    @admin.action(description='Regenerate system prompts from the persona')
    def action_regenerate_prompts(self, request, queryset):
        count = 0
        for agent in queryset:
            agent.system_prompt = (f"You are {agent.name}, a {agent.role}. "
                                   f"{agent.persona_description}")
            agent.save(update_fields=['system_prompt'])
            count += 1
        self.message_user(request, f'{count} system prompts were regenerated.')


# ===========================================================================
# Supporting admins
# ===========================================================================

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'job_title', 'theme', 'last_activity')
    list_filter = ('role', 'theme')
    search_fields = ('user__username', 'user__email', 'job_title')
    autocomplete_fields = ('user',)
    readonly_fields = ('created_at', 'updated_at', 'last_activity')


@admin.register(LLMProvider)
class LLMProviderAdmin(admin.ModelAdmin):
    list_display = ('get_provider_key_display', 'user', 'masked_key', 'is_enabled',
                    'connection_status', 'model_count', 'last_tested_at')
    list_filter = ('provider_key', 'is_enabled', 'connection_status')
    search_fields = ('display_name', 'base_url', 'user__username')
    autocomplete_fields = ('user',)
    readonly_fields = ('masked_key', 'last_tested_at', 'last_test_message',
                       'last_latency_ms', 'models_synced_at', 'created_at', 'updated_at')
    inlines = [LLMModelInline]
    fieldsets = (
        ('Provider', {'fields': ('user', 'provider_key', 'display_name', 'is_enabled')}),
        ('Connection', {'fields': ('base_url', 'api_key', 'masked_key')}),
        ('Last test', {'classes': ('collapse',),
                       'fields': ('connection_status', 'last_test_message',
                                  'last_latency_ms', 'last_tested_at', 'models_synced_at')}),
    )

    @admin.display(description='Stored key')
    def masked_key(self, obj):
        return obj.masked_key


@admin.register(LLMModel)
class LLMModelAdmin(admin.ModelAdmin):
    list_display = ('model_id', 'display_name', 'provider', 'context_length',
                    'source', 'is_enabled')
    list_filter = ('provider__provider_key', 'source', 'is_enabled')
    # search_fields is required here because AIAgentAdmin autocompletes this model.
    search_fields = ('model_id', 'display_name', 'description')
    list_editable = ('is_enabled',)


@admin.register(MCPServer)
class MCPServerAdmin(admin.ModelAdmin):
    list_display = ('name', 'server_key', 'user', 'category', 'transport',
                    'tool_count', 'is_enabled', 'connection_status', 'last_handshake_at')
    list_filter = ('category', 'transport', 'is_enabled', 'connection_status')
    search_fields = ('name', 'server_key', 'description', 'command', 'endpoint_url')
    autocomplete_fields = ('user',)
    list_editable = ('is_enabled',)
    readonly_fields = ('last_handshake_at', 'last_status_message', 'created_at')
    inlines = [MCPToolInline]
    fieldsets = (
        ('Server', {'fields': ('user', 'server_key', 'name', 'description',
                               'category', ('icon', 'color'), 'is_enabled')}),
        ('Connection', {'fields': ('transport', 'command', 'args', 'endpoint_url',
                                   'auth_token', 'protocol_version', 'config')}),
        ('Last handshake', {'classes': ('collapse',),
                            'fields': ('connection_status', 'last_status_message',
                                       'last_handshake_at', 'created_at')}),
    )

    @admin.display(description='Tools')
    def tool_count(self, obj):
        return obj.tool_count


@admin.register(MCPTool)
class MCPToolAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'tool_name', 'server', 'is_enabled',
                    'is_destructive', 'call_count')
    list_filter = ('server__server_key', 'is_enabled', 'is_destructive')
    # search_fields is required here because AgentToolLinkInline autocompletes it.
    search_fields = ('tool_name', 'display_name', 'description', 'server__name')
    list_editable = ('is_enabled',)
    readonly_fields = ('call_count',)


@admin.register(AgentToolLink)
class AgentToolLinkAdmin(admin.ModelAdmin):
    list_display = ('agent', 'tool', 'is_enabled', 'usage_count', 'attached_by', 'attached_at')
    list_filter = ('is_enabled', 'tool__server__server_key')
    search_fields = ('agent__name', 'tool__tool_name')
    autocomplete_fields = ('agent', 'tool')
    readonly_fields = ('attached_at',)


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('title', 'agent', 'user', 'message_count', 'updated_at')
    list_filter = ('agent__agent_type', 'is_archived', ('updated_at', admin.DateFieldListFilter))
    search_fields = ('title', 'user__username', 'agent__name', 'messages__content')
    autocomplete_fields = ('user', 'agent')
    date_hierarchy = 'updated_at'
    readonly_fields = ('created_at', 'updated_at')
    inlines = [ChatMessageInline]
    list_select_related = ('agent', 'user')

    @admin.display(description='Messages')
    def message_count(self, obj):
        return obj.message_count


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'conversation', 'role', 'short_content',
                    'generation_source', 'llm_model_used', 'tokens_used')
    list_filter = ('role', 'generation_source', ('created_at', admin.DateFieldListFilter))
    search_fields = ('content', 'conversation__title')
    readonly_fields = ('created_at',)
    list_select_related = ('conversation',)

    @admin.display(description='Content')
    def short_content(self, obj):
        return obj.content[:80] + ('...' if len(obj.content) > 80 else '')


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ('title', 'item_type', 'agent', 'user', 'status', 'priority',
                    'generation_source', 'requested_at', 'decided_by')
    list_filter = ('status', 'item_type', 'priority', 'generation_source',
                   'social_post__platform', ('requested_at', admin.DateFieldListFilter))
    search_fields = ('title', 'payload_preview', 'decision_reason', 'user__username')
    autocomplete_fields = ('user', 'agent')
    date_hierarchy = 'requested_at'
    readonly_fields = ('requested_at', 'decided_at', 'tokens_used', 'llm_model_used',
                       'source_message')
    inlines = [ApprovalAuditLogInline]
    list_select_related = ('agent', 'user', 'decided_by')

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('agent', 'user', 'decided_by', 'social_post',
                                'email_outreach', 'lead'))


@admin.register(ApprovalAuditLog)
class ApprovalAuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'approval', 'actor', 'action', 'from_status', 'to_status')
    list_filter = ('action', ('created_at', admin.DateFieldListFilter))
    search_fields = ('approval__title', 'note', 'actor__username')
    readonly_fields = ('created_at',)


@admin.register(MCPCallLog)
class MCPCallLogAdmin(admin.ModelAdmin):
    list_display = ('called_at', 'agent', 'tool', 'outcome', 'duration_ms', 'result_summary')
    list_filter = ('outcome', 'tool__server__server_key')
    search_fields = ('result_summary', 'agent__name', 'tool__tool_name')
    readonly_fields = ('called_at',)


@admin.register(MarketingCampaign)
class MarketingCampaignAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'status', 'target_audience', 'budget',
                    'assigned_agent', 'start_date')
    list_filter = ('status', 'start_date')
    search_fields = ('name', 'target_audience', 'objective')
    autocomplete_fields = ('user', 'assigned_agent')
    list_editable = ('status',)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'company', 'email', 'user', 'industry',
                    'lead_score', 'score_tier', 'status', 'created_at')
    list_filter = ('score_tier', 'status', 'industry')
    search_fields = ('full_name', 'email', 'company', 'job_title')
    autocomplete_fields = ('user', 'campaign', 'discovered_by')
    list_editable = ('status',)
    date_hierarchy = 'created_at'


@admin.register(SocialPost)
class SocialPostAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'platform', 'status', 'likes', 'shares',
                    'comments', 'published_at', 'created_at')
    list_filter = ('platform', 'status')
    search_fields = ('title', 'content', 'hashtags')
    autocomplete_fields = ('user', 'campaign', 'agent')
    list_editable = ('status',)
    date_hierarchy = 'created_at'


@admin.register(EmailOutreach)
class EmailOutreachAdmin(admin.ModelAdmin):
    list_display = ('subject', 'lead', 'user', 'sequence_step', 'status', 'sent_at')
    list_filter = ('status', 'sequence_step')
    search_fields = ('subject', 'body', 'lead__email', 'lead__full_name')
    autocomplete_fields = ('user', 'campaign', 'lead', 'agent')
    list_editable = ('status',)


@admin.register(AnalyticsMetric)
class AnalyticsMetricAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'category', 'value', 'change_percent',
                    'is_positive', 'metric_date')
    list_filter = ('category', 'is_positive')
    search_fields = ('title', 'value')
    autocomplete_fields = ('user',)


# ===========================================================================
# The workforce, domain and platform models are registered in a sibling
# module. The platform grew from seventeen models to sixty, and a single
# admin file holding all of them would be fifteen hundred lines that nobody
# can navigate -- so the split follows the one already made in the model
# layer, where models.py holds the original schema and models_platform,
# models_hr, models_eng, models_support, models_knowledge and models_content
# hold the rest.
#
# The import sits at the very bottom rather than the top because
# admin_workforce reuses the inlines and the flagship conventions defined
# above. By the time execution reaches here every inline class in this file
# is already defined, so the sibling module can rely on them.
# ===========================================================================

from . import admin_workforce  # noqa: E402,F401
