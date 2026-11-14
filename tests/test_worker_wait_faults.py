"""Actual Worker.run failure exits, with local synthetic provider responses.

Fault injection is named explicitly. A finite watchdog is only a test failure
guard; a refused/unknown execution never counts as a successful waiting sample.
"""

import hashlib
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import controller_test_support
import psycopg
import pytest
from doonex import sandbox as sandbox_module
from doonex import sdk
from doonex import store as store_module
from doonex import workspace as workspace_module
from doonex.cleanup import cleanup_intent_unresolved
from doonex.scheduling import Scheduler
from doonex.store import execution_fenced
from doonex.worker import Worker, run_directory
from test_api import repository
from test_scheduling import action_rows, claimed
from test_scheduling import taskset_source as taskset_fixture
from test_scheduling import tuning as tuning_fixture
from test_worker import response

controller_registry = controller_test_support.controller_registry
taskset_source = taskset_fixture
tuning = tuning_fixture
pytestmark = [pytest.mark.postgres, pytest.mark.docker]


def _tool_response():
    result = response("doonex_shell", {"command": "touch must-not-dispatch"}, 0)
    result.choices[0].message.tool_calls.extend(
        response("finish", {"message": "Synthetic background finish"}, 1)
        .choices[0]
        .message.tool_calls
    )
    return result


def _foreground_request(request_data, tmp_path):
    source = tmp_path / "wait-fault-foreground"
    source.mkdir()
    repo, git = repository(source)
    request = deepcopy(request_data)
    request.update(
        repo_path=str(repo),
        base_commit=git("rev-parse", "HEAD"),
        checks=["python -c 'assert True'"],
    )
    request["model"].update(model_id="gpt-4.1-mini")
    return request


@pytest.fixture
def wait_fault(tuning, store, request_data, tmp_path, monkeypatch):
    """One background Run, then an ordinary foreground through Worker.run."""
    settings, _, body, _ = tuning
    foreground_request = _foreground_request(request_data, tmp_path)

    def build(*, seconds=600):
        body["run_budget"]["max_seconds"] = seconds
        _, _, _, batch = claimed(tuning, store)
        background_id = batch["attempts"][0]["run_id"]
        context = SimpleNamespace(
            settings=settings,
            store=store,
            worker=Worker(settings),
            batch=batch,
            background_id=background_id,
            workspace=run_directory(settings, background_id) / "workspace",
            foreground=None,
            enqueue_bracket=None,
            background_before=None,
            calls=[],
            executions=[],
            errors=[],
            events=[],
            deadline=None,
            on_transport=None,
            on_session=None,
            on_checkpoint=None,
            after_background=None,
            response=_tool_response,
        )

        def observe(kind, **values):
            context.events.append(
                {"kind": kind, "monotonic": time.monotonic(), **values}
            )

        def enqueue():
            assert context.foreground is None
            before = time.monotonic()
            context.foreground = store.create_run(foreground_request)
            after = time.monotonic()
            context.enqueue_bracket = [before, after]
            context.background_before = store.get(background_id)
            observe("foreground_enqueue_commit_confirmed")
            return context.foreground

        original_session = sdk.run_session
        original_save = Scheduler.save_yield
        original_execute = context.worker.execute

        def transport(*args, **kwargs):
            current = context.executions[-1]
            context.calls.append(current)
            observe("synthetic_transport_enter", run_id=current)
            if current == background_id:
                assert context.calls.count(current) == 1
                if context.on_transport is not None:
                    context.on_transport()
                else:
                    enqueue()
                return context.response()
            assert context.foreground is not None
            assert current == context.foreground["id"]
            return response("finish", {"message": "Synthetic foreground finish"}, 2)

        def session(**kwargs):
            if kwargs["run_id"] == background_id:
                context.deadline = kwargs["persistence_deadline"]
                if context.on_session is not None:
                    context.on_session()
            return original_session(**kwargs)

        def checkpoint(scheduler, run_id, *args, **kwargs):
            if run_id == background_id and context.on_checkpoint is not None:
                context.on_checkpoint()
            return original_save(scheduler, run_id, *args, **kwargs)

        def execute(run_id):
            if run_id != background_id:
                assert context.foreground is not None
                assert run_id == context.foreground["id"], (
                    "A background sibling was dispatched"
                )
            context.executions.append(run_id)
            try:
                return original_execute(run_id)
            finally:
                if run_id == background_id and context.after_background is not None:
                    context.after_background()
                if run_id != background_id:
                    context.worker.stopping = True

        def run():
            try:
                context.worker.run()
            except BaseException as error:  # noqa: BLE001 - assert actual runtime errors below
                context.errors.append(error)
            finally:
                print(
                    "worker_wait_fault="
                    + json.dumps(
                        {
                            "background_id": background_id,
                            "foreground_id": (context.foreground or {}).get("id"),
                            "enqueue_commit_bracket": context.enqueue_bracket,
                            "executions": context.executions,
                            "provider": "synthetic",
                            "error_types": [
                                type(error).__name__ for error in context.errors
                            ],
                            "events": context.events,
                        },
                        sort_keys=True,
                    )
                )

        context.enqueue, context.observe, context.run = enqueue, observe, run
        monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
        monkeypatch.setattr(sdk, "run_session", session)
        monkeypatch.setattr(Scheduler, "save_yield", checkpoint)
        monkeypatch.setattr(context.worker, "execute", execute)
        return context

    return build


def _assert_preserved(context, *, calls=1):
    before = context.background_before
    assert before is not None and before["first_started_at"] is not None
    current = context.store.get(context.background_id)
    assert current["request"] == before["request"]
    assert current["first_started_at"] == before["first_started_at"]
    assert current["usage"]["calls"] == calls
    assert current["usage"]["reserved_tokens"] == current["usage"]["unknown_calls"] == 0
    assert not context.store.pending(context.background_id)
    assert all(
        row["status"] == "completed"
        for row in action_rows(context.store, context.background_id)
    )
    assert not (context.workspace / "must-not-dispatch").exists()
    for attempt in context.batch["attempts"][1:]:
        assert not action_rows(context.store, attempt["run_id"])
    for run_id in context.executions:
        assert run_id not in sdk._CALLBACKS
        assert run_id not in sdk._SCHEDULING
        assert run_id not in sdk._SESSION_STORES
    return current


def _assert_foreground_finished(context):
    assert not context.errors, context.errors
    assert context.executions == [context.background_id, context.foreground["id"]]
    current = context.store.get(context.foreground["id"])
    assert current["state"] == "completed_verified", current["reason"]
    assert current["usage"]["calls"] == 1
    assert not context.store.pending(current["id"])
    with context.store.connect() as connection:
        assert not execution_fenced(connection)


@pytest.mark.parametrize("fault", ["fifo", "changing_file"])
def test_worker_checkpoint_rejects_special_or_changing_file(
    wait_fault, monkeypatch, fault
):
    context = wait_fault()
    changes = []

    def inject():
        target = context.workspace / "fault-input.txt"
        if fault == "fifo":
            os.mkfifo(target, 0o600)
            changes.append("fifo")
            return
        target.write_bytes(b"fixed input")
        inode = target.stat().st_ino
        original_read = os.read

        def changing_read(descriptor, maximum):
            data = original_read(descriptor, maximum)
            if not changes and os.fstat(descriptor).st_ino == inode:
                with target.open("ab") as output:
                    output.write(b" changed during actual snapshot read")
                changes.append("changed_after_read")
            return data

        # Keep unrelated subprocess/SDK os.read calls unchanged.
        monkeypatch.setattr(
            workspace_module,
            "os",
            SimpleNamespace(**{**vars(os), "read": changing_read}),
        )

    context.on_checkpoint = inject
    context.run()
    current = _assert_preserved(context)
    assert changes
    assert current["state"] == "failed", current["reason"]
    assert ("special file" if fault == "fifo" else "changed") in current["reason"]
    assert "scheduling" not in current
    _assert_foreground_finished(context)


@pytest.mark.parametrize("stage", ["provider", "checkpoint"])
def test_worker_common_deadline_expires_without_fresh_background_admission(
    wait_fault, stage
):
    context = wait_fault(seconds=3)

    def expire():
        assert context.deadline is not None
        context.observe("bounded_fault_wait_enter", stage=stage)
        time.sleep(max(0, context.deadline - time.monotonic()) + 0.03)
        context.observe("bounded_fault_wait_returned", stage=stage)

    if stage == "provider":

        def late_response():
            context.enqueue()
            expire()

        context.on_transport = late_response
    else:
        context.on_checkpoint = expire
    context.run()
    current = _assert_preserved(context)
    assert current["state"] == "budget_exceeded", current["reason"]
    assert "scheduling" not in current
    assert [
        row["kind"] for row in action_rows(context.store, context.background_id)
    ] == ["model"]
    _assert_foreground_finished(context)


def test_worker_held_sdk_lock_expires_before_model_reservation(wait_fault):
    context = wait_fault(seconds=3)
    held = []

    def lock_and_enqueue():
        assert sdk._RUN_LOCK.acquire(blocking=False)
        held.append(True)
        context.enqueue()
        context.observe("actual_sdk_lock_held")

    def release():
        if held:
            sdk._RUN_LOCK.release()
            held.clear()
            context.observe("actual_sdk_lock_released")

    context.on_session = lock_and_enqueue
    context.after_background = release
    try:
        context.run()
    finally:
        release()
    current = _assert_preserved(context, calls=0)
    assert current["state"] in {"failed", "budget_exceeded"}, current["reason"]
    assert "deadline" in current["reason"].lower()
    assert context.calls == [context.foreground["id"]]
    assert not action_rows(context.store, context.background_id)
    _assert_foreground_finished(context)


def test_worker_actual_close_then_timeout_keeps_receipt_and_stops_dispatch(
    wait_fault, monkeypatch
):
    context = wait_fault()
    original_close = sdk._M1Conversation.close
    closed = []

    def close(conversation, *args, **kwargs):
        result = original_close(conversation, *args, **kwargs)
        identifier = getattr(conversation.state.agent, "doonex_run_id", None)
        if identifier == context.background_id and not closed:
            closed.append(identifier)
            context.observe("actual_sdk_close_returned_then_injected_timeout")
            raise TimeoutError("Controlled SDK close timeout after actual close")
        return result

    monkeypatch.setattr(sdk._M1Conversation, "close", close)
    context.run()
    current = _assert_preserved(context)
    assert closed == [context.background_id]
    assert current["state"] == "failed", current["reason"]
    assert current["scheduling"]["receipt"]
    assert "Controlled SDK close timeout" in current["reason"]
    _assert_foreground_finished(context)


def test_worker_real_checkpoint_lock_timeout_rolls_back_receipt(
    wait_fault, monkeypatch
):
    context = wait_fault()
    original_locked = store_module.Store.locked
    checkpoint_active, faults = [], []
    monkeypatch.setattr(store_module, "LOCK_TIMEOUT_MS", 100)

    def at_checkpoint():
        checkpoint_active.append(True)

    from contextlib import contextmanager

    @contextmanager
    def locked(current_store, run_id, *args, **kwargs):
        if run_id != context.background_id or not checkpoint_active or faults:
            with original_locked(current_store, run_id, *args, **kwargs) as value:
                yield value
            return
        with context.store.connect() as holder:
            holder.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (run_id,))
            try:
                with original_locked(current_store, run_id, *args, **kwargs) as value:
                    yield value
            except psycopg.errors.LockNotAvailable as error:
                faults.append(error.sqlstate)
                context.observe(
                    "actual_postgresql_checkpoint_lock_timeout", sqlstate=error.sqlstate
                )
                raise

    context.on_checkpoint = at_checkpoint
    monkeypatch.setattr(store_module.Store, "locked", locked)
    context.run()
    current = _assert_preserved(context)
    assert faults == ["55P03"]
    assert current["state"] == "failed", current["reason"]
    assert "scheduling" not in current
    _assert_foreground_finished(context)


_DELAYED_DOCKER_ACK = r"""
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
configuration = json.loads((root / 'configuration.json').read_text())
operation = sys.argv[1]
result = subprocess.run([configuration['docker'], *sys.argv[1:]], capture_output=True)
with (root / 'operations.jsonl').open('a') as output:
    output.write(json.dumps({'operation': operation, 'returncode': result.returncode,
                             'delegate_returned_monotonic': time.monotonic()}) + '\n')
if (root / 'armed').exists() and operation == configuration['fault_operation']:
    try:
        descriptor = os.open(root / 'fired', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
        # The real Docker CLI completed. Its ACK is withheld from the product;
        # the production supervisor must actually kill/reap this shim and EOF.
        time.sleep(30)
os.write(1, result.stdout)
os.write(2, result.stderr)
raise SystemExit(result.returncode)
"""


@pytest.mark.parametrize("fault_operation", ["rm", "ps"])
def test_worker_real_docker_delayed_ack_keeps_proof_or_durable_unknown(
    wait_fault, tmp_path, monkeypatch, fault_operation
):
    context = wait_fault()
    real_docker = shutil.which("docker")
    assert real_docker is not None
    control = tmp_path / "docker-ack-fault"
    control.mkdir()
    (control / "configuration.json").write_text(
        json.dumps({"docker": real_docker, "fault_operation": fault_operation})
    )
    executable = control / "docker"
    executable.write_text(f"#!{sys.executable}\n" + _DELAYED_DOCKER_ACK)
    executable.chmod(0o700)
    original_init = sandbox_module.Sandbox.__init__
    original_docker = sandbox_module.Sandbox._docker
    original_run = sandbox_module.Sandbox.run
    results = []
    monkeypatch.setattr(sandbox_module, "CLEANUP_SECONDS", 3.0)

    def init(sandbox, *args, **kwargs):
        original_init(sandbox, *args, **kwargs)
        sandbox.docker = str(executable)

    def docker(sandbox, *arguments, **kwargs):
        supervisor = kwargs.get("_supervisor")
        run_id = (
            supervisor.capability._operation.record["run_id"] if supervisor else None
        )
        if (
            arguments[0] == "rm"
            and run_id == context.background_id
            and context.foreground is None
        ):
            assert (context.workspace / "executed.txt").read_text() == "saved"
            context.enqueue()
            (control / "armed").write_text("fault only after foreground commit")
            context.observe("delayed_docker_ack_armed", operation=fault_operation)
        return original_docker(sandbox, *arguments, **kwargs)

    def run(sandbox, path, command, timeout, run_id, *args, **kwargs):
        value = original_run(sandbox, path, command, timeout, run_id, *args, **kwargs)
        if run_id == context.background_id:
            results.append(value)
        return value

    def response_with_shell():
        value = response("doonex_shell", {"command": "printf saved > executed.txt"}, 30)
        for tool, arguments, sequence in (
            ("doonex_shell", {"command": "touch sibling-must-not-run"}, 31),
            ("finish", {"message": "Synthetic Docker ACK finish"}, 32),
        ):
            value.choices[0].message.tool_calls.extend(
                response(tool, arguments, sequence).choices[0].message.tool_calls
            )
        return value

    context.on_transport = lambda: None
    context.response = response_with_shell
    monkeypatch.setattr(sandbox_module.Sandbox, "__init__", init)
    monkeypatch.setattr(sandbox_module.Sandbox, "_docker", docker)
    monkeypatch.setattr(sandbox_module.Sandbox, "run", run)
    context.run()
    assert (control / "fired").exists()
    assert len(results) == 1
    result = results[0]
    records = [
        json.loads(line)
        for line in (control / "operations.jsonl").read_text().splitlines()
    ]
    assert any(
        row["operation"] == fault_operation and row["returncode"] == 0
        for row in records
    )
    assert result["exit_code"] == 0
    assert all(
        item["reaped"] and item["pipe_eof"]
        for item in result["cleanup_evidence"]["processes"]
    )
    assert not (context.workspace / "sibling-must-not-run").exists()
    if fault_operation == "rm":
        current = _assert_preserved(context)
        assert not result["cleanup_unknown"]
        assert result["cleanup_evidence"]["container_absence_confirmed"] is True
        assert current["state"] == "yielded"
        assert [
            row["kind"] for row in action_rows(context.store, context.background_id)
        ] == ["model", "shell"]
        _assert_foreground_finished(context)
    else:
        assert result["cleanup_unknown"]
        assert any(type(error) is RuntimeError for error in context.errors)
        current = context.store.get(context.background_id)
        assert current["state"] == "interrupted"
        assert (
            current["first_started_at"] == context.background_before["first_started_at"]
        )
        assert current["request"] == context.background_before["request"]
        assert current["usage"]["calls"] == 1
        assert (
            current["usage"]["reserved_tokens"]
            == current["usage"]["unknown_calls"]
            == 0
        )
        assert [
            row["status"] for row in action_rows(context.store, context.background_id)
        ] == ["completed", "unknown"]
        assert context.store.get(context.foreground["id"])["state"] == "queued"
        assert context.calls == [context.background_id]
        original_rows = _controller_rows(context.store)
        unknown = [row for row in original_rows if row["data"]["state"] == "unknown"]
        assert (
            len(unknown) == 1
            and unknown[0]["data"]["operation_kind"] == "sandbox_execute"
        )
        assert unknown[0]["data"]["run_id"] == context.background_id
        name = unknown[0]["data"]["scope"]["container_name"]
        inspected = subprocess.run(
            [real_docker, "inspect", name], check=False, capture_output=True, timeout=10
        )
        assert (
            inspected.returncode != 0
        )  # actual remove succeeded before its lost/late ACK
        with pytest.raises(RuntimeError, match="终止状态不明"):
            Worker(context.settings).run(once=True)
        assert _controller_rows(context.store) == original_rows
        assert context.store.get(context.background_id)["usage"] == current["usage"]
        assert (
            context.store.get(context.background_id)["first_started_at"]
            == current["first_started_at"]
        )
        assert context.calls == [context.background_id]


def _crash_worker_child(settings, background_id, foreground_request, marker, stage):
    """An actual worker blocks at one named durable boundary until SIGKILL."""
    import doonex.worker as worker_module

    worker = Worker(settings)
    original_save = Scheduler.save_yield
    original_cleanup = worker_module.stop_containers
    foreground = []

    def gate():
        Path(marker).write_text(
            json.dumps({"foreground_id": foreground[0]["id"], "stage": stage})
        )
        end = time.monotonic() + 30
        while time.monotonic() < end:
            time.sleep(0.02)
        raise AssertionError("Fixture crash gate expired before parent killed worker")

    def transport(*args, **kwargs):
        assert not foreground
        foreground.append(worker.store.create_run(foreground_request))
        if stage == "cleanup_intent_committed":
            raise RuntimeError("Controlled provider failure before cleanup crash")
        return _tool_response()

    def save(scheduler, run_id, *args, **kwargs):
        assert run_id == background_id
        if stage == "checkpoint_before_commit":
            gate()
        value = original_save(scheduler, run_id, *args, **kwargs)
        if stage == "checkpoint_after_commit":
            gate()
        return value

    def cleanup(*args, **kwargs):
        if stage == "cleanup_intent_committed":
            gate()
        return original_cleanup(*args, **kwargs)

    sdk.GuardedLLM._transport_call = transport
    Scheduler.save_yield = save
    worker_module.stop_containers = cleanup
    worker.run()


def _controller_rows(store):
    with store.connect() as connection:
        return connection.execute(
            "SELECT id,run_id,parent_action_id,data FROM controller_operations ORDER BY id"
        ).fetchall()


def _session_hashes(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for session in directory.glob("session-*")
        for path in session.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "stage",
    ["checkpoint_before_commit", "checkpoint_after_commit", "cleanup_intent_committed"],
)
def test_worker_sigkill_at_checkpoint_or_cleanup_restarts_without_replay(
    tuning, store, request_data, tmp_path, monkeypatch, stage
):
    settings, _, _, _ = tuning
    _, _, _, batch = claimed(tuning, store)
    background_id = batch["attempts"][0]["run_id"]
    request = _foreground_request(request_data, tmp_path)
    marker = tmp_path / "worker-crash-gate.json"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_worker_child,
        args=(settings, background_id, request, str(marker), stage),
    )
    process.start()
    directory = run_directory(settings, background_id)
    calls = []
    try:
        end = time.monotonic() + 25
        while not marker.exists() and time.monotonic() < end and process.is_alive():
            time.sleep(0.02)
        assert marker.exists(), "Actual Worker did not reach the named crash boundary"
        front_id = json.loads(marker.read_text())["foreground_id"]
        before = store.get(background_id)
        assert before["first_started_at"] is not None
        assert (
            before["state"]
            == {
                "checkpoint_before_commit": "running",
                "checkpoint_after_commit": "yielded",
                "cleanup_intent_committed": "pausing",
            }[stage]
        )
        assert before["usage"]["calls"] == 1
        original_actions = action_rows(store, background_id)
        assert len(original_actions) == 1 and original_actions[0]["kind"] == "model"
        if stage == "cleanup_intent_committed":
            assert before["usage"]["unknown_calls"] == 1
            assert before["usage"]["reserved_tokens"] > 0
            assert original_actions[0]["status"] == "unknown"
        else:
            assert before["usage"]["tokens"] == 120
            assert before["usage"]["reserved_tokens"] == 0
            assert before["usage"]["unknown_calls"] == 0
            assert original_actions[0]["status"] == "completed"
        original_operations = _controller_rows(store)
        process.kill()
        process.join(timeout=10)
        assert not process.is_alive() and process.exitcode == -signal.SIGKILL
        session_hashes = _session_hashes(directory)
        assert session_hashes

        restarted = Worker(settings)
        selected = []
        original_execute = restarted.execute

        def execute(identifier):
            assert identifier == front_id, (
                "Restart selected background before queued foreground"
            )
            selected.append(identifier)
            return original_execute(identifier)

        def foreground_transport(*args, **kwargs):
            assert selected == [front_id]
            calls.append(selected[0])
            return response(
                "finish", {"message": "Synthetic foreground after crash"}, 20
            )

        monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", foreground_transport)
        monkeypatch.setattr(restarted, "execute", execute)
        if stage == "cleanup_intent_committed":
            assert cleanup_intent_unresolved(before)
            intent = deepcopy(before["cleanup_intent"])

            def forbidden(*args, **kwargs):
                pytest.fail("Pending cleanup intent was replayed after SIGKILL")

            monkeypatch.setattr("doonex.worker.stop_containers", forbidden)
            with pytest.raises(RuntimeError, match="终止状态不明"):
                restarted.run(once=True)
            current = store.get(background_id)
            assert current["cleanup_intent"] == intent
            assert current["usage"] == before["usage"]
            assert current["state"] == "interrupted"
            assert store.get(front_id)["state"] == "queued"
            assert not calls
            assert _controller_rows(store) == original_operations
            with store.connect() as connection:
                assert execution_fenced(connection)
        else:
            restarted.run(once=True)
            current = store.get(background_id)
            assert current["state"] == (
                "yielded" if stage == "checkpoint_after_commit" else "interrupted"
            )
            assert store.get(front_id)["state"] == "completed_verified"
            assert calls == [front_id]
            assert current["usage"] == before["usage"]
            if stage == "checkpoint_after_commit":
                assert current["scheduling"] == before["scheduling"]
            else:
                assert "scheduling" not in current
            assert all(row in _controller_rows(store) for row in original_operations)
        assert current["first_started_at"] == before["first_started_at"]
        assert current["request"] == before["request"]
        assert action_rows(store, background_id) == original_actions
        assert _session_hashes(directory) == session_hashes
        assert not (directory / "workspace" / "must-not-dispatch").exists()
        print(
            "worker_wait_crash="
            + json.dumps(
                {
                    "stage": stage,
                    "background_id": background_id,
                    "foreground_id": front_id,
                    "exitcode": process.exitcode,
                    "background_state_after_restart": current["state"],
                    "provider": "synthetic",
                    "latency_sample": False,
                },
                sort_keys=True,
            )
        )
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
