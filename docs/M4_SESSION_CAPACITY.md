# M4：SDK 会话容量切片

更新：2026-11-14（纽约；收据使用 UTC）。应用提交 `42688c7` 在 SDK 1.60.1 的正式 `file_store=` 接口注入有限、无缓存的本地存储；后续 Responses 修正不改变这部分源码。它限制会话的工作量，**不代表完整前台等待上界通过**。当前整合验证与真实调用见[续接记录](M4_RESUMPTION_20261113.md)。

## 容量合同

| 对象 | 当前限制 |
| --- | --- |
| `base_state.json` | 16 MiB，保持原恢复边界 |
| 单个 SDK 事件 | 16 MiB UTF-8 |
| 其他单文件，包括 sidecar | 16 KiB |
| 会话逻辑文件总量 | 64 MiB |
| 旧文件与新临时写入并存峰值 | 80 MiB |
| 事件数 / 全部目录项 | 1024 / 2048；目录、未知文件、sidecar 均计数 |
| 路径 / 深度 | UTF-8 1024 bytes / 8 层 |
| 持久 JSON | 解析前检查 32 层嵌套、131072 个结构标记 |
| 序列化源对象 | 32 层、65536 个节点，单容器最多 4096 项，源字符串及标量累计 16 MiB |
| canonical 摘要输入 | 128 MiB；保留原 `sort_keys`、`ensure_ascii` 与分隔符语义，分块哈希 |
| 单次模型返回的工具批次 | 最多 16 个，超出时在任何工具派发前拒绝 |

源容量计数用于限制序列化前的工作量，不等于精确 JSON 大小或进程内存配额；写入后另按实际 UTF-8 字节检查。它覆盖 Agent、统计、消息、事件和状态字段。固定工具的四种 schema 类单独核验；恢复前拒绝未启用的 hook、security analyzer、secret registry 来源、非默认确认策略和未知状态扩展。既有调用、token、时长、模型输入及工具输出上限继续生效。

现有 2 MiB shell 原始输出没有缩减。实际 `ShellObservation` / `ObservationEvent` 定标中，控制字符经嵌套 JSON 后约 14 MiB，因此事件上限采用 16 MiB。ASCII、控制字符、无效 UTF-8 替换与非 ASCII 的完整输出路径都有专项检查。

## 失败与恢复

SDK 构造前按项、有界读取全部会话文件，不通过缓存跳过旧文件。目录使用 descriptor 与 no-follow 打开，文件要求普通单链接文件，检查 inode、大小及时间戳稳定性；目录扫描前后检查变化。构造后、模型/工具准入及分支读取前复核目录清单。

任何已观察到的存储异常锁存，随后写入和准入均拒绝。SDK 的 EventLog 会吞掉普通扫描或 marker 异常，这里不把它解释为空历史；真实 SDK 故障注入检查确认旧字节不被覆盖、没有模型/工具准入，也不发布安全让出回执。部分写入保留临时文件作为证据，冷恢复不会静默忽略或删除它。超限不修剪历史、不新建会话绕过限制、不清零账本。

存储保持 SDK 的事件命名、顺序和单文件原子替换，并对文件和替换目录执行 fsync。事件锁沿用永久文件与 flock；显式提供 monotonic deadline 时，读写检查及锁等待使用其剩余时间。当前 worker 已将原 Run 的剩余 monotonic deadline 传入 FileStore、恢复前读取、canonical hash 及 SDK 全局锁；锁等待取原期限剩余时间。未提供 deadline 的既有调用仍保留默认限制。Skill/任务集、交付证据和调度接线及完整等待矩阵尚未完成，不能把本地持久化期限当作整链上界。

SDK 原生 close 保留。即使 close 抛出异常，嵌套 finally 仍清除 shell、调度及存储回调引用。失败、未知结果和未匹配 SDK 动作的原有恢复规则继续适用。

## 实际检查与边界

定向命令：

```bash
PYTHONPATH=apps/server:tests .venv/bin/python -m pytest -q \
  tests/test_session_store.py tests/test_sdk.py tests/test_sdk_scheduling.py \
  tests/test_sdk_skills.py tests/test_generation_sdk.py tests/test_sdk_unknown_tools.py
```

最终整合 `b462502` 全量 1826 passed / 3 warnings / exit 0，完整输入绑定见续接记录。本轮专项同时与 Responses、冷恢复和逐工具拒绝重放合同联合执行；完整命令、输入摘要、退出码及中间失败在私有 `verification/resume-20261113/combined-*.json/.log`。首轮接入没有识别 SDK 工具 schema 类，随后又暴露 JSON 类型失败分类被锁存覆盖，均已修正，原拒绝后续工具的断言保留。close 故障夹具补充原生资源清理，避免测试自己的 atexit 残留。

近容量样本使用真实 SDK EventLog：1024 个事件、1027 个目录项、63,362,935 bytes（总上限 67,108,864 bytes）。本机追加约 1.02 秒，冷 inventory 加 SDK 读取约 5.09 秒，canonical 哈希约 0.11 秒；第 1025 个事件拒绝且原事件摘要不变。证据和两份前后相同源码绑定在私有 `session-capacity-benchmark.json`、`session-benchmark-source-binding.json`。这是一次本机样本，不是 `Worker.run` 延迟或任意宿主的数值上界。

尚未覆盖：整个 worker 的共同剩余期限、Docker 管道与清理、Git/export/快照/压缩/验证复制的整链等待，以及普通、Arena、generation 前台初始化矩阵。健康本地 IO 下容量限制工作量；文件系统失联、阻塞的系统调用和宿主磁盘配额不由这片保证。下一步仍按[等待控制计划](M4_WAIT_CONTROL_PLAN.md)推进。
