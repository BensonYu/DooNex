"""Offline deadline propagation probes for immutable content verification."""

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from doonex import generation, skills, tasksets
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.discovery import MIT_BODY
from doonex.evidence import Evidence
from doonex.library import Library
from doonex.skills import SkillRegistry, _json, _sha, prepare_bytes
from doonex.tasksets import TaskSetRegistry
from doonex.workspace import FileContent, WorkspaceProcessUnknown, manifest_digest


def test_taskset_snapshot_digest_unchanged_with_deadline(tmp_path):
    (tmp_path / "hello.py").write_text("print('hello')\n")
    (tmp_path / "empty").mkdir()
    deadline = time.monotonic() + 10
    plain, files = tasksets._snapshot(tmp_path, "source")
    bounded, bounded_files = tasksets._snapshot(tmp_path, "source", deadline=deadline)
    assert files == bounded_files and plain == bounded
    assert bounded["source_digest"] == _sha(
        _json(
            {
                "files": bounded["source_files"],
                "directories": bounded["source_directories"],
            }
        )
    )


def test_taskset_snapshot_rejects_expired_deadline_before_read(tmp_path, monkeypatch):
    def unexpected(*arguments, **kwargs):
        pytest.fail("Expired snapshot read content")

    monkeypatch.setattr(tasksets, "read_files", unexpected)
    with pytest.raises(ExecutionDeadlineExceeded):
        tasksets._snapshot(tmp_path, "source", deadline=time.monotonic() - 1)


def test_taskset_snapshot_passes_one_deadline_to_each_stage(tmp_path, monkeypatch):
    (tmp_path / "hello.py").write_text("print('hello')\n")
    deadline = time.monotonic() + 10
    seen = []
    for name in ["read_files", "_tree", "manifest", "_json_digest"]:
        original = getattr(tasksets, name)

        def tracked(*arguments, _original=original, _name=name, **kwargs):
            seen.append((_name, kwargs.get("deadline")))
            return _original(*arguments, **kwargs)

        monkeypatch.setattr(tasksets, name, tracked)
    tasksets._snapshot(tmp_path, "source", deadline=deadline)
    assert seen == [
        (name, deadline) for name in ["read_files", "_tree", "manifest", "_json_digest"]
    ]


@pytest.mark.parametrize(
    "constructor", [SkillRegistry, TaskSetRegistry, Library, generation.Generation]
)
def test_expired_constructor_does_not_create_storage(tmp_path, constructor):
    home = tmp_path / "must-not-exist"
    with pytest.raises(ExecutionDeadlineExceeded):
        if constructor in {SkillRegistry, TaskSetRegistry}:
            constructor(home, deadline=time.monotonic() - 1)
        else:
            constructor(
                SimpleNamespace(home=home), object(), deadline=time.monotonic() - 1
            )
    assert not home.exists()


@pytest.fixture
def frozen_skill(tmp_path):
    registry = SkillRegistry(tmp_path / "private")
    files = {
        "SKILL.md": FileContent(b"Read existing conventions.\n", 0o644),
        "LICENSE": FileContent(
            (
                "MIT License\n\nCopyright (c) 2026 Test Authors\n\n" + MIT_BODY + "\n"
            ).encode(),
            0o644,
        ),
        "assets/data.bin": FileContent(b"\x00\xffbounded", 0o644),
    }
    metadata = {
        "source": "synthetic-private-test",
        "license_status": "declared",
        "license_text": "MIT",
        "scope": ["test"],
        "permissions": [],
    }
    record = registry.persist(prepare_bytes(files, ("assets", "empty"), metadata))
    return registry, {**record, "review_status": "approved"}, files


def test_skill_context_same_deadline_and_bytes(frozen_skill, monkeypatch):
    registry, record, _files = frozen_skill
    plain = registry.context(record["version_id"], record["report_sha256"])
    deadline = time.monotonic() + 10
    seen = []
    for name in ["_files", "_directories", "_identity", "_instructions"]:
        original = getattr(skills, name)

        def tracked(*arguments, _original=original, _name=name, **kwargs):
            seen.append((_name, kwargs.get("deadline")))
            return _original(*arguments, **kwargs)

        monkeypatch.setattr(skills, name, tracked)
    bounded = registry.context(
        record["version_id"], record["report_sha256"], deadline=deadline
    )
    assert bounded == plain
    assert {name for name, _ in seen} == {
        "_files",
        "_directories",
        "_identity",
        "_instructions",
    }
    assert all(value == deadline for _, value in seen)


def unknown_process():
    return WorkspaceProcessUnknown(
        SimpleNamespace(pid=1234567, returncode=None, stdout=None, stderr=None)
    )


@pytest.mark.parametrize("kind", ["skill", "taskset"])
@pytest.mark.parametrize(
    "exception", [ExecutionDeadlineExceeded, WorkspaceProcessUnknown]
)
def test_inspect_and_context_preserve_controller_abort(
    tmp_path, monkeypatch, kind, exception
):
    registry = (
        SkillRegistry(tmp_path / "private")
        if kind == "skill"
        else TaskSetRegistry(tmp_path / "private")
    )
    error = (
        unknown_process()
        if exception is WorkspaceProcessUnknown
        else ExecutionDeadlineExceeded("expired")
    )

    def interrupted(*arguments, **kwargs):
        raise error

    monkeypatch.setattr(registry, "_load", interrupted)
    for method in [registry.inspect, registry.context]:
        with pytest.raises(exception) as raised:
            method(
                "a" * 64,
                "b" * 64,
                *(["task"] if kind == "taskset" and method == registry.context else []),
                deadline=time.monotonic() + 10,
            )
        assert raised.value is error
        if exception is WorkspaceProcessUnknown:
            assert raised.value.cleanup_unknown
            assert raised.value.cleanup_evidence["local_process_unknown"]


def test_skill_timeout_during_file_scan_is_not_invalid(frozen_skill, monkeypatch):
    registry, record, _files = frozen_skill
    original = skills.read_files

    def expires_after_read(*arguments, **kwargs):
        original(*arguments, **kwargs)
        raise ExecutionDeadlineExceeded("scan reached original deadline")

    monkeypatch.setattr(skills, "read_files", expires_after_read)
    with pytest.raises(ExecutionDeadlineExceeded):
        registry.inspect(
            record["version_id"],
            record["report_sha256"],
            deadline=time.monotonic() + 10,
        )


def test_library_skill_context_keeps_binding_and_one_deadline(
    frozen_skill, monkeypatch
):
    registry, record, _files = frozen_skill
    library = Library.__new__(Library)
    library.skills = registry
    seen = []

    def saved(kind, identifier, *, deadline=None):
        seen.append((kind, identifier, deadline))
        return record

    monkeypatch.setattr(library, "get", saved)
    binding = {
        key: record[key]
        for key in ("version_id", "content_digest", "report_sha256", "metadata")
    }
    deadline = time.monotonic() + 10
    assert (
        library.skill_context(binding, deadline=deadline).instructions
        == "Read existing conventions.\n"
    )
    assert seen == [("skill", record["version_id"], deadline)]
    binding["content_digest"] = "0" * 64
    with pytest.raises(ValueError, match="绑定"):
        library.skill_context(binding, deadline=deadline)


@pytest.fixture
def offline_generation(frozen_skill, monkeypatch):
    registry, record, files = frozen_skill
    home = registry.home
    run = {
        "id": str(uuid4()),
        "owner": "test-owner",
        "state": "running",
        "assignments": {"executor": {"id": str(uuid4())}},
    }

    class MemoryStore:
        @contextmanager
        def locked(self, run_id, owner):
            assert run_id == run["id"] and owner == run["owner"]
            yield None, run

    service = generation.Generation(SimpleNamespace(home=home), MemoryStore())

    def saved_skill(kind, identifier, *, deadline=None):
        assert kind == "skill" and identifier == record["version_id"]
        return record

    monkeypatch.setattr(service.library, "get", saved_skill)
    parent = {
        key: record[key]
        for key in ("version_id", "content_digest", "report_sha256", "metadata")
    }
    _files, identity, license_receipt = service._parent(parent)
    value = {
        "schema_version": generation.SCHEMA_VERSION,
        "policy": generation.POLICY,
        "input": {"taskset_id": "e" * 64, "task_ids": ["one"]},
        "project": {"id": str(uuid4())},
        "parent": parent,
        "parent_manifest": identity,
        "license": license_receipt,
        "taskset_report_sha256": "f" * 64,
        "visible_tasks": [
            {
                "id": "one",
                "title": "Test",
                "prompt": "Visible only",
                "visible_checks": [],
            }
        ],
        "model": {},
        "model_profile": {},
        "prompt": "Frozen visible prompt",
        "budget": {},
    }
    value["projection_sha256"] = generation.digest(value["visible_tasks"])
    value["prompt_sha256"] = generation.sha(value["prompt"].encode())
    value["input_sha256"] = generation.digest(value)
    value.update(id=str(uuid4()), run_id=run["id"])
    run["request"] = {
        "purpose": "skill_generation",
        "generation": service._binding(value),
    }
    value["run_request_sha256"] = generation.digest(run["request"])

    def saved_generation(identifier, connection=None, *, deadline=None):
        assert identifier == value["id"]
        return value

    def forbidden(*arguments, **kwargs):
        pytest.fail(
            "Runtime generation inspected a taskset, source or heldout material"
        )

    monkeypatch.setattr(service, "get", saved_generation)
    monkeypatch.setattr(service, "_tasks", forbidden)
    for name in ["_load", "inspect", "context"]:
        monkeypatch.setattr(service.library.tasksets, name, forbidden)
    directory = home / "runs" / run["id"]
    directory.mkdir(parents=True, mode=0o700)
    return service, run, directory, files, value


def test_generation_prepare_output_candidate_resume_one_deadline(
    offline_generation, monkeypatch
):
    service, run, directory, files, _value = offline_generation
    deadline = time.monotonic() + 10
    seen = []
    for name in [
        "_read",
        "_write",
        "_snapshot",
        "_tree",
        "_archive",
        "manifest",
        "manifest_digest",
    ]:
        original = getattr(generation, name)

        def tracked(*arguments, _original=original, _name=name, **kwargs):
            seen.append((_name, kwargs.get("deadline")))
            return _original(*arguments, **kwargs)

        monkeypatch.setattr(generation, name, tracked)
    workspace = service.prepare_workspace(run, directory, deadline=deadline)
    run["baseline_digest"] = workspace.baseline_digest
    assert workspace.baseline_digest == manifest_digest(
        generation.manifest(files, deadline=deadline)
    )
    message = json.dumps({"append_text": "Keep a narrow change."})
    run["generation_output"] = service.save_output(
        run, directory, message, deadline=deadline
    )
    candidate = service.build_candidate(run, directory, deadline=deadline)
    assert candidate["valid"]
    run["generation_candidate"] = candidate
    service.validate_run(run, directory, require_workspace=True, deadline=deadline)
    resumed = service.prepare_workspace(run, directory, deadline=deadline)
    assert resumed.baseline_digest == workspace.baseline_digest
    assert (resumed.path / "SKILL.md").read_bytes() == files[
        "SKILL.md"
    ].data + generation.SEPARATOR + b"Keep a narrow change."
    assert (resumed.path / "assets/data.bin").read_bytes() == files[
        "assets/data.bin"
    ].data
    assert {name for name, _ in seen} == {
        "_read",
        "_write",
        "_snapshot",
        "_tree",
        "_archive",
        "manifest",
        "manifest_digest",
    }
    assert all(value == deadline for _, value in seen)


def test_generation_final_evidence_and_resume_share_one_deadline(
    offline_generation, monkeypatch
):
    service, run, directory, _files, _value = offline_generation
    deadline = time.monotonic() + 10
    workspace = service.prepare_workspace(run, directory, deadline=deadline)
    run["baseline_digest"] = workspace.baseline_digest
    run["generation_output"] = service.save_output(
        run,
        directory,
        json.dumps({"append_text": "Preserve the public contract."}),
        deadline=deadline,
    )
    run["generation_candidate"] = service.build_candidate(
        run, directory, deadline=deadline
    )
    run["state"] = "verifying"
    evidence = Evidence.capture(
        workspace,
        directory,
        [],
        SimpleNamespace(image="offline-no-execution"),
        1,
        run["id"],
        deadline=deadline,
    )
    run["evidence"] = evidence
    inspected = []
    original = Evidence.inspect

    def inspect(*args, deadline=None, **kwargs):
        inspected.append(deadline)
        return original(*args, deadline=deadline, **kwargs)

    monkeypatch.setattr(Evidence, "inspect", inspect)
    final = service.finalize_candidate(run, directory, evidence, deadline=deadline)
    assert final["valid"]
    assert final["evidence_report_sha256"] == evidence["report_sha256"]
    assert evidence["verified"] is False and evidence["commands"] == []
    run["generation_candidate"] = final
    service.validate_run(run, directory, require_workspace=True, deadline=deadline)
    assert (
        service.finalize_candidate(run, directory, evidence, deadline=deadline) == final
    )
    assert len(inspected) >= 4 and set(inspected) == {deadline}


def test_archive_identity_unchanged_and_expiry_checked_during_copy(monkeypatch):
    files = {"large.bin": FileContent(b"x" * (256 * 1024), 0o644)}
    assert generation._archive(files, []) == generation._archive(
        files, [], deadline=time.monotonic() + 10
    )
    clock, reads = [100.0], []
    original = generation.io.BytesIO

    class SlowReader(original):
        def read(self, size=-1):
            result = super().read(size)
            reads.append(len(result))
            clock[0] += 0.6
            return result

    monkeypatch.setattr(generation.io, "BytesIO", SlowReader)
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    with pytest.raises(ExecutionDeadlineExceeded):
        generation._archive(files, [], deadline=101.0)
    assert reads == [16 * 1024, 16 * 1024]


@pytest.mark.parametrize(
    "exception", [ExecutionDeadlineExceeded, WorkspaceProcessUnknown]
)
def test_generation_validation_preserves_deep_parent_abort(
    offline_generation, monkeypatch, exception
):
    service, run, directory, _files, _value = offline_generation
    error = (
        unknown_process()
        if exception is WorkspaceProcessUnknown
        else ExecutionDeadlineExceeded("expired parent")
    )

    def interrupted(*arguments, **kwargs):
        raise error

    monkeypatch.setattr(service.library.skills, "_load", interrupted)
    with pytest.raises(exception) as raised:
        service.validate_run(run, directory, deadline=time.monotonic() + 10)
    assert raised.value is error


def test_generation_expired_stage_does_not_publish_baseline(
    offline_generation, monkeypatch
):
    service, run, directory, _files, _value = offline_generation
    original = generation._tree
    clock = [time.monotonic()]
    deadline = clock[0] + 10

    def expires_after_copy(*arguments, **kwargs):
        original(*arguments, **kwargs)
        clock[0] = deadline + 1

    monkeypatch.setattr(generation, "_tree", expires_after_copy)
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    with pytest.raises(ExecutionDeadlineExceeded):
        service.prepare_workspace(run, directory, deadline=deadline)
    assert not (directory / "baseline").exists()
    assert not (directory / "workspace").exists()
    assert not list(directory.glob(".generation-stage-*"))


@pytest.mark.parametrize("helper", [skills._sha, generation.sha])
def test_content_hash_expiry_is_checked_between_chunks(monkeypatch, helper):
    clock = [100.0]
    deadline = 101.0
    module = skills if helper is skills._sha else generation
    original = module.hashlib.sha256
    updates = []

    class SlowHash:
        def __init__(self):
            self.inner = original()

        def update(self, chunk):
            self.inner.update(chunk)
            updates.append(len(chunk))
            clock[0] += 0.6

        def hexdigest(self):
            return self.inner.hexdigest()

    monkeypatch.setattr(module.hashlib, "sha256", SlowHash)
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    with pytest.raises(ExecutionDeadlineExceeded):
        helper(b"x" * (256 * 1024), deadline=deadline)
    assert updates == [64 * 1024, 64 * 1024]


@pytest.fixture
def frozen_taskset(tmp_path):
    registry = TaskSetRegistry(tmp_path / "taskset-private")
    root = registry.versions / "staging"
    context = root / "context"
    context.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    (context / "tasks").mkdir(mode=0o700)
    tasks = []
    provenance = {}
    for index in range(5):
        task_id = f"task-{index}"
        task_root = context / "tasks" / task_id
        task_root.mkdir(mode=0o700)
        for kind, name in [("source", "main.py"), ("verifier", "check.py")]:
            (task_root / kind).mkdir(mode=0o700)
            (task_root / kind / name).write_text("assert True\n")
        task = {
            "id": task_id,
            "title": "Test",
            "prompt": "Visible",
            "base_commit": "c" * 40,
            "visible_checks": [],
            "verifier_command": "python /verifier/check.py",
            "critical": False,
        }
        for kind in ["source", "verifier"]:
            info, _ = tasksets._snapshot(task_root / kind, kind, private=True)
            task.update(info)
        tasks.append(task)
        provenance[task_id] = {
            "repo_path": str(tmp_path / "frozen-source"),
            "requested_ref": "HEAD",
        }
    identity = tasksets._identity(
        {
            "name": "Synthetic frozen tasks",
            "category": "test",
            "split": "heldout",
            "minimum_pass_rate": 0.8,
        },
        tasks,
    )
    identifier = _sha(_json(identity))
    report = {
        **identity,
        "taskset_id": identifier,
        "provenance": provenance,
        "created_at": "2026-11-14T00:00:00+00:00",
    }
    raw = _json(report)
    (root / "report.json").write_bytes(raw)
    (root / "report.json").chmod(0o600)
    root.rename(registry.versions / identifier)
    return registry, identifier, _sha(raw)


def test_taskset_context_passes_same_deadline_without_changing_projection(
    frozen_taskset, monkeypatch
):
    registry, identifier, report = frozen_taskset
    plain = registry.context(identifier, report, "task-1")
    deadline = time.monotonic() + 10
    seen = []
    for name in ["_read_json", "_snapshot", "_tree", "_sha", "_json"]:
        original = getattr(tasksets, name)

        def tracked(*arguments, _original=original, _name=name, **kwargs):
            seen.append((_name, kwargs.get("deadline")))
            return _original(*arguments, **kwargs)

        monkeypatch.setattr(tasksets, name, tracked)
    bounded = registry.context(identifier, report, "task-1", deadline=deadline)
    assert bounded == plain
    assert "verifier_command" not in bounded.public_metadata
    assert "verifier_digest" not in bounded.public_metadata
    assert all(value == deadline for _, value in seen)
    assert {name for name, _ in seen} == {
        "_read_json",
        "_snapshot",
        "_tree",
        "_sha",
        "_json",
    }


@pytest.mark.parametrize("helper", [tasksets._read_json, generation._read])
def test_artifact_read_expiry_checks_chunks_and_closes_descriptor(
    tmp_path, monkeypatch, helper
):
    module = tasksets if helper is tasksets._read_json else generation
    path = tmp_path / "artifact.json"
    path.write_bytes(b'"' + b"x" * (256 * 1024) + b'"')
    path.chmod(0o600)
    original = module.os.fdopen
    clock = [100.0]
    observed = []

    class SlowReader:
        def __init__(self, fd, *arguments, **kwargs):
            self.fd = fd
            self.source = original(fd, *arguments, **kwargs)
            observed.append(fd)

        def __enter__(self):
            self.source.__enter__()
            return self

        def __exit__(self, *arguments):
            return self.source.__exit__(*arguments)

        def read(self, size):
            value = self.source.read(size)
            clock[0] += 0.6
            return value

    monkeypatch.setattr(module.os, "fdopen", SlowReader)
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    with pytest.raises(ExecutionDeadlineExceeded):
        helper(
            path,
            deadline=101.0,
            **({"maximum": 512 * 1024} if module is tasksets else {}),
        )
    assert len(observed) == 1
    with pytest.raises(OSError):
        module.os.fstat(observed[0])
