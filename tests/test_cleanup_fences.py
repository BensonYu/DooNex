"""Observed controller-process uncertainty survives ordinary recovery paths."""

from copy import deepcopy

import pytest
from doonex.api import create_app
from doonex.cleanup import PROCESS_UNKNOWN, controller_process_unknown, merge_cleanup
from doonex.sandbox import Sandbox, SandboxError
from doonex.scheduling import Scheduler
from doonex.store import execution_fenced, now
from doonex.worker import Worker, check_sandbox_cleanup, stop_containers
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_arena_recovery import evaluation as evaluation_fixture
from test_worker import prepare_run, scripted_model

evaluation = evaluation_fixture


def receipt(*, local_unknown=True):
    return {
        "container_name": "controlled-fixture",
        "container_absence_confirmed": True,
        "processes": [
            {"operation": "attach", "pid": 12345, "reaped": False, "pipe_eof": False}
        ],
        "errors": ["unconfirmed_fixture_process"],
        "local_process_unknown": local_unknown,
    }


def unknown_error():
    error = SandboxError("Synthetic local process uncertainty")
    error.cleanup_unknown = True
    error.cleanup_evidence = receipt()
    return error


def empty_cleanup(*_args, **_kwargs):
    return {"remaining": [], "errors": [], "stopped": []}


def test_latch_survives_wrapped_error_then_repeated_empty_cleanup():
    original = unknown_error()
    wrapper = RuntimeError("Tool outcome unknown")
    wrapper.__cause__ = original
    result = merge_cleanup(empty_cleanup(), error=wrapper)
    assert controller_process_unknown(result)
    assert result["errors"] == [PROCESS_UNKNOWN]
    assert result["cleanup_evidence"] == original.cleanup_evidence
    snapshot = deepcopy(result)
    for _ in range(10):
        result = merge_cleanup(empty_cleanup(), previous=result)
    assert result == snapshot
    assert original.cleanup_evidence == receipt()


def test_proven_empty_docker_cannot_clear_returned_local_unknown():
    with pytest.raises(SandboxError) as raised:
        check_sandbox_cleanup(
            {"cleanup_unknown": True, "cleanup_evidence": receipt()}
        )
    sandbox = type("SafeDocker", (), {"stop_run": staticmethod(empty_cleanup)})()
    result = stop_containers(sandbox, "unused", error=raised.value)
    assert result["remaining"] == []
    assert controller_process_unknown(result)


def test_complete_docker_proof_can_resolve_only_container_uncertainty():
    error = unknown_error()
    error.cleanup_evidence = receipt(local_unknown=False)
    error.cleanup_evidence["container_absence_confirmed"] = False
    assert merge_cleanup(empty_cleanup(), error=error) == empty_cleanup()


def test_new_unknown_process_keeps_distinct_receipt_and_overflow_is_explicit():
    from doonex.cleanup import MAX_PROCESS_RECEIPTS

    result = empty_cleanup()
    for index in range(MAX_PROCESS_RECEIPTS + 3):
        error = unknown_error()
        error.cleanup_evidence["processes"][0]["pid"] = 12345 + index
        result = merge_cleanup(empty_cleanup(), previous=result, error=error)
        assert result["cleanup_evidence"] == error.cleanup_evidence
    assert result["controller_process_evidence_overflow"] is True
    assert len(result["controller_process_evidence"]) == MAX_PROCESS_RECEIPTS
    assert result["controller_process_evidence"][0]["processes"][0]["pid"] == 12345


def test_latest_completed_cleanup_proof_is_not_replaced_by_prior_unknown():
    prior = merge_cleanup(empty_cleanup(), error=unknown_error())
    complete = receipt(local_unknown=False)
    complete.update(errors=[], processes=[])
    result = merge_cleanup(
        {**empty_cleanup(), "cleanup_evidence": complete}, previous=prior
    )
    assert result["cleanup_attempt_evidence"] == complete
    assert result["cleanup_evidence"] == prior["cleanup_evidence"]
    assert controller_process_unknown(result)


@pytest.mark.postgres
def test_worker_records_observed_unknown_and_restart_cannot_erase_it(
    settings, store, request_data, tmp_path, monkeypatch
):
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    monkeypatch.setattr(Sandbox, "stop_run", empty_cleanup)
    monkeypatch.setattr(Sandbox, "run", lambda *_a, **_k: (_ for _ in ()).throw(unknown_error()))
    calls = scripted_model(monkeypatch, ["touch must-stop"])
    worker = Worker(settings)
    worker.run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "interrupted"
    assert controller_process_unknown(final["recovery"])
    assert len(calls) == 1 and len(store.pending(run["id"])) == 1
    before = deepcopy(final)
    with pytest.raises(RuntimeError, match="终止状态不明"):
        Worker(settings).run(once=True)
    after = store.get(run["id"])
    assert after["recovery"] == before["recovery"]
    assert after["usage"] == before["usage"]
    assert after["first_started_at"] == before["first_started_at"]
    with store.connect() as c:
        assert execution_fenced(c)


@pytest.mark.postgres
@pytest.mark.parametrize("action", ["pause", "cancel", "resume"])
def test_ordinary_api_control_cannot_clear_local_fence(
    settings, store, request_data, tmp_path, monkeypatch, action
):
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    recovery = merge_cleanup(empty_cleanup(), error=unknown_error())
    with store.locked(run["id"]) as (_, current):
        current.update(state="interrupted", first_started_at=now(), recovery=recovery)
    monkeypatch.setattr(Sandbox, "stop_run", empty_cleanup)
    monkeypatch.setattr(Sandbox, "containers", lambda *_a, **_k: [])
    # The recovery route currently still uses its legacy Docker read. It is a
    # read-only stub here; component coverage exercises the actual bounded CLI.
    monkeypatch.setattr(
        "doonex.api.subprocess.run",
        lambda *_a, **_k: type("DockerResult", (), {"returncode": 0, "stdout": ""})(),
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        response = client.post(
            f"/api/runs/{run['id']}/{action}",
            json={"acknowledge_unknown": True} if action == "resume" else None,
        )
    assert response.status_code == (400 if action == "resume" else 200)
    after = store.get(run["id"])
    assert after["state"] == "interrupted"
    assert after["recovery"] == recovery
    with store.connect() as c:
        assert execution_fenced(c)


@pytest.mark.postgres
def test_arena_acknowledgment_cannot_clear_local_process_fence(
    evaluation, store, monkeypatch
):
    _, arena, batch, run_id = evaluation
    recovery = merge_cleanup(empty_cleanup(), error=unknown_error())
    with store.locked(run_id) as (_, current):
        current.update(state="interrupted", first_started_at=now(), recovery=recovery)
    batch.update(state="interrupted", first_started_at=now())
    with store.connect() as c:
        c.execute(
            "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
            (batch["state"], Jsonb(batch), batch["id"]),
        )
    monkeypatch.setattr(Sandbox, "containers", lambda *_a, **_k: [])
    with pytest.raises(ValueError, match="控制进程"):
        arena.resume(batch["id"], True)
    assert store.get(run_id)["recovery"] == recovery
    assert store.get(run_id)["state"] == "interrupted"
    with store.connect() as c:
        assert execution_fenced(c)


@pytest.mark.postgres
def test_yielded_resume_persists_new_container_probe_uncertainty(
    evaluation, store, monkeypatch
):
    settings, _, _, run_id = evaluation
    with store.locked(run_id) as (_, current):
        current.update(state="yielded", first_started_at=now())

    def probe(*_args, **_kwargs):
        raise unknown_error()

    monkeypatch.setattr(Sandbox, "containers", probe)
    scheduler = Scheduler(settings, store)
    assert scheduler.resume_yielded(store.get(run_id), None) is False
    after = store.get(run_id)
    assert after["state"] == "interrupted" and scheduler.cleanup_blocked
    assert controller_process_unknown(after["recovery"])
    with store.connect() as c:
        assert execution_fenced(c)
