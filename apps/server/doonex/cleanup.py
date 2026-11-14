"""Keep unresolved local controller processes distinct from Docker absence."""

from copy import deepcopy

PROCESS_UNKNOWN = "controller_process_unknown"
DAEMON_UNKNOWN = "controller_daemon_request_unknown"
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
        if isinstance(receipt, dict) and (
            receipt.get("local_process_unknown")
            or receipt.get("daemon_request_unknown")
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
        result["cleanup_evidence"] = receipts[-1]
        result["controller_process_evidence"] = receipts
    if overflow:
        result["controller_process_evidence_overflow"] = True
    for marker, unknown in (
        (PROCESS_UNKNOWN, process_unknown),
        (DAEMON_UNKNOWN, daemon_unknown),
    ):
        if unknown:
            result[marker] = True
            if marker not in result.setdefault("errors", []):
                result["errors"].append(marker)
    return result
