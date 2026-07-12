#!/usr/bin/env bash
# Build release binaries for atelier-daemon + server + cli + mcp and stage under dist/bin.
# Usage: bash scripts/build-release.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO}"

if ! command -v cargo >/dev/null 2>&1; then
  echo "error: cargo not found — install Rust (https://rustup.rs)" >&2
  exit 1
fi

echo "atelier: cargo build --release (daemon + server + cli + mcp)"
cargo build --release --manifest-path rust/Cargo.toml \
  -p atelier-daemon -p atelier-server -p atelier-cli -p atelier-mcp

STAGE="${REPO}/dist/bin"
SHARE="${REPO}/dist/share/atelier"
mkdir -p "${STAGE}"
for name in atelier-daemon atelier-server atelier-cli atelier-mcp; do
  cp -f "rust/target/release/${name}" "${STAGE}/"
  chmod +x "${STAGE}/${name}"
  if command -v xattr >/dev/null 2>&1; then
    xattr -d com.apple.provenance "${STAGE}/${name}" 2>/dev/null || true
  fi
  if [[ "$(uname -s)" == "Darwin" ]] && command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - "${STAGE}/${name}"
  fi
done

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
mkdir -p "${SHARE}/packaging"
cp -f packaging/io.atelier.daemon.plist "${SHARE}/packaging/" 2>/dev/null || true

ARCH="$(uname -m)"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCHIVE="${REPO}/dist/atelier-${OS}-${ARCH}.tar.gz"
cp -f scripts/install-release.sh "${REPO}/dist/install.sh"
chmod +x "${REPO}/dist/install.sh"
tar -czf "${ARCHIVE}" -C "${REPO}/dist" bin share install.sh
(cd "${REPO}/dist" && shasum -a 256 "$(basename "${ARCHIVE}")" > "$(basename "${ARCHIVE}").sha256")
echo "  ✓ ${STAGE}/atelier-daemon  (${OS}/${ARCH})"
echo "  ✓ ${STAGE}/atelier-server"
echo "  ✓ ${STAGE}/atelier-cli"
echo "  ✓ ${STAGE}/atelier-mcp"
echo "  ✓ ${SHARE}/assets"
echo "  ✓ ${ARCHIVE} + sha256"
echo "  size: $(du -h "${STAGE}/atelier-daemon" | awk '{print $1}')"
echo
echo "Install locally:"
echo "  bash install.sh"
echo "Or copy binaries:"
echo "  cp dist/bin/atelier-* \"\${HOME}/.local/bin/\""
