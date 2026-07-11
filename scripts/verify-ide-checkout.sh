#!/usr/bin/env bash
# Prove a clean tree can build CM6 and run editor contracts + shell e2e.
# Must work in a fully isolated git worktree with no inherited node_modules.
# Usage: bash scripts/verify-ide-checkout.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

echo "==> required sources"
for f in \
  package.json \
  package-lock.json \
  assets/editor/shell.js \
  assets/editor/languages.js \
  assets/editor_factory.js \
  assets/cm6/editor.bundle.js \
  cm6-src/facade.js \
  cm6-src/package.json \
  cm6-src/package-lock.json \
  tests/contracts/editor-surface-contract.test.mjs \
  tests/e2e/editor-shell.spec.js
do
  if [[ ! -f "$f" ]]; then
    echo "FAIL: missing tracked artifact: $f" >&2
    exit 1
  fi
done
echo "OK sources present"

echo "==> root npm ci (Playwright + test deps — required for isolated checkout)"
# Always install from lockfile; do not reuse a parent tree's node_modules.
rm -rf node_modules
npm ci
test -d node_modules/@playwright/test
echo "OK root deps"

echo "==> cm6-src npm ci + rebuild (must match assets/cm6)"
rm -rf cm6-src/node_modules
npm --prefix cm6-src ci
npm --prefix cm6-src run build
test -f assets/cm6/editor.bundle.js
test -f assets/cm6/VERSION
echo "OK cm6 rebuild"

echo "==> playwright browsers (chromium; no-op if already cached)"
if [[ ! -x node_modules/.bin/playwright ]]; then
  echo "FAIL: node_modules/.bin/playwright missing after npm ci" >&2
  exit 1
fi
# Idempotent: reuses $HOME ms-playwright cache when present, downloads otherwise.
./node_modules/.bin/playwright install chromium
echo "OK playwright binary + chromium"

echo "==> node contracts"
node --test tests/contracts/*.test.mjs
echo "OK contracts"

echo "==> playwright editor e2e (local binary, no parent resolution)"
./node_modules/.bin/playwright test tests/e2e/cm6-facade.spec.js tests/e2e/editor-shell.spec.js
echo "OK editor e2e"

echo "==> zero-python"
bash scripts/check-no-python.sh

echo "verify-ide-checkout: ALL GREEN"
