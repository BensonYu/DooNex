# M4：2026-11-13 续接记录

日期按纽约时间记录，原始收据使用 UTC；本次真实完整批次发生在 **UTC 2026-11-14**。用户于 2026-11-13 明确恢复开发。本轮从 `6a0a227` 续接，真实验收源码固定到 `b462502`；其后继续离线等待控制接线，原暂停事实见[交接文档](DEVELOPMENT_HANDOFF.md)。旧失败、未知预留和冻结日预算完整保留。

## 整合与环境

只读核对时 GitHub 整合分支为 `6a0a227`、`main` 为 `a965d7f`，没有无关未提交修改。切回已有 `codex/responses-transport`，未创建新分支或工作区。原 API、worker 均未加载，产品 PostgreSQL 健康；原 plist 仍指向旧 M5 `f7a1161`，没有升级常驻服务，不能自动恢复不了解新 helper 阻止规则的旧 worker。

恢复忽略的共享 `.venv` 链接，沿用 Python 3.12.16 / SDK 1.60.1；所有应用和检查显式设置 `PYTHONPATH`，避免旧 editable 安装指向其他工作区。没有升级依赖。验收使用干净 detached `b462502` 固定目录，开发目录只在非 Markdown 输入保持不变时运行最终检查。

| 整合提交 | 实际内容 |
| --- | --- |
| `83fe6d0`，原切片 `51cd8c7` | 原子预算预留和完整 reserved helper 身份；永久 lease inode、CAS 登记、期限只收紧、显式 `actions_helper_fenced_v2` |
| `98f0885`，原切片 `6183ec3` | 每次有限后台维护、enabled 授权 keyset、精确状态查询及 10 个匹配索引 |
| `689bc2a` | worker 启动只汇总 running/cancelling Arena；提供方元数据有限校验与投影 |
| `42688c7` | SDK 正式 `file_store=` 注入、有限会话容量、恢复前核验与故障锁存 |
| `1c2e90b`、`35a1da7`、`b462502` | 中性 Responses 元数据、opaque response cache key，以及工具/推理/细分用量的严格校验后投影 |

生产 10 个调度索引和 v2 helper 索引均已用 PostgreSQL 解析后的实际定义核对，并确认 valid/ready/live，全部一致；用于比较的临时对象已回滚。第一次私有 SQL.format 与 JSON 路径花括号冲突而失败，失败收据保留，没有修改生产行。

## 隔离网关：保留失败后完成普通闭环

各普通验收均使用新样例与 O_EXCL 一次性 intent，固定源码、模型 `gpt-6.1-sol`、Responses、`isolated_direct_v1`、subscription；每 Run 为 4 calls / 100,000 tokens / 300 秒，单请求 60 秒。不重放同一样例，不把普通样例算作 P19 后台矩阵。

| 样例 / 源码 | 实际结果 |
| --- | --- |
| gateway-01 / `83896dc` | HTTP 200 后 response_schema；Run `16966d99-3300-4c44-bf85-19631b9f89a7` |
| gateway-02 / `689bc2a` | HTTP 200 后 response_fields；Run `c34b63df-48ff-49cd-95a0-e8766b98b3fe` |
| gateway-03 / `1c2e90b` | HTTP 200 后 response_metadata；Run `9cc8d100-7463-437d-8e37-1213837807f6` |
| gateway-04 / `35a1da7` | HTTP 200 后 response_schema；Run `765ef1e5-0653-43bb-93e8-7fdc02e3ebe7` |
| gateway-05 / `b462502` | **completed_verified，3 calls / 2,842 tokens，0 unknown / reserved tokens**；Run `676ddf89-a239-4ae5-9a55-01adf56dea0d` |

前四轮各保留 1 次未知调用和 6,158 reserved tokens，均有实际 helper 回收记录；HTTP 200、结构修正和本机回收都不用于清账。第五轮脚本退出 0、19.50 秒，3 个 helper 均实际 reaped，原脏工作区哨兵不变，最终固定快照摘要为 `ab0de06d3ba22bc0200b8a92b0f33e8107fcb08694fa3075f0cfba0a7ec0afd6`。逐旧行后验确认原数据库、campaign 前缀、配置、Arena 和日账未改。

另有三个 direct 诊断样例，均计入原 campaign 并通过固定快照检查：分别 3 calls / 2,832、2,864、2,846 tokens。第三轮用私有 wrapper 保存有限、凭据脱敏的原始与 pinned 响应；后续对 6 份响应离线复现，不再通过真实调用猜测字段。原始诊断材料只保存在私有目录，不进入产品 Git 或生产普通日志。

## Responses 合同

锁定 LiteLLM 的 ResponsesAPIResponse 不含部分已验证提供方字段。现在先核验有限元数据和中性配置，再投影回原严格 SDK/IPC 子集；未知字段、非法工具、执行核心、重复/不完整 call、布尔/数值强制转换仍拒绝，不接受任意额外字段。

- response cache key 是最多 256 bytes 的 opaque string；出站 SDK 自有 key 仍要求 UUID。
- 默认 access_programs、disabled image compression、空 input transformations、null moderation、零采样惩罚有明确合同；内置 image/web 用量只允许严格整数零，非零或未知工具拒绝。
- 工具 output_schema/allowed_callers/defer_loading 只允许 null；reasoning context/mode 只允许已核对的中性值或 null，校验后移除。
- cache_write_tokens 只允许 0/null；attribution 限定 item/request_fields 结构、整数及 aggregate 上界，校验后移除。input/output/total、缓存和推理统计不改写。
- response_fields、response_metadata 和原 response_schema 分别记录有限失败类别，不输出任意提供方字段名或正文；无重试、无 fallback、无未知预留返还。

官方 Responses 对象页为 `https://developers.openai.com/api/reference/resources/responses/methods/create`，本机抓取 HTTP 200 收据保留；官方说明不能替代本私网网关和具体模型的实测。

## SDK 会话容量

[容量合同与专项证据](M4_SESSION_CAPACITY.md)规定 base/单事件各 16 MiB、总量 64 MiB、临时写峰值 80 MiB、最多 1024 个事件和 2048 项，并在解析/序列化/branch 整体分配前限制源形状。正式 FileStore 无缓存，descriptor/no-follow、单链接普通文件和稳定 inode/stat 复核；SDK 吞掉 list/marker 异常也会因故障锁存而拒绝覆盖、后续准入及安全回执。partial write 保留证据；close 异常仍清除全部回调登记。

近容量真实 SDK EventLog 样本为 1024 events / 1027 entries / 63,362,935 bytes；追加约 1.02 秒，冷 inventory 与 SDK 读取约 5.09 秒，canonical 哈希约 0.11 秒。第 1025 事件拒绝且旧事件摘要不变。该样本不是 Worker.run 延迟验收。后续父接线已将原 Run 的剩余 monotonic deadline 传入 SDK 本地持久化、恢复读取、锁和哈希；其他初始化与调度路径尚未贯通。

## UTC 2026-11-14 完整后台批次

授权 `e0e5646d-8dc8-45d1-b1b9-826b48717565`，批次 `c0b5c1ec-ea81-48b0-9f8d-0482ba0f99f1`；前台 Run `ed83ada1-79e3-4e6b-b6b3-7c7e8519d4b8`。固定原 tuning 任务集、原两份批准候选与每组每任务 1 次重复，形成 **5 tasks × 3 arms = 15 个后台 Run**。新私有配置档 `isolated-m4-20261114` 冻结 Sol/Responses/isolated_direct_v1/subscription/60 秒；原配置档逻辑值及版本保持。

上限保持原方案：每 Run 6 calls / 120,000 tokens / 300 秒，批次 90 / 2,000,000 / 1800 秒，UTC 日 160 / 4,000,000 / 7200 秒；campaign 一次预留后台加前台的 96 calls / 2,120,000 tokens。新真实 UTC 日未改动 11 月 6、7 日冻结预算或任何未知记录。

准备时两次私有脚本分别遗漏 pause 的 project context、误判 pause 会清 requested_action，均失败并保留日志。核对未发生模型调用和重复创建后，仅在原 campaign/worker 锁下清除本轮独有、从未执行且无 action 的前台 fixture stop flag，追加审计事件；没有清理历史停止请求。最终只有一个授权/前台 pair。执行先持久一次性 intent，再启用原 draft，未重试。

实际脚本 **exit 0，373.89 秒，外层 watchdog 未触发**：

| 项目 | 实际结果 |
| --- | --- |
| 后台 | **15/15 completed_verified，45 calls / 56,689 tokens，unknown 0 / reserved tokens 0** |
| 前台 | **completed_verified，3 calls / 3,219 tokens，unknown 0 / reserved tokens 0** |
| 本机 helper | **48 个均实际 reaped**，无 pending、未决 helper 或任务容器 |
| 日账 | 分配 settled，已用 **45 calls / 56,689 tokens / 373 秒**，仍预留为零；费用 null |
| campaign | 25 条尝试；reserved_calls **615**，reserved_tokens **8,807,060**，原 24 条前缀逐项不变 |

独立只读审计 **259 项通过**：25 表所有准备前旧行逐项不变；新增恰为 16 Runs、唯一授权/批次/分配/日账；逐 action→角色→Run→批次→campaign→日账复算一致，原 profile、凭据、service/plist 保持。首轮审计使用默认 legacy 档，与准备时 override 的 legacy 投影不同而失败；改为同一明确配置入口后独立审计通过，首轮导出与失败日志保留。

首个后台模型预留提交后确定性入队前台；后台仅该在途调用完成后保存安全回执，前台先完成，再沿原 Run、session UUID、generation、待处理工具身份续接。原首次启动时钟保持，待处理工具仅执行一次。脚本直接驱动 Worker.execute；queue-to-running **4.946589 秒**仍是该 harness 的样本，未覆盖 Worker.run 的完整维护及前台首次准入，不能作为发布等待上界。

浏览器核验 **32 项、32 次实际 GET、7 张截图复核通过**，无响应投影；桌面 1440×1000 验证授权、日账、三组各 5/5、全部 15 次尝试、原 Run 让出/续接和前后台检查退出 0。单张尝试列表截图只覆盖部分条目，DOM 断言覆盖全部 15 项；没有移动端验收。首轮 bundled Playwright 缺少配套 executable 而失败，使用已安装 Chrome 后通过，失败保留。临时 API 禁用 lifespan、数据库只读并拒绝非 GET；关闭后确认浏览器前后 25 表、campaign 及全部配置完全相同，launchd 服务仍未加载。完整后台闭环因此通过；完整前台等待上界仍是独立未通过条件。

## 本机验证与证据

旧候选 `689bc2a` 的全量为 1747 passed / 3 warnings / 892.72 秒；会话及 Responses 联合最终专项为 526 passed / 2 warnings / 46.59 秒；`b462502` Responses 专项为 222 passed / 1 warning / 20.77 秒。各组件结果不相加代替最终整合数。Ruff 通过。最终在 `b462502` 运行 `PYTHONPATH=apps/server:tests .venv/bin/python scripts/test_local.py`：**1826 passed、3 warnings、905.09 秒、exit 0，无跳过**；外层总时长 908.61 秒。138 份非 Markdown 输入前后相同并与该提交逐项绑定；一次性 PostgreSQL 和任务容器已清理。完整日志 SHA-256 为 `df2bd61a983c8a8ad6eea40b33a953594adf6b77d7513145b7e198b220e80ced`。Ruff 全范围及本轮实际前端构建均 exit 0，三份收据输入一致。

私有证据根为 `~/Library/Application Support/DooNex/verification/resume-20261113/`，包括原始失败、各 gateway 前后快照/intent/收据、official-docs-fetch、metadata/combined/full 检查、index-audit、session-capacity-benchmark 和 `background-20261114/`。原始响应、源码、日志、配置和运行工件不得提交产品仓库。

## 下一步与合并条件

UTC 2026-11-14 的后续离线切片已建立共同 monotonic deadline 检查基础（`3e88a96`，7 项专项通过），并接入已观察到的本机控制进程未知回执：不再被重启的空容器核查、普通暂停/取消/续接或 Arena 确认覆盖；后台自动续接的 Docker 探测失败也持久阻止。11 项专门检查使用实际 Worker.run、一次性 PostgreSQL 和合成故障，通过且源码输入前后相同；没有真实模型调用。私有 `cleanup-fence-03.json/.log` 保留 7.73 秒、exit 0 和日志 SHA `3a8c6251e26663cb7e1d924b127a86081d6f5e08fc5cf81460b6384792ae1139`。早一轮组合检查 53 项通过；第一轮在检查期间源码变化，`inputs_unchanged=false`，不能充当最终绑定。新增代码尚待后续整合全量，不能沿用 b462502 的 1826 项作为当前源码通过数。

有限 Git/export（原 `71c334f` → `d42a8ff`）、Sandbox（原 `d8a3d49` / `b8db9ee` → `1140aed` / `cc4d89e`）现已整合；依赖的 `3e88a96` 没有重复引入。Git 在过滤前最多 20,000 原始条目/16 MiB、路径 4096 bytes/深度 100，再保留 10,000 文件/单 10 MiB/总 100 MiB；导出统一 30 秒局部上限并受外层剩余期限收紧。组件近容量真实导出 104,850,000 bytes 约 2.596 秒，仅为组件样本。

Sandbox 的 mount/create/attach/inspect 使用同一业务期限，控制输出、严格完整 ID 和查询有限；必要 cleanup 共用独立 15 秒，单所有者非阻塞读，实际 reap/EOF 分开核验。create ACK 缺失锁存 daemon 请求未知，当前空 ps 不能代替 daemon 终态。父接线将 daemon-only 与 process-only 未知都持久进入原 global fence；普通恢复和重启不能解除。API/Arena 新探测故障自身也保存，不遗失其控制进程证据。

一次 Run 的剩余 monotonic 期限现已传至 Workspace 初始化/scan/checkpoint、SDK persistence/恢复前读取/锁/canonical hash、shell/verifier 与 isolated Responses helper；两个角色共用，恢复仍受原首次持久时钟限制。直接 SDK 模型请求尚未逐次钳制在途 timeout；Skill/Tasksets/Generation、Review/Evidence 和调度 receipt 的接线仍在独立实施。上述能力只覆盖健康本地操作之间的协作检查，不保证阻塞内核/文件系统调用或远端计费终止。

父接线应用提交 `43f6147` 的联合专项实际执行：

```bash
PYTHONPATH=apps/server:tests .venv/bin/python scripts/test_local.py \
  tests/test_arena_recovery.py tests/test_cleanup_fences.py \
  tests/test_execution_deadline.py tests/test_worker_wait_paths.py \
  tests/test_responses_reservation.py tests/test_responses_worker.py \
  tests/test_session_store.py tests/test_sdk.py tests/test_sdk_scheduling.py \
  tests/test_sdk_unknown_tools.py tests/test_sandbox_deadline.py tests/test_workspace.py
```

**334 passed / 3 warnings / 128.62 秒 / exit 0，无跳过**；外层 132.08 秒。`deadline-integration-03.json/.log` 的输入前后相同，提交绑定收据逐项核对全部非 Markdown 输入与 `43f6147` 一致，日志 SHA `05162d286270dd2d72d99d7b181da2967bb376300a4821f96752a2e6e0be6975`。Ruff 全范围 exit 0。另一次 89 项恢复专项通过；不把不同组件通过数相加当全量。

失败收据保留：`deadline-integration-01` 为 321 passed / 1 failed，原 stop_containers 测试替身缺新 keyword；只适配参数。`full-03` 在定位到旧 verifier 0.1 秒只计执行阶段的假设后中止，**220 passed / 1 failed / exit 2，不是全量通过**。夹具现给予包含真实 mount/create 的 5 秒，并新增日志哨兵证明检查命令确实进入，然后检查超时、历史失败与恢复仍保留；原断言未放宽。最终整合全量仍须在后续内容/证据切片接入后执行，旧 1826 项不适用于新源码。

本片仍只保证已观察回执不丢失；控制器在 Popen 后、回执持久前崩溃的窗口尚未覆盖。没有升级服务、调用真实模型或改动生产账本。具体剩余合同见[等待计划](M4_WAIT_CONTROL_PLAN.md)。

继续按[等待控制计划](M4_WAIT_CONTROL_PLAN.md)实施共同剩余期限、Docker 管道与清理、有限 Git/export/快照/检查复制和普通/Arena/generation 前台初始化，再对同一 monotonic 时钟的实际 Worker.run 做正常、近容量及故障矩阵。文件系统失联、阻塞系统调用、远端请求结束和供应商最终计费不由现有局部限制证明。

`main` 仍保留 M3；M4 完整退出条件及最终整合回归/证据核对通过后才推进。M5 社区仍关闭，未公开上线、收费或选择许可证。旧 API/worker 不自动恢复；服务升级须绑定已验证固定源码和回退收据，不指向开发目录。

## 内容、交付与初始化期限的后续整合

以下为真实后台验收之后的本机实现与检查，没有新增真实模型调用或操作生产账本。

| 提交 | 已整合内容 | 实际检查与边界 |
| --- | --- | --- |
| `fff5761`、`f2b015c`、`0af9a0b` | Tasksets、Evidence/Review、Skill/Library/Generation 的有限扫描、分块读写/hash、archive 与冻结内容期限 | 保留不可变版本、审批、heldout 隔离及执行控制异常；组件证据只适用其冻结源码 |
| `f271bfb` | Worker 内容与交付调用链、Scheduler receipt 和容器探测共用原执行期限；SDK 每次实际请求在预留提交后重算 timeout | 390 passed / 3 warnings / 169.56 秒 / exit 0，无跳过；146 个输入逐项绑定提交。传输 timeout 不保证整请求或远端计费结束 |
| `66fed5f`，原 `ced2240` | 独立 controller operation 登记组件、窄表/严格 SQL 终态/索引核对、已提交 launch capability 与实际回执 | 82 passed（36 离线、46 真实 PostgreSQL）/ 8.01 秒 / exit 0；**尚未接入生产 Store、全局 fence 或 Popen 入口** |
| `bfdb43f`，原 `24eb981` | Arena/Background 冻结准备、环境摘要与原子建批的 keyword-only deadline | 91 项离线及 190 项真实 PG/Docker 联合检查通过；standalone detail 的整链仍不能宣称有等待上界 |

`deadline-delivery-integration-01.json/.log` 与 source-binding 收据绑定 `f271bfb`，日志 SHA256 为 `b81baa56fd81540b6c9d6013d604ca8eca6f487fd799b93453f12b51709e8fc0`。对应命令实际包含 execution/content/generation、Evidence/Review、Scheduler、Worker wait、SDK、session store 与 cleanup fences 联合专项；不把组件数相加作为全量。登记组件的首两轮 fixture 失败和上下文组件的旧候选收据均保留，最终以各自 HANDOFF 指定的收据为准。

独立审查发现首次时钟之前的 Library/Generation/Arena 构造与凭据读取，以及最终 completion 行锁等待之后缺少期限复核。后续候选已将初始化移入首次时钟提交后的 try 和同一原期限，并在 completion 锁后重新核对 monotonic 与持久 wall 时长。私有凭据文件改为 no-follow/nonblocking 打开、普通文件/权限检查、1 MiB 总量、64 KiB 分块和 finally 关闭；错误不输出凭据正文。SDK 失败诊断记录实际请求 timeout。`deadline-initialization-integration-01` 已实际通过 190 项 / 2 warnings / 112.07 秒 / exit 0，151 个输入不变；该收据仍是此候选的中途专项。

随后又补齐 Arena/Background 两处 credential 的期限，以及 Scheduler.claim_next → Background.claim → metadata `_unchanged` 的同一期限；到期回滚授权消费、分配与批次，原输入、日账和预算不改。准备时钟最多 30 秒，只约束健康本地操作之间的协作检查，不替代 Run/批次/UTC 日预算。增加 metadata 复核中及 allocation 写入后到期的真实数据库测试。`deadline-initialization-offline-04` 47 passed / 6 deselected / 2 warnings / exit 0；这是显式排除 postgres 的专项，不是全量。首次完整回归 `deadline-initialization-full-01.json/.log` 实际为 **7 failed / 2117 passed / 3 warnings / exit 1**，pytest 821.17 秒，外层 824.922 秒，151 份输入不变；日志 SHA256 为 `9c9c2e54cb7c44bed684bf97d6a4dbe2a67c264ca84ff38e994c2f3e42fb88fd`。失败均涉及旧测试替身未接收新增 keyword-only deadline；后续只在三个测试文件补齐 `**kwargs` 接收/向真实实现透传，未放宽断言。修复后 `deadline-initialization-integration-02` 实际通过 **160 项 / 2 warnings / 159.31 秒 / exit 0**，151 个输入不变，独立审查核对日志与源码；其日志 SHA256 为 `078391998c27031c2bff4c5ca5957889d83e9194cfff31e50be7f23c1b21a19d`。第二轮完整 `deadline-initialization-full-02` 实际为 **1 failed / 2123 passed / 3 warnings / 835.01 秒 / exit 1**，外层 838.461 秒，151 份输入不变，日志 SHA256 为 `477fcd5bcaa8c8290670e44e0b7cb1e4c196c1f53009f8e1b1bfb83b2eddf07c`。失败在 evidence 期限测试，原日志未保存底层 reason，不能据此推断应用故障原因。同期实际前端构建 `deadline-initialization-web-01` 已 exit 0，但三份测试变化后的最终检查仍待重跑。

此前 `deadline-initialization-offline-01` 的 41 passed / 1 failed 保留：锁定 SDK 将 ModelCallError 包装成 ConversationRunError；仅修测试沿 cause 核对实际 ModelCallError、timeout 和未知预留，未改 SDK 失败语义。后续离线专项通过仍不替代最终全量。

本轮测试运行期间另有权限 PRD 文档合并 `e620fcd` 进入现有分支，涉及 README/PRD/架构/路线图/评测规范，没有非 Markdown 源码变更；其 P27–P32 仍是待实施设计，按路线在 M4 之后推进。本轮继续 M4，不启用新权限或审批能力。

下一项生产接线已拆为 Workspace 与 Sandbox 两个独立实现工作区，父负责 Store/actor/callers 与锁外 cleanup。普通新组不能排除自身历史 unknown；有原 sandbox operation 时只允许固定 ps/rm repair，且不解除原未知；没有可核验原身份的历史记录继续阻止。子片通过、空新表或空 ps 都不构成部署前的历史安全证明。完整 Worker.run 矩阵、有限 yielded 扫描及批次/窗口 monotonic 接线仍未完成。


期限测试的独立复现与修正：`deadline-initialization-diagnostic-01` 未进入 pytest，临时 PostgreSQL 容器内部就绪但宿主映射端口超时，脚本 exit 1 并清理；重试 `diagnostic-02` 原单测 1 passed / 3.20 秒 / exit 0。随后在 SDK 返回后确定性模拟持久 wall 时钟前移 5 秒，`diagnostic-03` 为 1 failed / 3 passed / 15 deselected / exit 1，reason 明确为 actual deadline `438952.873931625` 不等于 SDK 初始 `438957.8739235`。原合同允许 `remaining_execution` 只收紧期限，数值恒等不是其合同；现测试要求期限仍未到期、不得超过 SDK 初始期限，前移分支还要求至少收紧 4 秒，并继续以真实 Delivery 的已过期期限验证 budget_exceeded、零新交付工件、原调用和无未知预留。没有改应用期限、预算或失败状态来取得通过；所有失败收据保留。修正后的联合专项和完整回归结果见下表，历史失败与各自输入收据保留。


### 初始化期限最终源码绑定

实现提交为 `61a9dd3730cdc6523bcbee2af2ed286153e078c5`。下列四份检查的全部 151 个非 Markdown 输入前后相同，且已逐项核对实际提交 blob 与当前文件，不以相同 HEAD 或组件数量推定一致。

| 私有收据 | 实际结果 | 日志 SHA256 |
| --- | --- | --- |
| `deadline-initialization-integration-03` | 162 passed / 2 warnings / 162.32 秒 / exit 0 | `2f77e5314e573d72da300888c10278ce041585df4fec91f3dc5437ebe72059a6` |
| `deadline-initialization-full-03` | 2126 passed / 3 warnings / 828.30 秒 / exit 0，无跳过；外层 832.395 秒 | `26ace73053641fc5db8a8d1385ee16453d0e51a5aaecb8769dda72f67d8b6606` |
| `deadline-initialization-ruff-final-02` | 全范围 Ruff / exit 0 | 见原收据 |
| `deadline-initialization-web-final-02` | 实际前端构建 / exit 0 | 见原收据 |

绑定收据为 `deadline-initialization-final-source-binding.json`，SHA256 `305318a3cff18a066352c904cc8b0fbb2705de5c8303830065a10934326556b6`。临时数据库已由脚本清理；本轮没有真实模型调用、生产账本写入或服务升级。两轮完整回归失败、临时数据库连接失败及确定性诊断均保留，不能将它们改写为通过。

上述为 `61a9dd3` 当时的最终绑定。之后持久 controller 生产接线已整合，当前状态及未完成条件见下节；不能继续按旧的“草稿未应用”状态实施，也不能将 2126 项历史全量套用新源码。

## 持久 controller 生产接线

| 父提交 | 原切片 | 已纳入内容 |
| --- | --- | --- |
| `4d68f30` | `28a19e8` | Workspace 持久 controller group，Git config/export 及临时 Git 的显式 capability |
| `e4b0834` | `6a011106` | Store 初始化、严格 SQL 终态与索引、业务/登记两层 fence、实际 project/owner/action 绑定 |
| `3a910c5` | `8d406bb` | Sandbox 两个实际进程入口的显式 capability，覆盖 mount、create/start/inspect、rm 与完整 ps |
| `93bd7e3`、`18fd8ed` | `5cfdb23`、`5070c01` | Workspace/Sandbox 原检查迁移至真实持久 actor，保留隔离及故障断言 |
| `cb4416c` | `77a2418` | 有限 CLI actor 验证原连接实际持有 worker 锁；旧 background harness 在读取配置/DB/campaign 前明确禁用 |
| `c23a6bf` | `fa88055` | Git launch/终态异常保留实际 operation ID，普通已终态业务错误不一律伪造 record_unknown |
| `a5c7ece` | 父整合 | API/Worker/cleanup/Evidence/Review/Arena/Scheduler 生产调用方与专项 |
| `6b4a404` | `3bbc114` | Git filters 测试迁移；26 项组件检查通过，原 25 passed / 1 failed 保留 |

Store 在初始化时安装窄 controller 表、严格终态函数和索引。业务 `execution_fenced` 同时含旧外部未知与 controller 未决；只有 registry 内部续接使用 external fence，不为业务提供 exclude。registry 严格读取真实 Run/project/owner/action，owner 为 null 合法但不能省略身份检查。实际进程启动前必须确认 reserve/launch 已提交，回收分别核对实际 reap 与双 EOF；repair 只结清新组，不清旧 unknown。

API 每请求使用 fresh registry/ContextVar，finally 关闭；保留原 auth/Host/Origin。Worker 的 run/reconcile/execute 入口分别建立并关闭本层 registry、恢复外层，lease 实际核对当前连接持有 WORKER_LOCK。停止链先用短事务提交 intent、解锁后执行有限 cleanup，再按 nonce/owner/request 摘要 CAS；重复 pause 不重复 cleanup，并发 cancel 优先，旧 CAS 不覆盖变更后的状态。pending/unknown/损坏 intent 不重放、不恢复。Evidence/Review 临时 Git、verifier、Arena resume 和 Scheduler receipt 使用实际 workspace/verification 身份。

实际专项：

```bash
PYTHONPATH=apps/server:tests .venv/bin/python scripts/test_local.py \
  tests/test_controller_cleanup.py tests/test_controller_entrypoints.py \
  tests/test_controller_store.py tests/test_workspace_controller.py \
  tests/test_sandbox_controller.py tests/test_live_controller.py
```

首次 `controller-entrypoints-integration-01` 为 **158 passed / 2 failed / 2 warnings / 34.92 秒 / exit 1**，日志 SHA256 `7b1fe9a78cce81ae0d5fa154d46c633f61bb4f56be32666838cb1d3b1b5c32b9`。新 stale-CAS 测试直接索引未领取 Run 可能缺失的 owner；原日志未直出被 cleanup 捕获的 KeyError，根因来自静态 setup 核对。仅将 snapshot/比较改为 `.get(key)`，保留 owner/request/intent CAS、预算、首次时钟和单终态断言，未修改产品语义。

修正后的 `controller-entrypoints-integration-02` 为 **160 passed / 2 warnings / 35.37 秒 / exit 0**，外层 38.583 秒；同一 159 个非 Markdown 输入的 Ruff 与实际前端构建 exit 0。`controller-wiring-source-binding-01.json` 逐项绑定 `a5c7ece102626c8c38cba20cdd5f42ef59876a22`，SHA256 `77bcec272c7c606109ae24f12a416bbc30969e0d0da1e98b63895ecc7506a4d8`；专项日志 SHA256 `9c901fe29838b2e8279c1cfe91231fedc40bc855e1486210a73e402dbb11fc48`。独立审查另保存全部 159 份实际测试输入。后续 `6b4a404` 仅迁移 Git filters 测试，不能再说当前全部输入与专项相同。

专项包括真实 API→Run→Workspace→SDK shell→Evidence 纵向；每次实际 Popen 前以第二 PG 连接取得 advisory xact lock 和 Run NOWAIT 行锁，确认 launching 已提交。三处 cleanup、重复 pause/并发 cancel、owner/request/intent 变化和两个并发 API threadpool ContextVar 已检查；已真实提交终态后人工抛 RegistryError 的 ACK 未知保留，普通恢复不清除。

这些证据仍有边界：API 过期 copy_context 的 late Git 在测试线程执行，Worker closed context 直接检查，没有完整 late OS thread 矩阵；ACK 测试为真实提交后人工抛错，不是真实网络 ACK 丢失；leaf SIGKILL 不等于完整 Worker.run crash 矩阵。**未通过新整合完整回归，也未证明完整前台等待上界。**

旧入口首轮 `controller-existing-integration-01` 在 `6b4a404` 实际为 **76 passed / 9 failed / 15 setup errors / 2 warnings / 85.98 秒 / exit 1**，外层 89.699 秒，输入前后不变；日志 SHA256 `2048ceeae52804ebddb8a65a25708f4ec6e3efd5b5499e872c5883f3943b29b5`。15 个 setup 在共享 Arena fixture 导入 taskset 时缺 registry，其余 Worker/cleanup 行为正在逐项核对，不先认定全部为测试问题。测试进程结束且一次性 PG 已清理，原失败保留。下一步迁移真实调用上下文、处理回归并完成新整合全量，再继续调度 preflight 共同期限、有限 yielded、批次/窗口 monotonic 映射及实际 Worker.run 矩阵；不重放已通过的真实后台批次，不推进 main 或升级服务。

### 旧调用方迁移与清理证据修复

后续独立切片已纳入：Evidence/Review 三文件迁移 `ee03d8b`（原 `4a09a2a`，58 项真实 PG/Docker 检查通过）、M3 隔离 `1dcfe9e`（原 `604f023`，28 项通过）和 Arena fixture `ea3b443`（原 `28dd900`，17 项通过）。它们使用真实 Store/registry/Run/project/nullable owner/action；共享 prepare 改为显式 contextmanager，fixture 生命周期内 yield，Worker 自有 actor 结束后恢复外层，退出后关闭。不增加 autouse、fake registry 或生产旁路。隔离片首轮 19 passed / 9 failed 保留：旧测试一概禁止 Docker，连 mount 拒绝后的精确清理 ps 也阻断；现只允许当前 Run 完整参数的真实 ps，容器创建仍禁止，原隔离、只读、篡改与 10 秒断言保留。

任务集导入测试迁移已纳入 `b794e70`（原 `bc4b430`），Tasksets/Skills 组件实际 **129 passed / 89.67 秒 / exit 0**，10 份输入与原提交逐项一致；共享 taskset_source/registry fixture 不变，Skills 无需修改。原 37 个断言及顺序保留，新增实际 actor、Run/project/nullable owner 绑定检查。组件通过不代替父整合回归，首次 collection 失败仍保留。

独立审查确认旧入口失败包含真实产品缺陷：merge_cleanup 二次合并时丢失历史 cleanup_attempt_evidence，并可能把 aggregate 的历史 unknown 回执冒充本次清理；result 已有的回执库存/overflow 也可能被缩减。当前修复区分 raw attempt 与 aggregate、保留双方有限库存/overflow，无新尝试时保留上一份实际 attempt，新 raw attempt 则更新该字段。已有 global fence 时继续零普通 stop/probe；execution_fenced 和 remaining unknown 是保守状态变化，原全字典 equality 改为精确未知标记/回执/overflow/attempt、零调用及原 usage/首次时钟断言。新定点检查覆盖 process/daemon/record-only 二次合并、多个回执及 overflow、新回执覆盖与 deepcopy；provider 测试仅将原 body 放入 prepare scope，20 个原断言 AST 相同。

`controller-cleanup-provenance-offline-02` 实际 **15 passed / 14 deselected / 1 warning / 2.36 秒 / exit 0**，显式排除 PostgreSQL，不是整合通过。随后 `controller-recovery-integration-01` 联合六个 controller 模块、旧 API/Worker/cleanup、期限及 provider 分类，实际 **284 passed / 4 failed / 2 warnings / 256.23 秒 / exit 1**，外层 259.699 秒、159 个输入前后不变；日志 SHA256 `78d04ecec1660c87ceb62f856483e02061b4b4a8dd67667fd25eed592ea9515b`。一次性 PG 与测试进程已清理，失败记录保留。

四项失败另揭示恢复路径问题：普通 API resume 在检查已有控制阻止前调用 recovery，尝试新的 Docker probe；测试的禁止探测哨兵由 AnyIO 包装成退出时 CancelledError，该根因来自源码与调用链核对，未单独证明 lifespan 缺陷。Arena 已通过扩大后的全局 fence 拒绝，却仍固定提示模型传输，导致两项控制进程断言失败。当前候选将 resume 的原始状态读取、本地及全局阻止检查提前至 Review/Git 和容器核查之前，保留最终锁内复检；全局消息改为控制进程、Docker 请求或模型传输的准确通称。新增其他 Run 阻止下 Review/probe 零尝试、两 Run 完全不变检查，原禁止探测及未知预留断言保留。最终联合复验尚待。没有调用真实模型、改生产账本或启用常驻服务。

### 调度预检共同期限与最终准入

后台/调度测试迁移 `f1cdeb5`（原 `f575780`）的独立组件为 **66 passed / 2 warnings / 225.81 秒 / exit 0**，44 个输入与原提交逐项一致；依赖共享 fixture 的五个测试模块在父候选中显式导出同一真实 registry，原断言保留。之后预检期限片 `514774a`（原 `8a8a456`）已整合：Scheduler.preflight → Background.preflight → metadata `_unchanged` 传同一可选 absolute deadline，在锁、文件读取、allocation/day 核查及最后返回后检查。maintain 每次最多一个候选、一次取调用方期限与 30 秒准备窗口的最小值，Arena 初始化使用该值；结算返回后在原事务内超期则回滚，不释放预留。19 项真实 PostgreSQL 组件通过（65.09 秒 / exit 0），45 个输入与原提交绑定。

父候选将这个 deadline 传给 Worker 的 Ledger model/tool 准入及 GuardedVerifier，并在 preflight 返回后再次检查，再写新 action。新增三个真实 Worker.run/PG/SDK 合成传输检查分别覆盖 model、shell 和 verification 返回后到期，核对实际 action、原已结算用量、未知预留、首次时钟与 SDK 关闭。首轮 `controller-preflight-integration-01` 实际 **1 failed / 13 passed / 2 warnings / 6.64 秒 / exit 1**（外层 10.129 秒），日志 SHA256 `20d52982176ced5d65603955ac93e0a1086289eed3cfc01cbdccecc4f06ce008`：新测试的模拟时钟持续影响 execute 结束后的 maintain。仅将模拟 clock 限于真实 Worker.execute 作用域，保留触发和账本断言，没有修改产品来通过；失败收据保留。

`controller-preflight-integration-02` 已结束：**396 passed / 1 failed / 2 warnings / 599.04 秒 / exit 1**（外层 602.687 秒），160 个非 Markdown 输入前后相同，日志 SHA256 `7ab52924e15059203f0d0f367583ff586563330e9a193d97057d92c3428d6c16`。三个新 Worker 准入到期检查及恢复/证据修复已在本轮实际执行；整轮在 `-x` 首次失败处停止，后续用例未运行，不能记为联合通过。pytest 和一次性 PG 已退出并确认清理。

失败用例人工写入没有原 controller operation 身份的历史 `recovery.remaining/errors`，再要求普通 Arena resume 根据空容器列表清除阻止。此行为违反既有历史未知合同，不能通过产品旁路恢复。现保留真实 Worker 中断、原未知调用/预留、日账和首次时钟，改验收为两种确认值均在新探测之前拒绝；完整 Run、批次、日账、动作、controller 行与事件不变，不确认或返还未知用量。迁移后的实际行为结果见下节；Ruff 已通过，不能用静态检查冒充行为通过。

### 持续目标清除后的最后收尾

用户随后清除了持续目标；父与三个子任务均停止新增开发/测试，未继续实施有限 yielded cap 或 clock core。父实现已保存为 `ac0a075665020ceabe9cd34e2d155b4c369f24b9`，正常推送后用 `git ls-remote` 精确核对；`main` 仍是 `a965d7f`。没有真实模型调用、生产账本写入、服务启动或升级。

清除目标前已启动的 `controller-scheduling-tail-01` 只做收尾，结果为 **107 passed / 1 failed / 2 warnings / 202.16 秒 / exit 1**（外层 205.217 秒），160 个非 Markdown 输入前后相同，HEAD 为 `ac0a075`，日志 SHA256 `cf38b8be5592a43a9dcd6e2f40ff5da620dda90436b8548909c2ec3f13a7c94a`。它实际运行 `test_scheduling_limits.py`、`test_scheduling_maintenance.py`、`test_scheduling_latency.py`、`test_context_deadline_pg.py` 和 `test_live_background.py`，使用 `-x`；上述历史未知拒绝恢复用例在这轮已通过，整轮仍未通过，后续用例没有运行。

唯一失败是 `test_private_historical_fixture_with_postgres_docker_and_synthetic_model`：其 test-owned `fixture_inventory` 执行全局 Run-label Docker 清点，返回 `existing_task_container`，历史私有 fixture 拒绝运行，`main` 按原合同 exit 1。日志只证明当时库存非空，尚未确认容器原操作归属或产生者，不能先认定产品故障、无关残留或放宽检查。不要重启公开 `live_background.py`（正式入口仍明确禁用），也不能为通过测试宽扫删除容器。pytest、wrapper 与一次性 PG 已结束并确认清理；后验只读清点见到一个 Exited (0) 的 Run-label 容器，没有正在执行的任务容器，未作删除。脱敏字段清点保存在私有 `controller-scheduling-tail-01-container-inventory.json`；原操作归属核查留给下次恢复。

本次全范围 Ruff `controller-preflight-ruff-03` exit 0；不把它或上述 107 项与之前 396 项相加作为当前完整回归。Community 的 36 项与 Generation 的 113 项独立组件通过且原提交绑定，尚未整合；yielded candidate003 与 Worker SIGKILL 两模块只有静态证据，未运行行为验收。精确工作区、提交和收据见[交接恢复点](DEVELOPMENT_HANDOFF.md#2026-11-14-持续目标清除后的恢复点)。

上述检查是健康本地操作之间的协作期限；Arena.stop/settle 内部仍有只在前后检查的区间，数据库 commit、阻塞系统调用不能由局部检查中断。有限 yielded 集合、跨 execute/角色/批次的进程内 monotonic 不续期映射和完整 Worker.run 等待矩阵仍待，不能称完整维护或前台等待已通过。


## M4 收尾：共同期限与完整调度候选（2026-11-14，继续中）

当前应用候选为 `e868ad3`，父分支仍 `codex/responses-transport`。已纳入 Community/Generation/Worker SIGKILL 的生产 actor fixture 迁移、yielded 同一期限、Arena 有限维护、ExecutionClock 和 Background 时钟接线；不改模型、凭据、单 Run/批次/日额度或历史未知。本节不是最终退出结论，完整 Worker.run 等待矩阵和最终全量仍待。

最新进度：隔离 Responses 基础 15 场景和故障矩阵的全部场景已有分轮实际通过证据，已纳入父整合；原 60 秒 Responses、近容量和最终整合全量仍待。下文各轮失败及其首错后未执行项保留原义，不能把分轮通过相加冒充最终全量通过。

调用链修正：Worker、Scheduler、Background 共享一次进程生命周期 clock；Run 原首次启动、批次、allocation、窗口和 UTC 日截止只收紧，退出/让出后不重新计算出更晚期限；SDK store 和 Responses binding 同步收紧当前 bound。启动恢复和每轮准备复用各自的 30 秒协作期限；必要清理仍有独立 15 秒窗口，不等于整段 startup 的硬上界。reconcile 在提交 intent 后、开始外部 cleanup 前复检期限；如已过期，intent 保留，下次真实启动先阻止恢复/派发，不补造清理成功。yielded 探测前后均校验旧 owner/request，不能借新归属继续派发。

候选工作量边界为 16 个 yielded、64 个 active/interrupted 恢复记录、64 个启动活动批次；cap+1 拒绝完整集合，保留原记录。64 个恢复记录包含历史 interrupted，积累过多会保守拒绝，不能声称任意历史规模可直接启动。批次计划复用已有 1–400 个唯一 Run 的有限核验。Background 全日 allocation 汇总仍依赖原数据库查询和健康规模条件，前后 deadline 检查不构成 SQL 工作量硬界。

### 当前失败分类与复验

| 私有收据前缀 | 实际结果 | 结论与后续 |
| --- | --- | --- |
| `clock-control-integration-01` | 15 passed / 1 failed，exit 1，首错停止 | 新测试误将 pending cleanup intent 直接等同通用 execution_fenced 查询；真正合同是本 Run 阻止及下一 Worker.run 的先行恢复核查。已改为真实重启与零派发、原 intent/unknown/首次钟保留断言；后续未执行 |
| `clock-control-integration-02` | 39 passed / 1 failed，exit 1，首错停止 | 前述重启用例通过；新探测测试错误地把已核实 terminal receipt 填入未知库存并要求保留。两轮同类失败后重新检查完整 merge/registry 链，未发现历史删除；改为原 controller 行逐行保留、库存仅含新未知，并保留 prior-unknown 在探测前拒绝的独立用例。后续未执行 |
| `clock-control-tail-01` | 19 passed / 1 failed，exit 1，首错停止 | 原初始化 fixture 将所有带 deadline 的 Library 构造视作 Run 内构造；新增 startup 也使用准备期限。测试拆分 startup 到期零启动时钟与 Run 内到期保留已提交首次时钟，不改变产品安全要求；后续未执行 |
| `clock-control-boundary-fixes-01` | 6 passed / 40 deselected，exit 0，18.13 秒 | 以上初始化两边界、旧未知零探测、新探测未知与并发取消/terminal 原历史保留的针对性复验通过；不是最终专项或完整等待验收 |

日志与收据位于私有 `verification/resume-20261113/`，上述每轮非 Markdown 输入前后相同。对应日志 SHA256 顺序为 `e8350c388754adaf05a3fb421be8cb8192e92a9fd977183c16b73f0c194a992a`、`c6fe25ff21c5824775cda0c8bb3e7c9d567aeebd462c31e8c10ad75e521e451d`、`721f6c718afdc9ee251f5782ea8236b9e35621aad9f3274c22e3e3d4b09ca591`、`3afeb8e944f18ba8980eb584457dd5a9f834ae9599362cd03eca269d45d8e4f0`。这些 dirty 候选的输入清单是源码绑定依据，HEAD 单独不能代表当时全部改动。

下一步先完成剩余期限/维护专项，再运行真实 Worker.run 的四个在途阶段 × 三类前台，以及原计划的容量、锁/持久化/容器/数据库/期限和重启故障。仅合成响应，不重放已经成功的后台授权。最终固定源码才进行完整回归、Ruff、前端构建与文档一致性核对，再按既有条件推进 main 和固定版本服务升级。

补充：`clock-control-tail-02` 在 `e868ad3` 上为 **27 passed / 1 failed / exit 1**（首错停止，后续未执行），日志 SHA256 `ae5f973cdc2be1d185964dddfed4a26829eea6a747e1d08073375550d4c682b4`。旧 delivery fixture 仍通过替换 `worker.elapsed` 模拟墙钟前跳，而新共同期限读取共享 ExecutionClock 的 wall source；已将候选测试改为控制该实际 source，保留至少收紧 4 秒、原账本和零新增交付的断言，行为复验待执行。产品代码未因此修改。

`e868ad3` 的独立静态审查确认有限批次计划、reconcile 提交后复检和会话字段三项接线修复，收据 `m4-wait-control-review-20261114/worker-clock-integration-03/REVIEW_RECEIPT.json` SHA256 `646058bd63b02b1881c15005cf2d5a1da9e4a8f423696975d06ec85f30340498`；静态审查不代替行为测试。


`delivery-clock-fixture-01` 在后续测试候选上 **4 passed / 21 deselected / exit 0**，5.57 秒，非 Markdown 输入不变；日志 SHA256 `98e5ed3233809c229af603bb424d7ce506c4967fbe450945e9993dd1037b4c99`。该测试适配已提交 `0ea2f46`，产品源码仍等同 `e868ad3`。

后续 `clock-control-tail-03` 为 **173 passed / 1 failed / exit 1**，202.05 秒，输入不变，日志 SHA256 `9b37d1f3f133b100d18b12291daf53de5298322968a42de8f8bb2eaf2576c1e0`。首错为原 allocation fixture 要求 `_refresh_day` 未接收 deadline；新合同要求 metadata 和 allocation 均收到相同 caller 截止值。断言候选更新为两者均 `101.0`，仍保留授权、日账、分配/Run/action 完整回滚核对；后续未执行，不是联合通过。

实际 Worker.run 小型正常矩阵首轮 `m4-worker-wait-matrix/matrix-00` 为 **6 passed / 1 failed / exit 1**，后 5 场景未执行。首错“后台 verifier → Arena 前台”的真实检查日志为 `Synthetic fixture gate expired`：共享 checker 对前台错误启用后台专属 gate；无容器超时或清理未知，已核实各进程 reap/EOF。修正候选仅由后台 marker 开启 gate，双方仍需执行原答案检查，并将 SDK close observer 绑定实际 Run。当前该修正版行为待复验；首轮结果不能计为完整等待通过。


共享后台时钟的真实 PG 补测已提交 `e5876cb`，整合为 `b6093db`（仅新增测试，产品源码不变）。**25 passed / 92.30 秒 / exit 0**，170 个非 Markdown 输入前后及提交 blob 一致；真实 claim/create/attach/start、Ledger 最后准入、grant/day 行锁等待、allocation/window 到期、原日账绑定、10 类归属/冻结变更、attach 事务回滚、墙钟回拨后的完整时长结算，以及真实 400 子运行/401 和畸形计划拒绝均通过。未知预留及首次启动时钟保持。两类坏绑定仍为 `BackgroundConflict`，此处只验证拒绝新派发，不声称批次状态已统一转为停止。

该轮没有模型调用或 Worker.run 完整等待测试。测试 PG 与 Run-label 容器清点为空；原始 `m4-execution-clock/background-pg-001.json/.log`、`BACKGROUND_PG_RECEIPT.json` 保存，汇总收据 SHA256 `c2a129cbe12851d94d55a14b019d38684e75693ef599e7c4d84c4a44144438a5`，日志 SHA256 `bd662dc842ab7a7c7f08538a2bbbcc24fb78bbdb4000a25eb35bdfe8224b52f8`。

`scheduling-limits-tail-01` 已对尾部模块完成 **20 passed / 2 warnings / 74.36 秒 / exit 0**，输入前后相同；日志 SHA256 `1c9377694ece67dd90567cec230f2e3e1fb1e04f5cd0be2ab62ed66c0f3b12df`。上述 allocation 断言适配提交为 `47b28d7`，保留到期时完整事务回滚与停止新准入的核对；产品源码没有因此变化。测试进程已结束，PG 测试容器和 Run-label 容器均成功查询为空。该轮关闭已知专项断言失败，不代替最终全量。

正常矩阵的 verifier 夹具修正版 `repair-00` 已通过单例，收据 SHA256 `7285fe71b516a10af7b92f2dddb7443f31cc240af4179f567697b5e7acab8674`。随后 `matrix-01` 为 **8 passed / 1 failed / exit 1**，后 3 场景未执行：generation-model 在 Worker 启动前被 `generation_parent_file_mode_unsupported` 拒绝。调用链核对为私有 runner 的 `umask 0077` 使两个合成父文件为 `0600`，Library 如实冻结该模式，而既有生成合同只接受 `0644/0755`。最小测试修正 `f0b0a63` 仅显式设置两份合成材料为 `0644` 并核对冻结模式，不改产品模式校验或全局 umask；此修正行为仍待执行。原失败收据 SHA256 `8fb08897617b118d69c49ea3d93505b1f0bdb8e78c3f65024a3314b05a97148f`，日志 SHA256 `b54e751ac0495d68372e33799567157540838cc5798041f1b5bf4ac6130af00f`。这些合成场景不消费真实模型授权。

后续 `repair-01` 的生成单例通过，`matrix-02` 已完成 **12 passed / 2 warnings / 100.56 秒 / exit 0**（外层 104.649 秒）。它使用真实 Worker.run、一次性 PostgreSQL、真实 Docker 和 SDK，只有 Chat 模型响应为合成数据，覆盖 model/shell/verifier/preparation × 普通/Arena/generation 前台。每例核对事务提交后的首次模型准入、在途结算、让出回执和关闭、零新增后台动作与兄弟工具、原账目及首次时钟。源码 138 项前后不变；父已将测试整合至 `1e7ada2`，45 个应用/直接 fixture/锁文件与原收据逐哈希相同。

该小容量、无前置前台、入队提交后立即解除合成在途 gate 的样本，提交到首次准入区间总体落在 **1.149526–1.838920 秒**。这是实测范围，不是 60 秒请求配置、近容量或服务 SLA；Responses、较长在途剩余、容量、故障及重启仍为独立待验收项。测试进程退出，PG 与 Run-label 库存成功查询为空。`matrix-02.json` SHA256 `795c3101ddbbc5cc4583dc83fe0110fe5efd4c38c252eb82276546145cab5732`，日志 SHA256 `beac6412cf34f692646731b99bf666ab70b53acd2f8a30e4465c78e5fb1bf712`；逐例原 monotonic 时间线及汇总保存在 `m4-worker-wait-matrix/`，汇总 SHA256 `336b121f53e4c791902b53a51efbcdf6143b38169d392272c68f41d66d0bab65`。

父 `worker-recovery-integration-01` 在 `1e7ada2` 对 `tests/test_worker_process.py`、`tests/test_m3_api.py` 的整合检查为 **25 passed / 1 warning / 91.76 秒 / exit 0**（外层 96.260 秒），全部非 Markdown 输入相同。实际硬中断保留未决 controller 阻止及 API 恢复入口通过；日志 SHA256 `2ff4df9f0a91d599333c9467ad5dfcedc8a4d199a9291997b75730bdc376be44`。PG 与 Run-label 容器成功查询为空。该结果关闭原迁移后的两个模块复验，不能替代新 checkpoint/cleanup 故障矩阵或完整全量。

父 `worker-long-inflight-01` 的两项实际容器等待检查 **2 passed / 2 warnings / 132.01 秒 / exit 0**（外层 136.911 秒），输入前后不变；独立进程组 watchdog 为 360 秒且未触发。前台入队提交后，shell/verifier 容器分别继续真实工作 55 秒，实际 Sandbox 收到原 60 秒动作上限；完整结算、清理、回执/close 和首次前台准入继续走同一 Worker.run。入队提交到首次准入区间分别为 **56.755135–56.767005 秒**、**56.610749–56.624138 秒**，后台没有新增动作或兄弟工具，原已结算账目与首次时钟保持。测试修正与新场景已提交 `cb13165`，只增加合成 gate 的默认零延迟参数，不更改产品配置或期限。日志 SHA256 `56f206c627dccdfe6c8e825dffee6c8a2cf6dfbcbe4a327706152f9f449e9ab0`，PG/Run-label 库存成功查询为空。该实测仍是小容量和无前置前台，不能代替近容量、隔离 Responses 或异常路径验收。


### 隔离 Responses 与完整 Worker 故障场景

`822ef90` 的实际 Responses 基础矩阵为 **15 passed / exit 0**，外层 179.437 秒，195 个跟踪输入前后及 Git blob 相同；父整合为 `b3e459e`。四个后台在途阶段 × 三类前台走实际 SDK Responses 转换、binding、helper/匿名管道/lease，仅 HTTP 供应商为本机合成服务，禁止进入父进程假传输。另三个 8 秒配置临近截止场景分别在实际 helper 截止前约 1.2471、1.2460、1.2468 秒发送响应；helper 均实际 reap/EOF、FD/selector 关闭、lease unlocked。8 秒样本不外推原 60 秒配置。原始收据 `m4-worker-wait-faults/responses-matrix-00/RECEIPT.json` SHA256 `f6f665e2b1f85fd7b71fa72ae4f38c7fd79ccc04ed1d2b7e2f2b89f693c5390f`，日志 SHA256 `012580b7c86a862a9ca7ab2680078e7fa02a8e4aa6a2071b697bace225e46285`；先行单例 `responses-single-00` 也退出 0。两轮无 watchdog 触发，PG 已删除，最终 Run/test 标签库存成功查询为空。

故障分片的产品仍为 `e868ad3`，测试最终 `3335e37`，父整合至 `4afc1ab`；分轮覆盖全部 12 场景，尚须最终整合全量验证。以下失败不是产品安全检查失败，分类来自实际入口及调用链核对，不据失败名称推断：

- `fault-fifo-01` 在 Worker 前拒绝：私有 runner 把 basetemp 放入应用管理目录，TaskSet 正确拒绝；runner004 改用系统独立临时目录，原产品边界保持。`fault-fifo-02` 单例通过。
- `fault-matrix-01` 为 **4 passed / 1 failed**，首错后 7 场景未执行。特殊/变化文件及两个共同期限场景通过；持 SDK 锁时产品按期限拒绝，测试错误匹配英文而实际原因为中文。`e699d173` 仅改成准确原因，并保留 budget_exceeded、实际 deadline 后释放和无救援旁路断言；`fault-sdk-lock-01` 单例通过。
- `fault-tail-01` 为 **2 passed / 1 failed**，后续未执行。真实 SDK close 返回后合成 TimeoutError、真实 PG checkpoint 行锁等待通过；Docker ACK 夹具漏了直接 `_Supervisor.process` 的 start 入口，故在目标故障之前被夹具自身拒绝。`3335e37` 把绑定观察移到唯一实际启动入口，原调用不变；没有以修改生产清理逻辑适应错误夹具。
- `fault-ack-rm-01` 单例通过：真实 Docker rm 已完成，shim 延迟其 CLI 回执，产品终止/reap/EOF 后以实际独立 ps 证明零残留；后台安全让出，前台验证完成。收据 SHA256 `7c9709351b9d1577da0931aee0774526467f11e83ead5e410a3dbce7b25b5a30`。
- `fault-final-tail-01` **4 passed / exit 0**，外层 38.502 秒：实际 ps 回执丢失持久阻止前台及重启；before_save、after_commit、cleanup_intent_committed 三点真实 SIGKILL 后均先核查、不重放。原 Arena/日账/分配/首次时钟保持，cleanup intent 的 1 unknown / 4,824 reserved tokens 保留。收据 SHA256 `43d08e0605e46a77ae0ea3fbed0b76732a185817e1a87f6cb1867ddd5aedd15a`。

故障原始收据位于 `m4-worker-wait-faults/fault-*/RECEIPT.json`；每轮输入不变，无 watchdog 触发，pytest EOF/reap、精确资源清点和 PG 删除有记录。close 故障是实际返回后注入；ACK 故障是实际 Docker 完成后的 CLI 回执延迟，不伪称 daemon 内部永久停滞。正常等待与故障拒绝分开统计，任何未知仍阻止派发，未改变生产账本或模型配置。


原 60 秒 isolated Responses 后续样例 `responses-sixty-00` 也已通过：源码 `091f86f`，父整合为 `df275fe`，exit 0、外层 74.433 秒，195 个输入前后及 Git blobs 一致；没有改变产品期限，只为合成 fixture 增加显式观察时间。响应在实际 armed helper deadline 前 **1.241098 秒**送达，入队提交到首次前台准入为 **59.424576–59.445822 秒**；两个 helper 实际 reap，全部 FD/selector 与 lease 关闭，watchdog 未触发，PG/Run-label 清点为空。收据 SHA256 `671a38b717186a51de75bdc2c0f97ca154048bbc7f0ab1e24d993e0cfd9d55ae`，日志 SHA256 `de53712e1af890e7b00a80fc94676774a11a1a93c360c83d8b5f0991702c2955`。这是原 60 秒配置的小容量完整路径观测，不是任意容量或外部供应商 SLA。


近容量会话首两轮尚未通过：`capacity-small01` 在最终 close 次数断言失败；`capacity-small02` 在三次 receipt 必须全相同的断言失败。两轮实际 Worker 最后均完成，但不因此改写测试退出 1。第二轮后停止追加补丁，审计真实 SDK 初始化、发送消息、恢复验证、再次让出、保存回执、最终 pending 动作和 close 全链。审计确认旧 fixture 在 SDK 正常初始 SystemPrompt 之前插入 1000 事件，导致冷恢复合法追加相同的 SystemPrompt 与 Pause；原事件是完整相同前缀、原两项 Action 身份与结算记录保持，最终 Observation 各一次。应将合成历史插入移至真实初始化之后，并精确核对 R0 验证 → R1 保存 → R1 验证，不能简单允许任意摘要变化。

close 原解释同时更正：先前口头称“finish 关闭后 finally 再关闭”未经源码支持；源码核对为 `run_session` 的 finally 和 `LocalConversation.__del__` 的幂等 close，后续观察器须保留真实 caller 与所有清理完成证据。原工件审计 `m4-worker-session-capacity/LIFECYCLE_AUDIT_001.json` SHA256 `455185f887d86c2a6cc74b83bed77f493f34c3900b158da6403ba5a0de27ce0c`；small02 收据 SHA256 `6cce417774cf3dcd262a6d63bb07a40bb6696f77c8840a5b48f3b966302a07b1`，日志 SHA256 `061f9e678e47e7bcc6cbc4f169d217ff8ce178b9c6a19545af777bdc705bf8d9`。原 PG/Run 库存成功查询为空，watchdog 未触发；修正后的行为仍待。


### 完整容量矩阵与会话故障复核（2026-11-14，最终整合仍待）

Git/yielded 容量源码 `f72735e`、`9c12044` 已分别整合为 `bcdf7bb`、`57c7f84`；产品应用源码仍与 `e868ad3` 相同。私有 `m4-worker-wait-faults/capacity-matrix-00/RECEIPT.json` 记录 **13 passed / exit 0**，外层 165.533 秒，196 个输入与 Git blobs 前后匹配，watchdog 未触发，pytest 已 reap/EOF、一次性 PG 已精确回收，两种标签库存为空。父已独立核对原日志 SHA、收据、应用源码和资源终态。

五种正常配置从实际入队提交到首次 Ledger 准入提交的区间上界分别为：16 个真实独立 SDK yielded 检查点 2.352210 秒；原始 Git 树 19,999 项 1.740155 秒；原始树字节 16,776,454 处 2.065464 秒；保留 9,999 项 3.798053 秒；保留 104,856,576 字节 1.991486 秒。17 个 yielded、尾部残留及五种 Git 超限分别拒绝，前置前台队列单列，原账目保持。容量分别构造，不外推组合满额或冷机。收据 SHA256 `054ad318c92384dee9a93003112f1e2eb01b6f0ed3b21fee692817b867979b5c`，日志 `91a62e7dcafe7f1c6bb69635541587e220b7c374f3a4d89556e5b46138aaed19`。

会话容量夹具在 `e65a647` 保留 SDK 原生 bootstrap 后，`capacity-small03` **1 passed / exit 0**，外层 25.07 秒；原收据 R0 验证→新增一条原生 Pause 的 R1 保存→R1 验证，以及 pending 工具各一次完成均通过，原事件前缀、账目、首次启动时钟保持。原生 `run_session` finally 执行首次 close，SDK `__del__` 可幂等重复，全部调用记录保留。收据 SHA256 `3591e0e74578afe8d84ab93b6a57134c546896bdaff31784b90f9105687af4f3`，日志 `58db46a5e4919d85d8c5b4b8d1ffc477013c8b5560c0f5d540ad0f13f4dad4a8`。

随后同一冻结源码的 `capacity-matrix01` 是 **3 passed / 1 failed / exit 1**，外层 68.114 秒，首错后 4 项未执行，不能称只剩一项。small/bytes 两个正常恢复场景及输入 token 拒绝通过；第 4 项预期 `source capacity` 对外透传，实际为 `Session persistence failed`。源码显示 `SessionFileStore._operation` 在容量异常后锁存该错误，后续 `check_ok` 使用锁存原因；仍需以内部真实 source guard 观测证明正确限额在预留前触发，不能仅放宽字符串断言。此问题与此前 bootstrap 生命周期夹具缺陷不同。收据 SHA256 `1ac7b62ed049a311e1dd404d0209709bdce9b226bdc9adb375b842577b785f47`，日志 `03f57c899dbe83687363d030b21630948650c8d45f2a9b42206fa46e2103aeda`；输入不变、watchdog 未触发、PG/子进程及容器清点完成。旧失败全部保留。

正常等待按[最终检查前冻结的 90 秒目标](M4_WAIT_ACCEPTANCE.md)核对；以上仍是专项，不能替代最终固定源码全量及独立时间线后验。本轮没有真实模型调用、生产数据修改或常驻服务升级。


### 历史规模的实际 Worker 等待专项

新测试 `e4a3a3d` 首轮 `scale-cold-01` 在合成负载构造阶段退出 1（外层 15.264 秒）：`jsonb_build_object` 的文本占位参数缺显式类型，PostgreSQL 报 `IndeterminateDatatype`，负载事务回滚，目标入队计时尚未开始，warm 未执行。修复 `5dc8da5` 仅为八处占位显式增加 `::text`，不减少样本或修改产品。失败收据 SHA256 `5fa13c690c1c5ec9b2b1cc5d7bb4fdbdb819425626810ef6f9700ca4a8d19232` 保留。

修复后 `scale-cold-02` 与 `scale-warm-01` 均独立 **1 passed / exit 0**，外层分别 21.419、22.996 秒。实际完整等待区间分别为 `[0.752428, 0.762523]` 与 `[0.413982, 0.422654]` 秒；前者包含新 Worker 初始化、锁及恢复核查，后者同一 Worker 在 warmup 完成后执行真实 `next_enabled → prepare → enqueue → admission`。宿主、daemon 和缓存仍已热，不称冷机。

负载为明确标记的 100,000 条终态 Run、100,000 条终态 Action，及各 10,000 条历史 Arena/授权/分配，是索引负载而不是已完成真实任务的证据；64 份授权实际经过 create/enable，64 只是样本数。真实构造的 unknown 4,824 token 预留、yielded 回执、原首次启动时钟、父/分配/日账与历史行指纹保持。两轮 195 输入前后相同、watchdog 未触发、进程 reap/EOF、PG 精确回收及独立容器清点通过。父逐项核对收据、日志和应用源码后整合为 `440e0c4`、`5c7b068`；最终全量仍待。

私有目录 `m4-worker-wait-scale/` 的 cold 收据 SHA256 `50c4183bbc52720a572b0b47d01f91bb3737b38b626a2e867ccf0a58d30a54b9`，日志 `3db938e614378aa52bdb6bba72a24b14564f1ecb897ee373d33a32ee8e69048d`；warm 收据 `6ca902fbea6e98cd3c03bb57232944c90c30388f988ccd447bd1d77fcc124548`，日志 `d6a5a6d2cc7cc269be5708a0d54bc0318120d1204c4bfd30e1deefb0fd46c94c`。没有真实模型调用。


独立时间线审计补充了 long55 的精确源码归属：`worker-long-inflight-01.json` 记录的 HEAD 是 `1e7ada2`，运行时 `tests/test_worker_wait_matrix.py` 尚未提交，其实际捕获 SHA256 为 `cf23026a98e8313fa380d66458bb4f28a4e9783eaa0acedea269ff0172fe5c9d`，与随后提交 `cb13165` 的该文件逐字节一致；其余 170 个输入匹配原 HEAD。因此这轮绑定是“原 HEAD 加捕获的 fixture”，不能写成原 HEAD 干净运行。原收据不改，后验逐文件注明此补绑定；应用源码未变，最终整合仍将实际重验两个 55 秒场景。


会话尾部 `capacity-tail01`（候选 `4bac8e4`）为 **4 passed / 1 failed / exit 1**，外层 54.983 秒，最后 bytes 正常场景未执行。原 source 容量异常的内部 observer 与三项 cap+1 拒绝通过；unknown 场景仍注入原 `TimeoutError`，SDK 将其转为 `ModelCallError`／`ConversationRunError`，实际 Run 是 `failed`，测试错误地期待 `interrupted`。因与上一轮同属未核实异常包装／外部状态合同，已暂停局部修补并重新审查全部八场景的异常、用量、恢复和后续派发链。

既有 `test_provider_failure_classification.py` 已明确普通供应商异常为 `failed`，用量未知与生命周期状态分开。原八表显示 Run/executor 均 `failed`、一条 model action 为 unknown、`reservation_retained=true`、73,686 token 预留及父账相同，allocation/day 仍预留；没有证据支持改成 `interrupted` 或改注入故障。后续将保留超时、精确核对失败分类、原账/首次钟及重启不重放，不能只改为接受两个状态。原收据 `9de91287934e7a63a279a9f2e7024ab9f5029b9cbcff6bcf9a76bcd37be3ccb6` 与日志 `0843745d27f5a044fe9acda26fa33c4cce4ae795a8b0ac057d93de38ab1d9830` 保留；输入不变、watchdog 未触发、进程 reap/EOF、PG 与容器已精确清点。


### Session 最终专项与全量前环境核对

最终会话测试 `406ab38` 已整合至父 `0d83336`，文件 SHA256 为 `b02c2e737ad70fdd8a8580f0eaf8ac5654ee108ca250061a7697e00cf8252771`；产品代码仍与 `e868ad3` 相同。`capacity-final02` **2 passed / exit 0**，pytest 41.38 秒、外层 46.400835 秒。原 TimeoutError 精确传播为 failed，unknown action、73,686 token 预留、完整 Run、首次启动时钟、文件和父账在新 Worker 后保持，无同 Run 会话或传输重放。failed 不进入启动 reconcile，本样本不声称验证该路径。bytes 正向含 1000 active events、65,899,453 bytes，warm／startup／restore-arrival 等待上界为 **3.140930／0.699143／7.068546 秒**；原 pending shell 与 finish 各完成一次，额外模型调用零。

196 个冻结输入不变、watchdog 未触发、进程 reap/EOF、PG 精确删除及两类 Docker 标签库存清点通过。私有 `m4-worker-session-capacity/capacity-final02/` 收据 SHA256 `1562569c8239d2ccb86e9d987148ba83e9d03dc398498db4690351b65cc6bd75`，日志 `e14f7bd858d39891b4216e3e1dac2d14f586780f523600fed532526faa78bdda`。全链合同审计 `CAPACITY_CONTRACT_AUDIT_002.json` 为 `409b38c840fdebccdd89aee1a1f3a42f21daa38e53911a7116336f9b5f85d583`。八项完整整合仍待，不合并分轮数字冒充整轮通过。

全量前系统 Data 卷曾仅余 142 MiB，是环境容量阻塞。仅将四个已结束且有精确收据的 fixture 根目录逐文件复制、核对字节／模式／链接 manifest 后归档到私有 `/Volumes/T7/doonex-private-verification-20261114/fixture-archive/`，原路径保留符号链接；49,249 项、652,507,225 逻辑字节全部核对，未删除历史失败、生产数据或未知预留。归档收据 `ARCHIVE_RECEIPT.json` SHA256 `f6e5c666183429e9a1e96f985e72f44f5ada2f0b261607e9c6f372df9bd0b512`。之后实际系统盘可用 2.1 GiB、T7 734 GiB。最终整合将显式使用同一私有 T7 根的 `scratch` 作为 TMPDIR；这是测试存储条件，不是产品模型、预算或架构变更。

独立专项时间线目前覆盖冻结的 43 种正常配置，均有实际准入 upper≤90 秒；最终观测尚未执行。下一步固定源码，依次运行 Ruff、前端构建及带原始 nodeid／时间线输出的完整 `scripts/test_local.py -s -vv`，再独立配对 43 份最终观测与专项。

### 最终全量首轮：运行环境中断，原结果保留

最终源码冻结为 `a375ed492e1420da361472b14f1efedfdb251210`；Ruff 与前端构建均 exit 0，全部非 Markdown 输入前后不变。`m4-closure-full-01` 在该源码执行完整 `scripts/test_local.py -s -vv`，未首错停止；结果为 **1927 passed / 14 failed / 667 errors / 1 skipped / exit 1**，pytest 676.40 秒、外层 680.374 秒，watchdog 未触发。日志 SHA256 `47ab482d21e80d400f7801e68f78115e0a38986057771a9506dfe6de57ed374e`；不能计作最终通过。

完整错误分类与运行时日志定位到 UTC 2026-11-14 11:50（纽约 06:50）的 OrbStack 停止请求和 Docker 关闭。首个失败的原 SDK `ConversationErrorEvent` 记录 `terminating connection due to administrator command`；其 teardown 及其后 13 个失败、667 个错误均为同一临时 PG 的 shutdown／连接断开或拒绝。未推断停止请求来自谁；没有据此修改产品或测试。目标 Worker 等待矩阵未成功执行，不能把未执行场景视为通过。私有 `m4-closure-full-01-environment-audit.json` 保存脱敏逐类统计、原事件摘要、运行时停止日志和结束后空 Run/test 容器库存，SHA256 `0ed7a3d29d697d5dd1ba16355c8320955c41f33355dd5f5872b4e0b15f9b6718`。

T7 区分大小写，原 `test_case_alias_cannot_hide_verifier_repository_overlap` 按已有前提跳过。环境恢复后，在系统盘以相同源码、原断言补验此项，并复验首个受中断影响的历史合成后台夹具：`m4-closure-environment-tail-01` **2 passed / 0 skipped / exit 0**，72.21 秒、外层 86.829 秒，输入不变、无 watchdog，日志 SHA256 `b850ba7e1e8aa87a66ef43000f719eb8c6c0319f0861bd1a8706d0d3f5871241`。这只是环境诊断与平台补验，不替代整轮回归。原失败、跳过和全部工件保留。

随后确认 Run/test 容器库存为空，以同一 `a375ed4` 唯一启动 `m4-closure-full-02`，TMPDIR 仍为已记录的 T7 scratch，命令追加 `-rs` 显示跳过原因；没有修改源码、缩减正常配置或重放真实模型授权。最终结果待实际收据。

### 最终全量第二轮：八项失败的完整链复核

同一 `a375ed4` 的 `m4-closure-full-02` 已完成全部检查，**2599 passed / 8 failed / 1 skipped / exit 1**，pytest 4138.92 秒、外层 4152.802421 秒；176 个非 Markdown 源码输入未变，watchdog 未触发。日志 SHA256 `e37d06bb5a49026649759f50ba6f9c533a633542701a883dc66afa35a2ef3de6`。大小写平台跳过仍引用前述系统盘原断言补验。不能因正常等待场景通过就把整轮写为完成。

随后仅重现八个失败节点，`m4-closure-failures-reproduce-01` **3 passed / 5 failed / exit 1**，pytest 17.23 秒、外层 31.214498 秒，输入不变、无 watchdog。私有只读 pytest observer 在 teardown 前记录实际 controller 行；日志 SHA256 `c945b2d0cf1fc99f6c47b19b2776fca028ae8e514a17826dcfe1dc7e9a3d0648`。按完整调用链分类如下，不统一归为测试问题：

- **产品整合缺陷：恢复报告顺序。** recovery GET 先调用容器探测、后读取 helper fence，真实 registry 正确拒绝，接口却返回 409，原报告合同要求 200 与明确阻止原因。修复应在任何 Review/Git/文件扫描/Docker 操作前只读报告已有阻止，未核查字段不能伪装为空；POST 及 registry 的全局阻止保留。
- **产品整合缺陷：Run 时钟停止传播。** `Worker.run → resume_yielded → validate_receipt → preflight → execution_bound` 的 Run 级 `Halt` 未被恢复路径接住，逸出主循环；没有观察到额外模型/工具派发，但状态仍未持久停止。最小修复将其接入已有 `interrupted / scheduling_resume_refused` 分支；批次级 `BatchHalt` 仍单独向外传播。须验证首次时钟、原用量、SDK 回执、动作及再次启动不重放。
- **测试前提缺陷：真实容器 timeout 两项。** 0.4 秒总窗口包括持久登记、mount 与 Docker create；原始行证明 create 被 SIGKILL、未获 ACK，`daemon_unknown` 正确保留。原测试预期已启动 shell 后安全超时，实际未到该阶段。夹具改为入口一次性 3 秒，增加真实命令输出标记、container ID 和清理断言；不修改产品期限，不接受 unknown 为成功。
- **测试注入/修复窗口缺陷：迟到 create 与逃逸管道。** full02 的迟到 create 初次未知检查已通过，后续仅 0.35 秒 repair 未完成；复现该项通过。escaped 样本 full02 在 start permit 前到期，复现虽启动 CLI，但在创建逃逸子进程前结束，真实 reap/EOF 完整。夹具须明确到达故障：迟到 daemon 在实际空 ps 后通过有限 release gate 才创建；escaped 保留真实子进程身份与非 EOF 断言。入口 3 秒一次性期限和后续独立 3 秒 repair 不刷新原操作，修复后原 unknown/fence 必须逐项保持。
- **测试前提缺陷：Git copy local/outer 两项。** 0.2 秒窗口在 export 的 registry DB 复检已到期，原 `RegistryError(deadline)` 与 unknown 正是既有 `test_pg_expiry_while_before_process_waits_starts_no_popen` 合同；复现两项通过。保留生产拒绝类型，copy 夹具改为入口 3 秒并严格断言实际进入 copy 时尚未到期，再等待同一期限验证零目标目录。不能用接受任意异常替代 copy 覆盖。

full02 遗留一个、八项复现遗留两个 Created 容器，均按实际 inspect、测试挂载路径、命令和原 controller 行确认唯一归属，保存后精确删除，未按标签批量清理。两份收据 `m4-closure-full-02-residual-recovery.json`、`m4-closure-failures-reproduce-01-resource-recovery.json` 的 SHA256 分别为 `fde20124c3a845bbf3de6a5d652d1801428f1fb74d975da48b0d93b7f01a2fed`、`08536245a165eada9a0061c61323c0f0964fcec62e6984c1fb6e0c2f95a0a774`；最终 Run/test 库存成功查询为空，原失败和未知记录没有删除或改为已解决。修改后的针对性与最终整合验收仍待，不能合并 main。


上述父修复的 `m4-closure-fix-target-01` 已完成 **7 passed / exit 0**，pytest 37.97 秒、外层 52.241600 秒，176 个实际捕获输入前后不变、watchdog 未触发。日志 SHA256 `b036e99bcb9fcee5883b44f75ea3c29e8a68950d12b4fa2489d65fb2347b6857`。运行时 HEAD 仍为 `a375ed4`，含收据逐文件绑定的未提交 scheduling 与五份测试修改，不能写成干净旧 HEAD 通过；恢复 GET 不在这七项中。相关模块整合与最终全量仍待。

### 相邻调用方整合与故障夹具完整复核

父提交 `3429eed` 的八模块 `m4-closure-related-01` 全部执行，**197 passed / 1 failed / exit 1**，pytest 469.62 秒、外层 484.735457 秒，176 个非 Markdown 输入前后不变，无 watchdog；日志 SHA256 `8d25fde40f90608fe517c14fbd211dfac5c0dc11c3634256843e106e476d272e`。唯一失败为旧 `test_create_ack_loss_is_cleaned_by_preselected_name`，实际 operations 中没有 create，`next(create)` 抛 StopIteration；不是清理成功或安全要求失败。结束后 Run/test 库存成功查询为空。

同类启动窗口问题再次出现后，停止按节点追加时间数字，重新逐项审查整个 Sandbox 故障模块的 24 个测试函数。先前只修已报错样本的判断不足：多个夹具共享“登记、DB 复检及 Python 子进程初始化在几百毫秒内完成”的隐含前提，部分宽泛异常断言还可能在目标注入点前满足。独立完整表 `m4-sandbox-deadline-fixture-audit-01.json` SHA256 `318f4cb60529123297a8027eff2d13e5ce5241c9d3023d1e8e09834377ff347b`，先行 diff 审查 `m4-sandbox-workspace-failure-review-01.json` 为 `72313ac7104c942d79d73d9d889781eaa12a0a95e51aabc818e4ec4b5c3e5565`。

模块性修正保留生产 Sandbox/Workspace/controller：run 级故障从入口取得一次性 3 秒窗口，common-create/start 及 caller 场景被动核对各阶段实际绝对期限和持久 controller 期限；cancel 仅在 fake create 实际写入后触发；继承管道要求真实 fork 标记；copy、ACK、列表解码/限额和最终残留检查明确证明目标阶段。原未知保留、原 repair 不解除 fence、零新派发、清理窗口不刷新及外层耗时断言继续存在。默认夹具清理 0.35 秒、专用 stalled-cleanup 1 秒和生产配置均未更改；这些仍是健康本机下的附加测试条件，不能外推永久失联时成功。

恢复报告修复已整合为 `235e67f`，暂停任务显式重新核查入口为 `abea283`：已有全局或本 Run 阻止时 GET 在 Review/Git/scan/Docker 前返回报告，`containers/workspace_ok=null` 明确未核查，普通 POST 与确认界面仍拒绝；阻止解除后可以重新核查。子片仅做静态检查、构建及收集，没有把它们写成 API 行为通过；父当前以 `m4-closure-recovery-fixtures-01` 联合验证完整 Sandbox 故障、Responses fence、清理、API、多项目及生成调用方。

最终两轮准备改用新的私有系统盘 TMPDIR `/private/tmp/doonex-m4-verification-20261114`。现场系统卷可用约 115 GiB，T7 的全部原失败与收据保留；该变更使最后完整回归实际执行大小写别名场景，未减少范围。由于本轮有产品恢复路径变更，将在同一最终源码重新运行全部 **43 配置 / 39 唯一 nodeid** 正常专项，再运行无筛选全量；两个 session nodeid 各自输出 warm/startup/restore 三配置。90 秒目标不变，不用产品差异豁免替代新观测。两轮尚未执行，仍不能合并 main。


父联合 `m4-closure-recovery-fixtures-01` 已完成 **130 passed / exit 0**，pytest 147.97 秒、外层 161.208987 秒；176 个实际捕获输入不变、无 watchdog，日志 SHA256 `9d05beaa6f9ba741e38df4a3c1813335ab1098d7d8f83bae9e78a910aec2c959`。绑定为 `abea283` 加捕获的未提交 Sandbox fixture，不称干净 HEAD 全部通过。随后仅补充 ACK 实际副作用与真实 leader/fork 的严格证明，完整模块 `m4-closure-sandbox-phases-01` **42 passed / exit 0**，pytest 46.01 秒、外层 58.732973 秒，输入不变、无 watchdog；日志 SHA256 `b4c6b6cc2fdb9b91cbedc191770511a9aa8f2c1bc43a6c6312667f1c4a1f9e14`。结束后独立 Run/test 容器库存成功查询为空。以上关闭针对性整合检查，最终 43 正常配置专项及无筛选全量仍待。

### 第三轮完整回归与取消阶段实证

固定源码 `05ee7bb54c0671fbb777fb787fbe643f03d93304` 的 `m4-normal43-specialized-01` 完整执行 **39 passed / 43 正常配置 / exit 0**，pytest 828.97 秒、外层 842.225874 秒；176 个非 Markdown 输入与提交及前后文件一致，无 watchdog。独立原始时间线审计最大较大等待区间端点为 **59.875066125 秒**（原 60 秒 Responses），预冻结 90 秒目标不变。专项日志 SHA256 `b679db259bbaa97e7c553f24d06a5e21f44a8024c4b24a3f1343255b0942444a`。

同一源码无筛选 `m4-closure-full-03` 全部执行，**2612 passed / 1 failed / 3 warnings / 0 skipped / exit 1**，pytest 4979.42 秒、外层 4993.158180 秒。176 个输入不变，无 watchdog；系统盘 TMPDIR 实际执行了大小写别名场景。日志 SHA256 `5e64c0010a420402623027179f0a802f0f38f534cd14899d5f31a96924ffd855`，收据 SHA256 `889994a323ab80f5d9b212189e66415d994ec79e2008b93842faef68432e54b5`。恢复 GET 的五类副作用前阻止、yielded 原时钟到期拒绝、全部 Sandbox deadline 及 Worker 正常/故障/容量场景通过；唯一失败是 `test_cancel_stops_current_and_future_action` 的后续普通容器探测遇到 `RegistryError(fenced)`。这不是最终整合通过。

冻结 v3 独立审计据 full03 非零退出码返回 **invalid / exit 1**，`final=null`，43 项 final 仍 pending。原正常观测只作为诊断保存，最大端点 60.852313917 秒，最终覆盖计数为零；没有改 manifest 或增加源码差异豁免。失败审计 SHA256 `bd90154ec2803b1d19843191cb491cdf4530ee8203de45dd4974d5df017765af`。Ruff 与前端构建在 05ee7bb 均通过，但不改变整轮失败。后续源码改变后仍须新正常 43 专项及新无筛选完整回归。

取消原样例连续两次诊断 `m4-cancel-reproduce-01/02` 均在一次性 PostgreSQL 启动时退出 1，分别 17.482544 / 17.472217 秒，**未进入 pytest**。第二次只读保存删除前日志，initdb 尚未完成，没有凭连接拒绝推断产品问题或数据库崩溃。随后停止盲重试，独立 `m4-pg-startup-diagnostic-01` 使用相同固定镜像、相同 TCP SELECT 1，观察到 **25.773145 秒**后成功：initdb 落盘约 18 秒，临时服务 checkpoint sync 4.816 秒；VM 磁盘/内存充足、无 OOM，精确回收后库存为空。诊断收据 SHA256 `59b38de157f0d86f79fe33fb35810486d71a5885343f1caf089c720eac8e62b1`，该诊断不是测试通过。

最小 harness 修正仅将 `scripts/test_local.py` 的 60 次 × 0.25 秒准备探测改成一个 monotonic 60 秒启动窗口；固定镜像、真实 TCP 查询、失败退出、精确 finally 回收保持。连接/查询和最后 sleep 仍可跨过探测截止，这不是脚本严格总上界。此步骤在 pytest 和所有 Worker 测量前，不改变 90 秒验收目标、产品任何期限或模型配置。

修改 harness 后，原取消 fixture 的 `m4-cancel-reproduce-03` 实际进入 pytest 并复现 **1 failed / exit 1**，外层 28.790699 秒。只读 observer 在 teardown 前保存 result 和 controller：mount 成功，`docker_create` 被 0.6 秒回调取消（exit -9、实际 reap/EOF），没有 ACK/container ID，持久 `daemon_unknown`；后续 rm/ps 虽返回零且当时为空，普通新 probe 仍按合同拒绝。结束后 inspect 仍发现同身份 Created 容器，进一步证明不能把空快照视为原请求完成；其元数据不用于推断 daemon 创建的绝对先后。日志 SHA256 `adfba8a8fc04b23a61d7f1e4465186c192999b8e482765f2fc5b89c298003e2a`。逐项核对精确 name、Run ID、原 sleep 命令和测试 mount 后仅删除此容器，保留原未知；资源收据 SHA256 `4b5e07df79620bde197b161d75326add78aec39b3f10a204588c40c5aaf947c5`，最终 Run/test 库存为空。

本次确认为测试阶段前提不足，生产 Sandbox/controller 不改。原 fixture 用墙钟 0.6 秒推测 shell 已运行，新 fixture 必须观察真实容器命令写完 `running` 标记再取消，仍保留原 10 秒总业务窗口；断言实际 container ID、cancelled 且非 timeout、清理已知无错、零残留及后续命令未执行。第二次立即取消核对无 mount/create/start 业务派发，允许必要最终 ps。未到阶段、结果未知或清理失败不能通过；有针对性和相关整合结果待实际收据。

取消阶段单例 `m4-cancel-phase-target-01` **1 passed / exit 0**，外层 31.245800 秒，日志 SHA256 `78cdaa37efb36eff05e279dc386baf8d327c31bde84ab4596a06e7f8cfb30cde`。随后三个完整相关模块 `m4-cancel-related-01` **90 passed / exit 0**，pytest 83.12 秒、外层 110.143134 秒，日志 SHA256 `d8a1f61c7b2fcb4dd1d356f60485347c3599f48ea14331c658cb41a5ca96a651`。两轮 176 输入前后不变、无 watchdog，绑定 05ee7bb 加收据内未提交的 harness/fixture，不能称干净 05ee7bb 的新证据。原 create ACK 未知、迟到创建、EOF、旧 owner/context 拒绝和真实 SIGKILL 持久阻止均继续通过。修正后的测试准备实际正常进入 pytest；此处只关闭针对性与相邻整合，随后固定候选重新进行 43 专项与完整全量。

### 新固定候选 eb5dd1e：专项通过，完整全量进行中

当前非 Markdown 源码固定为 `eb5dd1e58c98d31afe49e8015240dfccdaef082d`。新 `m4-normal43-specialized-02` 完整执行 **39 passed / 43 配置 / 2 warnings / exit 0**，pytest 1126.25 秒、外层 1154.003637 秒；176 个输入前后及 Git blob 一致，无 watchdog。收据 SHA256 `94c5f162f24cb7b3502326e3229e92e9f834a766fd65b03df09e4c688e407c03`，日志 SHA256 `d74543ed6d9f0f5a85892d251ffde41b5e86ddf3c653da6ad8e18423b2a770ee`。

冻结 v3 的新专项独立审计核对 **43/43 配置、39 个唯一实际 PASSED 节点**，最大较大等待端点 **60.369285708 秒**（原 60 秒 Responses），预冻结 90 秒目标保持。报告 exit 2/pending、invalid=[]，恰有 43 final 项待完整全量配对，不能写成最终通过。新 manifest SHA256 `107ee3fc01cfbf9dfa754ee758e2929e18d7e5847712c620645479446e198ff3`，专项审计 SHA256 `597786dc1d2ad84408ef0846df3bcbba7e9ccc2b6fcd2095a1159f005711e0e7`；原八轮失败全部核验、计零覆盖，没有源码差异豁免。

同一候选 `m4-closure-ruff-04`、`m4-closure-web-04` 均 exit 0，176 输入不变。新环境收据 `m4-final-environment-04.json` SHA256 `83c0fd16efc312a827569ad9cbb8e64c5a644e0ee1808a9603944ca06e9c41d6`，系统盘 TMPDIR、大小写不敏感、已有固定镜像。独立 `m4-pre-full04-source-resources.json` 核对三轮全部输入、日志和 Git blobs 一致，Run/test 容器及测试进程为空后，唯一启动 `m4-closure-full-04`，命令为 `env TMPDIR=/private/tmp/doonex-m4-verification-20261114 .venv/bin/python scripts/test_local.py -s -vv -rs`，无测试筛选，外层 watchdog 7200 秒。完整结果待终态收据；不据启动或中间通过数提前合并 main。

完整回归 full04 中途出现 `test_stream_checks_original_saved_operation_deadline` 失败，仍在执行，尚无终态 traceback。静态核对发现原测试保存 0.1 秒期限后还须经过提交、Reader 初始化、请求预留及客户端准备，末尾 `count + 1` 断言只是阶段结果校验，不能保证故障注入前已到达 stream；此为候选原因，不能据此认定本轮根因。保持源码与运行不变，先收集完整失败，再原样复现并观察实际请求与持久预留阶段。该项对应原期限不刷新及到期停止派发的既有条件，没有增加验收范围。

同轮随后新增 `test_finite_cleanup_window_cannot_refresh_for_stalled_ps` 失败，仍待终态，不称只剩两项问题。该例涉及入口 3 秒业务期限、独立 1 秒清理窗口、rm/ps 共用截止与最终持久结算。静态核对发现原 `entry < 1.8` 还隐含准备阶段少于 0.8 秒，而 `ps.at-rm.at < 1` 不能单独证明清理期限未刷新；旧审查提出的同期限被动观察未落实。实际失败位置和分类仍须完整 traceback 及原样观察器复现，不据此放宽清理条件或增加时间数字。

两项原样复现的被动观察器已在私有验证目录准备，尚未执行；不改生产返回值、期限、休眠或原测试断言。Discovery 为 `resume-20261113/m4_discovery_observer.py`（SHA256 `2d9ac2b917ec80d4ecc3303dec60b6fba12bb8e0c10f56402dcd39afd69588ba`），清理为 `m4-worker-wait-scale/m4_stalled_ps_observer_001.py`（SHA256 `9bf2d70c68c09556598fd00126ef9d89183e73748eb300d2e2dc126e538c206b`）。后者记录真实 `Sandbox.run`、`_Cleanup.start`、各 process、permit 与 settle 起止，仅在原测试调用结束后读取持久 controller 行。完整回归结束并核对资源后才由父独占运行；静态审查和观察器准备不计为复现或验收通过。

### full04 终态及原样复现入口

`m4-closure-full-04` 已无筛选全部执行：**2611 passed / 2 failed / 3 warnings / 0 skipped / exit 1**，pytest 6038.68 秒、外层 6066.936805 秒；176 输入不变，watchdog 未触发。收据 SHA256 `8125f3cfc74190f81cdd184889c8a4da8b91552425fc6160d7dd3768d9218e46`，日志 SHA256 `d44e193a4f3e21c6ee2ea2a645015c632e7fbca148364802cd8900d879ed9619`。结束后原测试进程退出，实际 Run/test 标签库存均成功查询为空。

终态 traceback 明确：Discovery 的 failed、unknown 和 BLOB 预留断言已通过，但 HTTP 请求数为原 6 次而非 7 次；原 stream 故障尚未到达，预留后、HTTP 前的本地期限拒绝正是当前源码路径。Sandbox stalled-ps 在总调用耗时 `470525.761739125 - 470523.866629708 = 1.895109417` 秒处触发 `<1.8` 断言；其后的 unknown、容器缺席及 rm/ps 时间差断言均未执行，不能从本轮日志推断它们通过。父保持原源码，用两个被动观察器启动唯一 `m4-closure-failures-reproduce-02`；该轮尚未终态，静态候选原因不替代实际阶段证据。完整等待矩阵的正常输出只保留为失败轮诊断，不关闭最终条件。

### full04 原样阶段复现与最小合同修正

原源码的 `m4-closure-failures-reproduce-02` 已结束：**1 passed / 1 failed / exit 1**，pytest 3.34 秒、外层 33.709153 秒；176 输入不变，无 watchdog。日志 SHA256 `648635c97ca5f60b47717491fa9899534abbb51032384bfda6d52e8c542bde72`。Discovery 本次真实到达 HTTP，收到 220 字节后按原 deadline 保存 unknown；单例通过不覆盖 full04 的原失败。

stalled-ps 原断言再次在整个调用 1.846354 秒处失败。被动观察保存实际共同 D=474390.341268958：准备约 0.7775 秒，rm/ps 共用清理窗口，ps 完整回收/EOF 确认在 D 后约 10.25 毫秒，Supervisor 返回在 D 后约 10.75 毫秒，持久结算约 57.33 毫秒。全部六个真实子进程的 after_process 与持久记录一致，rm/ps 均 exit -9 且 reaped/EOF；最终 unknown/supervision_unknown，global fence=true。不能声称回收与持久结算严格在 D 内。观察收据 `m4-stalled-ps-reproduce-01.json` SHA256 `3a79ff412116c7aa5c06c87dede0ef6ba884c80347fb82f55ea7fa214aa37e30`。

随后 `m4-discovery-reserve-delay-01` 在真实 blob reserve 提交后显式注入 0.15 秒有限延迟，原样断言以 **6 != 7 / exit 1** 重现；HTTP 前拒绝，received=0、http_status=null、unknown 与完整 4,259,840 字节预留保留。外层 33.546855 秒，输入不变、无 watchdog，日志 SHA256 `e8f2e6fe06a8676fbca756a930f0cccd55ee29116d81f190b83a0bad540b386a`。这是受控延迟因果诊断，不冒充 full04 的微观时序。两次资源终态均无 Run/test 标签容器。

根因归类为两个测试阶段/时间锚点前提不足；现有证据未显示这两处生产 deadline 刷新。只改两个测试文件：Discovery 在同一 UTC/monotonic 逻辑钟上保存原 0.1 秒期限，真实 commit 后到 0.04，再真实读取 0.09/0.11 的分块响应；精确核对首块已接受、第二块计入 received、第三块不消费、原 intent/unknown/完整预留与零后续请求。它不证明墙钟上界。两个 cleanup 夹具保留入口业务 3 秒和清理 1 秒，直接被动核对首次 cleanup 及所有 rm/ps 的同一 D，并采用模块已有 0.3 秒回收/结算余量；同时保留业务+清理+余量的外层保护、实际进程 EOF 和持久终态/fence。旧总调用 <1.8 隐含准备必小于 0.8 秒，不能单独代表清理期限合同；新断言没有延长产品窗口或改变完整 Worker 的 90 秒目标。

冻结 v3 对 full04 的失败审计仍为 **invalid / exit 1 / final=null**，43 项最终覆盖计零；报告 SHA256 `a69e2a0149f92bd6c1ff9f0afca40613e52ab53acd9741601ca3bf48dc1a5e66`。诊断最大等待端点 60.382775375 秒不计最终通过。修改后的针对性、相关整合及同源码新 43 专项/全量尚待；不沿用 eb5dd1e 的成功专项跨源码配对。

### 相关整合的新 repair 失败与阶段合同

两项原修正的 `m4-deadline-contract-target-01` 为 **3 passed / exit 0**，pytest 12.21 秒、外层 41.569067 秒，日志 SHA256 `083a1f717b0384fee93ef7e18c1a69a3e15554f9cfd46fa639b55104b6cfcaa1`。随后四模块 `m4-deadline-contract-related-01` 无首错停止，**232 passed / 1 failed / exit 1**，pytest 132.63 秒、外层 159.850445 秒，日志 SHA256 `eda530d97a78b16a38767460d677a714108e962b1e2054049f26b33ff92e9d4f`。唯一新失败为 PG controller EOF 样例的独立 repair 返回 cleanup_unknown；前面的原 unknown/fence 断言通过。不能据此把失败写成原未知丢失或恢复权限已通过。

暂停全量准备后，重新核对该模块整个初始执行→未知持久化→普通拒绝→新 repair 调用链。原 EOF 业务 0.6 秒从真实 PG 登记和 mount/create/start 前开始，任意 !EOF 可来自尚未派发的 permit，不能证明真实逃逸 fork；repair 的独立 0.5 秒又包含真实登记及两至三次 CLI。原日志及残留 operations 只证明最终 ps 已进入，没有原 repair 的完整 D/EOF/数据库终态，因此原失败的具体未知子原因仍不能倒签确定。

首次私有 observer `m4-controller-repair-reproduce-01` 误读 ProcessPermit.sequence 而触发 AttributeError（实际字段 seq），**2 failed / exit 1**，日志 SHA256 `ad1a464db62f10387db26c91041497b8f04edaa048bb2aa0e2599dc98fb495e9`；它改变了异常路径，明确排除为诊断工具失败。原文件保留。修正 observer 后的原样 `m4-controller-repair-reproduce-02` **2 passed / exit 0**，外层 32.508264 秒，日志 SHA256 `7ee7eea5cafd2f96ae84e6d5945300555324717bfa1d87aa104e75f463f6697c`：真实 child.pid 和 start exit0/reaped/!EOF 已观察；原 rm/ps 因期限耗尽没有启动；新 repair 在 0.289 秒内完成 ps/rm/ps，原记录不变、全局阻止仍在。单例成功不覆盖整合失败。

`m4-controller-repair-delay-01` 明确在新 repair 首个真实 ps 返回后注入 0.35 秒有限延迟，原入口期限不变，后续 rm/ps 拒绝派发、repair 保持 unknown、全局阻止继续有效；原成功断言再次失败（exit 1），日志 SHA256 `4372557e41b69f3cd2dc88b3a9d6e501385419b97c67b59f5a06d425803b30b2`。这是受控因果诊断，不是原失败微观时序复原。

最小修正只针对该成功恢复样例：初始 EOF 采用入口一次性 3 秒并要求真实 child、精确 start PID/exit0/reaped/!EOF；ACK 要求实际 create 的严格坏标识原因。独立 repair 使用新的入口 3 秒，所有 ps/rm 共用同一截止，严格匹配实际残留所需的完整阶段、真实回收/EOF、新 terminal、原记录逐字段不变及全局阻止；普通无 repair 入口必须零派发。模块默认 0.5 秒及专门清理到期测试不改，生产代码、原未知、产品预算和 90 秒完整目标不改。此前仅修已报错模块的阶段前提不足，故本次同时复核该模块全部同类调用，不按名字将所有失败归为测试问题。新 targeted 与模块结果待实际收据。

repair 新定点 `m4-controller-repair-target-01` **2 passed / exit 0**，pytest 6.08 秒、外层 36.266119 秒，日志 SHA256 `5f7b561e68a4d59d4b450f8611d0b4e72eeb6f875d97a388f4d030485d5c6ec9`；完整 controller 模块 `m4-controller-repair-module-01` **43 passed / exit 0**，pytest 17.41 秒、外层 46.892837 秒，日志 SHA256 `af279eb99787872d351dd97b5a75c559f4da0bd9b5960941403962f58d630dbe`。所有实际输入前后不变、无 watchdog，绑定 eb5dd1e 加收据捕获的三份测试修改，不称旧干净 HEAD 的新验收。

三个测试文件已固定为新候选 `d65920fe17a402fb20887657741ce3cd5753bfdb`，生产源码不变。新 `m4-normal43-specialized-03` 沿用原 39 nodeid/43 配置命令与系统盘 TMPDIR，唯一执行中；随后需新无筛选完整全量，不能用旧专项跨源码配对。环境收据 `m4-final-environment-05.json` SHA256 `3bdf654f4ec68db23d1aa8c65bfc0f7eaad9700d4773deb4927ac1b5d68cb875` 在专项执行中采集，明确包含该轮唯一 test PG，最终空库存须另行核对。首次环境采集使用系统 Python 不支持 datetime.UTC 而在写收据前失败，已改用项目 Python；未影响测试、源码或原验收目标。


`m4-normal43-specialized-03` 随后在一次性 PostgreSQL 60 秒启动准备处退出 **1**，外层 63.132837 秒、watchdog=false、176 输入不变，**尚未进入 pytest，43 配置均未执行**。日志 SHA256 `ff7b3a0372cf84f623a8ef3e73025bf3c7a0fd8cfd005ee6a5b49c1b17a4d9d7`。没有据此增加窗口或更改源码；父先独占运行 `m4-pg-startup-diagnostic-02`，用相同镜像/真实 TCP SELECT 1，在明确的诊断窗口内保留启动日志/状态，再决定环境恢复。诊断不计产品验收。

环境诊断02用相同镜像和 TCP SELECT 1 于 28.563608 秒成功，保存日志显示 initdb 同步约 20 秒、bootstrap checkpoint sync 5.873 秒；VM 可用内存约 6.3 GiB、磁盘占用 12%、OOMKilled=false。诊断结束后精确删除自身容器、库存为空，收据 SHA256 `1c3df33a7b1aa276eb7e168d1fdc2455710d8182873bd9c58456370ef592effc`。这些是新诊断实证；原专项03未保存删除前的 PG 日志，不能把它的具体延迟来源倒签确定。保持候选和 60 秒准备窗口不变，环境核对后唯一启动新 `m4-normal43-specialized-04`，不重复仍运行的检查。

### d65920f 新专项通过、无筛选 full05 开始

`m4-normal43-specialized-04` 完整结束：**39 passed / 2 warnings / 0 skipped / exit 0**，覆盖冻结 43 个正常配置，pytest 1160.39 秒、外层 1189.759291959 秒；176 个非 Markdown 输入不变、无 watchdog，收据 SHA256 `5a0d5a4f6e9f73feef83390942b647755818d4cadfdb5a16a593a87ad0c0cc74`、日志 SHA256 `890e750f99bf4345edcb8ad0401cb53cfd9d406077c1c458237ff75c4a1aadef`。严格时间线审计单独执行，专项成功尚不关闭最终条件。

同一候选的 `m4-closure-ruff-05` 与 `m4-closure-web-05` 均 exit 0、176 输入不变。`m4-before-full05-source-resource-audit.json` 逐输入核对当前字节与 d65920f Git blob，三份日志 SHA 均匹配；实际 Run/test 标签库存为空、无活动测试进程。随后唯一启动 `m4-closure-full-05`：`env TMPDIR=/private/tmp/doonex-m4-verification-20261114 .venv/bin/python scripts/test_local.py -s -vv -rs`，外层 7200 秒 watchdog，无筛选、无首错停止；此处尚未终态，不能沿用旧全量或宣称通过。源码保持固定，本轮没有真实模型调用或常驻服务升级。

专项04的冻结 v3 独立审计为 **pending / exit 2 / invalid=[] / 恰好 43 final pending / final=null**：43 配置、39 唯一 PASSED 节点、176 输入和原 90 秒目标核对有效，没有源码差异豁免；最大 raw 区间为 `[60.369906583, 60.402813250]` 秒，来自 Responses 原 60 秒普通前台样本。原 8 轮失败加 full04 与专项03准备失败共 10 项哈希排除均验证，正常覆盖计零。新 manifest `../m4-wait-independent-audit/same_source_manifest-d65920f-specialized-04.json` SHA256 `151aeb436ffd30b26f465a487c766fb3fd653ae6f8d618a2ac038aa0f567c2f9`；专项审计 `audit-same-source-d65920f-specialized04-01.json` SHA256 `fef2cb64d93bb1ff4919d8f20c0733693ba748e9994a68523cf0adbb669ea40f`。尚待同源码 full05 终态配对，不能把 pending 解释为最终通过。

### full05 终态失败：Git 超时回收样例未到 Popen

`m4-closure-full-05` 无筛选完整结束：**2611 passed / 2 failed / 3 warnings / 0 skipped / exit 1**，pytest 6185.75 秒、外层 6216.471921708 秒；176 输入不变、无 watchdog，绑定 d65920f。收据 SHA256 `5f695b6016b6fb405707b484b73c1a4b1363c02924e954185f40fec30e21b984`，日志 SHA256 `2fcf900a16b2f2a74cf3fb870109f90ef5746edd253c1a35c1e7f95fbc383dfb`。原 Discovery、stalled-ps 及 PG repair 修正均通过本轮；新的两项失败不能据此前通过忽略，等待矩阵正常输出仅为失败轮诊断，不能形成最终通过。结束后的实际 Run/test 标签库存为空，无活动测试进程。

两项失败均为 `test_git_timeout_reaps_child_and_preserves_outer_marker[False/True]`。原 0.1 秒 deadline 从真实 PG reserve/launch 之前开始；False 在 Operation.launch 的持久 transition 与 live 检查后（controller_operations.py:1071）拒绝，True 在 before_process 的 PG 行/全局阻止与 live 复检后（:1229）拒绝，均为 RegistryError(deadline)，没有到 Popen。该样例期待 WorkspaceError/ExecutionDeadlineExceeded 并检查子进程回收，但本轮没有进入其目标阶段。尚未据此修改产品异常或放宽期限；父先原样被动观察全部登记/准入/Popen/持久结果，另做有限延迟因果诊断。此前逐模块修正未充分覆盖同类阶段前提，现暂停追加补丁，独立横审所有相关短期限测试，明确真实阶段、拒绝与回收合同后再选择最小修正。

### full05 的阶段因果诊断与横向合同核对

原样被动诊断 `m4-git-timeout-original-01` 为 **2 passed / exit 0**（外层28.265816秒，收据SHA256 `ab1f0739e9d157149138cb6c9dcf7bfaa91e6739755111bce6ee2fc4b38c46c0`）。两次真实Popen到达时原0.1秒期限仅剩约2毫秒和7.5毫秒；真实子进程exit -9、两管道关闭、持久terminal且fence=false。本轮成功不覆盖full05失败，也未证明Python用户代码已开始。

`m4-git-timeout-reserve-delay-01` 在真实reserve完成后显式注入0.15秒，两个原样断言均 **failed / exit 1**，外层34.711061秒、176输入不变、无watchdog；收据SHA256 `0b85894f46d68d6e31cc87684d8735f97f3913a689e6f4a6a7af18ac7896b1ff`，日志SHA256 `5f5046c95326b192becb1f896f52ccc19ae0a0864e27caca0b6ec9474bfad737`。两例实际launch前拒绝，零Popen，持久reserved、attempted_processes=0、fence=true。此为因果诊断，不倒签原full05的微观时序。原始阶段与持久行均保留在同目录的 `m4-git-phases-001-{original,delay}-{false,true}.json`。

full05冻结v3失败审计为invalid/exit1，唯一invalid为final-full非零退出，final=null、43 final pending；报告SHA256 `d34b9728964f23410949bc7be7700a6174097d9b114d5fa1f0813e8192aa298c`。另行151节点审计确认151个精确节点均各一次PASSED、176输入及14份覆盖源码绑定，但仍因整轮两项失败为invalid/exit1，报告SHA256 `d59ead8c9ad3318740af99ece469eee1594c55035b85da95278a2cf43490190d`。局部覆盖不提升为全量通过。

此前按报错模块逐项修正，没有充分横查「极短期限经过准备后仍假定目标阶段必达」的共同前提。本次先核对完整reserve→launch→before_process→Popen→回收→持久结算，再横审Git继承管道、配置FIFO、Sandbox控制、Responses helper和锁等待。静态发现不冒充新运行失败；只修与退出条件直接有关的阶段证据，不改产品代码、模型配置、预算或90秒目标。回收成功样例采用入口一次性有限余量并明确READY/实际PID/EOF/原D/持久终态；启动前期限拒绝单列，零派发和持久阻止保留。针对性及相关整合尚待实际收据，不能仅凭接线关闭。

Git阶段修正的 `m4-git-phase-target-01` 为 **8 passed / exit0**，外层57.432238秒、日志SHA256 `eafcdd5edb9c4ba067112abf2d28d3c17f6f14a87a6a17f4c591abf740b8c877`。随后Workspace/Controller/Git filters/Session store/Execution deadline五个完整模块 `m4-phase-related-01` **180 passed / 2 warnings / 0 skipped / exit0**，pytest106.58秒、外层133.512466秒，日志SHA256 `d48416b333fd5b97bfb24390d1a221b9168189907a831552e013adaa7cbff93e`。两轮176输入前后不变，无watchdog，绑定d65920f加各收据捕获的测试改动，不能冒充干净d65920f结果。结束后实际Run/test容器库存为空。

Git回收样例入口3秒涵盖真实PG登记，必须收到子进程READY、实际exit -9/reap/EOF、原D逐值保持及持久terminal；继承管道须有实际child PID、leader自然exit0、!EOF及持久unknown/fence。Git配置FIFO以真实非阻塞writer握手证明reader打开，原固定3秒localD内停止，只有config进程、无后续status，持久终态及真实回收匹配。PG launch和before_process两阶段各以原逻辑截止验证真实事务后过期、零Popen、持久阻止。SDK/文件锁样例使用一次入口3秒并实际观察acquire/flock竞争，保留原D和零模型/工具回调；不把准备阶段到期算作锁等待。以上仅修组件测试阶段合同，不提高产品任何期限或完整Worker90秒目标。

### 横向审查后相关整合的容量成功样例失败

五模块修改已提交 `8139763`，Sandbox七个阶段节点修正整合为 `42bc90a`。`m4-sandbox-phase-target-01` **7 passed / exit0**，pytest10.62秒、外层40.748369秒，日志SHA256 `0fbd2a11a907a7e829568e00de3e4b2305564e8d7a6d40a69157c9c129d7ca64`。三个完整Sandbox模块 `m4-sandbox-phase-related-01` **89 passed / 1 failed / exit1**，pytest85.42秒、外层115.940318秒、176输入不变、无watchdog，日志SHA256 `34e10fccb7fef06542f901a875c735e89a6c60619342e87bc597f91f0859f192`。新七项均通过，但原完整容器列表成功样例返回ps timeout，不能据局部通过忽略此失败。

保持原源码和原断言，`m4-probe-capacity-original-01` 原样被动复现 **1 failed / exit1**，外层32.096494秒，日志SHA256 `de2a77aec648a31db3219801e85a6e81cba903c95e847fbcd929f31121840385`。阶段收据 `m4-probe-capacity-stages-original-01.json`：原D=484284.317017541，真实reserve约44.9ms、launch45ms、before_process43.5ms；实际ps开始于484284.228360750，process在484284.262262583返回timeout、exit0/reap/EOF，持久结算在484284.342348完成，unknown/fence=true。Supervisor从原D内保留部分回收窗口，没有刷新D。此例已到真实ps，不能混称未Popen。

此前横审主要关注故障样例的阶段到达，遗漏同一极短夹具下成功样例的性能假设；现重新分类整个模块默认0.35秒的调用方，区分容量/格式/正常清理成功合同与明确窗口/到期拒绝合同。没有统一增加产品期限，也没有修改评分或清除unknown。具体修正及新结果待后续实际收据。

Responses单文件阶段合同候选整合为 `b282b17`，子工作区16个离线节点通过，仅为无PG/模型的helper阶段证据。父首次相关命令 `m4-responses-phase-related-01` 误写不存在的 `tests/test_responses_recovery.py`，**exit4、未执行测试**，外层28.277763秒，日志SHA256 `0e2a6e1da013f5c5d4d34cca374ca699c3f84b04a1f6b1db538161f4cb461f58`；这是父测试命令错误，不归因产品。核对文件清单后使用实际 `tests/test_sdk_responses_recovery.py` 启动独立related02，原错误日志保留。

正确的 `m4-responses-phase-related-02` 完整执行三个相关模块，**78 passed / exit0**，pytest47.86秒、外层77.914926秒，日志SHA256 `e1daa17cc92372f131c8a6890f66556a9908a21d39cb2405e507af11a5328065`；绑定b282b17、176输入前后不变、无watchdog，结束后Run/test容器库存为空。独立只读审查未发现实质问题；独立EOF只在真实waitpid之后、真实close前对该测试输出管道进行有界非阻塞观察，不修改产品接受判定。synthetic回调不是PG持久化证据，相关成功不替代新完整配对。

### 普通完成夹具与显式短窗口分离

新的两文件测试候选整合为 `25c221e`。默认3秒仅用于普通完成/格式/容量的有限测试准备与清理；4个原0.35秒、ACK原0.5秒、escaped原0.2秒、两个1秒及两个0.3秒专门窗口全部局部显式保留，对应原断言和数字逐AST核对不变，PG wait仍使用原逻辑1秒。只有三个标准库合成CLI的shebang加 `-S`，不加载共享venv的site初始化；生产mount/helper、真实Docker和产品15秒清理默认未改，不能称真实Docker性能改善。

完整列表必须观察实际完整stdout/exit0/reap/EOF、唯一ps原D与持久terminal/remaining64/non-fenced；错误和超限列表必须观测精确字节或截断/错误、unknown/fence，repair使用独立一次入口D且原unknown逐项保留。最终缺席核查须实际ps正常返回非空字节；取消须实际create/rm/ps和当前snapshot absence，daemon未知不能清除。独立只读审查未发现实质问题。

`m4-sandbox-fixture-target-01` **16 passed / exit0**，pytest28.76秒、外层56.225976秒，日志SHA256 `9abe107fd14b1275730ace70c5cc5e64a9b5ee7f751559fea4854d490ae1fc80`；绑定25c221e、176输入不变，无watchdog。随后三个完整Sandbox模块的related01尚在执行；原89/1失败和原样失败均保留，不提前关闭最终条件。


三个完整Sandbox模块 `m4-sandbox-fixture-related-01` 已终态：**90 passed / exit0**，pytest89.43秒、外层120.974882秒；收据SHA256 `70320ea8b4f635f62c250aecd46d9369100a20655ba5d1d65239d7612c1a3632`、日志SHA256 `55df907f824d6fe537b3a7a792f74f750cbcf52e57849efc49cd740348ea3537`。176输入前后不变、无watchdog；原失败保留。

### 固定25c221e的新完整配对（执行中）

最终候选为 `25c221e3f9cca942341cc3c3678a08bc3d94af26`。同源码 `m4-closure-ruff-06`、`m4-closure-web-06` 均exit0；收据SHA256分别为 `aea627e51b8b9ddb14b73b8e2ddd2da94ea9a7fafe05b347927597fa04ebe407`、`8dc60dbd95802344a353d6d1141b09ba5b042c478b015da203b55528ccc05603`。`m4-before-specialized05-source-resource-audit.json` 核对16/90/Ruff/构建收据与日志SHA、176项当前字节和Git blob一致，启动前Run/test库存为空、无活动检查。环境记录 `m4-final-environment-06.json` SHA256 `894c41e3148af62250628d3afb434f2019540e1c61d9aab5b9701f8923f94249`，继续使用系统盘TMPDIR及原固定依赖。

随后唯一启动 `m4-normal43-specialized-05`，完整复用专项04的39节点/43配置原参数及3600秒外层watchdog。此处尚无终态，不以进度标记视为通过。通过后须执行同源码、无筛选、无首错停止的 `m4-closure-full-06`，并完成冻结v3和151精确节点独立审计。原90秒目标、全部容量和排除规则不变；本轮零真实模型调用、未升级常驻服务。


专项05完整终态：**39 passed / 2 warnings / 0 skipped / exit0**，pytest1154.44秒、外层1185.087606042秒，176输入不变、无watchdog。收据SHA256 `a49ebe6283628cda37af6e2113ffc24a51bbf009e8db008a7ca99a6385133967`，日志SHA256 `e13d1d99c3552907074cf52df8965db7923f5bbe25bbcd2bab566f3b2239958c`。`m4-before-full06-source-resource-audit.json` 核对专项/Ruff/构建收据、日志SHA及176输入与25c221e一致，实际Run/test容器为空且无活动检查。

随后唯一启动 `m4-closure-full-06`：`env TMPDIR=/private/tmp/doonex-m4-verification-20261114 .venv/bin/python scripts/test_local.py -s -vv -rs`，7200秒外层watchdog，无筛选、无首错停止。当前尚无全量终态；专项与全量必须严格同源码配对，旧失败及全部未知保留，不能提前关闭等待退出条件。


专项05冻结v3独立审计为 **pending / exit2 / invalid=[] / 43 final pending**，43配置、39唯一节点、176输入和90秒原目标核对有效；最大raw等待区间为 `[60.723724916, 60.763902875]` 秒，来自原60秒Responses样本。原10项排除完整保留，追加full05为第11项，均计零正常覆盖；无源码差异豁免。新manifest `../m4-wait-independent-audit/same_source_manifest-25c221e-specialized-05.json` SHA256 `c12a86cf5f954f278c75ccb9b6c8121224aac933de3b54f1137abf6804b7f87c`，审计 `audit-same-source-25c221e-specialized05-01.json` SHA256 `69221c576c8b6424b3273791061bc5219954080b018b03a324d5aaae796adc20`。全量06尚在执行，pending不是最终通过。


<a id="m4-final-closure"></a>

### 最终收尾：25c221e 同源码配对与完整整合通过（UTC 2026-11-14）

以上“执行中”和 pending 均为当时快照。本次最终非 Markdown 源码固定为 `25c221e3f9cca942341cc3c3678a08bc3d94af26`，176 项输入在专项05、full06、Ruff06、构建06及当前文件/Git blob 间逐项一致；没有源码差异豁免。后续收尾仅更新 Markdown，不将旧 2126 项、分轮组件或失败轮中的正常输出作为本次最终结果。

| 检查 | 实际终态 | pytest / 外层耗时 |
| --- | --- | --- |
| `m4-normal43-specialized-05` | 39 passed、2 warnings、0 skipped、exit 0；冻结43配置完整执行 | 1154.44 / 1185.087606042 秒 |
| `m4-closure-full-06` | 2614 passed、3 warnings、0 failed、0 skipped、exit 0；无筛选、无首错停止 | 6360.20 / 6390.693510250 秒 |
| `m4-closure-ruff-06` | All checks passed、exit 0 | 外层0.083017542秒 |
| `m4-closure-web-06` | 前端生产构建成功、exit 0 | 外层0.881449833秒 |

四项均未触发 watchdog，输入前后不变。full06 于 UTC `2026-11-14T21:15:37.516047+00:00` 开始，采用一次性 PostgreSQL、真实 Docker、本地合成模型和实际 Worker.run；本轮零真实模型调用，不修改产品模型、凭据、预算。收据记录 `PYTHONPATH=apps/server:tests`，共享解释器/依赖不变。实际执行命令如下；这是已完成证据，不是待重放命令：

```bash
env TMPDIR=/private/tmp/doonex-m4-verification-20261114 .venv/bin/python scripts/test_local.py -s -vv -rs 'tests/test_worker_wait_capacity.py::test_actual_worker_complete_yielded_census[full]' 'tests/test_worker_wait_capacity.py::test_actual_worker_near_git_export_capacity[raw_bytes_near]' 'tests/test_worker_wait_capacity.py::test_actual_worker_near_git_export_capacity[raw_entries_near]' 'tests/test_worker_wait_capacity.py::test_actual_worker_near_git_export_capacity[retained_bytes_near]' 'tests/test_worker_wait_capacity.py::test_actual_worker_near_git_export_capacity[retained_entries_near]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[arena-model]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[arena-preparation]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[arena-shell]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[arena-verifier]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[generation-model]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[generation-preparation]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[generation-shell]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[generation-verifier]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[ordinary-model]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[ordinary-preparation]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[ordinary-shell]' 'tests/test_worker_wait_matrix.py::test_actual_worker_wait_timeline[ordinary-verifier]' 'tests/test_worker_wait_matrix.py::test_real_sixty_second_action_yields_after_long_remaining_work[shell]' 'tests/test_worker_wait_matrix.py::test_real_sixty_second_action_yields_after_long_remaining_work[verifier]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[arena-model]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[arena-preparation]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[arena-shell]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[arena-verifier]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[generation-model]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[generation-preparation]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[generation-shell]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[generation-verifier]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[ordinary-model]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[ordinary-preparation]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[ordinary-shell]' 'tests/test_worker_wait_responses.py::test_actual_isolated_worker_wait_timeline[ordinary-verifier]' 'tests/test_worker_wait_responses.py::test_isolated_request_completes_near_original_deadline[arena]' 'tests/test_worker_wait_responses.py::test_isolated_request_completes_near_original_deadline[generation]' 'tests/test_worker_wait_responses.py::test_isolated_request_completes_near_original_deadline[ordinary]' tests/test_worker_wait_responses.py::test_isolated_sixty_second_request_completes_near_original_deadline 'tests/test_worker_wait_scale.py::test_actual_worker_scale_preserves_accounting_to_foreground_admission[cold_start]' 'tests/test_worker_wait_scale.py::test_actual_worker_scale_preserves_accounting_to_foreground_admission[warm_ready]' 'tests/test_worker_wait_session_capacity.py::test_near_capacity_active_branch_receipt_and_actual_cold_worker_restore[bytes]' 'tests/test_worker_wait_session_capacity.py::test_near_capacity_active_branch_receipt_and_actual_cold_worker_restore[small]'
env TMPDIR=/private/tmp/doonex-m4-verification-20261114 .venv/bin/python scripts/test_local.py -s -vv -rs
.venv/bin/ruff check apps/server/doonex tests scripts
pnpm --dir apps/web build
```

全部原始文件位于私有 `~/Library/Application Support/DooNex/verification/`。下表路径相对该目录，产品仓库只记录脱敏结果及哈希：

| 收据 | SHA256 | 对应日志SHA256 |
| --- | --- | --- |
| `resume-20261113/m4-normal43-specialized-05.json` | `a49ebe6283628cda37af6e2113ffc24a51bbf009e8db008a7ca99a6385133967` | `e13d1d99c3552907074cf52df8965db7923f5bbe25bbcd2bab566f3b2239958c` |
| `resume-20261113/m4-closure-full-06.json` | `a9f657c7e949c859948d862bf25dd109c52c962c666eeec094d658efbf004cf5` | `40cd20e8d5a8b758a18ab45c59bd9f4c17faf89e3cab6ed08293108bea29151b` |
| `resume-20261113/m4-closure-ruff-06.json` | `aea627e51b8b9ddb14b73b8e2ddd2da94ea9a7fafe05b347927597fa04ebe407` | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` |
| `resume-20261113/m4-closure-web-06.json` | `8dc60dbd95802344a353d6d1141b09ba5b042c478b015da203b55528ccc05603` | `f0c0894d8768b0dff903ca173a2a543744e0f4ac8f350e8f7d0ecc93b7e30c64` |

冻结 v3 审计 `m4-wait-independent-audit/audit-same-source-25c221e-full06-passed-01.json` 为 **passed / exit0 / invalid=[] / pending=[]**，SHA256 `81e1181efa8c7b188926c7100700580b6a8dcbe263bfefa161c5606e562ca05c`。专项及全量各43配置/39唯一节点、176输入与runner完全一致，changed_files=[]、source_delta_exceptions=0；原90秒目标及审计规则未改。manifest `same_source_manifest-25c221e-specialized-05.json` SHA256 `c12a86cf5f954f278c75ccb9b6c8121224aac933de3b54f1137abf6804b7f87c`；原专项pending报告保留，不能改写为当时已通过。

按同一 monotonic 时间线的入队提交前后到首次实际准入计算，专项最大 raw 区间为 **[60.72372491599526, 60.76390287495451] 秒**（log146/support145），全量为 **[60.38583641703008, 60.41734975000145] 秒**（log2982/support2981），均为原60秒Responses配置。较大端点用于90秒判定，不用局部timeout或Worker.execute入口替代。冻结的11项历史排除逐项核对，每项最终正常覆盖计零；全部失败、未知与预留保留。健康依赖、独立容量配置和前置队列的边界以[等待验收](M4_WAIT_ACCEPTANCE.md)为准，不外推所有容量同时最大或宿主/文件系统失联时按时成功。

`m4-worker-wait-scale/WORKER_EXIT_151_FINAL06_AUDIT_001.json` 为 **passed_node_coverage / invalid=[]**，审计进程exit0，SHA256 `a73e944a3142aceefff677acfa2161105bd5e53a11e1b0fe3b56f0d68d6b1ed9`。151个精确节点各恰好一次PASSED，正常优先、容量拒绝、暂停/取消/清理/崩溃、期限/未知拒绝及账目保持五个组分别40/19/27/71/84，组间重叠，不能相加。全量collected/start/terminal/summary均2614；176输入及14份覆盖源码匹配。审计器 `audit_worker_exit_151_003.py` SHA256 `b0b8dcf65534c7e9c9da1e310690127913962921197759088a225922b7aa2f0a`，准备记录 `WORKER_EXIT_151_PREPARATION_003.json` SHA256 `9d2c48a46c8f59b725972c356037518c913ffdec0d53b4d6fe4ccb6207188e01`；规则正文未变，仅绑定最终候选，64个catalog函数AST保持。

故障证据的具体边界如下：

- 三条实际Worker.run SIGKILL marker分别为checkpoint_before_save→interrupted（log2807）、checkpoint_after_commit→yielded（log2827）、cleanup_intent_committed→interrupted（log2845）；均exit=-9、synthetic、latency_sample=false，原request/usage/first_started/action/session与父账目保持。这三个杀点没有live controller operation，不能泛化为任意Popen在途kill。cleanup intent样例保留unknown_calls=1及reserved_tokens>0，前台仍queued、零模型调用、禁止stop_containers重放且global fence；另外两例允许已排队前台完成，后台不重放。
- 在途shell另由 `tests/test_worker_process.py::test_killed_worker_preserves_changes_and_blocks_unresolved_controller`（log2652–2666）验证：真实Docker写progress=x并sleep，捕获容器及pending shell/sandbox_execute后kill；重启保留controller行、账本、首次时钟与session，零新模型调用，两种普通resume确认均400，重启仍fence。该节点断言非零exit，没有单独断言=-9，不作为第四条SIGKILL marker。
- 普通provider unknown样例 `near_capacity_unknown_model_reservation`（log3052–3053）保留合成TimeoutError、calls=unknown_calls=1、reserved_tokens>0与usage=null，重启不重放原SDK，独立前台可完成。普通provider未知不等于全局controller fence；控制未知与期限到期停止新派发由各自精确节点验证。

最终资源/源码审计 `resume-20261113/m4-final06-source-resource-audit.json` SHA256 `761804214be9a5b432f10eb2be11810a8ee4ea720bddec2f8d49c90ddb33c51b`：四份收据及日志哈希一致、176当前字节和Git blob匹配，无活动测试进程，Run/test标签容器均空。一次性PG带 `doonex.test=true`，已不存在；test_local.py finally只回收自己的容器。库存核对不代替逐controller的reap/EOF证明，后者来自具体测试断言和marker。审计脚本SHA256 `b6b2b3d97bbd4d47c1446da6673551b18de16a90e10c55e536d6c604c9ef22ef`，共同runner SHA256 `aac47930fdc5b948097afe126f1cb74a9abf5dd3d4b31c09e64e09d815dc7fa5`。

**本轮关闭有限后台首片的执行/清理恢复整合、共同期限与前台优先、条件内完整等待及最终回归条件。** 真实模型和浏览器证据仍绑定 `b462502`，本轮没有新真实模型或浏览器点击验收。循环生成/评测、重复批次、自动推广、生成版本质量改善未完成，不宣称整个M4–M5完成。API/worker仍未加载、plist保留旧M5 `f7a1161`、社区关闭，服务未升级；升级另按固定源码、核查与回退流程处理。main推进及远程SHA以[交接页](DEVELOPMENT_HANDOFF.md#main-与历史分支)的实际记录为准。
