"""Controller registry contracts; no model or production database is used."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest
from doonex import controller_operations as co
from doonex.store import OwnershipLost, external_execution_fenced
from psycopg.types.json import Jsonb


def base_record(kind="git_export"):
    return {
        "schema": 1,
        "operation_id": str(uuid4()),
        "operation_kind": kind,
        "run_id": None,
        "project_id": None,
        "parent_action_id": None,
        "actor": co.fresh_actor("api").data(),
        "expected_run_owner": None,
        "scope": co.scope(
            intent_digest="a" * 64,
            repository_digest="b" * 64,
            container_name="doonex-fixture",
            run_label="doonex.run_id=fixture",
        ),
        "state": "reserved",
        "revision": 0,
        "previous_record_sha256": None,
        "business_deadline_monotonic_ns": time.monotonic_ns() + 60_000_000_000,
        "cleanup_allowance_seconds": 15,
        "spawn_possible": False,
        "attempted_processes": 0,
        "processes": [],
        "terminal_evidence": None,
        "reason": None,
        "repair_of": None,
        "record_sha256": "c" * 64,
    }


def evidence(*, docker=None, outcome="success"):
    return {
        "outcome": outcome,
        "reason": "success" if outcome == "success" else "command_failed",
        "local_process_unknown": False,
        "daemon_request_unknown": False,
        "docker": docker,
    }


def docker_evidence(*, attempted=False, remaining=0):
    return {
        "create_attempted": attempted,
        "create_acknowledged": attempted,
        "container_removed": attempted,
        "observation_complete": True,
        "run_label": "doonex.run_id=fixture",
        "remaining_containers": remaining,
    }


@pytest.mark.parametrize("kind", ["api", "worker", "cli"])
def test_live_actor_is_process_local_and_distinct(kind):
    first, second = co.fresh_actor(kind), co.fresh_actor(kind)
    first.check_live()
    assert first.owner != second.owner
    assert first.instance_nonce != second.instance_nonce
    with pytest.raises(co.RegistryError, match="actor_not_live"):
        co.Actor(**first.data()).check_live()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(extra="secret-shaped-input"),
        lambda r: r.update(schema=True),
        lambda r: r.update(operation_id="invalid"),
        lambda r: r.update(cleanup_allowance_seconds=61),
        lambda r: r["scope"].update(unbounded="body"),
        lambda r: r["actor"].update(instance_nonce="bad"),
        lambda r: r["actor"].update(instance_started_at="2026-11-14T12:00:00-05:00"),
        lambda r: r.update(state="terminal"),
        lambda r: r.update(spawn_possible=True),
        lambda r: r.update(processes=[{}]),
    ],
)
def test_record_rejects_invalid_fixed_envelope(mutation):
    record = base_record()
    mutation(record)
    with pytest.raises(co.RegistryError) as error:
        co._validate_record(record)
    assert "secret-shaped-input" not in str(error.value)


class NoDatabase:
    @contextmanager
    def connect(self):
        yield None


@pytest.fixture
def live_cap(monkeypatch):
    def create(kind="git_export", *, repair=False):
        record = base_record(kind)
        actor = co.fresh_actor("api")
        record.update(
            actor=actor.data(),
            state="launching",
            revision=1,
            spawn_possible=True,
            previous_record_sha256="d" * 64,
        )
        if repair:
            record["repair_of"] = str(uuid4())
        registry = co.ControllerRegistry(
            NoDatabase(),
            actor=actor,
            check_owner=lambda: None,
            check_other_fences=lambda connection: False,
        )
        operation = co.Operation(registry, record, co._LIVE)
        operation._locked = lambda connection: operation._record
        capability = co.LaunchCapability(operation, co._LIVE)
        operation._capability = capability
        monkeypatch.setattr(co, "_fenced", lambda *args: False)
        return operation, capability

    return create


def complete(capability, permit, *, pid=123, exit_code=0, reaped=True, pipe_eof=True):
    capability.after_process(
        permit, pid=pid, exit_code=exit_code, reaped=reaped, pipe_eof=pipe_eof
    )


def test_context_requires_binding_and_expires_old_capability(live_cap):
    operation, capability = live_cap()
    with pytest.raises(co.RegistryError, match="registry_missing"):
        co.current_registry()
    with co.bind_registry(operation.registry):
        assert co.current_registry() is operation.registry
    with pytest.raises(co.RegistryError, match="actor_closed"):
        capability.before_process("git")


def test_incomplete_slot_prevents_next_business_and_cannot_be_rewritten(live_cap):
    _, capability = live_cap()
    permit = capability.before_process("git")
    with pytest.raises(co.RegistryError, match="previous_process_unknown"):
        capability.before_process("git")
    complete(capability, permit, pid=None, exit_code=None, reaped=False, pipe_eof=False)
    with pytest.raises(co.RegistryError, match="receipt_used"):
        complete(capability, permit)


def test_process_count_is_finite(live_cap):
    _, capability = live_cap()
    for _ in range(co.MAX_PROCESSES):
        complete(capability, capability.before_process("git"))
    with pytest.raises(co.RegistryError, match="process_limit"):
        capability.before_process("git")


@pytest.mark.parametrize(
    "kind,phase,target",
    [
        ("git_export", "docker_ps", "doonex.run_id=fixture"),
        ("sandbox_probe", "docker_create", "doonex-fixture"),
        ("sandbox_cleanup", "docker_start", "doonex-fixture"),
        ("sandbox_execute", "docker_rm", "unobserved"),
        ("sandbox_execute", "docker_create", "wrong-name"),
        ("sandbox_execute", "docker_ps", "wrong-label"),
        ("sandbox_execute", "mount_validation", "unexpected"),
    ],
)
def test_capability_rejects_wrong_phase_or_target(live_cap, kind, phase, target):
    _, capability = live_cap(kind)
    with pytest.raises(co.RegistryError):
        capability.before_process(phase, target=target)


def test_repair_only_allows_ps_and_strictly_observed_rm(live_cap):
    _, capability = live_cap("sandbox_cleanup", repair=True)
    with pytest.raises(co.RegistryError, match="phase"):
        capability.before_process("docker_create", target="doonex-fixture")
    permit = capability.before_process("docker_ps", target="doonex.run_id=fixture")
    with pytest.raises(co.RegistryError):
        capability.observe_containers(
            permit, ["e" * 64], run_label="doonex.run_id=fixture", complete=True
        )
    complete(capability, permit)
    capability.observe_containers(
        permit, ["e" * 64], run_label="doonex.run_id=fixture", complete=True
    )
    capability.before_process("docker_rm", target="e" * 64)
    with pytest.raises(co.RegistryError):
        capability.observe_containers(
            permit, ["f" * 64], run_label="doonex.run_id=fixture", complete=True
        )


def test_other_fences_block_business_but_not_bounded_cleanup(live_cap, monkeypatch):
    operation, capability = live_cap("sandbox_execute")
    operation.registry.check_other_fences = lambda connection: True
    with pytest.raises(co.RegistryError, match="fenced"):
        capability.before_process("docker_create", target="doonex-fixture")
    capability.before_process("docker_rm", target="doonex-fixture")
    monkeypatch.setattr(
        co.time, "monotonic_ns", lambda: capability._cleanup_deadline_ns
    )
    with pytest.raises(co.RegistryError, match="deadline"):
        capability.before_process("docker_ps", target="doonex.run_id=fixture")


def test_owner_loss_blocks_new_cleanup_but_accepts_local_observation(live_cap):
    operation, capability = live_cap("sandbox_execute")
    permit = capability.before_process("docker_create", target="doonex-fixture")

    def lost():
        raise OwnershipLost("fixture owner changed")

    operation.registry.check_owner = lost
    complete(capability, permit)
    with pytest.raises(OwnershipLost):
        capability.before_process("docker_rm", target="doonex-fixture")


def test_clock_expiry_while_database_waits_returns_no_permit(live_cap, monkeypatch):
    operation, capability = live_cap()
    clock = [operation.record["business_deadline_monotonic_ns"] - 1]
    monkeypatch.setattr(co.time, "monotonic_ns", lambda: clock[0])

    def waited(connection):
        clock[0] += 1
        return operation._record

    operation._locked = waited
    with pytest.raises(co.RegistryError, match="deadline"):
        capability.before_process("git")
    assert capability._processes == []


def test_launch_expiry_after_commit_returns_no_capability(live_cap, monkeypatch):
    operation, _ = live_cap()
    operation._record.update(
        state="reserved", spawn_possible=False, revision=0, previous_record_sha256=None
    )
    operation._capability = None
    clock = [operation.record["business_deadline_monotonic_ns"] - 1]
    monkeypatch.setattr(co.time, "monotonic_ns", lambda: clock[0])

    def committed(state):
        operation._record.update(state=state, spawn_possible=True)
        clock[0] += 1

    operation._transition = committed
    with pytest.raises(co.RegistryError, match="deadline"):
        operation.launch()
    assert operation.record["state"] == "launching"
    assert operation._capability is None
    with pytest.raises(co.RegistryError, match="spawn_possible"):
        operation.not_spawned()


def test_live_owner_expiry_after_database_wait_returns_no_permit(live_cap):
    operation, capability = live_cap()

    def waited(connection):
        operation.registry.close()
        return operation._record

    operation._locked = waited
    with pytest.raises(co.RegistryError, match="actor_closed"):
        capability.before_process("git")
    assert capability._processes == []


def test_create_cannot_be_replayed_in_same_group(live_cap):
    _, capability = live_cap("sandbox_execute")
    complete(
        capability, capability.before_process("docker_create", target="doonex-fixture")
    )
    with pytest.raises(co.RegistryError, match="phase_used"):
        capability.before_process("docker_create", target="doonex-fixture")


def test_cleanup_cannot_reenter_business(live_cap):
    _, capability = live_cap("sandbox_execute")
    complete(
        capability,
        capability.before_process("docker_ps", target="doonex.run_id=fixture"),
    )
    with pytest.raises(co.RegistryError, match="cleanup_started"):
        capability.before_process("docker_create", target="doonex-fixture")


def test_same_process_actor_can_cross_threads_but_group_cannot_be_used_concurrently(
    live_cap,
):
    operation, capability = live_cap()
    outcomes = []
    operation._mutex.acquire()

    def busy():
        operation.registry.actor.check_live()
        try:
            capability.before_process("git")
        except co.RegistryError as error:
            outcomes.append(error.reason)

    thread = threading.Thread(target=busy)
    thread.start()
    thread.join(timeout=2)
    operation._mutex.release()
    assert not thread.is_alive() and outcomes == ["capability_busy"]
    thread = threading.Thread(
        target=lambda: outcomes.append(capability.before_process("git").seq)
    )
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and outcomes[-1] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("local_process_unknown", True),
        ("daemon_request_unknown", True),
        ("outcome", "free"),
        ("reason", "unbounded"),
    ],
)
def test_terminal_evidence_rejects_unknown_and_unfixed_fields(field, value):
    item = evidence()
    item[field] = value
    with pytest.raises(co.RegistryError):
        co._validate_evidence(item, base_record())


@pytest.fixture
def controller_store(store):
    with store.connect() as connection:
        co.initialize(connection)
    return store


def registry(
    store, *, kind="api", check_owner=lambda: None, other=external_execution_fenced
):
    return co.ControllerRegistry(
        store,
        actor=co.fresh_actor(kind),
        check_owner=check_owner,
        check_other_fences=other,
    )


def reserve(store, *, kind="git_export", registry_instance=None, **kwargs):
    return (registry_instance or registry(store)).reserve(
        kind,
        scope=base_record(kind)["scope"],
        deadline_monotonic_ns=time.monotonic_ns() + 60_000_000_000,
        **kwargs,
    )


def is_fenced(store):
    with store.connect() as connection:
        return co.fenced(connection)


def row(store, operation):
    with store.connect() as connection:
        return connection.execute(
            "SELECT * FROM controller_operations WHERE id=%s", (operation.operation_id,)
        ).fetchone()


def finished(store, *, kind="git_export", outcome="success"):
    operation = reserve(store, kind=kind)
    capability = operation.launch()
    phase = "git" if kind.startswith("git_") else "docker_ps"
    permit = capability.before_process(
        phase, target=None if phase == "git" else "doonex.run_id=fixture"
    )
    complete(capability, permit)
    operation.finish(
        evidence(docker=None if phase == "git" else docker_evidence(), outcome=outcome)
    )
    return operation


def test_pg_reserve_and_gate_are_visible_on_another_connection(controller_store):
    store = controller_store
    operation = reserve(store)
    assert row(store, operation)["data"]["state"] == "reserved"
    assert is_fenced(store)
    with pytest.raises(co.RegistryError, match="fenced"):
        reserve(store)
    capability = operation.launch()
    assert row(store, operation)["data"]["state"] == "launching"
    assert is_fenced(store)
    permit = capability.before_process("git")
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "print('receipt')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output, _ = process.communicate(timeout=5)
    capability.after_process(
        permit,
        pid=process.pid,
        exit_code=process.returncode,
        reaped=process.returncode is not None,
        pipe_eof=output == b"receipt\n",
    )
    operation.finish(evidence())
    assert not is_fenced(store)
    assert row(store, operation)["data"]["processes"][0]["pid"] == process.pid


def test_pg_not_spawned_is_only_pre_gate_and_failure_is_safe_terminal(controller_store):
    operation = reserve(controller_store)
    operation.not_spawned()
    assert not is_fenced(controller_store)
    operation = finished(controller_store, outcome="failed")
    assert (
        row(controller_store, operation)["data"]["terminal_evidence"]["outcome"]
        == "failed"
    )
    assert not is_fenced(controller_store)
    with pytest.raises(co.RegistryError, match="spawn_possible"):
        operation.not_spawned()


def test_pg_not_spawned_rejects_fractional_process_count_even_with_valid_hash(
    controller_store,
):
    operation = reserve(controller_store)
    operation.not_spawned()
    changed = row(controller_store, operation)["data"]
    changed["attempted_processes"] = 0.0
    with controller_store.connect() as connection:
        changed["record_sha256"] = co._digest(connection, changed)
        connection.execute(
            "UPDATE controller_operations SET data=%s WHERE id=%s",
            (Jsonb(changed), operation.operation_id),
        )
    assert is_fenced(controller_store)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(schema=2),
        lambda r: r.update(schema=1.0),
        lambda r: r.update(operation_id=str(uuid4())),
        lambda r: r.update(parent_action_id=str(uuid4())),
        lambda r: r.update(project_id="invalid"),
        lambda r: r.update(state="acknowledged"),
        lambda r: r.update(revision=1),
        lambda r: r.update(spawn_possible=False),
        lambda r: r.update(attempted_processes=0),
        lambda r: r["processes"][0].update(pipe_eof=False),
        lambda r: r["processes"][0].update(reaped=False),
        lambda r: r["processes"][0].update(pid=None),
        lambda r: r["processes"][0].update(exit_code=None),
        lambda r: r["processes"][0].update(seq=2),
        lambda r: r["processes"][0].update(phase="docker_create"),
        lambda r: r["terminal_evidence"].update(daemon_request_unknown=True),
        lambda r: r["terminal_evidence"].update(extra="forbidden"),
        lambda r: r["actor"].update(instance_started_at="2026-02-31T00:00:00+00:00"),
        lambda r: r["scope"].update(repository_digest="invalid"),
    ],
)
def test_pg_invalid_terminal_with_recomputed_hash_remains_fenced(
    controller_store, mutation
):
    operation = finished(controller_store)
    changed = row(controller_store, operation)["data"]
    mutation(changed)
    with controller_store.connect() as connection:
        changed["record_sha256"] = co._digest(connection, changed)
        connection.execute(
            "UPDATE controller_operations SET data=%s WHERE id=%s",
            (Jsonb(changed), operation.operation_id),
        )
    assert is_fenced(controller_store)


@pytest.mark.parametrize("payload", [None, {}, [], "invalid", {"state": "terminal"}])
def test_pg_missing_or_scalar_envelope_is_fenced(controller_store, payload):
    with controller_store.connect() as connection:
        connection.execute(
            "INSERT INTO controller_operations(id,data) VALUES(%s,%s)",
            (str(uuid4()), Jsonb(payload)),
        )
    assert is_fenced(controller_store)


def test_pg_wrong_hash_and_missing_row_reject_existing_capability(controller_store):
    operation = reserve(controller_store)
    capability = operation.launch()
    changed = row(controller_store, operation)["data"]
    changed["record_sha256"] = "0" * 64
    with controller_store.connect() as connection:
        connection.execute(
            "UPDATE controller_operations SET data=%s WHERE id=%s",
            (Jsonb(changed), operation.operation_id),
        )
    assert is_fenced(controller_store)
    with pytest.raises(co.RegistryError, match="record_binding"):
        capability.before_process("git")
    with controller_store.connect() as connection:
        connection.execute(
            "DELETE FROM controller_operations WHERE id=%s", (operation.operation_id,)
        )
    with pytest.raises(co.RegistryError, match="record_missing"):
        capability.before_process("git")
    # An entirely deleted row cannot be discovered by a table scan. Production
    # Popen coverage/DB access policy, not an invented history, guards this case.
    assert not is_fenced(controller_store)


def test_pg_unknown_is_independent_of_run_and_action_acknowledgement(
    controller_store, request_data
):
    run = controller_store.create_run(request_data)
    with controller_store.locked(run["id"]) as (connection, current):
        current.update(owner="owner-fixture", state="running")
        action_id = controller_store.action(
            run["id"], "shell", {"command": "fixture"}, connection
        )
    operation = reserve(
        controller_store,
        run_id=run["id"],
        project_id=request_data["project_id"],
        parent_action_id=action_id,
        expected_run_owner="owner-fixture",
    )
    operation.launch()
    operation.mark_unknown()
    with controller_store.connect() as connection:
        connection.execute(
            "UPDATE actions SET status='acknowledged' WHERE id=%s", (action_id,)
        )
        connection.execute(
            "UPDATE runs SET data=data || %s WHERE id=%s",
            (Jsonb({"state": "cancelled", "recovery": {}}), run["id"]),
        )
    assert is_fenced(controller_store)
    with controller_store.connect() as connection:
        assert (
            co.list_unresolved(connection)["items"][0]["id"] == operation.operation_id
        )


def test_pg_new_run_owner_cannot_finish_old_group(controller_store, request_data):
    run = controller_store.create_run(request_data)
    with controller_store.locked(run["id"]) as (_, current):
        current["owner"] = "owner-before"
    operation = reserve(
        controller_store,
        run_id=run["id"],
        project_id=request_data["project_id"],
        expected_run_owner="owner-before",
    )
    capability = operation.launch()
    complete(capability, capability.before_process("git"))
    with controller_store.locked(run["id"]) as (_, current):
        current["owner"] = "owner-after"
    with pytest.raises(OwnershipLost):
        operation.finish(evidence())
    assert is_fenced(controller_store)


def test_pg_repair_terminal_does_not_resolve_original_unknown(controller_store):
    original = reserve(controller_store, kind="sandbox_execute")
    original.launch()
    original.mark_unknown("daemon_unknown")
    repair = registry(controller_store).repair_only(
        original.operation_id,
        deadline_monotonic_ns=time.monotonic_ns() + 30_000_000_000,
    )
    capability = repair.launch()
    permit = capability.before_process("docker_ps", target="doonex.run_id=fixture")
    complete(capability, permit)
    capability.observe_containers(
        permit, [], run_label="doonex.run_id=fixture", complete=True
    )
    repair.finish(evidence(docker=docker_evidence()))
    assert is_fenced(controller_store)
    assert row(controller_store, original)["data"]["state"] == "unknown"
    assert row(controller_store, repair)["data"]["state"] == "terminal"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(create_acknowledged=False),
        lambda d: d.update(container_removed=False),
        lambda d: d.update(observation_complete=False),
        lambda d: d.update(remaining_containers=1),
    ],
)
def test_pg_daemon_or_container_uncertainty_cannot_clear_group(
    controller_store, mutation
):
    operation = reserve(controller_store, kind="sandbox_execute")
    capability = operation.launch()
    complete(
        capability, capability.before_process("docker_create", target="doonex-fixture")
    )
    docker = docker_evidence(attempted=True)
    mutation(docker)
    with pytest.raises(co.RegistryError):
        operation.finish(evidence(docker=docker))
    assert is_fenced(controller_store)


def test_pg_explicit_legacy_marker_is_idempotent_and_has_no_fabricated_actor(
    controller_store, request_data
):
    run = controller_store.create_run(request_data)
    with controller_store.locked(run["id"]) as (connection, _):
        action_id = controller_store.action(
            run["id"], "shell", {"command": "fixture"}, connection
        )
    with controller_store.connect() as connection:
        first = co.register_legacy_unknown(connection, action_id=action_id)
    with controller_store.connect() as connection:
        assert co.register_legacy_unknown(connection, action_id=action_id) == first
        connection.execute(
            "UPDATE actions SET status='acknowledged' WHERE id=%s", (action_id,)
        )
    assert first["actor"] is None and first["processes"] == []
    assert first["business_deadline_monotonic_ns"] is None
    assert is_fenced(controller_store)


def test_pg_index_is_used_and_drift_is_rejected(controller_store):
    with controller_store.connect() as connection:
        co.initialize(connection)
        connection.execute("SET LOCAL enable_seqscan=off")
        plan = connection.execute(
            f"EXPLAIN SELECT id FROM controller_operations WHERE {co.FENCE_PREDICATE} ORDER BY id LIMIT 1"
        ).fetchall()
        assert "controller_operations_fenced_v1" in json.dumps(plan)
    with controller_store.connect() as connection:
        connection.execute("DROP INDEX controller_operations_fenced_v1")
        connection.execute(
            "CREATE INDEX controller_operations_fenced_v1 ON controller_operations(id) WHERE FALSE"
        )
    with (
        pytest.raises(co.RegistryError, match="index_drift"),
        controller_store.connect() as connection,
    ):
        co.initialize(connection)


def test_pg_function_drift_is_rejected(controller_store):
    with controller_store.connect() as connection:
        connection.execute(
            f"ALTER FUNCTION {co._FUNCTION}(uuid,uuid,uuid,jsonb) STABLE"
        )
    with (
        pytest.raises(co.RegistryError, match="function_drift"),
        controller_store.connect() as connection,
    ):
        co.initialize(connection)


def test_pg_table_drift_is_rejected(controller_store):
    with controller_store.connect() as connection:
        connection.execute(
            "ALTER TABLE controller_operations ALTER COLUMN data DROP NOT NULL"
        )
    with (
        pytest.raises(co.RegistryError, match="schema_drift"),
        controller_store.connect() as connection,
    ):
        co.initialize(connection)


def test_pg_terminal_cannot_hide_an_attempted_create_in_receipts(controller_store):
    operation = finished(controller_store, kind="sandbox_execute")
    assert not is_fenced(controller_store)
    changed = row(controller_store, operation)["data"]
    changed["processes"][0].update(phase="docker_create", target="doonex-fixture")
    with controller_store.connect() as connection:
        changed["record_sha256"] = co._digest(connection, changed)
        connection.execute(
            "UPDATE controller_operations SET data=%s WHERE id=%s",
            (Jsonb(changed), operation.operation_id),
        )
    assert is_fenced(controller_store)


def test_pg_parent_action_must_belong_to_exact_run(controller_store, request_data):
    first = controller_store.create_run(request_data)
    second = controller_store.create_run(request_data)
    action_id = controller_store.action(first["id"], "shell", {"command": "fixture"})
    with pytest.raises(co.RegistryError, match="action_binding"):
        reserve(
            controller_store,
            run_id=second["id"],
            project_id=request_data["project_id"],
            parent_action_id=action_id,
        )


def test_pg_new_actor_cannot_inherit_existing_group(controller_store):
    operation = reserve(controller_store)
    operation.launch()
    impostor = co.Operation(registry(controller_store), operation.record, co._LIVE)
    with pytest.raises(co.RegistryError, match="actor_binding"):
        impostor.mark_unknown()
    assert is_fenced(controller_store)


class LostCommitStore:
    def __init__(self, store):
        self.store, self.lose_next = store, False

    @contextmanager
    def connect(self):
        with self.store.connect() as connection:
            yield connection
        if self.lose_next:
            self.lose_next = False
            raise OSError("synthetic lost commit acknowledgement")


def test_pg_lost_reserve_ack_never_returns_capability_or_replays(controller_store):
    wrapped = LostCommitStore(controller_store)
    actor_registry = registry(wrapped)
    wrapped.lose_next = True
    with pytest.raises(co.RegistryError, match="reserve_commit_unknown") as error:
        reserve(wrapped, registry_instance=actor_registry)
    assert error.value.operation_id
    assert is_fenced(controller_store)
    with pytest.raises(co.RegistryError, match="commit_unknown"):
        reserve(wrapped, registry_instance=actor_registry)


def test_pg_lost_gate_ack_never_returns_live_capability(controller_store):
    wrapped = LostCommitStore(controller_store)
    operation = reserve(wrapped)
    wrapped.lose_next = True
    with pytest.raises(co.RegistryError, match="transition_commit_unknown"):
        operation.launch()
    assert operation._capability is None
    assert row(controller_store, operation)["data"]["state"] == "launching"
    with pytest.raises(co.RegistryError, match="launch_used"):
        operation.launch()
    assert is_fenced(controller_store)


def test_pg_lost_terminal_ack_accepts_only_same_receipt_without_process_replay(
    controller_store,
):
    wrapped = LostCommitStore(controller_store)
    operation = reserve(wrapped)
    capability = operation.launch()
    complete(capability, capability.before_process("git"))
    wrapped.lose_next = True
    with pytest.raises(co.RegistryError, match="transition_commit_unknown"):
        operation.finish(evidence())
    assert not is_fenced(controller_store)
    with pytest.raises(co.RegistryError, match="receipt_changed"):
        operation.finish(evidence(outcome="failed"))
    assert operation.finish(evidence())["state"] == "terminal"
    assert len(operation.record["processes"]) == 1


def test_pg_killed_actor_after_gate_leaves_independent_fence(controller_store):
    script = """
import os, signal, time
from doonex.store import Store
from doonex.controller_operations import ControllerRegistry, fresh_actor, scope
store=Store(os.environ['DOONEX_CONTROLLER_TEST_DSN'])
registry=ControllerRegistry(store,actor=fresh_actor('cli'),check_owner=lambda:None,check_other_fences=lambda connection:False)
operation=registry.reserve('git_validate',scope=scope(intent_digest='a'*64,repository_digest='b'*64),deadline_monotonic_ns=time.monotonic_ns()+60_000_000_000)
operation.launch()
print(operation.operation_id,flush=True)
signal.pause()
"""
    environment = {
        **os.environ,
        "DOONEX_CONTROLLER_TEST_DSN": controller_store.database_url,
    }
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        import selectors

        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10)
            operation_id = process.stdout.readline().strip()
        assert operation_id
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)
        assert is_fenced(controller_store)
        with controller_store.connect() as connection:
            record = connection.execute(
                "SELECT data FROM controller_operations WHERE id=%s", (operation_id,)
            ).fetchone()["data"]
        assert record["state"] == "launching" and record["spawn_possible"] is True
        assert record["processes"] == []
        with pytest.raises(co.RegistryError, match="fenced"):
            reserve(controller_store)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
