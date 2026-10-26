@echo off
rem Run against your real ServiceNow instance (overrides SERVICENOW_MODE in .env; URL, user and password still come from .env).
set SERVICENOW_MODE=live
call "%~dp0start.bat"
