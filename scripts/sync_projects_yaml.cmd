@echo off
cd /d "c:\AI\projects\claude-code-telegram" || exit /b 1
set APPROVED_DIRECTORY=c:\AI\projects
"C:\Program Files\Python314\python.exe" scripts\sync_projects_yaml.py
if %ERRORLEVEL% EQU 2 (
    echo projects.yaml changed, restarting bot...
    schtasks /end /tn "\claude-code-telegram"
    timeout /t 3 /nobreak >nul
    schtasks /run /tn "\claude-code-telegram"
)
