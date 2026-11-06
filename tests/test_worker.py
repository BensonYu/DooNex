"""Real SDK loop, PostgreSQL and Docker; only the paid model transport is fake."""

import json
import threading
import time

import pytest
from doonex.api import create_app
from doonex.budget import Ledger
from doonex.sdk import GuardedLLM
from doonex.store import now
from doonex.worker import WORKER_LOCK, Worker, run_directory
from fastapi.testclient import TestClient
from litellm import ModelResponse
from test_api import repository


def response(tool, arguments, sequence):
    return ModelResponse(
        id=f"offline-{sequence}",
        model="gpt-4.1-mini",
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"tool-{sequence}",
                            "type": "function",
                            "function": {
                                "name": tool,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )


def prepare_run(store, settings, request_data, tmp_path, checks=None):
    repo, git = repository(tmp_path)
    request_data.update(
        repo_path=str(repo),
        base_commit=git("rev-parse", "HEAD"),
        checks=checks or ["python -c 'from hello import answer; assert answer == 42'"],
    )
    request_data["model"].update(
        provider="openai", model_id="gpt-4.1-mini", max_input_tokens=32768
    )
    return store.create_run(request_data), repo, git


def scripted_model(monkeypatch, commands):
    answers = [
        response("doonex_shell", {"command": command}, index)
        for index, command in enumerate(commands)
    ]
    answers.append(
        response("finish", {"message": "Offline fixture complete"}, len(commands))
    )
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return answers.pop(0)

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    return calls


@pytest.mark.docker
@pytest.mark.parametrize("check_passes", [True, False])
def test_real_sdk_container_evidence_flow(
    settings, store, request_data, tmp_path, monkeypatch, check_passes
):
    check = (
        "python -c 'from hello import answer; assert answer == 42'"
        if check_passes
        else "python -c 'raise AssertionError(\"expected failure\")'"
    )
    run, repo, git = prepare_run(store, settings, request_data, tmp_path, [check])
    (repo / "hello.py").write_text("answer = 999\n")
    git("add", "hello.py")
    (repo / "untouched.txt").write_text("private dirty source")
    original_status = git("status", "--porcelain=v1")
    calls = scripted_model(
        monkeypatch,
        [
            "python -c \"from pathlib import Path; Path('hello.py').write_text('answer = 42\\n'); Path('added.txt').write_text('new file')\""
        ],
    )
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == ("completed_verified" if check_passes else "failed"), (
        final["reason"]
    )
    assert len(calls) == final["usage"]["calls"] == 2
    assert final["usage"]["tokens"] == 240
    assert final["evidence"]["checks"][0]["exit_code"] == (0 if check_passes else 1)
    assert git("status", "--porcelain=v1") == original_status
    assert (repo / "hello.py").read_text() == "answer = 999\n"
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "added.txt" in client.get(f"/api/runs/{run['id']}/diff").json()["diff"]
        if check_passes:
            directory = run_directory(settings, run["id"])
            (directory / "workspace" / "added.txt").write_text(
                "changed after verification"
            )
            assert (
                client.get(f"/api/runs/{run['id']}").json()["state"]
                == "completed_unverified"
            )


@pytest.mark.docker
def test_budget_exhaustion_does_not_dispatch_next_sdk_request(
    settings, store, request_data, tmp_path, monkeypatch
):
    request_data["budget"]["max_calls"] = 1
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    calls = scripted_model(monkeypatch, ["printf done > progress.txt"])
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "budget_exceeded", final["reason"]
    assert len(calls) == 1 and final["usage"]["calls"] == 1
    assert (
        run_directory(settings, run["id"]) / "workspace" / "progress.txt"
    ).read_text() == "done"


@pytest.mark.docker
def test_cancel_inflight_tool_stops_later_model_calls(
    settings, store, request_data, tmp_path, monkeypatch
):
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    calls = scripted_model(
        monkeypatch, ["printf saved > progress.txt; sleep 30; printf bad > late.txt"]
    )
    worker = Worker(settings)
    thread = threading.Thread(target=lambda: worker.run(once=True))
    thread.start()
    try:
        deadline = time.monotonic() + 15
        progress = run_directory(settings, run["id"]) / "workspace" / "progress.txt"
        while not progress.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert progress.exists()
        with TestClient(create_app(settings)) as client:
            client.headers["Authorization"] = "Bearer " + settings.token
            assert (
                client.post(f"/api/runs/{run['id']}/cancel").json()["state"]
                == "pausing"
            )
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert store.get(run["id"])["state"] == "cancelled"
        assert len(calls) == 1
        assert not progress.with_name("late.txt").exists()
    finally:
        worker.stopping = True
        thread.join(timeout=15)


@pytest.mark.docker
def test_restart_reconciles_unknown_call_without_reset_or_automatic_resume(
    settings, store, request_data, tmp_path
):
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    with store.locked(run["id"]) as (_, current):
        current.update(state="running", first_started_at=now())
    Ledger(store, run["id"]).reserve(100, 128)
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "interrupted"
    assert final["usage"]["calls"] == 1 and final["usage"]["reserved_tokens"] == 228
    assert final["usage"]["unknown_calls"] == 1
    assert store.pending(run["id"])[0]["status"] == "unknown"


def test_second_worker_cannot_claim_execution(settings, store):
    with store.connect() as c:
        c.autocommit = True
        c.execute("SELECT pg_advisory_lock(%s)", (WORKER_LOCK,))
        with pytest.raises(RuntimeError, match="已有 worker"):
            Worker(settings).run(once=True)


def test_docker_failure_records_unknown_termination(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.sandbox import Sandbox, SandboxError

    run, _, _ = prepare_run(store, settings, request_data, tmp_path)

    def unavailable(*args, **kwargs):
        raise SandboxError("Docker unavailable")

    monkeypatch.setattr(Sandbox, "stop_run", unavailable)
    with store.locked(run["id"]) as (_, current):
        current.update(state="running", first_started_at=now())
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "interrupted"
    assert final["recovery"]["remaining"] == ["unknown"]


@pytest.mark.docker
def test_pause_during_verification_restarts_checks_without_model_replay(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.sandbox import Sandbox

    run, _, _ = prepare_run(
        store, settings, request_data, tmp_path, ["python -c 'assert 1 == 1'"]
    )
    calls = scripted_model(monkeypatch, [])
    original = Sandbox.run
    interrupted = False

    def pause_check(
        self, workspace, command, timeout, run_id, should_stop=lambda: False
    ):
        nonlocal interrupted
        result = original(self, workspace, command, timeout, run_id, should_stop)
        if not interrupted:
            interrupted = True
            with store.locked(run_id) as (_, current):
                current.update(state="pausing", requested_action="pause")
        return result

    monkeypatch.setattr(Sandbox, "run", pause_check)
    Worker(settings).run(once=True)
    assert store.get(run["id"])["state"] == "paused"
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert (
            client.post(
                f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": False}
            ).status_code
            == 200
        )
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert len(calls) == 1
    assert list(run_directory(settings, run["id"]).glob("evidence-attempt-*"))


@pytest.mark.docker
def test_interrupted_cancel_reconciles_live_container(
    settings, store, request_data, tmp_path
):
    import subprocess

    from doonex.sandbox import TASK_IMAGE, Sandbox

    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    with store.locked(run["id"]) as (_, current):
        current.update(state="interrupted", first_started_at=now())
    sandbox = Sandbox()
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--network",
            "none",
            "--read-only",
            "--label",
            "doonex.run_id=" + run["id"],
            TASK_IMAGE,
            "sleep",
            "30",
        ],
        check=True,
        capture_output=True,
    )
    try:
        assert sandbox.containers(run["id"])
        with TestClient(create_app(settings)) as client:
            client.headers["Authorization"] = "Bearer " + settings.token
            result = client.post(f"/api/runs/{run['id']}/cancel").json()
        assert result["state"] == "cancelled"
        assert not sandbox.containers(run["id"])
    finally:
        sandbox.stop_run(run["id"])


@pytest.mark.docker
def test_report_resealing_does_not_override_database_evidence(
    settings, store, request_data, tmp_path, monkeypatch
):
    import hashlib

    run, _, _ = prepare_run(store, settings, request_data, tmp_path, ["true"])
    scripted_model(monkeypatch, [])
    Worker(settings).run(once=True)
    assert store.get(run["id"])["state"] == "completed_verified"
    evidence = run_directory(settings, run["id"]) / "evidence"
    report = json.loads((evidence / "report.json").read_text())
    report["created_at"] = "tampered"
    payload = json.dumps(report).encode()
    (evidence / "report.json").write_bytes(payload)
    (evidence / "report.sha256").write_text(hashlib.sha256(payload).hexdigest())
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert (
            client.get(f"/api/runs/{run['id']}").json()["state"]
            == "completed_unverified"
        )
