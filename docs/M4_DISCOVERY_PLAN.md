# M4 下一切片计划：有限 GitHub 候选发现与冻结导入

更新：2026-11-06 · 状态：**前后端已整合并通过离线检查；真实发现、导入和审核尚未验收**。初始计划基于控制器提交 `6b56a7be98ea5ca28f349f1d823ac08704f5f5e1`；实现以修复后的 `a2b64b8` 为基线。前一后台切片的真实状态以 [M4 实施记录](M4_IMPLEMENTATION.md) 为准。2026-11-06 调整实施顺序：后台续接修复已获真实证据，但后续供应商超时及保留的产品日额度使完整验收须等待新 UTC 日期；期间只在独立工作区实施本零模型切片，保持后台运行源码和历史账目不变。此调整不视作前片通过，也不减少其退出条件。

## 当前实现与验证状态

发现后端已在 `eb98cdc` 整合，前端在 `3be8b85` 整合；`61742fc` 进一步将 32 次请求与 32 MiB 总字节上限冻结到每份持久回执，后续默认值调整不能增加旧回执额度。下面第 1–7 节保留设计依据；对应 API、许可正文核对、不可变预览、事务组合导入和界面已实现，实际验收状态以本节为准。

- 整合源码 `eb98cdc` 运行 `PYTHONPATH=apps/server:tests .venv/bin/python scripts/test_local.py`：**874 passed、3 warnings、590.87 秒，退出码 0**。其中模拟供应商响应不代表真实模型调用。完整私有日志为 `verification/m4-discovery-full-first.log`。
- 同一整合源码 Ruff 和前端构建通过；发现界面合成浏览器检查 **27 项通过**，包括过期响应、写入结果未知、冲突和刷新。证据位于私有 `verification/m4-discovery-live/synthetic-ui/`。
- 冻结上限补丁在整合源码 `c5a0070` 补跑 `PYTHONPATH=apps/server:tests .venv/bin/python scripts/test_local.py tests/test_discovery.py tests/test_skills.py`：**173 passed、1 warning、12.76 秒，退出码 0**；Ruff、前端构建和差异检查通过。私有日志为 `verification/m4-discovery-final-regression.log`。
- 尚未运行真实公开来源发现→预览→导入→审核。受控验收会保持模型调用及上游执行为 0，逐项复算固定文件并核对旧数据。浏览器验收仅测本次新版本：主页面自动加载的 Run/推荐历史会在测试导航层隔离，旧 Skill 列表仅显示本次版本，不将这些隔离响应算作历史界面验收。
- 上游候选执行能力、依赖和质量仍未验证；导入验收结束将新版本设为拒绝，保留审核历史。后台整批与生成能力继续独立验收。

本计划承接 [PRD](PRD.md)、[路线图](ROADMAP.md) 与 [竞技规范](SKILL_ARENA.md)。目标是在一个用户指定的公开 GitHub 仓库内发现可检查的 Skill 目录，将用户选择的确切版本导入现有私有 Library。模型生成候选后续独立规划；M5 的公开市场、投稿、分发和商业机制不在范围内。

## 1. 最小可验收流程

用户填写公开 `owner/repo`、ref 和可选目录 → 明确发起一次有限发现 → 显示完整 commit 与候选目录 → 选择一个候选并预览来源、内容清单和许可 → 明确导入固定版本 → 进入现有 `pending` 审核 → 用户检查并批准 → 已批准且完整的版本可被现有 tuning Arena 或关闭状态的后台授权草稿选择。

本切片验收到“冻结导入、审核与可选择”即可，**模型调用为 0**。发现、预览、导入和批准均不自动创建 Run、Arena 批次或启用后台授权。实际 tuning 运行需要另一次明确动作及其原有预算授权，单独记录验收，不能以导入成功推断模型、工具或候选质量通过。

首片限定为：

- 单次发现一个公开的 `github.com` 仓库、一个 ref、一个有限子树；单份发现回执最多成功导入一个候选。
- ref 允许分支、tag 或完整 commit；第一次解析后固定完整 commit，后续读取不再使用可变 ref。
- 只把直接包含普通文件 `SKILL.md` 的目录列为待核对候选，按路径排序；预览和导入还必须确认其非空且为 UTF-8。选中的整个候选子树作为导入边界；若其内部还嵌套其他 `SKILL.md`，首片只预览，要求用户选择更窄目录。
- 只接受候选目录内有完整、明确且受支持许可正文的内容；未知、缺失、限制、冲突或适用范围不明时只预览，禁止远程导入和批准。首个实现只支持可核对的 Apache-2.0 与 MIT 正文，不根据仓库标签、前端选项或 SPDX 短字符串自行推定授权。
- 不做全站搜索、自动刷新、每日抓取、私有仓库、GitHub Enterprise、stars 排名、模型摘要、候选生成、自动批准、循环试验、自动更新或推荐推广。

## 2. 有限网络与权限边界

远程读取由控制器的专用 GitHub reader 发出，复用已有 `httpx` 依赖。它不是执行 Agent 的新工具，不给任务容器网络、GitHub 凭据或额外宿主权限；Skill 的内容和声明始终是待检查数据。

| 项目 | 首片拟采用的边界 |
| --- | --- |
| 输入 | 分开的 `owner/repo`、ref、相对 prefix；不接收任意下载 URL、本机路径、taskset ID 或 evaluation ID |
| 出站目的地 | 固定 `https://api.github.com`；服务端构造并编码端点，不请求响应中的 `download_url`、HTML 链接或 Skill 文本中的地址 |
| 身份 | 首片仅公开资源、无 GitHub token；不读取 `gh` 登录状态、宿主 Git credential helper 或模型凭据 |
| HTTP | 固定 Accept 与已验证的 API 版本，禁止自动重定向、环境代理和自动重试；不使用 clone、checkout 或归档解包 |
| 单份发现总请求 | 建议最多 32 次，覆盖解析、扫描、预览和导入；发出前消耗请求槽，未知结果仍占用，刷新/重启不重置；超过剩余槽时先拒绝并要求缩小范围 |
| 单次网络操作 | 建议整体截止 60 秒、单阶段传输超时最多 10 秒；这些是拟实现的读取边界，不是宿主失联时的绝对响应 SLA |
| 响应大小 | 建议树 JSON 最多 2 MiB、单 blob JSON 最多 4 MiB、整份发现累计接收最多 32 MiB；流式计数，不能只相信 Content-Length |
| 扫描范围 | 建议最多 2,000 个树条目、20 个候选、相对 prefix 深度 8；不完整、超限或截断不能返回“完整发现” |
| 候选内容 | 沿用现有 200 个目录/文件条目、单文件 2 MiB、总文件 16 MiB、深度 8 以及路径、模式、秘密文件名和特殊文件拒绝规则；网络请求预算可能先耗尽 |

以上数字是本切片待实现与验证的设计限制，不是现有配置。请求超限、限流或网络故障应保存明确原因和已使用请求数，不自动换凭据、改来源或反复重试。官方当前公开资源的未认证基础限额为每来源 IP 每小时 60 次；实际剩余额度与重置时间应显示为来源状态，不能等同本地请求预算，也不保证一定有足够额度。

网络操作是用户明确触发的有限请求，不加入长期调度、不新增服务框架或后台守护进程。浏览器中止不代表服务端已经停止；服务端仍受已保存请求槽、截止和字节上限约束，不继续派发后续请求。中断后查看原回执，不能重放未知导入操作。

读取意图、请求槽与结果使用短事务保存；远程 HTTP 和主要内容校验在数据库事务/行锁之外进行。并发操作通过版本与已保存请求槽约束，不能为等网络而持有影响 Library 或 worker 的锁。

## 3. Git 对象与来源冻结

1. 读取仓库公开状态、稳定仓库 ID 和输入 ref 对应的完整 commit，记录原始定位与解析时间。发生迁移、重定向或仓库身份变化时停止，要求用户重新核对定位。
2. 由 commit 的 root tree SHA 逐级定位 prefix。只沿 `040000` tree 下降，保留每一级身份；随后读取固定子树。远程请求始终使用固定对象 ID。
3. 检查每个路径、对象类型、mode、大小及响应完整性。`120000` symlink、`160000` submodule、未知模式、路径冲突、超深或越界均拒绝导入。Git Trees 的 `truncated=true` 或清单超限是失败，不从部分结果发布版本。
4. 对选中候选按固定 blob SHA 获取原始字节。确认响应 SHA、声明长度和实际长度一致，本地复算 Git blob 对象散列；只接受本切片已实现并验证的对象哈希格式，不猜测未知格式。再计算 SHA-256 与现有清单摘要，保留可执行文件 mode，不执行文件。
5. 许可文件与 `SKILL.md` 一样绑定固定路径、blob SHA 和字节摘要。预览显示实际许可正文、候选声明、嵌套冲突和核对状态；其他目录的许可不自动继承为本候选授权。
6. 生成不可变预览摘要，绑定仓库 ID、commit、候选路径、文件/mode/blob 清单及许可回执。最终导入意图再绑定该摘要、明确的来源/许可确认与 scope/permissions 声明；这些最终填写的声明不冒充已在预览摘要内。导入提交不能重新读取 `main` 或接受客户端替换来源、许可、摘要及 commit；相同预览但不同声明不是可重绑旧结果的幂等重试。

Contents API 对某些符号链接会直接返回目标内容，不能独立用它证明条目是普通文件；导入以 Git tree 的 mode/type 为安全依据。GitHub 的许可识别只匹配部分已知许可文件，不覆盖依赖或其他许可说明，也不能自动产生 `verified`。

`metadata.source` 拟使用不带凭据、query 或 fragment 的固定 commit 路径 URL，`metadata.commit` 保存完整 commit，`license_text` 保留确切许可正文与适用路径依据；状态最多自动填 `declared`。范围和权限声明可由用户填写，但不扩展执行授权。正文或声明变化产生新 Skill 版本，不能编辑旧版本取得新含义。

GitHub 传输详情不塞入现有严格的 metadata 字段，也不改变已有 Skill 报告 schema。独立来源回执保存仓库 ID、原始 ref、解析后的 commit/tree、候选路径、各 blob SHA、许可摘要、API 版本、时间、状态、有限请求记录和最终 `version_id`；私有材料不发送给 GitHub 或模型。

## 4. 复用路径与最小文件范围

| 现有实现 | 复用方式 / 本切片最小增量 |
| --- | --- |
| [skills.py](../apps/server/doonex/skills.py) 的 `FileContent`、`PreparedSkill`、清单/metadata 校验与 `SkillRegistry.persist` | 增加受控 bytes→PreparedSkill 入口，复用相同内容和身份规则；不得放宽本地 `prepare_import` 对私有管理目录的拒绝 |
| [library.py](../apps/server/doonex/library.py) 的数据库原始回执、`pending`、`review`、`freeze_skill` | 新来源绑定继续走同一审核/完整性信任链；导入只能新建 pending 或返回已有版本，不重置原审核历史和原始回执 |
| [model_registry.py](../apps/server/doonex/model_registry.py) 的有界流读取模式及已有 httpx | 借鉴固定出站、禁重定向/环境代理、大小/截止和脱敏方式；GitHub reader 独立定义来源，不能复用模型 key 或扩成通用 HTTP 代理 |
| [skills.tsx](../apps/web/src/skills.tsx) 的内容、文件清单、许可与审核界面 | 增加一次发现/选候选/预览/导入；导入后回到现有版本详情，不另建评分或推荐系统 |
| [arena.py](../apps/server/doonex/arena.py)、[background.py](../apps/server/doonex/background.py) | 不改变调度与推广合同；只验证已批准新来源版本能被原入口选中，未知/未批准仍拒绝 |

预计新增小型 `apps/server/doonex/discovery.py` 与来源回执表；在现有 API、Library、Skill 准备入口、Web API 类型及 Skills 页面接线，增加对应合成/网络合同检查。不新增依赖、队列框架、数据库服务、生成 Agent 或 SDK 工具。

文件发布和数据库不能假称为一个跨存储原子事务：先用现有不可变发布器保存完整版本，再在同一数据库事务内绑定首次 Library 回执、来源关系和导入消费状态。必要时只为 Library bind 增加可组合事务入口。崩溃留下的未绑定文件不出现在可选版本中；重试必须证明是同一输入并复用原结果，不能修补被篡改版本或覆盖已存在回执。

## 5. 拟增加的鉴权 API

所有接口沿用本机 Bearer 鉴权、Host/Origin 限制和私有存储。下面仅为拟议合同，尚无对应路由。

| 方法与路径 | 输入 / 结果 / 有限副作用 |
| --- | --- |
| `POST /api/skills/discoveries` | `repository`、`ref`、`prefix`；创建有限发现回执，解析 commit 和树，返回候选路径/摘要、请求消耗、完整性与明确状态 |
| `GET /api/skills/discoveries` | 返回当前本机已保存回执的公开定位、状态及导入关联；不触发远程刷新 |
| `GET /api/skills/discoveries/{id}` | 返回保存的固定结果与失败原因；不能以读取接口重新解析可变 ref |
| `POST /api/skills/discoveries/{id}/preview` | `candidate_id`、`expected_revision`；只读取该回执固定对象，生成有摘要的内容/许可预览；未知或冲突也只到此步 |
| `POST /api/skills/discoveries/{id}/import` | `candidate_id`、`expected_revision`、`preview_sha256`、明确来源/许可确认及有限 scope/permissions；校验来源与许可后发布一个 pending 版本，回执与版本关联同事务保存 |

客户端不得提交 `approved`、`verified`、任意下载地址或替代 commit。服务器校验未知字段、候选归属、版本冲突和预览摘要。许可未知/冲突的远程预览不进入 Library，现有审核 API 因而没有可批准的该版本。

单回执的并发导入只能产生一个结果；同一确定请求的安全重试返回原 `version_id` 和原审核历史，不重新发布。导入前后的网络失败、用户取消或数据库中断保留回执与已消耗请求槽。无已证明的完成结果时，状态保持失败/中断待核查，不能显示“已导入”。

## 6. 失败条件与资料隔离

| 条件 | 必须呈现的结果 |
| --- | --- |
| 仓库非公开、删除、迁移、ref 不存在或对象身份变化 | 停止读取，保留定位与原因；不自动降级到另一个 ref 或仓库 |
| API 401/403/404/429、5xx、超时、重定向、未知编码、异常 JSON | 记录实际 HTTP/错误类别、有限用量和未知步骤，不自动重试或改用宿主凭据 |
| 树截断、候选/条目/字节超限、sha/长度/mode 不匹配、symlink/submodule | 不完整或拒绝；禁止发布部分 Skill |
| 缺少 SKILL.md、非法 UTF-8、空指令、嵌套候选边界不明 | 可显示有限诊断，不发布可用版本 |
| 许可缺失、未知、限制、正文冲突或适用范围不清 | 只预览，不导入、不批准；本切片不提供绕过按钮 |
| 预览/导入来源漂移、旧 revision、内容或保存回执被篡改 | 409 或完整性错误；保留旧版本，不重绑原回执 |
| 进程/数据库中断、请求结果未记录 | 保留未知状态与请求计数，重启不自动读取或重放；孤立文件不可选用 |

发现器不接受任务集、Arena 结果、原始 Run 日志或隐藏检查器作为输入；只发送仓库定位、ref 和对象 ID。前端不能将已有评测证据混入发现请求，候选内容也不作为提示发送给模型。浏览器预览按纯文本呈现，不能执行 Markdown HTML、脚本或外部资源。

导入后沿用受限 `/skills` 挂载和原模型/工具/容器权限。源版本被拒绝、完整性失效或未获批准时，Run、Arena 和后台授权仍不可选择。后台仍只接受 `tuning`；本切片不读取 heldout 的输入、答案、隐藏测试或结果，也不能更改 `arena_current`。后续若进行模型生成，只允许另行定义的 tuning 输入，不能把现有证据接口直接提供给生成 Agent。

## 7. 验收顺序与退出条件

先实现完整导入闭环，再扩大支持的输入形态；模拟与真实读取证据分别保存。所有结果绑定最终源码、命令、退出码、配置、对象和私有日志，不把本文件的来源核对当作产品验收。

| 验收项 | 必须实际执行的检查 |
| --- | --- |
| 完整对象冻结 | 合成 API 在扫描后移动 branch/tag，仍读取原 commit/blob；内容/许可/声明变化产生新版本；完整重复请求返回原结果且保留审核 |
| 类型与完整性 | symlink、submodule、未知 mode、截断树、非法路径、嵌套候选、错 SHA、错 size、异常编码及限额逐项拒绝，不发布部分版本 |
| 有限网络 | 实际统计所有请求、字节和截止；重定向/环境代理/任意 origin/凭据 URL 被拒绝；429、断流和重启不清零或自动重试 |
| 许可门槛 | 候选内明确 Apache-2.0/MIT 可冻结为 declared；未知、冲突、只有标签或受限制内容仅预览；不能客户端传 approved/verified 绕过 |
| 原有信任链 | 真实 PostgreSQL 并发导入、事务失败及发布/绑定间中断；首次数据库回执不变；篡改版本不修复、不重绑、不进入可选列表 |
| 资料边界 | sentinel 检查出站请求、日志、前端和文件清单；模型凭据、数据库凭据、heldout 输入/结果及其他项目材料均未发送；上游脚本执行次数为 0 |
| 审核与接线 | 新版本为 pending；拒绝/未批准不可选；批准且完整后可进入已有 tuning Arena 和关闭后台草稿；所有这类操作的模型请求数为 0 |
| 浏览器 | 可见固定 commit、路径、许可正文/状态、清单、剩余请求与失败原因；刷新读取保存状态；旧响应不覆盖新选择；预览无外部资源加载 |
| 真实公开来源 | 按下一节固定提交完成一次只读发现→预览→冻结导入→人工审核→原入口可选，独立复算字节摘要；模型 0 次、脚本 0 次、源仓库无写入 |

实现整合后按风险运行现有 `.venv/bin/python scripts/test_local.py`、`.venv/bin/ruff check apps/server/doonex tests scripts` 和 `pnpm --dir apps/web build`，保留此前失败与未知记录；新增源代码使旧环境指纹与证据的适用性需要重新核验，不能静默继承旧成绩。

只读真实导入验收不运行现有模型验收脚本。若用户后来明确要求 tuning 模型实验，应单独冻结任务集/候选/模型/预算，按原 M4 授权执行和核算；本计划没有授予任何新增模型额度。

## 8. 已核对的未来只读验收候选

2026-11-06 14:18:34 UTC，使用公开 GitHub REST API、无 Authorization 头读取 `anthropics/skills`，将当时 `main` 解析为以下固定版本。随后对候选全部 blob 进行只读字节读取和本地 Git blob/SHA-256 复算；另外核对固定提交的根 README。**没有导入 DooNex、没有执行上游代码、没有调用模型**。

| 项目 | 本次实际核对结果 |
| --- | --- |
| 官方来源 | [Anthropic 的 skills 仓库](https://github.com/anthropics/skills)；固定提交 README 明示这是 Anthropic 的 Skill 实现 |
| commit | `ca45fc496a3436251f191354091d557da31c15b5`，提交时间 `2026-10-28T19:20:38Z`；未来验收须继续使用此 SHA，不重新取 main |
| 候选路径 | [skills/webapp-testing](https://github.com/anthropics/skills/tree/ca45fc496a3436251f191354091d557da31c15b5/skills/webapp-testing) |
| root tree / 候选 tree | `3b83fb7a7255ef97c3ce8657bd0a3a4d667bd7be` / `5ffb7dc66b9fd4c25c3e400a4c00da99a349b714` |
| 体积与条目 | 6 个文件、2 个目录、共 22,394 字节；树未截断；无 symlink/submodule；包含一个 mode 100755 文件，其余为 100644 |
| 指令 | [SKILL.md](https://github.com/anthropics/skills/blob/ca45fc496a3436251f191354091d557da31c15b5/skills/webapp-testing/SKILL.md)，3,913 字节，frontmatter 明确将完整许可指向同目录 LICENSE.txt |
| 许可 | [同目录 LICENSE.txt](https://github.com/anthropics/skills/blob/ca45fc496a3436251f191354091d557da31c15b5/skills/webapp-testing/LICENSE.txt)，11,345 字节，完整 Apache License 2.0 正文；此核对不推定其他路径使用同一许可 |
| 官方仓库的许可差异 | [固定 README](https://github.com/anthropics/skills/blob/ca45fc496a3436251f191354091d557da31c15b5/README.md) 明确区分 Apache 2.0 示例与 docx/pdf/pptx/xlsx 的 source-available 内容；不能把整个仓库一律当作 Apache-2.0 |
| 网络与结果 | 本机复算脚本 11 次公开 REST GET 均 HTTP 200；API 版本 `2022-11-28`；读取/散列脚本退出码 0；另以固定 URL 只读核对许可正文；模型调用 0、导入 0、上游执行 0 |

| 文件 | 字节 | mode | Git blob SHA | SHA-256 |
| --- | ---: | --- | --- | --- |
| LICENSE.txt | 11345 | 100644 | `4f881c52d1f72f4cfb720e339e2d35c3058d01a9` | `bc6b3af2f331cbc7fb0da1344efb2cbe5877a31498b4d70dbc7000f3405a1362` |
| SKILL.md | 3913 | 100644 | `4726215301db64a0cc4d41fc3219c61f37a30f4a` | `51b7349e77ec63b7744a6f63647e7566a0b4d2e301121cc10e8c2113af6556a2` |
| examples/console_logging.py | 1027 | 100644 | `9329b5e232ed53d944bfe95e750070117d31b9ab` | `ea46877289acb82da7e7ce59d0bc37c8977cd57e2a006d0c88d7a1c625bf95da` |
| examples/element_discovery.py | 1463 | 100644 | `917ba72f52421a1b324f93ae8e38af72010a87b9` | `d63c89604a22f8845d724e95dda45db49b1bf57c25ce0a83afbb7b8da3d402f0` |
| examples/static_html_automation.py | 953 | 100644 | `90bbedcc08cdb345c74fb938ebad02c0baf56603` | `9d533aafb875ee3ab8b8ebf8f5b9003ac8d999da3d09b285cce252e623140064` |
| scripts/with_server.py | 3693 | 100755 | `431f2eba16b268b7f3e2ae4daae9db41c0289b6d` | `b0dcf4918935b795f4eda9821579b9902119235ff4447f687a30286e7d0925fd` |

原始只读响应元数据及复算摘要保存在本机临时记录 `/tmp/doonex-m4-discovery-sourcecheck-20261106.json`，不作为长期验收证据，也不加入产品 Git。长期产品验收执行时应把新的代码版本、读取计划、原始 HTTP 状态、时间线、对象清单和独立复算写入授权私有 verification 目录。此文档仅保存公开定位和摘要，没有复制上游源码或许可正文。

该候选适合作为小体积、含可执行 mode 和目录内明确许可的**读取/版本流程样本**。它的 Playwright 脚本、依赖、权限及在 DooNex 中的任务能力没有验证；不因官方来源、许可核对或未来导入成功而批准执行、承诺兼容或宣称质量。

## 9. 官方资料与当前决定

- [Git Trees](https://docs.github.com/en/rest/git/trees)：对象 mode/type、recursive 和 truncated 的实际合同；本产品采用更小的限制，截断时拒绝完整导入。
- [Git Blobs](https://docs.github.com/en/rest/git/blobs)：按固定 SHA 获取 blob 字节及编码；下载不等于执行或可信。
- [Repository Contents](https://docs.github.com/en/rest/repos/contents)：符号链接返回行为、目录条目限制及 ref；不单独把 Contents 的文件响应当安全类型证明。
- [Licenses](https://docs.github.com/en/rest/licenses/licenses)：许可识别范围与限制；识别结果不能代替候选路径的正文和人工核对。
- [REST API 限流](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)：未认证、主/次限流及来源响应；不通过重试或身份切换绕过。
- [REST API 版本](https://docs.github.com/en/rest/about-the-rest-api/api-versions)：显式固定请求版本，升级后重跑对象和错误合同。

当前已可直接采用的决定：公开单仓库、有限一次读取；以上固定候选用于未来验收；许可未知/冲突只预览；首片零模型闭环；不做 M5。剩余实施细节按本文边界选择最少改动，并通过实际检查确认。按本页记录的实施顺序调整，在独立工作区开展零模型源代码实施；整合与验收分别记录，不能继承前片未通过的结论。发现架构冲突、凭据需求或新增模型预算时再说明具体问题。
