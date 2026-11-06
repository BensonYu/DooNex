"""Atomic cross-Run accounting against disposable PostgreSQL, no model calls."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from doonex.budget import BatchHalt, Ledger, mark_batch_unknown
from doonex.store import OwnershipLost, now
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres


def create_batch(store, request_data, **changes):
    subscription = request_data["model"].get("billing_mode") == "subscription"
    data = {
        "id": str(uuid4()),
        "state": "running",
        "first_started_at": now(),
        "batch_budget": {
            "max_calls": 10,
            "max_tokens": 100000,
            "max_cost_usd": None if subscription else 5,
            "max_seconds": 7200,
        },
        "usage": {
            "calls": 0,
            "tokens": 0,
            "reserved_tokens": 0,
            "unknown_calls": 0,
            "cost_usd": None if subscription else 0,
            "reserved_cost_usd": None if subscription else 0,
            "metered_cost_usd": 0,
            "reserved_metered_cost_usd": 0,
        },
        **changes,
    }
    with store.connect() as connection:
        # Root owns the production migration. This also runs before integration.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS arena_evaluations "
            "(id uuid PRIMARY KEY,state text NOT NULL,data jsonb NOT NULL)"
        )
        connection.execute(
            "INSERT INTO arena_evaluations VALUES(%s,%s,%s)",
            (data["id"], data["state"], Jsonb(data)),
        )
    return data["id"]


def batch(store, evaluation_id):
    with store.connect() as connection:
        return connection.execute(
            "SELECT data FROM arena_evaluations WHERE id=%s", (evaluation_id,)
        ).fetchone()["data"]


def update_batch(store, evaluation_id, **changes):
    with store.connect() as connection:
        data = connection.execute(
            "SELECT data FROM arena_evaluations WHERE id=%s FOR UPDATE",
            (evaluation_id,),
        ).fetchone()["data"]
        data.update(changes)
        connection.execute(
            "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
            (data["state"], Jsonb(data), evaluation_id),
        )


def child(store, request_data, evaluation_id, owner="worker"):
    request = deepcopy(request_data)
    request["arena"] = {"evaluation_id": evaluation_id}
    run_id = store.create_run(request)["id"]
    with store.locked(run_id) as (_, run):
        run.update(state="running", first_started_at=now(), owner=owner)
    return run_id, Ledger(store, run_id, owner)


def usages(store, evaluation_id, run_id):
    run = store.get(run_id)
    return [
        run["usage"],
        run["assignments"]["executor"]["usage"],
        batch(store, evaluation_id)["usage"],
    ]


def test_two_children_atomically_compete_for_last_batch_call(store, request_data):
    evaluation_id = create_batch(store, request_data)
    limits = batch(store, evaluation_id)["batch_budget"]
    limits["max_calls"] = 1
    update_batch(store, evaluation_id, batch_budget=limits)
    children = [child(store, request_data, evaluation_id) for _ in range(2)]
    barrier = Barrier(2)

    def reserve(item):
        run_id, ledger = item
        barrier.wait(timeout=5)
        try:
            return run_id, ledger.reserve(100, 128)
        except BatchHalt as error:
            assert error.evaluation_id == evaluation_id
            return run_id, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, children))
    assert sum(ticket is not None for _, ticket in results) == 1
    assert batch(store, evaluation_id)["usage"]["calls"] == 1
    assert batch(store, evaluation_id)["usage"]["reserved_tokens"] == 228
    assert sum(store.get(run_id)["usage"]["calls"] for run_id, _ in children) == 1
    with store.connect() as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM actions").fetchone()["n"] == 1
        )
    winner, ticket = next((run_id, ticket) for run_id, ticket in results if ticket)
    assert ticket["evaluation_id"] == evaluation_id
    for usage in usages(store, evaluation_id, winner):
        assert usage["calls"] == 1 and usage["reserved_tokens"] == 228


def test_settlement_is_atomic_authoritative_and_cannot_repeat(store, request_data):
    evaluation_id = create_batch(store, request_data)
    run_id, ledger = child(store, request_data, evaluation_id)
    reservation = ledger.reserve(100, 128)
    forged = {**reservation, "tokens": 1, "cost": 999, "evaluation_id": str(uuid4())}

    def settle(_):
        try:
            ledger.settle(forged, {"input_tokens": 50, "output_tokens": 10})
            return True
        except ValueError as error:
            assert "已结算" in str(error)
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(settle, range(2))) == 1
    for usage in usages(store, evaluation_id, run_id):
        assert usage["calls"] == 1 and usage["tokens"] == 60
        assert usage["reserved_tokens"] == 0
        assert usage["metered_cost_usd"] == pytest.approx(0.00007)
        assert usage["reserved_metered_cost_usd"] == 0
        assert usage["cost_usd"] == pytest.approx(0.00007)


@pytest.mark.parametrize(
    "actual", [None, {}, "unknown", {"input_tokens": True, "output_tokens": 0}]
)
def test_unknown_outcome_keeps_all_reservations_and_survives_resume(
    store, request_data, actual
):
    evaluation_id = create_batch(store, request_data)
    limits = batch(store, evaluation_id)["batch_budget"]
    limits["max_tokens"] = 400
    update_batch(store, evaluation_id, batch_budget=limits)
    run_id, ledger = child(store, request_data, evaluation_id)
    reservation = ledger.reserve(100, 128)
    ledger.settle(reservation, actual)
    before = batch(store, evaluation_id)
    for usage in usages(store, evaluation_id, run_id):
        assert usage["unknown_calls"] == 1
        assert usage["reserved_tokens"] == 228 and usage["tokens"] == 0
        assert usage["reserved_metered_cost_usd"] == pytest.approx(0.000356)
    with pytest.raises(ValueError, match="已结算"):
        ledger.settle(reservation, {"input_tokens": 10, "output_tokens": 5})
    update_batch(store, evaluation_id, state="interrupted")
    store.transition(run_id, "interrupted", "test interruption")
    update_batch(store, evaluation_id, state="running")
    store.transition(run_id, "running", "test resume")
    assert batch(store, evaluation_id)["first_started_at"] == before["first_started_at"]
    with pytest.raises(BatchHalt, match="token"):
        Ledger(store, run_id, "worker").reserve(100, 128)
    assert batch(store, evaluation_id)["usage"] == before["usage"]


def test_recovery_helper_marks_only_new_pending_models_without_releasing_reservations(
    store, request_data
):
    evaluation_id = create_batch(store, request_data)
    run_id, ledger = child(store, request_data, evaluation_id)
    ledger.reserve(100, 128)
    ledger.tool_intent("container command with unknown completion")

    def recover():
        # Store owns these Run/role updates; the helper owns only the batch.
        with store.locked(run_id, "worker") as (connection, run):
            count = connection.execute(
                "SELECT count(*) AS n FROM actions WHERE run_id=%s "
                "AND kind='model' AND status='pending'",
                (run_id,),
            ).fetchone()["n"]
            run["usage"]["unknown_calls"] += count
            run["assignments"]["executor"]["usage"]["unknown_calls"] += count
            mark_batch_unknown(connection, run, count)
            connection.execute(
                "UPDATE actions SET status='unknown' WHERE run_id=%s AND status='pending'",
                (run_id,),
            )
            return count

    assert recover() == 1
    original = batch(store, evaluation_id)
    assert recover() == 0
    assert batch(store, evaluation_id) == original
    for usage in usages(store, evaluation_id, run_id):
        assert usage["unknown_calls"] == 1 and usage["reserved_tokens"] == 228


@pytest.mark.parametrize("subscription", [False, True])
def test_metered_and_subscription_accounting_remain_distinct(
    store, request_data, subscription
):
    if subscription:
        request_data["model"].update(
            billing_mode="subscription",
            input_price_per_million=None,
            output_price_per_million=None,
        )
        request_data["budget"]["max_cost_usd"] = None
    evaluation_id = create_batch(store, request_data)
    run_id, ledger = child(store, request_data, evaluation_id)
    ticket = ledger.reserve(100, 128)
    ledger.settle(ticket, {"input_tokens": 80, "output_tokens": 15})
    for usage in usages(store, evaluation_id, run_id):
        assert usage["tokens"] == 95 and usage["reserved_tokens"] == 0
        assert usage["metered_cost_usd"] == pytest.approx(
            0 if subscription else 0.00011
        )
        assert usage["reserved_metered_cost_usd"] == 0
        if subscription:
            assert usage["cost_usd"] is usage["reserved_cost_usd"] is None
        else:
            assert usage["cost_usd"] == pytest.approx(0.00011)


@pytest.mark.parametrize("limit", ["max_calls", "max_tokens", "max_cost_usd"])
def test_batch_caps_are_independent_of_larger_child_caps(store, request_data, limit):
    evaluation_id = create_batch(store, request_data)
    data = batch(store, evaluation_id)
    if limit == "max_calls":
        data["usage"]["calls"] = data["batch_budget"][limit]
    else:
        data["batch_budget"][limit] = 200 if limit == "max_tokens" else 0.00001
    update_batch(
        store, evaluation_id, batch_budget=data["batch_budget"], usage=data["usage"]
    )
    run_id, ledger = child(store, request_data, evaluation_id)
    with pytest.raises(BatchHalt):
        ledger.reserve(100, 128)
    assert store.get(run_id)["usage"]["calls"] == 0
    assert not store.pending(run_id)
    assert batch(store, evaluation_id)["usage"] == data["usage"]


def test_unknown_total_cost_does_not_bypass_metered_subtotal_limit(store, request_data):
    evaluation_id = create_batch(store, request_data)
    data = batch(store, evaluation_id)
    data["usage"].update(cost_usd=None, reserved_cost_usd=None, metered_cost_usd=1)
    data["batch_budget"]["max_cost_usd"] = 1.0001
    update_batch(
        store, evaluation_id, usage=data["usage"], batch_budget=data["batch_budget"]
    )
    _, ledger = child(store, request_data, evaluation_id)
    with pytest.raises(BatchHalt, match="费用"):
        ledger.reserve(100, 128)


@pytest.mark.parametrize(
    "state",
    ["queued", "interrupted", "cancelling", "cancelled", "complete", "budget_exceeded"],
)
def test_nonrunning_batch_blocks_models_and_tools_but_allows_late_settlement(
    store, request_data, state
):
    evaluation_id = create_batch(store, request_data)
    run_id, ledger = child(store, request_data, evaluation_id)
    reservation = ledger.reserve(100, 128)
    update_batch(store, evaluation_id, state=state)
    with pytest.raises(BatchHalt, match="停止派发"):
        ledger.reserve(100, 128)
    with pytest.raises(BatchHalt, match="停止派发"):
        ledger.tool_intent("must not dispatch")
    ledger.settle(reservation, {"input_tokens": 20, "output_tokens": 5})
    assert batch(store, evaluation_id)["state"] == state
    for usage in usages(store, evaluation_id, run_id):
        assert usage["tokens"] == 25 and usage["reserved_tokens"] == 0


@pytest.mark.parametrize(
    "clock", [None, "2026-11-06T00:00:00", "invalid", "expired", "future"]
)
def test_batch_clock_cannot_be_missing_reset_or_bypassed_for_tools(
    store, request_data, clock
):
    if clock in {"expired", "future"}:
        clock = (
            datetime.now(UTC) + timedelta(hours=4 if clock == "future" else -4)
        ).isoformat()
    evaluation_id = create_batch(store, request_data, first_started_at=clock)
    run_id, ledger = child(store, request_data, evaluation_id)
    with pytest.raises(BatchHalt):
        ledger.reserve(100, 128)
    with pytest.raises(BatchHalt):
        ledger.tool_intent("must not dispatch")
    assert store.get(run_id)["usage"]["calls"] == 0
    assert batch(store, evaluation_id)["first_started_at"] == clock


def test_last_authorized_model_response_can_still_execute_tool(store, request_data):
    evaluation_id = create_batch(store, request_data)
    limits = batch(store, evaluation_id)["batch_budget"]
    limits["max_calls"] = 1
    update_batch(store, evaluation_id, batch_budget=limits)
    _, ledger = child(store, request_data, evaluation_id)
    ledger.reserve(100, 128)
    assert ledger.tool_intent("authorized last response command")
    with pytest.raises(BatchHalt, match="调用次数"):
        ledger.reserve(100, 128)


def test_stale_owner_cannot_charge_settle_or_dispatch_batch_actions(
    store, request_data
):
    evaluation_id = create_batch(store, request_data)
    run_id, old = child(store, request_data, evaluation_id, owner="old")
    reservation = old.reserve(100, 128)
    before = batch(store, evaluation_id)
    with store.locked(run_id) as (_, run):
        run["owner"] = "new"
    for operation in (
        lambda: old.reserve(100, 128),
        lambda: old.settle(reservation, {"input_tokens": 10, "output_tokens": 5}),
        lambda: old.tool_intent("must not dispatch"),
    ):
        with pytest.raises(OwnershipLost):
            operation()
    assert batch(store, evaluation_id) == before


@pytest.mark.parametrize("operation", ["reserve", "settle", "tool"])
def test_exception_rolls_back_all_three_ledgers_action_and_event(
    store, request_data, monkeypatch, operation
):
    evaluation_id = create_batch(store, request_data)
    run_id, ledger = child(store, request_data, evaluation_id)
    reservation = ledger.reserve(100, 128) if operation == "settle" else None
    before_run, before_batch = store.get(run_id), batch(store, evaluation_id)
    before_actions, before_events = store.pending(run_id), store.events(run_id)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated event persistence failure")

    monkeypatch.setattr(store, "event", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        if operation == "reserve":
            ledger.reserve(100, 128)
        elif operation == "settle":
            ledger.settle(reservation, {"input_tokens": 50, "output_tokens": 10})
        else:
            ledger.tool_intent("must roll back")
    assert store.get(run_id) == before_run
    assert batch(store, evaluation_id) == before_batch
    assert store.pending(run_id) == before_actions
    assert store.events(run_id) == before_events


def test_recovery_helper_participates_in_callers_transaction(store, request_data):
    evaluation_id = create_batch(store, request_data)
    run_id, ledger = child(store, request_data, evaluation_id)
    ledger.reserve(100, 128)
    before = batch(store, evaluation_id)
    with (
        pytest.raises(RuntimeError, match="rollback"),
        store.locked(run_id) as (connection, run),
    ):
        mark_batch_unknown(connection, run, 1)
        raise RuntimeError("rollback")
    assert batch(store, evaluation_id) == before


def test_batch_reference_cannot_change_between_reserve_and_settlement(
    store, request_data
):
    first, second = create_batch(store, request_data), create_batch(store, request_data)
    run_id, ledger = child(store, request_data, first)
    reservation = ledger.reserve(100, 128)
    before = batch(store, first)
    with store.locked(run_id) as (_, run):
        run["request"]["arena"]["evaluation_id"] = second
    with pytest.raises(ValueError, match="竞技批次"):
        ledger.settle(reservation, {"input_tokens": 10, "output_tokens": 5})
    assert batch(store, first) == before
    assert batch(store, second)["usage"]["calls"] == 0


def test_missing_or_malformed_batch_never_dispatches(store, request_data):
    create_batch(store, request_data)
    for evaluation_id in (str(uuid4()), "not-a-uuid"):
        run_id, ledger = child(store, request_data, evaluation_id)
        with pytest.raises(BatchHalt):
            ledger.reserve(100, 128)
        with pytest.raises(BatchHalt):
            ledger.tool_intent("must not dispatch")
        assert not store.pending(run_id)
