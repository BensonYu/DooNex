# DooNex 开发交接

更新：2026-11-14；用户于 2026-11-13 恢复开发，最新验收收据为 **UTC 2026-11-14**。用户已明确结束暂停；本页是当前启动入口。原暂停快照保存在 Git 提交 `6a0a227`，不要照其中“尚未执行”的命令重放本轮已执行动作。完整经过、失败和证据见[续接记录](M4_RESUMPTION_20261113.md)。

## 当前继续点（有限后台首片收尾已完成）

用户随后明确继续 M4 收尾，下面“持续目标清除”一节保留为历史快照，不是当前停止指令。当前只处理执行控制/清理恢复、共同期限、真实 Worker.run 完整等待矩阵和最终验证；不扩展权限、远程访问、App 或云调度，不修改产品模型、凭据或预算。

父工作区 `/Users/benson/.codex/worktrees/doonex-delivery-docs/doonex`，最终行为验收非 Markdown 源码 `25c221e3f9cca942341cc3c3678a08bc3d94af26`，176 项输入已与提交逐项绑定。收尾文档仅修改 Markdown，提交前后已核对非 Markdown 字节仍匹配验收源码；当前分支及 main/远程状态见本页末尾的实际核对。

`m4-normal43-specialized-05` 与 `m4-closure-full-06` 均已完整结束且退出0；具体通过数、警告、跳过、耗时、输入清单及日志SHA见[最终收尾记录](M4_RESUMPTION_20261113.md#m4-final-closure)。冻结v3独立审计核对43配置/39个唯一实际PASSED节点、两轮同源码与原90秒目标，invalid/pending均为空；151精确节点覆盖、故障/超限/重启、实际资源终态、Ruff06与构建06另有核对。不得用专项或局部通过替代这些门槛。

原full01–full05、准备失败、observer/命令错误和相关整合失败全部保留，均不提供最终正常覆盖。Git/锁、Sandbox及Responses测试仅修阶段到达/原期限/真实终态的证据，原短窗口与产品预算不变；过程见续接记录，不重复执行已结束检查。真实模型证据仍为 `b462502` 的普通样例及完整后台闭环，本轮等待收尾零真实模型调用。

有限census为yielded16、启动active/interrupted64、启动活动Arena64，超限完整拒绝；单Worker时钟最多4096 scope。43种正常配置只在[冻结健康依赖与容量条件](M4_WAIT_ACCEPTANCE.md)内满足90秒目标，不是通用SLA。最终两轮实际TMPDIR为 `/private/tmp/doonex-m4-verification-20261114`，T7历史失败保留。常驻API/worker未加载，plist仍指向旧M5 `f7a1161`，没有升级服务；M5社区仍关闭。

原遗留停止容器已从原始测试日志核实归属后精确回收，私有收据 `resume-20261113/legacy-fixture-container-recovery-01.json`（SHA256 `ddf42b48b50dfd158f40e50457d5fb4e2197812b2f940470e4bc23ec05f84369`）；没有清空产品账本或历史未知。当前检查与收据统一保存在 `verification/resume-20261113/`，先核对进程及收据再续接，不重复启动仍在运行的检查。

## 2026-11-14 持续目标清除后的恢复点（已续接的历史快照）

本轮持续目标已被用户清除，已停止派发新开发/测试切片；正在执行的父检查只做收尾。后续已明确继续，当前状态见页首；保留本节作为当时快照。不新建重复整合分支，也不按旧计划重放真实模型验收。`main` 的剩余退出条件没有变化。

| 对象 | 保存状态与下一步 |
| --- | --- |
| 父整合分支 | `codex/responses-transport` 的 `ac0a075665020ceabe9cd34e2d155b4c369f24b9` 已推送并逐哈希核对 GitHub；其前已纳入 Tasksets `b794e70`、Background/Scheduling `f1cdeb5` 与调度预检共同期限 `514774a` |
| 父当前修复 | 保留真实 cleanup attempt、未知回执库存和 overflow；普通 API resume 在 Review/Git/容器探测前拒绝已有 fence；Worker 最后 model/tool/verification 准入传同一期限并复检。联合 `controller-preflight-integration-02` 是 **396 passed / 1 failed / exit 1**，不能写成通过；原历史未知恢复用例已按既有安全合同迁移，尾部复验结果见本轮续接记录 |
| Community 测试迁移 | `codex/m4-community-evidence-actors` / `ab36a07ee63fabdfc1a8cce0abe74a49ae5b18dd`，目录 `m4-bounded-git-export/doonex`；仅 `test_community_evidence.py`、`test_community_review.py`。组件 **36 passed / exit 0**、120 个输入与原提交绑定；尚未 cherry-pick 到父，未推送；父所需 `test_community_integration.py` registry alias 已在 `ac0a075` |
| Generation 测试迁移 | `codex/m4-generation-evidence-actors` / `82dafbb857720a5f002e68eaab1efe135f67bf21`，同名 worktree；仅 `test_generation.py`、`test_content_deadline.py`。四模块组件 **113 passed / exit 0**、121 个输入与原提交绑定；尚未 cherry-pick 到父，未推送；共享 `setup_generation` 没有改动 |
| yielded 共同期限候选 | `codex/m4-yielded-shared-deadline`，目录 `m4-sandbox-deadline/doonex`，HEAD `514774a`；**未暂存、未提交**的 `apps/server/doonex/scheduling.py` 和新增 `tests/test_yielded_deadline.py`。candidate003 静态及独立只读审查通过，19 项行为检查完全未执行；不清理该工作区 |
| Worker 硬中断测试候选 | `codex/m4-worker-kill-actors` / `e7445607e600ff2ef907e1121cd2eeb7dee6e116`，同名 worktree，父提交恰为 `ac0a075`；仅 `test_worker_process.py`、`test_m3_api.py`，工作区干净。静态及 25 项收集通过，真实 PG/Docker 行为完全未执行；尚未整合、未推送 |
| 尚无实现的设计 | 有限 yielded cap+1/完整集合审计及跨 Run/批次/分配/窗口的进程生命周期 monotonic 映射仍只是设计。clock core 新任务仅做只读准备，未创建分支/worktree或源码；不能当成已实现 |

上述子工作区均位于 `/Users/benson/.codex/worktrees/`；已有提交/候选、私有收据和原失败保留。完成组件的原输入绑定不代替父整合验收，真实硬中断候选依赖父当前提前 fence 修复。父尾部 `controller-scheduling-tail-01` 已结束，**107 passed / 1 failed / exit 1**：历史私有 fixture 清点得到 `existing_task_container` 而拒绝执行，迁移后的历史未知拒绝恢复用例已通过。一次性 PG 与 pytest 已退出，留下一个已停止的 Run-label 容器；只做了私有清点，没有删除，不能按标签宽扫清理。

下一次先核对尾部失败收据与该容器的原操作归属，再依次取得独占 PG/Docker 资源验证 yielded 19 项及硬中断两个模块，确认清理后才移交；通过后仅整合各自负责文件并完成新完整回归。不要再次执行已完成的 Community/Generation 原组件轮来冒充最终整合，也不从失败消息猜测容器归属。父全部 160 个非 Markdown 尾部输入已逐项绑定 `ac0a075`；失败收据及清点为 `resume-20261113/controller-scheduling-tail-01.json/.log`、`controller-scheduling-tail-01-source-binding.json`、`controller-scheduling-tail-01-container-inventory.json`，后者明确是只读、未清理。

私有证据相对 `~/Library/Application Support/DooNex/verification/`：Community 与 Generation 分别为 `m4-community-evidence-actors/`、`m4-generation-evidence-actors/` 的 `pg_001.json/.log`、`HANDOFF.md`、`FINAL_RECEIPT.json`；yielded 为 `m4-yielded-shared-deadline/static-3.json`，SHA256 `1585c14fa6deba16312dc63c6b185dda03b405cd1166cec0675cd67e1aa7f68d`；硬中断为 `m4-worker-kill-actors/STATIC_RECEIPT_002.json`，SHA256 `39bfd0d258c41d9378023b7809d8aa05f1ad0b53eaec48784501b9533d8c285d`。各片独立审查位于 `m4-wait-control-review-20261114/`。这里只保存脱敏事实，原始材料不进入产品 Git。

## 上次保存时的状态与源码（历史快照）

| 对象 | 当前事实 |
| --- | --- |
| 开发入口 | `codex/responses-transport`；真实后台验收源码 `b4625026e1e247dedeee8859b8cd5cf36086b040`；后续期限 `61a9dd3` 的 2126 项完整回归通过；其后 controller 生产登记、actor 与锁外 cleanup 已纳入 `a5c7ece`，160 项专项、Ruff 与构建通过；旧调用方迁移及新整合全量仍待，见下文 |
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
- M4 发现、生成分别已通过受控流程。**UTC 2026-11-14 的完整后台闭环、独立账目与界面核验现已通过**，有限后台首片的条件内等待与最终整合已在 `25c221e` 通过受控核验；循环生成/评测、重复批次、自动推广和质量改善试验仍未完成，整个 M4 不能称完成。
- 原子 helper 预留 `51cd8c7` 已整合为 `83fe6d0`；有限数据库维护 `6183ec3` 已整合为 `98f0885`。原 worker 启动过滤补丁已应用。不要重复 cherry-pick，也不要把两片历史组件测试相加作为最终整合数。
- 新 reserved 身份与预算同事务；永久 inode、CAS、期限只收紧及 v2 索引保留。新动作消除首次登记缺记录窗口，旧 record_missing 仍全局阻止，不能补造身份或从空闲 lease 推断没发送。未知预算不返还。
- 10 个生产维护索引及 `actions_helper_fenced_v2` 的实际定义和 valid/ready/live 核对通过。数据库维护每次最多一个重单位，worker 每循环两次；规模查询计划不等于完整延迟保证。
- SDK 会话容量 `42688c7` 已整合，正式 file_store=、无缓存有限 inventory、故障锁存、源形状/事件/fanout 限额和 close finally，详见[容量合同](M4_SESSION_CAPACITY.md)。worker 已向 SDK 本地持久化、恢复前核验、哈希和锁传入原 Run 的剩余 monotonic deadline；后续全链接线与受控结果见[最终收尾记录](M4_RESUMPTION_20261113.md#m4-final-closure)，不能从组件时限推出通用等待保证。
- 严格 Responses 投影在 `b462502` 支持本网关已核验的中性元数据及细分用量；任意字段、非法工具和非零内置工具仍拒绝。无 retry/fallback，不改写 aggregate 用量。
- 后续离线切片已整合有限 Git/export（`d42a8ff`）、Sandbox（`1140aed`、`cc4d89e`）及父接线：共同 Run 剩余期限、SDK 本地读取/哈希/锁、isolated helper，以及 process/daemon 两类未知的持久全局阻止。API/Arena 新探测自身的未知也保存。普通确认、空 ps 和重启均不能清除。
- Tasksets、Skill/Library/Generation、Review/Evidence 的有限文件操作、调度回执及 Worker 调用链现已传递同一执行期限；`f271bfb` 也在每次 SDK Chat/Responses 请求的预算提交后重新收紧实际 timeout。联合专项 390 项通过，直接传输 timeout 不保证 OS 或供应商整请求结束。Arena/Background 组件 `bfdb43f` 的 91 项离线和 190 项 PG/Docker 检查通过；领取复核、凭据读取、首次时钟后的初始化和 completion 锁后期限复核已纳入 `61a9dd3`，完整回归 2126 项通过。
- `66fed5f` 的独立 controller operation 组件已接入生产：Workspace `4d68f30`、Store/fence `e4b0834`、Sandbox `3a910c5`、CLI actor `cb4416c`、Git 异常身份 `c23a6bf`，以及 API/Worker/cleanup/Evidence/Review/Arena/Scheduler 的 `a5c7ece`。真实 reserve/launch 提交后才 Popen；API 请求和 Worker 入口各自关闭 actor；停止意图先提交、锁外有限清理、再按 nonce/owner/request CAS。该历史源码的160项专项通过；后续 `25c221e` 的最终整合和受控完整Worker.run证据见[最终收尾记录](M4_RESUMPTION_20261113.md#m4-final-closure)。旧测试迁移 `93bd7e3`、`18fd8ed`、Git filters `6b4a404` 已纳入，后续调用方迁移亦已纳入最终候选，不能用 fake registry 或生产旁路替代。
- `scripts/live_background.py` 的公开运行入口已固定拒绝 `legacy_background_harness_disabled`，在读取配置、DB 或 campaign 前零副作用；历史宽范围容器清点不能提供真实 Run 绑定。旧完整后台验收保留，不从这个 harness 重放。

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

1. 续接先核对Git、真实进程与终态收据；不要重启已完成检查。普通gateway、后台run_once及原prepare/finish/finalize intent均已消费，不重放真实模型授权或清理脚本。
2. 核对文档提交的非Markdown输入与 `25c221e` 一致，并保留全部原失败、冻结v3/151节点后验、Ruff/构建和资源终态收据。
3. main已正常快进并同步GitHub，见页末核对点；下次只核对实际本地/远程SHA和新增变化，不重复制造整合。不能force或删除历史分支/worktree。
4. 常驻服务升级作为单独固定源码操作处理：先核对配置、未决记录、worker锁与回退路径，不能启动仍指向旧M5的worker。权限/App/云调度/公开上线和收费不在本轮范围；后续开发按路线图明确切片。

历史 `b462502` 真实后台矩阵保持原源码证据；本轮恢复界面有API和构建验证，没有新的浏览器点击验收。

## 检查与私有证据

历史交付接线 `f271bfb` 的联合专项 **390 passed / 3 warnings / 169.56 秒 / exit 0，无跳过**，146 份非 Markdown 输入与提交逐项绑定。初始化/凭据/领取期限 `61a9dd3` 的完整回归 **2126 passed / 3 warnings / 828.30 秒 / exit 0，无跳过**，外层 832.395 秒；同一 151 个非 Markdown 输入的 162 项专项、Ruff 与前端构建均通过。后续 controller 生产接线 `a5c7ece` 的 **160 passed / 2 warnings / 35.37 秒 / exit 0**、Ruff 与前端构建已绑定同一 159 个输入；这不是新完整回归。旧入口诊断在 `6b4a404` 为 **76 passed / 9 failed / 15 errors / 85.98 秒 / exit 1**，外层 89.699 秒，输入不变，临时 PG 已清理，失败完整保留。各项证据与限制见续接记录。

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
| 登记与上下文组件 | `../m4-controller-operations/HANDOFF.md`、`../m4-context-deadline/HANDOFF.md`；其中未接线为历史事实 |
| controller生产接线与最终等待整合 | 历史entrypoints/existing收据及全部失败保留；最终 `m4-normal43-specialized-05.json/.log`、`m4-closure-full-06.json/.log` 和冻结v3/151节点报告的确切路径与SHA见续接记录 |
| SDK 容量与近容量样本 | `combined-*.json/.log`、`session-capacity-benchmark.json`、`session-benchmark-source-binding.json` |
| 实际索引定义核对 | `index-audit.json`，首轮失败保留 |
| 唯一新后台尝试 | `background-20261114/execution.intent.json`、`execution-receipt.json`、`audit-02/independent-after.json` |
| 浏览器与只读后验 | `background-20261114/browser-02/`、`post-browser.json`；首轮浏览器缺 executable 的失败保留 |
| 后续完整路径设计 | `../m4-next-wait-design/REPORT.md`，其中已实施部分以本页更新为准 |

原始运行材料、配置、源码、凭据和日志都属于私有证据，产品 Git 只放实现与脱敏事实。最终两轮后已按各自收据核对临时PostgreSQL、测试进程及Run/test标签容器终态；下次仍先检查实际进程，不能按旧 PID 清理。

## main 与历史分支

分支多主要来自 M1–M5 的隔离实现与审查，cherry-pick 后原提交未必是当前祖先；不能只靠 branch --merged 判断是否交付。暂停时的详细分类见 `verification/pause-branch-audit-20261106/BRANCHES.md`。两片当时未整合的代码现已纳入当前入口，没有为了本轮再开重复分支。

**有限后台首片的等待和最终整合验收阻塞已解除，main已正常快进并同步GitHub。** 2026-11-14 的实际核对点为 `204b842baa2e3c980c22f4068aacc22b38e50e7d`：本地main、`codex/responses-transport`及GitHub两条同名分支完全一致。main从 `a965d7faaf83f6f70f22971da21ab5aa03e27257` 快进，原远端整合分支为 `78d893d6e53e48276f04879ebfad82b90e740e21`；原子推送exit0，没有force或覆盖远端新增历史。私有 `resume-20261113/m4-main-sync-evidence-01.json` SHA256 `e8c42cd96a8eb121c7114c7ef8856f90e7810c03c2edd3a72d103fb9c554c464` 保存该核对。

该核对点之后仅补记本页同步事实和README的main安装命令，再正常快进/同步文档提交；最新完整SHA以Git及私有 `resume-20261113/m4-main-sync-final-02.json` 为准，避免在提交内引用自身SHA。文档更新前后176项非Markdown输入继续绑定 `25c221e`，不重复运行已通过且源码未变的行为检查。无需逐个合并历史子分支；服务未升级。

本轮不删除分支或 worktree。之后先区分已纳入成果、独立未整合改动、脏/忽略文件、服务或固定验收引用；managed worktree 用应用归档保留恢复快照。共享 venv/node_modules 的 M1 依赖和仍被引用的固定目录须先处理，不直接清理。
