"""Sends with attachments: a background job that answers within the budget, one send per job however often the
client retries, a bounded attach loop, and a clean composer when anything stops it. The app is never touched."""
import contextlib, json, os, pathlib, signal, subprocess, sys, threading, time
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import core as C


@pytest.fixture
def files(tmp_path):
    out = []
    for n in ('demo.mp4', 'app.js', 'main.js', 'util.js', 'index.html', 'notes.txt'):
        f = tmp_path / n; f.write_bytes(b'x' * 10); out.append({'path': str(f), 'name': n, 'size': 10, 'mime': None, 'staged': False})
    return out


class App:
    """Just enough of the app for op_send: a composer, chips, and a picker that attaches each file after Open."""
    def __init__(self, stall=None, term_on=None):
        self.chips, self.text, self.stall, self.term_on = [], '', stall, term_on
        self.sent = 0; self.removed = []; self.home = 0; self.current = None; self.dismissed = 0; self.sent_with = None


@pytest.fixture
def app(monkeypatch, files):
    a = App()
    monkeypatch.setattr(C, 'log', lambda *x, **k: None)
    monkeypatch.setattr(C.STAGE, 'maybe_sweep', lambda: None)
    monkeypatch.setattr(C, 'resolve_project', lambda q, snap=None: {'id': 'chan_E', 'name': 'Notes'})
    monkeypatch.setattr(C, 'resolve_thread', lambda chan, thread=None, *x: None)
    monkeypatch.setattr(C, 'ui_lock', lambda *x, **k: contextlib.nullcontext())
    for f in ('open_target', 'settle'): monkeypatch.setattr(C, f, lambda *x, **k: None)
    monkeypatch.setattr(C, 'dialogs', lambda: None)
    monkeypatch.setattr(C, '_open_files_wids', lambda: [])
    monkeypatch.setattr(C, 'read_messages', lambda *x, **k: [])
    monkeypatch.setattr(C, 'composer', lambda: 'composer')
    monkeypatch.setattr(C, 'composer_text', lambda e: a.text)
    monkeypatch.setattr(C, 'main_doc', lambda: (None, ''))
    monkeypatch.setattr(C, 'find_all', lambda *x, **k: [])
    monkeypatch.setattr(C, 'go_home', lambda: setattr(a, 'home', a.home + 1))
    monkeypatch.setattr(C, 'route', lambda: '/project/chan_E')
    monkeypatch.setattr(C.AttachCtx, 'ensure', lambda self: self)
    def open_picker(ctx=None, limit=15): return '42'
    def type_loc(wid, path): a.current = os.path.basename(path)
    def click_open(root, wid):
        if a.term_on == a.current: raise C.Interrupted('stopped by SIGTERM')
        if a.current != a.stall: a.chips.append(a.current)
    monkeypatch.setattr(C, '_open_attach_picker', open_picker)
    monkeypatch.setattr(C, '_chooser_type_location', type_loc)
    monkeypatch.setattr(C, '_click_chooser_open', click_open)
    monkeypatch.setattr(C, '_composer_has_chip', lambda name, scope=None: name in a.chips)
    monkeypatch.setattr(C, 'composer_chips', lambda scope=None: list(a.chips))
    monkeypatch.setattr(C, '_dismiss_open_files', lambda reason='': (setattr(a, 'dismissed', a.dismissed + 1), True)[1])
    def remove(name, scope=None):
        a.removed.append(name); a.chips.remove(name); return True
    monkeypatch.setattr(C, '_remove_chip', remove)
    monkeypatch.setattr(C, '_clear_composer_keys', lambda: setattr(a, 'text', ''))
    def type_text(e, msg): a.text = msg
    monkeypatch.setattr(C, 'type_text', type_text)
    def xdo(*x, **k):
        if x[-1:] == ('Return',): a.sent += 1; a.sent_with = (list(a.chips), a.text)
        return ''
    monkeypatch.setattr(C, 'xdo', xdo)
    monkeypatch.setattr(C, '_verify_sent', lambda tid, msg, bmax, secs=40: {'index': 7})
    # a fast clock: every poll advances 1 s, so a 20 s per-file limit ends in 20 polls
    clock = [1_000_000.0]
    monkeypatch.setattr(C.time, 'time', lambda: clock[0])
    monkeypatch.setattr(C.time, 'sleep', lambda s: clock.__setitem__(0, clock[0] + max(s, .3)))
    return a


def test_six_files_one_stalls_per_file_timeout_and_clean_composer(app, files):
    app.stall = 'util.js'
    with pytest.raises(C.AttachError) as ei:
        C.op_send('Notes', 'see the files', prepared_files=files, deadline_s=420)
    ex = ei.value
    assert ex.file == 'util.js' and 'within 20 s after Open' in str(ex) and 'nothing was sent' in str(ex)
    assert app.removed == ['demo.mp4', 'app.js', 'main.js'] and app.chips == [], 'chips already attached are taken out'
    assert ex.cleanup['chips_left'] == [] and ex.cleanup['composer_empty'] is True
    assert app.sent == 0 and app.text == '' and app.home == 1


def test_all_six_attach_then_send(app, files):
    r = C.op_send('Notes', 'see the files', prepared_files=files, deadline_s=420)
    assert r['sent'] and r['attached'] == [f['name'] for f in files] and app.sent == 1 and app.removed == []
    assert r['origin_header'].startswith('[sent by Grok Bot')


def test_overall_deadline_is_enforced_inside_the_send(app, files):
    app.stall = 'notes.txt'
    with pytest.raises(C.AppError, match='longer than 30 s|within'):
        C.op_send('Notes', 'x', prepared_files=files, deadline_s=30)
    assert app.chips == [] and app.sent == 0


def test_sigterm_mid_attach_runs_cleanup(app, files):
    app.term_on = 'main.js'
    with pytest.raises(C.Interrupted) as ei:
        C.op_send('Notes', 'see the files', prepared_files=files)
    assert app.removed == ['demo.mp4', 'app.js'] and app.chips == [] and app.dismissed >= 1
    assert ei.value.cleanup['chips_removed'] == ['demo.mp4', 'app.js'] and app.home == 1 and app.sent == 0


def test_text_mismatch_also_removes_the_chips(app, files, monkeypatch):
    monkeypatch.setattr(C, 'type_text', lambda e, msg: setattr(app, 'text', 'garbled'))
    with pytest.raises(C.AppError, match='did not match'):
        C.op_send('Notes', 'see the files', prepared_files=files[:2])
    assert app.chips == [] and app.text == '' and app.sent == 0


# ---------------------------------------------------------------- leftover chips and the open dry run

def test_dry_run_is_not_empty_with_leftover_chips(app, files):
    app.chips = ['old.png', 'old.png']
    r = C.op_send('Notes', 'hi', dry_run=True)
    assert r['composer_empty'] is False and r['composer_chips'] == ['old.png', 'old.png'] and 'clear_leftovers' in r['leftover_note']
    assert app.sent == 0 and app.chips == ['old.png', 'old.png'] and app.removed == []


def test_text_only_send_refuses_with_leftover_chips(app, files):
    app.chips = ['old.png', 'notes.txt']
    with pytest.raises(C.Refused) as ei:
        C.op_send('Notes', 'just text')
    assert 'old.png' in str(ei.value) and 'notes.txt' in str(ei.value) and 'clear_leftovers' in str(ei.value)
    assert app.sent == 0 and app.text == '' and app.chips == ['old.png', 'notes.txt'], 'nothing typed, chips left alone'


def test_clear_leftovers_removes_them_and_sends_only_the_text(app, files):
    app.chips = ['old.png', 'old.png', 'app.js']
    r = C.op_send('Notes', 'just text', clear_leftovers=True)
    assert r['sent'] and r['cleared_leftovers'] == ['old.png', 'old.png', 'app.js'] and r['attached'] is None
    assert app.removed == ['old.png', 'old.png', 'app.js'] and app.sent == 1
    assert app.sent_with[0] == [] and app.sent_with[1].endswith('just text'), 'Send saw no chips, only the text'


def test_clear_leftovers_then_attach_sends_only_our_file(app, files):
    app.chips = ['app.js']                       # the same name as the file we attach: the leftover still goes first
    r = C.op_send('Notes', 'with file', prepared_files=files[1:2], clear_leftovers=True)
    assert r['sent'] and r['attached'] == ['app.js'] and app.sent_with[0] == ['app.js'] and app.removed == ['app.js']


def test_extra_chip_before_send_aborts_cleanly(app, files, monkeypatch):
    def type_and_stray(e, msg): app.text = msg; app.chips.append('stray.png')
    monkeypatch.setattr(C, 'type_text', type_and_stray)
    with pytest.raises(C.AppError, match=r"did not add: \['stray.png'\].*nothing was sent") as ei:
        C.op_send('Notes', 'see the files', prepared_files=files[:2])
    assert app.sent == 0 and app.text == '' and app.removed == ['demo.mp4', 'app.js'] and app.chips == ['stray.png']
    assert ei.value.cleanup['chips_removed'] == ['demo.mp4', 'app.js']


def test_duplicate_chip_names_are_all_detected(monkeypatch):
    buttons = ['Remove a.png', 'Add', 'Remove a.png', 'Remove b.json', 'a.png', 'Remove model']
    monkeypatch.setattr(C, 'nm', lambda e: e)
    monkeypatch.setattr(C, 'find_all', lambda root, r=None, pred=None: [b for b in buttons if pred is None or pred(b)])
    assert C.composer_chips(scope='composer') == ['a.png', 'a.png', 'b.json']
    assert C._chip_count('a.png', 'composer') == 2 and C._chip_count('b.js', 'composer') == 0, 'b.js is not b.json'
    assert C._extra_chips(['a.png', 'a.png', 'b.json'], ['a.png']) == (['a.png', 'b.json'], [])
    assert C._extra_chips(['a.png'], ['a.png', 'c.txt']) == ([], ['c.txt'])


def test_remove_chip_with_duplicates_waits_for_the_count_to_drop(monkeypatch):
    chips = ['a.png', 'a.png']
    monkeypatch.setattr(C, 'main_doc', lambda: ('doc', ''))
    monkeypatch.setattr(C, 'nm', lambda e: e)
    monkeypatch.setattr(C, 'find', lambda root, r=None, name=None, pred=None: next((b for b in ['Remove ' + c for c in chips] if pred is None or pred(b)), None))
    monkeypatch.setattr(C, 'act', lambda b, what='': chips.remove(b[7:]) or True)
    monkeypatch.setattr(C, 'composer_chips', lambda scope=None: list(chips))
    assert C._remove_chip('a.png') is True and chips == ['a.png'], 'one press, one chip; the other copy is still there'
    assert C._remove_chip('a.png') is True and chips == []


def test_attach_test_open_attaches_removes_and_never_types_or_sends(app, files, monkeypatch):
    monkeypatch.setattr(C, 'type_text', lambda e, msg: pytest.fail('the open test must never type the message'))
    r = C.op_send('Notes', 'hi', dry_run=True, attach_test='open', prepared_files=files[:3])
    assert r['attach_test'] == 'open' and r['all_attached'] and r['all_removed'] and r['composer_empty'] is True and r['composer_chips'] == []
    assert [f['name'] for f in r['files']] == ['demo.mp4', 'app.js', 'main.js'] and all('seconds' in f for f in r['files'])
    assert app.removed == ['demo.mp4', 'app.js', 'main.js'] and app.sent == 0 and app.text == '' and 'timing' in r


def test_attach_test_open_refuses_with_leftovers(app, files):
    app.chips = ['old.png']
    with pytest.raises(C.Refused, match='old.png'):
        C.op_send('Notes', 'hi', dry_run=True, attach_test='open', prepared_files=files[:1])
    assert app.chips == ['old.png'] and app.removed == [] and app.sent == 0


def test_attach_test_open_stall_cleans_up(app, files):
    app.stall = 'app.js'
    with pytest.raises(C.AttachError):
        C.op_send('Notes', 'hi', dry_run=True, attach_test='open', prepared_files=files[:3])
    assert app.chips == [] and app.sent == 0 and app.removed == ['demo.mp4']


def test_cli_attach_test_modes_and_clear_leftovers(monkeypatch):
    seen = {}
    monkeypatch.setattr(C, 'op_send', lambda *a, **k: seen.update(k) or {'ok': True})
    for argv, mode in ((['send', 'P', 'm', '--dry-run', '--attach-test'], 'cancel'), (['send', 'P', 'm', '--dry-run', '--attach-test=open'], 'open'),
                       (['send', 'P', 'm', '--dry-run'], False)):
        assert C.main(['--json'] + argv) == 0 and seen['attach_test'] == mode
    assert C.main(['--json', 'send', 'P', 'm', '--clear-leftovers']) == 0 and seen['clear_leftovers'] is True


def test_joined_send_says_how_to_send_again(jobs, files):
    r1 = send(files, budget=5); r2 = send(files, budget=5)
    assert r1['sent'] and r2['joined_existing_send'] is True and 'new idempotency_key' in r2['note'] and jobs['sends'] == 1
    r3 = send(files, budget=5, idempotency_key='again-1')
    assert r3['sent'] and not r3.get('joined_existing_send') and jobs['sends'] == 2


def test_clear_leftovers_reaches_the_worker(jobs, files, monkeypatch):
    seen = {}
    real = C.op_send
    monkeypatch.setattr(C, 'op_send', lambda *a, **k: seen.update(k) or real(*a, **k))
    send(files, budget=5, clear_leftovers=True)
    assert seen.get('clear_leftovers') is True


# ---------------------------------------------------------------- deploy busy check

def test_jobs_exits_nonzero_while_the_ui_lock_is_held(monkeypatch, capsys):
    monkeypatch.setattr(C, 'jobs_running', lambda: [])
    monkeypatch.setattr(C, '_read_ui_holder', lambda: {'pid': os.getpid(), 'op': 'attachment Demo 138.2'})
    assert C.main(['--json', 'jobs']) == 3
    out = json.loads(capsys.readouterr().out)
    assert out['busy'] is True and out['lock_holder']['op'] == 'attachment Demo 138.2'


def test_jobs_exits_nonzero_while_a_job_runs_and_zero_when_idle(monkeypatch, capsys):
    monkeypatch.setattr(C, '_read_ui_holder', lambda: None)
    monkeypatch.setattr(C, 'jobs_running', lambda: [{'kind': 'send', 'job_id': 's1'}])
    assert C.main(['--json', 'jobs']) == 3
    monkeypatch.setattr(C, 'jobs_running', lambda: [])
    assert C.main(['--json', 'jobs']) == 0 and json.loads(capsys.readouterr().out.strip().splitlines()[-1])['busy'] is False


def test_a_dead_lock_holder_is_not_busy(monkeypatch):
    p = subprocess.Popen([sys.executable, '-c', 'pass']); p.wait()
    monkeypatch.setattr(C, '_read_ui_holder', lambda: {'pid': p.pid, 'op': 'killed'})
    monkeypatch.setattr(C, 'jobs_running', lambda: [])
    assert C.ui_lock_holder() is None and C.deploy_busy()['busy'] is False


# ---------------------------------------------------------------- the job layer

@pytest.fixture
def jobs(monkeypatch, tmp_path, files):
    st = {'sends': 0, 'delay': 0.0, 'fail': None, 'after_send': None}
    monkeypatch.setattr(C, 'SEND_JOBS', tmp_path / 'send-jobs')
    monkeypatch.setattr(C, 'ATT_JOBS', tmp_path / 'att-jobs')
    monkeypatch.setattr(C.STAGE, 'maybe_sweep', lambda: None)
    monkeypatch.setattr(C, 'resolve_project', lambda q, snap=None: {'id': 'chan_E', 'name': 'Notes'})
    monkeypatch.setattr(C, 'prepare_attachment_files', lambda atts: [f for f in files if f['name'] in [pathlib.Path(x['path']).name for x in atts]])
    monkeypatch.setattr(C, 'log', lambda *x, **k: None)
    def fake_send(project, message, thread=None, force=False, bot_name=None, prepared_files=None, deadline_s=None, **k):
        C._progress('attaching', attached=0, total=len(prepared_files or []))
        time.sleep(st['delay'])
        if st['fail']: raise st['fail']
        C._progress('sending'); st['sends'] += 1
        if st['after_send']: raise st['after_send']
        return {'sent': True, 'verified_in_transcript': True, 'message_index': 140, 'attached': [f['name'] for f in prepared_files or []]}
    monkeypatch.setattr(C, 'op_send', fake_send)
    threads = []
    def spawn(argv, name):
        t = threading.Thread(target=C.run_send_job, args=(argv[-1],), daemon=True); threads.append(t); t.start(); return 'thread'
    monkeypatch.setattr(C, 'spawn_job', spawn)
    yield st
    for t in threads: t.join(5)


def send(files, **k): return C.op_send_job('Notes', 'see the files', attachments=[{'path': f['path']} for f in files], **k)


def test_quick_send_answers_with_the_result(jobs, files):
    r = send(files, budget=5)
    assert r['sent'] and r['message_index'] == 140 and r['job_id'] and jobs['sends'] == 1


def test_long_send_answers_pending_within_budget_then_result(jobs, files):
    jobs['delay'] = 1.0
    t0 = time.time(); r = send(files, budget=0.3)
    assert time.time() - t0 < 1.0 and r['pending'] and not r['sent'] and r['job_id'] and r['state'] == 'attaching'
    assert r['progress'] == 'attaching 1/6' and r['retry_after_s']
    time.sleep(1.3)
    s = C.op_send_job('Notes', None, job_id=r['job_id'])
    assert s['sent'] and s['job_id'] == r['job_id'] and jobs['sends'] == 1


def test_duplicate_calls_join_one_send(jobs, files):
    jobs['delay'] = 0.6
    out = []
    ts = [threading.Thread(target=lambda: out.append(send(files, budget=5))) for _ in range(3)]
    for t in ts: t.start(); time.sleep(.05)
    for t in ts: t.join()
    assert jobs['sends'] == 1 and all(r['sent'] for r in out), out
    again = send(files, budget=5)                 # a retry after it went out: answered, not sent again
    assert again['joined_existing_send'] and jobs['sends'] == 1


def test_idempotency_key_dedupes_even_with_other_text(jobs, files):
    send(files, budget=5, idempotency_key='grok-123')
    r = C.op_send_job('Notes', 'reworded', attachments=[{'path': files[0]['path']}], budget=5, idempotency_key='grok-123')
    assert r['joined_existing_send'] and jobs['sends'] == 1


def test_failed_before_send_is_reported_and_can_be_retried(jobs, files):
    e = C.AttachError('util.js', 'did not appear in the composer within 20 s after Open'); e.cleanup = {'chips_left': [], 'chips_removed': ['app.js']}
    jobs['fail'] = e
    r = send(files, budget=5)
    assert r['failed'] and not r['sent'] and 'util.js' in r['error'] and r['error_type'] == 'AttachError'
    assert 'composer was left clean' in r['note'] and 'nothing was sent' in r['note']
    jobs['fail'] = None
    assert send(files, budget=5)['sent'] and jobs['sends'] == 1


def test_crash_after_send_is_never_retried(jobs, files):
    jobs['after_send'] = C.AppError('transcript did not load')
    r = send(files, budget=5)
    assert r['sent'] and r['verified_in_transcript'] is False and 'check the thread' in r['note']
    jobs['after_send'] = None
    assert send(files, budget=5)['joined_existing_send'] and jobs['sends'] == 1


def test_running_send_counts_as_busy(jobs, files):
    jobs['delay'] = 0.8
    r = send(files, budget=0.1)
    run = C.jobs_running()
    assert any(j['kind'] == 'send' and j['job_id'] == r['job_id'] for j in run)


def test_sigterm_in_a_real_worker_process_records_and_cleans(tmp_path):
    """The worker's SIGTERM handler: the send stops at a clean point, the job says why, the lock is released."""
    jobs = tmp_path / 'send-jobs'; jobs.mkdir()
    key = 'sTESTSIGTERM'
    (jobs / f'{key}.json').write_text(json.dumps({'job_id': key, 'state': 'queued', 'started_ts': time.time(), 'total': 6,
                                                   'spec': {'project': 'chan_E', 'message': 'x', 'files': [], 'deadline_s': 420}}))
    script = tmp_path / 'worker.py'
    script.write_text(f"""
import sys, time, pathlib
sys.path.insert(0, {str(pathlib.Path(C.__file__).parent.parent)!r})
from claude_projects import core as C
C.SEND_JOBS = pathlib.Path({str(jobs)!r})
def fake_send(project, message, thread=None, **k):
    C._progress('attaching', attached=2, total=6, file='main.js')
    try: time.sleep(30)
    except BaseException as ex:
        ex.cleanup = {{'chips_removed': ['demo.mp4', 'app.js'], 'chips_left': [], 'composer_empty': True}}; raise
C.op_send = fake_send
sys.exit(C.main(['--json', 'send-job', {key!r}]))
""")
    pr = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))
    t0 = time.time()
    while time.time() - t0 < 20:
        st = json.loads((jobs / f'{key}.json').read_text())
        if st.get('state') == 'attaching': break
        time.sleep(.1)
    assert st['state'] == 'attaching'
    pr.send_signal(signal.SIGTERM); out, err = pr.communicate(timeout=20)
    st = json.loads((jobs / f'{key}.json').read_text())
    assert st['state'] == 'failed' and 'SIGTERM' in st['error'] and st['cleanup']['chips_removed'] == ['demo.mp4', 'app.js'], (st, err)
    assert not C.job_running(jobs, key)


def test_mcp_send_tool_uses_the_job_budget():
    src = (pathlib.Path(C.__file__).parent.parent / 'claude_projects/mcp_server.py').read_text()
    tool = src[src.index('async def claude_thread_send('):src.index('@mcp.tool()', src.index('async def claude_thread_send('))]
    assert "'--budget', str(SEND_BUDGET)" in tool and "'--job-id'" in tool and 'idempotency_key' in tool
    assert float(__import__('re').search(r"CLAUDE_PROJECTS_SEND_BUDGET', '(\d+)'", src).group(1)) <= 45
