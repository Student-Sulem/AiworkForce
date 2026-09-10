"""The integration registry: every connected application in one place.

A connector registers itself by decoration at import time, and this module
keeps the register. Two things then follow for free:

  * ``ensure_integrations`` creates the database row for any connector that
    does not have one yet, so adding a connector file is the whole of adding
    an integration -- no migration, no seed edit, no settings change.

  * ``call`` gives every tool one way to reach a service, which is where the
    demo-mode decision and the usage accounting happen. No tool talks to a
    connector directly.
"""

from .base import CallResult, ConfigField, Connector

_REGISTRY = {}


def register(cls):
    """Class decorator. Adds a connector to the register."""
    if not cls.key:
        raise ValueError(f'{cls.__name__} must define a key.')
    _REGISTRY[cls.key] = cls
    return cls


def connector_classes():
    """Every registered connector class, ordered for display."""
    order = {'communication': 0, 'calendar': 1, 'documents': 2,
             'development': 3, 'social': 4, 'internal': 5}
    return sorted(_REGISTRY.values(),
                  key=lambda c: (order.get(c.category, 9), c.name))


def connector_class(provider_key):
    return _REGISTRY.get(provider_key)


def get_connector(integration):
    """The connector object for one Integration row, or None if unknown.

    An unknown provider_key is not an error: a row can outlive the connector
    file that created it, and the Integrations page should say so rather than
    crash.
    """
    cls = _REGISTRY.get(integration.provider_key)
    return cls(integration) if cls else None


def blueprint():
    """The default Integration row for every registered connector."""
    rows = []
    for cls in connector_classes():
        rows.append({
            'provider_key': cls.key,
            'name': cls.name,
            'description': cls.description,
            'category': cls.category,
            'icon': cls.icon,
            'color': cls.color,
            'config': {spec.key: spec.default
                       for spec in cls.config_fields
                       if spec.default is not None and not spec.is_secret},
        })
    return rows


def ensure_integrations(owner=None):
    """Create any missing Integration row. Idempotent.

    Called on start-up and on every login, so a connector added to the project
    appears on the Integrations page without anyone running anything.
    """
    from ..models_platform import Integration

    created = []
    for row in blueprint():
        integration, was_created = Integration.objects.get_or_create(
            provider_key=row['provider_key'],
            defaults={
                'name': row['name'],
                'description': row['description'],
                'category': row['category'],
                'icon': row['icon'],
                'color': row['color'],
                'config': dict(row['config']),
                'configured_by': owner,
            },
        )
        if was_created:
            created.append(integration)
        else:
            # Keep the presentation fields in step with the connector, but
            # never touch config, secrets, mode or enabled state: those belong
            # to whoever configured the integration.
            changed = []
            for attribute in ('name', 'description', 'category', 'icon', 'color'):
                if getattr(integration, attribute) != row[attribute]:
                    setattr(integration, attribute, row[attribute])
                    changed.append(attribute)
            if changed:
                integration.save(update_fields=changed)
    return created


def get_integration(provider_key):
    """One Integration row by key, or None."""
    from ..models_platform import Integration
    return Integration.objects.filter(provider_key=provider_key).first()


def call(provider_key, operation, **kwargs):
    """Run one operation on one integration.

    The single door between the tool layer and the outside world. Returns a
    CallResult in every case, including when the integration does not exist,
    so a caller never has to guard the call.
    """
    integration = get_integration(provider_key)
    if integration is None:
        return CallResult(
            ok=False, provider=provider_key, operation=operation,
            error=(f'The {provider_key} integration is not registered. '
                   'Open the Integrations page to add it.'))

    connector = get_connector(integration)
    if connector is None:
        return CallResult(
            ok=False, provider=provider_key, operation=operation,
            error=f'No connector is installed for {provider_key}.')

    return connector.call(operation, **kwargs)


def probe(provider_key):
    """Check one connection and store the verdict on its row."""
    from django.utils import timezone

    integration = get_integration(provider_key)
    if integration is None:
        return {'ok': False, 'status': 'failed',
                'message': f'{provider_key} is not registered.'}

    connector = get_connector(integration)
    if connector is None:
        status, message = 'failed', f'No connector installed for {provider_key}.'
    else:
        status, message = connector.probe()

    integration.connection_status = status
    integration.last_status_message = message[:400]
    integration.last_checked_at = timezone.now()
    integration.save(update_fields=['connection_status', 'last_status_message',
                                    'last_checked_at'])

    return {
        'ok': status in ('connected', 'demo'),
        'status': status,
        'status_display': integration.get_connection_status_display(),
        'message': message,
        'effective_mode': integration.effective_mode,
        'missing': integration.missing_settings,
        'checked_at': integration.last_checked_at.strftime('%d %b %Y at %H:%M'),
    }


def probe_all():
    """Check every enabled connection. Used by the Integrations page."""
    from ..models_platform import Integration
    return {row.provider_key: probe(row.provider_key)
            for row in Integration.objects.all()}


# Importing the connector modules is what populates the register. This sits at
# the bottom because each module imports `register` from this one.
from . import (  # noqa: E402,F401  -- side-effect imports, deliberately last
    confluence,
    github,
    gmail,
    google_calendar,
    google_drive,
    jira,
    knowledge_base,
    sentry,
    stripe,
)

__all__ = [
    'CallResult', 'ConfigField', 'Connector', 'register', 'call', 'probe',
    'probe_all', 'get_connector', 'get_integration', 'connector_class',
    'connector_classes', 'blueprint', 'ensure_integrations',
]
