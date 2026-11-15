"""README links: every local link and image resolves, and nothing points at a download-only video."""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
README = (REPO / 'README.md').read_text()


def _local_targets(text):
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)          # placeholders in comments aren't links yet
    found = re.findall(r'\]\(([^)\s]+)\)', text) + re.findall(r'(?:href|src)="([^"]+)"', text)
    for t in found:
        if re.match(r'[a-z][a-z0-9+.-]*:', t, re.I) or t.startswith('#'):
            continue
        yield t.split('#', 1)[0]


def test_readme_local_links_resolve():
    missing = [t for t in _local_targets(README) if t and not (REPO / t).exists()]
    assert not missing, missing


def test_readme_has_no_video_download_link_and_keeps_the_demo_placeholder():
    visible = re.sub(r'<!--.*?-->', '', README, flags=re.S)
    assert not re.search(r'\.(mp4|mov|webm)\b', visible, re.I)
    assert 'Demo: coming soon on X' in visible and '<!-- DEMO_URL: replace with X post link -->' in README
