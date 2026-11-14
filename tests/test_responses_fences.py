"""Global dispatch and explicit recovery cannot be bypassed by Run controls."""

import os
import subprocess
import time

import pytest
from doonex import controller_operations as co
from doonex.api import create_app
from doonex.arena import Arena
from doonex.budget import Halt, Ledger
from doonex.cleanup import PROCESS_UNKNOWN, begin_cleanup_intent
from doonex.responses_process import LeaseIdentity
from doonex.responses_registry import helper_fenced
from doonex.scheduling import Scheduler
from doonex.store import external_execution_fenced, now
from doonex.worker import Worker
from fastapi.testclient import TestClient
from test_responses_registry import factory as registry_factory
from test_responses_registry import record

factory = registry_factory
pytestmark = pytest.mark.postgres


def forbid_recovery_inspection(monkeypatch):
    from doonex.evidence import Evidence
    from doonex.generation import Generation
    from doonex.review import Review
    from doonex.sandbox import Sandbox
    from doonex.workspace import Workspace

    def forbidden(*_args, **_kwargs):
        pytest.fail("An existing execution fence must precede recovery inspection")

    for target, name in (
        (Workspace, "scan"),
        (Review, "inspect"),
        (Evidence, "inspect"),
        (Generation, "validate_run"),
        (Sandbox, "containers"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(target, name, forbidden)


def recovery_snapshot(store):
    with store.connect() as connection:
        return {
            "runs": connection.execute("SELECT * FROM runs ORDER BY id").fetchall(),
            "actions": connection.execute(
                "SELECT * FROM actions ORDER BY id"
            ).fetchall(),
            "controller_operations": connection.execute(
                "SELECT * FROM controller_operations ORDER BY id"
            ).fetchall(),
            "events": connection.execute("SELECT * FROM events ORDER BY id").fetchall(),
        }


def unresolved_with_real_lease(factory, settings):
    lease = settings.home / "responses-helper" / "lifetime.lease"
    lease.parent.mkdir(parents=True, mode=0o700)
    fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    observed = os.fstat(fd)
    os.close(fd)
    item = factory(lease=LeaseIdentity(observed.st_dev, observed.st_ino))
    item["registry"].register(item["registration"])
    return item


@pytest.mark.parametrize(
    "action_status", ["pending", "unknown", "acknowledged", "completed"]
)
@pytest.mark.parametrize("stage", ["reserved", "prepared"])
def test_helper_fence_survives_action_and_run_state_changes(
    factory, store, settings, request_data, action_status, stage
):
    item = factory()
    if stage == "prepared":
        item["registry"].register(item["registration"])
    with store.connect() as connection:
        connection.execute(
            "UPDATE actions SET status=%s WHERE id=%s",
            (action_status, item["action_id"]),
        )
    with store.locked(item["run_id"]) as (_, run):
        run.update(state="completed_verified", recovery={"remaining": [], "errors": []})
    other = store.create_run(request_data)
    with store.locked(other["id"]) as (_, run):
        run.update(state="running", owner="other-owner")
    ledger = Ledger(store, other["id"], "other-owner")
    with pytest.raises(Halt, match="传输进程"):
        ledger.reserve(100, 50)
    with pytest.raises(Halt, match="传输进程"):
        ledger.tool_intent("must-not-run")
    with pytest.raises(ValueError, match="传输进程"):
        Arena(settings, store).resume("00000000-0000-0000-0000-000000000000", True)
    assert Scheduler(settings, store).claim_next() is None
    assert store.get(other["id"])["usage"]["calls"] == 0


def test_worker_startup_refuses_old_terminal_run_helper(factory, store, settings):
    item = factory()
    item["registry"].register(item["registration"])
    with store.locked(item["run_id"]) as (_, run):
        run.update(state="failed")
    with pytest.raises(RuntimeError, match="模型传输进程"):
        Worker(settings).run(once=True)
    assert record(store, item)["state"] == "prepared"


@pytest.mark.docker
def test_ordinary_resume_cannot_acknowledge_helper_fence(
    factory, store, settings, monkeypatch
):
    item = factory()
    item["registry"].register(item["registration"])
    with store.locked(item["run_id"]) as (connection, run):
        store.mark_pending_unknown(item["run_id"], connection, run)
        run.update(state="paused")
    original = store.get(item["run_id"])
    before = recovery_snapshot(store)
    forbid_recovery_inspection(monkeypatch)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        report = client.get(f"/api/runs/{item['run_id']}/recovery")
        assert report.status_code == 200
        assert report.json()["transport_blocked"] is True
        assert report.json()["inspection_blocked"] is True
        assert report.json()["containers"] is None
        assert report.json()["workspace_ok"] is None
        assert recovery_snapshot(store) == before
        result = client.post(
            f"/api/runs/{item['run_id']}/resume", json={"acknowledge_unknown": True}
        )
        assert result.status_code == 400
        assert "传输进程" in result.json()["detail"]
    assert store.get(item["run_id"]) == original
    assert record(store, item)["state"] == "prepared"
    assert recovery_snapshot(store) == before


@pytest.mark.parametrize(
    "blocker", ["reserved", "prepared", "controller", "termination", "local_intent"]
)
def test_recovery_reports_fence_before_workspace_git_generation_or_probe(
    factory, store, settings, request_data, monkeypatch, blocker
):
    if blocker in {"reserved", "prepared"}:
        item = factory()
        if blocker == "prepared":
            item["registry"].register(item["registration"])
    else:
        other = store.create_run(request_data)
        if blocker == "controller":
            registry = co.ControllerRegistry(
                store,
                actor=co.fresh_actor("api"),
                check_owner=lambda: None,
                check_other_fences=external_execution_fenced,
            )
            registry.reserve(
                "sandbox_probe",
                scope=co.scope(
                    intent_digest="a" * 64,
                    run_label="doonex.run_id=" + other["id"],
                ),
                run_id=other["id"],
                project_id=other["request"]["project_id"],
                expected_run_owner=other.get("owner"),
                deadline_monotonic_ns=time.monotonic_ns() + 60_000_000_000,
            ).launch()
        elif blocker == "termination":
            with store.locked(other["id"]) as (_, current):
                current["recovery"] = {
                    PROCESS_UNKNOWN: True,
                    "errors": [PROCESS_UNKNOWN],
                    "remaining": [],
                }
    target = store.create_run(request_data)
    with store.locked(target["id"]) as (_, current):
        current.update(
            state="paused", first_started_at=now(), handoff={"must_not_inspect": True}
        )
        if blocker == "prepared":
            current["request"]["purpose"] = "skill_generation"
        if blocker == "local_intent":
            begin_cleanup_intent(current, source="api", actor=co.fresh_actor("api"))
    directory = settings.home / "runs" / target["id"] / "workspace"
    directory.mkdir(parents=True)
    (directory / "sentinel.py").write_text("must_not_be_scanned = True\n")
    before = recovery_snapshot(store)
    forbid_recovery_inspection(monkeypatch)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        report = client.get(f"/api/runs/{target['id']}/recovery")
        assert report.status_code == 200, report.text
        data = report.json()
        assert data["inspection_blocked"] is True
        assert data["transport_blocked"] is (blocker in {"reserved", "prepared"})
        assert data["containers"] is None and data["workspace_ok"] is None
        assert data["notes"] and data["pending_actions"] == []
        assert recovery_snapshot(store) == before
        result = client.post(
            f"/api/runs/{target['id']}/resume", json={"acknowledge_unknown": True}
        )
        assert result.status_code == 400, result.text
    assert recovery_snapshot(store) == before
    if blocker in {"reserved", "prepared"}:
        assert record(store, item)["state"] == blocker


def test_authenticated_resolution_binds_current_hash_and_keeps_unknown_budget(
    factory, store, settings
):
    item = unresolved_with_real_lease(factory, settings)
    with store.locked(item["run_id"]) as (connection, run):
        store.mark_pending_unknown(item["run_id"], connection, run)
        run.update(state="interrupted")
    original = store.get(item["run_id"])
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/transport-helpers").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        listing = client.get("/api/transport-helpers").json()
        assert listing["items"] == [
            {"action_id": item["action_id"], "run_id": item["run_id"]}
        ]
        inspection = client.get(f"/api/transport-helpers/{item['action_id']}").json()
        assert inspection["valid"] and inspection["unresolved"]
        assert inspection["lease"]["locked"] is False
        body = {
            "expected_record_sha256": inspection["record_sha256"],
            "attempt_id": inspection["record"]["attempt_id"],
            "nonce": inspection["record"]["nonce"],
            "lease_identity": item["registration"]["lease"],
            "reason": "operator_confirmed_local_lease",
        }
        path = f"/api/transport-helpers/{item['action_id']}/resolve"
        assert (
            client.post(
                path, json={**body, "expected_record_sha256": "0" * 64}
            ).status_code
            == 400
        )
        assert record(store, item)["state"] == "prepared"
        result = client.post(path, json=body)
        assert result.status_code == 200, result.text
        assert result.json()["record"]["state"] == "operator_resolved"
        assert result.json()["unresolved"] is False
        assert client.get("/api/transport-helpers").json()["items"] == []
    final = store.get(item["run_id"])
    assert final == original
    assert final["usage"]["unknown_calls"] == 1
    assert final["usage"]["reserved_tokens"] > 0
    with store.connect() as connection:
        assert not helper_fenced(connection)
