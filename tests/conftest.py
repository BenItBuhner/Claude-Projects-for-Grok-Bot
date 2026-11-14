"""Isolate every test: private temp config/state, a fictional environment map, no real display or app."""
import json, os, pathlib, sys, tempfile

_T = pathlib.Path(tempfile.mkdtemp(prefix='ccp-test-'))
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
os.environ['CCP_CONFIG'] = str(_CFG)
for k in ('CCP_BOT_NAME', 'CCP_DISPLAY', 'CCP_STATE_DIR', 'CCP_TIMEZONE'):
    os.environ.pop(k, None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
