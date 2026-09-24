"""Shared writer for output/scenes.json, the list the viewer reads.

Both capture.py (video -> splats) and depth_splat.py (one image -> splats)
register their results here, so the sidebar lists everything that has been
generated. Kept in one place so the two producers cannot drift apart on the
format.
"""
from __future__ import annotations

import json
from pathlib import Path


def register(out_root: Path, name: str, *, served: Path, original: Path | None = None,
             cameras: Path | None = None, created: str, method: str | None = None) -> Path:
    """Add or replace a scene in output/scenes.json, newest first.

    `served` is the file the viewer should load (the compressed one where there
    is a choice); `original` is kept for provenance.
    """
    index_path = out_root / "scenes.json"
    try:
        data = json.loads(index_path.read_text())
        scenes = [s for s in data.get("scenes", []) if s.get("name") != name]
    except Exception:                                       # noqa: BLE001
        scenes = []

    scenes.append({
        "name": name,
        "ply": f"/output/{name}/{served.name}",
        "original": f"/output/{name}/{original.name}" if original else None,
        "cameras": f"/output/{name}/{cameras.name}" if cameras else None,
        "megabytes": round(served.stat().st_size / 1e6, 1),
        "method": method,
        "created": created,
    })
    scenes.sort(key=lambda s: s.get("created") or "", reverse=True)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(
        {"format": "dlscenes", "version": 1, "scenes": scenes}, indent=1))
    return index_path
