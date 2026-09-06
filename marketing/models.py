"""Database models for the AI Workforce application.

The schema is organised in five layers:

1. Identity      -- Profile extends the built-in auth.User.
2. Intelligence  -- LLMProvider and LLMModel describe *which* language model an
                    AI employee thinks with (OpenRouter, NVIDIA NIM, Ollama).
3. Capability    -- MCPServer, MCPTool, AgentToolLink and MCPCallLog describe
                    *what an employee can do* through Model Context Protocol
                    tool servers.
4. Workforce     -- AIAgent is the AI employee itself; MarketingCampaign, Lead,
                    SocialPost, EmailOutreach and AnalyticsMetric are the work
                    it produces.
5. Governance    -- ApprovalRequest and ApprovalAuditLog implement the
                    human-in-the-loop rule: agents propose, people dispose.

TENANCY
-------
This application serves ONE organisation, so the workspace is shared: campaigns,
prospects, AI employees, providers, MCP servers and the approval queue are
visible to everyone who signs in. The `user` foreign key on those models records
*who created the row*, as provenance; it is deliberately no longer an access
boundary.

What a person may DO with that shared data is decided by their role, through
Django's own Group and Permission system. See marketing/roles.py. The custom
permissions those roles hand out are declared in the `Meta.permissions` of the
models below.

Chat threads are the one exception: a Conversation belongs to whoever started
it, and other people do not read it.
"""

import os

from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


# ===========================================================================
# 1. IDENTITY
# ===========================================================================

class Profile(models.Model):
    """Extra attributes attached to a Django user.

    A OneToOneField is used rather than a custom AUTH_USER_MODEL because the
    user model cannot be swapped after the first migration has been applied,
    and this project already has live data keyed to auth.User.
    """

    ROLE_CHOICES = [
        ('owner', 'Workspace Owner'),
        ('manager', 'Marketing Manager'),
        ('analyst', 'Analyst'),
        ('viewer', 'Viewer'),
    ]
    THEME_CHOICES = [('light', 'Light'), ('dark', 'Dark')]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='owner')
    job_title = models.CharField(max_length=120, blank=True)
    avatar_icon = models.CharField(max_length=50, default='fa-circle-user')
    avatar_color = models.CharField(max_length=20, default='#4f46e5')
    theme = models.CharField(max_length=10, choices=THEME_CHOICES, default='light')
    bio = models.TextField(blank=True)
    last_activity = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['user__username']
        verbose_name = 'User Profile'
        verbose_name_plural = 'User Profiles'
        permissions = [
            ('assign_role', 'Can change which role a user holds'),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    @property
    def display_name(self):
        return self.user.get_full_name() or self.user.username

    @property
    def initials(self):
        source = self.user.get_full_name() or self.user.username
        parts = [p for p in source.split() if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return source[:2].upper()

    @property
    def status_label(self):
        return 'Active' if self.user.is_active else 'Suspended'

    @property
    def access_level(self):
        if self.user.is_superuser:
            return 'Superuser'
        return 'Staff' if self.user.is_staff else self.get_role_display()

    def touch(self):
        """Stamp a last-seen time without disturbing `updated_at`."""
        now = timezone.now()
        Profile.objects.filter(pk=self.pk).update(last_activity=now)
        self.last_activity = now


# ===========================================================================
# 2. INTELLIGENCE -- language model providers and their catalogues
# ===========================================================================

class LLMProvider(models.Model):
    """One configured language-model provider belonging to one user.

    `base_url` is a CharField rather than a URLField on purpose: URLValidator
    is fussy about local addresses such as http://localhost:11434, which is
    exactly what Ollama uses.
    """

    PROVIDER_CHOICES = [
        ('openrouter', 'OpenRouter'),
        ('nvidia', 'NVIDIA NIM'),
        ('ollama', 'Ollama (Local)'),
    ]
    STATUS_CHOICES = [
        ('untested', 'Not Tested'),
        ('online', 'Online'),
        ('unauthorized', 'Invalid API Key'),
        ('error', 'Connection Error'),
    ]
    DEFAULT_BASE_URLS = {
        'openrouter': 'https://openrouter.ai/api/v1',
        'nvidia': 'https://integrate.api.nvidia.com/v1',
        'ollama': 'http://localhost:11434',
    }
    ICONS = {
        'openrouter': 'fa-route',
        'nvidia': 'fa-microchip',
        'ollama': 'fa-server',
    }

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='llm_providers')
    provider_key = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    display_name = models.CharField(max_length=80, blank=True)
    base_url = models.CharField(
        max_length=200, blank=True,
        help_text='Leave blank to use the provider default.')
    api_key = models.CharField(
        max_length=255, blank=True,
        help_text='Stored as plain text in this academic build. See llm_client.py.')
    is_enabled = models.BooleanField(default=True)
    connection_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='untested')
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_message = models.CharField(max_length=255, blank=True)
    last_latency_ms = models.PositiveIntegerField(null=True, blank=True)
    models_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['provider_key']
        verbose_name = 'LLM Provider'
        verbose_name_plural = 'LLM Providers'
        constraints = [
            models.UniqueConstraint(fields=['provider_key'],
                                    name='uniq_provider_in_workspace'),
        ]
        permissions = [
            ('test_llmprovider', 'Can test a provider connection and refresh its models'),
        ]

    def __str__(self):
        return f"{self.get_provider_key_display()} ({self.user.username})"

    @property
    def label(self):
        return self.display_name or self.get_provider_key_display()

    @property
    def icon(self):
        return self.ICONS.get(self.provider_key, 'fa-plug')

    @property
    def effective_base_url(self):
        return (self.base_url or self.DEFAULT_BASE_URLS.get(self.provider_key, '')).rstrip('/')

    @property
    def requires_api_key(self):
        """Ollama runs locally and needs no credential."""
        return self.provider_key in ('openrouter', 'nvidia')

    @property
    def env_var_name(self):
        """The environment variable consulted when no key is stored."""
        return {'openrouter': 'OPENROUTER_API_KEY', 'nvidia': 'NVIDIA_API_KEY'}.get(
            self.provider_key, '')

    @property
    def env_key(self):
        """A key supplied through the environment, if there is one.

        Reading it here rather than only in llm_client keeps `is_configured`
        honest, which matters because AIAgent.has_live_llm depends on it: an
        employee whose provider is configured by environment variable must be
        treated as having a live model.
        """
        return os.environ.get(self.env_var_name, '') if self.env_var_name else ''

    @property
    def key_source(self):
        if self.api_key:
            return 'database'
        if self.env_key:
            return 'environment'
        return 'none'

    @property
    def masked_key(self):
        key = self.api_key or self.env_key
        if not key:
            return 'Not set'
        if len(key) <= 12:
            return '*' * len(key)
        return f"{key[:6]}{'*' * 10}{key[-4:]}"

    @property
    def is_configured(self):
        return bool(self.api_key or self.env_key) or not self.requires_api_key

    @property
    def model_count(self):
        return self.models.filter(is_enabled=True).count()


class LLMModel(models.Model):
    """A single model offered by a provider. Backs the cascading dropdown."""

    SOURCE_CHOICES = [
        ('live', 'Fetched Live'),
        ('fallback', 'Curated Fallback'),
        ('manual', 'Added Manually'),
    ]

    provider = models.ForeignKey(LLMProvider, on_delete=models.CASCADE, related_name='models')
    model_id = models.CharField(
        max_length=150,
        help_text="Exact API identifier, for example 'openai/gpt-4o-mini'.")
    display_name = models.CharField(max_length=150, blank=True)
    context_length = models.PositiveIntegerField(null=True, blank=True)
    description = models.CharField(max_length=300, blank=True)
    is_enabled = models.BooleanField(default=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='fallback')
    fetched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['provider__provider_key', 'display_name', 'model_id']
        verbose_name = 'LLM Model'
        verbose_name_plural = 'LLM Models'
        constraints = [
            models.UniqueConstraint(fields=['provider', 'model_id'],
                                    name='uniq_model_per_provider'),
        ]
        indexes = [
            models.Index(fields=['provider', 'is_enabled'], name='llmmodel_prov_enabled_idx'),
        ]

    def __str__(self):
        return self.display_name or self.model_id

    @property
    def label(self):
        return f"{self.display_name or self.model_id} - {self.provider.get_provider_key_display()}"

    @property
    def context_label(self):
        if not self.context_length:
            return ''
        if self.context_length >= 1000:
            return f"{self.context_length // 1000}K context"
        return f"{self.context_length} context"


# ===========================================================================
# 3. CAPABILITY -- Model Context Protocol servers and tools
# ===========================================================================

class MCPServer(models.Model):
    """A Model Context Protocol server registered against a workspace.

    This project models MCP servers in Django only: connection details are
    recorded and validated, but no external process is ever launched. See
    mcp_client.simulate_handshake for the precise scope of the simulation.
    """

    SERVER_CHOICES = [
        ('gmail', 'Gmail'),
        ('instagram', 'Instagram'),
        ('database', 'Database'),
        ('memory', 'Memory (Knowledge Graph)'),
        ('sequential_thinking', 'Sequential Thinking'),
        ('spotify', 'Spotify'),
        ('linkedin', 'LinkedIn'),
    ]
    TRANSPORT_CHOICES = [
        ('stdio', 'Standard I/O'),
        ('http', 'Streamable HTTP'),
        ('sse', 'Server-Sent Events'),
    ]
    CATEGORY_CHOICES = [
        ('communication', 'Communication'),
        ('social', 'Social Media'),
        ('data', 'Data & Storage'),
        ('reasoning', 'Reasoning'),
        ('media', 'Media'),
    ]
    STATUS_CHOICES = [
        ('unknown', 'Never Connected'),
        ('connected', 'Connected'),
        ('degraded', 'Degraded'),
        ('failed', 'Handshake Failed'),
        ('disabled', 'Disabled'),
    ]
    # Servers that act on a third-party account and therefore need a credential.
    CREDENTIAL_REQUIRED = ('gmail', 'instagram', 'spotify', 'linkedin')

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mcp_servers')
    server_key = models.CharField(max_length=30, choices=SERVER_CHOICES)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='data')
    icon = models.CharField(max_length=50, default='fa-plug')
    color = models.CharField(max_length=20, default='#4f46e5')
    transport = models.CharField(max_length=10, choices=TRANSPORT_CHOICES, default='stdio')
    command = models.CharField(
        max_length=200, blank=True,
        help_text='Recorded for documentation only. This project never executes it.')
    args = models.CharField(max_length=300, blank=True)
    endpoint_url = models.CharField(max_length=200, blank=True)
    auth_token = models.CharField(max_length=255, blank=True)
    config = models.JSONField(
        default=dict, blank=True,
        help_text='Extra settings, for example {"scopes": ["gmail.send"]}.')
    is_enabled = models.BooleanField(default=True)
    connection_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unknown')
    last_handshake_at = models.DateTimeField(null=True, blank=True)
    last_status_message = models.CharField(max_length=255, blank=True)
    protocol_version = models.CharField(max_length=20, default='2025-06-18')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'name']
        verbose_name = 'MCP Server'
        verbose_name_plural = 'MCP Servers'
        constraints = [
            models.UniqueConstraint(fields=['server_key'],
                                    name='uniq_mcp_server_in_workspace'),
        ]
        permissions = [
            ('test_mcpserver', 'Can run the connection test on an MCP server'),
        ]

    def __str__(self):
        return f"{self.name} [{self.get_transport_display()}]"

    @property
    def tool_count(self):
        return self.tools.count()

    @property
    def enabled_tool_count(self):
        return self.tools.filter(is_enabled=True).count()

    @property
    def requires_credential(self):
        return self.server_key in self.CREDENTIAL_REQUIRED

    @property
    def is_online(self):
        return self.is_enabled and self.connection_status == 'connected'

    @property
    def target_label(self):
        """What the connection actually points at, for display."""
        if self.transport == 'stdio':
            return f"{self.command} {self.args}".strip() or 'No command configured'
        return self.endpoint_url or 'No endpoint configured'


class MCPTool(models.Model):
    """One callable capability advertised by an MCP server."""

    server = models.ForeignKey(MCPServer, on_delete=models.CASCADE, related_name='tools')
    tool_name = models.CharField(
        max_length=80, help_text="MCP tool identifier, for example 'send_email'.")
    display_name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    input_schema = models.JSONField(default=dict, blank=True)
    is_enabled = models.BooleanField(default=True)
    is_destructive = models.BooleanField(
        default=False,
        help_text='Tools that write to the outside world always require human approval.')
    call_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['server__name', 'display_name']
        verbose_name = 'MCP Tool'
        verbose_name_plural = 'MCP Tools'
        constraints = [
            models.UniqueConstraint(fields=['server', 'tool_name'], name='uniq_tool_per_server'),
        ]

    def __str__(self):
        return f"{self.server.name}: {self.tool_name}"

    @property
    def qualified_name(self):
        return f"{self.server.server_key}.{self.tool_name}"


# ===========================================================================
# 4. WORKFORCE -- the AI employees and the work they produce
# ===========================================================================

class AIAgent(models.Model):
    """An AI employee.

    Each employee has a persona, a system prompt sent verbatim to the language
    model, an assigned LLM, and a set of MCP tools it is permitted to use.
    """

    AGENT_TYPE_CHOICES = [
        ('content', 'Social Content Creator'),
        ('lead_finder', 'Lead Finder Specialist'),
        ('outreach', 'Outreach & Email Manager'),
        ('analyst', 'Analytics & Strategy Officer'),
    ]

    STATUS_CHOICES = [
        ('active', 'Active & Monitoring'),
        ('busy', 'Executing Workflow'),
        ('idle', 'Idle / Ready'),
        ('offline', 'Offline'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='agents',
                             null=True, blank=True)
    name = models.CharField(max_length=100)
    role = models.CharField(max_length=100)
    agent_type = models.CharField(max_length=30, choices=AGENT_TYPE_CHOICES, default='content')
    avatar_icon = models.CharField(max_length=50, default='fa-robot')
    avatar_color = models.CharField(max_length=50, default='#4f46e5')
    persona_description = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    tasks_completed = models.PositiveIntegerField(default=0)
    success_rate = models.DecimalField(max_digits=5, decimal_places=2, default=98.50)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Language model configuration -------------------------------------
    system_prompt = models.TextField(
        blank=True, default='',
        help_text='Sent to the language model as the "system" role on every call.')
    llm_model = models.ForeignKey(LLMModel, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='agents')
    temperature = models.DecimalField(
        max_digits=3, decimal_places=2, default=0.70,
        validators=[MinValueValidator(0.0), MaxValueValidator(2.0)],
        help_text='0.00 is deterministic, 2.00 is maximally creative.')
    max_tokens = models.PositiveIntegerField(default=800)
    is_active = models.BooleanField(default=True)

    # --- Capability --------------------------------------------------------
    # `through` gives every attachment an audit trail; see AgentToolLink.
    mcp_tools = models.ManyToManyField(MCPTool, through='AgentToolLink',
                                       related_name='agents', blank=True)

    # null=True keeps makemigrations from prompting for a one-off default.
    updated_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        ordering = ['agent_type', 'name']
        verbose_name = 'AI Employee'
        verbose_name_plural = 'AI Employees'
        # One employee of each type in the shared workspace, not one per
        # user: the roster is organisation-wide.
        constraints = [
            models.UniqueConstraint(fields=['agent_type'],
                                    name='uniq_agent_type_in_workspace'),
        ]

    def __str__(self):
        return f"{self.name} ({self.role})"

    @property
    def tool_names(self):
        return [t.qualified_name for t in self.mcp_tools.all()]

    @property
    def has_live_llm(self):
        """True when this employee can actually reach a language model."""
        return bool(
            self.llm_model
            and self.llm_model.is_enabled
            and self.llm_model.provider.is_enabled
            and self.llm_model.provider.is_configured
        )

    @property
    def effective_system_prompt(self):
        """The system prompt, falling back to one derived from the persona."""
        return self.system_prompt.strip() or (
            f"You are {self.name}, a {self.role}. {self.persona_description}")

    @property
    def llm_label(self):
        return self.llm_model.model_id if self.llm_model else 'Template fallback'


class AgentToolLink(models.Model):
    """Through model joining an AI employee to an MCP tool.

    A plain ManyToManyField could not record when a tool was attached, by whom,
    whether it is enabled for this particular employee, or how often it ran.
    """

    agent = models.ForeignKey(AIAgent, on_delete=models.CASCADE, related_name='tool_links')
    tool = models.ForeignKey(MCPTool, on_delete=models.CASCADE, related_name='agent_links')
    is_enabled = models.BooleanField(default=True)
    attached_at = models.DateTimeField(auto_now_add=True)
    attached_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='tool_attachments')
    usage_count = models.PositiveIntegerField(default=0)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['tool__server__name', 'tool__display_name']
        verbose_name = 'Agent Tool Attachment'
        verbose_name_plural = 'Agent Tool Attachments'
        constraints = [
            models.UniqueConstraint(fields=['agent', 'tool'], name='uniq_tool_per_agent'),
        ]

    def __str__(self):
        return f"{self.agent.name} -> {self.tool.qualified_name}"


# The name a thread carries until it is auto-named from its opening question
# or renamed by hand. agent_engine imports this rather than repeating the
# literal, so the two can never drift apart.
DEFAULT_CONVERSATION_TITLE = 'New conversation'


class Conversation(models.Model):
    """One chat thread between a person and an AI employee.

    Modelled the way a chat product does it: a user may hold several separate
    conversations with the same employee, each with its own title and history.
    Ordering is by `updated_at` so the most recently used thread sits at the
    top of the conversation rail.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='conversations')
    agent = models.ForeignKey('AIAgent', on_delete=models.CASCADE, related_name='conversations')
    title = models.CharField(max_length=120, default=DEFAULT_CONVERSATION_TITLE)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Conversation'
        verbose_name_plural = 'Conversations'
        indexes = [
            models.Index(fields=['user', '-updated_at'], name='conversation_user_recent_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.agent.name})"

    @property
    def message_count(self):
        return self.messages.count()

    @property
    def preview(self):
        """The opening question, used as a subtitle in the conversation rail."""
        first = self.messages.filter(role='user').order_by('created_at').first()
        return first.content[:80] if first else 'No messages yet'

    def title_from_first_message(self):
        """Derive a readable title from the opening question.

        A chat product names a thread after what it is about rather than
        leaving every entry called "New conversation".
        """
        first = self.messages.filter(role='user').order_by('created_at').first()
        if not first:
            return self.title
        text = ' '.join(first.content.split())
        return (text[:60] + '...') if len(text) > 60 else text


class ChatMessage(models.Model):
    """A single turn in a conversation.

    `role` follows the convention used by the chat completion APIs this
    project talks to, so a conversation can be replayed straight into a
    request body with no translation.
    """

    ROLE_CHOICES = [
        ('user', 'Person'),
        ('assistant', 'AI Employee'),
        ('system', 'System'),
    ]
    SOURCE_CHOICES = [
        ('live', 'Live LLM Call'),
        ('fallback', 'Template Fallback'),
        ('manual', 'Typed by a Person'),
    ]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     related_name='messages')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    generation_source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='manual')
    llm_model_used = models.CharField(max_length=150, blank=True)
    tokens_used = models.PositiveIntegerField(default=0)
    tools_consulted = models.JSONField(
        default=list, blank=True,
        help_text='Qualified names of the MCP tools consulted for this reply.')
    is_error = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Chat Message'
        verbose_name_plural = 'Chat Messages'

    def __str__(self):
        return f"[{self.get_role_display()}] {self.content[:60]}"

    @property
    def is_assistant(self):
        return self.role == 'assistant'

    @property
    def submitted_approval(self):
        """The approval raised from this reply, if a reviewer sent one."""
        return self.approvals.first()


class MCPCallLog(models.Model):
    """Record of a simulated MCP tool invocation made during an agent run."""

    OUTCOME_CHOICES = [('ok', 'Success'), ('error', 'Error'), ('skipped', 'Skipped')]

    agent = models.ForeignKey(AIAgent, on_delete=models.CASCADE, related_name='mcp_calls')
    tool = models.ForeignKey(MCPTool, on_delete=models.CASCADE, related_name='calls')
    arguments = models.JSONField(default=dict, blank=True)
    result_summary = models.CharField(max_length=300, blank=True)
    outcome = models.CharField(max_length=10, choices=OUTCOME_CHOICES, default='ok')
    duration_ms = models.PositiveIntegerField(default=0)
    called_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-called_at']
        verbose_name = 'MCP Call Log'
        verbose_name_plural = 'MCP Call Log'

    def __str__(self):
        return f"{self.tool.qualified_name} at {self.called_at:%Y-%m-%d %H:%M}"


class MarketingCampaign(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('completed', 'Completed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='campaigns',
                             null=True, blank=True)
    name = models.CharField(max_length=200)
    objective = models.TextField()
    target_audience = models.CharField(max_length=200)
    budget = models.DecimalField(max_digits=10, decimal_places=2, default=1000.00)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    assigned_agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='campaigns')
    start_date = models.DateField(default=timezone.now)
    end_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Marketing Campaign'
        verbose_name_plural = 'Marketing Campaigns'

    def __str__(self):
        return self.name


class Lead(models.Model):
    SCORE_TIER_CHOICES = [
        ('hot', 'Hot Lead (80-100)'),
        ('warm', 'Warm Prospect (50-79)'),
        ('cold', 'Cold Contact (0-49)'),
    ]

    STATUS_CHOICES = [
        ('new', 'New Lead'),
        ('contacted', 'Contacted'),
        ('replied', 'Replied'),
        ('qualified', 'Qualified'),
        ('converted', 'Converted'),
        ('unresponsive', 'Unresponsive'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='leads',
                             null=True, blank=True)
    campaign = models.ForeignKey(MarketingCampaign, on_delete=models.CASCADE, related_name='leads',
                                 null=True, blank=True)
    full_name = models.CharField(max_length=150)
    email = models.EmailField()
    company = models.CharField(max_length=150)
    job_title = models.CharField(max_length=150)
    industry = models.CharField(max_length=100)
    lead_score = models.IntegerField(default=75)
    score_tier = models.CharField(max_length=20, choices=SCORE_TIER_CHOICES, default='warm')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    notes = models.TextField(blank=True)
    discovered_by = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='discovered_leads')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Lead'
        verbose_name_plural = 'Leads'

    def __str__(self):
        return f"{self.full_name} ({self.company})"


class SocialPost(models.Model):
    PLATFORM_CHOICES = [
        ('linkedin', 'LinkedIn'),
        ('twitter', 'Twitter / X'),
        ('instagram', 'Instagram'),
        ('facebook', 'Facebook'),
    ]

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('published', 'Published'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='posts',
                             null=True, blank=True)
    campaign = models.ForeignKey(MarketingCampaign, on_delete=models.CASCADE,
                                 related_name='social_posts', null=True, blank=True)
    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='posts')
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, default='linkedin')
    title = models.CharField(max_length=200)
    content = models.TextField()
    hashtags = models.CharField(max_length=300, blank=True)
    image_url = models.URLField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    scheduled_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    likes = models.PositiveIntegerField(default=0)
    shares = models.PositiveIntegerField(default=0)
    comments = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Social Post'
        verbose_name_plural = 'Social Posts'

    def __str__(self):
        return f"[{self.platform.capitalize()}] {self.title}"


class EmailOutreach(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('opened', 'Opened'),
        ('replied', 'Replied'),
        ('bounced', 'Bounced'),
    ]

    # related_name='emails' on User does not clash with Lead.emails: related
    # names only need to be unique per target model, and these differ.
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='emails',
                             null=True, blank=True)
    campaign = models.ForeignKey(MarketingCampaign, on_delete=models.CASCADE,
                                 related_name='email_outreaches', null=True, blank=True)
    # Nullable on purpose. Most outreach goes to a prospect on the pipeline,
    # but a person may also write to an address that is not one -- a partner, a
    # colleague, a test. Requiring a Lead row would either block that or
    # pollute the pipeline with people who are not prospects.
    lead = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='emails')
    recipient_email = models.EmailField(
        blank=True,
        help_text='Used when the message is not addressed to a prospect on the pipeline.')
    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='outreach_emails')
    subject = models.CharField(max_length=250)
    body = models.TextField()
    sequence_step = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')

    # auto_now_add, so this is when the DRAFT was written, not when it went out.
    sent_at = models.DateTimeField(auto_now_add=True)

    # Set only when the message actually left the server, which happens on
    # approval and never before.
    delivered_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the message was handed to the mail server.')
    delivery_error = models.TextField(
        blank=True,
        help_text='Why delivery failed, if it did. Empty on success.')

    class Meta:
        ordering = ['-sent_at']
        verbose_name = 'Outreach Email'
        verbose_name_plural = 'Outreach Emails'

    def __str__(self):
        return f"To {self.recipient or 'nobody'}: {self.subject}"

    @property
    def recipient(self):
        """Where this message is actually going.

        An explicit address wins; otherwise it is the prospect's. One of the
        two is always set, and mailer.send_outreach refuses to send when
        neither is.
        """
        return self.recipient_email or (self.lead.email if self.lead_id else '')

    @property
    def recipient_name(self):
        return self.lead.full_name if self.lead_id else self.recipient

    @property
    def was_delivered(self):
        return self.delivered_at is not None and not self.delivery_error

    @property
    def delivery_summary(self):
        if self.delivery_error:
            return f'Delivery failed: {self.delivery_error}'
        if self.delivered_at:
            # localtime, so this agrees with every other time in the interface
            # rather than quietly reporting UTC.
            local = timezone.localtime(self.delivered_at)
            return f'Delivered {local:%d %b %Y at %H:%M}'
        return 'Not sent yet'


class AnalyticsMetric(models.Model):
    CATEGORY_CHOICES = [
        ('lead_gen', 'Lead Generation'),
        ('social_reach', 'Social Reach'),
        ('email_conversion', 'Email Conversions'),
        ('roi', 'ROI & Revenue'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='metrics',
                             null=True, blank=True)
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default='lead_gen')
    title = models.CharField(max_length=150)
    value = models.CharField(max_length=50)
    change_percent = models.DecimalField(max_digits=5, decimal_places=2, default=12.50)
    is_positive = models.BooleanField(default=True)
    metric_date = models.DateField(default=timezone.now)

    class Meta:
        ordering = ['-metric_date']
        verbose_name = 'Analytics Metric'
        verbose_name_plural = 'Analytics Metrics'

    def __str__(self):
        return f"{self.title}: {self.value}"


# ===========================================================================
# 5. GOVERNANCE -- the human-in-the-loop approval queue
# ===========================================================================

class ApprovalRequest(models.Model):
    """One piece of AI-generated work awaiting a human decision.

    Three nullable foreign keys point at the possible targets rather than a
    GenericForeignKey. Concrete keys give real referential integrity, allow
    select_related to fetch the queue in one query, and let list_filter reach
    through to fields such as social_post__platform. A database check
    constraint guarantees at most one of them is populated.
    """

    ITEM_TYPE_CHOICES = [
        ('social_post', 'Social Media Post'),
        ('email', 'Outreach Email'),
        ('lead', 'Discovered Lead'),
        ('insight', 'Strategy Insight'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]
    PRIORITY_CHOICES = [('low', 'Low'), ('normal', 'Normal'), ('high', 'High')]
    SOURCE_CHOICES = [
        ('live', 'Live LLM Call'),
        ('fallback', 'Template Fallback'),
        ('manual', 'Created Manually'),
    ]
    ITEM_ICONS = {
        'social_post': 'fa-share-nodes',
        'email': 'fa-envelope',
        'lead': 'fa-user-plus',
        'insight': 'fa-lightbulb',
    }

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='approval_requests')
    agent = models.ForeignKey(AIAgent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='approval_requests')
    item_type = models.CharField(max_length=20, choices=ITEM_TYPE_CHOICES)
    title = models.CharField(max_length=200)
    payload_preview = models.TextField(
        blank=True, help_text='Human-readable snapshot of what the agent produced.')

    # Exactly zero or one of these three is populated.
    social_post = models.ForeignKey(SocialPost, on_delete=models.CASCADE, null=True, blank=True,
                                    related_name='approvals')
    email_outreach = models.ForeignKey(EmailOutreach, on_delete=models.CASCADE, null=True,
                                       blank=True, related_name='approvals')
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, null=True, blank=True,
                             related_name='approvals')

    # Provenance, not a target. This records which chat reply a reviewer chose
    # to submit, so the queue can link back to the conversation it came from.
    # It is deliberately outside the check constraint below, which governs only
    # the three target links above.
    source_message = models.ForeignKey('ChatMessage', on_delete=models.SET_NULL, null=True,
                                       blank=True, related_name='approvals')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='normal')
    generation_source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='fallback')
    llm_model_used = models.CharField(max_length=150, blank=True)
    tokens_used = models.PositiveIntegerField(default=0)

    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='approval_decisions')
    decision_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['-requested_at']
        verbose_name = 'Approval Request'
        verbose_name_plural = 'Approval Queue'
        indexes = [
            models.Index(fields=['user', 'status'], name='approval_user_status_idx'),
        ]
        permissions = [
            ('approve_approvalrequest', 'Can approve or reject queued work'),
        ]
        constraints = [
            # NOTE: Django 6 uses `condition=`. The old `check=` keyword was
            # removed in 6.0 and raises TypeError at import time.
            models.CheckConstraint(
                condition=(
                    Q(social_post__isnull=True, email_outreach__isnull=True, lead__isnull=True)
                    | Q(social_post__isnull=False, email_outreach__isnull=True, lead__isnull=True)
                    | Q(social_post__isnull=True, email_outreach__isnull=False, lead__isnull=True)
                    | Q(social_post__isnull=True, email_outreach__isnull=True, lead__isnull=False)
                ),
                name='approval_at_most_one_target',
            ),
        ]

    def __str__(self):
        return f"[{self.get_status_display()}] {self.title}"

    @property
    def target(self):
        """The concrete object under review, or None for a text-only insight."""
        return self.social_post or self.email_outreach or self.lead

    @property
    def is_pending(self):
        return self.status == 'pending'

    @property
    def icon(self):
        return self.ITEM_ICONS.get(self.item_type, 'fa-inbox')

    @property
    def age_hours(self):
        return round((timezone.now() - self.requested_at).total_seconds() / 3600, 1)

    @property
    def decision_summary(self):
        if self.status == 'pending':
            return 'Awaiting review'
        who = self.decided_by.username if self.decided_by else 'system'
        when = self.decided_at.strftime('%d %b %Y at %H:%M') if self.decided_at else 'unknown time'
        return f"{self.get_status_display()} by {who} on {when}"

    def decide(self, actor, decision, reason=''):
        """Stamp the decision. Side effects live in agent_engine.apply_approval."""
        self.status = decision
        self.decided_by = actor
        self.decided_at = timezone.now()
        self.decision_reason = reason or ''
        self.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_reason'])
        return self


class ApprovalAuditLog(models.Model):
    """Full history of everything that happened to an approval request."""

    ACTION_CHOICES = [
        ('created', 'Submitted for Review'),
        ('edited', 'Edited Before Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]

    approval = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE,
                                 related_name='audit_entries')
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='approval_actions')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    note = models.TextField(blank=True)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Approval Audit Entry'
        verbose_name_plural = 'Approval Audit Trail'

    def __str__(self):
        who = self.actor.username if self.actor else 'system'
        return f"{who} {self.get_action_display()} approval #{self.approval_id}"
