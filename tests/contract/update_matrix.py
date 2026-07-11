"""Régénère le tableau de docs/rust-route-parity.md depuis le registre.

Usage : python3 tests/contract/update_matrix.py

Seule la section entre les marqueurs parity:begin/end est réécrite ; la prose
du document est conservée. À lancer après toute modification du registre
``ROUTES`` de tests/contract/support.py (le test d'inventaire échoue si le
document et le registre divergent).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.contract import support  # noqa: E402


def render_table() -> str:
    lines = [
        "| Méthode | Route | Phase | Statut | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for spec in support.build_routes():
        notes = spec.notes or ""
        if spec.skip_live:
            suffix = f"probe différée : {spec.skip_live}"
            notes = f"{notes} — {suffix}" if notes else suffix
        lines.append(
            f"| `{spec.method}` | `{spec.route}` | {spec.phase} "
            f"| `{spec.status}` | {notes} |")
    return "\n".join(lines)


def main() -> None:
    doc = support.PARITY_DOC.read_text(encoding="utf-8")
    begin_marker = "<!-- parity:begin -->"
    end_marker = "<!-- parity:end -->"
    begin = doc.index(begin_marker) + len(begin_marker)
    end = doc.index(end_marker)
    updated = doc[:begin] + "\n" + render_table() + "\n" + doc[end:]
    support.PARITY_DOC.write_text(updated, encoding="utf-8")
    count = len(support.build_routes())
    print(f"matrice régénérée : {count} routes -> {support.PARITY_DOC}")


if __name__ == "__main__":
    main()
