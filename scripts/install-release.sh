#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${HOME}/.local/bin"
SHARE="${HOME}/.local/share/atelier"

for name in atelier-server atelier-cli atelier-mcp; do
  [[ -x "${ROOT}/bin/${name}" ]] || { echo "missing bin/${name}" >&2; exit 1; }
done
[[ -f "${ROOT}/share/atelier/assets/gallery_template.html" ]] || {
  echo "missing share/atelier/assets" >&2; exit 1;
}

mkdir -p "${BIN}" "${SHARE}"
cp -f "${ROOT}/bin/atelier-server" "${ROOT}/bin/atelier-cli" \
  "${ROOT}/bin/atelier-mcp" "${BIN}/"
chmod +x "${BIN}/atelier-server" "${BIN}/atelier-cli" "${BIN}/atelier-mcp"
rm -rf "${SHARE}/assets"
cp -R "${ROOT}/share/atelier/assets" "${SHARE}/assets"
ln -sf "${BIN}/atelier-cli" "${BIN}/atelier"
ln -sf "${BIN}/atelier-cli" "${BIN}/cmux-gallery"
echo "Atelier installed in ${BIN} (Rust runtime; no Python, Node or Cargo required)."
