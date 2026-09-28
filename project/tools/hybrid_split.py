"""A hybrid of mesh and Gaussians: the ground as mesh, the rest as splats (version 1).

    python tools/hybrid_split.py <scene name> --out <folder>
           [--ground-cells 1.5] [--smooth-deg 20] [--voxel-cells 2]

Marc's idea (2026-09-27, stage B): keep the textured mesh where the site is bare
ground or a plane surface, and 3D Gaussians everywhere else -- so the splat a
laptop has to draw shrinks, and the mesh carries what a mesh does well.

THE RULE, per mesh triangle -- it stays MESH when all four hold:
    on the ground   its centre lies within --ground-cells terrain cells of the
                    terrain model (the cloth-filter ground, tools/package_terrain.py)
    smooth          its normal is within --smooth-deg of its corners' averaged
                    normals (bare earth, a path, a slab -- not a boulder field)
    seen            at least half of its texture was painted directly from a frame
                    (mesh_texture.py's record)
    not a shard     its longest edge is under 8x the median (package_drawings.py)
Everything else is left to the Gaussians. This is a GEOMETRIC rule: height above
the ground and roughness. It does not detect vegetation (from phone colour alone
that would be speculative -- the standing rule); planting is merely one of the
things that is neither ground nor smooth.

THE SPLATS: a Gaussian is removed when it lies ON a kept mesh surface -- its
centre in a voxel (--voxel-cells terrain cells) that a kept triangle passes
through, or one of the 26 around it -- and it is small (its largest radius under
the voxel size). Everything above the ground, beyond the mesh, and every large,
soft Gaussian (background, atmosphere) stays.

Writes into --out (never into the package while this is an experiment):
    <name>_hybrid_mesh.obj/.mtl  (+ a copy of the texture)   the kept triangles
    <name>_3DGS_hybrid.ply                                  the kept Gaussians
    <name>_hybrid_map.png                                   plan: mesh / Gaussians
    <name>_hybrid.json                                      counts and the rule
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import cv2
import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import ground_dem          # noqa: E402 -- rasterise, fill_holes
import package_drawings as pdw   # noqa: E402 -- LONG_EDGE, ortho
import package_terrain as pt     # noqa: E402 -- read_cloud
import scene_paths         # noqa: E402
import site_exports        # noqa: E402 -- read_obj
import splat_levels as sl  # noqa: E402 -- read_splat, write_splat


def bilinear(grid, x0, y1, cell, x, y):
    ny, nx = grid.shape
    fx = (x - x0) / cell - 0.5
    fy = (y1 - y) / cell - 0.5
    j = np.clip(np.floor(fx).astype(int), 0, nx - 2)
    i = np.clip(np.floor(fy).astype(int), 0, ny - 2)
    tx, ty = np.clip(fx - j, 0, 1), np.clip(fy - i, 0, 1)
    return (grid[i, j] * (1 - tx) * (1 - ty) + grid[i, j + 1] * tx * (1 - ty)
            + grid[i + 1, j] * (1 - tx) * ty + grid[i + 1, j + 1] * tx * ty)


def voxel_keys(P, size, origin):
    k = np.floor((P - origin) / size).astype(np.int64)
    return k[:, 0] * 73856093 ^ k[:, 1] * 19349663 ^ k[:, 2] * 83492791, k


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ground-cells", type=float, default=1.5)
    ap.add_argument("--smooth-deg", type=float, default=20.0)
    ap.add_argument("--voxel-cells", type=float, default=2.0)
    ap.add_argument("--block-cells", type=int, default=2, help="decision block, in terrain cells")
    ap.add_argument("--rough", type=float, default=0.3,
                    help="max spread of the ground points in a block, in terrain cells")
    ap.add_argument("--above", type=float, default=0.1,
                    help="max share of a block's points standing above the ground")
    ap.add_argument("--min-region", type=int, default=12, help="smallest mesh region kept, in blocks")
    args = ap.parse_args()
    t0 = time.time()
    n = args.name
    folder = scene_paths.scene_dir(n)
    sd = scene_paths.site_data_dir(n)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in (f"{n}_hybrid_mesh.obj", f"{n}_3DGS_hybrid.ply"):
        if (out / f).exists():
            print(f"FAILED: {out / f} exists -- never overwritten", file=sys.stderr)
            return 2

    # ---- the mesh and its features
    full = folder / "mesh" / f"{n}_mesh_full.obj"     # the mesh the per-triangle record belongs to
    V, UV, N, F = site_exports.read_obj(full if full.is_file() else sd / "mesh" / f"{n}_mesh.obj")
    ter = json.loads((folder / "mesh" / "terrain.json").read_text(encoding="utf-8"))
    cell = ter["grid"]["cell"]
    G, _, _ = pt.read_cloud(sd / "terrain" / f"{n}_ground.ply")
    grid, (gx0, gy1, gnx, gny, _) = ground_dem.rasterise(G, cell, 2)
    grid, _ = ground_dem.fill_holes(grid, 4)

    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    cent = (a + b + c) / 3
    fn = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(fn, axis=1)
    fn = fn / (np.linalg.norm(fn, axis=1, keepdims=True) + 1e-20)
    vn = N[F].mean(axis=1)
    vn = vn / (np.linalg.norm(vn, axis=1, keepdims=True) + 1e-20)
    turn = np.degrees(np.arccos(np.clip(np.abs(np.sum(fn * vn, axis=1)), -1, 1)))
    zg = bilinear(grid, gx0, gy1, cell, cent[:, 0], cent[:, 1])
    on_ground = np.isfinite(zg) & (np.abs(cent[:, 2] - zg) <= args.ground_cells * cell)
    smooth = turn <= args.smooth_deg
    rec = folder / "mesh" / "texture_face_painted.npy"
    painted = np.load(rec) >= 0.5 if rec.is_file() else np.ones(len(F), bool)
    edge = np.stack([np.linalg.norm(V[F[:, i]] - V[F[:, (i + 1) % 3]], axis=1) for i in range(3)], 1).max(1)
    not_shard = edge <= pdw.LONG_EDGE * np.median(edge)
    # VERSION 2 (2026-09-27): decided by AREA, not per triangle. Version 1 asked each
    # triangle whether it was smooth, and the scanned mesh is noisy: the map came out
    # salt-and-pepper, every speck a seam between mesh and splat. Now the terrain grid
    # is cut into blocks of --block-cells; a block is MESH when enough ground was seen
    # in it, its ground points lie close to the terrain surface (spread <= --rough
    # cells) and few points stand above the ground (share <= --above); then small gaps
    # are closed and islands under --min-region blocks dropped, so the mesh comes in
    # contiguous patches. On one student scan the spread's median is 0.25 cells and the standing
    # share is 0 in over half the blocks, 0.27+ in a quarter -- the defaults sit there.
    P_all, _, _ = pt.read_cloud(sd / "point cloud" / f"{n}_points.ply")
    B = args.block_cells
    bs = cell * B
    bx, by = int(np.ceil(gnx / B)) + 1, int(np.ceil(gny / B)) + 1

    def block_index(Q):
        i = np.floor((Q[:, 0] - gx0) / bs).astype(int)
        j = np.floor((gy1 - Q[:, 1]) / bs).astype(int)
        ok = (i >= 0) & (j >= 0) & (i < bx) & (j < by)
        return j * bx + i, ok

    gz_pts = bilinear(grid, gx0, gy1, cell, G[:, 0], G[:, 1])
    kg, okg = block_index(G)
    okg &= np.isfinite(gz_pts)
    res = G[okg, 2] - gz_pts[okg]
    n_g = np.bincount(kg[okg], minlength=bx * by)
    m1 = np.bincount(kg[okg], res, bx * by) / np.maximum(n_g, 1)
    m2 = np.bincount(kg[okg], res ** 2, bx * by) / np.maximum(n_g, 1)
    spread = np.sqrt(np.maximum(m2 - m1 ** 2, 0)) / cell
    pz = bilinear(grid, gx0, gy1, cell, P_all[:, 0], P_all[:, 1])
    kp, okp = block_index(P_all)
    okp &= np.isfinite(pz)
    stands = (P_all[okp, 2] - pz[okp]) > args.ground_cells * cell
    n_p = np.bincount(kp[okp], minlength=bx * by)
    share_up = np.bincount(kp[okp], stands.astype(float), bx * by) / np.maximum(n_p, 1)
    blk = ((n_g >= 8) & (spread <= args.rough) & (share_up <= args.above)).reshape(by, bx).astype(np.uint8)
    blk = cv2.morphologyEx(blk, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    nlab, lab, stats, _ = cv2.connectedComponentsWithStats(blk, connectivity=8)
    big = np.zeros(nlab, bool)
    big[1:] = stats[1:, cv2.CC_STAT_AREA] >= args.min_region
    blk = big[lab]
    kc, okc = block_index(cent)
    in_mesh_block = np.zeros(len(F), bool)
    in_mesh_block[okc] = blk.reshape(-1)[kc[okc]]
    keep_mesh = in_mesh_block & on_ground & painted & not_shard

    # ---- the Gaussians
    data, _ = sl.read_splat(sd / "splat" / f"{n}_3DGS_high.ply")
    Pg = np.stack([data["x"], data["y"], data["z"]], 1).astype(np.float64)
    radius = 3 * np.exp(np.stack([data[f"scale_{i}"] for i in range(3)], 1).max(1))   # 3 sigma
    vox = args.voxel_cells * cell
    origin = np.minimum(Pg.min(0), V.min(0)) - vox
    # sample the kept surfaces densely enough that every voxel they cross gets a sample
    ka, kb, kc = a[keep_mesh], b[keep_mesh], c[keep_mesh]
    karea = area[keep_mesh]
    n_samp = int(min(20_000_000, max(1, karea.sum() / (vox / 3) ** 2)))
    rng = np.random.default_rng(3)
    idx = rng.choice(len(karea), n_samp, p=karea / karea.sum())
    r1, r2 = rng.random(n_samp), rng.random(n_samp)
    s = np.sqrt(r1)
    S = (1 - s)[:, None] * ka[idx] + (s * (1 - r2))[:, None] * kb[idx] + (s * r2)[:, None] * kc[idx]
    surf, sk = voxel_keys(S, vox, origin)
    occupied = set()
    uk = np.unique(sk, axis=0)
    for dx in (-1, 0, 1):                                  # the voxel and its 26 neighbours
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                kk = uk + np.array([dx, dy, dz])
                occupied.update((kk[:, 0] * 73856093 ^ kk[:, 1] * 19349663 ^ kk[:, 2] * 83492791).tolist())
    gkey, _ = voxel_keys(Pg, vox, origin)
    on_surface = np.fromiter((k in occupied for k in gkey.tolist()), bool, len(gkey))
    drop = on_surface & (radius < vox)
    keep_g = ~drop

    # ---- write
    mtl = f"{n}_hybrid_mesh.mtl"
    tex_name = f"{n}_mesh_texture.jpg"
    shutil.copy2(sd / "mesh" / tex_name, out / tex_name)
    (out / mtl).write_text(f"newmtl {n}_hybrid_mesh\nKa 1 1 1\nKd 1 1 1\nKs 0 0 0\nd 1\nillum 1\nmap_Kd {tex_name}\n",
                           encoding="utf-8")
    Fk = F[keep_mesh]
    used = np.unique(Fk)
    remap = -np.ones(len(V), np.int64)
    remap[used] = np.arange(len(used))
    with (out / f"{n}_hybrid_mesh.obj").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# {n} hybrid mesh (version 1): the triangles kept as mesh -- DL-SplatGenerator tools/hybrid_split.py\n"
                 f"# site coordinates, Z up; import with Up = Z, Forward = Y\nmtllib {mtl}\no {n}_hybrid_mesh\n")
        fh.write("".join(f"v {x:.6f} {y:.6f} {z:.6f}\n" for x, y, z in V[used]))
        fh.write("".join(f"vt {u:.6f} {v:.6f}\n" for u, v in UV[used]))
        fh.write("".join(f"vn {x:.5f} {y:.5f} {z:.5f}\n" for x, y, z in N[used]))
        fh.write(f"usemtl {n}_hybrid_mesh\n")
        fh.write("".join(f"f {p+1}/{p+1}/{p+1} {q+1}/{q+1}/{q+1} {r+1}/{r+1}/{r+1}\n" for p, q, r in remap[Fk]))
    names = data.dtype.names
    k = sl.sh_rest_count(names)
    sub = data[keep_g]
    sl.write_splat(out / f"{n}_3DGS_hybrid.ply",
                   np.stack([sub["x"], sub["y"], sub["z"]], 1), np.stack([sub[f"f_dc_{i}"] for i in range(3)], 1),
                   np.stack([sub[f"f_rest_{i}"] for i in range(k)], 1), np.asarray(sub["opacity"]),
                   np.stack([sub[f"scale_{i}"] for i in range(3)], 1), np.stack([sub[f"rot_{i}"] for i in range(4)], 1),
                   comments=["Vertical axis: z", "SH degree: 3",
                             f"{n} hybrid (version 1): Gaussians not lying on a kept mesh surface"])

    # ---- the map: plan, mesh kept (grey-blue) / left to Gaussians (orange)
    rep = json.loads((folder / "mesh" / "drawings.json").read_text(encoding="utf-8"))
    px = rep["px"]
    ext = V.max(0) - V.min(0)
    m = 0.04 * max(ext)
    u0, v1 = V[:, 0].min() - m, V[:, 1].max() + m
    W = int(np.ceil((V[:, 0].max() + m - u0) / px))
    H = int(np.ceil((v1 - (V[:, 1].min() - m)) / px))
    img = np.full((H, W, 3), 255, np.uint8)
    order = np.argsort(cent[:, 2])
    P2 = np.stack([(V[:, 0] - u0) / px, (v1 - V[:, 1]) / px], 1)
    for f in order:
        colour = (190, 150, 110) if keep_mesh[f] else (60, 140, 235)          # BGR
        cv2.fillConvexPoly(img, np.round(P2[F[f]] * 16).astype(np.int32), colour, lineType=cv2.LINE_8, shift=4)
    cv2.imwrite(str(out / f"{n}_hybrid_map.png"), img)

    total_area = float(area.sum())
    report = {
        "scene": n, "version": 2,
        "rule": {"ground_cells": args.ground_cells, "block_cells": args.block_cells, "rough": args.rough,
                 "above": args.above, "min_region": args.min_region, "voxel_cells": args.voxel_cells,
                 "mesh_blocks": int(blk.sum()), "regions": int(big.sum()),
                 "cell": cell, "voxel": vox},
        "mesh": {"triangles": int(len(F)), "kept": int(keep_mesh.sum()),
                 "kept_share_area": round(float(area[keep_mesh].sum() / total_area), 4),
                 "failed": {"not on the ground": int((~on_ground).sum()), "not in a mesh block": int((~in_mesh_block).sum()),
                            "not seen": int((~painted).sum()), "shard": int((~not_shard).sum())}},
        "gaussians": {"total": int(len(Pg)), "kept": int(keep_g.sum()), "removed": int(drop.sum()),
                      "removed_share": round(float(drop.mean()), 4),
                      "on_surface_but_large_kept": int((on_surface & ~drop).sum())},
        "files_mb": {p.name: round(p.stat().st_size / 1e6, 1) for p in sorted(out.glob(f"{n}_*"))},
        "seconds": round(time.time() - t0, 1)}
    (out / f"{n}_hybrid.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("mesh", "gaussians", "files_mb", "seconds")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
