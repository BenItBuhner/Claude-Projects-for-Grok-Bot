"""Isolate every test: private temp config/state, a fictional environment map, no real display or app."""
import json, os, pathlib, sys, tempfile

_T = pathlib.Path(tempfile.mkdtemp(prefix='claude-projects-test-'))
_CFG = _T / 'config.json'
_CFG.write_text(json.dumps({
    'state_dir': str(_T / 'state'),
    'display': ':199',
    'timezone': 'America/New_York',
    'app_timezone': 'UTC',
    'operator_name': 'Alex',
    'host_label': 'the workstation',
    'environment_labels': {
        'workstation': {'resolved': 'the workstation', 'detail': 'local env', 'machine_kind': 'workstation'},
        'laptop-relay': {'resolved': 'the laptop', 'detail': 'relay env', 'machine_kind': 'laptop'},
    },
    'environment_ids': {
        'env_TEST_WORKSTATION_0001': {'resolved': 'the workstation', 'detail': 'tmux session', 'machine_kind': 'workstation'},
        'env_TESTPFX': {'resolved': 'the workstation', 'detail': 'prefix match', 'machine_kind': 'workstation'},
        'env_TEST_LAPTOP_0001': {'resolved': 'the laptop', 'detail': 'relay', 'machine_kind': 'laptop'},
    },
}))
os.environ['CLAUDE_PROJECTS_CONFIG'] = str(_CFG)
for k in ('CLAUDE_PROJECTS_BOT_NAME', 'CLAUDE_PROJECTS_DISPLAY', 'CLAUDE_PROJECTS_STATE_DIR', 'CLAUDE_PROJECTS_TIMEZONE'):
    os.environ.pop(k, None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


# ---- shared by test_setup.py and test_box.py: the setup flow with shell and CLI calls faked
import pytest


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
    from claude_projects import setup as S
    monkeypatch.setattr(S, 'STATE_DIR', st); monkeypatch.setattr(S, 'STATE_FILE', st / 'setup.json')
    monkeypatch.setattr(S, 'CONFIG_DIR', cfgd); monkeypatch.setattr(S, 'UNIT_DIR', units)
    monkeypatch.setattr(S.time, 'sleep', lambda s: None)
    f = Fake(); monkeypatch.setattr(S, 'RUN', f.RUN); monkeypatch.setattr(S, 'CLI', f.CLI)
    monkeypatch.setattr(S, 'CFG', dict(S.CFG, viewport_mode='on-demand'))
    return f
