"""
Build TallySyncAgent.exe from agent.py using PyInstaller.

Requirements (install once):
    pip install pyinstaller cryptography

Run:
    python build_exe.py

Output:
    dist/TallySyncAgent.exe   — single-file Windows executable, no Python needed
"""

import subprocess
import sys
import shutil
from pathlib import Path

AGENT_SRC  = Path('src/agent.py')
DIST_DIR   = Path('dist')
BUILD_DIR  = Path('build')
ICON_FILE  = Path('src/icon.ico')    # optional — ignored if missing

def main():
    print('=== TallySync Agent — Build ===')

    # Check PyInstaller
    try:
        import PyInstaller
        print(f'PyInstaller {PyInstaller.__version__} found.')
    except ImportError:
        print('PyInstaller not found. Install with: pip install pyinstaller')
        sys.exit(1)

    if not AGENT_SRC.exists():
        print(f'ERROR: {AGENT_SRC} not found.')
        sys.exit(1)

    args = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',                          # single .exe
        '--console',                          # show console (set --windowed to hide)
        '--name', 'TallySyncAgent',
        '--distpath', str(DIST_DIR),
        '--workpath', str(BUILD_DIR),
        '--specpath', str(BUILD_DIR),
        '--clean',
        # Hidden imports for cryptography
        '--hidden-import', 'cryptography',
        '--hidden-import', 'cryptography.hazmat.primitives.ciphers.aead',
        '--hidden-import', 'cryptography.hazmat.backends.openssl',
    ]

    if ICON_FILE.exists():
        args += ['--icon', str(ICON_FILE)]

    args.append(str(AGENT_SRC))

    print('Running PyInstaller...')
    result = subprocess.run(args)

    if result.returncode == 0:
        exe = DIST_DIR / 'TallySyncAgent.exe'
        print(f'\n[SUCCESS] Built: {exe.resolve()}')
        print(f'Size: {exe.stat().st_size // 1024} KB')
        print('\nNext steps:')
        print('  1. Copy TallySyncAgent.exe + config.ini + install.bat to user\'s PC')
        print('  2. User fills in config.ini with their user_id, api_key, secret_key')
        print('  3. User runs install.bat as Administrator')
        print('  4. Done — agent syncs Tally every 5 minutes automatically')
    else:
        print('\n[FAILED] PyInstaller returned error code', result.returncode)
        sys.exit(result.returncode)

if __name__ == '__main__':
    main()
