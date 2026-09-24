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
   */
  format(m) {
    const unit = this.calibrated ? "m" : "units";
    const v = m.value * (m.kind === "area" ? this.scaleFactor ** 2 : this.scaleFactor);
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
   * Metres per scene unit, from outside the viewer: a photo scene whose depth
   * was metric to begin with, or a capture whose `capture.json` records a
   * calibration someone measured on site. Same effect as calibrating by hand,
   * without asking the user to do it twice.
   */
  setScale(metresPerUnit) {
    const f = Number(metresPerUnit);
    if (!Number.isFinite(f) || f <= 0) return false;
    this.scaleFactor = f;
    this.calibrated = true;
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
    const put = (key, cls, build) => {
      live.add(key);
      let el = this._nodes.get(key);
      if (!el) {
        el = document.createElement("div");
        el.className = cls;
        this.overlay.appendChild(el);
        this._nodes.set(key, el);
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
      place(put(`${n.id}:p`, "marker", () => {}), n.point);
      place(put(`${n.id}:label`, "label note", (el) => {
        el.innerHTML = "";
        const b = document.createElement("b");
        b.textContent = n.title;
        el.appendChild(b);
        if (n.text) el.appendChild(document.createTextNode(n.text));
      }), n.point);
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
      scale: { factor: this.scaleFactor, calibrated: this.calibrated },
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
