"""Exercise publishing locally: SSH, rsync and HTTP must never reach production."""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = "2.1.2"


def write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def executable(path, source):
    write(path, f"#!{sys.executable}\n" + textwrap.dedent(source))
    path.chmod(0o755)


def test_no_build_publishes_artifacts_and_recursive_metadata(tmp_path):
    repo = tmp_path / "repo"
    remote = tmp_path / "downloads"
    tools = tmp_path / "bin"
    for name in ["release-closex.sh", "generate_download_page.py"]:
        target = repo / "devtools" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "devtools" / name, target)
    write(repo / f"docs/releases/{VERSION}.md", f"# {VERSION}\n- Test release\n")
    for name in ["desktop-workbench/package.json", "desktop-workbench/renderer/package.json"]:
        write(repo / name, json.dumps({"version": VERSION}))
    write(repo / "connector/pyproject.toml", f'version = "{VERSION}"\n')
    write(repo / "android/app/build.gradle.kts", f'versionName = "{VERSION}"\nversionCode = 45\n')
    dmg = f"Agents Anywhere-{VERSION}-universal.dmg"
    connector = f"Agents Anywhere-Connector-{VERSION}-linux-x86_64.tar.gz"
    apk = f"agents-anywhere-{VERSION}.apk"
    write(repo / "desktop-workbench/release" / dmg, "DMG fixture" * 1024)
    apk_dir = repo / "android/app/build/outputs/apk/release"
    write(apk_dir / "app-release.apk", "APK fixture" * 1024)
    write(apk_dir / "output-metadata.json", json.dumps({
        "variantName": "release", "elements": [{"versionName": VERSION, "versionCode": 45}],
    }))
    bundle_path = repo / "connector/release" / connector
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_path, "w:gz") as bundle:
        data = f'version = "{VERSION}"\n'.encode()
        member = tarfile.TarInfo("agents-anywhere-connector/connector/pyproject.toml")
        member.size = len(data)
        bundle.addfile(member, io.BytesIO(data))
        # Range verification expects a complete 4096-byte prefix.
        data = os.urandom(8192)
        member = tarfile.TarInfo("agents-anywhere-connector/padding")
        member.size = len(data)
        bundle.addfile(member, io.BytesIO(data))

    executable(tools / "ssh", """
        import subprocess, sys
        assert sys.argv[1] == 'not-a-real-host'
        sys.exit(subprocess.run(['bash', '-c', sys.argv[-1]]).returncode)
    """)
    executable(tools / "rsync", """
        from pathlib import Path
        import shutil, sys
        source = Path(sys.argv[-2])
        target = Path(sys.argv[-1].split(':', 1)[1])
        target.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if '-r' not in sys.argv and '--recursive' not in sys.argv:
                sys.exit('Directory metadata upload requires recursive rsync')
            for child in source.iterdir():
                if child.suffix != '.apk': shutil.copy2(child, target / child.name)
        else:
            shutil.copy2(source, target / source.name)
    """)
    executable(tools / "curl", """
        from pathlib import Path
        from urllib.parse import unquote, urlsplit
        import os, sys
        args = sys.argv[1:]
        url = next(arg for arg in args if arg.startswith('https://'))
        assert urlsplit(url).netloc == 'example.test'
        source = Path(os.environ['TEST_DOWNLOAD_DIR'])
        dest = Path(args[args.index('-o') + 1])
        if '--range' in args:
            dest.write_bytes((source / unquote(urlsplit(url).path.rsplit('/', 1)[-1])).read_bytes()[:4096])
        else:
            dest.write_bytes((source / 'index.html').read_bytes())
            print('https://example.test/download/', end='')
    """)
    result = subprocess.run(
        ["bash", str(repo / "devtools/release-closex.sh"), VERSION, "--no-build"],
        env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
             "AA_REMOTE": "not-a-real-host", "AA_SSH_CONTROL_PATH": "",
             "AA_DOWNLOAD_DIR": str(remote), "TEST_DOWNLOAD_DIR": str(remote),
             "AA_SERVER_URL": "https://example.test", "AA_PAGE_URL": "https://example.test/download/"},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p.name for p in remote.iterdir()} == {
        dmg, connector, apk, "index.html", "CHANGELOG.md", "CHANGELOG.html", "SHA256SUMS.txt",
    }
    assert len((remote / "SHA256SUMS.txt").read_text().splitlines()) == 3
    assert "Released macOS + Linux Connector + Android 2.1.2" in result.stdout
