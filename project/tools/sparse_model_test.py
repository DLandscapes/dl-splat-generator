"""Regression test: the dense stage must mesh the BIGGEST sparse model.

    python tools/sparse_model_test.py

COLMAP writes sparse/0, sparse/1, ... when a solve fragments, numbered in the
order the mapper finished them -- not by size. On IMG_8950_clean sparse/0 held
2 images and sparse/1 held 409. dense_mesh.py and /api/mesh both took sparse/0,
so the dense stage meshed two frames while the panel quoted "409 views",
because it counted the images folder instead of the model.

This builds small synthetic COLMAP models in a temporary folder -- no COLMAP,
no GPU, about a second -- and checks the choice and the count the API reports.
Nothing is written into the project; the real IMG_8950_clean solve, if it is
still on disk, is only read.

Exit code 0 if every check passed, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
import struct
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PROJECT = TOOLS.parent
ROOT = PROJECT.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PROJECT / "app"))

import capture          # noqa: E402  -- StageError
import colmap_cameras   # noqa: E402  -- the real reader, to prove the fixtures are real
import dense_mesh       # noqa: E402  -- called by attribute, so a patched find_model is seen

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    checks.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def write_images_bin(model: Path, n: int) -> Path:
    """A real images.bin in COLMAP's binary layout, holding n images.

    Same record shape colmap_cameras.read_images_bin parses: id, quaternion,
    translation, camera id, a NUL-terminated name, and no 2D points. Written
    in full rather than faking the 8-byte count, so the fixture cannot pass a
    check the real format would fail.
    """
    model.mkdir(parents=True, exist_ok=True)
    out = bytearray(struct.pack("<Q", n))
    for i in range(n):
        out += struct.pack("<idddddddi", i + 1, 1.0, 0.0, 0.0, 0.0,
                           float(i), 0.0, 0.0, 1)
        out += f"frame_{i + 1:05d}.jpg".encode("ascii") + b"\0"
        out += struct.pack("<Q", 0)
    path = model / "images.bin"
    path.write_bytes(bytes(out))
    return path


def chosen(work: Path) -> tuple[str, str]:
    """find_model's answer, relative to the work folder, and what it printed.

    An exception is an ANSWER here, not a crash: a regression that raises where
    a model exists must show up as a failed check with the others still run,
    not stop the test on the first case it breaks.
    """
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said):
            model = dense_mesh.find_model(work)
    except Exception as exc:                                    # noqa: BLE001
        return f"raised {type(exc).__name__}", said.getvalue()
    return model.relative_to(work).as_posix(), said.getvalue()


def run() -> list[tuple[str, bool, str]]:
    checks.clear()
    with tempfile.TemporaryDirectory(prefix="sparse_model_test_") as tmp:
        tmp = Path(tmp)

        print("\n[1] the fixtures are real COLMAP files")
        f = write_images_bin(tmp / "fixture" / "sparse" / "0", 7)
        check("the real reader parses a synthetic images.bin",
              len(colmap_cameras.read_images_bin(f)) == 7)
        check("registered() reads the image count", dense_mesh.registered(f.parent) == 7)
        check("registered() is 0 for a folder with no images.bin",
              dense_mesh.registered(tmp / "fixture" / "nowhere") == 0)
        bad = tmp / "fixture" / "truncated"
        bad.mkdir(parents=True)
        (bad / "images.bin").write_bytes(b"\x01\x02\x03")
        check("registered() is 0 for a truncated images.bin, not a crash",
              dense_mesh.registered(bad) == 0)

        print("\n[2] the model chosen is the biggest, whatever its number")
        w = tmp / "the_bug"                          # the IMG_8950_clean shape
        write_images_bin(w / "sparse" / "0", 2)
        write_images_bin(w / "sparse" / "1", 409)
        rel, said = chosen(w)
        check("sparse/0 = 2, sparse/1 = 409  ->  sparse/1", rel == "sparse/1", rel)
        check("the choice is printed, not silent",
              "using 1, the biggest" in said, said.strip())

        w = tmp / "big_first"
        write_images_bin(w / "sparse" / "0", 409)
        write_images_bin(w / "sparse" / "1", 2)
        rel, _ = chosen(w)
        check("sparse/0 = 409, sparse/1 = 2  ->  sparse/0", rel == "sparse/0", rel)

        w = tmp / "three"
        write_images_bin(w / "sparse" / "0", 5)
        write_images_bin(w / "sparse" / "1", 40)
        write_images_bin(w / "sparse" / "2", 300)
        rel, _ = chosen(w)
        check("5 / 40 / 300  ->  sparse/2, the last and the biggest", rel == "sparse/2", rel)

        w = tmp / "empty_slot"
        (w / "sparse" / "0").mkdir(parents=True)     # a folder COLMAP left empty
        write_images_bin(w / "sparse" / "1", 12)
        rel, _ = chosen(w)
        check("a numbered folder without images.bin is skipped", rel == "sparse/1", rel)

        print("\n[3] the cases that must NOT change")
        w = tmp / "best"
        write_images_bin(w / "sparse_best", 100)
        write_images_bin(w / "sparse" / "0", 409)
        rel, _ = chosen(w)
        check("sparse_best wins -- capture.py already made the choice",
              rel == "sparse_best", rel)

        w = tmp / "flat"
        write_images_bin(w / "sparse", 60)
        rel, _ = chosen(w)
        check("a flat sparse/ with no numbered models is still found", rel == "sparse", rel)

        w = tmp / "nothing"
        (w / "sparse").mkdir(parents=True)
        try:
            dense_mesh.find_model(w)
            check("no model at all raises StageError", False, "returned instead")
        except capture.StageError:
            check("no model at all raises StageError", True)

        print("\n[4] /api/mesh reports the model's images, not the folder's")
        try:
            import main
        except Exception as exc:                                # noqa: BLE001
            check("the backend imports", False, f"{type(exc).__name__}: {exc}")
        else:
            work = tmp / "root" / "work" / "fragmented"
            write_images_bin(work / "sparse" / "0", 2)
            write_images_bin(work / "sparse" / "1", 409)
            (work / "images").mkdir(parents=True)
            for i in range(450):                     # more on disk than registered
                (work / "images" / f"frame_{i + 1:05d}.jpg").write_bytes(b"")
            saved = main.ROOT, main.OUTPUT
            main.ROOT, main.OUTPUT = tmp / "root", tmp / "root" / "output"
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    state = main.mesh_status("fragmented")
            finally:
                main.ROOT, main.OUTPUT = saved
            check("views come from the model: 409", state["views"] == 409, str(state["views"]))
            check("frames on disk are reported beside it: 450",
                  state["framesOnDisk"] == 450, str(state["framesOnDisk"]))
            check("the model named is sparse/1", state["model"] == "1", str(state["model"]))
            want = round(dense_mesh.estimate_minutes(
                409, dense_mesh.QUALITY["draft"]["size"], dense_mesh.QUALITY["draft"]["geometric"]))
            check("the time estimate is for 409 views, not 450 or 2",
                  state["estimateMinutes"].get("draft") == want,
                  f"{state['estimateMinutes'].get('draft')} min, expected {want}")

    print("\n[5] the real solve that exposed it (read only)")
    real = ROOT / "work" / "IMG_8950_clean"
    models = [d for d in (real / "sparse").glob("*") if (d / "images.bin").is_file()]
    if len(models) < 2:
        print("  SKIP  work/IMG_8950_clean no longer has a fragmented solve")
    else:
        rel, _ = chosen(real)
        biggest = max(models, key=dense_mesh.registered)
        check("IMG_8950_clean: the biggest of its models is the one chosen",
              rel == biggest.relative_to(real).as_posix(),
              ", ".join(f"{m.name}:{dense_mesh.registered(m)}" for m in sorted(models)))
    return checks


def main_() -> int:
    print("Sparse-model choice -- regression test")
    run()
    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
