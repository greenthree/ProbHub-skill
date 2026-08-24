# ProbHub References

`SKILL.md` 是入口和不可违反的边界；本目录按需提供细节。先根据当前任务选择一到两个 reference，不要把所有文件一次性读入上下文。命令的最终参数以当前安装包的 `--help` 为准。

## 权威边界

| 内容 | 唯一权威文件 | 其他文件怎么写 |
|---|---|---|
| 任务路由、规范源/生成物边界、验证模式和交付门 | `SKILL.md` | 只链接，不重复完整规则 |
| CLI 参数、退出码、JSON 字段、缓存和常见故障 | [`cli.md`](cli.md) | 入口只保留主线和命令示例 |
| Schema 字段、路径和资源限制 | [`workspace-schema-v1.md`](workspace-schema-v1.md) | 题型指南引用字段，不复制 schema |
| Checker、Interactor、Judge QA 协议和 fixture | [`checker-interactor.md`](checker-interactor.md) | CLI 只说明何时调用 |
| stress、反例 replay、Generator 约束 | [`stress.md`](stress.md) | 模式只规定证据，不复制协议 |
| 进程树、资源控制和取消 | [`process-control.md`](process-control.md) | 失败表只保留处理结论 |
| checkpoint、seal、generation 和并行语义 | [`generations.md`](generations.md) | CLI/Skill 只保留交接主线 |
| 多组数据累计上限推导 | [`aggregate-limit-derivation.md`](aggregate-limit-derivation.md) | 只保留必须触发该文档的门禁 |
| 算法设计、约束反推与题面表达 | [`problem-design-principles.md`](problem-design-principles.md) | 不把设计建议冒充证明或 Core 证据 |
| 数据组、运行域和期望状态 | [`data-groups-expectations.md`](data-groups-expectations.md) | 不在 Skill 中重复制表格式 |
| 错解枚举和数据强度 | [`mistake-taxonomy.md`](mistake-taxonomy.md) | 不把启发式写成硬约束 |
| Agent 模式、独立上下文和 mutation 适用性 | [`verification-modes.md`](verification-modes.md)、[`mutation-testing.md`](mutation-testing.md) | Skill 只保留决策表 |
| 安装、Node/Python、系统 Python 和 WebUI 检查 | [`installation.md`](installation.md) | 不另写安装命令变体 |

## 按任务读取

- **第一次接管/创作题目**：`SKILL.md` → `problem-design-principles.md` → `problem-creation-walkthrough.md` → `workspace-schema-v1.md` → `verification-modes.md`；多组数据再读 `aggregate-limit-derivation.md`。
- **运行或排查命令**：`SKILL.md` → `cli.md`；遇到进程/资源问题再读 `process-control.md`，遇到 stress 再读 `stress.md`。
- **Custom/Interactor**：`SKILL.md` → `checker-interactor.md`；执行 Judge QA 前确认题目级 `judge.qa.schema_version: 1`。
- **并行出题/封题**：`SKILL.md` → `generations.md`；正式构建前回到 `cli.md` 的 `build` 和 `verify-package` 章节。
- **完整模式**：`verification-modes.md` → `mutation-testing.md`（仅 standard+C++ 且主 Agent 判断适用时）。
- **安装/发布**：`installation.md`，发布顺序和包清单再看 `cli.md`/仓库 README。

## 文件清单

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
| `mistake-taxonomy.md` | 思路/复杂度/实现错解分类与数据强度 |
| `constraints-schema-evaluation.md` | 约束、Schema 和静态分析边界 |
| `cyaron.md` / `fast.md` | 生成器写法与跨平台 stdout 要求 |

Typst 模板、`testlib.h` 和示例资源也位于本目录，但它们是脚手架资产，不是 Agent 规则；通过 `new`/`init` 使用，不要把它们当作业务逻辑入口。
