"""An in-process journal of what KubeSage and the cluster just did, for the web UI's live log.

Code logs to the "kubesage" logger with `extra={"unit": ..., "stage": ...}`; `kubesage serve` attaches JOURNAL to
it. Stages follow the dataflow: 1 metrics, 2 triage, 3 remediation and planning, 4 autoscaling, 5 nodes and pods.
Nothing is written here unless something real happened.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone

LOGGER = "kubesage"


class Journal(logging.Handler):
    def __init__(self, size: int = 2000):
        super().__init__(level=logging.INFO)
        self._events: deque[dict] = deque(maxlen=size)
        self._next = 1
        self._guard = threading.Lock()
        self.sink = None  # called with each event outside the lock; `kubesage serve` mirrors them into the RAM ring

    def emit(self, record: logging.LogRecord) -> None:
        self.add(record.getMessage(), unit=getattr(record, "unit", record.name.rsplit(".", 1)[-1]),
                 stage=getattr(record, "stage", None), level=record.levelname.lower(), at=record.created)

    def add(self, msg: str, *, unit: str, stage: int | None = None, level: str = "info",
            at: float | str | None = None) -> None:
        if isinstance(at, (int, float)):
            at = datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="milliseconds")
        event = {"at": at or datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "unit": unit, "stage": stage,
                 "level": level, "msg": msg}
        with self._guard:
            event["id"] = self._next
            self._events.append(event)
            self._next += 1
        if self.sink:
            self.sink(event)

    def since(self, after: int = 0, limit: int = 500) -> tuple[list[dict], int]:
        with self._guard:
            events = [e for e in self._events if e["id"] > after][-limit:]
            return events, self._next - 1


JOURNAL = Journal()


def attach() -> Journal:
    """Route INFO and above from every kubesage.* logger into JOURNAL (idempotent)."""
    logger = logging.getLogger(LOGGER)
    logger.setLevel(logging.INFO)
    if JOURNAL not in logger.handlers:
        logger.addHandler(JOURNAL)
    return JOURNAL


def log(unit: str, msg: str, *, stage: int | None = None, level: int = logging.INFO, name: str = LOGGER) -> None:
    logging.getLogger(name).log(level, msg, extra={"unit": unit, "stage": stage})
