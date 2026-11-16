"""The UI lock queue (fair FIFO, position + ETA), cache-first reads while busy, and permission jobs that answer in budget."""
import json, os, pathlib, subprocess, sys, textwrap, time
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import core as C

REPO = pathlib.Path(__file__).resolve().parents[1]


def _ticket(pid, op, ns, job_id=None):
    C.UIQ.mkdir(parents=True, exist_ok=True)
    fp = C.UIQ / f'{C.op_priority(op)}-{ns:020d}-{pid}-1.json'
    fp.write_text(json.dumps({'pid': pid, 'op': op, 'since': C.now().isoformat(), 'job_id': job_id, 'priority': C.op_priority(op)})); return fp


@pytest.fixture
def clean_queue():
    for d in (C.UIQ,):
        if d.exists():
            for f in d.iterdir(): f.unlink()
    for f in (C.ROOT / 'ui.holder', C.OP_DURATIONS):
        try: f.unlink()
        except FileNotFoundError: pass
    yield
    if C.UIQ.exists():
        for f in C.UIQ.iterdir(): f.unlink()


def test_queue_orders_by_arrival_prunes_dead_and_adds_etas(clean_queue):
    dead = subprocess.Popen([sys.executable, '-c', 'pass']); dead.wait()
    _ticket(os.getpid(), 'send Demo primary', 3, job_id='sAAA')
    _ticket(dead.pid, 'read Demo primary', 1)                      # a waiter that died: pruned
    _ticket(os.getpid(), 'read Demo cmsg_X', 5)
    (C.ROOT / 'ui.holder').write_text(json.dumps({'pid': os.getpid(), 'op': 'attachment Demo primary 1.1', 'since': C.now().isoformat()}))
    q = C.ui_queue_status()
    assert [e['op'] for e in q['queue']] == ['send Demo primary', 'read Demo cmsg_X'] and q['length'] == 2
    assert [e['position'] for e in q['queue']] == [1, 2]
    # holder: attachment (45 s default) still to run; then the send (90 s default) ahead of the read
    assert q['queue'][0]['eta_s'] == pytest.approx(45, abs=2) and q['queue'][1]['eta_s'] == pytest.approx(135, abs=2)
    assert C.queue_entry_for(job_id='sAAA')['position'] == 1
    assert not any(str(dead.pid) in f.name for f in C.UIQ.iterdir())


def test_eta_learns_from_recorded_hold_times(clean_queue):
    C._record_op_duration('read Demo primary', 10.0)
    C._record_op_duration('read Other primary', 20.0)
    assert C.expected_op_seconds('read X') == pytest.approx(13.0)
    assert C.expected_op_seconds('mystery op') == 30.0


def test_lock_is_fifo_across_processes(clean_queue, tmp_path):
    """Three processes queue behind a holder; they get the lock in the order they arrived."""
    order = tmp_path / 'order.txt'
    code = textwrap.dedent(f'''
        import sys, time; sys.path.insert(0, {str(REPO)!r})
        from claude_projects import core as C
        with C.ui_lock('read Demo ' + sys.argv[1], force=True, timeout=60):
            open({str(order)!r}, 'a').write(sys.argv[1] + '\\n'); time.sleep(0.3)
    ''')
    env = dict(os.environ)
    with C.ui_lock('send Demo primary', force=True):
        procs = []
        for name in ('first', 'second', 'third'):
            procs.append(subprocess.Popen([sys.executable, '-c', code, name], env=env)); time.sleep(0.8)
        q = C.ui_queue_status()
        assert [e['op'] for e in q['queue']] == ['read Demo first', 'read Demo second', 'read Demo third']
        assert q['holder']['op'] == 'send Demo primary'
    for pr in procs: assert pr.wait(60) == 0
    assert order.read_text().split() == ['first', 'second', 'third']
    assert not list(C.UIQ.glob('*.json'))


def test_busy_timeout_names_queue_position(clean_queue):
    code = f"import sys,time; sys.path.insert(0,{str(REPO)!r})\nfrom claude_projects import core as C\nwith C.ui_lock('send Demo primary', force=True):\n    print('held', flush=True); time.sleep(6)"
    holder = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == 'held'
    t0 = time.time()
    with pytest.raises(C.Busy) as ei:
        with C.ui_lock('read Demo primary', force=True, timeout=1.5): pass
    assert time.time() - t0 < 5 and 'queue_position=1 of 1' in str(ei.value) and 'send Demo primary' in str(ei.value)
    holder.wait(20)


# ---- cache-first reads
def test_read_answers_from_cache_while_another_run_holds_the_app(monkeypatch, clean_queue):
    cached = {'project': 'Demo', 'project_id': 'chan_TEST', 'thread_id': 'primary', 'read_at': C.now().isoformat(), 'messages': [
        {'index': 1, 'role': 'assistant', 'text': 'hello', 'time_local': 'x'}]}
    monkeypatch.setattr(C, 'resolve_project', lambda p: {'id': 'chan_TEST', 'name': 'Demo', 'primary_thread_id': 'cse_TEST'})
    monkeypatch.setattr(C, 'resolve_thread', lambda chan, thread, ths=None: None)
    monkeypatch.setattr(C, 'read_cache', lambda chan, tid=None: dict(cached))
    monkeypatch.setattr(C, 'idb_env_fields_for_thread', lambda t: (None, None))
    monkeypatch.setattr(C, 'waiting_threads_for', lambda *a, **k: [])
    monkeypatch.setattr(C, 'ui_lock_holder', lambda: {'pid': 1, 'op': 'send Other primary', 'since': C.now().isoformat()})
    def no_lock(*a, **k): raise AssertionError('must not queue for the UI lock')
    monkeypatch.setattr(C, 'ui_lock', no_lock)
    r = C.op_read('Demo', None, True, cache_if_busy=True)
    assert r['served_from_cache_because_busy'] and r['from_cache'] and r['busy']['holder']['op'] == 'send Other primary'
    assert r['messages'][0]['text'] == 'hello' and r['busy']['eta_s'] > 0


# ---- permission jobs
def _perm_env(monkeypatch, tmp_path, worker_result=None, worker_delay=0.0):
    monkeypatch.setattr(C, 'PERM_JOBS', tmp_path / 'pj')
    monkeypatch.setattr(C, 'resolve_project', lambda p: {'id': 'chan_TEST', 'name': 'Demo', 'primary_thread_id': 'cse_TEST'})
    monkeypatch.setattr(C, 'log', lambda m: None)
    started = []
    def fake_spawn(argv, name):
        key = argv[-1]; started.append(key)
        st = C.job_read(C.PERM_JOBS, key)
        if worker_result is not None and worker_delay == 0:
            C.job_write(C.PERM_JOBS, key, dict(st, state='done', result=worker_result, finished_ts=time.time()))
        return 'process'
    monkeypatch.setattr(C, 'spawn_job', fake_spawn)
    return started


def test_permission_job_answers_within_budget_when_the_app_is_busy(monkeypatch, tmp_path, clean_queue):
    started = _perm_env(monkeypatch, tmp_path)
    t0 = time.time()
    r = C.op_permission_job('Demo', 'Allow for this session', 'cmsg_T', dry_run=False, only_apps=['Sketchpad', 'Notepad'], budget=1.0)
    assert time.time() - t0 < 3 and r['pending'] and r['pressed'] is None and r['job_id'] == started[0]
    assert r['diagnostics_file'].endswith(f"{r['job_id']}.diag.json")
    # the same call again joins the queued job instead of starting a second one
    r2 = C.op_permission_job('Demo', 'Allow for this session', 'cmsg_T', dry_run=False, only_apps=['Sketchpad', 'Notepad'], budget=0.5)
    assert r2['job_id'] == r['job_id'] and len(started) == 1


def test_permission_job_returns_the_result_and_never_presses_twice(monkeypatch, tmp_path, clean_queue):
    res = {'pressed': 'Continue', 'card_gone': True}
    started = _perm_env(monkeypatch, tmp_path, worker_result=res)
    r = C.op_permission_job('Demo', 'Continue', 'cmsg_T', dry_run=False, budget=2.0)
    assert r['pressed'] == 'Continue' and r['card_gone'] and len(started) == 1
    again = C.op_permission_job('Demo', 'Continue', 'cmsg_T', dry_run=False, budget=2.0)
    assert again['joined_existing_job'] and len(started) == 1
    assert C.op_permission_job('Demo', '-', job_id=r['job_id'])['card_gone']


def test_permission_worker_writes_a_diagnostics_timeline(monkeypatch, tmp_path, clean_queue):
    _perm_env(monkeypatch, tmp_path)
    key = 'ptest'
    C.job_write(C.PERM_JOBS, key, {'job_id': key, 'state': 'queued', 'started_ts': time.time(),
                                   'spec': {'project': 'chan_TEST', 'option': 'Deny', 'thread': 'cmsg_T', 'dry_run': True}})
    def fake_perm(*a, **k):
        C._pdiag('lock_acquired'); C._pdiag('cards_read', cards=[]); return {'dry_run': True, 'would_press': 'Deny'}
    monkeypatch.setattr(C, 'op_permission', fake_perm)
    r = C.run_permission_job(key)
    assert r['would_press'] == 'Deny'
    d = json.loads((C.PERM_JOBS / f'{key}.diag.json').read_text())
    assert d['state'] == 'done' and [x['step'] for x in d['timeline']] == ['lock_acquired', 'cards_read'] and d['timeline'][0]['at_local']


def test_permission_worker_failure_is_recorded(monkeypatch, tmp_path, clean_queue):
    _perm_env(monkeypatch, tmp_path)
    key = 'pfail'
    C.job_write(C.PERM_JOBS, key, {'job_id': key, 'state': 'queued', 'started_ts': time.time(), 'spec': {'project': 'chan_TEST', 'option': 'Deny'}})
    def boom(*a, **k): raise C.NotFound('no permission card is showing in this thread')
    monkeypatch.setattr(C, 'op_permission', boom)
    r = C.run_permission_job(key)
    assert r['state'] == 'failed' and r['error_type'] == 'NotFound'
    d = json.loads((C.PERM_JOBS / f'{key}.diag.json').read_text())
    assert d['timeline'][-1]['step'] == 'error'


def test_send_pending_answer_carries_queue_position(monkeypatch, clean_queue):
    _ticket(os.getpid(), 'send Demo primary', 7, job_id='sJOB')
    r = C._send_answer({'job_id': 'sJOB', 'state': 'queued'})
    assert r['pending'] and r['queue_position'] == 1 and r['busy'] == 'busy, queued as job sJOB'


def test_urgent_lane_runs_before_older_bulk_and_normal(clean_queue):
    _ticket(os.getpid(), 'attachment Demo primary 1.1', 1)
    _ticket(os.getpid(), 'read Demo primary', 2)
    C._ticket_new('permission Demo cmsg_X')
    q = C.ui_queue_status()['queue']
    assert [e['op'].split()[0] for e in q] == ['permission', 'read', 'attachment'] and q[0]['lane'] == 0


def test_bulk_context_puts_refresh_reads_in_bulk_lane():
    assert C.op_priority('read Demo primary') == 1
    old = C._BULK_CTX
    try:
        C._BULK_CTX = True; assert C.op_priority('read Demo primary') == 2 and C.op_priority('permission Demo x') == 0
    finally: C._BULK_CTX = old


def test_checkpoint_steps_aside_only_for_urgent(monkeypatch):
    calls = []
    monkeypatch.setattr(C, '_HELD', {'f': object(), 'op': 'attachment Demo primary 1.1', 'since': time.time()})
    monkeypatch.setattr(C, 'urgent_waiting', lambda: [{'op': 'permission Demo cmsg_X'}])
    monkeypatch.setattr(C, '_release_flock', lambda f: calls.append('release'))
    monkeypatch.setattr(C, '_acquire_flock', lambda op, prio, timeout, t0: calls.append(('acquire', prio)) or object())
    monkeypatch.setattr(C, '_record_op_duration', lambda *a: None)
    monkeypatch.setattr(C, 'log', lambda m: None)
    assert C.ui_checkpoint(lambda: calls.append('restore'), 'test') >= 0
    assert calls == ['release', ('acquire', 1), 'restore']
    calls.clear(); monkeypatch.setattr(C, 'urgent_waiting', lambda: [])
    assert C.ui_checkpoint(lambda: calls.append('restore')) == 0 and calls == []


class _Ext:
    def __init__(self, x, y, w, h): self.x, self.y, self.width, self.height = x, y, w, h
class _Comp:
    def __init__(self, ext): self.ext = ext
    def get_extents(self, kind): return self.ext
class _Btn:
    def __init__(self, ext): self.ext = ext
    def get_component_iface(self): return _Comp(self.ext)


def test_real_click_hits_the_button_centre(monkeypatch):
    clicks = []
    monkeypatch.setattr(C, '_click_xy', lambda x, y, **k: clicks.append((x, y)))
    monkeypatch.setattr(C, 'log', lambda m: None); monkeypatch.setattr(C, 'nm', lambda e: 'Allow for this session')
    assert C.real_click(_Btn(_Ext(137, 909, 166, 28))) == 'click' and clicks == [(220.0, 923.0)]   # real3.json: clicked 220,923


def test_real_click_falls_back_to_action_without_a_box(monkeypatch):
    monkeypatch.setattr(C, '_click_xy', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no click')))
    monkeypatch.setattr(C, 'act', lambda e, what='': True)
    assert C.real_click(_Btn(_Ext(0, 0, 0, 0))) == 'action'


def test_walk_is_time_bounded(monkeypatch):
    monkeypatch.setattr(C, 'kids', lambda e: [e + 1])        # endless tree
    t0 = time.time(); n = sum(1 for _ in C.walk(0, 10**9, max_s=0.3))
    assert time.time() - t0 < 1.5 and n > 0
