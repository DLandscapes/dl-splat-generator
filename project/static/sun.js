/* The sun at the moment of capture, and which way north is in the scene.
 *
 * THE SUN. Where the sun stands for a place and a time is astronomy, not
 * estimation: this is the NOAA solar-position algorithm (the equations behind
 * NOAA's Solar Calculator, after Meeus, "Astronomical Algorithms"), good to
 * about 0.01° for dates near today, with the standard atmospheric refraction.
 * Checked in tools/sun_test.mjs against the reference case of NREL's Solar
 * Position Algorithm (Reda & Andreas 2004).
 *
 * NORTH. A splat scene has no compass: a camera solve fixes shape, not
 * orientation. North is therefore SET, three ways, each with its own error:
 *   compass   a photograph records the direction the phone faced
 *             (GPSImgDirection); a phone compass is often several degrees off,
 *             and the photo is taken as level
 *   shadow    click the top of something upright and the tip of its shadow:
 *             the ray between them IS the sun's direction, whose compass
 *             bearing is known for that moment -- so north follows. It also
 *             measures the sun's height, a check on the scene's level
 *   by hand   turn it until it agrees with a map
 *
 * Dependency-free (plain arrays), so the same code runs in node for the test.
 * Vectors are [x, y, z] in whichever right-handed frame the caller uses; `up`
 * and `north` must be in the same one. Azimuths are degrees clockwise from
 * north, as on a compass; elevation is degrees above the horizon.
 */

const RAD = Math.PI / 180;
const DEG = 180 / Math.PI;

/**
 * Sun position for a UTC instant (ms since the epoch) at latitude/longitude
 * (degrees, north and east positive). Returns { azimuth, elevation, zenith }
 * in degrees; elevation includes refraction (the sun you would see).
 */
export function sunPosition(utcMs, lat, lon) {
  const jd = utcMs / 86400000 + 2440587.5;
  const T = (jd - 2451545) / 36525;                                // Julian centuries
  const L0 = mod(280.46646 + T * (36000.76983 + T * 0.0003032), 360);
  const M = 357.52911 + T * (35999.05029 - 0.0001537 * T);
  const e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T);
  const C = Math.sin(M * RAD) * (1.914602 - T * (0.004817 + 0.000014 * T))
    + Math.sin(2 * M * RAD) * (0.019993 - 0.000101 * T)
    + Math.sin(3 * M * RAD) * 0.000289;
  const omega = 125.04 - 1934.136 * T;
  const lambda = L0 + C - 0.00569 - 0.00478 * Math.sin(omega * RAD);  // apparent longitude
  const eps0 = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60;
  const eps = eps0 + 0.00256 * Math.cos(omega * RAD);
  const decl = Math.asin(Math.sin(eps * RAD) * Math.sin(lambda * RAD));   // radians

  const y = Math.tan(eps * RAD / 2) ** 2;
  const eqTime = 4 * DEG * (y * Math.sin(2 * L0 * RAD) - 2 * e * Math.sin(M * RAD)
    + 4 * e * y * Math.sin(M * RAD) * Math.cos(2 * L0 * RAD)
    - 0.5 * y * y * Math.sin(4 * L0 * RAD) - 1.25 * e * e * Math.sin(2 * M * RAD));  // minutes

  const minutesUtc = mod(utcMs / 60000, 1440);
  const trueSolar = mod(minutesUtc + eqTime + 4 * lon, 1440);
  let hourAngle = trueSolar / 4 - 180;
  if (hourAngle < -180) hourAngle += 360;

  const phi = lat * RAD;
  const cosZ = clamp(Math.sin(phi) * Math.sin(decl)
    + Math.cos(phi) * Math.cos(decl) * Math.cos(hourAngle * RAD), -1, 1);
  const zen = Math.acos(cosZ) * DEG;
  const sinZ = Math.sin(zen * RAD);
  let azimuth;
  if (Math.abs(Math.cos(phi) * sinZ) < 1e-9) {
    azimuth = phi > 0 ? 180 : 0;                  // sun at the zenith or a pole: any bearing
  } else {
    const a = Math.acos(clamp((Math.sin(phi) * cosZ - Math.sin(decl)) / (Math.cos(phi) * sinZ), -1, 1)) * DEG;
    azimuth = hourAngle > 0 ? mod(a + 180, 360) : mod(540 - a, 360);
  }
  const geometric = 90 - zen;
  const elevation = geometric + refraction(geometric);
  return { azimuth, elevation, zenith: 90 - elevation };
}

/** Standard atmospheric refraction in degrees (NOAA's approximation). */
function refraction(el) {
  if (el > 85) return 0;
  const t = Math.tan(el * RAD);
  let arcsec;
  if (el > 5) arcsec = 58.1 / t - 0.07 / t ** 3 + 0.000086 / t ** 5;
  else if (el > -0.575) arcsec = 1735 + el * (-518.2 + el * (103.4 + el * (-12.79 + el * 0.711)));
  else arcsec = -20.772 / t;
  return arcsec / 3600;
}

/**
 * A capture time as the metadata gives it -- "2026-09-11T11:22:17+0200",
 * "…+02:00", "…Z", or with no zone at all (EXIF without OffsetTimeOriginal).
 * Returns { date: "YYYY-MM-DD", time: "HH:MM:SS", offsetMin } with offsetMin
 * null when the file does not say, or null when it is not a time.
 */
export function parseWhen(s) {
  const m = String(s || "").match(
    /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?/);
  if (!m) return null;
  let offsetMin = null;
  if (m[7] === "Z") offsetMin = 0;
  else if (m[7]) {
    const sign = m[7][0] === "-" ? -1 : 1;
    const digits = m[7].slice(1).replace(":", "");
    offsetMin = sign * (Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2, 4)));
  }
  return { date: `${m[1]}-${m[2]}-${m[3]}`, time: `${m[4]}:${m[5]}:${m[6] || "00"}`, offsetMin };
}

/** Local date + time + offset (minutes east of UTC) -> UTC milliseconds. */
export function toUtcMs(date, time, offsetMin) {
  const [Y, Mo, D] = date.split("-").map(Number);
  const [h, mi, se] = (time + ":00:00").split(":").map(Number);
  return Date.UTC(Y, Mo - 1, D, h, mi, se || 0) - (offsetMin || 0) * 60000;
}

/** "+02:00" <-> 120 */
export function formatOffset(min) {
  const sign = min < 0 ? "-" : "+", a = Math.abs(min);
  return `${sign}${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}`;
}
export function parseOffset(s) {
  const m = String(s || "").trim().match(/^(?:UTC)?\s*([+-])(\d{1,2})(?::?(\d\d))?$/i);
  if (!m) return null;
  return (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3] || 0));
}

/** 158 -> "south-southeast" */
export function compassWord(az) {
  const names = ["north", "north-northeast", "northeast", "east-northeast", "east",
    "east-southeast", "southeast", "south-southeast", "south", "south-southwest",
    "southwest", "west-southwest", "west", "west-northwest", "northwest", "north-northwest"];
  return names[Math.round(mod(az, 360) / 22.5) % 16];
}

/* ------------------------------------------------------------ geometry */

export const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
export const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
export const norm = (a) => { const l = Math.hypot(...a); return l > 1e-12 ? a.map((x) => x / l) : null; };

/** `v` with its component along unit `up` removed, normalised (null if vertical). */
export function horizontal(v, up) {
  const k = dot(v, up);
  return norm([v[0] - k * up[0], v[1] - k * up[1], v[2] - k * up[2]]);
}

/**
 * Rotate `v` by `deg` about unit axis `k`, right-handed -- about an UP axis
 * that is anticlockwise seen from above, the opposite way to a compass.
 */
export function rotate(v, k, deg) {
  const c = Math.cos(deg * RAD), s = Math.sin(deg * RAD), kv = cross(k, v), d = dot(k, v);
  return [0, 1, 2].map((i) => v[i] * c + kv[i] * s + k[i] * d * (1 - c));
}

/**
 * North from a horizontal direction whose compass bearing is known: a photo's
 * view direction and its heading, or the direction towards the sun and its
 * azimuth. A bearing runs clockwise from north, so north is that direction
 * turned back ANTIclockwise by the bearing -- a positive right-handed turn
 * about up.
 */
export function northFromBearing(direction, bearingDeg, up) {
  const h = horizontal(direction, up);
  return h && rotate(h, up, bearingDeg);
}

/**
 * North from a shadow: `top` of something upright and the `tip` of its shadow
 * (scene points), and the sun's computed azimuth at that moment. The ray from
 * the tip to the top points at the sun. Also returns the sun's height as the
 * scene measures it -- compared with the computed one, a check on the level.
 */
export function northFromShadow(top, tip, up, sunAzimuth) {
  const ray = norm([top[0] - tip[0], top[1] - tip[1], top[2] - tip[2]]);
  if (!ray) return null;
  const measuredElevation = Math.asin(clamp(dot(ray, up), -1, 1)) * DEG;
  const north = northFromBearing(ray, sunAzimuth, up);
  return north && { north, measuredElevation };
}

/** Unit vector TOWARDS the sun, in the frame of `up` and `north`. */
export function sunVector(azimuth, elevation, up, north) {
  const east = cross(north, up);
  const ce = Math.cos(elevation * RAD), se = Math.sin(elevation * RAD);
  const ca = Math.cos(azimuth * RAD), sa = Math.sin(azimuth * RAD);
  return [0, 1, 2].map((i) => ce * (ca * north[i] + sa * east[i]) + se * up[i]);
}

/**
 * The compass bearing of horizontal direction `dir`, given north: degrees
 * clockwise from north (0..360).
 */
export function bearingOf(dir, up, north) {
  const h = horizontal(dir, up);
  if (!h) return null;
  const east = cross(north, up);
  return mod(Math.atan2(dot(h, east), dot(h, north)) * DEG, 360);
}

function mod(a, n) { return ((a % n) + n) % n; }
function clamp(x, lo, hi) { return Math.min(hi, Math.max(lo, x)); }
