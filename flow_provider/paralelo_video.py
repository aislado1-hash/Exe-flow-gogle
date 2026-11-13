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


# Flow mete una tarjeta dentro de otra: contar solo las exteriores.
_JS_CARDS = f"""[...document.querySelectorAll('{SEL_CARD}')].filter(c => !(c.parentElement && c.parentElement.closest('{SEL_CARD}')))"""


async def _contar(page) -> int:
    return await page.evaluate(f"() => {_JS_CARDS}.length")


_ERR_RE = r"No se pudo generar|Se produjo un error|infringir|pol[ií]tica|Something went wrong|failed"


async def _esperar_listos(page, n: int, timeout_s: int, log) -> list[str]:
    """Espera a que las n primeras tarjetas (los vídeos de la tanda) terminen.
    Devuelve el estado de cada una: 'lista', 'error' o 'generando' (si se agotó el tiempo)."""
    js = f"""(n) => {{
        const cards = {_JS_CARDS}.slice(0, n);
        return cards.map(card => {{
            const txt = card.innerText || '';
            if (/{_ERR_RE}/i.test(txt)) return 'error';
            if (/\\b\\d+%/.test(txt)) return 'generando';
            for (const sp of card.querySelectorAll('mat-progress-spinner, mat-spinner, flow-loading-indicator, [role="progressbar"]')) {{
                const st = getComputedStyle(sp);
                if (st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0' && (sp.offsetWidth || sp.offsetHeight)) return 'generando';
            }}
            const v = card.querySelector('video');
            const src = v ? (v.getAttribute('src') || v.currentSrc || '') : '';
            if (v && src.length > 20) return 'lista';
            const img = card.querySelector('img');
            if (card.querySelector('flow-video-tile') && img && img.complete && img.naturalWidth > 0) return 'lista';
            return 'generando';
        }});
    }}"""
    t0, ultimo = time.monotonic(), -10
    estados = ["generando"] * n
    while time.monotonic() - t0 < timeout_s:
        estados = await page.evaluate(js, n)
        if len(estados) == n and all(e != "generando" for e in estados):
            break
        seg = int(time.monotonic() - t0)
        if seg - ultimo >= 10:
            ultimo = seg
            log(f"    [paralelo] {sum(e == 'lista' for e in estados)}/{n} vídeos listos ({seg}s)...")
        await asyncio.sleep(2)
    return estados


async def _card_en_pos(page, idx: int, name: str):
    ok = await page.evaluate(
        f"""([idx, name]) => {{ const cards = {_JS_CARDS};
            for (const c of document.querySelectorAll('[data-gfk-dl="' + name + '"]')) delete c.dataset.gfkDl;
            if (!cards[idx]) return false; cards[idx].dataset.gfkDl = name; return true; }}""", [idx, name])
    if not ok:
        raise RuntimeError(f"no encuentro la tarjeta nº {idx + 1} del lienzo")
    return page.locator(f'[data-gfk-dl="{name}"]').first


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
        estados = await _esperar_listos(page, n, 360, log) if n else []
        huellas: dict[str, str] = {}
        for j, it in enumerate(enviadas):
            k = items.index(it) + 1
            log(f"\n--- [{k}/{total}] video :: {it['name']} ---")
            try:
                est = estados[n - 1 - j] if len(estados) == n else "generando"
                if est == "error":
                    raise RuntimeError("Flow no pudo generar este vídeo")
                card = await _card_en_pos(page, n - 1 - j, it["name"])
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
