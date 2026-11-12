"""
flow_multi.py - Reparte un guion JSON entre varias cuentas de Google Flow.

Cada cuenta tiene su propio perfil de Chrome (session/perfiles/<cuenta>) y
trabaja en su propia ventana y su propio proyecto de Flow, todas a la vez.

  python flow.py login --cuenta cuenta1          (una vez por cuenta)
  python flow.py cuentas                          (ver perfiles creados)
  python flow.py multi guion.json --cuentas cuenta1,cuenta2,cuenta3

Reglas del reparto:
  - Una imagen y los vídeos que la animan ("start": <nombre de la imagen>)
    forman un GRUPO y van siempre a la misma cuenta (se anima desde la
    tarjeta de esa imagen en el proyecto de esa cuenta).
  - Los grupos se reparten por turnos: grupo 1 -> cuenta1, grupo 2 -> cuenta2…
"""
import asyncio
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
PROFILES_DIR = BASE_DIR / "session" / "perfiles"


def profile_dir(cuenta: str) -> Path:
    safe = "".join(c for c in cuenta if c.isalnum() or c in "_-")
    if not safe:
        raise ValueError(f"Nombre de cuenta no válido: {cuenta!r}")
    return PROFILES_DIR / safe


def list_accounts() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir() and any(p.iterdir()))


def group_jobs(jobs: list[dict]) -> list[list[dict]]:
    """Agrupa cada imagen con los vídeos que la usan como fotograma inicial."""
    groups: list[list[dict]] = []
    image_group: dict[str, list[dict]] = {}
    escena_group: dict[str, list[dict]] = {}
    for i, job in enumerate(jobs, 1):
        jtype = job.get("type", "image")
        name = job.get("name") or f"{jtype}_{i:02d}"
        job = {**job, "name": name}
        start = Path(job["start"]).stem if job.get("start") else None
        if jtype == "video" and start in image_group:
            image_group[start].append(job)
            continue
        esc = str(job.get("escena") or "").strip()
        if jtype == "image" and esc:
            # Una escena entera va a la misma cuenta: las demás imágenes usan la
            # primera de la escena como referencia.
            if esc in escena_group:
                escena_group[esc].append(job)
                image_group[name] = escena_group[esc]
                continue
        group = [job]
        if jtype == "image" and esc:
            escena_group[esc] = group
        groups.append(group)
        if jtype == "image":
            image_group[name] = group
    return groups


def split_for_accounts(data: dict, json_dir: Path, cuentas: list[str]) -> dict[str, dict]:
    """Devuelve {cuenta: guion_json} con los grupos repartidos por turnos."""
    # Rutas de referencias en absoluto: el sub-guion se guarda en otra carpeta.
    refs = {}
    for key, ref_path in (data.get("references") or {}).items():
        cand = [Path(ref_path), json_dir / ref_path]
        found = next((c for c in cand if c.exists()), None)
        if not found:
            raise FileNotFoundError(f"No encuentro la referencia '{key}': {ref_path}")
        refs[key] = str(found.resolve())

    groups = group_jobs(data.get("jobs", []))
    per_account = {c: [] for c in cuentas}
    for idx, group in enumerate(groups):
        per_account[cuentas[idx % len(cuentas)]].extend(group)

    out = {}
    for cuenta, jobs in per_account.items():
        if not jobs:
            continue
        out[cuenta] = {
            "project": cuenta,
            "references": refs,
            "defaults": data.get("defaults", {}),
            "jobs": jobs,
        }
    return out


async def _run_account(cuenta: str, jobfile: Path, out_dir: Path, delay: float) -> int:
    await asyncio.sleep(delay)  # escalonar el arranque de los Chrome
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(BASE_DIR / "flow.py"), "--cuenta", cuenta,
        "batch", str(jobfile), "--out", str(out_dir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(BASE_DIR),
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        print(f"[{cuenta}] {line}", flush=True)
    return await proc.wait()


async def cmd_multi(args) -> int:
    cuentas = [c.strip() for c in args.cuentas.split(",") if c.strip()]
    if not cuentas:
        print("Indica las cuentas: --cuentas cuenta1,cuenta2")
        return 1
    missing = [c for c in cuentas if not (profile_dir(c).exists() and any(profile_dir(c).iterdir()))]
    if missing:
        for c in missing:
            print(f"SIN SESION en la cuenta '{c}'. Corre: python flow.py login --cuenta {c}")
        return 1

    jobfile = Path(args.jobfile)
    data = json.loads(jobfile.read_text(encoding="utf-8"))
    project = data.get("project") or jobfile.stem
    out_dir = Path(args.out) / "".join(c for c in project if c.isalnum() or c in "_-.")
    plans_dir = out_dir / "_cuentas"
    plans_dir.mkdir(parents=True, exist_ok=True)

    try:
        plans = split_for_accounts(data, jobfile.resolve().parent, cuentas)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1

    print(f"MULTI '{project}': {len(data.get('jobs', []))} trabajos en {len(plans)} cuentas.")
    files = {}
    for cuenta, plan in plans.items():
        names = ", ".join(j["name"] for j in plan["jobs"])
        print(f"  {cuenta}: {len(plan['jobs'])} trabajos -> {names}")
        f = plans_dir / f"{cuenta}.json"
        f.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        files[cuenta] = f
    print()

    codes = await asyncio.gather(*[
        _run_account(c, f, out_dir, delay=i * args.escalonar)
        for i, (c, f) in enumerate(files.items())
    ])

    # Resumen de todas las cuentas
    print("\n=== RESUMEN MULTI ===")
    total_ok = total = 0
    for (cuenta, _), code in zip(files.items(), codes):
        report = out_dir / cuenta / "batch_report.json"
        try:
            rep = json.loads(report.read_text(encoding="utf-8"))
            results = rep.get("results", [])
            ok = sum(1 for r in results if r.get("ok"))
            total_ok += ok
            total += len(results)
            print(f"  {cuenta}: {ok}/{len(results)} OK  ·  {rep.get('flow_project_url') or 'sin enlace'}")
            for r in results:
                if not r.get("ok"):
                    print(f"      ERROR {r['name']}: {r.get('error')}")
        except Exception:
            print(f"  {cuenta}: no terminó bien (código {code}). Revisa sus líneas [{cuenta}] arriba.")
    print(f"\nTOTAL: {total_ok}/{total} OK. Carpeta: {out_dir}")
    return 0 if total and total_ok == total else 1


async def cmd_cuentas(_args) -> int:
    accounts = list_accounts()
    if not accounts:
        print("No hay cuentas. Crea una con: python flow.py login --cuenta cuenta1")
        return 1
    print("Cuentas con sesión guardada:")
    for a in accounts:
        print(f"  - {a}")
    return 0
