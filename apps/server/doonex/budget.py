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
    def __init__(self, store, run_id, owner=None):
        self.store, self.run_id = store, run_id
        self.owner = owner

    def reserve(self, input_tokens, output_tokens):
        with self.store.locked(self.run_id, self.owner) as (c, run):
            guard(run)
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
            }
            action_id = self.store.action(self.run_id, "model", reservation, c)
            usage["calls"] += 1
            usage["reserved_tokens"] += tokens
            if cost is not None:
                usage["reserved_cost_usd"] += cost
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
                "SELECT status FROM actions WHERE id=%s FOR UPDATE",
                (reservation["id"],),
            ).fetchone()
            if existing["status"] != "pending":
                raise ValueError("模型请求已结算")
            usage, model = run["usage"], run["request"]["model"]
            valid = actual is not None and all(
                isinstance(actual.get(k), int)
                and not isinstance(actual[k], bool)
                and actual[k] >= 0
                for k in ("input_tokens", "output_tokens")
            )
            if not valid:
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
                    {"action_id": reservation["id"]},
                    c,
                )
                return
            usage["reserved_tokens"] -= reservation["tokens"]
            usage["tokens"] += actual["input_tokens"] + actual["output_tokens"]
            cost = None
            if reservation["cost"] is not None:
                usage["reserved_cost_usd"] = max(
                    0, usage["reserved_cost_usd"] - reservation["cost"]
                )
                cost = (
                    actual["input_tokens"] * model["input_price_per_million"]
                    + actual["output_tokens"] * model["output_price_per_million"]
                ) / 1_000_000
                usage["cost_usd"] += cost
            self.store.finish_action(
                reservation["id"],
                "completed",
                {"usage": actual, "estimated_cost_usd": cost},
                c,
            )
            self.store.event(
                self.run_id,
                "model_settled",
                {"usage": actual, "estimated_cost_usd": cost},
                c,
            )

    def tool_intent(self, command):
        with self.store.locked(self.run_id, self.owner) as (c, run):
            guard(run)
            action_id = self.store.action(self.run_id, "shell", {"command": command}, c)
            self.store.event(
                self.run_id,
                "tool_started",
                {"action_id": action_id, "command": command, "started_at": now()},
                c,
            )
        return action_id
