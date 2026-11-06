from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from doonex.budget import Halt, Ledger
from doonex.store import now


def running(store, data):
    run = store.create_run(data)
    with store.locked(run["id"]) as (_, current):
        current.update(state="running", first_started_at=now())
    return run["id"]


def test_reservations_are_atomic_and_call_limit_survives_resume(store, request_data):
    request_data["budget"]["max_calls"] = 1
    run_id = running(store, request_data)
    ledger = Ledger(store, run_id)

    def attempt():
        try:
            return ledger.reserve(100, 128)
        except Halt:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert len([result for result in results if result]) == 1
    reservation = next(result for result in results if result)
    ledger.settle(reservation, {"input_tokens": 50, "output_tokens": 10})
    store.transition(run_id, "paused", "test pause")
    store.transition(run_id, "running", "test resume")
    with pytest.raises(Halt, match="调用次数"):
        Ledger(store, run_id).reserve(100, 128)
    usage = store.get(run_id)["usage"]
    assert usage["calls"] == 1 and usage["tokens"] == 60
    assert usage["reserved_tokens"] == 0
    assert usage["cost_usd"] == pytest.approx(0.00007)


def test_unknown_response_keeps_reservation_and_cannot_double_settle(
    store, request_data
):
    run_id = running(store, request_data)
    ledger = Ledger(store, run_id)
    reservation = ledger.reserve(100, 128)
    ledger.settle(reservation, None)
    usage = store.get(run_id)["usage"]
    assert (
        usage["tokens"] == 0
        and usage["reserved_tokens"] == 228
        and usage["unknown_calls"] == 1
    )
    assert store.pending(run_id)[0]["status"] == "unknown"
    with pytest.raises(ValueError, match="已结算"):
        ledger.settle(reservation, {"input_tokens": 10, "output_tokens": 10})


@pytest.mark.parametrize("case", ["tokens", "cost", "unknown_price", "time", "pause"])
def test_no_dispatch_when_limit_or_control_blocks(store, request_data, case):
    if case == "tokens":
        request_data["budget"]["max_tokens"] = 1
    if case == "cost":
        request_data["budget"]["max_cost_usd"] = 0.000001
    if case == "unknown_price":
        request_data["model"]["input_price_per_million"] = None
    run_id = running(store, request_data)
    if case == "time":
        with store.locked(run_id) as (_, run):
            run["first_started_at"] = (
                datetime.now(UTC) - timedelta(hours=1)
            ).isoformat()
    if case == "pause":
        store.transition(run_id, "pausing", "pause requested")
    with pytest.raises(Halt):
        Ledger(store, run_id).reserve(100, 128)
    assert store.get(run_id)["usage"]["calls"] == 0
    assert not store.pending(run_id)


def test_pause_blocks_tool_dispatch_intent(store, request_data):
    run_id = running(store, request_data)
    store.transition(run_id, "pausing", "stop")
    with pytest.raises(Halt):
        Ledger(store, run_id).tool_intent("touch should-not-exist")
    assert not store.pending(run_id)


def test_subscription_tracks_tokens_but_never_claims_free_cost(store, request_data):
    request_data["model"].update(
        billing_mode="subscription",
        input_price_per_million=None,
        output_price_per_million=None,
    )
    request_data["budget"]["max_cost_usd"] = None
    run_id = running(store, request_data)
    ledger = Ledger(store, run_id)
    reserved = ledger.reserve(100, 128)
    ledger.settle(reserved, {"input_tokens": 80, "output_tokens": 15})
    usage = store.get(run_id)["usage"]
    assert usage["tokens"] == 95 and usage["calls"] == 1
    assert usage["cost_usd"] is None and usage["reserved_cost_usd"] is None


def test_stale_owner_cannot_dispatch_or_settle(store, request_data):
    from doonex.store import OwnershipLost

    run_id = running(store, request_data)
    with store.locked(run_id) as (_, run):
        run["owner"] = "old"
    old = Ledger(store, run_id, "old")
    reservation = old.reserve(100, 128)
    with store.locked(run_id) as (_, run):
        run["owner"] = "new"
    with pytest.raises(OwnershipLost):
        old.settle(reservation, {"input_tokens": 10, "output_tokens": 10})
    with pytest.raises(OwnershipLost):
        old.tool_intent("touch must-not-run")
    assert store.get(run_id)["usage"]["reserved_tokens"] == 228


def test_role_usage_shares_run_caps_and_stale_callbacks_cannot_switch_roles(
    store, request_data
):
    request_data["workflow"] = "execute_review"
    run_id = running(store, request_data)
    executor = Ledger(store, run_id)
    first = executor.reserve(100, 128)
    executor.settle(first, {"input_tokens": 50, "output_tokens": 10})
    with store.locked(run_id) as (_, run):
        run["assignments"]["executor"]["state"] = "completed"
        run["active_assignment"] = "reviewer"
        run["assignments"]["reviewer"]["state"] = "running"
        run["phase"] = "review"
    reviewer = Ledger(store, run_id)
    second = reviewer.reserve(100, 128)
    with pytest.raises(Halt, match="角色"):
        executor.reserve(100, 128)
    with pytest.raises(Halt, match="角色"):
        executor.tool_intent("must-not-run")
    with pytest.raises(ValueError, match="不属于"):
        executor.settle(second, None)
    reviewer.settle(second, None)
    store.transition(run_id, "paused", "pause reviewer")
    store.transition(run_id, "running", "resume reviewer")
    with pytest.raises(Halt, match="调用次数"):
        reviewer.reserve(100, 128)
    run = store.get(run_id)
    assert run["usage"]["calls"] == 2
    assert run["usage"]["reserved_tokens"] == 228
    assert run["usage"]["unknown_calls"] == 1
    assert run["assignments"]["executor"]["usage"]["tokens"] == 60
    assert run["assignments"]["reviewer"]["usage"]["unknown_calls"] == 1
    for key in ("calls", "tokens", "reserved_tokens", "unknown_calls"):
        assert run["usage"][key] == sum(
            assignment["usage"][key] for assignment in run["assignments"].values()
        )
    for event in store.events(run_id)["events"]:
        if event["kind"].startswith("model_"):
            payload = event["payload"]
            assert payload["assignment_id"] == run["assignments"][payload["role"]]["id"]
    action = store.pending(run_id)[0]
    assert action["data"]["assignment_id"] == run["assignments"]["reviewer"]["id"]


def test_settlement_uses_persisted_reservation_and_rejects_other_run(
    store, request_data
):
    first_id, second_id = running(store, request_data), running(store, request_data)
    ledger = Ledger(store, first_id)
    reservation = ledger.reserve(100, 128)
    with pytest.raises(ValueError, match="不属于"):
        Ledger(store, second_id).settle(
            reservation, {"input_tokens": 10, "output_tokens": 5}
        )
    ledger.settle(
        {**reservation, "tokens": 1, "cost": 999},
        {"input_tokens": 10, "output_tokens": 5},
    )
    usage = store.get(first_id)["usage"]
    assert usage["reserved_tokens"] == 0
    assert usage["reserved_cost_usd"] == 0
    assert usage["tokens"] == 15
    assert store.get(second_id)["usage"]["calls"] == 0


def test_review_uses_original_run_clock_and_subscription_unknown_cost(
    store, request_data
):
    request_data["workflow"] = "execute_review"
    request_data["model"].update(
        billing_mode="subscription",
        input_price_per_million=None,
        output_price_per_million=None,
    )
    request_data["budget"]["max_cost_usd"] = None
    run_id = running(store, request_data)
    with store.locked(run_id) as (_, run):
        run["active_assignment"] = "reviewer"
        run["phase"] = "review"
    ledger = Ledger(store, run_id)
    reservation = ledger.reserve(100, 128)
    ledger.settle(reservation, {"input_tokens": 10, "output_tokens": 5})
    with store.locked(run_id) as (_, run):
        run["first_started_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with pytest.raises(Halt, match="时长"):
        ledger.reserve(100, 128)
    run = store.get(run_id)
    assert run["assignments"]["reviewer"]["usage"]["calls"] == 1
    assert run["assignments"]["reviewer"]["usage"]["cost_usd"] is None
