"""A scoped release must never advertise packages that were not uploaded."""
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href")


def generate(tmp_path, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / "devtools/generate_download_page.py"),
         "2.1.1", "https://closex.cc", str(tmp_path), *args],
        capture_output=True, text=True, check=False,
    )


def test_macos_connector_release_has_only_uploaded_artifacts(tmp_path):
    result = generate(tmp_path, "--platforms", "mac,connector")
    assert result.returncode == 0, result.stderr
    links = Links()
    links.feed((tmp_path / "index.html").read_text())
    artifacts = [href for href in links.hrefs if href.endswith((".dmg", ".exe", ".apk", ".deb", ".AppImage", ".tar.gz"))]
    assert artifacts == [
        "Agents%20Anywhere-2.1.1-universal.dmg",
        "Agents%20Anywhere-Connector-2.1.1-linux-x86_64.tar.gz",
    ]
    assert "https://closex.cc" in links.hrefs
    assert (tmp_path / "CHANGELOG.html").is_file()
    assert (tmp_path / "CHANGELOG.md").is_file()


def test_default_preserves_all_platforms(tmp_path):
    result = generate(tmp_path)
    assert result.returncode == 0, result.stderr
    links = Links()
    links.feed((tmp_path / "index.html").read_text())
    assert len([href for href in links.hrefs if href.endswith((".dmg", ".exe", ".apk", ".deb", ".AppImage", ".tar.gz"))]) == 6


def test_unknown_platform_fails_before_creating_page(tmp_path):
    result = generate(tmp_path, "--platforms", "mac,ios")
    assert result.returncode != 0
    assert not (tmp_path / "index.html").exists()


def test_android_release_filename_is_advertised_only_when_requested(tmp_path):
    result = generate(tmp_path, "--platforms", "mac,connector,android", "--android-apk-name", "agents-anywhere-2.1.1.apk")
    assert result.returncode == 0, result.stderr
    links = Links()
    links.feed((tmp_path / "index.html").read_text())
    assert "agents-anywhere-2.1.1.apk" in links.hrefs
    assert not any(href.endswith((".exe", ".deb", ".AppImage", "-debug.apk")) for href in links.hrefs)


def test_android_filename_cannot_escape_download_directory(tmp_path):
    result = generate(tmp_path, "--android-apk-name", "../hidden.apk")
    assert result.returncode != 0
    assert not (tmp_path / "index.html").exists()
