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
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

# ── Paths ─────────────────────────────────────────────────────────────────────
EXE_DIR     = Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent
CONFIG_FILE = EXE_DIR / 'config.ini'
LOG_FILE    = EXE_DIR / 'tallysync_agent.log'

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8')]
)
log = logging.getLogger('TallySyncGUI')

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULTS = {'agent': {
    'user_id': '', 'api_key': '', 'secret_key': '',
    'server_url': 'http://localhost/tallysync/api/ingest.php',
    'tally_host': 'http://localhost:9000',
    'interval_min': '5', 'compress': 'true', 'encrypt': 'true',
}}

def load_cfg():
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';','#'))
    cfg.read_dict(DEFAULTS)
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE, encoding='utf-8')
    for k in ('tally_host','server_url','user_id','api_key','secret_key','interval_min'):
        if cfg.has_option('agent', k):
            v = cfg.get('agent', k).split(';')[0].split('#')[0].strip()
            cfg.set('agent', k, ' '.join(v.split()))
    return cfg

def save_cfg(cfg):
    with open(CONFIG_FILE, 'w') as f:
        cfg.write(f)

def is_configured():
    cfg = load_cfg()
    a = cfg['agent']
    return bool(a.get('user_id') and a.get('api_key') and a.get('server_url'))

# ── Network helpers ───────────────────────────────────────────────────────────

def tally_post(host, xml, timeout=120):
    data = xml.encode('utf-8')
    req  = urllib.request.Request(host, data=data,
        headers={'Content-Type':'text/xml','Content-Length':str(len(data))}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        try:    return raw.decode('utf-8')
        except: return raw.decode('cp1252', errors='replace')

def collection_xml(name, typ, fields, fd='', td=''):
    dates = (f'<SVFROMDATE>{fd}</SVFROMDATE>' if fd else '') + \
            (f'<SVTODATE>{td}</SVTODATE>'     if td else '')
    return (f'<ENVELOPE><HEADER><VERSION>1</VERSION>'
            f'<TALLYREQUEST>Export</TALLYREQUEST>'
            f'<TYPE>Collection</TYPE><ID>{name}</ID></HEADER>'
            f'<BODY><DESC><STATICVARIABLES>'
            f'<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{dates}'
            f'</STATICVARIABLES><TDL><TDLMESSAGE>'
            f'<COLLECTION NAME="{name}" ISMODIFY="No">'
            f'<TYPE>{typ}</TYPE><FETCH>{",".join(fields)}</FETCH>'
            f'</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>')

def fetch_companies(host):
    return tally_post(host, collection_xml('TSCo','Company',['NAME','GUID']), timeout=15)

def fetch_ledgers(host):
    return tally_post(host, collection_xml('TSLed','Ledger',
        ['GUID','ALTERID','NAME','PARENT','CLOSINGBALANCE',
         'LEDMAILINGDETAILS.LIST.PINCODE']), timeout=60)

def fetch_stock(host):
    return tally_post(host, collection_xml('TSStk','StockItem',
        ['GUID','ALTERID','NAME','PARENT','BASEUNITS',
         'CLOSINGBALANCE','CLOSINGVALUE','RATE']), timeout=60)

def fetch_all_vouchers(host):
    """Single request for ALL vouchers — no date range filter."""
    # Try full company export first (fastest)
    obj_xml = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>TSAllVch</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
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

# ── Parse company list ────────────────────────────────────────────────────────
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
        self.companies = []
        self._next_sync = time.time() + self._interval_secs()
        self._build_ui()
        self.root.after(500, self._auto_connect)
        self._tick()

    def _interval_secs(self):
        try:    return max(60, int(self.cfg['agent'].get('interval_min','5')) * 60)
        except: return 300

    # ── BUILD UI ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.title('TallySync Mobile — Sync Agent')
        self.root.geometry('600x660')
        self.root.resizable(False, False)
        self.root.configure(bg='#f0f4f8')
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        # Header
        hdr = tk.Frame(self.root, bg='#0f1923', height=60)
        hdr.pack(fill='x'); hdr.pack_propagate(False)
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
        self.btn_sync_all = self._btn(btn_row,'▶  Sync All Now', self._sync_all, 'primary')
        self.btn_sync_all.pack(side='left')
        self.btn_stop     = self._btn(btn_row,'⏹  Stop', self._stop, 'danger')
        self.btn_stop.pack(side='left', padx=(6,0))
        self.btn_stop.config(state='disabled')
        self.btn_test     = self._btn(btn_row,'🔍  Test Server', self._test_server, 'light')
        self.btn_test.pack(side='right')

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
        rem = int(self._next_sync - time.time())
        if rem <= 0:
            if not self.syncing:
                threading.Thread(target=self._do_sync, daemon=True).start()
            self._next_sync = time.time() + self._interval_secs()
            rem = self._interval_secs()
        m,s = divmod(rem,60)
        self.lbl_next.config(text=f'Next auto-sync in {m:02d}:{s:02d}')
        self.root.after(1000, self._tick)

    # ── CONNECT ───────────────────────────────────────────────────────────────

    def _auto_connect(self):
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _connect(self):
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        host = self.cfg['agent'].get('tally_host','http://localhost:9000')
        self._set_status('Connecting…', '#fcd34d')
        try:
            xml  = fetch_companies(host)
            cos  = parse_companies(xml)
            if not cos and ('<' in xml):
                # Tally responded but no parsed companies — still connected
                cos = [{'name':'(Company open in Tally)','guid':''}]
            self.companies = cos
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
        for co in self.companies:
            row=tk.Frame(self.co_frame,bg='white'); row.pack(fill='x',padx=10,pady=3)
            tk.Label(row,text='🏢',bg='white',font=('Segoe UI',12)).pack(side='left',padx=(0,8))
            inf=tk.Frame(row,bg='white'); inf.pack(side='left',fill='x',expand=True)
            tk.Label(inf,text=co['name'],bg='white',fg='#111827',
                     font=('Segoe UI',10,'bold'),anchor='w').pack(anchor='w')
            n=co['name']
            self._btn(row,'▶ Sync Now',
                lambda n=n: threading.Thread(target=self._do_sync,daemon=True).start(),
                'primary').pack(side='right',padx=4)

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

        a   = self.cfg['agent']
        host= a.get('tally_host','http://localhost:9000')
        srv = a.get('server_url','')
        uid = a.get('user_id','')
        key = a.get('api_key','')
        sec = a.get('secret_key','')
        cmp_= a.get('compress','true').lower()=='true'
        enc_= a.get('encrypt','true').lower()=='true'

        if not uid or not srv:
            self.log_append('ERROR: Agent not configured. Contact your TallySync admin.','error')
            self._sync_done(); return

        try:
            # ── 1. Ledgers (0→20%)
            self._progress(5,'Fetching ledgers from Tally…')
            xml = fetch_ledgers(host)
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
            xml = fetch_stock(host)
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

            # ── 3. Vouchers — SINGLE REQUEST for all vouchers
            self._progress(40,'Fetching ALL vouchers from Tally (single request)…')
            self.log_append('Requesting all vouchers from Tally…','info')
            xml = fetch_all_vouchers(host)
            vch_count = xml.upper().count('<VOUCHER')
            self.log_append(f'Vouchers: {len(xml):,} bytes, ~{vch_count} vouchers found','dim')

            if '<VOUCHER' in xml.upper():
                self._progress(60,'Compressing & sending vouchers to server…')
                bundle = build_bundle(uid,'vouchers',xml,
                                      meta={'from_date':'','to_date':''})
                orig   = len(bundle)
                res    = send_bundle(srv,uid,key,bundle,cmp_,enc_,sec,
                                     extra={'is_first':'1'})
                saved  = res.get('saved',0) if res.get('ok') else 0
                fetched= res.get('fetched',0)
                self.root.after(0,lambda s=saved:self.stat_vars['vouchers'].set(str(s)))
                self.log_append(f'Vouchers — fetched:{fetched} saved:{saved}',
                                'ok' if res.get('ok') else 'error')
                if res.get('error'): self.log_append(f'  └ {res.get("error")}','warn')
            else:
                self.log_append('No vouchers returned from Tally','warn')

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
        root.geometry('480x400')
        root.resizable(False, False)
        root.configure(bg='#0f1923')

        tk.Label(root, text='⚡ TallySync Mobile', bg='#0f1923', fg='white',
                 font=('Segoe UI',18,'bold')).pack(pady=(28,4))
        tk.Label(root, text='Paste your configuration block below.\nGet it from: TallySync Portal → Login → Sync page → "Agent Config".',
                 bg='#0f1923', fg='#9ca3af', font=('Segoe UI',10),
                 justify='center').pack(pady=(0,16))

        self.txt = tk.Text(root, height=10, font=('Consolas',10), bg='#1d2939',
                           fg='#a8c4e0', relief='flat', bd=0, insertbackground='white')
        self.txt.pack(fill='x', padx=20)
        self.txt.insert('end',
            '[agent]\nuser_id    = \napi_key    = \nsecret_key = \n'
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
                self.lbl_err.config(text='Must start with [agent]'); return
            if not cfg.get('agent','user_id','').strip():
                self.lbl_err.config(text='user_id is empty'); return
            if not cfg.get('agent','api_key','').strip():
                self.lbl_err.config(text='api_key is empty'); return
            with open(CONFIG_FILE,'w') as f: cfg.write(f)
            self.root.destroy()
            launch_main()
        except Exception as e:
            self.lbl_err.config(text=str(e)[:80])

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
    cfg  = load_cfg(); a = cfg["agent"]
    host = a.get("tally_host","http://localhost:9000")
    srv  = a.get("server_url",""); uid = a.get("user_id","")
    key  = a.get("api_key","");    sec = a.get("secret_key","")
    cmp_ = a.get("compress","true").lower()=="true"
    enc_ = a.get("encrypt","true").lower()=="true"
    if not uid or not srv:
        log.error("Agent not configured."); return
    log.info("=== Headless sync started ===")
    try:
        xml = fetch_ledgers(host)
        if "<LEDGER" in xml.upper():
            res = send_bundle(srv,uid,key,build_bundle(uid,"ledgers",xml),cmp_,enc_,sec)
            log.info(f"Ledgers: {res}")
        time.sleep(1.1)
        xml = fetch_stock(host)
        if "<STOCKITEM" in xml.upper():
            res = send_bundle(srv,uid,key,build_bundle(uid,"stock",xml),cmp_,enc_,sec)
            log.info(f"Stock: {res}")
        time.sleep(1.1)
        xml = fetch_all_vouchers(host)
        if "<VOUCHER" in xml.upper():
            res = send_bundle(srv,uid,key,
                    build_bundle(uid,"vouchers",xml,meta={"from_date":"","to_date":""}),
                    cmp_,enc_,sec,extra={"is_first":"1"})
            log.info(f"Vouchers: {res}")
        log.info("=== Headless sync complete ===")
    except Exception as e:
        log.error(f"Headless sync error: {e}")


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
