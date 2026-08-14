# std 变异测试

`mutation` 是对现有正式数据的补充检查：ProbHub 从首个 accepted C++ 标程生成少量、低歧义的语法变异体，再把每个变异体放进临时题目快照，用当前 Validator 和正式测试运行。它可以暴露“期望矩阵没有登记、但数据也没有区分”的薄弱点，不能证明不存在未知错解，也不能替代证明、独立标程、stress 或人工审查。

## 支持范围

首版只接受 `judge.type: standard` 和首个 accepted 的 `.cpp` 源码。Checker、Interactor、浮点比较和非 C++ 标程不在本切片内；它们仍使用 Judge QA 或普通 Judge 契约。当前支持的算子是：

| 算子 | 变换 | 目的 |
|---|---|---|
| `comparison-boundary` | `<`/`<=`、`>`/`>=`、`==`/`!=` 互换 | 检查边界和相等分支数据 |
| `boolean-negation` | 删除 `!` 或取反 `if`/`while` 条件 | 检查布尔分支覆盖 |
| `integer-boundary` | 只对十进制比较边界的常量尝试 `+1`/`-1` | 检查常量边界附近数据 |

源码使用固定的 `tree-sitter==0.26.0` 与 `tree-sitter-cpp==0.23.4` 构造 C++ 语法树，只定位函数或 lambda 复合语句体内的真实表达式。模板尖括号、运算符声明、`<=>`、预处理宏、concept/requires、`case` 标签、`static_assert`、`sizeof` / `decltype` / `noexcept` / `typeid` 等非执行或未求值上下文不会生成候选；注释、字符串和字符字面量也不进入语法候选。native binding 只在独立 worker 中加载；父进程通过版本化 JSON 协议核对解析器版本、源码哈希、位置与数量。worker 默认受 30 秒、512 MiB、4 MiB stdout/stderr 共享预算和 8 个进程限制，并响应 mutation 总 deadline 与取消请求。解析超时、资源超限、native 崩溃、畸形响应或可报告的语法失败都会结构化终止、清理完整进程树，不回退到 Token 猜测，也不覆盖上一份成功 evidence。

固定版本会把部分合法 `typeid(type-id)` 误解析为调用表达式。worker 只在全部语法错误都位于括号完整的 `typeid(...)` 参数内、参数内容可独立解析为单个 C++ type-id、并且等长替换该参数后的完整源码无其他语法错误时恢复定位；基础类型、限定类型、指针、数组、函数指针、elaborated type 与 `decltype(...)` 均覆盖定向测试。恢复只用于确认外围语法，整个 `typeid(...)` 仍是阻断上下文；缺括号、参数内语句注入、非法 type-id 或任何邻接错误继续以 `mutation_syntax_invalid` 失败闭锁。

每个变异继续使用稳定的 `cpp-token-v1` ID，计划由源码、`tree-sitter-cpp-v1` locator、算子列表、人工排除记录和上限共同决定。仍然有效的旧 ID 保持不变；旧 Token 扫描器产生但语法树不再接受的误报 ID 会成为 `unmatched`，需要作者复核后移除。解析器切换会使旧 evidence 显示 `stale`，不会把旧执行分类与新候选计划混用。

## 使用

```powershell
probhub mutation L01 --no-cache
probhub mutation L01 --jobs 2 --no-cache
probhub mutation L01 --operator comparison-boundary --max-mutants 64
probhub mutation L01 --operator comparison-boundary --operator boolean-negation --timeout 300
```

也可以使用别名 `probhub mutate ...`。`--max-mutants` 范围为 1 至 256；未指定算子时运行首版全部算子。`--no-cache` 传给每个临时 worker 的 Judge，确保编译和逐点结果不复用旧缓存。命令退出码为 0 且 `status: passed` 时才表示本次变异执行完整；`survived` 是报告结果，不是命令失败。

每次命令只从 live 题目捕获一次一致、不可变的 baseline；每个变异都从 baseline 建立独立 worker，不能继承其他变异的源码、编译产物或缓存写入。默认 `--jobs 1` 保持计划顺序串行执行；只有显式指定 `--jobs 2` 才使用 spawn worker 有界并行，结果仍按 mutation plan 顺序聚合。全局 `build.lock` 只覆盖事务恢复、baseline 捕获和最终输入围栏/证据发布，不覆盖长时间 Judge，因此 mutation 运行时不会阻塞其他遵守 Core 锁协议的题目写入。题目自身的 `mutation.lock` 仍覆盖整次命令，防止同题并发覆盖 evidence。

每个 worker 通过共享进程控制启动正式 Judge，并具有独立的 3600 秒监督器上限、128 MiB stdout/stderr 共享预算，以及根据题目限制增加固定余量的外层内存和进程预算。问题级调度器最多提供 2 个 worker token、8192 MiB Judge 内存额度和 160 个 Judge 进程额度；若一个 worker 的配置上限已经无法让两个任务同时满足额度，`requested_jobs: 2` 会降为 `effective_jobs: 1`。这些值是调度器用于限制同时活动 worker 的配置额度，不是宿主机资源预留，也不表示运行时实际消耗。

`--timeout` 是优先级更高的整题总 deadline。首个 `infrastructure-failed` 会立即停止派发新变异、协作取消活动 worker，并在 2 秒宽限后强制清理 worker 与后代进程树。已完成、失败和取消记录按计划顺序聚合；取消项只出现在本次失败结果的 `execution` 中，不计为 killed，也不进入成功 evidence。整题 deadline 到期返回 `mutation_timeout`，外部取消返回 `mutation_cancelled`，所有失败路径都保留上一份成功 evidence。

## 人工排除

第一次运行后，可以从 JSON evidence 或 `probhub report` 取得稳定 mutation ID。只有人工阅读变异位置并确认它等价、不适用或不能表达真实选手错误时，才在 `probhub.yaml` 记录：

```yaml
mutation:
  schema_version: 1
  exclusions:
    - id: cpp-token-v1:comparison-boundary:42:17:0123456789abcdef
      reason: Validator 保证 n >= 1，此处边界变化不改变可达输出
```

每项必须包含精确 ID 和非空理由；最多 256 项，理由最多 1024 字节。排除先于 `--max-mutants` 应用，不会让被排除项占用执行额度。计划与 report 并列给出：

- `raw`：当前算子从源码生成的全部候选数；
- `excluded`：命中当前计划的人工排除数；
- `effective`：排除后、限额前的候选数；
- `selected`：本次受 `--max-mutants` 限制后实际选择的数量；
- `out-of-scope exclusions`：ID 仍在完整当前计划中，但对应算子未在本轮选择；
- `unmatched exclusions`：配置存在但不再匹配当前源码计划的旧 ID 数量。

部分算子运行会把其他算子的有效 ID 标为 `out-of-scope`，不会误报 warning。旧 ID 真正失效时命令仍可完成，但 evidence/report 会保留记录并给出 `mutation_exclusion_unmatched` warning。不得为了提高 mutation score 排除幸存变异，也不得把人工理由当作机器证明。

每个变异分类为：

- `killed`：至少一个测试点区分了变异体；
- `survived`：所有已执行测试点都接受了变异体；
- `compile-invalid`：变异后源码无法编译，不计入可执行变异分母；
- `infrastructure-failed`：Validator、Judge、资源控制或清理失败，不能当作算法击杀。

并行失败结果还可能把尚未完成或未派发的项目标为 `cancelled`。它是本次执行状态，不是 mutation 分类，不进入成功 evidence 的 summary。

## Evidence 与安全边界

成功运行才会原子写入：

```text
<ID>/.probhub/mutation-evidence-v2.json
```

证据包含 source/data hash、accepted 源路径、算子与 locator 版本、计划 hash、当前 Core/编译器/解析器指纹、执行 profile、raw/excluded/effective/selected 计数、带三态匹配状态和理由的排除记录、变异 ID、击杀用例 ID、分类计数和有界诊断。执行 profile v2 记录 requested/effective jobs、串行或 spawn-bounded 调度、不可变快照模型、取消宽限、per-worker Judge 上限和问题级配置额度；它用于审计但不进入 mutation 计划 hash，也不会因为预算调整单独把旧结果标为 stale。旧 profile v1 或没有该可选字段的旧 schema v2 evidence 仍可读取。字段存在但结构、版本或预算无效时 evidence 为 `invalid`。每个变异最多保留前 16 个命中详情，并用 `hit_cases_total` / `hit_cases_truncated` 说明完整数量；单份 evidence 最多 4 MiB。失败、取消、超时、解析错误、证据超限、输入变化、锁竞争或发布故障不会覆盖上一份成功 evidence。evidence 过期时 report 重算当前计划与排除三态，但不继续展示旧执行分类。旧 `mutation-evidence-v1.json` 继续被 Git 忽略，但不作为当前证据读取。证据文件只用于本地 `report`，不进入 PDF、ZIP、Manifest，也不应提交到 Git。

变异体和其临时编译产物始终位于临时快照；命令不会写回题目 `code/`、`probhub.yaml`、`data/` 或任何正式构建产物。语法定位收紧后 raw/selected/compile-invalid 数量与 score 可能相对旧版本变化，这表示候选集合变化，不代表数据自动变强。mutation score 不作为 `build` 的硬门禁。看到幸存变异时，应阅读命中范围、补充边界/定向数据，再重新运行并比较同一 mutation ID；不要仅凭 score 宣称题目已被证明。
