#!/usr/bin/env bash
# ===========================================================================
#  AI Workforce -- one command to start the application (macOS / Linux).
#
#  Builds the virtual environment if it is missing, installs dependencies,
#  applies migrations, provisions the workforce, and starts the server.
#
#  USAGE
#      ./run.sh            start on http://127.0.0.1:8000/
#      ./run.sh 8123       start on a different port
# ===========================================================================
set -euo pipefail

# Always run from the directory this script lives in.
cd "$(dirname "$0")"

PORT="${1:-8000}"
VENV_PY="venv/bin/python"

# --- 1. Build the virtual environment if this is a fresh copy -------------
if [ ! -x "$VENV_PY" ]; then
    echo "No virtual environment found. Creating one..."
    # Prefer python3; fall back to python.
    PY="$(command -v python3 || command -v python || true)"
    if [ -z "$PY" ]; then
        echo "ERROR: no Python interpreter found. Install Python 3.11+ first."
        exit 1
    fi
    "$PY" -m venv venv
    echo "Installing Django..."
    "$VENV_PY" -m pip install --upgrade pip --quiet
    "$VENV_PY" -m pip install -r requirements.txt
fi

# --- 2. Apply any migrations this database has not seen -------------------
"$VENV_PY" manage.py migrate --noinput

# --- 3. Provision the workforce (idempotent, runs on every start) ---------
"$VENV_PY" manage.py seed_workforce || {
    echo "WARNING: provisioning did not fully complete. The site will still start."
}

# --- 4. Go ----------------------------------------------------------------
echo ""
echo "  Sign in as  admin / admin123     http://127.0.0.1:${PORT}/"
echo "  Then open the Dashboard: it tells you what is set up and what is not."
echo ""
exec "$VENV_PY" manage.py runserver "$PORT"
