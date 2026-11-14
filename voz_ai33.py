"""
voz_ai33.py — Narración con la API de ai33.pro (texto -> voz).

Lo usa la web (flow_web.py, paso 3 «Audio»): pegas el texto, eliges una voz
y el audio se guarda en el proyecto y puede usarse como narración.

API (https://api.ai33.pro, cabecera xi-api-key):
  GET  /v1/credits                     -> {"success": true, "credits": 123}
  GET  /v3/voices?provider=…&search=…&language=…&gender=…&page=…&page_size=…
                                       -> {"success", "data": [{voice_id, name, language,
                                           gender, tags, preview_url}], "pagination": {…}}
  POST /v3/text-to-speech  (multipart: text, voice_id, speed, with_transcript, file_name)
                                       -> {"success": true, "task_id": "…"}
  GET  /v1/task/{task_id}              -> {status: doing|done|error, progress,
                                           error_message, credit_cost, metadata: {audio_url}}

La clave se guarda en session/ai33.json (session/ nunca se sube a GitHub).
Solo librería estándar.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "session" / "ai33.json"
API = "https://api.ai33.pro"
UA = "GoogleFlowKillerVideos/1.0"
PROVIDERS = ("elevenlabs", "minimax", "fishaudio", "edge", "kokoro", "vbee", "clone")
SPEED_MIN, SPEED_MAX = 0.5, 1.5
MAX_CHARS = 1_000_000


class Ai33Error(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Configuración (clave y última voz usada)
# ---------------------------------------------------------------------------
def load_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(**cambios) -> dict:
    data = load_config()
    for k, v in cambios.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
    return data


def api_key() -> str:
    return (os.environ.get("AI33_API_KEY") or load_config().get("api_key") or "").strip()


def clave_oculta() -> str:
    k = api_key()
    return f"…{k[-4:]}" if len(k) >= 8 else ("guardada" if k else "")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _multipart(fields: dict) -> tuple[bytes, str]:
    boundary = f"----gfk{uuid.uuid4().hex}"
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode("utf-8"))
        parts.append(str(v).encode("utf-8"))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _request(method: str, path: str, params: dict | None = None, form: dict | None = None,
             key: str | None = None, timeout: float = 60) -> dict:
    key = (key or api_key()).strip()
    if not key:
        raise Ai33Error("Falta la clave de la API de ai33.pro (ponla en el paso Audio).")
    url = API + path
    if params:
        q = {k: v for k, v in params.items() if v not in (None, "")}
        if q:
            url += "?" + urllib.parse.urlencode(q)
    headers = {"xi-api-key": key, "Accept": "application/json", "User-Agent": UA}
    data = None
    if form is not None:
        data, ctype = _multipart(form)
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", errors="replace")[:400]
        if e.code in (401, 403):
            raise Ai33Error("ai33.pro rechaza la clave de la API (revisa que esté bien copiada).") from None
        if e.code == 429:
            raise Ai33Error("ai33.pro: demasiadas peticiones seguidas; espera unos segundos.") from None
        raise Ai33Error(f"ai33.pro respondió {e.code}: {_mensaje(cuerpo)}") from None
    except urllib.error.URLError as e:
        raise Ai33Error(f"No pude conectar con ai33.pro ({e.reason}).") from None
    try:
        out = json.loads(raw.decode("utf-8"))
    except Exception:
        raise Ai33Error(f"ai33.pro devolvió algo que no es JSON: {raw[:200]!r}") from None
    if isinstance(out, dict) and out.get("success") is False:
        raise Ai33Error("ai33.pro: " + _mensaje(json.dumps(out, ensure_ascii=False)))
    return out


def _mensaje(cuerpo: str) -> str:
    try:
        d = json.loads(cuerpo)
        for k in ("message", "error_message", "error", "detail"):
            if d.get(k):
                return str(d[k])[:300]
    except Exception:
        pass
    return cuerpo[:300] or "sin detalle"


# ---------------------------------------------------------------------------
# Funciones de la API
# ---------------------------------------------------------------------------
def creditos(key: str | None = None) -> float | None:
    out = _request("GET", "/v1/credits", key=key, timeout=20)
    c = out.get("credits")
    return c if isinstance(c, (int, float)) else None


def voces(provider: str = "elevenlabs", search: str = "", language: str = "", gender: str = "",
          page: int = 1, page_size: int = 30) -> dict:
    if provider not in PROVIDERS:
        raise Ai33Error(f"Proveedor desconocido: {provider}")
    out = _request("GET", "/v3/voices", params={
        "provider": provider, "search": search.strip(), "language": language, "gender": gender,
        "page": max(1, int(page)), "page_size": max(1, min(100, int(page_size)))}, timeout=30)
    lista = []
    for v in out.get("data") or []:
        if not isinstance(v, dict) or not v.get("voice_id"):
            continue
        tags = v.get("tags")
        if isinstance(tags, list):
            tags = ", ".join(str(t) for t in tags[:6])
        elif isinstance(tags, dict):
            tags = ", ".join(f"{a}: {b}" for a, b in list(tags.items())[:6])
        lista.append({"voice_id": str(v["voice_id"]), "name": str(v.get("name") or v["voice_id"]),
                      "language": v.get("language") or "", "gender": v.get("gender") or "",
                      "tags": tags or "", "preview_url": v.get("preview_url") or ""})
    pag = out.get("pagination") or {}
    return {"voces": lista, "page": pag.get("page", page),
            "has_more": bool(pag.get("has_more")), "total": pag.get("total")}


def crear_tarea(texto: str, voice_id: str, speed: float = 1.0, file_name: str | None = None,
                model_id: str | None = None) -> str:
    texto = (texto or "").strip()
    if not texto:
        raise Ai33Error("Escribe o pega el texto que quieres convertir en voz.")
    if len(texto) > MAX_CHARS:
        raise Ai33Error(f"El texto es demasiado largo ({len(texto)} caracteres; máximo {MAX_CHARS}).")
    voice_id = (voice_id or "").strip()
    if not voice_id:
        raise Ai33Error("Elige una voz.")
    if not any(voice_id.startswith(p + "_") for p in PROVIDERS):
        voice_id = "elevenlabs_" + voice_id          # id de ElevenLabs pegado a mano
    speed = round(max(SPEED_MIN, min(SPEED_MAX, float(speed or 1))), 2)
    form = {"text": texto, "voice_id": voice_id, "speed": speed, "with_transcript": "false",
            "file_name": file_name or None, "model_id": model_id or None}
    out = _request("POST", "/v3/text-to-speech", form=form, timeout=90)
    tid = out.get("task_id")
    if not tid:
        raise Ai33Error(f"ai33.pro no devolvió el número de tarea: {json.dumps(out)[:200]}")
    return str(tid)


def tarea(task_id: str) -> dict:
    out = _request("GET", f"/v1/task/{urllib.parse.quote(task_id)}", timeout=30)
    if isinstance(out.get("data"), dict) and "status" not in out:
        out = out["data"]
    return out


def esperar(task_id: str, al_progresar=None, cada: float = 4.0, max_seg: float = 1800,
            parar=lambda: False) -> dict:
    """Consulta la tarea hasta que termina. Devuelve la tarea terminada."""
    t0 = time.monotonic()
    fallos = 0
    while True:
        if parar():
            raise Ai33Error("Parado por ti (la tarea puede seguir en ai33.pro).")
        try:
            t = tarea(task_id)
            fallos = 0
        except Ai33Error:
            fallos += 1
            if fallos >= 5:
                raise
            time.sleep(cada)
            continue
        st = str(t.get("status") or "").lower()
        if al_progresar:
            al_progresar(t)
        if st == "done":
            return t
        if st in ("error", "failed", "fail"):
            raise Ai33Error("ai33.pro no pudo generar el audio: " +
                            str(t.get("error_message") or "sin detalle")[:300])
        if time.monotonic() - t0 > max_seg:
            raise Ai33Error("ai33.pro tarda demasiado (más de 30 min). Mira tu historial en ai33.pro.")
        time.sleep(cada)


def descargar(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise Ai33Error(f"No pude descargar el audio de ai33.pro ({e}).") from None
    if tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        raise Ai33Error("El audio descargado de ai33.pro está vacío.")
    os.replace(tmp, dest)
    return dest


def extension_de(url: str) -> str:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return ext if ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac") else ".mp3"
