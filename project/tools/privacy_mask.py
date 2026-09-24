"""Mask faces in capture frames before anything else sees them (GDPR).

    python privacy_mask.py <frames-dir> [--report report.json] [--dry-run]

Run this between frame extraction and pose solving. Masking here rather than
afterwards matters: once COLMAP and the trainer have consumed the frames, faces
are baked into the splats and cannot be removed without redoing everything.

WHAT THIS DOES AND DOES NOT DO
    It detects faces (YuNet) and blurs them beyond recovery, then reports what
    it found. That removes the most direct identifier and is a large reduction
    in risk.

    It is NOT complete anonymisation. A person can remain identifiable from
    build, gait, clothing, a pushchair, context, or simply being the only
    person at a known location. Vehicle registration plates, house numbers,
    signage and windows are not detected at all. Under GDPR the operator
    remains the controller: review the frames, and use --mask-region for
    anything the detector cannot see.

    Norwegian rules add to this rather than replace it -- personopplysningsloven
    applies the GDPR, and photographing identifiable people in public for
    publication has its own constraints.

The report is written so a later publish step can refuse to proceed on a
capture that was never reviewed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent.parent
MODEL = ROOT / "bin" / "models" / "face_detection_yunet_2023mar.onnx"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def parse_region(text: str) -> tuple:
    """'x,y,w,h' in fractions of the image, so it survives any resolution."""
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 4:
        raise ValueError(f"region must be x,y,w,h -- got {text!r}")
    return tuple(parts)


def mask_frames(frames_dir: Path, *, confidence: float, grow: float,
                regions: list, dry: bool) -> dict:
    import cv2
    import numpy as np

    if not MODEL.is_file():
        raise FileNotFoundError(
            f"face model missing: {MODEL}\n"
            f"Download face_detection_yunet_2023mar.onnx from the OpenCV model "
            f"zoo into bin/models/.")

    frames = sorted(p for p in frames_dir.iterdir()
                    if p.suffix.lower() in IMAGE_SUFFIXES)
    if not frames:
        raise FileNotFoundError(f"no images in {frames_dir}")

    first = cv2.imread(str(frames[0]))
    if first is None:
        raise ValueError(f"could not read {frames[0]}")
    h, w = first.shape[:2]
    detector = cv2.FaceDetectorYN.create(
        str(MODEL), "", (w, h), score_threshold=confidence)

    faces_total, frames_with_faces, masked_files = 0, 0, 0
    detections = []          # which frames, so the operator can review them
    for path in frames:
        img = cv2.imread(str(path))
        if img is None:
            continue
        if img.shape[:2] != (h, w):
            h, w = img.shape[:2]
            detector.setInputSize((w, h))

        _, dets = detector.detect(img)
        boxes = []
        if dets is not None:
            for d in dets:
                x, y, bw, bh = d[:4]
                # Grow the box: a tight crop leaves hairline, jaw and ears,
                # which are still identifying.
                cx, cy = x + bw / 2, y + bh / 2
                bw, bh = bw * (1 + grow), bh * (1 + grow)
                boxes.append((cx - bw / 2, cy - bh / 2, bw, bh))
            faces_total += len(dets)
            if len(dets):
                frames_with_faces += 1
                detections.append({
                    "file": path.name,
                    "faces": int(len(dets)),
                    "scores": [round(float(d[-1]), 3) for d in dets],
                })

        for fx, fy, fw, fh in regions:      # manual regions, as fractions
            boxes.append((fx * w, fy * h, fw * w, fh * h))

        if not boxes or dry:
            continue

        for (bx, by, bw, bh) in boxes:
            x0, y0 = max(0, int(bx)), max(0, int(by))
            x1, y1 = min(w, int(bx + bw)), min(h, int(by + bh))
            if x1 <= x0 or y1 <= y0:
                continue
            roi = img[y0:y1, x0:x1]
            # Heavy pixelation then blur: irreversible, unlike a light blur
            # which can sometimes be inverted.
            small = cv2.resize(roi, (max(1, (x1 - x0) // 16),
                                     max(1, (y1 - y0) // 16)),
                               interpolation=cv2.INTER_LINEAR)
            roi[:] = cv2.GaussianBlur(
                cv2.resize(small, (x1 - x0, y1 - y0),
                           interpolation=cv2.INTER_NEAREST), (0, 0), 8)
        cv2.imwrite(str(path), img)
        masked_files += 1

    return {
        "frames": len(frames),
        "faces_detected": faces_total,
        "frames_with_faces": frames_with_faces,
        "frames_masked": masked_files,
        "manual_regions": len(regions),
        "confidence": confidence,
        "grow": grow,
        "dry_run": dry,
        # named so the operator can open exactly the frames that matter
        "detections": detections,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Blur faces in capture frames.")
    ap.add_argument("frames", help="folder of extracted frames")
    ap.add_argument("--confidence", type=float, default=0.6,
                    help="detection threshold, lower catches more (default 0.6)")
    ap.add_argument("--grow", type=float, default=0.6,
                    help="enlarge each face box by this fraction (default 0.6)")
    ap.add_argument("--mask-region", action="append", default=[],
                    metavar="X,Y,W,H",
                    help="always blank this region, as fractions of the image; "
                         "repeatable. For plates, windows and anything the "
                         "detector cannot see.")
    ap.add_argument("--report", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="count faces without altering any frame")
    args = ap.parse_args()

    frames_dir = Path(args.frames).expanduser().resolve()
    try:
        regions = [parse_region(r) for r in args.mask_region]
        began = time.time()
        stats = mask_frames(frames_dir, confidence=args.confidence,
                            grow=args.grow, regions=regions, dry=args.dry_run)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    stats["seconds"] = round(time.time() - began, 1)
    stats["reviewed"] = False          # only a human can set this
    stats["note"] = ("Automatic face masking only. Plates, signage, windows and "
                     "otherwise identifiable people are NOT detected. The "
                     "operator remains the controller and must review.")
    print(json.dumps(stats, indent=1))
    if args.report:
        Path(args.report).write_text(json.dumps(stats, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
