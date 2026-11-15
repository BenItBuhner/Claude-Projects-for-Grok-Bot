"""1.0.3 layout: one package (claude_projects/), one CLI (claude-projects), claude-projects-* units, CLAUDE_PROJECTS_* env.
No file or module carries an internal short prefix, and the only places the old names appear are the migration code."""
import os, pathlib, re, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
from claude_projects import config as cfg_mod

REPO = pathlib.Path(__file__).resolve().parents[1]
OLD = re.compile(r'(?<![A-Za-z])(cpg|ccp)(?![A-Za-z])|\b(CPG|CCP)_', re.I)
# the files allowed to name the old prefixes, each for migration only
MIGRATION = {'CHANGELOG.md', 'install.sh', 'claude_projects/config.py', 'hosts/macos/setup.sh', 'hosts/macos/claude-projects',
             'hosts/windows/setup.ps1', 'tests/test_layout.py'}


def tracked():
    r = subprocess.run(['git', 'ls-files'], cwd=REPO, capture_output=True, text=True)
    files = [f for f in r.stdout.splitlines() if f] if r.returncode == 0 else \
        [str(p.relative_to(REPO)) for p in REPO.rglob('*') if p.is_file() and '.git' not in p.parts and '.venv' not in p.parts]
    return files


def test_no_file_or_module_name_has_an_old_prefix():
    bad = [f for f in tracked() if OLD.search(pathlib.PurePath(f).name)]
    assert bad == [], bad


def test_old_names_only_in_migration_code():
    hits = []
    for f in tracked():
        if f in MIGRATION or f.startswith(('docs/assets/', 'tests/fixtures/')): continue
        p = REPO / f
        try: text = p.read_text()
        except (UnicodeDecodeError, OSError): continue
        hits += [f'{f}:{i}' for i, ln in enumerate(text.splitlines(), 1) if OLD.search(ln)]
    assert hits == [], hits


def test_package_layout_and_entry_points():
    pkg = REPO / 'claude_projects'
    for m in ('__init__', '__main__', 'core', 'config', 'idb', 'mcp_server', 'origin', 'permissions', 'policy', 'proc', 'setup', 'staging'):
        assert (pkg / f'{m}.py').is_file(), m
    assert not list(REPO.glob('*.py')), 'no modules at the top level'
    assert 'claude-projects = "claude_projects.__main__:main"' in (REPO / 'pyproject.toml').read_text()
    assert 'exec "$PY" -m claude_projects "$@"' in (REPO / 'claude-projects').read_text()
    mcp = (REPO / 'systemd' / 'claude-projects-mcp.service').read_text()
    assert 'ExecStart=@REPO@/.venv/bin/python -m claude_projects.mcp_server' in mcp and 'Environment=PYTHONPATH=@REPO@' in mcp
    assert sorted(p.name for p in (REPO / 'systemd').iterdir()) == sorted(
        [f'claude-projects-{u}.service' for u in ('xvfb', 'wm', 'keyring', 'desktop', 'vnc', 'novnc', 'mcp', 'refresh')] + ['claude-projects-refresh.timer'])


def test_cli_runs_as_a_module():
    env = dict(os.environ, PYTHONPATH=str(REPO))
    r = subprocess.run([sys.executable, '-m', 'claude_projects', 'refresh', '--help'], capture_output=True, text=True, timeout=60, env=env, cwd='/')
    assert r.returncode == 0 and '--background' in r.stdout, r.stderr
    r = subprocess.run([sys.executable, '-m', 'claude_projects', 'setup', '--help'], capture_output=True, text=True, timeout=60, env=env, cwd='/')
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, str(REPO / 'claude_projects' / 'proc.py'), '--help'], capture_output=True, text=True, timeout=60, cwd='/')
    assert r.returncode == 0, r.stderr


def test_child_processes_find_the_package():
    assert str(cfg_mod.REPO_DIR) in os.environ['PYTHONPATH'].split(os.pathsep)
    assert cfg_mod.REPO_DIR == REPO
    assert cfg_mod.module_argv('claude_projects.idb', '/py') == ['/py', '-m', 'claude_projects.idb']


def test_legacy_env_names_are_read_when_the_new_one_is_unset():
    e = {'CPG_BOT_NAME': 'Old', 'CCP_TIMEZONE': 'UTC', 'CLAUDE_PROJECTS_DISPLAY': ':5', 'CPG_DISPLAY': ':9'}
    cfg_mod.env_aliases(e)
    assert e['CLAUDE_PROJECTS_BOT_NAME'] == 'Old' and e['CLAUDE_PROJECTS_TIMEZONE'] == 'UTC'
    assert e['CLAUDE_PROJECTS_DISPLAY'] == ':5'   # the new name wins


def test_installer_migrates_old_units_and_shims():
    s = (REPO / 'install.sh').read_text()
    assert 'for p in cpg ccp; do for u in xvfb wm keyring claude-desktop vnc novnc mcp refresh' in s
    assert 'remove_old_units' in s and '"$REPO"/ccp_*.py "$REPO"/cpg_*.py' in s and '$HOME/.local/bin/ccp' in s
    assert 'CPG_*|CCP_*) n="CLAUDE_PROJECTS_${k#???_}"' in s
    ps = (REPO / 'hosts' / 'windows' / 'setup.ps1').read_text()
    assert '$RuntimeUser = $existingUser' in ps and 'usermod' not in ps and 'pkill' not in ps
