"""Finite, explicit tuning grants and one shared ledger per UTC dispatch day.

Callers compose claim -> Arena.create(connection=...) -> attach in one transaction.
They acquire the dispatch advisory lock before these methods; this module never
locks a Run or dispatches work. No schedule, retry, or model call lives here.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from doonex.arena import BatchBudget, EvaluationInput, digest, environment
from doonex.library import Library
from doonex.model_registry import ModelRegistry
from doonex.models import Budget, StrictModel

BACKGROUND_SCHEMA = """
CREATE TABLE IF NOT EXISTS background_authorizations (
 id uuid PRIMARY KEY, state text NOT NULL, data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS background_days (
 day date PRIMARY KEY, data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS background_allocations (
 id uuid PRIMARY KEY,
 authorization_id uuid UNIQUE NOT NULL REFERENCES background_authorizations(id),
 evaluation_id uuid UNIQUE REFERENCES arena_evaluations(id),
 day date NOT NULL REFERENCES background_days(day),
 state text NOT NULL CHECK(state IN ('reserved','settled')),
 data jsonb NOT NULL
);
"""

TERMINAL_BATCHES = frozenset({"complete", "cancelled", "budget_exceeded", "failed"})
TERMINAL_RUNS = frozenset(
    {
        "completed_verified",
        "completed_unverified",
        "failed",
        "cancelled",
        "budget_exceeded",
    }
)
USAGE_KEYS = ("calls", "tokens", "seconds")


class BackgroundStopped(ValueError):
    """The grant can no longer dispatch; in-flight receipts may still settle."""


class BackgroundWaiting(ValueError):
    """An enabled grant has not reached its one finite window yet."""


class BackgroundConflict(ValueError):
    """A stale revision or already consumed grant cannot grant more work."""


class DailyBudget(StrictModel):
    max_calls: int = Field(ge=1, le=10000)
    max_tokens: int = Field(ge=1, le=100000000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=1000)
    max_seconds: int = Field(ge=1, le=86400)


class BackgroundInput(StrictModel):
    name: str = Field(default="后台调优", min_length=1, max_length=100)
    taskset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    skill_version_ids: list[str] = Field(min_length=2, max_length=3)
    model_profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    repeats: int = Field(default=1, ge=1, le=10)
    run_budget: Budget = Field(default_factory=Budget)
    batch_budget: BatchBudget = Field(default_factory=BatchBudget)
    daily_budget: DailyBudget
    window_start: datetime
    window_end: datetime

    @model_validator(mode="after")
    def finite_utc_window(self):
        if not self.name.strip() or any(ord(char) < 32 for char in self.name):
            raise ValueError("后台授权名称无效")
        for value in (self.window_start, self.window_end):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("窗口必须包含明确时区")
        self.window_start = self.window_start.astimezone(UTC)
        self.window_end = self.window_end.astimezone(UTC)
        if (
            self.window_start.date() != self.window_end.date()
            or not self.window_start < self.window_end
        ):
            raise ValueError("只支持同一 UTC 日期内 start < end 的有限窗口")
        EvaluationInput.model_validate(
            self.model_dump(
                include={
                    "taskset_id",
                    "skill_version_ids",
                    "model_profile_id",
                    "repeats",
                    "run_budget",
                    "batch_budget",
                }
            )
        )
        if len(set(self.skill_version_ids)) != len(self.skill_version_ids):
            raise ValueError("候选版本不可重复")
        return self


def _utc(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("后台时钟必须返回有时区的 datetime")
    return value.astimezone(UTC)


def _identifier(value):
    return str(UUID(str(value)))


def _day(value):
    return date.fromisoformat(str(value)).isoformat()


def _empty_usage():
    return {
        "calls": 0,
        "tokens": 0,
        "seconds": 0,
        "cost_usd": 0.0,
        "metered_cost_usd": 0.0,
    }


def _revision(value):
    return digest({key: item for key, item in value.items() if key != "revision"})


def _sum_usage(values):
    values = list(values)
    return {
        **{key: sum(value[key] for value in values) for key in USAGE_KEYS},
        "cost_usd": None
        if any(value["cost_usd"] is None for value in values)
        else sum(value["cost_usd"] for value in values),
        "metered_cost_usd": sum(value["metered_cost_usd"] for value in values),
    }


class Background:
    def __init__(self, settings, store, *, clock=None):
        self.settings, self.store = settings, store
        self.library = Library(settings, store)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self):
        return _utc(self.clock())

    def get(self, identifier, connection=None, *, lock=False):
        if connection is None:
            with self.store.connect() as c:
                return self.get(identifier, c, lock=lock)
        row = connection.execute(
            "SELECT data FROM background_authorizations WHERE id=%s"
            + (" FOR UPDATE" if lock else ""),
            (_identifier(identifier),),
        ).fetchone()
        if not row:
            raise KeyError("找不到后台授权")
        return row["data"]

    def list(self):
        with self.store.connect() as c:
            return [
                row["data"]
                for row in c.execute(
                    "SELECT data FROM background_authorizations ORDER BY data->>'created_at',id"
                )
            ]

    def day(self, value, connection=None, *, lock=False):
        if connection is None:
            with self.store.connect() as c:
                return self.day(value, c, lock=lock)
        row = connection.execute(
            "SELECT data FROM background_days WHERE day=%s"
            + (" FOR UPDATE" if lock else ""),
            (_day(value),),
        ).fetchone()
        if not row:
            raise KeyError("该 UTC 日期尚未设置后台日预算")
        return row["data"]

    def _save(self, c, value, event, **details):
        stamp = self._now().isoformat()
        value["updated_at"] = stamp
        value["history"].append({"event": event, "at": stamp, **details})
        value["revision"] = _revision(value)
        c.execute(
            "UPDATE background_authorizations SET state=%s,data=%s WHERE id=%s",
            (value["state"], Jsonb(value), value["id"]),
        )
        return value

    def _snapshot(self, body, c, *, inspect_contents=True):
        # Gate on metadata before opening any task-set report or task material.
        marker = c.execute(
            "SELECT data->>'split' AS split,data->>'report_sha256' AS report_sha256 FROM library_versions WHERE kind='taskset' AND id=%s FOR SHARE",
            (body.taskset_id,),
        ).fetchone()
        if not marker or marker["split"] != "tuning":
            raise BackgroundStopped("后台仅允许 tuning 任务集")
        if inspect_contents:
            taskset = self.library.get("taskset", body.taskset_id, c)
            if not self.library.taskset(taskset)["valid"]:
                raise BackgroundStopped("任务集完整性已变化")
        skills = {}
        # Stable order avoids deadlocks between grants sharing candidates.
        for identifier in sorted(body.skill_version_ids):
            record = c.execute(
                "SELECT data FROM library_versions WHERE kind='skill' AND id=%s FOR SHARE",
                (identifier,),
            ).fetchone()
            if not record or record["data"].get("review_status") != "approved":
                raise BackgroundStopped("后台候选必须已经审核批准")
            record = record["data"]
            if (
                inspect_contents
                and not self.library.skill(record)["integrity"]["valid"]
            ):
                raise BackgroundStopped("后台候选内容或保存凭据已变化")
            skills[identifier] = {
                **{
                    key: record[key]
                    for key in (
                        "version_id",
                        "content_digest",
                        "report_sha256",
                        "metadata",
                    )
                },
                "review_revision": digest(
                    {
                        "status": record["review_status"],
                        "history": record["review_history"],
                    }
                ),
            }
        profile = ModelRegistry(self.settings).get(body.model_profile_id)
        model = profile.model.model_dump()
        if not self.settings.credential(model):
            raise BackgroundStopped("所选后台模型凭据不可用")
        if model["billing_mode"] == "subscription":
            body.run_budget.max_cost_usd = body.batch_budget.max_cost_usd = None
        elif body.daily_budget.max_cost_usd is None:
            raise BackgroundStopped("按量后台模型必须具有明确日费用预算")
        upper_tokens = model["max_input_tokens"] + model["max_output_tokens"]
        if min(body.run_budget.max_tokens, body.batch_budget.max_tokens) < upper_tokens:
            raise BackgroundStopped("运行及批次预算不足以预留模型请求")
        if model["billing_mode"] == "metered":
            if not all(
                model[key]
                for key in (
                    "input_price_per_million",
                    "output_price_per_million",
                    "price_source",
                )
            ):
                raise BackgroundStopped("后台模型价格未知")
            upper_cost = (
                model["max_input_tokens"] * model["input_price_per_million"]
                + model["max_output_tokens"] * model["output_price_per_million"]
            ) / 1000000
            if any(
                budget.max_cost_usd is None or budget.max_cost_usd < upper_cost
                for budget in (body.run_budget, body.batch_budget)
            ):
                raise BackgroundStopped("运行或批次费用预算不足以预留模型请求")
        return {
            "taskset_id": body.taskset_id,
            "taskset_report_sha256": marker["report_sha256"],
            "skills": skills,
            "model": model,
            "model_profile": {
                "id": profile.id,
                "name": profile.name,
                "revision": profile.revision,
            },
            "environment": environment(),
        }

    def _unchanged(self, value, c, *, inspect_contents=True):
        body = BackgroundInput.model_validate(value["input"])
        if (
            self._snapshot(body, c, inspect_contents=inspect_contents)
            != value["snapshot"]
        ):
            raise BackgroundStopped("授权冻结的审核、版本、模型或执行环境已变化")
        if _revision(value) != value["revision"]:
            raise BackgroundStopped("后台授权记录完整性无效")
        return body

    def prepare(self, identifier):
        """Scan frozen contents before the short dispatch transaction.

        A caller may pass this immediate receipt to claim. Claim still rechecks
        the revision, database approvals, model and environment under its locks.
        Later dispatch preflight is metadata-only: the Arena/worker must verify
        exact task-set and Skill bytes on their existing use-before paths.
        """
        with self.store.connect() as c:
            value = self.get(identifier, c)
            self._unchanged(value, c)
        return {
            "authorization_id": value["id"],
            "revision": value["revision"],
            "snapshot": value["snapshot"],
            "checked_at": self._now().isoformat(),
        }

    def create(self, body):
        body = BackgroundInput.model_validate(body).model_copy(deep=True)
        stamp = self._now()
        if stamp >= body.window_end:
            raise BackgroundStopped("后台窗口已经到期")
        with self.store.connect() as c:
            snapshot = self._snapshot(body, c)
            day = body.window_start.date().isoformat()
            budget = body.daily_budget.model_dump()
            required = self._reservation(body)
            self._capacity(
                {"budget": budget, "used": _empty_usage(), "reserved": _empty_usage()},
                required,
            )
            ledger = {
                "date": day,
                "budget": budget,
                "used": _empty_usage(),
                "reserved": _empty_usage(),
                "created_at": stamp.isoformat(),
                "updated_at": stamp.isoformat(),
            }
            c.execute(
                "INSERT INTO background_days VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (day, Jsonb(ledger)),
            )
            ledger = self.day(day, c, lock=True)
            if ledger["budget"] != budget:
                raise BackgroundConflict("该 UTC 日预算已冻结，不能通过其他授权修改")
            value = {
                "id": str(uuid4()),
                "name": body.name,
                "state": "draft",
                "created_at": stamp.isoformat(),
                "updated_at": stamp.isoformat(),
                "enabled_at": None,
                "disabled_at": None,
                "consumed_at": None,
                "evaluation_id": None,
                "date": day,
                "window_start": body.window_start.isoformat(),
                "window_end": body.window_end.isoformat(),
                "expires_at": body.window_end.isoformat(),
                "input": body.model_dump(mode="json"),
                "snapshot": snapshot,
                "history": [{"event": "created", "at": stamp.isoformat()}],
            }
            value["revision"] = _revision(value)
            c.execute(
                "INSERT INTO background_authorizations VALUES(%s,%s,%s)",
                (value["id"], value["state"], Jsonb(value)),
            )
        return value

    def enable(self, identifier, expected_revision):
        with self.store.connect() as c:
            value = self.get(identifier, c, lock=True)
            if expected_revision != value["revision"]:
                raise BackgroundConflict("后台授权版本冲突")
            if value["state"] != "draft":
                raise BackgroundConflict("只有未启用的草稿可以启用一次")
            if self._now() >= datetime.fromisoformat(value["window_end"]):
                raise BackgroundStopped("后台窗口已经到期")
            self._unchanged(value, c)
            if self.day(value["date"], c)["budget"] != value["input"]["daily_budget"]:
                raise BackgroundStopped("后台日预算绑定已变化")
            value.update(state="enabled", enabled_at=self._now().isoformat())
            return self._save(c, value, "enabled")

    def disable(self, identifier, expected_revision):
        with self.store.connect() as c:
            value = self.get(identifier, c, lock=True)
            if expected_revision != value["revision"]:
                raise BackgroundConflict("后台授权版本冲突")
            if value["state"] == "disabled":
                return value
            value.update(state="disabled", disabled_at=self._now().isoformat())
            # No reservation is refunded. Already dispatched actions can settle.
            return self._save(c, value, "disabled")

    @staticmethod
    def _reservation(body):
        budget = body.batch_budget
        return {
            "calls": budget.max_calls,
            "tokens": budget.max_tokens,
            "seconds": budget.max_seconds,
            "cost_usd": budget.max_cost_usd,
            "metered_cost_usd": budget.max_cost_usd or 0.0,
        }

    @staticmethod
    def _capacity(ledger, required):
        for key in USAGE_KEYS:
            if (
                ledger["used"][key] + ledger["reserved"][key] + required[key]
                > ledger["budget"]["max_" + key]
            ):
                raise BackgroundStopped("后台共享日预算不足")
        metered = (
            ledger["used"]["metered_cost_usd"]
            + ledger["reserved"]["metered_cost_usd"]
            + required["metered_cost_usd"]
        )
        if metered and (
            ledger["budget"]["max_cost_usd"] is None
            or metered > ledger["budget"]["max_cost_usd"]
        ):
            raise BackgroundStopped("后台共享日费用预算不足")

    def _refresh_day(self, c, day):
        ledger = self.day(day, c, lock=True)
        rows = c.execute(
            "SELECT state,data FROM background_allocations WHERE day=%s", (day,)
        ).fetchall()
        ledger["used"] = _sum_usage(
            row["data"]["actual"] for row in rows if row["state"] == "settled"
        )
        ledger["reserved"] = _sum_usage(
            row["data"]["reserved"] for row in rows if row["state"] == "reserved"
        )
        ledger["updated_at"] = self._now().isoformat()
        c.execute(
            "UPDATE background_days SET data=%s WHERE day=%s", (Jsonb(ledger), day)
        )
        return ledger

    def _window(self, value, *, waiting=False):
        current = self._now()
        start, end = (
            datetime.fromisoformat(value[key]) for key in ("window_start", "window_end")
        )
        if current < start and waiting:
            raise BackgroundWaiting("后台授权尚未到开始时间")
        if current.date().isoformat() != value["date"] or not start <= current < end:
            raise BackgroundStopped("后台授权已到期或不在原 UTC 日窗口内")

    def claim(self, connection, identifier, *, prepared=None):
        value = self.get(identifier, connection, lock=True)
        if value["state"] != "enabled" or value["evaluation_id"]:
            raise BackgroundConflict("后台授权未启用或已消费")
        self._window(value, waiting=True)
        if prepared is not None and (
            prepared.get("authorization_id") != value["id"]
            or prepared.get("revision") != value["revision"]
            or prepared.get("snapshot") != value["snapshot"]
            or not 0
            <= (
                self._now() - datetime.fromisoformat(prepared["checked_at"])
            ).total_seconds()
            <= 10
        ):
            raise BackgroundStopped("后台使用前检查回执已变化或过期")
        body = self._unchanged(value, connection, inspect_contents=prepared is None)
        ledger = self.day(value["date"], connection, lock=True)
        if ledger["budget"] != body.daily_budget.model_dump():
            raise BackgroundStopped("后台日预算绑定已变化")
        required = self._reservation(body)
        self._capacity(ledger, required)
        allocation = {
            "id": str(uuid4()),
            "authorization_id": value["id"],
            "evaluation_id": None,
            "date": value["date"],
            "claimed_at": self._now().isoformat(),
            "settled_at": None,
            "reserved": required,
            "actual": None,
        }
        connection.execute(
            "INSERT INTO background_allocations VALUES(%s,%s,NULL,%s,'reserved',%s)",
            (allocation["id"], value["id"], value["date"], Jsonb(allocation)),
        )
        self._refresh_day(connection, value["date"])
        value.update(state="consumed", consumed_at=allocation["claimed_at"])
        self._save(connection, value, "claimed", reservation_id=allocation["id"])
        return {
            "authorization_id": value["id"],
            "date": value["date"],
            "reservation_id": allocation["id"],
            "evaluation_input": {
                key: body.model_dump(mode="json")[key]
                for key in (
                    "taskset_id",
                    "skill_version_ids",
                    "model_profile_id",
                    "repeats",
                    "run_budget",
                    "batch_budget",
                )
            },
            "frozen_snapshot": value["snapshot"],
        }

    def attach(self, connection, claim, evaluation):
        identifier = evaluation["id"] if isinstance(evaluation, dict) else evaluation
        identifier = _identifier(identifier)
        value = self.get(claim["authorization_id"], connection, lock=True)
        row = connection.execute(
            "SELECT data FROM background_allocations WHERE id=%s AND authorization_id=%s",
            (_identifier(claim["reservation_id"]), value["id"]),
        ).fetchone()
        if (
            not row
            or row["data"]["evaluation_id"]
            or value["evaluation_id"]
            or value["state"] != "consumed"
        ):
            raise BackgroundConflict("后台授权与预留关联冲突")
        batch = connection.execute(
            "SELECT data FROM arena_evaluations WHERE id=%s", (identifier,)
        ).fetchone()
        if not batch:
            raise BackgroundConflict("后台批次必须在同一事务中已创建")
        batch = batch["data"]
        body = BackgroundInput.model_validate(value["input"])
        snapshot = value["snapshot"]
        expected_skills = {
            key: {k: v for k, v in skill.items() if k != "review_revision"}
            for key, skill in snapshot["skills"].items()
        }
        checks = {
            "split": "tuning",
            "taskset_id": body.taskset_id,
            "taskset_report_sha256": snapshot["taskset_report_sha256"],
            "model": snapshot["model"],
            "model_profile": snapshot["model_profile"],
            "environment": snapshot["environment"],
            "run_budget": body.run_budget.model_dump(),
            "batch_budget": body.batch_budget.model_dump(),
            "repeats": body.repeats,
            "skills": expected_skills,
            "skill_version_ids": body.skill_version_ids,
        }
        binding = {
            "authorization_id": value["id"],
            "reservation_id": claim["reservation_id"],
            "date": value["date"],
        }
        if any(batch.get(key) != expected for key, expected in checks.items()) or (
            batch.get("background") is not None and batch["background"] != binding
        ):
            raise BackgroundStopped("创建的批次不符合后台冻结授权")
        child_bindings = connection.execute(
            "SELECT data->'request'->'background' AS binding FROM runs WHERE data->'request'->'arena'->>'evaluation_id'=%s",
            (identifier,),
        ).fetchall()
        if batch.get("background") is not None and any(
            row["binding"] != binding for row in child_bindings
        ):
            raise BackgroundStopped("后台子运行授权绑定不一致")
        batch["background"] = binding
        connection.execute(
            "UPDATE arena_evaluations SET data=%s WHERE id=%s",
            (Jsonb(batch), identifier),
        )
        allocation = row["data"]
        allocation["evaluation_id"] = identifier
        connection.execute(
            "UPDATE background_allocations SET evaluation_id=%s,data=%s WHERE id=%s",
            (identifier, Jsonb(allocation), claim["reservation_id"]),
        )
        value["evaluation_id"] = identifier
        self._save(connection, value, "attached", evaluation_id=identifier)
        return batch

    def preflight(self, connection, authorization_id, evaluation_id):
        # A row-share lock serializes revocation with an admitted dispatch intent.
        row = connection.execute(
            "SELECT data FROM background_authorizations WHERE id=%s FOR SHARE",
            (_identifier(authorization_id),),
        ).fetchone()
        if not row:
            raise BackgroundStopped("后台授权不存在")
        value = row["data"]
        if value["state"] != "consumed" or value["evaluation_id"] != _identifier(
            evaluation_id
        ):
            raise BackgroundStopped("后台授权已撤销或批次关联无效")
        self._window(value)
        self._unchanged(value, connection, inspect_contents=False)
        allocation = connection.execute(
            "SELECT state,data FROM background_allocations WHERE authorization_id=%s AND evaluation_id=%s",
            (value["id"], evaluation_id),
        ).fetchone()
        if not allocation or allocation["state"] != "reserved":
            raise BackgroundStopped("后台日额度预留无效或已经结算")
        ledger = self.day(value["date"], connection, lock=True)
        if ledger["budget"] != value["input"]["daily_budget"]:
            raise BackgroundStopped("后台日预算绑定已变化")
        # Existing allocations need no second reservation, but overrun in another
        # batch cannot make an already admitted grant dispatch beyond the day cap.
        self._capacity(ledger, _empty_usage())
        return value

    def settle(self, connection, evaluation_id, batch=None):
        identifier = _identifier(evaluation_id)
        # The caller holds its batch lock. Never acquire any Run lock here.
        if batch is None:
            row = connection.execute(
                "SELECT data FROM arena_evaluations WHERE id=%s", (identifier,)
            ).fetchone()
            if not row:
                raise KeyError("找不到后台批次")
            batch = row["data"]
        if batch["id"] != identifier:
            raise BackgroundConflict("结算批次关联无效")
        row = connection.execute(
            "SELECT day,data FROM background_allocations WHERE evaluation_id=%s",
            (identifier,),
        ).fetchone()
        if not row:
            raise KeyError("后台批次没有日额度分配")
        self.day(row["day"], connection, lock=True)
        row = connection.execute(
            "SELECT state,data FROM background_allocations WHERE evaluation_id=%s FOR UPDATE",
            (identifier,),
        ).fetchone()
        allocation = row["data"]
        if row["state"] == "settled":
            return allocation
        if batch["state"] not in TERMINAL_BATCHES or batch["usage"]["unknown_calls"]:
            return allocation
        states = connection.execute(
            "SELECT state,data->'usage' AS usage FROM runs WHERE data->'request'->'arena'->>'evaluation_id'=%s",
            (identifier,),
        ).fetchall()
        unresolved = connection.execute(
            "SELECT count(*) AS count FROM actions JOIN runs ON runs.id=actions.run_id WHERE runs.data->'request'->'arena'->>'evaluation_id'=%s AND actions.status IN ('pending','unknown','acknowledged')",
            (identifier,),
        ).fetchone()["count"]
        if (
            not states
            or any(item["state"] not in TERMINAL_RUNS for item in states)
            or unresolved
        ):
            return allocation
        usage = batch["usage"]
        if any(
            usage[key] != sum(item["usage"][key] for item in states)
            for key in ("calls", "tokens", "reserved_tokens", "unknown_calls")
        ):
            raise BackgroundStopped("后台批次与子运行用量不一致，保留整批分配")
        for key in (
            "cost_usd",
            "reserved_cost_usd",
            "metered_cost_usd",
            "reserved_metered_cost_usd",
        ):
            costs = [item["usage"][key] for item in states]
            expected = None if any(cost is None for cost in costs) else sum(costs)
            if (
                (expected is None or usage[key] is None)
                and expected is not usage[key]
                or expected is not None
                and usage[key] is not None
                and not math.isclose(expected, usage[key], abs_tol=1e-8)
            ):
                raise BackgroundStopped("后台批次与子运行费用不一致，保留整批分配")
        elapsed = (
            self._now() - datetime.fromisoformat(allocation["claimed_at"])
        ).total_seconds()
        if elapsed < 0:
            raise BackgroundStopped("后台时钟早于分配记录，保留整批分配")
        actual = {
            "calls": usage["calls"],
            "tokens": usage["tokens"] + usage["reserved_tokens"],
            "seconds": math.ceil(elapsed),
            "cost_usd": None
            if usage["cost_usd"] is None or usage["reserved_cost_usd"] is None
            else usage["cost_usd"] + usage["reserved_cost_usd"],
            "metered_cost_usd": usage["metered_cost_usd"]
            + usage["reserved_metered_cost_usd"],
        }
        if any(
            type(actual[key]) is not int or actual[key] < 0 for key in USAGE_KEYS
        ) or any(
            actual[key] is not None
            and (not math.isfinite(actual[key]) or actual[key] < 0)
            for key in ("cost_usd", "metered_cost_usd")
        ):
            raise BackgroundStopped("后台结算用量无效，保留整批分配")
        allocation.update(actual=actual, settled_at=self._now().isoformat())
        connection.execute(
            "UPDATE background_allocations SET state='settled',data=%s WHERE evaluation_id=%s",
            (Jsonb(allocation), identifier),
        )
        self._refresh_day(connection, allocation["date"])
        return allocation
