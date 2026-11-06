"""Offline safety checks for the explicit background acceptance harness."""

import copy
import fcntl
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from doonex.background import Background as RealBackground
from doonex.config import Settings
from doonex.sdk import GuardedLLM
from doonex.worker import WORKER_LOCK
from doonex.worker import Worker as RealWorker
from test_scheduling import tuning as tuning_fixture
from test_tasksets import taskset_source as full_taskset_fixture
from test_worker import prepare_run, response

from scripts import live_background

taskset_source = full_taskset_fixture
tuning = tuning_fixture


def usage(calls=0, tokens=0, reserved_tokens=0, unknown_calls=0):
    return {
        "calls": calls,
        "tokens": tokens,
        "reserved_tokens": reserved_tokens,
        "unknown_calls": unknown_calls,
    }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    previous = {
        "reserved_calls": 17,
        "reserved_tokens": 12345,
        "authorization": {"mode": "subscription_uncapped"},
        "authorization_history": [{"at": "historical-permission"}],
        "runs": [
            {"attempt_id": "old-failed", "status": "failed", "unknown_calls": 1},
            {"attempt_id": "old-unknown", "status": "reserved"},
        ],
    }
    path = home / "live-acceptance.json"
    path.write_text(json.dumps(previous))
    authorization_id, foreground_id, evaluation_id, unrelated = (
        str(uuid4()) for _ in range(4)
    )
    run_ids = [str(uuid4()), str(uuid4())]
    model = {
        "model_id": "openai/offline-background-fixture",
        "billing_mode": "subscription",
        "max_input_tokens": 32768,
        "max_output_tokens": 1024,
    }
    budget = {"max_calls": 2, "max_tokens": 40000, "max_seconds": 120}
    grant = {
        "id": authorization_id,
        "state": "enabled",
        "evaluation_id": None,
        "revision": "f" * 64,
        "snapshot": {"model": model},
        "input": {
            "taskset_id": "a" * 64,
            "skill_version_ids": ["b" * 64, "c" * 64],
            "model_profile_id": "offline",
            "repeats": 1,
            "run_budget": budget,
            "batch_budget": {
                "max_calls": 8,
                "max_tokens": 90000,
                "max_seconds": 600,
            },
        },
    }
    foreground = {
        "id": foreground_id,
        "state": "paused",
        "first_started_at": None,
        "phase": "execution",
        "created_at": datetime.now(UTC).isoformat(),
        "request": {
            "model": copy.deepcopy(model),
            "workflow": "execute",
            "budget": copy.deepcopy(budget),
            "prompt": "PRIVATE-FOREGROUND-MATERIAL",
        },
        "usage": usage(),
    }
    value = SimpleNamespace(
        path=path,
        previous=previous,
        grant=grant,
        foreground=foreground,
        run_ids=run_ids,
        unrelated=unrelated,
        evaluation={
            "id": evaluation_id,
            "state": "queued",
            "attempts": [{"run_id": run_id} for run_id in run_ids],
            "usage": usage(),
            "metrics": {"complete": False},
            "integrity": {"valid": True},
        },
        runs={
            foreground_id: foreground,
            unrelated: {"id": unrelated, "state": "completed_verified"},
        },
        actions={},
        events=[],
        executed=[],
        enabled=[],
        disabled=[],
        resumed=[],
        settled=[],
        stopped=[],
        model_reservations=[],
        connected=0,
        initialized=False,
        acquired=True,
        credential=True,
        unknown_termination=False,
        containers=False,
        unrelated_batch=False,
        on_execute=None,
        after_execute=None,
        prepare_error=None,
        claim_error=None,
        detail_error=None,
        preflight_error=None,
        settle_error=None,
        claim_connection=None,
        enable_committed_error=None,
        attach_committed_error=None,
        detail_mutation=None,
    )

    def assert_locks():
        assert value.connected
        with (
            (home / "live-acceptance.lock").open("a+") as other,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def allowance():
        assert_locks()
        campaign = json.loads(path.read_text())
        assert campaign["reserved_calls"] == previous["reserved_calls"] + 10
        assert campaign["reserved_tokens"] == previous["reserved_tokens"] + 130000
        assert campaign["runs"][:-1] == previous["runs"]
        return campaign["runs"][-1]

    class Connection:
        autocommit = False

        def execute(self, statement, parameters=None):
            if statement == "SELECT pg_try_advisory_lock(%s) AS acquired":
                assert parameters == (WORKER_LOCK,) and self.autocommit
                return SimpleNamespace(fetchone=lambda: {"acquired": value.acquired})
            if statement == "SELECT id,state FROM runs ORDER BY id":
                rows = [
                    {"id": UUID(item["id"]), "state": item["state"]}
                    for item in value.runs.values()
                ]
                return SimpleNamespace(fetchall=lambda: rows)
            if statement == "SELECT 1 FROM actions WHERE run_id=%s LIMIT 1":
                rows = [
                    item
                    for item in value.actions.values()
                    if item["run_id"] == parameters[0]
                ]
                return SimpleNamespace(fetchone=lambda: rows[0] if rows else None)
            if statement == "SELECT kind,status FROM actions WHERE id=%s AND run_id=%s":
                item = value.actions.get(parameters[0])
                row = (
                    {key: item[key] for key in ("kind", "status")}
                    if item and item["run_id"] == parameters[1]
                    else None
                )
                return SimpleNamespace(fetchone=lambda: row)
            if (
                statement
                == "SELECT id,kind,status FROM actions WHERE run_id=ANY(%s::uuid[])"
            ):
                rows = [
                    {
                        "id": UUID(identifier),
                        "kind": item["kind"],
                        "status": item["status"],
                    }
                    for identifier, item in value.actions.items()
                    if item["run_id"] in parameters[0]
                ]
                return SimpleNamespace(fetchall=lambda: rows)
            if statement == "SELECT data FROM arena_evaluations WHERE id=%s FOR UPDATE":
                assert parameters == (evaluation_id,)
                return SimpleNamespace(fetchone=lambda: {"data": value.evaluation})
            pytest.fail(f"Unexpected SQL: {statement}")

    connection = Connection()

    class Store:
        database_url = "offline-database"

        def initialize(self):
            value.initialized = True

        @contextmanager
        def connect(self):
            value.connected += 1
            try:
                yield connection
            finally:
                value.connected -= 1

        @contextmanager
        def locked(self, identifier, *, dispatch=False):
            assert dispatch
            with self.connect() as c:
                current = copy.deepcopy(value.runs[identifier])
                yield c, current
                value.runs[identifier].update(current)

        def get(self, identifier):
            return copy.deepcopy(value.runs[identifier])

        def pending(self, identifier):
            return [
                item
                for item in value.actions.values()
                if item["run_id"] == identifier
                and item["status"] in {"pending", "unknown"}
            ]

        def event(self, identifier, kind, payload, c):
            assert c is connection
            value.events.append(
                {
                    "run_id": identifier,
                    "kind": kind,
                    "payload": copy.deepcopy(payload),
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )

        def events(self, identifier):
            return {"events": [e for e in value.events if e["run_id"] == identifier]}

    store = Store()
    settings = SimpleNamespace(
        home=home,
        credential=lambda selected: "OFFLINE-PRIVATE-KEY" if value.credential else None,
    )

    class Ledger:
        def reserve(self, *_args, **_kwargs):
            allowance()
            identifier = str(uuid4())
            value.actions[identifier] = {
                "run_id": self.run_id,
                "kind": "model",
                "status": "pending",
            }
            value.model_reservations.append(identifier)
            return {"id": identifier}

    def model_action(identifier, tokens):
        reservation = live_background.Ledger.reserve(
            SimpleNamespace(run_id=identifier, store=store), 100, 100
        )
        value.actions[reservation["id"]]["status"] = "completed"
        value.runs[identifier]["usage"] = usage(1, tokens)

    def sync():
        value.evaluation["usage"] = {
            key: sum(value.runs[item]["usage"][key] for item in run_ids)
            for key in usage()
        }
        if all(
            value.runs[item]["state"] in live_background.TERMINAL_RUNS
            for item in run_ids
        ):
            value.evaluation.update(state="complete", metrics={"complete": True})

    class Scheduler:
        def resume_yielded(self, run, workspace):
            assert value.runs[foreground_id]["state"] == "completed_verified"
            assert run["id"] == run_ids[0] and run["state"] == "yielded"
            value.resumed.append(run["id"])
            value.runs[run["id"]]["state"] = "queued"
            return True

    class Worker:
        def __init__(self, configured):
            assert configured is settings
            self.settings, self.store = settings, store
            self.scheduler = Scheduler()
            self.lock_connection = None

        def execute(self, identifier):
            assert_locks()
            assert self.lock_connection is connection
            assert value.runs[identifier]["state"] == "queued"
            value.executed.append(identifier)
            if value.on_execute and value.on_execute(identifier):
                return
            if identifier == run_ids[0] and identifier not in value.resumed:
                assert value.runs[foreground_id]["state"] == "paused"
                model_action(identifier, 10)
                assert value.runs[foreground_id]["state"] == "queued"
                value.runs[identifier]["state"] = "yielded"
                value.evaluation["state"] = "running"
            else:
                if identifier == foreground_id:
                    assert live_background.Ledger.reserve is Ledger.reserve
                    store.event(identifier, "running", {}, connection)
                    value.runs[identifier]["first_started_at"] = datetime.now(
                        UTC
                    ).isoformat()
                    model_action(identifier, 7)
                elif identifier != run_ids[0]:
                    model_action(identifier, 11)
                value.runs[identifier]["state"] = "completed_verified"
            if value.after_execute:
                value.after_execute(identifier)
            sync()

        def run(self):
            pytest.fail("The harness must never consume the general queue")

        def reconcile(self):
            pytest.fail("The harness must never recover existing runs")

    class Arena:
        def __init__(self, configured, configured_store):
            assert configured is settings and configured_store is store

        def list(self):
            result = [value.evaluation] if run_ids[0] in value.runs else []
            if value.unrelated_batch:
                result.append({"id": unrelated, "state": "running"})
            return copy.deepcopy(result)

        def prepare(self, body):
            assert body.taskset_id == grant["input"]["taskset_id"]
            return "offline-prepared-plan"

        def create(self, body, *, connection, background, prepared):
            allowance()
            assert connection is value.claim_connection
            assert background["authorization_id"] == authorization_id
            assert prepared == "offline-prepared-plan"
            for identifier in run_ids:
                value.runs[identifier] = {
                    "id": identifier,
                    "state": "queued",
                    "usage": usage(),
                }
            return copy.deepcopy(value.evaluation)

        def sync(self, identifier):
            assert identifier == evaluation_id
            sync()

        def get(self, identifier):
            assert identifier == evaluation_id
            return copy.deepcopy(value.evaluation)

        def stop(self, identifier, state, reason):
            assert identifier == evaluation_id
            value.stopped.append(state)
            if value.evaluation["state"] in live_background.TERMINAL:
                return
            value.evaluation["state"] = (
                "interrupted" if state == "interrupted" else "cancelled"
            )
            if state == "cancelling":
                for run_id in run_ids:
                    if value.runs[run_id]["state"] in {"queued", "yielded"}:
                        value.runs[run_id]["state"] = "cancelled"

        def detail(self, identifier):
            assert identifier == evaluation_id
            if value.detail_error:
                raise value.detail_error
            if value.detail_mutation:
                value.detail_mutation()
            return copy.deepcopy(value.evaluation)

    class Background:
        def __init__(self, configured, configured_store):
            assert configured is settings and configured_store is store

        def get(self, identifier):
            assert identifier == authorization_id
            return copy.deepcopy(grant)

        def prepare(self, identifier):
            assert identifier == authorization_id
            if value.prepare_error:
                raise value.prepare_error
            return "offline-background-checked"

        def enable(self, identifier, revision):
            attempt = allowance()
            assert attempt["enable_authorization"] is True
            assert identifier == authorization_id and revision == grant["revision"]
            assert grant["state"] == "draft"
            value.enabled.append(identifier)
            grant.update(state="enabled", enabled_at=datetime.now(UTC).isoformat())
            if value.enable_committed_error:
                raise value.enable_committed_error
            return copy.deepcopy(grant)

        def disable(self, identifier, revision):
            allowance()
            assert identifier == authorization_id and revision == grant["revision"]
            assert grant["state"] == "enabled" and grant["evaluation_id"] is None
            value.disabled.append(identifier)
            grant["state"] = "disabled"

        def claim(self, c, identifier, *, prepared):
            allowance()
            if value.claim_error:
                raise value.claim_error
            assert identifier == authorization_id and grant["state"] == "enabled"
            assert prepared == "offline-background-checked"
            value.claim_connection = c
            grant["state"] = "consumed"
            return {
                "authorization_id": authorization_id,
                "date": "2026-11-06",
                "reservation_id": str(uuid4()),
            }

        def attach(self, c, claim, evaluation):
            assert c is value.claim_connection
            assert evaluation["id"] == evaluation_id
            grant["evaluation_id"] = evaluation_id
            if value.attach_committed_error:
                raise value.attach_committed_error

        def preflight(self, c, identifier, batch_id):
            assert identifier == authorization_id and batch_id == evaluation_id
            if value.preflight_error:
                raise value.preflight_error

        def settle(self, c, identifier, batch):
            if value.settle_error:
                raise value.settle_error
            assert identifier == evaluation_id and batch is value.evaluation
            assert batch["state"] in live_background.TERMINAL
            assert batch["usage"]["unknown_calls"] == 0
            value.settled.append(identifier)
            return {"settled_at": datetime.now(UTC).isoformat()}

    class Sandbox:
        def _docker(self, *args):
            assert args == ("ps", "-aq", "--filter", "label=doonex.run_id")
            return SimpleNamespace(stdout=b"residual\n" if value.containers else b"")

    monkeypatch.setattr(live_background.Settings, "load", lambda: settings)
    monkeypatch.setattr(live_background, "Worker", Worker)
    monkeypatch.setattr(live_background, "Ledger", Ledger)
    monkeypatch.setattr(live_background, "Arena", Arena)
    monkeypatch.setattr(live_background, "Background", Background)
    monkeypatch.setattr(live_background, "Sandbox", Sandbox)
    monkeypatch.setattr(
        live_background, "get_workspace", lambda *_: "offline-workspace"
    )
    monkeypatch.setattr(live_background, "dispatch_lock", lambda c: assert_locks())
    monkeypatch.setattr(
        live_background, "termination_unknown", lambda c: value.unknown_termination
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_background.py",
            "--confirm-model-usage",
            "--authorization-id",
            authorization_id,
            "--foreground-run-id",
            foreground_id,
            "--model",
            model["model_id"],
        ],
    )
    value.settings, value.store, value.ledger = settings, store, Ledger
    value.assert_locks = assert_locks
    return value


def rejected(harness, capsys):
    previous = harness.path.read_bytes() if harness.path.exists() else None
    with pytest.raises(SystemExit) as error:
        live_background.main()
    assert error.value.code == 1
    assert harness.executed == harness.enabled == harness.disabled == []
    assert (harness.path.read_bytes() if harness.path.exists() else None) == previous
    return json.loads(capsys.readouterr().out)


def retained(harness):
    campaign = json.loads(harness.path.read_text())
    assert campaign["reserved_calls"] == harness.previous["reserved_calls"] + 10
    assert campaign["reserved_tokens"] == harness.previous["reserved_tokens"] + 130000
    assert campaign["runs"][:-1] == harness.previous["runs"]
    assert campaign["authorization"] == harness.previous["authorization"]
    assert (
        campaign["authorization_history"] == harness.previous["authorization_history"]
    )
    return campaign["runs"][-1]


@pytest.mark.parametrize("enable", [False, True])
def test_only_selected_batch_and_foreground_execute_after_full_campaign_reservation(
    harness, capsys, monkeypatch, enable
):
    if enable:
        harness.grant["state"] = "draft"
        monkeypatch.setattr(sys, "argv", [*sys.argv, "--enable-authorization"])
    original_reserve = harness.ledger.reserve
    live_background.main()
    summary = json.loads(capsys.readouterr().out)
    assert harness.ledger.reserve is original_reserve
    assert harness.executed == [
        harness.run_ids[0],
        harness.foreground["id"],
        *harness.run_ids,
    ]
    assert harness.resumed == harness.run_ids[:1]
    assert harness.settled == [harness.evaluation["id"]]
    assert harness.enabled == ([harness.grant["id"]] if enable else [])
    assert harness.disabled == []
    assert summary["status"] == "settled" and summary["state"] == "complete"
    assert summary["metrics_complete"] is True
    assert summary["foreground_state"] == "completed_verified"
    assert summary["foreground_queue_to_running_seconds"] >= 0
    assert harness.runs[harness.unrelated]["state"] == "completed_verified"
    campaign = json.loads(harness.path.read_text())
    assert campaign["reserved_calls"] == harness.previous["reserved_calls"] + 3
    assert campaign["reserved_tokens"] == harness.previous["reserved_tokens"] + 28
    assert campaign["runs"][:-1] == harness.previous["runs"]
    assert (
        campaign["authorization_history"] == harness.previous["authorization_history"]
    )
    assert campaign["authorization"] == harness.previous["authorization"]
    assert "PRIVATE" not in json.dumps(summary)
    assert not harness.connected


@pytest.mark.parametrize("field", ["authorization_id", "foreground_run_id", "run_id"])
@pytest.mark.parametrize("status", ["reserved", "failed", "settled"])
def test_prior_attempt_is_never_replayed(harness, capsys, field, status):
    campaign = copy.deepcopy(harness.previous)
    identifier = (
        harness.grant["id"] if field == "authorization_id" else harness.foreground["id"]
    )
    campaign["runs"].append({field: identifier.upper(), "status": status})
    harness.path.write_text(json.dumps(campaign))
    reason = rejected(harness, capsys)["reason"]
    assert reason == (
        "authorization_already_attempted"
        if field == "authorization_id"
        else "foreground_already_attempted"
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("model", "confirmed_model_mismatch"),
        ("credential", "credential_unavailable"),
        ("metered", "subscription_billing_required"),
        ("worker", "worker_locked"),
        ("consumed", "unconsumed_enabled_authorization_required"),
        ("draft", "unconsumed_enabled_authorization_required"),
        ("queued_foreground", "pristine_paused_foreground_required"),
        ("started_foreground", "pristine_paused_foreground_required"),
        ("requested_action", "pristine_paused_foreground_required"),
        ("review", "pristine_paused_foreground_required"),
        ("arena", "pristine_paused_foreground_required"),
        ("unknown_foreground", "pristine_paused_foreground_required"),
        ("different_foreground_model", "foreground_model_configuration_mismatch"),
        ("prior_action", "foreground_already_dispatched"),
        ("directory", "foreground_execution_directory_exists"),
        ("unrelated_run", "unrelated_active_run"),
        ("unrelated_batch", "unrelated_active_evaluation"),
        ("container", "existing_task_container"),
        ("unknown_container", "unknown_container_termination"),
    ],
)
def test_preflight_refuses_without_enabling_or_reserving(
    harness, capsys, change, reason
):
    if change == "model":
        harness.grant["snapshot"]["model"]["model_id"] = "unconfirmed"
    elif change == "credential":
        harness.credential = False
    elif change == "metered":
        harness.grant["snapshot"]["model"]["billing_mode"] = "metered"
    elif change == "worker":
        harness.acquired = False
    elif change == "consumed":
        harness.grant["evaluation_id"] = str(uuid4())
    elif change == "draft":
        harness.grant["state"] = "draft"
    elif change == "queued_foreground":
        harness.foreground["state"] = "queued"
    elif change == "started_foreground":
        harness.foreground["first_started_at"] = "historically-started"
    elif change == "requested_action":
        harness.foreground["requested_action"] = "pause"
    elif change == "review":
        harness.foreground["request"]["workflow"] = "execute_review"
    elif change == "arena":
        harness.foreground["request"]["arena"] = {"evaluation_id": str(uuid4())}
    elif change == "unknown_foreground":
        harness.foreground["usage"]["unknown_calls"] = 1
    elif change == "different_foreground_model":
        harness.foreground["request"]["model"]["max_output_tokens"] += 1
    elif change == "prior_action":
        harness.actions[str(uuid4())] = {
            "run_id": harness.foreground["id"],
            "kind": "model",
            "status": "completed",
        }
    elif change == "directory":
        (harness.settings.home / "runs" / harness.foreground["id"]).mkdir(parents=True)
    elif change == "unrelated_run":
        harness.runs[harness.unrelated]["state"] = "queued"
    elif change == "unrelated_batch":
        harness.unrelated_batch = True
    elif change == "container":
        harness.containers = True
    else:
        harness.unknown_termination = True
    assert rejected(harness, capsys)["reason"] == reason


@pytest.mark.parametrize("interrupt", [False, True])
def test_provider_exception_retains_full_allowance_and_restores_reserve(
    harness, capsys, interrupt
):
    original = harness.ledger.reserve

    def fail(identifier):
        if interrupt:
            raise KeyboardInterrupt("PRIVATE-PROVIDER-KEY")
        raise RuntimeError("PRIVATE-PROVIDER-KEY")

    harness.on_execute = fail
    with pytest.raises(SystemExit) as error:
        live_background.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    summary = json.loads(output.out)
    assert (
        retained(harness)["status"]
        == summary["status"]
        == (
            "interrupted_allowance_retained"
            if interrupt
            else "exception_allowance_retained"
        )
    )
    assert harness.ledger.reserve is original
    assert harness.executed == harness.run_ids[:1]
    assert not harness.resumed and not harness.settled
    assert harness.stopped == ["cancelling"]
    assert harness.runs[harness.foreground["id"]]["state"] == "cancelled"


@pytest.mark.parametrize("unknown", ["foreground", "background", "pending"])
def test_unknown_usage_or_actions_never_settle_or_resume(harness, capsys, unknown):
    def damage(identifier):
        if identifier != harness.foreground["id"]:
            return
        if unknown == "foreground":
            harness.runs[identifier]["usage"]["unknown_calls"] = 1
        elif unknown == "background":
            harness.runs[harness.run_ids[0]]["usage"]["unknown_calls"] = 1
        else:
            harness.actions[str(uuid4())] = {
                "run_id": identifier,
                "kind": "shell",
                "status": "unknown",
            }

    harness.after_execute = damage
    with pytest.raises(SystemExit):
        live_background.main()
    capsys.readouterr()
    retained(harness)
    assert not harness.settled
    assert harness.stopped == (
        ["interrupted"] if unknown == "background" else ["cancelling"]
    )
    if unknown != "background":
        assert not harness.resumed


def test_expired_or_revoked_authorization_after_foreground_is_not_reauthorized(
    harness, capsys
):
    harness.preflight_error = ValueError("PRIVATE-EXPIRED-GRANT")
    with pytest.raises(SystemExit):
        live_background.main()
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert harness.executed == [harness.run_ids[0], harness.foreground["id"]]
    assert not harness.resumed and not harness.enabled and not harness.settled
    retained(harness)


def test_failed_final_report_keeps_even_terminal_usage_reserved(harness, capsys):
    harness.detail_error = ValueError("PRIVATE-REPORT")
    with pytest.raises(SystemExit):
        live_background.main()
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert harness.evaluation["state"] == "complete"
    assert not harness.settled
    retained(harness)


def test_failed_draft_claim_disables_only_the_just_enabled_grant(
    harness, capsys, monkeypatch
):
    harness.grant["state"] = "draft"
    harness.claim_error = RuntimeError("PRIVATE-CLAIM")
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--enable-authorization"])
    with pytest.raises(SystemExit):
        live_background.main()
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    summary = json.loads(output.out)
    assert summary["unconsumed_authorization_disabled"] is True
    assert harness.enabled == harness.disabled == [harness.grant["id"]]
    assert not harness.executed
    retained(harness)


@pytest.mark.parametrize("stage", ["initial", "arrival", "final"])
def test_persistence_failure_never_releases_allowance(
    harness, capsys, monkeypatch, stage
):
    save = live_background.save_campaign
    saves = 0
    target = {"initial": 1, "arrival": 3, "final": 4}[stage]

    def fail_once(path, campaign):
        nonlocal saves
        saves += 1
        if saves == target:
            raise OSError("PRIVATE-DISK")
        save(path, campaign)

    monkeypatch.setattr(live_background, "save_campaign", fail_once)
    if stage == "initial":
        assert rejected(harness, capsys)["reason"] == "preparation_failed"
    else:
        with pytest.raises(SystemExit):
            live_background.main()
        output = capsys.readouterr()
        assert "PRIVATE" not in output.out + output.err
        retained(harness)
        assert harness.ledger.reserve.__name__ == "reserve"


def test_campaign_lock_is_held_before_database_or_enabling(harness, capsys):
    with (harness.settings.home / "live-acceptance.lock").open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rejected(harness, capsys)["reason"] == "campaign_locked"
    assert not harness.initialized


def test_campaign_is_never_recreated(harness, capsys):
    harness.path.unlink()
    assert rejected(harness, capsys)["reason"] == "existing_campaign_required"


def test_missing_confirmation_cannot_reserve_enable_or_dispatch(
    harness, capsys, monkeypatch
):
    monkeypatch.setattr(
        sys, "argv", [arg for arg in sys.argv if arg != "--confirm-model-usage"]
    )
    with pytest.raises(SystemExit) as error:
        live_background.main()
    assert error.value.code == 2
    assert not harness.initialized and not harness.executed and not harness.enabled
    assert json.loads(harness.path.read_text()) == harness.previous
    capsys.readouterr()


@pytest.mark.parametrize("commit", ["enable", "attach"])
def test_committed_database_effects_are_recovered_before_signal_cleanup(
    harness, capsys, monkeypatch, commit
):
    if commit == "enable":
        harness.grant["state"] = "draft"
        harness.enable_committed_error = KeyboardInterrupt("PRIVATE-POST-COMMIT")
        monkeypatch.setattr(sys, "argv", [*sys.argv, "--enable-authorization"])
    else:
        harness.attach_committed_error = KeyboardInterrupt("PRIVATE-POST-COMMIT")
    with pytest.raises(SystemExit):
        live_background.main()
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    summary = json.loads(output.out)
    assert summary["status"] == "interrupted_allowance_retained"
    if commit == "enable":
        assert harness.grant["state"] == "disabled"
        assert harness.enabled == harness.disabled == [harness.grant["id"]]
    else:
        assert summary["evaluation_id"] == harness.evaluation["id"]
        assert harness.stopped == ["cancelling"]
        assert all(
            harness.runs[run_id]["state"] == "cancelled" for run_id in harness.run_ids
        )
    assert not harness.executed
    assert harness.runs[harness.foreground["id"]]["state"] == "cancelled"
    retained(harness)


def test_final_unknown_usage_cannot_report_success_or_release_campaign(harness, capsys):
    def damage():
        harness.evaluation["usage"]["unknown_calls"] = 1

    harness.detail_mutation = damage
    with pytest.raises(SystemExit):
        live_background.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "exception_allowance_retained"
    assert summary["reason"] == "final_usage_not_fully_settled"
    assert not harness.settled
    retained(harness)


def test_sigterm_uses_interrupted_cleanup_and_restores_handler(harness, capsys):
    import signal

    original = signal.getsignal(signal.SIGTERM)

    def terminate(identifier):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)

    harness.on_execute = terminate
    with pytest.raises(SystemExit):
        live_background.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "interrupted_allowance_retained"
    assert signal.getsignal(signal.SIGTERM) is original
    assert harness.stopped == ["cancelling"]
    retained(harness)


def test_known_budget_stop_settles_actual_usage_but_exits_failure(harness, capsys):
    def stop(identifier):
        if identifier != harness.run_ids[0] or not harness.resumed:
            return False
        harness.runs[identifier]["state"] = "budget_exceeded"
        harness.runs[harness.run_ids[1]]["state"] = "cancelled"
        harness.evaluation.update(state="budget_exceeded", metrics={"complete": False})
        return True

    harness.on_execute = stop
    with pytest.raises(SystemExit) as error:
        live_background.main()
    assert error.value.code == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["state"] == "budget_exceeded"
    assert summary["status"] == "settled" and summary["metrics_complete"] is False
    assert harness.settled == [harness.evaluation["id"]]
    campaign = json.loads(harness.path.read_text())
    assert campaign["reserved_calls"] == harness.previous["reserved_calls"] + 2
    assert campaign["reserved_tokens"] == harness.previous["reserved_tokens"] + 17
    assert campaign["runs"][:-1] == harness.previous["runs"]


@pytest.mark.postgres
@pytest.mark.docker
def test_real_sdk_postgres_docker_harness_with_synthetic_model_only(
    tuning, store, request_data, tmp_path, monkeypatch, capsys
):
    original_settings, _, body, _ = tuning
    settings = Settings(
        original_settings.home,
        original_settings.database_url,
        original_settings.token,
        original_settings.model.model_copy(update={"billing_mode": "subscription"}),
    )
    background = RealBackground(settings, store)
    body["batch_budget"]["max_calls"] = 40
    grant = background.create(body)
    request_data["model"] = settings.model.model_dump()
    foreground, _, _ = prepare_run(
        store, settings, request_data, tmp_path, ["python -c 'assert True'"]
    )
    store.transition(foreground["id"], "paused", "Prepared controlled fixture")
    campaign_path = settings.home / "live-acceptance.json"
    prior = {
        "reserved_calls": 5,
        "reserved_tokens": 50,
        "authorization": {"mode": "subscription_uncapped"},
        "runs": [{"attempt_id": "preserved-old-unknown", "status": "reserved"}],
    }
    campaign_path.write_text(json.dumps(prior))
    executed = []
    active = []
    calls = []
    actual_execute = RealWorker.execute

    def execute(worker, run_id):
        executed.append(run_id)
        active[:] = [run_id]
        try:
            return actual_execute(worker, run_id)
        finally:
            active.clear()

    def transport(*args, **kwargs):
        run_id = active[0]
        calls.append(run_id)
        if run_id == foreground["id"] or calls.count(run_id) > 1:
            return response(
                "finish", {"message": "Synthetic fixture complete"}, len(calls)
            )
        return response(
            "doonex_shell",
            {"command": "printf 'answer = 42\\n' > main.py"},
            len(calls),
        )

    monkeypatch.setattr(live_background.Settings, "load", lambda: settings)
    monkeypatch.setattr(RealWorker, "execute", execute)
    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_background.py",
            "--confirm-model-usage",
            "--enable-authorization",
            "--authorization-id",
            grant["id"],
            "--foreground-run-id",
            foreground["id"],
            "--model",
            settings.model.model_id,
        ],
    )
    live_background.main()
    output = capsys.readouterr().out
    summary = json.loads(output[output.index("{") :])
    assert summary["status"] == "settled" and summary["metrics_complete"] is True
    assert summary["foreground_state"] == "completed_verified"
    assert summary["foreground_queue_to_running_seconds"] >= 0
    assert executed[1] == foreground["id"] and executed[0] == executed[2]
    assert len(set(executed)) == 16 and len(calls) == 31
    assert background.get(grant["id"])["evaluation_id"] == summary["evaluation_id"]
    assert background.day(grant["date"])["reserved"]["calls"] == 0
    campaign = json.loads(campaign_path.read_text())
    assert campaign["runs"][:-1] == prior["runs"]
    assert campaign["reserved_calls"] == prior["reserved_calls"] + 31
    assert campaign["reserved_tokens"] == prior["reserved_tokens"] + 3720
