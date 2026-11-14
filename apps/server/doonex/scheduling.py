"""One worker, finite authorizations and provable foreground dispatch priority."""

import time
from copy import deepcopy

from doonex.budget import BatchHalt, Halt, elapsed
from doonex.cleanup import cleanup_intent_unresolved, sandbox_context
from doonex.deadline import check_deadline
from doonex.store import OwnershipLost, dispatch_lock, execution_fenced, now

PREPARATION_SECONDS = 30.0


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
    def __init__(self, settings, store, *, execution_clock=None):
        from doonex.background import Background

        self.settings, self.store = settings, store
        self.execution_clock = execution_clock
        self.background = Background(settings, store, execution_clock=execution_clock)
        self.cleanup_blocked = False
        self._authorization_cursor = None
        self._maintenance_cursor = None

    def execution_bound(self, connection, run, *, deadline=None, starting=False):
        """Called with dispatch/Run locks; original scopes never renew on resume."""
        from doonex.arena import _planned_run_ids, digest
        from doonex.background import BackgroundStopped
        from doonex.budget import _locked_batch
        from doonex.execution_clock import ClockError

        check_deadline(deadline)
        clock = self.execution_clock
        if clock is None:
            return None
        evaluation_id = run["request"].get("arena", {}).get("evaluation_id")
        try:
            bounds = [
                clock.duration(
                    "run",
                    run["id"],
                    started_at=run["first_started_at"],
                    seconds=run["request"]["budget"]["max_seconds"],
                    binding=digest(run["request"], deadline=deadline),
                )
            ]
            batch = _locked_batch(connection, run, dispatch=not starting)
            check_deadline(deadline)
            if batch is not None:
                identifier, value = batch
                if starting and value.get("state") not in {"queued", "running"}:
                    raise BatchHalt("批次已停止，禁止初始化新工作", identifier)
                try:
                    planned = _planned_run_ids(value, deadline=deadline)
                except ValueError as error:
                    raise BatchHalt(str(error), identifier) from error
                if (
                    value.get("id") != identifier
                    or value.get("background") != run["request"].get("background")
                    or run["id"] not in planned
                ):
                    raise BatchHalt("批次时钟与子运行冻结归属不一致", identifier)
                if value.get("first_started_at") is not None:
                    bounds.append(
                        clock.duration(
                            "batch",
                            identifier,
                            started_at=value["first_started_at"],
                            seconds=value["batch_budget"]["max_seconds"],
                            binding=digest(
                                {
                                    key: value.get(key)
                                    for key in (
                                        "batch_budget",
                                        "fingerprint",
                                        "attempts",
                                        "background",
                                    )
                                },
                                deadline=deadline,
                            ),
                        )
                    )
                elif not starting or value["state"] != "queued":
                    raise BatchHalt("批次原首次时钟缺失", identifier)
                if value.get("background"):
                    bound = self.background.deadline(
                        connection,
                        value["background"]["authorization_id"],
                        identifier,
                        deadline=deadline,
                    )
                    if bound is not None:
                        bounds.append(bound)
            bound = clock.minimum(*bounds, deadline=deadline)
            self.check_execution_bound(bound, evaluation_id)
            return bound
        except BackgroundStopped as error:
            raise BatchHalt(str(error), evaluation_id) from error
        except ClockError as error:
            if error.scope.kind in {"batch", "allocation", "window", "day"}:
                raise BatchHalt(str(error), evaluation_id) from error
            raise Halt(str(error)) from error

    def check_execution_bound(self, bound, evaluation_id=None):
        from doonex.execution_clock import ClockError

        try:
            self.execution_clock.check(bound)
        except ClockError as error:
            if error.scope.kind in {"batch", "allocation", "window", "day"}:
                raise BatchHalt(str(error), evaluation_id) from error
            raise Halt(str(error)) from error

    def preflight(
        self, connection, run, *, yield_foreground=True, deadline: float | None = None
    ):
        """Recheck under dispatch/Run/batch locks using one cooperative deadline.

        Background metadata still reads local profiles, credentials and controller
        hashes. Checks after those reads and database waits prevent later admission;
        they cannot interrupt a blocked filesystem or database operation.
        """
        from doonex.background import BackgroundStopped
        from doonex.sdk import SchedulerYield

        check_deadline(deadline)
        bound = self.execution_bound(connection, run, deadline=deadline)
        if bound is not None:
            deadline = bound.at
        if cleanup_intent_unresolved(run):
            raise BatchHalt(
                "原清理意图尚未核实；禁止派发",
                run["request"].get("arena", {}).get("evaluation_id"),
            )
        binding = run["request"].get("background")
        if not binding:
            return bound
        evaluation_id = run["request"]["arena"]["evaluation_id"]
        fenced = execution_fenced(connection)
        check_deadline(deadline)
        if fenced:
            raise BatchHalt(
                "存在未核查的容器或模型传输进程；禁止后台派发", evaluation_id
            )
        try:
            self.background.preflight(
                connection,
                binding["authorization_id"],
                evaluation_id,
                deadline=deadline,
            )
        except BackgroundStopped as error:
            raise BatchHalt(str(error), evaluation_id) from None
        check_deadline(deadline)
        waiting = yield_foreground and foreground_waiting(connection)
        check_deadline(deadline)
        if waiting:
            raise SchedulerYield("前台任务等待；已停止新后台派发")
        if bound is not None:
            self.check_execution_bound(bound, evaluation_id)
        return bound

    def should_yield(self, run_id, *, deadline: float | None = None):
        # This is an advisory early yield. Actual admission uses the same
        # transaction lock as foreground enqueue in Ledger/pre-verification.
        check_deadline(deadline)
        with self.store.connect() as c:
            check_deadline(deadline)
            waiting = foreground_waiting(c)
            check_deadline(deadline)
            return waiting

    def save_yield(self, run_id, owner, receipt, workspace, *, deadline=None):
        from doonex.sandbox import Sandbox
        from doonex.workspace import Workspace

        check_deadline(deadline)
        if Sandbox().containers(
            run_id, deadline=deadline, **sandbox_context(self.store.get(run_id))
        ):
            self.cleanup_blocked = True
            raise ValueError("后台仍有容器，不能保存安全让出检查点")
        workspace_digest = Workspace.execution_digest(workspace.path, deadline=deadline)
        with self.store.locked(run_id, owner) as (c, run):
            check_deadline(deadline)
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

    def validate_receipt(self, run_id, receipt, workspace, *, deadline=None):
        from doonex.workspace import Workspace

        check_deadline(deadline)
        workspace_digest = Workspace.execution_digest(workspace.path, deadline=deadline)
        check_deadline(deadline)
        with self.store.locked(run_id, dispatch=True) as (c, run):
            check_deadline(deadline)
            # Resume authorization itself does not dispatch. A foreground task
            # arriving now is handled by the next atomic model/tool admission.
            self.preflight(c, run, yield_foreground=False, deadline=deadline)
            check_deadline(deadline)
            saved = run.get("scheduling") or {}
            if (
                saved.get("receipt") != receipt
                or saved.get("workspace_digest") != workspace_digest
                or saved.get("ledger") != ledger_checkpoint(run)
            ):
                return False
            if run["usage"]["reserved_tokens"] or run["usage"]["unknown_calls"]:
                return False
            pending = c.execute(
                "SELECT 1 FROM actions WHERE run_id=%s AND status IN ('pending','unknown') LIMIT 1",
                (run_id,),
            ).fetchone()
            check_deadline(deadline)
            return not pending

    def resume_yielded(self, run, workspace, *, deadline: float | None = None):
        from doonex.sandbox import Sandbox, SandboxError

        check_deadline(deadline)
        if cleanup_intent_unresolved(run):
            self.cleanup_blocked = True
            return False
        from doonex.sdk import SchedulerYield
        from doonex.workspace import Workspace

        # Unknown containers block resume; no cleanup or side effects are
        # assumed merely because the durable state says yielded.
        try:
            with self.store.connect() as c:
                dispatch_lock(c)
                current = c.execute(
                    "SELECT data FROM runs WHERE id=%s FOR UPDATE", (run["id"],)
                ).fetchone()["data"]
                check_deadline(deadline)
                if current.get("owner") != run.get("owner") or current.get(
                    "request"
                ) != run.get("request"):
                    self.cleanup_blocked = True
                    raise OwnershipLost("后台核查的执行归属或冻结请求已变化")
                if execution_fenced(c) or cleanup_intent_unresolved(current):
                    self.cleanup_blocked = True
                    return False
                if current["state"] != "yielded":
                    return False
                check_deadline(deadline)
            if Sandbox().containers(
                run["id"], deadline=deadline, **sandbox_context(run)
            ):
                self.cleanup_blocked = True
                raise ValueError("后台仍有容器，不能自动续接")
            check_deadline(deadline)
            waiting = self.should_yield(run["id"], deadline=deadline)
            check_deadline(deadline)
            if waiting:
                return False
            saved = run.get("scheduling") or {}
            workspace_digest = Workspace.execution_digest(
                workspace.path, deadline=deadline
            )
            check_deadline(deadline)
            waiting = self.should_yield(run["id"], deadline=deadline)
            check_deadline(deadline)
            if waiting:
                return False
            if saved.get("workspace_digest") != workspace_digest:
                raise ValueError("后台检查点已变化，须人工核查")
            if run.get("phase") != "verification" and not saved.get("receipt"):
                raise ValueError("缺少 SDK 调度回执，不能自动续接")
            valid = self.validate_receipt(
                run["id"], saved.get("receipt"), workspace, deadline=deadline
            )
            check_deadline(deadline)
            waiting = self.should_yield(run["id"], deadline=deadline)
            check_deadline(deadline)
            if waiting:
                return False
            if not valid:
                raise ValueError("后台自动续接证明无效")
            with self.store.locked(run["id"], dispatch=True) as (c, current):
                from doonex.budget import _locked_batch

                check_deadline(deadline)
                if current.get("owner") != run.get("owner") or current.get(
                    "request"
                ) != run.get("request"):
                    self.cleanup_blocked = True
                    raise OwnershipLost("后台核查的执行归属或冻结请求已变化")
                if current["state"] != "yielded" or current.get("scheduling") != saved:
                    return False
                if cleanup_intent_unresolved(current):
                    self.cleanup_blocked = True
                    return False
                _locked_batch(c, current, dispatch=True)
                check_deadline(deadline)
                self.preflight(c, current, deadline=deadline)
                check_deadline(deadline)
                if current["state"] != "yielded" or current.get("scheduling") != saved:
                    return False
                if saved.get("ledger") != ledger_checkpoint(current):
                    raise ValueError("后台账本或会话归属已变化，不能自动续接")
                if elapsed(current) >= current["request"]["budget"]["max_seconds"]:
                    raise BatchHalt(
                        "原后台运行时长已达上限",
                        current["request"]["arena"]["evaluation_id"],
                    )
                check_deadline(deadline)
                current.update(state="queued", reason="调度条件恢复，沿用会话与原预算")
                self.store.event(run["id"], "scheduling_resumed", {}, c)
                check_deadline(deadline)
            return True
        except SchedulerYield:
            return False
        except BatchHalt:
            raise
        except (Halt, ValueError, OSError, SandboxError) as error:
            from doonex.cleanup import controller_cleanup_unknown, merge_cleanup

            probe_error = isinstance(error, SandboxError)
            observed = merge_cleanup({}, error=error)
            if probe_error or controller_cleanup_unknown(observed):
                self.cleanup_blocked = True
            with self.store.locked(run["id"]) as (c, current):
                # A concurrent cancel cannot discard this probe's uncertainty.
                previous = current.get("recovery")
                recovery = deepcopy(previous or {})
                if probe_error:
                    for key, item in (
                        ("remaining", "unknown"),
                        ("errors", type(error).__name__),
                    ):
                        if item not in recovery.setdefault(key, []):
                            recovery[key].append(item)
                    recovery.setdefault("stopped", [])
                recovery = merge_cleanup(
                    recovery,
                    previous=previous,
                    error=error,
                )
                if probe_error or controller_cleanup_unknown(recovery):
                    self.cleanup_blocked = True
                    current["recovery"] = recovery
                if current["state"] != "yielded":
                    return False
                current.update(state="interrupted", reason=str(error))
                self.store.event(
                    run["id"], "scheduling_resume_refused", {"reason": str(error)}, c
                )
            return False

    def claim_next(self, *, deadline=None):
        from doonex.arena import Arena, EvaluationInput
        from doonex.background import BackgroundStopped, BackgroundWaiting

        # This bounds cooperative preparation work; the Run/batch/day guards
        # remain authoritative and no model request is admitted by this clock.
        deadline = (
            min(time.monotonic() + PREPARATION_SECONDS, deadline)
            if deadline is not None
            else time.monotonic() + PREPARATION_SECONDS
        )
        check_deadline(deadline)
        with self.store.connect() as c:
            if execution_fenced(c) or queued_work(c):
                return
        authorization = self.background.next_enabled(self._authorization_cursor)
        if authorization is None and self._authorization_cursor is not None:
            self._authorization_cursor = None
            authorization = self.background.next_enabled()
        if authorization is None:
            return None
        self._authorization_cursor = (authorization["created_at"], authorization["id"])
        try:
            # Full content inspection takes place without the admission lock.
            prepared = self.background.prepare(authorization["id"], deadline=deadline)
            arena = Arena(self.settings, self.store, deadline=deadline)
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
            arena_prepared = arena.prepare(body, deadline=deadline)
            with self.store.connect() as c:
                dispatch_lock(c)
                check_deadline(deadline)
                if execution_fenced(c) or queued_work(c):
                    return
                claim = self.background.claim(
                    c, authorization["id"], prepared=prepared, deadline=deadline
                )
                binding = {
                    key: claim[key]
                    for key in ("authorization_id", "date", "reservation_id")
                }
                result = arena.create(
                    body,
                    connection=c,
                    background=binding,
                    prepared=arena_prepared,
                    deadline=deadline,
                )
                check_deadline(deadline)
                self.background.attach(c, claim, result, deadline=deadline)
                check_deadline(deadline)
            return result
        except BackgroundWaiting as error:
            self.background.dispatch_status(authorization["id"], "waiting", str(error))
            return None
        except (BackgroundStopped, ValueError, KeyError, OSError) as error:
            from doonex.config import redact

            reason = (
                str(error)
                if isinstance(error, BackgroundStopped)
                else "后台冻结配置或建批检查失败：" + type(error).__name__
            )
            reason = redact(reason, (self.settings.token, self.settings.database_url))
            self.background.dispatch_status(authorization["id"], "blocked", reason)
            return None

    def maintain(self, *, deadline: float | None = None):
        """Inspect one candidate under a single finite preparation allowance."""
        from doonex.arena import Arena
        from doonex.background import TERMINAL_BATCHES, BackgroundStopped

        check_deadline(deadline)
        limit = time.monotonic() + PREPARATION_SECONDS
        deadline = limit if deadline is None else min(limit, deadline)
        check_deadline(deadline)
        identifier = self.background.next_reserved(self._maintenance_cursor)
        check_deadline(deadline)
        if identifier is None and self._maintenance_cursor is not None:
            self._maintenance_cursor = None
            identifier = self.background.next_reserved()
            check_deadline(deadline)
        if identifier is None:
            return
        # Advance even when this allocation must retain unknown reservations.
        self._maintenance_cursor = identifier
        with self.store.connect() as c:
            check_deadline(deadline)
            batch = self.background.maintenance_batch(c, identifier, deadline=deadline)
            check_deadline(deadline)
        if batch is None:
            return
        arena = Arena(self.settings, self.store, deadline=deadline)
        if batch["state"] in {"queued", "running", "interrupted"}:
            try:
                with self.store.connect() as c:
                    check_deadline(deadline)
                    dispatch_lock(c)
                    check_deadline(deadline)
                    current = self.background.maintenance_batch(
                        c, identifier, deadline=deadline
                    )
                    if current is None:
                        return
                    self.background.preflight(
                        c,
                        current["background"]["authorization_id"],
                        current["id"],
                        deadline=deadline,
                    )
                    check_deadline(deadline)
            except BackgroundStopped as error:
                check_deadline(deadline)
                arena.stop(batch["id"], "cancelling", str(error), deadline=deadline)
                check_deadline(deadline)
        # A stop can have just made this parent terminal. Recheck its binding
        # under the parent lock before the original day/allocation settlement.
        with self.store.connect() as c:
            check_deadline(deadline)
            current = self.background.maintenance_batch(
                c, identifier, lock=True, deadline=deadline
            )
            check_deadline(deadline)
            if current is not None and current["state"] in TERMINAL_BATCHES:
                self.background.settle(c, current["id"], current, deadline=deadline)
                # An expired settlement is rolled back by this original transaction.
                check_deadline(deadline)


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
