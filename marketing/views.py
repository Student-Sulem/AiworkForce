"""Views for the AI Workforce application.

Two kinds of view live here.

PAGE VIEWS render an HTML template. There are six workspace pages -- Dashboard,
AI Employees, Approvals, Users, Configurations and MCP Tools -- plus two detail
pages and the public landing and authentication screens.

The AI Employees page is a chat interface: an employee is talked to rather than
triggered. Anything it writes reaches the outside world only when a person
submits it to the approval queue.

JSON ENDPOINTS are called by fetch() from static/js/. Every one of them is
decorated with @login_required and @require_POST, and every one that changes
something also checks a permission. They rely on Django's normal CSRF
protection: the browser sends the token in an X-CSRFToken header, read from the
hidden {% csrf_token %} that base.html renders on every page.

ACCESS CONTROL
--------------
The workspace is shared, so these views do not filter by owner. What a person
may DO is decided by their role, through Django's Group and Permission system:

    pages    @login_required, plus @permission_required where a whole page is
             privileged
    actions  request.user.has_perm(...) before anything is written
    JSON     the same check, returning 403 rather than ignoring it silently

Permission names come from the constants in marketing/roles.py, so a capability
can be re-scoped in one place. Conversations are the one thing that stays
private to whoever started them.
"""

import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import agent_engine, llm_client, mailer, markdown, mcp_client, roles
from .forms import (AIAgentForm, ApprovalDecisionForm, CampaignForm, LLMProviderForm,
                    MCPServerForm, MCPToolForm, ProfileForm, SocialPostForm,
                    UserCreateForm, UserEditForm, UserRegisterForm)
from .models import (AIAgent, ApprovalRequest, ChatMessage, Conversation, Lead,
                     LLMModel, LLMProvider, MarketingCampaign, MCPCallLog,
                     MCPServer, MCPTool, Profile, SocialPost)


# ===========================================================================
# Helpers
# ===========================================================================

def _json_body(request):
    """Parse a JSON request body, tolerating an empty or malformed one."""
    try:
        return json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _error(message, status=400):
    return JsonResponse({'status': 'error', 'message': message}, status=status)


def _forbidden(perm):
    """Refusal for a JSON endpoint the caller lacks the permission for."""
    return _error(
        f'Your role does not allow this action ({perm}).', status=403)


def _requires(request, perm):
    """True when the caller may proceed. Superusers always may."""
    return request.user.has_perm(perm)


def _pending_count():
    """Pending items across the shared workspace, not per person."""
    return ApprovalRequest.objects.filter(status='pending').count()


def _serialise_message(message):
    """One chat message, rendered by the same template Django uses on page load.

    `html` is the whole bubble, produced by partials/_chat_message.html. The
    browser inserts it as-is rather than rebuilding it, which is why a reply
    looks identical whether Django or JavaScript put it on the page -- and why
    mail cards, source badges and the send button only had to be written once.

    `content` is kept alongside it for the copy-to-clipboard action, which
    wants the original text rather than the markup.
    """
    return {
        'id': message.pk,
        'role': message.role,
        'content': message.content,
        'html': render_to_string('partials/_chat_message.html',
                                 {'message': message,
                                  'agent': message.conversation.agent,
                                  'perms': _perms_for(message.conversation.user)}),
        'source': message.generation_source,
        'source_label': message.get_generation_source_display(),
        'model': message.llm_model_used,
        'tokens': message.tokens_used,
        'tools': message.tools_consulted or [],
        'created_at': timezone.localtime(message.created_at).strftime('%H:%M'),
    }


def _perms_for(user):
    """The `perms` object a template gets from the auth context processor.

    render_to_string() outside a RequestContext has no context processors, so
    {% if perms.marketing.x %} would silently be false and the send button
    would never appear on a message added by JavaScript.
    """
    from django.contrib.auth.context_processors import PermWrapper
    return PermWrapper(user)


# ===========================================================================
# Public and authentication views
# ===========================================================================

def landing_page(request):
    """Public marketing page. Signed-in users go straight to their workspace."""
    if request.user.is_authenticated:
        return redirect('dashboard')
    return render(request, 'static_landing.html')


def login_view(request):
    """Sign in, then make sure the workspace is fully provisioned."""
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = authenticate(
                request,
                username=form.cleaned_data.get('username'),
                password=form.cleaned_data.get('password'),
            )
            if user is not None:
                login(request, user)
                agent_engine.ensure_workspace_for_user(user)
                user.profile.touch()
                messages.success(request, f'Welcome back, {user.username}.')
                return redirect(request.GET.get('next') or 'dashboard')
        messages.error(request, 'Those credentials were not recognised.')
    else:
        form = AuthenticationForm()

    return render(request, 'login.html', {'form': form})


def register_view(request):
    """Create an account, sign in, and provision the workspace."""
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            agent_engine.ensure_workspace_for_user(user)
            messages.success(
                request,
                'Your workspace is ready with four AI employees and seven MCP servers.')
            return redirect('dashboard')
        messages.error(request, 'Please correct the errors below.')
    else:
        form = UserRegisterForm()

    return render(request, 'register.html', {'form': form})


def logout_view(request):
    logout(request)
    messages.info(request, 'You have been signed out.')
    return redirect('landing')


# ===========================================================================
# Page 1: Dashboard
# ===========================================================================

@login_required(login_url='login')
def dashboard_view(request):
    """The company overview: the workforce, what is waiting, and what to do next.

    Four bands, in the order somebody actually reads them:

      1. What needs a person       the approval queue and anything urgent
      2. The workforce             six employees and what each has been doing
      3. The company              operational figures across the five areas
      4. What is not set up yet    the readiness checklist

    The fourth band is the one that matters most on a first run, and it is the
    reason this page is not just a wall of counters. A new user opening the
    platform has no language model, no connected applications and no idea
    which of the eleven pages to look at. `_readiness` answers that: it names
    what is missing, says what still works without it, and links to the page
    that fixes it. Without that, the honest first impression of a system this
    large is bewilderment.
    """
    user = request.user

    if request.method == 'POST':
        campaign_form = CampaignForm(request.POST, user=user)
        if campaign_form.is_valid():
            campaign = campaign_form.save(commit=False)
            campaign.user = user
            campaign.save()
            messages.success(request, f'Campaign "{campaign.name}" was created.')
            return redirect('dashboard')
        messages.error(request, 'The campaign could not be saved. Check the form below.')
    else:
        campaign_form = CampaignForm(user=user)

    from django.db.models import Avg, Count

    from . import audit as audit_log
    from .models_content import ContentPiece
    from .models_hr import Candidate, Interview, JobOpening
    from .models_knowledge import DocumentChunk, KnowledgeDocument
    from .models_platform import (AgentTask, AuditEvent, Integration,
                                  ProposedAction)
    from .models_support import SupportTicket

    today = timezone.now().date()
    week_ahead = timezone.now() + timezone.timedelta(days=7)
    fortnight_ago = timezone.now() - timezone.timedelta(days=14)

    actions = ProposedAction.objects.select_related('agent', 'integration')
    pending_actions = actions.filter(status='pending')
    oldest_pending = pending_actions.order_by('created_at').first()

    # The roster, annotated in one query each rather than a property call per
    # employee per counter, which on six employees and four counters would be
    # twenty-four queries for one page.
    agents = list(
        AIAgent.objects.select_related('llm_model', 'llm_model__provider')
        .annotate(
            capability_count=Count('capabilities', distinct=True),
            open_task_count=Count(
                'tasks',
                filter=Q(tasks__status__in=('queued', 'running', 'waiting_approval')),
                distinct=True),
            pending_action_count=Count(
                'proposed_actions',
                filter=Q(proposed_actions__status='pending'), distinct=True),
        )
    )

    integrations = list(Integration.objects.all())
    tickets = SupportTicket.objects.exclude(status__in=('resolved', 'closed'))

    context = {
        # --- The workforce ---------------------------------------------
        'agents': agents,
        'agents_with_live_model': sum(1 for a in agents if a.has_live_llm),

        # --- Governance -------------------------------------------------
        'action_pending_count': pending_actions.count(),
        'action_high_risk_count': pending_actions.filter(risk='high').count(),
        'action_edited_count': pending_actions.filter(edit_count__gt=0).count(),
        'action_approved_today': actions.filter(
            status__in=('approved', 'executed'), decided_at__date=today).count(),
        'action_executed_count': actions.filter(status='executed').count(),
        'action_simulated_count': actions.filter(
            status='executed', executed_in_demo=True).count(),
        'action_failed_count': actions.filter(status='failed').count(),
        'oldest_pending_action': oldest_pending,
        'recent_actions': list(actions[:6]),

        # The older text-approval queue, still in use for chat replies.
        'pending_count': ApprovalRequest.objects.filter(status='pending').count(),
        'recent_approvals': list(
            ApprovalRequest.objects.select_related('agent')[:5]),

        # --- Operations -------------------------------------------------
        'open_role_count': JobOpening.objects.filter(status='open').count(),
        'candidate_active_count': Candidate.objects.exclude(
            status__in=('hired', 'rejected', 'withdrawn')).count(),
        'shortlisted_count': Candidate.objects.filter(status='shortlisted').count(),
        'interviews_this_week': Interview.objects.filter(
            scheduled_at__gte=timezone.now(), scheduled_at__lte=week_ahead,
            status__in=('scheduled', 'rescheduled')).count(),

        'open_work_item_count': _work_item_counts()['open'],
        'overdue_work_item_count': _work_item_counts()['overdue'],
        'blocked_work_item_count': _work_item_counts()['blocked'],

        'open_ticket_count': tickets.count(),
        'urgent_ticket_count': tickets.filter(
            Q(priority='urgent') | Q(needs_human=True)).count(),
        'needs_human_tickets': list(
            tickets.filter(needs_human=True).select_related('customer')[:4]),

        'content_pending_count': ContentPiece.objects.filter(
            status__in=('draft', 'pending')).count(),
        'content_published_count': ContentPiece.objects.filter(
            status='published').count(),

        'document_count': KnowledgeDocument.objects.filter(is_active=True).count(),
        'chunk_count': DocumentChunk.objects.count(),

        # --- Platform ---------------------------------------------------
        'integrations': integrations,
        'integration_live_count': sum(
            1 for row in integrations if row.effective_mode == 'live'),
        'integration_demo_count': sum(
            1 for row in integrations if row.effective_mode == 'demo'),
        'integration_total': len(integrations),
        'configured_providers': sum(
            1 for provider in LLMProvider.objects.all() if provider.is_configured),
        'online_servers': MCPServer.objects.filter(
            is_enabled=True, connection_status='connected').count(),
        'total_servers': MCPServer.objects.count(),

        # --- Activity ---------------------------------------------------
        'recent_events': audit_log.recent(limit=12),
        'events_today': AuditEvent.objects.filter(created_at__date=today).count(),
        'tasks_completed_fortnight': AgentTask.objects.filter(
            status='done', completed_at__gte=fortnight_ago).count(),

        # --- Legacy marketing figures, still shown lower down -----------
        'campaigns': list(MarketingCampaign.objects.select_related('assigned_agent')[:6]),
        'campaign_form': campaign_form,
        'active_campaigns': MarketingCampaign.objects.filter(status='active').count(),
        'total_leads': Lead.objects.count(),
        'hot_leads': Lead.objects.filter(score_tier='hot').count(),
        'published_posts': SocialPost.objects.filter(status='published').count(),
    }

    context['readiness'] = _readiness(context)
    return render(request, 'dashboard.html', context)


def _work_item_counts():
    """Open, overdue and blocked engineering work, in one pass."""
    from .models_eng import WorkItem

    open_items = WorkItem.objects.exclude(status__in=('done', 'cancelled'))
    return {
        'open': open_items.count(),
        'overdue': open_items.filter(due_date__lt=timezone.localdate()).count(),
        'blocked': open_items.filter(status='blocked').count(),
    }


def _readiness(context):
    """What is not set up yet, what still works without it, and where to fix it.

    Each entry is a dict the template renders directly, so the shape is the
    contract: `level` ('ok'/'warn'/'todo'), `title`, `detail`, `url_name`,
    `action`. The order is the order a person should deal with them.

    Written as data rather than as template conditionals because the same
    assessment is wanted by the `workforce_status` management command, and
    because a checklist assembled from six nested {% if %} blocks in a
    template is a checklist nobody can reorder.
    """
    rows = []

    if not context['configured_providers']:
        rows.append({
            'level': 'todo',
            'title': 'No language model is connected',
            'detail': ('The employees still run their tools and produce real '
                       'records without one, but they cannot write prose. Add an '
                       'OpenRouter or NVIDIA key, or point at a local Ollama; '
                       'free options exist for all three.'),
            'url_name': 'configurations',
            'action': 'Connect a model',
        })
    else:
        rows.append({
            'level': 'ok',
            'title': f"{context['configured_providers']} language model provider"
                     f"{'s' if context['configured_providers'] != 1 else ''} connected",
            'detail': f"{context['agents_with_live_model']} of the six employees can "
                      f"reach the model assigned to them.",
            'url_name': 'configurations',
            'action': 'Review',
        })

    if not context['integration_live_count']:
        rows.append({
            'level': 'warn',
            'title': 'Every connected application is in demo mode',
            'detail': ('Nothing is broken. Each of the eleven integrations '
                       'simulates its actions and labels every one of them as '
                       'simulated, so the whole workflow is demonstrable. Add a '
                       'credential to any one of them to make it real.'),
            'url_name': 'integrations',
            'action': 'Configure an application',
        })
    else:
        rows.append({
            'level': 'ok',
            'title': f"{context['integration_live_count']} of "
                     f"{context['integration_total']} applications are live",
            'detail': (f"{context['integration_demo_count']} are still simulating, "
                       f"which is clearly marked wherever their actions appear."),
            'url_name': 'integrations',
            'action': 'Review',
        })

    if not context['document_count']:
        rows.append({
            'level': 'todo',
            'title': 'The knowledge base is empty',
            'detail': ('Five of the six employees are instructed not to invent '
                       'company facts, so with nothing indexed they can only say '
                       'they do not know. Add a document, or sync one of the '
                       'document sources.'),
            'url_name': 'ops_knowledge',
            'action': 'Add knowledge',
        })
    else:
        rows.append({
            'level': 'ok',
            'title': f"{context['document_count']} documents indexed",
            'detail': f"Searchable as {context['chunk_count']} passages, so a "
                      f"question returns the paragraph that answers it.",
            'url_name': 'ops_knowledge',
            'action': 'Search',
        })

    if context['action_pending_count']:
        oldest = context['oldest_pending_action']
        age = f"{oldest.age_hours} hours" if oldest else 'some time'
        rows.append({
            'level': 'warn' if context['action_high_risk_count'] else 'todo',
            'title': f"{context['action_pending_count']} actions are waiting for a person",
            'detail': (f"{context['action_high_risk_count']} of them would reach "
                       f"somebody outside the company. The oldest has been waiting "
                       f"{age}."),
            'url_name': 'actions',
            'action': 'Review the queue',
        })

    if context['urgent_ticket_count']:
        rows.append({
            'level': 'warn',
            'title': f"{context['urgent_ticket_count']} support tickets need attention",
            'detail': ('These are either marked urgent or flagged as needing a '
                       'person rather than an AI reply.'),
            'url_name': 'ops_support',
            'action': 'Open Support',
        })

    return rows


# ===========================================================================
# Page 2: AI Employees -- the chat interface
#
# An employee is talked to rather than triggered. The page is laid out like a
# chat client: a rail of conversations on the left, the active thread in the
# middle, and a slide-out panel holding that employee's configuration.
# ===========================================================================

@login_required(login_url='login')
def agents_view(request, conversation_pk=None):
    """The chat screen.

    With no conversation in the URL it opens the most recent one, and creates
    a first thread if the account has none, so the page is never empty.
    """
    user = request.user
    agents = (AIAgent.objects.all()
              .select_related('llm_model', 'llm_model__provider')
              .prefetch_related('tool_links__tool__server'))

    if not agents:
        agent_engine.ensure_workspace_for_user(user)
        agents = AIAgent.objects.select_related('llm_model')

    conversations = (Conversation.objects.filter(user=user, is_archived=False)
                     .select_related('agent'))

    if conversation_pk is not None:
        conversation = get_object_or_404(
            Conversation.objects.select_related('agent'), pk=conversation_pk, user=user)
    else:
        conversation = conversations.first()
        if conversation is None:
            conversation = agent_engine.start_conversation(user, agents[0])
            conversations = (Conversation.objects.filter(user=user, is_archived=False)
                             .select_related('agent'))

    agent = conversation.agent
    agent_form = AIAgentForm(instance=agent, user=user)

    if request.method == 'POST':
        # The configuration panel posts here. It is a real form, so the
        # employee can still be edited with JavaScript switched off.
        if not _requires(request, roles.CAN_EDIT_AGENT):
            messages.error(request, 'Your role does not allow editing an AI employee.')
            return redirect('conversation', conversation_pk=conversation.pk)
        agent_form = AIAgentForm(request.POST, instance=agent, user=user)
        if agent_form.is_valid():
            saved = agent_form.save()
            agent_engine.sync_agent_tools(saved, agent_form.cleaned_data['mcp_tools'], actor=user)
            messages.success(request, f'{saved.name} was updated.')
            return redirect('conversation', conversation_pk=conversation.pk)
        messages.error(request, 'The employee could not be saved. Check the form.')

    context = {
        'agents': agents,
        'agent': agent,
        'agent_form': agent_form,
        'conversation': conversation,
        'conversations': conversations,
        'chat_messages': conversation.messages.all(),
        'submittable_types': agent_engine.SUBMITTABLE_TYPES,
        'platforms': SocialPost.PLATFORM_CHOICES,
        'leads': Lead.objects.order_by('full_name')[:100],
        'live_llm_count': sum(1 for a in agents if a.has_live_llm),
    }
    return render(request, 'agents.html', context)


# ===========================================================================
# Page 3: Approvals
# ===========================================================================

@login_required(login_url='login')
def approvals_view(request):
    """The human-in-the-loop review queue."""
    user = request.user

    if request.method == 'POST':
        if not _requires(request, roles.CAN_APPROVE):
            messages.error(request, 'Only a Manager or Administrator may approve work.')
            return redirect('approvals')
        form = ApprovalDecisionForm(request.POST)
        if form.is_valid():
            approval = get_object_or_404(
                ApprovalRequest, pk=form.cleaned_data['approval_id'])
            if approval.is_pending:
                agent_engine.apply_approval(
                    approval, user,
                    form.cleaned_data['decision'],
                    form.cleaned_data['reason'])
                messages.success(
                    request, f'"{approval.title}" was {form.cleaned_data["decision"]}.')
            else:
                messages.warning(request, 'That item had already been decided.')
        else:
            messages.error(request,
                           form.errors.get('reason', ['That decision could not be recorded.'])[0])
        return redirect(f"{request.path}?status={request.POST.get('return_status', 'pending')}")

    queryset = (ApprovalRequest.objects.all()
                .select_related('agent', 'social_post', 'email_outreach', 'lead', 'decided_by'))

    status_filter = request.GET.get('status', 'pending')
    type_filter = request.GET.get('type', 'all')
    search_query = (request.GET.get('q') or '').strip()

    visible = queryset
    if status_filter != 'all':
        visible = visible.filter(status=status_filter)
    if type_filter != 'all':
        visible = visible.filter(item_type=type_filter)
    if search_query:
        visible = visible.filter(
            Q(title__icontains=search_query) | Q(payload_preview__icontains=search_query))

    paginator = Paginator(visible, 10)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj': page_obj,
        'approvals': page_obj.object_list,
        'decision_form': ApprovalDecisionForm(),
        'status_filter': status_filter,
        'type_filter': type_filter,
        'search_query': search_query,
        'item_types': ApprovalRequest.ITEM_TYPE_CHOICES,
        'pending_count': queryset.filter(status='pending').count(),
        'approved_count': queryset.filter(status='approved').count(),
        'rejected_count': queryset.filter(status='rejected').count(),
        'total_count': queryset.count(),
    }
    return render(request, 'approvals.html', context)


@login_required(login_url='login')
def approval_detail_view(request, pk):
    """One queued item in full, with its audit trail and an edit form."""
    user = request.user
    approval = get_object_or_404(
        ApprovalRequest.objects.select_related(
            'agent', 'social_post', 'email_outreach', 'lead', 'decided_by'),
        pk=pk)

    edit_form = (SocialPostForm(instance=approval.social_post, user=user)
                 if approval.social_post_id else None)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'edit' and approval.social_post_id:
            if not _requires(request, 'marketing.change_socialpost'):
                messages.error(request, 'Your role does not allow editing a draft.')
                return redirect('approval_detail', pk=approval.pk)
            edit_form = SocialPostForm(request.POST, instance=approval.social_post, user=user)
            if edit_form.is_valid():
                post = edit_form.save()
                approval.payload_preview = post.content
                approval.title = post.title
                approval.save(update_fields=['payload_preview', 'title'])
                approval.audit_entries.create(
                    actor=user, action='edited', note='Content edited before review.',
                    from_status=approval.status, to_status=approval.status)
                messages.success(request, 'The draft was updated.')
                return redirect('approval_detail', pk=approval.pk)
            messages.error(request, 'The draft could not be saved.')

        else:
            if not _requires(request, roles.CAN_APPROVE):
                messages.error(request, 'Only a Manager or Administrator may approve work.')
                return redirect('approval_detail', pk=approval.pk)
            decision_form = ApprovalDecisionForm(request.POST)
            if decision_form.is_valid():
                if approval.is_pending:
                    agent_engine.apply_approval(
                        approval, user,
                        decision_form.cleaned_data['decision'],
                        decision_form.cleaned_data['reason'])
                    messages.success(
                        request,
                        f'"{approval.title}" was {decision_form.cleaned_data["decision"]}.')
                else:
                    messages.warning(request, 'That item had already been decided.')
                return redirect('approvals')
            messages.error(
                request,
                decision_form.errors.get('reason', ['That decision could not be recorded.'])[0])

    context = {
        'approval': approval,
        'decision_form': ApprovalDecisionForm(initial={'approval_id': approval.pk}),
        'edit_form': edit_form,
        'audit_entries': approval.audit_entries.select_related('actor'),
    }
    return render(request, 'approval_detail.html', context)


# ===========================================================================
# Page 4: Users
# ===========================================================================

@login_required(login_url='login')
def users_view(request):
    """Everyone with access to the platform.

    The page is readable by any signed-in user; the buttons that change an
    account are shown only to staff, and the matching JSON endpoints enforce
    that server-side as well.
    """
    can_manage = request.user.has_perm(roles.CAN_MANAGE_USERS)
    can_assign_role = request.user.has_perm(roles.CAN_ASSIGN_ROLE)

    if request.method == 'POST':
        if not can_assign_role:
            messages.error(request, 'Only an Administrator may change a role.')
            return redirect('users')
        target = get_object_or_404(User, pk=request.POST.get('user_id'))
        profile_form = ProfileForm(request.POST, instance=target.profile)
        if profile_form.is_valid():
            profile_form.save()
            messages.success(request, f"{target.username}'s profile was updated.")
        else:
            messages.error(request, 'The profile could not be saved.')
        return redirect('users')

    # A single annotated query, so rendering the table costs no extra lookups.
    queryset = (User.objects
                .select_related('profile')
                .annotate(agent_count=Count('agents', distinct=True),
                          lead_count=Count('leads', distinct=True),
                          approval_count=Count('approval_requests', distinct=True))
                .order_by('-date_joined'))

    role_filter = request.GET.get('role', 'all')
    status_filter = request.GET.get('status', 'all')
    search_query = (request.GET.get('q') or '').strip()

    visible = queryset
    if role_filter != 'all':
        visible = visible.filter(profile__role=role_filter)
    if status_filter == 'active':
        visible = visible.filter(is_active=True)
    elif status_filter == 'suspended':
        visible = visible.filter(is_active=False)
    if search_query:
        visible = visible.filter(
            Q(username__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query))

    paginator = Paginator(visible, 12)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj': page_obj,
        'users': page_obj.object_list,
        'profile_form': ProfileForm(),
        'can_manage': can_manage,
        'can_assign_role': can_assign_role,
        'can_add_user': request.user.has_perm(roles.CAN_ADD_USER),
        'can_delete_user': request.user.has_perm(roles.CAN_DELETE_USER),
        'role_filter': role_filter,
        'status_filter': status_filter,
        'search_query': search_query,
        'role_choices': [('all', 'All roles')] + list(Profile.ROLE_CHOICES),
        'total_users': queryset.count(),
        'active_users': queryset.filter(is_active=True).count(),
        'staff_users': queryset.filter(is_staff=True).count(),
    }
    return render(request, 'users.html', context)


# ===========================================================================
# Page 5: Configurations
# ===========================================================================

@login_required(login_url='login')
def configurations_view(request):
    """LLM provider credentials, endpoints and model catalogues.

    Provisioning is not repeated here. The shared workspace is created once, at
    login, by agent_engine.ensure_workspace_for_user; running it again on every
    page view cost a handful of queries per request and bought nothing.
    """
    user = request.user

    if request.method == 'POST':
        if not _requires(request, roles.CAN_CONFIGURE_PROVIDER):
            messages.error(request, 'Only an Administrator may change provider settings.')
            return redirect('configurations')
        provider = get_object_or_404(LLMProvider, pk=request.POST.get('provider_id'))
        form = LLMProviderForm(request.POST, instance=provider)
        if form.is_valid():
            form.save()
            messages.success(request, f'{provider.label} settings were saved.')
        else:
            messages.error(request, f'{provider.label} settings could not be saved.')
        return redirect('configurations')

    providers = list(LLMProvider.objects.prefetch_related('models'))

    context = {
        'providers': providers,
        'models_by_provider': {
            p.pk: list(p.models.filter(is_enabled=True)) for p in providers},
        'configured_count': sum(1 for p in providers if p.is_configured),
        'online_count': sum(1 for p in providers if p.connection_status == 'online'),
        'total_models': LLMModel.objects.count(),
        # Email delivery, which is configured by environment variable rather
        # than through this page: a mailbox password should not be typed into
        # a form or stored in the database.
        'email_configured': mailer.is_configured(),
        'email_backend': mailer.backend_label(),
        'email_host': settings.EMAIL_HOST,
        'email_port': settings.EMAIL_PORT,
        'email_user': settings.EMAIL_HOST_USER,
        'email_from': settings.DEFAULT_FROM_EMAIL,

        # Consumed by static/js/config.js to drive the cascading dropdown
        # without a round trip on first render.
        'provider_model_map': {
            str(p.pk): [{'id': m.pk, 'model_id': m.model_id, 'label': str(m),
                         'context': m.context_label}
                        for m in p.models.filter(is_enabled=True)]
            for p in providers
        },
    }
    return render(request, 'configurations.html', context)


# ===========================================================================
# Page 6: MCP Tools
# ===========================================================================

@login_required(login_url='login')
def mcp_tools_view(request):
    """The seven Model Context Protocol servers and their capabilities.

    As with the Configurations page, provisioning happens once at login rather
    than on every request.
    """
    user = request.user

    if request.method == 'POST':
        if not _requires(request, roles.CAN_MANAGE_MCP):
            messages.error(request, 'Your role does not allow changing an MCP server.')
            return redirect('mcp_tools')
        server = get_object_or_404(MCPServer, pk=request.POST.get('server_id'))
        form = MCPServerForm(request.POST, instance=server)
        if form.is_valid():
            form.save()
            messages.success(request, f'{server.name} was updated.')
        else:
            messages.error(request, f'{server.name} could not be saved. Check the form.')
        return redirect('mcp_tools')

    servers = list(MCPServer.objects.prefetch_related('tools'))

    context = {
        'servers': servers,
        'forms_by_server': {s.pk: MCPServerForm(instance=s) for s in servers},
        'categories': MCPServer.CATEGORY_CHOICES,
        'total_servers': len(servers),
        'enabled_servers': sum(1 for s in servers if s.is_enabled),
        'online_count': sum(1 for s in servers if s.is_online),
        'total_tools': MCPTool.objects.count(),
        'enabled_tools': MCPTool.objects.filter(is_enabled=True).count(),
    }
    return render(request, 'mcp_tools.html', context)


@login_required(login_url='login')
def mcp_server_detail_view(request, pk):
    """One MCP server: connection settings, its tools, and recent calls."""
    user = request.user
    server = get_object_or_404(MCPServer, pk=pk)

    form = MCPServerForm(instance=server)
    tool_form = MCPToolForm(initial={'server': server}, user=user)

    if request.method == 'POST':
        if not _requires(request, roles.CAN_MANAGE_MCP):
            messages.error(request, 'Your role does not allow changing an MCP server.')
            return redirect('mcp_server_detail', pk=server.pk)
        if request.POST.get('action') == 'add_tool':
            tool_form = MCPToolForm(request.POST, user=user)
            if tool_form.is_valid():
                tool = tool_form.save()
                messages.success(request, f'Tool "{tool.display_name}" was added.')
                return redirect('mcp_server_detail', pk=server.pk)
            messages.error(request, 'The tool could not be added.')
        else:
            form = MCPServerForm(request.POST, instance=server)
            if form.is_valid():
                form.save()
                messages.success(request, f'{server.name} was updated.')
                return redirect('mcp_server_detail', pk=server.pk)
            messages.error(request, 'The server could not be saved. Check the form.')

    context = {
        'server': server,
        'form': form,
        'tool_form': tool_form,
        'tools': server.tools.all(),
        'recent_calls': (MCPCallLog.objects.filter(tool__server=server)
                         .select_related('tool', 'agent')[:10]),
    }
    return render(request, 'mcp_server_detail.html', context)


# ===========================================================================
# CRUD: AI employees
#
# The four defaults are provisioned automatically, but a workspace is not
# limited to them. Each of these is gated on the matching Django permission,
# so a Viewer or Analyst reaching the URL directly gets a 403 rather than a
# hidden button they could have guessed at.
# ===========================================================================

@login_required(login_url='login')
@permission_required(roles.CAN_ADD_AGENT, raise_exception=True)
def agent_create_view(request):
    """Add a new AI employee to the shared workspace."""
    if request.method == 'POST':
        form = AIAgentForm(request.POST)
        if form.is_valid():
            agent = form.save(commit=False)
            agent.user = request.user          # provenance, not ownership
            if not agent.system_prompt.strip():
                agent.system_prompt = (f'You are {agent.name}, a {agent.role}. '
                                       f'{agent.persona_description}')
            agent.save()
            agent_engine.sync_agent_tools(agent, form.cleaned_data['mcp_tools'],
                                          actor=request.user)
            messages.success(request, f'{agent.name} joined the workforce.')
            return redirect('agents')
        messages.error(request, 'The employee could not be created. Check the form.')
    else:
        form = AIAgentForm()

    return render(request, 'agent_form.html', {
        'form': form,
        'heading': 'New AI employee',
        'subheading': 'Give the employee a persona, a system prompt and its tools.',
        'submit_label': 'Create employee',
        'cancel_url': reverse('agents'),
    })


@login_required(login_url='login')
@permission_required(roles.CAN_DELETE_AGENT, raise_exception=True)
@require_POST
def agent_delete_view(request, pk):
    """Remove an AI employee.

    POST only, so a link or a crawler can never delete one. Conversations held
    with the employee cascade away with it, which the confirmation says plainly.
    """
    agent = get_object_or_404(AIAgent, pk=pk)
    name = agent.name
    agent.delete()
    messages.success(request, f'{name} was removed from the workforce.')
    return redirect('agents')


# ===========================================================================
# CRUD: MCP servers and tools
# ===========================================================================

@login_required(login_url='login')
@permission_required(roles.CAN_ADD_MCP, raise_exception=True)
def mcp_server_create_view(request):
    """Register a new Model Context Protocol server."""
    if request.method == 'POST':
        form = MCPServerForm(request.POST)
        if form.is_valid():
            server = form.save(commit=False)
            server.user = request.user
            server.save()
            mcp_client.simulate_handshake(server)
            messages.success(request, f'{server.name} was registered.')
            return redirect('mcp_server_detail', pk=server.pk)
        messages.error(request, 'The server could not be created. Check the form.')
    else:
        form = MCPServerForm()

    return render(request, 'mcp_server_form.html', {
        'form': form,
        'heading': 'Register an MCP server',
        'subheading': 'Connection details are stored and validated; no process is launched.',
        'submit_label': 'Register server',
        'cancel_url': reverse('mcp_tools'),
    })


@login_required(login_url='login')
@permission_required(roles.CAN_DELETE_MCP, raise_exception=True)
@require_POST
def mcp_server_delete_view(request, pk):
    """Remove an MCP server and every capability it advertised."""
    server = get_object_or_404(MCPServer, pk=pk)
    name = server.name
    server.delete()
    messages.success(request, f'{name} and its tools were removed.')
    return redirect('mcp_tools')


@login_required(login_url='login')
@permission_required('marketing.delete_mcptool', raise_exception=True)
@require_POST
def mcp_tool_delete_view(request, pk):
    """Remove one capability from a server."""
    tool = get_object_or_404(MCPTool.objects.select_related('server'), pk=pk)
    server_pk = tool.server_id
    name = tool.display_name
    tool.delete()
    messages.success(request, f'"{name}" was removed.')
    return redirect('mcp_server_detail', pk=server_pk)


# ===========================================================================
# CRUD: people
#
# Creating an account here also assigns its role, which is what actually
# determines what that person can do. The Profile post_save signal moves them
# into the matching Django group.
# ===========================================================================

@login_required(login_url='login')
@permission_required(roles.CAN_ADD_USER, raise_exception=True)
def user_create_view(request):
    """Add a person to the platform and give them a role."""
    if request.method == 'POST':
        form = UserCreateForm(request.POST)
        if form.is_valid():
            new_user = form.save()
            messages.success(
                request,
                f'{new_user.username} was added as '
                f'{new_user.profile.get_role_display()}.')
            return redirect('users')
        messages.error(request, 'The account could not be created. Check the form.')
    else:
        form = UserCreateForm()

    return render(request, 'user_form.html', {
        'form': form,
        'heading': 'Add a person',
        'subheading': 'Their role decides what they may do once they sign in.',
        'submit_label': 'Create account',
        'cancel_url': reverse('users'),
        'is_create': True,
    })


@login_required(login_url='login')
@permission_required(roles.CAN_MANAGE_USERS, raise_exception=True)
def user_edit_view(request, pk):
    """Edit someone's details and their role."""
    target = get_object_or_404(User.objects.select_related('profile'), pk=pk)

    if request.method == 'POST':
        form = UserEditForm(request.POST, instance=target,
                            editor=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, f'{target.username} was updated.')
            return redirect('users')
        messages.error(request, 'The account could not be saved. Check the form.')
    else:
        form = UserEditForm(instance=target, editor=request.user)

    return render(request, 'user_form.html', {
        'form': form,
        'target': target,
        'heading': f'Edit {target.username}',
        'subheading': 'Changing the role moves this person into the matching group.',
        'submit_label': 'Save changes',
        'cancel_url': reverse('users'),
        'is_create': False,
    })


@login_required(login_url='login')
@permission_required(roles.CAN_DELETE_USER, raise_exception=True)
@require_POST
def user_delete_view(request, pk):
    """Delete an account.

    Two guards, both of which would otherwise be easy to trip over during a
    demonstration: nobody may delete themselves, and the last remaining
    Administrator may not be removed, which would leave the workspace with
    nobody able to manage it.
    """
    target = get_object_or_404(User.objects.select_related('profile'), pk=pk)

    if target.pk == request.user.pk:
        messages.error(request, 'You cannot delete your own account.')
        return redirect('users')

    if target.profile.role == roles.ROLE_ADMIN:
        remaining = (Profile.objects.filter(role=roles.ROLE_ADMIN)
                     .exclude(user_id=target.pk).count())
        if remaining == 0:
            messages.error(
                request,
                'That is the only Administrator. Promote someone else first.')
            return redirect('users')

    username = target.username
    target.delete()
    messages.success(request, f'{username} was removed.')
    return redirect('users')


# ===========================================================================
# JSON endpoints
# ===========================================================================

@login_required(login_url='login')
@require_POST
def api_edit_agent(request):
    """Save an AI employee from the editor modal."""
    if not _requires(request, roles.CAN_EDIT_AGENT):
        return _forbidden(roles.CAN_EDIT_AGENT)
    data = _json_body(request)
    agent = get_object_or_404(AIAgent, pk=data.get('agent_id'))

    for field in ('name', 'role', 'status', 'persona_description', 'system_prompt'):
        if field in data:
            setattr(agent, field, data[field])

    if 'llm_model_id' in data:
        model_id = data['llm_model_id']
        agent.llm_model = (
            LLMModel.objects.filter(pk=model_id).first()
            if model_id else None)

    if 'temperature' in data:
        try:
            agent.temperature = max(0.0, min(2.0, float(data['temperature'])))
        except (TypeError, ValueError):
            return _error('Temperature must be a number between 0 and 2.')

    if 'max_tokens' in data:
        try:
            agent.max_tokens = max(64, min(8192, int(data['max_tokens'])))
        except (TypeError, ValueError):
            return _error('Maximum tokens must be a whole number between 64 and 8192.')

    agent.save()

    if 'mcp_tool_ids' in data:
        tools = MCPTool.objects.filter(pk__in=data['mcp_tool_ids'] or [])
        agent_engine.sync_agent_tools(agent, tools, actor=request.user)

    return JsonResponse({
        'status': 'success',
        'message': f'{agent.name} was saved.',
        'agent_id': agent.pk,
        'name': agent.name,
        'llm_label': agent.llm_label,
        'tool_count': agent.tool_links.count(),
    })


@login_required(login_url='login')
@require_POST
def api_send_message(request):
    """Post a message into a conversation and return the employee's reply."""
    if not _requires(request, roles.CAN_CHAT):
        return _forbidden(roles.CAN_CHAT)
    data = _json_body(request)
    text = (data.get('text') or '').strip()
    if not text:
        return _error('Type a message first.')

    conversation = get_object_or_404(
        Conversation.objects.select_related('agent'),
        pk=data.get('conversation_id'), user=request.user)

    user_message, assistant_message = agent_engine.send_message(
        conversation, text, user=request.user)

    return JsonResponse({
        'status': 'success',
        'conversation_id': conversation.pk,
        'conversation_title': conversation.title,
        'user_message': _serialise_message(user_message),
        'assistant_message': _serialise_message(assistant_message),
    })


@login_required(login_url='login')
@require_POST
def api_new_conversation(request):
    """Start a fresh thread with one employee."""
    if not _requires(request, roles.CAN_CHAT):
        return _forbidden(roles.CAN_CHAT)
    data = _json_body(request)
    agent = get_object_or_404(AIAgent, pk=data.get('agent_id'))
    conversation = agent_engine.start_conversation(request.user, agent)
    return JsonResponse({
        'status': 'success',
        'conversation_id': conversation.pk,
        'title': conversation.title,
        'agent_name': agent.name,
        'url': reverse('conversation', args=[conversation.pk]),
    })


@login_required(login_url='login')
@require_POST
def api_rename_conversation(request):
    """Rename a thread."""
    data = _json_body(request)
    title = (data.get('title') or '').strip()
    if not title:
        return _error('A conversation needs a title.')

    conversation = get_object_or_404(
        Conversation, pk=data.get('conversation_id'), user=request.user)
    conversation.title = title[:120]
    conversation.save(update_fields=['title', 'updated_at'])

    return JsonResponse({'status': 'success',
                         'conversation_id': conversation.pk,
                         'title': conversation.title})


@login_required(login_url='login')
@require_POST
def api_delete_conversation(request):
    """Delete a thread and every message in it."""
    conversation = get_object_or_404(
        Conversation, pk=_json_body(request).get('conversation_id'), user=request.user)
    conversation_id = conversation.pk
    title = conversation.title
    conversation.delete()

    remaining = Conversation.objects.filter(user=request.user, is_archived=False).first()
    return JsonResponse({
        'status': 'success',
        'deleted_id': conversation_id,
        'message': f'"{title}" was deleted.',
        'next_url': (reverse('conversation', args=[remaining.pk]) if remaining
                     else reverse('agents')),
    })


@login_required(login_url='login')
@require_POST
def api_send_now(request):
    """Approve and send one reply in a single click, from the chat itself.

    The two-step route still exists and is unchanged: submit here, decide on
    the Approvals page. This endpoint is the same two steps performed together,
    for the case where the reviewer is the person reading the reply and the
    address is already known.

    Nothing is skipped. An ApprovalRequest is still created, the decision is
    still recorded against the person who made it, and the audit log still
    gets both entries -- so the queue remains a complete record of everything
    that was ever sent. What is removed is the dialog and the page change, not
    the accountability.

    Both permissions are required, because this performs both actions.
    """
    data = _json_body(request)
    message = get_object_or_404(
        ChatMessage.objects.select_related('conversation', 'conversation__agent'),
        pk=data.get('message_id'), conversation__user=request.user)

    for permission in (roles.CAN_SUBMIT, roles.CAN_APPROVE):
        if not _requires(request, permission):
            return _forbidden(permission)

    if not message.is_assistant:
        return _error('Only a reply from an AI employee can be sent.')

    recipient = (data.get('recipient_email') or message.draft_recipient or '').strip()
    if not recipient:
        return _error('There is no address to send this to.')
    try:
        validate_email(recipient)
    except ValidationError:
        return _error(f'"{recipient}" is not a valid email address.')

    if message.submitted_approval:
        return _error('This reply has already been sent for approval.')

    subject, _body = agent_engine.split_subject(message.content, fallback='')
    try:
        approval = agent_engine.submit_message_for_approval(
            message, item_type='email', title=subject or message.content[:60],
            recipient_email=recipient)
    except ValueError as exc:
        return _error(str(exc))

    agent_engine.apply_approval(approval, request.user, 'approved')
    approval.refresh_from_db()
    email = approval.email_outreach

    # apply_approval never raises on a delivery failure -- it records the reason
    # on the row so the decision is not lost. Report it honestly rather than
    # claiming the message went out.
    if email.delivery_error:
        return JsonResponse({
            'status': 'error',
            'message': f'Not sent: {email.delivery_error}',
            'approval_id': approval.pk,
            'html': _serialise_message(message)['html'],
        }, status=200)

    return JsonResponse({
        'status': 'success',
        'message': f'Sent to {email.recipient}.',
        'approval_id': approval.pk,
        'approval_url': reverse('approval_detail', args=[approval.pk]),
        'html': _serialise_message(message)['html'],
        'pending_count': _pending_count(),
    })


@login_required(login_url='login')
@require_POST
def api_submit_for_approval(request):
    """Send one assistant reply from a conversation to the approval queue.

    This is the only route by which chat output can ever reach the outside
    world, and it is always a person who chooses to take it.
    """
    data = _json_body(request)
    message = get_object_or_404(
        ChatMessage.objects.select_related('conversation', 'conversation__agent'),
        pk=data.get('message_id'), conversation__user=request.user)

    if not _requires(request, roles.CAN_SUBMIT):
        return _forbidden(roles.CAN_SUBMIT)

    if not message.is_assistant:
        return _error('Only a reply from an AI employee can be submitted for approval.')

    item_type = data.get('item_type') or 'insight'
    title = (data.get('title') or '').strip() or message.content[:60]

    lead = None
    recipient_email = (data.get('recipient_email') or '').strip()
    if item_type == 'email':
        lead = Lead.objects.filter(pk=data.get('lead_id')).first()
        if lead is None and not recipient_email:
            return _error('Enter an address, or pick a prospect, to send this to.')
        if recipient_email:
            try:
                validate_email(recipient_email)
            except ValidationError:
                return _error(f'"{recipient_email}" is not a valid email address.')

    try:
        approval = agent_engine.submit_message_for_approval(
            message,
            item_type=item_type,
            title=title,
            platform=data.get('platform') or 'linkedin',
            lead=lead,
            recipient_email=recipient_email,
        )
    except ValueError as exc:
        return _error(str(exc))

    return JsonResponse({
        'status': 'success',
        'message': f'"{approval.title}" was sent for approval.',
        'approval_id': approval.pk,
        'approval_url': reverse('approval_detail', args=[approval.pk]),
        'pending_count': _pending_count(),
    })


@login_required(login_url='login')
@require_POST
def api_approval_decision(request):
    """Approve or reject one queued item."""
    if not _requires(request, roles.CAN_APPROVE):
        return _forbidden(roles.CAN_APPROVE)
    data = _json_body(request)
    decision = data.get('decision')
    reason = (data.get('reason') or '').strip()

    if decision not in ('approved', 'rejected'):
        return _error('The decision must be either "approved" or "rejected".')
    if decision == 'rejected' and not reason:
        return _error('A reason is required when rejecting an item.')

    approval = get_object_or_404(ApprovalRequest, pk=data.get('approval_id'))
    if not approval.is_pending:
        return _error('That item has already been decided.', status=409)

    agent_engine.apply_approval(approval, request.user, decision, reason)

    return JsonResponse({
        'status': 'success',
        'message': f'"{approval.title}" was {decision}.',
        'approval_id': approval.pk,
        'new_status': approval.status,
        'pending_count': _pending_count(),
    })


@login_required(login_url='login')
@require_POST
def api_bulk_approval(request):
    """Apply the same decision to several queued items at once."""
    if not _requires(request, roles.CAN_APPROVE):
        return _forbidden(roles.CAN_APPROVE)
    data = _json_body(request)
    decision = data.get('decision')
    reason = (data.get('reason') or '').strip()

    if decision not in ('approved', 'rejected'):
        return _error('The decision must be either "approved" or "rejected".')
    if decision == 'rejected' and not reason:
        return _error('A reason is required when rejecting items.')

    approvals = ApprovalRequest.objects.filter(
        pk__in=data.get('approval_ids') or [], status='pending')

    updated = 0
    for approval in approvals:
        agent_engine.apply_approval(approval, request.user, decision, reason)
        updated += 1

    return JsonResponse({
        'status': 'success',
        'message': f'{updated} items were {decision}.',
        'updated': updated,
        'pending_count': _pending_count(),
    })


@login_required(login_url='login')
@require_POST
def api_test_provider(request):
    """Probe a provider's endpoint and store the result."""
    if not _requires(request, roles.CAN_TEST_PROVIDER):
        return _forbidden(roles.CAN_TEST_PROVIDER)
    provider = get_object_or_404(
        LLMProvider, pk=_json_body(request).get('provider_id'))

    result = llm_client.test_connection(provider)

    provider.connection_status = result['connection_status']
    provider.last_test_message = result['message'][:255]
    provider.last_latency_ms = result['latency_ms']
    provider.last_tested_at = timezone.now()
    provider.save(update_fields=['connection_status', 'last_test_message',
                                 'last_latency_ms', 'last_tested_at'])

    return JsonResponse({
        'status': 'success' if result['ok'] else 'error',
        'connection_status': result['connection_status'],
        'status_display': provider.get_connection_status_display(),
        'message': result['message'],
        'latency_ms': result['latency_ms'],
        'tested_at': provider.last_tested_at.strftime('%d %b %Y at %H:%M'),
    })


@login_required(login_url='login')
@require_POST
def api_fetch_models(request):
    """Refresh a provider's model catalogue from its live endpoint."""
    if not _requires(request, roles.CAN_TEST_PROVIDER):
        return _forbidden(roles.CAN_TEST_PROVIDER)
    provider = get_object_or_404(
        LLMProvider, pk=_json_body(request).get('provider_id'))

    result = llm_client.sync_models_for_provider(provider)
    models = provider.models.filter(is_enabled=True)

    return JsonResponse({
        'status': 'success',
        'source': result['source'],
        'message': result['message'],
        'count': result['total'],
        'models': [{'id': m.pk, 'model_id': m.model_id, 'label': str(m),
                    'context': m.context_label} for m in models],
    })


@login_required(login_url='login')
@require_POST
def api_provider_models(request):
    """Return one provider's models. Drives the cascading dropdown."""
    provider = get_object_or_404(
        LLMProvider, pk=_json_body(request).get('provider_id'))

    models = provider.models.filter(is_enabled=True)
    return JsonResponse({
        'status': 'success',
        'provider': provider.label,
        'models': [{'id': m.pk, 'model_id': m.model_id, 'label': str(m),
                    'context': m.context_label} for m in models],
    })


@login_required(login_url='login')
@require_POST
def api_test_email(request):
    """Authenticate against the mail server without sending anything."""
    if not _requires(request, roles.CAN_TEST_PROVIDER):
        return _forbidden(roles.CAN_TEST_PROVIDER)

    result = mailer.check_connection()
    return JsonResponse({
        'status': 'success' if result['ok'] else 'error',
        'ok': result['ok'],
        'configured': result['configured'],
        'backend': result['backend'],
        'message': result['message'],
    })


@login_required(login_url='login')
@require_POST
def api_send_test_email(request):
    """Send one short message, to prove the path end to end.

    Administrator only. This is the one place in the application that sends
    email without an approval, which is defensible because it goes to an
    address the sender types and touches no prospect record.
    """
    if not _requires(request, roles.CAN_CONFIGURE_PROVIDER):
        return _forbidden(roles.CAN_CONFIGURE_PROVIDER)

    recipient = (_json_body(request).get('recipient') or '').strip()
    if not recipient:
        return _error('Type the address to send the test to.')

    try:
        validate_email(recipient)
    except ValidationError:
        return _error(f'"{recipient}" is not a valid email address.')

    result = mailer.send_test_message(recipient)
    return JsonResponse({
        'status': 'success' if result['ok'] else 'error',
        'message': result['message'],
    })


@login_required(login_url='login')
@require_POST
def api_test_mcp_server(request):
    """Run the simulated MCP handshake against one server."""
    if not _requires(request, roles.CAN_TEST_MCP):
        return _forbidden(roles.CAN_TEST_MCP)
    server = get_object_or_404(
        MCPServer, pk=_json_body(request).get('server_id'))

    result = mcp_client.simulate_handshake(server)
    result['status'] = 'success' if result['ok'] else 'error'
    result['server_id'] = server.pk
    return JsonResponse(result)


@login_required(login_url='login')
@require_POST
def api_toggle_mcp_server(request):
    """Enable or disable one MCP server."""
    if not _requires(request, roles.CAN_MANAGE_MCP):
        return _forbidden(roles.CAN_MANAGE_MCP)
    data = _json_body(request)
    server = get_object_or_404(MCPServer, pk=data.get('server_id'))

    server.is_enabled = bool(data.get('enabled'))
    server.save(update_fields=['is_enabled'])
    result = mcp_client.simulate_handshake(server)

    return JsonResponse({
        'status': 'success',
        'server_id': server.pk,
        'is_enabled': server.is_enabled,
        'connection_status': server.connection_status,
        'status_display': server.get_connection_status_display(),
        'message': result['message'],
    })


@login_required(login_url='login')
@require_POST
def api_toggle_user_active(request):
    """Activate or suspend an account. Administrators only."""
    if not _requires(request, roles.CAN_MANAGE_USERS):
        return _forbidden(roles.CAN_MANAGE_USERS)

    data = _json_body(request)
    target = get_object_or_404(User, pk=data.get('user_id'))

    # Without this guard one click could sign the presenter out of the demo.
    if target.pk == request.user.pk:
        return _error('You cannot change the status of your own account.')

    target.is_active = bool(data.get('active'))
    target.save(update_fields=['is_active'])

    return JsonResponse({
        'status': 'success',
        'user_id': target.pk,
        'is_active': target.is_active,
        'message': f'{target.username} was '
                   f'{"activated" if target.is_active else "suspended"}.',
    })


@login_required(login_url='login')
@require_POST
def api_set_user_staff(request):
    """Grant or revoke Django admin access. Administrators only."""
    if not _requires(request, roles.CAN_MANAGE_USERS):
        return _forbidden(roles.CAN_MANAGE_USERS)

    data = _json_body(request)
    target = get_object_or_404(User, pk=data.get('user_id'))

    if target.pk == request.user.pk:
        return _error('You cannot change your own access level.')

    target.is_staff = bool(data.get('is_staff'))
    target.save(update_fields=['is_staff'])

    return JsonResponse({
        'status': 'success',
        'user_id': target.pk,
        'is_staff': target.is_staff,
        'message': f'{target.username} is now '
                   f'{"a staff member" if target.is_staff else "a standard user"}.',
    })


@login_required(login_url='login')
@require_POST
def api_set_theme(request):
    """Persist the light or dark preference on the user's profile."""
    theme = _json_body(request).get('theme')
    if theme not in ('light', 'dark'):
        return _error('The theme must be either "light" or "dark".')

    profile = request.user.profile
    profile.theme = theme
    profile.save(update_fields=['theme'])

    return JsonResponse({'status': 'success', 'theme': theme})
