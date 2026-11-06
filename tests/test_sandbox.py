import json
import os
import time
import uuid

import pytest
from doonex.sandbox import Sandbox, SandboxError

pytestmark = pytest.mark.docker


def test_isolation_files_network_environment_and_container_permissions(
    tmp_path, monkeypatch
):
    sandbox = Sandbox()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-secret"
    outside.write_text("synthetic host secret")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-must-not-enter-container")
    run_id = uuid.uuid4().hex
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
    result = sandbox.run(workspace, "python - <<'PY'\n" + script + "\nPY", 10, run_id)
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
    assert sandbox.containers(run_id) == []


def test_timeout_kills_and_removes_container(tmp_path):
    sandbox = Sandbox()
    run_id = uuid.uuid4().hex
    result = sandbox.run(tmp_path, "sleep 30", 0.4, run_id)
    assert result["timed_out"]
    assert result["exit_code"] != 0
    assert not result["cleanup_error"]
    assert sandbox.containers(run_id) == []


def test_cancel_stops_current_and_future_action(tmp_path):
    sandbox = Sandbox()
    run_id = uuid.uuid4().hex
    deadline = time.monotonic() + 0.6
    result = sandbox.run(
        tmp_path, "sleep 30", 10, run_id, lambda: time.monotonic() > deadline
    )
    assert result["cancelled"]
    assert sandbox.containers(run_id) == []
    result = sandbox.run(tmp_path, "touch forbidden", 10, run_id, lambda: True)
    assert result["cancelled"] and result["container_id"] is None
    assert not (tmp_path / "forbidden").exists()


def test_recovery_can_find_and_stop_labeled_container(tmp_path):
    sandbox = Sandbox()
    run_id = uuid.uuid4().hex
    identifier = (
        sandbox._docker(
            "create",
            "--network",
            "none",
            "--label",
            "doonex.run_id=" + run_id,
            sandbox.image,
            "sleep",
            "30",
        )
        .stdout.decode()
        .strip()
    )
    try:
        sandbox._docker("start", identifier)
        assert identifier in sandbox.containers(run_id)
        stopped = sandbox.stop_run(run_id)
        assert identifier in stopped["stopped"]
        assert stopped["remaining"] == [] and stopped["errors"] == []
    finally:
        sandbox._docker("rm", "-f", identifier, check=False)


def test_no_unpinned_image_or_invalid_scope():
    with pytest.raises(SandboxError):
        Sandbox(image="python:latest")
    with pytest.raises(SandboxError):
        Sandbox().stop_run("")
