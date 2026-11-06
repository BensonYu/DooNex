"""Real SDK loop, PostgreSQL and Docker; only the paid model transport is fake."""

import json
import threading
import time
from pathlib import Path

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


def prepare_review_run(store, settings, request_data, tmp_path, checks=None):
    request_data["workflow"] = "execute_review"
    request_data["budget"]["max_calls"] = 4
    return prepare_run(store, settings, request_data, tmp_path, checks)


def scripted_review(
    monkeypatch,
    report,
    *,
    review_command=None,
    crash_review=False,
    executor_command="printf 'answer = 42\\n' > hello.py",
):
    answers = [
        response("doonex_shell", {"command": executor_command}, 1),
        response("finish", {"message": "Executor done"}, 2),
    ]
    if review_command:
        answers.append(response("doonex_shell", {"command": review_command}, 3))
    answers.append(response("finish", {"message": report}, 4))
    calls = []

    def transport(*args, **kwargs):
        calls.append(kwargs)
        if crash_review and len(calls) == 3:
            raise SystemExit("simulated worker termination before reviewer response")
        return answers.pop(0)

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    return calls


APPROVED = json.dumps(
    {"status": "approved", "summary": "Reviewed the change", "findings": []}
)


@pytest.mark.docker
@pytest.mark.parametrize(
    ("review_message", "check", "expected", "valid"),
    [
        (APPROVED, "true", "completed_verified", True),
        (APPROVED, "false", "failed", True),
        (
            json.dumps(
                {
                    "status": "changes_requested",
                    "summary": "Needs a fix",
                    "findings": [
                        {
                            "path": "hello.py",
                            "line": 1,
                            "severity": "medium",
                            "message": "Explain the constant",
                        }
                    ],
                }
            ),
            "true",
            "completed_unverified",
            True,
        ),
        ("unstructured approval", "true", "completed_unverified", False),
        (
            json.dumps(
                {
                    "status": "changes_requested",
                    "summary": "Needs a fix",
                    "findings": [
                        {
                            "path": "missing.py",
                            "severity": "high",
                            "message": "No such file",
                        }
                    ],
                }
            ),
            "true",
            "completed_unverified",
            False,
        ),
    ],
)
def test_two_role_flow_binds_review_and_checks_without_automatic_repair(
    settings,
    store,
    request_data,
    tmp_path,
    monkeypatch,
    review_message,
    check,
    expected,
    valid,
):
    run, repo, _ = prepare_review_run(store, settings, request_data, tmp_path, [check])
    calls = scripted_review(monkeypatch, review_message)
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == expected, final["reason"]
    assert final["usage"]["calls"] == len(calls) == 3
    assert final["assignments"]["executor"]["usage"]["calls"] == 2
    assert final["assignments"]["reviewer"]["usage"]["calls"] == 1
    assert all(item["state"] == "completed" for item in final["assignments"].values())
    assert final["review"]["valid"] is valid
    assert final["review"]["assignment_id"] == final["assignments"]["reviewer"]["id"]
    assert final["review"]["snapshot_digest"] == final["evidence"]["snapshot_digest"]
    assert final["handoff"]["snapshot_digest"] == final["evidence"]["snapshot_digest"]
    assert (repo / "hello.py").read_text() == "answer = 1\n"
    directory = run_directory(settings, run["id"])
    assert (directory / "session-executor-0").is_dir()
    assert (directory / "session-reviewer-0").is_dir()
    assert final["baseline_digest"]
    with store.connect() as connection:
        actions = connection.execute(
            "SELECT data FROM actions WHERE run_id=%s", (run["id"],)
        ).fetchall()
    for action in actions:
        data = action["data"]
        assert data["assignment_id"] == final["assignments"][data["role"]]["id"]


@pytest.mark.docker
def test_reviewer_context_is_real_readonly_mount_without_executor_write_access(
    settings, store, request_data, tmp_path, monkeypatch
):
    run, _, _ = prepare_review_run(store, settings, request_data, tmp_path, ["true"])
    command = "test -f before/hello.py && test -f after/hello.py && test -f diff.patch && printf bad > after/hello.py"
    calls = scripted_review(monkeypatch, APPROVED, review_command=command)
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert len(calls) == 4
    directory = run_directory(settings, run["id"])
    assert (directory / "workspace" / "hello.py").read_text() == "answer = 42\n"
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT data FROM actions WHERE run_id=%s AND kind='shell'", (run["id"],)
        ).fetchall()
    review_action = next(
        row["data"] for row in rows if row["data"]["role"] == "reviewer"
    )
    assert review_action["result"]["exit_code"] != 0
    assert "read-only" in review_action["result"]["output"].lower()


@pytest.mark.docker
def test_pause_and_resume_reviewer_never_replays_completed_executor(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.sandbox import Sandbox

    run, _, _ = prepare_review_run(store, settings, request_data, tmp_path, ["true"])
    calls = scripted_review(monkeypatch, APPROVED, review_command="cat diff.patch")
    original, paused = Sandbox.run, False

    def pause_review(self, *args, **kwargs):
        nonlocal paused
        result = original(self, *args, **kwargs)
        if kwargs.get("read_only") and not paused:
            paused = True
            with store.locked(run["id"]) as (_, current):
                current.update(state="pausing", requested_action="pause")
        return result

    monkeypatch.setattr(Sandbox, "run", pause_review)
    Worker(settings).run(once=True)
    first = store.get(run["id"])
    assert first["state"] == "paused", first["reason"]
    assert first["phase"] == "review"
    assert first["assignments"]["executor"]["state"] == "completed"
    assert first["assignments"]["reviewer"]["state"] == "paused"
    assert len(calls) == 3
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        resumed = client.post(
            f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": False}
        )
        assert resumed.status_code == 200, resumed.text
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert len(calls) == 4
    assert final["first_started_at"] == first["first_started_at"]
    assert final["assignments"]["executor"] == first["assignments"]["executor"]
    assert final["assignments"]["reviewer"]["session_generation"] == 0
    assert final["usage"]["tokens"] == 480


@pytest.mark.docker
def test_executor_checkpoint_survives_crash_before_handoff_database_commit(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.review import Review

    run, _, _ = prepare_review_run(store, settings, request_data, tmp_path, ["true"])
    calls = scripted_review(monkeypatch, APPROVED)
    original, crashed = Review.prepare, False

    def crash_once(*args, **kwargs):
        nonlocal crashed
        result = original(*args, **kwargs)
        if not crashed:
            crashed = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Review, "prepare", crash_once)
    Worker(settings).run(once=True)
    first = store.get(run["id"])
    assert first["state"] == "interrupted", first["reason"]
    assert first["assignments"]["executor"]["state"] == "completed"
    assert first["phase"] == "execution"
    assert len(calls) == 2
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        resumed = client.post(
            f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": True}
        )
        assert resumed.status_code == 200, resumed.text
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert len(calls) == 3
    assert final["first_started_at"] == first["first_started_at"]
    assert final["assignments"]["executor"] == first["assignments"]["executor"]


@pytest.mark.docker
def test_reviewer_cannot_reset_executor_call_budget(
    settings, store, request_data, tmp_path, monkeypatch
):
    run, _, _ = prepare_review_run(store, settings, request_data, tmp_path)
    with store.locked(run["id"]) as (_, current):
        current["request"]["budget"]["max_calls"] = 2
    calls = scripted_review(monkeypatch, APPROVED)
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "budget_exceeded", final["reason"]
    assert len(calls) == final["usage"]["calls"] == 2
    assert final["assignments"]["executor"]["state"] == "completed"
    assert final["assignments"]["reviewer"]["usage"]["calls"] == 0
    assert not final.get("evidence")


@pytest.mark.docker
def test_final_checks_cannot_verify_a_snapshot_different_from_review(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.evidence import Evidence

    run, _, _ = prepare_review_run(store, settings, request_data, tmp_path, ["true"])
    scripted_review(monkeypatch, APPROVED)
    original = Evidence.capture

    def change_after_review(workspace, *args, **kwargs):
        (workspace.path / "hello.py").write_text("answer = 99\n")
        return original(workspace, *args, **kwargs)

    monkeypatch.setattr(Evidence, "capture", change_after_review)
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_unverified", final["reason"]
    assert final["evidence"]["checks"][0]["exit_code"] == 0
    assert final["review"]["snapshot_digest"] != final["evidence"]["snapshot_digest"]


@pytest.mark.docker
def test_unknown_reviewer_call_requires_acknowledgment_and_keeps_shared_reservation(
    settings, store, request_data, tmp_path, monkeypatch
):
    run, _, _ = prepare_review_run(store, settings, request_data, tmp_path, ["true"])
    calls = scripted_review(monkeypatch, APPROVED, crash_review=True)
    Worker(settings).run(once=True)
    first = store.get(run["id"])
    assert first["state"] == "interrupted", first["reason"]
    assert len(calls) == 3
    assert first["usage"]["unknown_calls"] == 1
    assert first["assignments"]["reviewer"]["usage"]["unknown_calls"] == 1
    assert first["usage"]["reserved_tokens"] > 0
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        rejected = client.post(
            f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": False}
        )
        assert rejected.status_code == 400
        resumed = client.post(
            f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": True}
        )
        assert resumed.status_code == 200, resumed.text
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert len(calls) == final["usage"]["calls"] == 4
    assert final["usage"]["reserved_tokens"] == first["usage"]["reserved_tokens"]
    assert final["usage"]["unknown_calls"] == 1
    assert final["assignments"]["reviewer"]["session_generation"] == 1
    assert final["first_started_at"] == first["first_started_at"]


@pytest.mark.docker
def test_verification_resume_after_review_does_not_reopen_either_role(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.sandbox import Sandbox

    run, _, _ = prepare_review_run(store, settings, request_data, tmp_path, ["true"])
    calls = scripted_review(monkeypatch, APPROVED)
    original, paused = Sandbox.run, False

    def pause_verification(self, *args, **kwargs):
        nonlocal paused
        result = original(self, *args, **kwargs)
        if store.get(run["id"])["state"] == "verifying" and not paused:
            paused = True
            with store.locked(run["id"]) as (_, current):
                current.update(state="pausing", requested_action="pause")
        return result

    monkeypatch.setattr(Sandbox, "run", pause_verification)
    Worker(settings).run(once=True)
    first = store.get(run["id"])
    assert first["state"] == "paused", first["reason"]
    assert first["phase"] == "verification"
    assert all(item["state"] == "completed" for item in first["assignments"].values())
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        resumed = client.post(
            f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": False}
        )
        assert resumed.status_code == 200, resumed.text
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert len(calls) == 3
    assert final["assignments"] == first["assignments"]
    assert final["first_started_at"] == first["first_started_at"]


@pytest.mark.docker
def test_legacy_run_without_assignments_remains_executable(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.workspace import Workspace

    run, _, _ = prepare_run(store, settings, request_data, tmp_path, ["true"])
    directory = run_directory(settings, run["id"])
    directory.mkdir(parents=True)
    Workspace.create(
        Path(run["request"]["repo_path"]),
        run["request"]["base_commit"],
        directory / "workspace",
    )
    with store.locked(run["id"]) as (_, current):
        current.pop("assignments")
        current.pop("active_assignment")
        current.pop("phase")
        current.update(state="paused", first_started_at=now())
    calls = scripted_model(monkeypatch, [])
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        resumed = client.post(
            f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": False}
        )
        assert resumed.status_code == 200, resumed.text
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert len(calls) == final["usage"]["calls"] == 1
    assert "assignments" not in final
    assert (run_directory(settings, run["id"]) / "session-0").is_dir()
    assert not (directory / "baseline").exists()


@pytest.mark.docker
def test_review_finding_can_reference_deleted_file_in_bound_baseline(
    settings, store, request_data, tmp_path, monkeypatch
):
    report = json.dumps(
        {
            "status": "changes_requested",
            "summary": "The module was deleted",
            "findings": [
                {
                    "path": "hello.py",
                    "line": 1,
                    "severity": "high",
                    "message": "Restore this module",
                }
            ],
        }
    )
    run, _, _ = prepare_review_run(store, settings, request_data, tmp_path, ["true"])
    scripted_review(monkeypatch, report, executor_command="rm hello.py")
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_unverified", final["reason"]
    assert final["review"]["valid"] is True
    assert final["review"]["findings"][0]["side"] == "before"


@pytest.mark.docker
def test_valid_large_review_patch_uses_review_context_resource_bounds(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.workspace import MAX_FILE_BYTES

    run, repo, git = prepare_review_run(
        store, settings, request_data, tmp_path, ["true"]
    )
    # Each project version is within its file bound; their one-line diff is
    # larger than a project file and uses Review's separate context bound.
    size = MAX_FILE_BYTES // 2 + 1024
    (repo / "large.txt").write_text("a" * size)
    git("add", "large.txt")
    git("commit", "-qm", "Add bounded large input")
    with store.locked(run["id"]) as (_, current):
        current["request"]["base_commit"] = git("rev-parse", "HEAD")
    scripted_review(
        monkeypatch,
        APPROVED,
        executor_command=f"python -c \"from pathlib import Path; Path('large.txt').write_text('b' * {size})\"",
        review_command="wc -c diff.patch",
    )
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    patch = run_directory(settings, run["id"]) / "review" / "context" / "diff.patch"
    assert patch.stat().st_size > MAX_FILE_BYTES
