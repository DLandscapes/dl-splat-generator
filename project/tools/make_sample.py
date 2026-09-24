"""Turn a scene made here into a SAMPLE the viewer offers on its Import panel.

    python -X utf8 tools/make_sample.py <scene> --title "..." --place "..." --date "2026-09"
           [--note "..."] [--id NAME] [--max-splats N] [--root FOLDER]

<scene> is a folder name under output\\. The sample lands in
project\\data\\samples\\<id>\\ (or --root) and is listed in samples.json there:

    scene.spz       the splat, compressed (every splat, unless --max-splats)
    cameras.json    video scenes: the camera path, so the sample opens at camera 1
    thumb.jpg       480 x 360: the photograph itself, or the video's first solved
                    frame -- privacy-masked like every frame the solve saw
    samples.json    one entry per sample: what it is, where and when, and the part
                    of capture.json the viewer needs (field of view, image size,
                    scale), WITHOUT the absolute paths capture.json carries

⚠ THIS TOOL DOES NOT CHECK FOR PEOPLE. A sample is published with the viewer.
Look at thumb.jpg and walk the scene before committing it: faces are masked
before solving, but bodies, number plates, house numbers and windows are not.
Only Digital Landscapes' own material belongs here.

Keep a sample light by filming a SHORT walk, not by thinning: a 31-frame clip
trains to about 5 MB with nothing removed, while thinning a dense 2 M-splat
scene to 400 k visibly muddies it (measured; see thin()). Above 12 MB the tool
says so.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
PROJECT = TOOLS.parent
OUTPUT = PROJECT.parent / "output"
WORK = PROJECT.parent / "work"
DEFAULT_ROOT = PROJECT / "data" / "samples"
THUMB = (480, 360)
HEAVY_MB = 12                  # above this a sample weighs on every download
PHOTO_METHOD = "single-image metric depth"

# the parts of capture.json the viewer reads, and nothing that names a disk
RECORD_KEYS = ("method", "metric", "scale_m_per_unit", "scale_note", "fps", "image")
SETTING_KEYS = ("fov", "stride")


# ------------------------------------------------------------------ PLY in/out

def read_ply(path: Path) -> tuple[list[str], np.ndarray]:
    """A binary little-endian PLY of float vertex properties (INRIA layout)."""
    with path.open("rb") as fh:
        header = []
        while (line := fh.readline().decode("ascii").strip()) != "end_header":
            header.append(line)
        if "format binary_little_endian 1.0" not in header:
            raise ValueError(f"{path.name}: only binary little-endian PLY is supported")
        count = next(int(h.split()[2]) for h in header if h.startswith("element vertex"))
        props = [h.split()[2] for h in header if h.startswith("property float")]
        if len(props) != sum(h.startswith("property") for h in header):
            raise ValueError(f"{path.name}: every property must be a float")
        data = np.fromfile(fh, dtype=np.dtype([(p, "<f4") for p in props]), count=count)
    return props, data


def write_ply(path: Path, props: list[str], data: np.ndarray) -> None:
    header = ("ply\nformat binary_little_endian 1.0\n"
              f"element vertex {len(data)}\n"
              + "".join(f"property float {p}\n" for p in props) + "end_header\n")
    with path.open("wb") as fh:
        fh.write(header.encode("ascii"))
        data.tofile(fh)


def thin(data: np.ndarray, keep: int) -> np.ndarray:
    """The `keep` splats that carry the most of the picture: opacity x size.

    ⚠ A LAST RESORT, off by default. Measured 2026-09-24 on IMG_8950_clean
    (dense woodland), 400 k of 2 M splats against the full scene: 37-40 % of
    the pixels changed at cameras 41 and 201, the foreground muddy and holed.
    Ranking by projected size instead (÷ distance to the nearest camera) was no
    better overall: 28 % and 60 %. The honest way to a light sample is a SHORT
    walk -- a 31-frame clip trained to 218 k splats, 5.2 MB, nothing removed.
    """
    if len(data) <= keep:
        return data
    opacity = 1 / (1 + np.exp(-data["opacity"].astype(np.float64)))
    size = np.exp((data["scale_0"] + data["scale_1"] + data["scale_2"]).astype(np.float64) / 3)
    order = np.argsort(-(opacity * size), kind="stable")[:keep]
    return data[np.sort(order)]            # keep the file's own order


# ------------------------------------------------------------------ the sample

def thumbnail(src: Path, dst: Path) -> None:
    from PIL import Image
    img = Image.open(src).convert("RGB")
    k = max(THUMB[0] / img.width, THUMB[1] / img.height)
    img = img.resize((max(THUMB[0], round(img.width * k)),
                      max(THUMB[1], round(img.height * k))), Image.LANCZOS)
    left, top = (img.width - THUMB[0]) // 2, (img.height - THUMB[1]) // 2
    img.crop((left, top, left + THUMB[0], top + THUMB[1])).save(dst, quality=85)


def first_frame(name: str, cameras: dict) -> Path:
    """The video's first solved frame, as the solve saw it (masked, undistorted)."""
    first = cameras["cameras"][0]["name"]
    for folder in (WORK / name / "undistorted" / "images", WORK / name / "dense" / "images",
                   WORK / name / "images"):
        if (folder / first).is_file():
            return folder / first
    raise FileNotFoundError(f"no frame {first} under work\\{name} -- the intermediates "
                            "may have been cleared")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scene", help="folder name under output\\")
    ap.add_argument("--title", required=True, help="what it shows, e.g. 'Birch mire'")
    ap.add_argument("--place", required=True, help="where it was captured")
    ap.add_argument("--date", required=True, help="when, e.g. 2026-09")
    ap.add_argument("--note", default="", help="one line shown under the title")
    ap.add_argument("--id", help="sample folder name (default: the scene name)")
    ap.add_argument("--max-splats", type=int, default=0,
                    help="thin to this many splats first -- a last resort that "
                         "costs visible quality (default: keep every splat)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT),
                    help="where samples live (default project\\data\\samples)")
    args = ap.parse_args()

    scene = OUTPUT / args.scene
    rec = json.loads((scene / "capture.json").read_text(encoding="utf-8"))
    sid = args.id or args.scene
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        print(f"sample id {sid!r}: letters, digits, - and _ only", file=sys.stderr)
        return 2
    kind = "photo" if rec.get("method") == PHOTO_METHOD else "video"
    root = Path(args.root).resolve()
    out = root / sid
    out.mkdir(parents=True, exist_ok=True)

    # 1. the splat -- thinned only if asked -- then SPZ through the same encoder
    # the pipeline uses
    ply = scene / rec["ply"]
    props, data = read_ply(ply)
    kept = thin(data, args.max_splats) if args.max_splats else data
    with tempfile.TemporaryDirectory() as tmp:
        small = Path(tmp) / "scene.ply"
        write_ply(small, props, kept)
        proc = subprocess.run(["node", str(TOOLS / "to_spz.mjs"), str(small),
                               str(out / "scene.spz")], capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"SPZ encoding failed:\n{proc.stderr}", file=sys.stderr)
            return 2

    # 2. cameras (video) and the thumbnail
    cameras = None
    if kind == "video":
        cameras = json.loads((scene / "cameras.json").read_text(encoding="utf-8"))
        shutil.copyfile(scene / "cameras.json", out / "cameras.json")
        thumbnail(first_frame(args.scene, cameras), out / "thumb.jpg")
    else:
        thumbnail(Path(rec["source"]), out / "thumb.jpg")

    # 3. the record the viewer needs -- no paths
    record = {k: rec[k] for k in RECORD_KEYS if k in rec}
    settings = {k: v for k, v in (rec.get("settings") or {}).items() if k in SETTING_KEYS}
    if settings:
        record["settings"] = settings
    if kind == "photo" and "image" not in record:
        # records before 2026-09-24: the aspect, read back as the viewer would
        pos = np.stack([data["x"], data["y"], data["z"]], axis=1)
        pos = pos[pos[:, 2] > 1e-6]
        aspect = float(np.abs(pos[:, 0] / pos[:, 2]).max() / np.abs(pos[:, 1] / pos[:, 2]).max())
        record["image"] = {"aspect": round(aspect, 5)}

    entry = {
        "id": sid, "kind": kind, "title": args.title, "place": args.place,
        "date": args.date, "note": args.note,
        "scene": f"{sid}/scene.spz", "thumb": f"{sid}/thumb.jpg",
        "splats": int(len(kept)), "splatsOriginal": int(len(data)),
        "frames": len(cameras["cameras"]) if cameras else None,
        "record": record,
    }

    manifest_path = root / "samples.json"
    manifest = {"format": "dlsamples", "version": 1, "samples": []}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["samples"] = [s for s in manifest["samples"] if s["id"] != sid] + [entry]
    manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False),
                             encoding="utf-8")

    mb = (out / "scene.spz").stat().st_size / 1e6
    print(f"{kind} sample '{sid}': {len(kept):,} of {len(data):,} splats, "
          f"{mb:.1f} MB SPZ -> {out}")
    if mb > HEAVY_MB:
        print(f"⚠ {mb:.0f} MB is heavy for a sample that ships with the viewer. A "
              f"shorter walk is better than --max-splats, which costs visible quality.")
    print("⚠ Look at thumb.jpg and walk the scene for people before committing it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
