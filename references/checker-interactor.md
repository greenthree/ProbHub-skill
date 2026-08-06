# Checker 与 Interactor 参考

本文件适用于 Workspace Schema v1。`judge.validator` 始终负责输入校验；Checker 和 Interactor 只负责输出/交互判定。

## 1. 自定义 Checker

配置：

```yaml
judge:
  type: custom
  validator: code/validator.cpp
  checker: code/checker.cpp
```

浮点题、非唯一答案题和需要忽略输出顺序的题目都使用 `type: custom`，不另设 `type: float`。

### 本地与 DOMjudge 协议

ProbHub 按 DOMjudge output validator 方式运行 Checker：

```text
checker <input-file> <answer-file> <feedback-dir>
```

选手输出从 Checker 的标准输入读取。使用附带 `testlib.h` 时，在 Checker 中调用：

```cpp
#include "testlib.h"

int main(int argc, char* argv[]) {
    registerTestlibCmd(argc, argv);

    // inf：输入文件
    // ouf：选手输出（stdin）
    // ans：官方答案

    quitf(_ok, "accepted");
}
```

本地状态映射：

| testlib/退出状态 | ProbHub 状态 | 含义 |
|---|---|---|
| `_ok` / `0` / `42` | `AC` | 选手输出正确 |
| `_wa`、`_pe` / `1`、`2`、`43` | `WA` | 选手输出错误 |
| `_fail` 或其他异常退出 | `FAIL` | Checker、官方答案或题目配置错误 |

`FAIL` 是题目基础设施失败，会直接导致沙箱失败，不能用于证明 wrong 被击杀。

### 浮点 Checker

建议显式控制绝对误差和相对误差，并拒绝非有限数：

```cpp
#include "testlib.h"
#include <algorithm>
#include <cmath>

int main(int argc, char* argv[]) {
    registerTestlibCmd(argc, argv);

    double expected = ans.readDouble();
    double actual = ouf.readDouble();
    if (!std::isfinite(actual))
        quitf(_wa, "non-finite output");
    if (!ouf.seekEof())
        quitf(_wa, "extra output");

    constexpr double ABS_EPS = 1e-6;
    constexpr double REL_EPS = 1e-6;
    double tolerance = std::max(ABS_EPS, REL_EPS * std::abs(expected));
    if (std::abs(actual - expected) <= tolerance)
        quitf(_ok, "accepted");
    quitf(_wa, "expected %.12f, found %.12f", expected, actual);
}
```

## 2. 交互题

配置：

```yaml
judge:
  type: interactive
  validator: code/validator.cpp
  interactor: code/interactor.cpp
  interactive:
    idle_limit: 1.0       # 双方多久无通信后判定空闲超时，单位秒
    transcript_limit: 65536 # 每个测试点最多保存的 Transcript 字节数
```

`idle_limit` 默认取 `min(limits.time, 2s)`，最小为 `0.1s`。`transcript_limit` 默认 `65536`；它是两个通信方向共享的原始字节预算，不是每个方向各自的额度，设为 `0` 可关闭 Transcript 内容记录。Core 会原子预留这份共享额度，并分别对两个方向做 UTF-8 增量解码，因此字符跨底层读取块时不会产生伪乱码；若额度本身截断了一个多字节字符，结果会标记 `truncated`，末尾可能出现替换字符。修改这些选项会使交互测试点缓存自动失效。

ProbHub 将选手程序与 Interactor 双向连接：

```text
Interactor stdout -> contestant stdin
contestant stdout -> Interactor stdin
```

Interactor 参数：

```text
interactor <input-file> <answer-file> <feedback-dir>
```

testlib 模板：

```cpp
#include "testlib.h"
#include <iostream>

int main(int argc, char* argv[]) {
    registerInteraction(argc, argv);

    int secret = inf.readInt();
    std::cout << secret << std::endl;  // 必须刷新

    int response = ouf.readInt();
    if (response == secret * 2)
        quitf(_ok, "correct response");
    quitf(_wa, "wrong response");
}
```

要求：

- 每次向选手程序发送信息后使用 `std::endl` 或显式 `flush`。
- 不要从真实 stdin 直接读取题目输入；题目输入由 `inf` 读取。
- 选手输出由 `ouf` 读取。
- 官方答案可由 `ans` 读取；如果不需要可以忽略。
- `_fail` 用于题目或 Interactor 自身错误，不能用于表示普通选手错误。
- 沙箱同时执行总时间限制和双向通信空闲超时。
- 沙箱终止超时进程，并捕获双方 stderr。
- JSONL 会输出 `transcript` 事件，条目方向为 `interactor_to_solution` 或 `solution_to_interactor`；两个方向共同消耗 `transcript_limit` 原始字节额度，超过上限时标记 `truncated`。

## 3. DOMjudge 打包

Schema v1 中只维护：

```text
code/checker.cpp
code/interactor.cpp
```

运行 `probhub package` 或 `probhub build` 时，Core 自动生成：

```text
output_validators/validate/
├── validate.cpp
└── testlib.h
```

同时生成：

```yaml
validation: custom
```

或：

```yaml
validation: custom interactive
```

`output_validators/` 是生成物，不要手工修改。修改 Checker/Interactor 后重新执行 `probhub judge <ID>` 和 `probhub build <ID>`。

## 3.1 Judge QA 主动测试

新题或修改过 Checker/Interactor 后，在 `probhub.yaml` 中增加 `judge.qa`，并把测试素材放在题目目录内：

```text
<problem>/
├── judge-fixtures/       # 显式 input、jury_answer、contestant_output
└── code/judge-qa/        # Interactor 的 C++/Python 模拟选手
```

配置必须使用 `schema_version: 1`。Checker fixture 可引用 `case: sample/<name>` 或 `secret/<name>`，也可互斥地提供 `input`、`jury_answer`；`contestant_output` 始终是题目目录内 `judge-fixtures/` 下的普通文件，期望状态为 `AC` 或 `WA`。Interactor fixture 的 `contestant` 必须二选一：`source: code/judge-qa/<file>`（`.cpp` 或 `.py`）或内建 `behavior: early-eof|idle|output-flood`，期望状态可为 `AC`、`WA`、`RE`、`TLE`、`MLE`、`OLE`，TLE 可附 `timeout_kind: idle|total`。

Checker 可额外声明：

```yaml
judge:
  qa:
    schema_version: 1
    robustness:
      baseline: accepts-alternative
      probes: [empty, truncated, extra-token, oversized]
    cases:
      - id: accepts-alternative
        purpose: valid alternative
        case: sample/basic
        contestant_output: judge-fixtures/checker/alternative.out
        expected: {status: AC}
```

`probhub judge-qa <ID> --no-cache` 会以正式 Validator、Checker/Interactor 和进程控制策略执行全部 fixture；每次都执行 fixture verdict，`--no-cache` 只强制重编译。Checker 自身 `_fail`、Interactor 崩溃、资源/进程树/清理故障是基础设施失败，不能用 `expected` 声明为成功。自动 Checker 探针返回 AC 时需要人工确认没有误放行。fixture 以原始字节计算 `fixture_hash`，按 Windows 大小写不敏感 ID/路径去重，并受数量、单文件和总字节上限约束；它们不进入 DOMjudge ZIP。

成功 evidence 只保存有界状态、期望/实际 verdict 和脱敏原因，不保存 stdout、stderr、feedback 正文或 transcript 条目。完整成功原子发布 `judge-qa-evidence-v1.json`；失败、取消、超时、输入变化或发布错误保留上一份成功 evidence。`lint`/`status` 将 evidence 报告为 `not-configured`、`missing`、`current`、`stale` 或 `invalid`，后三者是 warning；已配置题目的 `seal` 仍要求 Judge QA `passed` 且 evidence `current`。

## 4. 工艺守则

### Validator

- 把 Validator 当成"面对任意脏输入"的程序来写，不能假设输入已经基本合法。
- 对每一个后续会使用的量单独做范围检查；不要因为总规模变量合法，就省略字段级检查。
- 显式读入格式字符：题面要求单空格、单换行或严格顺序时，用 `readSpace()`、`readEoln()` 明确表达；结束必须 `readEof()`。
- 输入的结构约束也要检查：是否为树、图是否连通、是否为排列、编号是否互异、区间端点顺序、字符集、浮点格式。
- 错误信息尽量定位根因，不要让越界、漏边、重复点最后只报成一条模糊的次生错误。

### Checker

- Checker 首先判断"答案是否合法且正确"，不是复现选手算法。多解题先验证"是一个合法解"，再验证"满足目标"，不要只和标准输出逐项比较。
- 对每个参与判定的选手输出字段做合法性检查，尤其是点编号、边编号、区间端点、集合大小和浮点值；拒绝 NaN/inf/非法格式（浮点模板见第 1 节）。
- 原则上把 `ouf` 与 `ans` 都读到 EOF，防止多余输出或答案文件被截断而未被发现。

### Interactor

- 命令字、参数范围、查询次数上限、终止规则必须逐项校验，并与题面完全一致；题面未承诺的性质不得私自假设。
- 存在多种合法回答风格时，Interactor 应支持多策略，而不是固定一条回答路径。对自适应交互题，至少准备"能答 0 就答 0"与"能答 1 就答 1"两种策略。
- 需要按数据组切换策略时，可让 Generator 在输入中额外携带隐藏 `strategy` 字段：Validator 接受它，Interactor 读取它，题面对选手公开的协议保持不变。
- **Interactor 过弱会掩盖错解**：若某个错解在当前 Interactor 下看起来等价正确，优先怀疑 Interactor 回答太宽松，而不是认定错解不可卡。优先实现按需判定的轻量逻辑，避免为了实现方便预处理全部状态，反而把本该卡掉的高内存解法掩盖掉。

### 三方一致性

题面写了什么，Validator/Checker/Interactor 就按什么判。误差题的题面表述、Checker 误差实现与样例解释必须一致；几何题的题面范围、判定标准与数据设计必须一起收口，不要让题面允许出现危险边界而 Checker 靠宽松 `eps` 蒙混。错法枚举与配套数据设计见 `references/mistake-taxonomy.md`。
