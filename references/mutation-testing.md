# std 变异测试

`mutation` 是对现有正式数据的补充检查：ProbHub 从首个 accepted C++ 标程生成少量、低歧义的语法变异体，再把每个变异体放进临时题目快照，用当前 Validator 和正式测试运行。它可以暴露“期望矩阵没有登记、但数据也没有区分”的薄弱点，不能证明不存在未知错解，也不能替代证明、独立标程、stress 或人工审查。

## 支持范围

首版只接受 `judge.type: standard` 和首个 accepted 的 `.cpp` 源码。Checker、Interactor、浮点比较和非 C++ 标程不在本切片内；它们仍使用 Judge QA 或普通 Judge 契约。当前支持的算子是：

| 算子 | 变换 | 目的 |
|---|---|---|
| `comparison-boundary` | `<`/`<=`、`>`/`>=`、`==`/`!=` 互换 | 检查边界和相等分支数据 |
| `boolean-negation` | 删除 `!` 或取反 `if`/`while` 条件 | 检查布尔分支覆盖 |
| `integer-boundary` | 只对十进制比较边界的常量尝试 `+1`/`-1` | 检查常量边界附近数据 |

源码先遮罩注释、字符串和字符字面量，再扫描 Token；不支持的 C++ 语法不会被猜测式替换。每个变异有稳定的 `cpp-token-v1` ID，计划由源代码、算子列表和上限共同决定。

## 使用

```powershell
probhub mutation L01 --no-cache
probhub mutation L01 --operator comparison-boundary --max-mutants 64
probhub mutation L01 --operator comparison-boundary --operator boolean-negation --timeout 300
```

也可以使用别名 `probhub mutate ...`。`--max-mutants` 范围为 1 至 256；未指定算子时运行首版全部算子。`--no-cache` 传给每个临时快照的 Judge，确保编译和逐点结果不复用旧缓存。命令退出码为 0 且 `status: passed` 时才表示本次变异执行完整；`survived` 是报告结果，不是命令失败。

每个变异分类为：

- `killed`：至少一个测试点区分了变异体；
- `survived`：所有已执行测试点都接受了变异体；
- `compile-invalid`：变异后源码无法编译，不计入可执行变异分母；
- `infrastructure-failed`：Validator、Judge、资源控制或清理失败，不能当作算法击杀。

## Evidence 与安全边界

成功运行才会原子写入：

```text
<ID>/.probhub/mutation-evidence-v1.json
```

证据包含 source/data hash、accepted 源路径、算子版本、计划 hash、编译器指纹、变异 ID、击杀用例 ID、分类计数和有界诊断。每个变异最多保留前 16 个命中详情，并用 `hit_cases_total` / `hit_cases_truncated` 说明完整数量；单份 evidence 最多 4 MiB。失败、取消、超时、证据超限、输入变化、锁竞争或发布故障不会覆盖上一份成功 evidence。证据文件只用于本地 `status`/`report`，不进入 PDF、ZIP、Manifest，也不应提交到 Git。

变异体和其临时编译产物始终位于临时快照；命令不会写回题目 `code/`、`probhub.yaml`、`data/` 或任何正式构建产物。mutation score 不作为 `build` 的硬门禁。看到幸存变异时，应阅读命中范围、补充边界/定向数据，再重新运行并比较同一 mutation ID；不要仅凭 score 宣称题目已被证明。
