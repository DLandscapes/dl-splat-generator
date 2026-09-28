"""START HERE.pdf for a student's package -- what is inside, how to open it, its limits.

    python tools/package_readme.py <scene name> [--edge <msedge.exe>]

Written from the scene's own records (capture, site.json, the build reports),
so every package states its own facts: frames, splats, scale, north, what was
left out. HTML in the DL font (Source Sans 3), printed to PDF by Microsoft Edge
in headless mode with a throw-away profile; the HTML stays in the working
folder (working/START HERE.html), only the PDF goes into the package.

Marc's rules for teaching material apply: objective, no editorialising, no
false precision; roles, not people.
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import scene_paths   # noqa: E402

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
FONT = TOOLS.parent / "static" / "fonts" / "SourceSans3-VariableFont_wght.ttf"
VIEWER_URL = "https://digital-landscapes.com/splat-generator/"


def mb(p: Path) -> str:
    s = p.stat().st_size / 1e6
    return f"{s:.0f} MB" if s >= 10 else (f"{s:.1f} MB" if s >= 0.1 else f"{p.stat().st_size / 1e3:.0f} kB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--edge", default=str(EDGE))
    ap.add_argument("--out", default=None, help="PDF (default: the package's START HERE.pdf)")
    args = ap.parse_args()
    n = args.name
    folder = scene_paths.scene_dir(n)
    pkg = scene_paths.package_dir(n)
    sd = scene_paths.site_data_dir(n)
    out = (Path(args.out) if args.out else pkg / "START HERE.pdf").resolve()
    if out.exists():
        print(f"FAILED: {out} exists -- never overwritten", file=sys.stderr)
        return 2

    rec = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    site = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    meta = rec.get("source_meta") or {}
    # the mesh-based records exist only when the scene has a dense mesh (a machine without an
    # NVIDIA card cannot build one): each sentence that quotes them is left out without them
    def record(stem: str):
        p = folder / "mesh" / f"{stem}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    tex, ter, drw = record("texture"), record("terrain"), record("drawings")
    cams = json.loads((folder / "cameras.json").read_text(encoding="utf-8"))
    # terrain models (tools/package_dtm.py): the newest record per kind, if any
    tm = {}
    for kind in ("dsm", "dtm"):
        recs = sorted((folder / "mesh").glob(f"{kind}_*.json"))
        if recs:
            tm[kind] = json.loads(recs[-1].read_text(encoding="utf-8"))
    paths_file = sd / "cameras and sun" / f"{n}_camera_paths.json"
    paths_n = len(json.loads(paths_file.read_text(encoding="utf-8"))["paths"]) if paths_file.is_file() else 0
    # the example cameras inside the .blend files: said only when the step that put them there left its
    # record (until Capture Walk builds them itself -- pipeline request 008)
    bc_file = folder / "blend_cameras.json"
    bc = json.loads(bc_file.read_text(encoding="utf-8")) if bc_file.is_file() else None
    # the video keeps its own suffix (.mov, .mp4, ...), as the package's video step copied it
    videos = sorted((sd / "video").glob(f"{n}.*")) if (sd / "video").is_dir() else []
    video = videos[0] if videos else sd / "video" / f"{n}.mov"
    head = (folder / rec["ply"]).open("rb").read(4000).split(b"end_header")[0].decode("ascii", "replace")
    n_splats = int(head.split("element vertex ")[1].split()[0])
    metres = site["scale"]["m_per_unit"] is not None
    north = site["axes"]["y"] == "north"
    unit = "metres" if metres else "scan units (not metres)"
    addon = sorted((sd / "add-on").glob("capture_walk-*.zip"))
    addon_name = addon[0].name if addon else "capture_walk-<version>.zip"
    when = (meta.get("when") or "")[:10]
    e = html.escape

    def row(path: Path, what: str) -> str:
        rel = path.relative_to(pkg).as_posix()
        size = mb(path) if path.is_file() else ""
        return f"<tr><td class='f'>{e(rel)}</td><td>{what}</td><td class='s'>{size}</td></tr>"

    def splat_count(p: Path) -> str:
        if not p.is_file():
            return ""
        h = p.open("rb").read(4000).split(b"end_header")[0].decode("ascii", "replace")
        return f"{int(h.split('element vertex ')[1].split()[0]):,}"

    def terrain_rows(kind: str, what: str) -> list:
        r = tm.get(kind)
        if not r:
            return []
        stem = f"{n}_{kind.upper()}_{r['cell']:g}"
        cu = "m" if metres else "scan units"
        return [(sd / "terrain" / f"{stem}.tif", f"{what}: heights on a grid of {r['cell']:g} {cu} (GeoTIFF)."),
                (sd / "terrain" / f"{stem}.obj", "The same grid as a quad mesh, textured with the orthophoto "
                                                 "(+ .mtl + _ortho.jpg; the .jgw places the orthophoto in GIS)."),
                (sd / "terrain" / f"{stem}_hydro.tif", "Its water version: smoothed, every sink filled so water "
                                                       "runs off to the edge -- for flow analysis, not for measuring "
                                                       "(+ _hydro.obj)."),
                (sd / "terrain" / f"{stem}_shell.obj", f"The same as a closed shell, {r['shell']['thickness']:g} {cu} "
                                                       "thick, to cut pieces from for 3D printing.")]

    files = [
        (pkg / f"{n} - mesh.blend", "Blender, lightest: textured mesh, camera walk, video. For any laptop."),
        (pkg / f"{n} - splat very low.blend", "Blender with the smallest splat (as in the viewer, with the add-on)."),
        (pkg / f"{n} - splat low.blend", "Blender with a small splat."),
        (pkg / f"{n} - splat high.blend", "Blender with every splat. Needs a strong graphics card."),
        (pkg / f"{n}.dlscene", "The scan for the Splat Generator viewer, with its cameras and walk (one file)."),
        (sd / "splat" / f"{n}_3DGS_high.ply", f"Gaussian splat, every splat ({splat_count(sd / 'splat' / f'{n}_3DGS_high.ply')})."),
        (sd / "splat" / f"{n}_3DGS_med.ply", f"The same scene trained with {splat_count(sd / 'splat' / f'{n}_3DGS_med.ply')} splats."),
        (sd / "splat" / f"{n}_3DGS_low.ply", f"Trained with {splat_count(sd / 'splat' / f'{n}_3DGS_low.ply')} splats."),
        (sd / "splat" / f"{n}_3DGS_verylow.ply", f"Trained with {splat_count(sd / 'splat' / f'{n}_3DGS_verylow.ply')} splats."),
        (sd / "mesh" / f"{n}_mesh.obj", "Triangulated surface with texture, only where the video saw it "
                                         "(+ .mtl + _texture.jpg)."),
        (sd / "mesh" / f"{n}_mesh_ao.png", "Ambient occlusion baked from the mesh (darker in cavities)."),
        (sd / "mesh" / f"{n}_mesh_bump.jpg", "Bump map ESTIMATED from the texture's brightness (dark read as "
                                             "low) -- for the look, not a measurement."),
        (sd / "point cloud" / f"{n}_points.ply", "The dense point cloud, with colour."),
        *terrain_rows("dsm", "Surface model (the top of what was scanned)"),
        *terrain_rows("dtm", "Ground model (the points classified as ground)"),
        (sd / "terrain" / f"{n}_contours.dxf", "Contour lines, 3D, at their height."),
        (sd / "terrain" / f"{n}_ground.ply", "The points classified as ground."),
        (sd / "cameras and sun" / f"{n}.3dm", "Rhino: mesh, camera path, cameras, named views."),
        (sd / "cameras and sun" / f"{n}.glb", "glTF (Blender, other programs): mesh, cameras, path."),
        (sd / "cameras and sun" / f"{n}_camera_paths.glb", f"{paths_n} example camera moves as animated cameras "
                                                           "(+ .json with every frame, + .dxf with the paths)"
                                                           + ("; the same moves are in the Blender files." if bc else ".")),
        (sd / "cameras and sun" / f"{n}_cameras.xlsx", "Every frame: time, position, direction, sun. (+ .csv)"),
        (sd / "drawings" / f"{n}_plan.png", "Plan (+ with contours; world files .pgw for GIS)."),
        (sd / "drawings" / f"{n}_elevation_seen_from_walk_start.png", "Elevations, four sides, same scale as the plan."),
        (sd / "drawings" / f"{n}_plan.dxf", "Plan as DXF: camera path and positions, contours, outline."),
        (video, "The original video."),
        (sd / "add-on" / addon_name, "Capture Walk, the Blender add-on (licence and credits beside it)."),
    ]
    rows = "\n".join(row(p, w) for p, w in files if p.exists())
    missing = [p.relative_to(pkg).as_posix() for p, _ in files if not p.exists()]

    status = [
        ("Scale", "set: " + site["scale"]["note"] if metres else
         "NOT SET. Every file is in scan units: shapes and proportions are right, sizes are not "
         "metres. One known distance, measured on site, gives the scale."),
        ("North", "set" if north else
         "NOT SET. +Y is the direction the walk went, not north; there is no sun in the files yet."),
        ("Up", "levelled from how the phone was held during the walk."),
        ("Origin 0,0,0", "the ground below the first camera position."),
    ]
    status_rows = "\n".join(f"<tr><td class='f'>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in status)
    # since 2026-09-27 the package mesh leaves out the same triangles as the drawings (mesh_texture.py)
    mesh_holes = bool((tex or {}).get("package_mesh"))
    unseen_note = ("<li>Where the camera did not look, there is nothing"
                   + (f": the mesh has holes there and the drawings are white (on this scan about "
                      f"{round(drw['left_out']['share_area'] * 100)} % of the scanned surface's area, most of it "
                      "triangles the mesher had stretched across unfilmed gaps)" if drw and mesh_holes else
                      f": in the drawings such places are white (on this scan about "
                      f"{round(drw['left_out']['share_area'] * 100)} % of the mesh's area was left out of the "
                      "drawings for that reason)" if drw else "")
                   + ". The ground right below the walk was usually not filmed.</li>")
    texture_note = (f"<li>About {round(tex.get('texels_painted_directly', tex['texels_painted']) * 100)} % of the "
                    "mesh texture comes from the video; the rest is neutral grey. Faint seams appear where "
                    "neighbouring parts came from different frames.</li>" if tex else "")
    contour_note = (f"<li>Contours: every {ter['contours']['interval']:g} {'m' if metres else 'scan units'} "
                    "(every 5th darker).</li>" if ter else "")
    terrain_html = ""
    terrain_note = ""
    if tm:
        notes = []
        if tm.get("dsm"):
            notes.append(f"in the surface model {round(tm['dsm']['grid']['filled_share'] * 100)} % of the cells "
                         "are holes filled with a smooth guess")
        dg = (tm.get("dtm") or {}).get("grid") or {}
        if "without_ground_share" in dg:
            notes.append(f"in the ground model {round(dg['without_ground_share'] * 100)} % of the cells have no "
                         "ground points of their own: small gaps (a stone, a tuft) are bridged from the ground "
                         "around them, the rest follows the scanned surface")
        elif dg:
            notes.append(f"in the ground model {round(dg['filled_share'] * 100)} % of the cells are holes filled "
                         "with a smooth guess")
        terrain_note = ("<li>Terrain models: " + e("; ".join(notes)) + ".</li>") if notes else ""
        r0 = tm.get("dsm") or tm["dtm"]
        cu = "m" if metres else "scan units"
        ground_how = ("the points classified as ground; where there are none, or where rock stands above "
                      "them, the scanned surface, with stones and tufts in small gaps left out"
                      if "without_ground_share" in dg else
                      "only the points classified as ground; steep rock faces drop out of it")
        terrain_html = f"""
<h3>Terrain models, water flow and 3D printing</h3>
<p>Two height models on the same grid of {r0['cell']:g} {cu}, one height per point (no overhangs):
the <b>surface model</b> (DSM, the top of what was scanned: rock, stones, planting) and the <b>ground model</b>
(DTM, {ground_how}). Each comes as a GeoTIFF
(QGIS, DL-TerrainDiversity for slope and water flow), as a quad mesh (Blender, Rhino: import with Up = Z,
Forward = Y) and as a closed shell. To print a piece: in Blender or Rhino, place a box over the part you want,
take the boolean intersection with the shell, then scale the piece for the printer. The underside follows the
terrain, so the print needs supports.</p>
<p>Single cells and small groups that stood far above or below everything around them (errors of the
reconstruction) were removed and filled in from their surroundings. For water, use the <b>_hydro</b> version of
either model: smoothed, and with every sink filled so that water can run off every cell to the edge.</p>
<p class="note">In scan units until the scale is set: slope angles and flow directions are right, lengths and
areas are not metres (DL-TerrainDiversity reads heights as metres).</p>
"""
    plan = sd / "drawings" / f"{n}_plan_contours.png"
    sw_rec = json.loads((folder / "splat_switch.json").read_text(encoding="utf-8")) \
        if (folder / "splat_switch.json").is_file() else None
    sw_prop = bool(sw_rec and (sw_rec.get("object_property") or (folder / "splat_switch_ui.json").is_file()))
    # switch v2 (2026-09-27 night; Marc: "the splat is still emitting light"): the files open with Blender's
    # own drawing (the add-on's viewer drawing off at load) and the switch at 0 -- lit, not glowing
    sw_v2 = (folder / "splat_switch_v2.json").is_file()
    switch_li = (f"<li>The splat takes Blender's light: it opens lit by the scene's lights and sky, like the mesh "
                 f"(add a lamp to light it your way). To show it with the colours as filmed, glowing by itself: "
                 f"select <b>{e(n)} splat</b>, Object Properties &rsaquo; Custom Properties &rsaquo; <b>light "
                 "switch</b> = 1 (0 = lit, as it opens). The direction each splat faces comes from the splat's own "
                 "flattest axis; its colour already holds the light of the day it was filmed, so shade appears "
                 "twice when it is lit. (The splat's own material slot, capture_walk_splat, is not the one drawn "
                 "-- a note in it says so.)</li>"
                 if sw_v2 else
                 f"<li>The splat has a light switch: select <b>{e(n)} splat</b>, Object Properties &rsaquo; "
                 "Custom Properties &rsaquo; <b>light switch</b> -- 1 (as it opens): the splat's colour as it was "
                 "filmed, glowing by itself; 0: lit by Blender's lights like the mesh (add a lamp). It acts on "
                 "Blender's own drawing of the splat: with the add-on, first untick <b>Draw like the viewer</b> "
                 "(sidebar N &rsaquo; Capture Walk) -- the add-on's viewer look is never lit. The direction each "
                 "splat faces comes from the splat's own flattest axis; its colour already holds the light of the "
                 "day it was filmed, so shade appears twice at 0. (The splat's own material slot, "
                 "capture_walk_splat, is not the one drawn -- change the switch, not that material.)</li>"
                 if sw_prop else
                 "<li>The splat has a light switch: in the material <b>capture_walk_splat_billboard</b>, the "
                 "value <b>light switch</b> -- 1 (as it opens): the splat's captured colour as it was filmed; "
                 "0: lit by Blender's lights like the mesh. The direction each splat faces comes from the "
                 "splat's own flattest axis; its colour already holds the light of the day it was filmed, so "
                 "shade appears twice at 0.</li>"
                 if sw_rec else "")
    draw_note = ("The splat opens in Blender's own drawing: flat patches that take Blender's lights. With the "
                 "add-on, <b>Draw like the viewer</b> (sidebar N &rsaquo; Capture Walk) shows it as in the viewer: "
                 "fast and as filmed, but never lit."
                 if sw_v2 else
                 "With the add-on, the splat is drawn as in the viewer. Without it the file still opens and shows "
                 "the splat\nas flat patches.")
    maps_li = (f"<li>A second material is in each file: <b>{e(n)} mesh - with maps</b> (the texture with the "
               "baked ambient occlusion and the estimated bump). To use it: select the mesh, Material "
               "Properties, and pick it from the list; the plain one stays as it was.</li>"
               if (sd / "mesh" / f"{n}_mesh_ao.png").is_file() else "")
    if bc:
        cams_li = (f"<li>Example camera moves are in each Blender file: {len(bc['moves'])} cameras named "
                   f"<b>{e(n)} camera - &hellip;</b> ({e(', '.join(bc['moves']))}), each with its path and kept to "
                   f"what the video saw, zoomed in {bc['zoom']:g}&times; so the frame stays filled. To look through "
                   "one: select it, View &rsaquo; Cameras &rsaquo; Set Active Object as Camera, and press Play. "
                   f"<b>{e(n)} camera</b> is the camera as filmed.</li>")
    else:
        cams_li = (f"<li>Example camera moves: File &rsaquo; Import &rsaquo; glTF &rsaquo; "
                   f"<code>cameras and sun/{e(n)}_camera_paths.glb</code> &mdash; {paths_n} animated cameras "
                   "(smoothed walk, walk with a level horizon, orbit, pass-by, rise and tilt to a view straight "
                   "down, push-in, dolly zoom), each kept to what the video saw. Select one and make it the active "
                   "camera. The level walk's lens shift and the dolly zoom's changing lens are in the .json only.</li>")

    body = f"""
<header>
  <p class="kicker">Digital Landscapes &middot; DL-SplatGenerator</p>
  <h1>{e(n)} &mdash; start here</h1>
  <p class="lede">A 3D scan of a site made from one phone video ({e(meta.get('file') or n)},
  about {round(float(meta.get('duration_s') or 0))} s, filmed {e(when)}): {len(cams.get('cameras', []))} of
  the video's frames were placed in 3D, and a Gaussian splat of
  {n_splats:,} splats, a textured mesh, a terrain model and drawings
  were made from them. All files use one coordinate system, so they line up in any program.</p>
</header>

<section>
<h2>This scan</h2>
<table class="status">{status_rows}</table>
<p class="note">The coordinate system: Z is up; Y is {'north' if north else 'the walk&rsquo;s direction'};
X is at right angles to both; units: {unit}. The same in Blender, Rhino, the drawings and the spreadsheet.</p>
</section>

<section>
<h2>What is inside</h2>
<table class="files">{rows}</table>
<p class="note">Keep the folder together: the .blend finds the video, the splat and the mesh by their place in it.</p>
</section>

<section>
<h2>Open it</h2>
<h3>Blender (4.5 LTS or newer)</h3>
<ol>
<li>Install the add-on once: Edit &rsaquo; Preferences &rsaquo; Get Extensions &rsaquo; the menu at the top right
&rsaquo; Install from Disk &rsaquo; <code>{e(n)} - site data/add-on/{e(addon_name)}</code>.</li>
<li>Open one of the four Blender files &mdash; the same scene, four weights:
<b>{e(n)} - mesh</b> (no splat; runs without a graphics card), <b>{e(n)} - splat very low</b> and
<b>{e(n)} - splat low</b> (small splats), <b>{e(n)} - splat high</b> (every splat; a strong graphics card).
Start with the lightest; if navigating is smooth, try the next. Each opens in the camera's view; press Play
to walk the capture.</li>
<li>Add your own objects to the collection <b>Your objects</b>.</li>
{cams_li}
{maps_li}
{switch_li}
</ol>
<p class="note">{draw_note} Blender's own render (F12) shows those patches; the add-on's <i>Render splat frames</i> gives the
viewer's look. The mesh is a reference for placing objects and is left out of renders.</p>

<h3>The Splat Generator viewer (a browser, no installation)</h3>
<p>Open {e(VIEWER_URL)} and drop <code>{e(n)}.dlscene</code> onto the page: the scan opens with its
camera views and the walk. There you can measure, set the scale from a known distance, set north, and save stills.</p>

<h3>Rhino</h3>
<p>Open <code>cameras and sun/{e(n)}.3dm</code> (mesh, camera path, cameras, named views of the camera), or import
<code>mesh/{e(n)}_mesh.obj</code>, <code>terrain/{e(n)}_contours.dxf</code> and <code>drawings/{e(n)}_plan.dxf</code>.
Rhino is Z-up like these files; no rotation is needed.</p>

{terrain_html}
<h3>Other programs</h3>
<p><code>.glb</code>: most 3D programs. <code>.ply</code> point clouds: CloudCompare, Rhino, Blender.
<code>.tif</code> and the plans with their <code>.pgw</code>: QGIS. <code>.xlsx</code>/<code>.csv</code>: Excel,
Grasshopper.</p>
</section>

<section>
<h2>What a scan like this is not</h2>
<ul>
<li>A reconstruction from photographs, not a survey. It shows what the camera saw, from where it was.</li>
{unseen_note}
<li>Where planting covers the ground, the &ldquo;terrain&rdquo; is the top of the planting.</li>
{terrain_note}
{texture_note}
{contour_note}
<li>The video and the spreadsheet hold the date, time and place of filming. That is personal data about
the person who filmed; people who appear in the video are too. Check before sharing either.</li>
</ul>
</section>

<section>
<h2>Made with</h2>
<p class="small">DL-SplatGenerator, Digital Landscapes (Apache-2.0) &middot; Capture Walk for Blender, Digital Landscapes
(GPL-2.0-or-later) &middot; COLMAP (BSD-3-Clause), camera solve &middot; Brush (Apache-2.0), splat training &middot;
Spark (MIT) and three.js (MIT), the viewer &middot; FFmpeg, frame extraction &middot; OpenCV and YuNet, face masking &middot;
xatlas (MIT), mesh unfolding &middot; rhino3dm (MIT), openpyxl (MIT), NumPy (BSD) &middot; Blender. Gaussian splatting:
Kerbl et al. 2023, doi:10.1145/3592433. Ground filter: Zhang et al. 2016, doi:10.3390/rs8060501. Sun position: NOAA
Solar Calculator (Meeus). Made {time.strftime('%Y-%m-%d')}.</p>
</section>
"""
    if missing:
        body += "<p class='small'>Not in this package: " + e(", ".join(missing)) + "</p>"

    css = f"""
@font-face {{ font-family: "Source Sans 3"; src: url("{FONT.as_uri()}") format("truetype-variations"); font-weight: 200 900; }}
@page {{ size: A4; margin: 16mm 16mm 18mm 16mm; }}
:root {{ --ink: #1e1e1e; --soft: #5b5f66; --line: #d9dbde; --accent: #b45a1e; }}
body {{ font-family: "Source Sans 3", sans-serif; color: var(--ink); font-size: 10pt; line-height: 1.35; margin: 0; background: #fff; }}
.kicker {{ color: var(--soft); font-size: 9pt; letter-spacing: .04em; text-transform: uppercase; margin: 0 0 2mm; }}
h1 {{ font-size: 22pt; font-weight: 600; margin: 0 0 3mm; }}
h2 {{ font-size: 13pt; font-weight: 600; margin: 5mm 0 1.5mm; border-bottom: 1px solid var(--line); padding-bottom: 1mm; }}
h3 {{ font-size: 11pt; font-weight: 600; margin: 4mm 0 1mm; }}
.lede {{ font-size: 11.5pt; margin: 0 0 3mm; }}
table {{ border-collapse: collapse; width: 100%; }}
td {{ padding: 0.8mm 2mm 0.8mm 0; vertical-align: top; border-bottom: 1px solid var(--line); }}
td.f {{ white-space: nowrap; font-weight: 600; padding-right: 4mm; }}
td.s {{ white-space: nowrap; text-align: right; color: var(--soft); }}
table.files td {{ font-size: 9pt; }}
.note {{ color: var(--soft); font-size: 9.5pt; margin: 2mm 0 0; }}
.small {{ color: var(--soft); font-size: 8.5pt; }}
code {{ font-family: "Source Sans 3", sans-serif; font-weight: 600; }}
ol, ul {{ margin: 1mm 0 0 5mm; padding: 0; }}
li {{ margin: 0 0 1mm; }}
.page {{ break-before: page; }}
"""
    doc = f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{e(n)} - start here</title><style>{css}</style></head><body>{body}</body></html>"
    # a test run (--out) keeps its HTML beside its own PDF: the working copy belongs to the package's PDF
    html_path = out.with_suffix(".html") if args.out else folder / "START HERE.html"
    if args.out and html_path.exists():
        print(f"FAILED: {html_path} exists -- never overwritten", file=sys.stderr)
        return 2
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(doc, encoding="utf-8")

    edge = Path(args.edge)
    if not edge.is_file():
        print(f"FAILED: no Microsoft Edge at {edge}; the HTML is at {html_path}", file=sys.stderr)
        return 3
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as prof:
        cmd = [str(edge), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={prof}",
               "--no-pdf-header-footer", f"--print-to-pdf={out}", html_path.as_uri()]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        for _ in range(40):
            if out.is_file() and out.stat().st_size > 0:
                break
            time.sleep(0.5)
    if not out.is_file():
        print(f"FAILED: Edge wrote no PDF ({(run.stderr or '').strip()[-300:]}); the HTML is at {html_path}",
              file=sys.stderr)
        return 4
    print(f"    written {out}  ({mb(out)})" + (f"; not in the package yet: {', '.join(missing)}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
