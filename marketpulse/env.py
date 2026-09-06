"""A minimal .env loader, using only the standard library.

WHY THIS EXISTS
---------------
Credentials belong in the environment rather than in settings.py or the
database. But a variable set with `$env:X = "y"` in PowerShell lives only as
long as that terminal window, so the next `manage.py runserver` starts with
nothing and the application quietly falls back to its offline behaviour --
mail printed to the console, language models answering from templates.

A .env file fixes that: it is read on start-up, every time, however the server
is launched.

WHY NOT python-dotenv
---------------------
The virtual environment holds Django and nothing else, and this is thirty
lines. Adding a dependency to read `KEY=value` would not be a good trade.

THE RULES IT FOLLOWS
--------------------
    KEY=value                 assigned
    KEY="value with spaces"   quotes stripped
    KEY='value'               quotes stripped
    # comment                 ignored
    (blank line)              ignored
    export KEY=value          the `export` prefix is tolerated

A REAL environment variable always wins. That ordering matters: it means the
file is a convenient default, not an override, so CI or a container can still
inject its own values without editing anything.

The file itself is listed in .gitignore, so the secrets in it are never
committed.
"""

import os


def load(path):
    """Read `path` into os.environ, without overwriting anything already set.

    Missing or unreadable files are ignored on purpose: the application must
    still start on a machine that has no .env, using whatever the real
    environment provides.

    Returns the number of variables loaded.
    """
    try:
        with open(path, encoding='utf-8') as handle:
            lines = handle.readlines()
    except OSError:
        return 0

    loaded = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        if line.startswith('export '):
            line = line[len('export '):].lstrip()

        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()

        # Strip one matching pair of surrounding quotes, so a value containing
        # spaces can be written either way.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1

    return loaded
