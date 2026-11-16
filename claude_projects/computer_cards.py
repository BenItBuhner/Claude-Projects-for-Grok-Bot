"""Computer-use cards on the controlled computer: decide from a snapshot of its Claude app, press nothing here.

The computer-use apps card ("Claude wants to use: Sketchpad All ... Notepad All" [Deny] [Allow for this session]) is bound
to the computer Claude controls: the Claude app on any other desktop can only send Deny for it (see
claude_projects.permissions). So an Allow has to be pressed in the Claude app ON that computer. On a Windows PC,
hosts/windows/computer-permission.ps1 reads that app over UI Automation ({"action": "snapshot"}) and presses one
button by runtime id ({"action": "press", ...}). This module is the pure part in between: it finds the cards in the
snapshot with the same detector as the thread reader, checks them against the caller's rules, and returns the exact
press request (or a refusal). It never runs anything itself.

Rules for an Allow (all must hold, else the decision is 'refuse' and nothing is to be pressed):
  * the card is a computer-use APPS card (the enable probe has no apps and is answered from the thread instead);
  * every requested app is in only_apps (display name, program path or file name; case-insensitive);
  * the label is a one-time or this-session Allow (never 'Always allow');
  * the card does not ask for full-screen control, the clipboard or system keys.
"""
import json, datetime, pathlib
from claude_projects import permissions as PERM

TITLE = 'Claude wants to use'


def cards_in_snapshot(snapshot):
    """snapshot = the ps1 'snapshot' result -> cards (PERM.find_cards shape) each with _press [{hwnd, rid, label}] and window."""
    out = []
    for w in (snapshot or {}).get('windows') or []:
        for blk in w.get('blocks') or []:
            nodes = [dict(n, text=n.get('text') or '') for n in (blk.get('nodes') or [])]
            by_id = {n['id']: n for n in nodes}
            for c in PERM.find_cards(nodes, max_up=8):
                if c['type'] != 'computer_use': continue
                c['_press'] = [{'hwnd': w.get('hwnd'), 'rid': by_id[i].get('rid'), 'label': (by_id[i].get('name') or '').strip()} for i in c['_option_ids']]
                c['window'] = w.get('title'); c['hwnd'] = w.get('hwnd')
                c['cu_kind'] = PERM.computer_use_kind(c)
                if not any(x['card_id'] == c['card_id'] for x in out): out.append(c)
    return out


def decide(card, option, only_apps):
    """-> {'decision': 'press' | 'refuse', 'option', 'reason', 'press_request'?}."""
    try: label, idx = PERM.match_option(card, option)
    except ValueError as ex: return {'decision': 'refuse', 'option': option, 'reason': str(ex)}
    affirm = bool(PERM.AFFIRM_RE.match(label))
    if affirm:
        if card.get('cu_kind') != 'apps':
            return {'decision': 'refuse', 'option': label, 'reason': 'not an apps card (the enable probe is answered with Continue in the thread)'}
        if only_apps is None:
            return {'decision': 'refuse', 'option': label, 'reason': 'only_apps is required for an Allow on the controlled computer'}
        if PERM.CU_FULL_RE.search(card.get('prompt') or ''):
            return {'decision': 'refuse', 'option': label, 'reason': 'asks for full-screen control, the clipboard or system keys'}
        if PERM.CU_ALWAYS_RE.match(label):
            return {'decision': 'refuse', 'option': label, 'reason': f'{label!r} is a lasting grant'}
        extra = PERM.apps_outside(card, only_apps)
        if extra:
            return {'decision': 'refuse', 'option': label, 'apps_outside': extra, 'reason': f'apps outside only_apps: {", ".join(extra)}'}
    req = dict(card['_press'][idx], action='press', title=TITLE)
    if not req.get('rid'): return {'decision': 'refuse', 'option': label, 'reason': 'the button has no runtime id in the snapshot'}
    return {'decision': 'press', 'option': label, 'press_request': req,
            'reason': ('every requested app is in only_apps' if affirm else 'a Deny / Not now answer')}


def plan(snapshot, option, only_apps=None, card_id=None, apply=False):
    """The whole decision for one snapshot. apply=False (default) is a dry run: press_request is reported as
    would_press and must not be sent to the host script. Returns a dict safe to show (no private ids)."""
    cards = cards_in_snapshot(snapshot)
    host = (snapshot or {}).get('host')
    base = {'host': host, 'read_at': (snapshot or {}).get('read_at'), 'cards': PERM.public(cards), 'dry_run': not apply,
            'only_apps': list(only_apps) if only_apps is not None else None}
    if not cards: return dict(base, decision='none', reason='no computer-use card is showing in the Claude app on this computer')
    if card_id:
        card = next((c for c in cards if c['card_id'] == card_id), None)
        if not card: return dict(base, decision='refuse', reason=f'card {card_id} is not showing')
    elif len(cards) == 1: card = cards[0]
    else: return dict(base, decision='refuse', reason=f'{len(cards)} cards are showing; pass card_id')
    d = decide(card, option, only_apps)
    out = dict(base, card_id=card['card_id'], apps=[a.get('displayName') for a in card.get('apps') or []], **{k: v for k, v in d.items() if k != 'press_request'})
    if d['decision'] == 'press':
        if apply: out['press_request'] = d['press_request']
        else: out['would_press'] = d['option']; out['note'] = 'dry run: nothing is to be pressed (pass apply to get the press request)'
    return out


def log_entry(path, entry):
    p = pathlib.Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    e = dict(entry); e.setdefault('at', datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'))
    with open(p, 'a', encoding='utf-8') as f: f.write(json.dumps(e, ensure_ascii=False, default=str) + '\n')
    return e
