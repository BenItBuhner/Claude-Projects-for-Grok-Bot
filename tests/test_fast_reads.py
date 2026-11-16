"""Less UI per call (Nov 16): reads answer from a fresh cache, skip unchanged card/state scans, sends queue first and
the post check looks at the cache before the app. No app, no network."""
import datetime, json, time
import pytest
from claude_projects import core as C
from claude_projects.origin import add_origin


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(C, 'log', lambda m: None)
    monkeypatch.setattr(C, 'CACHE', tmp_path / 'cache'); (tmp_path / 'cache' / 'send-jobs').mkdir(parents=True)
    monkeypatch.setattr(C, 'UIQ', tmp_path / 'ui.queue')
    monkeypatch.setattr(C, 'resolve_project', lambda p, snap=None: {'id': 'chan_X', 'name': 'Sketchpad', 'primary_thread_id': None})
    monkeypatch.setattr(C, 'resolve_thread', lambda chan, t, ths=None: None)
    monkeypatch.setattr(C, 'same_read_inflight', lambda op: None)
    return tmp_path


def iso(sec_ago):
    return (C.now() - datetime.timedelta(seconds=sec_ago)).isoformat()


def reads(monkeypatch):
    calls = []
    monkeypatch.setattr(C, '_op_read_impl', lambda project, thread=None, refresh=True, *a, **k: (calls.append(refresh), {'read_at': iso(0)})[1])
    return calls


def test_fresh_cache_answers_without_a_ui_pass(monkeypatch):
    C.write_cache('chan_X', None, {'read_at': iso(5), 'messages': []})
    calls = reads(monkeypatch)
    assert C.op_read('Sketchpad')['freshness']['source'] == 'fresh' and calls == [False]


def test_old_cache_or_a_send_since_goes_live(monkeypatch, state):
    calls = reads(monkeypatch)
    C.write_cache('chan_X', None, {'read_at': iso(C.READ_FRESH_S + 5), 'messages': []})
    assert C.op_read('Sketchpad')['freshness']['source'] == 'live'
    C.write_cache('chan_X', None, {'read_at': iso(5), 'messages': []})
    (state / 'cache' / 'send-jobs' / 's1.json').write_text(json.dumps({'spec': {'project': 'chan_X'}, 'started_ts': time.time() - 1}))
    assert C.op_read('Sketchpad')['freshness']['source'] == 'live' and calls == [True, True]


def test_card_scan_runs_only_when_the_tail_changed_or_a_card_showed():
    msgs = [{'index': 7, 'role': 'assistant', 'text': 'Working on it'}]
    sig = C._cards_sig(msgs, False); recent = {'sig': sig, 'ts': time.time()}
    assert not C._cards_due(recent, sig, None)
    assert C._cards_due(recent, C._cards_sig(msgs + [{'index': 8, 'role': 'assistant', 'text': 'Allow?'}], False), None)
    assert C._cards_due(recent, sig, [{'card_id': 'c1'}])
    assert C._cards_due({'sig': sig, 'ts': time.time() - C.CARD_SCAN_EVERY_S - 1}, sig, None)


def test_a_send_queued_after_reads_runs_first():
    C._ticket_new('read Sketchpad primary'); time.sleep(0.001); C._ticket_new('send Sketchpad primary')
    assert [d['op'] for _, d in C._tickets()][0] == 'send Sketchpad primary'


def test_post_check_finds_the_post_in_the_cache_without_the_app(monkeypatch):
    text = add_origin('Ship the beta build tonight')
    C.write_cache('chan_X', None, {'read_at': iso(3), 'messages': [{'index': 12, 'role': 'user', 'text': text}]})
    def no_ui(*a, **k): raise AssertionError('took the UI lock')
    monkeypatch.setattr(C, 'ui_lock', no_ui)
    assert C.find_existing_post('Sketchpad', None, text, before_max=11)['index'] == 12
