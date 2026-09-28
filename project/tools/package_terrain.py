"""The point cloud and the terrain of a scene, in site coordinates, for the package.

    python tools/package_terrain.py <scene name> [--interval 0]

For the student package (pipeline\\requests\\004):

    <name> - site data\\point cloud\\<name>_points.ply     the dense cloud (colour, normals)
    <name> - site data\\terrain\\<name>_ground.ply         the points classified as ground
    (the GeoTIFFs: tools/package_dtm.py -- the ground and surface models, uniform cell;
     until 2026-09-27 this tool wrote <name>_terrain.tif, with its tie point on the wrong row)
    <name> - site data\\terrain\\<name>_contours.dxf       contour lines, 3D, at their height

All in site.json's frame (tools/site_frame.py: Z up, Y north or the walk, origin
on the ground below the first camera, metres when scaled), so they line up with
the mesh, the splats, the cameras and the .blend. The report stays with the
scene's working files (working\\mesh\\terrain.json).

THE GROUND comes from the same, tested parts as tools/ground_dem.py -- the cloth
simulation filter, the density-driven cell, the thin-cell rule, the small-hole
fill -- run on the cloud in site coordinates instead of ground_dem's own
levelled frame, so that the terrain lines up with everything else in the package.
The caveat is the same: a camera sees surfaces, not the ground under planting;
where vegetation hides the ground, the "ground" is the top of the vegetation.

CONTOURS are traced here (marching squares on the grid's cell centres, the
saddle cells decided by the cell's mean, segments joined into polylines) and
written as a plain DXF R12 -- no extra library -- with a minor and a major layer
(every 5th). The interval is chosen so that about 10-40 lines cover the ground's
height range, from a list of round numbers; units as the package (metres only
when scaled -- otherwise the DXF says unitless and the interval is in scan units).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import ground_dem       # noqa: E402  -- read_ply, write_ply, classify_ground, cell_for_density, rasterise, fill_holes, write_geotiff
import scene_paths      # noqa: E402

NICE = [0.001, 0.002, 0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5,
        1, 2, 2.5, 5, 10, 20, 25, 50, 100]


# ------------------------------------------------------------------ helpers

def read_cloud(path: Path):
    """x y z (+ nx ny nz) (+ red green blue) from a binary little-endian PLY."""
    with path.open("rb") as fh:
        head = b""
        while b"end_header" not in head:
            head += fh.readline()
        text = head.decode("ascii")
        n = int(text.split("element vertex ")[1].split()[0])
        np_of = {"float": "<f4", "double": "<f8", "uchar": "u1"}
        fields = [(l.split()[2], np_of[l.split()[1]]) for l in text.splitlines()
                  if l.startswith("property ")]
        a = np.frombuffer(fh.read(), dtype=np.dtype(fields), count=n)
    xyz = np.stack([a["x"], a["y"], a["z"]], 1).astype(np.float64)
    nrm = np.stack([a["nx"], a["ny"], a["nz"]], 1).astype(np.float64) if "nx" in a.dtype.names else None
    rgb = np.stack([a["red"], a["green"], a["blue"]], 1) if "red" in a.dtype.names else None
    return xyz, nrm, rgb


def write_cloud(path: Path, xyz, nrm, rgb, comment: str) -> None:
    fields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if nrm is not None:
        fields += [("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")]
    if rgb is not None:
        fields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    out = np.empty(len(xyz), dtype=np.dtype(fields))
    out["x"], out["y"], out["z"] = xyz.T
    if nrm is not None:
        out["nx"], out["ny"], out["nz"] = nrm.T
    if rgb is not None:
        out["red"], out["green"], out["blue"] = rgb.T
    types = {"<f4": "float", "u1": "uchar"}
    head = ["ply", "format binary_little_endian 1.0", f"comment {comment}",
            f"element vertex {len(xyz)}"] + [f"property {types[t]} {n}" for n, t in fields] + ["end_header"]
    with path.open("wb") as fh:
        fh.write(("\n".join(head) + "\n").encode("ascii"))
        fh.write(out.tobytes())


def choose_interval(z: np.ndarray, given: float) -> float:
    if given > 0:
        return given
    lo, hi = np.percentile(z, [2, 98])
    span = max(float(hi - lo), 1e-9)
    for step in NICE:
        if span / step <= 40:
            return step
    return NICE[-1]


# ------------------------------------------------------------------ contours

# corners: top-left 1, top-right 2, bottom-right 4, bottom-left 8 (bit set = above the level)
# edges:   T top, R right, B bottom, L left
CASES = {1: [("L", "T")], 2: [("T", "R")], 3: [("L", "R")], 4: [("R", "B")],
         6: [("T", "B")], 7: [("L", "B")], 8: [("B", "L")], 9: [("T", "B")],
         11: [("R", "B")], 12: [("L", "R")], 13: [("T", "R")], 14: [("L", "T")]}
# saddles: which pairs join depends on whether the cell's centre is above
SADDLE = {5: {True: [("T", "R"), ("B", "L")], False: [("L", "T"), ("R", "B")]},
          10: {True: [("L", "T"), ("R", "B")], False: [("T", "R"), ("B", "L")]}}


def trace(grid: np.ndarray, level: float, xs: np.ndarray, ys: np.ndarray) -> list[np.ndarray]:
    """Polylines (N x 2, site x/y) where the grid crosses `level`.
    grid[i, j] sits at (xs[j], ys[i]); NaN cells are skipped."""
    tl, tr = grid[:-1, :-1], grid[:-1, 1:]
    bl, br = grid[1:, :-1], grid[1:, 1:]
    ok = np.isfinite(tl) & np.isfinite(tr) & np.isfinite(bl) & np.isfinite(br)
    case = ((tl > level) * 1 + (tr > level) * 2 + (br > level) * 4 + (bl > level) * 8)
    case = np.where(ok, case, 0)
    ny, nx = grid.shape

    def point(edge, i, j):
        """The crossing on an edge of cell (i, j), and a key naming that edge."""
        if edge == "T":
            a, b, pa, pb, key = tl[i, j], tr[i, j], (xs[j], ys[i]), (xs[j + 1], ys[i]), ("H", i, j)
        elif edge == "B":
            a, b, pa, pb, key = bl[i, j], br[i, j], (xs[j], ys[i + 1]), (xs[j + 1], ys[i + 1]), ("H", i + 1, j)
        elif edge == "L":
            a, b, pa, pb, key = tl[i, j], bl[i, j], (xs[j], ys[i]), (xs[j], ys[i + 1]), ("V", i, j)
        else:
            a, b, pa, pb, key = tr[i, j], br[i, j], (xs[j + 1], ys[i]), (xs[j + 1], ys[i + 1]), ("V", i, j + 1)
        t = (level - a) / (b - a) if b != a else 0.5
        return key, (pa[0] + t * (pb[0] - pa[0]), pa[1] + t * (pb[1] - pa[1]))

    segs, where = [], {}
    for ci in list(CASES) + [5, 10]:
        ii, jj = np.nonzero(case == ci)
        for i, j in zip(ii.tolist(), jj.tolist()):
            if ci in SADDLE:
                centre = (tl[i, j] + tr[i, j] + bl[i, j] + br[i, j]) / 4 > level
                pairs = SADDLE[ci][bool(centre)]
            else:
                pairs = CASES[ci]
            for e1, e2 in pairs:
                k1, p1 = point(e1, i, j)
                k2, p2 = point(e2, i, j)
                segs.append((k1, k2))
                where[k1], where[k2] = p1, p2
    # join: every edge key has one or two segments
    nbr: dict = {}
    for s, (a, b) in enumerate(segs):
        nbr.setdefault(a, []).append((s, b))
        nbr.setdefault(b, []).append((s, a))
    used = [False] * len(segs)
    lines = []
    ends = [k for k, v in nbr.items() if len(v) == 1] + list(nbr)   # open ends first, then loops
    for start in ends:
        if all(used[s] for s, _ in nbr[start]):
            continue
        chain, cur = [start], start
        while True:
            nxt = [(s, o) for s, o in nbr[cur] if not used[s]]
            if not nxt:
                break
            s, o = nxt[0]
            used[s] = True
            chain.append(o)
            cur = o
            if cur == start:
                break
        if len(chain) >= 2:
            lines.append(np.array([where[k] for k in chain]))
    return lines


def write_dxf(path: Path, lines_by_level: list, major_step: float, metres: bool, name: str) -> int:
    """DXF R12, 3D polylines at their height; layers minor / major (a level is
    major when it is a whole multiple of `major_step`)."""
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009",
           "9", "$INSUNITS", "70", "6" if metres else "0", "0", "ENDSEC",
           "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", "2"]
    for lay, col in ((f"{name} contours minor", 8), (f"{name} contours major", 7)):
        out += ["0", "LAYER", "2", lay, "70", "0", "62", str(col), "6", "CONTINUOUS"]
    out += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    n = 0
    for z, lines in lines_by_level:
        q = z / major_step
        lay = f"{name} contours {'major' if abs(q - round(q)) < 1e-6 else 'minor'}"
        for pts in lines:
            closed = len(pts) > 3 and np.allclose(pts[0], pts[-1])
            out += ["0", "POLYLINE", "8", lay, "66", "1", "10", "0", "20", "0", "30", f"{z:.6f}",
                    "70", "9" if closed else "8"]
            for x, y in (pts[:-1] if closed else pts):
                out += ["0", "VERTEX", "8", lay, "10", f"{x:.6f}", "20", f"{y:.6f}",
                        "30", f"{z:.6f}", "70", "32"]
            out += ["0", "SEQEND", "8", lay]
            n += 1
    out += ["0", "ENDSEC", "0", "EOF"]
    path.write_text("\n".join(out) + "\n", encoding="ascii")
    return n


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="contour interval in the package's units (default: chosen)")
    ap.add_argument("--major-every", type=int, default=5)
    args = ap.parse_args()
    t0 = time.time()

    folder = scene_paths.scene_dir(args.name)
    site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    sd = scene_paths.site_data_dir(args.name)
    targets = {"points": sd / "point cloud" / f"{args.name}_points.ply",
               "ground": sd / "terrain" / f"{args.name}_ground.ply",
               "dxf": sd / "terrain" / f"{args.name}_contours.dxf"}
    for p in targets.values():
        if p.exists():
            print(f"FAILED: {p} exists -- never overwritten", file=sys.stderr)
            return 2
        p.parent.mkdir(parents=True, exist_ok=True)
    src = folder / "mesh" / "dense.ply"
    if not src.is_file():
        print(f"FAILED: no dense cloud at {src} -- build the mesh first", file=sys.stderr)
        return 2

    M = np.asarray(site["matrix"], float)
    A = M[:3, :3]
    Rs = A / np.cbrt(np.linalg.det(A))
    metres = site["scale"]["m_per_unit"] is not None
    unit = "m" if metres else "scan units"

    xyz, nrm, rgb = read_cloud(src)
    ok = np.isfinite(xyz).all(axis=1)
    xyz, nrm, rgb = xyz[ok], (nrm[ok] if nrm is not None else None), (rgb[ok] if rgb is not None else None)
    P = xyz @ A.T + M[:3, 3]
    Nn = nrm @ Rs.T if nrm is not None else None
    write_cloud(targets["points"], P, Nn, rgb,
                f"{args.name} dense cloud, site.json frame (Z up, Y {site['axes']['y']}), units {site['units']}")
    print(f"    point cloud: {len(P):,} points ({time.time() - t0:.0f} s)")

    extent = P.max(axis=0) - P.min(axis=0)
    cell = round(float(max(extent[0], extent[1])) / 300.0, 4)
    cloth_res, threshold = cell * 3.0, cell * 3.0
    ground, stats = ground_dem.classify_ground(P, cloth_res, threshold, 3, 300)
    G = P[ground]
    write_cloud(targets["ground"], G, Nn[ground] if Nn is not None else None,
                rgb[ground] if rgb is not None else None,
                f"{args.name} ground points (cloth simulation filter), site.json frame")
    print(f"    ground: {len(G):,} of {len(P):,} points ({len(G) / len(P):.0%}) ({time.time() - t0:.0f} s)")

    grid_cell, density = ground_dem.cell_for_density(G, cell, 2)
    grid, meta = ground_dem.rasterise(G, grid_cell, 2)
    x0, y1, nx, ny, thin = meta
    grid, filled = ground_dem.fill_holes(grid, 4)
    finite = grid[np.isfinite(grid)]
    print(f"    terrain grid: {nx} x {ny} cells of {grid_cell:g} {unit}, "
          f"{np.isfinite(grid).mean():.0%} with a height")

    xs = x0 + (np.arange(nx) + 0.5) * grid_cell            # cell centres
    ys = y1 - (np.arange(ny) + 0.5) * grid_cell
    step = choose_interval(finite, args.interval)
    first = math.ceil(float(finite.min()) / step) * step
    levels = np.arange(first, float(finite.max()), step)
    lines_by_level = [(float(z), trace(grid, float(z), xs, ys)) for z in levels]
    n = write_dxf(targets["dxf"], lines_by_level, step * args.major_every, metres, args.name)
    print(f"    contours: every {step:g} {unit} (major every {args.major_every}), "
          f"{len(levels)} levels, {n} polylines ({time.time() - t0:.0f} s)")

    report = {"scene": args.name, "units": site["units"], "points": int(len(P)),
              "ground_points": int(len(G)), "ground_share": round(len(G) / len(P), 4),
              "cloth": {"resolution": cloth_res, "threshold": threshold, **stats},
              "grid": {"cell": grid_cell, "width": nx, "height": ny, "filled_cells": int(filled),
                       "thin_cells_dropped": int(thin), "z_min": float(finite.min()),
                       "z_max": float(finite.max()), "with_height": float(np.isfinite(grid).mean()),
                       "x0": x0, "y_top": y1, "cell_from": "point density" if density else "extent"},
              "contours": {"interval": step, "major_every": args.major_every,
                           "levels": len(levels), "polylines": n},
              "caveat": "A camera sees surfaces, not the ground under planting: where vegetation "
                        "hides the ground, this 'ground' is the top of the vegetation.",
              "seconds": round(time.time() - t0, 1)}
    (folder / "mesh" / "terrain.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for p in targets.values():
        print(f"    written {p.name}  {p.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
