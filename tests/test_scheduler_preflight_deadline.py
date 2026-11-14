"""Actual PostgreSQL admission checks retain reservations on one shared deadline."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from doonex import controller_operations as co
from doonex import scheduling
from doonex.arena import Arena, EvaluationInput
from doonex.background import Background
from doonex.budget import BatchHalt, Ledger
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.model_registry import ModelRegistry
from doonex.store import dispatch_lock, external_execution_fenced, now
from psycopg.types.json import Jsonb
from test_tasksets import taskset_source as source_fixture

taskset_source = source_fixture
pytestmark = pytest.mark.postgres


@pytest.fixture
def admitted(settings, store, taskset_source, tmp_path):
    registry = co.ControllerRegistry(
        store,
        actor=co.fresh_actor("api"),
        check_owner=lambda: None,
        check_other_fences=external_execution_fenced,
    )
    with co.bind_registry(registry):
        source, spec, _, _, _ = taskset_source
        spec["split"] = "tuning"
        (source / "taskset.json").write_text(json.dumps(spec))
        wall = [datetime(2031, 4, 5, 10, tzinfo=UTC)]
        background = Background(settings, store, clock=lambda: wall[0])
        taskset = background.library.import_taskset(source)
        candidates = []
        for index in range(2):
            candidate = tmp_path / f"candidate-{index}"
            candidate.mkdir()
            (candidate / "SKILL.md").write_text(f"Inspect authorized changes. {index}.")
            skill = background.library.import_skill(candidate, {})
            background.library.review(skill["version_id"], "approved", "Test fixture")
            candidates.append(skill["version_id"])
        body = {
            "taskset_id": taskset["id"],
            "skill_version_ids": candidates,
            "model_profile_id": "legacy",
            "window_start": "2031-04-05T10:00:00Z",
            "window_end": "2031-04-05T11:00:00Z",
            "batch_budget": {
                "max_calls": 20,
                "max_tokens": 200_000,
                "max_cost_usd": 2,
                "max_seconds": 300,
            },
            "daily_budget": {
                "max_calls": 40,
                "max_tokens": 400_000,
                "max_cost_usd": 4,
                "max_seconds": 600,
            },
        }
        draft = background.create(body)
        grant = background.enable(draft["id"], draft["revision"])
        arena = Arena(settings, store)
        with store.connect() as connection:
            dispatch_lock(connection)
            claim = background.claim(connection, grant["id"])
            binding = {
                key: claim[key]
                for key in ("authorization_id", "date", "reservation_id")
            }
            batch = arena.create(
                EvaluationInput(**claim["evaluation_input"]),
                connection=connection,
                background=binding,
            )
            background.attach(connection, claim, batch)
        assert arena.start(batch["id"])
        run_id = batch["attempts"][0]["run_id"]
        with store.locked(run_id) as (_, run):
            run.update(state="running", first_started_at=now())
        scheduler = scheduling.Scheduler(settings, store)
        scheduler.background = background
        yield SimpleNamespace(
            store=store,
            background=background,
            scheduler=scheduler,
            arena=arena,
            grant=background.get(grant["id"]),
            batch=batch,
            run_id=run_id,
            wall=wall,
        )


def snapshot(context):
    """Compare actual durable rows, including original unknown reservations."""
    with context.store.connect() as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "actions",
                "runs",
                "arena_evaluations",
                "background_authorizations",
                "background_allocations",
                "background_days",
            )
        }


def clock_at(monkeypatch, value=100.0):
    clock = [value]
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    return clock


def slow_profile(monkeypatch, clock):
    original = ModelRegistry.get
    reads = []

    def read(registry, *args, **kwargs):
        profile = original(registry, *args, **kwargs)
        reads.append((profile.id, profile.revision))
        clock[0] = 102.0
        return profile

    monkeypatch.setattr(ModelRegistry, "get", read)
    return reads


def test_preflight_forwards_deadline_through_real_metadata(admitted, monkeypatch):
    calls = []
    original = admitted.background._unchanged

    def observe(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(admitted.background, "_unchanged", observe)
    clock_at(monkeypatch)
    before = snapshot(admitted)
    with admitted.store.connect() as connection:
        admitted.scheduler.preflight(
            connection, admitted.store.get(admitted.run_id), deadline=101.0
        )
    assert calls == [{"inspect_contents": False, "deadline": 101.0}]
    assert snapshot(admitted) == before


@pytest.mark.parametrize("kind", ["model", "shell"])
@pytest.mark.parametrize("unknown", [False, True])
def test_slow_metadata_expires_before_new_ledger_action(
    admitted, monkeypatch, kind, unknown
):
    if unknown:
        Ledger(admitted.store, admitted.run_id).reserve(3, 4)
        with admitted.store.locked(admitted.run_id) as (connection, run):
            admitted.store.mark_pending_unknown(admitted.run_id, connection, run)
        saved = admitted.store.get(admitted.run_id)
        assert saved["usage"]["unknown_calls"] == 1
        assert saved["usage"]["reserved_tokens"] == 7
    before = snapshot(admitted)
    clock = clock_at(monkeypatch)
    reads = slow_profile(monkeypatch, clock)
    ledger = Ledger(
        admitted.store,
        admitted.run_id,
        dispatch_check=lambda connection, run: admitted.scheduler.preflight(
            connection, run, deadline=101.0
        ),
    )
    with pytest.raises(ExecutionDeadlineExceeded):
        if kind == "model":
            ledger.reserve(5, 6)
        else:
            ledger.tool_intent("never dispatched")
    assert len(reads) == 1 and reads[0][0] == "legacy"
    assert snapshot(admitted) == before


@pytest.mark.parametrize("stage", ["entry", "day_lock", "capacity", "foreground"])
def test_preflight_checks_entry_lock_and_final_admission(admitted, monkeypatch, stage):
    clock = clock_at(monkeypatch)
    if stage == "entry":
        clock[0] = 102.0
        monkeypatch.setattr(
            admitted.background,
            "preflight",
            lambda *_a, **_k: pytest.fail("Expired entry reached metadata"),
        )
    elif stage == "day_lock":
        original = admitted.background.day

        def day(*args, **kwargs):
            result = original(*args, **kwargs)
            assert kwargs["lock"] is True
            clock[0] = 102.0
            return result

        monkeypatch.setattr(admitted.background, "day", day)
    elif stage == "capacity":
        original = admitted.background._capacity

        def capacity(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] = 102.0
            return result

        monkeypatch.setattr(admitted.background, "_capacity", capacity)
    else:
        original = scheduling.foreground_waiting

        def foreground(connection):
            result = original(connection)
            clock[0] = 102.0
            return result

        monkeypatch.setattr(scheduling, "foreground_waiting", foreground)
    before = snapshot(admitted)
    with (
        pytest.raises(ExecutionDeadlineExceeded),
        admitted.store.connect() as connection,
    ):
        admitted.scheduler.preflight(
            connection, admitted.store.get(admitted.run_id), deadline=101.0
        )
    assert snapshot(admitted) == before


def test_authorization_lock_wait_rechecks_deadline_before_metadata(
    admitted, monkeypatch
):
    real_monotonic, real_sleep = time.monotonic, time.sleep
    clock = clock_at(monkeypatch)
    monkeypatch.setattr(
        admitted.background,
        "_unchanged",
        lambda *_a, **_k: pytest.fail("Expired lock wait reached metadata"),
    )
    before = snapshot(admitted)
    ready, pids = threading.Event(), []

    def attempt():
        with admitted.store.connect() as connection:
            pids.append(
                connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            ready.set()
            admitted.background.preflight(
                connection, admitted.grant["id"], admitted.batch["id"], deadline=101.0
            )

    with admitted.store.connect() as blocker, admitted.store.connect() as observer:
        observer.autocommit = True
        blocker.execute(
            "SELECT id FROM background_authorizations WHERE id=%s FOR UPDATE",
            (admitted.grant["id"],),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(copy_context().run, attempt)
            try:
                assert ready.wait(timeout=2)
                end = real_monotonic() + 1
                while real_monotonic() < end:
                    row = observer.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (pids[0],),
                    ).fetchone()
                    if row and row["wait_event_type"] == "Lock":
                        break
                    real_sleep(0.01)
                else:
                    pytest.fail(
                        "Actual preflight did not wait on the authorization row"
                    )
                clock[0] = 102.0
            finally:
                # Release even if observing the real wait fails, before joining.
                blocker.commit()
            with pytest.raises(ExecutionDeadlineExceeded):
                future.result(timeout=3)
    assert snapshot(admitted) == before


@pytest.mark.parametrize(
    "deadline,expected", [(None, 130.0), (110.0, 110.0), (200.0, 130.0)]
)
def test_maintenance_uses_one_clipped_deadline(
    admitted, monkeypatch, deadline, expected
):
    clock_at(monkeypatch)
    original = admitted.background.maintenance_batch
    observed = []

    def batch(*args, **kwargs):
        observed.append(kwargs["deadline"])
        return original(*args, **kwargs)

    monkeypatch.setattr(admitted.background, "maintenance_batch", batch)
    before = snapshot(admitted)
    admitted.scheduler.maintain(deadline=deadline)
    assert observed and all(value == expected for value in observed)
    assert snapshot(admitted) == before


def test_maintenance_metadata_expiry_keeps_whole_allocation(admitted, monkeypatch):
    before = snapshot(admitted)
    clock = clock_at(monkeypatch)
    reads = slow_profile(monkeypatch, clock)
    with pytest.raises(ExecutionDeadlineExceeded):
        admitted.scheduler.maintain(deadline=101.0)
    assert len(reads) == 1
    assert snapshot(admitted) == before


def test_maintenance_expiry_rolls_back_actual_terminal_settlement(
    admitted, monkeypatch
):
    for attempt in admitted.batch["attempts"]:
        admitted.store.transition(
            attempt["run_id"], "cancelled", "Controlled test stop"
        )
    admitted.arena.sync(admitted.batch["id"])
    assert admitted.arena.get(admitted.batch["id"])["state"] == "complete"
    before = snapshot(admitted)
    clock = clock_at(monkeypatch)
    original = admitted.background.settle
    settled = []

    def settle(connection, *args, **kwargs):
        result = original(connection, *args, **kwargs)
        state = connection.execute(
            "SELECT state FROM background_allocations WHERE evaluation_id=%s",
            (admitted.batch["id"],),
        ).fetchone()["state"]
        settled.append(state)
        clock[0] = 102.0
        return result

    monkeypatch.setattr(admitted.background, "settle", settle)
    with pytest.raises(ExecutionDeadlineExceeded):
        admitted.scheduler.maintain(deadline=101.0)
    assert settled == ["settled"]
    assert snapshot(admitted) == before


@pytest.mark.parametrize(
    "guard", ["window", "allocation_duration", "day_binding", "day_capacity"]
)
def test_original_wall_window_and_day_guards_still_reject(admitted, monkeypatch, guard):
    if guard == "window":
        admitted.wall[0] += timedelta(hours=1)
    else:
        with admitted.store.connect() as connection:
            if guard == "allocation_duration":
                row = connection.execute(
                    "SELECT data FROM background_allocations WHERE evaluation_id=%s",
                    (admitted.batch["id"],),
                ).fetchone()["data"]
                row["claimed_at"] = (
                    admitted.wall[0] - timedelta(seconds=row["reserved"]["seconds"])
                ).isoformat()
                connection.execute(
                    "UPDATE background_allocations SET data=%s WHERE evaluation_id=%s",
                    (Jsonb(row), admitted.batch["id"]),
                )
            else:
                row = deepcopy(
                    admitted.background.day(admitted.grant["date"], connection)
                )
                if guard == "day_binding":
                    row["budget"]["max_calls"] += 1
                else:
                    row["used"]["calls"] = row["budget"]["max_calls"] + 1
                connection.execute(
                    "UPDATE background_days SET data=%s WHERE day=%s",
                    (Jsonb(row), admitted.grant["date"]),
                )
    before = snapshot(admitted)
    clock_at(monkeypatch)
    with pytest.raises(BatchHalt), admitted.store.connect() as connection:
        admitted.scheduler.preflight(
            connection, admitted.store.get(admitted.run_id), deadline=101.0
        )
    assert snapshot(admitted) == before
