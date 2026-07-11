import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "atelier_mcp", ROOT / "integrations/codex/atelier_mcp.py"
)
atelier_mcp = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(atelier_mcp)


class RustBridgeTests(unittest.TestCase):
    def test_rust_backend_command_is_explicit(self):
        with patch.dict(os.environ, {"ATELIER_BACKEND": "rust"}):
            command = atelier_mcp._server_command(str(ROOT), 9360)
        self.assertTrue(command[0].endswith("atelier-server"))
        self.assertIn("--watch", command)

    def test_rust_is_the_default_command(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATELIER_BACKEND", None)
            command = atelier_mcp._server_command(str(ROOT), 9360)
        self.assertTrue(command[0].endswith("atelier-server"))
        self.assertIn("--watch", command)

    def test_python_can_be_forced(self):
        with patch.dict(os.environ, {"ATELIER_BACKEND": "python"}):
            command = atelier_mcp._server_command(str(ROOT), 9360)
        self.assertEqual(command[1], "codex-serve")

    def test_rust_mode_can_build_without_installed_cli(self):
        with patch.dict(os.environ, {"ATELIER_BACKEND": "rust"}), \
             patch.object(atelier_mcp, "ATELIER", None):
            command = atelier_mcp._build_command(str(ROOT))
        self.assertEqual(command[:2], [atelier_mcp.sys.executable, str(ROOT / "cmux_gallery.py")])
        self.assertEqual(command[2:4], ["build", "--root"])
        self.assertEqual(command[4], str(ROOT))


if __name__ == "__main__":
    unittest.main()
