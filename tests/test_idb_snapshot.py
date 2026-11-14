"""IndexedDB snapshots: one reader at a time, queued callers reuse its result, and project lookups accept a recent one."""
import json, pathlib, subprocess, sys, threading, time
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
import ccp_core as C

SNAP = {'channels': [{'id': 'chan_A', 'name': 'Docs'}], 'sessions': []}


@pytest.fixture
def reader(tmp_path, monkeypatch):
    """Fake ccp_idb.py: counts runs, takes `delay` seconds, returns `snap`."""
    monkeypatch.setattr(C, 'CACHE', tmp_path)
    st = {'runs': 0, 'delay': 0.0, 'snap': SNAP, 'active': 0, 'max_active': 0}
    lock = threading.Lock()
    def fake_run(cmd, **kw):
        with lock: st['runs'] += 1; st['active'] += 1; st['max_active'] = max(st['max_active'], st['active'])
        time.sleep(st['delay'])
        with lock: st['active'] -= 1
        return subprocess.CompletedProcess(cmd, 0, json.dumps(st['snap']), '')
    monkeypatch.setattr(C.subprocess, 'run', fake_run)
    return st


def test_fresh_read_by_default(reader):
    C.idb_snapshot(); C.idb_snapshot()
    assert reader['runs'] == 2


def test_max_age_reuses_recent_snapshot(reader):
    C.idb_snapshot(); C.idb_snapshot(max_age=30)
    assert reader['runs'] == 1


def test_concurrent_readers_coalesce(reader):
    reader['delay'] = 0.4
    out = []
    ts = [threading.Thread(target=lambda: out.append(C.idb_snapshot())) for _ in range(5)]
    for t in ts: t.start(); time.sleep(0.02)
    for t in ts: t.join()
    assert len(out) == 5 and all(o == SNAP for o in out)
    assert reader['max_active'] == 1          # never two deserializations at once
    assert reader['runs'] <= 2                # callers queued behind a run reuse it


def test_resolve_project_uses_recent_snapshot_then_falls_back(reader):
    C.idb_snapshot()
    reader['snap'] = {'channels': [{'id': 'chan_A', 'name': 'Docs'}, {'id': 'chan_B', 'name': 'Brand New'}], 'sessions': []}
    assert C.resolve_project('Docs')['id'] == 'chan_A' and reader['runs'] == 1     # served from the recent snapshot
    assert C.resolve_project('Brand New')['id'] == 'chan_B' and reader['runs'] == 2  # miss -> fresh read
