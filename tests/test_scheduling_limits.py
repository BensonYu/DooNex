"""Independent limits review using PostgreSQL, real SDK/Docker and fake transport."""

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import controller_test_support
import pytest
from doonex.arena import Arena
from doonex.background import Background, BackgroundStopped
from doonex.budget import BatchHalt, Ledger
from doonex.config import Settings
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.model_registry import ModelRegistry, RegistryError
from doonex.scheduling import Scheduler, foreground_waiting
from doonex.sdk import GuardedLLM
from doonex.store import now, termination_unknown
from doonex.worker import Worker
from psycopg.types.json import Jsonb
from test_scheduling import claimed
from test_scheduling import taskset_source as source_fixture
from test_scheduling import tuning as tuning_fixture
from test_worker import response

tuning = tuning_fixture
controller_registry = controller_test_support.controller_registry
taskset_source = source_fixture
pytestmark = pytest.mark.postgres


def test_background_preparation_uses_one_deadline_and_expiry_does_not_claim(
    tuning, store, monkeypatch
):
    settings, background, body, _ = tuning
    draft = background.create(body)
    grant = background.enable(draft["id"], draft["revision"])
    scheduler = Scheduler(settings, store)
    original = scheduler.background.prepare
    deadline = time.monotonic() + 10
    observed = []

    def expired(*args, **kwargs):
        observed.append(kwargs["deadline"])
        return original(*args, **{**kwargs, "deadline": time.monotonic() - 1})

    monkeypatch.setattr(scheduler.background, "prepare", expired)
    assert scheduler.claim_next(deadline=deadline) is None
    assert observed == [deadline]
    final = background.get(grant["id"])
    assert final["state"] == "enabled" and final["evaluation_id"] is None
    assert final["snapshot"] == grant["snapshot"] and final["input"] == grant["input"]
    assert final["dispatch_status"]["state"] == "blocked"
    assert ExecutionDeadlineExceeded.__name__ in final["dispatch_status"]["reason"]
    with store.connect() as c:
        for table in ("arena_evaluations", "background_allocations", "runs"):
            assert c.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0


def test_background_preparation_keeps_deadline_through_atomic_batch_creation(
    tuning, store, monkeypatch
):
    settings, background, body, _ = tuning
    draft = background.create(body)
    grant = background.enable(draft["id"], draft["revision"])
    observed = []
    for owner, method in (
        (Background, "prepare"),
        (Arena, "prepare"),
        (Background, "claim"),
        (Arena, "create"),
    ):
        original = getattr(owner, method)

        def track(self, *args, _original=original, _method=method, **kwargs):
            observed.append((_method, kwargs["deadline"]))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(owner, method, track)
    deadline = time.monotonic() + 10
    batch = Scheduler(settings, store).claim_next(deadline=deadline)
    assert batch is not None and batch["background"]["authorization_id"] == grant["id"]
    assert [name for name, _ in observed] == ["prepare", "prepare", "claim", "create"]
    assert all(value == deadline for _, value in observed)
    assert batch["usage"]["calls"] == batch["usage"]["tokens"] == 0


@pytest.mark.parametrize("stage", ["metadata", "allocation"])
def test_background_claim_expiry_rolls_back_original_grant_and_allocation(
    tuning, store, monkeypatch, stage
):
    settings, background, body, _ = tuning
    draft = background.create(body)
    grant = background.enable(draft["id"], draft["revision"])
    before_day = deepcopy(background.day(grant["date"]))
    clock = [100.0]
    original = getattr(Background, "_unchanged" if stage == "metadata" else "_refresh_day")
    observed = []

    def expire(self, *args, **kwargs):
        if stage == "allocation" or kwargs.get("inspect_contents") is False:
            observed.append(kwargs.get("deadline"))
            clock[0] = 102.0
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        Background, "_unchanged" if stage == "metadata" else "_refresh_day", expire
    )
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    assert Scheduler(settings, store).claim_next(deadline=101.0) is None
    assert observed == ([101.0] if stage == "metadata" else [None])
    saved = background.get(grant["id"])
    assert saved["state"] == "enabled" and saved["evaluation_id"] is None
    assert saved["input"] == grant["input"] and saved["snapshot"] == grant["snapshot"]
    assert ExecutionDeadlineExceeded.__name__ in saved["dispatch_status"]["reason"]
    assert background.day(grant["date"]) == before_day
    with store.connect() as c:
        for table in ("arena_evaluations", "background_allocations", "runs", "actions"):
            assert c.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0


def counts(store):
    with store.connect() as c:
        return {
            name: c.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"]
            for name in ("arena_evaluations", "background_allocations", "actions")
        }


@pytest.mark.parametrize("stop", ["expiry", "next_day", "revoked"])
def test_unused_batch_stops_and_known_zero_usage_settles_its_original_day(
    tuning, store, monkeypatch, stop
):
    settings, background, grant, batch = claimed(tuning, store)
    original_day = background.day(grant["date"])
    assert original_day["reserved"]["calls"] == batch["batch_budget"]["max_calls"]
    if stop == "revoked":
        background.disable(grant["id"], grant["revision"])
    else:
        stopped_time = datetime.fromisoformat(grant["window_end"])
        if stop == "next_day":
            stopped_time += timedelta(days=1)
        monkeypatch.setattr(Background, "_now", lambda _: stopped_time)
    Scheduler(settings, store).maintain()
    arena = Arena(settings, store)
    assert arena.get(batch["id"])["state"] == "cancelled"
    assert all(
        store.get(item["run_id"])["state"] == "cancelled" for item in batch["attempts"]
    )
    day = background.day(grant["date"])
    assert day["reserved"]["calls"] == day["reserved"]["tokens"] == 0
    assert day["used"]["calls"] == day["used"]["tokens"] == 0
    assert not counts(store)["actions"]
    assert (
        counts(store)["arena_evaluations"]
        == counts(store)["background_allocations"]
        == 1
    )
    assert Scheduler(settings, store).claim_next() is None
    assert background.get(grant["id"])["evaluation_id"] == batch["id"]
    if stop == "next_day":
        with pytest.raises(KeyError):
            background.day(stopped_time.date().isoformat())


def test_future_window_waits_without_reading_task_contents_and_status_is_deduplicated(
    tuning, store, monkeypatch
):
    settings, background, body, _ = tuning
    clock = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(Background, "_now", lambda _: clock)
    body.update(
        window_start=(clock + timedelta(hours=1)).isoformat(),
        window_end=(clock + timedelta(hours=2)).isoformat(),
    )
    draft = background.create(body)
    grant = background.enable(draft["id"], draft["revision"])
    monkeypatch.setattr(
        "doonex.library.Library.taskset",
        lambda *_: pytest.fail("Scanned before window"),
    )
    scheduler = Scheduler(settings, store)
    assert scheduler.claim_next() is None
    first = background.get(grant["id"])
    assert scheduler.claim_next() is None
    second = background.get(grant["id"])
    assert second["state"] == "enabled" and first == second
    assert second["dispatch_status"]["state"] == "waiting"
    assert not any(counts(store).values())
    assert background.day(grant["date"])["reserved"]["calls"] == 0


@pytest.mark.parametrize("error", [KeyError, RegistryError, OSError])
def test_profile_read_failure_stops_only_background_and_retains_unknown_allocation(
    tuning, store, monkeypatch, error
):
    settings, background, grant, batch = claimed(tuning, store)
    run_id = batch["attempts"][0]["run_id"]
    with store.locked(run_id) as (c, run):
        run.update(state="interrupted", first_started_at=now())
        run["usage"]["calls"] = run["usage"]["unknown_calls"] = 1
        run["usage"]["reserved_tokens"] = 100
        store.action(run_id, "model", {"tokens": 100}, c)

    def fail(*_):
        raise error("SENSITIVE_CONFIG_DETAIL")

    monkeypatch.setattr(ModelRegistry, "get", fail)
    Scheduler(settings, store).maintain()
    assert Arena(settings, store).get(batch["id"])["state"] == "cancelled"
    assert (
        "SENSITIVE_CONFIG_DETAIL"
        not in Arena(settings, store).get(batch["id"])["reason"]
    )
    assert background.day(grant["date"])["reserved"]["calls"] == 20
    assert background.day(grant["date"])["used"]["calls"] == 0
    assert store.get(run_id)["state"] == "interrupted"


def test_invalid_enabled_profile_records_blocked_status_without_consuming_authorization(
    tuning, store, monkeypatch
):
    settings, background, body, _ = tuning
    draft = background.create(body)
    grant = background.enable(draft["id"], draft["revision"])

    def fail(*_):
        raise KeyError("PRIVATE_CONFIG_DETAIL")

    monkeypatch.setattr(ModelRegistry, "get", fail)
    assert Scheduler(settings, store).claim_next() is None
    result = background.get(grant["id"])
    assert result["state"] == "enabled" and result["evaluation_id"] is None
    assert result["dispatch_status"]["state"] == "blocked"
    assert "PRIVATE_CONFIG_DETAIL" not in json.dumps(result)
    assert not any(counts(store).values())
    assert background.day(grant["date"])["reserved"]["calls"] == 0


def test_queued_children_of_inactive_manual_arena_do_not_starve_background(
    tuning, store, request_data
):
    settings, background, body, _ = tuning
    manual_id = str(uuid4())
    with store.connect() as c:
        c.execute(
            "INSERT INTO arena_evaluations VALUES(%s,'interrupted',%s)",
            (manual_id, Jsonb({"id": manual_id, "state": "interrupted"})),
        )
        request = {**request_data, "arena": {"evaluation_id": manual_id}}
        store.create_run(request, connection=c)
    with store.connect() as c:
        assert foreground_waiting(c) is False
    draft = background.create(body)
    background.enable(draft["id"], draft["revision"])
    batch = Scheduler(settings, store).claim_next()
    assert batch is not None and batch.get("background")


def test_other_model_cannot_bypass_reserved_daily_calls_or_change_daily_cap(
    tuning, store
):
    settings, background, grant, _ = claimed(tuning, store)
    second_settings = Settings(
        settings.home,
        settings.database_url,
        settings.token,
        settings.model.model_copy(update={"model_id": "different-synthetic-model"}),
    )
    second = Background(second_settings, store)
    body = deepcopy(grant["input"])
    body["batch_budget"]["max_calls"] = 21
    draft = second.create(body)
    another = second.enable(draft["id"], draft["revision"])
    prepared = second.prepare(another["id"])
    with pytest.raises(BackgroundStopped, match="日预算"), store.connect() as c:
        second.claim(c, another["id"], prepared=prepared)
    assert counts(store)["background_allocations"] == 1
    assert background.day(grant["date"])["reserved"]["calls"] == 20
    assert second.get(another["id"])["state"] == "enabled"


@pytest.mark.docker
@pytest.mark.parametrize("stop", ["deadline", "parent_cancel"])
def test_verifier_cannot_commit_intent_when_batch_stops_after_outer_check(
    tuning, store, monkeypatch, stop
):
    settings, _, _, batch = claimed(tuning, store)
    run_id = batch["attempts"][0]["run_id"]
    replies = [
        response("doonex_shell", {"command": "printf 'answer = 42\\n' > main.py"}, 0),
        response("finish", {"message": "Done"}, 1),
    ]
    monkeypatch.setattr(GuardedLLM, "_transport_call", lambda *_, **__: replies.pop(0))
    from doonex import evidence

    actual_snapshot = evidence._verifier_snapshot
    changed = []

    def stop_after_check_copy(path, context, **kwargs):
        result = actual_snapshot(path, context, **kwargs)
        if not changed and any(
            part.startswith("doonex-check-") for part in Path(path).parts
        ):
            changed.append(True)
            with store.connect() as c:
                row = c.execute(
                    "SELECT data FROM arena_evaluations WHERE id=%s FOR UPDATE",
                    (batch["id"],),
                ).fetchone()["data"]
                if stop == "deadline":
                    row["first_started_at"] = (
                        datetime.now(UTC)
                        - timedelta(seconds=row["batch_budget"]["max_seconds"] + 1)
                    ).isoformat()
                else:
                    row["state"] = "cancelling"
                c.execute(
                    "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
                    (row["state"], Jsonb(row), batch["id"]),
                )
        return result

    monkeypatch.setattr(evidence, "_verifier_snapshot", stop_after_check_copy)
    Worker(settings).run(once=True)
    assert changed and not replies
    with store.connect() as c:
        verification = c.execute(
            "SELECT count(*) AS n FROM actions WHERE run_id=%s AND kind='verification'",
            (run_id,),
        ).fetchone()["n"]
    assert verification == 0
    assert store.get(run_id)["state"] in {"cancelled", "budget_exceeded"}


@pytest.mark.parametrize("stop", ["expiry", "revoked"])
def test_atomic_model_admission_stops_with_original_daily_allocation(
    tuning, store, monkeypatch, stop
):
    settings, background, grant, batch = claimed(tuning, store)
    assert Arena(settings, store).start(batch["id"])
    run_id = batch["attempts"][0]["run_id"]
    with store.locked(run_id) as (_, run):
        run.update(state="running", first_started_at=now(), owner="test")
    if stop == "expiry":
        closed = datetime.fromisoformat(grant["window_end"])
        monkeypatch.setattr(Background, "_now", lambda _: closed)
    else:
        background.disable(grant["id"], grant["revision"])
    ledger = Ledger(
        store, run_id, "test", dispatch_check=Scheduler(settings, store).preflight
    )
    with pytest.raises(BatchHalt):
        ledger.reserve(100, 100)
    assert store.get(run_id)["usage"]["calls"] == 0
    assert counts(store)["actions"] == 0
    assert background.day(grant["date"])["reserved"]["calls"] == 20


@pytest.mark.docker
def test_legacy_container_unknown_blocks_resume_without_resetting_unknown_usage(
    tuning, store, monkeypatch
):
    settings, background, grant, batch = claimed(tuning, store)
    run_id = batch["attempts"][0]["run_id"]

    def interrupted(*_, **__):
        raise KeyboardInterrupt("Synthetic unknown provider response")

    monkeypatch.setattr(GuardedLLM, "_transport_call", interrupted)
    Worker(settings).run(once=True)
    before = store.get(run_id)
    assert before["state"] == "interrupted"
    assert before["usage"]["unknown_calls"] == 1
    assert before["usage"]["reserved_tokens"] > 0
    assert Arena(settings, store).get(batch["id"])["state"] == "interrupted"
    with store.locked(run_id) as (c, current):
        current["recovery"] = {
            "remaining": ["previously-unconfirmed-container"],
            "errors": ["old cleanup could not confirm termination"],
            "stopped": [],
        }
        store.event(run_id, "termination_unknown", current["recovery"], c)
    with store.connect() as c:
        assert termination_unknown(c)
    preserved = store.get(run_id)
    arena = Arena(settings, store)
    preserved_batch = arena.get(batch["id"])
    preserved_day = background.day(grant["date"])

    def records():
        with store.connect() as c:
            return {
                table: c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("actions", "controller_operations", "events")
            }

    preserved_records = records()

    def forbidden(*_args, **_kwargs):
        pytest.fail("A legacy unknown without an original operation cannot probe")

    monkeypatch.setattr("doonex.sandbox.Sandbox.containers", forbidden)
    # No original operation identifies this historical uncertainty. Neither a
    # confirmation nor a hypothetical empty ps authorizes clearing the fence.
    for acknowledged in (False, True):
        with pytest.raises(ValueError, match="控制进程、Docker 请求或模型传输进程"):
            arena.resume(batch["id"], acknowledged)
        assert store.get(run_id) == preserved
        assert arena.get(batch["id"]) == preserved_batch
        assert background.day(grant["date"]) == preserved_day
        assert records() == preserved_records
    after = store.get(run_id)
    assert after["state"] == "interrupted"
    assert after["usage"] == before["usage"]
    assert after["first_started_at"] == before["first_started_at"]
    assert after["recovery"] == preserved["recovery"]
    assert after["recovery"]["remaining"] == ["previously-unconfirmed-container"]
    assert after["recovery"]["errors"] == ["old cleanup could not confirm termination"]
    assert "confirmed_at" not in after["recovery"]
    with store.connect() as c:
        assert termination_unknown(c)
        assert (
            c.execute(
                "SELECT count(*) AS n FROM events WHERE run_id=%s AND kind='termination_unknown'",
                (run_id,),
            ).fetchone()["n"]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) AS n FROM actions WHERE run_id=%s AND kind='model' AND status='unknown'",
                (run_id,),
            ).fetchone()["n"]
            == 1
        )
    assert background.day(grant["date"])["reserved"]["calls"] == 20


def test_foreground_wait_cannot_extend_original_claim_lifetime(
    tuning, store, monkeypatch
):
    settings, background, grant, batch = claimed(tuning, store)
    run_id = batch["attempts"][0]["run_id"]
    assert Arena(settings, store).start(batch["id"])
    # The Run and Arena have only just started; the original daily allocation
    # has already spent its finite lifetime waiting for the foreground queue.
    with store.locked(run_id) as (_, run):
        run.update(state="running", first_started_at=now(), owner="test")
    claim_time = datetime.fromisoformat(grant["consumed_at"])
    advanced = claim_time + timedelta(seconds=batch["batch_budget"]["max_seconds"])
    assert advanced < datetime.fromisoformat(grant["window_end"])
    monkeypatch.setattr(Background, "_now", lambda _: advanced)
    ledger = Ledger(
        store, run_id, "test", dispatch_check=Scheduler(settings, store).preflight
    )
    with pytest.raises(BatchHalt):
        ledger.reserve(100, 100)
    assert counts(store)["actions"] == 0
    assert store.get(run_id)["usage"]["calls"] == 0
    assert background.day(grant["date"])["reserved"]["calls"] == 20
