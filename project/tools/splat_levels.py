"""A scene's splat in site coordinates, at three sizes: 3DGS_high / _med / _low.ply.

    python tools/splat_levels.py <scene name> [--out <folder>] [--med 400000] [--low 150000]

For the student package (pipeline\\requests\\004): the Gaussian splat as standard
3DGS .ply files that line up with the package's mesh, cameras and contours --
i.e. in site.json's frame (tools/site_frame.py: Z up, Y north or the walk,
origin on the ground, metres when scaled) -- at three sizes for three kinds of
laptop:

    <name>_3DGS_high.ply   every splat
    <name>_3DGS_med.ply    the MED most visible          (default 400,000)
    <name>_3DGS_low.ply    the LOW most visible          (default 150,000)

"Most visible" = opacity x the splat's size squared (its projected area, roughly):
a faint or tiny splat is dropped first. The smaller files lose fine detail and
some fill; they keep the look of what remains.

!! BETTER: TRAIN A LEVEL AT ITS SIZE (--trained low=<ply>). Measured 2026-09-27 on
A student scan, six frames held out of training (test outputs\\2026-09-27\\budget training):
a splat trained with Brush's --max-splats 150,000 scored MAE 0.046 against the
video, the full splat thinned to 150,000 here 0.280; at 50,000, 0.063 against
0.386. Thinning keeps the big Gaussians and drops the many small ones the
surfaces are made of: fine from an overview (the viewer comparison passed it),
dark and full of holes from where the phone stood. A trained file only has to be
moved to site coordinates -- nothing is dropped. Below the sparse model's point
count Brush ignores the cap: start it from fewer points (--subsample-points).

MOVING A SPLAT MOVES FOUR THINGS, and each is done exactly:
  position     [site, 1] = site.json's matrix @ [p, 1]
  rotation     q' = q_R * q            (R = the matrix's rotation)
  size         log-scales + ln(s)       (s = metres per unit, 1 when not scaled)
  colour       the view-dependent colour (spherical harmonics, degrees 1-3) is
               ROTATED with the splat. Without this, a colour that changes with
               the viewing angle would change towards the wrong side. The
               rotation per degree is solved on 256 directions (least squares on
               the exact real-SH basis of 3DGS) -- tested in splat_levels_test.py.

The files are written in the property order of the original 3DGS release
(x y z nx ny nz f_dc_0..2 f_rest_0..44 opacity scale_0..2 rot_0..3), the order
most importers expect. Nothing is written into the scene itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import scene_paths   # noqa: E402

# ------------------------------------------------------------------ PLY in/out

NP = {"float": "<f4", "float32": "<f4", "double": "<f8", "uchar": "u1", "uint8": "u1",
      "int": "<i4", "uint": "<u4", "short": "<i2", "ushort": "<u2", "char": "i1"}


def read_splat(path: Path) -> tuple[np.ndarray, list[str]]:
    """A binary little-endian 3DGS .ply -> (structured array, header comments)."""
    with path.open("rb") as fh:
        head = b""
        while b"end_header" not in head:
            line = fh.readline()
            if not line:
                raise ValueError(f"{path}: no PLY header")
            head += line
        text = head.decode("ascii", "replace")
        if "binary_little_endian" not in text:
            raise ValueError(f"{path}: not a binary little-endian PLY")
        count, fields, comments = 0, [], []
        for line in text.splitlines():
            p = line.split()
            if p[:2] == ["element", "vertex"]:
                count = int(p[2])
            elif len(p) == 3 and p[0] == "property":
                fields.append((p[2], NP[p[1]]))
            elif p[:1] == ["comment"]:
                comments.append(line[8:])
        data = np.frombuffer(fh.read(), dtype=np.dtype(fields), count=count)
    return data, comments


def sh_rest_count(names) -> int:
    return sum(1 for n in names if n.startswith("f_rest_"))


def write_splat(path: Path, pos, f_dc, f_rest, opacity, scale, rot, comments=()) -> None:
    n = len(pos)
    k = f_rest.shape[1]
    names = (["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"]
             + [f"f_rest_{i}" for i in range(k)]
             + ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"])
    out = np.empty(n, dtype=np.dtype([(nm, "<f4") for nm in names]))
    cols = np.concatenate([pos, np.zeros((n, 3)), f_dc, f_rest, opacity[:, None], scale, rot],
                          axis=1).astype("<f4")
    for i, nm in enumerate(names):
        out[nm] = cols[:, i]
    head = ["ply", "format binary_little_endian 1.0"]
    head += [f"comment {c}" for c in comments]
    head += [f"element vertex {n}"] + [f"property float {nm}" for nm in names] + ["end_header"]
    with path.open("wb") as fh:
        fh.write(("\n".join(head) + "\n").encode("ascii"))
        fh.write(out.tobytes())


# ------------------------------------------------------ real spherical harmonics

def sh_basis(d: np.ndarray, degree: int) -> np.ndarray:
    """The 3DGS real-SH basis (bands 1..degree, DC excluded) at unit directions
    d (N x 3): the same constants and signs as the original release's eval_sh."""
    x, y, z = d[:, 0], d[:, 1], d[:, 2]
    cols = []
    if degree >= 1:
        C1 = 0.4886025119029199
        cols += [-C1 * y, C1 * z, -C1 * x]
    if degree >= 2:
        C2 = [1.0925484305920792, -1.0925484305920792, 0.31539156525252005,
              -1.0925484305920792, 0.5462742152960396]
        xx, yy, zz = x * x, y * y, z * z
        cols += [C2[0] * x * y, C2[1] * y * z, C2[2] * (2 * zz - xx - yy),
                 C2[3] * x * z, C2[4] * (xx - yy)]
    if degree >= 3:
        C3 = [-0.5900435899266435, 2.890611442640554, -0.4570457994644658,
              0.3731763325901154, -0.4570457994644658, 1.445305721320277,
              -0.5900435899266435]
        cols += [C3[0] * y * (3 * xx - yy), C3[1] * x * y * z,
                 C3[2] * y * (4 * zz - xx - yy), C3[3] * z * (2 * zz - 3 * xx - 3 * yy),
                 C3[4] * x * (4 * zz - xx - yy), C3[5] * z * (xx - yy),
                 C3[6] * x * (xx - 3 * yy)]
    return np.stack(cols, axis=1)


def sh_rotation(R: np.ndarray, degree: int) -> np.ndarray:
    """D with   colour'(R d) = colour(d)   for coefficient rows C' = C @ D.

    Per band l the basis transforms linearly: Y_l(R^T d) = D_l Y_l(d). Solved
    by least squares on 256 spread directions -- exact up to rounding, because
    each band is closed under rotation."""
    rng = np.random.default_rng(7)
    d = rng.normal(size=(256, 3))
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    A, B = sh_basis(d, degree), sh_basis(d @ R, degree)   # rows: Y(d), Y(R^T d)
    m = A.shape[1]
    D = np.zeros((m, m))
    start = 0
    for l in range(1, degree + 1):
        w = 2 * l + 1
        sl = slice(start, start + w)
        Dt, *_ = np.linalg.lstsq(A[:, sl], B[:, sl], rcond=None)   # B = A @ D_l^T
        # colour(R^T d') = c . D_l Y(d')  ->  c' = D_l^T c  ->  rows: C' = C @ D_l
        D[sl, sl] = Dt.T
        start += w
    return D


# ------------------------------------------------------------ the transform

def quat_from_matrix(R: np.ndarray) -> np.ndarray:
    """(w, x, y, z) of a proper rotation matrix."""
    t = np.trace(R)
    if t > 0:
        s = 2 * np.sqrt(t + 1)
        q = [0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s]
    else:
        i = int(np.argmax(np.diag(R)))
        j, k = (i + 1) % 3, (i + 2) % 3
        s = 2 * np.sqrt(1 + R[i, i] - R[j, j] - R[k, k])
        q = [0.0, 0.0, 0.0, 0.0]
        q[0] = (R[k, j] - R[j, k]) / s
        q[i + 1] = 0.25 * s
        q[j + 1] = (R[j, i] + R[i, j]) / s
        q[k + 1] = (R[k, i] + R[i, k]) / s
    q = np.asarray(q)
    return q / np.linalg.norm(q)


def quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a (4,) times each row of b (N x 4), (w, x, y, z)."""
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    return np.stack([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                     w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2], axis=1)


def to_site(data: np.ndarray, M: np.ndarray) -> dict:
    """Every splat of `data` moved by the 4x4 `M` (a scaled rotation + a shift)."""
    A = M[:3, :3]
    s = float(np.cbrt(np.linalg.det(A)))
    R = A / s
    names = data.dtype.names
    k = sh_rest_count(names)
    degree = int(round(np.sqrt(k / 3 + 1))) - 1
    pos = np.stack([data["x"], data["y"], data["z"]], 1).astype(np.float64)
    rot = np.stack([data[f"rot_{i}"] for i in range(4)], 1).astype(np.float64)
    rot /= np.linalg.norm(rot, axis=1, keepdims=True)
    f_dc = np.stack([data[f"f_dc_{i}"] for i in range(3)], 1)
    rest = np.stack([data[f"f_rest_{i}"] for i in range(k)], 1).astype(np.float64)
    if k:
        D = sh_rotation(R, degree)
        m = k // 3                           # per channel, channel-major (R..., G..., B...)
        rest = np.concatenate([rest[:, c * m:(c + 1) * m] @ D for c in range(3)], axis=1)
    return {
        "pos": pos @ A.T + M[:3, 3],
        "rot": quat_mul(quat_from_matrix(R), rot),
        "scale": np.stack([data[f"scale_{i}"] for i in range(3)], 1) + np.log(s),
        "opacity": np.asarray(data["opacity"], np.float64),
        "f_dc": f_dc, "f_rest": rest, "degree": degree, "s": s,
    }


def visibility(opacity_logit: np.ndarray, log_scale: np.ndarray) -> np.ndarray:
    """Opacity x size squared: what a splat contributes to the picture, roughly."""
    return (1 / (1 + np.exp(-opacity_logit))) * np.exp(2 * log_scale.mean(axis=1))


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--out", default=None, help="folder for the three files "
                    "(default: the package's site data/splat/)")
    ap.add_argument("--med", type=int, default=400_000)
    ap.add_argument("--low", type=int, default=150_000)
    ap.add_argument("--verylow", type=int, default=50_000)
    ap.add_argument("--only", default="high,med,low",
                    help="which levels to write, comma-separated (high, med, low, verylow)")
    ap.add_argument("--trained", action="append", default=[], metavar="LEVEL=PLY",
                    help="take this level from a splat TRAINED at its size (same scene frame as the "
                         "scene's own splat) instead of thinning; repeatable")
    args = ap.parse_args()
    trained = {}
    for item in args.trained:
        label, _, path = item.partition("=")
        if not path or not Path(path).is_file():
            print(f"FAILED: --trained {item!r}: no such file", file=sys.stderr)
            return 2
        trained[label.strip()] = Path(path)

    folder = scene_paths.scene_dir(args.name)
    rec = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    src = folder / rec["ply"]
    out = Path(args.out) if args.out else scene_paths.site_data_dir(args.name) / "splat"
    out.mkdir(parents=True, exist_ok=True)

    data, _ = read_splat(src)
    t = to_site(data, np.asarray(site["matrix"], float))
    vis = visibility(t["opacity"], t["scale"])
    order = np.argsort(-vis)
    note = (f"site.json frame: Z up, Y {site['axes']['y']}, units {site['units']}"
            f" -- {args.name}, made by DL-SplatGenerator tools/splat_levels.py")
    print(f"    {len(data):,} splats, SH degree {t['degree']}, {site['units']}")
    wanted = [w.strip() for w in args.only.split(",") if w.strip()]
    sizes = {"high": len(data), "med": args.med, "low": args.low, "verylow": args.verylow}
    for label in wanted:
        if label not in sizes:
            print(f"FAILED: unknown level {label!r}", file=sys.stderr)
            return 2
        if (out / f"{args.name}_3DGS_{label}.ply").exists():
            # found 2026-09-27: without this, a rerun silently rewrote the package's files
            print(f"FAILED: {out / f'{args.name}_3DGS_{label}.ply'} exists -- never overwritten",
                  file=sys.stderr)
            return 2
    for label, n in ((w, sizes[w]) for w in wanted):
        path = out / f"{args.name}_3DGS_{label}.ply"
        if label in trained:
            d2, _ = read_splat(trained[label])
            t2 = to_site(d2, np.asarray(site["matrix"], float))
            write_splat(path, t2["pos"], t2["f_dc"], t2["f_rest"], t2["opacity"], t2["scale"], t2["rot"],
                        comments=["Vertical axis: z", f"SH degree: {t2['degree']}", note,
                                  f"level {label}: {len(d2)} splats, TRAINED at this size "
                                  f"({trained[label].name}), not thinned"])
            print(f"    {path.name}: {len(d2):,} splats (trained), {path.stat().st_size / 1e6:.1f} MB")
            continue
        keep = np.sort(order[:min(n, len(data))])
        write_splat(path, t["pos"][keep], t["f_dc"][keep], t["f_rest"][keep],
                    t["opacity"][keep], t["scale"][keep], t["rot"][keep],
                    comments=["Vertical axis: z", f"SH degree: {t['degree']}", note,
                              f"level {label}: {len(keep)} of {len(data)} splats"])
        print(f"    {path.name}: {len(keep):,} splats, {path.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
