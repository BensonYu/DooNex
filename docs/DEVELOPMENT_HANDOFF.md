# 开发暂停与续接交接

交接日期：2026-11-06（纽约；UTC 2026-11-07）。用户要求暂停开发，次日再继续；自动推进目标已由用户清除。**本页记录暂停点，不授权自动恢复开发、模型验收或服务升级。** 后续任务在用户明确继续后先读本页，再读 [AGENTS](../AGENTS.md)、[README](../README.md)、[PRD](PRD.md)、[架构](ARCHITECTURE.md) 和 [路线图](ROADMAP.md)。

## 1. 从哪里继续

| 用途 | 分支 / 固定提交 | 本机工作区 |
| --- | --- | --- |
| 当前整合入口 | `codex/responses-transport`；暂停前已同步 `83896dc475b8510da7473606e86fad32d10c0a84`，本次交接文档追加在其后 | `/Users/benson/.codex/worktrees/doonex-delivery-docs/doonex` |
| 主分支，M3 已验收版本 | `main` / `a965d7faaf83f6f70f22971da21ab5aa03e27257` | `/Volumes/T7/projects/doonex` |
| 正在运行的 M5 本机服务 | `codex/m5-community-integration` / `f7a116123c076e30018fe17449b259d07c6c3977` | `/Users/benson/.codex/worktrees/doonex-m5-community-controller/doonex` |
| 为下一次网关验收固定的源码 | detached / `83896dc475b8510da7473606e86fad32d10c0a84` | `/Users/benson/.codex/worktrees/responses-process-transport-tests/doonex` |

主分支、整合代码、常驻服务是三个不同版本。不要因打开某个 worktree 就认为服务已升级；不要把固定验收目录或正在运行服务的目录拿来继续开发。

续接先做只读检查：

```bash
cd /Users/benson/.codex/worktrees/doonex-delivery-docs/doonex
git status --short
git log -5 --oneline
git branch -vv
git worktree list
git ls-remote --heads origin
```

共享 `.venv` 留有旧 editable 安装，并依赖 `/Users/benson/.codex/worktrees/doonex-m1/doonex`；清理历史工作区前必须保留或先迁移这项依赖。运行测试或应用时显式设置 `PYTHONPATH=apps/server:tests`，并核对实际导入位置。固定验收工作区没有 `.venv`，使用整合目录的绝对解释器路径；不要以安装路径推断实际源码：

```bash
PYTHONPATH=apps/server:tests .venv/bin/python -c 'import doonex; print(doonex.__file__)'
```

## 2. 已完成与仍未完成

- M1–M3 已完成受控验收；M5 自托管社区的本机受控验收通过，社区仍关闭。没有公开部署、收费或许可证决定。
- M4 的候选发现与有界生成分别通过受控验收；完整真实后台批次、正常条件下的完整前台等待上界仍未通过。两者都是合并当前整体成果到 `main` 前的退出条件。
- `83896dc` 所含应用代码提交为 `31d937f`。可选 `isolated_direct_v1` 已接入 SDK、预算、worker、全局阻止、恢复 API 与界面；**1650 passed、3 warnings、exit 0、无跳过**。Ruff 与前端构建通过，输入摘要匹配最终提交。详见 [Responses 记录](M4_RESPONSES_TRANSPORT.md)。
- 该模式尚未真实调用网关，常驻服务没有切换。它只限制传输子进程期限，不是整请求或前台等待上界。本机回收不能证明远端请求或计费结束。
- 已整合版本仍有预算提交后、helper 身份登记前硬退出的 `record_missing` 限制，保持全局阻止；不能凭空闲 lease 或手改数据库解除。修复候选在独立分支，未整合前本限制仍成立。

完整检查证据在私有数据目录的 `verification/m4-responses-transport/`：`full-02.json/.log`、`verified-commit-01.json`、`ruff-final-02`、`web-final-01` 与 `delivery-scan-01.json`。测试启动时 HEAD 为 `a350f25` 加接线改动，132 份非 Markdown 输入前后相同并匹配 `31d937f`；不能改写成测试启动时就是最终提交。保留 `full-01` 的两项失败和后续修复、12 项针对性回归。组件测试数量不能相加冒充整合总数。

## 3. 两个尚未整合的切片

以下工作与根目录分开保存，不能直接称为最终产品已验证。各 owner 的私有 `HANDOFF.md` 记录最终提交、源码摘要、真实检查、失败和清理状态；恢复时必须核对实际 Git 状态。

| 切片 | 分支 / 工作区 | 文件范围及交接 |
| --- | --- | --- |
| 原子预算预留与 helper 身份 | `codex/responses-atomic-reservation` / `51cd8c77ebb1dcb3570a47f87d08ec50015cd67f`；`/Users/benson/.codex/worktrees/responses-atomic-reservation/doonex` | `budget.py`、`sdk.py`、`worker.py` 的 isolated 接线、三个 Responses 模块及直接相关测试；`verification/m4-helper-reservation-implementation/HANDOFF.md` |
| 有限数据库维护 | `codex/m4-database-maintenance` / `6183ec342c992b75590792f146f688af4cb54095`；`/Users/benson/.codex/worktrees/m4-database-maintenance/doonex` | `store.py`、`background.py`、`scheduling.py`、`arena.py` 及两个相关测试；`verification/m4-database-maintenance-implementation/HANDOFF.md` |

原子预留的合同：事务外核验永久 lease inode；同一预算事务保存完整 `reserved` 身份；沿原 attempt/request/nonce/hash 做 CAS，持久确认后才 spawn；期限只能收紧；显式迁移 `actions_helper_fenced_v2`。新 reserved 的人工解除不能释放预算，旧缺记录不得补造身份。

原子预留已保存本地提交，**尚未推送或整合**，工作区 clean。最终定向检查 `pg-02` 为 **404 passed、3 warnings、125.06 秒、exit 0**，16 个源码/测试文件检查期间未变；Ruff 通过，本片进程与一次性 PostgreSQL 已清理。`helper-01` 的 5 项失败、`pg-01` 的 1 项失败及后续夹具/断言合同修正均保留。此结果不替代父分支整合回归或生产索引安装核查。

数据库切片每次 `maintain()` 只处理一个重维护单位，worker 每循环调用两次，因此是每循环最多两个；enabled 授权使用稳定 keyset，未来/阻止候选不能饿死后项。关联损坏继续明确失败，保留 owner、容器与全局阻止检查，结算仍从原 `claimed_at` 到实际结算时刻，可能保守增加秒数。

数据库切片已保存本地提交，**尚未推送或整合**，工作区 clean。最终 11 个相关测试文件为 **296 passed、2 warnings、524.98 秒、exit 0**，6 个源码/测试文件摘要未变化；Ruff 通过。100,000 Run、100,000 Action 及各 10,000 Arena/授权/分配的 28 份实际 default/generic 查询计划全部命中预期索引，覆盖 10 个新索引；首轮 generic plan 全表扫描失败保留。此样本不构成等待上界。本片测试进程、临时 PostgreSQL 与临时 DSN 配置已清理。两个分支均基于 `31d937f`，不是彼此的后续版本，后续在整合入口逐个评审合入。

数据库 owner 未编辑 `worker.py`。私有 `worker-startup-filter.patch` 仅做过 `git apply --check`，**尚未应用**；它将启动时完整 Arena 历史读取替换为 `arena.list(states={"running", "cancelling"})`。整合者须在两个分支合入后审查并应用，再验证最终源码。不要把建议补丁的检查当成产品已覆盖启动路径。

## 4. 明天的执行顺序

1. 核对本页、两个切片的最终交接、工作区与进程状态。先确认上次动作是否执行，不能盲目重跑验收、初始化 schema 或清理资源。
2. 按下面的只读计划，在固定 `83896dc` 源码上完成一次独立网关验收；这一步在暂停前**没有执行**。使用新样例，不接入旧后台授权；若状态已变化，先重做前检。
3. 审查并整合两个独立切片及 worker 建议补丁。保留各自失败记录，重新执行最终版本的相关回归、全量检查及静态检查；前端有变更时重新构建。通过之前不改写 `1650 passed` 的适用版本，也不把缺记录限制写成已消除。
4. 实施最小会话/文件容量切片，再覆盖共同剩余期限、容器清理与前台初始化。设计在私有 `verification/m4-next-wait-design/REPORT.md`，尚未实施。复用 SDK 正式 `file_store=`；构造前做有限 inventory，故障锁存，序列化前限制输入/事件/状态/branch。SDK EventLog 会吞普通 `list` 异常并可能视为空历史，必须用实际 SDK 故障注入证明不会覆盖历史或继续准入。
5. 依 [等待控制计划](M4_WAIT_CONTROL_PLAN.md) 测真实 `Worker.run` 的同一 monotonic 时间线，包含在途动作、结算、检查点、close、维护、前台初始化与首次准入。明确容量、队列与健康依赖，区分正常让出与故障阻止。不能把若干阶段超时或小样例相加作为完整上界。
6. 在新实际 UTC 日预算内完成新的有限后台矩阵、独立账目和界面核验，保留旧失败、未知和预留。若供应商再超时，保留失败，不能通过改评分、释放未知或重置账本造出通过。
7. 更新实施记录、README 与路线图，核对最终证据和源码，再推进主分支与服务升级。公开上线、收费与许可证仍需单独决定。

这些是有顺序的恢复步骤，不是明天必定完成的承诺。未完成设计可继续分片，但不要再创建重复整合分支。

## 5. 真实网关验收：已经准备，尚未启动

完整操作计划在私有 `verification/m4-isolated-gateway-readonly-plan-01/PLAN.md`，SHA-256 为 `7116144fffeacc936af08e9a0974786520ab92892a6c8b786e5a1c1971bddb76`。同目录 `model.proposed.json` 仅增加 `responses_transport="isolated_direct_v1"`；它没有安装到现用配置。沿用 `gpt-6.1-sol`、Responses、subscription、原 60 秒模型超时和凭据引用，不传 `--model-profile complex`，否则会选回原 direct 档。

拟定普通样例 `m3-sample-isolated-gateway-01`，`execute`，最多 4 calls / 100,000 tokens / 300 秒 Run 时长。这不是 M3 Arena 或 M4 P19 完整后台矩阵，不能充当其验收次数。完整命令只在私有计划中维护，执行前重新核对，不因复制命令而获得重复调用授权。

暂停前只读前检曾得到：凭据可读、直连环境支持、无活动 Run/批次、`execution_fenced=false`。原 campaign 为 `subscription_uncapped`，16 个 attempts，`reserved_calls=551`、`reserved_tokens=8,711,136`；样例尚不存在。**这些是时间点快照，明天必须重新读取。** 凭据仅在私有配置，不打印内容、不写入 Git。

执行要点：先保存原配置、campaign、旧 Run/action、Arena、后台日账及 allocation 的摘要；停 API 防止新入队，复核无在途任务后停 worker，确认身份、端口、全局 worker 锁及容器状态。脚本在取得 worker 锁前会初始化 schema，不能用试跑脚本探测旧 worker 是否仍在。

副作用之前用 `O_EXCL` 一次性 intent 绑定源码、配置、样例、原账本摘要与预算。脚本本身允许同样例再建 Run，不能自动重试；任何中断先核对 campaign、项目、Run/action/helper 和进程。后验须确认只新增一次普通尝试、原历史/未知与日账未改、实际检查与交付摘要匹配、helper 真实终态证据充分。

旧 `f7a1161` worker 不认识 helper 全局阻止。出现 unknown、缺记录、未决 helper 或可恢复的新模式任务时，**不能自动恢复旧 worker**，也不能让旧代码按 direct 续跑。只有确定安全且核验旧版本兼容后才恢复；若升级，使用已验证的固定源码和回退收据，不能指向开发工作区。

## 6. 后台日预算与历史失败

用户授权包月 campaign 无累计调用/token 上限；单 Run、批次与冻结 UTC 日上限继续生效，费用未知为 `null`。不能重新请求同一授权来清账，也不能把包月理解成无限后台日预算。

| UTC 2026-11-07 的完整尝试 | 实际结果 | 未知保留 |
| --- | --- | --- |
| `4ba41df2-9001-4a26-9cf8-7f65948af74b` | 9 通过、1 超时、5 取消；28 calls / 34,246 known tokens | unknown 1 / reserved 6,042 tokens；整批日预留 90 calls / 2,000,000 tokens / 1,800 秒 |
| `feb5f6ee-64b3-4480-b1a1-46f53c46c5be` | 3 通过、1 超时、11 取消；10 calls / 11,232 known tokens | unknown 1 / reserved 7,204 tokens；整批日预留 60 calls / 1,800,000 tokens / 1,800 秒 |

当天冻结上限为 160 calls / 4,000,000 tokens / 7,200 秒，剩 10 calls / 200,000 tokens / 3,600 秒，不够完整矩阵。下一 UTC 日从 **2026-11-08 00:00 UTC，即纽约 2026-11-07 19:00** 开始；用户次日上午继续时仍未到这个边界。不得提前改日期、增加当天冻结额度、重放旧授权或释放未知。完整失败和后验见 [M4 实施记录](M4_IMPLEMENTATION.md)。

## 7. 服务与私有证据

本次暂停只收尾开发，没有停止、升级或重启原服务，没有真实模型调用、生产数据库写入或新的验收 intent。暂停核对时，`local.doonex.api`、`local.doonex.worker` 仍绑定 `f7a1161`；本机 API 监听 `127.0.0.1:8741`，`5173` 没有监听，浏览器旧标签不代表开发服务正在运行。进程 PID 是易变信息，恢复前重新读取。

三个子 Agent 均已停止：两个实现 owner 保存上述本地提交，第三个仅保存只读操作计划与分支审计。没有遗留本轮测试进程或临时测试数据库；这里不把正常保留的产品 API、worker 与产品数据库列作开发残留。

launchd plist 位于 `~/Library/Application Support/DooNex/runtime/api.plist` 与 `worker.plist`，不在 `~/Library/LaunchAgents`。当前 `KeepAlive=false`、`RunAtLoad=true`；恢复必须检查实际 label、cwd、源码、PYTHONPATH 与 worker 锁，不按旧 PID 直接发信号。

本页所有 `verification/...` 路径都相对于私有 `~/Library/Application Support/DooNex`，不属于产品仓库；包含原始日志与运行数据的目录不得提交。重要索引：

| 内容 | 私有路径 |
| --- | --- |
| 1650 项最终整合回归与交付核验 | `verification/m4-responses-transport/` |
| helper 绑定与全局阻止独立审查 | `verification/m4-helper-binding-review/REVIEW-04.md`、`verification/m4-helper-fence-review/REVIEW.md` |
| 原子预留设计 | `verification/m4-helper-reservation-design-01/REPORT.md` |
| 原子预留实现、最终定向验证与暂停交接 | `verification/m4-helper-reservation-implementation/HANDOFF.md` |
| 数据库维护设计与原问题探针 | `verification/m4-database-maintenance-review/run-20261107T031955Z/REPORT.md` |
| 数据库实现、暂停交接与 worker 未应用补丁 | `verification/m4-database-maintenance-implementation/` |
| 文件/会话完整路径设计，未实施 | `verification/m4-next-wait-design/REPORT.md` |
| 网关单次验收计划，未执行 | `verification/m4-isolated-gateway-readonly-plan-01/PLAN.md` |
| 暂停时分支祖先、patch 与历史文件内容审计 | `verification/pause-branch-audit-20261106/BRANCHES.md` |

## 8. 分支为何多、什么时候合并 main

暂停清点时有 **57 个本地分支、25 个 worktree、8 个 GitHub 分支**。大量本地分支来自 M1–M5 各子任务的隔离开发与历史审查；不代表 57 份尚未交付功能。整合多次采用 cherry-pick，原提交不一定是当前 HEAD 的祖先，`git branch --merged` 单独不足以判断成果是否已纳入。

按祖先、patch 等价与两份历史 UI 分支的实际结果 blob 核对，24 个分支的成果已纳入 `main`；另 31 个分支的成果已纳入当前整合入口；剩余 2 个就是本页的原子预留和数据库维护提交。私有审计在 `04:04:26 UTC` 时记录为 24 / 32 / 1，随后数据库 owner 保存 `6183ec3`，该分支从整合基线组移入待整合组。此分类用于续接，不是删除授权；本次没有增加分支或工作区。

GitHub 上的阶段保留分支为 `codex/m4-controller`、`codex/m4-discovery-integration`、`codex/m4-generation-controller`、`codex/m5-community-integration`、`codex/delivery-documentation` 和 `codex/m4-bounded-waits`；当前整合入口是 `codex/responses-transport`，另有 `main`。这是交付阶段顺序，不是完整 Git 祖先链：发现到生成、生成到 M5 存在 cherry-pick 整合。暂停前 `main` 是 `83896dc` 的祖先，整合分支比它多 66 个提交；本次只追加交接文档，不推进 `main`。

**按现有路线约定，在 M4 两项退出条件通过、最终整合回归和证据核对完成后，将当前整合分支推进到 `main`。** 不需要把全部历史子分支逐一合并。若届时 `main` 仍是整合分支祖先，可 fast-forward；如远端有新提交，先正常整合并重验，不 force push。不会仅因分支数量多而提前宣布 M4 完成。

合并后再按三类整理：保留 `main` 与必要发布/回退引用；已纳入且无独立改动的历史分支可清理；未整合提交、脏工作区、常驻服务或固定验收使用的目录继续保留。cherry-pick 等价也不证明工作区内所有未提交/忽略文件都可删，须逐项检查。managed worktree 应通过应用的归档工具保留可恢复快照，再处理对应分支。本次不删除分支、不归档工作区、不改远程历史。

## 9. 给下一任务的启动说明

> 继续 DooNex。先读 docs/DEVELOPMENT_HANDOFF.md 及 AGENTS.md，检查当前 Git、两个未整合切片的最终交接和实际运行状态。从 codex/responses-transport 续接，保留已有证据、未知预留和冻结日预算。先完成已准备但未执行的固定源码网关验收，再审查整合原子预留与有限数据库维护，最后推进完整等待路径与新 UTC 日后台矩阵。按验收更新文档、提交同步；满足退出条件后推进 main。不要重做已经通过且源码未变的检查，不要把组件测试或本机社区验收写成整个 M4–M5 已完成。
