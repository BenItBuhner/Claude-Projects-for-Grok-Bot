"""claude_thread_attachment_get answers within its budget: one fetch per attachment (retries and concurrent calls join
it), a pending answer with a job_id when it runs long, and the finished file from the cache on the next call.
The app is never touched: _attachment_fetch is faked, and the background worker runs as a thread."""
import pathlib, sys, threading, time
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import core as C

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


@pytest.fixture
def jobs(monkeypatch, tmp_path):
    st = {'calls': 0, 'delay': 0.0, 'available': True, 'raise': None, 'spawned': 0,
          # the on-disk transcript cache: message 138 holds the two PNGs
          'transcript': {'messages': [{'index': 138, 'role': 'assistant', 'time_utc': '2026-11-15T15:36:00Z', 'text': 'Here are the PNGs',
                                       'attachments': [{'name': 'tablet-light.png'}, {'name': 'PNG tablet-light.png'}]}]}}
    monkeypatch.setattr(C, 'read_cache', lambda chan, thread=None: st['transcript'])
    monkeypatch.setattr(C, 'ATT_JOBS', tmp_path / 'jobs')
    monkeypatch.setattr(C, 'resolve_project', lambda q, snap=None: {'id': 'chan_X', 'name': 'Demo'})
    store = tmp_path / 'store'; store.mkdir()

    def fake_fetch(project, thread, message_index, n, force=False, stay=False, shot=None):
        st['calls'] += 1; time.sleep(st['delay'])
        if st['raise']: raise st['raise']
        if not st['available']: return {'available': False, 'name': 'tablet-light.png', 'source': 'not available: x', 'failed_steps': ['x']}
        f = store / f'{st["calls"]}.png'; f.write_bytes(PNG)
        return {'available': True, 'name': 'tablet-light.png', 'saved_name': 'tablet-light.png', 'mime': 'image/png', 'size': len(PNG),
                'path_on_host': str(f), 'sha256': 'x', 'source': "the Overview viewer's Download file button (original file)", 'original': True,
                'project_id': 'chan_X', 'thread_id': 'primary', 'message_index': message_index,
                'identity': {'name': 'tablet-light.png', 'n': n, 'role': 'assistant', 'time_utc': '2026-11-15T15:36:00Z', 'text_sha': C._text_sha('Here are the PNGs')}}
    monkeypatch.setattr(C, '_attachment_fetch', fake_fetch)
    monkeypatch.setattr(C, 'image_for_mcp', lambda data, full: (data, 'image/png', 1, 1, False))

    threads = []
    def spawn(key, job):
        st['spawned'] += 1
        t = threading.Thread(target=C._att_run_job, args=(key, job), daemon=True); threads.append(t); t.start()
    monkeypatch.setattr(C, '_att_spawn_worker', spawn)
    yield st
    for t in threads: t.join(5)   # finish background workers before the patches are undone


def get(**k): return C.op_attachment_get('Demo', attachment_id='138.2', **k)


def test_quick_fetch_answers_inside_the_budget(jobs):
    r = get(budget=5)
    assert r['available'] and r['returned_as'] == 'image' and r['image']['base64'] and r['job_id'] and not r.get('cached')
    assert jobs['calls'] == 1


def test_long_fetch_returns_pending_then_the_cached_file(jobs):
    jobs['delay'] = 1.0
    t0 = time.time(); r = get(budget=0.3)
    assert time.time() - t0 < 1.0, 'answers within the budget'
    assert r['pending'] and not r['available'] and r['job_id'] and 'call again' in r['note']
    r2 = get(budget=5)                           # the client's retry joins the running fetch
    assert r2['available'] and r2['job_id'] == r['job_id']
    assert jobs['calls'] == 1
    r3 = get(budget=5, as_='path')               # and later calls come from the cache, without the app
    assert r3['cached'] and r3['available'] and r3['returned_as'] == 'path' and 'image' not in r3
    assert jobs['calls'] == 1


def test_concurrent_calls_share_one_fetch(jobs):
    jobs['delay'] = 0.6
    out = []
    ts = [threading.Thread(target=lambda: out.append(get(budget=5))) for _ in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(out) == 4 and all(r['available'] for r in out)
    assert jobs['calls'] == 1, 'one fetch in the app for four calls'


def test_inline_call_joins_a_running_background_fetch(jobs):
    jobs['delay'] = 0.6
    assert get(budget=0.1)['pending']
    r = get()                                    # the CLI / forward path waits for the running fetch instead of starting one
    assert r['available'] and jobs['calls'] == 1


def test_cached_call_needs_the_file_on_disk(jobs):
    r = get(budget=5); pathlib.Path(r['path_on_host']).unlink()
    r2 = get(budget=5)
    assert r2['available'] and not r2.get('cached') and jobs['calls'] == 2


def test_refresh_fetches_again(jobs):
    get(budget=5); r = get(budget=5, refresh=True)
    assert r['available'] and not r.get('cached') and jobs['calls'] == 2


def test_not_available_is_answered_once_not_cached(jobs):
    jobs['available'] = False
    r = get(budget=5)
    assert r['available'] is False and r['failed_steps'] == ['x'] and not r.get('pending')
    jobs['available'] = True
    assert get(budget=5)['available'] and jobs['calls'] == 2


def test_errors_reach_the_caller(jobs):
    jobs['raise'] = C.NotFound('message 138 has 2 attachment(s); n must be 1..2')
    with pytest.raises(C.NotFound, match='n must be'):
        get(budget=5)
    with pytest.raises(C.NotFound):
        get()


def test_key_is_per_attachment_and_thread():
    k = C.att_job_key
    assert k('chan_X', None, 138, 2) == k('chan_X', 'primary', 138, 2) == k('chan_X', ' Primary ', 138, 2)
    assert len({k('chan_X', None, 138, 2), k('chan_X', None, 138, 3), k('chan_X', None, 139, 2), k('chan_Y', None, 138, 2), k('chan_X', 'Design', 138, 2)}) == 5


def test_worker_command_line(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(C, 'LOGD', tmp_path)
    monkeypatch.setattr(C.subprocess, 'Popen', lambda argv, **kw: seen.update(argv=argv, kw=kw))
    monkeypatch.setattr(C, 'log', lambda *a: None)
    monkeypatch.setattr(C, '_use_systemd_scope', lambda: False)
    C._att_spawn_worker('k', {'chan': 'chan_X', 'message_index': 138, 'n': 2, 'thread': 'Design review', 'force': True})
    argv = seen['argv']
    assert argv[1:3] == ['-m', 'claude_projects'] and argv[3:] == ['--json', '--force', 'attachment', 'get', 'chan_X', '138.2', '--job', '--thread', 'Design review']
    assert seen['kw']['start_new_session'] is True


def test_cli_and_mcp_pass_the_budget():
    root = pathlib.Path(C.__file__).resolve().parent.parent
    mcp_src = (root / 'claude_projects/mcp_server.py').read_text()
    tool = mcp_src[mcp_src.index('async def claude_thread_attachment_get'):mcp_src.index('@mcp.tool()', mcp_src.index('async def claude_thread_attachment_get'))]
    assert "'--budget', str(ATTACH_BUDGET)" in tool and "'--refresh'" in tool
    assert float(__import__('re').search(r"CLAUDE_PROJECTS_ATTACH_BUDGET', '(\d+)'", mcp_src).group(1)) <= 45
    core = (root / 'claude_projects/core.py').read_text()
    assert "'--budget'" in core and "'--job'" in core and "'--refresh'" in core


def test_save_dialog_path_polls_instead_of_sleeping(monkeypatch, tmp_path):
    """Download -> Save as -> file: with the dialog showing at once and the file written at once, it takes well under 3 s."""
    slot = tmp_path / 'slot'; slot.mkdir()
    monkeypatch.setattr(C.STAGE, 'new_slot', lambda kind: slot)
    state = {'dialog': False}
    btn = type('B', (), {'get_name': lambda s: 'Download file', 'get_role_name': lambda s: 'push button',
                         'get_component_iface': lambda s: type('X', (), {'scroll_to': lambda s, _: None})(),
                         'get_action_iface': lambda s: type('A', (), {'get_n_actions': lambda s: 1, 'do_action': lambda s, _: state.update(dialog=True)})()})()
    monkeypatch.setattr(C, '_save_dialog_wid', lambda: '42' if state['dialog'] else None)
    monkeypatch.setattr(C, '_active_win_name', lambda: 'Save as')
    monkeypatch.setattr(C, 'xdo', lambda *a, **k: '')
    def press_save(wid):
        (slot / 'tablet-light.png').write_bytes(PNG); state['dialog'] = False; return True
    monkeypatch.setattr(C, '_press_save', press_save)
    t0 = time.time(); path = C._save_with_button(btn, 'tablet-light.png', dialog_timeout=5)
    assert path == str(slot / 'tablet-light.png') and time.time() - t0 < 3, time.time() - t0


def test_renumbered_message_is_fetched_again_not_served_from_cache(jobs):
    """Message numbers shift: once message 138's attachment 2 is a different file, 138.2 must not return the old file."""
    r1 = get(budget=5); assert r1['available'] and jobs['calls'] == 1
    assert get(budget=5)['cached'] and jobs['calls'] == 1          # same transcript: served from the cache
    jobs['transcript']['messages'][0]['attachments'][1] = {'name': 'PNG foldable-dark.png'}
    r3 = get(budget=5)
    assert not r3.get('cached') and jobs['calls'] == 2, 'a changed attachment name means a fresh fetch'


@pytest.mark.parametrize('change', ['time_utc', 'role', 'text', 'gone', 'no_cache'])
def test_other_identity_changes_also_miss(jobs, change):
    get(budget=5)
    m = jobs['transcript']['messages'][0]
    if change == 'time_utc': m['time_utc'] = '2026-11-15T18:00:00Z'
    elif change == 'role': m['role'] = 'user'
    elif change == 'text': m['text'] = 'something else'
    elif change == 'gone': jobs['transcript']['messages'] = []
    else: jobs['transcript'] = None
    assert not get(budget=5).get('cached') and jobs['calls'] == 2


def test_status_lists_running_jobs_and_busy(jobs, monkeypatch):
    jobs['delay'] = 0.8
    assert get(budget=0.1)['pending']
    run = C.jobs_running()
    assert run and run[0]['kind'] == 'attachment' and run[0]['job_id']
    for f in ('vnc_viewers', 'x_idle_min', 'effective_user_idle_min', 'last_own_input_age_min', 'route', 'dialogs', '_open_files_wids'):
        monkeypatch.setattr(C, f, lambda *a, **k: None if f != '_open_files_wids' else [])
    st = C.op_status()
    assert st['busy'] is True and st['jobs_running'][0]['kind'] == 'attachment'


def test_worker_runs_in_its_own_systemd_scope_when_there_is_one(monkeypatch, tmp_path):
    """A deploy restart of the MCP service must not kill a running job: under systemd it gets a transient scope."""
    calls = []
    class P:
        def __init__(self, argv, **kw): calls.append(argv); self.returncode = None
        def poll(self): return None
    monkeypatch.setattr(C, 'LOGD', tmp_path); monkeypatch.setattr(C.subprocess, 'Popen', P)
    monkeypatch.setattr(C, '_use_systemd_scope', lambda: True); monkeypatch.setattr(C.time, 'sleep', lambda s: None)
    assert C.spawn_job(['python', 'x'], 'att-1') == 'scope'
    assert calls[0][:4] == ['systemd-run', '--user', '--scope', '--quiet'] and calls[0][-2:] == ['python', 'x']
    class Bad(P):
        def poll(self): self.returncode = 1; return 1
    calls.clear(); monkeypatch.setattr(C.subprocess, 'Popen', Bad)
    assert C.spawn_job(['python', 'x'], 'att-1') == 'process' and calls[-1] == ['python', 'x']
