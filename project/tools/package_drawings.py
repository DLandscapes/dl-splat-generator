"""Base drawings of a scene for the package: a plan and four elevations.

    python tools/package_drawings.py <scene name> [--px 3000]

For the student package (pipeline\\requests\\004): orthographic views of the
textured mesh (<name>_mesh.obj + its texture), all at ONE scale so they can be
laid side by side, each with a title line, a scale bar and the origin; the
plan also with the camera walk and the north arrow (or, until north is set,
an arrow that says it is the walk's direction and not north).

    <name> - site data\\drawings\\
        <name>_plan.png (+ .pgw)                   seen from above
        <name>_plan_contours.png (+ .pgw)          the same with the contour lines
        <name>_elevation_seen_from_<side>.png      four sides

The .pgw beside a plan is a world file: it places the picture at its site
coordinates in QGIS and other GIS programs.

WHY THE MESH AND NOT THE SPLAT. A Gaussian splat is made for perspective views
from where the phone was; seen flat from above or from the side it smears. The
mesh is exact geometry with the video's colours painted on -- what one draws on
and measures from.

HOW (numpy + OpenCV, no Blender, so this stays Apache-2.0): each triangle is
projected onto the drawing plane, drawn far-to-near into an ID image (the
nearest one wins), and every pixel then looks up its colour in the texture
through the triangle's texture coordinates -- the same approach as
mesh_texture.py. Pixels the mesh does not cover stay white.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import scene_paths      # noqa: E402
import site_exports     # noqa: E402  -- read_obj, cameras_site

FONT = TOOLS.parent / "static" / "fonts" / "SourceSans3-VariableFont_wght.ttf"
NICE = [0.001, 0.002, 0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5,
        1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 500]
INK = (30, 30, 30)
WALK = (230, 110, 20)
WHITE = (255, 255, 255)
from mesh_texture import LONG_EDGE   # noqa: E402 -- x the median edge: a triangle stretched across a hole
#                                      (one rule for the drawings and the package mesh, 2026-09-27)


def font(size: int, weight: int = 400):
    f = ImageFont.truetype(str(FONT), size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:                                  # noqa: BLE001 -- a static font
        pass
    return f


def ortho(V, UV, F, tex, U, Vv, D, px, bounds):
    """Colour and depth images of the mesh, orthographic.
    U, Vv: the drawing's right and up (unit vectors); D: towards the viewer.
    bounds: (u0, v1, W, H) -- left edge, top edge, size in pixels."""
    u0, v1, W, H = bounds
    pu = (V @ U - u0) / px
    pv = (v1 - V @ Vv) / px
    pd = V @ D
    order = np.argsort(pd[F].mean(axis=1))                   # far first; near overwrite
    ids = np.full((H, W), -1, np.int32)
    P = np.stack([pu, pv], 1)
    for f in order:
        tri = np.round(P[F[f]] * 16).astype(np.int32)
        cv2.fillConvexPoly(ids, tri, int(f), lineType=cv2.LINE_8, shift=4)
    ys, xs = np.nonzero(ids >= 0)
    fid = ids[ys, xs]
    p = np.stack([xs + 0.5, ys + 0.5], 1)
    A, B, C = P[F[fid, 0]], P[F[fid, 1]], P[F[fid, 2]]
    v0, v1_, v2 = B - A, C - A, p - A
    den = v0[:, 0] * v1_[:, 1] - v1_[:, 0] * v0[:, 1]
    den = np.where(np.abs(den) < 1e-12, 1e-12, den)
    l1 = (v2[:, 0] * v1_[:, 1] - v1_[:, 0] * v2[:, 1]) / den
    l2 = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / den
    l0 = 1 - l1 - l2
    uv = l0[:, None] * UV[F[fid, 0]] + l1[:, None] * UV[F[fid, 1]] + l2[:, None] * UV[F[fid, 2]]
    th, tw = tex.shape[:2]
    mu = (uv[:, 0] * tw - 0.5).astype(np.float32)
    mv = ((1 - uv[:, 1]) * th - 0.5).astype(np.float32)
    n = len(mu)
    rows = -(-n // 1024)
    MU = np.zeros(rows * 1024, np.float32); MU[:n] = mu
    MV = np.zeros(rows * 1024, np.float32); MV[:n] = mv
    col = cv2.remap(tex, MU.reshape(rows, 1024), MV.reshape(rows, 1024), cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE).reshape(-1, 3)[:n]
    img = np.full((H, W, 3), 255, np.uint8)
    img[ys, xs] = col[:, ::-1]                              # BGR texture -> RGB
    depth = np.full((H, W), np.nan)
    depth[ys, xs] = l0 * pd[F[fid, 0]] + l1 * pd[F[fid, 1]] + l2 * pd[F[fid, 2]]
    return img, depth


def read_dxf_polylines(path: Path) -> list[tuple[str, np.ndarray]]:
    """(layer, N x 3) for every POLYLINE of our own DXF R12."""
    lines = path.read_text(encoding="ascii").split("\n")
    out, cur, layer, v, invert = [], None, None, {}, False
    for k in range(0, len(lines) - 1, 2):
        code, val = lines[k].strip(), lines[k + 1].strip()
        if code == "0":
            if invert and len(v) == 3:
                cur.append((v["10"], v["20"], v["30"]))
            invert, v = val == "VERTEX", {}
            if val == "POLYLINE":
                cur, layer = [], None
                out.append([None, cur])
            if val == "SEQEND" and out:
                out[-1][0] = layer
        elif code == "8" and cur is not None and layer is None and not invert:
            layer = val
        elif invert and code in ("10", "20", "30"):
            v[code] = float(val)
    return [(lay, np.array(pts)) for lay, pts in out if len(pts) >= 2]


def drawn_mesh(name: str, with_all: bool = False):
    """The package's textured mesh as the drawings show it: (V, UV, F kept,
    texture, what was left out). Shared with package_plan_dxf.py, so the plan
    drawing and the plan DXF's outline cannot disagree.

    Left out is what the mesher invented: triangles the frames did not paint
    (under half their texture, as mesh_texture.py recorded it) and very long ones
    stretched across holes (an edge over LONG_EDGE x the median) -- flat shards,
    not surfaces. On one student scan: 4.2 % of the triangles, 15.8 % of the area; they show
    white, like anything else unseen. The texture's neutral grey is only the
    fallback when no record exists: rock is grey too (on that scan the guess also took
    real rock out)."""
    folder = scene_paths.scene_dir(name)
    sd = scene_paths.site_data_dir(name)
    # since 2026-09-27 the package OBJ holds only the seen triangles; the FULL mesh (the one the
    # per-triangle record belongs to) is in the working folder -- older packages have only the full one
    full = folder / "mesh" / f"{name}_mesh_full.obj"
    V, UV, N, F = site_exports.read_obj(full if full.is_file() else sd / "mesh" / f"{name}_mesh.obj")
    tex = cv2.imread(str(sd / "mesh" / f"{name}_mesh_texture.jpg"), cv2.IMREAD_COLOR)
    edge = np.stack([np.linalg.norm(V[F[:, i]] - V[F[:, (i + 1) % 3]], axis=1)
                     for i in range(3)], 1).max(1)
    rec = folder / "mesh" / "texture_face_painted.npy"
    frac = np.load(rec) if rec.is_file() else None
    if frac is not None and len(frac) == len(F):
        unpainted = frac < 0.5
        how = "less than half of its texture painted from a frame (mesh_texture.py)"
    else:
        c = UV[F].mean(1)
        th, tw = tex.shape[:2]
        at = tex[np.clip(((1 - c[:, 1]) * th).astype(int), 0, th - 1),
                 np.clip((c[:, 0] * tw).astype(int), 0, tw - 1)].astype(int)
        unpainted = np.abs(at - 128).max(1) <= 3
        how = "texture neutral grey (no per-triangle record)"
    keep = ~(unpainted | (edge > LONG_EDGE * np.median(edge)))
    area = 0.5 * np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1)
    left_out = {"triangles": int((~keep).sum()), "share_triangles": round(float((~keep).mean()), 4),
                "share_area": round(float(area[~keep].sum() / area.sum()), 4),
                "rule": f"unpainted ({how}) or longest edge > {LONG_EDGE}x the median"}
    if with_all:
        return V, F, keep
    return V, UV, F[keep], tex, left_out


def seen_and_unseen(name: str):
    """(V, every triangle, which ones were seen) -- the same rule as drawn_mesh, for tools that
    need the left-out triangles too (camera_paths.py: they still block the view)."""
    return drawn_mesh(name, with_all=True)


def nice_bar(width_units: float) -> float:
    target = width_units / 5
    return max([s for s in NICE if s <= target] or [NICE[0]])


def annotate(img: np.ndarray, title: str, sub: str, px: float, unit_label: str,
             origin_px=None, arrow=None) -> Image.Image:
    """Title lines, a scale bar, the origin and (plan) the arrow, on a white band."""
    H, W = img.shape[:2]
    band = max(90, H // 14)
    canvas = Image.new("RGB", (W, H + band), WHITE)
    canvas.paste(Image.fromarray(img), (0, band))
    d = ImageDraw.Draw(canvas)
    s = max(18, W // 90)
    d.text((s, s // 2), title, fill=INK, font=font(int(s * 1.4), 600))
    d.text((s, s // 2 + int(s * 1.8)), sub, fill=INK, font=font(s, 400))
    # scale bar, bottom left, over the drawing
    bar = nice_bar(W * px)
    n = 4
    seg = bar / n / px
    x0, y0 = s * 2, H + band - s * 3
    for i in range(n):
        d.rectangle([x0 + i * seg, y0, x0 + (i + 1) * seg, y0 + s * 0.5],
                    fill=INK if i % 2 == 0 else WHITE, outline=INK, width=2)
    for i in (0, n // 2, n):
        v = bar * i / n
        d.text((x0 + i * seg, y0 - int(s * 1.3)), f"{v:g}", fill=INK, font=font(s, 400), anchor="mm")
    d.text((x0 + n * seg + s, y0 + s * 0.25), unit_label, fill=INK, font=font(s, 400), anchor="lm")
    if origin_px is not None:
        ox, oy = origin_px[0], origin_px[1] + band
        r = s * 0.6
        d.line([ox - r, oy, ox + r, oy], fill=(200, 30, 30), width=3)
        d.line([ox, oy - r, ox, oy + r], fill=(200, 30, 30), width=3)
        d.text((ox + r, oy + r), "0,0", fill=(200, 30, 30), font=font(s, 600))
    if arrow is not None:
        label, is_north = arrow
        ax, ay = W - s * 4, band + s * 5
        L = s * 3
        d.polygon([(ax, ay - L), (ax - s * 0.8, ay + s * 0.2), (ax + s * 0.8, ay + s * 0.2)], fill=INK)
        d.line([ax, ay, ax, ay + L * 0.6], fill=INK, width=3)
        d.text((ax, ay - L - s * 0.6), "N" if is_north else "+Y", fill=INK,
               font=font(int(s * 1.3), 700), anchor="ms")
        if not is_north:
            d.text((ax - s * 1.4, ay + L * 0.9), label, fill=INK, font=font(s, 400), anchor="rs")
    return canvas, band


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--px", type=int, default=3000, help="pixels along the longest side of the scene")
    ap.add_argument("--out", default=None, help="folder (default: the package's site data/drawings/)")
    args = ap.parse_args()

    folder = scene_paths.scene_dir(args.name)
    site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    sd = scene_paths.site_data_dir(args.name)
    out = Path(args.out) if args.out else sd / "drawings"
    north = site["axes"]["y"] == "north"
    metres = site["scale"]["m_per_unit"] is not None
    unit_label = "m" if metres else "scan units - NOT TO SCALE"
    sides = ([("south", 0), ("north", 1), ("west", 2), ("east", 3)] if north else
             [("walk start", 0), ("walk end", 1), ("left of the walk", 2), ("right of the walk", 3)])
    names = {"plan": f"{args.name}_plan.png", "plan_c": f"{args.name}_plan_contours.png"}
    for side, _ in sides:
        names[side] = f"{args.name}_elevation_seen_from_{side.replace(' ', '_')}.png"
    for n in names.values():
        if (out / n).exists():
            print(f"FAILED: {out / n} exists -- never overwritten", file=sys.stderr)
            return 2
    out.mkdir(parents=True, exist_ok=True)

    V, UV, F, tex, left_out = drawn_mesh(args.name)
    cams = site_exports.cameras_site(args.name, np.asarray(site["matrix"], float))
    walk = np.array([c["pos"] for c in cams])
    ext = V.max(axis=0) - V.min(axis=0)
    px = float(max(ext)) / args.px                            # one scale for every drawing
    X, Y, Z = np.eye(3)
    report = {"px": px, "units": site["units"], "left_out": left_out, "views": {}}

    def bounds(U, Vv, extra=np.zeros((0, 3))):
        pts = np.vstack([V, extra])
        pu, pv = pts @ U, pts @ Vv
        m = 0.04 * max(ext)
        u0, v1 = pu.min() - m, pv.max() + m
        return (u0, v1, int(math.ceil((pu.max() + m - u0) / px)), int(math.ceil((v1 - (pv.min() - m)) / px)))

    # ---- plan
    b = bounds(X, Y, walk)
    img, depth = ortho(V, UV, F, tex, X, Y, Z, px, b)
    to_px = lambda P: ((P[:, 0] - b[0]) / px, (b[1] - P[:, 1]) / px)  # noqa: E731
    wx, wy = to_px(walk)
    plan = img.copy()
    cv2.polylines(plan, [np.round(np.stack([wx, wy], 1) * 16).astype(np.int32)], False, WALK,
                  max(2, b[2] // 600), cv2.LINE_AA, shift=4)
    ox, oy = to_px(np.zeros((1, 3)))
    arrow = ("walk direction - north not set", north)
    sub = (f"{args.name} - seen from above.  One scale for every drawing: 1 px = {px:.5g} "
           f"{'m' if metres else 'scan units'}.  " + ("Y is north." if north else
           "North not set: +Y is the walk's direction.") + "  Orange: the camera walk.")
    canvas, band = annotate(plan, f"{args.name}  plan", sub, px, unit_label, (ox[0], oy[0]), arrow)
    canvas.save(out / names["plan"])
    # contours over the plan
    planc = img.copy()
    for layer, pts in read_dxf_polylines(sd / "terrain" / f"{args.name}_contours.dxf"):
        cx, cy = to_px(pts)
        major = layer.endswith("major")
        cv2.polylines(planc, [np.round(np.stack([cx, cy], 1) * 16).astype(np.int32)], False,
                      (120, 40, 20) if major else (190, 110, 60), 3 if major else 1, cv2.LINE_AA, shift=4)
    canvas_c, _ = annotate(planc, f"{args.name}  plan with contours",
                           sub.replace("Orange: the camera walk.", "Contours from the terrain model "
                                       "(every 5th darker)."), px, unit_label, (ox[0], oy[0]), arrow)
    canvas_c.save(out / names["plan_c"])
    for n in (names["plan"], names["plan_c"]):
        # world file: pixel size, rotation terms, then the centre of the top-left pixel
        tl_x = b[0] + px / 2
        tl_y = b[1] + band * px - px / 2
        (out / n).with_suffix(".pgw").write_text(f"{px:.9f}\n0\n0\n{-px:.9f}\n{tl_x:.9f}\n{tl_y:.9f}\n")
    report["views"]["plan"] = {"bounds": b, "band": band, "origin_px": [float(ox[0]), float(oy[0])],
                               "walk_px": np.stack([wx, wy], 1).tolist()}
    # reports go to the scene's working folder only when this run builds the
    # package; a test run (--out) keeps them beside its own output -- found
    # 2026-09-27: test reruns had rewritten the package's reports, and START HERE
    # quoted a texture share (91 %) that was not the package's (88.9 %)
    rep_dir = out if args.out else folder / "mesh"
    np.save(rep_dir / "plan_height.npy", depth)

    # ---- elevations: (name, right, drawing-up = Z, towards the viewer)
    frames = [(-Y, X), (Y, -X), (-X, -Y), (X, Y)]       # seen from -Y, +Y, -X, +X
    for side, k in sides:
        D, U = frames[k]
        b = bounds(U, Z, np.zeros((1, 3)))                     # the origin stays on the sheet
        img, _ = ortho(V, UV, F, tex, U, Z, D, px, b)
        ox = (0 - b[0]) / px
        oy = (b[1] - 0) / px
        look = {0: "+Y", 1: "-Y", 2: "+X", 3: "-X"}[k]
        sub = (f"{args.name} - seen from the {side}, looking {look}; the drawing's up is up.  "
               f"1 px = {px:.5g} {'m' if metres else 'scan units'}, the same as the plan.")
        canvas, _ = annotate(img, f"{args.name}  elevation - seen from the {side}", sub, px, unit_label,
                             (ox, oy), None)
        canvas.save(out / names[side])
        report["views"][side] = {"bounds": b}
    (rep_dir / "drawings.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for n in names.values():
        p = out / n
        print(f"    {n}  {Image.open(p).size[0]} x {Image.open(p).size[1]} px")
    return 0


if __name__ == "__main__":
    sys.exit(main())
