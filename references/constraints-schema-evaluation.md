# `constraints` Schema 评估与设计决策

> 状态：执行切片进行中。当前只实现 Schema v1 的 fail-closed 检查、规范化和 fingerprint 基础，**尚未启用 Schema v2 执行路径**。
> 设计基线：2026-07-28。当前 Core 0.7.x 仍只支持现有问题 Schema；本文中的字段、Token、头文件、诊断码和状态均是未来契约，不是当前可用功能。

## 1. 决策摘要

当前 Core 执行切片不启用 `constraints` 字段，只完成样例快检、题面/Validator 约束对账和可确定的题面规范 lint。原因是“约束单一事实源”不是一个只改 YAML 或题面渲染器的局部功能，而是同时影响：

- `problem.md` 的源文本与最终渲染文本；
- Validator、Generator、accepted、brute、wrong、Checker 和 Interactor 的 C++ 编译环境；
- `judge` 的编译缓存、逐点缓存和本地校准 evidence；
- `stress`、replay、`--against`、`--fixate` 与 `gen` 的独立编译路径；
- WebUI 的源文本 round-trip、预览和冲突检测；
- source hash、collection hash、checkpoint、seal、status、build 和 DOMjudge 包内编译闭包。

在这些路径没有共享同一 Core 实现前，任何部分启用都会制造“看似单一事实源、实际仍有两套值”的危险状态。因此本次决策是：

1. 当前版本不把 `constraints` 写入正式 Schema 参考，不生成头文件，不展开 Token。
2. 当前版本若在问题 Schema v1 的 `probhub.yaml` 手工加入 `constraints`，Core 会以 `constraints_requires_problem_schema_v2` fail closed，不会继续 Judge、gen 或 build。
3. 未来实现必须是 problem-level、显式 opt-in、fail-closed 的完整切片；缺任一编译或渲染路径都不得发布。
4. 存量题不强制迁移；没有 opt-in 的题目保持现有行为。

## 2. 当前行为与必须写清的边界

当前 YAML loader 只确认文档根是 mapping、`schema_version` 为已支持值，并由各模块通过 `.get(...)` 读取已知字段。针对 `constraints`，Core 已增加显式的版本门禁；因此当前手工添加：

```yaml
constraints:
  version: 1
  values:
    n_max: 200000
```

不会进入现有执行路径，而会在加载问题配置时 fail closed：

- `problem.md` 中的 `{{constraints.n_max}}` 会保持普通文本，不会替换；
- 不会生成 `probhub_constraints.hpp`；
- 不会向任何编译命令增加 include 目录；
- Judge、stress、gen 和 build 不会获得该值；
- metadata、PDF 和 WebUI 预览不会把它当作约束；
- 当前 WebUI 不展示或验证该结构，现有写回路径即使偶然保留未知字段，也不构成公开兼容承诺。

`probhub.yaml` 文件本身已参与 source hash，所以添加该字段可能让 `source_hash`、seal 或 status 发生变化；当前会优先返回稳定的 `constraints_requires_problem_schema_v2`，**不表示该字段已生效**。在正式支持发布前，不得用它作为题面、Validator 或数据生成的事实来源。

## 3. 为什么不能做部分实现

### 3.1 不能只改 metadata 或题面渲染

只在 `build_meta` 或 Typst metadata 中替换数字，会让 PDF 看起来正确，但 Validator、Generator、标程、暴力和错解仍使用硬编码旧值。此时题面与实际接受域可能相反，且 `stress`/`gen` 无法发现是模板值和代码值不一致。metadata 是生成物，不是约束执行层，不能独自成为事实来源。

### 3.2 不能只改 local Judge

local Judge 只是普通评测的一条入口。`stress` 和 `gen` 目前使用另一套临时编译路径，正式 build 还会在快照中重新 Judge、排版、生成 DOMjudge 配置并验证包。只让 local Judge 看到头文件会导致：

- `judge` 能编译，`stress` 或 `gen` 却找不到头文件；
- 本地 Judge 使用新值，PDF 仍保留 Token 或旧字面量；
- build 快照、WebUI 预览和 DOMjudge output validator 的编译闭包与开发态不同；
- 编译缓存可能复用未包含新约束的旧二进制。

所以必须由 Python Core 提供同一份约束解析、渲染和编译上下文，CLI、local Judge、stress、gen、WebUI 与 build 只调用它。

### 3.3 不能把生成头文件写入 `code/`

`code/` 是作者维护的规范源，且 source hash 会递归跟踪其中的头文件和 include 片段。把派生的 `probhub_constraints.hpp` 写入 `code/` 会造成：

- YAML 和生成头文件同时成为两份可编辑值，破坏单一事实源；
- 第一次生成头文件本身改变 source hash，容易在 seal/build 输入栅栏中形成自触发变化；
- 命令失败或中断可能留下旧头文件，下次编译悄悄使用陈旧值；
- 并发 judge、stress、gen、WebUI 保存或 build 快照可能互相覆盖；
- 工作树变脏，文件可能被误提交、误同步到 `phtest` 或误打包；
- 作者手写同名文件可能被覆盖，或利用 include 搜索顺序遮蔽 Core 生成内容。

未来实现只能在命令私有临时目录中写一次固定名称的头文件，并通过受控编译参数引用；命令结束、失败和取消都清理临时目录。

## 4. 未来 opt-in Schema

### 4.1 fail-closed 的版本门禁

推荐让 constraints-enabled 问题使用新的**问题文件 Schema 版本**，而工作区 `.probhub/workspace.yaml` 仍保持 Workspace Schema v1：

```yaml
schema_version: 2
id: L01
name: 示例题

constraints:
  version: 1
  values:
    test_count_max: 10
    n_min: 1
    n_max: 200000
    value_min: -1000000000000000000
    value_max: 1000000000000000000
```

问题 `schema_version: 2` 是旧 Core 的 fail-closed 门禁：当前 Core 会拒绝未知问题 Schema，而不是静默忽略 `constraints` 后继续构建。`constraints.version` 则独立版本化约束模型、Token 和头文件序列化协议。未来 Core 应继续读取无 constraints 的问题 Schema v1，并只对显式迁移的问题启用 Schema v2。

如果未来选择在问题 Schema v1 内直接增加该字段，就必须接受旧 Core 会静默忽略它的前向兼容风险；本设计不推荐这种发布方式。

### 4.2 `constraints.version: 1` 的数据模型

v1 只接受一个非空的 `values` mapping，值域严格限定为有符号 64 位整数：

- `version` 必须是整数 `1`；布尔值、字符串 `"1"` 和其他版本都拒绝。
- `values` 必须是 mapping，建议最多 256 项，避免无界生成文件和 WebUI payload。
- key 必须是 ASCII lower snake case，建议正则：`[a-z][a-z0-9]*(?:_[a-z0-9]+)*`，长度 1..64。
- key 不得是 C++17 关键字或替代运算符关键字；不得包含点、空白、引号、斜杠、反斜杠、花括号或控制字符。
- key 大小写敏感；v1 不做自动大小写或命名风格转换。
- value 必须是 YAML integer，且满足 `-9223372036854775808 <= value <= 9223372036854775807`。
- `true`/`false` 虽在 Python 中属于 `int` 子类，仍必须拒绝；浮点、字符串、null、列表和 mapping 都拒绝。
- `constraints` 内只允许 `version` 与 `values`；未知子字段拒绝，不能静默忽略拼写错误。
- 必须检测 `values` 的重复 YAML key；不得依赖 YAML loader 的“最后一个覆盖前一个”。为保持来源明确，v1 也应拒绝该块内的 merge key/alias 展开。

v1 不支持字符串、单位、数组、表达式、派生公式或浮点数。需要表达 `sum(n) <= 200000` 时，作者应声明独立整数键，例如 `sum_n_max: 200000`；语义关系仍由题面和 Validator 表达。

## 5. 题面 Token 契约

### 5.1 唯一语法

Token 只在 `problem.md` 中生效，语法固定为：

```text
{{constraints.<name>}}
```

例如：

```markdown
输入一个整数 $n$（${{constraints.n_min}}\le n\le {{constraints.n_max}}$）。
```

v1 不允许空格、过滤器、默认值、函数、索引、算术或任意模板表达式。以下都必须作为 lint error，而不是尽力猜测：

```text
{{ constraints.n_max }}
{{constraints.n_max + 1}}
{{constraints["n_max"]}}
{{constraints.missing}}
{{constraint.n_max}}
```

实现应使用固定扫描器，而不是 Jinja、`eval`、Python format string 或 Typst 表达式。任何以 `{{constraints` 开始但不满足完整语法的片段都报告 `constraint_token_malformed`；引用不存在的 key 报告 `constraint_token_unknown`；问题未启用 constraints 却出现合法 Token 时报告 `constraint_token_without_schema`。

### 5.2 替换结果

替换值是规范十进制 ASCII：

- 正数无 `+`；
- 零固定为 `0`；
- 负数使用单个 ASCII `-`；
- 不加千位分隔、不转科学计数法、不自动加入 Markdown/Typst 标记。

Token 可重复出现，所有出现位置必须由同一解析结果替换。v1 不提供转义语法；文档若要展示 Token 字面形式，应使用 HTML entity 或拆开花括号，避免被扫描器识别。

### 5.3 源文本与渲染文本必须分离

`problem.md` 始终保存 Token，不得在保存、checkpoint 或 build 后被数字回写。Core 应明确区分：

- source view：WebUI 编辑、revision、checkpoint 和 source hash 使用的原始 Markdown；
- rendered view：lint 的渲染校验、metadata、Typst/PDF 预览和正式 build 使用的临时展开结果。

Token 展开不得作用于 YAML、样例输入/答案、Typst 模板、文件路径或任意其他文件。样例仍以 `data/sample/*.in`/`.ans` 为唯一事实来源。

## 6. 临时 `probhub_constraints.hpp`

### 6.1 固定接口

启用 constraints 后，Core 在命令私有临时目录生成固定文件：

```text
<temporary>/generated/probhub_constraints.hpp
```

作者代码使用固定 include：

```cpp
#include <probhub_constraints.hpp>
```

编译器参数把 Core 创建的 `generated/` 目录放在所有用户 include 目录之前。配置中不提供 header 文件名、输出目录或 namespace 自定义项，避免路径注入和不同入口产生不同 ABI。

头文件使用 UTF-8/ASCII、LF、稳定 key 排序和固定 C++17 结构，例如：

```cpp
#pragma once

#include <cstdint>
#include <limits>

namespace probhub_constraints {
inline constexpr std::int64_t n_max = std::int64_t{200000};
inline constexpr std::int64_t value_min = std::int64_t{-1000000000000000000};
}
```

`INT64_MIN` 不能直接依赖有歧义的十进制负字面量，生成器应使用 `std::numeric_limits<std::int64_t>::min()` 或等价的固定安全形式。头文件内容只能由已验证的 identifier 和整数序列化产生，不能拼接任意 YAML 字符串。

### 6.2 生命周期与同名冲突

- 每个问题、每次命令只解析一次 constraints，并让该命令内所有 C++ 角色共享相同的 header bytes/hash。
- 头文件在外部进程启动前完整写入；写入失败时返回结构化错误，不启动编译器。
- 临时目录由 Core 创建并持有，成功、失败、取消和异常退出路径都清理。
- `code/` 下任意层级出现用户文件 `probhub_constraints.hpp` 时，constraints-enabled 问题必须 lint 失败，建议诊断码 `constraints_reserved_header`，防止 `#include "..."` 或 include 顺序遮蔽生成文件。
- 文档与 scaffold 只示范 angle-bracket include；不支持把该文件手工复制进 `code/`。

### 6.3 DOMjudge 输出验证器闭包

当前 DOMjudge 包会复制 custom Checker/Interactor 源码和 `testlib.h` 到 `output_validators/validate/`。若这些源码可 include 约束头文件，package/build 必须把同一 header bytes 复制到该受控生成目录，并在临时验证编译和 ZIP 中包含它。标准题的本地输入 Validator 是否进入 DOMjudge 包仍遵循现有产品边界；本设计不借机宣称已增加 DOMjudge 输入 Validator 打包。

## 7. 三条执行路径必须同步接入

未来应新增单一 Core 模块（例如 `probhub/constraints.py`），负责：

1. 解析并验证 Schema；
2. 计算规范 constraints fingerprint；
3. 扫描/展开 statement Token；
4. 生成确定性 header bytes；
5. 以 context manager 物化临时 include 目录；
6. 为各编译入口提供同一 `include_dir` 与 `header_digest`。

各路径的最低接入面如下：

| 路径 | 必须使用同一约束上下文的角色 | 额外要求 |
|---|---|---|
| `judge` / local Judge | Validator、全部 solutions、Checker、Interactor | 编译 fingerprint 和 sandbox cache 必须包含 header 身份 |
| `stress` / replay / `--against` / `--fixate` | Generator、Validator、accepted、brute/target、Checker | replay 仍运行当前约束；反例 metadata 记录原 constraints hash；fixate 输入栅栏包含该 hash |
| `gen` plan / `--apply` | recipe Generator、Validator、首个 accepted、Checker | plan 仍只读；gen manifest 记录 constraints hash；manual-only 运行无需启动编译器 |
| `package` / `build` | DOMjudge Checker/Interactor 的临时验证编译 | 输出验证器目录携带同一受控 header |
| typeset / WebUI preview | 无 C++ 角色 | metadata 必须使用 rendered view，源 Markdown 不变 |

所有 C++ 角色都获得同一 include 环境，即使某个源文件当前没有 include 该头文件。v1 对 Python 或预编译可执行文件不注入环境变量、JSON 或命令行参数；它们仍可参与现有流程，但不能通过 C++ header 读取约束。若未来要支持语言无关消费，应另立版本化协议，不能临时复制业务逻辑。

## 8. 编译 fingerprint、缓存与本地 evidence

### 8.1 规范 fingerprint

constraints fingerprint 应至少绑定：

- constraints 协议版本；
- 按 key 排序后的 `(name, signed-int64-value)` 规范序列；
- statement Token 渲染协议版本；
- C++ header 序列化协议版本；
- 精确 header bytes 的 SHA-256。

不能只依赖 `probhub.yaml` 文件摘要：生成头文件位于临时目录，现有依赖扫描不会自动看到它；同时 Core 的序列化规则升级也需要使旧编译身份失效。

### 8.2 sandbox cache

local Judge 的每个 C++ `source_fingerprint` 必须显式加入 constraints fingerprint/header digest。这样 Validator cache、Case cache、Checker/Interactor fingerprint 和校准探针会通过程序 fingerprint 级联失效。

实现落地时必须提升 `SANDBOX_CACHE_SCHEMA_VERSION`；以当前值 4 为基线时应提升到 5。若其他改动先提升版本，则 constraints 改动再提升一次，不能复用已经发布的旧版本号。`sandbox-cache-v1.json` 文件名可保持不变，旧内部 Schema 整体拒绝。

constraints 值变化应保守地使该问题所有 C++ 编译 miss，而不是尝试静态判断某个源文件是否真的 include 了头文件。正确性优先于少量不必要的重编译。

### 8.3 stress、gen 与 evidence

- stress 不使用 sandbox cache，但 compile events 和保存的反例 metadata 应包含 constraints hash，便于确认反例产生时的约束版本。
- replay 按现有语义运行当前代码和当前 constraints；原 hash 不匹配只作为可见诊断，不能偷偷使用已保存的旧头文件。
- `--fixate` 的捕获/复核快照必须包含 constraints hash，运行期间变化时返回 `fixate_inputs_changed`，不得发布数据。
- gen manifest 需要提升内部 Schema，并为每个生成结果或整个 manifest 记录 constraints hash；Generator/Validator/accepted 因约束变化而产生的数据不能伪装成旧配方证据。
- sandbox cache Schema 提升会使现有 Judge calibration evidence 的 cache-schema 身份过期；constraints-enabled 题迁移后必须重新运行完整 Judge。不得沿用旧余量数据。

## 9. WebUI round-trip 契约

WebUI 必须把“可编辑源文本”和“渲染预览”分开：

- `/api/data` 的可编辑 statement 字段返回原始 Token，不返回已展开数字来替代源文本。
- 预览可额外返回只读 rendered statement，或在服务端临时构建 rendered metadata；预览结果不得进入 POST payload 的源字段。
- constraints 应作为结构化对象或有序键值行加载和保存；服务端仍以 live `probhub.yaml` deep copy 为基础，保留与本编辑无关的已知/未知字段。
- 现有 `_revision` 必须继续覆盖 `probhub.yaml` 与 `problem.md`，所以并发修改 values 或 Token 会返回 `source_conflict`。
- 未修改的 GET → POST 必须保持工作区 byte-for-byte 只读。
- 修改其他题面字段后，合法 Token 的字面形式必须原样保留，不能被保存为当前数字。
- 非法 name/value/version、未知 Token 或 malformed Token 在保存后的完整 lint 中失败，并原子回滚全部配置、题面和样例改动。
- WebUI 导航、预览和保存仍不得隐式刷新 PDF、ZIP、metadata 或 Manifest；只有显式分发调用 Core build。

最低 round-trip 断言是：源中 `{{constraints.n_max}}` 经 WebUI 加载、编辑其他字段、保存和重新加载后仍是完全相同的 Token，而预览显示规范数字 `200000`。

## 10. hash、status、checkpoint、seal 与 build

### 10.1 hash 影响

- `source_hash`：继续包含原始 `probhub.yaml` 和 `problem.md`，并在 constraints-enabled 问题中额外加入 constraints fingerprint 这一虚拟输入。这样 Core 序列化协议变化也能使旧 seal/build 失效。
- `data_hash`：仅跟踪 `.in`/`.ans` 字节；只改 constraints 不直接改变 data hash。若 gen 产物变化，应用后由数据字节自然改变。
- `collection_hash`：使用 rendered metadata。只有约束值被题面 Token 使用并改变排版输入时，整场 collection hash 才变化；只供 C++ 使用的约束值不应无故使其他题 PDF stale。
- `workspace_hash`：问题级 constraints 不改变工作区文件 hash。

lint/status 的结构化结果应增加 constraints 状态和 hash，例如 `disabled`、`current`、`invalid`。Build Manifest 和 checkpoint metadata 可增加可选 `constraints_hash` 便于诊断；constraints-enabled 的旧 Manifest 缺少或不匹配该值时必须 stale。仅增加可选诊断字段不要求预先指定新的 Manifest schema 号；若实现把它变成必需字段，则应按现有规则提升 Manifest schema 并让旧 Manifest 明确 stale。

### 10.2 checkpoint、seal 与正式构建

- checkpoint 只复制 `probhub.yaml`、`problem.md` 等规范源，不复制临时 header；读取 revision 时重新生成并核对 fingerprint。
- seal 的 lint、judge、stress 全部使用同一 revision 的约束上下文；任一步看到不同 hash 都失败。
- BuildPlan 捕获 constraints hash；快照从规范源重新生成 header 和 rendered statement，不从 live 临时目录复制。
- 发布前 live constraints 或 Token 变化属于现有 `inputs_changed` / `sealed_revision_changed` 栅栏，不得发布。
- header 物化、Token 渲染、Judge、Typst 或 package 任一步失败，都不能覆盖最后一份正确 PDF、ZIP、metadata 或 Manifest。
- status 不检查某个临时 header 文件是否存在；临时文件不是正式产物，状态只比较规范源、派生 fingerprint 和正式产物 hash。

## 11. 路径、注入与资源安全

未来实现必须满足以下边界：

1. Header 文件名、namespace、输出路径和 include 参数全部由 Core 固定，Schema 不能配置路径。
2. Header 位于新建的命令私有临时目录，不跟随题目目录中的符号链接；创建后按固定 bytes 写入，不允许用户文件覆盖。
3. 编译命令继续使用 argv 数组调用共享进程控制，不经 shell，不拼接命令字符串。
4. key 先通过 ASCII identifier 与 C++ reserved-word 校验，value 先转为受界 Python integer，再由固定 serializer 输出；不能把原始 YAML scalar 直接写入 C++。
5. Token scanner 不执行表达式、不读取环境变量、不访问属性/文件/网络，也不把 key 当作路径。
6. constraints 条目数、key 长度和最终 header 大小应有确定上限；超限在启动外部程序前失败。
7. `code/probhub_constraints.hpp`、大小写碰撞名和符号链接同名文件都应拒绝，Windows/Linux 行为一致。
8. DOMjudge 输出验证器只复制已知固定文件到受控目录；ZIP 路径仍经过现有安全校验。
9. WebUI 只把 raw Token 当文本，rendered value 只有数字；不得把 constraints 接入 HTML/JS 动态求值。
10. 本功能不改变本地沙箱边界，也不得宣称生成头文件使敌意代码可安全执行。

## 12. 迁移与兼容策略

### 12.1 存量题

无 constraints 的问题继续使用现有问题 Schema v1，行为、文件布局和交付流程不变，不做批量自动迁移。约束对账报告继续用于人工发现题面数字与 Validator 字面量差异。

### 12.2 单题 opt-in 迁移

未来发布支持后，建议逐题执行：

1. 把该题问题文件迁移到受支持的新问题 Schema 版本。
2. 添加 `constraints.version: 1` 与所需 signed-int64 values。
3. 把题面中对应的重复数字改为严格 Token。
4. 在 C++ Validator/Generator 等需要共享值的源码中 include 固定头文件并替换硬编码。
5. 运行 lint、WebUI round-trip 测试、`judge --no-cache`、配置存在时的 stress、`gen` plan。
6. 需要更新生成数据时审阅 plan 后再 `gen --apply`。
7. 重新 checkpoint、seal、批量 build、status 和 verify-package。

迁移可以只登记一部分确定的整数约束；Schema 不宣称自动覆盖字符集、互异性、连通性、浮点误差等语义约束。未迁移的规则仍由题面、Validator 和约束对账报告人工维护。

### 12.3 版本兼容

- 当前 Core：问题 Schema v2 会被拒绝，避免把 constraints-enabled 题静默当普通题构建。
- 新 Core：继续支持问题 Schema v1；Schema v2 的 constraints 缺失、版本未知或结构非法时 fail closed。
- constraints block 自身未来扩展通过 `constraints.version` 升级；未知版本不得降级解释。
- 不提供“忽略 constraints 继续构建”的兼容开关，也不在 build 中自动把新 Schema 降回旧 Schema。

## 13. 建议的结构化诊断

未来实现至少应稳定区分：

| code | 场景 | 结果 |
|---|---|---|
| `constraints_requires_problem_schema_v2` | 旧问题 Schema 中出现 constraints | lint/命令失败，exit 1 |
| `constraints_unsupported_version` | `constraints.version` 非 1 | lint/命令失败，exit 1 |
| `constraints_invalid_values` | values 类型、name、重复 key、int64 范围或数量非法 | lint/命令失败，exit 1 |
| `constraint_token_without_schema` | 未 opt-in 却使用合法 Token | lint/命令失败，exit 1 |
| `constraint_token_malformed` | 近似 Token 不符合唯一语法 | lint/命令失败，exit 1 |
| `constraint_token_unknown` | Token 引用未声明 key | lint/命令失败，exit 1 |
| `constraints_reserved_header` | `code/` 中存在保留同名文件/链接 | lint/命令失败，exit 1 |
| `constraints_materialize_failed` | 临时 header 创建或写入失败 | 不启动外部程序，不写正式产物，exit 1 |

WebUI 对 Schema/Token 错误返回 HTTP 400 并回滚；revision 冲突继续返回 HTTP 409 `source_conflict`。命令的 JSON/JSONL 输出不得只给自然语言，至少包含稳定 `code`、问题 ID 和相关 key/Token 位置。

## 14. 完整验收测试矩阵

以下测试全部通过前，constraints 不应从“评估”改为“已支持”。

### 14.1 Schema 与规范化

- 无 constraints 的问题 Schema v1 全部现有测试保持通过。
- 当前 Core 对未来问题 Schema 版本 fail closed。
- `version: 1` + 空/正常 values 的既定策略有测试；版本为 bool、string、0、2 均拒绝。
- 接受 `INT64_MIN`、`0`、`INT64_MAX`；拒绝上下越界、bool、float、string、null、list、mapping。
- 接受合法 lower snake case；拒绝 C++ 关键字、大小写、连续/尾随下划线、超长、非 ASCII、点、引号、路径分隔符、换行和花括号。
- 重复 YAML key、merge key/alias、未知 constraints 子字段均拒绝。
- 规范 fingerprint 和 header bytes 在 Windows/Ubuntu 相同。

### 14.2 Token

- 单个、重复、相邻 Token 和同一行多个 Token 正确替换。
- 正数、零、负数和两个 int64 边界使用规范十进制渲染。
- 未声明 key、未启用 Schema、空格变体、表达式、索引、缺括号和多余括号都产生对应稳定诊断。
- 不执行 Jinja/Python/Typst 表达式，恶意 key 文本不能进入执行路径。
- raw `problem.md` 在 lint、preview、checkpoint、seal、build 后字节不变；metadata/PDF 使用 rendered 值。
- Token 变化和 values 变化都使相关 source/collection 身份按设计更新。

### 14.3 Header 与编译

- 生成头文件可由 C++17 Validator include，并读到所有边界值。
- `INT64_MIN` 在 Windows g++ 和 Ubuntu g++ 编译及运行正确。
- Header key 顺序稳定、LF 稳定、内容 hash 稳定。
- 头文件只存在于临时目录；成功、CE、TLE、取消、异常后 `code/` 和工作区均无残留。
- 用户同名文件、大小写碰撞和符号链接被拒绝，不发生遮蔽或覆盖。
- include dir 排在用户目录前，编译命令不经 shell。

### 14.4 Judge 与缓存

- Validator、accepted、brute、wrong、Checker、Interactor 各至少一个 fixture 能 include 同一头文件。
- 相同 constraints 第二次 Judge 命中预期编译/逐点缓存。
- 任一 value、constraints 协议版本或 header serializer 身份变化都会产生 compile miss，并使相关 Validator/Case/Probe cache 失效。
- 旧 sandbox cache Schema 被整体拒绝；缓存文件名仍可兼容。
- constraints 迁移后旧 calibration evidence 为 stale，完整成功 Judge 才原子发布新 evidence；失败 Judge 不覆盖旧证据。

### 14.5 Stress、replay、against 与 fixate

- Generator、Validator、accepted、brute/target 和 Checker 同时依赖约束头文件时，普通 stress 与 custom stress 均通过。
- replay 使用当前 constraints，并在结果中显示原/当前 hash 是否不同。
- `--against` killer、`killer_confirmed` 和 `not_separated` 语义不变。
- fixate 捕获后修改 constraints 会触发 `fixate_inputs_changed`，不写 `.in`、`.ans` 或 YAML。
- 保存的反例 metadata 含 constraints hash，但不保存或复用临时 header。

### 14.6 Gen

- Generator、Validator、首个 accepted 和 Checker 同时依赖头文件时，plan/new/changed/unchanged 与 `--apply` 正确。
- constraints 变化能改变生成输入或答案，并在 gen manifest 中体现新 hash。
- manual-only 运行不要求编译器，也不产生临时 header 残留。
- 任一编译/运行/Token/Schema 错误保持 plan 只读、apply 零写入；事务回滚与恢复仍通过。
- 运行期间 constraints 变化命中现有输入栅栏，不发布混合版本数据。

### 14.7 WebUI

- GET 返回 raw Token，预览显示 rendered 数字。
- 未修改 GET → POST byte-for-byte 只读。
- 编辑题名、限制、题面其他段落或样例后，constraints values 与 Token 不丢失、不展开回写。
- 编辑 value 后预览刷新，source Token 不变，revision 更新。
- stale revision 返回 409，非法 Schema/Token 返回 400，所有源文件原子回滚。
- 导航、预览和保存不修改 PDF、ZIP、metadata、Manifest 或 `code/`。

### 14.8 Hash、seal、build 与包

- values 变化使本题 source hash、checkpoint/seal 和 constraints hash stale。
- 被题面使用的 value 变化使 collection hash 和相关全卷产物 stale；仅 header 使用的 value 不改变无关题目的 collection hash。
- data hash 只在实际 `.in`/`.ans` 变化时改变。
- build 快照重新物化相同 header；live 变化触发 `inputs_changed`/`sealed_revision_changed`。
- header/Token/Judge/Typst/package 任一步故障都不覆盖最后正确正式产物。
- custom Checker/Interactor include 头文件时，`output_validators/validate/` 临时验证编译成功，ZIP 含固定头文件且 verify-package 通过。
- status 不依赖临时文件存在，Manifest/checkpoint 中的 constraints hash 可正确诊断 stale。

### 14.9 安全与跨平台

- 恶意 name 无法注入 `#include`、namespace、声明、编译参数或文件路径。
- 不允许配置绝对路径、`..`、驱动器路径、UNC 或 alternate separator 作为 header 位置。
- 大量 values、超长 key 和异常 YAML 在启动外部程序前有界失败。
- Windows 与 Ubuntu CI 均覆盖 Schema、Token、header 编译、cache miss/hit、stress、gen、WebUI round-trip、build/package。
- 测试和文档继续明确：ProbHub 是可信本地出题环境的资源约束工具，不是敌意代码安全容器。

## 15. 重新评估的进入条件

只有同时满足以下条件，ROADMAP 才应把本项从“评估”推进为实现：

1. 接受问题 Schema 新版本的兼容决策，并确认旧 Core fail closed。
2. 先落地共享 `constraints` Core 模块，再接入 UI/CLI；不得复制解析或序列化逻辑。
3. Judge、stress、gen、package/build 四条编译路径在同一 PR 系列中闭环。
4. cache/evidence/gen manifest 的版本迁移和 stale 行为已有测试。
5. WebUI raw Token round-trip 与正式 rendered metadata 已明确分层。
6. Windows/Ubuntu 定向测试、`npm run check`、`npm run pack:check`、Skill 校验和真实工作区回归全部通过。

在此之前，约束对账报告仍应定位为人工审核工具，不能把启发式数字匹配或一个尚未生效的未知字段宣传成可执行的单一事实源。
