"""
flow_web.py - Web local de flow-mackenzie.

  python flow_web.py            -> abre http://localhost:8765 en tu navegador

Inicio: proyectos (crear, abrir, duplicar, borrar), referencias y cuentas de Flow.
Cada proyecto, por pasos:
  1. Guion     - planos (foto + voz en off), se guarda solo mientras editas.
  2. Imágenes  - generar en Flow, galería, rehacer una sola imagen.
  3. Audio     - narración, música de fondo con ducking y vista previa.
  4. Render    - montaje.py -> montaje.mp4 + montaje.srt.

Todo se guarda en outputs/<proyecto>/ (guion.json, ajustes.json, imágenes, audio).
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
from urllib.parse import parse_qs, unquote, urlparse

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
AJUSTES_DEF = {"image_model": "Nano Banana 2.1", "ratio": "16:9", "transicion": "fundido",
               "srt": True, "subtitulos_previa": False,
               "musica_db": -14, "ducking": "media"}
# Archivos que no se copian al duplicar un proyecto
NO_COPIAR = {"_montaje_tmp", "_debug", "_rehacer", "_cuentas"}


def _safe_name(s: str) -> str:
    return "".join(c for c in str(s) if c.isalnum() or c in "_-") or "sin_nombre"


def _pdir(project: str) -> Path:
    return OUT_DIR / _safe_name(project)


def _url(path: Path) -> str:
    """URL servible de un archivo de outputs/, con marca para no usar caché vieja."""
    return f"/outputs/{path.relative_to(OUT_DIR).as_posix()}?v={int(path.stat().st_mtime)}"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)        # escritura atómica: nunca queda un archivo a medias


def find_image(pdir: Path, name: str) -> Path | None:
    """La imagen más reciente de un plano (puede estar en cualquier carpeta de cuenta)."""
    hits = []
    for ext in IMG_EXT:
        hits += [h for h in pdir.rglob(f"{name}{ext}") if not (set(h.parts) & NO_COPIAR)]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def find_audio(pdir: Path, stem: str) -> Path | None:
    hits = [p for p in pdir.glob(f"{stem}.*") if p.suffix.lower() in AUDIO_EXT]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _all_refs() -> dict[str, str]:
    return {p.stem: str(p) for p in REFS_DIR.glob("*") if p.suffix.lower() in IMG_EXT}


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def load_guion(project: str) -> dict:
    g = _read_json(_pdir(project) / "guion.json", {})
    g.setdefault("project", _safe_name(project))
    g.setdefault("jobs", [])
    return g


def load_ajustes(project: str) -> dict:
    return {**AJUSTES_DEF, **_read_json(_pdir(project) / "ajustes.json", {})}


# ---------------------------------------------------------------------------
# Generación de imágenes (un proceso "flow.py multi" cada vez, de un proyecto)
# ---------------------------------------------------------------------------
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.project = None
        self.log: list[str] = []
        self.planos: dict[str, dict] = {}     # name -> {cuenta, estado, detalle}
        self.current: dict[str, str] = {}
        self.links: dict[str, str] = {}
        self.finished = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _reset(self, project: str) -> None:
        if self.project != project:
            self.project, self.log, self.planos, self.links = project, [], {}, {}
        self.current, self.finished = {}, None

    def start(self, project: str, cuentas: list[str], solo_faltan: bool = False,
              solo: list[str] | None = None) -> None:
        """Un único lote: cada cuenta abre UN navegador y UN proyecto de Flow y
        genera seguidas todas las imágenes que le tocan (el trabajo se reparte)."""
        if self.running:
            raise RuntimeError(f"Ya se están generando imágenes del proyecto «{self.project}».")
        guion = load_guion(project)
        jobs = [j for j in guion["jobs"] if j.get("type", "image") == "image"]
        if not jobs:
            raise RuntimeError("El guion no tiene planos.")
        if solo:
            jobs = [j for j in jobs if j["name"] in set(solo)]
            if not jobs:
                raise RuntimeError("No encuentro esos planos en el guion.")
        if solo_faltan:
            jobs = [j for j in jobs if not find_image(_pdir(project), j["name"])]
            if not jobs:
                raise RuntimeError("No falta ninguna imagen.")
        accounts = flow_multi.list_accounts()
        cuentas = [c for c in cuentas if c in accounts]
        if not cuentas:
            raise RuntimeError("Ninguna de las cuentas elegidas tiene sesión.")
        cuentas = cuentas[:len(jobs)]          # no abrir navegadores sin trabajo
        refs = _all_refs()
        missing = sorted({r for j in jobs for r in (j.get("refs") or [])} - set(refs))
        if missing:
            raise RuntimeError("Faltan referencias: " + ", ".join(missing))
        aj = load_ajustes(project)
        lote = {"project": _safe_name(project), "references": refs,
                "defaults": {"ratio": aj["ratio"], "image_model": aj["image_model"]},
                "jobs": [{**j, "count": 1} for j in jobs]}
        pdir = _pdir(project)
        jobfile = pdir / "_cuentas" / "_lote.json"
        jobfile.parent.mkdir(parents=True, exist_ok=True)
        _write_json(jobfile, lote)
        with self.lock:
            self._reset(project)
            self.log, self.links = [], {}
            self.planos = {}
            for g_idx, group in enumerate(flow_multi.group_jobs(lote["jobs"])):
                for j in group:
                    self.planos[j["name"]] = {"cuenta": cuentas[g_idx % len(cuentas)],
                                              "estado": "pendiente", "detalle": ""}
        self._launch(jobfile, cuentas)

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
                self.log = (self.log + [line])[-2000:]
                if m := self._RE_JOB.match(line):
                    self.current[m["c"]] = m["n"]
                    self.planos.setdefault(m["n"], {}).update(estado="generando", cuenta=m["c"])
                elif m := self._RE_OK.match(line):
                    name = self.current.get(m["c"])
                    if name in self.planos:
                        self.planos[name].update(estado="hecho", detalle=m["f"])
                elif m := self._RE_ERR.match(line):
                    self.planos.setdefault(m["n"], {}).update(estado="error", detalle=m["e"])
                elif m := self._RE_URL.match(line):
                    self.links[m["c"]] = m["u"]
        proc.wait()
        with self.lock:
            self.finished = datetime.now().isoformat(timespec="seconds")
            for p in self.planos.values():
                if p.get("estado") in ("pendiente", "generando"):
                    p["estado"] = "sin terminar"

    def stop(self) -> None:
        if self.running:
            self.proc.terminate()


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

    def start(self, project: str, mode: str) -> None:
        if self.running:
            raise RuntimeError(f"Ya hay un render en marcha («{self.project}»).")
        pdir = _pdir(project)
        voz = find_audio(pdir, "narracion")
        if not voz:
            raise RuntimeError("Sube primero la narración (paso 3).")
        aj = load_ajustes(project)
        cmd = [sys.executable, str(BASE_DIR / "montaje.py"), str(pdir), str(voz),
               "--transicion", "corte" if aj["transicion"] == "corte" else "fundido"]
        musica = find_audio(pdir, "musica")
        if musica and mode == "render":
            cmd += ["--musica", str(musica), "--musica-db", str(float(aj["musica_db"])),
                    "--ducking", aj["ducking"] if aj["ducking"] in ("no", "suave", "media", "fuerte") else "media"]
        if mode == "tiempos":
            cmd.append("--solo-tiempos")
        if not aj.get("srt", True):
            cmd.append("--sin-srt")
        with self.lock:
            self.log, self.mode, self.project, self.exit_code = [], mode, _safe_name(project), None
        self.proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=SUBPROC_ENV, creationflags=NEW_GROUP)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc) -> None:
        for raw in proc.stdout:
            with self.lock:
                self.log = (self.log + [raw.decode("utf-8", errors="replace").rstrip()])[-500:]
        code = proc.wait()
        with self.lock:
            self.exit_code = code

    def stop(self) -> None:
        if self.running:
            self.proc.terminate()


RENDER = Render()


# ---------------------------------------------------------------------------
# Estado de un proyecto (desde disco + lo que esté en marcha)
# ---------------------------------------------------------------------------
def project_media(pdir: Path) -> dict:
    out = {}
    voz = find_audio(pdir, "narracion")
    if voz:
        out["narracion"] = {"url": _url(voz), "name": voz.name}
    mus = find_audio(pdir, "musica")
    if mus:
        out["musica"] = {"url": _url(mus), "name": mus.name}
    t = _read_json(pdir / "montaje_tiempos.json", None)
    tfile = pdir / "montaje_tiempos.json"
    if t and voz and t.get("audio") == voz.name and tfile.stat().st_mtime >= voz.stat().st_mtime:
        for p in t.get("planos", []):
            img = pdir / p["imagen"] if p.get("imagen") else None
            p["url"] = _url(img) if img and img.exists() else None
        out["tiempos"] = t
    video = pdir / "montaje.mp4"
    if video.exists():
        out["video"] = _url(video)
    srt = pdir / "montaje.srt"
    if srt.exists():
        out["srt"] = _url(srt)
    return out


def project_state(project: str, with_guion: bool = False) -> dict:
    pdir = _pdir(project)
    if not (pdir / "guion.json").exists():
        raise RuntimeError(f"El proyecto «{project}» no existe.")
    guion = load_guion(project)
    live = JOB.project == pdir.name
    planos = []
    with JOB.lock:
        for j in guion["jobs"]:
            if j.get("type", "image") != "image":
                continue
            name = j["name"]
            img = find_image(pdir, name)
            item = {"name": name, "estado": "hecho" if img else "pendiente", "detalle": "",
                    "cuenta": img.parent.name if img and img.parent != pdir else ""}
            if live and name in JOB.planos:
                item.update({k: v for k, v in JOB.planos[name].items() if v is not None})
                if item["estado"] == "sin terminar" and img:
                    item["estado"] = "hecho"
            if img:
                item["thumb"] = _url(img)
            planos.append(item)
        gen = {"running": JOB.running and live, "otro": JOB.project if JOB.running and not live else None,
               "log": JOB.log[-300:] if live else [], "links": JOB.links if live else {},
               "finished": JOB.finished if live else None}
    with RENDER.lock:
        rlive = RENDER.project == pdir.name
        render = {"running": RENDER.running and rlive, "mode": RENDER.mode if rlive else None,
                  "exit_code": RENDER.exit_code if rlive else None, "log": RENDER.log[-200:] if rlive else [],
                  "otro": RENDER.project if RENDER.running and not rlive else None}
    out = {"project": pdir.name, "planos": planos, "gen": gen, "render": render,
           "media": project_media(pdir)}
    if with_guion:
        g = {k: v for k, v in guion.items() if k != "references"}
        out["guion"] = g
        out["ajustes"] = load_ajustes(project)
    return out


def list_projects() -> list[dict]:
    items = []
    if not OUT_DIR.exists():
        return items
    for d in OUT_DIR.iterdir():
        g = d / "guion.json"
        if not (d.is_dir() and g.exists()):
            continue
        guion = _read_json(g, {})
        imgs = [j["name"] for j in guion.get("jobs", []) if j.get("type", "image") == "image"]
        hechas = [find_image(d, n) for n in imgs]
        primera = next((h for h in hechas if h), None)
        mtimes = [p.stat().st_mtime for p in [g, d / "ajustes.json", d / "montaje.mp4"] if p.exists()]
        items.append({"name": d.name, "planos": len(imgs), "imagenes": sum(1 for h in hechas if h),
                      "thumb": _url(primera) if primera else None, "video": (d / "montaje.mp4").exists(),
                      "narracion": bool(find_audio(d, "narracion")),
                      "modificado": datetime.fromtimestamp(max(mtimes)).strftime("%d/%m/%Y %H:%M"),
                      "_t": max(mtimes),
                      "ocupado": (JOB.running and JOB.project == d.name) or (RENDER.running and RENDER.project == d.name)})
    items.sort(key=lambda x: x["_t"], reverse=True)
    return items


def _nombre_libre(base: str) -> str:
    base = _safe_name(base)
    name, n = base, 2
    while _pdir(name).exists():
        name, n = f"{base}_{n}", n + 1
    return name


def _ocupado(project: str) -> bool:
    p = _safe_name(project)
    return (JOB.running and JOB.project == p) or (RENDER.running and RENDER.project == p)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and size:
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
        u = urlparse(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self._file(WEB_DIR / "index.html", WEB_DIR)
            if path.startswith("/refs/"):
                return self._file(REFS_DIR / path[len("/refs/"):], REFS_DIR)
            if path.startswith("/outputs/"):
                return self._file(OUT_DIR / path[len("/outputs/"):], OUT_DIR)
            if path == "/api/cuentas":
                return self._json({"cuentas": flow_multi.list_accounts()})
            if path == "/api/refs":
                refs = [{"name": p.stem, "url": f"/refs/{p.name}?v={int(p.stat().st_mtime)}"}
                        for p in sorted(REFS_DIR.glob("*")) if p.suffix.lower() in IMG_EXT]
                return self._json({"refs": refs})
            if path == "/api/proyectos":
                return self._json({"proyectos": list_projects()})
            if path == "/api/proyecto":
                return self._json(project_state(q.get("name", [""])[0], with_guion="guion" in q))
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "no encontrado"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._body()
        except Exception:
            return self._json({"error": "Datos no válidos (¿archivo demasiado grande?)"}, 400)
        try:
            return self._post(path, data)
        except Exception as e:
            return self._json({"error": str(e)}, 400)

    def _post(self, path: str, data: dict):
        name = data.get("name", "")
        # --- referencias ---
        if path == "/api/ref":
            ref = _safe_name(data.get("ref", ""))
            ext = Path(data.get("filename", "")).suffix.lower()
            if ext not in IMG_EXT:
                return self._json({"error": "Formato no admitido (png, jpg, webp)."}, 400)
            REFS_DIR.mkdir(exist_ok=True)
            for old in REFS_DIR.glob(f"{ref}.*"):
                old.unlink()
            (REFS_DIR / f"{ref}{ext}").write_bytes(base64.b64decode(data["data"]))
            return self._json({"ok": True})
        if path == "/api/ref/borrar":
            for old in REFS_DIR.glob(f"{_safe_name(data.get('ref', ''))}.*"):
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
                return self._json({"error": "No se pudo borrar: cierra la ventana de Chrome de esa cuenta."}, 400)
            return self._json({"ok": True})
        # --- proyectos ---
        if path == "/api/proyecto/crear":
            nuevo = _nombre_libre(name or f"proyecto_{datetime.now():%Y%m%d_%H%M}")
            _pdir(nuevo).mkdir(parents=True)
            _write_json(_pdir(nuevo) / "guion.json", {"project": nuevo, "jobs": data.get("jobs") or []})
            _write_json(_pdir(nuevo) / "ajustes.json", AJUSTES_DEF)
            return self._json({"ok": True, "name": nuevo})
        if path == "/api/proyecto/duplicar":
            src = _pdir(name)
            if not (src / "guion.json").exists():
                return self._json({"error": "Ese proyecto no existe."}, 400)
            nuevo = _nombre_libre(data.get("nuevo") or f"{src.name}_copia")
            shutil.copytree(src, _pdir(nuevo), ignore=shutil.ignore_patterns(*NO_COPIAR, "*.tmp"))
            g = load_guion(nuevo)
            g["project"] = nuevo
            _write_json(_pdir(nuevo) / "guion.json", g)
            return self._json({"ok": True, "name": nuevo})
        if path == "/api/proyecto/borrar":
            if _ocupado(name):
                return self._json({"error": "Ese proyecto está generando o renderizando ahora mismo."}, 400)
            d = _pdir(name)
            if not (d / "guion.json").exists():
                return self._json({"error": "Ese proyecto no existe."}, 400)
            _rmtree(d)
            return self._json({"ok": True})
        if path == "/api/proyecto/guardar":
            pdir = _pdir(name)
            if not (pdir / "guion.json").exists():
                return self._json({"error": "Ese proyecto no existe."}, 400)
            if "guion" in data:
                old = load_guion(name)
                g = {**old, "jobs": data["guion"].get("jobs", []), "project": pdir.name}
                _write_json(pdir / "guion.json", g)
            if "ajustes" in data:
                aj = {**load_ajustes(name), **{k: v for k, v in data["ajustes"].items() if k in AJUSTES_DEF}}
                _write_json(pdir / "ajustes.json", aj)
            return self._json({"ok": True, "guardado": datetime.now().strftime("%H:%M:%S")})
        # --- generación ---
        if path == "/api/generar":
            cuentas = [c for c in data.get("cuentas", []) if c]
            if not cuentas:
                return self._json({"error": "Elige al menos una cuenta."}, 400)
            JOB.start(name, cuentas, bool(data.get("solo_faltan")))
            return self._json({"ok": True})
        if path == "/api/rehacer":
            planos = data.get("planos") or ([data["plano"]] if data.get("plano") else [])
            cuentas = [c for c in data.get("cuentas", []) if c] or flow_multi.list_accounts()
            JOB.start(name, cuentas, solo=planos)
            if planos:
                with JOB.lock:
                    JOB.log.insert(0, f"===== Rehacer {', '.join(planos)} =====")
            return self._json({"ok": True})
        if path == "/api/parar":
            if JOB.project == _safe_name(name):
                JOB.stop()
            return self._json({"ok": True})
        # --- audio, tiempos y render ---
        if path in ("/api/narracion", "/api/musica"):
            stem = "narracion" if path == "/api/narracion" else "musica"
            ext = Path(data.get("filename", "")).suffix.lower()
            if ext not in AUDIO_EXT:
                return self._json({"error": "Formato no admitido (mp3, wav, m4a, aac, ogg, flac)."}, 400)
            pdir = _pdir(name)
            for old in pdir.glob(f"{stem}.*"):
                old.unlink()
            (pdir / f"{stem}{ext}").write_bytes(base64.b64decode(data["data"]))
            return self._json({"ok": True})
        if path == "/api/musica/borrar":
            for old in _pdir(name).glob("musica.*"):
                old.unlink()
            return self._json({"ok": True})
        if path in ("/api/tiempos", "/api/render"):
            RENDER.start(name, "tiempos" if path == "/api/tiempos" else "render")
            return self._json({"ok": True})
        if path == "/api/render/parar":
            if RENDER.project == _safe_name(name):
                RENDER.stop()
            return self._json({"ok": True})
        return self._json({"error": "no encontrado"}, 404)


def main() -> int:
    REFS_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
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
