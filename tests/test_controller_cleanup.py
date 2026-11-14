"""Run stop intents preserve identity before lock-free controller cleanup."""

from copy import deepcopy
from uuid import uuid4

import pytest
from doonex.cleanup import (
    begin_cleanup_intent,
    cleanup_intent_matches,
    cleanup_intent_unresolved,
    controller_cleanup_unknown,
    finish_cleanup_intent,
    merge_cleanup,
    operation_context,
    sandbox_context,
)
from doonex.controller_operations import RegistryError, fresh_actor


@pytest.fixture
def cleanup_run():
    return {
        "id": str(uuid4()),
        "owner": None,
        "state": "paused",
        "request": {"project_id": str(uuid4()), "prompt": "synthetic fixture"},
        "usage": {"reserved_tokens": 37},
        "first_started_at": "2031-01-02T03:04:05+00:00",
    }


def test_context_uses_actual_nullable_owner_and_action(cleanup_run):
    action = str(uuid4())
    bound = operation_context(cleanup_run, parent_action_id=action)
    assert bound == {
        "run_id": cleanup_run["id"],
        "project_id": cleanup_run["request"]["project_id"],
        "expected_run_owner": None,
        "parent_action_id": action,
    }
    assert sandbox_context(cleanup_run, parent_action_id=action) == {
        key: value for key, value in bound.items() if key != "run_id"
    }


def test_intent_snapshot_cancel_precedence_and_original_budget(cleanup_run):
    before = deepcopy(cleanup_run)
    intent = begin_cleanup_intent(cleanup_run, source="api", actor=fresh_actor("api"))
    assert cleanup_intent_unresolved(cleanup_run)
    assert cleanup_intent_matches(cleanup_run, intent)
    cleanup_run.update(requested_action="cancel", state="pausing")
    assert cleanup_intent_matches(cleanup_run, intent)
    assert finish_cleanup_intent(cleanup_run, intent, {"remaining": [], "errors": []})
    assert not cleanup_intent_unresolved(cleanup_run)
    assert cleanup_run["requested_action"] == "cancel"
    assert cleanup_run["first_started_at"] == before["first_started_at"]
    assert cleanup_run["usage"] == before["usage"]
    assert intent["state"] == "pending"  # caller snapshot is immutable by writes


@pytest.mark.parametrize("change", ["owner", "project", "prompt", "nonce", "version"])
def test_stale_receipt_cannot_overwrite_new_binding(cleanup_run, change):
    intent = begin_cleanup_intent(
        cleanup_run, source="worker", actor=fresh_actor("worker")
    )
    if change == "owner":
        cleanup_run["owner"] = str(uuid4())
    elif change in {"project", "prompt"}:
        cleanup_run["request"]["project_id" if change == "project" else "prompt"] = str(
            uuid4()
        )
    elif change == "nonce":
        cleanup_run["cleanup_intent"]["id"] = str(uuid4())
    else:
        cleanup_run["cleanup_sequence"] += 1
    before = deepcopy(cleanup_run)
    assert not cleanup_intent_matches(cleanup_run, intent)
    assert not finish_cleanup_intent(
        cleanup_run, intent, {"remaining": [], "errors": []}
    )
    assert cleanup_run == before


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_duplicate_intent_cannot_mint_replacement_or_replay(cleanup_run, state):
    begin_cleanup_intent(cleanup_run, source="api", actor=fresh_actor("api"))
    cleanup_run["cleanup_intent"]["state"] = state
    before = deepcopy(cleanup_run)
    with pytest.raises(ValueError, match="未完成"):
        begin_cleanup_intent(cleanup_run, source="api", actor=fresh_actor("api"))
    assert cleanup_run == before


def test_completed_intent_is_followed_by_distinct_next_version(cleanup_run):
    first = begin_cleanup_intent(cleanup_run, source="api", actor=fresh_actor("api"))
    assert finish_cleanup_intent(cleanup_run, first, {"remaining": [], "errors": []})
    second = begin_cleanup_intent(cleanup_run, source="api", actor=fresh_actor("api"))
    assert second["version"] == first["version"] + 1
    assert second["id"] != first["id"]
    assert not finish_cleanup_intent(
        cleanup_run, first, {"remaining": [], "errors": []}
    )


@pytest.mark.parametrize(
    "intent", [{}, {"state": "unknown"}, {"state": "pending"}, "broken"]
)
def test_malformed_or_unresolved_intent_refuses_resume(cleanup_run, intent):
    cleanup_run["cleanup_intent"] = intent
    assert cleanup_intent_unresolved(cleanup_run)


@pytest.mark.parametrize(
    "change",
    ["fields", "id", "schema", "version", "source", "request", "project", "owner"],
)
def test_damaged_complete_intent_cannot_authorize_resume_or_new_cleanup(
    cleanup_run, change
):
    actor = fresh_actor("api")
    intent = begin_cleanup_intent(cleanup_run, source="api", actor=actor)
    assert finish_cleanup_intent(cleanup_run, intent, {"remaining": [], "errors": []})
    if change == "fields":
        cleanup_run["cleanup_intent"] = {"state": "complete"}
    elif change == "request":
        cleanup_run["request"]["prompt"] = "changed immutable input"
    elif change == "project":
        cleanup_run["cleanup_intent"]["project_id"] = str(uuid4())
    else:
        field = "expected_run_owner" if change == "owner" else change
        cleanup_run["cleanup_intent"][field] = {
            "id": "invalid",
            "schema": True,
            "version": True,
            "source": "other",
            "owner": ["invalid"],
        }[change]
    before = deepcopy(cleanup_run)
    assert cleanup_intent_unresolved(cleanup_run)
    with pytest.raises(ValueError, match="未完成"):
        begin_cleanup_intent(cleanup_run, source="api", actor=actor)
    assert cleanup_run == before


def test_registry_record_unknown_and_id_survive_later_empty_ps():
    operation_id = str(uuid4())
    evidence = {
        "controller_record_unknown": True,
        "controller_operation_id": operation_id,
        "local_process_unknown": False,
        "daemon_request_unknown": False,
    }
    first = merge_cleanup({"remaining": [], "errors": [], "cleanup_evidence": evidence})
    latest = merge_cleanup({"remaining": [], "errors": []}, previous=first)
    assert controller_cleanup_unknown(latest)
    assert latest["controller_record_unknown"]
    assert evidence in latest["controller_process_evidence"]
    assert latest["cleanup_evidence"]["controller_operation_id"] == operation_id


def test_registry_error_cause_keeps_real_id_without_fabricated_process_receipt():
    operation_id = str(uuid4())
    cause = RegistryError("transition_commit_unknown", operation_id=operation_id)
    outer = RuntimeError("synthetic wrapper")
    outer.__cause__ = cause
    result = merge_cleanup({"remaining": [], "errors": []}, error=outer)
    assert controller_cleanup_unknown(result)
    assert result["cleanup_evidence"] == {
        "controller_record_unknown": True,
        "controller_operation_id": operation_id,
    }
    assert "pid" not in result["cleanup_evidence"]


def test_ordinary_error_does_not_invent_a_registry_identity():
    result = merge_cleanup(
        {"remaining": [], "errors": []}, error=ValueError("synthetic content failure")
    )
    assert not controller_cleanup_unknown(result)
    assert not result.get("controller_process_evidence")
