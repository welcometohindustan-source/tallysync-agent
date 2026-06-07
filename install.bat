@echo off
:: TallySync Mobile Agent — Windows Installer
:: Run this as Administrator
:: ============================================================

title TallySync Agent Installer
color 0A
echo.
echo  ============================================
echo   TallySync Mobile — Sync Agent Installer
echo  ============================================
echo.

:: Check Admin
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo  ERROR: Please right-click this file and choose
  echo         "Run as Administrator"
  echo.
  pause
  exit /b 1
)

set "AGENT_DIR=%~dp0"
set "EXE=%AGENT_DIR%TallySyncAgent.exe"
set "CFG=%AGENT_DIR%config.ini"
set "TASK_NAME=TallySyncAgent"

:: Check exe exists
if not exist "%EXE%" (
  echo  ERROR: TallySyncAgent.exe not found in:
  echo         %AGENT_DIR%
  echo.
  echo  Make sure TallySyncAgent.exe and config.ini are in the same folder.
  pause
  exit /b 1
)

:: Check config has user_id filled
findstr /C:"user_id     =" "%CFG%" | findstr /V "user_id     =           " >nul
if %errorlevel% neq 0 (
  echo  WARNING: config.ini may not be configured yet.
  echo  Please open config.ini and fill in:
  echo    - user_id
  echo    - api_key
  echo    - secret_key
  echo    - server_url
  echo.
  echo  You can fill these after installing and restart the task.
  echo.
)

:: Read interval from config.ini
for /f "tokens=1,* delims==" %%A in ('findstr /i "interval_min" "%CFG%"') do (
  set "INTERVAL_RAW=%%B"
)
:: Default to 5 if not found
set "INTERVAL=5"
if defined INTERVAL_RAW (
  for /f "tokens=1" %%N in ("%INTERVAL_RAW%") do set "INTERVAL=%%N"
)

echo  Installing agent to run every %INTERVAL% minutes...
echo  Agent path: %EXE%
echo.

:: Remove existing task if present
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
  echo  Removing existing scheduled task...
  schtasks /delete /tn "%TASK_NAME%" /f >nul
)

:: Create scheduled task — runs at login + every N minutes
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"%EXE%\" --once" ^
  /sc MINUTE ^
  /mo %INTERVAL% ^
  /ru SYSTEM ^
  /rl HIGHEST ^
  /f ^
  /delay 0002:00 >nul

if %errorlevel% equ 0 (
  echo  [OK] Scheduled task created: %TASK_NAME%
  echo       Runs every %INTERVAL% minutes as SYSTEM.
) else (
  echo  [WARN] Could not create scheduled task via schtasks.
  echo         You can run the agent manually or add it to startup.
)

:: Also run once immediately to verify connection
echo.
echo  Running a test sync now...
"%EXE%" --once
echo.

echo  ============================================
echo   Installation complete!
echo  ============================================
echo.
echo  The agent will now sync Tally automatically every %INTERVAL% minutes.
echo  Check tallysync_agent.log in this folder for sync status.
echo.
echo  To check Task Scheduler: Start > Task Scheduler > TallySyncAgent
echo  To uninstall: run uninstall.bat as Administrator
echo.
pause
