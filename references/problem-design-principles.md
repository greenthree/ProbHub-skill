# 算法设计、题面表达与风险指导

本文件只负责出题前的设计判断：从目标算法反推证明义务、约束候选、数据职责和题面表达。它不是 Schema、证明器或自动出题器，也不重新定义 CLI、验证模式或 Judge 协议。

相关规则的唯一事实来源：

- 模式选择、升级信号和独立审查：[verification-modes.md](verification-modes.md)
- 多组数据、`T_max`、累计上限和联合校准：[aggregate-limit-derivation.md](aggregate-limit-derivation.md)
- 错解分类与数据强度：[mistake-taxonomy.md](mistake-taxonomy.md)
- `data.groups`、期望状态和 accepted 独立性：[data-groups-expectations.md](data-groups-expectations.md)
- Checker、Interactor、误差与 Judge QA：[checker-interactor.md](checker-interactor.md)
- Schema 字段与 `difficulty` 契约：[workspace-schema-v1.md](workspace-schema-v1.md)
- 从创建到交付的命令时间线：[problem-creation-walkthrough.md](problem-creation-walkthrough.md)

## 1. 先记录 `design_intent`

在任务笔记或验证交接中记录一份轻量设计意图：

```yaml
design_intent:
  audience: regional-contest
  difficulty_anchor: 3
  core_observation: "..."
  target_algorithm: "排序后维护单调结构，整体 O(n log n)"
  proof_obligations:
    - "转移始终保持不变量"
    - "所有可行答案都会被覆盖"
  complexity:
    time: "O(n log n) per case"
    memory: "O(n)"
  defended_mistakes: [quadratic-enumeration, forget-reset]
  boundary_classes: [minimum, duplicates, near-limit]
  data_responsibilities: [boundary, targeted-wrong, complexity-killer]
  statement_assumptions: [values-are-positive]
  validation_risk:
    proof_novelty: medium
    implementation_complexity: low
    special_judge: false
    resource_margin: unknown
    oracle_available: true
  needs_review: []
  decision: needs_review
```

`design_intent` 不是 `probhub.yaml` 字段，不进入 Manifest，也不改变 source/data hash。它用于暴露设计假设，不能替代题目已有的 `difficulty: 0..5`、人工证明或 Core 证据。

## 2. 从目标算法反向设计

按以下方向推进，而不是先选惯用的 `n=10^5` 再寻找算法：

```text
核心观察 -> 正确性证明 -> 复杂度模型 -> 约束候选
         -> 数据职责 -> 资源校准 -> 题面定稿
```

### 2.1 核心观察与任务边界

先用一句话说明选手要计算、构造、判定或交互什么，再写出目标算法成立所需的输入条件。明确对象、操作、输出语义、允许多解或无解时的行为，以及算法不能依赖的隐藏假设。

### 2.2 证明义务

把“看起来正确”拆成可审查的问题：

- 定义域：算法用到的性质是否都由题面保证；
- 必要性与充分性：判定条件是否既不漏解也不误收；
- 不变量：状态含义是否在初始化、转移和结束时一致；
- 最优性或可行性：贪心交换、动态规划转移或构造步骤是否完整；
- 终止性：循环、递归或交互过程是否一定结束；
- 边界：最小规模、相等元素、不可达状态、溢出和退化结构是否覆盖。

证明未闭合、依赖猜想或只能用随机测试支持时，把具体缺口写进 `needs_review`。更多随机数据不能替代缺失的证明。

### 2.3 复杂度与约束候选

分别建模单组最坏时间、全部测试的总工作量、峰值内存、输出大小，以及题型特有的边数、字符串总长或查询次数。说明最坏结构、初始化成本和常数，不使用平均随机输入替代最坏情况。

约束候选应同时满足：目标算法有足够平台余量；预期更慢算法可被数据区分；Generator、Validator、oracle 和 Judge 可在开发预算内运行。多组数据的具体推导、候选范围与校准门槛只按 [aggregate-limit-derivation.md](aggregate-limit-derivation.md) 执行，本文件不另设公式。

### 2.4 数据职责

把每项约束和每类风险映射到一项可解释的数据职责：

| 职责 | 要回答的问题 |
|---|---|
| 语义边界 | 最小值、相等值、空/单元素和端点是否处理正确 |
| 结构边界 | 链、星、稠密、周期、退化几何等最坏结构是否出现 |
| 复杂度压力 | 单组与累计最坏工作量是否真的接近候选上限 |
| 错解定向 | 已知思路、复杂度和实现错解是否有明确死亡模式 |
| 特殊 Judge | 合法替代、非法格式、协议分支和失败语义是否被主动测试 |

错解枚举与数据数量纪律以 [mistake-taxonomy.md](mistake-taxonomy.md) 为准；结构化运行域与期望状态以 [data-groups-expectations.md](data-groups-expectations.md) 为准。期望矩阵只能杀死已知错解，不能证明不存在未知错解。

### 2.5 校准与定稿

候选约束确定后，用代表性联合最坏输入测量 accepted、oracle、Generator 和 Judge，再在目标 Linux/DOMjudge 环境校准正式限制。本机 Core 结果是可复现的本地证据，不是目标平台承诺。

修改算法、约束、数据、Judge 或资源限制后，旧设计结论和受影响证据都要重新审查。实际命令及重跑位置遵循 [problem-creation-walkthrough.md](problem-creation-walkthrough.md)。

## 3. 难度锚点与验证风险

`difficulty_anchor` 只描述目标选手在指定比赛环境中的认知负担：

| 难度 | 非强制锚点 |
|---:|---|
| 0 | 直接读写或公式代入，没有实质算法选择 |
| 1 | 模拟、枚举或一个局部观察，证明近乎直接 |
| 2 | 一种常见基础算法，至多一个标准转化 |
| 3 | 一个明显非平凡观察，或组合两种常见技术 |
| 4 | 多个相互依赖的观察、较难证明或高级算法 |
| 5 | 新颖建模或强构造洞察，证明与实现均显著困难 |

验证风险另行记录为 `low`、`medium`、`high` 或 `needs_review`：

- 证明新颖度与独立思维步骤；
- 实现复杂度和边界类别数量；
- Checker/Interactor、浮点或协议风险；
- 资源余量与平台敏感性；
- 小范围 oracle 或独立证明的可得性。

难度与风险不求加权总分，也不能自动选择验证模式。用户未指定时仍默认普通模式；快速准入和完整模式硬升级信号只由 [verification-modes.md](verification-modes.md) 定义。

## 4. 题面防歧义清单

定义应靠近第一次使用的位置，题目目标应在题目描述中即可理解，不能依赖样例猜规则。定稿前检查：

- 量词、编号起点、下标范围和操作作用域是否明确；
- 图是否有向、连通，是否允许重边、自环或负权；
- 集合是否有序、元素是否互异，字符串字符集和空串语义是否明确；
- 是否保证有解，多解或无解时输出什么；
- 浮点误差和构造合法性由什么规则判定；
- 交互消息方向、参数范围、刷新、查询次数和终止规则是否完整；
- 每个输入量的单组与累计范围是否与 Validator 的实际约束一致；
- 样例是否覆盖容易误解的操作或输出类型，同时避免泄露核心解法。

Markdown 结构与 Schema 约束以 [workspace-schema-v1.md](workspace-schema-v1.md) 为准；Checker/Interactor 的具体协议和主动 fixture 以 [checker-interactor.md](checker-interactor.md) 为准。

## 5. 五类短案例

案例中的数值或结构都是设计候选，不是 Core 默认值或通用硬门禁。

### 5.1 数组/字符串

目标为线性扫描，主要风险是二次重复扫描。先证明扫描状态含义，再让数据覆盖长度 1、全相同、周期/交替结构、末尾失配和近上界。若证明依赖未说明的字符集，或实现存在隐藏拷贝，标记 `needs_review`。

### 5.2 图论

目标为 `O((n+m) log n)` 的最短路时，分别建模 `n`、`m`、权值和距离上界；数据职责区分不可达、平行边、零权、长链与稀疏/稠密结构。若算法要求非负边但题面允许负边，或 `INF` 安全性未证明，标记 `needs_review`。

### 5.3 多组数据

每组排序为 `O(n_i log n_i)` 时，先根据测试职责选择 `T_max` 和累计规模候选，再验证大量小组的初始化成本与联合最坏输入。若只有单组检查、累计量没有被 Validator 拒绝，或压力数据未覆盖大量小组，标记 `needs_review`；具体上限按累计约束指南推导。

### 5.4 浮点/构造

先定义判定语义，再选择算法和数据。浮点数据覆盖退化、尺度差异和阈值两侧；构造题把格式、合法性和目标值拆开验证。若 Checker 与答案生成器共享同一未复核假设，或误差界没有依据，标记 `needs_review`。

### 5.5 交互题

先画消息状态机，再由决策过程推导查询次数。题面与 Interactor 必须一致处理命令、范围、计数、成功终止和非法协议；主动测试覆盖正常、边界、提前 EOF、超限和空闲。查询上限未证明或状态可能不一致时，标记 `needs_review`。

## 6. `needs_review` 与证据边界

以下情况必须保留 `needs_review`：证明存在跳步或猜想；复杂度与实现不一致；约束会改变预期解法；约束无法映射到数据职责；题面与 Validator/Checker/Interactor 不一致；样例语义不清；资源余量不足；目标平台未知；独立审查仍有分歧。

交接时分开记录四类结论：

| 结论类型 | 能说明什么 | 不能说明什么 |
|---|---|---|
| 设计建议 | 一个约束、数据或题面方案值得验证 | 不证明算法、Judge 或性能正确 |
| 人工证明/审查 | 主 Agent 或独立审查者给出的推理与反例分析 | 不替代实际编译、Judge 或主动测试 |
| Core 证据 | lint、Judge、stress、mutation、seal/build 的结构化结果 | 不证明不存在未知错解，也不保证目标平台性能 |
| 目标平台校准 | Linux/DOMjudge 上的实际资源与协议余量 | 不证明题意、算法或数据覆盖完整 |

只有具体缺口已逐项处理，才能把 `decision` 从 `needs_review` 改为 `accepted`。这只是设计评审结论；验证模式和正式交付仍按各自权威 reference 执行。
