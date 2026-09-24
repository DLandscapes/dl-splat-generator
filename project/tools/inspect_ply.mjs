// Reports what a splat .ply actually contains: header layout, spherical-harmonic
// degree, splat count, and the real extent of the scene.
//
// Useful before loading an unfamiliar capture -- the extent tells you whether
// the file is already metric (a room-sized scan spanning ~3 units is metres;
// one spanning ~0.05 or ~800 is not).
//
// Run: node inspect_ply.mjs <file.ply>
import { open } from "node:fs/promises";

const path = process.argv[2];
if (!path) {
  console.error("usage: node inspect_ply.mjs <file.ply>");
  process.exit(1);
}

const fh = await open(path, "r");
const { size } = await fh.stat();

// read enough for any plausible header
const head = Buffer.alloc(Math.min(65536, size));
await fh.read(head, 0, head.length, 0);
const headText = head.toString("latin1");
const endIdx = headText.indexOf("end_header");
if (!headText.startsWith("ply") || endIdx < 0) {
  console.error("Not a PLY file.");
  process.exit(1);
}
const dataStart = headText.indexOf("\n", endIdx) + 1;

const TYPE_SIZES = {
  char: 1, int8: 1, uchar: 1, uint8: 1,
  short: 2, int16: 2, ushort: 2, uint16: 2,
  int: 4, int32: 4, uint: 4, uint32: 4,
  float: 4, float32: 4, double: 8, float64: 8,
};

let format = null, count = 0, inVertex = false, stride = 0;
const props = [];
for (const line of headText.slice(0, endIdx).split(/\r?\n/)) {
  const t = line.trim().split(/\s+/);
  if (t[0] === "format") format = t[1];
  else if (t[0] === "element") {
    inVertex = t[1] === "vertex";
    if (inVertex) count = parseInt(t[2]);
    else if (count > 0) break;
  } else if (t[0] === "property" && inVertex) {
    props.push({ name: t[2], type: t[1], offset: stride, size: TYPE_SIZES[t[1]] });
    stride += TYPE_SIZES[t[1]];
  }
}

const names = new Set(props.map((p) => p.name));
const shRest = props.filter((p) => /^f_rest_\d+$/.test(p.name)).length;
// 3 colour channels per SH coefficient; degree d has (d+1)^2 - 1 extra coeffs
const shDegree = { 0: 0, 9: 1, 24: 2, 45: 3 }[shRest] ?? `? (${shRest} f_rest)`;

console.log(`file            ${path.split(/[\\/]/).pop()}`);
console.log(`size            ${(size / 1e6).toFixed(1)} MB`);
console.log(`format          ${format}`);
console.log(`splats          ${count.toLocaleString("en-US")}`);
console.log(`bytes/splat     ${stride}  (${props.length} properties)`);
console.log(`spherical harm. degree ${shDegree}`);
console.log(`3DGS layout     ${names.has("scale_0") && names.has("rot_0") ? "yes" : "no — plain point cloud"}`);

if (format !== "binary_little_endian") {
  console.log("\n(extent only computed for binary little-endian)");
  await fh.close();
  process.exit(0);
}

// stream through, reading only x/y/z of each splat
const xo = props.find((p) => p.name === "x").offset;
const CHUNK = 1 << 22;
const buf = Buffer.alloc(CHUNK);
let pos = dataStart, read = 0, carry = Buffer.alloc(0);
let minX = Infinity, minY = Infinity, minZ = Infinity;
let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
const xs = [], ys = [], zs = [];   // sampled, for a robust percentile extent

while (pos < size) {
  const { bytesRead } = await fh.read(buf, 0, CHUNK, pos);
  if (!bytesRead) break;
  pos += bytesRead;
  let data = carry.length ? Buffer.concat([carry, buf.subarray(0, bytesRead)]) : buf.subarray(0, bytesRead);
  const whole = Math.floor(data.length / stride);
  for (let i = 0; i < whole; i++) {
    const o = i * stride + xo;
    const x = data.readFloatLE(o), y = data.readFloatLE(o + 4), z = data.readFloatLE(o + 8);
    if (x < minX) minX = x; if (x > maxX) maxX = x;
    if (y < minY) minY = y; if (y > maxY) maxY = y;
    if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
    if ((read + i) % 37 === 0) { xs.push(x); ys.push(y); zs.push(z); }
  }
  read += whole;
  carry = Buffer.from(data.subarray(whole * stride));
}
await fh.close();

const pct = (arr, p) => { arr.sort((a, b) => a - b); return arr[Math.floor(p * (arr.length - 1))]; };
const span = (arr) => +(pct(arr, 0.99) - pct(arr, 0.01)).toFixed(3);

console.log(`splats read     ${read.toLocaleString("en-US")}`);
console.log(`full extent     X ${(maxX - minX).toFixed(2)}  Y ${(maxY - minY).toFixed(2)}  Z ${(maxZ - minZ).toFixed(2)}`);
console.log(`core extent     X ${span(xs)}  Y ${span(ys)}  Z ${span(zs)}   (1–99th percentile, ignores stray splats)`);
console.log(`\nIf that core extent matches the real object in metres, the capture is`);
console.log(`already metric — use "Scene is already in metres" rather than calibrating.`);
