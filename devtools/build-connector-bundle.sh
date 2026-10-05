#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:?usage: build-connector-bundle.sh <version> [output]}"
OUTPUT="${2:-connector/release/Agents Anywhere-Connector-${VERSION}-linux-x86_64.tar.gz}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/agents-anywhere-connector"
cp -R "$ROOT/connector" "$STAGE/agents-anywhere-connector/connector"
find "$STAGE/agents-anywhere-connector/connector" -type d \( -name __pycache__ -o -name .pytest_cache -o -name release \) -prune -exec rm -rf {} +
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
