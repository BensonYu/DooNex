import json
from copy import deepcopy

import pytest
from doonex.arena_metrics import summarize


@pytest.fixture
def plan():
    return {
        "state": "complete",
        "split": "heldout",
        "category": "bugfix",
        "tasks": [
            {"id": f"task-{index}", "critical": index == 0} for index in range(5)
        ],
        "arms": [
            {"id": "baseline", "label": "No extra Skill"},
            {"id": "version-a", "label": "Candidate A"},
        ],
        "repeats": 3,
        "minimum_pass_rate": 0.8,
        "fingerprint": "frozen-model-tool-environment-taskset-authorization",
    }


def attempt(arm_id="version-a", task_id="task-0", repeat=0, **changes):
    return {
        "run_id": f"{arm_id}-{task_id}-{repeat}",
        "arm_id": arm_id,
        "task_id": task_id,
        "repeat": repeat,
        "classification": "passed",
        "evidence_valid": True,
        "safety_violation": False,
        "regression_failed": False,
        "duration_seconds": 10.0,
        "queued_seconds": 2.0,
        "usage": {
            "calls": 2,
            "tokens": 100,
            "reserved_tokens": 0,
            "unknown_calls": 0,
            "cost_usd": 0.2,
        },
        **changes,
    }


def completed(plan):
    return [
        attempt(arm["id"], task["id"], repeat)
        for arm in plan["arms"]
        for task in plan["tasks"]
        for repeat in range(plan["repeats"])
    ]


def arm(result, arm_id="version-a"):
    return next(item for item in result["arms"] if item["id"] == arm_id)


def record(records, arm_id="version-a", task_id="task-0", repeat=0):
    return next(
        item
        for item in records
        if (item["arm_id"], item["task_id"], item["repeat"])
        == (arm_id, task_id, repeat)
    )


def test_complete_heldout_metrics_preserve_inputs_and_return_candidates(plan):
    records = completed(plan)
    before = deepcopy((plan, records))
    result = summarize(plan, records)
    assert (plan, records) == before
    assert result["complete"] is True
    assert result["planned_attempts"] == 30
    assert result["counts"]["valid"] == result["counts"]["passed"] == 30
    assert result["fingerprint"] == plan["fingerprint"]
    assert arm(result)["pass_rate"] == 1
    assert arm(result)["stability"] == "all_passed"
    assert arm(result)["eligibility"] == {"eligible": True, "reasons": []}
    assert arm(result)["usage"]["cost_usd"] == pytest.approx(3)
    assert arm(result)["usage"]["unit_success_cost_usd"] == pytest.approx(0.2)
    assert arm(result)["timing"] == {
        "successful_median_seconds": 10,
        "valid_median_seconds": 10,
        "queued_median_seconds": 2,
    }
    assert result["recommendations"] == {
        "reliable": ["version-a"],
        "cost": ["version-a"],
        "speed": ["version-a"],
    }
    json.dumps(result, allow_nan=False)


def test_each_task_has_equal_weight_and_failed_retries_stay_in_denominator(plan):
    records = completed(plan)
    for item in records:
        if item["arm_id"] == "version-a" and item["task_id"] == "task-4":
            item["classification"] = "failed"
    for index in range(20):
        records.append(attempt(run_id=f"extra-success-{index}"))
    records.append(attempt(run_id="failed-retry", classification="failed"))
    result = arm(summarize(plan, records))
    assert result["counts"]["valid"] == 36
    assert result["counts"]["failed"] == 4
    assert result["pass_rate"] == pytest.approx((23 / 24 + 1 + 1 + 1 + 0) / 5)
    assert result["pass_rate"] != pytest.approx(32 / 36)
    assert result["stability"] == "unstable"
    assert "task_without_success" in result["eligibility"]["reasons"]
    assert result["usage"]["unit_success_cost_usd"] == pytest.approx(7.2 / 32)


def test_environment_failure_needs_same_slot_replacement_and_keeps_all_cost(plan):
    records = completed(plan)
    failed = record(records)
    failed["classification"] = "environment_failure"
    before = summarize(plan, records)
    assert before["counts"]["valid"] == 29
    assert before["counts"]["environment_failure"] == 1
    assert arm(before)["tasks"][0]["missing_repeats"] == [0]
    assert before["complete"] is False
    records.append(attempt(run_id="replacement"))
    after = summarize(plan, records)
    assert after["complete"] is True
    assert after["counts"]["attempts"] == 31
    assert after["counts"]["valid"] == 30
    assert arm(after)["usage"]["cost_usd"] == pytest.approx(3.2)
    assert arm(after)["usage"]["unit_success_cost_usd"] == pytest.approx(3.2 / 15)
    assert after["recommendations"]["reliable"] == ["version-a"]


def test_missing_task_or_repeated_one_slot_never_completes_original_plan(plan):
    records = completed(plan)
    records.remove(record(records, task_id="task-4", repeat=2))
    records.extend(attempt(run_id=f"oversample-{index}") for index in range(3))
    result = summarize(plan, records)
    assert result["complete"] is False
    assert result["counts"]["missing_slots"] == 1
    assert arm(result)["tasks"][4]["missing_repeats"] == [2]
    assert result["recommendations"] == {"reliable": [], "cost": [], "speed": []}
    records = [item for item in records if item["task_id"] != "task-4"]
    result = summarize(plan, records)
    assert arm(result)["pass_rate"] == 0.8
    assert arm(result)["tasks"][4]["pass_rate"] is None


@pytest.mark.parametrize("classification", ["cancelled", "assisted", "pending"])
def test_cancel_assist_or_pending_blocks_new_recommendations_globally(
    plan, classification
):
    records = completed(plan)
    records.append(attempt(run_id="excluded", classification=classification))
    result = summarize(plan, records)
    assert result["counts"][classification] == 1
    assert result["counts"]["valid"] == 30
    assert result["complete"] is False
    assert classification + "_attempts" in arm(result)["eligibility"]["reasons"]
    assert arm(result)["usage"]["cost_usd"] == pytest.approx(3.2)
    assert not any(result["recommendations"].values())


def test_missing_evidence_is_not_success_or_promotion_sample(plan):
    records = completed(plan)
    record(records)["evidence_valid"] = False
    result = summarize(plan, records)
    candidate = arm(result)
    assert candidate["counts"]["passed"] == 14
    assert candidate["counts"]["failed"] == 1
    assert candidate["counts"]["evidence_invalid"] == 1
    assert candidate["pass_rate"] == pytest.approx(14 / 15)
    assert candidate["tasks"][0]["missing_repeats"] == [0]
    assert {"missing_evidence", "insufficient_valid_attempts"} <= set(
        candidate["eligibility"]["reasons"]
    )
    records.append(attempt(run_id="valid-replacement"))
    after = summarize(plan, records)
    assert after["complete"] is True
    assert "missing_evidence" in arm(after)["eligibility"]["reasons"]
    assert not any(after["recommendations"].values())


def test_safety_bans_corresponding_version_without_banning_other_candidate(plan):
    plan["arms"].append({"id": "version-b", "label": "Candidate B"})
    records = completed(plan)
    record(records)["safety_violation"] = True
    result = summarize(plan, records)
    assert result["complete"] is True
    assert "safety_violation" in arm(result)["eligibility"]["reasons"]
    assert arm(result, "version-b")["eligibility"]["eligible"] is True
    assert result["recommendations"]["reliable"] == ["version-b"]
    record(records, arm_id="baseline")["safety_violation"] = True
    result = summarize(plan, records)
    assert "baseline_ineligible" in arm(result, "version-b")["eligibility"]["reasons"]
    assert not any(result["recommendations"].values())


def test_regression_failure_never_counts_as_pass_and_critical_task_must_all_pass(plan):
    records = completed(plan)
    for arm_id in ("baseline", "version-a"):
        record(records, arm_id=arm_id, task_id="task-1")["regression_failed"] = True
    result = summarize(plan, records)
    assert arm(result)["counts"]["regression_failures"] == 1
    assert arm(result)["stability"] == "unstable"
    assert arm(result)["eligibility"]["eligible"] is True
    record(records)["classification"] = "failed"
    result = summarize(plan, records)
    assert "critical_task_failed" in arm(result)["eligibility"]["reasons"]


def test_threshold_baseline_and_at_least_one_success_per_task_are_hard_gates(plan):
    records = completed(plan)
    record(records, task_id="task-1")["classification"] = "failed"
    result = summarize(plan, records)
    assert "below_baseline" in arm(result)["eligibility"]["reasons"]
    plan["minimum_pass_rate"] = 0.95
    result = summarize(plan, records)
    assert "below_threshold" in arm(result)["eligibility"]["reasons"]
    for item in records:
        if item["task_id"] in {"task-3", "task-4"}:
            item["classification"] = "failed"
    result = summarize(plan, records)
    assert "task_without_success" in arm(result)["eligibility"]["reasons"]
    assert "baseline_ineligible" in arm(result)["eligibility"]["reasons"]


def test_exact_quality_threshold_and_zero_cost_are_preserved(plan):
    records = completed(plan)
    for arm_id in ("baseline", "version-a"):
        for task_id, repeat in (("task-1", 0), ("task-1", 1), ("task-2", 0)):
            record(records, arm_id, task_id, repeat)["classification"] = "failed"
    for item in records:
        item["usage"]["cost_usd"] = 0
        item["duration_seconds"] = 0
        item["queued_seconds"] = None
    result = summarize(plan, records)
    assert arm(result)["pass_rate"] == 0.8
    assert arm(result)["eligibility"]["eligible"] is True
    assert arm(result)["stability"] == "unstable"
    assert arm(result)["usage"]["unit_success_cost_usd"] == 0
    assert arm(result)["timing"]["successful_median_seconds"] == 0
    assert arm(result)["timing"]["queued_median_seconds"] is None
    assert all(result["recommendations"].values())


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"split": "tuning"}, "not_heldout"),
        ({"state": "running"}, "evaluation_not_complete"),
        ({"repeats": 1}, "insufficient_repeats"),
    ],
)
def test_tuning_first_screen_and_incomplete_state_only_report_metrics(
    plan, change, reason
):
    plan.update(change)
    result = summarize(plan, completed(plan))
    assert result["complete"] is True
    assert arm(result)["pass_rate"] == 1
    assert reason in arm(result)["eligibility"]["reasons"]
    assert not any(result["recommendations"].values())


@pytest.mark.parametrize("unknown", ["cost", "unknown_calls", "missing_usage"])
def test_unknown_money_keeps_cost_slot_empty_without_using_tokens(plan, unknown):
    plan["arms"].append({"id": "version-b", "label": "Candidate B"})
    records = completed(plan)
    selected = record(records)
    if unknown == "cost":
        selected["usage"]["cost_usd"] = None
    elif unknown == "unknown_calls":
        selected["usage"]["unknown_calls"] = 1
        selected["usage"]["reserved_tokens"] = 8000
    else:
        del selected["usage"]
    result = summarize(plan, records)
    assert arm(result)["usage"]["cost_usd"] is None
    assert arm(result)["usage"]["unit_success_cost_usd"] is None
    assert result["usage"]["cost_usd"] is None
    assert result["recommendations"]["cost"] == []
    assert result["recommendations"]["reliable"] == ["version-a", "version-b"]


def test_all_failures_and_zero_success_have_no_unit_cost_or_success_duration(plan):
    records = completed(plan)
    for item in records:
        item["classification"] = "failed"
    result = arm(summarize(plan, records))
    assert result["usage"]["cost_usd"] == pytest.approx(3)
    assert result["usage"]["unit_success_cost_usd"] is None
    assert result["timing"]["successful_median_seconds"] is None
    assert result["stability"] == "all_failed"
    assert result["pass_rate"] == 0
    empty = summarize(plan, [])
    assert arm(empty)["pass_rate"] is None
    assert arm(empty)["stability"] == "incomplete"


def test_parallel_positions_keep_ties_and_compare_usd_and_success_medians(plan):
    plan["arms"].append({"id": "version-b", "label": "Candidate B"})
    records = completed(plan)
    for item in records:
        if item["arm_id"] == "version-b":
            item["usage"]["cost_usd"] = 0.1
            item["usage"]["tokens"] = 10000
            item["duration_seconds"] = 20
    candidate_a = [item for item in records if item["arm_id"] == "version-a"]
    candidate_a[0]["duration_seconds"] = 5000
    candidate_a[0]["queued_seconds"] = 10000
    result = summarize(plan, records)
    assert result["recommendations"] == {
        "reliable": ["version-a", "version-b"],
        "cost": ["version-b"],
        "speed": ["version-a"],
    }
    assert arm(result)["timing"]["successful_median_seconds"] == 10
    record(records)["duration_seconds"] = None
    assert summarize(plan, records)["recommendations"]["speed"] == []


@pytest.mark.parametrize(
    ("change", "issue"),
    [
        ({"run_id": "baseline-task-0-0"}, "duplicate_run_id"),
        ({"task_id": "unplanned"}, "unknown_task"),
        ({"arm_id": "unplanned"}, "unknown_arm"),
        ({"repeat": 3}, "invalid_repeat"),
        ({"repeat": True}, "invalid_repeat"),
        ({"classification": "model_says_passed"}, "invalid_attempt_fields"),
        ({"evidence_valid": "true"}, "invalid_attempt_fields"),
        ({"duration_seconds": float("nan")}, "invalid_attempt_fields"),
    ],
)
def test_tampered_attempts_are_visible_and_never_produce_recommendations(
    plan, change, issue
):
    records = completed(plan)
    record(records).update(change)
    result = summarize(plan, records)
    assert issue in result["issues"]
    assert result["counts"]["attempts"] == 30
    assert "invalid_attempt_records" in arm(result)["eligibility"]["reasons"]
    assert not any(result["recommendations"].values())
    json.dumps(result, allow_nan=False)


def test_orphan_attempt_cost_is_preserved_in_batch_total(plan):
    records = completed(plan)
    records.append(attempt(arm_id="unknown", run_id="orphan"))
    result = summarize(plan, records)
    assert result["usage"]["cost_usd"] == pytest.approx(6.2)
    assert sum(item["usage"]["cost_usd"] for item in result["arms"]) == pytest.approx(6)
    assert result["counts"]["attempts"] == 31


@pytest.mark.parametrize(
    "change",
    [
        {"minimum_pass_rate": 0.79},
        {"minimum_pass_rate": float("nan")},
        {"minimum_pass_rate": True},
        {"repeats": 0},
        {"repeats": True},
        {"split": "mixed"},
        {"tasks": [{"id": "only", "critical": False}]},
        {"tasks": [{"id": "duplicate", "critical": False}] * 5},
        {"arms": [{"id": "version-a", "label": "No baseline"}]},
        {"fingerprint": ""},
    ],
)
def test_ambiguous_or_below_protocol_plans_are_rejected(plan, change):
    plan.update(change)
    with pytest.raises(ValueError, match="Arena evaluation plan is invalid"):
        summarize(plan, [])


def test_missing_attempt_fields_and_non_mapping_rows_fail_closed(plan):
    records = completed(plan)
    del record(records)["evidence_valid"]
    records.append("model report")
    result = summarize(plan, records)
    assert "invalid_attempt_fields" in result["issues"]
    assert result["counts"]["evidence_invalid"] == 1
    assert result["counts"]["pending"] == 1
    assert result["usage"]["cost_usd"] is None
    assert not any(result["recommendations"].values())
