"""Ground points and a terrain model out of the dense cloud.

    python ground_dem.py <name> [--cell 0.05] [--scale 1.0]

The last step of the measuring half. Takes the dense cloud that dense_mesh.py
produced, separates the ground from everything standing on it, rasterises the
ground into a regular grid, and writes a GeoTIFF that DL-TerrainSlicer opens
directly -- so the chain runs video -> poses -> dense cloud -> DTM -> contours
-> laser-cut sheets without leaving the DL tools.

    [1/4] level    turn the cloud so gravity points down
    [2/4] ground   cloth simulation filter
    [3/4] grid     ground points -> a regular raster
    [4/4] write    ground.ply, dem.tif, dem.json

WHICH WAY IS UP
    A reconstruction's axes are arbitrary, and a terrain model is meaningless
    until gravity is known. The capture cameras carry it: COLMAP's camera Y
    points down, so the average of -Y over the frames is the best estimate of
    up a solve offers (measured on a walking clip: 2.3 deg median spread about
    the mean). The cloud is rotated by that before anything else happens. This
    is the same estimate the viewer uses to level the horizon.

GROUND FILTERING, AND WHAT IT CANNOT DO
    The cloth simulation filter of Zhang et al. 2016 ("An Easy-to-Use Airborne
    LiDAR Data Filtering Method Based on Cloth Simulation", Remote Sensing
    8(6):501, doi:10.3390/rs8060501) -- implemented here from the published
    description, not vendored. Invert the cloud, drop a cloth onto it, and what
    the cloth touches is ground.

    !! It can only classify points that EXIST. A camera reconstructs only what
    it sees, and there is no photographic equivalent of a laser pulse returning
    from under foliage. Where planting hides the ground the ground is absent,
    and the cloth settles on the planting instead -- which is exactly where CSF
    is known to fail even on LiDAR. Bare ground, paving, kerbs, quarry faces
    and streets are what this is for. On a vegetated site the raster is the top
    of the vegetation, and calling it a terrain model would be a lie.

SCALE
    The cloud is in COLMAP units unless --scale is given (metres per unit,
    from calibrating against something of known length). The GeoTIFF is written
    either way, and dem.json records which it is. Contours drawn off an
    uncalibrated model have the right shape and the wrong size.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
import warnings
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import capture            # noqa: E402  -- ROOT/OUTPUT/WORK, human()
import colmap_cameras     # noqa: E402  -- the pose maths, already proven

ROOT, OUTPUT, WORK = capture.ROOT, capture.OUTPUT, capture.WORK


# ----------------------------------------------------------------- ply io

def read_ply(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Binary-little-endian PLY -> (xyz float64, rgb uint8 or None)."""
    with path.open("rb") as fh:
        header = b""
        while b"end_header" not in header:
            chunk = fh.readline()
            if not chunk:
                raise ValueError(f"{path} has no PLY header")
            header += chunk
        text = header.decode("ascii", "replace")
        if "binary_little_endian" not in text:
            raise ValueError(f"{path} is not a binary little-endian PLY")
        count = int([l for l in text.splitlines()
                     if l.startswith("element vertex")][0].split()[-1])
        np_of = {"float": "<f4", "float32": "<f4", "double": "<f8",
                 "uchar": "u1", "uint8": "u1", "char": "i1", "int": "<i4",
                 "uint": "<u4", "short": "<i2", "ushort": "<u2"}
        fields = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == "property":
                fields.append((parts[2], np_of[parts[1]]))
        arr = np.frombuffer(fh.read(), dtype=np.dtype(fields), count=count)
    xyz = np.stack([arr["x"], arr["y"], arr["z"]], axis=1).astype(np.float64)
    rgb = None
    if {"red", "green", "blue"} <= {n for n, _ in fields}:
        rgb = np.stack([arr["red"], arr["green"], arr["blue"]], axis=1)
    return xyz, rgb


def write_ply(path: Path, xyz: np.ndarray, rgb: np.ndarray | None) -> None:
    n = len(xyz)
    head = ["ply", "format binary_little_endian 1.0", f"element vertex {n}",
            "property float x", "property float y", "property float z"]
    if rgb is not None:
        head += ["property uchar red", "property uchar green", "property uchar blue"]
    head.append("end_header")
    dt = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if rgb is not None:
        dt += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    rec = np.empty(n, dtype=np.dtype(dt))
    rec["x"], rec["y"], rec["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    if rgb is not None:
        rec["red"], rec["green"], rec["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    with path.open("wb") as fh:
        fh.write(("\n".join(head) + "\n").encode("ascii"))
        fh.write(rec.tobytes())


# -------------------------------------------------------------- geotiff

def write_geotiff(path: Path, grid: np.ndarray, cell: float,
                  origin_xy: tuple[float, float]) -> None:
    """A minimal float32 GeoTIFF: the grid, its cell size and its origin.

    Deliberately small. DL-TerrainSlicer reads elevation with tifffile and takes
    the cell size from ModelPixelScale (33550), the corner from ModelTiepoint
    (33922) and the nodata value from GDAL_NODATA (42113) -- and ignores the CRS
    on purpose, because slicing only needs the grid and the ground units. So no
    GeoKeyDirectory is written: there is no meaningful CRS for a local
    photogrammetric model anyway.
    """
    grid = np.ascontiguousarray(grid, dtype="<f4")
    h, w = grid.shape
    image = grid.tobytes()
    nodata = b"nan\0"

    SHORT, LONG, DOUBLE, ASCII = 3, 4, 12, 2
    # (tag, type, count, payload-or-None) -- ascending tag order is required
    scale = struct.pack("<3d", cell, cell, 0.0)
    tie = struct.pack("<6d", 0.0, 0.0, 0.0, origin_xy[0], origin_xy[1], 0.0)
    entries = [
        (256, LONG, 1, None, w), (257, LONG, 1, None, h),
        (258, SHORT, 1, None, 32), (259, SHORT, 1, None, 1),
        (262, SHORT, 1, None, 1), (273, LONG, 1, None, 0),   # StripOffsets
        (277, SHORT, 1, None, 1), (278, LONG, 1, None, h),
        (279, LONG, 1, None, len(image)), (339, SHORT, 1, None, 3),
        (33550, DOUBLE, 3, scale, 0), (33922, DOUBLE, 6, tie, 0),
        (42113, ASCII, len(nodata), nodata, 0),
    ]
    ifd_offset = 8
    ifd_size = 2 + 12 * len(entries) + 4
    payload_at = ifd_offset + ifd_size
    # TIFF stores a value INLINE when it fits in the 4-byte field, and only
    # then an offset. Writing an offset for a short value (our 4-byte "nan\0"
    # nodata string) makes readers parse the offset bytes as the value --
    # tifffile reported exactly that before this was fixed.
    blobs, offsets, inline = [], {}, {}
    cursor = payload_at
    for tag, _t, _c, payload, _v in entries:
        if payload is None:
            continue
        if len(payload) <= 4:
            inline[tag] = payload.ljust(4, b"\0")
        else:
            offsets[tag] = cursor
            blobs.append(payload)
            cursor += len(payload)
    image_at = cursor
    out = bytearray()
    out += struct.pack("<2sHI", b"II", 42, ifd_offset)
    out += struct.pack("<H", len(entries))
    for tag, typ, count, payload, value in entries:
        if tag == 273:
            value = image_at
        if payload is not None:
            field = inline[tag] if tag in inline else struct.pack("<I", offsets[tag])
        elif typ == SHORT:
            field = struct.pack("<HH", value, 0)
        else:
            field = struct.pack("<I", value)
        out += struct.pack("<HHI", tag, typ, count) + field
    out += struct.pack("<I", 0)
    for b in blobs:
        out += b
    out += image
    path.write_bytes(bytes(out))


# ------------------------------------------------------------- levelling

def up_from_model(sparse: Path, xyz: np.ndarray | None = None) -> dict | None:
    """Which way is up, in the cloud's own frame -- the SAME rule the viewer
    uses (tools/level.py, twin of static/level.js), so a terrain model and the
    scene agree on what is level.

    Until 2026-09-26 this was the mean of the frames' up vectors, which stands
    a ground filmed looking DOWN on its edge (student capture A: 78 deg off).
    COLMAP's camera axes are the rows of R: x right, y DOWN, z forward; the
    centre is -R^T t. No viewer flip here: the fused cloud is in COLMAP's frame.
    Returns level.estimate_up's dict (up, method, ...), or None.
    """
    images = sparse / "images.bin"
    if not images.is_file():
        return None
    rows = []
    for im in colmap_cameras.read_images_bin(images):
        R = np.asarray(colmap_cameras.quat_to_matrix(*im["q"]), dtype=np.float64)
        rows.append((im["name"], -R.T @ np.asarray(im["t"], dtype=np.float64), R[2], -R[1]))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])                    # filming order
    pos, dirs, ups = (np.asarray([r[i] for r in rows]) for i in (1, 2, 3))
    if xyz is None or len(xyz) < 50:
        v = ups.mean(axis=0)
        return {"up": v / np.linalg.norm(v), "method": "mean up"}
    step = max(1, len(xyz) // 20000)
    import level
    return level.estimate_up(pos, dirs, ups, xyz[::step])


def path_from_model(sparse: Path) -> np.ndarray | None:
    """The walk line: camera centres in the cloud's own frame, in frame order.

    COLMAP stores world-to-camera, so the centre is -R^T t. images.bin is in
    whatever order the mapper registered them, which is not the order they were
    filmed in -- sort by file name, which capture.py numbers sequentially.
    """
    images = sparse / "images.bin"
    if not images.is_file():
        return None
    rows = []
    for im in colmap_cameras.read_images_bin(images):
        R = np.asarray(colmap_cameras.quat_to_matrix(*im["q"]), dtype=np.float64)
        rows.append((im["name"], -R.T @ np.asarray(im["t"], dtype=np.float64)))
    if len(rows) < 2:
        return None
    rows.sort(key=lambda r: r[0])
    return np.asarray([c for _, c in rows], dtype=np.float64)


def rotation_to_z(up: np.ndarray) -> np.ndarray:
    """Rotation taking `up` onto +Z (Rodrigues, with the antiparallel case)."""
    a = up / np.linalg.norm(up)
    b = np.array([0.0, 0.0, 1.0])
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-12:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1.0 / (1.0 + c))


# -------------------------------------------------------------- the corridor

def to_polyline(xy: np.ndarray, line: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Each point in the walk line's own frame: distance, along, and across.

    Plan distance only: a corridor is a strip on the ground, so height plays no
    part in whether a point is inside it. Segment by segment with a running
    minimum, rather than points x segments at once, which would be a few
    hundred megabytes for nothing.

    `across` is the SIGNED offset -- left of the direction of travel is
    positive -- which is what lets an unrolled corridor be laid out flat.
    """
    seg = line[1:] - line[:-1]
    length = np.hypot(seg[:, 0], seg[:, 1])
    start = np.concatenate(([0.0], np.cumsum(length)))
    dist = np.full(len(xy), np.inf)
    along = np.zeros(len(xy))
    across = np.zeros(len(xy))
    for i in range(len(seg)):
        d = seg[i]
        l2 = float(d[0] * d[0] + d[1] * d[1])
        if l2 < 1e-12:
            continue
        rel = xy - line[i, :2]
        t = np.clip((rel[:, 0] * d[0] + rel[:, 1] * d[1]) / l2, 0.0, 1.0)
        dx = rel[:, 0] - t * d[0]
        dy = rel[:, 1] - t * d[1]
        this = np.hypot(dx, dy)
        closer = this < dist
        dist[closer] = this[closer]
        along[closer] = start[i] + t[closer] * length[i]
        # which side: the z of the 2D cross product of the segment and the
        # point, so the strip can be laid out with left and right kept apart
        side = np.sign(d[0] * rel[:, 1] - d[1] * rel[:, 0])
        across[closer] = side[closer] * this[closer]
    return dist, along, across


def profile_along(along: np.ndarray, z: np.ndarray, bands: float) -> list[dict]:
    """Mean ground height in bands along the walk -- the corridor's sanity check.

    A DEM's full height range is dominated by the far field. What a plan of a
    path actually needs to be right is the fall ALONG it, so state that here
    rather than leaving it to be rediscovered.
    """
    if not len(along):
        return []
    edges = np.arange(0.0, float(along.max()) + bands, bands)
    out = []
    for i in range(len(edges) - 1):
        sel = (along >= edges[i]) & (along < edges[i + 1])
        if sel.sum() < 10:
            continue
        out.append({"from": round(float(edges[i]), 2),
                    "to": round(float(edges[i + 1]), 2),
                    "mean_z": round(float(z[sel].mean()), 3),
                    "points": int(sel.sum())})
    return out


# --------------------------------------------------------------- the filter

def classify_ground(xyz: np.ndarray, cloth_res: float, threshold: float,
                    rigidness: int, iterations: int) -> tuple[np.ndarray, dict]:
    """Cloth simulation filter. Returns a boolean ground mask.

    Zhang et al. 2016. Invert the cloud so the ground becomes the top surface,
    drop a cloth onto it from above, and keep the points the cloth rests on.
    """
    x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    zi = -z                                   # inverted: ground is now highest
    x0, y0 = x.min(), y.min()
    nx = int(np.ceil((x.max() - x0) / cloth_res)) + 2
    ny = int(np.ceil((y.max() - y0) / cloth_res)) + 2
    ix = np.clip(((x - x0) / cloth_res).astype(np.int32), 0, nx - 1)
    iy = np.clip(((y - y0) / cloth_res).astype(np.int32), 0, ny - 1)

    # the highest inverted point in each cell = the lowest real point there
    floor = np.full((ny, nx), -np.inf)
    np.maximum.at(floor, (iy, ix), zi)
    empty = ~np.isfinite(floor)
    floor[empty] = zi.min()                   # nothing seen: let the cloth pass

    cloth = np.full((ny, nx), zi.max() + cloth_res)
    step = max((zi.max() - zi.min()) / 200.0, 1e-6)
    for _ in range(iterations):
        cloth = cloth - step                                  # gravity
        np.maximum(cloth, floor, out=cloth)                   # collision
        for _ in range(max(1, rigidness)):                    # internal rigidity
            s = cloth.copy()
            s[1:-1, 1:-1] = 0.25 * (cloth[:-2, 1:-1] + cloth[2:, 1:-1]
                                    + cloth[1:-1, :-2] + cloth[1:-1, 2:])
            np.maximum(s, floor, out=s)
            cloth = s

    dist = zi - cloth[iy, ix]
    ground = np.abs(dist) <= threshold
    return ground, {"cloth_grid": [int(ny), int(nx)],
                    "cells_unseen": int(empty.sum()),
                    "iterations": iterations}


# ------------------------------------------------------------------ raster

def cell_for_density(xyz: np.ndarray, start: float, min_points: int,
                     keep: float = 0.75, rounds: int = 6) -> tuple[float, dict]:
    """Choose the raster cell from the POINT DENSITY, not from the extent.

    The extent rule -- 1/300 of the longer side -- says how finely you would
    LIKE to draw the ground. It says nothing about whether the cloud can pay
    for it, and the two came apart the moment a corridor could be cut along
    one stretch of a walk: a shorter stretch got a FINER cell while its point
    density was unchanged, so most cells fell under `min_points` and were
    dropped. On the reference clip that was 64% nodata for a 5.7 m stretch
    against 46% for the whole 12.4 m one -- the smaller the piece of ground,
    the worse the drawing of it, which is backwards.

    So the extent rule becomes the FINEST cell we will consider, and this
    coarsens from there until the cloud can actually fill it. The criterion is
    stated rather than assumed: at least `keep` of the cells that hold any
    ground at all must reach `min_points`. It is measured from the real
    distribution each round, because ground points are not spread evenly --
    dense underfoot, thin and grazing further out.

    Never finer than `start`, and each round grows the cell by at most 2x so a
    single sparse quantile cannot overshoot into a coarse raster.
    """
    cell = float(start)
    history = []
    for _ in range(max(1, rounds)):
        x, y = xyz[:, 0], xyz[:, 1]
        ix = ((x - x.min()) / cell).astype(np.int64)
        iy = ((y - y.min()) / cell).astype(np.int64)
        counts = np.unique(ix * (iy.max() + 1) + iy, return_counts=True)[1]
        floor = float(np.quantile(counts, 1.0 - keep))
        history.append({"cell": round(cell, 4), "quantile_count": round(floor, 1),
                        "occupied_cells": int(counts.size)})
        if floor >= min_points:
            break
        cell *= min(2.0, math.sqrt(min_points / max(floor, 0.5)))
    return round(cell, 4), {"from_extent": round(float(start), 4),
                            "keep_fraction": keep, "rounds": history}


def rasterise(xyz: np.ndarray, cell: float, min_points: int = 2) -> tuple[np.ndarray, tuple]:
    """Ground points -> a grid of mean height, NaN where nothing was seen.

    Row 0 is the NORTH edge, which is what a GeoTIFF and DL-TerrainSlicer both
    expect; y therefore runs downwards through the array.

    `min_points` is the honesty control. Far from the walk a cell can rest on a
    single stray point, and one point is not a measurement -- on the reference
    clip those thin cells stretched the raster's range from about 1.2 m of real
    relief along the path to 12 m. Cells under the threshold are left as nodata
    rather than quietly inventing a height.
    """
    x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    x0, y1 = x.min(), y.max()
    nx = int(np.ceil((x.max() - x0) / cell)) + 1
    ny = int(np.ceil((y1 - y.min()) / cell)) + 1
    ix = np.clip(((x - x0) / cell).astype(np.int32), 0, nx - 1)
    iy = np.clip(((y1 - y) / cell).astype(np.int32), 0, ny - 1)
    total = np.zeros((ny, nx))
    count = np.zeros((ny, nx))
    np.add.at(total, (iy, ix), z)
    np.add.at(count, (iy, ix), 1)
    grid = np.where(count >= max(1, min_points), total / np.maximum(count, 1), np.nan)
    thin = int(((count > 0) & (count < max(1, min_points))).sum())
    return grid, (float(x0), float(y1), int(nx), int(ny), thin)


def fill_holes(grid: np.ndarray, passes: int) -> tuple[np.ndarray, int]:
    """Close small gaps by averaging the neighbours that do exist.

    Only small ones: a hole the size of a shed is missing DATA, and inventing a
    surface across it would be the kind of quiet lie this tool avoids.
    """
    out = grid.copy()
    before = int(np.isnan(out).sum())
    for _ in range(passes):
        holes = np.isnan(out)
        if not holes.any():
            break
        padded = np.pad(out, 1, constant_values=np.nan)
        stack = np.stack([padded[:-2, 1:-1], padded[2:, 1:-1],
                          padded[1:-1, :-2], padded[1:-1, 2:]])
        # a cell with no filled neighbour at all is the common case here, and
        # nanmean warns about it every time; the result (NaN) is what we want
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            neigh = np.nanmean(stack, axis=0)
        out = np.where(holes & np.isfinite(neigh), neigh, out)
    return out, before - int(np.isnan(out).sum())


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Ground filter and DEM from a dense cloud")
    ap.add_argument("name", help="scene name, or a path to a dense .ply")
    ap.add_argument("--cell", type=float, default=0.0,
                    help="DEM cell size in cloud units (default: 1/300 of the extent)")
    ap.add_argument("--cloth-res", type=float, default=0.0,
                    help="cloth spacing (default: 3 x cell)")
    ap.add_argument("--threshold", type=float, default=0.0,
                    help="ground band around the cloth (default: 1 x cloth spacing)")
    ap.add_argument("--rigidness", type=int, default=3)
    ap.add_argument("--iterations", type=int, default=300)
    ap.add_argument("--fill", type=int, default=4,
                    help="hole-filling passes; 0 leaves every gap as nodata")
    # 0 means "not given", and the two modes want different answers: see below.
    ap.add_argument("--min-points", type=int, default=0,
                    help="ground points a cell needs before it counts "
                         "(default 2 for a whole scene, 8 for a corridor; "
                         "1 keeps every cell, including cells built on a "
                         "single stray point)")
    # 0 means "not given". A calibration of exactly 1.000 m per unit is a real
    # answer -- IMG_1779's is -- so it must not be mistaken for the default.
    ap.add_argument("--scale", type=float, default=0.0,
                    help="metres per cloud unit, if the scene has been calibrated "
                         "(omit, or 0, when it has not)")
    ap.add_argument("--corridor", type=float, default=0.0,
                    help="keep only what lies within this distance of the walk "
                         "line, in output units (metres once --scale is given). "
                         "0 = the whole scene. A DEM's height range is mostly "
                         "far field seen at a shallow angle; a corridor is the "
                         "part a plan of the path is actually about.")
    ap.add_argument("--frames", default="",
                    help="FROM:TO, 1-based and inclusive, to take a corridor "
                         "along one STRETCH of the walk instead of all of it "
                         "(the numbers the viewer's frame scrubber shows)")
    ap.add_argument("--unroll", action="store_true",
                    help="lay the corridor out FLAT: grid it along the walk "
                         "rather than across the world, so a path that bends "
                         "comes out as a straight strip. Needs --corridor.")
    ap.add_argument("--band", type=float, default=0.0,
                    help="along-path profile band (default: 1/12 of the walk)")
    ap.add_argument("--out-root", default=None)
    args = ap.parse_args()

    began = time.time()
    calibrated = args.scale > 0        # 0 = not given; 1.0 is a real answer
    factor = args.scale if calibrated else 1.0
    src = Path(args.name)
    name = src.stem
    if not src.is_file():
        name = args.name
        src = (Path(args.out_root).resolve() if args.out_root else OUTPUT) / name / "mesh" / "dense.ply"
    if not src.is_file():
        print(f"FAILED: no dense cloud at {src} -- build the mesh first",
              file=sys.stderr)
        return 2
    out_dir = src.parent

    print("Ground filter and terrain model")
    print(f"  cloud   {src}")
    print(f"  output  {out_dir}")
    if calibrated:
        print(f"  scale   {factor:g} m per cloud unit")
    else:
        print("  scale   NOT CALIBRATED -- output is in COLMAP units, not metres")
    print()

    # ---- 1: which way is up
    print("[1/4] level")
    xyz, rgb = read_ply(src)
    print(f"    {len(xyz):,} points")
    sparse = WORK / name / "dense" / "sparse"
    est = up_from_model(sparse, xyz)
    level_record = None
    up = None
    if est is None:
        print("    WARNING: no camera model beside this cloud; assuming +Z is "
              "already up. A terrain model from a tilted cloud is wrong.")
        R = np.eye(3)
    else:
        import level
        up = est["up"]
        R = rotation_to_z(up)
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, float(up[2])))))
        print(f"    up by '{est['method']}': {np.round(up, 4).tolist()} "
              f"({tilt:.1f} deg off the cloud's own +Z)")
        level_record = {"method": est["method"], "up": [round(float(x), 6) for x in up]}
        if est.get("look_down") is not None:
            level_record["cameras_look_down_deg"] = round(est["look_down"], 1)
            print(f"    the cameras looked {est['look_down']:.0f} deg down onto the "
                  f"dominant plane ({est.get('share', 0):.0%} of the points)")
        if est.get("mean_up") is not None:
            off = level.angle_deg(up, est["mean_up"])
            level_record["vs_mean_up_deg"] = round(off, 2)
            print(f"    {off:.1f} deg from the cameras' mean up (the rule before 2026-09-26)")
        if est["method"] == "held level, walk straight" and est.get("walk_level") is not None:
            # the one ambiguity: say how big it is, so a grade is read with it
            alt = level.angle_deg(up, est["walk_level"])
            level_record["walk_horizontal_alternative_deg"] = round(alt, 2)
            level_record["caveat"] = (
                "The walk never turned, so along it the vertical rests on the phone "
                "being held level on average: a grade along the path is uncertain by "
                "the camera's average pitch. Assuming instead that the walk is "
                f"horizontal would tilt the model {alt:.1f} deg.")
            print(f"    NOTE: the walk never turned -- along it, 'level' assumes the "
                  f"phone was held level on average; assuming a horizontal walk "
                  f"instead would differ by {alt:.1f} deg")
    level = xyz @ R.T
    if calibrated:
        level = level * factor

    # ---- 1b: the corridor, if one was asked for
    corridor = None
    along = None
    across = None
    walk = None
    unrolled = bool(args.unroll and args.corridor > 0)
    if args.unroll and args.corridor <= 0:
        print("FAILED: --unroll lays out a CORRIDOR flat, so it needs --corridor",
              file=sys.stderr)
        return 8
    if args.corridor > 0:
        centres = path_from_model(sparse)
        if centres is None:
            print(f"FAILED: --corridor needs the camera path, and there is no "
                  f"usable images.bin at {sparse}", file=sys.stderr)
            return 4
        total_frames = len(centres)
        first, last = 1, total_frames
        if args.frames:
            # 1-based and inclusive, because those are the numbers the viewer's
            # frame scrubber puts in front of you. A stretch of the walk gives a
            # plan of one part of a site instead of all of it.
            try:
                a, b = (int(v) for v in args.frames.split(":"))
            except ValueError:
                print(f"FAILED: --frames wants FROM:TO, got {args.frames!r}",
                      file=sys.stderr)
                return 6
            first, last = max(1, min(a, b)), min(total_frames, max(a, b))
            if last - first < 1:
                print(f"FAILED: frames {args.frames} is under two frames of "
                      f"{total_frames} -- a walk line needs a segment",
                      file=sys.stderr)
                return 7
            centres = centres[first - 1:last]
            print(f"    stretch: frames {first}-{last} of {total_frames}")
        walk = centres @ R.T
        if calibrated:
            walk = walk * factor
        dist, along_all, across_all = to_polyline(level[:, :2], walk)
        keep = dist <= args.corridor
        walk_len = float(np.hypot(*(walk[1:, :2] - walk[:-1, :2]).T).sum())
        print(f"    walk line {len(walk)} frames, {walk_len:.2f} {'m' if calibrated else 'units'} long")
        if keep.sum() < 100:
            print(f"FAILED: only {int(keep.sum())} points lie within "
                  f"{args.corridor:g} of the walk line -- widen --corridor",
                  file=sys.stderr)
            return 5
        print(f"    corridor +/-{args.corridor:g}: kept {int(keep.sum()):,} of "
              f"{len(level):,} points ({100.0 * keep.sum() / len(level):.0f}%)")
        level = level[keep]
        if rgb is not None:
            rgb = rgb[keep]
        along = along_all[keep]
        across = across_all[keep]
        corridor = {"half_width": args.corridor, "frames": int(len(walk)),
                    "frames_from": first, "frames_to": last,
                    "frames_total": int(total_frames),
                    "whole_walk": first == 1 and last == total_frames,
                    "walk_length": round(walk_len, 3),
                    "points_kept": int(keep.sum()),
                    "points_dropped": int((~keep).sum())}

    extent = level.max(axis=0) - level.min(axis=0)
    cell = args.cell or round(float(max(extent[0], extent[1])) / 300.0, 4)
    cloth_res = args.cloth_res or cell * 3.0
    threshold = args.threshold or cloth_res
    unit = "m" if calibrated else "units"
    print(f"    extent {extent[0]:.2f} x {extent[1]:.2f} x {extent[2]:.2f} {unit}")
    print(f"    cell {cell:g} {unit}, cloth {cloth_res:g} {unit}, "
          f"ground band +/-{threshold:g} {unit}")

    # ---- 2: ground
    print("[2/4] ground")
    ground, stats = classify_ground(level, cloth_res, threshold,
                                    args.rigidness, args.iterations)
    n_ground = int(ground.sum())
    share = 100.0 * n_ground / max(1, len(level))
    print(f"    {n_ground:,} of {len(level):,} points classified as ground "
          f"({share:.0f}%)")
    if share < 5:
        print("    WARNING: almost nothing was classified as ground. Either the "
              "capture never saw it, or the cloth is too stiff -- try a larger "
              "--cloth-res or --threshold.")
    if n_ground == 0:
        print("FAILED: no ground points to grid", file=sys.stderr)
        return 3

    # ---- 3: grid
    # A corridor asks more of a cell than a whole scene does. Cropped to the
    # walk, the survivors of a 2-point rule are no longer distant terrain but
    # stray misclassification inside the strip -- on the reference clip, 17
    # cells of ~11,000 held the raster open to 2.46 m where 98% of the ground
    # lay within 0.75 m, which is 25 contour levels instead of about 8. Eight
    # points clears them (Marc, 2026-09-08). A whole scene keeps 2: out there
    # the extremes are real ground seen edge-on, not noise.
    min_points = args.min_points or (8 if corridor else 2)
    print("[3/4] grid")

    # UNROLLED: grid the corridor in the WALK's frame rather than the world's.
    # x becomes distance along the path and y the offset across it, so a route
    # that bends comes out as a straight strip and the raster stops being
    # mostly empty -- on a walk that turned, 42% of the cells held ground.
    #
    # !! This is a DEVELOPED strip, not a plan. Lengths along the path and
    # offsets across it are true; everything else is deformed, exactly as a
    # map projection deforms. On a bend the inside is compressed and the
    # outside stretched, and a straight line across the curve is not straight
    # here. Never measure an area or a diagonal off this raster.
    surface = level[ground]
    if unrolled:
        surface = np.column_stack([along[ground], across[ground], surface[:, 2]])
        print(f"    unrolled: gridding {surface[:, 0].max():.2f} {unit} along the "
              f"walk by +/-{args.corridor:g} across it")
    # The cloth was dropped at the extent's scale; the RASTER is drawn at the
    # scale the ground points can actually pay for. Only when --cell is silent.
    density = None
    grid_cell = cell
    if not args.cell:
        grid_cell, density = cell_for_density(surface, cell, min_points)
        if grid_cell > cell:
            first = density["rounds"][0]
            # NOT `share` -- that name already holds the ground percentage from
            # stage 2, and reusing it wrote 75 into the record's ground share.
            target = int(100 * density["keep_fraction"])
            print(f"    cell {grid_cell:g} {unit} from point density -- the "
                  f"extent alone would say {cell:g}, where the thinnest "
                  f"quarter of the cells holding ground carried only "
                  f"{first['quantile_count']:g} points, not {min_points}")
            print(f"    (coarsened until {target}% of them reach the threshold, "
                  f"in {len(density['rounds'])} step(s))")
    grid, (x0, y1, nx, ny, thin) = rasterise(surface, grid_cell, min_points)
    holes_before = int(np.isnan(grid).sum())
    grid, filled = fill_holes(grid, args.fill)
    holes_after = int(np.isnan(grid).sum())
    print(f"    {nx} x {ny} cells at {grid_cell:g} {unit}")
    if thin:
        print(f"    {thin:,} cells dropped for having under {min_points} "
              f"ground points -- too thin to be a measurement")
    print(f"    {holes_before:,} empty cells, {filled:,} filled, "
          f"{holes_after:,} left as nodata")

    # ---- 4: write
    # A corridor model answers a different question from the whole-scene one,
    # so it is written beside it rather than over it.
    print("[4/4] write")
    tag = "_unrolled" if unrolled else ("_corridor" if corridor else "")
    ply_name, tif_name, json_name = f"ground{tag}.ply", f"dem{tag}.tif", f"dem{tag}.json"
    # the .ply stays in the WORLD's frame whatever the raster does: a point
    # cloud laid out flat would be a lie about where anything is
    write_ply(out_dir / ply_name, level[ground],
              rgb[ground] if rgb is not None else None)
    write_geotiff(out_dir / tif_name, grid, grid_cell,
                  (x0, y1 - (ny - 1) * grid_cell))
    if corridor:
        band = args.band or max(round(corridor["walk_length"] / 12.0, 2), grid_cell)
        corridor["profile_band"] = band
        corridor["profile"] = profile_along(along[ground], level[ground][:, 2], band)
        # The grade comes from a least-squares fit over every ground point, not
        # from the first and last bands: an end band can rest on a handful of
        # points and would swing the answer on its own.
        a, z = along[ground], level[ground][:, 2]
        if len(a) > 2 and float(a.max() - a.min()) > 0:
            slope, intercept = np.polyfit(a, z, 1)
            run = float(a.max() - a.min())
            corridor["fall_along_path"] = round(float(-slope * run), 3)
            corridor["grade_percent"] = round(float(-slope) * 100.0, 1)
            corridor["grade_method"] = "least squares over the corridor ground points"
            print(f"    along the path: falls {-slope * run:.2f} over {run:.1f} "
                  f"({corridor['grade_percent']}% grade)")
    finite = grid[np.isfinite(grid)]
    record = {
        "name": name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": str(src),
        "points": int(len(xyz)),
        # what the filter actually saw: the whole cloud, or the corridor of it
        "points_considered": int(len(level)),
        "ground_points": n_ground,
        "ground_share_percent": round(share, 1),
        "up_used": None if up is None else [round(float(v), 5) for v in up],
        # how "up" was found (tools/level.py, the viewer's rule)
        "level": level_record,
        "corridor": corridor,
        "unrolled": unrolled,
        "raster_frame": ("along the walk (x) by offset across it (y) -- a "
                         "DEVELOPED strip, not a plan") if unrolled
                        else "world x/y, levelled",
        "grid": {"file": tif_name, "width": nx, "height": ny,
                 "cell_size": grid_cell, "nodata": "nan",
                 "z_min": round(float(finite.min()), 4) if finite.size else None,
                 "z_max": round(float(finite.max()), 4) if finite.size else None,
                 "empty_cells": holes_after, "thin_cells_dropped": thin,
                 "min_points_per_cell": min_points,
                 "cell_from": "point density" if density else "given",
                 "cell_choice": density},
        "cloth": {"resolution": cloth_res, "threshold": threshold,
                  "rigidness": args.rigidness, **stats},
        "units": "metres" if calibrated else "COLMAP units",
        "scale_m_per_unit": factor if calibrated else None,
        "metric": calibrated,
        "method": "cloth simulation filter (Zhang et al. 2016, "
                  "doi:10.3390/rs8060501), implemented here from the paper",
        "caveats": [
            "A ground filter can only classify points that exist. Where "
            "vegetation hid the ground, this raster is the top of the "
            "vegetation, not the terrain.",
            "Uncalibrated output is in COLMAP units: the right shape and the "
            "wrong size. Pass --scale once something of known length has been "
            "measured.",
        ],
    }
    if level_record and level_record.get("caveat"):
        record["caveats"].append(level_record["caveat"])
    if level_record and level_record.get("method") == "ground":
        record["caveats"].append(
            "Levelled to the GROUND: the capture looked down at it, so the "
            "dominant plane of the scan was taken as level. A real slope in that "
            "plane is flattened -- read no grade off this model.")
    if corridor:
        record["caveats"].append(
            f"Cropped to {corridor['half_width']:g} either side of the walk "
            f"line. Everything outside it is gone from this raster -- it "
            f"describes the strip that was walked, not the site.")
    if unrolled:
        record["caveats"].append(
            "UNROLLED: this raster is gridded along the walk, not across the "
            "world. Distances ALONG the path and offsets ACROSS it are true; "
            "everything else is deformed the way a map projection deforms. On "
            "a bend the inside is compressed and the outside stretched, and a "
            "straight line across the curve is not straight here. Do not "
            "measure areas or diagonals off it, and do not treat its corner "
            "coordinates as a position on the ground.")
    (out_dir / json_name).write_text(json.dumps(record, indent=2))
    print(f"    {ply_name}  {n_ground:,} points")
    print(f"    {tif_name}     {nx} x {ny}, {grid_cell:g} {unit} cells")
    print(f"    {json_name}")
    print(f"\nDone in {capture.human(time.time() - began)}.")
    if unrolled:
        print("    NOTE: a developed strip, not a plan -- true along and across "
              "the path, deformed everywhere else. Do not measure areas off it.")
    print(f"Open {tif_name} in DL-TerrainSlicer for contours and laser-cut sheets."
          + ("" if calibrated else
             "  NOTE: uncalibrated, so those contours are unitless."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
