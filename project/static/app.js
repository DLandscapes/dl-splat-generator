/* UI wiring. Everything visual lives in viewer.js / tools.js; this file only
 * connects the sidebar to them and keeps the panels in sync. */
import { Viewer } from "./viewer.js";
import { Tools } from "./tools.js";
import {
  buildScene, applyScene, downloadBlob, sha256, makeZip,
} from "./scene.js";
import { photoSceneGlb, splatPlyForBlender } from "./gltf.js";
import { PlanView } from "./plan.js";

const $ = (id) => document.getElementById(id);
const BUILD = "2026-09-25";

const viewer = new Viewer($("canvas"), $("overlay"));
const tools = new Tools(viewer, $("overlay"));

/** The file currently loaded, re-readable for hashing and export. */
let source = null;   // { file } | { url, name }
let sourceInfo = null;

const freshSection = () =>
  ({ enabled: false, min: { x: 0, y: 0, z: 0 }, max: { x: 1, y: 1, z: 1 } });

const display = {
  detail: null,            // share of splats drawn; null = this computer's default
  blur: 0.3,
  viewColour: true,        // colour by viewing angle (spherical harmonics)
  plan: true,              // the plan view under the navigation column
  section: freshSection(),
};

$("build").textContent = BUILD;

// Handle for the browser-side acceptance tests (and for poking at a scene in
// the console). Not used by the app itself.
window.dl = { viewer, tools, display, BUILD, get source() { return source; },
              // the capture progress card, so a test can show any job state
              renderJob: (job) => renderJob(job) };

/* ------------------------------------------------------------------ status */

let statusTimer = null;
function status(text, ms = 2600) {
  const el = $("status");
  if (!text) { el.hidden = true; return; }
  el.textContent = text;
  el.hidden = false;
  clearTimeout(statusTimer);
  if (ms) statusTimer = setTimeout(() => { el.hidden = true; }, ms);
}
function fail(err) {
  console.error(err);
  status(`⚠ ${err.message || err}`, 6000);
}
tools.onHint = (text) => {
  const el = $("hint");
  el.hidden = !text;
  if (text) el.textContent = text;
};

/* ------------------------------------------------------------------ loading */

/* The scene name under output\<name>\ when the open scene was made HERE, else
 * null. Exporting for Blender needs the solve in work\<name>\, which only a
 * scene from the generated list has. */
let generatedName = null;

/* The sample open now (an entry of samples.json), or null. A sample carries
 * the part of capture.json the viewer needs in its own `record`, since it has
 * no output folder to fetch one from. */
let currentSample = null;

async function loadSource(next, label, fromGenerated = null, sample = null) {
  generatedName = fromGenerated;
  currentSample = sample;
  showSampleCard();
  status(`Loading ${label}…`, 0);
  try {
    const info = next.file
      ? await viewer.loadFile(next.file)
      : await viewer.loadUrl(next.url, next.name);
    source = next;
    sourceInfo = info;
    tools.measurements = [];
    tools.notes = [];
    tools.viewpoints = [];
    tools.calibrated = false;
    tools.estimated = false;
    tools.scaleFactor = 1;
    display.section = freshSection();
    display.detail = null;   // a new scene starts at this computer's default
    onSceneChanged();
    status(`${info.name} — ${info.count.toLocaleString("en-US")} splats`);
  } catch (err) {
    fail(err);
    status(null);
  }
}

function onSceneChanged() {
  const loaded = !!viewer.mesh;
  for (const id of ["panel-display", "panel-scale", "panel-tools",
                    "panel-section", "panel-notes", "panel-views", "panel-scene",
                    "panel-still"]) {
    // panels marked data-incomplete stay hidden until their feature works
    $(id).hidden = !loaded || $(id).hasAttribute("data-incomplete");
  }
  // capture views only exist when the pipeline exported cameras.json beside the scene
  $("panel-capture").hidden = !loaded || !viewer.captureCameras;
  syncCameraButton();
  refreshBlenderPanel();
  refreshMeshPanel();
  syncFromCaptureRecord();
  refreshCaptureState();
  $("dropzone").classList.toggle("loaded", loaded);
  $("drop-hint").hidden = loaded;
  $("scene-card").hidden = !loaded;
  if (loaded && sourceInfo) {
    $("scene-name").textContent = sourceInfo.name;
    $("scene-meta").textContent =
      `${sourceInfo.count.toLocaleString("en-US")} splats · ${sourceInfo.type}`;
  }
  syncDetailUi();
  syncViewColourUi();
  rebuildPlan();
  // section box resets to the whole scene when a new scene arrives
  syncSectionUi();
  refreshLists();
  syncGroups();
}

/* ------------------------------------------------------- the menu window */

/* A group section is only worth a line in the window when something inside it
 * can be used. Each group is asked its own question rather than being told the
 * answer, so adding a fold later needs no bookkeeping here. */
function syncGroups() {
  for (const group of document.querySelectorAll("details.panel > .sec-body")) {
    const subs = [...group.children].filter((el) => el.matches("details.sub"));
    if (!subs.length) continue;                 // a flat section speaks for itself
    // ⚠️ A section with content OF ITS OWN is never hidden with its folds --
    // Import's drop area is the way in. On the website (no generator, no
    // generated scenes, no samples) every Import fold is hidden, and the old
    // rule hid Import itself: the page opened with an empty menu and nothing
    // to drop onto (live, 2026-09-25, found by Marc before class). Locally a
    // generator fold always showed, and until that morning the Test scenes
    // fold did too, so it never surfaced.
    const own = [...group.children].some((el) => !el.matches("details.sub") && !el.hidden);
    group.parentElement.hidden = !own && subs.every((el) => el.hidden);
  }
  badge("b-import", generatedName ? generatedName : (sourceInfo ? sourceInfo.name : ""));
  badge("b-display", viewer.mesh ? `${tools.viewpoints.length || ""}` : "");
  badge("b-measure", tools.calibrated
    ? (tools.estimated ? "≈ 1 unit = 1 m, estimated" : `1 unit = ${tools.scaleFactor.toFixed(2)} m`)
    : (viewer.mesh ? "no scale" : ""));
  badge("b-capture", viewer.captureCameras ? `${viewer.captureCameras.length} frames` : "");
}

function badge(id, text) {
  const el = $(id);
  if (el) el.textContent = text || "";
}

/* Fold the window away, and bring it back with the chip. The chip is placed on
 * the fold button's own rectangle, so the window closes and opens from the same
 * pixel rather than jumping across the viewport. Same behaviour as
 * DL-TerrainDiversity's and DL-TerrainMapper's menus. */
function foldMenu(folded) {
  const panel = $("sidebar"), min = $("menu-min"), chip = $("menu-chip");
  if (folded) {
    const r = min.getBoundingClientRect();
    if (r.width > 0) {
      chip.style.left = `${Math.max(8, r.left)}px`;
      chip.style.top = `${Math.max(8, r.top)}px`;
    }
  }
  panel.classList.toggle("min", folded);
  min.hidden = folded;
  chip.hidden = !folded;
}
$("menu-min").onclick = () => foldMenu(true);
$("menu-chip").onclick = () => foldMenu(false);

/* Drag the window by its header. Clamped so the header can always be grabbed
 * again: a window dragged off the edge is a window you cannot get back. */
(() => {
  const panel = $("sidebar"), head = panel.querySelector("header");
  let from = null;
  head.addEventListener("pointerdown", (e) => {
    if (e.target.closest("#menu-min, #logo-link, #title-link")) return;
    const r = panel.getBoundingClientRect();
    from = { x: e.clientX - r.left, y: e.clientY - r.top };
    panel.classList.add("dragging");
    head.setPointerCapture(e.pointerId);
  });
  head.addEventListener("pointermove", (e) => {
    if (!from) return;
    const w = panel.offsetWidth;
    panel.style.left =
      `${Math.min(Math.max(8 - w + 60, e.clientX - from.x), innerWidth - 60)}px`;
    panel.style.top =
      `${Math.min(Math.max(0, e.clientY - from.y), innerHeight - 40)}px`;
  });
  const drop = () => { from = null; panel.classList.remove("dragging"); };
  head.addEventListener("pointerup", drop);
  head.addEventListener("pointercancel", drop);
})();

/* -------------------------------------------------------------------- lists */

/* The open scene was made from ONE photograph, so its depth model's metres
 * can be offered -- as an estimate. Set from the scene's record. */
let photoScale = false;
const photoScaleOffered = () => photoScale && !!viewer.mesh;
let photoRecord = null;      // that scene's record, for the glTF export

/* Where the photograph of the open photo scene can be fetched: a sample ships
 * it beside its scene; a scene made here names it in capture.json by an
 * absolute path under the project's input folder, which the launcher serves at
 * /input/. Anything else is out of reach, and the export says so. */
function photoUrl() {
  if (currentSample?.photo) return new URL(currentSample.photo, samplesUrl()).href;
  const src = String(photoRecord?.source || "").replace(/\\/g, "/");
  const m = src.match(/\/input\/(.+)$/i);
  return m ? "/input/" + m[1].split("/").map(encodeURIComponent).join("/") : null;
}

/* A photo scene into Blender: the .glb alone, or -- "with the splat" -- a ZIP
 * of the .glb, the splat as a .ply in Blender's frame (splatPlyForBlender), so
 * a Gaussian-splat add-on puts it exactly where the camera and surface are,
 * and a README saying how. */
async function exportPhotoForBlender(withSplat) {
  const btn = $(withSplat ? "gltf-splat-go" : "gltf-go");
  const url = photoUrl();
  if (!url || !viewer.mesh || !photoRecord) return;
  btn.disabled = true;
  $("gltf-state").textContent = withSplat ? "Building the surface and the splat…" : "Building the surface…";
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`the photograph could not be read (HTTP ${res.status})`);
    let blob = await res.blob();
    const bitmap = await createImageBitmap(blob);
    // glTF carries PNG or JPEG only; anything else is re-encoded once -- and so
    // is every JPEG: a phone JPEG is often stored sideways with an EXIF
    // "rotate" flag, which the browser honours when decoding but Blender may
    // not. Redrawn from the decoded bitmap it is upright pixels, flag gone.
    let mime = blob.type;
    if (mime !== "image/png") {
      const c = document.createElement("canvas");
      c.width = bitmap.width; c.height = bitmap.height;
      c.getContext("2d").drawImage(bitmap, 0, 0);
      blob = await new Promise((r) => c.toBlob(r, "image/jpeg", 0.92));
      mime = "image/jpeg";
    }
    const photo = { bytes: await blob.arrayBuffer(), mime,
                    width: bitmap.width, height: bitmap.height };
    const scale = tools.calibrated ? tools.scaleFactor : 1;
    const estimated = !tools.calibrated || tools.estimated;
    const note = estimated
      ? "Scale is the depth model's ESTIMATE (1 unit ~ 1 m), not a measurement."
      : `Calibrated in the viewer: 1 unit = ${scale.toFixed(4)} m.`;
    const fill = $("gltf-fill").checked;
    const { blob: glbBlob, stats } = photoSceneGlb({
      splats: viewer.mesh.packedSplats, record: photoRecord, photo, scale, note, fill,
    });
    const stem = stillStem();
    const how = `In Blender set the output resolution to ${photo.width} × ${photo.height} to `
      + "render the photo's frame, and Color Management → View Transform to Standard "
      + "for the photo's own colours (the default, AgX, reshapes them). "
      + (estimated ? "Its metres are the depth model's estimate." : "In the scene's calibrated metres.");
    const surface = fill
      ? `one surface, ${stats.filledIn.toLocaleString("en-US")} gap cells filled`
      : "gaps kept, with a backdrop";
    let saved;
    if (!withSplat) {
      saved = `${stem}-blender.glb`;
      downloadBlob(glbBlob, saved);
    } else {
      const ply = splatPlyForBlender({ splats: viewer.mesh.packedSplats, scale });
      const readme = [
        `${stem} -- a photo scene for Blender, from DL-SplatGenerator (Digital Landscapes)`,
        "",
        `${stem}.glb        File > Import > glTF 2.0. The photo's camera, exactly, and the`,
        "                   scene as a surface textured with the photograph.",
        `${stem}-splat.ply  The Gaussian splat, for a Gaussian-splat importer (the Capture`,
        "                   Walk add-on imports one; Blender itself does not). It is written",
        "                   in Blender's own axes, already turned the way the glTF import",
        "                   turns the camera, so an importer that keeps a .ply's coordinates",
        "                   as they are puts it exactly where the camera and surface are.",
        "",
        `Render resolution: ${photo.width} x ${photo.height}. Color Management > View Transform:`,
        "Standard, for the photo's own colours.",
        estimated ? "Scale: the depth model's ESTIMATE, 1 unit ~ 1 m -- not a measurement."
                  : `Scale: calibrated in the viewer, 1 unit = ${scale.toFixed(4)} m.`,
        "A scene from one photograph is 2.5D: right from near the camera, stretched away from it.",
        "",
      ].join("\r\n");
      const enc = new TextEncoder();
      saved = `${stem}-blender.zip`;
      downloadBlob(makeZip([
        { name: `${stem}.glb`, data: new Uint8Array(await glbBlob.arrayBuffer()) },
        { name: `${stem}-splat.ply`, data: new Uint8Array(await ply.arrayBuffer()) },
        { name: "README.txt", data: enc.encode(readme) },
      ]), saved);
    }
    $("gltf-state").textContent = `Saved ${saved} — ${stats.triangles.toLocaleString("en-US")} `
      + `triangles (${surface})${withSplat ? `, ${viewer.splatCount.toLocaleString("en-US")} splats` : ""}. ${how}`;
    window.dl.lastGltf = { stats, blob: glbBlob, withSplat };   // for the browser-side checks
  } catch (err) {
    $("gltf-state").textContent = `⚠ ${err.message}`;
  }
  btn.disabled = false;
}
$("gltf-go").onclick = () => exportPhotoForBlender(false);
$("gltf-splat-go").onclick = () => exportPhotoForBlender(true);

function refreshLists() {
  const mList = $("measure-list");
  mList.innerHTML = "";
  for (const m of tools.measurements) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "grow";
    name.textContent = { distance: "Distance", height: "Height", area: "Area" }[m.kind];
    const val = document.createElement("span");
    val.className = "val";
    val.textContent = tools.format(m);
    const del = document.createElement("button");
    del.className = "link";
    del.textContent = "×";
    del.title = "Remove";
    del.onclick = () => tools.remove(m.id);
    li.append(name, val, del);
    mList.appendChild(li);
  }

  const nList = $("note-list");
  nList.innerHTML = "";
  for (const n of tools.notes) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "grow";
    name.textContent = n.title;
    const edit = document.createElement("button");
    edit.className = "link";
    edit.textContent = "edit";
    edit.title = "Change the title and text — or double-click the note in the scene";
    edit.onclick = () => tools.editNote(n.id);
    const del = document.createElement("button");
    del.className = "link";
    del.textContent = "×";
    del.onclick = () => tools.remove(n.id);
    li.append(name, edit, del);
    nList.appendChild(li);
  }

  const vList = $("view-list");
  vList.innerHTML = "";
  for (const v of tools.viewpoints) {
    const li = document.createElement("li");
    const go = document.createElement("button");
    go.className = "link grow";
    go.style.textAlign = "left";
    go.textContent = v.name;
    go.onclick = () => tools.goToViewpoint(v.id);
    const del = document.createElement("button");
    del.className = "link";
    del.textContent = "×";
    del.onclick = () => tools.remove(v.id);
    li.append(go, del);
    vList.appendChild(li);
  }

  $("scale-state").textContent = !tools.calibrated
    ? (photoScaleOffered()
      ? "Not calibrated. The depth model estimated metres from this one picture, "
        + "but on a calibrated capture it was 5–12× off — calibrate from a known "
        + "distance for real measurements, or use the estimate, labelled as one."
      : "Not calibrated. A splat scene has no built-in scale, so measurements "
        + "show as “units” until you set one.")
    : tools.estimated
      ? "1 unit ≈ 1 m — ESTIMATED by the depth model from one picture, not "
        + "measured, and it can be off many times over. Every figure is marked "
        + "“est.”; calibrate from a known distance to replace it."
      : `1 scene unit = ${tools.scaleFactor.toFixed(4)} m — measurements are in metres.`;
  $("assume-metric").disabled = tools.calibrated && tools.scaleFactor === 1 && !tools.estimated;
  $("use-estimate").hidden = !photoScaleOffered();
  $("use-estimate").disabled = tools.calibrated && tools.estimated;
  labelStillSize();                  // "one per viewpoint" follows the list

  for (const [id, kind] of [["tool-dist", "distance"], ["tool-height", "height"],
                            ["tool-area", "area"], ["tool-note", "note"],
                            ["calibrate-btn", "calibrate"]]) {
    $(id).classList.toggle("on", tools.active === kind);
  }
}
// the section badges follow too: the Measure badge used to keep saying
// "no scale" after a scale was set, until the next scene load
tools.onChange = () => { refreshLists(); syncGroups(); syncPlanScale(); };

/* ------------------------------------------------------------------- inputs */

$("dropzone").onclick = () => $("file-input").click();
$("file-input").onchange = (e) => {
  const f = e.target.files[0];
  if (f) loadSource({ file: f }, f.name);
  e.target.value = "";
};
$("clear-btn").onclick = (e) => {
  e.stopPropagation();
  viewer.clear();
  source = null; sourceInfo = null;
  currentSample = null;
  showSampleCard();
  tools.measurements = []; tools.notes = []; tools.viewpoints = [];
  onSceneChanged();
};
/* ------------------------------------------------------- make a scene (API) */

/* The viewer is pure client-side and must stay usable without a backend --
 * an exported scene runs on a plain static host. So this panel only appears
 * when /api/health answers. */
const QUALITY_NOTE = { draft: "~10 min", standard: "~30 min", high: "~1 hour+" };
let hasBackend = false;   // /api/health answered: capture and export are possible
let pollTimer = null;
let activeJob = null;

async function detectBackend() {
  try {
    const res = await fetch("/api/health");
    if (!res.ok) return;
    const info = await res.json();
    if (!info?.backend) return;
    hasBackend = true;
    $("panel-make").hidden = false;
    syncGroups();
    wireMakePanel();
    refreshBlenderPanel();      // a scene may already be open on a reload
    refreshMeshPanel();
    wireJobCard();
    resumeJob();
  } catch {
    // no backend: viewer-only, panel stays hidden
  }
}

/* A capture lives in the server, so it survives a page reload: attach to
 * whatever is running or queued and the progress card comes straight back. */
async function resumeJob() {
  try {
    const data = await (await fetch("/api/jobs")).json();
    const jobs = data.jobs || [];
    const live = jobs.find((j) => j.status === "running") || jobs.find((j) => j.status === "queued");
    if (!live) return;
    activeJob = live.id;
    $("make-progress").hidden = false;
    renderJob(live);
    pollJob();
  } catch {
    // nothing running
  }
}

async function cancelJob() {
  if (!activeJob) return;
  try { await fetch(`/api/jobs/${activeJob}/cancel`, { method: "POST" }); } catch { /* gone */ }
}

function wireMakePanel() {
  const drop = $("make-drop");
  const picker = document.createElement("input");
  picker.type = "file";
  picker.accept = ".mov,.mp4,.m4v,.avi,.mkv";
  picker.hidden = true;
  document.body.appendChild(picker);

  drop.onclick = () => picker.click();
  picker.onchange = () => { if (picker.files[0]) offerTrim(picker.files[0]); picker.value = ""; };
  ["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => {
    ev.preventDefault(); ev.stopPropagation(); drop.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => {
    ev.preventDefault(); ev.stopPropagation(); drop.classList.remove("dragover");
  }));
  drop.addEventListener("drop", (ev) => {
    const f = ev.dataTransfer.files?.[0];
    if (f) offerTrim(f);
  });

  $("make-quality").onchange = (e) => {
    $("make-quality-note").textContent = QUALITY_NOTE[e.target.value] || "";
    if (!$("trim-block").hidden) labelTrim();   // the stride changed
  };
  $("make-cancel").onclick = cancelJob;

  // the photograph route, wired the same way
  const pdrop = $("photo-drop");
  const ppicker = document.createElement("input");
  ppicker.type = "file";
  ppicker.accept = ".jpg,.jpeg,.png,.tif,.tiff";
  ppicker.hidden = true;
  document.body.appendChild(ppicker);

  pdrop.onclick = () => ppicker.click();
  ppicker.onchange = () => { if (ppicker.files[0]) startPhoto(ppicker.files[0]); ppicker.value = ""; };
  ["dragenter", "dragover"].forEach((e) => pdrop.addEventListener(e, (ev) => {
    ev.preventDefault(); ev.stopPropagation(); pdrop.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((e) => pdrop.addEventListener(e, (ev) => {
    ev.preventDefault(); ev.stopPropagation(); pdrop.classList.remove("dragover");
  }));
  pdrop.addEventListener("drop", (ev) => {
    const f = ev.dataTransfer.files?.[0];
    if (f) startPhoto(f);
  });
  $("photo-scene").onchange = (e) => {
    $("photo-scene-note").textContent = e.target.value;
  };
}

/* One photograph -> metric depth -> splats. Same job queue and same progress
 * card as a capture; it simply finishes in seconds. */
async function startPhoto(file) {
  const body = new FormData();
  body.append("file", file);
  body.append("scene", $("photo-scene").value);
  $("make-progress").hidden = false;
  $("make-stage").textContent = `Uploading ${file.name}…`;
  try {
    const res = await fetch("/api/photo", { method: "POST", body });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const data = await res.json();
    activeJob = data.job.id;
    pollJob();
  } catch (err) {
    $("make-stage").textContent = `⚠ ${err.message}`;
  }
}

/* ---- the trim step ------------------------------------------------------
 * A clip is rarely usable end to end. Dropping a video therefore does NOT
 * start a capture any more: it offers the part to use first, because trimming
 * here means those frames are never extracted, so the half hour of COLMAP and
 * Brush is spent only on the stretch that is worth solving.
 *
 * The handles are per-mille of the duration rather than seconds, so the same
 * slider has useful precision on a four-second clip and on a four-minute one.
 * Dragging either handle SEEKS the preview to that instant: the cut is chosen
 * by looking at the frame, not by typing a number. */
let pendingVideo = null;      // the file waiting for its trim to be confirmed
let trimDuration = 0;
let trimFps = 0;              // measured if the browser will tell us, else 0

const trimAt = (el) => (Number(el.value) / 1000) * trimDuration;

function offerTrim(file) {
  pendingVideo = file;
  const v = $("trim-video");
  if (v.src) URL.revokeObjectURL(v.src);
  v.src = URL.createObjectURL(file);
  trimDuration = 0; trimFps = 0;
  $("trim-in").value = 0;
  $("trim-out").value = 1000;
  $("trim-block").hidden = false;
  $("trim-readout").textContent = "reading the clip…";
  $("trim-note").textContent = "";
  v.onloadedmetadata = () => {
    trimDuration = v.duration || 0;
    measureFps(v);
    v.currentTime = 0;
    labelTrim();
  };
}

/* The frame rate is what turns a span in seconds into "how many frames will be
 * solved", and HTML5 video does not expose it. requestVideoFrameCallback does,
 * by timing consecutive presented frames. Where it is missing the count is
 * simply not claimed rather than guessed at 30. */
function measureFps(v) {
  if (!v.requestVideoFrameCallback) return;
  const seen = [];
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    v.pause();
    if (trimDuration) v.currentTime = trimAt($("trim-in"));
    const gaps = seen.slice(1).map((t, i) => t - seen[i]).filter((g) => g > 0);
    if (!gaps.length) return;     // never claim a number we did not measure
    gaps.sort((a, b) => a - b);
    const median = gaps[Math.floor(gaps.length / 2)];
    if (median > 0) { trimFps = 1 / median; labelTrim(); }
  };
  const tick = (_, meta) => {
    seen.push(meta.mediaTime);
    // Three gaps is enough for a median, and waiting for more is what broke
    // this: a throttled or backgrounded tab presents a couple of frames a
    // second, so a threshold of eight never resolved and the frame count was
    // silently never shown.
    if (seen.length >= 4) { finish(); return; }
    v.requestVideoFrameCallback(tick);
  };
  v.requestVideoFrameCallback(tick);
  v.play().catch(() => {});       // a few frames only, muted; paused in finish
  setTimeout(finish, 1500);       // whatever we have by then, or nothing
}

function labelTrim() {
  const a = trimAt($("trim-in")), b = trimAt($("trim-out"));
  const whole = Number($("trim-in").value) === 0 && Number($("trim-out").value) === 1000;
  $("trim-readout").textContent = trimDuration
    ? `${a.toFixed(1)}–${b.toFixed(1)} s of ${trimDuration.toFixed(1)} s`
    : "—";
  const stride = { draft: 6, standard: 3, high: 2 }[$("make-quality").value] || 3;
  const bits = [whole ? "The whole clip." : `Keeping ${(b - a).toFixed(1)} s.`];
  if (trimFps > 0) {
    bits.push(`About ${Math.max(1, Math.round((b - a) * trimFps / stride))} `
      + `frames at ${Math.round(trimFps)} fps, every ${stride}${stride === 1 ? "st" : "th"}.`);
  }
  if ((b - a) < 2) bits.push("⚠ Under 2 s rarely gives the solver enough baseline.");
  $("trim-note").textContent = bits.join(" ");
}

for (const id of ["trim-in", "trim-out"]) {
  $(id).oninput = () => {
    const lo = $("trim-in"), hi = $("trim-out");
    // never let the handles cross, and keep a sliver between them
    if (Number(lo.value) > Number(hi.value) - 10) {
      if (id === "trim-in") lo.value = Number(hi.value) - 10;
      else hi.value = Number(lo.value) + 10;
    }
    const v = $("trim-video");
    if (trimDuration) { v.pause(); v.currentTime = trimAt($(id)); }
    labelTrim();
  };
}

$("trim-all").onclick = () => {
  $("trim-in").value = 0; $("trim-out").value = 1000;
  const v = $("trim-video");
  if (trimDuration) v.currentTime = 0;
  labelTrim();
};

$("trim-go").onclick = () => {
  if (!pendingVideo) return;
  const a = trimAt($("trim-in")), b = trimAt($("trim-out"));
  const whole = Number($("trim-in").value) === 0 && Number($("trim-out").value) === 1000;
  $("trim-block").hidden = true;
  startCapture(pendingVideo, whole ? 0 : a, whole ? 0 : b);
  pendingVideo = null;
};

async function startCapture(file, start = 0, end = 0) {
  const body = new FormData();
  body.append("file", file);
  body.append("quality", $("make-quality").value);
  body.append("privacy", $("make-privacy").checked ? "auto" : "off");
  body.append("start", String(start));
  body.append("end", String(end));

  $("make-progress").hidden = false;
  $("make-stage").textContent = `Uploading ${file.name}…`;
  $("make-log").textContent = "";
  $("make-cancel").hidden = true;
  announced = null;
  cardHidden = false;
  renderJob({ status: "uploading", name: file.name, pct: 0, elapsed: 0,
              stageLabel: "Uploading", stageCount: 5, stageIndex: 0, lines: [] });
  try {
    const res = await fetch("/api/capture", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "could not start");
    activeJob = data.job.id;
    renderJob(data.job);
    pollJob();
  } catch (err) {
    $("make-stage").textContent = `⚠ ${err.message}`;
    status(`⚠ ${err.message}`, 6000);
    closeJobCard();
  }
}

function pollJob() {
  clearTimeout(pollTimer);
  if (!activeJob) return;
  pollTimer = setTimeout(async () => {
    try {
      const job = await (await fetch(`/api/jobs/${activeJob}`)).json();
      renderJob(job);
      if (["done", "failed", "cancelled"].includes(job.status)) {
        activeJob = null;
        if (job.status === "done") {
          status(`Scene "${job.name}" is ready.`, 8000);
          listGeneratedScenes();
        } else if (job.status === "failed") {
          status(`⚠ Capture stopped: ${job.error || "see the log"}`, 10000);
        }
        return;
      }
      pollJob();
    } catch {
      pollJob();          // transient error while the server is busy
    }
  }, 1000);
}

/* ------------------------------------------------ the progress card */

/* A capture takes half an hour and the viewport is black meanwhile. The card
 * shows what the tools themselves are counting (frames placed, training
 * steps), a percentage weighted by how long each stage really takes, and an
 * estimated finish -- then flashes and chimes so nobody has to keep looking. */
const RING = 2 * Math.PI * 52;        // circumference of the big ring
const RING_SMALL = 2 * Math.PI * 16;  // and of the pill's
let cardHidden = false;               // user pressed Hide -> pill only
let announced = null;                 // job id whose finish was signalled
let lastJob = null;
let titleTimer = null;

function wireJobCard() {
  $("job-hide").onclick = () => { cardHidden = true; syncCardVisibility(true); };
  $("job-pill").onclick = () => { cardHidden = false; syncCardVisibility(true); };
  $("job-close").onclick = closeJobCard;
  $("job-cancel").onclick = cancelJob;
  $("job-open").onclick = () => {
    if (!lastJob) { closeJobCard(); return; }
    // a mesh job produces a dense cloud, not a splat scene in the list
    if (lastJob.kind === "mesh") {
      loadSource({ url: `/output/${encodeURIComponent(lastJob.name)}/mesh/dense.ply`,
                   name: `${lastJob.name}_dense.ply` },
                 `${lastJob.name} dense cloud`);
    } else {
      openGenerated(lastJob.name);
    }
    closeJobCard();
  };
  const sound = $("job-sound");
  try { sound.checked = localStorage.getItem("dl3dgs.jobSound") !== "off"; } catch { /* private mode */ }
  sound.onchange = () => {
    try { localStorage.setItem("dl3dgs.jobSound", sound.checked ? "on" : "off"); } catch { /* ignore */ }
  };
  // the blinking tab title stops as soon as the user is back
  window.addEventListener("focus", stopTitleBlink);
  document.addEventListener("pointerdown", stopTitleBlink, { capture: true });
}

const fmtClock = (s) => {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(sec).padStart(2, "0")}`;
};
const fmtSpan = (s) => {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return `${s} s`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  if (h) return `${h} h ${m} min`;
  return `${m} min`;
};
const fmtTime = (epoch) =>
  new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function setRing(pct) {
  const f = Math.max(0, Math.min(100, pct)) / 100;
  $("job-card").querySelector(".arc").style.strokeDashoffset = RING * (1 - f);
  $("job-pill").querySelector(".arc").style.strokeDashoffset = RING_SMALL * (1 - f);
}

function renderJob(job) {
  lastJob = job;
  const active = ["running", "queued", "uploading"].includes(job.status);
  const pct = job.status === "done" ? 100 : (job.pct ?? 0);
  const phase = job.phaseLabel ? ` · ${job.phaseLabel}` : "";

  // sidebar: bar, one-line state, the log
  $("make-bar").firstElementChild.style.width = `${pct}%`;
  $("make-stage").textContent = active
    ? `${Math.round(pct)}% — ${job.stageLabel || job.status}${phase} (${fmtClock(job.elapsed)})`
    : `${job.status} — ${job.stage || ""} (${fmtClock(job.elapsed)})`;
  $("make-log").textContent = (job.lines || []).join("\n");
  $("make-log").scrollTop = $("make-log").scrollHeight;
  $("make-cancel").hidden = !active || job.status === "uploading";

  // the card
  const card = $("job-card");
  card.classList.toggle("running", job.status === "running");
  card.classList.toggle("done", job.status === "done");
  card.classList.toggle("failed", job.status === "failed");
  setRing(pct);
  $("job-pct").textContent = Math.round(pct);
  $("job-pill-text").textContent = `${Math.round(pct)}%`;
  const c = job.counter;
  const counter = c ? `${c.done.toLocaleString("en-US")} of ${c.total.toLocaleString("en-US")} ${c.what}` : "";
  const step = job.stageIndex ? `Step ${Math.min(job.stageIndex, job.stageCount)} of ${job.stageCount} — ` : "";
  let title = "", stage = "", count = "", times = "";
  // Anything the run recovered from on its own -- a mapper retry, a lowered
  // Brush setting. Shown while it is still running, not only at the end: a
  // silent two-minute retry is exactly when a wait starts to feel like a hang.
  const warn = (job.warnings?.length ? job.warnings[job.warnings.length - 1] : "")
    .replace(/^WARNING:\s*/, "");
  if (job.status === "uploading") {
    title = `Uploading “${job.name}”`;
    stage = "Sending the video to the capture backend…";
  } else if (job.status === "queued") {
    title = `“${job.name}” is waiting its turn`;
    stage = "Another capture is still running; this one starts when it finishes.";
  } else if (job.status === "running") {
    title = job.kind === "mesh"
      ? `Building a measurable mesh from “${job.name}”`
      : `Making a scene from “${job.name}”`;
    stage = `${step}${job.stageLabel}${phase}`;
    count = counter || (job.frames ? `${job.frames} frames in play` : "");
    times = `Elapsed ${fmtClock(job.elapsed)}`
      + (job.etaSeconds != null
        ? ` · about ${fmtSpan(job.etaSeconds)} left · ready around ${fmtTime(job.finishAt)} (estimate)`
        : " · estimating the time left…");
  } else if (job.status === "done") {
    title = job.kind === "mesh"
      ? `Mesh of “${job.name}” is ready`
      : `Scene “${job.name}” is ready`;
    stage = `Finished in ${fmtSpan(job.elapsed)}`
      + (job.registered && job.frames ? ` · ${job.registered} of ${job.frames} frames placed` : "");
  } else if (job.status === "failed") {
    title = job.kind === "mesh"
      ? `Mesh of “${job.name}” stopped`
      : `Capture of “${job.name}” stopped`;
    stage = job.error || "See the log in the sidebar.";
    count = job.hint || "";
    times = `After ${fmtSpan(job.elapsed)} · the full log is in the sidebar`;
  } else {
    title = `Capture of “${job.name}” cancelled`;
  }
  $("job-title").textContent = title;
  $("job-stage").textContent = stage;
  $("job-counter").textContent = count;
  $("job-warn").textContent = job.status === "failed" ? "" : warn;
  $("job-times").textContent = times;
  // the "keep this computer awake" note earns its place on a half-hour
  // capture; on a photo scene that finishes in seconds it would be silly
  $("job-note").textContent = job.kind === "photo"
    ? "One photograph, estimated in metres — a 2.5D impression, right from near "
      + "the original viewpoint and stretched away from it."
    : "Keep this computer awake and the app running. You can keep using the "
      + "viewer meanwhile; the time left is an estimate that improves as the "
      + "run goes on.";
  $("job-note").hidden = !active;
  $("job-hide").hidden = !active;
  $("job-cancel").hidden = job.status !== "running" && job.status !== "queued";
  $("job-open").hidden = job.status !== "done";
  $("job-open").textContent = job.kind === "mesh" ? "Show the cloud" : "Open scene";
  $("job-close").hidden = active;
  syncCardVisibility(active);

  if (active) {
    document.title = job.status === "running"
      ? `${Math.round(pct)}% · ${(job.stageLabel || "").split(" (")[0]} · DL-SplatGenerator`
      : "DL-SplatGenerator";
  } else if (announced !== job.id) {
    announced = job.id;
    announce(job);
  }
}

function syncCardVisibility(active) {
  const collapsed = active && cardHidden;
  $("job-card").hidden = collapsed;
  $("job-pill").hidden = !collapsed;
}

function closeJobCard() {
  $("job-card").hidden = true;
  $("job-pill").hidden = true;
  cardHidden = false;
  stopTitleBlink();
}

/* Three things say "it is finished" without the user watching: the viewport
 * blinks, a short chime plays (if allowed), and the tab title alternates until
 * the window is touched again. */
function announce(job) {
  if (job.status === "cancelled") { document.title = "DL-SplatGenerator"; return; }
  const ok = job.status === "done";
  const flash = $("job-flash");
  flash.classList.toggle("failed", !ok);
  flash.classList.remove("flash");
  void flash.offsetWidth;                // restart the animation
  flash.classList.add("flash");
  // a background tab throttles animations; never leave the tint behind
  const clear = () => flash.classList.remove("flash");
  flash.addEventListener("animationend", clear, { once: true });
  setTimeout(clear, 2500);
  if ($("job-sound").checked) chime(ok);
  startTitleBlink(ok ? "✓ Scene ready" : "✗ Capture stopped");
}

function chime(ok) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    if (ctx.state === "suspended") ctx.resume();
    const notes = ok ? [[523.25, 0], [659.25, 0.18], [783.99, 0.36]] : [[329.63, 0], [261.63, 0.3]];
    for (const [hz, at] of notes) {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = hz;
      const t = ctx.currentTime + at;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.22, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.5);
      o.connect(g).connect(ctx.destination);
      o.start(t); o.stop(t + 0.55);
    }
    setTimeout(() => ctx.close(), 1800);
  } catch {
    // no audio device, or autoplay blocked -- the flash and title still show
  }
}

function startTitleBlink(text) {
  stopTitleBlink();
  let on = false;
  document.title = text;
  titleTimer = setInterval(() => { on = !on; document.title = on ? "DL-SplatGenerator" : text; }, 1000);
}
function stopTitleBlink() {
  if (!titleTimer) return;
  clearInterval(titleTimer);
  titleTimer = null;
  document.title = "DL-SplatGenerator";
}

/* ------------------------------------------------------ measurable mesh (API) */

/* The second product of the same solve. Hidden unless a scene made here is
 * open AND its COLMAP model is still in work\, because that is what the dense
 * stage reads. */
async function refreshMeshPanel() {
  const panel = $("panel-mesh");
  if (!generatedName || !hasBackend) { panel.hidden = true; syncGroups(); return; }
  let info;
  try {
    info = await (await fetch(`/api/mesh/${encodeURIComponent(generatedName)}`)).json();
  } catch { panel.hidden = true; syncGroups(); return; }
  panel.hidden = false;
  meshInfo = info;
  $("mesh-photo").hidden = !info.singlePhoto;
  $("mesh-controls").hidden = !!info.singlePhoto;
  $("mesh-limits").hidden = !!info.singlePhoto;
  $("mesh-intro").hidden = !!info.singlePhoto;
  if (info.singlePhoto) { $("dem-block").hidden = true; syncGroups(); return; }

  $("mesh-go").disabled = !info.ready;
  $("mesh-show").hidden = !info.cloudUrl;
  $("mesh-go").textContent = info.built ? "Build it again" : "Build the mesh";
  labelMeshQuality();

  const bits = [];
  if (!info.ready) bits.push(`Cannot build: ${info.blocking.join("; ")}.`);
  if (info.built) {
    const b = info.built;
    bits.push(`Built ${b.created?.replace("T", " ").slice(0, 16)} — `
      + `${(b.cloud?.points || 0).toLocaleString("en-US")} points, `
      + `${(b.mesh?.faces || 0).toLocaleString("en-US")} faces, `
      + `${b.settings?.quality} quality, ${Math.round((b.seconds || 0) / 60)} min.`);
    if (b.extent_units) {
      bits.push(`Extent ${b.extent_units.join(" × ")} COLMAP units — not metres.`);
    }
  }
  $("mesh-state").textContent = bits.join(" ");
  refreshDemPanel();
}

function labelMeshQuality() {
  const q = $("mesh-quality").value;
  const mins = meshInfo?.estimateMinutes?.[q];
  const views = meshInfo?.views;
  $("mesh-quality-note").textContent = mins
    ? `about ${mins} min for ${views} views` : "";
}

let meshInfo = null;

$("mesh-quality").onchange = labelMeshQuality;

$("mesh-go").onclick = async () => {
  const name = generatedName;
  if (!name) return;
  const url = `/api/mesh/${encodeURIComponent(name)}`
    + `?quality=${$("mesh-quality").value}&mesher=${$("mesh-mesher").value}`;
  $("mesh-go").disabled = true;
  $("mesh-state").textContent = "Starting…";
  try {
    const res = await fetch(url, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    activeJob = data.job.id;
    $("make-progress").hidden = false;
    pollJob();
  } catch (err) {
    $("mesh-state").textContent = `⚠ ${err.message}`;
    $("mesh-go").disabled = false;
  }
};

/* ---- ground and terrain model -------------------------------------------
 * Fast enough (about a second of numpy) that it runs synchronously rather than
 * as a job. The scale checkbox is the useful bit: the dense cloud shares the
 * splat's units, so a scene calibrated in the viewer can be gridded straight
 * into metres. */
let demInfo = null;
let demScaleSetFor = null;   // the scene whose calibration box was set by hand

async function refreshDemPanel() {
  const block = $("dem-block");
  if (!generatedName || !meshInfo?.built) { block.hidden = true; return; }
  try {
    demInfo = await (await fetch(`/api/dem/${encodeURIComponent(generatedName)}`)).json();
  } catch { block.hidden = true; return; }
  block.hidden = !demInfo.ready;
  const cropping = $("dem-corridor-use").checked;
  const flat = cropping && $("dem-unroll").checked;
  const url = flat ? demInfo.unrolledGroundUrl
                   : (cropping ? demInfo.corridorGroundUrl : demInfo.groundUrl);
  $("dem-show").hidden = !url;
  $("dem-show").textContent = cropping ? "Show the corridor" : "Show the ground";
  $("dem-go").textContent =
    (flat ? demInfo.unrolled : (cropping ? demInfo.corridor : demInfo.built))
      ? "Build it again" : "Build the terrain model";

  // A calibrated scene defaults to using its calibration: the alternative is a
  // careless click writing a COLMAP-units raster from a scene that knows its
  // own scale. Once this scene's box has been set by hand, that choice stands.
  const box = $("dem-scale-use");
  box.disabled = !tools.calibrated;
  if (!tools.calibrated) box.checked = false;
  else if (demScaleSetFor !== generatedName) box.checked = true;
  $("dem-scale-label").textContent = tools.calibrated
    ? `Use this scene's calibration (1 unit = ${tools.scaleFactor.toFixed(4)} m)`
    : "No calibration on this scene — the model will be in COLMAP units";

  // The corridor needs the walk line, which lives with the mesh poses.
  const crop = $("dem-corridor-use");
  crop.disabled = !demInfo.hasPath;
  if (!demInfo.hasPath) crop.checked = false;
  $("dem-corridor-label").textContent = demInfo.hasPath
    ? "Crop to a corridor around the walk"
    : "No camera path on this scene — a corridor cannot be cut";
  $("dem-corridor-field").hidden = !crop.checked;
  $("dem-stretch-field").hidden = !(crop.checked && demInfo.hasPath);
  // unrolling lays a corridor out flat, so it only exists when there is one
  $("dem-unroll-row").hidden = !crop.checked;
  $("dem-unroll-note").hidden = !(crop.checked && $("dem-unroll").checked);
  if (!crop.checked) $("dem-unroll").checked = false;
  syncStretchRange();
  syncCorridorLabel();

  const shown = crop.checked
    ? ($("dem-unroll").checked ? demInfo.unrolled : demInfo.corridor)
    : demInfo.built;
  const bits = [];
  if (shown) {
    const b = shown, u = b.metric ? "m" : "units";
    // the share is of what the filter saw, which is the corridor when cropped
    const seen = b.points_considered ?? b.points;
    bits.push(`${b.ground_points?.toLocaleString("en-US")} of `
      + `${seen?.toLocaleString("en-US")} points${b.corridor ? " in the corridor" : ""}`
      + ` are ground (${b.ground_share_percent}%).`);
    bits.push(`Grid ${b.grid?.width} × ${b.grid?.height} at ${b.grid?.cell_size} `
      + `${u}, ${b.grid?.empty_cells?.toLocaleString("en-US")} cells nodata.`);
    if (b.corridor) {
      // a corridor asks 8 points of a cell, not 2 — say what that cost
      bits.push(`${b.grid?.thin_cells_dropped?.toLocaleString("en-US")} cells `
        + `dropped as too thin (under ${b.grid?.min_points_per_cell} points).`);
      const c = b.corridor;
      const along = c.whole_walk === false
        ? `frames ${c.frames_from}–${c.frames_to} of ${c.frames_total}`
        : "the whole walk";
      if (b.unrolled) bits.push("Unrolled: a developed strip, not a plan.");
      bits.push(`Corridor ±${c.half_width} ${u} along ${along} `
        + `(${c.walk_length.toFixed(2)} ${u}); the ground falls `
        + `${c.fall_along_path} ${u} (${c.grade_percent}%).`);
    }
    if (!b.metric) bits.push("COLMAP units — calibrate for real contours.");
  }
  $("dem-state").textContent = bits.join(" ");
  syncGroups();
}

/* The stretch: which part of the walk the corridor is taken along. The two
 * handles are frame numbers, the same ones the corner scrubber shows, and
 * dragging either one takes the view there — the selection is a place, not a
 * pair of numbers. */
function syncStretchRange() {
  const cams = viewer.captureCameras;
  const from = $("dem-stretch-from"), to = $("dem-stretch-to");
  if (!cams) return;
  if (Number(from.max) !== cams.length) {          // a scene just opened
    from.min = to.min = 1;
    from.max = to.max = cams.length;
    from.value = 1;
    to.value = cams.length;
  }
  labelStretch();
}

function labelStretch() {
  const cams = viewer.captureCameras;
  const a = Number($("dem-stretch-from").value), b = Number($("dem-stretch-to").value);
  $("dem-stretch-val").textContent = (!cams || (a === 1 && b === cams.length))
    ? `whole walk (${cams ? cams.length : 0} frames)`
    : `frames ${a}–${b} of ${cams.length}`;
}

function stretchRange() {
  const cams = viewer.captureCameras;
  if (!cams) return null;
  const a = Number($("dem-stretch-from").value), b = Number($("dem-stretch-to").value);
  return (a === 1 && b === cams.length) ? null : `${Math.min(a, b)}:${Math.max(a, b)}`;
}

for (const id of ["dem-stretch-from", "dem-stretch-to"]) {
  $(id).oninput = () => {
    const from = $("dem-stretch-from"), to = $("dem-stretch-to");
    // keep the handles from crossing: two frames is the least a line can have
    if (Number(from.value) > Number(to.value) - 1) {
      if (id === "dem-stretch-from") from.value = Number(to.value) - 1;
      else to.value = Number(from.value) + 1;
    }
    labelStretch();
    viewer.stopWalk();
    viewer.goToCaptureCamera(Number($(id).value) - 1, false);   // 1-based label
    refreshCaptureState();
    syncFrameSlider();
  };
}

function syncCorridorLabel() {
  const v = Number($("dem-corridor").value).toFixed(1);
  $("dem-corridor-val").textContent = tools.calibrated ? `${v} m` : `${v} units`;
}

$("dem-scale-use").onchange = () => { demScaleSetFor = generatedName; };

$("dem-unroll").onchange = () => {
  $("dem-unroll-note").hidden = !$("dem-unroll").checked;
  refreshDemPanel();
};

$("dem-corridor").oninput = syncCorridorLabel;
$("dem-corridor-use").onchange = () => {
  $("dem-corridor-field").hidden = !$("dem-corridor-use").checked;
  refreshDemPanel();
};

$("dem-go").onclick = async () => {
  const name = generatedName;
  if (!name) return;
  // 0 means "not calibrated"; a real factor of exactly 1.0 must still count
  const scale = ($("dem-scale-use").checked && tools.calibrated)
    ? tools.scaleFactor : 0;
  const corridor = ($("dem-corridor-use").checked && demInfo?.hasPath)
    ? Number($("dem-corridor").value) : 0;
  $("dem-go").disabled = true;
  $("dem-state").textContent = corridor
    ? "Cropping to the walk, then classifying ground…" : "Classifying ground…";
  try {
    const res = await fetch(
      `/api/dem/${encodeURIComponent(name)}?scale=${scale}&corridor=${corridor}`
      + (corridor && stretchRange() ? `&frames=${stretchRange()}` : "")
      + (corridor && $("dem-unroll").checked ? "&unroll=true" : ""),
      { method: "POST" });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || res.statusText);
    await refreshDemPanel();
    status(`Terrain model written for “${name}”.`, 7000);
  } catch (err) {
    $("dem-state").textContent = `⚠ ${err.message}`;
  }
  $("dem-go").disabled = false;
};

$("dem-show").onclick = () => {
  const cropping = $("dem-corridor-use").checked;
  const flat = cropping && $("dem-unroll").checked;
  // the .ply stays in the world's frame even for an unrolled raster, so this
  // shows you where the strip actually is rather than where it was flattened to
  const url = flat ? demInfo?.unrolledGroundUrl
                   : (cropping ? demInfo?.corridorGroundUrl : demInfo?.groundUrl);
  if (!url) return;
  const tag = flat ? "ground_unrolled" : (cropping ? "ground_corridor" : "ground");
  loadSource({ url, name: `${generatedName}_${tag}.ply` },
             `${generatedName} ${cropping ? "corridor ground" : "ground points"}`);
};

/* The dense cloud is a plain point-cloud PLY, which this viewer reads. Showing
 * it is the quickest way to judge whether the geometry is worth drawing from —
 * a hard surface comes out as a continuous sheet, planting as fragments. */
$("mesh-show").onclick = () => {
  if (!meshInfo?.cloudUrl) return;
  loadSource({ url: meshInfo.cloudUrl, name: `${generatedName}_dense.ply` },
             `${generatedName} dense cloud`);
};

/* ------------------------------------------------- export for Blender (API) */

/* The package format and its only writer live in the BLE project (the Capture
 * Walk add-on); this panel just asks the backend to run it and shows what it
 * said. Hidden unless a scene made here is open AND that writer is reachable,
 * so a viewer-only install never advertises something it cannot do. */
let blenderBusy = null;

async function refreshBlenderPanel() {
  const panel = $("panel-blender");
  if (!generatedName || !hasBackend) { panel.hidden = true; syncGroups(); return; }
  let info;
  try {
    info = await (await fetch(`/api/export/blender/${encodeURIComponent(generatedName)}`)).json();
  } catch { panel.hidden = true; syncGroups(); return; }
  if (info.exporter === null && !info.ready) { panel.hidden = true; syncGroups(); return; }

  panel.hidden = false;
  panel.dataset.scene = generatedName;
  // a single-photo scene has no camera path to pack: its way into Blender is
  // the glTF section above, so this one steps aside
  if (info.singlePhoto) { panel.hidden = true; syncGroups(); return; }
  $("blender-go").disabled = !info.ready;
  $("blender-splat").disabled = !info.splat;
  $("blender-splat-note").textContent = info.splat
    ? `The trained .ply is ${info.splatMB} MB. Leave it out unless the Blender `
      + "opening this has a Gaussian-splat importer — without one a splat loads "
      + "as a point cloud and renders as nothing."
    : "No .ply beside this scene to include.";

  const parts = [];
  if (!info.ready) parts.push(`Cannot export: ${info.blocking.join("; ")}.`);
  for (const w of info.warnings || []) parts.push(`⚠ ${w}.`);
  if (info.ready && info.exists)
    parts.push("A package for this scene already exists in output\\packages\\.");
  $("blender-state").textContent = parts.join(" ");
  $("blender-replace").hidden = !(info.ready && info.exists);
  $("blender-go").textContent = info.exists ? "Export again" : "Export for Blender";
  $("blender-log").hidden = true;
}

async function runBlenderExport(replace) {
  const name = generatedName;
  if (!name || blenderBusy) return;
  const withSplat = $("blender-splat").checked;
  const began = Date.now();
  blenderBusy = setInterval(() => {
    const s = Math.round((Date.now() - began) / 1000);
    $("blender-state").textContent =
      `Packing… ${s}s. Copying the video and the model, then zipping`
      + (withSplat ? " — with the splat this takes a minute or two." : ".");
  }, 250);
  $("blender-go").disabled = true;
  $("blender-replace").hidden = true;
  $("blender-log").hidden = true;

  try {
    const url = `/api/export/blender/${encodeURIComponent(name)}`
      + `?with_splat=${withSplat}&replace=${!!replace}`;
    const res = await fetch(url, { method: "POST" });
    const data = await res.json();
    clearInterval(blenderBusy); blenderBusy = null;
    $("blender-go").disabled = false;

    if (data.ok) {
      const bits = [`Package written in ${data.seconds}s`];
      if (data.zipMB) bits.push(`${data.zipMB} MB zipped`);
      if (data.replaced?.length) bits.push("replaced the previous one");
      $("blender-state").textContent = bits.join(" · ") + ".";
      const lines = [...(data.lines || [])];
      if (data.zipUrl) lines.push("", `open: ${data.zipUrl}`);
      $("blender-log").textContent = lines.join("\n");
      $("blender-log").hidden = false;
      $("blender-go").textContent = "Export again";
      $("blender-replace").hidden = false;
      status(`Blender package for "${name}" is ready.`, 8000);
    } else if (data.exists) {
      $("blender-state").textContent = data.error;
      $("blender-replace").hidden = false;
    } else {
      $("blender-state").textContent = `⚠ ${data.error || "the writer failed"}`;
      if (data.lines?.length) {
        $("blender-log").textContent = data.lines.join("\n");
        $("blender-log").hidden = false;
      }
    }
  } catch (err) {
    clearInterval(blenderBusy); blenderBusy = null;
    $("blender-go").disabled = false;
    $("blender-state").textContent = `⚠ ${err.message}`;
  }
}

$("blender-go").onclick = () => runBlenderExport(false);
$("blender-replace").onclick = () => runBlenderExport(true);

/* Scenes produced by tools/capture.py, listed from output/scenes.json.
 * Loading them by URL (rather than dropping a file in) is what lets the viewer
 * find the cameras.json beside them and open at a real capture position. */
let generated = [];   // the scenes.json entries, for "Open scene" on the card

function openGenerated(name) {
  const s = generated.find((g) => g.name === name);
  if (!s) { status(`Scene "${name}" is not in the list yet — refresh.`, 5000); return; }
  const ext = (s.ply.split(".").pop() || "ply").toLowerCase();
  loadSource({ url: s.ply, name: `${s.name}.${ext}` }, s.name, s.name);
}

async function listGeneratedScenes() {
  try {
    const res = await fetch("/output/scenes.json", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    if (data?.format !== "dlscenes" || !data.scenes?.length) return;
    generated = data.scenes;
    const list = $("generated-list");
    list.innerHTML = "";
    for (const s of data.scenes) {
      const li = document.createElement("li");
      const open = document.createElement("button");
      open.className = "link grow";
      open.style.textAlign = "left";
      open.textContent = s.name;
      open.title = `${s.megabytes} MB${s.cameras ? " · has capture cameras" : ""}`;
      // scenes.json's "ply" field holds whatever was actually shipped: the
      // pipeline compresses to SPZ and puts that here, keeping the raw .ply
      // under "original". The name must carry the REAL extension -- the viewer
      // picks its parser from it, so a hardcoded ".ply" fed a gzipped SPZ to
      // the PLY reader and every generated scene died on "not a valid PLY".
      const ext = (s.ply.split(".").pop() || "ply").toLowerCase();
      open.onclick = () => loadSource({ url: s.ply, name: `${s.name}.${ext}` }, s.name, s.name);
      const meta = document.createElement("span");
      meta.className = "val";
      meta.textContent = `${s.megabytes} MB`;
      li.append(open, meta);
      list.appendChild(li);
    }
    $("panel-generated").hidden = false;
    syncGroups();
  } catch {
    // no generated scenes yet, or the output folder is not being served
  }
}
listGeneratedScenes();
detectBackend();

/* ------------------------------------------------------------------ samples */

/* Real scenes made with this tool, listed in data/samples/samples.json (written
 * by tools/make_sample.py). `?samples=<same-origin url>` points at another list,
 * which is how a stand-in list is tried without touching the real one. */
function samplesUrl() {
  const asked = new URLSearchParams(location.search).get("samples");
  if (asked) {
    try {
      const u = new URL(asked, location.href);
      if (u.origin === location.origin) return u;   // never a request elsewhere
    } catch { /* not a URL: fall back */ }
  }
  return new URL("data/samples/samples.json", location.href);
}

const shortCount = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} M`
  : n >= 1e3 ? `${Math.round(n / 1e3)} k` : `${n}`);

async function listSamples() {
  const base = samplesUrl();
  let list = [];
  try {
    const res = await fetch(base, { cache: "no-store" });
    if (res.ok) {
      const data = await res.json();
      if (data?.format === "dlsamples" && Array.isArray(data.samples)) list = data.samples;
    }
  } catch { /* no samples shipped: the block stays hidden */ }

  const grid = $("samples-grid");
  grid.innerHTML = "";
  // the walked video first, then the photograph: the order of the Make panel
  list.sort((a, b) => (a.kind === "video" ? 0 : 1) - (b.kind === "video" ? 0 : 1));
  for (const s of list) {
    const tile = document.createElement("button");
    tile.className = "sample-tile";
    tile.dataset.id = s.id;
    const frame = document.createElement("div");
    frame.className = "sample-img";
    const img = document.createElement("img");
    img.src = new URL(s.thumb, base).href;
    img.alt = s.title;
    img.loading = "lazy";
    const chip = document.createElement("span");
    chip.className = "sample-chip";
    chip.textContent = s.kind === "photo" ? "Photo" : "Video";
    frame.append(img, chip);
    const cap = document.createElement("div");
    cap.className = "sample-cap";
    const title = document.createElement("b");
    title.textContent = s.title;
    const line = document.createElement("span");
    line.textContent = s.kind === "photo"
      ? `One photograph · 2.5D, scale estimated · ${shortCount(s.splats)} splats`
      : `A walked video · ${s.frames} frames solved · ${shortCount(s.splats)} splats`;
    cap.append(title, line);
    tile.append(frame, cap);
    tile.onclick = () => {
      for (const t of grid.children) t.classList.toggle("on", t === tile);
      loadSource({ url: new URL(s.scene, base).href, name: `${s.id}.spz` }, s.title, null, s);
    };
    grid.appendChild(tile);
  }
  $("samples").hidden = !list.length;
}
listSamples();

function showSampleCard() {
  const s = currentSample;
  $("sample-card").hidden = !s;
  for (const t of $("samples-grid").children) t.classList.toggle("on", !!s && t.dataset.id === s.id);
  if (!s) return;
  $("sample-kicker").textContent = `Sample · ${s.kind === "photo" ? "one photograph" : "walked video"}`;
  $("sample-title").textContent = [s.title, s.place, s.date].filter(Boolean).join(" · ");
  const how = s.kind === "photo"
    ? "Depth estimated from a single picture: faithful near where it was taken, "
      + "and opened in the photo's own frame."
    : `${s.frames} frames solved into a scene, opened at the first camera — `
      + "walk the capture to see it as filmed.";
  const thinned = s.splatsOriginal > s.splats
    ? ` Thinned to ${shortCount(s.splats)} of ${shortCount(s.splatsOriginal)} splats to travel light.` : "";
  $("sample-meta").textContent = (s.note ? `${s.note} ` : "") + how + thinned;
}
$("sample-card-close").onclick = () => { $("sample-card").hidden = true; };

const veil = $("dropveil");
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  e.preventDefault(); dragDepth++; veil.classList.add("on");
});
window.addEventListener("dragleave", (e) => {
  e.preventDefault();
  if (--dragDepth <= 0) { dragDepth = 0; veil.classList.remove("on"); }
});
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0; veil.classList.remove("on");
  const f = e.dataTransfer.files?.[0];
  if (!f) return;
  const lower = f.name.toLowerCase();
  if (lower.endsWith(".json") || lower.endsWith(".dlscene")) loadSceneFile(f);
  else if (Viewer.isSplatFile(f.name)) loadSource({ file: f }, f.name);
  else if (/\.(mov|mp4|m4v|avi|mkv)$/.test(lower)) {
    // a video is not something to view, it is something to turn into a scene
    if ($("panel-make").hidden) {
      fail(new Error("Video needs the capture backend — start the app with "
        + "the .venv interpreter."));
    } else {
      $("sec-import").open = true;
      $("panel-make").open = true;
      $("panel-make").scrollIntoView({ behavior: "smooth", block: "nearest" });
      startCapture(f);
    }
  } else fail(new Error(`${f.name} is not a splat, scene or video file.`));
});

/* ------------------------------------------------------------------ display */

const splatsShort = (n) => n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : n.toLocaleString("en-US");

/* The slider says what it does: the share of the scene's splats drawn, and how
 * many that is. On a scene small enough to need no level-of-detail tree it is
 * disabled and says why, rather than moving and changing nothing. */
function syncDetailUi() {
  const n = viewer.splatCount;
  const slider = $("lod-scale");
  slider.disabled = !viewer.hasLod;
  if (!viewer.hasLod) {
    slider.value = 1;
    $("lod-val").textContent = "100 %";
    $("lod-note").textContent = n
      ? `Every splat is drawn — ${splatsShort(n)} is few enough to need no level of detail.`
      : "";
    viewer.setDetail(null);
    return;
  }
  const share = display.detail ?? viewer.defaultDetail();
  slider.value = share;
  viewer.setDetail(display.detail);
  const drawn = Math.round(share * n);
  $("lod-val").textContent = `${Math.round(share * 100)} %`;
  $("lod-note").textContent = (share >= 1
    ? `All ${splatsShort(n)} splats drawn.`
    : `Up to ${splatsShort(drawn)} of ${splatsShort(n)} splats drawn; the rest stand in as coarser ones, mostly far away.`)
    + (display.detail == null ? " This computer's default." : " Lower is smoother on a slow computer.");
}
/* A saved scene's detail. Files before 2026-09-24 stored `lodScale`, a factor
 * on this computer's budget; turn it into the share it meant. 1 was the
 * default, so it stays the default. */
function sceneDetail(d) {
  if (d.detail !== undefined) return d.detail;
  if (d.lodScale == null || d.lodScale === 1 || !viewer.hasLod) return null;
  return Math.min(1, Math.max(0.1, d.lodScale * viewer.defaultDetail()));
}
$("lod-scale").oninput = (e) => {
  display.detail = parseFloat(e.target.value);
  syncDetailUi();
};
$("blur").oninput = (e) => {
  display.blur = parseFloat(e.target.value);
  viewer.setBlur(display.blur);
  $("blur-val").textContent = display.blur.toFixed(2);
};

/* ------------------------------------------------------ the density switch */

/* Hide the explanations once they have been read -- the DL tools' `?`, as in
 * DL-TerrainMapper. Only `.why` goes (see the stylesheet); warnings and live
 * state stay. HIDDEN BY DEFAULT, as Marc decided for TerrainMapper: the prose
 * earns its place the first time and is furniture after. The choice is
 * remembered on this browser; when storage is unavailable it simply starts
 * hidden again. */
function setTerse(terse) {
  document.body.classList.toggle("terse", terse);
  // the button says what it will DO, and never looks switched on
  $("explainToggle").textContent = terse ? "Show explanations" : "Hide explanations";
  try { localStorage.setItem("dlsg.terse", terse ? "1" : "0"); } catch { /* not offered */ }
}
$("explainToggle").onclick = () => setTerse(!document.body.classList.contains("terse"));
setTerse((() => { try { return localStorage.getItem("dlsg.terse") ?? "1"; } catch { return "1"; } })() === "1");

/* ------------------------------------------------------------ the source */

/* What the scene's source photo or video says about itself (tools/source_meta.py):
 * a scene made here asks the backend, which reads the file on demand; a sample
 * carries it in its record, without the GPS position. Nothing leaves the
 * machine -- coordinates can be copied, not sent to a map. */
let sourceMeta = null;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function formatWhen(s) {
  const m = String(s || "").match(/^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::\d\d(?:\.\d+)?)?(Z|[+-]\d\d:?\d\d)?/);
  if (!m) return s || "";
  const zone = !m[6] ? "" : m[6] === "Z" ? " (UTC)"
    : ` (UTC${m[6].slice(0, 3)}:${m[6].slice(-2)})`;
  return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}, ${m[4]}:${m[5]}${zone}`;
}

async function refreshSourcePanel() {
  const panel = $("panel-source");
  const want = currentSample || generatedName;
  sourceMeta = null;
  if (currentSample) sourceMeta = currentSample.record?.source_meta || null;
  else if (generatedName && hasBackend) {
    try {
      const res = await fetch(`/api/source/${encodeURIComponent(generatedName)}`);
      if (res.ok) sourceMeta = await res.json();
    } catch { /* no backend answer: the panel stays hidden */ }
  }
  if ((currentSample || generatedName) !== want) return;   // a newer scene took over
  const m = sourceMeta;
  panel.hidden = !m || !!m.error && !m.kind;
  $("source-copy-row").hidden = true;
  if (panel.hidden) { syncGroups(); return; }

  const facts = [];
  const add = (k, v) => { if (v) facts.push([k, v]); };
  add("File", m.file);
  add("Captured", formatWhen(m.when));
  if (m.where) {
    const w = m.where;
    const ns = w.lat >= 0 ? "N" : "S", ew = w.lon >= 0 ? "E" : "W";
    // 4 decimals (about 10 m) -- a phone's fix is good to several metres at
    // best, and Apple records exactly this many; more would be false precision
    add("Location", `${Math.abs(w.lat).toFixed(4)}° ${ns}, ${Math.abs(w.lon).toFixed(4)}° ${ew}`
      + (w.accuracy_m ? ` · ±${Math.round(w.accuracy_m)} m` : ""));
    if (w.alt != null) add("Altitude", `${Math.round(w.alt)} m`);
    if (w.heading_deg != null) add("Facing", `${Math.round(w.heading_deg)}° from ${w.heading_ref || "north"}`);
    $("source-copy-row").hidden = false;
  }
  const c = m.camera || {};
  add("Device", [c.make, c.model].filter(Boolean).join(" ").replace(/^(\w+) \1\b/, "$1")
    + (c.software && c.make === "Apple" ? ` · iOS ${c.software}` : ""));
  if (c.focal_mm || c.focal_35mm) {
    add("Lens", [c.lens, c.focal_mm && `${c.focal_mm} mm`,
      c.focal_35mm && `${c.focal_35mm} mm equiv.`].filter(Boolean).join(" · "));
  }
  if (c.hfov_deg) add("Field of view", `${c.hfov_deg}° across`);
  const im = m.image || {};
  add(m.kind === "video" ? "Video" : "Image", [
    im.width && `${im.width} × ${im.height}`, im.orientation,
    im.fps && `${Math.round(im.fps * 100) / 100} fps`,
    m.duration_s && `${m.duration_s} s`, im.format || im.codec,
  ].filter(Boolean).join(" · "));
  add("Artist", m.credit?.artist);
  add("Licence", m.credit?.copyright);

  const list = $("source-facts");
  list.innerHTML = "";
  for (const [k, v] of facts) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    list.append(dt, dd);
  }

  // the most useful fact: a photo's own lens against the field of view the
  // scene was built with (every photo got 65° before 2026-09-25; since then the
  // route uses the photo's own lens when the file records a 35 mm equivalent)
  const notes = [];
  const built = Number(photoRecord?.settings?.fov);
  if (photoRecord && built) {
    if (c.hfov_deg && Math.abs(c.hfov_deg - built) / built > 0.1) {
      notes.push(`⚠ The scene was built assuming ${built}° across; this photo's lens `
        + `gives ${c.hfov_deg}°. Its proportions are off by that much.`);
    } else if (!c.hfov_deg && c.focal_mm) {
      notes.push(`The file gives the focal length (${c.focal_mm} mm) but not the sensor `
        + `size, so the true field of view is unknown; the scene assumed ${built}°.`);
    }
  }
  if (m.where) notes.push("The position is where the capture began, from the phone's GPS; "
    + "it does not place or turn the scene.");
  if (currentSample && !m.where) notes.push("A sample carries no GPS position.");
  $("source-note").textContent = notes.join(" ");
  syncGroups();
}

$("source-copy").onclick = async () => {
  const w = sourceMeta?.where;
  if (!w) return;
  const text = `${w.lat.toFixed(4)}, ${w.lon.toFixed(4)}`;
  try { await navigator.clipboard.writeText(text); status(`Copied ${text}`, 3000); }
  catch { status(text, 8000); }
};

/* Colour by viewing angle. Offered so a stray tint can be checked: if it goes
 * when this is off, it was the extrapolated colour of a direction the camera
 * never looked from. Disabled, and says why, on a scene with one colour per
 * splat (a photo scene, a plain point cloud). */
function syncViewColourUi() {
  const has = viewer.hasViewColour();
  const box = $("view-colour");
  box.disabled = !has;
  box.checked = has ? display.viewColour : false;
  $("view-colour-note").textContent = !viewer.mesh ? ""
    : !has ? "This scene has one colour per splat, the same from every side."
    : display.viewColour
      ? "Splats change colour with the direction you look from — how sheen and "
        + "reflections are captured. Where the camera never looked from, that colour "
        + "is guessed and can show as stray tints: switch it off to check."
      : "Off: every splat keeps one colour from every side. A tint that went away "
        + "came from the viewing angle; sheen and reflections are gone too.";
}
$("view-colour").onchange = (e) => {
  display.viewColour = e.target.checked;
  viewer.setViewColour(display.viewColour);
  syncViewColourUi();
};
$("flip-btn").classList.add("on");
$("flip-btn").onclick = () => {
  viewer.setFlip(!viewer.flip);
  $("flip-btn").classList.toggle("on", viewer.flip);
  applySection();
  rebuildPlan();
};

/* ----------------------------------------------------------------- the plan */

const plan = new PlanView(viewer, $("plan"));
window.dl.plan = plan;                   // for the browser-side checks
plan.onPick = (i) => {
  viewer.stopWalk();
  viewer.goToCaptureCamera(i, true, cameraView);
  refreshCaptureState();
  syncFrameSlider();
};
let planTimer = 0;
/* Built once per scene -- one pass over the splats -- and deferred a moment so
 * a scene appears before its plan is worked out. */
function rebuildPlan() {
  clearTimeout(planTimer);
  $("plan").hidden = !(display.plan && viewer.mesh);
  if ($("plan").hidden) return;
  planTimer = setTimeout(() => { plan.build(); syncPlanScale(); }, 60);
}
function syncPlanScale() {
  plan.setScale(tools.calibrated ? tools.scaleFactor : null, tools.estimated);
}
/* under the navigation column, whose height changes with the frame slider */
function placePlan() {
  const g = $("gizmo"), el = $("plan");
  if (el.hidden) return;
  const top = g.offsetTop + g.offsetHeight + 4;      // the column's own 4 px rhythm
  if (el.style.top !== `${top}px`) el.style.top = `${top}px`;
}
$("plan-toggle").onchange = (e) => { display.plan = e.target.checked; rebuildPlan(); };
$("plan-close").onclick = () => { display.plan = false; $("plan-toggle").checked = false; rebuildPlan(); };
$("reset-btn").onclick = () => viewer.resetView();

/* ------------------------------------------------------------------- gizmo */

for (const btn of document.querySelectorAll("#gizmo-views button, #gizmo-iso")) {
  btn.onclick = () => { setCameraView(false); viewer.setStandardView(btn.dataset.view); };
}

/* Camera view: stand where the camera stood, and scrub along the capture with
 * the slider. The same positions the Capture views panel steps through — this
 * is the same thing under your hand instead of two buttons away. Only a scene
 * made here has them, so the button says so by disabling itself. */
let cameraView = false;

function setCameraView(on) {
  const cams = viewer.captureCameras;
  if (!cams && viewer.photoCamera) {
    cameraView = !!on;
    $("gizmo-camera").classList.toggle("on", cameraView);
    $("gizmo-frames").hidden = true;
    if (cameraView) viewer.enterPhotoView(true);
    else viewer.leavePhotoView();
    labelStillSize();
    return;
  }
  cameraView = !!on && !!cams;
  $("gizmo-camera").classList.toggle("on", cameraView);
  $("gizmo-frames").hidden = !cameraView;
  if (cameraView) {
    const slider = $("gizmo-frame");
    slider.max = cams.length - 1;
    slider.value = viewer.captureIndex || 0;
    // the camera view shows the frame the camera filmed, outlined, the rest
    // dimmed: in a wide window most of the view was never filmed from here
    viewer.goToCaptureCamera(Number(slider.value), true, true);
    labelFrame();
    if (!viewer.hasCaptureFrames()) {
      status("This scene's camera file predates frame sizes, so the filmed frame "
        + "cannot be outlined — re-export its cameras to see it.", 7000);
    }
  } else {
    viewer.leavePhotoView();
  }
  labelStillSize();
}

function labelFrame() {
  const cams = viewer.captureCameras;
  if (!cams) return;
  const i = Number($("gizmo-frame").value);
  $("gizmo-frame-label").textContent = `frame ${i + 1} / ${cams.length}`;
}

function syncCameraButton() {
  const photo = !viewer.captureCameras && !!viewer.photoCamera;
  const has = !!viewer.captureCameras || photo;
  $("gizmo-camera").disabled = !has;
  $("gizmo-camera").textContent = photo ? "Photo" : "Camera";
  $("gizmo-camera").title = photo
    ? "Stand where the photo was taken — its field of view, its frame"
    : has
      ? "Stand where the camera stood — the frame it filmed outlined"
      : "Only for a scene made here — it needs the capture's camera path";
  if (!has) setCameraView(false);
}

$("gizmo-camera").onclick = () => setCameraView(!cameraView);
$("gizmo-frame").oninput = () => {
  labelFrame();
  viewer.stopWalk();
  viewer.goToCaptureCamera(Number($("gizmo-frame").value), false, cameraView);
  refreshCaptureState();          // the sidebar names the same frame
};

// Live axis triad. Drawn from the projected world axes each frame, so it shows
// which way the scene is oriented rather than being decoration.
const AXES = {
  x: [$("ax-x"), $("ax-x-l")],
  y: [$("ax-y"), $("ax-y-l")],
  z: [$("ax-z"), $("ax-z-l")],
};
function drawGizmo() {
  if (!viewer.mesh) return;
  const dirs = viewer.axisScreenDirections();
  for (const [key, [line, label]] of Object.entries(AXES)) {
    const d = dirs[key];
    const len = Math.hypot(d.x, d.y) || 1e-6;
    // normalise so the triad keeps a constant size whatever the zoom
    const k = 34 / Math.max(len, 1e-6);
    const x = d.x * k, y = d.y * k;
    line.setAttribute("x2", x.toFixed(1));
    line.setAttribute("y2", y.toFixed(1));
    label.setAttribute("x", (x * 1.28).toFixed(1));
    label.setAttribute("y", (y * 1.28).toFixed(1));
    // fade the axis pointing away from the viewer
    const away = d.depth > 0;
    line.style.opacity = away ? 0.35 : 1;
    label.style.opacity = away ? 0.35 : 1;
  }
}

// Tools already owns onFrame for its overlay; chain rather than replace.
const toolsOnFrame = viewer.onFrame;
viewer.onFrame = () => {
  toolsOnFrame?.();
  drawGizmo();
  placePlan();
  plan.draw();
  // a drag or zoom leaves the photo view; the button follows
  if (cameraView && viewer.photoCamera && !viewer.captureCameras && !viewer.photoView) {
    cameraView = false;
    $("gizmo-camera").classList.remove("on");
  }
  // a still is cropped to a shown frame: its size note follows the frame
  const framed = !!viewer.frameRect();
  if (framed !== wasFramed) {
    wasFramed = framed;
    if (!$("panel-still").hidden) labelStillSize();
  }
};
let wasFramed = false;

/* ------------------------------------------------------------------ section */

function applySection() {
  viewer.setSection(display.section);
}

/** Push display.section into the toggle, sliders and readouts. */
function syncSectionUi() {
  $("section-toggle").classList.toggle("on", display.section.enabled);
  $("section-controls").hidden = !display.section.enabled;
  for (const a of ["x", "y", "z"]) {
    const lo = Math.round(display.section.min[a] * 100);
    const hi = Math.round(display.section.max[a] * 100);
    $(`sec-${a}-min`).value = lo;
    $(`sec-${a}-max`).value = hi;
    $(`sec-${a}-val`).textContent = `${lo}–${hi}%`;
  }
}

for (const a of ["x", "y", "z"]) {
  $(`sec-${a}-min`).oninput = (e) => {
    display.section.min[a] = Math.min(
      parseFloat(e.target.value) / 100, display.section.max[a]);
    syncSectionUi();
    applySection();
  };
  $(`sec-${a}-max`).oninput = (e) => {
    display.section.max[a] = Math.max(
      parseFloat(e.target.value) / 100, display.section.min[a]);
    syncSectionUi();
    applySection();
  };
}
$("section-toggle").onclick = () => {
  display.section.enabled = !display.section.enabled;
  syncSectionUi();
  applySection();
};
$("section-reset").onclick = () => {
  display.section = { ...freshSection(), enabled: true };
  syncSectionUi();
  applySection();
};

/* -------------------------------------------------------------------- tools */

$("tool-dist").onclick = () => tools.start("distance");
$("tool-height").onclick = () => tools.start("height");
$("tool-area").onclick = () => tools.start("area");
$("tool-note").onclick = () => tools.start("note");
$("calibrate-btn").onclick = () => tools.start("calibrate");
$("assume-metric").onclick = () => {
  tools.assumeMetric();
  status("Scene units are now read as metres.");
};
function refreshCaptureState() {
  const cams = viewer.captureCameras;
  syncLevelUi();
  if (!cams) return;
  const cam = cams[viewer.captureIndex] || {};
  $("capture-state").textContent =
    `${viewer.captureIndex + 1} of ${cams.length} — ${cam.name || ""}`;
  $("walk-play").textContent = viewer.walking ? "❚❚ Pause" : "▶ Walk the capture";
}

/* Levelling: a solve's axes are arbitrary, so a scene often sits a few degrees
 * off and the horizon leans as you orbit. The capture cameras know which way
 * was up; without them there is nothing to level against, and the control says
 * so instead of pretending. */
function syncLevelUi() {
  const info = viewer.upInfo();
  const box = $("level-horizon");
  box.checked = info.level;
  box.disabled = !info.available;
  $("level-note").textContent = info.available
    ? (info.tiltDeg >= 0.2
        ? `This scene sits ${info.tiltDeg}° off level; the capture cameras say `
          + "which way is up."
        : "This scene is already level.")
    : "No capture cameras with an up direction, so there is nothing to level "
      + "against — orbiting uses the file's own vertical.";
}

viewer.onWalk = (i, total, running) => {
  const cam = viewer.captureCameras?.[i] || {};
  $("capture-state").textContent = `${i + 1} of ${total} — ${cam.name || ""}`;
  $("walk-play").textContent = running ? "❚❚ Pause" : "▶ Walk the capture";
  // the scrubber follows the walk rather than fighting it
  if (cameraView) { $("gizmo-frame").value = i; labelFrame(); }
};

$("walk-play").onclick = () => {
  if (viewer.walking) { viewer.stopWalk(); return; }
  const fps = Number($("walk-speed").value) || 8;
  const atEnd = viewer.captureIndex >= (viewer.captureCameras?.length || 1) - 1;
  viewer.startWalk({ fps, loop: $("walk-loop").checked, from: atEnd ? 0 : null });
  refreshCaptureState();
};
/* The walk plays at the speed the site was actually filmed at when we know it:
 * the pipeline kept every Nth frame, so kept frames per second = fps / stride.
 * capture.json has carried `fps` since 2026-09-06; older scenes fall back to a
 * comfortable 8. */
let walkRealtimeFps = null;

function labelWalkSpeed() {
  const fps = Number($("walk-speed").value);
  $("walk-speed-note").textContent = `${fps} frames/s`
    + (walkRealtimeFps && fps === walkRealtimeFps ? " — as filmed" : "");
}

async function syncFromCaptureRecord() {
  walkRealtimeFps = null;
  // a sample carries its record; a scene made here has one in its output folder
  let rec = currentSample?.record ?? null;
  if (!rec && generatedName) {
    try {
      rec = await (await fetch(
        `/output/${encodeURIComponent(generatedName)}/capture.json`)).json();
    } catch { /* no record, or a scene not made here: keep the defaults */ }
  }
  const stride = rec?.settings?.stride;
  if (rec?.fps > 0 && stride > 0) {
    walkRealtimeFps = Math.max(1, Math.min(30, Math.round(rec.fps / stride)));
  }
  if (walkRealtimeFps) $("walk-speed").value = walkRealtimeFps;
  labelWalkSpeed();

  /* A single-photo scene: its camera is known exactly, so the navigation's
   * camera button stands there. The record carries the field of view, and the
   * image size since 2026-09-24; older records have the aspect read back from
   * the splats instead. */
  if (rec?.method === "single-image metric depth" && viewer.mesh) {
    const img = rec.image;
    const aspect = img?.width > 0 && img?.height > 0 ? img.width / img.height
      : img?.aspect > 0 ? img.aspect : viewer.photoAspectFromSplats();
    viewer.setPhotoCamera({ hfov: Number(rec.settings?.fov) || 65, aspect });
  } else {
    viewer.setPhotoCamera(null);
  }
  syncCameraButton();
  // a photo SAMPLE opens where it is faithful: in the photo's own frame
  if (currentSample?.kind === "photo" && viewer.photoCamera) setCameraView(true);

  /* Scale from the record -- only when someone MEASURED it. A capture whose
   * capture.json records `scale_m_per_unit` (a dimension measured on site) is
   * applied for you. A single photograph's depth-model metres are NOT: until
   * 2026-09-24 they were applied as "already in metres", and on a calibrated
   * capture the same model then put everything 5-12x too deep
   * (output\research\DEPTH SMALL VS BASE - RESULTS - 002.md). They are offered
   * on the Scale panel instead, and labelled as an estimate if used. */
  photoScale = rec?.method === "single-image metric depth";
  photoRecord = photoScale ? rec : null;
  $("panel-gltf").hidden = !(photoScale && viewer.mesh);
  if (photoScale) $("gltf-state").textContent = photoUrl() ? ""
    : "The photograph this scene was made from is not reachable from here, and "
      + "the export needs it — for the texture, and to rebuild the depth grid.";
  $("gltf-go").disabled = $("gltf-splat-go").disabled = !photoUrl();
  const measured = Number(rec?.scale_m_per_unit);
  if (!photoScale && measured > 0 && !tools.calibrated) {
    tools.setScale(measured);
    status(`Calibrated from ${rec.scale_note || "a measured dimension"}: `
      + `1 unit = ${measured.toFixed(3)} m.`, 7000);
  } else if (photoScale && !tools.calibrated) {
    status("Scale not set: this scene's metres are only the depth model's estimate "
      + "— see Measure → Scale.", 7000);
  }
  refreshLists();
  syncGroups();
  refreshSourcePanel();          // after photoRecord: it compares the lens with it
  if (viewer.photoCamera) rebuildPlan();   // a photo scene's camera is known only now
}

$("use-estimate").onclick = () => {
  tools.setScale(1, { estimated: true });
  status("Using the depth estimate: every figure is marked “est.”", 6000);
};

$("walk-speed").oninput = () => {
  labelWalkSpeed();
  viewer.setWalkFps(Number($("walk-speed").value));
};
$("walk-loop").onchange = (e) => { if (viewer._walk) viewer._walk.loop = e.target.checked; };
$("level-horizon").onchange = (e) => { viewer.setLevel(e.target.checked); rebuildPlan(); };
/* Prev/Next in the sidebar and the scrubber in the corner are the same control
 * in two places, so each moves the other. */
function syncFrameSlider() {
  if (!cameraView || !viewer.captureCameras) return;
  $("gizmo-frame").value = viewer.captureIndex || 0;
  labelFrame();
}

function stepCapture(delta) {
  if (!viewer.captureCameras) return;
  viewer.goToCaptureCamera(viewer.captureIndex + delta, true, cameraView);
  refreshCaptureState();
  syncFrameSlider();
}
$("capture-prev").onclick = () => stepCapture(-1);
$("capture-next").onclick = () => stepCapture(1);

$("view-add").onclick = () => {
  const name = prompt("Viewpoint name:", `View ${tools.viewpoints.length + 1}`);
  if (name) tools.addViewpoint(name);
};

window.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  if (e.key === "Enter") tools.finish();
  else if (e.key === "Escape") tools.cancel();
  else if (e.key === "r" || e.key === "R") viewer.resetView();
  else if (e.key === "u" || e.key === "U") $("flip-btn").click();
  else if (e.key === "[") stepCapture(-1);
  else if (e.key === "]") stepCapture(1);
  else if (e.key === "1") viewer.setStandardView("top");
  else if (e.key === "2") viewer.setStandardView("front");
  else if (e.key === "3") viewer.setStandardView("right");
  else if (e.key === "4") viewer.setStandardView("iso");
});

/* -------------------------------------------------------------- scene files */

async function currentBytes() {
  if (source?.file) return new Uint8Array(await source.file.arrayBuffer());
  const res = await fetch(source.url);
  return new Uint8Array(await res.arrayBuffer());
}

/* ------------------------------------------------------------ still image */

/* What a student hands in: the view as framed, at a chosen size. The 3D image
 * comes from Viewer.captureStill(); measurements and notes live in a DOM layer
 * ON TOP of the canvas, so a plain capture would silently drop them. They are
 * therefore redrawn here from the same data, in the same style, at the still's
 * own scale -- which also keeps them sharp at 4K where a screenshot of the
 * DOM would be blurred. The colours and fonts are read from the live theme, so
 * the still matches the screen. */

/* The width the whole viewport is rendered at. In the photo view the still is
 * cropped to the photo's rectangle, so "4K wide" is asked of the crop and the
 * viewport is rendered wider by the same factor. */
function stillWidth() {
  const v = $("still-size").value;
  const cssW = $("canvas").clientWidth || 1;
  const dpr = viewer.renderer.getPixelRatio();
  const frame = viewer.frameRect();
  if (v === "2x") return Math.round(cssW * dpr * 2);
  if (v === "3840") return Math.round(3840 * (frame ? cssW / frame.w : 1));
  return Math.round(cssW * dpr);
}

/* The size a still comes out at: the viewport, or the photo's rectangle. */
function stillSize() {
  const c = $("canvas");
  const cssW = Math.max(1, c.clientWidth);
  const scale = stillWidth() / cssW;
  const frame = viewer.frameRect();
  return frame
    ? { w: Math.round(frame.w * scale), h: Math.round(frame.h * scale), frame: true }
    : { w: Math.round(cssW * scale), h: Math.round(c.clientHeight * scale), frame: false };
}

function labelStillSize() {
  const { w, h, frame } = stillSize();
  $("still-size-note").textContent = `${w} × ${h} px${frame
    ? (viewer.photoCamera && !viewer.captureCameras ? " — the photo's frame" : " — the filmed frame")
    : ""}`;
  $("still-views").disabled = !tools.viewpoints.length;
  $("still-views").title = tools.viewpoints.length
    ? `${tools.viewpoints.length} saved viewpoint(s)`
    : "save a viewpoint first (Display → Viewpoints)";
}

function drawOverlay(ctx, scale) {
  const css = getComputedStyle(document.documentElement);
  const token = (n, fallback) => (css.getPropertyValue(n).trim() || fallback);
  const onStage = token("--on-stage", "#fdfcf9");
  const sheet = token("--sheet", "#14161a");
  const ink = token("--ink", "#ece8e1");
  const line = token("--line", "#343841");
  const body = token("--font-body", "sans-serif");
  const head = token("--font-head", "sans-serif");
  const W = ctx.canvas.width, H = ctx.canvas.height;
  const at = (p) => {
    const s = viewer.project(p);
    return { x: s.x * scale, y: s.y * scale,
             ok: s.visible && s.x >= 0 && s.y >= 0 && s.x * scale <= W && s.y * scale <= H };
  };

  // lines and areas first, so markers and labels sit on top of them
  for (const m of tools.measurements) {
    const pts = m.points.map(at);
    if (pts.length < 2 || !pts.every((q) => q.ok)) continue;
    ctx.beginPath();
    pts.forEach((q, i) => (i ? ctx.lineTo(q.x, q.y) : ctx.moveTo(q.x, q.y)));
    if (m.kind === "area") {
      ctx.closePath();
      ctx.fillStyle = "rgba(217,195,154,.25)";
      ctx.fill();
    }
    ctx.strokeStyle = onStage;
    ctx.lineWidth = 1.5 * scale;
    ctx.stroke();
  }

  const marker = (q) => {
    const r = 4.5 * scale;
    ctx.beginPath(); ctx.arc(q.x, q.y, r + 2 * scale, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(20,22,26,.85)"; ctx.fill();
    ctx.beginPath(); ctx.arc(q.x, q.y, r, 0, Math.PI * 2);
    ctx.fillStyle = onStage; ctx.fill();
  };

  // a label box centred above its point, the way .label sits on screen
  const label = (q, lines, lift) => {
    const size = 11.4 * scale, lh = size * 1.3;
    const padX = 7 * scale, padY = 3 * scale;
    const widths = lines.map(({ text, bold }) => {
      ctx.font = `${bold ? "700" : "400"} ${size}px ${bold ? head : body}`;
      return ctx.measureText(text).width;
    });
    const w = Math.max(...widths) + padX * 2;
    const h = lh * lines.length + padY * 2;
    const x = q.x - w / 2, y = q.y - h * lift;
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, 5 * scale);
    ctx.fillStyle = sheet; ctx.fill();
    ctx.strokeStyle = line; ctx.lineWidth = scale; ctx.stroke();
    ctx.fillStyle = ink;
    ctx.textBaseline = "middle";
    lines.forEach(({ text, bold }, i) => {
      ctx.font = `${bold ? "700" : "400"} ${size}px ${bold ? head : body}`;
      ctx.fillText(text, x + padX, y + padY + lh * (i + 0.5));
    });
  };

  for (const m of tools.measurements) {
    const pts = m.points.map(at);
    pts.filter((q) => q.ok).forEach(marker);
    if (!pts.every((q) => q.ok)) continue;
    const mid = pts.reduce((a, q) => ({ x: a.x + q.x / pts.length, y: a.y + q.y / pts.length }),
                           { x: 0, y: 0 });
    label({ ...mid, ok: true }, [{ text: tools.format(m) }], 1.4);
  }
  for (const n of tools.notes) {
    const q = at(n.point);
    if (!q.ok) continue;
    marker(q);
    const lines = [{ text: n.title, bold: true }];
    if (n.text) lines.push({ text: n.text });
    label(q, lines, 1.6);
  }
}

async function stillBlob() {
  if (viewer.walking) viewer.stopWalk();        // a moving camera never settles
  const frame = viewer.frameRect();         // read before rendering moves nothing
  let shot = await viewer.captureStill({ width: stillWidth() });
  const scale = shot.width / $("canvas").clientWidth;
  if ($("still-overlay").checked) {
    await document.fonts.ready;                 // labels in the house fonts
    drawOverlay(shot.getContext("2d"), scale);
  }
  if (frame) {
    // the photo view: keep exactly the photo's rectangle
    const crop = document.createElement("canvas");
    crop.width = Math.round(frame.w * scale);
    crop.height = Math.round(frame.h * scale);
    crop.getContext("2d").drawImage(shot, Math.round(frame.x * scale), Math.round(frame.y * scale),
      crop.width, crop.height, 0, 0, crop.width, crop.height);
    crop.stillInfo = { ...shot.stillInfo, width: crop.width, height: crop.height };
    shot = crop;
  }
  const blob = await new Promise((res) => shot.toBlob(res, "image/png"));
  if (!blob) throw new Error("the browser could not encode the image");
  return { blob, info: shot.stillInfo };
}

const stillStem = () => ((generatedName || sourceInfo?.name || "scene")
  .replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_-]+/g, "_"));
const stamp = () => new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");

function sayStill(info, what) {
  const clamp = info.clamped
    ? ` Capped at ${info.maxDim} px — the most this graphics chip can hold.` : "";
  $("still-state").textContent = `${what} — ${info.width} × ${info.height} px.${clamp}`;
}

$("still-size").onchange = labelStillSize;
window.addEventListener("resize", () => { if (!$("panel-still").hidden) labelStillSize(); });
$("panel-still").addEventListener("toggle", labelStillSize);

$("still-go").onclick = async () => {
  const btn = $("still-go");
  btn.disabled = true;
  $("still-state").textContent = "Rendering the still…";
  try {
    const { blob, info } = await stillBlob();
    const name = `${stillStem()}-still-${stamp()}.png`;
    downloadBlob(blob, name);
    sayStill(info, `Saved ${name}`);
  } catch (err) {
    $("still-state").textContent = `⚠ ${err.message}`;
  }
  btn.disabled = false;
};

/* One still per saved viewpoint, as one ZIP: the natural way to hand in
 * several states of the same scene. Each viewpoint is jumped to without
 * animation, and the view the student was on is put back afterwards. */
$("still-views").onclick = async () => {
  if (!tools.viewpoints.length) return;
  const btn = $("still-views");
  btn.disabled = true;
  const back = viewer.getCameraState();
  const entries = [];
  let info = null;
  try {
    for (const [i, v] of tools.viewpoints.entries()) {
      $("still-state").textContent =
        `Rendering ${i + 1} of ${tools.viewpoints.length}: ${v.name}…`;
      viewer.setCameraState(v.camera, false);
      const shot = await stillBlob();
      info = shot.info;
      const safe = v.name.replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 40) || `view${i + 1}`;
      entries.push({ name: `${String(i + 1).padStart(2, "0")}-${safe}.png`,
                     data: new Uint8Array(await shot.blob.arrayBuffer()) });
    }
    const name = `${stillStem()}-stills-${stamp()}.zip`;
    downloadBlob(makeZip(entries), name);
    sayStill(info, `Saved ${entries.length} stills in ${name}`);
  } catch (err) {
    $("still-state").textContent = `⚠ ${err.message}`;
  } finally {
    viewer.setCameraState(back, false);
    btn.disabled = false;
  }
};

$("scene-save").onclick = async () => {
  if (!source) return;
  try {
    status("Hashing source…", 0);
    const bytes = await currentBytes();
    const scene = buildScene({
      viewer, tools, display,
      source: {
        name: sourceInfo.name,
        bytes: bytes.length,
        splats: sourceInfo.count,
        sha256: await sha256(bytes),
      },
    });
    const base = sourceInfo.name.replace(/\.[^.]+$/, "");
    downloadBlob(new Blob([JSON.stringify(scene, null, 2)], { type: "application/json" }),
      `${base}.dlscene.json`);
    status("Scene saved.");
  } catch (err) { fail(err); }
};

$("scene-load").onclick = () => $("scene-input").click();
$("scene-input").onchange = (e) => {
  const f = e.target.files[0];
  if (f) loadSceneFile(f);
  e.target.value = "";
};

async function loadSceneFile(file) {
  try {
    const data = JSON.parse(await file.text());
    if (!viewer.mesh) {
      status(`Load ${data.source?.name || "the splat file"} first, then this scene.`, 6000);
      return;
    }
    const d = applyScene(data, { viewer, tools });
    if (data.source?.sha256 && sourceInfo) {
      const bytes = await currentBytes();
      if (await sha256(bytes) !== data.source.sha256)
        status(`⚠ Scene was saved against a different ${data.source.name}.`, 6000);
    }
    Object.assign(display, {
      detail: sceneDetail(d),
      blur: d.blur ?? display.blur,
      viewColour: d.viewColour ?? true,
      // normalize handles scenes saved with the old plane shape {axis, t, flip}
      section: data.section ? Viewer.normalizeSection(data.section) : display.section,
    });
    $("blur").value = display.blur;
    $("blur-val").textContent = display.blur.toFixed(2);
    viewer.setBlur(display.blur);
    viewer.setViewColour(display.viewColour);
    syncSectionUi();
    applySection();
    onSceneChanged();
    status("Scene restored.");
  } catch (err) { fail(err); }
}

/* --------------------------------------------------------------------- loop */

setInterval(() => {
  $("fps").textContent = viewer.mesh ? `${viewer.fps} fps` : "—";
}, 500);

onSceneChanged();
