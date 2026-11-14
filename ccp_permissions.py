"""Generic detection of the Claude app's permission cards (pure logic, no UI).

Claude Code Projects always run in 'Auto' permission mode, but threads still stop on dynamic cards such as

    Claude would like to add this folder to the session: /path/to/dir      [Add folder] [Not now]
    Claude wants to add a repository: owner/name                            [Add repository] [Not now]
    Allow Claude to use this machine? <machine>                             [Allow] [Deny]
    Allow Claude to run `npm test`?                                         [Allow once] [Always allow] [Deny]

The app renders these as a block of text plus push buttons. ccp_core snapshots the open transcript's accessibility tree
into plain dicts ({id, parent, role, name, text}) and this module finds the cards in that snapshot, so the same code
is exercised by mock tests and by the live read.

A card = the nearest ancestor of one or more option buttons whose text reads like a permission prompt.
Each card gets: card_id (stable hash of type + target + prompt), type, target, prompt, options (button labels, in order).
"""
import re, hashlib

OPTION_RE = re.compile(r"^(add folder|add repository|add repo|add directory|not now|allow|allow once|allow always|always allow|"
                       r"allow for this (session|project|chat)|allow in this session|don'?t allow|do not allow|deny|reject|decline|"
                       r"approve|yes|no|yes, allow|no, deny|trust|trust folder|trust this folder|grant access|connect|skip|"
                       r"continue|cancel|dismiss|use this machine|allow machine|allow access|block)$", re.I)
PROMPT_RE = re.compile(r"(would like to|wants to|needs (your )?(permission|approval)|requires? (your )?(permission|approval)|"
                       r"\ballow claude\b|do you want to (allow|add|trust|run)|permission to|grant .*access|trust this|"
                       r"add this (folder|directory|repo)|approval controls|\bapprove\b.*\?|allow .*\?)", re.I)
# a prompt needs at least one affirmative-looking option to count as a permission card
AFFIRM_RE = re.compile(r'^(add|allow|always|approve|yes|trust|grant|connect|use this|continue)', re.I)
NEGATE_RE = re.compile(r"^(not now|don'?t|do not|deny|reject|decline|no\b|skip|cancel|dismiss|block)", re.I)

TYPES = [
    ('add_folder', re.compile(r'(add|access|use) (this|a|the) (folder|directory)|folder to the session|add folder', re.I)),
    ('add_repository', re.compile(r'(add|attach|connect) (this |a |the )?(github )?(repository|repo)\b|add repository', re.I)),
    ('allow_machine', re.compile(r'(machine|device|computer|environment|remote host)\b', re.I)),
    ('run_command', re.compile(r'\b(run|execute|bash|command|shell)\b', re.I)),
    ('network_access', re.compile(r'\b(network|internet|fetch|domain|url|web)\b', re.I)),
    ('edit_files', re.compile(r'\b(edit|write|modify|delete|create) (files?|this file)\b', re.I)),
    ('tool_use', re.compile(r'\b(tool|mcp|connector|plugin)\b', re.I)),
]

_PATH_RE = re.compile(r'([A-Za-z]:\\[^\s:*?"<>|]+(?:\\[^\s:*?"<>|]+)*|~?/[^\s:*?"<>|]+(?:/[^\s:*?"<>|]*)*)')
_REPO_RE = re.compile(r'\b([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})\b')
_CODE_RE = re.compile(r'`([^`]{1,300})`')


def classify(prompt):
    for name, rx in TYPES:
        if rx.search(prompt or ''): return name
    return 'other'


def extract_target(prompt, kind=None):
    p = prompt or ''
    m = _CODE_RE.search(p)
    if kind in ('add_folder', 'edit_files'):
        mm = _PATH_RE.search(p.split(':', 1)[1] if ':' in p else p)
        if mm: return mm.group(1).rstrip('.,;)')
    if kind == 'add_repository':
        mm = _REPO_RE.search(p.split(':', 1)[1] if ':' in p else p)
        if mm: return mm.group(1)
    if m: return m.group(1)
    if ':' in p:
        tail = p.split(':', 1)[1].strip()
        if tail: return tail.split('\n', 1)[0][:300]
    mm = _PATH_RE.search(p)
    return mm.group(1).rstrip('.,;)') if mm else None


def card_id(kind, target, prompt):
    h = hashlib.sha1(f'{kind}\x00{target or ""}\x00{(prompt or "")[:200]}'.encode('utf-8')).hexdigest()[:10]
    return f'perm_{h}'


def find_cards(nodes, max_up=6):
    """nodes: list of dicts {id, parent, role, name, text} (document order). Returns a list of card dicts."""
    by_id = {n['id']: n for n in nodes}
    children = {}
    for n in nodes: children.setdefault(n.get('parent'), []).append(n)

    def subtree_text(nid, skip_buttons=True, limit=4000):
        out = []; st = [nid]; seen = 0
        while st and seen < 3000:
            cur = st.pop(); seen += 1; n = by_id.get(cur)
            if not n: continue
            if n['role'] == 'push button':
                if skip_buttons: continue
            elif n['role'] in ('static', 'text', 'label', 'heading', 'paragraph', 'code', 'link') and not children.get(cur):
                t = (n.get('text') or n.get('name') or '').strip()
                if t: out.append(t)
            st.extend(reversed([c['id'] for c in children.get(cur, [])]))
        s = ' '.join(out)
        return re.sub(r'\s+', ' ', s).strip()[:limit]

    def buttons_under(nid):
        out = []; st = [nid]
        while st:
            cur = st.pop(); n = by_id.get(cur)
            if not n: continue
            if n['role'] == 'push button' and OPTION_RE.match((n.get('name') or '').strip()): out.append(n)
            st.extend(reversed([c['id'] for c in children.get(cur, [])]))
        return out

    cards = {}; order = []
    for b in nodes:
        if b['role'] != 'push button' or not OPTION_RE.match((b.get('name') or '').strip()): continue
        cur = b.get('parent'); hops = 0
        while cur is not None and hops < max_up:
            n = by_id.get(cur)
            if not n: break
            txt = subtree_text(cur)
            name = (n.get('name') or '')
            if (txt and PROMPT_RE.search(txt)) or PROMPT_RE.search(name):
                if cur not in cards:
                    opts = buttons_under(cur)
                    labels = [o['name'].strip() for o in opts]
                    if any(AFFIRM_RE.match(l) for l in labels):
                        cards[cur] = {'container': cur, 'prompt': txt or name, 'options': labels, 'option_ids': [o['id'] for o in opts]}
                        order.append(cur)
                break
            cur = n.get('parent'); hops += 1
    # drop a card whose container is an ancestor of another card's container (keep the tightest box)
    def ancestors(nid):
        out = set(); cur = by_id.get(nid, {}).get('parent')
        while cur is not None and cur not in out: out.add(cur); cur = by_id.get(cur, {}).get('parent')
        return out
    inner_anc = set()
    for c in order: inner_anc |= ancestors(c)
    res = []
    for c in order:
        if c in inner_anc: continue
        card = cards[c]; prompt = card['prompt']
        kind = classify(prompt); target = extract_target(prompt, kind)
        res.append({'card_id': card_id(kind, target, prompt), 'type': kind, 'target': target, 'prompt': prompt[:600],
                    'options': card['options'], 'affirmative_options': [o for o in card['options'] if AFFIRM_RE.match(o)],
                    'negative_options': [o for o in card['options'] if NEGATE_RE.match(o)],
                    '_container': c, '_option_ids': card['option_ids']})
    return res


def public(cards):
    """Strip the private snapshot ids before returning cards to a caller."""
    return [{k: v for k, v in c.items() if not k.startswith('_')} for c in (cards or [])]


def match_option(card, option):
    """Exact (case-insensitive) label match, then unique prefix match. Returns (label, index) or raises ValueError."""
    want = (option or '').strip().casefold()
    labels = card['options']
    for i, l in enumerate(labels):
        if l.casefold() == want: return l, i
    pref = [(l, i) for i, l in enumerate(labels) if l.casefold().startswith(want)] if want else []
    if len(pref) == 1: return pref[0]
    raise ValueError(f'option {option!r} is not on this card; options: {labels}')


# Text-only fallback: an Auto-mode thread that stalled in prose instead of a card ("I need your permission to ...").
STALL_RE = re.compile(r"(need (your )?(permission|approval|explicit ok|go-ahead)|waiting (for|on) (your )?(approval|permission|ok|confirmation)|"
                      r"(can|may) i (go ahead|proceed)|should i (go ahead|proceed)|please (approve|confirm)|"
                      r"permission (prompt|request|card)|approve (this|the) (action|request|command)|requires? (your )?approval|"
                      r"(blocked|stuck) (on|waiting for) (a )?(permission|approval))", re.I)


def stalled_on_permission_text(text):
    m = STALL_RE.search(text or '')
    return m.group(0) if m else None
