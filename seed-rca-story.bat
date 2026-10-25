@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
echo Creates a demo incident + root-cause story in your ServiceNow instance (real records, tagged [demo-rca]).
".venv\Scripts\python.exe" scripts\seed_rca_story.py
pause
