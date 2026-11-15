"""Threads waiting on the user (child threads too), approval-ask classification, claude_attention (no app, no UI)."""
import datetime, pathlib, sys
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import attention as ATT
from claude_projects import core as C


# ---------------------------------------------------------------- classification

@pytest.mark.parametrize('text,line', [
    ('To finish the store listing I need a one-line approval from you, in your own words, e.g.:\n'
     '> I approve publishing Sketchpad to the store under my account.\nOnce you send it I will continue.',
     'I approve publishing Sketchpad to the store under my account.'),
    ('Please reply with exactly: "Yes, deploy Sketchpad 2.1 to production"', 'Yes, deploy Sketchpad 2.1 to production'),
    ('Reply with this line so it is on record:\n\nI authorize the sign-in migration for Sketchpad.',
     'I authorize the sign-in migration for Sketchpad.'),
    ('I need written approval from you. Paste this line: \u201cI approve the production database change.\u201d',
     'I approve the production database change.'),
    ('Send me one line in your own words approving the laptop sign-in, for example `I approve signing Sketchpad in on my laptop`.',
     'I approve signing Sketchpad in on my laptop'),
])
def test_approval_ask_returns_the_exact_requested_line(text, line):
    a = ATT.classify_ask(text)
    assert a['kind'] == 'approval' and a['requested_line'] == line, a
    assert 'never invent' in a['note'].lower()


def test_approval_ask_from_a_code_block_keeps_it_verbatim():
    text = 'Please reply with the following line, in your own words if you like:\nI approve  the  rollout (v2).\nThanks.'
    a = ATT.classify_ask(text, blocks=[{'type': 'code', 'language': None, 'text': 'I approve  the  rollout (v2).'}])
    assert a['kind'] == 'approval' and a['requested_line'] == 'I approve  the  rollout (v2).'


def test_approval_ask_without_a_line_says_to_ask_the_user():
    a = ATT.classify_ask('Before I continue I need your approval in your own words for the store submission.')
    assert a['kind'] == 'approval' and a['requested_line'] is None and 'ask the user' in a['note']


@pytest.mark.parametrize('text,kind', [
    ('Should I proceed with the release?', 'question'),
    ('Which option would you prefer, A or B?', 'question'),
    ('I need your permission to run this command.', 'permission'),
    ('What you need to do: run the next release; it is yours to run.', 'action'),
    ('Done. All 231 tests pass.', 'update'),
])
def test_other_kinds(text, kind):
    assert ATT.classify_ask(text)['kind'] == kind
    assert ATT.classify_ask(text)['requested_line'] is None


@pytest.mark.parametrize('text', [
    "I'll write the following files next: a.py and b.py.",
    'I will post the following summary in the PR.',
    'Changed one line of code in the parser; the exact text of the error is below.',
    'The log shows the error verbatim.',
    'I sent this to the build server and it passed.',
])
def test_ordinary_updates_are_not_approval_asks(text):
    assert ATT.classify_ask(text)['kind'] != 'approval', text


def test_empty_text_is_no_ask():
    assert ATT.classify_ask('') is None and ATT.classify_ask(None) is None


def test_current_state_treats_an_approval_ask_as_waiting_on_the_user_not_permission():
    msgs = [{'index': 3, 'role': 'assistant', 'text': 'I need your approval in your own words. Reply with exactly: "I approve the deploy"'}]
    cs = C.build_current_state(msgs, False, {'group': 'Waiting on you', 'status': 'Needs attention'}, None)
    assert cs['status'] == 'waiting_on_user' and cs['waiting_on'] == 'user' and not cs['permission_pending']
    assert cs['ask']['kind'] == 'approval' and cs['ask']['requested_line'] == 'I approve the deploy'


# ---------------------------------------------------------------- waiting threads from the cache

NOW = datetime.datetime.now(datetime.timezone.utc)
def iso(min_ago): return (NOW - datetime.timedelta(minutes=min_ago)).isoformat()

ASK = 'The laptop sign-in is ready. I need a one-line approval from you in your own words:\n> I approve signing Sketchpad in on my laptop.'


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'CACHE', tmp_path)
    proj = {'project': 'Sketchpad', 'project_id': 'chan_S', 'primary_thread_id': 'cse_S', 'read_at': iso(2), 'threads_waiting': 2,
            'current_state': {'status': 'idle', 'waiting_on': None, 'summary': 'all good'},
            'messages': [{'index': 9, 'role': 'assistant', 'text': 'Both threads are running.'}],
            'threads': [
                {'title': 'Set up Sketchpad on laptop', 'thread_id': 'cmsg_SETUP', 'group': 'Waiting on you', 'status': 'Needs attention', 'age': 'now'},
                {'title': 'Build the web app', 'thread_id': 'cmsg_WEB', 'group': 'Waiting on you', 'status': 'Needs attention', 'age': '1d'},
                {'title': 'Android app', 'thread_id': 'cmsg_AND', 'group': 'Working', 'status': 'Working', 'age': 'now'},
                {'title': 'Old work', 'thread_id': 'cmsg_OLD', 'group': 'Idle', 'status': 'Idle', 'age': '6d'},
            ]}
    C.write_cache('chan_S', None, proj)
    C.write_cache('chan_S', 'cmsg_SETUP', {'thread_id': 'cmsg_SETUP', 'read_at': iso(1),
                                            'messages': [{'index': 4, 'role': 'user', 'text': 'go'}, {'index': 5, 'role': 'assistant', 'text': ASK}]})
    C.write_cache('chan_S', 'cmsg_AND', {'thread_id': 'cmsg_AND', 'read_at': iso(1),
                                          'messages': [{'index': 2, 'role': 'assistant', 'text': 'Should I proceed?'}],
                                          'permission_cards': [{'card_id': 'perm_1', 'type': 'computer_use', 'options': ['Allow', 'Deny']}]})
    other = {'project': 'Quiet', 'project_id': 'chan_Q', 'read_at': iso(3), 'current_state': {'status': 'idle', 'waiting_on': None},
             'threads': [{'title': 'x', 'thread_id': 'cmsg_X', 'group': 'Idle', 'status': 'Idle'}]}
    C.write_cache('chan_Q', None, other)
    rows = [{'name': 'Sketchpad', 'id': 'chan_S', 'primary_thread_id': 'cse_S'}, {'name': 'Quiet', 'id': 'chan_Q', 'primary_thread_id': 'cse_Q'}]
    monkeypatch.setattr(C, 'idb_snapshot', lambda max_age=0.0: {'channels': []})
    monkeypatch.setattr(C, 'project_rows', lambda snap, include_archived=False: rows)
    monkeypatch.setattr(C, 'resolve_project', lambda q, snap=None: next(r for r in rows if q in (r['id'], r['name'])))
    monkeypatch.setattr(C, 'idb_env_fields_for_thread', lambda tid: (None, None))
    return tmp_path


def test_waiting_threads_lists_every_waiting_child_with_its_latest_ask(cache):
    wt = C.waiting_threads_for('chan_S', None, 'cse_S')
    assert [w['thread_id'] for w in wt] == ['cmsg_SETUP', 'cmsg_WEB']      # Working / Idle threads are not listed
    setup, web = wt
    assert setup['ask_kind'] == 'approval' and setup['requested_line'] == 'I approve signing Sketchpad in on my laptop.'
    assert setup['latest_ask']['index'] == 5 and not setup['ask_stale'] and setup['address'].endswith('cmsg_SETUP')
    assert web['latest_ask'] is None and web['ask_stale'] and 'claude_thread_read' in web['next']


def test_waiting_thread_read_long_before_the_project_read_is_stale(cache):
    c = C.read_cache('chan_S', 'cmsg_SETUP'); c['read_at'] = iso(600); C.write_cache('chan_S', 'cmsg_SETUP', c)
    assert C.waiting_threads_for('chan_S')[0]['ask_stale'] is True


def test_thread_status_on_the_primary_lists_waiting_child_threads(cache):
    r = C.op_thread_status('Sketchpad')        # cached, no UI
    assert r['from_cache'] and r['current_state']['waiting_on'] is None
    assert [w['thread_id'] for w in r['waiting_threads']] == ['cmsg_SETUP', 'cmsg_WEB']
    assert r['waiting_threads'][0]['requested_line'] == 'I approve signing Sketchpad in on my laptop.'


def test_thread_status_on_the_child_carries_the_approval_ask(cache):
    r = C.op_thread_status('Sketchpad', 'cmsg_SETUP')
    assert r['ask']['kind'] == 'approval' and r['waiting_on'] == 'user'
    assert r['ask']['requested_line'] == 'I approve signing Sketchpad in on my laptop.'


def test_cached_project_get_lists_waiting_threads(cache):
    r = C.op_read('Sketchpad', None, False, with_threads=True)
    assert [w['thread_id'] for w in r['waiting_threads']] == ['cmsg_SETUP', 'cmsg_WEB']


def test_attention_cached_pass_surfaces_waiting_threads_approval_asks_and_cards(cache, monkeypatch):
    monkeypatch.setattr(C, 'op_read', lambda *a, **k: pytest.fail('cached attention must not touch the UI'))
    r = C.op_attention()
    assert [p['project'] for p in r['projects']] == ['Sketchpad'] and r['count_threads'] == 2
    assert r['approval_asks'] == [{'project': 'Sketchpad', 'thread_id': 'cmsg_SETUP', 'title': 'Set up Sketchpad on laptop',
                                   'requested_line': 'I approve signing Sketchpad in on my laptop.'}]
    assert [(c['thread_id'], c['card_id']) for c in r['permission_cards']] == [('cmsg_AND', 'perm_1')]
    assert r['live_reads'] is None and 'every listed thread' in r['note'].lower()


def test_attention_refresh_reads_only_stale_waiting_threads_and_respects_the_lock(cache, monkeypatch):
    calls = []
    def fake_read(project, thread, refresh, max_messages, force=False, stay=False, **k):
        calls.append((project, thread, stay))
        C.write_cache('chan_S', thread, {'thread_id': thread, 'read_at': iso(0),
                                         'messages': [{'index': 1, 'role': 'assistant', 'text': 'Should I proceed with the deploy?'}]})
    monkeypatch.setattr(C, 'op_read', fake_read)
    monkeypatch.setattr(C, 'ui_lock_holder', lambda: None)
    r = C.op_attention(refresh=True, max_reads=5)
    assert calls == [('chan_S', 'cmsg_WEB', False)]                    # only the stale one; last read goes home
    web = next(w for w in r['projects'][0]['waiting_threads'] if w['thread_id'] == 'cmsg_WEB')
    assert web['ask_kind'] == 'question' and not web['ask_stale']
    # another caller holds the UI lock: skip, never queue behind it
    calls.clear(); c = C.read_cache('chan_S', 'cmsg_WEB'); c['read_at'] = iso(600); C.write_cache('chan_S', 'cmsg_WEB', c)
    monkeypatch.setattr(C, 'ui_lock_holder', lambda: {'pid': 1, 'op': 'send Sketchpad primary'})
    r = C.op_attention(refresh=True)
    assert calls == [] and 'UI busy' in r['live_reads'][0]['skipped']


def test_attention_cli_and_mcp_tool_exist():
    import argparse
    src = (pathlib.Path(C.__file__)).read_text()
    assert "sp.add_parser('attention'" in src and "a.cmd == 'attention'" in src
