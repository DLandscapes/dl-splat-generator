// Generates the bundled test scenes, in the standard INRIA 3DGS .ply layout
// (binary_little_endian, float properties):
//
//   data/demo.ply         synthetic island terrain, trees and cloud -- something
//                         to look at that exercises colour, opacity and depth
//   data/calibration.ply  markers at exactly known separations, plus a rectangle
//                         of known area, so measurement accuracy is testable
//                         rather than eyeballed
//   data/calibration.markers.json  ground truth for the above
//
// Both are authored in the COLMAP-style Y-down convention that real 3DGS
// captures use, so the viewer's default "flip up-axis" renders them upright.
//
// Run: node make_demo.mjs
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const SH_C0 = 0.28209479177387814;
const logit = (a) => Math.log(a / (1 - a));
const dc = (c) => (c - 0.5) / SH_C0;

const OUT = new URL("../data/", import.meta.url);

// deterministic PRNG, so regenerating gives byte-identical files
let seed = 1234567;
function rand() {
  seed = (seed * 1664525 + 1013904223) >>> 0;
  return seed / 4294967296;
}

/** Push a splat given in world (Y-up) coordinates; stored Y-down. */
function push(list, { x, y, z, r, g, b, a = 0.95, sx, sy, sz }) {
  list.push({ x, y: -y, z: -z, r, g, b, a, sx, sy, sz, qw: 1, qx: 0, qy: 0, qz: 0 });
}

/* ------------------------------------------------------------ demo terrain */

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

function buildDemo() {
  const out = [];
  const WATER = 0.02;
  const N = 230, EXT = 5, cell = (2 * EXT) / N;

  for (let i = 0; i < N; i++) {
    for (let j = 0; j < N; j++) {
      const x = -EXT + (i + 0.5 + (rand() - 0.5) * 0.7) * cell;
      const z = -EXT + (j + 0.5 + (rand() - 0.5) * 0.7) * cell;
      let h = height(x, z);
      let r, g, b, sy;
      if (h < WATER) {
        const depth = Math.min(1, (WATER - h) * 2.5);
        r = 0.10 - 0.04 * depth; g = 0.35 - 0.13 * depth; b = 0.55 - 0.12 * depth;
        h = WATER; sy = 0.012;
      } else {
        const t = Math.min(1, (h - WATER) / 1.6); // 0 shore .. 1 peak
        const n = (rand() - 0.5) * 0.06;
        if (t < 0.06) { r = 0.78 + n; g = 0.72 + n; b = 0.52 + n; }              // sand
        else if (t < 0.45) { r = 0.22 + 0.3 * t + n; g = 0.46 + 0.2 * t + n; b = 0.20 + n; }
        else if (t < 0.75) { r = 0.42 + n; g = 0.38 + n; b = 0.34 + n; }         // rock
        else { r = 0.92 + n; g = 0.93 + n; b = 0.95 + n; }                       // snow
        sy = 0.02 + 0.03 * rand();
      }
      push(out, {
        x, y: h, z, r, g, b, a: 0.92,
        sx: cell * (0.7 + rand() * 0.5), sy, sz: cell * (0.7 + rand() * 0.5),
      });
    }
  }

  let trees = 0;
  while (trees < 260) {
    const x = (rand() * 2 - 1) * 4.6;
    const z = (rand() * 2 - 1) * 4.6;
    const h = height(x, z);
    const t = (h - WATER) / 1.6;
    if (t < 0.08 || t > 0.5) continue;
    trees++;
    const treeH = 0.16 + rand() * 0.22;
    push(out, {
      x, y: h + treeH * 0.35, z,
      r: 0.32, g: 0.24, b: 0.16, a: 0.9,
      sx: 0.014, sy: treeH * 0.4, sz: 0.014,
    });
    const shade = 0.75 + rand() * 0.5;
    for (let k = 0; k < 3; k++) {
      const f = k / 2;
      push(out, {
        x: x + (rand() - 0.5) * 0.03,
        y: h + treeH * (0.55 + 0.45 * f),
        z: z + (rand() - 0.5) * 0.03,
        r: 0.10 * shade, g: 0.34 * shade, b: 0.12 * shade, a: 0.85,
        sx: treeH * 0.42 * (1 - 0.28 * f), sy: treeH * 0.3,
        sz: treeH * 0.42 * (1 - 0.28 * f),
      });
    }
  }

  for (let i = 0; i < 40; i++) {
    push(out, {
      x: (rand() * 2 - 1) * 5.5, y: 2.2 + rand() * 0.9, z: (rand() * 2 - 1) * 5.5,
      r: 0.95, g: 0.96, b: 0.99, a: 0.22 + rand() * 0.2,
      sx: 0.5 + rand() * 0.7, sy: 0.08 + rand() * 0.08, sz: 0.35 + rand() * 0.5,
    });
  }
  return out;
}

/* -------------------------------------------------------- calibration scene */

// Marker centres, in metres. The separations are exact by construction and are
// what the measurement acceptance test asserts against.
const MARK_Y = 0.15;      // marker centres float clear of the ground plane
const MARKERS = [
  { id: "A", p: [0, MARK_Y, 0], role: "origin" },
  { id: "B", p: [1, MARK_Y, 0], role: "span" },
  { id: "C", p: [5, MARK_Y, 0], role: "span" },
  { id: "D", p: [10, MARK_Y, 0], role: "span" },
  { id: "H0", p: [12, MARK_Y, 0], role: "height" },
  { id: "H1", p: [12, MARK_Y + 2, 0], role: "height" },
  { id: "R1", p: [0, MARK_Y, 4], role: "rect" },
  { id: "R2", p: [6, MARK_Y, 4], role: "rect" },
  { id: "R3", p: [6, MARK_Y, 7], role: "rect" },
  { id: "R4", p: [0, MARK_Y, 7], role: "rect" },
];
const TRUTH = {
  units: "metres",
  note: "Marker centres are exact. The figures below are what a correct "
      + "measurement must reproduce once the scene is calibrated 1 unit = 1 m.",
  markers: MARKERS,
  distances: [
    { from: "A", to: "B", metres: 1 },
    { from: "A", to: "C", metres: 5 },
    { from: "A", to: "D", metres: 10 },
  ],
  heights: [{ from: "H0", to: "H1", metres: 2 }],
  areas: [{ ring: ["R1", "R2", "R3", "R4"], squareMetres: 18 }],
};

/** A compact ball of tiny splats -- small enough that a click lands near centre. */
function marker(out, [x, y, z], [r, g, b]) {
  const RADIUS = 0.02;
  for (let i = 0; i < 90; i++) {
    const u = rand(), v = rand(), w = Math.cbrt(rand());
    const theta = 2 * Math.PI * u, phi = Math.acos(2 * v - 1);
    push(out, {
      x: x + RADIUS * w * Math.sin(phi) * Math.cos(theta),
      y: y + RADIUS * w * Math.cos(phi),
      z: z + RADIUS * w * Math.sin(phi) * Math.sin(theta),
      r, g, b, a: 0.99,
      sx: 0.008, sy: 0.008, sz: 0.008,
    });
  }
  // hairline stem to the ground, so the marker reads as located in space
  for (let i = 0; i < 24; i++) {
    const t = (i + 0.5) / 24;
    push(out, {
      x, y: y * (1 - t), z,
      r: 0.35, g: 0.33, b: 0.30, a: 0.9,
      sx: 0.004, sy: y / 48, sz: 0.004,
    });
  }
}

function buildCalibration() {
  const out = [];
  // flat, lightly mottled ground with a faint metre grid, so the known spacing
  // is legible on sight
  const X0 = -2, X1 = 14, Z0 = -2, Z1 = 9, STEP = 0.06;
  for (let x = X0; x <= X1; x += STEP) {
    for (let z = Z0; z <= Z1; z += STEP) {
      const n = (rand() - 0.5) * 0.05;
      const onGrid = Math.abs(x - Math.round(x)) < 0.012
                  || Math.abs(z - Math.round(z)) < 0.012;
      const base = onGrid ? 0.52 : 0.70;
      push(out, {
        x: x + (rand() - 0.5) * STEP * 0.4, y: 0, z: z + (rand() - 0.5) * STEP * 0.4,
        r: base + n, g: base + n - 0.01, b: base + n - 0.04, a: 0.95,
        sx: STEP * 0.9, sy: 0.006, sz: STEP * 0.9,
      });
    }
  }
  const COLOURS = {
    origin: [0.85, 0.18, 0.12],
    span: [0.85, 0.18, 0.12],
    height: [0.15, 0.55, 0.25],
    rect: [0.15, 0.35, 0.75],
  };
  for (const m of MARKERS) marker(out, m.p, COLOURS[m.role]);
  return out;
}

/* -------------------------------------------------------------- PLY writing */

const PROPS = [
  "x", "y", "z",
  "f_dc_0", "f_dc_1", "f_dc_2",
  "opacity",
  "scale_0", "scale_1", "scale_2",
  "rot_0", "rot_1", "rot_2", "rot_3",
];

function writePly(splats, url) {
  const header =
    "ply\n" +
    "format binary_little_endian 1.0\n" +
    `element vertex ${splats.length}\n` +
    PROPS.map((p) => `property float ${p}`).join("\n") + "\n" +
    "end_header\n";
  const headerBytes = Buffer.from(header, "ascii");
  const body = Buffer.alloc(splats.length * PROPS.length * 4);
  const clampA = (a) => Math.min(0.995, Math.max(0.005, a));
  let off = 0;
  const put = (v) => { body.writeFloatLE(v, off); off += 4; };
  for (const s of splats) {
    put(s.x); put(s.y); put(s.z);
    put(dc(s.r)); put(dc(s.g)); put(dc(s.b));
    put(logit(clampA(s.a)));
    put(Math.log(s.sx)); put(Math.log(s.sy)); put(Math.log(s.sz));
    put(s.qw); put(s.qx); put(s.qy); put(s.qz);
  }
  const path = fileURLToPath(url);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, Buffer.concat([headerBytes, body]));
  return headerBytes.length + body.length;
}

const demo = buildDemo();
let bytes = writePly(demo, new URL("demo.ply", OUT));
console.log(`demo.ply         ${demo.length} splats, ${(bytes / 1e6).toFixed(2)} MB`);

const calib = buildCalibration();
bytes = writePly(calib, new URL("calibration.ply", OUT));
console.log(`calibration.ply  ${calib.length} splats, ${(bytes / 1e6).toFixed(2)} MB`);

writeFileSync(fileURLToPath(new URL("calibration.markers.json", OUT)),
  JSON.stringify(TRUTH, null, 2));
console.log("calibration.markers.json written");
