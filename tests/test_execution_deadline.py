"""A shared clock reaches SDK restore, hashing, locks and Worker callbacks."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from doonex import sdk
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.sandbox import Sandbox
from doonex.session_store import SessionFileStore, SessionStorageError, canonical_sha256
from doonex.worker import Worker, exception_cause
from test_sdk import harness
from test_worker import prepare_run


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
