"""Shared in-memory background-job store, used by the various long-running
AI pipelines (chapter draft/revise/finalize, book-wide sweeps, translation,
scene draft/revise) that run in a daemon thread and get polled by the
frontend for progress instead of blocking one HTTP request.

Single-user local app, so no persistence is needed - each JobStore instance
is just a dict plus a TTL sweep to bound long-session memory growth. The
stored job *shape* (which fields beyond done/error/result a job carries)
is deliberately left up to each caller; this only owns id generation,
creation-time stamping, and pruning."""
from __future__ import annotations

import time
import uuid
from typing import Any

_JOB_TTL_SECONDS = 3600


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    def _prune_old(self) -> None:
        cutoff = time.time() - _JOB_TTL_SECONDS
        stale = [job_id for job_id, job in self._jobs.items() if job.get("done") and job.get("_created", 0) < cutoff]
        for job_id in stale:
            del self._jobs[job_id]

    def create(self, initial: dict[str, Any]) -> str:
        """Prunes stale entries, then seeds a new job (done=False/error=None/
        result=None as defaults, overridable by initial) and returns its id."""
        self._prune_old()
        job_id = uuid.uuid4().hex
        self._jobs[job_id] = {"done": False, "error": None, "result": None, **initial, "_created": time.time()}
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)
