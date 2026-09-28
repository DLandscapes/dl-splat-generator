"""The package's lighter splats, each TRAINED at its size -- not thinned from the full one.

    python tools/train_levels.py <scene name> [--levels med,low,verylow]

Why (measured 2026-09-27 on a student scan, six frames held out of training; test outputs\\2026-09-27\\
budget training): trained with Brush's --max-splats 150,000 the splat matched the video with
MAE 0.046, the full splat thinned to 150,000 with 0.280; at 50,000, 0.063 against 0.386. A
thinned file keeps the big Gaussians and loses the small ones the surfaces are made of -- fine
from an overview, dark and full of holes from where the phone stood.

Each level is a Brush run on the scene's own dataset (work\\<name>\\undistorted) with the
scene's own settings (capture.json: steps, SH degree, resolution), only the cap differs:

    med 400,000   low 150,000   verylow 50,000

!! Below the sparse model's point count Brush does not hold the cap (a 50,000 run on a student scan, whose
sparse cloud has 63,037 points, ended with 962,951 splats). A cap under 90 % of the sparse
count therefore starts from every k-th point (--subsample-points k).

The trained file goes to the scene's working folder (levels\\<level>\\), then into the package
through splat_levels.py --trained (moved to site coordinates, SH rotated). A level whose
package file exists is left alone. Uses capture.run_brush: the same memory check and the same
retry ladder (lower resolution) as a capture.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import capture       # noqa: E402 -- run_brush, WORK, StageError
import scene_paths   # noqa: E402

SIZES = {"med": 400_000, "low": 150_000, "verylow": 50_000}


def sparse_points(dataset: Path) -> int:
    p = dataset / "sparse" / "0" / "points3D.bin"
    if not p.is_file():
        return 0
    with p.open("rb") as fh:
        return struct.unpack("<Q", fh.read(8))[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--levels", default="med,low,verylow")
    args = ap.parse_args()
    n = args.name
    folder = scene_paths.scene_dir(n)
    rec = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    settings = rec.get("settings") or {}
    dataset = capture.WORK / n / "undistorted"
    if not (dataset / "sparse" / "0").is_dir():
        print(f"FAILED: no undistorted dataset at {dataset} -- the capture's intermediates are gone",
              file=sys.stderr)
        return 2
    splat_dir = scene_paths.site_data_dir(n) / "splat"
    levels = [l.strip() for l in args.levels.split(",") if l.strip()]
    bad = [l for l in levels if l not in SIZES]
    if bad:
        print(f"FAILED: unknown level(s) {bad}", file=sys.stderr)
        return 2
    todo = [l for l in levels if not (splat_dir / f"{n}_3DGS_{l}.ply").exists()]
    for l in levels:
        if l not in todo:
            print(f"    {n}_3DGS_{l}.ply already in the package -- left alone")
    sparse = sparse_points(dataset)
    trained = []
    for l in todo:
        cap = SIZES[l]
        k = math.ceil(sparse / (0.9 * cap)) if sparse > 0.9 * cap else 1
        out_dir = folder / "levels" / l
        steps = int(settings.get("steps") or 10000)
        ply = out_dir / f"splat_{steps}.ply"
        print(f"    {l}: {cap:,} splats" + (f", starting from every {k}th of {sparse:,} sparse points"
                                             if k > 1 else ""))
        if not ply.is_file():
            try:
                capture.run_brush(dataset, out_dir, steps=steps, max_splats=cap,
                                  sh_degree=int(settings.get("sh_degree") or 3),
                                  max_resolution=int(settings.get("max_resolution") or 1920),
                                  subsample=None, max_frames=None, viewer=False, dry=False,
                                  extra=("--subsample-points", k))
            except capture.StageError as exc:
                print(f"FAILED: training the {l} level: {str(exc).splitlines()[0]}", file=sys.stderr)
                return 3
        else:
            print(f"    {l}: trained before ({ply.relative_to(folder)}), used as it is")
        trained += ["--trained", f"{l}={ply}"]
    if not todo:
        return 0
    cmd = [sys.executable, "-u", str(TOOLS / "splat_levels.py"), n, "--only", ",".join(todo), *trained]
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
