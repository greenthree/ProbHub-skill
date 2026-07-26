"""Compilable problem scaffolding for `probhub new`.

The scaffold is a small but fully working A+B example problem: it lints
clean and passes `probhub judge` out of the box, and demonstrates the
expectation matrix (a statement-level wrong solution killed by samples
and an implementation-level wrong solution killed by a targeted
`overflow` data group). Authors replace the example logic while keeping
the structure.
"""

JUDGE_TYPES = ("standard", "custom", "interactive")

_STATEMENT = """# {name}

> 本题面由脚手架生成，是一道可直接评测的 A+B 示例；请在保留结构的前提下替换为正式内容。范围与样例写法守则见 SKILL.md 第 5 节。

## 题目描述

给定两个整数 $a$ 和 $b$，求它们的和。

## 输入格式

输入一行两个整数 $a,b$（$-2\\times 10^9 \\le a,b \\le 2\\times 10^9$），以单个空格分隔。

## 输出格式

输出一行一个整数，表示 $a+b$。
"""

_VALIDATOR = """#include "testlib.h"

// 脚手架示例：与题面同步维护每个量的范围、字符集与格式检查。
int main(int argc, char* argv[]) {
    registerValidation(argc, argv);

    inf.readLong(-2000000000LL, 2000000000LL, "a");
    inf.readSpace();
    inf.readLong(-2000000000LL, 2000000000LL, "b");
    inf.readEoln();
    inf.readEof();
    return 0;
}
"""

_STD_BATCH = """#include <iostream>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    long long a, b;
    std::cin >> a >> b;
    std::cout << a + b << "\\n";
    return 0;
}
"""

_STD2_BATCH = """#include <cstdio>

// 第二正确实现槽位：正式出题时应换成与 std.cpp 不同思路的独立实现，用于交叉验证。
int main() {
    long long a, b;
    if (std::scanf("%lld %lld", &a, &b) != 2) return 1;
    std::printf("%lld\\n", a + b);
    return 0;
}
"""

_BRUTE_BATCH = """#include <iostream>

// 朴素但绝对正确的实现槽位。实现真实暴力后，把它登记进 probhub.yaml 的
// solutions.brute，并准备能让它 TLE/MLE 的 brute-killer 数据组；
// 未登记时 judge 不会运行它。
int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    long long a, b;
    std::cin >> a >> b;
    std::cout << a + b << "\\n";
    return 0;
}
"""

_WRONG_BATCH = """#include <iostream>

// 思路层示例错解：把加法写成了减法，会被样例直接击杀。
// 正式出题时按 references/mistake-taxonomy.md 三层枚举替换为真实错解。
int main() {
    long long a, b;
    std::cin >> a >> b;
    std::cout << a - b << "\\n";
    return 0;
}
"""

_WRONG2_BATCH = """#include <iostream>

// 实现层示例错解：结果被收窄进 int，在 overflow 定向数据组上溢出，
// 由 probhub.yaml 的 data.groups 指认击杀（见 secret/overflow01）。
int main() {
    long long a, b;
    std::cin >> a >> b;
    int sum = static_cast<int>(a + b);
    std::cout << sum << "\\n";
    return 0;
}
"""

_STD_INTERACTIVE = """#include <iostream>

int main() {
    long long a, b;
    std::cin >> a >> b;
    std::cout << a + b << std::endl;  // 交互题每次输出后必须刷新
    return 0;
}
"""

_STD2_INTERACTIVE = """#include <cstdio>

// 第二正确实现槽位：正式出题时应换成与 std.cpp 不同思路的独立实现。
int main() {
    long long a, b;
    if (std::scanf("%lld %lld", &a, &b) != 2) return 1;
    std::printf("%lld\\n", a + b);
    std::fflush(stdout);
    return 0;
}
"""

_BRUTE_INTERACTIVE = """#include <iostream>

// 朴素实现槽位；登记与击杀要求同批处理模式。注意交互题当前不支持 stress。
int main() {
    long long a, b;
    std::cin >> a >> b;
    std::cout << a + b << std::endl;
    return 0;
}
"""

_WRONG_INTERACTIVE = """#include <iostream>

// 思路层示例错解：把加法写成了减法。
int main() {
    long long a, b;
    std::cin >> a >> b;
    std::cout << a - b << std::endl;
    return 0;
}
"""

_WRONG2_INTERACTIVE = """#include <iostream>

// 实现层示例错解：结果被收窄进 int，被 overflow 定向数据组击杀。
int main() {
    long long a, b;
    std::cin >> a >> b;
    int sum = static_cast<int>(a + b);
    std::cout << sum << std::endl;
    return 0;
}
"""

_INMAKER = """#include "testlib.h"
#include <iostream>

// 数据生成器骨架：inmaker <type> <seed>，把单个测试点写到 stdout。
// type 用于区分数据档位（边界 / 随机 / 极限 / 定向卡错解），
// 数量与配比纪律见 references/mistake-taxonomy.md。
int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    long long limit = 2000000000LL;
    long long a = rnd.next(-limit, limit);
    long long b = rnd.next(-limit, limit);
    std::cout << a << ' ' << b << std::endl;
    return 0;
}
"""

_CHECKER = """#include "testlib.h"

// Checker 骨架：先验证选手输出合法，再判定是否正确；ouf 与 ans 都读到 EOF。
// 多解题先验"是一个合法解"，再验"满足目标"；写法守则见 references/checker-interactor.md。
int main(int argc, char* argv[]) {
    registerTestlibCmd(argc, argv);

    long long expected = ans.readLong();
    long long actual = ouf.readLong();
    if (!ouf.seekEof())
        quitf(_wa, "extra output");
    if (!ans.seekEof())
        quitf(_fail, "answer file has extra content");
    if (actual != expected)
        quitf(_wa, "expected %lld, found %lld", expected, actual);
    quitf(_ok, "accepted");
}
"""

_INTERACTOR = """#include "testlib.h"
#include <iostream>

// Interactor 骨架：把输入发给选手，读回并判定；每次输出后必须刷新。
// 命令字、参数范围、查询上限与终止规则必须与题面逐项一致，
// 写法守则见 references/checker-interactor.md。
int main(int argc, char* argv[]) {
    registerInteraction(argc, argv);

    long long a = inf.readLong();
    long long b = inf.readLong();
    std::cout << a << ' ' << b << std::endl;

    long long response = ouf.readLong();
    if (response != a + b)
        quitf(_wa, "expected %lld, found %lld", a + b, response);
    quitf(_ok, "correct sum");
}
"""

_DATA_FILES = {
    "data/sample/1.in": "1 2\n",
    "data/sample/1.ans": "3\n",
    "data/secret/random01.in": "-3 8\n",
    "data/secret/random01.ans": "5\n",
    "data/secret/overflow01.in": "2000000000 2000000000\n",
    "data/secret/overflow01.ans": "4000000000\n",
}


def scaffold_files(name, judge_type):
    """Return {relative_path: content} for a new problem directory."""
    if judge_type not in JUDGE_TYPES:
        raise ValueError(f"unsupported judge type: {judge_type}")
    interactive = judge_type == "interactive"
    files = {
        "problem.md": _STATEMENT.format(name=name),
        "code/validator.cpp": _VALIDATOR,
        "code/std.cpp": _STD_INTERACTIVE if interactive else _STD_BATCH,
        "code/std2.cpp": _STD2_INTERACTIVE if interactive else _STD2_BATCH,
        "code/brute.cpp": _BRUTE_INTERACTIVE if interactive else _BRUTE_BATCH,
        "code/wrong.cpp": _WRONG_INTERACTIVE if interactive else _WRONG_BATCH,
        "code/wrong2.cpp": _WRONG2_INTERACTIVE if interactive else _WRONG2_BATCH,
        "code/inmaker.cpp": _INMAKER,
    }
    if judge_type == "custom":
        files["code/checker.cpp"] = _CHECKER
    if interactive:
        files["code/interactor.cpp"] = _INTERACTOR
    files.update(_DATA_FILES)
    return files


def scaffold_config(problem_id, name, judge_type):
    """Return the probhub.yaml payload matching scaffold_files."""
    if judge_type not in JUDGE_TYPES:
        raise ValueError(f"unsupported judge type: {judge_type}")
    judge = {"type": judge_type, "validator": "code/validator.cpp"}
    if judge_type == "custom":
        judge["checker"] = "code/checker.cpp"
    if judge_type == "interactive":
        judge["interactor"] = "code/interactor.cpp"
    return {
        "schema_version": 1,
        "id": problem_id,
        "name": name,
        "display_name": name,
        "difficulty": None,
        "tags": [],
        "limits": {"time": 1, "memory": 256, "output": 64, "processes": 32},
        "statement": {"source": "problem.md"},
        "judge": judge,
        "solutions": {
            "accepted": [
                {"file": "code/std.cpp", "expected": {"status": "AC", "all": True}},
                {"file": "code/std2.cpp", "expected": {"status": "AC", "all": True}},
            ],
            "brute": [],
            "wrong": [
                {"file": "code/wrong.cpp", "expected": {"status": "WA", "forbid": ["FAIL"]}},
                {
                    "file": "code/wrong2.cpp",
                    "expected": {"status": "WA", "groups": ["overflow"], "forbid": ["FAIL"]},
                },
            ],
        },
        "generators": ["code/inmaker.cpp"],
        "data": {
            "sample_dir": "data/sample",
            "secret_dir": "data/secret",
            "groups": [
                {
                    "name": "overflow",
                    "role": "wrong-solution-killer",
                    "patterns": ["secret/overflow*"],
                    "targets": ["code/wrong2.cpp"],
                },
            ],
        },
        "domjudge": {"include_pdf": True},
    }
