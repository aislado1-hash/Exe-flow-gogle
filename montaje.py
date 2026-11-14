#!/usr/bin/env python
"""
montaje.py - Render: tus imágenes como fondo de la voz en off.

  python montaje.py outputs\\mi_proyecto narracion.mp3
  python montaje.py outputs\\mi_proyecto narracion.mp3 --transicion corte --res 720

Qué hace:
  1. Transcribe la voz en off con Whisper y saca en qué segundo empieza cada palabra.
  2. Busca el "texto" de cada plano del guion (outputs\\<proyecto>\\guion.json)
     en esa transcripción: cada imagen dura lo que dura su frase. Los planos
     sin "texto" se reparten el tramo con el anterior que sí lo tenga.
  3. Cada imagen lleva movimiento suave, variado de forma automática
     (acercar, alejar, paneo a la derecha, paneo a la izquierda…) y entre
     imágenes hay un fundido corto y limpio (o corte seco con --transicion corte).
     Un plano puede forzar su efecto con "efecto": zoom_in, zoom_out,
     paneo_der, paneo_izq, subir, bajar.
  4. Une todo con la voz en off -> outputs\\<proyecto>\\montaje.mp4 (16:9)
     y/o montaje_vertical.mp4 (9:16, para Shorts/Reels/TikTok; --formato)
     y deja montaje.srt (subtítulos para CapCut) y montaje_planos.txt.

Busca cada imagen como <plano>.png (o .jpg/.webp) dentro de la carpeta.
"""
import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

FADE = 0.25     # fundido entre imágenes (s)
FPS = 30
MIN_PLANO = 0.3  # lo mínimo que dura un plano en pantalla (s)
INTERNAS = {"_debug", "_montaje_tmp", "_subidas", "_escenas", "_cuentas", "_rehacer", "_voz", "personajes"}
# Los tramos intermedios van casi sin pérdida (CRF 4, a simple vista idénticos);
# la compresión normal (CRF 18) se aplica una sola vez, al vídeo final. Antes
# cada tramo se comprimía a CRF 18 y otra vez al unirlos (y una 3ª con +25 planos).
# Sin pérdida total (qp 0) ocupaba ~1,4 GB temporales por cada 36 s de vídeo.
ENC_TMP = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "4", "-pix_fmt", "yuv420p", "-r", str(FPS)]
ENC_FINAL = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS)]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if not exe:
            sys.exit("No encuentro ffmpeg. Instala: pip install imageio-ffmpeg")
        return exe


FFMPEG = None


# Limitar el render al 75 % del procesador (tarda más, pero el PC sigue usable).
USO_CPU = 0.75
HILOS = max(1, int((os.cpu_count() or 4) * USO_CPU))
# En Windows, además, prioridad «por debajo de lo normal».
_PRIORIDAD = {"creationflags": 0x00004000} if os.name == "nt" else {}


def run(args: list[str]) -> None:
    # -threads va justo antes del archivo de salida (el último argumento)
    args = [*args[:-1], "-threads", str(HILOS), args[-1]]
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-filter_threads", str(HILOS), "-filter_complex_threads", str(HILOS), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", **_PRIORIDAD)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-800:])


def duration(path: Path) -> float:
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        raise RuntimeError(f"No puedo leer la duración de {path.name}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def norm(word: str) -> str:
    w = unicodedata.normalize("NFD", word.lower())
    w = "".join(c for c in w if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9ñ]", "", w)


def words_of(text: str) -> list[str]:
    return [w for w in (norm(x) for x in text.split()) if w]


def ts(t: float, srt: bool = False) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    if srt:
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)) % 1000:03d}"
    return f"{int(m)}:{s:05.2f}"


# ---------------------------------------------------------------------------
# 1. Guion -> lista de piezas
# ---------------------------------------------------------------------------
def pieces_from_guion(jobs: list[dict]) -> list[dict]:
    """Una pieza por imagen del guion (los vídeos, si los hay, se ignoran;
    su "texto" se suma al de su imagen)."""
    pieces, by_name = [], {}
    for j in jobs:
        if j.get("type", "image") == "image":
            piece = {"name": j["name"], "image": j["name"], "texto": j.get("texto", ""),
                     "efecto": j.get("efecto"), "usar": j.get("usar", "imagen")}
            pieces.append(piece)
            by_name[j["name"]] = piece
    for j in jobs:
        start = Path(str(j.get("start", ""))).stem
        if j.get("type") == "video" and start in by_name and j.get("texto") and not by_name[start]["texto"]:
            by_name[start]["texto"] = j["texto"]
    return pieces


def find_media(folder: Path, name: str, exts: tuple[str, ...]) -> Path | None:
    for ext in exts:
        hits = sorted(folder.rglob(f"{name}{ext}"), key=lambda p: p.stat().st_mtime, reverse=True)
        hits = [h for h in hits if not (set(h.parts) & INTERNAS)]
        if hits:
            return hits[0]
    return None


# ---------------------------------------------------------------------------
# 2. Transcripción y alineado
# ---------------------------------------------------------------------------
def load_audio_16k(audio: Path):
    """Lee el audio con ffmpeg (16 kHz, mono, float32). No depende de PyAV,
    que en algunas versiones es incompatible con faster-whisper."""
    import numpy as np
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(audio),
                        "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", "16000", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("No pude leer el audio: " + r.stderr.decode("utf-8", "replace")[-300:])
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def transcribe(audio: Path, model_size: str) -> list[dict]:
    from faster_whisper import WhisperModel
    samples = load_audio_16k(audio)
    last_error = None
    for device, ctype in (("cuda", "float16"), ("cpu", "int8")):
        try:
            model = WhisperModel(model_size, device=device, compute_type=ctype, cpu_threads=HILOS)
            segments, _ = model.transcribe(samples, language="es", word_timestamps=True,
                                           vad_filter=True)
            words = [{"w": w.word.strip(), "start": w.start, "end": w.end}
                     for seg in segments for w in (seg.words or [])]
            print(f"  Transcripción con {'GPU' if device == 'cuda' else 'CPU'}: {len(words)} palabras.")
            return words
        except Exception as e:
            last_error = e
            if device == "cuda":
                print(f"  (La GPU no funciona con Whisper en este PC: {str(e)[:100]}. Uso la CPU.)")
    raise RuntimeError(f"Whisper no pudo transcribir el audio: {last_error}")


def cached_transcription(folder: Path, audio: Path, model_size: str) -> list[dict]:
    """Transcribe una sola vez: si el audio no ha cambiado, reutiliza la anterior."""
    cache = folder / "montaje_palabras.json"
    st = audio.stat()
    key = {"audio": audio.name, "size": st.st_size, "mtime": int(st.st_mtime), "modelo": model_size}
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("key") == key and data.get("words"):
            print(f"  Uso la transcripción guardada ({len(data['words'])} palabras).")
            return data["words"]
    except Exception:
        pass
    words = transcribe(audio, model_size)
    cache.write_text(json.dumps({"key": key, "words": words}, ensure_ascii=False), encoding="utf-8")
    return words


def align(pieces: list[dict], words: list[dict], total: float) -> None:
    """Pone pieces[i]['t0'] y ['t1'] según dónde aparece su texto en el audio."""
    tw = [norm(w["w"]) for w in words]
    seq, owner = [], []
    for i, p in enumerate(pieces):
        for w in words_of(p.get("texto", "")):
            seq.append(w)
            owner.append(i)

    starts: list[float | None] = [None] * len(pieces)
    if seq and tw:
        sm = difflib.SequenceMatcher(None, seq, tw, autojunk=False)
        mapping = {}
        for a, b, n in sm.get_matching_blocks():
            for k in range(n):
                mapping[a + k] = b + k
        first_word = {}
        for idx, o in enumerate(owner):
            first_word.setdefault(o, idx)
        for i, p in enumerate(pieces):
            if i not in first_word:
                continue
            idx = first_word[i]
            # primera palabra de su texto que aparece en el audio
            for k in range(idx, len(owner)):
                if owner[k] != i:
                    break
                if k in mapping:
                    offset = k - idx   # palabras saltadas antes de la encontrada
                    t = words[mapping[k]]["start"]
                    if offset and mapping[k] - offset >= 0:
                        t = words[mapping[k] - offset]["start"]
                    starts[i] = t
                    break

    # El primer plano empieza en 0; los tiempos deben ir siempre hacia delante.
    # Una frase corta (desde MIN_PLANO) conserva su sitio exacto; solo se
    # descarta si sale fuera de orden o pegada a la anterior.
    starts[0] = 0.0
    last = 0.0
    for i in range(1, len(starts)):
        if starts[i] is not None and starts[i] < last + MIN_PLANO:
            starts[i] = None          # fuera de orden o pegado: se reparte
        if starts[i] is not None:
            last = starts[i]

    # Huecos (planos sin texto o no encontrados): repartir a partes iguales.
    anchors = [i for i, s in enumerate(starts) if s is not None] + [len(starts)]
    for a, b in zip(anchors, anchors[1:]):
        t_a = starts[a]
        t_b = starts[b] if b < len(starts) else total
        n = b - a
        for k in range(1, n):
            starts[a + k] = t_a + (t_b - t_a) * k / n

    for i, p in enumerate(pieces):
        p["t0"] = starts[i]
        p["t1"] = starts[i + 1] if i + 1 < len(pieces) else total


def align_without_audio_text(pieces: list[dict], total: float) -> None:
    """Sin textos: reparte la narración en proporción a la duración de cada pieza."""
    weights = [p.get("natural", 4.0) for p in pieces]
    scale = total / sum(weights)
    t = 0.0
    for p, w in zip(pieces, weights):
        p["t0"], p["t1"] = t, t + w * scale
        t += w * scale


# ---------------------------------------------------------------------------
# 3. Render de cada imagen con movimiento
# ---------------------------------------------------------------------------
EFECTOS = ["zoom_in", "paneo_der", "zoom_out", "paneo_izq", "zoom_in", "subir", "zoom_out", "bajar"]
ZOOM = 0.10   # cuánto se acerca (10 %)


def motion(efecto: str, frames: int) -> tuple[str, str, str]:
    """Expresiones z, x, y de zoompan para cada efecto (on = nº de fotograma)."""
    t = f"(on/{max(frames - 1, 1)})"
    ease = f"(0.5-0.5*cos(PI*{t}))"            # suave al empezar y al terminar
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    zmax = f"{1 + ZOOM}"
    if efecto == "zoom_in":
        return f"1+{ZOOM}*{ease}", cx, cy
    if efecto == "zoom_out":
        return f"{zmax}-{ZOOM}*{ease}", cx, cy
    if efecto == "paneo_der":
        return zmax, f"(iw-iw/zoom)*{ease}", cy
    if efecto == "paneo_izq":
        return zmax, f"(iw-iw/zoom)*(1-{ease})", cy
    if efecto == "subir":
        return zmax, cx, f"(ih-ih/zoom)*(1-{ease})"
    if efecto == "bajar":
        return zmax, cx, f"(ih-ih/zoom)*{ease}"
    return f"1+{ZOOM}*{ease}", cx, cy


def render_video_piece(p: dict, out: Path, W: int, H: int, seconds: float, enc: list[str]) -> str:
    """El clip de Flow ajustado a lo que dura su frase: si sobra se corta; si
    falta se ralentiza (hasta x2) y, si aún falta, se congela el último fotograma."""
    clip = duration(p["video_path"])
    frames = max(int(round(seconds * FPS)), 1)
    factor = 1.0
    if clip > 0 and clip < seconds:
        factor = min(seconds / clip, 2.0)
    resto = max(seconds - clip * factor, 0) + 0.5
    vf = (f"setpts={factor:.4f}*PTS,fps={FPS},"
          f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1,"
          f"tpad=stop_mode=clone:stop_duration={resto:.3f}")
    run(["-i", str(p["video_path"]), "-vf", vf, *enc, "-frames:v", str(frames), str(out)])
    if factor > 1.001:
        return f"vídeo {clip:.1f}s ralentizado x{factor:.2f}" + (" + congelado" if clip * factor < seconds - 0.05 else "")
    return f"vídeo {clip:.1f}s" + (" recortado" if clip > seconds + 0.05 else "")


def render_piece(p: dict, out: Path, W: int, H: int, seconds: float) -> str:
    frames = max(int(round(seconds * FPS)), 1)
    enc = ENC_TMP
    if p.get("usar") == "video" and p.get("video_path"):
        return render_video_piece(p, out, W, H, seconds, enc)
    if not p.get("image_path"):
        run(["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}", *enc,
             "-frames:v", str(frames), str(out)])
        return "FALTA la imagen: pantalla negra"
    z, x, y = motion(p["efecto"], frames)
    # Se amplía antes del zoompan (x3) para que el movimiento no tiemble.
    vf = (f"scale={W * 3}:{H * 3}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={W * 3}:{H * 3},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},setsar=1")
    run(["-loop", "1", "-i", str(p["image_path"]), "-vf", vf, *enc,
         "-frames:v", str(frames), str(out)])
    return p["efecto"]


def join_parts(parts: list[Path], durs: list[float], fade: float, out: Path) -> None:
    """Une los tramos. Con fundido, cada tramo (menos el último) dura `fade` de más
    y se mezcla con el siguiente: la duración total sigue siendo la suma de `durs`."""
    if fade <= 0 or len(parts) == 1:
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{x.as_posix()}'\n" for x in parts), encoding="utf-8")
        run(["-f", "concat", "-safe", "0", "-i", str(lst), *ENC_FINAL, str(out)])
        return
    # Por tandas para no abrir cientos de archivos a la vez.
    CHUNK = 25
    if len(parts) > CHUNK:
        groups, gdurs = [], []
        for k in range(0, len(parts), CHUNK):
            sub_p, sub_d = parts[k:k + CHUNK], durs[k:k + CHUNK]
            last_group = k + CHUNK >= len(parts)
            g = out.with_name(f"{out.stem}_g{k // CHUNK:03d}.mp4")
            _xfade_chain(sub_p, sub_d, fade, g, tail=not last_group, enc=ENC_TMP)
            groups.append(g)
            gdurs.append(sum(sub_d))
        _xfade_chain(groups, gdurs, fade, out, tail=False, enc=ENC_FINAL)
        return
    _xfade_chain(parts, durs, fade, out, tail=False, enc=ENC_FINAL)


def _xfade_chain(parts: list[Path], durs: list[float], fade: float, out: Path, tail: bool,
                 enc: list[str] = ENC_FINAL) -> None:
    if len(parts) == 1:
        if enc is ENC_TMP:
            shutil.copy(parts[0], out)
        else:
            run(["-i", str(parts[0]), *enc, str(out)])
        return
    inputs, chain, label, offset = [], [], "[0:v]", 0.0
    for p in parts:
        inputs += ["-i", str(p)]
    for k in range(1, len(parts)):
        offset += durs[k - 1]
        nxt = f"[v{k}]"
        chain.append(f"{label}[{k}:v]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}{nxt}")
        label = nxt
    run([*inputs, "-filter_complex", ";".join(chain), "-map", label, *[e for e in enc if e != "-an"], str(out)])


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def main() -> int:
    global FFMPEG
    ap = argparse.ArgumentParser(description="Monta las imágenes de un guion sobre la voz en off.")
    ap.add_argument("carpeta", help="Carpeta del proyecto (outputs\\<proyecto>) con guion.json y las imágenes.")
    ap.add_argument("audio", help="Narración completa (mp3, wav, m4a…).")
    ap.add_argument("--guion", help="Ruta del guion JSON (por defecto <carpeta>\\guion.json).")
    ap.add_argument("--res", type=int, default=1080, choices=[720, 1080], help="Altura del vídeo final.")
    ap.add_argument("--formato", choices=["horizontal", "vertical", "ambos"], default="horizontal",
                    help="horizontal 16:9 (por defecto), vertical 9:16 o ambos.")
    ap.add_argument("--modelo", default="small", help="Modelo de Whisper: base, small, medium…")
    ap.add_argument("--salida", help="Archivo de salida (por defecto <carpeta>\\montaje.mp4).")
    ap.add_argument("--transicion", choices=["fundido", "corte"], default="fundido",
                    help="fundido (0,25 s, por defecto) o corte seco.")
    ap.add_argument("--musica", help="Música de fondo (se repite si es corta y se funde al final).")
    ap.add_argument("--musica-db", type=float, default=-14.0,
                    help="Volumen de la música en dB (por defecto -14).")
    ap.add_argument("--ducking", choices=["no", "suave", "media", "fuerte"], default="media",
                    help="Cuánto baja la música cuando hablas (por defecto media).")
    ap.add_argument("--sin-srt", action="store_true", help="No crear el archivo de subtítulos .srt.")
    ap.add_argument("--solo-tiempos", action="store_true",
                    help="Solo calcula cuándo entra cada imagen (montaje_tiempos.json), sin renderizar.")
    args = ap.parse_args()

    FFMPEG = ffmpeg_exe()
    folder = Path(args.carpeta).resolve()
    audio = Path(args.audio).resolve()
    guion_path = Path(args.guion) if args.guion else folder / "guion.json"
    if not guion_path.exists():
        print(f"No encuentro el guion: {guion_path}")
        return 1
    if not audio.exists():
        print(f"No encuentro el audio: {audio}")
        return 1
    out_file = Path(args.salida) if args.salida else folder / "montaje.mp4"

    guion = json.loads(guion_path.read_text(encoding="utf-8"))
    pieces = pieces_from_guion(guion.get("jobs", []))
    if not pieces:
        print("El guion no tiene planos.")
        return 1

    print(f"MONTAJE: {len(pieces)} planos · audio {audio.name}")
    for k, p in enumerate(pieces):
        p["image_path"] = find_media(folder, p["image"], (".png", ".jpg", ".jpeg", ".webp"))
        p["video_path"] = find_media(folder, p["image"], (".mp4",)) if p["usar"] == "video" else None
        if p["usar"] == "video" and not p["video_path"]:
            print(f"  AVISO: {p['name']} está marcado como vídeo pero no hay vídeo; uso la imagen.")
        p["efecto"] = p.get("efecto") or EFECTOS[k % len(EFECTOS)]
        p["natural"] = 4.0
    missing = [p["name"] for p in pieces if not p.get("image_path")]
    if missing:
        print(f"  AVISO: no encuentro archivos de: {', '.join(missing)}")

    total = duration(audio)
    con_texto = sum(1 for p in pieces if p.get("texto"))
    words = []
    if con_texto:
        print("1/3 Transcribiendo la narración…")
        words = cached_transcription(folder, audio, args.modelo)
        print("2/3 Colocando cada plano en su frase…")
        align(pieces, words, total)
    else:
        print("1/3 El guion no tiene «texto» en los planos: reparto el audio en proporción.")
        align_without_audio_text(pieces, total)

    fade_s = 0.0 if args.transicion == "corte" else FADE
    tiempos = {"audio": audio.name, "duracion": total, "fundido": fade_s,
               "voz": [[round(a, 3), round(b, 3)] for a, b in speech_intervals(words)] if words else [],
               "planos": [
        {"name": p["name"], "t0": round(p["t0"], 3), "t1": round(p["t1"], 3), "efecto": p["efecto"],
         "texto": p.get("texto", ""),
         "video": p["video_path"].relative_to(folder).as_posix() if p.get("video_path") else None,
         "imagen": p["image_path"].relative_to(folder).as_posix() if p.get("image_path") else None}
        for p in pieces]}
    (folder / "montaje_tiempos.json").write_text(json.dumps(tiempos, ensure_ascii=False, indent=2), encoding="utf-8")
    if words and not args.sin_srt:
        write_srt(words, folder / "montaje.srt")
    elif args.sin_srt:
        (folder / "montaje.srt").unlink(missing_ok=True)
    if args.solo_tiempos:
        for p in pieces:
            print(f"   {ts(p['t0'])} – {ts(p['t1'])}  {p['name']:<14} {p['efecto']}")
        print(f"\nTIEMPOS OK -> {folder / 'montaje_tiempos.json'}")
        return 0

    print("3/3 Montando…")
    fade = 0.0 if args.transicion == "corte" else FADE
    formatos = ["horizontal", "vertical"] if args.formato == "ambos" else [args.formato]
    musica = Path(args.musica).resolve() if args.musica else None
    lines, salidas = [], []
    for fmt in formatos:
        vertical = fmt == "vertical"
        if vertical:
            W, H = (1080, 1920) if args.res == 1080 else (720, 1280)
            destino = out_file if (args.formato == "vertical" and args.salida) \
                else out_file.with_name(out_file.stem + "_vertical" + out_file.suffix)
        else:
            W, H = (1920, 1080) if args.res == 1080 else (1280, 720)
            destino = out_file
        print(f"— Formato {fmt} ({W}x{H}) -> {destino.name}")
        tmp = folder / "_montaje_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            parts, durs = [], []
            for i, p in enumerate(pieces, 1):
                D = max(p["t1"] - p["t0"], MIN_PLANO)
                extra = fade if (fade and i < len(pieces)) else 0.0
                part = tmp / f"{i:03d}.mp4"
                note = render_piece(p, part, W, H, D + extra)
                parts.append(part)
                durs.append(D)
                line = f"{ts(p['t0'])} – {ts(p['t1'])}  {p['name']:<14} {D:5.1f}s  {note}"
                if len(formatos) > 1 or vertical:
                    line = f"[{fmt}] " + line
                print("   " + line)
                lines.append(line)
            video_only = tmp / "video.mp4"
            join_parts(parts, durs, fade, video_only)
            if musica and musica.exists():
                print(f"   Música: {musica.name} · {args.musica_db:+.0f} dB · ducking {args.ducking}")
                mix_audio(video_only, audio, musica, total, args.musica_db, args.ducking, destino, words)
            else:
                run(["-i", str(video_only), "-i", str(audio), "-map", "0:v", "-map", "1:a",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                     "-movflags", "+faststart", str(destino)])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        salidas.append(destino)
        print(f"   OK -> {destino}")

    (folder / "montaje_planos.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nOK -> {', '.join(str(d) for d in salidas)}")
    print(f"Tiempos de cada plano: {folder / 'montaje_planos.txt'}")
    if con_texto and not args.sin_srt:
        print(f"Subtítulos: {folder / 'montaje.srt'}")
    return 0


DUCKING_DB = {"suave": -6.0, "media": -10.0, "fuerte": -16.0}   # cuánto baja la música al hablar
DUCK_ATTACK, DUCK_RELEASE = 0.25, 0.45                              # rampas (s)


def speech_intervals(words: list[dict], gap: float = 0.5) -> list[tuple[float, float]]:
    """Tramos en los que hablas (palabras seguidas, uniendo pausas cortas)."""
    out = []
    for w in words:
        a, b = float(w["start"]), float(w["end"])
        if out and a - out[-1][1] < gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def duck_expression(intervals: list[tuple[float, float]], duck_db: float) -> str:
    """Expresión de volumen para ffmpeg: 1 en las pausas y duck_db mientras hablas,
    con rampas suaves de entrada y salida."""
    if not intervals:
        return "1"
    g = 10 ** (duck_db / 20)
    terms = [f"clip(min((t-{a - DUCK_ATTACK:.3f})/{DUCK_ATTACK},({b + DUCK_RELEASE:.3f}-t)/{DUCK_RELEASE}),0,1)"
             for a, b in intervals]
    # max() de ffmpeg admite 2 argumentos: se anidan por parejas
    while len(terms) > 1:
        terms = [f"max({terms[k]},{terms[k + 1]})" if k + 1 < len(terms) else terms[k]
                 for k in range(0, len(terms), 2)]
    return f"1-{1 - g:.4f}*{terms[0]}"


def mix_audio(video: Path, voz: Path, musica: Path, total: float, musica_db: float,
              ducking: str, out: Path, words: list[dict] | None = None) -> None:
    """Voz en off + música de fondo. La música se repite hasta cubrir el vídeo,
    se funde al principio y al final y baja mientras hablas (ducking)."""
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    fade = min(2.5, total / 4)
    mus = (f"[2:a]{fmt},volume={musica_db}dB,atrim=duration={total:.3f},"
           f"afade=t=in:d=0.8,afade=t=out:st={max(total - fade, 0):.3f}:d={fade:.3f}")
    if ducking in DUCKING_DB and words:
        expr = duck_expression(speech_intervals(words), DUCKING_DB[ducking])
        graph = (f"[1:a]{fmt}[voz];{mus},volume='{expr}':eval=frame[musd];"
                 f"[voz][musd]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    elif ducking in DUCKING_DB:
        # Sin transcripción: compresor guiado por la voz
        graph = (f"[1:a]{fmt},asplit=2[voz][sc];{mus}[mus];"
                 f"[mus][sc]sidechaincompress=threshold=0.02:ratio=12:attack=30:release=600[musd];"
                 f"[voz][musd]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        graph = (f"[1:a]{fmt}[voz];{mus}[musd];"
                 f"[voz][musd]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    script = out.with_name("_mezcla_filtro.txt")      # en archivo: Windows limita la longitud del comando
    script.write_text(graph, encoding="utf-8")
    common = ["-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    inputs = ["-i", str(video), "-i", str(voz), "-stream_loop", "-1", "-i", str(musica)]
    try:
        try:
            run([*inputs, "-filter_complex_script", str(script), *common])
        except RuntimeError:
            run([*inputs, "-/filter_complex", str(script), *common])   # ffmpeg 7 o posterior
    finally:
        script.unlink(missing_ok=True)


def write_srt(words: list[dict], path: Path, max_words: int = 7) -> None:
    """Subtítulos cortos (hasta 7 palabras, cortando en pausas y puntos)."""
    subs, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = nxt is not None and nxt["start"] - w["end"] > 0.35
        if len(cur) >= max_words or pause or w["w"].endswith((".", "?", "!", "…")) or nxt is None:
            subs.append(cur)
            cur = []
    out = []
    for n, s in enumerate(subs, 1):
        text = " ".join(x["w"] for x in s).strip()
        out.append(f"{n}\n{ts(s[0]['start'], True)} --> {ts(s[-1]['end'], True)}\n{text}\n")
    path.write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
