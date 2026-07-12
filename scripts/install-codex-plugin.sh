#!/usr/bin/env bash
# Install Atelier's Codex plugin from this checkout or its public Git repository.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_BIN="${CODEX_BIN:-$(command -v codex || true)}"
SOURCE="${ATELIER_PLUGIN_SOURCE:-${REPO}}"

if [[ -z "${CODEX_BIN}" ]]; then
  echo "atelier: Codex CLI not found on PATH" >&2
  exit 1
fi

if ! command -v atelier >/dev/null 2>&1; then
  echo "atelier: install the local app first with: bash ${REPO}/install.sh" >&2
  exit 1
fi

echo "atelier: registering Codex marketplace from ${SOURCE}"
"${CODEX_BIN}" plugin marketplace add "${SOURCE}" --json

echo "atelier: installing atelier@atelier"
"${CODEX_BIN}" plugin add atelier@atelier --json

echo "atelier: plugin installed; start a new Codex task to load its tools"
