"""Hosts without systemd (the Grok Bot box): process mode, stdio-only installs, box sign-in, legacy layout, honest
viewport state and next steps. Shell and app calls are faked as in test_setup.py; the supervisor test runs real
processes (sleep) under claude_projects/proc.py with a private D-Bus bus when dbus-daemon is installed."""
import json, os, pathlib, shutil, subprocess, sys, time
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import setup as S
from claude_projects import config as cfg_mod
from test_setup import env, args, _installed, _preflight  # noqa: F401  (fixtures and helpers)

REPO = pathlib.Path(__file__).resolve().parents[1]
UBUNTU = {'os': 'linux', 'arch': 'x86_64', 'python': '3.12.3', 'user': 'box', 'hostname': 'h', 'distro_id': 'ubuntu',
          'version_id': '24.04', 'distro_like': 'debian', 'pretty': 'Ubuntu 24.04', 'wsl': False, 'lima': False}


# ---------------------------------------------------------------- detection and preflight

# exactly what the real Grok Bot box shows: PID 1 tini, none of the usual container files, cgroup v2 under a pod slice,
# no /run/systemd/system, systemd-detect-virt --container -> container-other
REAL_BOX = {'pid1': 'tini', 'dockerenv': False, 'containerenv': False, 'container_env': '', 'cgroup': '0::/system.slice/pod-3f2a9c\n',
            'detect_virt': 'container-other', 'run_systemd': False, 'wsl': False, 'lima': False}


def test_real_box_facts_are_detected():
    assert not S._in_container(dict(REAL_BOX, detect_virt='none')), 'none of the classic markers are there'
    r = S.box_reason(REAL_BOX)
    assert r and 'tini' in r and 'container-other' in r
    # even if systemd-detect-virt were missing: PID 1 isn't systemd and /run/systemd/system is absent
    assert 'no systemd running' in S.box_reason(dict(REAL_BOX, detect_virt=''))


@pytest.mark.parametrize('change', [{'pid1': 'systemd'}, {'run_systemd': True}, {'wsl': True, 'pid1': 'init'}, {'lima': True}])
def test_hosts_with_systemd_wsl_or_lima_are_not_the_box(change):
    assert S.box_reason(dict(REAL_BOX, **change)) is None


def test_detect_platform_uses_the_box_facts(monkeypatch):
    monkeypatch.delenv('CLAUDE_PROJECTS_HOST_KIND', raising=False)
    monkeypatch.setattr(S.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(S, '_pid1', lambda: 'tini')
    monkeypatch.setattr(S, '_container_facts', lambda: {k: v for k, v in REAL_BOX.items() if k not in ('pid1', 'wsl', 'lima')})
    f = S.detect_platform()
    if f['wsl'] or f['lima']: pytest.skip('this test host is WSL or Lima')
    assert f['grok_bot_box'] and f['container'] and 'container-other' in f['box_reason'] and S.classify_host(f)[0] == 'grok_bot_box'
    monkeypatch.setattr(S, '_pid1', lambda: 'systemd')
    assert not S.detect_platform()['grok_bot_box'], 'a container that boots systemd is an ordinary Linux host'


def test_preflight_without_systemd_uses_process_mode_and_no_linger(env):
    env.run_map['show-environment'] = (1, '', 'Failed to connect to bus: No medium found')
    checks = {c['id']: c for c in S.linux_checks(dict(UBUNTU, pid1='tini'), 'grok_bot_box')}
    assert checks['systemd_user']['status'] == 'pass' and checks['systemd_user']['service_mode'] == 'process'
    assert checks['linger']['status'] == 'pass' and 'enable-linger' not in json.dumps(checks)
    assert not any(c[:2] == ['loginctl', 'show-user'] for c in env.runs)


def test_preflight_on_systemd_host_with_dead_user_manager_still_fails(env):
    env.run_map['show-environment'] = (1, '', 'Failed to connect to bus')
    checks = {c['id']: c for c in S.linux_checks(dict(UBUNTU, pid1='systemd'), 'always_on_linux')}
    assert checks['systemd_user']['status'] == 'fail' and 'enable-linger' in checks['systemd_user']['fix']


def test_box_needs_no_mcp_port_and_busy_port_gets_a_suggestion(env, monkeypatch):
    monkeypatch.setattr(S, '_port_open', lambda p, host='127.0.0.1': p in (8791, 8792))
    box = {c['id']: c for c in S.linux_checks(dict(UBUNTU, pid1='tini'), 'grok_bot_box')}
    assert box['ports']['status'] == 'pass' and 'stdio' in box['ports']['detail']
    lin = {c['id']: c for c in S.linux_checks(dict(UBUNTU, pid1='systemd'), 'always_on_linux')}
    assert lin['ports']['status'] == 'fail' and '--mcp-port 8793' in lin['ports']['fix'] and lin['ports']['suggested_mcp_port'] == 8793


# ---------------------------------------------------------------- install

def test_install_refuses_after_failed_preflight_even_as_dry_run(env, monkeypatch):
    _preflight(monkeypatch, result='fail')
    r = S.cmd_install(args(no_apt=False, dry_run=True))
    assert r['result'] == 'preflight_failed' and r['checks'][0]['id'] == 'os'
    assert not any(c[0].endswith('install.sh') for c in env.runs)


def test_install_on_box_is_stdio_and_process_mode(env, monkeypatch):
    monkeypatch.setattr(S, '_missing_apt', lambda: False)
    _preflight(monkeypatch, host_class='grok_bot_box', mode='process')
    r = S.cmd_install(args(no_apt=False, dry_run=True))
    cmd = next(c for c in env.runs if c[0].endswith('install.sh'))
    assert '--no-http' in cmd and cmd[cmd.index('--service-mode') + 1] == 'process'
    assert r['result'] == 'ok' and r['http'] is False and r['service_mode'] == 'process'


def test_install_mcp_port_and_http_flags_pass_through(env, monkeypatch):
    monkeypatch.setattr(S, '_missing_apt', lambda: False); _preflight(monkeypatch)
    S.cmd_install(args(no_apt=True, dry_run=True, mcp_port=8899, http=True))
    cmd = next(c for c in env.runs if c[0].endswith('install.sh'))
    assert cmd[cmd.index('--mcp-port') + 1] == '8899' and '--http' in cmd and '--service-mode' not in cmd


def test_install_sh_options():
    src = (REPO / 'install.sh').read_text()
    assert '--no-install-recommends' in src and 'AS="as root"' in src and 'system packages ($AS)' in src
    assert 'system packages ($SUDO)' not in src
    r = subprocess.run(['bash', str(REPO / 'install.sh'), '--mcp-port', '80', '--dry-run'], capture_output=True, text=True)
    assert r.returncode == 2 and 'mcp-port' in r.stderr
    r = subprocess.run(['bash', str(REPO / 'install.sh'), '--service-mode', 'launchd'], capture_output=True, text=True)
    assert r.returncode == 2


def test_keyring_unit_takes_a_private_data_dir():
    unit = (REPO / 'systemd' / 'claude-projects-keyring.service').read_text()
    assert '@KEYRING_ENV@' in unit
    src = (REPO / 'install.sh').read_text()
    assert "keyring_data_dir" in src and 'Environment=XDG_DATA_HOME=$KEYRING_DIR' in src
    assert 'KEYRING_ENV="#' not in src, "a '#' would end the sed expression early"


# ---------------------------------------------------------------- legacy layout

def test_legacy_layout_counts_as_installed_and_install_leaves_it_alone(env, monkeypatch, tmp_path):
    repo = tmp_path / 'repo'; (repo / 'venv' / 'bin').mkdir(parents=True); (repo / 'venv' / 'bin' / 'python').write_text('')
    monkeypatch.setattr(S, 'REPO', repo)
    (S.UNIT_DIR / 'claude-projects-mcp.service').write_text('[Service]\nExecStart=/x\n')
    assert S.layout() == 'legacy' and S.installed()
    r = S.cmd_install(args(no_apt=False, dry_run=False))
    assert r['result'] == 'legacy_layout' and '--migrate' in r['note']
    assert not env.runs or not any(c[0].endswith('install.sh') for c in env.runs)
    assert S.cmd_reconfigure(args())['result'] == 'legacy_layout'


def test_status_and_verify_on_legacy_layout(env, monkeypatch, tmp_path):
    repo = tmp_path / 'repo'; (repo / 'venv' / 'bin').mkdir(parents=True); (repo / 'venv' / 'bin' / 'python').write_text('')
    monkeypatch.setattr(S, 'REPO', repo)
    (S.UNIT_DIR / 'claude-projects-mcp.service').write_text('[Service]\nExecStart=/x\n')
    env.run_map['is-active claude-projects-mcp'] = (0, 'active', ''); env.run_map['is-enabled claude-projects-mcp'] = (0, 'enabled', '')
    env.cli['status'] = {'signed_in': True, 'route': '/'}
    S.save_state({'host': {'class': 'always_on_linux'}, 'beta': {'verdict': 'has_projects'}})
    st = S.compute_status()
    assert st['installed'] and st['layout'] == 'legacy' and st['state'] == 'connecting'
    monkeypatch.setattr(S, 'mcp_tool_names', lambda: ['claude_setup'] * 25)
    v = {c['id']: c for c in S.cmd_verify(args(live=False))['checks']}
    assert v['layout']['status'] == 'warn' and v['unit:claude-projects-mcp']['status'] == 'pass'
    assert not any(k.startswith('unit:claude-projects-') and k != 'unit:claude-projects-mcp' for k in v), 'the legacy layout has only its one MCP unit'


# ---------------------------------------------------------------- viewport and status honesty

def test_viewport_on_reports_units_that_did_not_start(env):
    env.run_map['enable --now'] = (1, '', 'Failed to connect to bus: No medium found')
    r = S.viewport(True)
    assert r['viewport'] == 'failed' and r['units'] == {'claude-projects-vnc': 'inactive', 'claude-projects-novnc': 'inactive'}
    assert 'No medium found' in r['errors'][0] and 'journalctl' in r['fix']


def test_signin_start_stops_when_the_viewport_did_not_start(env):
    env.cli['status'] = {'signed_in': False, 'route': '/login'}
    r = S.cmd_signin_start(args(switch=False, public_url=None))
    assert r['result'] == 'viewport_failed' and 'ask_user' not in r


def test_status_next_steps_when_services_are_down(env, monkeypatch):
    _installed(monkeypatch, units=False)
    (S.UNIT_DIR / 'claude-projects-wm.service').unlink()
    r = S.compute_status(); assert r['state'] == 'installed_unhealthy' and r['missing'] == ['claude-projects-wm'] and r['next_action'].startswith('claude-projects setup install')
    (S.UNIT_DIR / 'claude-projects-wm.service').write_text('x')
    r = S.compute_status(); assert r['next_action'].startswith('claude-projects setup start') and 'journalctl' not in r['next_action'].split('(')[0]
    env.run_map['pgrep -f -a claude-desktop'] = (0, '4242 /usr/lib/claude-desktop/claude-desktop --no-sandbox\n', '')
    r = S.compute_status(); assert r['app_pids'] == [4242] and 'May I close it' in r['ask_user']


def test_process_mode_uses_the_supervisor_and_its_logs(env, monkeypatch):
    monkeypatch.setattr(S, 'CFG', dict(S.CFG, service_mode='process'))
    S._systemctl_user('is-active', 'claude-projects-xvfb.service')
    assert env.runs[-1][1:3] == ['-m', 'claude_projects.proc'] and env.runs[-1][3:] == ['is-active', 'claude-projects-xvfb.service']
    assert S.unit_log_hint('claude-projects-mcp').endswith('proc/logs/claude-projects-mcp.log')


def test_runtime_env_in_process_mode_overrides_the_bus():
    cfg = dict(cfg_mod.CFG, service_mode='process', state_dir='/tmp/cp-st')
    e = cfg_mod.apply_runtime_env(cfg, {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/elsewhere'})
    assert e['DBUS_SESSION_BUS_ADDRESS'] == 'unix:path=/tmp/cp-st/run/bus' and e['XDG_RUNTIME_DIR'] == '/tmp/cp-st/run'
    e = cfg_mod.apply_runtime_env(dict(cfg, service_mode='systemd'), {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/elsewhere'})
    assert e['DBUS_SESSION_BUS_ADDRESS'] == 'unix:path=/elsewhere'


# ---------------------------------------------------------------- sign-in on the box

def _box_signin(env, monkeypatch, tmp_path):
    env.run_map['is-active'] = (0, 'active', '')
    env.cli['status'] = {'signed_in': False, 'route': '/login', 'vnc_viewers': 0}
    S.save_state({'host': {'class': 'grok_bot_box'}})
    (S.CONFIG_DIR / 'vncpass').write_text('x')


def test_box_signin_asks_before_opening_a_tunnel(env, monkeypatch, tmp_path):
    _box_signin(env, monkeypatch, tmp_path)
    r = S.cmd_signin_start(args(switch=False, public_url=None))
    assert r['result'] == 'needs_viewer_route' and 'May I?' in r['ask_user'] and '--tunnel' in r['next_action']
    assert not any('enable' in c for c in env.runs), 'nothing is turned on before the user agrees'


def test_box_signin_with_tunnel_and_one_time_password(env, monkeypatch, tmp_path):
    _box_signin(env, monkeypatch, tmp_path)
    fake = tmp_path / 'cloudflared'; fake.write_text('#!/bin/sh\n'); fake.chmod(0o755)
    monkeypatch.setattr(S, '_cloudflared', lambda: str(fake))
    def spawn(exe, port, log):
        log.write_text(f'INF |  https://quiet-river-1234.trycloudflare.com  | port {port}\n'); return 99999
    monkeypatch.setattr(S, 'SPAWN_TUNNEL', spawn)
    r = S.cmd_signin_start(args(switch=False, public_url=None, tunnel=True))
    assert r['result'] == 'awaiting_signin' and r['tunnel'] == 'https://quiet-river-1234.trycloudflare.com'
    link = r['links']['your_link']; pw = (S.CONFIG_DIR / 'vncpass.txt').read_text()
    assert link.startswith('https://quiet-river-1234.trycloudflare.com/vnc.html') and link.endswith(f'#password={pw}') and len(pw) == 8
    assert pw not in link.split('#')[0], 'the password stays in the fragment, which the browser never sends (no server log)'
    assert 'on_this_machine' not in r['links'] and r['password_file'] is None and 'Get started' in r['ask_user']
    assert any(c[:2] == ['x11vnc', '-storepasswd'] for c in env.runs)
    killed = []
    monkeypatch.setattr(S.os, 'killpg', lambda pid, sig: killed.append(pid))
    env.cli['status'] = {'signed_in': True, 'vnc_viewers': 0}
    w = S.cmd_signin_wait(args(timeout=10, poll=1, close_timeout=10))
    assert w['result'] == 'signed_in' and w['closed_tunnel'] and killed == [99999] and w['replaced_shared_password']
    assert (S.CONFIG_DIR / 'vncpass.txt').read_text() != pw, 'the password that went through chat must stop working'
    assert S.load_state()['signin']['tunnel'] is None


def test_box_signin_without_cloudflared_says_how(env, monkeypatch, tmp_path):
    _box_signin(env, monkeypatch, tmp_path)
    monkeypatch.setattr(S, '_cloudflared', lambda: None)
    r = S.cmd_signin_start(args(switch=False, public_url=None, tunnel=True))
    assert r['result'] == 'tunnel_failed' and 'cloudflared' in r['next_action'] and 'ask the user first' in r['next_action']


def test_signin_steps_say_get_started(env):
    env.run_map['is-active'] = (0, 'active', '')
    env.cli['status'] = {'signed_in': False, 'route': '/login'}
    r = S.cmd_signin_start(args(switch=False, public_url=None))
    assert '"Get started"' in r['ask_user'] and 'older versions say "Sign in"' in r['ask_user']
    for f in ('skill/SKILL.md', 'docs/manual-install.md', 'install.sh'):
        assert 'Get started' in (REPO / f).read_text(), f


# ---------------------------------------------------------------- claude_projects.proc: the supervisor itself

def test_parse_unit_specifiers_and_deps(tmp_path, monkeypatch):
    from claude_projects import proc as P
    monkeypatch.setattr(P, 'UNIT_DIR', tmp_path)
    (tmp_path / 'claude-projects-keyring.service').write_text(
        '[Unit]\nRequires=dbus.socket\nAfter=dbus.socket\n[Service]\nType=dbus\nStandardInput=file:/c/keyring-pass\n'
        'Environment=XDG_DATA_HOME=/k\nExecStart=/usr/bin/gnome-keyring-daemon --foreground --control-directory=%t/keyring\nRestart=on-failure\nRestartSec=5\n')
    u = P.parse_unit('claude-projects-keyring', {'XDG_RUNTIME_DIR': '/rt', 'HOME': '/h'})
    assert u['exec'][-1] == '--control-directory=/rt/keyring' and u['env'] == {'XDG_DATA_HOME': '/k'} and u['stdin'] == '/c/keyring-pass'
    assert u['requires'] == ['claude-projects-dbus'] and u['restart'] == 'on-failure' and u['restart_sec'] == 5.0
    assert P.parse_unit('claude-projects-nope', {'XDG_RUNTIME_DIR': '/rt', 'HOME': '/h'}) is None


@pytest.mark.skipif(not shutil.which('dbus-daemon'), reason='needs dbus-daemon')
def test_supervisor_runs_restarts_and_stops_units(tmp_path):
    """Real processes: a fake claude-projects-xvfb (sleep), a dependent unit and a crashing one, under a fresh supervisor."""
    units = tmp_path / 'cfg' / 'systemd' / 'user'; units.mkdir(parents=True)
    (units / 'claude-projects-xvfb.service').write_text('[Service]\nExecStart=/bin/sleep 300\nRestart=on-failure\n')
    (units / 'claude-projects-wm.service').write_text('[Unit]\nRequires=claude-projects-xvfb.service\nAfter=claude-projects-xvfb.service\n[Service]\n'
                                          'Environment=MARK=wm-ok\nExecStart=/bin/sh -c "echo $MARK; exec sleep 300"\nRestart=on-failure\n')
    (units / 'claude-projects-crash.service').write_text('[Service]\nExecStart=/bin/sh -c "exit 3"\nRestart=on-failure\nRestartSec=0\n')
    cfgf = tmp_path / 'cfg' / 'config.json'; cfgf.write_text(json.dumps({'state_dir': str(tmp_path / 'st'), 'service_mode': 'process'}))
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / 'cfg'), CLAUDE_PROJECTS_CONFIG=str(cfgf))
    def proc(*a, check=False):
        return subprocess.run([sys.executable, str(REPO / 'claude_projects/proc.py'), *a], env=env, capture_output=True, text=True, timeout=60)
    try:
        assert proc('enable', '--now', 'claude-projects-xvfb.service', 'claude-projects-wm.service').returncode == 0
        assert proc('is-active', 'claude-projects-wm').stdout.strip() == 'active' and proc('is-enabled', 'claude-projects-wm').stdout.strip() == 'enabled'
        assert (tmp_path / 'st' / 'run' / 'bus').exists(), 'the supervisor brings its own session bus'
        assert 'wm-ok' in (tmp_path / 'st' / 'proc' / 'logs' / 'claude-projects-wm.log').read_text()
        pid = json.loads((tmp_path / 'st' / 'proc' / 'status.json').read_text())['units']['claude-projects-xvfb']['pid']
        os.kill(pid, 9)                                   # a crash: Restart=on-failure brings it back
        for _ in range(40):
            st = json.loads((tmp_path / 'st' / 'proc' / 'status.json').read_text())['units']
            if st['claude-projects-xvfb'].get('pid') not in (None, pid) and st['claude-projects-xvfb']['state'] == 'active': break
            time.sleep(0.25)
        assert st['claude-projects-xvfb']['state'] == 'active' and st['claude-projects-xvfb']['pid'] != pid
        r = proc('start', 'claude-projects-crash', '--timeout', '15')  # keeps exiting 3: gives up and says so
        assert r.returncode == 1 and 'failed' in r.stderr
        assert proc('restart', 'claude-projects-wm').returncode == 0
        assert proc('disable', '--now', 'claude-projects-wm').returncode == 0 and proc('is-active', 'claude-projects-wm').stdout.strip() == 'inactive'
        assert proc('up').returncode == 0, 'up is idempotent'
    finally:
        proc('down')
    assert proc('show-environment').returncode == 1


def test_starting_supervisor_waits_out_a_probe(tmp_path, monkeypatch):
    """The race from the review: a probe holding supervisor.lock for an instant must not make the supervisor quit."""
    import fcntl, threading
    from claude_projects import proc as P
    lock = tmp_path / 'supervisor.lock'
    probe = open(lock, 'a+'); fcntl.flock(probe, fcntl.LOCK_SH)
    threading.Timer(0.3, lambda: (fcntl.flock(probe, fcntl.LOCK_UN), probe.close())).start()
    with open(lock, 'a+') as f:
        assert P._take_supervisor_lock(f, retry=3.0), 'gave up while only a probe was in the way'
        with open(lock, 'a+') as other:   # a second supervisor really is excluded, after its retry window
            assert not P._take_supervisor_lock(other, retry=0.2)


@pytest.mark.skipif(not shutil.which('dbus-daemon'), reason='needs dbus-daemon')
def test_up_survives_hammering_probes_and_racing_callers(tmp_path, monkeypatch):
    """supervisor_running() in a tight loop while three `claude_projects/proc.py up` race, for several start/stop cycles:
    every up succeeds and exactly one supervisor starts per cycle."""
    import threading
    from claude_projects import proc as P
    (tmp_path / 'cfg' / 'systemd' / 'user').mkdir(parents=True)
    cfgf = tmp_path / 'cfg' / 'config.json'; cfgf.write_text(json.dumps({'state_dir': str(tmp_path / 'st'), 'service_mode': 'process'}))
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / 'cfg'), CLAUDE_PROJECTS_CONFIG=str(cfgf))
    proc_dir = tmp_path / 'st' / 'proc'
    monkeypatch.setattr(P, 'PROC', proc_dir); monkeypatch.setattr(P, 'LOCK', proc_dir / 'supervisor.lock')
    stop = threading.Event(); probes = [0]
    def hammer():
        while not stop.is_set(): P.supervisor_running(); probes[0] += 1
    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(2)]
    for t in threads: t.start()
    cmd = [sys.executable, str(REPO / 'claude_projects/proc.py')]
    cycles = 6
    try:
        for _ in range(cycles):
            ups = [subprocess.Popen(cmd + ['up'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
            outs = [p.communicate(timeout=60) for p in ups]
            assert all(p.returncode == 0 for p in ups), outs
            assert sum('started' in o for o, _ in outs) == 1, outs
            assert subprocess.run(cmd + ['down'], env=env, capture_output=True, text=True, timeout=60).returncode == 0
    finally:
        stop.set()
        for t in threads: t.join(5)
        subprocess.run(cmd + ['down'], env=env, capture_output=True, timeout=60)
    log = (proc_dir / 'supervisor.log').read_text()
    assert log.count('supervisor up') == cycles and probes[0] > 1000
