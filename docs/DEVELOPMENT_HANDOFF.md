# DooNex 开发交接

更新：2026-11-14；用户于 2026-11-13 恢复开发，最新验收收据为 **UTC 2026-11-14**。用户已明确结束暂停；本页是当前启动入口。原暂停快照保存在 Git 提交 `6a0a227`，不要照其中“尚未执行”的命令重放本轮已执行动作。完整经过、失败和证据见[续接记录](M4_RESUMPTION_20261113.md)。

## 当前状态与源码

| 对象 | 当前事实 |
| --- | --- |
| 开发入口 | `codex/responses-transport`；真实后台验收源码 `b4625026e1e247dedeee8859b8cd5cf36086b040`；后续内容、交付及 SDK 期限接线 `f271bfb` 联合专项 390 项通过；初始化/凭据/领取期限 `61a9dd3` 已通过 2126 项完整回归；登记组件 `66fed5f` 和 Arena/Background 组件 `bfdb43f` 已整合，生产登记入口尚未启用；见下文 |
| 开发目录 | `/Users/benson/.codex/worktrees/doonex-delivery-docs/doonex` |
| 固定真实验收目录 | `/Users/benson/.codex/worktrees/responses-process-transport-tests/doonex`，detached `b462502`；保持干净，不在此开发 |
| main | `a965d7faaf83f6f70f22971da21ab5aa03e27257`，M3 已验收版本，尚未推进 |
| 常驻服务 | API、worker 均未加载；保留 plist 仍指向 M5 `f7a1161`，没有升级，不能自动恢复旧 worker |
| 数据库 | 原产品 PostgreSQL 保留；两轮历史超时、全部未知预留和旧日账保持 |

共享 `.venv` 指向 `/Users/benson/.codex/worktrees/doonex-m1/doonex/.venv`；Python 3.12.16 / SDK 1.60.1。该旧工作区仍是环境依赖，不能直接归档删除。前端 node_modules 也复用原 M1 目录。后台验收轮没有升级依赖或新建分支/worktree；后续 Sandbox 和 Git/export 实施各使用独立 worktree，父继续整合到已有入口，不另开整合分支。

续接先只读核对真实状态：

```bash
cd /Users/benson/.codex/worktrees/doonex-delivery-docs/doonex
git status --short --branch
git log -8 --oneline
git worktree list
git ls-remote --heads origin
PYTHONPATH=apps/server:tests .venv/bin/python -c 'import doonex; print(doonex.__file__)'
```

不能用安装路径推断实际源码。固定验收目录没有 `.venv`，使用开发目录的绝对解释器及明确 PYTHONPATH。任何中断先核对进程、收据、账本和外部副作用，不能盲目重放脚本。

## 已交付事实

- M1–M3 已通过受控验收；M5 自托管社区本机验收通过，社区仍关闭，没有公开部署、收费或许可证决定。
- M4 发现、生成分别已通过受控流程。**UTC 2026-11-14 的完整后台闭环、独立账目与界面核验现已通过**，正常完整前台等待上界仍未通过，整个 M4 不能称完成。
- 原子 helper 预留 `51cd8c7` 已整合为 `83fe6d0`；有限数据库维护 `6183ec3` 已整合为 `98f0885`。原 worker 启动过滤补丁已应用。不要重复 cherry-pick，也不要把两片历史组件测试相加作为最终整合数。
- 新 reserved 身份与预算同事务；永久 inode、CAS、期限只收紧及 v2 索引保留。新动作消除首次登记缺记录窗口，旧 record_missing 仍全局阻止，不能补造身份或从空闲 lease 推断没发送。未知预算不返还。
- 10 个生产维护索引及 `actions_helper_fenced_v2` 的实际定义和 valid/ready/live 核对通过。数据库维护每次最多一个重单位，worker 每循环两次；规模查询计划不等于完整延迟保证。
- SDK 会话容量 `42688c7` 已整合，正式 file_store=、无缓存有限 inventory、故障锁存、源形状/事件/fanout 限额和 close finally，详见[容量合同](M4_SESSION_CAPACITY.md)。worker 已向 SDK 本地持久化、恢复前核验、哈希和锁传入原 Run 的剩余 monotonic deadline；其余全链接线仍待完成。
- 严格 Responses 投影在 `b462502` 支持本网关已核验的中性元数据及细分用量；任意字段、非法工具和非零内置工具仍拒绝。无 retry/fallback，不改写 aggregate 用量。
- 后续离线切片已整合有限 Git/export（`d42a8ff`）、Sandbox（`1140aed`、`cc4d89e`）及父接线：共同 Run 剩余期限、SDK 本地读取/哈希/锁、isolated helper，以及 process/daemon 两类未知的持久全局阻止。API/Arena 新探测自身的未知也保存。普通确认、空 ps 和重启均不能清除。
- Tasksets、Skill/Library/Generation、Review/Evidence 的有限文件操作、调度回执及 Worker 调用链现已传递同一执行期限；`f271bfb` 也在每次 SDK Chat/Responses 请求的预算提交后重新收紧实际 timeout。联合专项 390 项通过，直接传输 timeout 不保证 OS 或供应商整请求结束。Arena/Background 组件 `bfdb43f` 的 91 项离线和 190 项 PG/Docker 检查通过；领取复核、凭据读取、首次时钟后的初始化和 completion 锁后期限复核已纳入 `61a9dd3`，完整回归 2126 项通过。
- `66fed5f` 已加入独立 controller operation 表和严格登记组件，82 项组件检查通过；**Store、全局 fence、API/worker/CLI 与两个生产 Popen 入口尚未接齐**。不能把该组件完成写成启动崩溃窗口已覆盖。Git 和 Sandbox 入口正分别在独立工作区实施；锁内 cleanup 的拆分和父接线尚待。

## 本轮真实验收：全部已经执行，禁止重放

普通 isolated 网关样例前四轮结构拒绝，各保留 1 unknown / 6,158 reserved tokens；第五轮 Run `676ddf89-a239-4ae5-9a55-01adf56dea0d` 已通过，3 calls / 2,842 tokens，三个 helper 实际 reaped。另三轮 direct 诊断也已计入原 campaign；私有脱敏 raw/pinned 材料已足以离线复现，不能继续凭真实调用猜字段。

完整新批次：

- 授权 `e0e5646d-8dc8-45d1-b1b9-826b48717565`；批次 `c0b5c1ec-ea81-48b0-9f8d-0482ba0f99f1`；前台 `ed83ada1-79e3-4e6b-b6b3-7c7e8519d4b8`。
- 固定原 tuning 任务集和两份批准候选；5 tasks × 3 arms × 1 repeat，15/15 completed_verified。后台 45 calls / 56,689 tokens；前台 3 / 3,219；unknown/reserved tokens 均零。48 个 helper 实际 reaped，无残留任务容器。
- 原 SDK session UUID、generation、首次启动时钟和待处理工具身份保持；前台先完成，后台再续接，未重放已完成工具。脚本直接驱动 Worker.execute，4.946589 秒 queue-to-running 仅为该 harness 样本。
- 日账已结算 45 calls / 56,689 tokens / 373 秒，日上限仍 160 / 4,000,000 / 7200；费用 null。原 11 月 6、7 日未知分配逐行不变。
- campaign 为 subscription_uncapped，25 条尝试，reserved_calls=615、reserved_tokens=8,807,060；原 24 条前缀逐项不变，绝不能清零或释放历史未知。
- 新私有 profile `isolated-m4-20261114` 被冻结授权引用，需要保留；profiles.json 因新增该档变化，旧档值/版本保持。model.json、凭据、service 与 plist 未改。
- 独立审计 259 项通过；浏览器 32 项检查、32 次实际 GET 和 7 张截图复核通过，无响应投影。只读浏览器前后全部 25 表、campaign 和配置一致，临时 API 已关闭，launchd 服务仍未加载。

模型为 gpt-6.1-sol / Responses / isolated_direct_v1 / subscription，单请求 60 秒。每 Run 6 calls / 120,000 tokens / 300 秒；批次 90 / 2,000,000 / 1800，前台独立 6 / 120,000 / 300。包月 campaign 无累计上限，但 Run、批次和冻结 UTC 日上限继续强制，费用未知为 null。

## 下一步的具体顺序

1. 核对本页最终检查收据及 Git/进程状态。普通 gateway、后台 run_once、所有 prepare/finish/finalize intent 均已消费；不要重跑同一授权、样例或清理脚本。
2. 先核对父接线专项和后续最终整合收据。有限 Git/export、Sandbox 与 SDK 持久化期限已整合，不重复 cherry-pick；两轮中途失败和诊断记录保留；`61a9dd3` 的最终完整回归 2126 项通过，旧 1826 项仅适用历史源码。
3. 补齐 Popen 前持久 operation 身份、真实终态 CAS 与全局 fence；普通动作 acknowledged 不能解除。将 cleanup 移出 Run 锁前须先有持久身份和 owner 校验，不只移动调用。
4. 已整合内容/证据/SDK 请求期限，不重复 cherry-pick。首次时钟后的初始化、凭据及后台领取 metadata 复核已整合并验证；继续完成有限 yielded 候选与前台优先、批次/窗口向同进程 monotonic 期限的映射。保留冻结输入完整核查，近容量和超限拒绝须实际验证。
5. 用真实 Worker.run、一次性 PostgreSQL、真实 Docker、本地 synthetic provider，在同一 monotonic 时间线覆盖正常/近容量/故障及启动恢复；记录前台入队提交区间到首次准入 commit。明确容量、队列和健康依赖后才给出数值操作目标。
6. 最终整合验证及 M4 剩余退出条件通过后推进 main，再考虑常驻服务的固定源码升级和回退收据。公开上线、收费和许可证仍单独确认。

这次不需要再跑一个完整真实后台矩阵来“补齐”已通过的相同源码证据；后续如改动相关执行路径，再按影响范围决定新验证。不得用已有后台成功、局部传输超时或几个耗时之和代替完整前台等待验收。

## 检查与私有证据

当前已提交交付接线 `f271bfb` 的联合专项 **390 passed / 3 warnings / 169.56 秒 / exit 0，无跳过**，146 份非 Markdown 输入与提交逐项绑定。登记组件 `66fed5f` 的 82 项及上下文组件 `bfdb43f` 的 91/190 项为各自组件证据，不能相加当全量。其后的初始化/凭据/领取期限接线 `61a9dd3` 完整回归 **2126 passed / 3 warnings / 828.30 秒 / exit 0，无跳过**，外层 832.395 秒；同一 151 个非 Markdown 输入的 162 项专项、Ruff 与前端构建均通过，并逐项绑定该提交，结果见续接记录。历史 `43f6147` 的 334 项和 `full-03` 的 220 passed / 1 failed / exit 2 均保留。

历史 `b462502` 全量 **1826 passed / 3 warnings / 905.09 秒 / exit 0，无跳过**；138 份非 Markdown 输入前后相同且绑定该提交，临时 PostgreSQL 与任务容器已清理。`689bc2a` 的旧全量为 1747 passed / 3 warnings；会话/Responses 联合专项 526 passed / 2 warnings；b462502 Responses 专项 222 passed / 1 warning。Ruff 与本轮实际前端构建已通过，当前源码与验证绑定见续接记录。

私有根为 `~/Library/Application Support/DooNex/verification/resume-20261113/`：

| 内容 | 路径 |
| --- | --- |
| 固定普通验收、失败和独立后验 | 根目录 gateway-01，`gateway-02` 至 `gateway-05` |
| direct 诊断与脱敏 raw/pinned | `gateway-diagnostic-01` 至 `gateway-diagnostic-03`，不得进入产品 Git |
| 真实验收源码 b462502 的全量与构建 | `full-02.json/.log`、`ruff-final-01.json`、`web-01.json` |
| 后续期限接线专项及中途失败 | `deadline-integration-*.json/.log`、`full-03.json/.log`；最终状态见续接记录 |
| 内容与交付期限接线 | `deadline-delivery-integration-01.json/.log`、`deadline-delivery-integration-01-source-binding.json` |
| 初始化/凭据/领取期限后续检查 | `deadline-initialization-*.json/.log`；不能从名称推断通过，须核对 exit、输入及源码绑定 |
| 登记与上下文组件 | `../m4-controller-operations/HANDOFF.md`、`../m4-context-deadline/HANDOFF.md`；生产启用仍待父接线 |
| SDK 容量与近容量样本 | `combined-*.json/.log`、`session-capacity-benchmark.json`、`session-benchmark-source-binding.json` |
| 实际索引定义核对 | `index-audit.json`，首轮失败保留 |
| 唯一新后台尝试 | `background-20261114/execution.intent.json`、`execution-receipt.json`、`audit-02/independent-after.json` |
| 浏览器与只读后验 | `background-20261114/browser-02/`、`post-browser.json`；首轮浏览器缺 executable 的失败保留 |
| 后续完整路径设计 | `../m4-next-wait-design/REPORT.md`，其中已实施部分以本页更新为准 |

原始运行材料、配置、源码、凭据和日志都属于私有证据，产品 Git 只放实现与脱敏事实。最终回归后已核对临时 PostgreSQL 和任务容器不存在；下次仍先检查实际进程，不能按旧 PID 清理。

## main 与历史分支

分支多主要来自 M1–M5 的隔离实现与审查，cherry-pick 后原提交未必是当前祖先；不能只靠 branch --merged 判断是否交付。暂停时的详细分类见 `verification/pause-branch-audit-20261106/BRANCHES.md`。两片当时未整合的代码现已纳入当前入口，没有为了本轮再开重复分支。

**当前还不能按既定退出条件推进 main：完整前台等待上界未通过。** 满足该条件并完成最终整合回归/证据核对后，将当前整合分支正常推进到 main；若 main 仍为祖先可 fast-forward，否则先整合远端新提交并重验，不 force push。不需要逐个合并所有历史子分支。

本轮不删除分支或 worktree。之后先区分已纳入成果、独立未整合改动、脏/忽略文件、服务或固定验收引用；managed worktree 用应用归档保留恢复快照。共享 venv/node_modules 的 M1 依赖和仍被引用的固定目录须先处理，不直接清理。
