"""Test: a student's video is saved in output/3D scans for students/<name>/.

    python tools/student_scans_test.py

Marc's rule (2026-09-26): results from input/videos from students/ go into
output/3D scans for students/ under the same name. tools/scene_paths.py decides
that for every producer and reader; this checks the decision, the URLs the
viewer is given, and that the backend's per-scene endpoints (sun, mesh, ground,
Blender package) find a scene in either place.

Built in a temporary folder with a few bytes standing in for videos -- no
COLMAP, no GPU, a second or two. Nothing is written into the project. The last
two checks only READ two real uploads, to prove the content comparison works on
the copies the browser actually makes.

Exit code 0 if every check passed, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

TOOLS = Path(__file__).resolve().parent
PROJECT = TOOLS.parent
ROOT = PROJECT.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PROJECT / "app"))

import scene_index   # noqa: E402
import scene_paths   # noqa: E402

checks: list[tuple[str, bool, str]] = []

HANDED = "3D scans for students"            # ONE folder per student (Marc, 2026-09-27)


def check(name: str, ok: bool, detail: str = "") -> bool:
    checks.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def point_at(root: Path) -> dict:
    """Aim scene_paths at a temporary project root; returns what to restore."""
    saved = {k: getattr(scene_paths, k)
             for k in ("INPUT", "OUTPUT", "STUDENTS_IN", "STUDENTS_OUT")}
    scene_paths.INPUT = root / "input"
    scene_paths.OUTPUT = root / "output"
    scene_paths.STUDENTS_IN = scene_paths.INPUT / "videos from students"
    scene_paths.STUDENTS_OUT = scene_paths.OUTPUT / HANDED
    return saved


def run() -> None:
    real_students_out = scene_paths.STUDENTS_OUT
    real_output = scene_paths.OUTPUT

    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        saved = point_at(root)
        try:
            students_in, uploads = scene_paths.STUDENTS_IN, scene_paths.INPUT / "uploads"
            out, handed = scene_paths.OUTPUT, scene_paths.STUDENTS_OUT
            W = scene_paths.WORKING
            for d in (students_in, uploads, out):
                d.mkdir(parents=True)
            video = b"student clip " * 4096
            (students_in / "Stud A.MOV").write_bytes(video)
            (uploads / "Stud A.MOV").write_bytes(video)              # a browser drop
            (uploads / "renamed.mov").write_bytes(video)             # same bytes, new name
            (uploads / "Other.MOV").write_bytes(b"x" * len(video))   # same size, other bytes
            (uploads / "Mine.MOV").write_bytes(b"mine")

            print("Which videos are a student's")
            check("a file in the students' folder",
                  scene_paths.is_student_source(students_in / "Stud A.MOV"))
            check("its copy in uploads (a file dropped into the browser)",
                  scene_paths.is_student_source(uploads / "Stud A.MOV"))
            check("a copy under another name still counts (contents, not name)",
                  scene_paths.is_student_source(uploads / "renamed.mov"))
            check("same size, different contents: not a student's",
                  not scene_paths.is_student_source(uploads / "Other.MOV"))
            check("an unrelated video: not a student's",
                  not scene_paths.is_student_source(uploads / "Mine.MOV"))
            check("a student's video is made into 3D scans for students/<name>/working/",
                  scene_paths.new_scene_dir(uploads / "Stud A.MOV", "Stud_A") == handed / "Stud_A" / W)
            check("anything else into output/<name>/, as before",
                  scene_paths.new_scene_dir(uploads / "Mine.MOV", "Mine") == out / "Mine")

            print("Where an existing scene is found")
            for name, folder in (("Stud_A", handed / "Stud_A" / W), ("Mine", out / "Mine"),
                                 ("Ørndalen_Video", handed / "Ørndalen_Video" / W)):
                folder.mkdir(parents=True)
                (folder / "splat.spz").write_bytes(b"spz")
                (folder / "capture.json").write_text(json.dumps({"name": name}))
            check("a student's scene: its working folder",
                  scene_paths.scene_dir("Stud_A") == handed / "Stud_A" / W)
            check("any other scene: output/<name>",
                  scene_paths.scene_dir("Mine") == out / "Mine")
            check("an unknown name answers output/<name>, as before",
                  scene_paths.scene_dir("Nothing") == out / "Nothing")
            url = scene_paths.scene_url("Stud_A")
            check("its URL is encoded for the web",
                  url == "/output/3D%20scans%20for%20students/Stud_A/working", url)
            url = scene_paths.scene_url("Ørndalen_Video")
            check("a name with Ø round-trips through its URL",
                  unquote(url) == f"/output/{HANDED}/Ørndalen_Video/working", url)
            check("the viewer's Capture Walk export of a student's scene stays in its working folder",
                  scene_paths.packages_dir("Stud_A") == handed / "Stud_A" / W)
            check("the package handed out: 3D scans for students/<name>/<name>/",
                  scene_paths.package_dir("Stud_A") == handed / "Stud_A" / "Stud_A",
                  str(scene_paths.package_dir("Stud_A")))
            check("... its zip beside it, named like the video",
                  scene_paths.package_zip("Stud_A") == handed / "Stud_A" / "Stud_A.zip")
            check("... and its site data folder carries the scan's name",
                  scene_paths.site_data_dir("Stud_A") == handed / "Stud_A" / "Stud_A" / "Stud_A - site data")
            check("... and everyone else's in output/packages",
                  scene_paths.packages_dir("Mine") == out / "packages")

            print("The list the viewer reads")
            sa = handed / "Stud_A" / W
            scene_index.register(out, "Stud_A", served=sa / "splat.spz",
                                 created="2026-09-26T20:00:00", method="video capture",
                                 index_root=out, folder=sa)
            scene_index.register(out, "Mine", served=out / "Mine" / "splat.spz",
                                 created="2026-09-26T19:00:00", method="video capture")
            check("no second scenes.json inside the students' folder",
                  not (handed / "scenes.json").exists() and not (sa / "scenes.json").exists())
            listed = {s["name"]: s for s in
                      json.loads((out / "scenes.json").read_text())["scenes"]}
            check("one list holds both scenes", set(listed) == {"Stud_A", "Mine"},
                  ", ".join(sorted(listed)))
            ply = listed.get("Stud_A", {}).get("ply")
            check("the student scene's URL points into the group",
                  ply == "/output/3D%20scans%20for%20students/Stud_A/working/splat.spz", str(ply))
            ply = listed.get("Mine", {}).get("ply")
            check("any other scene's URL is unchanged: /output/<name>/<file>",
                  ply == "/output/Mine/splat.spz", str(ply))

            print("The backend finds a student's scene")
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    import blender_export
                    import main
            except Exception as exc:                            # noqa: BLE001
                check("the backend imports", False, f"{type(exc).__name__}: {exc}")
                return
            saved_be = blender_export.OUTPUT, blender_export.PACKAGES
            blender_export.OUTPUT, blender_export.PACKAGES = out, out / "packages"
            try:
                main.sun_put("Stud_A", {"north": [0.0, 0.0, 1.0], "up": [0.0, -1.0, 0.0]})
                check("sun and north are kept beside the student scene",
                      (sa / "sun.json").is_file()
                      and not (out / "Stud_A").exists())
                check("... and read back from there",
                      main.sun_get("Stud_A").get("north") == [0.0, 0.0, 1.0])

                (sa / "mesh").mkdir()
                (sa / "mesh" / "mesh.json").write_text(json.dumps({"faces": 1}))
                (sa / "mesh" / "dense.ply").write_bytes(b"ply")
                (sa / "mesh" / "dem.json").write_text(json.dumps({"cell": 0.1}))
                saved_main = main.ROOT
                main.ROOT = root
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        mesh = main.mesh_status("Stud_A")
                        dem = main.dem_status("Stud_A")
                finally:
                    main.ROOT = saved_main
                base = "/output/3D%20scans%20for%20students/Stud_A/working/mesh"
                check("the built mesh is found and served from the group",
                      mesh["built"] == {"faces": 1} and mesh["cloudUrl"] == f"{base}/dense.ply",
                      str(mesh["cloudUrl"]))
                check("the ground model is found and served from the group",
                      dem["ready"] and dem["demUrl"] == f"{base}/dem.tif",
                      str(dem["demUrl"]))
                folder, zipped = blender_export.paths("Stud_A")
                check("a Blender package would be written into the scan folder",
                      folder.parent == sa, str(folder.parent))
                url = blender_export.status("Stud_A")["zipUrl"]
                check("... and offered for download from there",
                      url == "/output/3D%20scans%20for%20students/Stud_A/working/"
                             "Stud_A.capturewalk.zip", url)
            finally:
                blender_export.OUTPUT, blender_export.PACKAGES = saved_be
        finally:
            for k, v in saved.items():
                setattr(scene_paths, k, v)

    print("Real uploads (read only)")
    uploads = ROOT / "input" / "uploads"
    # found by name, never written here by name: the file names are students'
    copies = ([p for p in uploads.glob("*")
               if p.is_file() and (scene_paths.STUDENTS_IN / p.name).is_file()]
              if uploads.is_dir() else [])
    mine = uploads / "IMG_1779.MOV"
    if copies:
        check("the browser copy of a real student video is recognised",
              scene_paths.new_scene_dir(copies[0], 'x').parent.parent == real_students_out)
    else:
        print("  SKIP  no browser copy of a student video on this machine")
    if mine.is_file():
        check("IMG_1779 (not a student's) still goes to output/",
              scene_paths.new_scene_dir(mine, 'x') == real_output / 'x')
    else:
        print("  SKIP  IMG_1779 is not on this machine")


def main_() -> int:
    print("Student scans -- where they are saved")
    run()
    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
