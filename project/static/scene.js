/* The .dlscene sidecar, a download helper, and a ZIP writer.
 *
 * A .dlscene.json sits next to a splat file and records everything the viewer
 * knows that the splat file itself cannot: orientation, real-world scale,
 * measurements, annotations, viewpoints and the section state.
 *
 * ZIP writing is done here (stored, uncompressed) to keep the app dependency
 * free; the still-image export uses it for "one per viewpoint".
 *
 * (An "export web scene" bundle -- the scene, its splat as SPZ and a read-only
 * shell, for a static host -- lived here until 2026-09-24, when Marc had it
 * removed. It is in the git history at 43b4826 and earlier.)
 */

export const SCENE_FORMAT = "dlscene";
export const SCENE_VERSION = 1;

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
