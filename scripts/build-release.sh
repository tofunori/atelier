#!/usr/bin/env bash
# Build release binaries for atelier-server + atelier-cli and stage them under dist/bin.
# Usage: bash scripts/build-release.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO}"

if ! command -v cargo >/dev/null 2>&1; then
  echo "error: cargo not found — install Rust (https://rustup.rs)" >&2
  exit 1
fi

echo "atelier: cargo build --release (atelier-server + atelier-cli)"
cargo build --release --manifest-path rust/Cargo.toml -p atelier-server -p atelier-cli -p atelier-mcp

STAGE="${REPO}/dist/bin"
SHARE="${REPO}/dist/share/atelier"
mkdir -p "${STAGE}"
cp -f rust/target/release/atelier-server "${STAGE}/"
cp -f rust/target/release/atelier-cli "${STAGE}/"
cp -f rust/target/release/atelier-mcp "${STAGE}/"
chmod +x "${STAGE}/atelier-server" "${STAGE}/atelier-cli" "${STAGE}/atelier-mcp"
if command -v xattr >/dev/null 2>&1; then
  xattr -d com.apple.provenance "${STAGE}/atelier-server" 2>/dev/null || true
  xattr -d com.apple.provenance "${STAGE}/atelier-cli" 2>/dev/null || true
  xattr -d com.apple.provenance "${STAGE}/atelier-mcp" 2>/dev/null || true
fi
if [[ "$(uname -s)" == "Darwin" ]] && command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - "${STAGE}/atelier-server"
  codesign --force --sign - "${STAGE}/atelier-cli"
  codesign --force --sign - "${STAGE}/atelier-mcp"
fi
# Frontend assets are part of the release and must compile successfully.
if command -v npm >/dev/null 2>&1 && [[ -f package.json ]]; then
  [[ -d node_modules ]] || npm ci --ignore-scripts
  npm run build:frontend
else
  echo "error: npm is required to build release frontend assets" >&2
  exit 1
fi
rm -rf "${SHARE}/assets"
mkdir -p "${SHARE}"
cp -R assets "${SHARE}/assets"

ARCH="$(uname -m)"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCHIVE="${REPO}/dist/atelier-${OS}-${ARCH}.tar.gz"
cp -f scripts/install-release.sh "${REPO}/dist/install.sh"
chmod +x "${REPO}/dist/install.sh"
tar -czf "${ARCHIVE}" -C "${REPO}/dist" bin share install.sh
(cd "${REPO}/dist" && shasum -a 256 "$(basename "${ARCHIVE}")" > "$(basename "${ARCHIVE}").sha256")
echo "  ✓ ${STAGE}/atelier-server  (${OS}/${ARCH})"
echo "  ✓ ${STAGE}/atelier-cli"
echo "  ✓ ${STAGE}/atelier-mcp"
echo "  ✓ ${SHARE}/assets"
echo "  ✓ ${ARCHIVE} + sha256"
echo "  size: $(du -h "${STAGE}/atelier-server" | awk '{print $1}')"
echo
echo "Install locally:"
echo "  bash install.sh"
echo "Or copy binaries:"
echo "  cp dist/bin/atelier-* \"\${HOME}/.local/bin/\""
