"""Bring the workforce up: roster, integrations, knowledge, and a company's work.

    python manage.py seed_workforce [--reset] [--owner USERNAME] [--minimal] [--noinput]

WHY THIS COMMAND RUNS ON EVERY START
------------------------------------
run.bat calls it before the server starts, and it is idempotent from end to
end: every writer is keyed on a natural value or guarded by the seed manifest,
so a second run adds nothing. That is what lets a fresh download become a
working company operating system with no setup steps, and lets an existing
installation pick up a new connector or a new tool without anyone doing
anything.

WHAT IT CREATES, IN ORDER
-------------------------
    1. the role groups and their permission matrix     roles.sync_groups
    2. a superuser and one account per role            only when absent
    3. the six employees, providers, integrations,
       settings, the capability catalogue and the
       seeded knowledge base                           provisioning + agent_engine
    4. the demonstration company                       demo_data.seed_all
       (skipped with --minimal)

--reset removes the demonstration rows first, by manifest, and asks before it
does unless --noinput is given. It never touches users, employees you created,
or anything the seed did not write.

THE ADDRESSES
-------------
Every email address the demonstration writes is on a reserved domain that
cannot receive mail, because the Gmail integration on a configured machine
genuinely sends. See demo_data._safe_email.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from marketing import agent_engine, roles

ROLE_ACCOUNTS = [
    # username, role, password, job title
    ('manager', 'manager', 'demo1234', 'Operations Manager'),
    ('analyst', 'analyst', 'demo1234', 'Business Analyst'),
    ('viewer', 'viewer', 'demo1234', 'Observer'),
]

SUGGESTIONS = [
    ('People Operations', 'Write a job description for a customer support specialist '
                          'and score the shortlisted candidates against it.'),
    ('Engineering Delivery', 'Break "customers can export a route plan as PDF" into '
                             'work items with estimates and a build order.'),
    ('Software Development', 'Explain this traceback and suggest the smallest fix: '
                             'TimeoutError in export_worker.run.'),
    ('Knowledge & Research', 'What is our refund window, and does any document '
                             'disagree about the processing time?'),
    ('Marketing & Communications', 'Write a LinkedIn post about the v2 release '
                                   'using only facts from the knowledge base.'),
    ('Customer Support', 'Triage the open tickets and draft a policy-grounded reply '
                         'to the out-of-window refund request.'),
]


class Command(BaseCommand):
    help = 'Provision the six-employee workforce and seed a demonstration company.'

    def add_arguments(self, parser):
        parser.add_argument('--owner', default='admin',
                            help='Username recorded as the creator of seeded rows.')
        parser.add_argument('--reset', action='store_true',
                            help='Remove the demonstration rows before seeding.')
        parser.add_argument('--minimal', action='store_true',
                            help='Provision the workspace and knowledge base only.')
        parser.add_argument('--noinput', action='store_true',
                            help='Do not ask for confirmation before --reset.')

    def handle(self, *args, **options):
        if options['reset'] and not options['noinput']:
            answer = input('This removes every demonstration row the seed wrote '
                           '(users and your own records are kept). Continue? [y/N] ')
            if answer.strip().lower() not in ('y', 'yes'):
                self.stdout.write('Nothing was changed.')
                return

        roles.sync_groups()
        owner = self._ensure_superuser(options['owner'])
        self._ensure_role_accounts()

        self.stdout.write('\nProvisioning the workspace...')
        report = agent_engine.ensure_workspace(owner=owner) or {}
        for key, value in report.items():
            self.stdout.write(f'  {key:14} {value}')

        if options['minimal']:
            self.stdout.write(self.style.SUCCESS(
                '\nMinimal provisioning complete. No demonstration company was seeded.'))
        else:
            self.stdout.write('\nSeeding the demonstration company...')
            try:
                from marketing import demo_data
            except ImportError as exc:  # pragma: no cover -- degraded install
                self.stdout.write(self.style.WARNING(
                    f'  Demonstration data unavailable: {exc}'))
            else:
                counts = demo_data.seed_all(owner=owner, reset=options['reset'])
                for key, value in counts.items():
                    if key == 'warnings':
                        continue
                    self.stdout.write(f'  {key:16} {value} new row(s)')
                for warning in counts.get('warnings', []):
                    self.stdout.write(self.style.WARNING(f'  note: {warning}'))

        self._print_summary(options['owner'])

    # -- users ---------------------------------------------------------------

    def _ensure_superuser(self, username):
        """A superuser to sign in as, created only when none exists."""
        existing = User.objects.filter(username=username).first()
        if existing is not None:
            agent_engine.ensure_profile(existing)
            return existing
        if User.objects.filter(is_superuser=True).exists():
            user = User.objects.filter(is_superuser=True).first()
            agent_engine.ensure_profile(user)
            return user

        with transaction.atomic():
            user = User.objects.create_superuser(
                username=username, email=f'{username}@example.com', password='admin123')
            profile = agent_engine.ensure_profile(user)
            profile.role = 'owner'
            profile.job_title = 'Workspace Owner'
            profile.save(update_fields=['role', 'job_title'])
        self.stdout.write(self.style.SUCCESS(f'Created superuser "{username}".'))
        return user

    def _ensure_role_accounts(self):
        """One account per role, so the permission matrix can be demonstrated.

        A password is set only on creation. An existing account keeps whatever
        password its owner chose.
        """
        for username, role, password, title in ROLE_ACCOUNTS:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={'email': f'{username}@example.com',
                          'is_staff': role == 'manager'})
            if created:
                user.set_password(password)
                user.save()
                self.stdout.write(self.style.SUCCESS(f'Created {role} account "{username}".'))
            profile = agent_engine.ensure_profile(user)
            if profile.role != role:
                profile.role = role
                profile.job_title = title
                profile.save(update_fields=['role', 'job_title'])
            roles.apply_role(user, role)

    # -- the closing report --------------------------------------------------

    def _print_summary(self, owner_username):
        self.stdout.write(self.style.SUCCESS('\nThe workforce is ready.'))
        self.stdout.write('\nSIGN IN\n')
        self.stdout.write(f'  {"USERNAME":<10} {"PASSWORD":<10} {"ROLE":<14} CAN')
        self.stdout.write('  ' + '-' * 70)
        rows = [
            (owner_username, 'admin123', 'Administrator',
             'everything, including configuring connected applications'),
            ('manager', 'demo1234', 'Manager', 'approve, edit and reject queued actions'),
            ('analyst', 'demo1234', 'Analyst', 'talk to employees and do operational work'),
            ('viewer', 'demo1234', 'Viewer', 'read everything, change nothing'),
        ]
        for row in rows:
            self.stdout.write(f'  {row[0]:<10} {row[1]:<10} {row[2]:<14} {row[3]}')
        self.stdout.write(
            '\n  If the admin account already existed its password was left alone.')

        self.stdout.write('\nOPEN   http://127.0.0.1:8000/   and start on the Dashboard: '
                          'it says what is set up and what is not.')
        self.stdout.write('\nTHINGS TO TRY, one per employee')
        for name, ask in SUGGESTIONS:
            self.stdout.write(f'  {name:28} "{ask}"')
        self.stdout.write(
            '\nAnything that would leave the company waits in Approvals with its '
            'full content. Connected applications without a credential simulate '
            'and label every action as simulated.\n')
