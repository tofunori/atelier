# Inventaire détaillé des routes — `fig_annotate_server.py`

_Complément de [rust-route-parity.md](rust-route-parity.md) : le détail par
route (paramètres, codes HTTP, réponses, fichiers lus/écrits, sous-processus)
extrait du serveur Python de référence, phase 0 du plan de migration.
Les numéros de lignes se réfèrent à l'état du fichier au 11 juillet 2026._

## Configuration globale

- Liaison **loopback uniquement** : `ThreadingHTTPServer(("127.0.0.1", PORT))`
  (`fig_annotate_server.py:3622`). Port : `FIG_PORT` env, sinon `PORT`, sinon 8790.
- `PROJECT = realpath(GALLERY_ROOT | cwd)` (`:26`) — racine de confinement.
- `ASSETS_DIR` = `assets/` de l'installation (viewers « toujours frais » servis
  sous `/.fig_thumbs/<asset>` avant le fallback projet).
- Modes : `ATELIER_STUDIO` → STUDIO ; `CLAUDE_PREVIEW` ; `ATELIER_AGENT_HOST=codex`
  → CODEX_PREVIEW ; `ATELIER_AGENT_TOKEN` ; `NO_PUSH = STUDIO|CLAUDE_PREVIEW|CODEX_PREVIEW`
  (supprime pbcopy et les push cmux/muxy/orca). `GALLERY_WATCH=0` désactive le watcher.
- Concurrence : sémaphores `_THUMB_SEM` (2-8 selon CPU) et `_CHROME_SEM` (2).

## Modèle de sécurité

- `_respond()` ajoute `Access-Control-Allow-Origin: *` ; `do_OPTIONS` répond
  200 `{}` sur **tout** chemin (préflight global).
- `_local_only()` (`:1776`) : requête acceptée si `Origin` absent ou hôte
  ∈ {127.0.0.1, localhost, ::1}. Appliqué à **tous les POST sauf `/pdfannot`**
  et à 3 GET (`/agent-status`, `/findscript`, `/provenance`).
- `_agent_authorized()` (`:1790`) : CODEX_PREVIEW + token +
  `Authorization: Bearer` en `compare_digest`. Requis sur : GET `/agent-selections`,
  GET `/agent-selection`, POST `/agent-consumers/register`,
  `/agent-annotations/status`, `/agent-selections/ack`, `/agent-selection`.
- `_safe_path()` (`:1796`) : expanduser → join sous PROJECT → realpath → doit
  rester sous PROJECT (symlinks sortants rejetés). `translate_path()` pinne le
  statique de la même façon. CSP `sandbox` injectée sur HTML/SVG non fiables en
  CODEX_PREVIEW.
- `GET /zotero/*` : validation séparée sous `~/Zotero/storage` (clé
  `[A-Za-z0-9]{8}`, fichier `.pdf`).

## Fichiers d'état

| Fichier | Rôle | Routes |
| --- | --- | --- |
| `PROJECT/.fig_state.json` | favoris/notes/tags/collections/workflow | GET/POST `/state` |
| `PROJECT/.fig_thumbs/pdf_annots.json` (+`.bak`) | annotations PDF | GET/POST `/pdfannot` |
| `PROJECT/.fig_thumbs/board.tldr.json` | whiteboard tldraw | `/board/load`, `/board/save` |
| `PROJECT/notes.md` | notes | `/notes/load`, `/notes/save` |
| `PROJECT/.fig_thumbs/dv_versions/<md5>.json` (gzip, `.bak`) | historique versions | GET/POST `/versions` |
| `PROJECT/.fig_thumbs/imgthumb_<md5>.png`, `rast_<md5>.png` | caches miniatures | `/thumb`, `/rasterize` |
| `PROJECT/figures_index.html`, `figures_data.json` | galerie générée | `/`, `/data`, `/rev`, rebuilds |
| `PROJECT/annotations/*.png` | captures annotées | POST `/save` |
| `PROJECT/_gallery_exports/*` | exports | POST `/export` |
| `<svg>.orig.bak`, `<base>.edits.json` | sidecars éditeur SVG | POST `/save-svg` |
| `~/.claude/fig-last-quote.txt` | dernière annotation poussée | `/quote`, `/save`, `/clear-quote` |
| `~/.claude/fig-selection.json` | sélection en direct | POST `/selinfo` |
| `~/Library/Application Support/Atelier/agent-inbox/<sha256[:24]>{,-history,-consumers}.json` | bridge agent | routes `/agent-*` |
| `~/Library/Application Support/cmux-gallery/zotero-read.sqlite`, `zotero-favs.json` | copie lecture Zotero + favoris | `/zotero-*` |
| `~/.Trash` | corbeille | POST `/delete` |

## Threads d'arrière-plan

- Watcher d'artefacts (poll 1,5 s, debounce 1,2 s) → rebuild via
  `build_gallery.py` (300 s, single-flight `_REBUILD_LOCK`).
- Réveil Codex automatique (`codex exec … resume <threadId>`, 1800 s) quand un
  consommateur `automatic` existe.
- Push-vers-Claude en thread détaché dans POST `/save`.

## Sous-processus (argv, jamais de shell)

| Exécutable | Routes | Timeout |
| --- | --- | --- |
| `rsvg-convert` | `/thumb` (svg), `/export-png` | 20 s / 120 s |
| Chrome headless | `/thumb` (html), `/rasterize` | 25 s / 30 s (killpg) |
| `sips` | `/thumb`, contact sheet | 15-20 s |
| `rg` | `/findscript` | 15 s |
| `git` | `/githead /versions /gitlog /gitshow /commitmsg /gitcommit` | 10-20 s |
| `claude -p --model haiku` (outils désactivés) | `/commitmsg` | 20 s |
| `python3 build_gallery.py` | `/rescan` (300 s, killpg + pkill qlmanage), `/delete` (fire-and-forget), watcher | 300 s |
| commande `provenance.command` déclarée | `/regenerate` | 900 s |
| `/Library/TeX/texbin/latexmk` / `synctex` | `/compile` / `/synctex` | 180 s / 10 s |
| `open`, `pbcopy`, `cmux`, `muxy`, `orca`, `osascript`(mort) | actions/push | 5-10 s |
| `python3 native_fullscreen_viewer.py` | `/orca-native-fullscreen` | détaché |

## Valeurs non déterministes (à ignorer dans les comparaisons)

`id` d'événement agent (`epoch-ms + token_hex(4)`), noms horodatés
(`*_annot_<ts>.png`, `export_<ts>`), `pid`, timestamps (`ts`, `statusAt`,
`lastSeen`, `claimedAt`, watcher), `rev` (mtime), sortie LLM de `/commitmsg`,
sha/ts git (déterministes à dépôt fixé).

## Détail par route

Le format : **params** → **réponses** (codes : clés JSON) ; **FS** = fichiers lus/écrits.

### GET

| Route | Params | Réponses | FS / effets |
| --- | --- | --- | --- |
| `/` | query préservée | 200 html | lit `figures_index.html` |
| `/zotero/<KEY>/<f>.pdf` | (STUDIO/CLAUDE_PREVIEW) | 200 pdf ; 404/500 `{error}` | lit `~/Zotero/storage` |
| `/thumb` | `path`*, `w` (64-2000, déf. 480) | 200 png/binaire (`Cache-Control: max-age=86400`) ; 404/500 | écrit cache `imgthumb_*` |
| `/snippet` | `path`*, `n` (1-40, déf. 10) | 200 text/plain (600 c max) ; 404/500 | lit le fichier |
| `/zotero-items` | `q`, `collection` | 200 `{items[, error]}` | copie la base si mtime changé |
| `/zotero-collections` | — | 200 `{collections[, error]}` | lecture seule |
| `/claude-events`, `/agent-events` | `since` (déf. 0) | 200 `{events, last}` | mémoire (cap 100) |
| `/agent-status` † | `limit` (1-200, déf. 50) | 200 `{ok, agentHost, consumers, pending, history, counts}` ; 403 | lit inbox/history/consumers |
| `/agent-selections` †‡ | `consumer`* (≤200), `destination` (≤240) | 200 `{items, count}` ; 400/403 | **claim** (bail 300 s) + màj history |
| `/agent-selection` †‡ | — | 200 `{ok, usage, pending, latest}` ; 403 | lecture seule |
| `/data` | — | 200 json brut (`no-cache`) ; 404/500 | lit `figures_data.json` |
| `/ls` | `dir` (déf. PROJECT) — dispatch `startswith("/ls?")` | 200 `{path, parent, items[{name,dir}]}` ; 404/400/500 | listing |
| `/texroot` | `path`* | 200 `{root, pdf}` ; 403/400/500 | scan `.tex` |
| `/raw` | `path`* | 200 pdf/octet-stream (`no-store`) ; 404/500 nus | lit le fichier |
| `/lint` | `path` (.py sous ~/Documents\|~/Desktop, STUDIO) | 200 `{available[, diagnostics≤200][, error]}` | `ruff` 5 s |
| `/githead` | `path`* | 200 `{ok, text, sha, ts}` \| `{ok:false}` | `git show <base>:<rel>` |
| `/versions` | `path`* | 200 `{ok, v, path, revision, base, texts, interventions, legacySnapshots, current}` \| `{ok:false}` | peut migrer/réécrire le fichier gzip |
| `/gitlog` | `path`* | 200 `{ok, items[{sha,ts,msg}]}` \| `{ok:false}` | `git log --follow -100` |
| `/gitshow` | `path`*, `sha`* (`[0-9a-f]{4,40}`) | 200 `{ok, text}` \| `{ok:false}` | `git show sha:rel` |
| `/commitmsg` | `path`* | 200 `{ok, msg≤100}` \| `{ok:false}` | diff + LLM (20 s) |
| `/code` | `path`* (KeyError→400) | 200 `{text, mtime, path}` ; 404/400/500 | lit utf-8 `errors=replace` |
| `/rasterize` | `path`*, `w` (320-2400, déf. 1000), `h` (200-20000, déf. 750) | 200 png ; 404/501/500/400 | Chrome + cache `rast_*` |
| `/notes/load` | — | 200 `{markdown}` ; 500 | lit `notes.md` |
| `/claude-targets` | — | 200 `{targets[{app,id,title,cwd,inProject,active}]}` ; 500 | NO_PUSH → `[]` sans CLI |
| `/board/load` | — | 200 `{snapshot\|null}` ; 500 | lit board.tldr.json |
| `/board/poll` | — | 200 `{commands}` | draine la file mémoire (cap 500) |
| `/pdfannot` | `rel` | 200 `{annots}` (dégrade en `[]`) | lit pdf_annots.json |
| `/ping` | — | 200 `{ok, service:"fig-annotate", project, claudePreview, agentHost, agentBridgeProtocol:2, agentInbox, watcher{enabled,running,lastScan,lastBuild,lastChanged,error}}` | peut déclencher rebuild différé |
| `/rev` | — | 200 `{rev}` (mtime index) | rebuild arrière-plan si stale |
| `/quote` | — | 200 `{pending}` (<900 s) ; 400/500 | lit fig-last-quote.txt |
| `/state` | — | 200 état \| défaut `{favs,ratings,hidden,tags,hideRules,collections,workflow}` ; 400/500 | lit .fig_state.json |
| `/findscript` † | `stem`* (≤200) | 200 `{script\|null}` ; 403 | `rg -F` 15 s |
| `/provenance` † | `rel`* | 200 `{ok, rel, provenance}` ; 404 `{error:"artifact not found"}` ; 400 ; 403 | lit figures_data.json |
| vidéos `.mp4/.m4v/.mov/.webm` | `Range` | 206/200/416 + `Accept-Ranges` | streaming |
| `*.html` projet | — | 200 avec `<script sel_overlay.js>` injecté (`no-cache`) | lit le fichier |
| statique (fallback) | — | comportement `SimpleHTTPRequestHandler` pinné sous PROJECT | — |

† = garde `_local_only()` · ‡ = bearer token agent · * = requis

### POST (tous `_local_only()` sauf `/pdfannot`)

| Route | Corps (cap) | Réponses | FS / effets |
| --- | --- | --- | --- |
| `/pdfannot` ⚠ sans garde/cap/try | `{rel, annots}` | 200 `{ok}` | écrit pdf_annots.json (+`.bak` si clear) |
| `/agent-consumers/register` ‡ | `{consumer*, destination, label, threadId, automatic, pid}` | 200 `{ok, destination{id,consumer,label,threadId,pid,lastSeen,automatic}}` ; 400 | écrit consumers ; purge >7 j non-thread |
| `/agent-annotations/status` ‡ | `{ids* (1-100), status*∈queued\|received\|processing\|completed\|failed\|cancelled, result, error}` (caps 2000) | 200 `{ok, updated}` ; 400 | écrit history |
| `/agent-preferences` | `{destination*, automatic, label}` | 200 `{ok, destination}` ; 400 unknown | écrit consumers |
| `/agent-annotations/release` | `{ids* (1-100), destination*}` | 200 `{ok, released, ids}` | held→queued + réveil éventuel |
| `/agent-annotations/delete` | `{ids*}` | 200 `{ok, deleted, ids}` | retire les held non réclamés ; history cancelled |
| `/agent-annotations/restore` | `{ids*}` | 200 `{ok, restored, ids}` | re-stage depuis history (restoredFrom) |
| `/agent-batches/release` | `{batchId*}` | 200 `{ok, released, ids}` | déblocage batch + réveil |
| `/agent-batches/cancel` | `{batchId*}` | 200 `{ok, cancelled, ids}` | annulation batch |
| `/orca-fullscreen-exit` | — | 200 `{ok, deprecated, method}` | no-op assumé |
| `/orca-native-fullscreen` | `{rel}` (ext image) | 200/500 `{ok, pid}` ; 400 | spawn viewer détaché |
| `/board/open-surface`, `/notes/open-surface` | `{host}` (≤4096) | 200 `{ok, via}` ; 500 no-push ; 502 | CLI cmux/muxy/orca 10 s |
| `/notes/save` | `{markdown*}` (16 Mo) | 200 `{ok}` ; 413/400/500 | écriture atomique notes.md |
| `/board/save` | `{snapshot*}` (64 Mo) | 200 `{ok}` ; 413/400/500 | écriture atomique board |
| `/board/command` | `{type*, …}` (8 Mo) | 200 `{ok, queued}` ; 429 plein ; 400/500 | file mémoire cap 500 |
| `/clear-quote` | — | 200 `{ok}` | tronque fig-last-quote.txt |
| `/save-svg` | `{rel\|name*, svg*, edits}` (64 Mo) | 200 `{ok, path}` ; 400 (XML/symlink) ; 413/500 | `.orig.bak` une fois + atomique + sidecar |
| `/export-png` | `{rel\|name*, svg*, dpi 72-1200 déf. 300}` (64 Mo) | 200 `{ok, path, dpi}` ; 501 sans rsvg ; 400/413/500 | png sibling atomique |
| `/state` | 7 clés sanitisées (tags≤30/clé, rules≤200, collections≤1000×80c, ratings int 1-5, workflow enum) | 200 `{ok, favs, ratings, hidden}` ; 400/500 | écriture atomique .fig_state.json |
| `/rescan` | — | 200 `{ok, out[-200:]}` (timeout → `{ok:false,out:"rescan timed out"}`) ; 400/500 | build_gallery 300 s killpg |
| `/regenerate` | `{rel*}` | 200 `{ok, returncode, output[-6000:]}` ; 409 `no declared argv command…` ; 408 timeout ; 400 | argv déclaré 900 s + rebuild async |
| `/delete` | `{rels*}` | 200 `{deleted}` ; 400/500 | déplace vers ~/.Trash (suffixe _n) + rebuild async |
| `/export` | `{rels*, mode: folder\|zip\|contact}` | 200 `{ok, path, count}` ; 400 vide ; 500 | écrit _gallery_exports + `open` |
| `/open` | `{rel*}` | 200 `{ok}` ; 404/400/500 | `open <path>` 10 s |
| `/compile` | `{path*}` | 200 `{ok, pdf, root, error}` (dégrade sans latexmk) ; 403/400/500 | latexmk 180 s |
| `/synctex` | `{tex, pdf, dir, line, col, page, x, y}` | 200 clés parsées \| `{error:"no match"}` ; 403/400/500 | synctex 10 s |
| `/versions` | `{path*, expectedRevision*, ops≤500}` (8 Mo) | 200 `{ok, revision}` ; 409 `revision-conflict` ; 400/500 | gzip + `.bak` |
| `/gitcommit` | `{path*, message*}` | 200 `{ok, sha}` \| `{ok:false, error}` ; 403/400/500 | `git add/commit --no-verify -- <rel>` (jamais -A) |
| `/codesave` | `{path*, text*, mtime}` | 200 `{mtime}` ; 409 `conflit` ; 403/400/500 | écrase utf-8 |
| `/selinfo` | dict libre (`lines` truthy = écrire) | 200 `{ok}` ; 400/500 | écrit/supprime fig-selection.json |
| `/zotero-fav` | `{key* [A-Za-z0-9]{8}, on}` | 200 `{key, fav}` ; 400/500 | écrit zotero-favs.json |
| `/zotero-add?name=*.pdf` | corps pdf brut (200 Mo) | 200 `{name, ok[, error\|status\|match]}` | MD5 + connecteur Zotero 23119 |
| `/claude-event`, `/agent-event` | `{rel*, note}` | 200 `{ok, id}` ; 404/400/500 | événement mémoire (cap 100) |
| `/agent-selections/ack` ‡ | `{ids* ≤100, consumer}` | 200 `{ok, acknowledged}` ; 400 | retire de l'inbox ; history acknowledged |
| `/agent-selection` ‡ | `{path\|rel*, source, type, comment, region, anchor, notes, direct, action, destination, batchId, held}` (1 Mo) | 200 `{ok, queuedForAgent, id}` ; 404 `file not found: …` ; 400/403/500 | enqueue inbox (cap 100) + réveil |
| `/quote` | `{rel*, text*, page, comment, direct, embed, target, action, destination, batchId, held, anchor, region}` (1 Mo, text≤100k, comment≤10k) | 200 `{sentToClaude\|embedded, message, clipboard, submitted, agentHost, queuedForAgent, agentSelectionId, agentSelectionStatus}` ; 404/400/500 | pbcopy/quote-file (hors NO_PUSH), enqueue, push CLI |
| `/save` | `{name*, dataURL* png, notes, direct, embed, target, action, destination, batchId, held}` (64 Mo) | 200 `{path, sentToClaude, clipboard, submitted, agentHost, queuedForAgent, agentSelectionId, agentSelectionStatus}` ; 400/500 | écrit `annotations/<name>_annot_<ts>.png`, enqueue, push thread |
| autre chemin | — | 404 `{error:"not found"}` | — |

‡ = bearer token agent · ⚠ = quirk documenté dans la matrice

### Code mort identifié (à ne pas porter)

`_activate_orca`, `_orca_ax_fullscreen`, `_orca_press_escape`,
`_osascript_escape_key`, `_osascript_fullscreen_hotkey`, `_orca_window_state` —
helpers osascript jamais appelés par une route ; `/orca-fullscreen-exit` est un
no-op assumé.
