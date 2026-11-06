"""Arena classification requires its exact controller-bound evidence report."""

from copy import deepcopy
from uuid import uuid4

import pytest
from doonex.arena import Arena
from doonex.evidence import Evidence
from doonex.store import now


@pytest.fixture
def arena_run(settings, store, request_data):
    request = deepcopy(request_data)
    request["arena"] = {
        "taskset_id": "a" * 64,
        "task_id": "task-0",
        "report_sha256": "b" * 64,
        "verifier_digest": "c" * 64,
    }
    run = store.create_run(request)
    with store.locked(run["id"]) as (_, current):
        current.update(
            state="completed_verified",
            first_started_at=now(),
            ended_at=now(),
            evidence={"report_sha256": "d" * 64},
            arena_outcome={"classification": "passed", "recorded_at": now()},
        )
    return Arena(settings, store), {
        "run_id": run["id"],
        "arm_id": "baseline",
        "task_id": "task-0",
        "repeat": 0,
    }


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        {},
        {"report_sha256": None},
        {"report_sha256": "short"},
        {"report_sha256": "G" * 64},
    ],
)
def test_unbound_digest_never_falls_back_to_disk_report(
    arena_run, monkeypatch, evidence
):
    arena, entry = arena_run
    with arena.store.locked(entry["run_id"]) as (_, run):
        run["evidence"] = evidence

    def forbidden(*args, **kwargs):
        pytest.fail("Arena must not request unbound Evidence.inspect")

    monkeypatch.setattr(Evidence, "inspect", forbidden)
    classified = arena.attempt(entry)
    assert classified["classification"] == "failed"
    assert classified["evidence_valid"] is False
    assert classified["run_id"] == entry["run_id"]


@pytest.mark.parametrize("state", ["completed_verified", "failed", "cancelled"])
@pytest.mark.parametrize("mismatch", ["run_id", "commands"])
def test_bound_digest_still_requires_same_run_and_commands(
    arena_run, monkeypatch, state, mismatch
):
    arena, entry = arena_run
    with arena.store.locked(entry["run_id"]) as (_, run):
        run["state"] = state
    run = arena.store.get(entry["run_id"])
    report = {"run_id": run["id"], "commands": run["request"]["checks"]}
    report[mismatch] = str(uuid4()) if mismatch == "run_id" else ["foreign-check"]
    seen = []

    def inspect(workspace, directory, expected, *, verification_context):
        seen.append((expected, verification_context))
        return {"valid": True, "verified": True, "report": report}

    monkeypatch.setattr(Evidence, "inspect", inspect)
    classified = arena.attempt(entry)
    assert len(seen) == 1 and seen[0][0] == run["evidence"]["report_sha256"]
    assert classified["evidence_valid"] is False
    assert classified["classification"] == (
        "cancelled" if state == "cancelled" else "failed"
    )


def test_original_success_and_controller_environment_failure_semantics_preserved(
    arena_run, monkeypatch
):
    arena, entry = arena_run
    run = arena.store.get(entry["run_id"])
    monkeypatch.setattr(
        Evidence,
        "inspect",
        lambda *a, **kw: {
            "valid": True,
            "verified": True,
            "report": {"run_id": run["id"], "commands": run["request"]["checks"]},
        },
    )
    classified = arena.attempt(entry)
    assert classified["classification"] == "passed" and classified["evidence_valid"]
    with arena.store.locked(entry["run_id"]) as (_, current):
        current.update(
            state="failed",
            evidence=None,
            arena_outcome={
                "classification": "environment_failure",
                "recorded_at": now(),
            },
        )
    classified = arena.attempt(entry)
    assert (
        classified["classification"] == "environment_failure"
        and classified["evidence_valid"]
    )
