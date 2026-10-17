@echo off
rem Repository root is the parent of this script's directory.
rem APPROVED_DIRECTORY: taken from the environment if set, otherwise
rem sync_projects_yaml.py reads it from the repository's .env.
cd /d "%~dp0.." || exit /b 1
rem The script imports the bot's own modules (structlog, yaml), which only the
rem project venv has: a system "py" failed on import every run, and the exit
rem code was not 2, so nothing was synced and nobody saw it.
set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo ERROR: %CD%\%PYTHON% not found - create the project venv first 1>&2
    exit /b 1
)
"%PYTHON%" scripts\sync_projects_yaml.py
if %ERRORLEVEL% EQU 2 goto restart
rem 0 = no changes; anything else is a failure Task Scheduler should show.
exit /b %ERRORLEVEL%

:restart
echo projects.yaml changed, restarting bot...
schtasks /end /tn "\claude-code-telegram"
rem No stdin under Task Scheduler: timeout fails there, ping does not.
ping -n 4 127.0.0.1 >nul
schtasks /run /tn "\claude-code-telegram"
