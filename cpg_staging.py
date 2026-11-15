"""Managed staging / cache directory for files moving into and out of Claude Code threads.

Layout (all under <state_dir>/staging unless `staging_dir` is configured):

    staging/
      incoming/     files fetched FROM threads (claude_thread_attachment_get with as=path, forwards)
      outgoing/     files staged TO be attached (base64 uploads, forwarded attachments); one unique subfolder per send
      attach-test/  OCR crops from --attach-test dry runs
      msg/          temporary message / attachment-list files the MCP server hands to the CLI

Every staged file lives in its own unique subfolder (<UTC stamp>-<random>/<original name>), so two sends with the same
file name never overwrite each other and the original file name (which the app shows on the attachment chip) is kept.

Cleanup: sweep() deletes entries older than their TTL and then trims the oldest entries until the whole tree is under
`staging_max_mb`. It runs automatically at most once per `staging_sweep_interval_min` (from send / attachment / forward)
and on demand through the claude_staging tool. Nothing outside the staging root is ever touched.
"""
import os, time, json, shutil, secrets, pathlib, datetime

KINDS = ('incoming', 'outgoing', 'attach-test', 'msg')
DEFAULT_TTL_H = {'incoming': 72, 'outgoing': 24, 'attach-test': 6, 'msg': 2}


class Staging:
    def __init__(self, root, ttl_hours=None, max_mb=2048, sweep_interval_min=60):
        self.root = pathlib.Path(root).expanduser()
        self.ttl = dict(DEFAULT_TTL_H); self.ttl.update({k: float(v) for k, v in (ttl_hours or {}).items() if k in KINDS})
        self.max_bytes = int(float(max_mb) * 2**20)
        self.interval_s = float(sweep_interval_min) * 60

    def dir(self, kind):
        if kind not in KINDS: raise ValueError(f'unknown staging kind {kind!r}; one of {KINDS}')
        d = self.root/kind; d.mkdir(parents=True, exist_ok=True)
        try: os.chmod(self.root, 0o700)
        except Exception: pass
        return d

    def new_slot(self, kind):
        """A fresh, unique folder for one operation: <kind>/<UTC stamp>-<random hex>/"""
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        d = self.dir(kind)/f'{stamp}-{secrets.token_hex(4)}'
        d.mkdir(parents=True, exist_ok=False)
        return d

    @staticmethod
    def safe_name(name, fallback='file.bin'):
        n = os.path.basename(str(name or '')).replace('\x00', '').strip()
        return n if n and n not in ('.', '..') else fallback

    def write_bytes(self, kind, name, data):
        p = self.new_slot(kind)/self.safe_name(name); p.write_bytes(data); return p

    def copy_in(self, kind, src, name=None):
        src = pathlib.Path(src)
        p = self.new_slot(kind)/self.safe_name(name or src.name)
        shutil.copy2(src, p); return p

    def contains(self, path):
        try: pathlib.Path(path).resolve().relative_to(self.root.resolve()); return True
        except Exception: return False

    # ------------------------------------------------------------ inspection / cleanup
    def _entries(self):
        out = []
        for kind in KINDS:
            d = self.root/kind
            if not d.is_dir(): continue
            for e in d.iterdir():
                try:
                    st = e.stat()
                    size = st.st_size if e.is_file() else sum(f.stat().st_size for f in e.rglob('*') if f.is_file())
                    out.append({'kind': kind, 'path': str(e), 'mtime': st.st_mtime, 'size': size})
                except FileNotFoundError: pass
        return out

    def info(self):
        ents = self._entries(); by = {}
        for k in KINDS:
            ks = [e for e in ents if e['kind'] == k]
            by[k] = {'path': str(self.root/k), 'entries': len(ks), 'bytes': sum(e['size'] for e in ks), 'ttl_hours': self.ttl[k]}
        return {'root': str(self.root), 'kinds': by, 'total_bytes': sum(e['size'] for e in ents), 'max_bytes': self.max_bytes,
                'last_sweep': self._last_sweep()}

    def _marker(self): return self.root/'.last-sweep'
    def _last_sweep(self):
        try: return json.loads(self._marker().read_text())
        except Exception: return None

    def sweep(self, dry_run=False, older_than_hours=None, now=None, kinds=None):
        """Delete expired entries, then oldest-first until under max size. Returns what was (or would be) removed."""
        now = time.time() if now is None else now
        kinds = [k for k in (kinds or KINDS) if k in KINDS]
        ents = [e for e in self._entries() if e['kind'] in kinds]
        removed = []
        for e in ents:
            ttl_h = self.ttl[e['kind']] if older_than_hours is None else float(older_than_hours)
            if now - e['mtime'] > ttl_h * 3600: removed.append(dict(e, reason='expired'))
        gone = {e['path'] for e in removed}
        left = sorted([e for e in self._entries() if e['path'] not in gone], key=lambda e: e['mtime'])
        total = sum(e['size'] for e in left)
        while left and total > self.max_bytes:
            e = left.pop(0)
            if e['kind'] not in kinds: continue
            removed.append(dict(e, reason='over_size_cap')); total -= e['size']
        if not dry_run:
            for e in removed:
                p = pathlib.Path(e['path'])
                if not self.contains(p): continue  # never leave the staging root
                if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
                else:
                    try: p.unlink()
                    except FileNotFoundError: pass
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                self._marker().write_text(json.dumps({'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'removed': len(removed)}))
            except Exception: pass
        return {'dry_run': bool(dry_run), 'removed': [{'path': e['path'], 'kind': e['kind'], 'bytes': e['size'], 'reason': e['reason']} for e in removed],
                'removed_count': len(removed), 'freed_bytes': sum(e['size'] for e in removed), 'root': str(self.root)}

    def maybe_sweep(self):
        """Automatic sweep, throttled to once per sweep interval. Never raises."""
        try:
            m = self._marker()
            if m.exists() and time.time() - m.stat().st_mtime < self.interval_s: return None
            return self.sweep()
        except Exception: return None


def from_config(cfg):
    root = cfg.get('staging_dir') or str(pathlib.Path(cfg['state_dir'])/'staging')
    return Staging(root, cfg.get('staging_ttl_hours') or {}, cfg.get('staging_max_mb') or 2048, cfg.get('staging_sweep_interval_min') or 60)
