"""Mock tests for the location-bar attach fix (no X, no UI)."""
import sys, pathlib, pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from claude_projects import core as c

@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(c, 'log', lambda m: None); monkeypatch.setattr(c.time, 'sleep', lambda s: None)

@pytest.fixture
def files(tmp_path):
    out = []
    for n in ['rec-0001-sample-sheet.mp4', 'rec-0002-sample-share.mp4', 'rec-0003-sample-units.mp4', 'rec-0004-sample-zoom-menu.mp4']:
        p = tmp_path/n; p.write_bytes(b'x' * 10); out.append({'path': str(p), 'name': n})
    return out

class UI:
    def __init__(self): self.keys = []; self.typed = []; self.opened = 0; self.cancelled = 0; self.open_pressed = 0
    def xdo(self, *a, inp=None):
        if a[0] == 'key': self.keys.append(a[-1])
        if a[0] == 'type': self.typed.append(inp)
        return ''

def wire(monkeypatch, ui, ocr=True):
    monkeypatch.setattr(c.AttachCtx, 'ensure', lambda self: self)
    monkeypatch.setattr(c, 'xdo', ui.xdo)
    monkeypatch.setattr(c, '_open_attach_picker', lambda *a, **k: (setattr(ui, 'opened', ui.opened + 1), '42')[1])
    monkeypatch.setattr(c, '_active_win_name', lambda: 'Open Files')
    monkeypatch.setattr(c, '_chooser_ocr', lambda wid, save=None, needle=None: ('Location: ' + ui.typed[-1]) if ocr else 'Recent Home Desktop')
    monkeypatch.setattr(c, '_dismiss_open_files', lambda reason='': (setattr(ui, 'cancelled', ui.cancelled + 1), True)[1])
    monkeypatch.setattr(c, '_click_chooser_open', lambda root, wid: setattr(ui, 'open_pressed', ui.open_pressed + 1))

def test_attach_test_types_full_paths_and_cancels_each(monkeypatch, files):
    ui = UI(); wire(monkeypatch, ui)
    res = c.attach_files(files, test_only=True, shot_dir=None)
    assert [r['name'] for r in res] == [f['name'] for f in files]
    assert all(r['ocr_saw_filename'] and r['cancelled'] for r in res)
    assert ui.typed == [str(pathlib.Path(f['path']).resolve()) for f in files]
    assert ui.opened == 4 and ui.cancelled == 4 and ui.open_pressed == 0
    assert 'ctrl+l' in ui.keys and 'Return' not in ui.keys

def test_attach_test_reports_ocr_miss(monkeypatch, files):
    ui = UI(); wire(monkeypatch, ui, ocr=False)
    res = c.attach_files(files[:1], test_only=True)
    assert res[0]['ocr_saw_filename'] is False and res[0]['cancelled'] is True and ui.open_pressed == 0

def test_real_attach_presses_open_and_waits_chip(monkeypatch, files):
    ui = UI(); wire(monkeypatch, ui)
    monkeypatch.setattr(c, '_composer_has_chip', lambda n, scope=None: True)
    res = c.attach_files(files)
    assert ui.open_pressed == 4 and all(r['attached'] for r in res)

def test_no_chip_after_open_raises_and_no_browse_retry(monkeypatch, files):
    ui = UI(); wire(monkeypatch, ui)
    monkeypatch.setattr(c, '_composer_has_chip', lambda n, scope=None: False)
    # the clock runs fast only once Open is pressed: the chip never shows, so the per-file limit ends the wait
    t = [0]; monkeypatch.setattr(c.time, 'time', lambda: (t.__setitem__(0, t[0] + (10 if ui.open_pressed else 0)), t[0])[1])
    called = []; monkeypatch.setattr(c, '_attach_files_impl', lambda fs: called.append(fs))
    with pytest.raises(c.AppError, match='after Open'): c.attach_files(files[:1])
    assert called == [] and ui.cancelled >= 1

def test_location_failure_falls_back_to_browse(monkeypatch, files):
    ui = UI(); wire(monkeypatch, ui)
    monkeypatch.setattr(c, '_open_attach_picker', lambda *a, **k: (_ for _ in ()).throw(c.AppError('Open Files dialog did not appear; nothing attached')))
    called = []; monkeypatch.setattr(c, '_attach_files_impl', lambda fs: called.append(fs[0]['name']))
    res = c.attach_files(files[:2])
    assert called == [files[0]['name'], files[1]['name']] and all(r['via'] == 'browse' for r in res)

def test_unreadable_file_rejected(monkeypatch, files):
    ui = UI(); wire(monkeypatch, ui)
    with pytest.raises(c.AppError, match='not a readable file'):
        c._attach_one_via_location({'path': '/nonexistent/x.mp4', 'name': 'x.mp4'}, test_only=True)

def test_ocr_fuzzy():
    assert c._ocr_contains('Location: /home/user/clips/rec-0004-sample-zoorn-menu.mp4', 'rec-0004-sample-zoom-menu.mp4')
    assert not c._ocr_contains('Recent Home Desktop Other Locations', 'rec-0001-sample-sheet.mp4')

def test_base64_staged_to_outgoing_and_uses_same_path(monkeypatch, tmp_path):
    import base64; from claude_projects import staging as staging_mod
    monkeypatch.setattr(c, 'STAGE', staging_mod.Staging(tmp_path/'stage'))
    fs = c.prepare_attachment_files([{'base64': base64.b64encode(b'abc').decode(), 'filename': 'clip.mp4'}])
    p = pathlib.Path(fs[0]['path'])
    assert p.parent.parent == (tmp_path/'stage'/'outgoing').resolve() and p.name == 'clip.mp4' and fs[0]['name'] == 'clip.mp4'
    ui = UI(); wire(monkeypatch, ui)
    c.attach_files(fs, test_only=True)
    assert ui.typed == [str(p.resolve())]

def test_cli_has_attach_test_flag():
    src = (pathlib.Path(__file__).resolve().parent.parent/"claude_projects/core.py").read_text()
    assert "'--attach-test'" in src and 'attach_test=getattr(a' in src
