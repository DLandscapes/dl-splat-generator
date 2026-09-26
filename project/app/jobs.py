"""Background capture jobs.

One job at a time, deliberately. A capture saturates the GPU and most of the
CPU; running two would make both slower and can exhaust memory during training.
Extra requests queue instead.

The job runner is a thin wrapper around tools/capture.py rather than a
reimplementation of it: the CLI stays the thing that actually works, and the UI
is one more way to drive it.

What the runner adds on top of the CLI's log is *trust*: a real percentage, the
counters the tools themselves report ("frames placed 57 of 118", "training
steps 4,200 of 10,000"), and an estimated finish time. The estimate is a model
fitted to previous runs on this machine (work\\timings.json) and corrected
live by how fast the current run is actually going. It is always labelled as
an estimate.
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

APP = Path(__file__).resolve().parent
PROJECT = APP.parent
ROOT = PROJECT.parent
TOOLS = PROJECT / "tools"
TIMINGS = ROOT / "work" / "timings.json"

# capture.py prints "[2/5] poses ..." for each stage; used to drive the bar.
STAGE_RE = re.compile(r"^\[(\d+)(b?)/(\d+)\]\s+(\S+)")
# "    ~ progress 57/118 frames placed" -- the tools' own counters, re-emitted
# by capture.py in one shape (see run() there)
PROGRESS_RE = re.compile(r"^\s*~ progress (\d+)/(\d+)\s*(.*)$")
FRAMES_RE = re.compile(r"(\d+) frames written|(\d+) images copied|reusing (\d+) frames")
KEPT_RE = re.compile(r"dropped \d+, kept (\d+)")
REGISTERED_RE = re.compile(r"registered (\d+) of (\d+) frames")
# sub-steps inside a stage, as capture.py prints them
PHASES = {
    "feature extraction": "features",
    "matching": "match",
    "mapping (this is the slow one)": "map",
    "undistorting": "undistort",
    "compressing to SPZ": "compress",
}

QUALITY = {
    # stride, training steps, max splats -- draft is for "does this scene work
    # at all", high for something worth publishing
    "draft": {"stride": 6, "steps": 3000, "max_splats": 600_000},
    "standard": {"stride": 3, "steps": 10000, "max_splats": 1_500_000},
    "high": {"stride": 2, "steps": 20000, "max_splats": 2_500_000},
}

STAGE_LABELS = {
    "frames": "Extracting frames from the video",
    "prune": "Removing blurred frames",
    "privacy": "Masking faces",
    "poses": "Solving where the camera was (COLMAP)",
    "train": "Training the splat scene (Brush)",
    "place": "Compressing and placing the scene",
    # the single-photo route (depth_splat.py prints [1/3] depth, [2/3] splats,
    # [3/3] write) -- seconds, not minutes, and no COLMAP or Brush involved
    "depth": "Estimating depth (Depth Anything V2)",
    "splats": "Turning the depth map into splats",
    "write": "Compressing and placing the scene",
    # the dense-mesh route (dense_mesh.py prints [1/4] workspace, [2/4] stereo,
    # [3/4] fuse, [4/4] mesh) -- the measurable half of the same capture
    "workspace": "Preparing the dense workspace",
    "stereo": "Matching every view against its neighbours (COLMAP)",
    "fuse": "Fusing the depth maps into one cloud",
    "mesh": "Building the surface",
    "finished": "Finished",
}
PHASE_LABELS = {
    "features": "finding features",
    "match": "matching frames",
    "map": "placing frames in 3D",
    "undistort": "undistorting",
    "compress": "compressing to SPZ",
}


# ------------------------------------------------------------- estimate

class Estimator:
    """Predicted seconds per phase, from frame count and training steps.

    Each phase costs `a + b * units`, where units is the frame count, the
    frame count squared (COLMAP's mapper), or the training steps. The starting
    coefficients come from two measured runs on an RTX A4000 (118 and 410
    frames); every finished job re-fits `b` for each phase it timed and saves
    the result, so the guesses converge on this machine's real speed.
    """

    # phase -> (a seconds, b seconds per unit, unit kind)
    DEFAULTS = {
        "frames":    (2.0,  0.015,  "n"),
        "prune":     (1.0,  0.03,   "n"),
        "privacy":   (2.0,  0.07,   "n"),
        "features":  (2.0,  0.05,   "n"),
        "match":     (3.0,  0.9,    "n"),
        "map":       (3.0,  0.0072, "n2"),
        "undistort": (1.0,  0.05,   "n"),
        "train":     (10.0, 0.022,  "steps"),
        "place":     (5.0,  0.00001, "steps"),
    }
    # the order phases run in, grouped by the stage capture.py announces
    ORDER = ["frames", "prune", "privacy", "features", "match", "map",
             "undistort", "train", "place"]
    STAGE_OF = {"frames": "frames", "prune": "prune", "privacy": "privacy",
                "features": "poses", "match": "poses", "map": "poses",
                "undistort": "poses", "train": "train", "place": "place"}

    def __init__(self) -> None:
        self.coef = {k: list(v) for k, v in self.DEFAULTS.items()}
        try:
            saved = json.loads(TIMINGS.read_text())
            for k, b in saved.get("b", {}).items():
                if k in self.coef and isinstance(b, (int, float)) and b > 0:
                    self.coef[k][1] = float(b)
        except Exception:                                    # noqa: BLE001
            pass

    def seconds(self, phase: str, n: int, steps: int) -> float:
        a, b, kind = self.coef[phase]
        units = {"n": n, "n2": n * n, "steps": steps}[kind]
        return a + b * units

    def units(self, phase: str, n: int, steps: int) -> float:
        kind = self.coef[phase][2]
        return {"n": n, "n2": n * n, "steps": steps}[kind]

    def learn(self, measured: dict, n: int, steps: int) -> None:
        """Blend measured phase durations into the coefficients and save."""
        if n <= 0:
            return
        for phase, secs in measured.items():
            if phase not in self.coef:
                continue
            a, b, kind = self.coef[phase]
            u = self.units(phase, n, steps)
            if u <= 0 or secs <= a:
                continue
            fitted = (secs - a) / u
            self.coef[phase][1] = 0.5 * b + 0.5 * fitted     # moving average
        try:
            TIMINGS.parent.mkdir(parents=True, exist_ok=True)
            TIMINGS.write_text(json.dumps(
                {"b": {k: v[1] for k, v in self.coef.items()},
                 "note": "learned seconds-per-unit for the capture ETA; "
                         "delete to reset to defaults"}, indent=1))
        except Exception:                                    # noqa: BLE001
            pass


ESTIMATOR = Estimator()


# ------------------------------------------------------------------ job

@dataclass
class Job:
    id: str
    name: str
    source: str
    quality: str
    privacy: str
    # "capture" = video -> COLMAP -> Brush (minutes); "photo" = one image ->
    # metric depth -> splats (seconds); "mesh" = the dense, measurable surface
    # from a capture already solved (minutes to an hour). One card, one queue.
    kind: str = "capture"
    scene: str = "outdoor"          # photo jobs: which metric fine-tune
    mesher: str = "delaunay"        # mesh jobs: which surface reconstruction
    # capture jobs: the part of the clip to use, in seconds. 0/0 is all of it.
    trim_start: float = 0.0
    trim_end: float = 0.0
    status: str = "queued"          # queued | running | done | failed | cancelled
    stage: str = ""
    stage_index: int = 0
    stage_count: int = 5
    phase: str = ""                 # current phase key (see Estimator.ORDER)
    lines: list = field(default_factory=list)
    error: str | None = None
    hint: str | None = None
    started: float | None = None
    finished: float | None = None
    created: float = field(default_factory=time.time)
    frames: int | None = None       # frames in play (after pruning, if known)
    registered: int | None = None
    counter: dict | None = None     # {"done", "total", "what"}
    phase_started: float | None = None
    phase_times: dict = field(default_factory=dict)   # phase -> seconds
    warnings: list = field(default_factory=list)

    # ---- progress model -------------------------------------------------

    def _steps(self) -> int:
        return QUALITY.get(self.quality, QUALITY["standard"])["steps"]

    def _n(self) -> int:
        # before the frame count is known, assume a typical clip
        return self.frames or 150

    def _enter_phase(self, phase: str) -> None:
        now = time.time()
        if self.phase and self.phase_started is not None:
            self.phase_times[self.phase] = now - self.phase_started
        self.phase = phase
        self.phase_started = now
        self.counter = None

    # A photo scene takes seconds, so its model is three fixed weights rather
    # than the capture estimator's learned per-phase coefficients. Measured on
    # this machine: depth ~8 s, splat building ~2 s, writing and compressing
    # ~1.5 s. There is too little of it to be worth learning from.
    PHOTO_PRED = {"depth": 8.0, "splats": 2.0, "write": 1.5}

    def _estimate_photo(self) -> dict:
        order = list(self.PHOTO_PRED)
        total = sum(self.PHOTO_PRED.values())
        cur = self.stage if self.stage in order else order[0]
        idx = order.index(cur)
        done = sum(self.PHOTO_PRED[p] for p in order[:idx])
        now = time.time()
        inside = now - (self.phase_started or self.started or now)
        # never let a stage claim it has finished; only the next one may say so
        done += self.PHOTO_PRED[cur] * min(0.95, max(0.0, inside / self.PHOTO_PRED[cur]))
        pct = max(0.0, min(99.0, 100.0 * done / total))
        eta = max(0, round(total - done))
        return {"pct": round(pct, 1), "eta": eta, "finishAt": round(now + eta)}

    # The dense route is dominated by one stage. Weights, not seconds: the
    # stereo pass is ~95% of it and carries its own per-view counter, so the
    # card's percentage comes almost entirely from that counter.
    MESH_PRED = {"workspace": 1.0, "stereo": 40.0, "fuse": 1.5, "mesh": 1.5}

    def _estimate_mesh(self) -> dict:
        order = list(self.MESH_PRED)
        total = sum(self.MESH_PRED.values())
        cur = self.stage if self.stage in order else order[0]
        idx = order.index(cur)
        done = sum(self.MESH_PRED[p] for p in order[:idx])
        # inside the stereo stage the tool's own counter is far better than any
        # time model, because a run is minutes to an hour depending on settings
        frac = 0.0
        if self.counter and self.counter.get("total"):
            frac = min(1.0, self.counter["done"] / self.counter["total"])
        elif self.phase_started:
            frac = min(0.9, (time.time() - self.phase_started) / 60.0)
        done += self.MESH_PRED[cur] * frac
        pct = max(0.0, min(99.0, 100.0 * done / total))
        now = time.time()
        elapsed = now - (self.started or now)
        eta = None
        if pct > 3:
            eta = max(0, round(elapsed * (100.0 - pct) / pct))
        return {"pct": round(pct, 1), "eta": eta,
                "finishAt": round(now + eta) if eta is not None else None}

    def estimate(self) -> dict:
        """Overall fraction done, seconds left and the finish time (epoch).

        Progress is weighted by predicted phase durations, so a fast phase
        does not jump the bar by a fifth. The prediction for what is left is
        scaled by how the finished phases compared with their predictions --
        a slower or faster machine corrects itself within the first minutes.
        """
        if self.status == "done":
            return {"pct": 100.0, "eta": 0, "finishAt": self.finished}
        if self.status != "running" or not self.started:
            return {"pct": 0.0, "eta": None, "finishAt": None}
        if self.kind == "photo":
            return self._estimate_photo()
        if self.kind == "mesh":
            return self._estimate_mesh()
        n, steps = self._n(), self._steps()
        pred = {p: ESTIMATOR.seconds(p, n, steps) for p in Estimator.ORDER}
        order = Estimator.ORDER
        if self.privacy == "off":
            pred["privacy"] = 0.0
        cur = self.phase if self.phase in order else order[0]
        idx = order.index(cur)

        # speed of this run against the model, from the phases already timed
        done_pred = sum(pred[p] for p in order[:idx])
        done_real = sum(self.phase_times.get(p, 0.0) for p in order[:idx])
        speed = 1.0
        if done_pred >= 20 and done_real > 0:
            speed = max(0.4, min(3.0, done_real / done_pred))

        # fraction of the current phase: its own counter if it has one, else
        # elapsed against prediction, never claiming it is finished
        now = time.time()
        in_phase = now - (self.phase_started or now)
        if self.counter and self.counter["total"]:
            frac = min(1.0, self.counter["done"] / self.counter["total"])
        else:
            frac = min(0.9, in_phase / max(1.0, pred[cur] * speed))
        total_pred = sum(pred.values())
        done_work = done_pred + pred[cur] * frac
        pct = 100.0 * done_work / total_pred if total_pred else 0.0

        remaining = (pred[cur] * (1 - frac) + sum(pred[p] for p in order[idx + 1:])) * speed
        # a phase that overruns its prediction still has *something* left
        remaining = max(remaining, 5.0)
        return {"pct": round(min(pct, 99.5), 1), "eta": round(remaining),
                "finishAt": round(now + remaining)}

    def public(self) -> dict:
        elapsed = ((self.finished or time.time()) - self.started) if self.started else 0
        est = self.estimate()
        stage_no = self.stage_index if self.status != "done" else self.stage_count
        return {
            "id": self.id, "name": self.name, "status": self.status,
            "stage": self.stage, "stageIndex": stage_no,
            "stageCount": self.stage_count,
            "stageLabel": STAGE_LABELS.get(self.stage, self.stage or "Starting"),
            "phase": self.phase, "phaseLabel": PHASE_LABELS.get(self.phase, ""),
            "quality": self.quality, "privacy": self.privacy, "kind": self.kind,
            "error": self.error, "hint": self.hint, "warnings": self.warnings[-3:],
            "elapsed": round(elapsed),
            "pct": est["pct"], "etaSeconds": est["eta"], "finishAt": est["finishAt"],
            "counter": self.counter, "frames": self.frames,
            "registered": self.registered,
            "lines": self.lines[-40:],
        }


# --------------------------------------------------------------- runner

class JobRunner:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue = queue.Queue()
        self._current: Job | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------------------------------------------------------------- public

    def submit(self, *, source: Path, name: str, quality: str, privacy: str,
               kind: str = "capture", scene: str = "outdoor",
               mesher: str = "delaunay",
               trim_start: float = 0.0, trim_end: float = 0.0) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], name=name, source=str(source),
                  quality=quality, privacy=privacy, kind=kind, scene=scene,
                  mesher=mesher, trim_start=trim_start, trim_end=trim_end,
                  stage_count={"photo": 3, "mesh": 4}.get(kind, 5))
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def listing(self) -> list:
        return [j.public() for j in
                sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)]

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        if job.status == "queued":
            job.status = "cancelled"
            return True
        if job.status == "running" and self._proc:
            job.status = "cancelled"
            self._proc.terminate()
            return True
        return False

    # ---------------------------------------------------------------- worker

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            if job.status == "cancelled":
                continue
            self._run(job)

    @staticmethod
    def _absorb(job: Job, line: str) -> None:
        """Update the job's progress model from one line of capture.py output."""
        text = line.strip()
        m = PROGRESS_RE.match(line)
        if m:
            job.counter = {"done": int(m.group(1)), "total": int(m.group(2)),
                           "what": m.group(3).strip()}
            return
        job.lines.append(line)
        m = STAGE_RE.match(text)
        if m:
            job.stage_index = int(m.group(1))
            job.stage_count = int(m.group(3))
            job.stage = m.group(4)
            if job.stage in ("frames", "prune", "privacy", "train", "place",
                             "depth", "splats", "write",
                             "workspace", "stereo", "fuse", "mesh"):
                job._enter_phase(job.stage)
            return
        if text in PHASES:
            job._enter_phase(PHASES[text])
            return
        m = FRAMES_RE.search(text)
        if m:
            job.frames = int(next(g for g in m.groups() if g))
            return
        m = KEPT_RE.search(text)
        if m:
            job.frames = int(m.group(1))
            return
        m = REGISTERED_RE.search(text)
        if m:
            job.registered = int(m.group(1))
            return
        if text.startswith("WARNING"):
            job.warnings.append(text)

    def _run(self, job: Job) -> None:
        if job.kind == "mesh":
            # `source` carries the scene name for this kind: the dense stage
            # works from work\<name>\, not from a file.
            cmd = [
                sys.executable, "-u", str(TOOLS / "dense_mesh.py"), job.name,
                "--quality", job.quality,
                "--mesher", job.mesher,
            ]
        elif job.kind == "photo":
            # One image, metric depth, no COLMAP and no Brush. The tool writes
            # into output\<name>\ and registers the scene itself, exactly as
            # capture.py does, so everything downstream is unchanged.
            cmd = [
                sys.executable, "-u", str(TOOLS / "depth_splat.py"), job.source,
                "--name", job.name,
                "--scene", job.scene,
            ]
        else:
            preset = QUALITY.get(job.quality, QUALITY["standard"])
            cmd = [
                sys.executable, "-u", str(TOOLS / "capture.py"), job.source,
                "--name", job.name,
                "--stride", str(preset["stride"]),
                "--steps", str(preset["steps"]),
                "--max-splats", str(preset["max_splats"]),
                "--privacy", job.privacy,
                # the training resolution follows the machine, capped by the
                # preset (tools/hardware.py); features/matching pick the
                # NVIDIA card or the processor by themselves
                "--quality", job.quality,
            ]
            if job.trim_start > 0:
                cmd += ["--start", f"{job.trim_start:.3f}"]
            if job.trim_end > job.trim_start:
                cmd += ["--end", f"{job.trim_end:.3f}"]
        job.status = "running"
        job.started = time.time()
        self._current = job
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            for line in self._proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                self._absorb(job, line)
            code = self._proc.wait()
            job._enter_phase("")
            if job.status == "cancelled":
                pass
            elif code == 0:
                job.status = "done"
                job.stage = "finished"
                job.stage_index = job.stage_count
                # only the capture route feeds the learned coefficients: a
                # photo job has no frame count and no training steps, and its
                # three stages are not in the estimator's table at all
                if job.kind != "photo":
                    ESTIMATOR.learn(job.phase_times, job.frames or 0,
                                    QUALITY.get(job.quality,
                                                QUALITY["standard"])["steps"])
            else:
                job.status = "failed"
                job.error, job.hint = self._explain(job.lines, code)
        except Exception as exc:                              # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
        finally:
            job.finished = time.time()
            self._proc = None
            self._current = None

    @staticmethod
    def _explain(lines: list, code: int) -> tuple[str, str | None]:
        """The FAILED line and the 'Try:' advice that follows it, if any."""
        for i in range(len(lines) - 1, -1, -1):
            if "FAILED:" in lines[i]:
                error = lines[i].split("FAILED:", 1)[1].strip()
                rest = []
                for l in lines[i + 1:i + 6]:
                    l = l.strip()
                    # Everything from "command:" on is the raw tool dump. STOP
                    # there rather than filtering those two labels out: the
                    # COLMAP log lines UNDER them were passing the filter, so a
                    # failed capture showed the user timestamped noise like
                    # "incremental_pipeline.cc:697] Discarding reconstruction"
                    # instead of the sentence written for them (2026-09-22).
                    if l.startswith(("command:", "output tail")):
                        break
                    if l:
                        rest.append(l)
                hint = next((l for l in rest if l.startswith("Try")), None)
                # the explanation lines between the failure and the advice
                # belong with the failure ("Usually the camera moved ...")
                more = [l for l in rest if l != hint]
                if more and len(error) < 400:
                    error = f"{error} {' '.join(more[:2])}"
                return error, hint
        for l in reversed(lines):
            if "Error" in l:
                return l.strip(), None
        return f"capture.py exited with code {code}", None


runner = JobRunner()
