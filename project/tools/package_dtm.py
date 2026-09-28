"""A digital terrain model of a scene: a grid, a quad mesh, an orthophoto and a shell to print.

    python tools/package_dtm.py <scene name> [--cell 0] [--out <folder>]
                                [--source ground|surface] [--shell 0]

Marc (2026-09-27): one more product from the video -- a DTM with a UNIFORM CELL, as

    <name>_DTM_<cell>.tif            GeoTIFF heights, for DL-TerrainDiversity, QGIS, DL-TerrainSlicer,
                                     DL-3DPrint (the layout of DL-TerrainDiversity's own writer)
    <name>_DTM_<cell>.obj / .mtl     the same grid as a QUAD mesh -- one vertex per cell, one
                                     four-sided face per 2 x 2 cells -- textured with
    <name>_DTM_<cell>_ortho.jpg      the orthophoto: the textured mesh seen straight down
                                     (+ .jgw world file, for GIS and Rhino)
    <name>_DTM_<cell>_shell.obj      a closed SHELL in the same coordinates: the surface, the
                                     surface offset underneath, walls along the outline --
                                     to cut a piece out of with a boolean and print that

A height field has no undercuts by construction: one height per x,y. That is what the
water-flow tools want (DL-TerrainDiversity routes water over the GeoTIFF; in Blender a quad
grid is what geometry nodes work on), and what a printer can build without supports.

THE CELL. --cell 0 (the default) takes the finest cell the ground points can fill -- the rule
of tools/ground_dem.cell_for_density: three in four of the cells that hold ground must reach two
points -- rounded UP to a round number (1-2-2.5-5), so a model can be made again at the same
cell. Any other value is used as given (in the package's units; metres once the scan is
calibrated). A cell finer than the data only interpolates; a coarser one smooths.

THE FOOTPRINT of both models is the surface model's: every cell where the video SAW a surface
(the textured mesh's painted triangles, seen from above; patches of at least 5 % of the largest;
holes inside the outline filled by relaxation from their rims). Outside it: NoData.

THE SURFACE MODEL (--source surface) is the top of that seen surface per cell.

THE GROUND MODEL (--source ground) starts from the package's ground points (package_terrain.py:
cloth simulation filter, site frame; a cell needs two points -- one is not a measurement) and
has NO GAPS inside the footprint (Marc, 2026-09-27: the first student scan's ground model had a band-shaped hole
where the mesh had none -- the cloth filter drops steep faces and whatever stands up, 31 % of
that scan's mesh, and only holes enclosed on all sides were being filled):
    ground points        kept as they are (their mean per cell)
    two heights          a cell whose seen surface stands more than 3 cells above its ground
                         points (rock over low ground -- the upper wall on that scan) takes the surface:
                         "the rock wins" (Marc's choice between two variants, 2026-09-27)
    narrow gaps          every cell within 5 cells of ground points (a stone, a tuft): bridged
                         from the ground around them -- what stands in them is left out
    wide gaps            the seen surface, matched to the ground at the rim (the difference
                         fading over 3 cells) -- a face the filter could not follow
    never above the seen surface
The caveat of every camera DTM: where planting hides the ground, the "ground" is its top -- and
with "the rock wins", ground points seen under a canopy give way to the canopy too.

THE SHELL (Marc, 2026-09-27: nobody prints the whole site -- students cut the part they want
out with a boolean, then scale it for the printer; and not a block with dead space under it,
a shell) stays in the site's coordinates, like every other file, so a box drawn over a spot
seen in the drawings cuts exactly that spot. Four-sided faces throughout: the surface on top,
underneath the surface OFFSET BY A BALL of radius --shell (0: three cells) -- the lower envelope
of spheres rolled along under the surface, so the shell is that thick measured in ANY
direction, also on the rock face (a plain downward shift would leave a steep face paper-thin),
and the underside is a height field again (it cannot cross itself) -- and a wall along each
outline edge. Checked closed before it is written (every edge in exactly two faces, once each
way; volume > 0) -- a boolean needs exactly that.
Same day, two earlier versions: a 200 mm STL to print whole, then a block down to a flat base.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import ground_dem          # noqa: E402 -- cell_for_density, rasterise, write_raster
import package_drawings as pdw   # noqa: E402 -- drawn_mesh, ortho
import package_terrain as pt     # noqa: E402 -- read_cloud, NICE
import scene_paths         # noqa: E402

KEEP_ISLANDS = 0.05     # keep patches of at least this share of the largest
RELAX_PASSES = 200      # smoothing passes over the filled holes (measured cells stay fixed)


def nice_up(x: float) -> float:
    return next((v for v in pt.NICE if v >= x - 1e-12), float(x))


def default_cell(G: np.ndarray) -> tuple[float, float]:
    extent = G.max(axis=0) - G.min(axis=0)
    start = round(float(max(extent[0], extent[1])) / 300.0, 4)
    c, _ = ground_dem.cell_for_density(G, start, 2)
    return nice_up(c), c


def cell_label(c: float) -> str:
    return f"{c:g}"


def keep_main(valid: np.ndarray) -> tuple[np.ndarray, int]:
    """Patches (4-connected) of at least KEEP_ISLANDS x the largest, and every patch INSIDE
    their outline; small patches outside are dropped.
    !! v1 (2026-09-27) dropped small patches inside the outline too, and the hole filling then
    painted over them: 27 measured cells on a student scan replaced by a guess, up to 0.58 off (check D1)."""
    n, lab = cv2.connectedComponents(valid.astype(np.uint8), connectivity=4)
    if n <= 2:
        return valid, 0
    sizes = np.bincount(lab.ravel())[1:]
    big = np.nonzero(sizes >= KEEP_ISLANDS * sizes.max())[0] + 1
    out = np.isin(lab, big)
    out |= valid & interior(~out)            # measurements in a hole are not islands
    return out, int(valid.sum() - out.sum())


def interior(empty: np.ndarray) -> np.ndarray:
    """Empty cells NOT connected to the raster's edge through empty cells: holes inside."""
    n, lab = cv2.connectedComponents(empty.astype(np.uint8), connectivity=4)
    edge = np.unique(np.r_[lab[0], lab[-1], lab[:, 0], lab[:, -1]])
    return empty & ~np.isin(lab, edge[edge > 0]) & (lab > 0)


def fill_interior(grid: np.ndarray, holes: np.ndarray) -> np.ndarray:
    """Holes filled from their rims (neighbour means, outward in), then relaxed: each filled
    cell becomes the mean of its four neighbours, measured cells held fixed."""
    out = grid.copy()
    todo = holes & np.isnan(out)
    while todo.any():
        p = np.pad(out, 1, constant_values=np.nan)
        st = np.stack([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
        cnt = np.isfinite(st).sum(0)
        s = np.nansum(st, 0)
        can = todo & (cnt > 0)
        if not can.any():
            break
        out[can] = s[can] / cnt[can]
        todo &= ~can
    for _ in range(RELAX_PASSES):
        p = np.pad(out, 1, mode="edge")
        st = np.stack([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
        cnt = np.isfinite(st).sum(0)
        m = np.nansum(st, 0) / np.maximum(cnt, 1)
        out[holes] = m[holes]
    return out


SUB = 8                 # surface source: depth samples per cell side
SURFACE_COVER = 0.5     # a cell needs this share of its samples on a seen surface


NARROW_CELLS = 5        # ground model: a gap whose every cell is within this many cells of ground
                        # points is bridged from the ground; a wider one follows the seen surface
TRANSITION_CELLS = 3    # ... matched to the ground at its rim, the match fading over this many cells
TWO_LEVEL_CELLS = 3.0   # a cell whose seen surface stands this many cells above its ground points
                        # has two heights; the surface wins


def rasterise_on(xyz: np.ndarray, x0: float, y1: float, nx: int, ny: int, cell: float,
                 min_points: int) -> np.ndarray:
    """Like ground_dem.rasterise, on a GIVEN grid (the surface model's): the mean height of the
    points in each cell holding at least min_points of them, NaN elsewhere."""
    ix = np.floor((xyz[:, 0] - x0) / cell).astype(np.int64)
    iy = np.floor((y1 - xyz[:, 1]) / cell).astype(np.int64)
    ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
    total = np.zeros((ny, nx))
    count = np.zeros((ny, nx))
    np.add.at(total, (iy[ok], ix[ok]), xyz[ok, 2])
    np.add.at(count, (iy[ok], ix[ok]), 1)
    return np.where(count >= max(1, min_points), total / np.maximum(count, 1), np.nan)


def ground_with_surface(ground: np.ndarray, surf: np.ndarray, cell: float) -> tuple[np.ndarray, dict]:
    """The ground model without gaps inside the footprint (see the module text): ground points
    kept, two-level cells to the surface, narrow gaps bridged from the ground, wide gaps the
    seen surface matched at the rim; never above the seen surface.
    Wrong turns, 2026-09-27 (test outputs\\2026-09-27\\dtm gaps\\): v1 took stones off the surface
    with a morphological OPENING -- it cut the rock face down 1-1.5 units (every convex edge of
    a steep face is 'narrower than the disk'); v2 relaxed the rim difference through the whole
    wide gap -- ground cells at the foot of the face hold the foot's height while the surface
    cell there already climbs it, and that lowered the whole face 0.6; v3 (fading) was clean
    except where cells have two heights -- a comb of spikes along the upper wall."""
    two = np.isfinite(ground) & (surf - ground > TWO_LEVEL_CELLS * cell)
    have = np.isfinite(ground) & ~two
    gap = np.isfinite(surf) & ~have
    dist = cv2.distanceTransform((~have).astype(np.uint8), cv2.DIST_L2, 5)   # cells to the nearest ground
    n, lab = cv2.connectedComponents(gap.astype(np.uint8), connectivity=4)
    reach = np.zeros(n)
    np.maximum.at(reach, lab[gap], dist[gap])                 # per gap: its cell farthest from ground
    narrow = gap & (reach[lab] <= NARROW_CELLS)
    wide = gap & ~narrow
    out = np.where(have, ground, np.nan)
    bridged = fill_interior(out, narrow)
    diff = fill_interior(np.where(have, ground - surf, np.nan), wide)
    diff = np.where(np.isfinite(diff), diff, 0.0)             # a wide gap with no ground around it
    fade = np.clip((TRANSITION_CELLS + 1 - dist) / TRANSITION_CELLS, 0.0, 1.0)   # 1 at the rim, 0 inside
    out[narrow] = bridged[narrow]
    out[wide] = (surf + diff * fade)[wide]
    left = gap & ~np.isfinite(out)                           # a narrow gap no ground reached
    out[left] = surf[left]
    out = np.where(gap, np.minimum(out, surf), out)
    return out, {"gap": gap, "narrow": int(narrow.sum()), "wide": int(wide.sum()), "two_level": int(two.sum())}


def surface_grid(V, F, cell):
    """The top of the SEEN surface per cell (--source surface): the textured mesh rendered
    straight down at cell/SUB, the highest surface per sample, averaged per cell. Cells under
    half covered stay empty. On a bare site -- rock, scree, short grass -- this is the terrain;
    under trees or next to a car it is their top (a DSM).
    Why it exists (2026-09-27): the cloth filter behind --source ground drops steep slopes as
    'not ground' -- on one student scan the whole inclined rock face between the scree and the top of the wall
    went missing, and water routed over that model would stop at the gap."""
    P = V[np.unique(F)]
    x0, y1 = float(P[:, 0].min()), float(P[:, 1].max())
    nx = int(np.ceil((P[:, 0].max() - x0) / cell)) + 1
    ny = int(np.ceil((y1 - P[:, 1].min()) / cell)) + 1
    tex = np.zeros((2, 2, 3), np.uint8)
    UVd = np.zeros((len(V), 2))
    _, depth = pdw.ortho(V, UVd, F, tex, np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0]),
                         cell / SUB, (x0, y1, nx * SUB, ny * SUB))
    blocks = depth.reshape(ny, SUB, nx, SUB)
    cnt = np.isfinite(blocks).sum(axis=(1, 3))
    tot = np.nansum(blocks, axis=(1, 3))
    grid = np.where(cnt >= SURFACE_COVER * SUB * SUB, tot / np.maximum(cnt, 1), np.nan)
    return grid, (x0, y1, nx, ny, int(((cnt > 0) & (cnt < SURFACE_COVER * SUB * SUB)).sum()))


SPIKE_CELLS = 3.0       # a cell this many cell sizes above (or below) ALL eight neighbours is noise


def despike(grid: np.ndarray, cell: float) -> tuple[np.ndarray, int, int]:
    """Single cells standing above -- or sunk below -- all eight neighbours by more than
    SPIKE_CELLS x the cell size take the neighbours' median. A cliff edge is not touched (its
    top cells have neighbours as high); a one-cell pit would trap water in any flow analysis.
    On a student scan's surface model (2026-09-27): 5 spikes, 8 pits among 12,467 cells."""
    import warnings
    p = np.pad(grid, 1, constant_values=np.nan)
    h, w = grid.shape
    nb = np.stack([p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        hi, lo, med = np.nanmax(nb, 0), np.nanmin(nb, 0), np.nanmedian(nb, 0)
    full = np.isfinite(nb).sum(0) >= 3
    up = full & ((grid - hi) > SPIKE_CELLS * cell)
    down = full & ((lo - grid) > SPIKE_CELLS * cell)
    out = grid.copy()
    out[up | down] = med[up | down]
    return out, int(up.sum()), int(down.sum())


GROUP_CELLS = 12        # outlier groups: at most this many connected cells ...
GROUP_LIMIT = 3.0       # ... standing above (or sunk below) ALL cells around them by more than this x cell


def outlier_groups(grid: np.ndarray, cell: float, passes: int = 3) -> tuple[np.ndarray, list]:
    """Groups of up to GROUP_CELLS connected cells that stand above -- or are sunk below -- ALL the
    cells around them by more than GROUP_LIMIT x the cell size are emptied and filled again from
    around them (fill_interior); repeated until none is left. The one-cell despike is the group of
    one; it missed groups (a student scan, 2026-09-27: a 2-cell spike 7 cells high) and left two pits 28 and 35
    cells deep in a published model. Candidates differ from the median of their 5 x 5 surroundings by
    more than the limit. A cliff edge is not a group standing above all its surroundings (its top
    continues beside it); a hollow with a sloping rim is not sunk below all of its rim. Tested on
    three student scans before it came here: test outputs\\2026-09-27\\terrain outliers\\checks - 001.txt."""
    import warnings
    g = grid.copy()
    found = []
    h, w = g.shape
    for _ in range(passes):
        valid = np.isfinite(g)
        p = np.pad(g, 2, constant_values=np.nan)
        st = np.stack([p[2 + dy:2 + dy + h, 2 + dx:2 + dx + w]
                       for dy in range(-2, 3) for dx in range(-2, 3) if dy or dx])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            med = np.nanmedian(st, 0)
        dev = np.where(valid, g - med, 0)
        holes = np.zeros(g.shape, bool)
        for sign in (1, -1):
            cand = (valid & (sign * dev > GROUP_LIMIT * cell)).astype(np.uint8)
            n, lab, stats, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
            for k in range(1, n):
                if stats[k, cv2.CC_STAT_AREA] > GROUP_CELLS:
                    continue
                comp = lab == k
                ring = (cv2.dilate(comp.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0) & ~comp & valid
                if not ring.any():
                    continue
                gap = (g[comp].min() - g[ring].max()) if sign > 0 else (g[ring].min() - g[comp].max())
                if gap > GROUP_LIMIT * cell:
                    found.append({"sign": "up" if sign > 0 else "down", "cells": int(comp.sum()),
                                  "stands_out_cells": round(float(gap / cell), 2)})
                    holes |= comp
        if not holes.any():
            break
        g[holes] = np.nan
        g = fill_interior(g, holes)
    return g, found


HYDRO_SIGMA = 1.0       # the water version: Gaussian smoothing, in cells


def hydro_grid(grid: np.ndarray, cell: float) -> tuple[np.ndarray, dict]:
    """The water version (Marc, 2026-09-27): the outlier-free model smoothed (Gaussian, HYDRO_SIGMA
    cells, over measured cells only), then every sink filled so that water runs off every cell to
    the model's edge -- priority-flood with a small slope on flats (Barnes, Lehman, Mulla 2014,
    doi:10.1016/j.cageo.2013.04.024). It only raises cells (after smoothing)."""
    import heapq
    valid = np.isfinite(grid)
    num = cv2.GaussianBlur(np.where(valid, grid, 0).astype(np.float64), (0, 0), HYDRO_SIGMA)
    den = cv2.GaussianBlur(valid.astype(np.float64), (0, 0), HYDRO_SIGMA)
    sm = np.where(valid, num / np.maximum(den, 1e-12), np.nan)
    z = sm.copy()
    eps = 1e-5 * cell
    h, w = z.shape
    border = valid & ~(cv2.erode(np.pad(valid, 1).astype(np.uint8), np.ones((3, 3), np.uint8))[1:-1, 1:-1] > 0)
    seen = border.copy()
    heap = [(float(z[y, x]), int(y), int(x)) for y, x in zip(*np.nonzero(border))]
    heapq.heapify(heap)
    while heap:
        zc, y, x = heapq.heappop(heap)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                ny, nx_ = y + dy, x + dx
                if (dy or dx) and 0 <= ny < h and 0 <= nx_ < w and valid[ny, nx_] and not seen[ny, nx_]:
                    seen[ny, nx_] = True
                    if z[ny, nx_] <= zc:
                        z[ny, nx_] = zc + eps
                    heapq.heappush(heap, (float(z[ny, nx_]), ny, nx_))
    raised = valid & (z > sm + 1e-9)
    info = {"smoothing_cells": HYDRO_SIGMA, "filled_cells": int(raised.sum()),
            "filled_share": round(float(raised.sum() / max(valid.sum(), 1)), 4),
            "max_fill": round(float(np.max(np.where(raised, z - sm, 0))), 6),
            "method": "Gaussian smoothing, then priority-flood sink filling with a small slope on flats "
                      "(Barnes et al. 2014)"}
    return z, info


def quads(valid: np.ndarray) -> np.ndarray:
    """Quad (i, j) joins cells (i,j) (i,j+1) (i+1,j) (i+1,j+1) when all four exist. Faces
    touching only at a corner are thinned until none do (else the solid is not closed)."""
    Q = valid[:-1, :-1] & valid[:-1, 1:] & valid[1:, :-1] & valid[1:, 1:]
    while True:
        P = np.pad(Q, 1)                       # P[i+1, j+1] = Q[i, j]
        a, b = P[:-1, :-1], P[:-1, 1:]         # quads around vertex (i, j): up-left, up-right,
        c, d = P[1:, :-1], P[1:, 1:]           # down-left, down-right
        pinch1 = a & d & ~b & ~c
        pinch2 = b & c & ~a & ~d
        if not (pinch1.any() or pinch2.any()):
            return Q
        # drop the down-right quad of a pinch1 vertex and the down-left of a pinch2 one
        i, j = np.nonzero(pinch1)
        Q[i, j] = False
        i, j = np.nonzero(pinch2)
        Q[i, j - 1] = False


def write_obj(path: Path, name: str, verts, uvs, faces, mtl: str, header: list[str]):
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for h in header:
            fh.write(f"# {h}\n")
        fh.write(f"mtllib {mtl}\no {path.stem}\n")
        fh.write("".join(f"v {x:.6f} {y:.6f} {z:.6f}\n" for x, y, z in verts))
        fh.write("".join(f"vt {u:.6f} {v:.6f}\n" for u, v in uvs))
        fh.write(f"usemtl {path.stem}\ns off\n")
        fh.write("".join("f " + " ".join(f"{k}/{k}" for k in (f + 1)) + "\n" for f in faces))


def ball_offset(grid: np.ndarray, cell: float, t: float) -> np.ndarray:
    """The underside of a shell of thickness t: per cell, the lowest point of the balls of radius
    t centred on the surface around it -- min over |d| <= t of z(x + d) - sqrt(t^2 - |d|^2), a
    grey-scale erosion with a spherical element. At least t from every surface point in any
    direction (a steep face keeps its thickness), exactly t under a flat one, and a height field
    again: the underside cannot cross itself or the top."""
    r = int(np.floor(t / cell + 1e-9))
    h, w = grid.shape
    p = np.pad(grid, r, constant_values=np.nan)
    out = np.full(grid.shape, np.inf)
    for di in range(-r, r + 1):
        for dj in range(-r, r + 1):
            d2 = (di * di + dj * dj) * cell * cell
            if d2 > t * t + 1e-12:
                continue
            out = np.fmin(out, p[r + di:r + di + h, r + dj:r + dj + w] - np.sqrt(max(t * t - d2, 0.0)))
    return np.where(np.isfinite(grid), out, np.nan)


SHELL_SUB = 4           # the top surface is offset from samples every 1/SHELL_SUB of a cell


def shell_underside(grid: np.ndarray, Q: np.ndarray, cell: float, t: float) -> np.ndarray:
    """The shell's underside under every cell centre, offset from the top SURFACE as written --
    each quad split SW-SE-NE + SW-NE-NW, sampled every cell/SHELL_SUB -- not from its vertices
    only. !! v6 (2026-09-27) offset from the vertices: between them the triangulated top came
    closer on rough steep ground -- 76 % of the thickness on a student scan's surface model (check D4'')."""
    k = SHELL_SUB
    h, w = grid.shape
    fine = np.full(((h - 1) * k + 1, (w - 1) * k + 1), np.nan)
    qi, qj = np.nonzero(Q)
    nw, ne = grid[qi, qj], grid[qi, qj + 1]
    sw, se = grid[qi + 1, qj], grid[qi + 1, qj + 1]
    for a in range(k + 1):                   # rows, from the quad's north edge
        s = 1 - a / k                        # 0 at the south edge, 1 at the north
        for b in range(k + 1):               # columns, from the west edge
            u = b / k
            if u >= s:                       # triangle SW-SE-NE
                z = sw + u * (se - sw) + s * (ne - se)
            else:                            # triangle SW-NE-NW
                z = sw + s * (nw - sw) + u * (ne - nw)
            fine[qi * k + a, qj * k + b] = z
    under = ball_offset(fine, cell / k, t)
    out = np.full(grid.shape, np.nan)
    out[: h, : w] = under[::k, ::k]
    return out


def solid(verts, faces, z_under):
    """Quads of a closed solid in the verts' own coordinates: the surface on top, the underside
    (z_under per vertex -- a shell -- or one number -- a flat base), a wall along every outline
    edge."""
    n = len(verts)
    bot = verts.copy()
    bot[:, 2] = z_under
    V = np.r_[verts, bot]
    # outline: directed edges of the quads that no other quad runs the other way
    e = np.concatenate([faces[:, [k, (k + 1) % 4]] for k in range(4)])
    key = e[:, 0].astype(np.int64) * n + e[:, 1]
    rev = e[:, 1].astype(np.int64) * n + e[:, 0]
    border = e[~np.isin(key, rev)]
    a, b = border[:, 0], border[:, 1]
    Fq = np.concatenate([faces,                                        # top, CCW from above
                         faces[:, ::-1] + n,                           # base, facing down
                         np.stack([a, a + n, b + n, b], 1)])           # walls, facing out
    return V, Fq


def triangles(Fq):
    return np.concatenate([Fq[:, [0, 1, 2]], Fq[:, [0, 2, 3]]])


def closed(V, T) -> tuple[bool, float]:
    """Every edge in exactly two triangles, once each way; and the signed volume."""
    e = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]]).astype(np.int64)
    n = len(V)
    fwd = e[:, 0] * n + e[:, 1]
    back = e[:, 1] * n + e[:, 0]
    u, cnt = np.unique(fwd, return_counts=True)
    ok = (cnt == 1).all() and np.isin(back, u).all()
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    vol = float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6)
    return bool(ok), vol


def write_solid_obj(path: Path, V, Fq, header: list[str]):
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for h in header:
            fh.write(f"# {h}\n")
        fh.write(f"o {path.stem}\n")
        fh.write("".join(f"v {x:.6f} {y:.6f} {z:.6f}\n" for x, y, z in V))
        fh.write("s off\n")
        fh.write("".join("f " + " ".join(str(k) for k in (f + 1)) + "\n" for f in Fq))


SHELL_CELLS = 3         # default shell thickness, in cells
SHELL_RANGE = (0.5, 10.0)   # allowed shell thickness, in cells (thinner: the offset has too few
                            # samples to hold the thickness; thicker: the ball offset gets slow)
MAX_CELLS = 1_000_000   # grid size limit (a student scan, 2026-09-27: 0.1 -> 29k cells 3 s, 0.05 -> 115k 9 s,
                        # 0.025 -> 454k 26 s)
MAX_SHELL_WORK = 5_000_000  # cells x (shell in cells)^2 -- the ball offset's cost (0.025 with a
                            # 3-cell shell: 4.1 M, 26 s; 0.1 with 10 cells: 2.9 M, 9 s)


def limits(G: np.ndarray, cell: float) -> tuple[float, float]:
    """(estimated cells at this cell, the finest cell within MAX_CELLS) from the ground points'
    extent."""
    ext = G[:, :2].max(axis=0) - G[:, :2].min(axis=0)
    est = float((ext[0] / cell + 1) * (ext[1] / cell + 1))
    finest = float(np.sqrt(ext[0] * ext[1] / MAX_CELLS)) * 1.05
    return est, finest


def readiness(n: str) -> list[str]:
    """What the scene still lacks for a terrain model (empty: ready)."""
    folder = scene_paths.scene_dir(n)
    sd = scene_paths.site_data_dir(n)
    need = [(folder / "site.json", "the site coordinate system (site.json)"),
            (sd / "terrain" / f"{n}_ground.ply", "the ground points in site coordinates (tools/package_terrain.py)"),
            (sd / "mesh" / f"{n}_mesh.obj", "the textured mesh (tools/mesh_texture.py)"),
            (sd / "mesh" / f"{n}_mesh_texture.jpg", "the mesh's texture (tools/mesh_texture.py)")]
    return [what for p, what in need if not p.is_file()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--cell", type=float, default=0.0, help="cell size in the package's units (0: from the data)")
    ap.add_argument("--out", default=None, help="folder (default: the package's site data/terrain/)")
    ap.add_argument("--shell", type=float, default=0.0,
                    help=f"shell thickness, package units (0: {SHELL_CELLS} cells)")
    ap.add_argument("--source", choices=("ground", "surface"), default="ground",
                    help="ground: the ground points (a DTM); surface: the top of the scanned surface (a DSM)")
    ap.add_argument("--info", action="store_true",
                    help="only say whether the scene is ready and which cell the data gives (one DTMINFO line)")
    args = ap.parse_args()
    t0 = time.time()
    n = args.name
    missing = readiness(n)
    if args.info:
        info = {"name": n, "ready": not missing, "missing": missing, "shell_cells": SHELL_CELLS}
        if not missing:
            site = json.loads((scene_paths.scene_dir(n) / "site.json").read_text(encoding="utf-8"))
            G, _, _ = pt.read_cloud(scene_paths.site_data_dir(n) / "terrain" / f"{n}_ground.ply")
            auto, dens = default_cell(G)
            _, finest = limits(G, auto)
            info.update(units=site["units"], metres=site["scale"]["m_per_unit"] is not None,
                        default_cell=auto, density_cell=round(float(dens), 6),
                        min_cell=float(f"{finest:.2g}"), shell_range_cells=list(SHELL_RANGE))
        print("DTMINFO " + json.dumps(info))
        return 0
    if missing:
        print("FAILED: this scene has no " + "; no ".join(missing), file=sys.stderr)
        return 2
    folder = scene_paths.scene_dir(n)
    site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    metres = site["scale"]["m_per_unit"] is not None
    unit = "m" if metres else "scan units"
    sd = scene_paths.site_data_dir(n)
    G, _, _ = pt.read_cloud(sd / "terrain" / f"{n}_ground.ply")

    auto, density_cell = default_cell(G)
    cell = args.cell if args.cell > 0 else auto
    lab = cell_label(cell)
    t = args.shell if args.shell > 0 else round(SHELL_CELLS * cell, 9)
    est, finest = limits(G, cell)
    if est > MAX_CELLS:
        print(f"FAILED: a cell of {cell:g} {unit} gives about {est:,.0f} cells (limit {MAX_CELLS:,}) -- "
              f"choose {finest:.2g} or more", file=sys.stderr)
        return 5
    lo, hi = SHELL_RANGE[0] * cell, SHELL_RANGE[1] * cell
    if not lo - 1e-12 <= t <= hi + 1e-12:
        print(f"FAILED: the shell must be between {SHELL_RANGE[0]:g} and {SHELL_RANGE[1]:g} cells thick "
              f"({lo:.3g}-{hi:.3g} {unit} at this cell)", file=sys.stderr)
        return 5
    if est * (t / cell) ** 2 > MAX_SHELL_WORK:
        print(f"FAILED: a {t:g} {unit} shell on a {cell:g} {unit} grid would take too long -- "
              "a thinner shell or a coarser cell", file=sys.stderr)
        return 5
    out = Path(args.out) if args.out else sd / "terrain"
    out.mkdir(parents=True, exist_ok=True)
    kind = "DTM" if args.source == "ground" else "DSM"
    stem = f"{n}_{kind}_{lab}"
    # the default thickness keeps the plain name; any other thickness is in the file name
    default_t = abs(t - SHELL_CELLS * cell) <= 1e-9 * max(1.0, t)
    final = {"tif": out / f"{stem}.tif", "obj": out / f"{stem}.obj", "mtl": out / f"{stem}.mtl",
             "ortho": out / f"{stem}_ortho.jpg", "jgw": out / f"{stem}_ortho.jgw",
             "hydro_tif": out / f"{stem}_hydro.tif", "hydro_obj": out / f"{stem}_hydro.obj",
             "shell": out / (f"{stem}_shell.obj" if default_t else f"{stem}_shell_{t:g}.obj")}
    # Everything is written to a folder of its own first. Then: a file that already exists
    # with the SAME bytes stays as it is (the model is made again exactly -- nothing is
    # overwritten); one that exists with DIFFERENT bytes stops the whole build (never
    # overwritten). So a second shell thickness at the same cell adds one file and nothing else.
    tmp = out / f".building {stem}"
    tmp.mkdir(exist_ok=True)
    files = {k: tmp / p.name for k, p in final.items()}
    print(f"    cell {cell:g} {unit}" + (f" (from the data: points fill {density_cell:.4g}, rounded up)"
                                         if args.cell <= 0 else " (as given)"))

    # the surface model: what the video saw, islands dropped, holes inside the outline filled,
    # despiked -- the FOOTPRINT and the grid of both models
    V, UV, F, tex, _ = pdw.drawn_mesh(n)
    surf, meta = surface_grid(V, F, cell)
    x0, y1, nx, ny, thin = meta
    valid, dropped = keep_main(np.isfinite(surf))
    surf = np.where(valid, surf, np.nan)
    holes = interior(~valid)
    surf = fill_interior(surf, holes)
    # despiked FIRST: the ground model is capped by exactly the published surface model (a first
    # version capped by the raw surface and despiked after -- one cell ended 0.55 above it)
    surf, spikes, pits = despike(surf, cell)
    # then groups of outliers, on the FINISHED surface -- the published surface model, and the cap
    # of the ground model below (2026-09-27)
    surf, groups = outlier_groups(surf, cell)
    parts = {"narrow": 0, "wide": 0, "two_level": 0}
    if args.source == "surface":
        grid = surf
    else:
        ground = rasterise_on(G, x0, y1, nx, ny, cell, 2)
        ground = np.where(np.isfinite(surf), ground, np.nan)          # the footprint is the seen surface's
        grid, parts = ground_with_surface(ground, surf, cell)
        grid, spikes, pits = despike(grid, cell)
        grid = np.where(parts["gap"], np.minimum(grid, surf), grid)     # still never above the seen surface
        grid, groups = outlier_groups(grid, cell)
        grid = np.where(parts["gap"], np.minimum(grid, surf), grid)     # (a refilled cell too)
    valid = np.isfinite(grid)
    ground_dem.write_raster(files["tif"], grid.astype(np.float32), cell, meta, geokeys=True)
    hydro, hinfo = hydro_grid(grid, cell)
    ground_dem.write_raster(files["hydro_tif"], hydro.astype(np.float32), cell, meta, geokeys=True)
    print(f"    outlier groups refilled: {len(groups)} ({sum(g_['cells'] for g_ in groups)} cells); water version: "
          f"smoothed, sinks filled in {hinfo['filled_share']:.1%} of the cells")
    from_surface = parts["narrow"] + parts["wide"]
    print(f"    grid {nx} x {ny}: {int(valid.sum()):,} cells with a height, "
          + (f"{from_surface:,} of them without ground points to keep ({from_surface / max(valid.sum(), 1):.1%}): "
             f"{parts['narrow']:,} in narrow gaps bridged from the ground, {parts['wide']:,} following the seen "
             f"surface ({parts['two_level']:,} of those had ground points far below the rock), "
             if args.source == "ground" else
             f"{int(holes.sum()):,} filled inside the outline ({holes.sum() / max(valid.sum(), 1):.1%}), ")
          + f"{dropped:,} island cells dropped, {thin:,} thin cells left out, {spikes} spikes and {pits} pits "
          f"(> {SPIKE_CELLS:g} cells beyond all neighbours) set to their neighbours' median")

    # quad mesh: a vertex per cell centre, a face per 2 x 2
    Q = quads(valid)
    used = np.zeros_like(valid)
    qi, qj = np.nonzero(Q)
    for di, dj in ((0, 0), (0, 1), (1, 0), (1, 1)):
        used[qi + di, qj + dj] = True
    index = -np.ones(valid.shape, np.int64)
    vi, vj = np.nonzero(used)
    index[vi, vj] = np.arange(len(vi))
    xs = x0 + (vj + 0.5) * cell
    ys = y1 - (vi + 0.5) * cell
    verts = np.stack([xs, ys, grid[vi, vj]], 1)
    # CCW seen from above: south-west, south-east, north-east, north-west
    faces = np.stack([index[qi + 1, qj], index[qi + 1, qj + 1], index[qi, qj + 1], index[qi, qj]], 1)

    # orthophoto over the grid's extent, a UV per vertex
    W_units, H_units = nx * cell, ny * cell
    px = max(cell / 8.0, max(W_units, H_units) / 4096.0)
    W, H = int(np.ceil(W_units / px)), int(np.ceil(H_units / px))
    img, _ = pdw.ortho(V, UV, F, tex, np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0]),
                       px, (x0, y1, W, H))
    cv2.imwrite(str(files["ortho"]), img[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 92])
    files["jgw"].write_text(f"{px:.9f}\n0\n0\n{-px:.9f}\n{x0 + px / 2:.9f}\n{y1 - px / 2:.9f}\n")
    uvs = np.stack([(xs - x0) / (W * px), 1 - (y1 - ys) / (H * px)], 1)
    files["mtl"].write_text(f"# {n} {kind}, DL-SplatGenerator tools/package_dtm.py (Digital Landscapes)\n"
                            f"newmtl {stem}\nKa 1 1 1\nKd 1 1 1\nKs 0 0 0\nillum 1\nmap_Kd {files['ortho'].name}\n",
                            encoding="utf-8")
    axes = f"Z up, Y {site['axes']['y']}, origin on the ground below the first camera"
    write_obj(files["obj"], n, verts, uvs, faces, files["mtl"].name,
              [f"{n} -- {'terrain' if kind == 'DTM' else 'surface'} model as a quad mesh, DL-SplatGenerator "
               f"tools/package_dtm.py (Digital Landscapes)",
               f"one vertex per cell of {stem}.tif ({cell:g} {unit}), one four-sided face per 2 x 2 cells",
               f"coordinates: site.json -- {axes}; units: {site['units']}",
               "import with Up = Z, Forward = Y"])
    print(f"    quad mesh: {len(verts):,} vertices, {len(faces):,} quads; orthophoto {W} x {H} px "
          f"({px:.4g} {unit} per pixel)")
    verts_h = verts.copy()
    verts_h[:, 2] = hydro[vi, vj]
    write_obj(files["hydro_obj"], n, verts_h, uvs, faces, files["mtl"].name,
              [f"{n} -- the water version of the {'terrain' if kind == 'DTM' else 'surface'} model: smoothed "
               f"({HYDRO_SIGMA:g} cell), every sink filled so water runs off to the edge -- for flow analysis, "
               "not for measuring (DL-SplatGenerator tools/package_dtm.py, Digital Landscapes)",
               f"one vertex per cell of {stem}_hydro.tif ({cell:g} {unit}), one four-sided face per 2 x 2 cells",
               f"coordinates: site.json -- {axes}; units: {site['units']}",
               "import with Up = Z, Forward = Y"])

    # the shell
    under = shell_underside(grid, Q, cell, t)
    Vs, Fq = solid(verts, faces, under[vi, vj])
    ok, vol = closed(Vs, triangles(Fq))
    if not ok or vol <= 0:
        for p in files.values():
            p.unlink(missing_ok=True)
        tmp.rmdir()
        print(f"FAILED: the shell is not closed (edges ok {ok}, volume {vol:.3g}) -- nothing written", file=sys.stderr)
        return 3
    write_solid_obj(files["shell"], Vs, Fq,
                    [f"{n} -- the {'terrain' if kind == 'DTM' else 'surface'} model as a closed shell, "
                     "DL-SplatGenerator tools/package_dtm.py (Digital Landscapes)",
                     f"top = {stem}.obj; underneath the same surface offset by a ball of radius {t:g} {unit} "
                     "(that thick in any direction); walls along the outline; closed: cut a piece out with a boolean",
                     f"coordinates: site.json -- {axes}; units: {site['units']}",
                     "import with Up = Z, Forward = Y"])
    size = Vs.max(0) - Vs.min(0)
    print(f"    shell: {len(Fq):,} four-sided faces, closed, {size[0]:.3g} x {size[1]:.3g} x {size[2]:.3g} {unit}, "
          f"{t:g} {unit} thick")

    record = {"scene": n, "kind": kind, "source": args.source, "units": site["units"], "cell": cell, "cell_from": "data" if args.cell <= 0 else "given",
              "density_cell": density_cell, "grid": {"width": nx, "height": ny, "x0": x0, "y_top": y1,
              "cells_with_height": int(valid.sum()), "filled_inside": int(holes.sum()),
              "filled_share": round(float(holes.sum() / max(valid.sum(), 1)), 4),
              "ground_kept": int(valid.sum()) - from_surface if args.source == "ground" else None,
              "narrow_bridged": parts["narrow"], "wide_from_surface": parts["wide"],
              "two_level_to_surface": parts["two_level"],
              "without_ground_share": round(from_surface / max(int(valid.sum()), 1), 4),
              "rules": {"narrow_cells": NARROW_CELLS, "transition_cells": TRANSITION_CELLS,
                        "two_level_cells": TWO_LEVEL_CELLS} if args.source == "ground" else None,
              "island_cells_dropped": dropped, "thin_cells": thin, "spikes": spikes, "pits": pits,
              "outlier_groups": groups, "outlier_rule": f"groups of <= {GROUP_CELLS} cells standing above or "
              f"below ALL cells around them by > {GROUP_LIMIT:g} cells, refilled from around them",
              "hydro": hinfo,
              "z_min": float(np.nanmin(grid)), "z_max": float(np.nanmax(grid))},
              "quad_mesh": {"vertices": len(verts), "quads": len(faces)},
              "ortho": {"width": W, "height": H, "pixel": px},
              "files": {k: v.name for k, v in final.items() if k != "shell"}, "seconds": round(time.time() - t0, 1),
              "caveat": "A camera sees surfaces, not the ground under planting: where vegetation hides the "
                        "ground, this 'ground' is the top of the vegetation. Filled cells are a smooth guess."}
    this_shell = {"file": final["shell"].name, "thickness": t, "faces": len(Fq),
                  "size": [round(float(v), 4) for v in size], "volume": round(vol, 4),
                  "offset": "ball (lower envelope of spheres of radius thickness)"}

    # into place: identical files stay, different ones stop everything
    written, unchanged, conflicts = [], [], []
    for k, p in files.items():
        dest = final[k]
        if dest.exists():
            (unchanged if dest.read_bytes() == p.read_bytes() else conflicts).append(dest.name)
    if conflicts:
        for p in files.values():
            p.unlink(missing_ok=True)
        tmp.rmdir()
        print(f"FAILED: {', '.join(conflicts)} already exist(s) with different content -- never overwritten. "
              "Move them away, or choose another cell size.", file=sys.stderr)
        return 4
    for k, p in files.items():
        if final[k].exists():
            p.unlink()
        else:
            p.replace(final[k])
            written.append(final[k].name)
    tmp.rmdir()

    # the record goes beside the files in a test run, to the working folder in a package build;
    # one per model and cell, listing every shell made from it
    rec_dir = out if args.out else folder / "mesh"
    rec_path = rec_dir / f"{kind.lower()}_{lab}.json"
    shells = {}
    if rec_path.is_file():
        try:
            shells = json.loads(rec_path.read_text(encoding="utf-8")).get("shells", {})
        except ValueError:
            shells = {}
    shells[f"{t:g}"] = this_shell
    record["shells"] = shells
    record["shell"] = next((s for s in shells.values() if s["file"] == f"{stem}_shell.obj"), this_shell)
    rec_path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    for name in written:
        print(f"    written {name}  ({(out / name).stat().st_size / 1e6:.2f} MB)")
    for name in unchanged:
        print(f"    unchanged {name} (made again, the same bytes)")
    print("DTMRESULT " + json.dumps({"kind": kind, "source": args.source, "cell": cell, "shell": t,
                                     "units": site["units"], "written": written, "unchanged": unchanged,
                                     "files": {k: v.name for k, v in final.items()}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
