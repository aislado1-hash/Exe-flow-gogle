#!/usr/bin/env python
"""
montaje_shorts.py - Render de la pestaña «Shorts Muñecas» (vertical 9:16, sin voz).

  python montaje_shorts.py outputs\\mi_short
  python montaje_shorts.py outputs\\mi_short --musica musica.mp3 --musica-db -12

Qué hace:
  1. Lee guion.json: cada plano dura su "dur" (por defecto 4 s).
  2. Usa el vídeo del plano si está marcado "usar": "video" y existe; si no, la
     imagen con movimiento suave (los mismos efectos que montaje.py).
  3. Pone el "texto" de cada plano en grande (gancho), arriba, con borde negro.
  4. Une todo con corte seco o fundido corto, añade la música (opcional)
     -> outputs\\<proyecto>\\montaje.mp4 (1080x1920)
  5. Deja shorts_titulos.txt con títulos, descripción y hashtags del guion.

Con --asmr (o "shorts": {"asmr": true} en el guion): sin texto ni música; cada
clip conserva su sonido y el conjunto se normaliza a -16 LUFS.
"""
import argparse
import json
import re
import shutil
import sys
import textwrap
from pathlib import Path

import montaje as M

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

W, H = 1080, 1920
DUR_DEF = 4.0
FUENTES = ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/impact.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
           "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]


def fuente() -> str | None:
    return next((f for f in FUENTES if Path(f).exists()), None)


def _ff_path(p: Path | str) -> str:
    """Ruta para dentro de un filtro de ffmpeg (C\\:/... en Windows)."""
    return Path(p).as_posix().replace(":", "\\:").replace("'", "\\'")


def poner_texto(src: Path, out: Path, texto: str, tmp: Path, idx: int) -> None:
    """Texto grande arriba (en el tercio superior, fuera de la zona de botones)."""
    # Los emojis no existen en la fuente del render (saldrían como cuadros): fuera.
    limpio = "".join(c for c in texto if ord(c) < 0x2000 or c in "¿¡€")
    limpio = re.sub(r"\s+", " ", limpio).strip()
    lineas = textwrap.wrap(limpio, width=18)[:4]
    if not lineas:
        shutil.copy(src, out)
        return
    f = fuente()
    font = f"fontfile='{_ff_path(f)}':" if f else ""
    size, paso = 86, 106
    filtros = []
    for k, linea in enumerate(lineas):              # una por línea: cada una centrada
        txt = tmp / f"txt_{idx:03d}_{k}.txt"
        txt.write_text(linea, encoding="utf-8")
        filtros.append(f"drawtext={font}textfile='{_ff_path(txt)}':fontsize={size}:fontcolor=white:"
                       f"borderw=7:bordercolor=black:x=(w-text_w)/2:y=h*0.16+{k * paso}")
    vf = ",".join(filtros)
    M.run(["-i", str(src), "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(M.FPS), str(out)])


def escribir_titulos(folder: Path, meta: dict) -> None:
    if not meta:
        return
    partes = []
    for k, t in [("titulo_es", "Título (ES)"), ("titulo_en", "Título (EN)"),
                 ("descripcion", "Descripción"), ("hashtags", "Hashtags")]:
        if meta.get(k):
            v = meta[k]
            partes.append(f"{t}:\n{' '.join(v) if isinstance(v, list) else v}\n")
    (folder / "shorts_titulos.txt").write_text("\n".join(partes), encoding="utf-8")


def parte_asmr(p: dict, out: Path, D: float) -> str:
    """Clip con su propio sonido (ASMR): recorta a D, 9:16 y fundidos de audio cortos
    para que no haya chasquidos entre cortes. Si no hay vídeo, imagen con movimiento
    y silencio."""
    vf = (f"fps={M.FPS},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={W}:{H},setsar=1,tpad=stop_mode=clone:stop_duration={D:.3f}")
    af = (f"aformat=sample_rates=48000:channel_layouts=stereo,apad,"
          f"afade=t=in:d=0.12,afade=t=out:st={max(D - 0.15, 0):.3f}:d=0.15")
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-t", f"{D:.3f}"]
    if p.get("video_path"):
        clip = p["video_path"]
        tiene_audio = "Audio:" in M.subprocess.run([M.FFMPEG, "-hide_banner", "-i", str(clip)],
                                                   capture_output=True, text=True, errors="replace").stderr
        largo = M.duration(clip)
        desde = p.get("desde")
        if desde is None:                     # tramo central: el arranque suele estar quieto
            desde = max(min((largo - D) / 2, 1.5), 0) if largo > D + 0.5 else 0
        ss = ["-ss", f"{float(desde):.3f}"]
        if tiene_audio:
            M.run([*ss, "-i", str(clip), "-vf", vf, "-af", af, *enc, str(out)])
            return f"vídeo con sonido (desde {float(desde):.1f}s de {largo:.1f}s)"
        M.run([*ss, "-i", str(clip), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-map", "0:v", "-map", "1:a", "-vf", vf, *enc, str(out)])
        return "vídeo sin sonido"
    silent = out.with_name(out.stem + "_v.mp4")
    nota = M.render_piece(p, silent, W, H, D)
    M.run(["-i", str(silent), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
           "-map", "0:v", "-map", "1:a", *enc, str(out)])
    return nota + " (imagen, sin sonido)"


def main_asmr(folder: Path, jobs: list[dict], out_file: Path, tmp: Path) -> float:
    parts, t = [], 0.0
    for i, j in enumerate(jobs, 1):
        try:
            D = max(float(j.get("dur") or DUR_DEF), 1.0)
        except (TypeError, ValueError):
            D = DUR_DEF
        p = {"name": j["name"], "desde": j.get("desde"), "efecto": j.get("efecto") or M.EFECTOS[(i - 1) % len(M.EFECTOS)],
             "image_path": M.find_media(folder, j["name"], (".png", ".jpg", ".jpeg", ".webp")),
             "video_path": M.find_media(folder, j["name"], (".mp4",)) if j.get("usar", "video") == "video" else None}
        part = tmp / f"{i:03d}.mp4"
        nota = parte_asmr(p, part, D)
        parts.append(part)
        print(f"   {M.ts(t)} – {M.ts(t + D)}  {j['name']:<10} {D:4.1f}s  {nota}")
        t += D
    ins = sum((["-i", str(x)] for x in parts), [])
    graph = "".join(f"[{k}:v][{k}:a]" for k in range(len(parts))) + \
        f"concat=n={len(parts)}:v=1:a=1[v][a0];[a0]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
    M.run([*ins, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(out_file)])
    return t


def main() -> int:
    ap = argparse.ArgumentParser(description="Monta un Short vertical con los planos del guion.")
    ap.add_argument("carpeta")
    ap.add_argument("--transicion", choices=["fundido", "corte"], default="corte")
    ap.add_argument("--musica")
    ap.add_argument("--musica-db", type=float, default=-12.0)
    ap.add_argument("--salida")
    ap.add_argument("--asmr", action="store_true",
                    help="Estilo ASMR: sin texto ni música, con el sonido de cada clip (normalizado).")
    args = ap.parse_args()

    M.FFMPEG = M.ffmpeg_exe()
    folder = Path(args.carpeta).resolve()
    guion = json.loads((folder / "guion.json").read_text(encoding="utf-8"))
    jobs = [j for j in guion.get("jobs", []) if j.get("type", "image") == "image"]
    if not jobs:
        print("El guion no tiene planos.")
        return 1
    out_file = Path(args.salida) if args.salida else folder / "montaje.mp4"
    fade = 0.0 if args.transicion == "corte" else M.FADE

    print(f"SHORT: {len(jobs)} planos · 9:16 · {W}x{H}")
    tmp = folder / "_montaje_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    if args.asmr or (guion.get("shorts") or {}).get("asmr"):
        try:
            total = main_asmr(folder, jobs, out_file, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        escribir_titulos(folder, guion.get("shorts") or {})
        print(f"\nOK (ASMR) -> {out_file}  ({total:.1f} s)")
        return 0
    try:
        parts, durs, t = [], [], 0.0
        for i, j in enumerate(jobs, 1):
            try:
                D = max(float(j.get("dur") or DUR_DEF), 1.0)
            except (TypeError, ValueError):
                D = DUR_DEF
            usar = j.get("usar", "imagen")
            p = {"name": j["name"], "usar": usar,
                 "efecto": j.get("efecto") or M.EFECTOS[(i - 1) % len(M.EFECTOS)],
                 "image_path": M.find_media(folder, j["name"], (".png", ".jpg", ".jpeg", ".webp")),
                 "video_path": M.find_media(folder, j["name"], (".mp4",)) if usar == "video" else None}
            if usar == "video" and not p["video_path"]:
                print(f"  AVISO: {j['name']} marcado como vídeo pero no hay vídeo; uso la imagen.")
            extra = fade if (fade and i < len(jobs)) else 0.0
            base = tmp / f"{i:03d}_base.mp4"
            nota = M.render_piece(p, base, W, H, D + extra)
            part = tmp / f"{i:03d}.mp4"
            if (j.get("texto") or "").strip():
                poner_texto(base, part, j["texto"], tmp, i)
                nota += " + texto"
            else:
                part = base
            parts.append(part)
            durs.append(D)
            print(f"   {M.ts(t)} – {M.ts(t + D)}  {j['name']:<10} {D:4.1f}s  {nota}")
            t += D
        video_only = tmp / "video.mp4"
        M.join_parts(parts, durs, fade, video_only)
        total = sum(durs)
        musica = Path(args.musica).resolve() if args.musica else None
        if musica and musica.exists():
            fo = min(1.5, total / 4)
            af = (f"volume={args.musica_db}dB,atrim=duration={total:.3f},afade=t=in:d=0.3,"
                  f"afade=t=out:st={max(total - fo, 0):.3f}:d={fo:.3f}")
            M.run(["-i", str(video_only), "-stream_loop", "-1", "-i", str(musica),
                   "-map", "0:v", "-map", "1:a", "-af", af, "-c:v", "copy", "-c:a", "aac",
                   "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out_file)])
            print(f"   Música: {musica.name} · {args.musica_db:+.0f} dB")
        else:
            # Pista de audio en silencio: algunas plataformas rechazan vídeos sin audio
            M.run(["-i", str(video_only), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                   "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest",
                   "-movflags", "+faststart", str(out_file)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    escribir_titulos(folder, guion.get("shorts") or {})
    (folder / "montaje.srt").unlink(missing_ok=True)
    print(f"\nOK -> {out_file}  ({total:.1f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
