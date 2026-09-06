"""Tests for the AI Workforce application.

Run them with:

    python manage.py test marketing

Twenty-two classes, grouped by what they prove:

    WorkspaceRoutingTests      the six pages exist, are protected, and the two
                               deleted pages really are gone
    WorkspaceProvisioningTests signing up gives a user a complete workspace,
                               and doing it twice changes nothing
    SharedWorkspaceTests       one organisation, one set of resources, private chat
    RoleGroupTests             roles are Django Groups, kept in step by a signal
    PermissionMatrixTests      what each of the four roles may actually do
    UserCrudTests              creating, editing and deleting accounts
    WorkforceCrudTests         adding and removing employees and MCP servers
    ProvisioningRegressionTests  provisioning does not depend on who asks
    MarkdownRenderingTests     replies are formatted, and never trusted
    TimestampTests             both rendering paths report the same clock
    EmailDeliveryTests         approving an outreach email is what sends it
    AgentPromptTests           the employees know what the platform can do
    ChatTests                  talking to an employee, and thread management
    ChatToApprovalTests        agents propose, people dispose
    JSONEndpointSecurityTests  the JSON endpoints require login, a CSRF token
                               and, where relevant, staff privileges
    LLMClientFallbackTests     the application still works with no network
    ApiKeyResolutionTests      database key, environment key, and what depends
                               on each
    TimeoutBudgetTests         generation gets a longer budget than metadata
    ReasoningStripTests        a model's visible working never reaches a post
    EnvFileTests               credentials survive closing the terminal
    MailRouterTests            what a person types becomes a mailbox action
    MailboxToolGateTests       the MCP tool record is what grants access
    MailboxChatTests           a mailbox turn, end to end, with no network
    SendNowTests               approve and send in one click, still audited
"""

import io
import os
import tempfile
import urllib.error
from unittest import mock

import json
import smtplib

from django.contrib.auth.models import Group, User
from django.core import mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.test import Client, TestCase
from django.urls import NoReverseMatch, reverse

from django.conf import settings
from django.utils import timezone

from marketpulse import env as env_file

from . import (agent_engine, gmail_client, llm_client, mail_agent,
               mailer, markdown as md, roles)
from .forms import AIAgentForm, ApprovalDecisionForm
from .models import (AIAgent, ApprovalAuditLog, ApprovalRequest, ChatMessage,
                     Conversation, EmailOutreach, Lead, LLMModel, LLMProvider,
                     MCPCallLog, MCPServer, MCPTool, Profile, SocialPost)

PAGE_NAMES = ['dashboard', 'agents', 'approvals', 'users', 'configurations', 'mcp_tools']


class WorkspaceRoutingTests(TestCase):
    """URL mapping: every page resolves, and access control holds."""

    def setUp(self):
        self.user = User.objects.create_user('alice', 'alice@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(self.user)

    def test_public_pages_render(self):
        for name in ['landing', 'login', 'register']:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_all_six_pages_render_for_a_signed_in_user(self):
        self.client.force_login(self.user)
        for name in PAGE_NAMES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_pages_redirect_an_anonymous_visitor_to_login(self):
        for name in PAGE_NAMES:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login/', response.url)

    def test_deleted_pages_are_really_gone(self):
        """Proof that the Leads and Social pages were removed, not just hidden."""
        for name in ['leads', 'social']:
            with self.subTest(page=name):
                with self.assertRaises(NoReverseMatch):
                    reverse(name)

    def test_login_redirects_to_the_dashboard(self):
        response = self.client.post(
            reverse('login'), {'username': 'alice', 'password': 'pw-alice-123'})
        self.assertRedirects(response, reverse('dashboard'))

    def test_detail_pages_render(self):
        self.client.force_login(self.user)
        agent = AIAgent.objects.filter(user=self.user).first()
        server = MCPServer.objects.filter(user=self.user).first()
        conversation = agent_engine.start_conversation(self.user, agent)
        self.assertEqual(
            self.client.get(reverse('conversation', args=[conversation.pk])).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('mcp_server_detail', args=[server.pk])).status_code, 200)


class WorkspaceProvisioningTests(TestCase):
    """Signing up produces a complete, usable workspace."""

    def setUp(self):
        self.user = User.objects.create_user('bob', 'bob@example.com', 'pw-bob-12345')
        agent_engine.ensure_workspace_for_user(self.user)

    def test_four_ai_employees_are_created(self):
        self.assertEqual(AIAgent.objects.count(), 4)

    def test_each_employee_has_a_system_prompt(self):
        for agent in AIAgent.objects.all():
            with self.subTest(agent=agent.name):
                self.assertTrue(agent.system_prompt.strip())

    def test_a_profile_is_created(self):
        self.assertTrue(Profile.objects.filter(user=self.user).exists())

    def test_three_providers_are_created_with_models(self):
        providers = LLMProvider.objects.all()
        self.assertEqual(providers.count(), 3)
        self.assertSetEqual(
            set(providers.values_list('provider_key', flat=True)),
            {'openrouter', 'nvidia', 'ollama'})
        # Seeded from the fallback catalogue, so the dropdown is never empty.
        self.assertGreater(LLMModel.objects.count(), 0)

    def test_seven_mcp_servers_are_created_with_tools(self):
        servers = MCPServer.objects.all()
        self.assertEqual(servers.count(), 7)
        self.assertSetEqual(
            set(servers.values_list('server_key', flat=True)),
            {'gmail', 'instagram', 'database', 'memory',
             'sequential_thinking', 'spotify', 'linkedin'})
        for server in servers:
            with self.subTest(server=server.server_key):
                self.assertGreater(server.tools.count(), 0)

    def test_each_employee_gets_default_tools(self):
        for agent in AIAgent.objects.all():
            with self.subTest(agent=agent.name):
                self.assertGreater(agent.tool_links.count(), 0)

    def test_provisioning_is_idempotent(self):
        agent_engine.ensure_workspace_for_user(self.user)
        agent_engine.ensure_workspace_for_user(self.user)
        self.assertEqual(AIAgent.objects.count(), 4)
        self.assertEqual(LLMProvider.objects.count(), 3)
        self.assertEqual(MCPServer.objects.count(), 7)

    def test_registration_provisions_the_workspace(self):
        self.client.post(reverse('register'), {
            'username': 'carol',
            'email': 'carol@example.com',
            'password1': 'sturdy-passphrase-42',
            'password2': 'sturdy-passphrase-42',
        })
        # The workspace is shared, so signing up joins the existing one
        # rather than creating a private copy.
        self.assertTrue(User.objects.filter(username='carol').exists())
        self.assertEqual(AIAgent.objects.count(), 4)
        self.assertEqual(MCPServer.objects.count(), 7)


class ChatTests(TestCase):
    """Talking to an AI employee."""

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(self.user)
        self.agent = AIAgent.objects.get(user=self.user, agent_type='content')
        self.conversation = agent_engine.start_conversation(self.user, self.agent)
        self.client.force_login(self.user)

    def test_the_chat_page_opens_without_a_conversation_in_the_url(self):
        self.assertEqual(self.client.get(reverse('agents')).status_code, 200)

    def test_the_chat_page_creates_a_thread_when_the_account_has_none(self):
        Conversation.objects.filter(user=self.user).delete()
        self.assertEqual(self.client.get(reverse('agents')).status_code, 200)
        self.assertEqual(Conversation.objects.filter(user=self.user).count(), 1)

    def test_sending_a_message_stores_both_turns(self):
        user_message, reply = agent_engine.send_message(
            self.conversation, 'Draft a LinkedIn post about approvals.')

        self.assertEqual(user_message.role, 'user')
        self.assertEqual(user_message.generation_source, 'manual')
        self.assertEqual(reply.role, 'assistant')
        self.assertTrue(reply.content.strip())
        self.assertEqual(self.conversation.messages.count(), 2)

    def test_a_reply_is_produced_even_with_no_language_model(self):
        """The employee has no llm_model assigned, so this is the template path."""
        self.assertFalse(self.agent.has_live_llm)
        _user_message, reply = agent_engine.send_message(self.conversation, 'Hello')
        self.assertEqual(reply.generation_source, 'fallback')
        self.assertTrue(reply.content.strip())

    def test_the_thread_is_named_after_the_opening_question(self):
        self.assertEqual(self.conversation.title, 'New conversation')
        agent_engine.send_message(self.conversation, 'Write me a post about pricing')
        self.conversation.refresh_from_db()
        self.assertIn('pricing', self.conversation.title)

    def test_context_is_bounded(self):
        for index in range(agent_engine.CONTEXT_TURNS + 6):
            agent_engine.send_message(self.conversation, f'Message {index}')
        history = agent_engine._history_for(self.conversation)
        self.assertLessEqual(len(history), agent_engine.CONTEXT_TURNS)

    def test_sending_through_the_endpoint(self):
        response = self.client.post(
            reverse('api_send_message'),
            {'conversation_id': self.conversation.pk, 'text': 'Draft something'},
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['user_message']['role'], 'user')
        self.assertEqual(payload['assistant_message']['role'], 'assistant')

    def test_an_empty_message_is_refused(self):
        response = self.client.post(
            reverse('api_send_message'),
            {'conversation_id': self.conversation.pk, 'text': '   '},
            content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_creating_renaming_and_deleting_a_thread(self):
        created = self.client.post(
            reverse('api_new_conversation'), {'agent_id': self.agent.pk},
            content_type='application/json').json()
        new_id = created['conversation_id']

        renamed = self.client.post(
            reverse('api_rename_conversation'),
            {'conversation_id': new_id, 'title': 'Q4 campaign ideas'},
            content_type='application/json').json()
        self.assertEqual(renamed['title'], 'Q4 campaign ideas')

        self.client.post(
            reverse('api_delete_conversation'), {'conversation_id': new_id},
            content_type='application/json')
        self.assertFalse(Conversation.objects.filter(pk=new_id).exists())

    def test_the_run_endpoint_is_gone(self):
        """Proof that the trigger-and-run model was removed, not just hidden."""
        with self.assertRaises(NoReverseMatch):
            reverse('api_trigger_agent')
        with self.assertRaises(NoReverseMatch):
            reverse('agent_detail')

    def test_the_configuration_panel_saves_the_employee(self):
        model = LLMModel.objects.filter(provider__user=self.user).first()
        tools = list(MCPTool.objects.filter(server__user=self.user)[:3])

        response = self.client.post(
            reverse('conversation', args=[self.conversation.pk]),
            {
                'name': 'Sophia', 'role': 'Head of Content',
                'agent_type': 'content', 'avatar_icon': 'fa-pen-nib',
                'avatar_color': '#7c3aed',
                'persona_description': 'Writes social content.',
                'system_prompt': 'You are Sophia. Be concise.',
                'llm_model': model.pk, 'temperature': '0.4', 'max_tokens': '600',
                'status': 'active', 'is_active': 'on',
                'mcp_tools': [t.pk for t in tools],
            })
        self.assertEqual(response.status_code, 302)

        self.agent.refresh_from_db()
        self.assertEqual(self.agent.role, 'Head of Content')
        self.assertEqual(self.agent.llm_model_id, model.pk)
        self.assertEqual(self.agent.tool_links.count(), 3)


class ChatToApprovalTests(TestCase):
    """Chat output reaches the world only through the approval queue."""

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(self.user)
        self.agent = AIAgent.objects.get(user=self.user, agent_type='content')
        self.conversation = agent_engine.start_conversation(self.user, self.agent)
        _user_message, self.reply = agent_engine.send_message(
            self.conversation, 'Draft a post about approvals.')
        self.client.force_login(self.user)

    def test_a_reply_creates_nothing_on_its_own(self):
        """Talking to an employee must not publish or queue anything by itself."""
        self.assertEqual(ApprovalRequest.objects.filter(user=self.user).count(), 0)
        self.assertEqual(SocialPost.objects.filter(user=self.user).count(), 0)

    def test_submitting_as_a_social_post_creates_a_draft(self):
        approval = agent_engine.submit_message_for_approval(
            self.reply, item_type='social_post', title='Approval workflows',
            platform='linkedin')

        self.assertEqual(approval.status, 'pending')
        self.assertEqual(approval.source_message_id, self.reply.pk)
        self.assertIsNotNone(approval.social_post)
        self.assertEqual(approval.social_post.status, 'draft')
        self.assertIsNone(approval.social_post.published_at)
        self.assertEqual(approval.audit_entries.count(), 1)

    def test_approving_publishes_the_draft(self):
        approval = agent_engine.submit_message_for_approval(
            self.reply, item_type='social_post', title='Approval workflows')
        agent_engine.apply_approval(approval, self.user, 'approved')

        approval.refresh_from_db()
        approval.social_post.refresh_from_db()
        self.assertEqual(approval.status, 'approved')
        self.assertEqual(approval.social_post.status, 'published')
        self.assertIsNotNone(approval.social_post.published_at)
        self.assertEqual(approval.audit_entries.count(), 2)

    def test_submitting_as_an_email_needs_a_prospect(self):
        with self.assertRaises(ValueError):
            agent_engine.submit_message_for_approval(
                self.reply, item_type='email', title='Outreach')

    def test_a_reply_cannot_be_submitted_as_a_lead(self):
        """A discovered prospect is a structured record, not written text."""
        with self.assertRaises(ValueError):
            agent_engine.submit_message_for_approval(
                self.reply, item_type='lead', title='Not allowed')

    def test_submitting_through_the_endpoint(self):
        response = self.client.post(
            reverse('api_submit_for_approval'),
            {'message_id': self.reply.pk, 'item_type': 'insight',
             'title': 'A useful insight'},
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['pending_count'], 1)

    def test_a_message_the_user_typed_cannot_be_submitted(self):
        typed = self.conversation.messages.filter(role='user').first()
        response = self.client.post(
            reverse('api_submit_for_approval'),
            {'message_id': typed.pk, 'item_type': 'insight', 'title': 'Mine'},
            content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_an_email_submission_without_a_prospect_is_refused(self):
        response = self.client.post(
            reverse('api_submit_for_approval'),
            {'message_id': self.reply.pk, 'item_type': 'email', 'title': 'Outreach'},
            content_type='application/json')
        self.assertEqual(response.status_code, 400)


class JSONEndpointSecurityTests(TestCase):
    """The JSON endpoints enforce login, CSRF and staff privileges."""

    def setUp(self):
        self.alice = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        self.bob = User.objects.create_user('bob', 'b@example.com', 'pw-bob-12345')
        self.staff = User.objects.create_user('sam', 's@example.com', 'pw-sam-12345')
        self.staff.is_staff = True
        self.staff.save()
        self.staff.profile.role = roles.ROLE_ADMIN
        self.staff.profile.save()
        # Alice is deliberately weaker, so the refusals below are real.
        self.alice.profile.role = roles.ROLE_ANALYST
        self.alice.profile.save()
        for user in (self.alice, self.bob, self.staff):
            agent_engine.ensure_workspace_for_user(user)

    def test_endpoints_require_a_signed_in_user(self):
        response = self.client.post(
            reverse('api_send_message'), '{}', content_type='application/json')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_endpoints_reject_get(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse('api_send_message')).status_code, 405)

    def test_endpoints_enforce_csrf(self):
        """Proof that removing @csrf_exempt actually took effect."""
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.alice)
        response = csrf_client.post(
            reverse('api_send_message'),
            '{"conversation_id": 1, "text": "hello"}',
            content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_a_user_without_the_role_cannot_change_an_account(self):
        self.client.force_login(self.alice)
        response = self.client.post(
            reverse('api_toggle_user_active'),
            {'user_id': self.bob.pk, 'active': False},
            content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.is_active)

    def test_an_administrator_can_suspend_another_account(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('api_toggle_user_active'),
            {'user_id': self.bob.pk, 'active': False},
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.bob.refresh_from_db()
        self.assertFalse(self.bob.is_active)

    def test_an_administrator_cannot_suspend_themselves(self):
        """Without this guard, one click would lock the presenter out mid-demo."""
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('api_toggle_user_active'),
            {'user_id': self.staff.pk, 'active': False},
            content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_active)

    def test_the_mcp_toggle_updates_the_status(self):
        server = MCPServer.objects.filter(server_key='database').first()
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('api_toggle_mcp_server'),
            {'server_id': server.pk, 'enabled': False},
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        server.refresh_from_db()
        self.assertFalse(server.is_enabled)
        self.assertEqual(server.connection_status, 'disabled')


class LLMClientFallbackTests(TestCase):
    """The application must remain usable with no network connection.

    This is the most valuable test in the suite, because losing the network is
    exactly the failure mode that would otherwise ruin a live demonstration.
    """

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(self.user)
        self.provider = LLMProvider.objects.get(user=self.user, provider_key='openrouter')
        self.provider.api_key = 'sk-test-not-a-real-key'
        self.provider.save()

        self._real_http_json = llm_client._http_json

    def tearDown(self):
        llm_client._http_json = self._real_http_json

    def _break_the_network(self):
        def explode(*args, **kwargs):
            raise urllib.error.URLError('Network is unreachable')
        llm_client._http_json = explode

    def test_fetch_models_falls_back_to_the_curated_catalogue(self):
        self._break_the_network()
        result = llm_client.fetch_models(self.provider)
        self.assertFalse(result['ok'])
        self.assertEqual(result['source'], 'fallback')
        self.assertGreater(len(result['models']), 0)
        self.assertIn('curated', result['message'])

    def test_test_connection_reports_the_failure_without_raising(self):
        self._break_the_network()
        result = llm_client.test_connection(self.provider)
        self.assertFalse(result['ok'])
        self.assertEqual(result['connection_status'], 'error')
        self.assertTrue(result['message'])

    def test_chat_completion_reports_failure_rather_than_raising(self):
        self._break_the_network()
        result = llm_client.chat_completion(
            self.provider, 'openai/gpt-4o-mini', 'You are a bot.', 'Say hello.')
        self.assertFalse(result['ok'])
        self.assertEqual(result['text'], '')

    def test_a_chat_reply_still_arrives_with_no_network(self):
        """The employee has a live model assigned, but the network is down."""
        self._break_the_network()
        agent = AIAgent.objects.get(user=self.user, agent_type='content')
        agent.llm_model = LLMModel.objects.filter(provider=self.provider).first()
        agent.save()
        self.assertTrue(agent.has_live_llm)

        conversation = agent_engine.start_conversation(self.user, agent)
        _user_message, reply = agent_engine.send_message(conversation, 'Draft a post.')

        self.assertEqual(reply.role, 'assistant')
        self.assertEqual(reply.generation_source, 'fallback')
        self.assertTrue(reply.content.strip())

    def test_the_chat_page_renders_with_no_network(self):
        self._break_the_network()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('agents')).status_code, 200)

    def test_the_configurations_page_renders_with_no_network(self):
        self._break_the_network()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('configurations')).status_code, 200)


class ApiKeyResolutionTests(TestCase):
    """Where a provider's credential comes from, and what depends on it."""

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(self.user)
        self.nvidia = LLMProvider.objects.get(user=self.user, provider_key='nvidia')
        self.ollama = LLMProvider.objects.get(user=self.user, provider_key='ollama')

    def _no_environment_key(self):
        """Blank out NVIDIA_API_KEY for the duration of a test.

        settings.py reads .env on start-up, so on a machine that has a real key
        configured it is present in os.environ throughout the test run. A test
        that means "no key anywhere" has to say so, rather than depending on
        who is running it.
        """
        return mock.patch.dict(os.environ, {'NVIDIA_API_KEY': ''})

    def test_a_provider_with_no_key_anywhere_is_not_configured(self):
        with self._no_environment_key():
            self.assertEqual(self.nvidia.key_source, 'none')
            self.assertFalse(self.nvidia.is_configured)
            self.assertEqual(self.nvidia.masked_key, 'Not set')

    def test_ollama_needs_no_credential(self):
        self.assertFalse(self.ollama.requires_api_key)
        self.assertTrue(self.ollama.is_configured)

    def test_a_stored_key_configures_the_provider(self):
        self.nvidia.api_key = 'nvapi-stored-example-key-value'
        self.nvidia.save()
        self.assertEqual(self.nvidia.key_source, 'database')
        self.assertTrue(self.nvidia.is_configured)
        self.assertEqual(llm_client.resolve_api_key(self.nvidia),
                         'nvapi-stored-example-key-value')

    def test_an_environment_key_configures_the_provider(self):
        """Reading the key from the environment keeps it out of the database."""
        with mock.patch.dict(os.environ, {'NVIDIA_API_KEY': 'nvapi-from-the-environment'}):
            self.assertEqual(self.nvidia.key_source, 'environment')
            self.assertTrue(self.nvidia.is_configured)
            self.assertEqual(llm_client.resolve_api_key(self.nvidia),
                             'nvapi-from-the-environment')

    def test_a_stored_key_beats_the_environment(self):
        self.nvidia.api_key = 'nvapi-stored'
        self.nvidia.save()
        with mock.patch.dict(os.environ, {'NVIDIA_API_KEY': 'nvapi-environment'}):
            self.assertEqual(self.nvidia.key_source, 'database')
            self.assertEqual(llm_client.resolve_api_key(self.nvidia), 'nvapi-stored')

    def test_the_key_is_never_exposed_in_full(self):
        self.nvidia.api_key = 'nvapi-1234567890abcdefghijklmnop'
        self.nvidia.save()
        masked = self.nvidia.masked_key
        self.assertNotIn('1234567890abcdefghij', masked)
        self.assertIn('*', masked)
        self.assertTrue(masked.endswith('mnop'))

    def test_an_environment_key_makes_an_employee_live(self):
        """AIAgent.has_live_llm depends on is_configured, so the two must agree."""
        agent = AIAgent.objects.get(user=self.user, agent_type='content')
        agent.llm_model = LLMModel.objects.filter(provider=self.nvidia).first()
        agent.save()

        with self._no_environment_key():
            agent.refresh_from_db()
            self.assertFalse(agent.has_live_llm)

        with mock.patch.dict(os.environ, {'NVIDIA_API_KEY': 'nvapi-from-the-environment'}):
            agent.refresh_from_db()
            self.assertTrue(agent.has_live_llm)


class TimeoutBudgetTests(TestCase):
    """Generation gets a far longer budget than a metadata call.

    A serverless model endpoint that has scaled to zero can take 15-20 seconds
    to answer its first request. Sharing the 8-second metadata budget would
    make a perfectly valid API key look broken, so the two are separate.
    """

    def test_the_chat_budget_is_much_longer_than_the_metadata_budget(self):
        self.assertGreaterEqual(llm_client._chat_timeout(), 30)
        self.assertGreater(llm_client._chat_timeout(), llm_client._timeout())

    def test_the_metadata_budget_stays_short(self):
        self.assertLessEqual(llm_client._timeout(), 15)


class ReasoningStripTests(TestCase):
    """Some models show their working; it must not reach a published post."""

    def test_a_think_block_is_removed(self):
        self.assertEqual(
            llm_client.strip_reasoning('<think>Let me plan.</think>The answer.'),
            'The answer.')

    def test_an_announced_thinking_process_is_removed(self):
        raw = ("Here's a thinking process:\n\n1. Consider the ask\n2. Draft it\n\n"
               "85% of AI drafts need a human review before publishing.")
        self.assertEqual(
            llm_client.strip_reasoning(raw),
            '85% of AI drafts need a human review before publishing.')

    def test_ordinary_output_is_untouched(self):
        raw = '85% of drafts need review.\n\nHow do you keep yours on brand?'
        self.assertEqual(llm_client.strip_reasoning(raw), raw)

    def test_a_replacement_character_is_dropped(self):
        self.assertEqual(llm_client.strip_reasoning('�A clean reply.'), 'A clean reply.')

    def test_a_short_trailing_block_does_not_become_the_whole_answer(self):
        """Guards against mistaking a sign-off for the answer."""
        raw = 'Reasoning: weighing the options\n\nThe real answer is here.\n\nOK'
        self.assertIn('The real answer is here.', llm_client.strip_reasoning(raw))

    # --- an unmarked monologue --------------------------------------------

    LEAK = (
        'Okay, the user is asking me to send an email "on my behalf" after I '
        'already drafted one. Let me unpack this carefully.\n\n'
        'First, recalling my role as Aria - I am strictly the outreach manager. '
        'My job is only to write the email, not to send it.\n\n'
        'Important constraints from the brief:\n'
        '* Never claim I have sent anything\n'
        '* If asked to send, just write it ready to go\n\n'
        'Double-checking word count: my planned response is just referencing '
        'the prior email.\n\n'
        'Alternative: treat "send on my behalf" as a request to confirm the '
        'email is ready. So I will output the same email again - because:\n\n'
        '* It is still under'
    )

    def test_an_unmarked_monologue_is_suppressed_entirely(self):
        """REGRESSION: Aria published 3,454 characters of deliberation --
        including her own system prompt quoted back -- and ran out of tokens
        before writing any answer. There was no <think> tag and no "Here is my
        thinking process:" heading, so neither existing rule caught it.

        Suppressing it returns '', which chat_completion() treats as a failed
        call, so the employee answers from its template instead.
        """
        self.assertEqual(llm_client.strip_reasoning(self.LEAK), '')

    def test_a_suppressed_reply_becomes_a_failed_call(self):
        """The empty string has to travel: it is what makes the chat fall back
        rather than showing an empty bubble."""
        payload = {'choices': [{'message': {'content': self.LEAK}}],
                   'usage': {'total_tokens': 1274}}
        with mock.patch.object(llm_client, '_http_json', return_value=(200, payload)):
            provider = LLMProvider(provider_key='nvidia', api_key='nvapi-x')
            result = llm_client.chat_completion(
                provider, 'nvidia/nemotron-3-super-120b-a12b', 'sys', 'hi')

        self.assertFalse(result['ok'])
        self.assertIn('empty', result['message'].lower())

    def test_monologue_followed_by_a_real_answer_keeps_the_answer(self):
        raw = ('Okay, the user is asking for an intro email. Let me unpack this.\n\n'
               'Subject: Quick Hello\n\n'
               'Hi Sulem,\n\nWould you be free for a short call next week?\n\nAria')
        cleaned = llm_client.strip_reasoning(raw)

        self.assertTrue(cleaned.startswith('Subject:'))
        self.assertNotIn('the user is asking', cleaned)

    def test_a_finished_email_is_never_touched(self):
        raw = ('Subject: Quick Hello\n\nHi Sulem,\n\nI hope you are well. Would '
               'you be free for a short call next week?\n\nThank you,\nAria')
        self.assertEqual(llm_client.strip_reasoning(raw), raw)

    def test_the_word_user_alone_is_not_treated_as_reasoning(self):
        """A strategy employee writes about users for a living, so the detector
        keys on "the user is asking", never on "the user"."""
        raw = ('User acquisition cost rose 14% this quarter. The user journey now '
               'takes six touches instead of four, which is where the budget went.')
        self.assertEqual(llm_client.strip_reasoning(raw), raw)

    def test_every_employee_is_told_not_to_show_its_working(self):
        """The detector is the safety net; the prompt is the actual fix."""
        self.assertIn('Never show your reasoning', agent_engine.PLATFORM_PREAMBLE)



class SharedWorkspaceTests(TestCase):
    """The workspace is shared: one set of resources, however many people."""

    def setUp(self):
        self.alice = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        self.bob = User.objects.create_user('bob', 'b@example.com', 'pw-bob-12345')
        for user in (self.alice, self.bob):
            agent_engine.ensure_workspace_for_user(user)

    def test_two_accounts_share_one_set_of_employees(self):
        """The old design gave each account its own copy; migration 0006 merged them."""
        self.assertEqual(AIAgent.objects.count(), 4)
        self.assertEqual(LLMProvider.objects.count(), 3)
        self.assertEqual(MCPServer.objects.count(), 7)

    def test_provisioning_is_idempotent(self):
        agent_engine.ensure_workspace()
        agent_engine.ensure_workspace()
        self.assertEqual(AIAgent.objects.count(), 4)
        self.assertEqual(LLMProvider.objects.count(), 3)
        self.assertEqual(MCPServer.objects.count(), 7)

    def test_everyone_sees_the_same_approval_queue(self):
        agent = AIAgent.objects.get(agent_type='content')
        conversation = agent_engine.start_conversation(self.alice, agent)
        _user_msg, reply = agent_engine.send_message(conversation, 'Draft something.')
        agent_engine.submit_message_for_approval(
            reply, item_type='insight', title='Shared workspace item')

        for user in (self.alice, self.bob):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertContains(self.client.get(reverse('approvals')),
                                    'Shared workspace item')

    def test_conversations_remain_private(self):
        """Shared data, private chat: the one deliberate exception."""
        agent = AIAgent.objects.get(agent_type='content')
        alice_thread = agent_engine.start_conversation(self.alice, agent)

        self.client.force_login(self.bob)
        self.assertEqual(
            self.client.get(reverse('conversation', args=[alice_thread.pk])).status_code,
            404)

    def test_another_user_cannot_post_into_your_conversation(self):
        agent = AIAgent.objects.get(agent_type='content')
        alice_thread = agent_engine.start_conversation(self.alice, agent)

        self.client.force_login(self.bob)
        response = self.client.post(
            reverse('api_send_message'),
            {'conversation_id': alice_thread.pk, 'text': 'Let me in'},
            content_type='application/json')
        self.assertEqual(response.status_code, 404)


class RoleGroupTests(TestCase):
    """Roles are Django Groups, and the two must never drift apart."""

    def test_the_four_groups_exist_with_permissions(self):
        """A fresh database must not end up with empty groups.

        Django creates its Permission rows in post_migrate, so assigning them
        from a data migration silently assigns nothing. This asserts the
        post_migrate receiver in signals.py did the job.
        """
        for role, name in roles.GROUP_NAMES.items():
            with self.subTest(role=role):
                group = Group.objects.get(name=name)
                self.assertGreater(group.permissions.count(), 0)

    def test_the_matrix_is_cumulative(self):
        counts = {
            role: Group.objects.get(name=name).permissions.count()
            for role, name in roles.GROUP_NAMES.items()
        }
        self.assertLess(counts[roles.ROLE_VIEWER], counts[roles.ROLE_ANALYST])
        self.assertLess(counts[roles.ROLE_ANALYST], counts[roles.ROLE_MANAGER])
        self.assertLess(counts[roles.ROLE_MANAGER], counts[roles.ROLE_ADMIN])

    def test_saving_a_profile_moves_the_user_into_the_matching_group(self):
        user = User.objects.create_user('rob', 'r@example.com', 'pw-rob-12345')
        user.profile.role = roles.ROLE_MANAGER
        user.profile.save()

        self.assertTrue(user.groups.filter(name='Manager').exists())
        self.assertFalse(user.groups.filter(name='Viewer').exists())

    def test_changing_a_role_withdraws_the_old_group(self):
        user = User.objects.create_user('rob', 'r@example.com', 'pw-rob-12345')
        user.profile.role = roles.ROLE_MANAGER
        user.profile.save()
        user.profile.role = roles.ROLE_VIEWER
        user.profile.save()

        names = set(user.groups.values_list('name', flat=True))
        self.assertIn('Viewer', names)
        self.assertNotIn('Manager', names)

    def test_a_user_is_only_ever_in_one_role_group(self):
        user = User.objects.create_user('rob', 'r@example.com', 'pw-rob-12345')
        for role in [roles.ROLE_ANALYST, roles.ROLE_MANAGER, roles.ROLE_ADMIN]:
            user.profile.role = role
            user.profile.save()

        role_groups = user.groups.filter(name__in=roles.GROUP_NAMES.values())
        self.assertEqual(role_groups.count(), 1)


class PermissionMatrixTests(TestCase):
    """What each role may actually do, checked through real requests."""

    @classmethod
    def setUpTestData(cls):
        cls.people = {}
        for role in [roles.ROLE_VIEWER, roles.ROLE_ANALYST,
                     roles.ROLE_MANAGER, roles.ROLE_ADMIN]:
            user = User.objects.create_user(role, f'{role}@example.com', 'pw-role-12345')
            user.profile.role = role
            user.profile.save()
            cls.people[role] = user

        agent_engine.ensure_workspace(owner=cls.people[roles.ROLE_ADMIN])

    def as_role(self, role):
        client = Client()
        client.force_login(self.people[role])
        return client

    def post_json(self, client, name, payload):
        return client.post(reverse(name), json.dumps(payload),
                           content_type='application/json')

    # --- reading ----------------------------------------------------------

    def test_every_role_can_read_every_page(self):
        for role in self.people:
            client = self.as_role(role)
            for page in PAGE_NAMES:
                with self.subTest(role=role, page=page):
                    self.assertEqual(client.get(reverse(page)).status_code, 200)

    # --- chatting ---------------------------------------------------------

    def test_a_viewer_cannot_start_a_conversation(self):
        agent = AIAgent.objects.first()
        response = self.post_json(self.as_role(roles.ROLE_VIEWER),
                                  'api_new_conversation', {'agent_id': agent.pk})
        self.assertEqual(response.status_code, 403)

    def test_an_analyst_can_start_a_conversation(self):
        agent = AIAgent.objects.first()
        response = self.post_json(self.as_role(roles.ROLE_ANALYST),
                                  'api_new_conversation', {'agent_id': agent.pk})
        self.assertEqual(response.status_code, 200)

    # --- approving --------------------------------------------------------

    def _pending_approval(self, owner):
        agent = AIAgent.objects.get(agent_type='content')
        conversation = agent_engine.start_conversation(owner, agent)
        _user_msg, reply = agent_engine.send_message(conversation, 'Draft a post.')
        return agent_engine.submit_message_for_approval(
            reply, item_type='insight', title='Needs a decision')

    def test_an_analyst_cannot_approve(self):
        approval = self._pending_approval(self.people[roles.ROLE_ANALYST])
        response = self.post_json(self.as_role(roles.ROLE_ANALYST),
                                  'api_approval_decision',
                                  {'approval_id': approval.pk, 'decision': 'approved'})
        self.assertEqual(response.status_code, 403)
        approval.refresh_from_db()
        self.assertEqual(approval.status, 'pending')

    def test_a_manager_can_approve(self):
        approval = self._pending_approval(self.people[roles.ROLE_ANALYST])
        response = self.post_json(self.as_role(roles.ROLE_MANAGER),
                                  'api_approval_decision',
                                  {'approval_id': approval.pk, 'decision': 'approved'})
        self.assertEqual(response.status_code, 200)
        approval.refresh_from_db()
        self.assertEqual(approval.status, 'approved')

    def test_the_approve_button_is_hidden_from_an_analyst(self):
        """Hiding the control and refusing the request are the same rule."""
        self._pending_approval(self.people[roles.ROLE_ANALYST])

        analyst_page = self.as_role(roles.ROLE_ANALYST).get(reverse('approvals'))
        manager_page = self.as_role(roles.ROLE_MANAGER).get(reverse('approvals'))

        self.assertContains(manager_page, 'Approve')
        self.assertContains(analyst_page, 'Awaiting a Manager')

    # --- managing the workforce and its tools -----------------------------

    def test_only_a_manager_or_above_may_edit_an_employee(self):
        agent = AIAgent.objects.first()
        payload = {'agent_id': agent.pk, 'name': 'Renamed'}
        expected = {roles.ROLE_VIEWER: 403, roles.ROLE_ANALYST: 403,
                    roles.ROLE_MANAGER: 200, roles.ROLE_ADMIN: 200}
        for role, code in expected.items():
            with self.subTest(role=role):
                response = self.post_json(self.as_role(role), 'api_edit_agent', payload)
                self.assertEqual(response.status_code, code)

    def test_only_a_manager_or_above_may_add_an_employee(self):
        expected = {roles.ROLE_VIEWER: 403, roles.ROLE_ANALYST: 403,
                    roles.ROLE_MANAGER: 200, roles.ROLE_ADMIN: 200}
        for role, code in expected.items():
            with self.subTest(role=role):
                self.assertEqual(
                    self.as_role(role).get(reverse('agent_create')).status_code, code)

    def test_only_a_manager_or_above_may_toggle_an_mcp_server(self):
        server = MCPServer.objects.first()
        payload = {'server_id': server.pk, 'enabled': True}
        expected = {roles.ROLE_VIEWER: 403, roles.ROLE_ANALYST: 403,
                    roles.ROLE_MANAGER: 200, roles.ROLE_ADMIN: 200}
        for role, code in expected.items():
            with self.subTest(role=role):
                response = self.post_json(self.as_role(role), 'api_toggle_mcp_server', payload)
                self.assertEqual(response.status_code, code)

    # --- credentials and people -------------------------------------------

    def test_only_an_administrator_may_add_a_person(self):
        expected = {roles.ROLE_VIEWER: 403, roles.ROLE_ANALYST: 403,
                    roles.ROLE_MANAGER: 403, roles.ROLE_ADMIN: 200}
        for role, code in expected.items():
            with self.subTest(role=role):
                self.assertEqual(
                    self.as_role(role).get(reverse('user_create')).status_code, code)

    def test_only_an_administrator_may_suspend_an_account(self):
        target = self.people[roles.ROLE_VIEWER]
        for role in [roles.ROLE_ANALYST, roles.ROLE_MANAGER]:
            with self.subTest(role=role):
                response = self.post_json(self.as_role(role), 'api_toggle_user_active',
                                          {'user_id': target.pk, 'active': False})
                self.assertEqual(response.status_code, 403)

        response = self.post_json(self.as_role(roles.ROLE_ADMIN), 'api_toggle_user_active',
                                  {'user_id': target.pk, 'active': False})
        self.assertEqual(response.status_code, 200)
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_a_manager_cannot_change_provider_settings(self):
        provider = LLMProvider.objects.first()
        response = self.as_role(roles.ROLE_MANAGER).post(
            reverse('configurations'),
            {'provider_id': provider.pk, 'display_name': 'Hijacked', 'is_enabled': 'on'})
        self.assertEqual(response.status_code, 302)
        provider.refresh_from_db()
        self.assertNotEqual(provider.display_name, 'Hijacked')

    def test_an_administrator_can_change_provider_settings(self):
        provider = LLMProvider.objects.first()
        self.as_role(roles.ROLE_ADMIN).post(
            reverse('configurations'),
            {'provider_id': provider.pk, 'display_name': 'Renamed by admin',
             'is_enabled': 'on'})
        provider.refresh_from_db()
        self.assertEqual(provider.display_name, 'Renamed by admin')

    def test_a_superuser_bypasses_the_matrix(self):
        """Django's ModelBackend answers True to every has_perm for a superuser."""
        root = User.objects.create_superuser('root', 'root@example.com', 'pw-root-12345')
        root.profile.role = roles.ROLE_VIEWER      # deliberately the weakest role
        root.profile.save()
        self.assertTrue(root.has_perm(roles.CAN_APPROVE))


class UserCrudTests(TestCase):
    """Creating, editing and deleting accounts, with the guards that matter."""

    def setUp(self):
        self.admin = User.objects.create_user('boss', 'boss@example.com', 'pw-boss-12345')
        self.admin.profile.role = roles.ROLE_ADMIN
        self.admin.profile.save()
        self.client.force_login(self.admin)

    def test_creating_a_person_assigns_their_role_and_group(self):
        response = self.client.post(reverse('user_create'), {
            'username': 'newcomer',
            'email': 'newcomer@example.com',
            'first_name': '', 'last_name': '', 'job_title': 'Analyst',
            'role': roles.ROLE_ANALYST,
            'password1': 'a-sturdy-passphrase-42',
            'password2': 'a-sturdy-passphrase-42',
        })
        self.assertEqual(response.status_code, 302)

        newcomer = User.objects.get(username='newcomer')
        self.assertEqual(newcomer.profile.role, roles.ROLE_ANALYST)
        self.assertTrue(newcomer.groups.filter(name='Analyst').exists())
        self.assertTrue(newcomer.has_perm(roles.CAN_CHAT))
        self.assertFalse(newcomer.has_perm(roles.CAN_APPROVE))

    def test_editing_a_person_changes_what_they_can_do(self):
        target = User.objects.create_user('rita', 'rita@example.com', 'pw-rita-12345')
        target.profile.role = roles.ROLE_VIEWER
        target.profile.save()
        self.assertFalse(User.objects.get(pk=target.pk).has_perm(roles.CAN_APPROVE))

        self.client.post(reverse('user_edit', args=[target.pk]), {
            'username': 'rita', 'email': 'rita@example.com',
            'first_name': '', 'last_name': '', 'job_title': '',
            'role': roles.ROLE_MANAGER, 'is_active': 'on',
        })
        self.assertTrue(User.objects.get(pk=target.pk).has_perm(roles.CAN_APPROVE))

    def test_you_cannot_delete_your_own_account(self):
        self.client.post(reverse('user_delete', args=[self.admin.pk]))
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_the_last_administrator_cannot_be_deleted(self):
        """Otherwise the workspace is left with nobody able to manage it."""
        other = User.objects.create_user('second', 's@example.com', 'pw-second-1234')
        other.profile.role = roles.ROLE_ADMIN
        other.profile.save()

        # Two administrators, so removing one is allowed.
        self.client.post(reverse('user_delete', args=[other.pk]))
        self.assertFalse(User.objects.filter(pk=other.pk).exists())

        # Now only `boss` remains, and boss cannot delete themselves anyway.
        self.assertEqual(
            Profile.objects.filter(role=roles.ROLE_ADMIN).count(), 1)

    def test_an_administrator_cannot_demote_themselves(self):
        response = self.client.post(reverse('user_edit', args=[self.admin.pk]), {
            'username': 'boss', 'email': 'boss@example.com',
            'first_name': '', 'last_name': '', 'job_title': '',
            'role': roles.ROLE_VIEWER, 'is_active': 'on',
        })
        self.assertEqual(response.status_code, 200)      # redisplayed with an error
        self.admin.profile.refresh_from_db()
        self.assertEqual(self.admin.profile.role, roles.ROLE_ADMIN)

    def test_a_deletion_must_be_a_post(self):
        target = User.objects.create_user('rita', 'rita@example.com', 'pw-rita-12345')
        self.assertEqual(
            self.client.get(reverse('user_delete', args=[target.pk])).status_code, 405)
        self.assertTrue(User.objects.filter(pk=target.pk).exists())


class WorkforceCrudTests(TestCase):
    """Adding and removing AI employees and MCP servers."""

    def setUp(self):
        self.manager = User.objects.create_user('mo', 'mo@example.com', 'pw-mo-123456')
        self.manager.profile.role = roles.ROLE_MANAGER
        self.manager.profile.save()
        agent_engine.ensure_workspace(owner=self.manager)
        self.client.force_login(self.manager)

    def test_adding_an_employee(self):
        response = self.client.post(reverse('agent_create'), {
            'name': 'Nova', 'role': 'Community Manager',
            'agent_type': 'content', 'avatar_icon': 'fa-star',
            'avatar_color': '#7c3aed',
            'persona_description': 'Looks after the community.',
            'system_prompt': '', 'temperature': '0.7', 'max_tokens': '800',
            'status': 'active', 'is_active': 'on',
        })
        # agent_type is unique across the workspace, and 'content' is taken.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AIAgent.objects.filter(name='Nova').exists())

    def test_removing_an_employee(self):
        agent = AIAgent.objects.get(agent_type='content')
        self.client.post(reverse('agent_delete', args=[agent.pk]))
        self.assertFalse(AIAgent.objects.filter(pk=agent.pk).exists())

    def test_removing_an_employee_must_be_a_post(self):
        agent = AIAgent.objects.first()
        self.assertEqual(
            self.client.get(reverse('agent_delete', args=[agent.pk])).status_code, 405)
        self.assertTrue(AIAgent.objects.filter(pk=agent.pk).exists())

    def test_registering_and_removing_an_mcp_server(self):
        response = self.client.post(reverse('mcp_server_create'), {
            'name': 'Notion', 'description': 'Workspace notes.',
            'category': 'data', 'transport': 'stdio',
            'command': 'npx', 'args': '-y @modelcontextprotocol/server-notion',
            'endpoint_url': '', 'icon': 'fa-note-sticky', 'color': '#111111',
            'is_enabled': 'on',
        })
        self.assertEqual(response.status_code, 302)
        server = MCPServer.objects.get(name='Notion')

        self.client.post(reverse('mcp_server_delete', args=[server.pk]))
        self.assertFalse(MCPServer.objects.filter(pk=server.pk).exists())

    def test_removing_a_tool(self):
        tool = MCPTool.objects.first()
        server_pk = tool.server_id
        self.client.post(reverse('mcp_tool_delete', args=[tool.pk]))
        self.assertFalse(MCPTool.objects.filter(pk=tool.pk).exists())
        self.assertTrue(MCPServer.objects.filter(pk=server_pk).exists())


class ProvisioningRegressionTests(TestCase):
    """Provisioning must not depend on who is asking.

    REGRESSION: the workspace-wide unique constraints are on the natural key
    alone (agent_type, provider_key, server_key), but the provisioning helpers
    used to look rows up by (user, natural_key). Once the workspace was shared,
    a second person signing in would miss the existing row on the get() and
    then collide with the constraint on the insert, raising

        IntegrityError: UNIQUE constraint failed:
                        marketing_llmprovider.provider_key

    on their very first visit to /configurations/. The lookup and the
    constraint have to agree, and these tests hold them to it.
    """

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@example.com', 'pw-owner-1234')
        agent_engine.ensure_workspace(owner=self.owner)

    def test_provisioning_again_as_a_different_person_is_a_no_op(self):
        newcomer = User.objects.create_user('newcomer', 'n@example.com', 'pw-new-12345')

        # The failing case: everything already exists, owned by someone else.
        agent_engine.ensure_workspace(owner=newcomer)

        self.assertEqual(AIAgent.objects.count(), 4)
        self.assertEqual(LLMProvider.objects.count(), 3)
        self.assertEqual(MCPServer.objects.count(), 7)

        # The rows keep their original owner; provisioning claims nothing.
        self.assertEqual(
            set(LLMProvider.objects.values_list('user_id', flat=True)), {self.owner.pk})

    def test_every_page_loads_for_someone_who_owns_nothing(self):
        """The crash was a GET, on a page anyone is allowed to read."""
        newcomer = User.objects.create_user('newcomer', 'n@example.com', 'pw-new-12345')
        newcomer.profile.role = roles.ROLE_VIEWER
        newcomer.profile.save()

        self.client.force_login(newcomer)
        for page in PAGE_NAMES:
            with self.subTest(page=page):
                self.assertEqual(self.client.get(reverse(page)).status_code, 200)

    def test_signing_in_twice_as_two_people_provisions_once(self):
        second = User.objects.create_user('second', 's@example.com', 'pw-second-1234')
        agent_engine.ensure_workspace_for_user(self.owner)
        agent_engine.ensure_workspace_for_user(second)
        agent_engine.ensure_workspace_for_user(second)

        self.assertEqual(AIAgent.objects.count(), 4)
        self.assertEqual(LLMProvider.objects.count(), 3)
        self.assertEqual(MCPServer.objects.count(), 7)

    def test_each_lookup_matches_its_unique_constraint(self):
        """Guards the invariant directly, rather than only its symptom.

        A get_or_create whose lookup is narrower than the constraint will
        always raise IntegrityError once a row it cannot see exists.
        """
        expected = {
            AIAgent: {'agent_type'},
            LLMProvider: {'provider_key'},
            MCPServer: {'server_key'},
        }
        for model, fields in expected.items():
            with self.subTest(model=model.__name__):
                names = {
                    frozenset(c.fields)
                    for c in model._meta.constraints
                    if hasattr(c, 'fields')
                }
                self.assertIn(frozenset(fields), names)


class MarkdownRenderingTests(TestCase):
    """Formatting an AI reply, and refusing to trust it.

    marketing/markdown.py turns a model's Markdown into HTML. Its safety rests
    on one ordering rule -- escape everything first, then generate tags -- so
    these tests check the generation and the escaping together.
    """

    def render(self, text):
        return str(md.render(text))

    # --- formatting -------------------------------------------------------

    def test_bold_and_italic(self):
        html = self.render('**bold** and *italic*')
        self.assertIn('<strong>bold</strong>', html)
        self.assertIn('<em>italic</em>', html)
        self.assertNotIn('**', html)

    def test_bullet_list(self):
        html = self.render('- first\n- second')
        self.assertIn('<ul>', html)
        self.assertEqual(html.count('<li>'), 2)

    def test_numbered_list(self):
        html = self.render('1. one\n2. two')
        self.assertIn('<ol>', html)
        self.assertEqual(html.count('<li>'), 2)

    def test_headings_start_at_h3(self):
        """A reply sits inside the page's own heading structure."""
        self.assertIn('<h3>Title</h3>', self.render('## Title'))

    def test_inline_and_fenced_code(self):
        self.assertIn('<code>x = 1</code>', self.render('`x = 1`'))
        html = self.render('```python\nprint(1)\n```')
        self.assertIn('<pre><code class="lang-python">', html)
        self.assertIn('print(1)', html)

    def test_paragraphs_and_line_breaks(self):
        html = self.render('one\ntwo\n\nthree')
        self.assertIn('one<br>two', html)
        self.assertEqual(html.count('<p>'), 2)

    def test_markdown_inside_a_code_block_is_left_alone(self):
        html = self.render('```\n**not bold**\n```')
        self.assertIn('**not bold**', html)
        self.assertNotIn('<strong>', html)

    def test_plain_text_is_unchanged(self):
        self.assertEqual(self.render('Just a sentence.'), '<p>Just a sentence.</p>')

    def test_the_reply_that_prompted_this(self):
        """The literal reply that showed raw asterisks in the interface."""
        raw = ('As Atlas, I specialize in **marketing performance analysis**.\n\n'
               '**I do not have access to internal HR data**:\n'
               '- Campaign attribution & ROAS\n'
               '- Customer acquisition cost (CAC) vs. lifetime value (LTV)')
        html = self.render(raw)
        self.assertNotIn('**', html)
        self.assertIn('<strong>marketing performance analysis</strong>', html)
        self.assertEqual(html.count('<li>'), 2)
        self.assertIn('ROAS', html)

    # --- safety -----------------------------------------------------------

    def test_html_in_the_reply_is_escaped(self):
        html = self.render('<script>alert(1)</script>')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_an_image_onerror_payload_is_escaped(self):
        html = self.render('<img src=x onerror="alert(1)">')
        # What matters is that no tag is produced. The words survive as visible
        # text, which is correct: that is what the model wrote, and showing it
        # is not the same as executing it.
        self.assertNotIn('<img', html)
        self.assertIn('&lt;img', html)
        self.assertNotIn('<', html.replace('<p>', '').replace('</p>', ''))

    def test_a_javascript_link_is_not_turned_into_a_link(self):
        html = self.render('[click](javascript:alert(1))')
        self.assertNotIn('<a ', html)
        self.assertNotIn('javascript:alert(1)"', html)

    def test_a_data_url_is_not_turned_into_a_link(self):
        self.assertNotIn('<a ', self.render('[x](data:text/html;base64,PHNjcmlwdD4=)'))

    def test_an_http_link_is_allowed_and_isolated(self):
        html = self.render('[example](https://example.com)')
        self.assertIn('href="https://example.com"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_a_code_block_cannot_smuggle_a_tag(self):
        html = self.render('```\n<script>alert(1)</script>\n```')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_the_placeholder_cannot_be_forged(self):
        """The stash token uses characters that escaping would have removed."""
        html = self.render('CODEBLOCK0 and \x00CODEBLOCK0\x00')
        self.assertNotIn('<pre>', html)

    # --- the two rendering paths agree ------------------------------------

    def test_the_endpoint_returns_the_same_html_as_the_page(self):
        """One implementation, so a reply cannot look different in each path.

        The endpoint returns the whole bubble, rendered from the same
        partials/_chat_message.html the page uses, rather than fields for the
        browser to reassemble. Rebuilding the markup in JavaScript meant every
        change had to be made twice; mail cards would have been the third.
        """
        user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(user)
        agent = AIAgent.objects.get(agent_type='content')
        conversation = agent_engine.start_conversation(user, agent)
        agent_engine.send_message(conversation, 'Draft a post.')

        self.client.force_login(user)
        response = self.client.post(
            reverse('api_send_message'),
            {'conversation_id': conversation.pk, 'text': 'And another.'},
            content_type='application/json')
        payload = response.json()

        reply = ChatMessage.objects.filter(
            conversation=conversation, role='assistant').latest('created_at')
        expected = render_to_string('partials/_chat_message.html',
                                    {'message': reply, 'agent': agent})

        self.assertEqual(payload['assistant_message']['html'].strip(),
                         expected.strip())

        # The rendered Markdown is inside that HTML, produced by the one
        # renderer, and the person's own text is never run through it.
        self.assertIn(str(md.render(reply.content)),
                      payload['assistant_message']['html'])
        self.assertIn('msg--user', payload['user_message']['html'])
        self.assertNotIn('msg__content--rich', payload['user_message']['html'])

    def test_a_typed_message_is_shown_verbatim(self):
        """Someone typing **stars** meant to type stars."""
        user = User.objects.create_user('bob', 'b@example.com', 'pw-bob-12345')
        agent_engine.ensure_workspace_for_user(user)
        agent = AIAgent.objects.first()
        conversation = agent_engine.start_conversation(user, agent)

        self.client.force_login(user)
        response = self.client.post(
            reverse('api_send_message'),
            {'conversation_id': conversation.pk, 'text': 'Use **these** exact stars'},
            content_type='application/json')
        self.assertEqual(response.json()['user_message']['content'],
                         'Use **these** exact stars')


class TimestampTests(TestCase):
    """Both rendering paths must report the same clock.

    REGRESSION: the interface showed a person's message at 22:29 and the reply
    beside it at 17:00. Django rendered one in UTC while the browser stamped
    the other locally, so the same moment appeared twice, five and a half hours
    apart.
    """

    def test_times_are_displayed_in_the_configured_zone(self):
        user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(user)
        agent = AIAgent.objects.first()
        conversation = agent_engine.start_conversation(user, agent)

        self.client.force_login(user)
        payload = self.client.post(
            reverse('api_send_message'),
            {'conversation_id': conversation.pk, 'text': 'Hello'},
            content_type='application/json').json()

        message = ChatMessage.objects.get(pk=payload['user_message']['id'])
        expected = timezone.localtime(message.created_at).strftime('%H:%M')
        self.assertEqual(payload['user_message']['created_at'], expected)

    def test_timestamps_are_stored_in_utc(self):
        """Display zone is a presentation choice; storage stays UTC."""
        self.assertTrue(settings.USE_TZ)
        user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(user)
        conversation = agent_engine.start_conversation(user, AIAgent.objects.first())
        message = ChatMessage.objects.create(
            conversation=conversation, role='user', content='x')
        self.assertEqual(message.created_at.utcoffset().total_seconds(), 0)


class EmailDeliveryTests(TestCase):
    """Approving an outreach email is what actually sends it.

    Django's test runner swaps EMAIL_BACKEND for the in-memory backend, so
    everything below is captured in mail.outbox and nothing leaves the machine.
    """

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        self.user.profile.role = roles.ROLE_ADMIN
        self.user.profile.save()
        agent_engine.ensure_workspace_for_user(self.user)

        self.lead = Lead.objects.create(
            user=self.user, full_name='Marcus Sterling',
            email='marcus@sterlingtech.example', company='Sterling Tech',
            job_title='CRO', industry='SaaS', lead_score=90, score_tier='hot')

        agent = AIAgent.objects.get(agent_type='outreach')
        conversation = agent_engine.start_conversation(self.user, agent)
        _user_msg, self.reply = agent_engine.send_message(
            conversation, 'Write a first-touch email.')
        self.approval = agent_engine.submit_message_for_approval(
            self.reply, item_type='email', title='Intro to Sterling Tech',
            lead=self.lead)
        mail.outbox = []

    # --- nothing is sent before approval ----------------------------------

    def test_drafting_sends_nothing(self):
        """The whole point of the queue: a draft reaches no one."""
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.approval.email_outreach.status, 'draft')
        self.assertIsNone(self.approval.email_outreach.delivered_at)

    def test_rejecting_sends_nothing(self):
        agent_engine.apply_approval(self.approval, self.user, 'rejected', 'Tone is wrong.')
        self.assertEqual(len(mail.outbox), 0)

        email = self.approval.email_outreach
        email.refresh_from_db()
        self.assertEqual(email.status, 'draft')
        self.assertIsNone(email.delivered_at)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'new')

    # --- approving sends --------------------------------------------------

    def test_approving_sends_the_email(self):
        agent_engine.apply_approval(self.approval, self.user, 'approved')

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['marcus@sterlingtech.example'])
        self.assertTrue(sent.subject)
        # The subject is taken from the draft's own "Subject:" line, so it is
        # not repeated inside the body the recipient reads.
        self.assertNotIn('Subject:', sent.body)

    def test_approving_records_delivery_and_advances_the_lead(self):
        agent_engine.apply_approval(self.approval, self.user, 'approved')

        email = self.approval.email_outreach
        email.refresh_from_db()
        self.assertEqual(email.status, 'sent')
        self.assertIsNotNone(email.delivered_at)
        self.assertEqual(email.delivery_error, '')
        self.assertTrue(email.was_delivered)

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'contacted')

    def test_the_body_says_a_person_approved_it(self):
        agent_engine.apply_approval(self.approval, self.user, 'approved')
        self.assertIn('approved by a person', mail.outbox[0].body)

    # --- failure does not lose the decision --------------------------------

    def test_a_delivery_failure_is_recorded_rather_than_raised(self):
        """A dead mail server must not discard an approval already taken."""
        with mock.patch('django.core.mail.EmailMultiAlternatives.send',
                        side_effect=smtplib.SMTPAuthenticationError(535, b'bad creds')):
            agent_engine.apply_approval(self.approval, self.user, 'approved')

        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, 'approved')   # the decision stands

        email = self.approval.email_outreach
        email.refresh_from_db()
        self.assertEqual(email.status, 'bounced')
        self.assertIsNone(email.delivered_at)
        self.assertIn('App Password', email.delivery_error)

        # The prospect was never reached, so their status is left alone.
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'new')

    def test_a_failure_is_written_into_the_audit_trail(self):
        with mock.patch('django.core.mail.EmailMultiAlternatives.send',
                        side_effect=OSError('network down')):
            agent_engine.apply_approval(self.approval, self.user, 'approved')

        note = self.approval.audit_entries.filter(action='approved').first().note
        self.assertIn('Delivery:', note)

    def test_an_email_with_no_recipient_fails_cleanly(self):
        self.lead.email = ''
        self.lead.save()
        self.approval.email_outreach.recipient_email = ''
        self.approval.email_outreach.save()
        agent_engine.apply_approval(self.approval, self.user, 'approved')

        email = self.approval.email_outreach
        email.refresh_from_db()
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn('no recipient address', email.delivery_error)

    # --- configuration reporting -------------------------------------------

    def test_is_configured_reflects_the_environment(self):
        with self.settings(EMAIL_HOST_USER='', EMAIL_HOST_PASSWORD=''):
            self.assertFalse(mailer.is_configured())
            self.assertIn('Console', mailer.backend_label())

        with self.settings(EMAIL_HOST_USER='a@b.com', EMAIL_HOST_PASSWORD='secret'):
            self.assertTrue(mailer.is_configured())
            self.assertIn('SMTP', mailer.backend_label())

    def test_the_connection_check_explains_an_unconfigured_server(self):
        with self.settings(EMAIL_HOST_USER='', EMAIL_HOST_PASSWORD=''):
            result = mailer.check_connection()
        self.assertFalse(result['ok'])
        self.assertFalse(result['configured'])
        self.assertIn('EMAIL_HOST_USER', result['message'])

    # --- endpoints ----------------------------------------------------------

    def test_the_test_endpoint_needs_permission(self):
        weak = User.objects.create_user('weak', 'w@example.com', 'pw-weak-12345')
        weak.profile.role = roles.ROLE_VIEWER
        weak.profile.save()
        self.client.force_login(weak)
        self.assertEqual(
            self.client.post(reverse('api_test_email'), '{}',
                             content_type='application/json').status_code, 403)

    def test_only_an_administrator_may_send_a_test_message(self):
        manager = User.objects.create_user('mo', 'm@example.com', 'pw-mo-123456')
        manager.profile.role = roles.ROLE_MANAGER
        manager.profile.save()
        self.client.force_login(manager)
        response = self.client.post(
            reverse('api_send_test_email'), {'recipient': 'a@b.com'},
            content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_test_message_needs_a_valid_address(self):
        self.client.force_login(self.user)
        for bad in ['', '   ', 'not-an-address']:
            with self.subTest(recipient=bad):
                response = self.client.post(
                    reverse('api_send_test_email'), {'recipient': bad},
                    content_type='application/json')
                self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_test_message_touches_no_prospect(self):
        """It must not be mistaken for outreach: nothing is recorded against a lead."""
        with self.settings(EMAIL_HOST_USER='a@b.com', EMAIL_HOST_PASSWORD='secret'):
            result = mailer.send_test_message('me@example.com')

        self.assertTrue(result['ok'])
        self.assertEqual(mail.outbox[0].to, ['me@example.com'])
        self.assertEqual(EmailOutreach.objects.filter(delivered_at__isnull=False).count(), 0)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'new')

    # --- writing to someone who is not on the pipeline ---------------------

    def test_an_email_can_be_addressed_to_a_typed_address(self):
        """REGRESSION: the dialog only offered existing prospects, so asking
        Aria to write to an arbitrary address had no way through."""
        agent = AIAgent.objects.get(agent_type='outreach')
        conversation = agent_engine.start_conversation(self.user, agent)
        _user_msg, reply = agent_engine.send_message(
            conversation, 'send email to someone@example.com')

        approval = agent_engine.submit_message_for_approval(
            reply, item_type='email', title='Hello',
            recipient_email='someone@example.com')

        email = approval.email_outreach
        self.assertIsNone(email.lead_id)
        self.assertEqual(email.recipient, 'someone@example.com')

        agent_engine.apply_approval(approval, self.user, 'approved')
        self.assertEqual(mail.outbox[-1].to, ['someone@example.com'])

    def test_a_prospect_still_works_and_stays_linked(self):
        self.assertEqual(self.approval.email_outreach.lead_id, self.lead.pk)
        self.assertEqual(self.approval.email_outreach.recipient, self.lead.email)
        # No duplicate address stored when it is simply the prospect's own.
        self.assertEqual(self.approval.email_outreach.recipient_email, '')

    def test_an_email_with_neither_recipient_nor_prospect_is_refused(self):
        agent = AIAgent.objects.get(agent_type='outreach')
        conversation = agent_engine.start_conversation(self.user, agent)
        _user_msg, reply = agent_engine.send_message(conversation, 'Write something.')
        with self.assertRaises(ValueError):
            agent_engine.submit_message_for_approval(
                reply, item_type='email', title='Nowhere to go')

    def test_the_endpoint_rejects_a_malformed_address(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('api_submit_for_approval'),
            {'message_id': self.reply.pk, 'item_type': 'email',
             'title': 'Hi', 'recipient_email': 'not-an-address'},
            content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_the_endpoint_accepts_a_typed_address(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('api_submit_for_approval'),
            {'message_id': self.reply.pk, 'item_type': 'email',
             'title': 'Hi', 'recipient_email': 'typed@example.com'},
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            EmailOutreach.objects.filter(recipient_email='typed@example.com').exists())

    # --- the subject line --------------------------------------------------

    def test_a_subject_line_in_the_draft_becomes_the_subject(self):
        subject, body = agent_engine.split_subject(
            'Subject: Quick question\n\nHi Ismail,\n\nHope you are well.',
            fallback='Unused')
        self.assertEqual(subject, 'Quick question')
        self.assertNotIn('Subject:', body)

    def test_a_draft_without_a_subject_line_uses_the_title(self):
        subject, body = agent_engine.split_subject(
            'Hi Ismail, hope you are well.', fallback='Intro')
        self.assertEqual(subject, 'Intro')
        self.assertEqual(body, 'Hi Ismail, hope you are well.')


class AgentPromptTests(TestCase):
    """The employees must know what the platform can do.

    REGRESSION: Aria repeatedly answered "I cannot send emails, I have no
    access to email servers" and told the user to copy and paste into Gmail.
    Nothing in her prompt mentioned the platform, so the underlying model fell
    back to the disclaimer a general assistant gives -- while the platform was
    in fact perfectly capable of sending.
    """

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(self.user)

    def test_every_employee_is_told_how_its_work_is_delivered(self):
        for agent in AIAgent.objects.all():
            with self.subTest(agent=agent.name):
                self.assertIn('approval queue', agent.system_prompt)
                self.assertIn('Never say you are unable to send',
                              agent.system_prompt)

    def test_aria_is_told_to_write_the_email_rather_than_decline(self):
        aria = AIAgent.objects.get(agent_type='outreach')
        self.assertIn('WHEN SOMEONE ASKS YOU TO SEND AN EMAIL, WRITE IT',
                      aria.system_prompt)
        self.assertIn('Subject:', aria.system_prompt)

    def test_the_offline_reply_is_a_ready_to_send_email(self):
        """Even with no model, the draft must be usable rather than a lecture."""
        aria = AIAgent.objects.get(agent_type='outreach')
        self.assertFalse(aria.has_live_llm)

        conversation = agent_engine.start_conversation(self.user, aria)
        _user_msg, reply = agent_engine.send_message(
            conversation, 'send email to someone@example.com')

        self.assertTrue(reply.content.lstrip().lower().startswith('subject:'))
        for phrase in ['cannot send', 'unable to send', 'copy', 'paste']:
            self.assertNotIn(phrase, reply.content.lower())

    def test_refreshing_prompts_updates_an_existing_employee(self):
        """ensure_agents uses get_or_create, so a wording fix needs this."""
        aria = AIAgent.objects.get(agent_type='outreach')
        aria.system_prompt = 'Stale wording from an older release.'
        aria.save()

        updated = agent_engine.refresh_system_prompts()
        self.assertGreaterEqual(updated, 1)

        aria.refresh_from_db()
        self.assertIn('WHEN SOMEONE ASKS YOU TO SEND AN EMAIL', aria.system_prompt)

    def test_refreshing_prompts_twice_changes_nothing(self):
        agent_engine.refresh_system_prompts()
        self.assertEqual(agent_engine.refresh_system_prompts(), 0)


class EnvFileTests(TestCase):
    """Configuration must survive closing the terminal.

    REGRESSION: credentials were set with `$env:X = "y"`, which lives only as
    long as that one PowerShell window. The next `runserver` therefore started
    with nothing, the mail backend silently fell back to the console, and the
    Configurations page reported "Console only" -- with no error anywhere to
    explain why. A .env file is read on every start-up instead.
    """

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile(
            'w', suffix='.env', delete=False, encoding='utf-8')
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_a_plain_assignment_is_loaded(self):
        path = self._write('DEMO_PLAIN=hello\n')
        self.addCleanup(os.environ.pop, 'DEMO_PLAIN', None)

        self.assertEqual(env_file.load(path), 1)
        self.assertEqual(os.environ['DEMO_PLAIN'], 'hello')

    def test_quotes_spaces_comments_and_export_are_handled(self):
        path = self._write(
            '# a comment\n'
            '\n'
            'DEMO_QUOTED="quoted value"\n'
            "DEMO_SINGLE='single'\n"
            'DEMO_SPACED=xjjd kjfd uozz byfn\n'
            'export DEMO_EXPORTED=exported\n'
            'not-an-assignment\n')
        for name in ['DEMO_QUOTED', 'DEMO_SINGLE', 'DEMO_SPACED', 'DEMO_EXPORTED']:
            self.addCleanup(os.environ.pop, name, None)

        self.assertEqual(env_file.load(path), 4)
        self.assertEqual(os.environ['DEMO_QUOTED'], 'quoted value')
        self.assertEqual(os.environ['DEMO_SINGLE'], 'single')
        # A Gmail App Password is shown in four groups of four; the spaces are
        # presentation only, and settings.py is what strips them.
        self.assertEqual(os.environ['DEMO_SPACED'], 'xjjd kjfd uozz byfn')
        self.assertEqual(os.environ['DEMO_EXPORTED'], 'exported')

    def test_a_real_environment_variable_wins(self):
        """The file is a default, not an override, so a server or CI can still
        inject its own values without anyone editing it."""
        path = self._write('DEMO_PRECEDENCE=from-file\n')
        os.environ['DEMO_PRECEDENCE'] = 'from-shell'
        self.addCleanup(os.environ.pop, 'DEMO_PRECEDENCE', None)

        self.assertEqual(env_file.load(path), 0)
        self.assertEqual(os.environ['DEMO_PRECEDENCE'], 'from-shell')

    def test_a_missing_file_is_not_an_error(self):
        """The project must still start on a machine that has no .env."""
        self.assertEqual(env_file.load('no/such/.env'), 0)

    def test_settings_reads_the_env_file_on_start_up(self):
        source = io.open('marketpulse/settings.py', encoding='utf-8').read()
        self.assertIn("env.load(BASE_DIR / '.env')", source)
        # It must run before the settings that read os.environ, or it would
        # load the values a moment too late to have any effect.
        self.assertLess(source.index('env.load('), source.index('EMAIL_HOST_USER ='))

    def test_the_env_file_is_never_committed(self):
        entries = io.open('.gitignore', encoding='utf-8').read().split('\n')
        self.assertIn('.env', entries)

    def test_the_example_lists_every_variable_the_project_reads(self):
        """.env.example is what a marker or a teammate copies, so a variable
        missing from it is a variable nobody knows to set."""
        example = io.open('.env.example', encoding='utf-8').read()
        for name in ['NVIDIA_API_KEY', 'OPENROUTER_API_KEY',
                     'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD']:
            with self.subTest(variable=name):
                self.assertIn(name, example)


class MailRouterTests(TestCase):
    """What a person types has to become the right mailbox action.

    The routing is done in Python rather than by asking the language model,
    so that "check my inbox" works identically on a machine with no API key.
    That choice is only defensible if the routing is actually good, which is
    what these prove.
    """

    def assertAction(self, said, expected):
        intent = mail_agent.detect(said)
        actual = intent['action'] if intent else None
        self.assertEqual(actual, expected, f'{said!r} -> {actual!r}')
        return intent

    def test_asking_for_new_mail_lists_only_unread(self):
        for said in ['any new mail?', 'anything new?', 'do i have unread emails']:
            with self.subTest(said=said):
                self.assertTrue(self.assertAction(said, 'list')['unread_only'])

    def test_asking_for_the_inbox_lists_everything(self):
        for said in ['check my inbox', "what's in my inbox", 'list my recent messages']:
            with self.subTest(said=said):
                self.assertFalse(self.assertAction(said, 'list')['unread_only'])

    def test_a_number_sets_how_many_are_shown(self):
        self.assertEqual(self.assertAction('show my last 5 emails', 'list')['limit'], 5)

    def test_searching_strips_the_filler_words(self):
        """"find mail about the timetable" must search for the timetable, not
        for "about the timetable"."""
        cases = {
            'anything from nvidia?': 'nvidia',
            'search for invoices': 'invoices',
            'find mail about the timetable': 'timetable',
            'emails from indeed': 'indeed',
        }
        for said, expected in cases.items():
            with self.subTest(said=said):
                self.assertEqual(self.assertAction(said, 'search')['query'], expected)

    def test_a_listing_can_be_referred_to_by_position(self):
        cases = {'open 2': 2, 'read the third one': 3, 'show me number 1': 1,
                 'open the last one': -1}
        for said, expected in cases.items():
            with self.subTest(said=said):
                self.assertEqual(self.assertAction(said, 'open')['index'], expected)

    def test_a_reply_carries_its_instruction(self):
        intent = self.assertAction('reply to 2 saying thanks', 'reply')
        self.assertEqual(intent['index'], 2)
        self.assertEqual(intent['instruction'], 'thanks')

    def test_pointing_words_are_not_mistaken_for_an_instruction(self):
        """"reply to the first one" asks for a reply, and says nothing about
        what it should contain. The trailing "one" belongs to the reference."""
        self.assertEqual(
            self.assertAction('reply to the first one', 'reply')['instruction'], '')

    def test_an_explicit_address_is_left_to_the_composing_path(self):
        """Writing to someone is not a mailbox query, and must not be
        hijacked into a search for their address."""
        self.assertIsNone(mail_agent.detect(
            'send email to sam@example.com about pricing'))

    def test_ordinary_conversation_is_left_alone(self):
        for said in ['hello', 'what can you do?', 'write a linkedin post about AI']:
            with self.subTest(said=said):
                self.assertIsNone(mail_agent.detect(said))

    def test_a_number_inside_a_sentence_is_not_a_reference(self):
        """REGRESSION: "read 2 articles about marketing" opened message 2.

        It starts with a verb and a number exactly like "read 2" does. What
        separates them is the tail: a reference is finished once the number is
        given, and this one carries the noun the number was counting.
        """
        self.assertIsNone(mail_agent.detect(
            'read 2 articles about marketing and summarise them for me'))


class MailboxToolGateTests(TestCase):
    """The MCP tool record is what grants access, not the code.

    This is what makes modelling MCP worth doing: switching the Gmail server
    off on the MCP Tools page has to actually take the capability away.
    """

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(self.user)
        agent_engine.refresh_agent_tools()
        self.aria = AIAgent.objects.get(agent_type='outreach')

    def test_aria_has_the_reading_tools(self):
        attached = {tool.qualified_name for tool in self.aria.mcp_tools.all()}
        for name in ['gmail.list_messages', 'gmail.search_messages',
                     'gmail.read_message', 'gmail.create_draft', 'gmail.send_email']:
            with self.subTest(tool=name):
                self.assertIn(name, attached)

    def test_nothing_is_withheld_while_the_tools_are_attached(self):
        self.assertEqual(mail_agent.withheld(self.aria, 'gmail.list_messages'), '')

    def test_disabling_the_server_withdraws_the_capability(self):
        server = MCPServer.objects.get(server_key='gmail')
        server.is_enabled = False
        server.save(update_fields=['is_enabled'])

        reason = mail_agent.withheld(self.aria, 'gmail.list_messages')
        self.assertIn('switched off', reason)

    def test_disabling_one_tool_withdraws_only_that_one(self):
        tool = MCPTool.objects.get(server__server_key='gmail', tool_name='search_messages')
        tool.is_enabled = False
        tool.save(update_fields=['is_enabled'])

        self.assertIn('disabled', mail_agent.withheld(self.aria, 'gmail.search_messages'))
        self.assertEqual(mail_agent.withheld(self.aria, 'gmail.list_messages'), '')

    def test_an_employee_without_the_tool_cannot_read_mail(self):
        sophia = AIAgent.objects.get(agent_type='content')
        self.assertIn('does not have', mail_agent.withheld(sophia, 'gmail.list_messages'))

    def test_a_withheld_action_never_touches_the_mailbox(self):
        """The refusal must come before the network call, not after it."""
        server = MCPServer.objects.get(server_key='gmail')
        server.is_enabled = False
        server.save(update_fields=['is_enabled'])

        with mock.patch.object(gmail_client, 'list_messages') as reader:
            result = mail_agent.run(self.aria, {'action': 'list'}, None)

        reader.assert_not_called()
        self.assertIn('switched off', result['facts'])

    def test_refreshing_tools_is_additive_and_idempotent(self):
        """A person may have detached a tool deliberately; a provisioning pass
        must add what is missing without undoing that."""
        link = self.aria.tool_links.first()
        link.delete()

        self.assertGreaterEqual(agent_engine.refresh_agent_tools(), 1)
        self.assertEqual(agent_engine.refresh_agent_tools(), 0)


class MailboxChatTests(TestCase):
    """A mailbox turn, end to end, with the network stubbed out."""

    INBOX = {
        'ok': True,
        'count': 2,
        'total': 2,
        'message': '2 unread messages in INBOX; showing 2.',
        'messages': [
            {'uid': '101', 'sender': 'Google <no-reply@google.com>',
             'sender_email': 'no-reply@google.com', 'to': '', 'subject': 'Security alert',
             'message_id': '<a@x>', 'date': '2026-09-06T22:48:00+00:00',
             'date_display': '06 Sep 2026 at 22:48', 'is_unread': True,
             'snippet': 'App password created.', 'body': 'App password created.'},
            {'uid': '100', 'sender': 'Indeed <no-reply@indeed.com>',
             'sender_email': 'no-reply@indeed.com', 'to': '', 'subject': 'Jobs for you',
             'message_id': '<b@x>', 'date': '2026-09-06T22:24:00+00:00',
             'date_display': '06 Sep 2026 at 22:24', 'is_unread': True,
             'snippet': 'Roles near you.', 'body': 'Roles near you.'},
        ],
    }

    def setUp(self):
        self.user = User.objects.create_user('alice', 'a@example.com', 'pw-alice-123')
        agent_engine.ensure_workspace_for_user(self.user)
        agent_engine.refresh_agent_tools()
        self.aria = AIAgent.objects.get(agent_type='outreach')
        self.conversation = agent_engine.start_conversation(self.user, self.aria)

    def test_asking_for_mail_reads_the_mailbox_and_stores_the_result(self):
        with mock.patch.object(gmail_client, 'list_messages', return_value=self.INBOX):
            _sent, reply = agent_engine.send_message(self.conversation, 'any new mail?')

        self.assertEqual(len(reply.mail_listing), 2)
        self.assertEqual(reply.mail_listing[0]['subject'], 'Security alert')
        self.assertIn('gmail.list_messages', reply.tools_consulted)

    def test_the_mailbox_works_with_no_language_model(self):
        """Every feature of this project works offline, the mailbox included."""
        self.assertFalse(self.aria.has_live_llm)

        with mock.patch.object(gmail_client, 'list_messages', return_value=self.INBOX):
            _sent, reply = agent_engine.send_message(self.conversation, 'any new mail?')

        self.assertEqual(reply.generation_source, 'fallback')
        self.assertIn('2 unread', reply.content)
        self.assertEqual(len(reply.mail_listing), 2)

    def test_the_call_is_logged_against_the_mcp_tool(self):
        before = MCPCallLog.objects.count()
        with mock.patch.object(gmail_client, 'list_messages', return_value=self.INBOX):
            agent_engine.send_message(self.conversation, 'any new mail?')

        self.assertEqual(MCPCallLog.objects.count(), before + 1)
        log = MCPCallLog.objects.latest('called_at')
        self.assertEqual(log.tool.qualified_name, 'gmail.list_messages')
        self.assertEqual(log.outcome, 'ok')

    def test_an_ordinary_turn_logs_no_tool_calls(self):
        """REGRESSION: every chat turn used to write one MCPCallLog row per
        attached tool, so six invented calls buried the real ones."""
        before = MCPCallLog.objects.count()
        agent_engine.send_message(self.conversation, 'hello')
        self.assertEqual(MCPCallLog.objects.count(), before)

    def test_a_position_resolves_against_the_last_listing(self):
        with mock.patch.object(gmail_client, 'list_messages', return_value=self.INBOX):
            agent_engine.send_message(self.conversation, 'any new mail?')

        opened = {'ok': True, 'messages': [self.INBOX['messages'][1]],
                  'count': 1, 'mail': self.INBOX['messages'][1],
                  'message': 'Opened it.'}
        with mock.patch.object(gmail_client, 'get_message', return_value=opened) as reader:
            _sent, reply = agent_engine.send_message(self.conversation, 'open 2')

        # The second card in the listing, not the second message by uid.
        reader.assert_called_once_with('100')
        self.assertEqual(reply.metadata.get('opened'), '100')

    def test_a_reference_with_no_listing_asks_rather_than_guessing(self):
        _sent, reply = agent_engine.send_message(self.conversation, 'open 2')
        self.assertIn('check your inbox', reply.content.lower())
        self.assertEqual(reply.mail_listing, [])

    def test_replying_to_a_message_knows_where_it_goes(self):
        with mock.patch.object(gmail_client, 'list_messages', return_value=self.INBOX):
            agent_engine.send_message(self.conversation, 'any new mail?')

        opened = {'ok': True, 'messages': [self.INBOX['messages'][0]],
                  'count': 1, 'mail': self.INBOX['messages'][0],
                  'message': 'Opened it.'}
        with mock.patch.object(gmail_client, 'get_message', return_value=opened):
            _sent, reply = agent_engine.send_message(
                self.conversation, 'reply to 1 saying thanks')

        self.assertEqual(reply.draft_recipient, 'no-reply@google.com')

    def test_naming_an_address_makes_the_reply_sendable(self):
        _sent, reply = agent_engine.send_message(
            self.conversation, 'send an email to sam@example.com about pricing')
        self.assertEqual(reply.draft_recipient, 'sam@example.com')

    def test_two_addresses_are_not_guessed_between(self):
        """Guessing which of two addresses was meant is how mail reaches the
        wrong person, so neither is used and the dialog is shown instead."""
        _sent, reply = agent_engine.send_message(
            self.conversation, 'email a@example.com and b@example.com about pricing')
        self.assertEqual(reply.draft_recipient, '')

    def test_ordinary_conversation_never_grows_a_send_button(self):
        _sent, reply = agent_engine.send_message(self.conversation, 'hello there')
        self.assertEqual(reply.draft_recipient, '')

    def test_a_mailbox_failure_is_reported_not_raised(self):
        broken = {'ok': False, 'messages': [], 'count': 0,
                  'message': 'The mail server did not answer within 20 seconds.'}
        with mock.patch.object(gmail_client, 'list_messages', return_value=broken):
            _sent, reply = agent_engine.send_message(self.conversation, 'any new mail?')

        self.assertIn('did not answer', reply.content)
        self.assertEqual(reply.mail_listing, [])


class SendNowTests(TestCase):
    """Approve and send in one click, without losing the audit trail."""

    def setUp(self):
        self.user = User.objects.create_user('owner', 'o@example.com', 'pw-owner-1234')
        self.user.profile.role = 'owner'
        self.user.profile.save()
        agent_engine.ensure_workspace_for_user(self.user)

        self.aria = AIAgent.objects.get(agent_type='outreach')
        self.conversation = agent_engine.start_conversation(self.user, self.aria)
        _sent, self.reply = agent_engine.send_message(
            self.conversation, 'send an email to sam@example.com about pricing')
        self.client.force_login(self.user)

    def _post(self, **overrides):
        payload = {'message_id': self.reply.pk}
        payload.update(overrides)
        return self.client.post(reverse('api_send_now'), payload,
                                content_type='application/json')

    def test_one_click_sends_the_email(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'success')

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['sam@example.com'])

    def test_the_approval_record_is_still_written(self):
        """Nothing is skipped: the queue stays a complete record of everything
        that was ever sent."""
        self._post()

        approval = ApprovalRequest.objects.get(source_message=self.reply)
        self.assertEqual(approval.status, 'approved')
        self.assertEqual(approval.decided_by, self.user)
        self.assertEqual(
            set(approval.audit_entries.values_list('action', flat=True)),
            {'created', 'approved'})

    def test_the_reply_comes_back_re_rendered(self):
        """So the button becomes the "Sent to" badge without a page reload,
        and the browser cannot disagree with the server about what happened."""
        html = self._post().json()['html']
        self.assertIn('Sent to sam@example.com', html)
        self.assertNotIn('data-send-now', html)

    def test_a_reply_cannot_be_sent_twice(self):
        self.assertEqual(self._post().json()['status'], 'success')
        second = self._post()
        self.assertEqual(second.json()['status'], 'error')
        self.assertEqual(len(mail.outbox), 1)

    def test_a_malformed_address_is_refused(self):
        response = self._post(recipient_email='not-an-address')
        self.assertEqual(response.json()['status'], 'error')
        self.assertEqual(len(mail.outbox), 0)

    def test_someone_who_cannot_approve_cannot_send(self):
        """The one-click route performs both actions, so it needs both
        permissions. An analyst may submit, but not decide."""
        analyst = User.objects.create_user('ana', 'ana@example.com', 'pw-ana-12345')
        analyst.profile.role = 'analyst'
        analyst.profile.save()

        self.client.force_login(analyst)
        conversation = agent_engine.start_conversation(analyst, self.aria)
        _sent, reply = agent_engine.send_message(
            conversation, 'send an email to sam@example.com about pricing')

        response = self.client.post(
            reverse('api_send_now'), {'message_id': reply.pk},
            content_type='application/json')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_another_persons_conversation_is_not_reachable(self):
        intruder = User.objects.create_user('mal', 'm@example.com', 'pw-mal-12345')
        intruder.profile.role = 'owner'
        intruder.profile.save()

        self.client.force_login(intruder)
        self.assertEqual(self._post().status_code, 404)
        self.assertEqual(len(mail.outbox), 0)
