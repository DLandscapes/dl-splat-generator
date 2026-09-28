"""Gate: a DEM written by the tools lands where its points are, read the standard way.

    python tools/geotiff_origin_test.py

Points on a sloping strip go through the same calls both writers make (rasterise ->
fill_holes -> write_raster). Read back as GDAL / QGIS / DL-TerrainDiversity / DL-3DPrint read
it -- ModelTiepoint = north-west corner of raster (0, 0), rows counted south, ModelPixelScale =
the cell -- every point must fall inside the raster, and every cell must hold EXACTLY the mean
height of the points the reading puts into it (float32 precision). A grid shifted by even one
row fails the last check.

History (2026-09-27): the writers put the tie point on the south row; read the standard way the
grid landed (ny-1) cells too far south. The first version of this test compared each point
with its cell within "half a cell's rise" -- a rule that was wrong by construction (a cell holds
the mean of randomly placed points, not the value at its centre: 0.072 against 0.062 allowed)
and it called write_geotiff directly, not the callers' path. Replaced by the exact rule below
and write_raster, the one writing path both tools now use.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import ground_dem   # noqa: E402


def read_standard(path: Path):
    im = Image.open(path)
    grid = np.asarray(im, dtype=float)
    cell = float(im.tag_v2[33550][0])
    tie = im.tag_v2[33922]
    return grid, cell, (float(tie[3]), float(tie[4]))


def main() -> int:
    rng = np.random.default_rng(3)
    n = 40_000
    x = rng.uniform(12.0, 20.0, n)         # a strip longer north-south than east-west,
    y = rng.uniform(-5.0, 9.0, n)          # away from the origin, so a wrong corner shows
    z = 2.0 + 0.3 * x - 0.2 * y
    cell = 0.25
    grid, meta = ground_dem.rasterise(np.stack([x, y, z], 1), cell, 2)
    grid, _ = ground_dem.fill_holes(grid, 4)
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        tif = Path(tmp) / "t.tif"
        ground_dem.write_raster(tif, grid, cell, meta)
        g, c, (tx, ty) = read_standard(tif)
    checks.append(("cell and size read back", c == cell and g.shape == grid.shape, f"cell {c}, {g.shape}"))
    j = np.floor((x - tx) / c).astype(int)
    i = np.floor((ty - y) / c).astype(int)
    inside = (i >= 0) & (i < g.shape[0]) & (j >= 0) & (j < g.shape[1])
    checks.append(("every point inside the raster", bool(inside.all()), f"{inside.mean():.1%}"))
    key = i[inside] * g.shape[1] + j[inside]
    cnt = np.bincount(key, minlength=g.size)
    mean = np.bincount(key, z[inside], minlength=g.size) / np.maximum(cnt, 1)
    measured = cnt >= 2                                   # rasterise's own threshold
    stored = g.ravel()[measured]
    err = np.abs(stored - mean[measured])
    if len(err):
        ok = np.isfinite(stored).all() and err.max() <= 1e-4 * max(1.0, np.abs(z).max())
        info = f"{measured.sum():,} cells, max difference {np.nanmax(err):.2e}"
    else:
        ok, info = False, "no cell holds two points -- the raster is not where the points are"
    checks.append(("each cell = the mean of the points read into it", bool(ok), info))
    bad = 0
    for name, good, info in checks:
        print(f"    {'PASS' if good else 'FAIL'}  {name}: {info}")
        bad += not good
    print(f"geotiff_origin_test: {len(checks) - bad}/{len(checks)} pass")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
