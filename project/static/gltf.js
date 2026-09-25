/* A single-photo scene as a glTF 2.0 binary (.glb) that plain Blender opens:
 * File > Import > glTF 2.0, no add-on.
 *
 * WHY glTF, AND WHY A SURFACE. Blender's own package route (Capture Walk) is
 * built from a video's camera path, which a photo scene does not have, and
 * anything that drives Blender itself imports bpy and is GPL. glTF is an open
 * format Blender reads natively, so this module stays Apache-2.0 and needs
 * nothing installed. It carries a mesh, not the splats: Blender does not draw a
 * mesh of bare points in object mode, so a point cloud would arrive invisible.
 *
 * WHAT IS IN IT
 *   Photo camera    at the origin, looking down the photo's axis, with the
 *                   photo's field of view and aspect -- exact, because the
 *                   scene was unprojected from this camera (tools/depth_splat.py)
 *   Photo surface   the depth grid meshed and textured with the photograph:
 *                   from the camera it IS the photo; away from it, the 2.5D it
 *                   is. By default every gap is FILLED (fill: true), so the
 *                   surface is one sheet with no holes -- the edges of depth
 *                   jumps become stretched faces, invisible from the camera.
 *   Photo backdrop  only with fill: false -- the photograph on a plane filling
 *                   the camera's view just behind the farthest surface, so the
 *                   kept gaps show the right pixels
 *   Sun, North      only once north is set (Display -> Sun and north): a
 *                   directional light (KHR_lights_punctual, which Blender imports
 *                   as a Sun lamp) shining from where the sun stood at the chosen
 *                   time, and an empty whose Y axis points north in Blender. The
 *                   splat and the photo already hold the real light; the sun is
 *                   for what is ADDED in Blender, so it is lit and shadowed like
 *                   the footage. Date, time and place ride along only if asked.
 *
 * REBUILDING THE GRID. Every splat of a photo scene came from one pixel of a
 * stride-s grid: X = (x - w/2) z / f, Y = (y - h/2) z / f, f = (w/2) / tan(hfov/2).
 * So each splat maps back to its grid cell. Spark holds positions as half
 * floats, too coarse to find the grid by clustering, but rounding against a
 * KNOWN spacing stays under half a cell (worst case about 0.4 at 1600 px) --
 * the spacing comes from the size the depth was worked at, which the record
 * carries since 2026-09-24, or which follows from the photograph's own size and
 * the recorded max_side, exactly as depth_splat.py computed it. Vertex
 * positions are then rebuilt from the cell and the depth, so the mesh is a
 * clean grid, not half-float noise.
 *
 * glTF is Y-up and a camera looks down -Z; the scene is stored COLMAP-style,
 * Y down and Z forward, so (x, y, z) -> (x, -y, -z): a half turn about X, which
 * keeps handedness.
 */

const EDGE_RATIO = 1.08;      // a triangle whose corner depths differ more is a jump
const BACKDROP = 1.02;        // backdrop distance, x the farthest surface

/** The size the depth map was worked at -- the grid the splats sit on. */
export function workedSize(record, photoSize) {
  const w = record?.image?.worked;
  if (Array.isArray(w) && w[0] > 0 && w[1] > 0) return { w: w[0], h: w[1] };
  if (!photoSize) return null;
  const maxSide = Number(record?.settings?.max_side) || 1600;
  let { width, height } = photoSize;
  if (Math.max(width, height) > maxSide) {        // depth_splat.py's own rule
    const k = maxSide / Math.max(width, height);
    width = Math.round(width * k); height = Math.round(height * k);
  }
  return { w: width, h: height };
}

/**
 * Build the .glb. `splats` is Spark's PackedSplats; `record` the scene's
 * capture record (fov, stride); `photo` { bytes: ArrayBuffer, mime, width,
 * height }; `scale` metres per scene unit (1 when uncalibrated: the depth
 * model's estimate). Returns { blob, stats }.
 */
export function photoSceneGlb({ splats, record, photo, scale = 1, note = "", fill = true, sun = null }) {
  const hfov = Number(record?.settings?.fov) || 65;
  const stride = Number(record?.settings?.stride) || 2;
  const size = workedSize(record, photo);
  if (!size) throw new Error("the photograph is needed to rebuild the depth grid");
  const { w, h } = size;
  const f = (w / 2) / Math.tan((hfov * Math.PI / 180) / 2);
  const cols = Math.floor((w - 1) / stride) + 1;
  const rows = Math.floor((h - 1) / stride) + 1;

  // 1. each splat back to its grid cell; keep its depth
  const depth = new Float32Array(rows * cols);          // 0 = no splat there
  let placed = 0, collided = 0, outside = 0, maxZ = 0;
  splats.forEachSplat((i, c) => {
    if (!(c.z > 1e-6)) return;
    const j = Math.round((c.x / c.z * f + w / 2) / stride);
    const k = Math.round((c.y / c.z * f + h / 2) / stride);
    if (j < 0 || k < 0 || j >= cols || k >= rows) { outside++; return; }
    const cell = k * cols + j;
    if (depth[cell]) { collided++; return; }
    depth[cell] = c.z;
    placed++;
    if (c.z > maxZ) maxZ = c.z;
  });
  if (placed < 3) throw new Error("no splats fell on the photo's grid");

  // 1b. FILL THE GAPS (default): every empty cell -- a splat dropped at a
  // depth jump, or never there -- gets the mean depth of the cells around it,
  // spreading inward from the edges of each hole (one breadth-first pass). The
  // surface is then ONE sheet with no holes: from the camera it is the photo
  // everywhere; from the side, faces stretch across each depth jump, textured
  // with the pixels at that edge. Left off, the gaps stay and a backdrop
  // behind the scene shows the right pixels through them instead.
  let filledIn = 0;
  if (fill) {
    const queue = [];
    const queued = new Uint8Array(rows * cols);
    const nbrs = (cell) => {
      const k = Math.floor(cell / cols), j = cell % cols, out = [];
      for (let dk = -1; dk <= 1; dk++) {
        for (let dj = -1; dj <= 1; dj++) {
          if (!dk && !dj) continue;
          const kk = k + dk, jj = j + dj;
          if (kk >= 0 && jj >= 0 && kk < rows && jj < cols) out.push(kk * cols + jj);
        }
      }
      return out;
    };
    for (let cell = 0; cell < rows * cols; cell++) {
      if (depth[cell]) continue;
      if (nbrs(cell).some((n) => depth[n])) { queue.push(cell); queued[cell] = 1; }
    }
    for (let q = 0; q < queue.length; q++) {
      const cell = queue[q];
      let sum = 0, n = 0;
      for (const nb of nbrs(cell)) {
        if (depth[nb]) { sum += depth[nb]; n++; }
        else if (!queued[nb]) { queued[nb] = 1; queue.push(nb); }
      }
      depth[cell] = sum / n;
      filledIn++;
    }
  }

  // 2. vertices for occupied cells, rebuilt from cell + depth; UVs into the photo
  const vid = new Int32Array(rows * cols).fill(-1);
  const pos = [], uv = [];
  for (let k = 0; k < rows; k++) {
    for (let j = 0; j < cols; j++) {
      const z = depth[k * cols + j];
      if (!z) continue;
      const px = j * stride, py = k * stride;
      vid[k * cols + j] = pos.length / 3;
      pos.push(((px - w / 2) * z / f) * scale, -((py - h / 2) * z / f) * scale, -z * scale);
      uv.push(px / w, py / h);
    }
  }

  // 3. triangles between neighbouring cells -- across depth jumps only when
  // filling (one sheet); with the gaps kept, a jump is left open
  const idx = [];
  const ok = (...cells) => {
    let lo = Infinity, hi = 0;
    for (const c of cells) { const z = depth[c]; if (!z) return false; lo = Math.min(lo, z); hi = Math.max(hi, z); }
    return fill || hi / lo < EDGE_RATIO;       // filled: one sheet, jumps bridged
  };
  for (let k = 0; k + 1 < rows; k++) {
    for (let j = 0; j + 1 < cols; j++) {
      const a = k * cols + j, b = a + 1, c = a + cols, d = c + 1;
      // counter-clockwise as seen from the camera, after the half turn
      if (ok(a, c, b)) idx.push(vid[a], vid[c], vid[b]);
      if (ok(b, c, d)) idx.push(vid[b], vid[c], vid[d]);
    }
  }

  // 4. the backdrop, filling the camera's view just behind the farthest surface
  const tanH = Math.tan((hfov * Math.PI / 180) / 2), tanV = tanH * h / w;
  const D = maxZ * BACKDROP;
  const bx = tanH * D * scale, by = tanV * D * scale, bz = -D * scale;
  const bpos = [-bx, by, bz, -bx, -by, bz, bx, by, bz, bx, -by, bz];
  const buv = [0, 0, 0, 1, 1, 0, 1, 1];
  const bidx = [0, 1, 2, 2, 1, 3];

  // 5. the glTF document -- the backdrop only when the gaps were kept: a
  // filled surface is one closed sheet, and nothing behind it shows
  const bin = new BinWriter();
  const posView = bin.add(new Float32Array(pos), 34962);
  const uvView = bin.add(new Float32Array(uv), 34962);
  const idxView = bin.add(new Uint32Array(idx), 34963);
  const imgView = bin.add(new Uint8Array(photo.bytes));
  const back = !fill && {
    pos: bin.add(new Float32Array(bpos), 34962),
    uv: bin.add(new Float32Array(buv), 34962),
    idx: bin.add(new Uint16Array(bidx), 34963),
  };

  const bounds = (a) => {
    const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < a.length; i += 3) {
      for (let q = 0; q < 3; q++) { min[q] = Math.min(min[q], a[i + q]); max[q] = Math.max(max[q], a[i + q]); }
    }
    return { min, max };
  };
  const unlit = { KHR_materials_unlit: {} };
  const material = (name) => ({
    name, doubleSided: true, extensions: unlit,
    pbrMetallicRoughness: { baseColorTexture: { index: 0 }, metallicFactor: 0, roughnessFactor: 1 },
  });

  // the sun and north, turned into glTF's frame like everything else
  const sunNodes = [];
  let lights = null;
  if (sun?.north && sun?.up) {
    const g = (v) => [v[0], -v[1], -v[2]];
    const up = g(sun.up);
    sunNodes.push({ name: "North", rotation: lookRotation(g(sun.north), up),
      extras: { note: "In Blender this empty's Y axis points north." } });
    if (sun.toSun && sun.elevation > 0) {
      const toSun = g(sun.toSun);
      lights = [{ type: "directional", name: "Sun", color: [1, 1, 1],
                  intensity: SUN_LUX }];
      sunNodes.push({
        name: "Sun", rotation: lookRotation(toSun.map((x) => -x), up),
        translation: toSun.map((x) => x * D * 0.5 * scale),
        extensions: { KHR_lights_punctual: { light: 0 } },
        extras: sun.extras,
      });
    }
  }
  const nodeCount = back ? 3 : 2;

  const gltf = {
    asset: {
      version: "2.0", generator: "DL-SplatGenerator (Digital Landscapes)",
      extras: { note: note || undefined, scale_m_per_unit: scale, hfov_deg: hfov,
                sun: sun?.extras },
    },
    extensionsUsed: ["KHR_materials_unlit", ...(lights ? ["KHR_lights_punctual"] : [])],
    ...(lights ? { extensions: { KHR_lights_punctual: { lights } } } : {}),
    scene: 0,
    scenes: [{ name: "Photo scene",
               nodes: [...Array(nodeCount + sunNodes.length).keys()] }],
    nodes: [
      { name: "Photo camera", camera: 0,
        extras: { resolution_x: photo.width || w, resolution_y: photo.height || h, hfov_deg: hfov } },
      { name: "Photo surface", mesh: 0 },
      ...(back ? [{ name: "Photo backdrop", mesh: 1 }] : []),
      ...sunNodes,
    ],
    cameras: [{
      name: "Photo camera", type: "perspective",
      perspective: { yfov: 2 * Math.atan(tanV), aspectRatio: w / h,
                     znear: 0.05 * scale, zfar: D * 1.5 * scale },
    }],
    meshes: [
      { name: "Photo surface", primitives: [{ attributes: { POSITION: 0, TEXCOORD_0: 1 }, indices: 2, material: 0 }] },
      ...(back ? [{ name: "Photo backdrop", primitives: [{ attributes: { POSITION: 3, TEXCOORD_0: 4 }, indices: 5, material: 1 }] }] : []),
    ],
    materials: back ? [material("Photo surface"), material("Photo backdrop")] : [material("Photo surface")],
    textures: [{ source: 0, sampler: 0 }],
    samplers: [{ magFilter: 9729, minFilter: 9987, wrapS: 33071, wrapT: 33071 }],
    images: [{ name: "photograph", bufferView: imgView, mimeType: photo.mime }],
    accessors: [
      { bufferView: posView, componentType: 5126, count: pos.length / 3, type: "VEC3", ...bounds(pos) },
      { bufferView: uvView, componentType: 5126, count: uv.length / 2, type: "VEC2" },
      { bufferView: idxView, componentType: 5125, count: idx.length, type: "SCALAR" },
      ...(back ? [
        { bufferView: back.pos, componentType: 5126, count: 4, type: "VEC3", ...bounds(bpos) },
        { bufferView: back.uv, componentType: 5126, count: 4, type: "VEC2" },
        { bufferView: back.idx, componentType: 5123, count: 6, type: "SCALAR" },
      ] : []),
    ],
    bufferViews: bin.views,
    buffers: [{ byteLength: bin.length }],
  };

  const blob = glb(gltf, bin.bytes());
  return {
    blob,
    stats: { grid: [cols, rows], worked: [w, h], placed, collided, outside, filledIn,
             holes: rows * cols - placed - filledIn, backdrop: !!back,
             vertices: pos.length / 3, triangles: idx.length / 3, bytes: blob.size,
             sun: !!lights, north: sunNodes.length > 0 },
  };
}

/* Blender's glTF importer turns a directional light's lux into its Sun
 * strength in W/m² by dividing by 683 (its "Standard" lighting mode); 3 W/m²
 * is a plain sunny-day Sun in Blender. Checked by importing into Blender 5.2. */
const SUN_LUX = 3 * 683;

/**
 * The node rotation (quaternion x, y, z, w) whose local -Z points along
 * `forward` and whose local +Y leans towards `up` -- how glTF aims a camera or
 * a light, and so how an empty's axes are set.
 */
function lookRotation(forward, up) {
  const n = (v) => { const l = Math.hypot(...v); return v.map((x) => x / l); };
  const crossP = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const z = n(forward.map((x) => -x));
  let u = up;
  if (Math.abs(u[0] * z[0] + u[1] * z[1] + u[2] * z[2]) > 0.999) u = Math.abs(z[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0];
  const x = n(crossP(u, z));
  const y = crossP(z, x);
  // rotation matrix with columns x, y, z -> quaternion
  const [m00, m10, m20] = x, [m01, m11, m21] = y, [m02, m12, m22] = z;
  const tr = m00 + m11 + m22;
  let qx, qy, qz, qw;
  if (tr > 0) {
    const s = Math.sqrt(tr + 1) * 2;
    qw = s / 4; qx = (m21 - m12) / s; qy = (m02 - m20) / s; qz = (m10 - m01) / s;
  } else if (m00 > m11 && m00 > m22) {
    const s = Math.sqrt(1 + m00 - m11 - m22) * 2;
    qw = (m21 - m12) / s; qx = s / 4; qy = (m01 + m10) / s; qz = (m02 + m20) / s;
  } else if (m11 > m22) {
    const s = Math.sqrt(1 + m11 - m00 - m22) * 2;
    qw = (m02 - m20) / s; qx = (m01 + m10) / s; qy = s / 4; qz = (m12 + m21) / s;
  } else {
    const s = Math.sqrt(1 + m22 - m00 - m11) * 2;
    qw = (m10 - m01) / s; qx = (m02 + m20) / s; qy = (m12 + m21) / s; qz = s / 4;
  }
  return [qx, qy, qz, qw];
}

/**
 * The splat itself, as a standard 3DGS .ply (INRIA layout) in BLENDER'S frame,
 * so it lands exactly where the glTF's camera and surface do.
 *
 * Blender's glTF importer turns glTF's Y-up into its own Z-up, (x, y, z) ->
 * (x, -z, y); the surface above is written as (x, -y, -z) of the scene's own
 * COLMAP-style frame. Composed: scene (x, y, z) -> Blender (x, z, -y), a -90°
 * turn about X. Splat importers take a .ply's coordinates as they are
 * (Capture Walk's does, when a splat is imported on its own), so the splat is
 * written already turned: positions, and each splat's rotation, by the same
 * quarter turn. `scale` multiplies positions and splat sizes alike.
 *
 * Written from Spark's PackedSplats: half-float positions (relative error about
 * 0.05 %), 8-bit colour. A photo scene has one colour per splat (degree-0
 * harmonics), so nothing view-dependent is lost; a trained scene's higher
 * harmonics are not carried.
 */
export function splatPlyForBlender({ splats, scale = 1 }) {
  const SH_C0 = 0.28209479177387814;
  const props = ["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2", "opacity",
                 "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"];
  const n = splats.numSplats;
  const body = new Float32Array(n * props.length);
  const r = Math.SQRT1_2;                       // quarter turn about X: (w, x) = (cos -45°, sin -45°)
  const logit = (a) => { const c = Math.min(0.9999, Math.max(1e-4, a)); return Math.log(c / (1 - c)); };
  const ls = Math.log(scale);
  let o = 0;
  splats.forEachSplat((i, c, s, q, opacity, col) => {
    // position: (x, y, z) -> (x, z, -y), scaled
    body[o++] = c.x * scale; body[o++] = c.z * scale; body[o++] = -c.y * scale;
    body[o++] = (col.r - 0.5) / SH_C0; body[o++] = (col.g - 0.5) / SH_C0; body[o++] = (col.b - 0.5) / SH_C0;
    body[o++] = logit(opacity);
    body[o++] = Math.log(Math.max(s.x, 1e-12)) + ls;
    body[o++] = Math.log(Math.max(s.y, 1e-12)) + ls;
    body[o++] = Math.log(Math.max(s.z, 1e-12)) + ls;
    // rotation: the Hamilton product qTurn * q, qTurn = (w r, x -r, y 0, z 0),
    // written in the INRIA order (w, x, y, z)
    const w = q.w, x = q.x, y = q.y, z = q.z;
    body[o++] = r * w + r * x;
    body[o++] = r * x - r * w;
    body[o++] = r * y + r * z;
    body[o++] = r * z - r * y;
  });
  const header = "ply\nformat binary_little_endian 1.0\n"
    + `element vertex ${n}\n` + props.map((p) => `property float ${p}\n`).join("")
    + "end_header\n";
  return new Blob([new TextEncoder().encode(header), body.buffer], { type: "application/octet-stream" });
}

/* ---------------------------------------------------------------- writing */

class BinWriter {
  constructor() { this.parts = []; this.views = []; this.length = 0; }
  /** Append a typed array as a buffer view, 4-byte aligned; returns its index. */
  add(array, target) {
    const pad = (4 - (this.length % 4)) % 4;
    if (pad) { this.parts.push(new Uint8Array(pad)); this.length += pad; }
    const bytes = new Uint8Array(array.buffer, array.byteOffset, array.byteLength);
    this.views.push({ buffer: 0, byteOffset: this.length, byteLength: bytes.byteLength,
                      ...(target ? { target } : {}) });
    this.parts.push(bytes);
    this.length += bytes.byteLength;
    return this.views.length - 1;
  }
  bytes() {
    const out = new Uint8Array(this.length);
    let o = 0;
    for (const p of this.parts) { out.set(p, o); o += p.byteLength; }
    return out;
  }
}

/** The binary glTF container: a 12-byte header, a JSON chunk, a BIN chunk. */
function glb(json, bin) {
  const enc = new TextEncoder().encode(JSON.stringify(json));
  const jsonLen = Math.ceil(enc.length / 4) * 4;
  const binLen = Math.ceil(bin.length / 4) * 4;
  const total = 12 + 8 + jsonLen + 8 + binLen;
  const out = new Uint8Array(total);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, 0x46546c67, true);        // "glTF"
  dv.setUint32(4, 2, true);
  dv.setUint32(8, total, true);
  dv.setUint32(12, jsonLen, true);
  dv.setUint32(16, 0x4e4f534a, true);       // "JSON"
  out.set(enc, 20);
  out.fill(0x20, 20 + enc.length, 20 + jsonLen);   // pad JSON with spaces
  dv.setUint32(20 + jsonLen, binLen, true);
  dv.setUint32(24 + jsonLen, 0x004e4942, true);    // "BIN\0"
  out.set(bin, 28 + jsonLen);
  return new Blob([out], { type: "model/gltf-binary" });
}
