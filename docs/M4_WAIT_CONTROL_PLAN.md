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
| SDK 会话容量 | `42688c7` 正式 FileStore、构造前完整有限 inventory、故障锁存、序列化源与工具 fanout 限额、partial-write 证据、close finally | 合同和实际 SDK 故障检查见[容量记录](M4_SESSION_CAPACITY.md)；worker 尚未传入整链共同 persistence deadline |
| 真实隔离传输 | `b462502` 的普通样例和完整后台矩阵均通过，旧响应结构失败保留 | 只证明当前 Mac mini、Sol、网关和固定样例，不泛化到任意模型、容量或故障 |

未知工具结果后的继续派发已修复；摘要计算在动作完成事务外，随后 owner CAS，未观察到可靠结果的动作继续保留未知。全局容器/helper 阻止、归属核查、冻结 Skill/任务集/Review 核验和历史失败不能为降低延迟数字而省略。

## 下一纵向切片：共同期限与容器回收

先从一次 sandbox 入口贯穿挂载验证、create、执行和输出管道，以同一 monotonic deadline 计算每个阶段的剩余时间。业务停止后仍需独立、明确且有限的必要清理期限；不能在每个 Docker 子操作重新取得完整额度，或因 Run 时长已到期而跳过清理。

限制 Docker CLI stdout/stderr、容器 ID 数与解析工作量；超限和超时不能解释为没有容器。子进程需真实终止、回收并观测 EOF，不能用存活的 daemon reader、遗弃 future 或关闭可能被持锁的 stdout 伪称清理完成。最终完整零残留检查失败、lost ACK、CLI 未回收或状态不明必须保留 unknown 并阻止前台派发。

容器清理移出数据库行锁前须另行冻结 cleanup 身份和持久阻止，外部清理后按 owner/状态 CAS；不得只移动调用并省略 fence。实现优先沿用现有标准库与 Sandbox，不新增服务或通用监管框架。以真实 Docker 正常/故障检查、原账本及首次时钟保留作为切片完成条件。

## 有限文件操作与前台初始化

工作区已有 10,000 项、单文件 10 MiB、总量 100 MiB、深度 100；Skill/任务集/Review/Evidence 也已有各自容量。它们尚未贯穿整链剩余期限，若干路径还会先全输出/全排序再计数，单项超时不能代替总工作量核验。

下一步给 read/write/hash/copy/archive/patch 按块和项检查剩余期限；Git stdout/stderr 与原始树条目在过滤前计数、有限读取，保留危险配置/helper 禁用及 Git 对象类型/大小/提交绑定。只复用冻结不可变且绑定 digest 的准备结果，实际使用前继续核验。首次摘要移出长事务后仍须重锁并复核 owner、状态和基线。

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
