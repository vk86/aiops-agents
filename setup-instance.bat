@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
echo Sets up a new ServiceNow PDI for the demo: service user, .env, demo data.
echo.
".venv\Scripts\python.exe" scripts\setup_instance.py --instance dev282967
echo.
pause
