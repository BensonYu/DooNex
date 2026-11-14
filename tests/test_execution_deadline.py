"""A shared clock reaches SDK restore, hashing, locks and Worker callbacks."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from doonex import sdk
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.evidence import Evidence
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


def test_actual_sdk_lock_uses_the_same_deadline_and_leaves_no_callbacks(tmp_path):
    kwargs, reservations, settlements, commands, events = harness(tmp_path)
    kwargs["persistence_deadline"] = time.monotonic() + 0.2
    with sdk._RUN_LOCK:
        start = time.monotonic()
        with pytest.raises(SessionStorageError) as raised:
            sdk.run_session(**kwargs)
        assert time.monotonic() - start < 1
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
def test_each_actual_sdk_request_clamps_timeout_after_reservation(
    tmp_path, monkeypatch, mode
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

    def reserve(*args, **options):
        result = before(*args, **options)
        if len(reservations) == 2:
            # Simulate preparation/DB time consuming the original allowance.
            # This happens after admission and before the actual SDK call.
            sdk._SESSION_STORES[kwargs["run_id"]].deadline = time.monotonic() + 2
        return result

    kwargs.update(before_model=reserve, persistence_deadline=time.monotonic() + 30)
    assert sdk.run_session(**kwargs) == "finished"
    assert len(timeouts) == len(reservations) == len(settlements) == 2
    assert timeouts[0] == kwargs["model"]["timeout_seconds"]
    assert 0 < timeouts[1] <= 2 < timeouts[0]
    assert commands == ["echo bounded"]
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
@pytest.mark.parametrize("stage", ["review", "evidence"])
def test_worker_expires_delivery_before_starting_new_work(
    settings, store, request_data, tmp_path, monkeypatch, stage
):
    if stage == "review":
        request_data["workflow"] = "execute_review"
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    calls = scripted_model(monkeypatch, [])
    deadlines = []
    original_session = sdk.run_session
    original_delivery = Review.prepare if stage == "review" else Evidence.capture

    def session(**kwargs):
        deadlines.append(kwargs["persistence_deadline"])
        return original_session(**kwargs)

    def expired_delivery(*args, **kwargs):
        assert kwargs["deadline"] == deadlines[0]
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
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "budget_exceeded"
    assert len(calls) == len(deadlines) == final["usage"]["calls"] == 1
    assert final["usage"]["tokens"] == 120
    assert final["usage"]["unknown_calls"] == final["usage"]["reserved_tokens"] == 0
    assert not store.pending(run["id"]) and not final.get("evidence")
    assert not (settings.home / "runs" / run["id"] / stage).exists()
