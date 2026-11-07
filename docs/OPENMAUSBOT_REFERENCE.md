# OpenMausBot 参考核对

更新：2026-11-07 UTC。用户提供的 [OpenMausBot](https://github.com/milind-soni/OpenMausBot) 有参考价值，重点是状态审计、恢复原因和部署边界；本次只读研究，不引入新的产品范围或运行上游程序。

在 [M3 历史研究](M3_IMPLEMENTATION.md#外部参考) 之外，本轮固定核对提交 `516ca75981902bb4b9a65d8273b039fcf435c1f1`（提交时间 `2026-11-06T23:57:49Z`）。22 份读取文件与该提交的 Git blob 一致，不表示已经审计整个项目，也不声称该提交一直是最新版。

| 主题与固定来源 | 可借鉴的内容 | DooNex 的决定 |
| --- | --- | --- |
| [生命周期检查](https://github.com/milind-soni/OpenMausBot/blob/516ca75981902bb4b9a65d8273b039fcf435c1f1/server/runtime/invariants.ts) | 事件、存储状态、终态结果和重复通知之间的一致性 | 作为只读审计清单；不能替代固定代码快照、真实检查退出码和数据库证据绑定 |
| [失败处理](https://github.com/milind-soni/OpenMausBot/blob/516ca75981902bb4b9a65d8273b039fcf435c1f1/server/routine-recovery.ts)及[重启处理](https://github.com/milind-soni/OpenMausBot/blob/516ca75981902bb4b9a65d8273b039fcf435c1f1/server/runtime/runtime.ts) | 区分临时失败、等待所有者处理和已发生外部动作 | 保留具体原因与核对入口；不照搬自动重试或备用模型，未知动作继续阻止自动重放 |
| [等待计时](https://github.com/milind-soni/OpenMausBot/blob/516ca75981902bb4b9a65d8273b039fcf435c1f1/server/routine-waits.ts) | 明确记录等待人答复的原因 | 上游扣除等待时间的语义不适用：DooNex 从首次启动计时，暂停、让出和等待不清零预算 |
| [用量记录](https://github.com/milind-soni/OpenMausBot/blob/516ca75981902bb4b9a65d8273b039fcf435c1f1/server/usage-ledger.ts) | 模型、触发者、价格来源和未定价记录的归因 | 其 JSONL 追加可以写失败，不能替代派发前事务预留；缺失用量继续未知，不能归零 |
| [安全边界](https://github.com/milind-soni/OpenMausBot/blob/516ca75981902bb4b9a65d8273b039fcf435c1f1/SECURITY.md)及[自托管说明](https://github.com/milind-soni/OpenMausBot/blob/516ca75981902bb4b9a65d8273b039fcf435c1f1/docs/self-hosting.md) | 说明可信主体、已知缺口、版本固定和持久数据备份范围 | 保持 DooNex 的容器、挂载、网络和私有管理 API 边界；不将上游本机 CLI 的用户权限视作隔离，也不照搬 SQLite 备份操作 |

上游明确披露 service-loopback 的能力限制，本次没有复现或评估其可利用性。目录摘要、导入时链接拒绝可继续用作审查参考；缓存或目录摘要本身不证明所有并发篡改场景已解决。`routine-run-window.ts` 是列表窗口，`harness-budget.ts` 是代理工具超时，不能把它们当成完整任务总预算。

[许可说明](https://github.com/milind-soni/OpenMausBot/blob/516ca75981902bb4b9a65d8273b039fcf435c1f1/LICENSING.md)区分普通 Apache-2.0 源码与采用独立限制的 `enterprise/`；不能称整个仓库都可按 Apache-2.0 使用。本次没有复制实现。未来若复用，应按具体文件核对归属并保留适用许可与 NOTICE；DooNex 自有许可证仍由维护者确定。

详细来源、文件摘要和逐项核对保存在本机私有 `verification/openmausbot-reference-20261107/`。研究没有调用模型、访问生产 API、安装依赖或测试其 Mac 兼容性/性能。当前直接任务仍是补齐 [M4 的完整批次和前台等待验收](M4_IMPLEMENTATION.md)，参考上游不能替代这些证据。
