"""Codex/agent bridge endpoints and persistent annotation inbox."""
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-agent-token"
CONSUMER = "test-codex-task"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lK3QWQAAAABJRU5ErkJggg=="
)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _inbox_path(root):
    key = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:24]
    return Path.home() / "Library" / "Application Support" / "Atelier" / "agent-inbox" / f"{key}.json"


def _agent_state_path(root, suffix):
    key = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:24]
    return (Path.home() / "Library" / "Application Support" / "Atelier" /
            "agent-inbox" / f"{key}-{suffix}.json")


def _request(port, path, payload=None, token=None, origin=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        finally:
            error.close()


class AgentBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        Path(cls.tmp.name, "figure.png").write_bytes(PNG)
        Path(cls.tmp.name, "report.html").write_text("<script>fetch('/agent-selections')</script>")
        Path(cls.tmp.name, ".fig_thumbs").mkdir()
        Path(cls.tmp.name, ".fig_thumbs", "evil.htm").write_text("<script>fetch('/save')</script>")
        env = dict(
            os.environ,
            GALLERY_ROOT=cls.tmp.name,
            FIG_PORT=str(cls.port),
            ATELIER_AGENT_HOST="codex",
            ATELIER_AGENT_TOKEN=TOKEN,
            GALLERY_NO_THUMBS="1",
        )
        cls.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "fig_annotate_server.py")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            try:
                _request(cls.port, "/ping")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        _inbox_path(cls.tmp.name).unlink(missing_ok=True)
        _agent_state_path(cls.tmp.name, "history").unlink(missing_ok=True)
        _agent_state_path(cls.tmp.name, "consumers").unlink(missing_ok=True)
        cls.tmp.cleanup()

    def setUp(self):
        _, inbox = _request(self.port, f"/agent-selections?consumer={CONSUMER}", token=TOKEN)
        if inbox["items"]:
            _request(self.port, "/agent-selections/ack",
                     {"ids": [item["id"] for item in inbox["items"]], "consumer": CONSUMER},
                     token=TOKEN)

    def test_manual_annotation_round_trip_and_acknowledge(self):
        code, result = _request(
            self.port,
            "/agent-selection",
            {"path": "figure.png", "comment": "Déplacer la légende", "direct": True},
            token=TOKEN,
        )
        self.assertEqual(code, 200)
        self.assertTrue(result["queuedForAgent"])

        _, status = _request(self.port, "/agent-selection", token=TOKEN)
        self.assertEqual(status["pending"], 1)
        self.assertEqual(status["latest"]["comment"], "Déplacer la légende")

        _, repeated = _request(self.port, f"/agent-selections?consumer={CONSUMER}", token=TOKEN)
        self.assertEqual(repeated["count"], 1)
        _, wrong_consumer = _request(
            self.port, "/agent-selections/ack",
            {"ids": [result["id"]], "consumer": "different-task"}, token=TOKEN,
        )
        self.assertEqual(wrong_consumer["acknowledged"], 0)
        _, acknowledged = _request(self.port, "/agent-selections/ack",
                                   {"ids": [result["id"]], "consumer": CONSUMER}, token=TOKEN)
        self.assertEqual(acknowledged["acknowledged"], 1)
        _, empty = _request(self.port, f"/agent-selections?consumer={CONSUMER}", token=TOKEN)
        self.assertEqual(empty["items"], [])

    def test_image_save_is_queued_for_codex(self):
        data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        code, result = _request(
            self.port,
            "/save",
            {"name": "figure.png", "dataURL": data_url, "notes": [{"n": 1, "text": "Titre trop bas"}]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(result["agentHost"], "codex")
        self.assertTrue(result["queuedForAgent"])
        self.assertFalse(result["sentToClaude"])

        _, inbox = _request(self.port, f"/agent-selections?consumer={CONSUMER}", token=TOKEN)
        self.assertEqual(inbox["count"], 1)
        event = inbox["items"][0]
        self.assertEqual(event["type"], "image_annotation")
        self.assertEqual(event["notes"], [{"n": 1, "text": "Titre trop bas"}])
        self.assertTrue(event["path"].startswith("annotations/figure_annot_"))

    def test_agent_event_alias_refreshes_gallery(self):
        code, result = _request(self.port, "/agent-event", {"rel": "figure.png", "note": "Régénérée"})
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        _, events = _request(self.port, "/agent-events?since=0")
        self.assertEqual(events["events"][-1]["rel"], "figure.png")
        self.assertEqual(events["events"][-1]["note"], "Régénérée")

    def test_manual_annotation_rejects_paths_outside_project(self):
        code, _ = _request(self.port, "/agent-selection", {"path": "../outside.png"}, token=TOKEN)
        self.assertEqual(code, 404)
        code, _ = _request(self.port, "/quote", {
            "rel": "../outside.tex", "text": "escape"
        }, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code, 404)

    def test_quote_accepts_absolute_path_from_studio_viewer(self):
        absolute = str(Path(self.tmp.name, "figure.png"))
        code, result = _request(self.port, "/quote", {
            "rel": absolute,
            "page": "L1-1",
            "text": "pixel",
            "embed": True,
        }, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code, 200)
        self.assertTrue(result["queuedForAgent"])

        _, inbox = _request(self.port, f"/agent-selections?consumer={CONSUMER}", token=TOKEN)
        self.assertEqual(inbox["count"], 1)
        self.assertEqual(inbox["items"][0]["path"], "figure.png")
        self.assertEqual(inbox["items"][0]["selection"], "pixel")

    def test_image_save_rejects_non_png_payload(self):
        code, _ = _request(self.port, "/save", {
            "name": "figure.png", "dataURL": "data:text/html;base64,PHNjcmlwdD4="
        }, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code, 400)

    def test_agent_inbox_requires_mcp_token(self):
        code, _ = _request(self.port, "/agent-selections")
        self.assertEqual(code, 403)

    def test_destination_batch_history_and_status_lifecycle(self):
        destination = "thread:00000000-0000-0000-0000-000000000001"
        code, registered = _request(self.port, "/agent-consumers/register", {
            "consumer": CONSUMER,
            "destination": destination,
            "label": "LaTeX review",
            "threadId": destination.split(":", 1)[1],
            "automatic": False,
        }, token=TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual(registered["destination"]["label"], "LaTeX review")

        code, staged = _request(self.port, "/quote", {
            "rel": "figure.png",
            "page": "L12-12",
            "text": "remplir",
            "comment": "Pourquoi ce mot?",
            "destination": destination,
            "action": "ask",
            "batchId": "batch-test",
            "held": True,
        }, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code, 200)
        self.assertEqual(staged["agentSelectionStatus"], "staged")

        _, empty = _request(
            self.port,
            f"/agent-selections?consumer={CONSUMER}&destination={destination}",
            token=TOKEN,
        )
        self.assertEqual(empty["items"], [])
        _, status = _request(self.port, "/agent-status?limit=20")
        self.assertEqual(status["counts"]["staged"], 1)
        self.assertEqual(status["pending"][0]["action"], "ask")
        self.assertEqual(status["pending"][0]["destination"], destination)

        _, released = _request(self.port, "/agent-batches/release", {"batchId": "batch-test"})
        self.assertEqual(released["released"], 1)
        _, claimed = _request(
            self.port,
            f"/agent-selections?consumer={CONSUMER}&destination={destination}",
            token=TOKEN,
        )
        self.assertEqual(claimed["count"], 1)
        event = claimed["items"][0]
        self.assertEqual(event["selection"], "remplir")
        self.assertEqual(event["comment"], "Pourquoi ce mot?")
        self.assertEqual(event["anchor"], {
            "kind": "text-range", "startLine": 12, "endLine": 12
        })

        _, processing = _request(self.port, "/agent-annotations/status", {
            "ids": [event["id"]], "status": "processing"
        }, token=TOKEN)
        self.assertEqual(processing["updated"], 1)
        _request(self.port, "/agent-selections/ack", {
            "ids": [event["id"]], "consumer": CONSUMER
        }, token=TOKEN)
        _, completed = _request(self.port, "/agent-annotations/status", {
            "ids": [event["id"]], "status": "completed", "result": "Réponse fournie"
        }, token=TOKEN)
        self.assertEqual(completed["updated"], 1)
        _, final = _request(self.port, "/agent-status?limit=20")
        saved = next(item for item in final["history"] if item["id"] == event["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["result"], "Réponse fournie")

    def test_targeted_annotation_is_not_claimed_by_another_task(self):
        target = "thread:00000000-0000-0000-0000-000000000002"
        _request(self.port, "/agent-consumers/register", {
            "consumer": "consumer-b", "destination": target, "label": "Task B",
            "threadId": target.split(":", 1)[1]
        }, token=TOKEN)
        _, queued = _request(self.port, "/agent-selection", {
            "path": "figure.png", "comment": "Only B", "destination": target
        }, token=TOKEN)
        _, wrong = _request(
            self.port,
            f"/agent-selections?consumer={CONSUMER}&destination=thread%3Aother",
            token=TOKEN,
        )
        self.assertEqual(wrong["items"], [])
        _, right = _request(
            self.port,
            f"/agent-selections?consumer=consumer-b&destination={target}",
            token=TOKEN,
        )
        self.assertEqual([item["id"] for item in right["items"]], [queued["id"]])

    def test_staged_batch_can_be_cancelled_without_leaving_pending_work(self):
        _, staged = _request(self.port, "/quote", {
            "rel": "figure.png", "text": "cancel me", "batchId": "batch-cancel",
            "held": True, "action": "ask"
        }, origin=f"http://127.0.0.1:{self.port}")
        _, cancelled = _request(self.port, "/agent-batches/cancel", {
            "batchId": "batch-cancel"
        }, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(cancelled["cancelled"], 1)
        _, status = _request(self.port, "/agent-status?limit=20")
        self.assertFalse(any(item["id"] == staged["agentSelectionId"]
                             for item in status["pending"]))
        saved = next(item for item in status["history"]
                     if item["id"] == staged["agentSelectionId"])
        self.assertEqual(saved["status"], "cancelled")
        code, _ = _request(self.port, "/agent-selections?consume=1",
                           origin="https://example.invalid")
        self.assertEqual(code, 403)

    def test_annotation_bank_supports_individual_send_and_delete(self):
        destination = "thread:00000000-0000-0000-0000-000000000003"
        _request(self.port, "/agent-consumers/register", {
            "consumer": "consumer-bank", "destination": destination,
            "label": "Current task", "threadId": destination.split(":", 1)[1]
        }, token=TOKEN)
        _, first = _request(self.port, "/quote", {
            "rel": "figure.png", "page": "L3-3", "text": "first", "held": True
        }, origin=f"http://127.0.0.1:{self.port}")
        _, second = _request(self.port, "/quote", {
            "rel": "figure.png", "page": "L8-8", "text": "second", "held": True
        }, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(first["agentSelectionStatus"], "staged")
        self.assertEqual(second["agentSelectionStatus"], "staged")

        _, before = _request(
            self.port,
            f"/agent-selections?consumer=consumer-bank&destination={destination}",
            token=TOKEN,
        )
        self.assertEqual(before["items"], [])

        _, sent = _request(self.port, "/agent-annotations/release", {
            "ids": [first["agentSelectionId"]], "destination": destination
        }, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(sent["released"], 1)
        _, claimed = _request(
            self.port,
            f"/agent-selections?consumer=consumer-bank&destination={destination}",
            token=TOKEN,
        )
        self.assertEqual([item["selection"] for item in claimed["items"]], ["first"])
        _request(self.port, "/agent-selections/ack", {
            "ids": [first["agentSelectionId"]], "consumer": "consumer-bank"
        }, token=TOKEN)

        _, deleted = _request(self.port, "/agent-annotations/delete", {
            "ids": [second["agentSelectionId"]]
        }, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(deleted["deleted"], 1)
        _, status = _request(self.port, "/agent-status?limit=20")
        self.assertFalse(any(item["id"] == second["agentSelectionId"]
                             for item in status["pending"]))
        saved = next(item for item in status["history"]
                     if item["id"] == second["agentSelectionId"])
        self.assertEqual(saved["status"], "cancelled")

        _, restored = _request(self.port, "/agent-annotations/restore", {
            "ids": [second["agentSelectionId"]]
        }, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(restored["restored"], 1)
        _, after_restore = _request(self.port, "/agent-status?limit=20")
        restored_item = next(item for item in after_restore["pending"]
                             if item.get("restoredFrom") == second["agentSelectionId"])
        self.assertEqual(restored_item["selection"], "second")
        self.assertEqual(restored_item["anchor"]["startLine"], 8)
        _request(self.port, "/agent-annotations/delete", {
            "ids": [restored_item["id"]]
        }, origin=f"http://127.0.0.1:{self.port}")

    def test_project_html_is_sandboxed_in_codex_mode(self):
        for rel in ("report.html", ".fig_thumbs/evil.htm"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/{rel}", timeout=5
            ) as response:
                policy = response.headers.get("Content-Security-Policy")
            self.assertIn("sandbox", policy)
            self.assertNotIn("allow-same-origin", policy)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/.fig_thumbs/md_viewer.html", timeout=5
        ) as response:
            self.assertIsNone(response.headers.get("Content-Security-Policy"))


class NonCodexBridgeTests(unittest.TestCase):
    def test_regular_gallery_does_not_leave_stale_codex_work(self):
        with tempfile.TemporaryDirectory() as root:
            port = _free_port()
            env = dict(os.environ, GALLERY_ROOT=root, FIG_PORT=str(port),
                       GALLERY_NO_THUMBS="1", ATELIER_STUDIO="1")
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "fig_annotate_server.py")],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    try:
                        _request(port, "/ping")
                        break
                    except Exception:
                        time.sleep(0.1)
                data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
                code, result = _request(port, "/save", {
                    "name": "figure.png", "dataURL": data_url, "notes": []
                })
                self.assertEqual(code, 200)
                self.assertFalse(result["queuedForAgent"])
                self.assertFalse(_inbox_path(root).exists())
            finally:
                proc.terminate()
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
