#!/usr/bin/env python3
"""Local server for the figure gallery (port from FIG_PORT, default 8790).

POST /save  {name, dataURL}  -> writes the annotated PNG to <project>/annotations/,
copies the path to the clipboard, and pastes it into the Claude Code panel of the
active cmux workspace if there is one.
"""
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import signal
import shutil
import subprocess
import tempfile
import threading
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from atelier_runtime import ARTIFACT_EXTENSIONS, EXCLUDED_DIRECTORIES, artifact_snapshot

PROJECT = os.path.realpath(os.environ.get("GALLERY_ROOT") or os.getcwd())
HERE = os.path.dirname(os.path.realpath(__file__))
ASSETS_DIR = os.path.join(HERE, "assets")   # source vivante des viewers (biblio, annot_kit, cm/, pdfjs/…)
OUT_DIR = os.path.join(PROJECT, "annotations")
STUDIO = bool(os.environ.get("ATELIER_STUDIO"))  # embarqué dans Atelier Studio : zéro push cmux/muxy/orca
# Claude Code desktop preview: pas de push cmux/muxy/orca (aucun terminal à viser),
# mais garde le canal fichier (~/.claude/fig-last-quote.txt, fig-selection.json) + clipboard.
CLAUDE_PREVIEW = bool(os.environ.get("CLAUDE_PREVIEW"))
AGENT_HOST = (os.environ.get("ATELIER_AGENT_HOST") or "").strip().lower()
CODEX_PREVIEW = AGENT_HOST == "codex"
AGENT_TOKEN = os.environ.get("ATELIER_AGENT_TOKEN") or ""
AGENT_BRIDGE_PROTOCOL = 2
NO_PUSH = STUDIO or CLAUDE_PREVIEW or CODEX_PREVIEW
# FIG_PORT prioritaire ; sinon PORT (assigné par les harness de preview, ex. Claude Code desktop)
PORT = int(os.environ.get("FIG_PORT") or os.environ.get("PORT") or 8790)

# /thumb spawns a rasteriser per request on the threaded server, so cap concurrency:
# cheap tools (sips/rsvg) share _THUMB_SEM; heavy headless-Chrome HTML renders get their
# own tiny pool so a burst of .html cards can't fork dozens of Chrome trees at once.
_THUMB_SEM = threading.BoundedSemaphore(max(2, min(8, (os.cpu_count() or 4))))
_CHROME_SEM = threading.BoundedSemaphore(2)

# Fil d'événements Claude -> galerie (Claude Code desktop) : quand l'agent régénère
# une figure il POST /claude-event ; la page affiche un toast + rafraîchit la carte.
_CLAUDE_EVENTS = []              # [{id, ts, rel, note, row}]
_CLAUDE_EVENTS_LOCK = threading.Lock()
_CLAUDE_EVENTS_NEXT = [1]
_SNIP_EXTS = {"py", "r", "jl", "sh", "tex", "md", "csv"}

# Persistent browser -> agent inbox.  The gallery writes annotations here and a
# Codex MCP tool drains them.  Keeping the queue under the project makes the
# handoff survive a server restart and avoids coupling Codex to ~/.claude.
_AGENT_INBOX_DIR = os.path.expanduser("~/Library/Application Support/Atelier/agent-inbox")
_AGENT_INBOX_KEY = hashlib.sha256(PROJECT.encode()).hexdigest()[:24]
_AGENT_INBOX_PATH = os.path.join(_AGENT_INBOX_DIR, _AGENT_INBOX_KEY + ".json")
_AGENT_HISTORY_PATH = os.path.join(_AGENT_INBOX_DIR, _AGENT_INBOX_KEY + "-history.json")
_AGENT_CONSUMERS_PATH = os.path.join(_AGENT_INBOX_DIR, _AGENT_INBOX_KEY + "-consumers.json")
_AGENT_INBOX_LOCK = threading.Lock()
_AGENT_WAKE_LOCK = threading.Lock()
_AGENT_WAKE_RUNNING = set()
_AGENT_WAKE_DIRTY = set()


def _read_agent_inbox_unlocked():
    try:
        with open(_AGENT_INBOX_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_agent_inbox_unlocked(items):
    os.makedirs(os.path.dirname(_AGENT_INBOX_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="agent-inbox-", suffix=".tmp",
                               dir=os.path.dirname(_AGENT_INBOX_PATH))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, _AGENT_INBOX_PATH)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _read_agent_json_unlocked(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_agent_json_unlocked(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="agent-state-", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _history_update_unlocked(event_id, new_status, **extra):
    history = _read_agent_json_unlocked(_AGENT_HISTORY_PATH, [])
    found = None
    for item in history:
        if str(item.get("id")) == str(event_id):
            found = item
            break
    if found is None:
        found = {"id": str(event_id), "project": PROJECT}
        history.append(found)
    found.update(extra)
    found["status"] = new_status
    found["statusAt"] = time.time()
    del history[:-500]
    _write_agent_json_unlocked(_AGENT_HISTORY_PATH, history)


def _public_event(item):
    allowed = ("id", "ts", "type", "path", "page", "selection", "comment", "notes",
               "original", "source", "region", "anchor", "restoredFrom",
               "status", "statusAt", "destination", "destinationLabel",
               "action", "batchId", "held", "claimedBy", "claimedAt", "result", "error")
    return {key: item.get(key) for key in allowed if item.get(key) is not None}


def register_agent_consumer(consumer, destination=None, label=None, thread_id=None,
                            automatic=None, pid=None):
    consumer = str(consumer or "").strip()[:200]
    destination = str(destination or consumer).strip()[:240]
    if not consumer or not destination:
        raise ValueError("consumer and destination are required")
    now = time.time()
    with _AGENT_INBOX_LOCK:
        consumers = _read_agent_json_unlocked(_AGENT_CONSUMERS_PATH, {})
        current = consumers.get(destination) if isinstance(consumers.get(destination), dict) else {}
        current.update({
            "id": destination,
            "consumer": consumer,
            "label": str(label or current.get("label") or "Codex task")[:160],
            "threadId": str(thread_id or current.get("threadId") or "")[:120] or None,
            "pid": int(pid) if str(pid or "").isdigit() else current.get("pid"),
            "lastSeen": now,
        })
        if automatic is not None:
            current["automatic"] = bool(automatic)
        else:
            current.setdefault("automatic", False)
        consumers[destination] = current
        # Forget dead ephemeral MCP consumers after seven days, while preserving
        # thread-backed destinations that the user may intentionally resume later.
        consumers = {key: value for key, value in consumers.items()
                     if value.get("threadId") or float(value.get("lastSeen") or 0) > now - 604800}
        _write_agent_json_unlocked(_AGENT_CONSUMERS_PATH, consumers)
        return dict(current)


def get_agent_consumers(active_seconds=180):
    now = time.time()
    with _AGENT_INBOX_LOCK:
        consumers = _read_agent_json_unlocked(_AGENT_CONSUMERS_PATH, {})
    result = []
    for value in consumers.values():
        if not isinstance(value, dict):
            continue
        item = dict(value)
        process_online = False
        try:
            if item.get("pid"):
                os.kill(int(item["pid"]), 0)
                process_online = True
        except (OSError, TypeError, ValueError):
            process_online = False
        item["online"] = process_online or float(item.get("lastSeen") or 0) > now - active_seconds
        result.append(item)
    return sorted(result, key=lambda item: float(item.get("lastSeen") or 0), reverse=True)


def _public_consumer(item):
    allowed = ("id", "label", "lastSeen", "online", "automatic", "wakeState",
               "lastWake", "lastWakeFinished", "wakeError")
    return {key: item.get(key) for key in allowed if item.get(key) is not None}


def set_agent_consumer_preferences(destination, automatic=None, label=None):
    destination = str(destination or "").strip()[:240]
    with _AGENT_INBOX_LOCK:
        consumers = _read_agent_json_unlocked(_AGENT_CONSUMERS_PATH, {})
        current = consumers.get(destination)
        if not isinstance(current, dict):
            raise ValueError("unknown destination")
        if automatic is not None:
            current["automatic"] = bool(automatic)
        if isinstance(label, str) and label.strip():
            current["label"] = label.strip()[:160]
        consumers[destination] = current
        _write_agent_json_unlocked(_AGENT_CONSUMERS_PATH, consumers)
        return dict(current)


def enqueue_agent_annotation(payload):
    event = dict(payload)
    event["id"] = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    event["ts"] = time.time()
    event["project"] = PROJECT
    event["status"] = "staged" if event.get("held") else "queued"
    event["statusAt"] = event["ts"]
    with _AGENT_INBOX_LOCK:
        items = _read_agent_inbox_unlocked()
        if len(items) >= 100:
            raise RuntimeError("agent inbox is full; acknowledge pending annotations first")
        items.append(event)
        _write_agent_inbox_unlocked(items)
        _history_update_unlocked(event["id"], event["status"], **_public_event(event))
    if event["status"] == "queued":
        _schedule_automatic_agent(event)
    return event


def get_agent_annotations():
    with _AGENT_INBOX_LOCK:
        return _read_agent_inbox_unlocked()


def claim_agent_annotations(consumer, destination=None, lease_seconds=300):
    now = time.time()
    destination = str(destination or consumer)
    with _AGENT_INBOX_LOCK:
        items = _read_agent_inbox_unlocked()
        changed = False
        claimed = []
        for item in items:
            if item.get("status") == "staged" or item.get("held"):
                continue
            target = item.get("destination")
            if target not in (None, "", "auto", destination):
                continue
            owner = item.get("claimedBy")
            claimed_at = float(item.get("claimedAt") or 0)
            if not owner or claimed_at < now - lease_seconds:
                item["claimedBy"] = consumer
                item["claimedAt"] = now
                item["status"] = "received"
                item["statusAt"] = now
                owner = consumer
                changed = True
            if owner == consumer:
                claimed.append(item)
        if changed:
            _write_agent_inbox_unlocked(items)
            for item in claimed:
                _history_update_unlocked(item["id"], "received", **_public_event(item))
        return claimed


def acknowledge_agent_annotations(ids, consumer):
    wanted = {str(value) for value in ids}
    with _AGENT_INBOX_LOCK:
        items = _read_agent_inbox_unlocked()
        kept = [item for item in items
                if not (str(item.get("id")) in wanted and item.get("claimedBy") == consumer)]
        removed = len(items) - len(kept)
        if removed:
            _write_agent_inbox_unlocked(kept)
            for item in items:
                if str(item.get("id")) in wanted and item.get("claimedBy") == consumer:
                    _history_update_unlocked(item["id"], "acknowledged", **_public_event(item))
        return removed


def update_agent_annotation_status(ids, status, result="", error=""):
    allowed = {"queued", "received", "processing", "completed", "failed", "cancelled"}
    if status not in allowed:
        raise ValueError("invalid annotation status")
    wanted = {str(value) for value in ids}
    with _AGENT_INBOX_LOCK:
        history = _read_agent_json_unlocked(_AGENT_HISTORY_PATH, [])
        changed = 0
        for item in history:
            if str(item.get("id")) not in wanted:
                continue
            item["status"] = status
            item["statusAt"] = time.time()
            if result:
                item["result"] = str(result)[:2000]
            if error:
                item["error"] = str(error)[:2000]
            changed += 1
        if changed:
            _write_agent_json_unlocked(_AGENT_HISTORY_PATH, history)
        return changed


def release_agent_batch(batch_id):
    batch_id = str(batch_id or "").strip()[:120]
    released = []
    with _AGENT_INBOX_LOCK:
        items = _read_agent_inbox_unlocked()
        for item in items:
            if item.get("batchId") != batch_id or not item.get("held"):
                continue
            item["held"] = False
            item["status"] = "queued"
            item["statusAt"] = time.time()
            released.append(dict(item))
        if released:
            _write_agent_inbox_unlocked(items)
            for item in released:
                _history_update_unlocked(item["id"], "queued", **_public_event(item))
    for item in released[:1]:
        _schedule_automatic_agent(item)
    return released


def cancel_agent_batch(batch_id):
    batch_id = str(batch_id or "").strip()[:120]
    with _AGENT_INBOX_LOCK:
        items = _read_agent_inbox_unlocked()
        cancelled = [dict(item) for item in items
                     if item.get("batchId") == batch_id and item.get("held")]
        if cancelled:
            kept = [item for item in items
                    if not (item.get("batchId") == batch_id and item.get("held"))]
            _write_agent_inbox_unlocked(kept)
            for item in cancelled:
                _history_update_unlocked(item["id"], "cancelled", **_public_event(item))
        return cancelled


def release_agent_annotations(ids, destination):
    """Move selected banked annotations to one explicit Codex task."""
    wanted = {str(value) for value in (ids or [])}
    destination = str(destination or "").strip()[:240]
    matched = next((item for item in get_agent_consumers(active_seconds=86400 * 30)
                    if item.get("id") == destination), None)
    if not wanted:
        raise ValueError("ids are required")
    if not matched:
        raise ValueError("unknown destination")
    released = []
    with _AGENT_INBOX_LOCK:
        items = _read_agent_inbox_unlocked()
        for item in items:
            if str(item.get("id")) not in wanted:
                continue
            if item.get("claimedBy") or not (item.get("held") or item.get("status") == "staged"):
                continue
            item["destination"] = destination
            item["destinationLabel"] = matched.get("label")
            item["held"] = False
            item["batchId"] = None
            item["status"] = "queued"
            item["statusAt"] = time.time()
            released.append(dict(item))
        if released:
            _write_agent_inbox_unlocked(items)
            for item in released:
                _history_update_unlocked(item["id"], "queued", **_public_event(item))
    for item in released:
        _schedule_automatic_agent(item)
    return released


def delete_agent_annotations(ids):
    """Delete selected unsent annotations from the project bank."""
    wanted = {str(value) for value in (ids or [])}
    if not wanted:
        raise ValueError("ids are required")
    with _AGENT_INBOX_LOCK:
        items = _read_agent_inbox_unlocked()
        deleted = [dict(item) for item in items
                   if str(item.get("id")) in wanted and not item.get("claimedBy")
                   and (item.get("held") or item.get("status") == "staged")]
        if deleted:
            deleted_ids = {str(item["id"]) for item in deleted}
            _write_agent_inbox_unlocked(
                [item for item in items if str(item.get("id")) not in deleted_ids]
            )
            for item in deleted:
                _history_update_unlocked(item["id"], "cancelled", **_public_event(item))
        return deleted


def restore_agent_annotations(ids):
    """Restore cancelled bank items as new staged events with traceable ids."""
    wanted = {str(value) for value in (ids or [])}
    if not wanted:
        raise ValueError("ids are required")
    with _AGENT_INBOX_LOCK:
        history = _read_agent_json_unlocked(_AGENT_HISTORY_PATH, [])
        source = [dict(item) for item in history
                  if str(item.get("id")) in wanted and item.get("status") == "cancelled"]
    restored = []
    for old in source:
        payload = {key: value for key, value in _public_event(old).items()
                   if key not in {"id", "ts", "status", "statusAt", "claimedBy", "claimedAt",
                                  "result", "error", "destination", "destinationLabel"}}
        payload.update({"held": True, "action": old.get("action") or "ask",
                        "restoredFrom": old.get("id")})
        restored.append(enqueue_agent_annotation(payload))
    return restored


def normalize_agent_notes(value):
    if not isinstance(value, list):
        return []
    notes = []
    for raw in value[:100]:
        if not isinstance(raw, dict):
            continue
        text = raw.get("text")
        if not isinstance(text, str):
            continue
        notes.append({"n": raw.get("n"), "text": text[:2000]})
    return notes


def normalize_agent_anchor(req, rel):
    """Produce one portable anchor contract from existing viewer payloads."""
    raw = req.get("anchor")
    if isinstance(raw, dict) and isinstance(raw.get("kind"), str):
        anchor = {"kind": raw["kind"][:80]}
        for key in ("startLine", "endLine", "startColumn", "endColumn", "page",
                    "x", "y", "width", "height", "selector"):
            value = raw.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                anchor[key] = value[:2000] if isinstance(value, str) else value
        return anchor
    page = req.get("page")
    match = re.fullmatch(r"L(\d+)-(\d+)", str(page or ""))
    if match:
        return {"kind": "text-range", "startLine": int(match.group(1)),
                "endLine": int(match.group(2))}
    region = req.get("region")
    if isinstance(region, dict):
        anchor = {"kind": "image-region"}
        for key in ("x", "y", "width", "height", "selector"):
            if isinstance(region.get(key), (str, int, float)):
                value = region[key]
                anchor[key] = value[:2000] if isinstance(value, str) else value
        if page not in (None, ""):
            anchor["page"] = page
            anchor["kind"] = "pdf-region"
        return anchor
    if page not in (None, "", "html"):
        ext = os.path.splitext(rel or "")[1].lower()
        return {"kind": "pdf-page" if ext == ".pdf" else "document-location", "page": page}
    return {"kind": "artifact"}


def normalize_agent_delivery(req):
    action = req.get("action")
    if action not in ("ask", "apply"):
        action = "apply" if req.get("direct") else "ask"
    destination = req.get("destination")
    destination = destination.strip()[:240] if isinstance(destination, str) else ""
    consumers = get_agent_consumers(active_seconds=86400 * 30)
    matched = next((item for item in consumers if item.get("id") == destination), None)
    if not matched:
        destination = ""
    batch_id = req.get("batchId")
    batch_id = batch_id.strip()[:120] if isinstance(batch_id, str) else ""
    held = bool(req.get("held"))
    return {
        "destination": destination or None,
        "destinationLabel": matched.get("label") if matched else None,
        "action": action,
        "batchId": batch_id or None,
        "held": held,
    }


def _set_agent_consumer_runtime(destination, **fields):
    with _AGENT_INBOX_LOCK:
        consumers = _read_agent_json_unlocked(_AGENT_CONSUMERS_PATH, {})
        current = consumers.get(destination)
        if not isinstance(current, dict):
            return
        current.update(fields)
        consumers[destination] = current
        _write_agent_json_unlocked(_AGENT_CONSUMERS_PATH, consumers)


def _schedule_automatic_agent(event):
    """Wake an explicitly paired Codex task when its user-controlled auto mode is on.

    The worker never uses a bypass flag.  It resumes the selected task under the
    workspace-write sandbox and lets the task's normal policy decide what can run.
    """
    destination = str(event.get("destination") or "").strip()
    if not destination:
        return False
    consumer = next((item for item in get_agent_consumers(active_seconds=86400 * 30)
                     if item.get("id") == destination), None)
    thread_id = str((consumer or {}).get("threadId") or "")
    if not consumer or not consumer.get("automatic") or not re.fullmatch(
            r"[0-9a-fA-F-]{32,40}", thread_id):
        return False
    codex = shutil.which("codex")
    if not codex:
        return False
    with _AGENT_WAKE_LOCK:
        if destination in _AGENT_WAKE_RUNNING:
            _AGENT_WAKE_DIRTY.add(destination)
            return False
        _AGENT_WAKE_RUNNING.add(destination)
    _set_agent_consumer_runtime(destination, wakeState="starting", lastWake=time.time())

    def run():
        prompt = (
            "Une ou plusieurs annotations Atelier sont en attente pour ce projet. "
            "Utilise le skill Atelier et atelier_get_selection pour toutes les récupérer. "
            "Respecte le champ action de chaque annotation: réponds seulement pour ask; "
            "n'applique une modification que pour apply. Accuse réception, marque le statut "
            "processing puis completed ou failed, et actualise l'artefact si nécessaire."
        )
        succeeded = False
        try:
            _set_agent_consumer_runtime(destination, wakeState="running")
            result = subprocess.run(
                [codex, "exec", "-C", PROJECT, "-s", "workspace-write",
                 "resume", thread_id, prompt],
                cwd=PROJECT, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=1800, start_new_session=True,
            )
            if result.returncode == 0:
                succeeded = True
                _set_agent_consumer_runtime(destination, wakeState="idle",
                                            lastWakeFinished=time.time(), wakeError="")
            else:
                error = (result.stderr or result.stdout or "Codex resume failed")[-1200:]
                _set_agent_consumer_runtime(destination, wakeState="failed",
                                            lastWakeFinished=time.time(), wakeError=error)
        except Exception as error:
            _set_agent_consumer_runtime(destination, wakeState="failed",
                                        lastWakeFinished=time.time(), wakeError=str(error)[:1200])
        finally:
            with _AGENT_WAKE_LOCK:
                _AGENT_WAKE_RUNNING.discard(destination)
                rerun = succeeded and destination in _AGENT_WAKE_DIRTY
                _AGENT_WAKE_DIRTY.discard(destination)
            if rerun:
                with _AGENT_INBOX_LOCK:
                    pending = next((dict(item) for item in _read_agent_inbox_unlocked()
                                    if item.get("status") == "queued" and
                                    item.get("destination") == destination), None)
                if pending:
                    _schedule_automatic_agent(pending)

    threading.Thread(target=run, daemon=True, name="atelier-codex-wake").start()
    return True


# ---------------------------------------------------------------------------
# Bibliothèque Zotero — lecture directe de zotero.sqlite (copie fraîche, readonly :
# fonctionne que Zotero soit ouvert ou non ; jamais d'écriture dans la base Zotero).
# Port Python du sidecar Node d'Atelier Studio (zotero.mjs).
ZOTERO_DIR = os.path.expanduser("~/Zotero")
_ZOTERO_SRC = os.path.join(ZOTERO_DIR, "zotero.sqlite")
_ZOTERO_CACHE = os.path.expanduser("~/Library/Application Support/cmux-gallery")
_ZOTERO_COPY = os.path.join(_ZOTERO_CACHE, "zotero-read.sqlite")
_ZOTERO_FAVS = os.path.join(_ZOTERO_CACHE, "zotero-favs.json")
_ZOTERO_LOCK = threading.Lock()
_ZOTERO_MTIME = [0.0]

_ZOTERO_BASE_SQL = """
  SELECT i.itemID, i.key, i.dateAdded,
    (SELECT v.value FROM itemData d
       JOIN fields f ON f.fieldID = d.fieldID AND f.fieldName = 'title'
       JOIN itemDataValues v ON v.valueID = d.valueID
     WHERE d.itemID = i.itemID) AS title,
    (SELECT v.value FROM itemData d
       JOIN fields f ON f.fieldID = d.fieldID AND f.fieldName = 'date'
       JOIN itemDataValues v ON v.valueID = d.valueID
     WHERE d.itemID = i.itemID) AS rawDate,
    (SELECT v.value FROM itemData d
       JOIN fields f ON f.fieldID = d.fieldID AND f.fieldName = 'publicationTitle'
       JOIN itemDataValues v ON v.valueID = d.valueID
     WHERE d.itemID = i.itemID) AS publication,
    (SELECT v.value FROM itemData d
       JOIN fields f ON f.fieldID = d.fieldID AND f.fieldName = 'DOI'
       JOIN itemDataValues v ON v.valueID = d.valueID
     WHERE d.itemID = i.itemID) AS doi,
    (SELECT v.value FROM itemData d
       JOIN fields f ON f.fieldID = d.fieldID AND f.fieldName = 'abstractNote'
       JOIN itemDataValues v ON v.valueID = d.valueID
     WHERE d.itemID = i.itemID) AS abstract,
    (SELECT GROUP_CONCAT(c.lastName, ', ') FROM itemCreators ic
       JOIN creators c ON c.creatorID = ic.creatorID
     WHERE ic.itemID = i.itemID ORDER BY ic.orderIndex) AS creators,
    (SELECT GROUP_CONCAT(t.name, char(31)) FROM itemTags it
       JOIN tags t ON t.tagID = it.tagID WHERE it.itemID = i.itemID) AS tags,
    (SELECT ia.path FROM itemAttachments ia
     WHERE ia.parentItemID = i.itemID AND ia.contentType = 'application/pdf'
       AND ia.path LIKE 'storage:%' LIMIT 1) AS pdfPath,
    (SELECT ai.key FROM itemAttachments ia JOIN items ai ON ai.itemID = ia.itemID
     WHERE ia.parentItemID = i.itemID AND ia.contentType = 'application/pdf'
       AND ia.path LIKE 'storage:%' LIMIT 1) AS pdfKey
  FROM items i
  JOIN itemTypes t ON t.itemTypeID = i.itemTypeID
  WHERE t.typeName NOT IN ('attachment', 'note', 'annotation')
    AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
"""


def _zotero_available():
    return os.path.exists(_ZOTERO_SRC)


def _zotero_db():
    """Fresh readonly connection; recopy the DB when Zotero's file changed."""
    import sqlite3
    if not _zotero_available():
        raise FileNotFoundError("Zotero introuvable (~/Zotero/zotero.sqlite)")
    with _ZOTERO_LOCK:
        mtime = os.path.getmtime(_ZOTERO_SRC)
        if mtime != _ZOTERO_MTIME[0] or not os.path.exists(_ZOTERO_COPY):
            os.makedirs(_ZOTERO_CACHE, exist_ok=True)
            shutil.copyfile(_ZOTERO_SRC, _ZOTERO_COPY)
            wal = _ZOTERO_SRC + "-wal"
            if os.path.exists(wal):
                try:
                    shutil.copyfile(wal, _ZOTERO_COPY + "-wal")
                except Exception:
                    pass
            _ZOTERO_MTIME[0] = mtime
    con = sqlite3.connect(f"file:{_ZOTERO_COPY}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _zotero_cite_key(creators, year):
    first = re.sub(r"[^a-z]", "", (creators or "").split(",")[0].strip().lower()) or "ref"
    return f"{first}{year or ''}"


def _zotero_row_to_item(r):
    m = re.search(r"(\d{4})", r["rawDate"] or "")
    year = m.group(1) if m else ""
    tags = [t for t in (r["tags"] or "").split(chr(31)) if t]
    creators = r["creators"] or ""
    pdf_path = r["pdfPath"] or ""
    return {"key": r["key"], "dateAdded": r["dateAdded"] or "",
            "title": r["title"] or "(sans titre)", "creators": creators,
            "year": year, "publication": r["publication"] or "",
            "doi": r["doi"] or "", "abstract": r["abstract"] or "", "tags": tags,
            "hasPdf": bool(pdf_path), "pdfKey": r["pdfKey"],
            "pdfFile": pdf_path[len("storage:"):] if pdf_path.startswith("storage:") else None,
            "citeKey": _zotero_cite_key(creators, year)}


def zotero_search(query="", collection_id=None, limit=400):
    con = _zotero_db()
    try:
        sql, params = _ZOTERO_BASE_SQL, []
        if collection_id:
            sql += " AND i.itemID IN (SELECT itemID FROM collectionItems WHERE collectionID = ?)"
            params.append(collection_id)
        sql += " ORDER BY i.dateModified DESC LIMIT 2000"
        rows = [_zotero_row_to_item(r) for r in con.execute(sql, params)]
    finally:
        con.close()
    q = (query or "").strip().lower()
    if q:
        terms = q.split()
        rows = [it for it in rows
                if all(t in f"{it['title']} {it['creators']} {it['year']} {it['publication']} {' '.join(it['tags'])}".lower()
                       for t in terms)]
    favs = _zotero_favs_load()
    for it in rows:
        it["fav"] = it["key"] in favs
    return rows[:limit]


def zotero_collections():
    con = _zotero_db()
    try:
        return [dict(r) for r in con.execute("""
            SELECT collectionID AS id, collectionName AS name, parentCollectionID AS parent
            FROM collections
            WHERE collectionID NOT IN (SELECT collectionID FROM deletedCollections)
            ORDER BY collectionName COLLATE NOCASE""")]
    finally:
        con.close()


def _zotero_favs_load():
    try:
        with open(_ZOTERO_FAVS) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _zotero_favs_save(favs):
    os.makedirs(_ZOTERO_CACHE, exist_ok=True)
    tmp = _ZOTERO_FAVS + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(sorted(favs), f)
    os.replace(tmp, _ZOTERO_FAVS)


def zotero_find_duplicate(md5, fname):
    """Titre du parent si un PDF identique (hash ou même nom) existe déjà, sinon None."""
    con = _zotero_db()
    try:
        rows = con.execute("""
            SELECT ia.path, ia.storageHash, ai.key AS attKey,
              COALESCE((SELECT v.value FROM itemData dd
                 JOIN fields f ON f.fieldID = dd.fieldID AND f.fieldName = 'title'
                 JOIN itemDataValues v ON v.valueID = dd.valueID
               WHERE dd.itemID = ia.parentItemID), ia.path) AS parentTitle
            FROM itemAttachments ia
            JOIN items ai ON ai.itemID = ia.itemID
            WHERE ia.contentType = 'application/pdf' AND ia.path LIKE 'storage:%'
              AND ai.itemID NOT IN (SELECT itemID FROM deletedItems)""").fetchall()
    finally:
        con.close()
    base = (fname or "").lower()
    for r in rows:
        f = str(r["path"])[len("storage:"):]
        if r["storageHash"] and r["storageHash"] == md5:
            return str(r["parentTitle"])
        stored = os.path.join(ZOTERO_DIR, "storage", r["attKey"], f)
        if f.lower() == base:
            if r["storageHash"]:
                return str(r["parentTitle"])
            try:
                if os.path.exists(stored) and hashlib.md5(open(stored, "rb").read()).hexdigest() == md5:
                    return str(r["parentTitle"])
            except Exception:
                pass
            return str(r["parentTitle"])   # même nom, hash illisible : prudence
        if not r["storageHash"]:
            try:
                if os.path.exists(stored) and hashlib.md5(open(stored, "rb").read()).hexdigest() == md5:
                    return str(r["parentTitle"])
            except Exception:
                pass
    return None


def zotero_add_pdf(name, data):
    """Envoie un PDF à Zotero via l'API locale du connecteur (port 23119).
    Zotero doit tourner ; il importe le fichier puis reconnaît les métadonnées."""
    import urllib.request
    import urllib.error
    import uuid
    md5 = hashlib.md5(data).hexdigest()
    dup = zotero_find_duplicate(md5, name)
    if dup:
        return {"name": name, "ok": False, "error": "duplicate", "match": dup}
    req = urllib.request.Request(
        "http://127.0.0.1:23119/connector/saveStandaloneAttachment", data=data, method="POST",
        headers={"Content-Type": "application/pdf",
                 "X-Metadata": json.dumps({"url": "file:///" + name, "title": name,
                                           "sessionID": uuid.uuid4().hex[:8]})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"name": name, "ok": resp.status == 201, "status": resp.status}
    except urllib.error.URLError:
        return {"name": name, "ok": False, "error": "zotero-off"}
    except Exception as e:
        return {"name": name, "ok": False, "error": str(e)}
# ---------------------------------------------------------------------------


def _claude_event_row(full, rel):
    st = os.stat(full)
    ext = os.path.splitext(rel)[1].lstrip(".").lower()
    bt = int(getattr(st, "st_birthtime", st.st_mtime))
    return {"thumb": None, "code": ext in _SNIP_EXTS, "name": os.path.basename(rel),
            "rel": rel, "folder": os.path.dirname(rel) or ".", "ext": ext,
            "mtime": int(st.st_mtime), "btime": bt,
            "mdate": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "bdate": time.strftime("%Y-%m-%d %H:%M", time.localtime(bt)),
            "size": st.st_size, "archive": False}

# Whiteboard: pending commands (Claude/gallery → canvas), drained by /board/poll.
_BOARD_QUEUE = []
_BOARD_LOCK = threading.Lock()
_BOARD_QUEUE_MAX = 500


def _kill_pg(proc):
    """SIGKILL a process AND its group. qlmanage/Chrome fork helper processes that
    outlive a plain proc.kill() — that is what orphans them after a timeout."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _chrome_html_screenshot(chrome, src, out_png):
    """Headless-Chrome screenshot of an .html file -> "<out_png>.tmp.png" (or None).

    Runs in its own session under a concurrency cap and is killpg'd on timeout, so a
    page that hangs Chrome (some heavy plotly bundles do) can't orphan Chrome's
    GPU/renderer children — the previous subprocess.run only killed the parent and left
    the helpers running. (No --user-data-dir: with one, this Chrome won't exit after the
    screenshot and every render would burn the full 25s timeout; --headless=new isolates
    each invocation on the default profile, so concurrent renders don't collide anyway.)"""
    shot = out_png + ".tmp.png"
    with _CHROME_SEM:
        proc = None
        try:
            proc = subprocess.Popen(
                [chrome, "--headless=new", "--hide-scrollbars",
                 "--screenshot=" + shot, "--window-size=1000,750",
                 "--virtual-time-budget=4000", "file://" + src],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            proc.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            _kill_pg(proc)
        except Exception:
            _kill_pg(proc)
    return shot if os.path.exists(shot) else None


def _orca_cli():
    return shutil.which("orca") or ("/usr/local/bin/orca" if os.path.exists("/usr/local/bin/orca") else None)


def _compact_window(win):
    if not isinstance(win, dict):
        return None
    keys = ("id", "title", "x", "y", "width", "height", "screenIndex",
            "isMinimized", "isOffscreen")
    return {k: win.get(k) for k in keys if k in win}


def _run_orca_json(args, timeout=8):
    cli = _orca_cli()
    if not cli:
        return False, {"error": "orca CLI not found"}
    try:
        r = subprocess.run([cli] + args + ["--json"], capture_output=True,
                           text=True, timeout=timeout)
    except Exception as e:
        return False, {"error": str(e)}
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        data = {"stdout": (r.stdout or "")[-800:]}
    if r.stderr:
        data["stderr"] = r.stderr[-800:]
    ok = r.returncode == 0 and data.get("ok", True) is not False
    if not ok and "error" not in data:
        data["error"] = "orca command failed"
    return ok, data


def _activate_orca():
    for script in (
        'tell application id "com.stablyai.orca" to activate',
        'tell application "Orca" to activate',
    ):
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True,
                               text=True, timeout=3)
            if r.returncode == 0:
                time.sleep(0.25)
                return True, None
        except Exception as e:
            err = str(e)
        else:
            err = (r.stderr or r.stdout or "activation failed").strip()
    return False, err


def _orca_window_state(restore=True):
    args = ["computer", "get-app-state", "--app", "Orca", "--no-screenshot"]
    if restore:
        args.append("--restore-window")
    ok, data = _run_orca_json(args, timeout=10)
    snap = ((data.get("result") or {}).get("snapshot") or {}) if isinstance(data, dict) else {}
    win = _compact_window(snap.get("window"))
    if ok and win:
        return True, {"window": win}

    ok2, data2 = _run_orca_json(["computer", "list-windows", "--app", "Orca"],
                                timeout=8)
    wins = ((data2.get("result") or {}).get("windows") or []) if isinstance(data2, dict) else []
    win2 = _compact_window(wins[0]) if wins else None
    if ok2 and win2:
        return True, {"window": win2, "fallback": "list-windows"}
    return False, {"error": (data.get("error") if isinstance(data, dict) else None)
                   or (data2.get("error") if isinstance(data2, dict) else None)
                   or "no Orca window found"}


def _orca_ax_fullscreen():
    script = '''
tell application id "com.stablyai.orca" to activate
delay 0.1
tell application "System Events"
  tell process "Orca"
    if (count of windows) is 0 then return "missing"
    return value of attribute "AXFullScreen" of window 1
  end tell
end tell
'''
    try:
        r = subprocess.run(["osascript"], input=script, capture_output=True,
                           text=True, timeout=5)
    except Exception:
        return None
    out = (r.stdout or "").strip().lower()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def _orca_press_escape(win_id=None):
    args = ["computer", "press-key", "--app", "Orca", "--restore-window",
            "--no-screenshot", "--key", "Escape"]
    if win_id:
        args[4:4] = ["--window-id", str(win_id)]
    return _run_orca_json(args, timeout=8)


def _osascript_escape_key():
    script = '''
tell application id "com.stablyai.orca" to activate
delay 0.1
tell application "System Events"
  key code 53
end tell
'''
    try:
        r = subprocess.run(["osascript"], input=script, capture_output=True,
                           text=True, timeout=5)
        return r.returncode == 0, {"stderr": (r.stderr or "")[-800:]}
    except Exception as e:
        return False, {"error": str(e)}


def _osascript_fullscreen_hotkey():
    script = '''
tell application id "com.stablyai.orca" to activate
delay 0.2
tell application "System Events"
  key code 3 using {control down, command down}
end tell
'''
    try:
        r = subprocess.run(["osascript"], input=script, capture_output=True,
                           text=True, timeout=5)
        return r.returncode == 0, {"stderr": (r.stderr or "")[-800:]}
    except Exception as e:
        return False, {"error": str(e)}


def orca_fullscreen_exit():
    """Deprecated compatibility endpoint.

    Older generated galleries called this after entering Orca's broken WebKit
    fullscreen. Driving Orca from that request can freeze the whole app, so the
    current Orca path avoids WebKit fullscreen entirely and this route is inert.
    """
    return {"ok": True, "deprecated": True, "method": "noop; use /orca-native-fullscreen"}


NATIVE_FULLSCREEN_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".svg"}


def launch_native_fullscreen(path):
    viewer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_fullscreen_viewer.py")
    if not os.path.isfile(viewer):
        return False, {"error": "native fullscreen viewer missing"}
    try:
        proc = subprocess.Popen(
            [sys.executable, viewer, path],
            cwd=PROJECT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return False, {"error": str(e)}
    threading.Thread(target=proc.wait, daemon=True).start()
    return True, {"pid": proc.pid}


def find_tex_root(p):
    """Root document of a .tex file: itself if it has \\documentclass,
    else the % !TEX root directive, else a sibling/parent .tex that includes it."""
    try:
        txt = open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return p
    if "\\documentclass" in txt:
        return p
    m = re.search(r"%\s*!TEX\s+root\s*=\s*(.+)", txt, re.I)
    if m:
        cand = os.path.realpath(os.path.join(os.path.dirname(p), m.group(1).strip()))
        if os.path.isfile(cand):
            return cand
    stem = os.path.splitext(os.path.basename(p))[0]
    d = os.path.dirname(p)
    for folder in (d, os.path.dirname(d)):
        try:
            for fn in os.listdir(folder):
                if not fn.endswith(".tex"):
                    continue
                cand = os.path.join(folder, fn)
                try:
                    t = open(cand, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                if "\\documentclass" in t and re.search(
                        r"\\(?:input|include)\{[^}]*" + re.escape(stem), t):
                    return cand
        except Exception:
            continue
    return p


def find_muxy_claude_pane():
    if NO_PUSH:
        return None
    """Muxy fallback: pane id of a Claude Code session, preferring this project.

    `muxy list-panes` lines: <id>\t<title>\t<cwd>\t<active>. Claude sessions
    carry a status glyph (✳ running / ⠂ working) in the title. Prefer a pane
    whose cwd is inside PROJECT (active one first), else any Claude pane."""
    exe = shutil.which("muxy") or ("/usr/local/bin/muxy" if os.path.exists("/usr/local/bin/muxy") else None)
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "list-panes"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        root = os.path.realpath(PROJECT)
        in_proj, anywhere = [], []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            pane, title, cwd = parts[0], parts[1], parts[2]
            active = len(parts) > 3 and parts[3].strip() == "true"
            if not any(g in title for g in ("✳", "⠂", "Claude")):
                continue
            entry = (0 if active else 1, pane)
            cw = os.path.realpath(cwd) if cwd else ""
            (in_proj if cw == root or cw.startswith(root + os.sep) else anywhere).append(entry)
        for pool in (in_proj, anywhere):
            if pool:
                return sorted(pool)[0][1]
        return None
    except Exception:
        return None


def find_orca_claude_terminal():
    if NO_PUSH:
        return None
    """Orca fallback: handle of a live Claude terminal in this project's worktree."""
    exe = shutil.which("orca") or ("/usr/local/bin/orca" if os.path.exists("/usr/local/bin/orca") else None)
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "terminal", "list", "--worktree",
                            "path:" + os.path.realpath(PROJECT), "--json"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout or "null")
        if isinstance(data, dict):
            terms = data.get("terminals") or (data.get("result") or {}).get("terminals")
        else:
            terms = data
        for t in terms or []:
            blob = json.dumps(t).lower()
            if "claude" in blob:
                return t.get("handle") or t.get("id")
        return None
    except Exception:
        return None


def list_claude_targets():
    """Every live Claude session across muxy/orca (+cmux registry), for the picker.
    Project-scoped entries first, active ones first within each group."""
    targets = []
    root = os.path.realpath(PROJECT)
    exe = shutil.which("muxy") or ("/usr/local/bin/muxy" if os.path.exists("/usr/local/bin/muxy") else None)
    if exe:
        try:
            r = subprocess.run([exe, "list-panes"], capture_output=True, text=True, timeout=5)
            for line in (r.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 3 or not any(g in parts[1] for g in ("✳", "⠂", "Claude")):
                    continue
                cw = os.path.realpath(parts[2]) if parts[2] else ""
                targets.append({"app": "muxy", "id": parts[0],
                                "title": parts[1].lstrip("✳⠂ ").strip()[:80],
                                "cwd": parts[2],
                                "inProject": cw == root or cw.startswith(root + os.sep),
                                "active": len(parts) > 3 and parts[3].strip() == "true"})
        except Exception:
            pass
    exe = shutil.which("orca") or ("/usr/local/bin/orca" if os.path.exists("/usr/local/bin/orca") else None)
    if exe:
        try:
            r = subprocess.run([exe, "terminal", "list", "--json"],
                               capture_output=True, text=True, timeout=5)
            data = json.loads(r.stdout or "null")
            if isinstance(data, dict):
                terms = data.get("terminals") or (data.get("result") or {}).get("terminals")
            else:
                terms = data
            for t in terms or []:
                blob = json.dumps(t).lower()
                if "claude" not in blob:
                    continue
                cw = str(t.get("worktreePath") or t.get("cwd") or t.get("path") or "")
                cwr = os.path.realpath(cw) if cw else ""
                targets.append({"app": "orca", "id": t.get("handle") or t.get("id"),
                                "title": str(t.get("title") or t.get("name") or "Claude").lstrip("\u2733\u2802 ").strip()[:80],
                                "cwd": cw,
                                "inProject": bool(cwr) and (cwr == root or cwr.startswith(root + os.sep)),
                                "active": bool(t.get("focused") or t.get("active"))})
        except Exception:
            pass
    for s2 in _cmux_all_claude_surfaces():
        targets.append({"app": "cmux", "id": s2["ref"],
                        "title": (s2["title"] + " \u2014 " + s2["ws"])[:80],
                        "cwd": "", "inProject": True,
                        "active": s2["selectedInWs"]})
    targets.sort(key=lambda t: (not t["active"], not t["inProject"]))
    return targets


def _oneline(msg):
    """muxy send truncates at the first newline — flatten the message."""
    return "  ·  ".join(part for part in (p.strip() for p in msg.splitlines()) if part)


def send_to_target(target, msg, direct):
    if NO_PUSH:
        return False
    """Push msg to an explicit {app, id} target. Returns True on success."""
    try:
        app, tid = target.get("app"), target.get("id")
        if not tid:
            return False
        if app == "muxy":
            r = subprocess.run(["muxy", "send", "--pane", tid, _oneline(msg)],
                               capture_output=True, timeout=5)
            if r.returncode != 0:
                return False
            if direct:
                time.sleep(0.4)
                subprocess.run(["muxy", "send-keys", "--pane", tid, "Enter"],
                               capture_output=True, timeout=5)
            return True
        if app == "orca":
            args = ["orca", "terminal", "send", "--terminal", tid, "--text", msg]
            if direct:
                args.append("--enter")
            return subprocess.run(args, capture_output=True, timeout=8).returncode == 0
        if app == "cmux":
            r = subprocess.run(["cmux", "send", "--surface", tid, msg], env=_cmux_env(),
                               capture_output=True, timeout=5)
            if r.returncode != 0:
                return False
            if direct:
                time.sleep(0.4)
                subprocess.run(["cmux", "send-key", "--surface", tid, "enter"], env=_cmux_env(),
                               capture_output=True, timeout=5)
            return True
    except Exception:
        pass
    return False


def _cmux_env():
    """Env for cmux CLI calls: present the socket password so the detached
    gallery daemon passes the app's password-mode socket policy."""
    env = dict(os.environ)
    try:
        pw = open(os.path.expanduser("~/.config/cmux/.gallery-socket-pw")).read().strip()
        if pw:
            env["CMUX_SOCKET_PASSWORD"] = pw
    except Exception:
        pass
    return env


def _cmux_exe():
    return shutil.which("cmux") or next(
        (p for p in ("/Applications/cmux.app/Contents/Resources/bin/cmux",
                     os.path.expanduser("~/.local/bin/cmux")) if os.path.exists(p)), None)


def _cmux_all_claude_surfaces():
    """Claude surfaces across ALL cmux workspaces:
    [{"ref", "title", "ws", "selectedInWs", "wsActive"}].
    The gallery usually lives in its own workspace, so the active workspace
    often has no Claude surface at all — enumerate everything."""
    exe = _cmux_exe()
    if not exe:
        return []
    out = []
    try:
        r = subprocess.run([exe, "tree", "--all"], capture_output=True,
                           text=True, timeout=5, env=_cmux_env())
        if r.returncode != 0:
            return []
        ws_title, ws_active = "", False
        for ln in r.stdout.splitlines():
            wm = re.search(r"workspace\s+(workspace:\d+)\s+\"([^\"]*)\"(.*)$", ln)
            if wm:
                ws_title = wm.group(2)
                ws_active = "active" in wm.group(3) or "[selected]" in wm.group(3)
                continue
            sm = re.search(r"surface\s+(surface:\d+)\s+\[terminal\]\s+\"([^\"]*)\"(.*)$", ln)
            if not sm or not re.search(r"[✳⠀-⣿]", sm.group(2)):
                continue
            title = re.sub(r"^[✳⠀-⣿\s]+", "", sm.group(2)).strip()
            out.append({"ref": sm.group(1), "title": title, "ws": ws_title,
                        "selectedInWs": "[selected]" in sm.group(3),
                        "wsActive": ws_active})
    except Exception:
        pass
    return out


def _cmux_visible_claude_surfaces():
    """(selected_ref, [other_refs]) of Claude surfaces in the active cmux workspace.

    `cmux list-pane-surfaces` lines look like `* surface:29  ⠂ Title  [selected]`;
    the ✳ (running) / ⠂ (working) glyph marks a Claude Code session."""
    exe = _cmux_exe()
    if not exe:
        return None, []
    try:
        r = subprocess.run([exe, "list-pane-surfaces"], capture_output=True, text=True, timeout=5, env=_cmux_env())
        if r.returncode != 0:
            return None, []
        sel, others = None, []
        for ln in r.stdout.splitlines():
            m = re.search(r"(surface:\d+)\s+(.*)$", ln)
            if not m or not re.search(r"[\u2733\u2800-\u28FF]", ln):
                continue
            if "[selected]" in ln and sel is None:
                sel = m.group(1)
            else:
                others.append(m.group(1))
        return sel, others
    except Exception:
        return None, []


def find_claude_surface():
    if NO_PUSH:
        return None
    """Target Claude Code panel surface.

    Priority: (1) selected Claude surface in the active workspace,
    (2) any Claude surface in the active workspace,
    (3) most-recent live Claude session (cmux-sessions.json registry).
    Claude sessions are identified via the registry filled by the
    SessionStart hook cmux-register.sh (PID still alive = active session).
    """
    # 0. what's on screen: Claude surfaces across workspaces — the selected one
    # in the active workspace first; the gallery's own workspace has none, so
    # fall back to the selected Claude surface of another workspace (unique
    # candidate wins outright). No registry needed.
    surfs = _cmux_all_claude_surfaces()
    if surfs:
        if len(surfs) == 1:
            return surfs[0]["ref"]
        ranked = sorted(surfs, key=lambda s2: (not (s2["wsActive"] and s2["selectedInWs"]),
                                               not s2["selectedInWs"], not s2["wsActive"]))
        return ranked[0]["ref"]

    # 1. registry of live Claude sessions, most recent first
    try:
        entries = json.load(open(os.path.expanduser("~/.claude/cmux-sessions.json")))
    except Exception:
        return None
    alive = []
    for e in sorted(entries, key=lambda x: -x.get("registered_at", 0)):
        pid = e.get("shell_pid")
        sid = e.get("surface_id")
        if not pid or not sid:
            continue
        try:
            os.kill(pid, 0)
            alive.append(sid.upper())
        except OSError:
            continue
    if not alive:
        return None

    # 2. surfaces in the active workspace, selected ones first
    def run(args):
        try:
            return subprocess.run(["cmux"] + args, capture_output=True,
                                  text=True, timeout=5).stdout
        except Exception:
            return ""

    ws = None
    try:
        ident = json.loads(run(["identify", "--json"]))
        ws = (ident.get("focused") or {}).get("workspace_ref")
    except Exception:
        pass

    if ws:
        lines = run(["list-pane-surfaces", "--workspace", ws,
                     "--id-format", "both"]).splitlines()
        uuids_sel, uuids_other = [], []
        for ln in lines:
            m = re.search(r"([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})", ln)
            if not m:
                continue
            (uuids_sel if "[selected]" in ln else uuids_other).append(m.group(1))
        for u in uuids_sel + uuids_other:
            if u in alive:
                return u

    # 3. fallback: most-recent live Claude session, wherever it is
    return alive[0]


VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".webm")  # served with HTTP Range so <video> can seek


def write_contact_sheet(out_path, files):
    """Self-contained printable HTML grid of the selected files (sips -> base64 jpeg for
    rasters/svg, a name placeholder otherwise). Open it and Print -> PDF to share."""
    RASTER = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
    cells = []
    for rel, p in files[:80]:                       # cap: keep the data-URI page reasonable
        ext = os.path.splitext(p)[1].lower()
        name = html.escape(os.path.basename(p))
        thumb = '<div class="ph">' + html.escape(ext.lstrip(".").upper() or "FILE") + '</div>'
        if ext in RASTER:
            with tempfile.TemporaryDirectory(prefix="atelier-contact-") as tmpdir:
                tmp = os.path.join(tmpdir, "preview.jpg")
                try:
                    subprocess.run(["sips", "-Z", "460", "-s", "format", "jpeg", p, "--out", tmp],
                                   capture_output=True, timeout=20)
                    if os.path.isfile(tmp):
                        with open(tmp, "rb") as fh:
                            thumb = '<img src="data:image/jpeg;base64,' + base64.b64encode(fh.read()).decode() + '">'
                except Exception:
                    pass
        cells.append('<figure>' + thumb + '<figcaption>' + name + '</figcaption></figure>')
    doc = ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Contact sheet</title><style>'
           'body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:24px;background:#fff;color:#111}'
           'h1{font-size:15px;font-weight:600;margin:0 0 14px}'
           '.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}'
           'figure{margin:0;border:1px solid #ddd;border-radius:8px;overflow:hidden;break-inside:avoid}'
           'figure img{width:100%;height:165px;object-fit:contain;background:#f6f6f6;display:block}'
           '.ph{height:165px;display:flex;align-items:center;justify-content:center;background:#f0f0f0;color:#999;font-size:13px}'
           'figcaption{font-size:10.5px;padding:6px 8px;word-break:break-all;color:#333}'
           '</style></head><body><h1>Contact sheet — ' + str(len(files)) + ' file(s)</h1>'
           '<div class="grid">' + "".join(cells) + '</div></body></html>')
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)


_REBUILD_LOCK = threading.Lock()
_REBUILD_LAST = [0.0]
_WATCH_EXTS = set(ARTIFACT_EXTENSIONS)
_WATCH_EXCLUDED = set(EXCLUDED_DIRECTORIES)
_WATCH_STATE = {"enabled": os.environ.get("GALLERY_WATCH", "1") != "0",
                "running": False, "lastScan": 0.0, "lastBuild": 0.0,
                "lastChanged": [], "error": ""}


def _artifact_snapshot(root=None):
    """Cheap artifact signature used by the dependency-free local watcher."""
    return artifact_snapshot(root or PROJECT, _WATCH_EXTS, _WATCH_EXCLUDED)


def _launch_gallery_rebuild(reason="change", changed=None):
    """Run one rebuild at a time; return False when a build is already running."""
    if not _REBUILD_LOCK.acquire(blocking=False):
        return False
    _REBUILD_LAST[0] = time.time()
    changed = list(changed or [])[:50]
    def run():
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "build_gallery.py")],
                cwd=PROJECT, env=dict(os.environ, GALLERY_ROOT=PROJECT),
                capture_output=True, text=True, timeout=300, start_new_session=True,
            )
            _WATCH_STATE["lastBuild"] = time.time()
            _WATCH_STATE["lastChanged"] = changed
            _WATCH_STATE["error"] = "" if result.returncode == 0 else (result.stdout or result.stderr or "build failed")[-500:]
        except Exception as error:
            _WATCH_STATE["error"] = str(error)[:500]
        finally:
            _REBUILD_LOCK.release()
    threading.Thread(target=run, daemon=True, name="atelier-gallery-rebuild").start()
    return True


def _start_artifact_watcher(interval=1.5, debounce=1.2):
    """Watch supported artifacts and rebuild after a short quiet period."""
    if not _WATCH_STATE["enabled"] or _WATCH_STATE["running"]:
        return
    _WATCH_STATE["running"] = True
    def watch():
        previous = _artifact_snapshot()
        pending = set()
        changed_at = 0.0
        while True:
            time.sleep(interval)
            try:
                current = _artifact_snapshot()
                _WATCH_STATE["lastScan"] = time.time()
                changed = {key for key in set(previous) | set(current)
                           if previous.get(key) != current.get(key)}
                previous = current
                if changed:
                    pending.update(changed)
                    changed_at = time.time()
                if pending and time.time() - changed_at >= debounce:
                    if _launch_gallery_rebuild("watch", sorted(pending)):
                        pending.clear()
            except Exception as error:
                _WATCH_STATE["error"] = str(error)[:500]
    threading.Thread(target=watch, daemon=True, name="atelier-artifact-watcher").start()

def _index_stale():
    """L'index du projet est-il plus vieux que le template ou le builder ?"""
    idx = os.path.join(PROJECT, "figures_index.html")
    try:
        idx_m = os.path.getmtime(idx)
    except OSError:
        return True
    for src in (os.path.join(ASSETS_DIR, "gallery_template.html"),
                os.path.join(HERE, "build_gallery.py")):
        try:
            if os.path.getmtime(src) > idx_m:
                return True
        except OSError:
            pass
    return False

def _rebuild_if_stale():
    """Reconstruit figures_index.html en arrière-plan si le template/builder a
    changé depuis la génération (auto-fraîcheur : le /rev bump fera recharger
    la page ouverte). Débounce 60 s pour ne pas empiler des builds."""
    now = time.time()
    if not _index_stale() or now - _REBUILD_LAST[0] < 60:
        return
    _launch_gallery_rebuild("source")

# ---- Historique de versions + git (parité avec le serveur Node d'Atelier
# Studio, gallery/server/routes/editors.mjs). Même layout de stockage
# (.fig_thumbs/dv_versions/<md5(realpath)>.json, gzip, .bak) : un projet
# ouvert tour à tour dans Studio et ici partage ses journaux. ----
VERSION_SOURCES = {"user-save", "external-reload", "external-merge",
                   "external-conflict", "restore", "legacy"}
VERSION_STATUSES = {"applied", "pending-conflict"}
VERSION_TEXT_LIMIT = 8 * 1024 * 1024

def _text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _versions_file(p):
    real = os.path.realpath(p)
    return os.path.join(PROJECT, ".fig_thumbs", "dv_versions",
                        hashlib.md5(real.encode("utf-8")).hexdigest() + ".json")

def _empty_version_state(p):
    return {"v": 2, "path": p, "revision": 0, "base": None, "texts": {},
            "interventions": [], "legacySnapshots": [], "current": None}

def _valid_hash(h):
    return isinstance(h, str) and re.fullmatch(r"[a-f0-9]{64}", h) is not None

def _num_ok(v):
    """Parité Number(v) côté Node : null→0 (valide), nombres finis valides,
    chaînes numériques valides — NaN/objets invalides."""
    if v is None or isinstance(v, bool):
        return v is None
    if isinstance(v, (int, float)):
        return v == v and v not in (float("inf"), float("-inf"))
    if isinstance(v, str):
        try:
            float(v)
            return True
        except ValueError:
            return False
    return False

def _validate_version_state(state):
    if (not isinstance(state, dict) or state.get("v") != 2
            or not isinstance(state.get("path"), str)
            or not isinstance(state.get("revision"), int) or state["revision"] < 0
            or not isinstance(state.get("texts"), dict)
            or not isinstance(state.get("interventions"), list)
            or not isinstance(state.get("legacySnapshots"), list)):
        raise ValueError("invalid versions state")
    total = 0
    for h, text in state["texts"].items():
        if not _valid_hash(h) or not isinstance(text, str) or _text_hash(text) != h:
            raise ValueError("invalid text hash")
        total += len(text.encode("utf-8"))
    if total > VERSION_TEXT_LIMIT:
        raise ValueError("versions texts too large")
    def has_text(h):
        return _valid_hash(h) and h in state["texts"]
    base = state.get("base")
    if base and (not has_text(base.get("hash")) or base.get("kind") not in ("git", "session", "legacy")
                 or not isinstance(base.get("sha"), str)
                 or not _num_ok(base.get("ts"))):
        raise ValueError("invalid base")
    cur = state.get("current")
    if cur and (not has_text(cur.get("hash")) or not _num_ok(cur.get("ts"))):
        raise ValueError("invalid current")
    ids = set()
    for item in state["interventions"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                or item["id"] in ids or not has_text(item.get("fromHash")) or not has_text(item.get("toHash"))
                or not _num_ok(item.get("ts"))
                or item.get("source") not in VERSION_SOURCES
                or item.get("status") not in VERSION_STATUSES):
            raise ValueError("invalid intervention")
        ids.add(item["id"])
    for snap in state["legacySnapshots"]:
        if (not isinstance(snap, dict) or not has_text(snap.get("hash"))
                or not _num_ok(snap.get("ts")) or not isinstance(snap.get("label"), str)):
            raise ValueError("invalid legacy snapshot")
    return state

def _migrate_version_v1(data, p):
    allowed = {"v", "path", "items", "last"}
    if (not isinstance(data, dict) or any(k not in allowed for k in data)
            or data.get("v", 1) != 1
            or ("path" in data and not isinstance(data["path"], str))
            or not isinstance(data.get("items"), list)
            or not (isinstance(data.get("last"), str) or data.get("last") is None)
            or any(not isinstance(it, dict) or not isinstance(it.get("b"), str)
                   or ("t" in it and not isinstance(it["t"], (int, float)))
                   or any(k not in ("b", "t") for k in it) for it in data["items"])):
        raise ValueError("invalid versions v1 schema")
    state = _empty_version_state(p)
    snaps = [{"text": it["b"], "ts": it.get("t") or i, "label": "snapshot v1 %d" % (i + 1)}
             for i, it in enumerate(data["items"])]
    if isinstance(data.get("last"), str):
        snaps.append({"text": data["last"], "ts": snaps[-1]["ts"] if snaps else 0, "label": "dernier connu v1"})
    for snap in snaps:
        h = _text_hash(snap["text"]); state["texts"][h] = snap["text"]
        state["legacySnapshots"].append({"hash": h, "ts": snap["ts"], "label": snap["label"]})
    if snaps:
        first = snaps[0]
        state["base"] = {"hash": _text_hash(first["text"]), "kind": "legacy", "sha": "", "ts": first["ts"]}
        for i in range(1, len(snaps)):
            before, after = snaps[i - 1], snaps[i]
            if before["text"] == after["text"]:
                continue
            state["interventions"].append({
                "id": "legacy-%d-%s-%s" % (i, _text_hash(before["text"])[:8], _text_hash(after["text"])[:8]),
                "fromHash": _text_hash(before["text"]), "toHash": _text_hash(after["text"]),
                "ts": after["ts"], "source": "legacy", "status": "applied"})
        last = snaps[-1]
        state["current"] = {"hash": _text_hash(last["text"]), "ts": last["ts"]}
    return _validate_version_state(state)

def _decode_version_file(file, p):
    import gzip
    with open(file, "rb") as f:
        raw = f.read()
    try:
        parsed = json.loads(gzip.decompress(raw).decode("utf-8"))
    except Exception:
        parsed = json.loads(raw.decode("utf-8"))
    if isinstance(parsed, dict) and parsed.get("v") == 2:
        return _validate_version_state(parsed)
    return _migrate_version_v1(parsed, p)

def _read_version_state_result(file, p):
    if not os.path.exists(file):
        try:
            return _decode_version_file(file + ".bak", p), True
        except Exception:
            return _empty_version_state(p), False
    try:
        return _decode_version_file(file, p), False
    except Exception:
        try:
            return _decode_version_file(file + ".bak", p), True
        except Exception:
            raise

def _write_file_atomic(file, data, backup=False):
    d = os.path.dirname(file)
    os.makedirs(d, exist_ok=True)
    nonce = "%d.%d.%s" % (os.getpid(), int(time.time() * 1000), os.urandom(6).hex())
    tmp = os.path.join(d, ".%s.%s.tmp" % (os.path.basename(file), nonce))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if backup and os.path.exists(file):
        bak_tmp = os.path.join(d, ".%s.%s.bak.tmp" % (os.path.basename(file), nonce))
        shutil.copyfile(file, bak_tmp)
        os.replace(bak_tmp, file + ".bak")
    os.replace(tmp, file)

def _add_version_texts(state, texts):
    if not isinstance(texts, dict):
        raise ValueError("invalid texts")
    for h, text in texts.items():
        if not _valid_hash(h) or not isinstance(text, str) or _text_hash(text) != h:
            raise ValueError("invalid text hash")
        state["texts"][h] = text

def _apply_version_ops(current, ops):
    import copy as _copy
    if not isinstance(ops, list) or len(ops) > 500:
        raise ValueError("invalid ops")
    state = _copy.deepcopy(current)
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get("type"), str):
            raise ValueError("invalid op")
        _add_version_texts(state, op.get("texts") or {})
        if op["type"] == "init":
            if state["base"] and json.dumps(state["base"], sort_keys=True) != json.dumps(op.get("base"), sort_keys=True):
                raise ValueError("base-conflict")
            if not state["base"]:
                state["base"] = _copy.deepcopy(op["base"])
            if op.get("current"):
                state["current"] = _copy.deepcopy(op["current"])
            for snap in (op.get("legacySnapshots") or []):
                key = json.dumps([snap.get("hash"), snap.get("ts"), snap.get("label")])
                if not any(json.dumps([it["hash"], it["ts"], it["label"]]) == key
                           for it in state["legacySnapshots"]):
                    state["legacySnapshots"].append(_copy.deepcopy(snap))
        elif op["type"] == "append":
            item = _copy.deepcopy(op.get("intervention"))
            existing = next((it for it in state["interventions"] if it["id"] == (item or {}).get("id")), None)
            if existing and json.dumps(existing, sort_keys=True) != json.dumps(item, sort_keys=True):
                raise ValueError("intervention-id-conflict")
            if not existing:
                state["interventions"].append(item)
            if op.get("current"):
                state["current"] = _copy.deepcopy(op["current"])
        elif op["type"] == "set-current":
            state["current"] = _copy.deepcopy(op["current"])
        else:
            raise ValueError("invalid op type")
    state["interventions"].sort(key=lambda it: (float(it["ts"] or 0), it["id"]))
    refs = set()
    if state["base"]:
        refs.add(state["base"]["hash"])
    if state["current"]:
        refs.add(state["current"]["hash"])
    for it in state["interventions"]:
        refs.add(it["fromHash"]); refs.add(it["toHash"])
    for snap in state["legacySnapshots"]:
        refs.add(snap["hash"])
    for h in list(state["texts"].keys()):
        if h not in refs:
            del state["texts"][h]
    return _validate_version_state(state)

def _git_out(args, cwd):
    """stdout de git, ou None si git absent / code non nul (dégradation silencieuse)."""
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, timeout=10)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None

def _git_root_rel(p):
    """(racine du dépôt, chemin relatif posix) pour un fichier, ou (None, None)."""
    top = _git_out(["rev-parse", "--show-toplevel"], os.path.dirname(p))
    if not top:
        return None, None
    root = top.strip()
    rel = os.path.relpath(p, root).replace(os.sep, "/")
    return root, rel

def _git_base(root):
    """Dernier commit significatif (saute les « auto: … » de session)."""
    out = _git_out(["log", "-100", "--format=%h\t%s"], root)
    if out:
        for line in out.splitlines():
            sha, _, subject = line.partition("\t")
            if sha and not subject.startswith("auto: "):
                return sha
    return "HEAD"

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # webviews (Studio) : ne jamais servir de JS/HTML périmé
        if self.path.endswith((".js", ".html")) or self.path == "/":
            self.send_header("Cache-Control", "no-cache")
        # In Codex mode, project-authored HTML/SVG is untrusted content.  Give it
        # an opaque sandboxed origin so it cannot call the gallery control API.
        clean = self.path.split("?", 1)[0].split("#", 1)[0]
        trusted = clean in ("/", "/figures_index.html")
        if clean.startswith("/.fig_thumbs/"):
            rel = clean[len("/.fig_thumbs/"):]
            candidate = os.path.realpath(os.path.join(ASSETS_DIR, rel))
            assets_root = os.path.realpath(ASSETS_DIR)
            trusted = ((candidate == assets_root or candidate.startswith(assets_root + os.sep)) and
                       os.path.isfile(candidate))
        content_type = mimetypes.guess_type(clean)[0]
        executable_document = content_type in ("text/html", "application/xhtml+xml", "image/svg+xml")
        if not trusted and executable_document:
            self.send_header("Content-Security-Policy",
                             "sandbox allow-scripts allow-forms allow-modals allow-popups")
        super().end_headers()

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=PROJECT, **kw)

    def log_message(self, *a):
        pass

    def _respond(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._respond(200, {})

    def _local_only(self):
        """Reject browser cross-site requests (drive-by CSRF/RCE). The gallery's own
        requests carry a loopback Origin or none; curl sends none. A page on evil.com
        carries Origin: https://evil.com and is refused."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            from urllib.parse import urlparse
            host = urlparse(origin).hostname
        except Exception:
            return False
        return host in ("127.0.0.1", "localhost", "::1")

    def _agent_authorized(self):
        if not CODEX_PREVIEW or not AGENT_TOKEN:
            return False
        supplied = self.headers.get("Authorization") or ""
        return secrets.compare_digest(supplied, "Bearer " + AGENT_TOKEN)

    def _safe_path(self, p):
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.join(PROJECT, p)    # resolve a project-relative path against PROJECT, not the server's CWD
        p = os.path.realpath(p)
        root = os.path.realpath(PROJECT)
        return p if p == root or p.startswith(root + os.sep) else None

    def translate_path(self, path):
        # Viewers « toujours frais » : /.fig_thumbs/<asset> est servi depuis le
        # assets/ de l'install (source) quand le fichier y existe — une mise à
        # jour de cmux-gallery apparaît partout au simple rechargement de page.
        # Les fichiers générés (vignettes, états board/notes) n'existent pas
        # dans assets/ et retombent sur la copie du projet.
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/.fig_thumbs/"):
            rel = clean[len("/.fig_thumbs/"):]
            cand = os.path.realpath(os.path.join(ASSETS_DIR, rel))
            aroot = os.path.realpath(ASSETS_DIR)
            if (cand == aroot or cand.startswith(aroot + os.sep)) and os.path.isfile(cand):
                return cand
        # SimpleHTTPRequestHandler serves symlink targets without bound-checking.
        # Pin static GETs to PROJECT with the same realpath rule as the JSON API,
        # so an in-tree symlink pointing outside the project can't be read.
        full = super().translate_path(path)
        root = os.path.realpath(PROJECT)
        rp = os.path.realpath(full)
        if rp == root or rp.startswith(root + os.sep):
            return full
        return os.path.join(root, "__forbidden_symlink_escape__")  # nonexistent -> 404

    def _serve_file(self, path):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._respond(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _serve_video(self):
        """Serve a video file with HTTP Range support so <video> can stream and seek.
        SimpleHTTPRequestHandler answers every GET with a full 200 body and no
        Accept-Ranges, which most players refuse to scrub (or to play at all)."""
        full = self.translate_path(self.path)  # pinned to PROJECT, symlink-safe
        if not os.path.isfile(full):
            return self._respond(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "video/mp4"
        fsize = os.path.getsize(full)
        start, end, partial = 0, fsize - 1, False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[6:].partition("-")
                if s.strip():
                    start = int(s)
                    end = int(e) if e.strip() else fsize - 1
                else:                                  # suffix range: bytes=-N
                    start = max(0, fsize - int(e))
                if start > end or start >= fsize:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % fsize)
                    self.end_headers()
                    return
                end = min(end, fsize - 1)
                partial = True
            except ValueError:
                start, end, partial = 0, fsize - 1, False
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, fsize))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break                              # player aborted on seek — normal
                remaining -= len(chunk)

    def do_GET(self):
        # Racine -> galerie (au lieu du directory listing), en preservant la query string
        if self.path == "/" or self.path.startswith("/?"):
            self.path = "/figures_index.html" + self.path[1:]
        # PDFs Zotero (mode Studio ou Claude preview) : /zotero/<ITEMKEY>/<fichier>.pdf
        if (STUDIO or CLAUDE_PREVIEW) and self.path.startswith("/zotero/"):
            import re as _re
            from urllib.parse import unquote, urlparse
            parts = urlparse(self.path).path.split("/")
            if len(parts) == 4:
                key, fname = parts[2], unquote(parts[3])
                if _re.fullmatch(r"[A-Za-z0-9]{8}", key) and _re.fullmatch(r"[^/\\]+\.pdf", fname, _re.I):
                    zp = os.path.join(os.path.expanduser("~/Zotero/storage"), key, fname)
                    zroot = os.path.realpath(os.path.expanduser("~/Zotero/storage"))
                    rp = os.path.realpath(zp)
                    if rp.startswith(zroot + os.sep) and os.path.isfile(rp):
                        try:
                            with open(rp, "rb") as f:
                                data = f.read()
                            self.send_response(200)
                            self.send_header("Content-Type", "application/pdf")
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            self.wfile.write(data)
                        except Exception:
                            self._respond(500, {"error": "read error"})
                        return
            return self._respond(404, {"error": "not found"})

        # On-demand downscaled thumbnail for grid cards (keeps full-res images out of
        # the browser: a 4320px plot decodes to ~38MB; its 480px thumb to ~0.5MB).
        # The lightbox still loads the full original, so viewing quality is unchanged.
        if self.path.startswith("/thumb?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                src = self._safe_path(q.get("path", [""])[0])
                if not src or not os.path.isfile(src):
                    return self._respond(404, {"error": "not found"})
                try:
                    w = max(64, min(2000, int(q.get("w", ["480"])[0])))
                except ValueError:
                    w = 480
                key = hashlib.md5((os.path.realpath(src) + ":" + str(int(os.path.getmtime(src))) + ":" + str(w) + (":svg-rsvg" if src.lower().endswith(".svg") else "")).encode()).hexdigest()
                td = os.path.join(PROJECT, ".fig_thumbs")
                os.makedirs(td, exist_ok=True)
                out = os.path.join(td, "imgthumb_" + key + ".png")
                if not os.path.exists(out):
                    if src.lower().endswith(".svg"):
                        # sips/Quick Look explode matplotlib's <use>-glyph text; rsvg renders it faithfully
                        rsvg = shutil.which("rsvg-convert")
                        try:
                            if rsvg:
                                with _THUMB_SEM:
                                    subprocess.run([rsvg, "-w", str(w), "-o", out, src],
                                                   capture_output=True, timeout=20, check=True)
                            else:
                                out = src  # no rsvg -> serve the raw svg (browsers render it correctly)
                        except Exception:
                            out = src
                    elif src.lower().endswith((".html", ".htm")):
                        # render the page with headless Chrome (a real preview), then downscale.
                        # _chrome_html_screenshot caps concurrency + killpg's a hung render so it
                        # can't orphan Chrome's helper processes or collide on the default profile.
                        chrome = next((c for c in (
                            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                            "/Applications/Chromium.app/Contents/MacOS/Chromium") if os.path.isfile(c)),
                            shutil.which("google-chrome") or shutil.which("chromium-browser")
                            or shutil.which("chromium") or shutil.which("chrome"))
                        if not chrome:
                            return self._respond(404, {"error": "no html preview (chrome not found)"})
                        tmp = _chrome_html_screenshot(chrome, src, out)
                        if tmp and os.path.exists(tmp):
                            try:
                                with _THUMB_SEM:
                                    subprocess.run(["sips", "-Z", str(w), "-s", "format", "png", tmp, "--out", out],
                                                   capture_output=True, timeout=15, check=True)
                            except Exception:
                                os.replace(tmp, out)
                            if os.path.exists(tmp):
                                try:
                                    os.remove(tmp)
                                except OSError:
                                    pass
                        if not os.path.exists(out):
                            return self._respond(404, {"error": "html preview failed"})
                    else:
                        try:
                            with _THUMB_SEM:
                                subprocess.run(["sips", "-Z", str(w), "-s", "format", "png", src, "--out", out],
                                               capture_output=True, timeout=20, check=True)
                        except Exception:
                            out = src  # sips missing/failed -> serve the original (correct, just not downscaled)
                return self._serve_file(out)
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/snippet?"):
            # first lines of a text/code file, fetched lazily by visible cards
            # (keeps the snippets out of the embedded gallery data — ~3.8MB lighter).
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                src = self._safe_path(q.get("path", [""])[0])
                if not src or not os.path.isfile(src):
                    return self._respond(404, {"error": "not found"})
                try:
                    n = max(1, min(40, int(q.get("n", ["10"])[0])))
                except ValueError:
                    n = 10
                lines = []
                with open(src, encoding="utf-8", errors="replace") as f:
                    for _ in range(n):
                        ln = f.readline()
                        if not ln:
                            break
                        lines.append(ln.rstrip("\n"))
                body = ("\n".join(lines)[:600]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=300")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/zotero-items"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            try:
                items = zotero_search(query=(q.get("q") or [""])[0],
                                      collection_id=(q.get("collection") or [None])[0] or None)
                return self._respond(200, {"items": items})
            except FileNotFoundError as e:
                return self._respond(200, {"items": [], "error": str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/zotero-collections":
            try:
                return self._respond(200, {"collections": zotero_collections()})
            except FileNotFoundError as e:
                return self._respond(200, {"collections": [], "error": str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith(("/claude-events", "/agent-events")):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            since = int((q.get("since") or ["0"])[0] or 0)
            with _CLAUDE_EVENTS_LOCK:
                evs = [e for e in _CLAUDE_EVENTS if e["id"] > since]
                last = _CLAUDE_EVENTS[-1]["id"] if _CLAUDE_EVENTS else 0
            return self._respond(200, {"events": evs, "last": last})
        if self.path.split("?", 1)[0] == "/agent-status":
            if not self._local_only():
                return self._respond(403, {"error": "loopback origin required"})
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(200, int((q.get("limit") or ["50"])[0])))
            except ValueError:
                limit = 50
            with _AGENT_INBOX_LOCK:
                pending = _read_agent_inbox_unlocked()
                history = _read_agent_json_unlocked(_AGENT_HISTORY_PATH, [])
            return self._respond(200, {
                "ok": True,
                "agentHost": AGENT_HOST or None,
                "consumers": [_public_consumer(item) for item in get_agent_consumers()],
                "pending": [_public_event(item) for item in pending],
                "history": [_public_event(item) for item in history[-limit:]][::-1],
                "counts": {
                    "staged": sum(1 for item in pending if item.get("status") == "staged"),
                    "queued": sum(1 for item in pending if item.get("status") != "staged"),
                },
            })
        if self.path.split("?", 1)[0] == "/agent-selections":
            if not self._local_only() or not self._agent_authorized():
                return self._respond(403, {"error": "agent authorization required"})
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            consumer = (q.get("consumer") or [""])[0].strip()[:200]
            destination = (q.get("destination") or [consumer])[0].strip()[:240]
            if not consumer:
                return self._respond(400, {"error": "consumer is required"})
            items = claim_agent_annotations(consumer, destination=destination)
            return self._respond(200, {"items": items, "count": len(items)})
        if self.path == "/agent-selection":
            if not self._local_only() or not self._agent_authorized():
                return self._respond(403, {"error": "agent authorization required"})
            items = get_agent_annotations()
            return self._respond(200, {
                "ok": True,
                "usage": "POST an annotation here; Codex reads it through the Atelier MCP tool",
                "pending": len(items),
                "latest": items[-1] if items else None,
            })
        if self.path == "/data":
            # figures_data.json brut (parité serveur Node Atelier) : rafraîchissement
            # live du template sans rebuild de figures_index.html.
            dp = os.path.join(PROJECT, "figures_data.json")
            if not os.path.isfile(dp):
                return self._respond(404, {"error": "not found"})
            try:
                with open(dp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._respond(500, {"error": str(e)})
            return
        if self.path.startswith("/ls?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                d = self._safe_path(q.get("dir", [PROJECT])[0]) or PROJECT
                if not os.path.isdir(d):
                    return self._respond(404, {"error": "not a directory"})
                items = []
                for name in sorted(os.listdir(d), key=str.lower):
                    if name.startswith("."):
                        continue
                    p = os.path.join(d, name)
                    items.append({"name": name, "dir": os.path.isdir(p)})
                root = PROJECT
                parent = os.path.dirname(d) if d != root else None
                return self._respond(200, {"path": d, "parent": parent, "items": items})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/texroot?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q["path"][0])
                if not p:
                    return self._respond(403, {"error": "outside the project"})
                root = find_tex_root(p)
                return self._respond(200, {"root": root, "pdf": root.rsplit(".", 1)[0] + ".pdf"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/raw?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q["path"][0])
                if not p or not os.path.isfile(p):
                    self.send_response(404); self.end_headers(); return
                with open(p, "rb") as f:
                    data = f.read()
                self.send_response(200)
                ctype = "application/pdf" if p.endswith(".pdf") else "application/octet-stream"
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(500); self.end_headers()
            return
        if STUDIO and self.path.startswith("/lint?"):
            try:
                from urllib.parse import parse_qs, urlparse
                import shutil as _sh, subprocess as _sp
                q = parse_qs(urlparse(self.path).query)
                p = os.path.realpath(os.path.expanduser(q.get("path", [""])[0]))
                home = os.path.expanduser("~")
                allowed = any(p.startswith(os.path.join(home, d) + os.sep)
                              for d in ("Documents", "Desktop"))
                if not (allowed and p.endswith(".py") and os.path.isfile(p)):
                    return self._respond(200, {"available": False})
                ruff = _sh.which("ruff")
                if not ruff:
                    return self._respond(200, {"available": False})
                try:
                    r = _sp.run([ruff, "check", "--output-format", "json", "--quiet", p],
                                capture_output=True, text=True, timeout=5)
                    diags = json.loads(r.stdout or "[]")
                except Exception:
                    return self._respond(200, {"available": False})
                out = [{"row": d.get("location", {}).get("row", 1),
                        "col": d.get("location", {}).get("column", 1),
                        "code": d.get("code") or "",
                        "message": d.get("message") or ""} for d in diags[:200]]
                return self._respond(200, {"available": True, "diagnostics": out})
            except Exception as e:
                return self._respond(200, {"available": False, "error": str(e)})
        if self.path.startswith("/githead?"):
            # version committée (HEAD/base) d'un fichier suivi — gouttière et
            # pseudo-version « HEAD » du comparateur. ok:false = dégradation douce.
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q.get("path", [""])[0])
                if not p:
                    return self._respond(200, {"ok": False})
                root, rel = _git_root_rel(p)
                if not root:
                    return self._respond(200, {"ok": False})
                base = _git_base(root)
                text = _git_out(["show", "%s:%s" % (base, rel)], root)
                if text is None:
                    return self._respond(200, {"ok": False})
                sha = (_git_out(["rev-parse", "--short", base], root) or "").strip()
                ts = (_git_out(["show", "-s", "--format=%ct", base], root) or "").strip()
                return self._respond(200, {"ok": True, "text": text, "sha": sha,
                                           "ts": int(ts) if ts.isdigit() else 0})
            except Exception:
                return self._respond(200, {"ok": False})
        if self.path.startswith("/versions?"):
            try:
                from urllib.parse import parse_qs, urlparse
                import gzip
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q.get("path", [""])[0])
                if not p:
                    return self._respond(200, {"ok": False})
                file = _versions_file(p)
                state, recovered = _read_version_state_result(file, p)
                if recovered:
                    _write_file_atomic(file, gzip.compress(json.dumps(state).encode("utf-8")))
                elif os.path.exists(file):
                    with open(file, "rb") as f:
                        prefix = f.read(2)
                    if prefix[:2] != b"\x1f\x8b":
                        _write_file_atomic(file, gzip.compress(json.dumps(state).encode("utf-8")), backup=True)
                return self._respond(200, dict({"ok": True}, **state))
            except Exception:
                return self._respond(200, {"ok": False})
        if self.path.startswith("/gitlog?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q.get("path", [""])[0])
                if not p:
                    return self._respond(200, {"ok": False})
                root, rel = _git_root_rel(p)
                if not root:
                    return self._respond(200, {"ok": False})
                out = _git_out(["log", "--follow", "-100", "--format=%h\t%ct\t%s", "--", rel], root)
                if out is None:
                    return self._respond(200, {"ok": False})
                items = []
                for line in out.splitlines():
                    if not line:
                        continue
                    parts = line.split("\t")
                    items.append({"sha": parts[0],
                                  "ts": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
                                  "msg": "\t".join(parts[2:])})
                return self._respond(200, {"ok": True, "items": items})
            except Exception:
                return self._respond(200, {"ok": False})
        if self.path.startswith("/gitshow?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q.get("path", [""])[0])
                sha = q.get("sha", [""])[0]
                if not p or not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha):
                    return self._respond(200, {"ok": False})
                root, rel = _git_root_rel(p)
                if not root:
                    return self._respond(200, {"ok": False})
                text = _git_out(["show", "%s:%s" % (sha, rel)], root)
                if text is None:
                    return self._respond(200, {"ok": False})
                return self._respond(200, {"ok": True, "text": text})
            except Exception:
                return self._respond(200, {"ok": False})
        if self.path.startswith("/code?"):
            try:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                p = self._safe_path(q["path"][0])
                if not p or not os.path.isfile(p):
                    return self._respond(404, {"error": "file not found or outside the project"})
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                return self._respond(200, {"text": text, "mtime": os.path.getmtime(p), "path": p})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/rasterize?"):
            # Full-page PNG of a project .html/.md-rendered file, for the drawn-
            # annotation kit (the client sends its document size). Chrome-rendered,
            # same safety net as /thumb (semaphore + killpg via _chrome_html_screenshot).
            try:
                from urllib.parse import parse_qs, urlparse   # local imports elsewhere in do_GET make these function-locals
                q = parse_qs(urlparse(self.path).query)
                src = self._safe_path(q.get("path", [""])[0])
                if not src or not os.path.isfile(src):
                    return self._respond(404, {"error": "not found"})
                w = max(320, min(2400, int(q.get("w", ["1000"])[0])))
                h = max(200, min(20000, int(q.get("h", ["750"])[0])))
                key = hashlib.md5((os.path.realpath(src) + ":" + str(int(os.path.getmtime(src)))
                                   + f":rast:{w}x{h}").encode()).hexdigest()
                td = os.path.join(PROJECT, ".fig_thumbs")
                os.makedirs(td, exist_ok=True)
                out = os.path.join(td, "rast_" + key + ".png")
                if not os.path.isfile(out):
                    chrome = next((c for c in (
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                        "/Applications/Chromium.app/Contents/MacOS/Chromium") if os.path.exists(c)), None)
                    if not chrome:
                        return self._respond(501, {"error": "no chrome available"})
                    shot = out + ".tmp.png"
                    with _CHROME_SEM:
                        proc = None
                        try:
                            proc = subprocess.Popen(
                                [chrome, "--headless=new", "--hide-scrollbars",
                                 "--screenshot=" + shot, f"--window-size={w},{h}",
                                 "--virtual-time-budget=6000", "file://" + src],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
                            proc.communicate(timeout=30)
                        except subprocess.TimeoutExpired:
                            _kill_pg(proc)
                        except Exception:
                            _kill_pg(proc)
                    if os.path.isfile(shot):
                        os.replace(shot, out)
                    else:
                        return self._respond(500, {"error": "render failed"})
                return self._serve_file(out)
            except (KeyError, ValueError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/notes/load":
            try:
                np_ = os.path.join(PROJECT, "notes.md")
                if os.path.isfile(np_):
                    with open(np_, encoding="utf-8", errors="replace") as f:
                        return self._respond(200, {"markdown": f.read()})
                return self._respond(200, {"markdown": ""})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/claude-targets":
            if NO_PUSH:
                return self._respond(200, {"targets": []})
            try:
                return self._respond(200, {"targets": list_claude_targets()})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/board/load":
            try:
                bp = os.path.join(PROJECT, ".fig_thumbs", "board.tldr.json")
                if os.path.isfile(bp):
                    with open(bp, encoding="utf-8") as f:
                        return self._respond(200, {"snapshot": json.load(f)})
                return self._respond(200, {"snapshot": None})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/board/poll":
            with _BOARD_LOCK:
                cmds, _BOARD_QUEUE[:] = _BOARD_QUEUE[:], []
            return self._respond(200, {"commands": cmds})
        if self.path.startswith("/pdfannot"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            rel = (q.get("rel") or [""])[0]
            store_path = os.path.join(PROJECT, ".fig_thumbs", "pdf_annots.json")
            try:
                with open(store_path) as f:
                    store = json.load(f)
            except Exception:
                store = {}
            return self._respond(200, {"annots": store.get(rel, [])})
        if self.path == "/ping":
                return self._respond(200, {"ok": True, "service": "fig-annotate",
                                       "project": os.path.realpath(PROJECT),
                                       "claudePreview": CLAUDE_PREVIEW,
                                       "agentHost": AGENT_HOST or None,
                                       "agentBridgeProtocol": AGENT_BRIDGE_PROTOCOL,
                                       "agentInbox": len(get_agent_annotations()),
                                       "watcher": dict(_WATCH_STATE)})
        if self.path == "/rev":
            # build revision = mtime of the generated index; bumps on every rescan/rebuild,
            # so the open gallery can auto-reload after Claude edits + rescans
            _rebuild_if_stale()   # auto-fraîcheur : template/builder modifiés -> rebuild arrière-plan
            try:
                idx = os.path.join(PROJECT, "figures_index.html")
                rev = int(os.path.getmtime(idx)) if os.path.exists(idx) else 0
            except Exception:
                rev = 0
            return self._respond(200, {"rev": rev})
        if self.path == "/quote":
            try:
                qf = os.path.expanduser("~/.claude/fig-last-quote.txt")
                pending = os.path.isfile(qf) and "Annotations" in open(qf).read(500) \
                    and (time.time() - os.path.getmtime(qf)) < 900
                return self._respond(200, {"pending": bool(pending)})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/state":
            try:
                sp = os.path.join(PROJECT, ".fig_state.json")
                if os.path.isfile(sp):
                    with open(sp, encoding="utf-8") as f:
                        return self._respond(200, json.load(f))
                return self._respond(200, {"favs": [], "ratings": {}, "hidden": [],
                                           "tags": {}, "hideRules": [],
                                           "collections": {}, "workflow": {}})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/findscript?"):
            try:
                if not self._local_only():
                    return self._respond(403, {"error": "cross-origin blocked"})
                from urllib.parse import parse_qs, urlparse
                stem = (parse_qs(urlparse(self.path).query).get("stem", [""])[0] or "").strip()[:200]
                if not stem:
                    return self._respond(400, {"error": "no stem"})
                hit = None
                try:
                    # "--" stops option parsing (stem can't become an rg flag like --pre=…);
                    # --no-config ignores RIPGREP_CONFIG_PATH. -F keeps it a literal string.
                    r = subprocess.run(["rg", "-l", "--no-messages", "--no-config", "-F",
                                        "-g", "*.{py,r,R,jl,sh,ipynb}", "--", stem, PROJECT],
                                       capture_output=True, text=True, timeout=15)
                    for line in (r.stdout or "").splitlines():
                        ap = os.path.realpath(line.strip())
                        if ap.startswith(PROJECT + os.sep):
                            hit = os.path.relpath(ap, PROJECT)
                            break
                except FileNotFoundError:
                    pass            # ripgrep not installed -> client already tried a stem match
                return self._respond(200, {"script": hit})
            except (KeyError, ValueError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/provenance?"):
            try:
                if not self._local_only():
                    return self._respond(403, {"error": "cross-origin blocked"})
                from urllib.parse import parse_qs, urlparse
                rel = (parse_qs(urlparse(self.path).query).get("rel", [""])[0] or "").strip()
                dp = os.path.join(PROJECT, "figures_data.json")
                with open(dp, encoding="utf-8") as f:
                    data = json.load(f)
                row = next((item for item in data.get("files", []) if item.get("rel") == rel), None)
                if not row:
                    return self._respond(404, {"error": "artifact not found"})
                return self._respond(200, {"ok": True, "rel": rel,
                                           "provenance": row.get("provenance")})
            except (OSError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        from urllib.parse import urlparse as _up
        if os.path.splitext(_up(self.path).path)[1].lower() in VIDEO_EXTS:
            return self._serve_video()
        # Project .html reports: inject the text-selection → Claude overlay
        # (never the gallery index itself, nor the /.fig_thumbs viewers which
        # have their own selection systems).
        _pth = _up(self.path).path
        if (os.path.splitext(_pth)[1].lower() in (".html", ".htm")
                and not _pth.startswith("/.fig_thumbs/")
                and os.path.basename(_pth) != "figures_index.html"):
            from urllib.parse import unquote as _uq
            p = self._safe_path(_uq(_pth).lstrip("/"))
            if p and os.path.isfile(p):
                try:
                    with open(p, "rb") as f:
                        body = f.read()
                    tag = b'<script defer src="/.fig_thumbs/sel_overlay.js?v=3"></script>'
                    i = body.lower().rfind(b"</body>")
                    body = body[:i] + tag + body[i:] if i != -1 else body + tag
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                except OSError:
                    pass
        super().do_GET()

    def do_HEAD(self):
        from urllib.parse import urlparse as _up
        if os.path.splitext(_up(self.path).path)[1].lower() in VIDEO_EXTS:
            return self._serve_video()
        super().do_HEAD()

    def do_POST(self):
        if self.path == "/pdfannot":
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            store_path = os.path.join(PROJECT, ".fig_thumbs", "pdf_annots.json")
            try:
                with open(store_path) as f:
                    store = json.load(f)
            except Exception:
                store = {}
            rel_key = req.get("rel") or ""
            new_annots = req.get("annots") or []
            # filet : si un client vide un rel qui avait des annots, garder une copie
            if not new_annots and store.get(rel_key):
                try:
                    bak = store_path + ".bak"
                    _write_file_atomic(bak, json.dumps(store, ensure_ascii=False).encode("utf-8"))
                except Exception:
                    pass
            store[rel_key] = new_annots
            _write_file_atomic(
                store_path,
                (json.dumps(store, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
            return self._respond(200, {"ok": True})
        if not self._local_only():
            return self._respond(403, {"error": "cross-origin blocked"})
        if self.path == "/agent-consumers/register":
            if not self._agent_authorized():
                return self._respond(403, {"error": "agent authorization required"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                result = register_agent_consumer(
                    req.get("consumer"), req.get("destination"), req.get("label"),
                    req.get("threadId"), req.get("automatic") if "automatic" in req else None,
                    req.get("pid"),
                )
                return self._respond(200, {"ok": True, "destination": result})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        if self.path == "/agent-annotations/status":
            if not self._agent_authorized():
                return self._respond(403, {"error": "agent authorization required"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                ids = req.get("ids")
                if not isinstance(ids, list) or not ids or len(ids) > 100:
                    return self._respond(400, {"error": "ids are required"})
                changed = update_agent_annotation_status(
                    ids, str(req.get("status") or ""), req.get("result") or "",
                    req.get("error") or "",
                )
                return self._respond(200, {"ok": True, "updated": changed})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        if self.path == "/agent-preferences":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                result = set_agent_consumer_preferences(
                    req.get("destination"),
                    req.get("automatic") if "automatic" in req else None,
                    req.get("label"),
                )
                return self._respond(200, {"ok": True, "destination": result})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        if self.path == "/agent-annotations/release":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                ids = req.get("ids") if isinstance(req, dict) else None
                if not isinstance(ids, list) or not ids or len(ids) > 100:
                    return self._respond(400, {"error": "ids are required"})
                released = release_agent_annotations(ids, req.get("destination"))
                return self._respond(200, {"ok": True, "released": len(released),
                                           "ids": [item["id"] for item in released]})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        if self.path == "/agent-annotations/delete":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                ids = req.get("ids") if isinstance(req, dict) else None
                if not isinstance(ids, list) or not ids or len(ids) > 100:
                    return self._respond(400, {"error": "ids are required"})
                deleted = delete_agent_annotations(ids)
                return self._respond(200, {"ok": True, "deleted": len(deleted),
                                           "ids": [item["id"] for item in deleted]})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        if self.path == "/agent-annotations/restore":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                ids = req.get("ids") if isinstance(req, dict) else None
                if not isinstance(ids, list) or not ids or len(ids) > 100:
                    return self._respond(400, {"error": "ids are required"})
                restored = restore_agent_annotations(ids)
                return self._respond(200, {"ok": True, "restored": len(restored),
                                           "ids": [item["id"] for item in restored]})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        if self.path == "/agent-batches/release":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                released = release_agent_batch(req.get("batchId"))
                return self._respond(200, {"ok": True, "released": len(released),
                                           "ids": [item["id"] for item in released]})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        if self.path == "/agent-batches/cancel":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                cancelled = cancel_agent_batch(req.get("batchId"))
                return self._respond(200, {"ok": True, "cancelled": len(cancelled),
                                           "ids": [item["id"] for item in cancelled]})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        if self.path == "/orca-fullscreen-exit":
            try:
                result = orca_fullscreen_exit()
                return self._respond(200 if result.get("ok") else 500, result)
            except Exception as e:
                return self._respond(500, {"ok": False, "error": str(e)})
        if self.path == "/orca-native-fullscreen":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length)) if length > 0 else {}
                rel = req.get("rel") or ""
                p = self._safe_path(rel)
                ext = os.path.splitext(p or "")[1].lower()
                if not p or not os.path.isfile(p) or ext not in NATIVE_FULLSCREEN_EXTS:
                    return self._respond(400, {"ok": False, "error": "not a supported project image"})
                ok, data = launch_native_fullscreen(p)
                data["ok"] = ok
                return self._respond(200 if ok else 500, data)
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"ok": False, "error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"ok": False, "error": str(e)})
        if self.path in ("/board/open-surface", "/notes/open-surface"):
            # Open the whiteboard/notes as a new embedded-browser tab (window.open
            # is swallowed inside embedded surfaces, so the page asks us to do it).
            try:
                if NO_PUSH:
                    # pas de push cmux/muxy/orca (mode Studio / Claude preview) : la page
                    # ouvre l'onglet elle-même (postMessage) ; 500 => fallback lightbox
                    return self._respond(500, {"ok": False, "error": "no-push mode"})
                page = "whiteboard" if self.path.startswith("/board") else "notes"
                url = f"http://127.0.0.1:{PORT}/.fig_thumbs/{page}/index.html"
                host = ""                                           # optional hint from the gallery page
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    if 0 < length <= 4096:
                        host = str(json.loads(self.rfile.read(length)).get("host", ""))
                except Exception:
                    pass
                # Only ever open inside an embedded workspace browser (muxy/orca/cmux).
                # No default-browser fallback: on failure the gallery falls back to
                # its in-page lightbox viewer instead.
                candidates = [
                    (_cmux_exe(), ["browser", "open", url], "cmux"),
                    (shutil.which("muxy"), ["browser", "open", url], "muxy"),
                    (shutil.which("orca") or ("/usr/local/bin/orca" if os.path.exists("/usr/local/bin/orca") else None),
                     ["tab", "create", "--url", url, "--json"], "orca"),
                ]
                # Both apps can run at once — the tab must open in the app hosting
                # the gallery that was clicked, so its hint wins the order.
                candidates.sort(key=lambda c: c[2] != host)
                for exe, args, name in candidates:
                    if not exe:
                        continue
                    r = subprocess.run([exe] + args, capture_output=True, text=True,
                                       timeout=10, env=_cmux_env())
                    if r.returncode != 0:
                        continue
                    if name == "orca":
                        # orca's CLI exits 0 even when the app is closed — trust
                        # its JSON "ok" field instead.
                        try:
                            if not json.loads(r.stdout or "{}").get("ok"):
                                continue
                        except Exception:
                            continue
                    return self._respond(200, {"ok": True, "via": name})
                return self._respond(502, {"error": "no embedded browser available (muxy/orca/cmux)"})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/notes/save":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 16 * 1024 * 1024:        # 16 MB cap
                    return self._respond(413, {"error": "empty or oversized notes"})
                req = json.loads(self.rfile.read(length))
                md = req.get("markdown")
                if not isinstance(md, str):
                    return self._respond(400, {"error": "markdown must be a string"})
                fd, tmp = tempfile.mkstemp(dir=PROJECT, prefix=".notes.", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(md)
                os.replace(tmp, os.path.join(PROJECT, "notes.md"))   # atomic
                return self._respond(200, {"ok": True})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/board/save":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 64 * 1024 * 1024:        # 64 MB cap
                    return self._respond(413, {"error": "empty or oversized snapshot"})
                req = json.loads(self.rfile.read(length))
                snap = req.get("snapshot")
                if not isinstance(snap, dict):
                    return self._respond(400, {"error": "snapshot must be an object"})
                bdir = os.path.join(PROJECT, ".fig_thumbs")
                os.makedirs(bdir, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=bdir, prefix=".board.", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(snap, f, ensure_ascii=False)
                os.replace(tmp, os.path.join(bdir, "board.tldr.json"))  # atomic
                return self._respond(200, {"ok": True})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/board/command":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 8 * 1024 * 1024:
                    return self._respond(413, {"error": "empty or oversized command"})
                cmd = json.loads(self.rfile.read(length))
                if not isinstance(cmd, dict) or not isinstance(cmd.get("type"), str):
                    return self._respond(400, {"error": "command needs a string 'type'"})
                if cmd["type"] == "add_image":
                    rel = str(cmd.get("url") or cmd.get("rel") or "")
                    p = self._safe_path(rel.lstrip("/"))
                    if not p or not os.path.isfile(p):
                        return self._respond(404, {"error": "image not found in project"})
                    cmd["url"] = "/" + os.path.relpath(p, PROJECT).replace(os.sep, "/")
                with _BOARD_LOCK:
                    if len(_BOARD_QUEUE) >= _BOARD_QUEUE_MAX:
                        return self._respond(429, {"error": "board queue full (canvas not open?)"})
                    _BOARD_QUEUE.append(cmd)
                return self._respond(200, {"ok": True, "queued": True})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/clear-quote":
            try:
                open(os.path.expanduser("~/.claude/fig-last-quote.txt"), "w").close()
                return self._respond(200, {"ok": True})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/save-svg":
            # Overwrite an in-project .svg with an edited version (labels moved in the
            # SVG viewer's drag mode). Keeps a one-time pristine .orig.bak alongside it.
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 64 * 1024 * 1024:        # 64 MB cap
                    return self._respond(413, {"error": "empty or oversized svg"})
                req = json.loads(self.rfile.read(length))
                rel = req.get("rel") or req.get("name") or ""
                svg = req.get("svg", "")
                if not isinstance(svg, str) or "<svg" not in svg[:4000]:
                    return self._respond(400, {"error": "not an svg payload"})
                try:                                                # reject malformed: must parse to an EXACT <svg> root
                    from xml.etree import ElementTree as ET         # (ElementTree rejects external entities; 64MB cap bounds expansion)
                    if ET.fromstring(svg).tag.split("}")[-1].lower() != "svg":
                        raise ValueError("root element is not <svg>")
                except Exception as e:
                    return self._respond(400, {"error": "not well-formed svg: " + str(e)[:120]})
                dst = self._safe_path(rel)                          # pin to PROJECT, symlink-safe (final component)
                if not dst or not dst.lower().endswith(".svg") or not os.path.isfile(dst) or os.path.islink(dst):
                    return self._respond(400, {"error": "bad/non-svg/symlink path"})
                # NB: residual parent-directory TOCTOU is out of scope for this localhost, single-user,
                # _local_only tool (an attacker who can swap a dir inside PROJECT mid-request already owns the files).
                ddir = os.path.dirname(dst)
                bak = dst + ".orig.bak"                              # keep the pristine original ONCE
                if not os.path.islink(bak) and not os.path.exists(bak):
                    fd, tb = tempfile.mkstemp(dir=ddir, prefix=".bak.", suffix=".tmp")   # O_EXCL secure temp
                    try:
                        with os.fdopen(fd, "wb") as bf, open(dst, "rb") as sf:
                            shutil.copyfileobj(sf, bf)
                        try:
                            os.link(tb, bak)                        # atomic publish; FileExistsError if another save raced
                        except FileExistsError:
                            pass
                    finally:
                        try:
                            os.unlink(tb)
                        except OSError:
                            pass
                fd, tmp = tempfile.mkstemp(dir=ddir, prefix=".save.", suffix=".tmp")     # secure temp, same dir/fs
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(svg)
                os.replace(tmp, dst)                                 # atomic
                edits = req.get("edits")                             # durable layer: re-applied onto the regenerated SVG
                if isinstance(edits, list):
                    ep = os.path.splitext(dst)[0] + ".edits.json"
                    if edits:
                        fd2, t2 = tempfile.mkstemp(dir=ddir, prefix=".edits.", suffix=".tmp")
                        with os.fdopen(fd2, "w", encoding="utf-8") as f:
                            json.dump({"svg": os.path.basename(dst), "edits": edits}, f, ensure_ascii=False, indent=1)
                        os.replace(t2, ep)
                    elif os.path.exists(ep) and not os.path.islink(ep):
                        os.remove(ep)                                # all edits undone → drop the stale sidecar
                return self._respond(200, {"ok": True, "path": os.path.relpath(dst, PROJECT)})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/export-png":
            # Render the (edited) SVG from the viewer to a sibling .png via rsvg-convert.
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 64 * 1024 * 1024:        # 64 MB cap
                    return self._respond(413, {"error": "empty or oversized svg"})
                req = json.loads(self.rfile.read(length))
                rel = req.get("rel") or req.get("name") or ""
                svg = req.get("svg", "")
                try:
                    dpi = max(72, min(1200, int(req.get("dpi", 300))))
                except (TypeError, ValueError):
                    dpi = 300
                if not isinstance(svg, str) or "<svg" not in svg[:4000]:
                    return self._respond(400, {"error": "not an svg payload"})
                dst = self._safe_path(rel)                           # pin to PROJECT, symlink-safe (final component)
                if not dst or not dst.lower().endswith(".svg") or not os.path.isfile(dst) or os.path.islink(dst):
                    return self._respond(400, {"error": "svg not found / non-svg / symlink"})
                png = dst[:-4] + ".png"                              # re-validate the OUTPUT target too
                if os.path.islink(png) or not self._safe_path(png):  # never follow a same-name .png symlink out of PROJECT
                    return self._respond(400, {"error": "bad png output path"})
                rsvg = shutil.which("rsvg-convert")
                if not rsvg:
                    return self._respond(501, {"error": "rsvg-convert not installed "
                                               "(brew install librsvg / apt install librsvg2-bin)"})
                fd_s, tmp_svg = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".exp.", suffix=".svg")  # O_EXCL secure temps
                fd_p, tmp_png = tempfile.mkstemp(dir=os.path.dirname(png), prefix=".exp.", suffix=".png")
                os.close(fd_p)
                try:
                    with os.fdopen(fd_s, "w", encoding="utf-8") as f:
                        f.write(svg)
                    r = subprocess.run([rsvg, "--dpi-x", str(dpi), "--dpi-y", str(dpi), "-o", tmp_png, tmp_svg],
                                       capture_output=True, text=True, timeout=120)
                    if r.returncode != 0 or os.path.getsize(tmp_png) == 0:
                        return self._respond(500, {"error": "rsvg-convert failed: " + (r.stderr or "")[-300:]})
                    os.replace(tmp_png, png)                         # atomic — never truncates an existing png on failure
                finally:
                    for t in (tmp_svg, tmp_png):
                        try:
                            os.remove(t)
                        except OSError:
                            pass
                return self._respond(200, {"ok": True, "path": os.path.relpath(png, PROJECT), "dpi": dpi})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/state":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                tags_in = req.get("tags", {})
                tags = {}
                if isinstance(tags_in, dict):
                    for k, v in tags_in.items():
                        if isinstance(v, list) and v:
                            clean = sorted({str(t).strip() for t in v if str(t).strip()})[:30]
                            if clean:
                                tags[k] = clean
                rules = sorted({str(r).strip() for r in req.get("hideRules", [])
                                if isinstance(r, str) and str(r).strip()})[:200]
                collections_in = req.get("collections", {})
                collections = {}
                if isinstance(collections_in, dict):
                    for k, v in collections_in.items():
                        name = str(k).strip()[:80]
                        if name and isinstance(v, list):
                            clean = sorted({str(rel) for rel in v if isinstance(rel, str) and str(rel).strip()})[:1000]
                            collections[name] = clean
                workflow_in = req.get("workflow", {})
                workflow = {}
                if isinstance(workflow_in, dict):
                    allowed_status = {"draft", "candidate", "final", "rejected"}
                    workflow = {str(k): str(v) for k, v in workflow_in.items()
                                if isinstance(k, str) and str(v) in allowed_status}
                rin = req.get("ratings", {})
                rin = rin if isinstance(rin, dict) else {}
                _strs = lambda v: sorted({str(x) for x in v}) if isinstance(v, list) else []
                state = {"favs": _strs(req.get("favs", [])),
                         "ratings": {k: v for k, v in rin.items()
                                     if isinstance(v, int) and 1 <= v <= 5},
                         "hidden": _strs(req.get("hidden", [])),
                         "tags": tags,
                         "hideRules": rules,
                         "collections": collections,
                         "workflow": workflow}
                sp = os.path.join(PROJECT, ".fig_state.json")
                tmp = sp + ".tmp." + str(os.getpid()) + "." + str(threading.get_ident())
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=1)
                os.replace(tmp, sp)
                return self._respond(200, {"ok": True,
                                           "favs": len(state["favs"]),
                                           "ratings": len(state["ratings"]),
                                           "hidden": len(state["hidden"])})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/rescan":
            builder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_gallery.py")
            proc = None
            try:
                # Own session so a 300s timeout can killpg the builder AND its
                # children cleanly; qlmanage runs in yet another session inside
                # the builder, so we pkill -f qlmanage too as a safety net.
                proc = subprocess.Popen(
                    [sys.executable, builder],
                    cwd=PROJECT,
                    env=dict(os.environ, GALLERY_ROOT=PROJECT),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True,
                )
                out, _ = proc.communicate(timeout=300)
                rc = proc.returncode
                return self._respond(200, {"ok": rc == 0,
                                           "out": (out or "")[-200:]})
            except subprocess.TimeoutExpired:
                if proc is not None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                # mop up any qlmanage renderers orphaned by the aborted build
                try:
                    subprocess.run(["pkill", "-f", "qlmanage"],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass
                return self._respond(200, {"ok": False, "out": "rescan timed out"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/regenerate":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                rel = req.get("rel") if isinstance(req, dict) else None
                if not isinstance(rel, str):
                    return self._respond(400, {"error": "rel is required"})
                dp = os.path.join(PROJECT, "figures_data.json")
                with open(dp, encoding="utf-8") as f:
                    data = json.load(f)
                row = next((item for item in data.get("files", []) if item.get("rel") == rel), None)
                provenance = row.get("provenance") if isinstance(row, dict) else None
                command = provenance.get("command") if isinstance(provenance, dict) else None
                if not (isinstance(command, list) and 0 < len(command) <= 32 and
                        all(isinstance(arg, str) and 0 < len(arg) <= 2000 for arg in command)):
                    return self._respond(409, {"error": "no declared argv command for this artifact"})
                result = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True,
                                        timeout=900, start_new_session=True)
                output = ((result.stdout or "") + (result.stderr or ""))[-6000:]
                if result.returncode == 0:
                    _launch_gallery_rebuild("regenerate", [rel])
                return self._respond(200, {"ok": result.returncode == 0,
                                           "returncode": result.returncode, "output": output})
            except subprocess.TimeoutExpired:
                return self._respond(408, {"error": "regeneration timed out"})
            except (OSError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": str(e)})
        if self.path == "/delete":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                trash = os.path.expanduser("~/.Trash")
                deleted = []
                for rel in req.get("rels", []):
                    p = os.path.realpath(os.path.join(PROJECT, rel))
                    if not p.startswith(PROJECT + os.sep) or not os.path.isfile(p):
                        continue
                    dest = os.path.join(trash, os.path.basename(p))
                    i = 1
                    while os.path.exists(dest):
                        base, ext = os.path.splitext(os.path.basename(p))
                        dest = os.path.join(trash, f"{base}_{i}{ext}")
                        i += 1
                    os.rename(p, dest)
                    deleted.append(rel)
                if deleted:
                    # Rebuild the index in the background so /rev bumps and every
                    # OTHER open gallery tab auto-reloads without the deleted files
                    # (the deleting tab already updated its own list locally).
                    builder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_gallery.py")
                    try:
                        subprocess.Popen([sys.executable, builder], cwd=PROJECT,
                                         env=dict(os.environ, GALLERY_ROOT=PROJECT),
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
                    except Exception:
                        pass
                return self._respond(200, {"deleted": deleted})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/export":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                mode = req.get("mode", "folder")
                files = []
                for rel in req.get("rels", []):
                    p = os.path.realpath(os.path.join(PROJECT, rel))
                    if (p == PROJECT or p.startswith(PROJECT + os.sep)) and os.path.isfile(p):
                        files.append((rel, p))
                if not files:
                    return self._respond(400, {"error": "no valid files selected"})
                exp = os.path.join(PROJECT, "_gallery_exports")
                os.makedirs(exp, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                if mode == "zip":
                    import zipfile
                    out = os.path.join(exp, "export_" + ts + ".zip")
                    seen = {}
                    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                        for rel, p in files:
                            arc = os.path.basename(p)
                            n = seen.get(arc, 0)
                            seen[arc] = n + 1
                            if n:
                                b, e = os.path.splitext(arc)
                                arc = b + "_" + str(n) + e
                            z.write(p, arc)
                elif mode == "contact":
                    out = os.path.join(exp, "contact_" + ts + ".html")
                    write_contact_sheet(out, files)
                else:
                    out = os.path.join(exp, "export_" + ts)
                    os.makedirs(out, exist_ok=True)
                    for rel, p in files:
                        dest = os.path.join(out, os.path.basename(p))
                        i = 1
                        while os.path.exists(dest):
                            b, e = os.path.splitext(os.path.basename(p))
                            dest = os.path.join(out, b + "_" + str(i) + e)
                            i += 1
                        shutil.copy2(p, dest)
                try:
                    subprocess.run(["open", "-R", out] if os.path.isfile(out) else ["open", out],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass
                return self._respond(200, {"ok": True, "path": os.path.relpath(out, PROJECT), "count": len(files)})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/open":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = os.path.realpath(os.path.join(PROJECT, req["rel"]))
                if p.startswith(PROJECT + os.sep) and os.path.exists(p):
                    subprocess.run(["open", p], timeout=10)
                    return self._respond(200, {"ok": True})
                return self._respond(404, {"error": "not found"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/compile":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = self._safe_path(req["path"])
                if not p:
                    return self._respond(403, {"error": "outside the project"})
                root = find_tex_root(p)
                # -g: force a build even when latexmk thinks everything is up to date —
                # an explicit Compile click must (re)generate the PDF AND its .synctex.gz
                # (old PDFs compiled without -synctex=1 otherwise never gain sync data).
                r = subprocess.run(
                    ["/Library/TeX/texbin/latexmk", "-pdf", "-synctex=1", "-g",
                     "-interaction=nonstopmode", "-halt-on-error",
                     os.path.basename(root)],
                    cwd=os.path.dirname(root), capture_output=True, text=True, timeout=180)
                pdf = root.rsplit(".", 1)[0] + ".pdf"
                ok = r.returncode == 0 and os.path.exists(pdf)
                log = (r.stdout or "") + (r.stderr or "")
                err = ""
                if not ok:
                    lines = [l for l in log.splitlines() if l.startswith("!") or "Error" in l]
                    err = "\n".join(lines[:8]) or log[-1500:]
                return self._respond(200, {"ok": ok, "pdf": pdf if ok else None,
                                           "root": root, "error": err})
            except FileNotFoundError:
                return self._respond(200, {"ok": False,
                                           "error": "latexmk not found at /Library/TeX/texbin/latexmk — install MacTeX or TeX Live"})
            except subprocess.TimeoutExpired:
                return self._respond(200, {"ok": False, "error": "compilation > 180 s"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/synctex":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                tex = self._safe_path(req["tex"])
                pdf = self._safe_path(req["pdf"])
                if not tex or not pdf:
                    return self._respond(403, {"error": "outside the project"})
                if req["dir"] == "view":  # source -> PDF
                    r = subprocess.run(
                        ["/Library/TeX/texbin/synctex", "view",
                         "-i", f"{req['line']}:{req.get('col',1)}:{tex}", "-o", pdf],
                        capture_output=True, text=True, timeout=10)
                    out = {}
                    for ln in r.stdout.splitlines():
                        for k in ("Page:", "x:", "y:"):
                            if ln.startswith(k):
                                out[k[:-1].lower()] = float(ln.split(":")[1])
                    return self._respond(200, out or {"error": "no match"})
                else:  # PDF -> source
                    r = subprocess.run(
                        ["/Library/TeX/texbin/synctex", "edit",
                         "-o", f"{int(req['page'])}:{req['x']}:{req['y']}:{pdf}"],
                        capture_output=True, text=True, timeout=10)
                    out = {}
                    for ln in r.stdout.splitlines():
                        if ln.startswith("Line:"):
                            out["line"] = int(ln.split(":")[1])
                        if ln.startswith("Input:"):
                            out["input"] = ln.split(":", 1)[1]
                    return self._respond(200, out or {"error": "no match"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/versions":
            # Le serveur est l'autorité de révision : ops appliquées seulement si
            # expectedRevision correspond, sinon 409 avec l'état courant.
            try:
                import gzip
                length = int(self.headers.get("Content-Length", 0))
                if length > 8 * 1024 * 1024:
                    return self._respond(400, {"error": "payload too large"})
                req = json.loads(self.rfile.read(length))
                p = self._safe_path(req.get("path") or "")
                if not p:
                    return self._respond(403, {"error": "outside the project"})
                file = _versions_file(p)
                current, _ = _read_version_state_result(file, p)
                if not isinstance(req.get("expectedRevision"), int) or req["expectedRevision"] != current["revision"]:
                    return self._respond(409, {"ok": False, "error": "revision-conflict",
                                               "revision": current["revision"], "state": current})
                nxt = _apply_version_ops(current, req.get("ops"))
                nxt["path"] = p
                nxt["revision"] = current["revision"] + 1
                _validate_version_state(nxt)
                if os.path.exists(file):
                    try:
                        _decode_version_file(file, p)
                    except Exception:
                        os.remove(file)
                _write_file_atomic(file, gzip.compress(json.dumps(nxt).encode("utf-8")), backup=True)
                return self._respond(200, {"ok": True, "revision": nxt["revision"]})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/commitmsg":
            # POST only: generating a message launches Claude and must not be
            # triggerable by a cross-site image/navigation GET.
            if not self._local_only():
                return self._respond(403, {"error": "cross-origin blocked"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = self._safe_path(req.get("path") or "")
                if not p:
                    return self._respond(200, {"ok": False})
                root, rel = _git_root_rel(p)
                if not root:
                    return self._respond(200, {"ok": False})
                base = _git_base(root)
                diff = _git_out(["diff", base, "--", rel], root)
                if not diff or not diff.strip():
                    return self._respond(200, {"ok": False})
                claude = shutil.which("claude")
                if not claude:
                    return self._respond(200, {"ok": False})
                sys_prompt = ("Tu écris des messages de commit git. Réponds UNIQUEMENT avec le "
                              "message : une seule ligne, impérative, concise (max 72 caractères), "
                              "en français, sans guillemets, sans préfixe conventionnel, sans explication.")
                env = dict(os.environ, MAX_THINKING_TOKENS="0")
                result = subprocess.run(
                    [claude, "-p", "--model", "haiku", "--setting-sources", "user",
                     "--system-prompt", sys_prompt,
                     "--disallowedTools", "Bash,Edit,Write,Read,Grep,Glob,Task,WebFetch,WebSearch,NotebookEdit"],
                    input="Fichier : %s\n\nDiff :\n%s" % (rel, diff[:8000]),
                    cwd=root, env=env, capture_output=True, text=True, timeout=20)
                text = result.stdout.strip() if result.returncode == 0 else ""
                if not text:
                    return self._respond(200, {"ok": False})
                msg = re.sub(r"^[\"'`]+|[\"'`]+$", "", text.splitlines()[0])[:100]
                return self._respond(200, {"ok": True, "msg": msg})
            except Exception:
                return self._respond(200, {"ok": False})
        if self.path == "/gitcommit":
            # commit du fichier courant SEUL (jamais -A) — bouton commit de l'éditeur
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = self._safe_path(req.get("path") or "")
                msg = str(req.get("message") or "").strip()
                if not p:
                    return self._respond(403, {"error": "outside the project"})
                if not msg:
                    return self._respond(400, {"error": "message vide"})
                root, rel = _git_root_rel(p)
                if not root:
                    return self._respond(200, {"ok": False, "error": "hors dépôt git"})
                if _git_out(["add", "--", rel], root) is None:
                    return self._respond(200, {"ok": False, "error": "git add a échoué"})
                if _git_out(["commit", "--no-verify", "-m", msg, "--", rel], root) is None:
                    # arbre propre ? l'auto-commit de fond a déjà enregistré — si le
                    # fichier a bougé depuis la base, poser un jalon (commit vide)
                    base = _git_base(root)
                    clean = _git_out(["diff", "--quiet", base, "HEAD", "--", rel], root)
                    if clean is not None:  # exit 0 = identique à la base : rien à committer
                        return self._respond(200, {"ok": False, "error": "git commit a échoué (rien à committer ?)"})
                    return self._respond(200, {"ok": False, "error": "git commit ciblé a échoué"})
                sha = (_git_out(["rev-parse", "--short", "HEAD"], root) or "").strip()
                return self._respond(200, {"ok": True, "sha": sha})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/codesave":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = self._safe_path(req["path"])
                if not p:
                    return self._respond(403, {"error": "outside the project"})
                disk_mtime = os.path.getmtime(p) if os.path.exists(p) else 0
                if req.get("mtime") and abs(disk_mtime - req["mtime"]) > 0.001:
                    return self._respond(409, {"error": "conflit", "mtime": disk_mtime})
                with open(p, "w", encoding="utf-8") as f:
                    f.write(req["text"])
                return self._respond(200, {"mtime": os.path.getmtime(p)})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/selinfo":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                p = os.path.expanduser("~/.claude/fig-selection.json")
                if req.get("lines"):
                    req["ts"] = time.time()
                    with open(p, "w") as f:
                        json.dump(req, f)
                elif os.path.exists(p):
                    os.remove(p)
                return self._respond(200, {"ok": True})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/zotero-fav":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                key = (req.get("key") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9]{8}", key):
                    return self._respond(400, {"error": "bad key"})
                with _ZOTERO_LOCK:
                    favs = _zotero_favs_load()
                    (favs.add if req.get("on") else favs.discard)(key)
                    _zotero_favs_save(favs)
                return self._respond(200, {"key": key, "fav": bool(req.get("on"))})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path.startswith("/zotero-add"):
            try:
                from urllib.parse import urlparse, parse_qs, unquote
                q = parse_qs(urlparse(self.path).query)
                name = os.path.basename(unquote((q.get("name") or ["document.pdf"])[0]))
                if not name.lower().endswith(".pdf"):
                    return self._respond(400, {"error": "PDF only"})
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 200 * 1024 * 1024:
                    return self._respond(400, {"error": "bad size"})
                data = self.rfile.read(length)
                return self._respond(200, zotero_add_pdf(name, data))
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path in ("/claude-event", "/agent-event"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                rel = (req.get("rel") or "").strip().lstrip("/")
                full = self._safe_path(os.path.join(PROJECT, rel))
                if not full or not os.path.isfile(full):
                    return self._respond(404, {"error": "file not found: " + rel})
                rel = os.path.relpath(full, PROJECT)
                with _CLAUDE_EVENTS_LOCK:
                    eid = _CLAUDE_EVENTS_NEXT[0]
                    _CLAUDE_EVENTS_NEXT[0] += 1
                    _CLAUDE_EVENTS.append({"id": eid, "ts": time.time(), "rel": rel,
                                           "note": (req.get("note") or "").strip()[:500],
                                           "row": _claude_event_row(full, rel)})
                    del _CLAUDE_EVENTS[:-100]
                return self._respond(200, {"ok": True, "id": eid})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/agent-selections/ack":
            if not self._agent_authorized():
                return self._respond(403, {"error": "agent authorization required"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                ids = req.get("ids") if isinstance(req, dict) else None
                consumer = req.get("consumer") if isinstance(req, dict) else None
                if (not isinstance(ids, list) or len(ids) > 100 or
                        not isinstance(consumer, str) or not consumer.strip()):
                    return self._respond(400, {"error": "consumer and ids are required"})
                removed = acknowledge_agent_annotations(ids, consumer.strip()[:200])
                return self._respond(200, {"ok": True, "acknowledged": removed})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
        if self.path == "/agent-selection":
            try:
                if not self._agent_authorized():
                    return self._respond(403, {"error": "agent authorization required"})
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 1024 * 1024:
                    return self._respond(400, {"error": "bad size"})
                req = json.loads(self.rfile.read(length))
                if not isinstance(req, dict):
                    return self._respond(400, {"error": "JSON object required"})
                raw_rel = req.get("path") or req.get("rel") or ""
                if not isinstance(raw_rel, str):
                    return self._respond(400, {"error": "path must be a string"})
                requested_path = raw_rel.strip()
                full = self._safe_path(requested_path) if requested_path else None
                if not full or not os.path.isfile(full):
                    return self._respond(404, {"error": "file not found: " + requested_path})
                rel = os.path.relpath(full, PROJECT).replace(os.sep, "/")
                raw_source = req.get("source") or ""
                if not isinstance(raw_source, str):
                    return self._respond(400, {"error": "source must be a string"})
                source = raw_source.strip()
                if source:
                    source_full = self._safe_path(source)
                    source = (os.path.relpath(source_full, PROJECT).replace(os.sep, "/")
                              if source_full and os.path.isfile(source_full) else "")
                raw_type = req.get("type") or "annotation"
                raw_comment = req.get("comment") or ""
                if not isinstance(raw_type, str) or not isinstance(raw_comment, str):
                    return self._respond(400, {"error": "type and comment must be strings"})
                event = enqueue_agent_annotation({
                    "type": raw_type[:80],
                    "path": rel,
                    "source": source or None,
                    "comment": raw_comment.strip()[:10000],
                    "region": req.get("region") if isinstance(req.get("region"), dict) else None,
                    "anchor": normalize_agent_anchor(req, rel),
                    "notes": normalize_agent_notes(req.get("notes")),
                    "requestedDirect": bool(req.get("direct")),
                    **normalize_agent_delivery(req),
                })
                return self._respond(200, {"ok": True, "queuedForAgent": True,
                                           "id": event["id"]})
            except (ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path == "/quote":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 1024 * 1024:
                    return self._respond(400, {"error": "bad size"})
                req = json.loads(self.rfile.read(length))
                if not isinstance(req, dict):
                    return self._respond(400, {"error": "JSON object required"})
                raw_rel = req.get("rel")
                raw_text = req.get("text")
                if not isinstance(raw_rel, str) or not isinstance(raw_text, str):
                    return self._respond(400, {"error": "rel and text must be strings"})
                # Studio viewers receive an absolute project path in their query
                # string.  _safe_path already accepts both absolute and
                # project-relative paths while enforcing the project boundary;
                # stripping the leading slash turned valid absolute paths into
                # nonexistent project-relative ones.
                full = self._safe_path(raw_rel.strip())
                if not full or not os.path.isfile(full):
                    return self._respond(404, {"error": "file not found"})
                rel = os.path.relpath(full, PROJECT).replace(os.sep, "/")
                text = raw_text.strip()[:100000]
                if not text:
                    return self._respond(400, {"error": "text is required"})
                req["rel"] = rel
                req["text"] = text
                pdf = full
                page = req.get("page")
                loc = f" (p.{page})" if page not in (None, "", "html") else ""
                msg = f"{pdf}{loc} : \u00ab {text} \u00bb "
                raw_comment = req.get("comment") or ""
                if not isinstance(raw_comment, str):
                    return self._respond(400, {"error": "comment must be a string"})
                comment = raw_comment.strip()[:10000]
                if comment:
                    msg = msg.rstrip() + f"\nCommentaire : {comment}"
                direct = bool(req.get("direct"))
                agent_event = (enqueue_agent_annotation({
                    "type": "text_annotation",
                    "path": req["rel"],
                    "page": page,
                    "selection": text,
                    "comment": comment,
                    "anchor": normalize_agent_anchor(req, rel),
                    "message": msg,
                    "requestedDirect": direct,
                    **normalize_agent_delivery(req),
                }) if CODEX_PREVIEW else None)
                # Composer line kept short: the full payload lives in
                # ~/.claude/fig-last-quote.txt, which the annotation skill reads.
                short = (f"✏️ Regarde mon annotation ({os.path.basename(req['rel'])}{loc}"
                         + (", avec commentaire" if comment else "") + ") et agis en conséquence.")
                if not (STUDIO or CODEX_PREVIEW):
                    subprocess.run("pbcopy", input=msg.encode(), timeout=5)
                    with open(os.path.expanduser("~/.claude/fig-last-quote.txt"), "w") as f:
                        f.write(msg)
                if req.get("embed") or STUDIO:
                    # Embarqué (iframe) : le client livre le message au composer via
                    # postMessage (Atelier) ; en Claude preview le presse-papier +
                    # ~/.claude/fig-last-quote.txt sont déjà remplis ci-dessus.
                    return self._respond(200, {"embedded": True, "message": msg,
                                               "claudePreview": CLAUDE_PREVIEW,
                                               "agentHost": AGENT_HOST or None,
                                               "queuedForAgent": bool(agent_event),
                                               "agentSelectionId": agent_event["id"] if agent_event else None,
                                               "agentSelectionStatus": agent_event.get("status") if agent_event else None})
                sent = False
                tgt = req.get("target")
                if isinstance(tgt, dict):
                    sent = send_to_target(tgt, short, direct)
                ref = None if sent else find_claude_surface()
                if ref:
                    r = subprocess.run(["cmux", "send", "--surface", ref, short], env=_cmux_env(),
                                       capture_output=True, timeout=5)
                    sent = r.returncode == 0
                    if sent and direct:
                        time.sleep(0.4)   # let the composer settle before submitting
                        subprocess.run(["cmux", "send-key", "--surface", ref, "enter"], env=_cmux_env(),
                                       capture_output=True, timeout=5)
                if not sent:
                    pane = find_muxy_claude_pane()
                    if pane:
                        r = subprocess.run(["muxy", "send", "--pane", pane, _oneline(short)],
                                           capture_output=True, timeout=5)
                        sent = r.returncode == 0
                        if sent and direct:
                            time.sleep(0.4)
                            subprocess.run(["muxy", "send-keys", "--pane", pane, "Enter"],
                                           capture_output=True, timeout=5)
                if not sent:
                    term = find_orca_claude_terminal()
                    if term:
                        args = ["orca", "terminal", "send", "--terminal", term, "--text", short]
                        if direct:
                            args.append("--enter")
                        r = subprocess.run(args, capture_output=True, timeout=5)
                        sent = r.returncode == 0
                return self._respond(200, {"sentToClaude": sent,
                                           "clipboard": not CODEX_PREVIEW,
                                           "submitted": sent and direct,
                                           "agentHost": AGENT_HOST or None,
                                           "queuedForAgent": bool(agent_event),
                                           "agentSelectionId": agent_event["id"] if agent_event else None,
                                           "agentSelectionStatus": agent_event.get("status") if agent_event else None})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._respond(400, {"error": "bad request: " + str(e)})
            except Exception as e:
                return self._respond(500, {"error": str(e)})
        if self.path != "/save":
            return self._respond(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 64 * 1024 * 1024:
                return self._respond(400, {"error": "bad size"})
            req = json.loads(self.rfile.read(length))
            if not isinstance(req, dict) or not isinstance(req.get("name"), str) or \
                    not isinstance(req.get("dataURL"), str):
                return self._respond(400, {"error": "name and dataURL are required"})
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(req["name"])[0])
            encoded = req["dataURL"].split(",", 1)
            if len(encoded) != 2 or not encoded[0].startswith("data:image/png;base64"):
                return self._respond(400, {"error": "PNG dataURL required"})
            raw = base64.b64decode(encoded[1], validate=True)  # decode FIRST: a bad dataURL must not leave a 0-byte orphan
            os.makedirs(OUT_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(OUT_DIR, f"{name}_annot_{stamp}.png")
            with open(path, "wb") as f:
                f.write(raw)

            notes = normalize_agent_notes(req.get("notes"))
            direct = bool(req.get("direct"))
            msg = path
            if notes:
                lignes = "\n".join(f"{n['n']}. {n['text']}" for n in notes)
                msg = f"{path}\nAnnotations (badges numerotes sur l'image) :\n{lignes}"
            if direct:
                # Direct send: self-contained actionable prompt, auto-submitted below —
                # no need to invoke the corrige-figure skill afterwards.
                msg += ("\nApplique directement ces annotations : retrouve le script qui genere "
                        "cette figure, fais les corrections demandees et regenere la figure.")

            rel_path = os.path.relpath(path, PROJECT).replace(os.sep, "/")
            agent_event = (enqueue_agent_annotation({
                "type": "image_annotation",
                "path": rel_path,
                "original": req.get("name"),
                "notes": notes,
                "anchor": {"kind": "image-region", "x": 0, "y": 0,
                           "width": 1, "height": 1},
                "message": msg,
                "requestedDirect": direct,
                **normalize_agent_delivery(req),
            }) if CODEX_PREVIEW else None)

            if not (STUDIO or CODEX_PREVIEW):
                subprocess.run("pbcopy", input=msg.encode(), timeout=5)
                with open(os.path.expanduser("~/.claude/fig-last-quote.txt"), "w") as f:
                    f.write(msg)
            if req.get("embed") or STUDIO:
                return self._respond(200, {"embedded": True, "message": msg,
                                           "claudePreview": CLAUDE_PREVIEW,
                                           "agentHost": AGENT_HOST or None,
                                           "queuedForAgent": bool(agent_event),
                                           "agentSelectionId": agent_event["id"] if agent_event else None,
                                           "agentSelectionStatus": agent_event.get("status") if agent_event else None,
                                           "path": path})
            # Composer line kept short: the full payload (path + numbered notes +
            # instruction) lives in fig-last-quote.txt, which the annotation skill reads.
            nb = len(notes)
            short = (f"✏️ Regarde mon annotation ({os.path.basename(path)}, "
                     f"{nb} note{'s' if nb > 1 else ''})"
                     + (" et applique-la." if direct else "."))

            # cmux/muxy/orca push in the background: the response returns immediately
            tgt = req.get("target")
            def push():
                try:
                    if isinstance(tgt, dict):
                        ok = send_to_target(tgt, short, direct)
                        if ok:
                            return
                    ref = find_claude_surface()
                    if ref:
                        r = subprocess.run(["cmux", "send", "--surface", ref, short + " "], env=_cmux_env(),
                                           capture_output=True, timeout=5, start_new_session=True)
                        if r.returncode == 0:                     # cmux may be dead even when the
                            if direct:                            # registry lists live Claude PIDs
                                time.sleep(0.4)   # let the composer settle before submitting
                                subprocess.run(["cmux", "send-key", "--surface", ref, "enter"], env=_cmux_env(),
                                               capture_output=True, timeout=5, start_new_session=True)
                            return
                    pane = find_muxy_claude_pane()
                    if pane:
                        r = subprocess.run(["muxy", "send", "--pane", pane, short],
                                           capture_output=True, timeout=5, start_new_session=True)
                        if r.returncode == 0:
                            if direct:
                                time.sleep(0.4)
                                subprocess.run(["muxy", "send-keys", "--pane", pane, "Enter"],
                                               capture_output=True, timeout=5, start_new_session=True)
                            return
                    term = find_orca_claude_terminal()
                    if term:
                        args = ["orca", "terminal", "send", "--terminal", term, "--text", short]
                        if direct:
                            args.append("--enter")
                        subprocess.run(args, capture_output=True, timeout=8, start_new_session=True)
                except Exception:
                    pass
            threading.Thread(target=push, daemon=True).start()

            self._respond(200, {"path": path,
                                "sentToClaude": False if CODEX_PREVIEW else True,
                                "clipboard": not CODEX_PREVIEW,
                                "submitted": False if CODEX_PREVIEW else direct,
                                "agentHost": AGENT_HOST or None,
                                "queuedForAgent": bool(agent_event),
                                "agentSelectionId": agent_event["id"] if agent_event else None,
                                "agentSelectionStatus": agent_event.get("status") if agent_event else None})
        except Exception as e:
            self._respond(500, {"error": str(e)})


if __name__ == "__main__":
    _rebuild_if_stale()   # serveur relancé après une mise à jour de cmux-gallery -> index régénéré
    _start_artifact_watcher()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
