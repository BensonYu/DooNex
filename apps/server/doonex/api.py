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
from doonex.models import Budget, ModelProfileInput, ProjectInput, ResumeInput, RunInput
from doonex.store import Store, now
from doonex.worker import get_workspace, run_directory


def git(repository, *args):
    from doonex.workspace import _git

    return _git(Path(repository), *args).stdout.decode(errors="replace").strip()


def branch(repository):
    from doonex.workspace import _git

    result = _git(Path(repository), "symbolic-ref", "--quiet", "HEAD", check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def identifier(value):
    try:
        return str(UUID(value))
    except ValueError:
        raise HTTPException(404, "记录不存在")


def create_app(settings=None):
    from doonex.model_registry import MetadataClient, ModelRegistry

    settings = settings or Settings.load()
    store = Store(settings.database_url)
    registry = ModelRegistry(settings)

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

    def authenticated(request: Request, authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + settings.token):
            raise HTTPException(401, "请输入本机访问令牌")
        # Project context is supplied by each browser request, never stored as a
        # mutable server-wide selection. Reject a stale/wrong context before any
        # run read, control action or artifact access.
        project_id = request.query_params.get("project_id")
        if (
            request.path_params.get("run_id")
            and project_id is None
            and len(store.list_projects()) > 1
        ):
            raise HTTPException(400, "存在多个项目，运行请求必须包含项目上下文")
        if project_id is not None:
            project_id = identifier(project_id)
            store.project(project_id)
            run_id = request.path_params.get("run_id")
            if (
                run_id
                and store.get(identifier(run_id))["request"]["project_id"] != project_id
            ):
                raise HTTPException(404, "运行不属于所选项目")

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
        run_id = identifier(run_id)
        run = store.get(run_id)
        review_integrity = None
        if run.get("handoff"):
            from doonex.review import Review

            directory = run_directory(settings, run_id)
            review_integrity = Review.inspect(
                get_workspace(run, directory), directory, run["handoff"]
            )
        if run["state"] == "completed_verified":
            from doonex.evidence import Evidence

            directory = run_directory(settings, run_id)
            binding = run["request"].get("arena")
            verification_context = (
                {
                    key: binding[key]
                    for key in (
                        "taskset_id",
                        "task_id",
                        "report_sha256",
                        "verifier_digest",
                    )
                }
                if binding
                else None
            )
            result = Evidence.inspect(
                get_workspace(run, directory),
                directory,
                expected_report_sha256=(run.get("evidence") or {}).get("report_sha256"),
                **({"verification_context": verification_context} if binding else {}),
            )
            frozen_valid = True
            if run["request"].get("skill") or binding:
                from doonex.library import Library

                library = Library(settings, store)
                try:
                    if run["request"].get("skill"):
                        library.skill_context(run["request"]["skill"])
                    if binding:
                        frozen_valid = library.tasksets.inspect(
                            binding["taskset_id"], binding["report_sha256"]
                        )["valid"]
                except (OSError, ValueError, KeyError):
                    frozen_valid = False
            if (
                not frozen_valid
                or not result.get("verified")
                or not (run.get("evidence") or {}).get("report_sha256")
                or (result.get("report") or {}).get("commands")
                != run["request"]["checks"]
                or (
                    run["request"].get("workflow") == "execute_review"
                    and (
                        not review_integrity
                        or not review_integrity.get("valid")
                        or (run.get("review") or {}).get("status") != "approved"
                        or (run.get("review") or {}).get("snapshot_digest")
                        != (run.get("evidence") or {}).get("snapshot_digest")
                    )
                )
            ):
                run = store.transition(
                    run_id,
                    "completed_unverified",
                    "代码或证据已变化，旧验证失效",
                    allowed={"completed_verified"},
                )
        if review_integrity is not None:
            run["review_integrity"] = review_integrity
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

    def public_profile(profile):
        return {
            "id": profile.id,
            "name": profile.name,
            "revision": profile.revision,
            "read_only": profile.read_only,
            "usage_adapter": profile.usage_adapter,
            "model": profile.model.model_dump(),
            "credential_available": bool(
                settings.credential(profile.model.model_dump())
            ),
        }

    @app.get("/api/model-profiles", dependencies=auth)
    def model_profiles():
        return {
            "profiles": [public_profile(profile) for profile in registry.list()],
            "default_profile_id": "legacy" if settings.model else None,
        }

    @app.put("/api/model-profiles/{profile_id}", dependencies=auth)
    def save_model_profile(profile_id: str, body: ModelProfileInput):
        return public_profile(
            registry.save(
                profile_id,
                body.name,
                body.model,
                usage_adapter=body.usage_adapter,
                expected_revision=body.expected_revision,
            )
        )

    @app.get("/api/model-profiles/{profile_id}/models", dependencies=auth)
    def discover_models(profile_id: str):
        return MetadataClient(settings).discover(registry.get(profile_id))

    @app.get("/api/model-profiles/{profile_id}/usage", dependencies=auth)
    def provider_usage(profile_id: str):
        return MetadataClient(settings).usage(registry.get(profile_id))

    @app.get("/api/project", dependencies=auth)
    def project():
        return store.project()

    @app.get("/api/projects", dependencies=auth)
    def projects():
        return store.list_projects()

    @app.get("/api/projects/{project_id}", dependencies=auth)
    def selected_project(project_id: str):
        return store.project(identifier(project_id))

    @app.post("/api/projects", dependencies=auth)
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
            "id": str(uuid4()),
            "name": repository.name,
            "path": str(repository),
            "head": git(repository, "rev-parse", "HEAD"),
            "dirty": bool(
                git(repository, "status", "--porcelain=v1", "--untracked-files=normal")
            ),
        }
        return store.set_project(data)

    @app.get("/api/runs", dependencies=auth)
    def runs(project_id: str | None = None):
        if project_id is not None:
            project_id = identifier(project_id)
            store.project(project_id)
        elif len(store.list_projects()) > 1:
            raise ValueError("存在多个项目，请明确选择项目")
        return [load_run(r["id"]) for r in store.list_runs(project_id=project_id)]

    @app.post("/api/runs", dependencies=auth)
    def start(body: RunInput, http_request: Request):
        registered = store.project(
            identifier(body.project_id) if body.project_id else None
        )
        if not registered:
            raise ValueError("请先注册项目")
        context = http_request.query_params.get("project_id")
        if context and identifier(context) != registered["id"]:
            raise ValueError("创建请求的项目与当前项目上下文不一致")
        if body.reviewer_model_profile_id and body.workflow != "execute_review":
            raise ValueError("只有执行后审查流程可以选择审查模型")
        selected_id = body.model_profile_id or ("legacy" if settings.model else None)
        if not selected_id:
            raise ValueError("请先选择服务端模型配置档；模型 API 预算需单独授权")
        selected = {"executor": registry.get(selected_id)}
        if body.workflow == "execute_review":
            selected["reviewer"] = registry.get(
                body.reviewer_model_profile_id or selected_id
            )
        for profile in selected.values():
            model = profile.model
            if not settings.credential(model.model_dump()):
                raise ValueError("worker 缺少所选配置档引用的模型凭据")
            if model.billing_mode != "subscription" and (
                body.budget.max_cost_usd is None
                or not model.input_price_per_million
                or not model.output_price_per_million
                or not model.price_source
            ):
                raise ValueError("模型价格未知；请配置有来源的输入/输出价格后启动")
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
            if body.budget.max_tokens < upper or (
                cost is not None and body.budget.max_cost_usd < cost
            ):
                raise ValueError("预算不足以预留一次所选角色配置上限的模型请求")
        if body.base_ref.startswith("-"):
            raise ValueError("起始版本不能是 Git 选项")
        commit = git(
            registered["path"], "rev-parse", "--verify", body.base_ref + "^{commit}"
        )
        request = {
            **body.model_dump(),
            "project_id": registered["id"],
            "project_name": registered.get("name", Path(registered["path"]).name),
            "repo_path": registered["path"],
            "base_commit": commit,
            "base_branch": branch(registered["path"]),
            "model": selected["executor"].model.model_dump(),
            "role_models": {
                role: profile.model.model_dump() for role, profile in selected.items()
            },
            "model_profiles": {
                role: {
                    "id": profile.id,
                    "name": profile.name,
                    "revision": profile.revision,
                }
                for role, profile in selected.items()
            },
            "acceptance_version": 1,
        }
        # A mixed run has unknown total cost, but still caps its priced subtotal.
        if all(p.model.billing_mode == "subscription" for p in selected.values()):
            request["budget"]["max_cost_usd"] = None
        from doonex.library import Library

        if body.recommendation_id:
            from doonex.arena import Arena

            recommendation, skill = Arena(settings, store).select_for_run(
                body.recommendation_id, request["model"]
            )
            if body.skill_version_id and body.skill_version_id != skill["version_id"]:
                raise ValueError("所选 Skill 与推荐版本不一致")
            request["skill_version_id"] = skill["version_id"]
            request["skill"] = skill
            request["recommendation"] = recommendation
        else:
            request["skill"] = Library(settings, store).freeze_skill(
                body.skill_version_id
            )
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
                "yielded",
            }:
                raise ValueError("该运行已经结束")
            immediate = current["state"] in {
                "queued",
                "paused",
                "interrupted",
                "yielded",
            }
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
        if run["request"].get("purpose") == "skill_generation":
            from doonex.generation import Generation

            try:
                executor = run.get("assignments", {}).get("executor", {})
                prepared = bool(
                    run.get("baseline_digest")
                    or executor.get("started_at")
                    or run.get("generation_output")
                    or run["usage"]["calls"]
                )
                Generation(settings, store).validate_run(
                    run, directory, require_workspace=prepared
                )
                # first_started_at precedes atomic baseline/workspace publish.
                # Verified absence before any model action can safely resume
                # that same Run's preparation without inventing a new attempt.
                workspace_ok = True
            except (ValueError, OSError, KeyError):
                workspace_ok = False
                notes.append("生成来源、工具策略或完整父基线校验失败")
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
        if run["request"].get("arena"):
            raise ValueError(
                "请在 Arena 中核查并恢复整个批次；单独恢复不能绕过批次预算"
            )
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
        # A completed SDK finish can precede the product completion record.
        # Keep that original generation session when no external action is
        # unknown; its persisted finish must recover without another request.
        preserve_generation_session = (
            run["request"].get("purpose") == "skill_generation"
            and not report["pending_actions"]
        )
        reset_session = uncertain and not preserve_generation_session
        with store.locked(run["id"], dispatch=True) as (c, current):
            if current["state"] not in {"paused", "interrupted"}:
                raise ValueError("运行状态已变化")
            if reset_session:
                current["session_generation"] = current.get("session_generation", 0) + 1
                current["resume_session"] = False
                c.execute(
                    "UPDATE actions SET status='acknowledged' WHERE run_id=%s AND status='unknown'",
                    (run["id"],),
                )
            else:
                current["resume_session"] = bool(current["first_started_at"])
            active = current.get("assignments", {}).get(
                current.get("active_assignment")
            )
            if active and active.get("state") != "completed":
                if reset_session:
                    active["session_generation"] = (
                        active.get("session_generation", 0) + 1
                    )
                    active["resume_session"] = False
                else:
                    active["resume_session"] = bool(active.get("started_at"))
                active["state"] = "pending"
            current.update(
                state="queued",
                requested_action=None,
                reason="已核查，等待续接",
                recovery={
                    "remaining": [],
                    "errors": [],
                    "stopped": [],
                    "confirmed_at": now(),
                },
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
            return {
                "diff": content.decode("utf-8", errors="replace"),
                "source": "evidence",
            }
        except OSError:
            if run.get("handoff") and (run.get("review_integrity") or {}).get("valid"):
                content = _read_artifact(
                    run_directory(settings, run["id"]) / "review" / "context",
                    "diff.patch",
                )
                return {
                    "diff": content.decode("utf-8", errors="replace"),
                    "source": "review_handoff",
                    "snapshot_digest": run["handoff"]["snapshot_digest"],
                    "verified": False,
                }
            return {"diff": "尚未生成可读取的最终差异", "source": "unavailable"}

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
        if run["request"].get("purpose") == "skill_generation":
            return {
                "safe": False,
                "reason": "生成候选须经 Skills 预览与导入审核；没有 Git 采纳目标",
            }
        target = run["request"]["repo_path"]
        if git(target, "rev-parse", "HEAD") != run["request"]["base_commit"]:
            return {"safe": False, "reason": "目标分支已变化，请手动比较与整合"}
        if (
            "base_branch" in run["request"]
            and branch(target) != run["request"]["base_branch"]
        ):
            return {"safe": False, "reason": "目标分支名称已变化，请手动比较与整合"}
        if git(target, "status", "--porcelain=v1", "--untracked-files=normal"):
            return {
                "safe": False,
                "reason": "目标工作区有未提交修改，请先保存并手动整合",
            }
        if not run.get("evidence"):
            return {"safe": False, "reason": "尚无可采纳差异"}
        import hashlib
        import tempfile

        from doonex.evidence import Evidence, _read_artifact
        from doonex.workspace import _git

        directory = run_directory(settings, run["id"])
        integrity = Evidence.inspect(
            get_workspace(run, directory),
            directory,
            expected_report_sha256=run["evidence"].get("report_sha256"),
        )
        if not integrity["valid"] or (
            run.get("review_integrity") and not run["review_integrity"]["valid"]
        ):
            return {"safe": False, "reason": "代码或交付证据已变化，请先重新核验"}
        patch = _read_artifact(directory / "evidence", "diff.patch")
        if (
            hashlib.sha256(patch).hexdigest()
            != run["evidence"]["artifacts"]["diff.patch"]
        ):
            return {"safe": False, "reason": "补丁内容已变化，禁止采纳"}
        # Check the exact verified bytes, not a path that can change into a link
        # between inspection and Git reading it.
        with tempfile.NamedTemporaryFile(prefix="doonex-adoption-") as temporary:
            temporary.write(patch)
            temporary.flush()
            result = _git(
                Path(target), "apply", "--check", "--", temporary.name, check=False
            )
        return {
            "safe": result.returncode == 0,
            "reason": "可下载补丁后人工采纳；预检不会修改、提交或推送目标"
            if result.returncode == 0
            else "补丁存在冲突，禁止自动覆盖",
        }

    from doonex.generation import install_generation_routes

    install_generation_routes(app, settings, store, auth)
    from doonex.discovery import install_discovery_routes

    install_discovery_routes(app, settings, store, auth)
    from doonex.library import install_library_routes

    install_library_routes(app, settings, store, auth)
    from doonex.arena import install_arena_routes

    install_arena_routes(app, settings, store, auth)
    from doonex.scheduling import install_background_routes

    install_background_routes(app, settings, store, auth)
    from doonex.community import install_community_routes

    install_community_routes(app, settings, store, auth)

    static_dir = Path(__file__).resolve().parents[2] / "web" / "dist"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app
