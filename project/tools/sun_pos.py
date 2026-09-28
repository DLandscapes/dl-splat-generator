"""The sun's position for a place and a time -- the Python twin of static/sun.js.

The NOAA solar-position algorithm (after Meeus), line for line as in the viewer,
so a spreadsheet and the viewer never disagree about where the sun was. Checked
against sun.js itself in tools/sun_pos_test.py (and sun.js against NREL's SPA
reference case in tools/sun_test.mjs).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

RAD = math.pi / 180
DEG = 180 / math.pi


def _mod(a: float, n: float) -> float:
    return ((a % n) + n) % n


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def refraction(el: float) -> float:
    """Standard atmospheric refraction in degrees (NOAA's approximation)."""
    if el > 85:
        return 0.0
    t = math.tan(el * RAD)
    if el > 5:
        arcsec = 58.1 / t - 0.07 / t ** 3 + 0.000086 / t ** 5
    elif el > -0.575:
        arcsec = 1735 + el * (-518.2 + el * (103.4 + el * (-12.79 + el * 0.711)))
    else:
        arcsec = -20.772 / t
    return arcsec / 3600


def sun_position(utc_ms: float, lat: float, lon: float) -> dict:
    """{azimuth, elevation, zenith, geometric}: degrees; azimuth clockwise from
    true north; elevation includes refraction (the sun you would see);
    `geometric` is the elevation without it (used for sunrise and sunset)."""
    jd = utc_ms / 86400000 + 2440587.5
    T = (jd - 2451545) / 36525
    L0 = _mod(280.46646 + T * (36000.76983 + T * 0.0003032), 360)
    M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
    e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
    C = (math.sin(M * RAD) * (1.914602 - T * (0.004817 + 0.000014 * T))
         + math.sin(2 * M * RAD) * (0.019993 - 0.000101 * T)
         + math.sin(3 * M * RAD) * 0.000289)
    omega = 125.04 - 1934.136 * T
    lam = L0 + C - 0.00569 - 0.00478 * math.sin(omega * RAD)
    eps0 = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(omega * RAD)
    decl = math.asin(math.sin(eps * RAD) * math.sin(lam * RAD))

    y = math.tan(eps * RAD / 2) ** 2
    eq_time = 4 * DEG * (y * math.sin(2 * L0 * RAD) - 2 * e * math.sin(M * RAD)
                         + 4 * e * y * math.sin(M * RAD) * math.cos(2 * L0 * RAD)
                         - 0.5 * y * y * math.sin(4 * L0 * RAD)
                         - 1.25 * e * e * math.sin(2 * M * RAD))

    minutes_utc = _mod(utc_ms / 60000, 1440)
    true_solar = _mod(minutes_utc + eq_time + 4 * lon, 1440)
    hour_angle = true_solar / 4 - 180
    if hour_angle < -180:
        hour_angle += 360

    phi = lat * RAD
    cos_z = _clamp(math.sin(phi) * math.sin(decl)
                   + math.cos(phi) * math.cos(decl) * math.cos(hour_angle * RAD), -1, 1)
    zen = math.acos(cos_z) * DEG
    sin_z = math.sin(zen * RAD)
    if abs(math.cos(phi) * sin_z) < 1e-9:
        azimuth = 180.0 if phi > 0 else 0.0
    else:
        a = math.acos(_clamp((math.sin(phi) * cos_z - math.sin(decl))
                             / (math.cos(phi) * sin_z), -1, 1)) * DEG
        azimuth = _mod(a + 180, 360) if hour_angle > 0 else _mod(540 - a, 360)
    geometric = 90 - zen
    elevation = geometric + refraction(geometric)
    return {"azimuth": azimuth, "elevation": elevation, "zenith": 90 - elevation,
            "geometric": geometric}


def utc_ms(local: datetime) -> float:
    """A timezone-aware datetime -> milliseconds since the epoch (UTC)."""
    return local.astimezone(timezone.utc).timestamp() * 1000


def sunrise_sunset(day: datetime, lat: float, lon: float) -> tuple:
    """Local times of sunrise and sunset on `day` (aware, any time that day):
    where the geometric elevation crosses -0.833 deg (NOAA's definition: the
    upper limb on the horizon, with refraction). None where the sun does not
    cross it that day (midnight sun, polar night) -- with the reason."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    prev = None
    rise = set_ = None
    for m in range(0, 24 * 60 + 1):
        t = start + timedelta(minutes=m)
        g = sun_position(utc_ms(t), lat, lon)["geometric"] + 0.833
        if prev is not None:
            if prev < 0 <= g and rise is None:
                rise = t
            if prev >= 0 > g and set_ is None:
                set_ = t
        prev = g
    return rise, set_
