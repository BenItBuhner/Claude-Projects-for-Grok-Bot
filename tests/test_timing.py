"""Per-step timing on the open path, and the shorter waits: settle ends once the transcript is stable, a child thread
the open pane shows is accepted without walking the Threads sidebar. The app is never touched."""
import pathlib, sys
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
import cpg_core as C


@pytest.fixture
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(C.time, 'time', lambda: t[0])
    monkeypatch.setattr(C.time, 'sleep', lambda s: t.__setitem__(0, t[0] + s))
    lines = []
    monkeypatch.setattr(C, 'log', lambda m: lines.append(m))
    return t, lines


def test_step_timer_logs_each_step(clock):
    t, lines = clock
    with C.step_timer('attachment Demo 138.2') as tm:
        t[0] += 1.5; C._mark('deeplink'); t[0] += 2; C._mark('settle'); t[0] += .5; C._mark('settle')
    d = tm.as_dict()
    assert d['deeplink'] == 1.5 and d['settle'] == 2.5 and d['total'] == 4.0
    assert lines[-1] == 'timing attachment Demo 138.2: deeplink 1.5, settle 2.5, total 4.0'
    C._mark('outside')          # no timer: a no-op
    assert C._TIMER is None


def test_settle_returns_soon_after_the_transcript_is_stable(clock, monkeypatch):
    t, _ = clock
    monkeypatch.setattr(C, 'msg_panel', lambda thread: 'panel')
    monkeypatch.setattr(C, 'kids', lambda p: [])
    monkeypatch.setattr(C, 'kids_deep_articles', lambda p: ['a'] * (3 if t[0] < 1001 else 5))
    monkeypatch.setattr(C, 'nm', lambda e: 'Message 5')
    t0 = t[0]
    assert C.settle(None, 25) is True
    assert t[0] - t0 <= 2.5, f'settled after {t[0] - t0:.1f} s; the old 1 s polls took 3+ s'


def test_settle_waits_while_loading(clock, monkeypatch):
    t, _ = clock
    monkeypatch.setattr(C, 'msg_panel', lambda thread: 'panel')
    monkeypatch.setattr(C, 'kids', lambda p: ['Loading...'] if t[0] < 1004 else [])
    monkeypatch.setattr(C, 'text_of', lambda e: e)
    monkeypatch.setattr(C, 'nm', lambda e: e)
    monkeypatch.setattr(C, 'kids_deep_articles', lambda p: ['Message 1'])
    assert C.settle(None, 25) is True and t[0] >= 1004


def test_new_child_thread_shown_in_the_pane_skips_the_sidebar_walk(clock, monkeypatch):
    monkeypatch.setattr(C, 'deeplink', lambda link: None)
    monkeypatch.setattr(C, 'route', lambda: '/epitaxy/project/chan_A?thread=cmsg_new')
    monkeypatch.setattr(C, 'read_cache', lambda chan: {'threads': [{'thread_id': 'cmsg_old'}]})
    monkeypatch.setattr(C, 'msg_panel', lambda thread: 'panel')
    monkeypatch.setattr(C, 'pane_info', lambda thread: {'thread_title': 'Fix the export button'})
    monkeypatch.setattr(C, 'parse_threads', lambda *a, **k: pytest.fail('the sidebar must not be walked'))
    assert 'cmsg_new' in C.open_target('chan_A', 'cmsg_new')


def test_unknown_child_id_still_checks_the_sidebar(clock, monkeypatch):
    monkeypatch.setattr(C, 'deeplink', lambda link: None)
    monkeypatch.setattr(C, 'route', lambda: '/epitaxy/project/chan_A?thread=cmsg_bad')
    monkeypatch.setattr(C, 'read_cache', lambda chan: {'threads': [{'thread_id': 'cmsg_old', 'title': 'Old'}]})
    monkeypatch.setattr(C, 'msg_panel', lambda thread: None)
    monkeypatch.setattr(C, 'pane_info', lambda thread: {'thread_title': 'Thread'})
    monkeypatch.setattr(C, 'settle', lambda *a, **k: False)
    monkeypatch.setattr(C, 'ensure_threads_panel', lambda *a: None)
    walked = []
    monkeypatch.setattr(C, 'parse_threads', lambda *a, **k: walked.append(1) or [{'thread_id': 'cmsg_old', 'title': 'Old'}])
    monkeypatch.setattr(C, 'write_cache', lambda *a: None)
    with pytest.raises(C.NotFound):
        C.open_target('chan_A', 'cmsg_bad')
    assert walked == [1]


def test_deeplink_passes_display_and_binary_to_the_launcher(clock, monkeypatch):
    seen = {}
    class R: returncode = 0
    monkeypatch.setattr(C.subprocess, 'run', lambda argv, **k: seen.update(k) or R())
    C.deeplink('claude://code/new')
    assert seen['env']['CPG_DISPLAY'] and seen['env']['CPG_CLAUDE_BINARY']
