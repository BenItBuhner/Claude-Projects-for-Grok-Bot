"""1.0.5: verified sends, bulk cap, watcher yield, service-limit detection, read freshness and coalescing, lane order."""
import sys, pathlib, json, os, subprocess, time, datetime, pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import core as c

@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(c, 'log', lambda m: None)
    monkeypatch.setattr(c, 'UIQ', tmp_path / 'ui.queue')
    monkeypatch.setattr(c, 'SERVICE_LIMIT_FILE', tmp_path / 'service-limit.json')

@pytest.fixture
def other_pid():
    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    yield p.pid
    p.kill(); p.wait()

def iso(sec_ago=0):
    return (c.now() - datetime.timedelta(seconds=sec_ago)).isoformat()

# ---- send verification
def msg(text, idx=235, role='user', ts=None):
    return {'role': role, 'index': idx, 'text': text, 'time_utc': ts}

def test_post_matches_ignores_rerendered_whitespace():
    sent = 'Spellfix ticket OK (8:32 PM build) - go ahead and ship it to the beta channel now'
    shown = 'Spellfix ticket OK ( 8:32 PM build) -  go ahead and ship it to the beta channel now'
    assert c.post_matches(msg(shown), sent, before_max=234)

def test_post_matches_needs_new_user_message():
    t = 'Please rebuild the Android APK and post the link here when it is done'
    assert not c.post_matches(msg(t, idx=230), t, before_max=234)           # an older message
    assert not c.post_matches(msg(t, role='assistant'), t, before_max=234)  # Claude quoting it
    assert not c.post_matches(msg('something else entirely, not the post'), t, before_max=234)

# ---- lanes: a user send queued after a bulk poll still goes first
def test_send_ticket_served_before_older_bulk_ticket(monkeypatch):
    bulk = c._ticket_new('refresh all projects'); time.sleep(0.01)
    send = c._ticket_new('send Sketchpad primary')
    order = [fp for fp, _ in c._tickets()]
    assert order.index(send) < order.index(bulk)
    assert c.op_priority('send X') == 1 and c.op_priority('attachment get') == 2 and c.op_priority('card-watch X') == 0

# ---- bulk hard cap
def test_bulk_over_cap_only_when_someone_waits(monkeypatch, other_pid):
    monkeypatch.setattr(c, '_HELD', {'op': 'attachment get 138.2', 'since': time.time() - 30})
    monkeypatch.setattr(c, '_PREEMPT', {'armed': True, 'off': False})
    assert not c.bulk_over_cap()                                  # nobody waiting: keep going
    (c.UIQ).mkdir(parents=True, exist_ok=True)
    (c.UIQ / f'1-{time.time_ns():020d}-{other_pid}-1.json').write_text(json.dumps({'pid': other_pid, 'op': 'send X', 'priority': 1}))
    assert c.bulk_over_cap()
    monkeypatch.setattr(c, '_HELD', {'op': 'attachment get 138.2', 'since': time.time() - 5})
    assert not c.bulk_over_cap()                                  # under the cap
    monkeypatch.setattr(c, '_HELD', {'op': 'send Sketchpad primary', 'since': time.time() - 300})
    assert not c.bulk_over_cap()                                  # sends are never preempted

def test_preemptible_kinds():
    assert c.preemptible('attachment get 1.1') and c.preemptible('refresh all')
    assert not c.preemptible('wait X') and not c.preemptible('permission allow') and not c.preemptible('send X')

def test_preempted_is_interrupted():
    assert issubclass(c.Preempted, c.Interrupted)

# ---- card watcher yields
def test_watch_sleep_yields_when_others_wait():
    assert c.watch_sleep(5, 3, False) == 5
    assert c.watch_sleep(5, 3, True) >= c.WATCH_YIELD_MIN_S
    assert c.watch_sleep(5, 20, True) == 40

# ---- service limits
@pytest.mark.parametrize('t', ["You've reached your usage limit", 'Too many requests', 'Claude is at capacity right now',
                               'Your limit resets at 10 PM', 'Error 429'])
def test_service_limit_regex_hits(t):
    assert c.SERVICE_LIMIT_RE.search(t)

def test_service_limit_regex_ignores_normal_text():
    assert not c.SERVICE_LIMIT_RE.search('Sent the build to the beta channel; tests passed')

def test_service_limit_recent(tmp_path):
    c.SERVICE_LIMIT_FILE.write_text(json.dumps({'text': 'usage limit', 'at': iso(60)}))
    assert c.service_limit_recent(1800)['text'] == 'usage limit'
    c.SERVICE_LIMIT_FILE.write_text(json.dumps({'text': 'usage limit', 'at': iso(4000)}))
    assert c.service_limit_recent(1800) is None

# ---- read freshness
def test_freshness_live_is_never_stale():
    out = c.stamp_freshness({'read_at': iso(1000)}, 'live')
    assert out['freshness']['source'] == 'live' and not out['freshness']['stale'] and 'stale_warning' not in out

def test_freshness_old_cache_is_loudly_stale():
    out = c.stamp_freshness({'read_at': iso(c.READ_STALE_S + 60)}, 'cache')
    f = out['freshness']
    assert f['stale'] and f['age_s'] >= c.READ_STALE_S and out['stale_warning'].startswith('STALE')

def test_freshness_recent_cache_not_stale():
    out = c.stamp_freshness({'read_at': iso(10)}, 'cache_busy')
    assert not out['freshness']['stale'] and 5 <= out['freshness']['age_s'] <= 30

def test_freshness_missing_read_at_is_stale():
    assert c.stamp_freshness({}, 'cache')['freshness']['stale']

# ---- read coalescing
def test_duplicate_read_waits_and_shares_the_running_pass(monkeypatch):
    monkeypatch.setattr(c.time, 'sleep', lambda s: None)
    monkeypatch.setattr(c, 'resolve_project', lambda p: {'id': 'chan_X', 'name': 'Sketchpad', 'primary_thread_id': None})
    monkeypatch.setattr(c, 'resolve_thread', lambda chan, t, ths=None: None)
    seen = {'n': 0}
    def inflight(op):
        seen['n'] += 1; return {'pid': 4242, 'op': op} if seen['n'] < 4 else None
    monkeypatch.setattr(c, 'same_read_inflight', inflight)
    monkeypatch.setattr(c, 'read_cache', lambda chan, thread=None: {'read_at': iso(0)})
    calls = []
    def impl(project, thread=None, refresh=True, *a, **k):
        calls.append(refresh); return {'read_at': iso(0), 'messages': [], 'from_cache': not refresh}
    monkeypatch.setattr(c, '_op_read_impl', impl)
    out = c.op_read('Sketchpad')
    assert calls == [False], 'no second UI pass'
    assert out['freshness']['source'] == 'coalesced' and out['coalesced_with_pid'] == 4242

def test_read_without_duplicate_goes_live(monkeypatch):
    monkeypatch.setattr(c, 'resolve_project', lambda p: {'id': 'chan_X', 'name': 'Sketchpad', 'primary_thread_id': None})
    monkeypatch.setattr(c, 'same_read_inflight', lambda op: None)
    calls = []
    monkeypatch.setattr(c, '_op_read_impl', lambda project, thread=None, refresh=True, *a, **k: (calls.append(refresh), {'read_at': iso(0)})[1])
    out = c.op_read('Sketchpad')
    assert calls == [True] and out['freshness']['source'] == 'live'

def test_coalesced_read_falls_back_to_live_when_cache_is_old(monkeypatch):
    monkeypatch.setattr(c.time, 'sleep', lambda s: None)
    monkeypatch.setattr(c, 'resolve_project', lambda p: {'id': 'chan_X', 'name': 'Sketchpad', 'primary_thread_id': None})
    monkeypatch.setattr(c, 'resolve_thread', lambda chan, t, ths=None: None)
    st = {'n': 0}
    monkeypatch.setattr(c, 'same_read_inflight', lambda op: (st.__setitem__('n', st['n'] + 1), {'pid': 1} if st['n'] < 2 else None)[1])
    monkeypatch.setattr(c, 'read_cache', lambda chan, thread=None: {'read_at': iso(900)})   # the other pass failed: no new cache
    calls = []
    monkeypatch.setattr(c, '_op_read_impl', lambda project, thread=None, refresh=True, *a, **k: (calls.append(refresh), {'read_at': iso(0)})[1])
    assert c.op_read('Sketchpad')['freshness']['source'] == 'live' and calls == [True]

def test_same_read_inflight_sees_other_process_ticket(other_pid):
    c.UIQ.mkdir(parents=True, exist_ok=True)
    (c.UIQ / f'1-{time.time_ns():020d}-{other_pid}-1.json').write_text(json.dumps({'pid': other_pid, 'op': 'read Sketchpad primary', 'priority': 1}))
    assert c.same_read_inflight('read Sketchpad primary')['pid'] == other_pid
    assert c.same_read_inflight('read Sketchpad Design') is None

# ---- typing: the app's box refuses AT-SPI EditableText (0 of 38 real sends), so paste goes first
def test_type_text_skips_atspi_insert_by_default(monkeypatch):
    monkeypatch.delenv('CLAUDE_PROJECTS_ATSPI_INSERT', raising=False)
    called = []
    monkeypatch.setattr(c, '_focus_composer', lambda e: None)
    monkeypatch.setattr(c, 'atspi_insert_text', lambda e, t: called.append('atspi') or False)
    monkeypatch.setattr(c, '_paste_via_clipboard', lambda t: called.append('paste') or True)
    monkeypatch.setattr(c, '_wait_composer_match', lambda t, e=None, timeout_s=2.5: (True, t))
    c.type_text(object(), 'hello there')
    assert called == ['paste']

def test_send_buttons_prefer_the_composer_scope(monkeypatch):
    monkeypatch.setattr(c, '_composer_scope', lambda e=None: 'scope')
    monkeypatch.setattr(c, 'find_all', lambda root, r=None, pred=None: ['send-in-scope'] if root == 'scope' else ['doc-wide'])
    monkeypatch.setattr(c, 'main_doc', lambda: (_ for _ in ()).throw(AssertionError('no whole-document walk')))
    assert c._send_buttons('composer') == ['send-in-scope']

def test_post_matches_time_window_without_index():
    t = 'OK, ship the Spellfix ticket now please'
    old = msg(t, idx=100, ts='2026-11-16T01:00:00.000Z'); new = msg(t, idx=101, ts='2026-11-16T03:35:10.000Z'); undated = msg(t, idx=102)
    since = '2026-11-16T03:30:00Z'
    assert not c.post_matches(old, t, None, since)
    assert c.post_matches(new, t, None, since)
    assert not c.post_matches(undated, t, None, since), 'an undated identical message may be an older post'
