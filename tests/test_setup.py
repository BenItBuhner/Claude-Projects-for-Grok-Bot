"""Guided setup flow (cpg_setup.py): beta verdicts, host classes, the status state machine, sign-in, tidy, MCP tool.
No app, display, systemd or network: RUN (shell) and CLI (`claude-projects --json ...`) are replaced with fakes."""
import asyncio, json, os, pathlib, re, sys, time
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
import cpg_setup as S

REPO = pathlib.Path(__file__).resolve().parents[1]


class Fake:
    """Records shell calls; answers claude-projects --json calls from a dict of command -> result (or list of results, in order)."""
    def __init__(self, cli=None, run=None):
        self.cli = dict(cli or {}); self.runs = []; self.cli_calls = []; self.run_map = run or {}
    def RUN(self, cmd, timeout=60, env=None, input=None):
        self.runs.append(list(cmd))
        for k, v in self.run_map.items():
            if k in ' '.join(cmd): return v
        return 0, '', ''
    def CLI(self, args, timeout=180):
        self.cli_calls.append(list(args)); r = self.cli.get(args[0], {})
        if isinstance(r, list): return r.pop(0) if len(r) > 1 else r[0]
        return r


@pytest.fixture
def env(tmp_path, monkeypatch):
    st, cfgd, units = tmp_path / 'state', tmp_path / 'cfg', tmp_path / 'units'
    for d in (st, cfgd, units): d.mkdir()
    monkeypatch.setattr(S, 'STATE_DIR', st); monkeypatch.setattr(S, 'STATE_FILE', st / 'setup.json')
    monkeypatch.setattr(S, 'CONFIG_DIR', cfgd); monkeypatch.setattr(S, 'UNIT_DIR', units)
    monkeypatch.setattr(S.time, 'sleep', lambda s: None)
    f = Fake(); monkeypatch.setattr(S, 'RUN', f.RUN); monkeypatch.setattr(S, 'CLI', f.CLI)
    monkeypatch.setattr(S, 'CFG', dict(S.CFG, viewport_mode='on-demand'))
    return f


def args(**kw): return type('A', (), kw)()


# ---------------------------------------------------------------- beta detection

@pytest.mark.parametrize('probe,verdict', [
    ({'signed_in': True, 'sidebar_entry': 'Projects Beta', 'browse_route_ok': True, 'project_list_cached': True, 'project_count': 3}, 'has_projects'),
    ({'signed_in': True, 'sidebar_entry': 'Projects', 'browse_route_ok': True, 'project_list_cached': False}, 'has_projects'),
    ({'signed_in': True, 'sidebar_entry': None, 'browse_route_ok': False, 'project_list_cached': False,
      'app_version': {'installed': '1.2.0', 'candidate': '1.2.0'}}, 'no_projects'),
    ({'signed_in': True, 'sidebar_entry': None, 'browse_route_ok': False, 'project_list_cached': True,
      'project_list_query_status': 'error', 'project_count': 0}, 'no_projects'),
    ({'signed_in': True, 'sidebar_entry': None, 'browse_route_ok': False, 'app_version': {'installed': '1.1.0', 'candidate': '1.2.0'}}, 'app_outdated'),
    ({'signed_in': True, 'sidebar_entry': None, 'browse_route_ok': False, 'project_list_cached': True, 'project_count': 4}, 'unknown'),
    ({'signed_in': True, 'sidebar_entry': 'Projects', 'browse_route_ok': False}, 'unknown'),
    ({'signed_in': False}, 'not_signed_in'),
    ({'error': 'AppError', 'message': 'not reachable'}, 'app_unreachable'),
])
def test_beta_verdict(probe, verdict):
    assert S.beta_verdict(probe) == verdict


def test_check_beta_no_projects_stays_idle(env):
    env.cli['probe-projects'] = {'signed_in': True, 'sidebar_entry': None, 'browse_route_ok': False, 'project_list_cached': False}
    r = S.cmd_check_beta(args(force=False))
    assert r['verdict'] == 'no_projects' and r['has_projects'] is False
    assert "doesn't have Claude Code Projects" in r['tell_user'] and 'uninstall' not in r['tell_user'].lower()
    assert ['systemctl', '--user', 'disable', '--now', 'cpg-refresh.timer'] in env.runs
    assert S.load_state()['beta']['verdict'] == 'no_projects'


def test_check_beta_has_projects_turns_refresh_on(env):
    env.cli['probe-projects'] = {'signed_in': True, 'sidebar_entry': 'Projects Beta', 'browse_route_ok': True, 'project_list_cached': True, 'project_count': 2}
    r = S.cmd_check_beta(args(force=False))
    assert r['verdict'] == 'has_projects' and 'tell_user' not in r
    assert ['systemctl', '--user', 'enable', '--now', 'cpg-refresh.timer'] in env.runs


def test_check_beta_unknown_takes_screenshot(env):
    env.cli['probe-projects'] = {'signed_in': True, 'sidebar_entry': 'Projects', 'browse_route_ok': False}
    env.cli['screenshot'] = {'file': '/tmp/x.jpg'}
    r = S.cmd_check_beta(args(force=False))
    assert r['verdict'] == 'unknown' and r['screenshot'] == '/tmp/x.jpg'


# ---------------------------------------------------------------- host choice

@pytest.mark.parametrize('facts,kind,cls', [
    ({'os': 'linux'}, 'auto', 'always_on_linux'),
    ({'os': 'linux', 'laptop': True}, 'auto', 'laptop'),
    ({'os': 'linux', 'grok_bot_box': True}, 'auto', 'grok_bot_box'),
    ({'os': 'linux', 'wsl': True}, 'auto', 'windows_runtime'),
    ({'os': 'linux', 'lima': True}, 'auto', 'macos_runtime'),
    ({'os': 'macos'}, 'auto', 'macos_runtime'),
    ({'os': 'windows'}, 'auto', 'windows_runtime'),
    ({'os': 'freebsd'}, 'auto', 'unsupported'),
    ({'os': 'linux'}, 'box', 'grok_bot_box'),
])
def test_classify_host(facts, kind, cls):
    assert S.classify_host(facts, kind)[0] == cls


def test_recommended_host_ranks_first():
    ranks = {c: S.classify_host({'os': 'linux'}, k)[1] for c, k in
             (('linux', 'local'), ('mac', 'macos-vm'), ('win', 'windows-wsl'), ('laptop', 'laptop'), ('box', 'box'))}
    assert ranks['linux'] < ranks['mac'] == ranks['win'] < ranks['laptop'] < ranks['box']


def test_box_and_laptop_warn(env):
    assert S.always_on_checks({'os': 'linux'}, 'grok_bot_box')[0]['status'] == 'warn'
    assert 'only works while the box is up' in S.always_on_checks({'os': 'linux'}, 'grok_bot_box')[0]['detail']
    assert S.always_on_checks({'os': 'linux'}, 'laptop')[0]['status'] == 'warn'


def test_linux_preflight_old_distro_fails(env, monkeypatch):
    f = {'os': 'linux', 'arch': 'x86_64', 'python': '3.12.1', 'user': 'alex', 'hostname': 'h', 'distro_id': 'ubuntu',
         'version_id': '20.04', 'distro_like': 'debian', 'pretty': 'Ubuntu 20.04'}
    checks = {c['id']: c for c in S.linux_checks(f, 'always_on_linux')}
    assert checks['os']['status'] == 'fail' and checks['python']['status'] == 'pass'


def test_preflight_records_state(env, monkeypatch):
    monkeypatch.setattr(S, 'detect_platform', lambda: {'os': 'linux', 'arch': 'x86_64', 'python': '3.12.1', 'user': 'alex',
                                                      'hostname': 'h', 'distro_id': 'debian', 'version_id': '12', 'distro_like': '', 'pretty': 'Debian 12'})
    r = S.cmd_preflight(args(host_kind='auto'))
    assert r['host_class'] == 'always_on_linux' and {c['id'] for c in r['checks']} >= {'os', 'arch', 'systemd_user', 'sudo', 'disk', 'ports'}
    assert S.load_state()['preflight']['host_class'] == 'always_on_linux'


# ---------------------------------------------------------------- the state machine

def _installed(monkeypatch, units=True):
    monkeypatch.setattr(S, 'installed', lambda: True); monkeypatch.setattr(S, 'layout', lambda: 'standard')
    monkeypatch.setattr(S, 'unit_active', lambda u: units)
    for u in S.all_units(): (S.UNIT_DIR / f'{u}.service').write_text('[Service]\nExecStart=/bin/true\n')


def test_status_start_asks_host_question(env):
    r = S.compute_status()
    assert r['state'] == 'start' and r['ask_user'] == S.HOST_QUESTION and 'preflight' in r['next_action']
    assert 'always-on' in S.HOST_QUESTION and 'fallback' in S.HOST_QUESTION


def test_status_preflight_failed(env):
    S.save_state({'preflight': {'result': 'fail', 'failed': ['os']}, 'host': {'class': 'always_on_linux'}})
    r = S.compute_status(); assert r['state'] == 'preflight_failed' and r['failed_checks'] == ['os']


def test_status_installing_then_needs_sudo(env):
    S.save_state({'preflight': {'result': 'pass'}, 'host': {'class': 'always_on_linux'}})
    assert S.compute_status()['state'] == 'installing'
    S.save_state(dict(S.load_state(), needs_sudo='x'))
    r = S.compute_status(); assert r['state'] == 'needs_sudo' and '--apt-only' in r['ask_user']


def test_status_unhealthy(env, monkeypatch):
    _installed(monkeypatch, units=False)
    assert S.compute_status()['state'] == 'installed_unhealthy'


@pytest.mark.parametrize('app,state_extra,expected', [
    ({'signed_in': False, 'route': '/login'}, {}, 'awaiting_signin'),
    ({'signed_in': True, 'vnc_viewers': 1}, {}, 'close_viewer'),
    ({'signed_in': True}, {}, 'beta_check'),
    ({'signed_in': True}, {'beta': {'verdict': 'no_projects'}}, 'no_projects_beta'),
    ({'signed_in': True}, {'beta': {'verdict': 'app_outdated'}}, 'app_outdated'),
    ({'signed_in': True}, {'beta': {'verdict': 'has_projects'}}, 'connecting'),
    ({'signed_in': True}, {'beta': {'verdict': 'has_projects'}, 'connected': {'transport': 'ssh'}}, 'verifying'),
    ({'signed_in': True}, {'beta': {'verdict': 'has_projects'}, 'connected': {'transport': 'ssh'}, 'verify': {'result': 'warn', 'live': False}}, 'verifying'),
    ({'signed_in': True}, {'beta': {'verdict': 'has_projects'}, 'connected': {'transport': 'ssh'}, 'verify': {'result': 'warn', 'live': True}}, 'ready'),
])
def test_status_after_install(env, monkeypatch, app, state_extra, expected):
    _installed(monkeypatch); env.cli['status'] = app
    S.save_state(dict({'host': {'class': 'always_on_linux'}}, **state_extra))
    r = S.compute_status()
    assert r['state'] == expected
    assert r['state'] in S.STATES
    if expected == 'no_projects_beta': assert r['idle'] is True and "doesn't have Claude Code Projects" in r['ask_user']
    if expected == 'ready': assert r['next_action'] is None and 'Never send a test message' in r['offer']


def test_status_via_mcp_records_connection(env, monkeypatch):
    _installed(monkeypatch); env.cli['status'] = {'signed_in': True}
    S.save_state({'beta': {'verdict': 'has_projects'}})
    r = S.cmd_status(args(via_mcp=True))
    assert r['state'] == 'verifying' and S.load_state()['connected']['transport'] == 'mcp'


# ---------------------------------------------------------------- install

def test_install_needs_sudo(env, monkeypatch):
    monkeypatch.setattr(S, '_missing_apt', lambda: True)
    env.run_map = {'sudo -n true': (1, '', 'a password is required')}
    monkeypatch.setattr(S.os, 'geteuid', lambda: 1000); _preflight(monkeypatch)
    real_which = S.shutil.which
    monkeypatch.setattr(S.shutil, 'which', lambda n: '/usr/bin/sudo' if n == 'sudo' else real_which(n))
    r = S.cmd_install(args(no_apt=False, dry_run=False, bot_name='Scout'))
    assert r['result'] == 'needs_sudo' and r['ask_user'].count('sudo ') == 1 and '--apt-only' in r['ask_user']
    assert not any('install.sh' in ' '.join(c) and '--apt-only' not in ' '.join(c) for c in env.runs)
    # no sudo at all (a container): ask for whoever has root, never a sudo line
    monkeypatch.setattr(S.shutil, 'which', lambda n: None if n == 'sudo' else real_which(n))
    r = S.cmd_install(args(no_apt=False, dry_run=False, bot_name='Scout'))
    assert r['result'] == 'needs_sudo' and 'sudo ' not in r['ask_user'].replace('no sudo on', '') and 'root' in r['ask_user']


def _preflight(monkeypatch, result='pass', host_class='always_on_linux', mode='systemd', checks=None):
    monkeypatch.setattr(S, 'cmd_preflight', lambda a: {'result': result, 'host_class': host_class, 'service_mode': mode,
                                                       'checks': checks or [S.check('os', result, 'x', fix='do y')]})


def test_install_runs_installer_without_apt_when_packages_present(env, monkeypatch):
    monkeypatch.setattr(S, '_missing_apt', lambda: False); _preflight(monkeypatch)
    r = S.cmd_install(args(no_apt=False, dry_run=True, bot_name='Scout', timezone='America/New_York'))
    cmd = next(c for c in env.runs if c[0].endswith('install.sh'))
    assert '--no-apt' in cmd and '--dry-run' in cmd and cmd[cmd.index('--bot-name') + 1] == 'Scout'
    assert r['result'] == 'ok' and pathlib.Path(r['log']).is_file()


# ---------------------------------------------------------------- sign-in (noVNC)

def test_signin_start_never_prints_the_password(env):
    env.run_map['is-active'] = (0, 'active', '')
    (S.CONFIG_DIR / 'vncpass.txt').write_text('s3cretPW')
    env.cli['status'] = {'signed_in': False, 'route': '/login', 'vnc_viewers': 0}
    r = S.cmd_signin_start(args(switch=False, public_url=None))
    blob = json.dumps(r)
    assert 's3cretPW' not in blob and str(S.CONFIG_DIR / 'vncpass.txt') in blob
    assert r['result'] == 'awaiting_signin' and '/vnc.html' in r['links']['on_this_machine'] and 'ssh -N -L' in r['links']['from_another_computer']
    assert ['systemctl', '--user', 'enable', '--now', 'cpg-novnc.service'] in env.runs
    assert 'claude-open-url' in r['if_stuck_on_claude_link']


def test_signin_start_when_already_signed_in(env):
    env.cli['status'] = {'signed_in': True}
    assert S.cmd_signin_start(args(switch=False, public_url=None))['result'] == 'already_signed_in'


def test_signin_wait_then_viewport_off(env, monkeypatch):
    env.cli['status'] = [{'signed_in': False}, {'signed_in': True, 'vnc_viewers': 1}, {'signed_in': True, 'vnc_viewers': 0}]
    S.save_state({'beta': {'verdict': 'no_projects'}})
    r = S.cmd_signin_wait(args(timeout=60, poll=1, close_timeout=60))
    assert r['result'] == 'signed_in' and r['viewport']['viewport'] == 'off' and r['next_action'] == 'claude-projects setup check-beta'
    assert ['systemctl', '--user', 'disable', '--now', 'cpg-novnc.service'] in env.runs
    assert 'beta' not in S.load_state(), 'a new sign-in (maybe another account) must re-check Projects'


def test_signin_wait_timeout(env, monkeypatch):
    env.cli['status'] = {'signed_in': False, 'route': '/login'}
    clock = iter(range(0, 10_000, 400)); monkeypatch.setattr(S.time, 'time', lambda: next(clock))
    r = S.cmd_signin_wait(args(timeout=900, poll=30, close_timeout=60))
    assert r['result'] == 'signin_timeout'


def test_viewport_off_refuses_while_watched(env):
    env.cli['status'] = {'vnc_viewers': 1}
    r = S.viewport(False)
    assert 'refused' in r and not any('disable' in c for c in env.runs)


# ---------------------------------------------------------------- tidy

def test_tidy_dry_run_then_apply(env):
    (S.STATE_DIR / 'ui.holder').write_text(json.dumps({'pid': 999999999, 'op': 'send'}))
    old = S.STATE_DIR / '.cpg-idb-abc'; old.mkdir(); os.utime(old, (time.time() - 7200, time.time() - 7200))
    env.cli['staging'] = {'dry_run': True, 'removed_count': 0}; env.cli['status'] = {'signed_in': True}
    r = S.cmd_tidy(args(apply=False))
    assert r['dry_run'] and (S.STATE_DIR / 'ui.holder').exists() and old.exists()
    assert {a['what'].split(' (')[0] for a in r['actions']} >= {'staging sweep', 'stale UI-lock holder record'}
    r = S.cmd_tidy(args(apply=True))
    assert not (S.STATE_DIR / 'ui.holder').exists() and not old.exists()
    assert ['staging', 'sweep', '--apply'] in env.cli_calls
    assert not any('cpg-claude-desktop' in ' '.join(c) for c in env.runs), 'tidy never touches the app'


# ---------------------------------------------------------------- uninstall, connect

def test_uninstall_purge_needs_confirmation(env):
    r = S.cmd_uninstall(args(purge=True, yes=False))
    assert r['result'] == 'needs_confirmation' and not env.runs and S.CONFIG_DIR.exists()


def test_connect_box_recommends_same_host(env):
    S.save_state({'host': {'class': 'grok_bot_box'}})
    r = S.cmd_connect(args(mark=None))
    assert r['recommended'] == 'same_host' and r['options']['same_host']['command'].endswith('--stdio')
    assert 'token' not in r['options']['same_host']['command']


# ---------------------------------------------------------------- MCP tool, README, skill

def test_claude_setup_tool(monkeypatch):
    pytest.importorskip('mcp')
    import cpg_mcp as M
    seen = []
    async def fake_run(a, timeout=900, **kw): seen.append(a); return {'ok': True}
    monkeypatch.setattr(M, 'run', fake_run)
    asyncio.run(M.claude_setup('verify', live=True)); asyncio.run(M.claude_setup('tidy'))
    assert seen == [['setup', '--via-mcp', 'verify', '--live'], ['setup', '--via-mcp', 'tidy']]
    assert asyncio.run(M.claude_setup('uninstall'))['error'] == 'CPGError'


def test_readme_has_one_prompt_with_public_url():
    readme = (REPO / 'README.md').read_text()
    blocks = re.findall(r'```text\n(.*?)```', readme, re.S)
    assert len(blocks) == 1, 'exactly one copy-paste prompt'
    p = blocks[0]
    assert 'https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot' in p and 'claude-projects-mcp\n' not in p
    assert 'Part A' in p and 'claude-projects setup status' in p and 'test messages' in p and 'Claude Code Projects' in p


def test_skill_part_a_covers_the_flow():
    sk = (REPO / 'skill' / 'SKILL.md').read_text()
    a = sk[sk.index('# Part A'):sk.index('# Part B')]
    for needle in ('claude-projects setup preflight', 'claude-projects setup install', 'signin start', 'signin wait', 'check-beta', 'stay idle',
                   'claude-projects setup connect', 'verify --live', 'tidy', 'reconfigure', 'hosts/macos/setup.sh', 'hosts\\windows\\setup.ps1',
                   'claude-open-url', 'fallback'):
        assert needle in a, needle
    for cmd in re.findall(r'claude-projects setup ([a-z-]+)', a):
        assert cmd in S.build_parser()._subparsers._group_actions[0].choices, cmd


def test_cli_parses_every_documented_command():
    p = S.build_parser()
    for argv in (['status'], ['preflight', '--host-kind', 'box'], ['install', '--bot-name', 'x', '--no-apt'], ['signin', 'start', '--switch'],
                 ['signin', 'wait', '--timeout', '5'], ['check-beta'], ['connect'], ['verify', '--live'], ['viewport', 'off'],
                 ['tidy', '--apply'], ['reconfigure', '--timezone', 'UTC'], ['mark', 'connected', 'ssh'], ['move'], ['uninstall', '--purge', '--yes']):
        p.parse_args(argv)


def test_signin_start_without_browser_asks_first(env):
    env.cli['status'] = {'signed_in': False}
    env.run_map = {'claude-signin-browser --check': (127, '', 'none')}
    r = S.cmd_signin_start(args(switch=False, public_url=None))
    assert r['result'] == 'no_signin_browser' and 'May I install one?' in r['ask_user']
    assert not any('enable' in c for c in env.runs), 'no viewport before there is a browser'


def test_signin_browser_skips_snap_stubs(tmp_path):
    import subprocess
    b = tmp_path / 'bin'; b.mkdir()
    stub = b / 'chromium-browser'
    stub.write_text('#!/bin/sh\nif ! [ -x /snap/bin/chromium-not-here ]; then echo needs snap >&2; exit 1; fi\nexec /snap/bin/chromium-not-here "$@"\n'); stub.chmod(0o755)
    import shutil
    tools = tmp_path / 'tools'; tools.mkdir()
    for t in ('bash', 'env', 'grep', 'readlink', 'head', 'sh', 'mkdir', 'chmod'):
        (tools / t).symlink_to(shutil.which(t))
    env = {'PATH': f'{b}:{tools}', 'HOME': str(tmp_path)}
    r = subprocess.run([str(REPO / 'bin' / 'claude-signin-browser'), '--check'], env=env, capture_output=True, text=True)
    assert r.returncode == 127, r.stdout
    real = b / 'firefox-esr'; real.write_text('#!/bin/sh\nexit 0\n'); real.chmod(0o755)
    r = subprocess.run([str(REPO / 'bin' / 'claude-signin-browser'), '--check'], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip().endswith('firefox-esr')


# ---------------------------------------------------------------- host scripts: static checks (nothing is executed)

def test_powershell_scripts_parse():
    import shutil, subprocess
    pwsh = shutil.which('pwsh')
    if not pwsh: pytest.skip('pwsh not installed (CI runs tools/check-ps1.ps1)')
    r = subprocess.run([pwsh, '-NoProfile', '-File', str(REPO / 'tools' / 'check-ps1.ps1')], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


def test_shell_scripts_pass_bash_n():
    import subprocess
    for p in ('install.sh', 'claude-projects', 'ccp', 'hosts/macos/setup.sh', 'hosts/macos/claude-projects', 'hosts/macos/ccp', 'bin/claude-signin-browser', 'bin/claude-open-url', 'bin/claude-desktop-launch'):
        r = subprocess.run(['bash', '-n', str(REPO / p)], capture_output=True, text=True)
        assert r.returncode == 0, (p, r.stderr)


def test_windows_script_review_items():
    ps = (REPO / 'hosts' / 'windows' / 'setup.ps1').read_text()
    assert '$($BotName -replace' not in ps and '$($OperatorName -replace' not in ps, 'sanitize outside the string'
    assert 'NOPASSWD' not in ps and '-G sudo' not in ps, 'the distro user gets no sudo'
    assert 'conhost.exe' not in ps, 'no undocumented conhost --headless'
    assert '2.4.4' in ps and '--web-download' in ps and 'enable-linger cpg' in ps and 'seq 1 15' in ps
    assert (REPO / 'hosts' / 'windows' / 'claude-projects.cmd').read_bytes().count(b'\r\n') >= 3, 'claude-projects.cmd uses CRLF'


def test_macos_script_review_items():
    sh = (REPO / 'hosts' / 'macos' / 'setup.sh').read_text()
    assert '/opt/homebrew/bin:/usr/local/bin' in sh and 'brew shellenv' in sh
    assert 'start "$NAME" || true' not in sh and "'{{.Status}}'" in sh
    assert 'apt-get install -y -qq git ca-certificates' in sh
    assert '/tmp/claude-projects-vm.log' not in sh and 'Library/Logs' in sh
    import re as _re
    assert not _re.search(r'(?m)^\s*(run )?limactl ', sh), 'always call "$LIMACTL"'


def test_macos_flag_without_value_is_an_error(tmp_path):
    import subprocess
    r = subprocess.run(['bash', str(REPO / 'hosts' / 'macos' / 'setup.sh'), '--bot-name'], capture_output=True, text=True)
    assert r.returncode == 2 and 'needs a value' in r.stderr


def test_windows_scripts_never_travel_as_quoted_strings():
    """wsl.exe `--` expands $name against the guest env and PS 5.1 doesn't escape embedded double quotes, so every
    bash script goes through Invoke-WslScript (base64 + --exec) and no wsl.exe call carries bash -c / -lc text."""
    ps = (REPO / 'hosts' / 'windows' / 'setup.ps1').read_text()
    assert not re.search(r'"--",\s*"bash"', ps)
    for m in re.finditer(r'Invoke-Wsl\s+@\((.*?)\)', ps):
        args = m.group(1)
        if '"--exec", "/bin/bash", "-c", "echo $b64 | base64 -d | $bash"' in args: continue
        assert '"bash"' not in args and '"-c"' not in args and '"-lc"' not in args, args
    fn = ps[ps.index('function Invoke-WslScript'):ps.index('function Get-WslVersion')]
    assert 'ToBase64String' in fn and '"--exec"' in fn and '"--cd"' not in fn
    assert '& wsl.exe --version 2>$null' not in ps and '& wsl.exe --status' not in ps, 'stderr guarded via Invoke-WslQuiet'
    q = ps[ps.index('function Invoke-WslQuiet'):ps.index('function Invoke-WslScript')]
    assert '$ErrorActionPreference = "Continue"' in q and 'catch' in q


def test_windows_script_arrives_intact_through_fake_wsl(tmp_path):
    """With pwsh available (CI): run Invoke-WslScript against a fake wsl.exe that execs after --exec without a shell."""
    import shutil, subprocess
    pwsh = shutil.which('pwsh')
    if not pwsh: pytest.skip('pwsh not installed')
    b = tmp_path / 'bin'; b.mkdir(); shutil.copy(REPO / 'tests' / 'fixtures' / 'fake-wsl.exe', b / 'wsl.exe'); (b / 'wsl.exe').chmod(0o755)
    t = tmp_path / 't.ps1'
    t.write_text(r'''$ErrorActionPreference = "Stop"
$src = Get-Content "%s" -Raw
$funcs = $src.Substring($src.IndexOf('function Say'), $src.IndexOf('# 0. The ref') - $src.IndexOf('function Say'))
$Distro = "ClaudeProjects"; $DryRun = $false
Invoke-Expression $funcs
$script = @'
s="$(echo running)"
case "$s" in running|degraded) echo "case matched: $s";; *) echo "case missed";; esac
echo "quotes \"ok\" and 'single'"
'@
Invoke-WslScript root $script
"version " + (Get-WslVersion)
''' % (REPO / 'hosts' / 'windows' / 'setup.ps1'))
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")
    r = subprocess.run([pwsh, '-NoProfile', '-File', str(t)], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'case matched: running' in r.stdout and 'quotes "ok" and \'single\'' in r.stdout and 'version 2.4.4.0' in r.stdout


# ---------------------------------------------------------------- platform guidance in the skill (ships publicly)

def test_skill_has_mac_and_windows_guidance_with_references():
    sk = (REPO / 'skill' / 'SKILL.md').read_text()
    for ref in ('references/macos.md', 'references/windows.md'):
        assert f']({ref})' in sk and (REPO / 'skill' / ref).is_file(), ref
    mac = (REPO / 'skill' / 'references' / 'macos.md').read_text()
    for must in ('limactl create', '--mount-none', 'template://ubuntu-24.04', 'enable-linger', 'launchctl bootstrap',
                 'vncpass.txt', '127.0.0.1:6179', 'pmset', 'setup preflight', 'setup install', 'signin start',
                 'check-beta', 'verify', 'Common failures', 'Get started'):
        assert must in mac, f'macos.md: {must}'
    win = (REPO / 'skill' / 'references' / 'windows.md').read_text()
    for must in ('wsl --install', '--name ClaudeProjects', 'systemd=true', '/etc/wsl.conf', 'enable-linger', 'base64',
                 '--exec', 'Register-ScheduledTask', 'sleep infinity', 'localhostForwarding', 'vncpass.txt',
                 '127.0.0.1:6179', 'powercfg', 'install.sh --apt-only', 'setup install --no-apt', 'signin start',
                 'check-beta', 'verify', 'Common failures', 'Get started'):
        assert must in win, f'windows.md: {must}'
    # the Mac commands that cross into the VM keep ~ and $ for the VM's shell
    assert "limactl shell claude-projects -- cat ~" not in mac and "limactl shell claude-projects -- ~/" not in mac


def test_every_tool_count_says_24():
    files = [REPO / 'README.md', REPO / 'INSTALL.md', REPO / 'CHANGELOG.md', *(REPO / 'docs').glob('*.md'),
             *(REPO / 'skill').rglob('*.md')]
    seen = []
    for f in files:
        if not f.is_file(): continue
        text = f.read_text()
        if f.name == 'CHANGELOG.md': text = text.split('\n## ', 2)[1]   # only the current release; older ones are history
        for m in re.finditer(r'\b(\d+)\s+(?:MCP\s+)?tools\b|\((\d+) tools\)', text):
            seen.append((f.name, int(m.group(1) or m.group(2))))
    assert seen and all(n == S.EXPECTED_TOOLS == 24 for _, n in seen), seen


# ---------------------------------------------------------------- findings from a real Windows runner (PR 9)

def test_windows_distro_preflight_does_not_warn_about_sudo(env, monkeypatch):
    f = {'os': 'linux', 'arch': 'x86_64', 'python': '3.12.1', 'user': 'claude-projects', 'hostname': 'h', 'distro_id': 'ubuntu',
         'version_id': '24.04', 'distro_like': 'debian', 'pretty': 'Ubuntu 24.04', 'wsl': True, 'pid1': 'systemd'}
    monkeypatch.setattr(S.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(S, 'RUN', lambda cmd, timeout=60, env=None, input=None: (1, '', 'a password is required') if cmd[0] == 'sudo' else (0, 'yes', ''))
    monkeypatch.setattr(S, 'detect_platform', lambda: f)
    c = {x['id']: x for x in S.linux_checks(f, 'windows_runtime')}['sudo']
    assert c['status'] == 'pass' and c['detail'] == 'root steps run from Windows with wsl -u root; no sudo needed'
    assert 'wsl.exe -d' in c['command'] and '-u root' in c['command'] and 'fix' not in c
    other = {x['id']: x for x in S.linux_checks(f, 'always_on_linux')}['sudo']
    assert other['status'] == 'warn', 'elsewhere a password-only sudo still warns'


def _browser_pick(tmp_path, policies, candidates=None):
    import subprocess
    b = tmp_path / 'bin'; b.mkdir(exist_ok=True)
    cases = ''.join(f'  {p}) printf "{p}:\\n  Installed: (none)\\n  Candidate: {c}\\n";;\n' for p, c in policies.items())
    (b / 'apt-cache').write_text(f'#!/bin/bash\ncase "$2" in\n{cases}  *) ;;\nesac\n'); (b / 'apt-cache').chmod(0o755)
    sh = (REPO / 'install.sh').read_text()
    fn = sh[sh.index('apt_browser_pkg() {'):]
    fn = fn[:fn.index('\n}\n') + 3]
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")
    if candidates: env['CPG_BROWSER_CANDIDATES'] = candidates
    r = subprocess.run(['bash', '-c', fn + 'apt_browser_pkg'], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stderr == ''
    return r.stdout.strip()


def test_install_picks_a_browser_deb_that_exists(tmp_path):
    # Ubuntu 24.04: neither deb has a candidate -> nothing (the snap path), and apt is never asked to install it
    assert _browser_pick(tmp_path, {'firefox-esr': '(none)', 'chromium': '(none)'}) == ''
    assert _browser_pick(tmp_path, {}) == '', 'unknown package: no output at all'
    assert _browser_pick(tmp_path, {'firefox-esr': '115.12.0esr-1~deb12u1', 'chromium': '126.0-1'}) == 'firefox-esr'   # Debian
    assert _browser_pick(tmp_path, {'firefox-esr': '(none)', 'chromium': '126.0-1'}) == 'chromium'
    sh = (REPO / 'install.sh').read_text()
    assert 'firefox-esr || run' not in sh, 'no blind apt install that prints "no installation candidate"'
    assert '"$BROWSER_PKG"' in sh and 'snap install chromium' in sh
