#!/usr/bin/env bash
# Prove a clean tree can build CM6 and run editor contracts + shell e2e.
# Usage: bash scripts/verify-ide-checkout.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

echo "==> required sources"
for f in \
  assets/editor/shell.js \
  assets/editor/languages.js \
  assets/editor_factory.js \
  assets/cm6/editor.bundle.js \
  cm6-src/facade.js \
  cm6-src/package.json \
  tests/contracts/editor-surface-contract.test.mjs \
  tests/e2e/editor-shell.spec.js
do
  if [[ ! -f "$f" ]]; then
    echo "FAIL: missing tracked artifact: $f" >&2
    exit 1
  fi
done
echo "OK sources present"

echo "==> cm6-src install + rebuild (must match assets/cm6)"
if [[ ! -d cm6-src/node_modules ]]; then
  npm --prefix cm6-src ci
fi
npm --prefix cm6-src run build
test -f assets/cm6/editor.bundle.js
test -f assets/cm6/VERSION
echo "OK cm6 rebuild"

echo "==> node contracts"
node --test tests/contracts/*.test.mjs
echo "OK contracts"

echo "==> playwright editor e2e"
npx playwright test tests/e2e/cm6-facade.spec.js tests/e2e/editor-shell.spec.js
echo "OK editor e2e"

echo "==> zero-python"
bash scripts/check-no-python.sh

echo "verify-ide-checkout: ALL GREEN"
