"""Actual worker/API/SDK persistence with synthetic model transport and test DB."""

import json
from pathlib import Path

import pytest
from doonex.api import create_app
from doonex.generation import Generation, StartInput
from doonex.sandbox import Sandbox
from doonex.sdk import GuardedLLM
from doonex.worker import Worker, run_directory
from doonex.workspace import Workspace
from fastapi.testclient import TestClient
from test_generation import HIDDEN, OUTPUT, import_body
from test_generation import setup_generation as setup_generation  # noqa: PLC0414 - export pytest fixture
from test_worker import response


def enqueue(service, body):
    draft = service.create(body)
    started = service.start(
        draft["id"],
        StartInput(
            expected_revision=draft["revision"],
            confirm_model_usage=True,
            confirm_derivative_rights=True,
        ),
    )
    return draft, service.store.get(started["run_id"])


def transport(monkeypatch, output=OUTPUT, tool="finish"):
    requests = []

    def synthetic(*args, **kwargs):
        requests.append(kwargs)
        assert len(requests) == 1, "Generation must not repair or replay model output"
        return response(tool, {"message": output}, len(requests))

    def forbidden(*args, **kwargs):
        pytest.fail("Generation must not export the project or execute a container")

    monkeypatch.setattr(GuardedLLM, "_transport_call", synthetic)
    monkeypatch.setattr(Workspace, "create", forbidden)
    monkeypatch.setattr(Sandbox, "run", forbidden)
    return requests


def resume(service, run_id):
    with TestClient(create_app(service.settings)) as client:
        client.headers["Authorization"] = "Bearer " + service.settings.token
        report = client.get(f"/api/runs/{run_id}/recovery")
        assert report.status_code == 200, report.text
        assert report.json()["workspace_ok"] and not report.json()["containers"]
        result = client.post(
            f"/api/runs/{run_id}/resume", json={"acknowledge_unknown": True}
        )
        assert result.status_code == 200, result.text
        return result.json()


def test_generation_actual_worker_api_import_preserves_assets(
    setup_generation, monkeypatch
):
    service, body, files, _taskset, project = setup_generation
    calls = transport(monkeypatch)
    with TestClient(create_app(service.settings)) as client:
        client.headers["Authorization"] = "Bearer " + service.settings.token
        assert client.get("/api/skills/generations/inputs").status_code == 200
        created = client.post("/api/skills/generations", json=body.model_dump())
        assert created.status_code == 200, created.text
        draft = created.json()
        path = f"/api/skills/generations/{draft['id']}"
        started = client.post(
            path + "/start",
            json={
                "expected_revision": draft["revision"],
                "confirm_model_usage": True,
                "confirm_derivative_rights": True,
            },
        )
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]
        Worker(service.settings).run(once=True)
        final = service.store.get(run_id)
        assert final["state"] == "completed_unverified", final["reason"]
        assert final["generation_candidate"]["valid"]
        assert final["usage"]["calls"] == len(calls) == 1
        assert final["usage"]["tokens"] == 120
        assert final["evidence"]["checks"] == []
        assert final["request"]["repo_path"] is None
        assert final["request"]["base_commit"] is None
        assert not Path(project["path"]).exists()
        assert HIDDEN not in json.dumps(calls)
        assert {t["function"]["name"] for t in calls[0]["tools"]} == {"finish"}
        directory = run_directory(service.settings, run_id)
        for name, content in files.items():
            actual = (directory / "workspace" / name).read_bytes()
            assert (
                actual.startswith(content.data)
                if name == "SKILL.md"
                else actual == content.data
            )
            assert (
                directory / "workspace" / name
            ).stat().st_mode & 0o777 == content.mode
        assert (directory / "workspace" / "empty").is_dir()
        adoption = client.post(f"/api/runs/{run_id}/adoption-check")
        assert adoption.status_code == 200 and not adoption.json()["safe"]
        preview = client.post(
            path + "/preview",
            json={"expected_revision": client.get(path).json()["revision"]},
        )
        assert preview.status_code == 200, preview.text
        imported = client.post(
            path + "/import", json=import_body(preview.json()).model_dump()
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["skill"]["review_status"] == "pending"
        assert len(service.store.list_runs()) == 1


@pytest.mark.parametrize(
    "point",
    [
        "before_workspace",
        "baseline_only",
        "before_output",
        "after_output",
        "after_candidate",
        "after_final",
    ],
)
def test_generation_crash_recovers_original_attempt_without_replay(
    setup_generation, monkeypatch, point
):
    service, body, _files, _taskset, _project = setup_generation
    calls = transport(monkeypatch)
    _draft, run = enqueue(service, body)
    name = {
        "before_workspace": "prepare_workspace",
        "baseline_only": "_publish_tree",
        "before_output": "save_output",
        "after_output": "save_output",
        "after_candidate": "build_candidate",
        "after_final": "finalize_candidate",
    }[point]
    original = getattr(Generation, name)
    crashed = False

    def interrupt(self, *args, **kwargs):
        nonlocal crashed
        if not crashed and point in {"before_workspace", "before_output"}:
            crashed = True
            raise KeyboardInterrupt
        result = original(self, *args, **kwargs)
        if not crashed:
            if point == "baseline_only" and args[1].name != "baseline":
                return result
            crashed = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Generation, name, interrupt)
    Worker(service.settings).run(once=True)
    first = service.store.get(run["id"])
    assert crashed and first["state"] == "interrupted", first["reason"]
    assert first["usage"]["calls"] == (
        0 if point in {"before_workspace", "baseline_only"} else 1
    )
    directory = run_directory(service.settings, run["id"])
    final_bytes = (
        (directory / "generation-final" / "report.json").read_bytes()
        if point == "after_final"
        else None
    )
    resume(service, run["id"])
    Worker(service.settings).run(once=True)
    final = service.store.get(run["id"])
    assert final["state"] == "completed_unverified", final["reason"]
    assert final["first_started_at"] == first["first_started_at"]
    assert final["usage"]["calls"] == len(calls) == 1
    assert final["assignments"]["executor"]["session_generation"] == 0
    if final_bytes is not None:
        assert (
            directory / "generation-final" / "report.json"
        ).read_bytes() == final_bytes
        assert final["evidence"] == first["evidence"]
        assert not list(directory.glob("evidence-attempt-*"))


@pytest.mark.parametrize(
    "output,tool", [("not json", "finish"), (OUTPUT, "doonex_shell")]
)
def test_generation_invalid_response_never_retries(
    setup_generation, monkeypatch, output, tool
):
    service, body, _files, _taskset, _project = setup_generation
    calls = transport(monkeypatch, output, tool)
    _draft, run = enqueue(service, body)
    Worker(service.settings).run(once=True)
    final = service.store.get(run["id"])
    assert final["state"] == "failed", final["reason"]
    assert final["usage"]["calls"] == len(calls) == 1
    assert not final.get("evidence")
    if tool == "finish":
        assert final["generation_candidate"]["valid"] is False
        assert (
            run_directory(service.settings, run["id"])
            / "generation-output"
            / "output.json"
        ).read_text() == output


def test_generation_recovery_rejects_mutated_asset(setup_generation, monkeypatch):
    service, body, _files, _taskset, _project = setup_generation
    calls = transport(monkeypatch)
    _draft, run = enqueue(service, body)
    original = Generation.save_output

    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(Generation, "save_output", interrupt)
    Worker(service.settings).run(once=True)
    directory = run_directory(service.settings, run["id"])
    (directory / "workspace" / "assets" / "blob.bin").write_bytes(b"tampered")
    with TestClient(create_app(service.settings)) as client:
        client.headers["Authorization"] = "Bearer " + service.settings.token
        report = client.get(f"/api/runs/{run['id']}/recovery")
        assert report.status_code == 200 and not report.json()["workspace_ok"]
        resumed = client.post(
            f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": True}
        )
        assert resumed.status_code == 400
    assert service.store.get(run["id"])["state"] == "interrupted"
    assert len(calls) == 1


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_generation_stop_during_model_settles_usage_without_publishing(
    setup_generation, monkeypatch, action
):
    service, body, _files, _taskset, _project = setup_generation
    _draft, run = enqueue(service, body)
    calls = transport(monkeypatch)
    original = GuardedLLM._transport_call

    def stop_then_return(*args, **kwargs):
        result = original(*args, **kwargs)
        with service.store.locked(run["id"]) as (_c, current):
            current.update(state="pausing", requested_action=action)
        return result

    monkeypatch.setattr(GuardedLLM, "_transport_call", stop_then_return)
    Worker(service.settings).run(once=True)
    final = service.store.get(run["id"])
    assert final["state"] == ("paused" if action == "pause" else "cancelled")
    assert len(calls) == final["usage"]["calls"] == 1
    assert final["usage"]["tokens"] == 120
    assert final["usage"]["reserved_tokens"] == 0
    assert not final.get("generation_output")
    assert not final.get("generation_candidate")


def test_generation_unknown_call_resume_preserves_reservation(
    setup_generation, monkeypatch
):
    service, body, _files, _taskset, _project = setup_generation
    _draft, run = enqueue(service, body)
    calls = transport(monkeypatch)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(GuardedLLM, "_transport_call", interrupted)
    Worker(service.settings).run(once=True)
    first = service.store.get(run["id"])
    assert first["state"] == "interrupted"
    assert first["usage"]["calls"] == first["usage"]["unknown_calls"] == 1
    assert first["usage"]["reserved_tokens"] > 0
    resumed = resume(service, run["id"])
    assert resumed["usage"] == first["usage"]
    assert resumed["assignments"]["executor"]["session_generation"] == 1
    assert resumed["assignments"]["executor"]["resume_session"] is False
    transport(monkeypatch)
    Worker(service.settings).run(once=True)
    final = service.store.get(run["id"])
    assert final["state"] == "completed_unverified", final["reason"]
    assert final["usage"]["calls"] == 2
    assert final["usage"]["unknown_calls"] == 1
    assert final["usage"]["reserved_tokens"] == first["usage"]["reserved_tokens"]
    assert final["first_started_at"] == first["first_started_at"]
    assert calls == []


def test_saved_finish_tampering_cannot_replace_original_model_output(
    setup_generation, monkeypatch
):
    service, body, _files, _taskset, _project = setup_generation
    calls = transport(monkeypatch)
    _draft, run = enqueue(service, body)
    original = Generation.save_output

    def interrupt_before_output(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Generation, "save_output", interrupt_before_output)
    Worker(service.settings).run(once=True)
    first = service.store.get(run["id"])
    assert first["state"] == "interrupted" and len(calls) == 1
    directory = run_directory(service.settings, run["id"])
    changed = 0
    for path in (directory / "session-executor-0").rglob("*.json"):
        value = json.loads(path.read_text())
        if value.get("kind") == "ActionEvent" and value.get("tool_name") == "finish":
            value["action"]["message"] = '{"append_text":"FORGED FINISH"}'
            path.write_text(json.dumps(value))
            changed += 1
    assert changed == 1
    monkeypatch.setattr(Generation, "save_output", original)
    resume(service, run["id"])
    Worker(service.settings).run(once=True)
    final = service.store.get(run["id"])
    assert final["state"] == "failed"
    assert not final.get("generation_output")
    assert not final.get("generation_candidate")
    assert len(calls) == final["usage"]["calls"] == 1
