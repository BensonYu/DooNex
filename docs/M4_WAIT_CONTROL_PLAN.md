# M4 等待控制的后续实施顺序

更新：2026-11-14（纽约；真实收据至 UTC 2026-11-14）。**正常完整前台等待上界尚未通过**。原子 helper 预留、有限数据库维护、会话容量和 Responses 投影已整合，真实完整后台闭环、独立账目与界面核验通过，见[续接记录](M4_RESUMPTION_20261113.md)。完整后台成功与完整等待上界是独立退出条件，正式验收基线见 [M4 记录](M4_IMPLEMENTATION.md#8-验证与退出条件)。

## 已确认的约束

同步 Responses 的阶段超时不覆盖整段请求。原生异步桥接原型在连接错误下出现额外 POST，且同步准备线程晚于协程取消结束；该候选未进入产品。当前可选 isolated_direct_v1 只在 SDK 同步 Responses 边界插入一次性有限直连传输子进程，保留 SDK Agent、工具、消息转换、会话与原预算。无自动重试、无 fallback，不引入队列服务或并发执行者。

helper 的启动、传输和 IPC 使用一个绝对 monotonic deadline；所有失败路径均需有限终止和实际回收。严格 JSON、密钥匿名管道、最小环境、可信入口、永久 inode、持久阻止和显式人工核查合同见 [Responses 记录](M4_RESPONSES_TRANSPORT.md)。本机回收不能证明远端请求或计费结束。父端 SDK 准备、数据库回调、序列化、持久化和关闭仍在其外，不能称整请求或前台上界。

## 已整合的切片

| 切片 | 已验证事实 | 仍保留的边界 |
| --- | --- | --- |
| 原子 helper 预留 | `83fe6d0` 同一预算事务保存完整 reserved 身份；CAS、永久 lease inode、期限只收紧；生产 v2 索引定义核对通过 | 新动作消除 reserve/register 缺记录窗口；旧缺记录不补造身份，仍全局阻止；unknown 不返还 |
| 有限数据库维护 | `98f0885` 每 maintain 最多一个重维护单位，worker 每循环两次；enabled 授权稳定 keyset，启动 Arena 状态筛选；10 个实际索引定义核对通过 | 原 100,000 Run/Action 及各 10,000 Arena/授权/分配的查询计划是规模样本，不是完整延迟保证；claimed_at 到实际结算仍保守计时 |
| SDK 会话容量 | `42688c7` 正式 FileStore、构造前完整有限 inventory、故障锁存、序列化源与工具 fanout 限额、partial-write 证据、close finally | 合同和实际 SDK 故障检查见[容量记录](M4_SESSION_CAPACITY.md)；worker 已将原 Run 剩余 monotonic deadline 传入 SDK persistence/restore/锁与 isolated helper；其余文件/初始化和调度路径仍未贯通 |
| 真实隔离传输 | `b462502` 的普通样例和完整后台矩阵均通过，旧响应结构失败保留 | 只证明当前 Mac mini、Sol、网关和固定样例，不泛化到任意模型、容量或故障 |

未知工具结果后的继续派发已修复；摘要计算在动作完成事务外，随后 owner CAS，未观察到可靠结果的动作继续保留未知。全局容器/helper 阻止、归属核查、冻结 Skill/任务集/Review 核验和历史失败不能为降低延迟数字而省略。

## 共同期限与容器回收的当前切片

已整合有限 Git/export（`d42a8ff`）和 Sandbox（`1140aed`、`cc4d89e`）。父接线为一次 Run 首次时钟映射一个 monotonic 剩余期限，两个角色共用；恢复仍从持久首次时钟计算，不重置预算。SDK 本地持久化、恢复前核验、全局会话锁、canonical hash、shell/verification 及 isolated Responses helper 使用该期限。`f271bfb` 已在每次实际 SDK Chat/Responses 请求预算提交后重新取原 timeout、调用 timeout 和剩余期限的最小值，并以实际合成 SDK 传输验证；这只是现有传输参数，不保证整请求或供应商计费结束，也不能称完整 Run 等待上界。

Sandbox 从 mount/create 入口开始计时，控制输出和容器 ID 有限，单所有者非阻塞 drain，实际 reap 与 EOF 分开核验；必要 cleanup 共用一个独立 15 秒窗口，不因子调用而刷新。最终查询覆盖整个 Run label，不能把命令超时或无输出解释为没有容器。

create 子进程已启动却没有正常、严格的 ACK 时，CLI 回收和当前空列表仍不能证明 daemon 请求终止。`daemon_request_unknown` 持久进入 recovery.errors 和全局派发阻止；`container_snapshot_absence_confirmed` 只表示当前查询结果，不等于 `container_absence_confirmed`。rm ACK 丢失则仅在 CLI 已实际 reap/EOF、完整独立查询零残留时可核实容器清理。

已观察的 `local_process_unknown` 与 `daemon_request_unknown` 沿异常链和返回值合并，普通 pause/cancel/resume、Arena 确认、重启或后续空 ps 均不能清除。独立保留当前 cleanup attempt 与最多 16 份不同未决回执，溢出仍锁存阻止。API/Arena 新探测自身失败也持久记录，不能只抛异常然后遗失其子进程证据。

**Popen 前持久登记和生产接线已实现，完整崩溃矩阵仍待。** 独立组件 `66fed5f` 之后，Workspace、Store/fence、Sandbox、CLI actor 与父调用方已共同纳入 `a5c7ece`。两个实际 Popen 入口只接受已提交 reserve/launch 的 capability，业务全局 fence 同时包含 controller 与旧外部未知；Git/export、mount 与清理探测也须登记。API 每请求、Worker 各入口及 CLI actor 绑定实际 Run/project/nullable owner/action，退出时关闭；不能借用变更后的新 owner。

停止流程先短事务保存 cleanup intent、提交解锁，再有限清理，最后按 nonce/owner/request CAS；重复 pause 不重复 cleanup，并发 cancel 优先，pending/unknown/损坏 intent 不重放或恢复。160 项真实 PG/Git/Docker 与锁定 SDK 合成传输专项、Ruff、前端构建通过并绑定 `a5c7ece`；旧调用方回归仍有失败，新整合全量尚未通过。组件 leaf SIGKILL、复制上下文到期拒绝和提交后人工抛错的 ACK 测试不能代替完整 Worker.run 崩溃/真实网络丢 ACK 矩阵，详见[接线记录](M4_RESUMPTION_20261113.md#持久-controller-生产接线)。无原 sandbox operation 的历史未知继续阻止；空新表、空 ps 或普通 acknowledged 均不能解除，repair 仅结清新组。

API immediate control、Worker reconcile 与 execute 异常三处清理已移出 Run 行锁，并由持久 intent 和 CAS 连接前后事务。组件审查和本机故障检查不替代父整合回归或完整 Worker.run 矩阵，最终收据见[续接记录](M4_RESUMPTION_20261113.md)。

## 有限文件操作与前台初始化

工作区已有 10,000 项、单文件 10 MiB、总量 100 MiB、深度 100；Skill/任务集/Review/Evidence 也已有各自容量。`fff5761`、`f2b015c`、`0af9a0b` 与 `f271bfb` 已接入有限扫描、分块读写/hash、archive、冻结上下文和交付调用链；联合专项 390 项通过。Arena/Background 的 `bfdb43f` 组件保留审批、heldout 和环境绑定，91 项离线及 190 项数据库专项通过；其后初始化、凭据及 claim metadata 期限 `61a9dd3` 已通过 2126 项完整回归、Ruff 与前端构建。容量和局部超时仍不能代替完整工作量与调度等待验收。

上述实现按块和项检查 read/write/hash/copy/archive/patch 剩余期限；Git stdout/stderr 与原始树条目在过滤前计数、有限读取，保留危险配置/helper 禁用及 Git 对象类型/大小/提交绑定。只复用冻结不可变且绑定 digest 的准备结果，实际使用前继续核验；首次摘要移出长事务后仍重锁并复核 owner、状态和基线。后续仍须在最终源码验证近容量、超限与完整调度路径，不能从组件数推导完整上界。

锁内 Scheduler.preflight → Background.preflight → metadata `_unchanged` 的共同期限已在 `514774a` 接通，19 项组件通过；Worker 模型/工具及 verifier 的最后准入和返回后复检已在 `ac0a075` 接入。预检仍读取模型/凭据/环境文件，必要核验保留。父联合检查为 396 passed / 1 failed，并非联合通过，剩余复验见[续接记录](M4_RESUMPTION_20261113.md)。maintain 复用调用方期限与 30 秒准备窗口的较小值；Arena.stop/sync 仍有只在外部检查的区间，不能将局部接线说成完整维护上界。有限 yielded 集合及跨生命周期批次/窗口 monotonic 映射仍待，完成后再做完整 Worker.run 验收。

普通、Arena、generation 的前台初始化分别验证。健康本地 IO 下可以约束有限工作量；fsync、replace、阻塞系统调用、文件系统失联或宿主磁盘配额不能由局部 Python deadline 保证。新增整进程边界或宿主写盘配额如改变架构，先记录具体证据与决策。

## 最终实际 Worker.run 矩阵

保持现有一次性 PostgreSQL、真实 Docker、实际 Worker.run 和本地 synthetic provider 的 harness；不能以玩具调度器或直接 Worker.execute 代替。真实完整后台矩阵是另一条证据，本轮 Worker.execute 的 4.946589 秒 queue-to-running 只保留为样本。

schema 初始化 DDL 仍在 worker 全局执行锁之前；单个数据库等待限制不构成整个冷启动期限。冷启动与已就绪 worker 的等待分别测量，不跳过初始化或安全核查。

在同一宿主 monotonic 时钟记录入队提交前后、最后后台准入、在途终态/回收、结算 commit、pause flush、receipt、workspace hash、yield commit、close enter/exit、维护 slice、前台 claim commit、export/copy、SDK restore 及首次前台准入 commit。数据库 created_at 用于排序，不当成提交完成时刻。主区间为 first_admission 减 enqueue_commit_after/before，另报在途剩余和动作后开销。

| 维度 | 必须覆盖的实际路径 |
| --- | --- |
| 正常到达 | 在途 Responses loopback、真实 shell、verifier、后台准备；普通/Arena/generation 前台 |
| 容量 | 小型及接近发布 cap 的会话、branch/receipt、Git 条目/字节，warm/cold restore |
| 故障 | cap+1、特殊/变化文件、held SDK lock、慢 checkpoint、stalled Docker、lost cleanup ACK、close/DB timeout、共同期限到期 |
| 调度规模 | 有限维护的历史/yielded/enabled 授权；无前置任务主目标与有前置任务单列 |
| 重启 | 持续 worker 和 startup reconcile 分开，checkpoint/cleanup crash 后先核查，不盲重放 |

每轮同时证明前台提交后无新增后台派发、无兄弟工具越过停止、无重放、原 ledger/unknown/first clock 保留，以及容器/helper 真实终态。正常让出与故障阻止分开，watchdog 失败不能计为成功耗时。先固定容量、队列与健康依赖，再报告重复观测和适用数值操作目标，不承诺 OS/文件系统失联仍按时成功。

## 证据入口

当前整合与真实结果见[续接记录](M4_RESUMPTION_20261113.md)，当前下一步见[交接](DEVELOPMENT_HANDOFF.md)。私有 `verification/m4-next-wait-design/REPORT.md` 保存原始完整路径审计，`m4-helper-reservation-implementation/HANDOFF.md`、`m4-database-maintenance-implementation/HANDOFF.md` 保存原切片组件证据；其中“未整合”是当时事实，已由本轮整合记录更新。历史组件测试数不相加代替最终全量回归。


## 当前收尾候选（2026-11-14，未完成最终验收）

共同期限与调度候选 `e868ad3` 已接通 Worker/Scheduler/Background 的共享进程 clock、SDK store/Responses binding 动态收紧、完整有限 yielded census、Arena 内部维护期限及恢复 intent 提交后的复检。历史 396/107 项首错、2126 项全量均仍只适用原源码；当前具体失败分类和收据见[续接记录](M4_RESUMPTION_20261113.md#m4-收尾共同期限与完整调度候选2026-11-14继续中)。

候选 census 为 yielded 16、active/interrupted 恢复 64、启动活动批次 64；超限完整拒绝，包含历史 interrupted 的恢复集合不会被静默截断。原批次计划限制 400；单 Worker 的进程 clock 最多保留 4096 个 scope，满额拒绝新 scope，不通过淘汰原时钟续期。准备 deadline 不刷新；已经准入的必要清理仍有独立 15 秒回收窗口，不能将 30 秒准备参数当作冷启动绝对上界。日 allocation 汇总保持原查询合同，实际规模与健康依赖必须在最终等待报告中明确。

完整 Worker.run 正常场景、近容量和故障/重启分片继续使用实际 PG/Docker，只有模型响应为本地合成。当前正在逐项验证；测量 observer 必须在真实外层事务退出后标记 commit，SDK close 按实际 Run 归属记录。此前小样例与静态接线不关闭本计划的完整矩阵退出条件。
