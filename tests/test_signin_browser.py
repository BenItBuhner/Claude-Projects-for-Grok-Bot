"""The sign-in browser pick (bin/claude-signin-browser): a real chromium snap, the Ubuntu deb stub, and nothing.

Snap layout faked in a temp dir: CLAUDE_PROJECTS_SNAP_BIN stands in for /snap/bin, a fake `snap` answers `snap list <name>`, and
PATH never contains the snap dir, as under a systemd user manager or `wsl -u root` (the bug: verify said "no usable
browser" with the chromium snap installed)."""
import pathlib, shutil, subprocess
import pytest

from claude_projects import proc as proc_mod, setup as S

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'bin' / 'claude-signin-browser'
STUB = ('#!/bin/sh\n# the chromium-browser deb on Ubuntu\nif ! [ -x /snap/bin/chromium ]; then echo "needs the chromium snap" >&2; exit 1; fi\n'
        'exec /snap/bin/chromium "$@"\n')


@pytest.fixture
def box(tmp_path):
    usr = tmp_path / 'usr-bin'; usr.mkdir()
    snapbin = tmp_path / 'snap-bin'
    tools = tmp_path / 'tools'; tools.mkdir()
    for t in ('bash', 'env', 'grep', 'readlink', 'head', 'sh', 'mkdir', 'chmod'):
        (tools / t).symlink_to(shutil.which(t))
    listed = tmp_path / 'snaps-listed'; listed.write_text('')
    snap = tools / 'snap'   # like /usr/bin/snap: `snap list NAME` exits 0 only for installed snaps
    snap.write_text(f'#!/bin/sh\n[ "$1" = list ] && grep -qx "$2" {listed} && exit 0\nexit 1\n'); snap.chmod(0o755)

    class Box:
        def install_snap(self, name, listed_too=True):
            snapbin.mkdir(exist_ok=True)
            (snapbin / name).symlink_to(snap)          # /snap/bin/chromium -> /usr/bin/snap, as snapd lays it out
            if listed_too: listed.write_text(listed.read_text() + name + '\n')

        def put(self, name, text):
            p = usr / name; p.write_text(text); p.chmod(0o755)

        def check(self, path_has_snap=False):
            path = f'{usr}:{tools}' + (f':{snapbin}' if path_has_snap else '')
            env = {'PATH': path, 'HOME': str(tmp_path), 'CLAUDE_PROJECTS_SNAP_BIN': str(snapbin)}
            return subprocess.run([str(SCRIPT), '--check'], env=env, capture_output=True, text=True)
    b = Box(); b.snapbin = snapbin
    return b


def test_real_snap_found_without_snap_bin_on_path(box):
    box.install_snap('chromium')
    r = box.check()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f'{box.snapbin}/chromium'


def test_real_snap_with_deb_stub_beside_it(box):
    box.put('chromium-browser', STUB.replace('/snap/bin', str(box.snapbin)))
    box.install_snap('chromium')
    r = box.check()
    assert r.returncode == 0 and r.stdout.strip() == f'{box.snapbin}/chromium', r.stderr


def test_real_snap_already_on_path(box):
    box.install_snap('chromium')
    r = box.check(path_has_snap=True)
    assert r.returncode == 0 and r.stdout.strip() == f'{box.snapbin}/chromium'


def test_deb_stub_only_is_not_a_browser(box):
    box.put('chromium-browser', STUB)
    r = box.check()
    assert r.returncode == 127 and r.stdout == ''
    assert 'deb stub' in r.stderr


def test_snap_launcher_that_snapd_does_not_list(box):
    box.install_snap('chromium', listed_too=False)   # a leftover /snap/bin entry, or snapd not running
    r = box.check()
    assert r.returncode == 127 and "doesn't list it" in r.stderr


def test_nothing_installed(box):
    r = box.check()
    assert r.returncode == 127 and 'no browser installed' in r.stderr


def test_real_binary_still_wins(box):
    box.put('chromium', '\x7fELF fake binary\n')
    r = box.check()
    assert r.returncode == 0 and r.stdout.strip().endswith('usr-bin/chromium')


def test_firefox_snap_counts_too(box):
    box.install_snap('firefox')
    r = box.check()
    assert r.returncode == 0 and r.stdout.strip() == f'{box.snapbin}/firefox'


def test_setup_check_names_the_reason(monkeypatch):
    err = ('claude-signin-browser: no usable browser: only the chromium-browser deb stub (its snap isn\'t installed) '
           '(install chromium or firefox-esr; on Ubuntu: sudo snap install chromium)\n')
    monkeypatch.setattr(S, 'RUN', lambda cmd, timeout=60, env=None, input=None: (127, '', err))
    monkeypatch.setattr(S, 'detect_platform', lambda: {'distro_id': 'ubuntu'})
    c = S.signin_browser_check()
    assert c['status'] == 'fail' and 'deb stub' in c['detail'] and c['fix'] == 'sudo snap install chromium'
    monkeypatch.setattr(S, 'RUN', lambda cmd, timeout=60, env=None, input=None: (0, '/snap/bin/chromium\n', ''))
    assert S.signin_browser_check()['status'] == 'pass'


def test_snap_bin_on_path_for_cli_supervisor_and_units(monkeypatch, tmp_path):
    cli = (REPO / 'claude-projects').read_text()
    assert 'PATH="$PATH:/snap/bin"' in cli
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    assert '/snap/bin' in proc_mod.runtime_env()['PATH'].split(':')
    monkeypatch.setenv('PATH', '/usr/bin:/snap/bin:/bin')
    assert proc_mod.runtime_env()['PATH'].split(':').count('/snap/bin') == 1
    for u in ('claude-projects-desktop', 'claude-projects-mcp', 'claude-projects-refresh'):
        txt = (REPO / 'systemd' / f'{u}.service').read_text()
        line = next(ln for ln in txt.splitlines() if ln.startswith('Environment=PATH='))
        assert line.endswith(':/snap/bin') and '/usr/bin' in line, u
    # the process supervisor reads the same unit files: %h expands, /snap/bin stays
    monkeypatch.setattr(proc_mod, 'UNIT_DIR', REPO / 'systemd')
    u = proc_mod.parse_unit('claude-projects-mcp', {'XDG_RUNTIME_DIR': str(tmp_path), 'HOME': '/home/someone'})
    assert u['env']['PATH'].startswith('/home/someone/.local/bin:') and u['env']['PATH'].endswith(':/snap/bin')
