"""Origin bracket: constant prefix, optional bot name, idempotent, applied to send and thread-create."""
import json, pathlib, re, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
import cpg_origin as O
import cpg_core as C

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_constant_prefix():
    assert O.ORIGIN_PREFIX == '[sent by Grok Bot'
    assert O.origin_header() == '[sent by Grok Bot]'
    assert O.origin_header('Scout') == '[sent by Grok Bot · Scout]'


def test_add_origin_is_first_line():
    out = O.add_origin('Fix the failing test in parser.py')
    assert out.split('\n')[0] == '[sent by Grok Bot]'
    assert out.split('\n', 1)[1] == 'Fix the failing test in parser.py'
    assert O.add_origin('hi', 'Scout').startswith('[sent by Grok Bot · Scout]\nhi')


def test_idempotent():
    once = O.add_origin('do the thing', 'Scout')
    assert O.add_origin(once, 'Scout') == once
    assert O.add_origin(once) == once                # an existing named header is kept, not doubled
    assert O.add_origin(O.add_origin('x')) == '[sent by Grok Bot]\nx'
    assert O.add_origin('[sent by Grok Bot]') == '[sent by Grok Bot]'
    assert once.count(O.ORIGIN_PREFIX) == 1


def test_not_fooled_by_mentions():
    # mentioning the bracket later in the text, or inline on the first line, is not a header
    m = 'Please explain what [sent by Grok Bot] means'
    assert O.add_origin(m).split('\n')[0] == '[sent by Grok Bot]'
    m2 = 'line one\n[sent by Grok Bot]'
    assert O.add_origin(m2).startswith('[sent by Grok Bot]\nline one')


def test_bot_name_sanitized():
    assert O.clean_bot_name(' Scout\n[x] ') == 'Scout x'
    assert O.origin_header('a' * 100) == '[sent by Grok Bot · ' + 'a' * 40 + ']'
    assert O.origin_header('   ') == '[sent by Grok Bot]'
    assert O.has_origin(O.origin_header('Scout ] [ evil'))


def test_strip_and_detect():
    s = O.add_origin('body\nmore', 'Scout')
    assert O.strip_origin(s) == 'body\nmore'
    assert O.strip_origin('no header') == 'no header'
    assert O.is_bot_sent(s)
    assert O.is_bot_sent('[sent by Grok Bot] body collapsed onto one line')
    assert not O.is_bot_sent('typed by a person')


def test_create_thread_message_has_bracket_once():
    m = C.create_thread_message('Docs pass', 'Update the README', 'Scout')
    lines = m.split('\n')
    assert lines[0] == '[sent by Grok Bot · Scout]'
    assert lines[1] == 'Start a new thread titled "Docs pass" for this:'
    assert m.count('[sent by Grok Bot · Scout]') == 1
    assert C.CREATE_THREAD_NOTE in m


def test_annotate_marks_bot_messages():
    msgs = [
        {'index': 1, 'role': 'user', 'text': 'typed by a person', 'time_utc': '2026-01-01T00:00:00Z'},
        {'index': 2, 'role': 'user', 'text': '[sent by Grok Bot · Scout]\nrun the tests', 'time_utc': '2026-01-01T00:01:00Z'},
        {'index': 3, 'role': 'assistant', 'text': 'ok', 'time_utc': '2026-01-01T00:02:00Z'},
    ]
    C.annotate_messages_staleness(msgs)
    assert msgs[0]['sent_by_bot'] is False
    assert msgs[1]['sent_by_bot'] is True
    assert 'sent_by_bot' not in msgs[2]


def test_op_send_applies_bracket_before_anything(monkeypatch):
    """op_send must prefix the message before resolving/typing; capture what it would type, touching no app."""
    seen = {}

    class Stop(Exception): pass

    def fake_resolve(project, snap=None):
        seen['message_at_resolve'] = True
        raise Stop()
    orig_add = C.add_origin
    def spy(message, bot_name=None):
        out = orig_add(message, bot_name); seen['typed'] = out; return out
    monkeypatch.setattr(C, 'add_origin', spy)
    monkeypatch.setattr(C, 'resolve_project', fake_resolve)
    try: C.op_send('Demo Project', 'hello', bot_name='Scout', dry_run=True)
    except Stop: pass
    assert seen['typed'] == '[sent by Grok Bot · Scout]\nhello'


def test_cli_origin_preview_no_app():
    r = subprocess.run([sys.executable, str(REPO / 'cpg_core.py'), '--json', 'origin', '--message', 'hello', '--bot-name', 'Scout'],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d['origin_header'] == '[sent by Grok Bot · Scout]'
    assert d['text'] == '[sent by Grok Bot · Scout]\nhello'


def test_single_constant_in_code():
    """The literal prefix is defined once in code; everything else imports it (docs and tests may quote it)."""
    hits = []
    for f in REPO.glob('*.py'):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if "'[sent by Grok Bot'" in line or '"[sent by Grok Bot"' in line: hits.append((f.name, n))
    assert hits == [('cpg_origin.py', hits[0][1])], hits
