"""Configuration pytest du harness de contrat Python/Rust.

Les tests eux-mêmes sont des ``unittest.TestCase`` (exécutables aussi bien
par ``python3 -m unittest discover -s tests`` que par pytest, conformément
aux commandes de vérification du plan). Ce conftest fournit les fixtures
pytest pour d'éventuels tests pytest-natifs et garantit l'arrêt des serveurs
en fin de session.
"""

from __future__ import annotations

import pytest

from . import support


@pytest.fixture(scope="session")
def python_backend() -> support.Backend:
    return support.get_backend("python")


@pytest.fixture(scope="session")
def rust_backend() -> support.Backend:
    if not support.rust_available():
        pytest.skip("backend Rust indisponible (ni binaire ni cargo)")
    return support.get_backend("rust")


@pytest.fixture(scope="session")
def fixture_template():
    return support.build_template()


def pytest_sessionfinish(session, exitstatus):
    support.stop_all()
