# Contrat de surface éditeur (Phase 0)

Inventaire figé le 2026-07-11 avant extraction du shell CM6 unifié.
Voir aussi `docs/plan-ide-unifie-cm6-runtime-rust.md`.

## Matrice extension → surface

| Extension | Surface | Page actuelle | Mode CM6 | Dette |
| --- | --- | --- | --- | --- |
| `.rs` | code | `code_editor.html` | rust | non |
| `.py` | code | `code_editor.html` | python | non |
| `.r` / `.R` | code | `code_editor.html` | r | non |
| `.jl` | code | `code_editor.html` | julia | non |
| `.sh` / `.bash` | code | `code_editor.html` | shell | non |
| `.js` / `.jsx` | code | `code_editor.html` | javascript | partiel (peu de routes galerie) |
| `.ts` / `.tsx` | code | `code_editor.html` | typescript | partiel |
| `.json` | code | `code_editor.html` | json | partiel |
| `.toml` / `.yaml` / `.yml` | code | `code_editor.html` | text | grammaire à ajouter |
| `.tex` / `.sty` / `.bib` | latex | `latex_studio.html` | stex | non |
| `.md` | markdown | `md_viewer.html` (canonique) | markdown | non (md_studio optionnel) |

## Routes d’ouverture inventoriées

| Entrée | Comportement actuel |
| --- | --- |
| Galerie → onglet (USE_TABS / iframe) | PDF→`pdf_viewer`, MD→`md_viewer`, TeX→`latex_studio`, code→`code_editor` |
| Galerie → lightbox | TeX→`latex_studio`, PDF→`pdf_viewer`, MD→`md_viewer`, code→`code_editor`, SVG→`svg_viewer` |
| IDE chip / browse | `code_editor.html?browse=1` |
| Explorateur Open… / browser | `.tex`→`latex_studio`, sinon `code_editor` |
| URL directe | `?path=` sur la page surface |
| Session restaurée | `saveGallerySession` + `studioRecents` + onglets TabShell |

## Exceptions intentionnelles (dette classée)

1. **Markdown WYSIWYG optionnel** — route canonique = `md_viewer.html` (shell + aperçu). `md_studio.html` (Toast UI) reste disponible mais n’est plus la route galerie.
2. **LaTeX studio monolithe** — `latex_studio.html` reste la surface primaire ; le module `assets/editor/modules/latex.js` est montable via shell (`?surface=latex`) mais SyncTeX/outline complets restent dans le studio jusqu’à parité prouvée.
3. **Préférences dual-write** — migration vers `atelier.editor.v1.*` avec dual-write legacy pendant la transition.
4. **CM5 fallback** — `?editor=cm5` encore supporté via `editor_factory.js` (retrait phase 8 + approbation humaine).
5. **Assets dist/** — le snapshot `dist/share/atelier/assets` peut retarder les sources ; l’install live `~/.local/share/atelier/assets` doit matcher les sources.
6. **LSP** — non livré (phase 7).

## Raccourcis capturés

| Surface | Raccourci | Action |
| --- | --- | --- |
| code | ⌘/Ctrl+S | save |
| code | ⌥Q | rewrap paragraphe / sélection |
| code | ⇧⌥Q | rewrap tous les blocs commentaires |
| code | Esc | effacer marque de sélection agent |
| latex | ⌘/Ctrl+S | save |
| latex | ⌘/Ctrl+Enter | compile (si présent) |
| markdown | ⌘/Ctrl+S | save |

## États obligatoires (cible shell)

| État | Signal | aria-label |
| --- | --- | --- |
| propre | coche | `saved` |
| modifié | point ambre | `modified` |
| sauvegarde | indicateur bref | `saving` |
| conflit | `!` rouge | message conflit |
| rewrap ok | coche locale | nombre de blocs |
| rien à faire | point ambre local | `rien à reformater` |

## Fixtures

`tests/fixtures/editor/sample.{rs,py,r,jl,sh,tex,md,toml,yaml,js,ts,json}`

## Gate A

- [x] matrice d’ouverture documentée
- [x] fixtures disponibles
- [x] tests contrat Node (`tests/contracts/editor-surface-contract.test.mjs`)
- [x] exceptions intentionnelles documentées
- [x] suite zéro-Python + e2e éditeur verts

## Gate B (shell par défaut — code/markdown)

- [x] shell CM6 pour code (`code_editor.html` bootstrap mince)
- [x] module markdown avec aperçu (`md_viewer.html`)
- [x] thème / wrap / rewrap / save couverts par tests e2e shell
- [x] galerie et explorateur convergent (MD → `md_viewer`, code → `code_editor`)
- [x] assets installés resynchronisés (`~/.local/share/atelier/assets`)

## Gate C (LaTeX — 2026-07-11)

- [x] fixtures `tests/fixtures/editor/latex/` (main, broken, comments, root+\input)
- [x] assertions PDF, erreurs, logs (`log` dans `/compile`), outline
- [x] commentaires ancrés avant/après rewrap + reload (reanchor par contenu)
- [x] SyncTeX view (source→PDF) et edit (PDF→source)
- [x] helpers partagés sous `assets/editor/modules/latex/*` branchés dans `latex_studio.html` (sans suppression des fonctions studio)
- [x] e2e `tests/e2e/latex-parity.spec.js` + contrats `tests/contracts/latex-surface-contract.test.mjs`
- [x] e2e « codex annotation bank » corrigé (`agent_bridge_ui.js` visible en top-level)

## Restant (après Gate C)

- Phase 4 fin : retirer le monolithe `latex_studio` seulement après soak (studio délègue déjà aux helpers)
- Phase 7 : LSP
- Phase 8 : retrait CM5 (soak + approbation humaine)
- Migration URLs de session historiques
- Test multi-iframe simultané
