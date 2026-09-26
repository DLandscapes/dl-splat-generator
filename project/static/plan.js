/* The plan view: the scene seen from straight above, with the capture's
 * camera path and where you stand now.
 *
 * WHAT IT SHOWS
 *   the area   the splats up to eye level dropped onto the ground plane; each
 *              cell takes the colour of its highest one, so it reads like a
 *              rough site plan of what the scan covers (see build() for why it
 *              stops at eye level)
 *   the path   the capture cameras in the order shot: start dot, end arrow, the
 *              current capture position ringed
 *   the view   where the viewer's camera is, and a wedge for its field of view
 *
 * WHICH WAY IS UP. "Above" is the scene's own vertical -- the capture cameras'
 * mean up, as levelling uses. A solve has no compass, and a phone's single GPS
 * fix places a capture but cannot turn it, so by default the plan is turned so
 * the walk heads up the page, and says it is not north-up. Once north has been
 * SET (Display -> Sun and north: a photo's compass, a shadow, or by hand) the
 * plan is north-up, with an N and the sun's direction at the moment of
 * capture. Clicking the plan jumps to the nearest capture position.
 *
 * Built once per scene (one pass over the splats), drawn each frame only when
 * the camera or the capture position has changed.
 */
import * as THREE from "three";

const SIZE = 170;              // CSS px, square: fills the 184 px column inside its padding
const GRID = 100;              // raster cells per side
const MAX_SAMPLES = 400_000;   // splats read for the raster; more adds nothing at 100 x 100

export class PlanView {
  constructor(viewer, root) {
    this.viewer = viewer;
    this.root = root;
    this.canvas = root.querySelector("canvas");
    this.note = root.querySelector(".plan-note");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = this.canvas.height = Math.round(SIZE * dpr);
    this.canvas.style.width = this.canvas.style.height = `${SIZE}px`;
    this.dpr = dpr;
    this.raster = null;        // offscreen canvas with the area
    this.frame = null;         // { e1, e2, up, u0, v0, span }
    this.path = null;          // plan coords of the capture cameras
    this.scale = null;         // { f, estimated } metres per unit, when calibrated
    this.onPick = null;        // (captureIndex) => void
    this.north = null;         // world-space north, once set -> the plan is north-up
    this.toSun = null;         // world-space direction to the sun, when it is up
    this._sig = "";
    this.canvas.addEventListener("click", (e) => this._click(e));
  }

  /** Metres per scene unit (or null), for the scale bar. */
  setScale(f, estimated = false) { this.scale = f ? { f, estimated } : null; this._sig = ""; }

  /**
   * North and the sun, world-space unit vectors or null. A new north turns the
   * page, so it needs a build(); the sun only a redraw.
   */
  setSun(north, toSun) {
    this.north = north ? new THREE.Vector3().fromArray(north) : null;
    this.toSun = toSun ? new THREE.Vector3().fromArray(toSun) : null;
    this._sig = "";
  }

  /* ------------------------------------------------------------ building */

  build() {
    const v = this.viewer, mesh = v.mesh;
    this.raster = this.frame = this.path = null;
    this._sig = "";
    if (!mesh?.packedSplats) return;
    mesh.updateMatrixWorld(true);
    const M = mesh.matrixWorld;

    // plan axes: up = the scene's vertical; "north" on the page = the way the
    // walk heads (first to last camera), else the photo's view direction
    const up = (v._up ? v._up.clone() : new THREE.Vector3(0, 1, 0)).normalize();
    const cams = v.captureCameras;
    let fwd = new THREE.Vector3();
    if (this.north) {
      fwd.copy(this.north);                            // north-up, once north is known
    } else if (cams && cams.length > 1) {
      fwd.fromArray(cams[cams.length - 1].position).sub(new THREE.Vector3().fromArray(cams[0].position));
      if (fwd.lengthSq() < 1e-12) fwd.fromArray(cams[0].direction);
    } else {
      fwd.set(0, 0, 1).transformDirection(M);          // a photo looks down its +Z
    }
    fwd.addScaledVector(up, -fwd.dot(up));
    if (fwd.lengthSq() < 1e-12) fwd.set(1, 0, 0).addScaledVector(up, -up.x);
    const e2 = fwd.normalize();
    const e1 = new THREE.Vector3().crossVectors(e2, up).normalize();

    // one pass over (a sample of) the splats, in world space
    const n = v.splatCount || mesh.packedSplats.numSplats || 0;
    const step = Math.max(1, Math.ceil(n / MAX_SAMPLES));
    const U = [], V = [], H = [], C = [], S = [];
    const p = new THREE.Vector3();
    mesh.packedSplats.forEachSplat((i, c, s, q, opacity, col) => {
      // faint splats are the trainer's haze, not a surface
      if (i % step || opacity < 0.5) return;
      p.set(c.x, c.y, c.z).applyMatrix4(M);
      U.push(p.dot(e1)); V.push(p.dot(e2)); H.push(p.dot(up));
      S.push(Math.max(s.x, s.y, s.z));
      C.push(col.r, col.g, col.b);
    });
    if (U.length < 10) return;

    // "Highest splat wins" must not let the sky win. Tried first: dropping the
    // biggest 10 % and the top 3 % by height -- the canopy still read as white
    // speckle, because a walk's sky splats sit at every height. So the plan is
    // cut at EYE LEVEL: nothing above the cameras' height plus half their
    // height over the ground. It shows the ground, paths and low growth -- a
    // site plan, not an aerial -- and a cell needs 3 splats to count.
    const pct = (a, q) => { const s = Float32Array.from(a).sort(); return s[Math.floor(q * (s.length - 1))]; };
    const maxSize = pct(S, 0.9);
    const camH = [];
    if (cams) for (const cam of cams) camH.push(p.fromArray(cam.position).dot(up));
    else camH.push(p.set(0, 0, 0).applyMatrix4(M).dot(up));
    const eye = pct(camH, 0.5), ground = pct(H, 0.1);
    const maxHeight = eye + 0.5 * Math.max(eye - ground, 0);

    // extent: the middle 90 % of the splats, plus every camera, padded; the
    // far background (seen, but only from far) stays off the page
    let u0 = pct(U, 0.05), u1 = pct(U, 0.95), v0 = pct(V, 0.05), v1 = pct(V, 0.95);
    const path = [];
    if (cams) {
      for (const cam of cams) {
        p.fromArray(cam.position);
        const cu = p.dot(e1), cv = p.dot(e2);
        path.push([cu, cv]);
        u0 = Math.min(u0, cu); u1 = Math.max(u1, cu); v0 = Math.min(v0, cv); v1 = Math.max(v1, cv);
      }
    }
    this.photoAt = null;
    if (!cams && v.photoCamera) {
      p.set(0, 0, 0).applyMatrix4(M);                   // the photo's camera
      const cu = p.dot(e1), cv = p.dot(e2);
      this.photoAt = [cu, cv];
      const look = new THREE.Vector3(0, 0, 1).transformDirection(M);
      this.photoLook = Math.atan2(-look.dot(e2), look.dot(e1));   // page angle it looks along
      u0 = Math.min(u0, cu); u1 = Math.max(u1, cu); v0 = Math.min(v0, cv); v1 = Math.max(v1, cv);
    }
    const span = Math.max(u1 - u0, v1 - v0) * 1.08 || 1;
    const cu = (u0 + u1) / 2, cv = (v0 + v1) / 2;
    this.frame = { e1, e2, up, u0: cu - span / 2, v0: cv - span / 2, span };

    // raster: each cell keeps the colour of its highest splat
    const top = new Float32Array(GRID * GRID).fill(-Infinity);
    const count = new Uint16Array(GRID * GRID);
    const rgb = new Float32Array(GRID * GRID * 3);
    for (let i = 0; i < U.length; i++) {
      if (S[i] > maxSize || H[i] > maxHeight) continue;
      const gx = Math.floor((U[i] - this.frame.u0) / span * GRID);
      const gy = Math.floor((V[i] - this.frame.v0) / span * GRID);
      if (gx < 0 || gy < 0 || gx >= GRID || gy >= GRID) continue;
      const cell = (GRID - 1 - gy) * GRID + gx;             // page y runs down
      if (count[cell] < 65535) count[cell]++;
      if (H[i] > top[cell]) {
        top[cell] = H[i];
        rgb[3 * cell] = C[3 * i]; rgb[3 * cell + 1] = C[3 * i + 1]; rgb[3 * cell + 2] = C[3 * i + 2];
      }
    }
    const img = new ImageData(GRID, GRID);
    for (let cell = 0; cell < GRID * GRID; cell++) {
      if (count[cell] < 3) continue;
      img.data[4 * cell] = Math.min(255, rgb[3 * cell] * 255);
      img.data[4 * cell + 1] = Math.min(255, rgb[3 * cell + 1] * 255);
      img.data[4 * cell + 2] = Math.min(255, rgb[3 * cell + 2] * 255);
      img.data[4 * cell + 3] = 235;
    }
    this.raster = document.createElement("canvas");
    this.raster.width = this.raster.height = GRID;
    this.raster.getContext("2d").putImageData(img, 0, 0);
    this.path = path.length ? path : null;
    // A splat dropped in on its own brings no cameras.json: nothing says which
    // way is up, so "above" is only the file's own axis -- say that, rather
    // than calling it a photo (a screenshot of student capture A's .spz,
    // 2026-09-26: a video scene, labelled as a photo, lying on its side).
    this.note.textContent = this.north ? "From above · north-up"
      : cams ? "From above · walk heads up the page · not north-up"
      : v.photoCamera ? "From above · the photo looks up the page · not north-up"
      : "The file's own axes · no camera positions came with it, so not levelled";
  }

  /* ------------------------------------------------------------- drawing */

  _toPage(u, v) {
    const f = this.frame, k = SIZE / f.span;
    return [(u - f.u0) * k, SIZE - (v - f.v0) * k];
  }

  draw() {
    const v = this.viewer;
    if (this.root.hidden || !this.frame) return;
    const cam = v.camera.position, dir = v.camera.getWorldDirection(new THREE.Vector3());
    const sig = [cam.x, cam.y, cam.z, dir.x, dir.z, v.camera.fov, v.captureIndex,
                 this.scale?.f, this.toSun?.x, this.toSun?.z].map((x) => (x ?? 0).toFixed?.(3) ?? x).join();
    if (sig === this._sig) return;
    this._sig = sig;

    const ctx = this.canvas.getContext("2d");
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue("--on-stage").trim() || "#fdfcf9";
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, SIZE, SIZE);
    ctx.imageSmoothingEnabled = true;            // a soft plan, not a pixel grid
    if (this.raster) ctx.drawImage(this.raster, 0, 0, SIZE, SIZE);

    // a photo scene: where the photo was taken, and what it saw, with the
    // photo's own field of view -- up the page, or its bearing when north-up
    if (this.photoAt && v.photoCamera) {
      const [x, y] = this._toPage(...this.photoAt);
      const half = v.photoCamera.hfov * Math.PI / 360, len = 34, a = this.photoLook ?? -Math.PI / 2;
      ctx.strokeStyle = ink; ctx.fillStyle = "rgba(253,252,249,.18)"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x, y);
      ctx.lineTo(x + len * Math.cos(a - half), y + len * Math.sin(a - half));
      ctx.lineTo(x + len * Math.cos(a + half), y + len * Math.sin(a + half));
      ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.fillStyle = ink; ctx.beginPath(); ctx.arc(x, y, 3, 0, 2 * Math.PI); ctx.fill();
    }

    // the capture path, start dot, end arrow, current position ringed
    if (this.path) {
      ctx.strokeStyle = ink; ctx.lineWidth = 1.5; ctx.lineJoin = "round";
      ctx.shadowColor = "rgba(0,0,0,.6)"; ctx.shadowBlur = 2;
      ctx.beginPath();
      this.path.forEach(([pu, pv], i) => {
        const [x, y] = this._toPage(pu, pv);
        if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      });
      ctx.stroke();
      const [sx, sy] = this._toPage(...this.path[0]);
      ctx.fillStyle = ink; ctx.beginPath(); ctx.arc(sx, sy, 3, 0, 2 * Math.PI); ctx.fill();
      const cur = this.path[Math.min(v.captureIndex || 0, this.path.length - 1)];
      const [ix, iy] = this._toPage(...cur);
      ctx.beginPath(); ctx.arc(ix, iy, 5, 0, 2 * Math.PI); ctx.stroke();
      ctx.shadowBlur = 0;
    }

    // where the viewer stands, and a wedge for what it sees
    const f = this.frame;
    const pu = cam.dot(f.e1), pv = cam.dot(f.e2);
    const du = dir.dot(f.e1), dv = dir.dot(f.e2);
    const [x, y] = this._toPage(pu, pv);
    if (x > -20 && y > -20 && x < SIZE + 20 && y < SIZE + 20) {
      const half = Math.atan(Math.tan(v.camera.fov * Math.PI / 360) * v.camera.aspect);
      const a = Math.atan2(-dv, du), len = 26;
      ctx.fillStyle = "rgba(217,195,154,.35)"; ctx.strokeStyle = "rgba(217,195,154,.9)";
      ctx.beginPath(); ctx.moveTo(x, y);
      ctx.lineTo(x + len * Math.cos(a - half), y + len * Math.sin(a - half));
      ctx.lineTo(x + len * Math.cos(a + half), y + len * Math.sin(a + half));
      ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#d9c39a"; ctx.beginPath(); ctx.arc(x, y, 3, 0, 2 * Math.PI); ctx.fill();
    }

    // a scale bar, when the scene has a scale
    if (this.scale) {
      const metresAcross = f.span * this.scale.f;
      const nice = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
      const m = nice.find((q) => q >= metresAcross / 5) || nice[nice.length - 1];
      const px = m / metresAcross * SIZE;
      ctx.fillStyle = ink; ctx.fillRect(8, SIZE - 12, px, 2);
      ctx.font = "10px sans-serif";
      ctx.fillText(`${this.scale.estimated ? "≈ " : ""}${m} m`, 8, SIZE - 16);
    }

    // north-up: an N at the top right; the sun as a disc on the rim, in the
    // direction it stood, with a ray towards the middle (shadows fall the
    // other way)
    if (this.north) {
      ctx.fillStyle = ink; ctx.strokeStyle = ink; ctx.lineWidth = 1.5;
      ctx.shadowColor = "rgba(0,0,0,.6)"; ctx.shadowBlur = 2;
      ctx.beginPath(); ctx.moveTo(SIZE - 12, 8); ctx.lineTo(SIZE - 16, 18); ctx.lineTo(SIZE - 8, 18);
      ctx.closePath(); ctx.fill();
      ctx.font = "bold 10px sans-serif"; ctx.textAlign = "center";
      ctx.fillText("N", SIZE - 12, 30);
      ctx.textAlign = "start";
      if (this.toSun) {
        const du = this.toSun.dot(f.e1), dv = this.toSun.dot(f.e2);
        const a = Math.atan2(-dv, du), c = SIZE / 2, rim = SIZE / 2 - 9;
        const sx = c + rim * Math.cos(a), sy = c + rim * Math.sin(a);
        ctx.strokeStyle = "rgba(217,195,154,.9)"; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(c, c); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "#d9c39a";
        ctx.beginPath(); ctx.arc(sx, sy, 5, 0, 2 * Math.PI); ctx.fill();
      }
      ctx.shadowBlur = 0;
    }
  }

  /** Jump to the capture position nearest the click. */
  _click(e) {
    if (!this.path || !this.onPick) return;
    const r = this.canvas.getBoundingClientRect();
    const x = (e.clientX - r.left) * SIZE / r.width, y = (e.clientY - r.top) * SIZE / r.height;
    let best = -1, bestD = Infinity;
    this.path.forEach((pt, i) => {
      const [px, py] = this._toPage(...pt);
      const d = (px - x) ** 2 + (py - y) ** 2;
      if (d < bestD) { bestD = d; best = i; }
    });
    if (best >= 0 && bestD < 30 * 30) this.onPick(best);
  }
}
