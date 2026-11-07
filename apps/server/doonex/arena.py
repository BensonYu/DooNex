"""Manual private comparisons with immutable plans and explicit recommendations."""

import hashlib
import json
import re
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from doonex.arena_metrics import summarize
from doonex.budget import elapsed
from doonex.library import Library
from doonex.model_registry import ModelRegistry
from doonex.models import Budget, StrictModel
from doonex.sandbox import TASK_IMAGE
from doonex.store import dispatch_lock, now

TERMINAL_RUNS = {
    "completed_verified",
    "completed_unverified",
    "failed",
    "cancelled",
    "budget_exceeded",
}
ACTIVE_BATCHES = {"queued", "running", "cancelling"}
MAX_PLANNED_ATTEMPTS = 400  # 10 tasks, baseline + 3 Skills, 10 repeats.


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def environment():
    from importlib.metadata import version

    root = Path(__file__).parent
    return {
        "sdk": version("openhands-sdk"),
        "image": TASK_IMAGE,
        "protocol": "private-arena-v1",
        "network": "none",
        "cpu": 1,
        "memory_mb": 512,
        "controller": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "sdk.py",
                "sandbox.py",
                "worker.py",
                "budget.py",
                "evidence.py",
                "arena.py",
                "arena_metrics.py",
                "workspace.py",
                "tasksets.py",
                "skills.py",
                "library.py",
                "models.py",
                "store.py",
                "background.py",
                "scheduling.py",
            )
        },
        "provider_cache_control": "unsupported",
        "provider_seed": "unsupported",
    }


class BatchBudget(StrictModel):
    max_calls: int = Field(default=200, ge=1, le=10000)
    max_tokens: int = Field(default=1000000, ge=1, le=100000000)
    max_cost_usd: float | None = Field(default=10, gt=0, le=1000)
    max_seconds: int = Field(default=14400, ge=1, le=86400)


class EvaluationInput(StrictModel):
    taskset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    skill_version_ids: list[str] = Field(min_length=2, max_length=3)
    model_profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    repeats: int = Field(default=3, ge=1, le=10)
    run_budget: Budget = Field(default_factory=Budget)
    batch_budget: BatchBudget = Field(default_factory=BatchBudget)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.skill_version_ids)) != len(self.skill_version_ids):
            raise ValueError("候选版本不可重复")
        return self


class RecommendationInput(StrictModel):
    evaluation_id: str
    position: Literal["reliable", "cost", "speed"]
    arm_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class RollbackInput(StrictModel):
    recommendation_id: str


def _uuid(identifier):
    return str(UUID(identifier))


def seconds(start, end):
    if not start or not end:
        return None
    return max(
        0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    )


class Arena:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.library = Library(settings, store)

    def get(self, identifier, connection=None):
        identifier = _uuid(identifier)
        if connection is None:
            with self.store.connect() as c:
                return self.get(identifier, c)
        row = connection.execute(
            "SELECT data FROM arena_evaluations WHERE id=%s", (identifier,)
        ).fetchone()
        if not row:
            raise KeyError("找不到评测")
        return row["data"]

    def list(self):
        with self.store.connect() as c:
            return [
                r["data"]
                for r in c.execute(
                    "SELECT data FROM arena_evaluations ORDER BY data->>'created_at' DESC,id"
                )
            ]

    @contextmanager
    def locked(self, identifier):
        with self.store.connect() as c:
            row = c.execute(
                "SELECT data FROM arena_evaluations WHERE id=%s FOR UPDATE",
                (_uuid(identifier),),
            ).fetchone()
            if not row:
                raise KeyError("找不到评测")
            value = row["data"]
            yield c, value
            value["updated_at"] = now()
            c.execute(
                "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
                (value["state"], Jsonb(value), value["id"]),
            )

    def prepare(self, body):
        taskset = self.library.get("taskset", body.taskset_id)
        if not self.library.taskset(taskset)["valid"]:
            raise ValueError("任务集完整性检查失败")
        skills = {
            identifier: self.library.freeze_skill(identifier)
            for identifier in body.skill_version_ids
        }
        profile = ModelRegistry(self.settings).get(body.model_profile_id)
        model = profile.model.model_dump()
        if not self.settings.credential(model):
            raise ValueError("所选模型凭据不可用")
        run_budget, batch_budget = (
            body.run_budget.model_dump(),
            body.batch_budget.model_dump(),
        )
        subscription = model["billing_mode"] == "subscription"
        upper = model["max_input_tokens"] + model["max_output_tokens"]
        if min(run_budget["max_tokens"], batch_budget["max_tokens"]) < upper:
            raise ValueError("运行及批次预算必须能预留一次模型配置上限的请求")
        if subscription:
            run_budget["max_cost_usd"] = batch_budget["max_cost_usd"] = None
        else:
            if not all(
                model[k]
                for k in (
                    "input_price_per_million",
                    "output_price_per_million",
                    "price_source",
                )
            ):
                raise ValueError("模型价格未知；不能创建按量评测")
            cost = (
                model["max_input_tokens"] * model["input_price_per_million"]
                + model["max_output_tokens"] * model["output_price_per_million"]
            ) / 1000000
            if any(
                b["max_cost_usd"] is None or b["max_cost_usd"] < cost
                for b in (run_budget, batch_budget)
            ):
                raise ValueError("运行或批次费用预算不足以预留模型请求")
        return taskset, skills, profile, model, run_budget, batch_budget

    def create(self, body, connection=None, *, background=None, prepared=None):
        # File validation is completed before taking the short dispatch lock.
        # Background.claim independently rechecks the frozen database binding.
        taskset, skills, profile, model, run_budget, batch_budget = (
            self.prepare(body) if prepared is None else prepared
        )
        subscription = model["billing_mode"] == "subscription"
        identifier = str(uuid4())
        profile_record = {
            "id": profile.id,
            "name": profile.name,
            "revision": profile.revision,
        }
        conditions = {
            "taskset_id": body.taskset_id,
            "model": model,
            "environment": environment(),
            "run_budget": run_budget,
            "batch_budget": batch_budget,
        }
        value = {
            "id": identifier,
            "state": "queued",
            "reason": "等待固定批次执行",
            "created_at": now(),
            "updated_at": now(),
            "first_started_at": None,
            "taskset_id": body.taskset_id,
            "taskset_report_sha256": taskset["report_sha256"],
            "model": model,
            "model_profile": profile_record,
            "environment": conditions["environment"],
            "run_budget": run_budget,
            "batch_budget": batch_budget,
            "repeats": body.repeats,
            "skill_version_ids": body.skill_version_ids,
            "skills": skills,
            "split": taskset["split"],
            "category": taskset["category"],
            "minimum_pass_rate": taskset["minimum_pass_rate"],
            "tasks": [
                {k: t[k] for k in ("id", "title", "critical")} for t in taskset["tasks"]
            ],
            "arms": [{"id": "baseline", "label": "无额外 Skill"}]
            + [{"id": k, "label": k[:12]} for k in skills],
            "fingerprint": digest(conditions),
            "attempts": [],
            "usage": {
                "calls": 0,
                "tokens": 0,
                "reserved_tokens": 0,
                "unknown_calls": 0,
                "cost_usd": None if subscription else 0.0,
                "reserved_cost_usd": None if subscription else 0.0,
                "metered_cost_usd": 0.0,
                "reserved_metered_cost_usd": 0.0,
            },
        }
        if background:
            if value["split"] != "tuning":
                raise ValueError("后台只能执行调优任务集")
            value["background"] = background
        # All planned children and their parent are committed together. The
        # worker only claims committed rows. Interleave arms across repeats.
        with (
            self.store.connect() if connection is None else nullcontext(connection)
        ) as c:
            dispatch_lock(c)
            c.execute(
                "INSERT INTO arena_evaluations VALUES(%s,%s,%s)",
                (identifier, "queued", Jsonb(value)),
            )
            for repeat in range(body.repeats):
                for task_index, task in enumerate(taskset["tasks"]):
                    arms = value["arms"]
                    offset = (repeat + task_index) % len(arms)
                    for arm in arms[offset:] + arms[:offset]:
                        project_id = taskset["project_ids"][task["id"]]
                        registered = self.store.project(project_id)
                        binding = {
                            "evaluation_id": identifier,
                            "taskset_id": body.taskset_id,
                            "task_id": task["id"],
                            "report_sha256": taskset["report_sha256"],
                            "verifier_digest": task["verifier_digest"],
                            "source_digest": task["source_digest"],
                            "arm_id": arm["id"],
                            "repeat": repeat,
                            "fingerprint": value["fingerprint"],
                        }
                        request = {
                            "project_id": project_id,
                            "project_name": registered["name"],
                            "repo_path": registered["path"],
                            "base_commit": task["base_commit"],
                            "base_ref": task["base_commit"],
                            "base_branch": None,
                            "prompt": task["prompt"],
                            "checks": [task["verifier_command"]],
                            "visible_checks": task["visible_checks"],
                            "workflow": "execute",
                            "budget": run_budget,
                            "model": model,
                            "role_models": {"executor": model},
                            "model_profiles": {"executor": profile_record},
                            "model_profile_id": profile.id,
                            "reviewer_model_profile_id": None,
                            "skill_version_id": None
                            if arm["id"] == "baseline"
                            else arm["id"],
                            "skill": skills.get(arm["id"]),
                            "arena": binding,
                            "acceptance_version": 1,
                        }
                        if background:
                            request["background"] = background
                        run = self.store.create_run(request, connection=c)
                        value["attempts"].append(
                            {
                                "run_id": run["id"],
                                "project_id": project_id,
                                "task_id": task["id"],
                                "arm_id": arm["id"],
                                "repeat": repeat,
                            }
                        )
            c.execute(
                "UPDATE arena_evaluations SET data=%s WHERE id=%s",
                (Jsonb(value), identifier),
            )
        return self.detail(identifier) if connection is None else value

    def integrity(self, value, *, approval=False):
        errors = []
        if value["environment"] != environment():
            errors.append("执行环境或控制器版本已变化，不能混合比较")
        status = self.library.tasksets.inspect(
            value["taskset_id"], value["taskset_report_sha256"]
        )
        if not status["valid"]:
            errors.extend(status["errors"])
        for binding in value["skills"].values():
            try:
                self.library.skill_context(binding)
                if approval:
                    self.library.freeze_skill(binding["version_id"])
            except (ValueError, OSError, KeyError) as error:
                errors.append(str(error))
        return {"valid": not errors, "errors": errors}

    def start(self, identifier):
        value = self.get(identifier)
        integrity = self.integrity(value)
        if not integrity["valid"]:
            self.stop(identifier, "interrupted", "; ".join(integrity["errors"]))
            return False
        with self.locked(identifier) as (_, value):
            if value["state"] not in {"queued", "running"}:
                return False
            value["state"] = "running"
            value["first_started_at"] = value["first_started_at"] or now()
            value["reason"] = "按固定计划执行基线与候选"
        return True

    def stopped(self, identifier):
        value = self.get(identifier)
        return (
            value["state"] != "running"
            or elapsed(value) >= value["batch_budget"]["max_seconds"]
        )

    def stop(self, identifier, state, reason):
        # Never acquire child Run locks while retaining the parent row lock:
        # request reservations consistently lock Run -> parent.
        with self.locked(identifier) as (_, value):
            if value["state"] in {"complete", "cancelled", "budget_exceeded"}:
                return
            value["state"], value["reason"] = state, reason
            children = list(value["attempts"])
        if state in {"cancelling", "budget_exceeded"}:
            for attempt in children:
                with self.store.locked(attempt["run_id"]) as (c, run):
                    if run["state"] in {"queued", "yielded"}:
                        run.update(
                            state="cancelled",
                            reason=reason,
                            ended_at=now(),
                            arena_outcome={
                                "classification": "cancelled",
                                "kind": "batch_stop",
                                "recorded_at": now(),
                            },
                        )
                        self.store.event(run["id"], "cancelled", {"reason": reason}, c)
                    elif run["state"] in {"running", "verifying", "pausing"}:
                        run["requested_action"] = (
                            "cancel" if state == "cancelling" else "batch_stop"
                        )
        self.sync(identifier)

    def sync(self, identifier):
        identifier = _uuid(identifier)
        value = self.get(identifier)
        attempts = value.get("attempts")
        if (
            not isinstance(attempts, list)
            or not 1 <= len(attempts) <= MAX_PLANNED_ATTEMPTS
        ):
            raise ValueError("评测子运行计划数量无效")
        try:
            run_ids = [_uuid(attempt["run_id"]) for attempt in attempts]
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError("评测子运行计划标识无效") from None
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("评测子运行计划包含重复运行")
        with self.locked(identifier) as (c, current):
            if current.get("attempts") != attempts or current.get("id") != identifier:
                raise ValueError("评测子运行计划已变化")
            # Read only this finite frozen plan in one statement. Do not acquire
            # child locks while holding the parent lock (reservations use Run -> parent).
            rows = c.execute(
                "SELECT id,state,data->>'id' AS run_id,data->>'state' AS run_state,"
                "data->'request'->'arena'->>'evaluation_id' AS evaluation_id "
                "FROM runs WHERE id=ANY(%s::uuid[])",
                (run_ids,),
            ).fetchall()
            if {str(row["id"]) for row in rows} != set(run_ids) or len(rows) != len(
                run_ids
            ):
                raise ValueError("评测子运行计划存在缺失运行")
            if any(
                row["run_id"] != str(row["id"])
                or row["run_state"] != row["state"]
                or row["evaluation_id"] != identifier
                for row in rows
            ):
                raise ValueError("评测子运行归属或状态绑定无效")
            states = {row["state"] for row in rows}
            if current["state"] == "cancelling":
                if states <= TERMINAL_RUNS | {"paused", "interrupted"}:
                    current.update(
                        state="cancelled", reason="已取消批次；记录与未知预留保留"
                    )
            elif current["state"] in {"queued", "running"}:
                if states & {"paused", "interrupted"}:
                    current.update(
                        state="interrupted",
                        reason="子运行中断或暂停；核查后明确恢复整个批次",
                    )
                elif states <= TERMINAL_RUNS:
                    current.update(
                        state="complete",
                        reason="计划尝试已结束；质量与推荐以实际指标为准",
                    )

    def resume(self, identifier, acknowledge_unknown):
        from doonex.responses_registry import helper_fenced
        from doonex.sandbox import Sandbox
        from doonex.worker import get_workspace, run_directory

        with self.store.connect() as connection:
            if helper_fenced(connection):
                raise ValueError("模型传输进程尚未完成独立核查；禁止恢复批次")
        value = self.get(identifier)
        if value["state"] != "interrupted" or not acknowledge_unknown:
            raise ValueError("请核查批次的未决动作并明确确认恢复")
        if elapsed(value) >= value["batch_budget"]["max_seconds"]:
            raise ValueError("原批次时长已经用尽，恢复不能清零预算")
        integrity = self.integrity(value)
        if not integrity["valid"]:
            raise ValueError("; ".join(integrity["errors"]))
        children = [self.store.get(a["run_id"]) for a in value["attempts"]]
        assistance = {}
        for run in children:
            if run["state"] in {"paused", "interrupted"}:
                directory = run_directory(self.settings, run["id"])
                if Sandbox().containers(run["id"]):
                    raise ValueError("子运行仍有容器，不能恢复")
                workspace = get_workspace(run, directory)
                from doonex.workspace import Workspace

                Workspace.scan(workspace.path)
                workspace.base_files()
                current_digest = Workspace.execution_digest(workspace.path)
                uncertain_shell = any(
                    action["kind"] == "shell"
                    for action in self.store.pending(run["id"])
                )
                if uncertain_shell or current_digest != run.get(
                    "workspace_checkpoint_digest"
                ):
                    assistance[run["id"]] = {
                        "reason": "未决工具或工作区与已记录检查点不一致，不能证明自主完成",
                        "at": now(),
                        "snapshot_digest": current_digest,
                    }
                if elapsed(run) >= run["request"]["budget"]["max_seconds"]:
                    raise ValueError("子运行时长已用尽；保留批次记录")
        for run in children:
            if run["state"] not in {"paused", "interrupted"}:
                continue
            with self.store.locked(run["id"], dispatch=True) as (c, current):
                if current["state"] not in {"paused", "interrupted"}:
                    raise ValueError("子运行状态已变化，请重新核查")
                if helper_fenced(c):
                    raise ValueError("模型传输进程尚未完成独立核查；禁止恢复批次")
                if current["request"].get("background"):
                    from doonex.background import Background

                    Background(self.settings, self.store).preflight(
                        c,
                        current["request"]["background"]["authorization_id"],
                        identifier,
                    )
                current.pop("scheduling", None)
                if run["id"] in assistance:
                    current["arena_assisted"] = True
                    current.setdefault("arena_assistance", []).append(
                        assistance[run["id"]]
                    )
                current.update(
                    state="queued",
                    requested_action=None,
                    reason="已核查批次恢复，预算不变",
                    recovery={
                        "remaining": [],
                        "errors": [],
                        "stopped": [],
                        "confirmed_at": now(),
                    },
                )
                active = current.get("assignments", {}).get(
                    current.get("active_assignment")
                )
                if active and active["state"] != "completed":
                    active.update(
                        state="pending",
                        session_generation=active.get("session_generation", 0) + 1,
                        resume_session=False,
                    )
                c.execute(
                    "UPDATE actions SET status='acknowledged' WHERE run_id=%s AND status='unknown'",
                    (run["id"],),
                )
                self.store.event(
                    run["id"],
                    "resume_requested",
                    {
                        "acknowledged_unknown": True,
                        "budget_reset": False,
                        "arena": identifier,
                    },
                    c,
                )
        with self.locked(identifier) as (_, current):
            if current["state"] != "interrupted":
                raise ValueError("批次状态已变化")
            current.update(state="running", reason="已确认核查，沿用原预算恢复")
        return self.detail(identifier)

    def inspect_run_evidence(self, run):
        """Inspect only a database-bound report belonging to this actual Run.

        Evidence.inspect also supports intentionally unbound local inspections.
        Arena quality evidence must never fall back to that weaker contract.
        """
        from doonex.evidence import Evidence
        from doonex.worker import get_workspace, run_directory

        invalid = {"valid": False, "verified": False, "report": None}
        evidence = run.get("evidence")
        expected = evidence.get("report_sha256") if isinstance(evidence, dict) else None
        if (
            not isinstance(expected, str)
            or re.fullmatch(r"[a-f0-9]{64}", expected) is None
        ):
            return invalid
        try:
            binding = run["request"]["arena"]
            context = {
                key: binding[key]
                for key in ("taskset_id", "task_id", "report_sha256", "verifier_digest")
            }
            directory = run_directory(self.settings, run["id"])
            inspection = Evidence.inspect(
                get_workspace(run, directory),
                directory,
                expected,
                verification_context=context,
            )
            report = inspection.get("report") or {}
            bound = bool(
                inspection["valid"]
                and report.get("run_id") == run["id"]
                and report.get("commands") == run["request"]["checks"]
            )
            return {
                **inspection,
                "valid": bound,
                "verified": bound and inspection["verified"],
            }
        except (ValueError, OSError, KeyError, TypeError):
            return invalid

    def attempt(self, entry):
        run = self.store.get(entry["run_id"])
        state = run["state"]
        outcome = run.get("arena_outcome") or {}
        classification = "pending"
        evidence_valid = False
        if state in TERMINAL_RUNS:
            classification = "cancelled" if state == "cancelled" else "failed"
            evidence_valid = bool(outcome and outcome.get("recorded_at"))
            if outcome.get("classification") == "environment_failure":
                classification = "environment_failure"
            if state == "completed_verified":
                evidence_valid = self.inspect_run_evidence(run)["verified"]
                classification = "passed" if evidence_valid else "failed"
        if (
            state in TERMINAL_RUNS
            and run.get("evidence")
            and state != "completed_verified"
        ):
            evidence_valid = self.inspect_run_evidence(run)["valid"]
        if state in TERMINAL_RUNS:
            if run.get("arena_observed_failure") or outcome.get("safety_violation"):
                classification = "failed"
            elif run.get("arena_assisted"):
                classification = "assisted"
        ended = run.get("ended_at")
        return {
            **entry,
            "classification": classification,
            "state": state,
            "reason": run.get("reason"),
            "evidence_valid": evidence_valid,
            "safety_violation": bool(outcome.get("safety_violation")),
            "regression_failed": bool(
                outcome.get("regression_failed") or run.get("arena_observed_failure")
            ),
            "assistance": run.get("arena_assistance", []),
            "duration_seconds": seconds(run.get("first_started_at"), ended),
            "queued_seconds": seconds(run["created_at"], run.get("first_started_at")),
            "usage": run["usage"],
            "classification_evidence": outcome,
        }

    def detail(self, identifier):
        value = self.get(identifier)
        attempts = [self.attempt(a) for a in value["attempts"]]
        metrics = summarize(value, attempts)
        integrity = self.integrity(value)
        if value["state"] in {"complete", "cancelled", "budget_exceeded"} and any(
            value["usage"][key] != sum(attempt["usage"][key] for attempt in attempts)
            for key in ("calls", "tokens", "reserved_tokens", "unknown_calls")
        ):
            integrity["valid"] = False
            integrity["errors"].append("批次与子运行总账不一致，禁止形成推荐")
        if not integrity["valid"]:
            metrics["recommendations"] = {k: [] for k in ("reliable", "cost", "speed")}
            for arm in metrics["arms"]:
                arm["eligibility"]["eligible"] = False
                arm["eligibility"]["reasons"].append("frozen_context_invalid")
        return {
            **value,
            "attempts": attempts,
            "metrics": metrics,
            "integrity": integrity,
        }

    def recommendations(self):
        with self.store.connect() as c:
            history = [
                r["data"]
                for r in c.execute(
                    "SELECT data FROM arena_recommendations ORDER BY data->>'created_at' DESC,id"
                )
            ]
            ids = {
                str(r["recommendation_id"])
                for r in c.execute("SELECT recommendation_id FROM arena_current")
            }
        return {"history": history, "current": [r for r in history if r["id"] in ids]}

    def recommend(self, evaluation_id, position, arm_id, *, rollback_id=None):
        value = self.detail(evaluation_id)
        if value.get("background"):
            raise ValueError("后台调优结果不能推广为推荐")
        if arm_id not in value["metrics"]["recommendations"][position]:
            raise ValueError("该版本不满足当前场景的推荐门槛")
        self.library.freeze_skill(arm_id)
        if not self.integrity(value, approval=True)["valid"]:
            raise ValueError("推广前版本或环境复核失败")
        with self.store.connect() as c:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (4142061108,))
            old = c.execute(
                "SELECT recommendation_id FROM arena_current WHERE fingerprint=%s AND position=%s",
                (value["fingerprint"], position),
            ).fetchone()
            record = {
                "id": str(uuid4()),
                "position": position,
                "arm_id": arm_id,
                "evaluation_id": value["id"],
                "fingerprint": value["fingerprint"],
                "category": value["category"],
                "model": value["model"],
                "created_at": now(),
                "previous_id": str(old["recommendation_id"]) if old else None,
            }
            if rollback_id:
                record["rollback_of"] = rollback_id
            c.execute(
                "INSERT INTO arena_recommendations VALUES(%s,%s)",
                (record["id"], Jsonb(record)),
            )
            c.execute(
                "INSERT INTO arena_current VALUES(%s,%s,%s) ON CONFLICT(fingerprint,position) DO UPDATE SET recommendation_id=EXCLUDED.recommendation_id",
                (record["fingerprint"], position, record["id"]),
            )
        return record

    def select_for_run(self, identifier, model):
        identifier = _uuid(identifier)
        with self.store.connect() as c:
            row = c.execute(
                "SELECT r.data FROM arena_recommendations r JOIN arena_current c ON c.recommendation_id=r.id WHERE r.id=%s",
                (identifier,),
            ).fetchone()
        if not row:
            raise ValueError("推荐已变更；请刷新后明确选择当前推荐")
        record = row["data"]
        value = self.detail(record["evaluation_id"])
        if model != value["model"]:
            raise ValueError("推荐的模型配置与执行模型不一致，不能跨条件应用")
        if (
            record["arm_id"]
            not in value["metrics"]["recommendations"][record["position"]]
        ):
            raise ValueError("推荐的证据或资格已失效")
        return record, self.library.freeze_skill(record["arm_id"])

    def rollback(self, identifier):
        with self.store.connect() as c:
            row = c.execute(
                "SELECT data FROM arena_recommendations WHERE id=%s",
                (_uuid(identifier),),
            ).fetchone()
        if not row:
            raise KeyError("找不到历史推荐")
        record = row["data"]
        return self.recommend(
            record["evaluation_id"],
            record["position"],
            record["arm_id"],
            rollback_id=record["id"],
        )


def install_arena_routes(app, settings, store, auth):
    arena = Arena(settings, store)

    @app.get("/api/arena/evaluations", dependencies=auth)
    def evaluations():
        return {"evaluations": [arena.detail(r["id"]) for r in arena.list()]}

    @app.post("/api/arena/evaluations", dependencies=auth)
    def create(body: EvaluationInput):
        return arena.create(body)

    @app.get("/api/arena/evaluations/{evaluation_id}", dependencies=auth)
    def detail(evaluation_id: str):
        return arena.detail(evaluation_id)

    @app.post("/api/arena/evaluations/{evaluation_id}/cancel", dependencies=auth)
    def cancel(evaluation_id: str):
        arena.stop(evaluation_id, "cancelling", "用户取消整个批次")
        return arena.detail(evaluation_id)

    @app.post("/api/arena/evaluations/{evaluation_id}/resume", dependencies=auth)
    def resume(evaluation_id: str, body: dict):
        return arena.resume(evaluation_id, body.get("acknowledge_unknown") is True)

    @app.get("/api/arena/recommendations", dependencies=auth)
    def recommendations():
        return arena.recommendations()

    @app.post("/api/arena/recommendations", dependencies=auth)
    def recommend(body: RecommendationInput):
        return arena.recommend(body.evaluation_id, body.position, body.arm_id)

    @app.post("/api/arena/recommendations/rollback", dependencies=auth)
    def rollback(body: RollbackInput):
        return arena.rollback(body.recommendation_id)
