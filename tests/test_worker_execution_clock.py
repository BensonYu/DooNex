"""Actual budget transactions share the Worker's process-lifetime clock."""

import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from doonex.budget import Halt, Ledger
from doonex.execution_clock import ExecutionClock
from doonex.store import now
from doonex.worker import Worker

pytestmark = pytest.mark.postgres


@pytest.fixture
def clocked(settings, store, request_data):
    worker = Worker(settings)
    stamp = datetime.now(UTC)
    mono = time.monotonic()
    offsets = {"wall": 0.0, "mono": 0.0}
    clock = ExecutionClock(
        wall=lambda: stamp + timedelta(seconds=offsets["wall"]),
        monotonic=lambda: mono + offsets["mono"],
    )
    worker.execution_clock = clock
    worker.scheduler.execution_clock = clock
    worker.scheduler.background.execution_clock = clock
    request = deepcopy(request_data)
    request["budget"]["max_seconds"] = 60
    run = store.create_run(request)
    with store.locked(run["id"]) as (_, current):
        current.update(
            state="running",
            owner=worker.owner,
            first_started_at=(stamp - timedelta(seconds=1)).isoformat(),
        )
    return worker, store.get(run["id"]), offsets


def observe(worker, run_id):
    with worker.store.locked(run_id, worker.owner, dispatch=True) as (c, run):
        return worker.scheduler.execution_bound(c, run)


def test_worker_shares_one_clock_with_scheduler_and_background(settings):
    worker = Worker(settings)
    assert worker.execution_clock is worker.scheduler.execution_clock
    assert worker.execution_clock is worker.scheduler.background.execution_clock


def test_reentry_and_role_metadata_cannot_extend_first_start(clocked):
    worker, original, offsets = clocked
    first = observe(worker, original["id"])
    offsets.update(wall=20.0, mono=2.0)
    shorter = observe(worker, original["id"])
    assert shorter.at < first.at
    with worker.store.locked(original["id"]) as (_, run):
        run["assignments"]["executor"]["session_generation"] += 1
        run["state"] = "yielded"
    offsets.update(wall=1.0, mono=3.0)
    resumed = observe(worker, original["id"])
    assert resumed.at == shorter.at
    after = worker.store.get(original["id"])
    assert after["first_started_at"] == original["first_started_at"]
    assert after["usage"] == original["usage"]


@pytest.mark.parametrize("mutation", ["first_started_at", "budget", "request"])
def test_changed_frozen_clock_binding_refuses_without_model_reservation(
    clocked, mutation
):
    worker, original, _ = clocked
    observe(worker, original["id"])
    with worker.store.locked(original["id"]) as (_, run):
        if mutation == "first_started_at":
            run[mutation] = now()
        elif mutation == "budget":
            run["request"]["budget"]["max_seconds"] += 10
        else:
            run["request"]["prompt"] += " altered"
    before = worker.store.get(original["id"])
    ledger = Ledger(
        worker.store,
        original["id"],
        worker.owner,
        dispatch_check=worker.scheduler.preflight,
    )
    with pytest.raises(Halt, match="binding_changed"):
        ledger.reserve(10, 10)
    assert worker.store.get(original["id"]) == before
    assert worker.store.pending(original["id"]) == []


@pytest.mark.parametrize("kind", ["model", "tool"])
def test_monotonic_expiry_blocks_real_admission_and_retains_unknown(clocked, kind):
    worker, original, offsets = clocked
    ledger = Ledger(
        worker.store,
        original["id"],
        worker.owner,
        dispatch_check=worker.scheduler.preflight,
    )
    ledger.reserve(10, 10)
    with worker.store.locked(original["id"]) as (c, run):
        worker.store.mark_pending_unknown(run["id"], c, run)
    before = worker.store.get(original["id"])
    pending = worker.store.pending(original["id"])
    offsets.update(mono=61.0, wall=0.0)
    with pytest.raises(Halt, match="expired"):
        if kind == "model":
            ledger.reserve(10, 10)
        else:
            ledger.tool_intent("true")
    assert worker.store.get(original["id"]) == before
    assert worker.store.pending(original["id"]) == pending
    assert before["usage"]["unknown_calls"] == 1
    assert before["usage"]["reserved_tokens"] == 20
