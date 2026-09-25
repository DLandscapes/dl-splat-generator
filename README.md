# DL-SplatGenerator

**Site video → measurable splat scene.** A viewer for Gaussian-splat scenes of
landscapes and sites that **measures** rather than only shows: distances,
heights and areas at a real-world scale, section boxes, annotations and saved
viewpoints — and still images of any of it, ready to hand in.

Behind it sits an optional **generator** that turns a walked phone video into
the scene itself, and into a measurable mesh and a ground-only terrain model.

Part of the Digital Landscapes tool family, beside
[DL-TerrainSlicer](https://github.com/DLandscapes/dl-terrain-slicer), which cuts
the terrain models this tool writes into laser sheets,
[DL-TerrainMapper](https://github.com/DLandscapes/dl-terrain-mapper) and
[DL-TerrainDiversity](https://github.com/DLandscapes/dl-terrain-diversity).

---

## Getting started

**What you need:** a splat scene — `.ply`, `.spz`, `.splat`, `.ksplat` or `.sog`
— from a phone scanning app, from Postshot, or from this tool's own generator.
A plain point-cloud `.ply` opens too. Sample scenes — one from a walked video,
one from a single photograph — will appear on the Import panel once they are
published.

### In your browser — nothing to install

**https://digital-landscapes.com/splat-generator-app/v3/**

Open it in a current Chrome, Edge, Firefox or Safari and drop a splat file onto
the window. The file is read by your browser on your own machine; it is not
uploaded anywhere. The viewer works the same way here as when run locally; the
generator (making scenes from video) is only available when run locally.

### On your own machine

The viewer needs **Python 3** and nothing else — only its standard library.

- **Windows:** double-click `start.bat`.
- **Any system:** `python project/launcher.py`

Either one serves the app and opens it in your browser, from port 8992 upward.
It has to be opened through that address rather than as a file: browsers will
not load the app's modules from a `file://` path.

## Your files never leave your computer

**Nothing you open is uploaded.** A scene you open is read in your browser, and
a still, a scene file or a Blender export is written on your own machine. The
viewer's libraries and fonts are part of this repository; the only requests it
makes are for its own files — and, when run locally, to its own generator. The generator runs locally too; the
one thing it downloads is the depth model, once, the first time you make a
scene from a single photograph.

## What it does

**Look.** Drag to orbit, right-drag to pan, and scroll to zoom — towards
whatever is under the cursor, so you arrive at what you point at instead of
scrolling past it.

| Key | Action |
| --- | --- |
| `1` `2` `3` `4` | top · front · right · three-quarter view |
| `R` | reset the view |
| `U` | flip the up-axis |
| `[` `]` | previous · next capture position |
| `Enter` · `Esc` | finish · cancel a measurement |

**Scale.** A scene reconstructed from photographs has **no real-world scale**
until one is given, so measurements read as *units* until you set it: click two
points and type the distance between them, or declare a scene that is already
in metres. The scale travels with the scene file and shows in every still.

**Measure.** Distance, height and area, listed as you take them.

**Section.** A box that keeps only what is inside it. Pull one face in for a
plane cut, two for a slab.

**Annotate and keep views.** Pin notes to the scene — drag a pin or its label
to move it along the surface, double-click to change its text — and save
viewpoints to return to.

**Walk the capture.** A scene made by the generator knows the path the camera
took. The viewer can fly that path, stand at any position along it, and level
the horizon from the cameras' own sense of up. A splat scene is only faithful
near where it was filmed from, so this is the honest way to look at one. In the
**Camera** view the frame the camera actually filmed is outlined and the rest
dimmed: a phone films portrait, so in a wide window most of the view from a
capture position was never filmed from there, and that is where a scene guesses.
**Colour by viewing angle** (Display) can be switched off to check whether a
stray tint comes from such a guess. A **plan** under the navigation shows the
scene from above — what the scan covers up to eye level, the camera path, and
where you stand; click near the path to stand there. It is turned so the walk
heads up the page, not north-up: a solve has no compass.

**Source.** A scene made from a photo or video lists what its file says about
itself — when it was taken, where (the phone's GPS), with which device and
lens, and who to credit. It is read on your machine and sent nowhere; a sample
leaves the location out. A scene made from one photograph has a
**Photo** button instead: it stands where
the photo was taken, with the photo's field of view, and outlines the photo's
own rectangle — and a still saved there is exactly that rectangle.

**Save stills.** The view exactly as framed, as a PNG — as on screen, twice as
sharp, or 4K wide — with its measurements and notes drawn in, and without the
section box's outline. **One per viewpoint** saves a still of every saved
viewpoint as one ZIP, which is the quick way to show several states of a scene.
On a laptop whose graphics chip cannot hold the size asked for, the still comes
out as large as the chip allows, and says so.

**Take a photo scene into Blender.** A scene made from one photograph exports
as a single `.glb` file that Blender opens without any add-on (*File → Import →
glTF 2.0*): the photo's camera exactly, the scene as a surface textured with the
photograph, and the photograph as a backdrop. Rendered from that camera in
Blender, it reproduces the photo.

**Save the scene.** A scene file (`.dlscene`) records the scale,
measurements, notes, viewpoints and section beside the splat, so the work can
be picked up again later.

### What a measurement means

A splat scene is **a reconstruction, not a survey**. The measuring tools are
tested against a synthetic scene of exactly known dimensions — spans of 1 to
10 m come out within 2 mm — but that tests the tool, not the capture. A real
scene is only as accurate as its reconstruction, has no scale until you give it
one, and is faithful only near the positions it was filmed from.

## Making scenes — the generator

The generator is **optional** and considerably heavier than the viewer. It
needs **Windows, an NVIDIA graphics card with CUDA**, a Python 3.12 environment,
and three programs that are **not part of this repository** — COLMAP, Brush and
FFmpeg — fetched separately. [`project/README.md`](project/README.md) covers the
setup and every stage in detail. With it running, the app gains:

- **A scene from a video.** Drop a walked phone video, trim it to the part worth
  solving, and it runs frames → camera poses → splat training, with a progress
  card that shows the tools' own counts and an honest time estimate.
- **A scene from one photograph**, in seconds, using a depth model that
  estimates metres. The estimate is offered, never applied for you, and marked
  "est." wherever it shows: on a capture with a measured dimension the same
  model was 5–12× off. It is 2.5D: right from near the original viewpoint,
  stretched and holed away from it. An impression, not a survey — and no mesh or terrain
  model comes from it, because one picture gives no solve to build them from.
- **A measurable mesh** from the same capture: a dense point cloud and surface
  for Blender, QGIS or Rhino.
- **A terrain model** of the ground alone, as a GeoTIFF that
  [DL-TerrainSlicer](https://github.com/DLandscapes/dl-terrain-slicer) opens
  directly — cropped to the walked corridor, or laid out along it.
- **Export for Blender**, for the *Capture Walk* extension, which puts the walk
  back on the footage.

⚠ **Where planting hides the ground, the ground is absent.** A camera
reconstructs only what it sees, so the terrain model there is the top of the
vegetation. Bare ground, paving, kerbs, walls and quarry faces are what it is
for.

⚠ **Footage of a site can show people.** The generator detects and blurs faces
*before* anything else sees the frames, because once they are in a scene they
cannot be taken out. That is not complete anonymisation — it does not detect
number plates, house numbers or windows, and a person can be recognisable
without a face. Under the GDPR you remain responsible for what you publish:
review a scene before it leaves your hands.

⚠ **The programs the generator calls carry their own licences**, which this
repository's licence does not cover. Some of them matter for how you may use or
redistribute a setup — see [`NOTICE`](NOTICE).

## Running the checks

| Check | What it proves | Result |
| --- | --- | --- |
| `project/tools/acceptance.html` | measurements, picking and the section box, through the real renderer | 28/28 |
| `project/tools/smoke_capture.py` | the generator is still wired end to end, including a pose-drift check | 26/26 |
| `project/tools/sparse_model_test.py` | a fragmented solve is read from its largest model | 17/17 |

The acceptance page runs in the browser with the app served; the other two need
the generator's Python environment.

## Licence and credits

**Apache License 2.0** — see [`LICENSE`](LICENSE). Copyright 2026 Digital
Landscapes.

[`NOTICE`](NOTICE) lists the third-party code this repository contains —
[three.js](https://github.com/mrdoob/three.js) and
[Spark](https://github.com/sparkjsdev/spark) (both MIT), and the Source Sans 3
and Quattrocento Sans fonts (SIL OFL) — the programs the generator calls, and
the published work this tool rests on: the 3D Gaussian Splatting paper of Kerbl
et al. (2023), and the cloth simulation filter of Zhang et al. (2016), written
here from the paper for the terrain model.

To cite the tool, see [`CITATION.cff`](CITATION.cff).
