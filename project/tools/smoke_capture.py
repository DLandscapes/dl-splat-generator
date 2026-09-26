"""Tier 1 smoke test for the capture pipeline.

Cuts a few seconds off a source video, runs the whole pipeline at deliberately
cheap settings, then checks that every stage produced what it should. It is not
a quality test -- it answers "is the pipeline still wired together correctly",
which is what breaks when a dependency renames a flag or a stage writes to the
wrong place.

    python smoke_capture.py                     # uses input\\data\\IMG_8950.MOV
    python smoke_capture.py --source <video> --seconds 6

Everything it writes goes under work\\_smoke\\<timestamp>\\ so the real work and
output folders are untouched. Nothing is deleted automatically; the run folder
is printed at the end so it can be removed by hand.

Exit code 0 if every check passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent.parent
BIN = ROOT / "bin"
DEFAULT_SOURCE = ROOT / "input" / "data" / "IMG_8950.MOV"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    checks.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def find_binary(name: str) -> Path | None:
    hits = sorted(BIN.rglob(name))
    return hits[0] if hits else None


def ply_header(path: Path) -> dict:
    """Splat count, property names and SH degree, without reading the body."""
    with path.open("rb") as fh:
        head = fh.read(65536).decode("latin1", errors="replace")
    end = head.find("end_header")
    if not head.startswith("ply") or end < 0:
        raise ValueError("not a PLY file")
    count, props = 0, []
    for line in head[:end].splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "element" and parts[1] == "vertex":
            count = int(parts[2])
        elif parts[0] == "property":
            props.append(parts[-1])
    n_rest = sum(1 for p in props if p.startswith("f_rest_"))
    sh_degree = {0: 0, 9: 1, 24: 2, 45: 3}.get(n_rest)
    return {"count": count, "props": props, "sh_degree": sh_degree}


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test the capture pipeline.")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--seconds", type=float, default=8.0,
                    help="length of clip to cut from the source (default 8)")
    ap.add_argument("--stride", type=int, default=4)
    ap.add_argument("--steps", type=int, default=1000)
    ap.add_argument("--compute", choices=["auto", "gpu", "cpu"], default="auto",
                    help="cpu runs COLMAP's features and matching on the processor "
                         "even with an NVIDIA card: the laptop route, testable here")
    args = ap.parse_args()

    source = Path(args.source).expanduser().resolve()
    ffmpeg = find_binary("ffmpeg.exe")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "work" / "_smoke" / stamp
    work_root, out_root = run_dir / "work", run_dir / "output"
    name = "smoke"

    print(f"Capture pipeline smoke test")
    print(f"  source {source}")
    print(f"  run    {run_dir}\n")

    if not check("source video exists", source.is_file(), str(source)):
        return 1
    if not check("ffmpeg found", ffmpeg is not None):
        return 1

    run_dir.mkdir(parents=True, exist_ok=True)
    clip = run_dir / "clip.mp4"

    print("\n-- cutting a short clip --")
    cut = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(source), "-t", str(args.seconds), "-c", "copy", str(clip)],
        capture_output=True, text=True)
    if not check("clip cut", cut.returncode == 0 and clip.is_file(),
                 f"{clip.stat().st_size / 1e6:.1f} MB" if clip.is_file()
                 else cut.stderr.strip()[:200]):
        return 1

    print("\n-- running the pipeline --")
    began = time.time()
    proc = subprocess.run(
        [sys.executable, "-u", str(TOOLS / "capture.py"), str(clip),
         "--name", name,
         "--work-root", str(work_root), "--out-root", str(out_root),
         "--stride", str(args.stride), "--steps", str(args.steps),
         "--max-resolution", "800", "--max-splats", "200000",
         "--blur-drop", "20", "--compute", args.compute],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    elapsed = time.time() - began
    for line in (proc.stdout or "").splitlines():
        print(f"    | {line}")
    if proc.returncode != 0:
        print((proc.stderr or "")[-1500:])
    check("pipeline exited cleanly", proc.returncode == 0,
          f"{elapsed:.0f}s, exit {proc.returncode}")

    work = work_root / name
    out_dir = out_root / name
    images = work / "images"
    rejected = work / "rejected"
    undist = work / "undistorted"

    print("\n-- checking artefacts --")
    frames = sorted(images.glob("*.jpg"))
    check("frames extracted", len(frames) >= 10, f"{len(frames)} kept")

    # Regression guard for a real bug: rejected frames were once moved to
    # images\_rejected, which COLMAP recursed into, so pruning did nothing.
    check("rejected frames are outside images/",
          not any(p.is_dir() for p in images.iterdir()) if images.is_dir() else False,
          f"{len(list(rejected.glob('*.jpg'))) if rejected.is_dir() else 0} rejected")

    sparse = undist / "sparse" / "0"
    for f in ("cameras.bin", "images.bin", "points3D.bin"):
        check(f"COLMAP produced {f}", (sparse / f).is_file())

    undist_images = list((undist / "images").glob("*")) if (undist / "images").is_dir() else []
    check("undistorted images present", len(undist_images) > 0, f"{len(undist_images)}")
    check("no rejected frames leaked into the model",
          not any(p.is_dir() for p in undist_images),
          "no subfolders under undistorted/images")

    registered = 0
    if (sparse / "images.bin").is_file():
        with (sparse / "images.bin").open("rb") as fh:
            registered = struct.unpack("<Q", fh.read(8))[0]
    check("COLMAP registered images", registered > 0, f"{registered} registered")

    cams_path = out_dir / "cameras.json"
    cams = None
    if check("cameras.json written", cams_path.is_file()):
        try:
            cams = json.loads(cams_path.read_text())
        except Exception as exc:                             # noqa: BLE001
            check("cameras.json parses", False, str(exc))
    if cams is not None:
        check("cameras.json parses", cams.get("format") == "dlcameras",
              f"{cams.get('count')} cameras")
        check("camera count matches COLMAP", cams.get("count") == registered,
              f"json {cams.get('count')} vs colmap {registered}")
        first = (cams.get("cameras") or [{}])[0]
        check("cameras carry position and direction",
              len(first.get("position", [])) == 3 and len(first.get("direction", [])) == 3)

    plys = sorted(out_dir.glob("*.ply")) if out_dir.is_dir() else []
    if check("splat .ply produced", len(plys) > 0,
             f"{plys[-1].stat().st_size / 1e6:.1f} MB" if plys else ""):
        try:
            info = ply_header(plys[-1])
            check("ply has splats", info["count"] > 0, f"{info['count']:,} splats")
            check("ply is 3DGS layout",
                  "scale_0" in info["props"] and "rot_0" in info["props"])
            check("ply carries spherical harmonics", info["sh_degree"] == 3,
                  f"degree {info['sh_degree']}")
        except Exception as exc:                             # noqa: BLE001
            check("ply header readable", False, str(exc))

    # Tier 2: has the recovered geometry drifted from a known-good run?
    # Structure-from-motion is only determined up to a similarity transform, so
    # this aligns before measuring -- see pose_regression.py.
    # pose_regression.py owns which baseline is current (it changed when the
    # camera model did); ask it rather than naming the file here
    sys.path.insert(0, str(TOOLS))
    import pose_regression
    reference = pose_regression.DEFAULT_REFERENCE
    print("\n-- pose regression --")
    if not reference.is_file():
        print("  (no reference recorded; run: python pose_regression.py record "
              "<cameras.json>)")
    elif not cams_path.is_file():
        check("pose regression could run", False, "no cameras.json produced")
    else:
        try:
            import pose_regression
            stats = pose_regression.compare(cams_path, reference)
            print(f"  aligned: scale x{stats['scale']:.4f}, "
                  f"rotation {stats['rotation_deg']:.2f} deg, "
                  f"extent {stats['extent']:.3f} units")
            for nm, passed, detail in pose_regression.verdict(stats)[1]:
                check(f"geometry: {nm}", passed, detail)
        except Exception as exc:                             # noqa: BLE001
            check("pose regression ran", False, str(exc))

    print()
    check("capture.json written", (out_dir / "capture.json").is_file())
    idx = out_root / "scenes.json"
    if check("scenes.json written", idx.is_file()):
        try:
            data = json.loads(idx.read_text())
            check("scenes.json lists this run",
                  any(s.get("name") == name for s in data.get("scenes", [])))
        except Exception as exc:                             # noqa: BLE001
            check("scenes.json parses", False, str(exc))

    failed = [c for c in checks if not c[1]]
    print(f"\n{'-' * 60}")
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed in {elapsed:.0f}s")
    if failed:
        print("FAILED:")
        for nm, _, detail in failed:
            print(f"  - {nm}{f' ({detail})' if detail else ''}")
    print(f"\nArtefacts left at {run_dir}")
    print("(nothing is deleted automatically — remove that folder when done)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
