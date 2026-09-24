/* UI wiring. Everything visual lives in viewer.js / tools.js; this file only
 * connects the sidebar to them and keeps the panels in sync. */
import { Viewer } from "./viewer.js";
import { Tools } from "./tools.js";
import {
  buildScene, applyScene, downloadBlob, exportWebScene, sha256,
} from "./scene.js";

const $ = (id) => document.getElementById(id);
const BUILD = "2026-09-08";

const viewer = new Viewer($("canvas"), $("overlay"));
const tools = new Tools(viewer, $("overlay"));

/** The file currently loaded, re-readable for hashing and export. */
let source = null;   // { file } | { url, name }
let sourceInfo = null;

const freshSection = () =>
  ({ enabled: false, min: { x: 0, y: 0, z: 0 }, max: { x: 1, y: 1, z: 1 } });

const display = {
  lodScale: 1,
  blur: 0.3,
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

async function loadSource(next, label, fromGenerated = null) {
  generatedName = fromGenerated;
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
    tools.scaleFactor = 1;
    display.section = freshSection();
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
                    "panel-section", "panel-notes", "panel-views", "panel-scene"]) {
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
    group.parentElement.hidden = subs.every((el) => el.hidden);
  }
  badge("b-import", generatedName ? generatedName : (sourceInfo ? sourceInfo.name : ""));
  badge("b-display", viewer.mesh ? `${tools.viewpoints.length || ""}` : "");
  badge("b-measure", tools.calibrated
    ? `1 unit = ${tools.scaleFactor.toFixed(2)} m` : (viewer.mesh ? "no scale" : ""));
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
    const del = document.createElement("button");
    del.className = "link";
    del.textContent = "×";
    del.onclick = () => tools.remove(n.id);
    li.append(name, del);
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

  $("scale-state").textContent = tools.calibrated
    ? `1 scene unit = ${tools.scaleFactor.toFixed(4)} m — measurements are in metres.`
    : "Not calibrated. A splat scene has no built-in scale, so measurements "
      + "show as “units” until you set one.";
  $("assume-metric").disabled = tools.calibrated && tools.scaleFactor === 1;

  for (const [id, kind] of [["tool-dist", "distance"], ["tool-height", "height"],
                            ["tool-area", "area"], ["tool-note", "note"],
                            ["calibrate-btn", "calibrate"]]) {
    $(id).classList.toggle("on", tools.active === kind);
  }
}
tools.onChange = refreshLists;

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

$("demo-btn").onclick = () => loadSource({ url: "data/demo.ply", name: "demo.ply" }, "demo");
$("calib-btn").onclick = () =>
  loadSource({ url: "data/calibration.ply", name: "calibration.ply" }, "calibration scene");

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

$("lod-scale").oninput = (e) => {
  display.lodScale = parseFloat(e.target.value);
  viewer.setLodScale(display.lodScale);
  $("lod-val").textContent = `${display.lodScale.toFixed(2)}×`;
};
$("blur").oninput = (e) => {
  display.blur = parseFloat(e.target.value);
  viewer.setBlur(display.blur);
  $("blur-val").textContent = display.blur.toFixed(2);
};
$("flip-btn").classList.add("on");
$("flip-btn").onclick = () => {
  viewer.setFlip(!viewer.flip);
  $("flip-btn").classList.toggle("on", viewer.flip);
  applySection();
};
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
  cameraView = !!on && !!cams;
  $("gizmo-camera").classList.toggle("on", cameraView);
  $("gizmo-frames").hidden = !cameraView;
  if (cameraView) {
    const slider = $("gizmo-frame");
    slider.max = cams.length - 1;
    slider.value = viewer.captureIndex || 0;
    viewer.goToCaptureCamera(Number(slider.value), true);
    labelFrame();
  }
}

function labelFrame() {
  const cams = viewer.captureCameras;
  if (!cams) return;
  const i = Number($("gizmo-frame").value);
  $("gizmo-frame-label").textContent = `frame ${i + 1} / ${cams.length}`;
}

function syncCameraButton() {
  const has = !!viewer.captureCameras;
  $("gizmo-camera").disabled = !has;
  $("gizmo-camera").title = has
    ? "Stand where the camera stood"
    : "Only for a scene made here — it needs the capture's camera path";
  if (!has) setCameraView(false);
}

$("gizmo-camera").onclick = () => setCameraView(!cameraView);
$("gizmo-frame").oninput = () => {
  labelFrame();
  viewer.stopWalk();
  viewer.goToCaptureCamera(Number($("gizmo-frame").value), false);
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
viewer.onFrame = () => { toolsOnFrame?.(); drawGizmo(); };

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
  let rec = null;
  if (generatedName) {
    try {
      rec = await (await fetch(
        `/output/${encodeURIComponent(generatedName)}/capture.json`)).json();
      const stride = rec?.settings?.stride;
      if (rec?.fps > 0 && stride > 0) {
        walkRealtimeFps = Math.max(1, Math.min(30, Math.round(rec.fps / stride)));
      }
    } catch { /* no record, or a scene not made here: keep the defaults */ }
  }
  if (walkRealtimeFps) $("walk-speed").value = walkRealtimeFps;
  labelWalkSpeed();

  /* A single-photo scene is built from METRIC depth, so it really does open
   * already scaled -- the depth tool's own last line tells you to press
   * "Scene is already in metres". Pressing it for you is the honest version of
   * that instruction, and it is what makes the panel's claim true. Only ever
   * from the record's own `metric` flag, never guessed. */
  if (rec?.metric === true && !tools.calibrated) {
    // a photo scene is metric at 1 unit = 1 m; a capture carries whatever was
    // measured on site, recorded as scale_m_per_unit
    const f = Number(rec.scale_m_per_unit) > 0 ? Number(rec.scale_m_per_unit) : 1;
    tools.setScale(f);
    status(f === 1
      ? "Depth is metric — this scene is already in metres."
      : `Calibrated from ${rec.scale_note || "a measured dimension"}: `
        + `1 unit = ${f.toFixed(3)} m.`, 7000);
  }
}

$("walk-speed").oninput = () => {
  labelWalkSpeed();
  viewer.setWalkFps(Number($("walk-speed").value));
};
$("walk-loop").onchange = (e) => { if (viewer._walk) viewer._walk.loop = e.target.checked; };
$("level-horizon").onchange = (e) => { viewer.setLevel(e.target.checked); };
/* Prev/Next in the sidebar and the scrubber in the corner are the same control
 * in two places, so each moves the other. */
function syncFrameSlider() {
  if (!cameraView || !viewer.captureCameras) return;
  $("gizmo-frame").value = viewer.captureIndex || 0;
  labelFrame();
}

function stepCapture(delta) {
  if (!viewer.captureCameras) return;
  viewer.goToCaptureCamera(viewer.captureIndex + delta, true);
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
      lodScale: d.lodScale ?? display.lodScale,
      blur: d.blur ?? display.blur,
      // normalize handles scenes saved with the old plane shape {axis, t, flip}
      section: data.section ? Viewer.normalizeSection(data.section) : display.section,
    });
    $("lod-scale").value = display.lodScale;
    $("lod-val").textContent = `${display.lodScale.toFixed(2)}×`;
    $("blur").value = display.blur;
    $("blur-val").textContent = display.blur.toFixed(2);
    viewer.setLodScale(display.lodScale);
    viewer.setBlur(display.blur);
    syncSectionUi();
    applySection();
    onSceneChanged();
    status("Scene restored.");
  } catch (err) { fail(err); }
}

$("scene-export").onclick = async () => {
  if (!source) return;
  const title = prompt("Title for the exported scene:",
    sourceInfo.name.replace(/\.[^.]+$/, "")) ;
  if (title === null) return;
  try {
    status("Building bundle…", 0);
    const bytes = await currentBytes();
    const scene = buildScene({
      viewer, tools, display,
      source: {
        name: sourceInfo.name, bytes: bytes.length,
        splats: sourceInfo.count, sha256: await sha256(bytes),
      },
    });
    scene.title = title;
    const zip = await exportWebScene({
      scene, splatBytes: bytes, splatName: sourceInfo.name, title,
      onProgress: (text) => status(text, 0),
      fetchAsset: async (path) => {
        const res = await fetch(path);
        if (!res.ok) throw new Error(`Bundle asset missing: ${path}`);
        return new Uint8Array(await res.arrayBuffer());
      },
    });
    downloadBlob(zip, `${title.replace(/[^\w.-]+/g, "-")}-web-scene.zip`);
    const s = zip.splatStats;
    status(s?.compressed
      ? `Bundle exported (${(zip.size / 1e6).toFixed(1)} MB) — splat compressed `
        + `to SPZ, ${(s.originalBytes / 1e6).toFixed(0)} → `
        + `${(s.bytes / 1e6).toFixed(0)} MB `
        + `(${(100 - 100 * s.bytes / s.originalBytes).toFixed(0)}% smaller).`
      : `Bundle exported (${(zip.size / 1e6).toFixed(1)} MB).`, 8000);
  } catch (err) { fail(err); }
};

/* --------------------------------------------------------------------- loop */

setInterval(() => {
  $("fps").textContent = viewer.mesh ? `${viewer.fps} fps` : "—";
}, 500);

onSceneChanged();
