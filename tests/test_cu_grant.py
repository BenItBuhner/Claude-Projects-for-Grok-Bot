"""A cleared computer-use card is not a working grant (Nov 16: the card went, then Claude said the request was declined).
The permission job reads the thread after an Allow and says success only when computer use shows. No app, no network."""
import json
from claude_projects import core as C


def msg(i, text, role='assistant'):
    return {'index': i, 'role': role, 'text': text}


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


def check(replies, wait_s=90):
    k = Clock(); it = iter(replies)
    def read():
        r = next(it, replies[-1])
        if isinstance(r, Exception): raise r
        return {'messages': r}
    return C.verify_cu_grant('chan_X', 'cmsg_X', 10, None, wait_s=wait_s, read=read, sleep=k.sleep, clock=k)


def test_declined_text_after_the_press_is_not_success():
    g = check([[msg(10, 'Asking to use Sketchpad'), msg(11, 'The screen-control request was declined, or the screen is locked.')]])
    out = C.cu_grant_outcome({'pressed': 'Allow for this session', 'card_gone': True}, g)
    assert g['grant'] == 'declined' and out['outcome'] == 'pressed_unverified' and 'declined' in out['reason']


def test_computer_use_starting_is_success():
    g = check([[msg(10, 'old')], [msg(11, 'I took a screenshot; the Sketchpad window is open.')]])
    assert g['grant'] == 'started' and C.cu_grant_outcome({}, g)['outcome'] == 'success'


def test_quiet_thread_for_the_whole_window_is_success():
    g = check([[msg(11, 'Working on the overlay now.')]])
    assert g['grant'] == 'no_decline_seen' and g['checked_s'] >= 90 and C.cu_grant_outcome({}, g)['outcome'] == 'success'


def test_unreadable_thread_is_unverified():
    g = check([C.Busy('the app is busy')])
    out = C.cu_grant_outcome({}, g)
    assert g['grant'] == 'unverified' and out['outcome'] == 'pressed_unverified'


def test_declined_text_from_before_the_press_does_not_count():
    assert C.cu_grant_verdict([msg(9, 'Last time the request was declined.'), msg(10, 'Asking again')], 10)[0] is None


def test_permission_job_reports_a_declined_grant(monkeypatch, tmp_path):
    monkeypatch.setattr(C, 'PERM_JOBS', tmp_path); monkeypatch.setattr(C, 'log', lambda m: None)
    monkeypatch.setattr(C, 'resolve_project', lambda p, snap=None: {'id': 'chan_X', 'name': 'Sketchpad', 'primary_thread_id': None})
    monkeypatch.setattr(C, 'resolve_thread', lambda chan, t, ths=None: t)
    monkeypatch.setattr(C, 'read_cache', lambda chan, thread=None: {'latest_message_index': 10})
    monkeypatch.setattr(C, 'op_permission', lambda *a, **k: {'project': 'Sketchpad', 'project_id': 'chan_X', 'thread_id': 'cmsg_X',
                        'pressed': 'Allow for this session', 'card_gone': True, 'card': {'type': 'computer_use'}})
    monkeypatch.setattr(C, 'op_read', lambda *a, **k: k['fresh_ok'] is False and {'messages': [msg(11, 'Your screen appears to be locked, so I could not start computer use.')]})
    monkeypatch.setattr(C.time, 'sleep', lambda s: None)
    key = 'p' + '0' * 23
    C.job_write(C.PERM_JOBS, key, {'job_id': key, 'state': 'queued', 'spec': {'project': 'chan_X', 'thread': 'cmsg_X', 'option': 'Allow for this session', 'dry_run': False}})
    r = C.run_permission_job(key)
    assert r['outcome'] == 'pressed_unverified' and r['grant_check']['grant'] == 'declined'
    assert json.loads((tmp_path / f'{key}.diag.json').read_text())['timeline'][-1]['step'] == 'grant_check'
