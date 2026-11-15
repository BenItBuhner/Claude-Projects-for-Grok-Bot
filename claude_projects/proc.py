#!/usr/bin/env python3
"""claude_projects.proc - run the plugin's services on a host without user systemd (process mode).

Some hosts have no systemd at all, for example a container whose PID 1 is tini. There the plugin keeps the same unit
files install.sh renders (~/.config/systemd/user/claude-projects-*.service) and this small supervisor runs them: it reads
Environment=, ExecStartPre=, ExecStart=, StandardInput=file:, Restart= and RestartSec=, starts units in dependency
order, restarts them when they fail, runs claude-projects-refresh every 10 minutes while claude-projects-refresh.timer is enabled, and adds
the one thing systemd would have provided: a private D-Bus session bus (claude-projects-dbus) for the app, the keyring and the
accessibility bus.

The command line mirrors `systemctl --user`, so the setup flow can use either:

  python -m claude_projects.proc up | down | status [--json]
  python -m claude_projects.proc enable [--now] UNIT... | disable [--now] UNIT... | start UNIT... | stop UNIT... | restart UNIT...
  python -m claude_projects.proc is-active UNIT | is-enabled UNIT | reset-failed [UNIT] | daemon-reload | show-environment

Nothing here needs root. State lives in <state_dir>/proc (enabled set, wanted set, status, per-unit logs).
Nothing starts the supervisor at boot on such a host, so the MCP server (stdio, started by the agent) and
`claude-projects setup status` bring it up when it isn't running; `claude-projects setup start` does it by hand.
"""
import os, sys, json, time, fcntl, shlex, signal, pathlib, subprocess, argparse

_PKG = pathlib.Path(__file__).resolve().parent   # run as a file (python claude_projects/<module>.py): import from the checkout,
sys.path[:] = [p for p in sys.path if pathlib.Path(p or '.').resolve() != _PKG]   # not from inside the package
sys.path.insert(0, str(_PKG.parent))
from claude_projects import config as cfg_mod

CFG = cfg_mod.CFG
UNIT_DIR = pathlib.Path(os.environ.get('XDG_CONFIG_HOME') or (pathlib.Path.home() / '.config')) / 'systemd' / 'user'
PROC = pathlib.Path(CFG['state_dir']) / 'proc'
LOGS = PROC / 'logs'
ENABLED, WANTED, STATUS, REQUESTS = PROC / 'enabled.json', PROC / 'wanted.json', PROC / 'status.json', PROC / 'requests'
LOCK, SUP_LOG = PROC / 'supervisor.lock', PROC / 'supervisor.log'
DBUS_UNIT = 'claude-projects-dbus'
PREFIXES = ('claude-projects-',)            # the plugin's units
TIMER_PERIOD = {'claude-projects-refresh': 600}   # claude-projects-refresh.timer: OnUnitActiveSec=10min
TIMER_FIRST = {'claude-projects-refresh': 300}    # OnBootSec=5min
MAX_RESTARTS, RESTART_WINDOW = 5, 120        # more failures than this inside the window -> 'failed' until started again
LOG_MAX = 5 * 2**20


def _name(u):
    u = os.path.basename(str(u))
    for suf in ('.service', '.timer', '.socket', '.target'):
        if u.endswith(suf): return u[:-len(suf)], suf[1:]
    return u, 'service'


def _read_json(p, default):
    try: return json.loads(p.read_text())
    except Exception: return default


def _write_json(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f'.{os.getpid()}.tmp'); tmp.write_text(json.dumps(data, indent=1, sort_keys=True)); os.replace(tmp, p)


class _Locked:
    """Serializes edits of enabled.json / wanted.json between CLI calls."""
    def __enter__(self):
        PROC.mkdir(parents=True, exist_ok=True); self.f = open(PROC / 'edit.lock', 'w'); fcntl.flock(self.f, fcntl.LOCK_EX); return self
    def __exit__(self, *a): fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()


SNAP_BIN = '/snap/bin'


def runtime_env():
    rd = pathlib.Path(cfg_mod.runtime_dir(CFG)); rd.mkdir(parents=True, exist_ok=True); os.chmod(rd, 0o700)
    env = dict(os.environ)
    env['XDG_RUNTIME_DIR'] = str(rd); env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={rd}/bus'
    env.setdefault('HOME', str(pathlib.Path.home())); env.setdefault('PATH', '/usr/local/bin:/usr/bin:/bin')
    if SNAP_BIN not in env['PATH'].split(':'): env['PATH'] += ':' + SNAP_BIN   # snaps (the sign-in browser)
    env.pop('NOTIFY_SOCKET', None)
    return env


# ---------------------------------------------------------------- unit files

def _spec(s, env):
    return (s.replace('%%', '\0').replace('%t', env['XDG_RUNTIME_DIR']).replace('%h', env['HOME'])
             .replace('%u', os.environ.get('USER') or str(os.getuid())).replace('\0', '%'))


def parse_unit(name, env):
    """-> dict(exec=[argv], pre=[[argv]...], env={}, stdin=path|None, restart=str, restart_sec=float, requires=[], after=[],
    nice=int|None, oneshot=bool) or None when the unit file doesn't exist."""
    if name == DBUS_UNIT:
        rd = env['XDG_RUNTIME_DIR']
        return {'exec': ['dbus-daemon', '--session', f'--address=unix:path={rd}/bus', '--nofork', '--nopidfile', '--nosyslog'],
                'pre': [], 'env': {}, 'stdin': None, 'restart': 'always', 'restart_sec': 1.0, 'requires': [], 'after': [],
                'nice': None, 'oneshot': False, 'cleanup': f'{rd}/bus'}
    p = UNIT_DIR / f'{name}.service'
    if not p.is_file(): return None
    u = {'exec': None, 'pre': [], 'env': {}, 'stdin': None, 'restart': 'no', 'restart_sec': 0.1, 'requires': [], 'after': [],
         'nice': None, 'oneshot': False}
    for raw in p.read_text().splitlines():
        ln = raw.strip()
        if not ln or ln.startswith(('#', ';', '[')) or '=' not in ln: continue
        k, v = ln.split('=', 1); k = k.strip(); v = _spec(v.strip(), env)
        if k == 'ExecStart': u['exec'] = shlex.split(v.lstrip('-@+!'))
        elif k == 'ExecStartPre': u['pre'].append(shlex.split(v.lstrip('-@+!')))
        elif k == 'Environment':
            for kv in shlex.split(v):
                if '=' in kv: a, b = kv.split('=', 1); u['env'][a] = b
        elif k == 'StandardInput' and v.startswith('file:'): u['stdin'] = v[5:]
        elif k == 'Restart': u['restart'] = v
        elif k == 'RestartSec':
            try: u['restart_sec'] = float(v.rstrip('s'))
            except ValueError: pass
        elif k == 'Nice':
            try: u['nice'] = int(v)
            except ValueError: pass
        elif k == 'Type' and v == 'oneshot': u['oneshot'] = True
        elif k in ('Requires', 'BindsTo'): u['requires'] += [_name(x)[0] for x in v.split()]
        elif k in ('After',): u['after'] += [_name(x)[0] for x in v.split()]
    if not u['exec']: return None
    # every service but the bus itself talks D-Bus (keyring, app, AT-SPI); systemd orders them after dbus.socket
    ours = lambda n: n.startswith(PREFIXES)   # dbus.socket, default.target ...: provided here by claude-projects-dbus or not needed
    u['requires'] = [r for r in u['requires'] if ours(r)] + ([DBUS_UNIT] if name != 'claude-projects-xvfb' else [])
    u['after'] = [a for a in u['after'] if ours(a)] + u['requires']
    return u


def known_units():
    names = sorted(_name(p.name)[0] for p in UNIT_DIR.glob('claude-projects-*.service')) if UNIT_DIR.is_dir() else []
    return [DBUS_UNIT] + [n for n in names if n != DBUS_UNIT]


# ---------------------------------------------------------------- the supervisor

class Supervisor:
    def __init__(self):
        self.env = runtime_env(); self.procs = {}; self.state = {}; self.stopping = {}; self.next_timer = {}
        self.restart_at = {}; self.failures = {}; self.boot = time.time(); self.quit = False

    def log(self, msg):
        with open(SUP_LOG, 'a') as f: f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')

    def _logf(self, name):
        LOGS.mkdir(parents=True, exist_ok=True); p = LOGS / f'{name}.log'
        try:
            if p.stat().st_size > LOG_MAX: os.replace(p, p.with_suffix('.log.1'))
        except FileNotFoundError: pass
        return open(p, 'ab')

    def active(self, name):
        pr = self.procs.get(name)
        return pr is not None and pr.poll() is None and name not in self.stopping

    def start(self, name, u):
        env = dict(self.env, **u['env'])
        out = self._logf(name)
        nice = u.get('nice')
        def pre():
            os.setsid()
            if nice: os.nice(nice)
        try:
            for argv in u['pre']:
                subprocess.run(argv, env=env, stdout=out, stderr=out, stdin=subprocess.DEVNULL, timeout=60)
            if u.get('cleanup'):
                try: os.unlink(u['cleanup'])
                except FileNotFoundError: pass
            stdin = open(u['stdin'], 'rb') if u['stdin'] else subprocess.DEVNULL
            self.procs[name] = subprocess.Popen(u['exec'], env=env, stdout=out, stderr=out, stdin=stdin, preexec_fn=pre, close_fds=True)
            if stdin is not subprocess.DEVNULL: stdin.close()
            prev = self.state.get(name) or {}
            self.state[name] = {'state': 'active', 'pid': self.procs[name].pid, 'since': time.time(),
                                **{k: prev[k] for k in ('restarts', 'last_exit') if prev.get('restarts')}}
            self.log(f'started {name} pid {self.procs[name].pid}')
        except Exception as ex:
            self.state[name] = {'state': 'failed', 'error': str(ex)[:300], 'since': time.time()}
            self.log(f'could not start {name}: {ex}')
        finally:
            out.close()

    def stop(self, name, hard=False):
        pr = self.procs.get(name)
        if pr is None or pr.poll() is not None: return
        try: os.killpg(pr.pid, signal.SIGKILL if hard else signal.SIGTERM)
        except ProcessLookupError: pass
        self.stopping.setdefault(name, time.time())

    def reap(self, wanted):
        for name, pr in list(self.procs.items()):
            rc = pr.poll()
            if name in self.stopping:
                if rc is not None:
                    self.stopping.pop(name); self.procs.pop(name)
                    self.state[name] = {'state': 'inactive', 'since': time.time()}
                    self.log(f'stopped {name}')
                elif time.time() - self.stopping[name] > 8: self.stop(name, hard=True)
                continue
            if rc is None: continue
            self.procs.pop(name)
            try: os.killpg(pr.pid, signal.SIGTERM)   # leftover children of a dead unit
            except (ProcessLookupError, PermissionError): pass
            u = parse_unit(name, self.env) or {}
            if u.get('oneshot'):
                self.state[name] = {'state': 'inactive', 'since': time.time(), 'last_exit': rc}; continue
            pol = u.get('restart', 'no')
            again = name in wanted and (pol == 'always' or (pol == 'on-failure' and rc != 0))
            now = time.time()
            fl = [t for t in self.failures.get(name, []) if now - t < RESTART_WINDOW] + [now]; self.failures[name] = fl
            if again and len(fl) <= MAX_RESTARTS:
                self.state[name] = {'state': 'activating', 'since': now, 'last_exit': rc, 'restarts': len(fl)}
                self.restart_at[name] = now + max(u.get('restart_sec', 0.1), 1.0)
                self.log(f'{name} exited {rc}; restarting')
            else:
                self.state[name] = {'state': 'failed' if rc != 0 or again else 'inactive', 'since': now, 'last_exit': rc}
                self.log(f'{name} exited {rc}; ' + ('too many restarts, giving up until started again' if again else 'not restarting'))

    def order(self, wanted):
        units = {n: parse_unit(n, self.env) for n in wanted if not n.endswith('.timer')}
        out, seen = [], set()
        def visit(n, depth=0):
            if n in seen or depth > 20: return
            seen.add(n)
            for d in (units.get(n) or {}).get('after', []):
                if d in units: visit(d, depth + 1)
            out.append(n)
        for n in sorted(units): visit(n)
        return [(n, units[n]) for n in out]

    def handle_requests(self):
        if not REQUESTS.is_dir(): return
        for r in sorted(REQUESTS.glob('*.json')):
            req = _read_json(r, {}); r.unlink(missing_ok=True)
            for n in req.get('restart', []):
                self.failures.pop(n, None); self.restart_at.pop(n, None)
                if self.active(n): self.stop(n); self.state[n] = dict(self.state.get(n, {}), restart_pending=True)
            for n in req.get('reset', []):
                self.failures.pop(n, None)
                if (self.state.get(n) or {}).get('state') == 'failed': self.state[n] = {'state': 'inactive', 'since': time.time()}
            if req.get('quit'): self.quit = True

    def tick(self):
        wanted = set(_read_json(WANTED, []))
        self.handle_requests()
        self.reap(wanted)
        timers = [n for n in wanted if n.endswith('.timer')] if not self.quit else []
        services = set(n for n in wanted if not n.endswith('.timer')) | {DBUS_UNIT} | {t[:-6] for t in timers}
        if self.quit: services = set()
        # stop what is no longer wanted, and what lost a required unit (systemd's Requires= semantics)
        for name in list(self.procs):
            u = parse_unit(name, self.env) or {}
            lost = [d for d in u.get('requires', []) if d in services and not self.active(d) and d != name]
            if name not in services or (lost and not u.get('oneshot')): self.stop(name)
        now = time.time()
        for name, u in self.order(services):
            st = (self.state.get(name) or {}).get('state')
            if u is None:
                self.state[name] = {'state': 'not-found', 'since': now}; continue
            if self.active(name) or name in self.stopping or u.get('oneshot'): continue
            if st == 'failed' and not self.state[name].get('restart_pending'): continue
            if self.restart_at.get(name, 0) > now: continue
            if not all(self.active(d) for d in u['requires'] if d != name): continue
            if name != DBUS_UNIT and not os.path.exists(self.env['XDG_RUNTIME_DIR'] + '/bus'): continue
            self.restart_at.pop(name, None); self.start(name, u)
        # timers: claude-projects-refresh.timer runs claude-projects-refresh.service (oneshot) every 10 minutes
        for t in timers:
            svc = t[:-6]
            first = self.next_timer.setdefault(svc, self.boot + TIMER_FIRST.get(svc, 300))
            if now >= first and not self.active(svc):
                u = parse_unit(svc, self.env)
                if u: self.start(svc, u)
                self.next_timer[svc] = now + TIMER_PERIOD.get(svc, 600)
        snap = {n: dict(v) for n, v in self.state.items()}
        for t in [n for n in wanted if n.endswith('.timer')]:
            snap[t] = {'state': 'active', 'next_run': self.next_timer.get(t[:-6])}
        _write_json(STATUS, {'supervisor_pid': os.getpid(), 'updated': now, 'runtime_dir': self.env['XDG_RUNTIME_DIR'], 'units': snap})

    def run(self):
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, 'quit', True))
        signal.signal(signal.SIGINT, lambda *a: setattr(self, 'quit', True))
        self.log(f'supervisor up (pid {os.getpid()})')
        while True:
            try: self.tick()
            except Exception as ex: self.log(f'tick error: {ex!r}')
            if self.quit and not any(pr.poll() is None for pr in self.procs.values()): break
            time.sleep(0.5)
        _write_json(STATUS, {'supervisor_pid': None, 'updated': time.time(), 'units': {}})
        self.log('supervisor down')


def _lock_held():
    """True while a supervisor holds supervisor.lock. The probe takes only a shared lock for an instant, so probes
    never block each other; a supervisor that is starting retries its exclusive lock instead of giving up on one."""
    PROC.mkdir(parents=True, exist_ok=True)
    with open(LOCK, 'a+') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(f, fcntl.LOCK_UN); return False
        except BlockingIOError:
            return True


def supervisor_running(): return _lock_held()


LOCK_RETRY_S = 5.0


def _take_supervisor_lock(f, retry=LOCK_RETRY_S):
    """Exclusive lock for the supervisor's whole life. A probe (shared lock, microseconds) may be in the way for a
    moment, so keep trying; only a lock still held after `retry` seconds means another supervisor really runs."""
    deadline = time.time() + retry
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); return True
        except BlockingIOError:
            if time.time() >= deadline: return False
            time.sleep(0.02)


def up(wait=10.0):
    """Start the supervisor in the background unless it is already running. Returns 'running' (it already was),
    'started' (this call started it) or False.
    Callers that race (the MCP server and `claude-projects setup status` at once) are serialized by start.lock, so only one of
    them forks a supervisor and the others see it running."""
    if supervisor_running(): return 'running'
    PROC.mkdir(parents=True, exist_ok=True)
    with open(PROC / 'start.lock', 'a+') as sl:
        fcntl.flock(sl, fcntl.LOCK_EX)   # blocking: waits while another caller starts the supervisor
        if supervisor_running(): return 'running'
        with _Locked():
            if not WANTED.exists(): _write_json(WANTED, sorted(set(_read_json(ENABLED, []))))
            else: _write_json(WANTED, sorted(set(_read_json(WANTED, [])) | set(_read_json(ENABLED, []))))
        pid = os.fork()
        if pid == 0:
            try:
                sl.close()   # the start lock stays with the caller; the supervisor must not keep it held
                os.setsid()
                if os.fork() != 0: os._exit(0)
                f = open(LOCK, 'a+')
                if not _take_supervisor_lock(f): os._exit(0)
                devnull = os.open(os.devnull, os.O_RDWR)
                for fd in (0, 1, 2): os.dup2(devnull, fd)
                os.chdir(str(pathlib.Path.home()))
                Supervisor().run()
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        t0 = time.time()
        while time.time() - t0 < wait:
            if supervisor_running(): return 'started'
            time.sleep(0.1)
        return 'started' if supervisor_running() else False


def ensure_up():
    """Called by the MCP server and the setup flow in process mode: idempotent, quiet."""
    try: return up()
    except Exception: return False


def unit_state(name):
    st = _read_json(STATUS, {})
    if not supervisor_running(): return 'inactive'
    return ((st.get('units') or {}).get(name) or {}).get('state') or 'inactive'


def _unit_pid(name):
    return (((_read_json(STATUS, {}).get('units') or {}).get(name)) or {}).get('pid')


def _wait_state(names, want_active, timeout, not_pids=None):
    t0 = time.time(); not_pids = not_pids or {}
    while time.time() - t0 < timeout:
        states = {n: unit_state(n) for n in names}
        if want_active and all(s == 'active' and (not not_pids.get(n) or _unit_pid(n) != not_pids[n]) for n, s in states.items()):
            return True
        if not want_active and all(s in ('inactive', 'failed', 'not-found') for s in states.values()): return True
        # a stale 'failed' from before this request may still be on file for a moment
        if want_active and time.time() - t0 > 2 and any(s in ('failed', 'not-found') for s in states.values()): return False
        time.sleep(0.3)
    return False


def _why(names):
    units = _read_json(STATUS, {}).get('units') or {}
    out = []
    for n in names:
        u = units.get(n) or {}
        st = u.get('state') or 'inactive'
        if st == 'active' and u.get('restarts'): st = f"failed (keeps exiting, last exit {u.get('last_exit')}; restarted {u['restarts']}x)"
        elif st in ('activating', 'failed') and u.get('last_exit') is not None: st = f"failed (exit {u['last_exit']})"
        if st != 'active': out.append(f'{n}: {st} (log: {LOGS / (n + ".log")})')
    return '; '.join(out) or 'not stable yet: ' + ', '.join(names)


def _edit(path, add=(), remove=()):
    with _Locked():
        cur = set(_read_json(path, [])); cur |= set(add); cur -= set(remove); _write_json(path, sorted(cur))


def _request(**req):
    REQUESTS.mkdir(parents=True, exist_ok=True)
    _write_json(REQUESTS / f'{time.time():.6f}-{os.getpid()}.json', req)


def _full(n):
    base, kind = _name(n)
    return base + '.timer' if kind == 'timer' else base


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m claude_projects.proc', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('verb')
    ap.add_argument('units', nargs='*')
    ap.add_argument('--now', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--timeout', type=float, default=20.0)
    ap.add_argument('--quiet', '-q', action='store_true')
    a = ap.parse_intermixed_args(argv)   # `enable --now UNIT...`, as with systemctl (plain parse_args drops them on 3.12)
    names = [_full(u) for u in a.units]
    v = a.verb
    if v in ('daemon-reload', 'reset-failed') and not names: return 0
    if v == 'show-environment':
        if not supervisor_running(): print('supervisor not running (claude_projects/proc.py up)', file=sys.stderr); return 1
        for k in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'): print(f'{k}={runtime_env()[k]}')
        return 0
    if v == 'up':
        r = up()
        print(f'supervisor {r}' if r else 'supervisor did not start (see ' + str(SUP_LOG) + ')'); return 0 if r else 1
    if v == 'down':
        if supervisor_running():
            _request(quit=True); t0 = time.time()
            while supervisor_running() and time.time() - t0 < a.timeout: time.sleep(0.2)
        print('supervisor stopped' if not supervisor_running() else 'supervisor still running'); return 0 if not supervisor_running() else 1
    if v == 'status':
        st = _read_json(STATUS, {}); st['running'] = supervisor_running(); st['enabled'] = _read_json(ENABLED, []); st['wanted'] = _read_json(WANTED, [])
        if a.json: print(json.dumps(st, default=str))
        else:
            print(f"supervisor: {'running' if st['running'] else 'not running'}")
            for n in sorted(set(st['enabled']) | set((st.get('units') or {}))):
                print(f"  {n:<22} {unit_state(n):<10} {'enabled' if n in st['enabled'] else ''}")
        return 0
    if v == 'is-active':
        s = unit_state(names[0]) if names else 'inactive'; print(s); return 0 if s == 'active' else 3
    if v == 'is-enabled':
        s = 'enabled' if names and names[0] in _read_json(ENABLED, []) else 'disabled'; print(s); return 0 if s == 'enabled' else 1
    if v == 'reset-failed':
        _request(reset=names); return 0
    if not names: print(f'{v}: name at least one unit', file=sys.stderr); return 2
    services = [n for n in names if not n.endswith('.timer')]
    missing = [n for n in services if parse_unit(n, runtime_env()) is None]
    if v in ('enable', 'start', 'restart') and missing:
        print(f"Unit {', '.join(m + '.service' for m in missing)} not found (render the units first: install.sh)", file=sys.stderr); return 5
    if v == 'enable':
        _edit(ENABLED, add=names)
        if not a.now: return 0
        v = 'start'
    if v == 'disable':
        _edit(ENABLED, remove=names)
        if not a.now: return 0
        v = 'stop'
    if v == 'start':
        _edit(WANTED, add=names)
        if not up(): print(f'supervisor did not start (see {SUP_LOG})', file=sys.stderr); return 1
        _request(reset=services)
        ok = _wait_state(services, True, a.timeout) if services else True
        if ok and services:   # a unit that exits right away also looks active for a moment: it has to stay up
            pids = {n: _unit_pid(n) for n in services}; time.sleep(1.5)
            ok = all(unit_state(n) == 'active' and _unit_pid(n) == pids[n] for n in services)
        if not ok:
            print(_why(services), file=sys.stderr); return 1
        return 0
    if v == 'stop':
        _edit(WANTED, remove=names)
        if supervisor_running(): _wait_state(services, False, a.timeout)
        return 0
    if v == 'restart':
        _edit(WANTED, add=names)
        if not up(): return 1
        old = {n: _unit_pid(n) for n in services if unit_state(n) == 'active'}
        _request(restart=services)
        ok = _wait_state(services, True, a.timeout, not_pids=old)
        if not ok: print(_why(services), file=sys.stderr)
        return 0 if ok else 1
    print(f'unknown verb: {v}', file=sys.stderr); return 2


if __name__ == '__main__':
    sys.exit(main())
