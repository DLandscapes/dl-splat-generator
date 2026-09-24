// Generates demo.ply — a synthetic 3D Gaussian Splatting scene (terrain + trees)
// in the standard INRIA .ply layout (binary_little_endian, float properties).
// Run: node make-demo.mjs
import { writeFileSync } from "node:fs";

const SH_C0 = 0.28209479177387814;
const logit = (a) => Math.log(a / (1 - a));
const dc = (c) => (c - 0.5) / SH_C0;

// deterministic PRNG
let seed = 1234567;
function rand() {
  seed = (seed * 1664525 + 1013904223) >>> 0;
  return seed / 4294967296;
}

// value-noise heightfield
const GRID = 64;
const noise = [];
for (let o = 0; o < 4; o++) {
  const g = new Float32Array((GRID + 2) * (GRID + 2));
  for (let i = 0; i < g.length; i++) g[i] = rand();
  noise.push(g);
}
function sampleOctave(g, x, y) {
  const xi = Math.floor(x), yi = Math.floor(y);
  const xf = x - xi, yf = y - yi;
  const sx = xf * xf * (3 - 2 * xf), sy = yf * yf * (3 - 2 * yf);
  const idx = (i, j) => g[((j % GRID) + GRID) % GRID * (GRID + 2) + ((i % GRID) + GRID) % GRID];
  const a = idx(xi, yi), b = idx(xi + 1, yi), c = idx(xi, yi + 1), d = idx(xi + 1, yi + 1);
  return a + (b - a) * sx + (c - a) * sy + (a - b - c + d) * sx * sy;
}
function height(x, z) {
  // x,z in [-5,5]
  let h = 0, amp = 1, freq = 0.35, norm = 0;
  for (let o = 0; o < 4; o++) {
    h += amp * sampleOctave(noise[o], (x + 20) * freq, (z + 20) * freq);
    norm += amp;
    amp *= 0.5; freq *= 2.1;
  }
  h /= norm;
  const r = Math.hypot(x, z);
  return h * 2.2 * Math.max(0, 1 - (r / 7.5) ** 2) - 0.35; // island falloff
}

const splats = []; // {x,y,z, r,g,b, a, sx,sy,sz, qw,qx,qy,qz}
const WATER = 0.02;

function push(s) { splats.push(s); }

// ---- terrain: jittered grid of flattened gaussians -------------------------
const N = 230, EXT = 5;
for (let i = 0; i < N; i++) {
  for (let j = 0; j < N; j++) {
    const x = -EXT + (i + 0.5 + (rand() - 0.5) * 0.7) * (2 * EXT / N);
    const z = -EXT + (j + 0.5 + (rand() - 0.5) * 0.7) * (2 * EXT / N);
    let h = height(x, z);
    const cell = (2 * EXT / N);
    let r, g, b, sy;
    if (h < WATER) {
      // water: flat blue, slight depth-based darkening
      const depth = Math.min(1, (WATER - h) * 2.5);
      r = 0.10 - 0.04 * depth; g = 0.35 - 0.13 * depth; b = 0.55 - 0.12 * depth;
      h = WATER;
      sy = 0.012;
    } else {
      const t = Math.min(1, (h - WATER) / 1.6); // 0 shore .. 1 peak
      const n = (rand() - 0.5) * 0.06;
      if (t < 0.06) { r = 0.78 + n; g = 0.72 + n; b = 0.52 + n; }          // sand
      else if (t < 0.45) { r = 0.22 + 0.3 * t + n; g = 0.46 + 0.2 * t + n; b = 0.20 + n; } // grass
      else if (t < 0.75) { r = 0.42 + n; g = 0.38 + n; b = 0.34 + n; }     // rock
      else { r = 0.92 + n; g = 0.93 + n; b = 0.95 + n; }                   // snow
      sy = 0.02 + 0.03 * rand();
    }
    push({
      x, y: -h, z: -z, // y-down / z-flipped, like COLMAP-space 3DGS scenes
      r, g, b, a: 0.92,
      sx: cell * (0.7 + rand() * 0.5), sy, sz: cell * (0.7 + rand() * 0.5),
      qw: 1, qx: 0, qy: 0, qz: 0,
    });
  }
}

// ---- trees: trunk + foliage blobs on grassy slopes --------------------------
let trees = 0;
while (trees < 260) {
  const x = (rand() * 2 - 1) * 4.6;
  const z = (rand() * 2 - 1) * 4.6;
  const h = height(x, z);
  const t = (h - WATER) / 1.6;
  if (t < 0.08 || t > 0.5) continue;
  trees++;
  const treeH = 0.16 + rand() * 0.22;
  // trunk
  push({
    x, y: -(h + treeH * 0.35), z: -z,
    r: 0.32, g: 0.24, b: 0.16, a: 0.9,
    sx: 0.014, sy: treeH * 0.4, sz: 0.014,
    qw: 1, qx: 0, qy: 0, qz: 0,
  });
  // foliage: 3 stacked blobs
  const shade = 0.75 + rand() * 0.5;
  for (let k = 0; k < 3; k++) {
    const f = k / 2;
    push({
      x: x + (rand() - 0.5) * 0.03, y: -(h + treeH * (0.55 + 0.45 * f)), z: -z + (rand() - 0.5) * 0.03,
      r: 0.10 * shade, g: 0.34 * shade, b: 0.12 * shade, a: 0.85,
      sx: treeH * 0.42 * (1 - 0.28 * f), sy: treeH * 0.3, sz: treeH * 0.42 * (1 - 0.28 * f),
      qw: 1, qx: 0, qy: 0, qz: 0,
    });
  }
}

// ---- clouds -----------------------------------------------------------------
for (let i = 0; i < 40; i++) {
  const x = (rand() * 2 - 1) * 5.5;
  const z = (rand() * 2 - 1) * 5.5;
  const y = 2.2 + rand() * 0.9;
  push({
    x, y: -y, z: -z,
    r: 0.95, g: 0.96, b: 0.99, a: 0.22 + rand() * 0.2,
    sx: 0.5 + rand() * 0.7, sy: 0.08 + rand() * 0.08, sz: 0.35 + rand() * 0.5,
    qw: 1, qx: 0, qy: 0, qz: 0,
  });
}

// ---- write INRIA-style PLY ----------------------------------------------------
const props = [
  "x", "y", "z",
  "f_dc_0", "f_dc_1", "f_dc_2",
  "opacity",
  "scale_0", "scale_1", "scale_2",
  "rot_0", "rot_1", "rot_2", "rot_3",
];
const header =
  "ply\n" +
  "format binary_little_endian 1.0\n" +
  `element vertex ${splats.length}\n` +
  props.map((p) => `property float ${p}`).join("\n") + "\n" +
  "end_header\n";

const headerBytes = Buffer.from(header, "ascii");
const body = Buffer.alloc(splats.length * props.length * 4);
let off = 0;
const clampA = (a) => Math.min(0.995, Math.max(0.005, a));
for (const s of splats) {
  body.writeFloatLE(s.x, off); off += 4;
  body.writeFloatLE(s.y, off); off += 4;
  body.writeFloatLE(s.z, off); off += 4;
  body.writeFloatLE(dc(s.r), off); off += 4;
  body.writeFloatLE(dc(s.g), off); off += 4;
  body.writeFloatLE(dc(s.b), off); off += 4;
  body.writeFloatLE(logit(clampA(s.a)), off); off += 4;
  body.writeFloatLE(Math.log(s.sx), off); off += 4;
  body.writeFloatLE(Math.log(s.sy), off); off += 4;
  body.writeFloatLE(Math.log(s.sz), off); off += 4;
  body.writeFloatLE(s.qw, off); off += 4;
  body.writeFloatLE(s.qx, off); off += 4;
  body.writeFloatLE(s.qy, off); off += 4;
  body.writeFloatLE(s.qz, off); off += 4;
}
writeFileSync(new URL("demo.ply", import.meta.url), Buffer.concat([headerBytes, body]));
console.log(`demo.ply written: ${splats.length} splats, ${(headerBytes.length + body.length) / 1e6} MB`);
