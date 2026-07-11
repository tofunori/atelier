"""Inventaire de parité : document, registre et serveurs vivants concordent.

Trois propriétés vérifiées :
1. la matrice ``docs/rust-route-parity.md`` et le registre ``support.ROUTES``
   listent exactement les mêmes routes avec les mêmes statuts ;
2. chaque route déclarée existe réellement dans le backend Python
   (probe vivante) ;
3. côté Rust, les routes ``ported``/``partial`` répondent, les routes
   ``missing`` renvoient la signature d'absence (404/405) — une route qui
   apparaît ou disparaît d'un backend sans mise à jour de la matrice fait
   échouer la suite.
"""

from __future__ import annotations

import unittest

from . import support


class TestMatrixDocSync(unittest.TestCase):
    """La matrice markdown est la source de vérité ; le registre doit la suivre."""

    def test_document_matches_registry(self):
        doc_rows = support.parse_parity_matrix()
        self.assertTrue(doc_rows, "matrice vide ou marqueurs parity:begin/end absents")
        doc = {(row["method"], row["route"]): row["status"] for row in doc_rows}
        registry = {(spec.method, spec.route): spec.status
                    for spec in support.build_routes()}
        only_doc = sorted(set(doc) - set(registry))
        only_registry = sorted(set(registry) - set(doc))
        self.assertFalse(
            only_doc or only_registry,
            f"routes désynchronisées — document seulement: {only_doc}, "
            f"registre seulement: {only_registry} "
            "(lancer python3 tests/contract/update_matrix.py)")
        for key, status in registry.items():
            self.assertEqual(
                doc[key], status,
                f"statut divergent pour {key[0]} {key[1]}: "
                f"document={doc[key]}, registre={status}")

    def test_registry_is_well_formed(self):
        seen = set()
        for spec in support.build_routes():
            key = (spec.method, spec.route)
            self.assertNotIn(key, seen, f"route dupliquée: {key}")
            seen.add(key)
            self.assertIn(spec.status, support.STATUSES)
            if spec.probe is None:
                self.assertIsNotNone(
                    spec.skip_live,
                    f"{key}: une route sans probe doit déclarer skip_live")

    def test_doc_statuses_valid(self):
        for row in support.parse_parity_matrix():
            self.assertIn(row["status"], support.STATUSES,
                          f"statut inconnu dans la matrice: {row}")


class TestPythonRoutes(unittest.TestCase):
    """Chaque route déclarée répond dans le backend Python (référence)."""

    backend: support.Backend

    @classmethod
    def setUpClass(cls):
        cls.backend = support.get_backend("python")

    def test_declared_routes_respond(self):
        for spec in support.build_routes():
            if spec.status == "rust-only" or spec.probe is None or spec.skip_live:
                continue
            with self.subTest(route=f"{spec.method} {spec.route}"):
                reply = support.probe(self.backend, spec)
                self.assertIn(
                    reply.status, spec.python_expect,
                    f"{spec.method} {spec.route}: {reply.status} hors de "
                    f"{sorted(spec.python_expect)} — corps: {reply.body[:200]!r}")

    def test_rust_only_routes_absent(self):
        for spec in support.build_routes():
            if spec.status != "rust-only" or spec.probe is None:
                continue
            with self.subTest(route=f"{spec.method} {spec.route}"):
                reply = support.probe(self.backend, spec)
                self.assertIn(
                    reply.status, support.MISSING_STATUSES,
                    f"{spec.method} {spec.route} répond dans Python "
                    "mais est marquée rust-only — mettre à jour la matrice")


@unittest.skipUnless(support.rust_available(),
                     "backend Rust indisponible (ni binaire ni cargo)")
class TestRustRoutes(unittest.TestCase):
    """Le backend Rust est conforme aux statuts déclarés dans la matrice."""

    backend: support.Backend

    @classmethod
    def setUpClass(cls):
        cls.backend = support.get_backend("rust")

    def test_ported_and_partial_routes_respond(self):
        for spec in support.build_routes():
            if spec.status not in ("ported", "partial") or spec.probe is None \
                    or spec.skip_live:
                continue
            with self.subTest(route=f"{spec.method} {spec.route}"):
                reply = support.probe(self.backend, spec)
                self.assertIn(
                    reply.status, spec.rust_expect,
                    f"{spec.method} {spec.route} (statut {spec.status}): "
                    f"{reply.status} hors de {sorted(spec.rust_expect)} — "
                    f"corps: {reply.body[:200]!r}")

    def test_missing_routes_are_absent(self):
        for spec in support.build_routes():
            if spec.status != "missing" or spec.probe is None or spec.skip_live:
                continue
            with self.subTest(route=f"{spec.method} {spec.route}"):
                reply = support.probe(self.backend, spec)
                self.assertIn(
                    reply.status, support.MISSING_STATUSES,
                    f"{spec.method} {spec.route} répond ({reply.status}) dans Rust "
                    "mais est marquée missing — mettre à jour la matrice et le "
                    "harness de contrat")

    def test_rust_only_routes_respond(self):
        for spec in support.build_routes():
            if spec.status != "rust-only" or spec.probe is None:
                continue
            with self.subTest(route=f"{spec.method} {spec.route}"):
                reply = support.probe(self.backend, spec)
                self.assertEqual(
                    reply.status, 200,
                    f"{spec.method} {spec.route} est déclarée rust-only "
                    f"mais répond {reply.status}")


if __name__ == "__main__":
    unittest.main()
