"""Pure clock/caller contracts with scripted rows; no PostgreSQL lock claims."""

from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from doonex.background import (
    Background,
    BackgroundConflict,
    BackgroundInput,
    BackgroundStopped,
)
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.execution_clock import ClockExpired
from test_execution_clock import START, ManualTime

GRANT = str(UUID(int=20))
BATCH = str(UUID(int=21))
ALLOCATION = str(UUID(int=22))


class Rows:
    def __init__(self, values):
        self.values = deepcopy(values)

    def fetchone(self):
        return self.values[0] if self.values else None

    def fetchall(self):
        return self.values


class ScriptedConnection:
    """Only emulate the rows touched below, never transaction/lock behavior."""

    def __init__(self, value, ledger, allocation, batch):
        self.grant, self.ledger, self.allocation, self.batch = map(
            deepcopy, (value, ledger, allocation, batch)
        )
        self.calls = []
        self.hook = lambda *_: None
        self.unresolved = 0
        self.child_state = "failed"

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self.hook(sql, params)
        if sql.startswith("SELECT"):
            if "FROM background_authorizations" in sql:
                return Rows([self.grant])
            if "FROM background_days" in sql:
                return Rows([self.ledger])
            if "FROM background_allocations" in sql:
                if "WHERE day=" in sql:
                    return Rows([self.allocation])
                return Rows([self.allocation] if self.allocation else [])
            if "FROM arena_evaluations" in sql:
                return Rows([{"data": self.batch}])
            if " AS binding FROM runs" in sql:
                return Rows([{"binding": self.batch["background"]}])
            if "SELECT count(*)" in sql:
                return Rows([{"count": self.unresolved}])
            if " AS usage FROM runs" in sql:
                return Rows([{"state": self.child_state, "usage": self.batch["usage"]}])
        elif sql.startswith("UPDATE background_authorizations"):
            self.grant.update(state=params[0], data=deepcopy(params[1].obj))
            return Rows([])
        elif sql.startswith("UPDATE background_days"):
            self.ledger["data"] = deepcopy(params[0].obj)
            return Rows([])
        elif sql.startswith("UPDATE arena_evaluations"):
            self.batch = deepcopy(params[0].obj)
            return Rows([])
        elif sql.startswith("UPDATE background_allocations SET evaluation_id="):
            self.allocation.update(
                evaluation_id=params[0], data=deepcopy(params[1].obj)
            )
            return Rows([])
        elif sql.startswith("UPDATE background_allocations SET state='settled'"):
            self.allocation.update(state="settled", data=deepcopy(params[0].obj))
            return Rows([])
        raise AssertionError(f"Unscripted SQL: {sql}")

    def writes(self):
        return [sql for sql, _ in self.calls if not sql.startswith("SELECT")]


@pytest.fixture
def sample(tmp_path, monkeypatch):
    timer = ManualTime()
    monkeypatch.setattr("doonex.deadline.time.monotonic", timer.monotonic)
    clock = timer.clock()
    service = Background(
        SimpleNamespace(home=tmp_path), None, clock=timer.wall, execution_clock=clock
    )
    body = BackgroundInput.model_validate(
        {
            "taskset_id": "a" * 64,
            "skill_version_ids": ["b" * 64, "c" * 64],
            "model_profile_id": "legacy",
            "window_start": START.isoformat(),
            "window_end": (START + timedelta(hours=1)).isoformat(),
            "batch_budget": {
                "max_calls": 10,
                "max_tokens": 100000,
                "max_seconds": 60,
                "max_cost_usd": None,
            },
            "daily_budget": {
                "max_calls": 30,
                "max_tokens": 300000,
                "max_seconds": 600,
                "max_cost_usd": None,
            },
        }
    )
    snapshot = {
        "model": {"billing_mode": "subscription"},
        "model_profile": {},
        "environment": {},
        "taskset_report_sha256": "d" * 64,
        "skills": {key: {"review_revision": 1} for key in body.skill_version_ids},
    }
    value = {
        "id": GRANT,
        "state": "consumed",
        "date": START.date().isoformat(),
        "evaluation_id": BATCH,
        "window_start": body.window_start.isoformat(),
        "window_end": body.window_end.isoformat(),
        "expires_at": body.window_end.isoformat(),
        "input": body.model_dump(mode="json"),
        "snapshot": snapshot,
        "history": [],
        "revision": "initial",
    }
    reserved = service._reservation(body)
    allocation = {
        "id": ALLOCATION,
        "authorization_id": GRANT,
        "evaluation_id": BATCH,
        "date": value["date"],
        "claimed_at": START.isoformat(),
        "reserved": reserved,
        "actual": None,
        "settled_at": None,
    }
    usage = {
        "calls": 1,
        "tokens": 5,
        "reserved_tokens": 0,
        "unknown_calls": 0,
        "cost_usd": None,
        "reserved_cost_usd": None,
        "metered_cost_usd": 0.0,
        "reserved_metered_cost_usd": 0.0,
    }
    binding = {
        "authorization_id": GRANT,
        "reservation_id": ALLOCATION,
        "date": value["date"],
    }
    batch = {
        "id": BATCH,
        "state": "complete",
        "background": binding,
        "usage": usage,
        "split": "tuning",
        **body.model_dump(
            include={
                "taskset_id",
                "skill_version_ids",
                "repeats",
                "run_budget",
                "batch_budget",
            }
        ),
        "model": snapshot["model"],
        "model_profile": {},
        "environment": {},
        "taskset_report_sha256": snapshot["taskset_report_sha256"],
        "skills": {key: {} for key in body.skill_version_ids},
    }
    ledger = {
        "date": value["date"],
        "budget": body.daily_budget.model_dump(),
        "used": {
            "calls": 0,
            "tokens": 0,
            "seconds": 0,
            "cost_usd": 0,
            "metered_cost_usd": 0,
        },
        "reserved": deepcopy(reserved),
    }
    connection = ScriptedConnection(
        {"id": GRANT, "state": "consumed", "data": value},
        {"day": START.date(), "data": ledger},
        {
            "id": ALLOCATION,
            "authorization_id": GRANT,
            "evaluation_id": BATCH,
            "day": START.date(),
            "state": "reserved",
            "data": allocation,
        },
        batch,
    )
    return SimpleNamespace(
        timer=timer,
        clock=clock,
        service=service,
        connection=connection,
        body=body,
        value=value,
        allocation=allocation,
    )


def test_legacy_optional_clock_does_not_create_an_epoch_or_query_rows(sample):
    sample.service.execution_clock = None
    sample.timer.now = START.replace(year=2031)
    assert sample.service.deadline(sample.connection, GRANT, BATCH) is None
    assert sample.connection.calls == []


def test_expired_outer_deadline_refuses_before_the_first_query(sample):
    with pytest.raises(ExecutionDeadlineExceeded):
        sample.service.deadline(sample.connection, GRANT, BATCH, deadline=100)
    assert sample.connection.calls == []


def test_bound_is_checked_and_reused_across_helper_recreation_and_revision(sample):
    bound = sample.service.deadline(sample.connection, GRANT, BATCH)
    assert bound.at == 160 and bound.scope.kind == "allocation"
    sample.timer.advance(20, wall_seconds=10)
    sample.connection.grant["data"].update(revision="changed", updated_at="later")
    other = Background(
        sample.service.settings,
        None,
        clock=sample.timer.wall,
        execution_clock=sample.clock,
    )
    assert other.deadline(sample.connection, GRANT, BATCH) == bound
    assert not sample.connection.writes()
    assert all(
        "FROM runs" not in sql and "arena_evaluations" not in sql
        for sql, _ in sample.connection.calls
    )


def test_expired_cached_allocation_cannot_be_renewed_by_wall_rollback(sample):
    sample.service.deadline(sample.connection, GRANT, BATCH)
    original = deepcopy(sample.connection.allocation)
    sample.timer.advance(70, wall_seconds=30)
    with pytest.raises(BackgroundStopped) as error:
        sample.service.deadline(sample.connection, GRANT, BATCH)
    assert error.value.scope.kind == "allocation"
    assert isinstance(error.value.__cause__, ClockExpired)
    assert sample.connection.allocation == original
    assert not sample.connection.writes()


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("grant_data", "id", str(UUID(int=100))),
        ("grant", "state", "disabled"),
        ("allocation_data", "id", str(UUID(int=100))),
        ("allocation", "authorization_id", str(UUID(int=100))),
        ("allocation", "evaluation_id", str(UUID(int=100))),
        ("allocation_data", "date", "2026-11-15"),
        ("allocation", "day", date(2026, 11, 15)),
        ("ledger_data", "date", "2026-11-15"),
    ],
)
def test_column_payload_or_original_scope_mismatch_refuses_without_writes(
    sample, target, key, value
):
    tables = {
        "grant": sample.connection.grant,
        "grant_data": sample.connection.grant["data"],
        "allocation": sample.connection.allocation,
        "allocation_data": sample.connection.allocation["data"],
        "ledger_data": sample.connection.ledger["data"],
    }
    tables[target][key] = value
    with pytest.raises((BackgroundStopped, BackgroundConflict)):
        sample.service.deadline(sample.connection, GRANT, BATCH)
    assert not sample.connection.writes()


def test_changed_original_reservation_and_future_claim_refuse(sample):
    sample.connection.allocation["data"]["reserved"]["seconds"] += 1
    with pytest.raises(BackgroundConflict):
        sample.service.deadline(sample.connection, GRANT, BATCH)
    sample.connection.allocation["data"]["reserved"]["seconds"] -= 1
    sample.connection.allocation["data"]["claimed_at"] = (
        START + timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(BackgroundStopped) as error:
        sample.service.deadline(sample.connection, GRANT, BATCH)
    assert error.value.reason == "future_start"
    assert not sample.connection.writes()


def test_grant_input_and_snapshot_are_immutable_but_status_is_not_a_timer_key(sample):
    sample.service.deadline(sample.connection, GRANT, BATCH)
    sample.connection.grant["data"]["snapshot"]["environment"]["changed"] = True
    with pytest.raises(BackgroundStopped) as error:
        sample.service.deadline(sample.connection, GRANT, BATCH)
    assert (
        error.value.scope.kind == "window" and error.value.reason == "binding_changed"
    )


def test_preflight_retains_one_metadata_check_and_attributes_nested_expiry(
    sample, monkeypatch
):
    observed = []

    def unchanged(value, connection, *, inspect_contents, deadline):
        observed.append((inspect_contents, deadline))
        sample.timer.advance(61, wall_seconds=1)
        raise ExecutionDeadlineExceeded("synthetic metadata checkpoint")

    monkeypatch.setattr(sample.service, "_unchanged", unchanged)
    with pytest.raises(BackgroundStopped) as error:
        sample.service.preflight(sample.connection, GRANT, BATCH, deadline=500)
    assert observed == [(False, 160)]
    assert error.value.scope.kind == "allocation"
    assert isinstance(error.value.__cause__, ClockExpired)
    assert isinstance(error.value.__cause__.__cause__, ExecutionDeadlineExceeded)
    assert not sample.connection.writes()


def test_outer_preparation_timeout_is_not_relabelled_as_business_expiry(
    sample, monkeypatch
):
    def unchanged(*args, **kwargs):
        sample.timer.advance(11, wall_seconds=1)
        raise ExecutionDeadlineExceeded("outer preparation")

    monkeypatch.setattr(sample.service, "_unchanged", unchanged)
    with pytest.raises(ExecutionDeadlineExceeded, match="outer preparation"):
        sample.service.preflight(sample.connection, GRANT, BATCH, deadline=110)
    assert not sample.connection.writes()


def test_claim_checks_original_window_before_metadata_or_new_allocation(
    sample, monkeypatch
):
    sample.connection.grant.update(state="enabled")
    sample.connection.grant["data"].update(state="enabled", evaluation_id=None)
    inspected = []

    def unchanged(*args, **kwargs):
        inspected.append(kwargs["deadline"])
        sample.timer.advance(3601, wall_seconds=1)
        raise ExecutionDeadlineExceeded("claim metadata")

    monkeypatch.setattr(sample.service, "_unchanged", unchanged)
    with pytest.raises(BackgroundStopped) as error:
        sample.service.claim(sample.connection, GRANT, deadline=5000)
    assert inspected == [3700]
    assert error.value.scope.kind == "window"
    assert not sample.connection.writes()


def test_attach_first_clock_binding_uses_actual_parent_and_original_claim(sample):
    sample.connection.grant["data"]["evaluation_id"] = None
    sample.connection.allocation["data"]["evaluation_id"] = None
    sample.connection.allocation["evaluation_id"] = None
    claim = {"authorization_id": GRANT, "reservation_id": ALLOCATION}
    sample.timer.advance(20)
    sample.service.attach(sample.connection, claim, BATCH, deadline=200)
    result = sample.service.deadline(sample.connection, GRANT, BATCH)
    assert result.at == 160
    assert sample.connection.allocation["data"]["claimed_at"] == START.isoformat()
    assert sample.connection.allocation["data"]["evaluation_id"] == BATCH


def test_settlement_uses_observed_elapsed_after_rollback_without_reservation_cap(
    sample,
):
    sample.service.deadline(sample.connection, GRANT, BATCH)
    sample.timer.advance(50)
    sample.service.deadline(sample.connection, GRANT, BATCH)
    sample.timer.advance(30, wall_seconds=-45)
    result = sample.service.settle(sample.connection, BATCH, sample.connection.batch)
    assert result["actual"]["seconds"] == 80
    assert result["reserved"]["seconds"] == 60
    assert result["actual"]["cost_usd"] is None
    assert sample.connection.ledger["data"]["used"]["seconds"] == 80
    assert sample.connection.ledger["data"]["reserved"]["seconds"] == 0


@pytest.mark.parametrize(
    "kind", ["unknown_call", "unknown_action", "child_interrupted", "batch_running"]
)
def test_unknown_or_inflight_outcome_keeps_original_full_reservation(sample, kind):
    sample.service.deadline(sample.connection, GRANT, BATCH)
    if kind == "unknown_call":
        sample.connection.batch["usage"]["unknown_calls"] = 1
    elif kind == "unknown_action":
        sample.connection.unresolved = 1
    elif kind == "child_interrupted":
        sample.connection.child_state = "interrupted"
    else:
        sample.connection.batch["state"] = "running"
    original = deepcopy(sample.connection.allocation)
    sample.timer.advance(80, wall_seconds=-10)
    result = sample.service.settle(sample.connection, BATCH, sample.connection.batch)
    assert result == original["data"]
    assert sample.connection.allocation == original
    assert not sample.connection.writes()


@pytest.mark.parametrize(
    "stage",
    ["entry", "grant", "day_lock", "allocation_lock", "allocation_write", "day_write"],
)
def test_settlement_checks_the_same_preparation_deadline_after_each_wait(sample, stage):
    def hook(sql, params):
        matched = {
            "grant": "FROM background_authorizations" in sql,
            "day_lock": "FROM background_days" in sql and "FOR UPDATE" in sql,
            "allocation_lock": "FROM background_allocations" in sql
            and "FOR UPDATE" in sql,
            "allocation_write": sql.startswith("UPDATE background_allocations"),
            "day_write": sql.startswith("UPDATE background_days"),
        }.get(stage, False)
        if matched:
            sample.timer.advance(2)

    sample.connection.hook = hook
    if stage == "entry":
        sample.timer.advance(2)
    with pytest.raises(ExecutionDeadlineExceeded):
        sample.service.settle(
            sample.connection, BATCH, sample.connection.batch, deadline=101
        )
    if stage in {"entry", "grant", "day_lock", "allocation_lock"}:
        assert not sample.connection.writes()
    # Scripted rows do not claim SQL rollback: actual PG transaction rollback
    # after the two post-write checkpoints is a separate integration check.
