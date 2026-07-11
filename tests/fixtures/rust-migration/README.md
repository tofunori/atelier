# Fixtures — migration Rust

Fichiers minimaux copiés dans un projet temporaire par `tests/contract/support.py`
pour exercer les deux backends (Python et Rust) avec un contenu identique :

| Fichier | Usage |
| --- | --- |
| `tiny.png` | image 1×1 — `/thumb`, `/data`, `/agent-event`, annotations |
| `plot.svg` | SVG — `/thumb`, `/save-svg`, `/export-png` |
| `report.md` | markdown — `/code`, `/snippet`, `/rasterize` |
| `doc.tex` | LaTeX — `/texroot`, `/compile` (phase 4) |
| `mini.pdf` | PDF une page — `/raw`, `/pdfannot` |
| `script.py` | code — `/code`, `/githead`, `/findscript`, `/codesave` |
| `report.html` | HTML — injection overlay + `/rasterize` |

Le dépôt Git du projet fixture est initialisé à l'exécution (`git init` + commit)
— pas de dépôt imbriqué commité ici. La fixture Zotero de la phase 0 est
l'absence de bibliothèque (HOME temporaire) : le contrat testé est la
dégradation contrôlée. Une base Zotero synthétique arrivera en phase 6.
