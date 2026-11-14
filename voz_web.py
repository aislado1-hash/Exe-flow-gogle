"""
voz_web.py — La parte del servidor web para generar la narración con ai33.pro.

flow_web.py llama a manejar_get() / manejar_post() para las rutas /api/ai33/*
y a estado_voz() para meter el bloque «voz» en el estado del proyecto.

Archivos de cada proyecto:
  outputs/<proyecto>/_voz/historial.json   lista de audios generados
  outputs/<proyecto>/_voz/<archivo>.mp3    cada audio
  outputs/<proyecto>/_voz/texto.txt        el texto que se está escribiendo
"""
from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path

import voz_ai33 as ai33


class VozJob:
    """Una generación de voz a la vez (en un hilo)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.project: str | None = None
        self.running = False
        self.estado = ""          # creando | generando | descargando | hecho | error
        self.progreso = 0
        self.error = ""
        self._parar = False

    def info(self, project: str) -> dict:
        with self.lock:
            if self.project != project:
                return {"running": False, "otro": self.project if self.running else None}
            return {"running": self.running, "estado": self.estado, "progreso": self.progreso,
                    "error": self.error, "otro": None}


JOB = VozJob()


def _vdir(pdir: Path) -> Path:
    return pdir / "_voz"


def _hist(pdir: Path) -> list[dict]:
    try:
        d = json.loads((_vdir(pdir) / "historial.json").read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _guardar_hist(pdir: Path, items: list[dict]) -> None:
    _vdir(pdir).mkdir(parents=True, exist_ok=True)
    (_vdir(pdir) / "historial.json").write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def estado_voz(pdir: Path, project: str, url_de, narracion: Path | None) -> dict:
    """Bloque «voz» del estado del proyecto para la web."""
    try:
        texto = (_vdir(pdir) / "texto.txt").read_text(encoding="utf-8")
    except Exception:
        texto = ""
    items = []
    for it in reversed(_hist(pdir)):
        f = _vdir(pdir) / it.get("archivo", "")
        if not f.exists():
            continue
        es_narr = bool(narracion and narracion.exists() and it.get("usada") and
                       narracion.stat().st_size == f.stat().st_size)
        items.append({**it, "url": url_de(f), "texto": (it.get("texto") or "")[:160], "narracion": es_narr})
    return {"texto": texto, "items": items, "job": JOB.info(project)}


def _usar_como_narracion(pdir: Path, f: Path, audio_ext: set) -> None:
    for old in pdir.glob("narracion.*"):
        if old.suffix.lower() in audio_ext:
            old.unlink()
    shutil.copyfile(f, pdir / f"narracion{f.suffix.lower()}")
    items = _hist(pdir)
    for it in items:
        it["usada"] = it.get("archivo") == f.name
    _guardar_hist(pdir, items)


def _generar(project: str, pdir: Path, texto: str, voice_id: str, voz_nombre: str, speed: float,
             model_id: str, usar: bool, audio_ext: set) -> None:
    def prog(t):
        with JOB.lock:
            JOB.estado = "generando"
            try:
                JOB.progreso = int(float(t.get("progress") or 0))
            except Exception:
                pass
    try:
        tid = ai33.crear_tarea(texto, voice_id, speed=speed, model_id=model_id or None,
                               file_name=f"{project}_{datetime.now():%Y%m%d_%H%M%S}")
        with JOB.lock:
            JOB.estado = "generando"
        t = ai33.esperar(tid, al_progresar=prog, parar=lambda: JOB._parar)
        url = ((t.get("metadata") or {}).get("audio_url") or t.get("audio_url") or "")
        if not url:
            raise ai33.Ai33Error("ai33.pro terminó pero no dio el enlace del audio.")
        with JOB.lock:
            JOB.estado = "descargando"
        nombre = f"voz_{datetime.now():%Y%m%d_%H%M%S}{ai33.extension_de(url)}"
        f = ai33.descargar(url, _vdir(pdir) / nombre)
        items = _hist(pdir)
        items.append({"archivo": nombre, "voz": voz_nombre or voice_id, "voice_id": voice_id,
                      "fecha": datetime.now().strftime("%d/%m/%Y %H:%M"), "chars": len(texto),
                      "speed": speed, "creditos": t.get("credit_cost"), "texto": texto[:400], "usada": False})
        _guardar_hist(pdir, items)
        if usar:
            _usar_como_narracion(pdir, f, audio_ext)
        with JOB.lock:
            JOB.estado, JOB.progreso = "hecho", 100
    except Exception as e:
        with JOB.lock:
            JOB.estado, JOB.error = "error", str(e)
    finally:
        with JOB.lock:
            JOB.running = False


def manejar_get(path: str, q: dict) -> dict | None:
    if path == "/api/ai33/estado":
        cfg = ai33.load_config()
        out = {"clave": ai33.clave_oculta(), "ultima_voz": cfg.get("ultima_voz")}
        if out["clave"] and q.get("creditos", ["0"])[0] == "1":
            try:
                out["creditos"] = ai33.creditos()
            except Exception as e:
                out["error"] = str(e)
        return out
    if path == "/api/ai33/guardadas":
        return {"voces": ai33.load_config().get("guardadas") or []}
    if path == "/api/ai33/voces":
        g = lambda k, d="": q.get(k, [d])[0]
        return ai33.voces(provider=g("provider", "elevenlabs"), search=g("search"), language=g("language"),
                          gender=g("gender"), page=int(g("page", "1") or 1))
    return None


def manejar_post(path: str, data: dict, pdir_de, audio_ext: set, guardar_ajuste) -> dict | None:
    if path == "/api/ai33/clave":
        k = (data.get("api_key") or "").strip()
        if not k:
            raise ai33.Ai33Error("Pega la clave primero.")
        ai33.creditos(key=k)                       # comprobar que funciona
        ai33.save_config(api_key=k)
        return {"ok": True}
    if path == "/api/ai33/guardar_voz":
        vid = (data.get("voice_id") or "").strip()
        if not vid:
            raise ai33.Ai33Error("Elige primero una voz.")
        voz = {"voice_id": vid, "name": (data.get("name") or vid).strip(),
               "preview_url": data.get("preview_url") or "",
               "speed": float(data.get("speed") or 1), "model_id": data.get("model_id") or "",
               "nota": (data.get("nota") or "").strip()[:80]}
        lista = [v for v in (ai33.load_config().get("guardadas") or []) if v.get("voice_id") != vid]
        lista.insert(0, voz)
        ai33.save_config(guardadas=lista)
        return {"voces": lista}
    if path == "/api/ai33/quitar_voz":
        vid = data.get("voice_id") or ""
        lista = [v for v in (ai33.load_config().get("guardadas") or []) if v.get("voice_id") != vid]
        ai33.save_config(guardadas=lista)
        return {"voces": lista}
    name = data.get("name") or ""
    if not path.startswith("/api/ai33/"):
        return None
    pdir = pdir_de(name)
    if path == "/api/ai33/texto":
        _vdir(pdir).mkdir(parents=True, exist_ok=True)
        (_vdir(pdir) / "texto.txt").write_text(data.get("texto") or "", encoding="utf-8")
        return {"ok": True}
    if path == "/api/ai33/generar":
        with JOB.lock:
            if JOB.running:
                raise RuntimeError(f"Ya se está generando una voz en «{JOB.project}».")
            JOB.project, JOB.running, JOB.estado, JOB.progreso, JOB.error, JOB._parar = \
                name, True, "creando", 0, "", False
        texto = (data.get("texto") or "").strip()
        voz = {"voice_id": data.get("voice_id") or "", "name": data.get("voz_nombre") or "",
               "speed": float(data.get("speed") or 1), "model_id": data.get("model_id") or ""}
        _vdir(pdir).mkdir(parents=True, exist_ok=True)
        (_vdir(pdir) / "texto.txt").write_text(data.get("texto") or "", encoding="utf-8")
        ai33.save_config(ultima_voz=voz)
        try:
            guardar_ajuste(name, "voz_ai33", voz)
        except Exception:
            pass
        threading.Thread(target=_generar, daemon=True, args=(
            name, pdir, texto, voz["voice_id"], voz["name"], voz["speed"], voz["model_id"],
            bool(data.get("usar")), audio_ext)).start()
        return {"ok": True}
    if path == "/api/ai33/parar":
        with JOB.lock:
            if JOB.project == name:
                JOB._parar = True
        return {"ok": True}
    archivo = Path(data.get("archivo") or "").name
    f = _vdir(pdir) / archivo
    if path == "/api/ai33/usar":
        if not archivo or not f.exists():
            raise RuntimeError("No encuentro ese audio.")
        _usar_como_narracion(pdir, f, audio_ext)
        return {"ok": True}
    if path == "/api/ai33/borrar":
        if archivo and f.exists():
            f.unlink()
        _guardar_hist(pdir, [it for it in _hist(pdir) if it.get("archivo") != archivo])
        return {"ok": True}
    return None
