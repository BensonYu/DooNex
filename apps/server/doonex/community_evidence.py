"""Explicit public Arena snapshots and controller-bound usage registrations.

Private source inspection only occurs on maintainer actions. Public requests read
strict, frozen projections and community publication state, never Arena or Runs.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import Field, field_validator

from doonex.arena import TERMINAL_RUNS, Arena
from doonex.arena_metrics import summarize
from doonex.community import Community, CommunityError, identifier, require, revision
from doonex.discovery import digest
from doonex.evidence import Evidence
from doonex.models import StrictModel
from doonex.store import now
from doonex.worker import get_workspace, run_directory

COMMUNITY_EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS community_evidence_limits (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS community_evidence_days (day date PRIMARY KEY, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS community_evidence (id uuid PRIMARY KEY, release_id text NOT NULL REFERENCES community_releases(id), state text NOT NULL CHECK(state IN ('preparing','prepared','failed','published','withdrawn')), data jsonb NOT NULL, snapshot jsonb);
CREATE TABLE IF NOT EXISTS community_verified_usage (id uuid PRIMARY KEY, release_id text NOT NULL REFERENCES community_releases(id), run_id uuid NOT NULL REFERENCES runs(id), data jsonb NOT NULL, fact jsonb NOT NULL, UNIQUE(release_id,run_id));
"""
EVIDENCE_LIMITS = {
    "preparations": {"daily": 20, "total": 1000},
    "publications": {"daily": 20, "total": 1000},
    "registrations": {"daily": 100, "total": 10000},
}
HASH = r"^[a-f0-9]{64}$"
POSITIONS = ("reliable", "cost", "speed")
COUNTERS = ("calls", "tokens", "reserved_tokens", "unknown_calls")
MONEY = (
    "cost_usd",
    "reserved_cost_usd",
    "metered_cost_usd",
    "reserved_metered_cost_usd",
)


class PrepareEvidenceInput(StrictModel):
    release_id: str = Field(pattern=HASH)
    evaluation_id: UUID
    arm_id: str = Field(pattern=HASH)
    expires_at: datetime


class ConfirmationInput(StrictModel):
    @field_validator("*", mode="before")
    @classmethod
    def explicit_confirmation(cls, value, info):
        if info.field_name.startswith("confirm_") and value is not True:
            raise ValueError("community_explicit_confirmation_required")
        return value


class PublishEvidenceInput(ConfirmationInput):
    expected_revision: str = Field(pattern=HASH)
    preview_sha256: str = Field(pattern=HASH)
    confirm_public_fields: Literal[True]
    confirm_publish: Literal[True]


class WithdrawEvidenceInput(StrictModel):
    expected_revision: str = Field(pattern=HASH)


class RegisterUsageInput(ConfirmationInput):
    release_id: str = Field(pattern=HASH)
    run_id: UUID
    distribution_id: UUID | None = None
    confirm_register: Literal[True]


class Counts(StrictModel):
    planned: int = Field(ge=0, strict=True)
    attempts: int = Field(ge=0, strict=True)
    completed: int = Field(ge=0, strict=True)
    valid: int = Field(ge=0, strict=True)
    passed: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)
    environment_failure: int = Field(ge=0, strict=True)
    cancelled: int = Field(ge=0, strict=True)
    assisted: int = Field(ge=0, strict=True)
    pending: int = Field(ge=0, strict=True)
    evidence_invalid: int = Field(ge=0, strict=True)
    safety_violations: int = Field(ge=0, strict=True)
    regression_failures: int = Field(ge=0, strict=True)
    missing_slots: int = Field(ge=0, strict=True)


class PublicUsage(StrictModel):
    calls: int = Field(ge=0, strict=True)
    tokens: int = Field(ge=0, strict=True)
    reserved_tokens: int = Field(ge=0, strict=True)
    unknown_calls: int = Field(ge=0, strict=True)
    cost_usd: float | None = Field(ge=0)
    unit_success_cost_usd: float | None = Field(ge=0)


class PublicTiming(StrictModel):
    successful_median_seconds: float | None = Field(ge=0)
    valid_median_seconds: float | None = Field(ge=0)
    queued_median_seconds: float | None = Field(ge=0)


class PublicRecommendations(StrictModel):
    reliable: bool = Field(strict=True)
    cost: bool = Field(strict=True)
    speed: bool = Field(strict=True)


class PublicEvidence(StrictModel):
    schema_version: Literal[1]
    source: Literal["controller_verified"]
    release_id: str = Field(pattern=HASH)
    content_digest: str = Field(pattern=HASH)
    category: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=200)
    model_fingerprint: str = Field(pattern=HASH)
    taskset_fingerprint: str = Field(pattern=HASH)
    environment_fingerprint: str = Field(pattern=HASH)
    budget_fingerprint: str = Field(pattern=HASH)
    condition_fingerprint: str = Field(pattern=HASH)
    task_count: int = Field(ge=5, le=100, strict=True)
    repeats: int = Field(ge=1, le=100, strict=True)
    minimum_pass_rate: float = Field(ge=0.8, le=1)
    pass_rate: float | None = Field(ge=0, le=1)
    stability: Literal["all_passed", "all_failed", "unstable", "incomplete"]
    counts: Counts
    batch_counts: Counts
    usage: PublicUsage
    timing: PublicTiming
    successful_timing_complete: bool = Field(strict=True)
    eligible: bool = Field(strict=True)
    recommendations: PublicRecommendations
    verified_at: datetime
    expires_at: datetime

    @field_validator("category", "model_id")
    @classmethod
    def public_label(cls, value):
        # No arbitrary descriptions, URI, account annotation or control text.
        # An unsafe necessary label prevents publication; it is never redacted
        # into a misleading model/category identity.
        if not re.fullmatch(r"[\w][\w .:/+-]{0,199}", value) or "://" in value:
            raise ValueError("community_evidence_public_label_unsafe")
        return value


class UsageFact(StrictModel):
    source: Literal["controller_registration"]
    registered_at: datetime
    outcome: Literal[
        "verified_success",
        "verified_failure",
        "verified_unchecked",
        "failed",
        "cancelled",
        "budget_exceeded",
        "evidence_invalid",
    ]
    controller_verified: bool = Field(strict=True)
    successful_acceptance: bool = Field(strict=True)
    unknown_calls: int = Field(ge=0, strict=True)
    amount_usd: None


def _fields(value, path=""):
    """Enumerate every public leaf, including null/false, for explicit preview."""
    result = []
    for key, item in value.items():
        name = f"{path}.{key}" if path else key
        if isinstance(item, dict):
            result.extend(_fields(item, name))
        else:
            result.append({"path": name, "value": item})
    return result


def _safe_public(value):
    return PublicEvidence.model_validate(value).model_dump(mode="json")


def _ledger_matches(parent, attempts):
    """Keep unknown money unknown; reject disagreement in every ledger field."""
    try:
        for key in COUNTERS + MONEY:
            values = [attempt["usage"][key] for attempt in attempts]
            value = parent[key]
            if any(item is None for item in values):
                if value is not None:
                    return False
            elif key in COUNTERS:
                if type(value) is not int or value != sum(values):
                    return False
            elif not (
                type(value) in (int, float)
                and math.isfinite(value)
                and math.isclose(value, sum(values), rel_tol=1e-9, abs_tol=1e-9)
            ):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


class CommunityEvidence:
    def __init__(self, settings, store, *, community=None, arena=None):
        self.settings, self.store = settings, store
        self.community = community or Community(settings, store)
        self.arena = arena or Arena(settings, store)

    def _gate(self, c):
        value = self.community._settings(c, lock=True)
        require(value["enabled"], "community_disabled", 404)

    def _consume(self, c, kind):
        self._gate(c)
        initial = {
            "limits": EVIDENCE_LIMITS,
            "totals": {k: 0 for k in EVIDENCE_LIMITS},
        }
        c.execute(
            "INSERT INTO community_evidence_limits VALUES(true,%s) ON CONFLICT DO NOTHING",
            (Jsonb(initial),),
        )
        value = c.execute(
            "SELECT data FROM community_evidence_limits WHERE singleton FOR UPDATE"
        ).fetchone()["data"]
        day = datetime.now(UTC).date()
        c.execute(
            "INSERT INTO community_evidence_days VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (
                day,
                Jsonb(
                    {
                        "limits": value["limits"],
                        "counts": {k: 0 for k in EVIDENCE_LIMITS},
                    }
                ),
            ),
        )
        daily = c.execute(
            "SELECT data FROM community_evidence_days WHERE day=%s FOR UPDATE", (day,)
        ).fetchone()["data"]
        require(
            daily["counts"][kind] < daily["limits"][kind]["daily"]
            and value["totals"][kind] < value["limits"][kind]["total"],
            "community_evidence_capacity_exhausted",
            429,
        )
        daily["counts"][kind] += 1
        value["totals"][kind] += 1
        c.execute(
            "UPDATE community_evidence_limits SET data=%s WHERE singleton",
            (Jsonb(value),),
        )
        c.execute(
            "UPDATE community_evidence_days SET data=%s WHERE day=%s",
            (Jsonb(daily), day),
        )

    @staticmethod
    def _save(c, value):
        value["revision"] = revision(value)
        c.execute(
            "UPDATE community_evidence SET state=%s,data=%s WHERE id=%s",
            (value["state"], Jsonb(value), value["id"]),
        )

    def _get(self, c, evidence_id, *, lock=False):
        row = c.execute(
            "SELECT data FROM community_evidence WHERE id=%s"
            + (" FOR UPDATE" if lock else ""),
            (identifier(evidence_id),),
        ).fetchone()
        require(row is not None, "community_evidence_not_found", 404)
        value = row["data"]
        require(
            value.get("revision") == revision(value),
            "community_evidence_record_changed",
        )
        if value.get("public") is not None:
            public = _safe_public(value["public"])
            require(
                digest(public) == value["preview_sha256"],
                "community_evidence_preview_changed",
            )
        return value

    @staticmethod
    def _admin(value):
        # No raw private source in an API DTO; exact original references remain
        # available to the authenticated maintainer through the private tools.
        return {
            key: value[key]
            for key in (
                "id",
                "release_id",
                "evaluation_id",
                "arm_id",
                "state",
                "created_at",
                "public",
                "preview_sha256",
                "private_binding_digest",
                "published_at",
                "withdrawn_at",
                "error",
                "revision",
            )
        } | {"public_fields": _fields(value["public"]) if value["public"] else []}

    def list(self):
        with self.store.connect() as c:
            ids = [
                str(r["id"])
                for r in c.execute("SELECT id FROM community_evidence ORDER BY id")
            ]
            return [self._admin(self._get(c, item)) for item in ids]

    def _source(self, release_id, evaluation_id, arm_id):
        release = self.community.release_binding(release_id)
        value = self.arena.get(evaluation_id)
        binding = value["skills"].get(arm_id)
        require(
            binding
            and binding["version_id"] == arm_id
            and binding["content_digest"] == release["content_digest"],
            "community_evidence_skill_mismatch",
        )
        # This validates exact bytes, not equality of unrelated release/library IDs.
        self.arena.library.skill_context(binding)
        attempts, runs = [], []
        for entry in value["attempts"]:
            run = self.store.get(entry["run_id"])
            request = run["request"]
            original = request.get("arena") or {}
            require(
                original.get("evaluation_id") == value["id"]
                and original.get("taskset_id") == value["taskset_id"]
                and original.get("report_sha256") == value["taskset_report_sha256"]
                and original.get("fingerprint") == value["fingerprint"]
                and all(
                    original.get(key) == entry[key]
                    for key in ("arm_id", "task_id", "repeat")
                )
                and request.get("skill") == value["skills"].get(entry["arm_id"])
                and request["model"] == value["model"]
                and request["budget"] == value["run_budget"]
                and not request.get("generation"),
                "community_evidence_original_run_binding_changed",
            )
            attempt = self.arena.attempt(entry)
            require(
                digest(run) == digest(self.store.get(entry["run_id"])),
                "community_evidence_source_changed",
                409,
            )
            attempts.append(attempt)
            runs.append(run)
        require(
            digest(value) == digest(self.arena.get(evaluation_id)),
            "community_evidence_source_changed",
            409,
        )
        metrics = summarize(value, attempts)
        arm = next(item for item in metrics["arms"] if item["id"] == arm_id)
        integrity = self.arena.integrity(value)
        ledger_valid = _ledger_matches(value["usage"], attempts)
        selected = [item for item in attempts if item["arm_id"] == arm_id]
        proof = {
            "release": release,
            "evaluation": value,
            "attempts": attempts,
            "runs": [{"id": run["id"], "sha256": digest(run)} for run in runs],
            "integrity": integrity,
            "ledger_valid": ledger_valid,
        }
        return {
            "proof": proof,
            "metrics": metrics,
            "arm": arm,
            "eligible": bool(
                arm["eligibility"]["eligible"]
                and integrity["valid"]
                and ledger_valid
                and not value.get("background")
            ),
            "timing_complete": all(
                item["duration_seconds"] is not None
                for item in selected
                if item["classification"] == "passed"
            ),
        }

    @staticmethod
    def _public(source, verified_at, expires_at):
        proof, arm = source["proof"], source["arm"]
        value = proof["evaluation"]
        conditions = {
            "category": value["category"],
            "model_fingerprint": digest(value["model"]),
            "taskset_fingerprint": digest(
                {
                    k: value[k]
                    for k in (
                        "taskset_id",
                        "taskset_report_sha256",
                        "split",
                        "minimum_pass_rate",
                        "tasks",
                    )
                }
            ),
            "environment_fingerprint": digest(value["environment"]),
            "budget_fingerprint": digest(
                {"run": value["run_budget"], "batch": value["batch_budget"]}
            ),
            "repeats": value["repeats"],
        }
        return _safe_public(
            {
                "schema_version": 1,
                "source": "controller_verified",
                "release_id": proof["release"]["release_id"],
                "content_digest": proof["release"]["content_digest"],
                **conditions,
                "condition_fingerprint": digest(conditions),
                "model_id": value["model"]["model_id"],
                "task_count": len(value["tasks"]),
                "minimum_pass_rate": value["minimum_pass_rate"],
                "pass_rate": arm["pass_rate"],
                "stability": arm["stability"],
                "counts": arm["counts"],
                "batch_counts": source["metrics"]["counts"],
                "usage": arm["usage"],
                "timing": arm["timing"],
                "successful_timing_complete": source["timing_complete"],
                "eligible": source["eligible"],
                "recommendations": {
                    key: source["eligible"]
                    and arm["id"] in source["metrics"]["recommendations"][key]
                    for key in POSITIONS
                },
                "verified_at": verified_at,
                "expires_at": expires_at,
            }
        )

    def prepare(self, body):
        current = datetime.now(UTC)
        require(
            body.expires_at.tzinfo is not None
            and current < body.expires_at <= current + timedelta(days=30),
            "community_evidence_expiry_invalid",
        )
        self.community.release_binding(body.release_id)
        value = {
            "id": str(uuid4()),
            "release_id": body.release_id,
            "evaluation_id": str(body.evaluation_id),
            "arm_id": body.arm_id,
            "state": "preparing",
            "created_at": now(),
            "public": None,
            "preview_sha256": None,
            "private_binding_digest": None,
            "published_at": None,
            "withdrawn_at": None,
            "error": None,
            "proof": None,
        }
        value["revision"] = revision(value)
        # Admission survives an interrupted or failed private inspection.
        with self.store.connect() as c:
            self._consume(c, "preparations")
            c.execute(
                "INSERT INTO community_evidence(id,release_id,state,data) VALUES(%s,%s,%s,%s)",
                (value["id"], body.release_id, value["state"], Jsonb(value)),
            )
        try:
            source = self._source(body.release_id, str(body.evaluation_id), body.arm_id)
            public = self._public(source, now(), body.expires_at)
            value.update(
                state="prepared",
                public=public,
                preview_sha256=digest(public),
                proof=source["proof"],
                private_binding_digest=digest(source["proof"]),
            )
        except (ValueError, OSError, KeyError, TypeError):
            value.update(state="failed", error="community_evidence_inspection_failed")
            with self.store.connect() as c:
                self._save(c, value)
            raise
        with self.store.connect() as c:
            self._save(c, value)
        return self._admin(value)

    @staticmethod
    def _check_database(c, proof):
        # Child -> parent is the existing ledger lock order. Prevent a terminal
        # Run or evaluation mutation between reinspection and snapshot commit.
        for run in sorted(proof["runs"], key=lambda item: item["id"]):
            row = c.execute(
                "SELECT data FROM runs WHERE id=%s FOR SHARE", (run["id"],)
            ).fetchone()
            require(
                row and digest(row["data"]) == run["sha256"],
                "community_evidence_source_changed",
                409,
            )
        row = c.execute(
            "SELECT data FROM arena_evaluations WHERE id=%s FOR SHARE",
            (proof["evaluation"]["id"],),
        ).fetchone()
        require(
            row and digest(row["data"]) == digest(proof["evaluation"]),
            "community_evidence_source_changed",
            409,
        )

    def publish(self, evidence_id, body):
        with self.store.connect() as c:
            old = self._get(c, evidence_id)
        require(
            old["preview_sha256"] == body.preview_sha256,
            "community_evidence_preview_conflict",
            409,
        )
        if old["state"] == "published":
            return self._admin(old)
        require(
            old["revision"] == body.expected_revision and old["state"] == "prepared",
            "community_evidence_revision_conflict",
            409,
        )
        require(
            datetime.fromisoformat(old["public"]["expires_at"]) > datetime.now(UTC),
            "community_evidence_expired",
            409,
        )
        source = self._source(old["release_id"], old["evaluation_id"], old["arm_id"])
        require(
            digest(source["proof"]) == old["private_binding_digest"],
            "community_evidence_source_changed",
            409,
        )
        with self.store.connect() as c:
            self._gate(c)
            release = c.execute(
                "SELECT state FROM community_releases WHERE id=%s FOR SHARE",
                (old["release_id"],),
            ).fetchone()
            require(
                release and release["state"] == "published",
                "community_release_not_published",
                409,
            )
            self._check_database(c, source["proof"])
            value = self._get(c, evidence_id, lock=True)
            if value["state"] == "published":
                return self._admin(value)
            require(
                value["revision"] == body.expected_revision,
                "community_evidence_revision_conflict",
                409,
            )
            self._consume(c, "publications")
            value.update(state="published", published_at=now())
            self._save(c, value)
            c.execute(
                "UPDATE community_evidence SET snapshot=%s WHERE id=%s",
                (
                    Jsonb(
                        {
                            "public": value["public"],
                            "sha256": value["preview_sha256"],
                            "published_at": value["published_at"],
                        }
                    ),
                    value["id"],
                ),
            )
        return self._admin(value)

    def withdraw(self, evidence_id, body):
        with self.store.connect() as c:
            self._gate(c)
            value = self._get(c, evidence_id, lock=True)
            if value["state"] != "withdrawn":
                require(
                    value["revision"] == body.expected_revision
                    and value["state"] == "published",
                    "community_evidence_revision_conflict",
                    409,
                )
                value.update(state="withdrawn", withdrawn_at=now())
                self._save(c, value)
        return self._admin(value)

    def public_evidence(self):
        require(self.community.settings_get()["enabled"], "community_disabled", 404)
        with self.store.connect() as c:
            rows = c.execute(
                "SELECT e.id,e.release_id,e.snapshot,r.data->>'content_digest' AS release_content_digest FROM community_evidence e JOIN community_releases r ON r.id=e.release_id WHERE e.state='published' AND r.state='published' ORDER BY e.id"
            ).fetchall()
        result = []
        for row in rows:
            value = row["snapshot"]
            public = _safe_public(value["public"])
            require(
                digest(public) == value["sha256"]
                and public["release_id"] == row["release_id"]
                and public["content_digest"] == row["release_content_digest"],
                "community_evidence_public_binding_changed",
            )
            if datetime.fromisoformat(public["expires_at"]) > datetime.now(UTC):
                result.append(
                    {
                        "id": str(row["id"]),
                        **public,
                        "published_at": value["published_at"],
                    }
                )
        return result

    def leaderboard(self):
        groups = {}
        for item in self.public_evidence():
            if not item["eligible"]:
                continue
            fingerprint = item["condition_fingerprint"]
            if fingerprint not in groups:
                groups[fingerprint] = {
                    key: item[key]
                    for key in (
                        "condition_fingerprint",
                        "category",
                        "model_id",
                        "model_fingerprint",
                        "taskset_fingerprint",
                        "environment_fingerprint",
                        "budget_fingerprint",
                    )
                }
                groups[fingerprint].update(
                    entries=[], recommendations={key: [] for key in POSITIONS}
                )
            groups[fingerprint]["entries"].append(item)
        for group in groups.values():
            entries = group["entries"]
            reliable = [
                item
                for item in entries
                if item["pass_rate"] == max(item["pass_rate"] for item in entries)
            ]
            stable = [item for item in reliable if item["stability"] == "all_passed"]
            group["recommendations"]["reliable"] = [
                item["id"] for item in stable or reliable
            ]
            for position, field, metric in (
                ("cost", "usage", "unit_success_cost_usd"),
                ("speed", "timing", "successful_median_seconds"),
            ):
                values = [item[field][metric] for item in entries]
                if all(value is not None for value in values) and (
                    position != "speed"
                    or all(item["successful_timing_complete"] for item in entries)
                ):
                    group["recommendations"][position] = [
                        item["id"]
                        for item in entries
                        if item[field][metric] == min(values)
                    ]
        return {"groups": [groups[key] for key in sorted(groups)]}

    def register_usage(self, body):
        run_id = str(body.run_id)
        distribution_id = str(body.distribution_id) if body.distribution_id else None
        with self.store.connect() as c:
            old = c.execute(
                "SELECT data FROM community_verified_usage WHERE release_id=%s AND run_id=%s",
                (body.release_id, run_id),
            ).fetchone()
        if old:
            require(
                old["data"]["distribution_id"] == distribution_id,
                "community_usage_intent_conflict",
                409,
            )
            return old["data"]
        release = self.community.release_binding(body.release_id)
        run = self.store.get(run_id)
        request = run["request"]
        binding = request.get("skill")
        require(
            binding
            and request.get("purpose") != "skill_generation"
            and not request.get("generation")
            and binding.get("content_digest") == release["content_digest"],
            "community_usage_requires_exact_skill_run",
        )
        self.arena.library.skill_context(binding)
        require(
            run["state"] in TERMINAL_RUNS and run.get("first_started_at"),
            "community_usage_requires_terminal_executed_run",
        )
        if distribution_id:
            receipt = self.community.distribution_binding(distribution_id)["receipt"]
            require(
                receipt["release_id"] == body.release_id
                and receipt["content_digest"] == release["content_digest"],
                "community_usage_distribution_mismatch",
            )
        report_hash = (run.get("evidence") or {}).get("report_sha256")
        inspection = {"valid": False, "verified": False, "report": None}
        if isinstance(report_hash, str) and re.fullmatch(HASH, report_hash):
            context = request.get("arena")
            context = (
                {
                    key: context[key]
                    for key in (
                        "taskset_id",
                        "task_id",
                        "report_sha256",
                        "verifier_digest",
                    )
                }
                if context
                else None
            )
            inspection = Evidence.inspect(
                get_workspace(run, run_directory(self.settings, run_id)),
                run_directory(self.settings, run_id),
                report_hash,
                verification_context=context,
            )
        report = inspection.get("report") or {}
        valid = bool(
            inspection["valid"]
            and report.get("run_id") == run_id
            and report.get("commands") == request["checks"]
        )
        success = bool(
            valid
            and inspection["verified"]
            and run["state"] == "completed_verified"
            and not run.get("arena_observed_failure")
            and not (run.get("arena_outcome") or {}).get("safety_violation")
        )
        outcome = "verified_success" if success else "evidence_invalid"
        if not success and run["state"] in {"cancelled", "failed", "budget_exceeded"}:
            outcome = run["state"]
        elif valid and not success:
            outcome = "verified_failure" if request["checks"] else "verified_unchecked"
        fact = UsageFact(
            source="controller_registration",
            registered_at=now(),
            outcome=outcome,
            controller_verified=valid,
            successful_acceptance=success,
            unknown_calls=run["usage"]["unknown_calls"],
            amount_usd=None,
        ).model_dump(mode="json")
        value = {
            "id": str(uuid4()),
            "release_id": body.release_id,
            "run_id": run_id,
            "distribution_id": distribution_id,
            "content_digest": release["content_digest"],
            "run_state": run["state"],
            "public": fact,
            "private_binding_digest": digest(
                {"run": run, "inspection": inspection, "release": release}
            ),
        }
        value["revision"] = revision(value)
        with self.store.connect() as c:
            self._gate(c)
            old = c.execute(
                "SELECT data FROM community_verified_usage WHERE release_id=%s AND run_id=%s",
                (body.release_id, run_id),
            ).fetchone()
            if old:
                require(
                    old["data"]["distribution_id"] == distribution_id,
                    "community_usage_intent_conflict",
                    409,
                )
                return old["data"]
            state = c.execute(
                "SELECT state FROM community_releases WHERE id=%s FOR SHARE",
                (body.release_id,),
            ).fetchone()
            require(
                state and state["state"] == "published",
                "community_release_not_published",
                409,
            )
            current = c.execute(
                "SELECT data FROM runs WHERE id=%s FOR SHARE", (run_id,)
            ).fetchone()
            require(
                current and digest(current["data"]) == digest(run),
                "community_usage_run_changed",
                409,
            )
            self._consume(c, "registrations")
            c.execute(
                "INSERT INTO community_verified_usage VALUES(%s,%s,%s,%s,%s)",
                (value["id"], body.release_id, run_id, Jsonb(value), Jsonb(fact)),
            )
        return value

    def registrations(self):
        with self.store.connect() as c:
            return [
                row["data"]
                for row in c.execute(
                    "SELECT data FROM community_verified_usage ORDER BY id"
                )
            ]

    def public_usage(self, release_id):
        # Community.release reads only public release state/content. It does not
        # resolve a Library, Run, project or private evaluation.
        self.community.release(release_id)
        with self.store.connect() as c:
            distributions = c.execute(
                "SELECT data->'downloads' AS downloads FROM community_distributions WHERE release_id=%s",
                (release_id,),
            ).fetchall()
            reports = c.execute(
                "SELECT data->>'outcome' AS outcome FROM community_usage_reports WHERE data->>'release_id'=%s",
                (release_id,),
            ).fetchall()
            registrations = c.execute(
                "SELECT fact FROM community_verified_usage WHERE release_id=%s",
                (release_id,),
            ).fetchall()
        facts = []
        for row in registrations:
            facts.append(UsageFact.model_validate(row["fact"]).model_dump(mode="json"))
        return {
            "release_id": release_id,
            "window": "lifetime_since_publication",
            "as_of": now(),
            "distribution_requests": len(distributions),
            "download_response_admissions": sum(
                row["downloads"] for row in distributions
            ),
            "download_transfer_verified": False,
            "self_reported": {
                key: sum(row["outcome"] == key for row in reports)
                for key in ("installed", "used", "failed")
            },
            "controller": {
                "registrations": len(facts),
                "verified_uses": sum(item["controller_verified"] for item in facts),
                "successful_acceptances": sum(
                    item["successful_acceptance"] for item in facts
                ),
                "failed": sum(
                    item["outcome"]
                    in {
                        "failed",
                        "verified_failure",
                        "budget_exceeded",
                        "evidence_invalid",
                    }
                    for item in facts
                ),
                "cancelled": sum(item["outcome"] == "cancelled" for item in facts),
                "unknown_calls": sum(item["unknown_calls"] for item in facts),
            },
            "commercial": {
                "enabled": False,
                "settlement_enabled": False,
                "amount_usd": None,
                "earnings_usd": None,
            },
        }


def install_community_evidence_routes(app, settings, store, auth):
    service = CommunityEvidence(settings, store)

    def call(function, *args):
        try:
            return function(*args)
        except CommunityError as error:
            raise HTTPException(error.status, {"code": error.code}) from None
        except (ValueError, OSError, KeyError, TypeError):
            raise HTTPException(
                400, {"code": "community_evidence_input_or_integrity_invalid"}
            ) from None

    @app.get("/api/community/admin/evidence", dependencies=auth)
    def evidence_list():
        return {"evidence": call(service.list)}

    @app.post("/api/community/admin/evidence/prepare", dependencies=auth)
    def prepare(body: PrepareEvidenceInput):
        return call(service.prepare, body)

    @app.post("/api/community/admin/evidence/{evidence_id}/publish", dependencies=auth)
    def publish(evidence_id: str, body: PublishEvidenceInput):
        return call(service.publish, evidence_id, body)

    @app.post("/api/community/admin/evidence/{evidence_id}/withdraw", dependencies=auth)
    def withdraw(evidence_id: str, body: WithdrawEvidenceInput):
        return call(service.withdraw, evidence_id, body)

    @app.get("/api/community/admin/verified-usage", dependencies=auth)
    def registrations():
        return {"registrations": call(service.registrations)}

    @app.post("/api/community/admin/verified-usage", dependencies=auth)
    def register(body: RegisterUsageInput):
        return call(service.register_usage, body)

    @app.get("/api/community/public/evidence")
    def public_evidence():
        return {"evidence": call(service.public_evidence)}

    @app.get("/api/community/public/leaderboard")
    def leaderboard():
        return call(service.leaderboard)

    @app.get("/api/community/public/releases/{release_id}/usage")
    def usage(release_id: str):
        return call(service.public_usage, release_id)
