"""Yielded preparation keeps one deadline and the original durable checkpoint."""

import hashlib
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from doonex import budget, scheduling
from doonex import controller_operations as co
from doonex.budget import Ledger
from doonex.cleanup import (
    DAEMON_UNKNOWN,
    RECORD_UNKNOWN,
    controller_cleanup_unknown,
    sandbox_context,
)
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.sandbox import Sandbox, SandboxError
from doonex.scheduling import ledger_checkpoint
from doonex.store import execution_fenced
from doonex.workspace import Workspace
from test_scheduler_preflight_deadline import admitted as admitted_fixture
from test_scheduler_preflight_deadline import snapshot
from test_scheduler_preflight_deadline import taskset_source as source_fixture

admitted = admitted_fixture
taskset_source = source_fixture
pytestmark = [pytest.mark.postgres, pytest.mark.docker]


@pytest.fixture
def yielded(admitted, tmp_path):
    run = admitted.store.get(admitted.run_id)
    request = run["request"]
    admitted.workspace = Workspace.create(
        Path(request["repo_path"]),
        request["base_commit"],
        tmp_path / "execution",
        operation_context={"run_id": run["id"], **sandbox_context(run)},
    )
    admitted.scheduler.save_yield(
        run["id"], run.get("owner"), "controlled-sdk-receipt", admitted.workspace
    )
    assert admitted.store.get(run["id"])["state"] == "yielded"
    return admitted


def common_clock(monkeypatch):
    clock = [time.monotonic()]
    # Only the cooperative deadline checks use this clock. Real Docker/process
    # supervision retains the actual host monotonic clock and its own deadlines.
    monkeypatch.setattr(
        "doonex.deadline.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    return clock, clock[0] + 30.0


def observe_probe(
    monkeypatch, context, deadline, *, after=None, error=None, before_error=None
):
    original = Sandbox.containers
    calls = []

    def containers(
        self,
        identifier,
        *,
        deadline=None,
        project_id,
        parent_action_id,
        expected_run_owner,
    ):
        assert identifier == context.run_id
        assert {
            "project_id": project_id,
            "parent_action_id": parent_action_id,
            "expected_run_owner": expected_run_owner,
        } == sandbox_context(context.store.get(identifier))
        assert deadline == expected_deadline
        assert co.current_registry().store is context.store
        calls.append(identifier)
        if error is not None:
            if before_error is not None:
                before_error()
            raise error
        result = original(
            self,
            identifier,
            deadline=deadline,
            project_id=project_id,
            parent_action_id=parent_action_id,
            expected_run_owner=expected_run_owner,
        )
        assert result == []
        if after is not None:
            after()
        return result

    expected_deadline = deadline
    monkeypatch.setattr(Sandbox, "containers", containers)
    return calls


def observe_digest(monkeypatch, deadline, *, after=None):
    original = Workspace.execution_digest
    calls = []

    def digest(path, *, deadline=None):
        assert deadline == expected_deadline
        result = original(path, deadline=deadline)
        calls.append(result)
        if after is not None:
            after(len(calls))
        return result

    expected_deadline = deadline
    monkeypatch.setattr(Workspace, "execution_digest", staticmethod(digest))
    return calls


def state(context):
    return {
        "rows": snapshot(context),
        "run": context.store.get(context.run_id),
        "events": context.store.events(context.run_id)["events"],
    }


def assert_refused(context, before):
    after = state(context)
    assert after["run"]["state"] == "interrupted"
    assert ledger_checkpoint(after["run"]) == ledger_checkpoint(before["run"])
    assert after["run"]["request"] == before["run"]["request"]
    assert after["run"]["scheduling"] == before["run"]["scheduling"]
    for table in before["rows"]:
        if table != "runs":
            assert after["rows"][table] == before["rows"][table]
    assert [
        row for row in after["rows"]["runs"] if str(row["id"]) != context.run_id
    ] == [row for row in before["rows"]["runs"] if str(row["id"]) != context.run_id]
    assert after["events"][:-1] == before["events"]
    assert after["events"][-1]["kind"] == "scheduling_resume_refused"
    return after


@pytest.mark.parametrize("bounded", [False, True])
def test_actual_yield_resume_forwards_one_deadline(yielded, monkeypatch, bounded):
    _, limit = common_clock(monkeypatch)
    deadline = limit if bounded else None
    probes = observe_probe(monkeypatch, yielded, deadline)
    digests = observe_digest(monkeypatch, deadline)
    checks = []
    original = yielded.scheduler.preflight

    def preflight(connection, run, *, yield_foreground=True, deadline=None):
        checks.append((yield_foreground, deadline))
        return original(
            connection, run, yield_foreground=yield_foreground, deadline=deadline
        )

    monkeypatch.setattr(yielded.scheduler, "preflight", preflight)
    before = state(yielded)
    assert yielded.scheduler.resume_yielded(
        before["run"], yielded.workspace, deadline=deadline
    )
    after = state(yielded)
    assert probes == [yielded.run_id] and len(digests) == 2
    assert checks == [(False, deadline), (True, deadline)]
    assert after["run"]["state"] == "queued"
    assert ledger_checkpoint(after["run"]) == ledger_checkpoint(before["run"])
    assert after["run"]["scheduling"] == before["run"]["scheduling"]
    assert after["rows"]["actions"] == before["rows"]["actions"]
    assert after["events"][-1]["kind"] == "scheduling_resumed"


def test_expired_entry_keeps_checkpoint_and_never_probes(yielded, monkeypatch):
    clock, deadline = common_clock(monkeypatch)
    clock[0] = deadline + 1
    probes = observe_probe(monkeypatch, yielded, deadline)
    digests = observe_digest(monkeypatch, deadline)
    before = state(yielded)
    with pytest.raises(ExecutionDeadlineExceeded):
        yielded.scheduler.resume_yielded(
            before["run"], yielded.workspace, deadline=deadline
        )
    assert not probes and not digests
    assert state(yielded) == before


@pytest.mark.parametrize("unknown", [False, True])
def test_probe_return_expiry_stops_hash_and_keeps_old_reservations(
    yielded, monkeypatch, unknown
):
    if unknown:
        with yielded.store.locked(yielded.run_id) as (_, run):
            run["state"] = "running"
        Ledger(yielded.store, yielded.run_id).reserve(3, 4)
        with yielded.store.locked(yielded.run_id) as (connection, run):
            yielded.store.mark_pending_unknown(yielded.run_id, connection, run)
            run["state"] = "yielded"
        run = yielded.store.get(yielded.run_id)
        assert run["usage"]["unknown_calls"] == 1
        assert run["usage"]["reserved_tokens"] == 7
    clock, deadline = common_clock(monkeypatch)
    probes = observe_probe(
        monkeypatch,
        yielded,
        deadline,
        after=lambda: clock.__setitem__(0, deadline + 1),
    )
    digests = observe_digest(monkeypatch, deadline)
    before = state(yielded)
    assert not yielded.scheduler.resume_yielded(
        before["run"], yielded.workspace, deadline=deadline
    )
    assert probes == [yielded.run_id] and not digests
    after = assert_refused(yielded, before)
    assert not controller_cleanup_unknown(after["run"].get("recovery"))
    assert not yielded.scheduler.cleanup_blocked


@pytest.mark.parametrize("expired_digest", [1, 2])
def test_hash_return_expiry_never_enters_admission_transaction(
    yielded, monkeypatch, expired_digest
):
    clock, deadline = common_clock(monkeypatch)
    observe_probe(monkeypatch, yielded, deadline)
    digests = observe_digest(
        monkeypatch,
        deadline,
        after=lambda count: clock.__setitem__(0, deadline + 1)
        if count == expired_digest
        else None,
    )
    original = yielded.store.locked
    dispatches = []

    @contextmanager
    def locked(run_id, owner=None, *, dispatch=False):
        if dispatch:
            dispatches.append(run_id)
        with original(run_id, owner, dispatch=dispatch) as value:
            yield value

    monkeypatch.setattr(yielded.store, "locked", locked)
    before = state(yielded)
    assert not yielded.scheduler.resume_yielded(
        before["run"], yielded.workspace, deadline=deadline
    )
    assert len(digests) == expired_digest and not dispatches
    assert_refused(yielded, before)


def test_foreground_query_return_expiry_does_not_hash(yielded, monkeypatch):
    clock, deadline = common_clock(monkeypatch)
    probes = observe_probe(monkeypatch, yielded, deadline)
    digests = observe_digest(monkeypatch, deadline)
    original = scheduling.foreground_waiting

    def foreground(connection):
        assert probes == [yielded.run_id]
        result = original(connection)
        clock[0] = deadline + 1
        return result

    monkeypatch.setattr(scheduling, "foreground_waiting", foreground)
    before = state(yielded)
    assert not yielded.scheduler.resume_yielded(
        before["run"], yielded.workspace, deadline=deadline
    )
    assert not digests
    assert_refused(yielded, before)


def test_queued_foreground_still_gets_full_probe_before_optional_hash(
    yielded, monkeypatch
):
    request = deepcopy(yielded.store.get(yielded.run_id)["request"])
    request.pop("background")
    request.pop("arena")
    foreground = yielded.store.create_run(request)
    _, deadline = common_clock(monkeypatch)
    probes = observe_probe(monkeypatch, yielded, deadline)
    digests = observe_digest(monkeypatch, deadline)
    original = scheduling.foreground_waiting
    queries = []

    def waiting(connection):
        assert probes == [yielded.run_id]
        queries.append(foreground["id"])
        return original(connection)

    monkeypatch.setattr(scheduling, "foreground_waiting", waiting)
    before = state(yielded)
    assert not yielded.scheduler.resume_yielded(
        before["run"], yielded.workspace, deadline=deadline
    )
    assert queries == [foreground["id"]] and not digests
    assert state(yielded) == before


@pytest.mark.parametrize("arrival", ["first_hash", "validate"])
def test_foreground_committed_during_hash_stops_next_optional_stage(
    yielded, monkeypatch, arrival
):
    _, deadline = common_clock(monkeypatch)
    probes = observe_probe(monkeypatch, yielded, deadline)
    submitted = []
    arrival_hash = 1 if arrival == "first_hash" else 2

    def enqueue(count):
        assert probes == [yielded.run_id]
        if count == arrival_hash:
            request = deepcopy(yielded.store.get(yielded.run_id)["request"])
            request.pop("background")
            request.pop("arena")
            submitted.append(yielded.store.create_run(request)["id"])

    digests = observe_digest(monkeypatch, deadline, after=enqueue)
    original = yielded.scheduler.preflight
    checks = []

    def preflight(connection, run, *, yield_foreground=True, deadline=None):
        checks.append((yield_foreground, deadline))
        return original(
            connection, run, yield_foreground=yield_foreground, deadline=deadline
        )

    monkeypatch.setattr(yielded.scheduler, "preflight", preflight)
    before = state(yielded)
    assert not yielded.scheduler.resume_yielded(
        before["run"], yielded.workspace, deadline=deadline
    )
    after = state(yielded)
    assert len(submitted) == 1 and len(digests) == arrival_hash
    assert yielded.store.get(submitted[0])["state"] == "queued"
    assert checks == ([] if arrival == "first_hash" else [(False, deadline)])
    assert after["run"]["state"] == "yielded"
    assert ledger_checkpoint(after["run"]) == ledger_checkpoint(before["run"])
    assert after["run"]["request"] == before["run"]["request"]
    assert after["run"]["scheduling"] == before["run"]["scheduling"]
    for table in before["rows"]:
        if table != "runs":
            assert after["rows"][table] == before["rows"][table]
    assert after["events"] == before["events"]


@pytest.mark.parametrize(
    "stage",
    ["validate_preflight", "final_lock", "batch_guard", "final_preflight", "queue_event"],
)
def test_final_admission_expiry_rolls_back_queue_and_event(
    yielded, monkeypatch, stage
):
    clock, deadline = common_clock(monkeypatch)
    observe_probe(monkeypatch, yielded, deadline)
    observe_digest(monkeypatch, deadline)
    expired = []

    def expire():
        expired.append(stage)
        clock[0] = deadline + 1

    if stage in {"validate_preflight", "final_preflight"}:
        original = yielded.scheduler.preflight

        def preflight(connection, run, *, yield_foreground=True, deadline=None):
            assert deadline == limit
            result = original(
                connection, run, yield_foreground=yield_foreground, deadline=deadline
            )
            if yield_foreground == (stage == "final_preflight"):
                expire()
            return result

        limit = deadline
        monkeypatch.setattr(yielded.scheduler, "preflight", preflight)
    elif stage == "final_lock":
        original = yielded.store.locked
        dispatches = []

        @contextmanager
        def locked(run_id, owner=None, *, dispatch=False):
            with original(run_id, owner, dispatch=dispatch) as value:
                if dispatch:
                    dispatches.append(run_id)
                    if len(dispatches) == 2:
                        expire()
                yield value

        monkeypatch.setattr(yielded.store, "locked", locked)
    elif stage == "batch_guard":
        original = budget._locked_batch

        def batch(connection, run, *, dispatch=False):
            result = original(connection, run, dispatch=dispatch)
            assert dispatch
            expire()
            return result

        monkeypatch.setattr(budget, "_locked_batch", batch)
    else:
        original = yielded.store.event

        def event(run_id, kind, payload, connection=None):
            result = original(run_id, kind, payload, connection)
            if kind == "scheduling_resumed":
                assert connection is not None
                expire()
            return result

        monkeypatch.setattr(yielded.store, "event", event)
    before = state(yielded)
    assert not yielded.scheduler.resume_yielded(
        before["run"], yielded.workspace, deadline=deadline
    )
    assert expired == [stage]
    assert_refused(yielded, before)


def test_pure_probe_timeout_does_not_fabricate_cleanup_evidence(yielded, monkeypatch):
    _, deadline = common_clock(monkeypatch)
    error = ExecutionDeadlineExceeded("Controlled pure preparation timeout")
    probes = observe_probe(monkeypatch, yielded, deadline, error=error)
    before = state(yielded)
    assert not yielded.scheduler.resume_yielded(
        before["run"], yielded.workspace, deadline=deadline
    )
    assert probes == [yielded.run_id]
    after = assert_refused(yielded, before)
    assert after["run"].get("recovery") == before["run"].get("recovery")
    assert not controller_cleanup_unknown(after["run"].get("recovery"))
    assert not yielded.scheduler.cleanup_blocked


@pytest.mark.parametrize("cancelled", [False, True])
def test_timeout_daemon_chain_keeps_prior_real_operation_inventory(
    yielded, monkeypatch, cancelled
):
    run = yielded.store.get(yielded.run_id)
    operation = co.current_registry().reserve(
        "sandbox_probe",
        scope=co.scope(
            intent_digest=hashlib.sha256(b"Controlled prior probe intent").hexdigest(),
            run_label="doonex.run_id=" + run["id"],
        ),
        deadline_monotonic_ns=time.monotonic_ns() + 30_000_000_000,
        run_id=run["id"],
        **sandbox_context(run),
    )
    operation.mark_unknown()
    prior_receipt = {
        RECORD_UNKNOWN: True,
        "controller_operation_id": operation.operation_id,
    }
    prior_attempt = {"controlled_prior_attempt": True}
    with yielded.store.locked(run["id"]) as (_, current):
        current["recovery"] = {
            RECORD_UNKNOWN: True,
            "errors": [RECORD_UNKNOWN],
            "cleanup_evidence": prior_receipt,
            "cleanup_attempt_evidence": prior_attempt,
            "controller_process_evidence": [prior_receipt],
        }
    cause = SandboxError("Controlled daemon uncertainty")
    cause.cleanup_evidence = {
        "daemon_request_unknown": True,
        "controlled_probe_fault": True,
    }
    cause.cleanup_unknown = True
    error = ExecutionDeadlineExceeded("Controlled timeout with actual prior record")
    error.__cause__ = cause
    _, deadline = common_clock(monkeypatch)
    probes = observe_probe(
        monkeypatch,
        yielded,
        deadline,
        error=error,
        before_error=lambda: yielded.store.transition(
            run["id"], "cancelled", "Controlled concurrent API cancellation"
        )
        if cancelled
        else None,
    )
    before = state(yielded)
    assert not yielded.scheduler.resume_yielded(
        before["run"], yielded.workspace, deadline=deadline
    )
    assert probes == [yielded.run_id]
    if cancelled:
        after = state(yielded)
        assert after["run"]["state"] == "cancelled"
        assert ledger_checkpoint(after["run"]) == ledger_checkpoint(before["run"])
        assert after["run"]["request"] == before["run"]["request"]
        assert after["run"]["scheduling"] == before["run"]["scheduling"]
        for table in before["rows"]:
            if table != "runs":
                assert after["rows"][table] == before["rows"][table]
        assert after["events"][:-1] == before["events"]
        assert after["events"][-1]["kind"] == "cancelled"
    else:
        after = assert_refused(yielded, before)
    recovery = after["run"]["recovery"]
    assert recovery[RECORD_UNKNOWN] and recovery[DAEMON_UNKNOWN]
    assert recovery["cleanup_attempt_evidence"] == prior_attempt
    assert recovery["controller_process_evidence"] == [
        prior_receipt,
        cause.cleanup_evidence,
    ]
    assert recovery["cleanup_evidence"] == cause.cleanup_evidence
    assert yielded.scheduler.cleanup_blocked
    with yielded.store.connect() as connection:
        assert execution_fenced(connection)
        assert connection.execute(
            "SELECT data->>'state' AS state FROM controller_operations WHERE id=%s",
            (operation.operation_id,),
        ).fetchone()["state"] == "unknown"
