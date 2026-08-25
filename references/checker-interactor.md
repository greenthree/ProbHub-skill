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

<a id="judge-qa-active-testing"></a>

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

### 3.1.1 设计模式：把 Checker 分成两层

对非唯一答案、构造和浮点题，先验证选手输出是一个合法 witness，再验证它满足题目目标。两层应使用不同的变量和失败原因，避免“能解析”被误当成“最优/正确”：

1. **格式与合法性层**：读取所有必需字段，检查 EOF、编号/边界、重复项、集合大小、几何退化、整数溢出和 `NaN`/`inf`。
2. **目标层**：在合法性已成立后，检查目标值、可行性、最优性或误差。不要把官方答案逐项复制成唯一格式，除非题面确实规定唯一输出。

官方答案只应提供可审计的目标值或参考信息。Checker 不应与答案生成器共享同一个未复核的浮点公式、最优值实现或构造假设；至少用小范围穷举、第二种数值方法或独立构造验证答案。Core 可以执行这些程序和 fixture，但不能自动证明 Checker 的数学关系或官方答案独立正确。

建议的 QA 最小矩阵：

| 类别 | 例子 | 期望 |
|---|---|---|
| 合法替代 | 不同但满足约束的 witness | AC |
| 格式错误 | 缺字段、非法 token、额外 token | WA |
| 合法格式但目标错误 | 非最优值、越界边、错误误差 | WA |
| 数值边界 | 误差阈值两侧、`NaN`、`+inf`、`-inf` | 按题意明确判定 |
| 官方/Checker 故障 | 答案截断、Checker `_fail` 或清理失败 | infrastructure FAIL |

`FAIL` 不应写入 `expected.status` 来“通过” fixture，也不能当作错解被击杀。`robustness` 探针返回 AC 时仍需人工确认它没有误放行。

### 3.1.2 设计模式：Interactor 状态机

交互题先画状态机，再写 Interactor 和题面。每条边都标出消息方向、语法、参数范围、计数点、刷新和下一状态：

| 状态 | 收到/发送 | 条件与动作 | 失败分类 |
|---|---|---|---|
| `START` | Interactor → 选手 | 发送初始数据并 `flush` | 发送失败/Interactor 崩溃：FAIL |
| `WAIT_QUERY` | 选手 → Interactor | 解析命令和参数，记录一次查询 | 非法命令/参数：WA；超过题目上限：WA |
| `ANSWER` | 选手 → Interactor | 验证最终答案，不再接受查询 | 正确 AC，错误 WA |
| `DONE` | Interactor → 选手/退出 | 发送终止信息并结束 | 提前 EOF、协议断裂：按题意 WA/RE |

查询计数、命令字、终止 token 和状态转移是具体题目的语义，必须由题面与 Interactor 共同实现。当前 Schema 不增加通用 `query_limit` 字段；`interactive.idle_limit` 只表示通信空闲时间，`transcript_limit` 只限制保存的通信字节，二者都不是查询次数上限。

至少主动覆盖：正常策略、相反/边界回答、非法命令或参数、超查询、提前 EOF、未 flush 导致的 idle、总时限、输出洪泛和 Interactor 自身 `_fail`。其中 Interactor/进程控制/清理故障必须保持 infrastructure 语义。

### 3.1.3 固定、自适应与隐藏策略

Interactor 可以采用以下策略，但选择必须服务于题面承诺，而不是让错误程序“碰巧通过”：

- **固定回答**：适合协议和基础边界回归；至少再准备一个相反边界，避免只测试一条路径。
- **自适应回答**：根据选手历史查询维护状态；用小状态空间穷举或独立模型检查状态转移，记录覆盖到的状态和边。
- **隐藏 strategy**：让输入携带 Validator 接受、题面不公开的策略编号；数据组分别选择相反策略，Interactor 读取该字段，选手仍只看到公开协议。

一个可审计的最小设计是 `strategy=0` 优先回答左分支、`strategy=1` 优先回答右分支，再用 `normal`、`always-left`、`always-right` 和 `query-over-limit` 四类 QA 选手验证。若某个错误解在所有策略下都 AC，先怀疑 Interactor 过弱；不能仅凭一次成功运行认定错解正确。

这些策略、查询上限和状态覆盖是人工设计与审查结论，不是 Core 自动推导的证明。错解分类继续遵循 [mistake-taxonomy.md](mistake-taxonomy.md)，验证深度遵循 [verification-modes.md](verification-modes.md)；Fixture evidence 只证明列出的输入、选手和协议分支。

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
