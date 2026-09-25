"""Read COLMAP camera poses and write cameras.json for the viewer.

This is what makes "open the scene where it was actually filmed" possible: a
.ply carries no camera poses, but the COLMAP model our own pipeline produces
carries one per registered frame.

    python colmap_cameras.py <undistorted-dir> [-o cameras.json]

<undistorted-dir> is the folder capture.py produces, containing sparse/0/ with
cameras.bin and images.bin.

Coordinate convention
  COLMAP stores world-to-camera rotation R and translation t, with the camera
  looking down +Z and Y pointing down. The camera centre in world space is
  C = -R^T t and the viewing direction is R^T (0,0,1).

  The viewer renders COLMAP scenes with a 180 degree turn about X to bring them
  Y-up, so the same flip (x, -y, -z) is applied here. Positions and directions
  written out are therefore already in viewer world space.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path

# COLMAP camera model id -> (name, number of params)
CAMERA_MODELS = {
    0: ("SIMPLE_PINHOLE", 3), 1: ("PINHOLE", 4), 2: ("SIMPLE_RADIAL", 4),
    3: ("RADIAL", 5), 4: ("OPENCV", 8), 5: ("OPENCV_FISHEYE", 8),
    6: ("FULL_OPENCV", 12), 7: ("FOV", 5), 8: ("SIMPLE_RADIAL_FISHEYE", 4),
    9: ("RADIAL_FISHEYE", 5), 10: ("THIN_PRISM_FISHEYE", 12),
}


def read_cameras_bin(path: Path) -> dict:
    cameras = {}
    with path.open("rb") as fh:
        count = struct.unpack("<Q", fh.read(8))[0]
        for _ in range(count):
            cam_id, model_id, width, height = struct.unpack("<iiQQ", fh.read(24))
            name, n_params = CAMERA_MODELS.get(model_id, ("UNKNOWN", 4))
            params = struct.unpack(f"<{n_params}d", fh.read(8 * n_params))
            cameras[cam_id] = {
                "model": name, "width": width, "height": height, "params": params,
            }
    return cameras


def read_images_bin(path: Path) -> list:
    images = []
    with path.open("rb") as fh:
        count = struct.unpack("<Q", fh.read(8))[0]
        for _ in range(count):
            image_id, qw, qx, qy, qz, tx, ty, tz, cam_id = struct.unpack(
                "<idddddddi", fh.read(64))
            name_bytes = bytearray()
            while (ch := fh.read(1)) != b"\x00":
                if not ch:
                    raise ValueError("images.bin ended inside an image name")
                name_bytes += ch
            n_points = struct.unpack("<Q", fh.read(8))[0]
            fh.seek(24 * n_points, 1)          # skip the 2D observations
            images.append({
                "id": image_id, "name": name_bytes.decode("utf-8"),
                "camera_id": cam_id, "q": (qw, qx, qy, qz), "t": (tx, ty, tz),
            })
    return images


def quat_to_matrix(qw, qx, qy, qz):
    """COLMAP quaternion (w,x,y,z) -> 3x3 world-to-camera rotation."""
    n = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz) or 1.0
    qw, qx, qy, qz = qw / n, qx / n, qy / n, qz / n
    return (
        (1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qw * qz), 2 * (qx * qz + qw * qy)),
        (2 * (qx * qy + qw * qz), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qw * qx)),
        (2 * (qx * qz - qw * qy), 2 * (qy * qz + qw * qx), 1 - 2 * (qx * qx + qy * qy)),
    )


def build(undistorted: Path) -> dict:
    sparse = undistorted / "sparse" / "0"
    if not sparse.is_dir():
        sparse = undistorted / "sparse"
    cams_path, imgs_path = sparse / "cameras.bin", sparse / "images.bin"
    if not cams_path.is_file() or not imgs_path.is_file():
        raise FileNotFoundError(f"no cameras.bin/images.bin under {sparse}")

    cameras = read_cameras_bin(cams_path)
    images = read_images_bin(imgs_path)
    images.sort(key=lambda im: im["name"])

    out = []
    for im in images:
        R = quat_to_matrix(*im["q"])
        tx, ty, tz = im["t"]
        # C = -R^T t
        cx = -(R[0][0] * tx + R[1][0] * ty + R[2][0] * tz)
        cy = -(R[0][1] * tx + R[1][1] * ty + R[2][1] * tz)
        cz = -(R[0][2] * tx + R[1][2] * ty + R[2][2] * tz)
        # viewing direction = R^T (0,0,1) = third row of R
        dx, dy, dz = R[2][0], R[2][1], R[2][2]
        # COLMAP's camera Y points DOWN, so world up = -R^T (0,1,0) = -second
        # row of R. Averaged over the frames this is the best gravity estimate
        # a solve gives us, and it is what lets the viewer put the horizon
        # level: a reconstruction's own axes are arbitrary, and a scene that
        # sits tilted is the single thing that makes orbiting feel wrong.
        ux, uy, uz = -R[1][0], -R[1][1], -R[1][2]

        cam = cameras.get(im["camera_id"], {})
        params, height = cam.get("params", ()), cam.get("height", 0)
        fy = params[1] if cam.get("model") == "PINHOLE" and len(params) > 1 else (
            params[0] if params else 0)
        fov_y = math.degrees(2 * math.atan(height / (2 * fy))) if fy and height else None

        # apply the viewer's Y-up flip to both position and direction
        out.append({
            "name": im["name"],
            "position": [cx, -cy, -cz],
            "direction": [dx, -dy, -dz],
            "up": [ux, -uy, -uz],
            "fovY": round(fov_y, 3) if fov_y else None,
            # the frame's own size: with fovY it gives the frustum the frame
            # actually covers, which the viewer outlines in the camera view
            "width": cam.get("width") or None,
            "height": height or None,
        })

    centres = [c["position"] for c in out]
    centroid = [sum(p[i] for p in centres) / len(centres) for i in range(3)] if centres else [0, 0, 0]
    return {
        "format": "dlcameras",
        "version": 3,
        "note": ("positions, directions and up vectors are in viewer world space "
                 "(Y-up flip applied); version 2 added 'up', which the viewer "
                 "averages to level the horizon; version 3 added each frame's "
                 "width and height, for the camera view's frame outline"),
        "count": len(out),
        "centroid": centroid,
        "cameras": out,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Export COLMAP poses as cameras.json")
    ap.add_argument("undistorted", help="folder containing sparse/0/")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    src = Path(args.undistorted).expanduser().resolve()
    try:
        data = build(src)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else src / "cameras.json"
    out.write_text(json.dumps(data, indent=1))
    print(f"{data['count']} cameras -> {out}")
    if data["cameras"]:
        first = data["cameras"][0]
        print(f"first: {first['name']}  at "
              f"[{', '.join(f'{v:.2f}' for v in first['position'])}]  "
              f"fovY {first['fovY']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
