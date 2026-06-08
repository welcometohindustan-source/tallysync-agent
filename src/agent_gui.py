"""
TallySync Mobile — Desktop Sync Agent  v3
==========================================
GUI application that runs in the system tray on Windows.
Shows company list, sync progress bar, last sync time.
Syncs automatically every N minutes in background thread.
"""

import os, sys, time, gzip, json, base64, hashlib, logging
import configparser, urllib.request, urllib.error, threading, traceback
from datetime import datetime, timedelta
from pathlib import Path

# tkinter is bundled with Python — no pip install needed
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont

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
DEFAULTS = {
    'agent': {
        'user_id': '', 'api_key': '', 'secret_key': '',
        'server_url': 'http://localhost/tallysync/api/ingest.php',
        'tally_host': 'http://localhost:9000',
        'interval_min': '5', 'days_back': '365',
        'compress': 'true', 'encrypt': 'false',
    }
}

def load_cfg():
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';','#'))
    cfg.read_dict(DEFAULTS)
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE, encoding='utf-8')
    for k in ('tally_host','server_url','user_id','api_key','secret_key','interval_min'):
        if cfg.has_option('agent', k):
            v = cfg.get('agent', k).split(';')[0].split('#')[0].strip()
            v = ' '.join(v.split())
            cfg.set('agent', k, v)
    return cfg

def save_cfg(cfg):
    with open(CONFIG_FILE, 'w') as f:
        cfg.write(f)

# ── Tally + Server helpers (same as agent.py) ─────────────────────────────────

def tally_post(host, xml_body, timeout=60):
    data = xml_body.encode('utf-8')
    req  = urllib.request.Request(
        host, data=data,
        headers={'Content-Type':'text/xml','Content-Length':str(len(data))},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        try: return raw.decode('utf-8')
        except: return raw.decode('cp1252', errors='replace')

def collection_xml(name, typ, fields, fd='', td=''):
    dates = (f'<SVFROMDATE>{fd}</SVFROMDATE>' if fd else '') + \
            (f'<SVTODATE>{td}</SVTODATE>'     if td else '')
    fetch = ','.join(fields)
    return (f'<ENVELOPE><HEADER><VERSION>1</VERSION>'
            f'<TALLYREQUEST>Export</TALLYREQUEST>'
            f'<TYPE>Collection</TYPE><ID>{name}</ID></HEADER>'
            f'<BODY><DESC><STATICVARIABLES>'
            f'<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{dates}'
            f'</STATICVARIABLES><TDL><TDLMESSAGE>'
            f'<COLLECTION NAME="{name}" ISMODIFY="No">'
            f'<TYPE>{typ}</TYPE><FETCH>{fetch}</FETCH>'
            f'</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>')

def fetch_companies(host):
    return tally_post(host, collection_xml('TSComp','Company',['NAME','GUID']), timeout=15)

def fetch_ledgers(host):
    return tally_post(host, collection_xml('TSLed','Ledger',
        ['GUID','ALTERID','NAME','PARENT','CLOSINGBALANCE',
         'LEDMAILINGDETAILS.LIST.PINCODE']), timeout=60)

def fetch_stock(host):
    return tally_post(host, collection_xml('TSStk','StockItem',
        ['GUID','ALTERID','NAME','PARENT','BASEUNITS',
         'CLOSINGBALANCE','CLOSINGVALUE','RATE']), timeout=60)

def fetch_vouchers(host, fd, td):
    return tally_post(host, collection_xml('TSVch','Voucher',
        ['GUID','ALTERID','MASTERID','DATE','VOUCHERTYPENAME',
         'VOUCHERNUMBER','PARTYLEDGERNAME','NARRATION',
         'ALLLEDGERENTRIES.LIST.LEDGERNAME',
         'ALLLEDGERENTRIES.LIST.AMOUNT',
         'ALLLEDGERENTRIES.LIST.ISDEEMEDPOSITIVE',
         'INVENTORYENTRIES.LIST.STOCKITEMNAME',
         'INVENTORYENTRIES.LIST.ACTUALQTY',
         'INVENTORYENTRIES.LIST.RATE',
         'INVENTORYENTRIES.LIST.AMOUNT',
        ], fd=fd, td=td), timeout=90)

def compress_data(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6)

def build_bundle(user_id, data_type, xml_body, meta=None):
    return json.dumps({
        'user_id': user_id, 'data_type': data_type,
        'from_date': (meta or {}).get('from_date',''),
        'to_date':   (meta or {}).get('to_date',''),
        'fetched_at': datetime.utcnow().isoformat()+'Z',
        'agent_ver': '3.0', 'xml': xml_body,
    }, ensure_ascii=False).encode('utf-8')

def send_bundle(server_url, user_id, api_key, bundle_bytes, compress_, encrypt_, secret):
    payload = compress_data(bundle_bytes) if compress_ else bundle_bytes
    cmp_flag = '1' if compress_ else '0'
    enc_flag = '0'
    payload  = base64.b64encode(payload)

    boundary = 'TSSyncBnd' + hashlib.md5(payload[:16]).hexdigest()[:8]
    fields   = {'uid': str(user_id), 'key': api_key,
                'enc': enc_flag, 'cmp': cmp_flag,
                'payload': payload.decode('ascii')}
    parts = []
    for k,v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}')
    parts.append(f'--{boundary}--')
    body = ('\r\n'.join(parts)).encode('utf-8')

    req = urllib.request.Request(server_url, data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                 'Content-Length': str(len(body)),
                 'X-TallySync-Agent': '3.0'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = r.read().decode('utf-8', errors='replace')
            try:    return json.loads(resp)
            except: return {'ok': False, 'error': f'Bad response: {resp[:200]}'}
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:300]
        return {'ok': False, 'error': f'HTTP {e.code}: {body}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

# ── Main GUI Application ───────────────────────────────────────────────────────

class TallySyncApp:
    def __init__(self, root):
        self.root      = root
        self.cfg       = load_cfg()
        self.syncing   = False
        self.stop_flag = False
        self.sync_thread = None
        self.companies = []

        self._build_ui()
        self._apply_config_to_ui()
        self.root.after(500, self._auto_connect)

    # ── UI BUILD ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.title('TallySync Mobile — Sync Agent')
        self.root.geometry('620x700')
        self.root.resizable(False, False)
        self.root.configure(bg='#f0f4f8')
        try: self.root.iconbitmap(default='')
        except: pass

        # ── Header
        hdr = tk.Frame(self.root, bg='#0f1923', height=64)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        tk.Label(hdr, text='⚡ TallySync Mobile', bg='#0f1923', fg='white',
                 font=('Segoe UI', 16, 'bold')).pack(side='left', padx=18, pady=14)
        self.lbl_version = tk.Label(hdr, text='Agent v3.0', bg='#0f1923', fg='#6b8cae',
                                    font=('Segoe UI', 10))
        self.lbl_version.pack(side='right', padx=18)

        # ── Status bar (top)
        status_bar = tk.Frame(self.root, bg='#1d2939', height=38)
        status_bar.pack(fill='x')
        status_bar.pack_propagate(False)
        self.lbl_tally_dot = tk.Label(status_bar, text='●', bg='#1d2939', fg='#4b5563',
                                      font=('Segoe UI', 13))
        self.lbl_tally_dot.pack(side='left', padx=(14,4), pady=8)
        self.lbl_tally_status = tk.Label(status_bar, text='Not connected',
                                         bg='#1d2939', fg='#9ca3af', font=('Segoe UI', 10))
        self.lbl_tally_status.pack(side='left')
        self.lbl_last_sync = tk.Label(status_bar, text='Last sync: Never',
                                      bg='#1d2939', fg='#6b7280', font=('Segoe UI', 9))
        self.lbl_last_sync.pack(side='right', padx=14)

        # ── Main content
        content = tk.Frame(self.root, bg='#f0f4f8')
        content.pack(fill='both', expand=True, padx=16, pady=12)

        # ── Company card
        self._card(content, '📋 Tally Companies', 0)
        self.companies_frame = tk.Frame(self.company_card_body, bg='white')
        self.companies_frame.pack(fill='x')
        self.lbl_no_companies = tk.Label(self.companies_frame,
            text='Click "Connect to Tally" to detect open companies.',
            bg='white', fg='#9ca3af', font=('Segoe UI', 10), pady=12)
        self.lbl_no_companies.pack()

        btn_row = tk.Frame(self.company_card_body, bg='white')
        btn_row.pack(fill='x', padx=10, pady=(6,10))
        self.btn_connect = self._btn(btn_row, '🔌  Connect to Tally',
                                     self._connect_tally, style='light')
        self.btn_connect.pack(side='left', padx=(0,8))
        self.btn_sync_all = self._btn(btn_row, '▶  Sync All Now',
                                      self._sync_all, style='primary')
        self.btn_sync_all.pack(side='left')
        self.btn_stop = self._btn(btn_row, '⏹  Stop',
                                   self._stop_sync, style='danger')
        self.btn_stop.pack(side='left', padx=(8,0))
        self.btn_stop.config(state='disabled')

        # ── Progress card
        self._card(content, '📊 Sync Progress', 1)
        prog_body = self.progress_card_body

        self.lbl_current_task = tk.Label(prog_body, text='Idle',
            bg='white', fg='#374151', font=('Segoe UI', 10), anchor='w')
        self.lbl_current_task.pack(fill='x', padx=10, pady=(8,4))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(prog_body, variable=self.progress_var,
                                             maximum=100, length=560,
                                             style='TallSync.Horizontal.TProgressbar')
        self.progress_bar.pack(padx=10, pady=(0,4))

        self.lbl_progress_pct = tk.Label(prog_body, text='0%',
            bg='white', fg='#6b7280', font=('Segoe UI', 9), anchor='e')
        self.lbl_progress_pct.pack(fill='x', padx=10)

        # Stats row
        stats = tk.Frame(prog_body, bg='white')
        stats.pack(fill='x', padx=10, pady=(4,10))
        self.stat_vars = {}
        for i, (key, label) in enumerate([
                ('ledgers','Ledgers'), ('stock','Stock Items'), ('vouchers','Vouchers')]):
            f = tk.Frame(stats, bg='#f8fafc', bd=1, relief='groove')
            f.grid(row=0, column=i, padx=4, sticky='ew')
            stats.columnconfigure(i, weight=1)
            tk.Label(f, text=label, bg='#f8fafc', fg='#6b7280',
                     font=('Segoe UI', 8)).pack(pady=(6,0))
            var = tk.StringVar(value='—')
            self.stat_vars[key] = var
            tk.Label(f, textvariable=var, bg='#f8fafc', fg='#111827',
                     font=('Segoe UI', 14, 'bold')).pack(pady=(2,6))

        # ── Settings card
        self._card(content, '⚙️  Settings', 2)
        sb = self.settings_card_body
        fields = [
            ('Tally URL',    'tally_host',    'http://localhost:9000'),
            ('Server URL',   'server_url',    'http://localhost/tallysync/api/ingest.php'),
            ('User ID',      'user_id',       ''),
            ('API Key',      'api_key',       ''),
            ('Interval (min)','interval_min', '5'),
        ]
        self.setting_vars = {}
        for row, (label, key, placeholder) in enumerate(fields):
            tk.Label(sb, text=label, bg='white', fg='#374151',
                     font=('Segoe UI', 9, 'bold'), width=14, anchor='e').grid(
                row=row, column=0, padx=(10,6), pady=4, sticky='e')
            var = tk.StringVar()
            entry = tk.Entry(sb, textvariable=var, font=('Segoe UI', 10),
                            bg='#f9fafb', relief='flat', bd=1,
                            highlightthickness=1, highlightbackground='#d1d5db',
                            highlightcolor='#1464f4', width=46)
            entry.grid(row=row, column=1, padx=(0,10), pady=4, sticky='ew')
            self.setting_vars[key] = var

        save_row = tk.Frame(sb, bg='white')
        save_row.grid(row=len(fields), column=0, columnspan=2, padx=10, pady=(4,10), sticky='w')
        self._btn(save_row, '💾  Save Settings', self._save_settings, 'primary').pack(side='left')
        self.lbl_save_ok = tk.Label(save_row, text='', bg='white', fg='#0e9f6e',
                                    font=('Segoe UI', 9))
        self.lbl_save_ok.pack(side='left', padx=8)

        # ── Log card
        self._card(content, '📝 Log', 3)
        log_body = self.log_card_body
        self.log_text = tk.Text(log_body, height=8, font=('Consolas', 9),
                                bg='#0f1923', fg='#a8c4e0',
                                relief='flat', bd=0, wrap='word',
                                state='disabled')
        self.log_text.pack(fill='x', padx=1, pady=1)
        self.log_text.tag_config('ok',    foreground='#4ade80')
        self.log_text.tag_config('error', foreground='#f87171')
        self.log_text.tag_config('info',  foreground='#93c5fd')
        self.log_text.tag_config('dim',   foreground='#6b7280')

        # ── Progress bar style
        style = ttk.Style()
        style.theme_use('default')
        style.configure('TallSync.Horizontal.TProgressbar',
                        troughcolor='#e5e7eb', background='#1464f4',
                        thickness=18, borderwidth=0)

        # Auto-sync countdown label
        self.lbl_next_sync = tk.Label(self.root, text='',
                                       bg='#f0f4f8', fg='#9ca3af', font=('Segoe UI', 8))
        self.lbl_next_sync.pack(pady=(0,4))

        self._start_countdown()

    def _card(self, parent, title, idx):
        frame = tk.Frame(parent, bg='white', bd=1, relief='flat',
                         highlightthickness=1, highlightbackground='#e5e7eb')
        frame.pack(fill='x', pady=(0,10))
        hdr = tk.Frame(frame, bg='#f8fafc', height=34)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        tk.Label(hdr, text=title, bg='#f8fafc', fg='#111827',
                 font=('Segoe UI', 10, 'bold')).pack(side='left', padx=12, pady=6)
        body = tk.Frame(frame, bg='white')
        body.pack(fill='x')
        # Store card body by index
        names = ['company','progress','settings','log']
        if idx < len(names):
            setattr(self, f'{names[idx]}_card_body', body)

    def _btn(self, parent, text, cmd, style='light'):
        colors = {
            'primary': ('#1464f4','white','#0d4db8'),
            'light':   ('#f3f4f6','#374151','#e5e7eb'),
            'danger':  ('#fef2f2','#b91c1c','#fee2e2'),
        }
        bg, fg, hover = colors.get(style, colors['light'])
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      font=('Segoe UI', 9, 'bold'), relief='flat', cursor='hand2',
                      padx=12, pady=7, bd=0)
        b.bind('<Enter>', lambda e: b.config(bg=hover))
        b.bind('<Leave>', lambda e: b.config(bg=bg))
        return b

    # ── Config → UI ──────────────────────────────────────────────────────────

    def _apply_config_to_ui(self):
        a = self.cfg['agent']
        for key, var in self.setting_vars.items():
            var.set(a.get(key, ''))

    def _save_settings(self):
        a = self.cfg['agent']
        for key, var in self.setting_vars.items():
            a[key] = var.get().strip()
        save_cfg(self.cfg)
        self.lbl_save_ok.config(text='✓ Saved')
        self.root.after(2000, lambda: self.lbl_save_ok.config(text=''))
        self.log_append('Settings saved.', 'info')

    # ── Log output ────────────────────────────────────────────────────────────

    def log_append(self, msg, tag='info'):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log_text.config(state='normal')
        self.log_text.insert('end', f'{ts}  {msg}\n', tag)
        self.log_text.see('end')
        self.log_text.config(state='disabled')
        log.info(msg)

    # ── Progress helpers ──────────────────────────────────────────────────────

    def set_progress(self, pct, task=''):
        self.progress_var.set(pct)
        self.lbl_progress_pct.config(text=f'{int(pct)}%')
        if task:
            self.lbl_current_task.config(text=task)
        self.root.update_idletasks()

    def set_stat(self, key, val):
        if key in self.stat_vars:
            self.stat_vars[key].set(str(val))

    # ── Connect to Tally ─────────────────────────────────────────────────────

    def _auto_connect(self):
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _connect_tally(self):
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        host = self.cfg['agent'].get('tally_host','http://localhost:9000')
        self.root.after(0, lambda: self.lbl_tally_status.config(
            text=f'Connecting to {host}...', fg='#fcd34d'))
        self.root.after(0, lambda: self.lbl_tally_dot.config(fg='#fcd34d'))
        try:
            xml = fetch_companies(host)
            if '<' not in xml:
                raise Exception('Tally not responding — open Tally with a company loaded.')
            self.companies = []
            import re
            for m in re.finditer(r'<COMPANY[^>]*>(.*?)</COMPANY>', xml, re.S|re.I):
                block = m.group(0)
                name_m = re.search(r'<NAME>(.*?)</NAME>', block, re.I)
                guid_m = re.search(r'<GUID>(.*?)</GUID>', block, re.I)
                if name_m:
                    self.companies.append({
                        'name': name_m.group(1).strip(),
                        'guid': guid_m.group(1).strip() if guid_m else ''
                    })
            if not self.companies:
                # Try NAME attribute
                for m in re.finditer(r'<COMPANY\s+NAME="([^"]+)"', xml, re.I):
                    self.companies.append({'name': m.group(1), 'guid': ''})

            if self.companies:
                self.root.after(0, self._render_companies)
                self.root.after(0, lambda: self.lbl_tally_status.config(
                    text=f'TallyPrime Connected  ({len(self.companies)} company found)',
                    fg='#4ade80'))
                self.root.after(0, lambda: self.lbl_tally_dot.config(fg='#4ade80'))
                self.log_append(f'Connected — {len(self.companies)} company found', 'ok')
            else:
                raise Exception('No companies found. Open a company in Tally first.')
        except Exception as e:
            self.root.after(0, lambda: self.lbl_tally_status.config(
                text=f'Not connected: {str(e)[:60]}', fg='#f87171'))
            self.root.after(0, lambda: self.lbl_tally_dot.config(fg='#f87171'))
            self.log_append(f'Connect failed: {e}', 'error')

    def _render_companies(self):
        for w in self.companies_frame.winfo_children():
            w.destroy()
        for co in self.companies:
            row = tk.Frame(self.companies_frame, bg='white')
            row.pack(fill='x', padx=10, pady=3)
            tk.Label(row, text='🏢', bg='white', font=('Segoe UI', 12)).pack(side='left', padx=(0,8))
            info = tk.Frame(row, bg='white')
            info.pack(side='left', fill='x', expand=True)
            tk.Label(info, text=co['name'], bg='white', fg='#111827',
                     font=('Segoe UI', 10, 'bold'), anchor='w').pack(anchor='w')
            if co['guid']:
                tk.Label(info, text=co['guid'][:36], bg='white', fg='#9ca3af',
                         font=('Consolas', 8), anchor='w').pack(anchor='w')
            name = co['name']
            btn = self._btn(row, '▶ Sync Now',
                            lambda n=name: threading.Thread(
                                target=self._do_sync, args=(n,), daemon=True).start(),
                            'primary')
            btn.pack(side='right', padx=4)

    # ── Sync All ─────────────────────────────────────────────────────────────

    def _sync_all(self):
        if self.syncing:
            messagebox.showinfo('Sync running', 'A sync is already in progress.')
            return
        threading.Thread(target=self._do_sync, args=(None,), daemon=True).start()

    def _stop_sync(self):
        self.stop_flag = True
        self.log_append('Stop requested — will stop after current batch.', 'error')

    def _do_sync(self, company_name=None):
        if self.syncing: return
        self.syncing   = True
        self.stop_flag = False
        self.root.after(0, lambda: self.btn_sync_all.config(state='disabled'))
        self.root.after(0, lambda: self.btn_stop.config(state='normal'))
        self.root.after(0, lambda: self.set_progress(0, 'Starting sync…'))

        cfg  = self.cfg['agent']
        host = cfg.get('tally_host','http://localhost:9000')
        srv  = cfg.get('server_url','')
        uid  = cfg.get('user_id','')
        key  = cfg.get('api_key','')
        sec  = cfg.get('secret_key','')
        days = int(cfg.get('days_back','365'))
        cmp_ = cfg.get('compress','true').lower() == 'true'
        enc_ = cfg.get('encrypt','false').lower() == 'true'

        if not uid or not srv:
            self.log_append('ERROR: user_id and server_url must be set in Settings.','error')
            self._sync_done()
            return

        try:
            # ── Step 1: Ledgers (0→20%)
            self.root.after(0, lambda: self.set_progress(5, 'Fetching ledgers from Tally…'))
            xml = fetch_ledgers(host)
            if '<LEDGER' in xml.upper():
                self.root.after(0, lambda: self.set_progress(12, 'Sending ledgers to server…'))
                bundle = build_bundle(uid, 'ledgers', xml)
                res    = send_bundle(srv, uid, key, bundle, cmp_, enc_, sec)
                saved  = res.get('saved', 0) if res.get('ok') else 0
                self.root.after(0, lambda s=saved: self.set_stat('ledgers', s))
                self.log_append(f'Ledgers saved: {saved}', 'ok' if res.get('ok') else 'error')
                if not res.get('ok'):
                    self.log_append(f'  Error: {res.get("error")}', 'error')
            else:
                self.log_append('No ledger data from Tally.', 'dim')
            time.sleep(4)
            if self.stop_flag: raise Exception('Stopped by user')
            self.root.after(0, lambda: self.set_progress(20, 'Ledgers done.'))

            # ── Step 2: Stock items (20→35%)
            self.root.after(0, lambda: self.set_progress(22, 'Fetching stock items from Tally…'))
            xml = fetch_stock(host)
            if '<STOCKITEM' in xml.upper():
                self.root.after(0, lambda: self.set_progress(28, 'Sending stock items to server…'))
                bundle = build_bundle(uid, 'stock', xml)
                res    = send_bundle(srv, uid, key, bundle, cmp_, enc_, sec)
                saved  = res.get('saved', 0) if res.get('ok') else 0
                self.root.after(0, lambda s=saved: self.set_stat('stock', s))
                self.log_append(f'Stock items saved: {saved}', 'ok' if res.get('ok') else 'error')
                if not res.get('ok'):
                    self.log_append(f'  Error: {res.get("error")}', 'error')
            else:
                self.log_append('No stock data from Tally.', 'dim')
            time.sleep(4)
            if self.stop_flag: raise Exception('Stopped by user')
            self.root.after(0, lambda: self.set_progress(35, 'Stock done.'))

            # ── Step 3: Vouchers in monthly batches (35→100%)
            today  = datetime.now()
            start  = today - timedelta(days=days)
            ranges = []
            cursor = start.replace(day=1)
            while cursor <= today:
                ms = cursor.strftime('%Y%m%d')
                if cursor.month == 12:
                    me = cursor.replace(year=cursor.year+1, month=1, day=1) - timedelta(days=1)
                else:
                    me = cursor.replace(month=cursor.month+1, day=1) - timedelta(days=1)
                me = min(me, today)
                ranges.append((ms, me.strftime('%Y%m%d')))
                if cursor.month == 12:
                    cursor = cursor.replace(year=cursor.year+1, month=1)
                else:
                    cursor = cursor.replace(month=cursor.month+1)

            total_v   = 0
            n_batches = max(len(ranges), 1)
            for i, (fd, td) in enumerate(ranges):
                if self.stop_flag: raise Exception('Stopped by user')
                pct  = 35 + int((i / n_batches) * 65)
                label = f'Vouchers {fd[:4]}-{fd[4:6]} → {td[:4]}-{td[4:6]}  ({i+1}/{n_batches})'
                self.root.after(0, lambda p=pct, l=label: self.set_progress(p, l))

                xml = fetch_vouchers(host, fd, td)
                if '<VOUCHER' not in xml.upper():
                    self.log_append(f'  {fd}→{td}: no vouchers', 'dim')
                    time.sleep(4)
                    continue

                orig  = len(xml.encode('utf-8'))
                bundle = build_bundle(uid, 'vouchers', xml,
                                      meta={'from_date':fd,'to_date':td})
                cmp_sz = len(gzip.compress(bundle, compresslevel=6)) if cmp_ else len(bundle)
                self.log_append(
                    f'  {fd}→{td}: {orig//1024}KB → {cmp_sz//1024}KB '
                    f'({100-round(cmp_sz/max(orig,1)*100)}% reduction)', 'dim')

                res   = send_bundle(srv, uid, key, bundle, cmp_, enc_, sec)
                saved = res.get('saved', 0) if res.get('ok') else 0
                total_v += saved
                self.root.after(0, lambda v=total_v: self.set_stat('vouchers', v))
                if res.get('ok'):
                    self.log_append(f'  Saved: {saved}', 'ok')
                else:
                    self.log_append(f'  Error: {res.get("error")}', 'error')
                time.sleep(4)

            self.root.after(0, lambda: self.set_progress(100, 'Sync complete ✓'))
            now = datetime.now().strftime('%d %b %Y, %H:%M')
            self.root.after(0, lambda: self.lbl_last_sync.config(
                text=f'Last sync: {now}', fg='#4ade80'))
            self.log_append(
                f'Sync complete — Ledgers:{self.stat_vars["ledgers"].get()} '
                f'Stock:{self.stat_vars["stock"].get()} '
                f'Vouchers:{total_v}', 'ok')

        except Exception as e:
            self.log_append(f'Sync error: {e}', 'error')
            self.root.after(0, lambda: self.set_progress(0, f'Error: {str(e)[:60]}'))

        self._sync_done()

    def _sync_done(self):
        self.syncing = False
        self.root.after(0, lambda: self.btn_sync_all.config(state='normal'))
        self.root.after(0, lambda: self.btn_stop.config(state='disabled'))

    # ── Auto-sync countdown ───────────────────────────────────────────────────

    def _start_countdown(self):
        self._next_sync_time = time.time() + int(
            self.cfg['agent'].get('interval_min','5')) * 60
        self._tick()

    def _tick(self):
        remaining = int(self._next_sync_time - time.time())
        if remaining <= 0:
            if not self.syncing:
                threading.Thread(target=self._do_sync, daemon=True).start()
            self._next_sync_time = time.time() + int(
                self.cfg['agent'].get('interval_min','5')) * 60
            remaining = int(self.cfg['agent'].get('interval_min','5')) * 60
        m, s = divmod(remaining, 60)
        self.lbl_next_sync.config(text=f'Next auto-sync in {m:02d}:{s:02d}')
        self.root.after(1000, self._tick)


def main():
    root = tk.Tk()
    app  = TallySyncApp(root)
    root.mainloop()

if __name__ == '__main__':
    main()
