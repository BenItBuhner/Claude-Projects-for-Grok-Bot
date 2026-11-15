"""hosts/windows/setup.ps1 against old and odd wsl.exe builds (from a run on GitHub's windows-2022 runner).

The old inbox wsl.exe (Windows Server 2022, build 20348) has no `--version`, no `--install --no-distribution`, defaults
to WSL 1 and prints UTF-16LE whatever WSL_UTF8 says. These run setup.ps1 with pwsh against tests/fixtures/fake-wsl.exe,
which replays raw byte fixtures (tests/fixtures/wsl-output) and never runs anything after --exec (FAKE_WSL_NO_EXEC)."""
import os, pathlib, shutil, subprocess
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
WIN = REPO / 'hosts' / 'windows' / 'setup.ps1'
OUT = REPO / 'tests' / 'fixtures' / 'wsl-output'
PWSH = shutil.which('pwsh')
pytestmark = pytest.mark.skipif(not PWSH, reason='pwsh not installed (CI runs these)')
VERSION = 'WSL version: 2.4.4.0\nKernel version: 5.15.167.4-1\nWSLg version: 1.0.65\nWindows version: 10.0.26100.1\n'
STACK = ('CategoryInfo', 'FullyQualifiedErrorId', 'char:', 'ScriptStackTrace', '+ ~~~')


def fake(tmp_path, replies):
    """replies: {wsl-args-joined-by-_: (fixture name or bytes or None, exit code)}"""
    b = tmp_path / 'bin'; b.mkdir(exist_ok=True)
    shutil.copy(REPO / 'tests' / 'fixtures' / 'fake-wsl.exe', b / 'wsl.exe'); (b / 'wsl.exe').chmod(0o755)
    d = tmp_path / 'replies'; d.mkdir(exist_ok=True)
    for key, (out, code) in replies.items():
        if out is not None: (d / f'{key}.out').write_bytes(out if isinstance(out, bytes) else (OUT / out).read_bytes())
        (d / f'{key}.code').write_text(str(code))
    env = {**os.environ, 'PATH': f"{b}:{os.environ['PATH']}", 'FAKE_WSL_DIR': str(d), 'FAKE_WSL_NO_EXEC': '1',
           'TMPDIR': str(tmp_path)}
    env.pop('CPG_REPO_REF', None)
    return env, tmp_path / 'fake-wsl-argv.log'


def run_setup(env, *args):
    r = subprocess.run([PWSH, '-NoProfile', '-File', str(WIN), *args], capture_output=True, text=True, env=env, timeout=180)
    return r, r.stdout + r.stderr


def run_funcs(tmp_path, env, body):
    """Load setup.ps1's functions (not its steps) and run body."""
    t = tmp_path / 't.ps1'
    t.write_text(r'''$ErrorActionPreference = "Stop"
$src = Get-Content "%s" -Raw
$funcs = $src.Substring($src.IndexOf('function Say'), $src.IndexOf('# 0. The ref') - $src.IndexOf('function Say'))
$Distro = "ClaudeProjects"; $DryRun = $false
Invoke-Expression $funcs
''' % WIN + body)
    r = subprocess.run([PWSH, '-NoProfile', '-File', str(t)], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_decoder_handles_utf16_with_and_without_bom_and_utf8(tmp_path):
    env, _ = fake(tmp_path, {})
    names = ['version-utf16le-bom.bin', 'version-utf16le.bin', 'version-utf8.bin', 'version-utf8-bom.bin',
             'list-utf16le.bin', 'list-utf8.bin']
    body = ''.join(f'''[Console]::Out.Write("<<" + (ConvertFrom-WslOutput ([IO.File]::ReadAllBytes("{OUT / n}"))) + ">>")\n'''
                   for n in names)
    out = run_funcs(tmp_path, env, body)
    parts = out.split('>>')[:-1]
    got = [p.split('<<', 1)[1] for p in parts]
    assert got == [VERSION] * 4 + ['Ubuntu\nClaudeProjects\n'] * 2
    assert '\x00' not in out and '﻿' not in out


@pytest.mark.parametrize('fixture', ['version-utf16le-bom.bin', 'version-utf16le.bin', 'version-utf8.bin'])
def test_version_list_and_distro_version_parse_in_any_encoding(tmp_path, fixture):
    env, _ = fake(tmp_path, {'--version': (fixture, 0), '-l_-q': ('list-utf16le.bin', 0), '-l_-v': ('list-v-utf16le.bin', 0)})
    out = run_funcs(tmp_path, env, '''"version=" + (Get-WslVersion)
"list=" + (((Invoke-WslQuiet @("-l", "-q")).Out -split "`n" | Where-Object { $_ }) -join ",")
"cp=" + (Get-DistroWslVersion "ClaudeProjects") + " ub=" + (Get-DistroWslVersion "Ubuntu") + " none=" + (Get-DistroWslVersion "Nope")
''')
    assert 'version=2.4.4.0' in out and 'list=Ubuntu,ClaudeProjects' in out and 'cp=1 ub=2 none=\n' in out


def test_old_inbox_usage_text_is_not_a_version(tmp_path):
    env, _ = fake(tmp_path, {'--version': ('usage-old-utf16le.bin', 0)})
    assert 'version=\n' in run_funcs(tmp_path, env, '"version=" + (Get-WslVersion)\n')


OLD_INBOX = {'--version': ('usage-old-utf16le.bin', 255), '--update_--web-download': ('usage-old-utf16le.bin', 255),
             '--update': ('usage-old-utf16le.bin', 255), '--status': ('status-old-utf16le.bin', 0),
             '--install_--no-distribution': ('usage-old-utf16le.bin', 255)}


def test_old_inbox_wsl_tries_both_updates_then_stops_with_next_steps(tmp_path):
    env, log = fake(tmp_path, OLD_INBOX)
    for args in ([], ['-WebDownload']):
        if log.exists(): log.unlink()
        r, out = run_setup(env, *args)
        assert r.returncode == 3, out
        assert "the old inbox WSL, without 'wsl --version'" in out and 'updating it didn\'t work' in out
        assert 'VirtualMachinePlatform' in out and 'Microsoft-Windows-Subsystem-Linux' in out
        assert 'https://github.com/microsoft/WSL/releases' in out and 'Reboot.' in out and 'Re-run this script' in out
        assert not any(m in out for m in STACK), out
        argv = log.read_text()
        assert argv.index('ARG[--update]\nARG[--web-download]') < argv.rindex('ARG[--update]\n')
        assert 'ARG[--install]' not in argv and 'ARG[--exec]' not in argv


def test_wsl2_not_starting_stops_with_the_reason(tmp_path):
    env, log = fake(tmp_path, {'--status': (b'The Virtual Machine Platform feature is not enabled.\r\n', 1)})
    r, out = run_setup(env)
    assert r.returncode == 3 and "WSL2 can't start yet" in out and 'Virtual Machine Platform feature is not enabled' in out
    assert 'https://github.com/microsoft/WSL/releases' in out and not any(m in out for m in STACK), out


def test_wsl1_only_stops_at_set_default_version(tmp_path):
    env, log = fake(tmp_path, {'--set-default-version_2': ('usage-old-utf16le.bin', 255)})
    r, out = run_setup(env)
    assert r.returncode == 3 and "--set-default-version 2' failed" in out and not any(m in out for m in STACK), out
    assert 'ARG[--install]' not in log.read_text()


def test_distro_install_failure_is_a_readable_stop(tmp_path):
    env, log = fake(tmp_path, {'-l_-q': ('Ubuntu\r\n'.encode('utf-16-le'), 0),
                               '--install_Ubuntu-24.04_--name_ClaudeProjects_--no-launch': (None, 255),
                               '--install_Ubuntu-24.04_--name_ClaudeProjects_--no-launch_--web-download': (None, 255)})
    r, out = run_setup(env)
    assert r.returncode == 3, out
    assert "Couldn't create the WSL distro ClaudeProjects" in out and 'exited 255' in out and 'Default Version: 2' in out
    assert not any(m in out for m in STACK), out
    argv = log.read_text()
    assert 'ARG[--set-default-version]\nARG[2]' in argv and argv.count('ARG[Ubuntu-24.04]') == 2


def test_existing_wsl1_distro_is_converted_and_unexpected_errors_have_no_stack_trace(tmp_path):
    env, log = fake(tmp_path, {'-l_-q': ('list-utf16le.bin', 0), '-l_-v': ('list-v-utf16le.bin', 0),
                               '--set-version_ClaudeProjects_2': (None, 0)})
    r, out = run_setup(env)
    argv = log.read_text()
    assert 'ARG[--set-version]\nARG[ClaudeProjects]\nARG[2]' in argv, out
    assert 'Converting ClaudeProjects to WSL 2' in out
    # the next step needs the distro, which the fake refuses to run (exit 97): one readable line, exit 1
    assert r.returncode == 1 and 'setup.ps1 stopped: wsl.exe -d ClaudeProjects -u root --exec' in out and 'exited 97' in out
    assert not any(m in out for m in STACK), out
