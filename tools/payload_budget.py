#!/usr/bin/env python3
"""Payload budget: replay a synthetic Project through the real MCP tool handlers and measure what goes over the wire.

No app, no display, no network and no user data: a fictional Project (40 messages, 30 child threads) is written into a
private state dir, then claude_thread_read / claude_thread_status / claude_project_get are called through FastMCP's
call_tool (the same path a client's tools/call takes, CLI subprocess included) with refresh=false.

    python tools/payload_budget.py --json out.json                 # measure this checkout
    python tools/payload_budget.py --compare base.json --json out.json --table table.md
Exit 1 when a call exceeds its ceiling or grows more than 10% (and 512 bytes) over the base."""
import argparse, asyncio, datetime, json, os, pathlib, sys, tempfile, time

REPO = pathlib.Path(__file__).resolve().parents[1]
CEILING = {'claude_thread_read': 48 * 1024, 'claude_thread_status': 6 * 1024, 'claude_project_get': 40 * 1024}
GROWTH = 0.10
CHAN, PRIMARY = 'chan_PAYLOAD0000000000000001', 'cse_PAYLOAD0000000000000001'


def fixture(state):
    now = datetime.datetime.now(datetime.timezone.utc)
    iso = lambda h: (now - datetime.timedelta(hours=h)).isoformat()
    cache = state / 'cache'; cache.mkdir(parents=True)
    (cache / 'idb.json').write_text(json.dumps({'channels': [{'id': CHAN, 'name': 'Payload Demo', 'overview_session_id': PRIMARY,
                                                              'last_message_at': iso(0.1), 'created_at': iso(500)}]}))
    para = ('The build passed on the second try after the cache key changed; the tests that touch the picker now use the '
            'fake file chooser, and nothing was published. ')
    msgs = []
    for i in range(1, 41):
        user = i % 4 == 1
        text = ('[sent by Grok Bot]\nPlease run the next step and report back.' if user else (para * (2 + i % 5)).strip())
        msgs.append({'index': i, 'role': 'user' if user else 'assistant', 'text': text, 'summary': None, 'time_app': None,
                     'time_local': None, 'time_utc': iso(40 - i), 'truncated_in_app': False, 'links': None})
    threads = [{'title': f'Workstream {n}: tidy the release notes and check the links', 'thread_id': f'cmsg_PAYLOAD{n:020d}',
                'group': 'Waiting on you' if n % 7 == 0 else 'Done', 'status': 'done', 'age': f'{n}h'} for n in range(30)]
    data = {'project': 'Payload Demo', 'project_id': CHAN, 'primary_thread_id': PRIMARY, 'thread_id': 'primary',
            'thread_title': None, 'route': f'/epitaxy/project/{CHAN}?thread={PRIMARY}', 'read_at': iso(0.05),
            'claude_working': False, 'environment': None, 'dialogs': [], 'messages': msgs, 'message_count_returned': len(msgs),
            'latest_message_index': 40, 'permission_cards': [], 'threads': threads, 'threads_waiting': 4}
    (cache / f'{CHAN}.json').write_text(json.dumps(data))


CALLS = [('claude_thread_read', {'project': CHAN, 'refresh': False}),
         ('claude_thread_status', {'project': CHAN}),
         ('claude_project_get', {'project': CHAN, 'refresh': False})]


def measure():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='claude-projects-payload-'))
    cfg = tmp / 'config.json'
    cfg.write_text(json.dumps({'state_dir': str(tmp / 'state'), 'display': ':199', 'timezone': 'America/New_York', 'app_timezone': 'UTC'}))
    fixture(tmp / 'state')
    os.environ.update(CLAUDE_PROJECTS_CONFIG=str(cfg), CLAUDE_PROJECTS_IDB_RESOLVE_MAX_AGE='86400')
    sys.path.insert(0, str(REPO))
    from claude_projects import mcp_server as M
    out = {}
    for name, args in CALLS:
        t0 = time.monotonic()
        res = asyncio.run(M.mcp.call_tool(name, args))
        content = res[0] if isinstance(res, tuple) else res
        wire = json.dumps([c.model_dump(mode='json') for c in content], ensure_ascii=False).encode()
        body = json.loads(content[0].text) if content and getattr(content[0], 'text', None) else {}
        if body.get('error'): raise SystemExit(f'{name} failed: {body}')
        out[name] = {'bytes': len(wire), 'messages': len(body.get('messages') or []), 's': round(time.monotonic() - t0, 2)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json'); ap.add_argument('--compare'); ap.add_argument('--table')
    a = ap.parse_args()
    cur = measure()
    if a.json: pathlib.Path(a.json).write_text(json.dumps(cur, indent=1))
    base = json.loads(pathlib.Path(a.compare).read_text()) if a.compare and pathlib.Path(a.compare).exists() else {}
    rows, bad = ['| Call | Main | This PR | Impact | Ceiling | Messages |', '|---|---|---|---|---|---|'], []
    for name, m in cur.items():
        b = (base.get(name) or {}).get('bytes')
        impact = f'{m["bytes"] - b:+d} B ({(m["bytes"] - b) / b:+.1%})' if b else 'n/a'
        rows.append(f'| {name} | {b if b else "n/a"} B | {m["bytes"]} B | {impact} | {CEILING[name]} B | {m["messages"]} |')
        if m['bytes'] > CEILING[name]: bad.append(f'{name}: {m["bytes"]} B is over the {CEILING[name]} B ceiling')
        if b and m['bytes'] - b > max(512, b * GROWTH): bad.append(f'{name}: grew {m["bytes"] - b} B over main ({b} B)')
    table = '\n'.join(rows)
    print(table)
    if a.table: pathlib.Path(a.table).write_text('### Payload budget\n\n' + table + '\n' + ('\n'.join(f'- {x}' for x in bad) + '\n' if bad else ''))
    for x in bad: print('FAIL', x)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
