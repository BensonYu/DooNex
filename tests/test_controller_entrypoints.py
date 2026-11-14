"""Real entrypoints, request contexts and lock-free stop intent transitions."""

import subprocess
import threading
from contextlib import contextmanager
from contextvars import copy_context
from uuid import uuid4

import pytest
from doonex.api import create_app
from doonex.config import Settings
from doonex.controller_operations import Operation, RegistryError, current_registry
from doonex.library import Library
from doonex.sandbox import Sandbox
from doonex.store import DISPATCH_LOCK, OwnershipLost, execution_fenced, now
from doonex.worker import WORKER_LOCK, Worker
from doonex.workspace import _git
from fastapi.testclient import TestClient
from test_api import repository
from test_worker import scripted_model


@contextmanager
def acquired_worker(settings):
    worker = Worker(settings)
    with worker.store.connect() as connection:
        connection.autocommit = True
        assert connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS held", (WORKER_LOCK,)
        ).fetchone()["held"]
        worker.lock_connection = connection
        try:
            yield worker
        finally:
            worker.lock_connection = None
            connection.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK,))


def observe_product_popen(monkeypatch, store):
    """Inspect durable launch and take both locks on a different connection."""
    original = subprocess.Popen
    observed = []

    def launch(arguments, *args, **kwargs):
        registry = current_registry()
        with store.connect() as connection:
            assert connection.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS held", (DISPATCH_LOCK,)
            ).fetchone()["held"]
            records = connection.execute(
                "SELECT data FROM controller_operations WHERE data->>'state'='launching'"
            ).fetchall()
            assert len(records) == 1
            record = records[0]["data"]
            assert record["actor"] == registry.actor.data()
            if record["run_id"]:
                run = connection.execute(
                    "SELECT data FROM runs WHERE id=%s FOR UPDATE NOWAIT",
                    (record["run_id"],),
                ).fetchone()["data"]
                assert record["expected_run_owner"] == run.get("owner")
                assert record["project_id"] == run["request"]["project_id"]
                if record["operation_kind"] == "sandbox_cleanup":
                    assert run["cleanup_intent"]["state"] == "pending"
                    assert run["state"] in {"pausing", "interrupted"}
            observed.append(record)
        return original(arguments, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    return observed


@pytest.mark.postgres
def test_direct_execute_and_actor_require_actual_worker_lock(
    settings, store, request_data
):
    run = store.create_run(request_data)
    worker = Worker(settings)
    with pytest.raises(OwnershipLost):
        worker.execute(run["id"])
    assert store.get(run["id"])["first_started_at"] is None
    with acquired_worker(settings) as worker:
        with worker.controller_actor() as outer:
            assert current_registry() is outer
            with worker.controller_actor() as inner:
                assert current_registry() is inner and inner is not outer
            with pytest.raises(RegistryError, match="actor_closed"):
                inner._live()
            assert current_registry() is outer
        with pytest.raises(RegistryError, match="actor_closed"):
            outer._live()
        worker.lock_connection.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK,))
        with pytest.raises(OwnershipLost), worker.controller_actor():
            pytest.fail("Released worker lock entered a controller actor")


@pytest.mark.postgres
def test_api_threadpool_contexts_are_distinct_and_expire(
    settings, tmp_path, monkeypatch
):
    barrier = threading.Barrier(2)
    captured, contexts, failures = [], [], []

    def credential(self, *args, **kwargs):
        registry = current_registry()
        captured.append(registry)
        contexts.append(copy_context())
        barrier.wait(timeout=10)
        assert current_registry() is registry
        return "synthetic"

    monkeypatch.setattr(Settings, "credential", credential)
    with TestClient(create_app(settings)) as client:

        def get():
            try:
                result = client.get(
                    "/api/settings",
                    headers={"Authorization": "Bearer " + settings.token},
                )
                assert result.status_code == 200, result.text
            except BaseException as error:  # noqa: BLE001 - forward thread failures
                failures.append(error)

        threads = [threading.Thread(target=get) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads)
    assert not failures and len(captured) == 2 and captured[0] is not captured[1]
    assert captured[0].actor.owner != captured[1].actor.owner
    for registry in captured:
        with pytest.raises(RegistryError, match="actor_closed"):
            registry._live()
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("Late API actor spawned a process"),
    )
    for context in contexts:
        with pytest.raises(RegistryError, match="actor_closed"):
            context.run(_git, tmp_path, "status")
    with pytest.raises(RegistryError, match="registry_missing"):
        current_registry()


@pytest.mark.postgres
@pytest.mark.docker
def test_api_registration_worker_sdk_delivery_use_durable_product_leaves(
    settings, store, tmp_path, monkeypatch
):
    repo, git = repository(tmp_path)
    before = git("status", "--porcelain=v1")
    calls = scripted_model(monkeypatch, ["printf 'answer = 42\\n' > hello.py"])
    observed = observe_product_popen(monkeypatch, store)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        registered = client.post("/api/projects", json={"path": str(repo)})
        assert registered.status_code == 200, registered.text
        response = client.post(
            "/api/runs",
            json={
                "project_id": registered.json()["id"],
                "prompt": "controlled fixture",
                "checks": ["python -c 'from hello import answer; assert answer == 42'"],
            },
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["id"]
        Worker(settings).run(once=True)
        final = store.get(run_id)
        assert final["state"] == "completed_verified", final["reason"]
        assert len(calls) == final["usage"]["calls"] == 2
        assert client.get(f"/api/runs/{run_id}/evidence").status_code == 200
    monkeypatch.undo()
    assert git("status", "--porcelain=v1") == before
    assert (repo / "hello.py").read_text() == "answer = 1\n"
    assert {record["actor"]["kind"] for record in observed} == {"api", "worker"}
    assert any(record["run_id"] is None for record in observed)
    assert any(record["operation_kind"] == "sandbox_execute" for record in observed)
    with store.connect() as connection:
        assert not connection.execute(
            "SELECT 1 FROM controller_operations WHERE data->>'state'<>'terminal'"
        ).fetchone()


@pytest.mark.postgres
@pytest.mark.docker
def test_duplicate_api_stop_and_concurrent_cancel_share_one_lock_free_cleanup(
    settings, store, request_data, monkeypatch
):
    run = store.create_run(request_data)
    with store.locked(run["id"]) as (_, current):
        current.update(state="paused", first_started_at=now())
    first_started_at = store.get(run["id"])["first_started_at"]
    entered, release = threading.Event(), threading.Event()
    original = Sandbox.stop_run
    cleanup_calls, responses, failures = [], [], []

    def stop(sandbox, run_id, **kwargs):
        cleanup_calls.append(kwargs)
        with store.connect() as connection:
            assert connection.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS held", (DISPATCH_LOCK,)
            ).fetchone()["held"]
            current = connection.execute(
                "SELECT data FROM runs WHERE id=%s FOR UPDATE NOWAIT", (run_id,)
            ).fetchone()["data"]
            assert current["cleanup_intent"]["state"] == "pending"
        entered.set()
        assert release.wait(timeout=10)
        return original(sandbox, run_id, **kwargs)

    monkeypatch.setattr(Sandbox, "stop_run", stop)
    observed = observe_product_popen(monkeypatch, store)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token

        def pause():
            try:
                responses.append(client.post(f"/api/runs/{run['id']}/pause"))
            except BaseException as error:  # noqa: BLE001 - forward thread failures
                failures.append(error)

        thread = threading.Thread(target=pause)
        thread.start()
        try:
            assert entered.wait(timeout=10)
            pending = store.get(run["id"])["cleanup_intent"]
            duplicate = client.post(f"/api/runs/{run['id']}/pause")
            assert duplicate.status_code == 200, duplicate.text
            assert duplicate.json()["state"] == "pausing"
            assert store.get(run["id"])["cleanup_intent"] == pending
            assert len(cleanup_calls) == 1
            cancel = client.post(f"/api/runs/{run['id']}/cancel")
            assert cancel.status_code == 200, cancel.text
            assert cancel.json()["state"] == "pausing"
            assert store.get(run["id"])["cleanup_intent"] == pending
            assert len(cleanup_calls) == 1
        finally:
            release.set()
            thread.join(timeout=15)
        assert not thread.is_alive() and not failures
    assert len(responses) == 1 and responses[0].status_code == 200
    final = store.get(run["id"])
    assert final["state"] == "cancelled" and final["requested_action"] == "cancel"
    assert final["cleanup_intent"]["id"] == pending["id"]
    assert final["cleanup_intent"]["state"] == "complete"
    assert final["first_started_at"] == first_started_at
    assert observed and {record["operation_kind"] for record in observed} == {
        "sandbox_cleanup"
    }


@pytest.mark.postgres
@pytest.mark.docker
@pytest.mark.parametrize("change", ["owner", "request", "intent"])
def test_api_cleanup_receipt_cannot_overwrite_a_new_run_binding(
    settings, store, request_data, monkeypatch, change
):
    run = store.create_run(request_data)
    with store.locked(run["id"]) as (_, current):
        current.update(state="paused", first_started_at=now())
    original, changed = Sandbox.stop_run, []

    def stop(sandbox, run_id, **kwargs):
        result = original(sandbox, run_id, **kwargs)
        with store.locked(run_id) as (_, current):
            if change == "owner":
                current["owner"] = str(uuid4())
            elif change == "request":
                current["request"]["prompt"] = "changed immutable request"
            else:
                current["cleanup_intent"]["id"] = str(uuid4())
            changed.append(
                {
                    key: current.get(key)
                    for key in (
                        "owner",
                        "request",
                        "cleanup_intent",
                        "usage",
                        "first_started_at",
                    )
                }
            )
        return result

    monkeypatch.setattr(Sandbox, "stop_run", stop)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        response = client.post(f"/api/runs/{run['id']}/pause")
        assert response.status_code == 200, response.text
    final = store.get(run["id"])
    assert len(changed) == 1 and final["state"] == "pausing", final
    assert final["cleanup_intent"]["state"] == "pending"
    assert {key: final.get(key) for key in changed[0]} == changed[0]
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS total FROM controller_operations"
            ).fetchone()["total"]
            == 1
        )
        assert (
            connection.execute(
                "SELECT data->>'state' AS state FROM controller_operations"
            ).fetchone()["state"]
            == "terminal"
        )


@pytest.mark.postgres
@pytest.mark.docker
@pytest.mark.parametrize("entry", ["reconcile", "exception"])
def test_worker_cleanup_commits_intent_and_releases_both_locks_before_popen(
    settings, store, request_data, monkeypatch, entry
):
    run = store.create_run(request_data)
    if entry == "reconcile":
        with store.locked(run["id"]) as (_, current):
            current.update(
                state="running", owner="previous-worker", first_started_at=now()
            )
    observed = observe_product_popen(monkeypatch, store)
    with acquired_worker(settings) as worker:
        if entry == "reconcile":
            worker.reconcile()
        else:

            def unavailable(*args, **kwargs):
                raise ValueError("synthetic content initialization failure")

            monkeypatch.setattr(Library, "__init__", unavailable)
            worker.execute(run["id"])
    final = store.get(run["id"])
    assert final["state"] == ("interrupted" if entry == "reconcile" else "failed"), (
        final["reason"]
    )
    assert final["cleanup_intent"]["state"] == "complete"
    assert final["usage"]["calls"] == final["usage"]["reserved_tokens"] == 0
    assert observed and {record["operation_kind"] for record in observed} == {
        "sandbox_cleanup"
    }


@pytest.mark.postgres
@pytest.mark.docker
def test_controller_only_terminal_ack_unknown_remains_bound_and_blocks_resume(
    settings, store, request_data, monkeypatch
):
    run = store.create_run(request_data)
    with store.locked(run["id"]) as (_, current):
        current.update(state="paused", first_started_at=now())
    original, attempts = Operation.finish, []

    def lost_ack(operation, evidence):
        original(operation, evidence)
        attempts.append(operation.operation_id)
        raise RegistryError(
            "transition_commit_unknown", operation_id=operation.operation_id
        )

    monkeypatch.setattr(Operation, "finish", lost_ack)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        stopped = client.post(f"/api/runs/{run['id']}/pause")
        assert stopped.status_code == 200, stopped.text
        first = store.get(run["id"])
        assert first["state"] == "interrupted"
        assert first["cleanup_intent"]["state"] == "unknown"
        assert first["recovery"]["controller_record_unknown"]
        assert attempts == [
            first["recovery"]["cleanup_evidence"]["controller_operation_id"]
        ]
        assert not first["recovery"]["cleanup_evidence"].get("local_process_unknown")
        assert not first["recovery"]["cleanup_evidence"].get("daemon_request_unknown")
        repeated = client.post(f"/api/runs/{run['id']}/pause")
        assert repeated.status_code == 200 and len(attempts) == 1
        resumed = client.post(
            f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": True}
        )
        assert resumed.status_code == 400, resumed.text
    final = store.get(run["id"])
    assert final["cleanup_intent"] == first["cleanup_intent"]
    assert final["recovery"] == first["recovery"]
    assert final["usage"] == first["usage"]
    with store.connect() as connection:
        assert execution_fenced(connection)
