@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
echo Checking ServiceNow login...
".venv\Scripts\python.exe" scripts\diagnose_auth.py > diagnose.log 2>&1
type diagnose.log
echo.
echo Saved to diagnose.log. You can close this window.
pause
