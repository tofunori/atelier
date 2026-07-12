# Inventaire zéro-Python (phase 0)

| Fonction Python (ex) | Équivalent Rust | Tests | Parité | Décision |
|---|---|---|---|---|
| `fig_annotate_server.py` | `atelier-server` | `http_smoke` + e2e | complète (phases 0–8) | **supprimé** |
| `build_gallery.py` / `atelier_runtime` | `atelier-core::gallery_builder` | unit + rescan smoke | complète | **supprimé** |
| `cmux_gallery.py` | `atelier-cli` | CLI manuelle + smoke | complète | **supprimé** |
| `integrations/codex/atelier_mcp.py` | `atelier-mcp` | plugin `.mcp.json` | complète | **supprimé** |
| `reapply_svg_edits.py` | `atelier-core::svg_edits` + `atelier svg reapply` | unit svg_edits | complète | **supprimé** |
| `native_fullscreen_viewer.py` | macOS `open` via `host.rs` | probe 400/200 | équivalent | **supprimé** |
| `zotero_to_gallery.py` | API `/zotero-*` déjà en serveur | smoke + contrats historiques | endpoints remplacent export CLI | **supprimé** (API HTTP) |
| tests contract dual Python/Rust | `http_smoke` + Playwright | cargo + e2e | smoke strict | **supprimé** pytest |
| `tests/fixtures/**/*.py` | contenu d’exemple (non exécuté) | — | — | **conservé** |

## Runtime unique

- Install : `install.sh` / `dist/install.sh` → binaires Rust + assets
- MCP : `plugins/atelier/.mcp.json` → `atelier-mcp`
- Rebuild watcher/rescan : `gallery_builder::build` (plus de `python3 build_gallery.py`)
- Verrou : `scripts/check-no-python.sh`
