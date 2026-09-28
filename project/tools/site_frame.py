"""One coordinate system for everything exported from a scene: site.json.

    python tools/site_frame.py <scene name>

Every file in a student's package -- mesh, cameras, sun, contours, drawings, the
.blend -- must line up when brought into Rhino or Blender. They do only if all
of them use the same frame. This writes that frame once, beside the scene, and
every exporter applies it:

    site = scale * R @ (p - origin)          p in the scene file's own frame
                                             (COLMAP: Y down, Z forward)

    Z  up         sun.json's "up" (tools/scene_up.py, the viewer's levelling rule)
    Y  north      once north is set in the viewer; until then the WALK's mean
                  heading, levelled -- and site.json says so
    X  east       (or: right of the walk), so X, Y, Z is right-handed as in Rhino
                  and Blender
    0  origin     the ground below the first placed camera
    units         metres when capture.json records a measured scale
                  (scale_m_per_unit, the same field the viewer reads); scan units
                  otherwise, and site.json says "not to scale"

Nothing here guesses a scale. The camera's height above the ground is recorded
in scan units as a check a person can make ("the phone was about 1.5 m up") --
it is NOT used as a scale: in a forward walk it is not one (see the project's
measured lessons).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import colmap_cameras   # noqa: E402
import dense_mesh       # noqa: E402  -- find_model
import ground_dem       # noqa: E402  -- read_ply, up_from_model
import scene_paths      # noqa: E402

FRAME = "scene file (COLMAP-style: Y down, Z forward)"
NEAREST = 3000          # points around the first camera that decide the ground
GROUND_PERCENTILE = 5   # the ground is near the LOW end of what is below the camera
HEIGHT_SHARE = 0.08     # "below a camera" = within this share of the walk's length
                        # (scan units differ from scan to scan; on student capture B 0.08 x 18 = 1.4)


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def cameras_in_order(model: Path) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Centres and view directions, in filming order (frame names sort so)."""
    rows = []
    for im in colmap_cameras.read_images_bin(model / "images.bin"):
        R = np.asarray(colmap_cameras.quat_to_matrix(*im["q"]), dtype=np.float64)
        rows.append((im["name"], -R.T @ np.asarray(im["t"], dtype=np.float64), R[2]))
    rows.sort(key=lambda r: r[0])
    return (np.array([r[1] for r in rows]), np.array([r[2] for r in rows]),
            [r[0] for r in rows])


def build(name: str, *, sun: dict | None = None, scale: float | None = None) -> dict:
    """site.json's content. `sun` and `scale` replace what is on disk -- for
    tests, which must not write into a scene."""
    folder = scene_paths.scene_dir(name)
    rec = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    if scale is not None:
        rec["scale_m_per_unit"] = scale
    if sun is None:
        sun = {}
        if (folder / "sun.json").is_file():
            sun = json.loads((folder / "sun.json").read_text(encoding="utf-8"))

    model = dense_mesh.find_model(scene_paths.ROOT / "work" / name)
    pos, fwd, names = cameras_in_order(model)

    # the points that decide where the ground is: the dense cloud if there is
    # one (surfaces only), else the splat's centres
    cloud = folder / "mesh" / "dense.ply"
    splat = folder / rec["ply"] if rec.get("ply") else None
    src = cloud if cloud.is_file() else splat
    xyz, _ = ground_dem.read_ply(src)
    xyz = xyz[np.isfinite(xyz).all(axis=1)]

    # Z: sun.json's up, or the same rule computed here
    if sun.get("up"):
        z, up_how = _unit(np.asarray(sun["up"], float)), \
            "sun.json" + (f" ({sun['level']['method']})" if sun.get("level") else "")
    else:
        est = ground_dem.up_from_model(model, xyz)
        z, up_how = _unit(np.asarray(est["up"], float)), f"computed ({est['method']})"

    # Y: north when set, else the walk's mean heading -- both levelled
    if sun.get("north"):
        n = np.asarray(sun["north"], float)
        y_is = "north"
        y_how = f"north set in the viewer ({sun.get('north_source', 'by hand')})"
    else:
        n = fwd.mean(axis=0)
        y_is = "walk direction"
        y_how = "the cameras' mean heading, levelled -- NOT north (north not set yet)"
    y = _unit(n - (n @ z) * z)
    x = np.cross(y, z)                       # east (or right of the walk)
    R = np.array([x, y, z])

    # origin: the ground below the first placed camera.
    # A phone looks AHEAD, so the ground at the walker's feet is rarely in the
    # scan -- on student capture B nothing lies within 5.8 units of the first camera. Where
    # the walk DOES pass over scanned ground, the camera's height above it is
    # measured (low 10 % of the points within HEIGHT_RADIUS of each camera,
    # read in the site axes); the median of those heights is how high the
    # phone was held, and the origin is that far below the first camera.
    c0 = pos[0]
    rel = (xyz - c0) @ R.T                   # site axes, scan units, camera at 0
    cams = (pos - c0) @ R.T
    walk = float(np.sum(np.linalg.norm(np.diff(cams[:, :2], axis=0), axis=1)))
    radius = max(HEIGHT_SHARE * walk, 1e-6)
    heights = []
    for c in cams:
        m = np.hypot(rel[:, 0] - c[0], rel[:, 1] - c[1]) < radius
        if m.sum() >= 30:
            heights.append(c[2] - np.percentile(rel[m, 2], 10))
    kind = "cloud" if src == cloud else "splat"
    if len(heights) >= 3:
        held = float(np.median(heights))
        ground_z = -held
        ground_how = (f"the phone was held {held:.3f} units above the ground (median over "
                      f"the {len(heights)} of {len(cams)} frames whose ground below was "
                      f"scanned -- low 10 % of the {kind} points within {radius:.2f} "
                      f"units); origin that far below the first camera")
    else:
        horiz = np.hypot(rel[:, 0], rel[:, 1])
        near = np.argsort(horiz)[:NEAREST]
        below = rel[near][rel[near][:, 2] < 0]
        if len(below) >= 50:
            ground_z = float(np.percentile(below[:, 2], GROUND_PERCENTILE))
            ground_how = (f"no scanned ground below the walk; {GROUND_PERCENTILE}th "
                          f"percentile height of the {NEAREST} {kind} points nearest the "
                          f"first camera (up to {horiz[near].max():.1f} units away)")
        else:
            ground_z = 0.0
            ground_how = "no ground found near the walk -- origin AT the first camera"
    origin = c0 + ground_z * z               # z is a unit vector in the scene frame

    scale = float(rec.get("scale_m_per_unit") or 0)
    scaled = scale > 0
    s = scale if scaled else 1.0
    M = np.eye(4)
    M[:3, :3] = s * R
    M[:3, 3] = -s * R @ origin

    return {
        "format": "dlsite", "version": 1,
        "scene": name,
        "from_frame": FRAME,
        "matrix": [[round(float(v), 9) for v in row] for row in M],
        "matrix_note": "row-major 4x4: [site, 1] = matrix @ [p, 1], p a point in the scene "
                       "file's frame; directions use the upper 3x3 (then normalise)",
        "units": "metres" if scaled else "scan units",
        "scale": {"m_per_unit": scale if scaled else None,
                  "status": "measured" if scaled else "not set -- NOT TO SCALE",
                  "note": rec.get("scale_note") or ""},
        "axes": {"x": "east" if y_is == "north" else "right of the walk",
                 "y": y_is, "z": "up"},
        "how": {"z": up_how, "y": y_how, "origin": ground_how},
        "origin_scene": [round(float(v), 6) for v in origin],
        "first_camera": names[0],
        "first_camera_height": round(float(-ground_z * s), 4),
        "first_camera_height_note": "height of the first camera above the origin, in the "
                                    "units above -- a check, not a scale",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    args = ap.parse_args()
    folder = scene_paths.scene_dir(args.name)
    if not (folder / "capture.json").is_file():
        print(f"FAILED: no scene called {args.name}", file=sys.stderr)
        return 2
    site = build(args.name)
    (folder / "site.json").write_text(json.dumps(site, indent=1), encoding="utf-8")
    print(f"    units   {site['units']} ({site['scale']['status']})")
    print(f"    Z up    {site['how']['z']}")
    print(f"    Y       {site['how']['y']}")
    print(f"    origin  {site['how']['origin']}")
    print(f"    first camera {site['first_camera_height']} {site['units']} above the origin")
    print(f"    written {folder / 'site.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
