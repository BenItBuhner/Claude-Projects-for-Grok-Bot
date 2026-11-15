"""Forwarding: header + verbatim content, origin bracket once, dry run sends nothing, attachments staged."""
import pathlib, sys
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import core as C
from claude_projects import staging as S

SRC = {'project': 'Docs', 'project_id': 'chan_SRC', 'primary_thread_id': 'cse_SRC', 'thread_id': 'cmsg_CHILD', 'thread_title': 'Fix links',
       'messages': [
           {'index': 4, 'role': 'user', 'text': 'please fix', 'time_local': 'Nov 14 09:00 AM EST'},
           {'index': 5, 'role': 'assistant', 'text': 'Fixed 12 links.\n\n- a\n- b', 'time_local': 'Nov 14 09:10 AM EST',
            'attachments': [{'id': '5.1', 'kind': 'image', 'name': 'report.png'}, {'id': '5.2', 'kind': 'pull_request', 'name': 'PR #7', 'url': 'https://example.com/pr/7'}]},
       ]}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(C, 'STAGE', S.Staging(tmp_path/'stage'))
    monkeypatch.setattr(C, 'op_read', lambda *a, **k: dict(SRC))
    def fake_send(project, message, thread=None, *a, dry_run=False, attachments=None, bot_name=None, **k):
        full = C.add_origin(message, bot_name)
        sent.append({'project': project, 'thread': thread, 'message': full, 'dry_run': dry_run, 'attachments': attachments})
        base = {'project': project, 'project_id': 'chan_DST', 'thread_id': thread or 'primary', 'origin_header': full.split('\n', 1)[0], 'would_send_chars': len(full)}
        return dict(base, dry_run=True) if dry_run else dict(base, sent=True, verified_in_transcript=True)
    monkeypatch.setattr(C, 'op_send', fake_send)
    img = tmp_path/'report.png'; img.write_bytes(b'png')
    monkeypatch.setattr(C, 'op_attachment_get', lambda *a, **k: {'path_on_host': str(img), 'saved_name': 'report.png'})
    return sent


def test_forward_dry_run_default(wired):
    r = C.op_forward('Docs', 'Release', source_thread='cmsg_CHILD')
    assert r['dry_run'] and r['source']['address'] == 'chan_SRC/cmsg_CHILD' and r['source']['message_index'] == 5
    msg = wired[-1]['message']
    lines = msg.split('\n')
    assert lines[0] == '[sent by Grok Bot]' and lines[1].startswith('Forwarded from Project "Docs" \u203a thread "Fix links" (chan_SRC/cmsg_CHILD), message #5 by Claude')
    assert 'Fixed 12 links.\n\n- a\n- b' in msg and 'https://example.com/pr/7' in msg
    assert wired[-1]['dry_run'] is True and r['would_attach'] is None


def test_forward_specific_message_with_note(wired):
    r = C.op_forward('Docs', 'Release', message_index=4, note='FYI from the docs side', bot_name='Scout')
    msg = wired[-1]['message']
    assert msg.startswith('[sent by Grok Bot \u00b7 Scout]\nFYI from the docs side\nForwarded from') and 'message #4 by' in msg
    assert msg.count('[sent by Grok Bot') == 1


def test_forward_real_with_attachments(wired):
    r = C.op_forward('Docs', 'Release', target_thread='cmsg_DST', include_attachments=True, dry_run=False)
    assert r['forwarded'] and r['sent'] and len(r['staged_files']) == 1
    st = pathlib.Path(r['staged_files'][0]['path'])
    assert st.name == 'report.png' and C.STAGE.contains(st) and wired[-1]['attachments'] == [{'path': str(st)}]
    assert wired[-1]['thread'] == 'cmsg_DST'


def test_forward_missing_message(wired):
    with pytest.raises(C.NotFound): C.op_forward('Docs', 'Release', message_index=99)


def test_forward_truncates_long_content():
    text, truncated = C.forward_text('H', 'x' * 30000, limit=1000)
    assert truncated and len(text) <= 1000 and text.endswith('[... truncated by the forwarder ...]')


def test_address():
    assert C.thread_address('chan_A', None, 'cse_B') == 'chan_A/cse_B'
    assert C.thread_address('chan_A', 'primary', 'cse_B') == 'chan_A/cse_B'
    assert C.thread_address('chan_A', 'cmsg_C', 'cse_B') == 'chan_A/cmsg_C'


def test_legacy_aliases():
    r = C.legacy_aliases({'time_local': 't', 'a': [{'path_on_host': '/x', 'read_at_local': 'r'}]})
    assert r['time_ct'] == 't' and r['a'][0]['path_on_home_lab'] == '/x' and r['a'][0]['read_at_ct'] == 'r'
