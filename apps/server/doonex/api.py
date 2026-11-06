"""Authenticated loopback API. Only the worker dispatches model and tool work."""

import hmac
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from doonex.budget import elapsed
from doonex.config import Settings, redact
from doonex.models import Budget, ProjectInput, ResumeInput, RunInput
from doonex.store import Store
from doonex.worker import get_workspace, run_directory


def git(repository, *args):
    # Disable user/repository hooks, fsmonitor and external diff/filter helpers.
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repository),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise ValueError("Git 操作失败：" + result.stderr[:500])
    return result.stdout.strip()


def create_app(settings=None):
    settings = settings or Settings.load()
    store = Store(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        yield

    app = FastAPI(
        title="DooNex",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {
            "http://127.0.0.1:8741",
            "http://localhost:8741",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        }:
            return JSONResponse({"detail": "来源未获授权"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    def authenticated(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + settings.token):
            raise HTTPException(401, "请输入本机访问令牌")

    auth = [Depends(authenticated)]

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(
            {
                "detail": redact(
                    str(exc),
                    (settings.credential(), settings.database_url, settings.token),
                )
            },
            status_code=400,
        )

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    def load_run(run_id):
        try:
            run_id = str(UUID(run_id))
        except ValueError:
            raise HTTPException(404, "记录不存在")
        run = store.get(run_id)
        if run["state"] == "completed_verified":
            from doonex.evidence import Evidence

            directory = run_directory(settings, run_id)
            result = Evidence.inspect(
                get_workspace(run, directory),
                directory,
                expected_report_sha256=(run.get("evidence") or {}).get("report_sha256"),
            )
            if (
                not result.get("verified")
                or not (run.get("evidence") or {}).get("report_sha256")
                or (result.get("report") or {}).get("commands")
                != run["request"]["checks"]
            ):
                run = store.transition(
                    run_id,
                    "completed_unverified",
                    "代码或证据已变化，旧验证失效",
                    allowed={"completed_verified"},
                )
        return run

    @app.get("/api/settings", dependencies=auth)
    def get_settings():
        return {
            "model": settings.model.model_dump() if settings.model else None,
            "credential_available": bool(settings.credential()),
            "default_budget": Budget(
                max_cost_usd=None
                if settings.model and settings.model.billing_mode == "subscription"
                else 2
            ).model_dump(),
        }

    @app.get("/api/project", dependencies=auth)
    def project():
        return store.project()

    @app.post("/api/project", dependencies=auth)
    def register(body: ProjectInput):
        from doonex.workspace import Workspace, WorkspaceError

        try:
            repository = Workspace.validate_repository(Path(body.path).expanduser())
        except (OSError, WorkspaceError) as error:
            raise ValueError(
                "无法注册项目：路径不存在、不可访问或不是受支持的 Git 仓库"
            ) from error
        data = {
            "id": (store.project() or {}).get("id", str(uuid4())),
            "path": str(repository),
            "head": git(repository, "rev-parse", "HEAD"),
            "dirty": bool(
                git(repository, "status", "--porcelain=v1", "--untracked-files=normal")
            ),
        }
        return store.set_project(data)

    @app.get("/api/runs", dependencies=auth)
    def runs():
        return [load_run(r["id"]) for r in store.list_runs()]

    @app.post("/api/runs", dependencies=auth)
    def start(body: RunInput):
        registered = store.project()
        if not registered:
            raise ValueError("请先注册项目")
        if not settings.model or not settings.credential():
            raise ValueError("请先在服务端配置模型和凭据；模型 API 预算需单独授权")
        model = settings.model
        if model.billing_mode != "subscription" and (
            body.budget.max_cost_usd is None
            or not model.input_price_per_million
            or not model.output_price_per_million
            or not model.price_source
        ):
            raise ValueError("模型价格未知；请配置有来源的输入/输出价格后启动")
        if body.base_ref.startswith("-"):
            raise ValueError("起始版本不能是 Git 选项")
        commit = git(
            registered["path"], "rev-parse", "--verify", body.base_ref + "^{commit}"
        )
        request = {
            **body.model_dump(),
            "project_id": registered["id"],
            "repo_path": registered["path"],
            "base_commit": commit,
            "model": model.model_dump(),
            "acceptance_version": 1,
        }
        # Preflight does not reserve or call the provider.
        upper = model.max_input_tokens + model.max_output_tokens
        cost = (
            None
            if model.billing_mode == "subscription"
            else (
                model.max_input_tokens * model.input_price_per_million
                + model.max_output_tokens * model.output_price_per_million
            )
            / 1_000_000
        )
        if model.billing_mode == "subscription":
            request["budget"]["max_cost_usd"] = None
        if body.budget.max_tokens < upper or (
            cost is not None and body.budget.max_cost_usd < cost
        ):
            raise ValueError("预算不足以预留一次配置上限的模型请求")
        return store.create_run(request)

    @app.get("/api/runs/{run_id}", dependencies=auth)
    def run(run_id: str):
        return load_run(run_id)

    @app.get("/api/runs/{run_id}/events", dependencies=auth)
    def events(run_id: str, after: int = 0):
        run = load_run(run_id)
        return store.events(run["id"], max(0, after))

    def control(run_id, action):
        run = load_run(run_id)
        with store.locked(run["id"]) as (c, current):
            if current["state"] not in {
                "queued",
                "running",
                "verifying",
                "pausing",
                "paused",
                "interrupted",
            }:
                raise ValueError("该运行已经结束")
            immediate = current["state"] in {"queued", "paused", "interrupted"}
            if immediate and current["first_started_at"]:
                from doonex.sandbox import Sandbox
                from doonex.worker import stop_containers

                cleanup = stop_containers(Sandbox(), run["id"])
                current["recovery"] = cleanup
                store.mark_pending_unknown(run["id"], c, current)
                if cleanup["remaining"] or cleanup["errors"]:
                    current.update(
                        state="interrupted",
                        requested_action=action,
                        reason="无法确认任务容器已终止；保留中断状态",
                    )
                    store.event(run["id"], "termination_unknown", cleanup, c)
                    return current
            current.update(
                state=("cancelled" if action == "cancel" else "paused")
                if immediate
                else "pausing",
                requested_action=action,
                reason="已记录停止请求；等待在途动作终止"
                if not immediate
                else "已停止",
            )
            store.event(
                run["id"],
                "control_requested",
                {"action": action, "in_flight": not immediate},
                c,
            )
        return store.get(run["id"])

    @app.post("/api/runs/{run_id}/pause", dependencies=auth)
    def pause(run_id: str):
        return control(run_id, "pause")

    @app.post("/api/runs/{run_id}/cancel", dependencies=auth)
    def cancel(run_id: str):
        return control(run_id, "cancel")

    @app.get("/api/runs/{run_id}/recovery", dependencies=auth)
    def recovery(run_id: str):
        from doonex.workspace import Workspace

        run = load_run(run_id)
        directory = run_directory(settings, run["id"])
        workspace_ok = not run["first_started_at"]
        notes = []
        try:
            if (directory / "workspace").exists():
                Workspace.scan(directory / "workspace")
                workspace_ok = True
        except ValueError as exc:
            notes.append(str(exc))
        # Read-only container reconciliation; no code is started by this route.
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=doonex.run_id=" + run["id"],
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            raise ValueError("无法核查 Docker 状态，禁止恢复")
        pending = store.pending(run["id"])
        if pending:
            notes.append(
                "存在未决动作；未知模型用量保留预留额度。请核对现有文件再确认续接。"
            )
        return {
            "containers": result.stdout.split(),
            "pending_actions": pending,
            "workspace_ok": workspace_ok,
            "notes": notes,
        }

    @app.post("/api/runs/{run_id}/resume", dependencies=auth)
    def resume(run_id: str, body: ResumeInput):
        run = load_run(run_id)
        if run["state"] not in {"paused", "interrupted"}:
            raise ValueError("仅暂停或中断的运行可以续接")
        report = recovery(run_id)
        if report["containers"] or not report["workspace_ok"]:
            raise ValueError("工作区或在途容器尚未完成核查")
        uncertain = bool(report["pending_actions"]) or run["state"] == "interrupted"
        if uncertain and not body.acknowledge_unknown:
            raise ValueError("请核对未决动作并明确确认后续接")
        if elapsed(run) >= run["request"]["budget"]["max_seconds"]:
            raise ValueError("原运行的时长预算已用尽；请保留当前结果并创建新任务")
        with store.locked(run["id"]) as (c, current):
            if current["state"] not in {"paused", "interrupted"}:
                raise ValueError("运行状态已变化")
            if uncertain:
                current["session_generation"] = current.get("session_generation", 0) + 1
                current["resume_session"] = False
                c.execute(
                    "UPDATE actions SET status='acknowledged' WHERE run_id=%s AND status='unknown'",
                    (run["id"],),
                )
            else:
                current["resume_session"] = bool(current["first_started_at"])
            current.update(
                state="queued", requested_action=None, reason="已核查，等待续接"
            )
            store.event(
                run["id"],
                "resume_requested",
                {"acknowledged_unknown": uncertain, "budget_reset": False},
                c,
            )
        return store.get(run["id"])

    @app.get("/api/runs/{run_id}/evidence", dependencies=auth)
    def evidence(run_id: str):
        from doonex.evidence import Evidence

        run = load_run(run_id)
        directory = run_directory(settings, run["id"])
        result = Evidence.inspect(
            get_workspace(run, directory),
            directory,
            expected_report_sha256=(run.get("evidence") or {}).get("report_sha256"),
        )
        from doonex.evidence import _read_artifact

        if result.get("report"):
            for check in result["report"].get("checks", []):
                try:
                    content = _read_artifact(
                        directory / "evidence", check["log"]
                    ).decode("utf-8", errors="replace")
                    check["output"] = content[:200_000]
                    check["output_truncated"] = len(content) > 200_000
                except (OSError, ValueError):
                    check["output"] = "日志缺失或无法安全读取"
        return result

    @app.get("/api/runs/{run_id}/diff", dependencies=auth)
    def diff(run_id: str):
        run = load_run(run_id)
        from doonex.evidence import _read_artifact

        try:
            content = _read_artifact(
                run_directory(settings, run["id"]) / "evidence", "diff.patch"
            )
            return {"diff": content.decode("utf-8", errors="replace")}
        except OSError:
            return {"diff": "尚未生成可读取的最终差异"}

    @app.get("/api/runs/{run_id}/artifact", dependencies=auth)
    def artifact(run_id: str, name: str):
        filenames = {
            "patch": "diff.patch",
            "snapshot": "snapshot.tar.gz",
            "report": "report.json",
        }
        run = load_run(run_id)
        import re

        if re.fullmatch(r"check-[1-9][0-9]*\.log", name) and name in (
            run.get("evidence") or {}
        ).get("artifacts", {}):
            filenames[name] = name
        if name not in filenames:
            raise HTTPException(404, "工件不存在")
        from doonex.evidence import _read_artifact

        try:
            content = _read_artifact(
                run_directory(settings, run["id"]) / "evidence", filenames[name]
            )
        except (OSError, ValueError):
            raise HTTPException(404, "工件不存在或无法安全读取")
        filename = f"doonex-{run['id']}-{filenames[name]}"
        return Response(
            content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/runs/{run_id}/adoption-check", dependencies=auth)
    def adoption(run_id: str):
        run = load_run(run_id)
        target = run["request"]["repo_path"]
        if git(target, "rev-parse", "HEAD") != run["request"]["base_commit"]:
            return {"safe": False, "reason": "目标分支已变化，请手动比较与整合"}
        if git(target, "status", "--porcelain=v1", "--untracked-files=normal"):
            return {
                "safe": False,
                "reason": "目标工作区有未提交修改，请先保存并手动整合",
            }
        path = run_directory(settings, run["id"]) / "evidence" / "diff.patch"
        if not path.is_file():
            return {"safe": False, "reason": "尚无可采纳差异"}
        result = subprocess.run(
            ["git", "-C", target, "apply", "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return {
            "safe": result.returncode == 0,
            "reason": "可下载补丁后人工采纳；预检不会修改、提交或推送目标"
            if result.returncode == 0
            else "补丁存在冲突，禁止自动覆盖",
        }

    static_dir = Path(__file__).resolve().parents[2] / "web" / "dist"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app
