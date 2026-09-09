"""Views for the platform layer: integrations, settings and the audit log.

WHY THIS MODULE IS SEPARATE FROM views.py
-----------------------------------------
views.py holds the six original workspace pages. The three pages here are the
ones that make the project's central promise true for somebody who never opens
an editor: every connected application is configured from a web page, every
company-wide setting is a database row edited in a form, and everything either
of those causes is written to an append-only history that a person can read.
Keeping them in their own module means the platform layer can be read on its
own, and that neither file has to be opened to understand the other.

THE THREE PAGES
---------------
``integrations_view``        every connected application, grouped by category,
                             each one honest about whether a call would reach
                             the real service or be simulated.
``integration_detail_view``  one connection in full: its configuration with
                             secrets masked, the employees and capabilities
                             that depend on it, and everything it has done.
``settings_view``            every SystemSetting, grouped, with a real form
                             control chosen from its declared value type.
``audit_log_view``           the append-only history, filterable and paged.

SECRETS
-------
A credential is written to ``Integration.secrets`` and is never read back out.
No view in this module renders a secret value into HTML, returns one in JSON or
puts one in a data attribute. The interface reports only whether a value is
present, as "(currently set)" or "(not set)", which is the only fact a person
actually needs in order to decide whether to paste a new one.

APPROVAL
--------
Configuring an integration and changing a global setting are themselves
governed actions. When ``marketing/tools/platform.py`` registers the tools that
perform them, these endpoints route the change through the tool layer, which
may answer "queued for approval" rather than "saved" -- and the endpoints say
so rather than pretending the change landed. The tool module is written
separately from this one, so every call into it is guarded: if it is absent or
its arguments differ, the change is applied here instead and the audit entry
records that it was applied directly.

ACCESS CONTROL
--------------
Every page requires authentication. Every endpoint requires authentication, a
POST, Django's CSRF token, and the matching permission from marketing/roles.py.
The permission is checked in the view as well as in the template, because a
button that is not rendered is a courtesy and not a control: somebody who
knows the URL can still call the endpoint.
"""

import json
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import audit, integrations, roles
from .models import AIAgent
from .models_platform import (AuditEvent, CalendarEvent, ExternalIssue, Integration,
                              OutboundMessage, ProposedAction, SystemSetting)


# ===========================================================================
# Helpers
#
# These four are deliberately the same shape as the ones in views.py rather
# than imported from it. They are three lines each, and copying them keeps
# this module independent of a file it does not own -- so a change to the
# other module's private helpers cannot silently change what these endpoints
# return.
# ===========================================================================

def _json_body(request):
    """Parse a JSON request body, tolerating an empty or malformed one."""
    try:
        return json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _error(message, status=400):
    return JsonResponse({'status': 'error', 'message': message}, status=status)


def _forbidden(perm):
    """Refusal for a JSON endpoint the caller lacks the permission for."""
    return _error(f'Your role does not allow this action ({perm}).', status=403)


def _requires(request, perm):
    """True when the caller may proceed. Superusers always may."""
    return request.user.has_perm(perm)


def _reverse_or_blank(name, *args):
    """The path for a named route, or '' when that route is not mounted.

    The proposed-action queue and its detail page are built by a different
    part of the project. Linking to them with {% url %} directly would raise
    NoReverseMatch and take the whole page down if they are not mounted yet,
    which is a poor trade for a convenience link, so the path is resolved
    here and the template simply omits the link when it is empty.
    """
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ''


# ===========================================================================
# Presentation of one integration
#
# The mode wording lives here, in one dictionary, because three different
# places need exactly the same words: the card on the Integrations page, the
# status panel on the detail page, and static/js/integrations.js when it
# repaints a card after a connection test. The dictionary is handed to the
# browser with json_script, so the JavaScript does not carry its own copy of
# the sentences and the two cannot drift apart.
# ===========================================================================

MODE_META = {
    'live': {
        'label': 'Live',
        'tone': 'success',
        'icon': 'fa-bolt',
        'explanation': 'Live -- actions reach the real service.',
    },
    'demo': {
        'label': 'Demo',
        'tone': 'warning',
        'icon': 'fa-clone',
        'explanation': 'Demo -- actions are simulated and labelled.',
    },
    'unavailable': {
        'label': 'Unavailable',
        'tone': 'danger',
        'icon': 'fa-triangle-exclamation',
        'explanation': 'Live only, but not configured -- calls fail instead of simulating.',
    },
    'disabled': {
        'label': 'Disabled',
        'tone': 'neutral',
        'icon': 'fa-power-off',
        'explanation': 'Switched off -- calls fail rather than being simulated.',
    },
}

STATUS_TONES = {
    'connected': 'success',
    'demo': 'warning',
    'degraded': 'warning',
    'failed': 'danger',
    'disabled': 'neutral',
    'unknown': 'neutral',
}

# Font Awesome ships these as brand glyphs rather than solid ones, so the
# card has to pick the right style prefix or the icon renders as a blank box.
BRAND_ICONS = {
    'fa-slack', 'fa-github', 'fa-google', 'fa-instagram', 'fa-linkedin',
    'fa-jira', 'fa-confluence', 'fa-atlassian', 'fa-google-drive', 'fa-notion',
}


def _config_rows(integration, connector):
    """One row per ConfigField, safe to render.

    A secret contributes ``is_set`` and nothing else. Its value is never put
    into the returned structure, so no template can render it even by mistake.
    """
    if connector is None:
        return []

    config = integration.config or {}
    rows = []
    for spec in connector.config_fields:
        entry = spec.as_dict()
        entry['is_set'] = connector.has(spec.key)
        if entry['secret']:
            entry['value'] = ''
        else:
            # The stored value where there is one, otherwise the connector's
            # declared default, so the form shows what a call would actually
            # use rather than an empty box.
            current = config.get(spec.key)
            if current in (None, ''):
                current = spec.default if spec.default is not None else ''
            entry['value'] = current
            # A checkbox needs a real boolean, not the string 'False'.
            if entry['field_type'] == 'boolean':
                entry['value'] = bool(current) and str(current).lower() not in ('false', '0')
        rows.append(entry)
    return rows


def _card(integration):
    """Everything one integration card or panel needs, worked out once."""
    connector = integration.connector
    mode = integration.effective_mode
    return {
        'row': integration,
        'connector': connector,
        'has_connector': connector is not None,
        'mode': mode,
        'mode_meta': MODE_META.get(mode, MODE_META['disabled']),
        'status_tone': STATUS_TONES.get(integration.connection_status, 'neutral'),
        'missing': integration.missing_settings,
        'fields': _config_rows(integration, connector),
        'operations': list(connector.operations) if connector else [],
        'docs_url': getattr(connector, 'docs_url', '') if connector else '',
        'credential_help': _credential_help(connector),
        'is_brand_icon': integration.icon in BRAND_ICONS,
        'mode_choices': Integration.MODE_CHOICES,
    }


def _credential_help(connector):
    """The help text of the required secret, which is the 'where do I get this'.

    Surfacing it beside the field is the whole point: the moment a person
    needs to know where an app password or a bot token comes from is the
    moment they are looking at the empty box for it.
    """
    if connector is None:
        return []
    return [{'label': spec.label, 'help_text': spec.help_text}
            for spec in connector.config_fields
            if spec.is_secret and spec.help_text]


def _dependants(provider_key):
    """Which AI employees and capabilities stop working without this connection.

    Derived from the tool registry rather than from a list written by hand, so
    the answer cannot go stale: a capability appears here because a registered
    tool declares this integration, which is the same fact that makes the
    capability real.

    The registry imports every tool module, and those modules are written
    elsewhere in the project, so an import failure is reported as "not known
    yet" rather than taken down the page with it.
    """
    try:
        from . import tools
    except ImportError:
        return [], 0, False

    try:
        entries = [entry for entry in tools.all_tools()
                   if entry.integration == provider_key]
    except Exception:  # noqa: BLE001 -- a broken registry must not break the page
        return [], 0, False

    labels = dict(AIAgent.AGENT_TYPE_CHOICES)
    agents = {agent.agent_type: agent for agent in AIAgent.objects.all()}

    grouped = {}
    for entry in entries:
        targets = labels.keys() if entry.shared else entry.agent_types
        for agent_type in targets:
            if agent_type in labels:
                grouped.setdefault(agent_type, []).append(entry)

    rows = [{
        'agent_type': agent_type,
        'label': labels[agent_type],
        'agent': agents.get(agent_type),
        'capabilities': sorted(items, key=lambda e: (e.group, e.capability)),
    } for agent_type, items in grouped.items()]

    return sorted(rows, key=lambda row: row['label']), len(entries), True


def _integration_payload(integration, message=''):
    """The JSON body every integration endpoint answers with.

    One shape for all four endpoints, so static/js/integrations.js has exactly
    one function that repaints a card. Nothing here is a secret: the counters,
    the mode and the status message are all facts about the connection rather
    than facts about the credential.
    """
    mode = integration.effective_mode
    return {
        'status': 'success',
        'provider_key': integration.provider_key,
        'name': integration.name,
        'message': message or integration.last_status_message,
        'is_enabled': integration.is_enabled,
        'mode': integration.mode,
        'mode_display': integration.get_mode_display(),
        'effective_mode': mode,
        'connection_status': integration.connection_status,
        'status_display': integration.get_connection_status_display(),
        'status_tone': STATUS_TONES.get(integration.connection_status, 'neutral'),
        'missing': integration.missing_settings,
        'is_configured': integration.is_configured,
        'call_count': integration.call_count,
        'live_call_count': integration.live_call_count,
        'demo_call_count': integration.demo_call_count,
        'checked_at': (timezone.localtime(integration.last_checked_at)
                       .strftime('%d %b %Y at %H:%M')
                       if integration.last_checked_at else ''),
    }


# ===========================================================================
# Page 1: Integrations
# ===========================================================================

@login_required(login_url='login')
def integrations_view(request):
    """Every connected application, grouped by what it is for.

    The page exists to answer one question honestly for each row: would an
    action taken right now reach the real service, or be simulated? That is
    ``Integration.effective_mode``, and it is not the same thing as "is there
    a credential" -- an integration can be configured and still deliberately
    held in demo mode, or set to live only and be unable to run at all. The
    card shows the effective answer and the reason for it.
    """
    # Idempotent, and cheap when the table is already populated: a connector
    # added to the project should appear here without anyone running a command.
    if not Integration.objects.exists():
        try:
            integrations.ensure_integrations(owner=request.user)
        except Exception:  # noqa: BLE001 -- an empty page beats a 500
            pass

    rows = list(Integration.objects.all())
    cards = [_card(row) for row in rows]

    groups = []
    for value, label in Integration.CATEGORY_CHOICES:
        members = [card for card in cards if card['row'].category == value]
        if members:
            groups.append({'key': value, 'label': label, 'integrations': members})

    modes = [card['mode'] for card in cards]

    context = {
        'groups': groups,
        'cards': cards,
        'mode_meta': MODE_META,
        'mode_choices': Integration.MODE_CHOICES,
        'total_count': len(cards),
        'live_count': modes.count('live'),
        'demo_count': modes.count('demo'),
        'unavailable_count': modes.count('unavailable'),
        'disabled_count': modes.count('disabled'),
        'total_calls': sum(card['row'].call_count for card in cards),
        'live_calls': sum(card['row'].live_call_count for card in cards),
        'demo_calls': sum(card['row'].demo_call_count for card in cards),
        'actions_url': _reverse_or_blank('actions'),
    }
    return render(request, 'integrations.html', context)


@login_required(login_url='login')
def integration_detail_view(request, provider_key):
    """One connection in full, including what stops working without it.

    The dependants section is the reason this page is worth having. A status
    badge tells somebody that Slack is not configured; this page tells them
    that four of the HR employee's capabilities and two of the Engineering
    Manager's are therefore being simulated, which is the fact they need in
    order to decide whether to care.
    """
    integration = get_object_or_404(Integration, provider_key=provider_key)
    card = _card(integration)
    dependants, capability_count, registry_available = _dependants(provider_key)

    context = {
        'integration': integration,
        'card': card,
        'mode_meta': MODE_META,
        'mode_choices': Integration.MODE_CHOICES,
        'dependants': dependants,
        'capability_count': capability_count,
        'registry_available': registry_available,
        'events': (AuditEvent.objects.filter(integration=integration)
                   .select_related('actor', 'agent', 'proposed_action')[:25]),
        'actions': (ProposedAction.objects.filter(integration=integration)
                    .select_related('agent', 'decided_by')[:15]),
        'messages_sent': (OutboundMessage.objects.filter(integration=integration)
                          .select_related('agent')[:15]),
        # CalendarEvent and ExternalIssue carry no integration column of their
        # own, so they are reached through the approved action that created
        # them. That is the only honest link available, and it is enough.
        'calendar_events': (CalendarEvent.objects.filter(action__integration=integration)
                            .select_related('agent')[:15]),
        'external_issues': (ExternalIssue.objects.filter(action__integration=integration)
                            .select_related('agent')[:15]),
        'actions_url': _reverse_or_blank('actions'),
        'action_detail_available': bool(_reverse_or_blank('action_detail', 1)),
    }
    return render(request, 'integration_detail.html', context)


# ===========================================================================
# Page 3: Platform settings
# ===========================================================================

@login_required(login_url='login')
@permission_required('marketing.view_systemsetting', raise_exception=True)
def settings_view(request):
    """The company profile and working rules the AI employees read.

    These are settings rather than constants because an AI employee reads them
    before it writes anything: the company name in an email signature, the
    tone of voice in a caption, the working hours a meeting is scheduled
    inside. Putting them in a database row edited from this page is what lets
    somebody change how the whole workforce behaves without touching code.
    """
    if not SystemSetting.objects.exists():
        _seed_settings(request)

    rows = list(SystemSetting.objects.all())

    groups = []
    for value, label in SystemSetting.GROUP_CHOICES:
        members = [row for row in rows if row.group == value]
        if members:
            groups.append({
                'key': value,
                'label': label,
                'settings': [_setting_row(row) for row in members],
            })

    # A group value that is not in GROUP_CHOICES would otherwise vanish from
    # the page entirely, which is the one failure a settings screen must not
    # have: a setting that exists, is read by the workforce, and cannot be seen.
    known = {value for value, _label in SystemSetting.GROUP_CHOICES}
    orphans = [row for row in rows if row.group not in known]
    if orphans:
        groups.append({'key': 'other', 'label': 'Other Settings',
                       'settings': [_setting_row(row) for row in orphans]})

    context = {
        'groups': groups,
        'total_count': len(rows),
        'secret_count': sum(1 for row in rows if row.is_secret),
        'actions_url': _reverse_or_blank('actions'),
    }
    return render(request, 'platform_settings.html', context)


def _seed_settings(request):
    """Create the default settings when the table is empty.

    The defaults are declared beside the tools that read them, in
    marketing/tools/platform.py, which is the right place for them: whoever
    adds a setting adds it next to the code that consults it. Provisioning
    normally does this at login.

    Two routes are tried because this page must not be the one that renders
    empty while every other part of the platform assumes the rows exist:
    ``platform.ensure_defaults`` is the tool module's own entry point and is
    documented as immediate and idempotent, and ``provisioning.ensure_settings``
    is the older one. Both are guarded -- an empty page is a poor outcome, but
    a 500 on the settings screen is a worse one.
    """
    try:
        from . import tools
        if tools.get_tool('platform.ensure_defaults') is not None:
            tools.run('platform.ensure_defaults', tools.ToolContext(user=request.user))
            if SystemSetting.objects.exists():
                return
    except Exception:  # noqa: BLE001
        pass

    try:
        from . import provisioning
        provisioning.ensure_settings(request.user)
    except Exception:  # noqa: BLE001
        pass


def _setting_row(setting):
    """One SystemSetting, prepared for a form control.

    A secret contributes ``is_set`` and never its value, exactly as an
    integration credential does.
    """
    value = setting.resolved
    entry = {
        'row': setting,
        'is_set': value not in (None, '', [], {}),
        'value': '',
        'text_value': '',
        'bool_value': False,
        'choices': _setting_choices(setting),
    }
    if setting.is_secret:
        return entry

    entry['value'] = value
    if setting.value_type == 'boolean':
        entry['bool_value'] = bool(value)
    elif setting.value_type == 'json':
        try:
            entry['text_value'] = json.dumps(value, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            entry['text_value'] = str(value)
    elif value in (None, ''):
        entry['text_value'] = ''
    else:
        entry['text_value'] = str(value)
    return entry


def _setting_choices(setting):
    """The choice list as (value, label) pairs, whatever shape it was stored in.

    A seed can reasonably write either ``["formal", "friendly"]`` or
    ``[["formal", "Formal"], ...]``, and a settings page that only understood
    one of those would render an empty dropdown for the other.
    """
    pairs = []
    for entry in (setting.choices or []):
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            pairs.append((entry[0], entry[1]))
        elif isinstance(entry, dict):
            pairs.append((entry.get('value'), entry.get('label', entry.get('value'))))
        else:
            pairs.append((entry, str(entry).replace('_', ' ').capitalize()))
    return pairs


# ===========================================================================
# Page 4: Audit log
# ===========================================================================

AUDIT_DAY_CHOICES = [
    ('1', 'Today'),
    ('7', 'Last 7 days'),
    ('30', 'Last 30 days'),
    ('90', 'Last 90 days'),
    ('0', 'All time'),
]


@login_required(login_url='login')
@permission_required(roles.CAN_VIEW_AUDIT, raise_exception=True)
def audit_log_view(request):
    """The append-only history of everything the platform did.

    Two things make this page worth reading rather than merely present. The
    first is that a simulated action is visually distinct from a real one, so
    nobody can mistake demo output for something that left the building. The
    second is that the filters are ordinary GET parameters, so a narrowed view
    is a URL somebody can send to a colleague.
    """
    category = (request.GET.get('category') or 'all').strip()
    status = (request.GET.get('status') or 'all').strip()
    agent_filter = (request.GET.get('agent') or 'all').strip()
    app_filter = (request.GET.get('app') or 'all').strip()
    query = (request.GET.get('q') or '').strip()
    days = (request.GET.get('days') or '30').strip()

    events = AuditEvent.objects.select_related(
        'actor', 'agent', 'integration', 'proposed_action', 'task')

    if category != 'all':
        events = events.filter(category=category)
    if status != 'all':
        events = events.filter(status=status)
    if agent_filter not in ('all', ''):
        if agent_filter == 'none':
            events = events.filter(agent__isnull=True)
        elif agent_filter.isdigit():
            events = events.filter(agent_id=int(agent_filter))
    if app_filter != 'all':
        events = events.filter(target_app=app_filter)
    if query:
        events = events.filter(
            Q(action__icontains=query)
            | Q(message__icontains=query)
            | Q(object_label__icontains=query))

    window_days = 0
    if days.isdigit() and int(days) > 0:
        window_days = int(days)
        events = events.filter(
            created_at__gte=timezone.now() - timedelta(days=window_days))

    paginator = Paginator(events, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    # The filter values, rebuilt as a query string so every pagination link
    # keeps the narrowed view. Written here rather than in the template
    # because a template cannot drop the empty ones.
    # urlencode rather than string joining: a search term with a space or an
    # ampersand in it, or an application named "Google Drive", would otherwise
    # produce a pagination link that silently loses the filter.
    querystring = urlencode([
        (key, value) for key, value, default in (('category', category, 'all'),
                                                 ('status', status, 'all'),
                                                 ('agent', agent_filter, 'all'),
                                                 ('app', app_filter, 'all'),
                                                 ('q', query, ''),
                                                 ('days', days, '30'))
        if value and value != default])

    today = timezone.localtime(timezone.now()).replace(
        hour=0, minute=0, second=0, microsecond=0)

    # The distinct applications actually named in the log, so the dropdown
    # offers what happened rather than what might have.
    apps = sorted({name for name in AuditEvent.objects
                   .exclude(target_app='')
                   .values_list('target_app', flat=True)
                   .distinct() if name})

    context = {
        'page_obj': page_obj,
        'events': page_obj.object_list,
        'querystring': querystring,
        'category_filter': category,
        'status_filter': status,
        'agent_filter': agent_filter,
        'app_filter': app_filter,
        'search_query': query,
        'days_filter': days,
        'window_days': window_days,
        'categories': AuditEvent.CATEGORY_CHOICES,
        'statuses': AuditEvent.STATUS_CHOICES,
        'day_choices': AUDIT_DAY_CHOICES,
        'agents': AIAgent.objects.only('id', 'name').order_by('name'),
        'apps': apps,
        'matched_count': paginator.count,
        'today_count': AuditEvent.objects.filter(created_at__gte=today).count(),
        'executed_count': events.filter(status='ok').count(),
        'simulated_count': events.filter(status='demo').count(),
        'refused_count': events.filter(status__in=('failed', 'denied')).count(),
        'has_filters': bool(querystring),
        'actions_url': _reverse_or_blank('actions'),
        'action_detail_available': bool(_reverse_or_blank('action_detail', 1)),
    }
    return render(request, 'audit_log.html', context)


# ===========================================================================
# The approval bridge
#
# Configuring an integration and changing a setting are governed actions. When
# the platform tools are registered, a change made here goes through them and
# may come back as a pending ProposedAction rather than a completed write.
#
# The tools live in a module written separately from this one, so nothing here
# assumes it exists or assumes the exact names of its arguments. The bridge
# below tries, checks, and falls back to writing the change directly with an
# audit entry that says it was applied directly. A settings page that stops
# working because a sibling module is half-finished would be a worse outcome
# than one that applies the change and records how.
#
# WHICH CHANGES GO TO THE QUEUE, AND WHY NOT ALL OF THEM
# One rule decides it: a change that could cause an action to reach the outside
# world is proposed, and a change that can only reduce reach is applied at
# once.
#
#     settings and credentials  ->  platform.configure_integration   proposed
#     mode                      ->  platform.set_integration_mode    proposed
#     switching on              ->  platform.enable_integration      proposed
#     switching off             ->  applied immediately
#
# Switching something off is the exception on purpose. The usual reason to
# disable an integration is that it is misbehaving, and an approval queue
# standing between a person and the off switch during an incident would be a
# governance control working against the thing it exists to protect. Stopping
# is always allowed; starting is what needs a second person.
#
# The other exception is a secret. A pending action is deliberately displayed
# and editable on the review page, and the tool layer records its arguments in
# the audit detail, so a credential is never handed to the queue: it is
# written straight to the row it belongs on, where nothing reads it back out.
# ===========================================================================

# The argument each value might be called, in order of preference. A tool that
# accepts none of the names for the subject is not the tool we are looking for,
# and the bridge declines rather than guessing.
_ARGUMENT_ALIASES = {
    'subject': ('provider_key', 'integration', 'integration_key', 'key', 'provider'),
    'settings': ('settings', 'config', 'values', 'fields', 'options'),
    'mode': ('mode',),
    'setting_key': ('key', 'setting', 'setting_key', 'name'),
    'value': ('value', 'new_value'),
}


def _platform_tool(name):
    """One registered platform tool, or None if the registry cannot answer."""
    try:
        from . import tools
    except ImportError:
        return None
    try:
        return tools.get_tool(name)
    except Exception:  # noqa: BLE001
        return None


def _map_arguments(entry, wanted):
    """Fit our values onto whatever argument names the tool declares.

    Returns None when a required value has no home in the tool's schema, which
    is the signal to stop and apply the change directly instead of calling a
    tool with arguments it will reject.
    """
    accepted = set((entry.parameters or {}).get('properties') or {})
    if not accepted:
        return None

    arguments = {}
    for role, value in wanted.items():
        if value is None:
            continue
        target = next((alias for alias in _ARGUMENT_ALIASES.get(role, (role,))
                       if alias in accepted), None)
        if target is None:
            # The subject and the key are not optional: without them the tool
            # cannot know what it is being asked to change.
            if role in ('subject', 'setting_key'):
                return None
            continue
        arguments[target] = value
    return arguments


def _run_platform_tool(request, tool_name, wanted):
    """Try to make a change through the tool layer.

    Returns (handled, response). ``handled`` is False when the tool layer
    could not be used at all, in which case the caller applies the change
    itself.
    """
    entry = _platform_tool(tool_name)
    if entry is None:
        return False, None

    arguments = _map_arguments(entry, wanted)
    if arguments is None:
        return False, None

    from . import tools

    context = tools.ToolContext(user=request.user)
    result = tools.run(tool_name, context, arguments)

    if result.awaiting_approval:
        return True, JsonResponse({
            'status': 'pending',
            'queued': True,
            'action_id': result.pending_action_id,
            'message': (result.text
                        or 'The change is waiting in the approval queue. '
                           'Nothing has been altered yet.'),
        })

    if not result.ok:
        # A rejected argument set is not a reason to give up on the change:
        # the caller applies it directly, which is what would have happened
        # if the tool had never been registered.
        return False, None

    return True, JsonResponse({
        'status': 'success',
        'queued': False,
        'message': result.text or 'The change was applied.',
    })


def _merge_queued(response, integration):
    """Add the integration's current state to a bridge response.

    The browser repaints a card from whatever comes back, and after a queued
    change the current state is the OLD state -- which is exactly right: the
    switch flicks back, the mode pill does not move, and nothing on the screen
    suggests a change that has not happened yet.
    """
    payload = json.loads(response.content)
    payload.update({key: value
                    for key, value in _integration_payload(integration).items()
                    if key not in ('status', 'message')})
    return JsonResponse(payload, status=response.status_code)


# ===========================================================================
# Endpoints: integrations
# ===========================================================================

def _coerce_config_value(spec, raw):
    """Turn one submitted form value into what the connector expects.

    A number field that arrives as the string '587' has to be stored as 587,
    because the connector passes it to smtplib, and a checkbox that arrives
    as the string 'false' has to be stored as False rather than as a truthy
    non-empty string.
    """
    field_type = spec.get('field_type')

    if field_type == 'boolean':
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ('1', 'true', 'on', 'yes')

    if field_type == 'number':
        if raw in (None, ''):
            return ''
        try:
            text = str(raw).strip()
            return int(text) if text.lstrip('-').isdigit() else float(text)
        except (TypeError, ValueError):
            raise ValueError(f'{spec.get("label")} must be a number.')

    if raw is None:
        return ''
    return str(raw).strip()


@login_required(login_url='login')
@require_POST
def api_integration_configure(request):
    """Save the settings for one integration.

    SECRETS ARE NEVER QUEUED. A pending ProposedAction is deliberately visible
    and editable on the review page, so putting a bot token or an app password
    into one would render a credential in a browser. The credential is
    therefore written straight to the Integration row -- where nothing ever
    reads it back out -- and only the non-secret settings and the mode are
    offered to the approval queue. The response says which of the two
    happened, so the interface never claims a queued change was saved.
    """
    if not _requires(request, roles.CAN_CONFIGURE_INTEGRATION):
        return _forbidden(roles.CAN_CONFIGURE_INTEGRATION)

    data = _json_body(request)
    integration = Integration.objects.filter(
        provider_key=data.get('provider_key')).first()
    if integration is None:
        return _error('That integration does not exist. Reload the page.', status=404)

    connector = integration.connector
    if connector is None:
        return _error(
            f'No connector is installed for {integration.provider_key}, so its '
            f'settings cannot be validated. The row can be removed in the admin.',
            status=409)

    submitted = data.get('settings') or {}
    if not isinstance(submitted, dict):
        return _error('The settings must be sent as an object of field names to values.')

    cleared = [str(key) for key in (data.get('clear_secrets') or [])]
    specs = {spec['key']: spec for spec in _config_rows(integration, connector)}

    config_changes = {}
    secret_changes = {}
    removed_secrets = []

    for key, spec in specs.items():
        if spec['secret']:
            if key in cleared:
                removed_secrets.append(key)
                continue
            # A blank secret field means "keep what is stored". It cannot mean
            # "clear it", because the box is blank every time the page loads:
            # the stored value is never sent to the browser.
            raw = submitted.get(key)
            if raw not in (None, ''):
                secret_changes[key] = str(raw).strip()
            continue

        if key not in submitted:
            continue
        try:
            config_changes[key] = _coerce_config_value(spec, submitted.get(key))
        except ValueError as exc:
            return _error(str(exc))

    mode = data.get('mode')
    if mode is not None:
        valid = {value for value, _label in Integration.MODE_CHOICES}
        if mode not in valid:
            return _error(f'"{mode}" is not a mode. Choose one of {", ".join(sorted(valid))}.')

    # Required fields that would still be empty after this save, so the reply
    # can tell the truth about what the integration will actually do.
    missing_after = [spec['label'] for spec in specs.values()
                     if spec['required']
                     and not (spec['is_set'] and spec['key'] not in removed_secrets)
                     and not config_changes.get(spec['key'])
                     and spec['key'] not in secret_changes]

    # -- the credential half, applied here and never queued ----------------
    if secret_changes or removed_secrets:
        secrets = dict(integration.secrets or {})
        secrets.update(secret_changes)
        for key in removed_secrets:
            secrets.pop(key, None)
        integration.secrets = secrets
        integration.configured_by = request.user
        integration.save(update_fields=['secrets', 'configured_by', 'updated_at'])
        parts = []
        if secret_changes:
            parts.append(f'{len(secret_changes)} credential field'
                         f'{"" if len(secret_changes) == 1 else "s"} stored')
        if removed_secrets:
            parts.append(f'{len(removed_secrets)} removed')
        audit.log(
            'integration.credential_changed', category='integration',
            actor=request.user, integration=integration,
            target_app=integration.name, object_label=integration.name,
            # The field names are recorded; the values deliberately are not.
            # That a credential changed is the auditable event, not its value.
            message=f'{", ".join(parts)}. Values are not recorded here.',
            detail={'fields_set': sorted(secret_changes),
                    'fields_removed': sorted(removed_secrets)})

    # -- the non-secret half, offered to the approval queue first -----------
    #
    # Two different tools, because the platform models them as two different
    # decisions: changing a setting and changing whether the integration is
    # live at all. Sending a mode-only change to the configuration tool would
    # fail its own required-argument check, so it is routed to the tool that
    # actually owns that switch.
    queued_response = None
    if config_changes:
        handled, response = _run_platform_tool(
            request, 'platform.configure_integration',
            {'subject': integration.provider_key,
             'settings': config_changes,
             'mode': mode})
        if handled:
            queued_response = response
        else:
            _apply_integration_config(request, integration, config_changes, mode)
    elif mode is not None and mode != integration.mode:
        handled, response = _run_platform_tool(
            request, 'platform.set_integration_mode',
            {'subject': integration.provider_key, 'mode': mode})
        if handled:
            queued_response = response
        else:
            _apply_integration_config(request, integration, {}, mode)

    if queued_response is not None:
        # Re-read, because the credential half may have changed the row.
        integration.refresh_from_db()
        merged = _merge_queued(queued_response, integration)
        if secret_changes or removed_secrets:
            payload = json.loads(merged.content)
            payload['message'] = (
                'The credential was stored. The remaining settings are waiting '
                'in the approval queue and have not been applied yet.')
            return JsonResponse(payload, status=merged.status_code)
        return merged

    integration.refresh_from_db()

    if missing_after:
        message = (f'{integration.name} was saved, but still needs '
                   f'{", ".join(missing_after)}. Until then its actions are '
                   f'{"simulated and labelled" if integration.mode != "live" else "refused"}.')
    else:
        message = f'{integration.name} was saved.'

    return JsonResponse(_integration_payload(integration, message))


def _apply_integration_config(request, integration, config_changes, mode):
    """Write the non-secret half of a configuration change directly.

    Used when the approval tool is not available. The audit entry says
    ``applied_directly`` so that the history distinguishes a change that went
    through the queue from one that did not, rather than making them look the
    same.
    """
    fields = ['updated_at']
    if config_changes:
        config = dict(integration.config or {})
        config.update(config_changes)
        integration.config = config
        fields.append('config')
    if mode is not None and mode != integration.mode:
        integration.mode = mode
        fields.append('mode')
    integration.configured_by = request.user
    fields.append('configured_by')
    integration.save(update_fields=fields)

    audit.log(
        'integration.configured', category='integration', actor=request.user,
        integration=integration, target_app=integration.name,
        object_label=integration.name,
        message=(f'{integration.name} configured directly: '
                 f'{", ".join(sorted(config_changes)) or "mode only"}. '
                 f'Effective mode is now {integration.effective_mode}.'),
        detail={'fields': sorted(config_changes), 'mode': integration.mode,
                'applied_directly': True})


@login_required(login_url='login')
@require_POST
def api_integration_test(request):
    """Check one connection and store the verdict.

    The probe is deliberately allowed to a Manager as well as an
    Administrator: finding out whether a connection works reveals nothing
    about the credential behind it, and somebody investigating why an employee
    is simulating should not need permission to change the credential in order
    to look.
    """
    if not _requires(request, roles.CAN_TEST_INTEGRATION):
        return _forbidden(roles.CAN_TEST_INTEGRATION)

    provider_key = _json_body(request).get('provider_key')
    integration = Integration.objects.filter(provider_key=provider_key).first()
    if integration is None:
        return _error('That integration does not exist. Reload the page.', status=404)

    result = integrations.probe(provider_key)
    integration.refresh_from_db()

    # A probe that reports demo is recorded as demo rather than as a success,
    # so scanning the audit log's outcome column tells the truth about which
    # checks found a real connection and which found a simulation.
    audit_status = {'connected': 'ok', 'demo': 'demo',
                    'disabled': 'denied'}.get(integration.connection_status, 'failed')

    audit.log(
        'integration.tested', category='integration', status=audit_status,
        actor=request.user, integration=integration, target_app=integration.name,
        object_label=integration.name, message=result.get('message', ''),
        detail={'connection_status': result.get('status'),
                'effective_mode': integration.effective_mode})

    payload = _integration_payload(integration, result.get('message', ''))
    payload['status'] = 'success' if result.get('ok') else 'error'
    payload['ok'] = bool(result.get('ok'))
    return JsonResponse(payload)


@login_required(login_url='login')
@require_POST
def api_integration_toggle(request):
    """Switch one integration on or off.

    Switching an integration off does not put it into demo mode. A disabled
    integration refuses calls, because switching something off should stop it
    rather than quietly replace it with a pretend version -- and the card says
    exactly that.

    Switching one ON is proposed for approval, because it restores the ability
    to reach the outside world. Switching one OFF is applied immediately, for
    the reason set out above the bridge: an approval queue between a person
    and the off switch during an incident would be a control working against
    what it protects.
    """
    if not _requires(request, roles.CAN_CONFIGURE_INTEGRATION):
        return _forbidden(roles.CAN_CONFIGURE_INTEGRATION)

    data = _json_body(request)
    integration = Integration.objects.filter(
        provider_key=data.get('provider_key')).first()
    if integration is None:
        return _error('That integration does not exist. Reload the page.', status=404)

    wanted = bool(data.get('enabled'))

    if wanted and not integration.is_enabled:
        handled, response = _run_platform_tool(
            request, 'platform.enable_integration',
            {'subject': integration.provider_key})
        if handled:
            integration.refresh_from_db()
            return _merge_queued(response, integration)

    integration.is_enabled = wanted
    integration.save(update_fields=['is_enabled', 'updated_at'])

    meta = MODE_META.get(integration.effective_mode, MODE_META['disabled'])
    message = (f'{integration.name} was '
               f'{"enabled" if integration.is_enabled else "disabled"}. '
               f'{meta["explanation"]}')

    audit.log('integration.enabled' if integration.is_enabled else 'integration.disabled',
              category='integration', actor=request.user, integration=integration,
              target_app=integration.name, object_label=integration.name,
              message=message, detail={'is_enabled': integration.is_enabled})

    return JsonResponse(_integration_payload(integration, message))


@login_required(login_url='login')
@require_POST
def api_integration_mode(request):
    """Change how one integration decides between live and simulated.

    Kept separate from the configuration endpoint because it is the one
    control on the page whose consequence is not obvious: choosing "live only"
    makes a missing credential an error instead of a simulation, which is what
    somebody wants when a demonstration must not be mistaken for the real
    thing -- and is exactly what they do not want mid-demonstration.
    """
    if not _requires(request, roles.CAN_CONFIGURE_INTEGRATION):
        return _forbidden(roles.CAN_CONFIGURE_INTEGRATION)

    data = _json_body(request)
    integration = Integration.objects.filter(
        provider_key=data.get('provider_key')).first()
    if integration is None:
        return _error('That integration does not exist. Reload the page.', status=404)

    mode = data.get('mode')
    valid = {value for value, _label in Integration.MODE_CHOICES}
    if mode not in valid:
        return _error(f'"{mode}" is not a mode. Choose one of {", ".join(sorted(valid))}.')

    handled, response = _run_platform_tool(
        request, 'platform.set_integration_mode',
        {'subject': integration.provider_key, 'mode': mode})
    if handled:
        integration.refresh_from_db()
        return _merge_queued(response, integration)

    integration.mode = mode
    integration.configured_by = request.user
    integration.save(update_fields=['mode', 'configured_by', 'updated_at'])

    meta = MODE_META.get(integration.effective_mode, MODE_META['disabled'])
    message = f'{integration.name} is set to {integration.get_mode_display()}. {meta["explanation"]}'

    audit.log('integration.mode_changed', category='integration', actor=request.user,
              integration=integration, target_app=integration.name,
              object_label=integration.name, message=message,
              detail={'mode': mode, 'effective_mode': integration.effective_mode,
                      'applied_directly': True})

    return JsonResponse(_integration_payload(integration, message))


# ===========================================================================
# Endpoint: settings
# ===========================================================================

def _coerce_setting_value(setting, raw):
    """Turn one submitted value into what the setting's type promises.

    Raises ValueError with a sentence a person can act on, because this is the
    one place where a typo in a JSON block or a word in a number field is
    caught before it reaches an AI employee that expected a number.
    """
    kind = setting.value_type

    if kind == 'boolean':
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ('1', 'true', 'on', 'yes')

    if kind == 'number':
        if raw in (None, ''):
            raise ValueError(f'{setting.label} needs a number.')
        text = str(raw).strip()
        try:
            return int(text) if text.lstrip('-').isdigit() else float(text)
        except (TypeError, ValueError):
            raise ValueError(f'{setting.label} must be a number, not "{text}".')

    if kind == 'choice':
        allowed = [str(value) for value, _label in _setting_choices(setting)]
        text = '' if raw is None else str(raw)
        if allowed and text not in allowed:
            raise ValueError(
                f'{setting.label} must be one of: {", ".join(allowed)}.')
        return text

    if kind == 'json':
        if isinstance(raw, (dict, list)):
            return raw
        text = ('' if raw is None else str(raw)).strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f'{setting.label} is not valid JSON: {exc.msg} '
                             f'at line {exc.lineno}.')

    return '' if raw is None else str(raw)


@login_required(login_url='login')
@require_POST
def api_setting_update(request):
    """Change one global setting.

    One setting per request, on purpose. Each of these is read by the AI
    employees before they write anything, each is separately audited, and each
    may separately need approval, so a single form control mapping to a single
    recorded change is the shape that keeps the history readable.
    """
    if not _requires(request, roles.CAN_CHANGE_SETTINGS):
        return _forbidden(roles.CAN_CHANGE_SETTINGS)

    data = _json_body(request)
    setting = SystemSetting.objects.filter(key=data.get('key')).first()
    if setting is None:
        return _error('That setting does not exist. Reload the page.', status=404)

    raw = data.get('value')
    if setting.is_secret and raw in (None, ''):
        return _error(f'{setting.label} was left blank, so nothing was changed. '
                      f'The stored value is never sent to the browser, which is '
                      f'why the box is always empty.')

    try:
        value = _coerce_setting_value(setting, raw)
    except ValueError as exc:
        return _error(str(exc))

    # A secret is never handed to the approval queue. A pending action's
    # payload is shown to a reviewer and the tool layer records its arguments
    # in the audit detail, so routing a credential through it would put that
    # credential on two screens. It is written directly instead, and the
    # response says so.
    if not setting.is_secret:
        handled, response = _run_platform_tool(
            request, 'platform.update_setting',
            {'setting_key': setting.key, 'value': value})
        if handled:
            return response

    # The stored shape is the envelope the model documents: resolved() unwraps
    # {'v': ...} and passes anything else through, so writing the envelope is
    # safe whether a seed wrote one or not.
    setting.value = {'v': value}
    setting.updated_by = request.user
    setting.save(update_fields=['value', 'updated_by', 'updated_at'])

    audit.log(
        'setting.changed', category='system', actor=request.user,
        target_app='AI Workforce', object_label=setting.label,
        object_ref=f'systemsetting#{setting.pk}',
        message=f'{setting.label} was changed.',
        # A secret's new value is not recorded in the audit detail either. The
        # fact that it changed is the auditable event; the value is not.
        detail={'key': setting.key,
                'value': '(hidden)' if setting.is_secret else value,
                'applied_directly': True})

    return JsonResponse({
        'status': 'success',
        'queued': False,
        'key': setting.key,
        'is_set': True,
        'message': f'{setting.label} was saved.',
        'updated_at': timezone.localtime(setting.updated_at).strftime('%d %b %Y at %H:%M'),
    })
