"""A scene's cameras, walk, mesh, north and sun for other programs: <name>.3dm and <name>.glb.

    python tools/site_exports.py <scene name> [--out <folder>] [--views-every 10]

For the student package (pipeline\\requests\\004), item 4: "the camera positions
and the sun position that can be imported both to blender, but also to Rhino".
Both files are in site.json's frame (tools/site_frame.py: Z up, Y north or the
walk, origin on the ground below the first camera, metres when scaled), so they
line up with the package's OBJ, splats and contours.

  <name>.3dm   Rhino (written with rhino3dm, MIT). Layers under "<name>":
                 mesh          the textured mesh (texture: ..\\mesh\\<name>_mesh_texture.jpg)
                 camera path   a polyline through every placed camera
                 cameras       a line per camera: where it stood -> where it looked
                 notes         text dots: the origin, and what is not set yet
               Named views "<name> frame NNN" every --views-every frames, with the
               frame's own field of view (the frustum, not a guessed lens).
               Once north AND scale are set: units metres, Rhino's sun at the
               capture's date, time and place, and the earth anchor (the
               real-world position of the origin). Until then none of these is
               guessed; the notes say so.
  <name>.glb   glTF 2.0, one file (Blender: File > Import > glTF; many other
               programs): the textured mesh, every placed camera as a camera
               ("<name> frame NNN"), the walk as a line, and -- once north is
               set -- a sun (KHR_lights_punctual) and a "<name> north" empty.
               glTF is Y-up: site (x, y, z) is written as (x, z, -y), which the
               Blender importer turns back into Z-up.

Place and time are personal data: the sun and the earth anchor are written only
with --with-place (for a file that goes back to the person who filmed it).
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import colmap_cameras    # noqa: E402
import dense_mesh        # noqa: E402
import scene_paths       # noqa: E402
import splat_levels      # noqa: E402  -- quat_from_matrix
import sun_pos           # noqa: E402

SITE_TO_GLTF = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float)   # (x, y, z) -> (x, z, -y)


# ------------------------------------------------------------------ inputs

def read_obj(path: Path):
    """The package's own OBJ: v, vt, vn, triangles with v/vt/vn = one index."""
    V, T, N, F = [], [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("v "):
            V.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("vt "):
            T.append([float(x) for x in line.split()[1:3]])
        elif line.startswith("vn "):
            N.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "):
            F.append([int(p.split("/")[0]) - 1 for p in line.split()[1:4]])
    return np.array(V), np.array(T), np.array(N), np.array(F, np.int64)


def cameras_site(name: str, M: np.ndarray) -> list[dict]:
    """Placed cameras in site coordinates, filming order: position, right,
    up, forward (unit), vertical and horizontal field of view.

    The LENS-CORRECTED cameras (work/<name>/undistorted/sparse/0, PINHOLE,
    1059 x 1887 for one student capture): the frames the splat was trained on and the mesh
    texture was painted from. Found 2026-09-27: taking the solve's own camera
    (OPENCV, the phone's 1080 x 1920 WITH lens distortion) gave a view 0.8 deg
    wider than the frames, and the .glb render missed the video frame by a
    visible shift (MAE 0.165 vs 0.090)."""
    work = scene_paths.ROOT / "work" / name
    und = work / "undistorted" / "sparse" / "0"
    model = und if (und / "images.bin").is_file() else dense_mesh.find_model(work)
    cams = colmap_cameras.read_cameras_bin(model / "cameras.bin")
    A = M[:3, :3]
    Rs = A / np.cbrt(np.linalg.det(A))
    out = []
    for im in sorted(colmap_cameras.read_images_bin(model / "images.bin"), key=lambda r: r["name"]):
        R = np.asarray(colmap_cameras.quat_to_matrix(*im["q"]), float)
        c = -R.T @ np.asarray(im["t"], float)
        cam = cams[im["camera_id"]]
        fx, fy = cam["params"][0], cam["params"][1]
        out.append({"name": im["name"], "frame": int("".join(ch for ch in im["name"] if ch.isdigit()) or 0),
                    "pos": A @ c + M[:3, 3], "right": Rs @ R[0], "up": -(Rs @ R[1]), "fwd": Rs @ R[2],
                    "fov_v": 2 * math.atan(cam["height"] / (2 * fy)),
                    "fov_h": 2 * math.atan(cam["width"] / (2 * fx)),
                    "aspect": cam["width"] / cam["height"]})
    return out


def sun_at_start(rec: dict, with_place: bool):
    """(azimuth, elevation, when, lat, lon, alt) at the clip's start, or None."""
    if not with_place or not rec.get("source"):
        return None
    import camera_table
    import source_meta
    meta = source_meta.read(Path(rec["source"]))
    when = camera_table.parse_when(meta.get("when"))
    where = meta.get("where") or {}
    if not when or where.get("lat") is None:
        return None
    s = sun_pos.sun_position(sun_pos.utc_ms(when), where["lat"], where["lon"])
    return {"azimuth": s["azimuth"], "elevation": s["elevation"], "when": when,
            "lat": where["lat"], "lon": where["lon"], "alt": where.get("alt")}


def sun_vector(az: float, el: float) -> np.ndarray:
    """Towards the sun, in site coordinates -- valid only when +Y is north."""
    a, e = math.radians(az), math.radians(el)
    return np.array([math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)])


# ------------------------------------------------------------------ .3dm

def write_3dm(path: Path, name: str, site: dict, mesh, cams: list, sun, views_every: int,
              texture_rel: str) -> dict:
    import rhino3dm as rh
    V, T, N, F = mesh
    f = rh.File3dm()
    scaled = site["scale"]["m_per_unit"] is not None
    north = site["axes"]["y"] == "north"
    f.Settings.ModelUnitSystem = rh.UnitSystem.Meters if scaled else getattr(rh.UnitSystem, "None")

    def layer(label, rgb, parent=None):
        L = rh.Layer()
        L.Name = label
        L.Color = (*rgb, 255)
        if parent is not None:
            L.ParentLayerId = f.Layers.FindIndex(parent).Id
        return f.Layers.Add(L)

    root = layer(name, (120, 120, 120))
    l_mesh = layer(f"{name} mesh", (160, 160, 160), root)
    l_path = layer(f"{name} camera path", (230, 120, 30), root)
    l_cams = layer(f"{name} cameras", (40, 150, 230), root)
    l_note = layer(f"{name} notes", (220, 40, 40), root)

    mat = rh.Material()
    mat.Name = f"{name} mesh"
    mat.DiffuseColor = (255, 255, 255, 255)
    tex = rh.Texture()
    tex.FileName = texture_rel
    mat.SetBitmapTexture(tex)
    mi = f.Materials.Add(mat)

    m = rh.Mesh()
    for (x, y, z) in V:
        m.Vertices.Add(float(x), float(y), float(z))
    for a, b, c in F:
        m.Faces.AddFace(int(a), int(b), int(c))
    tcs = m.TextureCoordinates
    for (u, v) in T:
        # rhino3dm 8.35's Python binding exposes MeshTextureCoordinateList.Add as
        # __add__ (a naming slip in the binding); same signature (u, v) -> index
        tcs.__add__(float(u), float(v))
    for (x, y, z) in N:
        m.Normals.Add(float(x), float(y), float(z))
    at = rh.ObjectAttributes()
    at.Name, at.LayerIndex = f"{name} mesh", l_mesh
    at.MaterialIndex, at.MaterialSource = mi, rh.ObjectMaterialSource.MaterialFromObject
    f.Objects.AddMesh(m, at)

    walk = float(np.sum(np.linalg.norm(np.diff([c["pos"] for c in cams], axis=0), axis=1)))
    pl = rh.Polyline(len(cams))
    for c in cams:
        pl.Add(*[float(v) for v in c["pos"]])
    at = rh.ObjectAttributes()
    at.Name, at.LayerIndex = f"{name} camera path", l_path
    f.Objects.AddPolyline(pl, at)

    arrow = 0.05 * walk
    for c in cams:
        at = rh.ObjectAttributes()
        at.Name, at.LayerIndex = f"{name} frame {c['frame']:03d}", l_cams
        a, b = c["pos"], c["pos"] + arrow * c["fwd"]
        f.Objects.AddLine(rh.Point3d(*map(float, a)), rh.Point3d(*map(float, b)), at)

    n_views = 0
    for i, c in enumerate(cams):
        if not (i % views_every == 0 or i == len(cams) - 1):
            continue
        vp = rh.ViewportInfo()
        vp.SetCameraLocation(rh.Point3d(*map(float, c["pos"])))
        vp.SetCameraDirection(rh.Vector3d(*map(float, c["fwd"])))
        vp.SetCameraUp(rh.Vector3d(*map(float, c["up"])))
        near = 0.01 * walk
        th, tw = near * math.tan(c["fov_v"] / 2), near * math.tan(c["fov_h"] / 2)
        vp.SetFrustum(-tw, tw, -th, th, near, 100 * walk)
        vi = rh.ViewInfo()
        vi.Name = f"{name} frame {c['frame']:03d}"
        vi.Viewport = vp
        f.NamedViews.Add(vi)
        n_views += 1

    def dot(text, p):
        at = rh.ObjectAttributes()
        at.LayerIndex = l_note
        f.Objects.AddTextDot(text, rh.Point3d(*map(float, p)), at)

    dot(f"{name}: origin - the ground below the first camera", (0, 0, 0))
    notes = []
    if not north:
        notes.append("north not set: +Y is the walk's direction, not north")
    if not scaled:
        notes.append("NOT TO SCALE: scan units, not metres")
    for k, t in enumerate(notes):
        dot(t, (0, 0, -(k + 1) * 0.04 * walk))

    placed_sun = False
    if sun and north and scaled:
        S = f.Settings.RenderSettings.Sun
        S.Latitude, S.Longitude = float(sun["lat"]), float(sun["lon"])
        S.North = 90.0                       # Rhino: the angle of north from +X; our north is +Y
        w = sun["when"]
        S.Year, S.Month, S.Day = w.year, w.month, w.day
        S.Hours = w.hour + w.minute / 60 + w.second / 3600
        S.TimeZone = w.utcoffset().total_seconds() / 3600
        S.EnableOn = True
        E = f.Settings.EarthAnchorPoint
        E.EarthBasepointLatitude, E.EarthBasepointLongitude = float(sun["lat"]), float(sun["lon"])
        if sun.get("alt") is not None:
            E.EarthBasepointElevation = float(sun["alt"]) - float(site["first_camera_height"])
        E.ModelBasePoint = rh.Point3d(0, 0, 0)
        E.ModelNorth = rh.Vector3d(0, 1, 0)
        E.ModelEast = rh.Vector3d(1, 0, 0)
        placed_sun = True
    f.Write(str(path), 8)
    return {"views": n_views, "cameras": len(cams), "sun": placed_sun}


# ------------------------------------------------------------------ .glb

def write_glb(path: Path, name: str, site: dict, mesh, cams: list, sun, texture: Path) -> dict:
    V, T, N, F = mesh
    G = lambda P: np.asarray(P, float) @ SITE_TO_GLTF.T          # noqa: E731
    bins, views, accessors = [], [], []

    def add(data: bytes, target=None) -> int:
        off = sum(len(b) for b in bins)
        pad = (-off) % 4
        if pad:
            bins.append(b"\0" * pad)
            off += pad
        bins.append(data)
        v = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target:
            v["target"] = target
        views.append(v)
        return len(views) - 1

    def acc(arr: np.ndarray, ctype: int, typ: str, target=None, minmax=False) -> int:
        a = np.ascontiguousarray(arr)
        view = add(a.tobytes(), target)
        d = {"bufferView": view, "componentType": ctype, "count": int(len(a)), "type": typ}
        if minmax:
            d["min"] = a.min(axis=0).tolist()
            d["max"] = a.max(axis=0).tolist()
        accessors.append(d)
        return len(accessors) - 1

    pos = acc(G(V).astype("<f4"), 5126, "VEC3", 34962, True)
    nrm = acc(G(N).astype("<f4"), 5126, "VEC3", 34962)
    uv = acc(np.stack([T[:, 0], 1 - T[:, 1]], 1).astype("<f4"), 5126, "VEC2", 34962)
    idx = acc(F.reshape(-1).astype("<u4"), 5125, "SCALAR", 34963)
    img_view = add(texture.read_bytes())

    path_acc = acc(G([c["pos"] for c in cams]).astype("<f4"), 5126, "VEC3", 34962, True)

    nodes, cameras = [], []
    meshes = [
        {"name": f"{name} mesh", "primitives": [{"attributes": {"POSITION": pos, "NORMAL": nrm,
                                                                "TEXCOORD_0": uv},
                                                 "indices": idx, "material": 0}]},
        {"name": f"{name} camera path", "primitives": [{"attributes": {"POSITION": path_acc},
                                                        "mode": 3, "material": 1}]},
    ]
    nodes.append({"name": f"{name} mesh", "mesh": 0})
    nodes.append({"name": f"{name} camera path", "mesh": 1})
    for c in cams:
        # a glTF camera looks down its -Z with +Y up
        Rm = SITE_TO_GLTF @ np.stack([c["right"], c["up"], -c["fwd"]], axis=1)
        w, x, y, z = splat_levels.quat_from_matrix(Rm)
        cameras.append({"type": "perspective", "name": f"{name} frame {c['frame']:03d}",
                        "perspective": {"yfov": c["fov_v"], "aspectRatio": c["aspect"],
                                        "znear": 0.01}})
        nodes.append({"name": f"{name} frame {c['frame']:03d}", "camera": len(cameras) - 1,
                      "translation": G(c["pos"]).tolist(), "rotation": [x, y, z, w]})
    ext_used = []
    extensions = {}
    north = site["axes"]["y"] == "north"
    if north:
        # an empty whose +Y points north (site +Y), at the origin
        nodes.append({"name": f"{name} north"})
        if sun:
            s = sun_vector(sun["azimuth"], sun["elevation"])
            # the light shines down its -Z: -Z = -s  ->  +Z = s
            zax = SITE_TO_GLTF @ s
            xax = np.cross([0, 1, 0], zax)
            xax = xax / np.linalg.norm(xax) if np.linalg.norm(xax) > 1e-9 else np.array([1, 0, 0])
            yax = np.cross(zax, xax)
            w, x, y, z = splat_levels.quat_from_matrix(np.stack([xax, yax, zax], 1))
            extensions["KHR_lights_punctual"] = {"lights": [{"name": f"{name} sun", "type": "directional",
                                                             "intensity": 3.0}]}
            ext_used.append("KHR_lights_punctual")
            nodes.append({"name": f"{name} sun", "rotation": [x, y, z, w],
                          "extensions": {"KHR_lights_punctual": {"light": 0}}})
    doc = {
        "asset": {"version": "2.0", "generator": "DL-SplatGenerator tools/site_exports.py (Digital Landscapes)",
                  "extras": {"coordinates": "site.json, Z up (written Y-up as glTF requires)",
                             "units": site["units"], "scale": site["scale"]["status"],
                             "y_axis": site["axes"]["y"]}},
        "scene": 0, "scenes": [{"name": name, "nodes": list(range(len(nodes)))}],
        "nodes": nodes, "meshes": meshes, "cameras": cameras,
        "materials": [
            {"name": f"{name} mesh", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0},
                                                             "metallicFactor": 0.0, "roughnessFactor": 1.0}},
            {"name": f"{name} camera path", "pbrMetallicRoughness": {"baseColorFactor": [0.9, 0.47, 0.12, 1],
                                                                    "metallicFactor": 0.0}}],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "images": [{"bufferView": img_view, "mimeType": "image/jpeg", "name": texture.stem}],
        "accessors": accessors, "bufferViews": views,
        "buffers": [{"byteLength": sum(len(b) for b in bins)}],
    }
    if ext_used:
        doc["extensionsUsed"] = ext_used
        doc["extensions"] = extensions
    js = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    js += b" " * ((-len(js)) % 4)
    blob = b"".join(bins)
    blob += b"\0" * ((-len(blob)) % 4)
    with path.open("wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(blob)))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        fh.write(struct.pack("<II", len(blob), 0x004E4942) + blob)
    return {"cameras": len(cameras), "sun": "KHR_lights_punctual" in ext_used, "north": north}


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--out", default=None, help="folder (default: the package's site data/cameras and sun/)")
    ap.add_argument("--views-every", type=int, default=10)
    ap.add_argument("--with-place", action="store_true",
                    help="write the sun and the earth anchor (date, time, GPS: personal data)")
    args = ap.parse_args()

    folder = scene_paths.scene_dir(args.name)
    rec = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    sd = scene_paths.site_data_dir(args.name)
    out = Path(args.out) if args.out else sd / "cameras and sun"
    obj = sd / "mesh" / f"{args.name}_mesh.obj"
    tex = sd / "mesh" / f"{args.name}_mesh_texture.jpg"
    if not obj.is_file():
        print(f"FAILED: no {obj} -- run tools/mesh_texture.py first", file=sys.stderr)
        return 2
    targets = [out / f"{args.name}.3dm", out / f"{args.name}.glb"]
    for t in targets:
        if t.exists():
            print(f"FAILED: {t} exists -- never overwritten", file=sys.stderr)
            return 2
    out.mkdir(parents=True, exist_ok=True)

    M = np.asarray(site["matrix"], float)
    mesh = read_obj(obj)
    cams = cameras_site(args.name, M)
    sun = sun_at_start(rec, args.with_place)
    rel = Path("..") / "mesh" / tex.name
    r3 = write_3dm(targets[0], args.name, site, mesh, cams, sun, args.views_every, str(rel))
    rg = write_glb(targets[1], args.name, site, mesh, cams, sun, tex)
    print(f"    {targets[0].name}: {len(mesh[0]):,} vertices, {r3['cameras']} cameras, "
          f"{r3['views']} named views, sun {'set' if r3['sun'] else 'not set (needs north and scale)'}")
    print(f"    {targets[1].name}: {rg['cameras']} cameras, north {'set' if rg['north'] else 'not set'}, "
          f"sun {'set' if rg['sun'] else 'not set'}")
    for t in targets:
        print(f"    written {t}  ({t.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
