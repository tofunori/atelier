# Matrice de parité des routes — migration Rust

_Source de vérité de la phase 0 du plan [rust-backend-full-migration-plan.md](rust-backend-full-migration-plan.md). Mise à jour obligatoire à chaque route portée. Le tableau entre les marqueurs `parity:begin/end` est vérifié automatiquement par `tests/contract/` contre le registre `tests/contract/support.py` **et** contre les deux serveurs vivants._

## Principes d'architecture

- **TypeScript = couche d'interface mince.** Le frontend affiche, capte les
  interactions et appelle l'API locale. Toute logique métier, validation,
  persistance, orchestration et intégration système vit dans Rust
  (`atelier-server`). Pas de rendu DOM/éditeurs/canvas en Rust/WASM sans
  nécessité démontrée. Les règles métier aujourd'hui dispersées dans les
  assets JS (ex. la banque d'annotations de `agent_bridge_ui.js` qui réécrit
  les POST `/quote`/`/save`) sont à porter comme comportement d'API.
- **Pas de renommage de route publique pendant le portage.**
- **Aucune validation assouplie pour obtenir artificiellement la parité** :
  un écart est déclaré ici, jamais masqué.
- `ATELIER_BACKEND=python` reste disponible jusqu'à la phase 9.

## Statuts

| Statut | Signification |
| --- | --- |
| `ported` | route servie par Rust, réponse conforme au harness de contrat |
| `partial` | route servie par Rust avec écarts déclarés (section « Différences déclarées ») |
| `missing` | route servie par Python seulement ; Rust doit répondre 404/405 |
| `rust-only` | route ajoutée par Rust, assumée et documentée |

Garantie exacte du harness : toute **transition de statut non déclarée**
fait échouer `tests/contract/test_route_inventory.py` — une route `missing`
qui se met à répondre côté Rust, une route `ported`/`partial` qui régresse,
une route `rust-only` qui apparaît côté Python, ou une divergence
matrice ↔ registre. Une route entièrement nouvelle, jamais déclarée dans
aucun des deux backends, n'est pas détectée automatiquement : l'ajout d'une
route passe par ce document d'abord (revue Codex).

## Vérification

```bash
python3 -m pytest tests/contract/test_route_inventory.py -v
python3 -m pytest tests/contract/test_response_contracts.py -v
# ou, via le runner du dépôt :
python3 -m unittest discover -s tests -v
```

Le harness démarre chaque backend avec `cmux_gallery.backend_command()`
(la même logique que la production), dans un projet fixture temporaire
(`tests/fixtures/rust-migration/` + `git init` + `build_gallery.py`) et un
HOME temporaire — aucune écriture hors sandbox. Les serveurs tournent en mode
agent (`ATELIER_AGENT_HOST=codex`), ce qui neutralise pbcopy et les push
cmux/muxy/orca.

## Matrice

<!-- parity:begin -->
| Méthode | Route | Phase | Statut | Notes |
| --- | --- | --- | --- | --- |
| `GET` | `/ping` | 0 | `partial` | clés divergentes déclarées : service, backend, revision, claudePreview, watcher.* |
| `GET` | `/rev` | 0 | `ported` | valeur volatile (mtime côté Python, compteur côté Rust) |
| `GET` | `/health` | 0 | `rust-only` | route Rust supplémentaire assumée (diagnostic) |
| `OPTIONS` | `*` | 8 | `ported` | préflight CORS global → 200 {} |
| `GET` | `/` | 2 | `ported` | réécrit vers figures_index.html |
| `GET` | `/data` | 2 | `ported` |  |
| `GET` | `/state` | 1 | `ported` |  |
| `POST` | `/state` | 1 | `ported` |  |
| `POST` | `/rescan` | 2 | `ported` | synchron : relance build_gallery.py |
| `POST` | `/agent-event` | 2 | `ported` | événement toast en mémoire {ok,id} |
| `POST` | `/claude-event` | 2 | `ported` | alias historique de /agent-event |
| `GET` | `/agent-events` | 2 | `ported` |  |
| `GET` | `/claude-events` | 2 | `ported` | alias historique de /agent-events |
| `GET` | `/provenance` | 2 | `ported` | fixture sans provenance déclarée : contrat = artefact trouvé sans commande |
| `POST` | `/regenerate` | 2 | `ported` | fixture sans commande déclarée → 409 dans les deux backends |
| `GET` | `/thumb` | 2 | `ported` |  |
| `GET` | `/rasterize` | 2 | `ported` | Chrome headless ; 501 si Chrome absent |
| `POST` | `/delete` | 2 | `ported` |  |
| `POST` | `/export` | 2 | `ported` |  |
| `POST` | `/open` | 2 | `ported` | probe volontairement refusée (aucune app ouverte pendant les tests) |
| `POST` | `/clear-quote` | 2 | `ported` |  |
| `GET` | `/claude-targets` | 2 | `ported` | mode agent = NO_PUSH → targets: [] sans sous-processus |
| `GET` | `/quote` | 2 | `ported` |  |
| `GET` | `/ls` | 1 | `ported` | sans query la requête tombe dans le fallback statique (dispatch startswith) |
| `GET` | `/snippet` | 1 | `ported` |  |
| `GET` | `/raw` | 1 | `ported` |  |
| `GET` | `/code` | 1 | `ported` |  |
| `GET` | `/texroot` | 1 | `ported` |  |
| `GET` | `/findscript` | 1 | `ported` |  |
| `GET` | `/lint` | 4 | `ported` | Rust : available:false hors ~/Documents|Desktop *.py — probe différée : Python n'enregistre /lint qu'en mode STUDIO ; Rust l'expose toujours |
| `POST` | `/codesave` | 1 | `ported` |  |
| `POST` | `/save-svg` | 1 | `ported` |  |
| `POST` | `/selinfo` | 1 | `ported` |  |
| `GET` | `/githead` | 3 | `ported` |  |
| `GET` | `/versions` | 3 | `ported` |  |
| `POST` | `/versions` | 3 | `ported` |  |
| `GET` | `/gitlog` | 3 | `ported` |  |
| `GET` | `/gitshow` | 3 | `ported` |  |
| `POST` | `/commitmsg` | 3 | `ported` | arbre propre → ok:false sans invocation du CLI claude |
| `POST` | `/gitcommit` | 3 | `ported` | arbre propre → ok:false ; aucun commit créé |
| `POST` | `/compile` | 4 | `ported` | probe hors projet → 403 rapide, sans lancer latexmk |
| `POST` | `/synctex` | 4 | `ported` |  |
| `GET` | `/pdfannot` | 4 | `ported` |  |
| `POST` | `/pdfannot` | 4 | `ported` | Rust : garde loopback + cap 64 Mo (Python n'en a pas — écart de sécu assumé) |
| `POST` | `/export-png` | 4 | `ported` | 501 si rsvg-convert absent de la machine |
| `GET` | `/notes/load` | 5 | `ported` |  |
| `POST` | `/notes/save` | 5 | `ported` |  |
| `GET` | `/board/load` | 5 | `ported` |  |
| `POST` | `/board/save` | 5 | `ported` |  |
| `GET` | `/board/poll` | 5 | `ported` |  |
| `POST` | `/board/command` | 5 | `ported` |  |
| `POST` | `/notes/open-surface` | 5 | `ported` | mode agent = NO_PUSH → 500 no-push assumé, aucun CLI lancé |
| `POST` | `/board/open-surface` | 5 | `ported` |  |
| `GET` | `/zotero-items` | 6 | `ported` | HOME temporaire sans bibliothèque : contrat = dégradation contrôlée |
| `GET` | `/zotero-collections` | 6 | `ported` |  |
| `POST` | `/zotero-fav` | 6 | `ported` |  |
| `POST` | `/zotero-add` | 6 | `ported` | probe différée : POSTerait au connecteur Zotero réel (port 23119) ; sondé hors suite |
| `GET` | `/zotero/<KEY>/<file>.pdf` | 6 | `ported` | Python n'enregistre la route qu'en STUDIO ; probe 404 hors studio |
| `POST` | `/orca-fullscreen-exit` | 7 | `ported` |  |
| `POST` | `/orca-native-fullscreen` | 7 | `ported` | probe refusée (fichier absent) : aucun viewer lancé |
| `GET` | `/agent-status` | 8 | `ported` |  |
| `GET` | `/agent-selections` | 8 | `ported` |  |
| `GET` | `/agent-selection` | 8 | `ported` | peek sans claim |
| `POST` | `/agent-selection` | 8 | `ported` | anchors/notes/delivery/cap 1 Mo alignés ; au-delà de 2 Mo axum répond 413 avant la garde |
| `POST` | `/agent-consumers/register` | 8 | `ported` |  |
| `POST` | `/agent-selections/ack` | 8 | `ported` |  |
| `POST` | `/agent-annotations/status` | 8 | `ported` |  |
| `POST` | `/agent-annotations/release` | 8 | `ported` |  |
| `POST` | `/agent-annotations/delete` | 8 | `ported` |  |
| `POST` | `/agent-annotations/restore` | 8 | `ported` |  |
| `POST` | `/agent-preferences` | 8 | `ported` |  |
| `POST` | `/agent-batches/release` | 8 | `ported` |  |
| `POST` | `/agent-batches/cancel` | 8 | `ported` |  |
| `POST` | `/quote` | 8 | `partial` | Rust : mise en file agent ; pbcopy/push hôte hors mode agent = optionnel |
| `POST` | `/save` | 8 | `partial` | écrit annotations/*.png ; push hôte hors mode agent = optionnel |
| `GET` | `static:/tiny.png` | 1 | `ported` | fallback statique confiné au projet |
| `HEAD` | `static:/tiny.png` | 1 | `ported` |  |
| `GET` | `static:/report.html` | 1 | `ported` | injection sel_overlay.js avant </body> |
| `GET` | `video:(Range)` | 2 | `ported` | Accept-Ranges + 206 si Range (probe sans header = 200) |
<!-- parity:end -->

## Différences déclarées (statut `partial` ou assumé)

| Route | Écart | Décision |
| --- | --- | --- |
| `GET /ping` | Python : `service:"fig-annotate"`, `claudePreview`, watcher `{lastScan,lastBuild,error:str}` ; Rust : `service:"atelier"`, `backend:"rust"`, `revision`, watcher `{lastEventAt,lastBuildAt,error:null?}` | le champ `backend` est requis par `cmux_gallery.server_backend()` ; l'alignement du reste des clés est dû en phase 2 |
| `GET /rev` | Python : mtime de `figures_index.html` ; Rust : compteur interne | contrat = « entier qui change à chaque rebuild » ; valeur déclarée volatile |
| `POST /quote`, `POST /save` | Rust ne fait que la mise en file agent ; Python ajoute pbcopy, `fig-last-quote.txt` et la livraison cmux/muxy/orca hors mode agent | livraison hôte = phase 7 (`HostIntegration`) |
| `GET *.html` (rapports) | Python injecte `<script src="/.fig_thumbs/sel_overlay.js">` ; Rust sert brut | injection à porter (sélection en direct) |
| `GET /claude-targets` (hors mode agent) | Rust renvoie `[]` sans scanner muxy/orca/cmux | peuplement CLI = phase 7 (`HostIntegration`) |
| `POST /export` | Rust ajoute `manifest.json` (folder/zip) en plus des fichiers Python | compatible (fichier supplémentaire) ; plan phase 2 |
| `POST /agent-selection` | parité complète (anchors, notes, delivery, historique, cap 1 Mo) sauf : un corps entre 2 Mo et l'infini est refusé 413 par axum avant la garde 400 `bad size` | assumé — le refus reste un refus ; revalider en phase 8 |
| `POST /save` | nom de fichier `_annot_<nanos>` côté Rust vs `_annot_<YYYYmmdd-HHMMSS>` côté Python | aligner le format d'horodatage en phase 7 avec la livraison hôte |
| `GET /health` | route Rust seulement | assumée : diagnostic `atelier doctor` |

## Valeurs volatiles déclarées (ignorées par la comparaison)

PID, port, timestamps (`ts`, `mtime`, `btime`, `started`, `lastScan`,
`lastBuild`, `lastEventAt`, `lastBuildAt`, `claimedAt`, `createdAt`),
identifiants aléatoires (`id` d'événement agent = epoch-ms + hex aléatoire),
noms de fichiers horodatés (`*_annot_<ts>.png`, `export_<ts>`), `rev`/`revision`,
sortie LLM de `POST /commitmsg`, chemins absolus machine (réécrits en
`<PROJECT>`/`<HOME>`/`<PORT>`).

## Modèle de sécurité (contrat commun)

- Liaison loopback par défaut ; Rust exige `ATELIER_ALLOW_REMOTE=1` **et**
  `ATELIER_AGENT_TOKEN` pour tout autre host.
- Trois niveaux : routes publiques locales (GET lecture), routes mutantes
  gardées par origine loopback (`_local_only` / `request_allowed`), routes
  agent gardées par bearer token (`ATELIER_AGENT_TOKEN`).
- Confinement chemins : `realpath` sous `PROJECT` (`_safe_path` /
  `safe_project_path`), symlinks sortants rejetés.
- CSP `sandbox` sur HTML/SVG non fiables en mode agent (les deux backends).

## Quirks Python à trancher (ne pas reproduire aveuglément)

| Quirk | Constat | Proposition |
| --- | --- | --- |
| `POST /pdfannot` | Python sans garde d'origine ni cap ; Rust exige loopback + cap 64 Mo | écart de sécurité volontaire (phase 4 faite) |
| `OPTIONS *` répond 200 partout | préflight CORS global + `Access-Control-Allow-Origin:*` sur les réponses JSON | à restreindre en phase 8 (la garde d'origine reste le vrai contrôle) |
| Helpers `osascript` Orca | code mort (jamais appelé par une route) ; `/orca-fullscreen-exit` est un no-op assumé | ne pas porter ; conserver le no-op |
| `/compile`/`/synctex` | Python : MacTeX hardcodé ; Rust : `/Library/TeX/texbin` → PATH → tectonic | fallback élargi (phase 4 faite) |
| `GET /lint` | Python : absente hors STUDIO ; Rust : toujours exposée | assumé — `available:false` hors Documents/Desktop |
| Caps de taille absents | plusieurs POST lisent `Content-Length` sans plafond | Rust : plafond global par route, identique aux caps existants sinon 64 Mo |

## Fixtures

Voir [tests/fixtures/rust-migration/README.md](../tests/fixtures/rust-migration/README.md).
Zotero phase 0 = absence de bibliothèque (dégradation contrôlée) ; base
synthétique en phase 6. Fixture vidéo (Range) en phase 2.
