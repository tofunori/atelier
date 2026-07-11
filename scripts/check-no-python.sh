#!/usr/bin/env bash
# Fail if production / test code still depends on a Python runtime.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

ALLOW='(tests/fixtures/|example/|notes-src/|whiteboard-src/|node_modules/|rust/target/|dist/share/|docs/)'

fail=0

# Production Python modules must not exist.
for f in \
  fig_annotate_server.py \
  build_gallery.py \
  cmux_gallery.py \
  atelier_runtime.py \
  zotero_to_gallery.py \
  native_fullscreen_viewer.py \
  reapply_svg_edits.py \
  integrations/codex/atelier_mcp.py
do
  if [[ -e "$f" ]]; then
    echo "FAIL: production Python file still present: $f" >&2
    fail=1
  fi
done

# No pytest / unittest discovery for the project.
if [[ -f package.json ]] && rg -n 'pytest|unittest discover' package.json >/dev/null 2>&1; then
  echo "FAIL: package.json still invokes pytest/unittest" >&2
  fail=1
fi

# No ATELIER_BACKEND=python fallback in Rust or installers.
hits=$(rg -n 'ATELIER_BACKEND.*python|backend fallback → python' \
  rust install.sh scripts plugins package.json \
  --glob '!**/target/**' --glob '!scripts/check-no-python.sh' 2>/dev/null \
  | rg -v "$ALLOW" || true)
if [[ -n "$hits" ]]; then
  echo "FAIL: Python backend fallback still referenced:" >&2
  echo "$hits" >&2
  fail=1
fi

# No python3 shebang outside fixtures/example.
while IFS= read -r -d '' file; do
  if [[ "$file" =~ tests/fixtures/|example/ ]]; then
    continue
  fi
  if head -1 "$file" 2>/dev/null | rg -q 'python'; then
    echo "FAIL: Python shebang in $file" >&2
    fail=1
  fi
done < <(find . -type f \( -name '*.py' -o -name '*.sh' \) \
  ! -path './node_modules/*' ! -path './rust/target/*' ! -path './.git/*' \
  ! -path './notes-src/*' ! -path './whiteboard-src/*' -print0 2>/dev/null)

# No production .py outside fixtures/example (content samples OK).
while IFS= read -r file; do
  if [[ "$file" =~ tests/fixtures/|example/ ]]; then
    continue
  fi
  echo "FAIL: unexpected Python file: $file" >&2
  fail=1
done < <(rg --files -g '*.py' 2>/dev/null | rg -v "$ALLOW" || true)

# Config launchers must not still call deleted Python servers.
if rg -n 'fig_annotate_server\.py|python@3\.[0-9]+.*atelier|GALLERY_ROOT=.*python' \
  .claude launch.json 2>/dev/null \
  | rg -v 'docs/|check-no-python' >/dev/null 2>&1; then
  echo "FAIL: Claude/cmux launch config still invokes Python fig_annotate_server:" >&2
  rg -n 'fig_annotate_server\.py|python@3\.[0-9]+' .claude 2>/dev/null || true
  fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  echo "check-no-python: FAILED" >&2
  exit 1
fi
echo "check-no-python: OK (no production Python runtime)"
