# M4：显式 Responses 传输子进程

更新：2026-11-06（纽约）。本切片实现 `isolated_direct_v1` 可选模式，沿用 SDK 1.60.1 与现有预算账本；本机整合验证 1650 项通过。**尚未验证真实网关，也没有升级常驻服务。** 正常前台等待上界与完整真实后台矩阵仍是独立未通过项，见 [M4 记录](M4_IMPLEMENTATION.md) 和 [等待控制计划](M4_WAIT_CONTROL_PLAN.md)。

## 选择与使用

「模型设置」的 OpenAI Responses 配置可选择「独立传输进程（直连）」，对应 `responses_transport: "isolated_direct_v1"`。没有该字段的旧配置继续使用 `direct`；默认字段不进入旧配置的规范化摘要，避免改写既有配置档版本和冻结运行绑定。每个 Run 仍冻结所选配置，不自动切换历史运行。

独立模式只接受明确的 HTTPS 地址或已经授权的私网 HTTP；地址留空时明确使用 `https://api.openai.com/v1`。不支持代理、自定义 CA、环境路由覆盖、流式输出、Chat 回退或自动重试；检测到相关环境配置会拒绝启动，不静默忽略。这里的 OpenAI 兼容性仍须对实际供应商、模型和接口分别验证。

运行方式与现有 [安装说明](../README.md#本机安装与启动) 相同，不新增常驻服务。私有服务配置和数据库使用现有初始化流程；未决传输恢复入口位于「模型设置 → 模型传输恢复核查」。验证本切片可以运行：

```bash
PYTHONPATH=apps/server:tests .venv/bin/python scripts/test_local.py
.venv/bin/ruff check apps/server/doonex tests scripts
pnpm --dir apps/web build
```

显式 `PYTHONPATH` 保证共享虚拟环境使用当前 worktree 的应用和测试源码。测试启动一次性 PostgreSQL，含真实本机进程、回环 HTTP 和 Docker；不调用真实模型。

本轮环境为 Apple Silicon / macOS 27.0.1（26A434）、Python 3.12.16、SDK 1.60.1、Docker 客户端/服务端 29.8.2、pnpm 11.1.1；依赖与 PostgreSQL、任务镜像继续按仓库锁定版本。未由这些检查推断其他操作系统或硬件兼容性。

## 执行与证据合同

SDK 继续负责 Agent、会话、工具、消息转换和响应处理。DooNex 在锁定 SDK 的同步 `litellm_responses` 别名处插入一次调用上下文，将已预留的 action 绑定到一个 helper；上下文缺失、重复或嵌套派发、工具偏离授权、跳过 helper 或吞掉 helper 异常后返回均被拒绝。旧 `direct` 路径保留；独立模式失败不会回退。

helper 使用固定入口、`-I -S`、独立工作目录及最小环境；不加载项目代码、`.pth`、父进程的数据库或其他模型凭据。READY 与持久 ARM 完成后才经匿名管道发送当前请求密钥。严格 JSON 协议绑定 attempt、client request 和 nonce，拒绝任意 Python 对象。READY、ARM、terminal 帧上限分别为 4 KiB、1 MiB、2 MiB；最多一个终态，随后必须收到 EOF、确认正常退出并由原父进程回收。

直接 HTTP 传输关闭代理、重定向和重试，启用 TLS 验证，要求 identity 编码，并在 JSON 解析前限制响应正文为 1 MiB。响应头大小检查发生在 HTTPX/HTTPCore 已解析头部之后，不能声称限制了其底层分配。响应按锁定类型重建；有限错误协议保留实际 HTTP 状态、请求 ID 与阶段。控制器错误单独标注 `source=controller`，不伪造供应商 HTTP 异常。

绝对 monotonic deadline 取本次配置超时和 Run 剩余时间的较小值，覆盖 helper 启动、传输和 IPC 接收判定；子进程在第三方导入前安装期限信号。失败执行有限的 TERM、KILL 和 waitpid 清理，父进程退出管道检测提供补充。**这是传输子进程期限，不是整个 SDK 请求或前台等待上界。** 父端 SDK 准备、数据库 hook、序列化、返回后处理、持久化和关闭均在其外。回收证据也不表示远端请求或计费已经停止；即使本机进程已回收，迟到或不明结果仍保留未知用量。

## 持久阻止与人工核查

原模型 action 的 `transport_helper` 保存 helper 身份、状态和证据，不新增预算表。记录摘要由 PostgreSQL 的规范化 JSONB 表达式唯一计算；终态判定及局部索引使用同一严格表达式。坏结构、错误摘要、未知状态和缺失记录均阻止后续执行；Run 状态或普通动作确认不能清除此阻止。

helper lifetime lease 使用永久 inode。父子只关闭描述符，不显式 `LOCK_UN`，不删除或重新创建已有 lease；重启后不能根据旧 PID 杀进程，也不能宣称已 `reaped`。只有原父进程的真实 waitpid 回执可证明回收。

私有认证接口包括分页列表、单记录核查和显式解除：

| 接口 | 用途 |
| --- | --- |
| `GET /api/transport-helpers` | 按 action UUID 游标分页读取未决记录，每页最多 100 条 |
| `GET /api/transport-helpers/{action_id}` | 核对绑定和当前本机 lease |
| `POST /api/transport-helpers/{action_id}/resolve` | 原子复核记录版本、attempt、nonce、同 inode 与当前空闲锁后保存人工核查 |

解除要求没有活 worker 持有全局锁，并再次检查现场 lease。界面确认绑定确切 action 和记录摘要，刷新、切换或新操作清除确认；服务端继续执行独立核对。结果为 `operator_resolved`，不是 `reaped`，不修改原 Run 预算、不释放未知预留。之后仍须按原 Run/Arena 的恢复规则处理。

**当前可达限制**：预算 reserve 已提交而 helper register 尚未持久化时，如果父进程硬退出，会留下有传输标记但无 helper 记录的 action。系统保持全局阻止，核查显示 `record_missing`；由于没有 hash、nonce 和 lease 身份，当前 resolver 无法解除。不能凭当前空闲锁推断从未启动，也不能手改数据库跳过核查。后续须单独设计 reservation 与初始 helper 身份的原子绑定，当前不承诺从任意中断点恢复。

schema 初始化使用 `IF NOT EXISTS`。将来若修改终态谓词、字段或状态集合，必须显式迁移相应索引，不能以重复初始化代替迁移。

## 已取得的检查与未通过项

组件检查覆盖严格协议和 HTTP 限额、子进程期限与父进程退出、重复中断和描述符继承、Registry 真实文件锁和索引、SDK 单次预算上下文、全局阻止与私有恢复 API。固定 bootstrap 对本地 HTTP 的独立纵线 7 项通过；一次性 PostgreSQL、真实 SDK、helper 与 Docker 的组合 26 项通过，包含固定快照交付、429 保留未知以及准备失败的 `not_spawned`。这些结果不相加为完整整合测试总数。

浏览器用独立数据库和合成记录实际验证了模式选择与保存、未确认禁用、刷新清除旧确认、显式解除和反馈文案。解除前后 Run 完全相同，仍为 1 次未知调用、228 个预留 token；临时服务与数据库已清理。此处只有实际浏览器交互、可访问性状态和数据库证据，没有保存截图，不能称截图视觉验收。

独立审查发现并修复了 helper 调用未消费、吞异常后返回和界面跨记录复用确认的问题。首轮整合还暴露测试读取 events 结构错误，以及三个独立 SDK 会话的提示时间跨分钟；后者仅固定测试输入 `AgentContext.current_datetime`，保留完整 HTTP 请求体相等断言，不改预算时钟或评分条件。全部中间失败保留。

首轮全量为 **1648 passed、2 failed、3 warnings、741.67 秒**；两项旧容器故障测试均因新增统一阻止文案遗漏“终止状态不明”而失败。恢复准确诊断词后，相关容器、helper 和 API 组合 **12 passed、2 warnings、13.86 秒**，未修改原断言或阻止条件。最终应用代码提交为 `31d937f`，后续完整回归核验了该提交的相同源码字节。

最终完整回归为 **1650 passed、3 warnings、732.81 秒、退出 0，无跳过**。测试开始时 HEAD 为 `a350f25` 加接线改动，前后 132 份非 Markdown 输入摘要一致，并与 `31d937f` 提交逐项匹配；一次性 PostgreSQL 已确认清理。Ruff 全范围通过，已成功构建的 22 份前端输入与最终提交逐项一致。`full-02.json`、`full-02.log`、`verified-commit-01.json` 保存命令、环境绑定和核验收据，完整日志 SHA-256 为 `55c8f29a476a0c0dfe257e7344aa36d6985fbe9051a6170373969743fc8ad57c`。私有证据位于 `verification/m4-responses-transport/`，生命周期、协议和 Registry 的交接及独立审查分别位于 `verification/m4-helper-lifecycle-implementation/`、`verification/m4-helper-process-transport/`、`verification/m4-helper-registry/`、`verification/m4-helper-binding-review/` 和 `verification/m4-helper-fence-review/`。时钟调查在 `verification/m4-recovery-clock-review/`。这些路径相对于本机私有 DooNex 数据目录，不属于产品仓库。

下一步处理预留与 helper 身份原子绑定、有限维护、文件/会话容量、清理及前台初始化的完整等待路径。实际网关验证、新 UTC 日完整后台矩阵及独立账目仍待执行；原失败、日预算和未知预留保留。
