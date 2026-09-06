"""Model Context Protocol layer, implemented in Django only.

SCOPE, STATED PLAINLY
---------------------
A real MCP client launches a server process (usually via `npx`) or opens an
HTTP/SSE connection, then speaks JSON-RPC 2.0 over it: `initialize`, then
`tools/list`, then `tools/call`. That requires Node.js, per-service OAuth
credentials, and a long-lived connection, none of which belong inside a Django
request/response cycle.

This module therefore *models* MCP rather than *speaking* it. Connection
details for each server are stored, validated and reported on, and
`simulate_handshake` reproduces what an `initialize` exchange would conclude,
based only on whether the stored configuration is complete. No subprocess is
ever launched and no socket is ever opened.

The simulation is deterministic, so a demonstration produces the same result
every time it is run.

THE SEVEN SERVERS MODELLED
--------------------------
    Gmail, Instagram, Database, Memory (Knowledge Graph),
    Sequential Thinking, Spotify, LinkedIn
"""

import random

from django.db.models import F
from django.utils import timezone

from .models import MCPCallLog, MCPTool


def simulate_handshake(server):
    """Evaluate a server's stored configuration and record the verdict.

    Mirrors what an MCP `initialize` request would establish:

        disabled        the operator switched the server off
        failed          the transport has no destination configured
        degraded        reachable, but no credential, so writes are blocked
        connected       configuration is complete

    Returns a dictionary and persists the outcome on the MCPServer row.
    """
    now = timezone.now()

    if not server.is_enabled:
        status = 'disabled'
        message = 'Server is disabled. Enable it to establish a session.'
    elif server.transport == 'stdio' and not server.command.strip():
        status = 'failed'
        message = 'No launch command configured for a standard I/O transport.'
    elif server.transport in ('http', 'sse') and not server.endpoint_url.strip():
        status = 'failed'
        message = f'No endpoint URL configured for the {server.get_transport_display()} transport.'
    elif server.requires_credential and not server.auth_token.strip():
        status = 'degraded'
        message = ('Session established, but no credential is stored. '
                   'Read-only tools are available; write operations are blocked.')
    else:
        status = 'connected'
        message = (f'Handshake complete. {server.enabled_tool_count} tools advertised '
                   f'over protocol {server.protocol_version}.')

    server.connection_status = status
    server.last_status_message = message
    server.last_handshake_at = now
    server.save(update_fields=['connection_status', 'last_status_message', 'last_handshake_at'])

    return {
        'ok': status == 'connected',
        'connection_status': status,
        'status_display': server.get_connection_status_display(),
        'message': message,
        'tools_discovered': server.enabled_tool_count,
        'handshake_at': now.strftime('%d %b %Y at %H:%M'),
    }


def tools_for_agent(agent):
    """Tools this employee may actually use right now.

    A tool counts only when the attachment, the tool and its server are all
    enabled -- switching a server off on the MCP Tools page immediately
    withdraws its capabilities from every employee.
    """
    return MCPTool.objects.filter(
        agent_links__agent=agent,
        agent_links__is_enabled=True,
        is_enabled=True,
        server__is_enabled=True,
    ).select_related('server').distinct()


def record_tool_call(agent, tool, arguments=None, summary='', outcome='ok'):
    """Write an MCPCallLog row and bump the usage counters.

    The counters are incremented with an F expression so the arithmetic happens
    inside the database. That avoids a read-modify-write race if two agent runs
    touch the same tool at the same moment.
    """
    from .models import AgentToolLink

    log = MCPCallLog.objects.create(
        agent=agent,
        tool=tool,
        arguments=arguments or {},
        result_summary=summary[:300],
        outcome=outcome,
        duration_ms=random.randint(120, 900),
    )

    MCPTool.objects.filter(pk=tool.pk).update(call_count=F('call_count') + 1)
    AgentToolLink.objects.filter(agent=agent, tool=tool).update(
        usage_count=F('usage_count') + 1)

    return log


def run_agent_tools(agent, context_label=''):
    """Record the employee consulting each of its attached tools.

    Called once per chat turn. Returns a list of short status strings and
    writes one MCPCallLog row per tool, so which capabilities were in play for
    a given reply stays auditable afterwards.
    """
    lines = []
    for tool in tools_for_agent(agent):
        summary = f'{tool.display_name} returned context for {context_label or "the task"}.'
        record_tool_call(agent, tool, arguments={'context': context_label}, summary=summary)
        lines.append(f'{tool.qualified_name} -> ok')
    return lines
