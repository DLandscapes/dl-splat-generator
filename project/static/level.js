/* Which way is up in a video scene -- the vertical the view orbits about, the
 * plan looks down along, and the sun is reckoned against.
 *
 * A camera solve has no gravity: its axes are arbitrary. Until 2026-09-26 the
 * viewer took the MEAN of the capture cameras' up vectors. That is right when
 * the phone looks ahead, and wrong when it looks down at the ground: a camera
 * pitched 70° down has its "up" pointing nearly forward, and the ground came
 * out standing on edge (student capture A: 78.6° off).
 *
 * Measured on six captures (scratch up_analysis.py, 2026-09-26), against the
 * dominant plane of each scan:
 *
 *                         look down   mean-up   held-level   ground plane
 *   student capture A       73°        78.6°      43.8°        (reference)
 *   student capture B         39°        22.1°       8.8°
 *   IMG_1988                30°        27.3°      18.6°
 *   IMG_1779                 0.5°       5.7°       3.5°        = its 5 % slope
 *     (the "held-level" column here is the first, walk-horizontal form of the
 *      straight-walk branch -- replaced the same day, see below)
 *   sample-netherlands       —          8.5°       8.5°
 *   IMG_8950_clean           —          7.1°       5.6°
 *
 * Two clues, and which one to trust depends on how the phone was held:
 *
 *   HELD LEVEL  A phone is held without tilting it sideways (measured: 0.5-2°
 *               rms on every capture), so each frame's RIGHT vector is
 *               horizontal however far it points down. The vertical is the
 *               direction most perpendicular to all of them (the smallest
 *               eigenvector of sum r r^T). It keeps a real slope. A walk that
 *               never turns leaves the tilt along it open; there the cameras'
 *               mean up, sideways part removed ("held level, walk straight").
 *   THE GROUND  The dominant plane of the scan (RANSAC over splat centres),
 *               taken only if it is under the cameras and holds most of the
 *               scene. It makes that plane level, so a slope is flattened.
 *
 *   Filming AHEAD (cameras look < 20° down onto the plane): held level.
 *   Filming DOWN at the ground: the ground -- pointing down makes "held
 *   level" meaningless (turning the phone about a vertical view axis keeps its
 *   right vector flat whatever the roll).
 *
 * Dependency-free: plain arrays in, plain arrays out.
 */

const DEG = 180 / Math.PI;
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a) => { const l = Math.hypot(...a); return l > 1e-12 ? a.map((x) => x / l) : null; };
const neg = (a) => a.map((x) => -x);

export const LOOK_DOWN_FOR_GROUND = 20;   // degrees: steeper than this, the ground rules
export const GROUND_SHARE = 0.6;          // the plane must hold this share of the scan

/** Smallest-eigenvalue eigenvector of a symmetric 3x3 (power iteration on the shifted matrix). */
function smallestEigen(M) {
  const tr = M[0][0] + M[1][1] + M[2][2];
  const S = M.map((row, i) => row.map((x, j) => (i === j ? tr : 0) - x));   // tr*I - M
  let v = [0.577, 0.577, 0.577];
  for (let k = 0; k < 200; k++) {
    const w = S.map((row) => dot(row, v));
    const n = norm(w);
    if (!n) break;
    v = n;
  }
  const lambdas = [dot(v, M.map((row) => dot(row, v)))];
  return { v, lambda: lambdas[0], trace: tr };
}

/** Eigenvalues of a symmetric 3x3, ascending (the closed form for symmetric matrices). */
function eigenvalues3(A) {
  const p1 = A[0][1] ** 2 + A[0][2] ** 2 + A[1][2] ** 2;
  const q = (A[0][0] + A[1][1] + A[2][2]) / 3;
  if (p1 < 1e-18) return [A[0][0], A[1][1], A[2][2]].sort((a, b) => a - b);
  const p2 = (A[0][0] - q) ** 2 + (A[1][1] - q) ** 2 + (A[2][2] - q) ** 2 + 2 * p1;
  const p = Math.sqrt(p2 / 6);
  const B = A.map((row, i) => row.map((x, j) => (x - (i === j ? q : 0)) / p));
  const det = B[0][0] * (B[1][1] * B[2][2] - B[1][2] * B[2][1])
    - B[0][1] * (B[1][0] * B[2][2] - B[1][2] * B[2][0])
    + B[0][2] * (B[1][0] * B[2][1] - B[1][1] * B[2][0]);
  const phi = Math.acos(Math.max(-1, Math.min(1, det / 2))) / 3;
  const big = q + 2 * p * Math.cos(phi);
  const small = q + 2 * p * Math.cos(phi + 2 * Math.PI / 3);
  return [small, 3 * q - big - small, big];
}

/** A small seeded random generator, so the same scene levels the same way every time. */
function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
}

/** The dominant plane through `points` (RANSAC): { n, share } or null. */
export function dominantPlane(points, { trials = 600, tolerance = 0.02 } = {}) {
  if (points.length < 50) return null;
  let lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (const p of points) for (let i = 0; i < 3; i++) { lo[i] = Math.min(lo[i], p[i]); hi[i] = Math.max(hi[i], p[i]); }
  const tol = tolerance * Math.max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]);
  const rand = rng(12345);
  const pick = () => points[Math.floor(rand() * points.length)];
  let best = null, bestCount = 0;
  for (let t = 0; t < trials; t++) {
    const a = pick(), b = pick(), c = pick();
    const n = norm(cross(sub(b, a), sub(c, a)));
    if (!n) continue;
    const d = dot(n, a);
    let count = 0;
    for (const p of points) if (Math.abs(dot(n, p) - d) < tol) count++;
    if (count > bestCount) { bestCount = count; best = { n, d }; }
  }
  if (!best) return null;
  // Refine: a three-point plane is only as good as its three points. Refit
  // through ALL its inliers by least squares (the normal is the covariance's
  // smallest eigenvector), twice, re-selecting inliers each time.
  for (let round = 0; round < 2; round++) {
    const inl = points.filter((p) => Math.abs(dot(best.n, p) - best.d) < tol);
    if (inl.length < 10) break;
    const c = [0, 1, 2].map((i) => inl.reduce((s, p) => s + p[i], 0) / inl.length);
    const C = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
    for (const p of inl) {
      const q = sub(p, c);
      for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) C[i][j] += q[i] * q[j];
    }
    const n = smallestEigen(C).v;
    best = { n, d: dot(n, c) };
  }
  bestCount = points.filter((p) => Math.abs(dot(best.n, p) - best.d) < tol).length;
  return { n: best.n, d: best.d, share: bestCount / points.length };
}

/**
 * The scene's vertical from its capture cameras ({position, direction, up}
 * in world space) and a sample of splat centres (world space).
 * Returns { up, method, lookDown, detail } -- method is "ground", "held level"
 * or "mean up" (too little to go on).
 */
export function estimateUp(cams, points) {
  const U = cams.map((c) => norm(c.up)).filter(Boolean);
  const D = cams.map((c) => norm(c.direction)).filter(Boolean);
  if (!U.length || U.length !== D.length) return null;
  const meanUp = norm(U.reduce((s, u) => [s[0] + u[0], s[1] + u[1], s[2] + u[2]], [0, 0, 0]));
  const R = D.map((d, i) => norm(cross(d, U[i]))).filter(Boolean);

  // -- held level: most perpendicular to every right vector
  const M = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (const r of R) for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) M[i][j] += r[i] * r[j] / R.length;
  const { v } = smallestEigen(M);
  // middle / largest eigenvalue: 0 when every right vector points one way
  // (a walk that never turned), larger as they spread
  const [, mid, top] = eigenvalues3(M);
  const spread = mid / Math.max(1e-12, top);
  let held = v;
  let heldHow = "right vectors";
  let method = "held level";
  if (!(spread > 0.02)) {
    // A walk that never turned leaves the tilt ALONG it open: "the path
    // slopes" and "the phone pointed a little down" look the same. Until
    // 2026-09-26 this took the walk as horizontal (right x walk), which
    // erases a real slope -- IMG_1779 came out 4° flatter, and a terrain
    // model must not do that. Now: the cameras' mean up with its sideways
    // component removed -- the phone held level ON AVERAGE, which keeps the
    // slope and is off by the average pitch. Same rule in tools/level.py.
    const meanR = norm(R.reduce((s, r) => [s[0] + r[0], s[1] + r[1], s[2] + r[2]], [0, 0, 0]));
    const k = meanR ? dot(meanUp, meanR) : 0;
    const c = meanR && norm([meanUp[0] - k * meanR[0], meanUp[1] - k * meanR[1], meanUp[2] - k * meanR[2]]);
    if (c) { held = c; heldHow = "mean up, sideways tilt removed"; method = "held level, walk straight"; }
  }
  if (dot(held, meanUp) < 0) held = neg(held);

  // -- the ground
  const plane = dominantPlane(points);
  let ground = null, lookDown = null;
  if (plane) {
    ground = dot(plane.n, meanUp) < 0 ? neg(plane.n) : plane.n;
    const downs = D.map((d) => Math.asin(Math.max(-1, Math.min(1, -dot(d, ground)))) * DEG).sort((a, b) => a - b);
    lookDown = downs[Math.floor(downs.length / 2)];
    // the cameras must be ABOVE it, and it must hold most of the scan
    const above = cams.filter((c) => dot(c.position, ground) > plane.d * Math.sign(dot(plane.n, ground))).length;
    const ok = plane.share >= GROUND_SHARE && above >= 0.8 * cams.length;
    if (ok && lookDown >= LOOK_DOWN_FOR_GROUND) {
      return { up: ground, method: "ground", lookDown, meanUp, held,
               detail: { share: plane.share, heldHow } };
    }
  }
  return { up: held, method, lookDown, meanUp, held, ground,
           detail: { share: plane?.share ?? 0, heldHow, spread } };
}

/** Angle between two directions in degrees (sign-free). */
export function angleDeg(a, b) {
  return Math.acos(Math.min(1, Math.abs(dot(norm(a), norm(b))))) * DEG;
}

/** Up from three picked points on level ground (the manual fallback). */
export function upFromThreePoints(a, b, c, hint) {
  const n = norm(cross(sub(b, a), sub(c, a)));
  if (!n) return null;
  return hint && dot(n, hint) < 0 ? neg(n) : n;
}
