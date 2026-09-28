"""Test: site.json -- one coordinate system for every export of a scene.

    python tools/site_frame_test.py [scene name]      (default: the first scene
                                                       that has a solve and sun.json)

The pass rules, fixed before the first run (2026-09-27):
  1. the first camera lands on the Z axis, above the origin (x = y = 0, z > 0)
  2. sun.json's "up" becomes +Z exactly (to 1e-6)
  3. without north, the walk heads along +Y: its levelled mean heading has
     x = 0 and y > 0
  4. the axes are right-handed (determinant +1) and orthonormal
  5. no measured scale -> units "scan units", status "NOT TO SCALE", no scaling
  6. a scale of s multiplies every site coordinate by exactly s
  7. with north set, Y is north levelled, and X = east = Y x Z
  8. the origin is on the ground: over the frames whose ground was scanned, the
     phone's height is within 10 % of the first camera's height above the origin

Reads a real scene; writes nothing (site_frame.build takes the sun and the
scale as arguments). Exit code 0 if every check passed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import dense_mesh    # noqa: E402
import scene_paths   # noqa: E402
import site_frame    # noqa: E402

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    checks.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def apply(M, p):
    return (np.asarray(M) @ np.append(p, 1.0))[:3]


def direction(M, v):
    w = np.asarray(M)[:3, :3] @ np.asarray(v, float)
    return w / np.linalg.norm(w)


def pick_scene() -> str | None:
    """A scene with a solve and a sun.json: a student's first, then any other."""
    names = []
    if scene_paths.STUDENTS_OUT.is_dir():
        names += [d.name for d in sorted(scene_paths.STUDENTS_OUT.iterdir())
                  if (d / scene_paths.WORKING).is_dir()]
    if scene_paths.OUTPUT.is_dir():
        names += [d.name for d in sorted(scene_paths.OUTPUT.iterdir()) if d.is_dir()]
    for n in names:
        d = scene_paths.scene_dir(n)
        if (d / "sun.json").is_file() and (d / "capture.json").is_file() \
                and (scene_paths.ROOT / "work" / n).is_dir():
            return n
    return None


def run(name: str) -> None:
    folder = scene_paths.scene_dir(name)
    sun = json.loads((folder / "sun.json").read_text(encoding="utf-8"))
    up_only = {k: v for k, v in sun.items() if k not in ("north", "to_sun")}
    pos, fwd, _ = site_frame.cameras_in_order(
        dense_mesh.find_model(scene_paths.ROOT / "work" / name))

    site = site_frame.build(name, sun=up_only, scale=0)
    M = np.array(site["matrix"])
    c0 = apply(M, pos[0])
    check("1 the first camera stands on the Z axis, above the origin",
          abs(c0[0]) < 1e-6 and abs(c0[1]) < 1e-6 and c0[2] > 0, str(np.round(c0, 4)))
    z = direction(M, up_only["up"])
    check("2 sun.json's up becomes +Z", np.allclose(z, [0, 0, 1], atol=1e-6), str(np.round(z, 6)))
    h = direction(M, fwd.mean(axis=0))
    h[2] = 0
    check("3 without north the walk heads along +Y",
          abs(h[0]) < 1e-6 and h[1] > 0, str(np.round(h, 6)))
    R = M[:3, :3]
    check("4 right-handed and orthonormal",
          abs(np.linalg.det(R) - 1) < 1e-6 and np.allclose(R @ R.T, np.eye(3), atol=1e-6),
          f"det {np.linalg.det(R):.9f}")
    check("5 no scale: scan units, marked NOT TO SCALE",
          site["units"] == "scan units" and "NOT TO SCALE" in site["scale"]["status"]
          and site["axes"]["y"] == "walk direction", f"{site['units']}, {site['scale']['status']}")

    s = 0.3125
    scaled = site_frame.build(name, sun=up_only, scale=s)
    Ms = np.array(scaled["matrix"])
    probe = [pos[len(pos) // 2], pos[-1], pos[0] + np.array([1.0, -2.0, 0.5])]
    ok = all(np.allclose(apply(Ms, p), s * apply(M, p), atol=1e-6) for p in probe)
    check("6 a scale of s multiplies every coordinate by s",
          ok and scaled["units"] == "metres" and scaled["scale"]["status"] == "measured")

    # a north 30 deg off the walk, levelled against up
    u = np.asarray(up_only["up"], float)
    f = fwd.mean(axis=0) - (fwd.mean(axis=0) @ u) * u
    f /= np.linalg.norm(f)
    e = np.cross(f, u)
    north = np.cos(np.radians(30)) * f + np.sin(np.radians(30)) * e
    withn = site_frame.build(name, sun={**up_only, "north": north.tolist(),
                                        "north_source": "test"}, scale=0)
    Mn = np.array(withn["matrix"])
    yn = direction(Mn, north)
    xe = direction(Mn, np.cross(north, u))
    check("7 north set: Y is north, X = east = Y x Z",
          np.allclose(yn, [0, 1, 0], atol=1e-6) and np.allclose(xe, [1, 0, 0], atol=1e-6)
          and withn["axes"] == {"x": "east", "y": "north", "z": "up"},
          f"north -> {np.round(yn, 6)}, east -> {np.round(xe, 6)}")

    how = site["how"]["origin"]
    held = site["first_camera_height"]
    if "phone was held" in how:
        measured = float(how.split("held ")[1].split(" units")[0])
        check("8 the origin is on the ground (phone height agrees within 10 %)",
              held > 0 and abs(measured - held) <= 0.1 * held, f"{held} vs {measured}")
    else:
        print(f"  SKIP  8 -- no scanned ground below the walk ({how})")


def main_() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else pick_scene()
    print(f"site.json -- the export coordinate system ({name})")
    if not name:
        print("  SKIP  no scene with a solve and sun.json on this machine")
        return 0
    run(name)
    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
