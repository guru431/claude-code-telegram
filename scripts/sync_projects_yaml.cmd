@echo off
rem Repository root is the parent of this script's directory.
rem APPROVED_DIRECTORY: taken from the environment if set, otherwise
rem sync_projects_yaml.py reads it from the repository's .env.
cd /d "%~dp0.." || exit /b 1
py scripts\sync_projects_yaml.py
if %ERRORLEVEL% EQU 2 (
    echo projects.yaml changed, restarting bot...
    schtasks /end /tn "\claude-code-telegram"
    rem No stdin under Task Scheduler: timeout fails there, ping does not.
    ping -n 4 127.0.0.1 >nul
    schtasks /run /tn "\claude-code-telegram"
)
