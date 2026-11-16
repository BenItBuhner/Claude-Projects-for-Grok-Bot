#!/usr/bin/env python3
"""Download of full-size originals: loading-pane detection, Save as file names, on-disk store."""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_projects import core as C


def test_loading_text_is_not_content():
    for s in ('Loading file', 'Loading', 'Loading\u2026', '  loading file  ', 'Loading...'):
        assert C.is_loading_text(s), s
    for s in ('', None, 'Loading files is covered in step 2', '# README', 'x'):
        assert not C.is_loading_text(s), s


def test_save_target_is_ascii_and_in_dir():
    d = Path('/tmp/dl')
    assert C.save_target(d, 'demo-16x9.mp4') == d / 'demo-16x9.mp4'
    t = C.save_target(d, 'Café poster (1).jpg')
    assert t.parent == d and t.name.isascii() and t.suffix == '.jpg' and '/' not in t.name
    assert C.save_target(d, '../../etc/passwd').parent == d
    assert C.save_target(d, '').name == 'download.bin'


def test_store_file_moves_and_indexes(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix='claude-projects-dl-test-'))
    monkeypatch.setattr(C, 'ATT_DIR', tmp / 'attachments')
    src_dir = tmp / 'attachments' / 'incoming' / '1'
    src_dir.mkdir(parents=True)
    src = src_dir / 'clip.mp4'
    body = b'\x00\x00\x00\x18ftypmp42' + bytes(range(256)) * 4000
    src.write_bytes(body)
    meta = {'project_id': 'chan_TEST', 'thread_id': 'primary', 'message_index': 3, 'name': 'clip.mp4', 'mime': 'video/mp4'}
    path, sha, size = C.store_file(str(src), 'clip.mp4', 'video/mp4', meta)
    assert sha == hashlib.sha256(body).hexdigest()
    assert size == len(body) and Path(path).read_bytes() == body
    assert Path(path).suffix == '.mp4' and not src.exists() and not src_dir.exists()
    side = json.loads((C.ATT_DIR / f'{sha[:32]}.json').read_text())
    assert side['size'] == len(body) and side['path_on_host'] == path
    ix = json.loads((C.ATT_DIR / 'index.json').read_text())
    assert ix['chan_TEST|primary|3|clip.mp4']['sha256'] == sha
    # same bytes again: the duplicate is dropped, the stored copy stays
    src_dir.mkdir(parents=True)
    src.write_bytes(body)
    path2, sha2, _ = C.store_file(str(src), 'clip.mp4', 'video/mp4', meta)
    assert (path2, sha2) == (path, sha) and not src.exists()


def test_parallel_stores_keep_every_index_entry_and_never_fail(tmp_path):
    """Several attachment workers store at once (after the UI lock is released): no worker fails on another's temp
    file, the same bytes stored twice don't collide, and the index keeps every entry."""
    import os, subprocess, sys
    att = tmp_path / 'attachments'; n = 24
    code = ("import sys, pathlib; from claude_projects import core as C; C.ATT_DIR = pathlib.Path(sys.argv[1]); i = int(sys.argv[2]); "
            "data = (b'same' * 1000) if i % 2 else (b'x%d' % i) * 1000; "
            "C.store_bytes(data, f'f{i}.txt', 'text/plain', {'project_id': 'chan_P', 'thread_id': 'primary', 'message_index': i, 'name': f'f{i}.txt'})")
    root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get('PYTHONPATH', ''))
    ps = [subprocess.Popen([sys.executable, '-c', code, str(att), str(i)], env=env, stderr=subprocess.PIPE, text=True) for i in range(n)]
    errs = {i: p.communicate(timeout=60)[1] for i, p in enumerate(ps) if p.wait(timeout=60) != 0}
    assert not errs, errs
    ix = json.loads((att / 'index.json').read_text())
    assert {f'chan_P|primary|{i}|f{i}.txt' for i in range(n)} <= set(ix), 'index lost updates'
    assert not list(att.glob('*.tmp')), 'temp files left behind'


def test_prune_never_touches_files_being_written(monkeypatch, tmp_path):
    monkeypatch.setattr(C, 'ATT_DIR', tmp_path / 'attachments'); C.ATT_DIR.mkdir()
    monkeypatch.setattr(C, 'ATT_TOTAL_CAP', 10)
    (C.ATT_DIR / 'abc.bin.123-4.tmp').write_bytes(b'x' * 100)        # another worker mid-write
    (C.ATT_DIR / 'old.bin').write_bytes(b'y' * 100)
    C._att_prune()
    assert (C.ATT_DIR / 'abc.bin.123-4.tmp').exists() and not (C.ATT_DIR / 'old.bin').exists()
