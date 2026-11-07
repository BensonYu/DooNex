# 模型接入与用量查询参考

更新：2026-11-06。状态：**模型配置与用量阶段已实现，受控验收结果如下**。用户提出参考 [cc-switch](https://github.com/farion1231/cc-switch) 后，按其源码核对模型配置和用量路径；M2 历史验收不因此改写；用户后续授权持续推进后，作为 M3 前的独立纵向流程实施。

## 参考基准与决定

上游基准提交：`566448f0617165cfc4cd058911db6508be2c8259`。只读源码，没有运行其程序、复制实现或让上游脚本读取 DooNex 凭据。

| 关注点 | 上游实现 | 对 DooNex 的建议 |
| --- | --- | --- |
| 多供应商配置 | [`provider.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/provider.rs) 将供应商身份、客户端配置和元数据分开 | 保存稳定供应商 ID、API 地址、凭据引用、实际模型 ID 与接口模式；使用现有 SDK，不引入新的模型代理服务 |
| 复杂/简单调用 | [`model_mapper.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/proxy/model_mapper.rs) 根据显式配置映射大小模型；其主/子模型配置另见 [聚合说明](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/docs/user-manual/zh/4-proxy/4.6-aggregation.md) | 先提供“复杂任务”和“快速任务”两个明确配置档；模型及接口经过实际验证后才能选择。映射本身不证明可以自动判断任务复杂度 |
| 账户余额 | [`balance.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/services/balance.rs) 按供应商查询余额 | 独立显示供应商返回的金额、单位、来源及查询时间，不当成本次 Run 消耗 |
| 套餐额度 | [`coding_plan.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/services/coding_plan.rs)、[`subscription.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/services/subscription.rs) 查询套餐窗口、重置时间等 | 额度必须来自账户查询，不通过 token 或模型名推算；没有接口或口径不明确时显示未知 |
| 请求用量与费用 | [`proxy/usage`](https://github.com/farion1231/cc-switch/tree/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/proxy/usage) 和 [用量说明](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/docs/user-manual/zh/4-proxy/4.4-usage.md) 从响应或客户端日志统计用量，再按价格估算 | 保留现有 SDK 响应用量与 Run 总账；价格有明确来源才估算费用，包月费用保持 `null`。供应商账单、网关记录与本地 Run 的覆盖范围不同 |

“接入模型”在这里指连接远程 API，不是把模型权重下载到 Mac mini。模型列表只提供候选 ID，不能证明工具调用、推理档位、Chat/Responses 或上下文上限可用；这些能力仍需按具体配置验证。DooNex 现已支持私有模型配置档、明确选择执行/审查模型和账户查询面板。复杂/快速仅是用户给配置档的名称；当前没有自动任务复杂度分类，也没有模型自动降级。

## 当前网关的只读实测

2026-11-06 使用本机已授权的网关和凭据，发起有超时、响应大小限制且不跟随重定向的元数据查询，没有模型推理调用：

- `GET /v1/models`：HTTP 200，返回 **21 个模型 ID**。没有逐个调用或宣称全部兼容。
- `GET /v1/usage`：HTTP 200，JSON 包含 `isValid`、`mode`、`planName`、`remaining`、`unit`、`usage`、`daily_usage` 和 `model_stats`。`usage` 有当天、累计、RPM、TPM 等字段。
- 当前响应的 `mode` 为 `unrestricted`，`unit` 为 `USD`。这不足以确定上游 ChatGPT 包月额度剩余，也不能把网关中的金额字段写成本次包月实付费用或免费。该接口的数据来源、重置窗口和字段单位仍需供应商说明或进一步对照。

cc-switch 的 [`UsageScriptModal.tsx`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src/components/UsageScriptModal.tsx) 含 Sub2API 的同名 `GET /v1/usage` / Bearer 查询模板，可参考字段处理。当前端点可用并不证明网关就是 Sub2API；不基于 URL 或相似 JSON 自动识别供应商。

实测状态、字段和查询时间存于本机私有验收记录；网关地址、密钥、具体账户额度及完整账户响应不进入产品仓库。

UTC `2026-11-07T00:32:53Z`，在 M4 两次 Responses 请求超时后又完成一次原适配器的只读用量查询：HTTP 200、有效标记与 `unrestricted` 标记仍在。它没有返回超时请求的逐条结算或原因，不能据此认定套餐耗尽、恢复推理正常或释放本地未知预留；没有因此额外调用模型。

## 已实施的流程

1. **供应商与模型配置档**：沿用现有 `ModelConfig` 和秘密引用，增加稳定配置 ID、只读模型列表与手工输入回退。创建 Run 时冻结实际模型、接口、能力和价格来源；后续修改供应商设置不能改变历史运行。先由用户明确选择复杂/快速档，不做自动复杂度分类或故障后偷偷换模型。
2. **按角色选择**：在固定 executor/reviewer 流程中显式绑定配置档，每个请求预留该模型所需额度，两个角色继续共享 Run 总账、时钟及未知预留。模型是否适合某角色取决于实际结果，不预设审查一定适合小模型。
3. **网关原生只读查询**：先支持已验证的固定端点，服务端注入凭据并严格解析结果；手动刷新和短缓存即可。账户/供应商切换必须清理旧缓存，带上来源和查询时间，区分未配置、不支持、鉴权失败、临时失败、过期与有效数据。
4. **三种口径分别展示**：本次运行的调用/token/未知预留；网关或账户的套餐/余额；有价格依据的费用估算。缓存与推理 token 等细分仅在响应明确提供时展示，未知不用零代替。额度查询不得清空本地账本或隐藏失败。
5. **验收后再扩展**：用固定响应覆盖零值、不限量标记、多窗口、401、429、超时、坏 JSON、字段缺失和换账户；再做实际元数据查询。新增模型的工具调用和恢复仍需单独受控验证，不能拿模型列表查询替代。

以上扩展 P03 模型配置和现有用量展示，不代表 M3 Skill Arena 或 M4 自动调度已完成。原服务端 model.json 作为只读 legacy 配置保留；新配置保存到私有 profiles.json，更新校验版本摘要，凭据只保存服务端引用。混合按量/包月角色的总货币费用为 null，另记按量小计并施加共享费用上限。

## 暂不直接引入用量脚本

上游 [`usage_script.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/usage_script.rs) 使用进程内 QuickJS，分别构造请求与解析响应，具有时间、内存和栈限制，HTTP 由宿主执行。这些资源限制不等于独立进程或网络权限隔离。静态审查可见自定义模板可跳过 HTTPS/同源限制，凭据通过字符串替换进入脚本；不能直接作为 DooNex 的秘密边界。

当前需求用固定原生只读适配即可满足：限制目标地址、方法、响应大小、超时和重定向，密钥只在 HTTP 层注入。无需引入任意 JS、外部 shell、自动修改 CLI 配置或自动故障转移。

上游 [LICENSE](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/LICENSE) 为 MIT（2025 Jason Young）。后续若复制代码或实质性片段，应保留许可、版权和具体文件/提交来源；本次仅研究与设计，不改变 DooNex 自有许可证状态。

## 本阶段实际验收

后端及真实模型验证版本为 `7c86b40fc0f2a367164eb217288a6a17ff3e3b38`，2026-11-06 在原 Apple Silicon Mac mini 环境验证：

- 完整本机回归 **214 passed，3 warnings，退出码 0**，含真实 Docker 边界；这些测试中的模型响应是模拟数据。
- 配置档保存、模型列表和账户用量走真实服务端 API；两份配置的 `/v1/models` 与 `/v1/usage` 均 HTTP 200，每次模型列表返回 21 个 ID。没有把未调用模型记为兼容。
- 首次 Sol 执行、Mini 审查：执行角色完成；`gpt-5.4-mini` Responses 请求返回 `NotFoundError`，Run 为 `failed`。共 4 次调用、2,854 已知 tokens、1 次未知调用，7,024 tokens 预留保持；不隐藏或按零用量结算。
- 新建明确的 Terra 配置后单独验收：`gpt-6.1-sol` 执行与 `gpt-6.1-terra` 审查均完成，结构化审查批准，固定快照通过实际检查。共 **6 次调用、7,424 tokens**，无未知预留，费用 `null`；原脏工作区保持完整。没有自动修改失败运行的模型或重放动作。
- 真实浏览器完成配置展示、21 个模型 ID、账户查询来源、未知上游额度、切换清理旧结果、角色选择和桌面/手机布局检查；初次测试发现初始化请求会抢走设置页，`85cb32c` 修复后显式延迟初始响应复验，8 项真实界面检查通过；模型输入边界另在 `aa2dac6` 修复，构建通过。合成响应界面检查 19 项通过，覆盖失败状态及配置冲突，不能代替真实模型验收。

上述两个模型只验证了当前网关的 Responses 编程/审查受控样例，不证明速度、价格或复杂度优劣。Mini 的模型列表记录保留，其 Responses 路径不能列为可用。Anthropic 和其他模型仍未真实验证。

私有原始记录保存在本机 DooNex 数据目录的 `verification/model-profiles/`；完整历史 campaign 继续累积，没有清零。真实账户响应、网关地址、API 密钥和样例源代码不进入本仓库。

## 后续 Responses 失败诊断（离线验证，尚未切换常驻服务）

M4 的两次真实超时只保存了外层 `LLMTimeoutError`，无法从历史记录区分连接、读取、写入或连接池等待。新增实现为每个新的 Responses 请求生成独立 `X-Client-Request-Id`；普通失败时保存私有 `model_request_failed` 事件，绑定预算 action、客户端耗时、配置超时、有界异常链、可观测的 HTTPX 阶段及符合限制的服务端请求 ID。诊断不保存原始异常文案、URL、请求正文或任意响应头。关联头只是追踪线索，兼容网关是否保存或支持检索仍未验证。

锁定 SDK 的离线实验表明 LiteLLM 会为超时生成合成 408；实现只把实际 `httpx.HTTPStatusError` 所绑定响应作为 HTTP 状态证据。客户端的 `read` 阶段不代表已定位网关内部或上游故障；没有服务端证据时，失败原因和实际用量仍可未知。头部准备在预算预留前完成；诊断器本身失效时保留固定降级记录和原失败，不重发请求，不释放未知预留。硬中断发生在失败处理之前时，仍可能没有该诊断事件。

独立实现提交 `ed96238`（整合为 `a9e2faf`）的组合检查 **296 passed、3 warnings、82.81 秒、退出 0**，使用一次性 PostgreSQL、真实锁定 SDK 和 MockTransport，没有真实网关调用。包含连接/读/写/池超时、真实与合成 408、504、未知异常、关联 ID、秘密回显拒绝、诊断降级及 worker 持久化。完整整合回归另见 [M4 记录](M4_IMPLEMENTATION.md)。原始记录位于私有 `verification/m4-timeout-investigation-sdk/`；旧失败不补写新字段，也不因新增诊断改写历史分类或账本。
