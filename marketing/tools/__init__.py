"""The tool registry: every capability the workforce has.

Importing this package imports every tool module, and importing a tool module
registers its tools. So a capability exists because a function exists, the
roster page lists it because it is registered, and the language model is
offered it because it is registered. There is no separate list to keep in step.

    from . import tools

    tools.tools_for('hr')          # what the HR employee can do
    tools.llm_specs('hr')          # the same, as function definitions
    tools.run('hr.score_candidate', ctx, {'candidate_id': 3})
"""

from .base import (Proposal, Tool, ToolContext, ToolResult, all_tools, editable,
                   executor, get_tool, groups_for, llm_specs, resolve_llm_name,
                   run, tool, tools_for)

# Side-effect imports. Each module registers its tools on import, so the order
# here is the only thing that decides registration order, and nothing else
# depends on it.
from . import (  # noqa: E402,F401
    developer,
    engineering,
    hr,
    marketing_tools,
    platform,
    research,
    support,
)


def capability_blueprint():
    """Every registered tool, grouped by the employee that owns it.

    Used by the provisioner to write AgentCapability rows, which is how the
    employee profile page comes to list real capabilities rather than a
    hand-maintained paragraph.
    """
    from ..models import AIAgent

    per_agent = {key: [] for key, _label in AIAgent.AGENT_TYPE_CHOICES}
    for entry in all_tools():
        targets = per_agent.keys() if entry.shared else entry.agent_types
        for agent_type in targets:
            if agent_type in per_agent:
                per_agent[agent_type].append(entry)
    return per_agent


def ensure_capabilities():
    """Create or refresh the AgentCapability rows from the registry.

    Idempotent, and it withdraws as well as adds: a capability row whose tool
    no longer exists is deleted, so the page can never advertise something the
    platform cannot do.
    """
    from django.utils.text import slugify

    from ..models import AIAgent
    from ..models_platform import AgentCapability

    blueprint = capability_blueprint()
    touched = 0

    for agent in AIAgent.objects.all():
        entries = blueprint.get(agent.agent_type, [])
        wanted = set()
        for order, entry in enumerate(entries):
            slug = slugify(entry.name.replace('.', '-'))[:140]
            wanted.add(slug)
            AgentCapability.objects.update_or_create(
                agent=agent, slug=slug,
                defaults={
                    'group': entry.group,
                    'name': entry.capability,
                    'description': entry.description[:600],
                    'tool_name': entry.name,
                    'integration_key': entry.integration,
                    'requires_approval': entry.requires_approval,
                    'display_order': order,
                },
            )
            touched += 1
        AgentCapability.objects.filter(agent=agent).exclude(slug__in=wanted).delete()

    return touched


__all__ = [
    'Proposal', 'Tool', 'ToolContext', 'ToolResult', 'all_tools', 'editable',
    'executor', 'get_tool', 'groups_for', 'llm_specs', 'resolve_llm_name',
    'run', 'tool', 'tools_for', 'capability_blueprint', 'ensure_capabilities',
]
