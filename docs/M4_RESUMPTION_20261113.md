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
