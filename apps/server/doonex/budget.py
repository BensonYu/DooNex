"""Pre-dispatch reservations persist across retries, crashes and resumes."""

from datetime import UTC, datetime

from doonex.store import now


class Halt(RuntimeError):
    pass


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
    def __init__(self, store, run_id, owner=None, *, assignment_id=None, role=None):
        self.store, self.run_id = store, run_id
        self.owner = owner
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
        with self.store.locked(self.run_id, self.owner) as (c, run):
            guard(run)
            usages = self.usages(run, dispatch=True)
            model, limits, usage = (
                run["request"]["model"],
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
            if usage["calls"] >= limits["max_calls"]:
                raise Halt("模型调用次数已达上限")
            if (
                usage["tokens"] + usage["reserved_tokens"] + tokens
                > limits["max_tokens"]
            ):
                raise Halt("剩余 token 预算不足以预留本次请求")
            if not subscription and (
                usage["cost_usd"] + usage["reserved_cost_usd"] + cost
                > limits["max_cost_usd"]
            ):
                raise Halt("剩余费用预算不足以预留本次请求")
            reservation = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tokens": tokens,
                "cost": cost,
                **self.attribution(),
            }
            action_id = self.store.action(self.run_id, "model", reservation, c)
            for item in usages:
                item["calls"] += 1
                item["reserved_tokens"] += tokens
                if cost is not None:
                    item["reserved_cost_usd"] += cost
            self.store.event(
                self.run_id,
                "model_reserved",
                {"action_id": action_id, **reservation},
                c,
            )
        return {"id": action_id, **reservation}

    def settle(self, reservation, actual):
        with self.store.locked(self.run_id, self.owner) as (c, run):
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
            usages, model = self.usages(run), run["request"]["model"]
            valid = actual is not None and all(
                isinstance(actual.get(k), int)
                and not isinstance(actual[k], bool)
                and actual[k] >= 0
                for k in ("input_tokens", "output_tokens")
            )
            if not valid:
                for usage in usages:
                    usage["unknown_calls"] += 1
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
                    usage["reserved_cost_usd"] = max(
                        0, usage["reserved_cost_usd"] - reservation["cost"]
                    )
                    usage["cost_usd"] += cost
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

    def tool_intent(self, command):
        with self.store.locked(self.run_id, self.owner) as (c, run):
            guard(run)
            self.usages(run, dispatch=True)
            action_id = self.store.action(
                self.run_id, "shell", {"command": command, **self.attribution()}, c
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
