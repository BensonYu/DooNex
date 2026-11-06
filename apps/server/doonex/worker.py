"""One independently running worker. Browser requests never run task code."""

import signal
import time
from pathlib import Path
from uuid import uuid4

from doonex.budget import Halt, Ledger, elapsed, guard
from doonex.config import Settings, redact
from doonex.store import OwnershipLost, Store, now

WORKER_LOCK = 4142061106
ACTIVE = {"running", "verifying", "pausing"}


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


def stop_containers(sandbox, run_id):
    try:
        return sandbox.stop_run(run_id)
    except Exception as error:  # noqa: BLE001 - preserve unknown termination state
        return {
            "remaining": ["unknown"],
            "stopped": [],
            "errors": [type(error).__name__],
        }


def get_workspace(run, directory):
    from doonex.workspace import Workspace

    return Workspace(
        path=directory / "workspace",
        repository=Path(run["request"]["repo_path"]),
        base_commit=run["request"]["base_commit"],
    )


class Worker:
    def __init__(self, settings: Settings):
        self.settings, self.store = settings, Store(settings.database_url)
        self.lock_connection = None
        self.stopping = False
        self.owner = str(uuid4())

    def lease(self):
        if self.lock_connection is None:
            raise OwnershipLost("执行连接已失效")
        try:
            self.lock_connection.execute("SELECT 1")
        except Exception as error:
            raise OwnershipLost("执行连接已失效") from error

    def fence(self):
        if self.lock_connection is None or self.stopping:
            raise Halt("执行者已停止")
        self.lease()

    def stop_requested(self, run_id):
        self.fence()
        run = self.store.get(run_id)
        return (
            run["state"] != "running"
            or elapsed(run) >= run["request"]["budget"]["max_seconds"]
        )

    def reconcile(self):
        from doonex.sandbox import Sandbox

        sandbox = Sandbox()
        for run in self.store.list_runs(limit=None):
            if run["state"] not in ACTIVE | {"interrupted"}:
                continue
            with self.store.locked(run["id"]) as (c, current):
                self.lease()
                result = stop_containers(sandbox, run["id"])
                self.store.mark_pending_unknown(run["id"], c, current)
                current.update(
                    state="interrupted",
                    owner=self.owner,
                    reason="执行服务中断；已保留变更，恢复前须核对未决动作",
                    recovery=result,
                )
                self.store.event(
                    run["id"], "interrupted", {"container_reconciliation": result}, c
                )

    def execute(self, run_id):
        from doonex.evidence import Evidence
        from doonex.sandbox import Sandbox
        from doonex.sdk import SessionRecoveryRequired, run_session
        from doonex.workspace import Workspace

        directory = run_directory(self.settings, run_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        sandbox, ledger = Sandbox(), Ledger(self.store, run_id, self.owner)
        run = self.store.get(run_id)
        request = run["request"]
        key = self.settings.credential(request["model"])
        secrets = (key, self.settings.token, self.settings.database_url)

        def event(kind, payload):
            self.lease()
            with self.store.locked(run_id, self.owner) as (c, _):
                self.store.event(run_id, kind, redact(payload, secrets), c)

        try:
            with self.store.locked(run_id) as (c, current):
                if current["state"] != "queued":
                    return
                current.update(state="running", owner=self.owner, reason="Agent 执行中")
                if current["first_started_at"] is None:
                    current["first_started_at"] = now()
                self.store.event(run_id, "running", {}, c)
            self.fence()
            if not key:
                raise ValueError("worker 缺少运行所引用的模型凭据环境变量")
            if not (directory / "workspace").exists():
                workspace = Workspace.create(
                    Path(request["repo_path"]),
                    request["base_commit"],
                    directory / "workspace",
                )
            else:
                workspace = get_workspace(run, directory)
                Workspace.scan(workspace.path)

            def before_model(input_tokens, output_tokens):
                self.fence()
                return ledger.reserve(input_tokens, output_tokens)

            def after_model(reservation, usage):
                self.lease()
                ledger.settle(reservation, usage)

            def shell(command):
                self.fence()
                action_id = ledger.tool_intent(command)
                current = self.store.get(run_id)
                remaining = max(
                    0.1, request["budget"]["max_seconds"] - elapsed(current)
                )
                try:
                    result = sandbox.run(
                        workspace.path,
                        command,
                        min(60, remaining),
                        run_id,
                        should_stop=lambda: self.stop_requested(run_id),
                    )
                    result = redact(result, secrets)
                    self.lease()
                    self.store.finish_action(
                        action_id, "completed", result, owner=self.owner
                    )
                    event("tool_finished", {"action_id": action_id, **result})
                    Workspace.scan(workspace.path)
                    return result
                except BaseException:
                    self.lease()
                    self.store.finish_action(
                        action_id,
                        "unknown",
                        {"reason": "工具结果未完整记录"},
                        owner=self.owner,
                    )
                    raise

            if run.get("phase") != "verification":
                generation = run.get("session_generation", 0)
                session_dir = directory / f"session-{generation}"
                resume = run.get("resume_session", False) and session_dir.exists()
                prompt = (
                    None
                    if resume
                    else request["prompt"]
                    + "\n预先约定的验收命令：\n"
                    + "\n".join(request["checks"])
                )
                if generation:
                    prompt = (
                        (prompt or request["prompt"])
                        + "\n这是经用户确认后的中断续接。工作区已保留上次改动。先检查现有文件状态，勿盲目重放上次动作。验收条件：\n"
                        + "\n".join(request["checks"])
                    )
                result = run_session(
                    run_id=run_id,
                    state_dir=session_dir,
                    prompt=prompt,
                    model={
                        **request["model"],
                        "timeout_seconds": max(
                            1,
                            min(
                                request["model"]["timeout_seconds"],
                                int(
                                    request["budget"]["max_seconds"]
                                    - elapsed(self.store.get(run_id))
                                ),
                            ),
                        ),
                    },
                    api_key=key,
                    before_model=before_model,
                    after_model=after_model,
                    shell=shell,
                    on_event=lambda payload: event("sdk", payload),
                    should_stop=lambda: self.stop_requested(run_id),
                    resume=resume,
                )
                current = self.store.get(run_id)
                if self.stop_requested(run_id):
                    raise Halt("运行已停止或时长用尽")
                if result != "finished":
                    raise RuntimeError("SDK 未确认任务完成：" + str(result))
            with self.store.locked(run_id, self.owner) as (c, current):
                guard(current)
                current.update(
                    state="verifying",
                    phase="verification",
                    reason="正在检查固定代码快照",
                )
                self.store.event(run_id, "verifying", {}, c)

            def verify_stop():
                self.fence()
                current = self.store.get(run_id)
                return (
                    current["state"] != "verifying"
                    or elapsed(current) >= request["budget"]["max_seconds"]
                )

            remaining = max(0.1, request["budget"]["max_seconds"] - elapsed(current))
            evidence_path = directory / "evidence"
            if evidence_path.exists():
                # Preserve incomplete prior validation; never silently overwrite it.
                archive = directory / ("evidence-attempt-" + str(time.time_ns()))
                evidence_path.rename(archive)
                event("verification_restarted", {"previous_evidence": archive.name})
            report = Evidence.capture(
                workspace,
                directory,
                request["checks"],
                sandbox,
                min(60, remaining),
                run_id,
                should_stop=verify_stop,
            )
            inspection = Evidence.inspect(
                workspace, directory, expected_report_sha256=report["report_sha256"]
            )
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
            state = (
                "completed_verified"
                if verified
                else ("failed" if checks and not passed else "completed_unverified")
            )
            with self.store.locked(run_id, self.owner) as (c, current):
                if current["state"] != "verifying":
                    raise Halt("验证结束时已收到停止请求")
                current.update(
                    state=state,
                    reason="固定快照通过全部检查"
                    if verified
                    else "检查未通过或证据尚不完整",
                    evidence=report,
                )
                self.store.event(
                    run_id,
                    state,
                    {
                        "snapshot": report.get("snapshot_digest"),
                        "valid": inspection.get("valid"),
                    },
                    c,
                )
        except BaseException as exc:  # noqa: BLE001 - record shutdown and clean task containers
            # A new lock holder owns cleanup after a connection loss.
            self.lease()
            with self.store.locked(run_id, self.owner) as (c, current):
                cleanup = stop_containers(sandbox, run_id)
                reason = str(redact(str(exc), secrets))[:1500]
                if cleanup.get("remaining") or cleanup.get("errors"):
                    state, reason = "interrupted", "任务进程终止状态不明，须核查容器"
                elif current.get("requested_action") == "cancel":
                    state, reason = "cancelled", "已取消，任务容器已停止"
                elif current.get("requested_action") == "pause":
                    state, reason = "paused", "已暂停，保留工作区与会话"
                elif exception_cause(exc, SessionRecoveryRequired):
                    state, reason = (
                        "interrupted",
                        "SDK 存在未记录结果的动作；请核对后确认以新会话续接",
                    )
                elif exception_cause(exc, Halt):
                    state, reason = "budget_exceeded", str(exception_cause(exc, Halt))
                elif isinstance(exc, (KeyboardInterrupt, SystemExit)) or self.stopping:
                    state = "interrupted"
                else:
                    state = "failed"
                self.store.mark_pending_unknown(run_id, c, current)
                current.update(state=state, reason=reason, recovery=cleanup)
                self.store.event(run_id, state, {"reason": reason}, c)
                self.store.event(run_id, "cleanup", cleanup, c)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                self.stopping = True

    def run(self, once=False):
        self.store.initialize()
        with self.store.connect() as connection:
            connection.autocommit = True
            if not connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)
            ).fetchone()["acquired"]:
                raise RuntimeError("已有 worker 持有执行权")
            self.lock_connection = connection
            self.reconcile()
            while not self.stopping:
                self.fence()
                queued = [
                    r
                    for r in reversed(self.store.list_runs(limit=None))
                    if r["state"] == "queued"
                ]
                if queued:
                    self.execute(queued[0]["id"])
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
