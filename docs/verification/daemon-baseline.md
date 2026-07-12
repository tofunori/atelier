# Daemon migration baseline

Frozen before implementing `docs/plan-atelier-daemon-launchd.md`.

## Identity

| Field | Value |
| --- | --- |
| Date (UTC) | 2026-07-12T01:48:00Z |
| Git SHA | `54a19e709f18c3320dc93a1333327b3c89ebe372` |
| Host | macOS 26.5 (Build 25F71), arm64 |
| Plan | `docs/plan-atelier-daemon-launchd.md` |

## Untracked files (documented, not modified)

At Phase 0 start the working tree was clean for tracked content, with these untracked paths left alone:

- `.agents/`
- `dist/`
- `docs/plan-atelier-daemon-launchd.md`
- `notes.md`
- `test_wrap2.pdf`
- `tests/fixtures/editor/latex/long_article.out`
- `tests/fixtures/editor/latex/long_article.pdf`
- `tests/fixtures/editor/latex/long_article.toc`

## Test suite at baseline

| Command | Result |
| --- | --- |
| `bash scripts/check-no-python.sh` | OK |
| `cargo test --manifest-path rust/Cargo.toml --workspace` | core 11 ok, mcp 1 ok, server unit 12 ok; `http_smoke` 5 ok (one flaky ConnectionReset on first `rescan` run, green on retry) |
| `npm run typecheck` | OK |
| `npm run test:editor-contracts` | 32 pass / 0 fail |
| `npm run test:e2e` | not re-run in Phase 0 freeze (Playwright; assumed green from prior SHA `54a19e7`) |

## Live mono-project measurements

Process: `rust/target/debug/atelier-server --root <tmp> --port 19421 --host 127.0.0.1 --no-watch`

| Measure | Value |
| --- | --- |
| PID | 80570 |
| RSS at rest | 13968 KiB (~13.6 MiB) |
| `/ping` latency | 16 ms |
| `/figures_index.html` latency | 13 ms |
| Processes owned by one Codex MCP path | one `atelier-server` child per MCP (pre-daemon model) |
| Port model | stable hash port per project (MCP), not a single daemon port |

`/ping` body sample:

```json
{"ok":true,"service":"atelier","backend":"rust","project":"/private/tmp/atelier-baseline-yy89","revision":1,"watcher":{"enabled":false,"running":false,"lastEventAt":null,"lastBuildAt":null,"lastChanged":[],"error":null},"agentInbox":0}
```

## Same-project URL behaviour today

Two Codex tasks on the same project currently:

1. each load their own `atelier-mcp` stdio process;
2. each MCP may spawn/own an `atelier-server` for the project hash port;
3. URLs share the project port when the same server stays alive, but the server lifetime is tied to MCP ownership and can go stale when Codex reloads.

This is the failure mode the daemon removes.

## Second-project fixture

Added for isolation tests (does not change production behaviour):

```text
tests/fixtures/second-project/
├── README.md
├── figures_data.json
├── figures_index.html
├── notes.md
├── scripts/script.py
└── outputs/
    ├── figures/tiny.png
    └── results.csv
```

Filenames deliberately collide with other fixtures (`tiny.png`, `script.py`, `notes.md`).

## Exit criteria (Phase 0)

- [x] Starting SHA recorded
- [x] Existing tests green (Rust + contracts + no-python)
- [x] Untracked user files documented without modification
- [x] Baseline PID/RSS/latency captured
- [x] Independent second project fixture added
- [x] No unrelated user files rewritten
