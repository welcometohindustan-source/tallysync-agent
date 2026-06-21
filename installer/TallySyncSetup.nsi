; ════════════════════════════════════════════════════════════════
; BizView Pro — Windows NSIS Installer  v4.0
; ════════════════════════════════════════════════════════════════
; Generates: BizViewProSetup.exe
; Requires:  NSIS 3.x + MUI2 plugin
;
; Features
;   • Welcome → Directory → Progress → Finish pages (no separate License)
;   • Per-user install ($LOCALAPPDATA\Programs\BizViewPro) — no UAC
;   • Detects and cleanly kills running agent before upgrading
;   • Desktop + Start Menu shortcuts with BizView Pro icon
;   • Windows Startup registry entry (--tray, no UAC)
;   • Task Scheduler job replaced/updated on every install
;   • Clean uninstall removes all files, registry, shortcuts
; ════════════════════════════════════════════════════════════════

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

; ── Metadata ──────────────────────────────────────────────────────────────────
!define PRODUCT_NAME    "BizView Pro Sync Agent"
!define PRODUCT_VER     "4.0"
!define PRODUCT_SUBKEY  "Software\BizViewPro"
!define UNINST_SUBKEY   "Software\Microsoft\Windows\CurrentVersion\Uninstall\BizViewPro"
!define EXE_NAME        "BizViewProAgent.exe"
!define APPDATA_DIR     "$LOCALAPPDATA\Programs\BizViewPro"

Name               "${PRODUCT_NAME}"
OutFile            "BizViewProSetup.exe"

InstallDir         "${APPDATA_DIR}"
InstallDirRegKey   HKCU "${PRODUCT_SUBKEY}" "InstallDir"

RequestExecutionLevel user

SetCompressor      /SOLID lzma
BrandingText       "BizView Pro — Real-Time Business Intelligence v${PRODUCT_VER}"

; ── MUI Settings ──────────────────────────────────────────────────────────────
!define MUI_ABORTWARNING

; Use BizView Pro icon
!ifdef ICON_FILE
  !define MUI_ICON   "${ICON_FILE}"
  !define MUI_UNICON "${ICON_FILE}"
!endif

; Welcome page
!define MUI_WELCOMEPAGE_TITLE  "Welcome to ${PRODUCT_NAME}"
!define MUI_WELCOMEPAGE_TEXT   \
  "This wizard will install BizView Pro Sync Agent v${PRODUCT_VER} on your computer.\
  $\r$\n$\r$\n\
  The agent runs silently in the Windows system tray, connects to your \
  TallyPrime ERP and syncs ledgers, stock and vouchers to your BizView Pro \
  portal automatically.\
  $\r$\n$\r$\n\
  Click Next to continue."

; Directory page
!define MUI_DIRECTORYPAGE_TEXT_TOP \
  "Choose where to install ${PRODUCT_NAME}. The default location \
  does not require administrator rights."

; Finish page
!define MUI_FINISHPAGE_RUN          "$INSTDIR\${EXE_NAME}"
!define MUI_FINISHPAGE_RUN_TEXT     "Start BizView Pro Agent now"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!define MUI_FINISHPAGE_LINK         "Open BizView Pro Portal"
!define MUI_FINISHPAGE_LINK_LOCATION "https://bizviewpro.in/tallysync"
!define MUI_FINISHPAGE_TEXT \
  "${PRODUCT_NAME} has been installed successfully.\
  $\r$\n$\r$\n\
  Launch the agent from your desktop shortcut or Start Menu. \
  It will appear in the system tray (bottom-right notification area).\
  $\r$\n$\r$\n\
  Configure the sync agent by clicking Settings in the agent, \
  or copy your config.ini credentials from your BizView Pro portal.\
  $\r$\n$\r$\n\
  Visit bizviewpro.in for support."

; ── Pages ──────────────────────────────────────────────────────────────────────
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ── Install Section ────────────────────────────────────────────────────────────
Section "${PRODUCT_NAME}" SecMain

  ; Kill any running instance first
  ExecWait 'taskkill /F /IM "${EXE_NAME}"' $0

  SetOutPath "$INSTDIR"

  ; Main executable
  File "dist\${EXE_NAME}"

  ; Logo files — copied to installer/ by GitHub Actions build
  File /nonfatal "logo.ico"
  File /nonfatal "logo.png"
  File /nonfatal "logo_icon.png"

  ; Config (preserve existing config.ini if present)
  ${IfNot} ${FileExists} "$INSTDIR\config.ini"
    File "config.ini"
  ${EndIf}

  ; Registry — install path + uninstall info
  WriteRegStr HKCU "${PRODUCT_SUBKEY}"  "InstallDir"     "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_SUBKEY}"  "Version"        "${PRODUCT_VER}"
  WriteRegStr HKCU "${UNINST_SUBKEY}"   "DisplayName"    "${PRODUCT_NAME}"
  WriteRegStr HKCU "${UNINST_SUBKEY}"   "DisplayVersion" "${PRODUCT_VER}"
  WriteRegStr HKCU "${UNINST_SUBKEY}"   "Publisher"      "Raj Systems & Technologies"
  WriteRegStr HKCU "${UNINST_SUBKEY}"   "URLInfoAbout"   "https://bizviewpro.in"
  WriteRegStr HKCU "${UNINST_SUBKEY}"   "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNINST_SUBKEY}"   "InstallLocation" "$INSTDIR"

  ; Desktop shortcut
  CreateShortCut "$DESKTOP\BizView Pro Agent.lnk" \
    "$INSTDIR\${EXE_NAME}" "" \
    "$INSTDIR\logo.ico" 0

  ; Start Menu shortcut
  CreateDirectory "$SMPROGRAMS\BizView Pro"
  CreateShortCut  "$SMPROGRAMS\BizView Pro\BizView Pro Agent.lnk" \
    "$INSTDIR\${EXE_NAME}" "" \
    "$INSTDIR\logo.ico" 0
  CreateShortCut  "$SMPROGRAMS\BizView Pro\Uninstall BizView Pro.lnk" \
    "$INSTDIR\Uninstall.exe"

  ; Windows Startup — auto-start in tray on login
  WriteRegStr HKCU \
    "Software\Microsoft\Windows\CurrentVersion\Run" \
    "BizViewProAgent" \
    '"$INSTDIR\${EXE_NAME}" --tray'

  ; Task Scheduler — runs even when not logged in (optional, admin only)
  ExecWait 'schtasks /Delete /TN "BizViewProAgent" /F' $0
  ExecWait 'schtasks /Create /TN "BizViewProAgent" \
    /TR "\"$INSTDIR\${EXE_NAME}\" --headless" \
    /SC ONLOGON /DELAY 0000:30 /RL HIGHEST /F' $0

  ; Uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

SectionEnd

; ── Uninstall Section ──────────────────────────────────────────────────────────
Section "Uninstall"

  ExecWait 'taskkill /F /IM "${EXE_NAME}"' $0
  ExecWait 'schtasks /Delete /TN "BizViewProAgent" /F' $0

  DeleteRegValue HKCU \
    "Software\Microsoft\Windows\CurrentVersion\Run" \
    "BizViewProAgent"

  Delete "$INSTDIR\${EXE_NAME}"
  Delete "$INSTDIR\logo.ico"
  Delete "$INSTDIR\logo.png"
  Delete "$INSTDIR\logo_icon.png"
  Delete "$INSTDIR\Uninstall.exe"

  ; Preserve config.ini on uninstall so user doesn't lose credentials
  ; Delete "$INSTDIR\config.ini"

  RMDir "$INSTDIR"

  Delete "$DESKTOP\BizView Pro Agent.lnk"
  Delete "$SMPROGRAMS\BizView Pro\BizView Pro Agent.lnk"
  Delete "$SMPROGRAMS\BizView Pro\Uninstall BizView Pro.lnk"
  RMDir  "$SMPROGRAMS\BizView Pro"

  DeleteRegKey HKCU "${PRODUCT_SUBKEY}"
  DeleteRegKey HKCU "${UNINST_SUBKEY}"

SectionEnd
