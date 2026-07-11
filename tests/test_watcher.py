import os
import tempfile
import unittest
from pathlib import Path

import fig_annotate_server as server


class ArtifactWatcherTests(unittest.TestCase):
    def test_snapshot_tracks_supported_files_and_ignores_generated_trees(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "figure.png").write_bytes(b"png")
            Path(td, "notes.txt").write_text("ignored")
            Path(td, "figures_index.html").write_text("generated")
            Path(td, ".atelier-provenance.json").write_text("{}")
            hidden = Path(td, ".fig_thumbs")
            hidden.mkdir()
            Path(hidden, "copy.png").write_bytes(b"ignored")
            snap = server._artifact_snapshot(td)
            self.assertEqual(set(snap), {"figure.png", ".atelier-provenance.json"})

    def test_snapshot_changes_when_an_artifact_changes(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td, "paper.tex")
            artifact.write_text("one")
            before = server._artifact_snapshot(td)
            artifact.write_text("a longer value")
            os.utime(artifact, None)
            after = server._artifact_snapshot(td)
            self.assertNotEqual(before["paper.tex"], after["paper.tex"])


if __name__ == "__main__":
    unittest.main()
