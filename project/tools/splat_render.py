"""A reference 3D Gaussian Splatting renderer in numpy -- for measuring, not for viewing.

    python tools/splat_render.py <splat.ply> <colmap model folder> <image name> <out.png> [--scale 0.5]

Renders a 3DGS .ply (in the frame of a COLMAP model: the scene file's own frame) at one
of that model's cameras, the way the original 3DGS release defines it:

    each Gaussian -> its 2D covariance  J W Sigma W^T J^T + 0.3 I   (the EWA projection
                     with the usual 0.3 px^2 low-pass), footprint to 3 sigma
    colour          -> DC + spherical harmonics to degree 3, viewing direction from the
                     camera to the Gaussian (the reference sign), + 0.5, clamped at 0
    opacity         -> sigmoid; a contribution under 1/255 is skipped, alpha capped at 0.99
    blending        -> front to back by depth, over black; a pixel stops at transmittance 1e-4

Pure numpy, vectorised in depth-ordered batches: within a batch every (Gaussian, pixel) pair
is formed, sorted by pixel (stable, so depth order is kept), and the transmittance each pair
sees is a segment-wise cumulative product -- the same result as the sequential loop.

Written 2026-09-27 to compare splats that Brush did not train itself (thinned versions) with
ones it did, on held-out frames, with ONE renderer. It is checked against Brush's own renders
before its numbers are used (tools notes in test outputs\\2026-09-27\\budget training).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import splat_levels as sl   # noqa: E402 -- read_splat, sh_basis
import mesh_texture as mt   # noqa: E402 -- load_cameras (undistorted PINHOLE)

C0 = 0.28209479177387814


def quat_to_rot(q):
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    w, x, y, z = q.T
    return np.stack([np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)], -1),
                     np.stack([2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)], -1),
                     np.stack([2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)], -1)], 1)


def render(data, cam, scale=0.5, batch_pairs=12_000_000):
    W, H = int(round(cam["W"] * scale)), int(round(cam["H"] * scale))
    fx, fy = cam["fx"] * scale, cam["fy"] * scale
    cx, cy = cam["cx"] * scale, cam["cy"] * scale
    R, t = cam["R"], cam["t"]
    centre = -R.T @ t
    P = np.stack([data["x"], data["y"], data["z"]], 1).astype(np.float64)
    Pc = P @ R.T + t
    z = Pc[:, 2]
    ok = z > 0.01
    u = fx * Pc[:, 0] / np.where(ok, z, 1) + cx
    v = fy * Pc[:, 1] / np.where(ok, z, 1) + cy
    # covariance, projected
    S = np.exp(np.stack([data[f"scale_{i}"] for i in range(3)], 1).astype(np.float64))
    Rg = quat_to_rot(np.stack([data[f"rot_{i}"] for i in range(4)], 1).astype(np.float64))
    M = Rg * S[:, None, :]
    Sig = M @ np.transpose(M, (0, 2, 1))
    Sc = R @ Sig @ R.T
    zz = np.where(ok, z, 1)
    J = np.zeros((len(P), 2, 3))
    J[:, 0, 0] = fx / zz
    J[:, 0, 2] = -fx * Pc[:, 0] / zz ** 2
    J[:, 1, 1] = fy / zz
    J[:, 1, 2] = -fy * Pc[:, 1] / zz ** 2
    S2 = J @ Sc @ np.transpose(J, (0, 2, 1))
    a, b, c = S2[:, 0, 0] + 0.3, S2[:, 0, 1], S2[:, 1, 1] + 0.3
    det = a * c - b * b
    ok &= det > 1e-12
    inv_a, inv_b, inv_c = c / det, -b / det, a / det
    lam = 0.5 * (a + c) + np.sqrt(np.maximum(0.25 * (a - c) ** 2 + b * b, 0))
    rad = np.ceil(3 * np.sqrt(lam)).astype(np.int64)
    ok &= (u + rad >= 0) & (u - rad < W) & (v + rad >= 0) & (v - rad < H) & (rad < max(W, H))
    # colour by viewing direction (camera -> Gaussian)
    k = sl.sh_rest_count(data.dtype.names)
    deg = int(round(np.sqrt(k / 3 + 1))) - 1
    d = P - centre
    d /= np.linalg.norm(d, axis=1, keepdims=True) + 1e-20
    Y = sl.sh_basis(d, deg) if deg > 0 else np.zeros((len(P), 0))
    m = k // 3
    rest = np.stack([data[f"f_rest_{i}"] for i in range(k)], 1).astype(np.float64) if k else np.zeros((len(P), 0))
    col = np.stack([C0 * data[f"f_dc_{ch}"] + (np.sum(rest[:, ch * m:(ch + 1) * m] * Y, 1) if k else 0)
                    for ch in range(3)], 1) + 0.5
    col = np.maximum(col, 0)
    op = 1 / (1 + np.exp(-np.asarray(data["opacity"], np.float64)))

    idx = np.nonzero(ok)[0]
    idx = idx[np.argsort(z[idx], kind="stable")]              # near first
    logT = np.zeros(W * H)
    img = np.zeros((W * H, 3))
    # batches cut by their PAIR count, not by Gaussians: one large Gaussian near the camera
    # can cover a million pixels, and a fixed Gaussian count would then run out of memory
    pairs = np.cumsum((2 * rad[idx] + 1) ** 2)
    cuts = np.searchsorted(pairs, np.arange(batch_pairs, pairs[-1] + batch_pairs, batch_pairs)) if len(idx) else []
    bounds = np.unique(np.r_[0, np.minimum(np.asarray(cuts) + 1, len(idx)), len(idx)])
    for s0, s1 in zip(bounds[:-1], bounds[1:]):
        g = idx[s0:s1]
        r = rad[g]
        side = 2 * r + 1
        n = side * side
        tot = int(n.sum())
        gid = np.repeat(np.arange(len(g)), n)
        start = np.repeat(np.cumsum(n) - n, n)
        loc = np.arange(tot) - start
        sd = side[gid]
        px = np.floor(u[g]).astype(np.int64)[gid] - r[gid] + loc % sd
        py = np.floor(v[g]).astype(np.int64)[gid] - r[gid] + loc // sd
        inside = (px >= 0) & (px < W) & (py >= 0) & (py < H)
        gid, px, py = gid[inside], px[inside], py[inside]
        dx = px + 0.5 - u[g][gid]
        dy = py + 0.5 - v[g][gid]
        gg = g[gid]
        power = -0.5 * (inv_a[gg] * dx * dx + 2 * inv_b[gg] * dx * dy + inv_c[gg] * dy * dy)
        alpha = np.minimum(0.99, op[gg] * np.exp(np.minimum(power, 0)))
        keep = alpha >= 1 / 255
        gid, px, py, alpha, gg = gid[keep], px[keep], py[keep], alpha[keep], gg[keep]
        pix = py * W + px
        order = np.argsort(pix, kind="stable")                  # depth order kept within a pixel
        pix, alpha, gg = pix[order], alpha[order], gg[order]
        la = np.log1p(-alpha)
        csum = np.cumsum(la)
        first = np.r_[True, pix[1:] != pix[:-1]]
        seg_start = np.maximum.accumulate(np.where(first, np.arange(len(pix)), 0))
        before = csum - la - (csum[seg_start] - la[seg_start])  # exclusive, within the pixel
        Tb = np.exp(logT[pix] + before)
        w = np.where(Tb > 1e-4, Tb * alpha, 0.0)
        for ch in range(3):
            img[:, ch] += np.bincount(pix, w * col[gg, ch], minlength=W * H)
        logT += np.bincount(pix, la, minlength=W * H)
    out = np.clip(img.reshape(H, W, 3), 0, 1)
    return out, 1 - np.exp(logT).reshape(H, W)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ply")
    ap.add_argument("model")
    ap.add_argument("image")
    ap.add_argument("out")
    ap.add_argument("--scale", type=float, default=0.5)
    args = ap.parse_args()
    data, _ = sl.read_splat(Path(args.ply))
    cams = {c["name"]: c for c in mt.load_cameras(Path(args.model))}
    img, _ = render(data, cams[args.image], args.scale)
    cv2.imwrite(args.out, (img[:, :, ::-1] * 255 + 0.5).astype(np.uint8))
    return 0


if __name__ == "__main__":
    sys.exit(main())
