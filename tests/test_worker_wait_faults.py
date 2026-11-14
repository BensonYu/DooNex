"""Actual Worker.run failure exits, with local synthetic provider responses.

Fault injection is named explicitly. A finite watchdog is only a test failure
guard; a refused/unknown execution never counts as a successful waiting sample.
"""

import hashlib
import json
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
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


class _FaultWatchdogExpired(BaseException):
    """A fixture failure guard, never a product deadline success."""


@contextmanager
def _failure_watchdog(events, seconds=60):
    # SIGALRM belongs to this main-thread test only. A repeating alarm can also
    # interrupt cleanup after the first alarm; the outer private runner remains
    # responsible for genuinely uninterruptible native calls/process death.
    assert threading.current_thread() is threading.main_thread()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    assert previous_timer == (0.0, 0.0)
    fired = []

    def expired(signum, frame):
        fired.append(time.monotonic())
        events.append(
            {"kind": "fixture_failure_watchdog_fired", "monotonic": fired[-1]}
        )
        raise _FaultWatchdogExpired(
            "Worker fault fixture exceeded its external failure guard"
        )

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        assert not fired, "Failure watchdog fired; this is a failed fault sample"


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


def _durable_snapshot(store):
    # This Store is the fixture's disposable schema. Preserve the actual rows
    # before resource cleanup/schema teardown, including failure/unknown data.
    tables = {
        "runs": "id",
        "events": "id",
        "actions": "id",
        "controller_operations": "id",
        "arena_evaluations": "id",
        "background_authorizations": "id",
        "background_allocations": "id",
        "background_days": "day",
    }
    with store.connect() as connection:
        return {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY {order}"
            ).fetchall()
            for table, order in tables.items()
        }


def _parent_accounting(store, evaluation_id):
    with store.connect() as connection:
        parent = connection.execute(
            "SELECT data FROM arena_evaluations WHERE id=%s", (evaluation_id,)
        ).fetchone()["data"]
        allocations = connection.execute(
            "SELECT * FROM background_allocations WHERE evaluation_id=%s ORDER BY id",
            (evaluation_id,),
        ).fetchall()
        days = connection.execute(
            "SELECT * FROM background_days WHERE day IN "
            "(SELECT day FROM background_allocations WHERE evaluation_id=%s) ORDER BY day",
            (evaluation_id,),
        ).fetchall()
    assert allocations and days
    # Startup sync legitimately changes only the parent's status metadata.
    # Frozen inputs, original first clock and every usage field remain exact.
    return {
        "arena_frozen_and_usage": {
            key: value
            for key, value in parent.items()
            if key not in {"state", "reason", "updated_at"}
        },
        "allocations": allocations,
        "days": days,
    }


def _cleanup_fixture_resources(settings, store, run_ids, *, processes=()):
    """Reap live handles and remove only inspected, registered fixture IDs."""
    before = _durable_snapshot(store)
    print(
        "worker_wait_fault_before_resource_cleanup="
        + json.dumps(before, default=str, sort_keys=True)
    )
    reaped = []
    for process in processes:
        # These are this parent process's actual Popen handles, never old DB
        # PIDs. A live, unreaped child cannot have had its PID reused.
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            reaped.append(process.pid)
        if process.stdout is not None:
            process.stdout.close()
    docker = shutil.which("docker")
    assert docker is not None
    removed = []
    for run_id in sorted(set(run_ids)):
        known_names = {
            row["data"]["scope"]["container_name"]
            for row in before["controller_operations"]
            if row["data"]["run_id"] == run_id
            and row["data"]["scope"]["container_name"] is not None
        }
        listed = subprocess.run(
            [
                docker,
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=doonex.run_id=" + run_id,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        for identifier in listed.stdout.splitlines():
            assert re.fullmatch(r"[0-9a-f]{64}", identifier)
            inspected = subprocess.run(
                [docker, "inspect", identifier],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            values = json.loads(inspected.stdout)
            assert len(values) == 1
            value = values[0]
            assert value["Id"] == identifier
            assert value["Config"]["Labels"]["doonex.run_id"] == run_id
            assert value["Name"].removeprefix("/") in known_names
            root = run_directory(settings, run_id).resolve()
            mounts = [
                mount
                for mount in value["Mounts"]
                if mount["Destination"] == "/workspace"
            ]
            assert len(mounts) == 1 and mounts[0]["Type"] == "bind"
            assert Path(mounts[0]["Source"]).resolve().is_relative_to(root)
            removed.append(
                {
                    "run_id": run_id,
                    "container_id": identifier,
                    "container_name": value["Name"],
                }
            )
            subprocess.run(
                [docker, "rm", "-f", identifier],
                capture_output=True,
                timeout=15,
                check=True,
            )
        remaining = subprocess.run(
            [
                docker,
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=doonex.run_id=" + run_id,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert remaining.stdout == ""
    assert _durable_snapshot(store) == before, (
        "Fixture cleanup changed product accounting or recovery records"
    )
    print(
        "worker_wait_fault_resource_cleanup="
        + json.dumps(
            {
                "removed_exact_ids": removed,
                "reaped_live_handles": reaped,
                "durable_records_unchanged": True,
            },
            sort_keys=True,
        )
    )


@pytest.fixture
def wait_fault(tuning, store, request_data, tmp_path, monkeypatch):
    """One background Run, then an ordinary foreground through Worker.run."""
    settings, _, body, _ = tuning
    foreground_request = _foreground_request(request_data, tmp_path)
    contexts = []

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
            processes=[],
        )
        contexts.append(context)

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
                with _failure_watchdog(context.events):
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
            assert not any(
                item["kind"] == "fixture_failure_watchdog_fired"
                for item in context.events
            ), "Failure watchdog fired; this is a failed fault sample"

        context.enqueue, context.observe, context.run = enqueue, observe, run

        def popen(*args, **kwargs):
            assert kwargs.get("start_new_session") is True
            process = subprocess.Popen(*args, **kwargs)
            context.processes.append(process)
            return process

        supervised_subprocess = SimpleNamespace(**{**vars(subprocess), "Popen": popen})
        monkeypatch.setattr(sandbox_module, "subprocess", supervised_subprocess)
        monkeypatch.setattr(workspace_module, "subprocess", supervised_subprocess)
        monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
        monkeypatch.setattr(sdk, "run_session", session)
        monkeypatch.setattr(Scheduler, "save_yield", checkpoint)
        monkeypatch.setattr(context.worker, "execute", execute)
        return context

    yield build
    for context in contexts:
        owned = [attempt["run_id"] for attempt in context.batch["attempts"]]
        if context.foreground is not None:
            owned.append(context.foreground["id"])
        _cleanup_fixture_resources(settings, store, owned, processes=context.processes)


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
    guard = threading.Lock()
    watchdog_fired = []
    watchdog = []

    def release(*, rescue=False):
        with guard:
            if held:
                sdk._RUN_LOCK.release()
                held.clear()
                if rescue:
                    watchdog_fired.append(time.monotonic())
                context.observe("actual_sdk_lock_released", watchdog_rescue=rescue)
        if not rescue and watchdog:
            watchdog[0].cancel()
            watchdog[0].join(timeout=2)
            assert not watchdog[0].is_alive()

    def lock_and_enqueue():
        assert sdk._RUN_LOCK.acquire(blocking=False)
        with guard:
            held.append(True)
        context.enqueue()
        context.observe("actual_sdk_lock_held")
        timer = threading.Timer(
            max(0, context.deadline - time.monotonic()) + 10,
            lambda: release(rescue=True),
        )
        timer.daemon = True
        watchdog.append(timer)
        timer.start()

    context.on_session = lock_and_enqueue
    context.after_background = release
    try:
        context.run()
    finally:
        release()
    assert not watchdog_fired, "SDK-lock rescue fired; this is a failed fault sample"
    current = _assert_preserved(context, calls=0)
    assert current["state"] == "budget_exceeded", current["reason"]
    assert current["reason"] == "运行的共同执行期限已到，停止新增工作"
    released = [
        item for item in context.events if item["kind"] == "actual_sdk_lock_released"
    ]
    assert len(released) == 1 and released[0]["monotonic"] >= context.deadline
    assert released[0]["watchdog_rescue"] is False
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
binding = json.loads((root / 'current-call.json').read_text())
assert binding['arguments'] == sys.argv[1:]
delegate = subprocess.Popen([configuration['docker'], *sys.argv[1:]], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
stdout, stderr = delegate.communicate()
record = {**binding, 'delegate_pid': delegate.pid, 'shim_pid': os.getpid(),
          'delegate_executable': configuration['docker'], 'returncode': delegate.returncode,
          'delegate_returned_monotonic': time.monotonic()}
with (root / 'operations.jsonl').open('a') as output:
    output.write(json.dumps(record) + '\n')
if ((root / 'armed').exists() and operation == configuration['fault_operation']
        and binding['run_id'] == configuration['run_id']):
    try:
        descriptor = os.open(root / 'fired-claim', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
        pending = root / ('fired-' + str(os.getpid()) + '.tmp')
        pending.write_text(json.dumps({**record, 'phase': 'actual_delegate_returned_ack_withheld'}))
        os.replace(pending, root / 'fired.json')
        # The real Docker CLI completed. Its ACK is withheld from the product;
        # the production supervisor must actually kill/reap this shim and EOF.
        time.sleep(30)
os.write(1, stdout)
os.write(2, stderr)
raise SystemExit(delegate.returncode)
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
        json.dumps(
            {
                "docker": real_docker,
                "fault_operation": fault_operation,
                "run_id": context.background_id,
            }
        )
    )
    executable = control / "docker"
    executable.write_text(f"#!{sys.executable}\n" + _DELAYED_DOCKER_ACK)
    executable.chmod(0o700)
    original_init = sandbox_module.Sandbox.__init__
    original_docker = sandbox_module.Sandbox._docker
    original_process = sandbox_module._Supervisor.process
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
        assert supervisor is not None
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

    def supervised_process(supervisor, arguments, *args, **kwargs):
        if arguments[0] == str(executable):
            operation_record = supervisor.capability._operation.record
            # start uses the supervisor directly, so observe the actual launch
            # boundary rather than assuming every CLI traverses Sandbox._docker.
            # Worker actions are sequential; the shim also verifies exact argv.
            (control / "current-call.json").write_text(
                json.dumps(
                    {
                        "operation": arguments[1],
                        "arguments": list(arguments[1:]),
                        "run_id": operation_record["run_id"],
                        "controller_operation_id": operation_record["operation_id"],
                        "controller_operation_kind": operation_record["operation_kind"],
                        "container_name": operation_record["scope"]["container_name"],
                    }
                )
            )
        return original_process(supervisor, arguments, *args, **kwargs)

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
    monkeypatch.setattr(sandbox_module._Supervisor, "process", supervised_process)
    monkeypatch.setattr(sandbox_module.Sandbox, "run", run)
    context.run()
    fired = json.loads((control / "fired.json").read_text())
    assert fired["phase"] == "actual_delegate_returned_ack_withheld"
    assert fired["operation"] == fault_operation
    assert fired["run_id"] == context.background_id
    assert fired["controller_operation_kind"] == "sandbox_execute"
    assert fired["delegate_executable"] == real_docker
    assert fired["returncode"] == 0
    assert fired["delegate_pid"] > 0 and fired["shim_pid"] > 0
    assert len(results) == 1
    result = results[0]
    records = [
        json.loads(line)
        for line in (control / "operations.jsonl").read_text().splitlines()
    ]
    assert [
        {**row, "phase": fired["phase"]}
        for row in records
        if row["shim_pid"] == fired["shim_pid"]
    ] == [fired]
    matching_operations = [
        row
        for row in _controller_rows(context.store)
        if row["data"]["operation_id"] == fired["controller_operation_id"]
    ]
    assert len(matching_operations) == 1
    actual_operation = matching_operations[0]["data"]
    assert actual_operation["run_id"] == context.background_id
    assert actual_operation["scope"]["container_name"] == fired["container_name"]
    assert any(
        item["pid"] == fired["shim_pid"]
        and item["phase"] == "docker_" + fault_operation
        for item in actual_operation["processes"]
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
        absent = subprocess.run(
            [
                real_docker,
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=doonex.run_id=" + context.background_id,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (
            absent.stdout == ""
        )  # successful complete observation, not inspect failure
        with _failure_watchdog([]), pytest.raises(RuntimeError, match="终止状态不明"):
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
    processes = []

    def popen(*args, **kwargs):
        assert kwargs.get("start_new_session") is True
        process = subprocess.Popen(*args, **kwargs)
        processes.append(process)
        return process

    supervised_subprocess = SimpleNamespace(**{**vars(subprocess), "Popen": popen})
    sandbox_module.subprocess = supervised_subprocess
    workspace_module.subprocess = supervised_subprocess

    def terminated(signum, frame):
        worker.stopping = True
        raise _FaultWatchdogExpired("Parent aborted an unreached crash fixture gate")

    signal.signal(signal.SIGTERM, terminated)

    def gate():
        # These gates contain no external operation in flight. Actual SIGKILL
        # must never leave an untracked local CLI handle behind in this test.
        operations = _controller_rows(worker.store)
        assert all(
            row["data"]["state"] in {"terminal", "not_spawned"} for row in operations
        )
        assert all(process.poll() is not None for process in processes)
        pending = Path(marker).with_suffix(f".{os.getpid()}.tmp")
        pending.write_text(
            json.dumps(
                {
                    "foreground_id": foreground[0]["id"],
                    "background_id": background_id,
                    "stage": stage,
                    "no_live_controller_operation": True,
                    "terminal_controller_hashes": [
                        row["data"]["record_sha256"] for row in operations
                    ],
                }
            )
        )
        os.replace(pending, marker)
        end = time.monotonic() + 30
        while time.monotonic() < end:
            time.sleep(0.02)
        worker.stopping = True
        raise AssertionError("Fixture crash gate expired before parent killed worker")

    def transport(*args, **kwargs):
        assert not foreground
        foreground.append(worker.store.create_run(foreground_request))
        if stage == "cleanup_intent_committed":
            raise RuntimeError("Controlled provider failure before cleanup crash")
        return _tool_response()

    def save(scheduler, run_id, *args, **kwargs):
        assert run_id == background_id
        if stage == "checkpoint_before_save":
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
    try:
        worker.run()
    finally:
        _cleanup_fixture_resources(
            settings,
            worker.store,
            [background_id, *[item["id"] for item in foreground]],
            processes=processes,
        )


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
    ["checkpoint_before_save", "checkpoint_after_commit", "cleanup_intent_committed"],
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
    front_id = None
    processes = []
    try:
        end = time.monotonic() + 25
        while not marker.exists() and time.monotonic() < end and process.is_alive():
            time.sleep(0.02)
        assert marker.exists(), "Actual Worker did not reach the named crash boundary"
        gate = json.loads(marker.read_text())
        assert gate["background_id"] == background_id
        assert gate["stage"] == stage and gate["no_live_controller_operation"] is True
        front_id = gate["foreground_id"]
        before = store.get(background_id)
        assert before["first_started_at"] is not None
        assert (
            before["state"]
            == {
                "checkpoint_before_save": "running",
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
        assert all(
            row["data"]["state"] in {"terminal", "not_spawned"}
            for row in original_operations
        )
        assert [row["data"]["record_sha256"] for row in original_operations] == gate[
            "terminal_controller_hashes"
        ]
        original_parent_accounting = _parent_accounting(store, batch["id"])
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

        def popen(*args, **kwargs):
            assert kwargs.get("start_new_session") is True
            child = subprocess.Popen(*args, **kwargs)
            processes.append(child)
            return child

        supervised_subprocess = SimpleNamespace(**{**vars(subprocess), "Popen": popen})
        monkeypatch.setattr(sandbox_module, "subprocess", supervised_subprocess)
        monkeypatch.setattr(workspace_module, "subprocess", supervised_subprocess)
        monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", foreground_transport)
        monkeypatch.setattr(restarted, "execute", execute)
        if stage == "cleanup_intent_committed":
            assert cleanup_intent_unresolved(before)
            intent = deepcopy(before["cleanup_intent"])

            def forbidden(*args, **kwargs):
                pytest.fail("Pending cleanup intent was replayed after SIGKILL")

            monkeypatch.setattr("doonex.worker.stop_containers", forbidden)
            with (
                _failure_watchdog([]),
                pytest.raises(RuntimeError, match="终止状态不明"),
            ):
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
            with _failure_watchdog([]):
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
        assert _parent_accounting(store, batch["id"]) == original_parent_accounting
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
                    "parent_accounting_before_kill": original_parent_accounting,
                    "parent_accounting_after_restart": _parent_accounting(
                        store, batch["id"]
                    ),
                },
                sort_keys=True,
                default=str,
            )
        )
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        assert not process.is_alive(), "Actual worker child was not reaped"
        owned = [attempt["run_id"] for attempt in batch["attempts"]]
        # If the child failed before publishing its marker, its foreground
        # creation may still have committed. Discover only rows bound to this
        # fixture's authored repository, then inspect IDs/labels/mounts below.
        with store.connect() as connection:
            owned.extend(
                row["data"]["id"]
                for row in connection.execute("SELECT data FROM runs").fetchall()
                if row["data"]["request"]["repo_path"] == request["repo_path"]
            )
        _cleanup_fixture_resources(settings, store, owned, processes=processes)
