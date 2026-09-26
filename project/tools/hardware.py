"""What this computer can do, and the fastest route through the pipeline it allows.

THE PREMISE (Marc, 2026-09-26): run as fast as the hardware allows. On a
machine with an NVIDIA card (CUDA) every step that can use it does; on a
laptop without one, each step takes the route that does not need it -- slower,
but it finishes. Failing is not fine; waiting longer is.

What each step needs (measured on this workstation, status 18 and 2026-09-26):

  frames      ffmpeg                  processor
  poses       COLMAP features/match   NVIDIA card (CUDA) if present, else the
                                      processor -- same result, slower
              COLMAP mapper           processor always
  train       Brush                   ANY graphics card (wgpu: DX12/Vulkan/
                                      Metal) -- NVIDIA, AMD, Intel, Apple. No
                                      card at all: cannot train.
  mesh        COLMAP dense stereo     NVIDIA card only (CUDA); no other route
  photo       Depth Anything V2       CUDA or Apple's MPS if the installed
                                      torch has them, else the processor

COLMAP ON THE PROCESSOR -- THE THREAD LIMIT. COLMAP's default (all logical
cores) CRASHES this build's CPU feature extraction: measured on 80 phone
frames (1080 x 1920) on a 24-thread machine,

    threads   8: 0 of 3 crashed, 38 s        16: 1 of 5 crashed, 27 s
    threads  12: 0 of 5 crashed, 31 s        24: 4 of 4 crashed

(access violation / stack overrun -- not memory: it died at 5 GB of 32).
And each thread holds about 0.6 GB for a phone frame. So the processor route
uses at most 12 threads, fewer when memory is short, and if it crashes anyway
the caller retries with half as many (capture.py). Past 12 the gain is small.

TRAINING RESOLUTION. Brush holds every training view in memory, and the
graphics card has to render at that size, so the sharpest resolution a
machine can train at follows its graphics memory and its free RAM. The tiers
below are REASONED, not measured on weak laptops (none was available): the
top tier is this workstation (16 GB NVIDIA, 32 GB RAM); the rest step down
so that a laptop gets a scene rather than a crash.

Everything here is read-only: it asks the system, it changes nothing.
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from functools import lru_cache

COLMAP_THREAD_CAP = 12           # measured: above this this COLMAP build crashes on CPU
GB_PER_SIFT_THREAD = 0.6         # measured: 0.58 GB per thread on 1080 x 1920 frames
KEEP_FREE_GB = 2.0               # leave this much for the rest of the machine

# (graphics memory GB, free RAM GB) -> the longest image edge Brush trains at.
# First tier that the machine meets wins. 1920 = a phone video frame's own size.
TRAIN_TIERS = [
    (12, 12, 1920),
    (8, 8, 1600),
    (6, 6, 1200),
    (4, 4, 960),
    (0, 0, 720),
]


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ------------------------------------------------------------------ memory

def memory() -> dict:
    """Total and free RAM, and -- on Windows -- free COMMIT (RAM + page file),
    which is what an allocation actually draws on: Brush failed twice on this
    machine with RAM to spare and the commit charge exhausted."""
    if sys.platform == "win32":
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        ms = MS(); ms.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            g = 1 << 30
            return {"total_gb": ms.ullTotalPhys / g, "free_gb": ms.ullAvailPhys / g,
                    "commit_free_gb": ms.ullAvailPageFile / g}
    if sys.platform == "darwin":
        total = int(_run(["sysctl", "-n", "hw.memsize"]) or 0) / (1 << 30)
        page = int(_run(["sysctl", "-n", "hw.pagesize"]) or 4096)
        vm = _run(["vm_stat"])
        free_pages = sum(int(m) for m in re.findall(
            r"Pages (?:free|inactive|speculative|purgeable):\s+(\d+)", vm))
        free = free_pages * page / (1 << 30)
        return {"total_gb": total, "free_gb": free, "commit_free_gb": free}
    try:
        info = dict(line.split(":", 1) for line in open("/proc/meminfo"))
        kb = lambda k: int(info[k].split()[0]) / (1 << 20)          # noqa: E731
        return {"total_gb": kb("MemTotal"), "free_gb": kb("MemAvailable"),
                "commit_free_gb": kb("MemAvailable")}
    except (OSError, KeyError, ValueError):
        return {"total_gb": 0.0, "free_gb": 0.0, "commit_free_gb": 0.0}


# ---------------------------------------------------------------- the rest

def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    if sys.platform == "darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or platform.processor()
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


def _nvidia() -> list[dict]:
    """NVIDIA cards with a working driver -- nvidia-smi ships with it."""
    smi = shutil.which("nvidia-smi") or (
        r"C:\Windows\System32\nvidia-smi.exe" if sys.platform == "win32" else None)
    if not smi or not os.path.exists(smi):
        return []
    out = _run([smi, "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits"])
    cards = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            try:
                cards.append({"name": parts[0], "memory_gb": float(parts[1]) / 1024,
                              "free_gb": float(parts[2]) / 1024, "driver": parts[3]})
            except ValueError:
                pass
    return cards


BASIC_ADAPTERS = ("microsoft basic", "remote display", "virtual", "parsec", "citrix")


def _adapters() -> list[dict]:
    """Every graphics adapter, any maker: what Brush (wgpu) could run on."""
    found = []
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | "
                    "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"], timeout=20)
        try:
            data = json.loads(out) if out.strip() else []
            for a in data if isinstance(data, list) else [data]:
                ram = a.get("AdapterRAM") or 0
                # AdapterRAM is a 32-bit field: it tops out near 4 GB, so it is a
                # floor, not the card's memory; NVIDIA's own figure replaces it
                found.append({"name": (a.get("Name") or "").strip(),
                              "memory_gb": ram / (1 << 30) if ram > 0 else None})
        except (ValueError, AttributeError):
            pass
    elif sys.platform == "darwin":
        out = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=20)
        try:
            for a in json.loads(out).get("SPDisplaysDataType", []):
                vram = a.get("spdisplays_vram") or a.get("spdisplays_vram_shared") or ""
                m = re.match(r"(\d+)\s*(GB|MB)", vram)
                gb = (int(m[1]) / (1 if m[2] == "GB" else 1024)) if m else None
                found.append({"name": a.get("sppci_model", "").strip(), "memory_gb": gb})
        except ValueError:
            pass
    else:
        for line in _run(["lspci"]).splitlines():
            if re.search(r"VGA|3D controller|Display controller", line):
                found.append({"name": line.split(":", 2)[-1].strip(), "memory_gb": None})
    return [a for a in found if a["name"]
            and not any(b in a["name"].lower() for b in BASIC_ADAPTERS)]


@lru_cache(maxsize=1)
def _fixed() -> dict:
    """The parts that do not change while the app runs (cached)."""
    return {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu": _cpu_name(),
        "threads": os.cpu_count() or 1,
        "nvidia": _nvidia(),
        "adapters": _adapters(),
    }


def probe() -> dict:
    """Everything the plan needs; memory is read fresh every call."""
    return {**_fixed(), "memory": memory()}


# ------------------------------------------------------------------- plan

def plan(hw: dict | None = None, *, compute: str = "auto", quality: str = "standard",
         views: int = 0) -> dict:
    """The route for this machine.

    compute  auto (the fastest this machine has) | gpu | cpu -- cpu forces the
             processor route for COLMAP even with an NVIDIA card, which is how
             the laptop route is tested on this workstation
    quality  draft | standard | high: caps the training resolution below what
             the hardware allows (draft is for "does this scene work at all")
    views    training views, when known: lets the memory check size the set
    """
    hw = hw or probe()
    mem = hw["memory"]
    free = min(mem["free_gb"], mem["commit_free_gb"]) if mem["commit_free_gb"] else mem["free_gb"]
    nvidia = hw["nvidia"]
    notes = []

    # -- poses: COLMAP
    colmap_gpu = bool(nvidia) and compute != "cpu"
    if compute == "gpu" and not nvidia:
        notes.append("an NVIDIA card was asked for, but none was found: "
                     "the camera solve runs on the processor")
    by_memory = int(max(1.0, free - KEEP_FREE_GB) / GB_PER_SIFT_THREAD)
    threads = max(1, min(COLMAP_THREAD_CAP, hw["threads"] - 1 if hw["threads"] > 2 else 1, by_memory))

    # -- train: Brush (any graphics card)
    adapters = hw["adapters"]
    can_train = bool(adapters or nvidia)
    vram = max([c["memory_gb"] for c in nvidia], default=0.0)
    if not vram:
        # not NVIDIA: integrated graphics share the RAM, a discrete AMD/Intel
        # card reports at least its 32-bit floor -- take the larger, cautiously
        known = [a["memory_gb"] for a in adapters if a["memory_gb"]]
        vram = max(known, default=0.0)
    edge = next(e for g, r, e in TRAIN_TIERS if vram >= g and free >= r)
    wanted = {"draft": 960, "standard": 1920, "high": 1920}.get(quality, 1920)
    max_resolution = min(edge, wanted)
    if max_resolution < wanted:
        notes.append(f"training at {max_resolution} px on the long side, not {wanted}: "
                     f"that is what this machine's graphics memory ({vram:.0f} GB) and "
                     f"free memory ({free:.0f} GB) allow -- softer, but it finishes")

    # -- mesh: COLMAP dense (CUDA only)
    mesh_ok = bool(nvidia)

    gpu_name = nvidia[0]["name"] if nvidia else (adapters[0]["name"] if adapters else None)
    return {
        "colmap": {"gpu": colmap_gpu, "threads": threads},
        "train": {"ok": can_train, "max_resolution": max_resolution, "gpu": gpu_name,
                  "why": None if can_train else
                  "no graphics adapter was found, and training a splat scene needs one "
                  "(any maker: NVIDIA, AMD, Intel or Apple)"},
        "mesh": {"ok": mesh_ok, "why": None if mesh_ok else
                 "the measurable mesh needs an NVIDIA graphics card (CUDA): COLMAP's "
                 "dense stereo has no other route"},
        "free_gb": round(free, 1),
        "notes": notes,
    }


def summary(hw: dict | None = None, p: dict | None = None) -> str:
    """One line for a log or the panel."""
    hw = hw or probe()
    p = p or plan(hw)
    gpu = hw["nvidia"][0]["name"] if hw["nvidia"] else (
        hw["adapters"][0]["name"] if hw["adapters"] else "no graphics adapter")
    solve = "on the NVIDIA card" if p["colmap"]["gpu"] else f"on the processor ({p['colmap']['threads']} threads)"
    # plain ASCII: it goes through subprocess pipes and logs of any encoding
    return (f"{gpu}, {hw['threads']} threads, {hw['memory']['total_gb']:.0f} GB RAM "
            f"({p['free_gb']:.0f} GB free) - camera solve {solve}; "
            + (f"training up to {p['train']['max_resolution']} px" if p["train"]["ok"]
               else "cannot train here"))


if __name__ == "__main__":
    hw = probe()
    p = plan(hw, compute=(sys.argv[1] if len(sys.argv) > 1 else "auto"))
    print(json.dumps({"hardware": hw, "plan": p}, indent=1, default=str))
    print(summary(hw, p))
