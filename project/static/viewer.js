/* Render layer.
 *
 * Spark (MIT, World Labs) does the splat rendering: full spherical harmonics,
 * anti-aliasing and level-of-detail streaming for multi-million-splat captures.
 * Everything the rest of the app touches -- camera, picking, sectioning,
 * projection -- lives here, so the render core stays swappable.
 */
import * as THREE from "three";
import {
  SparkRenderer, SplatMesh, SplatEdit, SplatEditSdf,
  SplatEditSdfType, SplatEditRgbaBlendMode,
} from "@sparkjsdev/spark";
import { parsePlyHeader, isGaussianPly, parsePointCloud } from "./ply.js";

const FOV_Y = 60;
const WORLD_UP = new THREE.Vector3(0, 1, 0);
const SPLAT_EXTS = ["ply", "spz", "splat", "ksplat", "sog", "zip"];

// Above this many splats it is worth spending a moment building a level-of-detail
// tree so navigation stays fluid. Below it we render every splat: LOD subsamples,
// and on a small scene that costs picking accuracy for no visible gain.
const LOD_THRESHOLD = 1_000_000;

// In the photo view the photo's rectangle fills this share of the viewport
// along whichever side limits it, so its outline stays visible as a frame.
const PHOTO_FILL = 0.92;

export class Viewer {
  constructor(canvas, overlayEl) {
    this.canvas = canvas;
    this.overlay = overlayEl;

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x14161a, 1);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(FOV_Y, 1, 0.02, 5000);

    this.spark = new SparkRenderer({
      renderer: this.renderer,
      enableLod: true,
      // generous raycast budget: picking accuracy drives every measurement
      lodRaycast: 200000,
      blurAmount: 0.3,
    });
    this.scene.add(this.spark);

    this.mesh = null;
    this.flip = true;
    this.splatCount = 0;
    this.hasLod = false;       // true once a level-of-detail tree exists for the scene
    this.fileType = null;

    this._edit = null;
    this._sdf = null;
    this._sectionHelper = null;
    this._raycaster = new THREE.Raycaster();
    this._cam = { yaw: 0.6, pitch: -0.32, dist: 6, target: new THREE.Vector3() };
    this.home = null;
    this._anim = null;

    /* Levelling. A reconstruction's axes are arbitrary: nothing in the solve
     * ties them to gravity, so a scene routinely sits a few degrees off and the
     * horizon leans while you orbit. The capture cameras know which way was up
     * (COLMAP's camera Y points down), so their average up vector is the best
     * gravity estimate available. It is applied to the ORBIT FRAME, never to
     * the mesh: Spark evaluates section SDFs in the mesh's own frame, and the
     * only reason that works is that our single mesh transform (the 180 degree
     * flip) is its own inverse. Rotating the mesh would silently break the
     * section box. */
    this.level = true;
    this._up = new THREE.Vector3(0, 1, 0);
    this._upQuat = new THREE.Quaternion();
    this._upQuatInv = new THREE.Quaternion();
    this.upTiltDeg = 0;
    this.upFromCameras = false;

    this._walk = null;
    this.onFrame = null;
    this.onPick = null;

    /* The photo view. A scene made from ONE photograph was unprojected from a
     * single camera at the origin, looking down its +Z, with a known
     * horizontal field of view -- so "where the photo was taken" is exact, not
     * estimated. The view stands there with the photo's own field of view and
     * outlines the photo's rectangle; any drag or zoom leaves it. */
    this.photoCamera = null;   // { hfov: degrees, aspect: width / height }
    this.photoView = false;
    this._lastNow = 0;
    if (overlayEl?.parentElement) {
      const mask = document.createElement("div");
      mask.className = "photo-mask";
      this._frameEl = document.createElement("div");
      this._frameEl.className = "photo-frame";
      this._frameEl.hidden = true;
      mask.appendChild(this._frameEl);
      overlayEl.parentElement.insertBefore(mask, overlayEl);
    }

    this._initControls();
    this._resize();
    new ResizeObserver(() => this._resize()).observe(canvas.parentElement);

    this._fpsFrames = 0;
    this._fpsTime = performance.now();
    this.fps = 0;
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  /* ---------------------------------------------------------------- loading */

  static isSplatFile(name) {
    return SPLAT_EXTS.includes((name.split(".").pop() || "").toLowerCase());
  }

  async loadFile(file) {
    const buffer = await file.arrayBuffer();
    return this.loadBuffer(buffer, file.name);
  }

  async loadUrl(url, name) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Could not fetch ${url} (HTTP ${res.status})`);
    return this.loadBuffer(await res.arrayBuffer(), name || url.split("/").pop(), url);
  }

  /** cameras.json sitting beside a scene, if the pipeline exported one. */
  async _tryLoadCaptureCameras(url) {
    try {
      const camUrl = url.replace(/[^/]*$/, "cameras.json");
      const res = await fetch(camUrl);
      if (!res.ok) return;
      const data = await res.json();
      if (data?.format !== "dlcameras" || !Array.isArray(data.cameras)) return;
      if (this.setCaptureCameras(data.cameras)) this.goToCaptureCamera(0, false);
    } catch {
      // no cameras.json beside this scene; the fitted view stands
    }
  }

  async loadBuffer(buffer, name, url = null) {
    this.clear();
    const ext = (name.split(".").pop() || "").toLowerCase();

    // Plain point-cloud PLY is the one case Spark does not target: no per-point
    // scale or rotation to build a covariance from. Parse it ourselves and push
    // uniform round splats instead.
    let pointCloud = null;
    if (ext === "ply") {
      try {
        const header = parsePlyHeader(buffer);
        if (!isGaussianPly(header)) pointCloud = parsePointCloud(buffer);
      } catch (err) {
        throw new Error(`Could not read PLY header: ${err.message}`);
      }
    }

    let mesh;
    if (pointCloud) {
      const { count, positions, colors, pointSize } = pointCloud;
      const scales = new THREE.Vector3(pointSize, pointSize, pointSize);
      const quat = new THREE.Quaternion();
      const centre = new THREE.Vector3();
      const colour = new THREE.Color();
      mesh = new SplatMesh({
        maxSplats: count,
        raycastable: true,
        editable: true,
        constructSplats: (splats) => {
          for (let i = 0; i < count; i++) {
            centre.set(positions[3 * i], positions[3 * i + 1], positions[3 * i + 2]);
            colour.setRGB(colors[3 * i], colors[3 * i + 1], colors[3 * i + 2]);
            splats.pushSplat(centre, scales, quat, 1.0, colour);
          }
        },
      });
      this.fileType = "point cloud";
    } else {
      // Note: no `lod` option here. On a file that carries no LOD tree of its
      // own (any ordinary .ply/.spz) that option loads an empty mesh; the tree
      // has to be built after loading instead, below.
      mesh = new SplatMesh({
        fileBytes: new Uint8Array(buffer),
        fileName: name,
        raycastable: true,
        editable: true,
      });
      this.fileType = ext;
    }

    await mesh.initialized;
    this.splatCount = mesh.packedSplats?.numSplats ?? mesh.splats?.getNumSplats?.() ?? 0;

    this.hasLod = false;
    if (this.splatCount >= LOD_THRESHOLD) {
      try {
        await mesh.createLodSplats();
        this.hasLod = true;
      } catch (err) {
        // LOD is an optimisation; a scene that renders every splat is still correct
        console.warn("LOD tree could not be built, rendering at full detail:", err);
      }
    }

    this.mesh = mesh;
    this.scene.add(mesh);

    this._applyFlip();
    this.bounds = this._worldBounds();
    this._robustLocal = this._robustLocalBounds();
    this.fitView();

    // If the pipeline exported capture cameras beside this file, open from one
    // of them rather than a synthetic orbit angle.
    this.captureCameras = null;
    this.captureIndex = 0;
    if (url) await this._tryLoadCaptureCameras(url);

    this.home = this.getCameraState();
    return { name, count: this.splatCount, type: this.fileType };
  }

  clear() {
    if (this._edit) { this._edit.removeFromParent(); this._edit = null; this._sdf = null; }
    if (this._sectionHelper) { this._sectionHelper.removeFromParent(); this._sectionHelper = null; }
    if (this.mesh) {
      this.scene.remove(this.mesh);
      this.mesh.dispose();
      this.mesh = null;
    }
    this.splatCount = 0;
    this.hasLod = false;
    this.photoCamera = null;
    this.photoView = false;
    this.bounds = null;
    this._robustLocal = null;
  }

  /* ---------------------------------------------------------------- framing */

  _worldBounds() {
    if (!this.mesh) return null;
    this.mesh.updateMatrixWorld(true);
    const local = this.mesh.getBoundingBox(true);
    return local.clone().applyMatrix4(this.mesh.matrixWorld);
  }

  /**
   * Bounds of the splats that actually matter, in object space.
   *
   * Real captures carry stray "floater" splats far outside the scene -- a
   * Postshot export measured here spanned 123 m by its full bounding box but
   * only 68 m of real content, which dragged the box centre ~50 m off and left
   * the scene a speck off-frame. Percentiles of the splat centres ignore those
   * outliers. Costs one pass over the splats (~0.6 s for 3M), once per load.
   */
  _robustLocalBounds(pLow = 0.02, pHigh = 0.98) {
    const n = this.splatCount;
    if (!this.mesh || !n) return null;
    const step = Math.max(1, Math.floor(n / 60000));
    const xs = [], ys = [], zs = [];
    let i = 0;
    this.mesh.forEachSplat((_index, centre) => {
      if (i++ % step) return;
      xs.push(centre.x); ys.push(centre.y); zs.push(centre.z);
    });
    if (!xs.length) return null;
    const pct = (arr, p) => { arr.sort((a, b) => a - b); return arr[Math.floor(p * (arr.length - 1))]; };
    return new THREE.Box3(
      new THREE.Vector3(pct(xs, pLow), pct(ys, pLow), pct(zs, pLow)),
      new THREE.Vector3(pct(xs, pHigh), pct(ys, pHigh), pct(zs, pHigh)),
    );
  }

  /** World-space bounds used for framing the camera. */
  frameBounds() {
    if (this._robustLocal && this.mesh)
      return this._robustLocal.clone().applyMatrix4(this.mesh.matrixWorld);
    return this.bounds;
  }

  fitView() {
    const box = this.frameBounds();
    if (!box) return;
    this.photoView = false;
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.length() * 0.5, 1e-3);
    this.sceneRadius = radius;
    this._cam.target.copy(centre);
    this._cam.dist = radius / Math.tan(THREE.MathUtils.degToRad(FOV_Y) / 2) * 1.1;
    this._cam.yaw = 0.6;
    this._cam.pitch = -0.32;
    this._updateClip();
    this._applyCamera();
  }

  /**
   * Near and far planes sized from the scene, not just the orbit distance.
   * Tying `far` to distance alone clipped away the rest of a large scan as soon
   * as you moved in close, which reads as geometry vanishing while you zoom.
   */
  _updateClip() {
    const r = this.sceneRadius || this._cam.dist;
    this.camera.near = Math.max(this._cam.dist * 1e-4, r * 1e-5, 1e-4);
    this.camera.far = this._cam.dist + r * 8;
    this.camera.updateProjectionMatrix();
  }

  resetView() {
    if (this.home) this.setCameraState(this.home, true);
    else this.fitView();
  }

  /**
   * Adopt the capture cameras exported alongside a scene (cameras.json).
   *
   * A .ply carries no camera poses, but the COLMAP model our own pipeline
   * produces has one per registered frame. Opening at a real capture position
   * matters because a splat scene only looks right from roughly where it was
   * photographed; from arbitrary angles you are looking at reconstruction
   * artefacts rather than the site.
   */
  setCaptureCameras(list) {
    this.captureCameras = Array.isArray(list) && list.length ? list : null;
    this._deriveUp();
    return this.captureCameras ? this.captureCameras.length : 0;
  }

  /**
   * Work out which way is up, from the capture cameras' own up vectors
   * (cameras.json version 2 and later). Older files have none, and a dropped
   * .ply has no cameras at all; in both cases we keep world +Y and say so,
   * rather than guessing from geometry and levelling the scene wrongly.
   */
  _deriveUp() {
    const cams = this.captureCameras;
    const ups = (cams || []).filter((c) => Array.isArray(c.up) && c.up.length === 3);
    const up = new THREE.Vector3(0, 1, 0);
    this.upFromCameras = false;
    if (ups.length) {
      const sum = new THREE.Vector3();
      for (const c of ups) sum.add(new THREE.Vector3().fromArray(c.up));
      if (sum.lengthSq() > 1e-9) {
        up.copy(sum.normalize());
        this.upFromCameras = true;
      }
    }
    this.setSceneUp(up);
  }

  /** Point the orbit frame's vertical at `v`: a Vector3 or a [x,y,z] array. */
  setSceneUp(v) {
    if (Array.isArray(v)) this._up.fromArray(v);
    else this._up.copy(v);
    this._up.normalize();
    if (!Number.isFinite(this._up.lengthSq()) || this._up.lengthSq() < 0.5) {
      this._up.set(0, 1, 0);
    }
    this._upQuat.setFromUnitVectors(new THREE.Vector3(0, 1, 0), this._up);
    this._upQuatInv.copy(this._upQuat).invert();
    this.upTiltDeg = THREE.MathUtils.radToDeg(
      Math.acos(THREE.MathUtils.clamp(this._up.y, -1, 1)));
    this._applyCamera();
  }

  /** Level the horizon (default) or orbit about plain world +Y. */
  setLevel(on) {
    this.level = !!on;
    this._applyCamera();
  }

  /** What the UI needs to explain levelling. */
  upInfo() {
    return {
      available: this.upFromCameras,
      level: this.level,
      tiltDeg: Math.round(this.upTiltDeg * 10) / 10,
      cameras: this.captureCameras ? this.captureCameras.length : 0,
    };
  }

  /** Camera state matching capture camera `i`, or null. */
  captureCameraState(i) {
    const cams = this.captureCameras;
    if (!cams || !cams.length) return null;
    const cam = cams[((i % cams.length) + cams.length) % cams.length];
    const eye = new THREE.Vector3().fromArray(cam.position);
    const dir = new THREE.Vector3().fromArray(cam.direction).normalize();

    // Put the orbit pivot where this camera was actually looking: the point on
    // its view ray nearest the scene centre, so orbiting from here behaves.
    const box = this.frameBounds();
    const centre = box ? box.getCenter(new THREE.Vector3()) : eye.clone().add(dir);
    const radius = this.sceneRadius || centre.distanceTo(eye) || 1;
    const t = THREE.MathUtils.clamp(
      centre.clone().sub(eye).dot(dir), radius * 0.1, radius * 4);
    const target = eye.clone().addScaledVector(dir, t);

    return { ...this._orbitFrom(eye, target), target: target.toArray() };
  }

  /**
   * Walk the capture path: the camera goes where the camera actually went, in
   * the order it was shot, interpolating between neighbouring frames. This is
   * the honest way to look at a splat scene -- it keeps you near the positions
   * the site was photographed from, where the reconstruction is faithful.
   */
  startWalk({ fps = 8, loop = false, from = null } = {}) {
    const cams = this.captureCameras;
    if (!cams || cams.length < 2) return false;
    this._anim = null;
    const start = from == null ? (this.captureIndex || 0) : from;
    this._walk = { pos: Math.min(start, cams.length - 1), fps, loop,
                   last: performance.now() };
    return true;
  }

  stopWalk() {
    const was = !!this._walk;
    this._walk = null;
    if (was && this.onWalk) {
      this.onWalk(this.captureIndex, this.captureCameras?.length || 0, false);
    }
  }

  get walking() { return !!this._walk; }

  setWalkFps(fps) { if (this._walk) this._walk.fps = Math.max(0.5, fps); }

  _tickWalk(now) {
    const w = this._walk;
    if (!w) return;
    const cams = this.captureCameras;
    if (!cams || cams.length < 2) { this.stopWalk(); return; }
    const last = cams.length - 1;
    // clamp the step so a background tab that wakes up does not jump the walk
    const dt = Math.min(0.25, Math.max(0, (now - w.last) / 1000));
    w.last = now;
    w.pos += dt * w.fps;
    let ended = false;
    if (w.pos >= last) {
      if (w.loop) w.pos -= last;
      else { w.pos = last; ended = true; }
    }

    const i = Math.floor(w.pos);
    const f = w.pos - i;
    const a = this.captureCameraState(i);
    const b = this.captureCameraState(Math.min(i + 1, last));
    if (a && b) {
      let dYaw = b.yaw - a.yaw;
      while (dYaw > Math.PI) dYaw -= 2 * Math.PI;
      while (dYaw < -Math.PI) dYaw += 2 * Math.PI;
      this._cam.yaw = a.yaw + dYaw * f;
      this._cam.pitch = a.pitch + (b.pitch - a.pitch) * f;
      this._cam.dist = a.dist + (b.dist - a.dist) * f;
      this._cam.target.set(
        a.target[0] + (b.target[0] - a.target[0]) * f,
        a.target[1] + (b.target[1] - a.target[1]) * f,
        a.target[2] + (b.target[2] - a.target[2]) * f);
      this.captureIndex = Math.min(Math.round(w.pos), last);
      this._updateClip();
      this._applyCamera();
    }
    if (ended) { this._walk = null; }
    if (this.onWalk) this.onWalk(this.captureIndex, cams.length, !!this._walk);
  }

  goToCaptureCamera(i, animate = true) {
    const state = this.captureCameraState(i);
    if (!state) return false;
    this.captureIndex = ((i % this.captureCameras.length) + this.captureCameras.length)
      % this.captureCameras.length;
    this.setCameraState(state, animate);
    return true;
  }

  /* ------------------------------------------------------------ photo view */

  /** Adopt a single photograph's camera: `{ hfov, aspect }`, or null. */
  setPhotoCamera(p) {
    this.photoView = false;
    this.photoCamera = p && p.hfov > 0 && p.aspect > 0
      ? { hfov: p.hfov, aspect: p.aspect } : null;
  }

  /**
   * The photo's width / height, read back from the splats, for photo scenes
   * recorded before capture.json carried the image size. Every splat was
   * unprojected from one pixel, so x/z and y/z span the photo's frustum
   * exactly: their extremes are tan(hfov/2) and tan(vfov/2).
   */
  photoAspectFromSplats() {
    const splats = this.mesh?.packedSplats;
    if (!splats) return 0;
    let mx = 0, my = 0;
    splats.forEachSplat((i, c) => {
      if (c.z > 1e-6) {
        mx = Math.max(mx, Math.abs(c.x / c.z));
        my = Math.max(my, Math.abs(c.y / c.z));
      }
    });
    return mx > 0 && my > 0 ? mx / my : 0;
  }

  /** Stand where the photograph was taken. */
  enterPhotoView(animate = true) {
    if (!this.mesh || !this.photoCamera) return false;
    this.stopWalk();
    this.mesh.updateMatrixWorld(true);
    const eye = new THREE.Vector3(0, 0, 0).applyMatrix4(this.mesh.matrixWorld);
    const dir = new THREE.Vector3(0, 0, 1).transformDirection(this.mesh.matrixWorld);
    // pivot on the view axis, level with the scene centre, so orbiting away
    // from here turns about what the photo shows
    const box = this.frameBounds();
    const centre = box ? box.getCenter(new THREE.Vector3()) : eye.clone().add(dir);
    const t = Math.max(centre.clone().sub(eye).dot(dir), 1e-2);
    const target = eye.clone().addScaledVector(dir, t);
    this.setCameraState({ ...this._orbitFrom(eye, target), target: target.toArray() },
      animate);
    this.photoView = true;           // after setCameraState, which clears it
    return true;
  }

  leavePhotoView() { this.photoView = false; }

  /** The vertical field of view that fits the photo's frustum in the viewport. */
  _photoFovY() {
    const p = this.photoCamera;
    const th = Math.tan(THREE.MathUtils.degToRad(p.hfov) / 2);
    const fit = Math.max(th / p.aspect, th / this.camera.aspect) / PHOTO_FILL;
    return THREE.MathUtils.radToDeg(2 * Math.atan(fit));
  }

  /** The photo's rectangle in canvas CSS pixels while in the photo view, else null. */
  photoFrameRect() {
    if (!this.photoView || !this.photoCamera) return null;
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    if (!W || !H) return null;
    const T = Math.tan(THREE.MathUtils.degToRad(this.camera.fov) / 2);
    const th = Math.tan(THREE.MathUtils.degToRad(this.photoCamera.hfov) / 2);
    const w = Math.min(W, (th / (T * this.camera.aspect)) * W);
    const h = Math.min(H, (th / this.photoCamera.aspect / T) * H);
    return { x: (W - w) / 2, y: (H - h) / 2, w, h };
  }

  /* The field of view eases between the photo's and the ordinary one, so
   * leaving the photo view is a quick widening rather than a jump. */
  _tickFov(now) {
    const dt = Math.min(0.1, Math.max(0, (now - (this._lastNow || now)) / 1000));
    this._lastNow = now;
    const goal = this.photoView && this.photoCamera ? this._photoFovY() : FOV_Y;
    const d = goal - this.camera.fov;
    if (Math.abs(d) > 1e-3) {
      this.camera.fov = Math.abs(d) < 0.05 ? goal : this.camera.fov + d * Math.min(1, dt * 12);
      this.camera.updateProjectionMatrix();
    }
    if (this._frameEl) {
      const r = this.photoFrameRect();
      this._frameEl.hidden = !r;
      if (r) {
        const st = this._frameEl.style;
        st.left = `${r.x}px`; st.top = `${r.y}px`;
        st.width = `${r.w}px`; st.height = `${r.h}px`;
      }
    }
  }

  /**
   * Jump to an axis-aligned view, keeping the current pivot and distance.
   *
   * Pitch stops just short of vertical: at exactly 90 degrees the up vector and
   * the view direction line up and lookAt has no defined roll, so the view
   * flips about unpredictably.
   */
  setStandardView(name, animate = true) {
    const NEAR_VERTICAL = 1.54;   // ~88 degrees
    const views = {
      top: { yaw: this._cam.yaw, pitch: -NEAR_VERTICAL },
      bottom: { yaw: this._cam.yaw, pitch: NEAR_VERTICAL },
      front: { yaw: 0, pitch: 0 },
      back: { yaw: Math.PI, pitch: 0 },
      right: { yaw: Math.PI / 2, pitch: 0 },
      left: { yaw: -Math.PI / 2, pitch: 0 },
      // a three-quarter view, which reads best as a default for terrain
      iso: { yaw: Math.PI * 0.25, pitch: -0.62 },
    };
    const v = views[name];
    if (!v) return false;
    this.setCameraState({
      yaw: v.yaw, pitch: v.pitch,
      dist: this._cam.dist,
      target: this._cam.target.toArray(),
    }, animate);
    return true;
  }

  /** Screen-space directions of the world axes, for the orientation gizmo. */
  axisScreenDirections() {
    const origin = this._cam.target;
    const out = {};
    for (const [key, axis] of [["x", [1, 0, 0]], ["y", [0, 1, 0]], ["z", [0, 0, 1]]]) {
      const tip = new THREE.Vector3().fromArray(axis)
        .multiplyScalar(this._cam.dist * 0.25).add(origin);
      const a = origin.clone().project(this.camera);
      const b = tip.project(this.camera);
      // depth relative to the pivot, not raw clip depth: positive means this
      // axis points away from the viewer and should be drawn faded
      out[key] = { x: b.x - a.x, y: -(b.y - a.y), depth: b.z - a.z };
    }
    return out;
  }

  setFlip(on) {
    this.flip = on;
    this._applyFlip();
    this.bounds = this._worldBounds();
  }

  _applyFlip() {
    if (!this.mesh) return;
    // 3DGS scenes trained from COLMAP are Y-down; a 180 degree turn about X
    // brings them into our Y-up world.
    this.mesh.rotation.set(this.flip ? Math.PI : 0, 0, 0);
    this.mesh.updateMatrixWorld(true);
  }

  /* Detail = the share of the scene's splats Spark may draw at once. It only
   * bites on a scene with a level-of-detail tree; without one every splat is
   * drawn regardless. null means this computer's default: Spark's own budget
   * for the device (2.5 M on a desktop, 1.5 M on a Mac, less on a phone), which
   * is ALL of most scenes. The old control scaled that budget instead, so on a
   * desktop anything above 1x changed nothing and a small scene never changed
   * at all. */
  defaultDetail() {
    if (!this.hasLod || !this.splatCount) return 1;
    return Math.min(1, this.spark.defaultSplatTarget() / this.splatCount);
  }
  setDetail(share) {
    this.spark.lodSplatScale = 1;
    this.spark.lodSplatCount = share == null || !this.hasLod
      ? undefined
      : Math.max(1, Math.round(Math.min(1, share) * this.splatCount));
  }
  setBlur(v) { this.spark.blurAmount = v; }

  /* ---------------------------------------------------------------- camera */

  _applyCamera() {
    const { yaw, pitch, dist, target } = this._cam;
    const cp = Math.cos(pitch);
    // The orbit is computed about a plain vertical, then turned into the
    // scene's own vertical. yaw/pitch keep their meaning either way, so saved
    // viewpoints and the axis presets are unaffected by levelling.
    const offset = new THREE.Vector3(
      dist * cp * Math.sin(yaw),
      dist * Math.sin(pitch) * -1,
      dist * cp * Math.cos(yaw),
    );
    if (this.level) offset.applyQuaternion(this._upQuat);
    this.camera.position.copy(target).add(offset);
    this.camera.up.copy(this.level ? this._up : WORLD_UP);
    this.camera.lookAt(target);
  }

  /** yaw/pitch/dist of an eye position about `target`, in the orbit frame. */
  _orbitFrom(eye, target) {
    const v = eye.clone().sub(target);
    const d = v.length() || 1e-3;
    if (this.level) v.applyQuaternion(this._upQuatInv);
    return {
      yaw: Math.atan2(v.x, v.z),
      pitch: THREE.MathUtils.clamp(-Math.asin(v.y / d), -1.54, 1.54),
      dist: d,
    };
  }

  getCameraState() {
    return {
      yaw: this._cam.yaw, pitch: this._cam.pitch, dist: this._cam.dist,
      target: this._cam.target.toArray(),
    };
  }

  setCameraState(s, animate = false) {
    this.photoView = false;          // any other view leaves the photo's
    const to = {
      yaw: s.yaw, pitch: s.pitch, dist: s.dist,
      target: new THREE.Vector3().fromArray(s.target),
    };
    if (!animate) {
      Object.assign(this._cam, to);
      this._cam.target.copy(to.target);
      this._updateClip();
      this._applyCamera();
      return;
    }
    const from = {
      yaw: this._cam.yaw, pitch: this._cam.pitch, dist: this._cam.dist,
      target: this._cam.target.clone(),
    };
    // take the short way round the yaw circle
    let dYaw = to.yaw - from.yaw;
    while (dYaw > Math.PI) dYaw -= 2 * Math.PI;
    while (dYaw < -Math.PI) dYaw += 2 * Math.PI;
    this._anim = { from, to, dYaw, t0: performance.now(), ms: 650 };
  }

  _tickAnim(now) {
    if (!this._anim) return;
    const { from, to, dYaw, t0, ms } = this._anim;
    const k = Math.min(1, (now - t0) / ms);
    const e = k < 0.5 ? 2 * k * k : 1 - 2 * (1 - k) * (1 - k); // ease in-out
    this._cam.yaw = from.yaw + dYaw * e;
    this._cam.pitch = from.pitch + (to.pitch - from.pitch) * e;
    this._cam.dist = from.dist + (to.dist - from.dist) * e;
    this._cam.target.lerpVectors(from.target, to.target, e);
    this._updateClip();
    this._applyCamera();
    if (k >= 1) this._anim = null;
  }

  _initControls() {
    const c = this.canvas;
    let dragging = false, panning = false, moved = 0, lastX = 0, lastY = 0;

    c.addEventListener("contextmenu", (e) => e.preventDefault());
    c.addEventListener("pointerdown", (e) => {
      c.setPointerCapture(e.pointerId);
      dragging = true; moved = 0;
      panning = e.button === 2 || e.shiftKey;
      lastX = e.clientX; lastY = e.clientY;
      this._anim = null;
      // touching the view takes control back from the walk
      this.stopWalk();
    });
    c.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - lastX, dy = e.clientY - lastY;
      lastX = e.clientX; lastY = e.clientY;
      moved += Math.abs(dx) + Math.abs(dy);
      if (moved > 4) this.photoView = false;   // a drag leaves the photo view
      if (panning) {
        const k = this._cam.dist * 0.0015;
        const right = new THREE.Vector3().setFromMatrixColumn(this.camera.matrix, 0);
        const up = new THREE.Vector3().setFromMatrixColumn(this.camera.matrix, 1);
        this._cam.target.addScaledVector(right, -dx * k).addScaledVector(up, dy * k);
      } else {
        this._cam.yaw -= dx * 0.005;
        this._cam.pitch += dy * 0.005;
        this._cam.pitch = THREE.MathUtils.clamp(this._cam.pitch, -1.54, 1.54);
      }
      this._applyCamera();
    });
    c.addEventListener("pointerup", (e) => {
      dragging = false;
      // a click, not a drag: offer it to whoever is picking
      if (moved < 5 && this.onPick) {
        const hit = this.pick(e.clientX, e.clientY);
        if (hit) this.onPick(hit);
      }
    });
    // Zoom towards whatever is under the cursor rather than towards the orbit
    // pivot. With a fixed pivot you can scroll forever on a large scan and never
    // arrive at the thing you are looking at, because the camera is closing on a
    // point that may be nowhere near it.
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      if (!r.width || !r.height) return;
      this.zoomAt(
        ((e.clientX - r.left) / r.width) * 2 - 1,
        -((e.clientY - r.top) / r.height) * 2 + 1,
        e.deltaY,
      );
    }, { passive: false });

    // Double-click recentres the orbit pivot on the surface you clicked, which
    // is the quickest way to get from "the whole site" to "this one detail".
    c.addEventListener("dblclick", (e) => {
      const hit = this.pick(e.clientX, e.clientY);
      if (!hit) return;
      this.setCameraState({
        yaw: this._cam.yaw,
        pitch: this._cam.pitch,
        dist: Math.min(this._cam.dist, Math.max((this.sceneRadius || 1) * 0.35, 1e-3)),
        target: hit.toArray(),
      }, true);
    });
  }

  /* ---------------------------------------------------------------- picking */

  /**
   * Zoom by one wheel step towards a normalised device coordinate.
   *
   * The orbit pivot is slid onto the surface under the cursor without moving the
   * camera, by re-deriving yaw/pitch/dist about the new pivot. Zooming then
   * genuinely closes on what you are looking at, and orbiting afterwards turns
   * around it rather than around empty space -- with a fixed pivot you can
   * scroll forever on a large scan and never arrive.
   */
  zoomAt(ndcX, ndcY, deltaY) {
    this.photoView = false;
    this._anim = null;
    const anchor = this._zoomAnchor(ndcX, ndcY);
    if (anchor) {
      const eye = this.camera.position.clone();
      this._cam.target.lerp(anchor, 0.35);
      const v = eye.sub(this._cam.target);
      const d = v.length();
      if (d > 1e-6) {
        this._cam.dist = d;
        this._cam.pitch = THREE.MathUtils.clamp(-Math.asin(v.y / d), -1.54, 1.54);
        this._cam.yaw = Math.atan2(v.x, v.z);
      }
    }
    const floor = Math.max((this.sceneRadius || 1) * 1e-4, 1e-4);
    this._cam.dist = THREE.MathUtils.clamp(
      this._cam.dist * Math.exp(deltaY * 0.0012), floor, 1e6);
    this._updateClip();
    this._applyCamera();
  }

  /**
   * Surface point to zoom towards, cached across a scroll burst.
   * Picking costs ~4 ms on a 3M-splat scene, so it is fine to do while
   * scrolling, but not worth repeating for every notch at the same spot.
   */
  _zoomAnchor(ndcX, ndcY) {
    const now = performance.now();
    const a = this._anchor;
    if (a && now - a.at < 500 &&
        Math.abs(ndcX - a.x) < 0.02 && Math.abs(ndcY - a.y) < 0.02) {
      a.at = now;
      return a.point;
    }
    const point = this.pickNdc(ndcX, ndcY);
    this._anchor = { point, x: ndcX, y: ndcY, at: now };
    return point;
  }

  /** World-space point under the cursor, or null. */
  pick(clientX, clientY) {
    const r = this.canvas.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    return this.pickNdc(
      ((clientX - r.left) / r.width) * 2 - 1,
      -((clientY - r.top) / r.height) * 2 + 1,
    );
  }

  /**
   * World-space point under a normalised device coordinate, or null.
   * Layout-independent, so tests can drive it without a laid-out canvas.
   */
  pickNdc(x, y) {
    if (!this.mesh) return null;
    const ndc = new THREE.Vector2(x, y);
    this._raycaster.setFromCamera(ndc, this.camera);
    const hits = [];
    this.mesh.raycast(this._raycaster, hits);
    if (!hits.length) return null;
    hits.sort((a, b) => a.distance - b.distance);
    return hits[0].point.clone();
  }

  /** World point -> overlay pixel coordinates. */
  project(world) {
    const v = world.clone().project(this.camera);
    const r = this.canvas.getBoundingClientRect();
    return {
      x: (v.x * 0.5 + 0.5) * r.width,
      y: (-v.y * 0.5 + 0.5) * r.height,
      visible: v.z < 1,
    };
  }

  /* --------------------------------------------------------------- section */

  /**
   * Section box: keep only the splats inside an axis-aligned world-space box,
   * given as 0..1 fractions of frameBounds() per axis. An inverted BOX SDF
   * erases everything outside, so pulling in one face is a plane cut and the
   * default full box already crops the floater splats beyond the robust bounds.
   *
   * Why a box and not the earlier plane: Spark 2.1.0's plane SDF keeps its
   * inside in the plane's local -Z half-space (shader: `distance = sdfPos.z`),
   * while the old code rotated local +Y onto the cut normal -- so the actual
   * cut plane stayed perpendicular to the intended one, pinned through the
   * scene centre, and the position slider could never move it. The box SDF
   * has no such axis convention to trip over and is the more useful tool
   * (slabs for TerrainSlicer-style contours).
   *
   * Also accepts the legacy plane shape {axis, t, flip} from old .dlscene
   * files and converts it to the equivalent one-face cut.
   */
  setSection(section, { outline = true } = {}) {
    if (!this.mesh) return;
    const s = Viewer.normalizeSection(section);
    if (!s.enabled) {
      if (this._edit) { this._edit.removeFromParent(); this._edit = null; this._sdf = null; }
      if (this._sectionHelper) { this._sectionHelper.removeFromParent(); this._sectionHelper = null; }
      return;
    }
    if (!this._edit) {
      this._sdf = new SplatEditSdf({
        type: SplatEditSdfType.BOX,
        invert: true,        // inside becomes outside: erase everything beyond the box
        opacity: 0,          // splats inside the (inverted) SDF are erased
        color: new THREE.Color(1, 1, 1),
      });
      this._edit = new SplatEdit({
        rgbaBlendMode: SplatEditRgbaBlendMode.MULTIPLY,
        sdfSmooth: 0,
        softEdge: 0,
      });
      this._edit.addSdf(this._sdf);
      this.mesh.add(this._edit);
    }
    // re-asserted every call: anything that pokes the sdf (console, tests)
    // must not silently turn the crop into its complement
    this._sdf.invert = true;
    this._edit.invert = false;

    const b = this.frameBounds();
    const box = new THREE.Box3(
      new THREE.Vector3(
        THREE.MathUtils.lerp(b.min.x, b.max.x, s.min.x),
        THREE.MathUtils.lerp(b.min.y, b.max.y, s.min.y),
        THREE.MathUtils.lerp(b.min.z, b.max.z, s.min.z)),
      new THREE.Vector3(
        THREE.MathUtils.lerp(b.min.x, b.max.x, s.max.x),
        THREE.MathUtils.lerp(b.min.y, b.max.y, s.max.y),
        THREE.MathUtils.lerp(b.min.z, b.max.z, s.max.z)),
    );

    // Spark packs the inverse of the SDF's full matrixWorld, but evaluates it
    // against splat positions in the ACCUMULATOR's frame -- the mesh's own
    // transform at accumulation time, not world space (measured empirically;
    // this is also why the old plane cut "never moved"). The mesh transform
    // therefore appears twice in the composition: once in the SDF's parent
    // chain, once in the splat positions. Our only mesh transform is the
    // 180-degree flip, which is its own inverse, so the two cancel exactly
    // when the SDF's LOCAL transform is the plain world-space box: position =
    // world centre, identity rotation. Do not "compensate" via worldToLocal --
    // that is what re-broke it. Spark reads `scale` as the box half-extents,
    // separate from the rigid transform.
    const centre = box.getCenter(new THREE.Vector3());
    const half = box.getSize(new THREE.Vector3()).multiplyScalar(0.5);
    this._sdf.position.copy(centre);
    this._sdf.quaternion.identity();
    this._sdf.scale.set(
      Math.max(half.x, 1e-6), Math.max(half.y, 1e-6), Math.max(half.z, 1e-6));
    this._sdf.updateMatrixWorld(true);

    // faint outline while adjusting; embeds pass outline:false -- a published
    // scene shows the cut, not the scaffolding around it
    if (!outline) {
      if (this._sectionHelper) { this._sectionHelper.removeFromParent(); this._sectionHelper = null; }
    } else if (!this._sectionHelper) {
      this._sectionHelper = new THREE.Box3Helper(box, 0x8a8a8a);
      this.scene.add(this._sectionHelper);
    } else {
      this._sectionHelper.box.copy(box);
    }
  }

  /**
   * Accept both section shapes and clamp to a valid box.
   * New: {enabled, min:{x,y,z}, max:{x,y,z}} as 0..1 fractions.
   * Legacy plane: {enabled, axis, t, flip} -> one face pulled in to t.
   */
  static normalizeSection(section) {
    const full = { enabled: false, min: { x: 0, y: 0, z: 0 }, max: { x: 1, y: 1, z: 1 } };
    if (!section?.enabled) return full;
    const s = { enabled: true, min: { ...full.min }, max: { ...full.max } };
    if (section.min || section.max) {
      for (const a of ["x", "y", "z"]) {
        s.min[a] = THREE.MathUtils.clamp(section.min?.[a] ?? 0, 0, 1);
        s.max[a] = THREE.MathUtils.clamp(section.max?.[a] ?? 1, 0, 1);
        if (s.max[a] < s.min[a]) [s.min[a], s.max[a]] = [s.max[a], s.min[a]];
      }
    } else if (section.axis) {
      const t = THREE.MathUtils.clamp(section.t ?? 1, 0, 1);
      if (section.flip) s.min[section.axis] = t;
      else s.max[section.axis] = t;
    }
    return s;
  }

  /* ----------------------------------------------------------------- still */

  /**
   * The current view as an image: same framing and aspect as on screen, at
   * `width` pixels wide (0 = the screen's own resolution). Returns a 2D canvas.
   *
   * Three things make this more than a toDataURL():
   *
   * - SIZE BY PIXEL RATIO, not by resizing the canvas. Only the pixel density
   *   changes, so the still frames exactly what is on screen whatever its size.
   * - CLAMPED TO THE GPU. A student's old laptop may not hold a 4K render
   *   target, and asking for one can lose the WebGL context -- the one failure
   *   this tool must never cause. The size is capped at the chip's own
   *   MAX_RENDERBUFFER_SIZE / MAX_VIEWPORT_DIMS, and the caller is told what it
   *   actually got.
   * - SETTLED, not grabbed. Spark sorts and refines on its own schedule, so the
   *   first frame after a change can be a state behind, or a buffer cleared but
   *   not yet drawn -- which the section-box test found reads as an empty
   *   scene. It renders until two successive frames agree, and refuses to hand
   *   back an empty image.
   *
   * Scaffolding (the section box's grey outline) is hidden for the capture: a
   * still shows the cut, not the tool that made it -- the same rule exports
   * follow. Timers rather than requestAnimationFrame drive the settling, so it
   * also completes in a tab the browser is throttling.
   */
  async captureStill({ width = 0, hideScaffolding = true } = {}) {
    if (!this.mesh) throw new Error("open a scene first");
    const cssW = this.canvas.clientWidth, cssH = this.canvas.clientHeight;
    if (!cssW || !cssH) throw new Error("the viewport has no size to capture");

    const gl = this.renderer.getContext();
    const dims = gl.getParameter(gl.MAX_VIEWPORT_DIMS);
    const maxDim = Math.min(gl.getParameter(gl.MAX_RENDERBUFFER_SIZE), dims[0], dims[1]);
    const screenRatio = this.renderer.getPixelRatio();
    const asked = width > 0 ? width / cssW : screenRatio;
    const ratio = Math.min(asked, maxDim / cssW, maxDim / cssH);

    const helper = this._sectionHelper;
    const helperWas = helper ? helper.visible : false;
    if (hideScaffolding && helper) helper.visible = false;
    this.renderer.setPixelRatio(ratio);
    this.renderer.setSize(cssW, cssH, false);

    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    // a 48 x 27 thumbnail is plenty to tell whether two frames agree
    const probe = document.createElement("canvas");
    probe.width = 48; probe.height = 27;
    const pctx = probe.getContext("2d", { willReadFrequently: true });
    const fingerprint = () => {
      pctx.drawImage(this.canvas, 0, 0, probe.width, probe.height);
      const px = pctx.getImageData(0, 0, probe.width, probe.height).data;
      let sum = 0, lit = 0;
      for (let i = 0; i < px.length; i += 4) {
        sum = (sum * 31 + px[i] + px[i + 1] * 7 + px[i + 2] * 13) >>> 0;
        if (px[i] > 30 || px[i + 1] > 30 || px[i + 2] > 34) lit++;   // not the stage
      }
      return { sum, lit };
    };

    try {
      const out = document.createElement("canvas");
      let last = null;
      for (let i = 0; i < 24; i++) {
        await wait(i < 3 ? 60 : 120);
        if (gl.isContextLost()) {
          throw new Error("the graphics chip gave up at this size — try a smaller one");
        }
        // Render and copy in the SAME task: a WebGL canvas is only readable
        // until the browser composites it.
        this.renderer.render(this.scene, this.camera);
        const f = fingerprint();
        if (f.lit > 0 && last && f.sum === last.sum) {
          out.width = this.canvas.width;
          out.height = this.canvas.height;
          out.getContext("2d").drawImage(this.canvas, 0, 0);
          out.stillInfo = { width: out.width, height: out.height,
                            clamped: asked > ratio + 1e-6, maxDim };
          return out;
        }
        last = f;
      }
      throw new Error("the view would not settle into a still — try again");
    } finally {
      this.renderer.setPixelRatio(screenRatio);
      this.renderer.setSize(cssW, cssH, false);
      if (helper) helper.visible = helperWas;
    }
  }

  /* ------------------------------------------------------------------ loop */

  _resize() {
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    // No layout yet (container hidden, or the window collapsed). Keep whatever
    // size we last had rather than shrinking the target to nothing, so the view
    // comes back intact when the element is shown again.
    if (!w || !h) return;
    const dpr = this.renderer.getPixelRatio();
    if (this.canvas.width !== Math.floor(w * dpr) ||
        this.canvas.height !== Math.floor(h * dpr)) {
      this.renderer.setSize(w, h, false);
    }
    // always kept in step with the element, including on the very first call
    if (this.camera.aspect !== w / h) {
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
    }
  }

  _loop(now) {
    requestAnimationFrame(this._loop);
    // ResizeObserver callbacks are delivered on the frame loop, which browsers
    // throttle for backgrounded or hidden frames -- an embed in an iframe can
    // otherwise keep rendering at a stale size. Checking here is two integer
    // comparisons and makes resizing independent of that timing.
    this._resize();
    this._tickAnim(now);
    this._tickWalk(now);
    this._tickFov(now);
    this.renderer.render(this.scene, this.camera);

    this._fpsFrames++;
    if (now - this._fpsTime > 500) {
      this.fps = Math.round((this._fpsFrames * 1000) / (now - this._fpsTime));
      this._fpsFrames = 0; this._fpsTime = now;
    }
    if (this.onFrame) this.onFrame();
  }
}
