"""Keep unresolved local controller processes distinct from Docker absence."""

from copy import deepcopy

PROCESS_UNKNOWN = "controller_process_unknown"
MAX_PROCESS_RECEIPTS = 16


def controller_process_unknown(recovery: dict | None) -> bool:
    recovery = recovery or {}
    return bool(
        recovery.get(PROCESS_UNKNOWN)
        or PROCESS_UNKNOWN in recovery.get("errors", [])
        or (recovery.get("cleanup_evidence") or {}).get("local_process_unknown")
    )


def merge_cleanup(result: dict, *, previous=None, error=None) -> dict:
    """A later empty ps cannot prove a prior CLI was reaped with pipe EOF.

    Keep distinct receipts, with a fixed inventory and an explicit overflow
    latch. The latest receipt is also retained for the caller's cleanup event.
    No ordinary container cleanup clears this latch.
    """
    result = deepcopy(result)
    if "cleanup_evidence" in result:
        result.setdefault("cleanup_attempt_evidence", deepcopy(result["cleanup_evidence"]))
    receipts = []
    overflow = bool((previous or {}).get("controller_process_evidence_overflow"))

    def keep(receipt):
        nonlocal overflow
        if receipt not in receipts:
            if len(receipts) >= MAX_PROCESS_RECEIPTS:
                overflow = True
                receipts[-1] = deepcopy(receipt)
            else:
                receipts.append(deepcopy(receipt))

    if controller_process_unknown(previous):
        for receipt in (previous or {}).get("controller_process_evidence", []):
            keep(receipt)
        keep((previous or {}).get("cleanup_evidence") or {})
    if controller_process_unknown(result):
        keep(result.get("cleanup_evidence") or {})
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        receipt = getattr(error, "cleanup_evidence", None)
        if isinstance(receipt, dict) and receipt.get("local_process_unknown"):
            keep(receipt)
        error = error.__cause__ or error.__context__
    if receipts:
        result[PROCESS_UNKNOWN] = True
        result["cleanup_evidence"] = receipts[-1]
        result["controller_process_evidence"] = receipts
        if overflow:
            result["controller_process_evidence_overflow"] = True
        if PROCESS_UNKNOWN not in result.setdefault("errors", []):
            result["errors"].append(PROCESS_UNKNOWN)
    return result
