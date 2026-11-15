"""Computer-use permission cards in a thread (the request for screen control), from the live tree seen on 2026-11-15."""
import json, pathlib, sys
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import permissions as P
from claude_projects import core as C


def desktop_card(allow='Allow for this session'):
    """The card as the app's accessibility tree showed it (paths and names as seen; ids renumbered)."""
    rows = [(0, None, 'section', ''), (1, 0, 'status bar', ''), (2, 1, 'static', 'Claude wants to use your computer. Choose an option to respond.'),
            (3, 0, 'section', ''), (4, 3, 'static', 'Claude wants to use:'), (5, 3, 'static', 'Sketchpad'), (6, 3, 'static', 'All'),
            (7, 3, 'section', ''), (8, 7, 'static', 'c:\\users\\alex\\appdata\\local\\programs\\sketchpad\\sketchpad.exe'),
            (9, 3, 'static', 'Notepad'), (10, 3, 'static', 'All'),
            (11, 3, 'paragraph', 'Your other windows will be hidden, then restored when Claude is done.'),
            (12, 11, 'static', 'Your other windows will be hidden, then restored when Claude is done.'),
            (13, 3, 'push button', 'Deny'), (14, 3, 'section', ''), (15, 14, 'section', ''), (16, 15, 'section', ''),
            (17, 16, 'push button', allow)]
    return [{'id': i, 'parent': par, 'role': r, 'name': n if r == 'push button' else '', 'text': '' if r == 'push button' else n}
            for i, par, r, n in rows]


def json_card():
    """The generic tool-approval rendering: 'Computer Request Access requests permission' + the request as JSON."""
    req = {'apps': [{'bundleId': 'c:\\users\\alex\\appdata\\local\\programs\\sketchpad\\sketchpad.exe', 'displayName': 'Sketchpad', 'tier': 'full'},
                    {'bundleId': 'notepad.exe', 'displayName': 'Notepad', 'tier': 'full'}], 'reason': 'run the on-screen checks'}
    rows = [(0, None, 'section', ''), (1, 0, 'heading', 'Computer Request Access requests permission'),
            (2, 0, 'code', json.dumps(req, indent=2)), (3, 0, 'push button', 'Allow once'), (4, 0, 'push button', 'Deny')]
    return [{'id': i, 'parent': par, 'role': r, 'name': n if r == 'push button' else '', 'text': '' if r == 'push button' else n}
            for i, par, r, n in rows]


@pytest.mark.parametrize('allow', ['Allow for this session', 'Allow once', 'Allow'])
def test_desktop_card_is_a_computer_use_card_with_apps(allow):
    [c] = P.find_cards(desktop_card(allow))
    assert c['type'] == 'computer_use' and c['options'] == ['Deny', allow] and c['affirmative_options'] == [allow]
    assert [(a['displayName'], a['tier']) for a in c['apps']] == [('Sketchpad', 'full'), ('Notepad', 'full')]
    assert c['apps'][0]['bundleId'].endswith('sketchpad.exe') and c['apps'][1]['bundleId'] is None
    assert c['target'] == 'Sketchpad, Notepad' and c['tiers'] == ['full']


def test_json_card_variant():
    [c] = P.find_cards(json_card())
    assert c['type'] == 'computer_use' and c['options'] == ['Allow once', 'Deny']
    assert [(a['displayName'], a['bundleId'], a['tier']) for a in c['apps']] == [
        ('Sketchpad', 'c:\\users\\alex\\appdata\\local\\programs\\sketchpad\\sketchpad.exe', 'full'), ('Notepad', 'notepad.exe', 'full')]


class FakeLock:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Btn:
    def __init__(self, label): self.label = label


def _env(monkeypatch, nodes, take_on_continue):
    cards = P.find_cards(nodes); cards[0]['_objs'] = [Btn(o) for o in cards[0]['options']]
    state = {'cards': cards, 'pressed': [], 'intro': False}
    monkeypatch.setattr(C, 'resolve_project', lambda p: {'id': 'chan_TEST', 'name': 'Demo', 'primary_thread_id': 'cse_TEST'})
    monkeypatch.setattr(C, '_resolve_tid', lambda p, chan, thread: thread)
    monkeypatch.setattr(C, 'open_target_settled', lambda chan, tid: None)
    monkeypatch.setattr(C, 'go_home', lambda: True)
    monkeypatch.setattr(C, 'ui_lock', FakeLock)
    monkeypatch.setattr(C, 'busy_state', lambda tid: True)
    monkeypatch.setattr(C, 'dialogs', lambda: [])   # never the real accessibility bus (CI has none)
    monkeypatch.setattr(C, 'log', lambda m: None); monkeypatch.setattr(C.time, 'sleep', lambda s: None)
    monkeypatch.setattr(C, 'detect_permission_cards', lambda: state['cards'])
    def fake_act(e, what=''):
        state['pressed'].append(e.label)
        if take_on_continue: state['intro'] = True        # Allow only opens the intro dialog
        else: state['cards'] = []
        return True
    monkeypatch.setattr(C, 'act', fake_act)
    def fake_finish(wait_s=6.0):
        if not state['intro']: return []
        state['pressed'].append('Continue'); state['cards'] = []; return ['Continue (Background computer use intro)']
    monkeypatch.setattr(C, '_finish_computer_use_allow', fake_finish)
    return state


def test_allow_goes_through_the_background_intro(monkeypatch):
    """Root cause of the 3:58 / 4:01 PM presses: Allow opened the one-time intro; the grant needs its Continue."""
    st = _env(monkeypatch, desktop_card(), take_on_continue=True)
    r = C.op_permission('Demo', 'Allow for this session', 'cmsg_T', dry_run=False)
    assert st['pressed'] == ['Allow for this session', 'Continue'] and r['card_gone'] and r['also_pressed'] and r['claude_working']


def test_allow_once_variant_without_intro(monkeypatch):
    st = _env(monkeypatch, json_card(), take_on_continue=False)
    r = C.op_permission('Demo', 'Allow once', 'cmsg_T', dry_run=False)
    assert st['pressed'] == ['Allow once'] and r['card_gone'] and 'also_pressed' not in r


def test_deny_never_touches_the_intro(monkeypatch):
    st = _env(monkeypatch, desktop_card(), take_on_continue=False)
    called = []
    monkeypatch.setattr(C, '_finish_computer_use_allow', lambda wait_s=6.0: called.append(1) or [])
    C.op_permission('Demo', 'Deny', 'cmsg_T', dry_run=False)
    assert st['pressed'] == ['Deny'] and called == []


def test_still_showing_is_reported_not_hidden(monkeypatch):
    st = _env(monkeypatch, desktop_card(), take_on_continue=True)
    monkeypatch.setattr(C, '_finish_computer_use_allow', lambda wait_s=6.0: [])   # no dialog came up
    monkeypatch.setattr(C, '_press_diagnostics', lambda b: {'button': {'name': b.label, 'busy': True}, 'notices': []})
    r = C.op_permission('Demo', 'Allow for this session', 'cmsg_T', dry_run=False)
    assert r['card_gone'] is False and 'still showing' in r['warning'] and r['diagnostics']['button']['busy']


