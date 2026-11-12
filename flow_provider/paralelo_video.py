"""
Vídeos EN PARALELO dentro de un mismo proyecto de Flow.

En vez de: animar → esperar → descargar → animar el siguiente…
hace:      subir todas las fotos → animar y enviar hasta 4 seguidos →
           descargar en orden (la descarga espera a que cada vídeo termine).

Qué tarjeta es de cada vídeo se sabe por la POSICIÓN: Flow pone arriba la
última enviada, así que tras enviar k vídeos las k primeras tarjetas son esos
k vídeos en orden inverso. Las fotos se suben todas ANTES de empezar, para que
ninguna tarjeta nueva se cuele entre ellos.
"""
import asyncio
import hashlib
import os
import time

from .browser import get_page

SEL_CARD = 'flow-grid-tile-container, flow-tile-container, [aria-roledescription="draggable"]'


def _huella(path) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None


async def _contar(page) -> int:
    return await page.locator(SEL_CARD).count()


async def generar_videos_en_paralelo(items: list[dict], upload_ref, animate, configure_video, submit,
                                     download, close_overlays, max_par: int = 4, log=print,
                                     reintentos: int = 1, _subidas: set | None = None) -> dict[str, str]:
    """items: [{name, prompt, start (ruta de la foto), duration, out}] en orden.
    Devuelve {name: ruta | 'ERROR: …'}."""
    page = await get_page()
    out: dict[str, str] = {}
    total = len(items)
    subidas = _subidas if _subidas is not None else set()

    # 1. Subir todas las fotos de golpe
    for it in items:
        if it["start"] not in subidas:
            try:
                await upload_ref(it["start"])
                subidas.add(it["start"])
            except Exception as e:
                out[it["name"]] = f"ERROR: no pude subir la foto: {e}"
                log(f"  ERROR en '{it['name']}': no pude subir la foto: {e}")
    pendientes = [it for it in items if it["name"] not in out]

    for c0 in range(0, len(pendientes), max_par):
        tanda = pendientes[c0:c0 + max_par]
        log(f"\n=== Tanda {c0 // max_par + 1}: animando {len(tanda)} vídeos seguidos ===")
        enviadas = []
        for it in tanda:
            k = items.index(it) + 1
            log(f"\n--- [{k}/{total}] video :: {it['name']} ---")
            try:
                await close_overlays()

                async def _conf(d=it.get("duration")):
                    await configure_video(d)

                await animate(it["start"], _conf)
                n0 = await _contar(page)
                await submit(it["prompt"])
                t0 = time.monotonic()
                while await _contar(page) <= n0:
                    if time.monotonic() - t0 > 40:
                        raise RuntimeError("Flow no creó la tarjeta del vídeo tras enviarlo.")
                    await asyncio.sleep(0.5)
                enviadas.append(it)
                log(f"    [paralelo] Enviado {it['name']} ({len(enviadas)}/{len(tanda)} en esta tanda)")
            except Exception as e:
                out[it["name"]] = f"ERROR: {e}"
                log(f"  ERROR en '{it['name']}': {e}")

        # 2. Descargar en orden (cada descarga espera a que su vídeo termine)
        if enviadas:
            log(f"    [paralelo] Esperando y descargando {len(enviadas)} vídeos en orden...")
        n = len(enviadas)
        huellas: dict[str, str] = {}
        for j, it in enumerate(enviadas):
            k = items.index(it) + 1
            log(f"\n--- [{k}/{total}] video :: {it['name']} ---")
            try:
                card = page.locator(SEL_CARD).nth(n - 1 - j)
                saved = await download(it["out"], card)
                h = _huella(saved)
                if h and h in huellas:
                    try:
                        os.remove(saved)
                    except Exception:
                        pass
                    raise RuntimeError(f"salió el mismo vídeo que {huellas[h]} (tarjeta confundida); se reintenta")
                if h:
                    huellas[h] = it["name"]
                out[it["name"]] = saved
                log(f"  OK -> {saved}")
            except Exception as e:
                out[it["name"]] = f"ERROR: {e}"
                log(f"  ERROR en '{it['name']}': {e}")

    fallidas = [it for it in items if str(out.get(it["name"], "ERROR")).startswith("ERROR")]
    if fallidas and reintentos > 0:
        log(f"\n=== Reintentando {len(fallidas)} vídeo(s) que fallaron ===")
        out.update(await generar_videos_en_paralelo(
            fallidas, upload_ref, animate, configure_video, submit, download, close_overlays,
            max_par, log, reintentos - 1, subidas))
    return out
