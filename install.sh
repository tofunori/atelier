#!/usr/bin/env bash
# Atelier installer — Rust binaries only (no Python runtime required for serve/run).
# Usage: bash install.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${HOME}/.local/bin"
SHARE="${HOME}/.local/share/atelier"

echo "atelier: installing from ${REPO}"

mkdir -p "${BIN}" "${SHARE}"

# Prefer prebuilt dist/bin, else build release.
if [[ -x "${REPO}/dist/bin/atelier-server" && -x "${REPO}/dist/bin/atelier-cli" && -x "${REPO}/dist/bin/atelier-mcp" ]]; then
  echo "  → using prebuilt dist/bin"
  cp -f "${REPO}/dist/bin/atelier-server" "${REPO}/dist/bin/atelier-cli" \
    "${REPO}/dist/bin/atelier-mcp" "${BIN}/"
elif command -v cargo >/dev/null 2>&1 && [[ -f "${REPO}/rust/Cargo.toml" ]]; then
  echo "  → building release binaries…"
  bash "${REPO}/scripts/build-release.sh"
  cp -f "${REPO}/dist/bin/atelier-server" "${REPO}/dist/bin/atelier-cli" \
    "${REPO}/dist/bin/atelier-mcp" "${BIN}/"
else
  echo "error: no prebuilt binaries and cargo not found" >&2
  exit 1
fi
chmod +x "${BIN}/atelier-server" "${BIN}/atelier-cli" "${BIN}/atelier-mcp"

# Assets (gallery template + viewers)
if [[ -d "${REPO}/dist/share/atelier/assets" ]]; then
  ASSETS_SRC="${REPO}/dist/share/atelier/assets"
elif [[ -d "${REPO}/assets" ]]; then
  ASSETS_SRC="${REPO}/assets"
else
  echo "error: assets directory not found" >&2
  exit 1
fi
rm -rf "${SHARE}/assets"
cp -R "${ASSETS_SRC}" "${SHARE}/assets"
echo "  ✓ assets → ${SHARE}/assets"

ln -sf "${BIN}/atelier-cli" "${BIN}/atelier"
ln -sf "${BIN}/atelier-cli" "${BIN}/cmux-gallery"
echo "  ✓ ${BIN}/atelier → atelier-cli"
echo "  ✓ ${BIN}/atelier-server"
echo "  ✓ ${BIN}/atelier-mcp"

case ":${PATH}:" in
  *":${BIN}:"*) echo "  ✓ ${BIN} is on PATH" ;;
  *)
    echo "  ⚠ add ${BIN} to PATH (e.g. export PATH=\"\$HOME/.local/bin:\$PATH\")"
    ;;
esac

cat <<'EOF'

Done — pure Rust runtime (no Python).
  atelier build --root .
  atelier run --root .
  atelier doctor --root .
  atelier svg reapply figure.svg

Codex MCP: point plugins at `atelier-mcp` (already in plugins/atelier/.mcp.json).
EOF
