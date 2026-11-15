"""HTTP transport: bearer required, DNS-rebinding protection on, private token file enforced."""
import json, os, pathlib, secrets, socket, subprocess, sys, time, urllib.error, urllib.request
import pytest
pytest.importorskip('mcp'); pytest.importorskip('uvicorn')
REPO = pathlib.Path(__file__).resolve().parents[1]
INIT = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}}).encode()


def _port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


def _post(port, headers):
    h = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', **headers}
    req = urllib.request.Request(f'http://127.0.0.1:{port}/mcp', data=INIT, headers=h)
    try: return urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e: return e.code


def _server(tmp_path, mode=0o600):
    tok = tmp_path / 'tok'; tok.write_text(secrets.token_urlsafe(32)); tok.chmod(mode)
    port = _port(); cfg = tmp_path / 'c.json'
    cfg.write_text(json.dumps({'token_file': str(tok), 'mcp_port': port, 'state_dir': str(tmp_path / 'st')}))
    env = dict(os.environ, CPG_CONFIG=str(cfg))
    p = subprocess.Popen([sys.executable, str(REPO / 'cpg_mcp.py')], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p, port, tok.read_text()


def test_bearer_and_host(tmp_path):
    p, port, tok = _server(tmp_path)
    try:
        for _ in range(50):
            try: urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=1); break
            except Exception: time.sleep(0.2)
        assert _post(port, {}) == 401
        assert _post(port, {'Authorization': 'Bearer wrong'}) == 401
        assert _post(port, {'Authorization': f'Bearer {tok}'}) == 200
        assert _post(port, {'Authorization': f'Bearer {tok}', 'Host': 'evil.example'}) == 421
    finally:
        p.terminate(); p.wait(10)


def test_refuses_world_readable_token(tmp_path):
    p, _, _ = _server(tmp_path, 0o644)
    out, err = p.communicate(timeout=30)
    assert p.returncode != 0 and b'chmod 600' in err
