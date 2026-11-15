"""The 1.0.2 rename (ccp -> claude-projects CLI, cpg_* modules, cpg-* units, CPG_* env): every old name still works.

What has to keep working unchanged after a deploy of this code over an older install:
- the hand-built layout: claude-projects-mcp.service runs `<dir>/venv/bin/python <dir>/ccp_mcp.py`,
  claude-projects-refresh.service runs `<dir>/ccp refresh --background --max 3`, ~/.local/bin/ccp -> <dir>/ccp,
  and deploy scripts call `<dir>/ccp --json status`;
- install.sh installs from before 1.0.2, whose units are ccp-*;
- CCP_* environment variables, a Mac VM called "ccp", the ccp / ccp.cmd host wrappers."""
import importlib, os, pathlib, shutil, subprocess, sys
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
import cpg_setup as S
import cpg_config
from test_setup import env, args  # noqa: F401  (fixtures and helpers)

REPO = pathlib.Path(__file__).resolve().parents[1]
MODULES = ['config', 'core', 'idb', 'mcp', 'origin', 'permissions', 'policy', 'staging', 'setup', 'proc']
PY = str(REPO / '.venv' / 'bin' / 'python') if (REPO / '.venv' / 'bin' / 'python').exists() else sys.executable


# ---------------------------------------------------------------- module shims

@pytest.mark.parametrize('m', [m for m in MODULES if m not in ('idb', 'mcp')])
def test_old_module_name_is_the_same_module(m):
    new = importlib.import_module(f'cpg_{m}')
    old = importlib.import_module(f'ccp_{m}')
    assert old is new, f'ccp_{m} must be cpg_{m} itself, so patches and globals are shared'


def test_every_module_has_a_shim_that_runs_it_as_a_script():
    for m in MODULES:
        text = (REPO / f'ccp_{m}.py').read_text()
        assert f"runpy.run_module('cpg_{m}', run_name='__main__', alter_sys=True)" in text
        assert f"importlib.import_module('cpg_{m}')" in text
        assert (REPO / f'cpg_{m}.py').is_file()


def test_old_entry_points_run_for_real():
    # what the hand-built layout's units and a deploy script run; --help parses without touching the app
    for argv in (['ccp_core.py', 'refresh', '--help'], ['ccp_core.py', '--json', 'status', '--help'],
                 ['ccp_proc.py', '--help'], ['ccp_setup.py', 'setup', '--help']):
        r = subprocess.run([PY, str(REPO / argv[0]), *argv[1:]], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (argv, r.stderr)
    r = subprocess.run([PY, str(REPO / 'ccp_core.py'), 'refresh', '--help'], capture_output=True, text=True, timeout=60)
    assert '--background' in r.stdout and '--max' in r.stdout


def test_ccp_mcp_shim_runs_the_server_module(tmp_path):
    # running the server would bind a port; check instead that the shim hands over to cpg_mcp as __main__
    t = tmp_path / 'probe.py'
    t.write_text('import runpy, sys\nseen = {}\nreal = runpy.run_module\n'
                 'runpy.run_module = lambda name, **kw: seen.update(name=name, **kw)\n'
                 f'sys.argv = [{str(REPO / "ccp_mcp.py")!r}, "--stdio"]\n'
                 f'runpy.run_path({str(REPO / "ccp_mcp.py")!r}, run_name="__main__")\nprint(seen)\n')
    r = subprocess.run([PY, str(t)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "'name': 'cpg_mcp'" in r.stdout and "'run_name': '__main__'" in r.stdout


# ---------------------------------------------------------------- CLI shims

def test_ccp_cli_shim_and_a_symlink_to_it(tmp_path):
    new = subprocess.run([str(REPO / 'claude-projects'), 'refresh', '--help'], capture_output=True, text=True, timeout=60)
    old = subprocess.run([str(REPO / 'ccp'), 'refresh', '--help'], capture_output=True, text=True, timeout=60)
    assert new.returncode == old.returncode == 0 and new.stdout == old.stdout
    link = tmp_path / 'bin' / 'ccp'; link.parent.mkdir(); link.symlink_to(REPO / 'ccp')   # like ~/.local/bin/ccp
    r = subprocess.run([str(link), '--json', 'status', '--help'], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([str(link), 'setup', '--help'], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and 'status' in r.stdout


def test_windows_wrappers():
    new = (REPO / 'hosts' / 'windows' / 'claude-projects.cmd').read_bytes()
    old = (REPO / 'hosts' / 'windows' / 'ccp.cmd').read_bytes()
    for b in (new, old): assert b.count(b'\r\n') == b.count(b'\n') >= 3, 'cmd files use CRLF'
    assert b'-u cpg --exec /home/cpg/claude-projects-mcp/claude-projects %*' in new and b'%CCP_WSL_DISTRO%' in new
    assert b'call "%~dp0claude-projects.cmd" %*' in old


def mac_limactl(tmp_path, vms):
    b = tmp_path / 'stub'; b.mkdir(exist_ok=True); log = tmp_path / 'limactl.log'
    names = ' '.join(vms)
    (b / 'limactl').write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> ' + str(log) + '\n'
                               'if [ "$1" = list ]; then for v in ' + names + '; do [ "$v" = "${@: -1}" ] && echo "$v"; done; fi\nexit 0\n')
    (b / 'limactl').chmod(0o755)
    return b, log


@pytest.mark.parametrize('vms,want', [(['ccp'], 'ccp'), (['claude-projects'], 'claude-projects'),
                                      (['ccp', 'claude-projects'], 'claude-projects'), ([], 'claude-projects')])
def test_mac_wrapper_and_its_shim_find_the_vm(tmp_path, vms, want):
    b, log = mac_limactl(tmp_path, vms)
    env = {k: v for k, v in os.environ.items() if not k.endswith('LIMA_NAME')}
    env['PATH'] = f"{b}:{env['PATH']}"
    for wrapper in ('claude-projects', 'ccp'):
        if log.exists(): log.unlink()
        r = subprocess.run(['bash', str(REPO / 'hosts' / 'macos' / wrapper), 'setup', 'status'], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        shell = [ln for ln in log.read_text().splitlines() if ln.startswith('shell ')]
        assert shell and shell[0].startswith(f'shell {want} -- bash -lc '), shell
        assert shell[0].endswith('claude-projects setup status')
    r = subprocess.run(['bash', str(REPO / 'hosts' / 'macos' / 'claude-projects'), 'x'], capture_output=True, text=True,
                       env=dict(env, CCP_LIMA_NAME='old-name'))
    assert 'shell old-name ' in log.read_text(), 'the old CCP_LIMA_NAME still picks the VM'


# ---------------------------------------------------------------- environment variables

def test_old_env_names_still_work_and_the_new_ones_win(tmp_path):
    f = tmp_path / 'missing.json'
    assert cpg_config.load(f, environ={'CCP_DISPLAY': ':7'})['display'] == ':7'
    assert cpg_config.load(f, environ={'CCP_DISPLAY': ':7', 'CPG_DISPLAY': ':8'})['display'] == ':8'
    assert cpg_config.load(f, environ={'CPG_MCP_PORT': '9100'})['mcp_port'] == 9100
    e = cpg_config.env_aliases({'CCP_REPO_REF': 'v1', 'CCP_X': 'a', 'CPG_X': 'b'})
    assert e['CPG_REPO_REF'] == 'v1' and e['CPG_X'] == 'b'


def test_install_sh_maps_old_env_names(tmp_path):
    sh = (REPO / 'install.sh').read_text()
    line = next(ln for ln in sh.splitlines() if 'compgen -e' in ln)
    r = subprocess.run(['bash', '-c', 'set -u\n' + line + '\necho "$CPG_CONFIG|$CPG_STATE_DIR"'], capture_output=True, text=True,
                       env=dict({k: v for k, v in os.environ.items() if not k.startswith(('CPG_', 'CCP_'))},
                                CCP_CONFIG='/old/c.json', CCP_STATE_DIR='/old/s', CPG_STATE_DIR='/new/s'))
    assert r.returncode == 0 and r.stdout.strip() == '/old/c.json|/new/s', r.stderr


# ---------------------------------------------------------------- installs from before the rename

def _units(d, prefix):
    for u in ('xvfb', 'wm', 'keyring', 'claude-desktop', 'vnc', 'novnc', 'mcp', 'refresh'):
        (d / f'{prefix}-{u}.service').write_text('[Service]\nExecStart=/bin/true\n')
    (d / f'{prefix}-refresh.timer').write_text('[Timer]\nOnBootSec=5min\n')


def test_old_ccp_units_are_recognized_until_install_sh_renames_them(env, monkeypatch, tmp_path):
    repo = tmp_path / 'repo'; (repo / '.venv' / 'bin').mkdir(parents=True); (repo / '.venv' / 'bin' / 'python').write_text('')
    monkeypatch.setattr(S, 'REPO', repo)
    cfg = tmp_path / 'config.json'; cfg.write_text('{}'); monkeypatch.setattr(S, 'CONFIG_FILE', cfg)
    _units(S.UNIT_DIR, 'ccp')
    assert S.unit_prefix() == 'ccp' and S.layout() == 'standard'
    assert S.core_units() == ['ccp-xvfb', 'ccp-wm', 'ccp-keyring', 'ccp-claude-desktop'] and S.mcp_unit() == 'ccp-mcp'
    monkeypatch.setattr(S, 'unit_active', lambda u: u.startswith('ccp-'))
    monkeypatch.setattr(S, 'unit_enabled', lambda u: u.startswith('ccp-'))
    env.cli['status'] = {'signed_in': True, 'route': '/'}
    S.save_state({'host': {'class': 'always_on_linux'}, 'beta': {'verdict': 'has_projects'}})
    st = S.compute_status()
    assert st['installed'] and st['layout'] == 'standard' and st['state'] != 'installed_unhealthy', st
    monkeypatch.setattr(S, 'mcp_tool_names', lambda: ['claude_setup'] * 24)
    v = {c['id']: c for c in S.cmd_verify(args(live=False))['checks']}
    assert v['unit:ccp-xvfb']['status'] == 'pass' and not any(k.startswith('unit:cpg-') for k in v)
    _units(S.UNIT_DIR, 'cpg')                       # install.sh ran: the new names take over
    assert S.unit_prefix() == 'cpg' and S.core_units()[0] == 'cpg-xvfb'


def test_supervisor_runs_old_ccp_units_when_they_are_all_there(tmp_path, monkeypatch):
    import cpg_proc as P
    monkeypatch.setattr(P, 'UNIT_DIR', tmp_path)
    _units(tmp_path, 'ccp')
    assert set(P.known_units()) >= {'cpg-dbus', 'ccp-xvfb', 'ccp-claude-desktop'}
    _units(tmp_path, 'cpg')
    names = P.known_units()
    assert 'cpg-xvfb' in names and not any(n.startswith('ccp-') for n in names), 'both present: only the new set runs'


def test_install_sh_removes_old_ccp_units_before_starting_new_ones(tmp_path):
    units = tmp_path / 'units'; units.mkdir(); _units(units, 'ccp'); _units(units, 'cpg')
    sh = (REPO / 'install.sh').read_text()
    olds = next(ln for ln in sh.splitlines() if ln.startswith('OLD_UNITS='))
    fn = sh[sh.index('remove_old_units() {'):]; fn = fn[:fn.index('\n}\n') + 3]
    log = tmp_path / 'svc.log'
    script = f'''set -u
UNIT_DIR={units}; MODE=systemd
say() {{ echo "==> $*"; }}
run() {{ "$@"; }}
SVC() {{ echo "$*" >> {log}; }}
{olds}
{fn}
remove_old_units
remove_old_units   # a second run finds nothing to do
'''
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    left = sorted(p.name for p in units.iterdir())
    assert left and all(n.startswith('cpg-') for n in left), left
    calls = log.read_text()
    assert 'disable --now ccp-xvfb.service' in calls and 'disable --now ccp-refresh.timer' in calls and 'cpg-' not in calls
    assert r.stdout.count('Moving the services to their new names') == 1
    assert sh.index('remove_old_units\nfor f in "$REPO"/systemd/*') > 0, 'runs before the new units are rendered'


def test_legacy_hand_built_layout_entry_points_exist():
    # claude-projects-mcp.service / claude-projects-refresh.service in the hand-built layout run these by name
    for f in ('ccp_mcp.py', 'ccp', 'ccp_core.py'):
        assert (REPO / f).is_file()
    assert os.access(REPO / 'ccp', os.X_OK) and os.access(REPO / 'claude-projects', os.X_OK)


# ---------------------------------------------------------------- AGENTS.md

def test_agents_md_is_canonical_and_claude_md_only_imports_it():
    agents = (REPO / 'AGENTS.md').read_text()
    assert '[sent by Grok Bot]' in agents and 'not a password or a permission grant' in agents
    lines = [ln for ln in (REPO / 'CLAUDE.md').read_text().splitlines() if ln.strip() and not ln.startswith('<!--')]
    assert lines == ['@AGENTS.md']
    assert 'cat "$REPO/AGENTS.md"' in (REPO / 'install.sh').read_text()
