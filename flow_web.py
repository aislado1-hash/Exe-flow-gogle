"""
flow_web.py - Web local de flow-mackenzie.

  python flow_web.py            -> abre http://localhost:8765 en tu navegador

Desde la web:
  - Subes las referencias (mackenzie, cabina, camión…) a la carpeta refs/.
  - Ves tus cuentas de Flow y añades nuevas (abre Chrome para iniciar sesión).
  - Pegas o cargas el guion JSON, ajustas modelos/duración/resolución.
  - Lanzas el trabajo (comando multi) y sigues el estado de cada plano.

Solo escucha en 127.0.0.1 (tu PC). Usa solo librería estándar.
"""
import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import flow_multi

BASE_DIR = Path(__file__).parent.resolve()
WEB_DIR = BASE_DIR / "web"
REFS_DIR = BASE_DIR / "refs"
OUT_DIR = BASE_DIR / "outputs"
PORT = int(os.getenv("FLOW_WEB_PORT", "8765"))
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def _safe_name(s: str) -> str:
    return "".join(c for c in s if c.isalnum() or c in "_-") or "sin_nombre"


# ---------------------------------------------------------------------------
# Trabajo en marcha (un proceso "flow.py multi" cada vez)
# ---------------------------------------------------------------------------
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.log: list[str] = []
        self.planos: dict[str, dict] = {}     # name -> {type, cuenta, estado, detalle}
        self.current: dict[str, str] = {}     # cuenta -> plano en curso
        self.links: dict[str, str] = {}       # cuenta -> url proyecto Flow
        self.project = None
        self.started = None
        self.finished = None
        self.exit_code = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, guion: dict, cuentas: list[str]) -> None:
        if self.running:
            raise RuntimeError("Ya hay un trabajo en marcha.")
        project = _safe_name(guion.get("project") or f"guion_{datetime.now():%Y%m%d_%H%M}")
        guion["project"] = project
        # Referencias: las de refs/ + las que traiga el guion (estas mandan).
        refs = {p.stem: str(p) for p in REFS_DIR.glob("*") if p.suffix.lower() in IMG_EXT}
        refs.update(guion.get("references") or {})
        guion["references"] = refs
        used = {r for j in guion.get("jobs", []) for r in (j.get("refs") or [])}
        used |= {j["start_ref"] for j in guion.get("jobs", []) if j.get("start_ref")}
        missing = sorted(used - set(refs))
        if missing:
            raise RuntimeError("Faltan referencias: " + ", ".join(missing))

        proj_dir = OUT_DIR / project
        proj_dir.mkdir(parents=True, exist_ok=True)
        jobfile = proj_dir / "guion.json"
        jobfile.write_text(json.dumps(guion, indent=2, ensure_ascii=False), encoding="utf-8")

        with self.lock:
            self.log = []
            self.links = {}
            self.current = {}
            self.project = project
            self.started = datetime.now().isoformat(timespec="seconds")
            self.finished = None
            self.exit_code = None
            self.planos = {}
            for g_idx, group in enumerate(flow_multi.group_jobs(guion.get("jobs", []))):
                cuenta = cuentas[g_idx % len(cuentas)]
                for j in group:
                    self.planos[j["name"]] = {"type": j.get("type", "image"), "cuenta": cuenta,
                                              "estado": "pendiente", "detalle": "",
                                              "prompt": j.get("prompt", "")}

        self.proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "flow.py"), "multi", str(jobfile),
             "--cuentas", ",".join(cuentas)],
            cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        threading.Thread(target=self._reader, daemon=True).start()

    _RE_JOB = re.compile(r"^\[(?P<c>[^\]]+)\] --- \[\d+/\d+\] (?P<t>\w+) :: (?P<n>.+?) ---")
    _RE_OK = re.compile(r"^\[(?P<c>[^\]]+)\]\s+OK -> (?P<f>.+)$")
    _RE_ERR = re.compile(r"^\[(?P<c>[^\]]+)\]\s+ERROR en '(?P<n>[^']+)': (?P<e>.*)$")
    _RE_URL = re.compile(r"^\[(?P<c>[^\]]+)\] Proyecto en Flow: (?P<u>\S+)")

    def _reader(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            with self.lock:
                self.log.append(line)
                if len(self.log) > 2000:
                    self.log = self.log[-2000:]
                if m := self._RE_JOB.match(line):
                    self.current[m["c"]] = m["n"]
                    if m["n"] in self.planos:
                        self.planos[m["n"]].update(estado="generando", cuenta=m["c"])
                elif m := self._RE_OK.match(line):
                    name = self.current.get(m["c"])
                    if name in self.planos:
                        self.planos[name].update(estado="hecho", detalle=m["f"])
                elif m := self._RE_ERR.match(line):
                    if m["n"] in self.planos:
                        self.planos[m["n"]].update(estado="error", detalle=m["e"])
                elif m := self._RE_URL.match(line):
                    self.links[m["c"]] = m["u"]
        code = self.proc.wait()
        with self.lock:
            self.exit_code = code
            self.finished = datetime.now().isoformat(timespec="seconds")
            for p in self.planos.values():
                if p["estado"] in ("pendiente", "generando"):
                    p["estado"] = "sin terminar"

    def stop(self) -> None:
        if self.running:
            self.proc.terminate()

    def snapshot(self) -> dict:
        with self.lock:
            planos = []
            for name, p in self.planos.items():
                item = {"name": name, **p}
                img = OUT_DIR / (self.project or "") / p["cuenta"] / f"{name}.png"
                if p["type"] == "image" and img.exists():
                    item["thumb"] = "/outputs/" + img.relative_to(OUT_DIR).as_posix()
                planos.append(item)
            return {"running": self.running, "project": self.project, "started": self.started,
                    "finished": self.finished, "exit_code": self.exit_code,
                    "planos": planos, "links": self.links, "log": self.log[-300:]}


JOB = Job()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # silencio en consola
        pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, root: Path):
        try:
            path = path.resolve()
            path.relative_to(root.resolve())
        except Exception:
            return self._json({"error": "ruta no válida"}, 403)
        if not path.is_file():
            return self._json({"error": "no existe"}, 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self._file(WEB_DIR / "index.html", WEB_DIR)
        if path.startswith("/refs/"):
            return self._file(REFS_DIR / path[len("/refs/"):], REFS_DIR)
        if path.startswith("/outputs/"):
            return self._file(OUT_DIR / path[len("/outputs/"):], OUT_DIR)
        if path == "/api/cuentas":
            return self._json({"cuentas": flow_multi.list_accounts()})
        if path == "/api/refs":
            refs = [{"name": p.stem, "file": p.name, "url": f"/refs/{p.name}"}
                    for p in sorted(REFS_DIR.glob("*")) if p.suffix.lower() in IMG_EXT]
            return self._json({"refs": refs})
        if path == "/api/estado":
            return self._json(JOB.snapshot())
        return self._json({"error": "no encontrado"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._body()
        except Exception:
            return self._json({"error": "JSON no válido"}, 400)
        try:
            if path == "/api/ref":
                name = _safe_name(data.get("name", ""))
                ext = Path(data.get("filename", "")).suffix.lower()
                if ext not in IMG_EXT:
                    return self._json({"error": "Formato no admitido (png, jpg, webp)."}, 400)
                REFS_DIR.mkdir(exist_ok=True)
                for old in REFS_DIR.glob(f"{name}.*"):
                    old.unlink()
                (REFS_DIR / f"{name}{ext}").write_bytes(base64.b64decode(data["data"]))
                return self._json({"ok": True})
            if path == "/api/ref/borrar":
                for old in REFS_DIR.glob(f"{_safe_name(data.get('name', ''))}.*"):
                    old.unlink()
                return self._json({"ok": True})
            if path == "/api/login":
                cuenta = _safe_name(data.get("cuenta", ""))
                subprocess.Popen([sys.executable, str(BASE_DIR / "flow.py"), "login", "--cuenta", cuenta],
                                 cwd=str(BASE_DIR))
                return self._json({"ok": True, "cuenta": cuenta})
            if path == "/api/start":
                cuentas = [c for c in data.get("cuentas", []) if c]
                if not cuentas:
                    return self._json({"error": "Elige al menos una cuenta."}, 400)
                JOB.start(data["guion"], cuentas)
                return self._json({"ok": True})
            if path == "/api/stop":
                JOB.stop()
                return self._json({"ok": True})
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "no encontrado"}, 404)


def main() -> int:
    REFS_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"flow-mackenzie web en {url}  (Ctrl+C para cerrar)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        JOB.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
