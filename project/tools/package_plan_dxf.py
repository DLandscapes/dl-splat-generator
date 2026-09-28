"""The plan as a DXF: camera path, camera positions, contours, the patch's outline.

    python tools/package_plan_dxf.py <scene name> [--out <file>]

Marc (2026-09-27): "in the drawings folder -> a dxf export for the camera path
with points of the according camera positions, as well as of the contour lines,
and the outline of the patch".

    <name> - site data\\drawings\\<name>_plan.dxf       (DXF R12, site coordinates)

Layers:
    <name> camera path         3D polyline through every placed camera, in order
    <name> cameras             a point at each camera; frame numbers every 10th + the last
    <name> camera directions   a short line per camera: where it looked
    <name> contours minor/major  the terrain's contour lines at their height
    <name> outline             the boundary of the scanned patch, as the plan drawing shows it
    <name> not seen            holes inside it larger than 0.2 % of the patch
    <name> notes               the origin, the units, what is not set

The outline is traced from EXACTLY what the plan drawing shows -- the same
triangles (package_drawings.drawn_mesh) at the same scale -- so drawing and DXF
agree; it lies at z = 0 (a plan outline, not a 3D edge). Contours are copied
unchanged from <name>_contours.dxf (the terrain step).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import package_drawings as pdw   # noqa: E402 -- drawn_mesh, ortho, read_dxf_polylines
import scene_paths               # noqa: E402
import site_exports              # noqa: E402 -- cameras_site
from dxf_r12 import Dxf          # noqa: E402

HOLE_SHARE = 0.002               # holes smaller than this share of the patch are not drawn
SIMPLIFY_PX = 1.5                # outline simplification (Douglas-Peucker), drawing pixels


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    folder = scene_paths.scene_dir(args.name)
    site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    sd = scene_paths.site_data_dir(args.name)
    out = Path(args.out) if args.out else sd / "drawings" / f"{args.name}_plan.dxf"
    if out.exists():
        print(f"FAILED: {out} exists -- never overwritten", file=sys.stderr)
        return 2
    rep = json.loads((folder / "mesh" / "drawings.json").read_text(encoding="utf-8"))
    px = rep["px"]
    metres = site["scale"]["m_per_unit"] is not None
    north = site["axes"]["y"] == "north"
    n = args.name

    V, UV, F, tex, left_out = pdw.drawn_mesh(n)
    X, Y, Z = np.eye(3)
    ext = V.max(axis=0) - V.min(axis=0)
    m = 0.04 * max(ext)
    u0, v1 = V[:, 0].min() - m, V[:, 1].max() + m
    W = int(np.ceil((V[:, 0].max() + m - u0) / px))
    H = int(np.ceil((v1 - (V[:, 1].min() - m)) / px))
    _, depth = pdw.ortho(V, UV, F, tex, X, Y, Z, px, (u0, v1, W, H))
    mask = np.isfinite(depth).astype(np.uint8)
    to_site = lambda c: np.stack([u0 + (c[:, 0] + 0.5) * px, v1 - (c[:, 1] + 0.5) * px], 1)  # noqa: E731

    d = Dxf(metres)
    L = {k: d.layer(f"{n} {k}", c) for k, c in (
        ("camera path", 30), ("cameras", 1), ("camera directions", 8), ("contours minor", 8),
        ("contours major", 7), ("outline", 5), ("not seen", 4), ("notes", 1))}

    cams = site_exports.cameras_site(n, np.asarray(site["matrix"], float))
    P = np.array([c["pos"] for c in cams])
    walk = float(np.sum(np.linalg.norm(np.diff(P, axis=0), axis=1)))
    th = 0.012 * walk                                         # text height
    d.polyline(L["camera path"], P)
    for i, c in enumerate(cams):
        d.point(L["cameras"], c["pos"])
        d.line(L["camera directions"], c["pos"], c["pos"] + 0.04 * walk * c["fwd"])
        if i % 10 == 0 or i == len(cams) - 1:
            d.text(L["cameras"], c["pos"] + np.array([0.6 * th, 0.6 * th, 0]), f"{c['frame']:03d}", th)

    n_cont = 0
    for layer, pts in pdw.read_dxf_polylines(sd / "terrain" / f"{n}_contours.dxf"):
        d.polyline(L["contours major" if layer.endswith("major") else "contours minor"], pts)
        n_cont += 1

    contours, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    total = float(mask.sum())
    n_out = n_holes = 0
    for c, h in zip(contours, hier[0]):
        area = cv2.contourArea(c)
        is_hole = h[3] >= 0
        if area < HOLE_SHARE * total:
            continue
        s = cv2.approxPolyDP(c, SIMPLIFY_PX, True)[:, 0, :].astype(float)
        if len(s) < 3:
            continue
        d.polyline(L["not seen" if is_hole else "outline"], to_site(s), closed=True)
        n_holes += int(is_hole)
        n_out += int(not is_hole)

    d.point(L["notes"], (0, 0, 0))
    notes = [f"{n}: origin 0,0 = the ground below the first camera",
             "units: metres" if metres else "units: scan units - NOT TO SCALE",
             "+Y = north" if north else "+Y = the walk's direction - north not set"]
    for k, t in enumerate(notes):
        d.text(L["notes"], (0, -(k + 1) * 1.8 * th, 0), t, th)

    out.parent.mkdir(parents=True, exist_ok=True)
    d.write(out)
    rep_dxf = {"cameras": len(cams), "contours": n_cont, "outline_polylines": n_out,
               "holes_drawn": n_holes, "hole_threshold_share": HOLE_SHARE,
               "simplify_px": SIMPLIFY_PX, "px": px, "left_out": left_out}
    rep_dir = out.parent if args.out else folder / "mesh"      # a test run keeps its report
    (rep_dir / "plan_dxf.json").write_text(json.dumps(rep_dxf, indent=1), encoding="utf-8")
    print(f"    {out.name}: {len(cams)} cameras, {n_cont} contour polylines, "
          f"{n_out} outline, {n_holes} holes  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
