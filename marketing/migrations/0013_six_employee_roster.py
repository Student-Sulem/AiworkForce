"""Map the four marketing agents onto the six-employee roster.

WHAT CHANGED, AND WHY THIS MIGRATION EXISTS
-------------------------------------------
The platform was four AI assistants for a marketing team: a content writer, a
lead finder, an outreach manager and an analyst. It is now six AI employees
covering a whole company: HR, engineering delivery, software development,
research, marketing and customer support.

`AIAgent.agent_type` is unique and is the key everything hangs off -- the tool
registry, the orchestrator's routing, the capability catalogue. So the four old
values have to become six new ones. Deleting the old rows would be simpler and
wrong: `Conversation.agent` and `ChatMessage` history point at them, and
throwing away somebody's chat history to tidy up an enumeration is not a
migration, it is data loss.

So the four are remapped by subject, keeping their conversations attached to an
employee:

    content      -> marketing              (both write copy)
    lead_finder  -> research               (both find and qualify information)
    outreach     -> hr                     (both write to people outside)
    analyst      -> engineering_manager    (both report on progress)

The remap is imperfect by nature. A conversation that used to be with the
analyst about campaign figures now belongs to Engineering Delivery, which is a
slightly odd home for it. That is the honest cost of the redesign, and it is
much smaller than the cost of deleting the thread.

The two employees with no predecessor -- Developer and Customer Support -- are
created by marketing/provisioning.py on the next start-up, along with the
description and instructions for all six. This migration deliberately does NOT
write those, because the wording lives in marketing/workforce.py and a copy
frozen into a migration file would be wrong within a week.
"""

from django.db import migrations

# Old value -> new value. Kept in step with LEGACY_AGENT_MAP in
# marketing/workforce.py, which is where the application reads it.
REMAP = {
    'content': 'marketing',
    'lead_finder': 'research',
    'outreach': 'hr',
    'analyst': 'engineering_manager',
}

REVERSE = {new: old for old, new in REMAP.items()}


def forwards(apps, schema_editor):
    AIAgent = apps.get_model('marketing', 'AIAgent')

    for old, new in REMAP.items():
        agent = AIAgent.objects.filter(agent_type=old).first()
        if agent is None:
            continue

        # If something already occupies the destination -- a second run, or a
        # workspace where provisioning got there first -- the old row is the
        # duplicate. Move its conversations across and remove it, rather than
        # violating the unique constraint on agent_type.
        occupant = AIAgent.objects.filter(agent_type=new).exclude(pk=agent.pk).first()
        if occupant is not None:
            _move_references(apps, agent, occupant)
            agent.delete()
            continue

        agent.agent_type = new
        agent.save(update_fields=['agent_type'])


def backwards(apps, schema_editor):
    """Put the four back, and delete the two that never existed before.

    A reverse migration that silently left six rows behind would make the
    older code path fail on a value its choices do not contain, so this is
    worth doing properly even though nobody is likely to run it.
    """
    AIAgent = apps.get_model('marketing', 'AIAgent')

    for new, old in REVERSE.items():
        agent = AIAgent.objects.filter(agent_type=new).first()
        if agent is None:
            continue
        if AIAgent.objects.filter(agent_type=old).exists():
            continue
        agent.agent_type = old
        agent.save(update_fields=['agent_type'])

    AIAgent.objects.filter(agent_type__in=('developer', 'support')).delete()


def _move_references(apps, source, target):
    """Repoint everything that names `source` at `target`.

    Written out by hand rather than relying on a cascade, because the point of
    the exercise is to keep the rows that reference an agent, not to let the
    database remove them.
    """
    for model_name, field in (
        ('Conversation', 'agent'),
        ('AgentToolLink', 'agent'),
        ('MCPCallLog', 'agent'),
        ('ApprovalRequest', 'agent'),
        ('SocialPost', 'agent'),
        ('EmailOutreach', 'agent'),
        ('Lead', 'agent'),
    ):
        try:
            model = apps.get_model('marketing', model_name)
        except LookupError:
            continue
        if not any(f.name == field for f in model._meta.get_fields()):
            continue

        if model_name == 'AgentToolLink':
            # A through row is unique on (agent, tool), so a straight update
            # would collide wherever the target already holds the same tool.
            existing = set(model.objects.filter(agent=target).values_list(
                'tool_id', flat=True))
            model.objects.filter(agent=source, tool_id__in=existing).delete()

        model.objects.filter(**{field: source}).update(**{field: target})


class Migration(migrations.Migration):

    dependencies = [
        ('marketing', '0012_workforce_os_platform'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
