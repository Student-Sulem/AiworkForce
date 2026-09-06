"""Forms for the AI Workforce application.

The workspace is shared, so a dropdown here offers every row of its type rather
than only the current user's. Each `__init__` still accepts and discards a
`user` keyword argument: it kept the call sites unchanged when the application
moved from per-user isolation to a shared workspace, and it leaves an obvious
place to reintroduce scoping should the project ever host more than one
organisation.

Who may submit a given form is decided by role, not by the form itself. See
marketing/roles.py and the permission checks in marketing/views.py.

Widget classes (`form-input`, `form-select`, `form-textarea`) are set here in
Python rather than in the template so that `{{ field }}` renders correctly
styled markup wherever it appears, including inside partials/_form_field.html.
"""

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import (AIAgent, LLMModel, LLMProvider, MarketingCampaign,
                     MCPServer, MCPTool, Profile, SocialPost)

INPUT = {'class': 'form-input'}
SELECT = {'class': 'form-select'}


class UserRegisterForm(UserCreationForm):
    """Account sign-up, with an email address added to Django's default."""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'class': 'form-input',
                                       'placeholder': 'you@company.com'}))

    class Meta:
        model = User
        fields = ['username', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-input')


class CampaignForm(forms.ModelForm):
    """Create or edit a marketing campaign."""

    class Meta:
        model = MarketingCampaign
        fields = ['name', 'objective', 'target_audience', 'budget', 'status', 'assigned_agent']
        widgets = {
            'name': forms.TextInput(attrs={**INPUT, 'placeholder': 'Q3 enterprise expansion'}),
            'objective': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3}),
            'target_audience': forms.TextInput(
                attrs={**INPUT, 'placeholder': 'SaaS marketing directors, 50-500 staff'}),
            'budget': forms.NumberInput(attrs={**INPUT, 'step': '0.01', 'min': '0'}),
            'status': forms.Select(attrs=SELECT),
            'assigned_agent': forms.Select(attrs=SELECT),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields['assigned_agent'].queryset = AIAgent.objects.all()
        self.fields['assigned_agent'].required = False
        self.fields['assigned_agent'].empty_label = 'No employee assigned'


class AIAgentForm(forms.ModelForm):
    """Edit an AI employee: identity, persona, system prompt, LLM and MCP tools.

    `mcp_tools` is declared as a form field but deliberately left out of
    Meta.fields. That keeps ModelForm.save_m2m() away from the AgentToolLink
    through table; the view writes the links explicitly through
    agent_engine.sync_agent_tools(), which is one readable function rather
    than framework machinery.
    """

    mcp_tools = forms.ModelMultipleChoiceField(
        queryset=MCPTool.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'chip-input'}),
        label='Attached MCP tools',
        help_text='Only tools on enabled servers can be attached.',
    )

    class Meta:
        model = AIAgent
        fields = ['name', 'role', 'agent_type', 'avatar_icon', 'avatar_color',
                  'persona_description', 'system_prompt', 'llm_model',
                  'temperature', 'max_tokens', 'status', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={**INPUT, 'placeholder': 'Sophia'}),
            'role': forms.TextInput(attrs={**INPUT, 'placeholder': 'Social Content Creator'}),
            'agent_type': forms.Select(attrs=SELECT),
            'avatar_icon': forms.TextInput(attrs={**INPUT, 'placeholder': 'fa-pen-nib'}),
            'avatar_color': forms.TextInput(attrs={'class': 'form-color', 'type': 'color'}),
            'persona_description': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3}),
            'system_prompt': forms.Textarea(attrs={
                'class': 'form-textarea form-textarea--prompt', 'rows': 10,
                'placeholder': 'You are Sophia, a senior B2B social content strategist...'}),
            'llm_model': forms.Select(attrs={**SELECT, 'data-role': 'model-select'}),
            'temperature': forms.NumberInput(
                attrs={**INPUT, 'step': '0.05', 'min': '0', 'max': '2'}),
            'max_tokens': forms.NumberInput(attrs={**INPUT, 'min': '64', 'max': '8192'}),
            'status': forms.Select(attrs=SELECT),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check'}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        # Only models and tools that are actually usable: a disabled
        # provider or server withdraws its rows from the choices.
        self.fields['llm_model'].queryset = (
            LLMModel.objects
            .filter(is_enabled=True, provider__is_enabled=True)
            .select_related('provider'))
        self.fields['mcp_tools'].queryset = (
            MCPTool.objects
            .filter(is_enabled=True, server__is_enabled=True)
            .select_related('server'))

        self.fields['llm_model'].required = False
        self.fields['llm_model'].empty_label = 'No model assigned (use template fallback)'

        if self.instance.pk:
            self.fields['mcp_tools'].initial = self.instance.mcp_tools.values_list('pk', flat=True)

    def clean_max_tokens(self):
        value = self.cleaned_data['max_tokens']
        if value < 64:
            raise forms.ValidationError('Use at least 64 tokens.')
        if value > 8192:
            raise forms.ValidationError('Use at most 8192 tokens.')
        return value


class LLMProviderForm(forms.ModelForm):
    """Configure one language-model provider's credentials and endpoint."""

    class Meta:
        model = LLMProvider
        fields = ['display_name', 'base_url', 'api_key', 'is_enabled']
        widgets = {
            'display_name': forms.TextInput(attrs=INPUT),
            'base_url': forms.TextInput(
                attrs={**INPUT, 'placeholder': 'Leave blank to use the provider default'}),
            'api_key': forms.PasswordInput(
                attrs={**INPUT, 'autocomplete': 'off',
                       'placeholder': 'Paste a key, or leave blank to keep the stored one'},
                render_value=False),
            'is_enabled': forms.CheckboxInput(attrs={'class': 'form-check'}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop('user', None)          # accepted for signature consistency
        super().__init__(*args, **kwargs)
        self.fields['api_key'].required = False
        self.fields['base_url'].required = False
        if self.instance.pk and not self.instance.requires_api_key:
            self.fields['api_key'].disabled = True
            self.fields['api_key'].widget.attrs['placeholder'] = (
                'Not required: Ollama runs locally without a credential')

    def clean_api_key(self):
        """Treat a blank submission as 'keep the stored key'.

        PasswordInput(render_value=False) always renders empty, so without this
        every save of the Configurations page would silently erase the key and
        the next connection test would fail.
        """
        submitted = (self.cleaned_data.get('api_key') or '').strip()
        return submitted or self.instance.api_key


class MCPServerForm(forms.ModelForm):
    """Edit an MCP server's connection details."""

    class Meta:
        model = MCPServer
        fields = ['name', 'description', 'category', 'transport', 'command', 'args',
                  'endpoint_url', 'auth_token', 'icon', 'color', 'is_enabled']
        widgets = {
            'name': forms.TextInput(attrs=INPUT),
            'description': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3}),
            'category': forms.Select(attrs=SELECT),
            'transport': forms.Select(attrs={**SELECT, 'data-role': 'transport-select'}),
            'command': forms.TextInput(attrs={**INPUT, 'placeholder': 'npx'}),
            'args': forms.TextInput(
                attrs={**INPUT, 'placeholder': '-y @modelcontextprotocol/server-gmail'}),
            'endpoint_url': forms.TextInput(
                attrs={**INPUT, 'placeholder': 'https://api.example.com/mcp'}),
            'auth_token': forms.PasswordInput(
                attrs={**INPUT, 'autocomplete': 'off',
                       'placeholder': 'Leave blank to keep the stored credential'},
                render_value=False),
            'icon': forms.TextInput(attrs={**INPUT, 'placeholder': 'fa-plug'}),
            'color': forms.TextInput(attrs={'class': 'form-color', 'type': 'color'}),
            'is_enabled': forms.CheckboxInput(attrs={'class': 'form-check'}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields['auth_token'].required = False

    def clean_auth_token(self):
        """Same reasoning as LLMProviderForm.clean_api_key."""
        return (self.cleaned_data.get('auth_token') or '').strip() or self.instance.auth_token

    def clean(self):
        """A transport is useless without somewhere to connect to."""
        cleaned = super().clean()
        transport = cleaned.get('transport')
        if transport == 'stdio' and not (cleaned.get('command') or '').strip():
            self.add_error('command', 'A standard I/O transport needs a launch command.')
        if transport in ('http', 'sse') and not (cleaned.get('endpoint_url') or '').strip():
            self.add_error('endpoint_url', f'A {transport.upper()} transport needs an endpoint URL.')
        return cleaned


class MCPToolForm(forms.ModelForm):
    """Add or edit a capability advertised by an MCP server."""

    class Meta:
        model = MCPTool
        fields = ['server', 'tool_name', 'display_name', 'description',
                  'is_enabled', 'is_destructive']
        widgets = {
            'server': forms.Select(attrs=SELECT),
            'tool_name': forms.TextInput(attrs={**INPUT, 'placeholder': 'send_email'}),
            'display_name': forms.TextInput(attrs={**INPUT, 'placeholder': 'Send Email'}),
            'description': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 2}),
            'is_enabled': forms.CheckboxInput(attrs={'class': 'form-check'}),
            'is_destructive': forms.CheckboxInput(attrs={'class': 'form-check'}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields['server'].queryset = MCPServer.objects.all()


class ApprovalDecisionForm(forms.Form):
    """Approve or reject one queued item.

    A plain Form rather than a ModelForm, because the decision is applied
    through agent_engine.apply_approval() so that the audit entry and the
    side effects always happen together.
    """

    DECISION_CHOICES = [('approved', 'Approve'), ('rejected', 'Reject')]

    approval_id = forms.IntegerField(widget=forms.HiddenInput())
    decision = forms.ChoiceField(
        choices=DECISION_CHOICES,
        widget=forms.RadioSelect(attrs={'class': 'form-radio'}))
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-textarea', 'rows': 3,
            'placeholder': 'Explain why this is being rejected'}))

    def clean(self):
        """Cross-field rule: a rejection must carry a reason."""
        cleaned = super().clean()
        if cleaned.get('decision') == 'rejected' and not (cleaned.get('reason') or '').strip():
            self.add_error('reason', 'A reason is required when rejecting an item.')
        return cleaned


class SocialPostForm(forms.ModelForm):
    """Edit a drafted post. Used on the approval detail page so a reviewer can
    correct the wording before approving it."""

    class Meta:
        model = SocialPost
        fields = ['platform', 'title', 'content', 'hashtags', 'campaign']
        widgets = {
            'platform': forms.Select(attrs=SELECT),
            'title': forms.TextInput(attrs=INPUT),
            'content': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 10}),
            'hashtags': forms.TextInput(attrs={**INPUT, 'placeholder': '#AIMarketing #B2B'}),
            'campaign': forms.Select(attrs=SELECT),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields['campaign'].queryset = MarketingCampaign.objects.all()
        self.fields['campaign'].required = False
        self.fields['campaign'].empty_label = 'No campaign'


class ProfileForm(forms.ModelForm):
    """Edit a user's profile from the Users page."""

    class Meta:
        model = Profile
        fields = ['role', 'job_title', 'avatar_icon', 'avatar_color', 'theme', 'bio']
        widgets = {
            'role': forms.Select(attrs=SELECT),
            'job_title': forms.TextInput(attrs={**INPUT, 'placeholder': 'Head of Growth'}),
            'avatar_icon': forms.TextInput(attrs=INPUT),
            'avatar_color': forms.TextInput(attrs={'class': 'form-color', 'type': 'color'}),
            'theme': forms.Select(attrs=SELECT),
            'bio': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop('user', None)
        super().__init__(*args, **kwargs)


class UserCreateForm(UserCreationForm):
    """Add a person and give them a role in one step.

    `role` is not a field on User, so it is declared here and written to the
    Profile in save(). The Profile post_save signal then moves the account into
    the matching Django group, which is what actually grants the permissions.
    """

    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={**INPUT, 'placeholder': 'them@company.com'}))
    first_name = forms.CharField(
        required=False, widget=forms.TextInput(attrs=INPUT))
    last_name = forms.CharField(
        required=False, widget=forms.TextInput(attrs=INPUT))
    role = forms.ChoiceField(
        choices=Profile.ROLE_CHOICES,
        initial='analyst',
        widget=forms.Select(attrs=SELECT),
        help_text='Decides what this person may do once they sign in.')
    job_title = forms.CharField(
        required=False, widget=forms.TextInput(attrs={**INPUT, 'placeholder': 'Head of Growth'}))

    class Meta:
        model = User
        fields = ['username', 'email', 'first_name', 'last_name']

    def __init__(self, *args, **kwargs):
        kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        for name in ('username', 'password1', 'password2'):
            self.fields[name].widget.attrs.setdefault('class', 'form-input')

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            # The signal has already created the Profile by this point.
            profile = user.profile
            profile.role = self.cleaned_data['role']
            profile.job_title = self.cleaned_data.get('job_title', '')
            profile.save()
        return user


class UserEditForm(forms.ModelForm):
    """Edit someone's details, their role, and whether they are active.

    Takes an `editor` so it can refuse to let an administrator strip their own
    access -- a mistake that would otherwise lock them out mid-session.
    """

    role = forms.ChoiceField(
        choices=Profile.ROLE_CHOICES,
        widget=forms.Select(attrs=SELECT),
        help_text='Changing this moves the account into the matching group.')
    job_title = forms.CharField(required=False, widget=forms.TextInput(attrs=INPUT))

    class Meta:
        model = User
        fields = ['username', 'email', 'first_name', 'last_name', 'is_active', 'is_staff']
        widgets = {
            'username': forms.TextInput(attrs=INPUT),
            'email': forms.EmailInput(attrs=INPUT),
            'first_name': forms.TextInput(attrs=INPUT),
            'last_name': forms.TextInput(attrs=INPUT),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check'}),
            'is_staff': forms.CheckboxInput(attrs={'class': 'form-check'}),
        }
        labels = {'is_staff': 'Can open the Django admin'}

    def __init__(self, *args, **kwargs):
        self.editor = kwargs.pop('editor', None)
        kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            profile = getattr(self.instance, 'profile', None)
            if profile is not None:
                self.fields['role'].initial = profile.role
                self.fields['job_title'].initial = profile.job_title

    def clean(self):
        cleaned = super().clean()
        editing_self = (self.editor is not None
                        and self.instance.pk == self.editor.pk)

        if editing_self:
            if not cleaned.get('is_active', True):
                self.add_error('is_active', 'You cannot deactivate your own account.')
            if cleaned.get('role') != Profile.ROLE_CHOICES[0][0]:
                # ROLE_CHOICES[0] is 'owner', the administrator role.
                self.add_error(
                    'role', 'You cannot remove your own administrator role.')
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            profile = user.profile
            profile.role = self.cleaned_data['role']
            profile.job_title = self.cleaned_data.get('job_title', '')
            profile.save()
        return user
