; TallySync Mobile — Windows Installer
; Built with NSIS (Nullsoft Scriptable Install System)
; Generates: TallySyncSetup.exe

!include "MUI2.nsh"
!include "FileFunc.nsh"

; ── Installer metadata ────────────────────────────────────────────────────────
Name               "TallySync Mobile Agent"
OutFile            "TallySyncSetup.exe"
InstallDir         "$PROGRAMFILES\TallySync"
InstallDirRegKey   HKLM "Software\TallySync" "InstallDir"
RequestExecutionLevel admin
SetCompressor      /SOLID lzma
BrandingText       "TallySync Mobile — Sync Agent v4.0"

; ── UI Pages ─────────────────────────────────────────────────────────────────
!define MUI_ABORTWARNING
!define MUI_ICON   "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_WELCOMEPAGE_TITLE  "Welcome to TallySync Mobile"
!define MUI_WELCOMEPAGE_TEXT   "This will install TallySync Sync Agent on your computer.$\r$\n$\r$\nThe agent connects to your Tally ERP and automatically syncs data to the TallySync portal every 5 minutes.$\r$\n$\r$\nClick Next to continue."
!define MUI_FINISHPAGE_RUN     "$INSTDIR\TallySyncAgent.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start TallySync Agent now"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_LINK    "Open TallySync Portal"
!define MUI_FINISHPAGE_LINK_LOCATION "http://localhost/tallysync"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE    "LICENSE.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ── Install ───────────────────────────────────────────────────────────────────
Section "TallySync Agent" SecMain
  SectionIn RO  ; Required — cannot deselect

  SetOutPath "$INSTDIR"

  ; Copy files
  File "TallySyncAgent.exe"
  File "config.ini"

  ; Write registry for uninstaller
  WriteRegStr HKLM "Software\TallySync" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\TallySync" "Version"    "4.0"

  ; Create uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Add/Remove Programs entry
  WriteRegStr HKLM \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\TallySync" \
    "DisplayName" "TallySync Mobile Agent"
  WriteRegStr HKLM \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\TallySync" \
    "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKLM \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\TallySync" \
    "DisplayVersion" "4.0"
  WriteRegStr HKLM \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\TallySync" \
    "Publisher" "TallySync Mobile"
  WriteRegDWORD HKLM \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\TallySync" \
    "NoModify" 1
  WriteRegDWORD HKLM \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\TallySync" \
    "NoRepair" 1

  ; Desktop shortcut
  CreateShortcut "$DESKTOP\TallySync Agent.lnk" \
    "$INSTDIR\TallySyncAgent.exe" "" \
    "$INSTDIR\TallySyncAgent.exe" 0 \
    SW_SHOWNORMAL "" "TallySync Mobile Sync Agent"

  ; Start Menu shortcut
  CreateDirectory "$SMPROGRAMS\TallySync Mobile"
  CreateShortcut "$SMPROGRAMS\TallySync Mobile\TallySync Agent.lnk" \
    "$INSTDIR\TallySyncAgent.exe"
  CreateShortcut "$SMPROGRAMS\TallySync Mobile\Uninstall.lnk" \
    "$INSTDIR\Uninstall.exe"

  ; Windows Startup — run agent at Windows login (hidden/tray)
  WriteRegStr HKCU \
    "Software\Microsoft\Windows\CurrentVersion\Run" \
    "TallySyncAgent" '"$INSTDIR\TallySyncAgent.exe" --tray'

  ; Windows Task Scheduler — sync every 5 minutes even when not logged in
  ExecWait 'schtasks /create /tn "TallySyncAgent" /tr "\"$INSTDIR\TallySyncAgent.exe\" --once" /sc MINUTE /mo 5 /ru SYSTEM /rl HIGHEST /f'

  ; Show completion message
  MessageBox MB_OK|MB_ICONINFORMATION \
    "TallySync Agent installed successfully!$\r$\n$\r$\nThe agent will:$\r$\n• Start automatically with Windows$\r$\n• Sync Tally data every 5 minutes$\r$\n• Appear in your system tray$\r$\n$\r$\nOn first launch, you will be asked to paste your configuration from the TallySync portal."

SectionEnd

; ── Uninstall ─────────────────────────────────────────────────────────────────
Section "Uninstall"
  ; Kill running agent
  ExecWait 'taskkill /f /im TallySyncAgent.exe'

  ; Remove Task Scheduler job
  ExecWait 'schtasks /delete /tn "TallySyncAgent" /f'

  ; Remove startup registry entry
  DeleteRegValue HKCU \
    "Software\Microsoft\Windows\CurrentVersion\Run" "TallySyncAgent"

  ; Remove files
  Delete "$INSTDIR\TallySyncAgent.exe"
  Delete "$INSTDIR\config.ini"
  Delete "$INSTDIR\tallysync_agent.log"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir  "$INSTDIR"

  ; Remove shortcuts
  Delete "$DESKTOP\TallySync Agent.lnk"
  Delete "$SMPROGRAMS\TallySync Mobile\TallySync Agent.lnk"
  Delete "$SMPROGRAMS\TallySync Mobile\Uninstall.lnk"
  RMDir  "$SMPROGRAMS\TallySync Mobile"

  ; Remove registry keys
  DeleteRegKey HKLM "Software\TallySync"
  DeleteRegKey HKLM \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\TallySync"

  MessageBox MB_OK "TallySync Agent has been uninstalled."
SectionEnd
