"""Real PostgreSQL admission uses one Worker epoch for every background scope.

Only time sources are synthetic. Materials, frozen plans, locks, reservations,
unknown outcomes and settlement all use their production implementations.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import controller_test_support
import pytest
from doonex.arena import Arena, EvaluationInput, digest
from doonex.background import BackgroundConflict, BackgroundStopped
from doonex.budget import BatchHalt, Ledger
from doonex.execution_clock import ClockInvalid, ExecutionClock
from doonex.store import dispatch_lock
from doonex.worker import Worker
from psycopg.types.json import Jsonb
from test_tasksets import taskset_source as source_fixture

taskset_source = source_fixture
controller_registry = controller_test_support.controller_registry
pytestmark = pytest.mark.postgres


def observe(context):
    with context.store.locked(context.run_id, context.worker.owner, dispatch=True) as (
        connection,
        run,
    ):
        return context.worker.scheduler.execution_bound(connection, run)


def snapshot(context):
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


def allocate(context, *, attach_monotonic=None, start_elapsed=10):
    with context.store.connect() as connection:
        dispatch_lock(connection)
        claim = context.background.claim(connection, context.grant["id"])
        binding = {
            key: claim[key] for key in ("authorization_id", "date", "reservation_id")
        }
        batch = context.arena.create(
            EvaluationInput(**claim["evaluation_input"]),
            connection=connection,
            background=binding,
        )
        if attach_monotonic is not None:
            context.offsets["mono"] = attach_monotonic
        context.background.attach(connection, claim, batch)
    context.offsets.update(wall=start_elapsed, mono=start_elapsed)
    assert context.arena.start(batch["id"])
    context.batch_id = batch["id"]
    context.run_id = batch["attempts"][0]["run_id"]
    with context.store.locked(context.run_id) as (_, run):
        run.update(
            state="running",
            owner=context.worker.owner,
            first_started_at=context.wall().isoformat(),
        )
    context.first_started_at = context.store.get(context.run_id)["first_started_at"]
    context.bound = observe(context)
    return context


@pytest.fixture
def background_clock(
    settings, store, taskset_source, tmp_path, monkeypatch, controller_registry
):
    def create(*, window_seconds=3600, full_plan=False, admitted=True):
        stamp = datetime(2031, 4, 5, 12, tzinfo=UTC)
        mono = time.monotonic()
        offsets = {"wall": 0.0, "mono": 0.0}

        def wall():
            return stamp + timedelta(seconds=offsets["wall"])

        class WallDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = wall()
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)

        clock = ExecutionClock(wall=wall, monotonic=lambda: mono + offsets["mono"])
        # Exercise the actual Worker constructor's single shared object.
        monkeypatch.setattr("doonex.execution_clock.ExecutionClock", lambda: clock)
        monkeypatch.setattr("doonex.budget.datetime", WallDateTime)
        monkeypatch.setattr("doonex.store.now", lambda: wall().isoformat())
        monkeypatch.setattr("doonex.arena.now", lambda: wall().isoformat())
        worker = Worker(settings)
        background = worker.scheduler.background
        background.clock = wall
        assert worker.execution_clock is worker.scheduler.execution_clock is clock
        assert background.execution_clock is clock

        source, spec, _, _, _ = taskset_source
        spec["split"] = "tuning"
        if full_plan:
            originals = deepcopy(spec["tasks"])
            spec["tasks"] = [
                {
                    **deepcopy(originals[index % len(originals)]),
                    "id": f"task-{index}",
                    "title": f"Task {index}",
                    "critical": index == 0,
                }
                for index in range(10)
            ]
        (source / "taskset.json").write_text(json.dumps(spec))
        taskset = background.library.import_taskset(source)
        candidates = []
        for index in range(3 if full_plan else 2):
            candidate = tmp_path / f"clock-candidate-{index}"
            candidate.mkdir()
            (candidate / "SKILL.md").write_text(
                f"Inspect only the authorized changes. Candidate {index}."
            )
            skill = background.library.import_skill(candidate, {})
            background.library.review(
                skill["version_id"], "approved", "Controlled clock fixture"
            )
            candidates.append(skill["version_id"])
        draft = background.create(
            {
                "taskset_id": taskset["id"],
                "skill_version_ids": candidates,
                "model_profile_id": "legacy",
                "repeats": 10 if full_plan else 1,
                "window_start": (stamp - timedelta(minutes=1)).isoformat(),
                "window_end": (stamp + timedelta(seconds=window_seconds)).isoformat(),
                "run_budget": {
                    "max_calls": 10,
                    "max_tokens": 100_000,
                    "max_cost_usd": 1,
                    "max_seconds": 600,
                },
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
        )
        grant = background.enable(draft["id"], draft["revision"])
        context = SimpleNamespace(
            store=store,
            worker=worker,
            background=background,
            arena=Arena(settings, store),
            grant=grant,
            offsets=offsets,
            wall=wall,
            mono=mono,
        )
        return allocate(context) if admitted else context

    return create


def unknown_ledger(context):
    ledger = Ledger(
        context.worker.store,
        context.run_id,
        context.worker.owner,
        dispatch_check=context.worker.scheduler.preflight,
    )
    ledger.reserve(3, 4)
    with context.store.locked(context.run_id, context.worker.owner) as (c, run):
        context.store.mark_pending_unknown(context.run_id, c, run)
    run = context.store.get(context.run_id)
    batch = context.arena.get(context.batch_id)
    assert run["usage"]["unknown_calls"] == batch["usage"]["unknown_calls"] == 1
    assert run["usage"]["reserved_tokens"] == batch["usage"]["reserved_tokens"] == 7
    assert run["first_started_at"] == context.first_started_at
    return ledger


@pytest.mark.parametrize("scope", ["allocation", "window"])
@pytest.mark.parametrize("action", ["model", "tool"])
def test_earliest_background_scope_blocks_final_admission_without_reset(
    background_clock, scope, action
):
    context = background_clock(window_seconds=40 if scope == "window" else 3600)
    ledger = unknown_ledger(context)
    assert context.bound.scope.kind == scope
    context.offsets.update(
        wall=25.0 if scope == "window" else 100.0,
        mono=15.0 if scope == "window" else 20.0,
    )
    shorter = observe(context)
    assert shorter.scope.kind == scope and shorter.at < context.bound.at
    context.offsets.update(wall=11.0, mono=21.0)
    assert observe(context) == shorter
    before = snapshot(context)
    context.offsets["mono"] = shorter.at - context.mono + 1
    with pytest.raises(BatchHalt, match=scope):
        if action == "model":
            ledger.reserve(5, 6)
        else:
            ledger.tool_intent("never dispatched")
    assert snapshot(context) == before
    assert (
        context.store.get(context.run_id)["first_started_at"]
        == context.first_started_at
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "batch_first_start",
        "batch_budget",
        "batch_plan",
        "batch_background",
        "window",
        "allocation_claim",
        "allocation_parent",
        "day_budget",
        "day_date",
        "grant_state_column",
    ],
)
def test_changed_durable_background_binding_preserves_unknowns(
    background_clock, mutation
):
    context = background_clock()
    ledger = unknown_ledger(context)
    with context.store.connect() as connection:
        if mutation.startswith("batch_"):
            value = context.arena.get(context.batch_id, connection)
            if mutation == "batch_first_start":
                value["first_started_at"] = (
                    datetime.fromisoformat(value["first_started_at"])
                    - timedelta(seconds=1)
                ).isoformat()
            elif mutation == "batch_budget":
                value["batch_budget"]["max_seconds"] += 1
            elif mutation == "batch_plan":
                value["attempts"][0], value["attempts"][1] = (
                    value["attempts"][1],
                    value["attempts"][0],
                )
            else:
                value["background"]["reservation_id"] = str(uuid4())
            connection.execute(
                "UPDATE arena_evaluations SET data=%s WHERE id=%s",
                (Jsonb(value), context.batch_id),
            )
        elif mutation.startswith("allocation_"):
            value = connection.execute(
                "SELECT data FROM background_allocations WHERE evaluation_id=%s",
                (context.batch_id,),
            ).fetchone()["data"]
            if mutation == "allocation_claim":
                value["claimed_at"] = (
                    datetime.fromisoformat(value["claimed_at"]) - timedelta(seconds=1)
                ).isoformat()
            else:
                value["evaluation_id"] = str(uuid4())
            connection.execute(
                "UPDATE background_allocations SET data=%s WHERE evaluation_id=%s",
                (Jsonb(value), context.batch_id),
            )
        elif mutation.startswith("day_"):
            value = context.background.day(context.grant["date"], connection)
            if mutation == "day_budget":
                value["budget"]["max_seconds"] += 1
            else:
                value["date"] = "2031-04-06"
            connection.execute(
                "UPDATE background_days SET data=%s WHERE day=%s",
                (Jsonb(value), context.grant["date"]),
            )
        elif mutation == "window":
            value = context.background.get(context.grant["id"], connection)
            end = (
                datetime.fromisoformat(value["window_end"]) + timedelta(seconds=1)
            ).isoformat()
            value["window_end"] = value["expires_at"] = value["input"]["window_end"] = (
                end
            )
            connection.execute(
                "UPDATE background_authorizations SET data=%s WHERE id=%s",
                (Jsonb(value), context.grant["id"]),
            )
        else:
            connection.execute(
                "UPDATE background_authorizations SET state='disabled' WHERE id=%s",
                (context.grant["id"],),
            )
    before = snapshot(context)
    # These two persistent-row integrity checks currently retain their explicit
    # conflict type. Rejection here does not prove the parent batch was stopped.
    error = (
        BackgroundConflict
        if mutation in {"allocation_parent", "day_date"}
        else BatchHalt
    )
    with pytest.raises(error):
        ledger.reserve(5, 6)
    assert snapshot(context) == before
    assert (
        context.store.get(context.run_id)["first_started_at"]
        == context.first_started_at
    )


def test_expired_attach_rolls_back_claim_children_and_day_reservation(background_clock):
    context = background_clock(window_seconds=40, admitted=False)
    before = snapshot(context)
    with pytest.raises(BackgroundStopped, match="window"):
        allocate(context, attach_monotonic=41)
    assert snapshot(context) == before
    assert context.background.day(context.grant["date"])["reserved"]["seconds"] == 0


def test_utc_day_is_registered_from_original_persisted_budget(background_clock):
    context = background_clock()
    day = context.background.day(context.grant["date"])
    binding = digest({"date": day["date"], "budget": day["budget"]})
    changed = deepcopy(day["budget"])
    changed["max_seconds"] += 1
    # A changed binding must already conflict before this test directly observes
    # the scope: the production Background path registered the original day.
    with pytest.raises(ClockInvalid, match="binding_changed") as stopped:
        context.worker.execution_clock.day(
            day["date"], binding=digest({"date": day["date"], "budget": changed})
        )
    assert stopped.value.scope.kind == "day"
    original = context.worker.execution_clock.day(day["date"], binding=binding)
    # Same-day windows necessarily expire first; the midnight cutoff remains
    # bound to the shared day ledger rather than its max_seconds quota.
    assert original.scope.kind == "day"
    assert original.at == pytest.approx(context.mono + 12 * 3600)
    context.offsets.update(wall=100, mono=20)
    tighter = context.worker.execution_clock.day(day["date"], binding=binding)
    context.offsets.update(wall=11, mono=21)
    assert context.worker.execution_clock.day(day["date"], binding=binding) == tighter
    assert tighter.at < original.at
    assert observe(context).at <= tighter.at


@pytest.mark.parametrize(
    "locked_table", ["background_authorizations", "background_days"]
)
def test_real_background_row_wait_rechecks_original_clock(
    background_clock, locked_table
):
    context = background_clock()
    ledger = unknown_ledger(context)
    before = snapshot(context)
    ready = threading.Event()

    def reserve():
        ready.set()
        return ledger.reserve(5, 6)

    with context.store.connect() as blocker, context.store.connect() as observer:
        observer.autocommit = True
        blocker_pid = blocker.execute("SELECT pg_backend_pid() AS pid").fetchone()[
            "pid"
        ]
        if locked_table == "background_authorizations":
            blocker.execute(
                "SELECT id FROM background_authorizations WHERE id=%s FOR UPDATE",
                (context.grant["id"],),
            )
        else:
            blocker.execute(
                "SELECT day FROM background_days WHERE day=%s FOR UPDATE",
                (context.grant["date"],),
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(copy_context().run, reserve)
            try:
                assert ready.wait(timeout=2)
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    waiting = observer.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE %s=ANY(pg_blocking_pids(pid))) AS waiting",
                        (blocker_pid,),
                    ).fetchone()["waiting"]
                    if waiting:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("Ledger did not actually wait on the background row")
                context.offsets["mono"] = 301
            finally:
                blocker.commit()
            with pytest.raises(BatchHalt, match="allocation"):
                future.result(timeout=5)
    assert snapshot(context) == before


def test_settlement_keeps_monotonic_duration_after_expiry_and_wall_rollback(
    background_clock,
):
    context = background_clock()
    context.offsets.update(wall=100, mono=20)
    observe(context)
    for attempt in context.arena.get(context.batch_id)["attempts"]:
        context.store.transition(attempt["run_id"], "cancelled", "Controlled stop")
    context.arena.sync(context.batch_id)
    batch = context.arena.get(context.batch_id)
    assert batch["state"] == "complete"
    context.offsets.update(wall=-5, mono=305)
    with context.store.connect() as connection:
        allocation = context.background.settle(connection, context.batch_id, batch)
    assert allocation["actual"]["seconds"] >= 305
    assert allocation["actual"]["seconds"] > allocation["reserved"]["seconds"]
    assert allocation["claimed_at"] == datetime(2031, 4, 5, 12, tzinfo=UTC).isoformat()
    day = context.background.day(context.grant["date"])
    assert day["used"]["seconds"] == allocation["actual"]["seconds"]
    assert day["reserved"]["seconds"] == 0
    before = snapshot(context)
    with context.store.connect() as connection:
        assert (
            context.background.settle(connection, context.batch_id, batch) == allocation
        )
    assert snapshot(context) == before


def test_actual_four_hundred_child_plan_can_reach_last_admission(background_clock):
    context = background_clock(full_plan=True)
    batch = context.arena.get(context.batch_id)
    assert len(batch["attempts"]) == 400
    with context.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM runs WHERE "
                "data->'request'->'arena'->>'evaluation_id'=%s",
                (context.batch_id,),
            ).fetchone()["n"]
            == 400
        )
    ledger = unknown_ledger(context)
    reservation = ledger.reserve(5, 6)
    assert reservation["evaluation_id"] == context.batch_id
    run = context.store.get(context.run_id)
    assert run["usage"]["calls"] == 2 and run["usage"]["unknown_calls"] == 1
    assert run["usage"]["reserved_tokens"] == 18
    assert run["first_started_at"] == context.first_started_at


@pytest.mark.parametrize(
    "plan", ["401", "duplicate", "missing", "invalid_uuid", "not_list"]
)
def test_bad_frozen_plan_refuses_last_admission_without_losing_unknowns(
    background_clock, plan
):
    context = background_clock()
    ledger = unknown_ledger(context)
    with context.store.connect() as connection:
        value = context.arena.get(context.batch_id, connection)
        if plan == "401":
            value["attempts"].extend(
                {"run_id": str(uuid4())} for _ in range(401 - len(value["attempts"]))
            )
        elif plan == "duplicate":
            value["attempts"].append(deepcopy(value["attempts"][0]))
        elif plan == "missing":
            value["attempts"][0].pop("run_id")
        elif plan == "invalid_uuid":
            value["attempts"][0]["run_id"] = "invalid"
        else:
            value["attempts"] = {"run_id": context.run_id}
        connection.execute(
            "UPDATE arena_evaluations SET data=%s WHERE id=%s",
            (Jsonb(value), context.batch_id),
        )
    before = snapshot(context)
    with pytest.raises(BatchHalt, match="计划"):
        ledger.reserve(5, 6)
    assert snapshot(context) == before
    assert (
        context.store.get(context.run_id)["first_started_at"]
        == context.first_started_at
    )
