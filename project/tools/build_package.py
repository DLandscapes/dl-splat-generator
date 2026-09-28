"""A scene's package -- the folder and the zip a student gets -- built step by step.

    python tools/build_package.py <scene name> [--levels train|skip] [--blender build|skip]
                                  [--no-place] [--zip] [--plan]

The same package tools, in the same order, as the first student package was made by hand (2026-09-27);
this runs them for any scene, from the app ("Package" in Export) or from here:

     1 site      the site coordinate system              site_frame.py
     2 video     the video, copied in
     3 splat     the full splat in site coordinates      splat_levels.py --only high
     4 levels    lighter splats, TRAINED at their size   train_levels.py            (--levels)
     5 texture   the textured mesh                       mesh_texture.py            (needs the mesh)
     6 table     every frame: time, position, sun        camera_table.py
     7 exports   Rhino .3dm and glTF .glb                site_exports.py
     8 terrain   point cloud, ground, contours           package_terrain.py         (needs the mesh)
     9 drawings  plan and elevations                     package_drawings.py
    10 plandxf   plan as DXF                             package_plan_dxf.py
    11 viewer    the .dlscene for the viewer             package_viewer_scene.py
    12 paths     example camera moves                    camera_paths.py
    13 models    surface and ground models, shells       package_dtm.py (x 2)
    14 addon     the Blender add-on, copied in           (pipeline\\ble\\<version>)
    15 blender   four .blend files, light to heavy       Capture Walk's make_blend.py (--blender)
    16 readme    START HERE.pdf                          package_readme.py
    17 zip       <name>.zip, verified                    package_zip.py             (--zip)

NOTHING IS OVERWRITTEN. A step whose files are there is "already made" and skipped; a step
that cannot run says why ("no dense mesh -- build it in Terrain") and the build goes on
without it; a step that FAILS stops the build. Only START HERE and the zip are ever made
again -- when files in the package are newer than they are -- and then the old one is MOVED
to the working folder (working\\replaced <date time>\\), never deleted.

--plan prints what would happen as one JSON line (PLAN ...), running nothing. Place and time
of filming go into the spreadsheet, the Rhino file and the viewer bundle unless --no-place
(they are personal data about whoever filmed; a student's own capture carries them).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import scene_paths   # noqa: E402

ROOT = TOOLS.parent.parent
WORK = ROOT / "work"
PY = sys.executable
WEIGHTS = [("mesh", None), ("splat very low", "verylow"), ("splat low", "low"), ("splat high", "high")]


# ------------------------------------------------------------------ helpers

def tool(script: str, *args) -> None:
    """Run one package tool, its output passed through (the app's job card reads the
    "~ progress" lines). Raises with the tool's own FAILED line."""
    proc = subprocess.Popen([PY, "-u", str(TOOLS / script), *[str(a) for a in args]],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    failed = None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        if "FAILED:" in line:
            failed = line.split("FAILED:", 1)[1].strip()
        print(line if line.startswith(" ") else "    " + line, flush=True)
    if proc.wait() != 0:
        raise RuntimeError(f"{script}: {failed or f'exited with code {proc.returncode}'}")


def version_key(p: Path):
    return tuple(int(x) for x in re.findall(r"\d+", p.name)[:3]) or (0,)


def find_blender() -> Path | None:
    """Blender 4.5 or newer: $DL_BLENDER, this tool's bin\\, Program Files, /Applications."""
    cands = []
    if os.environ.get("DL_BLENDER"):
        cands.append(Path(os.environ["DL_BLENDER"]))
    cands += sorted((ROOT / "bin").glob("blender-*/blender.exe"), key=lambda p: version_key(p.parent), reverse=True)
    cands += sorted((ROOT / "bin").glob("blender-*/blender"), key=lambda p: version_key(p.parent), reverse=True)
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"
    cands += sorted(pf.glob("Blender */blender.exe"), key=lambda p: version_key(p.parent), reverse=True)
    cands.append(Path("/Applications/Blender.app/Contents/MacOS/Blender"))
    for c in cands:
        v = version_key(c.parent)
        if c.is_file() and (v == (0,) or v[:2] >= (4, 5)):
            return c
    return None


def find_ble() -> Path | None:
    """The pinned Capture Walk folder (pipeline\\ble\\<version>) with the add-on zip, the
    scene writer and the .blend builder -- the newest one."""
    base = ROOT / "pipeline" / "ble"
    pins = sorted((p for p in base.glob("*") if (p / "make_blend.py").is_file()), key=version_key, reverse=True)
    return pins[0] if pins else None


def newest(folder: Path, skip: set) -> float:
    """The newest file in the package -- side files a program wrote (QGIS's .aux.xml, Blender's
    .blend1) do not count: opening a file must not make the zip 'out of date'."""
    import package_zip
    files, _ = package_zip.package_files(folder)
    return max((p.stat().st_mtime for p in files if p.name not in skip), default=0.0)


def move_aside(p: Path, working: Path) -> Path:
    # a folder per minute collided when START HERE was replaced twice in one minute (a student package, 2026-09-27
    # 20:10: a rebuild, then the zip) -- the second replacement stopped the build. Now the next free
    # "- 002", "- 003" ... folder of that minute takes it; still never an overwrite.
    stamp = f"replaced {time.strftime('%Y-%m-%d %H-%M')}"
    dest, k = working / stamp, 2
    while (dest / p.name).exists():
        dest, k = working / f"{stamp} - {k:03d}", k + 1
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / p.name
    shutil.move(str(p), str(target))
    return target


# ------------------------------------------------------------------ the steps

class Package:
    def __init__(self, n: str, args):
        self.n = n
        self.args = args
        self.folder = scene_paths.scene_dir(n)
        self.pkg = scene_paths.package_dir(n)
        self.sd = scene_paths.site_data_dir(n)
        self.work = WORK / n
        try:
            self.rec = json.loads((self.folder / "capture.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.rec = {}
        self.place = [] if args.no_place else ["--with-place"]
        self.cs = self.sd / "cameras and sun"

    # paths
    def p(self, *parts) -> Path:
        return self.sd.joinpath(*parts)

    @property
    def obj(self) -> Path:
        return self.p("mesh", f"{self.n}_mesh.obj")

    @property
    def mesh_ply(self) -> Path:
        return self.folder / "mesh" / "mesh.ply"

    def video(self) -> Path | None:
        hits = sorted(self.p("video").glob(f"{self.n}.*")) if self.p("video").is_dir() else []
        return hits[0] if hits else None

    def source(self) -> Path | None:
        s = self.rec.get("source")
        return Path(s) if s and Path(s).is_file() else None

    def splat_file(self, level: str) -> Path:
        return self.p("splat", f"{self.n}_3DGS_{level}.ply")

    # each: (key, label, done?, cannot-why or None, run)
    def steps(self):
        n, a = self.n, self.args
        no_mesh = "no dense mesh -- build it first (Terrain > Build the mesh; needs an NVIDIA graphics card)"
        tex_missing = None if self.obj.is_file() else "no textured mesh (step texture)"
        ble = find_ble()
        blender = find_blender()
        if self.rec.get("method") == "single-image metric depth":
            why = "a scene from one photograph has no camera path to build a package from"
            return [(k, lab, False, why, None) for k, lab in (
                ("site", "the site coordinate system"), ("video", "the video, copied in"),
                ("splat", "the full splat in site coordinates"), ("zip", f"{n}.zip, verified"))]
        return [
            ("site", "the site coordinate system", (self.folder / "site.json").is_file(),
             "no capture.json -- not a scene made from a video" if not self.rec else
             "a scene from one photograph has no camera path to build a package from"
             if self.rec.get("method") == "single-image metric depth" else None,
             lambda: tool("site_frame.py", n)),
            ("video", "the video, copied in", self.video() is not None,
             None if self.source() else "the source video is not where capture.json says",
             self.copy_video),
            ("splat", "the full splat in site coordinates", self.splat_file("high").is_file(),
             None if (self.folder / "site.json").is_file() else "no site.json (step site)",
             lambda: tool("splat_levels.py", n, "--only", "high")),
            ("levels", "lighter splats, trained at their size (400k, 150k, 50k)",
             all(self.splat_file(l).is_file() for l in ("med", "low", "verylow")),
             ("not asked for" if a.levels == "skip" else
              None if (self.work / "undistorted" / "sparse" / "0").is_dir() else
              "no undistorted dataset in work\\ -- the capture's intermediates are gone"),
             lambda: tool("train_levels.py", n)),
            ("texture", "the textured mesh", self.obj.is_file(),
             no_mesh if not self.mesh_ply.is_file() else
             None if (self.work / "undistorted" / "images").is_dir() else
             "no undistorted frames in work\\ -- the capture's intermediates are gone",
             lambda: tool("mesh_texture.py", n)),
            ("table", "every frame: time, position, direction, sun",
             (self.cs / f"{n}_cameras.xlsx").is_file(), None,
             lambda: tool("camera_table.py", n, *self.place)),
            ("exports", "Rhino .3dm and glTF .glb",
             (self.cs / f"{n}.3dm").is_file() and (self.cs / f"{n}.glb").is_file(),
             tex_missing, lambda: tool("site_exports.py", n, *self.place)),
            ("terrain", "point cloud, ground points, contours",
             self.p("terrain", f"{n}_ground.ply").is_file(),
             None if (self.folder / "mesh" / "dense.ply").is_file() else no_mesh,
             lambda: tool("package_terrain.py", n)),
            ("drawings", "plan and elevations", self.p("drawings", f"{n}_plan.png").is_file(),
             tex_missing or (None if self.p("terrain", f"{n}_contours.dxf").is_file() else "no contours (step terrain)"),
             lambda: tool("package_drawings.py", n)),
            ("plandxf", "the plan as DXF", self.p("drawings", f"{n}_plan.dxf").is_file(),
             None if (self.folder / "mesh" / "drawings.json").is_file() else "no drawings (step drawings)",
             lambda: tool("package_plan_dxf.py", n)),
            ("viewer", "the .dlscene for the viewer", (self.pkg / f"{n}.dlscene").is_file(),
             None if (self.folder / "cameras.json").is_file() else "no cameras.json",
             lambda: tool("package_viewer_scene.py", n, *self.place)),
            ("paths", "example camera moves", (self.cs / f"{n}_camera_paths.json").is_file(),
             tex_missing or (None if self.p("terrain", f"{n}_ground.ply").is_file() else "no ground points (step terrain)"),
             lambda: tool("camera_paths.py", n, "--out", self.cs)),
            ("models", "surface and ground models with shells",
             any((self.folder / "mesh").glob("dsm_*.json")) and any((self.folder / "mesh").glob("dtm_*.json")),
             tex_missing or (None if self.p("terrain", f"{n}_ground.ply").is_file() else "no ground points (step terrain)"),
             self.models),
            ("addon", "the Blender add-on, copied in", any(self.p("add-on").glob("capture_walk-*.zip")),
             None if ble else "no pinned Capture Walk in pipeline\\ble\\", lambda: self.copy_addon(ble)),
            ("blender", "Blender files, light to heavy",
             bool(self.weights()) and all((self.pkg / f"{n} - {w}.blend").is_file() for w, _ in self.weights()),
             ("not asked for" if a.blender == "skip" else
              "Blender 4.5 or newer was not found (set DL_BLENDER to blender.exe)" if not blender else
              "no pinned Capture Walk in pipeline\\ble\\" if not ble else
              None if self.weights() else "nothing to put in one yet (step splat)"),
             lambda: self.blend_files(blender, ble)),
            ("readme", "START HERE.pdf", self.fresh(self.pkg / "START HERE.pdf", {"START HERE.pdf"}),
             None, self.readme),
            ("zip", f"{n}.zip, verified", self.fresh(scene_paths.package_zip(n), set(), whole=True),
             None if a.zip else "not asked for", self.make_zip),
        ]

    def weights(self) -> list:
        """The .blend weights that can be made: the mesh one needs the textured mesh, each
        splat one its splat file (a machine without an NVIDIA card has no mesh -- it still gets
        the splat files)."""
        return [(w, l) for w, l in WEIGHTS
                if (self.obj.is_file() if l is None else self.splat_file(l).is_file())]

    def fresh(self, p: Path, skip: set, whole: bool = False) -> bool:
        """p exists and nothing in the package is newer (the zip: nothing at all)."""
        if not p.is_file() or not self.pkg.is_dir():
            return False
        return newest(self.pkg, skip) <= p.stat().st_mtime

    # runners that are not a single tool call
    def copy_video(self):
        src = self.source()
        dest = self.p("video", f"{self.n}{src.suffix}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"    copied {src.name} -> video/{dest.name} ({dest.stat().st_size / 1e6:.0f} MB)")

    def copy_addon(self, ble: Path):
        zips = sorted(ble.glob("capture_walk-*.zip"))
        if not zips:
            raise RuntimeError(f"no capture_walk-*.zip in {ble}")
        dest = self.p("add-on")
        dest.mkdir(parents=True, exist_ok=True)
        for f in [zips[-1], ble / "LICENSE.txt", ble / "CREDITS.md"]:
            if f.is_file() and not (dest / f.name).exists():
                shutil.copy2(f, dest / f.name)
                print(f"    copied {f.name}")

    def models(self):
        mesh = self.folder / "mesh"
        if not any(mesh.glob("dsm_*.json")):
            tool("package_dtm.py", self.n, "--source", "surface")
        if not any(mesh.glob("dtm_*.json")):
            tool("package_dtm.py", self.n, "--source", "ground")

    def blend_files(self, blender: Path, ble: Path):
        n = self.n
        scene_pkg = self.work / "_blend_package" / f"{n}.capturewalk"
        if not scene_pkg.is_dir():
            cmd = [PY, "-X", "utf8", str(ble / "export_for_blender.py"), str(self.work),
                   "--out", str(scene_pkg.parent)]
            src = self.source()
            if src:
                cmd += ["--video", str(src)]
            stride = (self.rec.get("settings") or {}).get("stride")
            if stride:
                cmd += ["--stride", str(int(stride))]
            if self.rec.get("ply") and (self.folder / self.rec["ply"]).is_file():
                cmd += ["--splat", str(self.folder / self.rec["ply"])]
            if self.mesh_ply.is_file():
                cmd += ["--mesh", str(self.mesh_ply)]
            if (self.folder / "sun.json").is_file():
                cmd += ["--sun", str(self.folder / "sun.json")]
            print("    the scene for Blender (Capture Walk's writer)", flush=True)
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if proc.returncode != 0 or not scene_pkg.is_dir():
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()
                raise RuntimeError(f"export_for_blender.py: {tail[-1] if tail else proc.returncode}")
        video = self.video()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        todo = self.weights()
        for w, _ in WEIGHTS:
            if (w, _) not in todo:
                print(f"    {n} - {w}.blend: left out -- " + ("no textured mesh" if _ is None else
                                                              f"no {self.splat_file(_).name}"))
        for i, (weight, level) in enumerate(todo, 1):
            out = self.pkg / f"{n} - {weight}.blend"
            if out.is_file():
                print(f"    {out.name}: already made")
                continue
            extra = ["--splat", "none"]
            if level:
                ply = self.splat_file(level)
                head = ply.open("rb").read(4000).split(b"end_header")[0].decode("ascii", "replace")
                count = int(head.split("element vertex ")[1].split()[0])
                extra = ["--ref-splat", str(ply), "--splat-points", str(count)]
            cmd = [str(blender), "-b", "--factory-startup", "--python-use-system-env",
                   "-P", str(ble / "make_blend.py"), "--", str(scene_pkg), "--out", str(self.pkg),
                   "--name", f"{n} - {weight}", "--scan", n, "--blend-only",
                   "--site", str(self.folder / "site.json")]
            if self.obj.is_file():
                cmd += ["--ref-mesh", str(self.obj)]
            if video:
                cmd += ["--ref-video", str(video)]
            cmd += extra
            print(f"    ~ progress {i - 1}/{len(todo)} Blender files", flush=True)
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
            result = next((l for l in reversed((proc.stdout or "").splitlines()) if l.startswith("{")), None)
            info = json.loads(result) if result else {}
            if proc.returncode != 0 or not info.get("ok") or not out.is_file():
                raise RuntimeError(f"{out.name}: {info.get('error') or 'Blender exited with code %s' % proc.returncode}")
            print(f"    {out.name}: {info.get('megabytes')} MB, {info.get('seconds')} s", flush=True)
        print(f"    ~ progress {len(todo)}/{len(todo)} Blender files", flush=True)

    def readme(self):
        p = self.pkg / "START HERE.pdf"
        if p.is_file():
            print(f"    START HERE.pdf is older than the package -- moved to {move_aside(p, self.folder).parent.name}")
        tool("package_readme.py", self.n)

    def make_zip(self):
        z = scene_paths.package_zip(self.n)
        if z.is_file():
            print(f"    {z.name} is older than the package -- moved to {move_aside(z, self.folder).parent.name}")
        tool("package_zip.py", self.n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--levels", choices=("train", "skip"), default="train")
    ap.add_argument("--blender", choices=("build", "skip"), default="build")
    ap.add_argument("--no-place", action="store_true")
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--plan", action="store_true")
    args = ap.parse_args()
    pk = Package(args.name, args)
    steps = pk.steps()
    if args.plan:
        # a step held up only by an earlier step of this same build ("(step site)") is to do,
        # after that one -- the plan follows the chain the build will run
        plan, state = [], {}
        for k, lab, done, why, _ in steps:
            m = re.search(r"\(step (\w+)\)$", why or "")
            if done:
                st, note = "done", None
            elif m and state.get(m.group(1)) == "todo":
                st, note = "todo", f"after step {m.group(1)}"
            else:
                st, note = ("off" if why == "not asked for" else "cannot") if why else "todo", why
            state[k] = st
            plan.append({"key": k, "label": lab, "state": st, "why": note})
        print("PLAN " + json.dumps({"name": args.name, "package": str(pk.pkg), "steps": plan}))
        return 0
    t0 = time.time()
    summary = {"made": [], "already": [], "left out": {}}
    total = len(steps)
    for i, (key, label, _done, _why, run) in enumerate(steps, 1):
        # the state is read again before each step: earlier steps change it
        key, label, done, why, run = next(s for s in Package(args.name, args).steps() if s[0] == key)
        print(f"[{i}/{total}] {key}  {label}", flush=True)
        if done:
            print("    already made")
            summary["already"].append(key)
            continue
        if why:
            print(f"    left out: {why}")
            summary["left out"][key] = why
            continue
        began = time.time()
        try:
            run()
        except RuntimeError as exc:
            print(f"FAILED: step {key} ({label}): {exc}", flush=True)
            print("PACKAGE " + json.dumps({**summary, "failed": key, "seconds": round(time.time() - t0)}))
            return 1
        print(f"    done in {time.time() - began:.0f} s", flush=True)
        summary["made"].append(key)
    print("PACKAGE " + json.dumps({**summary, "seconds": round(time.time() - t0),
                                   "package": str(pk.pkg)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
