"""Which way is up for a scene, kept in its sun.json before anyone sets north.

    python tools/scene_up.py <scene name>

A scene filmed looking down (student capture A: 78 deg) stands on its edge in
Blender unless the package says which way is up. That used to come only with
north, set by hand in the viewer. This writes the vertical by the SAME rule the
viewer and the terrain tool use (tools/level.py, twin of static/level.js),
from the solve's cameras and the scene's own points, in the scene file's frame
(COLMAP: Y down, Z forward) -- the frame sun.json's vectors are in.

What it writes, beside the scene (scene_paths.scene_dir):

    sun.json   {"format": "dlsun", "version": 1, "up": [...], "frame": ...,
                "level": {"up": [...], "method": "...", ...},
                "local_time", "utc_offset", "lat", "lon"}   <- place and time only
                                                              with --with-place

NO north and no sun: those need a human (a shadow, a known direction), and a
guessed north would put a wrong sun in the file. The "level" block is kept by
the backend when north is set or cleared later (app/main.py sun_put), so the
vertical is never lost; a north set by hand is never overwritten here.

Place and time are personal data: they are copied from the video's own
metadata only when asked (--with-place), for a file that goes back to the
person who filmed it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import dense_mesh    # noqa: E402  -- find_model: the solve the splat was trained on
import ground_dem    # noqa: E402  -- read_ply, up_from_model (the shared rule)
import scene_paths   # noqa: E402

FRAME = "scene file (COLMAP-style: Y down, Z forward)"


def place_and_time(source: Path) -> dict:
    import source_meta
    meta = source_meta.read(source)
    out = {}
    when = meta.get("when") or ""
    # "2026-09-11T11:12:21+0200" -> local time + offset, as the viewer keeps them
    if len(when) >= 19:
        out["local_time"] = when[:16]
        tail = when[19:]
        if len(tail) == 5 and tail[0] in "+-":
            out["utc_offset"] = f"{tail[:3]}:{tail[3:]}"
    where = meta.get("where") or {}
    if where.get("lat") is not None and where.get("lon") is not None:
        out["lat"], out["lon"] = where["lat"], where["lon"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--with-place", action="store_true",
                    help="also copy the video's date, time and GPS position")
    args = ap.parse_args()

    folder = scene_paths.scene_dir(args.name)
    record = folder / "capture.json"
    if not record.is_file():
        print(f"FAILED: no scene called {args.name}", file=sys.stderr)
        return 2
    rec = json.loads(record.read_text(encoding="utf-8"))
    sun_path = folder / "sun.json"
    existing = {}
    if sun_path.is_file():
        try:
            existing = json.loads(sun_path.read_text(encoding="utf-8"))
        except ValueError:
            existing = {}

    model = dense_mesh.find_model(scene_paths.ROOT / "work" / args.name)
    ply = folder / rec["ply"] if rec.get("ply") else None
    xyz = None
    if ply and ply.is_file():
        xyz, _ = ground_dem.read_ply(ply)
        xyz = xyz[np.isfinite(xyz).all(axis=1)]
    est = ground_dem.up_from_model(model, xyz)
    if est is None:
        print("FAILED: no cameras in the solve", file=sys.stderr)
        return 2

    import level
    up = [round(float(x), 6) for x in est["up"]]
    lvl = {"up": up, "method": est["method"], "made_by": "tools/scene_up.py"}
    if est.get("look_down") is not None:
        lvl["cameras_look_down_deg"] = round(est["look_down"], 1)
    if est.get("mean_up") is not None:
        lvl["vs_mean_up_deg"] = round(level.angle_deg(est["up"], est["mean_up"]), 2)

    if existing.get("north"):
        # north was set by hand: its "up" stays; only the level block is added
        out = {**existing, "level": lvl}
        print(f"    north already set ({existing.get('north_source', 'hand')}) -- kept")
    else:
        out = {"format": "dlsun", "version": 1, "up": up, "frame": FRAME, "level": lvl}
    if args.with_place and rec.get("source") and Path(rec["source"]).is_file():
        for k, v in place_and_time(Path(rec["source"])).items():
            out.setdefault(k, v)
    sun_path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"    up by '{est['method']}': {up}"
          + (f"  ({lvl['vs_mean_up_deg']} deg from the cameras' mean up)"
             if "vs_mean_up_deg" in lvl else ""))
    print(f"    written {sun_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
