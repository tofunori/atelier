"""Contrats de réponse : mêmes requêtes, mêmes réponses (Python vs Rust).

Chaque scénario exécute la même séquence de requêtes contre les deux
backends (instances fraîches, projets fixtures identiques) et compare les
réponses normalisées. Seules les valeurs déclarées volatiles dans
``docs/rust-route-parity.md`` sont ignorées (PID, port, timestamps,
identifiants aléatoires, chemins machine). Les routes ``partial`` ne
comparent que leur sous-ensemble stable déclaré.
"""

from __future__ import annotations

import unittest

from . import support


@unittest.skipUnless(support.rust_available(),
                     "backend Rust indisponible (ni binaire ni cargo)")
class TestResponseContracts(unittest.TestCase):
    maxDiff = None
    python: support.Backend
    rust: support.Backend

    @classmethod
    def setUpClass(cls):
        cls.python = support.get_backend("python", "contract")
        cls.rust = support.get_backend("rust", "contract")

    # -- aides ---------------------------------------------------------------

    def both(self, method, path, body=None, *, token=False):
        return (self.python.request(method, path, body, token=token),
                self.rust.request(method, path, body, token=token))

    def assert_parity(self, py_reply, rust_reply, volatile=(), context=""):
        self.assertEqual(
            py_reply.status, rust_reply.status,
            f"{context}: statuts divergents (python={py_reply.status} "
            f"corps={py_reply.body[:200]!r}, rust={rust_reply.status} "
            f"corps={rust_reply.body[:200]!r})")
        py_json = py_reply.json_or_none()
        rust_json = rust_reply.json_or_none()
        self.assertEqual(
            support.normalize(py_json, set(volatile), self.python),
            support.normalize(rust_json, set(volatile), self.rust),
            f"{context}: corps JSON divergents après normalisation")

    # -- cœur ------------------------------------------------------------------

    def test_01_ping_declared_subset(self):
        py, rust = self.both("GET", "/ping")
        self.assertEqual((py.status, rust.status), (200, 200))
        py_json, rust_json = py.json(), rust.json()
        # Sous-ensemble stable déclaré (les autres clés divergent — voir
        # « Différences déclarées » de la matrice).
        self.assertIs(py_json["ok"], True)
        self.assertIs(rust_json["ok"], True)
        self.assertEqual(rust_json["backend"], "rust")
        self.assertEqual(
            support.normalize(py_json["project"], set(), self.python), "<PROJECT>")
        self.assertEqual(
            support.normalize(rust_json["project"], set(), self.rust), "<PROJECT>")
        self.assertEqual(py_json["agentHost"], rust_json.get("agentHost"))
        self.assertEqual(py_json["agentBridgeProtocol"],
                         rust_json.get("agentBridgeProtocol"))

    def test_02_rev_shape(self):
        py, rust = self.both("GET", "/rev")
        self.assert_parity(py, rust, volatile={"rev"}, context="GET /rev")
        self.assertIsInstance(py.json()["rev"], int)
        self.assertIsInstance(rust.json()["rev"], int)

    def test_03_index_html(self):
        py, rust = self.both("GET", "/")
        self.assertEqual((py.status, rust.status), (200, 200), "GET /")
        self.assertEqual(py.content_type, "text/html")
        self.assertEqual(rust.content_type, "text/html")

    def test_04_static_png_bytes(self):
        py, rust = self.both("GET", "/tiny.png")
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertEqual(py.content_type, "image/png")
        self.assertEqual(rust.content_type, "image/png")
        self.assertEqual(py.body, rust.body, "GET /tiny.png: contenus divergents")

    def test_05_static_head(self):
        py, rust = self.both("HEAD", "/tiny.png")
        self.assertEqual((py.status, rust.status), (200, 200))

    def test_06_data_identical(self):
        py, rust = self.both("GET", "/data")
        self.assert_parity(py, rust, context="GET /data")

    def test_07_state_roundtrip(self):
        payload = {"favs": ["tiny.png"], "ratings": {"plot.svg": 4},
                   "hidden": ["report.html"], "tags": {"tiny.png": ["fig"]},
                   "hideRules": [], "collections": {}, "workflow": {}}
        py_post, rust_post = self.both("POST", "/state", payload)
        self.assert_parity(py_post, rust_post, context="POST /state")
        py_get, rust_get = self.both("GET", "/state")
        self.assert_parity(py_get, rust_get, context="GET /state (relecture)")
        self.assertEqual(py_get.json()["favs"], ["tiny.png"])

    def test_08_provenance(self):
        py, rust = self.both("GET", "/provenance?rel=tiny.png")
        self.assert_parity(py, rust, context="GET /provenance")

    def test_09_regenerate_without_command(self):
        py, rust = self.both("POST", "/regenerate", {"rel": "tiny.png"})
        self.assert_parity(py, rust, context="POST /regenerate (sans commande)")

    # -- cycle de vie agent -----------------------------------------------------

    def test_10_agent_lifecycle(self):
        volatile_item = {
            "items[].id", "items[].ts", "items[].statusAt", "items[].claimedAt",
        }
        volatile_status = {
            "pending[].id", "pending[].ts", "pending[].statusAt",
            "pending[].claimedAt",
            "history[].id", "history[].ts", "history[].statusAt",
            "history[].claimedAt",
            "consumers[].lastSeen", "consumers[].pid",
        }

        py_reg, rust_reg = self.both(
            "POST", "/agent-consumers/register",
            {"consumer": "contract", "destination": "contract"}, token=True)
        self.assert_parity(
            py_reg, rust_reg,
            volatile={"destination.lastSeen", "destination.pid"},
            context="POST /agent-consumers/register")

        py_enq, rust_enq = self.both(
            "POST", "/agent-selection",
            {"rel": "tiny.png", "text": "sélection de contrat",
             "comment": "contrat", "destination": "contract"},
            token=True)
        self.assert_parity(py_enq, rust_enq, volatile={"id"},
                           context="POST /agent-selection")

        py_status, rust_status = self.both("GET", "/agent-status?limit=20")
        self.assert_parity(py_status, rust_status, volatile=volatile_status,
                           context="GET /agent-status (1 en attente)")

        py_claim, rust_claim = self.both(
            "GET", "/agent-selections?consumer=contract&destination=contract",
            token=True)
        self.assert_parity(py_claim, rust_claim, volatile=volatile_item,
                           context="GET /agent-selections (claim)")
        py_items = py_claim.json().get("items", [])
        rust_items = rust_claim.json().get("items", [])
        self.assertTrue(py_items, "claim Python vide — scénario invalide")
        self.assertTrue(rust_items, "claim Rust vide — scénario invalide")

        py_ack, rust_ack = (
            self.python.request("POST", "/agent-selections/ack",
                                {"ids": [py_items[0]["id"]], "consumer": "contract"},
                                token=True),
            self.rust.request("POST", "/agent-selections/ack",
                              {"ids": [rust_items[0]["id"]], "consumer": "contract"},
                              token=True),
        )
        self.assert_parity(py_ack, rust_ack, context="POST /agent-selections/ack")

        py_after, rust_after = self.both("GET", "/agent-status?limit=20")
        self.assert_parity(py_after, rust_after, volatile=volatile_status,
                           context="GET /agent-status (après ack)")

    # -- routes partielles : sous-ensemble stable --------------------------------

    def test_11_quote_partial_subset(self):
        py, rust = self.both(
            "POST", "/quote",
            {"rel": "report.md", "text": "extrait de contrat", "embed": True,
             "destination": "contract"})
        self.assertEqual((py.status, rust.status), (200, 200), "POST /quote")
        self.assertTrue(py.json().get("queuedForAgent"),
                        f"quote Python non mise en file: {py.body[:200]!r}")
        self.assertTrue(rust.json().get("queuedForAgent"),
                        f"quote Rust non mise en file: {rust.body[:200]!r}")

    def test_12_save_partial_subset(self):
        body = {"name": "contract", "embed": True, "destination": "contract",
                "dataURL": f"data:image/png;base64,{support.TINY_PNG_B64}"}
        py, rust = self.both("POST", "/save", body)
        self.assertEqual((py.status, rust.status), (200, 200), "POST /save")
        for backend, reply in ((self.python, py), (self.rust, rust)):
            saved = reply.json().get("path", "")
            self.assertIn("annotations/", saved,
                          f"POST /save ({backend.name}): chemin inattendu {saved!r}")
            pngs = list((backend.project / "annotations").glob("contract_annot_*.png"))
            self.assertTrue(pngs,
                            f"POST /save ({backend.name}): PNG absent sur disque")

    # -- reconstruction ------------------------------------------------------------

    def test_13_rescan(self):
        py, rust = self.both("POST", "/rescan", {})
        self.assert_parity(py, rust, volatile={"out"}, context="POST /rescan")
        self.assertIs(py.json()["ok"], True)

    # -- phase 1 : fichiers / éditeurs -------------------------------------------

    def test_14_ls_root(self):
        py, rust = self.both("GET", "/ls?dir=")
        self.assert_parity(py, rust, context="GET /ls")
        items = {row["name"] for row in py.json()["items"]}
        self.assertIn("script.py", items)
        self.assertIn("tiny.png", items)

    def test_15_snippet_markdown(self):
        py, rust = self.both("GET", "/snippet?path=report.md&n=5")
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertEqual(py.content_type, "text/plain")
        self.assertEqual(rust.content_type, "text/plain")
        self.assertEqual(py.body, rust.body, "GET /snippet: corps divergents")

    def test_16_raw_pdf(self):
        py, rust = self.both("GET", "/raw?path=mini.pdf")
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertEqual(py.content_type, "application/pdf")
        self.assertEqual(rust.content_type, "application/pdf")
        self.assertEqual(py.body, rust.body)

    def test_17_code_roundtrip_and_conflict(self):
        py, rust = self.both("GET", "/code?path=script.py")
        self.assert_parity(py, rust, volatile={"mtime"}, context="GET /code")
        self.assertIn("fixture tiny.png", py.json()["text"])

        # Sauvegarde avec le mtime lu → 200, mtime mis à jour.
        text = py.json()["text"]
        py_m = py.json()["mtime"]
        rust_m = rust.json()["mtime"]
        py_save = self.python.request(
            "POST", "/codesave", {"path": "script.py", "text": text, "mtime": py_m})
        rust_save = self.rust.request(
            "POST", "/codesave", {"path": "script.py", "text": text, "mtime": rust_m})
        self.assertEqual((py_save.status, rust_save.status), (200, 200), "POST /codesave")
        self.assertIn("mtime", py_save.json())
        self.assertIn("mtime", rust_save.json())

        # Conflit : mtime périmé → 409 des deux côtés.
        py_c, rust_c = self.both(
            "POST", "/codesave",
            {"path": "script.py", "text": text, "mtime": 1.0})
        self.assertEqual((py_c.status, rust_c.status), (409, 409))
        self.assertEqual(py_c.json().get("error"), "conflit")
        self.assertEqual(rust_c.json().get("error"), "conflit")

    def test_18_texroot(self):
        py, rust = self.both("GET", "/texroot?path=doc.tex")
        self.assert_parity(py, rust, context="GET /texroot")
        self.assertTrue(str(py.json()["root"]).endswith("doc.tex"))
        self.assertTrue(str(py.json()["pdf"]).endswith("doc.pdf"))

    def test_19_findscript(self):
        py, rust = self.both("GET", "/findscript?stem=tiny")
        self.assert_parity(py, rust, context="GET /findscript")
        # fixture script.py contient "tiny.png"
        self.assertEqual(py.json().get("script"), "script.py")

    def test_20_save_svg_and_backup(self):
        svg = support._svg_payload()
        py, rust = self.both(
            "POST", "/save-svg", {"rel": "plot.svg", "svg": svg})
        self.assert_parity(py, rust, context="POST /save-svg")
        self.assertTrue(py.json().get("ok"))
        for backend in (self.python, self.rust):
            bak = backend.project / "plot.svg.orig.bak"
            self.assertTrue(bak.is_file(),
                            f"{backend.name}: .orig.bak absent")
            self.assertEqual(bak.read_text(), svg)

    def test_21_selinfo(self):
        py, rust = self.both(
            "POST", "/selinfo",
            {"rel": "script.py", "lines": "L1-3", "text": "print"})
        self.assert_parity(py, rust, context="POST /selinfo")
        for backend in (self.python, self.rust):
            sel = backend.home / ".claude" / "fig-selection.json"
            self.assertTrue(sel.is_file(),
                            f"{backend.name}: fig-selection.json absent")

    def test_22_path_escape_rejected(self):
        py, rust = self.both("GET", "/code?path=../outside.tex")
        self.assertEqual(py.status, 404)
        self.assertEqual(rust.status, 404)

    # -- phase 2 : galerie / actions -------------------------------------------

    def test_23_thumb_png(self):
        py, rust = self.both("GET", "/thumb?path=tiny.png&w=64")
        self.assertEqual((py.status, rust.status), (200, 200), "GET /thumb")
        self.assertEqual(py.content_type, "image/png")
        self.assertEqual(rust.content_type, "image/png")
        # Les PNG sips peuvent différer d'un octet ; on exige seulement une
        # image non vide des deux côtés (cache .fig_thumbs peuplé).
        self.assertGreater(len(py.body), 20)
        self.assertGreater(len(rust.body), 20)
        for backend in (self.python, self.rust):
            thumbs = list((backend.project / ".fig_thumbs").glob("imgthumb_*.png"))
            self.assertTrue(thumbs, f"{backend.name}: cache miniature absent")

    def test_24_agent_event_toast(self):
        py, rust = self.both("POST", "/agent-event", {"rel": "tiny.png", "note": "hi"})
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertTrue(py.json().get("ok"))
        self.assertTrue(rust.json().get("ok"))
        self.assertIn("id", py.json())
        self.assertIn("id", rust.json())
        py_ev, rust_ev = self.both("GET", "/agent-events?since=0")
        self.assert_parity(
            py_ev, rust_ev,
            volatile={"events[].id", "events[].ts", "events[].row.mtime",
                      "events[].row.btime", "events[].row.mdate",
                      "events[].row.bdate", "last"},
            context="GET /agent-events")
        self.assertGreaterEqual(len(py_ev.json().get("events", [])), 1)

    def test_25_claude_targets_agent_mode(self):
        py, rust = self.both("GET", "/claude-targets")
        self.assert_parity(py, rust, context="GET /claude-targets")
        self.assertEqual(py.json().get("targets"), [])

    def test_26_quote_pending_and_clear(self):
        py, rust = self.both("GET", "/quote")
        self.assert_parity(py, rust, context="GET /quote")
        py_c, rust_c = self.both("POST", "/clear-quote", {})
        self.assert_parity(py_c, rust_c, context="POST /clear-quote")
        self.assertTrue(py_c.json().get("ok"))

    def test_27_delete_to_trash(self):
        # Fichier jetable par backend. Le HOME de contrat est temporaire : il
        # faut créer ~/.Trash (présent sur un Mac réel) pour que Python puisse
        # y renommer le fichier.
        for backend in (self.python, self.rust):
            (backend.home / ".Trash").mkdir(parents=True, exist_ok=True)
            victim = backend.project / "to-delete-contract.txt"
            victim.write_text("bye\n", encoding="utf-8")
        py = self.python.request(
            "POST", "/delete", {"rels": ["to-delete-contract.txt"]})
        rust = self.rust.request(
            "POST", "/delete", {"rels": ["to-delete-contract.txt"]})
        self.assertEqual((py.status, rust.status), (200, 200),
                         f"delete: py={py.body[:200]!r} rust={rust.body[:200]!r}")
        self.assertEqual(py.json().get("deleted"), ["to-delete-contract.txt"])
        self.assertEqual(rust.json().get("deleted"), ["to-delete-contract.txt"])
        for backend in (self.python, self.rust):
            self.assertFalse(
                (backend.project / "to-delete-contract.txt").exists(),
                f"{backend.name}: fichier encore dans le projet")
            trash = backend.home / ".Trash"
            matches = list(trash.glob("to-delete-contract*.txt"))
            self.assertTrue(matches, f"{backend.name}: absent de ~/.Trash")

    def test_28_export_zip(self):
        py = self.python.request(
            "POST", "/export", {"rels": ["tiny.png"], "mode": "zip"})
        rust = self.rust.request(
            "POST", "/export", {"rels": ["tiny.png"], "mode": "zip"})
        self.assertEqual((py.status, rust.status), (200, 200), "POST /export zip")
        self.assertTrue(py.json().get("ok"))
        self.assertTrue(rust.json().get("ok"))
        self.assertEqual(py.json().get("count"), 1)
        self.assertEqual(rust.json().get("count"), 1)
        for backend, reply in ((self.python, py), (self.rust, rust)):
            rel = reply.json().get("path", "")
            zpath = backend.project / rel
            self.assertTrue(zpath.is_file(), f"{backend.name}: zip absent {rel}")
            self.assertTrue(rel.startswith("_gallery_exports/"))

    def test_29_export_empty_rejected(self):
        py, rust = self.both("POST", "/export", {"rels": [], "mode": "zip"})
        self.assertEqual((py.status, rust.status), (400, 400))

    def test_30_open_escape_rejected(self):
        py, rust = self.both("POST", "/open", {"rel": "../hors-projet.bin"})
        self.assertIn(py.status, (400, 404))
        self.assertIn(rust.status, (400, 404))

    # -- phase 3 : git + versions ----------------------------------------------

    def test_31_githead(self):
        py, rust = self.both("GET", "/githead?path=script.py")
        self.assert_parity(py, rust, volatile={"sha", "ts"}, context="GET /githead")
        self.assertTrue(py.json().get("ok"))
        self.assertIn("fixture tiny.png", py.json().get("text", ""))

    def test_32_gitlog_and_show(self):
        py, rust = self.both("GET", "/gitlog?path=script.py")
        self.assert_parity(py, rust, volatile={"items[].ts", "items[].sha"},
                           context="GET /gitlog")
        self.assertTrue(py.json().get("ok"))
        items = py.json().get("items") or []
        self.assertTrue(items, "gitlog vide sur fixture committée")
        py_sha = items[0]["sha"]
        rust_sha = (rust.json().get("items") or [{}])[0].get("sha", py_sha)
        py_s = self.python.request("GET", f"/gitshow?path=script.py&sha={py_sha}")
        rust_s = self.rust.request("GET", f"/gitshow?path=script.py&sha={rust_sha}")
        self.assertEqual((py_s.status, rust_s.status), (200, 200))
        self.assertTrue(py_s.json().get("ok"))
        self.assertTrue(rust_s.json().get("ok"))
        self.assertEqual(py_s.json().get("text"), rust_s.json().get("text"))

    def test_33_commitmsg_clean_tree(self):
        py, rust = self.both("POST", "/commitmsg", {"path": "script.py"})
        self.assert_parity(py, rust, context="POST /commitmsg (propre)")
        self.assertFalse(py.json().get("ok"))

    def test_34_gitcommit_nothing_to_commit(self):
        py, rust = self.both(
            "POST", "/gitcommit",
            {"path": "script.py", "message": "contract nothing"})
        self.assert_parity(py, rust, context="POST /gitcommit (propre)")
        self.assertFalse(py.json().get("ok"))

    def test_34b_gitcommit_isolates_unrelated_staged_files(self):
        import subprocess

        for backend in (self.python, self.rust):
            allowed = backend.project / "allowed-contract.txt"
            unrelated = backend.project / "unrelated-contract.txt"

            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=backend.project, check=True,
                    capture_output=True, text=True,
                ).stdout

            allowed.write_text("base\n", encoding="utf-8")
            unrelated.write_text("base\n", encoding="utf-8")
            git("add", allowed.name, unrelated.name)
            git("commit", "-qm", "contract base")
            allowed.write_text("allowed change\n", encoding="utf-8")
            unrelated.write_text("unrelated change\n", encoding="utf-8")
            git("add", unrelated.name)
            reply = backend.request(
                "POST", "/gitcommit",
                {"path": allowed.name, "message": "Commit allowed contract"},
            )
            self.assertEqual(reply.status, 200, backend.name)
            self.assertTrue(reply.json().get("ok"), f"{backend.name}: {reply.json()}")
            names = git("show", "--format=", "--name-only", "HEAD").splitlines()
            self.assertEqual(names, [allowed.name], backend.name)
            self.assertIn(unrelated.name, git("diff", "--cached", "--name-only").splitlines())
            git("reset", "--hard", "HEAD")

    def test_35_versions_roundtrip(self):
        # GET état vide
        py, rust = self.both("GET", "/versions?path=script.py")
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertTrue(py.json().get("ok"))
        self.assertTrue(rust.json().get("ok"))
        self.assertEqual(py.json().get("revision"), 0)
        self.assertEqual(rust.json().get("revision"), 0)

        # POST ops vides → revision 1
        py_p, rust_p = self.both(
            "POST", "/versions",
            {"path": "script.py", "expectedRevision": 0, "ops": []})
        self.assert_parity(py_p, rust_p, context="POST /versions (ops vides)")
        self.assertTrue(py_p.json().get("ok"))
        self.assertEqual(py_p.json().get("revision"), 1)

        # Conflit de révision
        py_c, rust_c = self.both(
            "POST", "/versions",
            {"path": "script.py", "expectedRevision": 0, "ops": []})
        self.assertEqual((py_c.status, rust_c.status), (409, 409))
        self.assertEqual(py_c.json().get("error"), "revision-conflict")
        self.assertEqual(rust_c.json().get("error"), "revision-conflict")

    def test_36_gitshow_rejects_bad_sha(self):
        py, rust = self.both("GET", "/gitshow?path=script.py&sha=HEAD")
        self.assert_parity(py, rust, context="GET /gitshow bad sha")
        self.assertFalse(py.json().get("ok"))

    # -- phase 4 : LaTeX / PDF / export PNG ------------------------------------

    def test_37_compile_and_synctex_outside_project(self):
        py, rust = self.both("POST", "/compile", {"path": "/etc/hosts"})
        self.assertEqual((py.status, rust.status), (403, 403))
        py_s, rust_s = self.both(
            "POST", "/synctex",
            {"tex": "/etc/hosts", "pdf": "/etc/hosts", "dir": "view",
             "line": 1, "col": 1})
        self.assertEqual((py_s.status, rust_s.status), (403, 403))

    def test_38_pdfannot_roundtrip(self):
        annots = [{"type": "highlight", "page": 1, "text": "contract"}]
        py_p, rust_p = self.both(
            "POST", "/pdfannot", {"rel": "mini.pdf", "annots": annots})
        self.assertEqual((py_p.status, rust_p.status), (200, 200))
        self.assertTrue(py_p.json().get("ok"))
        self.assertTrue(rust_p.json().get("ok"))
        py_g, rust_g = self.both("GET", "/pdfannot?rel=mini.pdf")
        self.assert_parity(py_g, rust_g, context="GET /pdfannot")
        self.assertEqual(py_g.json().get("annots"), annots)
        # clear
        self.both("POST", "/pdfannot", {"rel": "mini.pdf", "annots": []})

    def test_39_export_png(self):
        svg = support._svg_payload()
        py, rust = self.both(
            "POST", "/export-png",
            {"rel": "plot.svg", "svg": svg, "dpi": 72})
        self.assertEqual(py.status, rust.status, "POST /export-png status")
        self.assertIn(py.status, (200, 501))
        if py.status == 200:
            self.assertTrue(py.json().get("ok"))
            self.assertTrue(rust.json().get("ok"))
            self.assertEqual(py.json().get("dpi"), 72)
            self.assertEqual(rust.json().get("dpi"), 72)
            for backend, reply in ((self.python, py), (self.rust, rust)):
                rel = reply.json().get("path", "")
                self.assertTrue((backend.project / rel).is_file(),
                                f"{backend.name}: png absent {rel}")

    def test_40_compile_fixture_if_latexmk(self):
        """Compile doc.tex when latexmk is available (soft skip otherwise)."""
        import shutil
        if not (shutil.which("latexmk")
                or __import__("os").path.isfile("/Library/TeX/texbin/latexmk")):
            self.skipTest("latexmk absent")
        py = self.python.request("POST", "/compile", {"path": "doc.tex"})
        rust = self.rust.request("POST", "/compile", {"path": "doc.tex"})
        self.assertEqual((py.status, rust.status), (200, 200))
        # Both should succeed on the minimal fixture, or both fail for the same reason.
        self.assertEqual(py.json().get("ok"), rust.json().get("ok"),
                         f"compile parity: py={py.json()} rust={rust.json()}")
        if py.json().get("ok"):
            for backend in (self.python, self.rust):
                self.assertTrue((backend.project / "doc.pdf").is_file()
                                or any(backend.project.glob("**/*.pdf")),
                                f"{backend.name}: PDF manquant après compile")

    # -- phase 5 : notes + whiteboard ------------------------------------------

    def test_41_notes_roundtrip(self):
        md = "# notes contract\n\nligne deux\n"
        py_s, rust_s = self.both("POST", "/notes/save", {"markdown": md})
        self.assert_parity(py_s, rust_s, context="POST /notes/save")
        py_l, rust_l = self.both("GET", "/notes/load")
        self.assert_parity(py_l, rust_l, context="GET /notes/load")
        self.assertEqual(py_l.json().get("markdown"), md)
        for backend in (self.python, self.rust):
            self.assertEqual((backend.project / "notes.md").read_text(), md)

    def test_42_board_roundtrip_and_queue(self):
        snap = {"store": {"doc": "contract"}, "schema": 1}
        py_s, rust_s = self.both("POST", "/board/save", {"snapshot": snap})
        self.assert_parity(py_s, rust_s, context="POST /board/save")
        py_l, rust_l = self.both("GET", "/board/load")
        self.assert_parity(py_l, rust_l, context="GET /board/load")
        self.assertEqual(py_l.json().get("snapshot"), snap)

        py_c, rust_c = self.both(
            "POST", "/board/command", {"type": "contract-noop", "n": 1})
        self.assert_parity(py_c, rust_c, context="POST /board/command")
        self.assertTrue(py_c.json().get("queued"))

        py_p, rust_p = self.both("GET", "/board/poll")
        self.assertEqual((py_p.status, rust_p.status), (200, 200))
        self.assertEqual(len(py_p.json().get("commands") or []), 1)
        self.assertEqual(len(rust_p.json().get("commands") or []), 1)
        self.assertEqual(py_p.json()["commands"][0]["type"], "contract-noop")
        self.assertEqual(rust_p.json()["commands"][0]["type"], "contract-noop")

        # Second poll empties the queue
        py_e, rust_e = self.both("GET", "/board/poll")
        self.assertEqual(py_e.json().get("commands"), [])
        self.assertEqual(rust_e.json().get("commands"), [])

    def test_43_board_add_image_and_open_surface_nopush(self):
        py, rust = self.both(
            "POST", "/board/command",
            {"type": "add_image", "rel": "tiny.png"})
        self.assertEqual((py.status, rust.status), (200, 200))
        # Drain so queue does not leak into other tests
        self.both("GET", "/board/poll")

        py_o, rust_o = self.both("POST", "/notes/open-surface", {})
        self.assertEqual((py_o.status, rust_o.status), (500, 500))
        self.assertIn("no-push", py_o.json().get("error", ""))
        self.assertIn("no-push", rust_o.json().get("error", ""))

    # -- phase 6 : Zotero (HOME sandbox sans bibliothèque) ---------------------

    def test_44_zotero_degraded_without_library(self):
        py, rust = self.both("GET", "/zotero-items?q=")
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertEqual(py.json().get("items"), [])
        self.assertEqual(rust.json().get("items"), [])
        self.assertIn("error", py.json())
        self.assertIn("error", rust.json())

        py_c, rust_c = self.both("GET", "/zotero-collections")
        self.assertEqual((py_c.status, rust_c.status), (200, 200))
        self.assertEqual(py_c.json().get("collections"), [])
        self.assertEqual(rust_c.json().get("collections"), [])

    def test_45_zotero_fav_roundtrip(self):
        py, rust = self.both(
            "POST", "/zotero-fav", {"key": "ABCD1234", "on": True})
        self.assert_parity(py, rust, context="POST /zotero-fav on")
        self.assertEqual(py.json().get("key"), "ABCD1234")
        self.assertTrue(py.json().get("fav"))
        py_off, rust_off = self.both(
            "POST", "/zotero-fav", {"key": "ABCD1234", "on": False})
        self.assert_parity(py_off, rust_off, context="POST /zotero-fav off")
        self.assertFalse(py_off.json().get("fav"))

    def test_46_zotero_fav_bad_key(self):
        py, rust = self.both(
            "POST", "/zotero-fav", {"key": "bad", "on": True})
        self.assertEqual((py.status, rust.status), (400, 400))

    # -- phase 7–8 + média -----------------------------------------------------

    def test_47_options_preflight(self):
        py = self.python.request("OPTIONS", "/state")
        rust = self.rust.request("OPTIONS", "/state")
        self.assertEqual((py.status, rust.status), (200, 200))

    def test_48_orca_fullscreen_and_native_reject(self):
        py, rust = self.both("POST", "/orca-fullscreen-exit", {})
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertTrue(py.json().get("ok"))
        self.assertTrue(rust.json().get("ok"))
        self.assertTrue(py.json().get("deprecated"))
        py_n, rust_n = self.both(
            "POST", "/orca-native-fullscreen", {"rel": "absent.png"})
        self.assertEqual((py_n.status, rust_n.status), (400, 400))

    def test_49_agent_preferences_and_batches(self):
        self.both(
            "POST", "/agent-consumers/register",
            {"consumer": "pref-contract", "destination": "pref-contract"},
            token=True)
        py, rust = self.both(
            "POST", "/agent-preferences",
            {"destination": "pref-contract", "automatic": True, "label": "Pref"},
        )
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertTrue(py.json().get("ok"))
        self.assertTrue(rust.json().get("ok"))
        self.assertTrue(py.json()["destination"].get("automatic"))
        self.assertTrue(rust.json()["destination"].get("automatic"))

        py_b, rust_b = self.both(
            "POST", "/agent-batches/release", {"batchId": "no-such-batch"})
        self.assertEqual((py_b.status, rust_b.status), (200, 200))
        self.assertEqual(py_b.json().get("released"), 0)
        self.assertEqual(rust_b.json().get("released"), 0)

        py_c, rust_c = self.both(
            "POST", "/agent-batches/cancel", {"batchId": "no-such-batch"})
        self.assertEqual((py_c.status, rust_c.status), (200, 200))
        self.assertEqual(py_c.json().get("cancelled"), 0)

    def test_50_agent_selection_peek(self):
        py, rust = self.both("GET", "/agent-selection", token=True)
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertTrue(py.json().get("ok"))
        self.assertTrue(rust.json().get("ok"))
        self.assertIn("pending", py.json())
        self.assertIn("usage", rust.json())

    def test_51_html_sel_overlay_and_video_range(self):
        py, rust = self.both("GET", "/report.html")
        self.assertEqual((py.status, rust.status), (200, 200))
        self.assertIn(b"sel_overlay.js", py.body)
        self.assertIn(b"sel_overlay.js", rust.body)

        py_v = self.python.request("GET", "/tiny.mp4")
        rust_v = self.rust.request("GET", "/tiny.mp4")
        self.assertEqual((py_v.status, rust_v.status), (200, 200))
        self.assertEqual(py_v.body, rust_v.body)

        # Range request
        import urllib.request
        def ranged(port):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/tiny.mp4",
                headers={"Range": "bytes=0-9", "Accept": "*/*"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()

        import urllib.error
        py_st, py_h, py_b = ranged(self.python.port)
        rust_st, rust_h, rust_b = ranged(self.rust.port)
        self.assertEqual(py_st, 206)
        self.assertEqual(rust_st, 206)
        self.assertEqual(py_b, rust_b)
        self.assertEqual(len(py_b), 10)


if __name__ == "__main__":
    unittest.main()
