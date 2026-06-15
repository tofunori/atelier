# project-gallery

A full-featured **artifact gallery + annotation** for any project, packaged as a
[cmux](https://github.com/manaflow-ai/cmux) plugin. Point it at a project and it
builds a rich HTML gallery of your figures, PDFs, data and code — searchable,
filterable, with thumbnails, an image lightbox, **PDF / Markdown / code
viewers**, and **annotate-a-figure → send to Claude**.

It generalises an existing figures-index builder + a small local server so they
work in **any** project root instead of one hard-coded repo.

## What you get

Search · sort · folder + format filters · archive toggle · favourites + star
ratings · Quick-Look thumbnails · image lightbox · embedded PDF / Markdown / code
viewers · figure annotation (pen / arrow / rect + notes) that posts the
annotated PNG to your Claude session.

## How it works

`run` does four things, then stays in the foreground hosting the server:

1. **build** — `GALLERY_ROOT=<root> build_gallery.py` writes `figures_index.html`
   at the project root (Quick-Look thumbnails for PDF/Office on macOS).
2. **provision** — copies the bundled viewers into `<root>/.fig_thumbs/`.
3. **serve** — starts `fig_annotate_server.py` on a free port with the project
   as its root (serves the gallery + viewers; handles `/open`, `/state`,
   `/save`, `/rescan`, `/delete`).
4. **open** — `cmux browser open http://127.0.0.1:<port>/figures_index.html`.

The server needs the cmux socket (to push annotations to Claude), so it must run
inside a cmux terminal surface — which is exactly where the plugin command runs
it. Keep the terminal open; Ctrl-C stops the server.

## Install

```bash
ln -s "$PWD/project_gallery.py" ~/.local/bin/project-gallery
chmod +x project_gallery.py
```

Needs Python 3 (stdlib only) and the `cmux` CLI. Thumbnails use macOS `qlmanage`
(skipped gracefully elsewhere).

## Use

```bash
project-gallery run --root .            # build + serve + open in cmux (keep terminal open)
project-gallery run --root . --port 8790
project-gallery build --root .          # just write the HTML + viewers (no server)
```

As a cmux plugin: copy `actions` + `commands` from
[`cmux.example.json`](./cmux.example.json) into your project's `.cmux/cmux.json`
(or global `~/.config/cmux/cmux.json`), then run **Project Gallery** from
`Cmd+Shift+P`.

### Run the server in the Dock (no dedicated pane)

The server must stay running while you use the gallery. Instead of a foreground
pane, put it in the cmux Dock: copy [`dock.example.json`](./dock.example.json)
into the project's `.cmux/dock.json` (merge into `controls` if one exists). It
must be **project-local** — a global Dock control resolves `cwd: "."` to your
home directory, not the active project. The control is a cmux terminal, so
annotate→Claude still works. Toggle it from the Dock; closing it stops the
server.

## Files

| file | role |
|------|------|
| `project_gallery.py` | launcher (build + provision + serve + open) |
| `build_gallery.py` | gallery HTML builder (generalised `ROOT` → `GALLERY_ROOT`/cwd) |
| `fig_annotate_server.py` | local server (generalised `PROJECT` → `GALLERY_ROOT`/cwd) |
| `assets/` | bundled PDF/Markdown/code/LaTeX viewers, provisioned per project |
| `project_gallery_noserver.py` | alternative serverless build (cmux-bridge open, no viewers/annotation) |

## Caveats

- **Untrusted filenames**: hardened — filenames are HTML-escaped (`esc`/`escA`)
  and all card handlers go through `data-*` delegation, so a crafted filename
  (`<img onerror=…>`, quote-breakout) renders inert and cannot execute. Verified
  against adversarial filenames in cmux.
- **annotate → Claude** writes to your Claude/cmux session via the server; that
  half is specific to a Claude-Code-in-cmux setup. Everything else is generic.
- `figures_index.html`, `.fig_thumbs/`, `annotations/` are regenerated per build
  — gitignored by default.
