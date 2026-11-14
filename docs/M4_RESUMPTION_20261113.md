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
