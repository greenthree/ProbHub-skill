# ProbHub References

`SKILL.md` 是入口和不可违反的边界；本索引只负责按工作阶段路由文档。先读当前阶段的一组 reference，再按题型、风险或命令结果追加资料，不要首次接管就把整个目录载入上下文。命令的最终参数以当前安装包的 `--help` 为准。

## 分阶段路由

### 1. 设计

先读：

- [`problem-design-principles.md`](problem-design-principles.md)：算法、约束反推、题面表达和风险记录；
- [`data-groups-expectations.md`](data-groups-expectations.md)：数据组职责、运行域和已知错解期望。

按需追加：

- 多组数据、`T` 或累计规模：[`aggregate-limit-derivation.md`](aggregate-limit-derivation.md)；
- 错解审查：[`mistake-taxonomy.md`](mistake-taxonomy.md)；
- Checker/Interactor、浮点或非唯一输出：[`checker-interactor.md`](checker-interactor.md)。

### 2. 骨架与规范源

先读：

- [`workspace-schema-v1.md`](workspace-schema-v1.md)：Workspace/题目 Schema、路径、配方和 Judge QA 字段；
- [`problem-creation-walkthrough.md`](problem-creation-walkthrough.md)：从 `init/new` 到规范源闭环的骨架步骤。

按需追加：

- 安装、升级或 WebUI 启动：[`installation.md`](installation.md)；
- 需要生成器或跨平台 stdout：[`cyaron.md`](cyaron.md)、[`fast.md`](fast.md)。

### 3. 执行

先读：

- [`cli.md`](cli.md)：命令参数、退出码、JSON 字段、缓存、judge/build/package 故障；
- [`problem-creation-walkthrough.md`](problem-creation-walkthrough.md)：当前阶段的成功判据、失败重跑点和写入边界。

按需追加：

- stress、反例 replay 或生成器：[`stress.md`](stress.md)；
- 进程、资源、取消或后代清理：[`process-control.md`](process-control.md)；
- checkpoint、seal、generation 或并行交接：[`generations.md`](generations.md)；
- Custom/Interactor 的 Judge QA：[`checker-interactor.md`](checker-interactor.md)；
- 多测累计约束：[`aggregate-limit-derivation.md`](aggregate-limit-derivation.md)。

### 4. 验证

先读：

- [`verification-modes.md`](verification-modes.md)：选择快速/普通/完整模式、独立角色、升级和交接；
- [`cli.md`](cli.md)：读取结构化结果、evidence、status 和 package verification。

按需追加：

- 多组数据边界与平台校准：[`aggregate-limit-derivation.md`](aggregate-limit-derivation.md)；
- 错解、数据组和死亡模式：[`mistake-taxonomy.md`](mistake-taxonomy.md)、[`data-groups-expectations.md`](data-groups-expectations.md)；
- Checker/Interactor 或特殊 Judge：[`checker-interactor.md`](checker-interactor.md)；
- Standard+C++ 且完整模式判断适用时：[`mutation-testing.md`](mutation-testing.md)；
- generation、seal 和发布上下文：[`generations.md`](generations.md)。

## 权威边界

| 内容 | 唯一权威文件 | 其他文件怎么写 |
|---|---|---|
| 任务路由、规范源/生成物边界、验证模式和交付门 | `SKILL.md` | 只链接，不重复完整规则 |
| CLI 参数、退出码、JSON 字段、缓存和常见故障 | [`cli.md`](cli.md) | 入口只保留主线和命令示例 |
| Schema 字段、路径和资源限制 | [`workspace-schema-v1.md`](workspace-schema-v1.md) | 题型指南引用字段，不复制 schema |
| Checker、Interactor、Judge QA 协议和 fixture | [`checker-interactor.md`](checker-interactor.md) | CLI 只说明何时调用 |
| stress、反例 replay、Generator 约束 | [`stress.md`](stress.md) | 模式只规定证据，不复制协议 |
| 进程树、资源控制和取消 | [`process-control.md`](process-control.md) | 模式只保留失败处理和清理边界 |
| checkpoint、seal、generation 和并行语义 | [`generations.md`](generations.md) | 模式只保留交接动作 |
| 多组数据累计上限推导 | [`aggregate-limit-derivation.md`](aggregate-limit-derivation.md) | Skill/模式只保留触发门禁 |
| 算法设计、约束反推与题面表达 | [`problem-design-principles.md`](problem-design-principles.md) | 不把设计建议冒充证明或 Core 证据 |
| 数据组、运行域和期望状态 | [`data-groups-expectations.md`](data-groups-expectations.md) | 不在 Skill 中重复制表格式 |
| 错解枚举和数据强度 | [`mistake-taxonomy.md`](mistake-taxonomy.md) | 不把启发式写成硬约束 |
| Agent 模式、独立上下文和 mutation 适用性 | [`verification-modes.md`](verification-modes.md)、[`mutation-testing.md`](mutation-testing.md) | Skill 只保留决策表 |
| 安装、Node/Python、系统 Python 和 WebUI 检查 | [`installation.md`](installation.md) | 不另写安装命令变体 |

## 完整文件清单

| Reference | 主题 |
|---|---|
| `installation.md` | 安装、升级、doctor、WebUI 启动 |
| `workspace-schema-v1.md` | Workspace/题目 Schema、配方、限制、Judge QA 字段 |
| `cli.md` | 完整命令、结果、缓存、build/package/status 故障 |
| `checker-interactor.md` | Checker、Interactor、主动 Judge QA 和协议守则 |
| `stress.md` | Generator、stress、replay、fixate 和失败结构 |
| `process-control.md` | Windows/Linux 进程树、资源、取消与清理 |
| `verification-modes.md` | 快速/普通/完整模式和交接记录 |
| `mutation-testing.md` | std 语法变异、预算、evidence 和 survivor 处置 |
| `generations.md` | checkpoint、seal、隔离完整试卷和并行出题 |
| `problem-creation-walkthrough.md` | standard、custom、interactive 从设计到交付的可执行主线 |
| `aggregate-limit-derivation.md` | `T_max`、复杂度、累计约束和校准方法 |
| `data-groups-expectations.md` | 数据组、运行域、已知错解期望 |
| `mistake-taxonomy.md` | 思路/复杂度/实现错解与职责覆盖 |
| `constraints-schema-evaluation.md` | 约束、Schema 和静态分析边界 |
| `cyaron.md` / `fast.md` | 生成器写法与跨平台 stdout 要求 |

Typst 模板、`testlib.h` 和示例资源也位于本目录，但它们是脚手架资产，不是 Agent 规则；通过 `new`/`init` 使用，不要把它们当作业务逻辑入口。
