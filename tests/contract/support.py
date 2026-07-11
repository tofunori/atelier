"""Support du harness de parité Python/Rust (phase 0 de la migration).

Rôles :
- construire un projet fixture identique pour chaque backend (copie d'un
  template bâti une seule fois : fixtures + git init + build_gallery.py) ;
- démarrer chaque backend via ``cmux_gallery.backend_command`` (la même
  logique que la production, ATELIER_BACKEND=python|rust) dans un HOME
  temporaire (aucune écriture dans le vrai HOME) ;
- fournir le registre ``ROUTES`` : chaque route du serveur Python avec sa
  probe sûre, son statut de parité attendu et ses valeurs volatiles ;
- parser la matrice ``docs/rust-route-parity.md`` pour vérifier que le code
  et le document restent synchronisés.

Les probes sont choisies pour être sans effet hors du projet temporaire :
le serveur tourne en mode agent (ATELIER_AGENT_HOST=codex) ce qui désactive
pbcopy et les push cmux/muxy/orca, et les routes qui toucheraient le système
hôte (ex. /open sur un fichier réel, /zotero-add vers le connecteur) sont
sondées avec des entrées refusées tôt ou marquées skip_live.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "rust-migration"
PARITY_DOC = ROOT / "docs" / "rust-route-parity.md"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cmux_gallery  # noqa: E402

AGENT_TOKEN = "contract-suite-token"
STATUSES = {"ported", "partial", "missing", "rust-only"}
# Signature « route absente » : 404 (fallback statique GET) ou 405 (POST/OPTIONS
# refusés par le routeur axum ; jamais renvoyés par les routes JSON sondées).
MISSING_STATUSES = {404, 405}

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

_FIXTURE_FILES = [
    "tiny.png", "plot.svg", "report.md", "doc.tex",
    "mini.pdf", "script.py", "report.html", "tiny.mp4",
]


# ---------------------------------------------------------------------------
# Projet fixture
# ---------------------------------------------------------------------------

_SESSION_DIR: Path | None = None
_TEMPLATE: Path | None = None
CONTEXT: dict = {}


def _session_dir() -> Path:
    global _SESSION_DIR
    if _SESSION_DIR is None:
        # resolve() : macOS renvoie /var/folders/... (symlink de /private/var) ;
        # les serveurs canonicalisent leur racine, la comparaison de chemins doit
        # utiliser la même forme.
        _SESSION_DIR = Path(tempfile.mkdtemp(prefix="atelier-contract-")).resolve()
        atexit.register(shutil.rmtree, _SESSION_DIR, ignore_errors=True)
    return _SESSION_DIR


def _run(cmd, cwd, env=None, timeout=120):
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def build_template() -> Path:
    """Projet fixture bâti une seule fois puis copié pour chaque backend."""
    global _TEMPLATE
    if _TEMPLATE is not None:
        return _TEMPLATE
    template = _session_dir() / "template"
    template.mkdir()
    for name in _FIXTURE_FILES:
        shutil.copy2(FIXTURES / name, template / name)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "contract@test.local"],
        ["git", "config", "user.name", "Contract Suite"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "fixture initiale"],
    ):
        result = _run(cmd, template)
        if result.returncode != 0:
            raise RuntimeError(f"fixture git: {cmd} a échoué: {result.stdout}")
    sha = _run(["git", "rev-parse", "--short", "HEAD"], template).stdout.strip()
    CONTEXT["sha"] = sha
    env = dict(os.environ, GALLERY_ROOT=str(template), GALLERY_NO_THUMBS="1")
    result = _run([sys.executable, str(ROOT / "build_gallery.py")],
                  template, env=env, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"build_gallery.py a échoué: {result.stdout[-2000:]}")
    if not (template / "figures_data.json").exists():
        raise RuntimeError("figures_data.json absent après build")
    _TEMPLATE = template
    return template


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rust_available() -> bool:
    for rel in ("rust/target/release/atelier-server", "rust/target/debug/atelier-server"):
        candidate = ROOT / rel
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return True
    return shutil.which("cargo") is not None and (ROOT / "rust" / "Cargo.toml").is_file()


class Response:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip()

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def json_or_none(self):
        try:
            return self.json()
        except (ValueError, UnicodeDecodeError):
            return None


class Backend:
    """Un backend (python ou rust) servi depuis sa propre copie du template."""

    def __init__(self, name: str, tag: str):
        assert name in ("python", "rust")
        self.name = name
        self.tag = tag
        self.port: int | None = None
        self.project: Path | None = None
        self.home: Path | None = None
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Backend":
        template = build_template()
        base = _session_dir() / f"{self.name}-{self.tag}"
        self.project = base / "project"
        self.home = base / "home"
        shutil.copytree(template, self.project)
        (self.home / ".claude").mkdir(parents=True)
        self.port = _free_port()
        overrides = {
            "ATELIER_BACKEND": self.name,
            "GALLERY_WATCH": "0",
            "GALLERY_NO_THUMBS": "1",
            "HOME": str(self.home),
            "ATELIER_AGENT_HOST": "codex",
            "ATELIER_AGENT_TOKEN": AGENT_TOKEN,
        }
        with mock.patch.dict(os.environ, overrides):
            command, env = cmux_gallery.backend_command(str(self.project), self.port)
        env.update(overrides)
        self.proc = subprocess.Popen(
            command, cwd=str(self.project), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        atexit.register(self.stop)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"backend {self.name} terminé prématurément (rc={self.proc.returncode})")
            try:
                if self.request("GET", "/ping").status == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"backend {self.name} injoignable sur /ping")

    def request(self, method: str, path: str, body=None, *,
                token: bool = False, origin: str | None = None,
                content_type: str = "application/json",
                timeout: float = 330.0) -> Response:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = content_type
        if token:
            headers["Authorization"] = f"Bearer {AGENT_TOKEN}"
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return Response(reply.status, dict(reply.headers), reply.read())
        except urllib.error.HTTPError as error:
            return Response(error.code, dict(error.headers), error.read())

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None


_BACKENDS: dict[tuple[str, str], Backend] = {}


def get_backend(name: str, tag: str = "inventory") -> Backend:
    key = (name, tag)
    if key not in _BACKENDS:
        _BACKENDS[key] = Backend(name, tag).start()
    return _BACKENDS[key]


def stop_all():
    for backend in _BACKENDS.values():
        backend.stop()
    _BACKENDS.clear()


# ---------------------------------------------------------------------------
# Registre des routes
# ---------------------------------------------------------------------------

@dataclass
class RouteSpec:
    method: str
    route: str            # chemin canonique tel que listé dans la matrice
    phase: str            # phase du plan qui possède cette route
    status: str           # ported | partial | missing | rust-only
    probe: dict | None = None      # {path, body?, raw?, token?, origin?, content_type?}
    python_expect: frozenset = frozenset({200})
    rust_expect: frozenset | None = None   # ported/partial seulement ; défaut = python_expect
    skip_live: str | None = None   # raison si la route ne peut pas être sondée
    notes: str = ""

    def __post_init__(self):
        assert self.status in STATUSES, self.status
        if self.rust_expect is None and self.status in ("ported", "partial"):
            self.rust_expect = self.python_expect


def _svg_payload() -> str:
    return (FIXTURES / "plot.svg").read_text()


def build_routes() -> list[RouteSpec]:
    """La liste complète des routes, alignée 1:1 sur docs/rust-route-parity.md."""
    sha = CONTEXT.get("sha", "0000000")
    R = RouteSpec
    ok = frozenset({200})
    routes = [
        # --- Cœur / santé -------------------------------------------------
        R("GET", "/ping", "0", "partial", {"path": "/ping"},
          notes="clés divergentes déclarées : service, backend, revision, claudePreview, watcher.*"),
        R("GET", "/rev", "0", "ported", {"path": "/rev"},
          notes="valeur volatile (mtime côté Python, compteur côté Rust)"),
        R("GET", "/health", "0", "rust-only", {"path": "/health"},
          notes="route Rust supplémentaire assumée (diagnostic)"),
        R("OPTIONS", "*", "8", "ported", {"path": "/state"},
          notes="préflight CORS global → 200 {}"),

        # --- Galerie / données ---------------------------------------------
        R("GET", "/", "2", "ported", {"path": "/"},
          notes="réécrit vers figures_index.html"),
        R("GET", "/data", "2", "ported", {"path": "/data"}),
        R("GET", "/state", "1", "ported", {"path": "/state"}),
        R("POST", "/state", "1", "ported",
          {"path": "/state", "body": {"favs": ["tiny.png"], "ratings": {"tiny.png": 5},
                                      "hidden": [], "tags": {}, "hideRules": [],
                                      "collections": {}, "workflow": {}}}),
        R("POST", "/rescan", "2", "ported", {"path": "/rescan", "body": {}},
          notes="synchron : relance build_gallery.py"),
        R("POST", "/agent-event", "2", "ported",
          {"path": "/agent-event", "body": {"rel": "tiny.png"}},
          notes="événement toast en mémoire {ok,id}"),
        R("POST", "/claude-event", "2", "ported",
          {"path": "/claude-event", "body": {"rel": "tiny.png"}},
          notes="alias historique de /agent-event"),
        R("GET", "/agent-events", "2", "ported", {"path": "/agent-events?since=0"}),
        R("GET", "/claude-events", "2", "ported", {"path": "/claude-events?since=0"},
          notes="alias historique de /agent-events"),
        R("GET", "/provenance", "2", "ported", {"path": "/provenance?rel=tiny.png"},
          python_expect=frozenset({200, 404}),
          notes="fixture sans provenance déclarée : contrat = artefact trouvé sans commande"),
        R("POST", "/regenerate", "2", "ported",
          {"path": "/regenerate", "body": {"rel": "tiny.png"}},
          python_expect=frozenset({409}),
          notes="fixture sans commande déclarée → 409 dans les deux backends"),
        R("GET", "/thumb", "2", "ported", {"path": "/thumb?path=tiny.png&w=64"}),
        R("GET", "/rasterize", "2", "ported",
          {"path": "/rasterize?path=report.html&w=400&h=300"},
          python_expect=frozenset({200, 501, 500}),
          notes="Chrome headless ; 501 si Chrome absent"),
        R("POST", "/delete", "2", "ported",
          {"path": "/delete", "body": {"rels": []}},
          python_expect=frozenset({200, 400})),
        R("POST", "/export", "2", "ported",
          {"path": "/export", "body": {"rels": [], "mode": "zip"}},
          python_expect=frozenset({400})),
        R("POST", "/open", "2", "ported",
          {"path": "/open", "body": {"rel": "../hors-projet.bin"}},
          python_expect=frozenset({400, 404}),
          notes="probe volontairement refusée (aucune app ouverte pendant les tests)"),
        R("POST", "/clear-quote", "2", "ported", {"path": "/clear-quote", "body": {}}),
        R("GET", "/claude-targets", "2", "ported", {"path": "/claude-targets"},
          notes="mode agent = NO_PUSH → targets: [] sans sous-processus"),
        R("GET", "/quote", "2", "ported", {"path": "/quote"}),

        # --- Fichiers / éditeurs (phase 1) ---------------------------------
        R("GET", "/ls", "1", "ported", {"path": "/ls?dir="},
          notes="sans query la requête tombe dans le fallback statique (dispatch startswith)"),
        R("GET", "/snippet", "1", "ported", {"path": "/snippet?path=report.md&n=5"}),
        R("GET", "/raw", "1", "ported", {"path": "/raw?path=mini.pdf"}),
        R("GET", "/code", "1", "ported", {"path": "/code?path=script.py"}),
        R("GET", "/texroot", "1", "ported", {"path": "/texroot?path=doc.tex"}),
        R("GET", "/findscript", "1", "ported", {"path": "/findscript?stem=tiny"}),
        R("GET", "/lint", "4", "ported",
          {"path": "/lint?path=/tmp/nope.py"},
          skip_live="Python n'enregistre /lint qu'en mode STUDIO ; Rust l'expose toujours",
          notes="Rust : available:false hors ~/Documents|Desktop *.py"),
        R("POST", "/codesave", "1", "ported",
          {"path": "/codesave",
           "body": {"path": "script.py",
                    "text": (FIXTURES / "script.py").read_text()}}),
        R("POST", "/save-svg", "1", "ported",
          {"path": "/save-svg", "body": {"rel": "plot.svg", "svg": _svg_payload()}}),
        R("POST", "/selinfo", "1", "ported",
          {"path": "/selinfo",
           "body": {"rel": "script.py", "lines": "L1-3", "text": "print"}}),

        # --- Git / versions (phase 3) ---------------------------------------
        R("GET", "/githead", "3", "ported", {"path": "/githead?path=script.py"}),
        R("GET", "/versions", "3", "ported", {"path": "/versions?path=script.py"}),
        R("POST", "/versions", "3", "ported",
          {"path": "/versions",
           "body": {"path": "script.py", "expectedRevision": 0, "ops": []}},
          python_expect=frozenset({200, 400, 409})),
        R("GET", "/gitlog", "3", "ported", {"path": "/gitlog?path=script.py"}),
        R("GET", "/gitshow", "3", "ported",
          {"path": f"/gitshow?path=script.py&sha={sha}"}),
        R("POST", "/commitmsg", "3", "ported", {"path": "/commitmsg", "body": {"path": "script.py"}},
          notes="arbre propre → ok:false sans invocation du CLI claude"),
        R("POST", "/gitcommit", "3", "ported",
          {"path": "/gitcommit", "body": {"path": "script.py", "message": "contract"}},
          notes="arbre propre → ok:false ; aucun commit créé"),

        # --- LaTeX / PDF (phase 4) ------------------------------------------
        R("POST", "/compile", "4", "ported",
          {"path": "/compile", "body": {"path": "/etc/hosts"}},
          python_expect=frozenset({403}),
          notes="probe hors projet → 403 rapide, sans lancer latexmk"),
        R("POST", "/synctex", "4", "ported",
          {"path": "/synctex",
           "body": {"tex": "/etc/hosts", "pdf": "/etc/hosts", "dir": "view",
                    "line": 1, "col": 1}},
          python_expect=frozenset({403})),
        R("GET", "/pdfannot", "4", "ported", {"path": "/pdfannot?rel=mini.pdf"}),
        R("POST", "/pdfannot", "4", "ported",
          {"path": "/pdfannot", "body": {"rel": "mini.pdf", "annots": []}},
          notes="Rust : garde loopback + cap 64 Mo (Python n'en a pas — écart de sécu assumé)"),
        R("POST", "/export-png", "4", "ported",
          {"path": "/export-png",
           "body": {"rel": "plot.svg", "svg": _svg_payload(), "dpi": 72}},
          python_expect=frozenset({200, 501}),
          notes="501 si rsvg-convert absent de la machine"),

        # --- Notes / whiteboard (phase 5) -----------------------------------
        R("GET", "/notes/load", "5", "ported", {"path": "/notes/load"}),
        R("POST", "/notes/save", "5", "ported",
          {"path": "/notes/save", "body": {"markdown": "# notes contract"}}),
        R("GET", "/board/load", "5", "ported", {"path": "/board/load"}),
        R("POST", "/board/save", "5", "ported",
          {"path": "/board/save", "body": {"snapshot": {}}}),
        R("GET", "/board/poll", "5", "ported", {"path": "/board/poll"}),
        R("POST", "/board/command", "5", "ported",
          {"path": "/board/command", "body": {"type": "contract-noop"}}),
        R("POST", "/notes/open-surface", "5", "ported",
          {"path": "/notes/open-surface", "body": {}},
          python_expect=frozenset({500}),
          notes="mode agent = NO_PUSH → 500 no-push assumé, aucun CLI lancé"),
        R("POST", "/board/open-surface", "5", "ported",
          {"path": "/board/open-surface", "body": {}},
          python_expect=frozenset({500})),

        # --- Zotero (phase 6) ------------------------------------------------
        R("GET", "/zotero-items", "6", "ported", {"path": "/zotero-items?q="},
          notes="HOME temporaire sans bibliothèque : contrat = dégradation contrôlée"),
        R("GET", "/zotero-collections", "6", "ported", {"path": "/zotero-collections"}),
        R("POST", "/zotero-fav", "6", "ported",
          {"path": "/zotero-fav", "body": {"key": "ABCD1234", "on": True}}),
        R("POST", "/zotero-add", "6", "ported", None,
          skip_live="POSTerait au connecteur Zotero réel (port 23119) ; sondé hors suite"),
        R("GET", "/zotero/<KEY>/<file>.pdf", "6", "ported",
          {"path": "/zotero/ABCD1234/nope.pdf"},
          python_expect=frozenset({404}),
          notes="Python n'enregistre la route qu'en STUDIO ; probe 404 hors studio"),

        # --- macOS / hôtes (phase 7) -----------------------------------------
        R("POST", "/orca-fullscreen-exit", "7", "ported",
          {"path": "/orca-fullscreen-exit", "body": {}}),
        R("POST", "/orca-native-fullscreen", "7", "ported",
          {"path": "/orca-native-fullscreen", "body": {"rel": "absent.png"}},
          python_expect=frozenset({400}),
          notes="probe refusée (fichier absent) : aucun viewer lancé"),

        # --- Bridge agent (phase 8) ------------------------------------------
        R("GET", "/agent-status", "8", "ported", {"path": "/agent-status?limit=10"}),
        R("GET", "/agent-selections", "8", "ported",
          {"path": "/agent-selections?consumer=contract&destination=contract",
           "token": True}),
        R("GET", "/agent-selection", "8", "ported",
          {"path": "/agent-selection", "token": True},
          notes="peek sans claim"),
        R("POST", "/agent-selection", "8", "ported",
          {"path": "/agent-selection",
           "body": {"rel": "tiny.png", "comment": "probe inventaire", "held": True},
           "token": True},
          notes="anchors/notes/delivery/cap 1 Mo alignés ; au-delà de 2 Mo axum répond 413 avant la garde"),
        R("POST", "/agent-consumers/register", "8", "ported",
          {"path": "/agent-consumers/register",
           "body": {"consumer": "contract", "destination": "contract"}, "token": True}),
        R("POST", "/agent-selections/ack", "8", "ported",
          {"path": "/agent-selections/ack",
           "body": {"ids": ["probe-inexistant"], "consumer": "contract"}, "token": True}),
        R("POST", "/agent-annotations/status", "8", "ported",
          {"path": "/agent-annotations/status",
           "body": {"ids": ["probe-inexistant"], "status": "completed"}, "token": True}),
        R("POST", "/agent-annotations/release", "8", "ported",
          {"path": "/agent-annotations/release",
           "body": {"ids": ["probe-inexistant"], "destination": "contract"}}),
        R("POST", "/agent-annotations/delete", "8", "ported",
          {"path": "/agent-annotations/delete", "body": {"ids": ["probe-inexistant"]}}),
        R("POST", "/agent-annotations/restore", "8", "ported",
          {"path": "/agent-annotations/restore", "body": {"ids": ["probe-inexistant"]}}),
        R("POST", "/agent-preferences", "8", "ported",
          {"path": "/agent-preferences",
           "body": {"destination": "contract", "automatic": False}},
          python_expect=frozenset({200, 400})),
        R("POST", "/agent-batches/release", "8", "ported",
          {"path": "/agent-batches/release", "body": {"batchId": "probe"}},
          python_expect=frozenset({200, 400})),
        R("POST", "/agent-batches/cancel", "8", "ported",
          {"path": "/agent-batches/cancel", "body": {"batchId": "probe"}},
          python_expect=frozenset({200, 400})),
        R("POST", "/quote", "8", "partial",
          {"path": "/quote",
           "body": {"rel": "report.md", "text": "probe", "embed": True}},
          notes="Rust : mise en file agent ; pbcopy/push hôte hors mode agent = optionnel"),
        R("POST", "/save", "8", "partial",
          {"path": "/save",
           "body": {"name": "contract", "dataURL": f"data:image/png;base64,{TINY_PNG_B64}",
                    "embed": True}},
          notes="écrit annotations/*.png ; push hôte hors mode agent = optionnel"),

        # --- Statique / médias ------------------------------------------------
        R("GET", "static:/tiny.png", "1", "ported", {"path": "/tiny.png"},
          notes="fallback statique confiné au projet"),
        R("HEAD", "static:/tiny.png", "1", "ported", {"path": "/tiny.png"}),
        R("GET", "static:/report.html", "1", "ported", {"path": "/report.html"},
          notes="injection sel_overlay.js avant </body>"),
        R("GET", "video:(Range)", "2", "ported", {"path": "/tiny.mp4"},
          notes="Accept-Ranges + 206 si Range (probe sans header = 200)"),
    ]
    return routes


def routes_by_key() -> dict[tuple[str, str], RouteSpec]:
    return {(spec.method, spec.route): spec for spec in build_routes()}


def probe(backend: Backend, spec: RouteSpec) -> Response:
    assert spec.probe, f"pas de probe pour {spec.method} {spec.route}"
    p = spec.probe
    return backend.request(
        p.get("method", spec.method),
        p["path"],
        body=p.get("body"),
        token=p.get("token", False),
        origin=p.get("origin"),
    )


# ---------------------------------------------------------------------------
# Parsing de la matrice
# ---------------------------------------------------------------------------

def parse_parity_matrix() -> list[dict]:
    """Extrait les lignes du tableau entre les marqueurs parity:begin/end."""
    text = PARITY_DOC.read_text(encoding="utf-8")
    begin = text.index("<!-- parity:begin -->")
    end = text.index("<!-- parity:end -->")
    rows = []
    for line in text[begin:end].splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| ---") or line.startswith("| Méthode"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        rows.append({
            "method": cells[0].strip("`"),
            "route": cells[1].strip("`"),
            "phase": cells[2],
            "status": cells[3].strip("`"),
        })
    return rows


# ---------------------------------------------------------------------------
# Normalisation pour la comparaison de contrats
# ---------------------------------------------------------------------------

def normalize(value, volatile: set[str], backend: Backend, path: str = ""):
    """Remplace les valeurs volatiles déclarées et les chemins machine.

    ``volatile`` contient des chemins pointés : "rev", "watcher.*",
    "items[].id". Les chaînes contenant le chemin du projet, le HOME
    temporaire ou le port sont réécrites vers des jetons stables.
    """
    def matches(candidate: str) -> bool:
        for rule in volatile:
            if rule == candidate:
                return True
            if rule.endswith(".*") and candidate.startswith(rule[:-2] + "."):
                return True
        return False

    if matches(path):
        return "<volatile>"
    if isinstance(value, dict):
        return {k: normalize(v, volatile, backend, f"{path}.{k}".lstrip("."))
                for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v, volatile, backend, f"{path}[]") for v in value]
    if isinstance(value, str):
        out = value.replace(str(backend.project), "<PROJECT>")
        out = out.replace(str(backend.home), "<HOME>")
        out = out.replace(f"127.0.0.1:{backend.port}", "127.0.0.1:<PORT>")
        return out
    return value
