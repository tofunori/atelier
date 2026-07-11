#!/usr/bin/env python3
"""atelier — artifact gallery, whiteboard, notes and annotations for cmux/Muxy/Orca.

Generalises an existing figures-index builder and fig-annotate server so they
work in ANY project. `run` builds the gallery, provisions the viewer assets into
the project, starts the server (a free port, cwd = project root) and opens it as
a cmux browser surface. Full functions are preserved: search · sort · folder +
format filters · archive toggle · favourites + star ratings · thumbnails ·
PDF/Markdown/code viewers · image lightbox with annotation → Claude.

Use `run` or `open` when you want a detached per-project server and your prompt
back. Use `foreground` when you want the server attached to the terminal.

Subcommands:
    build   GALLERY_ROOT=<root> build_gallery.py  +  drop viewer assets
    run     build + start/reuse a detached server + open the gallery
    open    build + start/reuse a detached server + open the gallery
"""
import argparse
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

from atelier_runtime import atomic_write_json

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath: resolve the PATH symlink
BUILDER = os.path.join(HERE, "build_gallery.py")
SERVER = os.path.join(HERE, "fig_annotate_server.py")
ASSETS = os.path.join(HERE, "assets")
VIEWERS = ("pdf_viewer.html", "md_viewer.html", "code_editor.html", "latex_studio.html")
OUT = "figures_index.html"
RUST_MANIFEST = os.path.join(HERE, "rust", "Cargo.toml")


PORT_BASE = 8790  # each project gets a stable port derived from its path


def state_path(root: str, port: int) -> str:
    """Return the per-project background server metadata path."""
    return os.path.join(root, ".fig_thumbs", f"cmux-gallery-{port}.json")


def project_port(root: str) -> int:
    """A stable, per-project port (same project → same URL, bookmarkable;
    different projects coexist on different ports)."""
    h = int(hashlib.md5(os.path.realpath(root).encode()).hexdigest(), 16)
    return PORT_BASE + (h % 1000)  # 8790–9789


def git_project_root(start: str) -> str | None:
    """Return the enclosing git worktree root for ``start``, if there is one."""
    try:
        res = subprocess.run(
            ["git", "-C", start, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    root = res.stdout.strip()
    if res.returncode == 0 and root:
        return os.path.abspath(root)
    return None


def default_project_root(start: str | None = None) -> str:
    """Pick the project root for commands launched from inside a project."""
    cwd = os.path.abspath(os.path.expanduser(start or os.getcwd()))
    return git_project_root(cwd) or cwd


def root_arg(value: str) -> str:
    """Normalize an explicit ``--root`` argument."""
    return os.path.abspath(os.path.expanduser(value))


def gallery_url(port: int) -> str:
    """Return the browser URL, selecting the safest fullscreen mode for the host.

    Orca's embedded WebKit accepts requestFullscreen() but ignores
    exitFullscreen(), so native fullscreen leaves the pane stuck full-screen on
    exit. Inside Orca we use native entry plus a local-server exit route that
    asks the Orca desktop bridge to leave fullscreen. System browsers keep plain
    native fullscreen, so opening this URL in Safari/Chrome gives true
    whole-screen with a clean exit.
    """
    if os.environ.get("ORCA_APP_VERSION") or os.environ.get("TERM_PROGRAM") == "Orca":
        qs = "?orcaFs=1"
    else:
        qs = "?nativeFs=1"
    return f"http://127.0.0.1:{port}/{OUT}{qs}"


def open_cmux_browser(url: str) -> bool:
    """Open ``url`` in cmux when the CLI is available."""
    if not shutil.which("cmux"):
        print("[atelier] cmux CLI not found on PATH; open this URL manually "
              "or run with --no-open", file=sys.stderr)
        return False
    res = subprocess.run(["cmux", "browser", "open", url], capture_output=True, text=True)
    msg = res.stdout.strip() or res.stderr.strip()
    if msg:
        print(msg)
    return res.returncode == 0


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


def server_project(port: int):
    """If one of our gallery servers answers on `port`, return the project root
    it serves (realpath); otherwise None. Lets `run` reuse an already-running
    server for the same project instead of spawning a duplicate on a new port."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        c.request("GET", "/ping")
        r = c.getresponse()
        body = r.read()
        c.close()
        if r.status != 200:
            return None
        d = json.loads(body or b"{}")
        if d.get("service") in ("fig-annotate", "atelier") and d.get("project"):
            return os.path.realpath(d["project"])
    except (OSError, ValueError):
        pass
    return None


def _log_python_fallback(reason: str) -> None:
    """Journalise chaque bascule vers Python (phase 9 : visible et temporaire)."""
    print(f"[atelier] backend fallback → python ({reason})", file=sys.stderr)


def backend_name() -> str:
    """Backend sélectionné. Défaut = rust (phase 9) ; ``ATELIER_BACKEND=python`` force Python."""
    raw = os.environ.get("ATELIER_BACKEND", "rust").strip().lower()
    if raw in ("python", "py"):
        return "python"
    return "rust"


def server_backend(port: int):
    try:
        payload = fetch_health(port) or {}
        if payload.get("backend") in ("python", "rust"):
            return payload["backend"]
        return "python" if payload.get("service") == "fig-annotate" else None
    except (OSError, ValueError):
        return None


def provision_viewers(root: str) -> None:
    """Copy every bundled viewer asset (the *.html viewers + cm/, pdfjs/,
    marked.min.js …) into <root>/.fig_thumbs/, where the server serves them.
    Both files and vendor dirs are refreshed each build so a tool upgrade ships
    new CodeMirror/pdf.js to existing projects (was: dirs copied once = stale)."""
    td = os.path.join(root, ".fig_thumbs")
    os.makedirs(td, exist_ok=True)
    for name in os.listdir(ASSETS):
        src, dst = os.path.join(ASSETS, name), os.path.join(td, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def build(root: str) -> str:
    frontend_package = os.path.join(HERE, "package.json")
    if (os.environ.get("ATELIER_BUILD_TYPESCRIPT") == "1"
            and os.path.isfile(frontend_package)
            and os.path.isdir(os.path.join(HERE, "frontend"))):
        subprocess.run(["npm", "run", "build:frontend"], cwd=HERE, check=True)
    env = dict(os.environ, GALLERY_ROOT=root)
    subprocess.run([sys.executable, BUILDER], cwd=root, env=env, check=True)
    provision_viewers(root)
    return os.path.join(root, OUT)


def rust_server_binary(build: bool = True) -> str | None:
    """Locate atelier-server: PATH, then release/debug build, then optional cargo build."""
    env_bin = os.environ.get("ATELIER_RUST_SERVER", "").strip()
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    which = shutil.which("atelier-server")
    if which:
        return which
    candidates = [
        os.path.join(HERE, "dist", "bin", "atelier-server"),
        os.path.join(HERE, "rust", "target", "release", "atelier-server"),
        os.path.join(HERE, "rust", "target", "debug", "atelier-server"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    if not build or not os.path.isfile(RUST_MANIFEST):
        return None
    if shutil.which("cargo") is None:
        return None
    subprocess.run(
        ["cargo", "build", "--release", "--manifest-path", RUST_MANIFEST,
         "-p", "atelier-server", "-p", "atelier-cli"],
        cwd=HERE, check=True,
    )
    release = candidates[1]
    return release if os.path.isfile(release) else None


def backend_command(root: str, port: int):
    """Return the selected server command and environment.

    Rust is the default (phase 9). ``ATELIER_BACKEND=python`` forces the
    legacy server; if the Rust binary is missing, we fall back to Python
    and log the reason so the fallback stays observable.
    """
    env = dict(os.environ, FIG_PORT=str(port), GALLERY_ROOT=root,
               ATELIER_TOOL_ROOT=HERE, ATELIER_BUILDER=BUILDER)
    wanted = backend_name()
    if wanted == "python":
        _log_python_fallback("ATELIER_BACKEND=python")
        return [sys.executable, SERVER], env
    binary = rust_server_binary(build=True)
    if not binary:
        _log_python_fallback("atelier-server binary unavailable")
        return [sys.executable, SERVER], env
    command = [binary, "--root", root, "--port", str(port)]
    command.append("--no-watch" if os.environ.get("GALLERY_WATCH", "1") == "0" else "--watch")
    return command, env


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


def command_backend(command: list[str]) -> str:
    return "python" if command and os.path.realpath(command[0]) == os.path.realpath(sys.executable) else "rust"


def write_server_state(root: str, port: int, pid: int, log_path: str,
                       effective_backend: str | None = None) -> None:
    os.makedirs(os.path.join(root, ".fig_thumbs"), exist_ok=True)
    data = {
        "service": "atelier",
        "backend": effective_backend or backend_name(),
        "project": os.path.realpath(root),
        "port": port,
        "pid": pid,
        "log": log_path,
        "started": int(time.time()),
    }
    path = state_path(root, port)
    atomic_write_json(path, data)


def read_server_state(root: str, port: int) -> dict | None:
    try:
        with open(state_path(root, port), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def fetch_health(port: int) -> dict | None:
    """Return the live gallery health payload, or None when unreachable."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/ping")
        r = c.getresponse()
        payload = json.loads(r.read() or b"{}")
        c.close()
        return payload if r.status == 200 and isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def project_diagnostics(root: str, port: int) -> dict:
    root = os.path.realpath(root)
    state = read_server_state(root, port) or {}
    pid = int(state.get("pid") or 0)
    health = fetch_health(port)
    index = os.path.join(root, OUT)
    data_file = os.path.join(root, "figures_data.json")
    assets_copy = os.path.join(root, ".fig_thumbs", "gallery_template.html")
    source_asset = os.path.join(ASSETS, "gallery_template.html")
    def mtime(path):
        try:
            return int(os.path.getmtime(path))
        except OSError:
            return 0
    return {
        "project": root,
        "port": port,
        "url": gallery_url(port),
        "running": bool(health),
        "pid": pid or None,
        "pidAlive": bool(pid and process_alive(pid)),
        "health": health,
        "indexExists": os.path.isfile(index),
        "dataExists": os.path.isfile(data_file),
        "assetsCurrent": bool(mtime(assets_copy) >= mtime(source_asset)),
        "indexUpdated": mtime(index) or None,
        "log": state.get("log"),
    }


def cmd_status(a) -> None:
    port = a.port or project_port(a.root)
    d = project_diagnostics(a.root, port)
    h = d.get("health") or {}
    print(f"Project       {d['project']}")
    print(f"Server        {'running' if d['running'] else 'stopped'}" +
          (f" · PID {d['pid']}" if d.get("pid") else ""))
    print(f"URL           {d['url']}")
    print(f"Codex         {'connected' if h.get('agentHost') == 'codex' else 'not connected'}")
    print(f"Annotations   {int(h.get('agentInbox') or 0)} pending")
    print(f"Index         {'ready' if d['indexExists'] and d['dataExists'] else 'missing'}")
    watcher = h.get("watcher") if isinstance(h.get("watcher"), dict) else {}
    print(f"Watcher       {'active' if watcher.get('running') else ('enabled' if watcher.get('enabled') else 'off')}")
    print(f"Assets        {'current' if d['assetsCurrent'] else 'stale'}")


def cmd_doctor(a) -> None:
    port = a.port or project_port(a.root)
    d = project_diagnostics(a.root, port)
    if a.repair:
        state = read_server_state(a.root, port)
        if state and not d["pidAlive"]:
            try:
                os.remove(state_path(a.root, port))
            except OSError:
                pass
        if not d["indexExists"] or not d["dataExists"] or not d["assetsCurrent"]:
            build(a.root)
        if not d["running"] and not _port_busy(port):
            start_detached_server(a.root, port)
            wait_up(port)
        d = project_diagnostics(a.root, port)
    checks = [
        ("project", os.path.isdir(d["project"]), d["project"]),
        ("server", d["running"], f"127.0.0.1:{port}"),
        ("metadata", not d["pid"] or d["pidAlive"], "PID state"),
        ("index", d["indexExists"] and d["dataExists"], "HTML + JSON"),
        ("assets", d["assetsCurrent"], "bundled viewers"),
        ("write", os.access(d["project"], os.W_OK), "project writable"),
    ]
    failed = 0
    for name, ok, detail in checks:
        failed += not ok
        print(f"{'OK' if ok else 'FAIL':4}  {name:10} {detail}")
    if failed:
        hint = "" if a.repair else "; retry with `atelier doctor --repair`"
        raise SystemExit(f"[atelier] {failed} diagnostic check(s) failed{hint}")


def start_detached_server(root: str, port: int, command_env=None) -> tuple[int, str]:
    command, env = command_env or backend_command(root, port)
    os.makedirs(os.path.join(root, ".fig_thumbs"), exist_ok=True)
    log_path = os.path.join(root, ".fig_thumbs", f"cmux-gallery-{port}.log")
    log = open(log_path, "a", encoding="utf-8")
    try:
        srv = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()
    write_server_state(root, port, srv.pid, log_path, command_backend(command))
    return srv.pid, log_path


def resolve_port_for_host(root: str, requested_port: int) -> int:
    port = requested_port or project_port(root)
    command, _ = backend_command(root, port)
    effective = command_backend(command)
    if not _port_busy(port):
        return port
    served_project = server_project(port)
    if served_project == os.path.realpath(root) and server_backend(port) == effective:
        return port
    if requested_port:
        raise SystemExit(f"[atelier] port {port} is busy and is not serving {root}")
    print(f"[atelier] port {port} busy (not our gallery) → using a free port", file=sys.stderr)
    return next((c for c in range(port + 1, port + 50) if not _port_busy(c)), 0) or free_port()


def cmd_build(a) -> None:
    out = build(a.root)
    print(f"[atelier] built {out}  (+ viewers provisioned)")


def cmd_foreground(a) -> None:
    out = build(a.root)
    print(f"[atelier] built {out}")
    port = a.port or project_port(a.root)
    command, env = backend_command(a.root, port)
    effective = command_backend(command)
    # The build above already refreshed figures_index.html + viewers. If our own
    # gallery for THIS project is already running on its stable port, reuse it
    # (the live server serves the fresh file) instead of starting a duplicate on
    # a random port — that's what was leaking a new port on every run.
    if not a.port and _port_busy(port):
        if (server_project(port) == os.path.realpath(a.root)
                and server_backend(port) == effective):
            url = gallery_url(port)
            print(f"[atelier] gallery already running on :{port} → reusing it "
                  f"(rebuilt; stable URL, no duplicate server)")
            if a.open:
                open_cmux_browser(url)
            print(f"[atelier] gallery → {url}")
            return
        print(f"[atelier] port {port} busy (not our gallery) → using a free port", file=sys.stderr)
        port = next((c for c in range(port + 1, port + 50) if not _port_busy(c)), 0) or free_port()
        command, env = backend_command(a.root, port)
    print(f"[atelier] starting server on :{port}  (cwd={a.root})")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # SIGTERM -> SystemExit -> finally tears down the server (no orphan)
    srv = subprocess.Popen(command, cwd=a.root, env=env)
    try:
        if not wait_up(port):
            print("[atelier] warning: server /ping did not answer", file=sys.stderr)
        url = gallery_url(port)
        if a.open:
            open_cmux_browser(url)
        print(f"[atelier] gallery → {url}   (Ctrl-C to stop)")
        srv.wait()
    except KeyboardInterrupt:
        print("\n[atelier] stopping server")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


def cmd_open(a) -> None:
    out = build(a.root)
    print(f"[atelier] built {out}")
    port = resolve_port_for_host(a.root, a.port)
    command_env = backend_command(a.root, port)
    effective = command_backend(command_env[0])
    url = gallery_url(port)
    served_project = server_project(port) if _port_busy(port) else None
    if (served_project == os.path.realpath(a.root)
            and server_backend(port) == effective):
        print(f"[atelier] gallery already running on :{port} → reusing it")
    else:
        pid, log_path = start_detached_server(a.root, port, command_env)
        if wait_up(port):
            print(f"[atelier] started detached server pid {pid} on :{port}")
        else:
            print(f"[atelier] warning: server /ping did not answer; log: {log_path}",
                  file=sys.stderr)
    if a.open:
        open_cmux_browser(url)
    print(f"[atelier] gallery → {url}")


def cmd_run(a) -> None:
    cmd_open(a)


def cmd_stop(a) -> None:
    port = a.port or project_port(a.root)
    state = read_server_state(a.root, port)
    if not state:
        print(f"[atelier] no background server metadata for :{port}")
        return
    pid = int(state.get("pid") or 0)
    if pid and process_alive(pid):
        os.kill(pid, signal.SIGTERM)
        print(f"[atelier] stopped background server pid {pid} on :{port}")
    else:
        print(f"[atelier] background server pid {pid or '?'} is not running")
    try:
        os.remove(state_path(a.root, port))
    except OSError:
        pass


def cmd_serve(a) -> None:
    """Build, then HOST the server in the foreground and keep it alive (self-healing).

    Unlike `run`, this never reuses-and-exits — it IS the host. No browser tab is
    opened. Ideal for a cmux Dock control or a long-lived pane: the server lives as
    long as this process, and restarts itself if it ever dies."""
    out = build(a.root)
    print(f"[atelier] built {out}")
    port = a.port or project_port(a.root)
    command, env = backend_command(a.root, port)
    print(f"[atelier] serving {gallery_url(port)}  "
          f"(cwd={a.root}; hosting; self-healing; Ctrl-C to stop)")
    srv = None
    def _stop(*_):
        if srv:
            srv.terminate()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while True:
            srv = subprocess.Popen(command, cwd=a.root, env=env)
            srv.wait()
            print("[atelier] server exited — restarting in 2s", file=sys.stderr)
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        if srv:
            srv.terminate()
            try:
                srv.wait(timeout=5)
            except subprocess.TimeoutExpired:
                srv.kill()


def cmd_codex_serve(a) -> None:
    """Internal foreground host used by the bundled Codex MCP plugin."""
    token = os.environ.get("ATELIER_AGENT_TOKEN") or ""
    if not token:
        raise SystemExit("[atelier] ATELIER_AGENT_TOKEN is required for codex-serve")
    env = dict(os.environ, GALLERY_ROOT=a.root, FIG_PORT=str(a.port or project_port(a.root)),
               ATELIER_AGENT_HOST="codex")
    os.chdir(a.root)
    command, command_env = backend_command(a.root, int(a.port or project_port(a.root)))
    command_env.update(env)
    os.execve(command[0], command, command_env)


def cmd_rust_serve(a) -> None:
    """Internal Rust foreground host used by the Codex MCP bridge."""
    os.environ["ATELIER_BACKEND"] = "rust"
    env = dict(os.environ, ATELIER_BACKEND="rust", GALLERY_ROOT=a.root,
               FIG_PORT=str(a.port or project_port(a.root)))
    command, _ = backend_command(a.root, int(a.port or project_port(a.root)))
    os.chdir(a.root)
    os.execve(command[0], command, env)


def cmd_claude_init(a) -> None:
    """Écrit l'entrée figures-gallery dans <root>/.claude/launch.json (Claude Code
    desktop, menu Serveurs). Fusionne sans toucher aux autres entrées ; idempotent.
    Le serveur never-stale construit l'index tout seul au premier démarrage —
    aucune autre préparation n'est nécessaire dans un nouveau projet."""
    server = os.path.join(HERE, "fig_annotate_server.py")
    entry = {
        "name": "figures-gallery",
        "runtimeExecutable": "sh",
        "runtimeArgs": [
            "-c",
            'CLAUDE_PREVIEW=1 FIG_PORT="${PORT:-%d}" GALLERY_ROOT="$PWD" %s %s'
            % (a.port or project_port(a.root), sys.executable, server),
        ],
        "port": a.port or project_port(a.root),
        "autoPort": True,
    }
    d = os.path.join(a.root, ".claude")
    os.makedirs(d, exist_ok=True)
    lp = os.path.join(d, "launch.json")
    cfg = {"version": "0.0.1", "configurations": []}
    if os.path.exists(lp):
        try:
            with open(lp) as f:
                cfg = json.load(f)
        except Exception:
            print(f"⚠ {lp} illisible — je le remplace", file=sys.stderr)
    cfg.setdefault("configurations", [])
    cfg["configurations"] = [c for c in cfg["configurations"] if c.get("name") != "figures-gallery"]
    cfg["configurations"].append(entry)
    with open(lp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"✓ figures-gallery ajouté à {lp} (port {entry['port']}, autoPort)")
    print("  Dans Claude Code desktop : menu Serveurs → figures-gallery → Exécuter.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="atelier", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the gallery HTML + provision viewers")
    b.add_argument("--root", default=None, type=root_arg,
                   help="project to scan (default: git root for cwd, else cwd)")
    r = sub.add_parser("run", help="build + start/reuse a detached server + open in cmux")
    r.add_argument("--root", default=None, type=root_arg,
                   help="project to scan (default: git root for cwd, else cwd)")
    r.add_argument("--port", type=int, default=0,
                   help="server port (default: a stable port derived from the project path)")
    r.add_argument("--no-open", dest="open", action="store_false",
                   help="start (or reuse) the detached server without opening a cmux browser tab")
    o = sub.add_parser("open", help="build + start/reuse a detached server + open in cmux")
    o.add_argument("--root", default=None, type=root_arg,
                   help="project to scan (default: git root for cwd, else cwd)")
    o.add_argument("--port", type=int, default=0,
                   help="server port (default: a stable port derived from the project path)")
    o.add_argument("--no-open", dest="open", action="store_false",
                   help="start (or reuse) the detached server without opening a cmux browser tab")
    st = sub.add_parser("stop", help="stop a detached server started by atelier run/open")
    st.add_argument("--root", default=None, type=root_arg,
                    help="project to stop (default: git root for cwd, else cwd)")
    st.add_argument("--port", type=int, default=0,
                    help="server port (default: the stable port derived from the project path)")
    fg = sub.add_parser("foreground", help="build + host the server in this terminal")
    fg.add_argument("--root", default=None, type=root_arg,
                    help="project to scan (default: git root for cwd, else cwd)")
    fg.add_argument("--port", type=int, default=0,
                    help="server port (default: a stable port derived from the project path)")
    fg.add_argument("--no-open", dest="open", action="store_false",
                    help="start (or reuse) the server without opening a cmux browser tab")
    ci = sub.add_parser("claude-init", help="add the figures-gallery entry to <root>/.claude/launch.json (Claude Code desktop)")
    ci.add_argument("--root", default=None, type=root_arg,
                    help="project to set up (default: git root for cwd, else cwd)")
    ci.add_argument("--port", type=int, default=0,
                    help="preferred port (default: a stable port derived from the project path)")
    s = sub.add_parser("serve", help="build + HOST the server, self-healing, no browser (for a Dock control)")
    s.add_argument("--root", default=None, type=root_arg,
                   help="project to scan (default: git root for cwd, else cwd)")
    s.add_argument("--port", type=int, default=0,
                   help="server port (default: a stable port derived from the project path)")
    cs = sub.add_parser("codex-serve", help=argparse.SUPPRESS)
    cs.add_argument("--root", required=True, type=root_arg)
    cs.add_argument("--port", required=True, type=int)
    rs = sub.add_parser("rust-serve", help=argparse.SUPPRESS)
    rs.add_argument("--root", required=True, type=root_arg)
    rs.add_argument("--port", required=True, type=int)
    status = sub.add_parser("status", help="show the active project, server and Codex state")
    status.add_argument("--root", default=None, type=root_arg)
    status.add_argument("--port", type=int, default=0)
    doctor = sub.add_parser("doctor", help="diagnose the local gallery runtime")
    doctor.add_argument("--root", default=None, type=root_arg)
    doctor.add_argument("--port", type=int, default=0)
    doctor.add_argument("--repair", action="store_true",
                        help="remove stale metadata and rebuild missing/stale assets")
    a = p.parse_args(argv)
    if a.root is None:
        a.root = default_project_root()
    {
        "build": cmd_build,
        "run": cmd_run,
        "open": cmd_open,
        "stop": cmd_stop,
        "foreground": cmd_foreground,
        "serve": cmd_serve,
        "codex-serve": cmd_codex_serve,
        "rust-serve": cmd_rust_serve,
        "claude-init": cmd_claude_init,
        "status": cmd_status,
        "doctor": cmd_doctor,
    }[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
