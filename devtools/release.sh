#!/usr/bin/env bash
#
# Cut a client release end to end.
#
# A release touches five version locations, two installers and a download page
# on the server. Doing that by hand is how the page ends up advertising the
# previous build, so the whole sequence lives here:
#
#   devtools/release.sh 2.0.4            # bump, build, publish, update page
#   devtools/release.sh 2.0.4 --no-build # bump and update the page only
#
# The page is generated from the version being released, so it cannot describe
# artifacts that were not uploaded.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# closex.cc is the production host. The previous host (124.220.147.199) is kept
# only as a rollback target; override AA_REMOTE to publish there.
REMOTE="${AA_REMOTE:-aa-new}"
DOWNLOAD_DIR="${AA_DOWNLOAD_DIR:-/opt/aa-downloads}"
SERVER_URL="${AA_SERVER_URL:-https://closex.cc}"
PAGE_URL="${AA_PAGE_URL:-https://closex.cc/download}"

VERSION="${1:-}"
SKIP_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --no-build) SKIP_BUILD=1 ;;
  esac
done

if [[ -z "$VERSION" ]]; then
  echo "usage: devtools/release.sh <version> [--no-build]" >&2
  echo "example: devtools/release.sh 2.0.4" >&2
  exit 2
fi
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "version must look like 2.0.4 (got: $VERSION)" >&2
  exit 2
fi

VERSION_FILE="server/agent_server/core/version.py"
PYPROJECT="server/pyproject.toml"
DESKTOP_PKG="desktop-workbench/package.json"
GRADLE="android/app/build.gradle.kts"
APPCONFIG="android/app/src/main/java/com/agentsanywhere/app/config/AppConfig.kt"

CURRENT="$(grep -oE 'SERVER_VERSION: Final = "[0-9.]+"' "$VERSION_FILE" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
echo "==> releasing $CURRENT -> $VERSION"

current_code="$(grep -oE 'versionCode = [0-9]+' "$GRADLE" | grep -oE '[0-9]+')"
next_code=$((current_code + 1))

if [[ "$SKIP_BUILD" -eq 1 ]]; then
  # Publishing an already-built release must not move the version again: running
  # the publish step twice would keep inflating versionCode and leave the source
  # disagreeing with the APK that was actually shipped.
  if [[ "$CURRENT" != "$VERSION" ]]; then
    echo "--no-build cannot publish $VERSION while the tree is at $CURRENT" >&2
    echo "run without --no-build to bump and build it first" >&2
    exit 1
  fi
  echo "==> publish only: tree stays at $VERSION (versionCode $current_code)"
else
echo "==> bumping version in all five locations"
python3 - "$VERSION" "$current_code" "$next_code" <<'PY'
import io, re, sys
version, current_code, next_code = sys.argv[1], sys.argv[2], sys.argv[3]
edits = [
    ("server/agent_server/core/version.py",
     r'SERVER_VERSION: Final = "[0-9.]+"', f'SERVER_VERSION: Final = "{version}"'),
    ("server/pyproject.toml",
     r'^version = "[0-9.]+"', f'version = "{version}"'),
    ("desktop-workbench/package.json",
     r'"version": "[0-9.]+"', f'"version": "{version}"'),
    ("android/app/build.gradle.kts",
     r'versionCode = [0-9]+', f'versionCode = {next_code}'),
    ("android/app/build.gradle.kts",
     r'versionName = "[0-9.]+"', f'versionName = "{version}"'),
    ("android/app/src/main/java/com/agentsanywhere/app/config/AppConfig.kt",
     r'agents-anywhere-[0-9.]+-debug\.apk', f'agents-anywhere-{version}-debug.apk'),
]
for path, pattern, replacement in edits:
    text = io.open(path, encoding="utf-8").read()
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f"could not bump {path}: pattern {pattern!r} matched {count} times")
    io.open(path, "w", encoding="utf-8").write(updated)
print(f"   versionCode {current_code} -> {next_code}")
PY
fi

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  echo "==> running the version parity test"
  ( cd server && env -u AGENT_SERVER_DB_URL .venv/bin/python -m pytest tests/test_version.py -q )

  echo "==> building the Android APK"
  ( cd android && ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}" ./gradlew assembleDebug --console=plain -q )

  echo "==> building the macOS DMG (universal)"
  rm -f "desktop-workbench/release/Agents Anywhere-${VERSION}-universal.dmg"
  ( cd desktop-workbench && yarn dist:mac )
  echo "==> building the Windows installer"
  rm -f "desktop-workbench/release/Agents Anywhere Setup ${VERSION}.exe"
  ( cd desktop-workbench && yarn dist:win )
  echo "==> building the Linux AppImage"
  rm -f "desktop-workbench/release/Agents Anywhere-${VERSION}-x86_64.AppImage"
  ( cd desktop-workbench && yarn dist )
fi

APK="android/app/build/outputs/apk/debug/app-debug.apk"
DMG="desktop-workbench/release/Agents Anywhere-${VERSION}-universal.dmg"
WIN="desktop-workbench/release/Agents Anywhere Setup ${VERSION}.exe"
LINUX="desktop-workbench/release/Agents Anywhere-${VERSION}-x86_64.AppImage"

for artifact in "$APK" "$DMG" "$WIN" "$LINUX"; do
  if [[ ! -f "$artifact" ]]; then
    echo "missing artifact: $artifact" >&2
    exit 1
  fi
done

echo "==> uploading artifacts to $REMOTE:$DOWNLOAD_DIR"
scp -q "$APK" "$REMOTE:$DOWNLOAD_DIR/agents-anywhere-${VERSION}-debug.apk"
scp -q "$DMG" "$REMOTE:$DOWNLOAD_DIR/Agents Anywhere-${VERSION}-universal.dmg"
scp -q "$WIN" "$REMOTE:$DOWNLOAD_DIR/Agents Anywhere-${VERSION}-x64.exe"
scp -q "$LINUX" "$REMOTE:$DOWNLOAD_DIR/Agents Anywhere-${VERSION}-x86_64.AppImage"

echo "==> verifying the uploads byte for byte"
for pair in \
  "$APK:agents-anywhere-${VERSION}-debug.apk" \
  "$DMG:Agents Anywhere-${VERSION}-universal.dmg" \
  "$WIN:Agents Anywhere-${VERSION}-x64.exe" \
  "$LINUX:Agents Anywhere-${VERSION}-x86_64.AppImage"
do
  local_path="${pair%%:*}"; remote_name="${pair##*:}"
  local_hash="$(shasum -a 256 "$local_path" | cut -d' ' -f1)"
  remote_hash="$(ssh "$REMOTE" "shasum -a 256 '$DOWNLOAD_DIR/$remote_name'" | cut -d' ' -f1)"
  if [[ "$local_hash" != "$remote_hash" ]]; then
    echo "hash mismatch for $remote_name" >&2
    echo "  local:  $local_hash" >&2
    echo "  remote: $remote_hash" >&2
    exit 1
  fi
  echo "   $remote_name ok ($local_hash)"
done

echo "==> writing SHA256SUMS.txt"
DMG_HASH="$(shasum -a 256 "$DMG" | cut -d' ' -f1)"
APK_HASH="$(shasum -a 256 "$APK" | cut -d' ' -f1)"
WIN_HASH="$(shasum -a 256 "$WIN" | cut -d' ' -f1)"
LINUX_HASH="$(shasum -a 256 "$LINUX" | cut -d' ' -f1)"
ssh "$REMOTE" "cat > '$DOWNLOAD_DIR/SHA256SUMS.txt'" <<EOF
$DMG_HASH  Agents Anywhere-${VERSION}-universal.dmg
$APK_HASH  agents-anywhere-${VERSION}-debug.apk
$WIN_HASH  Agents Anywhere-${VERSION}-x64.exe
$LINUX_HASH  Agents Anywhere-${VERSION}-x86_64.AppImage
EOF
ssh "$REMOTE" "cd '$DOWNLOAD_DIR' && shasum -a 256 -c SHA256SUMS.txt"

echo "==> updating the download page"
# The page's "what changed" section is generated from the release notes, so the
# page cannot describe a release it has no notes for. A missing notes file fails
# the release rather than publishing a page with no explanation of the update.
NOTES_FILE="docs/releases/${VERSION}.md"
if [[ ! -f "$NOTES_FILE" ]]; then
  echo "missing release notes: $NOTES_FILE" >&2
  echo "write them first; the download page is generated from them" >&2
  exit 1
fi
python3 devtools/generate_download_page.py "$VERSION" "$SERVER_URL" /tmp/aa-download-page
scp -q /tmp/aa-download-page/index.html "$REMOTE:$DOWNLOAD_DIR/index.html"
scp -q /tmp/aa-download-page/CHANGELOG.md "$REMOTE:$DOWNLOAD_DIR/CHANGELOG.md"
scp -q /tmp/aa-download-page/CHANGELOG.html "$REMOTE:$DOWNLOAD_DIR/CHANGELOG.html"
python3 - "$VERSION" "$SERVER_URL" "$NOTES_FILE" > /tmp/aa-index.html <<'PY'
import html
import re
import sys

version, server_url, notes_path = sys.argv[1], sys.argv[2], sys.argv[3]
notes = open(notes_path, encoding="utf-8").read()


def section(title: str) -> str:
    """Return the body of one `## <title>` section, without the heading."""
    match = re.search(
        rf"^## {re.escape(title)}\s*$(.*?)(?=^## |\Z)", notes, re.S | re.M
    )
    return match.group(1).strip() if match else ""


def to_list(body: str) -> str:
    """Render the section's bullets, keeping bold and inline code."""
    items = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        text = html.escape(line[2:].strip())
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        items.append(f"      <li>{text}</li>")
    return "\n".join(items)


# Lift the reader-facing bullets. Notes are written for maintainers and do not
# always use the same headings, so several are tried before falling back to the
# sub-headings of whichever section describes the change.
CHANGE_SECTIONS = ("产品变化", "服务端迁到新主机", "客户端变化", "本次变化")
changes = ""
for title in CHANGE_SECTIONS:
    body = section(title)
    if not body:
        continue
    changes = to_list(body)
    if changes:
        break
    heading_items = re.findall(r"^### (.+)$", body, re.M)
    if heading_items:
        changes = "\n".join(
            f"      <li><b>{html.escape(h)}</b></li>" for h in heading_items
        )
        break
html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agents Anywhere 客户端下载</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
         max-width: 46rem; margin: 3rem auto; padding: 0 1.25rem; line-height: 1.6; }}
  h1 {{ font-size: 1.6rem; margin-bottom: .25rem; }}
  .sub {{ color: #6b7280; margin-top: 0; }}
  .card {{ border: 1px solid #e5e7eb; border-radius: 14px; padding: 1.1rem 1.25rem; margin: 1rem 0; }}
  .card h2 {{ margin: 0 0 .35rem; font-size: 1.05rem; }}
  .card p {{ margin: .2rem 0 .7rem; color: #6b7280; font-size: .9rem; }}
  a.btn {{ display: inline-block; background: #111827; color: #fff; text-decoration: none;
          padding: .5rem 1rem; border-radius: 9px; font-size: .9rem; font-weight: 600; }}
  code {{ background: #f3f4f6; padding: .12rem .38rem; border-radius: 5px; font-size: .85em; }}
  .note {{ font-size: .85rem; color: #6b7280; }}
  .fix {{ border-left: 3px solid #10b981; padding-left: .8rem; margin: 1.2rem 0; }}
  .fix h3 {{ margin: 0 0 .3rem; font-size: .95rem; }}
  .fix ul {{ margin: .3rem 0; padding-left: 1.2rem; font-size: .9rem; color: #4b5563; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0b0b0c; color: #f3f4f6; }}
    .card {{ border-color: #2a2a2e; }}
    .sub, .card p, .note {{ color: #9ca3af; }}
    a.btn {{ background: #f3f4f6; color: #111827; }}
    code {{ background: #1f1f23; }}
    .fix ul {{ color: #9ca3af; }}
  }}
</style>
</head>
<body>
  <h1>Agents Anywhere 客户端</h1>
  <p class="sub">版本 {version} · 服务端同步升级至 {version}</p>

  <div class="fix">
    <h3>{version} 更新</h3>
    <ul>
{changes}
    </ul>
  </div>

  <div class="card">
    <h2>macOS 桌面客户端（Universal）· {version}</h2>
    <p>同时支持 Apple Silicon 与 Intel，一个安装包通用。未签名，首次打开需右键 →「打开」。</p>
    <a class="btn" href="Agents%20Anywhere-{version}-universal.dmg" download>下载 DMG</a>
  </div>

  <div class="card">
    <h2>Android 客户端（APK）· {version}</h2>
    <p>Debug 构建，可直接安装。系统需允许「安装未知来源应用」；已装同签名旧版可直接覆盖安装。</p>
    <a class="btn" href="agents-anywhere-{version}-debug.apk" download>下载 APK</a>
  </div>

  <div class="card">
    <h2>服务端 Web 控制台</h2>
    <p>部署在 <code>{server_url}</code>，浏览器直接打开即可。健康检查可访问 <code>/api/v2/health</code> 确认版本为 {version}。</p>
    <a class="btn" href="{server_url}" target="_blank" rel="noreferrer">打开 Web 控制台</a>
  </div>

  <p class="note">校验和见 <a href="SHA256SUMS.txt">SHA256SUMS.txt</a></p>
</body>
</html>
"""
sys.stdout.write(html)
PY
# The generated page above is authoritative; retain the legacy renderer below
# only as a syntax-compatible fallback for older release checkouts.

echo "==> verifying the published page"
# Follow redirects first: the page may be served at a URL that canonicalises
# with a trailing slash, and the links inside it are relative.
PAGE_FINAL="$(curl -s -o /dev/null -w '%{url_effective}' -L --max-time 30 "$PAGE_URL")"
published="$(curl -sL --max-time 30 "$PAGE_URL" | grep -oE "版本 [0-9.]+" | head -1)"
if [[ "$published" != "版本 $VERSION" ]]; then
  echo "page still advertises '$published'" >&2
  exit 1
fi
echo "   $published at $PAGE_FINAL"

# Fetch the installers exactly as a browser would: resolve the page's relative
# href against the URL the page actually ended up at, then download. Checking
# only that the link responds 200 is not enough — a mis-resolved relative link
# is answered by the site's HTML fallback with 200, which is how a 47 MB
# installer once downloaded as a 17 KB web page.
echo "==> verifying each installer downloads from the page"
PAGE_BASE="${PAGE_FINAL%/}/"
for entry in "agents-anywhere-${VERSION}-debug.apk:$APK" \
             "Agents Anywhere-${VERSION}-universal.dmg:$DMG" \
             "Agents Anywhere-${VERSION}-x64.exe:$WIN" \
             "Agents Anywhere-${VERSION}-x86_64.AppImage:$LINUX"
do
  name="${entry%%:*}"; local_path="${entry##*:}"
  # Percent-encode spaces the way a browser does.
  href="${name// /%20}"
  url="${PAGE_BASE}${href}"
  tmp="$(mktemp)"
  curl -sL --max-time 600 -o "$tmp" "$url" || { echo "download failed: $url" >&2; exit 1; }
  local_size="$(wc -c < "$local_path" | tr -d ' ')"
  got_size="$(wc -c < "$tmp" | tr -d ' ')"
  if [[ "$got_size" != "$local_size" ]]; then
    echo "$name downloaded $got_size bytes, expected $local_size" >&2
    echo "  url: $url" >&2
    echo "  (a much smaller file is usually the site's HTML fallback being saved" >&2
    echo "   as the installer, which means the link resolved to the wrong path)" >&2
    rm -f "$tmp"; exit 1
  fi
  if ! cmp -s "$tmp" "$local_path"; then
    echo "$name differs from the built artifact" >&2
    rm -f "$tmp"; exit 1
  fi
  rm -f "$tmp"
  echo "   $name ok ($got_size bytes, byte-identical)"
done

cat <<EOF

Released $VERSION.

  page      $PAGE_URL
  server    $SERVER_URL
  artifacts $REMOTE:$DOWNLOAD_DIR

Remaining steps (deliberately not automated):
  1. deploy the server image so /api/v2/health reports $VERSION
  2. commit the version bump and push
EOF
