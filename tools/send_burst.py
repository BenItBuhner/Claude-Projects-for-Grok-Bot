#!/usr/bin/env python3
"""Send-burst check: N concurrent claude_thread_send calls with dry_run=true, each to a different Project, through the
real MCP tool handler (FastMCP call_tool, the same path a client's tools/call takes). Dry runs open the target and check
the composer and the Send button; nothing is typed, attached or sent.

    python tools/send_burst.py chan_A chan_B chan_C chan_D chan_E --json burst.json
    python tools/send_burst.py --auto 5                          # the 5 most recently active Projects
    python tools/send_burst.py ... --real-extra-s 9 --real-tail-s 2

Reports p50 / p95 / max from queued to done, the lock wait and lock hold of each call, and an estimate of the same burst
with real sends: each lock hold grows by --real-extra-s (paste, read-back, Send and the on-lock verify, measured on real
single sends), and each send ends --real-tail-s after it lets go of the lock. Run it only on the live host, at a quiet
moment: it takes the app for a few seconds per Project."""
import argparse, asyncio, json, pathlib, statistics, sys, time

REPO = pathlib.Path(__file__).resolve().parents[1]


def pct(xs, p):
    """Percentile with linear interpolation (numpy's default)."""
    xs = sorted(xs)
    if not xs: return None
    k = (len(xs) - 1) * p / 100.0; f = int(k); c = min(f + 1, len(xs) - 1)
    return round(xs[f] + (xs[c] - xs[f]) * (k - f), 1)


def summary(xs):
    return {'p50': pct(xs, 50), 'p95': pct(xs, 95), 'max': round(max(xs), 1) if xs else None, 'mean': round(statistics.mean(xs), 1) if xs else None}


def estimate(holds, extra, tail):
    """All sends queued at t=0 and served one after another: send k ends at sum(hold_1..k) + extra*k + tail."""
    t = 0.0; ends = []
    for h in holds:
        t += h + extra; ends.append(t + tail)
    return summary(ends)


async def one(M, chan, text):
    t0 = time.monotonic()
    res = await M.mcp.call_tool('claude_thread_send', {'project': chan, 'message': text, 'dry_run': True})
    wall = time.monotonic() - t0
    content = res[0] if isinstance(res, tuple) else res
    body = json.loads(content[0].text) if content and getattr(content[0], 'text', None) else {}
    tm = body.get('timing') or {}
    wait = float(tm.get('lock') or body.get('lock_waited_s') or 0.0)
    total = float(tm.get('total') or wall)
    return {'project_id': chan, 'ok': bool(body.get('dry_run')) and not body.get('error'), 'error': body.get('error'), 'message': body.get('message'),
            'queued_to_done_s': round(wall, 2), 'lock_wait_s': round(wait, 2), 'lock_hold_s': round(max(0.0, total - wait), 2),
            'timing': tm, 'busy_retries': body.get('busy_retries')}


async def burst(chans, text):
    sys.path.insert(0, str(REPO))
    from claude_projects import mcp_server as M
    return await asyncio.gather(*(one(M, c, text) for c in chans))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('projects', nargs='*', help='chan_ ids, one per concurrent dry run')
    ap.add_argument('--auto', type=int, help='use the N most recently active Projects (from the app cache)')
    ap.add_argument('--text', default='Burst dry run: nothing is typed or sent.')
    ap.add_argument('--real-extra-s', type=float, default=None, help='extra lock hold of a real send over a dry run (s)')
    ap.add_argument('--real-tail-s', type=float, default=0.0, help='time a real send spends after the lock (s)')
    ap.add_argument('--json', help='write the full result here')
    a = ap.parse_args(argv)
    chans = list(a.projects)
    if a.auto:
        sys.path.insert(0, str(REPO))
        from claude_projects import core as C
        chans = [r['id'] for r in C.project_rows(C.idb_snapshot(max_age=600))][:a.auto]
    if len(set(chans)) != len(chans) or not chans: raise SystemExit('pass distinct chan_ ids (one per dry run)')
    t0 = time.time()
    rows = asyncio.run(burst(chans, a.text))
    out = {'started_epoch': round(t0, 1), 'n': len(rows), 'all_ok': all(r['ok'] for r in rows), 'calls': rows,
           'queued_to_done_s': summary([r['queued_to_done_s'] for r in rows]),
           'lock_wait_s': summary([r['lock_wait_s'] for r in rows]),
           'lock_hold_s': summary([r['lock_hold_s'] for r in rows])}
    if a.real_extra_s is not None:
        holds = [r['lock_hold_s'] for r in sorted(rows, key=lambda r: r['lock_wait_s'])]
        out['real_burst_estimate_s'] = dict(estimate(holds, a.real_extra_s, a.real_tail_s), extra_s=a.real_extra_s, tail_s=a.real_tail_s)
    if a.json: pathlib.Path(a.json).write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != 'calls'}, indent=2))
    for r in rows: print(f"{r['project_id']}: done {r['queued_to_done_s']} s, waited {r['lock_wait_s']} s, held {r['lock_hold_s']} s" + (f" ERROR {r['error']}: {r['message']}" if not r['ok'] else ''))
    return 0 if out['all_ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
