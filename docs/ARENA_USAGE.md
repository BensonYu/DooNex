# 私有 Skill Arena 使用说明

更新：2026-11-06。本文描述当前已实现接口及限制，**不表示 M3 已通过验收**。至少 45 个有效尝试的真实评测尚未开始。进度见 [M3 实施记录](M3_IMPLEMENTATION.md)，质量分母和推广门槛见 [竞技规范](SKILL_ARENA.md)。安装、API 与独立 worker 的启动命令仍见 [README](../README.md)。

## 1. 本地导入与审核

启动后在「Skills」导入完整本地 Skill 目录，再检查内容、来源、许可、适用范围和权限声明，明确批准或拒绝。根目录必须包含非空 UTF-8 `SKILL.md`；导入保存文件字节、模式、空目录及元数据，不执行脚本、不解释 frontmatter 为工具配置，也不自动安装依赖。

导入 API 请求体为 `{"path":"/本机目录/skill","metadata":{...}}`。`metadata` 仅接受以下字段，省略值按表中默认处理：

| 字段 | 含义 / 默认值 |
| --- | --- |
| `source` | 来源说明，默认 `local`；URL 不得带凭据、查询或片段 |
| `license_status` | `unknown`、`declared`、`verified` 或 `restricted`，默认 `unknown` |
| `license_text` | 许可正文或标识，默认 `null`；状态非 `unknown` 时必须非空 |
| `commit` | 完整 40 或 64 位 Git 对象 ID，默认 `null` |
| `scope` | 适用范围声明列表，默认 `[]` |
| `permissions` | 权限需求声明列表，默认 `[]`；声明不会授予权限 |

版本 ID 绑定内容与上述声明；时间戳不影响版本身份，内容或声明变化生成新版本。数据库保存原始报告摘要，重复导入不能用新回执覆盖原回执。只有 `approved` 且完整性有效的版本可被新运行选中。已有 Run 使用其冻结绑定，后续审核决定只影响新选用；每次执行仍复核实际内容。

当前限制为最多 200 个目录/文件条目、单文件 2 MiB、总文件 16 MiB、目录深度 8。拒绝符号链接、特殊文件、危险模式、受保护管理目录和已识别的秘密文件名；这不是任意文件内容的完整秘密识别器。许可状态是用户声明及审核记录，不自动证明再分发权。

## 2. `taskset.json` 格式

在 DooNex 私有管理目录之外准备一个本地目录，包含 `taskset.json`。每个 `repo_path` 指向本地 Git 仓库，每个 `verifier_path` 指向独立检查器目录；所有检查器目录必须与所有源仓库互不重叠。路径可以是绝对路径，或相对于 `taskset.json` 所在目录；路径不能包含符号链接或受保护管理存储。

顶层只接受 `name`、`category`、`split`、`minimum_pass_rate` 和 `tasks`。`split` 必须为 `tuning` 或 `heldout`；阈值默认 0.8，只可在 0.8–1 之间选择。任务数为 5–10，ID 不区分大小写唯一，匹配 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`。

每个任务只接受以下字段：

| 字段 | 要求 |
| --- | --- |
| `id`、`title`、`prompt` | 必填；标题最多 256 字符、需求最多 32768 字符 |
| `repo_path` | 必填，本地 Git 仓库目录；不会把未提交内容混入起始快照 |
| `base_ref` | 默认 `HEAD`，导入时解析为完整 commit 并保存源快照 |
| `visible_checks` | 默认 `[]`，最多 20 条，每条最多 4096 字符；向模型展示，不能替代隐藏最终检查 |
| `verifier_path` | 必填，独立的本地检查器目录 |
| `verifier_command` | 必填，最多 4096 字符；最终在 `/workspace` 中执行，可读取只读 `/verifier` |
| `critical` | 布尔值，默认 `false`；须在运行前确定 |

下面仅为格式示例，路径和任务需求均为占位内容，不是实际评测材料。使用时须准备对应仓库和检查器，并在运行前写好真实验收标准：

```json
{
  "name": "本地编程任务集",
  "category": "python-small-changes",
  "split": "heldout",
  "minimum_pass_rate": 0.8,
  "tasks": [
    {
      "id": "task-1",
      "title": "任务一",
      "prompt": "预先定义的任务一需求",
      "repo_path": "../repositories/task-1",
      "base_ref": "HEAD",
      "visible_checks": ["python -B -m unittest"],
      "verifier_path": "../verifiers/task-1",
      "verifier_command": "python -B /verifier/check.py",
      "critical": true
    },
    {
      "id": "task-2",
      "title": "任务二",
      "prompt": "预先定义的任务二需求",
      "repo_path": "../repositories/task-2",
      "verifier_path": "../verifiers/task-2",
      "verifier_command": "python -B /verifier/check.py"
    },
    {
      "id": "task-3",
      "title": "任务三",
      "prompt": "预先定义的任务三需求",
      "repo_path": "../repositories/task-3",
      "verifier_path": "../verifiers/task-3",
      "verifier_command": "python -B /verifier/check.py"
    },
    {
      "id": "task-4",
      "title": "任务四",
      "prompt": "预先定义的任务四需求",
      "repo_path": "../repositories/task-4",
      "verifier_path": "../verifiers/task-4",
      "verifier_command": "python -B /verifier/check.py"
    },
    {
      "id": "task-5",
      "title": "任务五",
      "prompt": "预先定义的任务五需求",
      "repo_path": "../repositories/task-5",
      "verifier_path": "../verifiers/task-5",
      "verifier_command": "python -B /verifier/check.py"
    }
  ]
}
```

导入解析 Git 对象、复制源文件与独立检查器，保存文件和目录清单及摘要，不执行 Git hooks、源程序或检查器。API 列表只显示名称、类别、split、阈值和任务 ID/标题/关键标记，不返回隐藏检查器内容。内容、任务说明或验收标准变化须重新导入为新版本，已有批次继续绑定旧版本。

配置文件上限为 256 KiB；整个任务集上限为 256 MiB、20000 个条目。各源快照限制 10000 文件、单文件 10 MiB、总文件 100 MiB；各检查器沿用 200 条目、单文件 2 MiB、总文件 16 MiB 和深度 8 的限制。超限或发现不支持的路径时拒绝导入，不发布部分版本。

## 3. 创建与执行批次

在 Arena 选择有效任务集、2–3 个已批准候选和一个有凭据的模型配置档。无额外 Skill 的 `baseline` 自动加入。`repeats` 可为 1–10，默认 3；不足 3 次的调试批次可以查看指标，不能形成推荐。

创建接口的字段为：

```json
{
  "taskset_id": "64位任务集摘要",
  "skill_version_ids": ["候选一的64位版本摘要", "候选二的64位版本摘要"],
  "model_profile_id": "已配置的模型档案ID",
  "repeats": 3,
  "run_budget": {"max_calls": 10, "max_tokens": 100000, "max_cost_usd": null, "max_seconds": 600},
  "batch_budget": {"max_calls": 200, "max_tokens": 1000000, "max_cost_usd": null, "max_seconds": 14400}
}
```

摘要及配置 ID 是占位说明；此例的 `null` 费用仅适用于已获授权的 `subscription` 模型。每个计划 Run 都使用同一份 `run_budget`，整个批次另受 `batch_budget` 约束，不能把批次额度理解为每个候选各有一份。

| 限额 | 每 Run 默认值 / API 上限 | 批次默认值 / API 上限 |
| --- | --- | --- |
| 调用数 | 10 / 100 | 200 / 10000 |
| token | 100000 / 10000000 | 1000000 / 100000000 |
| 按量费用 US$ | 2 / 100 | 10 / 1000 |
| 累计时长秒 | 600 / 7200 | 14400 / 86400 |

所有限额须为正；默认值不保证足以完成计划。创建时每 Run 和批次 token 都须能预留一次模型配置的 `max_input_tokens + max_output_tokens`。按量模型必须有输入/输出价格、价格来源及足够的一次请求费用额度；包月则将费用和费用上限保存为 `null`，仍限制调用、token 与时长。

创建批次与全部子 Run 在同一事务中提交。顺序按重复轮、任务及各组轮换交错；当前一个有效 worker 顺序执行，普通用户 Run 优先于 Arena 排队 Run。每次尝试从冻结源快照建立独立基线、可写工作区和独立 SDK 会话，不共享前一次产物。

模型请求前同时预留 Run、角色和批次额度，返回后按已知用量结算。失败和环境故障已发生用量都保留；结果未知时不释放预留。各自时长从首次启动计算，包含暂停时间；恢复不重置预算。达到批次上限停止新动作，未启动子 Run 会取消；这不是供应商账单的绝对封顶保证。

### 显式受控验收入口

真实验收前确认模型、凭据及预算，并先停止常规 worker，避免已排队评测被普通调度领取。批次可经 API 创建；以下脚本只运行指定的已有排队评测，不生成任务、候选或新授权：

```bash
.venv/bin/python scripts/live_arena.py --confirm-model-usage \
  --evaluation-id EXISTING_EVALUATION_UUID --model ACTUAL_API_MODEL_ID
```

该入口只接受冻结模型 ID 精确匹配、凭据可用且 `billing_mode=subscription` 的批次；私有 `live-acceptance.json` 必须已存在 `authorization.mode=subscription_uncapped`。脚本没有开启此授权的选项，不新建累计账本，也不更改冻结预算或环境。取得现有 campaign 文件锁和 PostgreSQL worker 锁后，先将整批调用/token 上限预留入原账本，再执行冻结列表中仍为 `queued` 的子 Run。

同一 evaluation ID 的任何旧尝试都阻止再次运行，不自动重试或恢复。`complete`、`cancelled`、`budget_exceeded` 才按实际 `calls` 与 `tokens + reserved_tokens` 结算；非终态、异常和中断保留完整分配，旧失败和未知历史不变。仅批次状态为 `complete` 且 `metrics.complete=true` 时退出码为 0；个别失败尝试仍可形成完整评测，退出 0 不代表候选合格或测试全过。

## 4. 隔离、证据与故障口径

执行容器仅挂载可写 `/workspace`，选择 Skill 时另只读挂载 `/skills`，不挂载隐藏检查器。模型只看到任务提示和 `visible_checks`。现有容器使用锁定镜像、无网络、只读根文件系统、当前 UID/GID、1 CPU、512 MiB、64 PID、去除 capabilities 和禁止新增权限；不挂载宿主完整磁盘、管理目录、Docker socket、数据库或模型凭据。Skill 只能经现有受限 shell 使用，声明不能增加工具、MCP、宿主命令或网络权限。

Agent 停止写入后，控制器对固定代码快照逐命令创建全新检查副本，另只读挂载冻结检查器到 `/verifier`，记录检查命令、退出码、日志、任务集/检查器摘要与快照报告。验证容器会运行被测代码并可读取检查器；这一阶段不继续调用被评模型，也不把隐藏检查结果作为新的调优提示。API 的证据和原始指标仅供已鉴权用户核验，不应转交候选生成 Agent。

| 分类 | 当前实现依据及计数 |
| --- | --- |
| `passed` | `completed_verified` 且报告、命令、固定代码和检查器绑定重新核验有效；计质量成功 |
| `failed` | 功能/回归失败、Run 预算或超时、普通模型/配置错误、Skill 运行失败或安全违规；计质量失败，保留证据 |
| `environment_failure` | 控制器捕获 `SandboxError` 的 Docker 控制故障，或供应商类型为 `RateLimitError`、`Timeout`、`APITimeoutError`、`APIConnectionError`、`ServiceUnavailableError`；不计质量分母，费用仍累计 |
| `cancelled` | 用户或批次停止导致取消；不计质量分母，缺失位置阻止完整推荐 |
| `assisted` | 已记录的恢复辅助运行；不计自主质量分母和所需有效重复 |
| `pending` | 尚未终止或没有可核验的最终分类；不计有效完成 |

分类来自控制器，不采信模型自述。显式访问检查器或宿主秘密路径的工具请求会记录 `safety_violation`；路径文字检测仅识别显式探测，容器挂载才是实际边界。缺失或变化的证据不能算成功，也不能覆盖推广所需的计划重复位置。

批次 `state=complete` 仅表示子 Run 到终态。指标还检查各任务/组/重复位置是否由有效证据覆盖；环境故障、取消、辅助、缺失、重复或异常记录不能伪造完整批次。当前没有环境故障补跑接口或自动重试；原批次不完整时保留现状，重新创建的批次不能覆盖原记录或冒充其补跑。

## 5. 取消、中断与人工介入

批次取消把父批次置为 `cancelling`，阻止新派发并请求停止活动子 Run；只有子运行已终止或停止到可核查状态后才标为 `cancelled`。不能把取消请求等同于进程已经停止。

子 Run 暂停或中断使整批 `interrupted`。查看子 Run 证据和恢复核查，确认无残留容器，基线、工作区、Skill 和任务集仍有效；以 `{"acknowledge_unknown":true}` 明确恢复整个批次。单 Run 的恢复 API 拒绝 Arena 子 Run。批次或子 Run 原时长已耗尽时不能恢复；新会话代数和动作确认不会退还未知用量，也不重放未决动作。

恢复发现未决 shell 动作，或当前工作区执行摘要与记录的检查点不同，会保存 `arena_assisted` 及介入证据，最终单列 `assisted`。执行摘要覆盖生成缓存路径及空目录名称/模式，交付快照仍过滤生成物；将人工改动放入缓存也不能规避检查。介入前已记录的检查失败以及安全违规优先归为失败，不能借人工修复移出失败分母。当前没有在批次中追加聊天提示的入口；检测限于受控检查点和工具窗口，不声称能检测所有人工帮助，操作人必须遵守预先约定的无额外帮助条件。

## 6. 推荐、选用与回滚

指标按任务等权聚合，每任务通过率为通过数/有效质量尝试数。界面分别显示计划数、原始尝试数、完成数、有效分母、环境故障、取消、辅助、缺失、证据无效、回归和边界违规，以及成功/有效/排队耗时中位数。费用包含失败与环境故障；未知调用或费用使总成本和单位成功成本为 `null`，零成功的单位成本不可计算。

推荐须满足 [竞技规范](SKILL_ARENA.md) 的全部门槛：保留集、完整证据、基线与候选每任务至少三次有效无辅助重复、关键任务全部通过、每任务至少一次通过、任务等权通过率达到预设阈值且不低于合格基线、无安全违规。调优结果不能推广。没有额外加权综合分，合格候选允许并列或空缺：

- `reliable`：最高等权通过率，同率时优先 `all_passed`。
- `cost`：全部合格候选货币成本已知时，选择含失败成本的最低单位成功成本；包月或未知成本不以 token 替代。
- `speed`：合格候选的成功耗时完整且已知时，选择成功耗时中位数最低者。

用户先在评测页明确选择位置及候选，才保存推荐记录；以后新建 Run 时还须明确选择当前 `recommendation_id`。服务端复核当前记录、完整模型配置、候选资格与批准状态，将推荐来源及确切 Skill 版本冻结进新 Run。任务类别由用户核对，不从新需求自动判断适用性；跨模型不继承成绩。

回滚传入历史 `recommendation_id`，重新核验该历史候选当前仍满足资格，再生成带 `rollback_of` 和 `previous_id` 的新记录。环境、控制器、模型或冻结材料变化导致证据不适用时可拒绝回滚。正在运行的版本和历史成绩不改写，也不自动替新 Run 选推荐。

## 7. 鉴权 API 对照

所有接口沿用本机 API 的 Bearer 鉴权及 Host/Origin 限制。以下路径以 `/api` 开头；不要把凭据放入 URL 或命令历史。

| 方法与路径 | 请求 / 用途 |
| --- | --- |
| `GET /api/skills` | 版本列表与完整性、审核状态 |
| `POST /api/skills/import` | `path`、`metadata` |
| `GET /api/skills/{version_id}` | 已导入版本和有效时的指令文本 |
| `POST /api/skills/{version_id}/review` | `status: approved/rejected`，可选 `note` |
| `GET /api/arena/tasksets` | 任务集公开元数据和完整性 |
| `POST /api/arena/tasksets/import` | `path`，指向包含 `taskset.json` 的目录 |
| `GET /api/arena/evaluations` | 已保存评测与当前指标 |
| `POST /api/arena/evaluations` | 固定任务集、候选、模型、重复次数及两级预算 |
| `GET /api/arena/evaluations/{evaluation_id}` | 原始尝试引用、分类、指标及完整性 |
| `POST /api/arena/evaluations/{evaluation_id}/cancel` | 取消整批 |
| `POST /api/arena/evaluations/{evaluation_id}/resume` | `acknowledge_unknown: true`，核查后恢复整批 |
| `GET /api/arena/recommendations` | `history` 与 `current` |
| `POST /api/arena/recommendations` | `evaluation_id`、`position: reliable/cost/speed`、`arm_id` |
| `POST /api/arena/recommendations/rollback` | 历史 `recommendation_id` |
| `POST /api/runs` | 原有字段外可选 `skill_version_id` 或当前 `recommendation_id`；二者同时提供时必须指向同一版本 |

Run 证据仍按原有项目上下文访问，查看按钮携带对应 `project_id`，不能混用其他项目。数据库状态和私有工件应一起保留；缺失工件使证据无效。真实源代码、候选、隐藏测试、会话和运行记录不提交到 DooNex 产品仓库。
