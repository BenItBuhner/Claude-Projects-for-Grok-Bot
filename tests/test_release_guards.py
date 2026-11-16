"""Guards for misses seen in real traffic (Nov 16): a long post the app shows collapsed was reported not_posted, and a
send job id passed as the message was posted into a Project. No app, no network."""
import asyncio
import pytest
from claude_projects import core as C
from claude_projects.origin import add_origin

LONG = add_origin('Correction from the owner on Android 0.6.43, his words (7:24 AM CT):\n' + 'details ' * 300)


def collapsed(index, text='[sent by Grok Bot]'):
    """What parse_article returns for a long user post: only its first line shows, then "Show more"."""
    return {'index': index, 'role': 'user', 'text': text, 'truncated_in_app': True}


def test_collapsed_long_post_is_the_post():
    assert C.post_matches(collapsed(150), LONG, before_max=149)
    assert C.post_matches(collapsed(149, '626e8587.gz GZ [sent by Grok Bot] Correction from the owner'), LONG, before_max=148)


def test_collapsed_post_needs_a_new_index_and_the_same_start():
    assert not C.post_matches(collapsed(149), LONG, before_max=149)                       # not newer than the send
    assert not C.post_matches(collapsed(150), LONG, before_max=None, since_utc='2026-11-16T13:25:00Z')
    assert not C.post_matches(collapsed(150, '[sent by Grok Bot] Something else'), LONG, before_max=149)
    assert not C.post_matches(dict(collapsed(150), truncated_in_app=False), LONG, before_max=149)
    assert not C.post_matches(dict(collapsed(150), role='assistant'), LONG, before_max=149)


def test_verify_finds_a_collapsed_post(monkeypatch):
    monkeypatch.setattr(C, 'read_messages', lambda *a, **k: [collapsed(150)])
    m = C._verify_sent(None, LONG, 149, secs=1)
    assert m and m['index'] == 150


@pytest.fixture
def jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(C, 'SEND_JOBS', tmp_path / 'send-jobs')
    monkeypatch.setattr(C.STAGE, 'maybe_sweep', lambda: None)
    monkeypatch.setattr(C, 'resolve_project', lambda q, snap=None: {'id': 'chan_E', 'name': 'Sketchpad'})
    monkeypatch.setattr(C, 'spawn_job', lambda *a, **k: pytest.fail('a job id must never start a send'))
    monkeypatch.setattr(C, 'op_send', lambda *a, **k: pytest.fail('a job id must never be typed'))
    C.job_write(C.SEND_JOBS, 'sd89215bf08414a7dbb5a246', {'job_id': 'sd89215bf08414a7dbb5a246', 'state': 'not_posted', 'error': 'x'})


def test_job_id_as_message_returns_that_job(jobs):
    r = C.op_send_job('Sketchpad', 'sd89215bf08414a7dbb5a246')
    assert r['job_id'] == 'sd89215bf08414a7dbb5a246' and r['state'] == 'not_posted' and not r.get('sent')
    r = C.op_send_job('Sketchpad', '[sent by Grok Bot]\nsd89215bf08414a7dbb5a246')
    assert r['job_id'] == 'sd89215bf08414a7dbb5a246'


def test_unknown_job_id_as_message_is_refused(jobs):
    with pytest.raises(C.ClaudeProjectsError, match='Nothing was sent'):
        C.op_send_job('Sketchpad', 'sc2f7ed0a58c0b068029b29c')


def test_op_send_refuses_a_job_id_before_opening_anything(monkeypatch):
    monkeypatch.setattr(C.STAGE, 'maybe_sweep', lambda: None)
    monkeypatch.setattr(C, 'resolve_project', lambda *a, **k: pytest.fail('refused before any lookup'))
    with pytest.raises(C.ClaudeProjectsError, match='Nothing was sent'):
        C.op_send('Sketchpad', 'sc2f7ed0a58c0b068029b29c', dry_run=True)


def test_mcp_send_with_a_job_id_message_asks_for_the_job(monkeypatch):
    M = pytest.importorskip('claude_projects.mcp_server')
    seen = {}
    async def fake_run(args, timeout=900, msg=None, attach=None, busy_budget=None):
        seen.update(args=args, msg=msg); return {'ok': True}
    monkeypatch.setattr(M, 'run', fake_run)
    asyncio.run(M.claude_thread_send('Sketchpad', 'sd89215bf08414a7dbb5a246'))
    assert seen['args'][-2:] == ['--job-id', 'sd89215bf08414a7dbb5a246'] and seen['msg'] is None
