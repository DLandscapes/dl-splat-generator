/* PLY reading.
 *
 * Spark handles real 3DGS files (INRIA .ply, .spz, .splat, .ksplat, .sog) far
 * better than we could, so this module is deliberately narrow: it reads the
 * header to decide which loader a file should go to, and fully parses the one
 * case Spark does not target -- plain point-cloud .ply with no per-point scale
 * or rotation. Those points are pushed into a SplatMesh as small round splats.
 *
 * Carried over from the v1 single-file viewer (see reference/v1-single-file.html).
 */

const TYPE_SIZES = {
  char: 1, int8: 1, uchar: 1, uint8: 1,
  short: 2, int16: 2, ushort: 2, uint16: 2,
  int: 4, int32: 4, uint: 4, uint32: 4,
  float: 4, float32: 4, double: 8, float64: 8,
};

export function parsePlyHeader(buffer) {
  const headBytes = new Uint8Array(buffer, 0, Math.min(buffer.byteLength, 65536));
  const headText = new TextDecoder("ascii").decode(headBytes);
  const endIdx = headText.indexOf("end_header");
  if (!headText.startsWith("ply") || endIdx < 0)
    throw new Error("Not a valid PLY file (missing ply/end_header).");
  const dataStart = headText.indexOf("\n", endIdx) + 1;
  const lines = headText.slice(0, endIdx).split(/\r?\n/);

  let format = null, vertexCount = 0, inVertex = false, stride = 0;
  const props = [];
  for (const line of lines) {
    const t = line.trim().split(/\s+/);
    if (t[0] === "format") format = t[1];
    else if (t[0] === "element") {
      inVertex = t[1] === "vertex";
      if (inVertex) vertexCount = parseInt(t[2]);
      else if (vertexCount > 0) break; // vertex block done; ignore later elements
    } else if (t[0] === "property" && inVertex) {
      if (t[1] === "list") throw new Error("PLY vertex element with list properties is not supported.");
      const size = TYPE_SIZES[t[1]];
      if (!size) throw new Error(`Unsupported PLY property type: ${t[1]}`);
      props.push({ name: t[2], type: t[1], offset: stride, size });
      stride += size;
    }
  }
  if (!format) throw new Error("PLY header has no format line.");
  if (format === "binary_big_endian") throw new Error("Big-endian PLY files are not supported.");
  if (!vertexCount) throw new Error("PLY file contains no vertices.");
  return { format, vertexCount, props, stride, dataStart };
}

/** True when the file carries per-splat scale + rotation, i.e. real 3DGS. */
export function isGaussianPly(header) {
  const names = new Set(header.props.map((p) => p.name));
  return names.has("scale_0") && names.has("rot_0");
}

function makeGetters(buffer, h) {
  const n = h.vertexCount;
  const byName = {};
  for (const p of h.props) byName[p.name] = p;

  if (h.format === "ascii") {
    const text = new TextDecoder().decode(new Uint8Array(buffer, h.dataStart));
    const tokens = text.trim().split(/\s+/);
    const pcount = h.props.length;
    if (tokens.length < n * pcount) throw new Error("ASCII PLY: not enough data values.");
    const cols = {};
    h.props.forEach((p, j) => {
      const arr = new Float32Array(n);
      for (let i = 0; i < n; i++) arr[i] = parseFloat(tokens[i * pcount + j]);
      cols[p.name] = arr;
    });
    return (name) => { const a = cols[name]; return a ? (i) => a[i] : null; };
  }

  const need = h.dataStart + n * h.stride;
  if (buffer.byteLength < need)
    throw new Error(`PLY file truncated: expected ${need} bytes, got ${buffer.byteLength}.`);

  const allFloat = h.props.every((p) => p.size === 4 && (p.type === "float" || p.type === "float32"));
  if (allFloat) {
    // fast path: slice for 4-byte alignment, then read one big Float32Array
    const f32 = new Float32Array(buffer.slice(h.dataStart, need));
    const sf = h.stride / 4;
    return (name) => {
      const p = byName[name];
      if (!p) return null;
      const off = p.offset / 4;
      return (i) => f32[i * sf + off];
    };
  }

  const dv = new DataView(buffer, h.dataStart);
  const stride = h.stride;
  return (name) => {
    const p = byName[name];
    if (!p) return null;
    const off = p.offset;
    switch (p.type) {
      case "float": case "float32": return (i) => dv.getFloat32(i * stride + off, true);
      case "double": case "float64": return (i) => dv.getFloat64(i * stride + off, true);
      case "uchar": case "uint8": return (i) => dv.getUint8(i * stride + off);
      case "char": case "int8": return (i) => dv.getInt8(i * stride + off);
      case "ushort": case "uint16": return (i) => dv.getUint16(i * stride + off, true);
      case "short": case "int16": return (i) => dv.getInt16(i * stride + off, true);
      case "uint": case "uint32": return (i) => dv.getUint32(i * stride + off, true);
      default: return (i) => dv.getInt32(i * stride + off, true);
    }
  };
}

/**
 * Parse a plain point-cloud PLY into flat arrays.
 * Returns { count, positions: Float32Array(3n), colors: Float32Array(3n), pointSize }
 * where pointSize is a sensible splat radius derived from the cloud's extent.
 */
export function parsePointCloud(buffer) {
  const h = parsePlyHeader(buffer);
  const n = h.vertexCount;
  const get = makeGetters(buffer, h);
  const byName = new Set(h.props.map((p) => p.name));

  const gx = get("x"), gy = get("y"), gz = get("z");
  if (!gx || !gy || !gz) throw new Error("PLY has no x/y/z vertex positions.");
  const gr = get("red"), gg = get("green"), gb = get("blue");
  const colorIsByte = byName.has("red") &&
    h.props.find((p) => p.name === "red").size === 1;

  const positions = new Float32Array(n * 3);
  const colors = new Float32Array(n * 3);
  let minX = Infinity, minY = Infinity, minZ = Infinity;
  let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;

  for (let i = 0; i < n; i++) {
    const x = gx(i), y = gy(i), z = gz(i);
    positions[3 * i] = x; positions[3 * i + 1] = y; positions[3 * i + 2] = z;
    if (x < minX) minX = x; if (x > maxX) maxX = x;
    if (y < minY) minY = y; if (y > maxY) maxY = y;
    if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;

    const s = colorIsByte ? 1 / 255 : 1;
    const r = gr ? gr(i) * s : 0.75;
    colors[3 * i] = r;
    colors[3 * i + 1] = gg ? gg(i) * s : r;
    colors[3 * i + 2] = gb ? gb(i) * s : r;
  }

  const diag = Math.hypot(maxX - minX, maxY - minY, maxZ - minZ) || 1;
  return { count: n, positions, colors, pointSize: diag * 0.0015 };
}
