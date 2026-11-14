"""One independently running worker. Browser requests never run task code."""

import signal
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from doonex.budget import Halt, Ledger, elapsed, guard
from doonex.cleanup import (
    begin_cleanup_intent,
    cleanup_intent_matches,
    cleanup_intent_unresolved,
    finish_cleanup_intent,
    merge_cleanup,
    operation_context,
    sandbox_context,
)
from doonex.config import Settings, redact
from doonex.deadline import ExecutionDeadlineExceeded, check_deadline, remaining_time
from doonex.models import model_for_role, run_roles
from doonex.store import OwnershipLost, Store, now

WORKER_LOCK = 4142061106
ACTIVE = {"running", "verifying", "pausing"}
MAX_YIELDED_RUNS = 16
MAX_RECOVERY_RUNS = 64
MAX_STARTUP_BATCHES = 64


class PermissionViolation(RuntimeError):
    """An explicit request targeted a controller-only mount or host secret."""


def run_directory(settings, run_id):
    # IDs are database UUIDs; callers validate before constructing paths.
    return settings.home / "runs" / run_id


def exception_cause(error, expected):
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, expected):
            return error
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return None


def stop_containers(sandbox, run_id, *, context, previous=None, error=None):
    try:
        from doonex.controller_operations import current_registry
        from doonex.store import execution_fenced

        registry = current_registry()
        with registry.store.connect() as connection:
            blocked = execution_fenced(connection)
        if blocked:
            result = {
                "remaining": ["unknown"],
                "stopped": [],
                "errors": ["execution_fenced"],
            }
        else:
            result = sandbox.stop_run(run_id, **context)
    except Exception as cleanup_error:  # noqa: BLE001 - preserve unknown termination state
        result = {
            "remaining": ["unknown"],
            "stopped": [],
            "errors": [type(cleanup_error).__name__],
        }
        result = merge_cleanup(result, error=cleanup_error)
    return merge_cleanup(result, previous=previous, error=error)


def check_sandbox_cleanup(result):
    from doonex.sandbox import SandboxError

    if result.get("cleanup_error") or result.get("cleanup_unknown"):
        error = SandboxError("任务容器或本机控制进程清理失败，不能确认执行已经终止")
        error.cleanup_unknown = bool(result.get("cleanup_unknown"))
        error.cleanup_evidence = result.get("cleanup_evidence") or {}
        raise error


def record_probe_failure(store, run_id, error):
    """A read-only Docker probe may itself leave a local process unresolved."""
    with store.locked(run_id) as (c, current):
        current["recovery"] = merge_cleanup(
            {"remaining": ["unknown"], "stopped": [], "errors": [type(error).__name__]},
            previous=current.get("recovery"),
            error=error,
        )
        store.event(run_id, "container_probe_failed", current["recovery"], c)


def get_workspace(run, directory):
    from doonex.workspace import Workspace

    return Workspace(
        path=directory / "workspace",
        repository=(
            None
            if run["request"].get("purpose") == "skill_generation"
            else Path(run["request"]["repo_path"])
        ),
        base_commit=run["request"]["base_commit"],
        baseline_path=directory / "baseline" if run.get("baseline_digest") else None,
        baseline_digest=run.get("baseline_digest"),
        operation_context=operation_context(run),
    )


class Worker:
    def __init__(self, settings: Settings):
        from doonex.execution_clock import ExecutionClock
        from doonex.scheduling import Scheduler

        self.settings, self.store = settings, Store(settings.database_url)
        self.execution_clock = ExecutionClock()
        self.scheduler = Scheduler(
            settings, self.store, execution_clock=self.execution_clock
        )
        self.lock_connection = None
        self.stopping = False
        self.cleanup_blocked = False
        self.owner = str(uuid4())

    def lease(self):
        if self.lock_connection is None:
            raise OwnershipLost("执行连接已失效")
        try:
            held = self.lock_connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND pid=pg_backend_pid() AND classid=%s AND objid=%s "
                "AND objsubid=1 AND granted) AS held",
                (WORKER_LOCK >> 32, WORKER_LOCK & 0xFFFFFFFF),
            ).fetchone()
            if not held["held"]:
                raise OwnershipLost("worker 执行锁已释放")
        except Exception as error:
            raise OwnershipLost("执行连接已失效") from error

    def operation_context(self, run, *, parent_action_id=None, sandbox=False):
        self.lease()
        if run.get("owner") != self.owner:
            raise OwnershipLost("执行权已移交，旧 worker 不得借用新 owner")
        return (sandbox_context if sandbox else operation_context)(
            run, parent_action_id=parent_action_id
        )

    def fence(self):
        if self.lock_connection is None or self.stopping:
            raise Halt("执行者已停止")
        self.lease()

    def stop_requested(self, run_id):
        self.fence()
        run = self.store.get(run_id)
        batch_stopped = False
        if run["request"].get("arena"):
            from doonex.arena import Arena

            batch_stopped = Arena(self.settings, self.store).stopped(
                run["request"]["arena"]["evaluation_id"]
            )
        return (
            run["state"] != "running"
            or bool(run.get("safety_violation"))
            or elapsed(run) >= run["request"]["budget"]["max_seconds"]
            or batch_stopped
        )

    @contextmanager
    def controller_actor(self):
        from doonex.controller_operations import (
            ControllerRegistry,
            bind_registry,
            fresh_actor,
        )
        from doonex.store import external_execution_fenced

        self.lease()
        registry = ControllerRegistry(
            self.store,
            actor=fresh_actor("worker"),
            check_owner=self.lease,
            check_other_fences=external_execution_fenced,
        )
        with bind_registry(registry):
            yield registry

    def reconcile(self, *, deadline=None):
        from doonex.controller_operations import current_registry
        from doonex.sandbox import Sandbox
        from doonex.scheduling import PREPARATION_SECONDS
        from doonex.store import execution_fenced

        limit = time.monotonic() + PREPARATION_SECONDS
        deadline = limit if deadline is None else min(deadline, limit)
        with self.controller_actor():
            sandbox = Sandbox()
            for run in self.store.scheduling_runs(
                capacity=MAX_RECOVERY_RUNS,
                states=ACTIVE | {"interrupted"},
                deadline=deadline,
            ):
                check_deadline(deadline)
                with self.store.locked(run["id"], dispatch=True) as (c, current):
                    check_deadline(deadline)
                    self.lease()
                    if (
                        current.get("owner") != run.get("owner")
                        or current.get("request") != run.get("request")
                        or current.get("state") != run.get("state")
                    ):
                        raise OwnershipLost("恢复核查期间运行归属或状态已变化")
                    blocked = execution_fenced(c)
                    pending_intent = cleanup_intent_unresolved(current)
                    self.store.mark_pending_unknown(run["id"], c, current)
                    current.update(
                        state="interrupted",
                        owner=self.owner,
                        reason="执行服务中断；已保留变更，恢复前须核对未决动作",
                    )
                    assignment = current.get("assignments", {}).get(
                        current.get("active_assignment")
                    )
                    if assignment and assignment["state"] != "completed":
                        assignment["state"] = "interrupted"
                    if blocked or pending_intent:
                        current["recovery"] = merge_cleanup(
                            {
                                "remaining": ["unknown"],
                                "stopped": [],
                                "errors": [
                                    "execution_fenced"
                                    if blocked
                                    else "cleanup_intent_unresolved"
                                ],
                            },
                            previous=current.get("recovery"),
                        )
                        intent = None
                        self.cleanup_blocked = True
                    else:
                        intent = begin_cleanup_intent(
                            current, source="reconcile", actor=current_registry().actor
                        )
                        previous = current.get("recovery")
                    self.store.event(
                        run["id"], "interrupted", {"cleanup_intent": intent}, c
                    )
                if intent is None:
                    continue
                # A committed intent remains unresolved if preparation expired.
                # Do not begin a fresh external cleanup after that boundary.
                check_deadline(deadline)
                result = stop_containers(
                    sandbox,
                    run["id"],
                    context=self.operation_context(current, sandbox=True),
                    previous=previous,
                )
                with self.store.locked(run["id"], self.owner) as (c, latest):
                    self.lease()
                    if not cleanup_intent_matches(latest, intent):
                        self.cleanup_blocked = True
                        continue
                    result = merge_cleanup(result, previous=latest.get("recovery"))
                    finish_cleanup_intent(latest, intent, result)
                    latest["recovery"] = result
                    if result.get("remaining") or result.get("errors"):
                        self.cleanup_blocked = True
                        latest.update(
                            state="interrupted", reason="任务容器终态未核实；须核查"
                        )
                    elif latest.get("requested_action") in {"cancel", "pause"}:
                        latest.update(
                            state="cancelled"
                            if latest["requested_action"] == "cancel"
                            else "paused",
                            reason="已取消"
                            if latest["requested_action"] == "cancel"
                            else "已暂停，保留工作区与会话",
                        )
                        if latest["state"] == "cancelled":
                            latest["ended_at"] = now()
                    else:
                        latest.update(
                            state="interrupted",
                            reason="执行服务中断；恢复前须核对未决动作",
                        )
                    assignment = latest.get("assignments", {}).get(
                        latest.get("active_assignment")
                    )
                    if assignment and assignment["state"] != "completed":
                        assignment["state"] = latest["state"]
                    self.store.event(run["id"], "cleanup", result, c)
                check_deadline(deadline)

    def execute(self, run_id):
        with self.controller_actor():
            return self._execute(run_id)

    def _execute(self, run_id):
        from doonex.evidence import Evidence
        from doonex.review import Review
        from doonex.sandbox import Sandbox, SandboxError
        from doonex.scheduling import Yielded
        from doonex.sdk import (
            ModelCallError,
            ReviewReportError,
            SchedulerYield,
            SessionRecoveryRequired,
            SessionScheduling,
            ToolOutcomeUnknown,
            parse_review_report,
            run_session,
        )
        from doonex.workspace import Workspace, read_files

        directory = run_directory(self.settings, run_id)
        sandbox = Sandbox()
        run = self.store.get(run_id)
        request = run["request"]
        from doonex.library import Library

        generation_service = None
        arena = None
        execution_bound = None
        settlement_deadline = None
        role_keys = {}
        secrets = (self.settings.token, self.settings.database_url)

        def event(kind, payload):
            self.lease()
            with self.store.locked(run_id, self.owner) as (c, _):
                self.store.event(run_id, kind, redact(payload, secrets), c)

        try:
            with self.store.locked(run_id, dispatch=True) as (c, current):
                if current["state"] != "queued":
                    return
                if current["request"] != request:
                    raise OwnershipLost("领取期间冻结请求已变化")
                if cleanup_intent_unresolved(current):
                    current.update(
                        state="interrupted", reason="原清理意图尚未核实；禁止领取"
                    )
                    self.cleanup_blocked = True
                    self.store.event(run_id, "cleanup_intent_unresolved", {}, c)
                    return
                from doonex.store import execution_fenced

                if execution_fenced(c):
                    raise SessionRecoveryRequired(
                        "容器或模型传输进程尚未完成核查，禁止领取运行"
                    )
                self.fence()
                current.update(state="running", owner=self.owner, reason="Agent 执行中")
                if current["first_started_at"] is None:
                    current["first_started_at"] = now()
                self.store.event(run_id, "running", {}, c)

            # Map only the committed original start, under the established
            # dispatch -> Run -> batch -> grant/day order. The clock belongs
            # to this Worker, including later execute calls after a yield.
            with self.store.locked(run_id, self.owner, dispatch=True) as (c, current):
                execution_bound = self.scheduler.execution_bound(
                    c, current, starting=True
                )
                execution_deadline = execution_bound.at

            def execution_fence():
                self.fence()
                self.scheduler.check_execution_bound(
                    execution_bound, request.get("arena", {}).get("evaluation_id")
                )
                check_deadline(execution_deadline)

            def remaining_execution():
                nonlocal execution_deadline, execution_bound
                with self.store.locked(run_id, self.owner, dispatch=True) as (
                    c,
                    current,
                ):
                    execution_bound = self.scheduler.execution_bound(
                        c, current, deadline=execution_deadline
                    )
                    execution_deadline = execution_bound.at
                return remaining_time(execution_deadline)

            def dispatch_check(connection, current):
                nonlocal execution_deadline, execution_bound
                execution_fence()
                bound = self.scheduler.preflight(
                    connection, current, deadline=execution_deadline
                )
                if bound is not None:
                    execution_bound = bound
                    execution_deadline = bound.at
                execution_fence()

            execution_fence()
            for role in run_roles(request):
                role_keys[role] = self.settings.credential(
                    model_for_role(request, role), deadline=execution_deadline
                )
                secrets = (
                    *role_keys.values(),
                    self.settings.token,
                    self.settings.database_url,
                )
                check_deadline(execution_deadline)
            library = Library(self.settings, self.store, deadline=execution_deadline)
            if request.get("purpose") == "skill_generation":
                from doonex.generation import Generation

                generation_service = Generation(
                    self.settings, self.store, deadline=execution_deadline
                )
            if request.get("arena"):
                from doonex.arena import Arena
                from doonex.budget import BatchHalt

                arena = Arena(self.settings, self.store, deadline=execution_deadline)
                if not arena.start(
                    request["arena"]["evaluation_id"], deadline=execution_deadline
                ):
                    raise BatchHalt("评测批次已停止", request["arena"]["evaluation_id"])
                remaining_execution()
            check_deadline(execution_deadline)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if generation_service:
                generation_service.validate_run(
                    self.store.get(run_id), directory, deadline=execution_deadline
                )
            if not all(role_keys.values()):
                raise ValueError("worker 缺少运行所引用的模型凭据环境变量")
            skill_context = (
                library.skill_context(request["skill"], deadline=execution_deadline)
                if request.get("skill")
                else None
            )
            task_context = None
            verification_context = None
            if arena:
                binding = request["arena"]
                task_context = library.tasksets.context(
                    binding["taskset_id"],
                    binding["report_sha256"],
                    binding["task_id"],
                    deadline=execution_deadline,
                )
                if (
                    task_context.metadata["source_digest"] != binding["source_digest"]
                    or task_context.metadata["verifier_digest"]
                    != binding["verifier_digest"]
                ):
                    raise ValueError("任务集冻结上下文与运行不一致")
                if (
                    request["checks"] != [task_context.metadata["verifier_command"]]
                    or request.get("visible_checks")
                    != task_context.metadata["visible_checks"]
                ):
                    raise ValueError("运行检查器绑定已变化")
                verification_context = {
                    key: binding[key]
                    for key in (
                        "taskset_id",
                        "task_id",
                        "report_sha256",
                        "verifier_digest",
                    )
                }
            if generation_service:
                workspace = generation_service.prepare_workspace(
                    self.store.get(run_id), directory, deadline=execution_deadline
                )
                with self.store.locked(run_id, self.owner) as (_, current):
                    current["baseline_digest"] = workspace.baseline_digest
            elif not (directory / "workspace").exists():
                if task_context:
                    from doonex.workspace import manifest, manifest_digest, write_files

                    files = read_files(
                        task_context.source_path,
                        exclude_generated=False,
                        deadline=execution_deadline,
                    )
                    baseline_digest = manifest_digest(
                        manifest(files, deadline=execution_deadline),
                        deadline=execution_deadline,
                    )
                    for target in (directory / "baseline", directory / "workspace"):
                        if target.exists():
                            if (
                                Workspace.snapshot_digest(
                                    target, deadline=execution_deadline
                                )
                                != baseline_digest
                            ):
                                raise ValueError("不完整的起始快照已变化，不能自动覆盖")
                        else:
                            write_files(files, target, deadline=execution_deadline)
                            for relative in task_context.metadata["source_directories"]:
                                check_deadline(execution_deadline)
                                (target / relative).mkdir(
                                    mode=0o700, parents=True, exist_ok=True
                                )
                    workspace = Workspace(
                        directory / "workspace",
                        Path(request["repo_path"]),
                        request["base_commit"],
                        directory / "baseline",
                        baseline_digest,
                    )
                else:
                    workspace = Workspace.create(
                        Path(request["repo_path"]),
                        request["base_commit"],
                        directory / "workspace",
                        baseline_path=directory / "baseline",
                        deadline=execution_deadline,
                        operation_context=self.operation_context(
                            self.store.get(run_id)
                        ),
                    )
                checkpoint_digest = (
                    Workspace.execution_digest(
                        workspace.path, deadline=execution_deadline
                    )
                    if arena
                    else None
                )
                with self.store.locked(run_id, self.owner) as (_, current):
                    current["baseline_digest"] = workspace.baseline_digest
                    if arena:
                        current["workspace_checkpoint_digest"] = checkpoint_digest
            else:
                workspace = get_workspace(self.store.get(run_id), directory)
                Workspace.scan(workspace.path, deadline=execution_deadline)
                if run.get("assignments") and workspace.baseline_digest is None:
                    raise ValueError("持久化基线尚未绑定，须核对后重新创建任务")

            workspace = replace(
                workspace,
                operation_context=self.operation_context(self.store.get(run_id)),
            )

            def execute_role(role, shell_path, *, read_only=False):
                remaining_execution()
                selected_model = model_for_role(request, role)
                with self.store.locked(run_id, self.owner) as (c, current):
                    guard(current)
                    assignment = current.get("assignments", {}).get(role)
                    if assignment:
                        if current.get("active_assignment") != role:
                            raise Halt("角色分配已改变")
                        if assignment["state"] == "completed":
                            raise RuntimeError("已完成的角色不能再次执行")
                        assignment["state"] = "running"
                        assignment["started_at"] = assignment.get("started_at") or now()
                        self.store.event(
                            run_id,
                            "assignment_started",
                            {"assignment_id": assignment["id"], "role": role},
                            c,
                        )
                    generation = (assignment or current).get("session_generation", 0)
                    session_dir = directory / (
                        f"session-{role}-{generation}"
                        if assignment
                        else f"session-{generation}"
                    )
                    resume = (assignment or current).get("resume_session", False)
                    resume = resume and session_dir.exists()
                ledger = Ledger(
                    self.store,
                    run_id,
                    self.owner,
                    dispatch_check=dispatch_check,
                )
                attribution = ledger.attribution()
                responses_transport = None
                if selected_model.get("responses_transport") == "isolated_direct_v1":
                    from doonex.responses_binding import ResponsesBinding

                    responses_transport = ResponsesBinding(
                        self.settings,
                        self.store,
                        run_id=run_id,
                        owner=self.owner,
                        check_owner=self.lease,
                        check_dispatch=execution_fence,
                        model=selected_model,
                        api_key=role_keys[role],
                        execution_deadline=execution_deadline,
                    )

                def role_stop():
                    check_deadline(execution_deadline)
                    if self.stop_requested(run_id):
                        return True
                    current = self.store.get(run_id)
                    if assignment:
                        active = current.get("assignments", {}).get(role, {})
                        return (
                            current.get("active_assignment") != role
                            or active.get("id") != assignment["id"]
                            or active.get("state") != "running"
                        )
                    return False

                def role_event(kind, payload):
                    event(
                        kind,
                        {
                            **payload,
                            **attribution,
                            **(
                                {"session_generation": generation}
                                if generation_service
                                else {}
                            ),
                        },
                    )

                def before_model(
                    input_tokens, output_tokens, *, responses_request_id=None
                ):
                    execution_fence()
                    if role_stop():
                        raise Halt("运行或角色已停止派发新动作")
                    intent = None
                    if responses_transport is not None:
                        responses_transport.execution_deadline = min(
                            responses_transport.execution_deadline, execution_deadline
                        )
                        intent = responses_transport.prepare_intent(
                            responses_request_id
                        )
                    elif responses_request_id is not None:
                        raise ValueError(
                            "Direct model cannot reserve a helper identity"
                        )
                    reservation = ledger.reserve(
                        input_tokens, output_tokens, helper_intent=intent
                    )
                    if responses_transport is not None:
                        responses_transport.execution_deadline = min(
                            responses_transport.execution_deadline, execution_deadline
                        )
                    return reservation

                def after_model(reservation, usage):
                    self.lease()
                    if usage is None and responses_transport is not None:
                        responses_transport.abandon_uninvoked(reservation)
                    ledger.settle(reservation, usage)

                def shell(command, action_identity=None):
                    action_id = None
                    try:
                        if generation_service:
                            raise PermissionViolation("候选生成没有 shell 工具权限")
                        execution_fence()
                        if role_stop():
                            raise Halt("运行或角色已停止派发新动作")
                        # These references have no legitimate model-tool purpose.
                        # This detects explicit probes; Docker remains the actual
                        # boundary for indirect/obfuscated commands and scripts.
                        if any(
                            target in command
                            for target in (
                                "/verifier",
                                "/var/run/docker.sock",
                                "/.ssh/",
                                str(self.settings.home),
                            )
                        ):
                            with self.store.locked(run_id, self.owner) as (c, current):
                                current["safety_violation"] = True
                                self.store.event(
                                    run_id,
                                    "permission_denied",
                                    {
                                        "reason": "模型工具不可访问检查器或宿主秘密",
                                        **attribution,
                                    },
                                    c,
                                )
                            raise PermissionViolation(
                                "模型请求触及未授权的检查器或宿主秘密路径"
                            )
                        # Admission includes the intent commit: an exception may
                        # leave a durable pending action even before its ID returns.
                        action_id = ledger.tool_intent(
                            command, action_identity=action_identity
                        )
                        remaining = remaining_execution()
                        options = {"read_only": True} if read_only else {}
                        if role == "executor" and skill_context:
                            fresh_skill = library.skill_context(
                                request["skill"], deadline=execution_deadline
                            )
                            options["skill_path"] = fresh_skill.path
                        result = sandbox.run(
                            shell_path,
                            command,
                            min(60, remaining),
                            run_id,
                            should_stop=role_stop,
                            deadline=execution_deadline,
                            **self.operation_context(
                                self.store.get(run_id),
                                parent_action_id=action_id,
                                sandbox=True,
                            ),
                            **options,
                        )
                        result = redact(result, secrets)
                        check_sandbox_cleanup(result)
                        self.lease()
                        if read_only:
                            saved_handoff = self.store.get(run_id).get("handoff")
                            if not Review.inspect(
                                workspace,
                                directory,
                                saved_handoff,
                                deadline=execution_deadline,
                            )["valid"]:
                                raise RuntimeError(
                                    "审查工具结束时交接材料或代码快照发生变化"
                                )
                        else:
                            Workspace.scan(shell_path, deadline=execution_deadline)
                            if skill_context:
                                library.skill_context(
                                    request["skill"], deadline=execution_deadline
                                )
                        checkpoint_digest = (
                            Workspace.execution_digest(
                                workspace.path, deadline=execution_deadline
                            )
                            if arena and not read_only
                            else None
                        )
                        # Snapshot work must finish before recording completion,
                        # without preventing a concurrent stop request from committing.
                        self.lease()
                        with self.store.locked(run_id, self.owner) as (c, current):
                            self.store.finish_action(
                                action_id,
                                "completed",
                                result,
                                connection=c,
                                owner=self.owner,
                            )
                            if checkpoint_digest is not None:
                                current["workspace_checkpoint_digest"] = (
                                    checkpoint_digest
                                )
                            self.store.event(
                                run_id,
                                "tool_finished",
                                {"action_id": action_id, **result, **attribution},
                                c,
                            )
                        return result
                    except BaseException as error:
                        if action_id is None and isinstance(
                            error, (Halt, PermissionViolation, OwnershipLost)
                        ):
                            raise
                        try:
                            if action_id is not None:
                                self.lease()
                                with self.store.locked(run_id, self.owner) as (c, _):
                                    action = c.execute(
                                        "SELECT status FROM actions WHERE id=%s AND run_id=%s",
                                        (action_id, run_id),
                                    ).fetchone()
                                    # A failed commit acknowledgement may hide a
                                    # completed result. Never downgrade that receipt.
                                    if action and action["status"] == "pending":
                                        self.store.finish_action(
                                            action_id,
                                            "unknown",
                                            {"reason": "工具结果未完整记录"},
                                            connection=c,
                                            owner=self.owner,
                                        )
                        except BaseException as recording_error:
                            if not isinstance(error, Exception):
                                raise error from recording_error
                            if not isinstance(recording_error, Exception):
                                raise
                            raise ToolOutcomeUnknown(
                                "工具结果及恢复记录未能确认；须人工核对"
                            ) from recording_error
                        if not isinstance(error, Exception):
                            raise
                        raise ToolOutcomeUnknown(
                            "工具准入或结果未能确认；须人工核对"
                        ) from error

                prompt = (
                    None
                    if resume
                    else request["prompt"]
                    + "\n预先约定的验收命令：\n"
                    + "\n".join(request.get("visible_checks", request["checks"]))
                )
                if generation_service:
                    prompt = None if resume else request["prompt"]
                if role == "reviewer" and not resume:
                    prompt = (
                        "只读审查执行者的固定代码快照。原始任务：\n"
                        + request["prompt"]
                        + "\n预先约定的验收命令（审查结论不替代实际验证）：\n"
                        + "\n".join(request.get("visible_checks", request["checks"]))
                        + "\n上下文：/workspace/before、/workspace/after、"
                        "/workspace/diff.patch、/workspace/handoff.json。"
                        "依照系统格式用 finish 返回结构化审查，不做修改。"
                    )
                if generation and not resume and not generation_service:
                    prompt = (
                        (prompt or request["prompt"])
                        + "\n这是经用户确认后的中断续接。上下文已保留。先检查现有状态，勿盲目重放上次动作。验收条件：\n"
                        + "\n".join(request.get("visible_checks", request["checks"]))
                    )
                finishes = []
                scheduling = None
                if request.get("background"):
                    saved = self.store.get(run_id).get("scheduling") or {}
                    scheduling = SessionScheduling(
                        generation=generation,
                        should_yield=lambda: self.scheduler.should_yield(
                            run_id, deadline=execution_deadline
                        ),
                        on_yield=lambda receipt: self.scheduler.save_yield(
                            run_id,
                            self.owner,
                            receipt,
                            workspace,
                            deadline=execution_deadline,
                        ),
                        resume_receipt=saved.get("receipt") if resume else None,
                        authorize_resume=lambda receipt: (
                            self.scheduler.validate_receipt(
                                run_id, receipt, workspace, deadline=execution_deadline
                            )
                        ),
                    )
                result = run_session(
                    run_id=run_id,
                    state_dir=session_dir,
                    prompt=prompt,
                    model={
                        **selected_model,
                        "timeout_seconds": max(
                            1,
                            min(
                                selected_model["timeout_seconds"],
                                int(remaining_execution()),
                            ),
                        ),
                    },
                    api_key=role_keys[role],
                    before_model=before_model,
                    after_model=after_model,
                    shell=shell,
                    on_event=lambda payload: role_event("sdk", payload),
                    should_stop=role_stop,
                    persistence_deadline=execution_deadline,
                    deadline_provider=lambda: execution_deadline,
                    **(
                        {"responses_transport": responses_transport}
                        if responses_transport is not None
                        else {}
                    ),
                    resume=resume,
                    role=role,
                    on_finish=finishes.append,
                    **(
                        {
                            "execution_policy": "finish_only",
                            "frozen_prompt": request["prompt"],
                        }
                        if generation_service
                        else {}
                    ),
                    **(
                        {"scheduling": scheduling, "shell_with_action": shell}
                        if scheduling
                        else {}
                    ),
                    **(
                        {
                            "skill": {
                                "version_id": skill_context.version_id,
                                "instructions": skill_context.instructions,
                                "assets": skill_context.assets,
                            }
                        }
                        if skill_context and role == "executor"
                        else {}
                    ),
                )
                if result == "yielded" and scheduling:
                    raise Yielded()
                if role_stop():
                    raise Halt("运行已停止或时长用尽")
                if result != "finished":
                    raise RuntimeError("SDK 未确认任务完成：" + str(result))
                if generation_service:
                    # SDK files alone are not an output trust anchor after a
                    # crash. Match the exact finish against the original event
                    # already persisted in the product database for this role
                    # and session generation before binding a candidate.
                    with self.store.connect() as c:
                        recorded = c.execute(
                            "SELECT payload FROM events WHERE run_id=%s AND kind='sdk' "
                            "AND payload->>'kind'='ActionEvent' AND payload->>'tool_name'='finish' "
                            "AND payload->>'assignment_id'=%s AND payload->>'session_generation'=%s",
                            (run_id, attribution["assignment_id"], str(generation)),
                        ).fetchall()
                    if (
                        not finishes
                        or not recorded
                        or len({row["payload"]["id"] for row in recorded}) != 1
                        or any(
                            row["payload"].get("action", {}).get("message")
                            != finishes[-1]
                            for row in recorded
                        )
                    ):
                        raise ValueError("生成完成输出与原会话的数据库事件不一致")
                return finishes[-1] if finishes else None

            def complete_role(role, **updates):
                with self.store.locked(run_id, self.owner) as (c, current):
                    guard(current)
                    assignment = current.get("assignments", {}).get(role)
                    if assignment:
                        if current.get("active_assignment") != role:
                            raise Halt("角色分配已改变")
                        assignment.update(state="completed", ended_at=now())
                        self.store.event(
                            run_id,
                            "assignment_completed",
                            {"assignment_id": assignment["id"], "role": role},
                            c,
                        )
                    current.update(updates)

            run = self.store.get(run_id)
            review_required = request.get("workflow", "execute") == "execute_review"
            if run.get("phase", "execution") == "execution":
                executor = run.get("assignments", {}).get("executor", {})
                if executor.get("state") != "completed":
                    finish_message = execute_role("executor", workspace.path)
                    completion = {}
                    if generation_service:
                        self.fence()
                        completion["generation_output"] = (
                            generation_service.save_output(
                                self.store.get(run_id),
                                directory,
                                finish_message,
                                deadline=execution_deadline,
                            )
                        )
                    # Persist output binding and completion together. The SDK
                    # also retains finish for recovery before this transaction.
                    complete_role("executor", **completion)
                if generation_service:
                    self.fence()
                    candidate = generation_service.build_candidate(
                        self.store.get(run_id), directory, deadline=execution_deadline
                    )
                    with self.store.locked(run_id, self.owner) as (_, current):
                        guard(current)
                        current["generation_candidate"] = candidate
                    if not candidate["valid"]:
                        raise ValueError("生成输出未通过候选结构检查；保留原始输出")
                if review_required:
                    handoff = Review.prepare(
                        workspace, directory, run_id, deadline=execution_deadline
                    )
                    if not Review.inspect(
                        workspace, directory, handoff, deadline=execution_deadline
                    )["valid"]:
                        raise RuntimeError("审查交接材料完整性检查失败")
                    with self.store.locked(run_id, self.owner) as (c, current):
                        guard(current)
                        if "reviewer" not in current.get("assignments", {}):
                            raise ValueError("缺少审查角色分配")
                        current.update(
                            handoff=handoff,
                            phase="review",
                            active_assignment="reviewer",
                            reason="只读审查固定代码快照",
                        )
                        self.store.event(run_id, "review_prepared", handoff, c)
                else:
                    with self.store.locked(run_id, self.owner) as (_, current):
                        guard(current)
                        current["phase"] = "verification"

            run = self.store.get(run_id)
            if run.get("phase") == "review":
                handoff = run.get("handoff")
                if (
                    not handoff
                    or not Review.inspect(
                        workspace, directory, handoff, deadline=execution_deadline
                    )["valid"]
                ):
                    raise RuntimeError("已保存的审查交接材料完整性检查失败")
                message = execute_role(
                    "reviewer", Path(handoff["context_path"]), read_only=True
                )
                if not Review.inspect(
                    workspace, directory, handoff, deadline=execution_deadline
                )["valid"]:
                    raise RuntimeError("审查期间固定快照或交接材料发生变化")
                try:
                    review = parse_review_report(message)
                    after = read_files(
                        Path(handoff["context_path"]) / "after",
                        deadline=execution_deadline,
                    )
                    before = read_files(
                        Path(handoff["context_path"]) / "before",
                        deadline=execution_deadline,
                    )
                    for finding in review["findings"]:
                        path = finding["path"]
                        if not path:
                            continue
                        if path not in after and path not in before:
                            raise ReviewReportError(
                                "审查问题引用了固定快照中不存在的文件"
                            )
                        if path not in after:
                            finding["side"] = "before"
                        content = (after[path] if path in after else before[path]).data
                        lines = max(
                            1, content.count(b"\n") + (not content.endswith(b"\n"))
                        )
                        line = finding.get("line")
                        if line is not None and line > lines:
                            raise ReviewReportError("审查问题行号超出固定文件范围")
                    review["valid"] = True
                except ReviewReportError as error:
                    review = {
                        "status": "changes_requested",
                        "summary": "审查未提供有效结构化结果",
                        "findings": [],
                        "valid": False,
                        "errors": [str(error)],
                    }
                review.update(
                    assignment_id=run["assignments"]["reviewer"]["id"],
                    snapshot_digest=handoff["snapshot_digest"],
                )
                complete_role("reviewer", review=review, phase="verification")
                event("review_completed", {**review, "role": "reviewer"})

            run = self.store.get(run_id)
            if run.get("phase") != "verification":
                raise RuntimeError("执行阶段未知，拒绝自动重放")
            with self.store.locked(run_id, self.owner) as (c, current):
                guard(current)
                current.update(
                    state="verifying",
                    phase="verification",
                    reason="正在检查固定代码快照",
                )
                self.store.event(run_id, "verifying", {}, c)

            def verify_stop():
                execution_fence()
                current = self.store.get(run_id)
                return (
                    current["state"] != "verifying"
                    or elapsed(current) >= request["budget"]["max_seconds"]
                    or (
                        arena is not None
                        and arena.stopped(request["arena"]["evaluation_id"])
                    )
                )

            remaining = remaining_execution()
            evidence_path = directory / "evidence"
            saved_generation_evidence = None
            if generation_service and run.get("evidence"):
                saved = run["evidence"]
                saved_inspection = Evidence.inspect(
                    workspace,
                    directory,
                    expected_report_sha256=saved["report_sha256"],
                    deadline=execution_deadline,
                )
                if not saved_inspection.get("valid"):
                    raise ValueError("已绑定的生成交付证据失效；禁止自动重新生成")
                saved_generation_evidence = saved
            if evidence_path.exists() and saved_generation_evidence is None:
                # Preserve incomplete prior validation; never silently overwrite it.
                archive = directory / ("evidence-attempt-" + str(time.time_ns()))
                evidence_path.rename(archive)
                event("verification_restarted", {"previous_evidence": archive.name})
            if skill_context:
                library.skill_context(request["skill"], deadline=execution_deadline)
            if (
                arena
                and not arena.integrity(
                    arena.get(
                        request["arena"]["evaluation_id"], deadline=execution_deadline
                    ),
                    deadline=execution_deadline,
                )["valid"]
            ):
                raise ValueError("评测冻结上下文已变化，禁止生成通过证据")

            class GuardedVerifier:
                # The fixed tuning task has exactly one check. Admission is
                # atomic with foreground enqueue; an admitted check may finish.
                image = sandbox.image

                def run(inner, *args, **kwargs):
                    try:
                        with self.store.locked(run_id, self.owner, dispatch=True) as (
                            c,
                            current,
                        ):
                            if current["state"] != "verifying" or current.get(
                                "requested_action"
                            ):
                                raise Halt("验证已经停止")
                            if elapsed(current) >= request["budget"]["max_seconds"]:
                                raise Halt("原运行时长已达上限")
                            check_deadline(execution_deadline)
                            from doonex.budget import _locked_batch
                            from doonex.store import execution_fenced

                            if execution_fenced(c):
                                raise Halt(
                                    "容器或模型传输进程终止状态不明；禁止验证派发"
                                )
                            _locked_batch(c, current, dispatch=True)
                            dispatch_check(c, current)
                            check_deadline(execution_deadline)
                            action = self.store.action(
                                run_id,
                                "verification",
                                {
                                    "command": args[1],
                                    "phase": "verification",
                                    "dispatched_by": "controller",
                                    **Ledger(
                                        self.store, run_id, self.owner
                                    ).attribution(),
                                },
                                c,
                            )
                            self.store.event(
                                run_id,
                                "verification_dispatched",
                                {"action_id": action},
                                c,
                            )
                    except SchedulerYield:
                        self.scheduler.save_yield(
                            run_id,
                            self.owner,
                            None,
                            workspace,
                            deadline=execution_deadline,
                        )
                        raise Yielded() from None
                    try:
                        kwargs["deadline"] = execution_deadline
                        kwargs.update(
                            self.operation_context(
                                self.store.get(run_id),
                                parent_action_id=action,
                                sandbox=True,
                            )
                        )
                        result = sandbox.run(*args, **kwargs)
                        check_sandbox_cleanup(result)
                        self.lease()
                        self.store.finish_action(
                            action,
                            "completed",
                            redact(result, secrets),
                            owner=self.owner,
                        )
                        return result
                    except BaseException:
                        self.lease()
                        self.store.finish_action(
                            action,
                            "unknown",
                            {"reason": "验证结果未完整记录"},
                            owner=self.owner,
                        )
                        raise

            report = saved_generation_evidence or Evidence.capture(
                workspace,
                directory,
                request["checks"],
                GuardedVerifier(),
                min(60, remaining),
                run_id,
                should_stop=verify_stop,
                deadline=execution_deadline,
                **(
                    {
                        "verifier_path": task_context.verifier_path,
                        "verification_context": verification_context,
                    }
                    if task_context
                    else {}
                ),
            )
            with self.store.locked(run_id, self.owner) as (_, current):
                current["evidence"] = report
                if (
                    arena
                    and not current.get("arena_assisted")
                    and any(
                        (
                            check.get("exit_code") not in (0, None)
                            or check.get("timed_out")
                        )
                        and not check.get("cancelled")
                        for check in report.get("checks", [])
                    )
                ):
                    current.setdefault(
                        "arena_observed_failure",
                        {
                            "report_sha256": report["report_sha256"],
                            "recorded_at": now(),
                        },
                    )
            inspection = Evidence.inspect(
                workspace,
                directory,
                expected_report_sha256=report["report_sha256"],
                deadline=execution_deadline,
                **(
                    {"verification_context": verification_context}
                    if task_context
                    else {}
                ),
            )
            final_candidate = None
            if generation_service:
                if not inspection.get("valid"):
                    raise ValueError("生成候选的通用交付证据无效")
                self.fence()
                final_candidate = generation_service.finalize_candidate(
                    self.store.get(run_id),
                    directory,
                    report,
                    deadline=execution_deadline,
                )
                if not final_candidate["valid"]:
                    raise ValueError("生成候选的完整结构证据无效")
            current = self.store.get(run_id)
            if current["state"] != "verifying" or verify_stop():
                raise Halt("验证期间收到停止请求或时长用尽")
            # Evidence module owns per-command exit codes and integrity checks.
            checks = report.get("checks", [])
            passed = (
                bool(request["checks"])
                and len(checks) == len(request["checks"])
                and all(
                    check.get("exit_code") == 0
                    and not check.get("timed_out")
                    and not check.get("cancelled")
                    and not check.get("cleanup_error")
                    for check in checks
                )
            )
            verified = (
                passed
                and inspection.get("verified", False)
                and report.get("commands") == request["checks"]
            )
            if review_required:
                review, handoff = current.get("review", {}), current.get("handoff")
                reviewer = current.get("assignments", {}).get("reviewer", {})
                verified = bool(
                    verified
                    and handoff
                    and Review.inspect(
                        workspace, directory, handoff, deadline=execution_deadline
                    )["valid"]
                    and review.get("valid") is True
                    and review.get("status") == "approved"
                    and reviewer.get("state") == "completed"
                    and review.get("assignment_id") == reviewer.get("id")
                    and review.get("snapshot_digest") == handoff["snapshot_digest"]
                    and report.get("snapshot_digest") == handoff["snapshot_digest"]
                )
            state = (
                "completed_verified"
                if verified
                else ("failed" if checks and not passed else "completed_unverified")
            )
            with self.store.locked(run_id, self.owner) as (c, current):
                if current["state"] != "verifying":
                    raise Halt("验证结束时已收到停止请求")
                check_deadline(execution_deadline)
                if elapsed(current) >= request["budget"]["max_seconds"]:
                    raise Halt("验证完成提交前，原运行时长已达上限")
                current.update(
                    state=state,
                    reason="固定快照通过全部检查"
                    if verified
                    else "检查通过，但审查要求修改或审查证据无效"
                    if passed and review_required
                    else "检查未通过或证据尚不完整",
                    evidence=report,
                    ended_at=now(),
                )
                if generation_service:
                    current["generation_candidate"] = final_candidate
                    current["reason"] = (
                        "候选已生成并保留结构证据，等待人工审核与独立评测"
                    )
                if arena:
                    current["arena_outcome"] = {
                        "classification": "passed" if verified else "failed",
                        "kind": "fixed_verifier",
                        "report_sha256": report["report_sha256"],
                        "regression_failed": not passed,
                        "safety_violation": bool(current.get("safety_violation")),
                        "recorded_at": now(),
                    }

                self.store.event(
                    run_id,
                    state,
                    {
                        "snapshot": report.get("snapshot_digest"),
                        "valid": inspection.get("valid"),
                    },
                    c,
                )
        except Yielded:
            # SDK observation, usage and product receipt have all been saved.
            # A scheduling checkpoint must not pass through failure recovery.
            self.lease()
        except BaseException as exc:  # noqa: BLE001 - record shutdown and clean task containers
            # A new lock holder owns cleanup after a connection loss.
            self.lease()
            from doonex.budget import BatchHalt
            from doonex.scheduling import PREPARATION_SECONDS

            # Settling an admitted outcome remains necessary after business
            # expiry. This one independent allowance never authorizes dispatch.
            settlement_deadline = time.monotonic() + PREPARATION_SECONDS

            if request.get("arena") and (
                exception_cause(exc, BatchHalt)
                or execution_bound is not None
                and execution_bound.scope.kind
                in {"batch", "allocation", "window", "day"}
                and exception_cause(exc, ExecutionDeadlineExceeded)
            ):
                if not exception_cause(exc, BatchHalt):
                    stopped = BatchHalt(str(exc), request["arena"]["evaluation_id"])
                    stopped.__cause__ = exc
                    exc = stopped
                if arena is None:
                    from doonex.arena import Arena

                    arena = Arena(self.settings, self.store)
            from doonex.controller_operations import current_registry

            with self.store.locked(run_id, self.owner, dispatch=True) as (c, current):
                # A committed pausing state prevents new work while cleanup runs
                # without this row lock. Cancellation can still commit meanwhile.
                current.update(state="pausing", reason="等待任务容器清理")
                self.store.mark_pending_unknown(run_id, c, current)
                intent = begin_cleanup_intent(
                    current, source="worker", actor=current_registry().actor
                )
                previous = current.get("recovery")
                context = self.operation_context(current, sandbox=True)
                self.store.event(run_id, "cleanup_requested", {"intent": intent}, c)
            cleanup = stop_containers(
                sandbox, run_id, context=context, previous=previous, error=exc
            )
            with self.store.locked(run_id, self.owner) as (c, current):
                self.lease()
                if not cleanup_intent_matches(current, intent):
                    self.cleanup_blocked = True
                    return
                cleanup = merge_cleanup(
                    cleanup, previous=current.get("recovery"), error=exc
                )
                finish_cleanup_intent(current, intent, cleanup)
                reason = str(redact(str(exc), secrets))[:1500]
                if cleanup.get("remaining") or cleanup.get("errors"):
                    self.cleanup_blocked = True
                    state, reason = "interrupted", "任务进程终止状态不明，须核查容器"
                elif current.get("requested_action") == "cancel":
                    state, reason = "cancelled", "已取消，任务容器已停止"
                elif current.get("requested_action") == "pause":
                    state, reason = "paused", "已暂停，保留工作区与会话"
                elif current.get("safety_violation"):
                    state, reason = (
                        "failed",
                        "检测到未授权访问请求；已停止后续模型及工具动作",
                    )
                elif exception_cause(exc, SessionRecoveryRequired):
                    state, reason = (
                        "interrupted",
                        "SDK 存在未记录结果的动作；请核对后确认以新会话续接",
                    )
                elif exception_cause(exc, Halt):
                    state, reason = "budget_exceeded", str(exception_cause(exc, Halt))
                elif exception_cause(exc, ExecutionDeadlineExceeded):
                    state, reason = (
                        "budget_exceeded",
                        str(exception_cause(exc, ExecutionDeadlineExceeded)),
                    )
                elif isinstance(exc, (KeyboardInterrupt, SystemExit)) or self.stopping:
                    state = "interrupted"
                else:
                    state = "failed"
                self.store.mark_pending_unknown(run_id, c, current)
                assignment = current.get("assignments", {}).get(
                    current.get("active_assignment")
                )
                if assignment and assignment["state"] != "completed":
                    assignment["state"] = state
                current.update(state=state, reason=reason, recovery=cleanup)
                if state in {"failed", "cancelled", "budget_exceeded"}:
                    current["ended_at"] = now()
                provider_error = exception_cause(exc, ModelCallError)
                if provider_error and provider_error.diagnostics is not None:
                    self.store.event(
                        run_id,
                        "model_request_failed",
                        redact(provider_error.diagnostics, secrets),
                        c,
                    )
                if arena:
                    current["arena_outcome"] = {
                        "classification": "cancelled"
                        if state == "cancelled"
                        else "failed",
                        "kind": "controller_stop",
                        "safety_violation": bool(current.get("safety_violation")),
                        "recorded_at": now(),
                    }
                    provider_type = getattr(provider_error, "provider_error_type", None)
                    if state == "failed" and not current.get("safety_violation"):
                        if (
                            provider_error
                            and (provider_error.diagnostics or {}).get("source")
                            == "controller"
                        ):
                            current["arena_outcome"].update(
                                classification="environment_failure",
                                kind="model_transport_control_failure",
                            )
                        elif exception_cause(exc, SandboxError):
                            current["arena_outcome"].update(
                                classification="environment_failure",
                                kind="docker_control_failure",
                            )
                        elif provider_type in {
                            "RateLimitError",
                            "Timeout",
                            "APITimeoutError",
                            "APIConnectionError",
                            "ServiceUnavailableError",
                            "LLMRateLimitError",
                            "LLMTimeoutError",
                            "LLMServiceUnavailableError",
                        }:
                            current["arena_outcome"].update(
                                classification="environment_failure",
                                kind="provider_transport",
                                provider_error_type=provider_type,
                            )
                self.store.event(run_id, state, {"reason": reason}, c)
                self.store.event(run_id, "cleanup", cleanup, c)
            if arena:
                from doonex.budget import BatchHalt

                if exception_cause(exc, BatchHalt):
                    arena.stop(
                        request["arena"]["evaluation_id"],
                        "budget_exceeded",
                        str(exception_cause(exc, BatchHalt)),
                        deadline=settlement_deadline,
                    )
                else:
                    current_batch = arena.get(
                        request["arena"]["evaluation_id"], deadline=settlement_deadline
                    )
                    if (
                        current_batch["state"] == "running"
                        and elapsed(current_batch)
                        >= current_batch["batch_budget"]["max_seconds"]
                    ):
                        arena.stop(
                            request["arena"]["evaluation_id"],
                            "budget_exceeded",
                            "批次时长预算已用尽",
                            deadline=settlement_deadline,
                        )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                self.stopping = True
        finally:
            if arena:
                from doonex.scheduling import PREPARATION_SECONDS

                if settlement_deadline is None:
                    settlement_deadline = min(
                        time.monotonic() + PREPARATION_SECONDS, execution_deadline
                    )
                arena.sync(
                    request["arena"]["evaluation_id"], deadline=settlement_deadline
                )

    def run(self, once=False):
        self.store.initialize()
        with self.store.connect() as connection:
            connection.autocommit = True
            if not connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)
            ).fetchone()["acquired"]:
                raise RuntimeError("已有 worker 持有执行权")
            self.lock_connection = connection
            try:
                with self.controller_actor():
                    self._run_ready(once)
            finally:
                self.lock_connection = None

    def _run_ready(self, once):
        from doonex.arena import Arena
        from doonex.scheduling import PREPARATION_SECONDS

        startup_deadline = time.monotonic() + PREPARATION_SECONDS
        arena = Arena(self.settings, self.store, deadline=startup_deadline)
        batches = arena.list(
            states={"running", "cancelling"},
            capacity=MAX_STARTUP_BATCHES,
            deadline=startup_deadline,
        )
        self.reconcile(deadline=startup_deadline)
        for batch in batches:
            check_deadline(startup_deadline)
            arena.sync(batch["id"], deadline=startup_deadline)
        check_deadline(startup_deadline)
        while not self.stopping:
            preparation_deadline = time.monotonic() + PREPARATION_SECONDS
            self.fence()
            from doonex.store import execution_fenced

            with self.store.connect() as c:
                blocked = execution_fenced(c)
            if self.cleanup_blocked or self.scheduler.cleanup_blocked or blocked:
                raise RuntimeError(
                    "容器或模型传输进程终止状态不明；执行服务阻止后续任务，须核查"
                )
            from doonex.budget import BatchHalt

            yielded_runs = self.store.scheduling_runs(
                capacity=MAX_YIELDED_RUNS,
                states={"yielded"},
                deadline=preparation_deadline,
            )
            self.scheduler.maintain(deadline=preparation_deadline)
            for run in yielded_runs:
                try:
                    self.scheduler.resume_yielded(
                        run,
                        get_workspace(run, run_directory(self.settings, run["id"])),
                        deadline=preparation_deadline,
                    )
                    arena.sync(
                        run["request"]["arena"]["evaluation_id"],
                        deadline=preparation_deadline,
                    )
                except BatchHalt as error:
                    arena.stop(
                        run["request"]["arena"]["evaluation_id"],
                        "budget_exceeded",
                        str(error),
                        deadline=preparation_deadline,
                    )
                if self.scheduler.cleanup_blocked:
                    raise RuntimeError("后台检查点存在残留或未知容器；须核查后恢复服务")
            check_deadline(preparation_deadline)
            self.scheduler.claim_next(deadline=preparation_deadline)
            queued = self.store.next_queued_run()
            check_deadline(preparation_deadline)
            if queued:
                self.execute(queued["id"])
            # Post-execution settlement is a separate finite slice. It cannot
            # renew the original Run/batch clocks or the next admission census.
            self.scheduler.maintain(deadline=time.monotonic() + PREPARATION_SECONDS)
            if once:
                break
            time.sleep(0.5)


def main():
    worker = Worker(Settings.load())

    def shutdown(signum, frame):
        worker.stopping = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    worker.run()


if __name__ == "__main__":
    main()
