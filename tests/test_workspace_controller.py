"""Workspace admission tests with explicit fake or disposable PG registries."""

import copy
import hashlib
import os
import selectors
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from doonex import controller_operations as co
from doonex import workspace as ws
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.store import OwnershipLost


class FakeCapability:
    """Explicit component fixture; no production no-hook bypass is installed."""

    def __init__(self, operation):
        self.operation, self.processes = operation, []

    def before_process(self, phase):
        if phase != "git" or not self.operation.registry.live:
            raise co.RegistryError("fixture_capability")
        if len(self.processes) >= co.MAX_PROCESSES:
            raise co.RegistryError("process_limit")
        self.processes.append({"phase": phase})
        return len(self.processes)

    def after_process(self, permit, **receipt):
        self.processes[permit - 1].update(receipt)


class FakeOperation:
    def __init__(self, registry, kind, arguments):
        self.registry, self.kind, self.arguments = (
            registry,
            kind,
            copy.deepcopy(arguments),
        )
        self.operation_id, self.state = str(uuid4()), "reserved"
        self.capability = FakeCapability(self)
        self.evidence = self.reason = None

    def launch(self):
        self.state = "launching"
        return self.capability

    def finish(self, evidence):
        if self.registry.finish_error is not None:
            raise self.registry.finish_error
        self.state, self.evidence = "terminal", copy.deepcopy(evidence)

    def mark_unknown(self, reason):
        if self.registry.finish_error is not None:
            raise self.registry.finish_error
        self.state, self.reason = "unknown", reason


class FakeRegistry:
    def __init__(self):
        self.operations, self.live, self.finish_error = [], True, None

    def reserve(self, kind, **arguments):
        if not self.live or any(
            operation.state != "terminal" for operation in self.operations
        ):
            raise co.RegistryError("fixture_fenced")
        operation = FakeOperation(self, kind, arguments)
        self.operations.append(operation)
        return operation


@pytest.fixture
def fake_registry(monkeypatch):
    registry = FakeRegistry()

    def current():
        if not registry.live:
            raise co.RegistryError("fixture_expired")
        return registry

    monkeypatch.setattr(ws, "current_registry", current)
    return registry


@pytest.fixture
def repository(tmp_path):
    repository = tmp_path / "private project"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Offline fixture"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "fixture@example.invalid",
        ],
        check=True,
    )
    (repository / "sample.txt").write_text("controlled fixture\n")
    subprocess.run(["git", "-C", str(repository), "add", "sample.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True
    )
    return repository


@pytest.mark.parametrize("entry", ["git", "validate", "create"])
def test_missing_context_refuses_every_high_level_before_popen(
    repository, tmp_path, monkeypatch, entry
):
    monkeypatch.setattr(ws, "current_registry", co.current_registry)
    calls = []
    monkeypatch.setattr(ws.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(co.RegistryError, match="registry_missing"):
        if entry == "git":
            ws._git(repository, "rev-parse", "HEAD")
        elif entry == "validate":
            ws.validate_repository(repository)
        else:
            ws.Workspace.create(repository, "HEAD", tmp_path / "export")
    assert calls == []


def test_leaf_and_constructor_refuse_missing_capability(repository, monkeypatch):
    calls = []
    monkeypatch.setattr(ws.subprocess, "Popen", lambda *a, **k: calls.append(a))
    deadline = time.monotonic() + 10
    with pytest.raises(co.RegistryError, match="capability_missing"):
        ws._GitOperation(repository, deadline)
    with pytest.raises(co.RegistryError, match="capability_missing"):
        ws._run_git_command(
            ["git", "--version"],
            os.environ.copy(),
            deadline=deadline,
            stdout_limit=1024,
        )
    assert calls == []


@pytest.mark.parametrize(
    "entry,kind,expected",
    [
        ("git", "git_api_read", 2),
        ("validate", "git_validate", 3),
        ("create", "git_export", 6),
    ],
)
def test_every_git_process_is_preceded_by_one_durable_group_and_receipted(
    repository, tmp_path, fake_registry, monkeypatch, entry, kind, expected
):
    real_popen = subprocess.Popen
    commands = []

    def observed(command, *args, **kwargs):
        assert len(fake_registry.operations) == 1
        operation = fake_registry.operations[0]
        assert operation.state == "launching"
        assert len(operation.capability.processes) == len(commands) + 1
        commands.append(command)
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(ws.subprocess, "Popen", observed)
    if entry == "git":
        assert ws._git(repository, "rev-parse", "HEAD").returncode == 0
    elif entry == "validate":
        assert ws.validate_repository(repository) == repository.resolve()
    else:
        exported = ws.Workspace.create(
            repository, "HEAD", tmp_path / "export", baseline_path=tmp_path / "baseline"
        )
        assert (exported.path / "sample.txt").read_text() == "controlled fixture\n"
    operation = fake_registry.operations[0]
    assert operation.kind == kind and operation.state == "terminal"
    assert len(commands) == expected
    assert "config" in commands[0]
    assert all(
        item["reaped"] and item["pipe_eof"] and item["exit_code"] == 0
        for item in operation.capability.processes
    )


def test_scope_has_only_fixed_digests_and_aliases_share_source(
    repository, tmp_path, fake_registry
):
    ws._git(repository, "rev-parse", "HEAD")
    first = fake_registry.operations[0].arguments["scope"]
    alias = tmp_path / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    ws._git(alias, "rev-parse", "HEAD")
    second = fake_registry.operations[1].arguments["scope"]
    assert first == second
    assert str(repository) not in repr(first)
    assert (
        first["repository_digest"]
        == hashlib.sha256(
            b"doonex-git-source-v1\0" + os.fsencode(repository.resolve())
        ).hexdigest()
    )
    assert set(first) == {
        "intent_digest",
        "repository_digest",
        "container_name",
        "run_label",
    }


def test_group_deadline_preserves_absolute_outer_cutoff(repository, fake_registry):
    deadline = time.monotonic() + 10
    ws.validate_repository(repository, deadline=deadline)
    assert fake_registry.operations[0].arguments["deadline_monotonic_ns"] == int(
        deadline * 1_000_000_000
    )
    assert (
        fake_registry.operations[0].arguments["cleanup_allowance_seconds"]
        == ws.GIT_CLEANUP_TIMEOUT
    )


def test_command_failure_is_safe_failed_terminal_and_can_be_followed(
    repository, fake_registry
):
    with pytest.raises(ws.WorkspaceError, match="Git project operation failed"):
        ws._git(repository, "rev-parse", "refs/heads/nonexistent-fixture")
    operation = fake_registry.operations[0]
    assert (
        operation.state == "terminal"
        and operation.evidence["reason"] == "command_failed"
    )
    assert operation.capability.processes[-1]["exit_code"] != 0
    ws.validate_repository(repository)
    assert len(fake_registry.operations) == 2


def test_result_copy_failure_does_not_erase_reaped_git_receipts(
    repository, tmp_path, fake_registry, monkeypatch
):
    def fail(*args, **kwargs):
        raise ws.WorkspaceError("controlled write failed")

    monkeypatch.setattr(ws, "write_files", fail)
    with pytest.raises(ws.WorkspaceError, match="controlled write failed"):
        ws.Workspace.create(repository, "HEAD", tmp_path / "export")
    operation = fake_registry.operations[0]
    assert (
        operation.state == "terminal"
        and operation.evidence["reason"] == "operation_failed"
    )
    assert len(operation.capability.processes) == 6


def test_popen_start_failure_leaves_unknown_without_fabricated_pid(
    repository, fake_registry, monkeypatch
):
    def fail(*args, **kwargs):
        raise OSError("controlled spawn error")

    monkeypatch.setattr(ws.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="controlled spawn error"):
        ws.validate_repository(repository)
    operation = fake_registry.operations[0]
    assert operation.state == "unknown"
    assert operation.capability.processes == [{"phase": "git"}]
    with pytest.raises(co.RegistryError, match="fixture_fenced"):
        ws.validate_repository(repository)


def test_live_context_expiry_between_permit_and_popen_starts_nothing(
    repository, fake_registry, monkeypatch
):
    real_before = FakeCapability.before_process

    def expired(self, phase):
        permit = real_before(self, phase)
        self.operation.registry.live = False
        return permit

    monkeypatch.setattr(FakeCapability, "before_process", expired)
    calls = []
    monkeypatch.setattr(ws.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(co.RegistryError, match="fixture_expired"):
        ws.validate_repository(repository)
    assert calls == []
    assert fake_registry.operations[0].state == "unknown"


def test_deadline_expiry_between_permit_and_popen_starts_nothing(
    repository, fake_registry, monkeypatch
):
    clock = [time.monotonic()]
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock[0])
    real_before = FakeCapability.before_process

    def expired(self, phase):
        permit = real_before(self, phase)
        clock[0] += 20
        return permit

    monkeypatch.setattr(FakeCapability, "before_process", expired)
    calls = []
    monkeypatch.setattr(ws.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(ExecutionDeadlineExceeded):
        ws.validate_repository(repository, deadline=clock[0] + 10)
    assert calls == []
    assert fake_registry.operations[0].state == "unknown"


def test_registry_failure_preserves_original_cleanup_unknown_in_cause(
    repository, fake_registry, monkeypatch
):
    process = SimpleNamespace(
        pid=123,
        returncode=None,
        stdout=SimpleNamespace(closed=True),
        stderr=SimpleNamespace(closed=True),
    )
    unknown = ws.WorkspaceProcessUnknown(process)
    assert unknown.cleanup_evidence["processes"][0]["pipe_eof"] is False

    def fail(*args, **kwargs):
        raise unknown

    fake_registry.finish_error = co.RegistryError("synthetic_commit_unknown")
    monkeypatch.setattr(ws, "_GitOperation", fail)
    with pytest.raises(co.RegistryError) as captured:
        ws.validate_repository(repository)
    assert captured.value.__cause__ is unknown


def test_legacy_base_files_fallback_creates_one_export_group(
    repository, tmp_path, fake_registry
):
    workspace = ws.Workspace(tmp_path / "unused", repository, "HEAD")
    assert workspace.base_files()["sample.txt"].data == b"controlled fixture\n"
    assert (
        len(fake_registry.operations) == 1
        and fake_registry.operations[0].kind == "git_export"
    )


def test_near_file_count_export_stays_below_fixed_process_limit(
    repository, tmp_path, fake_registry
):
    for index in range(ws.MAX_FILES - 1):
        (repository / f"f{index:05d}.txt").write_bytes(b"x")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "near count limit"], check=True
    )
    exported = ws.Workspace.create(repository, "HEAD", tmp_path / "export")
    assert len(list(exported.path.iterdir())) == ws.MAX_FILES
    processes = fake_registry.operations[0].capability.processes
    assert len(processes) == 6 < co.MAX_PROCESSES
    assert all(item["reaped"] and item["pipe_eof"] for item in processes)


def test_near_byte_capacity_export_remains_one_batch_process(
    repository, tmp_path, fake_registry
):
    (repository / "sample.txt").unlink()
    content = b"x" * ws.MAX_FILE_BYTES
    for index in range(ws.MAX_TOTAL_BYTES // ws.MAX_FILE_BYTES):
        (repository / f"capacity-{index}.bin").write_bytes(content)
    subprocess.run(["git", "-C", str(repository), "add", "--all"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "near byte limit"], check=True
    )
    exported = ws.Workspace.create(repository, "HEAD", tmp_path / "export")
    assert (
        sum(path.stat().st_size for path in exported.path.iterdir())
        == ws.MAX_TOTAL_BYTES
    )
    assert len(fake_registry.operations[0].capability.processes) == 6


@pytest.mark.parametrize(
    "intent",
    [(1,), tuple("x" for _ in range(65)), ("x" * (ws.MAX_GIT_CONFIG_BYTES + 1),)],
)
def test_intent_is_bounded_before_reserve(repository, fake_registry, intent):
    with pytest.raises(ws.WorkspaceError, match="intent limit"):
        ws._git(repository, *intent)
    assert fake_registry.operations == []


@pytest.fixture
def pg_registry(store):
    with store.connect() as connection:
        co.initialize(connection)
    registry = co.ControllerRegistry(
        store,
        actor=co.fresh_actor("cli"),
        check_owner=lambda: None,
        check_other_fences=lambda connection: False,
    )
    with co.bind_registry(registry):
        yield store, registry


def pg_rows(store):
    with store.connect() as connection:
        return connection.execute(
            "SELECT * FROM controller_operations ORDER BY id"
        ).fetchall()


def test_pg_config_first_popen_sees_committed_launching_on_second_connection(
    repository, tmp_path, pg_registry, monkeypatch
):
    store, _ = pg_registry
    real_popen, observed = subprocess.Popen, []

    def visible(command, *args, **kwargs):
        rows = pg_rows(store)
        assert len(rows) == 1
        assert rows[0]["data"]["state"] == "launching"
        with store.connect() as connection:
            assert co.fenced(connection)
        observed.append(command)
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(ws.subprocess, "Popen", visible)
    exported = ws.Workspace.create(repository, "HEAD", tmp_path / "export")
    assert (exported.path / "sample.txt").is_file()
    record = pg_rows(store)[0]["data"]
    assert record["state"] == "terminal" and len(record["processes"]) == 6
    assert len(observed) == 6 and "config" in observed[0]
    with store.connect() as connection:
        assert not co.fenced(connection)


@pytest.mark.parametrize("entry", ["git", "validate", "create", "base_files"])
def test_explicit_operation_context_is_forwarded_to_registry(
    repository, tmp_path, fake_registry, entry
):
    context = {
        "run_id": str(uuid4()),
        "project_id": str(uuid4()),
        "parent_action_id": str(uuid4()),
        "expected_run_owner": "fixture-owner",
    }
    if entry == "git":
        ws._git(repository, "rev-parse", "HEAD", operation_context=context)
    elif entry == "validate":
        ws.Workspace.validate_repository(repository, operation_context=context)
    elif entry == "create":
        ws.Workspace.create(
            repository, "HEAD", tmp_path / "export", operation_context=context
        )
    else:
        ws.Workspace(tmp_path / "unused", repository, "HEAD").base_files(
            operation_context=context
        )
    arguments = fake_registry.operations[0].arguments
    assert {name: arguments[name] for name in context} == context


def test_created_workspace_captures_context_and_legacy_fallback_reuses_it(
    repository, tmp_path, fake_registry
):
    context = {"run_id": str(uuid4()), "expected_run_owner": "captured-owner"}
    workspace = ws.Workspace.create(
        repository, "HEAD", tmp_path / "export", operation_context=context
    )
    context["expected_run_owner"] = "changed-caller"
    assert workspace.operation_context["expected_run_owner"] == "captured-owner"
    workspace.base_files()
    assert (
        fake_registry.operations[1].arguments["expected_run_owner"] == "captured-owner"
    )


def test_context_captured_before_git_cannot_change_with_caller_dict(
    repository, tmp_path, fake_registry, monkeypatch
):
    context = {"run_id": str(uuid4()), "expected_run_owner": "captured-owner"}
    original = subprocess.Popen

    def changed(*args, **kwargs):
        context["expected_run_owner"] = "changed-during-git"
        return original(*args, **kwargs)

    monkeypatch.setattr(ws.subprocess, "Popen", changed)
    workspace = ws.Workspace.create(
        repository, "HEAD", tmp_path / "export", operation_context=context
    )
    assert workspace.operation_context["expected_run_owner"] == "captured-owner"
    assert (
        fake_registry.operations[0].arguments["expected_run_owner"] == "captured-owner"
    )


def test_unrecognized_context_cannot_override_actor_or_kind(repository, fake_registry):
    with pytest.raises(co.RegistryError, match="operation_context"):
        ws.validate_repository(repository, operation_context={"actor": "forged"})
    assert fake_registry.operations == []


def test_real_timeout_reaps_and_observes_both_eof_before_failed_terminal(
    repository, fake_registry
):
    cutoff = time.monotonic() + 0.15
    with (
        pytest.raises(ExecutionDeadlineExceeded),
        ws._controller_group(repository, "git_api_read", cutoff) as controller,
    ):
        ws._run_git_command(
            [sys.executable, "-I", "-S", "-c", "import time;time.sleep(30)"],
            os.environ.copy(),
            deadline=cutoff,
            outer_deadline=cutoff,
            stdout_limit=32,
            controller=controller,
        )
    operation = fake_registry.operations[0]
    assert operation.state == "terminal" and operation.evidence["reason"] == "deadline"
    assert (
        operation.capability.processes[0]["reaped"]
        and operation.capability.processes[0]["pipe_eof"]
    )


def test_real_inherited_pipe_unknown_remains_false_after_finally_close(
    repository, fake_registry, monkeypatch
):
    child_ids = []
    monkeypatch.setattr(ws, "GIT_CLEANUP_TIMEOUT", 0.1)
    code = "import subprocess,sys;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True);print(p.pid,flush=True)"

    def consume(chunk):
        child_ids.extend(int(value) for value in chunk.split())

    try:
        with (
            pytest.raises(ws.WorkspaceProcessUnknown) as captured,
            ws._controller_group(
                repository, "git_api_read", time.monotonic() + 1
            ) as controller,
        ):
            ws._run_git_command(
                [sys.executable, "-c", code],
                os.environ.copy(),
                deadline=time.monotonic() + 0.2,
                stdout_limit=128,
                consume=consume,
                controller=controller,
            )
        process = captured.value.cleanup_evidence["processes"][0]
        assert process["reaped"] is True and process["pipe_eof"] is False
        operation = fake_registry.operations[0]
        assert operation.state == "unknown"
        assert operation.capability.processes[0]["pipe_eof"] is False
    finally:
        for child_id in child_ids:
            try:
                os.kill(child_id, 9)
            except ProcessLookupError:
                pass


class LostCommitStore:
    def __init__(self, store, *, lose_at):
        self.store, self.calls, self.lose_at = store, 0, lose_at

    @contextmanager
    def connect(self):
        self.calls += 1
        current = self.calls
        with self.store.connect() as connection:
            yield connection
        if current == self.lose_at:
            raise OSError("synthetic lost commit acknowledgement")


@pytest.mark.parametrize("lose_at", [1, 2])
def test_pg_reserve_or_gate_ack_loss_starts_zero_git_processes(
    repository, store, monkeypatch, lose_at
):
    with store.connect() as connection:
        co.initialize(connection)
    wrapped = LostCommitStore(store, lose_at=lose_at)
    registry = co.ControllerRegistry(
        wrapped,
        actor=co.fresh_actor("cli"),
        check_owner=lambda: None,
        check_other_fences=lambda connection: False,
    )
    calls = []
    monkeypatch.setattr(ws.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with (
        co.bind_registry(registry),
        pytest.raises(co.RegistryError, match="commit_unknown"),
    ):
        ws.validate_repository(repository)
    assert calls == [] and len(pg_rows(store)) == 1
    with store.connect() as connection:
        assert co.fenced(connection)


def test_pg_spawn_failure_fences_following_git_without_replay(
    repository, pg_registry, monkeypatch
):
    store, _ = pg_registry
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise OSError("controlled spawn failure")

    monkeypatch.setattr(ws.subprocess, "Popen", fail)
    with pytest.raises(OSError):
        ws.validate_repository(repository)
    record = pg_rows(store)[0]["data"]
    assert record["state"] == "unknown" and record["processes"][0]["pid"] is None
    with pytest.raises(co.RegistryError, match="fenced"):
        ws.validate_repository(repository)
    assert len(calls) == 1


def test_pg_command_failure_terminal_preserves_real_receipts(repository, pg_registry):
    store, _ = pg_registry
    with pytest.raises(ws.WorkspaceError):
        ws._git(repository, "rev-parse", "refs/heads/nonexistent-fixture")
    record = pg_rows(store)[0]["data"]
    assert (
        record["state"] == "terminal"
        and record["terminal_evidence"]["outcome"] == "failed"
    )
    assert all(item["reaped"] and item["pipe_eof"] for item in record["processes"])
    ws.validate_repository(repository)
    assert len(pg_rows(store)) == 2


def test_pg_run_and_action_context_are_persisted_and_new_owner_blocks_next_git(
    repository, pg_registry, request_data, monkeypatch
):
    store, _ = pg_registry
    run = store.create_run(request_data)
    with store.locked(run["id"]) as (connection, current):
        current["owner"] = "owner-before"
        action_id = store.action(run["id"], "shell", {"command": "fixture"}, connection)
    context = {
        "run_id": run["id"],
        "project_id": request_data["project_id"],
        "parent_action_id": action_id,
        "expected_run_owner": "owner-before",
    }
    real_after = co.LaunchCapability.after_process

    def changed(self, *args, **kwargs):
        result = real_after(self, *args, **kwargs)
        with store.locked(run["id"]) as (_, current):
            current["owner"] = "owner-after"
        return result

    monkeypatch.setattr(co.LaunchCapability, "after_process", changed)
    with pytest.raises(OwnershipLost):
        ws.validate_repository(repository, operation_context=context)
    record = pg_rows(store)[0]["data"]
    assert {name: record[name] for name in context} == context
    assert record["state"] == "launching"
    with store.connect() as connection:
        assert co.fenced(connection)


def test_pg_expiry_while_before_process_waits_starts_no_popen(
    repository, pg_registry, monkeypatch
):
    store, _ = pg_registry
    clock = [time.monotonic()]
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(co.time, "monotonic_ns", lambda: int(clock[0] * 1_000_000_000))
    real_locked, count = co.Operation._locked, [0]

    def delayed(self, connection):
        result = real_locked(self, connection)
        count[0] += 1
        if count[0] == 2:
            clock[0] = self.record["business_deadline_monotonic_ns"] / 1_000_000_000 + 1
        return result

    monkeypatch.setattr(co.Operation, "_locked", delayed)
    calls = []
    monkeypatch.setattr(ws.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(co.RegistryError, match="deadline"):
        ws.validate_repository(repository)
    assert calls == []
    assert pg_rows(store)[0]["data"]["state"] == "unknown"


def test_pg_owner_expiry_after_gate_starts_no_popen(
    repository, pg_registry, monkeypatch
):
    store, registry = pg_registry
    live = [True]

    def check():
        if not live[0]:
            raise OwnershipLost("controlled owner loss")

    registry.check_owner = check
    real_launch = co.Operation.launch

    def expired(self):
        result = real_launch(self)
        live[0] = False
        return result

    monkeypatch.setattr(co.Operation, "launch", expired)
    calls = []
    monkeypatch.setattr(ws.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(OwnershipLost):
        ws.validate_repository(repository)
    assert calls == []
    assert pg_rows(store)[0]["data"]["state"] == "launching"


@pytest.mark.parametrize("stage", ["before_popen", "after_popen", "before_terminal"])
def test_pg_killed_workspace_actor_leaves_durable_fence_at_actual_leaf_boundaries(
    repository, store, stage
):
    with store.connect() as connection:
        co.initialize(connection)
    script = """
import os,signal
from pathlib import Path
from doonex import workspace as ws
from doonex import controller_operations as co
from doonex.store import Store
store=Store(os.environ['DOONEX_CONTROLLER_TEST_DSN'])
registry=co.ControllerRegistry(store,actor=co.fresh_actor('cli'),check_owner=lambda:None,check_other_fences=lambda connection:False)
stage=os.environ['DOONEX_CONTROLLER_TEST_STAGE']
original=ws.subprocess.Popen
def pause():
    print('boundary-ready',flush=True)
    signal.pause()
def popen(*args,**kwargs):
    if stage=='before_popen': pause()
    process=original(*args,**kwargs)
    if stage=='after_popen': pause()
    return process
ws.subprocess.Popen=popen
original_finish=co.Operation.finish
def finish(self,evidence):
    if stage=='before_terminal': pause()
    return original_finish(self,evidence)
co.Operation.finish=finish
with co.bind_registry(registry):
    ws.validate_repository(Path(os.environ['DOONEX_CONTROLLER_TEST_REPOSITORY']))
"""
    environment = {
        **os.environ,
        "DOONEX_CONTROLLER_TEST_DSN": store.database_url,
        "DOONEX_CONTROLLER_TEST_REPOSITORY": str(repository),
        "DOONEX_CONTROLLER_TEST_STAGE": stage,
    }
    actor = subprocess.Popen(
        [sys.executable, "-c", script],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(actor.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10)
            assert actor.stdout.readline().strip() == "boundary-ready"
        record = pg_rows(store)[0]["data"]
        assert record["state"] == "launching" and record["spawn_possible"] is True
        actor.send_signal(signal.SIGKILL)
        actor.wait(timeout=5)
        with store.connect() as connection:
            assert co.fenced(connection)
        registry = co.ControllerRegistry(
            store,
            actor=co.fresh_actor("cli"),
            check_owner=lambda: None,
            check_other_fences=lambda connection: False,
        )
        with (
            co.bind_registry(registry),
            pytest.raises(co.RegistryError, match="fenced"),
        ):
            ws.validate_repository(repository)
        # All actual child commands are short Git reads. No stored bare PID is
        # signalled by recovery or used as proof of a terminal child.
    finally:
        if actor.poll() is None:
            actor.kill()
            actor.wait(timeout=5)
