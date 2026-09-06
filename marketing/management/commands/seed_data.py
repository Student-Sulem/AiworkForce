"""Populate the database with a realistic demonstration workspace.

Run it with:

    python manage.py seed_data --fix-orphans

Every write uses get_or_create or update_or_create keyed on the owning user, so
the command is idempotent: running it twice produces the same database, not
duplicated rows.

THE ORPHAN BUG THIS COMMAND FIXES
---------------------------------
The previous version called, for example,

    AIAgent.objects.get_or_create(name="Sophia", defaults={...})

`user` appeared neither in the lookup nor in `defaults`, so every seeded row was
written with user=NULL. Since every workspace view filters on
`user=request.user`, those rows were invisible to everyone. The fix is to put
the owner in the *lookup*, which is what this file does throughout, and
--fix-orphans re-homes the rows the old command left behind.
"""

import random

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from marketing import agent_engine, mcp_client, roles
from marketing.models import (AIAgent, AnalyticsMetric, ApprovalAuditLog, ApprovalRequest,
                              Conversation, EmailOutreach, Lead, MarketingCampaign,
                              SocialPost)

CAMPAIGNS = [
    {
        'name': 'Q3 Enterprise SaaS Expansion',
        'objective': 'Generate 150 qualified enterprise leads and book 30 product demonstrations.',
        'target_audience': 'SaaS marketing directors at companies with 200-2000 employees',
        'budget': 25000.00,
        'status': 'active',
    },
    {
        'name': 'AI Workforce Product Launch',
        'objective': 'Announce the autonomous agent platform and drive trial sign-ups.',
        'target_audience': 'Growth leads and heads of marketing operations',
        'budget': 18000.00,
        'status': 'active',
    },
    {
        'name': 'Retention and Upsell Programme',
        'objective': 'Re-engage dormant accounts and move them onto the annual plan.',
        'target_audience': 'Existing customers inactive for 60 days or more',
        'budget': 9000.00,
        'status': 'paused',
    },
]

LEADS = [
    ('Marcus Sterling', 'marcus.sterling@sterlingtech.io', 'Sterling Tech',
     'Chief Revenue Officer', 'SaaS & Cloud', 92, 'hot', 'qualified'),
    ('Elena Kovacs', 'elena.kovacs@novacloud.com', 'Nova Cloud',
     'VP of Marketing', 'SaaS & Cloud', 88, 'hot', 'replied'),
    ('David Chen', 'david.chen@apexanalytics.com', 'Apex Analytics',
     'Head of Growth', 'Data & Analytics', 81, 'hot', 'contacted'),
    ('Sarah Miller', 'sarah.miller@brightpath.co', 'BrightPath',
     'Director of Digital Strategy', 'Professional Services', 74, 'warm', 'contacted'),
    ('Olivia Wright', 'olivia.wright@helixlabs.com', 'Helix Labs',
     'VP of Marketing', 'Biotechnology', 69, 'warm', 'new'),
    ('James Novak', 'james.novak@ridgegroup.com', 'Ridge Group',
     'Chief Revenue Officer', 'Manufacturing', 63, 'warm', 'new'),
    ('Chloe Dupont', 'chloe.dupont@lumenretail.fr', 'Lumen Retail',
     'Head of Growth', 'Retail & E-commerce', 47, 'cold', 'unresponsive'),
    ('Daniel Patel', 'daniel.patel@orbitfinance.com', 'Orbit Finance',
     'Director of Digital Strategy', 'Financial Services', 41, 'cold', 'new'),
]

POSTS = [
    ('linkedin', 'Why marketing teams are hiring AI employees',
     'Marketing teams lose roughly 60% of their week to work that does not need a human.\n\n'
     'The teams pulling ahead are not working longer hours. They have delegated the repeatable '
     'part to AI employees and kept the judgement for themselves.\n\n'
     'Which part of your week would you hand over first?',
     '#AIMarketing #Automation #B2BGrowth', 'published', 142, 28, 19),
    ('twitter', 'The lead scoring shortcut nobody mentions',
     'Most lead scores measure how much someone browsed. The useful ones measure how much '
     'someone struggled. Track the second and your pipeline gets honest very quickly.',
     '#MarTech #B2B #LeadGeneration', 'published', 96, 41, 12),
    ('instagram', 'Behind the scenes of an autonomous campaign',
     'Four AI employees. One campaign. Zero missed follow-ups.\n\n'
     'Sophia writes, Hunter sources, Aria follows up, Atlas reports. '
     'A person approves every single thing that goes out.',
     '#AIMarketing #BusinessGrowth', 'published', 214, 17, 33),
    ('linkedin', 'Human-in-the-loop is not a limitation',
     'Every post, email and prospect our AI employees produce lands in an approval queue.\n\n'
     'That is not friction. That is the reason the output is trustworthy enough to send.',
     '#AIGovernance #MarketingOps', 'draft', 0, 0, 0),
    ('facebook', 'A shorter path from prospect to conversation',
     'Automated multi-touch follow-up means no interested buyer goes quiet by accident. '
     'That single change lifted qualified conversations by 68% for our pilot customers.',
     '#EmailMarketing #GrowthHacking', 'draft', 0, 0, 0),
]

METRICS = [
    ('lead_gen', 'Lead Conversion Rate', '24.8%', 4.2, True),
    ('social_reach', 'Monthly Social Reach', '48.2K', 12.6, True),
    ('email_conversion', 'Email Reply Rate', '18.4%', -2.1, False),
    ('roi', 'Campaign ROI', '340%', 18.4, True),
]


class Command(BaseCommand):
    help = ('Seeds AI employees, LLM providers and models, MCP servers and tools, '
            'campaigns, leads, posts, emails and a demonstration approval queue.')

    # One account per role, so the permission matrix can be demonstrated by
    # signing in as each in turn. Passwords are deliberately obvious: this is a
    # coursework build, and they are printed at the end of every seed run.
    DEMO_ACCOUNTS = [
        ('manager', 'manager', 'Manager12345', 'Campaign Manager', False),
        ('analyst', 'analyst', 'Analyst12345', 'Marketing Analyst', False),
        ('viewer', 'viewer', 'Viewer12345', 'Stakeholder', False),
    ]

    def add_arguments(self, parser):
        parser.add_argument('--username', default='admin',
                            help='Username that will own the seeded data (default: admin).')
        parser.add_argument('--fix-orphans', action='store_true',
                            help='Re-home rows left with user=NULL by the old seed command.')
        parser.add_argument('--reset', action='store_true',
                            help='Delete this owner\'s marketing rows before seeding.')

    @transaction.atomic
    def handle(self, *args, **options):
        username = options['username']

        owner = self._ensure_superuser(username)
        demo = self._ensure_demo_user()
        role_accounts = self._ensure_role_accounts()

        if options['reset']:
            self._reset(owner)

        if options['fix_orphans']:
            self._fix_orphans(owner)

        # Roles are auth Groups; re-applying the matrix keeps an existing
        # installation in step with any edit to marketing/roles.py.
        roles.sync_groups()
        for user in [owner, demo] + role_accounts:
            profile = agent_engine.ensure_profile(user)
            roles.apply_role(user, profile.role)

        # ONE shared workspace, not one per account.
        self.stdout.write('\nProvisioning the shared workspace...')
        agent_engine.ensure_workspace(owner=owner)
        self._report_provisioning()

        self.stdout.write('\nSeeding demonstration content...')
        campaigns = self._seed_campaigns(owner)
        leads = self._seed_leads(owner, campaigns)
        posts = self._seed_posts(owner, campaigns)
        emails = self._seed_emails(owner, leads)
        self._seed_metrics(owner)
        self._seed_approvals(owner, posts, emails, leads)
        self._seed_conversations(owner)
        self._run_handshakes(owner)

        self.stdout.write(self.style.SUCCESS('\nSeeding complete.'))
        self.stdout.write('\nSIGN-IN DETAILS -- one account per role:\n')
        self.stdout.write(
            f'  {"USERNAME":<12} {"PASSWORD":<16} {"ROLE":<16} WHAT THEY CAN DO')
        self.stdout.write('  ' + '-' * 76)
        rows = [
            (username, 'admin123', 'Administrator',
             'Everything, and bypasses checks as a superuser'),
            ('demo', 'demo12345', 'Manager',
             'Approve work, manage employees and MCP servers'),
            ('manager', 'Manager12345', 'Manager',
             'Approve work, manage employees and MCP servers'),
            ('analyst', 'Analyst12345', 'Analyst',
             'Chat and submit for approval, but cannot approve'),
            ('viewer', 'Viewer12345', 'Viewer',
             'Read-only: cannot chat, submit or approve'),
        ]
        for row in rows:
            self.stdout.write(f'  {row[0]:<12} {row[1]:<16} {row[2]:<16} {row[3]}')
        self.stdout.write(
            '\n  The Django admin at /admin/ needs a staff account: use "admin".')

    # -- users --------------------------------------------------------------

    def _ensure_superuser(self, username):
        user, created = User.objects.get_or_create(
            username=username,
            defaults={'email': f'{username}@aiworkforce.local',
                      'is_staff': True, 'is_superuser': True})
        if created:
            user.set_password('admin123')
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Created superuser "{username}".'))
        else:
            self.stdout.write(f'Using existing user "{username}".')

        profile = agent_engine.ensure_profile(user)
        profile.role = 'owner'
        profile.job_title = 'Head of Marketing Operations'
        profile.save(update_fields=['role', 'job_title'])
        return user

    def _ensure_demo_user(self):
        user, created = User.objects.get_or_create(
            username='demo',
            defaults={'email': 'demo@aiworkforce.local', 'is_staff': True})
        if created:
            user.set_password('demo12345')
            user.save()
            self.stdout.write(self.style.SUCCESS('Created staff user "demo".'))

        profile = agent_engine.ensure_profile(user)
        profile.role = 'manager'
        profile.job_title = 'Campaign Manager'
        profile.save(update_fields=['role', 'job_title'])
        return user

    def _ensure_role_accounts(self):
        """Create one account per role so RBAC can be demonstrated."""
        created_accounts = []
        for username, role, password, job_title, is_staff in self.DEMO_ACCOUNTS:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={'email': f'{username}@aiworkforce.local', 'is_staff': is_staff})
            if created:
                user.set_password(password)
                user.save()
                self.stdout.write(self.style.SUCCESS(
                    f'Created {role} account "{username}".'))

            profile = agent_engine.ensure_profile(user)
            profile.role = role
            profile.job_title = job_title
            profile.save()
            created_accounts.append(user)
        return created_accounts

    # -- maintenance --------------------------------------------------------

    def _reset(self, owner):
        counts = {
            'approvals': ApprovalRequest.objects.filter(user=owner).delete()[0],
            'posts': SocialPost.objects.filter(user=owner).delete()[0],
            'leads': Lead.objects.filter(user=owner).delete()[0],
            'campaigns': MarketingCampaign.objects.filter(user=owner).delete()[0],
            'metrics': AnalyticsMetric.objects.filter(user=owner).delete()[0],
        }
        self.stdout.write(self.style.WARNING(f'Reset removed: {counts}'))

    def _fix_orphans(self, owner):
        """Adopt rows the previous seed command wrote with user=NULL.

        Agents need care. AIAgent now carries a unique constraint on
        (user, agent_type), so an orphan of a type the owner already has cannot
        simply be reassigned -- that would raise IntegrityError. Such an orphan
        is merged instead: anything pointing at it is re-pointed at the owner's
        existing employee, and the duplicate is then removed.
        """
        owned_agents = {a.agent_type: a for a in AIAgent.objects.filter(user=owner)}
        adopted_agents = merged_agents = 0

        for orphan in AIAgent.objects.filter(user__isnull=True):
            keeper = owned_agents.get(orphan.agent_type)
            if keeper is None:
                orphan.user = owner
                orphan.save(update_fields=['user'])
                owned_agents[orphan.agent_type] = orphan
                adopted_agents += 1
                continue

            MarketingCampaign.objects.filter(assigned_agent=orphan).update(assigned_agent=keeper)
            Lead.objects.filter(discovered_by=orphan).update(discovered_by=keeper)
            SocialPost.objects.filter(agent=orphan).update(agent=keeper)
            EmailOutreach.objects.filter(agent=orphan).update(agent=keeper)
            ApprovalRequest.objects.filter(agent=orphan).update(agent=keeper)
            orphan.delete()
            merged_agents += 1

        adopted = {
            'agents_adopted': adopted_agents,
            'agents_merged': merged_agents,
            'campaigns': self._adopt_unique(MarketingCampaign, owner, 'name'),
            'leads': self._adopt_unique(Lead, owner, 'email'),
            'posts': self._adopt_unique(SocialPost, owner, 'title'),
            'metrics': self._adopt_unique(AnalyticsMetric, owner, 'title'),
        }
        # Outreach emails follow the lead they were sent to.
        emails = 0
        for email in EmailOutreach.objects.filter(user__isnull=True).select_related('lead'):
            EmailOutreach.objects.filter(pk=email.pk).update(
                user=email.lead.user if email.lead_id and email.lead.user_id else owner)
            emails += 1
        adopted['emails'] = emails
        self.stdout.write(self.style.WARNING(f'Adopted orphaned rows: {adopted}'))

    def _adopt_unique(self, model, owner, key_field):
        """Give the owner every orphaned row, dropping ones they already have.

        The seeding methods below identify rows by a natural key -- a lead by
        its email address, a metric by its title -- so silently adopting an
        orphan that duplicates one of those keys would make a later
        get_or_create raise MultipleObjectsReturned.
        """
        taken = set(model.objects.filter(user=owner).values_list(key_field, flat=True))
        adopted = 0
        for orphan in model.objects.filter(user__isnull=True):
            if getattr(orphan, key_field) in taken:
                orphan.delete()
                continue
            model.objects.filter(pk=orphan.pk).update(user=owner)
            taken.add(getattr(orphan, key_field))
            adopted += 1
        return adopted

    def _report_provisioning(self):
        from marketing.models import AIAgent, LLMModel, LLMProvider, MCPServer, MCPTool
        self.stdout.write(
            f'  {AIAgent.objects.count()} AI employees, '
            f'{LLMProvider.objects.count()} LLM providers, '
            f'{LLMModel.objects.count()} models, '
            f'{MCPServer.objects.count()} MCP servers, '
            f'{MCPTool.objects.count()} tools.')

    # -- demonstration content ---------------------------------------------

    def _seed_campaigns(self, owner):
        agents = {a.agent_type: a for a in AIAgent.objects.filter(user=owner)}
        created = []
        for index, data in enumerate(CAMPAIGNS):
            campaign, _ = MarketingCampaign.objects.get_or_create(
                user=owner, name=data['name'],
                defaults={**{k: v for k, v in data.items() if k != 'name'},
                          'assigned_agent': list(agents.values())[index % len(agents)]
                          if agents else None,
                          'start_date': timezone.now().date()})
            created.append(campaign)
        self.stdout.write(f'  {len(created)} campaigns.')
        return created

    def _seed_leads(self, owner, campaigns):
        hunter = AIAgent.objects.filter(user=owner, agent_type='lead_finder').first()
        created = []
        for index, row in enumerate(LEADS):
            name, email, company, title, industry, score, tier, status = row
            lead, _ = Lead.objects.get_or_create(
                user=owner, email=email,
                defaults={
                    'campaign': campaigns[index % len(campaigns)] if campaigns else None,
                    'full_name': name, 'company': company, 'job_title': title,
                    'industry': industry, 'lead_score': score, 'score_tier': tier,
                    'status': status, 'discovered_by': hunter,
                    'notes': f'Sourced through intent mining in the {industry} sector.',
                })
            created.append(lead)
        self.stdout.write(f'  {len(created)} leads.')
        return created

    def _seed_posts(self, owner, campaigns):
        sophia = AIAgent.objects.filter(user=owner, agent_type='content').first()
        created = []
        for index, row in enumerate(POSTS):
            platform, title, content, hashtags, status, likes, shares, comments = row
            post, _ = SocialPost.objects.get_or_create(
                user=owner, title=title,
                defaults={
                    'campaign': campaigns[index % len(campaigns)] if campaigns else None,
                    'agent': sophia, 'platform': platform, 'content': content,
                    'hashtags': hashtags, 'status': status,
                    'likes': likes, 'shares': shares, 'comments': comments,
                    'published_at': timezone.now() if status == 'published' else None,
                })
            created.append(post)
        self.stdout.write(f'  {len(created)} social posts.')
        return created

    def _seed_emails(self, owner, leads):
        aria = AIAgent.objects.filter(user=owner, agent_type='outreach').first()
        created = []
        for lead in leads[:3]:
            template = random.choice(agent_engine.EMAIL_TEMPLATES)
            email, _ = EmailOutreach.objects.get_or_create(
                user=owner, lead=lead, sequence_step=1,
                defaults={
                    'campaign': lead.campaign, 'agent': aria,
                    'subject': template['subject'].format(company=lead.company),
                    'body': template['body'].format(
                        name=lead.full_name, company=lead.company, industry=lead.industry),
                    'status': 'sent' if lead.status != 'new' else 'draft',
                })
            created.append(email)
        self.stdout.write(f'  {len(created)} outreach emails.')
        return created

    def _seed_metrics(self, owner):
        for category, title, value, change, positive in METRICS:
            AnalyticsMetric.objects.update_or_create(
                user=owner, title=title,
                defaults={'category': category, 'value': value,
                          'change_percent': change, 'is_positive': positive})
        self.stdout.write(f'  {len(METRICS)} analytics metrics.')

    def _seed_approvals(self, owner, posts, emails, leads):
        """Six approvals: four pending, one approved, one rejected.

        A populated queue means the Approvals page, its filters and its audit
        trail all have something to show the moment the site is opened.
        """
        agents = {a.agent_type: a for a in AIAgent.objects.filter(user=owner)}
        draft_posts = [p for p in posts if p.status == 'draft']
        published_posts = [p for p in posts if p.status == 'published']

        plan = []
        for post in draft_posts[:2]:
            plan.append({
                'item_type': 'social_post', 'agent': agents.get('content'),
                'title': post.title, 'preview': post.content,
                'social_post': post, 'status': 'pending', 'priority': 'normal',
            })
        if emails:
            plan.append({
                'item_type': 'email', 'agent': agents.get('outreach'),
                'title': emails[-1].subject,
                'preview': f'To: {emails[-1].lead.email}\n\n{emails[-1].body}',
                'email_outreach': emails[-1], 'status': 'pending', 'priority': 'high',
            })
        new_leads = [lead for lead in leads if lead.status == 'new']
        if new_leads:
            lead = new_leads[0]
            plan.append({
                'item_type': 'lead', 'agent': agents.get('lead_finder'),
                'title': f'{lead.full_name} at {lead.company}',
                'preview': f'{lead.job_title}, {lead.industry}\n'
                           f'Score: {lead.lead_score} ({lead.score_tier})\n\n{lead.notes}',
                'lead': lead, 'status': 'pending', 'priority': 'normal',
            })
        if published_posts:
            plan.append({
                'item_type': 'social_post', 'agent': agents.get('content'),
                'title': published_posts[0].title, 'preview': published_posts[0].content,
                'social_post': published_posts[0], 'status': 'approved', 'priority': 'normal',
                'reason': '',
            })
        plan.append({
            'item_type': 'insight', 'agent': agents.get('analyst'),
            'title': 'Increase outreach volume by 40%',
            'preview': 'Recommendation: triple the weekly outreach quota across all '
                       'segments to accelerate pipeline growth.',
            'status': 'rejected', 'priority': 'low',
            'reason': 'Tripling volume without segmenting first would damage sender '
                      'reputation. Re-run the analysis restricted to hot prospects.',
        })

        created = 0
        for entry in plan:
            approval, was_created = ApprovalRequest.objects.get_or_create(
                user=owner, item_type=entry['item_type'], title=entry['title'],
                defaults={
                    'agent': entry.get('agent'),
                    'payload_preview': entry['preview'],
                    'social_post': entry.get('social_post'),
                    'email_outreach': entry.get('email_outreach'),
                    'lead': entry.get('lead'),
                    'status': entry['status'],
                    'priority': entry['priority'],
                    'generation_source': 'fallback',
                    'decided_by': owner if entry['status'] != 'pending' else None,
                    'decided_at': timezone.now() if entry['status'] != 'pending' else None,
                    'decision_reason': entry.get('reason', ''),
                })
            if not was_created:
                continue
            created += 1

            ApprovalAuditLog.objects.create(
                approval=approval, actor=owner, action='created',
                note=f'Generated by {approval.agent.name}.' if approval.agent else 'Seeded.',
                from_status='', to_status='pending')
            if approval.status != 'pending':
                ApprovalAuditLog.objects.create(
                    approval=approval, actor=owner, action=approval.status,
                    note=approval.decision_reason,
                    from_status='pending', to_status=approval.status)

        self.stdout.write(f'  {created} approval requests (with audit entries).')

    def _seed_conversations(self, owner):
        """Give the chat screen some history to open with.

        Messages go through agent_engine.send_message, so each thread exercises
        the real path: the employee replies from its own template or model, and
        its MCP tool calls are logged exactly as they would be in use.
        """
        openers = {
            'content': 'Draft a LinkedIn post about why every AI draft should be '
                       'reviewed by a person before it goes out.',
            'lead_finder': 'What buying signals suggest a SaaS company is ready to '
                           'buy marketing automation?',
            'outreach': 'Write a short first-touch email to a VP of Marketing at a '
                        'mid-sized SaaS company.',
            'analyst': 'Summarise the current pipeline and tell me the single '
                       'biggest risk.',
        }

        created = 0
        for agent in AIAgent.objects.filter(user=owner):
            if Conversation.objects.filter(user=owner, agent=agent).exists():
                continue
            question = openers.get(agent.agent_type)
            if not question:
                continue
            conversation = agent_engine.start_conversation(owner, agent)
            agent_engine.send_message(conversation, question)
            created += 1

        self.stdout.write(f'  {created} conversations (each with a reply).')

    def _run_handshakes(self, owner):
        """Give every MCP server a status so the page is not blank on first view."""
        results = {}
        for server in owner.mcp_servers.all():
            outcome = mcp_client.simulate_handshake(server)
            results[outcome['connection_status']] = results.get(
                outcome['connection_status'], 0) + 1
        self.stdout.write(f'  MCP handshakes: {results}')
