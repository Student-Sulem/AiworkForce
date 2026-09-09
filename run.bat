@echo off
REM ===========================================================================
REM  AI Workforce -- one command to start the application.
REM
REM  WHY THIS FILE EXISTS
REM  --------------------
REM  Typing `python manage.py runserver` fails with:
REM
REM      ImportError: Couldn't import Django. Are you sure it's installed...
REM
REM  because `python` is the system interpreter, and Django is installed inside
REM  this project's virtual environment, not system-wide. The environment has to
REM  be activated first, and it is easy to forget.
REM
REM  It is also missing entirely from a fresh download: `venv/` is listed in
REM  .gitignore -- as it should be, since it is 50MB of rebuildable files -- so
REM  anyone who clones or downloads this repository has the code but no Django.
REM
REM  This script handles both. It builds the environment if it is not there,
REM  applies any outstanding migrations, and starts the server.
REM
REM  WHY .bat AND NOT .ps1
REM  ---------------------
REM  PowerShell refuses to run downloaded .ps1 files under the default
REM  RemoteSigned policy, because a file extracted from a ZIP carries the
REM  mark-of-the-web. A batch file is not subject to execution policy, so this
REM  works on a marker's machine as well as on the author's.
REM
REM  USAGE
REM      run.bat              start the server on http://127.0.0.1:8000/
REM      run.bat 8123         start it on a different port
REM ===========================================================================

setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

set "VENV_PY=venv\Scripts\python.exe"

REM --- 1. Build the virtual environment, if this is a fresh copy -------------
if not exist "%VENV_PY%" (
    echo No virtual environment found. Creating one...
    echo.

    REM The py launcher is the reliable way to find Python on Windows; fall
    REM back to whatever `python` resolves to if it is not installed.
    where py >nul 2>&1
    if errorlevel 1 (
        python -m venv venv
    ) else (
        py -3 -m venv venv
    )

    if not exist "%VENV_PY%" (
        echo.
        echo ERROR: could not create the virtual environment.
        echo Install Python 3.11 or newer from https://www.python.org/downloads/
        echo and make sure you tick "Add python.exe to PATH".
        exit /b 1
    )

    echo Installing Django...
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: could not install the dependencies. Are you online?
        exit /b 1
    )
    echo.
)

REM --- 2. Apply any migrations this database has not seen -------------------
"%VENV_PY%" manage.py migrate --noinput
if errorlevel 1 (
    echo.
    echo ERROR: the migrations failed. The database may be from an older build.
    exit /b 1
)

REM --- 3. Provision the workforce -------------------------------------------
REM
REM  Idempotent, so it runs on every start. It creates the six AI employees,
REM  registers every connected application, generates the capability
REM  catalogue from the tool registry, seeds the company knowledge base and
REM  fills the operational pages with a demonstration company's work.
REM
REM  This is why there is nothing to configure before the application is
REM  usable. Everything an integration needs is a database row, entered on the
REM  Integrations page or by asking an AI employee to set it up.
"%VENV_PY%" manage.py seed_workforce
if errorlevel 1 (
    echo.
    echo WARNING: provisioning did not complete. The site will still start,
    echo          but some pages may be empty. Run this for a diagnosis:
    echo              venv\Scripts\python.exe manage.py workforce_status
    echo.
)

REM --- 4. Say plainly which features are switched off ------------------------
echo.
echo   Every connected application starts in DEMO MODE, which is not the same
echo   as broken: each one simulates its actions and labels every single one
echo   as simulated, so the whole approval workflow is demonstrable with
echo   nothing configured. Add a credential on the Integrations page, or ask
echo   an AI employee to set one up, to make any of them real.
echo.
if not exist ".env" (
    echo   No .env file, so no language model is connected yet. The employees
    echo   still run their tools and produce real records without one, but they
    echo   cannot write prose. Add a key on the Language models page -- free
    echo   options exist for OpenRouter, NVIDIA and local Ollama.
    echo.
)

REM --- 5. Go ----------------------------------------------------------------
echo.
echo   Sign in as  admin / admin123     http://127.0.0.1:%PORT%/
echo   Then open the Dashboard: it tells you what is set up and what is not.
echo.
"%VENV_PY%" manage.py runserver %PORT%

endlocal
