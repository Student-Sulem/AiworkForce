"""The single place an audit entry is written.

Callers say what happened; this module decides what an entry looks like. That
is the whole reason it exists: an audit trail assembled by thirty different
call sites is thirty slightly different shapes, and the first question anyone
asks of it -- "show me everything that touched Gmail" -- stops working.

USAGE

    from . import audit

    audit.log('action.approved', category='approval', actor=request.user,
              agent=action.agent, proposed_action=action,
              target_app=action.target_app, message='Interview invitation approved')

Every argument except the action name is optional, so a call site records what
it knows and nothing more.
"""

from django.utils import timezone


def log(action, *, category='system', status='ok', message='', actor=None, agent=None,
        target_app='', object_label='', object_ref='', task=None, proposed_action=None,
        conversation=None, integration=None, detail=None, duration_ms=0):
    """Write one AuditEvent row and return it.

    Never raises. An audit trail that can break the operation it is recording
    is worse than one with a gap in it, so a failure here is swallowed and
    reported as None.
    """
    from .models_platform import AuditEvent

    try:
        return AuditEvent.objects.create(
            action=action[:90],
            category=category,
            status=status,
            message=str(message)[:400],
            actor=actor if _is_real_user(actor) else None,
            agent=agent,
            target_app=str(target_app)[:60],
            object_label=str(object_label)[:200],
            object_ref=str(object_ref)[:60],
            task=task,
            proposed_action=proposed_action,
            conversation=conversation,
            integration=integration,
            detail=_safe(detail or {}),
            duration_ms=int(duration_ms or 0),
        )
    except Exception:  # noqa: BLE001 -- deliberately total, see docstring
        return None


def log_record(action, obj, *, category='data', **kwargs):
    """Log an event about a database row, filling in the label and reference."""
    kwargs.setdefault('object_label', str(obj)[:200])
    kwargs.setdefault('object_ref', f'{obj._meta.model_name}#{obj.pk}')
    return log(action, category=category, **kwargs)


def _is_real_user(actor):
    """AnonymousUser has no primary key and cannot be a foreign key value."""
    return bool(actor is not None and getattr(actor, 'pk', None))


def _safe(value, _depth=0):
    """Reduce a value to something JSONField will certainly accept.

    Tool arguments arrive from a language model and from view code, so they can
    contain model instances, dates and sets. Coercing here keeps a bad detail
    payload from failing the write.
    """
    if _depth > 6:
        return '...'
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k)[:80]: _safe(v, _depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v, _depth + 1) for v in list(value)[:60]]
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)[:600]


def recent(limit=50, **filters):
    """The newest entries, optionally narrowed. Used by the audit log page."""
    from .models_platform import AuditEvent

    query = AuditEvent.objects.select_related(
        'actor', 'agent', 'integration', 'proposed_action', 'task')
    if filters:
        query = query.filter(**filters)
    return list(query[:limit])


def touch_integration(integration, *, demo, summary=''):
    """Record that an integration was called, and update its counters."""
    if integration is None:
        return
    integration.record_call(demo=demo)
    integration.last_used_at = timezone.now()
