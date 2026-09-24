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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blender_export  # noqa: E402
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

    blocking = []
    if not work.is_dir():
        blocking.append(f"no solved capture in work\\{safe} — the intermediates "
                        f"may have been cleared; re-run the capture")
    elif model is None:
        blocking.append(f"no COLMAP model under work\\{safe}")
    elif views == 0:
        blocking.append(f"no frames left in work\\{safe}\\images")

    try:
        import dense_mesh
        minutes = {q: round(dense_mesh.estimate_minutes(
            views, dense_mesh.QUALITY[q]["size"],
            dense_mesh.QUALITY[q]["geometric"])) for q in MESH_QUALITY}
    except Exception:                                          # noqa: BLE001
        minutes = {}

    built = OUTPUT / safe / "mesh" / "mesh.json"
    record = None
    if built.is_file():
        try:
            record = json.loads(built.read_text(encoding="utf-8"))
        except ValueError:
            record = None

    return {"name": safe, "ready": not blocking, "blocking": blocking,
            "views": views, "framesOnDisk": on_disk,
            "model": model.name if model else None,
            "estimateMinutes": minutes, "built": record,
            "cloudUrl": f"/output/{safe}/mesh/dense.ply" if record else None,
            "meshUrl": f"/output/{safe}/mesh/mesh.ply" if record else None}


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
    mesh_dir = OUTPUT / safe / "mesh"

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
            "groundUrl": f"/output/{safe}/mesh/ground.ply" if whole else None,
            "demUrl": f"/output/{safe}/mesh/dem.tif" if whole else None,
            "corridorGroundUrl":
                f"/output/{safe}/mesh/ground_corridor.ply" if corridor else None,
            "corridorDemUrl":
                f"/output/{safe}/mesh/dem_corridor.tif" if corridor else None,
            "unrolledGroundUrl":
                f"/output/{safe}/mesh/ground_unrolled.ply" if unrolled else None,
            "unrolledDemUrl":
                f"/output/{safe}/mesh/dem_unrolled.tif" if unrolled else None}


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
