"""Pure, conservative Skill Arena statistics; never selects or persists a Skill.

The caller classifies attempts from verified controller evidence. Model text is
not an input to this module. Every supplied attempt stays in cost accounting,
including excluded environment failures and retries of the same planned slot.
"""

from __future__ import annotations

import math
from statistics import median

CLASSIFICATIONS = frozenset(
    {"passed", "failed", "environment_failure", "cancelled", "assisted", "pending"}
)
QUALITY_RESULTS = frozenset({"passed", "failed"})
USAGE_COUNTERS = ("calls", "tokens", "reserved_tokens", "unknown_calls")


def _number(value):
    return type(value) in (int, float) and 0 <= value <= 1e18 and math.isfinite(value)


def _identifier(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 256
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _validate(evaluation, attempts):
    """Refuse an ambiguous plan rather than inventing a comparison protocol."""
    try:
        tasks, arms = evaluation["tasks"], evaluation["arms"]
        threshold, repeats = evaluation["minimum_pass_rate"], evaluation["repeats"]
        valid = (
            isinstance(evaluation, dict)
            and isinstance(attempts, list)
            and evaluation["split"] in {"heldout", "tuning"}
            and all(
                _identifier(evaluation[key])
                for key in ("state", "category", "fingerprint")
            )
            and type(repeats) is int
            and 1 <= repeats <= 100
            and _number(threshold)
            and 0.8 <= threshold <= 1
            and isinstance(tasks, list)
            and 5 <= len(tasks) <= 100
            and all(
                isinstance(task, dict)
                and _identifier(task["id"])
                and type(task["critical"]) is bool
                for task in tasks
            )
            and len({task["id"] for task in tasks}) == len(tasks)
            and isinstance(arms, list)
            and 2 <= len(arms) <= 20
            and all(
                isinstance(arm, dict)
                and _identifier(arm["id"])
                and _identifier(arm["label"])
                for arm in arms
            )
            and len({arm["id"] for arm in arms}) == len(arms)
            and sum(arm["id"] == "baseline" for arm in arms) == 1
        )
        if not valid:
            raise ValueError
    except (TypeError, KeyError, ValueError):
        raise ValueError("Arena evaluation plan is invalid") from None


def _attempts(evaluation, attempts):
    task_ids = {task["id"] for task in evaluation["tasks"]}
    arm_ids = {arm["id"] for arm in evaluation["arms"]}
    issues, records, run_ids = set(), [], set()
    for attempt in attempts:
        errors = set()
        if not isinstance(attempt, dict):
            attempt = {}
            errors.add("invalid_attempt_fields")
        record = {
            key: attempt.get(key)
            for key in ("run_id", "task_id", "arm_id", "repeat", "classification")
        }
        if not _identifier(record["run_id"]):
            record["run_id"] = None
            errors.add("invalid_attempt_fields")
        elif record["run_id"] in run_ids:
            errors.add("duplicate_run_id")
        else:
            run_ids.add(record["run_id"])
        for key, allowed, issue in (
            ("task_id", task_ids, "unknown_task"),
            ("arm_id", arm_ids, "unknown_arm"),
        ):
            if not isinstance(record[key], str) or record[key] not in allowed:
                record[key] = None
                errors.add(issue)
        if (
            type(record["repeat"]) is not int
            or not 0 <= record["repeat"] < evaluation["repeats"]
        ):
            record["repeat"] = None
            errors.add("invalid_repeat")
        if (
            not isinstance(record["classification"], str)
            or record["classification"] not in CLASSIFICATIONS
        ):
            record["classification"] = "pending"
            errors.add("invalid_attempt_fields")
        for key in ("evidence_valid", "safety_violation", "regression_failed"):
            value = attempt.get(key)
            if type(value) is not bool:
                errors.add("invalid_attempt_fields")
            record[key] = value is True
        for key in ("duration_seconds", "queued_seconds"):
            value = attempt.get(key)
            if value is not None and not _number(value):
                errors.add("invalid_attempt_fields")
                value = None
            record[key] = value
        usage = attempt.get("usage")
        record["usage"] = {}
        if not isinstance(usage, dict):
            usage = {}
        for key in USAGE_COUNTERS:
            value = usage.get(key, 0)
            if type(value) is not int or not _number(value):
                errors.add("invalid_attempt_fields")
                value = 0
            record["usage"][key] = value
        cost = usage.get("cost_usd")
        if cost is not None and not _number(cost):
            errors.add("invalid_attempt_fields")
            cost = None
        record["usage"]["cost_usd"] = cost
        if "duplicate_run_id" in errors:
            # A duplicated record is not proof of a second billable request.
            record["usage"]["cost_usd"] = None
        record["reported_classification"] = record["classification"]
        if record["classification"] == "passed" and (
            errors
            or not record["evidence_valid"]
            or record["safety_violation"]
            or record["regression_failed"]
        ):
            record["classification"] = "failed"
        record["_qualified"] = (
            not errors
            and record["evidence_valid"]
            and record["classification"] in QUALITY_RESULTS
        )
        records.append(record)
        issues.update(errors)
    return records, sorted(issues)


def _counts(records, planned=0, missing=0):
    counts = {
        "planned": planned,
        "attempts": len(records),
        "completed": sum(item["classification"] != "pending" for item in records),
        "valid": sum(item["classification"] in QUALITY_RESULTS for item in records),
        **{
            key: sum(item["classification"] == key for item in records)
            for key in sorted(CLASSIFICATIONS)
        },
        "evidence_invalid": sum(
            not item["evidence_valid"] and item["classification"] != "pending"
            for item in records
        ),
        "safety_violations": sum(item["safety_violation"] for item in records),
        "regression_failures": sum(item["regression_failed"] for item in records),
        "missing_slots": missing,
    }
    return counts


def _stability(counts):
    if counts["passed"] and counts["failed"]:
        return "unstable"
    if counts["missing_slots"] or not counts["valid"] or counts["pending"]:
        return "incomplete"
    return "all_passed" if counts["passed"] else "all_failed"


def _usage(records):
    result = {
        key: sum(item["usage"][key] for item in records) for key in USAGE_COUNTERS
    }
    costs = [item["usage"]["cost_usd"] for item in records]
    known = result["unknown_calls"] == 0 and all(cost is not None for cost in costs)
    result["cost_usd"] = sum(costs) if known else None
    successes = sum(item["classification"] == "passed" for item in records)
    result["unit_success_cost_usd"] = (
        result["cost_usd"] / successes if known and successes else None
    )
    return result


def _median(values):
    values = [value for value in values if value is not None]
    return median(values) if values else None


def _timing(records):
    return {
        "successful_median_seconds": _median(
            item["duration_seconds"]
            for item in records
            if item["classification"] == "passed"
        ),
        "valid_median_seconds": _median(
            item["duration_seconds"]
            for item in records
            if item["classification"] in QUALITY_RESULTS
        ),
        "queued_median_seconds": _median(item["queued_seconds"] for item in records),
    }


def _arm(evaluation, arm, records):
    records = [item for item in records if item["arm_id"] == arm["id"]]
    tasks = []
    for task in evaluation["tasks"]:
        selected = [item for item in records if item["task_id"] == task["id"]]
        covered = {item["repeat"] for item in selected if item["_qualified"]}
        missing = sorted(set(range(evaluation["repeats"])) - covered)
        counts = _counts(selected, evaluation["repeats"], len(missing))
        tasks.append(
            {
                "task_id": task["id"],
                "critical": task["critical"],
                "counts": counts,
                "pass_rate": counts["passed"] / counts["valid"]
                if counts["valid"]
                else None,
                "stability": _stability(counts),
                "missing_repeats": missing,
                "attempts": [
                    {
                        key: value
                        for key, value in item.items()
                        if not key.startswith("_")
                    }
                    for item in selected
                ],
            }
        )
    counts = _counts(
        records,
        len(tasks) * evaluation["repeats"],
        sum(task["counts"]["missing_slots"] for task in tasks),
    )
    return {
        "id": arm["id"],
        "label": arm["label"],
        "counts": counts,
        "tasks": tasks,
        # A missing task cannot disappear from the equal-task denominator.
        "pass_rate": sum(task["pass_rate"] or 0 for task in tasks) / len(tasks)
        if counts["valid"]
        else None,
        "stability": _stability(counts),
        "usage": _usage(records),
        "timing": _timing(records),
    }


def _reasons(evaluation, result, arm):
    reasons = []
    if evaluation["split"] != "heldout":
        reasons.append("not_heldout")
    if evaluation["state"] != "complete":
        reasons.append("evaluation_not_complete")
    if not result["complete"]:
        reasons.append("batch_incomplete")
    if evaluation["repeats"] < 3:
        reasons.append("insufficient_repeats")
    for field in ("cancelled", "assisted", "pending"):
        if result["counts"][field]:
            reasons.append(field + "_attempts")
    if result["issues"]:
        reasons.append("invalid_attempt_records")
    if arm["counts"]["evidence_invalid"]:
        reasons.append("missing_evidence")
    if arm["counts"]["safety_violations"]:
        reasons.append("safety_violation")
    if any(
        sum(
            item["evidence_valid"] and item["classification"] in QUALITY_RESULTS
            for item in task["attempts"]
        )
        < 3
        for task in arm["tasks"]
    ):
        reasons.append("insufficient_valid_attempts")
    if any(not task["counts"]["passed"] for task in arm["tasks"]):
        reasons.append("task_without_success")
    if any(
        task["critical"]
        and (task["counts"]["failed"] or task["counts"]["missing_slots"])
        for task in arm["tasks"]
    ):
        reasons.append("critical_task_failed")
    if arm["pass_rate"] is None or arm["pass_rate"] < evaluation["minimum_pass_rate"]:
        reasons.append("below_threshold")
    return reasons


def summarize(evaluation: dict, attempts: list[dict]) -> dict:
    """Return metrics and eligible candidate IDs, without choosing a recommendation.

    ``repeat`` is zero based. A replacement may cover an environment-failed slot;
    the original attempt and its cost remain. ``valid`` is the conservative
    quality denominator; evidence-invalid claimed passes become failures but
    never cover a planned slot or satisfy promotion sample counts.
    """
    _validate(evaluation, attempts)
    records, issues = _attempts(evaluation, attempts)
    arms = [_arm(evaluation, arm, records) for arm in evaluation["arms"]]
    planned = len(arms) * len(evaluation["tasks"]) * evaluation["repeats"]
    counts = _counts(
        records, planned, sum(arm["counts"]["missing_slots"] for arm in arms)
    )
    result = {
        **{
            key: evaluation[key]
            for key in ("state", "split", "category", "fingerprint")
        },
        "planned_attempts": planned,
        "counts": counts,
        "complete": not counts["missing_slots"]
        and not any(counts[key] for key in ("cancelled", "assisted", "pending"))
        and not issues,
        "issues": issues,
        "arms": arms,
        "usage": _usage(records),
        "timing": _timing(records),
        "recommendations": {"reliable": [], "cost": [], "speed": []},
    }
    baseline = next(arm for arm in arms if arm["id"] == "baseline")
    for arm in arms:
        reasons = _reasons(evaluation, result, arm)
        arm["eligibility"] = {"eligible": not reasons, "reasons": reasons}
    for arm in arms:
        if arm["id"] == "baseline":
            continue
        reasons = arm["eligibility"]["reasons"]
        if not baseline["eligibility"]["eligible"]:
            reasons.append("baseline_ineligible")
        if baseline["pass_rate"] is not None and (
            arm["pass_rate"] is None or arm["pass_rate"] < baseline["pass_rate"]
        ):
            reasons.append("below_baseline")
        arm["eligibility"]["eligible"] = not reasons
    eligible = [
        arm
        for arm in arms
        if arm["id"] != "baseline" and arm["eligibility"]["eligible"]
    ]
    if eligible:
        quality = max(arm["pass_rate"] for arm in eligible)
        reliable = [arm for arm in eligible if arm["pass_rate"] == quality]
        stable = [arm for arm in reliable if arm["stability"] == "all_passed"]
        result["recommendations"]["reliable"] = [
            arm["id"] for arm in (stable or reliable)
        ]
        # Unknown money/timing cannot establish the cheapest/fastest candidate.
        costs = [arm["usage"]["unit_success_cost_usd"] for arm in eligible]
        if all(cost is not None for cost in costs):
            result["recommendations"]["cost"] = [
                arm["id"]
                for arm in eligible
                if arm["usage"]["unit_success_cost_usd"] == min(costs)
            ]
        durations_complete = all(
            item["duration_seconds"] is not None
            for item in records
            if item["arm_id"] in {arm["id"] for arm in eligible}
            and item["classification"] == "passed"
        )
        speeds = [arm["timing"]["successful_median_seconds"] for arm in eligible]
        if durations_complete and all(speed is not None for speed in speeds):
            result["recommendations"]["speed"] = [
                arm["id"]
                for arm in eligible
                if arm["timing"]["successful_median_seconds"] == min(speeds)
            ]
    return result
