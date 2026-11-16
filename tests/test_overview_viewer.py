"""Attachment get with the app's "Overview" document viewer (it replaced the Image preview dialog for files Claude
uploads): a fake AT-SPI tree shaped like the one probed on the live app. Nothing here touches a real app or Project."""
import pathlib
import pytest

from claude_projects import core as C

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class El:
    def __init__(self, role, name='', kids=(), attrs=None, ext=(0, 0, 0, 0), on_press=None):
        self.r, self.n, self.k, self.a, self.ext, self.on_press = role, name, list(kids), attrs or {}, ext, on_press
    def get_name(self): return self.n
    def get_role_name(self): return self.r
    def get_child_count(self): return len(self.k)
    def get_child_at_index(self, i): return self.k[i]
    def get_attributes(self): return self.a
    def get_component_iface(self):
        el = self
        class Comp:
            def scroll_to(self, _): pass
            def get_extents(self, _):
                x, y, w, h = el.ext
                return type('E', (), {'x': x, 'y': y, 'width': w, 'height': h})()
        return Comp()
    def get_action_iface(self):
        el = self
        class Act:
            def get_n_actions(self): return 1
            def do_action(self, _):
                App.pressed.append(el.n)
                if el.on_press: el.on_press()
        return Act()


class App:
    pressed = []


@pytest.fixture
def app(monkeypatch):
    """A transcript with one tile; pressing the tile swaps the transcript for the Overview viewer."""
    App.pressed = []
    st = {'route': '/project/chan_X', 'escapes': 0}
    doc = El('document web')

    def transcript():
        tile = El('push button', 'PNG tablet-light.png', [El('static', 'PNG'), El('static', 'tablet-light.png')], ext=(10, 10, 200, 120), on_press=open_viewer)
        return [El('article', 'Message 138', [tile])]

    def viewer(kind):
        close = El('push button', 'Close', on_press=close_viewer)
        nav = El('tool bar', 'Document navigation and actions', [
            El('push button', 'Threads'), El('section', '', [El('section', st['file'])]), El('push button', 'View thread 2h ago'),   # live: the named section sits in an unnamed one
            El('push button', 'Previous'), El('push button', 'Next'), El('push button', 'Download file', ext=(400, 5, 30, 30)),
            El('push button', 'More actions')])
        if kind == 'image':
            body = [El('push button', f'Add a pinned comment to {st["file"]}', [
                El('image', st['file'], attrs={'src': 'blob:https://claude.ai/4c1e8a52-0000-4000-8000-000000000000'}, ext=(0, 80, 540, 700))], ext=(0, 80, 540, 700))]
        else:
            body = [El('push button', f'Add a pinned comment to {st["file"]}', [El('paragraph', '', [El('static', 'line one of the notes')])])]
        return [El('landmark', 'Overview', [El('tool bar', 'Overview controls', [close]), nav] + body)]

    def open_viewer(): doc.k = viewer(st['kind'])
    def close_viewer(): doc.k = transcript()
    st.update(kind='image', file='tablet-light.png')
    doc.k = transcript()

    def xdo(*a, **k):
        if a[:2] == ('key', 'Escape'):
            st['escapes'] += 1
            if st.get('escape_closes', True) and C.overview_viewer(doc): close_viewer()
        return ''
    monkeypatch.setattr(C, 'main_doc', lambda: (doc, 'https://claude.ai' + st['route']))
    monkeypatch.setattr(C, 'route', lambda: st['route'])
    monkeypatch.setattr(C, 'xdo', xdo)
    monkeypatch.setattr(C.time, 'sleep', lambda s: None)
    monkeypatch.setattr(C, 'http_cache_get', lambda *a, **k: None)
    monkeypatch.setattr(C, 'http_cache_by_name', lambda name: None)
    monkeypatch.setattr(C, 'screen_crop', lambda ext: PNG if ext and ext[2] > 4 else None)
    monkeypatch.setattr(C, 'log', lambda *a, **k: None)
    st['doc'] = doc
    st['tile'] = lambda: doc.k[0].k[0]
    return st


def att(name='tablet-light.png', kind='image', mime='image/png'): return {'name': name, 'kind': kind, 'mime': mime}


def test_viewer_is_recognised(app):
    app['tile']().get_action_iface().do_action(0)
    ov = C.overview_viewer(app['doc'])
    assert ov and ov['section'] == 'tablet-light.png'
    assert nm(ov['close']) == 'Close' and nm(ov['download']) == 'Download file'
    assert C._attrs(ov['image'])['src'].startswith('blob:')


def nm(e): return C.nm(e)


def test_image_original_via_download_file(app, monkeypatch, tmp_path):
    saved = tmp_path / 'tablet-light.png'; saved.write_bytes(PNG)
    seen = []
    monkeypatch.setattr(C, '_save_with_button', lambda btn, name, **k: (seen.append(C.nm(btn)), str(saved))[1])
    data, name, mime, how, extra = C._fetch_upload(att(), app['tile']())
    assert data is None and extra['download_path'] == str(saved) and extra['original'] is True and extra['viewer'] == 'overview'
    assert seen == ['Download file'] and 'Download file' in how and mime == 'image/png'
    assert C.overview_viewer(app['doc']) is None, 'the viewer is closed afterwards'
    assert App.pressed == ['PNG tablet-light.png', 'Close'], App.pressed
    assert not any(p.startswith(('Add a pinned comment', 'More actions')) for p in App.pressed)


def test_image_falls_back_to_crop_and_says_why(app, monkeypatch):
    monkeypatch.setattr(C, '_save_with_button', lambda btn, name, **k: None)
    data, name, mime, how, extra = C._fetch_upload(att(), app['tile']())
    assert data == PNG and mime == 'image/png' and extra['original'] is False and extra['rendered_size'] == [540, 700]
    assert 'Overview viewer image' in how and any('Download file' in s for s in extra['failed_steps'])
    assert C.overview_viewer(app['doc']) is None
    assert not any(p.startswith(('Add a pinned comment', 'More actions')) for p in App.pressed)


def test_cache_by_name_beats_the_crop(app, monkeypatch):
    monkeypatch.setattr(C, '_save_with_button', lambda btn, name, **k: None)
    monkeypatch.setattr(C, 'http_cache_by_name', lambda name: {'body': PNG, 'content_type': 'image/png', 'filename': name, 'url': '/files/x/contents'})
    data, name, mime, how, extra = C._fetch_upload(att(), app['tile']())
    assert data == PNG and extra['original'] is True and 'found by file name' in how


def test_non_image_via_download_then_text(app, monkeypatch, tmp_path):
    app.update(kind='text', file='notes.md')
    app['doc'].k[0].k[0].n = 'MD notes.md'
    saved = tmp_path / 'notes.md'; saved.write_text('# notes\n')
    monkeypatch.setattr(C, '_save_with_button', lambda btn, name, **k: str(saved))
    data, name, mime, how, extra = C._fetch_upload(att('notes.md', 'file', 'text/markdown'), app['tile']())
    assert extra['download_path'] == str(saved) and name == 'notes.md'
    # the download fails: the viewer's rendered text, without the tool bars
    monkeypatch.setattr(C, '_save_with_button', lambda btn, name, **k: None)
    data, name, mime, how, extra = C._fetch_upload(att('notes.md', 'file', 'text/markdown'), app['tile']())
    assert data and b'line one of the notes' in data and b'Download file' not in data and b'pinned comment' not in data
    assert extra['original'] is False and 'rendered text' in how
    assert not any(p.startswith(('Add a pinned comment', 'More actions')) for p in App.pressed)


def test_wrong_file_in_viewer_is_not_downloaded(app, monkeypatch):
    app['file'] = 'foldable-dark.png'
    called = []
    monkeypatch.setattr(C, '_save_with_button', lambda btn, name, **k: called.append(1))
    data, name, mime, how, extra = C._fetch_upload(att(), app['tile']())
    assert data is None and not called and "'foldable-dark.png'" in how and how.startswith('not available: ')


def test_nothing_opens_names_each_step(app, monkeypatch):
    app['tile']().on_press = None   # the tile does nothing
    clock = iter(range(0, 10000, 3)); monkeypatch.setattr(C.time, 'time', lambda: next(clock))
    monkeypatch.setattr(C, 'screen_crop', lambda ext: None)
    data, name, mime, how, extra = C._fetch_upload(att(), app['tile']())
    assert data is None and 'preview did not open' not in how
    assert 'neither the Image preview nor the Overview viewer appeared' in how
    assert any('HTTP cache by file name' in s for s in extra['failed_steps'])


def test_old_image_preview_dialog_still_works(app, monkeypatch):
    fid = '0f8b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d'
    def open_dialog():
        app['doc'].k = [El('dialog', 'Image preview', [El('image', 'x', attrs={'src': f'https://claude.ai/api/x/files/{fid}/preview'}, ext=(0, 0, 300, 300))])]
    app['tile']().on_press = open_dialog
    monkeypatch.setattr(C, 'http_cache_get', lambda sub, **k: {'body': PNG, 'content_type': 'image/png', 'filename': 'tablet-light.png', 'url': sub} if sub.endswith('/contents') else None)
    def esc(*a, **k):
        if a[:2] == ('key', 'Escape'): app['doc'].k = []
        return ''
    monkeypatch.setattr(C, 'xdo', esc)
    data, name, mime, how, extra = C._fetch_upload(att(), app['tile']())
    assert data == PNG and extra['file_id'] == fid and 'original file' in how
    assert C.find(app['doc'], 'dialog', 'Image preview') is None


def test_close_uses_the_viewer_close_button(app):
    app['tile']().get_action_iface().do_action(0)
    App.pressed = []
    assert C._close_att_overlays() is True
    assert App.pressed == ['Close'] and app['escapes'] == 0


def test_close_falls_back_to_escape(app):
    app['tile']().get_action_iface().do_action(0)
    ov = C.overview_viewer(app['doc']); ov['region'].k[0].k = []   # no Close button in this build
    App.pressed = []
    assert C._close_att_overlays() is True and app['escapes'] >= 1 and App.pressed == []


def test_close_reports_a_viewer_it_could_not_close(app):
    app['tile']().get_action_iface().do_action(0)
    C.overview_viewer(app['doc'])['region'].k[0].k = []
    app['escape_closes'] = False
    assert C._close_att_overlays() is False


def test_close_with_nothing_open_presses_nothing(app):
    assert C._close_att_overlays() is True and App.pressed == [] and app['escapes'] == 0


@pytest.mark.parametrize('label', ['Add a pinned comment to tablet-light.png', 'More actions', 'Send', 'Reply', 'Delete file', 'Share'])
def test_posting_controls_are_refused(label):
    e = El('push button', label)
    with pytest.raises(C.AppError):
        C._press(e)
    assert label not in App.pressed


def test_http_cache_by_name_needs_one_unambiguous_match(monkeypatch, tmp_path):
    monkeypatch.undo()
    entries = {}
    def mk(i, key, cd, body):
        f = tmp_path / f'{i:016x}_0'; f.write_bytes(key.encode() + b'\0' * 10)
        entries[str(f)] = {'key': key, 'content_disposition': cd, 'status': '200', 'content_type': 'image/png', 'body': body}
    monkeypatch.setattr(C, 'HTTPC', tmp_path)
    monkeypatch.setattr(C, '_cache_entry', lambda f: entries[str(f)])
    monkeypatch.setattr(C, '_decode_body', lambda e: e['body'])
    mk(1, '1/0/https://claude.ai/api/o/files/aaaa/contents', "attachment; filename*=utf-8''tablet-light.png", PNG)
    mk(2, '1/0/https://claude.ai/api/o/files/bbbb/preview', "attachment; filename=other.png", b'x')
    got = C.http_cache_by_name('tablet-light.png')
    assert got and got['body'] == PNG
    assert C.http_cache_by_name('missing.png') is None
    mk(3, '1/0/https://claude.ai/api/o/files/cccc/contents', 'attachment; filename="tablet-light.png"', PNG + b'different')
    assert C.http_cache_by_name('tablet-light.png') is None, 'two different files with one name: ambiguous'


def test_attachment_get_stores_the_downloaded_original(monkeypatch, tmp_path):
    """End to end through op_attachment_get: an Overview download lands in the attachment store and comes back as an image."""
    import contextlib, types
    monkeypatch.setattr(C, 'ATT_JOBS', tmp_path / 'jobs')
    saved = tmp_path / 'incoming' / '1' / 'tablet-light.png'; saved.parent.mkdir(parents=True)
    Image = pytest.importorskip('PIL.Image')
    Image.new('RGB', (40, 30), 'white').save(saved, 'PNG')
    monkeypatch.setattr(C, 'ATT_DIR', tmp_path / 'attachments')
    monkeypatch.setattr(C, 'resolve_project', lambda q, snap=None: {'id': 'chan_X', 'name': 'Demo'})
    monkeypatch.setattr(C, 'ui_lock', lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(C, '_resolve_tid', lambda p, chan, thread: None)
    monkeypatch.setattr(C, 'open_target_settled', lambda chan, tid: None)
    monkeypatch.setattr(C, 'find_article', lambda tid, idx: object())
    R = types.SimpleNamespace(atts=[att()] * 2, elems=[None, None])
    monkeypatch.setattr(C, 'rich_of', lambda a, idx=None: (R, None))
    monkeypatch.setattr(C, '_fetch_upload', lambda a, el: (None, 'tablet-light.png', 'image/png', "the Overview viewer's Download file button (original file)",
                                                           {'viewer': 'overview', 'original': True, 'download_path': str(saved)}))
    monkeypatch.setattr(C, '_close_att_overlays', lambda: True)
    monkeypatch.setattr(C, 'go_home', lambda: None)
    r = C.op_attachment_get('Demo', attachment_id='138.2', as_='auto')
    assert r['available'] is True and r['returned_as'] == 'image' and r['width'] == 40 and r['original'] is True
    assert r['viewer'] == 'overview' and 'Download file' in r['source'] and 'download_path' not in r
    assert pathlib.Path(r['path_on_host']).is_file() and not saved.exists()


def test_section_found_inside_unnamed_wrapper(app):
    app['tile']().get_action_iface().do_action(0)
    assert C.overview_viewer(app['doc'])['section'] == 'tablet-light.png'


def test_guard_live_shape_wrong_file_refused_right_file_saved(app, monkeypatch, tmp_path):
    """The live shape: an unnamed wrapper section, and a tile whose label carries the type badge."""
    saved = tmp_path / 'x.png'; saved.write_bytes(PNG)
    monkeypatch.setattr(C, '_save_with_button', lambda btn, name, **k: str(saved))
    a = att(); a['tile_label'] = 'PNG tablet-light.png'
    data, name, mime, how, extra = C._fetch_upload(a, app['tile']())
    assert extra.get('download_path') == str(saved) and name == 'tablet-light.png'
    app['file'] = 'foldable-dark.png'
    data, name, mime, how, extra = C._fetch_upload(a, app['tile']())
    assert data is None and 'download_path' not in extra and "'foldable-dark.png'" in how
    # an older badge-carrying name still compares by file name
    app['file'] = 'tablet-light.png'
    data, name, mime, how, extra = C._fetch_upload(att('PNG tablet-light.png'), app['tile']())
    assert extra.get('download_path') == str(saved)


@pytest.mark.parametrize('label,statics,want', [
    ('PNG tablet-light.png', ['PNG', 'tablet-light.png'], 'tablet-light.png'),
    ('MD notes.md', ['MD', 'notes.md'], 'notes.md'),
    ('PDF Q3 report final.pdf', ['PDF', 'Q3 report final.pdf'], 'Q3 report final.pdf'),
    ('JPEG photo.jpg', [], 'photo.jpg'),
    ('PNG tablet-light.png', [], 'tablet-light.png'),
    ('tablet-light.png', ['tablet-light.png'], 'tablet-light.png'),
    ('IMG 2041.png', [], 'IMG 2041.png'),          # a real file name that only looks like a badge stays whole
    ('USA map.pdf', [], 'USA map.pdf'),
])
def test_tile_file_name(label, statics, want):
    assert C.tile_file_name(label, statics) == want


def test_transcript_tile_name_has_no_badge():
    tile = El('push button', 'PNG tablet-light.png', [El('static', 'PNG'), El('static', 'tablet-light.png')], ext=(0, 0, 200, 120))
    R = C.Rich(); md = C.md_walk(El('article', 'Message 138', [tile]), R, {}, 0)
    assert R.atts[0]['name'] == 'tablet-light.png' and R.atts[0]['tile_label'] == 'PNG tablet-light.png'
    assert R.atts[0]['kind'] == 'image' and 'tablet-light.png' in md and 'PNG tablet' not in md
