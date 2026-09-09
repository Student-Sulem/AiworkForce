"""The connector contract every integration implements.

WHAT A CONNECTOR IS
-------------------
A connector is the code half of an integration. The data half is the
``Integration`` row: endpoint, credential, behaviour, all entered through the
web interface or by an AI employee calling a configuration tool. A connector
therefore never reads an environment variable and never contains a literal
credential. It asks its Integration row for what it needs.

That split is what makes the promise in the brief -- nothing requires a code
edit, nothing requires an environment file -- actually true rather than
aspirational.

DEMO MODE, AND WHY IT IS NOT A LIE
----------------------------------
Most of these services need an OAuth application, a company account and a
review process before they will accept an API call. A demonstration cannot
wait for that, so every connector implements each operation twice:

    live_<operation>    speaks to the real service
    demo_<operation>    produces a faithful, deterministic simulation

``call`` picks between them and *always* reports which one ran, in
``CallResult.demo``. Every layer above passes that flag through: the tool
result says "simulated", the execution record is marked simulated, the audit
entry gets status 'demo', and the interface labels it. A simulated action is
never presented as a real one.

An integration's ``mode`` decides the choice:

    auto  live when the credential is present, simulated when it is not
    live  live only -- a missing credential is an error, not a simulation
    demo  always simulated, even with a working credential

WRITING A CONNECTOR
-------------------
    @register
    class SlackConnector(Connector):
        key = 'slack'
        name = 'Slack'
        category = 'communication'
        config_fields = (
            ConfigField('bot_token', 'Bot token', secret=True, required=True),
            ConfigField('default_channel', 'Default channel', default='#general'),
        )

        def live_post_message(self, channel, text, **kwargs):
            ...
            return self.ok('Posted to #general', {'ts': ts})

        def demo_post_message(self, channel, text, **kwargs):
            return self.simulated(f'Posted to {channel}', {'ts': 'demo-1'})

Anything not implemented falls back to a generic simulation, so a connector is
never the reason a demonstration stops.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 12
USER_AGENT = 'AIWorkforceOS/2.0'


# ===========================================================================
# Configuration fields -- what the interface renders and the agents can set
# ===========================================================================

@dataclass(frozen=True)
class ConfigField:
    """One setting a connector needs, described well enough to render a form.

    The same description drives three things: the field on the Integrations
    page, the argument validation in ``platform.configure_integration``, and
    the "what is still missing" list on the integration card. Declaring it once
    is what keeps those three in step.
    """

    key: str
    label: str
    help_text: str = ''
    field_type: str = 'text'          # text longtext password url number boolean choice
    required: bool = False
    secret: bool = False
    placeholder: str = ''
    default: object = None
    choices: tuple = ()

    @property
    def is_secret(self):
        return self.secret or self.field_type == 'password'

    def as_dict(self):
        return {
            'key': self.key,
            'label': self.label,
            'help_text': self.help_text,
            'field_type': 'password' if self.is_secret else self.field_type,
            'required': self.required,
            'secret': self.is_secret,
            'placeholder': self.placeholder,
            'default': self.default,
            'choices': list(self.choices),
        }


@dataclass
class CallResult:
    """What one operation did.

    ``demo`` is not decoration. It is the difference between "the invitation
    was sent" and "the invitation would have been sent", and it travels with
    the result all the way to the screen.
    """

    ok: bool = True
    demo: bool = False
    summary: str = ''
    data: dict = field(default_factory=dict)
    error: str = ''
    operation: str = ''
    provider: str = ''

    @property
    def status(self):
        if not self.ok:
            return 'failed'
        return 'demo' if self.demo else 'ok'

    def as_dict(self):
        return {
            'ok': self.ok, 'demo': self.demo, 'summary': self.summary,
            'data': self.data, 'error': self.error,
            'operation': self.operation, 'provider': self.provider,
        }


# ===========================================================================
# The connector base class
# ===========================================================================

class Connector:
    """Base class for every connected application."""

    key = ''
    name = ''
    description = ''
    category = 'internal'
    icon = 'fa-plug'
    color = '#4f46e5'
    docs_url = ''

    # Settings this connector needs, in the order the form shows them.
    config_fields = ()

    # Operations it advertises. Documentation for the Integrations page; the
    # dispatcher does not consult it, so an undeclared operation still runs.
    operations = ()

    def __init__(self, integration):
        self.integration = integration

    def __repr__(self):
        return f'<{type(self).__name__} {self.key}>'

    # -- settings ----------------------------------------------------------

    def setting(self, key, default=None):
        """One configured value, secret or not.

        Secrets are looked up first so that moving a field from config to
        secrets, or the reverse, never changes what a connector reads.
        """
        secrets = self.integration.secrets or {}
        if key in secrets and secrets[key] not in (None, ''):
            return secrets[key]
        config = self.integration.config or {}
        if key in config and config[key] not in (None, ''):
            return config[key]
        for spec in self.config_fields:
            if spec.key == key:
                return spec.default if spec.default is not None else default
        return default

    def has(self, key):
        return self.setting(key) not in (None, '', [], {})

    def required_fields(self):
        return [spec for spec in self.config_fields if spec.required]

    def is_configured(self):
        """Whether a live call could be attempted at all."""
        return all(self.has(spec.key) for spec in self.required_fields())

    def missing_settings(self):
        """Labels of the required settings still empty."""
        return [spec.label for spec in self.required_fields() if not self.has(spec.key)]

    # -- results -----------------------------------------------------------

    def ok(self, summary, data=None):
        return CallResult(ok=True, demo=False, summary=summary, data=data or {},
                          provider=self.key)

    def simulated(self, summary, data=None):
        return CallResult(ok=True, demo=True, summary=summary, data=data or {},
                          provider=self.key)

    def failure(self, error, data=None):
        return CallResult(ok=False, demo=False, summary='', error=str(error),
                          data=data or {}, provider=self.key)

    # -- dispatch ----------------------------------------------------------

    def call(self, operation, **kwargs):
        """Run one operation, live or simulated, and say which.

        The decision order matters. A disabled integration is an error, not a
        simulation: switching something off should stop it, not quietly
        replace it with a pretend version.
        """
        integration = self.integration

        if not integration.is_enabled:
            return self._finish(CallResult(
                ok=False, error=f'{self.name} is switched off in Integrations.',
                provider=self.key), operation)

        # Running the test suite forces the simulated path for every
        # connector, whatever the integration's own mode says. See the TESTING
        # flag in marketpulse/settings.py for why this is a hard override and
        # not a default: these connectors bypass Django's test email backend
        # entirely, so without it a test could send real mail to an address a
        # fixture invented.
        if _testing():
            result = self._run_demo(operation, kwargs)
            if result.ok:
                result.summary = (f'{result.summary} (forced to simulate: the test '
                                  f'suite is running)')
            return self._finish(result, operation)

        mode = integration.mode
        if mode == 'demo':
            return self._finish(self._run_demo(operation, kwargs), operation)

        if not self.is_configured():
            if mode == 'live':
                missing = ', '.join(self.missing_settings()) or 'credentials'
                return self._finish(CallResult(
                    ok=False, provider=self.key,
                    error=(f'{self.name} is set to live only and is missing: {missing}. '
                           'Add it on the Integrations page, or set the mode to '
                           'automatic to simulate instead.')), operation)
            return self._finish(self._run_demo(operation, kwargs), operation)

        handler = getattr(self, f'live_{operation}', None)
        if handler is None:
            # Configured, but this particular operation has no live
            # implementation. Simulating is the honest outcome, and the flag
            # says so.
            return self._finish(self._run_demo(operation, kwargs), operation)

        try:
            result = handler(**kwargs)
        except Exception as exc:  # noqa: BLE001 -- one bad call must not stop the platform
            result = CallResult(
                ok=False, provider=self.key,
                error=f'{self.name} call failed: {_describe(exc)}')
        return self._finish(result, operation)

    def _run_demo(self, operation, kwargs):
        handler = getattr(self, f'demo_{operation}', None)
        if handler is None:
            return self.generic_demo(operation, kwargs)
        try:
            return handler(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return CallResult(ok=False, provider=self.key,
                              error=f'Simulation failed: {_describe(exc)}')

    def generic_demo(self, operation, kwargs):
        """The fallback simulation: honest, informative, never a crash."""
        readable = operation.replace('_', ' ')
        detail = ', '.join(f'{k}={_short(v)}' for k, v in list(kwargs.items())[:4])
        return self.simulated(
            f'{self.name}: {readable} completed in demo mode.',
            {'operation': operation, 'arguments': detail, 'simulated': True})

    def _finish(self, result, operation):
        """Stamp the result and record the usage."""
        from .. import audit

        if not isinstance(result, CallResult):
            # A connector that returned a dict or a string still works.
            result = CallResult(ok=True, summary=str(result), provider=self.key)
        result.operation = operation
        result.provider = self.key
        if result.ok:
            audit.touch_integration(self.integration, demo=result.demo)
        return result

    # -- connection check --------------------------------------------------

    def probe(self):
        """Whether this connection currently works.

        A connector with a real check overrides ``live_probe``. Without one the
        verdict comes from the configuration, which is all there is to go on.
        """
        integration = self.integration

        if not integration.is_enabled:
            return ('disabled', f'{self.name} is switched off.')

        if integration.mode == 'demo':
            return ('demo', f'{self.name} is in demo mode. '
                            'Every action is simulated and clearly labelled.')

        if _testing():
            return ('demo', f'{self.name} is simulated while the test suite runs.')

        if not self.is_configured():
            missing = ', '.join(self.missing_settings())
            if integration.mode == 'live':
                return ('failed', f'Live only, but missing: {missing}.')
            return ('demo', f'Not configured yet ({missing} missing), so actions are '
                            'simulated. Add the settings to go live.')

        checker = getattr(self, 'live_probe', None)
        if checker is None:
            return ('connected', f'{self.name} is configured. '
                                 f'{len(self.operations)} operations available.')
        try:
            result = checker()
        except Exception as exc:  # noqa: BLE001
            return ('failed', f'{self.name} check failed: {_describe(exc)}')
        if isinstance(result, CallResult):
            return ('connected', result.summary) if result.ok else ('failed', result.error)
        return ('connected', str(result))

    # -- HTTP helper -------------------------------------------------------

    def request_json(self, url, *, method='GET', headers=None, payload=None,
                     params=None, timeout=DEFAULT_TIMEOUT, form=False):
        """A small JSON-over-HTTP helper, on the standard library only.

        The project has no third-party HTTP dependency by design, so every
        live connector shares this rather than each rolling its own.
        """
        if params:
            url = f'{url}?{urllib.parse.urlencode(params)}'

        body = None
        head = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
        head.update(headers or {})

        if payload is not None:
            if form:
                body = urllib.parse.urlencode(payload).encode()
                head['Content-Type'] = 'application/x-www-form-urlencoded'
            else:
                body = json.dumps(payload).encode()
                head['Content-Type'] = 'application/json'

        request = urllib.request.Request(url, data=body, headers=head, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8', errors='replace')
            status = response.status
        try:
            return status, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return status, {'raw': raw[:4000]}


def _testing():
    """Whether the test suite is running, so every connector must simulate.

    Read lazily rather than captured at import time: this module is imported
    while the app registry is loading, and reading a setting then is a way to
    get a half-configured answer.
    """
    from django.conf import settings
    return bool(getattr(settings, 'TESTING', False))


def _describe(exc):
    """A short, readable account of a failure, without a stack trace."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode('utf-8', errors='replace')[:300]
        except Exception:  # noqa: BLE001
            detail = ''
        return f'HTTP {exc.code} {exc.reason}. {detail}'.strip()
    if isinstance(exc, urllib.error.URLError):
        return f'could not reach the service ({exc.reason})'
    if isinstance(exc, TimeoutError):
        return 'the service did not respond in time'
    message = str(exc).strip()
    return message or type(exc).__name__


def _short(value, limit=60):
    text = str(value).replace('\n', ' ')
    return text if len(text) <= limit else text[:limit - 3] + '...'
