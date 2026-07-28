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
    - file: code/reference_dp.cpp
      run_on: [reference-small]
      expected:
        status: AC
        groups: [reference-small]
        all: true
      independence:
        from: code/std.cpp
        basis: algorithm
        note: 使用独立 DP，而 std.cpp 使用贪心；两者不共享核心实现。

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
    - name: reference-small
      role: reference-domain
      patterns: [sample/*, secret/small*]

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

## `run_on` 运行域

结构化 solution 条目可以声明 `run_on: [groups]`，让较慢的参考实现只在可承受的数据组上运行。多个组取并集；所有 sample 无论是否命中这些组都始终隐式执行。未配置 `run_on` 时仍运行全部 sample 与 secret。

运行域只影响本地 `probhub judge`。它不改变 `stress` 选择的 accepted/brute，不改变 DOMjudge 包中的数据或正式评测语义，也不把局部参考解包装成完整标程。

约束如下：

- 配置顺序中的首个 accepted 必须全量运行，禁止声明 `run_on`；它仍负责样例快检、答案生成与完整正确性基线。
- 第二及后续 accepted 可以声明 `run_on`，但必须同时显式声明 `expected.groups`；这些期望组和由 `data.groups.targets` 隐式选出的目标组都必须落在 `run_on` 内。
- `run_on` 引用未知组、空组或没有匹配任何测试点的组会使 lint 失败。
- 同一测试点属于多个 `run_on` 组时只执行一次。
- `forbid`、expectation 与校准只检查实际执行域。结构化结果会公开 `run_on`、`executed_cases` 与 `skipped_cases`，局部结果不得被解释成全数据覆盖。

样例虽然始终执行，但只有显式属于 `expected.groups` 时才进入该组的目标状态断言；这让“执行域”和“宿命选择域”保持可区分。

## accepted 独立性声明

第二及后续 accepted 可用 `independence` 记录作者声明和人工复核依据：

```yaml
independence:
  from: code/std.cpp
  basis: key_implementation   # algorithm 或 key_implementation
  note: 独立实现状态转移，不 include 或复用 std.cpp 的核心函数。
```

- `from` 指向被互证的另一 accepted；`basis` 只能是 `algorithm` 或 `key_implementation`；`note` 必须非空并具体说明差异。
- Core 不会自动证明两个算法真正独立，这份声明仍需作者或审题人复核。
- Core 能确定的反证会直接阻断，例如两份源码字节相同、仅重复登记同一路径，或通过 `#include` 直接复用被声明独立实现的源码。
- `difficulty >= 4` 的题目若只有一个 accepted，且没有额外覆盖完整数据域的 AC 参考实现，会产生结构化 warning。是否“覆盖完整数据域”按实际用例集合判断：`run_on`、`expected.groups` 或 `data.groups.targets` 只要使 expectation 少于全部用例，就不能消除该 warning。

## expected 字段

- `status`：目标状态或状态列表。
- `groups`：只在这些数据组内寻找目标状态。
- `all`：`true` 表示选中用例必须全部属于 `status`；`false` 表示至少一个选中用例属于 `status`。
- `forbid`：在该程序的实际执行域中禁止出现的状态。命中任意一个即失败。

当 `groups` 未显式配置时：

1. 未声明 `run_on` 的 accepted 检查全部测试点；首个 accepted 强制如此；
2. brute/wrong 若被某个数据组的 `targets` 明确引用，则自动使用这些目标组；
3. 否则检查全部测试点；
4. 没有 `targets` 的普通分组不会自动缩小程序的宿命范围。

## 默认宿命

字符串形式的 solution entry 使用以下默认值：

```yaml
# accepted/std
status: [AC]
all: true
forbid: [WA, TLE, MLE, OLE, RE, FAIL]

# brute
status: [TLE, MLE, OLE]
all: false
forbid: [WA, FAIL]

# wrong
status: [WA, TLE, MLE, OLE, RE]
all: false
forbid: [FAIL]
```

因此 brute 即使出现了 TLE，只要另一个测试点出现 WA，仍会判定宿命失败。若要求错解必须被 WA 击杀而不能依赖 TLE/RE，应显式写 `status: WA`。

## 结构化结果

沙箱 JSONL 会输出：

- `groups`：全部分组定义；
- `case.groups`：每个测试点所属的数据组；
- `summary.expectation`：程序统计和宿命结果；
- `summary.calibration`：逐解法最大时间、TL 占比、缓存来源与期望 TLE/MLE/OLE 的资源击杀证据；
- `expectation`：独立宿命事件。

`expectation` 关键字段包括：

- `selected_cases`、`matched_cases`、`forbidden_cases`；
- `run_on`、`executed_cases`、`skipped_cases`：配置运行域与实际执行/跳过用例；
- `first_non_ac`：程序遇到的首个非 AC 用例；
- `first_expected_match`：首个满足目标状态的用例；
- `first_forbidden`：首个命中禁止状态的用例；
- `ok`：宿命是否满足。

宿命配置和数据组配置不参与逐点执行缓存键。仅修改 `expected`、`run_on` 或分组映射时，已有逐点结果仍可复用；Judge 会按新的运行域选择需要执行/复用的用例，并重新计算宿命断言与 evidence。

校准只在 expectation 选中的用例范围内寻找相应资源状态。组外 TLE/MLE/OLE 不能充当目标组的击杀余量。普通 TLE 事件在 TL 处即被终止，只能证明 `runtime >= TL`；因此 expected-TLE 余量使用额外延长时限探针，不能把普通 case elapsed 冒充为 `1.5 × TL` 实测值。

`expected.status: [TLE, MLE]` 表示两种状态任选其一即可满足宿命，不表示两种状态都必须出现。若实际由 MLE 满足，则只报告 MLE 证据，不额外制造缺失 TLE 的 warning。
