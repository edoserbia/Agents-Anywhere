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

REMOTE="${AA_REMOTE:-aa-server}"
DOWNLOAD_DIR="${AA_DOWNLOAD_DIR:-/opt/aa-downloads}"
SERVER_URL="${AA_SERVER_URL:-http://124.220.147.199:4000}"
PAGE_URL="${AA_PAGE_URL:-http://124.220.147.199:4001}"

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
     r'agents-anywhere-[0-9.]+-release\.apk', f'agents-anywhere-{version}-release.apk'),
]
for path, pattern, replacement in edits:
    text = io.open(path, encoding="utf-8").read()
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f"could not bump {path}: pattern {pattern!r} matched {count} times")
    io.open(path, "w", encoding="utf-8").write(updated)
print(f"   versionCode {current_code} -> {next_code}")
PY

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  echo "==> running the version parity test"
  ( cd server && env -u AGENT_SERVER_DB_URL .venv/bin/python -m pytest tests/test_version.py -q )

  echo "==> building the Android APK"
  ( cd android && ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}" ./gradlew assembleDebug --console=plain -q )

  echo "==> building the macOS DMG (universal)"
  rm -f "desktop-workbench/release/Agents Anywhere-${VERSION}-universal.dmg"
  ( cd desktop-workbench && yarn dist:mac )
fi

APK="android/app/build/outputs/apk/debug/app-debug.apk"
DMG="desktop-workbench/release/Agents Anywhere-${VERSION}-universal.dmg"

for artifact in "$APK" "$DMG"; do
  if [[ ! -f "$artifact" ]]; then
    echo "missing artifact: $artifact" >&2
    exit 1
  fi
done

echo "==> uploading artifacts to $REMOTE:$DOWNLOAD_DIR"
scp -q "$APK" "$REMOTE:$DOWNLOAD_DIR/agents-anywhere-${VERSION}-debug.apk"
scp -q "$DMG" "$REMOTE:$DOWNLOAD_DIR/Agents Anywhere-${VERSION}-universal.dmg"

echo "==> verifying the uploads byte for byte"
for pair in \
  "$APK:agents-anywhere-${VERSION}-debug.apk" \
  "$DMG:Agents Anywhere-${VERSION}-universal.dmg"
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
ssh "$REMOTE" "cat > '$DOWNLOAD_DIR/SHA256SUMS.txt'" <<EOF
$DMG_HASH  Agents Anywhere-${VERSION}-universal.dmg
$APK_HASH  agents-anywhere-${VERSION}-debug.apk
EOF
ssh "$REMOTE" "cd '$DOWNLOAD_DIR' && shasum -a 256 -c SHA256SUMS.txt"

echo "==> updating the download page"
python3 - "$VERSION" "$SERVER_URL" > /tmp/aa-index.html <<'PY'
import sys
version, server_url = sys.argv[1], sys.argv[2]
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
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0b0b0c; color: #f3f4f6; }}
    .card {{ border-color: #2a2a2e; }}
    .sub, .card p, .note {{ color: #9ca3af; }}
    a.btn {{ background: #f3f4f6; color: #111827; }}
    code {{ background: #1f1f23; }}
  }}
</style>
</head>
<body>
  <h1>Agents Anywhere 客户端</h1>
  <p class="sub">版本 {version} · 服务端同步升级至 {version}</p>

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
scp -q /tmp/aa-index.html "$REMOTE:$DOWNLOAD_DIR/index.html"

echo "==> verifying the published page"
published="$(curl -s --max-time 20 "$PAGE_URL/" | grep -oE "版本 [0-9.]+" | head -1)"
if [[ "$published" != "版本 $VERSION" ]]; then
  echo "page still advertises '$published'" >&2
  exit 1
fi
echo "   $published at $PAGE_URL"

cat <<EOF

Released $VERSION.

  page      $PAGE_URL
  server    $SERVER_URL
  artifacts $REMOTE:$DOWNLOAD_DIR

Remaining steps (deliberately not automated):
  1. deploy the server image so /api/v2/health reports $VERSION
  2. commit the version bump and push
EOF
