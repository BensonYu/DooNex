"""Sandbox capability wiring with real child processes, never a Docker daemon.

The memory registry validates the real fixed records/capabilities but is not a
durability substitute; PostgreSQL registry contracts live in their own suite.
"""

import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from doonex import controller_operations as co
from doonex import sandbox as implementation
from doonex.sandbox import Sandbox, SandboxError, _Cleanup, _evidence, _Supervisor
from doonex.store import OwnershipLost
from test_controller_operations import controller_store as store_fixture
from test_sandbox_deadline import FAKE_DOCKER

controller_store = store_fixture


@pytest.fixture
def controller(monkeypatch):
    class MemoryStore:
        @contextmanager
        def connect(self):
            yield None

    state = SimpleNamespace(
        operations=[], events=[], foreign_fence=False, owner="owner"
    )

    def check_owner():
        if state.owner == "lost":
            raise OwnershipLost("Synthetic controller owner changed")

    registry = co.ControllerRegistry(
        MemoryStore(),
        actor=co.fresh_actor("api"),
        check_owner=check_owner,
        check_other_fences=lambda connection: state.foreign_fence,
    )
    monkeypatch.setattr(co, "_fenced", lambda *args: False)

    def reserve(kind, *, repair_of=None, **arguments):
        registry._live()
        if registry._uncertain:
            raise co.RegistryError("commit_unknown")
        if repair_of is None and (
            state.foreign_fence
            or any(
                item.record["state"] in {"reserved", "launching", "unknown"}
                for item in state.operations
            )
        ):
            raise co.RegistryError("fenced")
        if arguments["expected_run_owner"] != state.owner:
            raise OwnershipLost("Synthetic Run snapshot does not match")
        record = dict(
            schema=1,
            operation_id=str(uuid4()),
            operation_kind=kind,
            actor=registry.actor.data(),
            state="reserved",
            revision=0,
            previous_record_sha256=None,
            spawn_possible=False,
            attempted_processes=0,
            processes=[],
            terminal_evidence=None,
            reason=None,
            repair_of=repair_of,
            record_sha256="a" * 64,
            **copy.deepcopy(arguments),
        )
        record["business_deadline_monotonic_ns"] = record.pop("deadline_monotonic_ns")
        co._validate_record(record)
        if record["business_deadline_monotonic_ns"] <= time.monotonic_ns():
            raise co.RegistryError("deadline")
        operation = co.Operation(registry, record, co._LIVE)
        operation._locked = lambda connection: operation._record

        def transition(new_state, *, processes=None, evidence=None, reason=None):
            registry._live()
            previous = operation.record
            proposed = {
                **previous,
                "state": new_state,
                "revision": previous["revision"] + 1,
                "previous_record_sha256": previous["record_sha256"],
                "record_sha256": "b" * 64,
                "reason": reason,
                "terminal_evidence": copy.deepcopy(evidence),
            }
            if new_state == "launching":
                proposed["spawn_possible"] = True
            if processes is not None:
                proposed.update(
                    processes=copy.deepcopy(processes),
                    attempted_processes=len(processes),
                )
            co._validate_record(proposed)
            state.events.append((new_state, operation.operation_id))
            operation._record = proposed
            return operation.record

        operation._transition = transition
        state.operations.append(operation)
        state.events.append(("reserved", operation.operation_id))
        return operation

    def repair(original_id, *, deadline_monotonic_ns, expected_run_owner=None):
        original = next(
            operation
            for operation in state.operations
            if operation.operation_id == original_id
        ).record
        if original["state"] not in {"reserved", "launching", "unknown"}:
            raise co.RegistryError("repair_scope")
        return reserve(
            "sandbox_cleanup",
            repair_of=original_id,
            scope=original["scope"],
            run_id=original["run_id"],
            project_id=original["project_id"],
            parent_action_id=None,
            expected_run_owner=expected_run_owner,
            deadline_monotonic_ns=deadline_monotonic_ns,
            cleanup_allowance_seconds=15,
        )

    monkeypatch.setattr(registry, "reserve", reserve)
    monkeypatch.setattr(registry, "repair_only", repair)
    state.registry = registry
    with co.bind_registry(registry):
        yield state


@pytest.fixture
def fake(tmp_path, monkeypatch, controller):
    control = tmp_path / "control"
    control.mkdir()
    executable = control / "docker"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    executable.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 0.5)

    def configure(**scenario):
        (control / "scenario.json").write_text(json.dumps(scenario))
        return Sandbox(docker=str(executable))

    value = SimpleNamespace(
        control=control,
        workspace=workspace,
        configure=configure,
        run_id=str(uuid4()),
        binding={
            "expected_run_owner": "owner",
            "project_id": str(uuid4()),
            "parent_action_id": str(uuid4()),
        },
        controller=controller,
    )
    yield value
    for name in ["child.pid", "daemon.pid"]:
        path = control / name
        if path.exists():
            try:
                os.kill(int(path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def commands(fake):
    path = fake.control / "operations.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def run(fake, **scenario):
    return fake.configure(**scenario).run(
        fake.workspace, "true", 10, fake.run_id, **fake.binding
    )


def supervisor(controller):
    operation = controller.registry.reserve(
        "sandbox_execute",
        scope=co.scope(
            intent_digest="a" * 64,
            container_name="doonex-fixture",
            run_label="doonex.run_id=" + str(uuid4()),
        ),
        deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
        run_id=str(uuid4()),
        project_id=None,
        parent_action_id=None,
        expected_run_owner="owner",
        cleanup_allowance_seconds=15,
    )
    capability = operation.launch()
    return operation, _Supervisor(_evidence(), _Cleanup(), capability=capability)


def test_execute_group_binds_every_real_process_before_launch_and_exact_targets(fake):
    result = run(fake)
    assert result["exit_code"] == 0 and not result["cleanup_unknown"]
    operation = fake.controller.operations[0]
    record = operation.record
    assert record["state"] == "terminal"
    assert record["run_id"] == fake.run_id
    for name, value in fake.binding.items():
        assert record[name] == value
    assert [item["phase"] for item in record["processes"]] == [
        "mount_validation",
        "docker_create",
        "docker_start",
        "docker_inspect",
        "docker_rm",
        "docker_ps",
    ]
    name = record["scope"]["container_name"]
    for command in commands(fake):
        if command["operation"] in {"start", "inspect", "rm"}:
            assert command["arguments"][-1] == name
    for item in record["processes"]:
        assert item["pid"] and item["reaped"] and item["pipe_eof"]
        with pytest.raises(ChildProcessError):
            os.waitpid(item["pid"], os.WNOHANG)
    assert record["terminal_evidence"]["docker"] == {
        "create_attempted": True,
        "create_acknowledged": True,
        "container_removed": True,
        "observation_complete": True,
        "run_label": "doonex.run_id=" + fake.run_id,
        "remaining_containers": 0,
    }


@pytest.mark.parametrize("method", ["run", "containers", "stop_run"])
def test_high_level_requires_explicit_run_binding_before_any_process(fake, method):
    sandbox = fake.configure()
    with pytest.raises(co.RegistryError, match="run_owner_missing"):
        if method == "run":
            sandbox.run(fake.workspace, "true", 10, fake.run_id)
        else:
            getattr(sandbox, method)(fake.run_id)
    assert fake.controller.operations == [] and commands(fake) == []


def test_explicit_nullable_actual_owner_is_preserved(fake):
    fake.controller.owner = None
    fake.binding["expected_run_owner"] = None
    assert run(fake)["exit_code"] == 0
    assert fake.controller.operations[0].record["expected_run_owner"] is None


def test_wrong_owner_and_unbound_registry_cannot_spawn(fake, monkeypatch):
    fake.binding["expected_run_owner"] = "different"
    with pytest.raises(OwnershipLost):
        run(fake)
    monkeypatch.setattr(
        implementation, "current_registry", lambda: co.current_registry()
    )
    token = co._CURRENT.set(None)
    try:
        fake.binding["expected_run_owner"] = "owner"
        with pytest.raises(co.RegistryError, match="registry_missing"):
            run(fake)
    finally:
        co._CURRENT.reset(token)
    assert commands(fake) == []


def test_low_level_docker_has_no_anonymous_supervisor_fallback(fake):
    with pytest.raises(co.RegistryError, match="capability"):
        fake.configure()._docker("ps", "-aq", "--no-trunc", "--filter", "label=x")
    with pytest.raises(co.RegistryError, match="capability"):
        _Supervisor(_evidence(), _Cleanup(), capability=object())
    assert commands(fake) == []


def test_supervisor_requires_same_explicit_capability(controller, monkeypatch):
    _operation, service = supervisor(controller)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Popen without cap")
    )
    with pytest.raises(co.RegistryError, match="capability"):
        service.process(
            [sys.executable, "-c", "pass"],
            deadline=time.monotonic() + 1,
            operation="mount_validation",
            maximum=1024,
            capability=object(),
        )


def test_deadline_after_checked_process_permit_never_spawns(controller, monkeypatch):
    operation, service = supervisor(controller)
    clock = [100.0]
    original = service.capability.before_process

    def before(*args, **kwargs):
        permit = original(*args, **kwargs)
        clock[0] = 102.0
        return permit

    monkeypatch.setattr(service.capability, "before_process", before)
    monkeypatch.setattr(implementation.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("expired Popen")
    )
    result = service.process(
        [sys.executable, "-c", "pass"],
        deadline=101.0,
        operation="mount_validation",
        maximum=1024,
        capability=service.capability,
    )
    assert result.reason == "timeout" and service.evidence["local_process_unknown"]
    process = service.capability._processes[0]
    assert process["pid"] is None and not process["reaped"] and not process["pipe_eof"]
    implementation._settle(operation, service, reason="deadline")
    assert operation.record["state"] == "unknown"


@pytest.mark.parametrize("change", ["close", "context", "owner"])
def test_false_callback_cannot_spawn_after_live_authority_changes(
    controller, monkeypatch, change
):
    _operation, service = supervisor(controller)
    calls, context_tokens = [], []

    def should_stop():
        calls.append(1)
        if len(calls) == 2:
            if change == "close":
                controller.registry.close()
            elif change == "owner":
                controller.owner = "lost"
            else:
                other = co.ControllerRegistry(
                    controller.registry.store,
                    actor=co.fresh_actor("api"),
                    check_owner=lambda: None,
                    check_other_fences=lambda connection: False,
                )
                context_tokens.append(co._CURRENT.set(other))
        return False

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("stale authority spawned"),
    )
    try:
        with pytest.raises(OwnershipLost if change == "owner" else co.RegistryError):
            service.process(
                [sys.executable, "-c", "pass"],
                deadline=time.monotonic() + 1,
                operation="mount_validation",
                maximum=1024,
                capability=service.capability,
                should_stop=should_stop,
            )
    finally:
        for token in reversed(context_tokens):
            co._CURRENT.reset(token)
    assert len(calls) == 2
    if change == "context":
        assert service.evidence["local_process_unknown"]
        process = service.capability._processes[0]
        assert (
            process["pid"] is None and not process["reaped"] and not process["pipe_eof"]
        )
    else:
        assert service.capability._processes == []


@pytest.mark.parametrize("ack", ["bad\n", "a" * 64 + "\nextra\n", ""])
def test_invalid_create_ack_cannot_clear_group_with_empty_final_ps(fake, ack):
    with pytest.raises(SandboxError) as error:
        run(fake, create_output=ack)
    record = fake.controller.operations[0].record
    assert error.value.cleanup_unknown and record["state"] == "unknown"
    assert error.value.cleanup_evidence["daemon_request_unknown"]
    assert commands(fake)[-1]["operation"] == "ps"


@pytest.mark.parametrize(
    "output", ["short\n", "a" * 64 + "\n" + "a" * 64 + "\n", "x" * 5000]
)
def test_probe_records_unknown_on_malformed_or_incomplete_ps(fake, output):
    with pytest.raises(SandboxError) as error:
        fake.configure(ps_output=output).containers(fake.run_id, **fake.binding)
    assert error.value.cleanup_unknown
    assert fake.controller.operations[0].record["state"] == "unknown"


def test_probe_can_finish_with_exact_observed_nonempty_list(fake):
    ids = ["a" * 64, "b" * 64]
    result = fake.configure(ps_output="\n".join(ids) + "\n").containers(
        fake.run_id, **fake.binding
    )
    assert result == ids
    operation = fake.controller.operations[0]
    assert operation.record["operation_kind"] == "sandbox_probe"
    assert operation.record["state"] == "terminal"
    assert operation.record["terminal_evidence"]["docker"]["remaining_containers"] == 2
    assert operation._capability._observed == set(ids)


def test_cleanup_uses_only_actual_observed_ids_and_complete_final_ps(fake):
    (fake.control / "container").write_text("existing")
    result = fake.configure().stop_run(fake.run_id, **fake.binding)
    assert result["stopped"] == ["a" * 64] and result["remaining"] == []
    assert not result["cleanup_unknown"]
    record = fake.controller.operations[0].record
    assert record["state"] == "terminal"
    assert [item["phase"] for item in record["processes"]] == [
        "docker_ps",
        "docker_rm",
        "docker_ps",
    ]
    assert record["processes"][1]["target"] == "a" * 64


def test_old_unknown_blocks_normal_cleanup_but_repair_never_clears_original(fake):
    with pytest.raises(SandboxError):
        run(fake, create_output="bad\n")
    original = fake.controller.operations[0]
    before = original.record
    with pytest.raises(co.RegistryError, match="fenced"):
        fake.configure().stop_run(fake.run_id, **fake.binding)
    binding = {k: v for k, v in fake.binding.items() if k != "parent_action_id"}
    result = fake.configure().stop_run(
        fake.run_id, repair_of=original.operation_id, **binding
    )
    assert not result["cleanup_unknown"] and original.record == before
    repair = fake.controller.operations[-1].record
    assert (
        repair["repair_of"] == original.operation_id and repair["state"] == "terminal"
    )
    assert all(
        item["phase"] in {"docker_ps", "docker_rm"} for item in repair["processes"]
    )
    with pytest.raises(co.RegistryError, match="fenced"):
        fake.configure().containers(fake.run_id, **fake.binding)


def test_other_global_fence_blocks_first_sandbox_process(fake):
    fake.controller.foreign_fence = True
    with pytest.raises(co.RegistryError, match="fenced"):
        run(fake)
    assert fake.controller.operations == [] and commands(fake) == []


def test_lost_terminal_commit_is_unknown_and_not_replayed(fake, monkeypatch):
    original = fake.controller.registry.reserve

    def reserve(*args, **kwargs):
        operation = original(*args, **kwargs)
        transition = operation._transition

        def commit(state, **payload):
            if state == "terminal":
                operation.registry._uncertain = True
                raise co.RegistryError(
                    "transition_commit_unknown", operation_id=operation.operation_id
                )
            return transition(state, **payload)

        operation._transition = commit
        return operation

    monkeypatch.setattr(fake.controller.registry, "reserve", reserve)
    with pytest.raises(co.RegistryError, match="transition_commit_unknown") as error:
        run(fake)
    assert (
        error.value.cleanup_unknown
        and fake.controller.operations[0].record["state"] == "launching"
    )
    before = commands(fake)
    with pytest.raises(co.RegistryError, match="commit_unknown"):
        run(fake)
    assert commands(fake) == before


@pytest.mark.parametrize(
    "arguments,operation,target",
    [
        (["docker", "start", "-a", "different"], "start", "doonex-fixture"),
        (["docker", "rm", "-f", "different"], "rm", "doonex-fixture"),
        (["docker", "start", "-a", "doonex-fixture"], "inspect", "doonex-fixture"),
        (["docker", "exec", "doonex-fixture", "true"], "start", "doonex-fixture"),
    ],
)
def test_process_cannot_spoof_receipt_target_or_docker_phase(
    controller, monkeypatch, arguments, operation, target
):
    _operation, service = supervisor(controller)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("scope bypass Popen")
    )
    with pytest.raises(co.RegistryError, match="scope"):
        service.process(
            arguments,
            deadline=time.monotonic() + 1,
            operation=operation,
            target=target,
            maximum=1024,
            capability=service.capability,
        )
    assert service.capability._processes == []


def test_create_ack_timeout_retains_daemon_unknown_after_complete_empty_ps(fake):
    sandbox = fake.configure(create_delay=2)
    result = sandbox.run(fake.workspace, "true", 0.6, fake.run_id, **fake.binding)
    assert result["timed_out"] and result["cleanup_unknown"]
    assert result["cleanup_evidence"]["daemon_request_unknown"]
    assert result["cleanup_evidence"]["container_snapshot_absence_confirmed"]
    record = fake.controller.operations[0].record
    assert record["state"] == "unknown" and record["reason"] == "daemon_unknown"
    assert all(item["reaped"] and item["pipe_eof"] for item in record["processes"])


def test_actual_escaped_inherited_pipe_never_counts_as_eof(
    controller, monkeypatch, tmp_path
):
    operation, service = supervisor(controller)
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 0.2)
    pid_file = tmp_path / "escaped.pid"
    script = (
        "import os,time,pathlib; pid=os.fork(); "
        "\nif pid==0:\n os.setsid(); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30); os._exit(0)\n"
    )
    try:
        result = service.process(
            [sys.executable, "-c", script],
            deadline=time.monotonic() + 0.15,
            operation="mount_validation",
            maximum=1024,
            capability=service.capability,
        )
        assert result.reason == "timeout"
        receipt = service.capability._processes[0]
        assert receipt["reaped"] and not receipt["pipe_eof"]
        assert service.evidence["local_process_unknown"]
        implementation._settle(operation, service, reason="deadline")
        assert operation.record["state"] == "unknown"
        assert operation.record["processes"][0]["pipe_eof"] is False
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_popen_failure_has_no_invented_pid_reap_or_eof(controller, monkeypatch):
    operation, service = supervisor(controller)

    def fail(*args, **kwargs):
        raise FileNotFoundError("Synthetic unavailable executable")

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(SandboxError):
        service.process(
            [sys.executable, "-c", "pass"],
            deadline=time.monotonic() + 1,
            operation="mount_validation",
            maximum=1024,
            capability=service.capability,
        )
    implementation._settle(operation, service, reason="operation_failed")
    process = operation.record["processes"][0]
    assert process["pid"] is None and process["exit_code"] is None
    assert process["reaped"] is False and process["pipe_eof"] is False
    assert operation.record["state"] == "unknown"


def test_last_callback_cancellation_precedes_permit_and_spawns_nothing(
    controller, monkeypatch
):
    operation, service = supervisor(controller)
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) == 2

    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("cancelled Popen")
    )
    result = service.process(
        [sys.executable, "-c", "pass"],
        deadline=time.monotonic() + 1,
        operation="mount_validation",
        maximum=1024,
        capability=service.capability,
        should_stop=cancel,
    )
    assert result.reason == "cancelled"
    implementation._settle(operation, service, reason="cancelled")
    assert operation.record["state"] == "unknown"
    assert operation.record["spawn_possible"] is True
    assert operation.record["processes"] == []


def test_complete_failed_mount_group_is_terminal_failed_after_final_ps(fake):
    missing = fake.workspace / "missing"
    with pytest.raises(SandboxError) as error:
        fake.configure().run(missing, "true", 10, fake.run_id, **fake.binding)
    assert not error.value.cleanup_unknown
    record = fake.controller.operations[0].record
    assert record["state"] == "terminal"
    assert record["terminal_evidence"]["outcome"] == "failed"
    assert [item["phase"] for item in record["processes"]] == [
        "mount_validation",
        "docker_ps",
    ]
    assert [item["operation"] for item in commands(fake)] == ["ps"]


def test_cleanup_deadline_cannot_refresh_across_child_calls(fake):
    (fake.control / "container").write_text("existing")
    started = time.monotonic()
    result = fake.configure(ps_delay=0.2, rm_delay=2).stop_run(
        fake.run_id, **fake.binding
    )
    assert time.monotonic() - started < 1.2
    assert result["cleanup_unknown"]
    assert fake.controller.operations[0].record["state"] == "unknown"


def test_repair_cannot_change_run_or_project_scope(fake):
    with pytest.raises(SandboxError):
        run(fake, create_output="bad\n")
    original = fake.controller.operations[0]
    binding = {k: v for k, v in fake.binding.items() if k != "parent_action_id"}
    binding["project_id"] = str(uuid4())
    before = commands(fake)
    with pytest.raises(co.RegistryError, match="repair_scope"):
        fake.configure().stop_run(
            fake.run_id, repair_of=original.operation_id, **binding
        )
    assert commands(fake) == before
    assert original.record["state"] == "unknown"


def test_owner_loss_after_create_cannot_launch_start_or_cleanup(fake, monkeypatch):
    sandbox = fake.configure()
    original = sandbox._docker

    def docker(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "create":
            fake.controller.owner = "lost"
        return result

    monkeypatch.setattr(sandbox, "_docker", docker)
    with pytest.raises(OwnershipLost) as error:
        sandbox.run(fake.workspace, "true", 10, fake.run_id, **fake.binding)
    assert error.value.cleanup_unknown
    assert [item["operation"] for item in commands(fake)] == ["create"]
    assert fake.controller.operations[0].record["state"] == "launching"


@pytest.fixture
def pg_sandbox(controller_store, request_data, tmp_path, monkeypatch):
    store = controller_store
    run = store.create_run(request_data)
    with store.locked(run["id"]) as (_connection, value):
        value["owner"] = "sandbox-controller-test-owner"
    action_id = store.action(run["id"], "shell", {"command": "controlled fixture"})
    saved = store.get(run["id"])
    control = tmp_path / "control"
    control.mkdir()
    executable = control / "docker"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    executable.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 0.5)
    registry = co.ControllerRegistry(
        store,
        actor=co.fresh_actor("api"),
        check_owner=lambda: None,
        check_other_fences=lambda connection: False,
    )

    def configure(**scenario):
        (control / "scenario.json").write_text(json.dumps(scenario))
        return Sandbox(docker=str(executable))

    value = SimpleNamespace(
        store=store,
        registry=registry,
        control=control,
        workspace=workspace,
        configure=configure,
        run_id=run["id"],
        binding={
            "expected_run_owner": saved["owner"],
            "project_id": saved["request"]["project_id"],
            "parent_action_id": action_id,
        },
    )
    with co.bind_registry(registry):
        yield value
    for name in ["child.pid", "daemon.pid"]:
        path = control / name
        if path.exists():
            try:
                os.kill(int(path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def pg_records(fixture):
    with fixture.store.connect() as connection:
        return [
            row["data"]
            for row in connection.execute(
                "SELECT data FROM controller_operations ORDER BY id"
            ).fetchall()
        ]


def pg_fenced(fixture):
    with fixture.store.connect() as connection:
        return co.fenced(connection)


@pytest.mark.postgres
def test_pg_production_leaf_sees_committed_launching_on_independent_connection(
    pg_sandbox, monkeypatch
):
    fixture, original, observed = pg_sandbox, subprocess.Popen, []

    def spawn(*args, **kwargs):
        records = pg_records(fixture)
        assert len(records) == 1
        record = records[0]
        assert record["state"] == "launching" and record["spawn_possible"]
        assert record["run_id"] == fixture.run_id
        for name, value in fixture.binding.items():
            assert record[name] == value
        assert pg_fenced(fixture)
        observed.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = run(fixture)
    assert result["exit_code"] == 0 and not result["cleanup_unknown"]
    record = pg_records(fixture)[0]
    assert len(observed) == len(record["processes"]) == 6
    assert record["state"] == "terminal" and not pg_fenced(fixture)
    assert all(
        item["pid"] and item["reaped"] and item["pipe_eof"]
        for item in record["processes"]
    )


@pytest.mark.postgres
def test_pg_database_wait_expiry_at_production_leaf_starts_zero_processes(
    pg_sandbox, monkeypatch
):
    from doonex.store import dispatch_lock

    fixture = pg_sandbox
    deadline = time.monotonic() + 1
    operation = implementation._reserve(
        "sandbox_execute",
        run_id=fixture.run_id,
        deadline=deadline,
        intent={"operation": "bounded database wait"},
        container_name="doonex-fixture",
        **fixture.binding,
    )
    evidence = _evidence()
    service = _Supervisor(
        evidence,
        _Cleanup(business_deadline=deadline),
        capability=implementation._launch(operation, evidence),
    )
    held, release, failures = threading.Event(), threading.Event(), []

    def lock():
        try:
            with fixture.store.connect() as connection:
                dispatch_lock(connection)
                held.set()
                assert release.wait(timeout=5)
        except BaseException as error:  # noqa: BLE001 - relay thread assertion failures
            failures.append(error)

    holder = threading.Thread(target=lock)
    holder.start()
    assert held.wait(timeout=5)
    timer = threading.Timer(max(0, deadline - time.monotonic() + 0.15), release.set)
    timer.start()
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("expired actual leaf spawned"),
    )
    try:
        with pytest.raises(co.RegistryError, match="deadline"):
            service.process(
                [sys.executable, "-c", "pass"],
                deadline=deadline,
                operation="mount_validation",
                maximum=1024,
                capability=service.capability,
            )
    finally:
        release.set()
        timer.cancel()
        holder.join(timeout=5)
    assert not holder.is_alive() and not failures
    assert service.capability._processes == []
    implementation._settle(operation, service, reason="deadline")
    assert pg_records(fixture)[0]["state"] == "unknown" and pg_fenced(fixture)


@pytest.mark.postgres
@pytest.mark.parametrize("kind", ["ack", "eof"])
def test_pg_actual_ack_or_eof_unknown_keeps_independent_fence_and_repair_scope(
    pg_sandbox, kind
):
    fixture = pg_sandbox
    if kind == "ack":
        with pytest.raises(SandboxError) as error:
            run(fixture, create_output="bad\n")
        evidence = error.value.cleanup_evidence
        assert evidence["daemon_request_unknown"]
    else:
        result = fixture.configure(pipe_child=True, escaped_child=True).run(
            fixture.workspace, "true", 0.6, fixture.run_id, **fixture.binding
        )
        evidence = result["cleanup_evidence"]
        assert result["cleanup_unknown"] and evidence["local_process_unknown"]
    original = pg_records(fixture)[0]
    assert original["state"] == "unknown" and pg_fenced(fixture)
    if kind == "eof":
        assert any(not item["pipe_eof"] for item in original["processes"])
    fixture.store.finish_action(fixture.binding["parent_action_id"], "acknowledged", {})
    assert pg_fenced(fixture)
    with pytest.raises(co.RegistryError, match="fenced"):
        fixture.configure().stop_run(fixture.run_id, **fixture.binding)
    binding = {
        name: value
        for name, value in fixture.binding.items()
        if name != "parent_action_id"
    }
    result = fixture.configure().stop_run(
        fixture.run_id, repair_of=original["operation_id"], **binding
    )
    assert not result["cleanup_unknown"] and pg_fenced(fixture)
    records = pg_records(fixture)
    assert (
        next(
            record
            for record in records
            if record["operation_id"] == original["operation_id"]
        )
        == original
    )
    repair = next(
        record for record in records if record["repair_of"] == original["operation_id"]
    )
    assert repair["state"] == "terminal"
    assert all(
        item["phase"] in {"docker_ps", "docker_rm"} for item in repair["processes"]
    )


@pytest.mark.postgres
def test_pg_real_run_owner_change_blocks_start_and_new_cleanup(pg_sandbox, monkeypatch):
    fixture = pg_sandbox
    sandbox = fixture.configure()
    original = sandbox._docker

    def docker(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[0] == "create":
            with fixture.store.locked(fixture.run_id) as (_connection, run_value):
                run_value["owner"] = "new-owner"
        return value

    monkeypatch.setattr(sandbox, "_docker", docker)
    with pytest.raises(OwnershipLost) as error:
        sandbox.run(fixture.workspace, "true", 10, fixture.run_id, **fixture.binding)
    assert error.value.cleanup_unknown
    assert [value["operation"] for value in commands(fixture)] == ["create"]
    assert pg_records(fixture)[0]["state"] == "launching" and pg_fenced(fixture)


@pytest.mark.postgres
def test_pg_false_callback_loses_actual_run_authority_before_leaf_popen(
    pg_sandbox, monkeypatch
):
    fixture = pg_sandbox

    operation = implementation._reserve(
        "sandbox_execute",
        run_id=fixture.run_id,
        deadline=time.monotonic() + 10,
        intent={"operation": "callback Run authority"},
        container_name="doonex-fixture",
        **fixture.binding,
    )
    evidence = _evidence()
    service = _Supervisor(
        evidence, _Cleanup(), capability=implementation._launch(operation, evidence)
    )
    calls = []

    def should_stop():
        calls.append(1)
        if len(calls) == 2:
            with fixture.store.locked(fixture.run_id) as (_connection, value):
                value["owner"] = "new-owner"
        return False

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("lost Run authority spawned"),
    )
    with pytest.raises(OwnershipLost):
        service.process(
            [sys.executable, "-c", "pass"],
            deadline=time.monotonic() + 5,
            operation="mount_validation",
            maximum=1024,
            capability=service.capability,
            should_stop=should_stop,
        )
    assert len(calls) == 2 and service.capability._processes == []
    assert pg_records(fixture)[0]["state"] == "launching" and pg_fenced(fixture)


ACTUAL_ACTOR = r"""
import json,os,signal
from doonex import controller_operations as co
from doonex.sandbox import Sandbox
from doonex.store import Store
store=Store(os.environ['DOONEX_SANDBOX_TEST_DSN'])
stage=os.environ['DOONEX_SANDBOX_TEST_STAGE']
registry=co.ControllerRegistry(store,actor=co.fresh_actor('api'),check_owner=lambda:None,check_other_fences=lambda connection:False)
if stage=='before_receipt':
    original=co.LaunchCapability.after_process
    def after(self,permit,**receipt):
        print(json.dumps({'stage':stage,'operation_id':self._operation.operation_id,'actual_leaf_pid':receipt['pid']}),flush=True)
        signal.pause()
        return original(self,permit,**receipt)
    co.LaunchCapability.after_process=after
else:
    original=co.Operation.finish
    def finish(self,evidence):
        print(json.dumps({'stage':stage,'operation_id':self.operation_id,'actual_leaf_pids':[item['pid'] for item in self._capability._processes]}),flush=True)
        signal.pause()
        return original(self,evidence)
    co.Operation.finish=finish
with co.bind_registry(registry):
    Sandbox(docker=os.environ['DOONEX_SANDBOX_TEST_EXECUTABLE']).run(
        os.environ['DOONEX_SANDBOX_TEST_WORKSPACE'],'true',15,os.environ['DOONEX_SANDBOX_TEST_RUN'],
        project_id=os.environ['DOONEX_SANDBOX_TEST_PROJECT'],
        parent_action_id=os.environ['DOONEX_SANDBOX_TEST_ACTION'],
        expected_run_owner='sandbox-controller-test-owner')
"""


@pytest.mark.postgres
@pytest.mark.parametrize("stage", ["before_receipt", "before_terminal"])
def test_pg_sigkill_actual_sandbox_actor_after_real_leaf_leaves_fence(
    pg_sandbox, stage
):
    import selectors

    fixture = pg_sandbox
    sandbox = fixture.configure()
    environment = {
        **os.environ,
        "DOONEX_SANDBOX_TEST_DSN": fixture.store.database_url,
        "DOONEX_SANDBOX_TEST_STAGE": stage,
        "DOONEX_SANDBOX_TEST_EXECUTABLE": sandbox.docker,
        "DOONEX_SANDBOX_TEST_WORKSPACE": str(fixture.workspace),
        "DOONEX_SANDBOX_TEST_RUN": fixture.run_id,
        "DOONEX_SANDBOX_TEST_PROJECT": fixture.binding["project_id"],
        "DOONEX_SANDBOX_TEST_ACTION": fixture.binding["parent_action_id"],
    }
    actor = subprocess.Popen(
        [sys.executable, "-c", ACTUAL_ACTOR],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(actor.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=12)
            checkpoint = json.loads(actor.stdout.readline())
        assert checkpoint["stage"] == stage
        if stage == "before_receipt":
            assert type(checkpoint["actual_leaf_pid"]) is int
        else:
            assert len(checkpoint["actual_leaf_pids"]) == 6
            assert all(type(pid) is int for pid in checkpoint["actual_leaf_pids"])
        actor.send_signal(signal.SIGKILL)
        assert actor.wait(timeout=5) == -signal.SIGKILL
        record = pg_records(fixture)[0]
        assert record["operation_id"] == checkpoint["operation_id"]
        assert record["state"] == "launching" and record["spawn_possible"]
        assert record["processes"] == [] and record["terminal_evidence"] is None
        assert pg_fenced(fixture)
        fixture.store.finish_action(
            fixture.binding["parent_action_id"], "acknowledged", {}
        )
        assert pg_fenced(fixture)
        with pytest.raises(co.RegistryError, match="fenced"):
            fixture.configure().containers(fixture.run_id, **fixture.binding)
    finally:
        if actor.poll() is None:
            actor.kill()
            actor.wait(timeout=5)
        actor.stdout.close()
