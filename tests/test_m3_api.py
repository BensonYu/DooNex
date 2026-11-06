"""Private M3 API/database integration with controlled local fixtures only.

No worker, provider, live model or persistent database is started by this module.
Run through scripts/test_local.py to provision a disposable PostgreSQL schema.
"""

import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from doonex.api import create_app
from doonex.arena import Arena
from doonex.library import Library
from doonex.model_registry import ModelRegistry
from doonex.sandbox import Sandbox
from doonex.store import now
from doonex.worker import run_directory
from doonex.workspace import Workspace
from fastapi.testclient import TestClient


@pytest.fixture
def m3_client(settings, monkeypatch):
    # Root may be integrating independent files while these tests run. Each
    # test freezes the actual environment receipt once, then separately tests
    # drift rejection rather than racing edits to controller source files.
    from doonex import arena

    frozen = arena.environment()
    monkeypatch.setattr(arena, "environment", lambda: copy.deepcopy(frozen))
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        yield client


@pytest.fixture
def m3_sources(tmp_path):
    repository = tmp_path / "controlled-repository"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repository), *args], text=True
        ).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "M3 API fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repository / "main.py").write_text("answer = 1\n")
    git("add", ".")
    git("commit", "-qm", "controlled initial source")
    commit = git("rev-parse", "HEAD")
    verifier = tmp_path / "controlled-verifier"
    verifier.mkdir()
    (verifier / "verify.py").write_text("assert True\n")
    suite = tmp_path / "controlled-taskset"
    suite.mkdir()
    description = {
        "name": "M3 API controlled tasks",
        "category": "python",
        "split": "heldout",
        "tasks": [
            {
                "id": f"task-{index}",
                "title": f"Controlled task {index}",
                "prompt": "Fix the controlled fixture.",
                "repo_path": str(repository),
                "base_ref": commit,
                "visible_checks": ["python main.py"],
                "verifier_path": str(verifier),
                "verifier_command": "python /verifier/verify.py",
                "critical": index == 0,
            }
            for index in range(5)
        ],
    }
    (suite / "taskset.json").write_text(json.dumps(description))
    sources = []
    for index in range(3):
        source = tmp_path / f"controlled-skill-{index}"
        source.mkdir()
        (source / "SKILL.md").write_text(
            f"---\nname: fixture-{index}\n---\nControlled instruction {index}.\n"
        )
        sources.append(source)
    return {
        "repository": repository,
        "git": git,
        "commit": commit,
        "suite": suite,
        "verifier": verifier,
        "description": description,
        "skills": sources,
    }


def import_skill(client, source, approved=True):
    response = client.post(
        "/api/skills/import",
        json={
            "path": str(source),
            "metadata": {
                "source": "controlled local API fixture",
                "license_status": "declared",
                "license_text": "Synthetic private fixture authorization",
                "scope": ["python"],
                "permissions": ["workspace.read"],
            },
        },
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["review_status"] == "pending"
    if approved:
        response = client.post(
            f"/api/skills/{record['version_id']}/review",
            json={"status": "approved", "note": "Controlled fixture review"},
        )
        assert response.status_code == 200, response.text
        record = response.json()
    return record


@pytest.fixture
def m3_library(m3_client, m3_sources):
    skills = [import_skill(m3_client, source) for source in m3_sources["skills"]]
    response = m3_client.post(
        "/api/arena/tasksets/import", json={"path": str(m3_sources["suite"])}
    )
    assert response.status_code == 200, response.text
    return {"skills": skills, "taskset": response.json()}


def evaluation_body(library, *, repeats=1):
    return {
        "taskset_id": library["taskset"]["id"],
        "skill_version_ids": [item["version_id"] for item in library["skills"][:2]],
        "model_profile_id": "legacy",
        "repeats": repeats,
        "run_budget": {
            "max_calls": 2,
            "max_tokens": 100000,
            "max_cost_usd": 1,
            "max_seconds": 600,
        },
        "batch_budget": {
            "max_calls": 200,
            "max_tokens": 1000000,
            "max_cost_usd": 10,
            "max_seconds": 14400,
        },
    }


def create_evaluation(client, library, *, repeats=1, **updates):
    body = evaluation_body(library, repeats=repeats)
    body.update(updates)
    response = client.post("/api/arena/evaluations", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_skill_api_requires_review_and_new_runs_freeze_original_version(
    m3_client, settings, store, m3_sources
):
    pending = import_skill(m3_client, m3_sources["skills"][0], approved=False)
    version_id = pending["version_id"]
    project = m3_client.post(
        "/api/projects", json={"path": str(m3_sources["repository"])}
    ).json()
    body = {
        "project_id": project["id"],
        "prompt": "Controlled task",
        "skill_version_id": version_id,
    }
    assert m3_client.post("/api/runs", json=body).status_code == 400
    assert store.list_runs() == []
    approved = m3_client.post(
        f"/api/skills/{version_id}/review",
        json={"status": "approved", "note": "Controlled approval"},
    ).json()
    response = m3_client.post("/api/runs", json=body)
    assert response.status_code == 200, response.text
    frozen = response.json()["request"]["skill"]
    assert frozen == {
        key: approved[key]
        for key in ("version_id", "content_digest", "report_sha256", "metadata")
    }
    rejected = m3_client.post(
        f"/api/skills/{version_id}/review",
        json={"status": "rejected", "note": "No longer approved for new runs"},
    ).json()
    assert len(rejected["review_history"]) == 2
    assert m3_client.post("/api/runs", json=body).status_code == 400
    assert store.get(response.json()["id"])["request"]["skill"] == frozen
    assert Library(settings, store).skill_context(frozen).version_id == version_id
    assert "Controlled instruction" in m3_client.get(f"/api/skills/{version_id}").json()["instructions"]
    baseline = m3_client.post("/api/runs", json={**body, "skill_version_id": None})
    assert baseline.status_code == 200 and baseline.json()["request"]["skill"] is None


@pytest.mark.parametrize("kind", ["skill", "taskset"])
def test_reimport_never_rebinds_original_database_receipt(
    m3_client, settings, store, m3_library, m3_sources, kind
):
    library = Library(settings, store)
    public = m3_library["skills"][0] if kind == "skill" else m3_library["taskset"]
    identifier = public["version_id"] if kind == "skill" else public["id"]
    original = library.get(kind, identifier)
    report_path = Path(original["context_path"]).parent / "report.json"
    contents = json.loads(report_path.read_text())
    # These fields are deliberately excluded from content identity, making
    # the database's first report hash essential to detecting a rewrite.
    if kind == "skill":
        contents["created_at"] = "2020-01-01T00:00:00+00:00"
    else:
        contents["provenance"]["task-0"]["requested_ref"] = "different receipt"
    report_path.write_text(json.dumps(contents))
    source = m3_sources["skills"][0] if kind == "skill" else m3_sources["suite"]
    endpoint = "/api/skills/import" if kind == "skill" else "/api/arena/tasksets/import"
    body = {"path": str(source)}
    if kind == "skill":
        body["metadata"] = original["metadata"]
    response = m3_client.post(endpoint, json=body)
    assert response.status_code == 400, response.text
    assert library.get(kind, identifier)["report_sha256"] == original["report_sha256"]
    assert library.get(kind, identifier) == original
    if kind == "skill":
        assert not m3_client.get(f"/api/skills/{identifier}").json()["integrity"]["valid"]
        assert m3_client.post(
            f"/api/skills/{identifier}/review",
            json={"status": "approved", "note": "Cannot reapprove changed receipt"},
        ).status_code == 400
    else:
        item = m3_client.get("/api/arena/tasksets").json()["tasksets"][0]
        assert not item["valid"]


@pytest.mark.parametrize("skill_count", [2, 3])
def test_evaluation_freezes_model_budgets_skill_receipts_and_interleaves_plan(
    m3_client, settings, store, m3_library, skill_count
):
    registry = ModelRegistry(settings)
    profile = registry.save(
        "controlled",
        "Controlled profile",
        settings.model,
    )
    body = evaluation_body(m3_library, repeats=3)
    body["skill_version_ids"] = [item["version_id"] for item in m3_library["skills"][:skill_count]]
    body["model_profile_id"] = profile.id
    response = m3_client.post("/api/arena/evaluations", json=body)
    assert response.status_code == 200, response.text
    value = response.json()
    expected_count = 5 * 3 * (skill_count + 1)
    assert value["state"] == "queued" and len(value["attempts"]) == expected_count
    assert value["metrics"]["counts"]["planned"] == expected_count
    assert value["run_budget"] == body["run_budget"]
    assert value["batch_budget"] == body["batch_budget"]
    assert value["repeats"] == 3
    arms = [arm["id"] for arm in value["arms"]]
    assert arms == ["baseline", *body["skill_version_ids"]]
    actual = [
        (item["repeat"], item["task_id"], item["arm_id"])
        for item in value["attempts"]
    ]
    expected = []
    for repeat in range(3):
        for index, task in enumerate(value["tasks"]):
            offset = (repeat + index) % len(arms)
            expected.extend(
                (repeat, task["id"], arm) for arm in arms[offset:] + arms[:offset]
            )
    assert actual == expected
    revised = settings.model.model_copy(update={"model_id": "offline/changed-model"})
    registry.save(
        profile.id,
        "Changed",
        revised,
        expected_revision=profile.revision,
    )
    for attempt in value["attempts"]:
        request = store.get(attempt["run_id"])["request"]
        assert request["model"] == value["model"]
        assert request["model_profiles"]["executor"]["revision"] == profile.revision
        assert request["budget"] == body["run_budget"]
        assert request["workflow"] == "execute"
        assert request["arena"]["fingerprint"] == value["fingerprint"]
        assert request["checks"] == ["python /verifier/verify.py"]
        assert request["visible_checks"] == ["python main.py"]
        assert request["skill"] == value["skills"].get(attempt["arm_id"])
    latest = m3_client.get(f"/api/arena/evaluations/{value['id']}").json()
    assert latest["model"]["model_id"] == settings.model.model_id
    assert latest["metrics"]["recommendations"] == {"reliable": [], "cost": [], "speed": []}


@pytest.mark.parametrize(
    "change",
    ["one_candidate", "four_candidates", "duplicate", "zero_repeats", "many_repeats", "run_budget", "batch_budget", "extra"],
)
def test_invalid_evaluations_reject_without_partial_runs(
    m3_client, store, m3_library, change
):
    body = evaluation_body(m3_library)
    if change == "one_candidate":
        body["skill_version_ids"] = body["skill_version_ids"][:1]
    elif change == "four_candidates":
        body["skill_version_ids"] *= 2
    elif change == "duplicate":
        body["skill_version_ids"] = [body["skill_version_ids"][0]] * 2
    elif change == "zero_repeats":
        body["repeats"] = 0
    elif change == "many_repeats":
        body["repeats"] = 11
    elif change == "run_budget":
        body["run_budget"]["max_calls"] = 101
    elif change == "batch_budget":
        body["batch_budget"]["max_calls"] = 10001
    else:
        body["automatic_publish"] = True
    response = m3_client.post("/api/arena/evaluations", json=body)
    assert response.status_code == 422, response.text
    assert store.list_runs() == []
    assert m3_client.get("/api/arena/evaluations").json()["evaluations"] == []


def test_unapproved_unknown_and_tampered_candidates_never_dispatch(
    m3_client, settings, store, m3_library, monkeypatch
):
    body = evaluation_body(m3_library)
    version = body["skill_version_ids"][0]
    m3_client.post(f"/api/skills/{version}/review", json={"status": "rejected"})
    assert m3_client.post("/api/arena/evaluations", json=body).status_code == 400
    body["skill_version_ids"][0] = "f" * 64
    assert m3_client.post("/api/arena/evaluations", json=body).status_code == 404
    body = evaluation_body(m3_library)
    m3_client.post(f"/api/skills/{version}/review", json={"status": "approved"})
    binding = Library(settings, store).get("skill", version)
    (Path(binding["context_path"]) / "SKILL.md").write_text("Changed after approval\n")
    assert m3_client.post("/api/arena/evaluations", json=body).status_code == 400
    assert store.list_runs() == []
    assert m3_client.get("/api/arena/evaluations").json()["evaluations"] == []


def test_imported_snapshots_survive_changed_and_moved_original_sources(
    m3_client, settings, store, m3_library, m3_sources
):
    value = create_evaluation(m3_client, m3_library)
    library = Library(settings, store)
    for source in m3_sources["skills"]:
        (source / "SKILL.md").write_text("Changed unimported original\n")
    (m3_sources["verifier"] / "verify.py").write_text("raise RuntimeError('changed original')\n")
    m3_sources["repository"].rename(m3_sources["repository"].with_name("moved-original"))
    for attempt in value["attempts"]:
        binding = store.get(attempt["run_id"])["request"]["arena"]
        context = library.tasksets.context(
            binding["taskset_id"], binding["report_sha256"], binding["task_id"]
        )
        assert (context.source_path / "main.py").read_text() == "answer = 1\n"
        assert (context.verifier_path / "verify.py").read_text() == "assert True\n"
    for binding in value["skills"].values():
        assert "Controlled instruction" in library.skill_context(binding).instructions
    detail = m3_client.get(f"/api/arena/evaluations/{value['id']}").json()
    assert detail["integrity"]["valid"]


def test_environment_drift_interrupts_before_dispatch_and_blocks_resume(
    m3_client, settings, store, m3_library, monkeypatch
):
    from doonex import arena as arena_module

    value = create_evaluation(m3_client, m3_library)
    changed = {**value["environment"], "synthetic_controller_change": True}
    monkeypatch.setattr(arena_module, "environment", lambda: changed)
    arena = Arena(settings, store)
    assert not arena.start(value["id"])
    detail = m3_client.get(f"/api/arena/evaluations/{value['id']}").json()
    assert detail["state"] == "interrupted" and not detail["integrity"]["valid"]
    assert all(store.get(item["run_id"])["state"] == "queued" for item in value["attempts"])
    assert all(store.get(item["run_id"])["usage"]["calls"] == 0 for item in value["attempts"])
    assert detail["metrics"]["recommendations"] == {"reliable": [], "cost": [], "speed": []}
    response = m3_client.post(
        f"/api/arena/evaluations/{value['id']}/resume", json={"acknowledge_unknown": True}
    )
    assert response.status_code == 400 and "环境" in response.json()["detail"]


def test_queued_batch_cancel_preserves_usage_and_cannot_promote_or_resume(
    m3_client, settings, store, m3_library
):
    value = create_evaluation(m3_client, m3_library)
    first = value["attempts"][0]
    with store.locked(first["run_id"]) as (_, run):
        run["usage"].update(calls=1, tokens=20, reserved_tokens=123, unknown_calls=1)
    response = m3_client.post(f"/api/arena/evaluations/{value['id']}/cancel")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == "cancelled"
    assert all(store.get(item["run_id"])["state"] == "cancelled" for item in value["attempts"])
    preserved = store.get(first["run_id"])["usage"]
    assert preserved["calls"] == 1 and preserved["reserved_tokens"] == 123
    assert result["metrics"]["counts"]["cancelled"] == 15
    assert not result["metrics"]["complete"]
    assert m3_client.post(
        "/api/arena/recommendations",
        json={"evaluation_id": value["id"], "position": "reliable", "arm_id": value["skill_version_ids"][0]},
    ).status_code == 400
    assert m3_client.post(
        f"/api/arena/evaluations/{value['id']}/resume", json={"acknowledge_unknown": True}
    ).status_code == 400
    assert m3_client.get("/api/arena/recommendations").json() == {"history": [], "current": []}
    assert Arena(settings, store).get(value["id"])["state"] == "cancelled"


def test_running_batch_cancel_waits_for_inflight_child_and_retains_records(
    m3_client, settings, store, m3_library
):
    value = create_evaluation(m3_client, m3_library)
    arena = Arena(settings, store)
    assert arena.start(value["id"])
    first = value["attempts"][0]
    with store.locked(first["run_id"]) as (_, run):
        run["state"] = "running"
        run["first_started_at"] = now()
    response = m3_client.post(f"/api/arena/evaluations/{value['id']}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cancelling"
    assert store.get(first["run_id"])["requested_action"] == "cancel"
    with store.locked(first["run_id"]) as (_, run):
        run.update(state="cancelled", ended_at=now(), arena_outcome={"classification": "cancelled", "recorded_at": now()})
    arena.sync(value["id"])
    detail = m3_client.get(f"/api/arena/evaluations/{value['id']}").json()
    assert detail["state"] == "cancelled" and len(detail["attempts"]) == 15


def test_interrupted_batch_resume_requires_acknowledgement_and_keeps_clocks(
    m3_client, settings, store, m3_library, m3_sources, monkeypatch
):
    value = create_evaluation(m3_client, m3_library)
    arena = Arena(settings, store)
    arena.start(value["id"])
    first = value["attempts"][0]
    directory = run_directory(settings, first["run_id"])
    directory.mkdir(parents=True)
    workspace = Workspace.create(
        m3_sources["repository"], m3_sources["commit"], directory / "workspace", baseline_path=directory / "baseline"
    )
    started = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    with store.locked(first["run_id"]) as (_, run):
        run.update(state="paused", baseline_digest=workspace.baseline_digest, first_started_at=started)
        run["usage"].update(calls=1, reserved_tokens=200, unknown_calls=1)
        run["assignments"]["executor"]["state"] = "paused"
    arena.sync(value["id"])
    original = arena.get(value["id"])
    assert original["state"] == "interrupted"
    endpoint = f"/api/arena/evaluations/{value['id']}/resume"
    assert m3_client.post(endpoint, json={}).status_code == 400
    assert m3_client.post(endpoint, json={"acknowledge_unknown": "yes"}).status_code == 400
    child_resume = m3_client.post(
        f"/api/runs/{first['run_id']}/resume?project_id={first['project_id']}", json={"acknowledge_unknown": True}
    )
    assert child_resume.status_code == 400 and "Arena" in child_resume.json()["detail"]
    monkeypatch.setattr(Sandbox, "containers", lambda *_: ["synthetic still-active container"])
    assert m3_client.post(endpoint, json={"acknowledge_unknown": True}).status_code == 400
    assert store.get(first["run_id"])["state"] == "paused"
    monkeypatch.setattr(Sandbox, "containers", lambda *_: [])
    response = m3_client.post(endpoint, json={"acknowledge_unknown": True})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "running"
    assert response.json()["first_started_at"] == original["first_started_at"]
    resumed = store.get(first["run_id"])
    assert resumed["state"] == "queued" and resumed["first_started_at"] == started
    assert resumed["usage"]["calls"] == 1 and resumed["usage"]["reserved_tokens"] == 200
    assert resumed["assignments"]["executor"]["session_generation"] == 1
    assert resumed["assignments"]["executor"]["resume_session"] is False


def test_queued_and_incomplete_results_cannot_create_recommendation(
    m3_client, settings, store, m3_library
):
    value = create_evaluation(m3_client, m3_library, repeats=3)
    body = {"evaluation_id": value["id"], "position": "reliable", "arm_id": value["skill_version_ids"][0]}
    assert m3_client.post("/api/arena/recommendations", json=body).status_code == 400
    with Arena(settings, store).locked(value["id"]) as (_, current):
        current["state"] = "complete"
    detail = m3_client.get(f"/api/arena/evaluations/{value['id']}").json()
    assert detail["state"] == "complete" and not detail["metrics"]["complete"]
    assert detail["metrics"]["counts"]["pending"] == 45
    assert m3_client.post("/api/arena/recommendations", json=body).status_code == 400
    assert m3_client.get("/api/arena/recommendations").json() == {"history": [], "current": []}
