"""The controlled computer's side: decide a computer-use card from a UI Automation snapshot of its Claude app."""
import json, pathlib, subprocess, sys
import pytest
from claude_projects import computer_cards as CC
from test_computer_use_cards import desktop_card, probe_card


def snapshot(nodes, host='DESKTOP-PC', hwnd=900):
    """The shape hosts/windows/computer-permission.ps1 writes: names carry the text, each node has a runtime id."""
    ns = [{'id': n['id'], 'parent': n['parent'], 'role': n['role'], 'name': n['name'] or n['text'], 'text': '', 'rid': f'42.{hwnd}.{n["id"]}'} for n in nodes]
    return {'ok': True, 'action': 'snapshot', 'host': host, 'read_at': '2026-11-15T17:30:00-06:00',
            'windows': [{'hwnd': hwnd, 'pid': 1, 'title': 'Claude', 'blocks': [{'rid': '42.900.0', 'nodes': ns}]}]}


def test_allow_for_allowlisted_apps_gives_the_exact_press_request():
    r = CC.plan(snapshot(desktop_card()), 'Allow for this session', ['Sketchpad', 'Notepad'], apply=True)
    assert r['decision'] == 'press' and r['card_id'] == 'perm_27ea95c718' and r['apps'] == ['Sketchpad', 'Notepad']
    assert r['press_request'] == {'hwnd': 900, 'rid': '42.900.17', 'label': 'Allow for this session', 'action': 'press', 'title': 'Claude wants to use'}


def test_default_is_a_dry_run():
    r = CC.plan(snapshot(desktop_card()), 'Allow for this session', ['Sketchpad', 'Notepad'])
    assert r['decision'] == 'press' and r['would_press'] == 'Allow for this session' and 'press_request' not in r and r['dry_run']


@pytest.mark.parametrize('only,why', [(['Sketchpad'], 'apps outside only_apps: Notepad'), (None, 'only_apps is required')])
def test_other_apps_or_no_allowlist_are_refused(only, why):
    r = CC.plan(snapshot(desktop_card()), 'Allow for this session', only, apply=True)
    assert r['decision'] == 'refuse' and why in r['reason'] and 'press_request' not in r


def test_lasting_grant_and_probe_are_refused():
    assert CC.plan(snapshot(desktop_card('Always allow')), 'Always allow', ['Sketchpad', 'Notepad'], apply=True)['decision'] == 'refuse'
    r = CC.plan(snapshot(probe_card()), 'Continue', ['Sketchpad', 'Notepad'], apply=True)
    assert r['decision'] == 'refuse' and 'not an apps card' in r['reason']


def test_deny_is_always_pressable():
    r = CC.plan(snapshot(desktop_card()), 'Deny', None, apply=True)
    assert r['decision'] == 'press' and r['press_request']['label'] == 'Deny'


def test_no_card():
    r = CC.plan({'host': 'X', 'windows': []}, 'Allow', ['Notepad'])
    assert r['decision'] == 'none'


def test_cli_decide(tmp_path):
    f = tmp_path / 'snap.json'; f.write_text(json.dumps(snapshot(desktop_card())), encoding='utf-8')
    out = subprocess.run([sys.executable, '-m', 'claude_projects', '--json', 'computer-card', 'decide', '--snapshot', str(f),
                          '--option', 'Allow for this session', '--only-apps', 'Sketchpad,Notepad'],
                         capture_output=True, text=True, cwd=str(pathlib.Path(__file__).resolve().parents[1]), timeout=60)
    r = json.loads(out.stdout)
    assert r['decision'] == 'press' and r['would_press'] == 'Allow for this session' and r['dry_run']
