"""
revisar_imagenes.py — Detecta imágenes dañadas (descargas cortadas a medias).

Dos señales:
  1. Archivo incompleto: le falta la marca de cierre (PNG: IEND, JPG: FFD9)
     o Pillow no puede leerlo entero.
  2. Parte de abajo de un solo color: varias filas seguidas al final de la
     imagen sin ninguna variación (Flow aún estaba pintándola).

revisar(path) -> None si está bien, o un texto con el motivo.
Los resultados se guardan en memoria por (ruta, fecha, tamaño); revisar_en_segundo_plano()
revisa una lista de imágenes en un hilo para no frenar la web.
"""
from __future__ import annotations

import threading
from pathlib import Path

_cache: dict[str, tuple[float, int, str | None]] = {}
_lock = threading.Lock()
_pendientes: set[str] = set()
_hilo: threading.Thread | None = None

FILAS_MUESTRA = 96          # la imagen se reduce a 96 filas para mirarla
FILAS_LISAS_MIN = 12        # >= 12 filas finales idénticas (1/8 de la imagen) = dañada


def _final_ok(path: Path) -> bool:
    ext = path.suffix.lower()
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - 64))
            cola = f.read()
    except OSError:
        return False
    if ext == ".png":
        return b"IEND" in cola[-16:]
    if ext in (".jpg", ".jpeg"):
        return b"\xff\xd9" in cola.rstrip(b"\x00")[-4:]
    return True


def revisar(path: Path) -> str | None:
    path = Path(path)
    try:
        if path.stat().st_size < 2000:
            return "archivo casi vacío"
    except OSError:
        return None
    if not _final_ok(path):
        return "descarga incompleta"
    try:
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        with Image.open(path) as im:
            im.load()
            w, h = im.size
            if w < 16 or h < 16:
                return "imagen demasiado pequeña"
            g = im.convert("L").resize((64, FILAS_MUESTRA))
    except ImportError:
        return None                       # sin Pillow solo vale la señal 1
    except Exception:
        return "descarga incompleta"
    px = g.load()
    lisas = 0
    for y in range(FILAS_MUESTRA - 1, -1, -1):
        fila = [px[x, y] for x in range(64)]
        if max(fila) - min(fila) <= 1:
            lisas += 1
        else:
            break
    if lisas >= FILAS_LISAS_MIN:
        return f"la parte de abajo ({round(lisas * 100 / FILAS_MUESTRA)} %) es de un solo color"
    return None


def resultado(path: Path) -> tuple[bool, str | None]:
    """(ya revisada, motivo o None). No revisa: solo mira la caché."""
    try:
        st = path.stat()
    except OSError:
        return True, None
    with _lock:
        c = _cache.get(str(path))
    if c and c[0] == st.st_mtime and c[1] == st.st_size:
        return True, c[2]
    return False, None


def _trabajar() -> None:
    global _hilo
    while True:
        with _lock:
            if not _pendientes:
                _hilo = None
                return
            p = _pendientes.pop()
        path = Path(p)
        try:
            st = path.stat()
            r = revisar(path)
            with _lock:
                _cache[p] = (st.st_mtime, st.st_size, r)
        except OSError:
            pass


def revisar_en_segundo_plano(paths) -> int:
    """Pone en cola las imágenes aún no revisadas. Devuelve cuántas faltan."""
    global _hilo
    nuevas = [str(p) for p in paths if not resultado(Path(p))[0]]
    with _lock:
        _pendientes.update(nuevas)
        faltan = len(_pendientes)
        if faltan and _hilo is None:
            _hilo = threading.Thread(target=_trabajar, daemon=True)
            _hilo.start()
    return faltan
