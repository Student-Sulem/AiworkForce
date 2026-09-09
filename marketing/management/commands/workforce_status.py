"""One screen that says whether the platform is working, and what to do next.

    python manage.py workforce_status

The first thing to run when something looks wrong. It reads, never writes.
"""

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from marketing.models import AIAgent, LLMProvider
from marketing.models_content import ContentPiece
from marketing.models_eng import WorkItem
from marketing.models_hr import Candidate, JobOpening
from marketing.models_knowledge import DocumentChunk, KnowledgeDocument
from marketing.models_platform import AgentTask, AuditEvent, Integration, ProposedAction
from marketing.models_support import SupportTicket


class Command(BaseCommand):
    help = 'Report the health of the workforce platform and what to do next.'

    def handle(self, *args, **options):
        out = self.stdout.write
        next_steps = []

        out(self.style.MIGRATE_HEADING('EMPLOYEES'))
        agents = list(AIAgent.objects.select_related('llm_model', 'llm_model__provider'))
        live_models = 0
        for agent in agents:
            reachable = agent.has_live_llm
            live_models += int(reachable)
            caps = agent.capabilities.count()
            mark = self.style.SUCCESS('live ') if reachable else self.style.WARNING('tools')
            out(f'  {mark}  {agent.name:<28} {agent.role:<24} {caps:>3} capabilities  '
                f'{agent.llm_label}')
        if not agents:
            out(self.style.ERROR('  No employees. Run: manage.py seed_workforce'))
            next_steps.append('Run manage.py seed_workforce to provision the roster.')
        elif not live_models:
            configured = sum(1 for p in LLMProvider.objects.all() if p.is_configured)
            if configured:
                next_steps.append('A provider has a key but no employee can reach its model. '
                                  'Open Language models, press Test, and check the model id.')
            else:
                next_steps.append('No language model is configured. Open Language models and '
                                  'add an OpenRouter or NVIDIA key, or point at local Ollama. '
                                  'The employees still run their tools without one, but '
                                  'cannot write prose.')

        out('\n' + self.style.MIGRATE_HEADING('CONNECTED APPLICATIONS'))
        rows = list(Integration.objects.all())
        live = 0
        for row in rows:
            mode = row.effective_mode
            live += int(mode == 'live')
            style = (self.style.SUCCESS if mode == 'live'
                     else self.style.WARNING if mode == 'demo' else self.style.ERROR)
            missing = ', '.join(row.missing_settings)
            out(style(f'  {row.name:<26} {mode:<12} {row.connection_status:<10} '
                      f'{("missing: " + missing) if missing else ""}'))
        if rows and not live:
            next_steps.append('Every application is in demo mode. That is fine for a '
                              'demonstration; add a credential on Integrations to make one real.')

        out('\n' + self.style.MIGRATE_HEADING('KNOWLEDGE BASE'))
        docs = KnowledgeDocument.objects.filter(is_active=True).count()
        chunks = DocumentChunk.objects.count()
        last = KnowledgeDocument.objects.order_by('-indexed_at').values_list(
            'indexed_at', flat=True).first()
        out(f'  {docs} documents, {chunks} searchable passages, last indexed '
            f'{last.strftime("%d %b %Y %H:%M") if last else "never"}')
        if not docs:
            next_steps.append('The knowledge base is empty. Run manage.py seed_workforce, or '
                              'add a document on the Knowledge page.')

        out('\n' + self.style.MIGRATE_HEADING('APPROVAL QUEUE'))
        pending = ProposedAction.objects.filter(status='pending')
        oldest = pending.order_by('created_at').first()
        out(f'  {pending.count()} pending'
            + (f' (oldest {oldest.age_hours} hours)' if oldest else '')
            + f', {ProposedAction.objects.filter(status="executed").count()} executed, '
              f'{ProposedAction.objects.filter(status="executed", executed_in_demo=True).count()} '
              f'simulated, {ProposedAction.objects.filter(status="failed").count()} failed')
        if oldest and oldest.age_hours > 48:
            next_steps.append('Actions have waited more than two days. Open Approvals.')

        out('\n' + self.style.MIGRATE_HEADING('OPERATIONS'))
        open_items = WorkItem.objects.exclude(status__in=('done', 'cancelled'))
        tickets = SupportTicket.objects.exclude(status__in=('resolved', 'closed'))
        out(f'  Recruitment   {JobOpening.objects.filter(status="open").count()} open roles, '
            f'{Candidate.objects.count()} candidates')
        out(f'  Engineering   {open_items.count()} open items, '
            f'{open_items.filter(due_date__lt=timezone.localdate()).count()} overdue, '
            f'{open_items.filter(status="blocked").count()} blocked')
        out(f'  Support       {tickets.count()} open tickets, '
            f'{tickets.filter(Q(priority="urgent") | Q(needs_human=True)).count()} urgent or flagged')
        out(f'  Content       {ContentPiece.objects.count()} pieces, '
            f'{ContentPiece.objects.filter(status="published").count()} published')
        out(f'  Tasks         {AgentTask.objects.filter(status__in=("queued", "running", "waiting_approval")).count()} open')

        since = timezone.now() - timezone.timedelta(hours=24)
        out('\n' + self.style.MIGRATE_HEADING('AUDIT'))
        out(f'  {AuditEvent.objects.filter(created_at__gte=since).count()} events in the '
            f'last 24 hours, {AuditEvent.objects.count()} in total')

        out('\n' + self.style.MIGRATE_HEADING('VERDICT'))
        if not next_steps:
            out(self.style.SUCCESS('  Ready. Everything that can be checked from here is in order.'))
        else:
            out(self.style.WARNING('  Usable, with things to do:'))
            for index, step in enumerate(next_steps, start=1):
                out(f'  {index}. {step}')
