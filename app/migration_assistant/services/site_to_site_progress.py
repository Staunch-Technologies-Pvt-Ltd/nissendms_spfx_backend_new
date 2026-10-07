"""Live progress + run control for Site-to-Site copy jobs.

One `CopyRun` per job while its copy (or verification) is running in this
process. It holds:

- the live counters the UI streams (files/bytes done, failures, skips, ...),
  plus a rolling window of byte samples to derive speed and time left;
- a bounded log of per-item events with sequence numbers, so a stream
  subscriber only receives what it hasn't seen yet;
- the pause gate and cancel flag the copy workers check before each item.

State lives in memory on purpose: it changes many times a second and only
matters while the run is alive. Everything durable (item statuses, the final
summary, copy_status) is still written to the database by the mover, so a
job survives a restart as "interrupted" and can be resumed, and a process
that doesn't own the run (another worker) falls back to the DB snapshot.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

# Copy states after which a run is over and the stream can close.
TERMINAL_STATES = {"cancelled", "failed", "completed", "completed_with_warnings", "completed_with_errors", "interrupted"}

_SPEED_WINDOW_SECONDS = 30.0
_MAX_EVENTS = 500


@dataclass
class CopyRun:
    job_id: int
    state: str = "running"  # running | paused | cancelling | verifying | <terminal>
    phase: str = "folders"  # folders | files | verifying | done

    files_total: int = 0
    bytes_total: int = 0
    folders_total: int = 0
    files_done: int = 0  # copied (incl. already-copied on resume)
    bytes_done: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    folders_done: int = 0
    folders_failed: int = 0
    metadata_attention: int = 0
    permissions_attention: int = 0
    verify_total: int = 0
    verify_done: int = 0

    in_flight: set[str] = field(default_factory=set)
    started_monotonic: float = field(default_factory=time.monotonic)
    _samples: deque = field(default_factory=lambda: deque(maxlen=400))
    _events: deque = field(default_factory=lambda: deque(maxlen=_MAX_EVENTS))
    _seq: int = 0
    _bytes_this_run: int = 0

    pause_gate: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested: bool = False
    _changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        self.pause_gate.set()  # not paused

    # --- mutation (called by the copy engine) ---------------------------

    def add_bytes(self, n: int) -> None:
        self.bytes_done += n
        self._bytes_this_run += n
        self._samples.append((time.monotonic(), self._bytes_this_run))

    def event(self, path: str, kind: str, status: str, detail: str | None = None) -> None:
        self._seq += 1
        self._events.append({"seq": self._seq, "path": path, "kind": kind, "status": status, "detail": detail})

    async def notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    async def wait_for_change(self, timeout: float) -> None:
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    # --- control --------------------------------------------------------

    def pause(self) -> None:
        if self.state == "running":
            self.pause_gate.clear()
            self.state = "paused"

    def resume(self) -> None:
        if self.state == "paused":
            self.pause_gate.set()
            self.state = "running"

    def cancel(self) -> None:
        self.cancel_requested = True
        self.state = "cancelling"
        self.pause_gate.set()  # let paused workers wake up and exit

    # --- read -----------------------------------------------------------

    def speed_bps(self) -> float:
        """Bytes/second over the last `_SPEED_WINDOW_SECONDS`. Graph copies
        each file server-side, so bytes arrive in per-file steps — a window
        smooths that into a usable rate."""
        if self.state == "paused" or len(self._samples) < 2:
            return 0.0
        now = time.monotonic()
        recent = [s for s in self._samples if now - s[0] <= _SPEED_WINDOW_SECONDS]
        if len(recent) < 2:
            recent = list(self._samples)[-2:]
        (t0, b0), (t1, b1) = recent[0], recent[-1]
        # Measure up to "now", not the last sample, so the rate decays while
        # a big file is still copying instead of freezing at its last value.
        elapsed = max(now - t0, t1 - t0, 1e-6)
        return max(b1 - b0, 0) / elapsed

    def snapshot(self, since_seq: int = 0) -> dict:
        speed = self.speed_bps()
        remaining = max(self.bytes_total - self.bytes_done, 0)
        eta = int(remaining / speed) if speed > 0 and self.phase == "files" else None
        return {
            "type": "progress",
            "job_id": str(self.job_id),
            "state": self.state,
            "phase": self.phase,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "files_failed": self.files_failed,
            "files_skipped": self.files_skipped,
            "folders_total": self.folders_total,
            "folders_done": self.folders_done,
            "folders_failed": self.folders_failed,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "metadata_attention": self.metadata_attention,
            "permissions_attention": self.permissions_attention,
            "verify_total": self.verify_total,
            "verify_done": self.verify_done,
            "speed_bps": round(speed),
            "eta_seconds": eta,
            "elapsed_seconds": int(time.monotonic() - self.started_monotonic),
            "in_flight": sorted(self.in_flight)[:8],
            "events": [e for e in self._events if e["seq"] > since_seq],
            "seq": self._seq,
        }

    def counters(self) -> dict:
        """The durable subset, persisted to `SiteToSiteJob.copy_summary`."""
        return {
            "files_total": self.files_total,
            "files_done": self.files_done,
            "files_failed": self.files_failed,
            "files_skipped": self.files_skipped,
            "folders_total": self.folders_total,
            "folders_done": self.folders_done,
            "folders_failed": self.folders_failed,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "metadata_attention": self.metadata_attention,
            "permissions_attention": self.permissions_attention,
            "elapsed_seconds": int(time.monotonic() - self.started_monotonic),
        }


_runs: dict[int, CopyRun] = {}


def get(job_id: int) -> CopyRun | None:
    return _runs.get(job_id)


def start(job_id: int) -> CopyRun:
    run = CopyRun(job_id=job_id)
    _runs[job_id] = run
    return run


def is_active(job_id: int) -> bool:
    run = _runs.get(job_id)
    return run is not None and run.task is not None and not run.task.done()
