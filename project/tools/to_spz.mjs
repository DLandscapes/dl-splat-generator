// Transcode a splat file to SPZ, headless.
//
// SPZ is Niantic's compressed splat format -- roughly a tenth the size of PLY
// with no visible loss, and the only splat format on a formal standards track
// (Khronos KHR_gaussian_splatting). Compressing at the end of the capture
// pipeline means the viewer loads ~50 MB instead of ~570 MB.
//
//   node to_spz.mjs <in.ply> [out.spz]
//
// Uses the same vendored Spark the browser uses. Node resolves Spark's bare
// "three" import through project/node_modules/three, which is a shim pointing
// back at static/vendor/three.
import { readFileSync, writeFileSync } from "node:fs";
import { transcodeSpz } from "../static/vendor/spark/spark.module.min.js";

const [input, output] = process.argv.slice(2);
if (!input) {
  console.error("usage: node to_spz.mjs <in.ply> [out.spz]");
  process.exit(1);
}
const outPath = output || input.replace(/\.[^.]+$/, "") + ".spz";

const bytes = new Uint8Array(readFileSync(input));
const started = Date.now();
const result = await transcodeSpz({
  inputs: [{ fileBytes: bytes, pathOrUrl: input }],
});
const out = result?.fileBytes;

if (!out?.length) {
  console.error("FAILED: transcode produced no output");
  process.exit(2);
}
if (out.length >= bytes.length) {
  console.error(`FAILED: SPZ (${out.length}) is not smaller than the input `
    + `(${bytes.length}); keeping the original`);
  process.exit(3);
}

writeFileSync(outPath, out);
console.log(JSON.stringify({
  input: input,
  output: outPath,
  inputBytes: bytes.length,
  outputBytes: out.length,
  savingPct: +(100 - (100 * out.length) / bytes.length).toFixed(1),
  ms: Date.now() - started,
  clippedCount: result.clippedCount ?? 0,
}));
