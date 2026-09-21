"""
The admission gate and the ingest quota. The gate is threaded, so these tests
drive it with real threads and short timeouts rather than mocking the clock:
what matters is that a slot is never held by two turns at once and that
nothing is left stuck when a turn raises.
"""

from __future__ import annotations

import threading
import time

import pytest
from limits import Busy, Gate, GateConfig, Stopped, UploadLimits


def gate(**overrides) -> Gate:
    cfg = GateConfig(max_concurrent=1, max_per_user=1, max_queued=8, max_wait=2.0)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return Gate(cfg)


class Runner:
    """Runs gate.admit in a thread and records when it got in and let go."""

    def __init__(self, g: Gate, user: str = "u1", conv: str | None = None):
        self.gate, self.user, self.conv = g, user, conv
        self.admitted = threading.Event()
        self.release = threading.Event()
        self.positions: list[int] = []
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            with self.gate.admit(self.user, self.conv, on_position=self.positions.append):
                self.admitted.set()
                self.release.wait(5)
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            self.error = exc
            self.admitted.set()

    def start(self) -> "Runner":
        self.thread.start()
        return self

    def finish(self) -> None:
        self.release.set()
        self.thread.join(5)


# -------------------------------------------------------------------- gate --

def test_a_single_turn_runs_straight_away():
    g = gate()
    with g.admit("u1"):
        assert g.stats()["running"] == 1
    assert g.stats() == {"running": 0, "queued": 0, "max_concurrent": 1, "max_per_user": 1}


def test_a_second_turn_waits_for_the_first_and_is_told_its_place():
    g = gate()
    first = Runner(g).start()
    assert first.admitted.wait(2)

    second = Runner(g, user="u2").start()
    time.sleep(0.05)
    assert not second.admitted.is_set()
    assert second.positions == [1]  # told where it stands, once

    first.finish()
    assert second.admitted.wait(2) and second.error is None
    second.finish()


def test_one_user_cannot_fill_the_machine_while_another_waits():
    g = gate(max_concurrent=3, max_per_user=1)
    mine = [Runner(g, user="u1").start() for _ in range(2)]
    assert mine[0].admitted.wait(2)
    time.sleep(0.05)
    assert not mine[1].admitted.is_set()  # my own second turn waits

    theirs = Runner(g, user="u2").start()
    assert theirs.admitted.wait(2)  # and doesn't hold up anyone else's
    assert theirs.error is None

    for r in (*mine, theirs):
        r.finish()


def test_two_turns_in_one_conversation_are_serialised():
    """They read and append the same history, so they can't overlap."""
    g = gate(max_concurrent=4, max_per_user=4)
    first = Runner(g, conv="c1").start()
    assert first.admitted.wait(2)
    same = Runner(g, conv="c1").start()
    other = Runner(g, conv="c2").start()

    assert other.admitted.wait(2)  # a different conversation is free to go
    time.sleep(0.05)
    assert not same.admitted.is_set()

    first.finish()
    assert same.admitted.wait(2)
    same.finish()
    other.finish()


def test_waiting_too_long_gives_up_rather_than_hanging():
    g = gate(max_wait=0.2)
    first = Runner(g).start()
    assert first.admitted.wait(2)

    with pytest.raises(Busy, match="still waiting"):
        with g.admit("u2"):
            pass

    first.finish()
    assert g.stats()["queued"] == 0  # the abandoned ticket is gone


def test_a_full_queue_is_refused_immediately():
    g = gate(max_queued=1, max_wait=5)
    first = Runner(g).start()
    assert first.admitted.wait(2)
    waiting = Runner(g, user="u2").start()
    time.sleep(0.05)

    with pytest.raises(Busy, match="already waiting"):
        with g.admit("u3"):
            pass

    first.finish()
    assert waiting.admitted.wait(2)
    waiting.finish()


def test_a_failed_turn_still_frees_its_slot():
    g = gate()
    with pytest.raises(ValueError):
        with g.admit("u1"):
            raise ValueError("the turn broke")
    assert g.stats()["running"] == 0
    with g.admit("u1"):  # the next one gets in
        pass


def test_stopping_a_conversation_asks_its_running_turn_to_stop():
    g = gate(max_concurrent=2, max_per_user=2)
    with g.admit("u1", "c1") as stop, g.admit("u1", "c2") as other:
        assert g.stop("c1") == 1
        assert stop.is_set()
        assert not other.is_set()  # another conversation's turn carries on
    assert g.stats()["running"] == 0  # the body still ends it, as usual


def test_stopping_takes_a_waiting_turn_out_of_line():
    g = gate()
    first = Runner(g, conv="c1").start()
    assert first.admitted.wait(2)
    waiting = Runner(g, conv="c1").start()
    while g.stats()["queued"] < 1:
        time.sleep(0.005)

    assert g.stop("c1") == 2  # the running one and the one behind it

    assert waiting.admitted.wait(2)
    assert isinstance(waiting.error, Stopped)
    assert g.stats()["queued"] == 0
    first.finish()


def test_stopping_a_conversation_with_nothing_running_is_a_no_op():
    g = gate()
    with g.admit("u1"):  # no conversation: a search, say
        assert g.stop("c1") == 0


def queue_up(g: Gate, *users: str) -> list[Runner]:
    """Starts a waiting Runner per user, in this order -- each one in line
    before the next arrives, so their arrival order is the one given."""
    runners = []
    for user in users:
        runners.append(Runner(g, user=user).start())
        deadline = time.monotonic() + 2
        while g.stats()["queued"] < len(runners) and time.monotonic() < deadline:
            time.sleep(0.005)
    return runners


def admitted_in_order(holder: Runner, waiting: list[Runner]) -> list[Runner]:
    """Lets each turn finish as soon as it starts, and returns the order they
    got in."""
    order, pending = [], list(waiting)
    holder.finish()
    while pending:
        started = next((r for r in pending if r.admitted.wait(0.01)), None)
        if started is None:
            continue
        order.append(started)
        pending.remove(started)
        started.finish()
    return order


def test_a_burst_from_one_user_interleaves_with_everyone_elses():
    """First come, first served would run all three of u1's before u2's."""
    g = gate(max_concurrent=1, max_per_user=1)
    holder = Runner(g, user="u0").start()
    assert holder.admitted.wait(2)
    a1, a2, a3, b1 = queue_up(g, "u1", "u1", "u1", "u2")

    assert admitted_in_order(holder, [a1, a2, a3, b1]) == [a1, b1, a2, a3]


def test_a_waiting_turns_position_is_its_fair_place_not_its_arrival():
    g = gate(max_concurrent=1, max_per_user=1)
    holder = Runner(g, user="u0").start()
    assert holder.admitted.wait(2)
    a1, a2, b1 = queue_up(g, "u1", "u1", "u2")
    time.sleep(0.05)

    assert b1.positions == [2]  # behind u1's first turn, ahead of its second
    assert a2.positions == [2, 3]  # and u1's second is told it slipped back

    admitted_in_order(holder, [a1, a2, b1])


def test_one_user_cannot_fill_the_queue():
    g = gate(max_queued=8, max_queued_per_user=2, max_wait=5)
    holder = Runner(g, user="u0").start()
    assert holder.admitted.wait(2)
    mine = queue_up(g, "u1", "u1")

    with pytest.raises(Busy, match="already have 2 requests waiting"):
        with g.admit("u1"):
            pass
    theirs = queue_up(g, "u2")[0]  # someone else still gets in line

    admitted_in_order(holder, [*mine, theirs])
    assert theirs.error is None


def test_the_gate_reads_its_limits_from_the_environment():
    cfg = GateConfig.from_env({"QUERY_MAX_CONCURRENT": "4", "QUERY_MAX_PER_USER": "2",
                               "QUERY_MAX_QUEUED": "10", "QUERY_MAX_QUEUED_PER_USER": "3",
                               "QUERY_QUEUE_WAIT": "30"})
    assert (cfg.max_concurrent, cfg.max_per_user, cfg.max_queued, cfg.max_queued_per_user, cfg.max_wait) == \
        (4, 2, 10, 3, 30.0)
    assert GateConfig.from_env({"QUERY_MAX_CONCURRENT": "0"}).max_concurrent == 1  # never zero


# ------------------------------------------------------------ ingest quota --

def limits(**overrides) -> UploadLimits:
    cfg = UploadLimits(max_file_mb=10, max_files=3, max_mb=20, window_hours=24)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_a_file_within_the_quota_is_accepted():
    assert limits().rejection(5_000_000, files_used=1, bytes_used=5_000_000) is None


def test_an_oversized_single_file_is_rejected_whatever_is_left():
    assert "limit is 10 MB per file" in limits().rejection(11_000_000, 0, 0)


def test_the_file_count_runs_out():
    assert "3 files per 24h" in limits().rejection(1_000, files_used=3, bytes_used=0)


def test_the_byte_allowance_runs_out_on_the_file_that_would_cross_it():
    cfg = limits()
    assert cfg.rejection(4_000_000, 1, 16_000_000) is None
    assert "20 MB per 24h" in cfg.rejection(5_000_000, 1, 16_000_000)


def test_what_is_left_is_reported_for_the_ui():
    left = limits().remaining(1, 5_000_000)
    assert (left["files_left"], left["mb_left"]) == (2, 15.0)
    over = limits().remaining(9, 99_000_000)
    assert (over["files_left"], over["mb_left"]) == (0, 0.0)  # never negative


def test_an_admin_is_not_metered():
    """Filling the library is an admin's job, so nothing stops them -- not the
    window, and not the per-file cap either."""
    admin = limits().for_user(is_admin=True)

    assert admin.rejection(10_000_000_000, files_used=999, bytes_used=10**12) is None
    assert admin.remaining(3, 5_000_000) == {"unlimited": True, "files_used": 3, "mb_used": 5.0}


def test_an_ordinary_user_is_still_metered():
    member = limits().for_user(is_admin=False)
    assert member.rejection(11_000_000, 0, 0) is not None
    assert member.remaining(0, 0)["unlimited"] is False


def test_the_quota_reads_its_limits_from_the_environment():
    cfg = UploadLimits.from_env({"INGEST_MAX_FILE_MB": "50", "INGEST_MAX_FILES_PER_WINDOW": "5",
                                 "INGEST_MAX_MB_PER_WINDOW": "100", "INGEST_QUOTA_WINDOW_HOURS": "1"})
    assert (cfg.max_file_mb, cfg.max_files, cfg.max_mb, cfg.window_seconds) == (50.0, 5, 100.0, 3600.0)
