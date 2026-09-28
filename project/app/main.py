"""DL-SplatGenerator backend — serves the viewer and runs capture jobs.

Thin HTTP layer only: the real work lives in tools/capture.py, which stays
usable on its own from a terminal. Nothing here reimplements the pipeline.

Follows the pattern of DL-TerrainSlicer's app/main.py: routes, guards, static
mount, no logic.

The viewer itself is pure client-side and must stay that way — an exported
scene has to run on a plain static host with no backend at all. These endpoints
are additive: when the API is absent the sidebar simply does not offer to make
scenes from video.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import blender_export  # noqa: E402
import scene_paths  # noqa: E402  -- output/<name>/ or the students' folder
from jobs import QUALITY, runner  # noqa: E402

APP = Path(__file__).resolve().parent
PROJECT = APP.parent
ROOT = PROJECT.parent
INPUT = ROOT / "input"
OUTPUT = ROOT / "output"
UPLOADS = INPUT / "uploads"

VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".avi", ".mkv"}

app = FastAPI(title="DL-SplatGenerator")


def no_cache(path: Path) -> FileResponse:
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


NO_CACHE_SUFFIXES = (".html", ".js", ".css", ".json")


@app.middleware("http")
async def no_cache_app_shell(request, call_next):
    """Match launcher.py's stdlib handler: never cache the app shell.

    The catch-all StaticFiles mount below has no such header of its own, so in
    backend mode the browser cached viewer.html and its modules and edits did
    not take effect until a hard reload -- while the same files reloaded fine
    in viewer-only mode. Same rule in both modes now.
    """
    response = await call_next(request)
    if request.url.path.endswith(NO_CACHE_SUFFIXES):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/health")
def health():
    """Presence of this endpoint is how the frontend knows a backend exists."""
    return {
        "backend": True,
        "qualities": list(QUALITY),
        "python": sys.version.split()[0],
    }


def _route(quality: str = "standard") -> dict:
    sys.path.insert(0, str(PROJECT / "tools"))
    import hardware
    return hardware.plan(hardware.probe(), quality=quality)


@app.get("/api/hardware")
def hardware_info():
    """What this computer has and the route a capture will take on it
    (tools/hardware.py). Read-only; the graphics details are probed once per
    server start, the memory every call."""
    sys.path.insert(0, str(PROJECT / "tools"))
    import hardware
    hw = hardware.probe()
    route = hardware.plan(hw)
    return {"summary": hardware.summary(hw, route), "route": route,
            "cpu": hw["cpu"], "threads": hw["threads"],
            "gpu": (hw["nvidia"][0]["name"] if hw["nvidia"]
                    else hw["adapters"][0]["name"] if hw["adapters"] else None),
            "nvidia": bool(hw["nvidia"]), "memory": hw["memory"]}


@app.get("/api/jobs")
def jobs():
    return {"jobs": runner.listing()}


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    found = runner.get(job_id)
    if not found:
        raise HTTPException(404, "no such job")
    return found.public()


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    if not runner.cancel(job_id):
        raise HTTPException(409, "job is not cancellable")
    return {"cancelled": True}


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


@app.post("/api/photo")
async def photo(
    file: UploadFile | None = File(default=None),
    path: str | None = Form(default=None),
    name: str | None = Form(default=None),
    scene: str = Form(default="outdoor"),
):
    """Make a scene from ONE photograph, via metric depth.

    Seconds rather than the capture route's half hour, and it needs neither
    COLMAP nor Brush -- only the venv. What it produces is 2.5D: right from
    near the original viewpoint, stretched and holed away from it. The tool
    says so in its own capture.json and the panel says so on screen.
    """
    if scene not in ("outdoor", "indoor"):
        raise HTTPException(400, f"unknown scene type {scene!r}")

    if file is not None and file.filename:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise HTTPException(400, f"{suffix or 'that'} is not an image file")
        UPLOADS.mkdir(parents=True, exist_ok=True)
        source = UPLOADS / Path(file.filename).name
        with source.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    elif path:
        source = Path(path).expanduser().resolve()
        try:
            source.relative_to(INPUT.resolve())
        except ValueError:
            raise HTTPException(400, "path must be inside the input folder")
        if not source.is_file():
            raise HTTPException(404, f"no such file: {source}")
    else:
        raise HTTPException(400, "provide a file upload or a path")

    # depth_splat.py's own default is "<stem>_depth"; keep that so a scene made
    # from a photo is never mistaken for a capture of the same name
    scene_name = (name or f"{source.stem}_depth").strip()
    scene_name = "".join(c if (c.isalnum() or c in "-_") else "_"
                         for c in scene_name)[:60]

    started = runner.submit(source=source, name=scene_name, quality="standard",
                            privacy="off", kind="photo", scene=scene)
    return JSONResponse({"job": started.public()}, status_code=202)


MESH_QUALITY = ("draft", "standard", "high")


@app.get("/api/mesh/{name}")
def mesh_status(name: str):
    """Can this scene be meshed, is one already built, and what would it cost."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:60]
    work = ROOT / "work" / safe
    # The same choice dense_mesh.py makes, for the same reason: COLMAP writes
    # sparse/0, sparse/1, ... when a solve FRAGMENTS, and the numbering is the
    # order they finished, not their size. Ask the tool rather than repeating
    # the rule here, so the two cannot drift apart.
    sys.path.insert(0, str(PROJECT / "tools"))
    try:
        import dense_mesh as _dm
        model = _dm.find_model(work)
    except Exception:                                          # noqa: BLE001
        model = None
    # ... and the estimate must count the frames the MODEL holds, not the
    # frames on disk: a fragmented solve leaves every frame in the images
    # folder while registering a handful of them, so the estimate was quoting
    # 409 views while the actual work was two.
    images = work / "images"
    on_disk = len([p for p in images.iterdir()
                   if p.suffix.lower() in IMAGE_SUFFIXES]) if images.is_dir() else 0
    views = (_dm.registered(model) if model else 0) or on_disk

    # A scene from ONE photograph has no solve at all: its depth is estimated
    # by a network, not triangulated from several views, so there is nothing
    # for the dense pipeline to work from. Say that, instead of the generic
    # "no solved capture ... re-run the capture", which sends people to re-run
    # something that can never produce one.
    single_photo = False
    try:
        rec = json.loads((scene_paths.scene_dir(safe) / "capture.json").read_text(encoding="utf-8"))
        single_photo = rec.get("method") == "single-image metric depth"
    except (OSError, ValueError):
        pass

    blocking = []
    if single_photo:
        blocking.append("this scene was made from one photograph")
    elif not work.is_dir():
        blocking.append(f"no solved capture in work\\{safe} — the intermediates "
                        f"may have been cleared; re-run the capture")
    elif model is None:
        blocking.append(f"no COLMAP model under work\\{safe}")
    elif views == 0:
        blocking.append(f"no frames left in work\\{safe}\\images")
    if not single_photo:
        # dense stereo is CUDA-only in COLMAP: on a machine without an NVIDIA
        # card, say so rather than start a job that cannot finish
        route = _route()
        if not route["mesh"]["ok"]:
            blocking.append(route["mesh"]["why"])

    try:
        import dense_mesh
        minutes = {q: round(dense_mesh.estimate_minutes(
            views, dense_mesh.QUALITY[q]["size"],
            dense_mesh.QUALITY[q]["geometric"])) for q in MESH_QUALITY}
    except Exception:                                          # noqa: BLE001
        minutes = {}

    built = scene_paths.scene_dir(safe) / "mesh" / "mesh.json"
    record = None
    if built.is_file():
        try:
            record = json.loads(built.read_text(encoding="utf-8"))
        except ValueError:
            record = None

    return {"name": safe, "ready": not blocking, "blocking": blocking,
            "singlePhoto": single_photo, "views": views, "framesOnDisk": on_disk,
            "model": model.name if model else None,
            "estimateMinutes": minutes, "built": record,
            "cloudUrl": f"{scene_paths.scene_url(safe)}/mesh/dense.ply" if record else None,
            "meshUrl": f"{scene_paths.scene_url(safe)}/mesh/mesh.ply" if record else None}


@app.post("/api/mesh/{name}")
def mesh_build(name: str, quality: str = "standard", mesher: str = "delaunay"):
    """Build the dense, measurable surface from a capture already solved.

    Same queue and same progress card as a capture; it is the other product of
    the same solve. Minutes to an hour depending on quality — the stereo pass
    grows with the square of the image size.
    """
    if quality not in MESH_QUALITY:
        raise HTTPException(400, f"unknown quality {quality!r}")
    if mesher not in ("delaunay", "poisson"):
        raise HTTPException(400, f"unknown mesher {mesher!r}")
    state = mesh_status(name)
    if not state["ready"]:
        raise HTTPException(409, state["blocking"][0])
    started = runner.submit(source=ROOT / "work" / state["name"],
                            name=state["name"], quality=quality, privacy="off",
                            kind="mesh", mesher=mesher)
    return JSONResponse({"job": started.public()}, status_code=202)


@app.get("/api/dem/{name}")
def dem_status(name: str):
    """Is there a ground model for this scene, and can one be made.

    Two models can exist side by side: the whole scene, and a corridor around
    the walk. They answer different questions, so neither replaces the other.
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:60]
    mesh_dir = scene_paths.scene_dir(safe) / "mesh"
    mesh_url = f"{scene_paths.scene_url(safe)}/mesh"

    def record(stem: str):
        path = mesh_dir / f"{stem}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    whole, corridor, unrolled = record("dem"), record("dem_corridor"), record("dem_unrolled")
    return {"name": safe,
            "ready": (mesh_dir / "dense.ply").is_file(),
            # the walk line lives in the mesh workspace, beside the poses
            "hasPath": (ROOT / "work" / safe / "dense" / "sparse" / "images.bin").is_file(),
            "built": whole,
            "corridor": corridor,
            "unrolled": unrolled,
            "groundUrl": f"{mesh_url}/ground.ply" if whole else None,
            "demUrl": f"{mesh_url}/dem.tif" if whole else None,
            "corridorGroundUrl":
                f"{mesh_url}/ground_corridor.ply" if corridor else None,
            "corridorDemUrl":
                f"{mesh_url}/dem_corridor.tif" if corridor else None,
            "unrolledGroundUrl":
                f"{mesh_url}/ground_unrolled.ply" if unrolled else None,
            "unrolledDemUrl":
                f"{mesh_url}/dem_unrolled.tif" if unrolled else None}


@app.post("/api/dem/{name}")
def dem_build(name: str, scale: float = 0.0, cell: float = 0.0, fill: int = 4,
              corridor: float = 0.0, frames: str = "", unroll: bool = False):
    """Classify ground in the dense cloud and write a DEM.

    Synchronous on purpose: it is numpy over a few hundred thousand points and
    finishes in about a second, so a job and a progress card would be more
    ceremony than work. `scale` is metres per cloud unit — pass the viewer's
    calibration and the raster comes out in metres. `corridor` is a half-width
    in the same units; it writes the `_corridor` set instead of overwriting the
    whole-scene one. `frames` is FROM:TO, 1-based and inclusive, to take that
    corridor along one stretch of the walk rather than all of it. `unroll`
    grids that corridor in the WALK's frame instead of the world's, so a path
    that bends comes out as a straight strip — a developed strip, not a plan.
    """
    state = dem_status(name)
    if not state["ready"]:
        raise HTTPException(409, "build the mesh first — there is no dense cloud")
    if corridor > 0 and not state["hasPath"]:
        raise HTTPException(409, "a corridor needs the camera path, and this "
                                 "scene's mesh workspace has none")
    cmd = [sys.executable, "-u", str(PROJECT / "tools" / "ground_dem.py"),
           state["name"], "--scale", str(scale), "--fill", str(fill)]
    if corridor > 0:
        cmd += ["--corridor", str(corridor)]
        if frames:
            cmd += ["--frames", frames]
        if unroll:
            cmd += ["--unroll"]
    elif unroll:
        raise HTTPException(409, "unrolling lays a corridor out flat, so it "
                                 "needs a corridor to lay out")
    if cell > 0:
        cmd += ["--cell", str(cell)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    lines = [l.rstrip() for l in (proc.stdout or "").splitlines() if l.strip()]
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return JSONResponse({"ok": False, "lines": lines,
                             "error": tail[-1] if tail else
                                      f"exited with code {proc.returncode}"},
                            status_code=409)
    return {"ok": True, "lines": lines, **dem_status(name)}


# ---- terrain model for Blender, Rhino and printing (tools/package_dtm.py) ------
# Unlike the DEM above (the levelled frame, for DL-TerrainSlicer), this works in the scene's
# SITE coordinates and writes into its package (site data/terrain): a GeoTIFF, a quad mesh
# with the orthophoto, and a closed shell to cut pieces from. It needs the package's ground
# points and textured mesh; until the app builds those, a scene without them says so.

def _terrain_models(safe: str) -> list:
    """The terrain models already made for a scene, with the URLs of their files."""
    rec_dir = scene_paths.scene_dir(safe) / "mesh"
    terr = scene_paths.site_data_dir(safe) / "terrain"
    models = []
    for rec in sorted(rec_dir.glob("dsm_*.json")) + sorted(rec_dir.glob("dtm_*.json")):
        try:
            r = json.loads(rec.read_text(encoding="utf-8"))
        except ValueError:
            continue
        names = [v for k, v in (r.get("files") or {}).items() if k != "shell"]
        shells = r.get("shells") or ({f"{r['shell']['thickness']:g}": {"file": r["files"].get("shell")}}
                                     if r.get("shell") and (r.get("files") or {}).get("shell") else {})
        names += [s["file"] for s in shells.values() if s.get("file")]
        files = [{"name": nm, "url": scene_paths.path_url(terr / nm)} for nm in names if (terr / nm).is_file()]
        if files:
            models.append({"kind": r.get("kind"), "cell": r.get("cell"), "units": r.get("units"),
                           "filledShare": (r.get("grid") or {}).get("filled_share"),
                           "shells": sorted(float(k) for k in shells), "files": files})
    return models


@app.get("/api/terrain-model/{name}")
def terrain_model_status(name: str):
    """Can a terrain model be made for this scene, which cell the data gives, which exist."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:60]
    proc = subprocess.run([sys.executable, "-u", str(PROJECT / "tools" / "package_dtm.py"), safe, "--info"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    line = next((l for l in (proc.stdout or "").splitlines() if l.startswith("DTMINFO ")), None)
    if proc.returncode != 0 or not line:
        tail = (proc.stderr or "").strip().splitlines()
        return {"name": safe, "ready": False, "missing": [tail[-1] if tail else "the terrain tool did not answer"],
                "built": []}
    info = json.loads(line[len("DTMINFO "):])
    info["built"] = _terrain_models(safe) if info.get("ready") else []
    return info


@app.post("/api/terrain-model/{name}")
def terrain_model_build(name: str, source: str = "surface", cell: float = 0.0, shell: float = 0.0):
    """Make one terrain model. Synchronous, like the DEM: seconds at the default cell, up to about
    a minute at the finest the tool allows. `cell` and `shell` 0 = the tool's defaults (the cell the
    data can fill; three cells). Files that exist with the same bytes stay; different ones stop it."""
    if source not in ("ground", "surface"):
        raise HTTPException(400, "source is 'ground' or 'surface'")
    state = terrain_model_status(name)
    if not state.get("ready"):
        raise HTTPException(409, "this scene has no " + "; no ".join(state.get("missing") or ["package"]))
    cmd = [sys.executable, "-u", str(PROJECT / "tools" / "package_dtm.py"), state["name"], "--source", source]
    if cell > 0:
        cmd += ["--cell", str(cell)]
    if shell > 0:
        cmd += ["--shell", str(shell)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    out = (proc.stdout or "").splitlines()
    result = next((json.loads(l[len("DTMRESULT "):]) for l in out if l.startswith("DTMRESULT ")), None)
    lines = [l.rstrip() for l in out if l.strip() and not l.startswith("DTMRESULT ")]
    if proc.returncode != 0 or result is None:
        tail = (proc.stderr or "").strip().splitlines()
        return JSONResponse({"ok": False, "lines": lines,
                             "error": (tail[-1].replace("FAILED: ", "") if tail else
                                       f"exited with code {proc.returncode}")}, status_code=409)
    return {"ok": True, "lines": lines, "result": result, **terrain_model_status(name)}


# ---- the package: everything in one folder, and a zip (tools/build_package.py) ------

def _package_flags(levels: str, blender: str, place: bool, zip: bool) -> list:
    if levels not in ("train", "skip") or blender not in ("build", "skip"):
        raise HTTPException(400, "levels is train|skip, blender is build|skip")
    return ["--levels", levels, "--blender", blender] + ([] if place else ["--no-place"]) + (["--zip"] if zip else [])


@app.get("/api/package/{name}")
def package_status(name: str, levels: str = "train", blender: str = "build",
                   place: bool = True, zip: bool = True):
    """What building the package would do, step by step (done / to do / cannot, and why),
    for the options given; runs nothing. Plus where the package and its zip are."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:60]
    cmd = [sys.executable, "-u", str(PROJECT / "tools" / "build_package.py"), safe, "--plan",
           *_package_flags(levels, blender, place, zip)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    line = next((l for l in (proc.stdout or "").splitlines() if l.startswith("PLAN ")), None)
    if proc.returncode != 0 or not line:
        tail = (proc.stderr or "").strip().splitlines()
        return {"name": safe, "ready": False, "error": tail[-1] if tail else "the package tool did not answer",
                "steps": []}
    plan = json.loads(line[len("PLAN "):])
    zipped = scene_paths.package_zip(safe)
    running = next((j for j in runner.listing() if j["kind"] == "package" and j["name"] == safe
                    and j["status"] in ("queued", "running")), None)
    return {"name": safe, "ready": any(s["state"] == "todo" for s in plan["steps"]),
            "steps": plan["steps"],
            "folder": plan["package"],
            "zipUrl": scene_paths.path_url(zipped) if zipped.is_file() else None,
            "zipMB": round(zipped.stat().st_size / 1e6) if zipped.is_file() else None,
            "job": running}


@app.post("/api/package/{name}")
def package_build(name: str, levels: str = "train", blender: str = "build",
                  place: bool = True, zip: bool = True):
    """Build the package as a job (minutes: the lighter splats are trained; one job at a
    time, like a capture). Nothing that exists is overwritten -- see build_package.py."""
    state = package_status(name, levels, blender, place, zip)
    if state.get("job"):
        raise HTTPException(409, "this scene's package is already being built")
    if not state.get("ready"):
        raise HTTPException(409, state.get("error") or "nothing to do -- every step is made or cannot run")
    started = runner.submit(source=state["name"], name=state["name"], quality="standard", privacy="off",
                            kind="package", options={"levels": levels, "blender": blender,
                                                     "place": place, "zip": zip})
    return JSONResponse({"job": started.public()}, status_code=202)


@app.get("/api/source/{name}")
def source_info(name: str):
    """What the scene's source photo or video says about itself -- when, where,
    with what (tools/source_meta.py). Read from the file on demand, so scenes
    made before this existed get it too. Local only: nothing is sent anywhere."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:120]
    record = scene_paths.scene_dir(safe) / "capture.json"
    if not record.is_file():
        raise HTTPException(404, f"no capture record for {safe}")
    try:
        rec = json.loads(record.read_text(encoding="utf-8"))
    except ValueError:
        raise HTTPException(500, "the capture record is not readable")
    if not rec.get("source"):
        raise HTTPException(404, "the record names no source file")
    sys.path.insert(0, str(PROJECT / "tools"))
    import source_meta
    return source_meta.read(Path(rec["source"]))


# ------------------------------------------------------------ sun and north

# What output/<name>/sun.json may hold -- written by the viewer's Sun and north
# panel, read back on reload and by Blender exports (see output\for BLE\
# HANDOVER - sun - 001.txt). Vectors are in the scene file's own frame (the
# COLMAP-style frame of the .ply: Y down, Z forward), unit length.
SUN_VECTORS = ("north", "up", "to_sun")
SUN_NUMBERS = ("azimuth_deg", "elevation_deg", "measured_elevation_deg", "lat", "lon")
SUN_TEXTS = ("north_source", "north_detail", "local_time", "utc_offset", "utc")


def _sun_path(name: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:120]
    folder = scene_paths.scene_dir(safe)
    if not (folder / "capture.json").is_file():
        raise HTTPException(404, f"no scene made here called {safe}")
    return folder / "sun.json"


@app.get("/api/sun/{name}")
def sun_get(name: str):
    """The north and sun set for this scene, or {} when none has been."""
    path = _sun_path(name)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


@app.post("/api/sun/{name}")
def sun_put(name: str, payload: dict = Body(...)):
    """Keep this scene's north and sun beside it. Only known fields are
    written, and only finite numbers; a payload without north removes the file.
    Date, time and place arrive only when the user ticked that they may.

    The one exception: a "level" block (the vertical tools/scene_up.py found
    before anyone set north) survives both ways -- clearing north falls back to
    it rather than deleting the file, so a scene exported for Blender still
    stands level."""
    import math
    path = _sun_path(name)
    level = None
    if path.is_file():
        try:
            level = json.loads(path.read_text(encoding="utf-8")).get("level")
        except ValueError:
            level = None
    if not payload.get("north"):
        if isinstance(level, dict) and level.get("up"):
            path.write_text(json.dumps(
                {"format": "dlsun", "version": 1, "up": level["up"],
                 "frame": "scene file (COLMAP-style: Y down, Z forward)",
                 "level": level}, indent=1), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        _site_follow(path.parent.name)
        return {"ok": True, "removed": True}
    out = {"format": "dlsun", "version": 1}
    for key in SUN_VECTORS:
        v = payload.get(key)
        if v is None:
            continue
        if not (isinstance(v, list) and len(v) == 3
                and all(isinstance(x, (int, float)) and math.isfinite(x) for x in v)):
            raise HTTPException(400, f"{key} must be three finite numbers")
        out[key] = [float(x) for x in v]
    for key in SUN_NUMBERS:
        v = payload.get(key)
        if isinstance(v, (int, float)) and math.isfinite(v):
            out[key] = float(v)
    for key in SUN_TEXTS:
        v = payload.get(key)
        if isinstance(v, str) and len(v) <= 120:
            out[key] = v
    out["frame"] = "scene file (COLMAP-style: Y down, Z forward)"
    if isinstance(level, dict):
        out["level"] = level
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    _site_follow(path.parent.name)
    return {"ok": True}


def _site_follow(safe: str) -> None:
    """North decides site.json's Y axis: rebuild it when north changes -- in
    the background, since the viewer saves north on every move of the slider
    (debounced) and a rebuild reads the dense cloud."""
    import threading
    if (scene_paths.scene_dir(safe) / "site.json").is_file():
        threading.Thread(target=_site_rebuild, args=(safe,), daemon=True).start()


# ------------------------------------------------------------ scale and site

@app.post("/api/scale/{name}")
def scale_put(name: str, payload: dict = Body(...)):
    """Keep a scale measured by hand with the scene: capture.json's
    scale_m_per_unit (the viewer applies it on the next open) and site.json
    (tools/site_frame.py), which every export reads."""
    import math
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:120]
    record = scene_paths.scene_dir(safe) / "capture.json"
    if not record.is_file():
        raise HTTPException(404, f"no scene made here called {safe}")
    f = payload.get("m_per_unit")
    if not (isinstance(f, (int, float)) and math.isfinite(f) and f > 0):
        raise HTTPException(400, "m_per_unit must be a positive number")
    note = payload.get("note")
    rec = json.loads(record.read_text(encoding="utf-8"))
    rec["scale_m_per_unit"] = float(f)
    rec["scale_note"] = note[:200] if isinstance(note, str) else "measured in the viewer"
    record.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return {"ok": True, "site": _site_rebuild(safe)}


@app.get("/api/site/{name}")
def site_get(name: str):
    """The scene's coordinate system (site.json), or {} when none was made."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:120]
    path = scene_paths.scene_dir(safe) / "site.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _site_rebuild(safe: str) -> dict:
    """Re-run tools/site_frame.py; a scene without a solve (a photo) has none."""
    proc = subprocess.run([sys.executable, "-u", str(PROJECT / "tools" / "site_frame.py"), safe],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "error": tail[-1] if tail else f"exit {proc.returncode}"}
    return {"ok": True, **site_get(safe)}


@app.get("/api/export/blender/{name}")
def blender_status(name: str):
    """Can this scene be handed to the Blender add-on, and if not, why not."""
    try:
        return blender_export.status(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/export/blender/{name}")
def blender_run(name: str, with_splat: bool = False, with_frames: bool = False,
                replace: bool = False):
    """Write a Capture Walk package for this scene.

    Runs BLE's writer, which is the only thing that knows the format. Blocking
    on purpose: packaging takes seconds without the splat, and the frontend
    shows the elapsed time while it waits rather than pretending it is instant.
    """
    try:
        result = blender_export.run(name, with_splat=with_splat,
                                    with_frames=with_frames, replace=replace)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@app.post("/api/capture")
async def capture(
    file: UploadFile | None = File(default=None),
    path: str | None = Form(default=None),
    name: str | None = Form(default=None),
    quality: str = Form(default="standard"),
    privacy: str = Form(default="auto"),
    start: float = Form(default=0.0),
    end: float = Form(default=0.0),
):
    """Start a capture from an uploaded video, or one already on disk.

    `start` and `end` are seconds and trim the clip: only that span is ever
    extracted, so the solver and the trainer never see the rest.
    """
    if quality not in QUALITY:
        raise HTTPException(400, f"unknown quality {quality!r}")
    if privacy not in ("auto", "off"):
        raise HTTPException(400, f"unknown privacy mode {privacy!r}")

    if file is not None and file.filename:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            raise HTTPException(400, f"{suffix or 'that'} is not a video file")
        UPLOADS.mkdir(parents=True, exist_ok=True)
        source = UPLOADS / Path(file.filename).name
        with source.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    elif path:
        source = Path(path).expanduser().resolve()
        # only from the input tree, so a stray path cannot pull in the disk
        try:
            source.relative_to(INPUT.resolve())
        except ValueError:
            raise HTTPException(400, "path must be inside the input folder")
        if not source.is_file():
            raise HTTPException(404, f"no such file: {source}")
    else:
        raise HTTPException(400, "provide a file upload or a path")

    scene_name = (name or source.stem).strip() or source.stem
    # keep it filesystem- and URL-safe; the name becomes a folder and a URL
    scene_name = "".join(c if (c.isalnum() or c in "-_") else "_"
                         for c in scene_name)[:60]

    if start < 0 or end < 0:
        raise HTTPException(400, "a trim cannot start or end before zero")
    if end > 0 and end <= start:
        raise HTTPException(400, f"the trim ends ({end:g}s) at or before it "
                                 f"starts ({start:g}s)")
    started = runner.submit(source=source, name=scene_name,
                            quality=quality, privacy=privacy,
                            trim_start=start, trim_end=end)
    return JSONResponse({"job": started.public()}, status_code=202)


# ------------------------------------------------------------------ static

@app.get("/")
def index():
    return no_cache(PROJECT / "viewer.html")


@app.get("/viewer.html")
def viewer():
    return no_cache(PROJECT / "viewer.html")


for name, folder in (("input", INPUT), ("output", OUTPUT)):
    folder.mkdir(parents=True, exist_ok=True)
    app.mount(f"/{name}", StaticFiles(directory=folder), name=name)

# everything else (static/, data/, tools/) comes from the project folder
app.mount("/", StaticFiles(directory=PROJECT, html=True), name="project")
