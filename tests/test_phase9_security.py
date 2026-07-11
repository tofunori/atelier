import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "rust/target/debug/atelier-server"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Phase9SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(
            ["cargo", "build", "--manifest-path", str(ROOT / "rust/Cargo.toml"),
             "-p", "atelier-server"],
            cwd=ROOT,
            check=True,
        )

    def setUp(self):
        if not SERVER.is_file():
            self.skipTest("atelier-server debug binary absent")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "evil.html").write_text(
            '<script>fetch("/delete", {method:"POST"})</script>', encoding="utf-8"
        )
        (self.root / "secret.txt").write_text("secret", encoding="utf-8")
        self.port = free_port()
        env = dict(
            os.environ,
            ATELIER_ALLOW_REMOTE="1",
            ATELIER_AGENT_TOKEN="phase9-test-token",
            ATELIER_TOOL_ROOT=str(ROOT),
        )
        self.proc = subprocess.Popen(
            [str(SERVER), "--root", str(self.root), "--host", "0.0.0.0",
             "--port", str(self.port), "--no-watch"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(50):
            try:
                self.request("/health", authorized=True)
                break
            except (OSError, urllib.error.HTTPError):
                time.sleep(0.1)
        else:
            self.fail("Rust server did not start")

    def tearDown(self):
        if hasattr(self, "proc"):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            if self.proc.stderr:
                self.proc.stderr.close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def request(self, path, *, authorized=False, body=None):
        headers = {"Authorization": "Bearer phase9-test-token"} if authorized else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers=headers, data=data
        )
        return urllib.request.urlopen(request, timeout=2)

    def test_remote_reads_require_bearer_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/raw?path=secret.txt")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        with self.request("/raw?path=secret.txt", authorized=True) as reply:
            self.assertEqual(reply.read(), b"secret")

    def test_project_html_is_always_sandboxed(self):
        with self.request("/evil.html", authorized=True) as reply:
            self.assertEqual(
                reply.headers.get("Content-Security-Policy"),
                "sandbox allow-scripts allow-forms allow-modals allow-popups",
            )

    def test_gitcommit_never_commits_unrelated_staged_files(self):
        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=self.root, check=True,
                capture_output=True, text=True,
            ).stdout

        git("init", "-q")
        git("config", "user.email", "phase9@example.invalid")
        git("config", "user.name", "Phase 9 Test")
        (self.root / "allowed.txt").write_text("base\n", encoding="utf-8")
        (self.root / "unrelated.txt").write_text("base\n", encoding="utf-8")
        git("add", "allowed.txt", "unrelated.txt")
        git("commit", "-qm", "base")
        (self.root / "allowed.txt").write_text("allowed change\n", encoding="utf-8")
        (self.root / "unrelated.txt").write_text("unrelated change\n", encoding="utf-8")
        git("add", "unrelated.txt")

        with self.request(
            "/gitcommit", authorized=True,
            body={"path": "allowed.txt", "message": "Commit allowed only"},
        ) as reply:
            payload = json.load(reply)
        self.assertTrue(payload.get("ok"), payload)
        names = git("show", "--format=", "--name-only", "HEAD").splitlines()
        self.assertEqual(names, ["allowed.txt"])
        self.assertIn("unrelated.txt", git("diff", "--cached", "--name-only").splitlines())


if __name__ == "__main__":
    unittest.main()
