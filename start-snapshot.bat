@echo off
rem Run the agent on CSV exports of your real ServiceNow records (snapshot\ folder) instead of the live API.
set SERVICENOW_MODE=snapshot
call "%~dp0start.bat"
