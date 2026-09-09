"""One company's real week of work, written into the database on first run.

WHY THIS FILE EXISTS
--------------------
A platform with eleven operational pages and no rows in them does not read as
an empty platform. It reads as a broken one. Somebody opening the Recruitment
page for the first time cannot tell whether the scoring works, whether the
audit trail records anything, or whether the approval queue is wired up,
because every one of those questions is answered by content. So the content
ships.

Everything here belongs to ONE fictional company, deliberately. Northwind
Systems is a forty-person B2B software company in Melbourne selling a logistics
planning platform to freight and distribution operators. The engineering
project, the support queue, the candidates, the campaigns and the audit trail
are all the same week at the same company, which is what makes the platform
legible: the export timeout a customer complains about in Support is the bug
Engineering has open, is the reason the recurring-problem report finds
something true, and is the Jira issue somebody approved in the queue.

IDEMPOTENCY
-----------
Every function is keyed on a natural unique value -- an email address, a title,
a reference, a (parent, title) pair -- so running the seed three times leaves
the database exactly as running it once did. Nothing here uses a bare
``objects.create`` for a row that could already exist.

HOW SEEDED ROWS ARE MARKED, AND WHY THERE ARE TWO MECHANISMS
------------------------------------------------------------
``clear_demo_data`` has to remove precisely what was seeded and nothing a
person has since added. Two mechanisms, because the models differ:

1. A ``demo_seed: true`` key inside an existing dict-shaped JSON field. Used
   where a model already carries a free-form dictionary that nothing renders
   verbatim, so an extra key is invisible and harmless:

       CodeArtifact.metadata     OutboundMessage.metadata
       AuditEvent.detail         SprintReport.metrics
       SupportReport.metrics     AudienceSegment.criteria is NOT used, because
                                 that dictionary is rendered to the reader by
                                 ``criteria_summary``

2. A manifest, held in a single ``SystemSetting`` row with the key
   ``demo_seed_manifest``, mapping ``app_label.modelname`` to the list of
   primary keys created. A manifest was needed because most models in this
   project have no dict-shaped JSON field to hide a flag in. Employee,
   Customer and SupportTicket carry ``notes`` and ``urgency_signals`` -- a
   TextField and a list -- and writing a marker into either would put
   demonstration plumbing on screen in front of a reader.
   CandidateEvaluation's only JSON field is ``score_breakdown``, which is the
   substance of the row and must not carry anything that is not a scored
   requirement. So for those models the provenance lives outside the row
   rather than inside it.

The manifest is the authority; the JSON flags are a second, independent way of
finding the same rows if the manifest is ever lost.

THE auto_now_add TRICK
----------------------
Most models here stamp ``created_at`` with ``auto_now_add=True``, which ignores
whatever value is passed to ``create()``. A demonstration in which every row
was created in the same second looks like a fixture rather than a company, so
rows are written first and their timestamps corrected afterwards with

    Model.objects.filter(pk=obj.pk).update(created_at=when)

A queryset ``update`` writes the column directly and never calls ``save()``,
which is the only way past ``auto_now_add``. It is not an obvious trick, hence
this note and the comment at ``_backdate``.

AUSTRALIAN THROUGHOUT
---------------------
Melbourne, AEST, AUD, ``+61`` numbers, ``.com.au`` addresses. An employee that
offers a candidate a nine o'clock slot in the wrong hemisphere is a bug the
seed data should not be teaching.
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from .models import AIAgent, MarketingCampaign
from .models_content import (AudienceSegment, CampaignBrief, ContentCalendarEntry,
                             ContentPiece, MarketingEmail)
from .models_eng import (CodeArtifact, CodeReview, Project, Sprint, SprintReport,
                         WorkItem, WorkItemComment)
from .models_hr import (Candidate, CandidateEvaluation, Employee, Interview, JobOpening,
                        OnboardingTask)
from .models_knowledge import KnowledgeDocument, ResearchCitation, ResearchReport
from .models_platform import (ActionAuditTrail, AgentMemory, AuditEvent, CalendarEvent,
                              ExternalIssue, Integration, OutboundMessage, ProposedAction,
                              SystemSetting)
from .models_support import (Customer, SupportReport, SupportTicket, TicketMessage,
                             TicketTag)

# The company every row in this module belongs to.
COMPANY = 'Northwind Systems'
DOMAIN = 'example.com'  # RFC 2606 reserved: seeded mail can reach nobody

MANIFEST_KEY = 'demo_seed_manifest'

# Deterministic jitter. A fixed seed matters: re-running the seed must not
# produce a different spread of timestamps, or two runs would look like two
# different companies.
_RNG_SEED = 20260908


# ===========================================================================
# Provenance -- the manifest, and the marker inside JSON fields
# ===========================================================================

_PENDING = {}
_WARNINGS = []

# Deletion has to run children before parents, because several of these
# relations are PROTECT-free but still ordered by sense: removing a Project
# before its WorkItems would cascade rows the manifest also lists, and the
# second delete would then be a no-op that quietly under-reports. Listed
# explicitly rather than derived, so the order is reviewable.
_CLEAR_ORDER = [
    'marketing.actionaudittrail',
    'marketing.outboundmessage',
    'marketing.externalissue',
    'marketing.calendarevent',
    'marketing.proposedaction',
    'marketing.auditevent',
    'marketing.agentmemory',
    'marketing.researchcitation',
    'marketing.researchreport',
    'marketing.contentcalendarentry',
    'marketing.contentpiece',
    'marketing.marketingemail',
    'marketing.audiencesegment',
    'marketing.campaignbrief',
    'marketing.marketingcampaign',
    'marketing.ticketmessage',
    'marketing.supportreport',
    'marketing.supportticket',
    'marketing.tickettag',
    'marketing.customer',
    'marketing.codereview',
    'marketing.codeartifact',
    'marketing.sprintreport',
    'marketing.workitemcomment',
    'marketing.workitem',
    'marketing.sprint',
    'marketing.project',
    'marketing.interview',
    'marketing.candidateevaluation',
    'marketing.candidate',
    'marketing.jobopening',
    'marketing.onboardingtask',
    'marketing.employee',
]


def _label(obj_or_model):
    meta = obj_or_model._meta
    return f'{meta.app_label}.{meta.model_name}'


def _remember(obj):
    """Note a created row so the manifest can find it again."""
    _PENDING.setdefault(_label(obj), []).append(obj.pk)
    return obj


def _manifest_row():
    row, _created = SystemSetting.objects.get_or_create(
        key=MANIFEST_KEY,
        defaults={
            'label': 'Demonstration seed manifest',
            'description': ('Primary keys of the rows written by '
                            'marketing.demo_data. Read only by clear_demo_data, '
                            'so that removing the demonstration content cannot '
                            'touch a row somebody added themselves. Not meant '
                            'to be edited by hand.'),
            'group': 'knowledge',
            'value_type': 'json',
            'value': {'v': {}},
            'display_order': 900,
        },
    )
    return row


def _manifest_load():
    stored = _manifest_row().resolved
    return dict(stored) if isinstance(stored, dict) else {}


def _flush_manifest():
    """Merge this run's created keys into the stored manifest.

    Called at the end of every public seeder rather than only from seed_all,
    because each seeder is a supported entry point on its own.
    """
    if not _PENDING:
        return
    row = _manifest_row()
    stored = _manifest_load()
    for label, pks in _PENDING.items():
        merged = list(stored.get(label) or [])
        for pk in pks:
            if pk not in merged:
                merged.append(pk)
        stored[label] = merged
    row.value = {'v': stored}
    row.save(update_fields=['value'])
    _PENDING.clear()


def _already_seeded(model):
    """Whether the manifest already lists rows of this model.

    The idempotency guard for rows with no natural key at all. An audit event
    is not identified by any of its own columns, so "have these been written
    already" is the only question that can honestly be asked of them.
    """
    return bool(_manifest_load().get(_label(model)))


def warnings():
    """Degradation notices collected during the last seed run."""
    return list(_WARNINGS)


# ===========================================================================
# Timestamps
# ===========================================================================

def _today():
    return timezone.localdate()


def _at(days_ago, hour=10, minute=0):
    """An aware datetime this many days back, at a plausible working hour."""
    when = timezone.localtime(timezone.now()) - timedelta(days=days_ago)
    return when.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _ahead(days, hour=10, minute=0):
    return _at(-days, hour=hour, minute=minute)


def _backdate(obj, when):
    """Push a row's created_at backwards, past auto_now_add.

    auto_now_add overwrites whatever was passed to create(), so the only way to
    set a historical timestamp is a queryset update, which writes the column
    without going through save(). Doing it this way also leaves updated_at
    alone, which is honest: the row genuinely was written just now.
    """
    if when is None or obj is None or obj.pk is None:
        return obj
    names = {f.name for f in obj._meta.get_fields() if hasattr(f, 'attname')}
    if 'created_at' not in names:
        return obj
    type(obj).objects.filter(pk=obj.pk).update(created_at=when)
    obj.created_at = when
    return obj


# ===========================================================================
# Small lookups the seeders share
# ===========================================================================

def _agents():
    """The six employees by agent_type, or an empty mapping if none exist."""
    return {a.agent_type: a for a in AIAgent.objects.all()}


def _integration(provider_key):
    return Integration.objects.filter(provider_key=provider_key).first()


def _owner_or_first(owner):
    if getattr(owner, 'pk', None):
        return owner
    return User.objects.filter(is_superuser=True).first() or User.objects.first()


def _document(title):
    """A seeded knowledge document by title, or None.

    Looked up rather than assumed. The research citations must point at rows
    that genuinely exist, and knowledge.ensure_seed_documents is another
    module's work that may not have run yet.
    """
    return KnowledgeDocument.objects.filter(title=title).first()


def _employee(name):
    return Employee.objects.filter(full_name=name).first()


# ===========================================================================
# 1. COMPANY PROFILE
# ===========================================================================

# The settings that make every employee sound like it works somewhere real.
# Written as full specifications rather than bare values because
# provisioning.ensure_settings depends on marketing/tools/platform.py, which
# may not have created the rows yet; seeding a value into a row that does not
# exist would silently do nothing.
_COMPANY_SETTINGS = [
    ('company_name', 'Company name', 'company', 'text', 1, COMPANY,
     'Used in signatures, documents and anything an employee writes on the '
     'company behalf.'),
    ('company_industry', 'Industry', 'company', 'text', 2,
     'B2B logistics and fleet planning software',
     'Shapes the vocabulary an employee reaches for. A healthcare provider '
     'and a games studio do not write the same rejection email.'),
    ('company_website', 'Website', 'company', 'text', 3, f'https://www.{DOMAIN}',
     'Quoted in outbound messages and marketing copy.'),
    ('company_timezone', 'Timezone', 'company', 'text', 4, 'Australia/Melbourne',
     'Every time an employee writes into an invitation or a deadline is stated '
     'in this zone. An unstated timezone is the most common cause of a missed '
     'interview.'),
    ('working_hours', 'Working hours', 'company', 'json', 5,
     {'start': '08:30', 'end': '17:30',
      'days': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']},
     'When the company is contactable. Used when proposing meeting times, so '
     'an employee does not offer a candidate a Sunday slot.'),
    ('email_signature', 'Email signature', 'company', 'longtext', 6,
     f'Kind regards,\n{COMPANY}\nLevel 4, 120 Bourke Street, Melbourne VIC 3000\n'
     f'+61 3 9034 7720  |  www.{DOMAIN}',
     'Appended to outbound email. Kept here rather than in a template so a '
     'person can change it without a code edit.'),
    ('brand_tone', 'Brand tone', 'company', 'text', 7,
     'Warm but authoritative -- plain Australian English, specific numbers, '
     'no hype',
     'The voice every employee writes in. Marketing copy and a support '
     'apology should sound like the same company.'),
]


def seed_company_profile(owner=None):
    """Give the platform a company to be, in SystemSetting rows.

    An existing value a person has typed is never overwritten. The row is
    created if it is missing, and its value is filled in only when it is still
    empty or still the shipped placeholder, because somebody who has already
    renamed the company should not have that undone by a second seed run.
    """
    # A tuple, not a set: {} and [] are placeholders too, and neither is
    # hashable, so a set literal holding them fails at import time.
    placeholders = ('', None, {}, [], 'Our Company', 'Warm but authoritative')
    count = 0
    for key, label, group, value_type, order, value, description in _COMPANY_SETTINGS:
        row, created = SystemSetting.objects.get_or_create(
            key=key,
            defaults={
                'label': label,
                'description': description,
                'group': group,
                'value_type': value_type,
                'value': {'v': value},
                'display_order': order,
            },
        )
        if created:
            count += 1
            continue
        current = row.resolved
        if current in placeholders or (isinstance(current, str) and not current.strip()):
            row.value = {'v': value}
            row.save(update_fields=['value'])
            count += 1
    _flush_manifest()
    return count


# ===========================================================================
# 2. THE STAFF
# ===========================================================================

# Ten people, which is what a forty-person company's engineering, support and
# marketing floor actually looks like. Start dates are relative to today so the
# directory stays plausible however long after seeding somebody opens it.
_STAFF = [
    # (name, title, department, days since start, status, manager, phone tail,
    #  leave days, skills)
    ('Priya Raghavan', 'Engineering Manager', 'Engineering', 1290, 'active', None,
     '412 668 031', Decimal('18.5'),
     ['delivery planning', 'Python', 'coaching', 'incident command']),
    ('Daniel Okafor', 'Senior Backend Engineer', 'Engineering', 1178, 'active',
     'Priya Raghavan', '413 771 254', Decimal('22.0'),
     ['Python', 'Django', 'PostgreSQL', 'PostGIS', 'AWS']),
    ('Hannah Whitcombe', 'Backend Engineer', 'Engineering', 632, 'active',
     'Priya Raghavan', '404 118 907', Decimal('15.0'),
     ['Python', 'Django REST Framework', 'Celery', 'pytest']),
    ('Liam Petrakis', 'Frontend Engineer', 'Engineering', 331, 'active',
     'Priya Raghavan', '417 502 663', Decimal('11.5'),
     ['TypeScript', 'React', 'accessibility', 'Mapbox']),
    ('Chloe Mainwaring', 'Product Designer', 'Product', 487, 'active', None,
     '431 209 884', Decimal('16.0'),
     ['interaction design', 'Figma', 'usability testing']),
    ('Nadia Halloran', 'Support Lead', 'Customer Operations', 1082, 'active', None,
     '408 335 712', Decimal('9.0'),
     ['escalation management', 'SLA reporting', 'Zendesk', 'SQL']),
    ('Joshua Kirkbride', 'Support Specialist', 'Customer Operations', 214, 'active',
     'Nadia Halloran', '429 640 118', Decimal('19.5'),
     ['ticket triage', 'customer communication', 'Zendesk']),
    ('Aisha Bakhtiari', 'Marketing Manager', 'Marketing', 428, 'active', None,
     '405 887 340', Decimal('13.0'),
     ['B2B content', 'demand generation', 'brand voice', 'HubSpot']),
    ('Marcus Delaney', 'People and Culture Coordinator', 'People Operations',
     -5, 'onboarding', None, '422 913 776', Decimal('20.0'),
     ['HR administration', 'onboarding', 'payroll support']),
    ('Emily Tran', 'Finance Analyst', 'Finance', -12, 'onboarding', None,
     '411 264 559', Decimal('20.0'),
     ['management reporting', 'Xero', 'forecasting']),
]


def _email_for(name):
    first, _, last = name.partition(' ')
    return f'{first.lower()}.{last.split()[-1].lower()}@{DOMAIN}'


# A full checklist, every category represented, with the offsets a real
# induction runs on: hardware before day one, compliance in the second week,
# the thirty-day review at the end. The status mix is what a checklist actually
# looks like part way through rather than a wall of "todo".
_ONBOARDING_TEMPLATE = [
    # (title, category, owner_role, offset, status, description)
    ('Return the signed contract and tax declaration', 'paperwork',
     'People Operations', -7, 'done',
     'Contract, tax file number declaration and superannuation choice form. '
     'Payroll cannot be set up without all three.'),
    ('Collect bank and superannuation details', 'paperwork',
     'People Operations', -6, 'done',
     'Entered directly into payroll by People Operations. Never accepted over '
     'email.'),
    ('Order and image the laptop', 'equipment', 'IT Support', -5, 'done',
     'MacBook Pro 14 inch, imaged with the standard build and encrypted. '
     'Shipped to arrive at least one day before the start date.'),
    ('Ship monitor, dock and headset', 'equipment', 'IT Support', -3,
     'in_progress',
     'Second screen, USB-C dock and a headset for customer calls. Courier '
     'reference goes on this task.'),
    ('Create the email account and single sign-on profile', 'access',
     'IT Support', -3, 'done',
     f'Account at {DOMAIN}, single sign-on profile, and the security groups '
     'for the role.'),
    ('Grant tool access for the role', 'access', 'IT Support', -2, 'in_progress',
     'Slack, the planning platform staging tenant, Jira, Confluence and the '
     'shared drive. Access is granted by role, never by copying another '
     'person account.'),
    ('Enrol in the password manager and enable two-factor', 'compliance',
     'IT Support', 1, 'todo',
     'Mandatory before any production access. Recorded against the employee '
     'for the access audit.'),
    ('Write the thirty-day objectives', 'paperwork', 'Hiring Manager', -2,
     'in_progress',
     'Three objectives the new starter can genuinely reach in a month, '
     'written before they arrive and reviewed at the midpoint.'),
    ('Name a buddy from the team', 'introduction', 'Hiring Manager', -2, 'done',
     'Somebody who is not the manager, and who is the default question '
     'channel for the first month.'),
    ('Run the People Operations induction', 'training', 'People Operations', 0,
     'todo',
     'Forty-five minutes on the handbook, leave, expenses, security and how to '
     'raise a concern.'),
    ('Run the IT access and security session', 'training', 'IT Support', 0,
     'todo',
     'Thirty minutes on access, the password manager and device encryption.'),
    ('Buddy lunch or informal call', 'introduction', 'Buddy', 0, 'todo',
     'Nothing technical is expected on day one and no meeting is scheduled '
     'after three o clock.'),
    ('Complete the security awareness module', 'compliance',
     'People Operations', 8, 'todo',
     'Required for everyone. Records the completion date against the '
     'employee.'),
    ('Read the brand and tone guidelines', 'training', 'Hiring Manager', 9,
     'todo',
     'Required for every customer-facing role, because everything the company '
     'writes should sound like the same company.'),
    ('Verify every account actually works', 'access', 'People Operations', 3,
     'todo',
     'Checked on day three, because an access problem found in week three has '
     'usually been silently worked around since week one.'),
    ('Introductions to the three people the role depends on', 'introduction',
     'Hiring Manager', 10, 'todo',
     'Arranged by the manager, outside the immediate team.'),
    ('Ship one small, genuinely real piece of work', 'training', 'Buddy', 5,
     'todo',
     'Shipping something in week one matters more than its size.'),
    ('Hold the thirty-day review', 'paperwork', 'Hiring Manager', 28, 'todo',
     'What has gone well, what is still unclear, what support is needed, and '
     'whether the objectives were the right ones.'),
]


def seed_employees(owner=None):
    """The staff directory, plus a full onboarding checklist for the two starters.

    Keyed on the email address, which is the one thing about a person that a
    payroll system, a directory and a login all agree on.
    """
    created = 0
    today = _today()

    for (name, title, department, offset, status, manager_name,
         phone, leave, skills) in _STAFF:
        employee, was_created = Employee.objects.get_or_create(
            email=_email_for(name),
            defaults={
                'full_name': name,
                'job_title': title,
                'department': department,
                'location': 'Melbourne, VIC',
                'phone': f'+61 {phone}',
                'employment_status': status,
                'start_date': today - timedelta(days=offset),
                'leave_balance_days': leave,
                'skills': skills,
                'notes': (f'{title} at {COMPANY}. '
                          + ('Starts shortly; onboarding is in progress.'
                             if status == 'onboarding' else
                             'On the permanent staff.')),
            },
        )
        if was_created:
            created += 1
            _remember(employee)
            # Spread the directory's own created_at over the last three weeks
            # rather than the last second. See _backdate.
            _backdate(employee, _at(min(20, offset % 21), hour=9, minute=15))

    # The reporting line is set in a second pass, because a manager has to
    # exist as a row before anybody can point at it.
    for name, _t, _d, _o, _s, manager_name, _p, _l, _sk in _STAFF:
        if not manager_name:
            continue
        person = _employee(name)
        manager = _employee(manager_name)
        if person and manager and person.manager_id != manager.pk:
            person.manager = manager
            person.save(update_fields=['manager'])

    for employee in Employee.objects.filter(employment_status='onboarding'):
        created += _seed_onboarding_for(employee)

    _flush_manifest()
    return created


def _seed_onboarding_for(employee):
    """Resolve the checklist template against one starter's start date.

    The offset is the reusable rule and the date is the resolved instance of
    it, which is exactly the split OnboardingTask was designed for: a start
    date that moves should move the dates and leave the checklist alone.
    """
    hr = _agents().get('hr')
    start = employee.start_date or _today()
    created = 0

    for order, (title, category, owner_role, offset, status,
                description) in enumerate(_ONBOARDING_TEMPLATE, start=1):
        task, was_created = OnboardingTask.objects.get_or_create(
            employee=employee, title=title,
            defaults={
                'description': description,
                'category': category,
                'owner_role': owner_role,
                'due_offset_days': offset,
                'due_date': start + timedelta(days=offset),
                'status': status,
                'display_order': order,
                'created_by_agent': hr,
                'completed_at': _at(4, hour=14) if status == 'done' else None,
            },
        )
        if was_created:
            created += 1
            _remember(task)
            _backdate(task, _at(6, hour=8, minute=40))
    return created


# ===========================================================================
# 3. RECRUITMENT
# ===========================================================================

# The scoreable requirement set for each opening. Shaped exactly as the model
# documents -- {"text", "weight", "must_have"} -- because every evaluation
# below scores these lines by name, and a screening decision that cannot be
# traced back to a numbered requirement is a number out of the air.
_OPENINGS = [
    {
        'title': 'Senior Backend Engineer',
        'department': 'Engineering',
        'seniority': 'senior',
        'hiring_manager': 'Priya Raghavan',
        'status': 'open',
        'salary_min': 145000,
        'salary_max': 175000,
        'openings': 1,
        'start_offset': 45,
        'summary': (
            'We are hiring a senior backend engineer to own the planning API '
            'behind Route Optimisation v2. The work is real routing at real '
            'scale: our largest customer plans 1,400 stops across 38 vehicles '
            'every morning, and the current solver stops being fast enough at '
            'about 200 stops. You would own the service that fixes that, from '
            'the PostGIS schema underneath it to the versioned API three '
            'freight partners consume.'),
        'description': (
            'Northwind Systems builds logistics planning software for freight, '
            'cold chain and distribution operators across Australia and New '
            'Zealand. We are forty people in Melbourne, profitable, and small '
            'enough that the person who designs a schema is the person who '
            'runs it.\n\n'
            'This role sits in the Route Optimisation team alongside two '
            'backend engineers, a frontend engineer and a product designer, '
            'reporting to Priya Raghavan. You would be the most senior backend '
            'engineer on the team, which means the design decisions and the '
            'code review load both land with you.\n\n'
            'We work in two-week sprints, deploy on merge, and hold a genuine '
            'definition of done: tests, migrations, documentation and a '
            'rollback plan before anything ships. On-call is business hours '
            'only, shared across the team, and has been quiet for eleven '
            'weeks.'),
        'responsibilities': [
            'Own the planning API end to end, from the PostGIS schema to the '
            'public contract our freight partners integrate against.',
            'Take the multi-stop solver from 200 stops in eleven seconds to '
            '200 stops in under five, and prove it with a benchmark that runs '
            'in CI.',
            'Design and version the REST interfaces the driver application '
            'and partner systems depend on, including a deprecation path.',
            'Review the team code and raise the standard of it, particularly '
            'around migrations and query cost.',
            'Mentor two mid-level engineers, deliberately rather than '
            'incidentally.',
            'Share business-hours on-call for the planning service.',
        ],
        'requirements': [
            {'text': 'Five or more years building and operating production '
                     'Python services', 'weight': 5, 'must_have': True},
            {'text': 'Deep Django and Django REST Framework experience, '
                     'including migrations on live data',
             'weight': 5, 'must_have': True},
            {'text': 'PostgreSQL schema design and query tuning at scale',
             'weight': 4, 'must_have': True},
            {'text': 'Designing and versioning REST APIs consumed by third '
                     'parties', 'weight': 4, 'must_have': False},
            {'text': 'Production experience on AWS, including RDS and '
                     'container deployment', 'weight': 3, 'must_have': False},
            {'text': 'Owning automated testing and CI/CD rather than only '
                     'using it', 'weight': 3, 'must_have': True},
            {'text': 'Reviewing other engineers code and mentoring '
                     'mid-level engineers', 'weight': 3, 'must_have': False},
            {'text': 'Logistics, fleet, mapping or geospatial domain '
                     'exposure', 'weight': 2, 'must_have': False},
        ],
        'nice_to_have': [
            'PostGIS or another spatial extension in production',
            'Operations research or constraint solver experience',
            'Having been the person who carried a pager for a service you '
            'designed',
        ],
        'benefits': [
            'AUD 145,000 to 175,000 plus superannuation, reviewed every '
            'twelve months',
            'Hybrid: two days a week in the Bourke Street office, the rest '
            'wherever you work best',
            'AUD 3,000 a year for conferences, courses or books, no approval '
            'theatre',
            'Sixteen weeks paid parental leave for either parent',
            'One paid volunteering day a quarter',
        ],
    },
    {
        'title': 'Customer Support Specialist',
        'department': 'Customer Operations',
        'seniority': 'mid',
        'hiring_manager': 'Nadia Halloran',
        'status': 'open',
        'salary_min': 78000,
        'salary_max': 92000,
        'openings': 1,
        'start_offset': 30,
        'summary': (
            'Our support queue has grown from 40 tickets a week to 130 in '
            'eighteen months and two people are carrying it. We are hiring a '
            'third: somebody who can read a log line, quote a policy '
            'accurately, and write a reply a dispatcher reads once and '
            'understands. This is business software for people whose trucks '
            'are already on the road, so the standard for a wrong answer is '
            'high.'),
        'description': (
            'You would join Nadia Halloran and Joshua Kirkbride in Customer '
            'Operations, supporting 210 accounts across freight, cold chain '
            'and wholesale distribution. Most contact arrives by email, some '
            'by live chat, and a Priority 1 incident occasionally arrives by '
            'phone at six in the morning.\n\n'
            'We answer from the documented policy and nothing else. An '
            'improvised concession becomes the company obligation the moment '
            'it is sent, so the job includes knowing when the answer is "the '
            'Support Lead has to approve that" and saying so warmly.\n\n'
            'Support hours are 9.00am to 6.00pm AEST, Monday to Friday. '
            'Priority 1 cover is rostered and paid separately; you would join '
            'that roster after three months, not before.'),
        'responsibilities': [
            'Own a queue of tickets from first contact to confirmed '
            'resolution, against published response targets.',
            'Set priority from the actual impact on the customer rather than '
            'the tone of the message.',
            'Quote the refund, SLA and privacy policies accurately, and '
            'escalate the cases those policies reserve for a person.',
            'Reproduce reported faults far enough to hand engineering '
            'something they can act on.',
            'Write the troubleshooting articles for the problems you see '
            'twice.',
        ],
        'requirements': [
            {'text': 'Two or more years in a customer-facing support role',
             'weight': 5, 'must_have': True},
            {'text': 'Written English clear enough to send to a customer '
                     'without editing', 'weight': 5, 'must_have': True},
            {'text': 'Ticketing system experience -- Zendesk, Freshdesk, '
                     'Jira Service Management or similar',
             'weight': 4, 'must_have': True},
            {'text': 'Working from a documented policy rather than '
                     'improvising a concession', 'weight': 4, 'must_have': True},
            {'text': 'Comfortable reading application logs or running a '
                     'simple SQL query to confirm a fault',
             'weight': 3, 'must_have': False},
            {'text': 'B2B software support rather than consumer retail',
             'weight': 3, 'must_have': False},
            {'text': 'Available across Melbourne business hours',
             'weight': 2, 'must_have': True},
        ],
        'nice_to_have': [
            'Logistics, transport or warehouse operations background',
            'Written a knowledge base article somebody else then used',
        ],
        'benefits': [
            'AUD 78,000 to 92,000 plus superannuation',
            'Hybrid, with a hard rule that nobody covers the queue alone',
            'Paid Priority 1 roster after three months',
            'AUD 1,500 a year for training',
        ],
    },
    {
        'title': 'Marketing Coordinator',
        'department': 'Marketing',
        'seniority': 'junior',
        'hiring_manager': 'Aisha Bakhtiari',
        'status': 'open',
        'salary_min': 72000,
        'salary_max': 84000,
        'openings': 1,
        'start_offset': 38,
        'summary': (
            'Aisha is the marketing team. We are making it two. This role runs '
            'the content calendar, writes the shorter pieces, and keeps the '
            'newsletter going out on the same Thursday every fortnight. Our '
            'audience is dispatchers, fleet managers and operations directors '
            'who can tell within a sentence whether the writer has ever seen '
            'a depot.'),
        'description': (
            'Northwind Systems markets to a small, specific and fairly '
            'sceptical audience. Nothing we publish carries a number that is '
            'not sourced, which is a real constraint on the writing rather '
            'than a slogan: if the copy says on-time delivery improved by a '
            'given percentage, the brief has to name the customer study it '
            'came from.\n\n'
            'You would work directly with Aisha Bakhtiari, and closely with '
            'Customer Operations, because the best material we publish comes '
            'out of tickets somebody solved.'),
        'responsibilities': [
            'Run the content calendar across LinkedIn, the blog and the '
            'fortnightly newsletter.',
            'Write short-form copy and first drafts of longer pieces.',
            'Keep the source list for every published claim, and refuse the '
            'ones that have none.',
            'Manage the email list, its segments and its hygiene.',
            'Report monthly on what each channel actually did.',
        ],
        'requirements': [
            {'text': 'Two or more years producing B2B marketing content',
             'weight': 5, 'must_have': True},
            {'text': 'Writing for a technical or operational audience '
                     'without talking down to it', 'weight': 5,
             'must_have': True},
            {'text': 'Running a content calendar across several channels',
             'weight': 4, 'must_have': True},
            {'text': 'Email marketing platform experience, including list '
                     'segmentation and hygiene', 'weight': 3,
             'must_have': False},
            {'text': 'Sourcing every claim in published copy',
             'weight': 3, 'must_have': True},
            {'text': 'Reporting on channel performance with numbers rather '
                     'than adjectives', 'weight': 2, 'must_have': False},
        ],
        'nice_to_have': [
            'Logistics, industrial or trade sector experience',
            'Basic HTML for email templates',
        ],
        'benefits': [
            'AUD 72,000 to 84,000 plus superannuation',
            'Hybrid, two days a week in the office',
            'A named mentor outside the company, paid for',
        ],
    },
]


# Eight applicants, deliberately unequal. Two are strong, three are middling,
# two are weak, and one is excellent at a job we are not advertising. That
# spread is the point: a screening tool that returns eight scores in the
# seventies has not been tested, and the evaluation for Yuki Tanaka -- a
# genuinely outstanding engineer scoring in the forties against THIS
# requirement set -- is the one that shows the score means something specific
# rather than something general.
_CANDIDATES = [
    {
        'name': 'Ravi Chandrasekaran',
        'opening': 'Senior Backend Engineer',
        'email': 'ravi.chandrasekaran@fastmail.com.au',
        'phone': '+61 438 220 194',
        'location': 'Brunswick East, VIC',
        'current_title': 'Senior Backend Engineer',
        'current_company': 'Linfox Digital',
        'years': Decimal('8.5'),
        'status': 'interviewing',
        'source': 'application',
        'days_ago': 19,
        'skills': ['Python', 'Django', 'Django REST Framework', 'PostgreSQL',
                   'PostGIS', 'Celery', 'AWS', 'Terraform', 'GitHub Actions',
                   'pytest'],
        'resume': """Ravi Chandrasekaran
Brunswick East, VIC 3057 | +61 438 220 194 | ravi.chandrasekaran@fastmail.com.au

SUMMARY
Backend engineer with eight and a half years building Python services for
freight and field-service operators, the last four of them on routing and
dispatch at scale.

SKILLS
Python 3.12, Django, Django REST Framework, PostgreSQL, PostGIS, Celery,
Redis, AWS (ECS, RDS, Lambda, S3), Terraform, GitHub Actions, pytest,
OpenAPI, Grafana.

EXPERIENCE
Senior Backend Engineer, Linfox Digital, Melbourne. March 2021 to present.
- Rebuilt the dispatch API on Django REST Framework. Median planning time for
  a 150-stop run fell from 41 seconds to 6.
- Designed the PostGIS depot and stop schema and tuned the queries over it;
  the nightly territory rebuild went from 28 minutes to 4 after adding two
  covering indexes and removing a correlated subquery.
- Owned the versioned public API consumed by three freight partners,
  including the v1 to v2 deprecation window and the contract tests that made
  it safe.
- Ran two hundred and forty migrations against a live 900 GB database without
  a rollback, using expand-and-contract for every column change.
- Built and owned the GitHub Actions pipeline: 2,400 tests in eleven minutes,
  deploy on merge to ECS, automatic rollback on health check failure.
- Reviewed most of the team's backend pull requests and ran a fortnightly
  query-cost review that two other teams later adopted.

Backend Engineer, Aconex (Oracle Construction), Melbourne. Feb 2018 to Mar 2021.
- Django services for document control on infrastructure projects.
- Moved the reporting workload off the primary database onto a read replica,
  cutting p95 report latency by 71 per cent.
- Mentored two graduate engineers through their first year, including their
  first on-call rotations.

EDUCATION
Bachelor of Engineering (Software), RMIT University, 2017. Distinction average.

Australian citizen. Available with four weeks notice.""",
    },
    {
        'name': 'Sophie Lindqvist',
        'opening': 'Senior Backend Engineer',
        'email': 'sophie.lindqvist@protonmail.com',
        'phone': '+61 401 773 528',
        'location': 'Fitzroy North, VIC',
        'current_title': 'Backend Engineer',
        'current_company': 'Redbubble',
        'years': Decimal('7.0'),
        'status': 'shortlisted',
        'source': 'referral',
        'days_ago': 16,
        'skills': ['Python', 'Django', 'FastAPI', 'PostgreSQL', 'Google Cloud',
                   'Kubernetes', 'pytest', 'GitLab CI'],
        'resume': """Sophie Lindqvist
Fitzroy North, VIC 3068 | +61 401 773 528 | sophie.lindqvist@protonmail.com

SUMMARY
Seven years of Python backend work, most of it on high-traffic marketplace
systems. Referred by Daniel Okafor.

SKILLS
Python, Django, Django REST Framework, FastAPI, PostgreSQL, Redis, Google
Cloud Platform (GKE, Cloud SQL, Pub/Sub), Kubernetes, Terraform, pytest,
GitLab CI, OpenTelemetry.

EXPERIENCE
Backend Engineer, Redbubble, Melbourne. July 2020 to present.
- Owned the order fulfilment service: Django, PostgreSQL, 3.2 million orders
  a year across sixteen print partners.
- Redesigned the order state machine after a bad peak season; duplicate
  fulfilment requests fell from roughly 400 a week to under 10.
- Designed and shipped the partner-facing REST API, versioned from the start,
  with a published OpenAPI schema and a six-month deprecation policy.
- Tuned the PostgreSQL schema behind order search: partitioned by month,
  replaced four ad hoc indexes with two composite ones, cut p95 search from
  2.1 seconds to 240 milliseconds.
- Wrote the test suite for the fulfilment domain from 34 per cent to 88 per
  cent statement coverage and made it a merge gate on GitLab CI.

Software Engineer, Culture Amp, Melbourne. Jan 2019 to July 2020.
- Django and Python services for survey delivery.
- Built the export pipeline still used for customer reporting.

Junior Developer, Sportsbet, Melbourne. Feb 2018 to Dec 2018.
- Python reporting jobs and internal tooling.

EDUCATION
Master of Information Technology, University of Melbourne, 2017.
Bachelor of Science (Mathematics), Lund University, Sweden, 2015.

Permanent resident. Available immediately.""",
    },
    {
        'name': 'Ben Trowbridge',
        'opening': 'Senior Backend Engineer',
        'email': 'ben.trowbridge@gmail.com',
        'phone': '+61 422 106 337',
        'location': 'Geelong, VIC',
        'current_title': 'Python Developer',
        'current_company': 'Barwon Water',
        'years': Decimal('4.0'),
        'status': 'screening',
        'source': 'application',
        'days_ago': 13,
        'skills': ['Python', 'Flask', 'Django', 'MySQL', 'Docker', 'Jenkins'],
        'resume': """Ben Trowbridge
Geelong, VIC 3220 | +61 422 106 337 | ben.trowbridge@gmail.com

SUMMARY
Python developer with four years of experience across internal web
applications and data integration work. Looking to move into a product
engineering team.

SKILLS
Python, Flask, some Django, MySQL, a little PostgreSQL, Docker, Jenkins,
pandas, Bash, JavaScript.

EXPERIENCE
Python Developer, Barwon Water, Geelong. August 2022 to present.
- Built and maintained six internal Flask applications for asset inspection
  and meter data reconciliation, used by about 90 field staff.
- Wrote the nightly integration between the asset register and the SCADA
  historian in Python; it has run without intervention for two years.
- Added the first automated tests the team had, roughly 200 of them, and set
  them running on a Jenkins job somebody else configured.
- Took over one small Django application when the original developer left and
  kept it running, including two upgrades.
- Handled the on-call phone for the internal applications one week in four.

Junior Developer, Deakin University IT Services, Geelong. Jan 2021 to Aug 2022.
- Python scripting and reporting for student systems.
- Maintained MySQL queries behind the enrolment dashboards.

EDUCATION
Bachelor of Information Technology, Deakin University, 2020.

Australian citizen. Available with two weeks notice. Happy to be in Melbourne
two days a week; the commute from Geelong is about an hour each way.""",
    },
    {
        'name': 'Yuki Tanaka',
        'opening': 'Senior Backend Engineer',
        'email': 'yuki.tanaka@outlook.com.au',
        'phone': '+61 466 812 705',
        'location': 'Docklands, VIC',
        'current_title': 'Principal Data Scientist',
        'current_company': 'Australia Post',
        'years': Decimal('11.0'),
        'status': 'rejected',
        'source': 'sourced',
        'days_ago': 15,
        'skills': ['Python', 'operations research', 'OR-Tools', 'PyTorch',
                   'NumPy', 'BigQuery', 'SQL', 'forecasting'],
        'resume': """Yuki Tanaka
Docklands, VIC 3008 | +61 466 812 705 | yuki.tanaka@outlook.com.au

SUMMARY
Principal data scientist, eleven years, specialising in vehicle routing and
network optimisation for postal and parcel networks. Published on
capacitated vehicle routing with time windows.

SKILLS
Python, OR-Tools, Gurobi, NumPy, SciPy, PyTorch, pandas, BigQuery, SQL, R,
Airflow. Some Flask for model-serving endpoints.

EXPERIENCE
Principal Data Scientist, Australia Post, Melbourne. May 2019 to present.
- Led the optimisation team that replans the metropolitan parcel network
  daily: 4,300 vehicles, 2.1 million delivery points.
- Built the capacitated vehicle routing solver now used across four states.
  Modelled travel time from historical telematics rather than road speed
  limits, which cut planned-versus-actual variance by 34 per cent.
- Reduced total planned kilometres by 9.2 per cent in the first year, an
  audited saving of AUD 14.6 million.
- Served models behind small Flask endpoints; production hardening, database
  design and API versioning were handled by a separate platform team.

Senior Data Scientist, Toll Group, Melbourne. Feb 2015 to Apr 2019.
- Demand forecasting and depot placement models.
- Simulation of network changes before capital commitment.

Operations Research Analyst, Japan Post, Tokyo. 2014 to 2015.

EDUCATION
PhD, Operations Research, University of Melbourne, 2014. Thesis on
capacitated vehicle routing with stochastic travel times.
Bachelor of Engineering, University of Tokyo, 2009.

PUBLICATIONS
Six peer-reviewed papers on vehicle routing. Reviewer for Transportation
Science.

Australian citizen. Note: I have not owned a production web service or a
relational schema; my work sits behind them.""",
    },
    {
        'name': 'Aaron Whitlow',
        'opening': 'Senior Backend Engineer',
        'email': 'aaron.whitlow@yahoo.com.au',
        'phone': '+61 434 559 026',
        'location': 'Werribee, VIC',
        'current_title': 'Web Developer',
        'current_company': 'Freelance',
        'years': Decimal('1.5'),
        'status': 'withdrawn',
        'source': 'application',
        'days_ago': 11,
        'skills': ['PHP', 'WordPress', 'HTML', 'CSS', 'jQuery', 'some Python'],
        'resume': """Aaron Whitlow
Werribee, VIC 3030 | +61 434 559 026 | aaron.whitlow@yahoo.com.au

SUMMARY
Career changer, eighteen months into web development after eight years in
warehouse operations. Keen to work on logistics software because I have
actually run a pick face.

SKILLS
PHP, WordPress, HTML, CSS, jQuery, MySQL basics, some Python (completed a
twelve-week bootcamp), Git.

EXPERIENCE
Freelance Web Developer, Werribee. March 2025 to present.
- Built and maintain eleven small business WordPress sites, mostly for local
  trades and one transport broker.
- Wrote a PHP plugin that pulls consignment tracking numbers into a client
  site from a carrier CSV feed.
- Handle hosting, backups and support for all eleven clients myself.

Warehouse Supervisor, Metcash, Laverton. 2017 to 2025.
- Supervised a team of fourteen on the night pick shift.
- Ran the cycle count programme; stock accuracy went from 94.1 to 99.3 per
  cent over two years.
- Was the person who called the software vendor when the wave planner broke,
  which is how I got interested in this.

EDUCATION
Certificate IV in Information Technology, Victoria University, 2024.
Coder Academy full-stack bootcamp, twelve weeks, 2025.

Australian citizen. Available immediately. I know I am light on the Python and
the databases; I learn fast and I understand the customers.""",
    },
    {
        'name': 'Grace Mbeki',
        'opening': 'Customer Support Specialist',
        'email': 'grace.mbeki@gmail.com',
        'phone': '+61 429 887 214',
        'location': 'Preston, VIC',
        'current_title': 'Support Consultant',
        'current_company': 'MYOB',
        'years': Decimal('3.5'),
        'status': 'offer',
        'source': 'application',
        'days_ago': 17,
        'skills': ['Zendesk', 'customer communication', 'SQL basics',
                   'policy interpretation', 'knowledge base writing'],
        'resume': """Grace Mbeki
Preston, VIC 3072 | +61 429 887 214 | grace.mbeki@gmail.com

SUMMARY
Three and a half years supporting small-business accounting software.
Comfortable with a queue, a policy and an upset customer in the same hour.

SKILLS
Zendesk, Jira Service Management, basic SQL, written customer communication,
knowledge base authoring, refund and billing policy interpretation.

EXPERIENCE
Support Consultant, MYOB, Melbourne. February 2023 to present.
- Own a queue of about 45 tickets a week across billing, payroll compliance
  and data import problems, for accounting practices and their clients.
- Held first response within target on 96 per cent of tickets over the last
  two quarters.
- Wrote fourteen knowledge base articles, four of which are in the top twenty
  most-viewed; the single-touch payroll import one deflects roughly 90
  tickets a month.
- Run simple SQL SELECT queries against the reporting replica to confirm
  whether a customer's import genuinely failed before replying, rather than
  asking them to try again.
- Quote the refund policy and the subscription terms directly and escalate
  out-of-policy requests to the team lead; I do not offer what I cannot
  approve.
- Trained two new starters on the queue, including how to set priority from
  impact rather than tone.

Customer Service Officer, Bank of Melbourne, Melbourne. Jan 2022 to Feb 2023.
- Branch and phone enquiries, complaints handling, hardship referrals.

EDUCATION
Bachelor of Commerce, La Trobe University, 2021.

Australian citizen. Available with three weeks notice. Melbourne based, happy
with 9 to 6 AEST.""",
    },
    {
        'name': 'Dylan Cassar',
        'opening': 'Customer Support Specialist',
        'email': 'dylan.cassar@hotmail.com',
        'phone': '+61 415 330 872',
        'location': 'Sunshine, VIC',
        'current_title': 'Retail Team Member',
        'current_company': 'JB Hi-Fi',
        'years': Decimal('2.0'),
        'status': 'new',
        'source': 'application',
        'days_ago': 4,
        'skills': ['customer service', 'retail sales', 'point of sale'],
        'resume': """Dylan Cassar
Sunshine, VIC 3020 | +61 415 330 872 | dylan.cassar@hotmail.com

SUMMARY
Two years of retail customer service, looking for a first office role in
technology support.

SKILLS
Face to face customer service, point of sale systems, phone enquiries, cash
handling, rostering, basic Microsoft Office.

EXPERIENCE
Retail Team Member, JB Hi-Fi, Highpoint. October 2024 to present.
- Serve customers on the computing floor, average 60 to 80 interactions a
  shift.
- Handle returns and warranty claims under the store policy and escalate the
  ones outside it to the duty manager.
- Set up laptops and phones for customers, including data transfer.
- Was the store's highest accessory attachment rate for two quarters.

Crew Member, Grill'd, Watergardens. February 2024 to October 2024.
- Front counter and order accuracy.

EDUCATION
Victorian Certificate of Education, Sunshine College, 2023.
Currently enrolled part time in a Diploma of Information Technology, Victoria
University, expected 2027.

Australian citizen. Available immediately, full time, any hours. I have not
used a ticketing system but I pick things up quickly and I have never had a
complaint escalated about my service.""",
    },
    {
        'name': 'Isabelle Fournier',
        'opening': 'Marketing Coordinator',
        'email': 'isabelle.fournier@gmail.com',
        'phone': '+61 407 662 199',
        'location': 'South Yarra, VIC',
        'current_title': 'Content Executive',
        'current_company': 'Brightline Agency',
        'years': Decimal('2.5'),
        'status': 'screening',
        'source': 'agency',
        'days_ago': 9,
        'skills': ['copywriting', 'social media', 'Canva', 'Mailchimp',
                   'content calendars'],
        'resume': """Isabelle Fournier
South Yarra, VIC 3141 | +61 407 662 199 | isabelle.fournier@gmail.com

SUMMARY
Content executive with two and a half years at a Melbourne agency, mostly
consumer brands. Wanting to move client side and into B2B.

SKILLS
Copywriting, social media scheduling (Later, Hootsuite), Mailchimp, Canva,
basic Google Analytics, content calendars, influencer coordination.

EXPERIENCE
Content Executive, Brightline Agency, Melbourne. April 2024 to present.
- Run the content calendars for five consumer accounts across Instagram,
  TikTok and Facebook -- a skincare brand, two hospitality groups, a gym
  franchise and a pet food label.
- Write roughly 40 pieces of short-form copy a month plus two blog posts.
- Build and send the fortnightly newsletters for three of those accounts in
  Mailchimp, including segment setup and list clean-ups before each send.
- Report monthly on reach, engagement rate and click-through by channel.
- Won the agency's internal copy award in 2025 for a hospitality campaign.

Marketing Assistant, Brightline Agency, Melbourne. Jan 2024 to Apr 2024.
- Scheduling, asset management and reporting support.

Intern, Melbourne Fringe Festival, 2023.
- Social media and programme copy.

EDUCATION
Bachelor of Communication (Public Relations), RMIT University, 2023.

Australian citizen. Available with four weeks notice. I have not written for a
technical audience before, but I read the Northwind blog before applying and I
think I understand who it is for.""",
    },
]


# THE MOST IMPORTANT DATA IN THIS FILE.
#
# Every entry scores each requirement individually, with the evidence quoted
# from that candidate's own resume text above -- verbatim, so a reader can find
# the sentence. That is the whole difference between a screening tool and a
# random number generator: the total is derived from the lines, the lines are
# derived from quotes, and a candidate who disagrees with their score can be
# shown exactly which requirement they lost and on what evidence.
#
# The totals are NOT written here. They are computed from the breakdown when
# the row is created, so the number on the page can never drift away from the
# lines that justify it.
_EVALUATIONS = [
    {
        'candidate': 'Ravi Chandrasekaran',
        'recommendation': 'advance',
        'days_ago': 18,
        'breakdown': [
            {'requirement': 'Five or more years building and operating '
                            'production Python services',
             'score': 5, 'max': 5,
             'evidence': '"eight and a half years building Python services for '
                         'freight and field-service operators, the last four of '
                         'them on routing and dispatch at scale" -- comfortably '
                         'past the threshold, and in our own domain.'},
            {'requirement': 'Deep Django and Django REST Framework experience, '
                            'including migrations on live data',
             'score': 5, 'max': 5,
             'evidence': '"Rebuilt the dispatch API on Django REST Framework" '
                         'and "two hundred and forty migrations against a live '
                         '900 GB database without a rollback, using '
                         'expand-and-contract for every column change". The '
                         'second quote is the one that matters; most senior '
                         'resumes claim the first.'},
            {'requirement': 'PostgreSQL schema design and query tuning at scale',
             'score': 4, 'max': 4,
             'evidence': '"the nightly territory rebuild went from 28 minutes '
                         'to 4 after adding two covering indexes and removing a '
                         'correlated subquery" -- a named cause and a measured '
                         'effect, not an adjective.'},
            {'requirement': 'Designing and versioning REST APIs consumed by '
                            'third parties',
             'score': 3.5, 'max': 4,
             'evidence': '"Owned the versioned public API consumed by three '
                         'freight partners, including the v1 to v2 deprecation '
                         'window and the contract tests that made it safe." '
                         'Half a point withheld: he inherited that contract '
                         'rather than designing it, on this evidence.'},
            {'requirement': 'Production experience on AWS, including RDS and '
                            'container deployment',
             'score': 3, 'max': 3,
             'evidence': '"AWS (ECS, RDS, Lambda, S3)" in the skills list, '
                         'corroborated by "deploy on merge to ECS, automatic '
                         'rollback on health check failure".'},
            {'requirement': 'Owning automated testing and CI/CD rather than '
                            'only using it',
             'score': 3, 'max': 3,
             'evidence': '"Built and owned the GitHub Actions pipeline: 2,400 '
                         'tests in eleven minutes, deploy on merge to ECS." '
                         'Built and owned, not used.'},
            {'requirement': 'Reviewing other engineers code and mentoring '
                            'mid-level engineers',
             'score': 2, 'max': 3,
             'evidence': '"Reviewed most of the team backend pull requests and '
                         'ran a fortnightly query-cost review that two other '
                         'teams later adopted." Review is well evidenced; the '
                         'only explicit mentoring is at the previous employer '
                         '("Mentored two graduate engineers"), so this is not '
                         'full marks on current practice.'},
            {'requirement': 'Logistics, fleet, mapping or geospatial domain '
                            'exposure',
             'score': 2, 'max': 2,
             'evidence': '"Designed the PostGIS depot and stop schema" at a '
                         'freight operator. This is the exact problem shape of '
                         'Route Optimisation v2.'},
        ],
        'strengths': [
            'The only applicant who has personally run migrations against a '
            'large live database and said how.',
            'PostGIS and depot/stop modelling already, so the domain ramp is '
            'close to zero.',
            'Owns his CI pipeline rather than inheriting it, which is the '
            'habit this team is missing.',
            'Every claim on the resume carries a number and a mechanism.',
        ],
        'gaps': [
            'Mentoring evidence is from the previous role, not the current '
            'one. Worth probing.',
            'No evidence he has designed a third-party API contract from '
            'nothing, only versioned an existing one.',
            'Four weeks notice against a target start in six weeks -- tight '
            'but workable.',
        ],
        'rationale': (
            'Advance to a technical interview. He clears every must-have '
            'requirement on quoted evidence rather than on self-description, '
            'and two of the requirements -- live-data migrations and PostGIS '
            'depot modelling -- he is the only applicant to evidence at all. '
            'The score is not full marks for two honest reasons: the '
            'third-party API contract appears to have been inherited, and the '
            'mentoring he describes is from Aconex rather than Linfox. Both '
            'are interview questions, not objections. Recommend Priya '
            'Raghavan and Daniel Okafor interview, and that the interview '
            'spend its time on how he would take the solver from eleven '
            'seconds to five rather than on whether he can write Django.'),
    },
    {
        'candidate': 'Sophie Lindqvist',
        'recommendation': 'advance',
        'days_ago': 15,
        'breakdown': [
            {'requirement': 'Five or more years building and operating '
                            'production Python services',
             'score': 5, 'max': 5,
             'evidence': '"Seven years of Python backend work, most of it on '
                         'high-traffic marketplace systems", with three named '
                         'employers accounting for the period.'},
            {'requirement': 'Deep Django and Django REST Framework experience, '
                            'including migrations on live data',
             'score': 4, 'max': 5,
             'evidence': '"Owned the order fulfilment service: Django, '
                         'PostgreSQL, 3.2 million orders a year" -- Django '
                         'depth is clear. One point withheld because nothing '
                         'in the resume mentions migrating live data, which is '
                         'the half of this requirement that goes wrong at '
                         'three in the morning.'},
            {'requirement': 'PostgreSQL schema design and query tuning at scale',
             'score': 4, 'max': 4,
             'evidence': '"partitioned by month, replaced four ad hoc indexes '
                         'with two composite ones, cut p95 search from 2.1 '
                         'seconds to 240 milliseconds". Strongest tuning '
                         'evidence of any applicant, Ravi included.'},
            {'requirement': 'Designing and versioning REST APIs consumed by '
                            'third parties',
             'score': 4, 'max': 4,
             'evidence': '"Designed and shipped the partner-facing REST API, '
                         'versioned from the start, with a published OpenAPI '
                         'schema and a six-month deprecation policy." Designed '
                         'from the start, which is more than Ravi evidences.'},
            {'requirement': 'Production experience on AWS, including RDS and '
                            'container deployment',
             'score': 1, 'max': 3,
             'evidence': '"Google Cloud Platform (GKE, Cloud SQL, Pub/Sub)". '
                         'The concepts transfer and the container experience '
                         'is real, but this requirement names AWS and there is '
                         'no AWS anywhere in the resume. One point for '
                         'transferable cloud operations.'},
            {'requirement': 'Owning automated testing and CI/CD rather than '
                            'only using it',
             'score': 3, 'max': 3,
             'evidence': '"Wrote the test suite for the fulfilment domain from '
                         '34 per cent to 88 per cent statement coverage and '
                         'made it a merge gate on GitLab CI." She changed the '
                         'gate, not just the tests.'},
            {'requirement': 'Reviewing other engineers code and mentoring '
                            'mid-level engineers',
             'score': 1, 'max': 3,
             'evidence': 'No evidence anywhere in the resume. Not a claim '
                         'either way -- the resume simply does not mention '
                         'review or mentoring. One point given because seven '
                         'years on a team implies some, and this is the '
                         'largest single thing the interview needs to '
                         'establish.'},
            {'requirement': 'Logistics, fleet, mapping or geospatial domain '
                            'exposure',
             'score': 0.5, 'max': 2,
             'evidence': '"3.2 million orders a year across sixteen print '
                         'partners" is fulfilment logistics of a kind, but '
                         'there is no vehicle, route or spatial work. Half a '
                         'point for fulfilment adjacency.'},
        ],
        'strengths': [
            'Best evidenced query tuning in the pool, with the numbers to '
            'support it.',
            'Designed a versioned partner API from the start, including the '
            'deprecation policy -- exactly the artefact this role owns.',
            'Made testing a merge gate, which is a cultural act rather than a '
            'technical one.',
            'Internal referral from Daniel Okafor, who would be her closest '
            'colleague.',
        ],
        'gaps': [
            'No AWS at all. Google Cloud instead, so the transfer is real but '
            'the first month would be slower.',
            'Nothing about migrating live data, which this role does often.',
            'No mentoring or code review evidence, and this is the senior seat '
            'on the team.',
        ],
        'rationale': (
            'Advance, with the interview deliberately weighted towards the '
            'two gaps. She is stronger than Ravi on API design and on query '
            'tuning and weaker on everything to do with operating a service '
            'somebody else has to keep running: no AWS, no live migrations, no '
            'evidence of review or mentoring. For the most senior backend seat '
            'on a five-person team, those are the expensive gaps rather than '
            'the cheap ones, which is why she scores below Ravi despite the '
            'better tuning evidence. Interview should ask her to describe the '
            'worst migration she has run and who reviewed it.'),
    },
    {
        'candidate': 'Ben Trowbridge',
        'recommendation': 'hold',
        'days_ago': 12,
        'breakdown': [
            {'requirement': 'Five or more years building and operating '
                            'production Python services',
             'score': 2.5, 'max': 5,
             'evidence': '"four years of experience across internal web '
                         'applications and data integration work". Four '
                         'against a stated five, and internal tools rather '
                         'than a product. This is a must-have requirement and '
                         'it is not met.'},
            {'requirement': 'Deep Django and Django REST Framework experience, '
                            'including migrations on live data',
             'score': 2, 'max': 5,
             'evidence': '"some Django" in the skills list and "Took over one '
                         'small Django application when the original developer '
                         'left and kept it running, including two upgrades". '
                         'Maintaining one small application is not depth, and '
                         'there is no mention of Django REST Framework at all.'},
            {'requirement': 'PostgreSQL schema design and query tuning at scale',
             'score': 2, 'max': 4,
             'evidence': '"MySQL, a little PostgreSQL" and "Maintained MySQL '
                         'queries behind the enrolment dashboards". Real SQL '
                         'work, wrong engine, and nothing about designing a '
                         'schema or tuning at scale.'},
            {'requirement': 'Designing and versioning REST APIs consumed by '
                            'third parties',
             'score': 0.5, 'max': 4,
             'evidence': '"Wrote the nightly integration between the asset '
                         'register and the SCADA historian in Python" is '
                         'integration work, but nothing external consumes an '
                         'interface he designed.'},
            {'requirement': 'Production experience on AWS, including RDS and '
                            'container deployment',
             'score': 0, 'max': 3,
             'evidence': 'No cloud provider appears anywhere in the resume. '
                         'Docker is listed; AWS, GCP and Azure are not.'},
            {'requirement': 'Owning automated testing and CI/CD rather than '
                            'only using it',
             'score': 2.5, 'max': 3,
             'evidence': '"Added the first automated tests the team had, '
                         'roughly 200 of them" -- that is ownership, and it is '
                         'the most impressive line on the resume. Half a point '
                         'withheld for "set them running on a Jenkins job '
                         'somebody else configured".'},
            {'requirement': 'Reviewing other engineers code and mentoring '
                            'mid-level engineers',
             'score': 0.5, 'max': 3,
             'evidence': 'No review or mentoring is described. He appears to '
                         'have been the only developer on most of this work, '
                         'which explains the absence without excusing it for a '
                         'senior seat.'},
            {'requirement': 'Logistics, fleet, mapping or geospatial domain '
                            'exposure',
             'score': 1, 'max': 2,
             'evidence': '"six internal Flask applications for asset '
                         'inspection and meter data reconciliation, used by '
                         'about 90 field staff" -- field operations software '
                         'with a spatial dimension, at a utility. Adjacent '
                         'rather than the same.'},
        ],
        'strengths': [
            'Introduced automated testing to a team that had none, which is '
            'an act of initiative rather than a skill.',
            'Two years of an unattended nightly integration running without '
            'intervention says something real about his care.',
            'Field operations software for 90 staff is closer to our customers '
            'than a marketplace is.',
            'Carried the on-call phone one week in four.',
        ],
        'gaps': [
            'Four years against a five-year must-have, and internal tools '
            'rather than a product.',
            'Django is incidental, Django REST Framework absent.',
            'No cloud experience of any kind.',
            'No code review or mentoring, which is half of what this seat is '
            'for.',
        ],
        'rationale': (
            'Hold, and the honest recommendation is a different conversation. '
            'Against the Senior Backend Engineer requirement set he fails two '
            'of the four must-haves, and the score reflects that rather than '
            'reflecting his ability: a four-year developer who introduced the '
            'first tests his team ever had is a good mid-level hire and a poor '
            'senior one. Do not advance him to this interview loop, because '
            'interviewing somebody for a seat they cannot hold wastes their '
            'time more than ours. Worth asking Priya whether the team would '
            'take a mid-level engineer alongside the senior hire, and if so, '
            'Ben should be first on that list.'),
    },
    {
        'candidate': 'Yuki Tanaka',
        'recommendation': 'reject',
        'days_ago': 14,
        'breakdown': [
            {'requirement': 'Five or more years building and operating '
                            'production Python services',
             'score': 3, 'max': 5,
             'evidence': 'Eleven years of Python, but "Served models behind '
                         'small Flask endpoints; production hardening, '
                         'database design and API versioning were handled by a '
                         'separate platform team". The Python is not in doubt; '
                         'operating a production service is explicitly '
                         'somebody else job.'},
            {'requirement': 'Deep Django and Django REST Framework experience, '
                            'including migrations on live data',
             'score': 0, 'max': 5,
             'evidence': 'Django does not appear anywhere in the resume. This '
                         'is a must-have requirement and the evidence for it '
                         'is nil.'},
            {'requirement': 'PostgreSQL schema design and query tuning at scale',
             'score': 1, 'max': 4,
             'evidence': '"BigQuery, SQL, R" and heavy analytical query work, '
                         'against an explicit statement that "I have not '
                         'owned a production web service or a relational '
                         'schema". Analytical SQL is not schema design.'},
            {'requirement': 'Designing and versioning REST APIs consumed by '
                            'third parties',
             'score': 0.5, 'max': 4,
             'evidence': '"Some Flask for model-serving endpoints" and nothing '
                         'about versioning or external consumers.'},
            {'requirement': 'Production experience on AWS, including RDS and '
                            'container deployment',
             'score': 1, 'max': 3,
             'evidence': 'BigQuery and Airflow imply cloud data platforms, but '
                         'no AWS, no RDS and no container deployment is '
                         'described.'},
            {'requirement': 'Owning automated testing and CI/CD rather than '
                            'only using it',
             'score': 1, 'max': 3,
             'evidence': 'No testing or CI/CD appears in the resume. One point '
                         'because an audited AUD 14.6 million saving is not '
                         'produced by untested code.'},
            {'requirement': 'Reviewing other engineers code and mentoring '
                            'mid-level engineers',
             'score': 3, 'max': 3,
             'evidence': '"Led the optimisation team that replans the '
                         'metropolitan parcel network daily" and "Reviewer for '
                         'Transportation Science". Full marks; this is the one '
                         'requirement she is over-qualified for.'},
            {'requirement': 'Logistics, fleet, mapping or geospatial domain '
                            'exposure',
             'score': 2, 'max': 2,
             'evidence': '"Built the capacitated vehicle routing solver now '
                         'used across four states" for 4,300 vehicles. This is '
                         'the strongest domain evidence the company has ever '
                         'received from an applicant.'},
        ],
        'strengths': [
            'World-class at the actual mathematics of the problem Route '
            'Optimisation v2 exists to solve.',
            'Reduced planned kilometres by 9.2 per cent for a national parcel '
            'network, audited at AUD 14.6 million.',
            'Led a team and reviews for an academic journal.',
            'Six peer-reviewed papers on vehicle routing.',
        ],
        'gaps': [
            'No Django whatsoever, which is a must-have.',
            'Explicitly states she has never owned a production web service or '
            'a relational schema.',
            'No API design, no CI/CD, no container deployment.',
        ],
        'rationale': (
            'Do not advance for THIS role, and this evaluation should be read '
            'as a criticism of the vacancy rather than of the candidate. She '
            'is the most accomplished applicant by a wide margin and she '
            'scores in the thirties, because the requirement set measures '
            'building and operating a Django service and she has spent eleven '
            'years doing something adjacent and harder. She says so herself in '
            'the last line of her resume, which is to her credit.\n\n'
            'The right action is not a rejection email. It is a conversation '
            'with Priya Raghavan about whether the OR-Tools spike on the Route '
            'Optimisation board should be a contract engagement, and whether '
            'there is a solver role here that does not exist yet. Escalating '
            'to the hiring manager rather than sending the standard note.'),
    },
    {
        'candidate': 'Grace Mbeki',
        'recommendation': 'advance',
        'days_ago': 16,
        'breakdown': [
            {'requirement': 'Two or more years in a customer-facing support '
                            'role',
             'score': 5, 'max': 5,
             'evidence': '"Three and a half years supporting small-business '
                         'accounting software", plus a year of branch and '
                         'phone work before it.'},
            {'requirement': 'Written English clear enough to send to a '
                            'customer without editing',
             'score': 4, 'max': 5,
             'evidence': '"Wrote fourteen knowledge base articles, four of '
                         'which are in the top twenty most-viewed; the '
                         'single-touch payroll import one deflects roughly 90 '
                         'tickets a month." Published writing that measurably '
                         'worked. One point withheld only because knowledge '
                         'base prose and a reply to an angry dispatcher are '
                         'different registers, and we have a sample of one of '
                         'them.'},
            {'requirement': 'Ticketing system experience -- Zendesk, '
                            'Freshdesk, Jira Service Management or similar',
             'score': 4, 'max': 4,
             'evidence': '"Zendesk, Jira Service Management" in the skills '
                         'list, corroborated by "Own a queue of about 45 '
                         'tickets a week".'},
            {'requirement': 'Working from a documented policy rather than '
                            'improvising a concession',
             'score': 4, 'max': 4,
             'evidence': '"Quote the refund policy and the subscription terms '
                         'directly and escalate out-of-policy requests to the '
                         'team lead; I do not offer what I cannot approve." '
                         'That sentence is the requirement, in her own words, '
                         'unprompted.'},
            {'requirement': 'Comfortable reading application logs or running a '
                            'simple SQL query to confirm a fault',
             'score': 2.5, 'max': 3,
             'evidence': '"Run simple SQL SELECT queries against the reporting '
                         'replica to confirm whether a customer import '
                         'genuinely failed before replying." SQL yes, logs not '
                         'mentioned.'},
            {'requirement': 'B2B software support rather than consumer retail',
             'score': 1.5, 'max': 3,
             'evidence': 'MYOB support is for "accounting practices and their '
                         'clients", which is business software, but the '
                         'failure mode is a wrong number in a ledger rather '
                         'than eighteen trucks waiting on a gate. Half marks '
                         'for the right shape at a lower stake.'},
            {'requirement': 'Available across Melbourne business hours',
             'score': 2, 'max': 2,
             'evidence': '"Melbourne based, happy with 9 to 6 AEST."'},
        ],
        'strengths': [
            'Volunteers the policy discipline this role is built around, '
            'without being asked.',
            'Verifies a fault with a query before replying, which is the habit '
            'that keeps a queue honest.',
            'Knowledge base articles with measured deflection -- she has '
            'already done the "write it once it happens twice" part of the '
            'job.',
            '96 per cent first response attainment over two quarters.',
        ],
        'gaps': [
            'Accounting software rather than operational software; the '
            'consequence of a wrong answer here is larger.',
            'No log reading, only SQL.',
            'Three weeks notice.',
        ],
        'rationale': (
            'Advance, and Nadia should meet her this week. She is the only '
            'support applicant who describes working from a policy and '
            'verifying before replying, which are the two behaviours that '
            'actually predict whether a support hire is safe with our refund '
            'window. The two points she loses are honest: her domain is lower '
            'stakes than ours and she has not read a log. Both are teachable '
            'inside a month, unlike either of the behaviours she already has. '
            'An offer at the mid-point of the band is defensible on this '
            'evidence.'),
    },
]


_INTERVIEW_QUESTIONS_TECHNICAL = [
    {'question': 'Walk me through the last migration you ran against a large '
                 'live table. What was the plan, and what was the rollback?',
     'competency': 'Operating a service somebody depends on',
     'what_to_look_for': 'Expand and contract, or a named equivalent. A '
                         'candidate who describes taking a lock on a large '
                         'table without mentioning duration has not done this '
                         'at scale.'},
    {'question': 'Our solver plans 200 stops in about eleven seconds and we '
                 'need five. Where would you look first, and what would you '
                 'refuse to do?',
     'competency': 'Performance diagnosis under a constraint',
     'what_to_look_for': 'Measure before changing. Look for somebody who asks '
                         'what the eleven seconds is made of before proposing '
                         'anything, and who names a thing they would not do '
                         '-- rewriting in another language, caching the '
                         'answer -- and says why.'},
    {'question': 'You are asked to add a field to a public API three freight '
                 'partners consume. Describe the whole change.',
     'competency': 'API design and versioning',
     'what_to_look_for': 'Additive change, no version bump needed, schema '
                         'published, contract tests updated, partners notified '
                         'anyway. A candidate who reaches for v3 for an '
                         'additive field does not understand the cost they '
                         'impose on integrators.'},
    {'question': 'A mid-level engineer sends you a pull request that works and '
                 'that you would have written differently. What do you do?',
     'competency': 'Review and mentoring',
     'what_to_look_for': 'Distinguishes correctness from preference, and says '
                         'out loud which of the two a comment is. The wrong '
                         'answer is any answer where the engineer ends up '
                         'writing the reviewer code.'},
    {'question': 'Tell me about a production incident you caused.',
     'competency': 'Honesty and operational judgement',
     'what_to_look_for': 'A specific incident, a mechanism, and a change that '
                         'followed. "I cannot think of one" after eight years '
                         'is itself the answer.'},
]

_EVALUATION_FORM_TECHNICAL = [
    {'criterion': 'Django and relational depth',
     'scale': '1 to 5',
     'guidance': '5 means they have designed a schema and migrated it live '
                 'under load. 3 means they have worked competently inside '
                 'somebody else schema.'},
    {'criterion': 'Performance reasoning',
     'scale': '1 to 5',
     'guidance': 'Did they measure before proposing? A 5 measures first and '
                 'names what they would not do.'},
    {'criterion': 'API and contract thinking',
     'scale': '1 to 5',
     'guidance': '5 treats an external consumer cost as a real constraint on '
                 'their own design freedom.'},
    {'criterion': 'Review and mentoring',
     'scale': '1 to 5',
     'guidance': 'This is the senior seat. A 3 or below here is a reason not '
                 'to hire, regardless of the technical score.'},
    {'criterion': 'Operational honesty',
     'scale': '1 to 5',
     'guidance': 'Did they describe a failure they caused, plainly, with what '
                 'changed afterwards?'},
]

_INTERVIEW_QUESTIONS_SUPPORT = [
    {'question': 'A customer asks for a refund 44 days after purchase. Our '
                 'window is 30 days. What do you send?',
     'competency': 'Policy discipline',
     'what_to_look_for': 'Declines warmly, states the window, names the one '
                         'exception route (the Support Lead may approve where '
                         'the delay was the company fault), and does not '
                         'promise it. Anybody who grants it has failed the '
                         'question.'},
    {'question': 'Two tickets arrive at once. One is furious about a delivery '
                 'that ran two hours late. One is calm and says some '
                 'historical runs have disappeared. Which do you pick up?',
     'competency': 'Priority from impact rather than tone',
     'what_to_look_for': 'The calm one, immediately, and an explanation that '
                         'possible data loss outranks a late delivery no '
                         'matter how the message is written.'},
    {'question': 'A dispatcher says the export "just spins". What do you ask, '
                 'and what do you check?',
     'competency': 'Fault reproduction',
     'what_to_look_for': 'Date range, account size, browser, whether it has '
                         'ever worked. Then a check rather than a guess. Bonus '
                         'for asking whether other customers have reported the '
                         'same shape.'},
    {'question': 'You do not know the answer and the customer is waiting. '
                 'What do you write?',
     'competency': 'Written communication under uncertainty',
     'what_to_look_for': 'Says what is known, what is not, what happens next '
                         'and when. Never invents a timeframe.'},
]

_EVALUATION_FORM_SUPPORT = [
    {'criterion': 'Policy accuracy',
     'scale': '1 to 5',
     'guidance': '5 quotes the policy correctly and declines what it declines. '
                 'Any answer that improvises a concession scores 1.'},
    {'criterion': 'Priority judgement',
     'scale': '1 to 5',
     'guidance': 'Impact over tone, stated as a principle rather than guessed '
                 'at once.'},
    {'criterion': 'Written clarity',
     'scale': '1 to 5',
     'guidance': 'Would we send their draft unedited to our largest account?'},
    {'criterion': 'Technical curiosity',
     'scale': '1 to 5',
     'guidance': 'Do they check before replying, and are they willing to read '
                 'a log to do it?'},
]


def _weighted_total(breakdown):
    """The evaluation total, computed from the lines rather than asserted.

    Kept as a function so the number on the page cannot drift away from the
    evidence beneath it. If somebody edits a requirement score, the total
    follows on the next seed of a fresh database.
    """
    earned = sum(float(row.get('score') or 0) for row in breakdown)
    available = sum(float(row.get('max') or 0) for row in breakdown)
    if not available:
        return Decimal('0.00')
    return Decimal(str(round(100.0 * earned / available, 2)))


def seed_recruitment(owner=None):
    """Three openings, eight candidates, five evaluations and three interviews.

    Keyed on the opening title, the candidate email address and the
    (candidate, round) pair. An evaluation is append-only by design, so one is
    written only when that candidate has none at all.
    """
    hr = _agents().get('hr')
    created = 0
    today = _today()

    openings = {}
    for spec in _OPENINGS:
        opening, was_created = JobOpening.objects.get_or_create(
            title=spec['title'],
            defaults={
                'department': spec['department'],
                'location': 'Melbourne, VIC',
                'employment_type': 'full_time',
                'seniority': spec['seniority'],
                'is_remote': False,
                'summary': spec['summary'],
                'description': spec['description'],
                'responsibilities': spec['responsibilities'],
                'requirements': spec['requirements'],
                'nice_to_have': spec['nice_to_have'],
                'benefits': spec['benefits'],
                'salary_min': spec['salary_min'],
                'salary_max': spec['salary_max'],
                'salary_currency': 'AUD',
                'openings': spec['openings'],
                'hiring_manager': spec['hiring_manager'],
                'status': spec['status'],
                'target_start_date': today + timedelta(days=spec['start_offset']),
                'created_by_agent': hr,
                'created_by': _owner_or_first(owner),
            },
        )
        openings[spec['title']] = opening
        if was_created:
            created += 1
            _remember(opening)
            _backdate(opening, _at(24, hour=9, minute=30))

    candidates = {}
    for spec in _CANDIDATES:
        candidate, was_created = Candidate.objects.get_or_create(
            email=spec['email'],
            defaults={
                'full_name': spec['name'],
                'phone': spec['phone'],
                'location': spec['location'],
                'job_opening': openings.get(spec['opening']),
                'current_title': spec['current_title'],
                'current_company': spec['current_company'],
                'years_experience': spec['years'],
                'skills': spec['skills'],
                'resume_text': spec['resume'].strip(),
                'resume_source': 'Attached to the application email, text '
                                 'extracted on receipt.',
                'source': spec['source'],
                'status': spec['status'],
                'created_by_agent': hr,
            },
        )
        candidates[spec['name']] = candidate
        if was_created:
            created += 1
            _remember(candidate)
            _backdate(candidate, _at(spec['days_ago'], hour=8, minute=20))

    for spec in _EVALUATIONS:
        candidate = candidates.get(spec['candidate'])
        if candidate is None or candidate.evaluations.exists():
            continue
        total = _weighted_total(spec['breakdown'])
        evaluation = CandidateEvaluation.objects.create(
            candidate=candidate,
            job_opening=candidate.job_opening,
            agent=hr,
            score=total,
            score_breakdown=spec['breakdown'],
            strengths=spec['strengths'],
            gaps=spec['gaps'],
            recommendation=spec['recommendation'],
            rationale=spec['rationale'],
        )
        created += 1
        _remember(evaluation)
        _backdate(evaluation, _at(spec['days_ago'], hour=11, minute=5))
        # The cached score on the candidate row is exactly that -- a cache of
        # the latest evaluation. Written here so the list page can sort without
        # loading every breakdown.
        Candidate.objects.filter(pk=candidate.pk).update(score=total)

    created += _seed_interviews(candidates, openings, hr)
    _flush_manifest()
    return created


def _seed_interviews(candidates, openings, hr):
    """One completed interview, one booked, one still only planned.

    The three states matter more than the three rows. A proposed interview with
    written questions and no calendar entry is the platform's central claim
    made concrete: the hiring work happened immediately, and the part that
    reaches a person outside the company is still waiting for approval.
    """
    created = 0

    ravi = candidates.get('Ravi Chandrasekaran')
    if ravi and not ravi.interviews.exists():
        interview = Interview.objects.create(
            candidate=ravi,
            job_opening=openings.get('Senior Backend Engineer'),
            round_name='Technical Interview',
            round_number=1,
            scheduled_at=_at(6, hour=14),
            duration_minutes=60,
            mode='video',
            interviewers=['Priya Raghavan', 'Daniel Okafor'],
            location_or_link='https://meet.google.com/nwd-route-tech',
            status='completed',
            questions=_INTERVIEW_QUESTIONS_TECHNICAL,
            evaluation_form=_EVALUATION_FORM_TECHNICAL,
            outcome_score=Decimal('84.00'),
            feedback=(
                'Priya Raghavan and Daniel Okafor, sixty minutes, video.\n\n'
                'Django and relational depth: 5. Described the expand and '
                'contract pattern by name and unprompted, including the '
                'window where both columns exist and the backfill job that '
                'has to be idempotent because it will be retried. Named the '
                'one migration that did go wrong -- a NOT NULL added before '
                'the backfill finished -- and what he changed afterwards.\n\n'
                'Performance reasoning: 4. Asked what the eleven seconds is '
                'made of before proposing anything, which nobody else has '
                'done. Guessed distance-matrix construction rather than the '
                'solve itself, which is our actual finding. Lost a point for '
                'reaching for a Redis cache before he had the profile.\n\n'
                'API and contract thinking: 4. Additive field, no version '
                'bump, notify anyway. Confirmed he inherited the Linfox v1 '
                'contract rather than designing it, exactly as the screening '
                'evaluation suspected.\n\n'
                'Review and mentoring: 4. The query-cost review is a genuine '
                'mentoring mechanism rather than a claim. Distinguished '
                'correctness from preference without being prompted and said '
                'he labels his comments as one or the other.\n\n'
                'Operational honesty: 5.\n\n'
                'Recommendation: proceed to a final conversation with the '
                'founders. Both interviewers would hire.'),
            created_by_agent=hr,
        )
        created += 1
        _remember(interview)
        _backdate(interview, _at(9, hour=16, minute=10))

    sophie = candidates.get('Sophie Lindqvist')
    if sophie and not sophie.interviews.exists():
        # A booked interview has a calendar entry. This one is simulated
        # rather than live, which is the honest state on a fresh installation
        # with no Google credentials, and it is labelled as such everywhere.
        event = CalendarEvent.objects.create(
            title='Northwind Systems -- Technical Interview, Senior Backend '
                  'Engineer',
            description=(
                'Sixty minutes, video. Interviewers: Priya Raghavan '
                '(Engineering Manager) and Daniel Okafor (Senior Backend '
                'Engineer, and the referrer).\n\n'
                'The screening evaluation flagged two gaps to spend the time '
                'on: no AWS experience, and no evidence of code review or '
                'mentoring. Please weight the questions accordingly.'),
            start_at=_ahead(3, hour=11),
            end_at=_ahead(3, hour=12),
            duration_minutes=60,
            attendees=['sophie.lindqvist@protonmail.com',
                       f'priya.raghavan@{DOMAIN}',
                       f'daniel.okafor@{DOMAIN}'],
            location='Video call',
            meeting_link='https://meet.google.com/nwd-route-tech-2',
            status='simulated',
            agent=hr,
            subject_label='marketing.candidate',
            subject_id=sophie.pk,
        )
        _remember(event)
        _backdate(event, _at(2, hour=9, minute=45))

        interview = Interview.objects.create(
            candidate=sophie,
            job_opening=openings.get('Senior Backend Engineer'),
            round_name='Technical Interview',
            round_number=1,
            scheduled_at=_ahead(3, hour=11),
            duration_minutes=60,
            mode='video',
            interviewers=['Priya Raghavan', 'Daniel Okafor'],
            location_or_link='https://meet.google.com/nwd-route-tech-2',
            status='scheduled',
            questions=_INTERVIEW_QUESTIONS_TECHNICAL,
            evaluation_form=_EVALUATION_FORM_TECHNICAL,
            calendar_event=event,
            created_by_agent=hr,
        )
        created += 2
        _remember(interview)
        _backdate(interview, _at(2, hour=9, minute=40))

    grace = candidates.get('Grace Mbeki')
    if grace and not grace.interviews.exists():
        interview = Interview.objects.create(
            candidate=grace,
            job_opening=openings.get('Customer Support Specialist'),
            round_name='Support Scenario Interview',
            round_number=1,
            duration_minutes=45,
            mode='video',
            interviewers=['Nadia Halloran', 'Joshua Kirkbride'],
            status='proposed',
            questions=_INTERVIEW_QUESTIONS_SUPPORT,
            evaluation_form=_EVALUATION_FORM_SUPPORT,
            feedback='',
            created_by_agent=hr,
        )
        created += 1
        _remember(interview)
        _backdate(interview, _at(1, hour=15, minute=20))

    return created


# ===========================================================================
# 4. ENGINEERING
# ===========================================================================

# Twenty-two work items in a real tree: three epics, stories beneath them,
# subtasks beneath two of those stories, three bugs and a spike. The
# sequence_hint values make a buildable order rather than an arbitrary one, and
# the dependency links below include a chain of three, because a plan whose
# dependencies are all one hop deep has never met a real project.
#
# 'ref' is a local handle used only by this module to wire parents and
# dependencies together. It is not written to the database; WorkItem derives
# its own reference from the project key and the primary key.
_WORK_ITEMS = [
    # ---- Epic A: the solver ------------------------------------------
    {'ref': 'A', 'type': 'epic', 'seq': 100,
     'title': 'Multi-stop route solver that holds 200 stops under five seconds',
     'status': 'in_progress', 'priority': 'critical', 'complexity': 'large',
     'assignee': 'Daniel Okafor', 'points': 21, 'sprint': None,
     'labels': ['route-optimisation', 'performance'],
     'description':
         'The current solver plans a 200-stop run in about eleven seconds, '
         'which is past the point where a dispatcher will wait for it. Our '
         'largest account plans 1,400 stops across 38 vehicles before six in '
         'the morning, so eleven seconds becomes seventy-seven and the '
         'planner is abandoned in favour of a spreadsheet. This epic covers '
         'ingesting the stop data, solving it, and exposing the result.',
     'criteria': [
         'A 200-stop plan completes in under five seconds at the 95th '
         'percentile on production-shaped data.',
         'The benchmark runs in CI and fails the build on regression.',
         'The planning API returns the same result shape as v1 so the driver '
         'application needs no change.'],
     },
    {'ref': 'A1', 'parent': 'A', 'type': 'story', 'seq': 110,
     'title': 'Ingest depot and stop data from the fleet API',
     'status': 'done', 'priority': 'high', 'complexity': 'moderate',
     'assignee': 'Hannah Whitcombe', 'points': 5, 'sprint': 'closed',
     'labels': ['route-optimisation', 'ingest'],
     'description':
         'Pull depots, vehicles and stops from the customer fleet API and '
         'land them in the planning schema. Everything downstream is blocked '
         'on this, so it went first.',
     'criteria': [
         'A full sync of 1,400 stops completes in under 30 seconds.',
         'A partial failure leaves no half-written run.',
         'Every stop carries a validated latitude and longitude or is '
         'rejected with a named reason.'],
     },
    {'ref': 'A1a', 'parent': 'A1', 'type': 'subtask', 'seq': 111,
     'title': 'Map the fleet API payload to the internal stop schema',
     'status': 'done', 'priority': 'high', 'complexity': 'small',
     'assignee': 'Hannah Whitcombe', 'points': 2, 'sprint': 'closed',
     'labels': ['ingest'],
     'description':
         'The fleet API returns service windows as local time strings with no '
         'zone. Everything internal is stored as UTC with an explicit zone on '
         'the depot, so the mapping has to resolve the zone from the depot '
         'rather than assume Melbourne.',
     'criteria': [
         'A depot in Perth produces correct UTC service windows.',
         'A missing or unparseable window rejects the stop rather than '
         'defaulting it.'],
     },
    {'ref': 'A1b', 'parent': 'A1', 'type': 'subtask', 'seq': 112,
     'title': 'Add retry and backoff to the fleet API client',
     'status': 'in_progress', 'priority': 'high', 'complexity': 'small',
     'assignee': 'Hannah Whitcombe', 'points': 2, 'sprint': 'active',
     'due_offset': -6, 'labels': ['ingest', 'reliability'],
     'description':
         'The fleet API returns a 502 roughly once in every four hundred '
         'calls and we currently fail the whole sync. Exponential backoff '
         'with a jitter, four attempts, and an idempotency key so a retry '
         'cannot create the stop twice. The missing idempotency key is the '
         'direct cause of the duplicate-stops bug.',
     'criteria': [
         'A single 502 is invisible to the caller.',
         'Four consecutive failures abort the sync with the upstream status '
         'in the error.',
         'A retried create does not produce a second stop.'],
     },
    {'ref': 'A2', 'parent': 'A', 'type': 'story', 'seq': 120,
     'title': 'Solve a 200-stop run in under five seconds',
     'status': 'in_progress', 'priority': 'critical', 'complexity': 'large',
     'assignee': 'Daniel Okafor', 'points': 8, 'sprint': 'active',
     'labels': ['route-optimisation', 'performance'],
     'description':
         'Profiling says 7.4 of the eleven seconds is spent building the '
         'distance matrix, not solving. That is the work: cache the matrix per '
         'depot, invalidate on stop change, and only then look at the solver '
         'itself.',
     'criteria': [
         'p95 under five seconds for the 200-stop fixture.',
         'The profile is attached to the ticket, before and after.',
         'No change to the plan output for the regression fixtures.'],
     },
    {'ref': 'A2a', 'parent': 'A2', 'type': 'subtask', 'seq': 121,
     'title': 'Benchmark the solver against the 200-stop fixture',
     'status': 'blocked', 'priority': 'high', 'complexity': 'small',
     'assignee': 'Daniel Okafor', 'points': 2, 'sprint': 'active',
     'labels': ['performance', 'testing'],
     'blocked_reason':
         'Waiting on the anonymised 200-stop production fixture from Kembla '
         'Freight Group. Nadia Halloran requested it on the account call and '
         'their operations manager is back from leave on Monday. The '
         'synthetic fixture we have is uniformly distributed, which makes the '
         'distance matrix cache look better than it is, so benchmarking '
         'against it would be worse than not benchmarking.',
     'description':
         'A repeatable benchmark that runs in CI and fails the build if p95 '
         'regresses by more than ten per cent.',
     'criteria': [
         'Runs in under two minutes in CI.',
         'Reports p50, p95 and p99 rather than a mean.'],
     },
    {'ref': 'A3', 'parent': 'A', 'type': 'story', 'seq': 130,
     'title': 'Expose the solver through the planning API',
     'status': 'todo', 'priority': 'high', 'complexity': 'moderate',
     'assignee': 'Daniel Okafor', 'points': 5, 'sprint': 'active',
     'labels': ['route-optimisation', 'api'],
     'description':
         'A versioned POST /api/v2/plans that accepts a depot, a vehicle set '
         'and a stop list, and returns an ordered plan per vehicle. Additive '
         'against v1, so the driver application and the two partner '
         'integrations need no change on the day we ship.',
     'criteria': [
         'OpenAPI schema published and checked in.',
         'Contract tests cover both partner payload shapes.',
         'A 400 names the offending field.'],
     },
    # ---- Epic B: the driver-facing view -------------------------------
    {'ref': 'B', 'type': 'epic', 'seq': 200,
     'title': 'Driver and dispatcher schedule view',
     'status': 'todo', 'priority': 'high', 'complexity': 'large',
     'assignee': 'Liam Petrakis', 'points': 13, 'sprint': None,
     'labels': ['frontend', 'driver-app'],
     'description':
         'A plan nobody can see is not a plan. This epic puts the solved route '
         'in front of the driver on a phone and in front of the dispatcher on '
         'a desk, and lets the dispatcher override it.',
     'criteria': [
         'A driver can see the day route on a phone, offline, after one load.',
         'A dispatcher can reorder stops and see the time impact before '
         'saving.'],
     },
    {'ref': 'B1', 'parent': 'B', 'type': 'story', 'seq': 210,
     'title': 'Show the day route on the driver dashboard',
     'status': 'todo', 'priority': 'high', 'complexity': 'moderate',
     'assignee': 'Liam Petrakis', 'points': 5, 'sprint': None,
     'labels': ['frontend', 'driver-app'],
     'description':
         'Ordered stop list, estimated arrival per stop, and the map. Reads '
         'the v2 planning API, so it cannot start before that endpoint exists.',
     'criteria': [
         'Renders 200 stops without the list stuttering on a mid-range '
         'Android phone.',
         'Works from cache with no network after one successful load.',
         'Times are shown in the depot zone with the zone named.'],
     },
    {'ref': 'B2', 'parent': 'B', 'type': 'story', 'seq': 220,
     'title': 'Allow a dispatcher to reorder stops by hand',
     'status': 'blocked', 'priority': 'medium', 'complexity': 'moderate',
     'assignee': 'Liam Petrakis', 'points': 5, 'sprint': None,
     'labels': ['frontend', 'dispatcher'],
     'blocked_reason':
         'Waiting on the drag-and-drop interaction specification from Chloe '
         'Mainwaring. The open question is what happens to the estimated '
         'arrival times of every downstream stop while a stop is mid-drag: '
         'recompute live, recompute on drop, or show them as stale. Building '
         'either of the first two without the design decision would be a '
         'rewrite, so this waits.',
     'description':
         'Drag to reorder, with the time impact of the change shown before it '
         'is committed. A dispatcher who cannot see the cost of an override '
         'will make it anyway.',
     'criteria': [
         'The change is not saved until it is confirmed.',
         'Downstream arrival times update or are clearly marked stale.',
         'Keyboard reordering works, not just the mouse.'],
     },
    # ---- Epic C: reporting -------------------------------------------
    {'ref': 'C', 'type': 'epic', 'seq': 300,
     'title': 'Run reporting and export',
     'status': 'in_progress', 'priority': 'high', 'complexity': 'moderate',
     'assignee': 'Hannah Whitcombe', 'points': 13, 'sprint': None,
     'labels': ['reporting', 'export'],
     'description':
         'Every enterprise account asks the same two questions at the end of '
         'the month: what did we plan, and did it happen. Three of them '
         'currently answer it by exporting to CSV and the export is the '
         'largest single source of support tickets we have.',
     'criteria': [
         'A completed run exports to CSV for any date range up to twelve '
         'months.',
         'The weekly on-time report reaches the account owner without '
         'anybody running anything.'],
     },
    {'ref': 'C1', 'parent': 'C', 'type': 'story', 'seq': 310,
     'title': 'Export a completed run to CSV',
     'status': 'todo', 'priority': 'high', 'complexity': 'moderate',
     'assignee': 'Hannah Whitcombe', 'points': 5, 'sprint': 'active',
     'due_offset': -3, 'labels': ['reporting', 'export'],
     'description':
         'Stream the export rather than building it in memory, which is the '
         'actual fix for the timeout three customers have now reported. Reads '
         'the v2 plan shape, so it follows the planning API.',
     'criteria': [
         'A twelve-month range for the largest account completes without a '
         'timeout.',
         'Memory use is flat regardless of the range.',
         'The file is byte-identical to the current export for a range that '
         'currently works.'],
     },
    {'ref': 'C2', 'parent': 'C', 'type': 'story', 'seq': 320,
     'title': 'Weekly on-time delivery report per account',
     'status': 'backlog', 'priority': 'medium', 'complexity': 'moderate',
     'assignee': 'Hannah Whitcombe', 'points': 5, 'sprint': None,
     'labels': ['reporting'],
     'description':
         'Planned versus actual arrival, by vehicle and by stop, for the week. '
         'The number the account managers are asked for on every renewal call.',
     'criteria': [
         'Runs for every active account in under ten minutes total.',
         'A stop with no actual arrival is excluded and counted separately '
         'rather than treated as late.'],
     },
    {'ref': 'C3', 'parent': 'C', 'type': 'story', 'seq': 330,
     'title': 'Scheduled email delivery of the weekly report',
     'status': 'backlog', 'priority': 'low', 'complexity': 'small',
     'assignee': None, 'points': 3, 'sprint': None,
     'labels': ['reporting', 'email'],
     'description':
         'Send the weekly report to the named account contacts on Monday '
         'morning, with a one-line summary in the body so it is useful '
         'without opening the attachment.',
     'criteria': [
         'A failed send retries and then alerts, rather than failing '
         'silently.',
         'A contact can unsubscribe from the report without leaving the '
         'account.'],
     },
    # ---- Bugs --------------------------------------------------------
    {'ref': 'BUG1', 'type': 'bug', 'seq': 400,
     'title': 'Route export times out for date ranges over 90 days',
     'status': 'in_progress', 'priority': 'critical', 'complexity': 'moderate',
     'assignee': 'Hannah Whitcombe', 'points': 3, 'sprint': 'active',
     'labels': ['export', 'customer-reported', 'performance'],
     'description':
         'Reported by three separate enterprise accounts in eight days: '
         'Kembla Freight Group, Yarra Valley Produce Co and Bendigo Cold '
         'Chain. The export builds the entire result set in memory before '
         'writing anything, so beyond roughly 90 days of runs on a large '
         'account the worker exceeds its memory limit and the gateway returns '
         '504 after 60 seconds. The customer sees a spinner that never '
         'finishes and no error at all.\n\n'
         'The streaming rewrite in ROUTE C1 is the real fix. This ticket '
         'covers the immediate mitigation: cap the range at 90 days with an '
         'explicit message, so a dispatcher gets a refusal they can act on '
         'instead of a spinner.',
     'criteria': [
         'A range over 90 days returns a clear message naming the limit and '
         'suggesting a narrower range.',
         'The 504 no longer occurs for any range.',
         'The three reporting accounts are told, by Support, that the cap is '
         'temporary.'],
     },
    {'ref': 'BUG2', 'type': 'bug', 'seq': 410,
     'title': 'Duplicate stops created when the fleet API retries',
     'status': 'todo', 'priority': 'high', 'complexity': 'small',
     'assignee': 'Hannah Whitcombe', 'points': 2, 'sprint': 'active',
     'labels': ['ingest', 'data-integrity'],
     'description':
         'When the fleet API times out after having actually accepted the '
         'create, our retry writes the stop a second time. Sixty-one '
         'duplicate stops across four accounts since the sync went live. A '
         'duplicate stop makes the plan visit an address twice, which the '
         'driver notices before we do.\n\n'
         'Cannot be fixed independently of the idempotency key being added in '
         'ROUTE A1b, so it depends on it.',
     'criteria': [
         'A retried create is a no-op rather than a second row.',
         'The 61 existing duplicates are merged by a one-off migration that '
         'reports what it changed.'],
     },
    {'ref': 'BUG3', 'type': 'bug', 'seq': 420,
     'title': 'Driver dashboard shows yesterday route after midnight AEST',
     'status': 'in_progress', 'priority': 'high', 'complexity': 'small',
     'assignee': 'Liam Petrakis', 'points': 2, 'sprint': 'active',
     'due_offset': -9, 'labels': ['frontend', 'timezone', 'customer-reported'],
     'description':
         'The dashboard resolves "today" from the browser clock in UTC while '
         'the plan is keyed on the depot local date. Between midnight and '
         '10am AEST the two disagree, so a driver starting a 4am shift sees '
         'the previous day route. Reported by two Sandbelt Couriers drivers '
         'and, embarrassingly, by our own designer.',
     'criteria': [
         'The dashboard resolves the date from the depot zone, not the '
         'browser.',
         'A test covers a 4am AEST start against a UTC-set device clock.'],
     },
    # ---- Spike -------------------------------------------------------
    {'ref': 'SPIKE', 'type': 'spike', 'seq': 430,
     'title': 'Evaluate OR-Tools against the in-house solver',
     'status': 'in_review', 'priority': 'medium', 'complexity': 'unknown',
     'assignee': 'Daniel Okafor', 'points': 3, 'sprint': 'active',
     'labels': ['route-optimisation', 'spike'],
     'description':
         'Two days, timeboxed, to answer one question: would OR-Tools give us '
         'a better plan than the in-house heuristic at 200 stops, and at what '
         'cost in solve time and in maintenance. Written up as a '
         'recommendation with numbers, not a preference.\n\n'
         'Prompted partly by the Yuki Tanaka application, whose resume '
         'describes exactly this comparison at a much larger scale.',
     'criteria': [
         'Plan quality compared on the same fixture, measured in total '
         'planned kilometres.',
         'Solve time compared at 50, 200 and 500 stops.',
         'A recommendation with a named cost, including who would maintain '
         'it.'],
     },
    # ---- Tasks and chores --------------------------------------------
    {'ref': 'T1', 'type': 'task', 'seq': 140,
     'title': 'Add PostGIS indexes to the stop and depot tables',
     'status': 'done', 'priority': 'high', 'complexity': 'small',
     'assignee': 'Daniel Okafor', 'points': 2, 'sprint': 'closed',
     'labels': ['database', 'performance'],
     'description':
         'GiST index on stop.location and a composite on (depot_id, '
         'service_date). Created concurrently, because the stop table is 41 '
         'million rows and a plain CREATE INDEX would lock it for minutes.',
     'criteria': [
         'Indexes created concurrently with no lock held.',
         'The nearest-depot query plan shows an index scan, attached to the '
         'ticket.'],
     },
    {'ref': 'T2', 'type': 'task', 'seq': 150,
     'title': 'Upgrade Django to 6.1 and re-run the solver test suite',
     'status': 'done', 'priority': 'medium', 'complexity': 'small',
     'assignee': 'Daniel Okafor', 'points': 3, 'sprint': 'closed',
     'labels': ['maintenance'],
     'description':
         'Routine upgrade. Two deprecation warnings in the ingest module, '
         'both fixed. No behaviour change in the solver.',
     'criteria': [
         'Full suite green.',
         'No deprecation warnings remaining in our own code.'],
     },
    # Left in the closed sprint deliberately and not finished. A sprint report
    # in which everything committed was completed has never been produced by a
    # real team, and the carry-over is the interesting line on it.
    {'ref': 'T3', 'type': 'task', 'seq': 440,
     'title': 'Add structured logging to the solver worker',
     'status': 'todo', 'priority': 'medium', 'complexity': 'small',
     'assignee': 'Hannah Whitcombe', 'points': 2, 'sprint': 'closed',
     'labels': ['observability'],
     'description':
         'One JSON line per solve with the stop count, the vehicle count, the '
         'matrix build time and the solve time. We diagnosed the export '
         'timeout from customer descriptions because we could not diagnose it '
         'from our own logs.',
     'criteria': [
         'Every solve emits exactly one structured line.',
         'No customer address or name appears in any log line.'],
     },
    {'ref': 'T4', 'type': 'chore', 'seq': 450,
     'title': 'Rotate the fleet API credentials',
     'status': 'done', 'priority': 'medium', 'complexity': 'trivial',
     'assignee': 'Priya Raghavan', 'points': 1, 'sprint': 'closed',
     'labels': ['security', 'maintenance'],
     'description':
         'Quarterly rotation, per the data privacy and security policy. New '
         'credentials in the secret store, old ones revoked after the '
         'twenty-four hour overlap.',
     'criteria': [
         'Old credential confirmed revoked.',
         'Rotation date recorded for the access audit.'],
     },
    {'ref': 'T5', 'type': 'task', 'seq': 460,
     'title': 'Cancel the legacy XML plan feed',
     'status': 'cancelled', 'priority': 'lowest', 'complexity': 'small',
     'assignee': None, 'points': 2, 'sprint': None,
     'labels': ['maintenance'],
     'description':
         'Superseded. Two Bays Distribution was the only remaining consumer '
         'of the XML feed and has moved to the JSON API, so the removal work '
         'is now a deletion rather than a migration and will ride along with '
         'ROUTE A3.',
     'criteria': [],
     },
]

# At least four real dependency links, including the chain A1 -> A2 -> A3.
# Written as (item, depends on) pairs because that is the direction a reader
# asks the question in.
_DEPENDENCIES = [
    ('A2', 'A1'),      # cannot solve what has not been ingested
    ('A3', 'A2'),      # cannot expose a solver that is not fast enough
    ('B1', 'A3'),      # the dashboard reads the v2 endpoint
    ('C1', 'A3'),      # so does the export
    ('BUG2', 'A1b'),   # the duplicate fix needs the idempotency key
    ('A2a', 'A1'),     # the benchmark needs ingested data
]

# Six comments, some from a person and some from an AI employee, on the items
# where a real team would actually have talked.
_WORK_COMMENTS = [
    ('A2', 'agent', 'engineering_manager', 3,
     'Profiled this against the synthetic fixture: 7.4 seconds of the 11.2 is '
     'distance matrix construction and 3.1 is the solve. Recommend the cache '
     'lands before anybody touches the heuristic, because a faster heuristic '
     'over a slow matrix build gets us to nine seconds and no further.'),
    ('A2', 'user', None, 2,
     'Agreed. Daniel, take the matrix cache and leave the solver alone this '
     'sprint. If the OR-Tools spike says something surprising we will want the '
     'heuristic untouched to compare against.'),
    ('BUG1', 'agent', 'support', 5,
     'Third report of this shape in eight days -- Kembla Freight Group, Yarra '
     'Valley Produce Co and Bendigo Cold Chain, all enterprise, all on ranges '
     'over three months. Every one of them described a spinner rather than an '
     'error, so nobody raised it as a failure and two of them had been living '
     'with it for a fortnight before they told us.'),
    ('BUG1', 'user', None, 4,
     'The 90-day cap is a mitigation, not a fix, and I want the message to '
     'say so. Nadia is telling all three accounts today that the real fix is '
     'in the sprint after this one.'),
    ('A2a', 'agent', 'engineering_manager', 2,
     'Blocked, and worth being explicit about why rather than just marking it '
     'blocked: the synthetic fixture is uniformly distributed, so the matrix '
     'cache hit rate on it is about 94 per cent, where production clustering '
     'suggests it will be nearer 60. Benchmarking against the synthetic data '
     'would tell us the change worked when it had not.'),
    ('SPIKE', 'agent', 'developer', 1,
     'Two days spent, written up in the artefact attached to this item. Short '
     'version: OR-Tools finds a plan roughly 6 per cent shorter at 200 stops '
     'and takes 2.8 times as long to do it, and the maintenance cost is a new '
     'dependency nobody here has operated. Recommend we keep the heuristic '
     'for now, revisit at 500 stops, and note that the candidate currently in '
     'the HR pipeline has done exactly this at national scale.'),
]


def seed_engineering(owner=None):
    """Two projects, two sprints, the work item tree, and the artefacts.

    Keyed on the project name, the (project, sprint name) pair and the
    (project, title) pair for work items. The dependency links are set rather
    than added, so a second run cannot double them.
    """
    agents = _agents()
    manager = agents.get('engineering_manager')
    developer = agents.get('developer')
    account = _owner_or_first(owner)
    created = 0
    today = _today()

    project, was_created = Project.objects.get_or_create(
        name='Route Optimisation v2',
        defaults={
            'description':
                'The second generation of the route planning engine. The '
                'first generation solves a 200-stop run in about eleven '
                'seconds, which is past the point at which a dispatcher stops '
                'waiting and opens a spreadsheet instead. v2 rebuilds the '
                'ingest, the solver and the planning API, and puts the result '
                'in front of both the driver and the dispatcher.\n\n'
                'Three enterprise accounts are waiting on the export fix that '
                'falls out of this work, so the project has customer '
                'commitments against it rather than only internal ones.',
            'status': 'active',
            'owner': 'Priya Raghavan',
            'start_date': today - timedelta(days=52),
            'target_date': today + timedelta(days=63),
            'repository': 'northwind-systems/route-optimiser',
            'jira_project_key': 'ROUTE',
            'tech_stack': ['Python 3.13', 'Django 6.1', 'Django REST Framework',
                           'PostgreSQL 16', 'PostGIS', 'Celery', 'Redis',
                           'React', 'TypeScript', 'AWS ECS'],
            'created_by_agent': manager,
            'created_by': account,
        },
    )
    if was_created:
        created += 1
        _remember(project)
        _backdate(project, _at(21, hour=9))

    planning, was_created = Project.objects.get_or_create(
        name='Warehouse Mobile Companion',
        defaults={
            'description':
                'A phone application for warehouse staff loading against a '
                'planned run: scan the consignment, confirm it onto the right '
                'vehicle, and flag a short pick before the truck leaves rather '
                'than after it arrives.\n\n'
                'In planning only. Nothing starts until Route Optimisation v2 '
                'ships, because the loading sequence it would display does not '
                'exist until the v2 planner produces it. Chloe Mainwaring has '
                'done the discovery interviews; the write-up is the only '
                'artefact so far.',
            'status': 'planning',
            'owner': 'Priya Raghavan',
            'start_date': today + timedelta(days=70),
            'target_date': today + timedelta(days=210),
            'repository': '',
            'jira_project_key': '',
            'tech_stack': ['React Native', 'TypeScript', 'Django REST Framework'],
            'created_by_agent': manager,
            'created_by': account,
        },
    )
    if was_created:
        created += 1
        _remember(planning)
        _backdate(planning, _at(11, hour=14, minute=30))

    sprints = {}
    for key, name, goal, start_off, end_off, status, capacity in (
        ('closed', 'Sprint 12',
         'Land the fleet API ingest end to end, with the PostGIS indexes it '
         'needs underneath it, so the solver work in Sprint 13 has real data '
         'to run against.',
         28, 15, 'closed', 30),
        ('active', 'Sprint 13',
         'Get a 200-stop plan under five seconds and stop the export timing '
         'out for the three accounts that have reported it. Nothing else '
         'matters this fortnight.',
         13, -1, 'active', 30),
    ):
        sprint, was_created = Sprint.objects.get_or_create(
            project=project, name=name,
            defaults={
                'goal': goal,
                'start_date': today - timedelta(days=start_off),
                'end_date': today - timedelta(days=end_off),
                'status': status,
                'capacity_points': capacity,
                'created_by_agent': manager,
            },
        )
        sprints[key] = sprint
        if was_created:
            created += 1
            _remember(sprint)
            _backdate(sprint, _at(start_off, hour=9, minute=10))

    # The tree is built in the order it is declared, so a parent is always
    # present before the child that names it.
    items = {}
    for spec in _WORK_ITEMS:
        parent = items.get(spec.get('parent')) if spec.get('parent') else None
        assignee = _employee(spec['assignee']) if spec.get('assignee') else None
        due = None
        if spec.get('due_offset') is not None:
            due = today + timedelta(days=spec['due_offset'])
        item, was_created = WorkItem.objects.get_or_create(
            project=project, title=spec['title'],
            defaults={
                'sprint': sprints.get(spec.get('sprint')),
                'parent': parent,
                'description': spec['description'],
                'acceptance_criteria': spec['criteria'],
                'item_type': spec['type'],
                'status': spec['status'],
                'priority': spec['priority'],
                'complexity': spec['complexity'],
                'assignee': assignee,
                'assignee_name': '' if assignee else (spec.get('assignee') or ''),
                'estimate_points': spec['points'],
                'due_date': due,
                'labels': spec['labels'],
                'sequence_hint': spec['seq'],
                'blocked_reason': spec.get('blocked_reason', ''),
                'created_by_agent': manager,
                'completed_at': (_at(16, hour=15)
                                 if spec['status'] == 'done' else None),
            },
        )
        items[spec['ref']] = item
        if was_created:
            created += 1
            _remember(item)
            _backdate(item, _at(min(20, 4 + spec['seq'] % 17), hour=10,
                                minute=spec['seq'] % 60))

    for child_ref, parent_ref in _DEPENDENCIES:
        child, blocker = items.get(child_ref), items.get(parent_ref)
        if child and blocker:
            child.depends_on.add(blocker)

    created += _seed_work_comments(items, agents, account)
    created += _seed_code_artefacts(project, items, developer)
    created += _seed_sprint_report(project, sprints.get('closed'), manager)

    _flush_manifest()
    return created


def _seed_work_comments(items, agents, account):
    created = 0
    for ref, author_kind, agent_type, days_ago, body in _WORK_COMMENTS:
        item = items.get(ref)
        if item is None:
            continue
        agent = agents.get(agent_type) if author_kind == 'agent' else None
        author = account if author_kind == 'user' else None
        # Keyed on the opening of the body, because a comment has no other
        # natural key and two people never open a comment identically.
        if item.comments.filter(body__startswith=body[:60]).exists():
            continue
        comment = WorkItemComment.objects.create(
            work_item=item, author=author, agent=agent, body=body)
        created += 1
        _remember(comment)
        _backdate(comment, _at(days_ago, hour=11, minute=25))
    return created


# Four artefacts of four different kinds, with content somebody could actually
# use. The point of a CodeArtifact row is that an AI employee produced
# something reviewable rather than a paragraph of advice, so a seeded artefact
# containing a placeholder would demonstrate the opposite.
_ARTEFACTS = [
    {
        'kind': 'model',
        'title': 'Stop and RouteRun models for the v2 planner',
        'language': 'python',
        'item': 'A1',
        'status': 'applied',
        'path': 'routing/models.py',
        'days_ago': 17,
        'explanation':
            'Two decisions in here are worth stating rather than leaving to be '
            'discovered. First, service_window_start and service_window_end '
            'are stored in UTC and the zone lives on the depot: the fleet API '
            'sends local time strings with no offset, and resolving them '
            'against the depot rather than against the server is the only way '
            'a Perth depot plans correctly from a Melbourne server. Second, '
            'external_id is unique per depot rather than globally, because two '
            'customers legitimately use the same internal stop code and a '
            'global constraint would make the second customer unable to sync.',
        'content': '''from django.contrib.gis.db import models as gis
from django.db import models


class Depot(models.Model):
    # A depot owns the timezone every stop under it is resolved against.
    # Storing it here rather than on the account is deliberate: a national
    # customer has depots in three zones and one account setting cannot be
    # right for all of them.
    account = models.ForeignKey('accounts.Account', on_delete=models.CASCADE,
                                related_name='depots')
    external_id = models.CharField(max_length=64)
    name = models.CharField(max_length=160)
    location = gis.PointField(geography=True, srid=4326)
    timezone_name = models.CharField(max_length=64, default='Australia/Melbourne')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'external_id'],
                                    name='uniq_depot_per_account'),
        ]
        indexes = [gis.Index(fields=['location'], name='depot_location_gist')]


class Stop(models.Model):
    # Service windows are UTC. The local time the customer sent is resolved
    # against depot.timezone_name at ingest, never at read time, so a report
    # run from another zone cannot shift a window that was already correct.
    depot = models.ForeignKey(Depot, on_delete=models.CASCADE,
                              related_name='stops')
    external_id = models.CharField(max_length=64)
    address = models.CharField(max_length=400)
    location = gis.PointField(geography=True, srid=4326)
    service_date = models.DateField()
    service_window_start = models.DateTimeField()
    service_window_end = models.DateTimeField()
    service_minutes = models.PositiveSmallIntegerField(default=5)
    weight_kg = models.DecimalField(max_digits=9, decimal_places=2, default=0)

    class Meta:
        constraints = [
            # Unique per depot, not globally. Two customers use the same
            # internal stop codes and a global constraint locks the second
            # one out of syncing at all.
            models.UniqueConstraint(fields=['depot', 'external_id'],
                                    name='uniq_stop_per_depot'),
            models.CheckConstraint(
                condition=models.Q(
                    service_window_end__gt=models.F('service_window_start')),
                name='stop_window_ordered'),
        ]
        indexes = [
            gis.Index(fields=['location'], name='stop_location_gist'),
            models.Index(fields=['depot', 'service_date'],
                         name='stop_depot_date_idx'),
        ]


class RouteRun(models.Model):
    # One solved plan. Kept as a row rather than recomputed because a
    # dispatcher who overrode the plan at 6am needs to be able to show what
    # the planner originally proposed.
    STATUS = [('planned', 'Planned'), ('dispatched', 'Dispatched'),
              ('completed', 'Completed'), ('abandoned', 'Abandoned')]

    depot = models.ForeignKey(Depot, on_delete=models.CASCADE,
                              related_name='runs')
    service_date = models.DateField()
    status = models.CharField(max_length=12, choices=STATUS, default='planned')
    solver_version = models.CharField(max_length=30)
    solve_seconds = models.DecimalField(max_digits=6, decimal_places=3,
                                        null=True, blank=True)
    planned_km = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    plan = models.JSONField(default=dict, blank=True)
    overridden_by = models.CharField(max_length=140, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['depot', '-service_date'],
                         name='run_depot_date_idx'),
        ]
''',
    },
    {
        'kind': 'test',
        'title': 'Tests for the 200-stop solver benchmark',
        'language': 'python',
        'item': 'A2a',
        'status': 'draft',
        'path': 'routing/tests/test_solver_performance.py',
        'days_ago': 5,
        'explanation':
            'These are the tests the benchmark subtask needs, written while it '
            'is blocked on the production fixture so that the moment the '
            'fixture arrives there is nothing left to write. Note that the '
            'performance assertion is on p95 rather than on a single run: a '
            'single-run assertion on shared CI hardware fails for reasons that '
            'have nothing to do with the code, and a flaky performance test '
            'gets disabled within a fortnight, which is worse than not having '
            'one.',
        'content': '''import statistics
import time

import pytest
from django.test import TestCase

from routing.fixtures import load_stop_fixture
from routing.solver import solve_run

# The synthetic fixture is uniformly distributed and flatters the distance
# matrix cache. Both are loaded so a regression can be attributed: if
# synthetic holds and production degrades, the cause is clustering.
FIXTURES = ('synthetic_200', 'kembla_200_anonymised')

TARGET_P95_SECONDS = 5.0
REGRESSION_TOLERANCE = 1.10


def _timed_solve(fixture_name, runs=12):
    depot, stops, vehicles = load_stop_fixture(fixture_name)
    durations = []
    for _ in range(runs):
        started = time.perf_counter()
        solve_run(depot=depot, stops=stops, vehicles=vehicles)
        durations.append(time.perf_counter() - started)
    durations.sort()
    return {
        'p50': statistics.median(durations),
        'p95': durations[int(len(durations) * 0.95) - 1],
        'p99': durations[-1],
    }


class SolverPerformanceTests(TestCase):

    def test_two_hundred_stops_under_five_seconds_p95(self):
        # The requirement, stated once, in the place it is enforced.
        result = _timed_solve('kembla_200_anonymised')
        self.assertLess(
            result['p95'], TARGET_P95_SECONDS,
            f"p95 was {result['p95']:.2f}s against a {TARGET_P95_SECONDS}s "
            f"target. p50 {result['p50']:.2f}s, p99 {result['p99']:.2f}s.")

    def test_plan_output_is_unchanged_for_regression_fixtures(self):
        # Speed is worthless if the plan changed. This is the guard that lets
        # the matrix cache be reviewed on its performance alone.
        depot, stops, vehicles = load_stop_fixture('regression_50')
        first = solve_run(depot=depot, stops=stops, vehicles=vehicles)
        second = solve_run(depot=depot, stops=stops, vehicles=vehicles)
        self.assertEqual(first.stop_order, second.stop_order)
        self.assertAlmostEqual(float(first.planned_km),
                               float(second.planned_km), places=3)

    def test_matrix_cache_is_invalidated_when_a_stop_moves(self):
        depot, stops, vehicles = load_stop_fixture('regression_50')
        before = solve_run(depot=depot, stops=stops, vehicles=vehicles)
        stops[7].location = stops[7].location.transform(
            4326, clone=True)  # nudge the seventh stop
        stops[7].save(update_fields=['location'])
        after = solve_run(depot=depot, stops=stops, vehicles=vehicles)
        self.assertNotEqual(before.plan_digest, after.plan_digest,
                            'A moved stop must not be served from cache.')


@pytest.mark.skipif(not load_stop_fixture.exists('kembla_200_anonymised'),
                    reason='Production-shaped fixture not yet supplied. '
                           'Tracked on the benchmark subtask.')
def test_clustered_data_does_not_regress_against_synthetic():
    synthetic = _timed_solve('synthetic_200', runs=6)
    clustered = _timed_solve('kembla_200_anonymised', runs=6)
    assert clustered['p95'] < synthetic['p95'] * 2.0, (
        'Clustered production data is more than twice as slow as synthetic, '
        'which means the cache hit rate assumption is wrong.')
''',
    },
    {
        'kind': 'debug',
        'title': 'Why the route export times out beyond ninety days',
        'language': 'markdown',
        'item': 'BUG1',
        'status': 'reviewed',
        'path': '',
        'days_ago': 6,
        'explanation':
            'Written from three customer descriptions rather than from a log, '
            'because the solver worker does not currently emit one. That is '
            'itself a finding and it has been raised as its own work item.',
        'content': '''# Route export timeout beyond ninety days -- analysis

## What the customer sees

A spinner that never resolves. No error message, no failed download, no
notification. Two of the three reporting accounts had been living with this for
around a fortnight before they raised it, because a spinner does not look like
a fault -- it looks like a slow report.

## What actually happens

1. `POST /exports/runs` builds the entire result set in memory as a list of
   dictionaries before writing a single byte of CSV.
2. For Kembla Freight Group, ninety days is roughly 118,000 run-stop rows. At
   about 2.9 KB per row once the nested stop payload is expanded, that is
   around 340 MB in a worker limited to 512 MB.
3. The worker is not killed. It swaps, slows, and keeps going.
4. The gateway gives up first: a hard sixty-second timeout returns 504 to the
   browser.
5. The front end treats a 504 on this endpoint as "still working" because the
   original implementation polled for a slow-but-eventual response. So the
   spinner stays up forever.

## Evidence

- All three reports name a range of three months or more. No report exists for
  a range under ninety days.
- All three are enterprise accounts, which is a proxy for row count rather
  than for anything about the tier.
- Bendigo Cold Chain reported it succeeding for a single month, which is the
  control case.
- The 504 is visible in the gateway access log. Nothing appears in the
  application log at all, because the worker never finishes and never logs a
  completion.

## Why the fix is two tickets and not one

The correct fix is to stream the response and never hold the set in memory,
which is the streaming rewrite already scoped as the CSV export story. That is
five points of work and it lands next sprint.

Meanwhile three enterprise accounts are getting a spinner. So the immediate
change is a ninety-day cap with an explicit message, which converts a silent
infinite spinner into a refusal a dispatcher can act on. A cap is not a fix and
the message should not pretend it is.

## Two things this found by accident

- The front end interprets 504 as "keep waiting" on this endpoint. That is
  wrong for every endpoint and should be changed regardless of the export.
- We diagnosed a production fault from customer prose because our own logs
  were silent. Structured logging on the solver worker is now on the board.
''',
    },
    {
        'kind': 'readme',
        'title': 'README section: running the planner locally',
        'language': 'markdown',
        'item': 'A3',
        'status': 'applied',
        'path': 'README.md',
        'days_ago': 8,
        'explanation':
            'The section a new starter needs on day two. Written after Liam '
            'Petrakis lost most of a morning to the PostGIS extension not '
            'being enabled, which nothing told him and nothing checked.',
        'content': '''## Running the planner locally

### What you need

- Python 3.13
- PostgreSQL 16 with PostGIS 3.4. The extension is not optional and the error
  you get without it is unhelpful, so `make check-db` verifies it before
  anything else runs.
- Redis 7, for the Celery queue and the distance matrix cache.

### First run

    cp .env.example .env
    make db                # creates the database and enables postgis
    make check-db          # fails loudly if the extension is missing
    venv/bin/python manage.py migrate
    venv/bin/python manage.py load_fixtures --set=dev
    make run

`load_fixtures --set=dev` gives you one depot in Melbourne, one in Perth, 240
stops and four vehicles. The Perth depot is there on purpose: it is the only
way to notice locally that you have resolved a service window against the
server clock instead of the depot.

### Solving a run by hand

    venv/bin/python manage.py solve_run --depot=MEL01 --date=today --explain

`--explain` prints the matrix build time and the solve time separately, which
is the first number to look at whenever somebody says the planner is slow.

### The fixtures, and which one to trust

| Fixture | Stops | Shape | Use it for |
|---------|-------|-------|------------|
| `dev` | 240 | Two depots, mixed | Everyday development |
| `synthetic_200` | 200 | Uniformly distributed | Quick comparisons only |
| `regression_50` | 50 | Fixed, hand-checked | Asserting plan output has not changed |

`synthetic_200` is uniformly distributed, which gives the distance matrix cache
a hit rate around 94 per cent. Real customer data clusters and sits nearer 60
per cent. Never quote a performance number measured on `synthetic_200` as
though it were a production number.

### Before you open a pull request

    make lint test

The definition of done is in `docs/engineering-standards.md` and it is not
advisory: tests, migrations, documentation and a rollback note, or the pull
request is not ready for review.
''',
    },
]

_REVIEW_FINDINGS = [
    {'severity': 'blocker',
     'file': 'routing/api/v2/views.py',
     'line': 88,
     'note': 'The plan endpoint takes the stop list from the request body and '
             'passes it to solve_run without checking that every stop belongs '
             'to the requested depot. A caller can plan another account stops '
             'by sending their identifiers.',
     'suggestion': 'Filter the stop queryset by depot__account=request.account '
                   'and reject any identifier not returned by that filter, '
                   'naming the count rather than the identifiers in the 400.'},
    {'severity': 'major',
     'file': 'routing/solver/matrix.py',
     'line': 142,
     'note': 'The distance matrix cache key is built from the depot id and the '
             'service date but not from the stop set. Adding a stop to an '
             'existing run therefore serves a stale matrix, and the plan '
             'silently omits the new stop.',
     'suggestion': 'Include a digest of the sorted stop identifiers in the key. '
                   'A sha1 of the joined identifiers is cheap next to the '
                   'matrix build it protects.'},
    {'severity': 'minor',
     'file': 'routing/api/v2/serializers.py',
     'line': 51,
     'note': 'service_window_start is serialised with isoformat() on a naive '
             'datetime in three places, so the response carries no offset. '
             'Clients that guess will guess wrong, which is the whole cause of '
             'the driver dashboard date bug.',
     'suggestion': 'Use DateTimeField with the depot zone, and add a test '
                   'asserting the rendered string ends in an offset.'},
    {'severity': 'nitpick',
     'file': 'routing/solver/heuristic.py',
     'line': 207,
     'note': 'The nested comprehension building candidate_pairs is four lines '
             'long and does three things. It is correct; it is also the part of '
             'this file nobody will want to touch in six months.',
     'suggestion': 'Split into a named generator, candidate_pairs(stops), with '
                   'the ordering rule stated in one comment.'},
]


def _seed_code_artefacts(project, items, developer):
    created = 0
    for spec in _ARTEFACTS:
        artefact, was_created = CodeArtifact.objects.get_or_create(
            title=spec['title'],
            defaults={
                'kind': spec['kind'],
                'language': spec['language'],
                'content': spec['content'].strip(),
                'explanation': spec['explanation'],
                'suggested_path': spec['path'],
                'repository': project.repository,
                'work_item': items.get(spec['item']),
                'project': project,
                'status': spec['status'],
                # A dict-shaped JSON field, so the provenance marker lives
                # inside the row here rather than only in the manifest.
                'metadata': {'demo_seed': True,
                             'produced_by': 'Software Development',
                             'sprint': 'Sprint 13'},
                'created_by_agent': developer,
            },
        )
        if was_created:
            created += 1
            _remember(artefact)
            _backdate(artefact, _at(spec['days_ago'], hour=13, minute=5))

    review, was_created = CodeReview.objects.get_or_create(
        title='Review of PR 214: v2 planning API and matrix cache',
        defaults={
            'repository': project.repository,
            'pull_request_number': 214,
            'summary':
                'Four findings, one of which blocks. The account isolation gap '
                'on the plan endpoint is the only one that matters today: as '
                'written, a caller can plan against another account stop '
                'identifiers, which is a data exposure rather than a bug. The '
                'stale matrix cache key is the one that would have cost the '
                'most to find later, because it fails silently and produces a '
                'plan that is merely wrong rather than an error anybody sees.\n\n'
                'The performance work itself is good and the profile attached '
                'to the pull request supports the claim. Nothing here is a '
                'reason to redo the approach.',
            'findings': _REVIEW_FINDINGS,
            'verdict': 'request_changes',
            'work_item': items.get('A3'),
            'created_by_agent': developer,
        },
    )
    if was_created:
        created += 1
        _remember(review)
        _backdate(review, _at(4, hour=16, minute=40))
    return created


def _seed_sprint_report(project, sprint, manager):
    """A closing report whose metrics are computed, never asserted.

    Every figure below comes out of a query against the items that were
    actually created, so the report cannot claim a velocity the board does not
    show. That is the difference between a report and a decoration.
    """
    if sprint is None:
        return 0
    title = f'{sprint.name} closing report -- Route Optimisation v2'
    if SprintReport.objects.filter(title=title).exists():
        return 0

    in_sprint = list(sprint.work_items.all())
    committed = sum(i.estimate_points or 0 for i in in_sprint)
    done = [i for i in in_sprint if i.status == 'done']
    completed = sum(i.estimate_points or 0 for i in done)
    carried = [i for i in in_sprint if i.status != 'done']
    carried_points = sum(i.estimate_points or 0 for i in carried)
    percent = int(round(100.0 * completed / committed)) if committed else 0

    metrics = {
        'demo_seed': True,
        'items_committed': len(in_sprint),
        'items_completed': len(done),
        'items_carried': len(carried),
        'points_committed': committed,
        'points_completed': completed,
        'points_carried': carried_points,
        'capacity_points': sprint.capacity_points,
        'completion_percent': percent,
        'blockers_hit': 1,
    }

    lines = [
        f'{sprint.name} ran from '
        f'{sprint.start_date:%d %b} to {sprint.end_date:%d %b}.',
        '',
        f'Goal: {sprint.goal}',
        '',
        f'The goal was met. {len(done)} of {len(in_sprint)} items closed, '
        f'{completed} of {committed} points, which is {percent} per cent of '
        f'what was committed against a capacity of {sprint.capacity_points}.',
        '',
        'Completed:',
    ]
    for item in sorted(done, key=lambda i: i.sequence_hint):
        lines.append(f'  {item.reference}  {item.title} '
                     f'({item.estimate_points} pts)')
    lines.append('')
    if carried:
        lines.append(f'Carried into the next sprint ({carried_points} pts):')
        for item in sorted(carried, key=lambda i: i.sequence_hint):
            lines.append(f'  {item.reference}  {item.title} '
                         f'({item.estimate_points} pts, {item.status})')
        lines.append('')
        lines.append(
            'The carry-over is honest rather than accidental. Structured '
            'logging was the lowest-value item in the sprint and it was the '
            'one dropped when the fleet API retry work turned out to need the '
            'idempotency key it did not have. It has since become more '
            'valuable, because the export timeout was diagnosed from customer '
            'prose rather than from our own logs.')
    lines += [
        '',
        'What went well:',
        '  The ingest landed end to end and the PostGIS indexes went in '
        'concurrently with no lock held on a 41 million row table.',
        '  The Django 6.1 upgrade was uneventful, which is the correct outcome '
        'for an upgrade.',
        '',
        'What did not:',
        '  One blocker cost most of a day. The fleet API retry work was '
        'started before anybody had established that the upstream create was '
        'not idempotent, and the duplicate stops bug is the consequence.',
        '  Nobody noticed the export timeout until customers reported it '
        'three times, because the worker emits no log line on failure.',
        '',
        'Carried assumptions worth re-testing in Sprint 13:',
        '  That the synthetic fixture is representative. Cache hit rate on it '
        'is around 94 per cent against an estimated 60 per cent in '
        'production, so every performance number measured this sprint should '
        'be treated as an upper bound.',
    ]

    report = SprintReport.objects.create(
        kind='sprint',
        title=title,
        project=project,
        sprint=sprint,
        body='\n'.join(lines),
        metrics=metrics,
        period_start=sprint.start_date,
        period_end=sprint.end_date,
        created_by_agent=manager,
    )
    _remember(report)
    _backdate(report, _at(14, hour=17, minute=15))
    return 1


# ===========================================================================
# 5. SUPPORT
# ===========================================================================

_CUSTOMERS = [
    ('Kembla Freight Group', 'operations@kemblafreight.com.au', '+61 2 4223 8890',
     'Kembla Freight Group Pty Ltd', 'enterprise', 'NW-1042',
     'Australia/Sydney',
     'Largest account. 38 vehicles, four depots, 1,400 stops a day out of '
     'Port Kembla. Renewal in March. Their operations manager, who is the '
     'only person who reports faults, is on leave until Monday.'),
    ('Yarra Valley Produce Co', 'support@yvproduce.com.au', '+61 3 5962 4417',
     'Yarra Valley Produce Co', 'enterprise', 'NW-1088',
     'Australia/Melbourne',
     'Cold chain, 22 vehicles, tight morning delivery windows into the '
     'wholesale market. Very tolerant of a slow fix and completely intolerant '
     'of a wrong plan.'),
    ('Bendigo Cold Chain', 'help@bendigocoldchain.com.au', '+61 3 5443 2201',
     'Bendigo Cold Chain Logistics', 'premium', 'NW-1121',
     'Australia/Melbourne',
     'Nine vehicles, regional Victoria. Runs the quarterly reporting export '
     'themselves rather than asking us, which is how they found the timeout.'),
    ('Sandbelt Couriers', 'admin@sandbeltcouriers.com.au', '+61 3 9584 7712',
     'Sandbelt Couriers', 'standard', 'NW-1163',
     'Australia/Melbourne',
     'Metropolitan courier, 14 vehicles, drivers start at 4am. The early '
     'start is why they found the dashboard date bug.'),
    ('Ashgrove Wholesale Supplies', 'accounts@ashgrovewholesale.com.au',
     '+61 7 3355 1180', 'Ashgrove Wholesale Supplies', 'standard', 'NW-1194',
     'Australia/Brisbane',
     'Queensland wholesaler, six vehicles. Note the Brisbane timezone: they '
     'do not observe daylight saving and half our scheduling confusion with '
     'them comes from that.'),
    ('Two Bays Distribution', 'hello@twobaysdist.com.au', '+61 3 5981 6624',
     'Two Bays Distribution', 'free', 'NW-1240',
     'Australia/Melbourne',
     'Free tier, three vehicles, Mornington Peninsula. Evaluating. Moved off '
     'the legacy XML feed last month.'),
]

_TICKET_TAGS = [
    ('billing-error', 'A charge the customer did not expect or authorise.',
     '#dc2626'),
    ('export-timeout', 'The recurring route export timeout. Grouped so the '
     'recurring-problem report can count it.', '#ea580c'),
    ('data-loss', 'Any report of records missing or changed without an '
     'action. Never answered without a person.', '#7f1d1d'),
    ('out-of-policy', 'A request the documented policy declines.', '#a16207'),
    ('answered-from-kb', 'Resolved by quoting an existing document, and '
     'therefore a candidate for the FAQ.', '#0f766e'),
]

# Twelve tickets that read like one morning's queue. Priority is set from
# impact rather than from tone throughout, which is the whole point of two of
# these rows: the furious delivery complaint is normal priority and the
# perfectly calm data-loss report is urgent.
#
# 'key' becomes source_message_id, which is the natural key an inbound mail
# gateway would supply and therefore the honest thing to key idempotency on.
_TICKETS = [
    {
        'key': 'demo-nw-8801',
        'customer': 'Kembla Freight Group',
        'subject': 'We have been charged twice for the August invoice',
        'category': 'billing', 'priority': 'high', 'status': 'open',
        'sentiment': 'negative', 'channel': 'email',
        'days_ago': 4, 'sla_hours': -6, 'tags': ['billing-error'],
        'signals': ['charged twice', 'invoice 4471', 'AUD 8,940',
                    'need this back before month end'],
        'assignee': 'Nadia Halloran',
        'body':
            'Morning,\n\nOur August invoice 4471 for AUD 8,940 has been debited '
            'twice, on the 2nd and again on the 4th. Both show against the same '
            'invoice number in our banking. I have attached both transaction '
            'lines.\n\nWe need the duplicate returned before month end because '
            'our finance close is on the 12th and this will sit as an '
            'unreconciled item until it is resolved.\n\nCan you confirm today '
            'that you can see the double charge at your end.\n\nRegards\n'
            'Operations, Kembla Freight Group',
        'messages': [
            ('inbound', 'Kembla Freight Group', False, None),
            ('internal', 'Customer Support (AI)', False,
             'Confirmed against the billing export: invoice 4471 appears twice '
             'with the same reference, 2 and 4 September, AUD 8,940 each. This '
             'is a genuine duplicate debit and not a customer misreading.\n\n'
             'Not drafting a reply that promises a refund date. The refund '
             'policy states an approved refund is processed to the original '
             'payment method within 5 business days and the bank may take a '
             'further 3 to 5 to show it, and a duplicate debit of this size at '
             'our largest account should be committed to by a person rather '
             'than by me.'),
            ('outbound', 'Nadia Halloran', False,
             'Hello,\n\nYes -- I can see both debits against invoice 4471 and '
             'you are right that the second one should not have happened. I am '
             'sorry.\n\nI have raised the reversal with our finance team this '
             'morning. It goes back to the card the original payment came from '
             'and takes up to five business days at our end, and your bank may '
             'take a further three to five to show it. That means it should be '
             'visible to you by the 15th at the latest, which I know is after '
             'your close on the 12th.\n\nSo that the close is not held up: I '
             'can send you a written confirmation of the reversal today with '
             'the amount and the reference, which your finance team can carry '
             'as a receivable rather than an unreconciled item. Say the word '
             'and it will be with you within the hour.\n\nI will also tell you '
             'what caused it once I know, rather than leaving you to wonder.\n\n'
             'Kind regards\nNadia Halloran\nSupport Lead, Northwind Systems'),
        ],
    },
    {
        'key': 'demo-nw-8802',
        'customer': 'Sandbelt Couriers',
        'subject': 'Refund for the July subscription -- 44 days',
        'category': 'refund', 'priority': 'normal', 'status': 'pending',
        'sentiment': 'neutral', 'channel': 'email',
        'days_ago': 3, 'sla_hours': 26, 'tags': ['out-of-policy'],
        'signals': ['44 days', 'refund', 'did not use it'],
        'assignee': 'Joshua Kirkbride',
        'body':
            'Hi,\n\nWe would like a refund on our July subscription. We barely '
            'used the platform in July because two of our vehicles were off the '
            'road and we planned manually.\n\nI realise it is a bit late -- it '
            'is 44 days since the July invoice. Is there anything you can do.\n\n'
            'Thanks\nAdmin, Sandbelt Couriers',
        'messages': [
            ('inbound', 'Sandbelt Couriers', False, None),
            ('internal', 'Customer Support (AI)', False,
             'Out of window. The refund policy sets 30 calendar days from the '
             'purchase date for subscriptions, and this is day 44. The policy '
             'also states plainly that a support representative may not grant '
             'an out-of-window refund and that the Support Lead may approve one '
             'where the delay was caused by the company, by a carrier, or by '
             'documented illness or bereavement. None of those applies here: '
             'the reason given is the customer own vehicles being off the '
             'road.\n\nAlso relevant: light usage is not a refund ground under '
             'this policy at all, in or out of window.\n\nDrafted a decline '
             'below for Nadia to approve or amend. Not sending it, because a '
             'decline to a paying customer is not mine to send.'),
            ('outbound', 'Customer Support (AI)', True,
             'Hi,\n\nThank you for being straight with me about the timing -- '
             'it makes this easier to answer honestly.\n\nI am not able to '
             'refund the July subscription. Our refund window for '
             'subscriptions is 30 calendar days from the invoice date, and we '
             'are at day 44. The window is the same for everybody, which is '
             'the only way it stays fair.\n\nI should also be honest about the '
             'second part: light usage in a period is not something the policy '
             'refunds, even inside the window, because the subscription pays '
             'for the availability of the platform rather than for the runs '
             'planned on it.\n\nWhat I can do is more useful than a refund '
             'anyway. If vehicles are off the road again, tell us and we can '
             'move you down a vehicle tier for the affected months, which '
             'lowers the invoice rather than refunding it afterwards. Two of '
             'our courier customers do exactly that over winter.\n\nWould you '
             'like me to look at what that would have cost you for July, so '
             'you can see the difference.\n\nKind regards\nCustomer Support\n'
             'Northwind Systems'),
        ],
    },
    {
        'key': 'demo-nw-8803',
        'customer': 'Kembla Freight Group',
        'subject': 'Route export never finishes for a six month range',
        'category': 'technical', 'priority': 'high', 'status': 'open',
        'sentiment': 'neutral', 'channel': 'email',
        'days_ago': 8, 'sla_hours': 30, 'tags': ['export-timeout'],
        'signals': ['never finishes', 'six months', 'spinner',
                    'been happening for a fortnight'],
        'assignee': 'Nadia Halloran',
        'body':
            'Hello,\n\nWhen I try to export our completed runs for the last six '
            'months the page just spins. It has been doing this for about a '
            'fortnight. I left it for forty minutes yesterday and it was still '
            'spinning.\n\nA single month comes out fine.\n\nWe need the six '
            'month file for a customer tender.\n\nThanks',
        'messages': [
            ('inbound', 'Kembla Freight Group', False, None),
            ('internal', 'Customer Support (AI)', False,
             'Third report of this shape. Same signature each time: a range '
             'over roughly three months, an enterprise account, and a spinner '
             'rather than an error. Filed against the engineering board and '
             'linked to the export timeout bug.\n\nThe pattern matters more '
             'than the individual ticket: because the front end shows a '
             'spinner instead of a failure, customers do not experience this '
             'as a fault and do not report it promptly. Two of the three had '
             'been living with it for a fortnight.'),
            ('outbound', 'Nadia Halloran', False,
             'Hello,\n\nThank you, and I am sorry it took three customers '
             'telling us before we understood this properly.\n\nWhat is '
             'happening is not a slow export -- it is a failure that our page '
             'is showing you as a spinner, which is why it never resolves no '
             'matter how long you wait. Beyond about ninety days of runs on an '
             'account your size the export exceeds a limit at our end and '
             'stops, and the browser is never told.\n\nTwo things from us. '
             'This week we are making it refuse clearly instead of spinning, '
             'so you get an answer in seconds. Next sprint we are rewriting '
             'the export so the range does not matter at all.\n\nFor the '
             'tender: send me the exact date range and I will produce the six '
             'month file for you from our side today. Do not wait for the '
             'fix.\n\nKind regards\nNadia Halloran\nSupport Lead, Northwind '
             'Systems'),
        ],
    },
    {
        'key': 'demo-nw-8804',
        'customer': 'Yarra Valley Produce Co',
        'subject': 'Quarterly export times out every time',
        'category': 'technical', 'priority': 'high', 'status': 'open',
        'sentiment': 'negative', 'channel': 'web',
        'days_ago': 6, 'sla_hours': -3, 'tags': ['export-timeout'],
        'signals': ['every time', 'quarterly', 'board pack due Friday'],
        'assignee': 'Joshua Kirkbride',
        'body':
            'The quarterly run export times out every single time. Three '
            'attempts this morning. Our board pack is due Friday and this '
            'report goes in it.\n\nThis worked in June.',
        'messages': [
            ('inbound', 'Yarra Valley Produce Co', False, None),
            ('internal', 'Customer Support (AI)', False,
             'Second report of the export timeout, and the "this worked in '
             'June" line is the useful part: a quarterly range crossed the '
             'ninety day threshold when the quarter did. Linked to the same '
             'engineering item. SLA target has passed on this one.'),
        ],
    },
    {
        'key': 'demo-nw-8805',
        'customer': 'Bendigo Cold Chain',
        'subject': 'CSV export spins and then fails',
        'category': 'technical', 'priority': 'normal', 'status': 'open',
        'sentiment': 'neutral', 'channel': 'chat',
        'days_ago': 2, 'sla_hours': 34, 'tags': ['export-timeout'],
        'signals': ['spins', 'then fails', 'four months'],
        'assignee': 'Joshua Kirkbride',
        'body':
            'Trying to pull four months of runs to CSV. It spins for about a '
            'minute then the download never arrives. One month is instant.\n\n'
            'Not urgent, just letting you know.',
        'messages': [
            ('inbound', 'Bendigo Cold Chain', False, None),
            ('outbound', 'Joshua Kirkbride', False,
             'Thanks for telling us -- and for the detail that one month is '
             'instant, which is exactly what we needed to confirm the '
             'cause.\n\nYou are the third customer to hit this and it is a '
             'fault at our end, not a slow report. It affects ranges over '
             'roughly ninety days. Engineering has it, the temporary fix lands '
             'this week and the real one next sprint.\n\nIf you need the four '
             'month file before then, say so and we will produce it for you.\n\n'
             'Joshua'),
            ('internal', 'Customer Support (AI)', False,
             'Third occurrence in eight days. This is now the recurring '
             'problem with the highest volume in the queue and the '
             'recurring-problem report should be naming it.'),
        ],
    },
    {
        'key': 'demo-nw-8806',
        'customer': 'Ashgrove Wholesale Supplies',
        'subject': 'ABSOLUTELY UNACCEPTABLE -- delivery two hours late',
        'category': 'delivery', 'priority': 'normal', 'status': 'resolved',
        'sentiment': 'angry', 'channel': 'email',
        'days_ago': 9, 'sla_hours': 40, 'tags': [],
        'signals': ['absolutely unacceptable', 'two hours late',
                    'considering our options', 'nobody told us'],
        'assignee': 'Joshua Kirkbride',
        'resolution':
            'The delay was the carrier, not the plan. The planned window was '
            '9.00 to 9.30 and the vehicle departed the depot 95 minutes late '
            'for a reason recorded in their own dispatch notes. The gap worth '
            'apologising for is real but different: we do not notify a '
            'customer when a vehicle departs outside its planned window, so '
            'nobody told them. Raised as a product request; the customer '
            'accepted the explanation.',
        'body':
            'This is ABSOLUTELY UNACCEPTABLE. Our Tuesday delivery arrived two '
            'hours late and nobody told us anything. We had staff standing '
            'around waiting. We are considering our options with another '
            'provider.\n\nSomeone needs to explain themselves.',
        'messages': [
            ('inbound', 'Ashgrove Wholesale Supplies', False, None),
            ('internal', 'Customer Support (AI)', False,
             'Priority set to normal, not urgent, and that is deliberate. The '
             'SLA policy states priority is set from the actual impact rather '
             'than the tone of the message: one delivery, one occurrence, '
             'delivered, no data affected, no service unavailable. That is a '
             'Priority 3 by the published definitions no matter how the email '
             'is written.\n\nThe tone still matters for how we reply, just not '
             'for where it sits in the queue. Checked the run: planned window '
             '9.00 to 9.30, vehicle departed the depot 95 minutes late per the '
             'carrier own notes. The plan was correct.\n\nThe genuine finding '
             'is that we never told them. We have no departure-variance '
             'notification at all.'),
            ('outbound', 'Joshua Kirkbride', False,
             'Hello,\n\nYou are right to be annoyed about the part that was '
             'ours, so let me separate the two things.\n\nThe delivery itself: '
             'the run was planned for a 9.00 to 9.30 arrival and the vehicle '
             'left the depot 95 minutes behind, which is recorded in the '
             'carrier dispatch notes. The plan was right; the departure was '
             'not, and that sits with the carrier.\n\nThe part that is ours is '
             'the one you actually raised: nobody told you. We do not '
             'currently notify a customer when a vehicle departs outside its '
             'planned window, which means you found out by having staff stand '
             'and wait. That is a fair criticism and I have raised it as a '
             'product request with your name on it.\n\nI am sorry about the '
             'waiting. If you would like, I can set up a short call with our '
             'Support Lead to go through what notification would look like.\n\n'
             'Joshua Kirkbride\nCustomer Support, Northwind Systems'),
        ],
    },
    {
        'key': 'demo-nw-8807',
        'customer': 'Yarra Valley Produce Co',
        'subject': 'Some historical runs appear to be missing',
        'category': 'technical', 'priority': 'urgent', 'status': 'escalated',
        'sentiment': 'neutral', 'channel': 'email',
        'days_ago': 1, 'sla_hours': -2, 'tags': ['data-loss'],
        'signals': ['missing', 'were there last week', 'audit'],
        'needs_human': True,
        'escalated_to': 'Head of Customer Operations, and Engineering on-call',
        'escalation_reason':
            'Reported loss of historical records. The SLA and escalation '
            'policy names data loss as an immediate handover: a suspected data '
            'breach, a safety issue, a legal threat, a media enquiry, a '
            'bereavement or a vulnerable customer are handed to a person '
            'immediately and never answered by an AI employee, and no draft '
            'reply is prepared for them. So none has been.\n\n'
            'What has been done instead is fact-gathering with no customer '
            'contact: the account has 412 runs before 1 July and 0 between 1 '
            'July and 14 July, which is consistent with the customer account '
            'rather than with a misreading. That is a real gap and it needs '
            'engineering and the Head of Customer Operations now, not a reply '
            'from me.\n\nPriority is urgent despite the completely calm tone, '
            'per the same policy: a calm report of lost data outranks an angry '
            'message about a slow reply.',
        'assignee': 'Nadia Halloran',
        'body':
            'Good afternoon,\n\nWe are preparing for an audit and have noticed '
            'that our completed runs for the first two weeks of July do not '
            'appear in the platform. Runs before and after that period are '
            'there. I am fairly sure they were visible last week because I '
            'used them for the June-July comparison.\n\nThis is not urgent for '
            'today but we will need them for the audit in three weeks.\n\n'
            'Kind regards',
        'messages': [
            ('inbound', 'Yarra Valley Produce Co', False, None),
            ('internal', 'Customer Support (AI)', False,
             'Handed to a person without a draft reply, per the escalation '
             'policy. Facts gathered, no customer contact made:\n\n'
             '- 412 runs exist for this account before 1 July.\n'
             '- 0 runs exist between 1 and 14 July.\n'
             '- term runs resume from 15 July.\n'
             '- The customer statement that they were visible last week is '
             'consistent with a deletion or a failed migration rather than a '
             'never-ingested period.\n\n'
             'Escalated to the Head of Customer Operations and engineering '
             'on-call. Nothing has been sent to the customer and nothing '
             'should be sent by me.'),
            ('internal', 'Nadia Halloran', False,
             'Taken. I have called their operations manager rather than '
             'emailing -- an audit and missing records is not a thread. '
             'Engineering on-call is checking the July migration window. Do '
             'not send anything on this ticket without me.'),
        ],
    },
    {
        'key': 'demo-nw-8808',
        'customer': 'Kembla Freight Group',
        'subject': 'Can we get a webhook when a run completes',
        'category': 'feature_request', 'priority': 'low', 'status': 'open',
        'sentiment': 'positive', 'channel': 'email',
        'days_ago': 12, 'sla_hours': 96, 'tags': [],
        'signals': ['would be useful', 'no rush'],
        'assignee': 'Nadia Halloran',
        'body':
            'Not a problem, a request. It would be genuinely useful if the '
            'platform could call a URL of ours when a run completes, so we can '
            'close the job in our own system without polling you every five '
            'minutes.\n\nNo rush at all.\n\nThanks, and the planner has been '
            'good this month.',
        'messages': [
            ('inbound', 'Kembla Freight Group', False, None),
            ('outbound', 'Nadia Halloran', False,
             'Thanks -- and thank you for the kind word about the planner, '
             'which I have passed to the team.\n\nRun-completion webhooks are '
             'on the roadmap and not scheduled yet, so I will not give you a '
             'date I cannot keep. I have recorded the request against your '
             'account with the detail about closing the job in your own '
             'system, because that use case is more specific than the request '
             'we already had.\n\nIn the meantime, if the polling is costing '
             'you anything, tell me the interval you are using and I will '
             'check whether there is a cheaper endpoint for it.\n\nNadia'),
        ],
    },
    {
        'key': 'demo-nw-8809',
        'customer': 'Two Bays Distribution',
        'subject': 'Cannot sign in since the single sign-on change',
        'category': 'account', 'priority': 'high', 'status': 'resolved',
        'sentiment': 'negative', 'channel': 'phone',
        'days_ago': 7, 'sla_hours': 20, 'tags': [],
        'signals': ['cannot sign in', 'all three of us', 'since yesterday'],
        'assignee': 'Joshua Kirkbride',
        'resolution':
            'Their three accounts were created before single sign-on was '
            'introduced and were never migrated to it, so the new sign-in flow '
            'had nothing to match them against. Migrated all three, confirmed '
            'sign-in with the customer on the phone. Root cause raised '
            'internally: the migration script skipped accounts with no '
            'verified email, and nothing reported the skips.',
        'body':
            'Phone call, transcribed. None of their three users can sign in '
            'since yesterday. They get "we could not verify your account". '
            'Tried password reset, no email arrives.',
        'messages': [
            ('inbound', 'Two Bays Distribution', False, None),
            ('internal', 'Customer Support (AI)', False,
             'All three accounts predate the single sign-on migration and are '
             'not in the identity provider. The password reset email does not '
             'arrive because the address on the account was never verified, '
             'which is also the reason the migration script skipped them.\n\n'
             'Worth noting that nothing alerted anybody about the skipped '
             'accounts, so the only way this surfaces is a customer being '
             'locked out. There may be others.'),
            ('outbound', 'Joshua Kirkbride', False,
             'Hello,\n\nSorted -- all three of you should be able to sign in '
             'now. Please try and tell me if not.\n\nWhat happened: your '
             'accounts were set up before we moved to the new sign-in system '
             'and they were missed in the move, so the new sign-in had nothing '
             'to check them against. The reset email did not arrive for the '
             'same underlying reason.\n\nThat was our error and I am sorry it '
             'cost you a day. I have raised it internally because if it '
             'happened to your three accounts it may have happened to others, '
             'and I would rather we find them than wait for the phone to '
             'ring.\n\nJoshua'),
        ],
    },
    {
        'key': 'demo-nw-8810',
        'customer': 'Two Bays Distribution',
        'subject': 'How many stops can one route hold',
        'category': 'other', 'priority': 'low', 'status': 'closed',
        'sentiment': 'neutral', 'channel': 'chat',
        'days_ago': 11, 'sla_hours': 60, 'tags': ['answered-from-kb'],
        'signals': [],
        'assignee': 'Joshua Kirkbride',
        'resolution':
            'Answered from the product overview document. Added to the FAQ '
            'because it is the fourth time it has been asked.',
        'body': 'Quick one -- is there a limit on how many stops we can put in '
                'a single route.',
        'messages': [
            ('inbound', 'Two Bays Distribution', False, None),
            ('outbound', 'Customer Support (AI)', False,
             'Yes, and it is a practical limit rather than a hard one.\n\nA '
             'single route is capped at 250 stops in the planner. Above that '
             'the plan is split across vehicles automatically, which is almost '
             'always what you want anyway -- one driver with 300 stops is not '
             'a route, it is two days.\n\nOn your plan you have three '
             'vehicles, so the effective ceiling is 750 stops a day.\n\nSource: '
             'Product Overview, section on planning limits. Happy to send you '
             'the page if useful.'),
        ],
    },
    {
        'key': 'demo-nw-8811',
        'customer': 'Kembla Freight Group',
        'subject': 'Export still timing out',
        'category': 'technical', 'priority': 'normal', 'status': 'closed',
        'sentiment': 'neutral', 'channel': 'email',
        'days_ago': 5, 'sla_hours': 48, 'tags': ['export-timeout'],
        'signals': ['still'],
        'duplicate_of': 'demo-nw-8803',
        'assignee': 'Joshua Kirkbride',
        'resolution':
            'Duplicate of the existing export timeout ticket from the same '
            'account. Merged rather than answered separately, so the '
            'conversation stays in one place and the recurring-problem count '
            'is not inflated by one customer writing twice.',
        'body':
            'Following up -- the export is still timing out. Same as my earlier '
            'message.',
        'messages': [
            ('inbound', 'Kembla Freight Group', False, None),
            ('internal', 'Customer Support (AI)', False,
             'Same account, same subject matter and same signature as the '
             'earlier export ticket. Marked as a duplicate and pointed at the '
             'original rather than answered here.\n\nWorth saying why this '
             'matters beyond tidiness: if this were counted as a fourth '
             'occurrence, the recurring-problem report would say four '
             'customers when the truth is three. A report that overstates is '
             'no more useful than one that understates.'),
        ],
    },
    {
        'key': 'demo-nw-8812',
        'customer': 'Ashgrove Wholesale Supplies',
        'subject': 'Complaint about how long the last ticket took',
        'category': 'complaint', 'priority': 'normal', 'status': 'waiting_customer',
        'sentiment': 'negative', 'channel': 'email',
        'days_ago': 3, 'sla_hours': 44, 'tags': [],
        'signals': ['four days', 'had to chase', 'not good enough'],
        'assignee': 'Nadia Halloran',
        'body':
            'I want to raise a complaint about the handling of our delivery '
            'issue. It took four days and I had to chase twice. That is not '
            'good enough for what we pay.',
        'messages': [
            ('inbound', 'Ashgrove Wholesale Supplies', False, None),
            ('internal', 'Customer Support (AI)', False,
             'Checked before replying, because a complaint about response time '
             'should be answered with the actual timeline rather than an '
             'apology in general terms.\n\nThe delivery ticket was Priority 3 '
             'with an 8 business hour first response target. First response '
             'was 6 business hours, within target. Resolution took 4 calendar '
             'days against a 3 business day target, so the resolution target '
             'was met with a weekend in the middle -- but the customer '
             'experienced four days and chased twice, and both of those are '
             'true.\n\nThe escalation policy sends any customer request for a '
             'manager to the Support Lead, so this is Nadia rather than a '
             'reply from me.'),
            ('outbound', 'Nadia Halloran', False,
             'Hello,\n\nThank you for raising it, and I would rather have the '
             'complaint than the quiet dissatisfaction.\n\nI have looked at '
             'the actual timeline rather than guessing. Your first reply came '
             'within six working hours against a target of eight, and the '
             'resolution came inside our three business day target -- but a '
             'weekend sat in the middle of it, so what you experienced was '
             'four days and two chases. Both of those are real and a target '
             'being met is not the same as you being well served.\n\nThe '
             'chasing is the part I am not comfortable with. You should not '
             'have had to ask twice, and the reason you did is that we do not '
             'send progress updates on a Priority 3 unless something changes. '
             'I am changing that for your account: you will get an update '
             'every working day on any open ticket, whether or not there is '
             'news.\n\nIs there anything about the delivery issue itself still '
             'unresolved.\n\nNadia Halloran\nSupport Lead, Northwind Systems'),
        ],
    },
]



def _safe_email(address):
    """Force an address onto a reserved domain that can reach nobody.

    The Gmail connector on a machine with real credentials genuinely sends.
    Every address the demonstration writes into a sendable payload therefore
    lives on example.com, example.org or example.net, which RFC 2606 reserves
    for exactly this purpose: they cannot receive mail. The local part is kept,
    so the data still reads like a company; only the domain changes.
    """
    address = (address or '').strip()
    if '@' not in address:
        return address
    local, _at_sign, _domain = address.rpartition('@')
    return f'{local}@example.com'


def seed_support(owner=None):
    """Six customers, twelve tickets with their threads, and one report.

    Keyed on the customer email address, the tag name and the ticket's
    ``source_message_id`` -- the natural key an inbound mail gateway would
    supply. Messages are written only when the ticket itself was just created,
    since a thread has no natural key of its own.
    """
    support = _agents().get('support')
    created = 0

    tags = {}
    for name, description, colour in _TICKET_TAGS:
        tag, was_created = TicketTag.objects.get_or_create(
            name=name, defaults={'description': description, 'colour': colour})
        tags[name] = tag
        if was_created:
            created += 1
            _remember(tag)

    customers = {}
    for (name, email, phone, company, tier, reference, tz_name, notes) in _CUSTOMERS:
        customer, was_created = Customer.objects.get_or_create(
            email=_safe_email(email),
            defaults={'name': name, 'phone': phone, 'company': company, 'tier': tier,
                      'account_reference': reference, 'timezone_name': tz_name,
                      'notes': notes})
        customers[name] = customer
        if was_created:
            created += 1
            _remember(customer)
            _backdate(customer, _at(60 + len(customers) * 9, hour=9))

    by_key = {}
    fresh = []
    for spec in _TICKETS:
        customer = customers.get(spec['customer'])
        opened = _at(spec['days_ago'], hour=8 + (spec['days_ago'] % 6), minute=15)
        status = spec['status']
        ticket, was_created = SupportTicket.objects.get_or_create(
            source_message_id=spec['key'],
            defaults={
                'customer': customer,
                'contact_email': customer.email if customer else '',
                'subject': spec['subject'], 'body': spec['body'],
                'channel': spec.get('channel', 'email'),
                'category': spec['category'], 'priority': spec['priority'],
                'status': status, 'sentiment': spec['sentiment'],
                'urgency_signals': spec.get('signals', []),
                'assigned_agent': support,
                'needs_human': bool(spec.get('needs_human')),
                'escalated_to': spec.get('escalated_to', ''),
                'escalation_reason': spec.get('escalation_reason', ''),
                'resolution': spec.get('resolution', ''),
                'sla_due_at': timezone.now() + timedelta(hours=spec.get('sla_hours', 24)),
                'created_by_agent': support,
            })
        by_key[spec['key']] = ticket
        if not was_created:
            continue
        created += 1
        _remember(ticket)
        _backdate(ticket, opened)
        fresh.append((spec, ticket, opened))

        if tag_names := spec.get('tags'):
            ticket.tags.set(tags[n] for n in tag_names if n in tags)

        when = opened
        first_reply = None
        for direction, author, is_draft, body in spec.get('messages', []):
            when = when + timedelta(hours=2, minutes=10)
            message = TicketMessage.objects.create(
                ticket=ticket, direction=direction, author_label=author,
                body=body if body else spec['body'], is_ai_draft=bool(is_draft))
            _remember(message)
            _backdate(message, when)
            if direction == 'outbound' and not is_draft and first_reply is None:
                first_reply = when

        updates = {}
        if first_reply is not None:
            updates['first_response_at'] = first_reply
        if status in ('resolved', 'closed'):
            updates['resolved_at'] = when + timedelta(hours=1)
        if updates:
            SupportTicket.objects.filter(pk=ticket.pk).update(**updates)

    # Duplicates are linked after every ticket exists, because the target may
    # sit later in the fixture than the ticket that duplicates it.
    for spec, ticket, _opened in fresh:
        target = by_key.get(spec.get('duplicate_of', ''))
        if target is not None and target.pk != ticket.pk:
            SupportTicket.objects.filter(pk=ticket.pk).update(duplicate_of=target)

    created += _seed_support_report(support)
    _flush_manifest()
    return created


def _seed_support_report(support):
    """One volume report whose figures are computed from the seeded queue."""
    title = 'Support queue: the last thirty days'
    if SupportReport.objects.filter(title=title).exists():
        return 0

    tickets = SupportTicket.objects.filter(source_message_id__startswith='demo-nw-')
    total = tickets.count()
    if not total:
        return 0

    by_category, by_priority = {}, {}
    for ticket in tickets:
        by_category[ticket.category] = by_category.get(ticket.category, 0) + 1
        by_priority[ticket.priority] = by_priority.get(ticket.priority, 0) + 1
    open_count = tickets.exclude(status__in=('resolved', 'closed')).count()
    breaching = sum(1 for t in tickets if t.is_breaching_sla)
    needs_human = tickets.filter(needs_human=True).count()
    recurring = tickets.filter(tags__name='export-timeout').distinct().count()

    lines = [
        f'{total} tickets in the period, {open_count} still open, {breaching} past '
        f'their SLA target, {needs_human} flagged for a person.',
        '',
        'By category:',
    ]
    lines += [f'  {k:16} {v}' for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])]
    lines += ['', 'By priority:']
    lines += [f'  {k:16} {v}' for k, v in sorted(by_priority.items(), key=lambda kv: -kv[1])]
    lines += [
        '',
        'Recurring problem:',
        f'  The route export timeout appears {recurring} times from different '
        f'customers. That is a product signal, not a support one -- it is the '
        f'same defect Engineering has open, and the count belongs in the sprint '
        f'review.',
        '',
        'Priority was set from impact rather than tone throughout. The loudest '
        'message in the queue is normal priority; the quietest is urgent.',
    ]
    report = SupportReport.objects.create(
        title=title, kind='volume', body='\n'.join(lines),
        metrics={'demo_seed': True, 'total': total, 'open': open_count,
                 'breaching_sla': breaching, 'needs_human': needs_human,
                 'by_category': by_category, 'by_priority': by_priority,
                 'recurring_export_timeout': recurring},
        # SupportReport dates are DateTimeFields, so aware datetimes rather
        # than dates: a naive value here warns and stores an ambiguous instant.
        period_start=_at(30, hour=0), period_end=_at(0, hour=23, minute=59),
        created_by_agent=support)
    _remember(report)
    _backdate(report, _at(1, hour=16, minute=40))
    return 1


# ===========================================================================
# 6. MARKETING CONTENT
# ===========================================================================

_CAMPAIGNS = [
    {
        'name': 'Route Optimisation v2 launch',
        'objective': 'Move the three enterprise accounts waiting on the export fix '
                     'onto v2 within a month of release, and book eight demonstrations '
                     'with fleets of ten or more vehicles.',
        'audience': 'Operations and dispatch managers at Australian freight and '
                    'distribution companies running ten to sixty vehicles.',
        'budget': 14000, 'status': 'active',
        'key_message': 'A 200-stop run planned in under five seconds, so the '
                       'dispatcher never opens the spreadsheet.',
        'channels': ['linkedin', 'email', 'blog'],
        'tone': 'Warm but authoritative -- specific numbers, no hype',
        'proof_points': [
            {'claim': 'A 200-stop run plans in under five seconds on v2.',
             'source': 'Engineering Standards and Definition of Done',
             'document_id': None},
            {'claim': 'Every change to a plan is logged and can be reviewed.',
             'source': 'Product Overview: AI Workforce OS', 'document_id': None},
        ],
        'success_measures': ['Three enterprise migrations completed',
                             'Eight demonstrations booked', 'Export tickets fall to zero'],
        'constraints': 'No performance figure goes out that Engineering has not '
                       'measured on production-shaped data.',
    },
    {
        'name': 'Cold chain spring programme',
        'objective': 'Reach cold-chain operators before the summer peak with the '
                     'delivery-window planning feature.',
        'audience': 'Cold chain and fresh produce distributors in Victoria and NSW.',
        'budget': 6000, 'status': 'draft',
        'key_message': 'Tight morning windows into the wholesale market, planned '
                       'the night before.',
        'channels': ['linkedin', 'instagram', 'email'],
        'tone': 'Warm but authoritative',
        'proof_points': [
            {'claim': 'Customers report fewer missed windows after adopting '
                      'planned sequencing.',
             'source': '', 'document_id': None},
        ],
        'success_measures': ['Forty qualified conversations', 'Two pilot accounts'],
        'constraints': 'The missed-window claim has no source yet and must not '
                       'be published until it has one.',
    },
]

_CONTENT = [
    {
        'campaign': 'Route Optimisation v2 launch', 'channel': 'linkedin', 'kind': 'post',
        'title': 'v2 launch: the eleven-second problem', 'status': 'published',
        'days_ago': 6, 'hashtags': ['logistics', 'routeplanning', 'fleetmanagement',
                                    'dispatch', 'australianbusiness'],
        'sources': ['Product Overview: AI Workforce OS',
                    'Engineering Standards and Definition of Done'],
        'body': (
            'Eleven seconds.\n\nThat is how long the first version of our planner took '
            'to route a 200-stop run, and it is roughly the point at which a '
            'dispatcher stops waiting and opens a spreadsheet instead.\n\nRoute '
            'Optimisation v2 rebuilt the ingest, the solver and the planning API. '
            'The target we set ourselves was five seconds on production-shaped '
            'data, and we are holding to it before we quote it.\n\nEvery change to '
            'a plan is logged and reviewable, because a plan a dispatcher cannot '
            'trust is a plan they will not use.\n\nIf you run ten or more vehicles '
            'out of Melbourne or Sydney, we would like to show you a run of your own.'),
    },
    {
        'campaign': 'Route Optimisation v2 launch', 'channel': 'linkedin', 'kind': 'post',
        'title': 'v2 launch: the eleven-second problem (variant B)', 'status': 'draft',
        'variant_of': 'v2 launch: the eleven-second problem', 'variant_label': 'B: question hook',
        'days_ago': 5, 'hashtags': ['logistics', 'routeplanning', 'dispatch'],
        'sources': ['Product Overview: AI Workforce OS'],
        'body': (
            'How long does your dispatcher wait for a plan before giving up and '
            'doing it by hand?\n\nFor our first planner the honest answer was about '
            'eleven seconds on a 200-stop run. Route Optimisation v2 was built to '
            'get that under five, and every change to the plan is logged so it can '
            'be reviewed.\n\nWe would rather show you than tell you.'),
    },
    {
        'campaign': 'Route Optimisation v2 launch', 'channel': 'blog', 'kind': 'article',
        'title': 'Why we log every change to a route plan', 'status': 'pending',
        'days_ago': 3, 'hashtags': [],
        'sources': ['Product Overview: AI Workforce OS', 'Data Privacy and Security Policy'],
        'body': (
            '## Why we log every change to a route plan\n\nA route plan is a set of '
            'promises: to the driver about the day, to the customer about the '
            'window, to the business about the cost. When a plan changes, somebody '
            'should be able to see what changed, when, and why.\n\n### What is logged\n\n'
            'Every reorder, every reassignment and every manual override, with the '
            'user and the time. Nothing about the driver beyond what the plan '
            'needs, which is a deliberate choice under our data classification '
            'policy.\n\n### What it is for\n\nMostly for the dispatcher at 6am who '
            'wants to know why stop 14 moved. Occasionally for the conversation '
            'with a customer about a missed window. Never for watching people.'),
    },
    {
        'campaign': 'Cold chain spring programme', 'channel': 'instagram', 'kind': 'caption',
        'title': 'Cold chain: the 4am plan', 'status': 'draft',
        'days_ago': 2, 'hashtags': ['coldchain', 'freshproduce', 'logistics',
                                    'melbourne', 'wholesalemarket', 'dispatch'],
        'sources': [],
        'body': (
            'The market opens at 4am and the plan was finished at 9pm the night '
            'before. Tight windows, cold trucks, no spreadsheet. That is the '
            'job, and it is what the planner is for.'),
    },
    {
        'campaign': 'Cold chain spring programme', 'channel': 'twitter', 'kind': 'post',
        'title': 'Cold chain: over the limit on purpose', 'status': 'rejected',
        'days_ago': 2, 'hashtags': ['coldchain', 'logistics'],
        'sources': [],
        'body': (
            'Cold chain operators: the summer peak is eleven weeks away and the '
            'morning windows into the wholesale market are not getting any wider. '
            'Our delivery-window planner sequences the night before so the 4am '
            'departure is already decided. We are looking for two pilot accounts '
            'in Victoria and New South Wales before October -- reply or message us '
            'and we will show you a run built from your own stops.'),
    },
    {
        'campaign': 'Route Optimisation v2 launch', 'channel': 'email', 'kind': 'announcement',
        'title': 'Customer announcement: v2 is available', 'status': 'approved',
        'days_ago': 4, 'hashtags': [],
        'sources': ['Product Overview: AI Workforce OS'],
        'body': (
            'Route Optimisation v2 is available to every account from today.\n\n'
            'What is different: the planner is rebuilt end to end, plan changes '
            'are logged and reviewable, and the export that some of you have '
            'reported timing out on large date ranges is fixed in this release.\n\n'
            'Nothing changes for you unless you choose to switch. Your account '
            'manager will be in touch about a migration window.'),
    },
]

_EMAILS = [
    ('Route Optimisation v2 launch', 'campaign', 'v2 availability announcement',
     'Route Optimisation v2 is available today',
     'The planner rebuilt end to end, and the export timeout fixed.',
     'approved', 'Enterprise and premium accounts'),
    ('Route Optimisation v2 launch', 'newsletter', 'September dispatch notes',
     'Dispatch notes for September: v2, the export fix, and what is next on the '
     'roadmap for cold chain operators',
     'Three things worth two minutes of your time.',
     'draft', 'All accounts'),
    ('Cold chain spring programme', 'nurture', 'Cold chain pilot invitation',
     'Two pilot places for cold chain operators before summer',
     'Planned the night before. Cold in the morning.',
     'draft', 'Cold chain prospects'),
]

_SEGMENTS = [
    ('Enterprise fleets, 30+ vehicles',
     'Operators with multiple depots and a dedicated dispatch function.',
     {'vehicles_min': 30, 'depots_min': 2, 'tier': ['enterprise']}, 22,
     ['linkedin', 'email']),
    ('Cold chain, Victoria and NSW',
     'Fresh produce and refrigerated distribution with morning market windows.',
     {'sector': 'cold_chain', 'states': ['VIC', 'NSW'], 'vehicles_min': 8}, 140,
     ['linkedin', 'instagram', 'email']),
    ('Free-tier evaluators',
     'Accounts on the free tier for more than thirty days that have planned at '
     'least ten runs.',
     {'tier': ['free'], 'age_days_min': 30, 'runs_min': 10}, 61, ['email']),
]


def seed_content(owner=None):
    """Two campaigns with briefs, the copy, a four-week calendar, mail and segments.

    Keyed on campaign name, content title, email name, segment name and the
    (date, slot, channel) triple for calendar entries.
    """
    marketing = _agents().get('marketing')
    account = _owner_or_first(owner)
    created = 0
    today = _today()

    campaigns, briefs = {}, {}
    for spec in _CAMPAIGNS:
        campaign, was_created = MarketingCampaign.objects.get_or_create(
            name=spec['name'],
            defaults={'objective': spec['objective'],
                      'target_audience': spec['audience'],
                      'budget': Decimal(spec['budget']), 'status': spec['status'],
                      'user': account, 'assigned_agent': marketing})
        campaigns[spec['name']] = campaign
        if was_created:
            created += 1
            _remember(campaign)
            _backdate(campaign, _at(18, hour=10))

        brief, was_created = CampaignBrief.objects.get_or_create(
            campaign=campaign,
            defaults={'objective': spec['objective'], 'audience': spec['audience'],
                      'key_message': spec['key_message'], 'channels': spec['channels'],
                      'tone': spec['tone'], 'proof_points': _resolve_proof(spec['proof_points']),
                      'success_measures': spec['success_measures'],
                      'constraints': spec['constraints'],
                      'start_date': today - timedelta(days=14),
                      'end_date': today + timedelta(days=28),
                      'status': 'approved' if spec['status'] == 'active' else 'draft',
                      'created_by_agent': marketing})
        briefs[spec['name']] = brief
        if was_created:
            created += 1
            _remember(brief)

    pieces = {}
    for spec in _CONTENT:
        campaign = campaigns.get(spec['campaign'])
        body = spec['body']
        piece, was_created = ContentPiece.objects.get_or_create(
            title=spec['title'],
            defaults={
                'campaign': campaign, 'brief': briefs.get(spec['campaign']),
                'channel': spec['channel'], 'kind': spec['kind'], 'body': body,
                'hashtags': spec['hashtags'],
                'call_to_action': 'Book a demonstration' if spec['channel'] != 'blog' else '',
                'audience': (campaign.target_audience if campaign else ''),
                'tone': 'Warm but authoritative', 'status': spec['status'],
                'variant_label': spec.get('variant_label', ''),
                'published_at': (_at(spec['days_ago'] - 1, hour=9)
                                 if spec['status'] == 'published' else None),
                'external_reference': ('urn:li:share:demo-7731-v2'
                                       if spec['status'] == 'published' else ''),
                'word_count': len(body.split()), 'character_count': len(body),
                'sources': _resolve_sources(spec['sources']),
                'created_by_agent': marketing,
            })
        pieces[spec['title']] = piece
        if was_created:
            created += 1
            _remember(piece)
            _backdate(piece, _at(spec['days_ago'], hour=11))

    for spec in _CONTENT:
        parent_title = spec.get('variant_of')
        if parent_title and parent_title in pieces:
            ContentPiece.objects.filter(pk=pieces[spec['title']].pk,
                                        variant_of__isnull=True).update(
                variant_of=pieces[parent_title])

    for (campaign_name, kind, name, subject, preheader, status, segment) in _EMAILS:
        campaign = campaigns.get(campaign_name)
        email, was_created = MarketingEmail.objects.get_or_create(
            name=name,
            defaults={'campaign': campaign, 'kind': kind, 'subject': subject,
                      'preheader': preheader,
                      'body': next((p['body'] for p in _CONTENT
                                    if p['channel'] == 'email'), ''),
                      'audience_segment': segment,
                      'recipients': ['operations@example.com', 'support@example.com'],
                      'status': status, 'recipient_count': 2,
                      'created_by_agent': marketing})
        if was_created:
            created += 1
            _remember(email)
            _backdate(email, _at(4, hour=14))

    for name, description, criteria, size, fit in _SEGMENTS:
        segment, was_created = AudienceSegment.objects.get_or_create(
            name=name, defaults={'description': description, 'criteria': criteria,
                                 'estimated_size': size, 'channel_fit': fit,
                                 'created_by_agent': marketing})
        if was_created:
            created += 1
            _remember(segment)

    created += _seed_calendar(campaigns, pieces, marketing)
    _flush_manifest()
    return created


def _resolve_proof(points):
    """Attach real document ids to proof points where the document exists."""
    out = []
    for point in points:
        document = _document(point.get('source', '')) if point.get('source') else None
        out.append({'claim': point['claim'], 'source': point.get('source', ''),
                    'document_id': document.pk if document else None})
    return out


def _resolve_sources(titles):
    out = []
    for title in titles:
        document = _document(title)
        out.append({'fact': f'See {title}', 'document': title,
                    'document_id': document.pk if document else None, 'url': ''})
    return out


def _seed_calendar(campaigns, pieces, marketing):
    """Four weeks of slots, Monday to Friday, rotating the channels."""
    created = 0
    monday = _today() - timedelta(days=_today().weekday()) - timedelta(days=7)
    rotation = ['linkedin', 'email', 'blog', 'instagram', 'linkedin']
    titles = list(pieces.values())
    campaign = campaigns.get('Route Optimisation v2 launch')

    for week in range(4):
        for weekday in range(5):
            date = monday + timedelta(days=week * 7 + weekday)
            channel = rotation[(week + weekday) % len(rotation)]
            if weekday % 2:
                continue
            piece = next((p for p in titles if p.channel == channel), None)
            entry, was_created = ContentCalendarEntry.objects.get_or_create(
                date=date, slot='morning', channel=channel,
                defaults={'content': piece if date <= _today() else None,
                          'title': (piece.title if piece and date <= _today()
                                    else f'{channel.capitalize()} slot'),
                          'owner': 'Marketing & Communications', 'campaign': campaign,
                          'status': ('published' if piece and piece.status == 'published'
                                     and date < _today() else
                                     ('drafted' if piece and date <= _today() else 'planned')),
                          'created_by_agent': marketing})
            if was_created:
                created += 1
                _remember(entry)
    return created


# ===========================================================================
# 7. RESEARCH
# ===========================================================================

_RESEARCH_QUESTIONS = [
    ('What is our refund window and how long does a refund take?',
     'Refund window and processing time'),
    ('What are the support SLA response targets by priority?',
     'Support SLA response targets'),
    ('What happens in the first week of onboarding a new starter?',
     'Onboarding: the first week'),
]


def seed_research(owner=None):
    """Three answered questions with citations that point at real documents.

    Uses the retrieval engine itself rather than hand-written citations, so a
    quote in a seeded report is a passage that genuinely exists in the
    knowledge base. Skips a question if nothing is indexed and says so in the
    warnings, rather than inventing a source.
    """
    try:
        from . import knowledge
    except ImportError:
        _WARNINGS.append('Research seed skipped: the knowledge module is unavailable.')
        return 0

    research = _agents().get('research')
    account = _owner_or_first(owner)
    created = 0

    for question, title in _RESEARCH_QUESTIONS:
        if ResearchReport.objects.filter(title=title).exists():
            continue
        hits = knowledge.search(question, limit=5)
        if not hits:
            _WARNINGS.append(f'Research seed: no documents answer "{question}".')
            continue

        citations = knowledge.citations_for(hits)
        conflicts = knowledge.detect_conflicts(hits)
        body_lines = [f'Question: {question}', '']
        for index, hit in enumerate(hits[:4], start=1):
            body_lines.append(f'[{index}] {hit.document.title}: {hit.snippet.strip()}')
            body_lines.append('')
        if conflicts:
            body_lines.append('Conflict noted: two documents give different figures for '
                              'the same subject. Both are reported below; neither has '
                              'been chosen.')
            for conflict in conflicts[:2]:
                body_lines.append(f'  {conflict.get("note", "")}')
        summary = hits[0].snippet.strip()[:300]

        report = ResearchReport.objects.create(
            title=title, question=question, kind='answer', agent=research,
            summary=summary, body='\n'.join(body_lines),
            confidence=Decimal(str(round(min(1.0, hits[0].score), 3))),
            conflicts=conflicts,
            coverage_note=(f'Searched the knowledge base for the question above; '
                           f'{len(hits)} passages matched across '
                           f'{len({h.document_id for h in hits})} documents.'),
            requested_by=account)
        created += 1
        _remember(report)
        _backdate(report, _at(7 - created, hour=15))

        for ordinal, cite in enumerate(citations[:4], start=1):
            citation = ResearchCitation.objects.create(
                report=report,
                document=KnowledgeDocument.objects.filter(pk=cite.get('document_id')).first(),
                source_label=cite.get('label', '')[:300], url=cite.get('url', ''),
                quote=cite.get('quote', '')[:1000],
                relevance=Decimal(str(round(float(cite.get('relevance', 0)), 3))),
                ordinal=ordinal)
            created += 1
            _remember(citation)

    _flush_manifest()
    return created


# ===========================================================================
# 8. GOVERNANCE -- the approval queue in every state, and the audit trail
# ===========================================================================

def _first_ticket(**filters):
    return SupportTicket.objects.filter(source_message_id__startswith='demo-nw-',
                                        **filters).first()


def _governance_specs(agents):
    """The proposed actions, built against whatever the other seeders produced.

    Each entry names a REAL registered tool and carries the payload shape that
    tool's executor reads, so a seeded pending action can be approved and will
    execute (in demo mode, simulated and labelled). Where the subject row is
    missing -- the recruitment seed has not run, say -- the entry is skipped.
    """
    hr = agents.get('hr')
    marketing = agents.get('marketing')
    manager = agents.get('engineering_manager')
    support = agents.get('support')
    specs = []

    candidate = Candidate.objects.filter(status__in=('shortlisted', 'interviewing')).first()
    if candidate is not None and hr is not None:
        body = (f'Dear {candidate.full_name.split()[0]},\n\nThank you for your application. '
                f'We would like to invite you to a first interview for the '
                f'{candidate.job_opening.title if candidate.job_opening else "role"}.\n\n'
                f'Proposed time: Tuesday 10:00 to 10:45 (Australia/Melbourne), by video. '
                f'You will meet the hiring manager and one member of the team.\n\n'
                f'Please reply with whether that time suits, or offer two alternatives.\n\n'
                f'Kind regards,\nPeople Operations, {COMPANY}')
        specs.append({
            'agent': hr, 'integration': 'gmail', 'risk': 'high', 'status': 'pending',
            'action_type': 'hr.send_interview_invitation',
            'title': f'Invite {candidate.full_name} to a first interview',
            'summary': 'A first-round invitation with the time, format and panel stated.',
            'payload': {'candidate_id': candidate.pk, 'interview_id': None,
                        'to': _safe_email(candidate.email or 'candidate@example.com'),
                        'subject': f'Interview invitation: {COMPANY}', 'body': body},
            'editable': [('to', 'To', 'text'), ('subject', 'Subject', 'text'),
                         ('body', 'Body', 'longtext')],
            'subject_label': 'marketing.candidate', 'subject_id': candidate.pk,
            'subject_display': candidate.full_name, 'days_ago': 1,
        })

    piece = ContentPiece.objects.filter(status='pending').first() or \
        ContentPiece.objects.filter(status='draft', channel='linkedin').first()
    if piece is not None and marketing is not None:
        specs.append({
            'agent': marketing, 'integration': 'linkedin', 'risk': 'high',
            'status': 'pending', 'action_type': 'mkt.publish_linkedin_post',
            'title': f'Publish to LinkedIn: {piece.title[:60]}',
            'summary': 'The post copy, ready to publish to the company page.',
            'payload': {'content_id': piece.pk, 'text': piece.body, 'visibility': 'PUBLIC'},
            'editable': [('text', 'Post copy', 'longtext'), ('visibility', 'Visibility', 'text')],
            'subject_label': 'marketing.contentpiece', 'subject_id': piece.pk,
            'subject_display': piece.title, 'days_ago': 2,
        })

    bug = WorkItem.objects.filter(item_type='bug').first()
    if bug is not None and manager is not None:
        specs.append({
            'agent': manager, 'integration': 'jira', 'risk': 'medium', 'status': 'pending',
            'action_type': 'eng.create_jira_issue',
            'title': f'File {bug.reference} in Jira: {bug.title[:50]}',
            'summary': 'Mirror the work item into the team Jira project.',
            'payload': {'work_item_id': bug.pk, 'project_key': bug.project.jira_project_key or 'ROUTE',
                        'summary': bug.title, 'description': bug.description or bug.title,
                        'issue_type': 'Bug', 'priority': 'High', 'assignee': '',
                        'labels': ['from-ai-workforce']},
            'editable': [('summary', 'Summary', 'text'), ('description', 'Description', 'longtext'),
                         ('priority', 'Priority', 'text')],
            'subject_label': 'marketing.workitem', 'subject_id': bug.pk,
            'subject_display': bug.reference, 'days_ago': 2,
        })

    ticket = _first_ticket(category='refund')
    if ticket is not None and support is not None:
        draft = ticket.messages.filter(is_ai_draft=True).first()
        text = draft.body if draft else (
            'Hello,\n\nThank you for getting in touch about the July subscription. '
            'Our refund policy allows a refund within 30 calendar days of the '
            'purchase date, and this request falls outside that window, so I am '
            'not able to approve it. I am sorry -- I know that is not the answer '
            'you were hoping for.\n\nKind regards,\nCustomer Support')
        specs.append({
            'agent': support, 'integration': 'gmail', 'risk': 'high', 'status': 'pending',
            'action_type': 'support.send_customer_reply',
            'title': f'Reply to {ticket.reference}: {ticket.subject[:50]}',
            'summary': 'A reply grounded in the refund policy, declining an out-of-window request.',
            'payload': {'ticket_id': ticket.pk, 'to': _safe_email(ticket.contact_email),
                        'subject': f'Re: {ticket.subject}', 'body': text},
            'editable': [('to', 'To', 'text'), ('subject', 'Subject', 'text'),
                         ('body', 'Message', 'longtext')],
            'subject_label': 'marketing.supportticket', 'subject_id': ticket.pk,
            'subject_display': ticket.reference, 'days_ago': 1,
            # A reviewer has already softened the second paragraph.
            'edited_payload': {'body': text.replace(
                'I am not able to approve it.',
                'I am not able to approve it as a refund, but I can offer a credit '
                'of one month against your next invoice.')},
        })

    if support is not None:
        specs.append({
            'agent': support, 'integration': 'slack', 'risk': 'medium', 'status': 'executed',
            'demo': True, 'action_type': 'support.notify_support_team',
            'title': 'Tell the support channel about the export timeout pattern',
            'summary': 'Three tickets in a week name the same export timeout.',
            'payload': {'channel': '#support', 'text': (
                'Heads up: three tickets this week report the route export timing out '
                'on date ranges over 90 days. Engineering has it open. Please tag any '
                'new report export-timeout so the count stays honest.'), 'ticket_id': None},
            'editable': [('text', 'Message', 'longtext'), ('channel', 'Channel', 'text')],
            'days_ago': 5, 'execution_summary':
                'Would post to #support. Nothing was sent: Slack is in demo mode.',
        })

    if hr is not None:
        specs.append({
            'agent': hr, 'integration': 'slack', 'risk': 'medium', 'status': 'executed',
            'demo': False, 'action_type': 'hr.send_hr_announcement',
            'title': 'Announce the public holiday arrangements',
            'summary': 'Office closed on the public holiday; support roster covers it.',
            'payload': {'title': 'Public holiday arrangements', 'channel': 'slack',
                        'audience': 'All staff', 'body': (
                            'The office is closed on the public holiday. The support '
                            'roster covers customer contact; everyone else, enjoy the day.')},
            'editable': [('body', 'Announcement', 'longtext')],
            'days_ago': 9, 'execution_summary':
                'Posted the announcement to #people (1 message).',
        })

    if marketing is not None:
        specs.append({
            'agent': marketing, 'integration': 'instagram', 'risk': 'high', 'status': 'rejected',
            'action_type': 'mkt.publish_instagram_post',
            'title': 'Publish to Instagram: Cold chain teaser',
            'summary': 'A teaser caption for the cold chain programme.',
            'payload': {'content_id': None, 'caption': (
                'Fewer missed windows after switching to planned sequencing. '
                'Ask us how. #coldchain #logistics'), 'image_url': ''},
            'editable': [('caption', 'Caption', 'longtext')],
            'days_ago': 4, 'decision_reason':
                'The missed-windows claim has no source in the brief. Not publishing '
                'a figure we cannot substantiate.',
        })

    if manager is not None:
        specs.append({
            'agent': manager, 'integration': 'google_calendar', 'risk': 'medium',
            'status': 'failed', 'action_type': 'eng.schedule_engineering_meeting',
            'title': 'Schedule the Sprint 13 planning session',
            'summary': 'Ninety minutes, the whole engineering team.',
            'payload': {'title': 'Sprint 13 planning', 'start_at': 'next Monday 10:00',
                        'duration_minutes': 90, 'attendees': ['engineering@example.com'],
                        'agenda': 'Carry-over, capacity, the export fix.', 'kind': 'sprint_planning'},
            'editable': [('start_at', 'Start', 'text'), ('agenda', 'Agenda', 'longtext')],
            'days_ago': 3, 'execution_summary':
                'Could not parse "next Monday 10:00" as a start time. Use an ISO date '
                'such as 2026-09-14 10:00.',
        })

        specs.append({
            'agent': manager, 'integration': 'slack', 'risk': 'medium', 'status': 'cancelled',
            'action_type': 'eng.send_deadline_reminder',
            'title': 'Remind the team about the ingest deadline',
            'summary': 'Withdrawn: the deadline moved before the reminder went out.',
            'payload': {'work_item_id': None, 'channel': '#engineering',
                        'message': 'Reminder: the ingest work is due Friday.'},
            'editable': [('message', 'Message', 'longtext')],
            'days_ago': 6, 'decision_reason': 'Deadline moved to the following sprint.',
        })

    return specs


def seed_governance(owner=None):
    """Proposed actions in every state, their trails, and a fortnight of audit events."""
    agents = _agents()
    account = _owner_or_first(owner)
    created = 0

    for spec in _governance_specs(agents):
        if ProposedAction.objects.filter(title=spec['title'],
                                         action_type=spec['action_type']).exists():
            continue
        integration = _integration(spec['integration'])
        status = spec['status']
        original = dict(spec['payload'])
        payload = dict(original)
        edited = spec.get('edited_payload') or {}
        payload.update(edited)
        opened = _at(spec['days_ago'], hour=10, minute=20)

        action = ProposedAction.objects.create(
            action_type=spec['action_type'], title=spec['title'][:250],
            summary=spec['summary'], payload=payload, original_payload=original,
            editable_fields=[{'key': k, 'label': lbl, 'field_type': ft,
                              'help_text': '', 'rows': 10 if ft == 'longtext' else 1}
                             for k, lbl, ft in spec['editable']],
            agent=spec['agent'], integration=integration, requested_by=account,
            subject_label=spec.get('subject_label', ''),
            subject_id=spec.get('subject_id'),
            subject_display=spec.get('subject_display', '')[:200],
            status=status, risk=spec['risk'], edit_count=1 if edited else 0,
            decided_by=account if status != 'pending' else None,
            decided_at=(opened + timedelta(hours=3)) if status != 'pending' else None,
            decision_reason=spec.get('decision_reason', ''),
            executed_at=(opened + timedelta(hours=3, minutes=1))
            if status in ('executed', 'failed') else None,
            execution_summary=spec.get('execution_summary', ''),
            execution_result={'demo_seed': True, 'simulated': bool(spec.get('demo'))}
            if status in ('executed', 'failed') else {},
            executed_in_demo=bool(spec.get('demo')) and status == 'executed')
        created += 1
        _remember(action)
        _backdate(action, opened)

        trail = [('proposed', account, f'{spec["agent"].name} prepared this action.', 'pending')]
        if edited:
            trail.append(('edited', account, 'Edited before approval: '
                          + ', '.join(sorted(edited)) + '.', 'pending'))
        if status in ('executed', 'failed'):
            trail.append(('approved', account, 'Approved.', 'approved'))
            trail.append((status, None, spec.get('execution_summary', ''), status))
        elif status in ('rejected', 'cancelled'):
            trail.append((status, account, spec.get('decision_reason', ''), status))
        when = opened
        for event, actor, note, to_status in trail:
            when = when + timedelta(minutes=35)
            entry = ActionAuditTrail.objects.create(
                action=action, event=event, actor=actor, note=note,
                changes={k: {'from': original.get(k), 'to': v} for k, v in edited.items()}
                if event == 'edited' else {},
                from_status='pending' if event != 'proposed' else '',
                to_status=to_status)
            _remember(entry)
            _backdate(entry, when)

        if status == 'executed':
            channel = ('slack' if spec['integration'] == 'slack' else 'email')
            message = OutboundMessage.objects.create(
                channel=channel,
                recipient=str(payload.get('channel') or payload.get('to') or 'team')[:400],
                subject=action.title[:300],
                body=str(payload.get('text') or payload.get('body') or ''),
                status='simulated' if spec.get('demo') else 'sent',
                agent=spec['agent'], action=action, integration=integration,
                metadata={'demo_seed': True})
            created += 1
            _remember(message)
            _backdate(message, opened + timedelta(hours=3, minutes=1))

    created += _seed_audit_events(agents, account)
    _flush_manifest()
    return created


_AUDIT_TEMPLATE = [
    # (category, action, status, agent_type, target_app, message)
    ('agent', 'agent.selected', 'ok', 'hr', '', 'Routed a recruitment request to People Operations.'),
    ('tool', 'tool.hr.score_candidate', 'ok', 'hr', 'AI Workforce', 'Scored a candidate against the Senior Backend Engineer opening.'),
    ('tool', 'tool.hr.generate_interview_questions', 'ok', 'hr', 'AI Workforce', 'Generated eight competency questions for the first round.'),
    ('knowledge', 'knowledge.search', 'ok', 'research', 'Company Knowledge Base', 'Searched: annual leave accrual.'),
    ('knowledge', 'knowledge.search', 'ok', 'support', 'Company Knowledge Base', 'Searched: refund processing time.'),
    ('tool', 'tool.support.set_ticket_priority', 'ok', 'support', 'AI Workforce', 'Set priority urgent from impact: data loss reported.'),
    ('tool', 'tool.support.flag_for_human', 'ok', 'support', 'AI Workforce', 'Flagged a ticket for a person: possible data loss.'),
    ('tool', 'tool.eng.break_down_requirement', 'ok', 'engineering_manager', 'AI Workforce', 'Broke a requirement into six work items.'),
    ('tool', 'tool.eng.identify_blockers', 'ok', 'engineering_manager', 'AI Workforce', 'Identified two blocked items.'),
    ('tool', 'tool.dev.analyse_error', 'ok', 'developer', 'AI Workforce', 'Read a traceback: export worker timeout.'),
    ('tool', 'tool.dev.generate_unit_tests', 'ok', 'developer', 'AI Workforce', 'Generated tests for the matrix cache.'),
    ('tool', 'tool.mkt.generate_linkedin_post', 'ok', 'marketing', 'AI Workforce', 'Wrote a LinkedIn post for the v2 launch.'),
    ('tool', 'tool.mkt.check_brand_compliance', 'ok', 'marketing', 'AI Workforce', 'Brand check: one unsourced superlative removed.'),
    ('approval', 'action.proposed', 'pending', 'marketing', 'LinkedIn', 'Publish to LinkedIn queued for approval.'),
    ('approval', 'action.approved', 'ok', None, 'Slack', 'Announcement approved.'),
    ('execution', 'action.executed', 'demo', 'support', 'Slack', 'Simulated: would post to #support.'),
    ('execution', 'action.executed', 'ok', 'hr', 'Slack', 'Posted the announcement to #people.'),
    ('approval', 'action.rejected', 'denied', None, 'Instagram', 'Rejected: unsourced claim.'),
    ('integration', 'integration.tested', 'demo', None, 'Google Drive', 'Connection check: demo mode, not configured.'),
    ('integration', 'integration.tested', 'ok', None, 'Gmail', 'Connection check: mailbox reachable.'),
    ('data', 'candidate.status_changed', 'ok', 'hr', 'AI Workforce', 'Candidate moved to shortlisted.'),
    ('data', 'work_item.status_changed', 'ok', 'engineering_manager', 'AI Workforce', 'Work item moved to in review.'),
    ('auth', 'user.signed_in', 'ok', None, '', 'Signed in.'),
    ('system', 'workspace.provisioned', 'ok', None, '', 'Capability catalogue regenerated from the tool registry.'),
]


def _seed_audit_events(agents, account):
    """A fortnight of events across every category, marked as seeded."""
    if _already_seeded(AuditEvent):
        return 0
    rng = random.Random(_RNG_SEED)
    created = 0
    for day in range(14, 0, -1):
        for _n in range(rng.randint(4, 7)):
            category, action, status, agent_type, app, message = rng.choice(_AUDIT_TEMPLATE)
            when = _at(day, hour=rng.randint(8, 17), minute=rng.randint(0, 59))
            event = AuditEvent.objects.create(
                actor=account if agent_type is None or rng.random() < 0.3 else None,
                agent=agents.get(agent_type) if agent_type else None,
                category=category, action=action, status=status, message=message,
                target_app=app, object_label=message[:60],
                detail={'demo_seed': True, 'seeded_day_offset': day},
                duration_ms=rng.randint(40, 900))
            created += 1
            _remember(event)
            _backdate(event, when)
    return created


# ===========================================================================
# 9. AGENT MEMORY
# ===========================================================================

_MEMORIES = {
    'hr': [
        ('hiring-manager-backend', 'fact', 8,
         'The hiring manager for the Senior Backend Engineer opening is the '
         'engineering manager, Priya Raghavan. Interview panels include her plus '
         'one developer.'),
        ('interview-format', 'preference', 6,
         'First interviews are 45 minutes by video, in Australia/Melbourne '
         'time, and the invitation always names the panel.'),
    ],
    'engineering_manager': [
        ('sprint-cadence', 'fact', 7, 'Sprints are two weeks, Monday to Friday, '
         'planning on the first Monday at 10:00.'),
        ('export-timeout', 'fact', 9, 'The route export timeout on ranges over '
         '90 days is the highest-impact open bug: three enterprise customers '
         'have reported it.'),
    ],
    'developer': [
        ('stack', 'fact', 8, 'Route Optimisation v2 is Python 3.13, Django 6.1, '
         'PostgreSQL 16 with PostGIS, Celery and Redis, deployed on AWS ECS.'),
        ('fixture-caveat', 'fact', 7, 'The synthetic 200-stop fixture flatters '
         'the matrix cache (about 94 per cent hit rate against an estimated 60 '
         'in production). Performance numbers from it are upper bounds.'),
    ],
    'research': [
        ('refund-conflict', 'fact', 9, 'The refund policy says 5 business days '
         'to process a refund; the troubleshooting guide says 7. Report both '
         'until the documents are reconciled.'),
        ('authoritative-leave', 'policy', 8, 'The Leave and Time Off Policy is '
         'the authoritative source for leave figures; quote it, not the handbook.'),
    ],
    'marketing': [
        ('banned-words', 'policy', 8, 'Never use synergy, revolutionary, '
         'game-changing or unlock. Five hashtags at most on LinkedIn.'),
        ('v2-claim', 'fact', 9, 'The five-second planning figure may only be '
         'quoted once Engineering has measured it on production-shaped data.'),
    ],
    'support': [
        ('priority-from-impact', 'policy', 9, 'Priority comes from impact, not '
         'tone. Data loss, security, double billing and blocked work outrank '
         'anger.'),
        ('kembla-contact', 'entity', 7, 'Kembla Freight Group is the largest '
         'account. Their operations manager is the only person who reports '
         'faults and is on leave until Monday.'),
    ],
}

_SHARED_MEMORIES = [
    ('company-timezone', 'fact', 8, 'The company works in Australia/Melbourne; '
     'every time written into a message states the zone.'),
    ('approval-rule', 'policy', 10, 'Nothing external happens without a person. '
     'Prepare the action, say it is waiting, never claim it was sent.'),
]


def seed_agent_memory(owner=None):
    """Two or three memories per employee, and two shared by the whole workforce."""
    agents = _agents()
    account = _owner_or_first(owner)
    created = 0
    for agent_type, rows in _MEMORIES.items():
        agent = agents.get(agent_type)
        if agent is None:
            continue
        for key, kind, importance, content in rows:
            memory, was_created = AgentMemory.objects.get_or_create(
                agent=agent, key=key,
                defaults={'scope': 'agent', 'kind': kind, 'content': content,
                          'importance': importance, 'source': 'demonstration seed',
                          'created_by': account})
            if was_created:
                created += 1
                _remember(memory)
    for key, kind, importance, content in _SHARED_MEMORIES:
        memory, was_created = AgentMemory.objects.get_or_create(
            agent=None, key=key,
            defaults={'scope': 'shared', 'kind': kind, 'content': content,
                      'importance': importance, 'source': 'demonstration seed',
                      'created_by': account})
        if was_created:
            created += 1
            _remember(memory)
    _flush_manifest()
    return created


# ===========================================================================
# 10. THE WHOLE COMPANY, AND ITS REMOVAL
# ===========================================================================

def seed_all(owner=None, reset=False):
    """Every seeder, in dependency order. Returns a report of counts."""
    _WARNINGS.clear()
    if reset:
        clear_demo_data()
    report = {}
    with transaction.atomic():
        report['company_profile'] = seed_company_profile(owner)
        report['employees'] = seed_employees(owner)
        report['recruitment'] = seed_recruitment(owner)
        report['engineering'] = seed_engineering(owner)
        report['support'] = seed_support(owner)
        report['content'] = seed_content(owner)
        report['research'] = seed_research(owner)
        report['governance'] = seed_governance(owner)
        report['memory'] = seed_agent_memory(owner)
    report['warnings'] = warnings()
    return report


def clear_demo_data():
    """Remove exactly what the seed wrote, and nothing a person added.

    The manifest is the authority. The JSON markers are checked as well, so a
    row that lost its manifest entry -- an older seed run, a restored
    database -- is still found. Deletion runs children before parents so a
    cascade cannot pre-empt a listed delete and under-report.
    """
    from django.apps import apps

    manifest = _manifest_load()
    removed = {}

    for label in _CLEAR_ORDER:
        app_label, model_name = label.split('.')
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            continue
        pks = list(manifest.get(label) or [])
        query = model.objects.filter(pk__in=pks) if pks else model.objects.none()
        for field_name in ('metadata', 'detail', 'metrics'):
            if any(f.name == field_name for f in model._meta.get_fields()):
                query = query | model.objects.filter(**{f'{field_name}__demo_seed': True})
        count, _detail = query.distinct().delete() if pks or query.exists() else (0, {})
        if count:
            removed[label] = count

    row = SystemSetting.objects.filter(key=MANIFEST_KEY).first()
    if row is not None:
        row.value = {'v': {}}
        row.save(update_fields=['value'])
    _PENDING.clear()
    return removed
