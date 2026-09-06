"""Data migration: collapse per-user resources into one shared workspace.

WHY
---
The application used to give every account its own private copy of the AI
employees, the LLM providers and the MCP servers. It now serves a single
organisation, so there must be exactly one of each. Before the new
workspace-wide unique constraints can be added in 0007, the duplicates that
already exist have to be merged.

HOW
---
For each group of duplicates a canonical row is chosen -- the one owned by the
lowest-numbered superuser, falling back to the lowest primary key -- and every
foreign key pointing at a duplicate is repointed at the canonical row before
the duplicate is deleted.

The merge cascades, because the resources are nested:

    LLMProvider  ->  LLMModel      ->  AIAgent.llm_model
    MCPServer    ->  MCPTool       ->  AgentToolLink.tool, MCPCallLog.tool
    AIAgent      ->  eight reverse foreign keys, plus AgentToolLink.agent

AgentToolLink is handled last and separately: once agents and tools have both
been merged, two previously distinct links can collapse onto the same
(agent, tool) pair, which its own unique constraint forbids.

Reversing this migration cannot recreate deleted rows, so it is marked
irreversible rather than pretending otherwise.
"""

from django.db import migrations


def _canonical_ids(model, key_field, superuser_ids):
    """Choose one surviving row per key, and list the ones to fold into it.

    Returns {key: (keeper_id, [duplicate_ids])}.
    """
    grouped = {}
    for row in model.objects.all().order_by('pk'):
        grouped.setdefault(getattr(row, key_field), []).append(row)

    resolved = {}
    for key, rows in grouped.items():
        owned_by_superuser = [r for r in rows if r.user_id in superuser_ids]
        keeper = (owned_by_superuser or rows)[0]
        resolved[key] = (keeper.pk, [r.pk for r in rows if r.pk != keeper.pk])
    return resolved


def _repoint_links(AgentToolLink, field, old_id, new_id):
    """Move AgentToolLink rows from one parent to another without collisions.

    AgentToolLink is unique on (agent, tool). Repointing either half can land a
    row on a pair that already exists, so each row is moved only when the
    target pair is free, and dropped as a duplicate when it is not.
    """
    other = 'tool_id' if field == 'agent_id' else 'agent_id'
    taken = set(
        AgentToolLink.objects.filter(**{field: new_id}).values_list(other, flat=True))

    for link in AgentToolLink.objects.filter(**{field: old_id}).order_by('pk'):
        partner = getattr(link, other)
        if partner in taken:
            AgentToolLink.objects.filter(pk=link.pk).delete()
        else:
            AgentToolLink.objects.filter(pk=link.pk).update(**{field: new_id})
            taken.add(partner)


def merge_workspace(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    AIAgent = apps.get_model('marketing', 'AIAgent')
    AgentToolLink = apps.get_model('marketing', 'AgentToolLink')
    ApprovalRequest = apps.get_model('marketing', 'ApprovalRequest')
    Conversation = apps.get_model('marketing', 'Conversation')
    EmailOutreach = apps.get_model('marketing', 'EmailOutreach')
    Lead = apps.get_model('marketing', 'Lead')
    LLMModel = apps.get_model('marketing', 'LLMModel')
    LLMProvider = apps.get_model('marketing', 'LLMProvider')
    MarketingCampaign = apps.get_model('marketing', 'MarketingCampaign')
    MCPCallLog = apps.get_model('marketing', 'MCPCallLog')
    MCPServer = apps.get_model('marketing', 'MCPServer')
    MCPTool = apps.get_model('marketing', 'MCPTool')
    SocialPost = apps.get_model('marketing', 'SocialPost')

    superuser_ids = set(
        User.objects.filter(is_superuser=True).order_by('pk').values_list('pk', flat=True))

    # ---------------------------------------------------------------- providers
    #
    # A child row cannot simply be reassigned to the keeper: the keeper almost
    # always owns a row with the same natural key already, and moving one on to
    # it would breach the existing per-parent unique constraint. So each child
    # is handled individually -- moved when the keeper has no equivalent, and
    # folded into the keeper's own row when it does.
    for _key, (keeper_id, duplicate_ids) in _canonical_ids(
            LLMProvider, 'provider_key', superuser_ids).items():
        if not duplicate_ids:
            continue

        keeper_models = {
            row.model_id: row.pk
            for row in LLMModel.objects.filter(provider_id=keeper_id)
        }
        for child in LLMModel.objects.filter(provider_id__in=duplicate_ids).order_by('pk'):
            equivalent_id = keeper_models.get(child.model_id)
            if equivalent_id is None:
                LLMModel.objects.filter(pk=child.pk).update(provider_id=keeper_id)
                keeper_models[child.model_id] = child.pk
            else:
                AIAgent.objects.filter(llm_model_id=child.pk).update(llm_model_id=equivalent_id)
                LLMModel.objects.filter(pk=child.pk).delete()

        LLMProvider.objects.filter(pk__in=duplicate_ids).delete()

    # -------------------------------------------------------------- MCP servers
    for _key, (keeper_id, duplicate_ids) in _canonical_ids(
            MCPServer, 'server_key', superuser_ids).items():
        if not duplicate_ids:
            continue

        keeper_tools = {
            row.tool_name: row.pk
            for row in MCPTool.objects.filter(server_id=keeper_id)
        }
        for child in MCPTool.objects.filter(server_id__in=duplicate_ids).order_by('pk'):
            equivalent_id = keeper_tools.get(child.tool_name)
            if equivalent_id is None:
                MCPTool.objects.filter(pk=child.pk).update(server_id=keeper_id)
                keeper_tools[child.tool_name] = child.pk
            else:
                _repoint_links(AgentToolLink, 'tool_id', child.pk, equivalent_id)
                MCPCallLog.objects.filter(tool_id=child.pk).update(tool_id=equivalent_id)
                MCPTool.objects.filter(pk=child.pk).delete()

        MCPServer.objects.filter(pk__in=duplicate_ids).delete()

    # ------------------------------------------------------------- AI employees
    for _key, (keeper_id, duplicate_ids) in _canonical_ids(
            AIAgent, 'agent_type', superuser_ids).items():
        if not duplicate_ids:
            continue
        for duplicate_id in duplicate_ids:
            _repoint_links(AgentToolLink, 'agent_id', duplicate_id, keeper_id)

        for model, field in [
            (Conversation, 'agent_id'),
            (ApprovalRequest, 'agent_id'),
            (MCPCallLog, 'agent_id'),
            (MarketingCampaign, 'assigned_agent_id'),
            (Lead, 'discovered_by_id'),
            (SocialPost, 'agent_id'),
            (EmailOutreach, 'agent_id'),
        ]:
            model.objects.filter(**{f'{field}__in': duplicate_ids}).update(**{field: keeper_id})
        AIAgent.objects.filter(pk__in=duplicate_ids).delete()

    # ------------------------------------------------ collapse duplicated links
    # Merging agents and tools can leave two links on the same pair, which
    # AgentToolLink's own unique constraint forbids.
    seen_links = {}
    for row in AgentToolLink.objects.all().order_by('pk'):
        seen_links.setdefault((row.agent_id, row.tool_id), []).append(row.pk)
    for _pair, ids in seen_links.items():
        if len(ids) > 1:
            AgentToolLink.objects.filter(pk__in=ids[1:]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('marketing', '0005_chat_conversations'),
    ]

    operations = [
        migrations.RunPython(merge_workspace, migrations.RunPython.noop),
    ]
