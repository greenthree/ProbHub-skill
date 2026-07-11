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
statement:
  source: problem.md
judge:
  type: standard
  validator: code/validator.cpp
solutions:
  accepted: [code/std.cpp]
  brute:
    - file: code/brute.cpp
      expected:
        status: [TLE, MLE]
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
    - name: greedy-counterexample
      role: wrong-solution-killer
      patterns: [secret/greedy*]
      targets: [code/wrong_greedy.cpp]
    - name: stress
      role: brute-killer
      patterns: [secret/stress*]
      targets: [code/brute.cpp]
domjudge:
  include_pdf: true
```

### Resource limits

`limits` controls every local sandbox path, including ordinary solutions, Checker, Validator, compiler, Interactor, and stress roles:

| Field | Unit | Default | Meaning |
|---|---:|---:|---|
| `time` | seconds | `1` | Contestant wall-clock limit |
| `memory` | MiB | `256` | Contestant memory limit |
| `output` | MiB | `64` | Captured stdout + stderr budget for one contestant run |
| `processes` | processes | `32` | Maximum processes in the controlled process tree |

`time`, `output`, and `processes` must be positive integers. `memory` must be a power of two and at least `256` MiB. Output above the budget is terminated and reported as `OLE`, with captured files truncated. Official tools use bounded internal time/output policies and the same full-tree containment; their resource failure is infrastructure `FAIL`, not contestant WA. Windows uses Job Objects and refuses to run if containment cannot be established. Linux/Unix uses a separate process group, `RLIMIT_AS`, and low-frequency `/proc` tree monitoring. See `references/process-control.md`.

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

`solutions` 可继续使用字符串列表，也可使用带 `file`、`expected.status`、`expected.groups`、`expected.all` 和 `expected.forbid` 的结构化条目。`data.groups` 使用 glob 将测试点映射到逻辑组，并可通过 `targets` 指定需要被该组击杀的程序。完整语义、默认宿命和 JSONL 字段见 `references/data-groups-expectations.md`。

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

Samples are not duplicated in Markdown. `data/sample/*.in` and matching `.ans` files are their only source.

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

`<problem>/.probhub/sandbox-cache-v1.json` is an ignored local artifact. It stores content-addressed compile, validator, and per-testcase results. Relevant source, header, input, answer, time/memory/output/process limit, compiler, platform, sandbox policy, or cache-schema changes invalidate entries automatically. Use `probhub judge <id> --no-cache` or `probhub build <id> --no-cache` to force a complete run and refresh the cache.

`<problem>/.probhub/stress/` is a separate ignored diagnostic directory containing replayable counterexamples and `latest.json`; stress does not reuse the sandbox cache. Resource-control semantics are versioned in the cache Schema, so older cached AC/RE/TLE results cannot bypass newer OLE or process-tree policies. Do not package or commit either local artifact.
