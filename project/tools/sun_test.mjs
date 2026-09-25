// Checks for static/sun.js -- the sun's position and the geometry of north.
//   node project/tools/sun_test.mjs
// Expected values were fixed BEFORE the first run: a published reference case,
// two consequences of the solstice at Tromsø that follow from geometry alone,
// and synthetic scenes whose north is known by construction.
import {
  sunPosition, parseWhen, toUtcMs, parseOffset, formatOffset, compassWord,
  northFromBearing, northFromShadow, sunVector, bearingOf, rotate, norm, dot,
} from "../static/sun.js";

const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok: !!ok, detail });
const near = (a, b, tol) => Math.abs(a - b) <= tol;
const vnear = (a, b, tol) => a.every((x, i) => Math.abs(x - b[i]) <= tol);

// 1. NREL SPA reference case (Reda & Andreas 2004, Table A4.1): Golden, CO,
//    17 Oct 2003 12:30:30 local (UTC-7) -> zenith 50.11162°, azimuth 194.34024°.
//    SPA's refraction there uses 820 mbar / 11 °C; NOAA's the standard
//    atmosphere, a few thousandths of a degree apart at 40°. Pass: within 0.05°.
{
  const t = toUtcMs("2003-10-17", "12:30:30", -7 * 60);
  const s = sunPosition(t, 39.742476, -105.1786);
  check("SPA reference: zenith 50.11162°", near(s.zenith, 50.11162, 0.05), s.zenith.toFixed(5));
  check("SPA reference: azimuth 194.34024°", near(s.azimuth, 194.34024, 0.05), s.azimuth.toFixed(5));
}

// 2. Midnight sun at Tromsø (69.65 N, 18.96 E) on 21 Jun 2026: the lowest sun
//    of the night is 69.65 + 23.44 - 90 = 3.09° geometric, + ~0.23° refraction.
//    Pass: the night's minimum lies in 3.1..3.5°.
{
  let lo = 90;
  for (let m = 0; m < 6 * 60; m += 2) {
    const t = Date.UTC(2026, 5, 20, 21, 0) + m * 60000;
    lo = Math.min(lo, sunPosition(t, 69.65, 18.96).elevation);
  }
  check("Tromsø, midsummer night: sun stays up, lowest 3.1–3.5°", lo > 3.1 && lo < 3.5, lo.toFixed(3));
}

// 3. Polar night at Tromsø on 21 Dec 2026: the highest sun of the day is
//    90 - 69.65 - 23.44 = -3.09° geometric. Pass: the day's maximum is below 0.
{
  let hi = -90;
  for (let m = 0; m < 24 * 60; m += 5) {
    hi = Math.max(hi, sunPosition(Date.UTC(2026, 11, 21) + m * 60000, 69.65, 18.96).elevation);
  }
  check("Tromsø, midwinter: sun never rises", hi < 0, hi.toFixed(3));
}

// 4. Times as the metadata writes them.
{
  const a = parseWhen("2026-09-11T11:22:17+0200");
  check("parse Apple video time", a && a.date === "2026-09-11" && a.time === "11:22:17" && a.offsetMin === 120,
    JSON.stringify(a));
  const b = parseWhen("2026-09-11T09:22:17.000000Z");
  check("parse ffprobe UTC time", b && b.offsetMin === 0 && b.time === "09:22:17", JSON.stringify(b));
  const c = parseWhen("2026-09-24T19:54:03");
  check("EXIF without a zone: offset unknown", c && c.offsetMin === null, JSON.stringify(c));
  const d = parseWhen("2026-09-24T19:54:03-03:30");
  check("negative half-hour offset", d && d.offsetMin === -210, JSON.stringify(d));
  check("same instant from both", toUtcMs(a.date, a.time, a.offsetMin) === toUtcMs(b.date, b.time, b.offsetMin));
  check("offset text round trip", parseOffset(formatOffset(-210)) === -210 && parseOffset("UTC+2") === 120);
  check("compass words", compassWord(158) === "south-southeast" && compassWord(359) === "north");
}

// 5. A photo looking along +Z with up = -Y (the scene's own COLMAP frame):
//    facing east (heading 90°) puts north to the photo's LEFT, i.e. -X.
{
  const n = northFromBearing([0, 0, 1], 90, [0, -1, 0]);
  check("photo facing east: north is to its left", vnear(n, [-1, 0, 0], 1e-12), n.map((x) => x.toFixed(6)));
  const n0 = northFromBearing([0, 0, 1], 0, [0, -1, 0]);
  const eastward = bearingOf([1, 0, 0], [0, -1, 0], n0);
  check("photo facing north: the photo's right is east (90°)", near(eastward, 90, 1e-9), eastward);
}

// 6. A shadow in a tilted, turned scene: pick an arbitrary up and north, put
//    the sun at a known azimuth/elevation, drop a pole's shadow, recover north.
{
  const up = norm([0.12, 0.97, -0.21]);
  const north = norm(rotate([1, 0, 0].map((x, i) => x - dot([1, 0, 0], up) * up[i]), up, 37));
  const az = 213.4, el = 27.9;
  const toSun = sunVector(az, el, up, north);
  const tip = [2.5, -1.2, 4.0];
  const top = tip.map((x, i) => x + 3.3 * toSun[i]);
  const r = northFromShadow(top, tip, up, az);
  check("shadow recovers north", vnear(r.north, north, 1e-9), r.north.map((x) => x.toFixed(6)));
  check("shadow measures the sun's height", near(r.measuredElevation, el, 1e-9), r.measuredElevation);
  check("bearing of the sun's direction = its azimuth", near(bearingOf(toSun, up, north), az, 1e-9));
}

let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.name}${r.detail !== "" ? `  (${r.detail})` : ""}`);
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
