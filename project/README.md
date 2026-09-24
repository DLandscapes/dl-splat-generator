# DL-SplatGenerator

Site captures into measurable 3D scenes, for landscape architecture. Make a
Gaussian-splat scene from a phone video, a folder of stills or a single photo;
set its real-world scale; measure and annotate it; walk the path it was filmed
from; and hand it on — as a self-contained bundle that embeds in a web page, or
as a package the Capture Walk add-on opens in Blender.

This is the **technical reference**: how each stage works, why, and what was
measured. For what the tool is and how to start, see the
[README at the repository root](../README.md).

Named `DL-3DGS` until 2026-09-06; renamed to match the other Digital Landscapes
tools — `DL-TerrainMapper`, `DL-TerrainSlicer`, `DL-TerrainDiversity`.

The **viewer half is entirely client-side** — no server logic, no build step, no
network requests — so an exported scene drops straight into
digital-landscapes.com. Python appears only in the generator half, where local
GPU training genuinely needs it.

## Running it

```bash
..\start.bat
```

It sits at the project root, one level up from here, so the whole tool is one
double-click from the top folder. It uses the venv when there is one — that is
the full app, viewer plus capture backend — and falls back to whatever `python`
is on PATH, which gives the viewer alone.

Or directly:

```bash
python launcher.py
```

`launcher.py` serves this folder and opens a browser. It uses only the standard
library, negotiates a free port from 8992 upward, and sends `no-cache` for HTML,
JS, CSS and JSON so edits always take effect. Useful flags: `--port N`,
`--no-browser`, `--page index.html`, `--selftest`.

It must be served over HTTP — ES modules and the import map will not load from a
`file://` URL.

## What loads

| Input | Handled by |
| --- | --- |
| 3DGS `.ply` (INRIA layout), `.spz`, `.splat`, `.ksplat`, `.sog` | Spark |
| Plain point-cloud `.ply` (no per-point scale/rotation) | `static/ply.js`, pushed into Spark as round splats |

Both binary little-endian and ASCII PLY are read. Big-endian is not supported.

## Layout

```
viewer.html            the app
index.html             v1 single-file viewer, kept for comparison (a copy of
                       reference/v1-single-file.html)
launcher.py            serves the app; standard library only
static/
  app.js               UI wiring
  viewer.js            render layer — wraps Spark, owns camera, picking, framing
  tools.js             scale calibration, measurement, annotations, viewpoints
  scene.js             .dlscene sidecar + ZIP writer for the export bundle
  ply.js               PLY header inspection and point-cloud parsing
  embed.js             read-only shell used by exported bundles
  style.css            DL theme, DARK: the family's tokens with dark values,
                       because splats read best against a dark stage
  fonts/               Source Sans 3 and Quattrocento Sans, SIL OFL, texts alongside
  vendor/              three.js and Spark, both MIT, licences alongside
app/                   the generator's HTTP layer (FastAPI) — present only when
                       the venv is: main.py, jobs.py, blender_export.py
tools/
  capture.py           video / stills -> poses -> splats (the generator)
  dense_mesh.py        the measurable mesh from the same solve
  ground_dem.py        ground filter and terrain model (GeoTIFF)
  depth_splat.py       a scene from one photograph
  privacy_mask.py      face masking before pose solving
  colmap_cameras.py    cameras.json from a COLMAP model
  scene_index.py       output/scenes.json, the generated-scenes list
  inspect_video.py     frame count, fps and rotation of a clip
  inspect_ply.mjs      layout, SH degree and extent of a splat file
  to_spz.mjs           PLY -> SPZ
  make_demo.mjs        generates the bundled test scenes
  acceptance.html      measurement acceptance test (see below)
  smoke_capture.py     the generator's end-to-end gate
  sparse_model_test.py regression test for the sparse-model choice
  pose_regression.py   camera-path drift check (Tier 2)
data/
  demo.ply             synthetic island terrain, 53,980 splats
  calibration.ply      markers at exactly known separations, 50,268 splats
  calibration.markers.json   ground truth for the acceptance test
reference/
  v1-single-file.html  the original hand-written WebGL2 renderer
```

Regenerate the test scenes with `node tools/make_demo.mjs` (deterministic —
the same bytes every run).

## Navigating

Drag to orbit · right-drag or Shift-drag to pan · wheel to zoom · double-click to
centre on a point · `R` reset · `U` flip up-axis.

Zoom works towards **whatever is under the cursor**, not towards a fixed pivot:
each wheel step slides the orbit pivot onto the surface you are pointing at
without moving the camera, so you actually arrive at what you are looking at and
then orbit around it. On a large scan a fixed pivot means you can scroll
indefinitely and never get closer. Picking costs about 4 ms on a 3M-splat scene
and the result is cached across a scroll burst.

Near and far planes are sized from the scene, not the orbit distance — tying
`far` to distance alone made the rest of a large scan vanish as you moved in.

### Level the horizon

Nothing in a reconstruction ties its axes to gravity. The solve picks them, so a
scene routinely sits a few degrees off and the horizon leans — and it leans by a
*different* amount depending on where you have orbited to, which is what makes a
tilted scene feel wrong rather than merely crooked. Measured on the walking clip
below: with levelling off, the scene's true vertical lands anywhere between 4.6°
left and 4.4° right of screen vertical as you swing around it.

The capture cameras know which way was up — COLMAP's camera Y points down, so
each frame carries an up vector, and their average is the best gravity estimate
a solve offers. `cameras.json` version 2 records it, and the viewer orbits about
that vertical instead of world +Y: the scene's vertical is then exactly vertical
on screen at every orbit angle (measured 0.00° at seven angles). The checkbox
sits in **Display**; it is disabled, and says why, for a scene with no capture
cameras.

Levelling is applied to the **orbit frame, never to the mesh**. Spark evaluates
section-box SDFs in the mesh's own frame, and that only works because our single
mesh transform (the 180° flip) is its own inverse — rotating the mesh to level
it would silently break the section box.

### Walk the capture

**Capture views → Walk the capture** flies the camera along the path it actually
travelled, frame by frame, in the order shot. A splat scene is only faithful
near the positions it was photographed from, so this is the honest way to look
at one; drag the view at any time to take control back. Speed is in kept frames
per second, and it defaults to the speed the site was filmed at when
`capture.json` records the source `fps` (kept frames per second = fps ÷ stride;
30 fps at stride 3 gives 10). Older records have no `fps` and fall back to 8.

## Units

A splat scene reconstructed from photographs has **no metric scale** —
structure-from-motion recovers the geometry only up to an unknown factor. So
measurements read as `units` until the scene is given a scale, at which point
they read as `m` / `m²`. Two ways to set it, both in the Scale panel:

- **Calibrate from known distance** — click two points, type the real distance.
  Always correct, needs something of known size in the scene.
- **Scene is already in metres** — one click, for captures that are already
  metric: georeferenced drone sets, ARKit/LiDAR phone scans, or anything
  exported with a known scale. The bundled test scenes are authored 1 unit = 1 m,
  so this is the right button for them.

The scale factor is stored in the `.dlscene` file and travels with exports.

## Scene files

A `.dlscene.json` sits beside a splat file and records what the splat file
cannot: orientation, real-world scale, measurements, annotations, viewpoints and
the section state, plus a SHA-256 of the source so a mismatched pairing is
caught on load.

**Export web scene** writes a ZIP containing that scene, its splat file, a slim
read-only shell and the vendored libraries — about 6.7 MB plus the splat.
Serve the unzipped folder from any static host and embed it:

```html
<iframe src="path/to/index.html" style="width:100%;aspect-ratio:16/9;border:0"
        allowfullscreen loading="lazy"></iframe>
```

### SPZ compression

The splat is transcoded to **SPZ** on export — Niantic's compressed format, and
the only splat format on a formal standards track (Khronos
`KHR_gaussian_splatting`). This is what makes a real capture publishable at all:

| Scene | PLY | SPZ | Saving | Transcode |
| --- | --- | --- | --- | --- |
| demo.ply (54k splats) | 3.0 MB | 0.5 MB | 83% | 0.1 s |
| generated capture (2.4M splats) | 569 MB | **55 MB** | **90%** | 18 s |

Verified lossless where it counts: reloading the SPZ gives an **identical splat
count** and a maximum geometry drift of 0.002 units on a 10-unit scene (0.02%),
which is SPZ's quantisation.

`scene.source.name` is rewritten to the `.spz` so the embed shell loads the file
actually shipped, with the original name, size and SHA-256 kept under
`scene.source.original` for provenance. If the transcode fails or fails to save
space, the original file is shipped instead.

## Making splats from a video, in the app

Start the app with the **venv** interpreter and the sidebar gains a **Make a
scene** panel: drop a video, trim it, pick a quality, watch the stages go by.

### Trimming the clip

A clip is rarely usable end to end — the walk that solves is often a few
seconds in the middle, with a swing at each end that only confuses the solver.
So dropping a video does **not** start a capture: it shows the clip with two
handles, and dragging either one seeks the preview to that instant, so the cut
is chosen by looking at the frame. **Make the scene** starts with that span;
**Use all of it** resets.

Only that span is ever **extracted**, so COLMAP and Brush never see the rest.
`-ss` goes before `-i` — a keyframe seek, instant on a phone clip — and `-t` is
the duration; both sit ahead of the frame-selection filter, so "every Nth
frame" means the same thing whatever is cut. Measured on a 3.57 s clip: 36
frames kept whole, 15 for a 1.0–2.5 s window. The span is recorded in
`capture.json` under `settings.trim`, and `--start` / `--end` (seconds) do the
same from the command line.

The panel warns under 2 s. Its "about N frames" estimate needs the frame rate,
which HTML5 video does not expose, so it measures it from a few played frames —
and where the browser will not present them, it shows no number rather than
guessing one.

While it runs, a progress card sits in the viewport (the viewport is empty
anyway): a ring with a percentage weighted by how long each stage really
takes, the counter the current tool is itself reporting (*57 of 118 frames
placed*, *4,200 of 10,000 training steps*), elapsed time, and an estimated
time left and finish time — labelled as an estimate. The estimate starts from
a model fitted to two measured runs and is re-fitted from every run that
finishes on this machine (`work\timings.json`), and corrected live by how the
current run compares. **Hide** collapses it to a small pill in the corner; the
tab title carries the percentage. When the run ends the viewport blinks three
times, a short chime plays (switchable on the card) and the tab title
alternates until the window is touched. A capture belongs to the server, so
reloading the page brings the card straight back. The sidebar keeps the
compact bar and the full log.

The counters are the tools' own: COLMAP's *Processed file [i/N]*, *Indexing
image [i/N]* and *num_reg_frames=k*, and Brush's *Refine iter N* — Brush only
prints that with `RUST_LOG=brush_cli=info`, which the pipeline sets for it.
`capture.py` re-emits them as `~ progress done/total what` lines, one shape
for the job runner to read; nothing else of the tools' chatter reaches the log.

```bash
..\.venv\Scripts\python launcher.py
```

Dropping a video anywhere in the window works too — videos are routed to the
capture panel rather than rejected as "not a splat file".

The backend (`app/main.py`, `app/jobs.py`) is a thin HTTP layer over
`tools/capture.py`; it does not reimplement the pipeline, so the CLI stays the
thing that actually works and the UI is one more way to drive it. One job runs
at a time — a capture saturates the GPU and most of the CPU, so a second would
make both slower.

**The backend is strictly additive.** Started with plain `python launcher.py`
(no FastAPI), you get the viewer alone and the panel stays hidden — which is
also what an exported scene needs, since those run on any static host with no
server at all. `--viewer-only` forces that mode.

| Quality | Stride | Steps | Max splats | Rough time |
| --- | --- | --- | --- | --- |
| draft | 6 | 3,000 | 600k | ~10 min |
| standard | 3 | 10,000 | 1.5M | ~30 min |
| high | 2 | 20,000 | 2.5M | 1 hour+ |

Endpoints: `GET /api/health`, `POST /api/capture` (multipart video **or** a path
inside `input\`), `GET /api/jobs`, `GET /api/jobs/{id}`,
`POST /api/jobs/{id}/cancel`.

### When a capture is no good

COLMAP will happily register a handful of views and Brush will train on them,
producing a "done" with a black viewport — a 14-second walk along a path once
registered **2 of 118** frames and finished cleanly with a 0.1 MB scene. The
pipeline now **stops** instead: under 20 registered views, or under half the
frames, the run fails at the pose stage with the counts, the likely cause
(motion blur, frames too far apart, untextured scene — forward motion is ruled
out by then, see below) and what to try. The card shows that text. `--allow-poor`
trains anyway, for experiments. The counts are recorded in `capture.json`
under `reconstruction` — against the frames that went in, not the ones that
came out (the old total was read from the undistorted folder, which only ever
holds the registered ones, so that failure reported itself as "2 of 2").

That same clip exposed something deeper: **the mapper is randomised**. COLMAP
picks an initial image pair before it can place anything, and it rejects a pair
whose motion is mostly forward (`init_max_forward_motion`, 0.95) or whose
triangulation angle is under `init_min_tri_angle` (16°). On a walk straight
ahead every pair is both, so the mapper falls back on randomised trials — the
identical database registered 118 frames on one run and 2 on each of the next
two. The pipeline now pins `Mapper.random_seed`, so the same clip gives the
same answer every time, and when too few frames come back it re-runs the mapper
with initialisation thresholds for forward motion, then a last-resort rung,
keeping the best attempt. A retry costs seconds when it fails and a couple of
minutes when it works; it is announced on the card and in the log, and
`capture.json` records which settings produced the scene under
`reconstruction.mapper`.

⚠ **The ladder must survive a mapper that fails outright.** COLMAP *exits 1*
when it finds no initial pair — exactly the failure the relaxed rungs exist to
rescue — and the ladder originally only handled a mapper that succeeded weakly,
so that exit ended the capture after one attempt. Each rung's failure is now
caught and the next rung runs; only if every rung fails does the capture stop,
quoting the last reason. Found on a 3.57 s clip that died after 35 s, and which
then placed all 31 frames — rescued by the *second* rung, the one being
skipped.

With that ladder in place the camera model turned out not to decide whether the
clip reconstructs at all: OPENCV without a prior, OPENCV with one, and
`SIMPLE_RADIAL` with one each placed 118 of 118 frames on three pinned seeds.
The pipeline stays on **OPENCV** — radial and tangential distortion, the better
fit for a phone lens on a tool that measures — with a focal prior from
`--focal-35mm`, since video carries no EXIF and COLMAP's own guess is 1.2× the
long side, about 60% too long for a phone.

## A measurable mesh from the same capture

The splat is the scene as it **looks**. The dense mesh is the same scene as it
**measures** — a point cloud and a surface you can take into Blender, QGIS or
Rhino and draw a plan from. Both come from **one video and one pose solve**:

```
video ─ COLMAP sparse ─┬─ Brush ────────→  3DGS   (appearance)
                       └─ dense MVS ────→  mesh   (geometry)
```

Open a scene made here and the sidebar offers **Measurable mesh**: pick a
quality and a surface type, and it runs COLMAP's own dense pipeline —
`patch_match_stereo` → `stereo_fusion` → a mesher. Nothing is re-captured and
nothing is re-solved.

```bash
..\.venv\Scripts\python tools\dense_mesh.py IMG_1779 --quality standard
```

| Quality | Stereo size | Consistency pass | 118 views |
| --- | --- | --- | --- |
| draft | 480 px | no | ~5 min |
| standard | 800 px | no | ~14 min |
| high | 1200 px | yes | ~60 min |

The stereo pass is the whole cost and it grows with the **square** of the image
size, so that table is the only lever that matters. Output lands in
`output\<name>\mesh\` as `dense.ply`, `mesh.ply` and a `mesh.json` recording the
settings, the counts and the caveats. It is deliberately **not** added to
`scenes.json` — a mesh is not a splat scene — but the dense cloud is a plain
point-cloud PLY, so *Show the cloud* loads it straight into this viewer, which
is the quickest way to judge whether the geometry is worth drawing from.

### Two honest limits

**Scale.** Structure-from-motion fixes geometry only up to a similarity, so the
output is in **COLMAP units, not metres** — the right shape and the wrong size —
until something of known size is measured. Put a scale bar or any object of
known length in the shot; calibrate in the viewer, or use COLMAP's
`model_aligner`. Smartphone videogrammetry lands around **centimetre level**
against laser scanning once scaled.

**Vegetation.** A camera reconstructs only what it can see, and there is no
photographic equivalent of a laser pulse slipping through foliage to return from
the ground. Where planting hides the ground, the ground is **absent** — the
surface you get there is the top of the planting. Measured on a walking clip
through scrub: the gravel path came out as a clean traceable ribbon, the
planting either side as fragments. **Bare ground, quarries, paving, kerbs, walls
and facades are what this is for.**

### Ground, and a terrain model DL-TerrainSlicer opens

Once a cloud exists, the same panel offers **ground and terrain model**. It
separates the ground from what stands on it, grids the ground into a raster and
writes three files beside the mesh:

```
ground.ply   the classified ground points
dem.tif      a float32 GeoTIFF: the grid, its cell size, its origin, NaN nodata
dem.json     what was done, and what it does and does not mean
```

```bash
..\.venv\Scripts\python tools\ground_dem.py IMG_1779 --scale 0.5
```

That closes the chain **video → poses → dense cloud → DTM → contours →
laser-cut sheets** without leaving the DL tools: `dem.tif` is written with
exactly the tags DL-TerrainSlicer reads (ModelPixelScale 33550, ModelTiepoint
33922, GDAL_NODATA 42113 — it ignores the CRS on purpose), and was verified by
loading it with that project's own `load_geotiff`.

**Which way is up** comes from the capture cameras, not a guess: COLMAP's camera
Y points down, so the average of −Y over the frames is the best gravity estimate
a solve offers, and the cloud is rotated by it before anything else. Measured on
the walking clip: 2.3° median spread about the mean.

**The filter** is the cloth simulation filter of Zhang et al. 2016 (credited
under *Licences*), implemented here from the paper. Measured on the same clip,
against height above the local ground:

| Height above ground | Points | Called ground |
| --- | --- | --- |
| 0–0.3 (the surface itself) | 153,385 | **91%** |
| 0.3–1.0 (low growth) | 8,680 | 0.7% |
| 1.0–3.0 (shrub) | 17,272 | **0%** |
| above 3.0 (canopy) | 102 | **0%** |

So it separates cleanly. The 78% overall figure is simply because a walking
capture of a path is mostly ground.

⚠ **It can only classify points that exist.** Where planting hid the ground,
the ground is absent and the cloth settles on the planting instead — the raster
there is the top of the vegetation, and calling it terrain would be a lie. This
is the documented failure mode of CSF even on LiDAR, and photogrammetry has it
worse.

**Scale** carries over from the viewer: the dense cloud shares the splat's
units, so if the scene has been calibrated the panel offers to grid straight
into metres. Uncalibrated, everything is in COLMAP units and `dem.json` says so.

### Why it builds its own workspace

Two traps, both hit while developing this:

1. The splat workspace puts the sparse model in `undistorted\sparse\0\` because
   that is where Brush looks; COLMAP's dense tools want `undistorted\sparse\`
   and fail outright otherwise.
2. `stereo\` is **scaffolding, not scratch** — `image_undistorter` writes
   `patch-match.cfg` and `fusion.cfg` there. Delete it and the dense stage dies
   with *Could not open …\stereo\patch-match.cfg*.

So the stage builds a separate `work\<name>\dense\` (about 5 s for 118 frames),
which also leaves the splat workspace and every Capture Walk package untouched.
It is kept afterwards so a re-mesh is cheap; `--clean` removes it.

## Export for Blender

A scene made here can be handed to **Capture Walk**, the Blender extension that
puts the walk back on the footage. Open a generated scene and the sidebar offers
**Export for Blender**: one `.capturewalk.zip` in `output\packages\` holding the
video, the solved camera path and the sparse model, with no absolute paths, so
it survives being copied to a student's laptop.

The splat is left out by default. A trained `.ply` is hundreds of megabytes, and
a Blender without a Gaussian-splat importer loads it as a point cloud that
renders as nothing; the checkbox includes it for those who have one.

**The format and its only writer live in the BLE project**
(`BLE\project\PACKAGE-FORMAT.md`, `export_for_blender.py`). This tool does not
re-implement the package — it runs that script and shows what it printed, so the
producer and the consumer cannot drift apart. If BLE is not on the machine the
panel stays hidden; `DL3DGS_BLE_EXPORTER` points at the script if it lives
somewhere unusual.

The writer refuses to overwrite an existing package and so does this: a second
export offers **Replace it**, which deletes that scene's package folder and zip
first. Its warnings are surfaced rather than swallowed — `NO SCALE` (the scene
is in COLMAP units until something is measured) and `RAW model` (the undistorted
model is missing).

⚠ A package is a **snapshot of a solve**. Re-run the capture and the package is
stale — re-export it.

## Making splats from a video (command line)

`tools/capture.py` turns a phone video — or a folder of stills — into a `.ply`
this viewer can open.

```bash
python tools/capture.py "..\input\data\IMG_8950.MOV"
```

Stages: **frames** (ffmpeg, honouring the rotation flag so portrait video is not
extracted sideways) → **prune** (drop the blurriest frames; motion blur is the
main cause of a failed reconstruction from handheld video) → **privacy** (see
below) → **poses** (COLMAP
feature extraction, sequential matching with loop detection, mapping, then
undistortion into the standard layout) → **train** (Brush) → **place** the result
in `output\<name>\` with a `capture.json` recording every setting used.

Useful flags: `--stride N` (default 3), `--blur-drop PCT` (default 15),
`--matcher exhaustive`, `--focal-35mm MM` (default 26, the phone's main camera
in video mode; 0 lets COLMAP guess at 1.2× the long side),
`--allow-poor` (train even when too few frames were placed), `--steps N`
(default 15000), `--max-splats N`, `--start S` / `--end S` to use only part of
the clip (seconds), `--with-viewer` to watch Brush train,
`--dry-run` to print the commands, and `--from {frames,prune,poses,train}` to
resume without redoing earlier stages. A run that reaches the pose stage clears
its own `database.db`, `sparse\` and `undistorted\` first — COLMAP reuses
whatever camera an existing database holds, so a re-run would otherwise keep
the old settings silently.

### Privacy masking (GDPR)

Faces are detected and blurred **before pose solving**, by `tools/privacy_mask.py`
(YuNet, via the venv). The ordering is the point: once COLMAP and the trainer
have consumed the frames, faces are baked into the splats and cannot be removed
without redoing the entire run.

```bash
# on its own, e.g. to check a folder before committing to a 30-minute run
..\.venv\Scripts\python tools\privacy_mask.py <frames-dir> --dry-run
```

Detected boxes are grown 60% before masking — a tight crop leaves hairline, jaw
and ears, which still identify — then pixelated *and* blurred, which is
irreversible where a light blur alone can sometimes be undone.

`--privacy off` skips it, for captures with no people in them. `--mask-region
X,Y,W,H` (fractions of the image, repeatable) always blanks a region, for things
the detector cannot see.

**This is not complete anonymisation, and the tool says so in its own report.**
It masks faces. It does *not* detect vehicle plates, house numbers, signage or
windows, and a person can stay identifiable from build, gait, clothing or simply
being the only person at a known place. Under GDPR — and
personopplysningsloven, which applies it in Norway — you remain the controller.
The report names every frame with a detection so those can be reviewed, and
records `"reviewed": false`, which only a human can change. `capture.json`
carries the result so a later publish step can refuse an unreviewed capture.

**Third-party tools live in `DL-SplatGenerator\bin\`** and are found automatically —
nothing is installed system-wide, and the folder is deliberately outside
`project\` so it stays out of backups, exports **and the repository**:

| Tool | Version | Purpose | Licence |
| --- | --- | --- | --- |
| ffmpeg | n7.1 (BtbN build) | frame extraction | GPL-3.0 build ⚠ |
| COLMAP | 4.1.1, CUDA build | camera poses | BSD 3-Clause |
| Brush | v0.3.0 (SHA-256 verified) | splat training | Apache-2.0 |

⚠ `bin\` is excluded from git on purpose. Size is one reason; the other is that
the ffmpeg build used here is GPL-3.0, which cannot sit inside this Apache-2.0
repository as-is — an LGPL build exists and would do the same job. See `NOTICE`
at the repository root, which also flags the two licences not yet verified: the
Depth Anything V2 checkpoints (some are non-commercial) and the YuNet model.

COLMAP 4.x renamed its options: it is `--FeatureExtraction.use_gpu` and
`--FeatureMatching.use_gpu` now, not the old `SiftExtraction`/`SiftMatching`
spellings that most tutorials still show.

A second, heavier trainer (gsplat) can be added later behind the same stage —
Brush was chosen first because it is a single binary with no CUDA toolkit or
Python dependency.

## Testing

`tools/acceptance.html` is the acceptance test. It loads `calibration.ply`,
picks each marker through the real renderer, calibrates on the span that is
exactly 1.000 m by construction, then checks the remaining spans and the
rectangle of known area — and then puts the **section box** through the failure
it once had (see *Known gaps* history below). It is deliberately
layout-independent, so it produces the same result in any window size.

Last run — **28/28** passed. The measurement half:

| Check | True | Measured | Error |
| --- | --- | --- | --- |
| distance A–C | 5 m | 4.9981 m | −1.9 mm |
| distance A–D | 10 m | 9.9987 m | −1.3 mm |
| height H0–H1 | 2 m | 1.9994 m | −0.6 mm |
| area R1–R4 | 18 m² | 17.9767 m² | −0.13% |

Marker picking lands ~20–28 mm off centre, which is the marker ball's own
radius: a ray hits the near surface, not the middle. That offset is systematic
and largely cancels between two markers viewed from the same direction, which is
why the spans come out to a millimetre or two.

The section-box half (13 checks) is written against that box's history of
silently doing nothing:

- the box lands where the fractions say — the SDF's own position and scale
  against `frameBounds()`, to a ten-thousandth of the scene diagonal — and is
  axis-aligned;
- the two flags that decide crop-versus-complement (an *inverted* SDF on a
  *non-inverted* edit) are set wrong on purpose and must come back;
- it erases something **on screen**: pixels that differ from the background,
  cropped to 30 % in x and then switched off — measured 15.7 % left, 100.0 %
  back;
- `normalizeSection` clamps, swaps an inverted range, and still converts the
  legacy plane shape `{axis, t, flip}` from old `.dlscene` files.

⚠ The pixel check **settles** rather than waiting a fixed time. Spark
accumulates on its own schedule, so a single read lands either one state behind
or on a buffer cleared but not yet drawn — which reads as an empty scene, and an
empty scene *passes* a "content disappeared" check. It reads until the same
non-empty count comes back twice.

`embed-test.html` + `scene.json` and `iframe-test.html` exercise the exported
embed path against the dev server without unpacking a bundle.

## A scene from one photograph

**In the app:** *Make a scene → or from one photograph*. Drop a `.jpg`, `.png`
or `.tif`, pick outdoor or indoor, and the scene is ready in seconds — the same
progress card and the same queue as a video capture, just three stages instead
of five. It needs neither COLMAP nor Brush, only the venv, so it is the one
route that works without the multi-gigabyte `bin\` install.

Because the depth is metric, a scene made this way **opens already scaled**:
the viewer reads `metric: true` from its `capture.json` and applies *Scene is
already in metres* for you, so the measurement tools work immediately.

From the command line it is the same tool:

```bash
..\.venv\Scripts\python tools\depth_splat.py <image.jpg>
```

**This is not a capture.** There is no information behind anything, so the
result is 2.5D: right from near the original viewpoint, degrading as you move,
with stretching and holes where surfaces were hidden. Good for site
impressions, historical photographs and design context. Not a survey.

It uses **Depth Anything V2 Metric** (outdoor fine-tune by default,
`--scene indoor` for rooms), which returns **metres** rather than relative
depth — so the scene opens already scaled and works with the measurement tools.
Most depth models return arbitrary scale *and* offset, which makes measuring
meaningless; that is why this one was chosen.

Three things separate a crisp result from soup, and all three are implemented:

1. **Depth discontinuities are cut, not interpolated.** A splat spanning the
   edge between a foreground branch and distant ground would smear across both;
   those are detected by relative depth step and dropped.
2. **Each splat covers its own pixel footprint at its own depth** — smaller
   leaves holes, larger blurs.
3. **Splats are oriented to the surface normal** from the depth gradient.
   Camera-facing splats read as a flat sprite sheet; oriented ones read as
   solid geometry.

Spherical harmonics are degree 0 — one photograph carries no view-dependent
information to fit.

Requires the venv at `DL-SplatGenerator\.venv` (torch, torchvision, transformers).
It runs on CPU; the model downloads once on first use.

### The generator

`tools/smoke_capture.py` is the pipeline's equivalent: it cuts a few seconds off
a source video, runs the whole thing at cheap settings, and checks that every
stage produced what it should.

```bash
python tools/smoke_capture.py
python tools/smoke_capture.py --source <video> --seconds 6 --steps 800
```

It is not a quality test — it answers *is the pipeline still wired together*,
which is what breaks when a dependency renames a flag or a stage writes to the
wrong place. Among its checks is a regression guard for a real bug: rejected
frames were once moved to `images\_rejected`, which COLMAP recursed into, so the
blur pruning silently did nothing and 72 blurry frames went into the model.

Everything it writes goes under `work\_smoke\<timestamp>\`, isolated by
`--work-root` / `--out-root`, so the real folders are untouched. Nothing is
deleted automatically.

### Sparse-model choice

`tools/sparse_model_test.py` guards a quieter bug. COLMAP writes `sparse/0`,
`sparse/1`, … when a solve fragments, numbered in the order the mapper
finished them — not by size. On `IMG_8950_clean`, `sparse/0` held 2 images and
`sparse/1` held 409, and the dense stage took `sparse/0`: it meshed two frames
while the panel quoted 409 views, because it counted the images folder instead
of the model.

```bash
python tools/sparse_model_test.py
```

It builds small synthetic COLMAP models in a temporary folder — no COLMAP, no
GPU, about a second — and checks that `dense_mesh.find_model` picks the model
with the most registered images whatever its number, that `sparse_best` still
wins when `capture.py` has already chosen, and that `/api/mesh` reports the
model's image count rather than the folder's. Nothing is written into the
project.

### Pose regression (Tier 2)

`tools/pose_regression.py` answers a harder question: *has the recovered
geometry drifted?* Structure-from-motion fixes a scene only up to a **similarity
transform** — scale, rotation and translation are all arbitrary — so two
perfectly good runs produce completely different numbers. Comparing them
directly is meaningless. It therefore aligns the two camera paths with the
closed-form Umeyama solution and measures the residual that survives alignment.

```bash
python tools/pose_regression.py record <cameras.json>   # store a reference
python tools/pose_regression.py check  <cameras.json>   # compare against it
```

The smoke test runs this automatically when `tools/reference/smoke_poses.json`
exists.

Thresholds were measured, not guessed:

| Case | RMS (% of extent) | Worst |
| --- | --- | --- |
| Two independent COLMAP runs, same clip | 0.01% | 0.02% |
| Deliberate 3% sine bend of the path | 0.52% | 0.95% |

Limits sit at 0.20% / 0.60% — roughly 20× above the observed noise and clearly
below a real distortion. The method was validated against four cases: identity
and a pure similarity transform (scale ×3.7, 40° rotation) both pass with
**zero** residual, a real independent re-run passes, and the sine bend fails.
Residuals are normalised by the camera-path **extent**, not its length: length
grows with every wiggle and every extra frame, which inflates the denominator
and hides distortion.

### Still to build (Tier 3)

An **absolute ground-truth** test — film something of known size, run the
pipeline, then calibrate on one length in the viewer and measure another. That
is the test that actually matters for survey-grade use, and it needs a physical
capture with a known dimension in shot.

## Known gaps

- **The generator is Windows-only and needs an NVIDIA GPU with CUDA.** Its
  programs are Windows builds in `bin\`, and COLMAP's dense stage requires
  CUDA. The viewer has no such limit.
- **No absolute accuracy test yet** (Tier 3 above): nothing has yet compared a
  real capture against a site of known dimensions.
- **The trim panel's frame-count estimate is unverified** in an ordinary
  browser window — see *Trimming the clip*.
- Level-of-detail is only built above 1,000,000 splats
  (`LOD_THRESHOLD` in `viewer.js`). Below that every splat renders: LOD
  subsamples, which costs picking accuracy on small scenes for no visible gain.
  Note that `SplatMesh`'s own `lod: true` option loads an *empty* mesh for any
  file with no LOD tree of its own — the tree has to be built after loading.
- Large **source** files are slow to open: a 675 MB / 3M-splat Postshot export
  takes ~17 s, and the whole file is read into memory before parsing. Exports
  are not affected — they are transcoded to SPZ (see *SPZ compression*), and the
  generator writes SPZ beside every `.ply` it trains.

### Closed

- **The section tool works.** It was first built as a *plane*, whose position
  and axis never took effect: Spark 2.1.0's plane SDF keeps its inside in the
  plane's local −Z half-space, while the old code rotated local +Y onto the cut
  normal, so the cut stayed perpendicular to the intended one, pinned through
  the scene centre. It is now a **box** (`SplatEditSdfType.BOX`), which has no
  axis convention to trip over and is the more useful tool anyway — slabs, not
  just cuts. `acceptance.html` checks it against exactly that failure. Old
  `.dlscene` files with a plane still load, converted to one face of the box.
- Level-of-detail is unproven. Building it on a 514k-splat capture took 2.3 s and
  changed nothing visible; 3M splats render fine without it. The 1M threshold is
  a guess that has not been shown to earn its keep.

## Real captures tested

| Capture | Splats | Size | SH | Result |
| --- | --- | --- | --- | --- |
| Scaniverse phone scan (`Bol.ply`) | 514,624 | 122 MB | degree 3 | loads in ~2 s, renders correctly |
| Postshot export (Sennalandet) | 3,000,000 | 675 MB | degree 3 | loads in ~17 s, renders correctly |

Both carry full degree-3 spherical harmonics, which the v1 renderer could not
show — this is what the Spark swap bought.

Use `node tools/inspect_ply.mjs <file.ply>` to check an unfamiliar capture: it
reports the header layout, SH degree, splat count and the real extent, which
also tells you whether the file is already metric.

## Licences and credit

**Apache License 2.0**, Digital Landscapes — `LICENSE` at the repository root.
The full account of third-party code and prior work is in `NOTICE` there; in
short:

Bundled third-party code, both MIT, with licence files alongside:
three.js r185 (`static/vendor/three/`) and Spark 2.1.0 by World Labs
(`static/vendor/spark/`). The fonts are SIL OFL, with their texts in
`static/fonts/`.

Called, not bundled: **COLMAP** (poses, and the dense pipeline behind the
measurable mesh), **Brush** (splat training), **ffmpeg**, **Depth Anything V2**
for the single-photograph route and **YuNet** for the privacy mask — each under
its own licence, in `bin\` and the venv, none in the repository.

The representation itself is **3D Gaussian Splatting**: Kerbl, Kopanas,
Leimkühler and Drettakis, *"3D Gaussian Splatting for Real-Time Radiance Field
Rendering"*, ACM Transactions on Graphics 42(4), 2023,
[doi:10.1145/3592433](https://doi.org/10.1145/3592433).

**Methods implemented here from published work, rather than vendored:**

- The ground filter in `tools\ground_dem.py` is the **cloth simulation filter**
  of Zhang, Qi, Wan, Wang, Xie, Wang and Yan, *"An Easy-to-Use Airborne LiDAR
  Data Filtering Method Based on Cloth Simulation"*, Remote Sensing 8(6):501,
  2016, [doi:10.3390/rs8060501](https://doi.org/10.3390/rs8060501) — written
  here from the paper's description, with its documented weakness on low and
  dense vegetation stated wherever the output is offered.
