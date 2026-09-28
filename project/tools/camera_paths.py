"""Alternative camera paths for a scan: smoothed and level walks, and classic moves.

    python tools/camera_paths.py <scene name> --out <folder> [--fps 30] [--seconds 12]

Marc (2026-09-27): besides the filmed walk, example camera paths students can explore --
a steadier walk, a walk with a level horizon (verticals parallel), and the moves of aerial
documentaries: orbit, pass-by, rise-and-tilt to a top-down view, push-in, dolly zoom.

Every path is a camera per frame -- position, forward, up, vertical field of view, and a
vertical lens shift -- in site.json's frame (Z up, Y north or the walk, origin on the ground
below the first camera), so it lines up with the .blend, the .3dm and the drawings.

    walk smoothed      the filmed walk, positions and directions filtered over about half a
                       second (Gaussian, sigma --smooth s), timing unchanged
    walk level         the smoothed walk with the horizon level and no tilt: forward
                       horizontal, up = +Z, so vertical lines stay parallel (two-point
                       perspective); the lens shift keeps the ground the phone looked at in
                       frame, as an architectural photographer would
    orbit              around the point of interest, on the side the phone filmed (120 deg)
    pass-by            a straight line past the point, the camera following it
    rise and tilt      from eye level up to straight down onto the point
    push-in            towards the point
    dolly zoom         moving back while zooming in: the point keeps its size, the background
                       grows ("vertigo")
The POINT OF INTEREST is the middle of the scanned patch, on the ground; students can move it
(the paths are made again from it) or pick another object to look at in Blender.

HONEST LIMIT, written into every path: a scan holds only what the phone saw. For each frame
the distance to the nearest FILMED camera (as a share of the walk's length) and the angle
between the two viewing directions are recorded; a frame is "near the filmed views" when it
is within 25 % of the walk and 35 deg. The further a path leaves them, the more gaps and
smeared splats appear -- which is part of what a student can learn from these paths.

Writes into --out:
    <name>_camera_paths.json   every path, every frame (for BLE's .blend builder; CONTRACT)
    <name>_camera_paths.glb    animated cameras + the paths as lines (Blender: File > Import > glTF)
    <name>_camera_paths.dxf    the paths as 3D polylines, one layer each (Rhino, CAD)
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import ground_dem          # noqa: E402
import package_drawings as pdw   # noqa: E402 -- drawn_mesh
import package_terrain as pt     # noqa: E402 -- read_cloud
import hybrid_split as hs  # noqa: E402 -- bilinear
import scene_paths         # noqa: E402
import site_exports        # noqa: E402 -- cameras_site, SITE_TO_GLTF
import splat_levels as sl  # noqa: E402 -- quat_from_matrix
from dxf_r12 import Dxf    # noqa: E402

NEAR_SHARE, NEAR_DEG = 0.25, 35.0
COVER_LONG = 96              # coverage frame: pixels on its long side
COVER_SAMPLES = 1_500_000    # surface samples for the coverage z-buffer
COVER_PCT = 10               # yardstick: this percentile of the smoothed walk's coverage
CHECK_EVERY = 5              # frames checked when trimming


def surface_samples(name: str, n: int = COVER_SAMPLES, seed: int = 7):
    """Area-weighted points on the textured mesh, and whether their triangle was SEEN
    (package_drawings' rule: painted from a frame, not a stretched shard)."""
    V, F, keep = pdw.seen_and_unseen(name)
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(F), n, p=area / area.sum())
    r1, r2 = np.sqrt(rng.random(n)), rng.random(n)
    pts = ((1 - r1)[:, None] * a[idx] + (r1 * (1 - r2))[:, None] * b[idx]
           + (r1 * r2)[:, None] * c[idx])
    return pts.astype(np.float32), keep[idx]


def coverage(samples, pos, fwd, up, fov_v, shift_y, aspect, mask=False):
    """Share of the frame's pixels that show SEEN scan surface. Nearest sample per pixel wins
    (unseen triangles still hide what is behind them); empty pixels -- holes, nothing
    scanned -- are not covered. One-pixel sampling gaps (6 of 8 neighbours filled) take
    their neighbours' majority."""
    pts, seen = samples
    if aspect <= 1:
        H = COVER_LONG
        W = max(1, round(H * aspect))
    else:
        W = COVER_LONG
        H = max(1, round(W / aspect))
    right = np.cross(fwd, up).astype(np.float32)
    d = pts - np.asarray(pos, np.float32)
    z = d @ np.asarray(fwd, np.float32)
    ok = z > 1e-3
    f = (H / 2) / math.tan(fov_v / 2)
    zz = z[ok]
    dd = d[ok]
    u = W / 2 + f * (dd @ right) / zz
    # Blender's shift: a fraction of the larger side, positive moves the frame up
    v = H / 2 - f * (dd @ np.asarray(up, np.float32)) / zz + shift_y * max(W, H)
    iu = np.floor(u).astype(np.int64)
    iv = np.floor(v).astype(np.int64)
    inside = (iu >= 0) & (iu < W) & (iv >= 0) & (iv < H)
    pix = iv[inside] * W + iu[inside]
    state = np.zeros(W * H, np.int8)                              # 0 empty, 1 seen, 2 unseen
    if len(pix):
        zs = zz[inside].astype(np.float64)
        order = np.argsort(pix + zs / (zs.max() * 1.0001))        # by pixel, then nearest
        ps = pix[order]
        first = np.r_[True, ps[1:] != ps[:-1]]
        state[ps[first]] = np.where(seen[ok][inside][order][first], 1, 2)
    state = state.reshape(H, W)
    filled = state > 0

    def nb(m):
        p = np.pad(m.astype(np.int16), 1)
        return sum(p[1 + dy:1 + dy + H, 1 + dx:1 + dx + W]
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx)
    nf, ns = nb(filled), nb(state == 1)
    gap = ~filled & (nf >= 6)
    state[gap] = np.where(2 * ns[gap] >= nf[gap], 1, 2)
    cov = float((state == 1).mean())
    return (cov, state) if mask else cov


def path_coverage(samples, p, aspect, every=CHECK_EVERY, stop_below=None):
    """Coverage of every `every`-th frame (and the last); with stop_below, stops at the
    first frame under it (returns what was measured so far)."""
    idx = list(range(0, len(p["pos"]), every))
    if idx[-1] != len(p["pos"]) - 1:
        idx.append(len(p["pos"]) - 1)
    out = []
    for i in idx:
        c = coverage(samples, p["pos"][i], p["fwd"][i], p["up"][i], p["fov_v"][i], p["shift_y"][i], aspect)
        out.append(c)
        if stop_below is not None and c < stop_below:
            break
    return idx[:len(out)], np.array(out)


def unit(v):
    return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-20)


def ease(s):
    return s * s * (3 - 2 * s)                                   # smoothstep


def look_at(pos, target, up_hint=np.array([0.0, 0.0, 1.0])):
    fwd = unit(target - pos)
    up_h = np.broadcast_to(up_hint, fwd.shape).copy()
    right = np.cross(fwd, up_h)
    bad = np.linalg.norm(right, axis=-1) < 1e-6
    if bad.any():                                                # looking straight down/up
        right[bad] = np.cross(fwd[bad], np.array([0.0, 1.0, 0.0]))
    right = unit(right)
    up = np.cross(right, fwd)
    return fwd, up


def gaussian_smooth(x, sigma):
    """Along axis 0, edges held (reflect)."""
    if sigma <= 0:
        return x.copy()
    r = int(math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    pad = np.concatenate([x[r:0:-1], x, x[-2:-r - 2:-1]], axis=0)
    out = np.stack([np.convolve(pad[:, j], k, mode="valid") for j in range(x.shape[1])], 1)
    return out


def resample(t_src, x, t_out):
    return np.stack([np.interp(t_out, t_src, x[:, j]) for j in range(x.shape[1])], 1)


def build(name: str, fps: float, seconds: float, smooth_s: float) -> dict:
    folder = scene_paths.scene_dir(name)
    site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    rec = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    M = np.asarray(site["matrix"], float)
    cams = site_exports.cameras_site(name, M)
    stride = int((rec.get("settings") or {}).get("stride") or 1)
    src_fps = float(((rec.get("source_meta") or {}).get("image") or {}).get("fps") or rec.get("fps") or 30)
    t_cam = np.array([(c["frame"] - 1) * stride / src_fps for c in cams])
    P = np.array([c["pos"] for c in cams])
    Fw = np.array([c["fwd"] for c in cams])
    Up = np.array([c["up"] for c in cams])
    fov_v = float(np.median([c["fov_v"] for c in cams]))
    aspect = float(np.median([c["aspect"] for c in cams]))
    walk_len = float(np.sum(np.linalg.norm(np.diff(P, axis=0), axis=1)))

    # point of interest: the middle of the drawn patch, on the ground
    V, _, Fk, _, _ = pdw.drawn_mesh(name)
    cxy = V[np.unique(Fk)][:, :2].mean(0)
    ter = json.loads((folder / "mesh" / "terrain.json").read_text(encoding="utf-8"))
    cell = ter["grid"]["cell"]
    sd = scene_paths.site_data_dir(name)
    G, _, _ = pt.read_cloud(sd / "terrain" / f"{name}_ground.ply")
    grid, (gx0, gy1, _, _, _) = ground_dem.rasterise(G, cell, 2)
    grid, _ = ground_dem.fill_holes(grid, 4)
    gz = hs.bilinear(grid, gx0, gy1, cell, np.array([cxy[0]]), np.array([cxy[1]]))[0]
    poi = np.array([cxy[0], cxy[1], float(gz) if np.isfinite(gz) else 0.0])
    eye = float(np.median(P[:, 2] - np.interp(0, [0, 1], [0, 0])))   # camera heights above the origin
    mean_walk = P.mean(0)
    to_walk = mean_walk[:2] - poi[:2]
    dist = float(np.linalg.norm(to_walk))
    side = unit(np.r_[to_walk, 0.0])                                 # from the point towards the filmed side
    n = int(round(seconds * fps)) + 1
    t = np.linspace(0, 1, n)
    paths = {}

    def add(key, label, pos, fwd, up, fov=None, shift=None, note=""):
        paths[key] = {"label": label, "pos": pos, "fwd": unit(fwd), "up": unit(up),
                      "fov_v": np.full(len(pos), fov_v) if fov is None else fov,
                      "shift_y": np.zeros(len(pos)) if shift is None else shift, "note": note}

    # 1-2 the walk, smoothed; and level
    t_out = np.arange(0, t_cam[-1] + 1e-9, 1 / fps)
    # resampled to the frame rate FIRST, then smoothed there (sigma in frames): smooth over half a
    # second AND from frame to frame. !! v1-v5 smoothed the poses (one per 0.1 s) and then joined
    # them with straight segments -- a kink every 3rd frame; in the .blend the per-frame change of
    # the view direction was no smoother than the filmed camera's (check C1, smoothed camera).
    sig = smooth_s * fps
    pos = gaussian_smooth(resample(t_cam, P, t_out), sig)
    fwd = unit(gaussian_smooth(resample(t_cam, Fw, t_out), sig))
    up = unit(gaussian_smooth(resample(t_cam, Up, t_out), sig))
    right = unit(np.cross(fwd, up))
    up = np.cross(right, fwd)
    add("walk_smoothed", "walk, smoothed", pos, fwd, up,
        note=f"the filmed walk filtered over {smooth_s:g} s (Gaussian sigma)")
    fh = fwd.copy()
    fh[:, 2] = 0
    pitch = np.arcsin(np.clip(fwd[:, 2], -1, 1))
    # Blender: a positive shift_y moves the frame UP. Looking down (pitch < 0) the frame must
    # move down, so shift = tan(pitch) / (2 tan(fov/2)) -- a fraction of the frame's height (the
    # larger side of a portrait frame). v1-v3 had the sign flipped: the level walk showed the rock
    # wall above and empty space below (seen on the contact sheet; check R6 added for it).
    shift = np.tan(pitch) / (2 * math.tan(fov_v / 2))
    shift = gaussian_smooth(shift[:, None], fps * 1.0)[:, 0]
    add("walk_level", "walk, level horizon", pos, fh, np.tile([0.0, 0.0, 1.0], (len(pos), 1)), shift=shift,
        note="horizon level, no tilt (verticals parallel); a vertical lens shift keeps the ground in frame")
    # TRIMMED TO WHAT WAS FILMED (Marc, 2026-09-27): each move is made as large as it can be
    # while every checked frame still shows as much scanned surface as nine in ten of the
    # smoothed walk's frames do (coverage >= the yardstick). v1-v4 were fixed-size moves; their
    # orbit and pass-by began on the side the phone never saw (45 % / 49 % coverage).
    samples = surface_samples(name)
    _, walk_cov = path_coverage(samples, paths["walk_smoothed"], aspect)
    yard = float(np.percentile(walk_cov, COVER_PCT))
    s = ease(t)
    trim = {"yardstick": yard, "walk_coverage_min": float(walk_cov.min())}

    def one(p_pos, p_fwd, p_up, fov=fov_v, shift=0.0):
        return coverage(samples, p_pos, p_fwd, p_up, fov, shift, aspect)

    def passes(pp):
        _, c = path_coverage(samples, pp, aspect, stop_below=yard)
        return bool((c >= yard).all())

    def as_path(pos, f_, u_, fov=None):
        return {"pos": pos, "fwd": unit(f_), "up": unit(u_),
                "fov_v": np.full(len(pos), fov_v) if fov is None else fov, "shift_y": np.zeros(len(pos))}

    # 3 orbit on the filmed side: angles whose view reaches the yardstick, the longest run
    # through (or nearest to) the filmed side, at most 120 deg
    ang0 = math.atan2(side[1], side[0])
    r = 0.9 * dist
    step = math.radians(2)
    grid_a = ang0 + np.radians(np.arange(-90, 91, 2))                # -90..90 deg, 2 deg steps

    def orbit_at(a):
        a = np.atleast_1d(a)
        pos = np.stack([poi[0] + r * np.cos(a), poi[1] + r * np.sin(a), np.full(len(a), poi[2] + eye)], 1)
        f_, u_ = look_at(pos, poi)
        return pos, f_, u_
    ok_a = np.array([one(*[x[0] for x in orbit_at(a)]) >= yard for a in grid_a])
    lo, hi = run_around(ok_a, int(np.argmin(np.abs(grid_a - ang0))))
    a_lo, a_hi = (grid_a[lo], grid_a[hi]) if lo is not None else (ang0, ang0)
    if a_hi - a_lo > math.radians(120):
        c = min(max(ang0, a_lo + math.radians(60)), a_hi - math.radians(60))
        a_lo, a_hi = c - math.radians(60), c + math.radians(60)
    while True:
        pp = as_path(*orbit_at(a_lo + (a_hi - a_lo) * s))
        if passes(pp) or a_hi - a_lo < step:
            break
        a_lo, a_hi = a_lo + step / 2, a_hi - step / 2
    span = math.degrees(a_hi - a_lo)
    trim["orbit_deg"] = span
    add("orbit", f"orbit around the point (filmed side, {span:.0f} deg)", pp["pos"], pp["fwd"], pp["up"])

    # 4 pass-by: a line on the filmed side, at right angles to it -- the stretch whose views
    # reach the yardstick, around the point straight across, at most +-0.9 x distance
    across = np.array([-side[1], side[0], 0.0])
    grid_u = np.linspace(-0.9, 0.9, 61) * dist

    def pass_at(u):
        u = np.atleast_1d(u)
        pos = poi + side * dist + across * u[:, None]
        pos[:, 2] = poi[2] + eye
        f_, u_ = look_at(pos, poi)
        return pos, f_, u_
    ok_u = np.array([one(*[x[0] for x in pass_at(u)]) >= yard for u in grid_u])
    lo, hi = run_around(ok_u, len(grid_u) // 2)
    u_lo, u_hi = (grid_u[lo], grid_u[hi]) if lo is not None else (0.0, 0.0)
    du = grid_u[1] - grid_u[0]
    while True:
        pp = as_path(*pass_at(u_lo + (u_hi - u_lo) * s))
        if passes(pp) or u_hi - u_lo < du:
            break
        u_lo, u_hi = u_lo + du / 2, u_hi - du / 2
    trim["pass_by_travel"] = float((u_hi - u_lo) / dist)
    add("pass_by", "pass-by, following the point", pp["pos"], pp["fwd"], pp["up"])

    # 5 rise and tilt: from eye level on the filmed side to straight above the point -- the
    # highest top (at most 1.6 x distance) at which every checked frame reaches the yardstick
    start = poi + side * dist
    start[2] = poi[2] + eye
    tgt = np.tile(poi, (n, 1))
    # "up" in the image turns gradually from vertical to the heading the camera already had
    # (towards the point, horizontally), so the view comes down to straight-down without a roll.
    # Found 2026-09-27: switching "up" to +Y near the top left it off square to the view (by
    # 0.05) and jumped -- the point drifted 80 px off centre (the pre-fixed check R3 caught it).
    hint = unit((1 - s)[:, None] * np.array([0.0, 0.0, 1.0]) + s[:, None] * (-side))
    for h in np.arange(1.6, 0.19, -0.1):
        top = poi + np.array([0, 0, h * dist])
        pos = start[None] * (1 - s)[:, None] + top[None] * s[:, None]
        pp = as_path(pos, *look_at(pos, tgt, hint))
        if passes(pp):
            break
    trim["rise_top"] = float(h)
    add("rise_and_tilt", "rise and tilt to straight down", pp["pos"], pp["fwd"], pp["up"])

    # 6 push-in and 7 dolly zoom: the furthest start (at most 1.8 x distance) at which every
    # checked frame reaches the yardstick; the near end stays at 0.4 x distance
    near = poi + side * 0.4 * dist
    near[2] = poi[2] + eye * 0.8
    d0 = np.linalg.norm(near - poi)
    width = 2 * d0 * math.tan(fov_v / 2)

    def far_at(F):
        far = poi + side * F * dist
        far[2] = poi[2] + eye * (0.8 + 0.8 * (F - 0.4) / 1.4)     # 1.6 x eye at F = 1.8, as v1-v4
        return far
    for F in np.arange(1.8, 0.75, -0.1):
        pos = far_at(F)[None] * (1 - s)[:, None] + near[None] * s[:, None]
        pp = as_path(pos, *look_at(pos, np.tile(poi, (n, 1))))
        if passes(pp):
            break
    trim["push_in_start"] = float(F)
    add("push_in", "push-in towards the point", pp["pos"], pp["fwd"], pp["up"])
    # dolly zoom: back from near to far, the field of view narrowing so the point keeps its size
    for F in np.arange(1.8, 0.75, -0.1):
        pos = near[None] * (1 - s)[:, None] + far_at(F)[None] * s[:, None]
        fov = 2 * np.arctan(width / (2 * np.linalg.norm(pos - poi, axis=1)))
        pp = as_path(pos, *look_at(pos, np.tile(poi, (n, 1))), fov=fov)
        if passes(pp):
            break
    trim["dolly_end"] = float(F)
    add("dolly_zoom", "dolly zoom (the point keeps its size)", pp["pos"], pp["fwd"], pp["up"], fov=pp["fov_v"])

    for p in paths.values():
        idx, c = path_coverage(samples, p, aspect)
        p["coverage"] = {"every": CHECK_EVERY, "frames": idx, "values": c}

    # nearness to the filmed views
    for p in paths.values():
        dd = np.linalg.norm(p["pos"][:, None, :] - P[None], axis=2)
        k = dd.argmin(1)
        p["near_dist"] = dd[np.arange(len(k)), k] / walk_len
        p["near_deg"] = np.degrees(np.arccos(np.clip(np.sum(p["fwd"] * Fw[k], 1), -1, 1)))
        p["near_ok"] = (p["near_dist"] <= NEAR_SHARE) & (p["near_deg"] <= NEAR_DEG)
    return {"paths": paths, "poi": poi, "fps": fps, "fov_v": fov_v, "aspect": aspect, "walk_len": walk_len,
            "site": site, "filmed": {"pos": P, "fwd": Fw}, "trim": trim}


def run_around(ok, centre):
    """The run of True in `ok` through index `centre` -- or, when that one is False, through
    the nearest True. (first, last) indices, or (None, None) when nothing is True."""
    good = np.nonzero(ok)[0]
    if not len(good):
        return None, None
    c = centre if ok[centre] else int(good[np.argmin(np.abs(good - centre))])
    lo = c
    while lo > 0 and ok[lo - 1]:
        lo -= 1
    hi = c
    while hi < len(ok) - 1 and ok[hi + 1]:
        hi += 1
    return lo, hi


def write_json(path, name, b):
    out = {"format": "dlcamerapaths", "version": 1, "scene": name, "fps": b["fps"],
           "frame": "site.json (Z up)", "units": b["site"]["units"],
           "point_of_interest": [round(float(v), 6) for v in b["poi"]],
           "aspect": b["aspect"],
           "note": "per frame: position, forward, up (unit), fov_v (radians, vertical), shift_y "
                   "(vertical lens shift as a fraction of the frame's height); near_* = distance to the "
                   "nearest FILMED camera as a share of the walk, and the angle between the views; "
                   "coverage = share of the frame showing SEEN scan surface, every 5th frame",
           "coverage_yardstick": round(b["trim"]["yardstick"], 4),
           "trim": {k: round(v, 4) for k, v in b["trim"].items()},
           "paths": {}}
    for k, p in b["paths"].items():
        out["paths"][k] = {
            "label": p["label"], "note": p["note"], "frames": len(p["pos"]),
            "near_filmed_share": round(float(p["near_ok"].mean()), 3),
            "coverage_min": round(float(p["coverage"]["values"].min()), 4),
            "coverage": {"frames": p["coverage"]["frames"],
                         "values": np.round(p["coverage"]["values"], 4).tolist()},
            "pos": np.round(p["pos"], 5).tolist(), "fwd": np.round(p["fwd"], 6).tolist(),
            "up": np.round(p["up"], 6).tolist(), "fov_v": np.round(p["fov_v"], 6).tolist(),
            "shift_y": np.round(p["shift_y"], 5).tolist(),
            "near_dist": np.round(p["near_dist"], 4).tolist(), "near_deg": np.round(p["near_deg"], 2).tolist()}
    path.write_text(json.dumps(out), encoding="utf-8")


def write_glb(path, name, b):
    C = site_exports.SITE_TO_GLTF
    bins, views, accs = [], [], []

    def acc(arr, ctype, typ, target=None, minmax=False):
        a = np.ascontiguousarray(arr)
        off = sum(len(x) for x in bins)
        pad = (-off) % 4
        if pad:
            bins.append(b"\0" * pad)
            off += pad
        bins.append(a.tobytes())
        v = {"buffer": 0, "byteOffset": off, "byteLength": a.nbytes}
        if target:
            v["target"] = target
        views.append(v)
        d = {"bufferView": len(views) - 1, "componentType": ctype, "count": int(len(a)), "type": typ}
        if minmax:
            d["min"] = a.min(0).tolist() if a.ndim > 1 else [float(a.min())]
            d["max"] = a.max(0).tolist() if a.ndim > 1 else [float(a.max())]
        accs.append(d)
        return len(accs) - 1

    nodes, cams, meshes, anims = [], [], [], []
    mats = [{"name": "path", "pbrMetallicRoughness": {"baseColorFactor": [0.9, 0.47, 0.12, 1], "metallicFactor": 0}}]
    for k, p in b["paths"].items():
        n = len(p["pos"])
        times = acc((np.arange(n) / b["fps"]).astype("<f4"), 5126, "SCALAR", minmax=True)
        tr = acc((p["pos"] @ C.T).astype("<f4"), 5126, "VEC3")
        right = np.cross(p["fwd"], p["up"])
        q = np.array([sl.quat_from_matrix(C @ np.stack([r, u, -f], 1))
                      for r, u, f in zip(right, p["up"], p["fwd"])])
        # keep the quaternions on one hemisphere, or the interpolation spins the long way round
        for i in range(1, len(q)):
            if np.dot(q[i], q[i - 1]) < 0:
                q[i] = -q[i]
        rot = acc(q[:, [1, 2, 3, 0]].astype("<f4"), 5126, "VEC4")
        cams.append({"type": "perspective", "name": f"{name} camera - {p['label']}",
                     "perspective": {"yfov": float(p["fov_v"][0]), "aspectRatio": b["aspect"], "znear": 0.01}})
        nodes.append({"name": f"{name} camera - {p['label']}", "camera": len(cams) - 1,
                      "translation": (p["pos"][0] @ C.T).tolist(), "rotation": q[0, [1, 2, 3, 0]].tolist(),
                      "extras": {"path": k, "note": p["note"], "near_filmed_share": float(p["near_ok"].mean()),
                                 "shift_y_first": float(p["shift_y"][0]),
                                 "fov_animated": bool(np.ptp(p["fov_v"]) > 1e-6)}})
        ni = len(nodes) - 1
        # ONE animation for all cameras: Blender's importer makes an action per glTF animation
        # and plays only the first -- found 2026-09-27 (six of seven cameras stood still)
        if not anims:
            anims.append({"name": f"{name} camera paths", "samplers": [], "channels": []})
        smp = anims[0]["samplers"]
        smp += [{"input": times, "output": tr, "interpolation": "LINEAR"},
                {"input": times, "output": rot, "interpolation": "LINEAR"}]
        anims[0]["channels"] += [{"sampler": len(smp) - 2, "target": {"node": ni, "path": "translation"}},
                                 {"sampler": len(smp) - 1, "target": {"node": ni, "path": "rotation"}}]
        line = acc((p["pos"] @ C.T).astype("<f4"), 5126, "VEC3", 34962, True)
        meshes.append({"name": f"{name} path - {p['label']}",
                       "primitives": [{"attributes": {"POSITION": line}, "mode": 3, "material": 0}]})
        nodes.append({"name": f"{name} path - {p['label']}", "mesh": len(meshes) - 1})
    nodes.append({"name": f"{name} point of interest", "translation": (b["poi"] @ C.T).tolist()})
    doc = {"asset": {"version": "2.0", "generator": "DL-SplatGenerator tools/camera_paths.py (Digital Landscapes)",
                     "extras": {"frame": "site.json, Z up (written Y-up)", "units": b["site"]["units"]}},
           "scene": 0, "scenes": [{"name": f"{name} camera paths", "nodes": list(range(len(nodes)))}],
           "nodes": nodes, "cameras": cams, "meshes": meshes, "materials": mats, "animations": anims,
           "accessors": accs, "bufferViews": views, "buffers": [{"byteLength": sum(len(x) for x in bins)}]}
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    blob = b"".join(bins)
    blob += b"\0" * ((-len(blob)) % 4)
    with path.open("wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(blob)))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        fh.write(struct.pack("<II", len(blob), 0x004E4942) + blob)


def write_dxf(path, name, b):
    d = Dxf(b["site"]["scale"]["m_per_unit"] is not None)
    colours = [30, 5, 1, 3, 4, 6, 2, 8]
    for i, (k, p) in enumerate(b["paths"].items()):
        lay = d.layer(f"{name} path - {p['label']}", colours[i % len(colours)])
        d.polyline(lay, p["pos"])
    lay = d.layer(f"{name} point of interest", 1)
    d.point(lay, b["poi"])
    d.write(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--smooth", type=float, default=0.5, help="walk smoothing, seconds (Gaussian sigma)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    n = args.name
    files = [out / f"{n}_camera_paths.{e}" for e in ("json", "glb", "dxf")]
    for f in files:
        if f.exists():
            print(f"FAILED: {f} exists -- never overwritten", file=sys.stderr)
            return 2
    b = build(n, args.fps, args.seconds, args.smooth)
    write_json(files[0], n, b)
    write_glb(files[1], n, b)
    write_dxf(files[2], n, b)
    print(f"    coverage yardstick (walk's {COVER_PCT}th percentile): {b['trim']['yardstick']:.3f}; trim "
          + ", ".join(f"{k} {v:.3g}" for k, v in b["trim"].items() if k != "yardstick"))
    for k, p in b["paths"].items():
        print(f"    {p['label']:48s} {len(p['pos']):4d} frames, near the filmed views {p['near_ok'].mean():5.0%}, "
              f"coverage min {p['coverage']['values'].min():.3f}")
    for f in files:
        print(f"    written {f.name}  ({f.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
