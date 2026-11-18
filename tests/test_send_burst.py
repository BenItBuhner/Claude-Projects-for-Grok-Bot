"""Send bursts: the lock deadline answers with a queue status, a queued send answers fast and still goes out once,
waiters for the target the app already shows go first (bounded so nobody starves), and no trip home while others wait."""
import os, pathlib, subprocess, sys, textwrap, threading, time
import pytest
from claude_projects import core as C

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_queue():
    def wipe():
        if C.UIQ.exists():
            for f in C.UIQ.iterdir(): f.unlink()
        for f in (C.ROOT / 'ui.holder', C.OP_DURATIONS, C.UI_LAST):
            try: f.unlink()
            except FileNotFoundError: pass
    wipe(); C._SHOWN['target'] = None
    yield
    wipe(); C._SHOWN['target'] = None


def _t(op, target, ts, pid=None):
    return (pathlib.Path(f'/x/{ts}'), {'pid': pid or os.getpid(), 'op': op, 'priority': C.op_priority(op), 'target': target, 'ts': ts})


def dict_t(x): return (x[0], dict(x[1], target=None))


def test_service_order_batches_the_shown_project_but_never_starves_the_oldest():
    now = 1000.0
    a = _t('send A primary', 'chan_A|primary', now - 10)       # oldest
    b = _t('send B primary', 'chan_B|primary', now - 8)
    c = _t('send C primary', 'chan_C|primary', now - 6)
    b2 = _t('send B cmsg_1', 'chan_B|cmsg_1', now - 4)
    b3 = _t('send B primary', 'chan_B|primary', now - 2)
    live = [a, b, c, b2, b3]
    # the app shows chan_B primary: both chan_B primary sends first, then the other chan_B thread, then FIFO
    order = [x[1]['op'] + '@' + str(x[1]['ts']) for x in C._service_order(live, 'chan_B|primary', now)]
    assert order == ['send B primary@992.0', 'send B primary@998.0', 'send B cmsg_1@996.0', 'send A primary@990.0', 'send C primary@994.0']
    # nothing shown: the oldest first; once B is served the app shows B, so B's other waiters follow it
    assert [x[1]['ts'] for x in C._service_order(live, None, now)] == [990.0, 992.0, 998.0, 996.0, 994.0]
    # no targets at all (reads, older tickets): plain FIFO
    assert [x[1]['ts'] for x in C._service_order([dict_t(x) for x in live], None, now)] == [990.0, 992.0, 994.0, 996.0, 998.0]
    # the oldest has waited AFFINITY_MAX_S: it goes first however the app is left
    assert C._service_order(live, 'chan_B|primary', now - 10 + C.AFFINITY_MAX_S + 1)[0] is a
    # lanes still come first: an urgent send beats an older read for the shown target
    r = _t('read B primary', 'chan_B|primary', now - 20)
    assert C._service_order([r, c], 'chan_B|primary', now)[0] is c


def test_lock_deadline_raises_busy_with_a_queue_status(clean_queue):
    code = (f"import sys,time; sys.path.insert(0,{str(REPO)!r})\nfrom claude_projects import core as C\n"
            "with C.ui_lock('send Demo primary', force=True):\n    print('held', flush=True); time.sleep(8)")
    holder = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        t0 = time.time()
        with pytest.raises(C.Busy) as ei:
            with C.ui_lock('send Other primary', force=True, timeout=1.5, target='chan_O|primary'): pass
        assert time.time() - t0 < 5
        q = ei.value.queue
        assert ei.value.lock_deadline is True
        assert q['queued'] is True and q['queue_position'] == 1 and q['queue_length'] == 1
        assert q['waited_s'] >= 1.5 and 5 <= q['retry_after_s'] <= 120 and q['eta_s'] >= 5
        assert 'retry_after_s=' in str(ei.value)
        assert not list(C.UIQ.glob('*.json'))           # the ticket is gone: nothing left waiting
    finally:
        holder.kill(); holder.wait(10)


def test_waiter_for_the_shown_project_goes_first_across_processes(clean_queue, tmp_path):
    order = tmp_path / 'order.txt'
    code = textwrap.dedent(f'''
        import sys, time; sys.path.insert(0, {str(REPO)!r})
        from claude_projects import core as C
        with C.ui_lock('send ' + sys.argv[1], force=True, timeout=60, target=sys.argv[2]):
            open({str(order)!r}, 'a').write(sys.argv[1] + '\\n'); time.sleep(0.3)
    ''')
    with C.ui_lock('send Alpha primary', force=True, target='chan_A|primary'):
        C._SHOWN['target'] = 'chan_A|primary'            # this op left the app on Alpha (no trip home: others wait)
        procs = []
        for name, tgt in (('Beta', 'chan_B|primary'), ('Alpha2', 'chan_A|primary')):
            procs.append(subprocess.Popen([sys.executable, '-c', code, name, tgt])); time.sleep(0.8)
        q = C.ui_queue_status()
        assert [e['op'] for e in q['queue']] == ['send Beta', 'send Alpha2']   # no last target yet: arrival order
    for pr in procs: assert pr.wait(60) == 0
    assert order.read_text().split() == ['Alpha2', 'Beta']   # Alpha2 reuses the open Project, Beta waits one turn


def test_leave_app_skips_the_trip_home_only_while_others_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(C, 'go_home', lambda: calls.append('home') or True)
    monkeypatch.setattr(C, 'others_waiting', lambda: [{'op': 'send B primary'}])
    assert C.leave_app('test') is False and calls == []
    monkeypatch.setattr(C, 'others_waiting', lambda: [])
    assert C.leave_app('test') is True and calls == ['home']


# ---------------------------------------------------------------- the job layer

@pytest.fixture
def jobs(monkeypatch, tmp_path):
    st = {'sends': 0, 'gate': threading.Event(), 'raise': None}
    monkeypatch.setattr(C, 'SEND_JOBS', tmp_path / 'send-jobs')
    monkeypatch.setattr(C, 'SEND_RETRY_BACKOFF', (0.0, 0.0))
    monkeypatch.setattr(C, 'SEND_REVERIFY_DELAYS', (0.0, 0.0))
    monkeypatch.setattr(C, 'service_limit_recent', lambda *a, **k: None)
    monkeypatch.setattr(C, 'find_existing_post', lambda *a, **k: None)
    monkeypatch.setattr(C.STAGE, 'maybe_sweep', lambda: None)
    monkeypatch.setattr(C, 'resolve_project', lambda q, snap=None: {'id': 'chan_E', 'name': 'Demo'})
    monkeypatch.setattr(C, 'log', lambda *x, **k: None)

    def fake_send(project, message, thread=None, **k):
        C._progress('queued')                            # waiting for the UI lock
        if st['raise']: raise st['raise']
        assert st['gate'].wait(10)
        C._progress('sending'); st['sends'] += 1
        return {'sent': True, 'verified_in_transcript': True, 'message_index': 140}
    monkeypatch.setattr(C, 'op_send', fake_send)
    threads = []
    def spawn(argv, name):
        t = threading.Thread(target=C.run_send_job, args=(argv[-1],), daemon=True); threads.append(t); t.start(); return 'thread'
    monkeypatch.setattr(C, 'spawn_job', spawn)
    yield st
    st['gate'].set()
    for t in threads: t.join(5)


def test_queued_send_answers_fast_with_its_place_and_still_sends_once(jobs, monkeypatch):
    monkeypatch.setattr(C, 'SEND_QUEUED_ANSWER_S', 0.5)
    monkeypatch.setattr(C, 'queue_entry_for', lambda pid=None, job_id=None: {'position': 3, 'eta_s': 95.0, 'length': 4})
    t0 = time.time(); r = C.op_send_job('Demo', 'hello', budget=40)
    assert time.time() - t0 < 3, 'a queued send must not hold the caller for the whole budget'
    assert r['pending'] and not r['sent'] and r['state'] == 'queued' and r['queued'] is True
    assert r['queue_position'] == 3 and r['queue_length'] == 4 and r['eta_s'] == 95.0 and r['retry_after_s'] == 60
    # a retry while it waits joins the same job and does not start another
    r2 = C.op_send_job('Demo', 'hello', budget=1)
    assert r2['job_id'] == r['job_id'] and r2['pending']
    jobs['gate'].set()
    for _ in range(150):
        s = C.op_send_job('Demo', None, job_id=r['job_id'])
        if s.get('sent'): break
        time.sleep(.1)
    assert s['sent'] and jobs['sends'] == 1
    again = C.op_send_job('Demo', 'hello', budget=5)
    assert again['joined_existing_send'] and jobs['sends'] == 1


def test_queued_send_with_a_short_wait_keeps_waiting_for_the_result(jobs, monkeypatch):
    monkeypatch.setattr(C, 'SEND_QUEUED_ANSWER_S', 0.2)
    monkeypatch.setattr(C, 'queue_entry_for', lambda pid=None, job_id=None: {'position': 1, 'eta_s': 2.0, 'length': 1})
    threading.Timer(0.8, jobs['gate'].set).start()
    r = C.op_send_job('Demo', 'soon', budget=10)
    assert r['sent'] and jobs['sends'] == 1


def test_lock_deadline_in_a_job_fails_cleanly_and_is_not_retried(jobs):
    ex = C.Busy('the app is busy; queue_position=4 of 5'); ex.lock_deadline = True
    ex.queue = {'queued': True, 'queue_position': 4, 'queue_length': 5, 'eta_s': 80.0, 'waited_s': 360.0, 'retry_after_s': 80}
    jobs['raise'] = ex
    r = C.op_send_job('Demo', 'late', budget=5)
    assert r['failed'] and not r['sent'] and r['lock_deadline'] and r['retry_after_s'] == 80
    assert r['queue_position'] == 4 and r.get('attempts') == 1 and jobs['sends'] == 0
    assert 'Nothing was sent' in r['note']


def test_burst_tool_estimate_and_percentiles():
    sys.path.insert(0, str(REPO / 'tools'))
    import send_burst as B
    assert B.pct([1, 2, 3, 4, 5], 50) == 3 and B.pct([1, 2, 3, 4, 5], 95) == 4.8
    # five sends served back to back: holds 4 s + 10 s extra each, 2 s after the lock -> ends at 16, 30, 44, 58, 72
    e = B.estimate([4, 4, 4, 4, 4], 10, 2)
    assert e['p50'] == 44 and e['max'] == 72 and e['p95'] == pytest.approx(69.2)
