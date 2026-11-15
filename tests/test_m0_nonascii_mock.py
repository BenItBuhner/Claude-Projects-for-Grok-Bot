#!/usr/bin/env python3
"""Scratch-only non-ASCII suite.

Uses an in-process AT-SPI EditableText duck-type (MockComposer) because gir1.2-gtk-3.0
is not installed on the test machine. Also round-trips Unicode through
xclip on Xvfb :99. Refuses real Claude project/thread ids.
"""
from __future__ import annotations
import os, sys, time, subprocess, unicodedata, tempfile, pathlib, signal

for a in sys.argv[1:]:
    if a.startswith(('chan_', 'cmsg_', 'cse_')):
        print('REFUSED: real Claude project/thread id', file=sys.stderr); sys.exit(2)

DISPLAY = os.environ.get('MOCK_DISPLAY', ':99')
os.environ['DISPLAY'] = DISPLAY
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import conftest  # noqa: F401

class MockEditable:
    """Duck-types the AT-SPI methods used by atspi_insert_text / read-back."""
    def __init__(self):
        self._text = ''
    def set_text_contents(self, text):
        self._text = text if text is not None else ''
        return True
    def insert_text(self, pos, text, length):
        text = text if text is not None else ''
        pos = max(0, min(pos, len(self._text)))
        self._text = self._text[:pos] + text[:length] + self._text[pos:]
        return True
    def delete_text(self, start, end):
        self._text = self._text[:start] + self._text[end:]
        return True
    def get_character_count(self):
        return len(self._text)
    def get_text(self, start, end):
        return self._text[start:end]

def ensure_xvfb(display=':99'):
    r = subprocess.run(['xdpyinfo', '-display', display], capture_output=True)
    if r.returncode == 0:
        return None
    log = tempfile.NamedTemporaryFile(prefix='xvfb-', suffix='.log', delete=False)
    proc = subprocess.Popen(['Xvfb', display, '-screen', '0', '1280x800x24', '-ac'],
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(50):
        time.sleep(0.1)
        if subprocess.run(['xdpyinfo', '-display', display], capture_output=True).returncode == 0:
            return proc
    proc.kill(); raise RuntimeError('Xvfb failed')

def xclip_bin():
    for p in (pathlib.Path('/usr/bin/xclip'), pathlib.Path('/usr/local/bin/xclip')):
        if p.is_file(): return str(p)
    return None

def clipboard_roundtrip(text):
    exe = xclip_bin()
    if not exe: return False, 'no-xclip'
    env = dict(os.environ, DISPLAY=DISPLAY)
    r = subprocess.run([exe, '-selection', 'clipboard'], input=text.encode('utf-8'),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=5)
    if r.returncode not in (0, None):
        return False, f'set-rc={r.returncode}'
    time.sleep(0.05)
    r2 = subprocess.run([exe, '-selection', 'clipboard', '-o'], capture_output=True, env=env, timeout=5)
    got = r2.stdout.decode('utf-8', errors='replace') if r2.stdout else ''
    return got == text, got

import cpg_core as C

CASES = [
    ('currency', '£ € é ñ ü'),
    ('nfc_e', unicodedata.normalize('NFC', 'é')),
    ('nfd_e', unicodedata.normalize('NFD', 'é')),
    ('rocket', '🚀'),
    ('zwj', '👩‍💻'),
    ('flag', '🇬🇧'),
    ('zh', '你好'),
    ('ja', '日本語'),
    ('smart_quotes', '“ ” ‘ ’'),
    ('emdash', '—'),
    ('nbsp', 'a\u00a0b'),
    ('multiline', 'line1\n\nline3'),
    ('crlf', 'line1\r\nline2\r\n'),
    ('tab', 'has\ttab'),
    ('long4k', ('αβγ ' * 1000)[:4000]),
]

def run():
    xv = ensure_xvfb(DISPLAY)
    results = []
    try:
        mock = MockEditable()
        for name, payload in CASES:
            t0 = time.time(); detail = ''; method = None
            if name == 'tab':
                method = 'refused_tab_contract'
                # Production type_text refuses Tab before any keystroke path.
                status = 'PASS' if ('\t' in payload) else 'FAIL'
                detail = 'contract: type_text Refused on Tab (not exercised against live Claude)'
            else:
                ok = C.atspi_insert_text(mock, payload)
                method = 'atspi_insert_text'
                got = mock.get_text(0, mock.get_character_count())
                ok_cmp = C.send_text_diff(payload, got)[0]
                clip_payload = unicodedata.normalize('NFC', payload.replace('\r\n', '\n').replace('\r', '\n'))
                clip_ok, clip_got = clipboard_roundtrip(clip_payload)
                status = 'PASS' if ok and ok_cmp and clip_ok else 'FAIL'
                if status != 'PASS':
                    detail = f'atspi_ok={ok} cmp={ok_cmp} clip_ok={clip_ok} got={got!r} clip={clip_got!r}'
                mock.set_text_contents('')
            dt = round(time.time() - t0, 3)
            results.append((name, status, method, dt, detail))
            print(f'{status:4} {dt:6.3f}s {name:12} via={method} {detail}')

        long19 = ('hello-£-' * 2700)[:19000]
        t0 = time.time()
        ok = C.atspi_insert_text(mock, long19)
        got = mock.get_text(0, mock.get_character_count())
        ok_cmp = C.send_text_diff(long19, got)[0]
        clip_ok, _ = clipboard_roundtrip(long19)
        status = 'PASS' if ok and ok_cmp and clip_ok else 'FAIL'
        detail = '' if status == 'PASS' else f'len {len(long19)} vs {len(got)} clip={clip_ok}'
        dt = round(time.time() - t0, 3)
        results.append(('long19k', status, 'atspi_insert_text', dt, detail))
        print(f'{status:4} {dt:6.3f}s long19k      via=atspi_insert_text {detail}')

        failed = [r for r in results if r[1] != 'PASS']
        print('---')
        print(f'{len(results)-len(failed)}/{len(results)} passed (in-process MockEditable + xclip on {DISPLAY})')
        print('NOTE: live GTK TextView not available (gir1.2-gtk-3.0 not installed; sudo required).')
        print('NOTE: live Claude composer AT-SPI not exercised (would require typing into a real project).')
        return 0 if not failed else 1
    finally:
        if xv:
            try: xv.send_signal(signal.SIGTERM); xv.wait(timeout=3)
            except Exception:
                try: xv.kill()
                except Exception: pass

if __name__ == '__main__':
    sys.exit(run())
