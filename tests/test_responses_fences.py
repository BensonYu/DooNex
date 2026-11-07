"""Global dispatch and explicit recovery cannot be bypassed by Run controls."""

import os

import pytest
from doonex.api import create_app
from doonex.arena import Arena
from doonex.budget import Halt, Ledger
from doonex.responses_registry import helper_fenced
from doonex.scheduling import Scheduler
from doonex.worker import Worker
from fastapi.testclient import TestClient
from test_responses_registry import factory as registry_factory
from test_responses_registry import record

factory = registry_factory
pytestmark = pytest.mark.postgres


def unresolved_with_real_lease(factory, settings):
    item = factory()
    lease = settings.home / "responses-helper" / "lifetime.lease"
    lease.parent.mkdir(parents=True, mode=0o700)
    fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    observed = os.fstat(fd)
    os.close(fd)
    item["registration"]["lease"] = {
        "device": observed.st_dev,
        "inode": observed.st_ino,
    }
    item["registry"].register(item["registration"])
    return item


@pytest.mark.parametrize(
    "action_status", ["pending", "unknown", "acknowledged", "completed"]
)
def test_helper_fence_survives_action_and_run_state_changes(
    factory, store, settings, request_data, action_status
):
    item = factory()
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
def test_ordinary_resume_cannot_acknowledge_helper_fence(factory, store, settings):
    item = factory()
    item["registry"].register(item["registration"])
    with store.locked(item["run_id"]) as (connection, run):
        store.mark_pending_unknown(item["run_id"], connection, run)
        run.update(state="paused")
    original = store.get(item["run_id"])
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        report = client.get(f"/api/runs/{item['run_id']}/recovery")
        assert report.status_code == 200
        assert report.json()["transport_blocked"] is True
        result = client.post(
            f"/api/runs/{item['run_id']}/resume", json={"acknowledge_unknown": True}
        )
        assert result.status_code == 400
        assert "传输进程" in result.json()["detail"]
    assert store.get(item["run_id"]) == original
    assert record(store, item)["state"] == "prepared"


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
