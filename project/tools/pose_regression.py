"""Tier 2: check that a pipeline re-run still recovers the same geometry.

Structure-from-motion fixes a scene only up to a similarity transform -- seven
degrees of freedom: scale, rotation and translation are all arbitrary. Two
perfectly good runs of the same footage can therefore produce camera positions
with entirely different numbers. Comparing them directly is meaningless.

So this aligns the two camera paths first, with the closed-form Umeyama
solution, and measures what is left over. Residual after alignment is the part
that actually indicates a change in the reconstruction.

    python pose_regression.py record <cameras.json> [--as reference/name.json]
    python pose_regression.py check  <cameras.json> [--against reference/name.json]

Residuals are reported both in scene units and as a percentage of the camera
path length, because the unit scale is itself arbitrary.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REFERENCE_DIR = TOOLS / "reference"
# smoke_poses.json (2026-08-03, OPENCV, no focal prior) is the baseline again.
# It was briefly replaced on 2026-09-06 while the pipeline used SIMPLE_RADIAL;
# that camera model was reverted the same evening once a controlled test showed
# it was not what made a forward-walking capture reconstruct (the mapper
# initialisation was). smoke_poses_simple_radial.json is left in place, unused,
# as the record of that detour.
DEFAULT_REFERENCE = REFERENCE_DIR / "smoke_poses.json"

# Tolerances, set from measurement rather than guesswork. Two independent
# COLMAP runs of the same clip differ by 0.01% RMS / 0.02% worst (its RANSAC is
# multithreaded, so runs are not bit-identical), while a deliberate 3% sine
# bend of the camera path shows up as 0.52% RMS / 0.95% worst. These limits sit
# roughly 20x above the observed noise and well below a real distortion.
#
# If this starts failing spuriously on other footage, re-measure the noise floor
# on that footage before loosening it -- the gap is wide enough that a genuine
# regression is far more likely than drift.
MAX_RMS_PCT = 0.20       # RMS residual, as a percentage of camera-path extent
MAX_WORST_PCT = 0.60     # worst single camera, same units
MIN_MATCH_PCT = 90.0     # share of reference cameras that must be present


def load_cameras(path: Path) -> dict:
    data = json.loads(Path(path).read_text())
    if data.get("format") != "dlcameras":
        raise ValueError(f"{path} is not a dlcameras file")
    return {c["name"]: c["position"] for c in data.get("cameras", [])}


def path_length(points) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def extent(points) -> float:
    """Diagonal of the camera positions' bounding box.

    Residuals are normalised by this rather than by path length. Path length
    grows with every wiggle and every extra frame, so it inflates the
    denominator and hides real distortion; the extent reflects how big the
    captured scene actually is.
    """
    lo = [min(p[i] for p in points) for i in range(3)]
    hi = [max(p[i] for p in points) for i in range(3)]
    return math.dist(lo, hi)


def umeyama(P, Q):
    """Similarity transform (scale c, rotation R, translation t) mapping P onto Q."""
    import numpy as np

    n = P.shape[0]
    mu_p, mu_q = P.mean(axis=0), Q.mean(axis=0)
    X, Y = P - mu_p, Q - mu_q
    C = (Y.T @ X) / n
    U, S, Vt = np.linalg.svd(C)
    D = np.eye(3)
    if np.linalg.det(U @ Vt) < 0:
        D[2, 2] = -1.0
    R = U @ D @ Vt
    var_p = (X ** 2).sum() / n
    c = float((np.diag(S) @ D).trace() / var_p) if var_p > 0 else 1.0
    t = mu_q - c * (R @ mu_p)
    return c, R, t


def compare(candidate: Path, reference: Path) -> dict:
    import numpy as np

    cand = load_cameras(candidate)
    ref = load_cameras(reference)
    shared = sorted(set(cand) & set(ref))
    if len(shared) < 4:
        raise ValueError(
            f"only {len(shared)} cameras in common — cannot align "
            f"(candidate {len(cand)}, reference {len(ref)})")

    P = np.array([cand[n] for n in shared], dtype=float)
    Q = np.array([ref[n] for n in shared], dtype=float)
    c, R, t = umeyama(P, Q)
    aligned = (c * (R @ P.T).T) + t
    err = np.linalg.norm(aligned - Q, axis=1)

    ref_points = [ref[n] for n in shared]
    ref_extent = extent(ref_points)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1) / 2))))
    rms = float(np.sqrt((err ** 2).mean()))
    worst = float(err.max())
    return {
        "reference_cameras": len(ref),
        "candidate_cameras": len(cand),
        "matched": len(shared),
        "match_pct": 100.0 * len(shared) / max(len(ref), 1),
        "scale": c,
        "rotation_deg": angle,
        "path_length": path_length(ref_points),
        "extent": ref_extent,
        "rms": rms,
        "worst": worst,
        "rms_pct": 100.0 * rms / ref_extent if ref_extent else float("inf"),
        "worst_pct": 100.0 * worst / ref_extent if ref_extent else float("inf"),
    }


def verdict(stats: dict) -> tuple[bool, list]:
    rows = [
        ("cameras matched", stats["match_pct"] >= MIN_MATCH_PCT,
         f"{stats['matched']} of {stats['reference_cameras']} "
         f"({stats['match_pct']:.1f}%, need {MIN_MATCH_PCT}%)"),
        ("RMS residual after alignment", stats["rms_pct"] <= MAX_RMS_PCT,
         f"{stats['rms']:.4f} units = {stats['rms_pct']:.2f}% of extent "
         f"(limit {MAX_RMS_PCT}%)"),
        ("worst single camera", stats["worst_pct"] <= MAX_WORST_PCT,
         f"{stats['worst']:.4f} units = {stats['worst_pct']:.2f}% of extent "
         f"(limit {MAX_WORST_PCT}%)"),
    ]
    return all(ok for _, ok, _ in rows), rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)

    rec = sub.add_parser("record", help="store a cameras.json as the reference")
    rec.add_argument("cameras")
    rec.add_argument("--as", dest="dest", default=str(DEFAULT_REFERENCE))

    chk = sub.add_parser("check", help="compare a cameras.json to the reference")
    chk.add_argument("cameras")
    chk.add_argument("--against", dest="ref", default=str(DEFAULT_REFERENCE))

    args = ap.parse_args()
    src = Path(args.cameras).expanduser().resolve()

    if args.mode == "record":
        dest = Path(args.dest).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(src.read_text())
        if data.get("format") != "dlcameras":
            print(f"FAILED: {src} is not a dlcameras file", file=sys.stderr)
            return 1
        dest.write_text(json.dumps(data, indent=1))
        print(f"recorded {data.get('count')} cameras as reference\n  {dest}")
        return 0

    ref = Path(args.ref).expanduser().resolve()
    if not ref.is_file():
        print(f"No reference at {ref}.\n"
              f"Record one first:  python pose_regression.py record <cameras.json>",
              file=sys.stderr)
        return 1
    try:
        stats = compare(src, ref)
    except (ValueError, OSError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print("Pose regression against reference")
    print(f"  reference {ref.name} ({stats['reference_cameras']} cameras)")
    print(f"  candidate {src.name} ({stats['candidate_cameras']} cameras)")
    print(f"  alignment: scale x{stats['scale']:.4f}, "
          f"rotation {stats['rotation_deg']:.2f} deg")
    print(f"  reference extent {stats['extent']:.3f} units "
          f"(path length {stats['path_length']:.3f})\n")
    ok, rows = verdict(stats)
    for name, passed, detail in rows:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}  — {detail}")
    print(f"\n{'PASS' if ok else 'FAIL'}: geometry "
          f"{'matches' if ok else 'has drifted from'} the reference")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
