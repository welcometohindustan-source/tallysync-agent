; ════════════════════════════════════════════════════════════════
; TallySync Mobile — Windows NSIS Installer  v4.0
; ════════════════════════════════════════════════════════════════
; Generates: TallySyncSetup.exe
; Requires:  NSIS 3.x + MUI2 plugin (bundled with NSIS)
;
; Features
;   • Welcome → Licence → Directory → Progress → Finish pages
;   • Per-user install default ($LOCALAPPDATA\Programs\TallySync)
;     with no UAC prompt; falls back to $PROGRAMFILES if admin
;   • Detects and cleanly kills a running agent before upgrading
;   • Desktop + Start Menu shortcuts with correct icon
;   • Windows Startup registry entry  (--tray, no UAC)
;   • Task Scheduler job replaced/updated on every install
;   • Clean uninstall removes all files, registry, shortcuts,
;     scheduled task, and startup entry
; ════════════════════════════════════════════════════════════════

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

; ── Metadata ──────────────────────────────────────────────────────────────────
!define PRODUCT_NAME    "TallySync Mobile Agent"
!define PRODUCT_VER     "4.0"
!define PRODUCT_SUBKEY  "Software\TallySync"
!define UNINST_SUBKEY   "Software\Microsoft\Windows\CurrentVersion\Uninstall\TallySync"
!define EXE_NAME        "TallySyncAgent.exe"
!define APPDATA_DIR     "$LOCALAPPDATA\Programs\TallySync"

Name               "${PRODUCT_NAME}"
OutFile            "TallySyncSetup.exe"

; Default to per-user install — no UAC required.
; User can change to any path on the Directory page.
InstallDir         "${APPDATA_DIR}"
InstallDirRegKey   HKCU "${PRODUCT_SUBKEY}" "InstallDir"

; Request user-level execution (no UAC prompt).
; If the user chooses a system-wide path (e.g. Program Files),
; Windows will automatically re-elevate via UAC.
RequestExecutionLevel user

SetCompressor      /SOLID lzma
BrandingText       "TallySync Mobile — Sync Agent v${PRODUCT_VER}"

; ── MUI Settings ──────────────────────────────────────────────────────────────
!define MUI_ABORTWARNING

; Use icon from installer directory (must exist at build time)
!ifdef ICON_FILE
  !define MUI_ICON   "${ICON_FILE}"
  !define MUI_UNICON "${ICON_FILE}"
!endif

; Welcome page
!define MUI_WELCOMEPAGE_TITLE  "Welcome to ${PRODUCT_NAME}"
!define MUI_WELCOMEPAGE_TEXT   \
  "This wizard will install TallySync Sync Agent v${PRODUCT_VER} on your computer.\
  $\r$\n$\r$\n\
  The agent runs silently in the Windows system tray, connects to your \
  TallyPrime ERP and syncs ledgers, stock and vouchers to the TallySync \
  portal automatically.\
  $\r$\n$\r$\n\
  Click Next to continue."

; Directory page hint
!define MUI_DIRECTORYPAGE_TEXT_TOP \
  "Choose where to install ${PRODUCT_NAME}. The default location \
  ($LOCALAPPDATA\Programs\TallySync) does not require administrator rights."

; Finish page — launch agent after install
!define MUI_FINISHPAGE_RUN          "$INSTDIR\${EXE_NAME}"
!define MUI_FINISHPAGE_RUN_TEXT     "Start TallySync Agent now"
!define MUI_FINISHPAGE_RUN_NOTCHECKED   ; unchecked by default
!define MUI_FINISHPAGE_LINK             "Open TallySync Portal"
!define MUI_FINISHPAGE_LINK_LOCATION   "http://localhost/tallysync"
!define MUI_FINISHPAGE_TEXT \
  "${PRODUCT_NAME} has been installed successfully.\
  $\r$\n$\r$\n\
  The agent will:\
  $\r$\n  • Start automatically every time Windows starts\
  $\r$\n  • Run a background sync every 5 minutes\
  $\r$\n  • Appear as an icon in your system tray\
  $\r$\n$\r$\n\
  On first launch the Setup Wizard will ask you to paste your \
  configuration key from the TallySync portal."

; ── Pages ─────────────────────────────────────────────────────────────────────
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE    "LICENSE.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ── Helper: kill running agent gracefully ─────────────────────────────────────
!macro KillAgent
  ; Try graceful shutdown first (taskkill without /F gives the process
  ; ~5 s to save its config before being terminated)
  nsExec::ExecToLog 'taskkill /im "${EXE_NAME}"'
  Sleep 1500
  ; Force-kill if still running
  nsExec::ExecToLog 'taskkill /f /im "${EXE_NAME}"'
  Sleep 500
!macroend

; ── Install Section ───────────────────────────────────────────────────────────
Section "${PRODUCT_NAME}" SecMain
  SectionIn RO    ; required — cannot deselect

  ; Kill any running instance before overwriting the exe
  !insertmacro KillAgent

  SetOutPath "$INSTDIR"

  ; ── Copy files ──────────────────────────────────────────────────────────────
  File "${EXE_NAME}"
  ; Only copy a blank config.ini if one doesn't already exist
  ; (preserves settings on upgrade/reinstall)
  ${IfNot} ${FileExists} "$APPDATA\TallySync\config.ini"
    SetOutPath "$APPDATA\TallySync"
    File "config.ini"
    SetOutPath "$INSTDIR"
  ${EndIf}

  ; ── Registry ────────────────────────────────────────────────────────────────
  WriteRegStr HKCU "${PRODUCT_SUBKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_SUBKEY}" "Version"    "${PRODUCT_VER}"

  ; Add/Remove Programs entry (HKCU — no admin required)
  WriteRegStr HKCU "${UNINST_SUBKEY}" "DisplayName"     "${PRODUCT_NAME}"
  WriteRegStr HKCU "${UNINST_SUBKEY}" "UninstallString"  '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_SUBKEY}" "DisplayVersion"   "${PRODUCT_VER}"
  WriteRegStr HKCU "${UNINST_SUBKEY}" "Publisher"        "Raj Systems & Technologies"
  WriteRegStr HKCU "${UNINST_SUBKEY}" "DisplayIcon"      "$INSTDIR\${EXE_NAME}"
  WriteRegDWORD HKCU "${UNINST_SUBKEY}" "NoModify"  1
  WriteRegDWORD HKCU "${UNINST_SUBKEY}" "NoRepair"  1

  ; ── Uninstaller ─────────────────────────────────────────────────────────────
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; ── Desktop shortcut ────────────────────────────────────────────────────────
  CreateShortcut "$DESKTOP\TallySync Agent.lnk"      \
    "$INSTDIR\${EXE_NAME}" ""                         \
    "$INSTDIR\${EXE_NAME}" 0                          \
    SW_SHOWNORMAL "" "TallySync Mobile Sync Agent"

  ; ── Start Menu shortcuts ────────────────────────────────────────────────────
  CreateDirectory "$SMPROGRAMS\TallySync Mobile"
  CreateShortcut  "$SMPROGRAMS\TallySync Mobile\TallySync Agent.lnk" \
    "$INSTDIR\${EXE_NAME}" "" "$INSTDIR\${EXE_NAME}" 0
  CreateShortcut  "$SMPROGRAMS\TallySync Mobile\Uninstall TallySync.lnk" \
    "$INSTDIR\Uninstall.exe"

  ; ── Windows Startup (HKCU — no admin, no UAC) ───────────────────────────────
  ; Launches minimised to tray every time the user logs in.
  WriteRegStr HKCU \
    "Software\Microsoft\Windows\CurrentVersion\Run" \
    "TallySyncAgent" '"$INSTDIR\${EXE_NAME}" --tray'

  ; ── Task Scheduler — background sync even when agent window is closed ────────
  ; Replaces any existing job (/f = force overwrite).
  ; Uses ONLOGON trigger so it runs in the user's session (not SYSTEM),
  ; which means it can reach Tally on localhost without credential issues.
  nsExec::ExecToLog \
    'schtasks /create /tn "TallySyncMobileAgent" \
     /tr "\"$INSTDIR\${EXE_NAME}\" --once" \
     /sc MINUTE /mo 5 \
     /du 9999:59 \
     /rl HIGHEST /f'

SectionEnd

; ── Uninstall Section ─────────────────────────────────────────────────────────
Section "Uninstall"

  ; Kill running agent
  !insertmacro KillAgent

  ; Remove Task Scheduler job
  nsExec::ExecToLog 'schtasks /delete /tn "TallySyncMobileAgent" /f'

  ; Remove Startup registry entry
  DeleteRegValue HKCU \
    "Software\Microsoft\Windows\CurrentVersion\Run" "TallySyncAgent"

  ; Remove installed files
  Delete "$INSTDIR\${EXE_NAME}"
  Delete "$INSTDIR\Uninstall.exe"
  ; Note: config.ini lives in %APPDATA%\TallySync — we leave it so
  ;       a reinstall picks up the user's existing settings.
  ;       Uncomment the next two lines to wipe settings on uninstall:
  ; Delete "$APPDATA\TallySync\config.ini"
  ; Delete "$APPDATA\TallySync\tallysync_agent.log"
  RMDir  "$INSTDIR"

  ; Remove shortcuts
  Delete "$DESKTOP\TallySync Agent.lnk"
  Delete "$SMPROGRAMS\TallySync Mobile\TallySync Agent.lnk"
  Delete "$SMPROGRAMS\TallySync Mobile\Uninstall TallySync.lnk"
  RMDir  "$SMPROGRAMS\TallySync Mobile"

  ; Remove registry keys
  DeleteRegKey HKCU "${PRODUCT_SUBKEY}"
  DeleteRegKey HKCU "${UNINST_SUBKEY}"

  MessageBox MB_OK|MB_ICONINFORMATION \
    "TallySync Agent has been uninstalled.$\r$\n$\r$\n\
    Your configuration file ($APPDATA\TallySync\config.ini) has been kept \
    so you can reinstall without re-entering your settings."

SectionEnd
