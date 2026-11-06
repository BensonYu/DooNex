"""Generation controller contracts; synthetic output and disposable PostgreSQL.

No model, HTTP provider, project checkout, upstream script, source, verifier or
container execution is involved in these tests.
"""

import copy
import io
import json
import stat
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from doonex.discovery import MIT_BODY
from doonex.evidence import Evidence
from doonex.generation import (
    GENERATION_SCHEMA,
    MAX_APPEND_BYTES,
    MAX_OUTPUT_BYTES,
    POLICY,
    SEPARATOR,
    Generation,
    GenerationError,
    GenerationInput,
    ImportInput,
    RevisionInput,
    StartInput,
    canonical,
    digest,
    install_generation_routes,
    parse_output,
)
from doonex.library import Library
from doonex.sandbox import Sandbox
from doonex.skills import prepare_bytes
from doonex.store import OwnershipLost, now
from doonex.tasksets import TaskSetRegistry
from doonex.workspace import FileContent, manifest, manifest_digest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

MIT = (
    "MIT License\n\nCopyright (c) 2026 Controlled Fixture Authors\n\n" + MIT_BODY + "\n"
).encode()
HIDDEN = "HIDDEN-GENERATION-ANSWER-SENTINEL-NOT-MODEL-INPUT"
OUTPUT = json.dumps(
    {
        "append_text": "Inspect existing conventions before editing.\nKeep the change focused."
    }
)


@pytest.fixture
def setup_generation(settings, store, tmp_path, monkeypatch):
    with store.connect() as c:
        c.execute(GENERATION_SCHEMA)
    library = Library(settings, store)
    files = {
        "SKILL.md": FileContent(
            b"---\nname: controlled\nlicense: LICENSE\n---\nRead carefully.\n", 0o644
        ),
        "LICENSE": FileContent(MIT, 0o644),
        "assets/blob.bin": FileContent(b"\x00\xfforiginal-binary", 0o644),
        "__pycache__/keep.pyc": FileContent(b"\xffcache-named-parent-asset", 0o644),
        "scripts/never.sh": FileContent(b"#!/bin/sh\nexit 99\n", 0o755),
    }
    directories = ("__pycache__", "assets", "empty", "scripts")
    record = library.skills.persist(
        prepare_bytes(
            files,
            directories,
            {
                "source": "synthetic-private-generation-parent",
                "license_status": "declared",
                "license_text": MIT.decode(),
                "scope": ["Synthetic tests"],
                "permissions": [],
            },
        )
    )
    parent = library.bind(
        "skill",
        record["version_id"],
        {**record, "review_status": "approved", "review_history": []},
    )
    project = store.set_project(
        {
            "id": str(uuid4()),
            "name": "Controlled context only",
            "path": str(tmp_path / "never-read-project"),
            "head": "0" * 40,
            "dirty": False,
        }
    )
    taskset_id = "1" * 64
    taskset = {
        "taskset_id": taskset_id,
        "name": "Visible synthetic tuning",
        "split": "tuning",
        "report_sha256": "2" * 64,
        "verifier_path": HIDDEN,
        "answers": HIDDEN,
        "source": HIDDEN,
        "old_evaluation": HIDDEN,
        "tasks": [
            {
                "id": "one",
                "title": "First task",
                "prompt": "Fix a small parsing error.",
                "visible_checks": ["Visible sample input passes"],
                "verifier_command": HIDDEN,
                "answer": HIDDEN,
                "source_digest": HIDDEN,
            },
            {
                "id": "two",
                "title": "Second task",
                "prompt": "Preserve documented behavior.",
                "visible_checks": [],
                "verifier_command": HIDDEN,
                "answer": HIDDEN,
            },
            {
                "id": "unselected",
                "title": HIDDEN,
                "prompt": HIDDEN,
                "visible_checks": [HIDDEN],
            },
        ],
    }
    library.bind("taskset", taskset_id, taskset)

    def forbidden(*args, **kwargs):
        raise AssertionError("TaskSet filesystem/context must never be read")

    for name in ("_load", "inspect", "context"):
        monkeypatch.setattr(TaskSetRegistry, name, forbidden)
    service = Generation(settings, store)
    body = GenerationInput(
        project_id=project["id"],
        parent_version_id=parent["version_id"],
        taskset_id=taskset_id,
        task_ids=["one", "two"],
        model_profile_id="legacy",
        budget={
            "max_calls": 2,
            "max_tokens": 100000,
            "max_cost_usd": 1,
            "max_seconds": 600,
        },
    )
    return service, body, files, taskset, project


def start_run(service, body):
    draft = service.create(body)
    start = StartInput(
        expected_revision=draft["revision"],
        confirm_model_usage=True,
        confirm_derivative_rights=True,
    )
    queued = service.start(draft["id"], start)
    with service.store.locked(queued["run_id"]) as (_c, run):
        run.update(
            state="running", owner="synthetic-generation-owner", first_started_at=now()
        )
    run = service.store.get(queued["run_id"])
    directory = Path(service.settings.home).absolute() / "runs" / run["id"]
    directory.mkdir(mode=0o700, parents=True)
    workspace = service.prepare_workspace(run, directory)
    with service.store.locked(run["id"]) as (_c, current):
        current["baseline_digest"] = workspace.baseline_digest
    return draft, service.store.get(run["id"]), directory, workspace, start


def bind_finish(service, run, directory, output=OUTPUT):
    binding = service.save_output(run, directory, output)
    with service.store.locked(run["id"]) as (_c, current):
        current["generation_output"] = binding
        current["assignments"]["executor"]["state"] = "completed"
    return service.store.get(run["id"])


def candidate_run(service, body):
    draft, run, directory, workspace, start = start_run(service, body)
    run = bind_finish(service, run, directory)
    candidate = service.build_candidate(run, directory)
    assert candidate["valid"]
    with service.store.locked(run["id"]) as (_c, current):
        current["generation_candidate"] = candidate
        current["state"] = "verifying"
    return draft, service.store.get(run["id"]), directory, workspace, start


def completed_run(service, body):
    draft, run, directory, workspace, start = candidate_run(service, body)
    evidence = Evidence.capture(workspace, directory, [], Sandbox(), 1, run["id"])
    final = service.finalize_candidate(run, directory, evidence)
    with service.store.locked(run["id"]) as (_c, current):
        current.update(
            state="completed_unverified", generation_candidate=final, evidence=evidence
        )
    return draft, service.store.get(run["id"]), directory, workspace, start


def import_body(preview):
    return ImportInput(
        expected_revision=preview["revision"],
        preview_sha256=preview["preview"]["sha256"],
        confirm_source=True,
        confirm_license=True,
    )


def test_draft_projection_never_reads_hidden_material(setup_generation):
    service, body, _files, _taskset, _project = setup_generation
    inputs = service.inputs()
    assert inputs["tasksets"][0]["tasks"] == [
        {"id": "one", "title": "First task"},
        {"id": "two", "title": "Second task"},
        {"id": "unselected", "title": HIDDEN},
    ]
    # The selector only exposes task titles; selected full model input excludes
    # all fields of the unselected task, all answers and every hidden path.
    draft = service.create(body)
    assert draft["state"] == "draft" and draft["run_id"] is None
    assert HIDDEN not in json.dumps(draft)
    assert set(draft["visible_tasks"][0]) == {"id", "title", "prompt", "visible_checks"}
    assert draft["projection_sha256"] == digest(draft["visible_tasks"])
    assert service.store.list_runs() == []
    assert not (service.settings.home / "runs").exists()


def test_heldout_rejected_before_any_projection_or_files(setup_generation):
    service, body, _files, _taskset, _project = setup_generation
    with service.store.connect() as c:
        c.execute(
            "UPDATE library_versions SET data=jsonb_set(data,'{split}','\"heldout\"') WHERE kind='taskset'"
        )
    assert service.inputs()["tasksets"] == []
    with pytest.raises(GenerationError, match="generation_requires_tuning"):
        service.create(body)
    assert service.list() == [] and service.store.list_runs() == []


def test_start_concurrency_consumes_one_exact_intent(setup_generation):
    service, body, _files, _taskset, _project = setup_generation
    draft = service.create(body)
    start = StartInput(
        expected_revision=draft["revision"],
        confirm_model_usage=True,
        confirm_derivative_rights=True,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.start(draft["id"], start), range(2)))
    assert results[0]["run_id"] == results[1]["run_id"]
    assert len(service.store.list_runs()) == 1
    run = service.store.get(results[0]["run_id"])
    request = run["request"]
    assert (
        request["purpose"] == "skill_generation"
        and request["execution_policy"] == POLICY
    )
    assert request["repo_path"] is request["base_commit"] is None
    assert request["checks"] == [] and request["skill"] is None
    assert not any(
        key in request for key in ("arena", "background", "recommendation_id")
    )
    with pytest.raises(GenerationError, match="generation_start_intent_conflict"):
        service.start(
            draft["id"], start.model_copy(update={"expected_revision": "f" * 64})
        )


def test_full_candidate_preserves_binary_modes_empty_and_cache_named_assets(
    setup_generation,
):
    service, body, parent, _taskset, _project = setup_generation
    draft, run, directory, workspace, _start = completed_run(service, body)
    assert workspace.repository is workspace.base_commit is None
    assert workspace.baseline_digest == manifest_digest(manifest(parent))
    assert (directory / "workspace/SKILL.md").read_bytes() == parent[
        "SKILL.md"
    ].data + SEPARATOR + json.loads(OUTPUT)["append_text"].encode()
    raw, _mime, _filename = service.artifact(draft["id"], "candidate")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        assert members["empty"].isdir() and members["empty"].mode == 0o700
        for name, content in parent.items():
            if name != "SKILL.md":
                assert archive.extractfile(name).read() == content.data
                assert members[name].mode == content.mode
        assert "__pycache__/keep.pyc" in members
    assert run["evidence"]["verified"] is False and run["evidence"]["commands"] == []
    preview = service.preview(
        draft["id"],
        RevisionInput(expected_revision=service.get(draft["id"])["revision"]),
    )
    assert preview["state"] == "preview_ready"
    assert preview["preview"]["structural_report"]["valid"] is True
    assert "+Inspect existing conventions" in preview["preview"]["diff"]
    result = service.import_skill(draft["id"], import_body(preview))
    assert result["skill"]["review_status"] == "pending"
    assert result["skill"]["metadata"]["source"] == "doonex-generation:" + draft["id"]
    assert result["skill"]["metadata"]["commit"] is None
    with pytest.raises(ValueError, match="只有明确审核批准"):
        service.library.freeze_skill(result["skill"]["version_id"])
    service.library.review(
        result["skill"]["version_id"], "approved", "Synthetic human review"
    )
    repeated = service.import_skill(draft["id"], import_body(preview))
    assert repeated["skill"]["review_status"] == "approved"
    assert len(repeated["skill"]["review_history"]) == 1
    assert (
        service.library.freeze_skill(result["skill"]["version_id"])["version_id"]
        == result["skill"]["version_id"]
    )
    assert len(service.store.list_runs()) == 1


@pytest.mark.parametrize(
    "message,code",
    [
        ("", "generation_output_invalid_json"),
        ("```json\n{}\n```", "generation_output_invalid_json"),
        ('{"append_text":"ok","extra":1}', "generation_output_fields"),
        ('{"append_text":"one","append_text":"two"}', "generation_output_invalid_json"),
        ('{"append_text":3}', "generation_append_empty"),
        ('{"append_text":"   "}', "generation_append_empty"),
        (
            json.dumps({"append_text": "x" * (MAX_APPEND_BYTES + 1)}),
            "generation_append_too_large",
        ),
        (json.dumps({"append_text": "a\x00b"}), "generation_append_control_character"),
        (
            json.dumps({"append_text": "---\nname: other\n---"}),
            "generation_append_frontmatter",
        ),
        (
            json.dumps({"append_text": "Copyright 2026 Other"}),
            "generation_append_rights_declaration",
        ),
        (
            json.dumps({"append_text": "License: MIT"}),
            "generation_append_rights_declaration",
        ),
        (
            json.dumps({"append_text": "Author: A"}),
            "generation_append_rights_declaration",
        ),
        (
            json.dumps({"append_text": "署名为其他人"}),
            "generation_append_rights_declaration",
        ),
        ('{"append_text":"\\ud800"}', "generation_append_invalid_utf8"),
    ],
)
def test_finish_contract_rejects_invalid_output(message, code):
    with pytest.raises(GenerationError, match=code):
        parse_output(message.encode())


def test_invalid_output_keeps_immutable_report_without_retry(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    draft, run, directory, _workspace, _start = start_run(service, body)
    run = bind_finish(service, run, directory, '{"append_text":"License: replacement"}')
    first = service.build_candidate(run, directory)
    assert first["valid"] is False
    assert first["errors"] == ["generation_append_rights_declaration"]
    assert service.build_candidate(run, directory) == first
    assert not (directory / "generation-candidate/context").exists()
    assert (directory / "workspace/SKILL.md").read_bytes() == (
        directory / "baseline/SKILL.md"
    ).read_bytes()
    with service.store.locked(run["id"]) as (_c, current):
        current.update(state="failed", generation_candidate=first)
    assert service.detail(draft["id"])["state"] == "candidate_invalid"
    with pytest.raises(GenerationError, match="generation_run_not_complete"):
        service.preview(
            draft["id"],
            RevisionInput(expected_revision=service.get(draft["id"])["revision"]),
        )


def test_published_finish_crash_window_reuses_exact_output(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    _draft, run, directory, _workspace, _start = start_run(service, body)
    first = service.save_output(run, directory, OUTPUT)
    service.validate_run(run, directory, require_workspace=True)
    assert service.save_output(run, directory, OUTPUT) == first
    with pytest.raises(GenerationError, match="generation_completed_output_changed"):
        service.save_output(run, directory, '{"append_text":"different"}')
    with pytest.raises(GenerationError, match="generation_output_not_bound"):
        service.build_candidate(run, directory)
    run = bind_finish(service, run, directory)
    assert service.build_candidate(run, directory)["valid"] is True


def test_candidate_publish_crash_can_resume_without_reconstructing_output(
    setup_generation, monkeypatch
):
    service, body, _parent, _taskset, _project = setup_generation
    _draft, run, directory, _workspace, _start = start_run(service, body)
    run = bind_finish(service, run, directory)
    activate = service._activate_candidate
    monkeypatch.setattr(
        service,
        "_activate_candidate",
        lambda *a: (_ for _ in ()).throw(RuntimeError("synthetic interruption")),
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        service.build_candidate(run, directory)
    original = (directory / "generation-candidate/report.json").read_bytes()
    monkeypatch.setattr(service, "_activate_candidate", activate)
    service.prepare_workspace(run, directory)
    candidate = service.build_candidate(run, directory)
    assert (
        candidate["valid"]
        and (directory / "generation-candidate/report.json").read_bytes() == original
    )


def test_final_publish_crash_reuses_same_evidence(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    _draft, run, directory, workspace, _start = candidate_run(service, body)
    evidence = Evidence.capture(workspace, directory, [], Sandbox(), 1, run["id"])
    first = service.finalize_candidate(run, directory, evidence)
    original = (directory / "generation-final/report.json").read_bytes()
    assert service.finalize_candidate(run, directory, evidence) == first
    assert (directory / "generation-final/report.json").read_bytes() == original
    with pytest.raises(GenerationError, match="generation_evidence_invalid"):
        service.finalize_candidate(
            run, directory, {**evidence, "report_sha256": "f" * 64}
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "baseline",
        "directory",
        "output",
        "report",
        "archive",
        "candidate",
        "empty",
        "final",
        "evidence",
    ],
)
def test_all_bound_artifact_tampering_blocks_import(setup_generation, mutation):
    service, body, _parent, _taskset, _project = setup_generation
    draft, run, directory, _workspace, _start = completed_run(service, body)
    preview = service.preview(
        draft["id"],
        RevisionInput(expected_revision=service.get(draft["id"])["revision"]),
    )
    targets = {
        "baseline": "baseline/assets/blob.bin",
        "output": "generation-output/output.json",
        "report": "generation-candidate/report.json",
        "archive": "generation-candidate/candidate.tar.gz",
        "candidate": "generation-candidate/context/__pycache__/keep.pyc",
        "final": "generation-final/report.json",
        "evidence": "evidence/report.json",
    }
    if mutation == "directory":
        (directory / "generation-candidate/context/assets").chmod(0o755)
    elif mutation == "empty":
        (directory / "generation-candidate/context/empty").rmdir()
    else:
        (directory / targets[mutation]).write_bytes(b"changed")
    with pytest.raises((ValueError, OSError, KeyError)):
        service.validate_run(run, directory, require_workspace=True)
    with pytest.raises((ValueError, OSError, KeyError)):
        service.import_skill(draft["id"], import_body(preview))
    assert len(service.library.list("skill")) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("purpose", "execute"),
        ("execution_policy", "shell"),
        ("checks", ["echo hidden"]),
        ("repo_path", "/private/other-project"),
        ("prompt", "altered"),
        ("skill", {}),
        ("background", {"id": "old-grant"}),
    ],
)
def test_run_request_binding_blocks_tampering(setup_generation, field, value):
    service, body, _parent, _taskset, _project = setup_generation
    _draft, run, directory, _workspace, _start = start_run(service, body)
    changed = copy.deepcopy(run)
    changed["request"][field] = value
    with pytest.raises(GenerationError):
        service.validate_run(changed, directory, require_workspace=True)


def test_stopped_or_reassigned_owner_cannot_publish(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    _draft, run, directory, _workspace, _start = start_run(service, body)
    with service.store.locked(run["id"]) as (_c, current):
        current["state"] = "pausing"
    with pytest.raises(GenerationError, match="generation_run_stopped_or_changed"):
        service.save_output(run, directory, OUTPUT)
    assert not (directory / "generation-output").exists()
    with service.store.locked(run["id"]) as (_c, current):
        current.update(state="running", owner="new-owner")
    with pytest.raises(OwnershipLost):
        service.save_output(run, directory, OUTPUT)
    assert not (directory / "generation-output").exists()


def test_parent_rejection_blocks_new_import_without_changing_frozen_run(
    setup_generation,
):
    service, body, _parent, _taskset, _project = setup_generation
    draft, run, directory, _workspace, _start = completed_run(service, body)
    service.library.review(body.parent_version_id, "rejected", "Synthetic withdrawal")
    service.validate_run(run, directory, require_workspace=True)
    with pytest.raises(GenerationError, match="generation_parent_not_approved"):
        service.preview(
            draft["id"],
            RevisionInput(expected_revision=service.get(draft["id"])["revision"]),
        )


def test_import_db_failure_preserves_orphan_and_reuses_original_report(
    setup_generation, monkeypatch
):
    service, body, _parent, _taskset, _project = setup_generation
    draft, _run, _directory, _workspace, _start = completed_run(service, body)
    preview = service.preview(
        draft["id"],
        RevisionInput(expected_revision=service.get(draft["id"])["revision"]),
    )
    bind = service.library.bind

    def failure(*args, **kwargs):
        bind(*args, **kwargs)
        raise RuntimeError("synthetic DB binding failure")

    monkeypatch.setattr(service.library, "bind", failure)
    with pytest.raises(RuntimeError, match="synthetic DB binding failure"):
        service.import_skill(draft["id"], import_body(preview))
    assert service.get(draft["id"])["imported"] is None
    assert len(service.library.list("skill")) == 1
    paths = sorted(service.library.skills.versions.iterdir())
    assert len(paths) == 2
    original = {p.name: (p / "report.json").read_bytes() for p in paths}
    monkeypatch.setattr(service.library, "bind", bind)
    result = service.import_skill(draft["id"], import_body(preview))
    assert result["skill"]["review_status"] == "pending"
    assert {p.name: (p / "report.json").read_bytes() for p in paths} == original


def test_strict_api_errors_do_not_echo_input_and_artifact_names_are_closed(
    setup_generation,
):
    service, body, _parent, _taskset, _project = setup_generation
    app = FastAPI()
    install_generation_routes(app, service.settings, service.store, [])
    with TestClient(app) as client:
        assert client.get("/api/skills/generations/inputs").status_code == 200
        bad = client.post(
            "/api/skills/generations", json={**body.model_dump(), "prompt": HIDDEN}
        )
        assert bad.status_code == 422
        created = client.post("/api/skills/generations", json=body.model_dump())
        assert created.status_code == 200
        draft = created.json()
        wrong = client.post(
            "/api/skills/generations/" + draft["id"] + "/start",
            json={
                "expected_revision": "f" * 64,
                "confirm_model_usage": True,
                "confirm_derivative_rights": True,
            },
        )
        assert (
            wrong.status_code == 409
            and wrong.json()["detail"]["code"] == "generation_revision_conflict"
        )
        assert HIDDEN not in wrong.text
        assert client.get("/api/skills/generations/not-a-uuid").status_code == 400
        assert (
            client.get(
                "/api/skills/generations/" + draft["id"] + "/artifact",
                params={"name": "../../credentials.json"},
            ).status_code
            == 404
        )


def test_explicit_confirmations_and_input_bounds_are_strict(setup_generation):
    _service, body, _parent, _taskset, _project = setup_generation
    for key in ("confirm_model_usage", "confirm_derivative_rights"):
        payload = {
            "expected_revision": "a" * 64,
            "confirm_model_usage": True,
            "confirm_derivative_rights": True,
            key: 1,
        }
        with pytest.raises(ValidationError):
            StartInput(**payload)
    with pytest.raises(ValidationError):
        GenerationInput(**{**body.model_dump(), "task_ids": ["one", "one"]})
    with pytest.raises(GenerationError, match="generation_output_too_large"):
        parse_output(b"x" * (MAX_OUTPUT_BYTES + 1))


def test_private_artifacts_have_fixed_permissions(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    _draft, _run, directory, _workspace, _start = completed_run(service, body)
    for name in ("generation-output", "generation-candidate", "generation-final"):
        root = directory / name
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        for path in root.iterdir():
            if path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
    report = json.loads((directory / "generation-candidate/report.json").read_bytes())
    assert (
        canonical(report)
        == (directory / "generation-candidate/report.json").read_bytes()
    )


def test_taskset_sql_projects_only_selected_visible_fields(
    setup_generation, monkeypatch
):
    service, body, _parent, _taskset, _project = setup_generation
    executed = []
    connect = service.store.connect

    class Connection:
        def __init__(self):
            self.connection = connect()

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, query, parameters=None):
            executed.append((query, parameters))
            return self.connection.execute(query, parameters)

    monkeypatch.setattr(service.store, "connect", Connection)
    report, selected = service._tasks(body.taskset_id, ["two"])
    assert report == "2" * 64 and [value["id"] for value in selected] == ["two"]
    assert len(executed) == 2
    assert "data->>'split' AS split" in executed[0][0]
    assert (
        "jsonb_build_object" in executed[1][0] and "t->>'id'=ANY(%s)" in executed[1][0]
    )
    assert executed[1][1] == (body.taskset_id, "2" * 64, ["two"])
    assert not any("SELECT data " in query for query, _params in executed)
    assert not any(
        word in executed[1][0]
        for word in ("answer", "source", "verifier", "evaluation")
    )


@pytest.mark.parametrize(
    "change,code",
    [
        (
            {
                "budget": {
                    "max_calls": 2,
                    "max_tokens": 100,
                    "max_cost_usd": 1,
                    "max_seconds": 600,
                }
            },
            "generation_budget_cannot_reserve_request",
        ),
        (
            {
                "budget": {
                    "max_calls": 2,
                    "max_tokens": 100000,
                    "max_cost_usd": 0.0001,
                    "max_seconds": 600,
                }
            },
            "generation_cost_budget_cannot_reserve_request",
        ),
        ({"task_ids": ["missing"]}, "generation_visible_task_not_found"),
    ],
)
def test_unusable_drafts_do_not_create_records_or_runs(setup_generation, change, code):
    service, body, _parent, _taskset, _project = setup_generation
    with pytest.raises(GenerationError, match=code):
        service.create(GenerationInput(**{**body.model_dump(), **change}))
    assert service.list() == [] and service.store.list_runs() == []


def test_draft_does_not_require_credentials_start_does(setup_generation, monkeypatch):
    service, body, _parent, _taskset, _project = setup_generation
    monkeypatch.delenv("DOONEX_LLM_API_KEY")
    draft = service.create(body)
    with pytest.raises(GenerationError, match="generation_credential_unavailable"):
        service.start(
            draft["id"],
            StartInput(
                expected_revision=draft["revision"],
                confirm_model_usage=True,
                confirm_derivative_rights=True,
            ),
        )
    assert (
        service.store.list_runs() == [] and service.get(draft["id"])["run_id"] is None
    )


def test_model_profile_changes_leave_frozen_input_and_budget_unchanged(
    setup_generation,
):
    service, body, _parent, _taskset, _project = setup_generation
    draft = service.create(body)
    original = service.settings.model
    object.__setattr__(
        service.settings,
        "model",
        original.model_copy(update={"model_id": "openai/other-config"}),
    )
    try:
        queued = service.start(
            draft["id"],
            StartInput(
                expected_revision=draft["revision"],
                confirm_model_usage=True,
                confirm_derivative_rights=True,
            ),
        )
    finally:
        object.__setattr__(service.settings, "model", original)
    request = service.store.get(queued["run_id"])["request"]
    assert request["model"] == draft["model"] and request["budget"] == draft["budget"]


def test_visible_projection_changes_block_start(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    draft = service.create(body)
    with service.store.connect() as c:
        c.execute(
            "UPDATE library_versions SET data=jsonb_set(data,'{tasks,0,prompt}','\"changed visible input\"') WHERE kind='taskset'"
        )
    with pytest.raises(GenerationError, match="generation_visible_projection_changed"):
        service.start(
            draft["id"],
            StartInput(
                expected_revision=draft["revision"],
                confirm_model_usage=True,
                confirm_derivative_rights=True,
            ),
        )
    assert service.store.list_runs() == []


def test_concurrent_import_publishes_one_pending_version(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    draft, _run, _directory, _workspace, _start = completed_run(service, body)
    preview = service.preview(
        draft["id"],
        RevisionInput(expected_revision=service.get(draft["id"])["revision"]),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service.import_skill(draft["id"], import_body(preview)),
                range(2),
            )
        )
    assert results[0]["skill"]["version_id"] == results[1]["skill"]["version_id"]
    assert (
        results[0]["skill"]["review_status"]
        == results[1]["skill"]["review_status"]
        == "pending"
    )
    assert (
        len(service.library.list("skill")) == 2 and len(service.store.list_runs()) == 1
    )


def test_existing_partial_baseline_or_symlink_is_never_repaired(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    draft = service.create(body)
    queued = service.start(
        draft["id"],
        StartInput(
            expected_revision=draft["revision"],
            confirm_model_usage=True,
            confirm_derivative_rights=True,
        ),
    )
    with service.store.locked(queued["run_id"]) as (_c, run):
        run.update(
            state="running", owner="synthetic-generation-owner", first_started_at=now()
        )
    run = service.store.get(queued["run_id"])
    directory = service.settings.home / "runs" / run["id"]
    directory.mkdir(mode=0o700, parents=True)
    service.validate_run(run, directory)
    (directory / "baseline").mkdir(mode=0o700)
    (directory / "baseline/SKILL.md").write_bytes(b"partial")
    with pytest.raises(GenerationError, match="generation_baseline_changed"):
        service.prepare_workspace(run, directory)
    assert (directory / "baseline/SKILL.md").read_bytes() == b"partial"
    (directory / "baseline/SKILL.md").unlink()
    (directory / "baseline").rmdir()
    (directory / "baseline").symlink_to(
        service.library.skills.versions / body.parent_version_id / "context"
    )
    with pytest.raises(GenerationError, match="generation_run_integrity_invalid"):
        service.prepare_workspace(run, directory)
    assert (directory / "baseline").is_symlink()


@pytest.mark.parametrize(
    "declared,status",
    [
        ("Apache-2.0", "declared"),
        ("Unknown custom terms", "declared"),
        ("MIT OR Apache-2.0", "declared"),
        ("MIT", "restricted"),
        (None, "unknown"),
    ],
)
def test_parent_metadata_conflicts_or_unknown_rights_block_draft(
    setup_generation, declared, status
):
    service, body, files, _taskset, _project = setup_generation
    old = service.library.get("skill", body.parent_version_id)
    metadata = {**old["metadata"], "license_text": declared, "license_status": status}
    report = service.library.skills.persist(
        prepare_bytes(files, ("__pycache__", "assets", "empty", "scripts"), metadata)
    )
    service.library.bind(
        "skill",
        report["version_id"],
        {**report, "review_status": "approved", "review_history": []},
    )
    changed = GenerationInput(
        **{**body.model_dump(), "parent_version_id": report["version_id"]}
    )
    with pytest.raises(
        GenerationError,
        match="generation_parent_(?:metadata_license_conflict|license_status_unsupported)",
    ):
        service.create(changed)
    assert service.list() == [] and service.store.list_runs() == []


def test_unbound_candidate_publication_recomputes_every_report_field(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    _draft, run, directory, _workspace, _start = start_run(service, body)
    run = bind_finish(service, run, directory)
    candidate = service.build_candidate(run, directory)
    assert candidate["valid"] and "generation_candidate" not in run
    path = directory / "generation-candidate/report.json"
    original = path.read_bytes()
    for field in (
        "schema_version",
        "content_digest",
        "parent_manifest_sha256",
        "parent_report_sha256",
        "manifest_sha256",
        "valid",
        "errors",
        "policy",
    ):
        for mutation in ("change", "remove"):
            changed = json.loads(original)
            if mutation == "change":
                changed[field] = "unexpected-value"
            else:
                changed.pop(field)
            path.write_bytes(canonical(changed))
            with pytest.raises(GenerationError):
                service.validate_run(run, directory, require_workspace=True)
            path.write_bytes(original)
    path.write_bytes(original + b"\n")
    with pytest.raises(
        GenerationError, match="generation_candidate_structural_report_changed"
    ):
        service.validate_run(run, directory, require_workspace=True)
    path.write_bytes(original)
    assert service.build_candidate(run, directory) == candidate


def test_unbound_invalid_report_and_final_schema_cannot_be_rebound(setup_generation):
    service, body, _parent, _taskset, _project = setup_generation
    _draft, run, directory, _workspace, _start = start_run(service, body)
    run = bind_finish(service, run, directory, '{"append_text":"Copyright fake"}')
    candidate = service.build_candidate(run, directory)
    assert not candidate["valid"]
    path = directory / "generation-candidate/report.json"
    original = json.loads(path.read_bytes())
    for field, value in (
        ("errors", ["different-safe-code"]),
        ("parent_report_sha256", "f" * 64),
        ("schema_version", 2),
    ):
        changed = {**original, field: value}
        path.write_bytes(canonical(changed))
        with pytest.raises(
            GenerationError, match="generation_candidate_structural_report_changed"
        ):
            service.build_candidate(run, directory)
    # A separate completed source tests the final publication-before-Run-bind
    # window: the worker has not saved the final binding yet.
    _draft, run, directory, workspace, _start = candidate_run(service, body)
    evidence = Evidence.capture(workspace, directory, [], Sandbox(), 1, run["id"])
    service.finalize_candidate(run, directory, evidence)
    path = directory / "generation-final/report.json"
    final = json.loads(path.read_bytes())
    for value in (2, True, 1.0):
        path.write_bytes(canonical({**final, "schema_version": value}))
        with pytest.raises(GenerationError, match="generation_final_binding_changed"):
            service.validate_run(run, directory, require_workspace=True)


def test_generation_output_and_candidate_do_not_reset_unknown_reserves(
    setup_generation,
):
    service, body, _parent, _taskset, _project = setup_generation
    _draft, run, directory, _workspace, _start = start_run(service, body)
    with service.store.locked(run["id"]) as (_c, current):
        current["usage"].update(
            calls=1,
            unknown_calls=1,
            reserved_tokens=1234,
            reserved_cost_usd=0.07,
            reserved_metered_cost_usd=0.07,
        )
    run = service.store.get(run["id"])
    before = copy.deepcopy(run["usage"])
    started = run["first_started_at"]
    run = bind_finish(service, run, directory)
    service.build_candidate(run, directory)
    saved = service.store.get(run["id"])
    assert saved["usage"] == before and saved["first_started_at"] == started
