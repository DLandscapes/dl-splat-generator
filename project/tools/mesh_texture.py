"""A scene's mesh as a textured OBJ (+ MTL + JPG texture), in site coordinates.

    python tools/mesh_texture.py <scene name> [--out <folder>] [--size 4096]

For the student package (pipeline\\requests\\004), item 1: the triangulated
mesh as .obj + .mtl + a texture, so it imports into Blender, Rhino, SketchUp ...
It lines up with every other file of the package because it is written in
site.json's frame (tools/site_frame.py: Z up, Y north or the walk, origin on
the ground, metres when scaled) -- Z up, as Rhino and Blender's "Z up" import
expect.

HOW THE TEXTURE IS MADE (no GPU, no Blender -- numpy, OpenCV, xatlas):
  1. UNFOLD   xatlas cuts the mesh into charts and lays them out flat (the UVs).
  2. SEE      every frame the splat was trained on (the undistorted video frames
              and their solved cameras) gets a depth image of the mesh -- points
              sampled on the surface, projected, nearest kept -- so a triangle
              hidden behind another from that frame is known to be hidden.
  3. CHOOSE   for each triangle, the THREE frames that see it best: visible,
              inside the picture, most face-on and closest (score = cos(angle)
              x (focal / distance)^2, the size it has in the picture).
  4. PAINT    each texture pixel -> its 3D point -> sampled in those frames,
              blended by score (squared, so the best view dominates). A pixel
              no frame sees stays neutral grey and is counted.
  5. PAD      colours are grown 8 pixels past every chart edge, so the texture
              filtering of a viewer never pulls in the black between charts.

What it cannot do: the mesh is only as good as the dense cloud (surfaces the
camera saw; see mesh.json's caveats), and frames were exposed separately by the
phone, so faint seams between triangles painted from different frames remain.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import colmap_cameras   # noqa: E402
import scene_paths      # noqa: E402

DEPTH_DOWN = 4          # depth images at 1/4 of the frame size
DEPTH_TOL = 0.03        # visible if no further than 3 % behind the nearest surface
BORDER = 0.02           # ignore the outer 2 % of every frame (lens edge, motion blur)
TOP_K = 3
PAD = 8
GREY = (128, 128, 128)
LONG_EDGE = 8           # x the median edge: a triangle stretched across a hole (package_drawings uses this)


def read_mesh_ply(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Binary PLY with float x y z and triangle faces (list uchar int)."""
    with path.open("rb") as fh:
        head = b""
        while b"end_header" not in head:
            head += fh.readline()
        text = head.decode("ascii")
        nv = int(text.split("element vertex ")[1].split()[0])
        nf = int(text.split("element face ")[1].split()[0])
        props = [l.split()[-1] for l in text.split("element vertex")[1].split("element face")[0]
                 .splitlines() if l.startswith("property")]
        verts = np.frombuffer(fh.read(4 * len(props) * nv), "<f4").reshape(nv, len(props))
        faces = np.frombuffer(fh.read(nf * 13), dtype=np.dtype([("n", "u1"), ("i", "<i4", 3)]))
    if not (faces["n"] == 3).all():
        raise ValueError("only triangle meshes are supported")
    return verts[:, :3].astype(np.float64), faces["i"].astype(np.int64)


def load_cameras(model: Path) -> list[dict]:
    cams = colmap_cameras.read_cameras_bin(model / "cameras.bin")
    out = []
    for im in sorted(colmap_cameras.read_images_bin(model / "images.bin"), key=lambda r: r["name"]):
        c = cams[im["camera_id"]]
        if c["model"] != "PINHOLE":
            raise ValueError(f"expected undistorted PINHOLE cameras, got {c['model']}")
        fx, fy, cx, cy = c["params"]
        out.append({"name": im["name"], "R": np.asarray(colmap_cameras.quat_to_matrix(*im["q"]), float),
                    "t": np.asarray(im["t"], float), "fx": fx, "fy": fy, "cx": cx, "cy": cy,
                    "W": c["width"], "H": c["height"]})
    return out


def project(cam: dict, P: np.ndarray):
    Pc = P @ cam["R"].T + cam["t"]
    z = Pc[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        u = cam["fx"] * Pc[:, 0] / z + cam["cx"]
        v = cam["fy"] * Pc[:, 1] / z + cam["cy"]
    return u, v, z


def surface_samples(V: np.ndarray, F: np.ndarray, n: int, rng) -> np.ndarray:
    """n points spread over the surface, proportional to triangle area."""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    idx = rng.choice(len(F), size=n, p=area / area.sum())
    r1, r2 = rng.random(n), rng.random(n)
    s = np.sqrt(r1)
    return (1 - s)[:, None] * a[idx] + (s * (1 - r2))[:, None] * b[idx] + (s * r2)[:, None] * c[idx]


def depth_image(cam: dict, pts: np.ndarray) -> np.ndarray:
    h, w = cam["H"] // DEPTH_DOWN, cam["W"] // DEPTH_DOWN
    u, v, z = project(cam, pts)
    ok = (z > 0) & (u >= 0) & (v >= 0) & (u < cam["W"]) & (v < cam["H"])
    iu = np.minimum((u[ok] / DEPTH_DOWN).astype(np.int64), w - 1)
    iv = np.minimum((v[ok] / DEPTH_DOWN).astype(np.int64), h - 1)
    zb = np.full(h * w, np.inf)
    np.minimum.at(zb, iv * w + iu, z[ok])
    zb = zb.reshape(h, w).astype(np.float32)
    # close pin-holes between samples: the nearest surface in a 3x3 neighbourhood
    return cv2.erode(zb, np.ones((3, 3), np.uint8))


def visible(cam: dict, zb: np.ndarray, P: np.ndarray):
    u, v, z = project(cam, P)
    mx, my = BORDER * cam["W"], BORDER * cam["H"]
    ins = (z > 0) & (u >= mx) & (v >= my) & (u < cam["W"] - mx) & (v < cam["H"] - my)
    vis = np.zeros(len(P), bool)
    iu = np.clip((u / DEPTH_DOWN).astype(np.int64), 0, zb.shape[1] - 1)
    iv = np.clip((v / DEPTH_DOWN).astype(np.int64), 0, zb.shape[0] - 1)
    zz = zb[iv, iu]
    vis[ins] = z[ins] <= zz[ins] * (1 + DEPTH_TOL)
    return vis, u, v, z


def sample(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Bilinear colours at (u, v). cv2.remap takes maps of at most 32767 rows,
    so the points go in as blocks 1024 wide."""
    n = len(u)
    rows = -(-n // 1024)
    mu = np.zeros(rows * 1024, np.float32)
    mv = np.zeros(rows * 1024, np.float32)
    mu[:n], mv[:n] = u, v
    out = cv2.remap(img, mu.reshape(rows, 1024), mv.reshape(rows, 1024), cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE)
    return out.reshape(-1, 3)[:n].astype(np.float64)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--out", default=None,
                    help="folder (default: <scene>\\<name>\\<name> - site data\\mesh\\)")
    ap.add_argument("--size", type=int, default=4096, help="texture size in pixels (square)")
    ap.add_argument("--samples", type=int, default=3_000_000,
                    help="surface points for the depth images")
    args = ap.parse_args()
    t0 = time.time()
    rng = np.random.default_rng(1)

    folder = scene_paths.scene_dir(args.name)
    site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else scene_paths.site_data_dir(args.name) / "mesh"
    stem = f"{args.name}_mesh"
    for ext in (".obj", ".mtl", "_texture.jpg"):
        if (out / f"{stem}{ext}").exists():
            print(f"FAILED: {out / (stem + ext)} exists -- never overwritten", file=sys.stderr)
            return 2
    out.mkdir(parents=True, exist_ok=True)

    V, F = read_mesh_ply(folder / "mesh" / "mesh.ply")
    und = scene_paths.ROOT / "work" / args.name / "undistorted"
    cams = load_cameras(und / "sparse" / "0")
    print(f"    mesh {len(V):,} vertices, {len(F):,} triangles; {len(cams)} frames")

    # 1. UNFOLD
    # xatlas takes minutes; its result is kept with the other intermediates in
    # work/<name>/ and reused while the mesh is unchanged
    cache = scene_paths.ROOT / "work" / args.name / "texture_uv.npz"
    mesh_mtime = (folder / "mesh" / "mesh.ply").stat().st_mtime
    if cache.is_file() and float(np.load(cache)["mesh_mtime"]) == mesh_mtime:
        z = np.load(cache)
        vmap, Fuv, UV = z["vmap"], z["Fuv"], z["UV"]
        print(f"    unfolded: {len(UV):,} texture vertices (reused {cache.name})")
    else:
        import xatlas
        vmap, Fuv, UV = xatlas.parametrize(V.astype(np.float32), F.astype(np.uint32))
        np.savez(cache, vmap=vmap, Fuv=Fuv, UV=UV, mesh_mtime=mesh_mtime)
        print(f"    unfolded: {len(UV):,} texture vertices ({time.time() - t0:.0f} s)")

    # 2. SEE -- depth images
    pts = surface_samples(V, F, args.samples, rng)
    depth = [depth_image(c, pts) for c in cams]
    print(f"    depth images: {len(depth)} ({time.time() - t0:.0f} s)")

    # 3. CHOOSE -- best frames per triangle
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    cent = (a + b + c) / 3
    nrm = np.cross(b - a, c - a)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-20
    score = np.zeros((len(F), len(cams)), np.float32)
    for k, cam in enumerate(cams):
        vis, _, _, z = visible(cam, depth[k], cent)
        centre = -cam["R"].T @ cam["t"]
        view = centre - cent
        dist = np.linalg.norm(view, axis=1)
        cosang = np.abs(np.sum(view * nrm, axis=1)) / (dist + 1e-20)
        s = cosang * (cam["fx"] / np.maximum(z, 1e-9)) ** 2
        score[:, k] = np.where(vis & (cosang > 0.1), s, 0)
    best = np.argsort(-score, axis=1)[:, :TOP_K]
    best_s = np.take_along_axis(score, best, axis=1)
    seen = best_s[:, 0] > 0
    print(f"    triangles seen by at least one frame: {seen.mean():.1%} ({time.time() - t0:.0f} s)")

    # 4. PAINT -- rasterise triangle ids into the texture, then sample
    S = args.size
    tri = np.full((S, S), -1, np.int32)
    uvpx = np.stack([UV[:, 0] * S, (1 - UV[:, 1]) * S], 1)          # image rows go down
    for i, f in enumerate(Fuv):
        poly = np.round(uvpx[f] * 16).astype(np.int32)              # 4 bits of sub-pixel
        cv2.fillConvexPoly(tri, poly, int(i), lineType=cv2.LINE_8, shift=4)
    ys, xs = np.nonzero(tri >= 0)
    fid = tri[ys, xs]
    # barycentrics of each texel centre in its triangle, in UV space
    p = np.stack([xs + 0.5, ys + 0.5], 1)
    A, B, C = uvpx[Fuv[fid, 0]], uvpx[Fuv[fid, 1]], uvpx[Fuv[fid, 2]]
    v0, v1, v2 = B - A, C - A, p - A
    den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    den = np.where(np.abs(den) < 1e-12, 1e-12, den)
    l1 = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / den
    l2 = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / den
    l0 = 1 - l1 - l2
    Vuv = V[vmap]                                                    # 3D of each UV vertex
    P3 = (l0[:, None] * Vuv[Fuv[fid, 0]] + l1[:, None] * Vuv[Fuv[fid, 1]]
          + l2[:, None] * Vuv[Fuv[fid, 2]])
    print(f"    texels inside charts: {len(fid):,} of {S * S:,} ({time.time() - t0:.0f} s)")

    acc = np.zeros((len(fid), 3), np.float64)
    wsum = np.zeros(len(fid), np.float64)
    # FALLBACK: a texel that fails the per-texel visibility test in all of its
    # triangle's frames, although the triangle itself was seen, takes its colour
    # from the triangle's best frame anyway. The per-texel test is strict (a 1/4-
    # size depth image, 3 % tolerance) and small bumps hide their own neighbours;
    # left grey, those texels made confetti holes in the drawings (2026-09-27).
    fb = np.zeros((len(fid), 3), np.float64)
    fb_ok = np.zeros(len(fid), bool)
    for k, cam in enumerate(cams):
        use = np.zeros(len(fid), bool)
        w = np.zeros(len(fid))
        for j in range(TOP_K):
            m = (best[fid, j] == k) & (best_s[fid, j] > 0)
            use |= m
            w[m] = best_s[fid[m], j] ** 2
        first = (best[fid, 0] == k) & (best_s[fid, 0] > 0)
        if not (use.any() or first.any()):
            continue
        img = cv2.imread(str(und / "images" / cam["name"]), cv2.IMREAD_COLOR)
        idx = np.nonzero(use)[0]
        vis, u, v, _ = visible(cam, depth[k], P3[idx])
        idx, u, v = idx[vis], u[vis], v[vis]
        if len(idx):
            # COLMAP puts a pixel's centre at .5, cv2.remap at the whole number: sample half a pixel
            # back. Measured on a student scan's 10 held-out frames: +0.037 SSIM in 10 of 10 (2026-09-27,
            # test outputs\2026-09-27\texture step 1); until then every texture sat half a pixel off
            col = sample(img, u - 0.5, v - 0.5)
            acc[idx] += col * w[idx, None]
            wsum[idx] += w[idx]
        fidx = np.nonzero(first)[0]
        if len(fidx):
            fu, fv, fz = project(cam, P3[fidx])
            inside = (fz > 0) & (fu >= 0) & (fv >= 0) & (fu < cam["W"]) & (fv < cam["H"])
            fidx, fu, fv = fidx[inside], fu[inside], fv[inside]
            fb[fidx] = sample(img, fu - 0.5, fv - 0.5)          # the same half pixel
            fb_ok[fidx] = True
    direct = wsum > 0
    use_fb = ~direct & fb_ok
    acc[use_fb], wsum[use_fb] = fb[use_fb], 1.0
    painted = wsum > 0
    print(f"    texels painted from frames: {direct.mean():.1%} directly, "
          f"{use_fb.mean():.1%} more from their triangle's best frame")
    # per triangle: the share of its texels painted from a frame. Kept with the
    # working files, for anything that must tell a real surface from a shard the
    # mesher stretched across a hole (the drawings) -- the texture's grey alone
    # cannot, because rock is grey too.
    n_tex = np.bincount(fid, minlength=len(Fuv))
    # DIRECT painting only (a frame that saw that texel), not the fallback: the
    # record answers "what did the phone see", and must not change when the
    # fallback does (2026-09-27: it did, and the drawings then disagreed with
    # the package's texture by 160 triangles)
    face_painted = np.bincount(fid, weights=direct.astype(float), minlength=len(Fuv)) / np.maximum(n_tex, 1)
    # reports go to the scene's working folder only when this run builds the
    # package; a test run (--out) keeps them beside its own output -- found
    # 2026-09-27: test reruns had rewritten the package's reports, and START HERE
    # quoted a texture share (91 %) that was not the package's (88.9 %)
    rep_dir = out if args.out else folder / "mesh"
    np.save(rep_dir / "texture_face_painted.npy", np.where(n_tex > 0, face_painted, 0.0))
    tex = np.zeros((S, S, 3), np.uint8)
    colour = np.where(painted[:, None], acc / np.maximum(wsum, 1e-20)[:, None], GREY)
    tex[ys, xs] = np.clip(colour, 0, 255).astype(np.uint8)
    print(f"    texels painted from frames: {painted.mean():.1%} ({time.time() - t0:.0f} s)")

    # 5. PAD
    mask = (tri >= 0).astype(np.uint8)
    for _ in range(PAD):
        grown = cv2.dilate(tex, np.ones((3, 3), np.uint8))
        ring = cv2.dilate(mask, np.ones((3, 3), np.uint8)) & (1 - mask)
        tex[ring > 0] = grown[ring > 0]
        mask |= ring
    cv2.imwrite(str(out / f"{stem}_texture.jpg"), tex, [cv2.IMWRITE_JPEG_QUALITY, 92])

    # the OBJ, in site coordinates
    M = np.asarray(site["matrix"], float)
    Vs = V @ M[:3, :3].T + M[:3, 3]
    Vsu = Vs[vmap]
    fn = np.cross(Vs[F[:, 1]] - Vs[F[:, 0]], Vs[F[:, 2]] - Vs[F[:, 0]])
    vn = np.zeros_like(Vs)
    for j in range(3):
        np.add.at(vn, F[:, j], fn)
    vn /= np.linalg.norm(vn, axis=1, keepdims=True) + 1e-20
    vnu = vn[vmap]
    units = site["units"]
    # THE PACKAGE'S MESH SHOWS WHAT WAS SEEN (Marc, 2026-09-27: "seen parts only"). The mesher closes
    # every gap with triangles, and on a short walk most of them span space no frame saw -- one student scan: 49 %
    # of the area in long grey shards, 66 % less than half painted. The package OBJ leaves out exactly
    # what the drawings leave out (package_drawings.drawn_mesh: under half painted from a frame, or
    # the longest edge over LONG_EDGE x the median), so it shows holes where nothing was filmed. The
    # FULL mesh is kept for the tools that need every triangle (drawings, plan DXF, terrain models,
    # camera paths -- whose coverage counts unseen triangles as blocking the view): working
    # mesh\<name>_mesh_full.obj, same vertices, UVs and triangle order as the record above.
    edge = np.stack([np.linalg.norm(V[F[:, i]] - V[F[:, (i + 1) % 3]], axis=1) for i in range(3)], 1).max(1)
    frac = np.where(n_tex > 0, face_painted, 0.0)
    keep = ~((frac < 0.5) | (edge > LONG_EDGE * np.median(edge)))
    area = 0.5 * np.linalg.norm(fn, axis=1)
    left_out = {"triangles": int((~keep).sum()), "share_triangles": round(float((~keep).mean()), 4),
                "share_area": round(float(area[~keep].sum() / max(area.sum(), 1e-20)), 4),
                "rule": f"under half painted from a frame, or longest edge > {LONG_EDGE}x the median "
                        "(the drawings' rule)"}
    print(f"    left out of the package mesh (never seen or stretched across holes): "
          f"{left_out['share_area']:.1%} of the area, {left_out['triangles']:,} triangles")

    def write_obj(path: Path, faces: np.ndarray, note: str, with_mtl: bool) -> None:
        used = np.unique(faces)                               # only the vertices these faces use
        remap = np.full(len(Vsu), -1, np.int64)
        remap[used] = np.arange(len(used))
        fr = remap[faces]
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"# {args.name} -- {note}, DL-SplatGenerator tools/mesh_texture.py (Digital Landscapes)\n"
                     f"# coordinates: site.json -- Z up, Y {site['axes']['y']}, X {site['axes']['x']}, "
                     f"origin on the ground below the first camera; units: {units}"
                     f"{'' if site['scale']['m_per_unit'] else ' (NOT TO SCALE)'}\n"
                     f"# import with Up = Z, Forward = Y\n"
                     + (f"mtllib {stem}.mtl\n" if with_mtl else "") + f"o {stem}\n")
            fh.write("".join(f"v {x:.6f} {y:.6f} {z:.6f}\n" for x, y, z in Vsu[used]))
            fh.write("".join(f"vt {s:.6f} {t:.6f}\n" for s, t in UV[used]))
            fh.write("".join(f"vn {x:.5f} {y:.5f} {z:.5f}\n" for x, y, z in vnu[used]))
            if with_mtl:
                fh.write(f"usemtl {stem}\n")
            fh.write("".join(f"f {a+1}/{a+1}/{a+1} {b+1}/{b+1}/{b+1} {c+1}/{c+1}/{c+1}\n"
                             for a, b, c in fr))

    with (out / f"{stem}.mtl").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# {args.name} -- DL-SplatGenerator tools/mesh_texture.py (Digital Landscapes)\n"
                 f"newmtl {stem}\nKa 1 1 1\nKd 1 1 1\nKs 0 0 0\nd 1\nillum 1\n"
                 f"map_Kd {stem}_texture.jpg\n")
    write_obj(out / f"{stem}.obj", Fuv[keep], "textured mesh, the parts the video saw", True)
    write_obj(rep_dir / f"{stem}_full.obj", Fuv, "the FULL mesh for the tools (not in the package)", False)
    report = {"scene": args.name, "triangles": int(len(F)), "texture_px": S,
              "triangles_seen": round(float(seen.mean()), 4),
              "texels_in_charts": int(len(fid)), "texels_painted": round(float(painted.mean()), 4),
              "texels_painted_directly": round(float(direct.mean()), 4),
              "texels_painted_by_fallback": round(float(use_fb.mean()), 4),
              "package_mesh": {"triangles": int(keep.sum()), "left_out": left_out,
                               "full_mesh": f"working mesh\\{stem}_full.obj"},
              "frames": len(cams), "units": units, "seconds": round(time.time() - t0, 1)}
    # the build report is for us, not for the student: it stays with the scene's
    # working files (mesh/texture.json), not in the package
    (rep_dir / "texture.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for p in sorted(out.glob(f"{stem}*")):
        print(f"    {p.name}  {p.stat().st_size / 1e6:.1f} MB")
    print(f"    done in {time.time() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
