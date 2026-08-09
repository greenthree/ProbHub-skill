# ProbHub Workspace Schema v1

## Workspace file

Path: `.probhub/workspace.yaml`

```yaml
schema_version: 1
contest:
  title: Contest title
  subtitle: 正式赛
  author: Team
  date: 2026年6月26日
typst:
  directory: typst-statement/正式赛
  creation_timestamp: 1782403200
  cover:
    logo: usts.png
    logo_width: 9cm
    logo_space_above: 0em
    logo_space_below: 0em
problems:
  - id: L01
    directory: L01
lint:
  forbidden_patterns: [TODO, FIXME, 114514, 待补充]
```

The order of `problems` is the official contest order. `id` is stable and does not change when the displayed letter changes.

`contest` 是赛事标题、封面副标题、作者和日期的规范源。`typst.cover` 可选，用于覆盖封面 Logo 和间距；Logo 路径必须留在 Typst 目录内，宽度和间距使用 Typst 长度。Core 排版时生成临时入口应用这些值，不修改正式 `main.typ` 或 `lib.typ`。

## Problem file

Path: `<problem>/probhub.yaml`

```yaml
schema_version: 1
id: L01
name: 数字重构
display_name: 数字重构
difficulty: 3
tags: [优先队列, 模拟, 贪心]
limits:
  time: 1
  memory: 256
  output: 64
  processes: 32
calibration:
  accepted_time_multiplier: 3.0
  expected_tle_time_multiplier: 1.5
statement:
  source: problem.md
judge:
  type: standard
  validator: code/validator.cpp
solutions:
  accepted:
    - file: code/std.cpp
      expected: {status: AC, all: true}
    - file: code/reference_dp.cpp
      run_on: [reference-small]
      expected: {status: AC, groups: [reference-small], all: true}
      independence:
        from: code/std.cpp
        basis: algorithm
        note: 使用独立 DP，而主标程使用贪心。
  brute:
    - file: code/brute.cpp
      expected:
        status: [TLE, MLE, OLE]
        groups: [stress]
  wrong:
    - file: code/wrong_greedy.cpp
      expected:
        status: WA
        groups: [greedy-counterexample]
generators: [code/inmaker.cpp]
stress:
  generator: code/stress_generator.cpp
  args: ["{seed}", "{round}"]
  rounds: 1000
  time_limit: 5
  tool_timeout: 5
data:
  sample_dir: data/sample
  secret_dir: data/secret
  groups:
    - name: reference-small
      role: reference-domain
      patterns: [sample/*, secret/small*]
    - name: greedy-counterexample
      role: wrong-solution-killer
      patterns: [secret/greedy*]
      targets: [code/wrong_greedy.cpp]
    - name: stress
      role: brute-killer
      patterns: [secret/stress*]
      targets: [code/brute.cpp]
  recipes:
    - case: greedy01            # secret 测试点主名（不含扩展名）
      args: ["greedy", "1"]     # 生成器 argv；同一 argv 必须复现同一字节
    - case: max01
      generator: code/inmaker.cpp   # 可省略，默认取 generators 第一项
      args: ["max", "7"]
    - case: handmade01
      manual: true              # 字节本身是事实来源，gen 不触碰
domjudge:
  include_pdf: true
```

### Test recipes (`data.recipes`)

每个 secret 测试点可声明来源配方：`manual: true`（手工数据，字节即事实来源），或生成器调用（可选 `generator` 路径 + 精确 `args` 列表）。case 名大小写不敏感去重（Windows 文件系统会塌缩 `gen01`/`GEN01`）；生成器、Validator 和 Checker 单次运行默认 60 秒超时，可用 `data.gen_tool_timeout`（秒，≤3600）覆盖；沙箱遵循 `limits.processes`。`probhub gen <ID>` 依配方生成输入 → Validator 过检 → 用首个 accepted 产 `.ans` → Custom Checker 复核，plan 模式报告 new/changed/unchanged（字节级一致才算 unchanged），`--apply` 才在工作区写锁内发布。配置会在取锁后重载，并在发布前再次核对；`.in`、`.ans` 与 gen manifest 全部先 staging，任一替换失败会回滚已有正式文件。输出统一 LF，保证 Windows/Linux 字节一致；生成证据写入本地 `.probhub/gen-manifest.json`（不提交，`--case` 局部运行按 case 合并）。交互题不支持 `gen`（答案由 Interactor 协议定义）。没有配方的 secret 测试点在 lint 中以 warning 呈现（存量数据兼容，不硬失败）。`stress --against --fixate` 命中后会先按原 argv 重放确认字节一致，再登记配方、数据组和目标错解。

### Resource limits

`limits` controls every local sandbox path, including ordinary solutions, Checker, Validator, compiler, Interactor, and stress roles:

| Field | Unit | Default | Meaning |
|---|---:|---:|---|
| `time` | seconds | `1` | Contestant wall-clock limit |
| `memory` | MiB | `256` | Contestant memory limit |
| `output` | MiB | `64` | Captured stdout + stderr budget for one contestant run |
| `processes` | processes | `32` | Maximum processes in the controlled process tree |

`time`, `output`, and `processes` must be positive integers. `memory` must be a power of two and at least `256` MiB. Output above the budget is terminated and reported as `OLE`, with captured files truncated. Official tools use bounded internal time/output policies and the same full-tree containment; their resource failure is infrastructure `FAIL`, not contestant WA. Windows uses Job Objects and refuses to run if containment cannot be established. Linux/Unix uses a separate process group, `RLIMIT_AS`, and low-frequency `/proc` tree monitoring. See `references/process-control.md`.

### Calibration policy

`calibration` 可选，用于本地 Judge 的余量诊断：

```yaml
calibration:
  accepted_time_multiplier: 3.0
  expected_tle_time_multiplier: 1.5
```

- `accepted_time_multiplier` 默认为 `3.0`，要求每个 accepted 的本机最大用时满足 `max_time × 3 <= TL`。必须是至少 `1` 的有限实数。
- `expected_tle_time_multiplier` 默认为 `1.5`。对期望状态包含 `TLE` 且确实在目标组被 TLE 的解法，Judge 会额外选择一个相关用例做延长时限探针；探针上限与验收阈值分离，默认至少运行到 `2 × TL`。必须是大于 `1` 的有限实数。
- TLE 探针不改变正常 Judge verdict 或宿命判断。程序若在探针上限前正常结束，记录精确本机用时；若仍超时，只记录 `runtime >= probe_limit` 的 `lower_bound`。
- MLE/OLE 的本地结果在触限时已被终止，summary 只报告峰值内存或截断前 stdout+stderr 总字节数的阈值下界；仅接近 ML 后异常退出的启发式 MLE 标为 `inferred`，无可靠遥测时明确为 `unavailable`，不得把缺失值当作零。
- 交互题不能脱离 Interactor 单独运行选手程序，当前 expected-TLE 延长探针明确标记为 `unavailable`；普通逐点最大时间仍会报告。

完整成功的 Judge 会原子更新本地忽略文件 `<problem>/.probhub/judge-evidence-v2.json`。lint/status 先按 evidence schema、source/data hash 与平台判断是否过期，再验证测量策略、结构和 solution 运行域；缺失、过期或无效证据只提示重新运行 Judge，不改变 lint 的 `ok` 或正式 build 的 `current/stale` 状态。失败、取消或超时不会覆盖上一份完整成功证据。

`expected.status` 列表表示可接受的备选结果；只有实际在 expectation 选中范围内发生的 TLE/MLE/OLE 才生成对应资源余量。程序已由 WA 或 MLE 满足宿命时，不会因为列表中同时允许 TLE 而产生虚假的 TLE 余量 warning。

`max_time` 与资源余量仅表示本次本机沙箱的观测结果。Windows 与 Linux/DOMjudge 在进程启动、静态链接、调度、计时和内存统计口径上可能不同；正式 TL、ML、OL 必须在目标 Linux 评测环境重新校准，本地结果不能作为评测承诺。结构化结果固定包含 `target_guarantee: false`。

### Judge modes

普通唯一答案题：

```yaml
judge:
  type: standard
  validator: code/validator.cpp
```

`standard` 比较会忽略整个输出首尾空白，以及每一行末尾的空格和 Tab。因此 `42` 与 `42   ` 等价；但行内多余空格、非首行的行首空白、缺少或增加的内部换行仍会导致 WA。需要更宽松的 Token 比较、浮点误差或非唯一答案时，应使用 `judge.type: custom` 和 Checker。

自定义 Checker（包含浮点题和非唯一答案题）：

```yaml
judge:
  type: custom
  validator: code/validator.cpp
  checker: code/checker.cpp
```

交互题：

```yaml
judge:
  type: interactive
  validator: code/validator.cpp
  interactor: code/interactor.cpp
  interactive:
    idle_limit: 1.0
    transcript_limit: 65536
```

`validator` 始终校验输入。`checker` 和 `interactor` 使用 ProbHub 附带的 DOMjudge/testlib 协议；完整参数、退出状态与模板见 `references/checker-interactor.md`。Core 会从规范源码生成 `output_validators/validate/validate.cpp` 和 `testlib.h`，不得手工维护生成目录。

### Judge QA Schema v1

`judge.qa` 是 custom/interactive 题的可选题目级主动测试配置。standard 题不能配置它；未配置的旧题保持兼容，但新 custom/interactive 题在 Agent 交付前必须配置并通过 Judge QA。所有 QA 路径都必须是题目目录内的普通非符号链接文件，fixture 按原始字节参与 `fixture_hash`。

Checker 示例：

```yaml
judge:
  type: custom
  validator: code/validator.cpp
  checker: code/checker.cpp
  qa:
    schema_version: 1
    robustness:
      baseline: accepts-alternative
      probes: [empty, truncated, extra-token, oversized]
    cases:
      - id: accepts-alternative
        purpose: valid alternative output
        case: sample/basic
        contestant_output: judge-fixtures/checker/alternative.out
        expected: {status: AC}
      - id: rejects-extra-token
        purpose: extra token
        input: judge-fixtures/checker/extra.in
        jury_answer: judge-fixtures/checker/extra.ans
        contestant_output: judge-fixtures/checker/extra.out
        expected: {status: WA}
```

Interactor 示例：

```yaml
judge:
  type: interactive
  validator: code/validator.cpp
  interactor: code/interactor.cpp
  qa:
    schema_version: 1
    cases:
      - id: normal-protocol
        purpose: normal protocol
        case: secret/basic
        contestant: {source: code/judge-qa/normal.cpp}
        expected: {status: AC}
      - id: idle-player
        purpose: idle contestant
        case: secret/basic
        contestant: {behavior: idle}
        expected: {status: TLE, timeout_kind: idle}
```

Checker fixture 的期望状态只能是 `AC`/`WA`；Interactor 可使用 `AC`、`WA`、`RE`、`TLE`、`MLE`、`OLE`，其中 TLE 可声明 `timeout_kind: idle|total`。Interactor 的题目特定模拟选手源码放在 `code/judge-qa/`，内建行为只有 `early-eof`、`idle`、`output-flood`。`judge-fixtures/` 与 `code/judge-qa/` 都属于规范源，会被 source/checkpoint 跟踪，但不进入正式 PDF、ZIP、Manifest 或 DOMjudge 数据。

Schema 固定限制 fixture 数量、单文件大小、总字节、诊断、transcript 和运行时间；ID 与路径按 Windows 大小写不敏感去重。`probhub judge-qa <ID>` 每次真实执行 fixture、Validator 和探针，只缓存内容寻址的编译结果。成功才原子发布 `<problem>/.probhub/judge-qa-evidence-v1.json`；失败、取消、超时、输入变化、锁竞争或发布失败保留上一份成功 evidence。evidence 状态由 lint/status 报告为 `not-configured`、`missing`、`current`、`stale` 或 `invalid`，missing/stale/invalid 是 warning；`seal` 和正式 `build` 对已配置 QA 要求当前通过的 evidence。

### Mutation 人工排除 Schema v1

标准题可以在人工审查稳定 mutation ID 后记录等价或不适用的变异：

```yaml
mutation:
  schema_version: 1
  exclusions:
    - id: cpp-token-v1:comparison-boundary:42:17:0123456789abcdef
      reason: 该分支在 Validator 保证的 n >= 1 下与原程序等价
```

`mutation` 只适用于 `judge.type: standard`。`exclusions` 最多 256 项；每项只能包含当前 `cpp-token-v1` 的稳定 `id` 和不超过 1024 字节的非空 `reason`，ID 不得重复，也不支持通配符。配置字段、版本、ID、重复项、数量和理由错误会由 lint 以稳定诊断阻断。

先在未排除状态运行 mutation 并审查源码、变异位置和执行结果，再登记排除。排除在 `--max-mutants` 限额之前应用，因此不会占用有效变异的执行配额。仅运行部分算子时，属于其他算子的有效 ID 标为 `out-of-scope`；源码变化后已不在完整计划中的旧 ID 不会被静默删除，而会标为 `unmatched` warning，供作者更新或移除。排除理由参与 source hash 和计划 hash；修改配置会使旧 evidence 过期。

人工排除只表达作者对特定生成变异的审查结论。raw、excluded、effective、selected 和最终分类必须并列解释，不得用过滤后的 mutation score 宣称不存在未知错解或算法已经正确。

### Stress differential testing

`stress` 是可选的单题差分测试配置：

```yaml
stress:
  generator: code/stress_generator.cpp  # 必需
  args: ["{seed}", "{round}"]           # 默认 ["{seed}"]，也可写单个字符串
  rounds: 1000                          # 默认 1000
  time_limit: 5                         # accepted/brute；默认 max(limits.time * 2, 5)
  tool_timeout: 5                       # generator/validator/checker；默认 5
  accepted: code/std.cpp                # 可选，默认 solutions.accepted 第一项
  brute: code/brute.cpp                 # 可选，默认 solutions.brute 第一项
```

`generator` 每轮向 stdout 写一个完整测试点；stderr 仅用于诊断。参数模板支持 `{seed}` 和从 1 开始的 `{round}`，本轮 seed 为 `master_seed + round - 1`。所有 stress 路径都相对题目目录，且不得逃逸到题目目录外。

执行顺序是 Generator → Validator → accepted → brute → 比较。`standard` 使用普通逐行比较；`custom` 把 accepted 输出作为 jury answer、brute 输出作为 contestant output 交给 `judge.checker`。`interactive` 当前不支持 stress。

首个失败保存在 `<problem>/.probhub/stress/` 并可用 `probhub stress <ID> --replay latest` 重放。完整命令、Generator 协议、反例文件和退出语义见 `references/stress.md`。

### Data groups and solution expectations

`solutions` 可继续使用字符串列表，也可使用结构化条目：

- `file`：程序路径。
- `expected.status/groups/all/forbid`：该程序在目标组内的结构化宿命。
- `run_on: [groups]`：本地 Judge 运行域；多个组取并集，sample 始终隐式执行。首个 accepted 禁止缩域，第二及后续 accepted 使用 `run_on` 时必须显式配置 `expected.groups`，且期望覆盖不能超出执行域。
- `independence: {from, basis, note}`：accepted 之间的作者声明与人工复核证据；`basis` 为 `algorithm` 或 `key_implementation`。Core 不自动证明独立，但确定的同路径、同字节或直接 include 复用会阻断。

`run_on` 只影响本地 Judge，不改变 stress 和 DOMjudge 包。`data.groups` 使用 glob 将测试点映射到逻辑组，并可通过 `targets` 指定需要被该组击杀的程序。`difficulty` 可省略；存在时必须是非 bool 的 `0..5` 整数。`difficulty >= 4` 且只有一个 accepted、没有额外全域 AC 参考实现时会给结构化 warning。完整运行域、独立性、默认宿命和 JSONL 字段见 `references/data-groups-expectations.md`。

## Problem directory layout

All problem-local C++ sources and locally compiled executables live under `code/`. Paths in `probhub.yaml` are relative to the problem directory.

```text
<problem>/
├── probhub.yaml
├── problem.md
├── assets/
│   └── diagram.png
├── code/
│   ├── std.cpp
│   ├── brute.cpp
│   ├── wrong*.cpp
│   ├── validator.cpp
│   ├── inmaker.cpp
│   ├── stress_generator.cpp
│   └── *.exe              # local build output, ignored by Git
├── data/
│   ├── sample/
│   └── secret/
└── .probhub/
    ├── build-manifest.json
    ├── sandbox-cache-v1.json
    └── stress/              # ignored local counterexamples
```

`checker.cpp`, `interactor.cpp`, auxiliary solutions, and diagnostic C++ programs also belong in `code/`. Generated DOMjudge validator files remain under `output_validators/` because that directory is part of the package format rather than the source-code workspace.

source hash 会递归覆盖 `code/` 下全部普通源码与辅助文本，包括 Python、头文件和 `.inc/.ipp/.tcc` 等 include 片段；仅排除可执行文件、目标文件、动态库和明确缓存/构建目录。符号链接不作为题目源码跟踪，题目目录也不得通过 `..`、绝对路径或父目录符号链接逃出工作区。

题面图片等媒体资源优先放在 `assets/`。Core 也会跟踪题目目录内、且不位于 `code/`、`data/`、`.probhub/` 或生成目录中的常见图片文件；修改这些文件会使本题 source hash 和整场 collection hash 过期。

## Statement file

`problem.md` must contain:

```markdown
# Problem title

## 题目描述
...

## 输入格式
...

## 输出格式
...

## 提示
... optional ...
```

题面必须恰好有一个非空 H1；`题目描述 -> 输入格式 -> 输出格式` 三个 H2 必须存在、非空、不重复且按序出现，可选 `提示` 只能位于输出之后。明确的样例输入/输出 Markdown 标题（任意 H2 及更深层级）会被 lint 拒绝；fenced code 内标题不参与扫描。Samples are not duplicated in Markdown. `data/sample/*.in` and matching `.ans` files are their only source.

`statement.source` 和 `judge.validator` 必须解析为题目目录内的普通非符号链接文件。lint 的 `constraint_reconciliation` 会保留题面输入范围与 Validator `readInt/readLong/readDouble/readStrictDouble/ensuref` 直接字面量的 path/line/raw 证据，并固定声明 `analysis_state: partial`。它不是完整 Markdown/C++ 语义分析：只有唯一同名变量的确定数值差异给 warning，动态、析取、歧义或不支持结构只要求人工复核，永不改变 lint 的 `ok` 或退出码。

对多组数据，`aggregate_constraints` 还会保守识别 `$\sum_{i=1}^{T} n_i\le 2\times 10^5$`、`所有测试用例中的 n 之和不超过 2×10^5` 等直接题面表述，以及 Validator 中 `sum_n += n` / `sum_n = sum_n + n`、`sum_len += s.size()` 后由 `ensuref` 检查的直接累加器。主体规范化为 `sum:n`、`sum:len:s` 或 `sum:n+m`，并报告 `matched`、`statement_only`、`validator_only`、`dynamic` 与 `state`。题面缺失、Validator 缺失和确定数值不一致为非阻断 warning；检测到多测和单组规模、但没有直接累计约束时只提示人工复核。Core 不解析宏、函数封装、数组归约或任意 C++/自然语言表达式，也不会自动推导正确总量；出题人仍必须人工检查 `T × 单组上界`、算法复杂度和累加类型是否安全。

当前 Schema v1 不支持可执行的 `constraints` 单一事实源。未来 Token、临时 C++ header、缓存/hash、WebUI round-trip 和构建快照的完整评估见 [`constraints-schema-evaluation.md`](constraints-schema-evaluation.md)；在该设计落地前，不要向 `probhub.yaml` 添加未知 `constraints` 字段或宣称题面与 Validator 已自动同步。

非交互题的 `data/sample/*.ans` 还必须由配置顺序中的首个 accepted 精确复现。`probhub sample-check` 与完整 Judge 都只归一 CRLF/裸 CR 为 LF，尾空格、缺少尾换行等差异仍失败；Custom Checker 接受非唯一输出不能绕过正式样例答案一致性。交互题对此检查明确不适用。

## Generated artifacts

The Core generates and may overwrite:

- `<problem>/meta.json`
- Typst `problems.json`
- `<problem>/problem.yaml`
- `<problem>/domjudge-problem.ini`
- `<problem>/problem.pdf`
- Full contest PDF
- `<id>.zip`
- `<problem>/.probhub/build-manifest.json`

Do not edit generated artifacts to make source changes.

## WebUI write boundaries

Schema v1 WebUI 遵循以下写入边界：

- 加载页面、切换赛事、切换题目和翻阅 PDF 只读，不修改工作区；PDF 页面缓存位于 WebUI 进程临时目录。
- 题面自动保存只写 `.probhub/workspace.yaml` 的题序、`probhub.yaml`、`problem.md` 与 `data/sample/`，保存后执行完整 lint；失败时回滚。
- 封面设置写入 `.probhub/workspace.yaml` 的 `contest` 与 `typst.cover`，不直接改 Typst 模板。
- 编辑器使用 revision 防止多个标签页或 Agent 静默覆盖；冲突返回 HTTP `409` 和 `code: source_conflict`。
- “编译”是隔离预览，只在系统临时目录生成 PDF；“分发”是显式正式 build，统一调用 Core 的锁、快照、staging、Judge、验包和发布流程。

## Local sandbox cache

`<workspace>/.probhub/build.lock` 是 `build`、`typeset` 和 `package` 共享的跨平台 OS 文件锁载体。锁文件可在命令结束后保留；不得通过删除文件或检查文件是否存在来判断锁状态。新工作区会把它加入 `.gitignore`。

并行出题期间，`checkpoint`、`seal` 和 `assemble` 使用以下本地产物：

- `<workspace>/.probhub/checkpoints/`：不可变题目 revision；
- `<workspace>/.probhub/generations/`：不可变完整试卷预览；
- `<workspace>/.probhub/generation.lock`：组卷 single-writer OS 锁。
- `<problem>/.probhub/judge.lock`：单题 Judge 全程 OS 排他锁，避免 CLI 与多个 WebUI 进程并发改写编译产物或缓存。
- `<problem>/.probhub/judge-evidence.lock`：单题校准证据发布 OS 锁。

这些路径不属于 Schema 规范源，不得提交或手工编辑。generation 不替换正式 PDF、ZIP、metadata 或 Build Manifest。完整语义见 `references/generations.md`。

`<problem>/.probhub/sandbox-cache-v1.json` is an ignored local artifact. It stores content-addressed compile, validator, and per-testcase results, including normalized sample stdout/answer summaries used by sample-check. Relevant source, header, input, answer, time/memory/output/process limit, compiler, platform, sandbox policy, or cache-schema changes invalidate entries automatically. Use `probhub sample-check <id> --no-cache` to refresh only the current sample execution while preserving unrelated entries; use `probhub judge <id> --no-cache` or `probhub build <id> --no-cache` to force a complete run.

`<problem>/.probhub/judge-evidence-v2.json` 是最近一次完整成功 Judge 的本地校准证据，不进入 Manifest、ZIP 或正式发布身份。lint/status 会用当前 schema、source/data hash、平台、测量策略与 solution 运行域验证它；失败 Judge 保留上一份成功证据。旧 `judge-evidence-v1.json` 继续被 Git 忽略，但不再读取。

`<problem>/.probhub/stress/` is a separate ignored diagnostic directory containing replayable counterexamples and `latest.json`; stress does not reuse the sandbox cache. Resource-control semantics are versioned in the cache Schema, so older cached AC/RE/TLE results cannot bypass newer OLE or process-tree policies. Do not package or commit either local artifact.
