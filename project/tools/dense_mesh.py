"""Dense mesh from a capture that has already been solved: the measurable half.

    python dense_mesh.py <name>                 # a scene already in work\\
    python dense_mesh.py <name> --quality high

The splat is the scene as it LOOKS. This is the same scene as it MEASURES: the
camera poses the pipeline already solved are re-used to compute a dense depth
map per view, fuse them into one point cloud, and turn that into a surface you
can take into Blender, QGIS or Rhino and draw from.

    video -> COLMAP sparse -+- Brush ------> 3DGS   (appearance)
                            +- THIS -------> mesh   (geometry)

Nothing new is captured and nothing is re-solved. Everything here comes from
COLMAP's own dense pipeline, which the bundled build already carries.

WHAT IT IS HONEST ABOUT
    SCALE. Structure-from-motion fixes geometry only up to a similarity, so the
    output is in COLMAP units, not metres, until something of known size is
    measured -- in the viewer, or with COLMAP's model_aligner. A plan drawn off
    an uncalibrated mesh is the right shape and the wrong size.

    VEGETATION. A camera reconstructs only what it can see. There is no
    photographic equivalent of a laser pulse slipping through foliage to return
    from the ground, so where planting hides the ground the ground is ABSENT --
    not noisy, absent. Measured on a walking clip through scrub: the gravel
    path came out as a clean traceable ribbon, the planting either side as
    fragments. Bare ground, quarries, paving, kerbs, walls and facades are what
    this is for.

WHY ITS OWN WORKSPACE
    The splat workspace puts the sparse model in undistorted\\sparse\\0\\
    because that is where Brush looks; COLMAP's dense tools want
    undistorted\\sparse\\ and fail outright otherwise. And stereo\\ is not
    scratch -- image_undistorter writes patch-match.cfg and fusion.cfg there.
    So this builds a separate dense\\ workspace (about 5 s for 118 frames) and
    leaves the splat workspace, and every Capture Walk package, untouched.
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import capture  # noqa: E402  -- COLMAP path, run(), counter(), human()

ROOT = capture.ROOT
WORK = capture.WORK
OUTPUT = capture.OUTPUT

# max_image_size for the stereo, and whether to run the second
# geometric-consistency pass. Patch match is the whole cost and it grows with
# the square of the image size, so this is the only lever that matters.
# Seconds per view measured on an RTX A4000: 4.4 s at 640 px, photometric only.
QUALITY = {
    "draft":    {"size": 480,  "geometric": False},
    "standard": {"size": 800,  "geometric": False},
    "high":     {"size": 1200, "geometric": True},
}
SECONDS_PER_VIEW_AT_640 = 4.4


def estimate_minutes(views: int, size: int, geometric: bool) -> float:
    per_view = SECONDS_PER_VIEW_AT_640 * (size / 640.0) ** 2 * (2 if geometric else 1)
    return views * per_view / 60.0


def ply_counts(path: Path) -> dict:
    """Vertex and face counts straight from a PLY header."""
    out = {}
    try:
        with path.open("rb") as fh:
            while True:
                line = fh.readline().decode("ascii", "replace").strip()
                if not line or line == "end_header":
                    break
                if line.startswith("element "):
                    _, kind, n = line.split()[:3]
                    out[kind] = int(n)
    except OSError:
        pass
    return out


def cloud_extent(path: Path) -> list | None:
    """Bounding box of a fused cloud, in COLMAP units. None if unreadable."""
    try:
        with path.open("rb") as fh:
            header = b""
            while b"end_header" not in header:
                chunk = fh.readline()
                if not chunk:
                    return None
                header += chunk
            text = header.decode("ascii", "replace")
            count = int([l for l in text.splitlines()
                         if l.startswith("element vertex")][0].split()[-1])
            sizes = {"float": 4, "double": 8, "uchar": 1, "char": 1,
                     "int": 4, "uint": 4, "short": 2, "ushort": 2}
            props = [(l.split()[2], l.split()[1]) for l in text.splitlines()
                     if l.startswith("property ") and len(l.split()) == 3]
            stride = sum(sizes.get(t, 4) for _, t in props)
            names = [n for n, _ in props]
            if not {"x", "y", "z"} <= set(names):
                return None
            offs = [sum(sizes.get(t, 4) for _, t in props[:names.index(a)])
                    for a in ("x", "y", "z")]
            lo = [float("inf")] * 3
            hi = [float("-inf")] * 3
            # every 20th point is plenty for a bounding box
            step = max(1, count // 20000)
            data = fh.read(stride * count)
            for i in range(0, count, step):
                base = i * stride
                for a in range(3):
                    v = struct.unpack_from("<f", data, base + offs[a])[0]
                    lo[a] = min(lo[a], v)
                    hi[a] = max(hi[a], v)
            return [round(hi[a] - lo[a], 3) for a in range(3)]
    except Exception:                                          # noqa: BLE001
        return None


def registered(model: Path) -> int:
    """How many images this sparse model actually holds."""
    try:
        with (model / "images.bin").open("rb") as fh:
            return struct.unpack("<Q", fh.read(8))[0]
    except Exception:                                          # noqa: BLE001
        return 0


def find_model(work: Path) -> Path:
    """The sparse model to undistort from -- the BIGGEST one, not the first.

    !! COLMAP writes sparse/0, sparse/1, ... when a reconstruction FRAGMENTS,
    and the numbering is the order the mapper finished them, NOT their size.
    On the reference clip sparse/0 held 2 images and sparse/1 held 409, so
    taking "sparse/0" meshed two frames and called it the scene -- quietly,
    because the frame count in the API came from the images FOLDER and not
    from the model. Pick by registered image count and say which was chosen.
    """
    if (work / "sparse_best" / "images.bin").is_file():
        return work / "sparse_best"          # capture.py already chose
    models = sorted(
        (d for d in (work / "sparse").glob("*") if (d / "images.bin").is_file()),
        key=registered, reverse=True)
    if models:
        if len(models) > 1:
            print(f"    {len(models)} sparse models here "
                  f"({', '.join(f'{m.name}:{registered(m)}' for m in models)})"
                  f" -- using {models[0].name}, the biggest")
        return models[0]
    if (work / "sparse" / "images.bin").is_file():
        return work / "sparse"
    raise capture.StageError(
        f"no sparse model under {work} -- run a capture first, or point this at "
        f"a work folder that has one")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dense mesh from an already-solved capture")
    ap.add_argument("name", help="scene name (a folder in work\\), or a path")
    ap.add_argument("--quality", choices=list(QUALITY), default="standard")
    ap.add_argument("--max-size", type=int, default=0,
                    help="override the quality preset's stereo image size")
    ap.add_argument("--geometric", action="store_true",
                    help="force the second, slower consistency pass on")
    ap.add_argument("--mesher", choices=["delaunay", "poisson"], default="delaunay",
                    help="delaunay keeps edges; poisson is smoother and closes holes")
    ap.add_argument("--keep-workspace", action="store_true", default=True,
                    help="keep dense\\ so a re-mesh is cheap (default)")
    ap.add_argument("--clean", dest="keep_workspace", action="store_false",
                    help="delete dense\\ afterwards (it is ~0.5 GB)")
    ap.add_argument("--out-root", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if capture.COLMAP is None:
        print("FAILED: COLMAP was not found under bin\\", file=sys.stderr)
        return 2

    work = Path(args.name)
    if not work.is_dir():
        work = WORK / args.name
    if not work.is_dir():
        print(f"FAILED: no work folder {work}", file=sys.stderr)
        return 2
    name = work.name
    out_root = Path(args.out_root).resolve() if args.out_root else OUTPUT
    out_dir = out_root / name / "mesh"
    dense = work / "dense"

    preset = QUALITY[args.quality]
    size = args.max_size or preset["size"]
    geometric = args.geometric or preset["geometric"]

    model = find_model(work)
    images = work / "images"
    n_views = len([p for p in images.iterdir()
                   if p.suffix.lower() in capture.IMAGE_SUFFIXES]) if images.is_dir() else 0

    print("Dense mesh from a solved capture")
    print(f"  work    {work}")
    print(f"  model   {model}")
    print(f"  output  {out_dir}")
    print(f"  quality {args.quality}: stereo at {size} px, "
          f"{'with' if geometric else 'without'} the consistency pass")
    print(f"  {n_views} views -> roughly "
          f"{estimate_minutes(n_views, size, geometric):.0f} min on this machine\n")
    began = time.time()

    # ---- 1: a workspace COLMAP's dense tools actually accept
    print("[1/4] workspace")
    if dense.exists() and not args.dry_run:
        print(f"    clearing the previous dense workspace")
        shutil.rmtree(dense)
    capture.run([capture.COLMAP, "image_undistorter",
                 "--image_path", images,
                 "--input_path", model,
                 "--output_path", dense,
                 "--output_type", "COLMAP",
                 "--max_image_size", str(size)],
                label="COLMAP image_undistorter", dry=args.dry_run,
                parse=capture.counter(r"Undistorting image \[(\d+)/(\d+)\]",
                                      "frames prepared"))

    # ---- 2: a depth and normal map per view. This is the whole cost.
    print("[2/4] stereo   (this is the slow one)")
    # COLMAP announces its per-view work in one of two shapes depending on
    # build; both are watched so the card shows a real counter either way.
    seen = {"n": 0}

    def stereo_progress(line: str):
        import re
        m = re.search(r"Processing view (\d+) / (\d+)", line)
        if m:
            return int(m.group(1)), int(m.group(2)), "views matched"
        if "output for" in line:
            seen["n"] += 1
            total = n_views * (2 if geometric else 1)
            return min(seen["n"], total), total, "views matched"
        return None

    capture.run([capture.COLMAP, "patch_match_stereo",
                 "--workspace_path", dense,
                 "--workspace_format", "COLMAP",
                 "--PatchMatchStereo.geom_consistency",
                 "true" if geometric else "false",
                 "--PatchMatchStereo.filter", "true"],
                label="COLMAP patch_match_stereo", dry=args.dry_run,
                parse=stereo_progress)

    # ---- 3: one cloud out of all those maps
    print("[3/4] fuse")
    cloud = dense / "fused.ply"
    capture.run([capture.COLMAP, "stereo_fusion",
                 "--workspace_path", dense,
                 "--workspace_format", "COLMAP",
                 "--input_type", "geometric" if geometric else "photometric",
                 "--output_path", cloud],
                label="COLMAP stereo_fusion", dry=args.dry_run,
                parse=capture.counter(r"Fusing image \[(\d+)/(\d+)\]", "views fused"))

    # ---- 4: a surface
    print("[4/4] mesh")
    mesh = dense / f"mesh_{args.mesher}.ply"
    if args.mesher == "delaunay":
        capture.run([capture.COLMAP, "delaunay_mesher",
                     "--input_path", dense, "--input_type", "dense",
                     "--output_path", mesh],
                    label="COLMAP delaunay_mesher", dry=args.dry_run)
    else:
        capture.run([capture.COLMAP, "poisson_mesher",
                     "--input_path", cloud, "--output_path", mesh],
                    label="COLMAP poisson_mesher", dry=args.dry_run)

    if args.dry_run:
        return 0
    if not cloud.is_file() or not mesh.is_file():
        raise capture.StageError(
            "the dense stage produced no cloud or no mesh. The usual cause is "
            "too few views seeing each surface -- a walking pass with little "
            "sideways motion gives thin coverage. Try --quality high, or "
            "re-shoot with more overlap.")

    # ---- place, beside the splat but NOT in scenes.json: a mesh is not a
    # splat scene and the viewer's scene list would try to load it as one.
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cloud, out_dir / "dense.ply")
    shutil.copy2(mesh, out_dir / "mesh.ply")

    counts_cloud = ply_counts(out_dir / "dense.ply")
    counts_mesh = ply_counts(out_dir / "mesh.ply")
    extent = cloud_extent(out_dir / "dense.ply")
    seconds = time.time() - began

    record = {
        "name": name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "from": str(model),
        "views": n_views,
        "seconds": round(seconds),
        "settings": {"quality": args.quality, "stereo_size": size,
                     "geometric_consistency": geometric, "mesher": args.mesher},
        "cloud": {"file": "dense.ply", "points": counts_cloud.get("vertex")},
        "mesh": {"file": "mesh.ply", "vertices": counts_mesh.get("vertex"),
                 "faces": counts_mesh.get("face")},
        "extent_units": extent,
        "metric": False,
        "caveats": [
            "COLMAP units, not metres: structure-from-motion fixes geometry "
            "only up to a similarity. Calibrate against something of known "
            "size before drawing anything to scale.",
            "A camera reconstructs only what it sees. Where vegetation hides "
            "the ground, the ground is absent -- the surface there is the top "
            "of the planting, not the terrain.",
        ],
    }
    (out_dir / "mesh.json").write_text(json.dumps(record, indent=2))

    print(f"    dense.ply  {counts_cloud.get('vertex', 0):,} points")
    print(f"    mesh.ply   {counts_mesh.get('vertex', 0):,} vertices, "
          f"{counts_mesh.get('face', 0):,} faces")
    if extent:
        print(f"    extent     {extent[0]} x {extent[1]} x {extent[2]} COLMAP units "
              f"(NOT metres -- calibrate before measuring)")
    if not args.keep_workspace:
        shutil.rmtree(dense, ignore_errors=True)
        print("    dense workspace removed")
    print(f"\nDone in {capture.human(seconds)}.  {out_dir}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except capture.StageError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
