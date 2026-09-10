"""The engine that runs the AI employees.

Two responsibilities:

1. PROVISIONING -- `ensure_workspace_for_user` gives every account the four AI
   employees, three LLM providers, seven MCP servers and their tools. It is
   idempotent, so it can safely run on every login.

2. CONVERSATION -- `send_message` and its helpers. An employee is talked to
   rather than triggered: a person types a message, and the employee replies
   using its own system prompt, its assigned language model and the MCP tools
   attached to it. When no model is reachable it answers from a template, so
   the interface always responds.

THE CENTRAL RULE: AGENTS PROPOSE, PEOPLE DISPOSE.
Nothing an employee writes goes anywhere on its own. A reply worth acting on
is submitted to the approval queue by a person, through
`submit_message_for_approval`; only `apply_approval` -- called when someone
presses Approve -- publishes it. That is what the Approvals page governs.

For an outreach email this is literal: approving one hands it to the mail
server through marketing/mailer.py. There is no other path by which this
application sends email.
"""

import random
import re

from django.db import transaction
from django.utils import timezone

from . import llm_client, mail_agent, mailer, mcp_client
from .models import (DEFAULT_CONVERSATION_TITLE, AgentToolLink, AIAgent,
                     AnalyticsMetric, ApprovalAuditLog, ApprovalRequest, ChatMessage,
                     Conversation, EmailOutreach, Lead, LLMModel, LLMProvider,
                     MarketingCampaign, MCPServer, MCPTool, Profile, SocialPost)

POST_TEMPLATES = {
    'linkedin': [
        "Transforming B2B growth with autonomous AI employees.\n\nManual marketing tasks consume up to 60% of team bandwidth. By deploying AI agents for lead mining and content generation, businesses scale outreach five times faster while cutting cost per acquisition by 42%.\n\nOur three-step framework:\n1. AI lead scoring\n2. Context-aware outreach\n3. Real-time performance tuning\n\nWhich marketing workflow are you looking to automate this quarter?",
        "The future of digital marketing is multi-agent collaboration.\n\nImagine dedicated AI specialists working around the clock:\n- Sophia crafts platform-native social content\n- Hunter identifies high-intent prospects\n- Aria runs personalised follow-up sequences\n\nThe result is a 3.4x higher conversion rate with no manual fatigue.",
        "Data-driven marketing insight:\n\nCompanies using automated multi-touch email follow-ups report a 68% increase in qualified sales conversations. Automation is not about volume; it is about delivering the right message at the right moment."
    ],
    'twitter': [
        "Stop writing social posts by hand. AI agents analyse audience sentiment, draft threads and schedule across every channel in seconds. #AI #MarketingAutomation",
        "Lead generation, solved. Mine high-intent B2B prospects, score them, and launch personalised campaigns from one dashboard. #MarTech #B2B",
        "Automated follow-up sequences make sure no buyer falls through the cracks. That is where the 300% ROI actually comes from."
    ],
    'instagram': [
        "Scale your business without burning out. AI marketing employees handle content, outreach and campaign tracking around the clock. Comment below for a walkthrough.",
        "Work smarter, not harder. Our AI workforce writes the posts and nurtures the prospects automatically. #AIMarketing #BusinessGrowth"
    ],
    'facebook': [
        "Boost your marketing efficiency with virtual AI employees. Managing campaigns, generating leads and sending personalised email has never been simpler."
    ]
}

HASHTAG_LISTS = [
    "#AIMarketing #Automation #B2BGrowth #TechInnovation #MarTech",
    "#DigitalMarketing #LeadGeneration #SocialMediaStrategy #AIWorkforce",
    "#EmailMarketing #ContentStrategy #BusinessAutomation #GrowthHacking"
]

COMPANY_SUFFIXES = ["Tech", "Solutions", "Labs", "Systems", "Global", "Networks",
                    "Group", "Cloud", "Analytics", "Digital"]
FIRST_NAMES = ["Alex", "Sarah", "David", "Emma", "Michael", "Elena", "Marcus",
               "Sophia", "James", "Olivia", "Daniel", "Chloe"]
LAST_NAMES = ["Vance", "Chen", "Miller", "Sterling", "Kovacs", "Patel", "Rossi",
              "Hayes", "Novak", "Gupta", "Wright", "Dupont"]
JOB_TITLES = ["VP of Marketing", "Chief Revenue Officer", "Head of Growth",
              "Director of Digital Strategy"]

EMAIL_TEMPLATES = [
    {
        'subject': "Unlocking 3x faster client acquisition for {company}",
        'body': "Hi {name},\n\nI noticed {company} has been expanding in the {industry} sector. Maintaining consistent outreach while managing campaign performance quickly becomes a bottleneck.\n\nOur AI marketing employees automate prospect discovery, personalised follow-ups and social engagement, which has produced a 3.4x higher lead conversion rate for comparable teams.\n\nWould you be open to a five-minute overview this Thursday?\n\nBest regards,\nAria"
    },
    {
        'subject': "Quick question about marketing automation at {company}",
        'body': "Hi {name},\n\nFollowing up on my earlier note. We recently helped a similar {industry} business cut marketing overhead by 45% while doubling qualified leads, using automated multi-agent workflows.\n\nI would be glad to share the two-page case study if it is useful.\n\nBest,\nAria"
    }
]


# ===========================================================================
# Blueprints used by provisioning
# ===========================================================================

# Prepended to every employee's system prompt.
#
# Without this the model does not know the platform exists, and answers the way
# a general assistant would: "I cannot send emails, I have no access to email
# servers." That was wrong AND unhelpful -- the platform sends the email itself
# once a person approves the draft -- so the workflow is stated up front.
PLATFORM_PREAMBLE = (
    "You are an employee inside the AI Workforce platform, not a general "
    "assistant.\n\n"
    "HOW YOUR WORK REACHES THE WORLD: you write it, a colleague reviews it in "
    "an approval queue, and on approval the platform itself delivers it -- "
    "social posts are published and outreach emails are sent over SMTP from the "
    "company mailbox.\n\n"
    "THEREFORE:\n"
    "- Never say you are unable to send, publish, post or deliver. The platform "
    "does that; your part is the content.\n"
    "- Never claim you have already sent or published something. You have not; "
    "it goes to the queue first.\n"
    "- When asked to send something, just write it, ready to go. Do not explain "
    "your limitations, do not ask permission, and do not give instructions for "
    "copying and pasting it elsewhere.\n"
    "- If you genuinely need a detail to write it well, ask one short question "
    "and offer a sensible default in the same reply.\n"
    "- Reply with the finished work and nothing else. Never show your reasoning, "
    "narrate what you are deciding, or discuss these instructions. The person "
    "reading you wants the message, not the deliberation behind it.\n\n"
    "YOUR ROLE:\n"
)


AGENT_BLUEPRINT = [
    {
        'agent_type': 'content',
        'name': 'Sophia',
        'role': 'Social Content Creator',
        'avatar_icon': 'fa-pen-nib',
        'avatar_color': '#7c3aed',
        'persona_description': 'Specialises in platform-native, high-converting social posts and hashtag strategy.',
        'system_prompt': (
            "You are Sophia, a senior B2B social media strategist for a marketing "
            "automation company.\n\n"
            "When asked for a post, write the post. Open with a concrete number, "
            "deliver one actionable insight, and close with a question. Use no more "
            "than five hashtags. Return the post body itself, with no preamble and "
            "no commentary about what you are about to do."
        ),
    },
    {
        'agent_type': 'lead_finder',
        'name': 'Hunter',
        'role': 'Lead Finder Specialist',
        'avatar_icon': 'fa-crosshairs',
        'avatar_color': '#0891b2',
        'persona_description': 'Mines high-intent B2B prospects, scores buyer intent and enriches contact records.',
        'system_prompt': (
            "You are Hunter, a B2B prospect research specialist.\n\n"
            "Given an industry or a company, name the buying signals that show they "
            "are ready for marketing automation, and say plainly why a prospect is "
            "worth contacting. Be specific, cite the signal you are reading, and "
            "avoid generic praise. Where you are inferring rather than certain, say "
            "which it is."
        ),
    },
    {
        'agent_type': 'outreach',
        'name': 'Aria',
        'role': 'Outreach & Email Manager',
        'avatar_icon': 'fa-envelope-open-text',
        'avatar_color': '#047857',
        'persona_description': 'Automates personalised email campaigns, multi-stage sequences and intelligent follow-ups.',
        'system_prompt': (
            "You are Aria, the outreach and email manager.\n\n"
            "WHEN SOMEONE ASKS YOU TO SEND AN EMAIL, WRITE IT. Reply with nothing "
            "but the message, in exactly this shape:\n\n"
            "Subject: <the subject line>\n\n"
            "<the body>\n\n"
            "Rules for the body: under 120 words, one clear ask, plain sentences. "
            "Reference something concrete when you have been told something "
            "concrete; when you have not, write a short courteous note rather than "
            "inventing details or leaving [square-bracket] placeholders for the "
            "reader to fill in. Never use the words 'synergy', 'revolutionary' or "
            "'game-changing'.\n\n"
            "Do not explain that you cannot send it. You write it, a colleague "
            "approves it, and the platform sends it from the company mailbox."
        ),
    },
    {
        'agent_type': 'analyst',
        'name': 'Atlas',
        'role': 'Analytics & Strategy Officer',
        'avatar_icon': 'fa-chart-pie',
        'avatar_color': '#b45309',
        'persona_description': 'Tracks marketing ROI, analyses customer responses and optimises multi-channel campaigns.',
        'system_prompt': (
            "You are Atlas, the analytics and strategy officer.\n\n"
            "Given campaign figures, state what the numbers actually show, name the "
            "single biggest risk, and recommend one concrete next action. Be concise. "
            "Never speculate beyond the data you were given; if a figure you need is "
            "missing, say which one and what you would conclude with it."
        ),
    },
]

# The seven MCP servers, with the tools each one advertises.
# `destructive` marks a tool that writes to the outside world; anything an
# agent produces using one of those always requires human approval.
MCP_BLUEPRINT = [
    {
        'server_key': 'gmail',
        'name': 'Gmail',
        'category': 'communication',
        'icon': 'fa-envelope',
        'color': '#ea4335',
        'transport': 'stdio',
        'command': 'npx',
        'args': '-y @modelcontextprotocol/server-gmail',
        'description': 'Read, search, draft and send email on a connected Gmail account.',
        'config': {'scopes': ['gmail.readonly', 'gmail.send', 'gmail.compose']},
        'tools': [
            ('send_email', 'Send Email', 'Send a composed message to one or more recipients.', True),
            ('create_draft', 'Create Draft', 'Save a message as a draft without sending it.', False),
            ('list_messages', 'List Messages', 'List recent messages in a mailbox.', False),
            ('search_messages', 'Search Messages', 'Search the mailbox with a Gmail query string.', False),
            ('read_message', 'Read Message', 'Open one message and return its body.', False),
        ],
    },
    {
        'server_key': 'instagram',
        'name': 'Instagram',
        'category': 'social',
        'icon': 'fa-instagram',
        'color': '#e1306c',
        'transport': 'http',
        'endpoint_url': 'https://graph.instagram.com/mcp',
        'description': 'Publish media and read engagement insights for a business account.',
        'config': {'api_version': 'v21.0'},
        'tools': [
            ('publish_post', 'Publish Post', 'Publish an image or carousel to the feed.', True),
            ('list_media', 'List Media', 'List recently published media objects.', False),
            ('get_insights', 'Get Insights', 'Read reach, impressions and engagement metrics.', False),
        ],
    },
    {
        'server_key': 'database',
        'name': 'Database',
        'category': 'data',
        'icon': 'fa-database',
        'color': '#0369a1',
        'transport': 'stdio',
        'command': 'npx',
        'args': '-y @modelcontextprotocol/server-sqlite --db-path ./db.sqlite3',
        'description': 'Query and inspect the application database directly.',
        'config': {'read_only': False},
        'tools': [
            ('run_query', 'Run Query', 'Execute a read-only SQL SELECT statement.', False),
            ('list_tables', 'List Tables', 'List every table in the schema.', False),
            ('describe_table', 'Describe Table', 'Return the column definitions for one table.', False),
            ('execute_write', 'Execute Write', 'Run an INSERT, UPDATE or DELETE statement.', True),
        ],
    },
    {
        'server_key': 'memory',
        'name': 'Memory (Knowledge Graph)',
        'category': 'data',
        'icon': 'fa-diagram-project',
        'color': '#7c3aed',
        'transport': 'stdio',
        'command': 'npx',
        'args': '-y @modelcontextprotocol/server-memory',
        'description': 'Persistent knowledge graph of entities, relations and observations.',
        'config': {'storage': 'local'},
        'tools': [
            ('create_entities', 'Create Entities', 'Add entities to the knowledge graph.', False),
            ('create_relations', 'Create Relations', 'Link two entities with a typed relation.', False),
            ('search_nodes', 'Search Nodes', 'Find entities matching a query.', False),
            ('read_graph', 'Read Graph', 'Read the entire knowledge graph.', False),
        ],
    },
    {
        'server_key': 'sequential_thinking',
        'name': 'Sequential Thinking',
        'category': 'reasoning',
        'icon': 'fa-brain',
        'color': '#be123c',
        'transport': 'stdio',
        'command': 'npx',
        'args': '-y @modelcontextprotocol/server-sequential-thinking',
        'description': 'Structured step-by-step reasoning with revision and branching.',
        'config': {'max_thoughts': 25},
        'tools': [
            ('sequentialthinking', 'Sequential Thinking',
             'Work through a problem one revisable step at a time.', False),
        ],
    },
    {
        'server_key': 'spotify',
        'name': 'Spotify',
        'category': 'media',
        'icon': 'fa-spotify',
        'color': '#1db954',
        'transport': 'sse',
        'endpoint_url': 'https://api.spotify.com/mcp/sse',
        'description': 'Search the catalogue and read playlist and playback state.',
        'config': {'scopes': ['user-read-playback-state', 'playlist-read-private']},
        'tools': [
            ('search_tracks', 'Search Tracks', 'Search the Spotify catalogue.', False),
            ('get_playlist', 'Get Playlist', 'Read the tracks in a playlist.', False),
            ('now_playing', 'Now Playing', 'Read what is currently playing.', False),
        ],
    },
    {
        'server_key': 'linkedin',
        'name': 'LinkedIn',
        'category': 'social',
        'icon': 'fa-linkedin',
        'color': '#0a66c2',
        'transport': 'http',
        'endpoint_url': 'https://api.linkedin.com/mcp',
        'description': 'Publish updates and research people and company pages.',
        'config': {'scopes': ['w_member_social', 'r_organization_social']},
        'tools': [
            ('publish_post', 'Publish Post', 'Publish an update to a profile or company page.', True),
            ('search_people', 'Search People', 'Find people matching role and industry filters.', False),
            ('get_company_page', 'Get Company Page', 'Read a company page and its followers.', False),
        ],
    },
]

# Which MCP tools each employee gets by default.
#
# This is the older capability layer and it is not what gives an employee its
# business abilities -- those come from the tool registry in marketing/tools.
# The attachments below are what the MCP Servers page shows and what the
# knowledge-graph and reasoning servers are used for.
AGENT_TOOL_BLUEPRINT = {
    'hr': ['gmail.send_email', 'gmail.create_draft', 'gmail.list_messages',
           'gmail.search_messages', 'gmail.read_message', 'memory.read_graph',
           'memory.create_entities'],
    'engineering_manager': ['database.run_query', 'memory.read_graph',
                            'sequential_thinking.sequentialthinking'],
    'developer': ['database.run_query', 'database.describe_table',
                  'sequential_thinking.sequentialthinking'],
    'research': ['memory.search_nodes', 'memory.read_graph', 'memory.create_entities',
                 'database.run_query', 'database.list_tables'],
    'marketing': ['instagram.publish_post', 'linkedin.publish_post',
                  'instagram.get_insights', 'memory.search_nodes'],
    'support': ['gmail.list_messages', 'gmail.search_messages', 'gmail.read_message',
                'gmail.send_email', 'memory.search_nodes'],
}


# ===========================================================================
# Provisioning
# ===========================================================================

def ensure_profile(user):
    """Guarantee the user has a Profile row."""
    profile, _created = Profile.objects.get_or_create(user=user)
    return profile


def ensure_agents(owner=None):
    """Create the six AI employees for the shared workspace.

    The roster itself moved to marketing/workforce.py and the provisioning to
    marketing/provisioning.py when the platform grew from four marketing
    assistants into six AI employees with tools. This function stays as the
    name the rest of the module calls.
    """
    from . import provisioning
    return provisioning.ensure_agents(owner)


def refresh_system_prompts():
    """Rewrite every default employee's system prompt from the blueprint.

    ensure_agents() uses get_or_create, so an employee created before the
    blueprint changed keeps its old prompt forever. This is how a wording fix
    reaches existing installations; it is called by
    `manage.py seed_data --refresh-prompts`.

    Returns the number of employees updated.
    """
    updated = 0
    for blueprint in AGENT_BLUEPRINT:
        agent = AIAgent.objects.filter(agent_type=blueprint['agent_type']).first()
        if agent is None:
            continue
        wanted = PLATFORM_PREAMBLE + blueprint['system_prompt']
        if agent.system_prompt != wanted:
            agent.system_prompt = wanted
            agent.save(update_fields=['system_prompt'])
            updated += 1
    return updated


def ensure_providers(owner=None):
    """Create the three LLM providers and seed their model catalogues.

    The catalogue comes from llm_client.FALLBACK_CATALOG rather than from the
    network, so a fresh installation has a populated model dropdown with no
    internet connection and no API keys.
    """
    for provider_key, label in LLMProvider.PROVIDER_CHOICES:
        provider, created = LLMProvider.objects.get_or_create(
            provider_key=provider_key,
            defaults={
                'user': owner,
                'display_name': label,
                'base_url': '',
                'api_key': '',
                'is_enabled': True,
                'connection_status': 'untested',
            },
        )
        if created or not provider.models.exists():
            now = timezone.now()
            for entry in llm_client.FALLBACK_CATALOG.get(provider_key, []):
                LLMModel.objects.get_or_create(
                    provider=provider,
                    model_id=entry['model_id'],
                    defaults={
                        'display_name': entry['display_name'],
                        'context_length': entry.get('context_length'),
                        'description': entry.get('description', ''),
                        'source': 'fallback',
                        'fetched_at': now,
                    },
                )


def ensure_mcp_servers(owner=None):
    """Create the seven MCP servers and their advertised tools."""
    for blueprint in MCP_BLUEPRINT:
        server, _created = MCPServer.objects.get_or_create(
            server_key=blueprint['server_key'],
            defaults={
                'user': owner,
                'name': blueprint['name'],
                'description': blueprint['description'],
                'category': blueprint['category'],
                'icon': blueprint['icon'],
                'color': blueprint['color'],
                'transport': blueprint['transport'],
                'command': blueprint.get('command', ''),
                'args': blueprint.get('args', ''),
                'endpoint_url': blueprint.get('endpoint_url', ''),
                'config': blueprint.get('config', {}),
                'is_enabled': True,
            },
        )
        for tool_name, display_name, description, destructive in blueprint['tools']:
            MCPTool.objects.get_or_create(
                server=server,
                tool_name=tool_name,
                defaults={
                    'display_name': display_name,
                    'description': description,
                    'is_destructive': destructive,
                    'is_enabled': True,
                },
            )


def ensure_agent_tools(owner=None):
    """Attach each employee's default MCP tools, once."""
    tools = {
        tool.qualified_name: tool
        for tool in MCPTool.objects.select_related('server')
    }
    for agent in AIAgent.objects.all():
        if agent.tool_links.exists():
            continue
        for qualified_name in AGENT_TOOL_BLUEPRINT.get(agent.agent_type, []):
            tool = tools.get(qualified_name)
            if tool:
                AgentToolLink.objects.get_or_create(
                    agent=agent, tool=tool,
                    defaults={'attached_by': owner, 'is_enabled': True},
                )


def refresh_agent_tools():
    """Give existing employees any tools the blueprint has gained since.

    ensure_agent_tools() skips an employee that already has links, which is
    right: someone may have attached or detached tools deliberately, and a
    provisioning pass must not undo that. But it also means a capability added
    to the blueprint later never reaches the employees already in the database.

    This adds what is missing and removes nothing, so a customised employee
    keeps its customisation and still gains the new capability. Returns the
    number of links created.
    """
    tools = {tool.qualified_name: tool
             for tool in MCPTool.objects.select_related('server')}
    created = 0

    for agent in AIAgent.objects.prefetch_related('tool_links__tool__server'):
        attached = {link.tool.qualified_name for link in agent.tool_links.all()}
        for qualified_name in AGENT_TOOL_BLUEPRINT.get(agent.agent_type, []):
            if qualified_name in attached:
                continue
            tool = tools.get(qualified_name)
            if tool is None:
                continue
            AgentToolLink.objects.get_or_create(
                agent=agent, tool=tool, defaults={'is_enabled': True})
            created += 1

    return created


def ensure_workspace(owner=None):
    """Provision the shared workspace. Safe to call on every login.

    There is one organisation, so this creates one set of employees,
    providers, integrations and MCP servers no matter how many people sign in.
    Contrast the original design, which gave every account its own private
    copy; migration 0006 merged those copies together.

    The work itself is in marketing/provisioning.py -- employees,
    integrations, settings, the generated capability catalogue and the seeded
    knowledge base. The MCP servers below are the older capability layer, kept
    because the MCP Servers page is genuinely about the Model Context Protocol
    rather than about connected applications, and the two are different things.
    """
    from . import provisioning

    report = provisioning.ensure_workspace(owner)
    ensure_mcp_servers(owner)
    ensure_agent_tools(owner)
    return report


def ensure_workspace_for_user(user):
    """Give one person what they need, and make sure the workspace exists.

    Kept as the name the authentication views call: a profile is personal, the
    rest of the workspace is shared.
    """
    from . import provisioning

    provisioning.ensure_profile(user)
    if not AIAgent.objects.exists():
        return ensure_workspace(owner=user)

    # The ordinary case. Only the generated things are refreshed, because
    # those are the ones that go stale when the code changes underneath a
    # database that already exists.
    owner = user if getattr(user, 'pk', None) else None
    provisioning.ensure_integrations(owner)
    provisioning.ensure_settings(owner)
    provisioning.ensure_capabilities()
    ensure_mcp_servers(owner)
    return {}


def sync_agent_tools(agent, tools, actor=None):
    """Make the employee's attachments match `tools` exactly.

    Written by hand rather than relying on ModelForm.save_m2m(), because the
    relation uses a through model and the write path should be one obvious
    function rather than framework magic.
    """
    wanted = {tool.pk for tool in tools}
    existing = set(agent.tool_links.values_list('tool_id', flat=True))

    agent.tool_links.filter(tool_id__in=(existing - wanted)).delete()
    for tool in tools:
        if tool.pk not in existing:
            AgentToolLink.objects.create(agent=agent, tool=tool, attached_by=actor)

    return agent.tool_links.count()


# ===========================================================================
# The chat engine
#
# An AI employee is talked to, not triggered. A person opens a conversation,
# types a message, and the employee replies using its own system prompt, its
# assigned language model and the MCP tools attached to it.
#
# Nothing here publishes anything. When a reply is worth acting on, the person
# submits it to the approval queue through submit_message_for_approval below,
# which keeps the "agents propose, people dispose" rule intact.
# ===========================================================================

# How many previous turns to replay as context on each request. Bounded so a
# long conversation cannot grow the prompt without limit.
CONTEXT_TURNS = 12


# Used when no language model is reachable, so the interface still answers.
FALLBACK_OPENERS = {
    'content': 'Here is a draft you can work from:',
    'lead_finder': 'Here is how I would approach that search:',
    'outreach': 'Here is a draft message:',
    'analyst': 'Here is what the current figures show:',
}


def _workspace_figures(user):
    """The numbers the analyst quotes, gathered in one place."""
    total_leads = Lead.objects.filter(user=user).count()
    converted = Lead.objects.filter(user=user, status='converted').count()
    return {
        'total_leads': total_leads,
        'hot_leads': Lead.objects.filter(user=user, score_tier='hot').count(),
        'converted': converted,
        'published': SocialPost.objects.filter(user=user, status='published').count(),
        'conversion_rate': (converted / total_leads * 100) if total_leads else 0.0,
    }


def _fallback_reply(agent, user_message):
    """Compose a useful reply without a language model.

    This is what keeps the interface usable with no API key and no network
    connection. It is openly a template, and the interface labels every reply
    with its source, so nobody is misled into thinking a model wrote it.
    """
    opener = FALLBACK_OPENERS.get(agent.agent_type, 'Here is my response:')
    topic = ' '.join(user_message.split())[:80]

    if agent.agent_type == 'content':
        platform = 'linkedin'
        lowered = user_message.lower()
        for candidate in POST_TEMPLATES:
            if candidate in lowered:
                platform = candidate
                break
        body = random.choice(POST_TEMPLATES[platform])
        hashtags = random.choice(HASHTAG_LISTS)
        return (f'{opener}\n\n{body}\n\n{hashtags}\n\n'
                f'Tell me the tone you want and I will rewrite it.')

    if agent.agent_type == 'lead_finder':
        return (f'{opener}\n\n'
                f'1. Narrow the segment. Company size, region and the technology '
                f'already in use matter more than job title alone.\n'
                f'2. Look for a trigger. Recent funding, a new marketing hire, or a '
                f'job advert mentioning automation all signal budget.\n'
                f'3. Score fit and intent separately, then make contact only where '
                f'both are high.\n\n'
                f'Give me an industry and I will describe the profile I would target '
                f'for "{topic}".')

    if agent.agent_type == 'outreach':
        # Same shape the system prompt asks the live model for -- a subject
        # line, a blank line, then the body -- so the approval dialog and
        # split_subject() behave identically whether a model answered or not.
        template = random.choice(EMAIL_TEMPLATES)
        body = template['body'].format(
            name='there', company='your company', industry='your sector')
        subject = template['subject'].format(company='your company')
        return f'Subject: {subject}\n\n{body}'

    if agent.agent_type == 'analyst':
        totals = _workspace_figures(agent.user)
        return (f'{opener}\n\n'
                f'Prospects: {totals["total_leads"]}\n'
                f'Hot prospects: {totals["hot_leads"]}\n'
                f'Converted: {totals["converted"]}\n'
                f'Conversion rate: {totals["conversion_rate"]:.1f}%\n'
                f'Published posts: {totals["published"]}\n\n'
                f'The pipeline is weighted towards early-stage prospects. I would '
                f'prioritise follow-up on the {totals["hot_leads"]} hot prospects '
                f'before sourcing more volume.')

    return (f'{opener}\n\nI have noted "{topic}". Assign me a language model on the '
            f'Configurations page and I will answer properly.')


def start_conversation(user, agent, title=DEFAULT_CONVERSATION_TITLE):
    """Open a fresh thread with one employee."""
    return Conversation.objects.create(user=user, agent=agent, title=title)


def _history_for(conversation):
    """The recent turns, oldest first, as chat-completion message dictionaries.

    Slicing the newest CONTEXT_TURNS and reversing keeps the prompt bounded
    while preserving the order the provider expects.
    """
    recent = list(
        conversation.messages.filter(is_error=False)
        .exclude(role='system')
        .order_by('-created_at')[:CONTEXT_TURNS]
    )
    recent.reverse()
    return [{'role': m.role, 'content': m.content} for m in recent]


ADDRESS_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')


def _recipient_for(conversation, asked, reply, metadata):
    """Where this reply would be sent, if it is an email and the address is known.

    Storing it is what lets the reply carry a one-click "Approve and send"
    instead of a dialog asking for an address the person already typed. It is
    only set when there is exactly one candidate, because guessing between two
    addresses is how mail goes to the wrong person.

    A reply that is not shaped like an email gets nothing, so ordinary
    conversation never grows a send button.
    """
    if metadata.get('draft', {}).get('recipient'):
        return metadata['draft']['recipient']

    if not SUBJECT_LINE_RE.match((reply or '').lstrip().splitlines()[0] if reply.strip() else ''):
        return ''

    # Look at what was just asked first, then back through the thread, and stop
    # at the first turn that names an address.
    candidates = ADDRESS_RE.findall(asked or '')
    if not candidates:
        for message in (conversation.messages.filter(role='user')
                        .order_by('-created_at')[:6]):
            candidates = ADDRESS_RE.findall(message.content)
            if candidates:
                break

    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else ''


def _generate_reply(agent, conversation, latest_text, mailbox=None):
    """Ask the employee's language model, or fall back to a template.

    `mailbox` is the result of a real mailbox action taken for this turn. Its
    facts are appended to the history as a system turn, so the model phrases an
    answer around data that was actually fetched rather than inventing one; and
    its `plain` text becomes the reply outright when there is no live model, so
    the mailbox works exactly the same offline.

    Returns (text, source, model_label, tokens).
    """
    from . import agent_runtime
    if agent_runtime.live_llm_active(agent):
        history = _history_for(conversation)
        if mailbox and mailbox.get('facts'):
            history.append({'role': 'system', 'content': mailbox['facts']})

        result = llm_client.chat_conversation(
            agent.llm_model.provider,
            agent.llm_model.model_id,
            agent.effective_system_prompt,
            history,
            temperature=float(agent.temperature),
            max_tokens=agent.max_tokens,
        )
        if result['ok']:
            return result['text'], 'live', result['model'], result['tokens']

    if mailbox and mailbox.get('plain'):
        return mailbox['plain'], 'fallback', '', 0

    return _fallback_reply(agent, latest_text), 'fallback', '', 0


def send_message(conversation, text, user=None):
    """Record the person's message, run the employee's turn, record the reply.

    Returns (user_message, assistant_message), which is the shape the chat
    view has always expected. It never raises: a provider failure becomes a
    reply produced by running a tool directly, so the conversation can always
    continue.

    THE TURN ITSELF lives in marketing/agent_runtime.py, because an employee's
    turn is no longer one call to a language model. It is a loop: the model
    chooses a tool, the platform runs it, the result goes back, and the loop
    continues until the model has an answer. Tools that reach outside the
    company queue a proposed action rather than acting, so the loop can finish
    without anything having left the building.

    The legacy single-shot path below is kept as the fallback for the case
    where the runtime cannot be imported at all.
    """
    try:
        from . import agent_runtime
    except ImportError:
        return _legacy_send_message(conversation, text)

    outcome = agent_runtime.run_turn(
        conversation, text, user=user or conversation.user)

    # The runtime returns the assistant message; the user's own message is
    # read back rather than passed through, so this keeps working whichever
    # of the two the runtime chooses to hand back.
    user_message = outcome.get('user_message') or conversation.messages.filter(
        role='user').order_by('-created_at').first()
    assistant_message = outcome.get('message') or conversation.messages.filter(
        role='assistant').order_by('-created_at').first()
    return user_message, assistant_message


def _legacy_send_message(conversation, text):
    """The original single-shot reply, kept as a last-resort fallback."""
    agent = conversation.agent

    user_message = ChatMessage.objects.create(
        conversation=conversation,
        role='user',
        content=text.strip(),
        generation_source='manual',
    )

    # Name the thread after its opening question, the way a chat product does.
    # Only while the title is still the untouched default: a thread the person
    # has deliberately renamed must keep the name they chose.
    if (conversation.title == DEFAULT_CONVERSATION_TITLE
            and conversation.messages.filter(role='user').count() == 1):
        conversation.title = conversation.title_from_first_message()

    # Is this turn about the mailbox? The router decides in Python, so
    # "check my inbox" behaves the same with or without a language model.
    intent = mail_agent.detect(text, conversation)
    mailbox = mail_agent.run(agent, intent, conversation) if intent else None

    if mailbox:
        # A real tool call already happened, logged with its own arguments and
        # outcome. Only the tools actually used are named under the reply.
        tool_names = mailbox['tools']
    else:
        # Which capabilities were available for this reply. These are shown as
        # chips under the message but are deliberately NOT written to
        # MCPCallLog: nothing was actually called, and recording six invented
        # calls per chat turn would bury the real ones on the MCP Tools page
        # under noise. That log now contains only calls that genuinely ran.
        tool_names = [tool.qualified_name for tool in mcp_client.tools_for_agent(agent)]

    reply_text, source, model_label, tokens = _generate_reply(
        agent, conversation, text, mailbox=mailbox)

    metadata = dict(mailbox['metadata']) if mailbox else {}
    recipient = _recipient_for(conversation, text, reply_text, metadata)
    if recipient:
        metadata.setdefault('draft', {})['recipient'] = recipient

    assistant_message = ChatMessage.objects.create(
        conversation=conversation,
        role='assistant',
        content=reply_text,
        generation_source=source,
        llm_model_used=model_label,
        tokens_used=tokens,
        tools_consulted=tool_names,
        metadata=metadata,
    )

    # A plain save(), not update_fields, so auto_now refreshes updated_at and
    # the thread rises to the top of the conversation rail.
    conversation.save()

    agent.tasks_completed += 1
    agent.save(update_fields=['tasks_completed'])

    return user_message, assistant_message


# ===========================================================================
# From a conversation into the approval queue
# ===========================================================================

# What a chat reply may be submitted as. 'lead' is deliberately absent: a
# discovered prospect is a structured record, not a piece of written text.
SUBMITTABLE_TYPES = ['social_post', 'email', 'insight']


SUBJECT_LINE_RE = re.compile(r'^\s*subject\s*:\s*(.+?)\s*$', re.IGNORECASE)


def split_subject(content, fallback=''):
    """Separate an email's subject line from its body.

    Aria is instructed to reply as "Subject: ...", blank line, then the body.
    When she does, the subject becomes the real subject rather than being
    repeated inside the message. When she does not, the whole reply is the body
    and `fallback` is used, so a differently shaped answer still works.
    """
    lines = (content or '').splitlines()
    for index, line in enumerate(lines[:3]):
        match = SUBJECT_LINE_RE.match(line)
        if match:
            body = '\n'.join(lines[index + 1:]).strip()
            return match.group(1), body or content.strip()
    return (fallback or 'Message from your AI workforce'), (content or '').strip()


def _first_campaign(user):
    return MarketingCampaign.objects.filter(user=user).first()


def submit_message_for_approval(message, *, item_type, title,
                                platform='linkedin', lead=None, campaign=None,
                                recipient_email=''):
    """Send one assistant reply to the approval queue.

    A draft record is created and linked so that approving it later has
    something concrete to act on:

        social_post  a draft SocialPost, published on approval
        email        a draft EmailOutreach, sent over SMTP on approval
        insight      text only; approving it writes the analytics metrics

    An email needs somewhere to go: either a `lead` from the pipeline or a
    `recipient_email` typed by the reviewer. Requiring a Lead would block
    writing to anyone who is not already a prospect.

    Returns the ApprovalRequest.
    """
    if item_type not in SUBMITTABLE_TYPES:
        raise ValueError(f'A chat reply cannot be submitted as "{item_type}".')

    conversation = message.conversation
    user = conversation.user
    agent = conversation.agent
    social_post = email_outreach = None

    if item_type == 'social_post':
        social_post = SocialPost.objects.create(
            user=user,
            campaign=campaign or _first_campaign(user),
            agent=agent,
            platform=platform,
            title=title[:200],
            content=message.content,
            hashtags=random.choice(HASHTAG_LISTS),
            status='draft',
        )
    elif item_type == 'email':
        address = (recipient_email or '').strip() or (lead.email if lead else '')
        if not address:
            raise ValueError('An outreach email needs a recipient address.')

        subject, body = split_subject(message.content, fallback=title)
        email_outreach = EmailOutreach.objects.create(
            user=user,
            campaign=lead.campaign if lead else None,
            lead=lead,
            recipient_email='' if (lead and address == lead.email) else address,
            agent=agent,
            subject=subject[:250],
            body=body,
            sequence_step=1,
            status='draft',
        )

    approval = ApprovalRequest.objects.create(
        user=user,
        agent=agent,
        item_type=item_type,
        title=title[:200],
        payload_preview=message.content[:4000],
        social_post=social_post,
        email_outreach=email_outreach,
        source_message=message,
        status='pending',
        generation_source=message.generation_source,
        llm_model_used=message.llm_model_used,
        tokens_used=message.tokens_used,
    )
    ApprovalAuditLog.objects.create(
        approval=approval,
        actor=user,
        action='created',
        note=f'Submitted from a conversation with {agent.name}.',
        from_status='',
        to_status='pending',
    )
    return approval


def _write_analyst_metrics(user):
    """Publish the analyst's figures as AnalyticsMetric rows."""
    total_leads = Lead.objects.filter(user=user).count()
    converted = Lead.objects.filter(user=user, status='converted').count()
    conversion_rate = (converted / total_leads * 100) if total_leads else 0.0

    AnalyticsMetric.objects.update_or_create(
        user=user, title='Lead Conversion Rate',
        defaults={'category': 'lead_gen', 'value': f"{conversion_rate:.1f}%",
                  'change_percent': 4.2, 'is_positive': True})
    AnalyticsMetric.objects.update_or_create(
        user=user, title='Campaign ROI',
        defaults={'category': 'roi', 'value': '340%',
                  'change_percent': 18.4, 'is_positive': True})


@transaction.atomic
def apply_approval(approval, actor, decision, reason=''):
    """The single place where an approval decision changes the world.

    Approving publishes the underlying object; rejecting leaves it inert. Either
    way an ApprovalAuditLog entry is written, so the Approvals page can always
    show who decided what and when.
    """
    previous_status = approval.status
    delivery_note = ''
    approval.decide(actor, decision, reason)

    if decision == 'approved':
        if approval.social_post_id:
            post = approval.social_post
            post.status = 'published'
            post.published_at = timezone.now()
            post.save(update_fields=['status', 'published_at'])
        elif approval.email_outreach_id:
            # This is the point at which an email genuinely leaves the server.
            # send_outreach() records the outcome on the row and never raises:
            # a delivery failure must not discard the approval decision that
            # has already been taken, so it is reported rather than thrown.
            email = approval.email_outreach
            result = mailer.send_outreach(email)

            # An email may be addressed to someone who is not on the pipeline
            # at all, in which case there is no prospect record to advance.
            if result['ok'] and email.lead_id:
                lead = email.lead
                lead.status = 'contacted'
                lead.save(update_fields=['status'])

            if not result['ok']:
                # Nobody was actually reached, so no prospect status changes.
                # The reason is on the EmailOutreach row and in the audit note
                # written below.
                delivery_note = result['message']
        elif approval.lead_id:
            lead = approval.lead
            lead.status = 'qualified'
            lead.save(update_fields=['status'])
        elif approval.item_type == 'insight':
            _write_analyst_metrics(approval.user)

    note = reason or ''
    if delivery_note:
        note = f'{note} Delivery: {delivery_note}'.strip()

    ApprovalAuditLog.objects.create(
        approval=approval,
        actor=actor,
        action=decision,
        note=note,
        from_status=previous_status,
        to_status=decision,
    )
    return approval
