"""Keep unresolved local controller processes distinct from Docker absence."""

import hashlib
import json
from copy import deepcopy
from uuid import UUID, uuid4

PROCESS_UNKNOWN = "controller_process_unknown"
DAEMON_UNKNOWN = "controller_daemon_request_unknown"
RECORD_UNKNOWN = "controller_record_unknown"
MAX_PROCESS_RECEIPTS = 16


def controller_process_unknown(recovery: dict | None) -> bool:
    recovery = recovery or {}
    return bool(
        recovery.get(PROCESS_UNKNOWN)
        or PROCESS_UNKNOWN in recovery.get("errors", [])
        or (recovery.get("cleanup_evidence") or {}).get("local_process_unknown")
    )


def controller_cleanup_unknown(recovery: dict | None) -> bool:
    recovery = recovery or {}
    return bool(
        controller_process_unknown(recovery)
        or recovery.get(DAEMON_UNKNOWN)
        or DAEMON_UNKNOWN in recovery.get("errors", [])
        or (recovery.get("cleanup_evidence") or {}).get("daemon_request_unknown")
        or recovery.get(RECORD_UNKNOWN)
        or RECORD_UNKNOWN in recovery.get("errors", [])
        or (recovery.get("cleanup_evidence") or {}).get(RECORD_UNKNOWN)
    )


def merge_cleanup(result: dict, *, previous=None, error=None) -> dict:
    """A later empty ps cannot prove a prior CLI was reaped with pipe EOF.

    Keep distinct receipts, with a fixed inventory and an explicit overflow
    latch. The latest receipt is also retained for the caller's cleanup event.
    No ordinary container cleanup clears this latch.
    """
    result = deepcopy(result)
    if "cleanup_evidence" in result:
        result.setdefault(
            "cleanup_attempt_evidence", deepcopy(result["cleanup_evidence"])
        )
    receipts = []
    overflow = bool((previous or {}).get("controller_process_evidence_overflow"))

    def keep(receipt):
        nonlocal overflow
        if not receipt:
            return
        if receipt not in receipts:
            if len(receipts) >= MAX_PROCESS_RECEIPTS:
                overflow = True
                receipts[-1] = deepcopy(receipt)
            else:
                receipts.append(deepcopy(receipt))

    process_unknown = controller_process_unknown(
        previous
    ) or controller_process_unknown(result)
    daemon_unknown = bool(
        ((previous or {}).get(DAEMON_UNKNOWN))
        or DAEMON_UNKNOWN in (previous or {}).get("errors", [])
        or result.get(DAEMON_UNKNOWN)
        or DAEMON_UNKNOWN in result.get("errors", [])
    )
    record_unknown = bool(
        (previous or {}).get(RECORD_UNKNOWN)
        or RECORD_UNKNOWN in (previous or {}).get("errors", [])
        or ((previous or {}).get("cleanup_evidence") or {}).get(RECORD_UNKNOWN)
        or result.get(RECORD_UNKNOWN)
        or RECORD_UNKNOWN in result.get("errors", [])
        or (result.get("cleanup_evidence") or {}).get(RECORD_UNKNOWN)
    )
    if controller_cleanup_unknown(previous):
        for receipt in (previous or {}).get("controller_process_evidence", []):
            keep(receipt)
        keep((previous or {}).get("cleanup_evidence") or {})
    if controller_cleanup_unknown(result):
        keep(result.get("cleanup_evidence") or {})
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        receipt = getattr(error, "cleanup_evidence", None)
        from doonex.controller_operations import RegistryError

        if isinstance(error, RegistryError) and error.operation_id is not None:
            keep({RECORD_UNKNOWN: True, "controller_operation_id": error.operation_id})
        if isinstance(receipt, dict) and (
            receipt.get("local_process_unknown")
            or receipt.get("daemon_request_unknown")
            or receipt.get(RECORD_UNKNOWN)
        ):
            keep(receipt)
        error = error.__cause__ or error.__context__
    if receipts:
        process_unknown = process_unknown or any(
            receipt.get("local_process_unknown") for receipt in receipts
        )
        daemon_unknown = daemon_unknown or any(
            receipt.get("daemon_request_unknown") for receipt in receipts
        )
        record_unknown = record_unknown or any(
            receipt.get(RECORD_UNKNOWN) for receipt in receipts
        )
        result["cleanup_evidence"] = receipts[-1]
        result["controller_process_evidence"] = receipts
    if overflow:
        result["controller_process_evidence_overflow"] = True
    for marker, unknown in (
        (PROCESS_UNKNOWN, process_unknown),
        (DAEMON_UNKNOWN, daemon_unknown),
        (RECORD_UNKNOWN, record_unknown),
    ):
        if unknown:
            result[marker] = True
            if marker not in result.setdefault("errors", []):
                result["errors"].append(marker)
    return result


def cleanup_intent_unresolved(run):
    intent = run.get("cleanup_intent")
    if intent is None:
        return False
    if type(intent) is not dict or intent.get("state") != "complete":
        return True
    expected = {
        "schema",
        "id",
        "version",
        "source",
        "actor_owner",
        "run_id",
        "project_id",
        "expected_run_owner",
        "parent_action_id",
        "request_sha256",
        "state",
    }
    if set(intent) != expected:
        return True
    try:
        for name in ("id", "actor_owner", "run_id"):
            if type(intent[name]) is not str or str(UUID(intent[name])) != intent[name]:
                return True
        if intent["project_id"] is not None and (
            type(intent["project_id"]) is not str
            or str(UUID(intent["project_id"])) != intent["project_id"]
        ):
            return True
        owner = intent["expected_run_owner"]
        return not (
            type(intent["schema"]) is int
            and intent["schema"] == 1
            and type(intent["version"]) is int
            and intent["version"] > 0
            and type(run.get("cleanup_sequence")) is int
            and intent["version"] == run["cleanup_sequence"]
            and intent["source"] in {"api", "worker", "reconcile"}
            and intent["run_id"] == run["id"]
            and intent["project_id"] == run["request"].get("project_id")
            and intent["parent_action_id"] is None
            and (
                owner is None
                or (
                    type(owner) is str
                    and 1 <= len(owner) <= 128
                    and not any(ord(c) < 32 or ord(c) == 127 for c in owner)
                )
            )
            and type(intent["request_sha256"]) is str
            and intent["request_sha256"] == _request_digest(run)
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return True


def _request_digest(run):
    return hashlib.sha256(
        json.dumps(
            run["request"], sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def operation_context(run, *, parent_action_id=None):
    """Capture actual Run bindings; callers never infer a worker's old owner."""
    return {
        "run_id": run["id"],
        "project_id": run["request"].get("project_id"),
        "expected_run_owner": run.get("owner"),
        "parent_action_id": parent_action_id,
    }


def sandbox_context(run, *, parent_action_id=None):
    context = operation_context(run, parent_action_id=parent_action_id)
    context.pop("run_id")
    return context


def begin_cleanup_intent(run, *, source, actor):
    """Caller commits this nonce before releasing the Run for external cleanup."""
    if source not in {"api", "worker", "reconcile"}:
        raise ValueError("清理意图来源无效")
    actor.check_live()
    if cleanup_intent_unresolved(run):
        raise ValueError("已有未完成的清理意图，不能重放清理")
    version = run.get("cleanup_sequence", 0) + 1
    intent = {
        "schema": 1,
        "id": str(uuid4()),
        "version": version,
        "source": source,
        "actor_owner": actor.owner,
        **operation_context(run),
        "request_sha256": _request_digest(run),
        "state": "pending",
    }
    run["cleanup_sequence"] = version
    run["cleanup_intent"] = intent
    return deepcopy(intent)


def cleanup_intent_matches(run, intent):
    """An old receipt cannot overwrite a newer owner, request or stop intent."""
    return (
        run.get("cleanup_intent") == intent
        and run.get("cleanup_sequence") == intent["version"]
        and operation_context(run)
        == {key: intent[key] for key in operation_context(run)}
        and _request_digest(run) == intent["request_sha256"]
    )


def finish_cleanup_intent(run, intent, result):
    if not cleanup_intent_matches(run, intent):
        return False
    run["cleanup_intent"] = {
        **intent,
        "state": "unknown"
        if result.get("remaining") or result.get("errors")
        else "complete",
    }
    return True
