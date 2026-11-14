"""Observed controller-process uncertainty survives ordinary recovery paths."""

from copy import deepcopy

import controller_test_support
import pytest
from doonex.api import create_app
from doonex.cleanup import (
    DAEMON_UNKNOWN,
    PROCESS_UNKNOWN,
    controller_cleanup_unknown,
    controller_process_unknown,
    merge_cleanup,
    sandbox_context,
)
from doonex.sandbox import Sandbox, SandboxError
from doonex.scheduling import Scheduler
from doonex.store import execution_fenced, now
from doonex.worker import Worker, check_sandbox_cleanup, stop_containers
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_arena_recovery import evaluation as evaluation_fixture
from test_worker import prepare_run, scripted_model

evaluation = evaluation_fixture
controller_registry = controller_test_support.controller_registry


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


def unknown_error(*, daemon=False):
    error = SandboxError("Synthetic local process uncertainty")
    error.cleanup_unknown = True
    error.cleanup_evidence = receipt()
    if daemon:
        error.cleanup_evidence.update(
            container_absence_confirmed=False,
            container_snapshot_absence_confirmed=True,
            local_process_unknown=False,
            daemon_request_unknown=True,
        )
        error.cleanup_evidence["processes"][0].update(reaped=True, pipe_eof=True)
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


@pytest.mark.postgres
def test_proven_empty_docker_cannot_clear_returned_local_unknown(
    store, request_data, controller_registry
):
    with pytest.raises(SandboxError) as raised:
        check_sandbox_cleanup({"cleanup_unknown": True, "cleanup_evidence": receipt()})
    run = store.create_run(request_data)
    context = sandbox_context(run)
    calls = []

    def stop(run_id, **binding):
        calls.append((run_id, binding))
        return empty_cleanup()

    sandbox = type("SafeDocker", (), {"stop_run": staticmethod(stop)})()
    result = stop_containers(sandbox, run["id"], context=context, error=raised.value)
    assert result["remaining"] == []
    assert controller_process_unknown(result)
    assert calls == [(run["id"], context)]


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


@pytest.mark.parametrize("daemon", [False, True])
def test_error_receipt_cannot_become_a_new_cleanup_attempt_on_remerge(daemon):
    first = merge_cleanup(empty_cleanup(), error=unknown_error(daemon=daemon))
    snapshot = deepcopy(first)
    assert "cleanup_attempt_evidence" not in first
    for _ in range(3):
        first = merge_cleanup(first, previous=snapshot)
        assert first == snapshot
        assert "cleanup_attempt_evidence" not in first


def test_record_only_error_cannot_become_a_new_cleanup_attempt_on_remerge():
    from uuid import uuid4

    from doonex.controller_operations import RegistryError

    error = RegistryError("record_unknown", operation_id=str(uuid4()))
    first = merge_cleanup(empty_cleanup(), error=error)
    snapshot = deepcopy(first)
    for _ in range(3):
        first = merge_cleanup(first, previous=snapshot)
        assert first == snapshot
        assert "cleanup_attempt_evidence" not in first
        assert first["controller_record_unknown"] is True


def test_new_raw_attempt_replaces_prior_attempt_but_preserves_unknown_inventory():
    prior = merge_cleanup(empty_cleanup(), error=unknown_error())
    first_proof = {"container_absence_confirmed": False, "processes": []}
    prior = merge_cleanup(
        {**empty_cleanup(), "cleanup_evidence": first_proof}, previous=prior
    )
    snapshot = deepcopy(prior)
    latest_proof = {"container_absence_confirmed": True, "processes": []}
    result = merge_cleanup(
        {**empty_cleanup(), "cleanup_evidence": latest_proof}, previous=prior
    )
    assert result["cleanup_attempt_evidence"] == latest_proof
    assert result["cleanup_evidence"] == prior["cleanup_evidence"]
    assert result["controller_process_evidence"] == prior["controller_process_evidence"]
    assert controller_cleanup_unknown(result)
    result["cleanup_attempt_evidence"]["processes"].append("copy mutation")
    assert latest_proof["processes"] == [] and prior == snapshot


@pytest.mark.parametrize("daemon", [False, True])
def test_blocked_cleanup_preserves_actual_prior_attempt_without_aliasing(daemon):
    original = merge_cleanup(empty_cleanup(), error=unknown_error(daemon=daemon))
    proof = receipt(local_unknown=False)
    proof.update(errors=[], processes=[])
    prior = merge_cleanup(
        {**empty_cleanup(), "cleanup_evidence": proof}, previous=original
    )
    snapshot = deepcopy(prior)
    blocked = {"remaining": ["unknown"], "stopped": [], "errors": ["execution_fenced"]}
    result = merge_cleanup(blocked, previous=prior)
    assert result["cleanup_attempt_evidence"] == proof
    assert result["cleanup_evidence"] == original["cleanup_evidence"]
    assert (
        result["controller_process_evidence"] == original["controller_process_evidence"]
    )
    assert controller_cleanup_unknown(result)
    result = merge_cleanup(result, previous=prior)
    assert result["cleanup_attempt_evidence"] == proof
    result["cleanup_attempt_evidence"]["errors"].append("changed copy")
    assert prior == snapshot


@pytest.mark.parametrize("with_previous", [False, True])
def test_aggregate_remerge_preserves_all_receipts_and_overflow(with_previous):
    from doonex.cleanup import MAX_PROCESS_RECEIPTS

    result = empty_cleanup()
    first = None
    for index in range(MAX_PROCESS_RECEIPTS + 3):
        error = unknown_error(daemon=bool(index % 2))
        error.cleanup_evidence["processes"][0]["pid"] += index
        result = merge_cleanup(empty_cleanup(), previous=result, error=error)
        if first is None:
            first = deepcopy(result)
    snapshot = deepcopy(result)
    for _ in range(3):
        result = merge_cleanup(result, previous=first if with_previous else None)
        assert result == snapshot
        assert result["controller_process_evidence_overflow"] is True
        assert len(result["controller_process_evidence"]) == MAX_PROCESS_RECEIPTS
        assert "cleanup_attempt_evidence" not in result


def assert_preserved_unknown(after, before):
    assert controller_cleanup_unknown(after)
    for key in (
        PROCESS_UNKNOWN,
        DAEMON_UNKNOWN,
        "cleanup_evidence",
        "controller_process_evidence",
        "controller_process_evidence_overflow",
        "cleanup_attempt_evidence",
    ):
        assert (key in after) == (key in before), key
        if key in before:
            assert after[key] == before[key], key
    assert set(after["errors"]) == set(before["errors"]) | {"execution_fenced"}
    assert after["remaining"] == ["unknown"]


@pytest.mark.postgres
@pytest.mark.parametrize("daemon", [False, True])
def test_worker_records_observed_unknown_and_restart_cannot_erase_it(
    settings, store, request_data, tmp_path, monkeypatch, daemon
):
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    stopped = []

    def stop(*args, **kwargs):
        stopped.append(True)
        return empty_cleanup()

    monkeypatch.setattr(Sandbox, "stop_run", stop)
    monkeypatch.setattr(
        Sandbox,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(unknown_error(daemon=daemon)),
    )
    calls = scripted_model(monkeypatch, ["touch must-stop"])
    worker = Worker(settings)
    worker.run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "interrupted"
    assert controller_cleanup_unknown(final["recovery"])
    assert controller_process_unknown(final["recovery"]) is not daemon
    assert len(calls) == 1 and len(store.pending(run["id"])) == 1
    before = deepcopy(final)
    stop_count = len(stopped)
    with pytest.raises(RuntimeError, match="终止状态不明"):
        Worker(settings).run(once=True)
    after = store.get(run["id"])
    assert_preserved_unknown(after["recovery"], before["recovery"])
    assert len(stopped) == stop_count and len(calls) == 1
    assert after["usage"] == before["usage"]
    assert after["first_started_at"] == before["first_started_at"]
    with store.connect() as c:
        assert execution_fenced(c)


@pytest.mark.postgres
@pytest.mark.parametrize("action", ["pause", "cancel", "resume"])
@pytest.mark.parametrize("daemon", [False, True])
def test_ordinary_api_control_cannot_clear_local_fence(
    settings, store, request_data, tmp_path, monkeypatch, action, daemon
):
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    recovery = merge_cleanup(empty_cleanup(), error=unknown_error(daemon=daemon))
    with store.locked(run["id"]) as (_, current):
        current.update(state="interrupted", first_started_at=now(), recovery=recovery)
    before = store.get(run["id"])

    def forbidden(*_args, **_kwargs):
        pytest.fail("An existing fence forbids new ordinary cleanup or probes")

    monkeypatch.setattr(Sandbox, "stop_run", forbidden)
    monkeypatch.setattr(Sandbox, "containers", forbidden)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        response = client.post(
            f"/api/runs/{run['id']}/{action}",
            json={"acknowledge_unknown": True} if action == "resume" else None,
        )
    assert response.status_code == (400 if action == "resume" else 200)
    if action == "resume":
        assert "控制进程或 Docker 请求" in response.json()["detail"]
    after = store.get(run["id"])
    assert after["state"] == "interrupted"
    if action == "resume":
        assert after["recovery"] == recovery
    else:
        assert_preserved_unknown(after["recovery"], recovery)
        assert after["cleanup_intent"]["state"] == "unknown"
    assert after["usage"] == before["usage"]
    assert after["first_started_at"] == before["first_started_at"]
    with store.connect() as c:
        assert execution_fenced(c)


@pytest.mark.postgres
@pytest.mark.parametrize("daemon", [False, True])
def test_resume_global_fence_rejects_before_review_or_container_probe(
    settings, store, request_data, tmp_path, monkeypatch, daemon
):
    from doonex.review import Review

    target, _, _ = prepare_run(store, settings, request_data, tmp_path)
    blocker = store.create_run(request_data)
    recovery = merge_cleanup(empty_cleanup(), error=unknown_error(daemon=daemon))
    with store.locked(blocker["id"]) as (_, current):
        current.update(state="interrupted", first_started_at=now(), recovery=recovery)
    with store.locked(target["id"]) as (_, current):
        current.update(
            state="interrupted", first_started_at=now(), handoff={"unverified": True}
        )
    before = store.get(target["id"]), store.get(blocker["id"])

    def forbidden(*_args, **_kwargs):
        pytest.fail("Resume must reject a global fence before external inspection")

    monkeypatch.setattr(Review, "inspect", forbidden)
    monkeypatch.setattr(Sandbox, "containers", forbidden)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        response = client.post(
            f"/api/runs/{target['id']}/resume", json={"acknowledge_unknown": True}
        )
    assert response.status_code == 400
    assert "控制进程、Docker 请求或模型传输进程" in response.json()["detail"]
    assert (store.get(target["id"]), store.get(blocker["id"])) == before
    with store.connect() as c:
        assert execution_fenced(c)


@pytest.mark.postgres
@pytest.mark.parametrize("daemon", [False, True])
def test_arena_acknowledgment_cannot_clear_local_process_fence(
    evaluation, store, monkeypatch, daemon
):
    _, arena, batch, run_id = evaluation
    recovery = merge_cleanup(empty_cleanup(), error=unknown_error(daemon=daemon))
    with store.locked(run_id) as (_, current):
        current.update(state="interrupted", first_started_at=now(), recovery=recovery)
    batch.update(state="interrupted", first_started_at=now())
    with store.connect() as c:
        c.execute(
            "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
            (batch["state"], Jsonb(batch), batch["id"]),
        )

    def forbidden(*_args, **_kwargs):
        pytest.fail("A global fence forbids new ordinary resume probes")

    monkeypatch.setattr(Sandbox, "containers", forbidden)
    with pytest.raises(ValueError, match="控制进程"):
        arena.resume(batch["id"], True)
    assert store.get(run_id)["recovery"] == recovery
    assert store.get(run_id)["state"] == "interrupted"
    with store.connect() as c:
        assert execution_fenced(c)


def test_create_daemon_uncertainty_survives_empty_ps_and_reaped_cli():
    error = unknown_error()
    evidence = receipt(local_unknown=False)
    evidence.update(container_absence_confirmed=False, daemon_request_unknown=True)
    evidence["processes"][0].update(reaped=True, pipe_eof=True)
    error.cleanup_evidence = evidence
    result = merge_cleanup(empty_cleanup(), error=error)
    for _ in range(3):
        result = merge_cleanup(empty_cleanup(), previous=result)
    assert not controller_process_unknown(result)
    assert controller_cleanup_unknown(result)
    assert result[DAEMON_UNKNOWN] is True and DAEMON_UNKNOWN in result["errors"]
    assert result["cleanup_evidence"] == evidence


@pytest.mark.parametrize("marker", [PROCESS_UNKNOWN, DAEMON_UNKNOWN])
def test_missing_receipt_cannot_clear_a_persisted_marker(marker):
    prior = {"errors": [marker], marker: True}
    result = merge_cleanup(empty_cleanup(), previous=prior)
    assert result[marker] is True and marker in result["errors"]
    assert controller_cleanup_unknown(result)


@pytest.mark.postgres
def test_api_failed_probe_records_its_new_process_evidence(
    settings, store, request_data, tmp_path, monkeypatch
):
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)

    def probe(*_args, **_kwargs):
        raise unknown_error()

    monkeypatch.setattr(Sandbox, "containers", probe)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        response = client.get(f"/api/runs/{run['id']}/recovery")
    assert response.status_code == 400
    assert controller_process_unknown(store.get(run["id"])["recovery"])
    with store.connect() as c:
        assert execution_fenced(c)


@pytest.mark.postgres
def test_arena_failed_probe_records_its_new_process_evidence(
    evaluation, store, monkeypatch
):
    _, arena, batch, run_id = evaluation
    with store.locked(run_id) as (_, current):
        current.update(state="interrupted", first_started_at=now())
    batch.update(state="interrupted", first_started_at=now())
    with store.connect() as c:
        c.execute(
            "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
            (batch["state"], Jsonb(batch), batch["id"]),
        )

    def probe(*_args, **_kwargs):
        raise unknown_error()

    monkeypatch.setattr(Sandbox, "containers", probe)
    with pytest.raises(ValueError, match="无法核查"):
        arena.resume(batch["id"], True)
    assert controller_process_unknown(store.get(run_id)["recovery"])
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
