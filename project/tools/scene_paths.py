"""Where a scene's results live: output/<name>/, or a student's folder.

Marc's rules: a video from input/videos from students/ is kept apart from his
own scenes and the tests -- the students' scans hold people and their makers'
names (2026-09-26) -- and ONE folder per student (2026-09-27), holding what the
student is given and, apart from it, the Splat Generator's own files:

    output/3D scans for students/<name>/
        <name>.zip          the package, zipped: the one file handed out
        <name>/             the same, unzipped, to look through
            <name>.blend  START HERE.pdf  <name> - site data/ (splat, mesh, ...)
        working/            the Splat Generator's own files, NOT handed out:
            capture.json  cameras.json  site.json  sun.json  the raw splat  mesh/

Every producer (capture.py, depth_splat.py, dense_mesh.py, ground_dem.py) and
every reader (app/main.py, app/blender_export.py) asks here instead of writing
`OUTPUT / name`, so the places cannot drift apart. The viewer never builds a
path itself for a listed scene: scenes.json carries each scene's URL.

A video counts as a student's when it lies in that input folder, OR when it is
a byte-identical copy of a file there -- dropping a file into the browser
copies it into input/uploads/ and loses where it came from, so the contents
are compared rather than trusting the name.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent.parent                # ...\DL-SplatGenerator
INPUT = ROOT / "input"
OUTPUT = ROOT / "output"
STUDENTS_IN = INPUT / "videos from students"
STUDENTS_OUT = OUTPUT / "3D scans for students"
WORKING = "working"                       # a student's scene: <students>/<name>/working/


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def is_student_source(source: Path) -> bool:
    """In the students' input folder, or an exact copy of a file in it."""
    source = Path(source)
    if _inside(source, STUDENTS_IN):
        return True
    if not (source.is_file() and STUDENTS_IN.is_dir()):
        return False
    size = source.stat().st_size
    same_size = [p for p in STUDENTS_IN.iterdir()
                 if p.is_file() and p.stat().st_size == size]
    if not same_size:
        return False
    digest = _sha256(source)
    return any(_sha256(p) == digest for p in same_size)


def new_scene_dir(source: Path, name: str) -> Path:
    """Where a NEW scene made from `source` puts its working files."""
    if is_student_source(source):
        return STUDENTS_OUT / name / WORKING
    return OUTPUT / name


def scene_dir(name: str) -> Path:
    """An existing scene's WORKING folder (capture.json, splat, site.json ...).
    A student's is looked for first; a name found nowhere answers
    output/<name>/, as before."""
    student = STUDENTS_OUT / name / WORKING
    return student if student.is_dir() else OUTPUT / name


def is_student_scene(name: str) -> bool:
    return (STUDENTS_OUT / name / WORKING).is_dir()


def package_dir(name: str) -> Path:
    """The unzipped package: 3D scans for students/<name>/<name>/ for a
    student's scene (the zip beside it: package_zip); for any other scene
    output/<name>/package/<name>/."""
    if is_student_scene(name):
        return STUDENTS_OUT / name / name
    return OUTPUT / name / "package" / name


def package_zip(name: str) -> Path:
    return package_dir(name).parent / f"{name}.zip"


def site_data_dir(name: str) -> Path:
    """The package's folder of individual files, one subfolder per kind."""
    return package_dir(name) / f"{name} - site data"


def scene_url(name: str) -> str:
    """The URL of scene_dir(name) as the app serves it (under /output/)."""
    rel = scene_dir(name).relative_to(OUTPUT).as_posix()
    return "/output/" + quote(rel)


def path_url(path: Path) -> str:
    """The URL of any file or folder under output/ as the app serves it."""
    return "/output/" + quote(Path(path).relative_to(OUTPUT).as_posix())


def packages_dir(name: str) -> Path:
    """Where the viewer's "Export for Blender" writes a Capture Walk package:
    output/packages/ as before; for a student's scene its working folder (the
    student's own .blend is built into the package by the package builder)."""
    return scene_dir(name) if is_student_scene(name) else OUTPUT / "packages"
