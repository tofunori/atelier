#!/usr/bin/env bash
# atelier installer — links the CLI onto PATH, builds the Rust backend when possible.
# Usage:  bash install.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${HOME}/.local/bin"
LINK="${BIN}/atelier"
LEGACY_LINK="${BIN}/cmux-gallery"   # compat alias

echo "atelier: installing from ${REPO}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "  ⚠ python3 not found — gallery *rebuild* (build_gallery.py) needs it;"
  echo "    the Rust server can still serve an already-built project."
else
  echo "  ✓ $(python3 --version 2>&1)"
fi

mkdir -p "${BIN}"
chmod +x "${REPO}/cmux_gallery.py"
ln -sf "${REPO}/cmux_gallery.py" "${LINK}"
ln -sf "${REPO}/cmux_gallery.py" "${LEGACY_LINK}"
echo "  ✓ linked ${LINK}  (high-level CLI: run/build/doctor/status)"

# Rust backend (default since phase 9)
RUST_OK=0
if command -v cargo >/dev/null 2>&1 && [[ -f "${REPO}/rust/Cargo.toml" ]]; then
  echo "  → building release binaries (atelier-server, atelier-cli)…"
  if bash "${REPO}/scripts/build-release.sh"; then
    cp -f "${REPO}/dist/bin/atelier-server" "${BIN}/atelier-server"
    cp -f "${REPO}/dist/bin/atelier-cli" "${BIN}/atelier-cli"
    chmod +x "${BIN}/atelier-server" "${BIN}/atelier-cli"
    if command -v xattr >/dev/null 2>&1; then
      xattr -d com.apple.provenance "${BIN}/atelier-server" 2>/dev/null || true
      xattr -d com.apple.provenance "${BIN}/atelier-cli" 2>/dev/null || true
    fi
    if [[ "$(uname -s)" == "Darwin" ]] && command -v codesign >/dev/null 2>&1; then
      codesign --force --sign - "${BIN}/atelier-server"
      codesign --force --sign - "${BIN}/atelier-cli"
    fi
    echo "  ✓ installed ${BIN}/atelier-server"
    echo "  ✓ installed ${BIN}/atelier-cli"
    RUST_OK=1
  else
    echo "  ⚠ cargo build failed — Python fallback will be used until Rust is built" >&2
  fi
elif [[ -x "${REPO}/dist/bin/atelier-server" ]]; then
  cp -f "${REPO}/dist/bin/atelier-server" "${BIN}/atelier-server"
  [[ -x "${REPO}/dist/bin/atelier-cli" ]] && cp -f "${REPO}/dist/bin/atelier-cli" "${BIN}/atelier-cli"
  chmod +x "${BIN}/atelier-server" 2>/dev/null || true
  echo "  ✓ installed prebuilt dist/bin/atelier-server"
  RUST_OK=1
elif command -v atelier-server >/dev/null 2>&1; then
  echo "  ✓ atelier-server already on PATH: $(command -v atelier-server)"
  RUST_OK=1
else
  echo "  ⚠ no Rust toolchain and no prebuilt binary — set ATELIER_BACKEND=python or install rustup"
fi

# Is ~/.local/bin on PATH?
case ":${PATH}:" in
  *":${BIN}:"*) echo "  ✓ ${BIN} is on your PATH" ;;
  *)
    case "${SHELL##*/}" in
      zsh)  rc="${HOME}/.zshrc" ;;
      bash) rc="${HOME}/.bashrc" ;;
      *)    rc="your shell rc file" ;;
    esac
    echo "  ⚠ ${BIN} is NOT on your PATH. Add it with:"
    echo "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ${rc}"
    echo "      exec \"\$SHELL\""
    ;;
esac

if command -v cmux >/dev/null 2>&1; then
  echo "  ✓ cmux CLI found"
else
  echo "  ⚠ cmux CLI not found — 'build' works; 'run'/'serve'/open need cmux (https://cmux.com)"
fi

cat <<EOF

Done. Backend default is **Rust** (phase 9).
  atelier run             # build + serve (Rust) + open
  atelier status          # project + server health
  atelier doctor          # diagnose runtime
  atelier-cli doctor --port <n>   # probe Rust /health only
  atelier stop

Force legacy Python for one session:
  ATELIER_BACKEND=python atelier run --no-open

EOF

if [[ "${RUST_OK}" -eq 1 ]]; then
  echo "Rust server binary: $(command -v atelier-server 2>/dev/null || echo "${BIN}/atelier-server")"
else
  echo "Note: Rust binary missing — atelier will log a python fallback until you run:"
  echo "  bash scripts/build-release.sh && bash install.sh"
fi
