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

`idle_limit` 默认取 `min(limits.time, 2s)`，最小为 `0.1s`。`transcript_limit` 默认 `65536`；设为 `0` 可关闭 Transcript 内容记录。修改这些选项会使交互测试点缓存自动失效。

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
- JSONL 会输出 `transcript` 事件，条目方向为 `interactor_to_solution` 或 `solution_to_interactor`；超过上限时标记 `truncated`。

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
