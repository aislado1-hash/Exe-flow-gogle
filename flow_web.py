"""
flow_web.py - Web local de flow-mackenzie.

  python flow_web.py            -> abre http://localhost:8765 en tu navegador

Desde la web:
  - Referencias (mackenzie, cabina, camión…): subir, cambiar y borrar (carpeta refs/).
  - Cuentas de Flow: añadir (abre Chrome para iniciar sesión) y borrar.
  - Guion: pegar/cargar el JSON y editar cada plano (solo imágenes).
  - Generar las imágenes en Flow, ver el progreso y REHACER una sola imagen.
  - Abrir un proyecto anterior desde outputs/.
  - Subir la narración, ver la VISTA PREVIA sincronizada y lanzar el RENDER (montaje.py).

Solo escucha en 127.0.0.1 (tu PC). Usa solo librería estándar.
"""
import base64
import json
import mimetypes
import os
import re
import shutil
import stat
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
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
SUBPROC_ENV = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _safe_name(s: str) -> str:
    return "".join(c for c in str(s) if c.isalnum() or c in "_-") or "sin_nombre"


def _project_dir(project: str) -> Path:
    return OUT_DIR / _safe_name(project)


def _url(path: Path) -> str:
    """URL servible de un archivo de outputs/, con marca para no usar caché vieja."""
    return f"/outputs/{path.relative_to(OUT_DIR).as_posix()}?v={int(path.stat().st_mtime)}"


def find_image(proj_dir: Path, name: str) -> Path | None:
    """La imagen más reciente de un plano (puede estar en cualquier carpeta de cuenta)."""
    hits = []
    for ext in IMG_EXT:
        hits += [h for h in proj_dir.rglob(f"{name}{ext}")
                 if "_debug" not in h.parts and "_montaje_tmp" not in h.parts]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def find_narration(proj_dir: Path) -> Path | None:
    hits = [p for p in proj_dir.glob("narracion.*") if p.suffix.lower() in AUDIO_EXT]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _all_refs() -> dict[str, str]:
    return {p.stem: str(p) for p in REFS_DIR.glob("*") if p.suffix.lower() in IMG_EXT}


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


# ---------------------------------------------------------------------------
# Generación de imágenes (un proceso "flow.py multi" cada vez)
# ---------------------------------------------------------------------------
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.log: list[str] = []
        self.planos: dict[str, dict] = {}     # name -> {type, cuenta, estado, detalle, prompt}
        self.order: list[str] = []
        self.current: dict[str, str] = {}     # cuenta -> plano en curso
        self.links: dict[str, str] = {}       # cuenta -> url proyecto Flow
        self.project = None
        self.started = None
        self.finished = None
        self.exit_code = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # --- proyecto -----------------------------------------------------------
    def _set_planos(self, jobs: list[dict], estado_fn) -> None:
        self.planos, self.order = {}, []
        for j in jobs:
            if j.get("type", "image") != "image":
                continue
            self.planos[j["name"]] = {"type": "image", "cuenta": "", "estado": estado_fn(j),
                                      "detalle": "", "prompt": j.get("prompt", "")}
            self.order.append(j["name"])

    def open(self, project: str) -> None:
        if self.running:
            raise RuntimeError("Espera a que termine la generación en marcha.")
        proj_dir = _project_dir(project)
        guion_path = proj_dir / "guion.json"
        if not guion_path.exists():
            raise RuntimeError(f"El proyecto {project} no tiene guion.json.")
        guion = json.loads(guion_path.read_text(encoding="utf-8"))
        with self.lock:
            self.project = proj_dir.name
            self.log, self.links, self.current = [], {}, {}
            self.started = self.finished = self.exit_code = None
            self._set_planos(guion.get("jobs", []),
                             lambda j: "hecho" if find_image(proj_dir, j["name"]) else "pendiente")
            for name, p in self.planos.items():
                img = find_image(proj_dir, name)
                if img and img.parent != proj_dir:
                    p["cuenta"] = img.parent.name

    def start(self, guion: dict, cuentas: list[str]) -> None:
        if self.running:
            raise RuntimeError("Ya hay un trabajo en marcha.")
        project = _safe_name(guion.get("project") or f"guion_{datetime.now():%Y%m%d_%H%M}")
        guion["project"] = project
        refs = _all_refs()
        refs.update(guion.get("references") or {})
        guion["references"] = refs
        missing = sorted({r for j in guion.get("jobs", []) for r in (j.get("refs") or [])} - set(refs))
        if missing:
            raise RuntimeError("Faltan referencias: " + ", ".join(missing))

        proj_dir = _project_dir(project)
        proj_dir.mkdir(parents=True, exist_ok=True)
        jobfile = proj_dir / "guion.json"
        jobfile.write_text(json.dumps(guion, indent=2, ensure_ascii=False), encoding="utf-8")

        with self.lock:
            self.log, self.links, self.current = [], {}, {}
            self.project = project
            self.started = datetime.now().isoformat(timespec="seconds")
            self.finished = self.exit_code = None
            self._set_planos(guion.get("jobs", []), lambda j: "pendiente")
            for g_idx, group in enumerate(flow_multi.group_jobs(guion.get("jobs", []))):
                for j in group:
                    if j["name"] in self.planos:
                        self.planos[j["name"]]["cuenta"] = cuentas[g_idx % len(cuentas)]
        self._launch(jobfile, cuentas)

    def rehacer(self, name: str, job_override: dict | None, cuentas: list[str]) -> None:
        """Regenera SOLO una imagen. Actualiza su entrada en guion.json (si viene un
        prompt/refs nuevos) y deja intactas las demás."""
        if self.running:
            raise RuntimeError("Espera a que termine la generación en marcha.")
        if not self.project:
            raise RuntimeError("Abre primero el proyecto.")
        proj_dir = _project_dir(self.project)
        guion_path = proj_dir / "guion.json"
        guion = json.loads(guion_path.read_text(encoding="utf-8"))
        job = next((j for j in guion.get("jobs", []) if j.get("name") == name), None)
        if job is None:
            raise RuntimeError(f"No encuentro el plano {name} en el guion.")
        if job_override:
            for k in ("prompt", "refs", "texto", "efecto"):
                if k in job_override:
                    job[k] = job_override[k]
            guion_path.write_text(json.dumps(guion, indent=2, ensure_ascii=False), encoding="utf-8")

        refs = _all_refs()
        refs.update(guion.get("references") or {})
        missing = sorted(set(job.get("refs") or []) - set(refs))
        if missing:
            raise RuntimeError("Faltan referencias: " + ", ".join(missing))
        cuenta = (self.planos.get(name, {}).get("cuenta") or "")
        accounts = flow_multi.list_accounts()
        if cuenta not in accounts:
            cuenta = next((c for c in cuentas if c in accounts), None) or (accounts[0] if accounts else None)
        if not cuenta:
            raise RuntimeError("No hay ninguna cuenta con sesión.")

        mini = {"project": self.project, "references": refs, "defaults": guion.get("defaults", {}),
                "jobs": [{**job, "count": 1}]}
        mini_file = proj_dir / "_rehacer" / f"{_safe_name(name)}.json"
        mini_file.parent.mkdir(exist_ok=True)
        mini_file.write_text(json.dumps(mini, indent=2, ensure_ascii=False), encoding="utf-8")

        with self.lock:
            self.log.append(f"===== Rehacer {name} con {cuenta} =====")
            self.current = {}
            self.started = datetime.now().isoformat(timespec="seconds")
            self.finished = self.exit_code = None
            p = self.planos.setdefault(name, {"type": "image"})
            p.update(cuenta=cuenta, estado="pendiente", detalle="", prompt=job.get("prompt", ""))
            if name not in self.order:
                self.order.append(name)
        self._launch(mini_file, [cuenta])

    def _launch(self, jobfile: Path, cuentas: list[str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "flow.py"), "multi", str(jobfile), "--cuentas", ",".join(cuentas)],
            cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=SUBPROC_ENV, creationflags=NEW_GROUP)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    _RE_JOB = re.compile(r"^\[(?P<c>[^\]]+)\] --- \[\d+/\d+\] (?P<t>\w+) :: (?P<n>.+?) ---")
    _RE_OK = re.compile(r"^\[(?P<c>[^\]]+)\]\s+OK -> (?P<f>.+)$")
    _RE_ERR = re.compile(r"^\[(?P<c>[^\]]+)\]\s+ERROR en '(?P<n>[^']+)': (?P<e>.*)$")
    _RE_URL = re.compile(r"^\[(?P<c>[^\]]+)\] Proyecto en Flow: (?P<u>\S+)")

    def _reader(self, proc) -> None:
        for raw in proc.stdout:
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
        code = proc.wait()
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
            proj_dir = _project_dir(self.project) if self.project else None
            planos = []
            for name in self.order:
                p = self.planos[name]
                item = {"name": name, **p}
                img = find_image(proj_dir, name) if proj_dir else None
                if img:
                    item["thumb"] = _url(img)
                planos.append(item)
            return {"running": self.running, "project": self.project, "started": self.started,
                    "finished": self.finished, "exit_code": self.exit_code,
                    "planos": planos, "links": self.links, "log": self.log[-300:]}


JOB = Job()


# ---------------------------------------------------------------------------
# Tiempos y render (un proceso "montaje.py" cada vez)
# ---------------------------------------------------------------------------
class Render:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.log: list[str] = []
        self.mode = None
        self.project = None
        self.exit_code = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, project: str, mode: str, transicion: str = "fundido") -> None:
        if self.running:
            raise RuntimeError("Ya hay un render o un cálculo de tiempos en marcha.")
        proj_dir = _project_dir(project)
        audio = find_narration(proj_dir)
        if not audio:
            raise RuntimeError("Sube primero la narración.")
        if not (proj_dir / "guion.json").exists():
            raise RuntimeError("El proyecto no tiene guion.json.")
        cmd = [sys.executable, str(BASE_DIR / "montaje.py"), str(proj_dir), str(audio),
               "--transicion", "corte" if transicion == "corte" else "fundido"]
        if mode == "tiempos":
            cmd.append("--solo-tiempos")
        with self.lock:
            self.log, self.mode, self.project, self.exit_code = [], mode, proj_dir.name, None
        self.proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=SUBPROC_ENV, creationflags=NEW_GROUP)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc) -> None:
        for raw in proc.stdout:
            with self.lock:
                self.log.append(raw.decode("utf-8", errors="replace").rstrip())
                self.log = self.log[-500:]
        code = proc.wait()
        with self.lock:
            self.exit_code = code

    def stop(self) -> None:
        if self.running:
            self.proc.terminate()

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "mode": self.mode, "project": self.project,
                    "exit_code": self.exit_code, "log": self.log[-200:]}


RENDER = Render()


def project_media(project: str | None) -> dict:
    """Narración, tiempos de la vista previa, vídeo y subtítulos de un proyecto."""
    if not project:
        return {}
    proj_dir = _project_dir(project)
    out = {}
    audio = find_narration(proj_dir)
    if audio:
        out["narracion"] = {"url": _url(audio), "name": audio.name}
    tiempos = proj_dir / "montaje_tiempos.json"
    if tiempos.exists():
        try:
            t = json.loads(tiempos.read_text(encoding="utf-8"))
            if audio and t.get("audio") == audio.name and tiempos.stat().st_mtime >= audio.stat().st_mtime:
                for p in t.get("planos", []):
                    img = proj_dir / p["imagen"] if p.get("imagen") else None
                    p["url"] = _url(img) if img and img.exists() else None
                out["tiempos"] = t
        except Exception:
            pass
    video = proj_dir / "montaje.mp4"
    if video.exists():
        out["video"] = _url(video)
    srt = proj_dir / "montaje.srt"
    if srt.exists():
        out["srt"] = _url(srt)
    return out


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
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() == ".srt":
            ctype = "text/plain; charset=utf-8"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and size:   # audio y vídeo necesitan rangos para poder saltar
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(size - int(m.group(2) or 0), 0)
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1 if size else 0
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            refs = [{"name": p.stem, "file": p.name, "url": f"/refs/{p.name}?v={int(p.stat().st_mtime)}"}
                    for p in sorted(REFS_DIR.glob("*")) if p.suffix.lower() in IMG_EXT]
            return self._json({"refs": refs})
        if path == "/api/proyectos":
            items = []
            if OUT_DIR.exists():
                for d in OUT_DIR.iterdir():
                    g = d / "guion.json"
                    if d.is_dir() and g.exists():
                        items.append((g.stat().st_mtime, d.name))
            return self._json({"proyectos": [n for _, n in sorted(items, reverse=True)]})
        if path == "/api/estado":
            snap = JOB.snapshot()
            snap["render"] = RENDER.snapshot()
            snap["media"] = project_media(JOB.project)
            return self._json(snap)
        return self._json({"error": "no encontrado"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._body()
        except Exception:
            return self._json({"error": "Datos no válidos (¿archivo demasiado grande?)"}, 400)
        try:
            # --- referencias ---
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
            # --- cuentas ---
            if path == "/api/login":
                cuenta = _safe_name(data.get("cuenta", ""))
                subprocess.Popen([sys.executable, str(BASE_DIR / "flow.py"), "login", "--cuenta", cuenta],
                                 cwd=str(BASE_DIR))
                return self._json({"ok": True, "cuenta": cuenta})
            if path == "/api/cuenta/borrar":
                if JOB.running:
                    return self._json({"error": "No se puede borrar una cuenta mientras se generan imágenes."}, 400)
                d = flow_multi.profile_dir(data.get("cuenta", ""))
                if not d.exists():
                    return self._json({"error": "Esa cuenta no existe."}, 400)
                try:
                    _rmtree(d)
                except Exception:
                    return self._json({"error": "No se pudo borrar: cierra la ventana de Chrome de esa cuenta y vuelve a intentarlo."}, 400)
                return self._json({"ok": True})
            # --- proyecto y generación ---
            if path == "/api/abrir":
                JOB.open(data.get("project", ""))
                return self._json({"ok": True})
            if path == "/api/start":
                cuentas = [c for c in data.get("cuentas", []) if c]
                if not cuentas:
                    return self._json({"error": "Elige al menos una cuenta."}, 400)
                JOB.start(data["guion"], cuentas)
                return self._json({"ok": True})
            if path == "/api/rehacer":
                JOB.rehacer(data.get("name", ""), data.get("job"), data.get("cuentas") or [])
                return self._json({"ok": True})
            if path == "/api/stop":
                JOB.stop()
                return self._json({"ok": True})
            # --- narración, vista previa y render ---
            if path == "/api/narracion":
                if not JOB.project:
                    return self._json({"error": "Abre o crea primero un proyecto."}, 400)
                ext = Path(data.get("filename", "")).suffix.lower()
                if ext not in AUDIO_EXT:
                    return self._json({"error": "Formato no admitido (mp3, wav, m4a, aac, ogg, flac)."}, 400)
                proj_dir = _project_dir(JOB.project)
                proj_dir.mkdir(parents=True, exist_ok=True)
                for old in proj_dir.glob("narracion.*"):
                    old.unlink()
                (proj_dir / f"narracion{ext}").write_bytes(base64.b64decode(data["data"]))
                return self._json({"ok": True})
            if path in ("/api/tiempos", "/api/render"):
                if not JOB.project:
                    return self._json({"error": "Abre o crea primero un proyecto."}, 400)
                RENDER.start(JOB.project, "tiempos" if path == "/api/tiempos" else "render",
                             data.get("transicion", "fundido"))
                return self._json({"ok": True})
            if path == "/api/render/stop":
                RENDER.stop()
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
        RENDER.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
