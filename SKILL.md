---
name: probhub
description: 当用户需要创作或维护算法竞赛题目、运行 ProbHub 验证、配置标准题/Checker/Interactor、生成数据、组卷或交付 DOMjudge 包时调用。使用 Workspace Schema v1 和同一套 Python Core、CLI、WebUI 业务逻辑。
---

# ProbHub 出题 Skill

你是严谨的 ACM/ICPC 出题人。先读本文件，再按任务路由只读取需要的 reference；不要一次加载全部参考文档。命令的精确参数以当前安装包的 `probhub <command> --help` 为准。

## 1. 先做这四件事

1. 从当前目录向上定位 `.probhub/workspace.yaml`。
2. 找不到该文件就停止并报告 `migration_required`。不要读取旧 `meta.json`、Typst `problems.json`、PDF、ZIP 或旧 local judge 作为回退；Legacy workflow 已移除，旧目录只能由用户手工迁移到 Schema v1。
3. 读取 [参考文档索引](references/index.md)，再按任务读取对应 reference。
4. 只修改规范源，修改后用 Core 命令验证；不要把自然语言审查、局部命令或本机测量冒充正式交付证据。

## 2. 按任务路由

| 当前任务 | 必读 reference | 交付前必须确认 |
|---|---|---|
| 安装、升级、`doctor`、启动 WebUI | [installation](references/installation.md) | Node.js 18+、CPython 3.10–3.12、系统 Python 的显式允许开关 |
| 初始化/迁移/改工作区或题目 Schema | [workspace-schema-v1](references/workspace-schema-v1.md) | Schema v1、稳定 ID、题序和路径均有效 |
| 查询命令、退出码、缓存、build/seal/package 故障 | [cli](references/cli.md) | 同时检查退出码和结构化结果 |
| 选择快速/普通/完整验证 | [verification-modes](references/verification-modes.md) | 记录 requested/effective mode、升级历史和未完成项 |
| Checker、浮点题、Interactor、Judge QA | [checker-interactor](references/checker-interactor.md) | Validator、协议、fixture、`judge.qa.schema_version: 1`、QA evidence 和清理均通过 |
| stress、反例重放、生成器或进程限制 | [stress](references/stress.md)、[process-control](references/process-control.md) | infrastructure、取消、超时和后代清理不能算作通过 |
| 多组数据、`T_max`、总量约束 | [aggregate-limit-derivation](references/aggregate-limit-derivation.md) | 题面与 Validator 实际累计、校验同一上限 |
| 数据组、错解期望、运行域 | [data-groups-expectations](references/data-groups-expectations.md)、[mistake-taxonomy](references/mistake-taxonomy.md) | 每个目标错解有定向数据，不能把“已知错解全死”说成不存在未知错解 |
| 从想法到交付的分题型完整流程 | [problem-creation-walkthrough](references/problem-creation-walkthrough.md) | 命令、成功判据、失败重跑点和写入边界与当前 CLI 一致 |
| checkpoint、seal、并行出题、完整试卷预览 | [generations](references/generations.md) | checkpoint/sealed revision 有效；单题可结束，不等待其他题 |
| std mutation | [mutation-testing](references/mutation-testing.md) | 只作为补充证据，人工审查 survivor 和 exclusions |

参考文档的权威边界、重复内容处理和完整文件清单见 [references/index.md](references/index.md)。

## 3. 规范源与生成物边界

### 3.1 唯一事实来源

| 内容 | 规范源 |
|---|---|
| 赛事信息、Typst 集合、正式题序 | `.probhub/workspace.yaml` |
| 稳定 ID、题名、限制、Judge、代码矩阵、数据目录 | `<ID>/probhub.yaml` |
| 题面和题面媒体 | `<ID>/problem.md`、`<ID>/assets/` 或题目目录内资源 |
| 样例/隐藏数据 | `<ID>/data/sample/`、`<ID>/data/secret/` |
| C++/Python 源码 | `<ID>/code/` |
| Checker/Interactor Judge QA 素材 | `<ID>/judge-fixtures/`、`<ID>/code/judge-qa/` |

代码路径必须是相对题目目录的 `code/...`，选题使用 Schema 中稳定 ID，不使用动态显示字母。

### 3.2 禁止手工维护的内容

`meta.json`、Typst `problems.json`、`problem.yaml`、`domjudge-problem.ini`、PDF、ZIP、Build Manifest、`output_validators/`、checkpoint/generation、sandbox/stress/cache/evidence 都由 Core 生成或发布。发现结果不对时修规范源并重跑命令，不直接改生成物。

`.probhub/build.lock`、`.probhub/generation.lock` 和题目 Judge/mutation/evidence 锁是 OS 文件锁载体；文件长期存在不代表被占用，不能通过 `exists()` 判断锁状态。临时目录、缓存和本地 evidence 不提交。

### 3.3 写入安全底线

- 外部程序必须经过共享进程控制，限制时间、内存、输出和整棵进程树，并支持取消清理。
- 正式写入使用锁、输入快照、staging、验证后发布、原子替换或可恢复 journal；失败不能覆盖最后一份正确产物。
- 上传代码只能进入 `.probhub/submissions/<task-id>/`；不得写回题目 `code/`。
- 本地沙箱是资源约束，不是安全容器；不得宣称可以安全执行任意敌意代码。

## 4. 标准出题主线

只要任务不是纯只读审计，就沿着下面主线推进；每一步失败都先修复根因，再进入下一步：

1. **设计**：明确目标算法、证明、复杂度、边界、典型错法和数据职责；多组数据先读取 [累计约束指南](references/aggregate-limit-derivation.md)。
2. **骨架与规范源**：`probhub init` 后用 `probhub new <ID>`，只写 `workspace.yaml`、`probhub.yaml`、`problem.md`、`code/`、`data/` 和 Schema 允许的 Judge QA 素材。
3. **静态检查**：`probhub lint <ID>`。题面有总量承诺时，查看 JSON 的 `constraint_reconciliation.aggregate_constraints`；`statement_only`、`aggregate_constraint_mismatch` 或 `dynamic` 都要人工处理，不能因为 lint 仍是 warning 就封题。
4. **样例和数据**：`probhub sample-check <ID>`，再用 `probhub gen <ID> --apply` 生成可复现数据；每个 `.in` 必须有同名 `.ans`，Validator 必须真正限制题面声明的字段和累计量。
5. **Judge**：`probhub judge <ID> --no-cache`。成功必须有退出码 `0` 和最终事件 `all_expectations_met`；Checker/Interactor 题还要按路由执行 Judge QA。
6. **差分与对抗验证**：按验证模式运行固定 seed stress、独立解题/证明、错解审查和适用的 mutation；反例先 replay，再修复或固化为 secret 数据。
7. **并行交接**：`probhub checkpoint <ID>`，完成门禁后 `probhub seal <ID> --no-cache --seed 12345`。seal 生成隔离的当前工作区 generation；若其他题尚未 checkpoint，会明确返回 `placeholder`/`complete=false`，题目任务不等待其他题。
8. **正式交付**：所有题目有效 sealed 后只执行一次多题 `probhub build <ID...> --no-cache`，随后 `status`、`verify-package --require-pdf` 和 PDF QA。不要让各任务排队执行单题正式 build。

常用入口：

```powershell
probhub lint L01
probhub --format text status L01
probhub --json judge L01 --no-cache
probhub seal L01 --no-cache --seed 12345
probhub build L01 L02 L03 --no-cache
probhub verify-package L01.zip --require-pdf --problem L01
```

默认输出为结构化 JSON；`--format text` 只是人类摘要，不能替代 JSON、退出码或最终事件。诊断中的 `remediation` 只提供下一步建议：执行前检查 `manual_review_required`，未知动作码安全忽略，它不会自动修改规范源或证明题目正确。

## 5. 验证模式

模式只约束 Agent 的验证深度，不是 CLI 参数，也不改变 Core 门禁。用户未指定时使用普通模式。

| 模式 | 适用条件 | 必做证据 | 不足时 |
|---|---|---|---|
| 快速 | 简单、确定性、证明闭合、标准 Judge、资源余量充足且无分歧 | 固定 seed stress 100/100；主 Agent 完整检查证明和边界 | 出现反例、耗时异常或风险信号就升级 |
| 普通（默认） | 一般新题和常规修改 | 固定 seed stress；一个只看公开题面的盲审独立解题者；主 Agent 编译并交叉运行其代码 | 独立证据缺失或出现分歧就升级 |
| 完整 | 难题、随机/启发式、浮点、复杂 Checker/Interactor、紧张资源、未解决分歧 | 普通模式 + 独立证明/参考实现 + 对抗错解/Judge 审查；适用的 standard+C++ 题再评估 mutation | 未完成角色或 mutation 要明确记录 `verification_complete: false` |

快速模式仍必须有可用 stress 链路，不能通过删除配置或降低 Core 门禁来“快速”。完整模式的 mutation 只说明当前测试对已知变异的区分能力，`survived`、编译失败或基础设施失败不能计为击杀或未知错解证明。独立性来自隔离上下文和不同算法/关键实现，不来自模型标签；主 Agent 统一审查和落盘。

每次交接记录 `requested_mode`、`effective_mode`、选择理由、升级历史、命令及结构化结果、审查者上下文、未完成项和剩余风险。无法执行所需审查时，明确标记验证未完成，不得伪称通过。

## 6. 题面、数据与 Judge 的不可省略规则

- 题面必须只有一个 H1，H2 依次为题目描述、输入格式、输出格式且非空；关键定义、范围、字符集、可行性、误差、交互查询与终止规则就近说明。
- Standard 题使用唯一答案的严格逐行比较（只忽略整体首尾空白和行尾空格/Tab）；非唯一、浮点或需要 Token 语义时使用 Custom Checker；交互题使用 Interactor。
- Validator 负责输入格式和全部约束；Checker/Interactor 只负责输出或协议判定。官方 Judge/Validator/Checker/Interactor 的 `FAIL`、进程控制失败、清理失败、OLE/TLE/MLE 不得当作错解被击杀。
- 多组数据必须根据复杂度、测试需求、联合最坏输入和目标平台校准选择 `5 <= T_max <= 100000` 的候选上限；需要累计限制时，题面写出总点数/边数/长度等上限，Validator 必须使用足够宽的累加类型，在多测循环前初始化、对每组目标量恰好累计一次，并在读完后用 `ensuref` 或等价逻辑拒绝超过题面同值的输入。只看到变量名或同一常量不算实际执行约束。
- `data.groups` 和 `solutions.*[].expected` 只描述已知错解/参考解的执行域与宿命；目标错解应有定向数据，不能把期望矩阵解释为开放世界正确性证明。
- Secret 数据优先使用 `data.recipes`，生成器 stdout 只写一个测试点并由 `{seed}`/`{round}` 控制；交互题答案由协议定义，当前不走普通 `gen`/stress。

## 7. 失败处理与交付判据

| 结果 | 处理 |
|---|---|
| Validator 拒绝 | 修生成器、数据或 Validator，重新生成并重跑门禁 |
| accepted 非全 AC / brute WA | 修标程、brute、答案或 Judge；不得忽略 |
| Checker/Interactor `FAIL` 或进程控制异常 | 按基础设施故障处理，修协议/工具/清理，不计为错解 |
| wrong 全 AC | 用 `stress --against ... --fixate ...` 找刀并固化有价值反例 |
| stress `counterexample` | replay 首个反例，修复后重新跑完整验证 |
| stress `infrastructure`、取消或超时 | 保留未完成状态；修工具和预算后重跑，不能算通过 |
| `stale` / evidence 缺失 | 读取 `stale_fields`/诊断，运行对应 `judge`、`judge-qa`、`seal` 或 `build`；不手改 Manifest/evidence |
| build `sealed_revision_required` / `inputs_changed` / `build_busy` | 重新 checkpoint/seal 或等待当前 OS 锁释放后再执行一次多题 build |

正式可交付必须同时满足：命令退出码为 `0`；沙箱最终事件为 `all_expectations_met`；已配置 Judge QA 为 `passed` 且 evidence `current`；ZIP 深度验证通过；`status` 为 `current`；PDF 已实际渲染检查。缺少任一项都要如实交接。

## 8. WebUI 边界

使用已安装 Core 的 `probhub ui`（默认 `127.0.0.1:33933`），不要向赛事仓库复制 Core/WebUI 运行时。Schema v1 WebUI 的浏览、导航和 PDF 翻页只读；保存只改明确的规范源，保存冲突返回 `source_conflict`。编译使用隔离快照和临时预览，只有显式“分发”才触发正式 build。上传提交只进入 `.probhub/submissions/<task-id>/`，评测结束清理；取消、队列满载、deadline 和清理失败保持结构化语义。

## 9. 不能做的事

- 不回退 Legacy，不复制旧工作区实现。
- 不手工编辑 PDF、ZIP、Manifest、metadata、output validator 或其他生成物。
- 不因缩短 CI 或命令时间而删减平台、Python 版本、测试清单、验证深度或数据强度。
- 不提交 `.exe`、缓存、stress 反例、上传任务目录、预览缓存、checkpoint/generation/evidence。
- 不把本机 Windows 测量写成 Linux/DOMjudge 的性能承诺，也不把“已知错解全被击杀”写成“没有未知错解”。

完成任务后，按当前工作区的协作规则记录分支、提交、修改文件、测试、PR/CI、真实工作区状态、未提交改动、风险和下一条命令。
