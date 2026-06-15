#!/usr/bin/env python3
"""project-gallery — a full-featured artifact gallery + annotation, as a cmux plugin.

Generalises an existing figures-index builder and fig-annotate server so they
work in ANY project. `run` builds the gallery, provisions the viewer assets into
the project, starts the server (a free port, cwd = project root) and opens it as
a cmux browser surface. Full functions are preserved: search · sort · folder +
format filters · archive toggle · favourites + star ratings · thumbnails ·
PDF/Markdown/code viewers · image lightbox with annotation → Claude.

Keep the `run` terminal open — it hosts the local server. Ctrl-C stops it.

Subcommands:
    build   GALLERY_ROOT=<root> build_gallery.py  +  drop viewer assets
    run     build + start the server + open the gallery in cmux (foreground)
"""
import argparse
import http.client
import os
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath: resolve the PATH symlink
BUILDER = os.path.join(HERE, "build_gallery.py")
SERVER = os.path.join(HERE, "fig_annotate_server.py")
ASSETS = os.path.join(HERE, "assets")
VIEWERS = ("pdf_viewer.html", "md_viewer.html", "code_editor.html", "latex_studio.html")
OUT = "figures_index.html"


DEFAULT_PORT = 8790  # stable, bookmarkable URL; falls back to a free port if busy


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_busy(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def provision_viewers(root: str) -> None:
    """Copy the bundled lightbox viewers into <root>/.fig_thumbs/ (served by the server)."""
    td = os.path.join(root, ".fig_thumbs")
    os.makedirs(td, exist_ok=True)
    for v in VIEWERS:
        src = os.path.join(ASSETS, v)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(td, v))
    pdfjs_src = os.path.join(ASSETS, "pdfjs")  # the PDF viewer's pdf.js bundle (~4 MB)
    pdfjs_dst = os.path.join(td, "pdfjs")
    if os.path.isdir(pdfjs_src) and not os.path.isdir(pdfjs_dst):
        shutil.copytree(pdfjs_src, pdfjs_dst)


def build(root: str) -> str:
    env = dict(os.environ, GALLERY_ROOT=root)
    subprocess.run([sys.executable, BUILDER], cwd=root, env=env, check=True)
    provision_viewers(root)
    return os.path.join(root, OUT)


def wait_up(port: int, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/ping")
            r = c.getresponse()
            c.close()
            if r.status == 200:
                return True
        except OSError:
            time.sleep(0.2)
    return False


def cmd_build(a) -> None:
    out = build(a.root)
    print(f"[project-gallery] built {out}  (+ viewers provisioned)")


def cmd_run(a) -> None:
    out = build(a.root)
    print(f"[project-gallery] built {out}")
    if a.port == 0:
        port = free_port()
    else:
        port = a.port
        if _port_busy(port):
            print(f"[project-gallery] port {port} busy → using a free port", file=sys.stderr)
            port = free_port()
    env = dict(os.environ, FIG_PORT=str(port), GALLERY_ROOT=a.root)
    print(f"[project-gallery] starting server on :{port}  (cwd={a.root})")
    srv = subprocess.Popen([sys.executable, SERVER], cwd=a.root, env=env)
    try:
        if not wait_up(port):
            print("[project-gallery] warning: server /ping did not answer", file=sys.stderr)
        url = f"http://127.0.0.1:{port}/{OUT}"
        res = subprocess.run(["cmux", "browser", "open", url], capture_output=True, text=True)
        print(res.stdout.strip() or res.stderr.strip())
        print(f"[project-gallery] gallery → {url}   (Ctrl-C to stop)")
        srv.wait()
    except KeyboardInterrupt:
        print("\n[project-gallery] stopping server")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="project-gallery", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the gallery HTML + provision viewers")
    b.add_argument("--root", default=os.getcwd(), type=os.path.abspath)
    r = sub.add_parser("run", help="build + start server + open in cmux (foreground)")
    r.add_argument("--root", default=os.getcwd(), type=os.path.abspath)
    r.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"server port (default {DEFAULT_PORT}; 0 = random free port)")
    a = p.parse_args(argv)
    {"build": cmd_build, "run": cmd_run}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
