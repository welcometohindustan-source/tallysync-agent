"""
TallySync Mobile — Background Sync Agent
=========================================
Runs silently on the Windows PC where Tally is installed.
Every N minutes it:
  1. Sends small TDL XML requests to Tally's local HTTP port
  2. Compresses the response (gzip) — reduces payload ~90%
  3. Encrypts with AES-256 using a shared secret key
  4. POSTs the encrypted bundle to your server API
  5. Server decrypts, parses, saves to DB

Install: run install.bat as Administrator
Uninstall: run uninstall.bat as Administrator

Config: edit config.ini in the same folder as TallySyncAgent.exe
"""

import os
import sys
import time
import gzip
import json
import base64
import hashlib
import logging
import platform
import argparse
import traceback
import configparser
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ── optional imports (bundled via PyInstaller) ───────────────────────────────
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
EXE_DIR    = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
CONFIG_FILE = EXE_DIR / 'config.ini'
LOG_FILE    = EXE_DIR / 'tallysync_agent.log'

# ── logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('TallySyncAgent')

# ── config ────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    'agent': {
        'user_id':        '',           # assigned by admin when approving account
        'api_key':        '',           # unique key per user, set by admin
        'server_url':     'http://localhost/tallysync/api/ingest.php',
        'tally_host':     'http://localhost:9000',
        'interval_min':   '5',          # sync every N minutes
        'days_back':      '365',        # how many days of vouchers to pull
        'encrypt':        'true',       # AES-256-GCM encryption (PHP 8.2+)
        'compress':       'true',       # gzip compression
        'secret_key':     '',           # 32-char AES key (set by admin portal)
        'batch_months':   '1',          # months per voucher request to Tally
    }
}

def load_config():
    # inline_comment_prefixes strips   key = value  ; comment  → value only
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read_dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE, encoding='utf-8')
    # Extra safety: strip any leftover whitespace/comments from critical values
    for key in ('tally_host', 'server_url', 'user_id', 'api_key', 'secret_key',
                'interval_min', 'days_back', 'batch_months'):
        if cfg.has_option('agent', key):
            val = cfg.get('agent', key)
            # Remove everything after first semicolon or hash that looks like a comment
            val = val.split(';')[0].split('#')[0].strip()
            # Remove embedded newlines (multi-line value continuation)
            val = ' '.join(val.split())
            cfg.set('agent', key, val)
    return cfg

def save_default_config():
    if not CONFIG_FILE.exists():
        cfg = configparser.ConfigParser()
        cfg.read_dict(DEFAULT_CONFIG)
        with open(CONFIG_FILE, 'w') as f:
            cfg.write(f)
        log.info(f'Default config written to {CONFIG_FILE}')

# ── encryption / compression ──────────────────────────────────────────────────

def derive_key(secret: str) -> bytes:
    """Derive a 32-byte AES key from the secret string."""
    return hashlib.sha256(secret.encode('utf-8')).digest()

def encrypt_payload(data: bytes, secret: str) -> bytes:
    """AES-256-GCM encrypt. Returns: 12-byte nonce + ciphertext (base64 encoded)."""
    if not HAS_CRYPTO:
        return data   # fallback: no encryption (not recommended in production)
    key   = derive_key(secret)
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, data, None)
    return base64.b64encode(nonce + ct)

def decrypt_payload(data: bytes, secret: str) -> bytes:
    """Inverse of encrypt_payload."""
    if not HAS_CRYPTO:
        return data
    raw   = base64.b64decode(data)
    nonce = raw[:12]
    ct    = raw[12:]
    key   = derive_key(secret)
    return AESGCM(key).decrypt(nonce, ct, None)

def compress(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6)

def decompress(data: bytes) -> bytes:
    return gzip.decompress(data)

# ── Tally XML helpers ─────────────────────────────────────────────────────────

def tally_post(host: str, xml_body: str, timeout: int = 60) -> str:
    """POST TDL XML to Tally and return response string."""
    data = xml_body.encode('utf-8')
    req  = urllib.request.Request(
        host,
        data=data,
        headers={'Content-Type': 'text/xml', 'Content-Length': str(len(data))},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            # Tally sometimes returns windows-1252 encoded data
            try:
                return raw.decode('utf-8')
            except UnicodeDecodeError:
                return raw.decode('cp1252', errors='replace')
    except urllib.error.URLError as e:
        raise ConnectionError(f'Cannot reach Tally at {host}: {e.reason}')
    except Exception as e:
        raise ConnectionError(f'Tally request failed: {e}')

def collection_xml(collection_name: str, type_name: str, fields: list,
                   from_date: str = '', to_date: str = '') -> str:
    date_vars = ''
    if from_date:
        date_vars += f'<SVFROMDATE>{from_date}</SVFROMDATE>'
    if to_date:
        date_vars += f'<SVTODATE>{to_date}</SVTODATE>'
    fetch = ','.join(fields)
    return (
        f'<ENVELOPE>'
        f'<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>'
        f'<TYPE>Collection</TYPE><ID>{collection_name}</ID></HEADER>'
        f'<BODY><DESC>'
        f'<STATICVARIABLES>'
        f'<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'{date_vars}'
        f'</STATICVARIABLES>'
        f'<TDL><TDLMESSAGE>'
        f'<COLLECTION NAME="{collection_name}" ISMODIFY="No">'
        f'<TYPE>{type_name}</TYPE>'
        f'<FETCH>{fetch}</FETCH>'
        f'</COLLECTION>'
        f'</TDLMESSAGE></TDL>'
        f'</DESC></BODY></ENVELOPE>'
    )

def fetch_ledgers(host: str) -> str:
    log.info('Fetching ledgers from Tally...')
    xml = collection_xml(
        'TSLedgers', 'Ledger',
        ['GUID','ALTERID','NAME','PARENT','CLOSINGBALANCE','OPENINGBALANCE',
         'LEDMAILINGDETAILS.LIST.PINCODE','LEDMAILINGDETAILS.LIST.MAILINGNAME']
    )
    return tally_post(host, xml)

def fetch_stock(host: str) -> str:
    log.info('Fetching stock items from Tally...')
    xml = collection_xml(
        'TSStock', 'StockItem',
        ['GUID','ALTERID','NAME','PARENT','BASEUNITS','CLOSINGBALANCE','CLOSINGVALUE','RATE','OPENINGBALANCE','OPENINGVALUE']
    )
    return tally_post(host, xml)

def fetch_vouchers(host: str, from_date: str, to_date: str) -> str:
    """Fetch vouchers — try OBJECTTYPE first, fallback to Collection."""
    fd = from_date; td = to_date
    obj_xml = (
        '<ENVELOPE>'
        '<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Object</TYPE><SUBTYPE>Voucher</SUBTYPE></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'<SVFROMDATE>{fd}</SVFROMDATE><SVTODATE>{td}</SVTODATE>'
        '</STATICVARIABLES></DESC></BODY></ENVELOPE>'
    )
    result = tally_post(host, obj_xml, timeout=120)
    if '<VOUCHER' in result.upper():
        return result
    # Fallback to Collection
    xml = collection_xml('TSVch','Voucher',
        ['GUID','ALTERID','MASTERID','DATE','VOUCHERTYPENAME','VOUCHERNUMBER',
         'PARTYLEDGERNAME','NARRATION',
         'ALLLEDGERENTRIES.LIST.LEDGERNAME','ALLLEDGERENTRIES.LIST.AMOUNT',
         'ALLLEDGERENTRIES.LIST.ISDEEMEDPOSITIVE',
         'INVENTORYENTRIES.LIST.STOCKITEMNAME','INVENTORYENTRIES.LIST.ACTUALQTY',
         'INVENTORYENTRIES.LIST.BILLEDQTY','INVENTORYENTRIES.LIST.RATE',
         'INVENTORYENTRIES.LIST.AMOUNT'],
        from_date=from_date, to_date=to_date)
    return tally_post(host, xml, timeout=120)

def fetch_companies(host: str) -> str:
    xml = collection_xml('TSCompanies', 'Company', ['NAME','GUID'])
    return tally_post(host, xml)

# ── Build payload bundle ──────────────────────────────────────────────────────

def build_bundle(user_id: str, data_type: str, xml_body: str,
                 meta: dict = None) -> dict:
    """Wrap raw XML with metadata before sending to server."""
    return {
        'user_id':   user_id,
        'data_type': data_type,          # 'ledgers' | 'stock' | 'vouchers'
        'from_date': (meta or {}).get('from_date', ''),
        'to_date':   (meta or {}).get('to_date',   ''),
        'fetched_at': datetime.utcnow().isoformat() + 'Z',
        'agent_ver': '2.0',
        'xml':       xml_body,
    }

# ── Send to server ────────────────────────────────────────────────────────────

def send_to_server(server_url: str, user_id: str, api_key: str,
                   bundle: dict, secret: str,
                   do_compress: bool = True, do_encrypt: bool = True) -> dict:
    """
    POST the bundle to server. Pipeline:
      JSON → gzip → AES-256-GCM encrypt → base64 → HTTP POST
    Server receives: multipart/form-data with fields:
      uid, key, enc (1/0), cmp (1/0), payload (base64 blob)
    """
    raw_json = json.dumps(bundle, ensure_ascii=False).encode('utf-8')

    # Compress
    if do_compress:
        payload = compress(raw_json)
        log.info(f'  Compress: {len(raw_json):,} → {len(payload):,} bytes '
                 f'({100-round(len(payload)/max(len(raw_json),1)*100)}% reduction)')
    else:
        payload = raw_json

    # Encrypt
    if do_encrypt and secret:
        payload = encrypt_payload(payload, secret)
        enc_flag = '1'
    else:
        payload = base64.b64encode(payload)
        enc_flag = '0'

    cmp_flag = '1' if do_compress else '0'

    # Build multipart form-data manually (no external libs)
    boundary = '----TallySyncBoundary' + hashlib.md5(payload[:32]).hexdigest()[:8]
    fields   = {
        'uid':     user_id,
        'key':     api_key,
        'enc':     enc_flag,
        'cmp':     cmp_flag,
        'payload': payload.decode('ascii') if isinstance(payload, bytes) else payload,
    }
    body_parts = []
    for k, v in fields.items():
        body_parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
            f'{v}'
        )
    body_parts.append(f'--{boundary}--')
    body = ('\r\n'.join(body_parts)).encode('utf-8')

    req = urllib.request.Request(
        server_url,
        data=body,
        headers={
            'Content-Type':   f'multipart/form-data; boundary={boundary}',
            'Content-Length': str(len(body)),
            'X-TallySync-Agent': '2.0',
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp_body = resp.read().decode('utf-8', errors='replace')
            try:
                return json.loads(resp_body)
            except json.JSONDecodeError:
                return {'ok': False, 'error': f'Bad server response: {resp_body[:200]}'}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='replace')[:300]
        return {'ok': False, 'error': f'HTTP {e.code}: {err_body}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

# ── Main sync cycle ───────────────────────────────────────────────────────────

def run_sync(cfg: configparser.ConfigParser) -> dict:
    a          = cfg['agent']
    host       = a.get('tally_host',   'http://localhost:9000').rstrip('/')
    server_url = a.get('server_url',   '')
    user_id    = a.get('user_id',      '')
    api_key    = a.get('api_key',      '')
    secret     = a.get('secret_key',   '')
    days_back  = int(a.get('days_back',  '365'))
    months     = int(a.get('batch_months','1'))
    compress_  = a.get('compress',    'true').lower() == 'true'
    encrypt_   = a.get('encrypt',     'true').lower() == 'true'

    if not user_id or not server_url:
        log.error('user_id and server_url must be set in config.ini')
        return {'ok': False, 'error': 'Config incomplete'}

    results = {'ok': True, 'ledgers': 0, 'stock': 0, 'vouchers': 0, 'errors': []}

    # ── 1. Ledgers
    try:
        xml = fetch_ledgers(host)
        if '<LEDGER' in xml.upper():
            bundle = build_bundle(user_id, 'ledgers', xml)
            res    = send_to_server(server_url, user_id, api_key, bundle,
                                    secret, compress_, encrypt_)
            if res.get('ok'):
                results['ledgers'] = res.get('saved', 0)
                log.info(f'  Ledgers saved: {results["ledgers"]}')
            else:
                log.error(f'  Ledger server error: {res.get("error")}')
                results['errors'].append('ledgers: ' + str(res.get('error')))
        else:
            log.warning('  Tally returned no ledger data.')
    except Exception as e:
        log.error(f'  Ledger fetch error: {e}')
        results['errors'].append('ledgers: ' + str(e))
    time.sleep(1)   # wait before next request

    # ── 2. Stock items
    try:
        xml = fetch_stock(host)
        if '<STOCKITEM' in xml.upper():
            bundle = build_bundle(user_id, 'stock', xml)
            res    = send_to_server(server_url, user_id, api_key, bundle,
                                    secret, compress_, encrypt_)
            if res.get('ok'):
                results['stock'] = res.get('saved', 0)
                log.info(f'  Stock items saved: {results["stock"]}')
            else:
                log.error(f'  Stock server error: {res.get("error")}')
                results['errors'].append('stock: ' + str(res.get('error')))
        else:
            log.warning('  Tally returned no stock data.')
    except Exception as e:
        log.error(f'  Stock fetch error: {e}')
        results['errors'].append('stock: ' + str(e))
    time.sleep(1)   # wait before next request

    # ── 3. Vouchers in monthly batches
    total_v = 0
    today   = datetime.now()
    start   = today - timedelta(days=days_back)
    # Build month ranges
    ranges  = []
    cursor  = start.replace(day=1)
    while cursor <= today:
        m_start = cursor.strftime('%Y%m%d')
        # End of this month
        if cursor.month == 12:
            m_end = cursor.replace(year=cursor.year+1, month=1, day=1) - timedelta(days=1)
        else:
            m_end = cursor.replace(month=cursor.month+1, day=1) - timedelta(days=1)
        m_end = min(m_end, today)
        ranges.append((m_start, m_end.strftime('%Y%m%d')))
        # Advance by `months` months
        for _ in range(months):
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year+1, month=1)
            else:
                cursor = cursor.replace(month=cursor.month+1)

    for from_dt, to_dt in ranges:
        try:
            log.info(f'  Vouchers {from_dt} → {to_dt} ...')
            xml = fetch_vouchers(host, from_dt, to_dt)
            if '<VOUCHER' not in xml.upper():
                log.info(f'    No vouchers in this period.')
                continue
            bundle = build_bundle(user_id, 'vouchers', xml,
                                  meta={'from_date': from_dt, 'to_date': to_dt})
            res = send_to_server(server_url, user_id, api_key, bundle,
                                 secret, compress_, encrypt_)
            if res.get('ok'):
                saved = res.get('saved', 0)
                total_v += saved
                log.info(f'    Saved: {saved}')
            else:
                err_msg = str(res.get('error', ''))
                log.error(f'    Server error: {err_msg}')
                # If server says PHP can't do GCM, auto-disable encryption
                if 'PHP_NO_GCM' in err_msg:
                    log.warning('    Server PHP does not support AES-GCM. '
                                'Setting encrypt=false automatically.')
                    encrypt_ = False
                    secret   = ''
                results['errors'].append(f'vouchers {from_dt}: ' + err_msg)
            # Small delay between batches — prevents rate limit on server
            time.sleep(1)
        except Exception as e:
            log.error(f'    Voucher batch error: {e}')
            results['errors'].append(f'vouchers {from_dt}: ' + str(e))
            time.sleep(1)

    results['vouchers'] = total_v
    log.info(f'Sync done — Ledgers:{results["ledgers"]} '
             f'Stock:{results["stock"]} Vouchers:{results["vouchers"]}')
    return results

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='TallySync Mobile Agent')
    parser.add_argument('--once',     action='store_true', help='Run one sync and exit')
    parser.add_argument('--config',   action='store_true', help='Show config file path and exit')
    parser.add_argument('--test',     action='store_true', help='Test Tally connection and exit')
    parser.add_argument('--version',  action='store_true', help='Show version and exit')
    args = parser.parse_args()

    if args.version:
        print('TallySync Agent v2.0'); sys.exit(0)

    save_default_config()
    cfg = load_config()

    if args.config:
        print(f'Config file: {CONFIG_FILE}'); sys.exit(0)

    if args.test:
        host = cfg['agent'].get('tally_host', 'http://localhost:9000')
        log.info(f'Testing Tally connection at {host}...')
        try:
            xml = fetch_companies(host)
            if '<COMPANY' in xml.upper() or '<ENVELOPE' in xml.upper():
                print(f'SUCCESS — Tally is reachable at {host}')
                print(f'Response preview: {xml[:300]}')
            else:
                print(f'WARNING — Got response but no company data. Tally may not have a company open.')
                print(f'Response: {xml[:300]}')
        except Exception as e:
            print(f'FAILED — {e}')
        sys.exit(0)

    if args.once:
        log.info('=== TallySync Agent — single run ===')
        run_sync(cfg)
        sys.exit(0)

    # Loop mode — runs indefinitely (used by Windows Task Scheduler via --once flag)
    # Task Scheduler handles the interval; we just run once per invocation.
    interval_min = int(cfg['agent'].get('interval_min', '5'))
    log.info(f'=== TallySync Agent started — interval: {interval_min} min ===')
    while True:
        try:
            run_sync(cfg)
        except Exception as e:
            log.error(f'Unexpected error in sync loop: {e}')
            log.error(traceback.format_exc())
        log.info(f'Sleeping {interval_min} minutes...')
        time.sleep(interval_min * 60)

if __name__ == '__main__':
    main()
