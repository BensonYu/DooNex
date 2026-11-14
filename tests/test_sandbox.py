import json
import os
import subprocess

import pytest
from doonex.sandbox import Sandbox, SandboxError
from sandbox_test_support import sandbox_context as sandbox_context_fixture

sandbox_context = sandbox_context_fixture

pytestmark = pytest.mark.docker


@pytest.mark.postgres
def test_isolation_files_network_environment_and_container_permissions(
    tmp_path, monkeypatch, sandbox_context
):
    sandbox = Sandbox()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-secret"
    outside.write_text("synthetic host secret")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-must-not-enter-container")
    run_id = sandbox_context.run_id
    script = """import json, os, socket
try:
    socket.create_connection(('1.1.1.1', 443), timeout=0.5)
    connected = True
except OSError:
    connected = False
try:
    open('/var/tmp/root-write-probe', 'w').write('bad')
    root_write = True
    root_errno = None
except OSError as error:
    root_write = False
    root_errno = error.errno
print(json.dumps({'uid': os.getuid(), 'outside': os.path.exists(OUTSIDE),
'key': 'OPENAI_API_KEY' in os.environ, 'network': connected, 'root_write': root_write,
'root_errno': root_errno, 'interfaces': os.listdir('/sys/class/net'),
'no_new_privileges': next(line.strip().split()[1] for line in open('/proc/self/status') if line.startswith('NoNewPrivs:')),
'socket': os.path.exists('/var/run/docker.sock'),
'capabilities': next(line.strip().split()[1] for line in open('/proc/self/status') if line.startswith('CapEff:'))}))
open('/workspace/result.txt', 'w').write('isolated')
"""
    script = script.replace("OUTSIDE", repr(str(outside)))
    result = sandbox.run(
        workspace,
        "python - <<'PY'\n" + script + "\nPY",
        10,
        run_id,
        **sandbox_context.binding,
    )
    assert result["exit_code"] == 0, result
    facts = json.loads(result["output"])
    assert facts == {
        "uid": os.getuid(),
        "outside": False,
        "key": False,
        "network": False,
        "root_write": False,
        "root_errno": 30,
        "interfaces": ["lo"],
        "no_new_privileges": "1",
        "socket": False,
        "capabilities": "0000000000000000",
    }
    assert (workspace / "result.txt").read_text() == "isolated"
    assert sandbox.containers(run_id, **sandbox_context.binding) == []


@pytest.mark.postgres
def test_timeout_kills_and_removes_container(tmp_path, sandbox_context):
    sandbox = Sandbox()
    run_id = sandbox_context.run_id
    # The total allowance includes durable registration and Docker create.
    # Reach the actual sleeping container before exercising execution timeout.
    result = sandbox.run(
        tmp_path, "printf started; sleep 30", 3, run_id, **sandbox_context.binding
    )
    assert result["container_id"] and result["output"] == "started"
    assert result["timed_out"]
    assert result["exit_code"] != 0
    assert not result["cleanup_error"]
    assert sandbox.containers(run_id, **sandbox_context.binding) == []


@pytest.mark.postgres
def test_cancel_stops_current_and_future_action(tmp_path, sandbox_context):
    sandbox = Sandbox()
    run_id = sandbox_context.run_id
    # Cancel the running shell, not an unacknowledged Docker create. The
    # original ten-second business deadline still covers all preparation.
    started = tmp_path / "started"
    result = sandbox.run(
        tmp_path,
        "printf running > started; sleep 30; touch late",
        10,
        run_id,
        lambda: started.exists() and started.read_text() == "running",
        **sandbox_context.binding,
    )
    assert started.read_text() == "running"
    assert result["container_id"]
    assert result["cancelled"] and not result["timed_out"]
    assert not result["cleanup_error"] and not result["cleanup_unknown"]
    assert not (tmp_path / "late").exists()
    assert sandbox.containers(run_id, **sandbox_context.binding) == []
    result = sandbox.run(
        tmp_path, "touch forbidden", 10, run_id, lambda: True, **sandbox_context.binding
    )
    assert result["cancelled"] and result["container_id"] is None
    assert not result["timed_out"]
    assert not result["cleanup_error"] and not result["cleanup_unknown"]
    assert not (tmp_path / "forbidden").exists()
    operation = next(
        row
        for row in sandbox_context.records()
        if row["operation_id"] == result["cleanup_evidence"]["controller_operation_id"]
    )
    assert not {"mount_validation", "docker_create", "docker_start"}.intersection(
        process["phase"] for process in operation["processes"]
    )


@pytest.mark.postgres
def test_recovery_can_find_and_stop_labeled_container(tmp_path, sandbox_context):
    sandbox = Sandbox()
    run_id = sandbox_context.run_id
    # Test-owned fixture setup deliberately does not call a product bypass.
    identifier = (
        subprocess.run(
            [
                sandbox.docker,
                "create",
                "--network",
                "none",
                "--label",
                "doonex.run_id=" + run_id,
                sandbox.image,
                "sleep",
                "30",
            ],
            check=True,
            stdout=subprocess.PIPE,
            timeout=10,
        )
        .stdout.decode()
        .strip()
    )
    try:
        subprocess.run([sandbox.docker, "start", identifier], check=True, timeout=10)
        assert identifier in sandbox.containers(run_id, **sandbox_context.binding)
        stopped = sandbox.stop_run(run_id, **sandbox_context.binding)
        assert identifier in stopped["stopped"]
        assert stopped["remaining"] == [] and stopped["errors"] == []
    finally:
        subprocess.run(
            [sandbox.docker, "rm", "-f", identifier], check=False, timeout=10
        )


def test_no_unpinned_image_or_invalid_scope():
    with pytest.raises(SandboxError):
        Sandbox(image="python:latest")
    with pytest.raises(SandboxError):
        Sandbox().stop_run("")
