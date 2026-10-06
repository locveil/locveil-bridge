"""Scenario jobs (SCN-19, docs/design/scenarios/scenario_jobs.md §3–§4).

A **job** is one run of a room's switch chain — a *switch* to a scenario, a *stop*
(deactivate the room) or a *reconcile* (one device forced, SCN-11). It is created at the
``ScenarioManager`` chokepoint so every door (canonical, REST, WB card, force-reconcile)
produces one, and it is the record ``GET /scenario/jobs/{id}`` serves.

The **registry** holds, per room, the running job plus a ring of the last finished ones,
and the room's ``asyncio.Lock`` — acquired non-blocking: a request that finds the room
busy is refused (``ScenarioJobInProgress``), never queued. Nothing is persisted: after a
restart every job id is unknown, which is the honest answer (the bridge cannot vouch for
a chain it did not finish).

Pure domain: dataclasses + asyncio, no imports outward.
"""

import asyncio
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

RECENT_JOBS_PER_ROOM = 20

JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_NOT_CONFIRMED = "not_confirmed"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(moment: Optional[datetime]) -> Optional[str]:
    """ISO-8601 UTC with millisecond precision and a ``Z`` — the stamp every job
    record field and job event carries."""
    if moment is None:
        return None
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def new_job_id(room_id: str) -> str:
    """Opaque to consumers; ``j-<room>-<utc stamp>-<4 hex>`` for humans reading logs."""
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"j-{room_id}-{stamp}-{secrets.token_hex(2)}"


@dataclass
class JobStep:
    """One planned action of a phase with its execution status."""

    index: int
    device_id: str
    domain: str
    target: Any
    command: str
    zone: Optional[str] = None
    feedback: bool = False
    poll_timeout_ms: Optional[int] = None
    delay_ms: int = 0
    pre_delay_ms: int = 0
    status: str = STEP_PENDING
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None

    def planned(self) -> Dict[str, Any]:
        """The step as a ``scenario_phase`` event lists it (no status)."""
        return {
            "index": self.index,
            "device_id": self.device_id,
            "domain": self.domain,
            "target": self.target,
            "command": self.command,
            "zone": self.zone,
            "feedback": self.feedback,
            "poll_timeout_ms": self.poll_timeout_ms,
            "delay_ms": self.delay_ms,
            "pre_delay_ms": self.pre_delay_ms,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.planned(),
            "status": self.status,
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            "error": self.error,
        }


@dataclass
class JobPhase:
    phase: str  # "teardown" | "activation"
    steps: List[JobStep] = field(default_factory=list)
    manual_steps: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "steps": [s.to_dict() for s in self.steps],
            "manual_steps": list(self.manual_steps),
        }


@dataclass
class ScenarioJob:
    """The record of one run of a room's chain (§3). ``kind``: switch | stop |
    reconcile; ``target``: the scenario id or ``none``; ``from_``: the room's active
    scenario at acceptance (``none`` if idle); ``source``: canonical | rest | wb_card."""

    job_id: str
    room_id: str
    kind: str
    target: str
    from_: str
    source: str
    max_duration_ms: int
    started_at: datetime = field(default_factory=utc_now)
    state: str = JOB_RUNNING
    finished_at: Optional[datetime] = None
    phases: List[JobPhase] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    powered_off: List[str] = field(default_factory=list)
    result_scenario: Optional[str] = None
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    @property
    def duration_ms(self) -> Optional[int]:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def elapsed_ms(self) -> int:
        return int((utc_now() - self.started_at).total_seconds() * 1000)

    @property
    def succeeded(self) -> bool:
        return self.state == JOB_SUCCEEDED

    def finish(self, result_scenario: Optional[str]) -> None:
        """Seal the record: a job is ``failed`` when any step failed or was not
        confirmed — the chain still completed (today's semantics)."""
        self.finished_at = utc_now()
        self.state = JOB_FAILED if self.failures else JOB_SUCCEEDED
        self.result_scenario = result_scenario
        self.done.set()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "room_id": self.room_id,
            "kind": self.kind,
            "target": self.target,
            "from": self.from_,
            "source": self.source,
            "state": self.state,
            "max_duration_ms": self.max_duration_ms,
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            "duration_ms": self.duration_ms,
            "phases": [p.to_dict() for p in self.phases],
            "failures": list(self.failures),
            "powered_off": list(self.powered_off),
            "result_scenario": self.result_scenario,
        }


class ScenarioJobInProgress(Exception):
    """Raised at the chokepoint when the room's lock is held: the request is refused,
    not queued (decision 10). ``job`` is the running job the caller may join."""

    def __init__(self, job: ScenarioJob):
        super().__init__(
            f"a scenario job is running in room '{job.room_id}' "
            f"(job {job.job_id}, {job.kind} → {job.target})"
        )
        self.job = job


class JobRegistry:
    """Per-room running job + ring of recent jobs + the room lock (§3, §4)."""

    def __init__(self, recent_per_room: int = RECENT_JOBS_PER_ROOM):
        self._recent_per_room = recent_per_room
        self._running: Dict[str, ScenarioJob] = {}
        self._recent: Dict[str, Deque[ScenarioJob]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def lock_for(self, room_id: str) -> asyncio.Lock:
        lock = self._locks.get(room_id)
        if lock is None:
            lock = self._locks[room_id] = asyncio.Lock()
        return lock

    async def accept(self, job: ScenarioJob) -> None:
        """Take the room's lock for ``job`` — non-blocking. Raises
        ``ScenarioJobInProgress`` when a job already holds it."""
        lock = self.lock_for(job.room_id)
        running = self._running.get(job.room_id)
        if running is not None or lock.locked():
            if running is None:  # lock held without a record — cannot happen; be loud
                raise RuntimeError(f"room '{job.room_id}' lock held by no job")
            raise ScenarioJobInProgress(running)
        # An unlocked asyncio.Lock with no waiters grants synchronously — the await
        # below does not suspend, so nothing can slip in between the check and the grant.
        await lock.acquire()
        self._running[job.room_id] = job

    def release(self, job: ScenarioJob) -> None:
        """Move ``job`` from running to the ring and free the room's lock."""
        if self._running.get(job.room_id) is job:
            del self._running[job.room_id]
        ring = self._recent.get(job.room_id)
        if ring is None:
            ring = self._recent[job.room_id] = deque(maxlen=self._recent_per_room)
        ring.appendleft(job)
        lock = self.lock_for(job.room_id)
        if lock.locked():
            lock.release()

    def running(self, room_id: str) -> Optional[ScenarioJob]:
        return self._running.get(room_id)

    def running_jobs(self) -> List[ScenarioJob]:
        return list(self._running.values())

    def recent(self, room_id: str) -> List[ScenarioJob]:
        return list(self._recent.get(room_id, ()))

    def rooms(self) -> List[str]:
        return sorted(set(self._running) | set(self._recent))

    def get(self, job_id: str) -> Optional[ScenarioJob]:
        for job in self._running.values():
            if job.job_id == job_id:
                return job
        for ring in self._recent.values():
            for job in ring:
                if job.job_id == job_id:
                    return job
        return None
