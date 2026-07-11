import json
import tempfile
import unittest
from pathlib import Path

import build_gallery


class ProvenanceTests(unittest.TestCase):
    def test_declared_manifest_attaches_safe_argv_command(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, ".atelier-provenance.json").write_text(json.dumps({
                "artifacts": {"figure.pdf": {
                    "generator": "scripts/figure.py",
                    "command": ["python3", "scripts/figure.py"],
                    "inputs": ["data.csv"],
                }}
            }))
            rows = [{"rel": "figure.pdf", "ext": "pdf"}]
            build_gallery.enrich_provenance(rows, td)
            self.assertEqual(rows[0]["provenance"]["confidence"], "declared")
            self.assertEqual(rows[0]["provenance"]["command"][0], "python3")

    def test_same_stem_script_is_inferred_without_executable_command(self):
        with tempfile.TemporaryDirectory() as td:
            rows = [
                {"rel": "scripts/map.py", "ext": "py"},
                {"rel": "outputs/map.png", "ext": "png"},
            ]
            build_gallery.enrich_provenance(rows, td)
            self.assertEqual(rows[1]["provenance"]["generator"], "scripts/map.py")
            self.assertNotIn("command", rows[1]["provenance"])


if __name__ == "__main__":
    unittest.main()
