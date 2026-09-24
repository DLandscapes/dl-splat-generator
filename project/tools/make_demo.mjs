// Generates the bundled test scenes, in the standard INRIA 3DGS .ply layout
// (binary_little_endian, float properties):
//
//   data/landform.ply     a laser-cut contour model of a landform: greyboard
//                         sheets on a base board, metric (see buildLandform)
//   data/calibration.ply  markers at exactly known separations, plus a rectangle
//                         of known area, so measurement accuracy is testable
//                         rather than eyeballed
//   data/calibration.markers.json  ground truth for the above
//
// Both are authored in the COLMAP-style Y-down convention that real 3DGS
// captures use, so the viewer's default "flip up-axis" renders them upright.
//
// Run: node make_demo.mjs [output folder]     (default: ../data/)
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SH_C0 = 0.28209479177387814;
const logit = (a) => Math.log(a / (1 - a));
const dc = (c) => (c - 0.5) / SH_C0;

const OUT = process.argv[2]
  ? pathToFileURL(resolve(process.argv[2]) + sep)
  : new URL("../data/", import.meta.url);

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

/* --------------------------------------------------------- contour landform */

// A laser-cut contour model, as DL-TerrainSlicer cuts them: a landform stacked
// from sheets of greyboard on a white base board, each sheet a flat terrace
// with a dark, laser-burnt cut edge. It replaced a synthetic island on
// 2026-09-24: the island (sea, sand, trees, clouds) looked like a game, and
// this is the house's own language -- the thing the tool's terrain models end
// up as. Metric: 12 x 9 m, 0.2 m sheets, 1 unit = 1 m.
//
// It has its OWN random stream, so it can change without moving a single byte
// of calibration.ply -- which the acceptance test depends on.

let landSeed = 20260924;
function landRand() {
  landSeed = (landSeed * 1664525 + 1013904223) >>> 0;
  return landSeed / 4294967296;
}

const SHEET = 0.2;                         // sheet thickness, m
const PLATE = { x0: -6, x1: 6, z0: -4.5, z1: 4.5 };
const STEP = 0.045;                        // splat spacing on the terraces, m

const bump = (x, z, cx, cz, sx, sz) =>
  Math.exp(-(((x - cx) ** 2) / (2 * sx * sx) + ((z - cz) ** 2) / (2 * sz * sz)));

/** Ground height before it is cut into sheets: two summits, a saddle between
 *  them and a shallow valley running out to the front edge. */
function landHeight(x, z) {
  const h = 2.5 * bump(x, z, -1.7, -0.5, 2.3, 1.9)
          + 1.5 * bump(x, z, 2.9, 1.3, 1.6, 1.4)
          + 0.5 * bump(x, z, 0.6, 0.4, 1.2, 2.6)
          - 0.45 * bump(x, z, 0.8, -3.2, 0.9, 2.4)
          + 0.12 * Math.sin(x * 1.3 + 0.4) * Math.cos(z * 1.1 - 0.2);
  // fade to the base board before the plate's edge, as a cut model does
  const edge = Math.min(x - PLATE.x0, PLATE.x1 - x, z - PLATE.z0, PLATE.z1 - z);
  return Math.max(0, h * Math.min(1, Math.max(0, (edge - 0.3) / 1.2)));
}

function buildLandform() {
  const out = [];
  const nx = Math.round((PLATE.x1 - PLATE.x0) / STEP);
  const nz = Math.round((PLATE.z1 - PLATE.z0) / STEP);
  const level = (i, j) => Math.floor(landHeight(PLATE.x0 + (i + 0.5) * STEP,
                                                PLATE.z0 + (j + 0.5) * STEP) / SHEET);
  const levels = [];
  for (let i = 0; i < nx; i++) {
    levels.push([]);
    for (let j = 0; j < nz; j++) levels[i].push(level(i, j));
  }
  const at = (i, j) => (i < 0 || j < 0 || i >= nx || j >= nz ? -1 : levels[i][j]);

  // board tones: a white base board, then greyboard alternating very slightly
  // sheet to sheet, the way two batches of board never quite match
  const tone = (k) => (k === 0 ? [0.88, 0.87, 0.84]
    : k % 2 ? [0.70, 0.68, 0.64] : [0.735, 0.715, 0.675]);
  const EDGE = [0.30, 0.28, 0.26];         // the laser-burnt cut edge

  for (let i = 0; i < nx; i++) {
    for (let j = 0; j < nz; j++) {
      const k = levels[i][j];
      const x = PLATE.x0 + (i + 0.5) * STEP, z = PLATE.z0 + (j + 0.5) * STEP;
      const [r, g, b] = tone(k);
      const n = (landRand() - 0.5) * 0.03;  // board fibre
      push(out, {
        x: x + (landRand() - 0.5) * STEP * 0.3, y: k * SHEET, z: z + (landRand() - 0.5) * STEP * 0.3,
        r: r + n, g: g + n, b: b + n, a: 0.97,
        sx: STEP * 0.78, sy: 0.004, sz: STEP * 0.78,
      });
      // a cut edge wherever a neighbour sits lower: a vertical strip of splats
      // on the boundary, from the neighbour's sheet up to this one's
      for (const [di, dj] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const kn = at(i + di, j + dj);
        if (kn >= k) continue;
        const bottom = kn < 0 ? -SHEET : kn * SHEET, top = k * SHEET;
        const steps = Math.max(1, Math.ceil((top - bottom) / 0.03));
        for (let s = 0; s < steps; s++) {
          const y = bottom + (s + 0.5) * (top - bottom) / steps;
          const e = (landRand() - 0.5) * 0.03;
          push(out, {
            x: x + di * STEP * 0.5, y, z: z + dj * STEP * 0.5,
            r: EDGE[0] + e, g: EDGE[1] + e, b: EDGE[2] + e, a: 0.97,
            sx: di ? 0.004 : STEP * 0.62, sy: (top - bottom) / steps * 0.62,
            sz: dj ? 0.004 : STEP * 0.62,
          });
        }
      }
    }
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

// The state the stream was in when this scene was first generated, after a
// synthetic island (removed 2026-09-24) had drawn from it. Starting here keeps
// calibration.ply byte-identical to the file the acceptance test was built on.
const CALIBRATION_SEED = 3834015543;

function buildCalibration() {
  seed = CALIBRATION_SEED;
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

const land = buildLandform();
let bytes = writePly(land, new URL("landform.ply", OUT));
console.log(`landform.ply     ${land.length} splats, ${(bytes / 1e6).toFixed(2)} MB`);

const calib = buildCalibration();
bytes = writePly(calib, new URL("calibration.ply", OUT));
console.log(`calibration.ply  ${calib.length} splats, ${(bytes / 1e6).toFixed(2)} MB`);

writeFileSync(fileURLToPath(new URL("calibration.markers.json", OUT)),
  JSON.stringify(TRUTH, null, 2));
console.log("calibration.markers.json written");
