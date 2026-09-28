"""The one file handed out: <name>.zip of the student's package, verified.

    python tools/package_zip.py <scene name>

Zips 3D scans for students/<name>/<name>/ into 3D scans for students/<name>/<name>.zip,
with the folder <name>/ at the top of the zip, so unzipping anywhere gives the same
folder (the .blend files find their video, splat and mesh by their place in it).
Files that are already compressed (video, images, .spz, the .dlscene bundle, the
add-on zip) are STORED; the rest is deflated. Never overwrites.

Verified after writing: the zip's entries are exactly the package's files, with the
same sizes, and every entry's CRC checks out (zipfile.testzip). A list of the files
with their SHA-256 goes to the working folder (working/zip manifest.txt) -- the record
of what exactly was handed out.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import scene_paths   # noqa: E402

STORED = {".jpg", ".jpeg", ".png", ".mov", ".mp4", ".m4v", ".zip", ".spz", ".dlscene"}
# Files programs write BESIDE what they open -- not part of the package. Found 2026-09-27: QGIS
# had left Ym_DTM_0.1.tif.aux.xml (statistics of the OLD raster) in the terrain folder, and the
# next zip took it along. Left out, and named, never deleted.
SIDE_FILES = (".aux.xml", ".blend1", ".blend2", ".ovr", ".~lock", "Thumbs.db", "desktop.ini", ".DS_Store")


def package_files(pkg: Path) -> tuple[list, list]:
    """(the package's files, the side files left out)."""
    files, side = [], []
    for p in sorted(q for q in pkg.rglob("*") if q.is_file()):
        (side if p.name.endswith(SIDE_FILES) or p.name.startswith(".~lock") else files).append(p)
    return files, side


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    args = ap.parse_args()
    n = args.name
    pkg = scene_paths.package_dir(n)
    out = scene_paths.package_zip(n)
    if out.exists():
        print(f"FAILED: {out} exists -- never overwritten", file=sys.stderr)
        return 2
    files, side = package_files(pkg)
    for p in side:
        print(f"    left out: {p.relative_to(pkg).as_posix()} (a side file a program wrote, not part of the package)")
    if not files:
        print(f"FAILED: nothing in {pkg}", file=sys.stderr)
        return 2
    t0 = time.time()
    tmp = out.with_name(out.name + ".partial")
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as z:
        for p in files:
            arc = f"{n}/" + p.relative_to(pkg).as_posix()
            if p.suffix.lower() in STORED:
                z.write(p, arc, compress_type=zipfile.ZIP_STORED)
            else:
                z.write(p, arc, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    tmp.rename(out)

    with zipfile.ZipFile(out) as z:
        bad = z.testzip()
        entries = {i.filename: i.file_size for i in z.infolist() if not i.is_dir()}
    want = {f"{n}/" + p.relative_to(pkg).as_posix(): p.stat().st_size for p in files}
    if bad or entries != want:
        print(f"FAILED verification: bad entry {bad!r}, "
              f"missing {sorted(set(want) - set(entries))}, extra {sorted(set(entries) - set(want))}",
              file=sys.stderr)
        return 3
    total = sum(want.values())
    manifest = scene_paths.scene_dir(n) / "zip manifest.txt"
    lines = [f"{out.name}  {time.strftime('%Y-%m-%d %H:%M')}  {len(files)} files, "
             f"{total / 1e6:.1f} MB unzipped, {out.stat().st_size / 1e6:.1f} MB zipped, zip sha256 {sha256(out)}"]
    lines += [f"{sha256(p)}  {p.stat().st_size:>11}  {p.relative_to(pkg).as_posix()}" for p in files]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"    {out.name}: {len(files)} files, {total / 1e6:.0f} MB -> {out.stat().st_size / 1e6:.0f} MB "
          f"({out.stat().st_size / total:.0%}), {time.time() - t0:.0f} s; entries, sizes and CRCs verified")
    print(f"    manifest: {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
