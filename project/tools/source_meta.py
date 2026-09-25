"""What a source photo or video says about itself: when, where, with what.

    python -X utf8 tools/source_meta.py <photo or video>

Only what matters for a site capture is kept -- decided 2026-09-25:

  WHEN     capture time, with its own time zone (season, time of day, the light)
  WHERE    GPS latitude / longitude / altitude, the fix's stated accuracy, and a
           photo's compass heading when the phone recorded one
  CAMERA   make, model, software; lens, focal length, and the 35 mm-equivalent
           focal length -> the horizontal FIELD OF VIEW. That last one matters
           most: a photo scene is unprojected with a field of view, and the photo
           route assumed 65 deg for every picture. The Everest photo in the
           uploads says 85 mm on a Canon EOS 350D (136 mm equivalent) -- about
           15 deg across, not 65.
  IMAGE    pixel size and orientation (a portrait phone video is stored
           landscape with a rotation flag); for video, frame rate and duration
  CREDIT   artist and copyright, when the file names them

Left out on purpose: exposure (shutter, aperture, ISO), which says nothing about
the site; maker notes; thumbnails.

⚠ GPS, time and device together are personal data (whoever held the camera was
there, then). They are shown to the person who made the scene; nothing here
sends them anywhere, and make_sample.py leaves the location out of a sample
unless asked.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".avi", ".mkv"}


def _ffprobe() -> Path | None:
    from capture import FFPROBE        # the same bundled binary the pipeline uses
    return FFPROBE


def _ratio(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _hfov(focal35: float | None, width: int, height: int) -> float | None:
    """Horizontal field of view from a 35 mm-equivalent focal length. The
    equivalence is defined on the 36 x 24 mm frame's DIAGONAL (43.3 mm), so the
    image's own aspect decides how much of it is horizontal."""
    if not focal35 or not width or not height:
        return None
    diag = math.hypot(36.0, 24.0)
    w_mm = diag * width / math.hypot(width, height)
    return round(math.degrees(2 * math.atan(w_mm / (2 * focal35))), 1)


def _dms(values, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in values)
    except (TypeError, ValueError):
        return None
    v = d + m / 60 + s / 3600
    return round(-v if ref in ("S", "W") else v, 6)


# ------------------------------------------------------------------- photos

def photo_meta(path: Path) -> dict:
    from PIL import ExifTags, Image
    img = Image.open(path)
    width, height = img.size
    exif = img.getexif()
    base = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
    sub = {ExifTags.TAGS.get(k, k): v for k, v in exif.get_ifd(0x8769).items()} if exif else {}
    gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in exif.get_ifd(0x8825).items()} if exif else {}

    orientation = base.get("Orientation")
    if orientation in (5, 6, 7, 8):        # stored rotated a quarter turn
        width, height = height, width

    focal = _ratio(sub.get("FocalLength"))
    focal35 = _ratio(sub.get("FocalLengthIn35mmFilm")) or None
    when = sub.get("DateTimeOriginal") or base.get("DateTime")
    if when:
        when = re.sub(r"^(\d{4}):(\d\d):(\d\d)", r"\1-\2-\3", str(when)).replace(" ", "T")
        if sub.get("OffsetTimeOriginal"):
            when += str(sub["OffsetTimeOriginal"])

    where = None
    if gps.get("GPSLatitude") and gps.get("GPSLongitude"):
        where = {
            "lat": _dms(gps["GPSLatitude"], gps.get("GPSLatitudeRef")),
            "lon": _dms(gps["GPSLongitude"], gps.get("GPSLongitudeRef")),
            "alt": round(_ratio(gps.get("GPSAltitude")) or 0, 1) if gps.get("GPSAltitude") else None,
            "accuracy_m": _ratio(gps.get("GPSHPositioningError")),
            "heading_deg": _ratio(gps.get("GPSImgDirection")),
            "heading_ref": {"T": "true north", "M": "magnetic north"}.get(
                gps.get("GPSImgDirectionRef")),
        }

    return _clean({
        "kind": "photo",
        "file": path.name,
        "when": when,
        "where": where,
        "camera": {
            "make": base.get("Make"), "model": base.get("Model"),
            "software": base.get("Software"), "lens": sub.get("LensModel"),
            "focal_mm": focal, "focal_35mm": focal35,
            "hfov_deg": _hfov(focal35, width, height),
        },
        "image": {"width": width, "height": height, "format": img.format},
        "credit": {"artist": base.get("Artist"), "copyright": base.get("Copyright")},
    })


# ------------------------------------------------------------------- videos

def _iso6709(s: str | None) -> dict | None:
    """'+69.7051+019.0098+045.661/' -> lat, lon, alt (Apple's location tag)."""
    m = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?", s or "")
    if not m:
        return None
    return {"lat": float(m[1]), "lon": float(m[2]),
            "alt": round(float(m[3]), 1) if m[3] else None}


def video_meta(path: Path) -> dict:
    probe = _ffprobe()
    if not probe:
        return {"kind": "video", "file": path.name, "error": "ffprobe not found"}
    out = subprocess.run([str(probe), "-v", "quiet", "-print_format", "json",
                          "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True, timeout=60)
    data = json.loads(out.stdout or "{}")
    fmt = data.get("format", {})
    tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})

    width, height = video.get("width"), video.get("height")
    rotation = next((sd.get("rotation") for sd in video.get("side_data_list", [])
                     if "rotation" in sd), None) or _ratio(video.get("tags", {}).get("rotate"))
    if rotation and abs(int(rotation)) % 180 == 90:
        width, height = height, width        # shown upright, as it was filmed
    num, _, den = str(video.get("avg_frame_rate", "0/1")).partition("/")
    fps = round(float(num) / float(den), 3) if den and float(den) else None

    where = _iso6709(tags.get("com.apple.quicktime.location.iso6709")
                     or tags.get("location"))
    if where:
        where["accuracy_m"] = _ratio(tags.get("com.apple.quicktime.location.accuracy.horizontal"))
        if where["accuracy_m"]:
            where["accuracy_m"] = round(where["accuracy_m"], 1)

    return _clean({
        "kind": "video",
        "file": path.name,
        "when": tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time"),
        "where": where,
        "camera": {
            "make": tags.get("com.apple.quicktime.make"),
            "model": tags.get("com.apple.quicktime.model"),
            "software": tags.get("com.apple.quicktime.software"),
        },
        "image": {"width": width, "height": height,
                  "orientation": ("portrait" if width and height and height > width
                                  else "landscape" if width else None),
                  "fps": fps, "codec": video.get("codec_name")},
        "duration_s": round(_ratio(fmt.get("duration")) or 0, 2) or None,
        "size_mb": round((_ratio(fmt.get("size")) or 0) / 1e6, 1) or None,
    })


# --------------------------------------------------------------------------

def _clean(d):
    """Drop empty values, so the viewer lists only what the file really says."""
    if d is None or isinstance(d, (bytes, bytearray)):
        return None
    if isinstance(d, dict):
        out = {k: _clean(v) for k, v in d.items()}
        return {k: v for k, v in out.items() if v not in (None, "", {}, [])}
    if not isinstance(d, (str, int, float, bool, list)):
        return str(d)
    return d


def read(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return {"file": path.name, "error": "the source file is no longer there"}
    if path.suffix.lower() in VIDEO_SUFFIXES:
        return video_meta(path)
    return photo_meta(path)


def without_location(meta: dict) -> dict:
    """The same record with the GPS position removed -- for anything shared."""
    return {k: v for k, v in meta.items() if k != "where"}


if __name__ == "__main__":
    print(json.dumps(read(Path(sys.argv[1])), indent=2, ensure_ascii=False))
