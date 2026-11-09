#!/usr/bin/env python
"""
montaje.py - Une los clips de un guion con tu narración, sincronizados.

  python montaje.py outputs\\mi_proyecto narracion.mp3
  python montaje.py outputs\\mi_proyecto narracion.mp3 --res 720 --modelo small

Qué hace:
  1. Transcribe la narración con Whisper (faster-whisper) y saca en qué
     segundo empieza cada palabra.
  2. Busca en esa transcripción el "texto" de cada plano del guion
     (outputs\\<proyecto>\\guion.json) y le asigna su tramo de audio.
     Los planos sin "texto" se reparten el tramo del anterior que sí lo tenga.
  3. Ajusta cada pieza a su tramo:
       - Foto + vídeo / Hablando: si el tramo es más largo, ralentiza el clip
         hasta 1,5x y, si aun así no llega, congela el último fotograma; si es
         más corto, lo recorta. Los clips hablados van SIN sonido.
       - Solo foto: zoom suave durante el tramo.
  4. Une todo con tu narración -> outputs\\<proyecto>\\montaje.mp4
     y deja montaje.srt (subtítulos) y montaje_planos.txt (tiempos).

Necesita los vídeos descargados (opción "Descargar también los vídeos" de la
web): busca <plano>_anim.mp4, habla01.mp4 y <plano>.png dentro de la carpeta.
"""
import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MAX_SLOW = 1.5   # ralentizar como mucho 1,5x
FPS = 30


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


def run(args: list[str]) -> None:
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
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
    """Agrupa los trabajos en piezas del montaje (una por plano)."""
    pieces, used = [], set()
    for i, j in enumerate(jobs):
        if i in used:
            continue
        t = j.get("type", "image")
        if t == "image":
            k = next((n for n in range(i + 1, len(jobs)) if n not in used
                      and jobs[n].get("type") == "video"
                      and Path(str(jobs[n].get("start", ""))).stem == j["name"]), None)
            if k is not None:
                used.add(k)
                pieces.append({"kind": "clip", "name": j["name"], "clip": jobs[k]["name"],
                               "image": j["name"], "texto": j.get("texto") or jobs[k].get("texto", "")})
            else:
                pieces.append({"kind": "foto", "name": j["name"], "image": j["name"],
                               "texto": j.get("texto", "")})
        elif j.get("start_ref"):
            pieces.append({"kind": "habla", "name": j["name"], "clip": j["name"],
                           "texto": j.get("texto", "")})
        else:
            pieces.append({"kind": "clip", "name": j["name"], "clip": j["name"],
                           "texto": j.get("texto", "")})
    return pieces


def find_media(folder: Path, name: str, exts: tuple[str, ...]) -> Path | None:
    for ext in exts:
        hits = sorted(folder.rglob(f"{name}{ext}"), key=lambda p: p.stat().st_mtime, reverse=True)
        hits = [h for h in hits if "_debug" not in h.parts and "_montaje_tmp" not in h.parts]
        if hits:
            return hits[0]
    return None


# ---------------------------------------------------------------------------
# 2. Transcripción y alineado
# ---------------------------------------------------------------------------
def transcribe(audio: Path, model_size: str) -> list[dict]:
    from faster_whisper import WhisperModel
    model = None
    for device, ctype in (("cuda", "float16"), ("cpu", "int8")):
        try:
            model = WhisperModel(model_size, device=device, compute_type=ctype)
            segments, _ = model.transcribe(str(audio), language="es", word_timestamps=True,
                                           vad_filter=True)
            words = [{"w": w.word.strip(), "start": w.start, "end": w.end}
                     for seg in segments for w in (seg.words or [])]
            print(f"  Transcripción con {device}: {len(words)} palabras.")
            return words
        except Exception as e:
            if device == "cpu":
                raise
            print(f"  (GPU no disponible para Whisper: {str(e)[:80]}… uso CPU)")
    return []


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
    starts[0] = 0.0
    last = 0.0
    for i in range(1, len(starts)):
        if starts[i] is not None and starts[i] <= last + 0.5:
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
# 3. Render de cada pieza
# ---------------------------------------------------------------------------
def render_piece(p: dict, out: Path, W: int, H: int) -> str:
    D = max(p["t1"] - p["t0"], 0.2)
    scale = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
    enc = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-r", str(FPS)]

    if p.get("clip_path"):
        C = duration(p["clip_path"])
        if D <= C:
            vf = f"{scale},trim=duration={D:.3f},setpts=PTS-STARTPTS"
            note = f"recortado {C:.1f}->{D:.1f}s"
        else:
            factor = min(D / C, MAX_SLOW)
            hold = D - C * factor
            vf = f"setpts={factor:.4f}*PTS,{scale}"
            if hold > 0.02:
                vf += f",tpad=stop_mode=clone:stop_duration={hold:.3f}"
            vf += f",trim=duration={D:.3f}"
            note = f"x{factor:.2f} lento" + (f" + {hold:.1f}s congelado" if hold > 0.02 else "")
        run(["-i", str(p["clip_path"]), "-vf", vf, *enc, "-t", f"{D:.3f}", str(out)])
        return note

    if p.get("image_path"):
        frames = max(int(round(D * FPS)), 1)
        # Zoom suave del 100% al 108% centrado (sobre una imagen ampliada para evitar temblor).
        vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase:flags=lanczos,"
              f"crop={W * 2}:{H * 2},"
              f"zoompan=z='1+0.08*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={W}x{H}:fps={FPS},setsar=1")
        run(["-loop", "1", "-i", str(p["image_path"]), "-vf", vf, *enc,
             "-frames:v", str(frames), str(out)])
        return "foto con zoom" if p["kind"] == "foto" else "FALTA el vídeo: uso la foto con zoom"

    run(["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}", *enc, "-t", f"{D:.3f}", str(out)])
    return "FALTA el archivo: pantalla negra"


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def main() -> int:
    global FFMPEG
    ap = argparse.ArgumentParser(description="Monta los clips de un guion sobre la narración.")
    ap.add_argument("carpeta", help="Carpeta del proyecto (outputs\\<proyecto>) con guion.json y los clips.")
    ap.add_argument("audio", help="Narración completa (mp3, wav, m4a…).")
    ap.add_argument("--guion", help="Ruta del guion JSON (por defecto <carpeta>\\guion.json).")
    ap.add_argument("--res", type=int, default=1080, choices=[720, 1080], help="Altura del vídeo final.")
    ap.add_argument("--modelo", default="small", help="Modelo de Whisper: base, small, medium…")
    ap.add_argument("--salida", help="Archivo de salida (por defecto <carpeta>\\montaje.mp4).")
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
    W, H = (1920, 1080) if args.res == 1080 else (1280, 720)
    out_file = Path(args.salida) if args.salida else folder / "montaje.mp4"

    guion = json.loads(guion_path.read_text(encoding="utf-8"))
    pieces = pieces_from_guion(guion.get("jobs", []))
    if not pieces:
        print("El guion no tiene planos.")
        return 1

    print(f"MONTAJE: {len(pieces)} planos · audio {audio.name}")
    for p in pieces:
        if p.get("clip"):
            p["clip_path"] = find_media(folder, p["clip"], (".mp4", ".mov", ".webm"))
        if p.get("image"):
            p["image_path"] = find_media(folder, p["image"], (".png", ".jpg", ".jpeg", ".webp"))
        p["natural"] = duration(p["clip_path"]) if p.get("clip_path") else 4.0
    missing = [p["name"] for p in pieces if not p.get("clip_path") and not p.get("image_path")]
    if missing:
        print(f"  AVISO: no encuentro archivos de: {', '.join(missing)}")

    total = duration(audio)
    con_texto = sum(1 for p in pieces if p.get("texto"))
    if con_texto:
        print("1/3 Transcribiendo la narración…")
        words = transcribe(audio, args.modelo)
        print("2/3 Colocando cada plano en su frase…")
        align(pieces, words, total)
    else:
        print("1/3 El guion no tiene «texto» en los planos: reparto el audio en proporción.")
        align_without_audio_text(pieces, total)

    print("3/3 Montando…")
    tmp = folder / "_montaje_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    lines = []
    try:
        parts = []
        for i, p in enumerate(pieces, 1):
            part = tmp / f"{i:03d}.mp4"
            note = render_piece(p, part, W, H)
            parts.append(part)
            tag = {"clip": "vídeo", "foto": "foto", "habla": "hablando"}[p["kind"]]
            line = f"{ts(p['t0'])} – {ts(p['t1'])}  {p['name']:<14} {tag:<9} {note}"
            print("   " + line)
            lines.append(line)
        lst = tmp / "lista.txt"
        lst.write_text("".join(f"file '{x.as_posix()}'\n" for x in parts), encoding="utf-8")
        video_only = tmp / "video.mp4"
        run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(video_only)])
        run(["-i", str(video_only), "-i", str(audio), "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
             "-movflags", "+faststart", str(out_file)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    (folder / "montaje_planos.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if con_texto and words:
        write_srt(words, folder / "montaje.srt")
    print(f"\nOK -> {out_file}")
    print(f"Tiempos de cada plano: {folder / 'montaje_planos.txt'}")
    if con_texto:
        print(f"Subtítulos: {folder / 'montaje.srt'}")
    return 0


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
