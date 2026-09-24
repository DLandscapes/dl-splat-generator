"""Export a solved capture as a Capture Walk package, for the Blender add-on.

The package format and its ONLY writer live in the BLE project
(`BLE\\project\\export_for_blender.py`, format spec in `PACKAGE-FORMAT.md`).
This module does not re-implement any of it: it finds that script, runs it, and
reads back what it printed. One owner of the format, so the producer here and
the consumer in Blender cannot drift apart. If BLE is not on this machine the
UI says so instead of half-doing the job.

Everything the writer needs already exists in this project: the undistorted
COLMAP model under `work\\<name>\\`, the `capture.json` beside the scene, and
ffprobe in `bin\\`. It reads those; it writes only into the folder we hand it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
PROJECT = APP.parent
ROOT = PROJECT.parent                      # ...\DL-SplatGenerator
WORK = ROOT / "work"
OUTPUT = ROOT / "output"
PACKAGES = OUTPUT / "packages"

# Where the writer lives. The environment variable is the escape hatch for a
# machine that keeps the projects somewhere else.
ENV_VAR = "DL3DGS_BLE_EXPORTER"
DEFAULT_BLE = ROOT.parent / "BLE" / "project" / "export_for_blender.py"

# The model the writer looks for, in its own order of preference. Checked here
# only so the UI can explain a missing one before the run rather than after.
MODEL_RELATIVE = ("undistorted/sparse/0", "undistorted/sparse", "sparse/0", "sparse")


def exporter() -> Path | None:
    """BLE's export_for_blender.py, or None if this machine has no BLE."""
    override = os.environ.get(ENV_VAR)
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    return DEFAULT_BLE if DEFAULT_BLE.is_file() else None


def _model(work: Path) -> tuple[Path | None, bool]:
    """The sparse model the writer will pick, and whether it is undistorted."""
    for rel in MODEL_RELATIVE:
        p = work / rel
        if (p / "images.bin").is_file():
            return p, rel.startswith("undistorted")
    return None, False


def _safe_name(name: str) -> str:
    """A scene name is a folder name; refuse anything that could escape."""
    if not name or name != Path(name).name or name in (".", ".."):
        raise ValueError(f"bad scene name: {name!r}")
    return name


def paths(name: str) -> tuple[Path, Path]:
    """Where this scene's package folder and zip go."""
    name = _safe_name(name)
    folder = PACKAGES / f"{name}.capturewalk"
    return folder, folder.with_suffix(".capturewalk.zip")


def status(name: str) -> dict:
    """Can this scene be exported, and if not, what is in the way?

    Answered before the run so the button can explain itself, and so a missing
    piece reads as a sentence rather than as a stack trace afterwards.
    """
    name = _safe_name(name)
    work = WORK / name
    script = exporter()
    model, undistorted = _model(work) if work.is_dir() else (None, False)
    folder, zipped = paths(name)

    record = OUTPUT / name / "capture.json"
    video = None
    stride = None
    splat = None
    if record.is_file():
        try:
            rec = json.loads(record.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            rec = {}
        src = rec.get("source")
        if src:
            video = Path(src)
        stride = (rec.get("settings") or {}).get("stride")
        if rec.get("ply"):
            cand = record.parent / rec["ply"]
            splat = cand if cand.is_file() else None

    blocking = []
    if script is None:
        blocking.append(
            "the Blender add-on project (BLE) is not on this machine, and it "
            "owns the package writer — set %s to export_for_blender.py if it "
            "lives somewhere unusual" % ENV_VAR)
    if not work.is_dir():
        blocking.append(f"no solved capture in work\\{name} — the intermediates "
                        f"may have been cleared; re-run the capture")
    elif model is None:
        blocking.append(f"no COLMAP model under work\\{name} — the pose stage "
                        f"did not finish")
    if not record.is_file():
        blocking.append(f"no output\\{name}\\capture.json — the writer reads the "
                        f"video path and stride from it")
    elif video is None or not video.is_file():
        blocking.append("capture.json does not name a source video that is still "
                        "there" if video is None else
                        f"the source video has moved: {video}")
    if record.is_file() and not stride:
        blocking.append("capture.json records no stride")

    warnings = []
    if model is not None and not undistorted:
        warnings.append("the model is the RAW one, not undistorted — the Blender "
                        "camera cannot reproduce lens distortion")

    return {
        "name": name,
        "ready": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "exporter": str(script) if script else None,
        "undistorted": undistorted,
        "splat": splat.name if splat else None,
        "splatMB": round(splat.stat().st_size / 1e6, 1) if splat else None,
        "exists": folder.is_dir() or zipped.is_file(),
        "folder": str(folder),
        "zip": str(zipped),
        "zipUrl": f"/output/packages/{zipped.name}",
    }


def _remove_existing(folder: Path, zipped: Path) -> list:
    """Delete a previous package for this scene. Only ever called for an
    explicit replace, and only on the two paths we generate ourselves."""
    removed = []
    for p in (folder, zipped):
        if p.parent.resolve() != PACKAGES.resolve():
            raise ValueError(f"refusing to remove outside packages\\: {p}")
        if p.is_dir():
            shutil.rmtree(p)
            removed.append(p.name)
        elif p.is_file():
            p.unlink()
            removed.append(p.name)
    return removed


def run(name: str, *, with_splat: bool = False, with_frames: bool = False,
        replace: bool = False, timeout: int = 3600) -> dict:
    """Run BLE's writer for one scene and report what it said."""
    name = _safe_name(name)
    state = status(name)
    if not state["ready"]:
        return {"ok": False, "error": state["blocking"][0],
                "blocking": state["blocking"], "status": state}

    folder, zipped = paths(name)
    removed = []
    if folder.is_dir() or zipped.is_file():
        if not replace:
            # The writer refuses to overwrite, and so do we without being asked.
            return {"ok": False, "exists": True, "status": state,
                    "error": f"a package for \"{name}\" is already in "
                             f"output\\packages\\. Replacing it deletes the "
                             f"folder and the .zip."}
        removed = _remove_existing(folder, zipped)

    PACKAGES.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-X", "utf8", str(exporter()), str(WORK / name),
           "--out", str(PACKAGES), "--zip"]
    if with_splat:
        cmd += ["--with-splat"]
    if with_frames:
        cmd += ["--with-frames"]

    began = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": state,
                "error": f"the writer did not finish within {timeout // 60} minutes"}
    seconds = round(time.time() - began, 1)

    out = [l.rstrip() for l in (proc.stdout or "").splitlines() if l.strip()]
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"ok": False, "status": state, "lines": out, "seconds": seconds,
                "error": err.splitlines()[-1] if err else
                         f"the writer exited with code {proc.returncode}"}

    # Its own printed warnings start with "!", and are the point of showing the
    # summary at all: NO SCALE and RAW model both change what the scene means.
    warnings = [l.lstrip("! ").strip() for l in out if l.lstrip().startswith("!")]
    made = {}
    for line in out:
        m = re.match(r"(written|zipped)\s+->\s+(.*?)\s+\(([\d.]+) MB\)", line.strip())
        if m:
            made[m.group(1)] = {"path": m.group(2), "megabytes": float(m.group(3))}

    return {
        "ok": True,
        "name": name,
        "seconds": seconds,
        "lines": out,
        "warnings": warnings,
        "replaced": removed,
        "folder": str(folder),
        "folderMB": made.get("written", {}).get("megabytes"),
        "zip": str(zipped) if zipped.is_file() else None,
        "zipMB": made.get("zipped", {}).get("megabytes"),
        "zipUrl": f"/output/packages/{zipped.name}" if zipped.is_file() else None,
        "withSplat": with_splat,
    }
