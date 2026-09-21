"""
Admission control, so several sessions can use one machine's GPU sanely.

A question costs far more than the HTTP request carrying it: the reranker is
one model behind a lock, and Ollama holds one set of weights. Letting twenty
questions in at once doesn't answer them faster, it makes all twenty slow and
leaves nobody able to tell why. So turns queue, in order, and a waiting
caller is told where it stands instead of staring at nothing.

Three limits, checked together:
  max_concurrent  turns running at once, whoever asked
  max_per_user    turns one user can have running at once, so one person
                  can't fill the machine while others wait
  one per conversation -- a second turn in the same conversation waits for
                  the first, because it has to read the history the first
                  one is still writing

The line is fair between users rather than first come, first served: a
freed slot goes to whoever has the least already running or ahead of them
in line (see _priority), so someone who sends ten questions at once gets
them answered in turn with everyone else's, not ahead of them. And one user
can only hold max_queued_per_user places in line, so nobody can fill the
queue itself and have everyone else turned away.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace


class Busy(Exception):
    """The queue is full, or waiting in it took too long."""


@dataclass
class GateConfig:
    max_concurrent: int = 2
    max_per_user: int = 2
    max_queued: int = 32
    max_queued_per_user: int = 4
    max_wait: float = 180.0

    @classmethod
    def from_env(cls, env: dict) -> "GateConfig":
        return cls(
            max_concurrent=max(1, int(env.get("QUERY_MAX_CONCURRENT") or 2)),
            max_per_user=max(1, int(env.get("QUERY_MAX_PER_USER") or 2)),
            max_queued=max(1, int(env.get("QUERY_MAX_QUEUED") or 32)),
            max_queued_per_user=max(1, int(env.get("QUERY_MAX_QUEUED_PER_USER") or 4)),
            max_wait=float(env.get("QUERY_QUEUE_WAIT") or 180),
        )


@dataclass
class _Ticket:
    seq: int
    user_id: str
    conv_id: str | None


class Gate:
    """Lets work in up to the configured limits, queueing the rest in order."""

    def __init__(self, cfg: GateConfig | None = None):
        self.cfg = cfg or GateConfig()
        self._cond = threading.Condition()
        self._waiting: list[_Ticket] = []
        self._running: list[_Ticket] = []
        self._seq = 0
        # Per user, the _admitted count when they last got a turn in -- the
        # round-robin tiebreak in _priority. Never pruned: it's one int per
        # account that has ever asked anything.
        self._admitted = 0
        self._last_admitted: dict[str, int] = {}

    # ------------------------------------------------------------ internals --

    def _in_flight(self, user_id: str) -> int:
        return sum(1 for t in self._running if t.user_id == user_id)

    def _free(self, ticket: _Ticket) -> bool:
        return (len(self._running) < self.cfg.max_concurrent
                and self._in_flight(ticket.user_id) < self.cfg.max_per_user
                and (ticket.conv_id is None
                     or not any(t.conv_id == ticket.conv_id for t in self._running)))

    def _priority(self, ticket: _Ticket) -> tuple[int, int, int]:
        """Where a waiting ticket stands: first by how much its user already
        has running or waiting ahead of it, so a user's third queued turn
        ranks with everyone else's third; then by who was let in least
        recently, so that when one turn ends its user doesn't win the next
        slot straight back; then by arrival. Together a burst from one
        person interleaves with everyone else's instead of going first."""
        ahead = sum(1 for t in self._waiting if t.user_id == ticket.user_id and t.seq < ticket.seq)
        return (self._in_flight(ticket.user_id) + ahead, self._last_admitted.get(ticket.user_id, -1),
                ticket.seq)

    def _next(self) -> _Ticket | None:
        """The best-placed waiting ticket that could start now. Not simply
        the best-placed one: a caller held back by its own per-user or
        per-conversation limit must not block someone else's turn behind it."""
        return min((t for t in self._waiting if self._free(t)), key=self._priority, default=None)

    def _position(self, ticket: _Ticket) -> int:
        """This ticket's 1-based place in line, in the order _next serves it."""
        mine = self._priority(ticket)
        return 1 + sum(1 for t in self._waiting if self._priority(t) < mine)

    # ---------------------------------------------------------------- admit --

    @contextmanager
    def admit(self, user_id: str, conv_id: str | None = None,
              on_position: Callable[[int], None] | None = None) -> Iterator[None]:
        """Blocks until this turn may run, then holds a slot for the body.

        on_position, if given, is called with this caller's 1-based place in
        the queue whenever it changes, and never for a turn that starts at
        once. Raises Busy if the queue is full or max_wait passes first.
        """
        with self._cond:
            if len(self._waiting) >= self.cfg.max_queued:
                raise Busy(f"{len(self._waiting)} requests are already waiting; try again shortly")
            if sum(1 for t in self._waiting if t.user_id == user_id) >= self.cfg.max_queued_per_user:
                raise Busy(f"you already have {self.cfg.max_queued_per_user} requests waiting; "
                           "try again when one has started")
            self._seq += 1
            ticket = _Ticket(self._seq, user_id, conv_id)
            self._waiting.append(ticket)
            # A newcomer can rank ahead of turns already waiting, so they
            # recheck their places rather than keep reporting stale ones.
            self._cond.notify_all()
            deadline = time.monotonic() + self.cfg.max_wait
            reported = 0
            try:
                while self._next() is not ticket:
                    position = self._position(ticket)
                    if position != reported and on_position:
                        reported = position
                        # Called with the lock held: it only queues an event.
                        on_position(position)
                    if not self._cond.wait(timeout=max(0.0, deadline - time.monotonic())):
                        raise Busy(f"still waiting after {self.cfg.max_wait:.0f}s with "
                                   f"{len(self._running)} turn(s) running")
                self._waiting.remove(ticket)
                self._running.append(ticket)
                self._admitted += 1
                self._last_admitted[user_id] = self._admitted
            except BaseException:
                if ticket in self._waiting:
                    self._waiting.remove(ticket)
                self._cond.notify_all()
                raise
        try:
            yield
        finally:
            with self._cond:
                self._running.remove(ticket)
                self._cond.notify_all()

    def stats(self) -> dict:
        with self._cond:
            return {"running": len(self._running), "queued": len(self._waiting),
                    "max_concurrent": self.cfg.max_concurrent, "max_per_user": self.cfg.max_per_user}


@dataclass
class UploadLimits:
    """Per-user ingest quota. Converting a document is the most expensive
    thing the stack does -- a 580-page scan is minutes of OCR and GPU time
    shared with everyone's questions -- so uploads are capped per user over a
    rolling window rather than per request."""

    max_file_mb: float = 200.0
    max_files: int = 20          # within the window
    max_mb: float = 500.0        # within the window
    window_hours: float = 24.0
    # Admins aren't metered: they run the machine, and filling the library is
    # their job. Their uploads are still recorded, so who added what is known.
    unlimited: bool = False

    @classmethod
    def from_env(cls, env: dict) -> "UploadLimits":
        return cls(
            max_file_mb=float(env.get("INGEST_MAX_FILE_MB") or 200),
            max_files=max(1, int(env.get("INGEST_MAX_FILES_PER_WINDOW") or 20)),
            max_mb=float(env.get("INGEST_MAX_MB_PER_WINDOW") or 500),
            window_hours=float(env.get("INGEST_QUOTA_WINDOW_HOURS") or 24),
        )

    @property
    def window_seconds(self) -> float:
        return self.window_hours * 3600

    def for_user(self, is_admin: bool) -> "UploadLimits":
        return replace(self, unlimited=True) if is_admin else self

    def rejection(self, size: int, files_used: int, bytes_used: int) -> str | None:
        """Why this file can't be accepted now, or None."""
        if self.unlimited:
            return None
        if size > self.max_file_mb * 1_000_000:
            return f"file is {size / 1e6:.0f} MB; the limit is {self.max_file_mb:.0f} MB per file"
        if files_used >= self.max_files:
            return (f"upload limit reached: {self.max_files} files per {self.window_hours:.0f}h")
        if bytes_used + size > self.max_mb * 1_000_000:
            return (f"upload limit reached: {self.max_mb:.0f} MB per {self.window_hours:.0f}h "
                    f"({bytes_used / 1e6:.0f} MB used)")
        return None

    def remaining(self, files_used: int, bytes_used: int) -> dict:
        if self.unlimited:
            return {"unlimited": True, "files_used": files_used, "mb_used": round(bytes_used / 1e6, 1)}
        return {"unlimited": False,
                "files_left": max(0, self.max_files - files_used),
                "mb_left": round(max(0.0, self.max_mb - bytes_used / 1e6), 1),
                "files_per_window": self.max_files, "mb_per_window": self.max_mb,
                "max_file_mb": self.max_file_mb, "window_hours": self.window_hours}
