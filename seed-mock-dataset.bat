@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
echo Loads the full mock demo dataset (60 incidents, changes, CIs, problem) into your ServiceNow instance.
echo Records are tagged demo-mock. Remove them later with: seed-mock-dataset.bat --cleanup
".venv\Scripts\python.exe" scripts\seed_mock_dataset.py %*
pause
