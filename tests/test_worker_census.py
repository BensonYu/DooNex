"""Production Worker.run refuses incomplete safety censuses before dispatch."""

import time
from contextlib import contextmanager
from copy import deepcopy

import pytest
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.sandbox import Sandbox
from doonex.sdk import GuardedLLM
from doonex.worker import MAX_RECOVERY_RUNS, MAX_YIELDED_RUNS, Worker
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres


def seed(store, request_data, state, count):
    result = []
    for _ in range(count):
        run = store.create_run(deepcopy(request_data))
        with store.locked(run["id"]) as (_, current):
            current["state"] = state
        result.append(store.get(run["id"]))
    return result


@pytest.mark.parametrize("count", [0, 1, MAX_YIELDED_RUNS])
def test_census_reads_entire_supported_set(store, request_data, count):
    expected = seed(store, request_data, "yielded", count)
    rows = store.scheduling_runs(
        states={"yielded"},
        capacity=MAX_YIELDED_RUNS,
        deadline=time.monotonic() + 10,
    )
    assert {run["id"] for run in rows} == {run["id"] for run in expected}
    assert (
        sorted(rows, key=lambda run: (run["created_at"], run["id"]), reverse=True)
        == rows
    )


@pytest.mark.parametrize(
    "state,capacity",
    [
        ("yielded", MAX_YIELDED_RUNS),
        ("interrupted", MAX_RECOVERY_RUNS),
    ],
)
def test_actual_worker_refuses_cap_plus_one_without_side_effects(
    settings, store, request_data, monkeypatch, state, capacity
):
    before = seed(store, request_data, state, capacity + 1)
    front = store.create_run(request_data)
    dispatched = []

    def forbidden(*args, **kwargs):
        dispatched.append(True)
        pytest.fail("Incomplete safety census reached external execution")

    monkeypatch.setattr(Sandbox, "containers", forbidden)
    monkeypatch.setattr(Sandbox, "stop_run", forbidden)
    monkeypatch.setattr(GuardedLLM, "_transport_call", forbidden)
    with pytest.raises(ValueError, match="容量超限"):
        Worker(settings).run(once=True)
    assert not dispatched
    assert [store.get(run["id"]) for run in before] == before
    assert store.get(front["id"]) == front
    with store.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM actions").fetchone()["n"] == 0
        assert (
            c.execute("SELECT count(*) AS n FROM controller_operations").fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("field", ["id", "state"])
def test_census_rejects_column_payload_mismatch(store, request_data, field):
    run = seed(store, request_data, "yielded", 1)[0]
    run[field] = "cancelled" if field == "state" else "wrong-id"
    with store.connect() as c:
        c.execute("UPDATE runs SET data=%s", (Jsonb(run),))
    with pytest.raises(ValueError, match="绑定无效"):
        store.scheduling_runs(
            states={"yielded"},
            capacity=MAX_YIELDED_RUNS,
            deadline=time.monotonic() + 10,
        )


def test_expired_census_never_opens_database(store, monkeypatch):
    def forbidden():
        pytest.fail("Expired census opened a connection")

    monkeypatch.setattr(store, "connect", forbidden)
    with pytest.raises(ExecutionDeadlineExceeded):
        store.scheduling_runs(
            states={"yielded"},
            capacity=MAX_YIELDED_RUNS,
            deadline=time.monotonic() - 1,
        )


def test_reconcile_expiry_after_intent_commit_does_not_start_cleanup(
    settings, store, request_data, monkeypatch
):
    from doonex import worker as worker_module
    from doonex.budget import Ledger
    from doonex.cleanup import cleanup_intent_unresolved
    from doonex.deadline import check_deadline
    from doonex.store import execution_fenced, now

    run = seed(store, request_data, "running", 1)[0]
    with store.locked(run["id"]) as (_, current):
        current["first_started_at"] = now()
    Ledger(store, run["id"]).reserve(10, 10)
    before = store.get(run["id"])
    worker = Worker(settings)
    locked = worker.store.locked
    expired = False

    @contextmanager
    def commit_then_expire(*args, **kwargs):
        nonlocal expired
        with locked(*args, **kwargs) as pair:
            yield pair
        if cleanup_intent_unresolved(worker.store.get(run["id"])):
            expired = True

    def deadline(value):
        check_deadline(time.monotonic() - 1 if expired else value)

    def forbidden(*args, **kwargs):
        pytest.fail("Expired recovery dispatched external cleanup")

    monkeypatch.setattr(worker.store, "locked", commit_then_expire)
    monkeypatch.setattr(worker_module, "check_deadline", deadline)
    monkeypatch.setattr(Sandbox, "stop_run", forbidden)
    with pytest.raises(ExecutionDeadlineExceeded):
        worker.run(once=True)
    after = store.get(run["id"])
    assert after["state"] == "interrupted"
    assert cleanup_intent_unresolved(after)
    assert after["first_started_at"] == before["first_started_at"]
    assert after["usage"]["calls"] == before["usage"]["calls"]
    assert after["usage"]["reserved_tokens"] == before["usage"]["reserved_tokens"]
    assert store.pending(run["id"])[0]["status"] == "unknown"
    intent = deepcopy(after["cleanup_intent"])
    front = store.create_run(request_data)
    monkeypatch.setattr(worker_module, "check_deadline", check_deadline)
    monkeypatch.setattr(Sandbox, "containers", forbidden)
    monkeypatch.setattr(GuardedLLM, "_transport_call", forbidden)
    with pytest.raises(RuntimeError, match="终止状态不明"):
        Worker(settings).run(once=True)
    recovered = store.get(run["id"])
    assert recovered["cleanup_intent"] == intent
    assert recovered["usage"] == after["usage"]
    assert recovered["first_started_at"] == before["first_started_at"]
    assert "cleanup_intent_unresolved" in recovered["recovery"]["errors"]
    assert store.get(front["id"]) == front
    with store.connect() as c:
        assert execution_fenced(c)
        assert (
            c.execute("SELECT count(*) AS n FROM controller_operations").fetchone()["n"]
            == 0
        )
