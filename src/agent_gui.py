"""
TallySync Mobile — Desktop Sync Agent  v4
==========================================
- Runs in Windows system tray (right-click to open/quit)
- Auto-syncs every N minutes in background thread
- Single request for ALL vouchers (no monthly batches)
- No sensitive credentials shown in UI
- Proper Windows installer via NSIS (see installer/ folder)
"""

import os, sys, time, gzip, json, base64, hashlib, logging
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
    """Load embedded logo. Returns PhotoImage or None."""
    p = _get_logo_path('logo.png')
    if p:
        try:
            from PIL import Image, ImageTk
            img = Image.open(p).resize(size, Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            pass
    return None

def set_window_icon(root):
    """Set window taskbar + tray icon from embedded logo.ico or logo.png."""
    # Try .ico first (best quality on Windows)
    p = _get_logo_path('logo.ico')
    if p:
        try:
            root.iconbitmap(str(p))
            return
        except Exception:
            pass
    # Fallback to .png
    p = _get_logo_path('logo.png')
    if p:
        try:
            from PIL import Image, ImageTk
            img = Image.open(p).resize((32,32), Image.LANCZOS)
            ico = ImageTk.PhotoImage(img)
            root.iconphoto(True, ico)
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
    'last_voucher_sync_date': '', 'incremental_overlap_days': '7',
}}

def load_cfg():
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';','#'))
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

def fetch_companies(host):
    return tally_post(host, collection_xml('TSCo','Company',['NAME','GUID']), timeout=15)

def fetch_ledgers(host, company=''):
    return tally_post(host, collection_xml('TSLed','Ledger',
        ['GUID','ALTERID','NAME','PARENT','CLOSINGBALANCE','OPENINGBALANCE',
         'LEDMAILINGDETAILS.LIST.PINCODE','LEDMAILINGDETAILS.LIST.MAILINGNAME'],
        company=company), timeout=60)

def fetch_stock(host, company=''):
    return tally_post(host, collection_xml('TSStk','StockItem',
        ['GUID','ALTERID','NAME','PARENT','BASEUNITS',
         'CLOSINGBALANCE','CLOSINGVALUE','RATE','OPENINGBALANCE','OPENINGVALUE'],
        company=company), timeout=60)

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
    # fallback: assume same directory
    base = server_url.rsplit('/', 1)[0]
    return base + '/agent_companies.php'

def discover_companies_on_server(server_url, master_key, master_secret, companies):
    """Tell the portal which Tally companies the agent can see."""
    url = agent_companies_url(server_url)
    return simple_post(url, {
        'master_key': master_key,
        'master_secret': master_secret,
        'action': 'discover',
        'companies_json': json.dumps([{'name': c['name'], 'guid': c.get('guid','')} for c in companies]),
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
    import re
    companies = []
    for m in re.finditer(r'<COMPANY\b[^>]*>(.*?)</COMPANY>', xml, re.S|re.I):
        block = m.group(0)
        name_m = re.search(r'NAME="([^"]+)"', block, re.I)
        if not name_m:
            name_m = re.search(r'<NAME[^>]*>(.*?)</NAME>', block, re.I)
            name = name_m.group(1).strip() if name_m else ''
        else:
            name = name_m.group(1).strip()
        guid_m = re.search(r'<GUID[^>]*>(.*?)</GUID>', block, re.I)
        guid = guid_m.group(1).strip() if guid_m else ''
        if name:
            companies.append({'name': name, 'guid': guid})
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
        self._settings_win = None
        self._build_ui()
        self.root.after(500, self._auto_connect)
        self._tick()

    def _interval_secs(self):
        try:
            val = self.cfg.get('agent','interval_min') if self.cfg.has_option('agent','interval_min') else '5'
            return max(60, int(val) * 60)
        except Exception:
            return 300

    # ── BUILD UI ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.title('TallySync Mobile — Sync Agent')
        self.root.geometry('600x700')
        self.root.resizable(False, False)
        self.root.configure(bg='#f0f4f8')
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        set_window_icon(self.root)

        # Header — show logo if available
        hdr = tk.Frame(self.root, bg='#0f1923', height=60)
        hdr.pack(fill='x'); hdr.pack_propagate(False)

        # Try to load logo image for header
        self._logo_img = load_logo_image(size=(36,36))
        if self._logo_img:
            tk.Label(hdr, image=self._logo_img, bg='#0f1923').pack(side='left', padx=(14,6), pady=10)
            tk.Label(hdr, text='TallySync Mobile', bg='#0f1923', fg='white',
                     font=('Segoe UI',16,'bold')).pack(side='left', pady=12)
        else:
            tk.Label(hdr, text='⚡  TallySync Mobile', bg='#0f1923', fg='white',
                     font=('Segoe UI',16,'bold')).pack(side='left', padx=18, pady=12)
        tk.Label(hdr, text='Agent v4.0', bg='#0f1923', fg='#6b8cae',
                 font=('Segoe UI',10)).pack(side='right', padx=18)

        # Status bar
        sb = tk.Frame(self.root, bg='#1d2939', height=36)
        sb.pack(fill='x'); sb.pack_propagate(False)
        self.dot = tk.Label(sb, text='●', bg='#1d2939', fg='#4b5563', font=('Segoe UI',13))
        self.dot.pack(side='left', padx=(14,4), pady=8)
        self.lbl_status = tk.Label(sb, text='Connecting…', bg='#1d2939', fg='#9ca3af',
                                    font=('Segoe UI',10))
        self.lbl_status.pack(side='left')
        self.lbl_last = tk.Label(sb, text='Last sync: Never', bg='#1d2939', fg='#6b7280',
                                  font=('Segoe UI',9))
        self.lbl_last.pack(side='right', padx=14)

        content = tk.Frame(self.root, bg='#f0f4f8')
        content.pack(fill='both', expand=True, padx=14, pady=10)

        # ── Company card
        self._card(content, '📋  Tally Companies', 'company')
        self.co_frame = tk.Frame(self.company_body, bg='white')
        self.co_frame.pack(fill='x', padx=2, pady=2)
        self.lbl_no_co = tk.Label(self.co_frame,
            text='Click "Connect" to detect open Tally companies.',
            bg='white', fg='#9ca3af', font=('Segoe UI',10), pady=10)
        self.lbl_no_co.pack()

        btn_row = tk.Frame(self.company_body, bg='white')
        btn_row.pack(fill='x', padx=10, pady=(4,10))
        self.btn_connect  = self._btn(btn_row,'🔌  Connect', self._connect,  'light')
        self.btn_connect.pack(side='left', padx=(0,6))
        self.btn_sync_all = self._btn(btn_row,'▶  Sync Now', self._sync_all, 'primary')
        self.btn_sync_all.pack(side='left')
        self.btn_stop     = self._btn(btn_row,'⏹  Stop', self._stop, 'danger')
        self.btn_stop.pack(side='left', padx=(6,0))
        self.btn_stop.config(state='disabled')
        self.btn_pause    = self._btn(btn_row,'⏸  Pause', self._toggle_pause, 'light')
        self.btn_pause.pack(side='left', padx=(6,0))
        self.btn_settings = self._btn(btn_row,'⚙  Settings', self._open_settings, 'light')
        self.btn_settings.pack(side='right', padx=(6,0))
        self.btn_test     = self._btn(btn_row,'🔍  Test', self._test_server, 'light')
        self.btn_test.pack(side='right')

        # Interval selector row
        irow = tk.Frame(self.company_body, bg='white')
        irow.pack(fill='x', padx=10, pady=(0,8))
        tk.Label(irow, text='Auto-sync interval:', bg='white', fg='#6b7280',
                 font=('Segoe UI',9)).pack(side='left', padx=(0,6))
        _iv = self.cfg.get('agent','interval_min').strip() if self.cfg.has_option('agent','interval_min') else '5'
        self.interval_var = tk.StringVar(value=_iv)
        interval_cb = ttk.Combobox(irow, textvariable=self.interval_var,
                                    values=['5','10','20','30','45','60'],
                                    width=5, state='readonly', font=('Segoe UI',9))
        interval_cb.pack(side='left')
        interval_cb.bind('<<ComboboxSelected>>', self._on_interval_change)
        tk.Label(irow, text='minutes', bg='white', fg='#6b7280',
                 font=('Segoe UI',9)).pack(side='left', padx=(4,0))

        # ── Progress card
        self._card(content, '📊  Sync Progress', 'progress')
        self.lbl_task = tk.Label(self.progress_body, text='Idle — waiting for next sync',
            bg='white', fg='#374151', font=('Segoe UI',10), anchor='w')
        self.lbl_task.pack(fill='x', padx=10, pady=(8,4))
        style = ttk.Style()
        style.theme_use('default')
        style.configure('TS.Horizontal.TProgressbar',
            troughcolor='#e5e7eb', background='#1464f4', thickness=18, borderwidth=0)
        self.pvar = tk.DoubleVar()
        self.pbar = ttk.Progressbar(self.progress_body, variable=self.pvar, maximum=100,
                                     length=555, style='TS.Horizontal.TProgressbar')
        self.pbar.pack(padx=10, pady=(0,4))
        self.lbl_pct = tk.Label(self.progress_body, text='0%', bg='white',
                                 fg='#6b7280', font=('Segoe UI',9), anchor='e')
        self.lbl_pct.pack(fill='x', padx=10)

        stats = tk.Frame(self.progress_body, bg='white')
        stats.pack(fill='x', padx=10, pady=(4,10))
        self.stat_vars = {}
        for i,(k,lbl) in enumerate([('ledgers','Ledgers'),('stock','Stock Items'),('vouchers','Vouchers')]):
            f=tk.Frame(stats,bg='#f8fafc',bd=1,relief='groove'); f.grid(row=0,column=i,padx=4,sticky='ew')
            stats.columnconfigure(i,weight=1)
            tk.Label(f,text=lbl,bg='#f8fafc',fg='#6b7280',font=('Segoe UI',8)).pack(pady=(6,0))
            v=tk.StringVar(value='—'); self.stat_vars[k]=v
            tk.Label(f,textvariable=v,bg='#f8fafc',fg='#111827',font=('Segoe UI',14,'bold')).pack(pady=(2,6))

        # ── Log card
        self._card(content, '📝  Log', 'log')
        lsf = tk.Frame(self.log_body, bg='#0f1923'); lsf.pack(fill='both', padx=1, pady=1)
        scr = tk.Scrollbar(lsf, bg='#1d2939', troughcolor='#0f1923', relief='flat', bd=0)
        scr.pack(side='right', fill='y')
        self.log_txt = tk.Text(lsf, height=9, font=('Consolas',9), bg='#0f1923', fg='#a8c4e0',
                                relief='flat', bd=0, wrap='word', state='disabled',
                                yscrollcommand=scr.set)
        self.log_txt.pack(side='left', fill='both', expand=True)
        scr.config(command=self.log_txt.yview)
        for tag,col in [('ok','#4ade80'),('error','#f87171'),('info','#93c5fd'),
                         ('dim','#6b7280'),('warn','#fcd34d')]:
            self.log_txt.tag_config(tag, foreground=col)
        ltb = tk.Frame(self.log_body, bg='white'); ltb.pack(fill='x', padx=8, pady=(4,6))
        self._btn(ltb,'🗑 Clear', self._clear_log,'light').pack(side='left')
        self._btn(ltb,'📋 Copy log', self._copy_log,'light').pack(side='left',padx=6)
        self.lbl_lines = tk.Label(ltb,text='',bg='white',fg='#9ca3af',font=('Segoe UI',8))
        self.lbl_lines.pack(side='right')

        # Countdown
        self.lbl_next = tk.Label(self.root, text='', bg='#f0f4f8', fg='#9ca3af',
                                  font=('Segoe UI',8))
        self.lbl_next.pack(pady=(0,4))

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
            self.lbl_next.config(text='Auto-sync paused ⏸')
            self.root.after(1000, self._tick)
            return
        rem = int(self._next_sync - time.time())
        if rem <= 0:
            if not self.syncing:
                threading.Thread(target=self._do_sync, daemon=True).start()
            self._next_sync = time.time() + self._interval_secs()
            rem = self._interval_secs()
        m,s = divmod(rem,60)
        self.lbl_next.config(text=f'Next auto-sync in {m:02d}:{s:02d}')
        self.root.after(1000, self._tick)

    def _toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.btn_pause.config(text='▶  Resume')
            self.log_append('Auto-sync paused. Click Resume to restart.', 'warn')
        else:
            self.paused = False
            self._next_sync = time.time() + self._interval_secs()
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
        win.title('TallySync — Settings')
        win.geometry('440x420')
        win.resizable(False, False)
        win.configure(bg='#f0f4f8')
        win.grab_set()
        self._settings_win = win

        # Header
        hdr = tk.Frame(win, bg='#0f1923', height=48)
        hdr.pack(fill='x'); hdr.pack_propagate(False)
        tk.Label(hdr, text='⚙  Agent Settings', bg='#0f1923', fg='white',
                 font=('Segoe UI',13,'bold')).pack(side='left', padx=14, pady=10)

        body = tk.Frame(win, bg='#f0f4f8', padx=16, pady=12)
        body.pack(fill='both', expand=True)

        tk.Label(body,
            text='These credentials are provided by your TallySync administrator. Do not share them with anyone.',
            bg='#f0f4f8', fg='#6b7280', font=('Segoe UI',9), wraplength=380,
            justify='left').pack(anchor='w', pady=(0,10))

        fields = [
            ('Master Key',    'master_key',    ''),
            ('Master Secret', 'master_secret', ''),
            ('Tally URL',     'tally_host',  'http://localhost:9000'),
            ('Server URL',    'server_url',  'http://localhost/tallysync/api/ingest.php'),
        ]
        svars = {}
        for label, key, placeholder in fields:
            row = tk.Frame(body, bg='#f0f4f8')
            row.pack(fill='x', pady=3)
            tk.Label(row, text=label, bg='#f0f4f8', fg='#374151',
                     font=('Segoe UI',9,'bold'), width=12, anchor='e').pack(side='left', padx=(0,8))
            cur = self.cfg.get('agent',key).strip() if self.cfg.has_option('agent',key) else ''
            var = tk.StringVar(value=cur)
            show = '*' if key in ('master_key','master_secret') else ''
            ent = tk.Entry(row, textvariable=var, font=('Segoe UI',10),
                           show=show, bg='white', relief='flat', bd=1,
                           highlightthickness=1, highlightbackground='#d1d5db',
                           highlightcolor='#1464f4')
            ent.pack(side='left', fill='x', expand=True)
            svars[key] = var

        def save_settings():
            if not self.cfg.has_section('agent'):
                self.cfg.add_section('agent')
            for key, var in svars.items():
                val = var.get().strip()
                if val:
                    self.cfg.set('agent', key, val)
            save_cfg(self.cfg)
            self.cfg = load_cfg()
            lbl_ok.config(text='Saved successfully')
            win.after(2000, lambda: lbl_ok.config(text=''))
            self.log_append('Settings saved.', 'ok')

        def toggle_show():
            for key in ('api_key','secret_key'):
                pass  # toggle visibility if needed

        save_btn_row = tk.Frame(body, bg='#f0f4f8')
        save_btn_row.pack(fill='x', pady=(12,0))
        tk.Button(save_btn_row, text='💾  Save Settings',
                  bg='#1464f4', fg='white', font=('Segoe UI',10,'bold'),
                  relief='flat', cursor='hand2', padx=14, pady=8, bd=0,
                  command=save_settings).pack(side='left')
        tk.Button(save_btn_row, text='Close',
                  bg='#f3f4f6', fg='#374151', font=('Segoe UI',10),
                  relief='flat', cursor='hand2', padx=14, pady=8, bd=0,
                  command=win.destroy).pack(side='left', padx=(8,0))
        lbl_ok = tk.Label(body, text='', bg='#f0f4f8', fg='#0e9f6e',
                          font=('Segoe UI',9))
        lbl_ok.pack(anchor='w', pady=(6,0))

        tk.Label(body,
            text='Log file: ' + str(LOG_FILE),
            bg='#f0f4f8', fg='#9ca3af', font=('Consolas',8),
            wraplength=380, justify='left').pack(anchor='w', pady=(12,0))

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

            self.assigned = []
            self.pending_setup = False
            self.setup_url = ''
            if srv and mkey and msec and cos and cos[0]['name'] != '(Company open in Tally)':
                # Tell the portal what we see, then fetch what's actually activated
                discover_res = discover_companies_on_server(srv, mkey, msec, cos)
                if discover_res.get('ok'):
                    n = discover_res.get('discovered', 0)
                    if n: self.log_append(f'Registered {n} new compan{"y" if n==1 else "ies"} with portal', 'info')
                else:
                    self.log_append(f'Portal discovery failed: {discover_res.get("error")}', 'warn')

                list_res = list_assigned_companies(srv, mkey, msec)
                if list_res.get('ok'):
                    self.assigned = list_res.get('active', [])
                    self.pending_setup = list_res.get('pending_setup', False)
                    self.setup_url = list_res.get('setup_url','')
                    plan = list_res.get('plan', {})
                    extra = list_res.get('discovered_unselected', 0)
                    self.log_append(
                        f'Portal: {len(self.assigned)}/{plan.get("max_companies","?")} companies active'
                        + (f', {extra} more available' if extra else ''), 'info')
                else:
                    self.log_append(f'Could not load company list from portal: {list_res.get("error")}', 'warn')
            elif not (mkey and msec):
                self.log_append('master_key/master_secret not set — add them to config.ini (see Sync Tally page on the portal)', 'warn')

            self.root.after(0, self._render_companies)
            self._set_status(f'TallyPrime Connected  ({len(cos)} company found)', '#4ade80')
            self.log_append(f'Connected — {len(cos)} company', 'ok')
        except Exception as e:
            self._set_status(f'Not connected: {str(e)[:55]}', '#f87171')
            self.log_append(f'Connect failed: {e}', 'error')

    def _set_status(self, msg, color):
        self.root.after(0, lambda: self.lbl_status.config(text=msg, fg=color))
        self.root.after(0, lambda: self.dot.config(fg=color))

    def _render_companies(self):
        for w in self.co_frame.winfo_children(): w.destroy()

        assigned_names = {a['name'] for a in self.assigned}
        # Match Tally's discovered companies against the portal's assigned list
        tally_names = {co['name'] for co in self.companies}

        if self.assigned:
            for a in self.assigned:
                row=tk.Frame(self.co_frame,bg='white'); row.pack(fill='x',padx=10,pady=3)
                tk.Label(row,text='🏢',bg='white',font=('Segoe UI',12)).pack(side='left',padx=(0,8))
                inf=tk.Frame(row,bg='white'); inf.pack(side='left',fill='x',expand=True)
                tk.Label(inf,text=a['name'],bg='white',fg='#111827',
                         font=('Segoe UI',10,'bold'),anchor='w').pack(anchor='w')
                if a['name'] not in tally_names:
                    tk.Label(inf,text='Not currently open in TallyPrime',bg='white',fg='#f59e0b',
                             font=('Segoe UI',8),anchor='w').pack(anchor='w')
                co = dict(a)  # company_id, name, api_key, secret_key
                self._btn(row,'▶ Sync Now',
                    lambda co=co: threading.Thread(target=self._do_sync,kwargs={'company':co},daemon=True).start(),
                    'primary').pack(side='right',padx=4)

        if self.pending_setup or (self.assigned and len(self.companies) > len(self.assigned)):
            extra = max(0, len(self.companies) - len(self.assigned))
            note = tk.Frame(self.co_frame,bg='#fff7ed'); note.pack(fill='x',padx=10,pady=(8,4))
            msg = ('Select which companies to sync — visit My Companies on the portal.'
                   if self.pending_setup else
                   f'{extra} more compan{"y" if extra==1 else "ies"} found in Tally — add them on the portal (My Companies) if your plan allows.')
            tk.Label(note,text='⚠ '+msg,bg='#fff7ed',fg='#b45309',
                     font=('Segoe UI',9),wraplength=620,justify='left').pack(anchor='w',padx=8,pady=6)
            if self.setup_url:
                link = tk.Label(note,text=self.setup_url,bg='#fff7ed',fg='#1464f4',
                                 font=('Segoe UI',9,'underline'),cursor='hand2')
                link.pack(anchor='w',padx=8,pady=(0,6))
                link.bind('<Button-1>', lambda e: __import__('webbrowser').open(self.setup_url))

        if not self.assigned and not self.pending_setup and not self.companies:
            tk.Label(self.co_frame,text='Click Connect to detect companies from TallyPrime.',
                     bg='white',fg='#9ca3af',font=('Segoe UI',9)).pack(anchor='w',padx=10,pady=6)

    # ── SYNC ─────────────────────────────────────────────────────────────────

    def _sync_all(self):
        if self.syncing:
            messagebox.showinfo('Sync running','A sync is already in progress.'); return
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _stop(self):
        self.stop_flag = True
        self.log_append('Stop requested…', 'warn')

    def _do_sync(self, company=None):
        if self.syncing: return
        self.syncing=True; self.stop_flag=False
        self.root.after(0,lambda:self.btn_sync_all.config(state='disabled'))
        self.root.after(0,lambda:self.btn_stop.config(state='normal'))
        self._progress(0,'Starting sync…')

        def _gcfg(k, default=''):
            return self.cfg.get('agent', k).strip() if self.cfg.has_option('agent', k) else default
        host = _gcfg('tally_host','http://localhost:9000')
        srv  = _gcfg('server_url','')

        company_name = ''
        if company:
            # company is a dict: {company_id, name, api_key, secret_key, tally_guid}
            uid = str(company.get('company_id',''))
            key = company.get('api_key','')
            sec = company.get('secret_key','')
            company_name = company.get('name','')
            self.log_append(f'Syncing "{company_name}" (company_id={uid})','info')
        elif self.assigned:
            # No specific company picked (e.g. "Sync Now" main button) — sync the first assigned one
            a = self.assigned[0]
            uid = str(a.get('company_id',''))
            key = a.get('api_key','')
            sec = a.get('secret_key','')
            company_name = a.get('name','')
            if len(self.assigned) > 1:
                self.log_append(f'Multiple companies activated - syncing "{company_name}" first. Use each company\'s "Sync Now" button to sync the others.','info')
        else:
            uid  = _gcfg('user_id','')
            key  = _gcfg('api_key','')
            sec  = _gcfg('secret_key','')
        cmp_ = _gcfg('compress','true').lower() == 'true'
        enc_ = _gcfg('encrypt','true').lower() == 'true'
        company = company_name  # downstream fetch_* calls expect a name string for SVCURRENTCOMPANY

        if not uid or not srv:
            self.log_append('ERROR: Agent not configured. Contact your TallySync admin.','error')
            self._sync_done(); return

        try:
            # ── 1. Ledgers (0→20%)
            self._progress(5,'Fetching ledgers from Tally…')
            xml = fetch_ledgers(host, company=company or '')
            self.log_append(f'Ledgers: {len(xml):,} bytes from Tally','dim')
            if '<LEDGER' in xml.upper():
                self._progress(12,'Sending ledgers to server…')
                bundle = build_bundle(uid,'ledgers',xml)
                res    = send_bundle(srv,uid,key,bundle,cmp_,enc_,sec)
                saved  = res.get('saved',0) if res.get('ok') else 0
                self.root.after(0,lambda s=saved:self.stat_vars['ledgers'].set(str(s)))
                self.log_append(f'Ledgers saved: {saved}','ok' if res.get('ok') else 'error')
                if not res.get('ok'): self.log_append(f'  └ {res.get("error")}','error')
            else:
                self.log_append('No ledger data from Tally','warn')
            if self.stop_flag: raise Exception('Stopped')
            time.sleep(1.1)
            self._progress(20,'Ledgers done.')

            # ── 2. Stock (20→35%)
            self._progress(22,'Fetching stock items from Tally…')
            xml = fetch_stock(host, company=company or '')
            self.log_append(f'Stock: {len(xml):,} bytes from Tally','dim')
            if '<STOCKITEM' in xml.upper():
                self._progress(28,'Sending stock items to server…')
                bundle = build_bundle(uid,'stock',xml)
                res    = send_bundle(srv,uid,key,bundle,cmp_,enc_,sec)
                saved  = res.get('saved',0) if res.get('ok') else 0
                self.root.after(0,lambda s=saved:self.stat_vars['stock'].set(str(s)))
                self.log_append(f'Stock saved: {saved}','ok' if res.get('ok') else 'error')
                if not res.get('ok'): self.log_append(f'  └ {res.get("error")}','error')
            else:
                self.log_append('No stock data from Tally (F11 → Enable Inventory)','warn')
            if self.stop_flag: raise Exception('Stopped')
            time.sleep(1.1)
            self._progress(35,'Stock done.')

            # ── 3. Vouchers — incremental (date-filtered) after first sync
            sync_date_key = ('last_voucher_sync_date__' + company) if company else 'last_voucher_sync_date'
            last_sync_date = _gcfg(sync_date_key,'').strip()
            overlap_days   = int(_gcfg('incremental_overlap_days','7') or '7')
            today_str = datetime.now().strftime('%Y%m%d')

            if last_sync_date:
                try:
                    resume_dt = datetime.strptime(last_sync_date,'%Y%m%d') - timedelta(days=overlap_days)
                    from_date = resume_dt.strftime('%Y%m%d')
                except ValueError:
                    from_date = None
                is_first = '0'
                self._progress(40, f'Fetching vouchers since {from_date} (incremental)...')
                self.log_append(f'Incremental sync: fetching vouchers from {from_date} to {today_str} '
                                 f'({overlap_days}-day overlap)','info')
            else:
                from_date = None
                is_first  = '1'
                self._progress(40,'Fetching ALL vouchers from Tally (first sync)...')
                self.log_append('First sync: requesting full voucher history...','info')

            xml = fetch_all_vouchers(host, from_date=from_date, to_date=today_str if from_date else None, company=company or '')
            vch_count = xml.upper().count('<VOUCHER')
            self.log_append(f'Vouchers: {len(xml):,} bytes, ~{vch_count} vouchers found','dim')

            if '<VOUCHER' in xml.upper():
                self._progress(60,'Compressing & sending vouchers to server...')
                bundle = build_bundle(uid,'vouchers',xml,
                                      meta={'from_date':from_date or '','to_date':today_str if from_date else ''})
                orig   = len(bundle)
                res    = send_bundle(srv,uid,key,bundle,cmp_,enc_,sec,
                                     extra={'is_first':is_first})
                saved  = res.get('saved',0) if res.get('ok') else 0
                fetched= res.get('fetched',0)
                self.root.after(0,lambda s=saved:self.stat_vars['vouchers'].set(str(s)))
                self.log_append(f'Vouchers - fetched:{fetched} saved:{saved}',
                                'ok' if res.get('ok') else 'error')
                if res.get('error'): self.log_append(f'  -> {res.get("error")}','warn')
                if res.get('ok'):
                    if not self.cfg.has_section('agent'): self.cfg.add_section('agent')
                    self.cfg.set('agent', sync_date_key, today_str)
                    save_cfg(self.cfg)
            else:
                self.log_append('No vouchers returned from Tally (or none in range)','warn')
                if from_date:
                    if not self.cfg.has_section('agent'): self.cfg.add_section('agent')
                    self.cfg.set('agent', sync_date_key, today_str)
                    save_cfg(self.cfg)

            self._progress(100,'Sync complete ✓')
            now = datetime.now().strftime('%d %b %Y, %H:%M')
            self.root.after(0,lambda:self.lbl_last.config(text=f'Last sync: {now}',fg='#4ade80'))
            self.log_append('Sync complete ✓','ok')
            self._next_sync = time.time() + self._interval_secs()

        except Exception as e:
            self.log_append(f'Sync error: {e}','error')
            self._progress(0,f'Error — {str(e)[:60]}')
        self._sync_done()

    def _sync_done(self):
        self.syncing=False
        self.root.after(0,lambda:self.btn_sync_all.config(state='normal'))
        self.root.after(0,lambda:self.btn_stop.config(state='disabled'))

    def _progress(self, pct, task=''):
        self.pvar.set(pct)
        self.root.after(0,lambda p=pct:self.lbl_pct.config(text=f'{int(p)}%'))
        if task: self.root.after(0,lambda t=task:self.lbl_task.config(text=t))
        self.root.update_idletasks()

    # ── TEST SERVER ───────────────────────────────────────────────────────────

    def _test_server(self):
        threading.Thread(target=self._do_test_server, daemon=True).start()

    def _do_test_server(self):
        a   = self.cfg['agent']
        srv = a.get('server_url','').rstrip('/')
        uid = a.get('user_id','')
        key = a.get('api_key','')
        base= srv.replace('/api/ingest.php','').replace('/ingest.php','').rstrip('/')
        url = f'{base}/api/debug.php' + (f'?uid={uid}&key={key}' if uid and key else '')
        self.log_append(f'Testing: {url}','info')
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            self.log_append(f'PHP {data.get("php_version")} | DB: {"✓" if data.get("db") else "✗"} | AES-GCM: {"✓" if data.get("aes_gcm") else "✗"}','info')
            if data.get('auth'):
                u=data.get('user',{}); self.log_append(f'Auth OK — {u.get("name")} | {u.get("plan")} | {u.get("status")}','ok')
            else:
                self.log_append(f'Auth: {data.get("auth_error")}','error')
            for w in data.get('warnings',[]): self.log_append(f'⚠ {w}','warn')
            if data.get('ok'): self.log_append('✓ Server ready','ok')
        except Exception as e:
            self.log_append(f'Server test failed: {e}','error')

    # ── LOG ───────────────────────────────────────────────────────────────────

    def log_append(self, msg, tag='info'):
        ts=datetime.now().strftime('%H:%M:%S')
        self.log_txt.config(state='normal')
        self.log_txt.insert('end',f'{ts}  {msg}\n',tag)
        self.log_txt.see('end')
        self.log_txt.config(state='disabled')
        lines=int(self.log_txt.index('end-1c').split('.')[0])
        self.lbl_lines.config(text=f'{lines} lines')
        log.info(msg)

    def _clear_log(self):
        self.log_txt.config(state='normal'); self.log_txt.delete('1.0','end')
        self.log_txt.config(state='disabled'); self.lbl_lines.config(text='')

    def _copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_txt.get('1.0','end'))
        self.lbl_lines.config(text='✓ Copied')
        self.root.after(2000,lambda:self.lbl_lines.config(text=''))

    def _on_close(self):
        """Minimize to taskbar instead of closing."""
        self.root.withdraw()

# ── Setup window (shown if not configured) ────────────────────────────────────

class SetupWindow:
    """Shown on first run — admin pastes the config block here."""
    def __init__(self, root):
        self.root = root
        root.title('TallySync — First Time Setup')
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
            cfg = configparser.ConfigParser(inline_comment_prefixes=(';','#'))
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
            pystray.MenuItem("Open TallySync Agent", show_win, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        )
        icon = pystray.Icon("TallySyncAgent", img, "TallySync Mobile", menu)
        icon.run()
    except ImportError:
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

            sync_date_key = ('last_voucher_sync_date__' + cname) if cname else 'last_voucher_sync_date'
            last_sync_date = _g(sync_date_key,'').strip()
            overlap_days   = int(_g('incremental_overlap_days','7') or '7')
            today_str = datetime.now().strftime('%Y%m%d')
            from_date = None
            is_first  = '1'
            if last_sync_date:
                try:
                    from_date = (datetime.strptime(last_sync_date,'%Y%m%d') - timedelta(days=overlap_days)).strftime('%Y%m%d')
                    is_first  = '0'
                except ValueError:
                    pass
            xml = fetch_all_vouchers(host, from_date=from_date, to_date=today_str if from_date else None, company=cname)
            if "<VOUCHER" in xml.upper():
                res = send_bundle(srv,uid,key,
                        build_bundle(uid,"vouchers",xml,meta={"from_date":from_date or "","to_date":today_str if from_date else ""}),
                        cmp_,enc_,sec,extra={"is_first":is_first})
                log.info(f"[{uid}{label}] Vouchers: {res}")
                if res.get('ok'):
                    if not cfg.has_section('agent'): cfg.add_section('agent')
                    cfg.set('agent', sync_date_key, today_str)
                    save_cfg(cfg)
            elif from_date:
                if not cfg.has_section('agent'): cfg.add_section('agent')
                cfg.set('agent', sync_date_key, today_str)
                save_cfg(cfg)
        except Exception as e:
            log.error(f"[{uid}{label}] Headless sync error: {e}")
    log.info("=== Headless sync complete ===")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="TallySync Agent")
    parser.add_argument("--tray",  action="store_true")
    parser.add_argument("--once",  action="store_true")
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args()

    if args.once:
        run_once_headless(); return

    if not is_configured() or args.setup:
        root = tk.Tk()
        SetupWindow(root)
        root.mainloop()
        if not is_configured(): return

    root = tk.Tk()
    app  = TallySyncApp(root)
    threading.Thread(target=run_tray, args=(root,), daemon=True).start()
    if args.tray:
        root.withdraw()
    root.mainloop()


if __name__ == "__main__":
    main()
