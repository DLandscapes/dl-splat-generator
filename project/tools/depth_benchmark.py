"""Depth Anything V2 Metric Outdoor: Small against Base, scored on COLMAP's points.

The plan -- frames, metrics and the pass rule -- was written BEFORE this ran:
output\\research\\DEPTH SMALL VS BASE - PLAN - 001.md. This script implements
that plan and nothing else; change the plan first, in a new numbered file, if
anything here has to change.

Ground truth is the solve's own triangulated points, so it is independent of
both models: every point a frame observes with reprojection error < 2 px and a
track of >= 3 views, at its depth in that camera, at the pixel COLMAP saw it.

    python -X utf8 tools/depth_benchmark.py
"""
from __future__ import annotations

import json
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent.parent
WORK = ROOT / "work"
RESEARCH = ROOT / "output" / "research"

MODELS = {
    "Small": "depth-anything/Depth-Anything-V2-Metric-Outdoor-Small-hf",
    "Base": "depth-anything/Depth-Anything-V2-Metric-Outdoor-Base-hf",
}
CAPTURES = {                       # class -> undistorted PINHOLE model
    "IMG_1779": WORK / "IMG_1779" / "dense" / "sparse",
    "IMG_1988": WORK / "IMG_1988" / "undistorted" / "sparse" / "0",
    "IMG_8950_clean": WORK / "IMG_8950_clean" / "undistorted" / "sparse" / "0",
    "sample-netherlands-IMG_2522":
        WORK / "sample-netherlands-IMG_2522" / "undistorted" / "sparse" / "0",
}
METRIC_M_PER_UNIT = {"IMG_1779": 1.0}   # Marc's 1.2 m path width; +/-10 % at best
MAX_ERROR_PX, MIN_TRACK, MIN_POINTS, PER_CAPTURE = 2.0, 3, 100, 12
MAX_SIDE = 1600                        # as depth_splat.py


# ------------------------------------------------------------- COLMAP reading

def read_cameras(path: Path) -> dict:
    cams = {}
    with path.open("rb") as fh:
        for _ in range(struct.unpack("<Q", fh.read(8))[0]):
            cid, model, w, h = struct.unpack("<iiQQ", fh.read(24))
            if model != 1:
                raise ValueError(f"{path}: camera {cid} is not PINHOLE (model {model})")
            fx, fy, cx, cy = struct.unpack("<4d", fh.read(32))
            cams[cid] = (w, h, fx, fy, cx, cy)
    return cams


def read_images(path: Path) -> list:
    images = []
    with path.open("rb") as fh:
        for _ in range(struct.unpack("<Q", fh.read(8))[0]):
            iid, qw, qx, qy, qz, tx, ty, tz, cid = struct.unpack("<idddddddi", fh.read(64))
            name = bytearray()
            while (ch := fh.read(1)) != b"\x00":
                name += ch
            n = struct.unpack("<Q", fh.read(8))[0]
            obs = np.frombuffer(fh.read(24 * n), dtype=[("x", "<f8"), ("y", "<f8"),
                                                        ("pid", "<i8")])
            images.append({"id": iid, "name": name.decode(), "cam": cid,
                           "q": (qw, qx, qy, qz), "t": np.array((tx, ty, tz)),
                           "obs": obs})
    return images


def read_points(path: Path) -> dict:
    pts = {}
    with path.open("rb") as fh:
        for _ in range(struct.unpack("<Q", fh.read(8))[0]):
            pid, x, y, z = struct.unpack("<Qddd", fh.read(32))
            fh.read(3)                                   # rgb
            err, = struct.unpack("<d", fh.read(8))
            track, = struct.unpack("<Q", fh.read(8))
            fh.read(8 * track)
            pts[pid] = (np.array((x, y, z)), err, track)
    return pts


def rotation(qw, qx, qy, qz):
    return np.array([
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)],
    ])


def ground_truth(model: Path) -> list:
    """The plan's test frames for one capture, each with its GT observations."""
    points = read_points(model / "points3D.bin")
    frames = []
    for im in sorted(read_images(model / "images.bin"), key=lambda i: i["name"]):
        R = rotation(*im["q"])
        us, vs, ds = [], [], []
        for o in im["obs"]:
            p = points.get(int(o["pid"])) if o["pid"] >= 0 else None
            if not p or p[1] >= MAX_ERROR_PX or p[2] < MIN_TRACK:
                continue
            z = (R @ p[0] + im["t"])[2]
            if z > 0:
                us.append(o["x"]); vs.append(o["y"]); ds.append(z)
        if len(ds) >= MIN_POINTS:
            frames.append({"name": im["name"], "u": np.array(us), "v": np.array(vs),
                           "d": np.array(ds)})
    if len(frames) > PER_CAPTURE:
        pick = np.linspace(0, len(frames) - 1, PER_CAPTURE).round().astype(int)
        frames = [frames[i] for i in pick]
    return frames


# ------------------------------------------------------------------ inference

class Depth:
    def __init__(self, model_id: str, device: str):
        import torch
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation
        self.torch, self.device = torch, device
        self.processor = AutoImageProcessor.from_pretrained(model_id)
        self.model = AutoModelForDepthEstimation.from_pretrained(model_id).to(device).eval()
        self.params = sum(p.numel() for p in self.model.parameters())

    def __call__(self, image_path: Path) -> tuple[np.ndarray, float, tuple]:
        """Metric depth at the image's own size, the way depth_splat.py makes it."""
        from PIL import Image
        torch = self.torch
        img = Image.open(image_path).convert("RGB")
        full = img.size
        if max(img.size) > MAX_SIDE:
            k = MAX_SIDE / max(img.size)
            img = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
        if self.device == "cuda":
            torch.cuda.synchronize()
        t0 = time.perf_counter()
        inputs = self.processor(images=img, return_tensors="pt").to(self.device)
        with torch.no_grad():
            pred = self.model(**inputs).predicted_depth
        depth = torch.nn.functional.interpolate(
            pred.unsqueeze(1), size=(img.height, img.width),
            mode="bicubic", align_corners=False).squeeze().cpu().numpy()
        if self.device == "cuda":
            torch.cuda.synchronize()
        seconds = time.perf_counter() - t0
        # the plan reads depth at COLMAP's pixel, in the full-size frame
        return depth, seconds, full


def sample(depth: np.ndarray, full: tuple, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    h, w = depth.shape
    x = np.clip(np.round(u * w / full[0] - 0.5).astype(int), 0, w - 1)
    y = np.clip(np.round(v * h / full[1] - 0.5).astype(int), 0, h - 1)
    return depth[y, x]


def score(p: np.ndarray, d: np.ndarray) -> dict:
    ok = p > 1e-6
    p, d = p[ok], d[ok]
    s = float(np.median(d / p))
    ratio = s * p / d
    return {"s": s, "absrel": float(np.mean(np.abs(s * p - d) / d)),
            "delta1": float(np.mean(np.maximum(ratio, 1 / ratio) < 1.25)),
            "metric": float(np.median(p / d)) - 1.0, "n": int(ok.sum())}


# ----------------------------------------------------------------------- run

def main() -> int:
    import torch
    t_all = time.time()
    data = {}
    for cls, model in CAPTURES.items():
        frames = ground_truth(model)
        folder = model.parent / "images" if (model.parent / "images").is_dir() \
            else model.parent.parent / "images"
        for f in frames:
            f["path"] = folder / f["name"]
            if not f["path"].is_file():
                raise FileNotFoundError(f["path"])
        data[cls] = frames
        print(f"{cls}: {len(frames)} frames, "
              f"{int(np.median([len(f['d']) for f in frames]))} GT points median")

    results = {"plan": "DEPTH SMALL VS BASE - PLAN - 001.md", "frames": {}, "cost": {}}
    # DEVIATION from the plan, recorded before scoring: the venv's PyTorch is a
    # CPU-only build (2.13.0+cpu), so the photo route has always run on the CPU
    # here and there is no GPU timing to take. Every frame runs on the CPU and
    # that is the timing reported. Cost was never scored, so the pass rule is
    # untouched.
    for label, model_id in MODELS.items():
        print(f"\n{label}: {model_id}")
        t0 = time.time()
        net = Depth(model_id, "cpu")
        load = time.time() - t0
        net(next(iter(data.values()))[0]["path"])            # warm-up, not timed
        times = []
        for cls, frames in data.items():
            for f in frames:
                depth, sec, full = net(f["path"])
                times.append(sec)
                r = score(sample(depth, full, f["u"], f["v"]), f["d"])
                results["frames"].setdefault(cls, {}).setdefault(f["name"], {})[label] = r
        results["cost"][label] = {
            "model": model_id, "params_M": round(net.params / 1e6, 1),
            "load_s": round(load, 1), "device": "cpu",
            "cpu_s_per_frame": round(float(np.mean(times)), 2),
            "cpu_threads": torch.get_num_threads(), "torch": torch.__version__}
        del net
        print("  ", results["cost"][label])

    # the trivial baseline: a constant depth, through the same median scaling
    for cls, frames in data.items():
        for f in frames:
            results["frames"][cls][f["name"]]["Constant"] = score(np.ones_like(f["d"]), f["d"])

    RESEARCH.mkdir(parents=True, exist_ok=True)
    out = RESEARCH / "DEPTH SMALL VS BASE - RESULTS - 001.json"
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\nwrote {out}  ({time.time() - t_all:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
