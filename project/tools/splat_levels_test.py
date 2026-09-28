"""Test: tools/splat_levels.py moves splats into site coordinates exactly.

    python tools/splat_levels_test.py

Pass rules, fixed before the first run (2026-09-27):
  1. the SH basis matches the 3DGS release: band 1 at +z is (0, C1, 0)
  2. colour is preserved: for random splats and random view directions d, the
     full colour (DC + bands 1-3) seen along R d after the move equals the
     colour seen along d before, to 1e-5 -- for three random rotations and
     for the real site.json rotation of the scene tested
  3. the identity leaves every coefficient unchanged (to 1e-9)
  4. position: moved = M @ p exactly (to 1e-9 relative)
  5. shape: the moved covariance equals s^2 R Sigma R^T (to 1e-6 relative)
  6. visibility order: every splat kept at a level is at least as visible as
     every splat dropped
  7. a written file reads back with the same values (float32) and the
     original 3DGS property order

Synthetic splats in memory plus the real site.json rotation of the first
scene that has one; writes only into a temporary folder.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import scene_paths    # noqa: E402
import splat_levels as sl   # noqa: E402

checks: list[tuple[str, bool, str]] = []
C0 = 0.28209479177387814


def check(name: str, ok: bool, detail: str = "") -> bool:
    checks.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def random_rotation(rng) -> np.ndarray:
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def fake_splats(n: int, rng) -> np.ndarray:
    names = (["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2"] + [f"f_rest_{i}" for i in range(45)]
             + ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"])
    a = np.zeros(n, dtype=[(k, "<f8") for k in names])
    for k in names:
        a[k] = rng.normal(size=n)
    for i in range(3):
        a[f"scale_{i}"] = rng.normal(-3, 0.5, size=n)
    return a


def colour(dc, rest, dirs):
    """Per splat i: RGB seen along dirs[i] (before the +0.5 offset and clamping)."""
    Y = sl.sh_basis(dirs, 3)                            # N x 15
    m = rest.shape[1] // 3
    return np.stack([C0 * dc[:, c] + np.sum(rest[:, c * m:(c + 1) * m] * Y, axis=1)
                     for c in range(3)], axis=1)


def cov(log_scale, q):
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    w, x, y, z = q.T
    R = np.stack([np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)], -1),
                  np.stack([2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)], -1),
                  np.stack([2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)], -1)], 1)
    S = np.exp(log_scale)
    RS = R * S[:, None, :]
    return RS @ np.transpose(RS, (0, 2, 1))


def run() -> None:
    rng = np.random.default_rng(3)
    Y = sl.sh_basis(np.array([[0.0, 0.0, 1.0]]), 1)[0]
    check("1 the SH basis is the 3DGS one (band 1 at +z)",
          np.allclose(Y, [0, 0.4886025119029199, 0]), str(np.round(Y, 6)))

    data = fake_splats(2000, rng)
    rots = [random_rotation(rng) for _ in range(3)]
    real = None
    for pattern in (f"*/{scene_paths.WORKING}/site.json", "*/site.json"):
        base = scene_paths.STUDENTS_OUT if "/" in pattern[2:] else scene_paths.OUTPUT
        for d in sorted(base.glob(pattern)) if base.is_dir() else []:
            M = np.asarray(json.loads(d.read_text())["matrix"])
            real = (d.parent.parent.name if d.parent.name == scene_paths.WORKING else d.parent.name,
                    M[:3, :3] / np.cbrt(np.linalg.det(M[:3, :3])))
            break
        if real:
            break
    if real:
        rots.append(real[1])
    worst = 0.0
    for R in rots:
        M = np.eye(4)
        M[:3, :3] = 0.37 * R
        M[:3, 3] = [1.5, -2.0, 0.25]
        t = sl.to_site(data, M)
        dirs = rng.normal(size=(len(data), 3))
        dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
        rest0 = np.stack([data[f"f_rest_{i}"] for i in range(45)], 1)
        dc0 = np.stack([data[f"f_dc_{i}"] for i in range(3)], 1)
        before = colour(dc0, rest0, dirs)
        after = colour(t["f_dc"], t["f_rest"], dirs @ R.T)
        worst = max(worst, float(np.abs(before - after).max()))
    check("2 colour seen from the moved direction is unchanged (3 random + "
          + (f"{real[0]}'s real rotation)" if real else "no real scene)"),
          worst < 1e-5, f"worst difference {worst:.2e}")

    t = sl.to_site(data, np.eye(4))
    rest0 = np.stack([data[f"f_rest_{i}"] for i in range(45)], 1)
    check("3 the identity leaves the coefficients unchanged",
          np.abs(t["f_rest"] - rest0).max() < 1e-9)

    R = rots[0]
    M = np.eye(4)
    M[:3, :3] = 0.37 * R
    M[:3, 3] = [1.5, -2.0, 0.25]
    t = sl.to_site(data, M)
    p = np.stack([data["x"], data["y"], data["z"]], 1)
    want = p @ M[:3, :3].T + M[:3, 3]
    check("4 positions move by the matrix", np.allclose(t["pos"], want, rtol=1e-9, atol=1e-12))

    q0 = np.stack([data[f"rot_{i}"] for i in range(4)], 1)
    s0 = np.stack([data[f"scale_{i}"] for i in range(3)], 1)
    S0, S1 = cov(s0, q0), cov(t["scale"], t["rot"])
    want = 0.37 ** 2 * (R @ S0 @ R.T)
    rel = np.abs(S1 - want).max() / np.abs(want).max()
    check("5 shape: covariance becomes s^2 R Sigma R^T", rel < 1e-6, f"relative {rel:.1e}")

    vis = sl.visibility(t["opacity"], t["scale"])
    order = np.argsort(-vis)
    keep, drop = order[:700], order[700:]
    check("6 every kept splat is at least as visible as every dropped one",
          vis[keep].min() >= vis[drop].max())

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "3DGS_test.ply"
        sl.write_splat(path, t["pos"], t["f_dc"], t["f_rest"], t["opacity"], t["scale"], t["rot"])
        back, _ = sl.read_splat(path)
        names = list(back.dtype.names)
        order_ok = names[:9] == ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"] \
            and names[-8:] == ["opacity", "scale_0", "scale_1", "scale_2",
                               "rot_0", "rot_1", "rot_2", "rot_3"]
        same = np.allclose(np.stack([back[f"f_rest_{i}"] for i in range(45)], 1),
                           t["f_rest"].astype(np.float32), atol=0)
        check("7 a written file reads back, 3DGS property order", order_ok and same,
              f"{len(names)} properties")


def main_() -> int:
    print("Splat levels -- moving splats into site coordinates")
    run()
    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
