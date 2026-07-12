"""
TallySync Mobile — Desktop Sync Agent  v4
==========================================
- Runs in Windows system tray (right-click to open/quit)
- Auto-syncs every N minutes in background thread
- Single request for ALL vouchers (no monthly batches)
- No sensitive credentials shown in UI
- Proper Windows installer via NSIS (see installer/ folder)
"""

import os, sys, time, gzip, json, base64, hashlib, logging, re, socket
import configparser, urllib.request, urllib.error, threading, traceback
from datetime import datetime, timedelta
from xml.sax.saxutils import escape
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

# ── Logo ──────────────────────────────────────────────────────────────────────
# Logo is embedded at build time — place logo.png in src/ folder before building
# GitHub Actions copies it into the exe via PyInstaller --add-data

def _get_logo_path(filename):
    """Find logo file — works both in dev and PyInstaller frozen exe."""
    # PyInstaller extracts data files to sys._MEIPASS
    if getattr(sys, '_MEIPASS', None):
        p = Path(sys._MEIPASS) / filename
        if p.exists(): return p
    # Dev mode — check src/ folder and EXE_DIR
    for d in [Path(__file__).parent, EXE_DIR]:
        p = d / filename
        if p.exists(): return p
    return None

def load_logo_image(size=(36,36)):
    """Load embedded logo — tries logo_icon.png first (no text), then logo.png."""
    for fname in ('logo_icon.png', 'logo.png', 'logo.ico'):
        p = _get_logo_path(fname)
        if p:
            try:
                from PIL import Image, ImageTk
                img = Image.open(str(p)).convert('RGBA').resize(size, Image.LANCZOS)
                return ImageTk.PhotoImage(img)
            except Exception:
                pass
    return None

def set_window_icon(root):
    """Set window taskbar + tray icon — tries .ico first, then .png."""
    # .ico is best on Windows (multi-res, shows in taskbar + tray + Alt+Tab)
    p = _get_logo_path('logo.ico')
    if p:
        try:
            root.iconbitmap(default=str(p))
            return
        except Exception:
            pass
    # Fallback: PNG via PIL (works in dev mode)
    for fname in ('logo_icon.png', 'logo.png'):
        p = _get_logo_path(fname)
        if p:
            try:
                from PIL import Image, ImageTk
                img = Image.open(str(p)).convert('RGBA').resize((32, 32), Image.LANCZOS)
                ico = ImageTk.PhotoImage(img)
                root.iconphoto(True, ico)
                root._taskbar_icon_ref = ico   # prevent GC
                return
            except Exception:
                pass

# ── Paths ─────────────────────────────────────────────────────────────────────
EXE_DIR = Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent

# Config and log go to AppData (writable) — not Program Files (read-only for users)
def _appdata_dir():
    # Windows: C:\Users\<user>\AppData\Roaming\TallySync
    appdata = os.environ.get('APPDATA', '')
    if appdata:
        d = Path(appdata) / 'TallySync'
    else:
        # Fallback: same folder as exe (dev mode)
        d = EXE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d

APP_DIR     = _appdata_dir()
CONFIG_FILE = APP_DIR / 'config.ini'
LOG_FILE    = APP_DIR / 'tallysync_agent.log'

# ── Logging (set up after APP_DIR is resolved) ────────────────────────────────
def _setup_logging():
    logger = logging.getLogger('TallySyncGUI')
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    try:
        fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass  # Can't write log — non-fatal
    return logger

log = _setup_logging()

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULTS = {'agent': {
    'master_key': '', 'master_secret': '',
    'user_id': '', 'api_key': '', 'secret_key': '',  # legacy single-company fallback
    'server_url': 'http://localhost/tallysync/api/ingest.php',
    'tally_host': 'http://localhost:9000',
    'interval_min': '5', 'compress': 'true', 'encrypt': 'true',
    'last_voucher_alterid': '',  # internal: AlterID watermark for incremental voucher sync
    'last_master_alterid': '',   # internal: AlterID watermark for incremental master sync
    'voucher_batch_size': '500',  # vouchers per request when sending to server
}}

def load_cfg():
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';','#'), strict=False, allow_no_value=True)
    cfg.read_dict(DEFAULTS)
    if CONFIG_FILE.exists():
        cfg.read(str(CONFIG_FILE), encoding='utf-8')
    for k in ('tally_host','server_url','user_id','api_key','secret_key','interval_min'):
        if cfg.has_option('agent', k):
            v = cfg.get('agent', k).split(';')[0].split('#')[0].strip()
            cfg.set('agent', k, ' '.join(v.split()))
    return cfg

def save_cfg(cfg):
    with open(CONFIG_FILE, 'w') as f:
        cfg.write(f)

def get_company_creds(cfg, company_name):
    """Look up per-company portal credentials.

    Each Tally company can be linked to a different TallySync portal account
    (its own user_id/api_key/secret_key), via an optional config section:

        [company:S.S. Electricals (from 1-Apr-25)]
        user_id    = 12
        api_key    = ...
        secret_key = ...

    Falls back to the [agent] defaults if no matching section exists —
    keeps single-company setups working with zero extra config.
    """
    section = f'company:{company_name}'
    def _get(key, default=''):
        if cfg.has_section(section) and cfg.has_option(section, key):
            return cfg.get(section, key).strip()
        return cfg.get('agent', key).strip() if cfg.has_option('agent', key) else default

    return {
        'user_id':    _get('user_id'),
        'api_key':    _get('api_key'),
        'secret_key': _get('secret_key'),
    }

def is_configured():
    cfg = load_cfg()
    if not cfg.has_section('agent'):
        return False
    mkey = cfg.get('agent','master_key').strip() if cfg.has_option('agent','master_key') else ''
    msec = cfg.get('agent','master_secret').strip() if cfg.has_option('agent','master_secret') else ''
    srv  = cfg.get('agent','server_url').strip() if cfg.has_option('agent','server_url') else ''
    if mkey and msec and srv:
        return True
    # Legacy single-company config still works
    uid = cfg.get('agent','user_id').strip() if cfg.has_option('agent','user_id') else ''
    key = cfg.get('agent','api_key').strip() if cfg.has_option('agent','api_key') else ''
    return bool(uid and key and srv)

# ── Windows "Start with Windows" (system tray autostart) ──────────────────────
# Uses the per-user registry Run key (HKCU) rather than Task Scheduler, because
# a Task Scheduler job that runs as SYSTEM cannot show a tray icon in the user's
# desktop session. HKCU\...\Run requires no admin rights and starts the GUI
# (minimised to tray via --tray) right after the user logs in.
AUTOSTART_REG_PATH   = r'Software\Microsoft\Windows\CurrentVersion\Run'
AUTOSTART_VALUE_NAME = 'BizViewProAgent'

def _autostart_command():
    """Command line stored in the registry Run key."""
    if getattr(sys, 'frozen', False):
        # Compiled EXE (PyInstaller) — launch itself minimised to tray.
        return f'"{sys.executable}" --tray'
    # Dev mode — launch via pythonw.exe (no console window) if available.
    pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not os.path.isfile(pyw):
        pyw = sys.executable
    script = os.path.abspath(sys.argv[0])
    return f'"{pyw}" "{script}" --tray'

def is_autostart_enabled():
    """True if the agent is currently registered to start with Windows."""
    if sys.platform != 'win32':
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH,
                              0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            return bool(val)
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False

def set_autostart(enable):
    """Enable/disable auto-launch (minimised to tray) on Windows login.
    Returns (ok: bool, error: str)."""
    if sys.platform != 'win32':
        return False, 'Auto-start with Windows is only supported on Windows.'
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH,
                              0, winreg.KEY_SET_VALUE)
        try:
            if enable:
                winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ,
                                   _autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True, ''
    except Exception as e:
        return False, str(e)

def find_tally_exe_path():
    """Locate tally.exe / tallyprime.exe on disk — works even when Tally is
    NOT currently running (unlike find_tally_ini_data_root's process lookup),
    so it can be used to launch Tally from the "Open Tally" button."""
    if sys.platform != 'win32':
        return None
    # 1) Windows "App Paths" registry — most installers register this.
    try:
        import winreg
        for exe_name in ('tally.exe', 'tallyprime.exe'):
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k = winreg.OpenKey(
                        hive,
                        r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s' % exe_name)
                    try:
                        val, _ = winreg.QueryValueEx(k, '')
                        if val and os.path.isfile(val):
                            return val
                    finally:
                        winreg.CloseKey(k)
                except OSError:
                    continue
    except Exception:
        pass
    # 2) Scan common install locations (same drives/dirs used elsewhere in this file).
    common_dirs = ['TallyPrime', 'Tally.ERP9', 'Tally9', 'TallyERP9']
    for drive in 'CDEFGH':
        for d in common_dirs:
            for base in (f'{drive}:\\{d}', f'{drive}:\\Program Files\\{d}',
                         f'{drive}:\\Program Files (x86)\\{d}'):
                for exe_name in ('tally.exe', 'tallyprime.exe'):
                    cand = os.path.join(base, exe_name)
                    if os.path.isfile(cand):
                        return cand
    return None

def _is_tally_unreachable_error(e):
    """True if the exception looks like 'nothing is listening on the Tally
    ODBC/XML port' (i.e. TallyPrime just isn't open) rather than some other
    failure (bad URL, portal down, auth error, etc). Covers the common
    variants: WinError 10061 (connection refused), urlopen errors, and
    generic 'connection refused'/'actively refused' phrasing."""
    msg = str(e).lower()
    return any(s in msg for s in (
        '10061', 'connection refused', 'actively refused',
        'urlopen error', 'connection reset', 'target machine actively',
        'econnrefused',
    ))

# ── Network helpers ───────────────────────────────────────────────────────────

def tally_post(host, xml, timeout=120):
    data = xml.encode('utf-8')
    req  = urllib.request.Request(host, data=data,
        headers={'Content-Type':'text/xml','Content-Length':str(len(data))}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        try:    return raw.decode('utf-8')
        except: return raw.decode('cp1252', errors='replace')

def collection_xml(name, typ, fields, fd='', td='', company=''):
    dates = (f'<SVFROMDATE>{fd}</SVFROMDATE>' if fd else '') + \
            (f'<SVTODATE>{td}</SVTODATE>'     if td else '')
    comp  = f'<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>' if company else ''
    return (f'<ENVELOPE><HEADER><VERSION>1</VERSION>'
            f'<TALLYREQUEST>Export</TALLYREQUEST>'
            f'<TYPE>Collection</TYPE><ID>{name}</ID></HEADER>'
            f'<BODY><DESC><STATICVARIABLES>'
            f'<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{dates}{comp}'
            f'</STATICVARIABLES><TDL><TDLMESSAGE>'
            f'<COLLECTION NAME="{name}" ISMODIFY="No">'
            f'<TYPE>{typ}</TYPE><FETCH>{",".join(fields)}</FETCH>'
            f'</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>')

_fetched_companies_cache = []

def _get_company_number(host, company_name):
    """Query Tally for one company's number using multiple TDL methods."""
    import re
    esc_name = escape(company_name)

    # Method A: Collection filtered to this company, compute $$Number
    xmlA = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>BVPNumA</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'<SVCURRENTCOMPANY>{esc_name}</SVCURRENTCOMPANY>'
        '</STATICVARIABLES><TDL><TDLMESSAGE>'
        '<COLLECTION NAME="BVPNumA" ISMODIFY="No">'
        '<TYPE>Company</TYPE>'
        '<FETCH>NAME</FETCH>'
        '<COMPUTE>N:$$String:$$Number</COMPUTE>'
        '<COMPUTE>P:$$DataPath</COMPUTE>'
        '</COLLECTION>'
        '</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>'
    )
    try:
        r = tally_post(host, xmlA, timeout=8)
        if r:
            # Save for diagnostics
            import os, tempfile
            safe = re.sub(r'[^a-zA-Z0-9]', '_', company_name[:25])
            try:
                with open(os.path.join(tempfile.gettempdir(), f'bvp_{safe}.xml'), 'w', encoding='utf-8') as f:
                    f.write(r)
            except Exception:
                pass
            # Try N field first
            m = re.search(r'<N[^>]*>(.*?)</N>', r, re.I)
            if m:
                val = m.group(1).strip()
                if re.match(r'^\d+$', val) and int(val) > 0:
                    return val
            # Try path-based extraction
            pm = re.search(r'<P[^>]*>(.*?)</P>', r, re.I)
            if pm:
                path = pm.group(1).strip()
                parts = re.split(r'[/\\]', path)
                for part in reversed(parts):
                    if re.match(r'^\d{5,6}$', part.strip()):
                        return part.strip()
    except Exception:
        pass

    # Method B: plain FETCH with standard field names
    xmlB = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>BVPNumB</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'<SVCURRENTCOMPANY>{esc_name}</SVCURRENTCOMPANY>'
        '</STATICVARIABLES><TDL><TDLMESSAGE>'
        '<COLLECTION NAME="BVPNumB" ISMODIFY="No">'
        '<TYPE>Company</TYPE>'
        '<FETCH>NAME,COMPANYNUMBER,BASICCOMPANYNUMBER,COMPANYID,DATAPATH,CMPDATAPATH</FETCH>'
        '</COLLECTION>'
        '</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>'
    )
    try:
        r = tally_post(host, xmlB, timeout=8)
        if r:
            for tag in ['COMPANYNUMBER','BASICCOMPANYNUMBER','COMPANYID']:
                m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', r, re.I)
                if m:
                    val = m.group(1).strip()
                    if re.match(r'^\d+$', val) and int(val) > 0:
                        return val
            for tag in ['DATAPATH','CMPDATAPATH']:
                m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', r, re.I)
                if m:
                    path = m.group(1).strip()
                    parts = re.split(r'[/\\]', path)
                    for part in reversed(parts):
                        if re.match(r'^\d{5,6}$', part.strip()):
                            return part.strip()
    except Exception:
        pass
    return ''


def fetch_companies(host):
    """
    Fetch all open companies in ONE request, no SVCURRENTCOMPANY.
    SVCURRENTCOMPANY is broken when multiple companies are open — querying
    per-company returns the ACTIVE company number for every row.
    Single request with FETCH DATAPATH returns correct per-row data.
    """
    import os, tempfile
    xml = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>BVPAllCo</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        '</STATICVARIABLES><TDL><TDLMESSAGE>'
        '<COLLECTION NAME="BVPAllCo" ISMODIFY="No">'
        '<TYPE>Company</TYPE>'
        '<FETCH>NAME,GUID,COMPANYNUMBER,BASICCOMPANYNUMBER,'
        'COMPANYID,DATAPATH,CMPDATAPATH,STARTINGFROM,ENDINGAT</FETCH>'
        '</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>'
    )
    raw = tally_post(host, xml, timeout=15)
    try:
        log = os.path.join(tempfile.gettempdir(), 'bvp_tally_companies.xml')
        with open(log, 'w', encoding='utf-8') as lf:
            lf.write(raw or 'EMPTY')
    except Exception:
        pass
    return raw

def fetch_ledgers(host, company=''):
    return tally_post(host, collection_xml('TSLed','Ledger',
        ['GUID','ALTERID','NAME','PARENT','CLOSINGBALANCE','OPENINGBALANCE',
         'LEDMAILINGDETAILS.LIST.MAILINGNAME',
         'LEDMAILINGDETAILS.LIST.PINCODE',
         'LEDMAILINGDETAILS.LIST.PHONENUMBER',
         'LEDMAILINGDETAILS.LIST.MOBILEPHONE',
         'LEDMAILINGDETAILS.LIST.LANDLINEPHONE',
         'CONTACTNO','PHONENUMBER','MOBILEPHONE','LEDGERPHONE','LEDGERMOBILE'],
        company=company), timeout=60)

def fetch_stock(host, company=''):
    return tally_post(host, collection_xml('TSStk','StockItem',
        ['GUID','ALTERID','NAME','PARENT','BASEUNITS',
         'CLOSINGBALANCE','CLOSINGVALUE','RATE','OPENINGBALANCE','OPENINGVALUE'],
        company=company), timeout=60)

def fetch_voucher_types(host, company=''):
    """Voucher Types master — PARENT gives the real base type (Sales,
    Purchase, Payment, Receipt, Journal...) for whatever custom name the
    company actually uses (e.g. 'Tax Invoice' with PARENT='Sales'). Lets the
    portal classify custom voucher type names accurately instead of guessing
    from the name alone."""
    return tally_post(host, collection_xml('TSVchTypes','VoucherType',
        ['GUID','ALTERID','NAME','PARENT','AFFECTSSTOCK','ISDEEMEDPOSITIVE'],
        company=company), timeout=60)

def fetch_godowns(host, company=''):
    """Godowns/warehouses master — not consumed by any report yet, synced
    proactively so godown-wise stock reporting can be built later without
    needing another agent release just to backfill history."""
    return tally_post(host, collection_xml('TSGdn','Godown',
        ['GUID','ALTERID','NAME','PARENT'],
        company=company), timeout=60)

VOUCHER_FETCH_FIELDS = (
    'GUID,ALTERID,MASTERID,DATE,VOUCHERTYPENAME,VOUCHERNUMBER,ISOPTIONAL,ISDELETED,'
    'PARTYLEDGERNAME,NARRATION,'
    'ALLLEDGERENTRIES.LIST.LEDGERNAME,'
    'ALLLEDGERENTRIES.LIST.AMOUNT,'
    'ALLLEDGERENTRIES.LIST.ISDEEMEDPOSITIVE,'
    'INVENTORYENTRIES.LIST.STOCKITEMNAME,'
    'INVENTORYENTRIES.LIST.ACTUALQTY,'
    'INVENTORYENTRIES.LIST.BILLEDQTY,'
    'INVENTORYENTRIES.LIST.RATE,'
    'INVENTORYENTRIES.LIST.AMOUNT,'
    'INVENTORYENTRIES.LIST.BATCHALLOCATIONS.LIST.BATCHNAME,'
    'INVENTORYENTRIES.LIST.BATCHALLOCATIONS.LIST.ACTUALQTY,'
    'INVENTORYENTRIES.LIST.BATCHALLOCATIONS.LIST.AMOUNT'
)

def fetch_vouchers_by_alterid(host, min_alterid, company=''):
    """Fetch vouchers with ALTERID > min_alterid — i.e. vouchers created OR
    EDITED since the last sync (Tally bumps ALTERID on every save, including
    edits to old vouchers), regardless of voucher date. This is the
    INCREMENTAL sync path — it naturally picks up backdated edits that a
    date-range filter would miss."""
    comp_var = f'<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>' if company else ''
    xml = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>TSAllVch</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'{comp_var}'
        '</STATICVARIABLES>'
        '<TDL><TDLMESSAGE>'
        '<COLLECTION NAME="TSAllVch" ISMODIFY="No">'
        '<TYPE>Voucher</TYPE>'
        '<FILTER>TSAlterFilter</FILTER>'
        f'<FETCH>{VOUCHER_FETCH_FIELDS}</FETCH>'
        '</COLLECTION>'
        '</TDLMESSAGE>'
        '<TDLMESSAGE>'
        f'<SYSTEM TYPE="Formulae" NAME="TSAlterFilter">$AlterId &gt; {int(min_alterid)}</SYSTEM>'
        '</TDLMESSAGE>'
        '</TDL></DESC></BODY></ENVELOPE>'
    )
    return tally_post(host, xml, timeout=300)

def fetch_ledgers_by_alterid(host, min_alterid, company=''):
    """Incremental ledger sync — only ledgers with AlterID > min_alterid.
    Tally bumps AlterID on every master save (create, edit, delete).
    Returns XML with only changed/new/deleted ledgers."""
    comp_var = f'<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>' if company else ''
    fields = ','.join([
        'GUID','ALTERID','NAME','PARENT','CLOSINGBALANCE','OPENINGBALANCE',
        'LEDMAILINGDETAILS.LIST.MAILINGNAME',
        'LEDMAILINGDETAILS.LIST.PINCODE',
        'LEDMAILINGDETAILS.LIST.PHONENUMBER',
        'LEDMAILINGDETAILS.LIST.MOBILEPHONE',
        'LEDMAILINGDETAILS.LIST.LANDLINEPHONE',
        'CONTACTNO','PHONENUMBER','MOBILEPHONE','LEDGERPHONE','LEDGERMOBILE','ISDELETEDMASTER',
    ])
    xml = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>TSLedIncr</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'{comp_var}'
        '</STATICVARIABLES>'
        '<TDL><TDLMESSAGE>'
        '<COLLECTION NAME="TSLedIncr" ISMODIFY="No">'
        '<TYPE>Ledger</TYPE>'
        '<FILTER>TSMasterAlterFilter</FILTER>'
        f'<FETCH>{fields}</FETCH>'
        '</COLLECTION>'
        '</TDLMESSAGE>'
        '<TDLMESSAGE>'
        f'<SYSTEM TYPE="Formulae" NAME="TSMasterAlterFilter">$AlterId &gt; {int(min_alterid)}</SYSTEM>'
        '</TDLMESSAGE>'
        '</TDL></DESC></BODY></ENVELOPE>'
    )
    return tally_post(host, xml, timeout=120)


def fetch_stock_by_alterid(host, min_alterid, company=''):
    """Incremental stock item sync — only items with AlterID > min_alterid."""
    comp_var = f'<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>' if company else ''
    fields = ','.join([
        'GUID','ALTERID','NAME','PARENT','BASEUNITS',
        'OPENINGBALANCE','OPENINGVALUE','OPENINGRATE',
        'CLOSINGBALANCE','CLOSINGVALUE','CLOSINGRATE','ISDELETEDMASTER',
    ])
    xml = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>TSStockIncr</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'{comp_var}'
        '</STATICVARIABLES>'
        '<TDL><TDLMESSAGE>'
        '<COLLECTION NAME="TSStockIncr" ISMODIFY="No">'
        '<TYPE>StockItem</TYPE>'
        '<FILTER>TSMasterAlterFilter</FILTER>'
        f'<FETCH>{fields}</FETCH>'
        '</COLLECTION>'
        '</TDLMESSAGE>'
        '<TDLMESSAGE>'
        f'<SYSTEM TYPE="Formulae" NAME="TSMasterAlterFilter">$AlterId &gt; {int(min_alterid)}</SYSTEM>'
        '</TDLMESSAGE>'
        '</TDL></DESC></BODY></ENVELOPE>'
    )
    return tally_post(host, xml, timeout=120)


def extract_max_master_alterid(xml):
    """Extract the highest AlterID seen in a master (ledger/stock) XML response."""
    max_id = 0
    for m in re.findall(r'<ALTERID[^>]*>(.*?)</ALTERID>', xml, re.I | re.S):
        try:
            v = int(re.sub(r'[^0-9]', '', m.strip()))
            if v > max_id:
                max_id = v
        except (ValueError, TypeError):
            pass
    return max_id


VOUCHER_BLOCK_RE = re.compile(r'<VOUCHER\b.*?</VOUCHER>', re.S)

def fetch_voucher_numbers_for_renumber(host, company='', from_date=None, to_date=None):
    """
    Lightweight renumber check — fetches only GUID+VOUCHERNUMBER for CURRENT FY.
    Limited to current financial year only (not all years) so it runs fast.
    Tally renumbers when a voucher is inserted between existing entries.
    """
    import datetime
    comp_var = f'<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>' if company else ''
    # Limit to current FY for speed (renumber only affects current period entries)
    if not from_date or not to_date:
        today = datetime.date.today()
        if today.month >= 4:
            fy_s = datetime.date(today.year,     4, 1)
            fy_e = datetime.date(today.year + 1, 3, 31)
        else:
            fy_s = datetime.date(today.year - 1, 4, 1)
            fy_e = datetime.date(today.year,     3, 31)
        fd = fy_s.strftime('%d-%b-%Y')
        td = fy_e.strftime('%d-%b-%Y')
    else:
        fd = from_date
        td = to_date
    dates = f'<SVFROMDATE>{fd}</SVFROMDATE><SVTODATE>{td}</SVTODATE>'
    xml = (f'<ENVELOPE><HEADER><VERSION>1</VERSION>'
           f'<TALLYREQUEST>Export</TALLYREQUEST>'
           f'<TYPE>Collection</TYPE><ID>TSVnoRenumber</ID></HEADER>'
           f'<BODY><DESC><STATICVARIABLES>'
           f'<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{dates}{comp_var}'
           f'</STATICVARIABLES><TDL><TDLMESSAGE>'
           f'<COLLECTION NAME="TSVnoRenumber" ISMODIFY="No">'
           f'<TYPE>Voucher</TYPE>'
           f'<FETCH>GUID,VOUCHERNUMBER</FETCH>'
           f'</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>')
    resp = tally_post(host, xml, timeout=30)  # 30s timeout — current FY only
    pairs = []
    if resp:
        for m in re.finditer(r'<VOUCHER[^>]*>(.*?)</VOUCHER>', resp, re.S|re.I):
            blk  = m.group(1)
            gm   = re.search(r'<GUID[^>]*>(.*?)</GUID>', blk, re.I)
            nm   = re.search(r'<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>', blk, re.I)
            guid = gm.group(1).strip() if gm else ''
            no   = nm.group(1).strip() if nm else ''
            if guid and no:
                pairs.append({'guid': guid, 'no': no})
    return pairs

def fetch_all_vouchers_unfiltered(host, company='', timeout=1800):
    """Fetch ALL vouchers for a company across ALL financial years.

    KEY LEARNINGS:
    1. SVCURRENTCOMPANY must be set to target the right company when multiple are open.
    2. Without SVFROMDATE/SVTODATE, Tally only returns the CURRENT PERIOD
       (shown in Tally header e.g. "1-Apr-26 to 31-Mar-27") — not all years!
    3. Setting SVFROMDATE=01-Apr-1990 and SVTODATE=31-Mar-2099 forces Tally
       to return ALL vouchers regardless of selected period.
    4. The Voucher collection TYPE respects these date variables.
    """
    comp_var = f'<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>' if company else ''
    # Wide date range to capture ALL financial years (1990 to 2099)
    date_range = (
        '<SVFROMDATE>01-Apr-1990</SVFROMDATE>'
        '<SVTODATE>31-Mar-2099</SVTODATE>'
    )
    xml = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>TSAllVch</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'{comp_var}{date_range}'
        '</STATICVARIABLES>'
        '<TDL><TDLMESSAGE>'
        '<COLLECTION NAME="TSAllVch" ISMODIFY="No">'
        '<TYPE>Voucher</TYPE>'
        f'<FETCH>{VOUCHER_FETCH_FIELDS}</FETCH>'
        '</COLLECTION>'
        '</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>'
    )
    return tally_post(host, xml, timeout=timeout)

def count_vouchers(xml):
    """Number of <VOUCHER ...>...</VOUCHER> blocks in a response."""
    return len(VOUCHER_BLOCK_RE.findall(xml))

def split_vouchers_xml(xml, batch_size=500):
    """Split a Tally voucher-collection XML response into batches of up to
    `batch_size` <VOUCHER>...</VOUCHER> blocks each, re-wrapped with the
    same header/footer so each batch is a valid standalone response the
    server can parse. Returns [xml] unchanged if there are no vouchers
    or fewer than batch_size."""
    vouchers = VOUCHER_BLOCK_RE.findall(xml)
    if len(vouchers) <= batch_size:
        return [xml]
    first_start = xml.find(vouchers[0])
    last_block  = vouchers[-1]
    last_end    = xml.rfind(last_block) + len(last_block)
    header = xml[:first_start]
    footer = xml[last_end:]
    batches = []
    for i in range(0, len(vouchers), batch_size):
        chunk = vouchers[i:i + batch_size]
        batches.append(header + ''.join(chunk) + footer)
    return batches

def extract_max_alterid(xml):
    """Max ALTERID seen across all vouchers in a response (0 if none).

    NOTE: must tolerate tag attributes (e.g. <ALTERID TYPE="Number">) and
    surrounding whitespace/newlines around the digits — Tally's actual XML
    export doesn't always match a strict '<ALTERID>123</ALTERID>' pattern.
    The server side (tally_client.php) already parses it this way; this
    mirrors that so the local watermark advances correctly."""
    ids = []
    for m in re.findall(r'<ALTERID[^>]*>(.*?)</ALTERID>', xml, re.I | re.S):
        m = m.strip()
        if m.isdigit():
            ids.append(int(m))
    return max(ids) if ids else 0

def extract_date_range(xml):
    """(min_date, max_date) as YYYYMMDD strings found in <DATE> tags, or
    (None, None) if no dates present. Used for informational logging only.
    Tolerant of tag attributes / whitespace — see extract_max_alterid."""
    dates = []
    for m in re.findall(r'<DATE[^>]*>(.*?)</DATE>', xml, re.I | re.S):
        m = m.strip()
        if re.match(r'^\d{8}$', m):
            dates.append(m)
    if not dates:
        return (None, None)
    return (min(dates), max(dates))

def fetch_all_vouchers(host, from_date=None, to_date=None, company=''):
    """Fetch vouchers from Tally.

    If from_date is given (YYYYMMDD), restricts the export to that date range
    via SVFROMDATE/SVTODATE — used for incremental syncs. With no date range,
    pulls ALL vouchers (first-ever sync).
    """
    date_vars = ''
    if from_date:
        to_d = to_date or datetime.now().strftime('%Y%m%d')
        date_vars = (
            f'<SVFROMDATE>{from_date}</SVFROMDATE>'
            f'<SVTODATE>{to_d}</SVTODATE>'
        )
    comp_var = f'<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>' if company else ''
    obj_xml = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>TSAllVch</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'{date_vars}{comp_var}'
        '</STATICVARIABLES>'
        '<TDL><TDLMESSAGE>'
        '<COLLECTION NAME="TSAllVch" ISMODIFY="No">'
        '<TYPE>Voucher</TYPE>'
        '<FETCH>GUID,ALTERID,MASTERID,DATE,VOUCHERTYPENAME,VOUCHERNUMBER,'
        'PARTYLEDGERNAME,NARRATION,'
        'ALLLEDGERENTRIES.LIST.LEDGERNAME,'
        'ALLLEDGERENTRIES.LIST.AMOUNT,'
        'ALLLEDGERENTRIES.LIST.ISDEEMEDPOSITIVE,'
        'INVENTORYENTRIES.LIST.STOCKITEMNAME,'
        'INVENTORYENTRIES.LIST.ACTUALQTY,'
        'INVENTORYENTRIES.LIST.BILLEDQTY,'
        'INVENTORYENTRIES.LIST.RATE,'
        'INVENTORYENTRIES.LIST.AMOUNT</FETCH>'
        '</COLLECTION>'
        '</TDLMESSAGE></TDL>'
        '</DESC></BODY></ENVELOPE>'
    )
    return tally_post(host, obj_xml, timeout=300)

def compress_data(data):
    return gzip.compress(data, compresslevel=6)

def derive_key(secret):
    return hashlib.sha256(secret.encode('utf-8')).digest()

def encrypt_aes_gcm(data, secret):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key   = derive_key(secret)
        nonce = os.urandom(12)
        ct    = AESGCM(key).encrypt(nonce, data, None)
        return base64.b64encode(nonce + ct)
    except ImportError:
        return base64.b64encode(data)

def build_bundle(user_id, data_type, xml_body, meta=None):
    return json.dumps({
        'user_id': user_id, 'data_type': data_type,
        'from_date': (meta or {}).get('from_date',''),
        'to_date':   (meta or {}).get('to_date',''),
        'fetched_at': datetime.utcnow().isoformat()+'Z',
        'agent_ver': '4.0', 'xml': xml_body,
    }, ensure_ascii=False).encode('utf-8')

def send_bundle(server_url, user_id, api_key, bundle_bytes,
                compress_=True, encrypt_=True, secret='', extra=None):
    payload  = compress_data(bundle_bytes) if compress_ else bundle_bytes
    cmp_flag = '1' if compress_ else '0'
    if encrypt_ and secret:
        payload  = encrypt_aes_gcm(payload, secret)
        enc_flag = '1'
    else:
        payload  = base64.b64encode(payload)
        enc_flag = '0'

    boundary = 'TSSyncBnd' + hashlib.md5(payload[:16]).hexdigest()[:8]
    fields   = {'uid': str(user_id), 'key': api_key,
                'enc': enc_flag, 'cmp': cmp_flag,
                'payload': payload.decode('ascii')}
    if extra:
        fields.update(extra)
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}'
             for k,v in fields.items()]
    parts.append(f'--{boundary}--')
    body = ('\r\n'.join(parts)).encode('utf-8')

    req = urllib.request.Request(server_url, data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                 'Content-Length': str(len(body)),
                 'X-TallySync-Agent': '4.0'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = r.read().decode('utf-8', errors='replace')
            log.info(f'Server response: {resp[:300]}')
            try:    return json.loads(resp)
            except: return {'ok': False, 'error': f'Bad JSON: {resp[:300]}'}
    except urllib.error.HTTPError as e:
        body2 = e.read().decode('utf-8', errors='replace')[:400]
        log.error(f'HTTP {e.code}: {body2}')
        return {'ok': False, 'error': f'HTTP {e.code}: {body2}'}
    except Exception as ex:
        return {'ok': False, 'error': str(ex)}

def simple_post(url, fields, timeout=30):
    """POST a simple application/x-www-form-urlencoded request, return parsed JSON."""
    import urllib.parse
    data = urllib.parse.urlencode(fields).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'X-TallySync-Agent': '4.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = r.read().decode('utf-8', errors='replace')
            try:    return json.loads(resp)
            except: return {'ok': False, 'error': f'Bad JSON: {resp[:300]}'}
    except urllib.error.HTTPError as e:
        body2 = e.read().decode('utf-8', errors='replace')[:400]
        return {'ok': False, 'error': f'HTTP {e.code}: {body2}'}
    except Exception as ex:
        return {'ok': False, 'error': str(ex)}

def agent_companies_url(server_url):
    """Derive api/agent_companies.php from the configured api/ingest.php URL."""
    if server_url.endswith('ingest.php'):
        return server_url[:-len('ingest.php')] + 'agent_companies.php'
    base = server_url.rsplit('/', 1)[0]
    return base + '/agent_companies.php'

def fetch_voucher_guids_for_presence_check(host, company='', from_date=None, to_date=None):
    """
    Lightweight GUID-only fetch across the whole synced range, used to detect
    vouchers that were hard-deleted in Tally. Tally does NOT emit an
    ISDELETED=Yes tombstone for a genuinely deleted voucher in a normal
    Collection export — the voucher simply stops appearing at all. So the
    only reliable way to know "voucher X no longer exists in Tally" is to
    compare a fresh full list of GUIDs against what the portal has marked
    active, and flag anything missing as deleted.
    """
    comp_var = f'<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>' if company else ''
    dates = ''
    if from_date: dates += f'<SVFROMDATE>{from_date}</SVFROMDATE>'
    if to_date:   dates += f'<SVTODATE>{to_date}</SVTODATE>'
    xml = (f'<ENVELOPE><HEADER><VERSION>1</VERSION>'
           f'<TALLYREQUEST>Export</TALLYREQUEST>'
           f'<TYPE>Collection</TYPE><ID>TSVGuids</ID></HEADER>'
           f'<BODY><DESC><STATICVARIABLES>'
           f'<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{dates}{comp_var}'
           f'</STATICVARIABLES><TDL><TDLMESSAGE>'
           f'<COLLECTION NAME="TSVGuids" ISMODIFY="No">'
           f'<TYPE>Voucher</TYPE>'
           f'<FETCH>GUID</FETCH>'
           f'</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>')
    resp = tally_post(host, xml, timeout=120)
    guids = []
    if resp:
        for m in re.finditer(r'<GUID[^>]*>(.*?)</GUID>', resp, re.I | re.S):
            g = re.sub(r'\s+', '', m.group(1))
            if g: guids.append(g)
    return guids

def voucher_presence_url(server_url):
    """Derive api/voucher_presence.php from the configured ingest URL."""
    if server_url.endswith('ingest.php'):
        return server_url[:-len('ingest.php')] + 'voucher_presence.php'
    base = server_url.rsplit('/', 1)[0]
    return base + '/voucher_presence.php'

def renumber_vouchers_url(server_url):
    """Derive api/renumber_vouchers.php from the configured ingest URL."""
    if server_url.endswith('ingest.php'):
        return server_url[:-len('ingest.php')] + 'renumber_vouchers.php'
    base = server_url.rsplit('/', 1)[0]
    return base + '/renumber_vouchers.php'

def agent_backup_url(server_url):
    """Derive api/agent_backup.php from the configured ingest URL."""
    if server_url.endswith('ingest.php'):
        return server_url[:-len('ingest.php')] + 'agent_backup.php'
    base = server_url.rsplit('/', 1)[0]
    return base + '/agent_backup.php'

_tally_ini_data_root_cache = {'value': None, 'tried': False}

def find_tally_ini_data_root():
    """
    Finds the Tally data ROOT folder (the "Data=" line in tally.ini) by
    locating the actual running Tally.exe and reading tally.ini from the
    same folder. This is the fallback used when Tally's own DATAPATH/
    CMPDATAPATH XML fields don't resolve to a real folder on this PC — the
    data path varies a lot machine to machine (custom drives, client-server
    setups where the XML path can refer to the server's path, etc.), so the
    installed Tally's own config file is the most reliable source of truth.
    Cached for the life of the process — this only needs to run once.
    """
    if _tally_ini_data_root_cache['tried']:
        return _tally_ini_data_root_cache['value']
    _tally_ini_data_root_cache['tried'] = True

    exe_path = None
    # Strategy 1: PowerShell (most reliable on modern Windows, incl. Win 11 24H2+
    # where wmic has been removed)
    try:
        import subprocess
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "(Get-Process -Name tally,tallyprime,tally9 -ErrorAction SilentlyContinue | "
             "Select-Object -First 1 -ExpandProperty Path)"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        p = (out.stdout or '').strip()
        if p and os.path.isfile(p):
            exe_path = p
    except Exception:
        pass

    # Strategy 2: wmic (older Windows, still common)
    if not exe_path:
        try:
            import subprocess
            out = subprocess.run(
                ['wmic', 'process', 'where', "name='tally.exe' or name='tallyprime.exe'",
                 'get', 'ExecutablePath'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )
            for line in (out.stdout or '').splitlines():
                line = line.strip()
                if line and line.lower() != 'executablepath' and os.path.isfile(line):
                    exe_path = line; break
        except Exception:
            pass

    # Strategy 3: scan common install locations on every drive letter
    if not exe_path:
        common_dirs = ['TallyPrime', 'Tally.ERP9', 'Tally9', 'TallyERP9']
        for drive in 'CDEFGH':
            for d in common_dirs:
                for base in (f'{drive}:\\{d}', f'{drive}:\\Program Files\\{d}', f'{drive}:\\Program Files (x86)\\{d}'):
                    for exe_name in ('tally.exe', 'tallyprime.exe'):
                        cand = os.path.join(base, exe_name)
                        if os.path.isfile(cand):
                            exe_path = cand; break
                    if exe_path: break
                if exe_path: break
            if exe_path: break

    if not exe_path:
        return None

    ini_path = os.path.join(os.path.dirname(exe_path), 'tally.ini')
    if not os.path.isfile(ini_path):
        return None

    try:
        with open(ini_path, 'r', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if line.lower().startswith('data='):
                    data_path = line.split('=', 1)[1].strip()
                    if data_path and os.path.isdir(data_path):
                        _tally_ini_data_root_cache['value'] = data_path
                        return data_path
    except Exception:
        pass
    return None

def zip_company_folder(folder_path, tally_number):
    """Zip a Tally company's data folder to a temp file. Returns the zip path, or None."""
    import tempfile, zipfile, os
    safe_num = re.sub(r'[^A-Za-z0-9]', '', str(tally_number)) or 'company'
    zip_path = os.path.join(tempfile.gettempdir(),
                             f'tsbackup_{safe_num}_{datetime.now().strftime("%Y%m%d")}.zip')
    try:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(folder_path):
                for f in files:
                    fp = os.path.join(root, f)
                    arcname = os.path.relpath(fp, folder_path)
                    try:
                        zf.write(fp, arcname)
                    except Exception:
                        pass  # Tally may hold some files open/locked — skip, don't fail the whole backup
        return zip_path if os.path.getsize(zip_path) > 0 else None
    except Exception as e:
        log.error(f'Zip backup failed: {e}')
        return None

def upload_company_backup(server_url, user_id, api_key, secret, tally_number, zip_path, company_name='', progress_cb=None):
    """
    Upload a zipped Tally company data folder to the portal in small base64
    chunks (NOT one large multipart file). upload_max_filesize/post_max_size
    are php.ini-level settings the portal can't override at runtime, and on
    shared hosting they're often just a few MB — a single-request upload of
    a real Tally data folder zip would keep failing with "upload error 1"
    (UPLOAD_ERR_INI_SIZE) regardless of what the portal script does. Chunking
    keeps every individual request small enough to clear any reasonable
    hosting default.
    """
    import os
    import urllib.parse
    url = agent_backup_url(server_url)
    try:
        file_size = os.path.getsize(zip_path)
    except Exception as e:
        return {'ok': False, 'error': f'Could not read zip: {e}'}

    CHUNK_SIZE = 1_500_000  # raw bytes per chunk (~2MB after base64 encoding)
    total_chunks = max(1, (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE)
    fname = os.path.basename(zip_path)
    backup_id = None

    try:
        with open(zip_path, 'rb') as f:
            chunk_no = 1
            while True:
                raw = f.read(CHUNK_SIZE)
                if not raw:
                    break
                b64 = base64.b64encode(raw).decode('ascii')
                fields = {
                    'uid': user_id, 'key': api_key,
                    'tally_number': tally_number, 'company_name': company_name,
                    'chunk_no': str(chunk_no), 'total_chunks': str(total_chunks),
                    'chunk_data': b64,
                }
                if chunk_no == 1:
                    fields['file_name'] = fname
                else:
                    fields['backup_id'] = str(backup_id)

                body = urllib.parse.urlencode(fields).encode('utf-8')
                req = urllib.request.Request(url, data=body,
                    headers={'Content-Type': 'application/x-www-form-urlencoded',
                             'X-TallySync-Agent': '4.0'},
                    method='POST')
                try:
                    with urllib.request.urlopen(req, timeout=120) as r:
                        resp = json.loads(r.read().decode('utf-8', errors='replace'))
                except urllib.error.HTTPError as e:
                    return {'ok': False, 'error': f'HTTP {e.code} on chunk {chunk_no}/{total_chunks}: '
                                                   f'{e.read().decode("utf-8", errors="replace")[:300]}'}
                except Exception as ex:
                    return {'ok': False, 'error': f'Chunk {chunk_no}/{total_chunks} failed: {ex}'}

                if not resp.get('ok'):
                    return {'ok': False, 'error': f'Chunk {chunk_no}/{total_chunks} rejected: {resp.get("error")}'}
                if chunk_no == 1:
                    backup_id = resp.get('backup_id')
                if progress_cb:
                    try: progress_cb(chunk_no, total_chunks)
                    except Exception: pass
                chunk_no += 1

        return {'ok': True, 'backup_id': backup_id, 'total_chunks': total_chunks}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def discover_companies_on_server(server_url, master_key, master_secret, companies):
    """Tell the portal which Tally companies the agent can see."""
    url = agent_companies_url(server_url)
    return simple_post(url, {
        'master_key': master_key,
        'master_secret': master_secret,
        'action': 'discover',
        'companies_json': json.dumps([{'name': c['name'], 'guid': c.get('guid',''), 'number': c.get('number',''), 'path': c.get('path','')} for c in companies]),
    })

def list_assigned_companies(server_url, master_key, master_secret):
    """Get the companies + per-company sync credentials the portal has activated for this user."""
    url = agent_companies_url(server_url)
    return simple_post(url, {
        'master_key': master_key,
        'master_secret': master_secret,
        'action': 'list',
    })
def parse_companies(xml):
    """Parse company XML. Extracts number from DATAPATH per row (no SVCURRENTCOMPANY)."""
    import re
    global _fetched_companies_cache
    if xml == '__BVP_DIRECT__':
        return list(_fetched_companies_cache)
    companies = []
    if not xml:
        return companies
    for m in re.finditer(r'<COMPANY\b[^>]*>(.*?)</COMPANY>', xml, re.S|re.I):
        block = m.group(0)
        nm = re.search(r'NAME="([^"]+)"', block, re.I)
        if not nm:
            nm = re.search(r'<NAME[^>]*>(.*?)</NAME>', block, re.I)
        name = nm.group(1).strip() if nm else ''
        if not name:
            continue
        gm = re.search(r'<GUID[^>]*>(.*?)</GUID>', block, re.I)
        guid = gm.group(1).strip() if gm else ''
        # Number: explicit fields first, then DATAPATH extraction
        number = ''
        for tag in ['COMPANYNUMBER', 'BASICCOMPANYNUMBER', 'COMPANYID']:
            fm = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', block, re.I)
            if fm:
                val = fm.group(1).strip()
                if re.match(r'^\d+$', val) and int(val) > 0:
                    number = val; break
        # Path: ALWAYS read DATAPATH/CMPDATAPATH — this used to be skipped
        # whenever `number` was already found via the tags above, which is
        # the common case, so `path` silently stayed empty for most users
        # and the daily Tally-folder backup could never find anything to
        # zip ("Backup skipped — Tally data folder not found"). Also used
        # as a fallback source for `number` when no explicit tag had it.
        path = ''
        for tag in ['DATAPATH', 'CMPDATAPATH']:
            pm = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', block, re.I)
            if pm:
                val = pm.group(1).strip()
                if val:
                    path = val
                    if not number:
                        for part in reversed(re.split(r'[/\\]', path)):
                            if re.match(r'^\d{5,6}$', part.strip()):
                                number = part.strip(); break
                    break
        companies.append({'name': name, 'guid': guid, 'number': number, 'path': path})
    return companies

# ── Main GUI ──────────────────────────────────────────────────────────────────

class TallySyncApp:
    def __init__(self, root):
        self.root      = root
        self.cfg       = load_cfg()
        self.syncing   = False
        self.stop_flag = False
        self.paused    = False
        self.companies = []
        self.assigned = []           # companies activated on the portal (with sync creds)
        self.pending_setup = False   # True if user must select companies on portal
        self.setup_url = ''
        self._next_sync = time.time() + self._interval_secs()
        self._pause_remaining = 0   # seconds left on countdown when Pause was pressed
        self._settings_win = None
        self._tally_connected = False   # tracks last-known Tally reachability (for UI + auto-refresh)
        self._build_ui()
        self.root.after(500, self._auto_connect)
        # Start the 30s auto-refresh loop unconditionally — this is what lets the
        # agent pick up Tally companies automatically once TallyPrime is opened,
        # even if the very first connect attempt happened while Tally was closed.
        self._start_auto_refresh()
        self._tick()

    def _interval_secs(self):
        try:
            val = self.cfg.get('agent','interval_min') if self.cfg.has_option('agent','interval_min') else '5'
            return max(60, int(val) * 60)
        except Exception:
            return 300

    # ── BUILD UI ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.title('BizView Pro — Sync Agent')
        self.root.resizable(False, True)
        self.root.configure(bg='#f0f4f9')
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        set_window_icon(self.root)

        # ── Royal Blue Windows title bar (Windows 11 / 10 build 22000+) ─────
        def _set_title_bar_color(hwnd, r=0x14, g=0x64, b=0xf4):
            """Use DwmSetWindowAttribute to color the title bar Royal Blue."""
            try:
                import ctypes
                DWMWA_CAPTION_COLOR = 35
                # COLORREF is 0x00BBGGRR
                color = ctypes.c_int(b << 16 | g << 8 | r)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(DWMWA_CAPTION_COLOR),
                    ctypes.byref(color),
                    ctypes.sizeof(color)
                )
                # Also set title bar text to white
                DWMWA_TEXT_COLOR = 36
                white = ctypes.c_int(0x00FFFFFF)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(DWMWA_TEXT_COLOR),
                    ctypes.byref(white),
                    ctypes.sizeof(white)
                )
            except Exception:
                pass  # Silently fail on older Windows / non-Windows

        def _apply_title_bar_color():
            """Must be called after window is fully created."""
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                if not hwnd:
                    hwnd = self.root.winfo_id()
                _set_title_bar_color(hwnd)
            except Exception:
                pass

        # Apply title bar color after window renders
        self.root.after(100, _apply_title_bar_color)
        self.root.after(500, _apply_title_bar_color)  # retry in case first call is too early

        # Colour palette — Clean White + Royal Blue
        BLUE     = '#1464f4'
        BLUE_DK  = '#0e50d0'
        BG       = '#f0f4f9'
        WHITE    = '#ffffff'
        LINE     = '#dfe6ee'
        INK      = '#17202a'
        MUTED    = '#617080'

        def _fit_height():
            self.root.update_idletasks()
            max_h = self.root.winfo_screenheight() - 60
            cur_h = self.root.winfo_reqheight()
            self.root.geometry(f'600x{min(max(400, cur_h), max_h)}')
        self.root.after(200, _fit_height)

        # ── Footer ────────────────────────────────────────────────────────────
        footer = tk.Frame(self.root, bg=LINE, height=24)
        footer.pack(fill='x', side='bottom'); footer.pack_propagate(False)
        tk.Label(footer, text='For help: rajsys.mtr@gmail.com',
                 bg=LINE, fg=MUTED, font=('Segoe UI', 8)).pack(side='left', padx=10)
        tk.Label(footer, text='Designed by Raj Systems & Technologies',
                 bg=LINE, fg=MUTED, font=('Segoe UI', 8)).pack(side='right', padx=10)

        # ── Button row ────────────────────────────────────────────────────────
        btn_outer = tk.Frame(self.root, bg=WHITE, highlightthickness=1,
                             highlightbackground=LINE)
        btn_outer.pack(fill='x', side='bottom')
        btn_row = tk.Frame(btn_outer, bg=WHITE)
        btn_row.pack(fill='x', padx=10, pady=8)
        self.btn_connect  = self._btn(btn_row, '🔌  Connect',   self._connect,      'light')
        self.btn_connect.pack(side='left', padx=(0,6))
        self.btn_sync_all = self._btn(btn_row, '▶  Sync All',   self._sync_all,    'primary')
        self.btn_sync_all.pack(side='left')
        self.btn_stop     = self._btn(btn_row, '⏹  Stop',       self._stop,        'danger')
        self.btn_stop.pack(side='left', padx=(6,0))
        self.btn_stop.config(state='disabled')
        self.btn_pause    = self._btn(btn_row, '⏸  Pause',      self._toggle_pause,'light')
        self.btn_pause.pack(side='left', padx=(6,0))
        self.btn_settings = self._btn(btn_row, '⚙  Settings',   self._open_settings,'light')
        self.btn_settings.pack(side='right')

        # ── Countdown strip ───────────────────────────────────────────────────
        self.lbl_next = tk.Frame(self.root, bg=BG, height=22)
        self.lbl_next_lbl = tk.Label(self.lbl_next, text='', bg=BG,
                                      fg=MUTED, font=('Segoe UI', 8))
        self.lbl_next_lbl.pack(pady=3)
        self.lbl_next.pack(fill='x', side='bottom')

        # ── Royal Blue Header ─────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=BLUE, height=62)
        hdr.pack(fill='x'); hdr.pack_propagate(False)
        self._logo_img = load_logo_image(size=(38, 38))
        # Load logo at larger size for header
        self._logo_hdr_img = load_logo_image(size=(48, 48))
        if self._logo_hdr_img:
            lbl_logo = tk.Label(hdr, image=self._logo_hdr_img, bg=BLUE, bd=0)
            lbl_logo.pack(side='left', padx=(12, 8), pady=7)
            lbl_logo.image = self._logo_hdr_img  # keep reference
        else:
            # Fallback text badge
            tk.Label(hdr, text='BV', bg=WHITE, fg=BLUE,
                     font=('Segoe UI', 14, 'bold'), width=3,
                     relief='flat').pack(side='left', padx=(12, 8), pady=7)
        tk.Label(hdr, text='BizView Pro', bg=BLUE, fg=WHITE,
                 font=('Segoe UI', 16, 'bold')).pack(side='left', pady=14)
        tk.Label(hdr, text='Agent v4.0', bg=BLUE, fg='#b3d0ff',
                 font=('Segoe UI', 10)).pack(side='right', padx=18)

        # ── Status bar (white strip below blue header) ────────────────────────
        sb = tk.Frame(self.root, bg='#eef4ff', height=34,
                      highlightthickness=1, highlightbackground='#c7d9fe')
        sb.pack(fill='x'); sb.pack_propagate(False)
        self.dot = tk.Label(sb, text='●', bg='#eef4ff', fg='#94a3b8',
                            font=('Segoe UI', 12))
        self.dot.pack(side='left', padx=(14, 4), pady=8)
        self.lbl_status = tk.Label(sb, text='Connecting…', bg='#eef4ff', fg=MUTED,
                                    font=('Segoe UI', 10))
        self.lbl_status.pack(side='left')
        # "Open Tally" button — hidden by default, shown only when Tally isn't
        # reachable so the user can launch it with one click.
        self.btn_open_tally = tk.Button(sb, text='🚀  Open Tally', command=self._open_tally,
                                         bg=BLUE, fg='white', font=('Segoe UI', 8, 'bold'),
                                         relief='flat', cursor='hand2', padx=8, pady=2, bd=0,
                                         activebackground=BLUE_DK, activeforeground='white')
        # not packed here — shown/hidden via _show_open_tally_button()

        # ── Company card (white, rounded border) ──────────────────────────────
        card = tk.Frame(self.root, bg=WHITE, highlightthickness=1,
                        highlightbackground=LINE)
        card.pack(fill='both', expand=True, padx=14, pady=(10, 4))

        card_hdr = tk.Frame(card, bg='#f5f8ff', height=34)
        card_hdr.pack(fill='x'); card_hdr.pack_propagate(False)
        # Blue left accent bar
        tk.Frame(card_hdr, bg=BLUE, width=4).pack(side='left', fill='y')
        tk.Label(card_hdr, text='  Tally Companies', bg='#f5f8ff', fg=INK,
                 font=('Segoe UI', 10, 'bold')).pack(side='left', padx=8, pady=8)
        self.lbl_sync_status = tk.Label(card_hdr, text='', bg='#f5f8ff', fg=BLUE,
                                         font=('Segoe UI', 9))
        self.lbl_sync_status.pack(side='right', padx=12)

        scroll_container = tk.Frame(card, bg=WHITE)
        scroll_container.pack(fill='both', expand=True)
        self._co_canvas = tk.Canvas(scroll_container, bg=WHITE,
                                     highlightthickness=0, bd=0)
        self._co_scrollbar = tk.Scrollbar(scroll_container, orient='vertical',
                                           command=self._co_canvas.yview)
        self._co_canvas.configure(yscrollcommand=self._co_scrollbar.set)
        self._co_scrollbar.pack(side='right', fill='y')
        self._co_canvas.pack(side='left', fill='both', expand=True)
        self.co_frame = tk.Frame(self._co_canvas, bg=WHITE)
        self._co_frame_id = self._co_canvas.create_window((0, 0),
                                                            window=self.co_frame,
                                                            anchor='nw')

        def _on_co_frame_resize(e):
            self._co_canvas.configure(scrollregion=self._co_canvas.bbox('all'))
            self._co_canvas.itemconfig(self._co_frame_id,
                                        width=self._co_canvas.winfo_width())
            self.root.after(50, _fit_height)
        self.co_frame.bind('<Configure>', _on_co_frame_resize)
        self._co_canvas.bind('<Configure>',
            lambda e: self._co_canvas.itemconfig(self._co_frame_id, width=e.width))

        def _on_mousewheel(e):
            self._co_canvas.yview_scroll(int(-1*(e.delta/120)), 'units')
        self._co_canvas.bind_all('<MouseWheel>', _on_mousewheel)

        tk.Label(self.co_frame,
            text='Click "Connect" to detect open Tally companies.',
            bg=WHITE, fg='#94a3b8', font=('Segoe UI', 10), pady=14).pack()

        self.co_progress = {}

    def _card(self, parent, title, key):
        f=tk.Frame(parent,bg='white',bd=1,relief='flat',highlightthickness=1,
                   highlightbackground='#e5e7eb'); f.pack(fill='x',pady=(0,8))
        h=tk.Frame(f,bg='#f8fafc',height=32); h.pack(fill='x'); h.pack_propagate(False)
        tk.Label(h,text=title,bg='#f8fafc',fg='#111827',
                 font=('Segoe UI',10,'bold')).pack(side='left',padx=12,pady=6)
        body=tk.Frame(f,bg='white'); body.pack(fill='x')
        setattr(self, key+'_body', body)

    def _btn(self, parent, text, cmd, style='light'):
        colors={'primary':('#1464f4','white','#0d4db8'),
                'light':('#f3f4f6','#374151','#e5e7eb'),
                'danger':('#fef2f2','#b91c1c','#fee2e2')}
        bg,fg,hv = colors.get(style,colors['light'])
        b=tk.Button(parent,text=text,command=cmd,bg=bg,fg=fg,
                    font=('Segoe UI',9,'bold'),relief='flat',cursor='hand2',
                    padx=10,pady=6,bd=0)
        b.bind('<Enter>',lambda e:b.config(bg=hv))
        b.bind('<Leave>',lambda e:b.config(bg=bg))
        return b

    # ── COUNTDOWN ────────────────────────────────────────────────────────────

    def _tick(self):
        if self.paused:
            self.lbl_next_lbl.config(text='Auto-sync paused ⏸')
            self.root.after(1000, self._tick)
            return
        rem = int(self._next_sync - time.time())
        if rem <= 0:
            if not self.syncing:
                threading.Thread(target=self._do_sync_all_thread, daemon=True).start()
            self._next_sync = time.time() + self._interval_secs()
            rem = self._interval_secs()
        # ── Refresh open companies from Tally 10 seconds before auto-sync ────
        # This ensures the UI and skip-logic know which companies are currently
        # open, avoiding Tally "No Company" exceptions on auto-sync.
        elif rem == 10 and not self.syncing:
            threading.Thread(target=self._refresh_tally_companies, daemon=True).start()
        m, s = divmod(rem, 60)
        self.lbl_next_lbl.config(text=f'Next auto-sync in {m:02d}:{s:02d}')
        self.root.after(1000, self._tick)

    def _refresh_tally_companies(self):
        """Re-fetch open companies from Tally and update self.companies. Runs
        every 30s (see _start_auto_refresh) and also 10s before auto-sync.

        This is also what detects TallyPrime being opened (or closed) after
        the app started — if we were previously disconnected and Tally is now
        reachable, it runs the full connect/portal-discovery flow so company
        list appears automatically with no user action needed. If Tally
        becomes unreachable, it clears the (now stale) company list so closed
        companies aren't shown as available.
        """
        try:
            def _gcfg(k, default=''):
                return self.cfg.get('agent', k).strip() if self.cfg.has_option('agent', k) else default
            host = _gcfg('tally_host', 'http://localhost:9000')
            xml   = fetch_companies(host)
            fresh = parse_companies(xml)
            if fresh is not None:
                was_disconnected = not self._tally_connected
                self._tally_connected = True

                if was_disconnected or not self.assigned:
                    # Tally just became reachable (or we never finished the
                    # portal handshake, e.g. first launch happened while
                    # Tally was closed) — run the full connect flow so the
                    # portal-assigned company list loads automatically.
                    self.log_append('TallyPrime detected — connecting…', 'ok')
                    self._show_open_tally_button(False)
                    self._do_connect()   # runs synchronously on this bg thread
                    return

                old_names = {c['name'] for c in self.companies}
                new_names = {c['name'] for c in fresh}
                self.companies = fresh
                opened  = new_names - old_names
                closed  = old_names - new_names
                for n in opened:
                    self.log_append(f'Tally: "{n}" opened.', 'ok')
                for n in closed:
                    self.log_append(f'Tally: "{n}" closed — will be skipped in next sync.', 'warn')
                # Re-render on any change (open/close) to update status + hint
                if opened or closed:
                    self.root.after(0, self._render_companies)
        except Exception as e:
            # Tally unreachable — clear stale company list (don't show companies
            # that may no longer actually be open) and show the friendly status
            # + Open Tally button instead of leaving old data on screen.
            if self._tally_connected or self.companies:
                self._tally_connected = False
                self.companies = []
                self.root.after(0, self._render_companies)
                if _is_tally_unreachable_error(e):
                    self._set_tally_not_open_status()
                else:
                    self._show_open_tally_button(True)

    def _start_auto_refresh(self):
        """Start periodic auto-refresh of company open/close status.
        Runs every 30 seconds, forever, regardless of whether Tally/portal are
        currently reachable — this is what detects TallyPrime being opened
        later and loads its companies automatically. Skipped during active sync.
        Also does an immediate first refresh after 5 seconds. Guarded so it
        only ever starts one loop even if called more than once.
        """
        if getattr(self, '_auto_refresh_started', False):
            return
        self._auto_refresh_started = True
        def _tick_refresh():
            if not self.syncing:
                try:
                    threading.Thread(target=self._refresh_tally_companies, daemon=True).start()
                except Exception:
                    pass
            self.root.after(30000, _tick_refresh)  # repeat every 30 seconds
        self.root.after(5000,  _tick_refresh)  # first run after 5s

    def _toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            # Save how much time was left on the countdown
            self._pause_remaining = max(0, int(self._next_sync - time.time()))
            self.btn_pause.config(text='▶  Resume')
            self.log_append('Auto-sync paused. Click Resume to continue.', 'warn')
        else:
            # Resume: restore the remaining time so countdown picks up where it left off
            self.paused = False
            self._next_sync = time.time() + (self._pause_remaining if self._pause_remaining > 0 else self._interval_secs())
            self._pause_remaining = 0
            self.btn_pause.config(text='⏸  Pause')
            self.log_append('Auto-sync resumed.', 'ok')

    def _on_interval_change(self, event=None):
        val = self.interval_var.get().strip()
        try:
            mins = int(val)
            if not self.cfg.has_section('agent'):
                self.cfg.add_section('agent')
            self.cfg.set('agent', 'interval_min', str(mins))
            save_cfg(self.cfg)
            self._next_sync = time.time() + self._interval_secs()
            self.log_append('Sync interval set to ' + str(mins) + ' minutes.', 'ok')
        except ValueError:
            pass

    # ── SETTINGS WINDOW ──────────────────────────────────────────────────────

    def _open_settings(self):
        if self._settings_win and tk.Toplevel.winfo_exists(self._settings_win):
            self._settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title('BizView Pro — Settings')
        win.geometry('460x580')
        win.resizable(False, False)
        win.configure(bg='#f0f4f8')
        win.grab_set()
        self._settings_win = win

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(win, bg='#1464f4', height=50)
        hdr.pack(fill='x'); hdr.pack_propagate(False)
        tk.Label(hdr, text='⚙  Agent Settings', bg='#1464f4', fg='white',
                 font=('Segoe UI', 13, 'bold')).pack(side='left', padx=14, pady=10)
        # Close button in header
        tk.Button(hdr, text='✕', bg='#1464f4', fg='white',
                  font=('Segoe UI', 12), relief='flat', bd=0, cursor='hand2',
                  activebackground='#0e4fbf', activeforeground='white',
                  command=win.destroy).pack(side='right', padx=10, pady=8)

        body = tk.Frame(win, bg='#f0f4f8', padx=16, pady=12)
        body.pack(fill='both', expand=True)

        tk.Label(body,
            text='Credentials are provided by your BizView Pro admin. Do not share them.',
            bg='#f0f4f8', fg='#6b7280', font=('Segoe UI', 9), wraplength=400,
            justify='left').pack(anchor='w', pady=(0, 10))

        fields = [
            ('Master Key',    'master_key',    ''),
            ('Master Secret', 'master_secret', ''),
            ('Tally URL',     'tally_host',    'http://localhost:9000'),
            ('Server URL',    'server_url',    'http://bizviewpro.in/tallysync/api/ingest.php'),
        ]
        svars = {}
        for label, key, placeholder in fields:
            row = tk.Frame(body, bg='#f0f4f8')
            row.pack(fill='x', pady=3)
            tk.Label(row, text=label, bg='#f0f4f8', fg='#374151',
                     font=('Segoe UI', 9, 'bold'), width=13, anchor='e').pack(side='left', padx=(0, 8))
            cur = self.cfg.get('agent', key).strip() if self.cfg.has_option('agent', key) else ''
            var = tk.StringVar(value=cur)
            show = '*' if key in ('master_key', 'master_secret') else ''
            ent = tk.Entry(row, textvariable=var, font=('Segoe UI', 10),
                           show=show, bg='white', relief='flat', bd=0,
                           highlightthickness=1, highlightbackground='#d1d5db',
                           highlightcolor='#1464f4')
            ent.pack(side='left', fill='x', expand=True, ipady=5)
            svars[key] = var

        # ── Sync interval ─────────────────────────────────────────────────────
        irow = tk.Frame(body, bg='#f0f4f8')
        irow.pack(fill='x', pady=3)
        tk.Label(irow, text='Sync Interval', bg='#f0f4f8', fg='#374151',
                 font=('Segoe UI', 9, 'bold'), width=13, anchor='e').pack(side='left', padx=(0, 8))
        _iv = self.cfg.get('agent', 'interval_min').strip() if self.cfg.has_option('agent', 'interval_min') else '5'
        self.interval_var = tk.StringVar(value=_iv)
        interval_cb = ttk.Combobox(irow, textvariable=self.interval_var,
                                    values=['5', '10', '20', '30', '45', '60'],
                                    width=6, state='readonly', font=('Segoe UI', 10))
        interval_cb.pack(side='left')
        interval_cb.bind('<<ComboboxSelected>>', self._on_interval_change)
        tk.Label(irow, text='minutes', bg='#f0f4f8', fg='#6b7280',
                 font=('Segoe UI', 9)).pack(side='left', padx=(6, 0))

        # ── Start with Windows ────────────────────────────────────────────────
        srow = tk.Frame(body, bg='#f0f4f8')
        srow.pack(fill='x', pady=(8, 3))
        autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        def _toggle_autostart():
            ok, err = set_autostart(autostart_var.get())
            if ok:
                state = 'enabled' if autostart_var.get() else 'disabled'
                lbl_ok.config(text=f'✓  Start with Windows {state}', fg='#059669')
                win.after(2500, lambda: lbl_ok.config(text=''))
                self.log_append(f'Start with Windows {state}.', 'ok')
            else:
                autostart_var.set(not autostart_var.get())  # revert checkbox
                messagebox.showerror('Could not update startup setting', err)
        autostart_chk = tk.Checkbutton(
            srow, text='Start automatically when Windows starts (minimised to system tray)',
            variable=autostart_var, command=_toggle_autostart,
            bg='#f0f4f8', fg='#374151', font=('Segoe UI', 9),
            activebackground='#f0f4f8', selectcolor='white',
            anchor='w', wraplength=400, justify='left')
        autostart_chk.pack(anchor='w')
        if sys.platform != 'win32':
            autostart_chk.config(state='disabled')
            tk.Label(srow, text='(Windows only)', bg='#f0f4f8', fg='#9ca3af',
                     font=('Segoe UI', 8)).pack(anchor='w', padx=(20, 0))

        # ── Action buttons row ────────────────────────────────────────────────
        tk.Frame(body, bg='#e5e7eb', height=1).pack(fill='x', pady=(12, 10))

        btn_row1 = tk.Frame(body, bg='#f0f4f8')
        btn_row1.pack(fill='x', pady=(0, 6))

        def save_settings():
            if not self.cfg.has_section('agent'):
                self.cfg.add_section('agent')
            for key, var in svars.items():
                val = var.get().strip()
                if val:
                    self.cfg.set('agent', key, val)
            interval_val = self.interval_var.get().strip()
            if interval_val:
                self.cfg.set('agent', 'interval_min', interval_val)
                self._next_sync = time.time() + self._interval_secs()
            save_cfg(self.cfg)
            self.cfg = load_cfg()
            lbl_ok.config(text='✓  Settings saved successfully', fg='#059669')
            win.after(2500, lambda: lbl_ok.config(text=''))
            self.log_append('Settings saved.', 'ok')

        # Save button — Royal Blue
        tk.Button(btn_row1, text='💾  Save Settings',
                  bg='#1464f4', fg='white', font=('Segoe UI', 10, 'bold'),
                  relief='flat', cursor='hand2', padx=14, pady=8, bd=0,
                  activebackground='#0e4fbf', activeforeground='white',
                  command=save_settings).pack(side='left')

        # Test Server button — Teal/Green
        lbl_test_result = tk.Label(body, text='', bg='#f0f4f8', font=('Segoe UI', 9))
        def _run_test():
            lbl_test_result.config(text='Testing connection…', fg='#6b7280')
            win.update_idletasks()
            self._test_server(result_label=lbl_test_result)
        tk.Button(btn_row1, text='🔍  Test Connection',
                  bg='#0f766e', fg='white', font=('Segoe UI', 10),
                  relief='flat', cursor='hand2', padx=14, pady=8, bd=0,
                  activebackground='#0d6460', activeforeground='white',
                  command=_run_test).pack(side='left', padx=(8, 0))

        # Open Logs button — Slate
        def _open_logs():
            import subprocess, sys, os
            try:
                if sys.platform == 'win32':
                    os.startfile(str(LOG_FILE))
                else:
                    subprocess.Popen(['xdg-open', str(LOG_FILE)])
            except Exception as ex:
                lbl_ok.config(text=f'Cannot open log: {ex}', fg='#dc2626')

        tk.Button(btn_row1, text='📋  View Logs',
                  bg='#475569', fg='white', font=('Segoe UI', 10),
                  relief='flat', cursor='hand2', padx=14, pady=8, bd=0,
                  activebackground='#334155', activeforeground='white',
                  command=_open_logs).pack(side='left', padx=(8, 0))

        lbl_test_result.pack(anchor='w', pady=(4, 0))
        lbl_ok = tk.Label(body, text='', bg='#f0f4f8', font=('Segoe UI', 9))
        lbl_ok.pack(anchor='w', pady=(2, 0))

        # Log file path
        tk.Label(body,
            text=f'Log: {LOG_FILE}',
            bg='#f0f4f8', fg='#9ca3af', font=('Consolas', 8),
            wraplength=420, justify='left').pack(anchor='w', pady=(10, 0))

    # ── CONNECT ───────────────────────────────────────────────────────────────

    def _auto_connect(self):
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _connect(self):
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        host = self.cfg['agent'].get('tally_host','http://localhost:9000')
        srv  = self.cfg['agent'].get('server_url','').strip()
        mkey = self.cfg['agent'].get('master_key','').strip()
        msec = self.cfg['agent'].get('master_secret','').strip()

        self._set_status('Connecting…', '#fcd34d')
        try:
            xml  = fetch_companies(host)
            cos  = parse_companies(xml)
            if not cos and ('<' in xml):
                # Tally responded but no parsed companies — still connected
                cos = [{'name':'(Company open in Tally)','guid':''}]
            self.companies = cos
            self.log_append(f'TallyPrime: found {len(cos)} compan{"y" if len(cos)==1 else "ies"}', 'ok')
            # Log parsed company details for diagnostics
            for _co in cos:
                _num = _co.get('number','?')
                _path = _co.get('path','')
                _pathshort = _path[-30:] if len(_path) > 30 else _path
                self.log_append(f'  • {_co["name"]}  [#{_num}]  {_pathshort}', 'dim')

            self.assigned = []
            self.pending_setup = False
            self.setup_url = ''
            if srv and mkey and msec and cos and cos[0]['name'] != '(Company open in Tally)':
                # Tell the portal what we see, then fetch what's actually activated
                discover_res = discover_companies_on_server(srv, mkey, msec, cos)
                if discover_res.get('ok'):
                    n = discover_res.get('discovered', 0)
                    m = discover_res.get('matched_existing', 0)
                    seen = discover_res.get('names_seen', [])
                    self.log_append(f'Sent to portal: {seen}', 'dim')
                    self.log_append(f'Portal discover: {m} matched existing, {n} newly registered', 'info')
                else:
                    self.log_append(f'Portal discovery failed: {discover_res.get("error")}', 'warn')

                list_res = list_assigned_companies(srv, mkey, msec)
                if list_res.get('ok'):
                    self.assigned = list_res.get('active', [])
                    self.pending_setup = list_res.get('pending_setup', False)
                    self.setup_url = list_res.get('setup_url','')
                    plan = list_res.get('plan', {})
                    extra = list_res.get('discovered_unselected', 0)
                    extra_names = list_res.get('discovered_names', [])
                    active_names = [a.get('name','') for a in self.assigned]
                    self.log_append(f'Portal active companies: {active_names}', 'dim')
                    self.log_append(
                        f'Portal: {len(self.assigned)}/{plan.get("max_companies","?")} companies active'
                        + (f', {extra} more available: {extra_names}' if extra else ''), 'info')
                    # Store unregistered companies so UI hint can show them
                    self._unregistered_names = extra_names if extra else []
                    self._unregistered_count  = extra if extra else 0
                    self._sync_watermarks_from_server()
                    # Migrate old name-based config keys → new id-based keys
                    self._migrate_config_keys()
                else:
                    self.log_append(f'Could not load company list from portal: {list_res.get("error")}', 'warn')
            elif not (mkey and msec):
                self.log_append('master_key/master_secret not set — add them to config.ini (see Sync Tally page on the portal)', 'warn')

            self._tally_connected = True
            self._show_open_tally_button(False)
            self.root.after(0, self._render_companies)
            active_count = len([a for a in self.assigned]) if self.assigned else len(cos)
            self._set_status(f'TallyPrime Connected  ({active_count} active company found)', '#4ade80')
            self.log_append(f'Connected — {len(cos)} company', 'ok')
            # The 30s auto-refresh loop is already running (started at app launch)
            # and will keep picking up open/close changes from here on.
        except Exception as e:
            self._tally_connected = False
            self.companies = []   # don't show stale companies while Tally is closed
            self.root.after(0, self._render_companies)
            if _is_tally_unreachable_error(e):
                self._set_tally_not_open_status(str(e))
            else:
                self._set_status(f'Not connected: {str(e)[:55]}', '#f87171')
                self._show_open_tally_button(True)
                self.log_append(f'Connect failed: {e}', 'error')

    def _force_full_resync(self, company):
        """Clear local watermarks for this company and trigger a full resync."""
        cid  = str(company.get('company_id',''))
        name = company.get('name','')
        cnum = company.get('tally_number','')
        label = f"{name} [{cnum}]" if cnum else name

        if not messagebox.askyesno(
            'Force Full Resync',
            f'Clear all local sync watermarks for:\n\n  {label}\n\n'
            f'This will re-send ALL ledgers, stock items and vouchers to the\n'
            f'portal on the next sync. Use this after resetting server data.\n\n'
            f'Continue?', icon='warning'):
            return

        changed = False
        if not self.cfg.has_section('agent'):
            self.cfg.add_section('agent')
        for prefix in ('last_voucher_alterid__', 'last_master_alterid__',
                       'ledger_hash__', 'stock_hash__'):
            key_ = f'{prefix}id{cid}'
            self.cfg.set('agent', key_, '0')   # set even if not exists yet
            changed = True
        # Set force-resync flag so _sync_watermarks_from_server doesn't re-seed
        # from the portal's old value (which is still high until full resync completes)
        self.cfg.set('agent', f'force_resync__id{cid}', '1')

        if changed:
            save_cfg(self.cfg)
            self.log_append(
                f'Watermarks cleared for "{label}" — next sync will be a FULL resync.', 'ok')
            messagebox.showinfo(
                'Watermarks Cleared',
                f'Full resync scheduled for:\n  {label}\n\n'
                f'Click Sync Now to start, or wait for auto-sync.')
            # Re-render so UI reflects cleared state
            self.root.after(100, self._render_companies)
        else:
            self.log_append(f'No watermarks found for "{label}" — already reset.', 'info')

    def _migrate_config_keys(self):
        """
        Migrate old name-based config keys to new id-based keys.
        Old: last_voucher_alterid__s.s. electricals (from 1-apr-26) = 83675
        New: last_voucher_alterid__id11 = 83675
        Runs once after connect so sync continues from correct watermarks.
        """
        import re as _re
        changed = False
        for a in self.assigned:
            cid  = str(a.get('company_id', ''))
            name = a.get('name', '').lower()
            if not cid or not name:
                continue
            for prefix in ('last_voucher_alterid__', 'last_master_alterid__',
                           'ledger_hash__', 'stock_hash__', 'co_paused__'):
                new_key = f'{prefix}id{cid}'
                # Build candidate old-style key variants
                old_keys = [
                    prefix + name,                                           # space-based
                    prefix + _re.sub(r'[\s/]', '_', name),                 # underscored
                    prefix + _re.sub(r'[^a-z0-9]', '_', name),             # fully sanitized
                    prefix + _re.sub(r'[^a-z0-9_.\s()/\-]', '_', name),  # partial
                ]
                if self.cfg.has_option('agent', new_key) and                    self.cfg.get('agent', new_key).strip():
                    continue  # already set, skip
                for old_key in old_keys:
                    if self.cfg.has_option('agent', old_key):
                        val = self.cfg.get('agent', old_key).strip()
                        if val:
                            self.cfg.set('agent', new_key, val)
                            changed = True
                            self.log_append(
                                f'Migrated config: {old_key} → {new_key} = {val}', 'dim')
                            break
        if changed:
            try:
                import os
                cfg_path = os.path.join(
                    os.environ.get('APPDATA', ''), 'TallySync', 'config.ini')
                with open(cfg_path, 'w', encoding='utf-8') as cf:
                    self.cfg.write(cf)
            except Exception as ex:
                self.log_append(f'Config save error: {ex}', 'warn')

    def _set_status(self, msg, color):
        self.root.after(0, lambda: self.lbl_status.config(text=msg, fg=color, bg='#eef4ff'))
        self.root.after(0, lambda: self.dot.config(fg=color, bg='#eef4ff'))

    def _show_open_tally_button(self, show):
        def _apply():
            if show:
                self.btn_open_tally.pack(side='left', padx=(10, 0))
            else:
                self.btn_open_tally.pack_forget()
        self.root.after(0, _apply)

    def _set_tally_not_open_status(self, raw_error=''):
        """Friendly status shown when TallyPrime isn't running — replaces the
        raw urllib/WinError text with a plain-language message + a one-click
        'Open Tally' button, instead of a scary red stack-trace-looking string."""
        self._set_status('TallyPrime is not open yet', '#f87171')
        self._show_open_tally_button(True)
        if raw_error:
            self.log_append(f'Connect failed (Tally not open): {raw_error}', 'warn')

    def _open_tally(self):
        """Try to launch TallyPrime from the 'Open Tally' button."""
        def _launch():
            self.root.after(0, lambda: self._set_status('Starting TallyPrime…', '#fcd34d'))
            path = find_tally_exe_path()
            if path:
                try:
                    if sys.platform == 'win32':
                        os.startfile(path)
                    self.log_append(f'Launching TallyPrime: {path}', 'ok')
                    self.log_append('Waiting for TallyPrime to start — companies will load automatically.', 'info')
                except Exception as e:
                    self.log_append(f'Could not launch TallyPrime: {e}', 'error')
                    self.root.after(0, lambda: messagebox.showerror(
                        'Could not start TallyPrime', str(e)))
                    self.root.after(0, lambda: self._set_tally_not_open_status())
            else:
                self.root.after(0, lambda: messagebox.showwarning(
                    'TallyPrime not found',
                    "Couldn't find TallyPrime automatically on this PC.\n\n"
                    "Please open TallyPrime manually — the agent checks every "
                    "30 seconds and will detect it and load companies automatically."))
                self.root.after(0, lambda: self._set_tally_not_open_status())
        threading.Thread(target=_launch, daemon=True).start()

    def _sync_watermarks_from_server(self):
        """Pull each active company's last_voucher_alterid from portal and sync
        to local config using company_id-based keys (e.g. last_voucher_alterid__id29).
        Never moves a watermark backwards (protects Force Full Resync = 0).
        """
        changed = False
        most_recent = None
        for a in self.assigned:
            name = a.get('name', '')
            cid  = str(a.get('company_id', ''))
            # ALWAYS use id-based key — prevents name-key vs id-key mismatch
            # that caused Force Full Resync to be overridden on next Connect
            key  = f'last_voucher_alterid__id{cid}' if cid else                    ('last_voucher_alterid__' + re.sub(r'[/\\\s]', '_', name))
            try:
                server_alterid = int(a.get('last_voucher_alterid', 0) or 0)
            except (TypeError, ValueError):
                server_alterid = 0
            local_alterid = 0
            if self.cfg.has_option('agent', key):
                try:
                    local_alterid = int(self.cfg.get('agent', key).strip() or '0')
                except ValueError:
                    local_alterid = 0
            if server_alterid > local_alterid:
                # Server has higher watermark — seed local (e.g. after reinstall)
                if not self.cfg.has_section('agent'):
                    self.cfg.add_section('agent')
                self.cfg.set('agent', key, str(server_alterid))
                changed = True
                self.log_append(
                    f'"{name}": seeding local watermark from portal: {server_alterid}', 'info')
            elif server_alterid == 0 and local_alterid > 0:
                # Server reset to 0 — clear local so next sync is full resync
                if not self.cfg.has_section('agent'):
                    self.cfg.add_section('agent')
                self.cfg.set('agent', key, '0')
                changed = True
                self.log_append(
                    f'"{name}": server AlterID=0 — local watermark cleared, full resync on next sync.', 'warn')
            # If local == 0 (Force Full Resync was used) and server > 0,
            # DO NOT seed from server — respect the forced reset
            # (condition above: server > local already handles the seed case correctly
            #  since 71906 > 0 would seed, BUT after Force Full Resync local=0
            #  and server=71906 — this WOULD incorrectly seed! Fix: check if
            #  force_resync flag is set for this company)
            # The correct logic: if local was explicitly set to 0 by Force Full Resync,
            # server should NOT override it. We detect this by checking if a special
            # force-resync flag is stored.
            elif server_alterid > 0 and local_alterid == 0:
                flag_key = f'force_resync__id{cid}'
                if self.cfg.has_option('agent', flag_key) and                    self.cfg.get('agent', flag_key).strip() == '1':
                    # Force resync was requested — keep local at 0, ignore server value
                    self.log_append(
                        f'"{name}": force resync active — keeping watermark at 0, ignoring server value {server_alterid}', 'info')
                else:
                    # Normal case: seed from server (e.g. reinstall)
                    if not self.cfg.has_section('agent'):
                        self.cfg.add_section('agent')
                    self.cfg.set('agent', key, str(server_alterid))
                    changed = True
                    self.log_append(
                        f'"{name}": seeding watermark from portal: {server_alterid}', 'info')

            last_sync = a.get('last_sync_at')
            if last_sync and (most_recent is None or last_sync > most_recent):
                most_recent = last_sync

            # ── Master (ledger + stock) watermark reconciliation ──────────────
            # The agent keeps ONE combined local watermark for both ledgers and
            # stock (last_master_alterid__id{cid}), while the server tracks them
            # separately (last_ledger_alterid / last_stock_alterid — each master
            # type has its own independent ALTERID sequence in Tally). We seed
            # from whichever of the two is LOWER, never higher — using the
            # higher one could make us skip real changes on the other type.
            mkey = f'last_master_alterid__id{cid}' if cid else 'last_master_alterid'
            try:
                server_ledger = int(a.get('last_ledger_alterid', 0) or 0)
            except (TypeError, ValueError):
                server_ledger = 0
            try:
                server_stock = int(a.get('last_stock_alterid', 0) or 0)
            except (TypeError, ValueError):
                server_stock = 0
            server_master = min(server_ledger, server_stock) if (server_ledger and server_stock) \
                else max(server_ledger, server_stock)
            local_master = 0
            if self.cfg.has_option('agent', mkey):
                try:
                    local_master = int(self.cfg.get('agent', mkey).strip() or '0')
                except ValueError:
                    local_master = 0
            if server_master > local_master:
                if not self.cfg.has_section('agent'):
                    self.cfg.add_section('agent')
                self.cfg.set('agent', mkey, str(server_master))
                changed = True
                self.log_append(
                    f'"{name}": seeding master watermark from portal: {server_master}', 'info')
            elif server_master == 0 and local_master > 0:
                if not self.cfg.has_section('agent'):
                    self.cfg.add_section('agent')
                self.cfg.set('agent', mkey, '0')
                changed = True
                self.log_append(
                    f'"{name}": server master AlterID=0 — local watermark cleared, full resync on next sync.', 'warn')

        if changed:
            save_cfg(self.cfg)

        if most_recent:
            try:
                dt = datetime.strptime(most_recent, '%Y-%m-%d %H:%M:%S')
                pretty = dt.strftime('%d %b %Y, %I:%M %p')
            except ValueError:
                pretty = most_recent
            self.root.after(0, lambda t=pretty: self.lbl_last.config(text=f'Last sync: {t}', fg='#4ade80'))

    def _rebuild_company_rows(self):
        """Alias for _render_companies — called after detecting Tally company open/close."""
        self._render_companies()

    def _render_companies(self):
        for w in self.co_frame.winfo_children():
            w.destroy()
        self.co_progress = {}

        # ── Open-company check ──────────────────────────────────────────────
        # PRIMARY KEY: tally_number (derived from data folder path).
        #   Each company file lives in a unique numbered folder — 100002 vs 100010.
        #   If we have the number, it's 100% unambiguous.
        # FALLBACK: name — Tally cannot open two same-name companies simultaneously.
        tally_by_number  = {co['number']: co for co in self.companies if co.get('number','')}
        tally_open_names = {co['name'] for co in self.companies}

        def _co_is_open(a):
            portal_num = a.get('tally_number','')
            # If portal has a stored number AND Tally reported numbers — use number match
            if portal_num and tally_by_number:
                return portal_num in tally_by_number
            # Fallback: name match (when number not yet stored or Tally didn't return it)
            return a.get('name','') in tally_open_names

        if len(self.assigned) > 1:
            self.btn_sync_all.config(text='▶  Sync All')
        else:
            self.btn_sync_all.config(text='▶  Sync Now')

        if self.assigned:
            # Sort: open companies (in Tally right now) first, closed ones below
            self.assigned = sorted(
                self.assigned,
                key=lambda a: (0 if _co_is_open(a) else 1)
            )
            for a in self.assigned:
                cname      = a['name']
                num        = a.get('tally_number','')
                # Show [number] suffix whenever we have it (makes same-name companies clear)
                if num:
                    cname = f'{cname}  [{num}]'
                is_open    = _co_is_open(a)

                # ── Separator ────────────────────────────────────────────────
                sep = tk.Frame(self.co_frame, bg='#f3f4f6', height=1)
                sep.pack(fill='x', padx=10)

                # ── Main company row ─────────────────────────────────────────
                row = tk.Frame(self.co_frame, bg='white')
                row.pack(fill='x', padx=10, pady=(6, 0))

                tk.Label(row, text='🏢', bg='white',
                         font=('Segoe UI', 12)).pack(side='left', padx=(0, 8))

                inf = tk.Frame(row, bg='white')
                inf.pack(side='left', fill='x', expand=True)

                # Company name — grey out if not open in Tally
                name_color = '#111827' if is_open else '#9ca3af'
                tk.Label(inf, text=cname, bg='white', fg=name_color,
                         font=('Segoe UI', 10, 'bold'), anchor='w').pack(anchor='w')

                # Status sub-label
                if not is_open:
                    tk.Label(inf, text='⚠  Not open in TallyPrime — will be skipped during sync',
                             bg='white', fg='#f59e0b',
                             font=('Segoe UI', 8), anchor='w').pack(anchor='w')

                # Last-sync label (reads from config.ini or portal data)
                last_sync_text = 'Never synced'
                last_sync_at = a.get('last_sync_at')
                if last_sync_at:
                    try:
                        from datetime import datetime as _dt
                        # The portal's DB session is IST (see db() in config.php),
                        # so last_sync_at already comes back in IST — no timezone
                        # shift needed here anymore.
                        dt_ist = _dt.strptime(last_sync_at, '%Y-%m-%d %H:%M:%S')
                        last_sync_text = dt_ist.strftime('%d %b %Y, %I:%M %p') + ' IST'
                    except Exception:
                        last_sync_text = last_sync_at
                lbl_ls = tk.Label(inf, text=f'Last sync: {last_sync_text}',
                         bg='white', fg='#6b7280',
                         font=('Segoe UI', 8), anchor='w')
                lbl_ls.pack(anchor='w')

                # Per-company Sync Now + per-company auto-sync pause toggle
                co      = dict(a)
                btn_clr = 'primary' if is_open else 'light'

                # ── Per-company pause state (stored in config.ini) ────────────
                _co_id_str   = str(a.get('company_id',''))
                co_pause_key = f'co_paused__id{_co_id_str}' if _co_id_str else f'co_paused__{re.sub(chr(47), "_", a["name"])}'
                co_is_paused = (
                    self.cfg.has_option('agent', co_pause_key) and
                    self.cfg.get('agent', co_pause_key).strip() == '1'
                )

                def _make_co_pause_toggle(cname_=cname, key_=co_pause_key):
                    """Return a callback that toggles per-company auto-sync pause."""
                    def _toggle():
                        cur = (self.cfg.has_option('agent', key_) and
                               self.cfg.get('agent', key_).strip() == '1')
                        new_val = '0' if cur else '1'
                        if not self.cfg.has_section('agent'):
                            self.cfg.add_section('agent')
                        self.cfg.set('agent', key_, new_val)
                        save_cfg(self.cfg)
                        action = 'paused' if new_val == '1' else 'resumed'
                        self.log_append(
                            f'Auto-sync for "{cname_}" {action}. '
                            + ('Manual sync still works.' if new_val == '1' else ''), 'info')
                        # Rebuild UI to reflect new button state
                        self.root.after(0, self._render_companies)
                    return _toggle

                # ── Right-click context menu ─────────────────────────────────
                def _make_context_menu(co_=co, cname_=cname, is_open_=is_open):
                    menu = tk.Menu(self.root, tearoff=0)
                    if is_open_:
                        menu.add_command(
                            label='▶  Sync Now (Incremental)',
                            command=lambda: threading.Thread(
                                target=self._do_sync_one, kwargs={'company': co_},
                                daemon=True).start())
                        menu.add_separator()
                        menu.add_command(
                            label='⟳  Force Full Resync (clear watermark)',
                            command=lambda: self._force_full_resync(co_))
                    else:
                        menu.add_command(
                            label=f'Company not open in Tally', state='disabled')
                    return menu
                def _show_ctx(event, co_=co, cname_=cname, is_open_=is_open):
                    _make_context_menu(co_, cname_, is_open_).tk_popup(event.x_root, event.y_root)
                # Bind right-click to the row AND all its child widgets
                # (child widgets consume click events and don't bubble up in tkinter)
                def _bind_ctx_recursive(widget, fn):
                    widget.bind('<Button-3>', fn)
                    for child in widget.winfo_children():
                        _bind_ctx_recursive(child, fn)
                # Schedule after widget is fully built
                row.after(50, lambda r=row, fn=_show_ctx: _bind_ctx_recursive(r, fn))

                # Sync Now button (right side)
                sync_btn = self._btn(row, '▶ Sync Now',
                    (lambda co=co: threading.Thread(
                        target=self._do_sync_one, kwargs={'company': co}, daemon=True
                    ).start()) if is_open else lambda: messagebox.showwarning(
                        'Company closed',
                        f'"{cname}" is not currently open in TallyPrime.\n'
                        'Please open the company in Tally and try again.'),
                    btn_clr)
                sync_btn.pack(side='right', padx=(0, 4))

                # Pause/resume icon button — immediately LEFT of Sync Now
                pause_icon  = '⏸' if not co_is_paused else '▶'
                pause_color = 'light' if not co_is_paused else 'warn'
                pause_btn   = self._btn(row, pause_icon, _make_co_pause_toggle(), pause_color)
                pause_btn.config(width=3)
                pause_btn.pack(side='right', padx=(0, 2))

                # Paused indicator under company name
                if co_is_paused:
                    tk.Label(inf, text='⏸  Auto-sync paused',
                             bg='white', fg='#f59e0b',
                             font=('Segoe UI', 8), anchor='w').pack(anchor='w')

                if not is_open:
                    sync_btn.config(state='disabled')

                # ── Thin progress bar ─────────────────────────────────────────
                bar_frame = tk.Frame(self.co_frame, bg='white')
                bar_frame.pack(fill='x', padx=18, pady=(3, 8))
                BAR_W, BAR_H = 240, 5
                canvas = tk.Canvas(bar_frame, width=BAR_W, height=BAR_H,
                                   bg='#e5e7eb', highlightthickness=0, bd=0)
                canvas.pack(side='left')
                status_var = tk.StringVar(value='Idle' if is_open else 'Skipped — not open in Tally')
                tk.Label(bar_frame, textvariable=status_var,
                         bg='white', fg='#9ca3af',
                         font=('Segoe UI', 8)).pack(side='left', padx=(8, 0))
                fill_id = canvas.create_rectangle(0, 0, 0, BAR_H, fill='#1464f4', outline='')
                _prog_entry = {
                    'canvas': canvas,
                    'fill':   fill_id,
                    'width':  BAR_W,
                    'status': status_var,
                    'is_open': is_open,
                    'last_sync_lbl': lbl_ls,
                }
                # Key by company_id (unique) — prevents same-name companies sharing a bar
                _co_id = str(a.get('company_id', '')) or cname
                self.co_progress[_co_id] = _prog_entry     # primary key used by _co_progress
                self.co_progress[cname]  = _prog_entry     # display key for reference

        # ── Warning note for unselected/extra companies ───────────────────────
        if self.pending_setup or (self.assigned and len(self.companies) > len(self.assigned)):
            extra = max(0, len(self.companies) - len(self.assigned))
            note  = tk.Frame(self.co_frame, bg='#fff7ed')
            note.pack(fill='x', padx=10, pady=(8, 4))
            msg = ('Select which companies to sync — visit My Companies on the portal.'
                   if self.pending_setup else
                   f'{extra} more compan{"y" if extra==1 else "ies"} found in Tally — '
                   f'add them on the portal (My Companies) if your plan allows.')
            tk.Label(note, text='⚠ ' + msg, bg='#fff7ed', fg='#b45309',
                     font=('Segoe UI', 9), wraplength=520,
                     justify='left').pack(anchor='w', padx=8, pady=(6,2))
            def _get_portal_url(self=self):
                url = getattr(self, 'setup_url', '') or ''
                if not url and self.cfg.has_option('agent','server_url'):
                    url = self.cfg.get('agent','server_url').replace('/api/ingest.php','')
                return (url.rstrip('/') + '/my-companies.php') if url else ''
            portal_url = _get_portal_url()
            if portal_url:
                link = tk.Label(note, text='→ Open My Companies on portal',
                                bg='#fff7ed', fg='#1464f4',
                                font=('Segoe UI', 9, 'underline'), cursor='hand2')
                link.pack(anchor='w', padx=8, pady=(0, 6))
                link.bind('<Button-1>',
                          lambda e, u=portal_url: __import__('webbrowser').open(u))

        if not self.assigned and not self.pending_setup and not self.companies:
            if not self._tally_connected:
                ph = tk.Frame(self.co_frame, bg='white')
                ph.pack(fill='x', padx=10, pady=14)
                tk.Label(ph, text='⏳  TallyPrime is not open yet',
                         bg='white', fg='#6b7280',
                         font=('Segoe UI', 10, 'bold')).pack(anchor='w')
                tk.Label(ph, text='Open TallyPrime and this list will fill in automatically —\n'
                                   'no need to click Connect.',
                         bg='white', fg='#9ca3af', font=('Segoe UI', 9),
                         justify='left').pack(anchor='w', pady=(2, 8))
                self._btn(ph, '🚀  Open Tally', self._open_tally, 'primary').pack(anchor='w')
            else:
                tk.Label(self.co_frame,
                         text='Click Connect to detect companies from TallyPrime.',
                         bg='white', fg='#9ca3af',
                         font=('Segoe UI', 9)).pack(anchor='w', padx=10, pady=6)

    # ── SYNC ─────────────────────────────────────────────────────────────────

    def _sync_all(self):
        """Main button — syncs ALL assigned companies sequentially."""
        if self.syncing:
            messagebox.showinfo('Sync running', 'A sync is already in progress.')
            return
        threading.Thread(target=self._do_sync_all_thread, daemon=True).start()

    def _do_sync_all_thread(self):
        """Worker: iterate every assigned company, skip those not open in Tally or paused."""
        if self.syncing: return
        self.syncing   = True
        self.stop_flag = False
        self.root.after(0, lambda: self.btn_sync_all.config(state='disabled'))
        self.root.after(0, lambda: self.btn_stop.config(state='normal'))
        try:
            # Number-first open check (data-path derived number is reliable)
            tally_by_num2    = {co['number']: co for co in self.companies if co.get('number','')}
            tally_open_names2= {co['name'] for co in self.companies}
            def _sa_open(a):
                pn = a.get('tally_number','')
                if pn and tally_by_num2:
                    return pn in tally_by_num2
                return a.get('name','') in tally_open_names2
            syncable = [co for co in self.assigned if _sa_open(co)]
            total       = len(syncable)
            for idx, co in enumerate(syncable, 1):
                if self.stop_flag:
                    self.log_append('Sync All stopped before next company.', 'warn')
                    break
                cname    = co.get('name', '')
                pause_key = 'co_paused__' + re.sub(r'[/\\\s]', '_', cname)
                if (self.cfg.has_option('agent', pause_key) and
                        self.cfg.get('agent', pause_key).strip() == '1'):
                    self.log_append(f'⏸ "{cname}" — auto-sync paused, skipping.', 'dim')
                    continue
                label = f'Syncing {idx} of {total} {"company" if total==1 else "companies"}…'
                self.root.after(0, lambda l=label: self.lbl_sync_status.config(text=l))
                self._sync_one_company(co)
        finally:
            self.root.after(0, lambda: self.lbl_sync_status.config(text=''))
            self._sync_done()

    def _do_sync_one(self, company):
        """Per-row Sync Now button — syncs a single company."""
        if self.syncing:
            messagebox.showinfo('Sync running', 'A sync is already in progress.')
            return
        self.syncing   = True
        self.stop_flag = False
        self.root.after(0, lambda: self.btn_sync_all.config(state='disabled'))
        self.root.after(0, lambda: self.btn_stop.config(state='normal'))
        try:
            self._sync_one_company(company)
        finally:
            self._sync_done()

    def _stop(self):
        if not self.syncing:
            return
        self.stop_flag = True
        self.log_append('Stop requested — will halt after current operation…', 'warn')
        self._progress(0, 'Stopping…')
        self.root.after(0, lambda: self.btn_stop.config(state='disabled', text='⏹  Stopping…'))

    def _sync_one_company(self, company=None):
        """Core sync for a single company. Skips silently if the company
        is not currently open in TallyPrime."""
        def _gcfg(k, default=''):
            return self.cfg.get('agent', k).strip() if self.cfg.has_option('agent', k) else default
        host = _gcfg('tally_host', 'http://localhost:9000')
        srv  = _gcfg('server_url', '')

        company_name = ''
        if company:
            uid          = str(company.get('company_id', ''))
            key          = company.get('api_key', '')
            sec          = company.get('secret_key', '')
            company_name = company.get('name', '')
        elif self.assigned:
            a            = self.assigned[0]
            uid          = str(a.get('company_id', ''))
            key          = a.get('api_key', '')
            sec          = a.get('secret_key', '')
            company_name = a.get('name', '')
        else:
            uid          = _gcfg('user_id', '')
            key          = _gcfg('api_key', '')
            sec          = _gcfg('secret_key', '')

        # Helper: update THIS company's progress bar by company_id (unique per company)
        # Defined FIRST so it can be used in the skip block below too
        def _prog(pct, msg=''):
            self._co_progress(company_name, pct, msg, company_id=uid)

        # ── Skip if this company is not open in TallyPrime right now ───────
        tally_by_num_s     = {co['number']: co for co in self.companies if co.get('number','')}
        tally_open_names_s = {co['name'] for co in self.companies}
        portal_num_s       = company.get('tally_number','') if isinstance(company, dict) else ''
        if portal_num_s and tally_by_num_s:
            this_co_open = portal_num_s in tally_by_num_s
        else:
            this_co_open = company_name in tally_open_names_s
        if company_name and not this_co_open:
            self.log_append(
                f'Skipping "{company_name}" — not currently open in TallyPrime.', 'warn')
            _prog(0, 'Skipped — not open in Tally')
            return

        self.log_append(f'Syncing "{company_name}" #{portal_num_s} (id={uid})', 'info')
        cmp_    = _gcfg('compress', 'true').lower() == 'true'
        enc_    = _gcfg('encrypt',  'true').lower() == 'true'
        company = company_name   # used for Tally XML context

        if not uid or not srv:
            self.log_append('ERROR: Agent not configured. Contact your TallySync admin.','error')
            return

        try:
            # ── 1 & 2. Masters (Ledgers + Stock) — Incremental via AlterID ────
            # Use company_id in the key — prevents same-name companies sharing a watermark
            master_key = f'last_master_alterid__id{uid}' if uid else 'last_master_alterid'
            last_master_alterid = 0
            if self.cfg.has_option('agent', master_key):
                try: last_master_alterid = int(self.cfg.get('agent', master_key).strip() or '0')
                except ValueError: pass

            # Safety net: force a full master resync once a day regardless of the
            # AlterID watermark. The incremental $AlterId filter is reliable for
            # Voucher collections but has been inconsistent for Ledger/StockItem
            # (master) collections on some Tally releases — if it ever silently
            # returns nothing, closing balances would otherwise go stale forever.
            full_master_key = f'last_full_master_sync__id{uid}' if uid else 'last_full_master_sync'
            last_full_master_date = self.cfg.get('agent', full_master_key, fallback='') \
                if self.cfg.has_option('agent', full_master_key) else ''
            today_str = datetime.now().strftime('%Y%m%d')
            force_daily_full_master = (last_full_master_date != today_str)

            is_first_master_sync = (last_master_alterid == 0) or force_daily_full_master
            max_master_alterid   = last_master_alterid

            # ── 1. Ledgers ────────────────────────────────────────────────────
            _prog( 5,
                'Fetching all ledgers (full sync)…' if is_first_master_sync
                else f'Checking ledger changes since AlterID {last_master_alterid}…')
            if is_first_master_sync:
                xml = fetch_ledgers(host, company=company or '')
                extra = {}
            else:
                xml = fetch_ledgers_by_alterid(host, last_master_alterid, company=company or '')
                extra = {'is_incremental': '1'}

            self.log_append(f'Ledgers: {len(xml):,} bytes from Tally', 'dim')
            if '<LINEERROR>' in xml.upper():
                err_m = re.search(r'<LINEERROR>(.*?)</LINEERROR>', xml, re.I | re.S)
                self.log_append(f'Tally error fetching ledgers: {(err_m.group(1) if err_m else xml[:200]).strip()}', 'error')
            elif '<LEDGER' in xml.upper():
                new_max = extract_max_master_alterid(xml)
                if new_max > max_master_alterid:
                    max_master_alterid = new_max
                mode_label = 'full' if is_first_master_sync else f'incremental (AlterID>{last_master_alterid})'
                _prog( 12, 'Sending ledgers…')
                bundle = build_bundle(uid, 'ledgers', xml)
                res    = send_bundle(srv, uid, key, bundle, cmp_, enc_, sec, extra=extra)
                saved  = res.get('saved', 0) if res.get('ok') else 0
                self.log_append(f'Ledgers {mode_label}: {saved} saved',
                                'ok' if res.get('ok') else 'error')
                if not res.get('ok'):
                    self.log_append(f'  └ {res.get("error")}', 'error')
            else:
                if is_first_master_sync:
                    self.log_append('No ledger data from Tally', 'warn')
                else:
                    self.log_append('No ledger changes since last sync ✓', 'dim')

            if self.stop_flag: raise Exception('Stopped')
            time.sleep(0.5)
            _prog( 20, 'Ledgers done.')

            # ── 2. Stock Items ────────────────────────────────────────────────
            _prog( 22,
                'Fetching all stock items (full sync)…' if is_first_master_sync
                else f'Checking stock changes since AlterID {last_master_alterid}…')
            if is_first_master_sync:
                xml = fetch_stock(host, company=company or '')
                extra_s = {}
            else:
                xml = fetch_stock_by_alterid(host, last_master_alterid, company=company or '')
                extra_s = {'is_incremental': '1'}

            self.log_append(f'Stock: {len(xml):,} bytes from Tally', 'dim')
            if '<LINEERROR>' in xml.upper():
                err_m = re.search(r'<LINEERROR>(.*?)</LINEERROR>', xml, re.I | re.S)
                self.log_append(f'Tally error fetching stock: {(err_m.group(1) if err_m else xml[:200]).strip()}', 'error')
            elif '<STOCKITEM' in xml.upper():
                new_max = extract_max_master_alterid(xml)
                if new_max > max_master_alterid:
                    max_master_alterid = new_max
                mode_label = 'full' if is_first_master_sync else f'incremental (AlterID>{last_master_alterid})'
                _prog( 28, 'Sending stock…')
                bundle = build_bundle(uid, 'stock', xml)
                res    = send_bundle(srv, uid, key, bundle, cmp_, enc_, sec, extra=extra_s)
                saved  = res.get('saved', 0) if res.get('ok') else 0
                self.log_append(f'Stock {mode_label}: {saved} saved',
                                'ok' if res.get('ok') else 'error')
                if not res.get('ok'):
                    self.log_append(f'  └ {res.get("error")}', 'error')
            else:
                if is_first_master_sync:
                    self.log_append('No stock data from Tally (F11 → Enable Inventory)', 'warn')
                else:
                    self.log_append('No stock changes since last sync ✓', 'dim')

            if self.stop_flag: raise Exception('Stopped')
            time.sleep(0.5)
            _prog( 35, 'Masters done.')

            # ── 2b. Voucher Types ────────────────────────────────────────────────
            # Small, low-churn master — full fetch every sync (cheap, no need for
            # incremental complexity here). This is what lets the portal correctly
            # classify a custom-named voucher type (e.g. "Tax Invoice") as a Sales
            # voucher by its real Tally PARENT, instead of guessing from the name.
            _prog( 36, 'Fetching voucher types…')
            try:
                xml = fetch_voucher_types(host, company=company or '')
                self.log_append(f'Voucher types: {len(xml):,} bytes from Tally', 'dim')
                if '<VOUCHERTYPE' in xml.upper():
                    bundle = build_bundle(uid, 'voucher_types', xml)
                    res    = send_bundle(srv, uid, key, bundle, cmp_, enc_, sec)
                    saved  = res.get('saved', 0) if res.get('ok') else 0
                    self.log_append(f'Voucher types: {saved} saved', 'ok' if res.get('ok') else 'error')
                    if not res.get('ok'):
                        self.log_append(f'  └ {res.get("error")}', 'error')
            except Exception as e:
                self.log_append(f'Voucher types sync skipped: {e}', 'warn')

            if self.stop_flag: raise Exception('Stopped')
            time.sleep(0.3)

            # ── 2c. Godowns ──────────────────────────────────────────────────────
            # Not used by any report yet — synced proactively so it's already
            # there once godown-wise stock reporting is built.
            _prog( 37, 'Fetching godowns…')
            try:
                xml = fetch_godowns(host, company=company or '')
                self.log_append(f'Godowns: {len(xml):,} bytes from Tally', 'dim')
                if '<GODOWN' in xml.upper():
                    bundle = build_bundle(uid, 'godowns', xml)
                    res    = send_bundle(srv, uid, key, bundle, cmp_, enc_, sec)
                    saved  = res.get('saved', 0) if res.get('ok') else 0
                    self.log_append(f'Godowns: {saved} saved', 'ok' if res.get('ok') else 'error')
                    if not res.get('ok'):
                        self.log_append(f'  └ {res.get("error")}', 'error')
            except Exception as e:
                self.log_append(f'Godowns sync skipped: {e}', 'warn')

            if self.stop_flag: raise Exception('Stopped')
            time.sleep(0.3)
            _prog( 38, 'Masters done.')

            # ── Save master AlterID watermark ─────────────────────────────────
            if max_master_alterid > last_master_alterid:
                if not self.cfg.has_section('agent'):
                    self.cfg.add_section('agent')
                self.cfg.set('agent', master_key, str(max_master_alterid))
                save_cfg(self.cfg)
                self.log_append(f'Master AlterID watermark updated: {last_master_alterid} → {max_master_alterid}', 'dim')
            if is_first_master_sync:
                if not self.cfg.has_section('agent'):
                    self.cfg.add_section('agent')
                self.cfg.set('agent', full_master_key, today_str)
                save_cfg(self.cfg)

            # ── 3. Vouchers ──────────────────────────────────────────────────
            alterid_key = (f'last_voucher_alterid__id{uid}') if company_name else 'last_voucher_alterid'
            last_alterid = 0
            if self.cfg.has_option('agent', alterid_key):
                try: last_alterid = int(self.cfg.get('agent', alterid_key).strip() or '0')
                except ValueError: pass
            batch_size       = int(_gcfg('voucher_batch_size','500') or '500')
            max_alterid_seen = last_alterid
            any_error        = False
            total_fetched    = 0
            total_saved      = 0

            def send_voucher_batches(xml, is_first_batch_clears):
                nonlocal total_fetched, total_saved, max_alterid_seen, any_error
                batches = split_vouchers_xml(xml, batch_size)
                n = len(batches)
                total_vch = count_vouchers(xml)   # total vouchers across all batches
                if n > 1:
                    self.log_append(f'Sending in {n} batches of up to {batch_size} vouchers ({total_vch} total)...', 'info')
                sent_so_far = 0
                for bi, bxml in enumerate(batches):
                    if self.stop_flag:
                        self.log_append(f'Stopped after batch {bi}/{n} — watermark NOT advanced.', 'warn')
                        any_error = True
                        return
                    bn        = count_vouchers(bxml)
                    sent_so_far += bn
                    pct = 40 + int(50 * (bi + 1) / n)
                    # Show e.g. "500 of 1473" or "3 of 3"
                    _prog( pct, f'Vouchers {sent_so_far} of {total_vch}…')
                    is_first = '1' if (is_first_batch_clears and bi == 0) else '0'
                    bundle   = build_bundle(uid, 'vouchers', bxml, meta={'from_date':'', 'to_date':''})
                    res      = send_bundle(srv, uid, key, bundle, cmp_, enc_, sec, extra={'is_first':is_first})
                    saved    = res.get('saved', 0) if res.get('ok') else 0
                    fetched  = res.get('fetched', bn)
                    total_fetched += fetched
                    total_saved   += saved
                    label = f'Batch {bi+1}/{n}' if n > 1 else 'Vouchers'
                    self.log_append(f'{label} ({bn} vouchers) — fetched:{fetched} saved:{saved}',
                                    'ok' if res.get('ok') else 'error')
                    if res.get('error'):
                        self.log_append(f'  -> {res.get("error")}', 'warn')
                        any_error = True
                if not any_error:
                    max_alterid_seen = max(max_alterid_seen, extract_max_alterid(xml))

            if last_alterid > 0:
                if self.stop_flag: raise Exception('Stopped')
                _prog( 40, 'Checking changes…')
                self.log_append(f'Incremental sync from AlterID {last_alterid} — fetching new/edited vouchers...', 'info')
                xml = fetch_vouchers_by_alterid(host, last_alterid, company=company or '')
                if '<LINEERROR>' in xml.upper():
                    err_m = re.search(r'<LINEERROR>(.*?)</LINEERROR>', xml, re.I | re.S)
                    self.log_append(f'Tally error: {(err_m.group(1) if err_m else xml[:200]).strip()}', 'error')
                vch_count = count_vouchers(xml)
                if vch_count > 0:
                    self.log_append(f'{vch_count} new/edited voucher(s) found since AlterID {last_alterid}', 'info')
                    send_voucher_batches(xml, is_first_batch_clears=False)

                    # (Historical-year backfill check removed — it was dead code:
                    #  'get_earliest_date' was never implemented server-side and
                    #  always failed with "Empty xml in bundle", adding log noise
                    #  with no actual effect. If you need to backfill older years
                    #  after a server data reset, use "Force Full Resync" instead
                    #  of relying on this automatic check.)
                else:
                    self.log_append('No new or edited vouchers since last sync.', 'dim')
                    _prog( 90, 'No changes.')
            else:
                if self.stop_flag: raise Exception('Stopped')
                _prog( 40, 'Fetching all vouchers (all years)…')
                self.log_append('First sync — fetching complete voucher history across ALL financial years...', 'info')
                self.log_append('⚠  This may take several minutes for large companies. Stop takes effect after Tally responds.', 'dim')
                xml = fetch_all_vouchers_unfiltered(host, company=company or '', timeout=1800)
                if self.stop_flag:
                    self.log_append('Stopped — data received but NOT sent to server.', 'warn')
                    raise Exception('Stopped')
                if '<LINEERROR>' in xml.upper():
                    err_m = re.search(r'<LINEERROR>(.*?)</LINEERROR>', xml, re.I | re.S)
                    self.log_append(f'Tally error: {(err_m.group(1) if err_m else xml[:200]).strip()}', 'error')
                vch_count = count_vouchers(xml)
                dmin, dmax = extract_date_range(xml)
                self.log_append(
                    f'{vch_count} voucher(s)' +
                    (f' spanning {dmin} to {dmax}' if dmin and dmax else ''), 'info')
                if vch_count > 0:
                    send_voucher_batches(xml, is_first_batch_clears=True)
                else:
                    self.log_append('No vouchers found in Tally — server data preserved.', 'warn')

            self.log_append(f'Vouchers total — fetched:{total_fetched} saved:{total_saved}',
                            'ok' if not any_error else 'warn')
            if not any_error:
                if not self.cfg.has_section('agent'): self.cfg.add_section('agent')
                self.cfg.set('agent', alterid_key, str(max_alterid_seen))
                # Clear force-resync flag now that full resync completed successfully
                flag_key = f'force_resync__id{uid}'
                if self.cfg.has_option('agent', flag_key):
                    self.cfg.remove_option('agent', flag_key)
                save_cfg(self.cfg)

            now_str = datetime.now().strftime('%d %b %Y, %I:%M %p')
            def _reset_to_idle(n=company_name, t=now_str, cid=uid):
                self._co_progress(n, 0, 'Idle', company_id=cid)
                p = self.co_progress.get(str(cid)) or self.co_progress.get(n)
                if p and p.get('last_sync_lbl'):
                    try: p['last_sync_lbl'].config(text=f'Last sync: {t}')
                    except Exception: pass

            # ── Voucher renumber check ────────────────────────────────────────
            # Only run for incremental syncs (skip after full first-sync to save time)
            # Renumber check: Tally renumbers vouchers when one is inserted between
            # existing ones — these don't get a new AlterID so incremental misses them.
            _ran_renumber = False
            try:
                _prog(95, 'Renumber check…')
                self.log_append('Checking for renumbered vouchers…', 'info')
                fy_start = self.cfg.get('agent', 'fy_start') if self.cfg.has_option('agent','fy_start') else None
                fy_end   = self.cfg.get('agent', 'fy_end')   if self.cfg.has_option('agent','fy_end')   else None
                pairs = fetch_voucher_numbers_for_renumber(
                    host, company=company_name, from_date=fy_start, to_date=fy_end)
                _ran_renumber = True
                if pairs:
                    rnurl = renumber_vouchers_url(srv)
                    rnres = simple_post(rnurl, {
                        'company_key': key,
                        'secret_key':  sec,
                        'pairs_json':  json.dumps(pairs),
                    }, timeout=30)
                    upd = rnres.get('updated', 0) if isinstance(rnres, dict) else 0
                    chk = rnres.get('checked', 0) if isinstance(rnres, dict) else 0
                    if upd > 0:
                        self.log_append(f'Renumber: {upd}/{chk} voucher numbers updated.', 'ok')
                    else:
                        self.log_append(f'Renumber: all {chk} numbers match ✓', 'info')
            except Exception as rne:
                self.log_append(f'Renumber check skipped: {rne}', 'warn')

            # ── Daily deleted-voucher check ──────────────────────────────────────
            # See fetch_voucher_guids_for_presence_check()'s docstring: Tally gives
            # us no direct signal for a hard-deleted voucher, so once a day we pull
            # every GUID currently in Tally for the synced range and let the portal
            # flag anything it has as active but that's now missing.
            try:
                presence_key = f'last_presence_check_date__id{uid}' if uid else 'last_presence_check_date'
                last_presence_date = self.cfg.get('agent', presence_key, fallback='') \
                    if self.cfg.has_option('agent', presence_key) else ''
                today_pc = datetime.now().strftime('%Y%m%d')
                if last_presence_date != today_pc:
                    _prog(98, 'Checking for deleted vouchers…')
                    self.log_append('Checking for vouchers deleted in Tally…', 'info')
                    fy_start = self.cfg.get('agent', 'fy_start') if self.cfg.has_option('agent','fy_start') else None
                    fy_end   = self.cfg.get('agent', 'fy_end')   if self.cfg.has_option('agent','fy_end')   else None
                    guids = fetch_voucher_guids_for_presence_check(
                        host, company=company_name, from_date=fy_start, to_date=fy_end)
                    if guids:
                        pres_url = voucher_presence_url(srv)
                        pres_res = simple_post(pres_url, {
                            'uid': uid, 'key': key,
                            'guids_json': json.dumps(guids),
                        }, timeout=60)
                        marked = pres_res.get('marked_deleted', 0) if isinstance(pres_res, dict) else 0
                        if marked > 0:
                            self.log_append(f'Deleted-voucher check: {marked} voucher(s) marked deleted.', 'ok')
                        else:
                            self.log_append('Deleted-voucher check: no changes.', 'info')
                        if not self.cfg.has_section('agent'): self.cfg.add_section('agent')
                        self.cfg.set('agent', presence_key, today_pc)
                        save_cfg(self.cfg)
                    else:
                        self.log_append('Deleted-voucher check skipped — Tally returned no GUIDs.', 'warn')
            except Exception as pce:
                self.log_append(f'Deleted-voucher check skipped: {pce}', 'warn')

            # ── Daily Tally data folder backup ──────────────────────────────────
            # Zips the on-disk Tally company folder (identified by Tally Number,
            # not name, so same-named split-year companies never get mixed up)
            # and uploads it to the portal once per day.
            try:
                backup_key = f'last_pc_backup_date__id{uid}' if uid else 'last_pc_backup_date'
                last_backup_date = self.cfg.get('agent', backup_key, fallback='') \
                    if self.cfg.has_option('agent', backup_key) else ''
                today_bk = datetime.now().strftime('%Y%m%d')
                if last_backup_date != today_bk:
                    co_info = tally_by_num_s.get(portal_num_s) if portal_num_s else None
                    folder  = (co_info or {}).get('path', '')
                    if not (folder and os.path.isdir(folder)) and portal_num_s:
                        # Tally's own reported path didn't resolve — fall back to
                        # the installed Tally's own tally.ini "Data=" line.
                        ini_root = find_tally_ini_data_root()
                        if ini_root:
                            candidate = os.path.join(ini_root, str(portal_num_s))
                            if os.path.isdir(candidate):
                                folder = candidate
                                self.log_append(
                                    f'Resolved Tally data folder via tally.ini: {folder}', 'dim')
                    if folder and os.path.isdir(folder):
                        _prog(97, 'Backing up Tally data…')
                        self.log_append(
                            f'Backing up Tally data folder for "{company_name}" (#{portal_num_s})…', 'info')
                        zpath = zip_company_folder(folder, portal_num_s)
                        if zpath:
                            zsize = os.path.getsize(zpath)
                            self.log_append(f'Uploading backup ({zsize:,} bytes) in chunks…', 'info')
                            def _bk_progress(cn, tot, _self=self):
                                if cn == 1 or cn == tot or cn % 5 == 0:
                                    _self.log_append(f'  Backup chunk {cn}/{tot} uploaded', 'dim')
                            bres = upload_company_backup(
                                srv, uid, key, sec, portal_num_s, zpath, company_name=company_name,
                                progress_cb=_bk_progress)
                            if bres.get('ok'):
                                self.log_append(
                                    f'Backup uploaded ✓ ({zsize:,} bytes, {bres.get("total_chunks","?")} chunks)', 'ok')
                                if not self.cfg.has_section('agent'): self.cfg.add_section('agent')
                                self.cfg.set('agent', backup_key, today_bk)
                                save_cfg(self.cfg)
                            else:
                                self.log_append(f'Backup upload failed: {bres.get("error")} '
                                                 f'— will retry next sync.', 'warn')
                            try: os.remove(zpath)
                            except Exception: pass
                        else:
                            self.log_append('Backup skipped — could not zip the Tally data folder.', 'warn')
                    else:
                        self.log_append(
                            f'Backup skipped — Tally data folder not found for #{portal_num_s}.', 'warn')
            except Exception as bke:
                self.log_append(f'Backup error: {bke}', 'warn')

            # ── Sync complete ─────────────────────────────────────────────────
            _prog(100, 'Sync complete ✓')
            self.log_append('Sync complete ✓', 'ok')
            self._next_sync = time.time() + self._interval_secs()
            self.root.after(2000, _reset_to_idle)

        except Exception as e:
            self.log_append(f'Sync error: {e}', 'error')
            _prog( 0, 'Error')

    def _sync_done(self):
        self.syncing = False
        self.root.after(0, lambda: self.btn_sync_all.config(state='normal'))
        self.root.after(0, lambda: self.btn_stop.config(state='disabled', text='⏹  Stop'))

    def _co_progress(self, company_name, pct, status='', company_id=''):
        """Update the thin per-company Canvas progress bar (0–100).
        Looks up by company_id first (unique), falls back to name."""
        p = self.co_progress.get(str(company_id)) if company_id else None
        if not p:
            p = self.co_progress.get(company_name)
        if not p:
            return
        bar_w = int(p['width'] * max(0, min(100, pct)) / 100)
        color = '#4ade80' if pct >= 100 else '#f87171' if status.lower().startswith('error') else '#1464f4'
        def _update(bar_w=bar_w, color=color, status=status):
            try:
                p['canvas'].coords(p['fill'], 0, 0, bar_w, 5)
                p['canvas'].itemconfig(p['fill'], fill=color)
                if status:
                    p['status'].set(status)
            except Exception:
                pass
        self.root.after(0, _update)

    def _progress(self, pct, task=''):
        pass  # removed — per-company bars replace this


    def _test_server(self, result_label=None):
        threading.Thread(target=self._do_test_server,
                         kwargs={'result_label': result_label}, daemon=True).start()

    def _do_test_server(self, result_label=None):
        def _show(msg, ok=True):
            self.log_append(msg, 'ok' if ok else 'error')
            if result_label:
                color = '#0e9f6e' if ok else '#e02424'
                self.root.after(0, lambda: result_label.config(text=msg, fg=color))
        a   = self.cfg['agent'] if self.cfg.has_section('agent') else {}
        srv = a.get('server_url', '').rstrip('/')
        uid = a.get('user_id', '')
        key = a.get('api_key', '')
        if not srv:
            _show('No server URL configured.', ok=False); return
        base = srv.replace('/api/ingest.php','').replace('/ingest.php','').rstrip('/')
        url  = f'{base}/api/debug.php' + (f'?uid={uid}&key={key}' if uid and key else '')
        self.log_append(f'Testing: {url}', 'info')
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            self.log_append(
                f'PHP {data.get("php_version")} | DB: {"✓" if data.get("db") else "✗"}'
                f' | AES-GCM: {"✓" if data.get("aes_gcm") else "✗"}', 'info')
            if data.get('auth'):
                u = data.get('user', {})
                self.log_append(f'Auth OK — {u.get("name")} | {u.get("plan")} | {u.get("status")}', 'ok')
            else:
                self.log_append(f'Auth: {data.get("auth_error")}', 'error')
            for w in data.get('warnings', []):
                self.log_append(f'⚠ {w}', 'warn')
            if data.get('ok'):
                _show('✓ Server ready', ok=True)
            else:
                _show('⚠ Server responded with errors — check log file.', ok=False)
        except Exception as e:
            _show(f'✗ {str(e)[:60]}', ok=False)


    # ── LOG (file only — no in-window panel) ─────────────────────────────────

    def log_append(self, msg, tag='info'):
        """Write a log line to the rotating log file and Python logger.
        The in-window log panel was removed in Phase 4 — all history lives
        in:  %APPDATA%\\TallySync\\tallysync_agent.log"""
        level = {'ok': logging.INFO, 'error': logging.ERROR,
                 'warn': logging.WARNING, 'dim': logging.DEBUG}.get(tag, logging.INFO)
        log.log(level, msg)

    def _clear_log(self):
        pass  # no-op — log panel removed

    def _copy_log(self):
        pass  # no-op — log panel removed

    def _on_close(self):
        """Minimize to taskbar instead of closing."""
        self.root.withdraw()

# ── Setup window (shown if not configured) ────────────────────────────────────

class SetupWindow:
    """Shown on first run — admin pastes the config block here."""
    def __init__(self, root):
        self.root = root
        root.title('BizView Pro — First Time Setup')
        root.geometry('480x430')
        root.resizable(False, False)
        root.configure(bg='#0f1923')
        set_window_icon(root)

        tk.Label(root, text='⚡ TallySync Mobile', bg='#0f1923', fg='white',
                 font=('Segoe UI',18,'bold')).pack(pady=(28,4))
        tk.Label(root, text='Paste your configuration block below.\nGet it from: TallySync Portal → Login → Sync page → "Agent Config".',
                 bg='#0f1923', fg='#9ca3af', font=('Segoe UI',10),
                 justify='center').pack(pady=(0,16))

        self.txt = tk.Text(root, height=10, font=('Consolas',10), bg='#1d2939',
                           fg='#a8c4e0', relief='flat', bd=0, insertbackground='white')
        self.txt.pack(fill='x', padx=20)
        self.txt.insert('end',
            '[agent]\nmaster_key    = \nmaster_secret = \n'
            'server_url = http://\ntally_host = http://localhost:9000\n'
            'interval_min = 5\ncompress = true\nencrypt = true\n')

        tk.Label(root, text='', bg='#0f1923').pack(pady=4)
        tk.Button(root, text='Save & Start →', bg='#1464f4', fg='white',
                  font=('Segoe UI',11,'bold'), relief='flat', cursor='hand2',
                  padx=20, pady=10, bd=0,
                  command=self._save).pack()

        self.lbl_err = tk.Label(root, text='', bg='#0f1923', fg='#f87171',
                                 font=('Segoe UI',9))
        self.lbl_err.pack(pady=6)

    def _save(self):
        raw = self.txt.get('1.0','end').strip()
        try:
            cfg = configparser.ConfigParser(inline_comment_prefixes=(';','#'), strict=False, allow_no_value=True)
            cfg.read_string(raw)
            if not cfg.has_section('agent'):
                self.lbl_err.config(text='Error: config must start with [agent]')
                return
            # Use has_option + get (no fallback arg) for compatibility
            mkey = cfg.get('agent','master_key').strip() if cfg.has_option('agent','master_key') else ''
            msec = cfg.get('agent','master_secret').strip() if cfg.has_option('agent','master_secret') else ''
            srv  = cfg.get('agent','server_url').strip() if cfg.has_option('agent','server_url') else ''
            if not mkey:
                self.lbl_err.config(text='Error: master_key is empty'); return
            if not msec:
                self.lbl_err.config(text='Error: master_secret is empty'); return
            if not srv:
                self.lbl_err.config(text='Error: server_url is empty'); return
            # Save to AppData config file
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                cfg.write(f)
            self.root.destroy()
            launch_main()
        except Exception as e:
            self.lbl_err.config(text=str(e)[:90])

# ── Entry point ───────────────────────────────────────────────────────────────

def launch_main():
    root = tk.Tk()
    app  = TallySyncApp(root)
    root.mainloop()

def run_tray(app_root):
    try:
        import pystray
        from PIL import Image, ImageDraw
        img = None
        logo_path = _get_logo_path('logo_icon.png') or _get_logo_path('logo.png')
        if logo_path:
            try:
                img = Image.open(logo_path).convert('RGBA').resize((64,64), Image.LANCZOS)
            except Exception:
                img = None
        if img is None:
            img  = Image.new("RGB", (64,64), color="#0f1923")
            draw = ImageDraw.Draw(img)
            draw.ellipse([4,4,60,60], fill="#1464f4")
        def show_win(icon, item):
            app_root.after(0, app_root.deiconify)
            app_root.after(0, app_root.lift)
        def quit_app(icon, item):
            icon.stop()
            app_root.after(0, app_root.destroy)
        menu = pystray.Menu(
            pystray.MenuItem("Open BizView Pro Agent", show_win, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        )
        icon = pystray.Icon("BizViewPro", img, "BizView Pro — Sync Agent", menu)
        icon.run()
    except ImportError:
        pass


# ── Single-instance lock ─────────────────────────────────────────────────────
# Strategy: bind a TCP port on 127.0.0.1 as a cross-process mutex.
# IMPORTANT: do NOT set SO_REUSEADDR — that would let a second instance
# bind the same port and defeat the lock entirely.
# If the port is already taken, the new instance sends a SHOW command to
# the existing one (which brings its window to front) then exits.
SINGLE_INSTANCE_PORT = 47236

def acquire_single_instance_lock():
    """Returns a bound listening socket if this is the first instance.
    Returns None if another instance is already running (it has been
    asked to show its window via a SHOW message)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # DO NOT set SO_REUSEADDR here — we want the bind to fail if another
    # instance already holds the port.
    try:
        s.bind(('127.0.0.1', SINGLE_INSTANCE_PORT))
        s.listen(5)
        s.settimeout(0.5)
        return s          # we are the first (and only) instance
    except OSError:
        # Port already bound — another instance is running.
        # Ask it to bring its window to front, then we exit.
        try:
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.settimeout(2.0)
            c.connect(('127.0.0.1', SINGLE_INSTANCE_PORT))
            c.sendall(b'SHOW\n')
            c.close()
        except Exception:
            pass
        finally:
            s.close()
        return None


def start_single_instance_listener(lock_socket, root):
    """Background thread: on any incoming 'SHOW' connection, bring the
    main window to front. Runs for the lifetime of the app."""
    def _listen():
        while True:
            try:
                conn, _addr = lock_socket.accept()
                try:
                    msg = conn.recv(16)
                except Exception:
                    msg = b''
                conn.close()
                if b'SHOW' in msg:
                    root.after(0, _show_and_focus, root)
            except socket.timeout:
                continue
            except OSError:
                break   # socket was closed — app is shutting down
            except Exception:
                continue
    threading.Thread(target=_listen, daemon=True, name='SingleInstanceListener').start()


def _show_and_focus(root):
    """Bring the main window to the foreground from any thread via root.after()."""
    try:
        root.deiconify()
        root.state('normal')
        root.lift()
        root.attributes('-topmost', True)
        root.after(300, lambda: root.attributes('-topmost', False))
        root.focus_force()
    except Exception:
        pass


def run_once_headless():
    cfg = load_cfg()
    def _g(k, d=''):
        return cfg.get('agent', k).strip() if cfg.has_option('agent', k) else d
    host = _g('tally_host','http://localhost:9000')
    srv  = _g('server_url','')
    cmp_ = _g('compress','true').lower() == 'true'
    enc_ = _g('encrypt','true').lower() == 'true'

    mkey = _g('master_key','')
    msec = _g('master_secret','')

    companies = []  # list of {company_id, name, api_key, secret_key}
    if mkey and msec and srv:
        try:
            xml = fetch_companies(host)
            cos = parse_companies(xml)
            if cos:
                discover_companies_on_server(srv, mkey, msec, cos)
            list_res = list_assigned_companies(srv, mkey, msec)
            if list_res.get('ok'):
                companies = list_res.get('active', [])
                if list_res.get('pending_setup'):
                    log.warning(f"No companies activated yet — visit {list_res.get('setup_url','the portal')} to select companies.")
            else:
                log.error(f"Could not load company list: {list_res.get('error')}")
        except Exception as e:
            log.error(f"Company discovery failed: {e}")

    if not companies:
        # Legacy single-company fallback
        uid = _g('user_id','')
        key = _g('api_key','')
        sec = _g('secret_key','')
        if uid and key and srv:
            companies = [{'company_id': uid, 'name': '', 'api_key': key, 'secret_key': sec}]

    if not companies or not srv:
        log.error("Agent not configured — set master_key/master_secret (and server_url) in config.ini.")
        return

    log.info(f"=== Headless sync started ({len(companies)} compan{'y' if len(companies)==1 else 'ies'}) ===")
    for co in companies:
        uid = str(co['company_id'])
        key = co['api_key']
        sec = co['secret_key']
        cname = co.get('name','')
        label = f' "{cname}"' if cname else ''
        try:
            xml = fetch_ledgers(host, company=cname)
            if "<LEDGER" in xml.upper():
                res = send_bundle(srv,uid,key,build_bundle(uid,"ledgers",xml),cmp_,enc_,sec)
                log.info(f"[{uid}{label}] Ledgers: {res}")
            time.sleep(1.1)
            xml = fetch_stock(host, company=cname)
            if "<STOCKITEM" in xml.upper():
                res = send_bundle(srv,uid,key,build_bundle(uid,"stock",xml),cmp_,enc_,sec)
                log.info(f"[{uid}{label}] Stock: {res}")
            time.sleep(1.1)

            try:
                xml = fetch_voucher_types(host, company=cname)
                if "<VOUCHERTYPE" in xml.upper():
                    res = send_bundle(srv,uid,key,build_bundle(uid,"voucher_types",xml),cmp_,enc_,sec)
                    log.info(f"[{uid}{label}] Voucher types: {res}")
            except Exception as e:
                log.warning(f"[{uid}{label}] Voucher types sync skipped: {e}")
            time.sleep(0.6)

            try:
                xml = fetch_godowns(host, company=cname)
                if "<GODOWN" in xml.upper():
                    res = send_bundle(srv,uid,key,build_bundle(uid,"godowns",xml),cmp_,enc_,sec)
                    log.info(f"[{uid}{label}] Godowns: {res}")
            except Exception as e:
                log.warning(f"[{uid}{label}] Godowns sync skipped: {e}")
            time.sleep(0.6)

            alterid_key  = ('last_voucher_alterid__' + re.sub(r'[/\\\s]', '_', cname)) if cname else 'last_voucher_alterid'
            last_alterid = int(_g(alterid_key, '0') or '0')
            try:
                server_alterid = int(co.get('last_voucher_alterid', 0) or 0)
            except (TypeError, ValueError):
                server_alterid = 0
            if server_alterid > last_alterid:
                log.info(f"[{uid}{label}] Resuming from portal AlterID watermark {server_alterid} (local was {last_alterid})")
                last_alterid = server_alterid
            batch_size   = int(_g('voucher_batch_size', '500') or '500')
            max_alterid_seen = last_alterid
            any_error = False

            def send_voucher_batches(xml, is_first_batch_clears):
                nonlocal max_alterid_seen, any_error
                batches = split_vouchers_xml(xml, batch_size)
                n = len(batches)
                for bi, bxml in enumerate(batches):
                    bn = count_vouchers(bxml)
                    is_first = '1' if (is_first_batch_clears and bi == 0) else '0'
                    res = send_bundle(srv,uid,key,
                            build_bundle(uid,"vouchers",bxml,meta={"from_date":"","to_date":""}),
                            cmp_,enc_,sec,extra={"is_first":is_first})
                    log.info(f"[{uid}{label}] Vouchers batch {bi+1}/{n} ({bn}): {res}")
                    if not res.get('ok'):
                        any_error = True
                if not any_error:
                    max_alterid_seen = max(max_alterid_seen, extract_max_alterid(xml))

            if last_alterid > 0:
                xml = fetch_vouchers_by_alterid(host, last_alterid, company=cname)
                if '<LINEERROR>' in xml.upper():
                    err_m = re.search(r'<LINEERROR>(.*?)</LINEERROR>', xml, re.I | re.S)
                    log.warning(f"[{uid}{label}] Tally TDL error: {(err_m.group(1) if err_m else xml[:200]).strip()}")
                vch_count = count_vouchers(xml)
                if vch_count > 0:
                    log.info(f"[{uid}{label}] {vch_count} new/edited voucher(s) found")
                    send_voucher_batches(xml, is_first_batch_clears=False)
                else:
                    log.info(f"[{uid}{label}] No new or edited vouchers since AlterID {last_alterid}")
            else:
                log.info(f"[{uid}{label}] First sync — fetching complete voucher history (may take a few minutes)...")
                xml = fetch_all_vouchers_unfiltered(host, company=cname, timeout=1800)
                if '<LINEERROR>' in xml.upper():
                    err_m = re.search(r'<LINEERROR>(.*?)</LINEERROR>', xml, re.I | re.S)
                    log.warning(f"[{uid}{label}] Tally TDL error: {(err_m.group(1) if err_m else xml[:200]).strip()}")
                vch_count = count_vouchers(xml)
                dmin, dmax = extract_date_range(xml)
                log.info(f"[{uid}{label}] Found {vch_count} voucher(s)"
                         + (f" spanning {dmin} to {dmax}" if dmin and dmax else ""))
                if vch_count > 0:
                    send_voucher_batches(xml, is_first_batch_clears=True)
                else:
                    res = send_bundle(srv,uid,key,
                            build_bundle(uid,"vouchers",xml,meta={"from_date":"","to_date":""}),
                            cmp_,enc_,sec,extra={"is_first":"1"})
                    if not res.get('ok'):
                        any_error = True

            if not any_error:
                if not cfg.has_section('agent'): cfg.add_section('agent')
                cfg.set('agent', alterid_key, str(max_alterid_seen))
                save_cfg(cfg)
        except Exception as e:
            log.error(f"[{uid}{label}] Headless sync error: {e}")
    log.info("=== Headless sync complete ===")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="TallySync Agent")
    parser.add_argument("--tray",  action="store_true",
                        help="Start minimised to system tray")
    parser.add_argument("--once",  action="store_true",
                        help="Run one headless sync then exit (Task Scheduler mode)")
    parser.add_argument("--setup", action="store_true",
                        help="Force the Setup Wizard even if already configured")
    args = parser.parse_args()

    # ── Headless Task Scheduler mode ─────────────────────────────────────────
    # --once must NOT show any GUI window (Task Scheduler runs as SYSTEM).
    # Use a separate lock port so the GUI and the scheduler can coexist:
    # the GUI holds port 47236; --once uses 47237 to prevent two scheduler
    # jobs from overlapping each other.
    if args.once:
        SCHED_PORT = 47237
        sched_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sched_lock.bind(('127.0.0.1', SCHED_PORT))
        except OSError:
            # Another --once run is already in progress — skip silently.
            log.info("Headless sync already running — skipping this trigger.")
            sched_lock.close()
            return
        try:
            run_once_headless()
        finally:
            sched_lock.close()
        return

    # ── GUI mode single-instance check ───────────────────────────────────────
    # Must happen BEFORE creating any Tk window so we don't flash a blank
    # window for a split second on a duplicate launch.
    lock_socket = acquire_single_instance_lock()
    if lock_socket is None:
        # Another GUI instance is already running — it has been told to
        # show its window. Just exit cleanly.
        return

    # ── Setup Wizard (first run or forced) ───────────────────────────────────
    if not is_configured() or args.setup:
        root = tk.Tk()
        SetupWindow(root)
        root.mainloop()
        if not is_configured():
            lock_socket.close()
            return

    # ── Main GUI ─────────────────────────────────────────────────────────────
    root = tk.Tk()
    app  = TallySyncApp(root)   # noqa: F841 — kept alive by mainloop

    # Single-instance listener — brings window to front on a second launch.
    start_single_instance_listener(lock_socket, root)

    # System tray — start AFTER the main window exists so pystray references
    # the correct root.  `--tray` launches minimised (window hidden).
    if args.tray:
        root.withdraw()   # hide before tray thread starts to avoid flash
    threading.Thread(target=run_tray, args=(root,), daemon=True,
                     name='TrayThread').start()

    try:
        root.mainloop()
    finally:
        lock_socket.close()


if __name__ == "__main__":
    main()
