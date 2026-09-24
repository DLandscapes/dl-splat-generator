/* The .dlscene sidecar, and the "export web scene" bundle.
 *
 * A .dlscene.json sits next to a splat file and records everything the viewer
 * knows that the splat file itself cannot: orientation, real-world scale,
 * measurements, annotations, viewpoints and the section state.
 *
 * The export writes a ZIP containing that scene, its splat file and a slim
 * read-only shell, so it runs from any static host and embeds in an iframe.
 * ZIP writing is done here (stored, uncompressed) to keep the app dependency
 * free -- splat files are already dense, so deflate would buy little.
 */

import { transcodeSpz } from "@sparkjsdev/spark";

export const SCENE_FORMAT = "dlscene";
export const SCENE_VERSION = 1;

/**
 * Re-encode splat bytes as SPZ.
 *
 * SPZ is Niantic's compressed splat format -- roughly a tenth the size of PLY
 * with no visible loss, and the only splat format on a formal standards track
 * (Khronos KHR_gaussian_splatting). For an exported scene this is the
 * difference between a bundle that can sit on a web page and one that cannot:
 * a 3M-splat capture is ~570 MB as PLY.
 *
 * Returns null if the transcode fails or fails to save space, so the caller can
 * fall back to shipping the original file.
 */
export async function toSpz(fileBytes, fileName) {
  try {
    const result = await transcodeSpz({
      inputs: [{ fileBytes, pathOrUrl: fileName }],
    });
    const out = result?.fileBytes;
    if (!out?.length || out.length >= fileBytes.length) return null;
    return out;
  } catch (err) {
    console.warn("SPZ transcode failed, shipping the original file:", err);
    return null;
  }
}

/** Files copied verbatim into an exported bundle. */
const BUNDLE_ASSETS = [
  "static/style.css",
  "static/viewer.js",
  "static/ply.js",
  "static/tools.js",
  "static/embed.js",
  "static/logo-dl.png",
  "static/fonts/SourceSans3-VariableFont_wght.ttf",
  "static/fonts/QuattrocentoSans-Regular.ttf",
  "static/fonts/QuattrocentoSans-Bold.ttf",
  "static/fonts/OFL.txt",
  "static/fonts/OFL-SourceSans3.txt",
  "static/vendor/three/three.module.min.js",
  "static/vendor/three/three.core.min.js",
  "static/vendor/three/addons/postprocessing/Pass.js",
  "static/vendor/three/LICENSE",
  "static/vendor/spark/spark.module.min.js",
  "static/vendor/spark/LICENSE",
];

export async function sha256(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Gather the full scene state into a plain object. */
export function buildScene({ viewer, tools, source, display }) {
  return {
    format: SCENE_FORMAT,
    version: SCENE_VERSION,
    app: "DL-SplatGenerator",
    saved: new Date().toISOString(),
    source,
    view: {
      flip: viewer.flip,
      // A saved viewpoint is yaw/pitch about the CURRENT vertical, so a scene
      // written with the horizon levelled has to say so or it reopens rotated.
      level: viewer.level,
      up: viewer.upFromCameras ? viewer._up.toArray() : null,
      home: viewer.home ?? viewer.getCameraState(),
      current: viewer.getCameraState(),
      display,
    },
    section: display.section,
    ...tools.toJSON(),
  };
}

/** Apply a loaded scene back onto the viewer and tools. */
export function applyScene(data, { viewer, tools }) {
  if (data?.format !== SCENE_FORMAT)
    throw new Error("Not a DL-SplatGenerator scene file.");
  if (data.version > SCENE_VERSION)
    throw new Error(`Scene was written by a newer version (v${data.version}).`);
  tools.fromJSON(data);
  if (typeof data.view?.flip === "boolean") viewer.setFlip(data.view.flip);
  // The up direction comes from the scene's own cameras when it has them; a
  // file written before this existed carries neither, and keeps world +Y.
  if (Array.isArray(data.view?.up) && data.view.up.length === 3) {
    viewer.setSceneUp(data.view.up);
    viewer.upFromCameras = true;
  }
  if (typeof data.view?.level === "boolean") viewer.setLevel(data.view.level);
  if (data.view?.home) viewer.home = data.view.home;
  if (data.view?.current) viewer.setCameraState(data.view.current);
  return data.view?.display ?? {};
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

/* ------------------------------------------------------------------- ZIP */

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[i] = c >>> 0;
  }
  return t;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function dosDateTime(d = new Date()) {
  const time = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
  const date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  return { time, date };
}

/**
 * Build a ZIP from [{ name, data: Uint8Array }] using the stored method.
 * Returns a Blob.
 */
export function makeZip(entries) {
  const enc = new TextEncoder();
  const { time, date } = dosDateTime();
  const parts = [];
  const central = [];
  let offset = 0;

  for (const { name, data } of entries) {
    const nameBytes = enc.encode(name);
    const crc = crc32(data);

    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);   // local file header signature
    local.setUint16(4, 20, true);           // version needed
    local.setUint16(6, 0, true);            // flags
    local.setUint16(8, 0, true);            // method: stored
    local.setUint16(10, time, true);
    local.setUint16(12, date, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, data.length, true); // compressed size
    local.setUint32(22, data.length, true); // uncompressed size
    local.setUint16(26, nameBytes.length, true);
    local.setUint16(28, 0, true);           // extra length

    parts.push(new Uint8Array(local.buffer), nameBytes, data);

    const cen = new DataView(new ArrayBuffer(46));
    cen.setUint32(0, 0x02014b50, true);     // central directory signature
    cen.setUint16(4, 20, true);             // version made by
    cen.setUint16(6, 20, true);             // version needed
    cen.setUint16(8, 0, true);
    cen.setUint16(10, 0, true);
    cen.setUint16(12, time, true);
    cen.setUint16(14, date, true);
    cen.setUint32(16, crc, true);
    cen.setUint32(20, data.length, true);
    cen.setUint32(24, data.length, true);
    cen.setUint16(28, nameBytes.length, true);
    cen.setUint32(42, offset, true);        // offset of local header
    central.push(new Uint8Array(cen.buffer), nameBytes);

    offset += 30 + nameBytes.length + data.length;
  }

  const centralSize = central.reduce((n, p) => n + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);       // end of central directory
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);

  return new Blob([...parts, ...central, new Uint8Array(end.buffer)],
    { type: "application/zip" });
}

/* ---------------------------------------------------------------- export */

function embedShell(title) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title.replace(/[<>&]/g, "")}</title>
<link rel="stylesheet" href="static/style.css">
<link rel="icon" href="static/logo-dl.png">
<script type="importmap">
{
  "imports": {
    "three": "./static/vendor/three/three.module.min.js",
    "three/addons/": "./static/vendor/three/addons/",
    "@sparkjsdev/spark": "./static/vendor/spark/spark.module.min.js"
  }
}
<\/script>
<style>
  body { display: block; }
  #viewport { position: fixed; inset: 0; }
  #views {
    position: absolute; left: 14px; bottom: 14px; display: flex; flex-wrap: wrap; gap: 6px;
    max-width: calc(100% - 28px);
  }
  #views button {
    background: rgba(253,252,249,.92); border: 1px solid var(--line);
    font-family: var(--font-head); font-size: .78rem;
  }
  #title {
    position: absolute; left: 14px; top: 14px; display: flex; align-items: center; gap: 8px;
    background: rgba(253,252,249,.92); border: 1px solid var(--line);
    border-radius: 6px; padding: 6px 10px;
    font-family: var(--font-head); font-size: .85rem; color: var(--ink);
  }
  #title img { width: 20px; height: 20px; }
</style>
</head>
<body>
<main id="viewport">
  <canvas id="canvas"></canvas>
  <div id="overlay"></div>
  <div id="title"><img src="static/logo-dl.png" alt=""><span></span></div>
  <div id="views"></div>
  <div id="status" class="hud" hidden></div>
</main>
<script type="module" src="static/embed.js"><\/script>
</body>
</html>
`;
}

/**
 * Build the exported bundle. `fetchAsset` resolves an app-relative path to a
 * Uint8Array (normally a fetch against the running app).
 */
export async function exportWebScene({ scene, splatBytes, splatName, title,
                                       fetchAsset, compress = true,
                                       onProgress = null }) {
  const enc = new TextEncoder();
  const originalName = splatName;
  const originalSize = splatBytes.length;

  if (compress && !/\.spz$/i.test(splatName)) {
    onProgress?.("Compressing to SPZ…");
    const spz = await toSpz(splatBytes, splatName);
    if (spz) {
      // The embed shell loads scene.source.name, so it has to point at the
      // file we actually ship. The original is recorded for provenance.
      splatBytes = spz;
      splatName = splatName.replace(/\.[^.]+$/, "") + ".spz";
      scene.source = {
        ...scene.source,
        name: splatName,
        bytes: spz.length,
        original: { name: originalName, bytes: originalSize,
                    sha256: scene.source?.sha256 },
      };
    }
  }

  const entries = [
    { name: "index.html", data: enc.encode(embedShell(title)) },
    { name: "scene.json", data: enc.encode(JSON.stringify(scene, null, 2)) },
    { name: splatName, data: splatBytes },
    {
      name: "README.txt",
      data: enc.encode(
        `${title}\n\n` +
        "Exported from DL-SplatGenerator.\n\n" +
        "Serve this folder over HTTP (it will not run from a file:// URL, as\n" +
        "browsers block ES modules there), then embed it with:\n\n" +
        `  <iframe src="path/to/index.html" style="width:100%;aspect-ratio:16/9;border:0"\n` +
        `          allowfullscreen loading="lazy"></iframe>\n\n` +
        "Bundled: three.js and Spark, both MIT -- see static/vendor/*/LICENSE.\n"),
    },
  ];
  onProgress?.("Collecting viewer files…");
  for (const path of BUNDLE_ASSETS) {
    entries.push({ name: path, data: await fetchAsset(path) });
  }
  onProgress?.("Packing…");
  const zip = makeZip(entries);
  zip.splatStats = {
    name: splatName,
    bytes: splatBytes.length,
    originalName,
    originalBytes: originalSize,
    compressed: splatName !== originalName,
  };
  return zip;
}
