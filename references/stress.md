# ProbHub Stress Differential Testing

`probhub stress` 用随机或定向小数据反复比较 accepted 与 brute，发现首个不一致后立即保存可重放反例。它适用于 `judge.type: standard` 和 `judge.type: custom`；当前不支持交互题。

## 1. 配置

在 `<problem>/probhub.yaml` 中增加：

```yaml
judge:
  type: standard
  validator: code/validator.cpp

solutions:
  accepted: [code/std.cpp]
  brute: [code/brute.cpp]

stress:
  generator: code/stress_generator.cpp
  args: ["{seed}", "{round}"]
  rounds: 1000
  time_limit: 5
  tool_timeout: 5
  accepted: code/std.cpp
  brute: code/brute.cpp
```

字段：

| 字段 | 必需 | 默认值 | 含义 |
|---|---:|---|---|
| `generator` | 是 | 无 | 每轮生成一个测试点的程序，路径相对题目目录且必须位于题目目录内 |
| `args` | 否 | `["{seed}"]` | 传给生成器的参数模板；也可写单个字符串 |
| `rounds` | 否 | `1000` | 未传 `--rounds` 时执行的轮数，必须为正整数 |
| `time_limit` | 否 | `max(limits.time * 2, 5)` 秒 | accepted 和 brute 每次运行的超时 |
| `tool_timeout` | 否 | `5` 秒 | generator、validator 和 Checker 每次运行的超时 |
| `accepted` | 否 | `solutions.accepted` 第一项 | 差分中的可信实现 |
| `brute` | 否 | `solutions.brute` 第一项 | 差分中的对照实现 |

`generator`、`accepted`、`brute`、`judge.validator` 和自定义题的 `judge.checker` 支持 `.cpp`、`.py` 或当前平台可直接执行的文件。C++ 会以 C++17、`-O2` 临时编译；构建目录在命令结束后删除，不写入 `code/`。

`accepted` 和 `brute` 覆盖项当前写文件路径字符串；若不覆盖，可以继续在 `solutions` 中使用字符串或带 `file` 的结构化条目。

### 资源控制

Stress 沿用题目的 `limits.memory`、`limits.output` 和 `limits.processes`：

```yaml
limits:
  memory: 256
  output: 64     # MiB，默认 64
  processes: 32  # 默认 32
```

Generator、Validator、accepted、brute、Checker 与编译器全部使用共享进程树控制。accepted/brute 使用 `stress.time_limit` 与题目资源限制；Generator、Validator、Checker 使用 `stress.tool_timeout` 及有界内部诊断输出。输出超限时会终止完整进程树并截断捕获文件；accepted/brute 记为 `OLE`，官方工具超限记为 `infrastructure`。即使某阶段的直接父进程已正常退出，其遗留后代仍会被清理。平台语义见 `references/process-control.md`。

### 自定义 Checker 配置

浮点题、非唯一答案题或 Token 级比较使用：

```yaml
judge:
  type: custom
  validator: code/validator.cpp
  checker: code/checker.cpp

stress:
  generator: code/stress_generator.cpp
  rounds: 10000
```

Checker 的完整模板、返回码和反馈文件见 `references/checker-interactor.md`。

## 2. Generator 协议

生成器每轮启动一次，并把**恰好一个完整测试点写到 stdout**。stdout 原样作为 Validator、accepted 和 brute 的 stdin；日志只能写 stderr，不能混入 stdout。生成器不应依赖交互式 stdin、当前时间或其他无法由 seed 恢复的状态。

`stress.args` 支持两个占位符：

- `{seed}`：本轮 seed；
- `{round}`：从 `1` 开始的轮号。

未传 `--seed` 时，ProbHub 随机选择一个非负 master seed，并在结果中输出。第 `round` 轮使用：

```text
seed = master_seed + round - 1
```

例如：

```cpp
#include <bits/stdc++.h>
using namespace std;

int main(int argc, char** argv) {
    long long seed = stoll(argv[1]);
    int round = argc >= 3 ? stoi(argv[2]) : 1;
    mt19937_64 rng(seed);

    int n = 1 + rng() % 20;
    cout << n << '\n';
    for (int i = 0; i < n; ++i) {
        cout << static_cast<int>(rng() % 101) << " \n"[i + 1 == n];
    }
}
```

对应配置：

```yaml
stress:
  generator: code/stress_generator.cpp
  args: ["{seed}", "{round}"]
```

为了可重现性，随机分支应只由传入 seed 决定。若生成器需要模式参数，可与占位符混用：

```yaml
args: ["--seed", "{seed}", "--round", "{round}", "--size", "20"]
```

## 3. 每轮执行与比较

每轮按以下顺序执行：

1. Generator 向 stdout 生成输入；
2. `judge.validator` 校验输入；
3. accepted 运行并产生可信输出；
4. brute 运行并产生待检查输出；
5. 按 `judge.type` 比较两份输出。

任一阶段失败或输出不匹配时停止，不再执行后续轮次，并保存首个反例。每个阶段都在完整进程树控制下运行；TLE、MLE、OLE、进程数超限或启动基础设施错误都会先清理所有后代再返回。

### `standard`

accepted stdout 是期望输出，brute stdout 是实际输出。比较规则与普通沙箱一致：

- 忽略整个输出首尾空白；
- 忽略每行末尾的空格和 Tab；
- 行内空格数量、非首行行首空白和内部换行结构仍须一致。

### `custom`

ProbHub 把 accepted stdout 写成 jury answer，把 brute stdout 作为 contestant output 交给现有 Checker。调用协议为：

```text
checker <input-file> <answer-file> <feedback-dir>
```

brute stdout 从 Checker stdin 传入。Checker 返回 AC 表示两份输出在题目语义下等价；WA 表示发现反例；FAIL、异常返回或超时属于评测基础设施失败。

### `interactive`

`judge.type: interactive` 当前不支持 `probhub stress`。`probhub lint` 会报告配置错误，直接运行也会以非零退出码拒绝。交互题继续使用 `probhub judge` 验证 Interactor 和选手程序。

## 4. 命令

使用题目稳定 ID：

```powershell
# 使用 probhub.yaml 中的 rounds，并随机生成 master seed
probhub stress L01

# 临时覆盖轮数
probhub stress L01 --rounds 10000

# 固定 master seed，便于复现完整随机序列
probhub stress L01 --rounds 10000 --seed 12345

# 可同时运行多题；任一题失败时总退出码非零
probhub stress L01 L03 --rounds 2000

# 机器可读结果；全局选项写在子命令前
probhub --json stress L01 --rounds 10000 --seed 12345
```

`--rounds` 必须为正整数，`--seed` 必须为非负整数。差分测试不会读取或写入 `sandbox-cache-v1.json`；每次命令都会重新临时准备程序并执行请求的轮次。

## 5. Replay

发现失败后，输出包含 `counterexample` 和可直接执行的 `replay_command`。也可以重放该题最近一次保存的反例：

```powershell
probhub stress L01 --replay latest
```

`--against` 猎杀产生的 `replay_command` 会同时保留目标路径，例如：

```powershell
probhub stress L01 --against "code/wrong.cpp" --replay "L01/.probhub/stress/<反例目录>"
```

不能删掉其中的 `--against`：否则命令会退回 accepted-vs-brute 的普通 stress 语义，无法确认同一个 killer。

显式传入反例目录或其中的输入文件：

```powershell
probhub stress L01 --replay "L01/.probhub/stress/20260710-120000-r37-s12381"
probhub stress L01 --replay "L01/.probhub/stress/20260710-120000-r37-s12381/input.in"
```

`--replay` 只能选择一个题目，路径必须位于该题的 `.probhub/stress/` 内；若 `metadata.json` 声明了其他题目 ID，命令会拒绝重放。Replay 会读取保存的 `input.in`，重新运行 Validator、当前 accepted、当前 brute 和当前 Checker；不会重新运行或编译 Generator，也不会覆盖原反例。修复代码后 replay 返回 `passed`，说明当前实现已在该输入上重新一致，但不代表其他随机输入都已通过，仍应继续执行新的 stress 轮次。

## 6. 反例目录

首个失败写入：

```text
<problem>/.probhub/stress/
├── latest.json
└── <UTC时间>-r<round>-s<seed>/
    ├── input.in
    ├── generator.out
    ├── generator.stderr
    ├── validator.out
    ├── validator.stderr
    ├── accepted.out
    ├── accepted.stderr
    ├── brute.out
    ├── brute.stderr
    ├── checker.stderr
    └── metadata.json
```

只会写入失败发生前已经产生的文件，因此较早阶段失败时部分文件可能为空或不存在。`metadata.json` 记录题目 ID、master seed、本轮 seed、轮号、生成器参数、程序路径、Judge 类型、失败分类和各阶段状态；`latest.json` 指向最近一次保存的反例目录。

`.probhub/stress/` 是本地诊断产物，应由 Git 忽略，不得打进 DOMjudge 包或提交到仓库。需要长期保留某个反例时，建议把 `input.in` 整理为命名清晰的 `data/secret/*.in`，生成对应 `.ans`，并加入数据组或回归测试。

## 7. 失败分类、结果与退出码

命令在正常完成时输出结构化对象；使用 `--json` 可稳定用于脚本。普通 stress 运行的关键字段包括：

- `ok`：是否所有请求轮次都匹配，或 replay 是否通过；
- `status`：`passed`、`counterexample` 或 `infrastructure`；
- `rounds_requested` / `rounds_completed`：请求轮数和失败前完整通过的轮数；
- `master_seed`、`seed`、`round`：随机序列和失败位置；
- `reason`、`message`：机器分类和诊断信息；
- `counterexample`、`replay_command`：保存位置和重放命令。

Replay 结果另外包含 `replay: true`、`artifact`、`input`、`seed` 和 `round`，但不包含普通运行的轮数统计或新的反例路径。

失败语义：

| 情况 | `status`/分类 | 典型 `reason` | 是否保存反例 |
|---|---|---|---:|
| 所有轮次匹配 | `passed` | — | 否 |
| accepted RE/TLE/MLE/OLE | `counterexample` | `accepted_re` / `accepted_tle` / `accepted_mle` / `accepted_ole` | 是 |
| brute RE/TLE/MLE/OLE | `counterexample` | `brute_re` / `brute_tle` / `brute_mle` / `brute_ole` | 是 |
| 输出不等价或 Checker 判 WA | `counterexample` | `output_mismatch` | 是 |
| Generator RE/TLE/MLE/OLE | `infrastructure` | `generator_re` / `generator_tle` / `generator_mle` / `generator_ole` | 是 |
| Validator 拒绝或异常 | `infrastructure` | `validator_rejected` | 是 |
| Checker FAIL、RE/TLE/MLE/OLE 或进程超限 | `infrastructure` | `checker_failed` | 是 |
| Schema、路径或编译错误 | 命令错误 | `error` 字段 | 通常否 |

进程正常退出且输出匹配才记为该轮通过。即使 brute 本来只用于小数据，brute 超时也会作为反例停止，通常说明生成规模过大或 `stress.time_limit` 太小。

CLI 退出码：

- `0`：所有所选题目的所有轮次通过，或 replay 通过；
- `1`：发现反例、基础设施失败、配置/路径/编译/参数错误，或多题中至少一题失败；
- `130`：用户中断（Ctrl+C）。

不要只匹配自然语言输出；自动化流程应检查退出码以及 `ok`、`status`、`reason` 和反例路径。

## 8. `--against`：给错解找刀

`stress --against <solution>` 把对拍对象从 brute 换成任意目标解法，语义随之翻转：**发现不一致就是成功**（产出 killer 候选），全部轮次一致则说明该错解在当前生成器分布下未被区分。

```powershell
probhub stress L05 --against code/wrong_greedy.cpp --rounds 200 --seed 7
probhub stress L05 --against code/wrong_greedy.cpp --fixate greedy-kill01 --rounds 200 --seed 7
probhub stress L05 --against code/wrong_greedy.cpp --fixate greedy-kill01 --group greedy-counterexample
```

状态语义（`--against` 模式）：

| 情形 | `ok` | `status` | 说明 |
|---|---|---|---|
| 目标输出与 accepted 不一致，或目标 RE/TLE/MLE/OLE | `true` | `killer_found` | 反例保存到 `.probhub/stress/`，可 `--replay` |
| 全部轮次一致 | `true` | `not_separated` | 加强生成器或放弃该错解模型 |
| accepted 自身失败 | `false` | `counterexample` | 标程被随机数据击穿，按正常 stress 反例处理 |
| Generator/Validator/Checker 失败 | `false` | `infrastructure` | 先修基础设施 |

`--replay` 配合 `--against` 复核已保存反例时，确认击杀返回 `killer_confirmed`。

### 一步固化（`--fixate`）

命中 killer 时，`--fixate <case>` 在工作区写锁内以一个可回滚事务完成三件事：

1. 归一化后的输入与 accepted 输出写入 `data/secret/<case>.in` / `.ans`；
2. `data.recipes` 追加配方（stress 生成器 + 命中轮的精确 argv）——之后 `probhub gen` 可字节一致地复现该测试点；
3. `data.groups` 追加（或并入 `--group` 指定的既有组）`wrong-solution-killer` 组，pattern `secret/<case>`、target 指向目标解法。

固化后按期望矩阵闭环：在 `solutions.wrong` 中为目标解法声明 `expected: {status: WA, groups: [<组名>], forbid: [FAIL]}`，再运行 `probhub judge` 确认击杀——期望矩阵仍是击杀判定的唯一事实来源。同名 case、数据文件或已有配方按大小写不敏感规则检查；冲突时以 `fixate_exists` 拒绝，不覆盖既有数据。

固化约束：

- `--fixate` 要求 `--against` 目标已在 `solutions.*` 声明（否则以 `fixate_undeclared` 拒绝）——未声明的目标写进 `data.groups.targets` 会直接 lint 失败。`--against` 本身可指向题目目录内任意文件，用于探索。
- 预检在开跑前完成：case 名不合法（`fixate_invalid`）、case 数据或配方已存在（`fixate_exists`）、目标未声明（`fixate_undeclared`）都立即失败，不浪费猎杀轮次。命中后在写锁内重读 live 配置，并复核配置以及 Generator、Validator、Checker、accepted 和目标源码；猎杀期间发生相关变化时返回 `fixate_inputs_changed`，不发布文件。
- Core 会用命中轮记录的精确 argv 重新运行 live Generator，并要求归一化后的输入逐字节一致；不确定生成器返回 `fixate_nondeterministic`。随后重新运行 live Validator、stress accepted、目标解法与 Checker，确认当前输入仍是 killer。固化的 `.ans` 始终重新运行 `solutions.accepted` 第一项生成，与 `probhub gen` 同源；Custom Judge 还会让 Checker 复核该 jury answer，失败返回 `fixate_answer_failed`。
- `.in`、`.ans` 和 `probhub.yaml` 先全部写入题目目录内的 staging，并在替换前写 journal；三者准备完成后才发布。任一写入或替换失败返回 `fixate_publish_failed` 并回滚。回滚不完整时以 `fixate_rollback_failed` 保留恢复材料；进程硬中断遗留的 journal 会在下次 fixate 取得锁后恢复。
- 既有组名、pattern 和 target 也按大小写不敏感规则合并；写回时统一使用本次 case 与声明解法的规范路径，避免在 Linux 上生成无法匹配的数据组。
- 目标进程无法启动（`start_error`，如不可执行/损坏的文件）不算击杀：按基础设施错误处理，不产生 `killer_found`。
- 固化会以规范形式重写 `probhub.yaml`（键序保留、注释不保留）；本项目规范源为机器可写 YAML，不在其中维护手写注释。

### 工作流纪律

- 先用小轮次（约 100）测量吞吐，再按预计时间至少 1.5 倍加 60 秒设置外层等待预算。
- 定向构造优先、随机分布只作补充：`not_separated` 通常意味着生成器分布需要向该错法的"死亡模式"倾斜（见 `references/mistake-taxonomy.md` 第 7 节）。
- `--against` 需要题目已有 `stress` 配置；尚未实现 brute 时可暂以 `stress.brute: code/std.cpp` 占位以通过 lint。
