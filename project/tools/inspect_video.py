"""Report what a .mp4/.mov actually contains, without needing ffprobe.

Parses just enough of the ISO base media container (mvhd/tkhd/hdlr/stsd/stts)
to answer the questions that decide how a capture should be processed: how long
it is, what resolution and orientation, how many frames, and at what rate.

    python inspect_video.py <file.mov>
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path


def walk(fh, end, depth=0):
    """Yield (type, payload_start, payload_end) for each atom in a range."""
    while fh.tell() < end - 8:
        start = fh.tell()
        header = fh.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        kind = kind.decode("latin1")
        if size == 1:                       # 64-bit extended size
            size = struct.unpack(">Q", fh.read(8))[0]
            body = start + 16
        elif size == 0:                     # runs to end of file
            size = end - start
            body = start + 8
        else:
            body = start + 8
        if size < 8:
            return
        yield kind, body, start + size
        fh.seek(start + size)


CONTAINERS = {"moov", "trak", "mdia", "minf", "stbl", "edts", "udta"}


def parse(path: Path) -> dict:
    info: dict = {"tracks": []}
    with path.open("rb") as fh:
        size = path.stat().st_size

        def descend(start, end, depth=0, track=None):
            fh.seek(start)
            for kind, body, atom_end in walk(fh, end, depth):
                if kind == "mvhd":
                    fh.seek(body)
                    ver = fh.read(1)[0]
                    fh.read(3)
                    if ver == 1:
                        fh.read(16)
                        timescale = struct.unpack(">I", fh.read(4))[0]
                        duration = struct.unpack(">Q", fh.read(8))[0]
                    else:
                        fh.read(8)
                        timescale = struct.unpack(">I", fh.read(4))[0]
                        duration = struct.unpack(">I", fh.read(4))[0]
                    info["duration_s"] = duration / timescale if timescale else None
                elif kind == "trak":
                    t: dict = {}
                    info["tracks"].append(t)
                    descend(body, atom_end, depth + 1, t)
                elif kind == "tkhd" and track is not None:
                    fh.seek(body)
                    ver = fh.read(1)[0]
                    fh.read(3)
                    fh.read(16 if ver == 1 else 8)
                    fh.read(4)              # track id
                    fh.read(4)              # reserved
                    fh.read(8 if ver == 1 else 4)   # duration
                    fh.read(8)              # reserved
                    fh.read(2 + 2 + 2 + 2)  # layer, altgroup, volume, reserved
                    m = struct.unpack(">9i", fh.read(36))   # 3x3 matrix, 16.16 fixed
                    w = struct.unpack(">I", fh.read(4))[0] / 65536
                    h = struct.unpack(">I", fh.read(4))[0] / 65536
                    if w and h:
                        track["width"] = round(w)
                        track["height"] = round(h)
                        # a/b/c/d of the display matrix tell us the rotation
                        a, b = m[0] / 65536, m[1] / 65536
                        if (a, b) == (0, 1):
                            track["rotation"] = 90
                        elif (a, b) == (0, -1):
                            track["rotation"] = 270
                        elif a == -1:
                            track["rotation"] = 180
                        else:
                            track["rotation"] = 0
                elif kind == "hdlr" and track is not None:
                    fh.seek(body + 8)
                    # Only the first hdlr in a trak is the media handler
                    # ('vide'/'soun'); minf carries a second one for the data
                    # reference ('alis'), which must not overwrite it.
                    track.setdefault("handler", fh.read(4).decode("latin1"))
                elif kind == "stsd" and track is not None:
                    fh.seek(body + 8)
                    fh.read(4)              # entry size
                    track["codec"] = fh.read(4).decode("latin1")
                elif kind == "stts" and track is not None:
                    fh.seek(body + 4)
                    count = struct.unpack(">I", fh.read(4))[0]
                    frames, ticks = 0, 0
                    for _ in range(min(count, 4096)):
                        n, delta = struct.unpack(">II", fh.read(8))
                        frames += n
                        ticks += n * delta
                    track["frames"] = frames
                    track["_ticks"] = ticks
                elif kind == "mdhd" and track is not None:
                    fh.seek(body)
                    ver = fh.read(1)[0]
                    fh.read(3)
                    if ver == 1:
                        fh.read(16)
                        track["timescale"] = struct.unpack(">I", fh.read(4))[0]
                    else:
                        fh.read(8)
                        track["timescale"] = struct.unpack(">I", fh.read(4))[0]
                elif kind in CONTAINERS:
                    descend(body, atom_end, depth + 1, track)

        descend(0, size)
    return info


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python inspect_video.py <file.mov>")
        return 1
    path = Path(sys.argv[1])
    info = parse(path)
    size_mb = path.stat().st_size / 1e6
    dur = info.get("duration_s")

    print(f"file        {path.name}")
    print(f"size        {size_mb:.1f} MB")
    print(f"duration    {dur:.2f} s" if dur else "duration    unknown")

    for t in info["tracks"]:
        if t.get("handler") != "vide":
            continue
        w, h = t.get("width"), t.get("height")
        rot = t.get("rotation", 0)
        frames = t.get("frames")
        ts = t.get("timescale")
        fps = (frames / (t["_ticks"] / ts)) if frames and ts and t.get("_ticks") else None
        disp = f"{h}x{w}" if rot in (90, 270) and w and h else f"{w}x{h}"
        print(f"video       {disp}  rotation {rot} deg  codec {t.get('codec')}")
        if fps:
            print(f"frames      {frames} at {fps:.2f} fps")
        if dur and size_mb:
            print(f"bitrate     ~{size_mb * 8 / dur:.1f} Mbit/s")
        if frames:
            for stride in (2, 3, 5):
                print(f"  every {stride}th frame -> {frames // stride} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
