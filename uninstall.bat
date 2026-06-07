@echo off
title TallySync Agent Uninstaller
net session >nul 2>&1
if %errorlevel% neq 0 (echo Run as Administrator & pause & exit /b 1)
schtasks /delete /tn "TallySyncAgent" /f
echo Agent uninstalled. Your synced data on the server is not deleted.
pause
