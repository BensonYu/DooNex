"""Subprocess fault probes with real PostgreSQL grants; no task Docker/API calls."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from doonex import controller_operations as co
from doonex import sandbox as implementation
from doonex.sandbox import (
    MAX_CONTAINER_IDS,
    MAX_CONTROL_OUTPUT_BYTES,
    MAX_OUTPUT_BYTES,
    Sandbox,
    SandboxError,
)
from sandbox_test_support import sandbox_context as sandbox_context_fixture

sandbox_context = sandbox_context_fixture

IDENTIFIER = "a" * 64

FAKE_DOCKER = r"""
import json, os, sys, time
from pathlib import Path
root = Path(__file__).parent
settings = json.loads((root / 'scenario.json').read_text())
operation = sys.argv[1]
with (root / 'operations.jsonl').open('a') as output:
    output.write(json.dumps({'operation': operation, 'at': time.monotonic(), 'arguments': sys.argv[2:]}) + '\n')
state = root / 'container'
if operation == 'create':
    name = sys.argv[sys.argv.index('--name') + 1]
    if settings.get('late_create_delay'):
        pid = os.fork()
        if pid == 0:
            os.setsid()
            (root / 'daemon.pid').write_text(str(os.getpid()))
            os.close(0)
            os.close(1)
            os.close(2)
            time.sleep(settings['late_create_delay'])
            if settings.get('late_create_gate'):
                until = time.monotonic() + 15
                while not (root / 'late-create-release').exists():
                    if time.monotonic() >= until:
                        os._exit(2)
                    time.sleep(0.01)
            state.write_text(name)
            os._exit(0)
    else:
        state.write_text(name)
    time.sleep(settings.get('create_delay', 0))
    sys.stdout.write(settings.get('create_output', 'a' * 64 + '\n'))
elif operation == 'start':
    time.sleep(settings.get('start_delay', 0))
    os.write(1, b'x' * settings.get('shell_output_bytes', 0))
    if settings.get('pipe_child'):
        pid = os.fork()
        if pid == 0:
            if settings.get('escaped_child'):
                os.setsid()
            (root / 'child.pid').write_text(str(os.getpid()))
            time.sleep(30)
            os._exit(0)
    if settings.get('start_wait'):
        time.sleep(30)
elif operation == 'inspect':
    time.sleep(settings.get('inspect_delay', 0))
    print(settings.get('exit_code', 0))
elif operation == 'rm':
    if not settings.get('rm_fail'):
        state.unlink(missing_ok=True)
    time.sleep(settings.get('rm_delay', 0))
    if settings.get('rm_fail'):
        print('synthetic refusal')
        raise SystemExit(1)
elif operation == 'ps':
    time.sleep(settings.get('ps_delay', 0))
    if 'ps_output' in settings:
        sys.stdout.write(settings['ps_output'])
    elif state.exists():
        print('a' * 64)
"""


@pytest.fixture
def fake_docker(tmp_path, monkeypatch, sandbox_context):
    control = tmp_path / "control"
    control.mkdir()
    executable = control / "docker"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    executable.chmod(0o700)
    (control / "scenario.json").write_text("{}")
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 0.35)

    def configure(**settings):
        (control / "scenario.json").write_text(json.dumps(settings))
        return Sandbox(docker=str(executable))

    yield control, configure, sandbox_context
    for name in ["child.pid", "daemon.pid"]:
        child = control / name
        if child.exists():
            try:
                os.kill(int(child.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def operations(control):
    path = control / "operations.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def assert_reaped_and_eof(evidence):
    assert not evidence["local_process_unknown"], evidence
    assert evidence["processes"]
    assert all(
        record["reaped"] and record["pipe_eof"] for record in evidence["processes"]
    )
    for record in evidence["processes"]:
        with pytest.raises(ChildProcessError):
            os.waitpid(record["pid"], os.WNOHANG)
    json.dumps(evidence)


@pytest.mark.postgres
def test_common_deadline_covers_create_and_attach(fake_docker, tmp_path):
    control, configure, context = fake_docker
    sandbox = configure(create_delay=0.2, start_delay=0.8)
    entered = time.monotonic()
    result = sandbox.run(tmp_path, "true", 0.70, context.run_id, **context.binding)
    elapsed = time.monotonic() - entered
    assert result["timed_out"]
    assert elapsed < 0.70 + 0.35 + 0.3
    assert not result["cleanup_unknown"], result
    assert not (control / "container").exists()
    assert "inspect" not in [item["operation"] for item in operations(control)]
    assert_reaped_and_eof(result["cleanup_evidence"])


@pytest.mark.postgres
def test_caller_deadline_does_not_refresh_at_sandbox_entry(fake_docker, tmp_path):
    control, configure, context = fake_docker
    sandbox = configure(create_delay=30)
    deadline = time.monotonic() + 0.55
    time.sleep(0.12)
    result = sandbox.run(
        tmp_path, "true", 3, context.run_id, deadline=deadline, **context.binding
    )
    assert result["timed_out"]
    assert result["cleanup_unknown"], result
    assert result["cleanup_evidence"]["daemon_request_unknown"]
    assert time.monotonic() < deadline + 0.35 + 0.3
    assert not (control / "container").exists()


@pytest.mark.postgres
def test_expired_caller_deadline_never_launches_a_process(
    fake_docker, tmp_path, monkeypatch
):
    _, configure, context = fake_docker

    def forbidden_launch(*arguments, **kwargs):
        pytest.fail("Expired business deadline started a process")

    monkeypatch.setattr(implementation.subprocess, "Popen", forbidden_launch)
    with pytest.raises(co.RegistryError, match="deadline"):
        configure().run(
            tmp_path,
            "true",
            3,
            context.run_id,
            deadline=time.monotonic() - 1,
            **context.binding,
        )
    assert context.records() == []


@pytest.mark.postgres
def test_create_ack_loss_is_cleaned_by_preselected_name(fake_docker, tmp_path):
    control, configure, context = fake_docker
    sandbox = configure(create_delay=30)
    result = sandbox.run(tmp_path, "true", 0.35, context.run_id, **context.binding)
    assert result["timed_out"] and result["container_id"] is None
    assert result["cleanup_unknown"], result
    calls = operations(control)
    created = next(item for item in calls if item["operation"] == "create")
    removed = next(item for item in calls if item["operation"] == "rm")
    name = created["arguments"][created["arguments"].index("--name") + 1]
    assert removed["arguments"] == ["-f", name]
    listed = next(item for item in calls if item["operation"] == "ps")
    assert "label=doonex.run_id=" + context.run_id in listed["arguments"]
    assert not any(argument.startswith("name=") for argument in listed["arguments"])
    assert result["cleanup_evidence"]["container_snapshot_absence_confirmed"] is True
    assert result["cleanup_evidence"]["container_absence_confirmed"] is False
    assert result["cleanup_evidence"]["daemon_request_unknown"]
    assert_reaped_and_eof(result["cleanup_evidence"])


@pytest.mark.postgres
def test_late_daemon_create_cannot_be_cleared_by_current_empty_ps(
    fake_docker, tmp_path, monkeypatch
):
    control, configure, context = fake_docker
    sandbox = configure(create_delay=30, late_create_delay=0.01, late_create_gate=True)
    result = sandbox.run(tmp_path, "true", 3, context.run_id, **context.binding)
    evidence = result["cleanup_evidence"]
    assert result["timed_out"] and result["cleanup_unknown"]
    assert evidence["daemon_request_unknown"]
    assert evidence["container_snapshot_absence_confirmed"] is True
    assert evidence["container_absence_confirmed"] is False
    assert not evidence["local_process_unknown"]
    assert_reaped_and_eof(evidence)
    assert (control / "daemon.pid").exists()
    assert not (control / "container").exists()
    # Materialize only after the real empty ps, independent of startup latency.
    (control / "late-create-release").touch()
    wait_until = time.monotonic() + 1.5
    while not (control / "container").exists() and time.monotonic() < wait_until:
        time.sleep(0.02)
    assert (control / "container").exists(), (
        "Synthetic daemon did not finish the late create"
    )
    original = context.records()[0]
    # Repair has its own allowance; the original unknown record stays fenced.
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 3)
    with pytest.raises(co.RegistryError, match="fenced"):
        sandbox.stop_run(context.run_id, **context.binding)
    assert (
        sandbox.stop_run(
            context.run_id,
            repair_of=original["operation_id"],
            **context.cleanup_binding,
        )["remaining"]
        == []
    )
    assert original in context.records() and context.fenced()
    assert result["cleanup_unknown"] and evidence["daemon_request_unknown"]


@pytest.mark.parametrize("output", ["short\n", IDENTIFIER + "\n\n", "a" * 100_000])
@pytest.mark.postgres
def test_invalid_or_excess_create_ack_never_bypasses_cleanup(
    fake_docker, tmp_path, output
):
    control, configure, context = fake_docker
    with pytest.raises(SandboxError) as raised:
        configure(create_output=output).run(
            tmp_path, "true", 3, context.run_id, **context.binding
        )
    assert raised.value.cleanup_unknown
    assert raised.value.cleanup_evidence["daemon_request_unknown"]
    assert raised.value.cleanup_evidence["container_absence_confirmed"] is False
    assert raised.value.cleanup_evidence["container_snapshot_absence_confirmed"] is True
    assert not (control / "container").exists()
    assert "start" not in [item["operation"] for item in operations(control)]


@pytest.mark.parametrize(
    "output",
    [
        "short\n",
        IDENTIFIER + " \n",
        IDENTIFIER + "\n" + IDENTIFIER + "\n",
        "\n",
        "é\n",
        "a" * 100_000,
    ],
)
@pytest.mark.postgres
def test_incomplete_or_malformed_container_lists_are_never_empty(fake_docker, output):
    _, configure, context = fake_docker
    sandbox = configure(ps_output=output)
    with pytest.raises(SandboxError):
        sandbox.containers(context.run_id, **context.binding)
    original = context.records()[0]
    with pytest.raises(co.RegistryError, match="fenced"):
        sandbox.stop_run(context.run_id, **context.binding)
    stopped = sandbox.stop_run(
        context.run_id,
        repair_of=original["operation_id"],
        **context.cleanup_binding,
    )
    assert stopped["remaining"] == ["unknown"]
    assert stopped["errors"] and stopped["cleanup_unknown"]
    assert original in context.records() and context.fenced()


@pytest.mark.postgres
def test_container_count_limit_is_strict(fake_docker):
    _, configure, context = fake_docker
    output = "".join(f"{index:064x}\n" for index in range(MAX_CONTAINER_IDS + 1))
    with pytest.raises(SandboxError):
        configure(ps_output=output).containers(context.run_id, **context.binding)


@pytest.mark.postgres
def test_complete_bounded_container_list_is_preserved(fake_docker):
    _, configure, context = fake_docker
    identifiers = [f"{index:064x}" for index in range(MAX_CONTAINER_IDS)]
    assert (
        configure(ps_output="\n".join(identifiers) + "\n").containers(
            context.run_id, **context.binding
        )
        == identifiers
    )


@pytest.mark.postgres
def test_final_absence_check_covers_every_container_with_run_label(
    fake_docker, tmp_path
):
    control, configure, context = fake_docker
    result = configure(ps_output="b" * 64 + "\n").run(
        tmp_path, "true", 3, context.run_id, **context.binding
    )
    assert result["cleanup_unknown"]
    assert result["cleanup_evidence"]["container_absence_confirmed"] is False
    listed = next(item for item in operations(control) if item["operation"] == "ps")
    assert listed["arguments"] == [
        "-aq",
        "--no-trunc",
        "--filter",
        "label=doonex.run_id=" + context.run_id,
    ]


@pytest.mark.parametrize("exit_code", [-1, 256, "unknown", "1\n2"])
@pytest.mark.postgres
def test_malformed_exit_state_cleans_container_and_raises(
    fake_docker, tmp_path, exit_code
):
    control, configure, context = fake_docker
    with pytest.raises(SandboxError, match="exit code") as raised:
        configure(exit_code=exit_code).run(
            tmp_path, "true", 3, context.run_id, **context.binding
        )
    assert not raised.value.cleanup_unknown
    assert not (control / "container").exists()


@pytest.mark.postgres
def test_shell_output_keeps_two_mib_and_discards_excess(fake_docker, tmp_path):
    _, configure, context = fake_docker
    result = configure(shell_output_bytes=MAX_OUTPUT_BYTES + 30_000).run(
        tmp_path, "true", 3, context.run_id, **context.binding
    )
    assert result["exit_code"] == 0 and result["output_truncated"]
    assert result["output"] == "x" * MAX_OUTPUT_BYTES
    assert not result["cleanup_unknown"]
    assert_reaped_and_eof(result["cleanup_evidence"])


@pytest.mark.postgres
def test_control_output_is_finite_and_raises(fake_docker):
    _, configure, context = fake_docker
    _, supervisor = context.supervisor(container_name="synthetic")
    with pytest.raises(SandboxError, match="output_limit"):
        configure(create_output="x" * (MAX_CONTROL_OUTPUT_BYTES + 1))._docker(
            "create",
            "--name",
            "synthetic",
            timeout=2,
            _supervisor=supervisor,
        )
    assert_reaped_and_eof(supervisor.evidence)


@pytest.mark.postgres
def test_exited_cli_with_inherited_pipe_is_not_completed(fake_docker, tmp_path):
    _, configure, context = fake_docker
    result = configure(pipe_child=True).run(
        tmp_path, "true", 0.5, context.run_id, **context.binding
    )
    assert result["timed_out"]
    assert not result["cleanup_unknown"], result
    record = next(
        item
        for item in result["cleanup_evidence"]["processes"]
        if item["operation"] == "start"
    )
    assert record["reaped"] and record["pipe_eof"]


@pytest.mark.postgres
def test_escaped_pipe_stays_unknown_despite_empty_container_list(
    fake_docker, tmp_path, monkeypatch
):
    control, configure, context = fake_docker
    sandbox = configure(pipe_child=True, escaped_child=True)
    entered = time.monotonic()
    result = sandbox.run(tmp_path, "true", 3, context.run_id, **context.binding)
    assert time.monotonic() - entered < 3 + 0.35 + 0.3
    assert result["timed_out"] and result["cleanup_unknown"]
    assert result["cleanup_error"]
    evidence = result["cleanup_evidence"]
    assert evidence["local_process_unknown"]
    record = next(
        item for item in evidence["processes"] if item["operation"] == "start"
    )
    assert record["reaped"] and not record["pipe_eof"]
    assert (control / "child.pid").exists()
    # A later successful container-only reconciliation cannot repair this evidence.
    (control / "scenario.json").write_text(json.dumps({"ps_output": ""}))
    original = context.records()[0]
    # Repair has its own allowance; the original unknown record stays fenced.
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 3)
    with pytest.raises(co.RegistryError, match="fenced"):
        sandbox.stop_run(context.run_id, **context.binding)
    assert (
        sandbox.stop_run(
            context.run_id,
            repair_of=original["operation_id"],
            **context.cleanup_binding,
        )["remaining"]
        == []
    )
    assert original in context.records() and context.fenced()
    assert result["cleanup_unknown"] and evidence["local_process_unknown"]


@pytest.mark.postgres
def test_lost_remove_ack_resolves_only_after_complete_ps(
    fake_docker, tmp_path, monkeypatch
):
    control, configure, context = fake_docker
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 1.0)
    sandbox = configure(rm_delay=30)
    entered = time.monotonic()
    result = sandbox.run(tmp_path, "true", 3, context.run_id, **context.binding)
    assert time.monotonic() - entered < 1.8
    assert not result["cleanup_unknown"], result
    assert result["cleanup_evidence"]["container_absence_confirmed"] is True
    assert_reaped_and_eof(result["cleanup_evidence"])
    assert [item["operation"] for item in operations(control)][-2:] == ["rm", "ps"]


@pytest.mark.postgres
def test_finite_cleanup_window_cannot_refresh_for_stalled_ps(
    fake_docker, tmp_path, monkeypatch
):
    control, configure, context = fake_docker
    # Give the second CLI enough launch time to test an actually stalled ps,
    # rather than merely exhausting the window before it can start.
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 1.0)
    sandbox = configure(rm_delay=30, ps_delay=30)
    entered = time.monotonic()
    result = sandbox.run(tmp_path, "true", 3, context.run_id, **context.binding)
    assert time.monotonic() - entered < 1.8
    assert result["cleanup_unknown"]
    assert result["cleanup_evidence"]["container_absence_confirmed"] is False
    calls = operations(control)
    removed = next(item for item in calls if item["operation"] == "rm")
    listed = next(item for item in calls if item["operation"] == "ps")
    assert listed["at"] - removed["at"] < 1.0


@pytest.mark.postgres
def test_cancel_during_create_preserves_cleanup_proof(fake_docker, tmp_path):
    _, configure, context = fake_docker
    stop_at = time.monotonic() + 0.35
    result = configure(create_delay=30).run(
        tmp_path,
        "true",
        3,
        context.run_id,
        lambda: time.monotonic() >= stop_at,
        **context.binding,
    )
    assert result["cancelled"] and not result["timed_out"]
    assert result["cleanup_unknown"]
    assert result["cleanup_evidence"]["daemon_request_unknown"]
    assert_reaped_and_eof(result["cleanup_evidence"])


@pytest.mark.postgres
def test_mount_scan_uses_same_deadline_and_is_reaped(
    fake_docker, tmp_path, monkeypatch
):
    control, configure, context = fake_docker
    original = subprocess.Popen

    def delayed_mount(arguments, **kwargs):
        if arguments[0] == sys.executable:
            arguments = [sys.executable, "-c", "import time; time.sleep(30)"]
        return original(arguments, **kwargs)

    monkeypatch.setattr(implementation.subprocess, "Popen", delayed_mount)
    entered = time.monotonic()
    result = configure().run(tmp_path, "true", 0.2, context.run_id, **context.binding)
    assert time.monotonic() - entered < 0.8
    assert result["timed_out"] and not result["cleanup_unknown"]
    # The controller requires a complete observation to finish a Sandbox group,
    # even when mount validation prevented every container mutation.
    calls = operations(control)
    assert [item["operation"] for item in calls] == ["ps"]
    assert calls[0]["arguments"] == [
        "-aq",
        "--no-trunc",
        "--filter",
        "label=doonex.run_id=" + context.run_id,
    ]
    (record,) = context.records()
    assert record["state"] == "terminal"
    assert [item["phase"] for item in record["processes"]] == [
        "mount_validation",
        "docker_ps",
    ]
    assert not context.fenced()
    assert_reaped_and_eof(result["cleanup_evidence"])


@pytest.mark.postgres
def test_callback_failure_reaps_process_and_preserves_cause(
    monkeypatch, sandbox_context
):
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 0.3)
    _, supervisor = sandbox_context.supervisor()
    evidence = supervisor.evidence

    calls = 0

    def failed_stop():
        nonlocal calls
        calls += 1
        # Two checks intentionally precede Popen; fail in the real poll loop.
        if calls <= 2:
            return False
        raise RuntimeError("synthetic callback failed")

    with pytest.raises(SandboxError) as raised:
        supervisor.process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            deadline=time.monotonic() + 1,
            operation="mount_validation",
            capability=supervisor.capability,
            maximum=100,
            should_stop=failed_stop,
        )
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert_reaped_and_eof(evidence)


@pytest.mark.postgres
def test_callback_failure_before_launch_starts_no_process(monkeypatch, sandbox_context):
    _, supervisor = sandbox_context.supervisor()
    evidence = supervisor.evidence

    def failed_stop():
        raise RuntimeError("synthetic callback failed before launch")

    def forbidden_launch(*arguments, **kwargs):
        pytest.fail("Prelaunch callback failure started a process")

    monkeypatch.setattr(implementation.subprocess, "Popen", forbidden_launch)
    with pytest.raises(RuntimeError, match="before launch"):
        supervisor.process(
            [sys.executable],
            deadline=time.monotonic() + 1,
            operation="mount_validation",
            capability=supervisor.capability,
            maximum=100,
            should_stop=failed_stop,
        )
    assert evidence["processes"] == []
    assert not evidence["local_process_unknown"]


@pytest.mark.postgres
def test_inherited_pipe_keeps_leader_identity_until_group_signal(
    monkeypatch, sandbox_context
):
    monkeypatch.setattr(implementation, "CLEANUP_SECONDS", 0.3)
    original_launch = subprocess.Popen
    original_signal = os.killpg
    polls_before_signal = []
    signals = []

    def tracked_launch(*arguments, **kwargs):
        process = original_launch(*arguments, **kwargs)
        original_poll = process.poll

        def tracked_poll():
            if not signals:
                polls_before_signal.append(process.pid)
            return original_poll()

        process.poll = tracked_poll
        return process

    def tracked_signal(group, value):
        signals.append(group)
        return original_signal(group, value)

    monkeypatch.setattr(implementation.subprocess, "Popen", tracked_launch)
    monkeypatch.setattr(implementation.os, "killpg", tracked_signal)
    _, supervisor = sandbox_context.supervisor()
    evidence = supervisor.evidence
    result = supervisor.process(
        [
            sys.executable,
            "-c",
            "import os, time; child = os.fork(); time.sleep(30) if child == 0 else None",
        ],
        deadline=time.monotonic() + 0.2,
        operation="mount_validation",
        capability=supervisor.capability,
        maximum=100,
    )
    assert result.reason == "timeout"
    assert polls_before_signal == []
    assert len(signals) == 1
    assert_reaped_and_eof(evidence)


@pytest.mark.parametrize("timeout", [True, float("nan"), float("inf"), 0, -1, 3601])
def test_timeout_is_finite_explicit_number(tmp_path, timeout):
    with pytest.raises(SandboxError, match="timeout"):
        Sandbox().run(tmp_path, "true", timeout, "invalid_timeout")


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), "invalid"])
def test_caller_deadline_is_finite_monotonic_number(tmp_path, deadline):
    with pytest.raises(SandboxError, match="deadline"):
        Sandbox().run(tmp_path, "true", 1, "invalid_deadline", deadline=deadline)
