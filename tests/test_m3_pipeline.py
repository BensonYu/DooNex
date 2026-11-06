"""The private Arena uses the actual SDK and Docker with a fake model transport."""

import json

import pytest
from doonex.api import create_app
from doonex.arena import Arena, EvaluationInput
from doonex.config import Settings
from doonex.library import Library
from doonex.sdk import GuardedLLM
from doonex.worker import Worker, run_directory
from fastapi.testclient import TestClient
from test_api import repository
from test_worker import response


def prepare(settings, store, tmp_path):
    library = Library(settings, store)
    candidates = []
    for index in range(2):
        candidate = tmp_path / f"candidate-{index}"
        candidate.mkdir()
        (candidate / "SKILL.md").write_text(
            f"Inspect the requested change and verify it. Candidate {index}."
        )
        (candidate / "checklist.md").write_text("Use only authorized workspace files.")
        record = library.import_skill(candidate, {})
        library.review(record["version_id"], "approved", "Synthetic test fixture")
        candidates.append(record["version_id"])
    project, _ = repository(tmp_path)
    verifier = tmp_path / "checker"
    verifier.mkdir()
    (verifier / "check.py").write_text(
        "import sys\nsys.path.insert(0,'/workspace')\nfrom hello import answer\nassert answer == 42, 'HIDDEN_TEST_SENTINEL'\n"
    )
    suite = tmp_path / "suite"
    suite.mkdir()
    spec = {
        "name": "Synthetic tests",
        "category": "python",
        "split": "heldout",
        "minimum_pass_rate": 0.8,
        "tasks": [
            {
                "id": f"task-{i}",
                "title": f"Task {i}",
                "prompt": "Set hello.answer to 42 using a minimal fix.",
                "repo_path": str(project),
                "base_ref": "HEAD",
                "visible_checks": ["python -B hello.py"],
                "verifier_path": str(verifier),
                "verifier_command": "python -B /verifier/check.py",
                "critical": False,
            }
            for i in range(5)
        ],
    }
    (suite / "taskset.json").write_text(json.dumps(spec))
    taskset = library.import_taskset(suite)
    body = EvaluationInput(
        taskset_id=taskset["id"],
        skill_version_ids=candidates,
        model_profile_id="legacy",
        repeats=1,
        run_budget={
            "max_calls": 3,
            "max_tokens": 100000,
            "max_cost_usd": 2,
            "max_seconds": 600,
        },
        batch_budget={
            "max_calls": 60,
            "max_tokens": 1000000,
            "max_cost_usd": 10,
            "max_seconds": 7200,
        },
    )
    arena = Arena(settings, store)
    return arena, arena.create(body), library


@pytest.mark.docker
def test_arena_sdk_containers_frozen_skill_hidden_verifier_and_shared_usage(
    settings, store, tmp_path, monkeypatch
):
    model = settings.model.model_copy(update={"model_id": "gpt-4.1-mini"})
    settings = Settings(settings.home, settings.database_url, settings.token, model)
    arena, batch, library = prepare(settings, store, tmp_path)
    calls = []
    command = "test ! -e /workspace/SKILL.md && printf 'answer = 42\\n' > hello.py"
    replies = [
        response("doonex_shell", {"command": command}, i * 2)
        if j == 0
        else response("finish", {"message": "Done"}, i * 2 + 1)
        for i in range(3)
        for j in range(2)
    ]

    def transport(*args, **kwargs):
        payload = json.dumps(kwargs, default=str)
        assert "HIDDEN_TEST_SENTINEL" not in payload
        assert "python -B /verifier/check.py" not in payload
        assert "python -B hello.py" in payload
        calls.append(payload)
        return replies.pop(0)

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    # Freeze at creation: subsequent review status blocks new runs, not these.
    library.review(
        batch["skill_version_ids"][0], "rejected", "Changed decision after creation"
    )
    for _ in range(3):
        Worker(settings).run(once=True)
    final = arena.detail(batch["id"])
    done = [a for a in final["attempts"] if a["state"] == "completed_verified"]
    assert len(done) == 3, [(a["state"], a["reason"]) for a in final["attempts"]]
    assert {a["arm_id"] for a in done} == {"baseline", *batch["skill_version_ids"]}
    assert len(calls) == 6 and not replies
    assert final["usage"]["calls"] == 6
    assert (
        final["usage"]["tokens"]
        == sum(a["usage"]["tokens"] for a in final["attempts"])
        == 720
    )
    assert final["usage"]["reserved_tokens"] == 0
    assert all(a["evidence_valid"] for a in done)
    assert any("Candidate 0." in payload for payload in calls)
    assert any("Candidate 1." in payload for payload in calls)
    assert not final["metrics"]["recommendations"]["reliable"]
    # The hidden checker is present in the report only after the model stopped,
    # and remains outside task artifacts. An original tree edit invalidates it.
    for attempt in done:
        run = store.get(attempt["run_id"])
        evidence = run["evidence"]
        assert evidence["verification_context"]["taskset_id"] == batch["taskset_id"]
        assert "verifier" not in {
            p.name for p in (run_directory(settings, run["id"]) / "workspace").iterdir()
        }
    arena.stop(batch["id"], "cancelling", "Test cancels remaining attempts")
    stopped = arena.detail(batch["id"])
    assert stopped["state"] == "cancelled"
    assert stopped["usage"] == final["usage"]
    assert stopped["metrics"]["counts"]["cancelled"] == 12
    assert all(not v for v in stopped["metrics"]["recommendations"].values())


@pytest.mark.docker
def test_hidden_check_failure_has_valid_failure_evidence(
    settings, store, tmp_path, monkeypatch
):
    model = settings.model.model_copy(update={"model_id": "gpt-4.1-mini"})
    settings = Settings(settings.home, settings.database_url, settings.token, model)
    arena, batch, _ = prepare(settings, store, tmp_path)
    monkeypatch.setattr(
        GuardedLLM,
        "_transport_call",
        lambda *a, **k: response("finish", {"message": "Claimed success"}, 0),
    )
    Worker(settings).run(once=True)
    result = arena.detail(batch["id"])
    attempt = result["attempts"][0]
    assert attempt["state"] == "failed"
    assert attempt["classification"] == "failed" and attempt["evidence_valid"]
    assert attempt["regression_failed"]
    assert result["metrics"]["counts"]["valid"] == 1
    assert result["metrics"]["counts"]["passed"] == 0
    assert result["usage"]["calls"] == 1
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        selected = client.post(
            "/api/arena/recommendations",
            json={
                "evaluation_id": batch["id"],
                "position": "reliable",
                "arm_id": batch["skill_version_ids"][0],
            },
        )
        assert selected.status_code == 400
        assert client.get("/api/arena/recommendations").json() == {
            "current": [],
            "history": [],
        }


@pytest.mark.docker
def test_explicit_checker_probe_is_recorded_and_disqualifies_version(
    settings, store, tmp_path, monkeypatch
):
    model = settings.model.model_copy(update={"model_id": "gpt-4.1-mini"})
    settings = Settings(settings.home, settings.database_url, settings.token, model)
    arena, batch, _ = prepare(settings, store, tmp_path)
    monkeypatch.setattr(
        GuardedLLM,
        "_transport_call",
        lambda *a, **k: response(
            "doonex_shell", {"command": "cat /verifier/check.py"}, 0
        ),
    )
    Worker(settings).run(once=True)
    attempt = arena.detail(batch["id"])["attempts"][0]
    assert attempt["state"] == "failed" and attempt["safety_violation"]
    assert attempt["classification"] == "failed"
    assert attempt["usage"]["calls"] == 1
    assert any(
        e["kind"] == "permission_denied"
        for e in store.events(attempt["run_id"])["events"]
    )
    assert not any(a["kind"] == "shell" for a in store.pending(attempt["run_id"]))


@pytest.mark.docker
@pytest.mark.parametrize(
    "provider_type,classification,valid",
    [
        ("APIConnectionError", "environment_failure", 0),
        ("BadRequestError", "failed", 1),
    ],
)
def test_failure_classification_requires_controller_error_evidence(
    settings, store, tmp_path, monkeypatch, provider_type, classification, valid
):
    model = settings.model.model_copy(update={"model_id": "gpt-4.1-mini"})
    settings = Settings(settings.home, settings.database_url, settings.token, model)
    arena, batch, _ = prepare(settings, store, tmp_path)
    error = type(provider_type, (RuntimeError,), {})

    def transport(*args, **kwargs):
        raise error("Synthetic raw provider failure must stay private")

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    Worker(settings).run(once=True)
    result = arena.detail(batch["id"])
    attempt = result["attempts"][0]
    assert attempt["classification"] == classification
    assert result["metrics"]["counts"]["valid"] == valid
    assert result["usage"]["unknown_calls"] == 1
    assert result["usage"]["reserved_tokens"] > 0
    assert "Synthetic raw provider failure" not in json.dumps(result)
