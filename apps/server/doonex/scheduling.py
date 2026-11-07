"""One worker, finite authorizations and provable foreground dispatch priority."""

from copy import deepcopy

from doonex.budget import BatchHalt, elapsed
from doonex.store import dispatch_lock, execution_fenced, now


class Yielded(BaseException):
    """The SDK or verifier has persisted a safe scheduling checkpoint."""


def ledger_checkpoint(run):
    """Only fields that must remain fixed while a scheduling yield is dormant."""
    return deepcopy(
        {
            "usage": run["usage"],
            "active_assignment": run.get("active_assignment"),
            "phase": run.get("phase"),
            "first_started_at": run.get("first_started_at"),
            "assignments": {
                role: {
                    key: value.get(key)
                    for key in ("id", "role", "session_generation", "usage")
                }
                for role, value in run.get("assignments", {}).items()
            },
        }
    )


def queued_work(connection, *, foreground=False):
    return bool(
        connection.execute(
            "SELECT 1 FROM runs r WHERE r.state='queued' "
            + ("AND NOT (r.data->'request' ? 'background') " if foreground else "")
            + "AND (NOT (r.data->'request' ? 'arena') OR EXISTS ("
            "SELECT 1 FROM arena_evaluations a WHERE a.id::text=r.data->'request'->'arena'->>'evaluation_id' "
            "AND a.state IN ('queued','running'))) LIMIT 1"
        ).fetchone()
    )


def foreground_waiting(connection):
    return queued_work(connection, foreground=True)


class Scheduler:
    def __init__(self, settings, store):
        from doonex.background import Background

        self.settings, self.store = settings, store
        self.background = Background(settings, store)
        self.cleanup_blocked = False

    def preflight(self, connection, run, *, yield_foreground=True):
        """Caller holds dispatch lock, then Run/batch locks, never external I/O."""
        from doonex.background import BackgroundStopped
        from doonex.sdk import SchedulerYield

        binding = run["request"].get("background")
        if not binding:
            return
        evaluation_id = run["request"]["arena"]["evaluation_id"]
        if execution_fenced(connection):
            raise BatchHalt("存在未核查的容器或模型传输进程；禁止后台派发", evaluation_id)
        try:
            self.background.preflight(
                connection, binding["authorization_id"], evaluation_id
            )
        except BackgroundStopped as error:
            raise BatchHalt(str(error), evaluation_id) from None
        if yield_foreground and foreground_waiting(connection):
            raise SchedulerYield("前台任务等待；已停止新后台派发")

    def should_yield(self, run_id):
        # This is an advisory early yield. Actual admission uses the same
        # transaction lock as foreground enqueue in Ledger/pre-verification.
        with self.store.connect() as c:
            return foreground_waiting(c)

    def save_yield(self, run_id, owner, receipt, workspace):
        from doonex.sandbox import Sandbox
        from doonex.workspace import Workspace

        if Sandbox().containers(run_id):
            self.cleanup_blocked = True
            raise ValueError("后台仍有容器，不能保存安全让出检查点")
        workspace_digest = Workspace.execution_digest(workspace.path)
        with self.store.locked(run_id, owner) as (c, run):
            if run["state"] not in {"running", "verifying"} or run.get(
                "requested_action"
            ):
                raise ValueError("用户停止或状态变化，不能保存自动续接检查点")
            pending = c.execute(
                "SELECT 1 FROM actions WHERE run_id=%s AND status IN ('pending','unknown') LIMIT 1",
                (run_id,),
            ).fetchone()
            if (
                pending
                or run["usage"]["reserved_tokens"]
                or run["usage"]["unknown_calls"]
            ):
                raise ValueError("存在未决用量或动作，不能自动续接")
            run["scheduling"] = {
                "receipt": receipt,
                "ledger": ledger_checkpoint(run),
                "workspace_digest": workspace_digest,
                "at": now(),
                "reason": "前台任务优先，后台已安全让出",
            }
            run.update(state="yielded", reason=run["scheduling"]["reason"])
            assignment = run.get("assignments", {}).get(run.get("active_assignment"))
            if assignment and assignment["state"] != "completed":
                assignment.update(state="yielded", resume_session=True)
            self.store.event(
                run_id,
                "scheduling_yielded",
                {
                    "phase": run["phase"],
                    "workspace_digest": run["scheduling"]["workspace_digest"],
                    "session_generation": (assignment or {}).get(
                        "session_generation", 0
                    ),
                },
                c,
            )

    def validate_receipt(self, run_id, receipt, workspace):
        from doonex.workspace import Workspace

        workspace_digest = Workspace.execution_digest(workspace.path)
        with self.store.locked(run_id, dispatch=True) as (c, run):
            # Resume authorization itself does not dispatch. A foreground task
            # arriving now is handled by the next atomic model/tool admission.
            self.preflight(c, run, yield_foreground=False)
            saved = run.get("scheduling") or {}
            if (
                saved.get("receipt") != receipt
                or saved.get("workspace_digest") != workspace_digest
                or saved.get("ledger") != ledger_checkpoint(run)
            ):
                return False
            if run["usage"]["reserved_tokens"] or run["usage"]["unknown_calls"]:
                return False
            return not c.execute(
                "SELECT 1 FROM actions WHERE run_id=%s AND status IN ('pending','unknown') LIMIT 1",
                (run_id,),
            ).fetchone()

    def resume_yielded(self, run, workspace):
        from doonex.sandbox import Sandbox, SandboxError
        from doonex.sdk import SchedulerYield
        from doonex.workspace import Workspace

        # Unknown containers block resume; no cleanup or side effects are
        # assumed merely because the durable state says yielded.
        try:
            if Sandbox().containers(run["id"]):
                self.cleanup_blocked = True
                raise ValueError("后台仍有容器，不能自动续接")
            if self.should_yield(run["id"]):
                return False
            saved = run.get("scheduling") or {}
            if saved.get("workspace_digest") != Workspace.execution_digest(
                workspace.path
            ):
                raise ValueError("后台检查点已变化，须人工核查")
            if run.get("phase") != "verification" and not saved.get("receipt"):
                raise ValueError("缺少 SDK 调度回执，不能自动续接")
            if not self.validate_receipt(run["id"], saved.get("receipt"), workspace):
                raise ValueError("后台自动续接证明无效")
            with self.store.locked(run["id"], dispatch=True) as (c, current):
                from doonex.budget import _locked_batch

                _locked_batch(c, current, dispatch=True)
                self.preflight(c, current)
                if current["state"] != "yielded" or current.get("scheduling") != saved:
                    return False
                if saved.get("ledger") != ledger_checkpoint(current):
                    raise ValueError("后台账本或会话归属已变化，不能自动续接")
                if elapsed(current) >= current["request"]["budget"]["max_seconds"]:
                    raise BatchHalt(
                        "原后台运行时长已达上限",
                        current["request"]["arena"]["evaluation_id"],
                    )
                current.update(state="queued", reason="调度条件恢复，沿用会话与原预算")
                self.store.event(run["id"], "scheduling_resumed", {}, c)
            return True
        except SchedulerYield:
            return False
        except BatchHalt:
            raise
        except (ValueError, OSError, SandboxError) as error:
            if isinstance(error, SandboxError):
                self.cleanup_blocked = True
            with self.store.locked(run["id"]) as (c, current):
                if current["state"] != "yielded":
                    return False
                current.update(state="interrupted", reason=str(error))
                self.store.event(
                    run["id"], "scheduling_resume_refused", {"reason": str(error)}, c
                )
            return False

    def claim_next(self):
        from doonex.arena import Arena, EvaluationInput
        from doonex.background import BackgroundStopped, BackgroundWaiting

        with self.store.connect() as c:
            if execution_fenced(c) or queued_work(c):
                return
        for authorization in self.background.list():
            if authorization["state"] != "enabled":
                continue
            try:
                # Full content inspection takes place without the admission lock.
                prepared = self.background.prepare(authorization["id"])
                arena = Arena(self.settings, self.store)
                body = EvaluationInput(
                    **{
                        k: authorization["input"][k]
                        for k in (
                            "taskset_id",
                            "skill_version_ids",
                            "model_profile_id",
                            "repeats",
                            "run_budget",
                            "batch_budget",
                        )
                    }
                )
                arena_prepared = arena.prepare(body)
                with self.store.connect() as c:
                    dispatch_lock(c)
                    if execution_fenced(c) or queued_work(c):
                        return
                    claim = self.background.claim(
                        c, authorization["id"], prepared=prepared
                    )
                    binding = {
                        key: claim[key]
                        for key in ("authorization_id", "date", "reservation_id")
                    }
                    result = arena.create(
                        body, connection=c, background=binding, prepared=arena_prepared
                    )
                    self.background.attach(c, claim, result)
                return result
            except BackgroundWaiting as error:
                self.background.dispatch_status(
                    authorization["id"], "waiting", str(error)
                )
                continue
            except (BackgroundStopped, ValueError, KeyError, OSError) as error:
                from doonex.config import redact

                reason = (
                    str(error)
                    if isinstance(error, BackgroundStopped)
                    else "后台冻结配置或建批检查失败：" + type(error).__name__
                )
                reason = redact(
                    reason, (self.settings.token, self.settings.database_url)
                )
                self.background.dispatch_status(authorization["id"], "blocked", reason)
                continue

    def maintain(self):
        from doonex.arena import Arena
        from doonex.background import BackgroundStopped

        arena = Arena(self.settings, self.store)
        for batch in arena.list():
            binding = batch.get("background")
            if not binding:
                continue
            if batch["state"] in {"queued", "running", "interrupted"}:
                try:
                    with self.store.connect() as c:
                        dispatch_lock(c)
                        self.background.preflight(
                            c, binding["authorization_id"], batch["id"]
                        )
                except BackgroundStopped as error:
                    arena.stop(batch["id"], "cancelling", str(error))
            batch = arena.get(batch["id"])
            if batch["state"] in {"complete", "cancelled", "budget_exceeded"}:
                with self.store.connect() as c:
                    row = c.execute(
                        "SELECT data FROM arena_evaluations WHERE id=%s FOR UPDATE",
                        (batch["id"],),
                    ).fetchone()
                    self.background.settle(c, batch["id"], row["data"])


def install_background_routes(app, settings, store, auth):
    from pydantic import Field

    from doonex.background import Background, BackgroundInput
    from doonex.models import StrictModel

    class Revision(StrictModel):
        expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")

    background = Background(settings, store)

    def describe(value):
        from doonex.arena import Arena

        if value.get("evaluation_id"):
            evaluation = Arena(settings, store).get(value["evaluation_id"])
            with store.connect() as c:
                yielded = c.execute(
                    "SELECT count(*) AS n FROM runs WHERE state='yielded' AND data->'request'->'arena'->>'evaluation_id'=%s",
                    (evaluation["id"],),
                ).fetchone()["n"]
            return {
                **value,
                "evaluation": {
                    key: evaluation[key] for key in ("id", "state", "reason", "usage")
                }
                | (
                    {"yielded_runs": yielded, "reason": "前台任务优先，后台已安全让出"}
                    if yielded
                    else {}
                ),
            }
        return value

    @app.get("/api/background/authorizations", dependencies=auth)
    def authorizations():
        return {"authorizations": [describe(value) for value in background.list()]}

    @app.post("/api/background/authorizations", dependencies=auth)
    def create(body: BackgroundInput):
        return background.create(body)

    @app.post("/api/background/authorizations/{identifier}/enable", dependencies=auth)
    def enable(identifier: str, body: Revision):
        return background.enable(identifier, body.expected_revision)

    @app.post("/api/background/authorizations/{identifier}/disable", dependencies=auth)
    def disable(identifier: str, body: Revision):
        return background.disable(identifier, body.expected_revision)

    @app.get("/api/background/days/{date}", dependencies=auth)
    def day(date: str):
        return background.day(date)
