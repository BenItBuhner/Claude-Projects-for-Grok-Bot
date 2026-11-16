"""1.0.6: pruned accessibility walks, tail-only reads and verifies, message reuse, faster bulk cap."""
import sys, pathlib, time, pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import core as c

class N:
    def __init__(self, role, name='', kids=()):
        self.role, self.name, self.kids = role, name, list(kids)
VISITS = []
@pytest.fixture(autouse=True)
def fake_tree(monkeypatch):
    VISITS.clear()
    monkeypatch.setattr(c, 'log', lambda m: None)
    monkeypatch.setattr(c, 'kids', lambda o: (VISITS.append(o), o.kids)[1])
    monkeypatch.setattr(c, 'role', lambda o: o.role)
    monkeypatch.setattr(c, 'nm', lambda o: o.name)

def body(n=50): return [N('static', f'text {i}') for i in range(n)]
def transcript(first, last, name='Chat messages'):
    return N('panel', name, [N('section', '', [N('article', f'Message {i}', body()) for i in range(first, last + 1)]), N('static', 'Thinking…')])
def page(panel):
    return N('document web', 'Claude', [N('panel', 'sidebar', [N('push button', 'Projects')]), panel,
                                         N('section', 'composer', [N('entry', 'Write your prompt to Claude'), N('push button', 'Add'), N('push button', 'Send')])])

def test_articles_found_without_walking_bodies():
    p = transcript(1, 30)
    arts = c.kids_deep_articles(p)
    assert [c.article_index(a) for a in arts] == list(range(1, 31))
    assert not any(v.role == 'static' and v.name.startswith('text') for v in VISITS), 'no message body was walked'

def test_last_index_is_cheap(monkeypatch):
    p = transcript(200, 241); d = page(p)
    monkeypatch.setattr(c, 'main_doc', lambda: (d, '/x'))
    assert c.last_index(None) == 241
    assert not any(v.name.startswith('text') for v in VISITS)

def test_walk_ui_skips_transcripts():
    d = page(transcript(1, 10))
    names = [e.name for e in c.walk_ui(d)]
    assert 'Send' in names and 'Chat messages' in names and 'Message 1' not in names

def test_msg_panel_does_not_walk_the_other_transcript(monkeypatch):
    primary = transcript(1, 40, 'Chat messages'); thread = transcript(1, 3, 'Thread messages')
    d = N('document web', 'Claude', [primary, thread])
    monkeypatch.setattr(c, 'main_doc', lambda: (d, '/x'))
    assert c.msg_panel('cmsg_x') is thread
    assert not any(v.role == 'article' for v in VISITS)

def test_read_messages_parses_only_the_tail(monkeypatch):
    p = transcript(1, 40); d = page(p)
    monkeypatch.setattr(c, 'main_doc', lambda: (d, '/x'))
    parsed = []
    monkeypatch.setattr(c, 'parse_article', lambda a, *x, **k: (parsed.append(c.article_index(a)), {'index': c.article_index(a), 'role': 'user', 'text': 't'})[1])
    msgs = c.read_messages(None, 5)
    assert [m['index'] for m in msgs] == [36, 37, 38, 39, 40] and parsed == [36, 37, 38, 39, 40]
    parsed.clear()
    assert [m['index'] for m in c.read_messages(None, 0, min_index=38)] == [39, 40] and parsed == [39, 40]

def test_read_messages_reuses_finished_messages(monkeypatch):
    p = transcript(1, 12); d = page(p)
    monkeypatch.setattr(c, 'main_doc', lambda: (d, '/x'))
    parsed = []
    monkeypatch.setattr(c, 'parse_article', lambda a, *x, **k: (parsed.append(c.article_index(a)), {'index': c.article_index(a), 'role': 'assistant', 'text': 'new'})[1])
    cached = {'messages': [{'index': i, 'role': 'assistant', 'text': f'old {i}', 'truncated_in_app': i == 9} for i in range(3, 12)]}
    reuse = c.reusable_messages(cached)
    assert 11 not in reuse and 9 not in reuse and 10 in reuse     # the last read's latest and truncated ones are parsed again
    msgs = c.read_messages(None, 5, reuse=reuse)
    assert [m['index'] for m in msgs] == [8, 9, 10, 11, 12]
    assert sorted(parsed) == [9, 11, 12] and msgs[0]['text'] == 'old 8'

def test_reuse_is_off_for_expand(monkeypatch):
    p = transcript(1, 4); d = page(p)
    monkeypatch.setattr(c, 'main_doc', lambda: (d, '/x'))
    monkeypatch.setattr(c, 'parse_article', lambda a, *x, **k: {'index': c.article_index(a), 'role': 'user', 'text': 'fresh'})
    msgs = c.read_messages(None, 2, expand=True, reuse={3: {'index': 3, 'text': 'old'}})
    assert all(m['text'] == 'fresh' for m in msgs)

def test_card_scan_tail_only_covers_last_three_messages():
    p = N('panel', 'Chat messages', [N('article', f'Message {i}', [N('push button', f'Allow {i}')]) for i in range(1, 8)] + [N('push button', 'Deny tail')])
    d = N('document web', '', [p, N('dialog', 'x', [N('push button', 'Outside')])])
    names = [e.name for e in c._card_scan_nodes(d, True)]
    assert 'Allow 7' in names and 'Allow 5' in names and 'Allow 4' not in names and 'Deny tail' in names and 'Outside' in names
    assert 'Allow 1' in [e.name for e in c._card_scan_nodes(d, False)], 'presses and the watcher keep the full scan'

def test_preempt_refires_until_released(monkeypatch):
    monkeypatch.setattr(c, '_HELD', {'op': 'refresh x', 'since': time.time() - 60})
    monkeypatch.setattr(c, '_PREEMPT', {'armed': True, 'off': False})
    monkeypatch.setattr(c, 'others_waiting', lambda: [{'pid': 1}])
    for _ in range(2):                                   # a swallowed first interrupt does not disarm the cap
        with pytest.raises(c.Preempted): c._preempt_signal(None, None)
    c._PREEMPT['armed'] = False
    c._preempt_signal(None, None)                        # released: nothing raised

def test_bulk_cap_default_is_short():
    assert c.BULK_HARD_CAP_S <= 10

def test_one_page_snapshot_serves_pane_info_dialogs_and_busy(monkeypatch):
    d = N('document web', '', [N('push button', 'Running on Desktop-PC'), N('toggle button', 'Threads, 2 threads waiting on you'),
                               N('dialog', 'Heads up'), transcript(1, 5), N('push button', 'Stop response')])
    monkeypatch.setattr(c, 'main_doc', lambda: (d, '/x'))
    monkeypatch.setattr(c, 'text_of', lambda e: e.name)
    snap = c.ui_snapshot(d); VISITS.clear()
    info = c.pane_info(None, snap)
    assert info['environment'] == 'Running on Desktop-PC' and info['threads_waiting'] == 2
    assert c.dialogs(snap) == ['Heads up'] and c.busy_state(None, snap) is True
    assert VISITS == [], 'no second walk of the page'

# ---- read single-flight: simultaneous identical reads share one UI pass, even when they start together
def test_simultaneous_identical_reads_share_one_pass(monkeypatch, tmp_path):
    import threading
    monkeypatch.setattr(c, 'CACHE', tmp_path)
    monkeypatch.setattr(c, 'resolve_project', lambda q, snap=None: {'id': 'chan_X', 'name': 'Sketchpad', 'primary_thread_id': None})
    monkeypatch.setattr(c, 'resolve_thread', lambda chan, t, ths=None: None)
    calls = []
    def dispatch(*a, **k):
        calls.append(1); time.sleep(0.4); return {'read_at': c.now().isoformat(), 'messages': [], 'freshness': {'source': 'live'}}
    monkeypatch.setattr(c, '_read_dispatch', dispatch)
    out = []
    ts = [threading.Thread(target=lambda: out.append(c.op_read('Sketchpad', None, True, 20))) for _ in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(calls) == 1 and len(out) == 4
    assert sorted(o['freshness']['source'] for o in out) == ['coalesced'] * 3 + ['live']

def test_different_read_shapes_do_not_share(monkeypatch, tmp_path):
    monkeypatch.setattr(c, 'CACHE', tmp_path)
    monkeypatch.setattr(c, 'resolve_project', lambda q, snap=None: {'id': 'chan_X', 'name': 'Sketchpad', 'primary_thread_id': None})
    monkeypatch.setattr(c, 'resolve_thread', lambda chan, t, ths=None: None)
    calls = []
    monkeypatch.setattr(c, '_read_dispatch', lambda *a, **k: (calls.append(a[3]), {'read_at': c.now().isoformat(), 'freshness': {'source': 'live'}})[1])
    c.op_read('Sketchpad', None, True, 20); c.op_read('Sketchpad', None, True, 5)
    assert calls == [20, 5]

def test_shared_failure_reaches_joiners(monkeypatch, tmp_path):
    import threading
    monkeypatch.setattr(c, 'CACHE', tmp_path)
    monkeypatch.setattr(c, 'resolve_project', lambda q, snap=None: {'id': 'chan_X', 'name': 'Sketchpad', 'primary_thread_id': None})
    monkeypatch.setattr(c, 'resolve_thread', lambda chan, t, ths=None: None)
    calls = []
    def boom(*a, **k): calls.append(1); time.sleep(0.4); raise c.Busy('app busy')
    monkeypatch.setattr(c, '_read_dispatch', boom)
    errs = []
    def run():
        try: c.op_read('Sketchpad')
        except c.Busy as ex: errs.append(str(ex))
    ts = [threading.Thread(target=run) for _ in range(3)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(calls) == 1 and len(errs) == 3

def test_background_read_yields_only_to_interactive_waiters(monkeypatch):
    monkeypatch.setattr(c, 'others_waiting', lambda: [{'pid': 1, 'priority': 2}])
    assert not c.interactive_waiting()
    monkeypatch.setattr(c, 'others_waiting', lambda: [{'pid': 1, 'priority': 2}, {'pid': 2, 'priority': 1}])
    assert c.interactive_waiting()

def test_final_attachment_try_has_a_longer_but_bounded_cap(monkeypatch):
    monkeypatch.setattr(c, '_HELD', {'op': 'attachment get 1.1', 'since': time.time() - 30})
    monkeypatch.setattr(c, 'others_waiting', lambda: [{'pid': 1}])
    monkeypatch.setattr(c, '_PREEMPT', {'armed': True, 'off': False})
    assert c.bulk_over_cap()
    c._PREEMPT['cap'] = c.ATT_FINAL_CAP_S
    assert not c.bulk_over_cap()
    monkeypatch.setattr(c, '_HELD', {'op': 'attachment get 1.1', 'since': time.time() - c.ATT_FINAL_CAP_S - 1})
    assert c.bulk_over_cap()

def test_go_home_skipped_when_preempted(monkeypatch):
    monkeypatch.setattr(c, '_HELD', {'op': 'refresh x', 'since': time.time() - 60})
    monkeypatch.setattr(c, '_PREEMPT', {'armed': True, 'off': False, 'fired': 1})
    monkeypatch.setattr(c, 'route', lambda: (_ for _ in ()).throw(AssertionError('no navigation')))
    assert c.go_home() is False
