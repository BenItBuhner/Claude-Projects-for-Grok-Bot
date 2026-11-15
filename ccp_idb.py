"""ccp_idb.py - print JSON {channels:[...], sessions:[...], channels_updated, sessions_updated} from a COPY of the
Claude desktop IndexedDB (react-query persisted cache). Read-only; skips account/billing/secrets.
Run with the python configured as idb_python (needs ccl_chromium_reader; install.sh sets it up)."""
import sys, os, io, json, re, shutil, tempfile, pathlib, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ccp_config import CFG
from ccl_chromium_reader import ccl_chromium_indexeddb as idb
from ccl_chromium_reader.serialization_formats import ccl_v8_value_deserializer as v8d, ccl_blink_value_deserializer as bd
import ccl_simplesnappy
BAD = re.compile(r'token|secret|cookie|auth|password|email|phone|prompt|billing|payment|api_?key', re.I)
SRC = pathlib.Path(CFG['claude_config_dir'])/'IndexedDB'
def txt(b):
    try: return b.decode('utf-8')
    except UnicodeDecodeError: return b.decode('latin-1')
def clean(o):
    if isinstance(o, dict): return {txt(k) if isinstance(k, (bytes, bytearray)) else str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [clean(v) for v in o]
    if isinstance(o, (bytes, bytearray)): return txt(bytes(o))
    if isinstance(o, (str, int, float, bool)) or o is None: return o
    return str(o)
def flat(d, pre=''):
    r = {}
    for k, v in (d or {}).items():
        k = str(k)
        if BAD.search(k): continue
        if isinstance(v, dict) and not pre: r.update(flat(v, k + '.'))
        elif isinstance(v, (str, int, float, bool)) or v is None: r[pre + k] = v[:300] if isinstance(v, str) else v
    return r
def iso(ms):
    try: return datetime.datetime.fromtimestamp(ms/1000, datetime.timezone.utc).isoformat() if isinstance(ms, (int, float)) else None
    except Exception: return None
def main():
    sd = pathlib.Path(CFG['state_dir']); sd.mkdir(parents=True, exist_ok=True); os.chmod(sd, 0o700)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='.ccp-idb-', dir=str(sd)))
    try:
        for d in SRC.iterdir():
            if d.name.startswith('https_claude.ai_0'): shutil.copytree(d, tmp/d.name, ignore=shutil.ignore_patterns('LOCK'))
        lv, bl = tmp/'https_claude.ai_0.indexeddb.leveldb', tmp/'https_claude.ai_0.indexeddb.blob'
        w = idb.WrappedIndexDB(lv, bl)
        pick = lambda v: v['clientState']['queries'] if isinstance(v, dict) and 'clientState' in v else None
        qs = None
        for dd in w.database_ids:
            if dd.name != 'keyval-store': continue
            st = w[dd.dbid_no].get_object_store_by_name('keyval')
            for r in st._raw_db.iterate_records(st._dbid_no, st._obj_store_id, live_only=True, bad_deserializer_data_handler=lambda k, b: None):
                try:
                    if (q := pick(r.value)): qs = q; break
                except Exception: pass
        if qs is None and bl.exists():
            for f in sorted([p for p in bl.rglob('*') if p.is_file()], key=lambda p: -p.stat().st_mtime):
                b = f.read_bytes()
                if b[:3] != b'\xff\x11\x02': continue
                try: raw = ccl_simplesnappy.decompress(io.BytesIO(b[3:]))
                except Exception: continue
                for off in range(24):
                    try:
                        v = v8d.Deserializer(io.BytesIO(raw[off:]), host_object_delegate=bd.BlinkV8Deserializer().read).read()
                        if (q := pick(v)): qs = q; break
                    except Exception: pass
                if qs: break
        out = {'channels': [], 'sessions': [], 'channels_updated': None, 'sessions_updated': None, 'read_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        for q in qs or []:
            k = q.get('queryKey') or []; name = k[0] if k else ''
            st = q.get('state') or {}
            if name == 'cc-project-channels':
                d = clean(st.get('data')); items = d if isinstance(d, list) else (d or {}).get('channels') or (d or {}).get('data') or []
                out['channels'] = [flat(i) for i in items]; out['channels_updated'] = iso(st.get('dataUpdatedAt'))
                out['channels_query_status'] = st.get('status') if isinstance(st.get('status'), str) else None
            elif name == 'sessions_api_list_sessions':
                d = clean(st.get('data')); pages = d.get('pages') if isinstance(d, dict) and 'pages' in d else [d]
                for p in pages:
                    arr = p if isinstance(p, list) else next((p[kk] for kk in ('data', 'sessions', 'items') if isinstance(p, dict) and isinstance(p.get(kk), list)), [])
                    out['sessions'] += [flat(i) for i in arr]
                out['sessions_updated'] = iso(st.get('dataUpdatedAt'))
        json.dump(out, sys.stdout, ensure_ascii=False, default=str)
    finally: shutil.rmtree(tmp, ignore_errors=True)
main()
