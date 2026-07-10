# 数据分组与解法宿命

ProbHub 可以把测试点映射到有业务含义的数据组，并为 accepted、brute、wrong 程序声明结构化预期。这样可以明确回答“哪组数据以什么状态击杀了哪个错解”，避免把偶然 RE/TLE 当成正确卡掉。

## 完整示例

```yaml
solutions:
  accepted:
    - file: code/std.cpp
      expected:
        status: AC
        all: true

  brute:
    - file: code/brute.cpp
      expected:
        status: [TLE, MLE]
        groups: [stress]
        forbid: [WA, FAIL]

  wrong:
    - file: code/wrong_int.cpp
      expected:
        status: WA
        groups: [overflow]
        forbid: [FAIL]

data:
  sample_dir: data/sample
  secret_dir: data/secret
  groups:
    - name: overflow
      role: wrong-solution-killer
      patterns: [secret/overflow*]
      targets: [code/wrong_int.cpp]

    - name: stress
      role: brute-killer
      patterns: [secret/stress*]
      targets: [code/brute.cpp]
```

旧写法仍兼容：

```yaml
solutions:
  accepted: [code/std.cpp]
  brute: [code/brute.cpp]
  wrong: [code/wrong.cpp]
```

## 数据组字段

- `name`：组名，题内唯一。
- `role`：说明性用途，例如 `wrong-solution-killer`、`brute-killer`、`boundary`；不直接改变判定。
- `patterns`：一个 glob 或 glob 列表。会同时匹配完整用例名（如 `secret/overflow1`）和文件基础名（如 `overflow1`）。
- `cases`：`patterns` 的兼容别名。
- `targets`：该组明确针对的解法路径，路径相对题目目录，例如 `code/wrong_int.cpp`。

未写 `patterns` 时，默认用组名作为前缀匹配，例如组 `overflow` 匹配 `overflow*` 和 `*/overflow*`。正式题目建议显式写 `patterns`，避免重命名数据后产生歧义。

同一测试点可以属于多个组。

## expected 字段

- `status`：目标状态或状态列表。
- `groups`：只在这些数据组内寻找目标状态。
- `all`：`true` 表示选中用例必须全部属于 `status`；`false` 表示至少一个选中用例属于 `status`。
- `forbid`：在该程序的全部测试点中禁止出现的状态。命中任意一个即失败。

当 `groups` 未显式配置时：

1. accepted 始终检查全部测试点；
2. brute/wrong 若被某个数据组的 `targets` 明确引用，则自动使用这些目标组；
3. 否则检查全部测试点；
4. 没有 `targets` 的普通分组不会自动缩小程序的宿命范围。

## 默认宿命

字符串形式的 solution entry 使用以下默认值：

```yaml
# accepted/std
status: [AC]
all: true
forbid: [WA, TLE, MLE, RE, FAIL]

# brute
status: [TLE, MLE]
all: false
forbid: [WA, FAIL]

# wrong
status: [WA, TLE, MLE, RE]
all: false
forbid: [FAIL]
```

因此 brute 即使出现了 TLE，只要另一个测试点出现 WA，仍会判定宿命失败。若要求错解必须被 WA 击杀而不能依赖 TLE/RE，应显式写 `status: WA`。

## 结构化结果

沙箱 JSONL 会输出：

- `groups`：全部分组定义；
- `case.groups`：每个测试点所属的数据组；
- `summary.expectation`：程序统计和宿命结果；
- `expectation`：独立宿命事件。

`expectation` 关键字段包括：

- `selected_cases`、`matched_cases`、`forbidden_cases`；
- `first_non_ac`：程序遇到的首个非 AC 用例；
- `first_expected_match`：首个满足目标状态的用例；
- `first_forbidden`：首个命中禁止状态的用例；
- `ok`：宿命是否满足。

宿命配置和数据组配置不参与逐点执行缓存键。仅修改 `expected` 或分组映射时，程序运行结果可以复用，但宿命断言会按新配置重新计算。
