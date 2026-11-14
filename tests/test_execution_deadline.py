"""A shared clock reaches SDK restore, hashing, locks and Worker callbacks."""

import threading
import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace

import pytest
from doonex import sdk
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.evidence import Evidence
from doonex.library import Library
from doonex.review import Review
from doonex.sandbox import Sandbox
from doonex.scheduling import Scheduler
from doonex.session_store import SessionFileStore, SessionStorageError, canonical_sha256
from doonex.worker import Worker, exception_cause
from test_sdk import answer, harness, response_harness, responses_answer
from test_worker import prepare_run, scripted_model


def test_session_deadline_cause_stays_latched_after_clock_allowance_changes(tmp_path):
    store = SessionFileStore(tmp_path, deadline=time.monotonic() + 10)
    store.write("base_state.json", "{}")
    store.deadline = time.monotonic() - 1
    with pytest.raises(SessionStorageError) as first:
        store.read("base_state.json")
    assert exception_cause(first.value, ExecutionDeadlineExceeded)
    store.deadline = time.monotonic() + 10
    with pytest.raises(SessionStorageError) as second:
        store.check_ok()
    assert exception_cause(second.value, ExecutionDeadlineExceeded)
    assert (tmp_path / "base_state.json").read_text() == "{}"
    store.close()


def test_canonical_hash_checks_during_bounded_encoding():
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 5:
            raise ExecutionDeadlineExceeded("Synthetic shared deadline")

    with pytest.raises(ExecutionDeadlineExceeded):
        canonical_sha256([{"text": "bounded"}] * 20, check=check)
    assert len(calls) == 5


def test_actual_sdk_lock_uses_the_same_deadline_and_leaves_no_callbacks(
    tmp_path, monkeypatch
):
    kwargs, reservations, settlements, commands, events = harness(tmp_path)
    deadline = time.monotonic() + 3
    kwargs["persistence_deadline"] = deadline
    lock, attempts = sdk._RUN_LOCK, []

    def acquire(*, timeout):
        entered = time.monotonic()
        result = lock.acquire(timeout=timeout)
        attempts.append((entered, timeout, result))
        return result

    with lock:
        monkeypatch.setattr(
            sdk, "_RUN_LOCK", SimpleNamespace(acquire=acquire, release=lock.release)
        )
        with pytest.raises(SessionStorageError) as raised:
            sdk.run_session(**kwargs)
        assert len(attempts) == 1
        entered, timeout, acquired = attempts[0]
        assert entered < deadline and acquired is False
        assert abs(entered + timeout - deadline) < 0.05
        assert deadline <= time.monotonic() < deadline + 1
    assert exception_cause(raised.value, ExecutionDeadlineExceeded)
    assert not reservations and not settlements and not commands and not events
    assert not sdk._CALLBACKS and not sdk._SCHEDULING and not sdk._SESSION_STORES


def test_resume_policy_reads_cannot_bypass_session_deadline(tmp_path, monkeypatch):
    kwargs, reservations, settlements, commands, _events = harness(
        tmp_path, should_stop=lambda: True
    )
    assert sdk.run_session(**kwargs) == "paused"
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    original = SessionFileStore.read

    def read(store, path):
        if path == "base_state.json":
            store.deadline = time.monotonic() - 1
        return original(store, path)

    monkeypatch.setattr(SessionFileStore, "read", read)
    kwargs.update(resume=True, prompt=None, should_stop=lambda: False)
    with pytest.raises(SessionStorageError) as raised:
        sdk.run_session(**kwargs)
    assert exception_cause(raised.value, ExecutionDeadlineExceeded)
    assert {
        str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before
    assert not reservations and not settlements and not commands
    assert not sdk._CALLBACKS and not sdk._SCHEDULING and not sdk._SESSION_STORES


@pytest.mark.parametrize("mode", ["chat", "responses"])
@pytest.mark.parametrize("tightening", ["store", "provider"])
def test_each_actual_sdk_request_clamps_timeout_after_reservation(
    tmp_path, monkeypatch, mode, tightening
):
    timeouts = []
    replies = iter(
        [
            (responses_answer if mode == "responses" else answer)(
                "doonex_shell", {"command": "echo bounded"}
            ),
            (responses_answer if mode == "responses" else answer)(
                "finish", {"message": "Done"}, sequence=2
            ),
        ]
    )

    def transport(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return next(replies)

    if mode == "responses":
        kwargs, reservations, settlements, commands, _ = response_harness(
            tmp_path, monkeypatch, transport
        )
    else:
        kwargs, reservations, settlements, commands, _ = harness(tmp_path)
        monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    before = kwargs["before_model"]
    bounds = [time.monotonic() + 30]

    def reserve(*args, **options):
        result = before(*args, **options)
        if len(reservations) == 2:
            # Simulate preparation/DB time consuming the original allowance.
            # This happens after admission and before the actual SDK call.
            bounds[0] = time.monotonic() + 2
            if tightening == "store":
                sdk._SESSION_STORES[kwargs["run_id"]].deadline = bounds[0]
        return result

    kwargs.update(before_model=reserve, persistence_deadline=time.monotonic() + 30)
    if tightening == "provider":
        kwargs["deadline_provider"] = lambda: bounds[0]
    assert sdk.run_session(**kwargs) == "finished"
    assert len(timeouts) == len(reservations) == len(settlements) == 2
    assert timeouts[0] == kwargs["model"]["timeout_seconds"]
    assert 0 < timeouts[1] <= 2 < timeouts[0]
    assert commands == ["echo bounded"]
    assert not sdk._CALLBACKS and not sdk._SCHEDULING and not sdk._SESSION_STORES


def test_failed_responses_diagnostic_records_actual_clamped_timeout(
    tmp_path, monkeypatch
):
    from openhands.sdk.conversation.exceptions import ConversationRunError

    captured = []

    def transport(*args, **kwargs):
        captured.append(kwargs["timeout"])
        raise RuntimeError("Synthetic transport failure")

    kwargs, reservations, settlements, commands, _ = response_harness(
        tmp_path, monkeypatch, transport
    )
    kwargs["persistence_deadline"] = time.monotonic() + 2
    with pytest.raises(ConversationRunError) as caught:
        sdk.run_session(**kwargs)
    assert len(captured) == len(reservations) == len(settlements) == 1
    error = exception_cause(caught.value, sdk.ModelCallError)
    assert error is not None
    assert 0 < error.diagnostics["timeout_seconds"] == captured[0] <= 2
    assert settlements[0][1] is None and not commands
    assert not sdk._CALLBACKS and not sdk._SCHEDULING and not sdk._SESSION_STORES


@pytest.mark.parametrize("stage", ["save", "validate"])
def test_scheduling_expiry_precedes_new_probe_or_snapshot(tmp_path, monkeypatch, stage):
    def unexpected(*args, **kwargs):
        pytest.fail("Expired scheduling step started external work")

    monkeypatch.setattr(Sandbox, "containers", unexpected)
    from doonex.workspace import Workspace

    monkeypatch.setattr(Workspace, "execution_digest", unexpected)
    scheduler = object.__new__(Scheduler)
    with pytest.raises(ExecutionDeadlineExceeded):
        if stage == "save":
            scheduler.save_yield(
                "unused", "owner", {}, None, deadline=time.monotonic() - 1
            )
        else:
            scheduler.validate_receipt(
                "unused", {}, None, deadline=time.monotonic() - 1
            )


def test_expired_container_probe_does_not_start_process(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Expired container probe started Docker")

    monkeypatch.setattr(Sandbox, "_containers", unexpected)
    with pytest.raises(ExecutionDeadlineExceeded):
        Sandbox().containers("unused", deadline=time.monotonic() - 1)


def test_expired_background_preparation_does_not_start_query():
    scheduler = object.__new__(Scheduler)
    with pytest.raises(ExecutionDeadlineExceeded):
        scheduler.claim_next(deadline=time.monotonic() - 1)


@pytest.mark.postgres
def test_worker_passes_original_remaining_clock_and_records_deadline_stop(
    settings, store, request_data, tmp_path, monkeypatch
):
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    first = (datetime.now(UTC) - timedelta(seconds=4)).isoformat()
    with store.locked(run["id"]) as (_, current):
        current["first_started_at"] = first
    deadlines = []

    def run_session(**kwargs):
        deadlines.append(kwargs["persistence_deadline"])
        raise ExecutionDeadlineExceeded("Synthetic original deadline stop")

    monkeypatch.setattr(sdk, "run_session", run_session)
    monkeypatch.setattr(
        Sandbox,
        "stop_run",
        lambda *_a, **_k: {"remaining": [], "errors": [], "stopped": []},
    )
    start = time.monotonic()
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert len(deadlines) == 1
    assert start < deadlines[0] <= start + run["request"]["budget"]["max_seconds"] - 4
    assert final["state"] == "budget_exceeded" and final["first_started_at"] == first
    assert final["usage"]["calls"] == 0 and not store.pending(run["id"])


@pytest.mark.postgres
@pytest.mark.parametrize("stage", ["startup", "run"])
def test_worker_initialization_expiry_respects_first_clock_boundary(
    settings, store, request_data, tmp_path, monkeypatch, stage
):
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    calls = scripted_model(monkeypatch, [])
    original = Library.__init__
    execute = Worker._execute
    in_execute = False
    observed = []

    def executing(worker, *args, **kwargs):
        nonlocal in_execute
        in_execute = True
        return execute(worker, *args, **kwargs)

    def expires(service, *args, **kwargs):
        if kwargs.get("deadline") is not None and in_execute == (stage == "run"):
            current = store.get(run["id"])
            if stage == "run":
                assert current["state"] == "running" and current["first_started_at"]
            else:
                assert current["state"] == "queued"
                assert current["first_started_at"] is None
            observed.append(kwargs["deadline"])
            kwargs["deadline"] = time.monotonic() - 1
        return original(service, *args, **kwargs)

    monkeypatch.setattr(Library, "__init__", expires)
    monkeypatch.setattr(Worker, "_execute", executing)
    monkeypatch.setattr(
        Sandbox,
        "stop_run",
        lambda *_a, **_k: {"remaining": [], "errors": [], "stopped": []},
    )
    if stage == "startup":
        with pytest.raises(ExecutionDeadlineExceeded):
            Worker(settings).run(once=True)
    else:
        Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert len(observed) == 1 and not calls
    if stage == "run":
        assert final["state"] == "budget_exceeded" and final["first_started_at"]
    else:
        assert final == store.get(run["id"]) == run
    assert final["usage"]["calls"] == final["usage"]["reserved_tokens"] == 0
    assert not store.pending(run["id"])
    assert not (settings.home / "runs" / run["id"]).exists()


@pytest.mark.postgres
@pytest.mark.parametrize("stage", ["model", "shell", "verification"])
def test_worker_rechecks_shared_deadline_after_preflight_before_action_commit(
    settings, store, request_data, tmp_path, monkeypatch, stage
):
    from doonex import deadline as deadline_module

    run, _, _ = prepare_run(
        store,
        settings,
        request_data,
        tmp_path,
        checks=["true"] if stage == "verification" else [],
    )
    calls = scripted_model(monkeypatch, ["true"] if stage == "shell" else [])
    original = Scheduler.preflight
    original_execute = Worker.execute
    observed = []
    expired = False
    execution_active = False

    def execute(worker, run_id):
        nonlocal execution_active
        execution_active = True
        try:
            return original_execute(worker, run_id)
        finally:
            execution_active = False

    def preflight(scheduler, connection, current, *, deadline, **kwargs):
        nonlocal expired
        assert deadline > time.monotonic()
        result = original(scheduler, connection, current, deadline=deadline, **kwargs)
        observed.append((deadline, current["first_started_at"]))
        if (
            stage == "model"
            or stage == "shell"
            and len(observed) == 2
            or stage == "verification"
            and current["state"] == "verifying"
        ):
            # Only the cooperative check clock advances. Actual subprocess,
            # database and SDK timing keep their real host monotonic clock.
            expired = True
        return result

    def forbidden(*_args, **_kwargs):
        pytest.fail("An expired preflight must not start a tool or verifier")

    monkeypatch.setattr(Scheduler, "preflight", preflight)
    monkeypatch.setattr(Worker, "execute", execute)
    monkeypatch.setattr(
        deadline_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: (
                time.monotonic() + (10000 if expired and execution_active else 0)
            )
        ),
    )
    monkeypatch.setattr(Sandbox, "run", forbidden)
    monkeypatch.setattr(
        Sandbox,
        "stop_run",
        lambda *_a, **_k: {"remaining": [], "errors": [], "stopped": []},
    )
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    expected_calls = 0 if stage == "model" else 1
    assert expired and len(observed) == (1 if stage == "model" else 2)
    assert all(later[0] <= earlier[0] for earlier, later in pairwise(observed))
    assert final["state"] == "budget_exceeded", final["reason"]
    assert final["first_started_at"] == observed[0][1]
    assert final["request"]["budget"] == run["request"]["budget"]
    assert final["usage"]["calls"] == len(calls) == expected_calls
    assert final["usage"]["tokens"] == expected_calls * 120
    assert final["usage"]["unknown_calls"] == final["usage"]["reserved_tokens"] == 0
    assert not store.pending(run["id"])
    with store.connect() as connection:
        actions = connection.execute(
            "SELECT kind,status FROM actions WHERE run_id=%s", (run["id"],)
        ).fetchall()
    assert [(a["kind"], a["status"]) for a in actions] == [
        ("model", "completed")
    ] * expected_calls
    assert not sdk._CALLBACKS and not sdk._SCHEDULING and not sdk._SESSION_STORES


@pytest.mark.postgres
@pytest.mark.parametrize("stage", ["review", "evidence"])
@pytest.mark.parametrize("wall_forward", [False, True])
def test_worker_expires_delivery_before_starting_new_work(
    settings, store, request_data, tmp_path, monkeypatch, stage, wall_forward
):
    clock_advanced = False
    worker = Worker(settings)
    # The Worker now retains one shared ExecutionClock across execute entries.
    # Advance its actual wall source, rather than the former elapsed helper.
    monkeypatch.setattr(
        worker.execution_clock,
        "_wall",
        lambda: datetime.now(UTC) + timedelta(seconds=5 if clock_advanced else 0),
    )
    if stage == "review":
        request_data["workflow"] = "execute_review"
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    calls = scripted_model(monkeypatch, [])
    deadlines = []
    original_session = sdk.run_session
    original_delivery = Review.prepare if stage == "review" else Evidence.capture

    def session(**kwargs):
        nonlocal clock_advanced
        deadlines.append(kwargs["persistence_deadline"])
        result = original_session(**kwargs)
        clock_advanced = wall_forward
        return result

    def expired_delivery(*args, **kwargs):
        # The original clock may tighten after a persisted wall-clock forward
        # step; it must remain live and may never extend the SDK's allowance.
        assert time.monotonic() < kwargs["deadline"] <= deadlines[0]
        if wall_forward and stage == "evidence":
            assert kwargs["deadline"] <= deadlines[0] - 4
        return original_delivery(*args, **{**kwargs, "deadline": time.monotonic() - 1})

    monkeypatch.setattr(sdk, "run_session", session)
    monkeypatch.setattr(
        Review if stage == "review" else Evidence,
        "prepare" if stage == "review" else "capture",
        expired_delivery,
    )
    monkeypatch.setattr(
        Sandbox,
        "stop_run",
        lambda *_a, **_k: {"remaining": [], "errors": [], "stopped": []},
    )
    worker.run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "budget_exceeded", final["reason"]
    assert len(calls) == len(deadlines) == final["usage"]["calls"] == 1
    assert final["usage"]["tokens"] == 120
    assert final["usage"]["unknown_calls"] == final["usage"]["reserved_tokens"] == 0
    assert not store.pending(run["id"]) and not final.get("evidence")
    assert not (settings.home / "runs" / run["id"] / stage).exists()


@pytest.mark.postgres
@pytest.mark.docker
def test_terminal_commit_wait_cannot_outlive_original_deadline(
    settings, store, request_data, tmp_path, monkeypatch
):
    request_data["budget"]["max_seconds"] = 8
    run, _, _ = prepare_run(store, settings, request_data, tmp_path, checks=["true"])
    calls = scripted_model(monkeypatch, [])
    original_session, original_inspect = sdk.run_session, Evidence.inspect
    deadlines, locks, threads = [], [], []

    def session(**kwargs):
        deadlines.append(kwargs["persistence_deadline"])
        return original_session(**kwargs)

    def inspect(*args, **kwargs):
        result = original_inspect(*args, **kwargs)
        if not locks:
            assert result["valid"] and time.monotonic() < deadlines[0]
            # Keep the actual lock hold below the configured 5 s DB limit,
            # while still crossing the original monotonic Run deadline.
            time.sleep(max(0, deadlines[0] - time.monotonic() - 3))
            connection = store.connect()
            row = connection.execute(
                "SELECT data FROM runs WHERE id=%s FOR UPDATE", (run["id"],)
            ).fetchone()["data"]
            assert row["state"] == "verifying"
            locks.append(row["first_started_at"])

            def release():
                try:
                    time.sleep(max(0, deadlines[0] - time.monotonic()) + 0.1)
                    connection.commit()
                finally:
                    connection.close()

            thread = threading.Thread(target=release, daemon=True)
            threads.append(thread)
            thread.start()
        return result

    monkeypatch.setattr(sdk, "run_session", session)
    monkeypatch.setattr(Evidence, "inspect", inspect)
    monkeypatch.setattr(
        Sandbox,
        "stop_run",
        lambda *_a, **_k: {"remaining": [], "errors": [], "stopped": []},
    )
    try:
        Worker(settings).run(once=True)
    finally:
        for thread in threads:
            thread.join(timeout=12)
    final = store.get(run["id"])
    assert len(locks) == len(deadlines) == len(calls) == 1
    assert final["state"] == "budget_exceeded" and final["first_started_at"] == locks[0]
    assert final["evidence"]["checks"][0]["exit_code"] == 0
    assert final["usage"]["calls"] == 1 and not store.pending(run["id"])
    assert final["usage"]["unknown_calls"] == final["usage"]["reserved_tokens"] == 0
    assert not any(
        event["kind"] in {"completed_verified", "completed_unverified"}
        for event in store.events(run["id"])["events"]
    )


@pytest.mark.postgres
def test_credential_error_is_recorded_after_original_clock_commit(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.config import Settings

    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    calls = scripted_model(monkeypatch, [])
    seen = []

    def invalid(self, *args, **kwargs):
        current = store.get(run["id"])
        assert current["state"] == "running" and current["first_started_at"]
        assert kwargs["deadline"] > time.monotonic()
        seen.append(True)
        raise ValueError("Synthetic invalid credential file")

    monkeypatch.setattr(Settings, "credential", invalid)
    monkeypatch.setattr(
        Sandbox,
        "stop_run",
        lambda *_a, **_k: {"remaining": [], "errors": [], "stopped": []},
    )
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert seen and final["first_started_at"] and final["state"] == "failed"
    assert not calls and not store.pending(run["id"])
    assert final["usage"]["calls"] == final["usage"]["reserved_tokens"] == 0
    assert any(event["kind"] == "failed" for event in store.events(run["id"])["events"])
