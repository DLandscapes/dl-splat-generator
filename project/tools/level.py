"""Which way is up in a capture -- the Python twin of static/level.js.

The viewer and the terrain tool must agree on the vertical, or a terrain model
and the scene it came from disagree about what is level. The rule and its
measurements are documented in static/level.js; in short:

  filmed DOWN at the ground (cameras look >= 20 deg down onto the dominant
      plane, which holds >= 60 % of the points, cameras above it)
      -> the ground plane is level                          method "ground"
  otherwise, the walk TURNED (frames' right vectors spread)
      -> the vertical most perpendicular to every right vector: the phone is
         held without tilting sideways, however far it points down
                                                            method "held level"
  otherwise -- a walk that never turned
      -> the cameras' mean up with its sideways component removed
                                                            method "held level, walk straight"

THE ONE HONEST AMBIGUITY. A walk that never turns cannot tell "the path slopes"
from "the phone pointed slightly down": both tilt the same axis. There the
rule assumes the phone was held level ON AVERAGE, which keeps a real slope
(IMG_1779's path falls about 5 %) and is off by the camera's average pitch.
The alternative -- that the walk itself is horizontal -- would erase every
slope, which a terrain tool must not do (static/level.js assumed it until
2026-09-26 and IMG_1779 came out 4 deg flatter). The result says which one
carried it, and how far the other would differ, so a grade can be read with
its uncertainty.

All vectors are numpy arrays in one right-handed frame (the caller's).
"""
from __future__ import annotations

import numpy as np

LOOK_DOWN_FOR_GROUND = 20.0      # degrees
GROUND_SHARE = 0.6
SPREAD_FOR_TURNING = 0.02        # middle/largest eigenvalue of sum r r^T


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else None


def dominant_plane(points: np.ndarray, *, trials: int = 600, tolerance: float = 0.02,
                   seed: int = 12345):
    """RANSAC plane + two least-squares refits through its inliers.
    Returns (normal, d, share) with normal . p = d, or None."""
    if len(points) < 50:
        return None
    tol = tolerance * float(np.ptp(points, axis=0).max())
    rng = np.random.default_rng(seed)
    best, best_count = None, 0
    for _ in range(trials):
        a, b, c = points[rng.integers(0, len(points), 3)]
        n = _unit(np.cross(b - a, c - a))
        if n is None:
            continue
        count = int((np.abs(points @ n - n @ a) < tol).sum())
        if count > best_count:
            best, best_count = (n, float(n @ a)), count
    if best is None:
        return None
    n, d = best
    for _ in range(2):
        inl = points[np.abs(points @ n - d) < tol]
        if len(inl) < 10:
            break
        c = inl.mean(axis=0)
        w, V = np.linalg.eigh(np.cov((inl - c).T))
        n = V[:, 0]
        d = float(n @ c)
    share = float((np.abs(points @ n - d) < tol).mean())
    return n, d, share


def estimate_up(positions: np.ndarray, directions: np.ndarray, ups: np.ndarray,
                points: np.ndarray) -> dict:
    """The vertical from the cameras (positions, view directions, up vectors --
    in filming order) and a sample of scene points, all in one frame."""
    D = np.array([_unit(d) for d in directions])
    U = np.array([_unit(u) for u in ups])
    mean_up = _unit(U.sum(axis=0))
    R = np.array([_unit(np.cross(d, u)) for d, u in zip(D, U)])
    out = {"mean_up": mean_up}

    # -- the ground, and how far the cameras looked down onto it
    plane = dominant_plane(points)
    look_down = None
    if plane is not None:
        n, d, share = plane
        ground = n if n @ mean_up > 0 else -n
        dg = d if n @ mean_up > 0 else -d
        look_down = float(np.degrees(np.median(np.arcsin(np.clip(-(D @ ground), -1, 1)))))
        above = float(((positions @ ground) > dg).mean())
        out.update(ground=ground, share=share, look_down=look_down)
        if share >= GROUND_SHARE and above >= 0.8 and look_down >= LOOK_DOWN_FOR_GROUND:
            return {**out, "up": ground, "method": "ground"}

    # -- held level
    M = R.T @ R / len(R)
    w, V = np.linalg.eigh(M)                     # ascending
    spread = float(w[1] / max(w[2], 1e-12))
    out["spread"] = spread
    walk = _unit(positions[-1] - positions[0]) if len(positions) > 1 else None
    mean_r = _unit(R.sum(axis=0))
    if walk is not None and mean_r is not None:
        wl = _unit(np.cross(mean_r, walk))
        if wl is not None:
            out["walk_level"] = wl if wl @ mean_up > 0 else -wl
    if spread > SPREAD_FOR_TURNING:
        up = V[:, 0]
        method = "held level"
    else:
        up = _unit(mean_up - (mean_up @ mean_r) * mean_r)
        method = "held level, walk straight"
    if up @ mean_up < 0:
        up = -up
    return {**out, "up": up, "method": method}


def angle_deg(a, b) -> float:
    return float(np.degrees(np.arccos(np.clip(abs(float(np.dot(a, b))), -1, 1))))
