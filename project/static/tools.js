/* Scene tools: scale calibration, measurement, annotations, viewpoints.
 *
 * All of these are just world-space points plus a bit of arithmetic; the only
 * thing they need from the render layer is Viewer.pick() to turn a click into a
 * point, and Viewer.project() to place their labels each frame.
 */
import * as THREE from "three";

let nextId = 1;
const uid = () => `${Date.now().toString(36)}-${nextId++}`;

/** Polygon area in 3D via Newell's method -- correct for any planar ring. */
export function polygonArea(points) {
  if (points.length < 3) return 0;
  const n = new THREE.Vector3();
  for (let i = 0; i < points.length; i++) {
    const a = points[i], b = points[(i + 1) % points.length];
    n.x += (a.y - b.y) * (a.z + b.z);
    n.y += (a.z - b.z) * (a.x + b.x);
    n.z += (a.x - b.x) * (a.y + b.y);
  }
  return n.length() / 2;
}

export class Tools {
  constructor(viewer, overlayEl) {
    this.viewer = viewer;
    this.overlay = overlayEl;

    this.scaleFactor = 1;      // raw scene units -> metres
    this.calibrated = false;
    this.estimated = false;    // the scale is a model's estimate, not a measurement
    this.measurements = [];
    this.notes = [];
    this.viewpoints = [];

    this.active = null;        // 'distance' | 'height' | 'area' | 'note' | 'calibrate'
    this._pending = [];        // points collected so far for the active tool

    this.onChange = null;      // UI refresh
    this.onHint = null;        // status line text (null clears)

    this._svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.overlay.appendChild(this._svg);
    this._nodes = new Map();   // key -> DOM element, reused across frames

    viewer.onPick = (p) => this._addPoint(p);
    viewer.onFrame = () => this.render();
  }

  /* ------------------------------------------------------------- tool flow */

  start(kind) {
    if (this.active === kind) return this.cancel();
    this.active = kind;
    this._pending = [];
    this._hint();
    this.onChange?.();
  }

  cancel() {
    this.active = null;
    this._pending = [];
    this.onHint?.(null);
    this.onChange?.();
  }

  _hint() {
    const need = {
      distance: "Click two points to measure a distance.",
      height: "Click two points to measure vertical height.",
      area: "Click around an area, then press Enter to close it (Esc to cancel).",
      note: "Click a point to place an annotation.",
      calibrate: "Click two points a known distance apart.",
    }[this.active];
    if (!need) return this.onHint?.(null);
    const got = this._pending.length;
    this.onHint?.(got ? `${need}  (${got} point${got > 1 ? "s" : ""} set)` : need);
  }

  _addPoint(p) {
    if (!this.active) return;
    this._pending.push(p);

    if (this.active === "note") {
      const title = prompt("Annotation title:", "Note");
      if (title) {
        const text = prompt("Annotation text (optional):", "") || "";
        this.notes.push({ id: uid(), point: p, title, text });
      }
      return this.cancel();
    }
    if (this.active === "calibrate" && this._pending.length === 2) {
      const raw = this._pending[0].distanceTo(this._pending[1]);
      const answer = prompt(
        `Those points are ${raw.toFixed(4)} scene units apart.\n` +
        "Enter the real distance in metres:", "1");
      const metres = parseFloat(answer);
      if (Number.isFinite(metres) && metres > 0 && raw > 0) {
        this.scaleFactor = metres / raw;
        this.calibrated = true;
        this.estimated = false;          // measured by hand: no longer an estimate
      }
      return this.cancel();
    }
    if ((this.active === "distance" || this.active === "height") && this._pending.length === 2) {
      this._commit();
      return;
    }
    this._hint();
    this.onChange?.();
  }

  /** Close an area polygon (Enter) or finish whatever is pending. */
  finish() {
    if (this.active === "area" && this._pending.length >= 3) this._commit();
    else this.cancel();
  }

  _commit() {
    const pts = this._pending.slice();
    const kind = this.active;
    this.measurements.push({ id: uid(), kind, points: pts, value: this._value(kind, pts) });
    this.cancel();
  }

  _value(kind, pts) {
    if (kind === "distance") return pts[0].distanceTo(pts[1]);
    if (kind === "height") return Math.abs(pts[1].y - pts[0].y);
    if (kind === "area") return polygonArea(pts);
    return 0;
  }

  /** Recompute stored values -- used after the scale factor changes. */
  refreshValues() {
    for (const m of this.measurements) m.value = this._value(m.kind, m.points);
  }

  /** Change a note's title and text: the same two questions as when placing it,
   *  answered in advance with what is there. Cancelling either keeps it as it was. */
  editNote(id) {
    const n = this.notes.find((x) => x.id === id);
    if (!n) return;
    const title = prompt("Annotation title:", n.title);
    if (title === null || !title.trim()) return;
    const text = prompt("Annotation text (optional):", n.text || "");
    if (text === null) return;
    n.title = title.trim();
    n.text = text;
    this.onChange?.();
  }

  /**
   * Move a note by dragging its pin or its label; double-click either to edit.
   * The pin follows the surface under the cursor (a pick through the renderer,
   * the same as placing it). Grabbing the label keeps the offset between cursor
   * and pin, so the note does not jump when you take hold of it. The pointer is
   * captured, so the orbit underneath never sees the drag.
   */
  _makeNoteHandle(el, id) {
    el.title = "Drag to move · double-click to edit";
    el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      const n = this.notes.find((x) => x.id === id);
      if (!n) return;
      e.preventDefault();
      e.stopPropagation();
      el.setPointerCapture(e.pointerId);
      const s = this.viewer.project(n.point);
      const r = this.viewer.canvas.getBoundingClientRect();
      this._drag = { id, el, moved: false, x0: e.clientX, y0: e.clientY,
                     dx: e.clientX - (r.left + s.x), dy: e.clientY - (r.top + s.y) };
      el.classList.add("dragging");
    });
    el.addEventListener("pointermove", (e) => {
      const d = this._drag;
      if (!d || d.id !== id) return;
      if (!d.moved && Math.hypot(e.clientX - d.x0, e.clientY - d.y0) < 4) return;
      d.moved = true;
      const hit = this.viewer.pick(e.clientX - d.dx, e.clientY - d.dy);
      const n = this.notes.find((x) => x.id === id);
      if (hit && n) n.point.copy(hit);     // off the scene: the pin waits where it was
    });
    const end = () => {
      const d = this._drag;
      if (!d || d.id !== id) return;
      d.el.classList.remove("dragging");
      this._drag = null;
      if (d.moved) this.onChange?.();
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("dblclick", (e) => { e.stopPropagation(); this.editNote(id); });
  }

  remove(id) {
    this.measurements = this.measurements.filter((m) => m.id !== id);
    this.notes = this.notes.filter((n) => n.id !== id);
    this.viewpoints = this.viewpoints.filter((v) => v.id !== id);
    this.onChange?.();
  }

  /* ------------------------------------------------------------ formatting */

  /**
   * A splat scene reconstructed from photographs carries no metric scale --
   * structure-from-motion solves the geometry only up to an unknown factor. So
   * until the scene is calibrated the numbers are honest but unitless, and are
   * labelled "units" rather than pretending to be metres.
   *
   * An ESTIMATED scale -- a single photograph's depth model -- reads "≈ … m est."
   * everywhere a number appears, stills included, and with fewer digits: on a
   * calibrated capture both depth models were measured 5-12x too deep
   * (output\research\DEPTH SMALL VS BASE - RESULTS - 002.md), so millimetres
   * would be false precision.
   */
  format(m) {
    const unit = this.calibrated ? "m" : "units";
    const v = m.value * (m.kind === "area" ? this.scaleFactor ** 2 : this.scaleFactor);
    if (this.calibrated && this.estimated) {
      return m.kind === "area" ? `≈ ${v.toPrecision(2)} m² est.` : `≈ ${v.toPrecision(2)} m est.`;
    }
    if (m.kind === "area") return `${v.toFixed(2)} ${unit}²`;
    return `${v.toFixed(v < 10 ? 3 : 2)} ${unit}`;
  }

  /**
   * Treat the scene's own units as metres. Correct whenever the capture already
   * came out metric -- a georeferenced drone set, an ARKit/LiDAR phone scan, or
   * anything exported with a known scale -- and for the bundled test scenes,
   * which are authored 1 unit = 1 m.
   */
  assumeMetric() {
    this.setScale(1);
  }

  /**
   * Metres per scene unit, from outside the viewer: a capture whose
   * `capture.json` records a calibration someone measured on site (same effect
   * as calibrating by hand, without asking twice), or -- with `estimated` -- a
   * photo scene's depth-model scale, which the user has to ask for and which is
   * then labelled as an estimate wherever it shows.
   */
  setScale(metresPerUnit, { estimated = false } = {}) {
    const f = Number(metresPerUnit);
    if (!Number.isFinite(f) || f <= 0) return false;
    this.scaleFactor = f;
    this.calibrated = true;
    this.estimated = !!estimated;
    this.onChange?.();
    return true;
  }

  /* --------------------------------------------------------------- markers */

  addViewpoint(name) {
    this.viewpoints.push({
      id: uid(),
      name: name || `View ${this.viewpoints.length + 1}`,
      camera: this.viewer.getCameraState(),
    });
    this.onChange?.();
  }

  goToViewpoint(id) {
    const v = this.viewpoints.find((x) => x.id === id);
    if (v) this.viewer.setCameraState(v.camera, true);
  }

  /* ---------------------------------------------------------------- render */

  render() {
    const live = new Set();
    const put = (key, cls, build, init) => {
      live.add(key);
      let el = this._nodes.get(key);
      if (!el) {
        el = document.createElement("div");
        el.className = cls;
        this.overlay.appendChild(el);
        this._nodes.set(key, el);
        init?.(el);                        // once, when the element is made
      }
      build(el);
      return el;
    };
    const place = (el, world) => {
      const s = this.viewer.project(world);
      el.style.display = s.visible ? "" : "none";
      el.style.left = `${s.x}px`;
      el.style.top = `${s.y}px`;
      return s;
    };

    const polys = [];

    const drawPoints = (key, pts) => {
      pts.forEach((p, i) => place(put(`${key}:p${i}`, "marker", () => {}), p));
    };

    for (const m of this.measurements) {
      drawPoints(m.id, m.points);
      const mid = m.points
        .reduce((a, p) => a.add(p), new THREE.Vector3())
        .divideScalar(m.points.length);
      place(put(`${m.id}:label`, "label", (el) => { el.textContent = this.format(m); }), mid);
      polys.push({ pts: m.points, closed: m.kind === "area" });
    }

    for (const n of this.notes) {
      const handle = (el) => this._makeNoteHandle(el, n.id);
      place(put(`${n.id}:p`, "marker note-pin", () => {}, handle), n.point);
      place(put(`${n.id}:label`, "label note", (el) => {
        // rebuilt only when the text changes, so a drag is not interrupted
        const sig = `${n.title}\u0000${n.text || ""}`;
        if (el.dataset.sig === sig) return;
        el.dataset.sig = sig;
        el.innerHTML = "";
        const b = document.createElement("b");
        b.textContent = n.title;
        el.appendChild(b);
        if (n.text) el.appendChild(document.createTextNode(n.text));
      }, handle), n.point);
    }

    if (this._pending.length) {
      drawPoints("pending", this._pending);
      if (this._pending.length > 1) polys.push({ pts: this._pending, closed: false });
    }

    for (const [key, el] of this._nodes) {
      if (!live.has(key)) { el.remove(); this._nodes.delete(key); }
    }
    this._drawLines(polys);
  }

  _drawLines(polys) {
    const svg = this._svg;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    for (const { pts, closed } of polys) {
      if (pts.length < 2) continue;
      const coords = pts.map((p) => {
        const s = this.viewer.project(p);
        return `${s.x},${s.y}`;
      }).join(" ");
      const el = document.createElementNS(
        "http://www.w3.org/2000/svg", closed ? "polygon" : "polyline");
      el.setAttribute("points", coords);
      if (!closed) el.setAttribute("fill", "none");
      svg.appendChild(el);
    }
  }

  /* ------------------------------------------------------------ serialising */

  toJSON() {
    return {
      scale: { factor: this.scaleFactor, calibrated: this.calibrated,
               estimated: this.estimated },
      measurements: this.measurements.map((m) => ({
        id: m.id, kind: m.kind, points: m.points.map((p) => p.toArray()),
      })),
      notes: this.notes.map((n) => ({
        id: n.id, point: n.point.toArray(), title: n.title, text: n.text,
      })),
      viewpoints: this.viewpoints.map((v) => ({ id: v.id, name: v.name, camera: v.camera })),
    };
  }

  fromJSON(data) {
    if (!data) return;
    this.scaleFactor = data.scale?.factor ?? 1;
    this.calibrated = !!data.scale?.calibrated;
    this.estimated = !!data.scale?.estimated;
    this.measurements = (data.measurements || []).map((m) => {
      const points = m.points.map((a) => new THREE.Vector3().fromArray(a));
      return { id: m.id || uid(), kind: m.kind, points, value: this._value(m.kind, points) };
    });
    this.notes = (data.notes || []).map((n) => ({
      id: n.id || uid(), point: new THREE.Vector3().fromArray(n.point),
      title: n.title, text: n.text || "",
    }));
    this.viewpoints = (data.viewpoints || []).map((v) => ({
      id: v.id || uid(), name: v.name, camera: v.camera,
    }));
    this.onChange?.();
  }
}
