# 模型接入与用量查询参考

更新：2026-11-06。状态：**研究与后续实施建议，不是已交付功能**。用户提出参考 [cc-switch](https://github.com/farion1231/cc-switch) 后，按其源码核对模型配置和用量路径；本轮 M2 应用范围不因此扩大。当前 DooNex 模型接入事实以 [README](../README.md) 和 [M2 实施记录](M2_IMPLEMENTATION.md) 为准。

## 参考基准与决定

上游基准提交：`566448f0617165cfc4cd058911db6508be2c8259`。只读源码，没有运行其程序、复制实现或让上游脚本读取 DooNex 凭据。

| 关注点 | 上游实现 | 对 DooNex 的建议 |
| --- | --- | --- |
| 多供应商配置 | [`provider.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/provider.rs) 将供应商身份、客户端配置和元数据分开 | 保存稳定供应商 ID、API 地址、凭据引用、实际模型 ID 与接口模式；使用现有 SDK，不引入新的模型代理服务 |
| 复杂/简单调用 | [`model_mapper.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/proxy/model_mapper.rs) 根据显式配置映射大小模型；其主/子模型配置另见 [聚合说明](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/docs/user-manual/zh/4-proxy/4.6-aggregation.md) | 先提供“复杂任务”和“快速任务”两个明确配置档；模型及接口经过实际验证后才能选择。映射本身不证明可以自动判断任务复杂度 |
| 账户余额 | [`balance.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/services/balance.rs) 按供应商查询余额 | 独立显示供应商返回的金额、单位、来源及查询时间，不当成本次 Run 消耗 |
| 套餐额度 | [`coding_plan.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/services/coding_plan.rs)、[`subscription.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/services/subscription.rs) 查询套餐窗口、重置时间等 | 额度必须来自账户查询，不通过 token 或模型名推算；没有接口或口径不明确时显示未知 |
| 请求用量与费用 | [`proxy/usage`](https://github.com/farion1231/cc-switch/tree/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/proxy/usage) 和 [用量说明](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/docs/user-manual/zh/4-proxy/4.4-usage.md) 从响应或客户端日志统计用量，再按价格估算 | 保留现有 SDK 响应用量与 Run 总账；价格有明确来源才估算费用，包月费用保持 `null`。供应商账单、网关记录与本地 Run 的覆盖范围不同 |

“接入模型”在这里指连接远程 API，不是把模型权重下载到 Mac mini。模型列表只提供候选 ID，不能证明工具调用、推理档位、Chat/Responses 或上下文上限可用；这些能力仍需按具体配置验证。当前 DooNex 仍是服务端单份模型配置，执行与审查共用该配置；两个配置档、角色模型选择和用量面板尚未实现。

## 当前网关的只读实测

2026-11-06 使用本机已授权的网关和凭据，发起有超时、响应大小限制且不跟随重定向的元数据查询，没有模型推理调用：

- `GET /v1/models`：HTTP 200，返回 **21 个模型 ID**。没有逐个调用或宣称全部兼容。
- `GET /v1/usage`：HTTP 200，JSON 包含 `isValid`、`mode`、`planName`、`remaining`、`unit`、`usage`、`daily_usage` 和 `model_stats`。`usage` 有当天、累计、RPM、TPM 等字段。
- 当前响应的 `mode` 为 `unrestricted`，`unit` 为 `USD`。这不足以确定上游 ChatGPT 包月额度剩余，也不能把网关中的金额字段写成本次包月实付费用或免费。该接口的数据来源、重置窗口和字段单位仍需供应商说明或进一步对照。

cc-switch 的 [`UsageScriptModal.tsx`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src/components/UsageScriptModal.tsx) 含 Sub2API 的同名 `GET /v1/usage` / Bearer 查询模板，可参考字段处理。当前端点可用并不证明网关就是 Sub2API；不基于 URL 或相似 JSON 自动识别供应商。

实测状态、字段和查询时间存于本机私有验收记录；网关地址、密钥、具体账户额度及完整账户响应不进入产品仓库。

## 建议实施顺序

1. **供应商与模型配置档**：沿用现有 `ModelConfig` 和秘密引用，增加稳定配置 ID、只读模型列表与手工输入回退。创建 Run 时冻结实际模型、接口、能力和价格来源；后续修改供应商设置不能改变历史运行。先由用户明确选择复杂/快速档，不做自动复杂度分类或故障后偷偷换模型。
2. **按角色选择**：在固定 executor/reviewer 流程中显式绑定配置档，每个请求预留该模型所需额度，两个角色继续共享 Run 总账、时钟及未知预留。模型是否适合某角色取决于实际结果，不预设审查一定适合小模型。
3. **网关原生只读查询**：先支持已验证的固定端点，服务端注入凭据并严格解析结果；手动刷新和短缓存即可。账户/供应商切换必须清理旧缓存，带上来源和查询时间，区分未配置、不支持、鉴权失败、临时失败、过期与有效数据。
4. **三种口径分别展示**：本次运行的调用/token/未知预留；网关或账户的套餐/余额；有价格依据的费用估算。缓存与推理 token 等细分仅在响应明确提供时展示，未知不用零代替。额度查询不得清空本地账本或隐藏失败。
5. **验收后再扩展**：用固定响应覆盖零值、不限量标记、多窗口、401、429、超时、坏 JSON、字段缺失和换账户；再做实际元数据查询。新增模型的工具调用和恢复仍需单独受控验证，不能拿模型列表查询替代。

这些是 P03 模型配置和现有用量展示的后续建议，不是 M3 Skill Arena 或 M4 自动调度的实现。开始应用改动前，应把本节收敛成一个独立可验收的小阶段。

## 暂不直接引入用量脚本

上游 [`usage_script.rs`](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/src-tauri/src/usage_script.rs) 使用进程内 QuickJS，分别构造请求与解析响应，具有时间、内存和栈限制，HTTP 由宿主执行。这些资源限制不等于独立进程或网络权限隔离。静态审查可见自定义模板可跳过 HTTPS/同源限制，凭据通过字符串替换进入脚本；不能直接作为 DooNex 的秘密边界。

当前需求用固定原生只读适配即可满足：限制目标地址、方法、响应大小、超时和重定向，密钥只在 HTTP 层注入。无需引入任意 JS、外部 shell、自动修改 CLI 配置或自动故障转移。

上游 [LICENSE](https://github.com/farion1231/cc-switch/blob/566448f0617165cfc4cd058911db6508be2c8259/LICENSE) 为 MIT（2025 Jason Young）。后续若复制代码或实质性片段，应保留许可、版权和具体文件/提交来源；本次仅研究与设计，不改变 DooNex 自有许可证状态。
