#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:?usage: build-connector-bundle.sh <version> [output]}"
OUTPUT="${2:-connector/release/Agents Anywhere-Connector-${VERSION}-linux-x86_64.tar.gz}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/agents-anywhere-connector"
# Exclude generated trees before copying, rather than copying a potentially
# multi-gigabyte .venv only to remove it again afterwards.
mkdir -p "$STAGE/agents-anywhere-connector/connector"
tar -C "$ROOT/connector" \
  --exclude=.venv --exclude=release --exclude=__pycache__ \
  --exclude=.pytest_cache --exclude=.ruff_cache --exclude='*.pyc' \
  --exclude=uv.lock --exclude=.DS_Store \
  -cf - . | tar -C "$STAGE/agents-anywhere-connector/connector" -xf -
cat > "$STAGE/agents-anywhere-connector/install.sh" <<'INSTALL'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/ and run this script again." >&2
  exit 1
fi
cd "$ROOT/connector"
uv tool install --editable .
printf 'Installed anywhere-cli and agent-connector. Run: anywhere-cli pair --help\n'
INSTALL
chmod +x "$STAGE/agents-anywhere-connector/install.sh"
if [[ "$OUTPUT" = /* ]]; then TARGET="$OUTPUT"; else TARGET="$ROOT/$OUTPUT"; fi
mkdir -p "$(dirname "$TARGET")"
tar -C "$STAGE" -czf "$TARGET" agents-anywhere-connector
printf '%s\n' "$TARGET"
