"""Offline checks for explicit Arena dispatch and the shared campaign allowance."""

import copy
import fcntl
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from doonex.worker import WORKER_LOCK

from scripts import live_arena


@pytest.fixture
def harness(tmp_path, monkeypatch):
    evaluation_id = str(uuid4())
    run_ids = [str(uuid4()), str(uuid4()), str(uuid4())]
    unrelated = str(uuid4())
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    previous = {
        "reserved_calls": 17,
        "reserved_tokens": 12345,
        "authorization": {
            "mode": "subscription_uncapped",
            "max_calls": None,
            "max_tokens": None,
        },
        "authorization_history": [{"recorded_calls": 10, "at": "old-permission"}],
        "runs": [
            {"attempt_id": "old-failed", "status": "failed", "unknown_calls": 1},
            {"attempt_id": "old-unknown", "status": "reserved"},
        ],
    }
    path = home / "live-acceptance.json"
    path.write_text(json.dumps(previous))
    evaluation = {
        "id": evaluation_id,
        "state": "queued",
        "model": {
            "model_id": "openai/offline-arena-fixture",
            "billing_mode": "subscription",
        },
        "batch_budget": {"max_calls": 8, "max_tokens": 90000},
        "attempts": [{"run_id": run_id} for run_id in run_ids],
        "usage": {"calls": 0, "tokens": 0, "reserved_tokens": 0, "unknown_calls": 0},
        "metrics": {"complete": False},
        "prompt": "PRIVATE-TASK-MATERIAL",
        "reason": "PRIVATE-RAW-REASON",
    }
    value = SimpleNamespace(
        path=path,
        previous=previous,
        evaluation=evaluation,
        run_ids=run_ids,
        unrelated=unrelated,
        runs={run_id: {"state": "queued"} for run_id in [*run_ids, unrelated]},
        executed=[],
        initialized=False,
        connected=False,
        acquired=True,
        credential=True,
        on_execute=None,
        detail_error=None,
        detail_calls=0,
        credential_models=[],
    )

    class Connection:
        autocommit = False

        def execute(self, statement, parameters):
            assert statement == "SELECT pg_try_advisory_lock(%s) AS acquired"
            assert parameters == (WORKER_LOCK,)
            assert self.autocommit
            return SimpleNamespace(fetchone=lambda: {"acquired": value.acquired})

    class Store:
        def initialize(self):
            value.initialized = True

        @contextmanager
        def connect(self):
            value.connected = True
            try:
                yield connection
            finally:
                value.connected = False

        def get(self, run_id):
            return copy.deepcopy(value.runs[run_id])

    def credential(model):
        value.credential_models.append(copy.deepcopy(model))
        return "OFFLINE-SECRET-CREDENTIAL" if value.credential else None

    settings = SimpleNamespace(home=home, credential=credential)
    store = Store()
    connection = Connection()

    def assert_locks():
        assert value.connected
        with (
            (home / "live-acceptance.lock").open("a+") as other,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    class Worker:
        def __init__(self, configured):
            assert configured is settings
            self.store = store
            self.lock_connection = None

        def execute(self, run_id):
            assert_locks()
            assert self.lock_connection is connection
            assert run_id in run_ids
            assert value.runs[run_id]["state"] == "queued"
            campaign = json.loads(path.read_text())
            assert campaign["reserved_calls"] == previous["reserved_calls"] + 8
            assert campaign["reserved_tokens"] == previous["reserved_tokens"] + 90000
            assert campaign["runs"][-1]["status"] == "reserved"
            assert campaign["runs"][-1]["evaluation_id"] == evaluation_id
            value.executed.append(run_id)
            if value.on_execute:
                value.on_execute(run_id)
            else:
                value.runs[run_id]["state"] = "failed"
                evaluation["state"] = "running"
                if all(value.runs[item]["state"] == "failed" for item in run_ids):
                    evaluation.update(state="complete", metrics={"complete": True})
                    evaluation["usage"].update(
                        calls=3, tokens=123, reserved_tokens=456, unknown_calls=1
                    )

        def reconcile(self):
            pytest.fail("The harness must not reconcile or execute unrelated runs")

    class Arena:
        def __init__(self, configured, configured_store):
            assert configured is settings and configured_store is store

        def get(self, identifier):
            assert identifier == evaluation_id
            assert_locks()
            return copy.deepcopy(evaluation)

        def detail(self, identifier):
            assert identifier == evaluation_id
            assert_locks()
            value.detail_calls += 1
            if value.detail_error:
                raise value.detail_error
            return copy.deepcopy(evaluation)

    monkeypatch.setattr(live_arena.Settings, "load", lambda: settings)
    monkeypatch.setattr(live_arena, "Worker", Worker)
    monkeypatch.setattr(live_arena, "Arena", Arena)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_arena.py",
            "--confirm-model-usage",
            "--evaluation-id",
            evaluation_id,
            "--model",
            evaluation["model"]["model_id"],
        ],
    )
    value.settings = settings
    value.assert_locks = assert_locks
    return value


def rejected(harness, capsys):
    original = harness.path.read_bytes() if harness.path.exists() else None
    with pytest.raises(SystemExit) as error:
        live_arena.main()
    assert error.value.code == 1
    assert harness.executed == []
    assert (harness.path.read_bytes() if harness.path.exists() else None) == original
    return json.loads(capsys.readouterr().out)


def retained(harness):
    campaign = json.loads(harness.path.read_text())
    assert campaign["reserved_calls"] == harness.previous["reserved_calls"] + 8
    assert campaign["reserved_tokens"] == harness.previous["reserved_tokens"] + 90000
    assert campaign["runs"][:-1] == harness.previous["runs"]
    assert (
        campaign["authorization_history"] == harness.previous["authorization_history"]
    )
    assert campaign["authorization"] == harness.previous["authorization"]
    return campaign["runs"][-1]


def test_only_frozen_queued_children_execute_and_old_failed_history_is_preserved(
    harness, capsys
):
    live_arena.main()
    summary = json.loads(capsys.readouterr().out)
    campaign = json.loads(harness.path.read_text())
    assert harness.initialized
    assert harness.executed == harness.run_ids
    assert harness.runs[harness.unrelated]["state"] == "queued"
    assert all(harness.runs[item]["state"] == "failed" for item in harness.run_ids)
    assert summary["state"] == "complete" and summary["metrics_complete"] is True
    assert summary["status"] == "settled"
    assert campaign["reserved_calls"] == harness.previous["reserved_calls"] + 3
    assert (
        campaign["reserved_tokens"] == harness.previous["reserved_tokens"] + 123 + 456
    )
    assert campaign["runs"][:-1] == harness.previous["runs"]
    assert (
        campaign["authorization_history"] == harness.previous["authorization_history"]
    )
    assert campaign["authorization"] == harness.previous["authorization"]
    assert harness.credential_models == [harness.evaluation["model"]]
    assert not harness.connected
    assert "PRIVATE" not in json.dumps(summary)


@pytest.mark.parametrize("status", ["reserved", "failed", "settled"])
def test_duplicate_evaluation_is_refused_regardless_of_attempt_status(
    harness, capsys, status
):
    campaign = copy.deepcopy(harness.previous)
    campaign["runs"].append(
        {"evaluation_id": harness.evaluation["id"].upper(), "status": status}
    )
    harness.path.write_text(json.dumps(campaign))
    assert rejected(harness, capsys)["reason"] == "evaluation_already_attempted"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("wrong_model", "confirmed_model_mismatch"),
        ("missing_credential", "credential_unavailable"),
        ("metered_model", "subscription_billing_required"),
        ("worker_locked", "worker_locked"),
        ("nonqueued", "queued_evaluation_required"),
    ],
)
def test_preflight_refuses_without_allocating(harness, capsys, change, reason):
    if change == "wrong_model":
        harness.evaluation["model"]["model_id"] = "unconfirmed-model"
    elif change == "missing_credential":
        harness.credential = False
    elif change == "metered_model":
        harness.evaluation["model"]["billing_mode"] = "metered"
    elif change == "worker_locked":
        harness.acquired = False
    else:
        harness.evaluation["state"] = "running"
    assert rejected(harness, capsys)["reason"] == reason


@pytest.mark.parametrize("mode", [None, "capped", "metered"])
def test_uncapped_authorization_must_already_exist(harness, capsys, mode):
    campaign = copy.deepcopy(harness.previous)
    if mode is None:
        del campaign["authorization"]
    else:
        campaign["authorization"]["mode"] = mode
    harness.path.write_text(json.dumps(campaign))
    assert rejected(harness, capsys)["reason"] == "subscription_authorization_required"


@pytest.mark.parametrize(
    "state", ["interrupted", "cancelling", "complete", "cancelled", "budget_exceeded"]
)
def test_finished_or_interrupted_evaluations_cannot_be_retried(harness, capsys, state):
    harness.evaluation["state"] = state
    assert rejected(harness, capsys)["reason"] == "queued_evaluation_required"


def test_missing_campaign_is_never_recreated(harness, capsys):
    harness.path.unlink()
    assert rejected(harness, capsys)["reason"] == "existing_campaign_required"


def test_campaign_flock_refuses_before_allocation(harness, capsys):
    with (harness.settings.home / "live-acceptance.lock").open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rejected(harness, capsys)["reason"] == "campaign_locked"
    assert not harness.initialized


@pytest.mark.parametrize("state", ["interrupted", "running", "queued"])
def test_partial_nonterminal_evaluation_retains_entire_allocation(
    harness, capsys, state
):
    def partial(run_id):
        harness.runs[run_id]["state"] = "paused"
        harness.evaluation["state"] = state
        harness.evaluation["usage"].update(calls=1, tokens=5, reserved_tokens=700)

    harness.on_execute = partial
    with pytest.raises(SystemExit) as error:
        live_arena.main()
    assert error.value.code == 1
    summary = json.loads(capsys.readouterr().out)
    attempt = retained(harness)
    assert attempt["status"] == summary["status"] == "nonterminal_allowance_retained"
    if state == "interrupted":
        assert harness.executed == harness.run_ids[:1]
    assert all(harness.runs[item]["state"] != "queued" for item in harness.executed)


def test_nonqueued_children_are_skipped_without_retry_or_recovery(harness, capsys):
    harness.runs[harness.run_ids[0]]["state"] = "paused"
    harness.runs[harness.run_ids[1]]["state"] = "interrupted"
    with pytest.raises(SystemExit):
        live_arena.main()
    capsys.readouterr()
    assert harness.executed == harness.run_ids[2:]
    assert harness.runs[harness.run_ids[0]]["state"] == "paused"
    assert harness.runs[harness.run_ids[1]]["state"] == "interrupted"
    retained(harness)


@pytest.mark.parametrize("interrupt", [False, True])
def test_execution_exception_keeps_allocation_and_prints_no_private_text(
    harness, capsys, interrupt
):
    def fail(run_id):
        harness.evaluation["usage"].update(calls=1, tokens=42, reserved_tokens=700)
        if interrupt:
            raise KeyboardInterrupt("PRIVATE-EXCEPTION")
        raise RuntimeError("PRIVATE-EXCEPTION OFFLINE-SECRET-CREDENTIAL")

    harness.on_execute = fail
    with pytest.raises(SystemExit) as error:
        live_arena.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert "PRIVATE" not in output.out + output.err
    assert "CREDENTIAL" not in output.out + output.err
    assert harness.executed == harness.run_ids[:1]
    attempt = retained(harness)
    assert (
        attempt["status"]
        == summary["status"]
        == (
            "interrupted_allowance_retained"
            if interrupt
            else "exception_allowance_retained"
        )
    )


def test_exception_after_execution_does_not_settle_even_a_terminal_batch(
    harness, capsys
):
    harness.detail_error = RuntimeError("PRIVATE-REPORT-FAILURE")
    with pytest.raises(SystemExit):
        live_arena.main()
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert harness.evaluation["state"] == "complete"
    assert retained(harness)["status"] == "exception_allowance_retained"


@pytest.mark.parametrize("state", ["complete", "cancelled", "budget_exceeded"])
def test_terminal_incomplete_or_cancelled_batch_settles_but_exits_failure(
    harness, capsys, state
):
    def finish(run_id):
        harness.evaluation.update(state=state, metrics={"complete": False})
        harness.evaluation["usage"].update(
            calls=1, tokens=50, reserved_tokens=600, unknown_calls=1
        )

    harness.on_execute = finish
    with pytest.raises(SystemExit) as error:
        live_arena.main()
    assert error.value.code == 1
    summary = json.loads(capsys.readouterr().out)
    campaign = json.loads(harness.path.read_text())
    assert summary["metrics_complete"] is False
    assert summary["status"] == "settled"
    assert harness.executed == harness.run_ids[:1]
    assert campaign["reserved_calls"] == harness.previous["reserved_calls"] + 1
    assert campaign["reserved_tokens"] == harness.previous["reserved_tokens"] + 50 + 600


def test_initial_persistence_failure_never_executes(harness, capsys, monkeypatch):
    def fail_save(path, campaign):
        raise OSError("PRIVATE-DISK-ERROR")

    monkeypatch.setattr(live_arena, "save_campaign", fail_save)
    assert rejected(harness, capsys)["reason"] == "preparation_failed"


def test_failed_final_save_restores_full_allocation(harness, capsys, monkeypatch):
    save = live_arena.save_campaign
    attempts = 0

    def fail_once(path, campaign):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("PRIVATE-DISK-ERROR")
        harness.assert_locks()
        save(path, campaign)

    monkeypatch.setattr(live_arena, "save_campaign", fail_once)
    with pytest.raises(SystemExit):
        live_arena.main()
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert retained(harness)["status"] == "exception_allowance_retained"


def test_failed_status_updates_leave_the_first_persisted_reservation(
    harness, capsys, monkeypatch
):
    save = live_arena.save_campaign
    attempts = 0

    def fail_after_reservation(path, campaign):
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise OSError("PRIVATE-DISK-ERROR")
        save(path, campaign)

    monkeypatch.setattr(live_arena, "save_campaign", fail_after_reservation)
    with pytest.raises(SystemExit):
        live_arena.main()
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert json.loads(output.out)["campaign_record_updated"] is False
    assert retained(harness)["status"] == "reserved"


def test_private_configuration_exception_is_safely_refused(
    harness, capsys, monkeypatch
):
    def fail_load():
        raise ValueError("PRIVATE-CONFIGURATION OFFLINE-SECRET-CREDENTIAL")

    monkeypatch.setattr(live_arena.Settings, "load", fail_load)
    assert rejected(harness, capsys)["reason"] == "preparation_failed"


def test_missing_confirmation_cannot_dispatch(harness, capsys, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", [arg for arg in sys.argv if arg != "--confirm-model-usage"]
    )
    with pytest.raises(SystemExit) as error:
        live_arena.main()
    assert error.value.code == 2
    assert harness.executed == []
    assert json.loads(harness.path.read_text()) == harness.previous
    capsys.readouterr()
