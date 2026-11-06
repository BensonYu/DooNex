# M4 最后一项能力计划：有界追加指令候选生成

更新：2026-11-06 · 状态：**前后端与 worker 已整合，针对性离线回归通过；完整整合与真实模型验收进行中，尚未交付**。初始设计核对基线为 `489f6c7160d4fde833fd8e11315e65bcdbe18d71`；以下编号章节保留设计与验收基线，当前实现事实见本节。后台与发现的结果分别记录，不能由本生成切片推断通过。

## 当前实施状态

已接入 `skill_generations`、API、单 worker 与「候选生成」页面；草稿冻结父版本、完整模型可见提示、1–3 个明确 tuning 可见任务、模型配置和有限预算。SQL 仅投影可见字段，不载入任务源码、隐藏验证器或旧评测。父文件仅支持原有导入器接受的 0644/0755 模式，其他模式在模型调用前拒绝；不放宽原 Library 规则。

SDK 的生成策略实际仅注册 finish；空响应、推理片段、非 finish 工具、格式错误或多重 finish 均在结算原响应后失败，不自动补问。生成运行没有 Git 基线、shell 或检查命令。父二进制文件、缓存名文件、可执行模式、空目录及许可均由控制器保留。输出、候选和最终结构报告分开不可变保存，未绑定报告也从父/输出重新计算全部字段，不能仅相信自身散列。候选最终为 `completed_unverified`，导入后为 `pending`，不继承父成绩。

真实 SDK 合成传输、一次性 PostgreSQL 与 Worker/API 整合已验证六个中断位置：工作区前、仅基线完成、SDK finish 已保存但输出尚未保存、输出发布后、候选发布后、最终结构报告发布后。已保存 finish 的恢复不重复模型调用；最终结构报告恢复复用原已绑定 Evidence；未知请求明确确认后使用新会话，原时钟与未知预留不变。停止时在途用量结算，但不再发布候选。保存会话的用户输入完整性补强与最后全量验证正在进行。

阶段性结果：backend 62 项；Worker/backend 75 项；真实 worker/API 与原 worker/API 回归 41 项；发现/生成整合 214 项；生成 UI 合成浏览器 42 项、发现 UI 回归 27 项。各组合有重叠，**不能相加成最终总数**；这些响应全为合成数据，没有真实模型调用，也不替代最终代码全量验收。真实验收脚本的 10 项数据库/SDK合同检查已通过，覆盖原 campaign 保留、未知预算、重复拒绝与状态写入失败；测试首轮暴露最终报告返回值解包错误，修复及失败日志均保留。

新增的 `scripts/live_generation.py` 只接收已冻结、未启动草稿，要求原包月 campaign、准确 revision/模型、显式用量及衍生用途确认。它先持久预留、取得唯一 worker 锁，再启动并执行一个 Run；成功也不导入、批准或开始评测。未知结果保留完整额度，同一 generation 无论成功失败都不能通过重跑脚本再次执行。脚本被强杀后须核对已保存 generation/Run 和原账本，再恢复普通 worker，不能假称数据库与文件账本跨存储原子。

安装和普通启动仍使用 README 的 Python 3.12、锁定 SDK、Docker、PostgreSQL 与前端命令。明确冻结草稿后，真实模型调用方式为：

```bash
PYTHONPATH=apps/server:tests .venv/bin/python scripts/live_generation.py \
  --confirm-model-usage --confirm-derivative-rights \
  --generation-id GENERATION_UUID --expected-revision FROZEN_REVISION \
  --model ACTUAL_API_MODEL_ID
```

仅在确认凭据可用、原 campaign 授权及该草稿有限预算后执行，不能与常驻 worker 同时执行。完整验收还须独立复核字节、用量、审核/可选择与浏览器结果，当前尚未执行真实模型生成。

本计划承接 [PRD](PRD.md)、[路线图](ROADMAP.md)、[架构说明](ARCHITECTURE.md) 与 [Skill Arena 规范](SKILL_ARENA.md)。目标是补足 M4 中尚缺的有限候选生成入口，复用现有 Run、OpenHands SDK 1.60.1、独立 worker、预算和 Library 信任链。M5 自托管社区由独立计划推进；本片不包含社区、发布、共享、市场或结算源码。

## 1. 最小纵向流程与退出边界

选择一个已批准且内容完整的父 Skill、一个明确的 tuning 任务集内的 1–3 个可见任务及一个模型配置 → 保存关闭的生成草稿，核对全部模型可见输入、许可依据和有限预算 → 明确启动一个独立 `execute` Run → 现有 worker 驱动模型返回一段追加指令 → 控制器保存原始输出，构造不可变候选和交付证据 → 用户预览 diff、来源、许可和结构检查 → 明确导入一个 `pending` 版本 → 使用原审核入口 → 如需回归，再明确创建原有 tuning Arena 批次或关闭的后台授权草稿。

首片只允许**在父 `SKILL.md` 后追加一段有界 UTF-8 指令**。原文件全部字节作为前缀保留，原文件 mode、其余文件的字节/mode、完整目录集合以及许可/归属材料逐项保持；不删除、替换或重排原内容，不新增脚本、依赖、文件或目录。任意文本替换、代码 Skill 创作、多父合并、自由附件、读取任务源码、基于旧评测结果再生成，以及自动生成→试验→再生成的循环均后置。

一个草稿最多启动一个 Run、形成一份候选并导入一个版本；不重试模型、不自动批准、不改当前推荐、不创建或启动评测。生成不建立质量成绩，结构检查通过不表示能力提高、兼容或安全；新的 Skill 不继承父版本的历史成绩。Run 使用 `completed_unverified` 表示产物等待检查，单独显示候选结构状态。

本片是用户明确启动的前台任务，与后台调优首片的窗口、日账和历史验收不同。它不复用或增加旧后台日额度，也不作为绕过该额度的调优运行；若以后要自动后台生成，须另定授权、日账和调度合同。

## 2. 现有调用方与必须补齐的冲突

以下是核对代码后的可复用事实，增量栏均为拟实现能力。

| 当前实现与真实函数 | 可复用内容 / 最小增量 |
| --- | --- |
| [library.py](../apps/server/doonex/library.py)：`Library.freeze_skill`、`skill_context`、`review` | 冻结批准版本及报告摘要；保留原人工审核。父 Skill 是生成的数据，不作为执行生成任务的额外 Agent Skill 注入。 |
| [tasksets.py](../apps/server/doonex/tasksets.py)：`TaskContext.public_metadata` | 可参考其显式可见字段思想，但不能直接调用 `context`、`inspect` 或 `_load`：`_load` 会扫描 source **和 verifier**。新增只投影已绑定数据库回执可见字段的窄入口。 |
| [models.py](../apps/server/doonex/models.py)：`StrictModel`、`Budget`、`run_roles`；[model_registry.py](../apps/server/doonex/model_registry.py)：`ModelRegistry.get` | 使用严格输入、同一预算和冻结模型配置。只用 executor，不新增角色图或模型适配器；客户端不能覆盖内部运行目的和工具策略。 |
| [store.py](../apps/server/doonex/store.py)：`Store.create_run(request, connection=...)`、`dispatch_lock`、`locked`、`event` | 生成回执与唯一 Run 在同一短事务提交；沿用 owner、动作、事件与原账本。当前没有生成表，需增加一张小型来源/状态回执表。 |
| [worker.py](../apps/server/doonex/worker.py)：`Worker.execute`、`get_workspace`、`reconcile`；[workspace.py](../apps/server/doonex/workspace.py)：`Workspace`、`FileContent`、`write_files`、`manifest_digest` | 复用有效 worker 和受控快照；当前普通 Run 从 Git 导出，生成应从父版本构造独立基线，不能伪造 Git 提交或导出所选项目。`get_workspace` 目前要求 `repo_path`，必须显式识别生成运行。 |
| [sdk.py](../apps/server/doonex/sdk.py)：`run_session`、`GuardedLLM`、`_M1Conversation`、`_M1Agent._initialize`、`_step`、`_execute_actions` | 复用协议、私密运行时、预算回调和会话。当前 `_initialize` 硬编码 ShellTool + FinishTool，后两者通过 `self.tools[0]` 取 Run ID；需增加服务端固定的 `finish_only` 策略及独立 Run 身份字段，实际不注册 shell，不能放一个假 shell 保持索引。 |
| [budget.py](../apps/server/doonex/budget.py)：`Ledger.reserve`、`settle`、`guard`、`elapsed` | 保持请求前预留、未知结果保留与首次启动时钟；无新账本和自动重试。 |
| [evidence.py](../apps/server/doonex/evidence.py)：`Evidence.capture`、`inspect`、`build_patch` | `commands=[]` 仍能生成固定快照、manifest、diff、归档和报告；`verified` 保持 false。但当前读取会过滤生成缓存名，归档不保留空目录，不能独立证明完整 Skill；附加生成工件绑定全部 bytes、目录/mode、原始输出和结构检查。 |
| [skills.py](../apps/server/doonex/skills.py)：`PreparedSkill`、`_validate_content`、`_metadata`、`SkillRegistry.persist`；[library.py](../apps/server/doonex/library.py)：`bind` | 复用不可变版本发布和原始数据库信任锚；经审查的 bytes→PreparedSkill 入口可与发现片共用。不能放宽 `prepare_import` 对私有管理目录的拒绝来导入 Run 目录。`bind` 若尚无可组合事务入口，仅为绑定/来源原子保存补充该参数。 |
| [arena.py](../apps/server/doonex/arena.py)：`Arena.prepare`、`create`；[background.py](../apps/server/doonex/background.py)：`Background.create`、`_snapshot` | 只验证批准后的版本可被原入口选择；生成操作不调用这些建批函数。原 Arena 仍比较基线与 2–3 个候选，后台仍只接受 tuning。 |

另一个真实冲突是当前 `Worker.execute` 没有持久化 executor 的 finish 正文。生成模式必须先保存完成输出与摘要，再标记 executor 完成；进程重启只能读回同一已完成输出并完成构造，不能重新调用模型。普通执行和 reviewer 的行为保持原合同。

## 3. 输入投影与资料边界

父输入绑定 `version_id`、`content_digest`、`report_sha256`、完整 metadata、目录集合和文件清单；启动前再次要求已批准且完整。父版本的指令原文供生成器参考，其余资产只由控制器完整保留，不作为模型附件，不运行、不安装依赖。父版本标为批准不代表其所有素材都已取得衍生权，许可门槛另见第 5 节。

任务输入必须先用 `library_versions` 中的保存标记检查 `kind='taskset'` 且 `split='tuning'`，然后在仍要求同一 split/报告摘要的查询中，仅对明确选中的 task ID 投影 `id`、`title`、`prompt`、`visible_checks`。不 `SELECT data` 返回整个任务集，不打开任务集的磁盘 `report.json`、source 或 verifier 目录，不使用 `TaskSetRegistry.inspect/context` 后再删字段。heldout 在任何文件读取之前拒绝。

该投影只依赖已经绑定的数据库可见回执，不声称验证了任务集磁盘内容；冻结所选可见字段的规范 JSON 与 SHA-256，供用户核对并在 Run 全程使用。拟新增的窄入口只允许这些字段，不返回 `verifier_command`、`verifier_digest`、答案、来源路径、隐藏检查器、旧 Run 日志、旧 Arena 指标/结果或推荐记录。任务集磁盘完整性留给以后明确创建的原 Arena 批次检查。

首片不提供任意备注、附件、URL、目录或结果 ID 作为生成上下文；改进目标是服务端固定模板：“参考父指令和所选 tuning 可见任务，提出可追加的通用操作指引”。这样无法把旧评测回执直接接入模型。所选 prompt 或父文本本身可能包含用户导入的材料，产品不能语义证明其中从未夹带答案；用户必须在启动预览中核对允许发送给所选供应商的全文。不能宣称自动识别了所有秘密或答案。

拟采用的首片输入上限：父 `SKILL.md` 最多 48 KiB，投影后的 tuning JSON 最多 24 KiB，1–3 个任务；最终拼装提示最多 80 KiB，且须满足所选模型实际输入与输出预留。超限拒绝，不截断隐藏地继续；不借助额外模型摘要。既有更大的父 Skill 仍可照常使用，只不能进入本首片生成。

## 4. 固定工具策略与候选构造

生成 Run 由服务端固定 `purpose='skill_generation'`、`workflow='execute'`、`execution_policy='append_instructions_v1'`、`checks=[]`；`skill`、`arena`、`background` 和推荐绑定均为空。模型调用仍由 worker 的 `run_session` 执行，使用 `GuardedLLM` 的既有 before/after 和停止回调。API 请求只保存草稿或排队，不直接调用模型。

SDK `finish_only` 模式只向模型提供 FinishTool，禁用 shell 定义、shell 回调、默认发现、记忆、插件及 MCP。恢复前重新核对服务端策略和保存的工具定义；会话中曾出现 shell 或未知工具时拒绝恢复。不能依靠“不要执行脚本”的提示词，也不能仅在执行 shell 后拒绝候选。本片不创建模型工具容器，也不执行候选或父脚本。

finish 正文须是唯一 JSON 对象 `{"append_text":"..."}`，不接受 Markdown 包装、额外字段或客户端指定路径。`append_text` 须非空、有效 UTF-8、最多 8 KiB，不含 NUL/禁止控制字符，不含新增 frontmatter 或显式的许可/版权/作者变更声明。使用固定、可测试的声明检查保存具体拒绝原因；这只拦截定义内的显式形式，不宣称能判断全部自然语言授权含义。含糊的权利表述留在原始输出供检查，不能导入。

控制器按 `原 SKILL.md 字节 + 固定分隔段 + append_text UTF-8 字节` 构造；固定分隔段为版本化模板的一部分。仅当后缀恰好匹配这次保存的 finish 输出时才接受。原字节前缀不变并不保证新增指令没有语义冲突，仍需人工审核；父 Markdown 未闭合等情况也不能由结构通过推断渲染或指令有效。

起始基线为父版本的精确文件/目录/mode，候选工作区是独立副本；模型不能写这两个目录。候选构造用临时目录、原子发布和不可变摘要，重启只验证或复用完整结果，不修补已变化的文件。首次保存原始输出、候选摘要与阶段意图在标记完成前进行；磁盘和数据库不是同一个原子事务，发布后未绑定的文件保持不可选择。

`Evidence.capture(..., commands=[])` 在停止模型后保存通用快照、diff 和归档，绑定 Run 的 evidence 报告；它不创建检查容器，也不产生 `completed_verified`。生成模块另外以 `exclude_generated=False` 的有界读取保存完整候选 manifest、含空目录的固定候选归档及结构报告，避免现有交付过滤规则遗漏父资产；所有工件摘要绑定同一 generation 与 Run，导入只读取此完整冻结候选。

新增控制器结构报告须核对：只有 `SKILL.md` 追加、其余 bytes/mode/目录精确一致、后缀和原始输出一致、许可/归属保留、Skill 资源上限和非空 UTF-8 指令有效。报告保存 `valid/errors`、检查版本、完整工件索引及 SHA-256；Run 的通用证据与完整候选报告同时有效才可进入导入预览。缺文件、空目录或缓存名称的父资产均有针对性检查，不能把被过滤部分当作已验证保留。

## 5. 许可、父版本与生成来源

首片只接受有完整、明确衍生使用依据的父版本；至少有候选内可核对的 MIT/Apache-2.0 全文、适用范围及保留的版权/NOTICE。复用发现片经验证的许可识别和正文回执（整合后核对实际函数），不能因现有 metadata 只有 `MIT` 标签、`declared` 或 `verified` 字样就推定满足。未知、缺失、限制或冲突时不启动模型，不提供绕过按钮；其他许可后续单独设计。

用户在启动前明确确认父内容、许可/归属及本次私有衍生用途。全部许可文件、NOTICE、版权/作者信息、父 source 和许可正文按原字节/回执保留。新增段不得声称更改许可或作者，控制器不让模型填写 metadata，也不自动给新段选许可证。父 `SKILL.md` 本身因纯追加保留其全部原文。

新 metadata 使用本机来源描述 `doonex-generation:<generation UUID>`，`commit=null`，不伪造 Git commit；`license_status` 最多为 `declared`。`license_text` 记录原许可全文和适用范围，明确“保留父授权与归属；新增模型文本未由系统独立授予许可，须人工核对”。不得继承父 `verified` 或宣称模型输出具备独占权。若该说明无法满足既定权利门槛，候选只保存预览，不能导入或批准。

父完整 metadata 与每个许可/NOTICE 摘要保存在独立 generation 来源回执中；不扩展现有严格 Skill metadata 字段，也不篡改父版本。回执还绑定：父版本/报告/内容摘要、可见 tuning 投影摘要、服务端提示/分隔段/工具策略版本、实际模型配置与 profile revision、Run/assignment ID、调用时间与可得响应标识、已知及未知用量、原始输出、候选结构报告与 evidence 摘要、最终 version_id。不存在的供应商版本或响应标识为 null，不能补造。

## 6. 最小 API 与持久状态合同（拟议）

沿用本机 Bearer、Host/Origin、私有存储和 `StrictModel`；固定静态路由须先于 `/api/skills/{version_id}`。UUID 和摘要校验，未知字段返回 422；客户端不能提交工具、prompt、路径、替代模型、review 状态、费用结算或伪造来源。

| 方法与路径 | 输入 / 有限效果 |
| --- | --- |
| `POST /api/skills/generations` | `{project_id,parent_version_id,taskset_id,task_ids,model_profile_id,budget}`；核对父与 tuning 标记，冻结明确模型可见输入、许可与配置，创建关闭的 draft；模型 0 次，不创建 Run。 |
| `GET /api/skills/generations`、`GET /api/skills/generations/{id}` | 返回保存的来源、输入预览、revision、关联 Run/实际状态、预算、结构检查及失败码；本机只读，不新调用模型、不扫描隐藏材料、不自动推进或重放。 |
| `GET /api/skills/generations/{id}/artifact?name=...` | 只读下载已绑定的 `candidate`、`report` 或 `output` 白名单工件；重新核对保存摘要，不接收路径、不解包执行；输出缺失或被篡改时拒绝。 |
| `POST /api/skills/generations/{id}/start` | `{expected_revision,confirm_model_usage:true,confirm_derivative_rights:true}`；重验冻结条件、凭据和一次请求预留能力，在 `dispatch_lock` 与短事务内创建唯一 Run 并绑定回执。重复同一已消费意图仅返回原 run_id，条件不同为 409。 |
| `POST /api/skills/generations/{id}/preview` | `{expected_revision}`；只检查已终止生成 Run、已绑定 evidence/结构报告和固定候选，保存包含内容/diff/许可/来源的 `preview_sha256`；无模型、无脚本、无 Library 写入。 |
| `POST /api/skills/generations/{id}/import` | `{expected_revision,preview_sha256,confirm_source:true,confirm_license:true}`；从固定候选 bytes 构造 PreparedSkill，发布并原子绑定 pending 与 generation 来源关系。完全相同请求只返回原版本及审核；不重新生成、不重置审核。 |

`project_id` 是用户明确选定的归属和 Run 访问上下文，不授权读取该项目内容。生成 request 的 `repo_path/base_commit` 应为 null，基线标为父 Skill 的 `content_digest`；不能将父提交填作本地项目 Git 基线。需对 `get_workspace`、前端 Run 类型/基线展示和 `adoption-check` 增加目的分支：生成不调用 Git 导出或采纳预检，只能经生成导入入口进入 Library。普通 Run 继续要求真实 Git 基线。

生成回执状态建议为 `draft → queued/running → candidate_ready → preview_ready → imported`；Run 状态仍为既有真相。`paused/interrupted/failed/cancelled/budget_exceeded` 反映原 Run，结构无效单列 `candidate_invalid`；回执不能在模型尚未确认完成时显示 ready。`revision` 为规范保存状态的 opaque SHA-256，preview 绑定 revision、Run、证据与候选摘要，过时或篡改返回 409/完整性错误。

暂停、取消、recovery 和 resume 复用 `/api/runs/{run_id}/...?...project_id=...`，生成页链接到确切 Run。恢复必须额外核对生成来源/策略/父基线；未知动作遵守原人工核查与 `acknowledge_unknown`，不得新建 Run 替代恢复。错误使用固定安全码（400 条件/完整性、404 归属或不存在、409 修订/状态），不给原始异常或模型文本拼接凭据；错误回执可含 generation_id 供读取。

单 generation 唯一 run_id、唯一成功 version_id；建议表 `skill_generations(id uuid primary key, run_id uuid unique, data jsonb)`，Library 关系写入回执。start 意图、原输入摘要和消费记录在同事务绑定；磁盘发布与 DB bind 之间崩溃后，只能验证同一确切输入并复用，不覆盖、修复或重新绑定历史信任锚。无确证结果保持待核查，不自动重放 import。

## 7. 预算、停止与恢复

草稿不使用模型；启动页必须显示实际供应商/模型、完整冻结配置、全部可见输入及调用/token/时长/费用上限，并明确确认本次模型用量。现有聊天包月或前片验收授权不能被本计划视作新增按量预算；真实验收前核对当前用户已授权的凭据、模型和范围。

产品 Run 继续使用 `Budget` 的有限上限，至少能预留一次所选模型配置的最大输入+输出。按量模式须有单价来源与费用上限；包月费用/预留/上限为 null，不能显示免费。首次启动时钟包含暂停，未知请求继续占调用/token/费用预留，恢复、重新加载页面或模型配置改动不清零。finish_only 通常只需一次完成请求，但不能假定模型必然一次返回正确 JSON；错误或超限直接保留失败，不追加自动修复请求。

本片不写 `request.background`/`arena`，不调用 `Scheduler.claim_next` 为它创建授权，也不从旧后台日账释放、借用或扣除额度。明确新建普通生成 Run 是另一项用户授权，不能把它记录成旧后台批次补跑。若真实验收使用现有 campaign，须在原文件中追加生成尝试的保守预留和结果，不重建/重置旧 campaign；新增显式脚本的实现与实测状态见上文；不得用普通执行脚本冒充生成验收。

| 故障窗口 | 必须保存与恢复的事实 |
| --- | --- |
| start 响应丢失/并发双击 | 读原 generation；同一意图返回原 Run，数据库约束阻止第二个。GET 不自动启动。 |
| 模型超时、断流、无用量或异常工具请求 | 保留 SDK/Run/账本与未知预留，候选不可导入；不重试、不降级模型。 |
| finish 已持久化、构造前中断 | 通过保存的完成事件和输出摘要续作控制器构造，不再次调用模型。输出绑定不完整时待核查。 |
| 半份基线/候选、格式错误、超长或权利声明失败 | 不覆盖已存在内容；保存诊断与输出，结构无效，不进入 pending。 |
| 人工改动候选、父内容/报告变化、证据缺失 | 摘要失败，旧预览失效，不修复信任锚、不导入；父随后被拒绝也阻止新的导入。 |
| 暂停/取消时模型在途 | 停止后续派发，显示实际等待与未知结果；不承诺供应商立即取消。原时钟和预留保留。 |
| 发布文件后 DB 绑定失败 | 保留孤立不可选文件和意图；检查确切版本后幂等绑定，不能默认为已导入。 |

模型超时和原 Run 时长限制是派发与等待边界，不是数据库/宿主失联时的绝对响应 SLA。无法证明 owner 或状态写入时停止推进，不靠前端取消请求推断停止。

## 8. 最小实施文件与顺序

1. 新增 `apps/server/doonex/generation.py`：严格草稿/start/来源、可见投影、finish 输出/候选结构检查、preview/import 合同；`store.py` 只增加表初始化与必要约束。任务可见投影放在 `tasksets.py` 的窄入口或本模块，始终避免完整 registry 加载。
2. `sdk.py` 增加固定 `finish_only` 策略、Run 身份与恢复核对；`worker.py` 增加生成基线、完成输出持久化、控制器追加，调用生成模块保存完整候选工件和结构报告。保留普通执行/审查及通用 Evidence 路径，复用 Ledger、owner 和停止回调，不新增 Agent 引擎、依赖或服务。
3. `skills.py/library.py` 仅复用或补足受控 bytes 准备与可组合绑定；与发现片共用真实已整合入口，不复制另一份宽松校验。`api.py` 接路由、生成目的的 workspace/evidence 与禁止 Git 采纳；仅在必要时扩展 Run 的内部目的与基线表示。
4. `apps/web/src/api.ts`、`skills.tsx`、必要的 `main.tsx` 与少量样式：明确输入/预算预览、启动、原 Run、失败/恢复、纯文本 diff/许可、pending 导入和原审核。模型输出作为文本显示，不执行 Markdown HTML 或外部资源。
5. 新增与风险相称的 SDK/数据库/worker/Library/API/浏览器回归；增加显式真实生成验收脚本并更新 README、M4 实施记录和路线图。计划阶段不修改这些文件；实现阶段安装、运行与测试命令须同步保存。

先完成一条父 Skill→tuning 可见投影→单 Run→固定候选→pending→原审核的闭环，再考虑更宽输出。若无法在锁定 SDK 中证明 finish_only 实际无 shell、或基线目的需要大幅改写通用 Run，先报告具体证据和替代方案，不静默增加新引擎、代理框架或运行服务。

## 9. 验收条件与证据

| 项目 | 必须实际执行的检查 |
| --- | --- |
| tuning 资料边界 | heldout 标记先拒绝；隐藏路径/答案/旧结果放 sentinel；监测文件读取、SQL 投影、模型请求、事件、日志和工件，证明未访问/外发。故意禁止完整 `TaskSetRegistry.inspect/context` 时闭环仍可完成。 |
| SDK 工具边界 | 模拟供应商检查只收到 finish schema；返回 shell/未知工具请求被拒绝；真实 Docker 派发、父脚本与上游执行次数 0。普通 shell/reviewer 和 SDK 恢复回归仍通过。 |
| 追加与权利边界 | 二进制资产、0755 文件、空目录及许可/NOTICE 前后完全一致；仅 SKILL.md 固定后缀变化。覆盖非法 UTF-8、控制字符、额外字段、超长、改许可/署名声明与父许可缺失/冲突；失败不发布。 |
| 状态与预算 | PostgreSQL 并发 start 只建一个 Run；调用/token/按量费用到限、超时、未知用量、暂停后时长和重启保留；模型配置改变不改冻结输入；旧后台日账/失败/未知记录前后不变。 |
| 恢复与交付 | 在 finish 保存前后、候选发布、Evidence.capture、Library bind 窗口注入中断；不重复模型、不重写旧证据。diff/快照/目录/mode/结构报告/原始输出摘要绑定最终 Run，篡改立即失效。 |
| Library 信任链 | pending 不可用于任务/Arena/后台；同输入重入保留原版本及审核；原审核明确批准且完整后可被新 tuning 批次与关闭后台草稿选择；这些选择动作模型 0 次、无自动启动。 |
| 浏览器 | 看到完整模型可见输入、父/模型来源、预算/未知预留、候选 diff/许可与原 Run；双击/刷新不重放；迟到响应不覆盖新选择；原始内容不加载外部资源；移动端无文档横向溢出。 |
| 受控真实模型 | 明确凭据/实际模型/有限预算后，以可分发的人工样例父 Skill 和 tuning 可见任务完成一次真实生成，独立核对追加字节、用量和证据；不执行真实项目材料、不推送父仓库。模型次数按实际记录，不能预写为 1。 |

真实生成验收到 pending/审核/原 tuning 入口可选为止；可选 tuning 回归必须由用户另行冻结父版本、新版本与第三个已批准候选（满足现有 2–3 候选合同）、无额外 Skill 基线、任务集、模型及批次预算，再按原流程明确启动。保留所有失败/未知/辅助证据，不将 tuning 成绩推广成 heldout 结论，不写“提高”除非相应证据支持。

最终代码按风险运行 `.venv/bin/python scripts/test_local.py`、`.venv/bin/ruff check apps/server/doonex tests scripts`、`pnpm --dir apps/web build`、新增合成 UI 检查和 Git diff 检查；真实模型证据单独记录源码/环境指纹、授权、命令、退出码、时间、账本与私有工件。此前后台或发现通过项不自动覆盖新增生成代码。

本计划的退出条件是上述受控生成流程有对应证据、原始失败均保留、文档准确区分结构合格/人工审核/真实回归，以及所有新增权限和数据读取边界经过验证；不是完成任意 Skill 创作，也不代表 M5 或 M4 的其他验收自动完成。
