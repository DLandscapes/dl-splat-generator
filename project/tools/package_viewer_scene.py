"""A scene bundle for the Splat Generator viewer: <name>.dlscene (one file).

    python tools/package_viewer_scene.py <scene name> [--out <file>]

Marc (2026-09-27): a splat dropped into the viewer arrives without its camera
walk. The student package therefore carries, next to <name>.blend, one file the
viewer opens with everything: drop it in (locally or on the website -- no
server needed) and the scan comes with its capture cameras, the walk at the
filmed speed, and the scene state.

    <name>.dlscene   a ZIP (stored, not compressed -- the splat already is):
        scene.json   the scene state, format "dlscene" v1 as the viewer saves it,
                     plus a "bundle" block naming the files below
        <name>.spz   the splat, compressed (every splat), in the scan's own frame
        cameras.json the capture cameras (format "dlcameras"), same frame

The splat is the scan's own .spz, NOT a site-frame .ply: the viewer levels and
orients a scene from its capture cameras, and those are in the scan's frame.
The scale travels (tools' "scale" block) when capture.json records a measured
one; north and the sun travel when sun.json has north. Place and time only with
--with-place (personal data -- Marc: yes for a student's own capture).
Nothing else from capture.json is copied (it names paths on this PC).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import scene_paths   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--out", default=None, help="file (default: the package's <name>.dlscene)")
    ap.add_argument("--with-place", action="store_true")
    args = ap.parse_args()

    folder = scene_paths.scene_dir(args.name)
    rec = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else scene_paths.package_dir(args.name) / f"{args.name}.dlscene"
    if out.exists():
        print(f"FAILED: {out} exists -- never overwritten", file=sys.stderr)
        return 2
    spz = folder / rec["ply"] if rec.get("ply", "").endswith(".spz") else None
    if spz is None:
        spz = next(iter(sorted(folder.glob("*.spz"))), None)
    cams = folder / "cameras.json"
    if not (spz and spz.is_file() and cams.is_file()):
        print(f"FAILED: {folder} needs a .spz and cameras.json", file=sys.stderr)
        return 2

    splat_name = f"{args.name}.spz"
    scene = {
        "format": "dlscene", "version": 1, "app": "DL-SplatGenerator",
        "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": {"name": splat_name, "bytes": spz.stat().st_size},
        "bundle": {
            "name": args.name, "splat": splat_name, "cameras": "cameras.json",
            # what the viewer's walk needs to play at the filmed speed
            "capture": {"fps": rec.get("fps"), "method": rec.get("method"),
                        "settings": {"stride": (rec.get("settings") or {}).get("stride")}},
            "made_by": "DL-SplatGenerator tools/package_viewer_scene.py (Digital Landscapes)",
        },
    }
    scale = float(rec.get("scale_m_per_unit") or 0)
    if scale > 0:
        scene["scale"] = {"factor": scale, "calibrated": True, "estimated": False}
    sun_path = folder / "sun.json"
    sun = json.loads(sun_path.read_text(encoding="utf-8")) if sun_path.is_file() else {}
    if sun.get("north") and sun.get("up"):
        s = {"north": sun["north"], "up": sun["up"], "source": sun.get("north_source", "hand"),
             "detail": sun.get("north_detail", ""), "measured": sun.get("measured_elevation_deg")}
        if args.with_place:
            for k_in, k_out in (("local_time", "when"), ("utc_offset", "zone"), ("lat", "lat"), ("lon", "lon")):
                if sun.get(k_in) is not None:
                    s[k_out] = sun[k_in]
        scene["view"] = {"display": {"sun": s}}

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("scene.json", json.dumps(scene, indent=1))
        z.write(spz, splat_name)
        z.write(cams, "cameras.json")
    n_cams = len(json.loads(cams.read_text(encoding="utf-8")).get("cameras", []))
    print(f"    {out.name}: {spz.stat().st_size / 1e6:.1f} MB splat, {n_cams} cameras, "
          f"scale {'set' if scale > 0 else 'not set'}, north {'set' if 'view' in scene else 'not set'}")
    print(f"    written {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
