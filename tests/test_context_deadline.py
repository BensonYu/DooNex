"""Offline Arena/background deadline checks against real frozen private content."""

import hashlib
import json
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from doonex import arena, background, skills, tasksets
from doonex.arena import Arena, EvaluationInput
from doonex.background import Background, BackgroundInput, BackgroundStopped
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.model_registry import ModelRegistry
from doonex.skills import FileContent, prepare_bytes
from doonex.workspace import WorkspaceProcessUnknown
from test_content_deadline import frozen_skill as skill_fixture
from test_content_deadline import frozen_taskset as taskset_fixture

frozen_skill = skill_fixture
frozen_taskset = taskset_fixture


@pytest.fixture
def context(frozen_skill, frozen_taskset, model):
    registry, first, files = frozen_skill
    first = {**first, "review_history": []}
    second_files = {
        **files,
        "SKILL.md": FileContent(b"Keep controlled changes.\n", 0o644),
    }
    second = registry.persist(
        prepare_bytes(second_files, ("assets", "empty"), first["metadata"])
    )
    second = {**second, "review_status": "approved", "review_history": []}
    task_registry, old_id, old_hash = frozen_taskset
    old = task_registry._load(old_id, old_hash)[0]
    identity = tasksets._identity(
        {key: old[key] for key in ("name", "category", "split", "minimum_pass_rate")},
        old["tasks"],
    )
    identity["split"] = "tuning"
    identifier = skills._sha(skills._json(identity))
    report = {
        **identity,
        "taskset_id": identifier,
        "provenance": old["provenance"],
        "created_at": old["created_at"],
    }
    raw = skills._json(report)
    old_root = task_registry.versions / old_id
    (old_root / "report.json").write_bytes(raw)
    old_root.rename(task_registry.versions / identifier)
    task_record = task_registry._load(identifier, skills._sha(raw))[0]
    records = {("taskset", identifier): task_record}
    records.update(
        {("skill", record["version_id"]): record for record in (first, second)}
    )

    class Connection:
        def execute(self, query, args=()):
            if "data->>'split'" in query:
                record = records[("taskset", args[0])]
                row = {
                    "split": record["split"],
                    "report_sha256": record["report_sha256"],
                }
            elif "WHERE kind='skill'" in query:
                row = {"data": records[("skill", args[0])]}
            elif "library_versions" in query:
                row = {"data": records[tuple(args)]}
            elif "background_authorizations" in query:
                row = {"data": store.grant}
            elif "arena_evaluations" in query:
                row = {"data": store.batch}
            else:
                pytest.fail("Unexpected write or query in frozen preparation")
            return SimpleNamespace(fetchone=lambda: row)

    class Store:
        connects = 0
        grant = None
        batch = None

        @contextmanager
        def connect(self):
            self.connects += 1
            yield Connection()

    store = Store()
    settings = SimpleNamespace(
        home=registry.home, model=model, credential=lambda model: "offline-fixture-only"
    )
    now = datetime(2031, 4, 5, 10, tzinfo=UTC)
    service = Background(settings, store, clock=lambda: now)
    service.library.skills, service.library.tasksets = registry, task_registry
    body = BackgroundInput(
        taskset_id=identifier,
        skill_version_ids=[first["version_id"], second["version_id"]],
        model_profile_id="legacy",
        repeats=1,
        window_start="2031-04-05T10:00:00Z",
        window_end="2031-04-05T11:00:00Z",
        run_budget={
            "max_calls": 3,
            "max_tokens": 100000,
            "max_cost_usd": 1,
            "max_seconds": 60,
        },
        batch_budget={
            "max_calls": 20,
            "max_tokens": 200000,
            "max_cost_usd": 2,
            "max_seconds": 300,
        },
        daily_budget={
            "max_calls": 40,
            "max_tokens": 400000,
            "max_cost_usd": 4,
            "max_seconds": 600,
        },
    )
    with store.connect() as c:
        snapshot = service._snapshot(body, c)
    store.grant = {
        "id": str(uuid4()),
        "state": "enabled",
        "date": "2031-04-05",
        "window_start": body.window_start.isoformat(),
        "window_end": body.window_end.isoformat(),
        "input": body.model_dump(mode="json"),
        "snapshot": snapshot,
    }
    store.grant["revision"] = background._revision(store.grant)
    arena_service = Arena(settings, store)
    arena_service.library = service.library
    store.batch = {
        "id": str(uuid4()),
        "state": "queued",
        "environment": snapshot["environment"],
        "taskset_id": identifier,
        "taskset_report_sha256": task_record["report_sha256"],
        "skills": {
            key: {
                name: value
                for name, value in binding.items()
                if name != "review_revision"
            }
            for key, binding in snapshot["skills"].items()
        },
        "first_started_at": None,
    }
    return service, arena_service, body, store, records, now


def tracked(monkeypatch, target, names, seen):
    for name in names:
        original = getattr(target, name)

        def observe(*args, _original=original, _name=name, **kwargs):
            seen.append((_name, kwargs.get("deadline")))
            return _original(*args, **kwargs)

        monkeypatch.setattr(target, name, observe)


def test_background_prepare_same_deadline_binding_and_budget(context, monkeypatch):
    service, _arena, _body, store, _records, now = context
    before = deepcopy(store.grant)
    deadline, seen = time.monotonic() + 10, []
    tracked(monkeypatch, service.library, ["get", "taskset", "skill"], seen)
    tracked(monkeypatch, service.library.skills, ["_load"], seen)
    tracked(monkeypatch, service.library.tasksets, ["_load"], seen)
    tracked(monkeypatch, background, ["environment", "digest"], seen)
    result = service.prepare(store.grant["id"], deadline=deadline)
    assert result == {
        "authorization_id": before["id"],
        "revision": before["revision"],
        "snapshot": before["snapshot"],
        "checked_at": now.isoformat(),
    }
    assert store.grant == before
    assert {name for name, _ in seen} == {
        "get",
        "taskset",
        "skill",
        "_load",
        "environment",
        "digest",
    }
    assert all(value == deadline for _, value in seen)


def test_arena_prepare_integrity_and_start_share_deadline(context, monkeypatch):
    _background, service, body, store, _records, _now = context
    deadline, seen = time.monotonic() + 10, []
    evaluation_body = EvaluationInput.model_validate(
        body.model_dump(include=set(EvaluationInput.model_fields))
    )
    plain = service.prepare(evaluation_body)
    tracked(
        monkeypatch,
        service.library,
        ["get", "taskset", "freeze_skill", "skill_context"],
        seen,
    )
    tracked(monkeypatch, service.library.skills, ["_load"], seen)
    tracked(monkeypatch, service.library.tasksets, ["_load"], seen)
    tracked(monkeypatch, arena, ["environment"], seen)
    bounded = service.prepare(evaluation_body, deadline=deadline)
    assert bounded == plain
    assert service.integrity(store.batch, approval=True, deadline=deadline) == {
        "valid": True,
        "errors": [],
    }
    locks = []

    @contextmanager
    def locked(identifier):
        locks.append(identifier)
        yield None, store.batch

    monkeypatch.setattr(service, "locked", locked)
    assert service.start(store.batch["id"], deadline=deadline)
    assert store.batch["state"] == "running" and store.batch["first_started_at"]
    first_started = store.batch["first_started_at"]
    assert service.start(store.batch["id"], deadline=deadline)
    assert store.batch["first_started_at"] == first_started
    assert len(locks) == 2 and seen
    assert all(value == deadline for _, value in seen)


@pytest.mark.parametrize("constructor", [Arena, Background])
def test_expired_context_constructor_never_creates_storage(tmp_path, constructor):
    path = tmp_path / "must-not-exist"
    with pytest.raises(ExecutionDeadlineExceeded):
        constructor(SimpleNamespace(home=path), object(), deadline=time.monotonic() - 1)
    assert not path.exists()


@pytest.mark.parametrize(
    "operation",
    [
        "background_prepare",
        "snapshot",
        "unchanged",
        "arena_prepare",
        "integrity",
        "start",
    ],
)
def test_expired_context_operation_stops_before_content_or_db(
    context, monkeypatch, operation
):
    service, arena_service, body, store, _records, _now = context
    connections = store.connects

    def forbidden(*args, **kwargs):
        pytest.fail("Expired operation opened frozen content")

    monkeypatch.setattr(service.library.tasksets, "_load", forbidden)
    monkeypatch.setattr(service.library.skills, "_load", forbidden)
    deadline = time.monotonic() - 1
    calls = {
        "background_prepare": lambda: service.prepare(
            store.grant["id"], deadline=deadline
        ),
        "snapshot": lambda: service._snapshot(body, object(), deadline=deadline),
        "unchanged": lambda: service._unchanged(
            store.grant, object(), deadline=deadline
        ),
        "arena_prepare": lambda: arena_service.prepare(body, deadline=deadline),
        "integrity": lambda: arena_service.integrity(store.batch, deadline=deadline),
        "start": lambda: arena_service.start(store.batch["id"], deadline=deadline),
    }
    with pytest.raises(ExecutionDeadlineExceeded):
        calls[operation]()
    assert store.connects == connections and store.batch["state"] == "queued"


def controlled_abort(kind):
    if kind == "deadline":
        return ExecutionDeadlineExceeded("synthetic deadline")
    if kind == "process":
        return WorkspaceProcessUnknown(
            SimpleNamespace(pid=1234567, returncode=None, stdout=None, stderr=None)
        )
    error = OSError("synthetic cleanup uncertainty")
    error.cleanup_unknown, error.cleanup_evidence = (
        True,
        {"local_process_unknown": True},
    )
    if kind == "wrapped":
        outer = ValueError("wrapped interruption")
        outer.__cause__ = error
        return outer
    return error


@pytest.mark.parametrize("kind", ["deadline", "process", "cleanup", "wrapped"])
@pytest.mark.parametrize("path", ["arena_integrity", "background_profile"])
def test_context_control_abort_is_preserved(context, monkeypatch, kind, path):
    service, arena_service, body, store, _records, _now = context
    error = controlled_abort(kind)

    def abort(*args, **kwargs):
        raise error

    if path == "arena_integrity":
        monkeypatch.setattr(service.library, "skill_context", abort)
        call = lambda: arena_service.integrity(
            store.batch, deadline=time.monotonic() + 10
        )
    else:
        monkeypatch.setattr(ModelRegistry, "get", abort)
        with store.connect() as c:
            call = lambda: service._snapshot(body, c, deadline=time.monotonic() + 10)
    with pytest.raises(type(error)) as raised:
        call()
    assert raised.value is error


@pytest.mark.parametrize("path", ["arena_integrity", "background_profile"])
def test_ordinary_context_integrity_errors_remain_refusals(context, monkeypatch, path):
    service, arena_service, body, store, _records, _now = context

    def invalid(*args, **kwargs):
        raise ValueError("ordinary content mismatch")

    if path == "arena_integrity":
        monkeypatch.setattr(service.library, "skill_context", invalid)
        result = arena_service.integrity(store.batch, deadline=time.monotonic() + 10)
        assert result["valid"] is False and len(result["errors"]) == 2
    else:
        monkeypatch.setattr(ModelRegistry, "get", invalid)
        with store.connect() as c, pytest.raises(BackgroundStopped, match="模型配置"):
            service._snapshot(body, c, deadline=time.monotonic() + 10)


def test_arena_expiry_after_content_check_never_enters_start_lock(context, monkeypatch):
    _background, service, _body, store, _records, _now = context
    original, clock = service.integrity, [100.0]

    def expires(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 102.0
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("Expired content validation entered start lock")

    monkeypatch.setattr(service, "integrity", expires)
    monkeypatch.setattr(service, "locked", forbidden)
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    with pytest.raises(ExecutionDeadlineExceeded):
        service.start(store.batch["id"], deadline=101.0)
    assert store.batch["state"] == "queued" and store.batch["first_started_at"] is None


def test_arena_prepared_create_keeps_deadline_before_transaction(context, monkeypatch):
    _background, service, body, store, _records, _now = context
    prepared = service.prepare(body)
    original, clock = arena.environment, [100.0]
    connections = store.connects

    def expires(*args, **kwargs):
        assert kwargs["deadline"] == 101.0
        result = original(*args, **kwargs)
        clock[0] = 102.0
        return result

    monkeypatch.setattr(arena, "environment", expires)
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    with pytest.raises(ExecutionDeadlineExceeded):
        service.create(body, prepared=prepared, deadline=101.0)
    assert store.connects == connections and store.batch["state"] == "queued"


def test_metadata_only_background_check_never_opens_frozen_content(
    context, monkeypatch
):
    service, _arena, _body, store, _records, _now = context

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata preflight opened frozen task or Skill material")

    for target, names in [
        (service.library, ["get", "taskset", "skill"]),
        (service.library.skills, ["_load"]),
        (service.library.tasksets, ["_load"]),
    ]:
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    with store.connect() as c:
        result = service._unchanged(
            store.grant, c, inspect_contents=False, deadline=time.monotonic() + 10
        )
    assert result.model_dump(mode="json") == store.grant["input"]


def test_background_heldout_gate_precedes_every_frozen_read(context, monkeypatch):
    service, _arena, body, store, records, _now = context
    records[("taskset", body.taskset_id)]["split"] = "heldout"

    def forbidden(*args, **kwargs):
        pytest.fail("Heldout gate opened frozen contents")

    monkeypatch.setattr(service.library, "get", forbidden)
    with store.connect() as c, pytest.raises(BackgroundStopped, match="tuning"):
        service._snapshot(body, c, deadline=time.monotonic() + 10)


def test_environment_digest_preserves_identity_and_deadline(tmp_path, monkeypatch):
    path = tmp_path / "controller.py"
    raw = b"x" * (256 * 1024)
    path.write_bytes(raw)
    assert (
        arena._controller_digest(path, deadline=time.monotonic() + 10)
        == hashlib.sha256(raw).hexdigest()
    )
    assert arena.environment() == arena.environment(deadline=time.monotonic() + 10)
    value = {"unicode": "调优", "list": [None, False, 1.2, {"a": "b"}]}
    assert (
        arena.digest(value, deadline=time.monotonic() + 10)
        == hashlib.sha256(
            json.dumps(
                value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    clock, reads = [100.0], []
    original = type(path).open

    class Reader:
        def __init__(self, item, *args, **kwargs):
            self.inner = original(item, *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()

        def read(self, size):
            raw = self.inner.read(size)
            reads.append(len(raw))
            clock[0] += 0.6
            return raw

    monkeypatch.setattr(
        type(path), "open", lambda item, *args, **kwargs: Reader(item, *args, **kwargs)
    )
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    with pytest.raises(ExecutionDeadlineExceeded):
        arena._controller_digest(path, deadline=101.0)
    assert reads == [64 * 1024, 64 * 1024]


def test_controller_digest_has_finite_size_limit(tmp_path, monkeypatch):
    path = tmp_path / "controller.py"
    path.write_bytes(b"x" * 65)
    monkeypatch.setattr(arena, "MAX_CONTROLLER_BYTES", 64)
    with pytest.raises(ValueError, match="允许大小"):
        arena._controller_digest(path, deadline=time.monotonic() + 10)
