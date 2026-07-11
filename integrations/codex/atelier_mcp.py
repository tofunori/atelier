#!/usr/bin/env python3
"""Minimal stdio MCP bridge between Codex and the local Atelier gallery."""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path


ATELIER = shutil.which("atelier")
REPO_ROOT = Path(__file__).resolve().parents[2]
PORT_BASE = 10790
BRIDGE_PROTOCOL = 2
STATE_DIR = Path.home() / "Library" / "Application Support" / "Atelier" / "codex-servers"
_OWNED: dict[str, tuple[subprocess.Popen, object, int]] = {}
_PORTS: dict[str, int] = {}
_TOKENS: dict[str, str] = {}
_CONSUMER_ID = "codex-" + secrets.token_urlsafe(12)
_THREAD_ID = (os.environ.get("CODEX_THREAD_ID") or "").strip()
_DESTINATION_ID = ("thread:" + _THREAD_ID) if _THREAD_ID else _CONSUMER_ID
_DESTINATION_LABEL = (os.environ.get("CODEX_THREAD_TITLE") or
                      ("Codex task " + (_THREAD_ID[:8] if _THREAD_ID else _CONSUMER_ID[-8:])))


def _root(value=None):
    if value is not None and not isinstance(value, str):
        raise ValueError("root must be a string")
    raw = os.path.abspath(os.path.expanduser(value or os.getcwd()))
    try:
        result = subprocess.run(
            ["git", "-C", raw, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = result.stdout.strip()
    except Exception:
        pass
    raw = os.path.realpath(raw)
    if not os.path.isdir(raw):
        raise ValueError(f"project root is not a directory: {raw}")
    return raw


def _stable_port(root):
    digest = int(hashlib.md5(os.path.realpath(root).encode()).hexdigest(), 16)
    return PORT_BASE + digest % 1000


def _tool_root(arguments):
    value = arguments.get("root")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("root is required")
    return _root(value)


def _json_request(port, path, payload=None, timeout=5, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _ping(port):
    try:
        return _json_request(port, "/ping", timeout=1)
    except Exception:
        return None


def _selections_path():
    return ("/agent-selections?consumer=" + urllib.parse.quote(_CONSUMER_ID, safe="") +
            "&destination=" + urllib.parse.quote(_DESTINATION_ID, safe=""))


def _register_destination(root, port, label=None, automatic=None):
    payload = {
        "consumer": _CONSUMER_ID,
        "destination": _DESTINATION_ID,
        "label": (label or _DESTINATION_LABEL)[:160],
        "threadId": _THREAD_ID or None,
        "pid": os.getpid(),
    }
    if automatic is not None:
        payload["automatic"] = bool(automatic)
    result = _json_request(port, "/agent-consumers/register", payload,
                           token=_TOKENS[root])
    return result.get("destination") or {}


def _free_port_near(preferred):
    for port in range(preferred, preferred + 50):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _state_path(root, port):
    key = hashlib.sha256(root.encode()).hexdigest()[:20]
    return STATE_DIR / f"{key}-{port}.json"


def _server_command(root, port):
    """Rust by default (phase 9); ``ATELIER_BACKEND=python`` keeps the legacy path."""
    if not _rust_backend_requested():
        if not ATELIER:
            raise RuntimeError("atelier executable not found on PATH")
        return [ATELIER, "codex-serve", "--root", root, "--port", str(port)]
    candidates = [
        os.environ.get("ATELIER_RUST_SERVER", ""),
        shutil.which("atelier-server") or "",
        str(REPO_ROOT / "dist/bin/atelier-server"),
        str(REPO_ROOT / "rust/target/release/atelier-server"),
        str(REPO_ROOT / "rust/target/debug/atelier-server"),
    ]
    binary = next((candidate for candidate in candidates
                   if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK)), None)
    if not binary:
        manifest = REPO_ROOT / "rust/Cargo.toml"
        if ATELIER:
            return [ATELIER, "rust-serve", "--root", root, "--port", str(port)]
        if not manifest.is_file():
            raise RuntimeError("Rust backend is not available in this checkout")
        subprocess.run(["cargo", "build", "--release", "--manifest-path", str(manifest),
                        "-p", "atelier-server"], cwd=REPO_ROOT, check=True)
        binary = str(REPO_ROOT / "rust/target/release/atelier-server")
        if not os.path.isfile(binary):
            binary = str(REPO_ROOT / "rust/target/debug/atelier-server")
    return [binary, "--root", root, "--port", str(port), "--watch"]


def _build_command(root):
    """Build through the installed CLI, or the checked-out tool."""
    if ATELIER:
        return [ATELIER, "build", "--root", root]
    return [sys.executable, str(REPO_ROOT / "cmux_gallery.py"),
            "build", "--root", root]


def _rust_backend_requested():
    """Rust is the default; only an explicit python switch disables it."""
    return os.environ.get("ATELIER_BACKEND", "rust").strip().lower() not in ("python", "py")


@contextmanager
def _project_lock(root):
    key = hashlib.sha256(root.encode()).hexdigest()[:20]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{key}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _write_state(root, port, pid, token):
    path = _state_path(root, port)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "service": "atelier-codex",
        "project": root,
        "port": port,
        "pid": pid,
        "token": token,
        "protocol": BRIDGE_PROTOCOL,
    }, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _remove_state(root, port):
    try:
        _state_path(root, port).unlink()
    except OSError:
        pass


def _process_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _discover_server(root):
    key = hashlib.sha256(root.encode()).hexdigest()[:20]
    for path in sorted(STATE_DIR.glob(f"{key}-*.json"), reverse=True):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            port = int(state["port"])
            token = state["token"]
            live = _ping(port)
            if (state.get("service") == "atelier-codex" and _process_alive(state.get("pid")) and
                    isinstance(token, str) and token and live and
                    state.get("protocol") == BRIDGE_PROTOCOL and
                    live.get("agentHost") == "codex" and
                    live.get("agentBridgeProtocol") == BRIDGE_PROTOCOL and
                    os.path.realpath(live.get("project", "")) == root):
                _PORTS[root] = port
                _TOKENS[root] = token
                return port
            if (state.get("service") == "atelier-codex" and
                    os.path.realpath(state.get("project", "")) == root and
                    _process_alive(state.get("pid"))):
                os.kill(int(state["pid"]), signal.SIGTERM)
        except Exception:
            pass
        try:
            path.unlink()
        except OSError:
            pass
    return None


def _server_for(root, start=False):
    root = _root(root)
    known = _PORTS.get(root)
    if known:
        live_known = _ping(known)
        if (live_known and live_known.get("agentHost") == "codex" and
                live_known.get("agentBridgeProtocol") == BRIDGE_PROTOCOL and
                os.path.realpath(live_known.get("project", "")) == root and
                _TOKENS.get(root)):
            return known
        _PORTS.pop(root, None)
        _TOKENS.pop(root, None)

    discovered = _discover_server(root)
    if discovered:
        return discovered

    preferred = _stable_port(root)
    live = _ping(preferred)
    if not start:
        raise RuntimeError("Atelier is not running for this project; call atelier_open first")

    port = _free_port_near(preferred)
    token = secrets.token_urlsafe(32)
    if not ATELIER and not _rust_backend_requested():
        raise RuntimeError("atelier executable not found on PATH")
    log_dir = Path(root) / ".fig_thumbs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"atelier-codex-{port}.log", "a", encoding="utf-8")
    env = dict(os.environ, GALLERY_ROOT=root, FIG_PORT=str(port),
               ATELIER_TOOL_ROOT=str(REPO_ROOT),
               ATELIER_BUILDER=str(REPO_ROOT / "build_gallery.py"),
               ATELIER_AGENT_HOST="codex", ATELIER_AGENT_TOKEN=token)
    proc = subprocess.Popen(
        _server_command(root, port),
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    _OWNED[root] = (proc, log, port)
    _PORTS[root] = port
    _TOKENS[root] = token
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            log.close()
            _OWNED.pop(root, None)
            _PORTS.pop(root, None)
            _TOKENS.pop(root, None)
            raise RuntimeError(f"Atelier server exited with code {proc.returncode}")
        live_started = _ping(port)
        if (live_started and live_started.get("agentHost") == "codex" and
                live_started.get("agentBridgeProtocol") == BRIDGE_PROTOCOL):
            _json_request(port, _selections_path(), token=token)
            _write_state(root, port, proc.pid, token)
            return port
        time.sleep(0.1)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    log.close()
    _OWNED.pop(root, None)
    _PORTS.pop(root, None)
    _TOKENS.pop(root, None)
    raise RuntimeError("Atelier server did not answer /ping")


def atelier_open(arguments):
    root = _tool_root(arguments)
    if not ATELIER and not _rust_backend_requested():
        raise RuntimeError("atelier executable not found on PATH")
    with _project_lock(root):
        subprocess.run(
            _build_command(root),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        port = _server_for(root, start=True)
        destination = _register_destination(root, port, arguments.get("label"),
                                            arguments.get("automatic") if "automatic" in arguments else None)
    return {
        "ok": True,
        "project": root,
        "port": port,
        "url": f"http://127.0.0.1:{port}/figures_index.html?nativeFs=1",
        "agentHost": (_ping(port) or {}).get("agentHost"),
        "destination": destination,
    }


def atelier_connect(arguments):
    root = _tool_root(arguments)
    port = _server_for(root, start=True)
    destination = _register_destination(
        root, port, arguments.get("label"),
        arguments.get("automatic") if "automatic" in arguments else None,
    )
    return {"ok": True, "project": root, "port": port, "destination": destination}


def atelier_get_selection(arguments):
    root = _tool_root(arguments)
    port = _server_for(root)
    _register_destination(root, port, arguments.get("label"))
    result = _json_request(port, _selections_path(), token=_TOKENS[root])
    result.update({"project": root, "port": port})
    return result


def atelier_wait_for_annotation(arguments):
    root = _tool_root(arguments)
    port = _server_for(root)
    _register_destination(root, port, arguments.get("label"))
    timeout = max(1, min(float(arguments.get("timeoutSeconds", 30)), 55))
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _json_request(port, _selections_path(), token=_TOKENS[root])
        if result.get("items"):
            result.update({"project": root, "port": port, "timedOut": False})
            return result
        time.sleep(0.25)
    return {"items": [], "count": 0, "project": root, "port": port, "timedOut": True}


def atelier_ack_selection(arguments):
    root = _tool_root(arguments)
    ids = arguments.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids is required and must be a non-empty list")
    port = _server_for(root)
    result = _json_request(port, "/agent-selections/ack",
                           {"ids": ids, "consumer": _CONSUMER_ID}, token=_TOKENS[root])
    result.update({"project": root, "port": port})
    return result


def atelier_list_annotations(arguments):
    root = _tool_root(arguments)
    port = _server_for(root)
    _register_destination(root, port, arguments.get("label"))
    limit = max(1, min(int(arguments.get("limit", 50)), 200))
    result = _json_request(port, f"/agent-status?limit={limit}")
    result.update({"project": root, "port": port, "destinationId": _DESTINATION_ID})
    return result


def atelier_set_annotation_status(arguments):
    root = _tool_root(arguments)
    ids = arguments.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids is required and must be a non-empty list")
    port = _server_for(root)
    result = _json_request(port, "/agent-annotations/status", {
        "ids": ids,
        "status": arguments.get("status"),
        "result": arguments.get("result", ""),
        "error": arguments.get("error", ""),
    }, token=_TOKENS[root])
    result.update({"project": root, "port": port})
    return result


def atelier_rescan(arguments):
    root = _tool_root(arguments)
    port = _server_for(root)
    result = _json_request(port, "/rescan", {})
    result.update({"project": root, "port": port})
    return result


def atelier_mark_updated(arguments):
    root = _tool_root(arguments)
    port = _server_for(root)
    return _json_request(port, "/agent-event", {
        "rel": arguments.get("path", ""),
        "note": arguments.get("note", ""),
    })


def atelier_stop(arguments):
    root = _tool_root(arguments)
    owned = _OWNED.pop(root, None)
    port = _PORTS.pop(root, None)
    _TOKENS.pop(root, None)
    if not owned:
        if port:
            try:
                state = json.loads(_state_path(root, port).read_text(encoding="utf-8"))
                pid = int(state["pid"])
                if _process_alive(pid):
                    os.kill(pid, signal.SIGTERM)
                    deadline = time.time() + 5
                    while time.time() < deadline and _process_alive(pid):
                        time.sleep(0.05)
                _remove_state(root, port)
                return {"ok": True, "stopped": not _process_alive(pid),
                        "port": port, "project": root}
            except Exception as error:
                return {"ok": False, "stopped": False, "error": str(error)}
        return {"ok": True, "stopped": False, "reason": "no Codex Atelier server found"}
    proc, log, owned_port = owned
    port = owned_port
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log.close()
    _remove_state(root, port)
    return {"ok": True, "stopped": True, "port": port, "project": root}


TOOLS = {
    "atelier_open": {
        "description": "Build and start Atelier for a local project, returning its browser URL.",
        "inputSchema": {"type": "object", "required": ["root"],
                        "properties": {"root": {"type": "string"},
                                       "label": {"type": "string"},
                                       "automatic": {"type": "boolean"}}},
        "call": atelier_open,
    },
    "atelier_connect": {
        "description": "Pair this Codex task with Atelier and optionally enable automatic wake-up.",
        "inputSchema": {"type": "object", "required": ["root"], "properties": {
            "root": {"type": "string"}, "label": {"type": "string"},
            "automatic": {"type": "boolean"}
        }},
        "call": atelier_connect,
    },
    "atelier_get_selection": {
        "description": "Read annotations sent from Atelier to Codex.",
        "inputSchema": {"type": "object", "required": ["root"], "properties": {
            "root": {"type": "string"}
        }},
        "call": atelier_get_selection,
    },
    "atelier_wait_for_annotation": {
        "description": "Wait for the user to send an annotation from the open Atelier gallery.",
        "inputSchema": {"type": "object", "required": ["root"], "properties": {
            "root": {"type": "string"},
            "timeoutSeconds": {"type": "number", "minimum": 1, "maximum": 55, "default": 30}
        }},
        "call": atelier_wait_for_annotation,
    },
    "atelier_ack_selection": {
        "description": "Acknowledge annotations after Codex has safely received them.",
        "inputSchema": {"type": "object", "required": ["root", "ids"], "properties": {
            "root": {"type": "string"},
            "ids": {"type": "array", "minItems": 1, "maxItems": 100,
                    "items": {"type": "string"}}
        }},
        "call": atelier_ack_selection,
    },
    "atelier_list_annotations": {
        "description": "List Atelier destinations, pending annotations, and durable annotation history.",
        "inputSchema": {"type": "object", "required": ["root"], "properties": {
            "root": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "label": {"type": "string"}
        }},
        "call": atelier_list_annotations,
    },
    "atelier_set_annotation_status": {
        "description": "Mark annotations as processing, completed, failed, or cancelled.",
        "inputSchema": {"type": "object", "required": ["root", "ids", "status"], "properties": {
            "root": {"type": "string"},
            "ids": {"type": "array", "minItems": 1, "maxItems": 100,
                    "items": {"type": "string"}},
            "status": {"type": "string", "enum": ["processing", "completed", "failed", "cancelled"]},
            "result": {"type": "string"}, "error": {"type": "string"}
        }},
        "call": atelier_set_annotation_status,
    },
    "atelier_rescan": {
        "description": "Rebuild the running Atelier gallery after project files change.",
        "inputSchema": {"type": "object", "required": ["root"],
                        "properties": {"root": {"type": "string"}}},
        "call": atelier_rescan,
    },
    "atelier_mark_updated": {
        "description": "Notify the gallery that a project artifact was regenerated.",
        "inputSchema": {"type": "object", "required": ["root", "path"], "properties": {
            "root": {"type": "string"}, "path": {"type": "string"}, "note": {"type": "string"}
        }},
        "call": atelier_mark_updated,
    },
    "atelier_stop": {
        "description": "Stop the Atelier server started by this MCP process.",
        "inputSchema": {"type": "object", "required": ["root"],
                        "properties": {"root": {"type": "string"}}},
        "call": atelier_stop,
    },
}


def _shutdown():
    # Deliberately leave detached gallery servers alive across MCP/plugin reloads.
    # Their 0600 state files allow the next bridge process to rediscover them.
    for proc, log, _port in list(_OWNED.values()):
        try:
            log.close()
        except Exception:
            pass
    _OWNED.clear()


atexit.register(_shutdown)


def _reply(request_id, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                version = (request.get("params") or {}).get("protocolVersion", "2025-03-26")
                _reply(request_id, {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "atelier", "version": "0.1.0"},
                })
            elif method == "tools/list":
                _reply(request_id, {"tools": [
                    {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
                    for name, spec in TOOLS.items()
                ]})
            elif method == "tools/call":
                params = request.get("params") or {}
                name = params.get("name")
                if name not in TOOLS:
                    _reply(request_id, error={"code": -32601, "message": f"Unknown tool: {name}"})
                    continue
                try:
                    result = TOOLS[name]["call"](params.get("arguments") or {})
                    _reply(request_id, {
                        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                        "structuredContent": result,
                    })
                except Exception as error:
                    _reply(request_id, {
                        "content": [{"type": "text", "text": str(error)}],
                        "isError": True,
                    })
            elif request_id is not None:
                _reply(request_id, error={"code": -32601, "message": f"Unknown method: {method}"})
        except Exception as error:
            _reply(None, error={"code": -32603, "message": str(error)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
