"""Fullscreen contract for the gallery + SVG viewer.

History: native requestFullscreen() was once disabled inside Orca because
exiting it left the embedded split pane stuck, and an earlier server-driven
`/orca-window-fs` toggle was removed. We now want *real* whole-screen
fullscreen everywhere (CSS fullscreen can only ever fill the pane), so native
FS is enabled by default and the exit path is hardened: lbFsReflow()/fsReflow()
nudge layout across several animation frames + timers after exit so the pane
re-settles. These tests lock in that contract.
"""

import unittest
import sys
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cmux_gallery


def _func_body(src: str, signature: str) -> str:
    """Return the body of a top-level JS function up to its column-0 ``}``."""
    return src.split(signature)[1].split("\n}")[0]


class FullscreenRegressionTests(unittest.TestCase):
    def test_server_does_not_toggle_orca_window_fullscreen(self):
        # Real fullscreen is driven from the client (requestFullscreen), never by
        # poking Orca's own window through the server. That endpoint stays gone.
        server = (ROOT / "fig_annotate_server.py").read_text()
        self.assertNotIn("/orca-window-fs", server)
        self.assertNotIn("orca_window_fs_toggle", server)

    def test_gallery_enables_native_fullscreen_by_default(self):
        gallery = (ROOT / "build_gallery.py").read_text()
        self.assertIn("function lbNativeFsAllowed()", gallery)
        body = _func_body(gallery, "function lbNativeFsAllowed(){")
        # Real fullscreen is the default; ?cssFs=1 is the only opt-out.
        self.assertIn("return true;", body)
        self.assertIn("'cssFs'", body)
        # The native call is still reachable from the toggle.
        self.assertIn(
            "const req=root.requestFullscreen||root.webkitRequestFullscreen;", gallery
        )

    def test_gallery_hardens_fullscreen_exit_reflow(self):
        gallery = (ROOT / "build_gallery.py").read_text()
        reflow = _func_body(gallery, "function lbFsReflow(){")
        # Not a single synchronous resize: nudge layout across frames + timers.
        self.assertIn("requestAnimationFrame", reflow)
        self.assertIn("setTimeout", reflow)
        # The reflow runs AFTER exitFullscreen resolves (in the finally clause).
        self.assertIn("finally { fsLeaving=false; lbFsReflow(); }", gallery)

    def test_gallery_url_requests_native_fullscreen(self):
        for env in ({}, {"ORCA_APP_VERSION": "1.4.101"}, {"TERM_PROGRAM": "Orca"}):
            with patch.dict("os.environ", env, clear=True):
                self.assertEqual(
                    cmux_gallery.gallery_url(8790),
                    "http://127.0.0.1:8790/figures_index.html?nativeFs=1",
                )

    def test_svg_viewer_enables_native_fullscreen_by_default(self):
        viewer = (ROOT / "assets" / "svg_viewer.html").read_text()
        self.assertIn("function nativeFsAllowed()", viewer)
        body = _func_body(viewer, "function nativeFsAllowed(){")
        self.assertIn("return true;", body)
        self.assertIn('"cssFs"', body)
        self.assertIn(
            "const req=document.documentElement.requestFullscreen || "
            "document.documentElement.webkitRequestFullscreen;",
            viewer,
        )
        # Hardened exit reflow present and wired into the exit path.
        self.assertIn("function fsReflow()", viewer)
        self.assertIn("requestAnimationFrame", _func_body(viewer, "function fsReflow(){"))
        self.assertIn("setFsUi(false);\n  fsReflow();", viewer)
        self.assertIn("body.fs-mode header{display:none}", viewer)


if __name__ == "__main__":
    unittest.main()
