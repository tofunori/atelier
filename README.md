# cmux-gallery

A portable artifact gallery + annotation tool for [cmux](https://github.com/manaflow-ai/cmux).
Point it at any project and it builds a searchable HTML gallery of your figures,
PDFs, data and code — with thumbnails, an image lightbox, PDF / Markdown / code
viewers, and figure annotation. Clicking a card opens the source file in a cmux
pane. No manual setup per project.

It generalises a figures-index builder + a small local server so they work in
**any** project root.

## Features

Search · sort · folder + format filters · archive toggle · favourites + star
ratings · Quick-Look thumbnails (macOS) · image lightbox · embedded PDF /
Markdown / code viewers · figure annotation (pen / arrow / rect + notes).

## How it works

`run` builds the gallery, provisions the viewer assets into the project, starts a
local server (on a stable port, with the project as its root) and opens it as a
cmux browser surface. Keep the launching terminal/pane open — it hosts the
server; Ctrl-C stops it.

```
build  → GALLERY_ROOT=<root> build_gallery.py  +  copy viewer assets
serve  → fig_annotate_server.py on a free port, project as root
open   → cmux browser open http://127.0.0.1:<port>/figures_index.html
```

## Install

```bash
git clone https://github.com/tofunori/cmux-gallery.git ~/tools/cmux-gallery
ln -s ~/tools/cmux-gallery/cmux_gallery.py ~/.local/bin/cmux-gallery
chmod +x ~/tools/cmux-gallery/cmux_gallery.py
```

`build` needs only the Python 3 standard library. `run` needs the `cmux` CLI.
Thumbnails use macOS `qlmanage` (skipped gracefully elsewhere).

## Use

```bash
cmux-gallery run                 # build + serve + open in cmux (keep the pane open)
cmux-gallery run --port 8790     # pin the port (default 8790 → stable, bookmarkable URL)
cmux-gallery run --root /path    # a specific project (default: current dir)
cmux-gallery build               # just write the HTML + viewers (no server)
```

The gallery is then a normal web page at `http://127.0.0.1:8790/figures_index.html`
— open it in any browser (cmux or system) and bookmark it.

### As a cmux command / Dock control

- **Command Palette / + menu**: copy the `actions` + `commands` from
  [`cmux.example.json`](./cmux.example.json) into `~/.config/cmux/cmux.json`,
  then run **Project Gallery**.
- **Dock** (server runs in the sidebar, can't be closed by accident): copy
  [`dock.example.json`](./dock.example.json) into the project's `.cmux/dock.json`.

## Configuration

| flag / env | meaning |
|---|---|
| `--root <dir>` | project to scan (default: current dir) |
| `--port <n>` | server port (default 8790; 0 = random free port) |
| `GALLERY_TITLE` | header wordmark (default `Gallery`) |
| `GALLERY_NO_THUMBS=1` | skip Quick-Look thumbnail generation |

## Notes & caveats

- **Untrusted filenames are safe**: filenames are HTML-escaped and all card
  handlers use `data-*` delegation, so a crafted filename can't execute script.
- **annotate → Claude** and the LaTeX/`open`/trash actions are macOS- and
  Claude-Code-in-cmux-specific; they degrade gracefully elsewhere.
- `figures_index.html`, `.fig_thumbs/`, `annotations/` are regenerated per build
  (gitignored).

## Bundled third-party

`assets/pdfjs/` is Mozilla [pdf.js](https://github.com/mozilla/pdf.js)
(Apache-2.0) — see `assets/pdfjs/NOTICE`.

## License

MIT — see [LICENSE](./LICENSE).
