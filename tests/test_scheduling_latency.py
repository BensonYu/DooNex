"""Measure real worker priority with PostgreSQL/Docker and fake model transport."""

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import pytest
from doonex.budget import Ledger
from doonex.sdk import GuardedLLM
from doonex.worker import Worker, run_directory
from test_scheduling import action_rows, claimed
from test_scheduling import taskset_source as taskset_fixture
from test_scheduling import tuning as tuning_fixture
from test_worker import prepare_run, response

taskset_source = taskset_fixture
tuning = tuning_fixture
pytestmark = [pytest.mark.postgres, pytest.mark.docker]


@pytest.mark.parametrize("inflight", ["model", "shell"])
def test_foreground_runs_after_only_the_inflight_background_action(
    tuning, store, request_data, tmp_path, monkeypatch, inflight
):
    settings, _, _, batch = claimed(tuning, store)
    background_id = batch["attempts"][0]["run_id"]
    workspace = run_directory(settings, background_id) / "workspace"
    model_entered = threading.Event()
    model_release = threading.Event()
    finished = threading.Event()
    errors = []
    calls = []
    execution_order = []
    foreground = []
    active = []
    checkpoint = []
    timeline = {"running_committed": {}, "model_admitted": {}}
    worker = Worker(settings)
    actual_execute = worker.execute
    actual_create = store.create_run
    actual_locked = worker.store.locked
    actual_reserve = Ledger.reserve

    def create_run(*args, **kwargs):
        # The commit occurs inside this bracket. Database now() is the
        # transaction start, and must not stand in for the commit timestamp.
        before = time.monotonic()
        value = actual_create(*args, **kwargs)
        after = time.monotonic()
        timeline["enqueue_commit_bracket"] = (before, after)
        return value

    @contextmanager
    def locked(run_id, *args, **kwargs):
        with actual_locked(run_id, *args, **kwargs) as (connection, current):
            prior_state = current["state"]
            yield connection, current
        if prior_state == "queued" and current["state"] == "running":
            timeline["running_committed"].setdefault(run_id, time.monotonic())

    def reserve(ledger, *args, **kwargs):
        value = actual_reserve(ledger, *args, **kwargs)
        # Successful reserve returns after its transaction commits. The
        # foreground workspace and SDK session have been prepared by now.
        timeline["model_admitted"].setdefault(ledger.run_id, time.monotonic())
        return value

    def execute(run_id):
        execution_order.append(run_id)
        active[:] = [run_id]
        try:
            actual_execute(run_id)
            if run_id == background_id:
                checkpoint.append(store.get(run_id))
                # A failed first run must not silently advance through siblings.
                if checkpoint[-1]["state"] != "yielded":
                    worker.stopping = True
            elif foreground and run_id == foreground[0]["id"]:
                worker.stopping = True
        finally:
            active.clear()

    def transport(*args, **kwargs):
        assert active, "Model dispatch must belong to the currently executing Run"
        run_id = active[0]
        calls.append(run_id)
        if run_id == background_id:
            assert calls.count(run_id) == 1, "A second background model was dispatched"
            model_entered.set()
            if inflight == "model":
                assert model_release.wait(10), (
                    "Foreground enqueue did not release model"
                )
                time.sleep(1)
            first = response(
                "doonex_shell",
                {
                    "command": "printf started > entered.txt; sleep 2; "
                    "printf x >> completed.txt; printf 'answer = 42\\n' > main.py"
                },
                0,
            )
            second = response(
                "doonex_shell", {"command": "printf extra >> later.txt"}, 1
            )
            first.choices[0].message.tool_calls.extend(
                second.choices[0].message.tool_calls
            )
            return first
        assert foreground and run_id == foreground[0]["id"]
        return response("finish", {"message": "Synthetic foreground complete"}, 2)

    def run_worker():
        try:
            worker.run()
        except BaseException as error:  # noqa: BLE001 - propagate worker thread failures
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(store, "create_run", create_run)
    monkeypatch.setattr(worker.store, "locked", locked)
    monkeypatch.setattr(Ledger, "reserve", reserve)
    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    thread = threading.Thread(target=run_worker)
    thread.start()
    try:
        assert model_entered.wait(15), "Background model transport was not dispatched"
        if inflight == "shell":
            deadline = time.monotonic() + 15
            while not (workspace / "entered.txt").exists():
                assert not finished.is_set(), errors
                assert time.monotonic() < deadline, "Real sleep command did not start"
                time.sleep(0.02)
            assert not (workspace / "completed.txt").exists()
        foreground_run, _, _ = prepare_run(
            store,
            settings,
            request_data,
            tmp_path,
            ["python -c 'assert True'"],
        )
        foreground.append(foreground_run)
        model_release.set()
        assert finished.wait(30), "Foreground did not complete after background yielded"
    finally:
        model_release.set()
        worker.stopping = True
        thread.join(timeout=15)
    assert not thread.is_alive(), "Worker thread did not terminate"
    assert not errors, errors
    assert execution_order == [background_id, foreground_run["id"]]
    assert len(checkpoint) == 1
    paused = checkpoint[0]
    assert paused["state"] == "yielded", paused["reason"]
    assert paused["usage"]["calls"] == 1
    assert paused["usage"]["unknown_calls"] == paused["usage"]["reserved_tokens"] == 0
    assert len(paused["scheduling"]["receipt"]["pending_actions"]) == (
        2 if inflight == "model" else 1
    )
    assert not store.pending(background_id)
    after = store.get(background_id)
    assert after["state"] == "yielded"
    assert after["scheduling"] == paused["scheduling"]
    assert after["usage"] == paused["usage"]
    final = store.get(foreground_run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert calls == [background_id, foreground_run["id"]]
    actions = action_rows(store, background_id)
    assert [item["kind"] for item in actions] == (
        ["model"] if inflight == "model" else ["model", "shell"]
    )
    assert all(item["status"] == "completed" for item in actions)
    for attempt in batch["attempts"][1:]:
        assert not action_rows(store, attempt["run_id"])
    assert not (workspace / "later.txt").exists()
    if inflight == "shell":
        assert (workspace / "completed.txt").read_text() == "x"
    else:
        assert not (workspace / "entered.txt").exists()
        assert not (workspace / "completed.txt").exists()

    foreground_events = store.events(foreground_run["id"])["events"]
    running = next(event for event in foreground_events if event["kind"] == "running")
    background_events = store.events(background_id)["events"]
    yielded = next(
        event for event in background_events if event["kind"] == "scheduling_yielded"
    )
    created_at = datetime.fromisoformat(foreground_run["created_at"])
    action_started_at = datetime.fromisoformat(actions[-1]["data"]["started_at"])
    action_ended_at = datetime.fromisoformat(actions[-1]["data"]["ended_at"])
    yielded_at = datetime.fromisoformat(yielded["created_at"])
    running_at = datetime.fromisoformat(running["created_at"])
    assert action_started_at <= created_at < action_ended_at <= yielded_at <= running_at
    wait_seconds = (running_at - created_at).total_seconds()
    current_action_remaining = (action_ended_at - created_at).total_seconds()
    overhead_seconds = (running_at - action_ended_at).total_seconds()
    # This is a coarse deadlock/queueing guard; the measured value is retained
    # separately and is not a hardware-independent latency promise.
    assert 0 <= overhead_seconds < 10
    assert wait_seconds <= current_action_remaining + 10
    commit_before, commit_after = timeline["enqueue_commit_bracket"]
    running_committed = timeline["running_committed"][foreground_run["id"]]
    first_model_admitted = timeline["model_admitted"][foreground_run["id"]]
    assert commit_before <= commit_after <= running_committed <= first_model_admitted
    # A guard for this finite synthetic fixture through Worker.run, including
    # foreground initialization; it is not the M4 release latency bound.
    assert first_model_admitted - commit_before < 30
    print(
        "scheduling_latency="
        + json.dumps(
            {
                "inflight": inflight,
                "foreground_created_at": created_at.isoformat(),
                "background_action_ended_at": action_ended_at.isoformat(),
                "foreground_running_at": running_at.isoformat(),
                "foreground_wait_seconds": wait_seconds,
                "current_action_remaining_seconds": current_action_remaining,
                "post_action_overhead_seconds": overhead_seconds,
                "background_model_calls": paused["usage"]["calls"],
                "background_shell_actions": sum(
                    action["kind"] == "shell" for action in actions
                ),
                "foreground_state": final["state"],
                "model_transport": "synthetic",
                "enqueue_commit_bracket_monotonic": [commit_before, commit_after],
                "running_commit_confirmed_monotonic": running_committed,
                "first_model_admitted_monotonic": first_model_admitted,
                "submit_to_first_model_seconds": {
                    "lower": first_model_admitted - commit_after,
                    "upper": first_model_admitted - commit_before,
                },
                "running_to_first_model_seconds": first_model_admitted
                - running_committed,
            },
            sort_keys=True,
        )
    )
