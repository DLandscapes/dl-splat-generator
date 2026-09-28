"""Test: tools/sun_pos.py agrees with static/sun.js (the viewer's sun).

    python tools/sun_pos_test.py        (needs node on the PATH)

Pass rule, fixed before the first run (2026-09-27): for 200 random instants in
2020-2030 at random places (latitude -66..70) the azimuth and elevation of the
two agree to 1e-6 degrees; plus the NREL SPA reference case (Golden, CO,
2003-10-17 12:30:30 UTC-7: zenith 50.11162, azimuth 194.34024) to 0.05 deg,
the same tolerance as sun_test.mjs.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import sun_pos  # noqa: E402

random.seed(11)
cases = [(random.uniform(1577836800000, 1893456000000), random.uniform(-66, 70),
          random.uniform(-180, 180)) for _ in range(200)]
js = ("import { sunPosition } from " + json.dumps((TOOLS.parent / "static" / "sun.js").as_uri())
      + "; const c = " + json.dumps(cases)
      + "; console.log(JSON.stringify(c.map(([t, a, o]) => { const s = sunPosition(t, a, o);"
        " return [s.azimuth, s.elevation]; })));")
out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True)
if out.returncode != 0:
    print("FAILED to run node:", out.stderr.strip()[-300:])
    sys.exit(1)
ref = json.loads(out.stdout)
worst = 0.0
for (t, a, o), (az, el) in zip(cases, ref):
    s = sun_pos.sun_position(t, a, o)
    d_az = abs((s["azimuth"] - az + 180) % 360 - 180)
    worst = max(worst, d_az, abs(s["elevation"] - el))
ok1 = worst < 1e-6
print(f"  {'PASS' if ok1 else 'FAIL'}  200 random instants agree with sun.js  -- worst {worst:.2e} deg")
t = (1066419030000)                       # 2003-10-17 19:30:30 UTC
s = sun_pos.sun_position(t, 39.742476, -105.1786)
ok2 = abs(s["zenith"] - 50.11162) < 0.05 and abs(s["azimuth"] - 194.34024) < 0.05
print(f"  {'PASS' if ok2 else 'FAIL'}  NREL SPA reference case  -- zenith {s['zenith']:.5f}, azimuth {s['azimuth']:.5f}")
print(f"\n{ok1 + ok2} of 2 checks passed")
sys.exit(0 if ok1 and ok2 else 1)
