"""
Generación de imágenes EN PARALELO dentro de un mismo proyecto de Flow.

En vez de: enviar → esperar → descargar → enviar la siguiente…
hace:      enviar hasta 8 seguidas → esperar a que terminen → descargar en orden.

Cada envío crea una tarjeta nueva en el lienzo. Se marca con el nombre del plano
(atributo data-gfk) y se guarda su data-tile-id, así luego se sabe qué tarjeta
es de cada plano aunque terminen en otro orden. Último recurso: la posición
(Flow pone las tarjetas nuevas arriba, en orden inverso de envío).
"""
import asyncio
import re
import time

from .browser import get_page

SEL_CARD = 'flow-grid-tile-container, flow-tile-container, [aria-roledescription="draggable"]'
_ERR_RE = r"No se pudo generar|Se produjo un error|infringir|Something went wrong|could not generate"

_JS_IDOF = "const idOf = (c) => c.getAttribute('data-tile-id') || (c.querySelector('[data-tile-id]') ? c.querySelector('[data-tile-id]').getAttribute('data-tile-id') : '');"

_JS_IDS = f"""() => {{ {_JS_IDOF} return [...document.querySelectorAll('{SEL_CARD}')].map(idOf).filter(Boolean); }}"""

_JS_MARK_NEW = f"""([before, name]) => {{
    const cards = [...document.querySelectorAll('{SEL_CARD}')];
    const idOf = (c) => c.getAttribute('data-tile-id') || (c.querySelector('[data-tile-id]') ? c.querySelector('[data-tile-id]').getAttribute('data-tile-id') : '');
    for (const c of cards) {{
        if (c.dataset.gfk) continue;
        const id = idOf(c);
        if (id && before.includes(id)) continue;
        if (!id && c.dataset.gfkVisto) continue;
        c.dataset.gfk = name;
        return {{ ok: true, id: id || null, total: cards.length }};
    }}
    return {{ ok: false, total: cards.length }};
}}"""

_JS_STATE = rf"""([name, tileId]) => {{
    const cards = [...document.querySelectorAll('{SEL_CARD}')];
    const idOf = (c) => c.getAttribute('data-tile-id') || (c.querySelector('[data-tile-id]') ? c.querySelector('[data-tile-id]').getAttribute('data-tile-id') : '');
    let card = cards.find(c => c.dataset.gfk === name) || (tileId ? cards.find(c => idOf(c) === tileId) : null);
    if (!card) return {{ estado: 'perdida' }};
    if (!card.dataset.gfk) card.dataset.gfk = name;      // re-marcar si Flow re-creó el nodo
    const txt = card.innerText || '';
    if (/{_ERR_RE}/i.test(txt)) return {{ estado: 'error', detalle: txt.slice(0, 160) }};
    const pct = txt.match(/\b\d+%/);
    if (pct) return {{ estado: 'generando', pct: pct[0] }};
    for (const sp of card.querySelectorAll('mat-progress-spinner, mat-spinner, flow-loading-indicator, [role="progressbar"]')) {{
        const st = getComputedStyle(sp);
        if (st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0' && (sp.offsetWidth || sp.offsetHeight)) return {{ estado: 'generando' }};
    }}
    const img = card.querySelector('flow-image-tile img, img.image, img');
    if (img && img.complete && img.naturalWidth > 0) return {{ estado: 'lista' }};
    return {{ estado: 'generando' }};
}}"""

_JS_MARCAR_VISTAS = f"""() => {{ for (const c of document.querySelectorAll('{SEL_CARD}')) c.dataset.gfkVisto = '1'; }}"""


async def _limpiar_refs_del_prompt(page) -> None:
    """Quita del cuadro del prompt las referencias que hubieran quedado del envío anterior."""
    try:
        await page.evaluate("""() => {
            const ed = document.querySelector('flow-rich-text-editor div[contenteditable="true"], div[contenteditable="true"]');
            if (!ed) return 0;
            let box = ed;
            for (let i = 0; i < 6 && box.parentElement; i++) { box = box.parentElement; if (box.querySelector('img')) break; }
            const btns = [...box.querySelectorAll('button')].filter(b => {
                const a = (b.getAttribute('aria-label') || '').toLowerCase();
                const icon = (b.querySelector('i, mat-icon') || {}).textContent || '';
                return /quitar|eliminar|remove|borrar|delete/.test(a) || icon.trim() === 'close' || icon.trim() === 'cancel';
            }).filter(b => b.closest('img, [class*="chip"], [class*="ingredient"], [class*="thumb"]') || b.parentElement.querySelector('img'));
            btns.forEach(b => b.click());
            return btns.length;
        }""")
        await asyncio.sleep(0.3)
    except Exception:
        pass


async def _esperar_tarjeta_nueva(page, before: list[str], name: str, timeout: float = 30.0) -> str | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        res = await page.evaluate(_JS_MARK_NEW, [before, name])
        if res.get("ok"):
            return res.get("id") or ""
        await asyncio.sleep(0.5)
    return None


async def generar_en_paralelo(items: list[dict], select_mode, add_ref, upload_ref, submit, download,
                              max_par: int = 8, timeout_s: int = 240, log=print,
                              reintentos: int = 1, _subidas: bool = False) -> dict[str, str]:
    """items: [{name, prompt, refs:[ruta], out}] en orden. Devuelve {name: ruta | 'ERROR: …'}.

    select_mode / add_ref / upload_ref / submit / download: funciones del motor de Flow.
    """
    page = await get_page()
    out: dict[str, str] = {}
    total = len(items)
    # Subir todas las referencias una vez (con el modo imagen ya configurado)
    await select_mode()
    if not _subidas:
        for ref in dict.fromkeys(r for it in items for r in it["refs"]):
            await upload_ref(ref)

    for c0 in range(0, total, max_par):
        tanda = items[c0:c0 + max_par]
        log(f"\n=== Tanda {c0 // max_par + 1}: enviando {len(tanda)} imágenes seguidas ===")
        await page.evaluate(_JS_MARCAR_VISTAS)
        enviadas = []
        for k, it in enumerate(tanda, c0 + 1):
            log(f"\n--- [{k}/{total}] image :: {it['name']} ---")
            try:
                await _limpiar_refs_del_prompt(page)
                for ref in it["refs"]:
                    await add_ref(ref)
                before = await page.evaluate(_JS_IDS)
                await submit(it["prompt"])
                tile = await _esperar_tarjeta_nueva(page, before, it["name"])
                if tile is None:
                    raise RuntimeError("Flow no creó la tarjeta tras enviar el prompt.")
                it["tile"] = tile
                enviadas.append(it)
                log(f"    [paralelo] Enviada {it['name']} ({len(enviadas)}/{len(tanda)} en esta tanda)")
            except Exception as e:
                out[it["name"]] = f"ERROR: {e}"
                log(f"  ERROR en '{it['name']}': {e}")

        # Esperar a que terminen todas las de la tanda
        log(f"    [paralelo] Esperando a que Flow termine {len(enviadas)} imágenes...")
        estado = {it["name"]: "generando" for it in enviadas}
        t0, ultimo = time.monotonic(), 0
        while any(v == "generando" for v in estado.values()) and time.monotonic() - t0 < timeout_s:
            for it in enviadas:
                if estado[it["name"]] != "generando":
                    continue
                r = await page.evaluate(_JS_STATE, [it["name"], it.get("tile")])
                if r["estado"] in ("lista", "error"):
                    estado[it["name"]] = r["estado"]
                    it["detalle"] = r.get("detalle", "")
            seg = int(time.monotonic() - t0)
            if seg - ultimo >= 5:
                ultimo = seg
                listas = sum(1 for v in estado.values() if v == "lista")
                log(f"    [paralelo] {listas}/{len(enviadas)} listas ({seg}s)...")
            await asyncio.sleep(1.5)
        await asyncio.sleep(2.5)   # dejar que terminen las animaciones de revelado

        # Descargar en orden
        for idx, it in enumerate(enviadas):
            k = c0 + tanda.index(it) + 1
            log(f"\n--- [{k}/{total}] image :: {it['name']} ---")
            st = estado[it["name"]]
            if st != "lista":
                motivo = "Flow rechazó o no pudo generar la imagen" if st == "error" else "tiempo de espera agotado"
                out[it["name"]] = f"ERROR: {motivo}. {it.get('detalle', '')}".strip()
                log(f"  ERROR en '{it['name']}': {motivo}")
                continue
            try:
                card = page.locator(f'[data-gfk="{it["name"]}"]').first
                if await card.count() == 0 and it.get("tile"):
                    card = page.locator(f'{SEL_CARD.split(",")[0]}:has([data-tile-id="{it["tile"]}"]), [data-tile-id="{it["tile"]}"]').first
                if await card.count() == 0:
                    # Último recurso: posición (las nuevas arriba, orden inverso de envío)
                    pos = len(enviadas) - 1 - idx
                    card = page.locator(SEL_CARD).nth(pos)
                    log(f"    [paralelo] Aviso: localizo {it['name']} por posición ({pos}).")
                saved = await download(it["out"], card)
                out[it["name"]] = saved
                log(f"  OK -> {saved}")
            except Exception as e:
                out[it["name"]] = f"ERROR: {e}"
                log(f"  ERROR en '{it['name']}': {e}")

    # Reintento: las que fallaron se vuelven a enviar juntas una vez más
    fallidas = [it for it in items if str(out.get(it["name"], "ERROR")).startswith("ERROR")]
    if fallidas and reintentos > 0:
        log(f"\n=== Reintentando {len(fallidas)} imagen(es) que fallaron ===")
        for it in fallidas:
            it.pop("tile", None)
        out.update(await generar_en_paralelo(fallidas, select_mode, add_ref, upload_ref, submit, download,
                                             max_par, timeout_s, log, reintentos - 1, _subidas=True))
    return out
