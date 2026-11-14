"""Real PostgreSQL checks for bounded frozen Arena preparation and atomic plans."""

import time
from copy import deepcopy

import controller_test_support
import pytest
from doonex import arena
from doonex.arena import Arena, EvaluationInput
from doonex.background import BackgroundInput
from doonex.deadline import ExecutionDeadlineExceeded
from test_background import background as background_fixture
from test_tasksets import taskset_source as source_fixture

background = background_fixture
controller_registry = controller_test_support.controller_registry
taskset_source = source_fixture
pytestmark = pytest.mark.postgres


def evaluation_context(background):
    service, raw, _clock = background
    grant = BackgroundInput.model_validate(raw)
    body = EvaluationInput.model_validate(
        grant.model_dump(include=set(EvaluationInput.model_fields))
    )
    return Arena(service.settings, service.store), body


def test_frozen_prepare_create_passes_same_deadline_and_preserves_plan(
    background, monkeypatch
):
    service, body = evaluation_context(background)
    before = deepcopy(body.model_dump())
    deadline, seen = time.monotonic() + 30, []
    original = arena.environment

    def environment(*args, **kwargs):
        seen.append(kwargs.get("deadline"))
        return original(*args, **kwargs)

    monkeypatch.setattr(arena, "environment", environment)
    prepared = service.prepare(body, deadline=deadline)
    with service.store.connect() as c:
        value = service.create(body, c, prepared=prepared, deadline=deadline)
    assert seen == [deadline]
    saved = service.get(value["id"], deadline=deadline)
    assert saved == value and body.model_dump() == before
    assert saved["run_budget"] == before["run_budget"]
    assert saved["batch_budget"] == before["batch_budget"]
    assert len(saved["attempts"]) == len(prepared[0]["tasks"]) * 3 * body.repeats
    runs = service.store.list_runs()
    assert len(runs) == len(saved["attempts"])
    assert all(run["state"] == "queued" and run["usage"]["calls"] == 0 for run in runs)
    assert all(run["request"]["budget"] == before["run_budget"] for run in runs)
    assert service.integrity(saved, approval=True, deadline=deadline)["valid"]


def test_expiry_during_child_creation_rolls_back_parent_and_every_child(
    background, monkeypatch
):
    service, body = evaluation_context(background)
    prepared = service.prepare(body)
    original, clock, created = service.store.create_run, [100.0], []

    def create(*args, **kwargs):
        value = original(*args, **kwargs)
        created.append(value["id"])
        clock[0] = 102.0
        return value

    monkeypatch.setattr(service.store, "create_run", create)
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    with pytest.raises(ExecutionDeadlineExceeded), service.store.connect() as c:
        service.create(body, c, prepared=prepared, deadline=101.0)
    assert len(created) == 1
    assert service.list() == [] and service.store.list_runs() == []


def test_expired_prepared_create_never_publishes_parent_or_child(background):
    service, body = evaluation_context(background)
    prepared = service.prepare(body)
    with pytest.raises(ExecutionDeadlineExceeded):
        service.create(body, prepared=prepared, deadline=time.monotonic() - 1)
    assert service.list() == [] and service.store.list_runs() == []
