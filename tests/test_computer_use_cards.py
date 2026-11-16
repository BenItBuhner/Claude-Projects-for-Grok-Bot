"""Computer-use permission cards in a thread (the request for screen control), from the live tree seen on 2026-11-15."""
import json
import pytest
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


def _env(monkeypatch, nodes, take_on_continue, on_this_desktop=True, button_state=None):
    """on_this_desktop=True: the app here IS the computer Claude controls (the Allow can take effect here)."""
    cards = P.find_cards(nodes); cards[0]['_objs'] = [Btn(o) for o in cards[0]['options']]
    state = {'cards': cards, 'pressed': [], 'intro': False}
    monkeypatch.setitem(C.CFG, 'computer_use_on_this_desktop', on_this_desktop)
    monkeypatch.setattr(C, 'resolve_project', lambda p: {'id': 'chan_TEST', 'name': 'Demo', 'primary_thread_id': 'cse_TEST'})
    monkeypatch.setattr(C, '_resolve_tid', lambda p, chan, thread: thread)
    monkeypatch.setattr(C, 'open_target_settled', lambda chan, tid: None)
    monkeypatch.setattr(C, 'go_home', lambda: True)
    monkeypatch.setattr(C, 'ui_lock', FakeLock)
    monkeypatch.setattr(C, 'ui_queue_status', lambda: {'holder': None, 'queue': [], 'length': 0})
    monkeypatch.setattr(C, 'busy_state', lambda tid: True)
    monkeypatch.setattr(C, 'dialogs', lambda: [])   # never the real accessibility bus (CI has none)
    monkeypatch.setattr(C, 'log', lambda m: None); monkeypatch.setattr(C.time, 'sleep', lambda s: None)
    monkeypatch.setattr(C, 'detect_permission_cards', lambda: state['cards'])
    monkeypatch.setattr(C, '_btn_state', lambda b: button_state)
    monkeypatch.setattr(C, '_wait_button_gone', lambda b, timeout_s=8.0, step=0.5: (not state['cards'], 0.5))
    def fake_act(e, what=''):
        state['pressed'].append(e.label)
        if take_on_continue: state['intro'] = True        # Allow only opens the intro dialog
        else: state['cards'] = []
        return True
    monkeypatch.setattr(C, 'act', fake_act)
    monkeypatch.setattr(C, 'real_click', lambda e, what='': (fake_act(e, what), 'click')[1])
    def fake_finish(wait_s=4.0):
        if not state['intro']: return []
        state['pressed'].append('Continue'); state['cards'] = []; return ['Continue (Background computer use intro)']
    monkeypatch.setattr(C, '_finish_computer_use_allow', fake_finish)
    return state


def probe_card(device='Desktop-PC'):
    """The enable step as a desktop that is not the controlled computer shows it."""
    rows = [(0, None, 'section', ''), (1, 0, 'status bar', ''), (2, 1, 'static', 'Claude wants to use your computer. Choose an option to respond.'),
            (3, 0, 'section', ''), (4, 3, 'heading', 'Claude wants to use your computer'),
            (5, 3, 'static', f'Check that computer use is turned on in the Claude desktop app on {device} (Settings \u2192 Computer use), then continue.'),
            (6, 3, 'push button', 'Deny'), (7, 3, 'push button', 'Continue')]
    return [{'id': i, 'parent': par, 'role': r, 'name': n if r == 'push button' else '', 'text': '' if r == 'push button' else n}
            for i, par, r, n in rows]


def test_allow_goes_through_the_background_intro(monkeypatch):
    """On the controlled computer: Allow opens the one-time intro; the grant needs its Continue."""
    st = _env(monkeypatch, desktop_card(), take_on_continue=True)
    r = C.op_permission('Demo', 'Allow for this session', 'cmsg_T', dry_run=False)
    assert st['pressed'] == ['Allow for this session', 'Continue'] and r['card_gone'] and r['also_pressed'] and r['claude_working']


def test_allow_once_variant_without_intro(monkeypatch):
    st = _env(monkeypatch, json_card(), take_on_continue=False)
    r = C.op_permission('Demo', 'Allow once', 'cmsg_T', dry_run=False)
    assert st['pressed'] == ['Allow once'] and r['card_gone'] and 'also_pressed' not in r


def test_deny_never_touches_the_intro(monkeypatch):
    st = _env(monkeypatch, desktop_card(), take_on_continue=False, on_this_desktop=False)
    called = []
    monkeypatch.setattr(C, '_finish_computer_use_allow', lambda wait_s=4.0: called.append(1) or [])
    C.op_permission('Demo', 'Deny', 'cmsg_T', dry_run=False)
    assert st['pressed'] == ['Deny'] and called == []


def test_still_showing_is_reported_not_hidden(monkeypatch):
    _env(monkeypatch, desktop_card(), take_on_continue=True)
    monkeypatch.setattr(C, '_finish_computer_use_allow', lambda wait_s=4.0: [])   # no dialog came up
    r = C.op_permission('Demo', 'Allow for this session', 'cmsg_T', dry_run=False)
    assert r['card_gone'] is False and 'still showing' in r['warning'] and 'diagnostics' in r


def test_replay_apps_card_real_click_from_other_desktop(monkeypatch):
    st = _env(monkeypatch, desktop_card(), take_on_continue=False, on_this_desktop=False)
    assert st['cards'][0]['card_id'] == 'perm_27ea95c718'
    r = C.op_permission('Demo', 'Allow for this session', 'cmsg_T', dry_run=False)
    assert r['card_gone'] and r['pressed_with'] == 'click'


def test_only_apps_refuses_extra_apps(monkeypatch):
    st = _env(monkeypatch, desktop_card(), take_on_continue=False)
    r = C.op_permission('Demo', 'Allow for this session', 'cmsg_T', dry_run=False, only_apps=['Sketchpad'])
    assert st['pressed'] == [] and r['refused'] == 'apps_not_allowed' and r['apps_outside'] == ['Notepad']
    r = C.op_permission('Demo', 'Allow for this session', 'cmsg_T', dry_run=False, only_apps=['sketchpad.exe', 'notepad'])
    assert st['pressed'] == ['Allow for this session'] and r['card_gone']


def test_lasting_grant_is_refused(monkeypatch):
    st = _env(monkeypatch, desktop_card('Always allow'), take_on_continue=False)
    r = C.op_permission('Demo', 'Always allow', 'cmsg_T', dry_run=False, only_apps=['Sketchpad', 'Notepad'])
    assert st['pressed'] == [] and r['refused'] == 'lasting_grant'


def test_disabled_button_is_not_pressed(monkeypatch):
    st = _env(monkeypatch, desktop_card(), take_on_continue=False, button_state={'name': 'Allow for this session', 'enabled': True, 'sensitive': False, 'showing': True})
    r = C.op_permission('Demo', 'Allow for this session', 'cmsg_T', dry_run=False)
    assert st['pressed'] == [] and r['refused'] == 'button_disabled'


def test_probe_card_continue_is_pressable_from_another_desktop(monkeypatch):
    st = _env(monkeypatch, probe_card(), take_on_continue=False, on_this_desktop=False)
    c = st['cards'][0]
    assert c['type'] == 'computer_use' and c['cu_kind'] == 'probe' and c['device'] == 'Desktop-PC' and c['options'] == ['Deny', 'Continue']
    called = []
    monkeypatch.setattr(C, '_finish_computer_use_allow', lambda wait_s=4.0: called.append(1) or [])
    r = C.op_permission('Demo', 'Continue', 'cmsg_T', dry_run=False, only_apps=['Sketchpad', 'Notepad'])
    assert st['pressed'] == ['Continue'] and r['card_gone'] and called == [] and r['computer_use_kind'] == 'probe'


def test_card_watch_presses_only_allowlisted(monkeypatch, tmp_path):
    st = _env(monkeypatch, desktop_card(), take_on_continue=False)
    monkeypatch.setattr(C, 'CARD_WATCH_LOG', tmp_path / 'w.jsonl')
    acts = C.card_watch_once('chan_TEST', 'cmsg_T', ['Sketchpad'])
    assert st['pressed'] == [] and acts[0]['action'] == 'left' and acts[0]['refused'] == 'apps_not_allowed'
    acts = C.card_watch_once('chan_TEST', 'cmsg_T', ['Sketchpad', 'Notepad'])
    assert st['pressed'] == ['Allow for this session'] and acts[0]['action'] == 'pressed' and acts[0]['card_gone']
    assert len((tmp_path / 'w.jsonl').read_text().splitlines()) == 2
