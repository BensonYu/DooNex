"""Pre-dispatch reservations persist across retries, crashes and resumes."""

import math
from datetime import UTC, datetime
from uuid import UUID

from psycopg.types.json import Jsonb

from doonex.models import model_for_role
from doonex.store import now, termination_unknown


class Halt(RuntimeError):
    pass


class BatchHalt(Halt):
    """A batch guard stopped dispatch; the worker owns its state transition."""

    def __init__(self, message, evaluation_id=None):
        self.evaluation_id = evaluation_id
        super().__init__(message)


def _amount(value):
    return type(value) in (int, float) and 0 <= value <= 1e18 and math.isfinite(value)


def _locked_batch(connection, run, *, dispatch=False):
    """Caller already holds the Run row lock; never acquire locks in reverse."""
    arena = run["request"].get("arena")
    if arena is None:
        return None
    try:
        evaluation_id = str(UUID(arena["evaluation_id"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise BatchHalt("竞技批次引用无效") from None
    row = connection.execute(
        "SELECT state,data FROM arena_evaluations WHERE id=%s FOR UPDATE",
        (evaluation_id,),
    ).fetchone()
    if not row or not isinstance(row["data"], dict):
        raise BatchHalt("竞技批次记录缺失", evaluation_id)
    batch = row["data"]
    usage = batch.get("usage")
    if (
        not isinstance(usage, dict)
        or any(
            type(usage.get(key)) is not int or usage[key] < 0
            for key in ("calls", "tokens", "reserved_tokens", "unknown_calls")
        )
        or any(
            key not in usage or (usage[key] is not None and not _amount(usage[key]))
            for key in ("cost_usd", "reserved_cost_usd")
        )
        or any(
            not _amount(usage.get(key))
            for key in ("metered_cost_usd", "reserved_metered_cost_usd")
        )
    ):
        raise BatchHalt("竞技批次账本缺失", evaluation_id)
    if dispatch:
        if row["state"] != "running" or batch.get("state") != "running":
            raise BatchHalt("竞技批次已停止派发新动作", evaluation_id)
        try:
            start = datetime.fromisoformat(batch["first_started_at"])
            if start.tzinfo is None:
                raise ValueError
            seconds = (datetime.now(UTC) - start).total_seconds()
            if seconds < 0:
                raise ValueError
            limits = batch["batch_budget"]
            if any(
                type(limits[key]) is not int or limits[key] < 1
                for key in ("max_calls", "max_tokens", "max_seconds")
            ) or (
                limits.get("max_cost_usd") is not None
                and not _amount(limits["max_cost_usd"])
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise BatchHalt(
                "竞技批次首次启动时钟或时长限制无效", evaluation_id
            ) from None
        if seconds >= limits["max_seconds"]:
            raise BatchHalt("竞技批次累计时长已达上限", evaluation_id)
    return evaluation_id, batch


def _save_batch(connection, batch):
    if batch is not None:
        evaluation_id, data = batch
        connection.execute(
            "UPDATE arena_evaluations SET data=%s WHERE id=%s",
            (Jsonb(data), evaluation_id),
        )


def mark_batch_unknown(connection, run, count):
    """Add pending model outcomes once, while Store holds their Run row lock.

    The caller marks only still-pending model actions unknown and updates Run
    and role counters in this same transaction. Existing unknown actions are
    excluded from ``count``. Reservations and first-start time stay intact.
    """
    if type(count) is not int or count < 0:
        raise ValueError("未知模型请求数量无效")
    if count == 0:
        return
    batch = _locked_batch(connection, run)
    if batch is not None:
        batch[1]["usage"]["unknown_calls"] += count
        _save_batch(connection, batch)


def _check_allowance(usage, limits, tokens, cost, halt):
    if usage["calls"] >= limits["max_calls"]:
        raise halt("模型调用次数已达上限")
    if usage["tokens"] + usage["reserved_tokens"] + tokens > limits["max_tokens"]:
        raise halt("剩余 token 预算不足以预留本次请求")
    if cost is not None and (
        limits.get("max_cost_usd") is None
        or usage.get("metered_cost_usd", usage["cost_usd"] or 0)
        + usage.get("reserved_metered_cost_usd", usage["reserved_cost_usd"] or 0)
        + cost
        > limits["max_cost_usd"]
    ):
        raise halt("剩余费用预算不足以预留本次请求")


def elapsed(run):
    start = run.get("first_started_at")
    return (
        (datetime.now(UTC) - datetime.fromisoformat(start)).total_seconds()
        if start
        else 0
    )


def guard(run):
    if run["state"] != "running":
        raise Halt("运行已停止派发新动作")
    if elapsed(run) >= run["request"]["budget"]["max_seconds"]:
        raise Halt("运行时长已达上限")


class Ledger:
    def __init__(
        self,
        store,
        run_id,
        owner=None,
        *,
        assignment_id=None,
        role=None,
        dispatch_check=None,
    ):
        self.store, self.run_id = store, run_id
        self.owner = owner
        self.dispatch_check = dispatch_check
        if (assignment_id is None) != (role is None):
            raise ValueError("角色与分配标识必须同时提供")
        if role is None:
            # Existing callers can bind the current role once. Never look it up
            # again at dispatch time: an old callback must not act as a new role.
            run = store.get(run_id)
            active = run.get("active_assignment")
            assignment = run.get("assignments", {}).get(active)
            if assignment:
                role, assignment_id = active, assignment["id"]
        self.assignment_id, self.role = assignment_id, role

    def attribution(self):
        return (
            {"assignment_id": self.assignment_id, "role": self.role}
            if self.role is not None
            else {}
        )

    def usages(self, run, *, dispatch=False):
        if self.role is None:
            if run.get("assignments"):
                raise Halt("角色分配已改变，旧回调不得继续派发或结算")
            return [run["usage"]]
        assignment = run.get("assignments", {}).get(self.role)
        if (
            not assignment
            or assignment["id"] != self.assignment_id
            or assignment["role"] != self.role
            or run.get("active_assignment") != self.role
            or (dispatch and assignment["state"] == "completed")
        ):
            raise Halt("角色分配已改变，旧回调不得继续派发或结算")
        return [run["usage"], assignment["usage"]]

    def reserve(self, input_tokens, output_tokens):
        with self.store.locked(self.run_id, self.owner, dispatch=True) as (c, run):
            if termination_unknown(c):
                raise Halt("存在终止状态不明的任务容器；禁止派发")
            guard(run)
            usages = self.usages(run, dispatch=True)
            batch = _locked_batch(c, run, dispatch=True)
            if self.dispatch_check:
                self.dispatch_check(c, run)
            model, limits, usage = (
                model_for_role(run["request"], self.role),
                run["request"]["budget"],
                run["usage"],
            )
            if (
                input_tokens < 0
                or output_tokens < 1
                or input_tokens > model["max_input_tokens"]
                or output_tokens > model["max_output_tokens"]
            ):
                raise Halt("模型上下文超出已配置的单次上限")
            subscription = model.get("billing_mode") == "subscription"
            if not subscription and (
                not model.get("input_price_per_million")
                or not model.get("output_price_per_million")
                or not model.get("price_source")
                or limits.get("max_cost_usd") is None
            ):
                raise Halt("模型价格或费用预算未知，无法遵守费用预算")
            tokens = input_tokens + output_tokens
            cost = (
                None
                if subscription
                else (
                    input_tokens * model["input_price_per_million"]
                    + output_tokens * model["output_price_per_million"]
                )
                / 1_000_000
            )
            _check_allowance(usage, limits, tokens, cost, Halt)
            if batch is not None:
                evaluation_id, data = batch
                _check_allowance(
                    data["usage"],
                    data["batch_budget"],
                    tokens,
                    cost,
                    lambda message: BatchHalt("竞技批次：" + message, evaluation_id),
                )
                usages.append(data["usage"])
            reservation = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tokens": tokens,
                "cost": cost,
                "model_id": model["model_id"],
                "provider": model["provider"],
                "api_mode": model.get("api_mode", "chat"),
                **self.attribution(),
                **({"evaluation_id": batch[0]} if batch is not None else {}),
            }
            action_id = self.store.action(self.run_id, "model", reservation, c)
            for item in usages:
                item["calls"] += 1
                item["reserved_tokens"] += tokens
                if subscription:
                    item["cost_usd"] = item["reserved_cost_usd"] = None
                if cost is not None:
                    item.setdefault("metered_cost_usd", item["cost_usd"] or 0)
                    item.setdefault(
                        "reserved_metered_cost_usd", item["reserved_cost_usd"] or 0
                    )
                    item["reserved_metered_cost_usd"] += cost
                    if item["reserved_cost_usd"] is not None:
                        item["reserved_cost_usd"] += cost
            _save_batch(c, batch)
            self.store.event(
                self.run_id,
                "model_reserved",
                {"action_id": action_id, **reservation},
                c,
            )
        return {"id": action_id, **reservation}

    def settle(self, reservation, actual):
        with self.store.locked(self.run_id, self.owner) as (c, run):
            batch = _locked_batch(c, run)
            existing = c.execute(
                "SELECT run_id,kind,status,data FROM actions WHERE id=%s FOR UPDATE",
                (reservation["id"],),
            ).fetchone()
            if (
                not existing
                or str(existing["run_id"]) != self.run_id
                or existing["kind"] != "model"
                or any(
                    existing["data"].get(key) != value
                    for key, value in self.attribution().items()
                )
                or (
                    self.role is None
                    and existing["data"].get("assignment_id") is not None
                )
            ):
                raise ValueError("模型请求不属于当前运行与角色")
            if existing["status"] != "pending":
                raise ValueError("模型请求已结算")
            # The persisted reservation is authoritative, not callback data.
            reservation = {**existing["data"], "id": reservation["id"]}
            if reservation.get("evaluation_id") != (batch[0] if batch else None):
                raise ValueError("模型请求不属于当前竞技批次")
            usages, model = self.usages(run), model_for_role(run["request"], self.role)
            if batch is not None:
                usages.append(batch[1]["usage"])
            valid = isinstance(actual, dict) and all(
                isinstance(actual.get(k), int)
                and not isinstance(actual[k], bool)
                and actual[k] >= 0
                for k in ("input_tokens", "output_tokens")
            )
            if not valid:
                for usage in usages:
                    usage["unknown_calls"] += 1
                _save_batch(c, batch)
                self.store.finish_action(
                    reservation["id"],
                    "unknown",
                    {"usage": None, "reservation_retained": True},
                    c,
                )
                self.store.event(
                    self.run_id,
                    "model_usage_unknown",
                    {"action_id": reservation["id"], **self.attribution()},
                    c,
                )
                return
            cost = None
            if reservation["cost"] is not None:
                cost = (
                    actual["input_tokens"] * model["input_price_per_million"]
                    + actual["output_tokens"] * model["output_price_per_million"]
                ) / 1_000_000
            for usage in usages:
                usage["reserved_tokens"] -= reservation["tokens"]
                usage["tokens"] += actual["input_tokens"] + actual["output_tokens"]
                if cost is not None:
                    usage.setdefault("metered_cost_usd", usage["cost_usd"] or 0)
                    usage.setdefault(
                        "reserved_metered_cost_usd", usage["reserved_cost_usd"] or 0
                    )
                    usage["reserved_metered_cost_usd"] = max(
                        0, usage["reserved_metered_cost_usd"] - reservation["cost"]
                    )
                    usage["metered_cost_usd"] += cost
                    if usage["cost_usd"] is not None:
                        usage["reserved_cost_usd"] = max(
                            0, usage["reserved_cost_usd"] - reservation["cost"]
                        )
                        usage["cost_usd"] += cost
            _save_batch(c, batch)
            self.store.finish_action(
                reservation["id"],
                "completed",
                {
                    "action_id": reservation["id"],
                    "usage": actual,
                    "estimated_cost_usd": cost,
                    **self.attribution(),
                },
                c,
            )
            self.store.event(
                self.run_id,
                "model_settled",
                {
                    "action_id": reservation["id"],
                    "usage": actual,
                    "estimated_cost_usd": cost,
                    **self.attribution(),
                },
                c,
            )

    def tool_intent(self, command, *, action_identity=None):
        with self.store.locked(self.run_id, self.owner, dispatch=True) as (c, run):
            if termination_unknown(c):
                raise Halt("存在终止状态不明的任务容器；禁止派发")
            guard(run)
            self.usages(run, dispatch=True)
            _locked_batch(c, run, dispatch=True)
            if self.dispatch_check:
                self.dispatch_check(c, run)
            action_id = self.store.action(
                self.run_id,
                "shell",
                {
                    "command": command,
                    **self.attribution(),
                    **({"sdk_action": action_identity} if action_identity else {}),
                },
                c,
            )
            self.store.event(
                self.run_id,
                "tool_started",
                {
                    "action_id": action_id,
                    "command": command,
                    "started_at": now(),
                    **self.attribution(),
                },
                c,
            )
        return action_id
