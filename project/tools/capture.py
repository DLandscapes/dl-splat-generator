"""DL-SplatGenerator capture pipeline: video -> frames -> camera poses -> splats.

Turns a phone video (or a folder of stills) into a .ply the viewer can open.

    python capture.py "..\\..\\input\\data\\IMG_8950.MOV"
    python capture.py <video> --stride 3 --steps 15000 --max-splats 800000
    python capture.py <video> --dry-run          # print the commands, run nothing
    python capture.py <video> --from poses       # resume, reusing existing frames

Stages
  1 frames   ffmpeg extracts every Nth frame, honouring the rotation flag so
             portrait video does not come out sideways
  2 prune    the blurriest frames are dropped -- motion blur is the single
             biggest cause of a failed reconstruction from handheld video
  3 poses    COLMAP: feature extraction, matching, mapping, then undistortion
             into the layout every 3DGS trainer expects
  4 train    Brush optimises the splats and exports .ply
  5 place    the result is copied to output\\<name>\\ and can be opened in the
             viewer at /input/... or dragged in

Everything third-party lives in DL-SplatGenerator\\bin and is found automatically.
Nothing is installed system-wide.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- locations

TOOLS = Path(__file__).resolve().parent
PROJECT = TOOLS.parent                    # ...\DL-SplatGenerator\project
ROOT = PROJECT.parent                     # ...\DL-SplatGenerator
BIN = ROOT / "bin"
OUTPUT = ROOT / "output"
WORK = ROOT / "work"

VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".avi", ".mkv"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def find_binary(name: str, *hints: str) -> Path | None:
    """Locate a bundled executable under bin\\, else fall back to PATH."""
    for hint in hints:
        candidate = BIN / hint
        if candidate.is_file():
            return candidate
    matches = sorted(BIN.rglob(name))
    if matches:
        return matches[0]
    found = shutil.which(name)
    return Path(found) if found else None


FFMPEG = find_binary("ffmpeg.exe")
FFPROBE = find_binary("ffprobe.exe")
COLMAP = find_binary("colmap.exe", "colmap/bin/colmap.exe")
BRUSH = find_binary("brush_app.exe", "brush/brush_app.exe")
NODE = shutil.which("node")


class StageError(RuntimeError):
    """A pipeline stage failed for a reason worth reporting plainly."""


# ------------------------------------------------------------------ helpers

def human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


# ---- progress lines -------------------------------------------------------
#
# Every long-running tool prints its own counter (COLMAP "Processed file
# [12/118]", Brush "Refine iter 1500"). Those are re-emitted here in ONE shape,
#     ~ progress <done>/<total> <what>
# throttled to about one line a second, so the job runner behind the "Make a
# scene" panel can show real numbers -- and a finish estimate -- instead of a
# bar that sits still for twenty minutes. Nothing else in the tools' chatter
# is echoed; the pipeline log stays readable.

_progress_last = {"t": 0.0, "key": None}


def progress(done: int, total: int, what: str) -> None:
    now = time.time()
    key = (done, total, what)
    if key == _progress_last["key"]:
        return
    if done < total and now - _progress_last["t"] < 1.0:
        return
    _progress_last.update(t=now, key=key)
    print(f"    ~ progress {done}/{total} {what}", flush=True)


def counter(pattern: str, what: str, total: int | None = None):
    """Build a line parser: regex with one or two integer groups -> progress.

    With two groups the tool reports "done/total" itself; with one, `total` is
    what we know it to be (COLMAP's mapper only says how many are placed).
    """
    rx = re.compile(pattern)

    def parse(line: str):
        m = rx.search(line)
        if not m:
            return None
        done = int(m.group(1))
        tot = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else total
        return (done, tot, what) if tot else None
    return parse


def run(cmd: list, *, label: str, dry: bool, cwd: Path | None = None,
        env: dict | None = None, parse=None) -> None:
    """Run a subprocess, echoing only progress, surfacing failures clearly.

    `parse(line)` may return (done, total, what) for a line it recognises; those
    become the throttled "~ progress" lines above. The last 15 lines of the
    tool's output are kept for the error message, nothing else is shown.
    """
    printable = " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)
    if dry:
        print(f"    [dry-run] {printable}")
        return
    started = time.time()
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    proc = subprocess.Popen(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=full_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    tail: collections.deque = collections.deque(maxlen=15)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        tail.append(line)
        if parse is not None:
            hit = parse(line)
            if hit:
                progress(*hit)
    code = proc.wait()
    if code != 0:
        raise StageError(f"{label} failed (exit {code}).\n"
                         f"  command: {printable}\n"
                         f"  output tail:\n" + "\n".join(tail))
    print(f"    done in {human(time.time() - started)}")


# ------------------------------------------------------------- 1: extract

def extract_frames(video: Path, images: Path, *, stride: int, max_frames: int | None,
                   max_width: int | None, dry: bool,
                   start: float = 0.0, end: float = 0.0) -> None:
    window = ""
    if start > 0 or end > 0:
        window = f", {start:g}s to {end:g}s" if end > 0 else f", from {start:g}s"
    print(f"[1/5] frames  {video.name} -> every "
          f"{stride}{'st' if stride == 1 else 'th'} frame{window}")
    if images.exists() and any(images.iterdir()):
        print(f"    reusing {len(list(images.iterdir()))} frames already in {images}")
        return
    images.mkdir(parents=True, exist_ok=True)

    # select drops all but every Nth frame; vsync 0 stops ffmpeg re-timing them.
    # ffmpeg applies the container rotation flag by default, so portrait video
    # comes out portrait rather than on its side.
    filters = [f"select=not(mod(n\\,{stride}))"]
    if max_width:
        filters.append(f"scale='min({max_width},iw)':-2")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    # TRIM. -ss BEFORE -i seeks by keyframe index rather than decoding the
    # whole file to the cut, which on a phone clip is the difference between
    # instant and most of a minute. -t is the DURATION from that point, not an
    # absolute end, so it has to be the difference. Both are applied before the
    # select filter, so "every Nth frame" counts from the start of the trim and
    # the stride means the same thing whatever is cut.
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", video]
    if end > start:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-vf", ",".join(filters), "-vsync", "0", "-q:v", "2"]
    if max_frames:
        cmd += ["-frames:v", str(max_frames)]
    cmd += [images / "frame_%05d.jpg"]
    run(cmd, label="ffmpeg frame extraction", dry=dry)
    if not dry:
        print(f"    {len(list(images.glob('*.jpg')))} frames written")


def copy_stills(folder: Path, images: Path, *, dry: bool) -> None:
    print(f"[1/5] frames  copying stills from {folder}")
    if dry:
        print("    [dry-run] copy images")
        return
    images.mkdir(parents=True, exist_ok=True)
    n = 0
    for src in sorted(folder.iterdir()):
        if src.suffix.lower() in IMAGE_SUFFIXES:
            shutil.copy2(src, images / src.name)
            n += 1
    print(f"    {n} images copied")


# --------------------------------------------------------------- 2: prune

def sharpness(path: Path) -> float:
    """Variance of the Laplacian on a downscaled grey copy: higher is sharper."""
    from PIL import Image
    import numpy as np

    with Image.open(path) as im:
        im = im.convert("L")
        im.thumbnail((512, 512))
        a = np.asarray(im, dtype=np.float32)
    # 4-neighbour Laplacian, interior only
    lap = (a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
           - 4.0 * a[1:-1, 1:-1])
    return float(lap.var())


def prune_blurry(images: Path, rejected: Path, *, drop_pct: float, dry: bool) -> None:
    print(f"[2/5] prune   dropping the blurriest {drop_pct:.0f}% of frames")
    frames = sorted(images.glob("*.jpg")) + sorted(images.glob("*.png"))
    if drop_pct <= 0 or len(frames) < 20:
        print("    skipped")
        return
    if rejected.is_dir() and any(rejected.iterdir()):
        # a re-run on reused frames: they were pruned last time, and pruning
        # the survivors again would throw away another 15% of good frames
        print(f"    already pruned ({len(list(rejected.iterdir()))} in "
              f"rejected\\); keeping the {len(frames)} frames as they are")
        return
    if dry:
        print(f"    [dry-run] would score {len(frames)} frames and drop "
              f"{int(len(frames) * drop_pct / 100)}")
        return

    started = time.time()
    scored = []
    for i, f in enumerate(frames, 1):
        scored.append((sharpness(f), f))
        if i % 10 == 0 or i == len(frames):
            progress(i, len(frames), "frames checked for blur")
    scored.sort(key=lambda t: t[0])
    n_drop = int(len(scored) * drop_pct / 100)
    # Rejected frames must land OUTSIDE the image folder: COLMAP recurses into
    # subdirectories, so a rejected\ folder nested under images\ gets read back
    # in and the pruning silently does nothing.
    rejected.mkdir(parents=True, exist_ok=True)
    for score, path in scored[:n_drop]:
        shutil.move(str(path), str(rejected / path.name))
    kept = [s for s, _ in scored[n_drop:]]
    print(f"    scored {len(scored)} in {human(time.time() - started)}; "
          f"dropped {n_drop}, kept {len(kept)}")
    if kept:
        print(f"    sharpness kept: min {min(kept):.0f}  median "
              f"{sorted(kept)[len(kept) // 2]:.0f}  max {max(kept):.0f}")


# --------------------------------------------------------------- 3: poses

VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def mask_privacy(images: Path, report: Path, *, mode: str, regions: list,
                 dry: bool) -> dict | None:
    """Blur faces before anything downstream sees the frames.

    Deliberately placed before pose solving: once COLMAP and the trainer have
    consumed the frames, faces are baked into the splats and cannot be removed
    without redoing the whole run.
    """
    if mode == "off":
        print("[2b/5] privacy  SKIPPED (--privacy off)")
        print("       nothing was masked; do not publish without reviewing")
        return None
    print("[2b/5] privacy  masking faces")
    if not VENV_PYTHON.is_file():
        print(f"    (no venv at {VENV_PYTHON}; skipping)")
        print("    WARNING: frames are unmasked")
        return None
    cmd = [VENV_PYTHON, TOOLS / "privacy_mask.py", images, "--report", report]
    for r in regions:
        cmd += ["--mask-region", r]
    if dry:
        print(f"    [dry-run] {' '.join(str(c) for c in cmd)}")
        return None
    proc = subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise StageError("privacy masking failed:\n"
                         + (proc.stderr or proc.stdout or "")[-800:])
    try:
        stats = json.loads(report.read_text())
    except Exception:                                        # noqa: BLE001
        stats = {}
    print(f"    {stats.get('faces_detected', '?')} faces in "
          f"{stats.get('frames_with_faces', '?')} of "
          f"{stats.get('frames', '?')} frames; masked "
          f"{stats.get('frames_masked', '?')}")
    if stats.get("detections"):
        names = [d["file"] for d in stats["detections"]][:6]
        print(f"    review these: {', '.join(names)}"
              + (" …" if len(stats["detections"]) > 6 else ""))
    print("    NOTE: faces only. Plates, signage and windows are not detected.")
    return stats


def source_fps(video: Path) -> float | None:
    """Frames per second of the source video, or None.

    Recorded in capture.json so that two other things do not have to ask
    ffprobe again later: the viewer plays the capture walk at the speed it was
    actually filmed at (kept frames per second = fps / stride), and BLE's
    Capture Walk exporter can read it instead of probing.
    """
    if not FFPROBE or not video.is_file():
        return None
    try:
        out = subprocess.run(
            [str(FFPROBE), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=30).stdout.strip().rstrip(",")
        num, den = out.split("/")
        fps = float(num) / float(den)
        return round(fps, 3) if fps > 0 else None
    except Exception:                                          # noqa: BLE001
        return None


def focal_prior(images: Path, focal_35mm: float) -> str | None:
    """OPENCV camera params (fx, fy, cx, cy, k1, k2, p1, p2) from a 35 mm-equiv.

    Video frames carry no EXIF, so COLMAP otherwise guesses a focal length of
    1.2 x the longest side -- about 60% too long for a phone, which an iPhone
    14 Pro Max clip solved to 26.9 mm-equivalent. Bundle adjustment refines the
    prior, so this is only a better starting point than a wrong guess.

    Measured, not assumed: on the forward-walking clip, with the mapper
    initialisation settings in MAPPER_ATTEMPTS, OPENCV with this prior and
    OPENCV without it both placed 118 of 118 frames on three pinned seeds
    (~34.7k vs ~35.1k points, 0.548 px either way). So the prior is insurance
    against a bad guess, not the thing that makes a forward walk work -- that
    is the initialisation ladder.
    """
    if focal_35mm <= 0:
        return None
    first = next((p for p in sorted(images.iterdir())
                  if p.suffix.lower() in IMAGE_SUFFIXES), None)
    if first is None:
        return None
    from PIL import Image
    with Image.open(first) as im:
        w, h = im.size
    f = max(w, h) * focal_35mm / 36.0
    print(f"    focal prior {f:.0f}px from {focal_35mm:g} mm-equivalent "
          f"({w}x{h})")
    return f"{f:.1f},{f:.1f},{w / 2:.1f},{h / 2:.1f},0,0,0,0"


def registered_frames(model: Path) -> int:
    """How many images a sparse model holds, from images.bin's leading count."""
    path = model / "images.bin"
    if not path.is_file():
        return 0
    with path.open("rb") as fh:
        return struct.unpack("<Q", fh.read(8))[0]


# COLMAP's mapper has to pick an initial image pair before it can place
# anything, and it refuses a pair whose motion is mostly forward
# (init_max_forward_motion, default 0.95) or whose triangulation angle is under
# init_min_tri_angle (default 16 deg). On a capture that walks straight ahead
# every pair is both, so it falls back on whatever its randomised trials
# stumble into: measured 2026-09-06 on one clip, the identical database gave
# 118 frames on one run and 2 on the next two. Hence this ladder -- each rung
# loosens initialisation for forward motion, and the seed is pinned so a result
# is reproducible instead of a lottery. A failing attempt costs seconds.
MAPPER_ATTEMPTS = [
    ("", []),
    ("initialisation settings for forward motion", [
        "--Mapper.init_min_tri_angle", "4",
        "--Mapper.init_max_forward_motion", "1.0",
        "--Mapper.init_num_trials", "500"]),
    ("the lowest initialisation thresholds", [
        "--Mapper.init_min_tri_angle", "2",
        "--Mapper.init_max_forward_motion", "1.0",
        "--Mapper.init_num_trials", "1000",
        "--Mapper.init_max_error", "6",
        "--Mapper.abs_pose_min_num_inliers", "20"]),
]


def _exit_code(exc: StageError) -> int | None:
    m = re.search(r"\(exit (-?\d+)\)", str(exc).splitlines()[0])
    return int(m.group(1)) if m else None


def run_adaptive(build, *, gpu: bool, threads: int, label: str, dry: bool,
                 before_retry=None, parse=None) -> dict:
    """Run a COLMAP step on the fastest route, and step down when it fails.

    `build(gpu, threads)` gives the command. The ladder: the NVIDIA card if the
    plan has one -> the processor at the planned thread count -> half as many
    threads -> ... -> one. A failure is only allowed to END the capture on the
    last rung: on a laptop, slower is fine and failing is not. Measured reason
    for the thread rungs: this COLMAP build's CPU feature extraction crashes
    intermittently above ~12 threads (tools/hardware.py). `before_retry()`
    cleans up whatever a crashed attempt left half-written.
    Returns what finally ran, for the record.
    """
    rungs = [(True, threads)] if gpu else []
    t = threads
    while True:
        rungs.append((False, t))
        if t <= 1:
            break
        t = max(1, t // 2)
    last = None
    for i, (use_gpu, n) in enumerate(rungs):
        if i:
            where = "the processor" if not use_gpu else "the graphics card"
            print(f"    {label} failed ({str(last).splitlines()[0]}); "
                  f"retrying on {where} with {n} thread{'s' * (n != 1)}")
            if before_retry and not dry:
                before_retry()
        try:
            run(build(use_gpu, n), label=label, dry=dry, parse=parse)
            return {"gpu": use_gpu, "threads": None if use_gpu else n,
                    "retries": i}
        except StageError as exc:
            last = exc
    raise last


def run_colmap(work: Path, *, matcher: str, focal_35mm: float, dry: bool,
               gpu: bool = True, threads: int = 12) -> tuple[Path, str, dict]:
    """Solve camera poses. Returns the undistorted dataset, which mapper
    initialisation settings produced it, and which route features and matching
    took -- for the record in capture.json."""
    route = "the NVIDIA card" if gpu else f"the processor, {threads} threads"
    print(f"[3/5] poses   COLMAP ({matcher} matching, features on {route})")
    images = work / "images"
    database = work / "database.db"
    sparse = work / "sparse"
    undist = work / "undistorted"

    if not dry and not any(images.glob("*")):
        raise StageError(f"no images to work from in {images}")

    if not dry:
        # A re-run must start from a clean database: COLMAP reuses whatever
        # camera model and features an existing database.db holds, so the
        # camera settings below would silently not apply. These are the
        # pipeline's own intermediates under work\, rebuilt in full here.
        stale = [p for p in (database, sparse, work / "sparse_best", undist)
                 if p.exists()]
        if stale:
            print(f"    clearing previous pose data in {work.name}\\ "
                  f"({', '.join(p.name for p in stale)})")
            for p in stale:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
    sparse.mkdir(parents=True, exist_ok=True)
    n_images = len([p for p in images.iterdir()
                    if p.suffix.lower() in IMAGE_SUFFIXES]) if not dry else 0

    print("    feature extraction")
    base = [COLMAP, "feature_extractor",
           "--database_path", database,
           "--image_path", images,
           # one physical camera shot the whole clip, so solving a single shared
           # intrinsic is both faster and better conditioned
           "--ImageReader.single_camera", "1",
           # OPENCV (fx, fy, cx, cy, k1, k2, p1, p2): radial AND tangential
           # distortion. A phone lens has both, and under-modelled distortion
           # bends straight lines into the reconstruction -- which this tool
           # then measures. SIMPLE_RADIAL was tried on 2026-09-06 and reverted:
           # it mapped faster and found ~6% more points, but fits worse (0.56
           # vs 0.55 px here, 0.79 vs 0.52 px on the smoke clip) and, once the
           # mapper initialisation was fixed, made no difference to whether a
           # forward walk reconstructs at all.
           "--ImageReader.camera_model", "OPENCV"]
    params = focal_prior(images, focal_35mm) if not dry else None
    if params:
        base += ["--ImageReader.camera_params", params]

    def extract(use_gpu: bool, n: int) -> list:
        # COLMAP 4.x renamed these from SiftExtraction/SiftMatching.use_gpu.
        # On the processor the thread count is ALWAYS given: COLMAP's default
        # (every core) crashes this build (tools/hardware.py).
        return base + ["--FeatureExtraction.use_gpu", "1" if use_gpu else "0",
                       *([] if use_gpu else ["--FeatureExtraction.num_threads", str(n)])]

    def fresh_database():
        # a crashed extraction leaves some images' features in the database;
        # the retry starts clean rather than trusting a half-written one
        database.unlink(missing_ok=True)

    routes = {"features": run_adaptive(
        extract, gpu=gpu, threads=threads, label="COLMAP feature_extractor",
        dry=dry, before_retry=fresh_database,
        parse=counter(r"Processed file \[(\d+)/(\d+)\]", "frames scanned for features"))}

    print("    matching")
    on = lambda use_gpu, n: ["--FeatureMatching.use_gpu", "1" if use_gpu else "0",   # noqa: E731
                             *([] if use_gpu else ["--FeatureMatching.num_threads", str(n)])]
    if matcher == "sequential":
        # Video frames arrive in order, so only nearby frames can overlap.
        # Loop detection lets a capture that returns to its starting point close
        # the loop, which handheld orbits around a subject usually do.
        # (Its vocabulary-tree indexing is most of this step's time.)
        index = counter(r"Indexing image \[(\d+)/(\d+)\]", "frames indexed for loop detection")
        match = counter(r"Processing image \[(\d+)/(\d+)\]", "frames matched")
        routes["matching"] = run_adaptive(
            lambda g, n: [COLMAP, "sequential_matcher",
                          "--database_path", database,
                          "--SequentialMatching.overlap", "10",
                          "--SequentialMatching.loop_detection", "1", *on(g, n)],
            gpu=gpu, threads=threads, label="COLMAP sequential_matcher", dry=dry,
            parse=lambda line: index(line) or match(line))
    else:
        routes["matching"] = run_adaptive(
            lambda g, n: [COLMAP, "exhaustive_matcher",
                          "--database_path", database, *on(g, n)],
            gpu=gpu, threads=threads, label="COLMAP exhaustive_matcher", dry=dry,
            parse=counter(r"Matching block \[(\d+)/(\d+)", "frame blocks matched"))

    print("    mapping (this is the slow one)")
    track = counter(r"num_reg_frames=(\d+)\)", "frames placed", total=n_images)
    if dry:
        run([COLMAP, "mapper",
             "--database_path", database,
             "--image_path", images,
             "--output_path", sparse],
            label="COLMAP mapper", dry=dry, parse=track)
        return work / "undistorted", "default", routes

    # Good enough to stop retrying. Deliberately stricter than the 50% bar
    # main() rejects a reconstruction at: a frame that is not placed is a piece
    # of the walk missing from the scene, and another attempt costs seconds
    # when it fails and a couple of minutes when it works -- against a training
    # stage that has to be thrown away if the poses are thin. Whatever the best
    # attempt managed is kept either way, so a capture that genuinely tops out
    # below this still finishes.
    enough = max(MIN_USEFUL_VIEWS, round(0.8 * n_images))
    keep = work / "sparse_best"
    best, best_count, best_label = None, -1, "default"
    last_failure = ""
    for attempt, (label, extra) in enumerate(MAPPER_ATTEMPTS):
        if attempt:
            so_far = (f"only {best_count} of {n_images} frames placed"
                      if best_count >= 0 else "the mapper could not start at all")
            print(f"    WARNING: {so_far}; retrying with {label}")
        if sparse.exists():
            shutil.rmtree(sparse)
        sparse.mkdir(parents=True, exist_ok=True)
        try:
            run([COLMAP, "mapper",
                 "--database_path", database,
                 "--image_path", images,
                 "--output_path", sparse,
                 # pinned so the same clip gives the same answer every time
                 "--Mapper.random_seed", "0", *extra],
                label="COLMAP mapper", dry=dry, parse=track)
        except StageError as exc:
            # !! A mapper that EXITS NON-ZERO must not end the ladder. COLMAP
            # returns 1 when it cannot find an initial pair -- which is the
            # exact failure the relaxed initialisation settings below exist to
            # rescue -- so aborting here spent one attempt on a walk and threw
            # away the two that were meant to save it. Caught on a 3.6 s clip
            # whose first attempt died with "no initial pair" (2026-09-22).
            last_failure = str(exc).splitlines()[0]
            print(f"    the mapper gave up at these settings ({last_failure})")
            continue

        found = sorted((p for p in sparse.iterdir() if p.is_dir()),
                       key=registered_frames, reverse=True)
        count = registered_frames(found[0]) if found else 0
        if len(found) > 1:
            counts = ", ".join(str(registered_frames(m)) for m in found)
            print(f"    {len(found)} disconnected models ({counts} frames); "
                  f"using the largest")
        if count > best_count:
            best_count = count
            best_label = label or "default"
            if keep.exists():
                shutil.rmtree(keep)
            if found:
                shutil.copytree(found[0], keep)
                best = keep
        if count >= enough:
            break

    if best is None:
        raise StageError(
            f"COLMAP mapped no model from {n_images} frames, at any of its "
            f"{len(MAPPER_ATTEMPTS)} initialisation settings"
            + (f" (last: {last_failure})" if last_failure else "") + ". "
            "The usual causes are too little overlap between frames, heavy "
            "motion blur, a scene without enough texture, or a clip too short "
            "to give the solver a baseline. Try --stride 2 for more frames, "
            "or --blur-drop 25.")
    model = best
    print(f"    registered model: {model} "
          f"({best_count} of {n_images} frames placed)")
    mapper_used = best_label

    # Undistort into the layout every 3DGS trainer expects: undistorted images
    # beside a PINHOLE sparse model.
    print("    undistorting")
    run([COLMAP, "image_undistorter",
         "--image_path", images,
         "--input_path", model,
         "--output_path", undist,
         "--output_type", "COLMAP"],
        label="COLMAP image_undistorter", dry=dry,
        parse=counter(r"Undistorting image \[(\d+)/(\d+)\]", "frames undistorted"))

    # image_undistorter writes sparse/ directly; trainers look for sparse/0/
    flat = undist / "sparse"
    nested = flat / "0"
    if flat.is_dir() and not nested.is_dir():
        nested.mkdir(parents=True, exist_ok=True)
        for f in list(flat.iterdir()):
            if f.is_file():
                shutil.move(str(f), str(nested / f.name))
    return undist, mapper_used, routes


def pose_summary(undist: Path, images: Path) -> dict:
    """How many cameras COLMAP actually registered, read from images.bin.

    The total is the number of frames that went IN (work\\images), not the
    undistorted folder: that only ever holds the registered ones, which is how
    a 2-of-118 failure once reported itself as "registered 2 of 2 images".
    """
    if not (undist / "sparse" / "0" / "images.bin").is_file():
        return {}
    count = registered_frames(undist / "sparse" / "0")
    n_images = len([p for p in images.iterdir()
                    if p.suffix.lower() in IMAGE_SUFFIXES]) if images.is_dir() else 0
    return {"registered": count, "images": n_images}


# --------------------------------------------------------------- 4: train

# Brush decodes every training view into memory and keeps it there. Past this
# many views the reconstruction gains little but the memory cost keeps rising,
# and a 400-frame set at full resolution reliably exhausts the allocator.
TRAIN_VIEW_TARGET = 250

# Below this many registered views a reconstruction is not worth trusting; a
# 6-second test clip once registered 2 and still reported success.
MIN_USEFUL_VIEWS = 20


def auto_subsample(dataset: Path, requested: int | None) -> int | None:
    """Pick a frame stride that keeps training inside memory.

    Learned the hard way: a 409-frame set died allocating 1.5 GB *after* 23
    minutes of pose solving. Choosing this automatically means a long run does
    not fail at the last stage.
    """
    if requested:
        return requested
    images = dataset / "images"
    n = len(list(images.glob("*"))) if images.is_dir() else 0
    if n <= TRAIN_VIEW_TARGET:
        return None
    stride = max(2, round(n / TRAIN_VIEW_TARGET))
    print(f"    {n} views registered; training on every {stride}th "
          f"(~{n // stride}) to stay within memory")
    return stride


def run_brush(dataset: Path, out_dir: Path, *, steps: int, max_splats: int | None,
              sh_degree: int, max_resolution: int, subsample: int | None,
              max_frames: int | None, viewer: bool, dry: bool) -> None:
    print(f"[4/5] train   Brush, {steps} steps, up to {max_resolution} px")
    out_dir.mkdir(parents=True, exist_ok=True)
    subsample = auto_subsample(dataset, subsample)
    if not dry:
        n = len(list((dataset / "images").glob("*"))) if (dataset / "images").is_dir() else 0
        memory_check(n // (subsample or 1), max_resolution)

    def build(stride: int | None, resolution: int) -> list:
        # Brush decodes the whole dataset into memory, so views times resolution
        # squared is what decides whether it fits.
        cmd = [BRUSH, dataset,
               "--total-steps", str(steps),
               "--sh-degree", str(sh_degree),
               "--max-resolution", str(resolution),
               "--export-every", str(steps),
               "--export-path", out_dir,
               "--export-name", "splat_{iter}.ply"]
        if stride:
            cmd += ["--subsample-frames", str(stride)]
        if max_frames:
            cmd += ["--max-frames", str(max_frames)]
        if max_splats:
            cmd += ["--max-splats", str(max_splats)]
        if viewer:
            cmd += ["--with-viewer"]
        return cmd

    # Fallbacks for memory exhaustion, cheapest loss first. Resolution is
    # reduced before views because memory goes with the square of resolution
    # but only linearly with view count -- and on a walking capture every view
    # covers new ground, so discarding views costs coverage that cannot be
    # recovered, while a smaller image mostly costs fine detail.
    attempts = [
        (subsample, max_resolution, "Brush training"),
        (subsample, int(max_resolution * 0.7), "Brush training (lower resolution)"),
        ((subsample or 1) * 2, int(max_resolution * 0.7),
         "Brush training (lower resolution, fewer views)"),
    ]
    # Brush is silent on the console unless asked; at info level brush_cli
    # reports "Refine iter N, K splats." every refinement, which is the only
    # live view of training there is. Only those two crates -- the wgpu
    # shader compiler alone logs 100k lines at debug.
    env = {"RUST_LOG": "brush_cli=info,brush_process=info"}
    steps_done = counter(r"Refine iter (\d+)", "training steps", total=steps)

    last: StageError | None = None
    for stride, resolution, label in attempts:
        try:
            if last is not None:
                print(f"    out of memory; retrying at {resolution}px"
                      + (f", every {stride}th view" if stride else ""))
            run(build(stride, resolution), label=label, dry=dry, env=env,
                parse=steps_done)
            return
        except StageError as exc:
            # Only memory exhaustion is worth retrying: it costs minutes,
            # against losing the half hour of pose solving that came before.
            # !! Rust reports it more than one way. "TryReserveError"/"AllocError"
            # (a failed reservation while decoding a view) was NOT recognised
            # until 2026-09-26, so student capture B and IMG_1779 each died on the
            # first attempt instead of retrying smaller.
            if not any(s in str(exc) for s in OOM_MARKERS):
                raise
            last = exc
    raise last if last else StageError("Brush training failed")


OOM_MARKERS = ("memory allocation", "allocation of", "TryReserveError", "AllocError",
               "out of memory", "OutOfMemory")


def memory_check(views: int, resolution: int) -> None:
    """Before training: is there memory for it? Measured 2026-09-26 on 103
    views: Brush's peak RAM was 2.4 GB at 1200 px and 2.9 GB at 1920 px -- the
    views barely count, the rest is fixed. A machine already full --
    two Blender windows and a test run -- made Brush fail on a 9.7 MB buffer
    (student capture B). Warn, with the figure, so the cause is on screen and
    not guessed; the retry ladder then trains smaller if it must."""
    try:
        import hardware
        free = hardware.memory()
    except Exception:                                   # noqa: BLE001
        return
    avail = min(free["free_gb"], free["commit_free_gb"] or free["free_gb"])
    # fitted to the two measurements (2.4 GB at 1200 px, 2.9 GB at 1920 px),
    # plus 1 GB of margin for everything else Brush and the system need
    need = 1.0 + 2.1 + 0.8 * (views / 103) * (resolution / 1920) ** 2
    print(f"    memory: {avail:.1f} GB free, training needs about {need:.1f} GB")
    if avail < need:
        print(f"    WARNING: less memory free than training needs. Close other "
              f"programs (Blender, browsers with many tabs) -- or it will retry "
              f"at a lower resolution, which is softer.")


def compress_to_spz(ply: Path, *, dry: bool) -> Path | None:
    """Write an SPZ beside the .ply, and return it.

    Brush emits PLY, which is uncompressed: a 2.4M-splat capture is ~570 MB and
    takes the better part of a minute to open. SPZ is about a tenth of that with
    no visible loss, so the viewer should be loading it instead. The .ply is
    kept -- it is the archival original and what other tools expect.
    """
    if NODE is None:
        print("    (node not found; skipping SPZ compression)")
        return None
    spz = ply.with_suffix(".spz")
    print(f"    compressing to SPZ")
    if dry:
        print(f"    [dry-run] node to_spz.mjs {ply.name} {spz.name}")
        return spz
    proc = subprocess.run(
        [NODE, str(TOOLS / "to_spz.mjs"), str(ply), str(spz)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not spz.is_file():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        print(f"    (SPZ compression skipped: {' '.join(tail)})")
        return None
    saved = 100 - 100 * spz.stat().st_size / ply.stat().st_size
    print(f"    {spz.name}  {spz.stat().st_size / 1e6:.1f} MB "
          f"({saved:.0f}% smaller than the .ply)")
    return spz


# ------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Turn a video or image folder into a 3D Gaussian splat.")
    ap.add_argument("source", help="video file, or a folder of stills")
    ap.add_argument("--name", help="output name (default: source stem)")
    ap.add_argument("--stride", type=int, default=3,
                    help="keep every Nth video frame (default 3)")
    ap.add_argument("--max-frames", type=int, default=None)
    # Trim. Only this span is extracted, so COLMAP and Brush never see the
    # rest: a capture that walks sideways for four seconds and then swings
    # round should be solved on the four seconds, not asked to reconcile both.
    # !! dest is trim_start/trim_end on purpose: --from already stores into
    # args.start (the stage to resume from), and reusing the name silently
    # broke --from on the first run of this.
    ap.add_argument("--start", dest="trim_start", type=float, default=0.0,
                    help="start of the part to use, in seconds (default: the "
                         "beginning)")
    ap.add_argument("--end", dest="trim_end", type=float, default=0.0,
                    help="end of the part to use, in seconds (default: the end "
                         "of the clip)")
    ap.add_argument("--max-width", type=int, default=1600,
                    help="downscale frames wider than this (default 1600)")
    ap.add_argument("--blur-drop", type=float, default=15.0,
                    help="percent of blurriest frames to discard (default 15)")
    ap.add_argument("--matcher", choices=["sequential", "exhaustive"],
                    default="sequential")
    ap.add_argument("--focal-35mm", type=float, default=26.0,
                    help="35 mm-equivalent focal length of the camera, as a "
                         "starting point for COLMAP (default 26, a phone's main "
                         "camera in video mode). 0 lets COLMAP guess -- which "
                         "fails on captures that walk straight ahead.")
    ap.add_argument("--allow-poor", action="store_true",
                    help="train even when too few frames could be placed "
                         "(normally the run stops there, since the scene "
                         "would be unusable)")
    ap.add_argument("--privacy", choices=["auto", "off"], default="auto",
                    help="mask faces before pose solving (default auto). "
                         "'off' leaves frames untouched -- only for captures "
                         "with no people in them.")
    ap.add_argument("--mask-region", action="append", default=[],
                    metavar="X,Y,W,H",
                    help="always blank this region of every frame, as fractions "
                         "of the image; repeatable. For plates and windows.")
    ap.add_argument("--steps", type=int, default=15000,
                    help="Brush training steps (default 15000)")
    ap.add_argument("--max-splats", type=int, default=2_000_000,
                    help="cap on splat count (default 2,000,000). Densification "
                         "is otherwise unbounded: 15k steps on a phone capture "
                         "produced 7.8M splats and a 1.8 GB file. Pass 0 to "
                         "leave it uncapped.")
    ap.add_argument("--sh-degree", type=int, default=3)
    ap.add_argument("--max-resolution", type=int, default=0,
                    help="cap on the longest image edge during training. Default "
                         "0 = what this machine allows (tools/hardware.py): 1920 "
                         "-- a phone frame's own size -- on a strong machine, "
                         "less on a weak one. It was a fixed 1200 until "
                         "2026-09-26, which trained phone video at 40%% of its "
                         "pixels and made scenes soft.")
    ap.add_argument("--quality", choices=["draft", "standard", "high"], default="standard",
                    help="caps the automatic training resolution (draft: 960)")
    ap.add_argument("--compute", choices=["auto", "gpu", "cpu"], default="auto",
                    help="where COLMAP's features and matching run: auto = the "
                         "NVIDIA card when there is one, else the processor; cpu "
                         "forces the processor (the laptop route)")
    ap.add_argument("--threads", type=int, default=0,
                    help="processor threads for COLMAP (default 0 = planned: at "
                         "most 12, fewer when memory is short)")
    ap.add_argument("--subsample-frames", type=int, default=None,
                    help="train on every Nth registered frame, to fit memory")
    ap.add_argument("--train-max-frames", type=int, default=None,
                    help="hard cap on frames loaded for training")
    ap.add_argument("--with-viewer", action="store_true",
                    help="open Brush's own window to watch training")
    ap.add_argument("--work-root", default=None,
                    help="override the work folder root (tests use this to "
                         "stay out of the real one)")
    ap.add_argument("--out-root", default=None,
                    help="override the output folder root")
    ap.add_argument("--from", dest="start", default="frames",
                    choices=["frames", "prune", "poses", "train"],
                    help="resume from a stage, reusing earlier output")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    missing = [n for n, p in
               (("ffmpeg", FFMPEG), ("colmap", COLMAP), ("brush_app", BRUSH))
               if p is None]
    if missing:
        print(f"Missing tools: {', '.join(missing)}.\n"
              f"Expected under {BIN}. See README.", file=sys.stderr)
        return 1

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        print(f"No such source: {source}", file=sys.stderr)
        return 1

    name = args.name or source.stem
    work_root = Path(args.work_root).expanduser().resolve() if args.work_root else WORK
    out_root = Path(args.out_root).expanduser().resolve() if args.out_root else OUTPUT
    work = work_root / name
    images = work / "images"
    out_dir = out_root / name
    order = ["frames", "prune", "poses", "train"]
    start_at = order.index(args.start)

    # THE ROUTE: what this machine has decides where each step runs
    import hardware
    hw = hardware.probe()
    route = hardware.plan(hw, compute=args.compute, quality=args.quality)
    colmap_gpu = route["colmap"]["gpu"]
    colmap_threads = args.threads or route["colmap"]["threads"]
    max_resolution = args.max_resolution or route["train"]["max_resolution"]

    print(f"DL-SplatGenerator capture pipeline")
    print(f"  source {source}")
    print(f"  work   {work}")
    print(f"  output {out_dir}")
    print(f"  this computer: {hardware.summary(hw, route)}")
    for note in route["notes"]:
        print(f"  note: {note}")
    print()
    # Brush needs a graphics adapter of some kind; without one the camera solve
    # alone would not make a scene, so say so BEFORE the minutes are spent.
    if not route["train"]["ok"] and start_at <= 3 and not args.dry_run:
        print(f"FAILED: {route['train']['why']}.", file=sys.stderr)
        return 2

    began = time.time()
    try:
        if start_at <= 0:
            if source.is_dir():
                copy_stills(source, images, dry=args.dry_run)
            elif source.suffix.lower() in VIDEO_SUFFIXES:
                extract_frames(source, images, stride=args.stride,
                               max_frames=args.max_frames,
                               max_width=args.max_width, dry=args.dry_run,
                               start=args.trim_start, end=args.trim_end)
            else:
                raise StageError(f"unsupported source type: {source.suffix}")
        # all three stay None on a resume that starts past the pose stage
        summary = None
        mapper_used = None
        colmap_routes = None
        if start_at <= 1:
            prune_blurry(images, work / "rejected",
                         drop_pct=args.blur_drop, dry=args.dry_run)
        if start_at <= 2:
            privacy = mask_privacy(images, work / "privacy.json",
                                   mode=args.privacy, regions=args.mask_region,
                                   dry=args.dry_run)
            undist, mapper_used, colmap_routes = run_colmap(
                work, matcher=args.matcher,
                focal_35mm=args.focal_35mm, dry=args.dry_run,
                gpu=colmap_gpu, threads=colmap_threads)
        else:
            undist = work / "undistorted"
        if not args.dry_run and start_at <= 2:
            summary = pose_summary(undist, images)
            if summary:
                got, total = summary["registered"], summary["images"]
                print(f"    registered {got} of {total} frames")
                # A reconstruction from a handful of views is geometrically
                # meaningless, but Brush will happily train on it and the run
                # would end in "done" with a black viewport. Stop here instead,
                # and say why -- the half hour is better spent on a re-shoot.
                share = got / total if total else 0
                if got < MIN_USEFUL_VIEWS or share < 0.5:
                    why = (f"only {got} of {total} frames could be placed "
                           f"({share * 100:.0f}%). A scene from so few views is "
                           f"unusable, so the run stops before training one.\n"
                           f"  The mapper already retried with initialisation "
                           f"settings for forward motion, so a camera that "
                           f"walked straight ahead is not the explanation here: "
                           f"the frames are more likely blurred, too far apart, "
                           f"or the scene has too little texture.\n"
                           f"  Try: --stride 2 for more frames; --matcher "
                           f"exhaustive; --focal-35mm if the lens was not the "
                           f"main camera; or film with some sideways motion.")
                    if args.allow_poor:
                        print(f"    WARNING: {why}\n    (--allow-poor: training anyway)")
                    else:
                        raise StageError(why)
        if start_at <= 3:
            run_brush(undist, out_dir, steps=args.steps,
                      max_splats=args.max_splats, sh_degree=args.sh_degree,
                      max_resolution=max_resolution,
                      subsample=args.subsample_frames,
                      max_frames=args.train_max_frames,
                      viewer=args.with_viewer, dry=args.dry_run)
    except StageError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 2

    print(f"\n[5/5] place")
    if not args.dry_run:
        plys = sorted(out_dir.glob("*.ply"), key=lambda p: p.stat().st_mtime)
        if not plys:
            print("    no .ply was produced -- check the Brush output above",
                  file=sys.stderr)
            return 2
        final = plys[-1]

        # The COLMAP model knows where every frame was shot from. Exporting it
        # beside the .ply is what lets the viewer open the scene from a real
        # capture position instead of a synthetic angle.
        try:
            import colmap_cameras
            cams = colmap_cameras.build(undist)
            (out_dir / "cameras.json").write_text(json.dumps(cams, indent=1))
            print(f"    {cams['count']} capture cameras -> cameras.json")
        except Exception as exc:                      # noqa: BLE001
            print(f"    (camera export skipped: {exc})")

        size_mb = final.stat().st_size / 1e6
        # Compress at source, so the viewer opens ~50 MB rather than ~570 MB.
        spz = compress_to_spz(final, dry=args.dry_run)
        served = spz or final

        # what the source says about itself: when, where, with what
        # (tools/source_meta.py). Kept locally; make_sample.py drops the location.
        try:
            import source_meta
            meta = source_meta.read(source)
        except Exception as exc:                      # noqa: BLE001
            meta = {"error": str(exc)}

        manifest = {
            "name": name,
            "source": str(source),
            "source_meta": meta,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            # the source video's own frame rate, so the viewer can walk the
            # capture at the speed it was filmed and the Blender exporter need
            # not probe for it
            "fps": source_fps(source) if source.suffix.lower() in VIDEO_SUFFIXES else None,
            "ply": final.name,
            "spz": spz.name if spz else None,
            # what was masked, so a publish step can refuse an unreviewed capture
            "privacy": (privacy if start_at <= 2 else None) or {
                "masked": False, "reason": "stage skipped or resumed past it"},
            "reconstruction": (dict(summary, mapper=mapper_used)
                               if summary else None),
            "settings": {
                "privacy": args.privacy,
                "stride": args.stride, "max_width": args.max_width,
                "trim": ({"start": args.trim_start, "end": args.trim_end}
                         if (args.trim_start > 0 or args.trim_end > 0) else None),
                "blur_drop": args.blur_drop, "matcher": args.matcher,
                "focal_35mm": args.focal_35mm,
                "steps": args.steps, "max_splats": args.max_splats,
                "sh_degree": args.sh_degree,
                "max_resolution": max_resolution,
                "quality": args.quality,
            },
            # where each step ran on this machine, so a slow or soft scene can
            # be explained afterwards (tools/hardware.py)
            "hardware": {
                "summary": hardware.summary(hw, route),
                "compute": args.compute,
                "colmap": colmap_routes,
                "train_gpu": route["train"]["gpu"],
            },
        }
        (out_dir / "capture.json").write_text(json.dumps(manifest, indent=2))

        # Index every generated scene so the viewer can list them without
        # needing a directory listing or a server endpoint.
        import scene_index
        cams_file = out_dir / "cameras.json"
        scene_index.register(
            out_root, name,
            served=served,                       # the compressed file if there is one
            original=final if spz else None,
            cameras=cams_file if cams_file.is_file() else None,
            created=manifest["created"],
            method="video capture")
        print(f"    {final}  ({size_mb:.1f} MB)")
        print(f"\nDone in {human(time.time() - began)}.")
        print(f"Open it: start the viewer and pick \"{name}\" under "
              f"Generated scenes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
