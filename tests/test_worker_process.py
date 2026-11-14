"""Kill a real worker while its real container is active; model traffic is fake."""

import copy
import hashlib
import json
import multiprocessing
import re
import subprocess
import time
from pathlib import Path

import pytest
from doonex.api import create_app
from doonex.controller_operations import fenced
from doonex.sdk import GuardedLLM
from doonex.worker import Worker, run_directory
from fastapi.testclient import TestClient
from test_worker import prepare_run, response


def _worker_child(settings):
    from doonex.sdk import GuardedLLM
    from doonex.worker import Worker

    GuardedLLM._transport_call = lambda *a, **k: response(
        "doonex_shell",
        {"command": "printf x >> progress.txt; sleep 60; printf bad > late.txt"},
        1,
    )
    Worker(settings).run(once=True)


@pytest.mark.docker
def test_killed_worker_preserves_changes_and_blocks_unresolved_controller(
    settings, store, request_data, tmp_path, monkeypatch
):
    request_data["budget"]["max_calls"] = 3
    run, _, _ = prepare_run(
        store,
        settings,
        request_data,
        tmp_path,
        [
            "python -c \"from pathlib import Path; assert Path('progress.txt').read_text() == 'x'; assert not Path('late.txt').exists()\""
        ],
    )
    process = multiprocessing.get_context("spawn").Process(
        target=_worker_child, args=(settings,)
    )
    process.start()
    directory = run_directory(settings, run["id"])
    progress = directory / "workspace" / "progress.txt"
    owned_container_id = None

    def actions():
        with store.connect() as connection:
            return connection.execute(
                "SELECT id,kind,status,data FROM actions WHERE run_id=%s ORDER BY id",
                (run["id"],),
            ).fetchall()

    def operations():
        with store.connect() as connection:
            return connection.execute(
                "SELECT id,run_id,parent_action_id,data FROM controller_operations "
                "WHERE run_id=%s ORDER BY id",
                (run["id"],),
            ).fetchall()

    try:
        deadline = time.monotonic() + 20
        while (
            not progress.exists() and time.monotonic() < deadline and process.is_alive()
        ):
            time.sleep(0.05)
        assert progress.exists()
        assert progress.read_text() == "x"
        assert not progress.with_name("late.txt").exists()
        before = store.get(run["id"])
        assert before["state"] == "running"
        assert before["first_started_at"] is not None
        assert before["owner"]
        assert before["usage"]["calls"] == 1
        assert before["usage"]["tokens"] == 120
        assert before["usage"]["unknown_calls"] == 0
        assert before["usage"]["reserved_tokens"] == 0
        original_actions = actions()
        assert len(original_actions) == 2
        model = [action for action in original_actions if action["kind"] == "model"]
        shell = [action for action in original_actions if action["kind"] == "shell"]
        assert len(model) == len(shell) == 1
        assert model[0]["status"] == "completed"
        assert shell[0]["status"] == "pending"
        original_operations = operations()
        pending = [
            row
            for row in original_operations
            if row["data"]["operation_kind"] == "sandbox_execute"
            and row["data"]["state"] != "terminal"
        ]
        assert len(pending) == 1
        operation = pending[0]
        record = operation["data"]
        assert str(operation["id"]) == record["operation_id"]
        assert str(operation["run_id"]) == record["run_id"] == run["id"]
        assert str(operation["parent_action_id"]) == record["parent_action_id"]
        assert record["parent_action_id"] == str(shell[0]["id"])
        assert record["project_id"] == before["request"]["project_id"]
        assert record["expected_run_owner"] == before["owner"]
        assert record["revision"] >= 1
        assert re.fullmatch(r"[0-9a-f]{64}", record["record_sha256"])
        assert record["scope"]["run_label"] == "doonex.run_id=" + run["id"]
        name = record["scope"]["container_name"]
        assert re.fullmatch(r"doonex-[0-9a-f]{32}", name)
        # Capture only this fixture's exact ID before killing its controller.
        # Cleanup below never trusts a broad label query or a later name lookup.
        inspected = json.loads(
            subprocess.check_output(["docker", "inspect", name], text=True, timeout=10)
        )
        assert len(inspected) == 1
        container = inspected[0]
        assert re.fullmatch(r"[0-9a-f]{64}", container["Id"])
        assert container["Name"] == "/" + name
        assert container["Config"]["Labels"]["doonex.run_id"] == run["id"]
        mounts = [m for m in container["Mounts"] if m["Destination"] == "/workspace"]
        assert len(mounts) == 1
        assert mounts[0]["Type"] == "bind"
        assert Path(mounts[0]["Source"]).resolve() == progress.parent.resolve()
        owned_container_id = container["Id"]
        session_names = sorted(path.name for path in directory.glob("session-*"))
        assert session_names == ["session-executor-0"]
        process.kill()
        process.join(timeout=10)
        assert not process.is_alive()
        assert process.exitcode is not None and process.exitcode != 0
        session_hashes = {
            str(path.relative_to(directory)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for session in directory.glob("session-*")
            for path in session.rglob("*")
            if path.is_file()
        }
        assert session_hashes
        original_assignments = copy.deepcopy(before["assignments"])
        for assignment in original_assignments.values():
            assignment.pop("state")
        calls = []

        def no_model(*args, **kwargs):
            calls.append(1)
            raise AssertionError(
                "An unresolved killed controller must prevent dispatch"
            )

        monkeypatch.setattr(GuardedLLM, "_transport_call", no_model)

        def preserved():
            current = store.get(run["id"])
            assert current["state"] == "interrupted"
            assert current["first_started_at"] == before["first_started_at"]
            assert current["request"]["budget"] == before["request"]["budget"]
            assert current["usage"] == before["usage"]
            assert current.get("session_generation") == before.get("session_generation")
            assert current.get("resume_session") == before.get("resume_session")
            assert current["active_assignment"] == before["active_assignment"]
            assignments = copy.deepcopy(current["assignments"])
            assert assignments["executor"]["state"] == "interrupted"
            for assignment in assignments.values():
                assignment.pop("state")
            assert assignments == original_assignments
            expected_actions = copy.deepcopy(original_actions)
            next(a for a in expected_actions if a["kind"] == "shell")["status"] = (
                "unknown"
            )
            assert actions() == expected_actions
            assert operations() == original_operations
            with store.connect() as connection:
                assert fenced(connection)
            assert progress.read_text() == "x"
            assert not progress.with_name("late.txt").exists()
            assert (
                sorted(path.name for path in directory.glob("session-*"))
                == session_names
            )
            assert {
                str(path.relative_to(directory)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for session in directory.glob("session-*")
                for path in session.rglob("*")
                if path.is_file()
            } == session_hashes
            assert not calls

        # A lost controller cannot be resolved by an ordinary unknown-action
        # acknowledgement, a restart or a new cleanup group. Keep its row intact.
        with pytest.raises(RuntimeError, match="终止状态不明"):
            Worker(settings).run(once=True)
        preserved()
        with TestClient(create_app(settings)) as client:
            client.headers["Authorization"] = "Bearer " + settings.token
            for acknowledge_unknown in (False, True):
                result = client.post(
                    f"/api/runs/{run['id']}/resume",
                    json={"acknowledge_unknown": acknowledge_unknown},
                )
                assert result.status_code == 400, result.text
                preserved()
        with pytest.raises(RuntimeError, match="终止状态不明"):
            Worker(settings).run(once=True)
        preserved()
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        if owned_container_id is not None:
            subprocess.run(
                ["docker", "rm", "-f", owned_container_id],
                capture_output=True,
                timeout=15,
                check=True,
            )
