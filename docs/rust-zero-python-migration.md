# Plan de migration finale — Atelier sans Python

> **Statut (2026-07-11) : implémenté.** Runtime de production 100 % Rust.
> Inventaire : [rust-zero-python-inventory.md](rust-zero-python-inventory.md).
> Verrou : `bash scripts/check-no-python.sh`.

## Objectif

Transformer `cmux-gallery` en application autonome : backend, CLI, MCP et utilitaires en Rust, frontend CM6 en JavaScript local, zéro runtime, script, test, fallback ou invocation Python.

La galerie doit continuer à afficher et éditer les fichiers `.py` comme contenu.

## Contraintes

- Ne pas casser `atelier@atelier` ni les contrats HTTP/JSON.
- Préserver annotations, bridge Codex, Git, Zotero, SVG, PDF, LaTeX et éditeurs.
- Ne supprimer un composant Python qu’après validation de son équivalent.
- Préserver les changements utilisateur du worktree.
- Aucun fallback Python, même temporaire.
- Commits petits et vérifiables par phase.

## Phase 0 — Inventaire

Voir [rust-zero-python-inventory.md](rust-zero-python-inventory.md) — **fait**.

## Phase 1 — Rust comme runtime unique

Composants de production : `atelier-cli`, `atelier-server`, `atelier-mcp`, `atelier-core`.

1. Confirmer que `build`, `run`, `open`, `serve`, `status`, `doctor` et `stop` n’appellent jamais Python.
2. Supprimer `ATELIER_BACKEND=python` et toutes les branches de sélection du backend.
3. Confirmer que `plugins/atelier/.mcp.json` appelle `atelier-mcp`.
4. Limiter les installateurs aux binaires Rust et aux assets.
5. Supprimer toute recherche de `fig_annotate_server.py`.

Validation sans Python :

```bash
env PATH="/usr/bin:/bin:$HOME/.local/bin" atelier doctor
env PATH="/usr/bin:/bin:$HOME/.local/bin" atelier open --root .
```

## Phase 2 — Générateur Rust

Remplacer `build_gallery.py` et les primitives de `atelier_runtime.py`.

Confirmer dans `atelier-core::gallery_builder` : scan, exclusions, extensions, métadonnées, snippets, miniatures, dates, état, écritures atomiques, `figures_index.html` et `figures_data.json`.

Ajouter des tests Rust déterministes et comparer les sorties sémantiquement, sans timestamps volatils.

Sortie : `atelier build --root <fixture>` produit seul tous les artefacts. Supprimer ensuite le générateur Python.

## Phase 3 — Serveur Rust complet

Remplacer définitivement `fig_annotate_server.py`.

Prouver la parité pour :

- fichiers statiques et sécurité des chemins ;
- annotations d’images et citations ;
- file Codex, consommateurs et destinations ;
- envoi individuel et « Tout envoyer » ;
- historique, suppression et restauration ;
- Git, SVG, PDF, LaTeX et Zotero ;
- fichiers, renommage et suppression ;
- health, rescan, watcher et thèmes ;
- plein écran Orca ;
- CORS et authentification locale.

Pour chaque route : succès, entrée invalide, chemin interdit, fichier absent, cross-origin et persistance. Ajouter les tests de redémarrage, réutilisation et fermeture de stdin MCP.

Supprimer le serveur Python seulement après couverture Rust ou Playwright complète.

## Phase 4 — CLI Rust complet

Remplacer `cmux_gallery.py`.

Confirmer dans `atelier-cli` : racine, port stable, serveur détaché, réutilisation, fichiers d’état, `open`, `foreground`, `doctor`, `status`, `stop`, intégration cmux, ouverture du panneau, thème Codex et assets.

Remplacer les anciennes commandes Claude lançant Python. Supprimer ensuite `cmux_gallery.py`.

## Phase 5 — Utilitaires

### Zotero

Porter `zotero_to_gallery.py` vers :

```bash
atelier zotero export --source <dir> --out <dir> --link hardlink|copy
```

Préserver SQLite en lecture seule, métadonnées, collections, écritures atomiques et sécurité. Si les endpoints Rust remplacent entièrement l’export, documenter puis supprimer sans portage.

### SVG

Porter `reapply_svg_edits.py` vers :

```bash
atelier svg reapply FIG.svg
atelier svg reapply FIG.svg --edits FIG.edits.json
atelier svg reapply FIG.svg --output OUT.svg
atelier svg reapply FIG.svg --stdout
```

Préserver association Matplotlib, fallback texte, transformations, idempotence, erreurs de correspondance et écriture atomique.

### Plein écran

Supprimer `native_fullscreen_viewer.py` si macOS `open` suffit. Sinon créer un binaire Rust avec `objc2`/AppKit. Aucun fallback PyObjC.

## Phase 5 bis — Migrer tous les éditeurs vers CodeMirror 6

Cette phase est obligatoire. Elle doit reprendre les bons principes d’Atelier Studio sans créer de dépendance envers son dépôt.

### Architecture

- Ajouter un bundle CM6 local à `assets/cm6/`.
- Ajouter une fabrique d’éditeur locale, par exemple `assets/editor_factory.js`.
- CM6 doit être le moteur par défaut.
- CM5 peut rester temporairement disponible uniquement comme fallback JavaScript de migration.
- Aucun asset ne doit être chargé depuis un CDN.
- Aucun composant ne doit importer des fichiers depuis `atelier-studio` à l’exécution.

### Surfaces à migrer

- éditeur de code ;
- `latex_studio.html` ;
- éditeur Markdown ;
- éditeur et panneau diff ;
- recherche, remplacement, pliage, gutters et historique ;
- sélection de texte et composer d’annotations ;
- commentaires ancrés ;
- autocomplétion fantôme ;
- sauvegarde et détection des modifications ;
- navigation ligne/colonne ;
- wrap et raccourcis clavier.

### Adaptateur de compatibilité

Créer une couche explicite pour remplacer les appels CM5 encore utilisés, notamment :

- `getValue` et `setValue` ;
- `getCursor` et sélection ;
- `charCoords` ;
- `markText` et décorations persistantes ;
- gutters et marqueurs de lignes ;
- scroll vers une ligne ;
- événements `change`, `cursorActivity` et focus ;
- undo/redo ;
- lecture seule et reconfiguration dynamique.

Ne pas disperser des conditions `if CM5/CM6` dans toute l’interface : les différences doivent rester dans la fabrique et l’adaptateur.

### Tests de parité

Ajouter des tests Node et Playwright prouvant réellement :

- CM6 est instancié par défaut ;
- le contenu chargé est exact ;
- édition et sauvegarde fonctionnent ;
- sélections et annotations conservent les bonnes lignes ;
- commentaires et envois Codex fonctionnent ;
- diff, restauration et historique restent corrects ;
- LaTeX compile et navigue correctement ;
- Markdown et fichiers inconnus utilisent un langage ou texte brut sûr ;
- aucun test ne se contente de vérifier qu’une option `cm6` a été transmise.

### Retrait de CM5

1. Conserver CM5 uniquement pendant la période de validation CM6.
2. Ajouter un commutateur de diagnostic temporaire, par exemple `?editor=cm5`.
3. Effectuer les tests automatisés et une validation réelle dans Atelier/Codex.
4. Après parité complète, supprimer les scripts, styles, modes et branches CM5.
5. Ajouter un verrou CI empêchant le retour de `codemirror.min.js`, `CodeMirror(...)` ou des APIs CM5.

### Critère de sortie

- Toutes les surfaces éditables utilisent CM6 par défaut.
- CM5 est entièrement supprimé de la distribution finale.
- Les bundles CM6 sont locaux et reproductibles.
- Les annotations, commentaires, diff, sauvegarde et LaTeX sont vérifiés dans le navigateur.
- La migration n’introduit aucune dépendance Python ni dépendance runtime envers Atelier Studio.

## Phase 6 — Tests sans Python

- Rust : builder, serveur, sécurité, chemins, Git, annotations, Zotero, SVG, CLI, MCP, persistance, JSON.
- Node : assets, rendu, contrats frontend et bundle CM6.
- Playwright : galerie, éditeur, annotations, envois, commentaires, bulle déplaçable, thème, panneaux, sauvegarde et diff.

Ne retirer un test Python qu’après ajout d’un équivalent au moins aussi strict.

```bash
rg --files tests -g '*.py'
```

doit finalement ne rien retourner.

## Phase 7 — Build et CI

Retirer `pytest` et `unittest` de `package.json`.

Contrat cible :

```json
{
  "scripts": {
    "test": "npm run typecheck && cargo test --manifest-path rust/Cargo.toml && npm run test:contracts && npm run test:e2e",
    "test:contracts": "node --test tests/contracts/*.test.mjs"
  }
}
```

Nettoyer GitHub Actions, scripts shell, installateurs, release, hooks, Makefiles et documentation. Une machine avec Rust et Node, sans Python, doit pouvoir compiler, tester, installer et lancer Atelier.

## Phase 8 — Suppression finale

Après validation, supprimer :

- `fig_annotate_server.py` ;
- `build_gallery.py` ;
- `cmux_gallery.py` ;
- `atelier_runtime.py` ;
- `zotero_to_gallery.py` ;
- `native_fullscreen_viewer.py` ;
- `reapply_svg_edits.py` ;
- tous les tests Python.

Supprimer aussi `__pycache__`, `.pytest_cache`, configuration pytest, PyObjC, documentation obsolète et variables Python. Conserver `.py` parmi les formats affichables.

## Phase 9 — Verrou anti-régression

Créer `scripts/check-no-python.sh` ou un test Rust/Node échouant en présence de :

- shebang ou invocation Python ;
- fichier Python de production ou de test ;
- dépendance pytest, unittest ou PyObjC ;
- fallback `ATELIER_BACKEND=python`.

Seuls des fichiers `.py` utilisés comme contenu d’exemple peuvent être autorisés, sans jamais être exécutés.

## Phase 10 — Validation finale

```bash
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml
npm run typecheck
npm run test:contracts
npm run test:e2e
bash scripts/check-no-python.sh
bash scripts/build-release.sh
tar -tf dist/atelier-*.tar.gz | rg '\.py$|pytest|python3'
```

La dernière commande ne doit retourner aucune correspondance (les assets JS
`python.min.js` / CodeMirror lang-python sont des **mode éditeur**, pas un runtime).

Test sans Python :

```bash
env PATH="/usr/bin:/bin:$HOME/.local/bin" atelier doctor
env PATH="/usr/bin:/bin:$HOME/.local/bin" atelier open --root /tmp/atelier-smoke
```

Tester manuellement galerie, CM6, annotations, envoi individuel, « Tout envoyer », historique, SVG, PDF, LaTeX, Git, Zotero, plugin Codex et redémarrage.

## Définition de terminé

- Aucun fichier Python nécessaire.
- Aucun test, build ou commande utilisant Python.
- Aucune archive contenant Python.
- Plugin fonctionnel avec les seuls binaires Rust.
- Frontend CM6 local fonctionnel.
- Rust, Node et Playwright verts sur le checkout final.
- Installation et exécution possibles sur une machine sans Python.
- Parité prouvée avant chaque suppression.
- Aucun fallback Python silencieux.
