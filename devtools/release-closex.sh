#!/usr/bin/env bash
# Release only macOS Universal, Linux CLI Connector and Android, without changing
# unrelated platform versions. Requires matching, already-bumped PATCH versions.
set -euo pipefail
unset YARN_NPM_MINIMAL_AGE_GATE pnpm_config_minimum_release_age
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="${1:-}"
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo 'usage: devtools/release-closex.sh <version> [--no-build]' >&2
  exit 2
fi
if [[ $# -gt 2 || ( $# -eq 2 && "$2" != --no-build ) ]]; then
  echo 'only --no-build is supported' >&2; exit 2
fi
REMOTE="${AA_REMOTE:-root@closex.cc}"
DOWNLOAD_DIR="${AA_DOWNLOAD_DIR:-/opt/aa-downloads}"
SERVER_URL="${AA_SERVER_URL:-https://closex.cc}"
PAGE_URL="${AA_PAGE_URL:-https://closex.cc/download/}"
SSH=(ssh)
if [[ -n "${AA_SSH_CONTROL_PATH:-}" ]]; then
  SSH+=(-S "$AA_SSH_CONTROL_PATH")
fi
printf -v RSYNC_SSH '%q ' "${SSH[@]}"
SSH+=("$REMOTE")
STAGE="$DOWNLOAD_DIR/.incoming-$VERSION-mac-connector-android"
DMG="desktop-workbench/release/Agents Anywhere-$VERSION-universal.dmg"
CLI="connector/release/Agents Anywhere-Connector-$VERSION-linux-x86_64.tar.gz"
APK="android/app/build/outputs/apk/release/app-release.apk"
APK_NAME="agents-anywhere-$VERSION.apk"
PAGE_STAGE="$(mktemp -d)"
trap 'rm -rf "$PAGE_STAGE"' EXIT

python3 - "$VERSION" <<'PY'
import json, pathlib, re, sys
version = sys.argv[1]
for path in ('desktop-workbench/package.json', 'desktop-workbench/renderer/package.json'):
    assert json.loads(pathlib.Path(path).read_text())['version'] == version, path
text = pathlib.Path('connector/pyproject.toml').read_text()
assert re.search(r'^version = "([^"]+)"', text, re.M).group(1) == version
text = pathlib.Path('android/app/build.gradle.kts').read_text()
assert re.search(r'versionName = "([^"]+)"', text).group(1) == version
assert pathlib.Path(f'docs/releases/{version}.md').is_file(), 'write release notes first'
PY
if [[ "${2:-}" != --no-build ]]; then
  (cd desktop-workbench && yarn dist:mac)
  ./devtools/build-connector-bundle.sh "$VERSION"
  (cd android && JAVA_HOME="${JAVA_HOME:-$(/usr/libexec/java_home -v 17)}" ./gradlew assembleRelease -PagentsAnywhere.signReleaseWithDebugKey=true --console=plain)
fi
for artifact in "$DMG" "$CLI" "$APK"; do
  [[ -s "$artifact" ]] || { echo "missing artifact: $artifact" >&2; exit 1; }
done
# Generic Gradle filenames must not let --no-build accidentally publish an old
# APK under the new release name. Check the Connector's embedded version too.
python3 - "$VERSION" "$CLI" <<'PYVERIFY'
import json, pathlib, re, sys, tarfile
version, archive = sys.argv[1:]
metadata = json.loads(pathlib.Path("android/app/build/outputs/apk/release/output-metadata.json").read_text())
assert metadata["variantName"] == "release"
assert all(item["versionName"] == version for item in metadata["elements"])
gradle = pathlib.Path("android/app/build.gradle.kts").read_text()
code = int(re.search(r"versionCode = (\d+)", gradle).group(1))
assert all(item["versionCode"] == code for item in metadata["elements"])
with tarfile.open(archive) as bundle:
    member = next(m for m in bundle.getmembers() if str(pathlib.PurePosixPath(m.name)) == "agents-anywhere-connector/connector/pyproject.toml")
    source = bundle.extractfile(member)
    if source is None:
        raise SystemExit("Connector package metadata is missing")
    declared = re.search(r'^version = "([^"]+)"', source.read().decode(), re.M)
    assert declared and declared.group(1) == version
PYVERIFY
cp "$APK" "$PAGE_STAGE/$APK_NAME"
APK="$PAGE_STAGE/$APK_NAME"
python3 devtools/generate_download_page.py "$VERSION" "$SERVER_URL" "$PAGE_STAGE" --platforms mac,connector,android --android-apk-name "$APK_NAME"
: > "$PAGE_STAGE/SHA256SUMS.txt"
"${SSH[@]}" "mkdir -p '$STAGE'"
for artifact in "$CLI" "$APK" "$DMG"; do
  name="${artifact##*/}"
  hash="$(shasum -a 256 "$artifact" | cut -d' ' -f1)"
  current="$("${SSH[@]}" "if [ -f '$DOWNLOAD_DIR/$name' ]; then sha256sum '$DOWNLOAD_DIR/$name'; fi" | cut -d' ' -f1)"
  if [[ "$hash" != "$current" ]]; then
    # Resume interrupted uploads in an unpublished directory. Never overwrite a
    # package that is already linked from the live page with a partial file.
    rsync --partial --progress -e "$RSYNC_SSH" "$artifact" "$REMOTE:$STAGE/"
    remote_hash="$("${SSH[@]}" "sha256sum '$STAGE/$name'" | cut -d' ' -f1)"
    [[ "$hash" == "$remote_hash" ]] || { echo "SHA256 mismatch: $name" >&2; exit 1; }
    "${SSH[@]}" "mv '$STAGE/$name' '$DOWNLOAD_DIR/$name'"
  else
    # A previous transport may have completed the same package while an
    # interrupted rsync left an unpublished partial copy behind.
    "${SSH[@]}" "rm -f '$STAGE/$name'"
  fi
  printf '%s  %s\n' "$hash" "$name" >> "$PAGE_STAGE/SHA256SUMS.txt"
done
# Every linked artifact is now complete and verified. Publish metadata before
# index.html; the old page remains usable until the final atomic rename.
rsync -r --partial --exclude='*.apk' -e "$RSYNC_SSH" "$PAGE_STAGE/" "$REMOTE:$STAGE/"
"${SSH[@]}" "set -e; cd '$DOWNLOAD_DIR'; sha256sum -c '$STAGE/SHA256SUMS.txt'; for name in CHANGELOG.md CHANGELOG.html SHA256SUMS.txt index.html; do mv '$STAGE/'\"\$name\" './'\"\$name\"; done; rmdir '$STAGE'"
PAGE_FINAL="$(curl -fsSL -o "$PAGE_STAGE/published.html" -w '%{url_effective}' --max-time 60 "$PAGE_URL")"
# Check the page URL exactly as a browser resolves relative download links.
python3 - "$PAGE_STAGE/published.html" "$PAGE_FINAL" "$VERSION" <<'PY'
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import sys
class Links(HTMLParser):
    hrefs = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a': self.hrefs.extend(v for k,v in attrs if k == 'href')
p = Links(); text = Path(sys.argv[1]).read_text(); p.feed(text)
version = sys.argv[3]
assert f'版本 {version}' in text
expected = {f'Agents%20Anywhere-{version}-universal.dmg', f'Agents%20Anywhere-Connector-{version}-linux-x86_64.tar.gz', f'agents-anywhere-{version}.apk'}
assert expected <= set(p.hrefs)
for href in expected:
    resolved = urlsplit(urljoin(sys.argv[2], href))
    assert resolved.path.startswith('/download/'), resolved.path
PY
for artifact in "$CLI" "$APK" "$DMG"; do
  name="${artifact##*/}"; encoded="${name// /%20}"
  # The full remote file was SHA256 checked above. Avoid downloading a second
  # 600 MB copy over the low-bandwidth link just to verify public routing.
  curl -fsSL --range 0-4095 --max-time 60 "${PAGE_FINAL%/}/$encoded" -o "$PAGE_STAGE/range"
  head -c 4096 "$artifact" > "$PAGE_STAGE/expected-range"
  cmp "$PAGE_STAGE/range" "$PAGE_STAGE/expected-range"
done
printf 'Released macOS + Linux Connector + Android %s at %s\n' "$VERSION" "$PAGE_FINAL"
