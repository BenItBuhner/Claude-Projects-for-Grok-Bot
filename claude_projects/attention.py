"""claude_projects.attention - what needs the user, across every thread of every Project (no UI; pure functions).

A Project's primary thread is not the only place Claude asks for something: child threads land in the sidebar's
'Waiting on you' group too. waiting_threads() lists every such thread from a cached project read, each with its latest
ask (from that thread's cached read) and classify_ask()'s reading of it. classify_ask() tells an approval ask (Claude
wants a line in the user's own words, an approval line, or an exact reply) from a permission stall, a question, or an
action item, and returns the exact line Claude asked for. Nothing here posts anything or invents the user's words."""
import re

WAITING_WORDS = ('waiting on you', 'needs attention')

# Claude asks for the user's own words / an approval line / an exact reply
_VERB = r"(?:reply|respond|answer|type|post|send|paste|say|write)"
APPROVAL_RE = re.compile(
    r"(in your own words|your own words|in your words|one[- ]line approval|approval (line|message|sentence|phrase)|"
    r"(a|one) line (from you|saying|that says|approving)|"
    r"(?:^|[.!?:;]\s+|\b(?:please|just|simply|then|you can|can you|could you|you could|would you)\s+)" + _VERB +
    r"(\s+(back|me|to me|here|in this thread))*\s+(with\s+)?(exactly|verbatim|this line|this exact|this\s*:|the following( line| words| sentence)?|"
    r"the (line|sentence|phrase|words) below)|"
    r"(these exact|the exact|exactly these) (words|wording|phrase|sentence)|"
    r"(written|explicit|recorded|typed) (approval|consent|confirmation|authori[sz]ation) (from|by) you|"
    r"approve (it|this|that) (by|with|in) (writing|replying|typing|saying)|"
    r"confirm (it |this |that )?(by|with) (writing|replying|typing|saying)|"
    r"(need|want) you to (say|write|type|reply with|confirm in writing))", re.I | re.M)
PERMISSION_RE = re.compile(
    r"(need (your )?(permission|explicit ok|go-ahead)|waiting (for|on) (your )?(permission|ok)|"
    r"permission (prompt|request|card)|approve (this|the) (action|request|command)|(allow|deny) (the |this )?(prompt|request|card)|"
    r"(blocked|stuck) (on|waiting for) (a )?(permission|approval))", re.I)
QUESTION_RE = re.compile(
    r"(can you confirm|please confirm|let me know|your call|what should i|should i (go ahead|proceed)|do you want|"
    r"want me to|please reply|awaiting your|over to you|which (one|option) (do|would) you|(can|could|would) you\b)", re.I)
ACTION_RE = re.compile(r"(what you need to do|you need to|you('ll| will) need to|is yours to|are yours to|needs? you to|"
                       r"on your side|waiting on you|for you to)", re.I)

_QUOTES = (('\u201c', '\u201d'), ('"', '"'), ('\u2018', '\u2019'), ('`', '`'), ("'", "'"))
_BULLET = re.compile(r'^\s*(?:[>\u2022*\-]|\d+[.)])\s*')


def _quoted(s):
    """The first quoted span in s (curly or straight double quotes, backticks, single quotes), or None."""
    for a, b in _QUOTES:
        m = re.search(re.escape(a) + r'([^' + re.escape(b) + r'\n]{3,400})' + re.escape(b), s)
        if m and (a != "'" or ' ' in m.group(1)): return m.group(1).strip()
    return None


def _clean(line):
    return _BULLET.sub('', line).strip().rstrip()


def requested_line(text, blocks=None):
    """The exact line Claude asked the user to send, exactly as written in its message (None when it gave none).
    Looks, in order: a code block or quote right after the ask, a quoted span on the ask's line, the first line
    after an ask that ends with ':' (a bullet or '>' prefix is dropped, nothing else is changed)."""
    lines = (text or '').replace('\r\n', '\n').split('\n')
    hit = next((i for i, ln in enumerate(lines) if APPROVAL_RE.search(ln)), None)
    if hit is None: return None
    for b in blocks or []:                       # a code block or quote is the clearest "send exactly this"
        if b.get('type') in ('code', 'quote') and (b.get('text') or '').strip():
            t = b['text'].strip()
            pos = (text or '').find(t[:60])
            if pos < 0 or pos >= len('\n'.join(lines[:hit])):  # after (or on) the ask line
                return t
    q = _quoted(lines[hit])
    if q: return q
    tail = lines[hit].split(':', 1)
    if len(tail) == 2 and tail[1].strip() and _quoted(tail[1]) is None and len(tail[1].strip()) > 3:
        cand = _clean(tail[1])
        if cand and not APPROVAL_RE.search(cand): return cand
    for ln in lines[hit + 1:hit + 6]:
        if not ln.strip(): continue
        q = _quoted(ln)
        if q: return q
        if lines[hit].rstrip().endswith(':') or ln.lstrip().startswith('>'): return _clean(ln)
        break
    return None


def classify_ask(text, blocks=None):
    """kind: approval | permission | question | action | update (None for empty text).
    approval = Claude asked for a line in the user's own words / an approval line / an exact reply; requested_line is
    that line exactly as Claude wrote it (or None when it described the line without giving it)."""
    t = text or ''
    if not t.strip(): return None
    m = APPROVAL_RE.search(t)
    if m:
        line = requested_line(t, blocks)
        return {'kind': 'approval', 'requested_line': line, 'trigger': m.group(0),
                'note': ('Claude asked for a line in the user\'s own words. Relay requested_line to the user exactly as written; '
                         'post it on their behalf only under an explicit standing approval that covers this exact action, '
                         'otherwise ask them. Never invent or reword the user\'s words.' if line else
                         'Claude asked for an approval in the user\'s own words but gave no exact line: ask the user for it; '
                         'never write it for them.')}
    m = PERMISSION_RE.search(t)
    if m: return {'kind': 'permission', 'requested_line': None, 'trigger': m.group(0),
                  'note': 'A permission stall: check permission_cards (claude_thread_permissions) and the user policy.'}
    m = QUESTION_RE.search(t) or (re.search(r'\?\s*$', t.strip()) and re.search(r'[^?]*\?\s*$', t.strip()))
    if m: return {'kind': 'question', 'requested_line': None, 'trigger': m.group(0)[-200:], 'note': 'Claude asked a question: relay it to the user.'}
    m = ACTION_RE.search(t)
    if m: return {'kind': 'action', 'requested_line': None, 'trigger': m.group(0), 'note': 'Claude listed something for the user to do.'}
    return {'kind': 'update', 'requested_line': None, 'trigger': None, 'note': None}


def is_waiting(row):
    s = f"{row.get('group') or ''} {row.get('status') or ''}".lower()
    return any(w in s for w in WAITING_WORDS)


def latest_ask(thread_cache):
    """The latest assistant message of a cached thread read, trimmed for a status listing, with its classification."""
    msgs = (thread_cache or {}).get('messages') or []
    m = next((x for x in reversed(msgs) if x.get('role') == 'assistant'), None)
    if not m: return None
    text = m.get('text') or ''
    return {'index': m.get('index'), 'text': text[-2000:], 'truncated': len(text) > 2000, 'time_local': m.get('time_local'),
            'age_hours': m.get('age_hours'), 'is_latest': m is msgs[-1], 'ask': classify_ask(text, m.get('blocks'))}


def _minutes_between(a, b):
    import datetime
    try:
        pa = datetime.datetime.fromisoformat(str(a).replace('Z', '+00:00')); pb = datetime.datetime.fromisoformat(str(b).replace('Z', '+00:00'))
        return (pb - pa).total_seconds() / 60.0
    except Exception: return None


def waiting_threads(project_cache, read_thread_cache, address=None, ct=None):
    """Every thread of a Project in the sidebar's 'Waiting on you' group (child threads included), from cached reads only.
    read_thread_cache(thread_id) returns that thread's cached read (or None). ask_stale is true when the thread's cached
    read is older than the project read that saw it waiting, so its latest ask may predate what Claude is waiting on now."""
    pc = project_cache or {}; out = []
    for row in pc.get('threads') or pc.get('threads_cached') or []:
        if not is_waiting(row) or not row.get('thread_id'): continue
        tc = read_thread_cache(row['thread_id'])
        la = latest_ask(tc)
        lag = _minutes_between((tc or {}).get('read_at'), pc.get('read_at')) if tc else None
        stale = tc is None or (lag is not None and lag > 1)
        out.append({'thread_id': row['thread_id'], 'title': row.get('title'), 'group': row.get('group'), 'status': row.get('status'),
                    'age': row.get('age'), 'address': address(row['thread_id']) if address else None,
                    'latest_ask': la, 'ask_kind': (la or {}).get('ask', {}).get('kind') if la else None,
                    'requested_line': ((la or {}).get('ask') or {}).get('requested_line') if la else None,
                    'thread_read_at_local': ct((tc or {}).get('read_at')) if (ct and tc) else (tc or {}).get('read_at'),
                    'ask_stale': stale,
                    'next': ('read this thread (claude_thread_read thread=<thread_id>) for its current ask' if stale else
                             'relay the ask to the user' + ('; for an approval ask relay requested_line exactly' if la and la['ask'] and la['ask']['kind'] == 'approval' else ''))})
    return out
