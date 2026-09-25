"""Build a 3D Gaussian splat scene from a SINGLE image, via metric depth.

    python depth_splat.py <image.jpg> [--name NAME] [--stride 2]

A monocular depth model estimates how far away every pixel is; unprojecting
those pixels through the camera's focal length gives a coloured point cloud,
and each point becomes an oriented splat.

WHAT THIS IS NOT
    A capture. There is no information behind anything, so this is 2.5D: it
    looks right from near the original viewpoint and degrades as you move away,
    with stretching and holes where surfaces were hidden. Use it for site
    impressions, historical photographs and context -- not as a survey.

WHY METRIC, AND WHY ONLY AN ESTIMATE
    Most depth models return *relative* depth: arbitrary scale and offset.
    Depth Anything V2's metric fine-tunes return metres, which keeps the scene
    in plausible proportion -- but the metres are an ESTIMATE. On a capture
    calibrated from a measured dimension, this model put every frame 5-12x too
    deep (output\\research\\DEPTH SMALL VS BASE - RESULTS - 002.md). So the
    viewer does NOT apply the scale for you (it did until 2026-09-24); it offers
    "Use the depth estimate" and marks every figure "est." if you do.
    `"metric": true` in capture.json records where the numbers came from, not
    that they are right. The outdoor fine-tune is the default here.

WHAT MAKES IT LOOK SHARP
    The obvious implementation looks like soup. Three things fix it:
      1. Splats spanning a depth discontinuity (a branch against distant
         ground) get smeared across both surfaces. Those are detected and
         dropped, which is most of the difference between crisp and mushy.
      2. Each splat is sized to cover exactly its pixel footprint at its own
         depth -- too small leaves holes, too large blurs.
      3. Splats are oriented to the local surface normal from the depth
         gradient. Camera-facing splats read as a flat sprite sheet; oriented
         ones read as solid geometry.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent.parent
OUTPUT = ROOT / "output"

# Metric fine-tunes of Depth Anything V2. Outdoor is trained on driving-scale
# scenes, which suits sites and landscapes; indoor covers rooms.
MODELS = {
    "outdoor": "depth-anything/Depth-Anything-V2-Metric-Outdoor-Base-hf",
    "indoor": "depth-anything/Depth-Anything-V2-Metric-Indoor-Base-hf",
}
SH_C0 = 0.28209479177387814

# The horizontal field of view assumed when a photo's file does not record its
# lens as a 35 mm-equivalent focal length. Phone main cameras sit at 65-70°.
ASSUMED_FOV = 65.0


def log(msg: str) -> None:
    print(msg, flush=True)


def estimate_depth(image_path: Path, model_id: str, max_side: int):
    """Return (depth_metres HxW, rgb HxWx3 uint8, the photo's own (w, h))."""
    import numpy as np
    import torch
    from PIL import Image
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation

    # Upright first. A phone stores a portrait photo as landscape pixels with an
    # EXIF "rotate 90°" flag; PIL does not apply it, so the depth was estimated
    # on a sideways picture while its lens was read upright. exif_transpose
    # turns the pixels the way the file says and drops the flag.
    from PIL import ImageOps
    img = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
    original = img.size
    if max(img.size) > max_side:
        scale = max_side / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    log(f"    image {img.width}x{img.height}")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"    loading {model_id} on {device}")
    processor = AutoImageProcessor.from_pretrained(model_id)
    model = AutoModelForDepthEstimation.from_pretrained(model_id).to(device).eval()

    inputs = processor(images=img, return_tensors="pt").to(device)
    with torch.no_grad():
        predicted = model(**inputs).predicted_depth
    depth = torch.nn.functional.interpolate(
        predicted.unsqueeze(1), size=(img.height, img.width),
        mode="bicubic", align_corners=False).squeeze().cpu().numpy()
    return depth.astype("float32"), np.asarray(img, dtype="uint8"), original


def build_splats(depth, rgb, *, fov_deg: float, stride: int, edge_drop: float):
    """Unproject the depth map into oriented splats.

    Returns a list of dicts ready for the PLY writer.
    """
    import numpy as np

    h, w = depth.shape
    # Focal length in pixels from the horizontal field of view -- the photo's
    # own lens when its file says, else an assumption (see main()). The depth
    # carries the distance; the field of view decides how wide everything at
    # that distance is, so a wrong one stretches or squeezes the whole scene.
    fx = fy = (w / 2) / math.tan(math.radians(fov_deg) / 2)
    cx, cy = w / 2, h / 2

    ys, xs = np.mgrid[0:h:stride, 0:w:stride]
    z = depth[::stride, ::stride]
    colour = rgb[::stride, ::stride].astype("float32") / 255.0

    # Depth discontinuities: compare each sample with its neighbours. A splat
    # that straddles an edge would be stretched between two surfaces, so those
    # are dropped rather than smeared.
    dzx = np.abs(np.diff(z, axis=1, prepend=z[:, :1]))
    dzy = np.abs(np.diff(z, axis=0, prepend=z[:1, :]))
    relative_step = np.maximum(dzx, dzy) / np.maximum(z, 1e-6)
    keep = relative_step < edge_drop
    dropped = int((~keep).sum())

    # Camera-space positions in the COLMAP convention every other scene here
    # uses: X right, Y *down*, Z forward into the scene. The viewer turns that
    # Y-up on load, so writing it any other way lands the scene upside down and
    # behind the camera.
    X = (xs - cx) * z / fx
    Y = (ys - cy) * z / fy
    Z = z

    # Surface normal from the depth gradient, so splats lie along surfaces
    # instead of facing the camera like billboards. -Z points back at the
    # camera, so a flat surface gets a normal facing the viewer.
    gy, gx = np.gradient(z)
    safe = np.maximum(z, 1e-6)
    nx, ny = gx * fx / safe, gy * fy / safe
    nz = -np.ones_like(z)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz)
    nx, ny, nz = nx / norm, ny / norm, nz / norm

    # A splat should cover its own pixel footprint at its own depth: any
    # smaller and the surface shows holes, any larger and detail blurs.
    footprint = (z * stride) / fx * 0.75

    splats = []
    for i in range(z.shape[0]):
        for j in range(z.shape[1]):
            if not keep[i, j]:
                continue
            d = float(z[i, j])
            if not math.isfinite(d) or d <= 0:
                continue
            n = (float(nx[i, j]), float(ny[i, j]), float(nz[i, j]))
            splats.append({
                "p": (float(X[i, j]), float(Y[i, j]), float(Z[i, j])),
                "c": tuple(float(v) for v in colour[i, j]),
                "s": float(footprint[i, j]),
                "n": n,
            })
    return splats, dropped


def quat_from_normal(n):
    """Rotation taking +Z onto the normal, as (w, x, y, z)."""
    nx, ny, nz = n
    # axis = z_axis x n, angle = acos(z_axis . n)
    ax, ay, az = -ny, nx, 0.0
    dot = max(-1.0, min(1.0, nz))
    angle = math.acos(dot)
    length = math.sqrt(ax * ax + ay * ay + az * az)
    if length < 1e-9:
        return (1.0, 0.0, 0.0, 0.0) if dot > 0 else (0.0, 1.0, 0.0, 0.0)
    ax, ay, az = ax / length, ay / length, az / length
    s = math.sin(angle / 2)
    return (math.cos(angle / 2), ax * s, ay * s, az * s)


def write_ply(splats, path: Path, *, flatten: float) -> int:
    """Write an INRIA-layout 3DGS .ply (SH degree 0 -- one photo has no
    view-dependent information to fit)."""
    import struct

    props = ["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2", "opacity",
             "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]
    header = ("ply\nformat binary_little_endian 1.0\n"
              f"element vertex {len(splats)}\n"
              + "".join(f"property float {p}\n" for p in props)
              + "end_header\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(header.encode("ascii"))
        buf = bytearray()
        for sp in splats:
            x, y, z = sp["p"]
            r, g, b = sp["c"]
            radius = max(sp["s"], 1e-5)
            qw, qx, qy, qz = quat_from_normal(sp["n"])
            # Flat in the normal direction: a surface element, not a ball.
            values = (
                x, y, z,
                (r - 0.5) / SH_C0, (g - 0.5) / SH_C0, (b - 0.5) / SH_C0,
                6.0,                                   # opacity, pre-sigmoid
                math.log(radius), math.log(radius), math.log(radius * flatten),
                qw, qx, qy, qz,
            )
            buf += struct.pack("<14f", *values)
        fh.write(buf)
    return len(splats)


def main() -> int:
    ap = argparse.ArgumentParser(description="Single image -> 3D Gaussian splats.")
    ap.add_argument("image")
    ap.add_argument("--name", default=None)
    ap.add_argument("--scene", choices=list(MODELS), default="outdoor")
    ap.add_argument("--stride", type=int, default=2,
                    help="sample every Nth pixel (default 2)")
    ap.add_argument("--max-side", type=int, default=1600,
                    help="downscale the image if larger (default 1600)")
    ap.add_argument("--fov", type=float, default=None,
                    help="horizontal field of view in degrees (default: the "
                         "photo's own lens when its file records a 35 mm-"
                         f"equivalent focal length, else {ASSUMED_FOV:g})")
    ap.add_argument("--edge-drop", type=float, default=0.03,
                    help="drop splats where depth changes by more than this "
                         "fraction between neighbours (default 0.03)")
    ap.add_argument("--flatten", type=float, default=0.15,
                    help="thickness of a splat along its normal, relative to "
                         "its width (default 0.15)")
    ap.add_argument("--out-root", default=None)
    args = ap.parse_args()

    image = Path(args.image).expanduser().resolve()
    if not image.is_file():
        print(f"No such image: {image}", file=sys.stderr)
        return 1
    name = args.name or f"{image.stem}_depth"
    out_root = Path(args.out_root).resolve() if args.out_root else OUTPUT
    out_dir = out_root / name

    log(f"Single-image depth -> splats")
    log(f"  image  {image}")
    log(f"  output {out_dir}")

    # What the photo says about itself -- and above all its LENS. The scene is
    # unprojected with a field of view; until 2026-09-25 every photo got 65°,
    # and the uploaded Everest photo (85 mm on an APS-C Canon, about 15°
    # across) came out several times too wide. Order: a --fov you give, else
    # the photo's own 35 mm-equivalent focal length, else the assumption.
    try:
        import source_meta
        meta = source_meta.read(image)
    except Exception as exc:                     # noqa: BLE001
        meta = {"error": str(exc)}
    lens_fov = (meta.get("camera") or {}).get("hfov_deg")
    if args.fov:
        fov, fov_source = args.fov, "given on the command line"
    elif lens_fov:
        fov = float(lens_fov)
        fov_source = (f"the photo's lens: {meta['camera']['focal_35mm']:g} mm "
                      f"35 mm-equivalent")
    else:
        fov, fov_source = ASSUMED_FOV, "assumed: the file records no 35 mm-equivalent focal length"
    log(f"  field of view {fov:g}° across -- {fov_source}\n")

    began = time.time()
    log("[1/3] depth")
    try:
        depth, rgb, original = estimate_depth(image, MODELS[args.scene], args.max_side)
    except Exception as exc:                                  # noqa: BLE001
        print(f"FAILED: depth estimation: {exc}", file=sys.stderr)
        return 2
    finite = depth[depth > 0]
    log(f"    metric depth {finite.min():.2f} m to {finite.max():.2f} m "
        f"(median {float(__import__('numpy').median(finite)):.2f} m)")

    log("[2/3] splats")
    splats, dropped = build_splats(depth, rgb, fov_deg=fov,
                                   stride=args.stride, edge_drop=args.edge_drop)
    log(f"    {len(splats):,} splats, {dropped:,} dropped at depth edges")

    log("[3/3] write")
    ply = out_dir / f"{name}.ply"
    write_ply(splats, ply, flatten=args.flatten)
    size_mb = ply.stat().st_size / 1e6
    log(f"    {ply}  ({size_mb:.1f} MB)")

    # Compress at source, the same as the video pipeline does.
    served = ply
    node = __import__("shutil").which("node")
    if node:
        import subprocess
        spz = ply.with_suffix(".spz")
        proc = subprocess.run([node, str(TOOLS / "to_spz.mjs"), str(ply), str(spz)],
                              capture_output=True, text=True)
        if proc.returncode == 0 and spz.is_file():
            served = spz
            log(f"    {spz.name}  {spz.stat().st_size / 1e6:.1f} MB "
                f"({100 - 100 * spz.stat().st_size / ply.stat().st_size:.0f}% smaller)")

    created = time.strftime("%Y-%m-%dT%H:%M:%S")
    import scene_index
    scene_index.register(out_root, name, served=served,
                         original=ply if served is not ply else None,
                         created=created, method="single-image depth")

    (out_dir / "capture.json").write_text(json.dumps({
        "name": name,
        "source": str(image),
        "source_meta": meta,
        "method": "single-image metric depth",
        "model": MODELS[args.scene],
        "created": created,
        "ply": ply.name,
        "metric": True,
        # The photo's own size, and the size it was worked at: the viewer's
        # photo view frames the scene at exactly this rectangle.
        "image": {"width": original[0], "height": original[1],
                  "worked": [int(depth.shape[1]), int(depth.shape[0])]},
        "settings": {"stride": args.stride, "fov": fov, "fov_source": fov_source,
                     "edge_drop": args.edge_drop, "flatten": args.flatten,
                     "max_side": args.max_side},
        "caveat": "2.5D: no data behind surfaces; accurate only near the "
                  "original viewpoint.",
    }, indent=2))

    log(f"\nDone in {time.time() - began:.0f}s. "
        f"The depth is in estimated metres - calibrate from a known distance "
        f"to measure, or use the estimate, labelled as one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
