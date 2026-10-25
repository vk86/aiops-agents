@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title ServiceNow Incident Agent
set LOG=%~dp0setup.log
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo ==== %date% %time% ==== > "%LOG%"

echo.
echo  ServiceNow Incident Agent - setup and start
echo  -------------------------------------------

rem ---- 1. find Python 3.10+ ---------------------------------------------
set PY=
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined PY ( py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V" )
)
if not defined PY (
  python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  for %%D in ("%LOCALAPPDATA%\Programs\Python\Python313" "%LOCALAPPDATA%\Programs\Python\Python312" "%LOCALAPPDATA%\Programs\Python\Python311") do (
    if not defined PY if exist "%%~D\python.exe" set "PY="%%~D\python.exe""
  )
)
if not defined PY (
  echo [1/5] Python not found. Installing Python 3.12 with winget...
  echo [1/5] installing python via winget >> "%LOG%"
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
)
if not defined PY (
  echo.
  echo  Could not find or install Python. Install Python 3.12 from https://www.python.org/downloads/
  echo  ^(tick "Add python.exe to PATH"^), then run start.bat again.
  echo FAILED: no python >> "%LOG%"
  pause & exit /b 1
)
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do set PYVER=%%v
echo [1/5] Using %PYVER%  ^(%PY%^)
echo python: %PY% %PYVER% >> "%LOG%"

rem ---- 2. virtual environment ---------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo [2/5] Creating virtual environment .venv ...
  %PY% -m venv .venv >> "%LOG%" 2>&1 || ( echo FAILED: venv >> "%LOG%" & echo  Could not create .venv - see setup.log & pause & exit /b 1 )
) else (
  echo [2/5] Virtual environment already exists
)
set VPY=%~dp0.venv\Scripts\python.exe

rem ---- 3. packages ----------------------------------------------------------
echo [3/5] Installing packages ^(first run takes a few minutes^)...
"%VPY%" -m pip install --upgrade pip >> "%LOG%" 2>&1
"%VPY%" -m pip install -r requirements.txt >> "%LOG%" 2>&1 || ( echo FAILED: pip install >> "%LOG%" & echo  Package install failed - see setup.log & pause & exit /b 1 )
"%VPY%" -m pip install -e . >> "%LOG%" 2>&1
echo PACKAGES_OK >> "%LOG%"

rem ---- 3b. corporate Application Control can block brand-new DLLs: pin older, widely used builds
echo       Checking compiled libraries against Application Control...
"%VPY%" scripts\probe_native.py > probe.txt 2>&1
if errorlevel 1 (
  echo       Some libraries are blocked; installing older builds...
  findstr /C:"BLOCKED orjson" probe.txt >nul && "%VPY%" -m pip install "orjson==3.11.5" >> "%LOG%" 2>&1
  findstr /C:"BLOCKED ormsgpack" probe.txt >nul && "%VPY%" -m pip install "ormsgpack==1.12.0" >> "%LOG%" 2>&1
  findstr /C:"BLOCKED xxhash" probe.txt >nul && "%VPY%" -m pip install "xxhash==3.5.0" >> "%LOG%" 2>&1
  findstr /C:"BLOCKED zstandard" probe.txt >nul && "%VPY%" -m pip install "zstandard==0.23.0" >> "%LOG%" 2>&1
  findstr /C:"BLOCKED uuid_utils" probe.txt >nul && "%VPY%" -m pip install "uuid-utils==0.12.0" >> "%LOG%" 2>&1
  findstr /C:"BLOCKED regex" probe.txt >nul && "%VPY%" -m pip install "regex==2024.11.6" >> "%LOG%" 2>&1
  findstr /C:"BLOCKED jiter" probe.txt >nul && "%VPY%" -m pip install "jiter==0.8.2" >> "%LOG%" 2>&1
  findstr /C:"BLOCKED tiktoken" probe.txt >nul && "%VPY%" -m pip install "tiktoken==0.8.0" >> "%LOG%" 2>&1
  findstr /C:"BLOCKED websockets" probe.txt >nul && "%VPY%" -m pip install "websockets==14.2" >> "%LOG%" 2>&1
  findstr /C:"BLOCKED httptools" probe.txt >nul && "%VPY%" -m pip uninstall -y httptools >> "%LOG%" 2>&1
  findstr /C:"BLOCKED watchfiles" probe.txt >nul && "%VPY%" -m pip uninstall -y watchfiles >> "%LOG%" 2>&1
  copy /y probe.txt probe_before.txt >nul
  "%VPY%" scripts\probe_native.py > probe.txt 2>&1
  if errorlevel 1 ( echo       Still blocked - see probe.txt ) else ( echo       Fixed. )
)
type probe.txt >> "%LOG%"

rem ---- 4. config + ServiceNow check ----------------------------------------
if not exist ".env" copy ".env.example" ".env" >nul
echo [4/5] Checking ServiceNow connection...
echo ---- check_servicenow ---- >> "%LOG%"
"%VPY%" scripts\check_servicenow.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo       ServiceNow check failed ^(see setup.log^). Starting the UI anyway.
) else (
  echo       ServiceNow OK
)

rem ---- 4b. stop an old server still holding port 8001 (it would keep the old .env settings)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":8001 .*LISTENING"') do (
  echo       Stopping old server ^(PID %%p^) on port 8001
  taskkill /PID %%p /F >nul 2>&1
)
timeout /t 1 >nul

rem ---- 5. start the web UI -----------------------------------------------------
echo [5/5] Starting web UI at http://localhost:8001
echo SERVER_STARTING >> "%LOG%"
start "" cmd /c "timeout /t 6 >nul & start http://localhost:8001"
echo.
echo  Running. Keep this window open; close it (or press Ctrl+C) to stop the server.
echo  Server log: server.log
echo.
"%VPY%" -m uvicorn incident_agent.api:app --app-dir src --port 8001 > server.log 2>&1
echo Server stopped. See server.log
pause
