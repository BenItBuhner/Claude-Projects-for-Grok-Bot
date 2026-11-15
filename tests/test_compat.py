"""Backward compatibility with consumers written for the pre-1.0 releases:
legacy status words, old on-disk caches, and the origin bracket in summaries."""
import pathlib, sys
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
import cpg_core as C
import cpg_origin as O


# ---------------------------------------------------------------- origin bracket in summaries
def test_origin_body_strips_header_line_and_collapsed_header():
    assert O.origin_body('[sent by Grok Bot]\nrun the tests') == 'run the tests'
    assert O.origin_body('[sent by Grok Bot · Scout]\nrun the tests') == 'run the tests'
    assert O.origin_body('[sent by Grok Bot] run the tests') == 'run the tests'
    assert O.origin_body('please run [sent by Grok Bot] tests') == 'please run [sent by Grok Bot] tests'
    assert O.origin_body('') == '' and O.origin_body(None) == ''


def test_bot_sent_summary_is_the_request_not_the_bracket():
    msgs = [{'index': 1, 'role': 'assistant', 'text': 'Ready.', 'summary': 'Ready.', 'time_utc': '2026-11-14T15:00:00Z'},
            {'index': 2, 'role': 'user', 'text': '[sent by Grok Bot]\nNew standing order: ship it.\nDetails...',
             'summary': '[sent by Grok Bot]', 'time_utc': '2026-11-14T15:01:00Z'}]
    C.annotate_messages_staleness(msgs)
    assert msgs[1]['sent_by_bot'] is True
    assert msgs[1]['summary'] == 'New standing order: ship it.'
    assert msgs[1]['text'].startswith('[sent by Grok Bot]')          # text stays verbatim
    cs = C.build_current_state(msgs)
    assert cs['summary'] == 'New standing order: ship it.' and cs['latest_sent_by_bot'] is True
    assert cs['waiting_on'] == 'claude'                               # a bot asked; Claude moves next


def test_collapsed_header_summary_and_missing_summary():
    msgs = [{'index': 2, 'role': 'user', 'text': '[sent by Grok Bot · Scout] go', 'summary': '[sent by Grok Bot · Scout] go'},
            {'index': 3, 'role': 'user', 'text': '[sent by Grok Bot]\nsecond'}]
    C.annotate_messages_staleness(msgs)
    assert msgs[0]['summary'] == 'go'
    assert msgs[1]['summary'] == 'second'


def test_human_messages_and_replies_are_untouched():
    msgs = [{'index': 1, 'role': 'user', 'text': 'Revamp needed.', 'summary': 'Revamp needed.'},
            {'index': 2, 'role': 'assistant', 'text': '[sent by Grok Bot] quoted by Claude', 'summary': '[sent by Grok Bot] quoted by Claude'}]
    C.annotate_messages_staleness(msgs)
    assert msgs[0]['sent_by_bot'] is False and msgs[0]['summary'] == 'Revamp needed.'
    assert 'sent_by_bot' not in msgs[1] and msgs[1]['summary'] == '[sent by Grok Bot] quoted by Claude'


# ---------------------------------------------------------------- legacy status vocabulary
@pytest.fixture
def alex(monkeypatch):
    monkeypatch.setitem(C.CFG, 'operator_name', 'Alex')


def test_legacy_status_fields_added_next_to_new_ones(alex):
    cs = {'status': 'waiting_on_user', 'waiting_on': 'user', 'time_local': 't'}
    r = C.legacy_aliases(cs)
    assert r['status'] == 'waiting_on_user' and r['waiting_on'] == 'user'          # new fields unchanged
    assert r['status_legacy'] == 'waiting_on_alex' and r['waiting_on_legacy'] == 'alex' and r['time_ct'] == 't'


@pytest.mark.parametrize('status,waiting_on,legacy_status,legacy_waiting', [
    ('working', 'claude', 'working', 'claude'),
    ('idle', None, 'idle', None),
    ('idle', 'background_watcher', 'idle', 'background_watcher'),
    ('waiting_on_user', 'permission', 'waiting_on_alex', 'alex'),
    ('done', None, 'done', None),
])
def test_legacy_status_mapping(alex, status, waiting_on, legacy_status, legacy_waiting):
    r = C.legacy_aliases({'status': status, 'waiting_on': waiting_on})
    assert r['status_legacy'] == legacy_status and r['waiting_on_legacy'] == legacy_waiting


def test_legacy_on_triage_shape(alex):
    triage = {'current_state': {'status': 'waiting_on_user', 'waiting_on': 'user'}, 'waiting_on': 'user',
              'latest_message': {'time_local': 'x'}, 'threads': [{'title': 'a', 'status': None, 'current_state': {'status': 'idle', 'waiting_on': None}}]}
    r = C.legacy_aliases(triage)
    assert r['waiting_on_legacy'] == 'alex' and 'status_legacy' not in r
    assert r['current_state']['status_legacy'] == 'waiting_on_alex'
    assert r['latest_message']['time_ct'] == 'x'
    assert 'status_legacy' not in r['threads'][0] and r['threads'][0]['current_state']['status_legacy'] == 'idle'


def test_legacy_word_follows_operator_name(monkeypatch):
    monkeypatch.setitem(C.CFG, 'operator_name', 'the user')
    assert C.legacy_aliases({'status': 'waiting_on_user', 'waiting_on': 'user'})['status_legacy'] == 'waiting_on_user'
    monkeypatch.setitem(C.CFG, 'operator_name', 'Alex Kim')
    assert C.legacy_aliases({'status': 'waiting_on_user', 'waiting_on': 'user'})['status_legacy'] == 'waiting_on_alex'
    monkeypatch.setitem(C.CFG, 'operator_name', '')
    assert C.legacy_aliases({'status': 'waiting_on_user', 'waiting_on': 'user'})['status_legacy'] == 'waiting_on_user'


# ---------------------------------------------------------------- old caches
def test_normalize_state_maps_old_cache_words():
    cs = {'status': 'waiting_on_alex', 'waiting_on': 'alex', 'sidebar_suggested_status': 'waiting_on_alex'}
    C.normalize_state(cs)
    assert cs == {'status': 'waiting_on_user', 'waiting_on': 'user', 'sidebar_suggested_status': 'waiting_on_user'}
    for st in C._STATUSES:
        assert C.normalize_state({'status': st})['status'] == st
    for w in C._WAITING_ON:
        assert C.normalize_state({'waiting_on': w})['waiting_on'] == w


def test_cached_read_normalizes_old_thread_rows(monkeypatch):
    old_cache = {'project': 'Docs', 'project_id': 'chan_X', 'thread_id': 'primary', 'read_at': '2026-11-14T15:00:00+00:00',
                 'claude_working': False,
                 'messages': [{'index': 1, 'role': 'assistant', 'text': 'done', 'time_utc': '2026-11-14T15:00:00Z', 'time_ct': 'Nov 14 09:00 AM CST'}],
                 'threads': [{'title': 'Old child', 'thread_id': 'cmsg_1', 'group': 'Waiting on you', 'status': None,
                              'current_state': {'status': 'waiting_on_alex', 'waiting_on': 'alex', 'summary': None}}]}
    monkeypatch.setattr(C, 'resolve_project', lambda p: {'id': 'chan_X', 'name': 'Docs', 'primary_thread_id': 'cse_X'})
    monkeypatch.setattr(C, 'resolve_thread', lambda chan, thread, *a: None)
    monkeypatch.setattr(C, 'read_cache', lambda chan, tid=None: old_cache)
    monkeypatch.setattr(C, 'idb_env_fields_for_thread', lambda tid: (None, None))
    r = C.op_read('Docs', refresh=False)
    row = r['threads'][0]['current_state']
    assert row['status'] == 'waiting_on_user' and row['waiting_on'] == 'user'
    assert r['current_state']['status'] in C._STATUSES
