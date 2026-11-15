"""--ref / -Ref / CLAUDE_PROJECTS_REPO_REF in the one-shot host scripts (hosts/macos/setup.sh, hosts/windows/setup.ps1).

Nothing here touches a real VM, WSL or the network: the macOS script runs against stub commands, the Windows script
against tests/fixtures/fake-wsl.exe (pwsh only), and the in-VM checkout runs against local git repositories."""
import os, pathlib, re, shutil, subprocess
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
MAC = REPO / 'hosts' / 'macos' / 'setup.sh'
WIN = REPO / 'hosts' / 'windows' / 'setup.ps1'
BEGIN, END = '# claude-projects-ref-checkout: begin', '# claude-projects-ref-checkout: end'

GOOD = ['release-1.0.2', 'v1.0.1', 'main', 'feature/x.y_z', 'a' * 200, '0123456789abcdef0123456789abcdef01234567']
BAD = ['-x', '--upload-pack=touch /tmp/p', 'a..b', 'a b', "a'b", 'a"b', 'a$b', '$(id)', 'a`id`', 'a;id', 'a|b', 'a&b',
       'a\nb', 'main\n', 'a@{1}', '@{-1}', 'x/', '/x', 'a//b', '.x', 'a/.x', 'x.lock', 'x.lock/y', 'x.', 'a\\b', 'a~1',
       'a^b', 'a:b', 'a?b', 'a*b', 'a[b', 'ä', 'K', 'a' * 201]


def snippet(text):
    return text[text.index(BEGIN):text.index(END) + len(END)]


def test_both_scripts_carry_the_same_checkout():
    assert snippet(MAC.read_text()) == snippet(WIN.read_text())


# ------------------------------------------------------------------ the in-VM checkout, against local repositories

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@example.invalid', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(*args, cwd=None):
    r = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare 'origin' with main (3 commits), branch release-1.0.2 and tag v1.0.1; returns (url, work, shas)."""
    work, bare = tmp_path / 'work', tmp_path / 'origin.git'
    git('init', '-q', '-b', 'main', str(work))
    shas = {}
    for i in range(3):
        (work / 'f.txt').write_text(f'main {i}\n'); git('add', '.', cwd=work); git('commit', '-qm', f'm{i}', cwd=work)
        shas[f'main{i}'] = git('rev-parse', 'HEAD', cwd=work)
    git('tag', 'v1.0.1', shas['main1'], cwd=work)
    git('checkout', '-qb', 'release-1.0.2', cwd=work)
    (work / 'f.txt').write_text('release\n'); git('commit', '-qam', 'rel', cwd=work)
    shas['release'] = git('rev-parse', 'HEAD', cwd=work)
    git('checkout', '-q', 'main', cwd=work)
    git('clone', '-q', '--bare', str(work), str(bare))
    return str(bare), work, shas


def checkout(home, url, ref):
    env = {**os.environ, **GIT_ENV, 'HOME': str(home), 'URL': url, 'REF': ref}
    return subprocess.run(['bash', '-c', 'set -e\n' + snippet(MAC.read_text())], capture_output=True, text=True, env=env)


def head(home):
    d = home / 'claude-projects-mcp'
    return git('rev-parse', 'HEAD', cwd=d), git('rev-parse', '--abbrev-ref', 'HEAD', cwd=d)


def test_no_ref_clones_the_default_branch_and_a_rerun_leaves_it(tmp_path, origin):
    url, _, shas = origin
    home = tmp_path / 'home'; home.mkdir()
    assert checkout(home, url, '').returncode == 0 and head(home) == (shas['main2'], 'main')
    git('checkout', '-q', '--detach', shas['main0'], cwd=home / 'claude-projects-mcp')
    r = checkout(home, url, '')
    assert r.returncode == 0 and head(home)[0] == shas['main0'], 'without a ref an existing clone is not touched'


def test_fresh_clone_of_a_branch_then_tag_then_sha_then_branch_update(tmp_path, origin):
    url, work, shas = origin
    home = tmp_path / 'home'; home.mkdir()
    r = checkout(home, url, 'release-1.0.2')
    assert r.returncode == 0, r.stderr
    assert head(home) == (shas['release'], 'release-1.0.2') and 'is at release-1.0.2' in r.stdout
    r = checkout(home, url, 'v1.0.1')
    assert r.returncode == 0, r.stderr
    assert head(home) == (shas['main1'], 'HEAD')
    r = checkout(home, url, shas['main0'])          # reachable, but not a branch tip
    assert r.returncode == 0, r.stderr
    assert head(home) == (shas['main0'], 'HEAD')
    git('checkout', '-q', 'release-1.0.2', cwd=work)
    (work / 'f.txt').write_text('release 2\n'); git('commit', '-qam', 'rel2', cwd=work)
    new = git('rev-parse', 'HEAD', cwd=work)
    git('push', '-q', url, 'release-1.0.2', cwd=work)
    r = checkout(home, url, 'release-1.0.2')
    assert r.returncode == 0, r.stderr
    assert head(home) == (new, 'release-1.0.2'), 'a re-run fetches the branch again'


def test_fresh_clone_of_a_tag_and_of_a_sha(tmp_path, origin):
    url, _, shas = origin
    for ref, want in (('v1.0.1', shas['main1']), (shas['main0'], shas['main0'])):
        home = tmp_path / ref[:8]; home.mkdir()
        r = checkout(home, url, ref)
        assert r.returncode == 0, r.stderr
        assert head(home) == (want, 'HEAD')


def test_local_changes_or_local_commits_are_refused(tmp_path, origin):
    url, _, shas = origin
    home = tmp_path / 'home'; home.mkdir()
    assert checkout(home, url, 'release-1.0.2').returncode == 0
    d = home / 'claude-projects-mcp'
    (d / 'f.txt').write_text('edited\n')
    r = checkout(home, url, 'v1.0.1')
    assert r.returncode == 1 and 'local changes' in r.stderr and (d / 'f.txt').read_text() == 'edited\n'
    assert head(home) == (shas['release'], 'release-1.0.2')
    git('commit', '-qam', 'local', cwd=d)
    local = git('rev-parse', 'HEAD', cwd=d)
    r = checkout(home, url, 'release-1.0.2')
    assert r.returncode == 1 and 'local commits' in r.stderr and head(home)[0] == local
    (d / 'untracked.txt').write_text('x')        # untracked files alone are not "local changes"
    git('reset', '-q', '--hard', shas['release'], cwd=d)
    assert checkout(home, url, 'v1.0.1').returncode == 0 and (d / 'untracked.txt').exists()


def test_unknown_ref_and_not_a_clone_fail_clearly(tmp_path, origin):
    url, _, _ = origin
    home = tmp_path / 'home'; home.mkdir()
    r = checkout(home, url, 'no-such-branch')
    assert r.returncode == 1 and 'not a branch, a tag or a full 40-character commit sha' in r.stderr
    r = checkout(home, url, 'f' * 40)
    assert r.returncode == 1 and 'not on origin' in r.stderr
    home2 = tmp_path / 'home2'; (home2 / 'claude-projects-mcp').mkdir(parents=True)
    r = checkout(home2, url, 'main')
    assert r.returncode == 1 and 'not a git clone' in r.stderr


# ------------------------------------------------------------------ macOS: validation and the dry-run fetch line

def mac_stubs(tmp_path):
    b = tmp_path / 'stub'; b.mkdir(exist_ok=True)
    log = tmp_path / 'limactl.log'
    stubs = {
        'uname': 'echo Darwin',
        'sw_vers': 'echo 14.5',
        'scutil': 'echo test-mac',
        'pmset': 'exit 0',
        'xcode-select': 'echo /Library/Developer/CommandLineTools',
        'limactl': f'printf "%s\\n" "$*" >> {log}; exit 0',
    }
    for name, body in stubs.items():
        (b / name).write_text(f'#!/bin/bash\n{body}\n'); (b / name).chmod(0o755)
    return b, log


def run_mac(tmp_path, *args, extra_env=None):
    b, log = mac_stubs(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PROJECTS_REPO_REF'}
    env.update(PATH=f"{b}:{os.environ['PATH']}", HOME=str(tmp_path), **(extra_env or {}))
    r = subprocess.run(['bash', str(MAC), '--dry-run', *args], capture_output=True, text=True, env=env)
    return r, log


@pytest.mark.parametrize('ref', BAD)
def test_macos_rejects_unsafe_refs_before_anything_runs(tmp_path, ref):
    r, log = run_mac(tmp_path, '--ref', ref)
    assert r.returncode == 2 and '--ref:' in r.stderr, (ref, r.stdout, r.stderr)
    assert not log.exists() and '==>' not in r.stdout, 'nothing ran'


def test_macos_rejects_unsafe_env_ref_and_an_empty_flag(tmp_path):
    r, log = run_mac(tmp_path, extra_env={'CLAUDE_PROJECTS_REPO_REF': 'x;touch /tmp/p'})
    assert r.returncode == 2 and '--ref:' in r.stderr and not log.exists()
    r, _ = run_mac(tmp_path, '--ref', '')
    assert r.returncode == 2 and 'needs a value' in r.stderr


@pytest.mark.parametrize('ref', GOOD)
def test_macos_dry_run_fetches_the_ref(tmp_path, ref):
    r, _ = run_mac(tmp_path, '--ref', ref)
    assert r.returncode == 0, r.stderr
    assert f'\n  REF={ref}\n' in r.stdout and BEGIN in r.stdout and 'git clone --quiet "$URL" "$D"' in r.stdout


def test_macos_env_fallback_and_the_flag_wins(tmp_path):
    r, _ = run_mac(tmp_path, extra_env={'CLAUDE_PROJECTS_REPO_REF': 'v1.0.1'})
    assert r.returncode == 0 and '\n  REF=v1.0.1\n' in r.stdout
    r, _ = run_mac(tmp_path, '--ref', 'release-1.0.2', extra_env={'CLAUDE_PROJECTS_REPO_REF': 'v1.0.1'})
    assert r.returncode == 0 and '\n  REF=release-1.0.2\n' in r.stdout and 'v1.0.1' not in r.stdout
    r, _ = run_mac(tmp_path)
    assert r.returncode == 0 and "\n  REF=''\n" in r.stdout, 'no ref: the default branch, as before'


# ------------------------------------------------------------------ Windows (needs pwsh; CI and the pwsh container)

PWSH = shutil.which('pwsh')
needs_pwsh = pytest.mark.skipif(not PWSH, reason='pwsh not installed (CI runs these)')


def fake_wsl(tmp_path):
    b = tmp_path / 'bin'; b.mkdir(exist_ok=True)
    shutil.copy(REPO / 'tests' / 'fixtures' / 'fake-wsl.exe', b / 'wsl.exe'); (b / 'wsl.exe').chmod(0o755)
    return b, tmp_path / 'fake-wsl-argv.log'


def run_win(tmp_path, *args, extra_env=None):
    b, log = fake_wsl(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PROJECTS_REPO_REF'}
    env.update(PATH=f"{b}:{os.environ['PATH']}", TMPDIR=str(tmp_path), **(extra_env or {}))
    r = subprocess.run([PWSH, '-NoProfile', '-File', str(WIN), '-DryRun', *args], capture_output=True, text=True,
                       env=env, timeout=180)
    return r, log


@needs_pwsh
def test_windows_rejects_unsafe_refs_before_anything_runs(tmp_path):
    for i, ref in enumerate(BAD):
        d = tmp_path / str(i); d.mkdir()
        r, log = run_win(d, '-Ref', ref)
        assert r.returncode == 2 and '-Ref:' in r.stdout, (ref, r.stdout, r.stderr)
        assert not log.exists(), (ref, 'wsl.exe was called')
    r, log = run_win(tmp_path / '0', extra_env={'CLAUDE_PROJECTS_REPO_REF': 'x;touch /tmp/p'})
    assert r.returncode == 2 and '-Ref:' in r.stdout


@needs_pwsh
def test_windows_dry_run_env_fallback_and_the_parameter_wins(tmp_path):
    for i, (args, env, want) in enumerate([(['-Ref', 'release-1.0.2'], {}, "REF='release-1.0.2'"),
                                           ([], {'CLAUDE_PROJECTS_REPO_REF': 'v1.0.1'}, "REF='v1.0.1'"),
                                           (['-Ref', 'release-1.0.2'], {'CLAUDE_PROJECTS_REPO_REF': 'v1.0.1'}, "REF='release-1.0.2'"),
                                           ([], {}, "REF=''")]):
        d = tmp_path / str(i); d.mkdir()
        r, _ = run_win(d, *args, extra_env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert f'| {want}\n' in r.stdout and BEGIN in r.stdout, (args, env, r.stdout)
        if env and args: assert 'v1.0.1' not in r.stdout


@needs_pwsh
def test_windows_ref_round_trip_through_fake_wsl(tmp_path, origin):
    """Get-FetchScript + Invoke-WslScript through the fake wsl.exe (which execs after --exec with no shell), for real:
    the ref arrives intact and the clone ends up on it."""
    url, _, shas = origin
    b, _ = fake_wsl(tmp_path)
    home = tmp_path / 'home'; home.mkdir()
    t = tmp_path / 't.ps1'
    t.write_text(r'''param([string]$Url, [string]$Ref)
$ErrorActionPreference = "Stop"
$src = Get-Content "%s" -Raw
$funcs = $src.Substring($src.IndexOf('function Say'), $src.IndexOf('# 0. The ref') - $src.IndexOf('function Say'))
$Distro = "ClaudeProjects"; $DryRun = $false
Invoke-Expression $funcs
if (Get-RefProblem $Ref) { throw "rejected $Ref" }
Invoke-WslScript claude (Get-FetchScript $Url $Ref) -Login
''' % WIN)
    env = {**os.environ, **GIT_ENV, 'PATH': f"{b}:{os.environ['PATH']}", 'HOME': str(home), 'TMPDIR': str(tmp_path)}
    for ref, want in (('release-1.0.2', shas['release']), ('v1.0.1', shas['main1']), (shas['main0'], shas['main0'])):
        r = subprocess.run([PWSH, '-NoProfile', '-File', str(t), '-Url', url, '-Ref', ref], capture_output=True,
                           text=True, env=env, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        assert f'is at {ref} (' in r.stdout and head(home)[0] == want
    argv = (tmp_path / 'fake-wsl-argv.log').read_text()
    assert 'ARG[--exec]' in argv and 'release-1.0.2' not in argv, 'the ref only travels inside the base64 script'


def test_docs_explain_the_ref():
    for f in ('docs/hosts.md', 'skill/references/macos.md', 'skill/references/windows.md'):
        t = (REPO / f).read_text()
        assert 'v1.0.2' in t and 'CLAUDE_PROJECTS_REPO_REF' in t, f
    assert '--ref v1.0.2' in (REPO / 'skill/references/macos.md').read_text()
    assert '-Ref v1.0.2' in (REPO / 'skill/references/windows.md').read_text()
    assert '--ref' in MAC.read_text().split('set -euo pipefail')[0] and '-Ref REF' in WIN.read_text().split('#>')[0]
