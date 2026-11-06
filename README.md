# DooNex

**Do next. 把下一步交给 AI。**

DooNex 是面向个人编程工作流的本地 Web 工作台，复用 OpenHands Software Agent SDK 执行任务，由 DooNex 管理项目、预算、恢复状态和交付证据。

> **当前状态：M1–M3 已完成受控验收。** 现有多项目执行/只读审查、模型配置与用量、私有 Skill 版本、固定 Arena 批次及推荐入口。M3 最终源码通过 570 项本机测试、静态检查和前端构建；真实 45 次尝试形成完整证据，43 次通过、2 次预算停止，全部记录保留。适用环境、版本与限制见 [M3 实施记录](docs/M3_IMPLEMENTATION.md)，历史见 [M2](docs/M2_IMPLEMENTATION.md) 与 [M1](docs/M1_IMPLEMENTATION.md)。受控样例不形成通用成功率或模型/Skill 优胜声明。

M4 的有限后台首片已通过 751 项本机回归；修复后真实原会话续接及 3 个后台任务通过，随后供应商超时，整批仍未验收完成。调用上限、在途撤销和真实只读界面检查通过，失败与未知预留保留。有限 GitHub 候选发现已整合并通过 874 项本机回归，真实固定来源导入、审核与可选择流程已通过，移动端分段视觉复核通过；候选生成按[独立计划](docs/M4_GENERATION_PLAN.md)实施中，尚未交付。具体状态见 [M4 实施记录](docs/M4_IMPLEMENTATION.md)。

## 当前使用流程

1. 注册并选择本地 Git 项目，选择明确起始提交，保存需求与验收命令。项目具有稳定 ID，切换项目不会改变旧运行的归属。
2. 在「模型设置」保存实际模型 ID、接口、凭据引用和预算；创建任务时分别选择执行与审查配置档。模型列表与账户用量可显式只读查询。
3. 执行者在独立代码副本中修改；审查者随后查看固定的修改前/后代码与差异。两个角色使用独立会话，共享一个 Run 的预算与时钟，由一个有效 worker 顺序执行。
4. 浏览器查看所属项目、角色状态、事件、总量和分项用量。需要时暂停或取消；中断后核查工作区、残留容器和未决动作，再确认续接。
5. 查看结构化审查及固定快照的检查结果。审查批准仍须通过实际检查；要求修改或无效审查不能标记已验证完成。
6. 下载补丁、快照及日志，人工决定是否采纳；目标提交、分支或脏工作区变化会阻止安全采纳预检通过。

当前采纳入口检查目标提交、分支、工作区状态及已绑定补丁的冲突，不自动修改目标、推送 GitHub 或部署。执行/审查流程不递归委派、不自动反复修复；私有 Skill 竞技已完成 M3 受控验收，当前结果与限制见 [M3 实施记录](docs/M3_IMPLEMENTATION.md)。

## 产品阶段

| 阶段 | 目标 | 当前状态 |
| --- | --- | --- |
| M0 · 项目定义 | 产品、架构、竞技规则与 Agent 规范 | 已建立文档 |
| M1 · 最小可用工作台 | 单项目单 Agent、隔离、预算、恢复与证据 | 已完成受控验收 |
| M2 · 有限多 Agent 协作 | 执行与审查分工、多项目隔离 | 已完成受控验收 |
| M3 · 私有 Skill Arena | 版本化技能、公平评测、多场景推荐 | 已完成受控验收 |
| M4 · 受控持续改进 | 有预算的后台试验与候选发现 | 首片有限后台批次验收中 |
| M5 · 自托管社区 | 公开目录、投稿审核、条件榜单、版本分发与使用归因 | 用户已授权产品实现，计划中 |

首个可用版本以 M1 为界，个人 Alpha 为 M1–M3。不包含公共市场、结算、分布式集群、专业 CAD/PCB 集成或原生桌面客户端。需求与验收以 [PRD](docs/PRD.md) 为准，交付顺序见 [路线图](docs/ROADMAP.md)。

后续 M5 已明确为自托管 Skill 社区，当前尚未实现，范围与验收见 [M5 计划](docs/M5_IMPLEMENTATION.md)。收费规则与对外上线单独确认；不将个人 Alpha 的既有完成状态扩写为社区已交付。

## 本机安装与启动

首个验证环境为 Apple Silicon Mac mini、macOS、Docker 与 Python 3.12。系统 Python 3.9 不用于应用运行；`uv` 根据项目约束创建 Python 3.12 环境。还需可用的 Git、Node.js 和 `pnpm`，前端包管理器锁定为 `pnpm@11.1.1`。这些命令供本机开发使用，不代表已验证其他硬件或公网部署。

```bash
git clone git@github.com:BensonYu/DooNex.git doonex
cd doonex
python3 -m venv .bootstrap
.bootstrap/bin/pip install uv==0.13.7
.bootstrap/bin/uv sync --frozen
pnpm --dir apps/web install --frozen-lockfile
pnpm --dir apps/web build
.venv/bin/python scripts/setup_local.py
docker compose --env-file "$HOME/Library/Application Support/DooNex/compose.env" up -d --wait
docker pull python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
.venv/bin/doonex init
```

`setup_local.py` 在私有数据目录创建 `service.json` 与 `compose.env`，不会推断模型凭据或 API 额度，已有配置会保留。PostgreSQL 仅映射到本机端口。默认数据目录为 `~/Library/Application Support/DooNex`，可通过 `DOONEX_HOME` 指向其他私有目录；自定义目录需同步用于配置和启动。

在两个终端分别运行：

```bash
.venv/bin/doonex serve
```

```bash
.venv/bin/doonex worker
```

打开 `http://127.0.0.1:8741`。首次登录使用私有数据目录中的 `access-token`，不要把令牌写进 URL、截图、普通日志或 Git。worker 独立于浏览器和 API 请求；网页刷新不会新建运行。

## 配置模型与预算

初始模型配置保存在私有数据目录的 `model.json`，在界面中显示为只读 `legacy`；「模型设置」可复制或创建独立配置档，保存到私有 `profiles.json`。创建 Run 时冻结各角色完整配置和版本，后续编辑不改写历史。以 [model.example.json](model.example.json) 为模板，填写实际供应商、API 模型 ID、接口模式、输入/输出上限与超时。当前适配器限定 `openai`、`anthropic`；OpenAI 适配器支持显式选择 Chat/Responses，已真实验证 `gpt-6.1-sol` 执行及 `gpt-6.1-terra` 审查的 Responses 受控闭环；列表中的 `gpt-5.4-mini` 在相同接口返回 NotFound，不能视为可用。Anthropic Chat 尚未真实调用验证。实际可用性仍须逐个模型和接口验证。远程地址默认要求 HTTPS；已授权私网 HTTP 需要显式开启 `allow_private_http`。

凭据由 `credential_env` 指定的服务端环境变量读取，也可保存到私有 `credentials.json`：它是以该环境变量名为键、密钥为值的 JSON 映射，文件权限须为 `0600`。模型密钥不交给浏览器或任务容器。`service.json` 保存数据库连接配置，也可用 `DOONEX_DATABASE_URL` 覆盖；`DOONEX_MODEL_CONFIG` 可指定模型配置文件。

计费模式有两种：

- `metered`：配置有来源的输入/输出单价与费用上限；价格未知时阻止启动。请求前预留调用数、token 和估计费用，返回后按实际用量结算；未知结果保留预留。
- `subscription`：仅在用户明确确认包月授权后配置。包月 Run 与批次的费用、费用预留及费用上限为 `null`，界面显示费用未知，不显示免费；仍强制限制调用数、token 和时长。共享后台日账可以保留已冻结的按量费用上限，它只约束当日按量小计。

时长从首次启动计算，包含暂停时间。角色可以选择不同模型，切换与恢复不清零预算；每个模型与工具动作记录所属角色，未知预留同时保留在运行总账和角色分项中。明确创建的新 Run 是一次新授权，混合计费时总货币费用为未知，按量部分仍受费用上限约束。当前没有自动重试；普通独立任务没有跨 Run 共享总账。M4 后台授权按同一 UTC 日期共用产品日账，新增授权、换模型或重启不会增加当天额度。费用限制约束新请求调度，不能保证供应商账单绝对封顶，也不从聊天产品套餐推断 API 权限。

## 检查与受控真实验收

不调用模型的本机检查：

```bash
.venv/bin/python scripts/test_local.py
.venv/bin/ruff check apps/server/doonex tests scripts
pnpm --dir apps/web build
git diff --check
git diff --cached --check
git status --short
```

`test_local.py` 启动一次性 PostgreSQL，运行包含真实 Docker 边界检查的测试，结束后清理测试数据库容器。SDK 合同测试使用模拟供应商响应；它们不消耗真实模型额度，也不替代真实发布验收。

确认凭据、模型和本轮授权范围后，可参照以下命令运行独立受控样例。每个 Run 受调用、token 和 300 秒时长限制，示例上限不保证完成：

```bash
.venv/bin/python scripts/live_acceptance.py --confirm-model-usage \
  --model ACTUAL_API_MODEL_ID --workflow execute_review \
  --sample-name m2-sample-acceptance-01 --max-calls 10 --max-tokens 100000
```

将 `ACTUAL_API_MODEL_ID` 替换为真实 API ID，并在授权范围内选择单 Run 调用和 token 上限。脚本生成独立的受控样例；已有 `sample-project` 及历史运行保留，后续样例使用不同的 `m2-sample-...` 名称。它仅执行本次创建的 Run，已有 worker 运行时拒绝启动；真实运行、会话和工件保存在私有数据目录。新 campaign 默认累计上限为 10 次调用/100,000 token；只有用户明确授权当前包月验收不设累计上限时，才追加 `--authorize-subscription-campaign`，且仅适用于 `subscription`。本项目这次授权变更及结果见 M2 实施记录，不代表其他用户已经授权。账本保留历史限额、授权变更、失败和未知用量；排队前预留本次额度，中断或结果未明时保留预留，不得删除或重置账本。按量模式仍须确认累计额度与单价。默认单执行流程可显式选 `--workflow execute`。脚本成功仍须完成 [PRD 发布验收](docs/PRD.md#8-发布验收与测量) 和 [M2 实施记录](docs/M2_IMPLEMENTATION.md) 中的其他检查。

## 有限候选生成（验收中）

「候选生成」可冻结一个已批准父 Skill、1–3 个 tuning 可见任务及有限模型预算，明确启动后只生成追加指令。原文件、资产和许可保留；产物为待检查，需预览、导入和独立审核。功能正进行整合验收，尚未通过真实模型发布验收；命令、恢复边界与当前证据见 [生成实施记录](docs/M4_GENERATION_PLAN.md)。

## 私有 Skill 与 Arena

在「Skills」导入本地目录并检查来源、许可证、权限及版本内容，明确批准后才能用于新任务。内容和目录结构冻结，源文件变化不会改写旧版本；使用前重新检查完整性。创建任务可以直接选择已批准版本，或明确选择同模型的当前场景推荐。

在「Arena」导入预先准备的任务集，选择一个已验证模型、2–3 个候选及重复次数，设定每个 Run 与整个批次的预算后创建评测。批次从固定代码和独立会话交错运行基线及候选，隐藏检查器只在模型执行结束后用于验证。每个结果可打开原始 Run 检查证据；完整次数不足、冻结内容失效或费用未知时，相应推荐保持空缺。选用与回滚必须明确操作，只影响后续任务。

任务集格式、接口与实际工作步骤见 [Arena 使用说明](docs/ARENA_USAGE.md)。完整回归不替代真实模型评测，当前验收状态见 [M3 实施记录](docs/M3_IMPLEMENTATION.md)。受控真实 Arena 验收使用已创建且未开始的批次，停止正常 worker 后运行：

```bash
.venv/bin/python scripts/live_arena.py --confirm-model-usage \
  --evaluation-id EXISTING_EVALUATION_UUID --model ACTUAL_API_MODEL_ID
```

脚本要求已有明确授权的包月 campaign，复核批次冻结模型及凭据，持有 campaign 与 worker 锁，只执行该批次。批次上限预留先写入原有账本，未知或中断记录保留；不自动创建、恢复或重试，不能以重复运行命令绕过已尝试记录。该脚本当前不支持按量 campaign。日常界面评测仍由正常 worker 按产品单 Run 和批次预算执行。

## 有限后台调优（首片验收中）

在「后台调优」选择有效的 `tuning` 任务集、2–3 个已批准且完整的 Skill 版本、一个模型配置、重复次数及 Run/批次/每日预算，保存关闭草稿。核对冻结条件和 UTC 窗口的本地换算后，勾选并明确启用才会授权模型用量。窗口必须位于同一 UTC 日期内；该 UTC 日的日预算在首次保存草稿时冻结，后续授权复用同一账本。

一次授权最多创建一个 Arena 批次。普通 Run 优先于手动 Arena，手动 Arena 优先于后台；后台在可核验的安全检查点让出，已派发动作等待结果或超时。可以撤销并停止新派发，在途结果继续结算，失败与未知预留保留；通过关联 Arena 查看全部尝试和运行证据。后台调优不具推荐推广资格。

当前离线检查通过，首轮真实测试的后台续接失败仍待修复，不代表首片已完成验收。没有循环建批、候选发现、模型生成候选、自动补跑或自动推广。实际检查、已知失败与等待边界见 [M4 实施记录](docs/M4_IMPLEMENTATION.md)。

受控真实让出验收要求已有有限授权草稿和从未开始、无动作或停止请求的 `paused` 前台样例，二者使用相同已确认模型；先停止普通 worker，再显式运行：

```bash
.venv/bin/python scripts/live_background.py --confirm-model-usage \
  --enable-authorization --authorization-id EXISTING_DRAFT_UUID \
  --foreground-run-id PRISTINE_PAUSED_RUN_UUID --model ACTUAL_API_MODEL_ID
```

脚本持有 campaign 与 worker 锁，先持久化批次及前台的完整额度，再启用草稿、创建唯一批次。它在首个后台模型请求预留后插入指定前台，验证安全让出、前台交付和原 SDK 会话续接；没有替换模型传输。它不提供通用样例创建或未知故障恢复，已有尝试禁止重跑。失败和未知记录保留；SIGKILL 或主机丢失后须先核查持久状态，不能直接重启普通 worker。

## 当前限制

- 支持多个稳定项目 ID；一个有效 worker 顺序处理 Run。协作仅为同一 Run 内执行者后接只读审查者，没有任意角色图、递归或并发执行器；M3 已完成受控验收，M4 首片有限后台调优已整合、验收中；M5 尚未实现。
- 独立副本从指定 Git 对象导出，不带 `.git`，也不带原工作区未提交内容；任务内不能直接使用 Git 管理命令。
- 新运行保存独立起始基线及摘要，后续审查和补丁生成不依赖原仓库路径仍在。旧 M1 记录没有该基线时保留原方式，仍需源仓库和起始 Git 对象可用。
- 拒绝符号链接、submodule、特殊文件、常见秘密文件名以及超出数量/大小限制的内容；常见生成缓存不进入快照。这一保守策略也会拒绝部分合法项目形态。
- 任务容器默认无网络、非 root、只读根文件系统，仅持久化 `/workspace`；依赖须已包含在固定镜像中，任务不能在线安装软件包。
- 每条验收命令使用同一固定快照的全新副本；验证失败、缺失或过期均不能标记已验证。
- 审查上下文仅含 `before/`、`after/`、`diff.patch` 与 `handoff.json`；容器挂载强制只读，交接报告摘要绑定数据库，内容、权限或额外文件变化使交接失效。审查意见与最终检查分别保存。
- SDK 集成包含针对固定版本的私有覆盖，用于禁用环境发现、额外工具和未计费元数据请求，以及逐工具暂停、动作回执与安全续接；升级必须复验这些合同。遥测与秘密边界见 [架构说明](docs/ARCHITECTURE.md)。

## 文档与来源

| 文件 | 用途 |
| --- | --- |
| [PRD](docs/PRD.md) | 范围、需求与发布验收 |
| [架构说明](docs/ARCHITECTURE.md) | 运行边界、数据、恢复与安全约束 |
| [M1 实施记录](docs/M1_IMPLEMENTATION.md) | 首次兼容性、受控验收与历史失败 |
| [M2 实施记录](docs/M2_IMPLEMENTATION.md) | 当前多项目与执行/审查验收状态 |
| [M3 实施记录](docs/M3_IMPLEMENTATION.md) | 私有 Skill/Arena 的实现、验收及限制 |
| [M4 实施记录](docs/M4_IMPLEMENTATION.md) | 有限后台首片、阶段性检查与待真实验收边界 |
| [Arena 使用说明](docs/ARENA_USAGE.md) | 本地任务集格式、批次、推荐和恢复 |
| [模型接入与验收](docs/MODEL_INTEGRATION.md) | cc-switch 参考、配置档、角色冻结与真实模型/账户查询结果 |
| [路线图](docs/ROADMAP.md) | 阶段依赖和退出条件 |
| [Skill Arena](docs/SKILL_ARENA.md) | 私有竞技的唯一详细规则 |
| [AGENTS.md](AGENTS.md) | 仓库协作与验证约定 |
| [CLAUDE.md](CLAUDE.md) | Claude 的规范入口 |

DooNex 是独立项目，复用 [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)，不代表 OpenHands 官方。当前锁定 SDK 1.60.1，上游采用 MIT 许可证；归属与许可见 [第三方说明](THIRD_PARTY_NOTICES.md)。DooNex 自有内容的许可证与开放范围仍未确定，不自动选择开源许可证，也未上线公共市场、收费或作者分成。
