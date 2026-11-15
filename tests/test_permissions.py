"""Generic permission-card detection on mock accessibility snapshots (no app, no display)."""
import pathlib, sys
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import permissions as P
from claude_projects import core as C


class Tree:
    """Build {id, parent, role, name, text} snapshots the way core_mod._snapshot_subtree does."""
    def __init__(self): self.nodes = []
    def add(self, role, name='', text='', parent=None):
        nid = len(self.nodes); self.nodes.append({'id': nid, 'parent': parent, 'role': role, 'name': name, 'text': text}); return nid


def folder_card(path=r'C:\Users\someone\AppData\Roaming\ExampleApp'):
    t = Tree(); doc = t.add('document web')
    t.add('article', 'Message 7', parent=doc)
    region = t.add('section', 'Quick reply and approval controls', parent=doc)
    box = t.add('panel', parent=region)
    t.add('static', text='Claude would like to add this folder to the session:', parent=box)
    t.add('static', text=path, parent=box)
    row = t.add('panel', parent=box)
    t.add('push button', 'Add folder', parent=row); t.add('push button', 'Not now', parent=row)
    comp = t.add('panel', parent=doc); t.add('push button', 'Send', parent=comp); t.add('push button', 'Cancel', parent=comp)
    return t


def test_add_folder_card():
    cards = P.find_cards(folder_card().nodes)
    assert len(cards) == 1
    c = cards[0]
    assert c['type'] == 'add_folder' and c['target'] == r'C:\Users\someone\AppData\Roaming\ExampleApp'
    assert c['options'] == ['Add folder', 'Not now'] and c['affirmative_options'] == ['Add folder'] and c['negative_options'] == ['Not now']
    assert c['card_id'].startswith('perm_') and c['card_id'] == P.find_cards(folder_card().nodes)[0]['card_id']   # stable


def test_unix_folder_and_card_id_changes_with_target():
    a = P.find_cards(folder_card('/srv/work/app').nodes)[0]
    b = P.find_cards(folder_card('/srv/work/other').nodes)[0]
    assert a['target'] == '/srv/work/app' and a['card_id'] != b['card_id']


@pytest.mark.parametrize('prompt,opts,kind,target', [
    ('Claude wants to add a repository: octo-org/widgets', ['Add repository', 'Not now'], 'add_repository', 'octo-org/widgets'),
    ('Allow Claude to use this machine? build-box-2', ['Allow', 'Deny'], 'allow_machine', None),
    ('Allow Claude to run `npm test`?', ['Allow once', 'Always allow', 'Deny'], 'run_command', 'npm test'),
    ('Claude needs permission to fetch https://example.com/data.json', ['Allow', "Don't allow"], 'network_access', None),
])
def test_other_card_types(prompt, opts, kind, target):
    t = Tree(); doc = t.add('document web'); box = t.add('dialog', parent=doc)
    t.add('static', text=prompt, parent=box)
    for o in opts: t.add('push button', o, parent=box)
    cards = P.find_cards(t.nodes)
    assert len(cards) == 1 and cards[0]['type'] == kind and cards[0]['options'] == opts
    if target: assert cards[0]['target'] == target


def test_no_card_for_plain_buttons():
    t = Tree(); doc = t.add('document web'); p = t.add('panel', parent=doc)
    t.add('static', text='Here is the summary you asked for.', parent=p)
    t.add('push button', 'Cancel', parent=p); t.add('push button', 'Continue', parent=p)
    assert P.find_cards(t.nodes) == []


def test_negative_only_prompt_is_not_a_card():
    t = Tree(); doc = t.add('document web'); p = t.add('panel', parent=doc)
    t.add('static', text='Claude would like to stop here', parent=p); t.add('push button', 'Dismiss', parent=p)
    assert P.find_cards(t.nodes) == []


def test_match_option():
    c = P.find_cards(folder_card().nodes)[0]
    assert P.match_option(c, 'add folder') == ('Add folder', 0)
    assert P.match_option(c, 'Not') == ('Not now', 1)
    with pytest.raises(ValueError): P.match_option(c, 'Allow')


def test_public_strips_private_fields():
    c = P.public(P.find_cards(folder_card().nodes))[0]
    assert not any(k.startswith('_') for k in c)


def test_current_state_waits_on_permission_card():
    msgs = [{'index': 7, 'role': 'assistant', 'text': 'Working on it.', 'time_utc': '2026-11-14T15:00:00Z'}]
    C.annotate_messages_staleness(msgs)
    cards = P.find_cards(folder_card().nodes)
    cs = C.build_current_state(msgs, busy=False, permission_cards=cards)
    assert cs['waiting_on'] == 'permission' and cs['status'] == 'waiting_on_user' and cs['permission_pending']
    assert cs['permission_cards'][0]['type'] == 'add_folder' and 'claude_thread_permission' in cs['note']


def test_current_state_prose_stall():
    msgs = [{'index': 3, 'role': 'assistant', 'text': 'The migration is ready. I need your approval to run it against staging.', 'time_utc': '2026-11-14T15:00:00Z'}]
    C.annotate_messages_staleness(msgs)
    cs = C.build_current_state(msgs, busy=False)
    assert cs['waiting_on'] == 'permission' and cs['permission_hint']
    busy = C.build_current_state(msgs, busy=True)
    assert busy['waiting_on'] == 'claude' and not busy['permission_pending']


def test_current_state_no_permission():
    msgs = [{'index': 3, 'role': 'assistant', 'text': 'Done. Tests pass.', 'time_utc': '2026-11-14T15:00:00Z'}]
    C.annotate_messages_staleness(msgs)
    cs = C.build_current_state(msgs, busy=False)
    assert cs['permission_pending'] is False and cs['waiting_on'] != 'permission'


class FakeLock:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_op_permission_dry_run_and_apply(monkeypatch):
    cards = P.find_cards(folder_card().nodes)
    pressed = []
    class Btn:
        def __init__(self, label): self.label = label
    cards[0]['_objs'] = [Btn(o) for o in cards[0]['options']]
    state = {'cards': cards}
    monkeypatch.setattr(C, 'resolve_project', lambda p: {'id': 'chan_TEST', 'name': 'Demo', 'primary_thread_id': 'cse_TEST'})
    monkeypatch.setattr(C, '_resolve_tid', lambda p, chan, thread: thread)
    monkeypatch.setattr(C, 'open_target_settled', lambda chan, tid: None)
    monkeypatch.setattr(C, 'go_home', lambda: True)
    monkeypatch.setattr(C, 'ui_lock', FakeLock)
    monkeypatch.setattr(C, 'log', lambda m: None); monkeypatch.setattr(C.time, 'sleep', lambda s: None)
    monkeypatch.setattr(C, 'busy_state', lambda tid: False); monkeypatch.setattr(C, 'dialogs', lambda: [])   # no real accessibility bus
    monkeypatch.setattr(C, 'detect_permission_cards', lambda: state['cards'])
    def fake_act(e, what=''):
        pressed.append(e.label); state['cards'] = []; return True
    monkeypatch.setattr(C, 'act', fake_act)
    r = C.op_permission('Demo', 'add folder')
    assert r['dry_run'] and r['would_press'] == 'Add folder' and pressed == [] and r['address'] == 'chan_TEST/cse_TEST'
    with pytest.raises(C.ClaudeProjectsError): C.op_permission('Demo', 'Allow')
    with pytest.raises(C.ClaudeProjectsError): C.op_permission('Demo', '')
    r = C.op_permission('Demo', 'Not now', dry_run=False)
    assert pressed == ['Not now'] and r['card_gone'] and r['pressed'] == 'Not now'
    with pytest.raises(C.NotFound): C.op_permission('Demo', 'Add folder')
