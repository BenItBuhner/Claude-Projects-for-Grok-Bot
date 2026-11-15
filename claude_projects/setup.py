#!/usr/bin/env python3
"""claude-projects setup - the guided setup flow, verification and upkeep, driven by an agent.

Every command prints one JSON object (with --json) or a short human summary. The agent loop is:

    claude-projects --json setup status        -> {state, next_action, ask_user, ...}
    run next_action; when ask_user is set, ask the user exactly that and wait for the answer; repeat until state == ready

Commands: status, preflight, choose, install, start, signin start|wait, check-beta, connect, verify, viewport on|off,
tidy, reconfigure, mark, move, uninstall. Only the standard library is used, so `preflight` and `status` run before the
installer has created the virtualenv. Facts about the app come from `claude-projects --json status|probe-projects|...`.
Progress lives in <state_dir>/setup.json (no secrets). Nothing here prints a token, password or cookie: secrets are
referred to by path only.
"""
import os, sys, re, json, time, shutil, socket, pathlib, platform, subprocess, datetime, argparse, getpass, hashlib, secrets

REPO = pathlib.Path(__file__).resolve().parent.parent   # the checkout
_PKG = REPO / 'claude_projects'   # run as a file: import from the checkout, not from inside the package
sys.path[:] = [p for p in sys.path if pathlib.Path(p or '.').resolve() != _PKG]
sys.path.insert(0, str(REPO))
from claude_projects import config as cfg_mod  # noqa: E402  (stdlib only)
from claude_projects.origin import ORIGIN_PREFIX  # noqa: E402

CFG = cfg_mod.CFG
CONFIG_DIR = cfg_mod.CONFIG_DIR
CONFIG_FILE = cfg_mod.CONFIG_FILE
STATE_DIR = pathlib.Path(CFG['state_dir'])
STATE_FILE = STATE_DIR / 'setup.json'
UNIT_DIR = pathlib.Path(os.environ.get('XDG_CONFIG_HOME') or (pathlib.Path.home() / '.config')) / 'systemd' / 'user'
LEGACY_MCP_UNIT = 'claude-projects-mcp'   # the hand-built layout from before install.sh (one unit + venv/)
UNIT_PREFIX = 'claude-projects'   # install.sh's units: claude-projects-xvfb, -wm, -keyring, -desktop, -vnc, -novnc, -mcp, -refresh


def U(name): return f'{UNIT_PREFIX}-{name}'
def core_units(): return [U('xvfb'), U('wm'), U('keyring'), U('desktop')]
def viewport_units(): return [U('vnc'), U('novnc')]
def mcp_unit(): return U('mcp')
def all_units(): return core_units() + viewport_units() + [mcp_unit()]
PUBLIC_REPO = 'https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot'
EXPECTED_TOOLS = 25
CLAUDE_KEY_URL = 'https://downloads.claude.ai/claude-desktop/key.asc'
STATES = ['start', 'preflight_failed', 'installing', 'needs_sudo', 'installed_unhealthy', 'awaiting_signin',
          'close_viewer', 'beta_check', 'app_outdated', 'no_projects_beta', 'connecting', 'verifying', 'ready']

HOST_QUESTION = (
    "Which computer should run this? It has to stay on all the time, because your Projects are reached through the "
    "Claude app running on it. I recommend an always-on machine of yours: a Linux server, mini PC or desktop that never "
    "sleeps is best, and an always-on Mac or Windows PC works too (I set up a small Linux runtime on it). If you don't "
    "have one, I can run it on my own box instead, but that's a fallback: it only works while the box is up.")

BETA_MESSAGES = {
    'has_projects': None,
    'no_projects': (
        "You're signed in, but this Claude account doesn't have Claude Code Projects yet (it's a beta). The plugin can't "
        "do anything without it. If another account of yours has Projects, I can switch the sign-in. Otherwise "
        "everything stays installed and idle, and you can ask me to check again any time."),
    'app_outdated': (
        "The Claude app on that computer is out of date and doesn't show Projects. I'd like to update it with one apt "
        "command (it may need your sudo password), then check again."),
    'not_signed_in': "The Claude app isn't signed in yet, so I can't check for Projects.",
    'app_unreachable': "I can't reach the Claude app on that computer right now, so I can't check for Projects yet. I'll look at why.",
    'unknown': (
        "I couldn't tell whether this account has Claude Code Projects: the app showed some signs of it but not all. "
        "I saved a screenshot of the app; can you look at it, or open the viewport and tell me whether you see "
        "\"Projects\" in the sidebar?"),
}

# ---------------------------------------------------------------- small helpers (RUN and CLI are swapped out in tests)

def _now(): return datetime.datetime.now(datetime.timezone.utc)
def _parse_iso(t):
    try: return datetime.datetime.strptime(t, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception: return 0.0
def _iso(t=None): return (t or _now()).strftime('%Y-%m-%dT%H:%M:%SZ')


def _run(cmd, timeout=60, env=None, input=None):
    """Run a command; never raises. Returns (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, input=input)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError: return 127, '', f'{cmd[0]}: not found'
    except subprocess.TimeoutExpired: return 124, '', f'{cmd[0]}: timed out after {timeout}s'
    except Exception as ex: return 1, '', str(ex)


RUN = _run


def _cli(args, timeout=180):
    """`claude-projects --json <args>` -> dict. Errors come back as {'error': ..., 'message': ...}."""
    rc, out, err = RUN([str(REPO / 'claude-projects'), '--json'] + list(args), timeout=timeout)
    try:
        res = json.loads((out or '').strip().splitlines()[-1]) if (out or '').strip() else None
    except Exception: res = None
    if isinstance(res, dict): return res
    return {'error': 'ToolError', 'message': (err or out or f'claude-projects exited {rc}').strip()[-400:], 'exit_code': rc}


CLI = _cli


def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except Exception: return {}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try: os.chmod(STATE_DIR, 0o700)
    except Exception: pass
    st['updated_at'] = _iso()
    tmp = STATE_FILE.with_suffix('.tmp'); tmp.write_text(json.dumps(st, indent=2) + '\n'); tmp.replace(STATE_FILE)
    return st


def _event(st, what, **kw):
    h = st.setdefault('history', []); h.append(dict({'at': _iso(), 'event': what}, **kw)); del h[:-40]


def check(cid, status, detail, fix=None, **extra):
    d = {'id': cid, 'status': status, 'detail': detail}
    if fix: d['fix'] = fix
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


def overall(checks):
    s = {c['status'] for c in checks}
    return 'fail' if 'fail' in s else 'warn' if 'warn' in s else 'pass'


def as_root(cmd):
    """How the agent runs a root command on this host. In the Windows distro the claude user has no sudo on purpose: root
    steps go through `wsl.exe -u root` from Windows."""
    if detect_platform().get('wsl') or (load_state().get('host') or {}).get('class') == 'windows_runtime':
        return f"wsl.exe -d {os.environ.get('WSL_DISTRO_NAME') or 'ClaudeProjects'} -u root -- bash -c '{cmd}'   (from Windows)"
    if os.geteuid() != 0 and not shutil.which('sudo'):   # e.g. a container: someone with root runs it (docker exec -u root ...)
        return f'as root: bash -c {cmd!r}' if ('&&' in cmd or ';' in cmd) else f'as root: {cmd}'
    return f'sudo bash -c {cmd!r}' if ('&&' in cmd or ';' in cmd) else f'sudo {cmd}'


def service_mode():
    """'systemd' (user units) or 'process' (claude_projects/proc.py: hosts without user systemd, such as a container on tini)."""
    return (CFG.get('service_mode') or 'systemd').strip()


def http_enabled(): return CFG.get('mcp_http', True) is not False


def _systemctl_user(*args, timeout=30):
    if service_mode() == 'process':
        return RUN([*cfg_mod.module_argv('claude_projects.proc'), *args], timeout=timeout)
    return RUN(['systemctl', '--user', *args], timeout=timeout)


def unit_log_hint(u):
    if service_mode() == 'process': return f'tail -n 50 {STATE_DIR}/proc/logs/{u}.log'
    return f'journalctl --user -u {u} -n 50'


def venv_python():
    for p in (REPO / '.venv' / 'bin' / 'python', REPO / 'venv' / 'bin' / 'python'):
        if p.exists(): return p
    return pathlib.Path(sys.executable)


def unit_active(u): return (_systemctl_user('is-active', f'{u}.service')[1] or '').strip() == 'active'


def unit_enabled(u, kind='service'): return (_systemctl_user('is-enabled', f'{u}.{kind}')[1] or '').strip() == 'enabled'


def _port_open(port, host='127.0.0.1'):
    try:
        with socket.create_connection((host, int(port)), timeout=1.0): return True
    except OSError: return False


def _display_n(): return cfg_mod.display_number(CFG.get('display') or ':99')


def ports():
    n = _display_n()
    return {'vnc': 5900 + n, 'novnc': 6080 + n, 'mcp': int(CFG.get('mcp_port') or 8791)}


def layout():
    """'standard' (install.sh: .venv + claude-projects-* units), 'legacy' (the hand-built layout: venv/ and one
    claude-projects-mcp.service, with the display and the app started some other way) or None."""
    if (REPO / '.venv' / 'bin' / 'python').exists() and CONFIG_FILE.is_file() and all((UNIT_DIR / f'{u}.service').exists() for u in core_units()):
        return 'standard'
    legacy_unit = (UNIT_DIR / f'{LEGACY_MCP_UNIT}.service').exists() or (service_mode() == 'systemd' and unit_active(LEGACY_MCP_UNIT))
    if legacy_unit or ((REPO / 'venv' / 'bin' / 'python').exists() and CONFIG_FILE.is_file()):
        return 'legacy'
    return None


def installed(): return layout() is not None


def viewport_mode():
    """'on-demand' (what install.sh writes for new installs: the noVNC viewport is only on during sign-in) or 'always'
    (older installs without the key keep their viewport as it was)."""
    return (CFG.get('viewport_mode') or 'always').strip()


# ---------------------------------------------------------------- platform facts and host class

def detect_platform():
    sysname = platform.system().lower()
    f = {'os': {'linux': 'linux', 'darwin': 'macos', 'windows': 'windows'}.get(sysname, sysname or 'unknown'),
         'arch': platform.machine().lower(), 'python': platform.python_version(), 'user': getpass.getuser(),
         'hostname': socket.gethostname()}
    if f['os'] == 'linux':
        osr = {}
        try:
            for ln in pathlib.Path('/etc/os-release').read_text().splitlines():
                if '=' in ln: k, v = ln.split('=', 1); osr[k] = v.strip().strip('"')
        except Exception: pass
        f.update(distro_id=osr.get('ID', ''), distro_like=osr.get('ID_LIKE', ''), version_id=osr.get('VERSION_ID', ''),
                 pretty=osr.get('PRETTY_NAME', ''))
        try: f['wsl'] = 'microsoft' in pathlib.Path('/proc/version').read_text().lower()
        except Exception: f['wsl'] = False
        f['lima'] = pathlib.Path('/etc/lima-boot.conf').exists() or f['hostname'].startswith('lima-')
        f['pid1'] = _pid1(); cf = _container_facts(); f['container'] = _in_container(cf)
        # a host without systemd is how the Grok Bot box runs; CLAUDE_PROJECTS_HOST_KIND=box (or --host-kind box) says so outright
        reason = 'CLAUDE_PROJECTS_HOST_KIND=box' if os.environ.get('CLAUDE_PROJECTS_HOST_KIND') == 'box' else box_reason(dict(cf, pid1=f['pid1'], wsl=f['wsl'], lima=f['lima']))
        f['grok_bot_box'] = bool(reason)
        if reason: f['box_reason'] = reason
        f['laptop'] = _is_laptop()
    return f


def _pid1():
    try: return pathlib.Path('/proc/1/comm').read_text().strip() or 'unknown'
    except Exception: return 'unknown'


def _container_facts():
    """What this host says about itself. Some sandboxes (the Grok Bot box among them) show none of the usual container
    files: there only systemd-detect-virt ('container-other') and the missing /run/systemd/system give it away."""
    try: cg = pathlib.Path('/proc/1/cgroup').read_text()
    except Exception: cg = ''
    virt = ''
    if shutil.which('systemd-detect-virt'):
        try: virt = subprocess.run(['systemd-detect-virt', '--container'], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception: virt = ''
    return {'dockerenv': pathlib.Path('/.dockerenv').exists(), 'containerenv': pathlib.Path('/run/.containerenv').exists(),
            'container_env': os.environ.get('container') or '', 'cgroup': cg, 'detect_virt': virt,
            'run_systemd': pathlib.Path('/run/systemd/system').is_dir()}


def _in_container(cf):
    return bool(cf.get('dockerenv') or cf.get('containerenv') or cf.get('container_env')
                or re.search(r'docker|kubepods|containerd|libpod|lxc', cf.get('cgroup') or '')
                or (cf.get('detect_virt') or 'none') != 'none')


def box_reason(cf):
    """Pure: facts -> why this looks like the Grok Bot box (a host without systemd), or None."""
    if cf.get('pid1') == 'systemd' or cf.get('run_systemd') or cf.get('wsl') or cf.get('lima'): return None
    if _in_container(cf):
        how = f"systemd-detect-virt: {cf['detect_virt']}" if (cf.get('detect_virt') or 'none') != 'none' else 'container markers'
        return f"a container without systemd (PID 1 is {cf.get('pid1')}; {how})"
    return f"no systemd running (PID 1 is {cf.get('pid1')}, no /run/systemd/system)"


def _is_laptop():
    try:
        if any(p.name.startswith('BAT') for p in pathlib.Path('/sys/class/power_supply').iterdir()): return True
    except Exception: pass
    try: return pathlib.Path('/sys/class/dmi/id/chassis_type').read_text().strip() in ('8', '9', '10', '14', '31', '32')
    except Exception: return False


def classify_host(f, host_kind='auto'):
    """-> (host_class, rank). Lower rank = better. Classes:
    always_on_linux (recommended), macos_runtime / windows_runtime (Linux runtime on an always-on Mac / PC),
    laptop (works only while awake), grok_bot_box (fallback), unsupported."""
    if host_kind and host_kind != 'auto':
        return {'local': ('always_on_linux', 0), 'box': ('grok_bot_box', 3), 'macos-vm': ('macos_runtime', 1),
                'windows-wsl': ('windows_runtime', 1), 'laptop': ('laptop', 2)}.get(host_kind, ('unsupported', 9))
    if f['os'] == 'macos': return 'macos_runtime', 1
    if f['os'] == 'windows': return 'windows_runtime', 1
    if f['os'] != 'linux': return 'unsupported', 9
    if f.get('grok_bot_box'): return 'grok_bot_box', 3
    if f.get('wsl'): return 'windows_runtime', 1
    if f.get('lima'): return 'macos_runtime', 1
    if f.get('laptop'): return 'laptop', 2
    return 'always_on_linux', 0


def _ver_tuple(v):
    try: return tuple(int(x) for x in re.findall(r'\d+', v)[:2])
    except Exception: return ()


def _mem_available_mb():
    try:
        for ln in pathlib.Path('/proc/meminfo').read_text().splitlines():
            if ln.startswith('MemAvailable:'): return int(ln.split()[1]) // 1024
    except Exception: return None


def free_display():
    for n in range(99, 80, -1):
        if not pathlib.Path(f'/tmp/.X11-unix/X{n}').exists() and not _port_open(6080 + n) and not _port_open(5900 + n): return f':{n}'
    return None


def free_port(start=8791):
    for p in range(max(1024, start), min(65535, start + 200)):
        if not _port_open(p): return p
    return None


def linux_checks(f, host_class):
    out = []
    did, like, ver = f.get('distro_id', ''), f.get('distro_like', ''), _ver_tuple(f.get('version_id', ''))
    if did == 'ubuntu': ok = ver >= (22, 4)
    elif did == 'debian': ok = ver >= (12,)
    else: ok = None
    if ok: out.append(check('os', 'pass', f.get('pretty') or did))
    elif ok is None and ('debian' in like or 'ubuntu' in like):
        out.append(check('os', 'warn', f"{f.get('pretty') or did}: Debian-based but not Ubuntu or Debian itself; the Claude apt package usually works"))
    else:
        out.append(check('os', 'fail', f"{f.get('pretty') or did or 'unknown Linux'}: the Claude desktop app for Linux ships for Ubuntu 22.04+ and Debian 12+",
                         fix='Use an Ubuntu 22.04+ or Debian 12+ machine, or a Mac or Windows PC with the Linux runtime'))
    out.append(check('arch', 'pass' if f['arch'] in ('x86_64', 'amd64', 'aarch64', 'arm64') else 'fail', f['arch'],
                     fix=None if f['arch'] in ('x86_64', 'amd64', 'aarch64', 'arm64') else 'The Claude app ships for x86_64 and arm64 only'))
    pv = tuple(int(x) for x in f['python'].split('.')[:2])
    out.append(check('python', 'pass' if pv >= (3, 10) else 'fail', f['python'], fix=None if pv >= (3, 10) else 'Install python3 3.10 or newer'))
    rc, _, err = RUN(['systemctl', '--user', 'show-environment'], timeout=10)
    mode = 'systemd'
    if rc == 0: out.append(check('systemd_user', 'pass', 'user systemd is running', service_mode='systemd'))
    elif f.get('pid1') not in ('systemd', None) and not f.get('wsl'):
        mode = 'process'
        out.append(check('systemd_user', 'pass', f"no systemd here (PID 1 is {f.get('pid1')}): the plugin runs its own process supervisor (claude_projects/proc.py)",
                         service_mode='process'))
    else:
        fix = ("WSL: put '[boot]\\nsystemd=true' in /etc/wsl.conf, then 'wsl --shutdown' and reopen" if f.get('wsl')
               else 'Log in once as this user (or run: sudo loginctl enable-linger $USER) so the user systemd manager starts')
        out.append(check('systemd_user', 'fail', f'user systemd not reachable: {(err or "").strip()[:120]}', fix=fix))
    if mode == 'process':
        out.append(check('linger', 'pass', 'not used without systemd: the MCP server starts the supervisor when the agent connects'))
    else:
        rc, o, _ = RUN(['loginctl', 'show-user', f['user'], '-p', 'Linger', '--value'], timeout=10)
        if (o or '').strip() == 'yes': out.append(check('linger', 'pass', 'services keep running without a login session'))
        else: out.append(check('linger', 'warn', 'linger is off: services stop when the user logs out', fix='install.sh turns it on (loginctl enable-linger)'))
    if os.geteuid() == 0:
        out.append(check('user', 'warn', 'running as root; the plugin is meant to run as a regular user', fix='Create or use a regular user with sudo'))
    rc, _, _ = RUN(['sudo', '-n', 'true'], timeout=10)
    if host_class == 'windows_runtime' and rc != 0 and os.geteuid() != 0:
        # the Windows distro's claude user has no sudo by design: hosts/windows/setup.ps1 runs the root steps itself
        out.append(check('sudo', 'pass', 'root steps run from Windows with wsl -u root; no sudo needed',
                         command=as_root(f'{REPO}/install.sh --apt-only')))
    else: out.append(check('sudo', 'pass' if rc == 0 or os.geteuid() == 0 else 'warn',
                     'passwordless sudo available' if rc == 0 or os.geteuid() == 0 else
                     ('sudo needs a password' if shutil.which('sudo') else 'no sudo here: the package step needs someone with root'),
                     fix=None if rc == 0 or os.geteuid() == 0 else f'The user pastes one line: {as_root(f"{REPO}/install.sh --apt-only")}'))
    mem = _mem_available_mb()
    if mem is not None:
        out.append(check('memory', 'pass' if mem >= 2048 else 'warn' if mem >= 1200 else 'fail', f'{mem} MB available',
                         fix=None if mem >= 2048 else 'The app plus the plugin want about 2 GB free'))
    try:
        free_gb = shutil.disk_usage(str(pathlib.Path.home())).free / 2**30
        out.append(check('disk', 'pass' if free_gb >= 3 else 'fail', f'{free_gb:.1f} GB free in the home directory',
                         fix=None if free_gb >= 3 else 'Free at least 3 GB'))
    except Exception: pass
    disp = CFG.get('display') or ':99'; n = _display_n(); ours = unit_active(U('xvfb'))
    if pathlib.Path(f'/tmp/.X11-unix/X{n}').exists() and not ours:
        alt = free_display()
        out.append(check('display', 'warn', f'display {disp} is already in use', fix=f'install with --display {alt}' if alt else 'pick a free display', suggested_display=alt))
    else: out.append(check('display', 'pass', f'display {disp} is free' if not ours else f'display {disp} is ours'))
    want = ports()
    if host_class == 'grok_bot_box' or not http_enabled(): want.pop('mcp')   # stdio only: no HTTP port needed
    busy = {k: p for k, p in want.items() if _port_open(p) and not (unit_active(mcp_unit()) if k == 'mcp' else unit_active(U('vnc') if k == 'vnc' else U('novnc')))}
    fixes = []
    if 'vnc' in busy or 'novnc' in busy: fixes.append(f"install with --display {free_display() or ':<free>'} (moves the VNC/noVNC ports)")
    if 'mcp' in busy: fixes.append(f'install with --mcp-port {free_port(busy["mcp"] + 1) or "<free port>"}, or --no-http (stdio only)')
    out.append(check('ports', 'fail' if busy else 'pass', ('in use by something else: ' + ', '.join(f'{k} {p}' for k, p in busy.items())) if busy
                     else 'free' + ('' if 'mcp' in want else ' (no HTTP port needed: stdio)'),
                     fix='; '.join(fixes) or None, suggested_mcp_port=free_port(busy['mcp'] + 1) if 'mcp' in busy else None))
    if not shutil.which('claude-desktop'):
        rc, _, err = RUN(['curl', '-fsSI', '--max-time', '15', CLAUDE_KEY_URL], timeout=25)
        out.append(check('claude_download', 'pass' if rc == 0 else 'fail',
                         "Anthropic's apt repository is reachable" if rc == 0 else f"can't reach downloads.claude.ai ({(err or '').strip()[:100]})",
                         fix=None if rc == 0 else 'Allow outbound HTTPS to downloads.claude.ai (proxy or firewall), then re-run preflight'))
    else:
        out.append(check('claude_download', 'pass', 'the Claude desktop app is already installed'))
    out += always_on_checks(f, host_class)
    return out


def always_on_checks(f, host_class):
    out = []
    if host_class == 'grok_bot_box':
        why = f" (detected: {f['box_reason']})" if f.get('box_reason') else ''
        out.append(check('always_on', 'warn', f"this is the Grok Bot box{why}: it's a fallback host and the plugin only works while the box is up",
                         fix='Prefer an always-on machine of the user; keep the box if they have none'))
    elif host_class == 'laptop':
        out.append(check('always_on', 'warn', 'this looks like a laptop: it only works while the laptop is awake and on power',
                         fix='Keep it plugged in with sleep turned off, or prefer an always-on machine'))
    else:
        rc, o, _ = RUN(['systemctl', 'is-enabled', 'suspend.target'], timeout=10)
        # a desktop login screen means a machine people use (and that may sleep); openbox's own xsession file doesn't count
        gui = (RUN(['systemctl', 'is-active', 'display-manager.service'], timeout=10)[1] or '').strip() == 'active'
        if (o or '').strip() != 'masked' and gui and not f.get('wsl') and not f.get('lima'):
            out.append(check('sleep', 'warn', 'this machine can still suspend, which pauses the plugin',
                             fix='sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target (ask the user first)'))
        else: out.append(check('sleep', 'pass', 'no suspend expected'))
    return out


def runtime_checks(f):
    """Native macOS / Windows: the plugin runs in a small Linux runtime there (Lima VM / WSL2)."""
    out = [check('os', 'pass', f"{f['os']} {platform.release()}: supported through a Linux runtime on this machine")]
    if f['os'] == 'macos':
        rc, o, _ = RUN(['sw_vers', '-productVersion'], timeout=10)
        v = _ver_tuple(o or '')
        out.append(check('macos_version', 'pass' if v >= (13,) else 'fail', (o or '?').strip(), fix=None if v >= (13,) else 'macOS 13 or newer is needed for the Linux VM'))
        out.append(check('lima', 'pass' if shutil.which('limactl') else 'warn', 'Lima installed' if shutil.which('limactl') else 'Lima not installed yet',
                         fix=None if shutil.which('limactl') else 'hosts/macos/setup.sh installs it with Homebrew'))
        rc, o, _ = RUN(['pmset', '-g', 'batt'], timeout=10)
        if 'InternalBattery' in (o or ''):
            out.append(check('always_on', 'warn', "this Mac is a laptop: it only works while it's awake and on power", fix='Keep it plugged in; hosts/macos/setup.sh can turn off sleep on power'))
        rc, o, _ = RUN(['pmset', '-g'], timeout=10)
        m = re.search(r'^\s*sleep\s+(\d+)', o or '', re.M)
        out.append(check('sleep', 'pass' if m and m.group(1) == '0' else 'warn', f"system sleep after {m.group(1) if m else '?'} min",
                         fix=None if m and m.group(1) == '0' else 'sudo pmset -c sleep 0 (ask the user first)'))
        out.append(check('next', 'pass', 'run hosts/macos/setup.sh on this Mac', command='bash hosts/macos/setup.sh'))
    else:
        rc, o, _ = RUN(['wsl.exe', '--status'], timeout=20)
        out.append(check('wsl', 'pass' if rc == 0 else 'warn', 'WSL is available' if rc == 0 else 'WSL is not installed yet',
                         fix=None if rc == 0 else 'hosts/windows/setup.ps1 installs it (needs an administrator PowerShell once)'))
        rc, o, _ = RUN(['powershell', '-NoProfile', '-Command', '(Get-CimInstance Win32_Battery | Measure-Object).Count'], timeout=30)
        if (o or '').strip() not in ('', '0'):
            out.append(check('always_on', 'warn', "this PC is a laptop: it only works while it's awake and on power", fix='Keep it plugged in with sleep off on AC'))
        out.append(check('next', 'pass', 'run hosts/windows/setup.ps1 in PowerShell on this PC',
                         command='powershell -ExecutionPolicy Bypass -File hosts\\windows\\setup.ps1'))
    return out


HOST_NOTES = {
    'always_on_linux': 'Recommended: an always-on Linux machine.',
    'macos_runtime': 'Supported: an always-on Mac, through a small Linux VM (Lima) that starts at login.',
    'windows_runtime': 'Supported: an always-on Windows PC, through WSL2 that starts at login.',
    'laptop': 'Works, with a warning: only while the laptop is awake and on power.',
    'grok_bot_box': 'Fallback: the Grok Bot box. It works only while the box is up; an always-on machine of the user is better.',
    'unsupported': 'Not supported.',
}


def cmd_preflight(a):
    f = detect_platform(); hc, rank = classify_host(f, a.host_kind)
    checks = runtime_checks(f) if f['os'] in ('macos', 'windows') else (linux_checks(f, hc) if f['os'] == 'linux' else
             [check('os', 'fail', f"{f['os']}: not supported", fix='Use Linux, macOS or Windows')])
    res = {'command': 'preflight', 'result': overall(checks), 'host_class': hc, 'host_rank': rank,
           'host_note': HOST_NOTES.get(hc), 'platform': f, 'checks': checks,
           'service_mode': next((c.get('service_mode') for c in checks if c['id'] == 'systemd_user'), None)}
    if f['os'] in ('macos', 'windows'): res['next_action'] = next(c['command'] for c in checks if c['id'] == 'next')
    st = load_state(); st['preflight'] = {'at': _iso(), 'result': res['result'], 'host_class': hc, 'service_mode': res['service_mode'],
                                         'failed': [c['id'] for c in checks if c['status'] == 'fail']}
    host = st.setdefault('host', {})
    # auto-detection may only fill in or sharpen a guess, never overwrite the user's own choice
    if not host.get('chosen_at'): host['class'] = hc
    _event(st, 'preflight', result=res['result']); save_state(st)
    return res


def cmd_choose(a):
    st = load_state()
    st['host'] = {'class': a.host_class, 'label': a.label or st.get('host', {}).get('label'), 'chosen_at': _iso()}
    _event(st, 'host_chosen', host_class=a.host_class); save_state(st)
    return {'command': 'choose', 'host': st['host'], 'note': HOST_NOTES.get(a.host_class)}


# ---------------------------------------------------------------- install / reconfigure

def _missing_apt():
    rc, _, _ = RUN(['dpkg-query', '-W', 'xvfb', 'x11vnc', 'novnc', 'xdotool', 'at-spi2-core', 'claude-desktop'], timeout=20)
    return rc != 0


def _install_args(a, cfg):
    args = []
    for flag, key in (('--bot-name', 'bot_name'), ('--operator-name', 'operator_name'), ('--timezone', 'timezone'),
                      ('--display', 'display'), ('--screen', 'screen')):
        v = getattr(a, key, None) or cfg.get(key)
        if key == 'operator_name' and v == 'the user' and not getattr(a, key, None): v = None
        if v: args += [flag, str(v)]
    if getattr(a, 'claude_md', False): args.append('--claude-md')
    if getattr(a, 'mcp_port', None): args += ['--mcp-port', str(a.mcp_port)]
    if getattr(a, 'http', None) is True: args.append('--http')
    elif getattr(a, 'http', None) is False: args.append('--no-http')
    if getattr(a, 'service_mode', None): args += ['--service-mode', a.service_mode]
    return args


def _unit_hashes():
    out = {}
    for u in all_units() + [U('refresh')]:
        p = UNIT_DIR / f'{u}.service'
        out[u] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    return out


def _file_hash(p):
    try: return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
    except Exception: return None


def cmd_install(a):
    st = load_state()
    if layout() == 'legacy' and not getattr(a, 'migrate', False):
        return {'command': 'install', 'result': 'legacy_layout', 'state': 'installed', 'layout': 'legacy',
                'note': (f'This machine already runs the plugin in the older hand-built layout ({LEGACY_MCP_UNIT}.service, venv/). '
                         'It keeps working as it is: claude-projects setup status and verify understand it. Installing over it would start a '
                         'second display and app next to the running ones, so it needs --migrate, and only after the user agrees '
                         'to a short outage and a fresh sign-in.'),
                'next_action': 'claude-projects setup status'}
    for k in ('force', 'migrate', 'http', 'service_mode', 'mcp_port'): setattr(a, k, getattr(a, k, None))
    pf = cmd_preflight(argparse.Namespace(host_kind=getattr(a, 'host_kind', None) or 'auto'))
    if pf['result'] == 'fail' and not a.force:
        failed = [c for c in pf['checks'] if c['status'] == 'fail']
        return {'command': 'install', 'result': 'preflight_failed', 'state': 'preflight_failed', 'dry_run': bool(a.dry_run),
                'checks': failed, 'next_action': 'fix the failed checks (each has a fix), then claude-projects setup install'}
    hc = (load_state().get('host') or {}).get('class') or pf['host_class']
    if hc == 'grok_bot_box' and a.http is None and 'mcp_http' not in _file_cfg():
        a.http = False   # the agent on the box connects over stdio; no HTTP server, no port to collide with
    if pf.get('service_mode') == 'process' and not a.service_mode: a.service_mode = 'process'
    st = load_state()
    no_root = _missing_apt() and RUN(['sudo', '-n', 'true'], timeout=10)[0] != 0 and os.geteuid() != 0 and not a.no_apt
    if no_root and not a.dry_run:
        st['needs_sudo'] = _iso(); _event(st, 'needs_sudo'); save_state(st)
        line = as_root(f"{REPO}/install.sh --apt-only")
        ask = (f'Please paste this one line into a terminal on {socket.gethostname()} (it asks for your password and only '
               f'installs packages): {line}') if shutil.which('sudo') else (
               f'The system packages need root once, and this user has no sudo on {socket.gethostname()}. Whoever has root '
               f'there runs (it only installs packages): {line.replace("as root: ", "")}')
        return {'command': 'install', 'result': 'needs_sudo', 'state': 'needs_sudo', 'ask_user': ask, 'then': 'claude-projects setup install --no-apt'}
    args = [str(REPO / 'install.sh')] + _install_args(a, CFG) + (['--no-apt'] if a.no_apt or not _missing_apt() else [])
    if a.dry_run: args.append('--dry-run')
    log = STATE_DIR / 'logs' / 'install.log'; log.parent.mkdir(parents=True, exist_ok=True)
    rc, out, err = RUN(args, timeout=1800)
    log.write_text(f'$ {" ".join(args)}\n{out}\n{err}')
    st.pop('needs_sudo', None)
    _event(st, 'install', rc=rc, dry_run=bool(a.dry_run)); save_state(st)
    lines = (out or '').splitlines()
    i = next((k for k, ln in enumerate(lines) if 'Self-check' in ln), None)
    tail = [ln for ln in (lines[i + 1:] if i is not None else []) if ln.strip()][:12]
    tail = tail[:next((k for k, ln in enumerate(tail) if not ln.startswith('    ')), len(tail))]
    return {'command': 'install', 'result': 'ok' if rc == 0 else 'failed', 'exit_code': rc, 'log': str(log), 'dry_run': bool(a.dry_run),
            'host_class': hc, 'service_mode': a.service_mode or 'systemd', 'http': a.http is not False,
            'preflight': {'result': pf['result'], 'warnings': [c['id'] for c in pf['checks'] if c['status'] == 'warn']},
            'self_check': [re.sub(r'\s+', ' ', ln.strip()) for ln in tail],
            'error_tail': (err or '').strip().splitlines()[-6:] if rc else None}


def _file_cfg():
    try: return json.loads(CONFIG_FILE.read_text())
    except Exception: return {}


def _current_screen():
    try:
        m = re.search(r'-screen 0 (\d+x\d+)x24', (UNIT_DIR / f"{U('xvfb')}.service").read_text())
        return m.group(1) if m else None
    except Exception: return None


def cmd_reconfigure(a):
    if not installed(): return {'command': 'reconfigure', 'result': 'not_installed', 'next_action': 'claude-projects setup install'}
    if layout() == 'legacy':
        return {'command': 'reconfigure', 'result': 'legacy_layout',
                'note': f'The older hand-built layout ({LEGACY_MCP_UNIT}.service) is configured by hand: edit {CONFIG_FILE}, then restart {LEGACY_MCP_UNIT} (ask the user first).'}
    before, cfg_before = _unit_hashes(), _file_hash(CONFIG_FILE)
    if not a.screen: a.screen = _current_screen()
    args = [str(REPO / 'install.sh'), '--no-apt'] + _install_args(a, CFG)
    rc, out, err = RUN(args, timeout=900)
    after, cfg_after = _unit_hashes(), _file_hash(CONFIG_FILE)
    changed = [u for u in after if after[u] != before.get(u)]
    restart = [u for u in changed if u != U('desktop') and u in all_units()]
    if cfg_after != cfg_before and mcp_unit() not in restart: restart.append(mcp_unit())
    held = []
    if U('desktop') in changed:
        if a.restart_app: restart.append(U('desktop'))
        else: held.append(U('desktop'))
    for u in restart:
        if unit_enabled(u) or unit_active(u): _systemctl_user('restart', f'{u}.service')
    vp = _viewport_auto_off()
    st = load_state(); _event(st, 'reconfigure', changed=changed); save_state(st)
    return {'command': 'reconfigure', 'result': 'ok' if rc == 0 else 'failed', 'changed_units': changed, 'restarted': restart,
            'needs_app_restart': held or None, 'viewport': vp,
            'note': ('The Claude app keeps running with its old settings until you pass --restart-app (ask the user first: '
                     'it interrupts any UI action in progress).') if held else None,
            'error_tail': (err or '').strip().splitlines()[-6:] if rc else None}


# ---------------------------------------------------------------- viewport + sign-in

def viewport(on, force=False):
    if not on:
        st = CLI(['status'], 30)
        if (st.get('vnc_viewers') or 0) > 0 and not force:
            return {'viewport': 'on', 'refused': 'a viewer is connected (someone is watching); ask them to close the tab first'}
    errors = []
    for u in viewport_units():
        rc, o, e = _systemctl_user('enable' if on else 'disable', '--now', f'{u}.service', timeout=45)
        if rc != 0: errors.append(f'{u}: ' + ((e or o or '').strip().splitlines() or [f'exit {rc}'])[-1][:200])
        if not on: _systemctl_user('reset-failed', f'{u}.service')   # x11vnc exits non-zero on SIGTERM
    units = {}
    for _ in range(10 if on else 1):
        units = {u: 'active' if unit_active(u) else 'inactive' for u in viewport_units()}
        if not on or all(v == 'active' for v in units.values()): break
        time.sleep(0.8)
    # a start error counts even if the unit looks active right now: a crashing unit is "active" between restarts
    ok = (all(v == 'active' for v in units.values()) and not errors) if on else not any(v == 'active' for v in units.values())
    res = {'viewport': ('on' if on else 'off') if ok else 'failed', 'units': units}
    if errors or not ok:
        res['errors'] = errors or None
        bad = [u for u, v in units.items() if (v != 'active') == on or any(e.startswith(u + ':') for e in errors)]
        res['fix'] = '; '.join(unit_log_hint(u) for u in bad) or None
    return res


def _viewport_auto_off():
    if viewport_mode() != 'on-demand': return None
    st = CLI(['status'], 30)
    if st.get('signed_in') and not st.get('vnc_viewers') and any(unit_active(u) for u in viewport_units()):
        return viewport(False)
    return None


def cmd_viewport(a):
    return dict({'command': 'viewport'}, **viewport(a.state == 'on', a.force))


def signin_links(public_url=None, password=None):
    p = ports()['novnc']; bind = CFG.get('viewport_bind') or '127.0.0.1'
    # the one-time password goes in the fragment: browsers never send it, so websockify's request log never has it
    path = '/vnc.html?autoconnect=1&resize=scale' + (f'#password={password}' if password else '')
    hc = load_state().get('host', {}).get('class')
    links = {}
    if hc != 'grok_bot_box':   # nobody sits at the box, and its localhost isn't the user's
        links['on_this_machine'] = f'http://{bind}:{p}{path}'
        links['from_another_computer'] = f'ssh -N -L {p}:127.0.0.1:{p} {getpass.getuser()}@{socket.gethostname()}   then open http://127.0.0.1:{p}{path}'
    if hc == 'macos_runtime': links['on_the_mac'] = f'http://127.0.0.1:{p}{path}   (Lima forwards it to the Mac)'
    if hc == 'windows_runtime': links['on_the_pc'] = f'http://127.0.0.1:{p}{path}   (WSL forwards it to Windows)'
    if public_url: links = dict({'your_link': public_url.rstrip('/') + path}, **links)
    return links


# ---- a temporary tunnel for hosts nobody can reach (the Grok Bot box): cloudflared's quick tunnel, no account needed.
# It is opened only after the user agreed (signin start --tunnel), is password protected, and closes after the sign-in
# (or after an hour at the latest).
TUNNEL_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')


def _cloudflared():
    return shutil.which('cloudflared') or next((str(x) for x in (REPO / 'local' / 'cloudflared', pathlib.Path.home() / '.local' / 'bin' / 'cloudflared')
                                                 if x.is_file() and os.access(x, os.X_OK)), None)


def _spawn_tunnel(exe, port, log):
    lf = open(log, 'ab')
    pr = subprocess.Popen(['timeout', '3600', exe, 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{port}'],
                          stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
    lf.close(); return pr.pid


SPAWN_TUNNEL = _spawn_tunnel   # swapped out in tests


def start_tunnel(port, wait=45):
    exe = _cloudflared()
    if not exe:
        return {'ok': False, 'error': 'cloudflared is not installed',
                'fix': ('install cloudflared (a single binary from https://github.com/cloudflare/cloudflared/releases, for example to '
                        '~/.local/bin/cloudflared; ask the user first), then claude-projects setup signin start --tunnel')}
    log = STATE_DIR / 'logs' / 'tunnel.log'; log.parent.mkdir(parents=True, exist_ok=True)
    try: log.unlink()
    except FileNotFoundError: pass
    pid = SPAWN_TUNNEL(exe, port, log); t0 = time.time()
    while time.time() - t0 < wait:
        try: m = TUNNEL_RE.search(log.read_text(errors='replace'))
        except FileNotFoundError: m = None
        if m: return {'ok': True, 'url': m.group(0), 'pid': pid}
        time.sleep(0.5)
    stop_tunnel(pid)
    return {'ok': False, 'error': f'the tunnel did not report a URL within {wait} s (log: {log})', 'fix': 'check outbound HTTPS from this host, then retry'}


def stop_tunnel(pid=None):
    pid = pid or ((load_state().get('signin') or {}).get('tunnel') or {}).get('pid')
    if not pid: return False
    try: os.killpg(int(pid), 15); return True
    except Exception:
        try: os.kill(int(pid), 15); return True
        except Exception: return False


def rotate_viewport_password():
    """A fresh 8-character viewport password (VNC's limit). Used when the password has to travel through chat to a user
    without a shell on this host: it is replaced again as soon as the sign-in is done, so the shared one dies with it."""
    pw = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789') for _ in range(8))
    rc, _, e = RUN(['x11vnc', '-storepasswd', pw, str(CONFIG_DIR / 'vncpass')], timeout=20)
    if rc != 0: return None, (e or '').strip()[:200] or f'x11vnc -storepasswd exited {rc}'
    txt = CONFIG_DIR / 'vncpass.txt'
    fd = os.open(str(txt), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh: fh.write(pw)
    os.chmod(txt, 0o600); os.chmod(CONFIG_DIR / 'vncpass', 0o600)
    if unit_active(U('vnc')): _systemctl_user('restart', U('vnc') + '.service', timeout=45)   # x11vnc reads -rfbauth at start
    return pw, None


def cmd_signin_start(a):
    st0 = CLI(['status'], 60)
    if st0.get('signed_in') and not a.switch:
        return {'command': 'signin start', 'result': 'already_signed_in', 'next_action': 'claude-projects setup check-beta'}
    br = signin_browser_check()
    if br['status'] == 'fail':
        return {'command': 'signin start', 'result': 'no_signin_browser', 'checks': [br], 'next_action': br['fix'] + ', then claude-projects setup signin start',
                'ask_user': f"The sign-in needs a web browser on {socket.gethostname()} and there isn't one I can use. May I install one? ({br['fix']})"}
    hc = load_state().get('host', {}).get('class')
    box = hc == 'grok_bot_box'
    a.tunnel = getattr(a, 'tunnel', False); a.share_password = getattr(a, 'share_password', False)
    if box and not a.public_url and not a.tunnel:
        return {'command': 'signin start', 'result': 'needs_viewer_route', 'host_class': hc,
                'ask_user': ("To sign in, you need to see the Claude app running on my box, and nothing on the box is reachable from "
                             "your browser yet. I can open a temporary, password-protected link for it (a Cloudflare quick tunnel, "
                             "no account needed), which I close as soon as you're signed in. May I? If you already have a URL that "
                             "reaches this box's port " + str(ports()['novnc']) + ", send it instead."),
                'next_action': 'claude-projects setup signin start --tunnel   (after the user agrees), or claude-projects setup signin start --public-url <their URL>'}
    vp = viewport(True)
    if vp['viewport'] != 'on':
        return {'command': 'signin start', 'result': 'viewport_failed', 'viewport': vp,
                'next_action': (vp.get('fix') or 'claude-projects setup verify') + '; then claude-projects setup signin start'}
    share = bool(a.share_password or box)
    pw = None
    if share:
        pw, err = rotate_viewport_password()
        if not pw:
            return {'command': 'signin start', 'result': 'viewport_failed', 'error': f'could not set a one-time viewport password: {err}'}
    tunnel = None
    if a.tunnel:
        tunnel = start_tunnel(ports()['novnc'])
        if not tunnel['ok']:
            if share: rotate_viewport_password()
            return {'command': 'signin start', 'result': 'tunnel_failed', 'error': tunnel['error'], 'next_action': tunnel['fix'],
                    'ask_user': ("I can't open the temporary sign-in link yet, because the tool for it (cloudflared, one file from "
                                 "Cloudflare's GitHub releases) isn't on my box. May I install it?") if 'install' in tunnel['fix'] else None}
    links = signin_links(a.public_url or (tunnel or {}).get('url'), password=pw)
    pwf = CONFIG_DIR / 'vncpass.txt'
    st = load_state()
    st['signin'] = {'started_at': _iso(), 'switch': bool(a.switch), 'shared_password': share,
                    'tunnel': {'pid': tunnel['pid'], 'url': tunnel['url'], 'at': _iso()} if tunnel else None}
    _event(st, 'signin_start', tunnel=bool(tunnel), shared_password=share); save_state(st)
    first = ('In the Claude app, open the account menu, sign out, then sign in with the account that has Projects.' if a.switch
             else 'In the Claude app window, click "Get started" (older versions say "Sign in") and log in. The login page opens '
                  'in a browser inside the same window.')
    if share:
        steps = ['Open the link below in your browser. It already carries a one-time password, which stops working once you are signed in.',
                 first, 'When the Claude app shows your home screen, close the browser tab.']
    else:
        steps = ['Open the link below in your browser.',
                 f'Enter the viewport password. It is saved in {pwf} on {socket.gethostname()} (read it there, for example with: cat {pwf}).',
                 first, 'When the Claude app shows your home screen, close the browser tab.']
    link = next(iter(links.values()), None)
    res = {'command': 'signin start', 'result': 'awaiting_signin', 'viewport': vp, 'links': links,
           'password_file': None if share else str(pwf), 'one_time_password_in_link': share or None,
           'tunnel': (tunnel or {}).get('url'),
           'ask_user': 'Please sign in to Claude:\n' + '\n'.join(f'{i+1}. {s}' for i, s in enumerate(steps)) + '\nLink: ' + str(link),
           'if_stuck_on_claude_link': f"{REPO}/bin/claude-open-url 'claude://...'   (when the sign-in browser stops on a claude:// link)",
           'next_action': 'claude-projects setup signin wait'}
    if share: res['note'] = ('The link contains a one-time viewport password so a user without a shell here can open it. Send it only to '
                             'the user, in this conversation; claude-projects setup signin wait replaces the password when the sign-in is done.')
    return res


def signin_browser_check():
    rc, o, e = RUN([str(REPO / 'bin' / 'claude-signin-browser'), '--check'], timeout=15)
    if rc == 0: return check('signin_browser', 'pass', f'sign-in browser: {(o or "").strip()}')
    did = detect_platform().get('distro_id')
    fix = 'sudo snap install chromium' if did == 'ubuntu' else 'sudo apt-get install -y chromium (or firefox-esr)'
    why = (e or '').strip().split('no usable browser: ', 1)[-1].split(' (install', 1)[0] if e else ''
    return check('signin_browser', 'fail', f'no usable browser for the sign-in page ({why or "only snap stubs or nothing"})', fix=fix)


def _kill_signin_browser():
    prof = str(CONFIG_DIR / 'browser-profile')
    rc, o, _ = RUN(['pgrep', '-f', '--', prof], timeout=10)
    pids = [int(x) for x in (o or '').split() if x.isdigit() and int(x) != os.getpid()]
    for pid in pids:
        try: os.kill(pid, 15)
        except Exception: pass
    return pids


def cmd_signin_wait(a):
    t0 = time.time(); last = {}
    while time.time() - t0 < a.timeout:
        last = CLI(['status'], 60)
        if last.get('signed_in'): break
        time.sleep(a.poll)
    if not last.get('signed_in'):
        st = load_state(); _event(st, 'signin_timeout'); save_state(st)
        return {'command': 'signin wait', 'result': 'signin_timeout', 'waited_s': int(time.time() - t0), 'route': last.get('route'),
                'app': last.get('app'), 'next_action': 'claude-projects setup signin wait (again), or claude-projects setup signin start to resend the link'}
    t1 = time.time()
    while (last.get('vnc_viewers') or 0) > 0 and time.time() - t1 < a.close_timeout:
        time.sleep(a.poll); last = CLI(['status'], 60)
    if (last.get('vnc_viewers') or 0) > 0:
        return {'command': 'signin wait', 'result': 'close_viewer', 'signed_in': True,
                'ask_user': "You're signed in. Please close the viewport tab, so I can use the app.", 'next_action': 'claude-projects setup signin wait'}
    killed = _kill_signin_browser()
    si = load_state().get('signin') or {}
    closed_tunnel = stop_tunnel() if si.get('tunnel') else None
    rotated = rotate_viewport_password()[0] is not None if si.get('shared_password') else None   # the shared one stops working
    vp = viewport(False) if viewport_mode() == 'on-demand' or si.get('tunnel') else None
    st = load_state(); st['signin'] = dict(si, done_at=_iso(), tunnel=None); st.pop('beta', None)
    _event(st, 'signed_in'); save_state(st)
    return {'command': 'signin wait', 'result': 'signed_in', 'closed_signin_browser': len(killed), 'viewport': vp,
            'closed_tunnel': closed_tunnel, 'replaced_shared_password': rotated, 'next_action': 'claude-projects setup check-beta'}


# ---------------------------------------------------------------- Projects (beta) detection

def beta_verdict(p):
    """Pure: probe result -> has_projects | no_projects | app_outdated | not_signed_in | app_unreachable | unknown."""
    if not isinstance(p, dict): return 'unknown'
    if p.get('error'): return 'app_unreachable'
    if p.get('signed_in') is False: return 'not_signed_in'
    entry, route_ok = bool(p.get('sidebar_entry')), bool(p.get('browse_route_ok'))
    cached = bool(p.get('project_list_cached')) and p.get('project_list_query_status') != 'error'
    if entry and route_ok: return 'has_projects'
    if not entry and not route_ok:
        v = p.get('app_version') or {}
        if v.get('installed') and v.get('candidate') and v['installed'] != v['candidate']: return 'app_outdated'
        if cached and (p.get('project_count') or 0) > 0: return 'unknown'
        return 'no_projects'
    return 'unknown'


def cmd_check_beta(a):
    p = CLI(['probe-projects'] + (['--force'] if a.force else []), 240)
    v = beta_verdict(p)
    res = {'command': 'check-beta', 'verdict': v, 'has_projects': {'has_projects': True, 'no_projects': False}.get(v), 'probe': p}
    if v == 'unknown':
        shot = STATE_DIR / 'setup-beta-check.jpg'; s = CLI(['screenshot', str(shot)], 60)
        if s.get('file'): res['screenshot'] = s['file']
    if v == 'app_unreachable': res['next_action'] = unit_log_hint(U('desktop')) + '; claude-projects setup verify'
    if v == 'app_outdated': res['next_action'] = as_root('apt-get update && apt-get install --only-upgrade -y claude-desktop') + ', then claude-projects setup check-beta'
    if BETA_MESSAGES.get(v): res['tell_user'] = BETA_MESSAGES[v]
    # the 10-minute background refresh only makes sense when there are Projects to refresh
    if v == 'has_projects': _systemctl_user('enable', '--now', U('refresh') + '.timer'); res['refresh_timer'] = 'on'
    elif v == 'no_projects': _systemctl_user('disable', '--now', U('refresh') + '.timer'); res['refresh_timer'] = 'off (idle until the account has Projects)'
    st = load_state(); st['beta'] = {'verdict': v, 'at': _iso(), 'project_count': p.get('project_count')}
    _event(st, 'check_beta', verdict=v); save_state(st)
    return res


# ---------------------------------------------------------------- connecting the agent

def connect_options():
    py = venv_python(); tok = CFG.get('token_file')
    user, host = getpass.getuser(), socket.gethostname(); hc = load_state().get('host', {}).get('class')
    stdio = f'env PYTHONPATH={REPO} {py} -m claude_projects.mcp_server --stdio'
    opts = {
        'same_host': {'when': 'the agent runs on this machine (for example the Grok Bot box)', 'transport': 'stdio', 'command': stdio},
        'ssh': {'when': 'the agent can SSH to this machine', 'transport': 'stdio over ssh', 'command': f'ssh {user}@{host} {stdio}'},
        'tunnel': {'when': "the agent can't SSH here: use the user's own tunnel or VPN to 127.0.0.1:%d" % ports()['mcp'],
                   'transport': 'Streamable HTTP + bearer token',
                   'steps': [f'Point the tunnel at http://127.0.0.1:{ports()["mcp"]} and add its hostname to allowed_hosts in {CONFIG_FILE}',
                             'claude-projects setup reconfigure (restarts the MCP service with the new config)',
                             f'Copy the token file {tok} to the agent machine privately (chmod 600); never paste it in chat',
                             f'Register a stdio connector: {REPO}/bin/grok-bot-bridge https://<tunnel-host>/mcp <token-file>']},
    }
    if not http_enabled():
        opts['tunnel']['steps'].insert(0, 'The HTTP server is off on this host (stdio only). Turn it on first: claude-projects setup reconfigure --http [--mcp-port N]')
    if hc == 'macos_runtime':
        opts['ssh']['command'] = f'ssh <you>@<mac> limactl shell claude-projects -- {stdio}'
        opts['ssh']['when'] = 'the agent can SSH to the Mac (Remote Login on)'
    if hc == 'windows_runtime':
        opts['ssh']['command'] = f'ssh <you>@<pc> wsl.exe -d ClaudeProjects -u claude --exec {stdio}'
        opts['ssh']['when'] = 'the agent can SSH to the PC (OpenSSH Server on)'
    rec = 'same_host' if hc == 'grok_bot_box' else 'ssh'
    return opts, rec


def cmd_connect(a):
    opts, rec = connect_options()
    st = load_state()
    if a.mark:
        st['connected'] = {'transport': a.mark, 'at': _iso()}; _event(st, 'connected', transport=a.mark); save_state(st)
    return {'command': 'connect', 'recommended': rec, 'options': opts, 'connected': st.get('connected'),
            'after_adding': 'Call claude_setup(action="status") through the new connector: that proves the connection and records it.'}


# ---------------------------------------------------------------- verification (read-only)

def _first_project(projects):
    rows = [r for r in (projects.get('projects') or []) if r.get('status') != 'archived']
    rows.sort(key=lambda r: r.get('last_activity') or '', reverse=True)
    return rows[0] if rows else None


def mcp_tool_names():
    py = venv_python()
    code = 'import asyncio,json;from claude_projects import mcp_server as m;print(json.dumps([t.name for t in asyncio.run(m.mcp.list_tools())]))'
    rc, o, e = RUN([str(py if py.exists() else sys.executable), '-c', code], timeout=60,
                   env=dict(os.environ, PYTHONPATH=str(REPO), PYTHONWARNINGS='ignore'))
    try: return json.loads(o.strip().splitlines()[-1])
    except Exception: return None


def cmd_verify(a):
    checks = []; lay = layout(); mode = service_mode()
    checks.append(check('layout', 'pass' if lay == 'standard' else 'warn' if lay == 'legacy' else 'fail',
                        {'standard': f'install.sh layout, services: {mode}', 'legacy': f'older hand-built layout ({LEGACY_MCP_UNIT}.service, venv/): works; '
                         'migrating is optional'}.get(lay, 'not installed'), fix=None if lay else 'claude-projects setup install'))
    if mode == 'process':
        rc, _, _ = _systemctl_user('show-environment', timeout=15)
        checks.append(check('supervisor', 'pass' if rc == 0 else 'fail', 'process supervisor running (no systemd here)' if rc == 0 else 'process supervisor not running',
                            fix=None if rc == 0 else 'claude-projects setup start'))
    # host + services
    units = (core_units() + ([mcp_unit()] if (UNIT_DIR / f'{mcp_unit()}.service').exists() and http_enabled() else [])) if lay != 'legacy' else \
            [u for u in [LEGACY_MCP_UNIT] if (UNIT_DIR / f'{u}.service').exists() or unit_active(u)]
    boot = 'starts with the supervisor' if mode == 'process' else 'starts at boot'
    for u in units:
        act, en = unit_active(u), unit_enabled(u)
        checks.append(check(f'unit:{u}', 'pass' if act and en else 'fail' if not act else 'warn',
                            f"{'active' if act else 'not active'}, {boot if en else 'not enabled'}",
                            fix=None if act and en else f'{unit_log_hint(u)}; claude-projects setup start'))
    if mode != 'process':
        rc, o, _ = RUN(['loginctl', 'show-user', getpass.getuser(), '-p', 'Linger', '--value'], timeout=10)
        checks.append(check('linger', 'pass' if (o or '').strip() == 'yes' else 'warn', 'services survive logout and reboot' if (o or '').strip() == 'yes' else 'linger is off',
                            fix=None if (o or '').strip() == 'yes' else 'sudo loginctl enable-linger $USER'))
    hc = load_state().get('host', {}).get('class') or classify_host(detect_platform())[0]
    checks += [dict(c, id='host:' + c['id']) for c in always_on_checks(detect_platform(), hc)]
    if http_enabled() and ((UNIT_DIR / f'{mcp_unit()}.service').exists() or lay == 'legacy'):
        import urllib.request
        try:
            ok = urllib.request.urlopen(f'http://127.0.0.1:{ports()["mcp"]}/healthz', timeout=5).read().strip() == b'ok'
        except Exception: ok = False
        checks.append(check('mcp_health', 'pass' if ok else 'fail', '/healthz ok' if ok else '/healthz not answering', fix=None if ok else unit_log_hint(LEGACY_MCP_UNIT if lay == 'legacy' else mcp_unit())))
    bad = []
    for name in ('mcp.token', 'vncpass', 'vncpass.txt', 'keyring-pass', 'config.json'):
        p = CONFIG_DIR / name
        if p.exists() and p.stat().st_mode & 0o077: bad.append(name)
    checks.append(check('secret_files', 'fail' if bad else 'pass', ('readable by others: ' + ', '.join(bad)) if bad else 'private (chmod 600)',
                        fix=f'chmod 600 {CONFIG_DIR}/<file>' if bad else None))
    checks.append(signin_browser_check())
    vp = [int(x) for x in (CFG.get('viewer_ports') or [])]; exp = [ports()['vnc'], ports()['novnc']]
    checks.append(check('viewer_ports', 'pass' if set(exp) <= set(vp) else 'warn', f'{vp}', fix=None if set(exp) <= set(vp) else f'set viewer_ports to {exp}'))
    tools = mcp_tool_names()
    if tools is None: checks.append(check('mcp_tools', 'fail', 'could not load the MCP server module', fix='claude-projects setup install'))
    else:
        checks.append(check('mcp_tools', 'pass' if len(tools) >= EXPECTED_TOOLS and 'claude_setup' in tools else 'fail', f'{len(tools)} tools',
                            fix=None if len(tools) >= EXPECTED_TOOLS else 'the checkout is older than this skill: git pull, then claude-projects setup reconfigure'))
    sk = CLI(['skill'], 30)
    if sk.get('sha256'): checks.append(check('skill', 'pass', f"sha256 {sk['sha256']}", sha256=sk['sha256']))
    # app
    s = CLI(['status'], 60)
    if s.get('app'): checks.append(check('app', 'fail', s['app'], fix=unit_log_hint(U('desktop'))))
    elif s.get('error'): checks.append(check('app', 'fail', s.get('message', 'status failed'), fix=unit_log_hint(U('desktop'))))
    else:
        checks.append(check('app', 'pass', f"route {s.get('route')}"))
        checks.append(check('signed_in', 'pass' if s.get('signed_in') else 'fail', 'signed in' if s.get('signed_in') else 'not signed in', fix=None if s.get('signed_in') else 'claude-projects setup signin start'))
        checks.append(check('dialogs', 'pass' if not s.get('dialogs') and not s.get('stray_open_files') else 'warn',
                            'none open' if not s.get('dialogs') and not s.get('stray_open_files') else f"open: {s.get('dialogs') or ''} {s.get('stray_open_files') or ''}",
                            fix=None if not s.get('dialogs') and not s.get('stray_open_files') else 'claude-projects setup tidy --apply'))
        checks.append(check('viewer', 'pass' if not s.get('vnc_viewers') else 'warn', f"{s.get('vnc_viewers') or 0} viewers connected",
                            fix=None if not s.get('vnc_viewers') else 'close the viewport tab (UI actions pause while someone watches)'))
        checks.append(check('ui_lock', 'pass' if not s.get('lock_holder') else 'warn', 'free' if not s.get('lock_holder') else f"held by {s['lock_holder']}"))
    # projects
    beta = (load_state().get('beta') or {}).get('verdict')
    checks.append(check('projects_beta', 'pass' if beta == 'has_projects' else 'fail' if beta in ('no_projects', 'app_outdated') else 'warn',
                        beta or 'not checked yet', fix=None if beta == 'has_projects' else 'claude-projects setup check-beta'))
    if s.get('signed_in') and beta == 'has_projects':
        pl = CLI(['projects'] + (['--refresh'] if a.live else []), 240)
        if pl.get('error'): checks.append(check('project_list', 'fail', pl.get('message', '')[:200]))
        else:
            checks.append(check('project_list', 'pass', f"{pl.get('count', 0)} projects (fetched by the app {pl.get('list_fetched_by_app_local')})"))
            fp = _first_project(pl)
            if not fp:
                checks.append(check('project_read', 'warn', "no Projects yet: ready, nothing to read", fix='create a Project in the Claude app (or ask the agent to, explicitly)'))
            else:
                pid = fp['id']; ts = CLI(['thread-status', pid] + (['--refresh'] if a.live else []), 240)
                checks.append(check('project_read', 'fail' if ts.get('error') else 'pass', ts.get('message', '')[:200] if ts.get('error') else f"read {fp.get('name')!r} ({pid})"))
                mach = (ts.get('machine') or {}) if isinstance(ts.get('machine'), dict) else {}
                if mach: checks.append(check('thread_machine', 'pass', f"primary thread runs on {mach.get('resolved') or 'unknown (not shown in the app)'}"))
                if a.live:
                    env = CLI(['project-env', pid], 300)
                    ce = env.get('cloud_environment')
                    checks.append(check('cloud_environment', 'pass' if ce else 'warn', f"{fp.get('name')!r}: cloud environment {ce!r}" if ce else f"{fp.get('name')!r} has no cloud environment",
                                        fix=None if ce else 'Project settings -> Environment -> pick or add a cloud environment (cloud threads need one)'))
                    dr = CLI(['send', pid, 'setup verification (dry run, never sent)', '--dry-run'], 240)
                    ok = dr.get('dry_run') is True and str(dr.get('origin_header', '')).startswith(ORIGIN_PREFIX)
                    checks.append(check('send_dry_run', 'pass' if ok else 'fail', f"origin header {dr.get('origin_header')!r}, nothing typed" if ok else (dr.get('message') or str(dr))[:200]))
                else:
                    checks.append(check('cloud_environment', 'warn', 'skipped (pass --live to open Project settings and run a send dry run)'))
    res = {'command': 'verify', 'result': overall(checks), 'checks': checks, 'live': bool(a.live),
           'passed': sum(c['status'] == 'pass' for c in checks), 'warnings': sum(c['status'] == 'warn' for c in checks),
           'failed': sum(c['status'] == 'fail' for c in checks)}
    st = load_state(); st['verify'] = {'at': _iso(), 'result': res['result'], 'failed': [c['id'] for c in checks if c['status'] == 'fail'], 'live': bool(a.live)}
    _event(st, 'verify', result=res['result']); save_state(st)
    return res


# ---------------------------------------------------------------- tidy

def cmd_tidy(a):
    apply = a.apply; acts = []
    sw = CLI(['staging', 'sweep'] + (['--apply'] if apply else []), 120)
    acts.append({'what': 'staging sweep', 'result': {k: sw.get(k) for k in ('dry_run', 'removed_count', 'freed_bytes', 'error', 'message') if sw.get(k) is not None}})
    h = STATE_DIR / 'ui.holder'
    try:
        pid = int(json.loads(h.read_text()).get('pid') or 0)
        alive = pid > 0 and pathlib.Path(f'/proc/{pid}').exists()
        if not alive:
            if apply: h.unlink(missing_ok=True)
            acts.append({'what': f'stale UI-lock holder record (pid {pid} is gone)', 'done': apply})
    except Exception: pass
    cutoff = time.time() - 3600
    for d in STATE_DIR.glob('.claude-projects-idb-*'):
        try:
            if d.stat().st_mtime < cutoff:
                if apply: shutil.rmtree(d, ignore_errors=True)
                acts.append({'what': f'leftover IndexedDB copy {d.name}', 'done': apply})
        except Exception: pass
    for p in list((STATE_DIR / 'logs').glob('*.1')) + list((STATE_DIR / 'logs').glob('*.log.old')):
        try:
            if p.stat().st_mtime < time.time() - 14 * 86400:
                if apply: p.unlink()
                acts.append({'what': f'old log {p.name}', 'done': apply})
        except Exception: pass
    s = CLI(['status'], 60)
    if s.get('stray_open_files'):
        if apply: r = CLI(['tidy-ui'], 120); acts.append({'what': 'stray file-picker windows', 'done': not r.get('error'), 'detail': r})
        else: acts.append({'what': f"{s['stray_open_files']} stray file-picker window(s)", 'done': False})
    rc, o, _ = RUN(['pgrep', '-f', '--', str(CONFIG_DIR / 'browser-profile')], timeout=10)
    if (o or '').strip() and s.get('signed_in') and not s.get('vnc_viewers'):
        if apply: _kill_signin_browser()
        acts.append({'what': 'leftover sign-in browser window', 'done': apply})
    if viewport_mode() == 'on-demand' and s.get('signed_in') and not s.get('vnc_viewers') and any(unit_active(u) for u in viewport_units()):
        if apply: viewport(False)
        acts.append({'what': 'noVNC viewport still on after sign-in (viewport_mode on-demand)', 'done': apply})
    si = load_state().get('signin') or {}
    if si.get('tunnel') and (si.get('done_at') or time.time() - _parse_iso(si['tunnel'].get('at')) > 3600):
        if apply:
            stop_tunnel(); st = load_state(); st['signin'] = dict(st.get('signin') or {}, tunnel=None); save_state(st)
        acts.append({'what': 'sign-in tunnel still open', 'done': apply})
    st = load_state(); _event(st, 'tidy', apply=apply, actions=len(acts)); save_state(st)
    return {'command': 'tidy', 'dry_run': not apply, 'actions': acts,
            'never': 'tidy never restarts the Claude app or any Claude Code session and never presses anything in a thread'}


# ---------------------------------------------------------------- status: the state machine

def compute_status():
    st = load_state(); hc = (st.get('host') or {}).get('class')
    lay = layout()
    res = {'command': 'status', 'host': st.get('host'), 'installed': lay is not None, 'layout': lay, 'repo': str(REPO),
           'config_file': str(CONFIG_FILE), 'services': service_mode() if lay else None, 'http': http_enabled() if lay else None}
    def done(state, next_action=None, ask_user=None, **kw):
        res.update(state=state, next_action=next_action, ask_user=ask_user, **kw); return res
    pf = st.get('preflight') or {}
    if not res['installed']:
        if not pf:
            return done('start', 'claude-projects setup preflight   (on the computer the user picks)', None if hc else HOST_QUESTION)
        if pf.get('result') == 'fail':
            return done('preflight_failed', 'fix the failed checks, then claude-projects setup preflight', None, failed_checks=pf.get('failed'))
        if hc in ('macos_runtime', 'windows_runtime') and platform.system().lower() in ('darwin', 'windows'):
            return done('installing', 'bash hosts/macos/setup.sh' if hc == 'macos_runtime' else 'powershell -ExecutionPolicy Bypass -File hosts\\windows\\setup.ps1')
        if st.get('needs_sudo'):
            return done('needs_sudo', 'claude-projects setup install --no-apt   (after the user ran the sudo line)',
                        f'Please paste this one line into a terminal on {socket.gethostname()}: {as_root(f"{REPO}/install.sh --apt-only")}')
        outside = _app_pids()
        return done('installing', 'claude-projects setup install --bot-name "<your bot name>"', None,
                    note=(f'The Claude app is already running here outside the plugin (pid {", ".join(map(str, outside[:3]))}). '
                          'The install sets up its own display and app next to it; it does not touch that one.') if outside else None)
    if service_mode() == 'process':
        rc, o, _ = _systemctl_user('up', timeout=30)   # nothing else starts it on this host
        res['supervisor'] = ('started' if 'started' in (o or '') else 'running') if rc == 0 else 'failed'
        if not all(unit_active(u) for u in core_units()):   # just (re)started: units take a few seconds to come up
            for _ in range(15):
                if all(unit_active(u) for u in core_units()): break
                time.sleep(1)
    if lay == 'standard':
        missing = [u for u in core_units() if not (UNIT_DIR / f'{u}.service').exists()]
        units = {u: unit_active(u) for u in core_units()}
        res['units'] = units
        if missing:
            return done('installed_unhealthy', 'claude-projects setup install   (re-creates the missing service files)', None, missing=missing)
        if not all(units.values()):
            down = [u for u, v in units.items() if not v]
            outside = _app_pids() if U('desktop') in down else []
            if outside:
                return done('installed_unhealthy', 'ask the user, then stop that app and run claude-projects setup start', (
                    f'The Claude app on {socket.gethostname()} was started by hand, outside the plugin\'s services, so the plugin '
                    "can't use it. May I close it and start it the plugin's way? It reopens signed in."), down=down, app_pids=outside[:5])
            return done('installed_unhealthy', f'claude-projects setup start   (if it stays down: {unit_log_hint(down[0])})', None, down=down,
                        supervisor=res.get('supervisor'))
    s = CLI(['status'], 60); res['app'] = {k: s.get(k) for k in ('route', 'signed_in', 'vnc_viewers', 'lock_holder', 'app', 'error')}
    if not s.get('signed_in'):
        signin = st.get('signin') or {}
        return done('awaiting_signin', 'claude-projects setup signin wait' if signin.get('started_at') and not signin.get('done_at') else 'claude-projects setup signin start')
    if s.get('vnc_viewers'):
        return done('close_viewer', 'claude-projects setup signin wait', "You're signed in. Please close the viewport tab, so I can use the app.")
    beta = (st.get('beta') or {}).get('verdict')
    if beta in (None, 'unknown', 'not_signed_in', 'app_unreachable'):
        return done('beta_check', 'claude-projects setup check-beta', BETA_MESSAGES['unknown'] if beta == 'unknown' else None)
    if beta == 'app_outdated':
        return done('app_outdated', as_root('apt-get update && apt-get install --only-upgrade -y claude-desktop') + '; then claude-projects setup check-beta', BETA_MESSAGES['app_outdated'])
    if beta == 'no_projects':
        return done('no_projects_beta', 'stay idle; claude-projects setup check-beta when the user asks', BETA_MESSAGES['no_projects'], idle=True)
    if not st.get('connected'):
        opts, rec = connect_options()
        return done('connecting', f'add the MCP connector ({rec}): see claude-projects setup connect, then call claude_setup(action="status") through it', None, recommended_transport=rec)
    v = st.get('verify') or {}
    if v.get('result') in (None, 'fail') or not v.get('live'):
        return done('verifying', 'claude-projects setup verify --live   (or claude_setup(action="verify", live=true))', None, last_verify=v or None)
    return done('ready', None, None, last_verify=v,
                offer=('Setup is done. If the user has a real first task for a Project, sending it proves the whole path '
                       '(and cloud threads, if the Project uses a cloud environment). Never send a test message.'))


def _app_pids():
    """Claude app processes not started by the plugin's claude-projects-desktop service."""
    if installed() and unit_active(U('desktop')): return []
    rc, o, _ = RUN(['pgrep', '-f', '-a', 'claude-desktop'], timeout=10)
    pids = []
    for ln in (o or '').splitlines():
        pid, _, cmd = ln.partition(' ')
        if pid.isdigit() and int(pid) != os.getpid() and not any(x in cmd for x in ('claude-desktop-launch', 'pgrep', 'claude_projects')):
            pids.append(int(pid))
    return pids


def cmd_start(a):
    """(Re)start the plugin's services: the supervisor in process mode, or the enabled user units."""
    if not installed(): return {'command': 'start', 'result': 'not_installed', 'next_action': 'claude-projects setup install'}
    if layout() == 'legacy':
        return {'command': 'start', 'result': 'legacy_layout', 'note': f'the older layout runs {LEGACY_MCP_UNIT}.service; start it with systemctl --user start {LEGACY_MCP_UNIT}'}
    errs = []
    if service_mode() == 'process':
        rc, o, e = _systemctl_user('up', timeout=30)
        if rc != 0: errs.append((e or o or 'supervisor did not start').strip()[:200])
    want = core_units() + viewport_units() * (viewport_mode() == 'always') + [mcp_unit()] * http_enabled()
    for u in want:
        if not unit_active(u):
            rc, o, e = _systemctl_user('start', f'{u}.service', timeout=60)
            if rc != 0: errs.append(f'{u}: ' + ((e or o or '').strip().splitlines() or [f'exit {rc}'])[-1][:200])
    units = {u: 'active' if unit_active(u) else 'inactive' for u in want}
    ok = all(v == 'active' for v in units.values())
    return {'command': 'start', 'result': 'ok' if ok else 'failed', 'services': service_mode(), 'units': units, 'errors': errs or None,
            'fix': None if ok else '; '.join(unit_log_hint(u) for u, v in units.items() if v != 'active'), 'next_action': 'claude-projects setup status'}


def cmd_status(a):
    if getattr(a, 'via_mcp', False):
        st = load_state()
        if not st.get('connected'):
            st['connected'] = {'transport': 'mcp', 'at': _iso()}; _event(st, 'connected', transport='mcp'); save_state(st)
    return compute_status()


def cmd_mark(a):
    st = load_state()
    if a.what == 'connected': st['connected'] = {'transport': a.value or 'mcp', 'at': _iso()}
    elif a.what == 'disconnected': st.pop('connected', None)
    elif a.what == 'reset': st = {'history': st.get('history', [])}
    _event(st, 'mark', what=a.what); save_state(st)
    return {'command': 'mark', 'state': st}


def cmd_move(a):
    return {'command': 'move', 'steps': [
        'On the NEW computer: clone the repo and run the setup flow from the start (preflight, install, sign in).',
        'Point the agent connector at the new computer (claude-projects setup connect there) and run claude-projects setup verify --live.',
        'On THIS computer: claude-projects setup uninstall (keeps config and sign-in) or claude-projects setup uninstall --purge --yes after the user confirms.',
        'Sign-in does not move between computers: the user signs in again on the new one.']}


def cmd_uninstall(a):
    if a.purge and not a.yes:
        return {'command': 'uninstall', 'result': 'needs_confirmation',
                'ask_user': f'This removes the plugin services and deletes {CONFIG_DIR} (token, viewport password, sign-in browser profile) and {STATE_DIR}. '
                            "The Claude app's own sign-in in ~/.config/Claude stays. OK to delete?",
                'then': 'claude-projects setup uninstall --purge --yes'}
    if (load_state().get('signin') or {}).get('tunnel'): stop_tunnel()
    rc, out, err = RUN([str(REPO / 'install.sh'), '--uninstall'], timeout=300)
    removed = []
    if a.purge:
        for d in (CONFIG_DIR, STATE_DIR):
            if d.exists(): shutil.rmtree(d, ignore_errors=True); removed.append(str(d))
    return {'command': 'uninstall', 'result': 'ok' if rc == 0 else 'failed', 'removed': removed or None,
            'kept': None if a.purge else [str(CONFIG_DIR), str(STATE_DIR)],
            'note': "The Claude desktop app package and its sign-in (~/.config/Claude) are left alone; remove them with apt if wanted."}


# ---------------------------------------------------------------- CLI

def _human(res):
    for k in ('state', 'result', 'verdict', 'host_class'):
        if res.get(k): print(f'{k}: {res[k]}')
    for c in res.get('checks') or []:
        mark = {'pass': 'ok  ', 'warn': 'warn', 'fail': 'FAIL'}[c['status']]
        print(f"  [{mark}] {c['id']}: {c['detail']}" + (f"\n         fix: {c['fix']}" if c.get('fix') and c['status'] != 'pass' else ''))
    for x in res.get('actions') or []: print(f"  - {x['what']}" + ('' if x.get('done') else '  (dry run)' if res.get('dry_run') else ''))
    if res.get('links'): [print(f'  {k}: {v}') for k, v in res['links'].items()]
    for k in ('tell_user', 'ask_user', 'next_action', 'note', 'offer'):
        if res.get(k): print(f'{k}: {res[k]}')


def build_parser():
    ap = argparse.ArgumentParser(prog='claude-projects setup', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--json', action='store_true'); ap.add_argument('--via-mcp', action='store_true', help=argparse.SUPPRESS)
    sp = ap.add_subparsers(dest='sub', required=True)
    sp.add_parser('status', help='where the setup stands and what to do next')
    s = sp.add_parser('preflight', help='check this computer before installing'); s.add_argument('--host-kind', default='auto', choices=['auto', 'local', 'box', 'macos-vm', 'windows-wsl', 'laptop'])
    s = sp.add_parser('choose', help='record the host the user picked'); s.add_argument('host_class', choices=list(HOST_NOTES)); s.add_argument('--label')
    for name in ('install', 'reconfigure'):
        s = sp.add_parser(name, help='run install.sh' if name == 'install' else 're-render config and services, restart only what changed')
        s.add_argument('--bot-name'); s.add_argument('--operator-name'); s.add_argument('--timezone'); s.add_argument('--display'); s.add_argument('--screen')
        s.add_argument('--claude-md', action='store_true')
        s.add_argument('--mcp-port', type=int, help='port for the HTTP MCP server (default 8791)')
        g = s.add_mutually_exclusive_group()
        g.add_argument('--http', dest='http', action='store_const', const=True, default=None, help='run the HTTP MCP server (default, except on the Grok Bot box)')
        g.add_argument('--no-http', dest='http', action='store_const', const=False, help='stdio only: no HTTP server')
        if name == 'install':
            s.add_argument('--no-apt', action='store_true'); s.add_argument('--dry-run', action='store_true')
            s.add_argument('--service-mode', choices=['systemd', 'process'], help='default: picked from the preflight')
            s.add_argument('--host-kind', default='auto', choices=['auto', 'local', 'box', 'macos-vm', 'windows-wsl', 'laptop'])
            s.add_argument('--force', action='store_true', help='install even though the preflight failed (ask the user first)')
            s.add_argument('--migrate', action='store_true', help='replace the older hand-built layout (ask the user first: outage + fresh sign-in)')
        else: s.add_argument('--restart-app', action='store_true', help='also restart the Claude app if its unit changed (ask the user first)')
    s = sp.add_parser('signin', help='sign in through the noVNC viewport'); ss = s.add_subparsers(dest='signin_cmd', required=True)
    x = ss.add_parser('start'); x.add_argument('--switch', action='store_true', help='sign in with a different account'); x.add_argument('--public-url', help="the user's own tunnel URL for the viewport")
    x.add_argument('--tunnel', action='store_true', help='open a temporary cloudflared quick tunnel to the viewport (ask the user first)')
    x.add_argument('--share-password', action='store_true', help='put a one-time viewport password in the link (default on the Grok Bot box)')
    x = ss.add_parser('wait'); x.add_argument('--timeout', type=int, default=900); x.add_argument('--poll', type=int, default=30); x.add_argument('--close-timeout', type=int, default=600)
    s = sp.add_parser('check-beta', help='does this account have Claude Code Projects?'); s.add_argument('--force', action='store_true', help=argparse.SUPPRESS)
    s = sp.add_parser('connect', help='how to connect the agent'); s.add_argument('--mark', help='record that the connector works (transport name)')
    s = sp.add_parser('verify', help='read-only end-to-end checks'); s.add_argument('--live', action='store_true', help='also refresh, open Project settings and run a send dry run')
    s = sp.add_parser('viewport', help='turn the noVNC viewport on or off'); s.add_argument('state', choices=['on', 'off']); s.add_argument('--force', action='store_true')
    s = sp.add_parser('tidy', help='clean up leftovers (dry run unless --apply)'); s.add_argument('--apply', action='store_true')
    s = sp.add_parser('mark', help='record a fact the agent learned'); s.add_argument('what', choices=['connected', 'disconnected', 'reset']); s.add_argument('value', nargs='?')
    sp.add_parser('move', help='move to another computer')
    sp.add_parser('start', help="start the plugin's services (the supervisor too, on hosts without systemd)")
    s = sp.add_parser('uninstall', help='remove the services'); s.add_argument('--purge', action='store_true'); s.add_argument('--yes', action='store_true')
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if 'setup' in argv[:3]: argv.remove('setup')
    if '--json' in argv: argv.remove('--json'); argv.insert(0, '--json')
    if '--via-mcp' in argv: argv.remove('--via-mcp'); argv.insert(0, '--via-mcp')
    a = build_parser().parse_args(argv)
    fn = {'status': cmd_status, 'preflight': cmd_preflight, 'choose': cmd_choose, 'install': cmd_install, 'reconfigure': cmd_reconfigure,
          'check-beta': cmd_check_beta, 'connect': cmd_connect, 'verify': cmd_verify, 'viewport': cmd_viewport, 'tidy': cmd_tidy,
          'mark': cmd_mark, 'move': cmd_move, 'uninstall': cmd_uninstall, 'start': cmd_start}.get(a.sub)
    if a.sub == 'signin': fn = cmd_signin_start if a.signin_cmd == 'start' else cmd_signin_wait
    res = fn(a)
    print(json.dumps(res, ensure_ascii=False, default=str) if a.json else '', end='\n' if a.json else '')
    if not a.json: _human(res)
    return 1 if res.get('result') in ('fail', 'failed') else 0


if __name__ == '__main__':
    sys.exit(main())
