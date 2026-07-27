# ProbHub CLI Reference

本文件是 Workspace Schema v1 的完整 CLI 参考。主执行规则以 `SKILL.md` 为准；参数精确值最终以 `probhub <command> --help` 为准。

## 1. 工作区与题目选择

工作区根目录必须包含：

```text
.probhub/workspace.yaml
```

从根目录或任意子目录运行时，CLI 会向上查找工作区。在工作区外运行时，把全局参数放在子命令前：

```powershell
probhub --workspace C:\path\to\workspace build L01
```

题目参数只接受 `.probhub/workspace.yaml` 中的稳定 `id`：

```powershell
probhub build L01          # 单题
probhub build L01 L03      # 多题
probhub build              # 全部题目
```

不要使用显示字母 `A/B/C`，因为显示题号由赛事题序动态确定。

## 2. 命令入口与退出码

首选全局命令：

```powershell
probhub --version
probhub --help
```

回退入口：

```powershell
python scripts/probhub.py <command>
```

工作区没有回退脚本时，调用已安装 Skill 中的 `scripts/probhub.py` 并传入 `--workspace`。全局选项必须写在子命令之前：

```powershell
probhub --workspace C:\path\to\workspace --json status L01
```

全局选项：

- `--workspace <path>`：显式指定工作区根目录或其内部路径。
- `--json`：输出单个 JSON 文档，适合脚本集成。
- `--version`：输出版本号并退出。

- 退出码 `0`：命令声明的验收条件满足。
- 非 `0`：失败、状态过期、包验证失败或参数错误。
- 自动化调用必须同时检查退出码和结构化输出；沙箱还必须检查最后一个 JSONL `final` 事件。

## 3. `init`

初始化新的空工作区：

```powershell
probhub init [directory] --title "Contest" --subtitle "正式赛" --author "Team"
```

生成：

```text
.probhub/workspace.yaml
```

已初始化的工作区不要再次执行 `init`。只有明确需要覆盖时才使用 `--force`。

## 4. `new`

创建**可编译、可直接评测**的新题骨架并加入稳定题序：

```powershell
probhub new L05 --name "新题"
probhub new L05 --name "新题" --directory problems/L05
probhub new L06 --name "构造题" --judge custom
probhub new L07 --name "交互题" --judge interactive
```

骨架是一道完整可工作的 A+B 示例题：lint 零错误，`judge` 开箱即 `all_expectations_met`；作者在保留结构的前提下替换为正式内容。生成：

```text
<directory>/
├── probhub.yaml            # 双 accepted + 双 wrong 期望矩阵、overflow 定向数据组、manual 配方
├── problem.md              # 按题面守则书写的示例题面
├── code/
│   ├── validator.cpp       # testlib 严格校验样板
│   ├── std.cpp             # 主标程
│   ├── std2.cpp            # 独立第二实现槽位（交叉验证）
│   ├── brute.cpp           # 暴力槽位；写好后自行登记进 solutions.brute
│   ├── wrong.cpp           # 思路层示例错解（被样例击杀）
│   ├── wrong2.cpp          # 实现层示例错解（被 overflow 定向组击杀）
│   ├── inmaker.cpp         # testlib 生成器骨架（<type> <seed>）
│   ├── checker.cpp         # 仅 --judge custom
│   └── interactor.cpp      # 仅 --judge interactive
└── data/
    ├── sample/             # 1.in / 1.ans
    └── secret/             # random01、overflow01（定向卡 wrong2）
```

要点：

- `--judge` 可选 `standard`（默认）、`custom`、`interactive`；custom 附 Checker 骨架，interactive 附 Interactor 骨架并使用带刷新的标程模板。
- 两个 secret 测试点以 `manual: true` 配方登记（配方覆盖完整，lint 无 warning）；换成生成器数据时改写 `data.recipes` 后运行 `probhub gen`。
- `solutions.brute` 初始为空：judge 对已登记的 brute 要求至少一个 TLE/MLE，脚手架数据尚无 brute-killer，实现真实暴力并准备击杀数据后再登记。
- 错解枚举与数据强度纪律见 `references/mistake-taxonomy.md`；示例的 `overflow` 组演示了 `data.groups` + `targets` + `expected` 的定向击杀写法。
- `new` 会持有工作区写锁并原子写入 `workspace.yaml`；并发写入返回 `build_busy`。
- 题目 ID 与 `--directory` 的每级路径组件仅允许 `[A-Za-z0-9][A-Za-z0-9_.-]*`，且目录必须位于工作区内（拒绝 `../`、绝对路径与工作区根本身）；注册进 `workspace.yaml` 的 directory 统一为 POSIX 相对路径。

## 5. `gen`

按 `data.recipes` 配方生成或校验 secret 数据（配方格式见 `references/workspace-schema-v1.md`）：

```powershell
probhub gen L05                 # plan：只报告，不写任何文件
probhub gen L05 --apply         # 全部成功后写入 new/changed 测试点
probhub gen L05 --case max01    # 只处理指定配方（可重复）
```

执行流程：编译生成器与首个 accepted → 按配方运行生成器 → Validator 全量过检 → accepted 产 `.ans` → 与磁盘现状比较。plan 输出逐测试点 `new` / `changed` / `unchanged` / `manual` 状态与新旧 SHA-256；`changed` 意味着配方结果与磁盘不一致（数据被手改、生成器或 accepted 变化），`--apply` 前先核对差异，不会静默覆盖。

要点：

- **失败即零写入**：任一配方出现生成器崩溃、Validator 拒绝或 accepted 非正常退出，整次 `gen` 以 `gen_failed` 失败（exit 1），不写任何数据文件。
- plan 严格只读；`--apply` 全程持有工作区写锁（与 `build`/`new` 相同的 `build.lock`），并发写入返回 `build_busy`。
- 输出统一 LF 归一，同一配方在 Windows 与 Linux 复现相同字节；plan 的 `unchanged` 是**字节级**一致（磁盘上 CRLF 的旧数据即使归一化后相等也报 `changed`，`--apply` 会把它改写为 LF 规范字节）。
- `manual: true` 的测试点不被触碰；文件缺失时 gen 与 lint 都给 warning。
- 数据目录（`data.sample_dir`/`data.secret_dir`）必须位于题目目录内，lint 与 gen 都会拒绝越界路径；case 名大小写不敏感（防 Windows 文件塌缩）。
- 生成器/Validator 单次运行默认 60s 超时（`data.gen_tool_timeout` 可覆盖，≤3600s）；进程数遵循 `limits.processes`。
- 交互题（`judge.type: interactive`）不支持 `gen`：答案由 Interactor 协议定义，不能靠把输入喂给 accepted 产生（报 `gen_unsupported`）。
- 生成证据（generator/args/输入与答案哈希）写入本地 `.probhub/gen-manifest.json`，属本地产物不提交。`--case` 局部运行按 case 合并进既有 manifest，不影响其他 case 的记录；配方被删除的 case 在下次 `--apply` 时从 manifest 移除。
- 没有配方的 secret 测试点由 lint 以 warning 报告；数量低于数据强度纪律（`references/mistake-taxonomy.md`）时同样给 warning。
- accepted 改动后重跑 `gen`：plan 会把所有答案变化列为 `changed` 并给出新旧哈希——先审阅差异再 `--apply`。

## 6. `doctor`

检查环境：

```powershell
probhub doctor
```

用于确认 Python、Node/npm、Typst、g++ 和 Python 依赖。首次安装、换机器或 CI 失败时优先运行。

## 7. `lint`

```powershell
probhub lint [ID...]
```

检查：

- 工作区和题目 Schema。
- 题名、题面章节和禁止占位符。
- 时间与内存限制。
- Validator 路径。
- 样例/隐藏数据目录。
- `.in` 与 `.ans` 配对。
- 源文件与数据哈希。

示例：

```powershell
probhub lint L01
probhub lint L01 L03
probhub lint
```

## 8. `status`

```powershell
probhub status [ID...]
```

状态：

- `current`：规范源、数据、工作区、整场排版输入、PDF、ZIP 与 Manifest 一致。
- `stale`：至少一个哈希与 Manifest 不一致；读取 `stale_fields` 定位。
- `never-built`：缺少 Manifest 或正式产物。

`status` 非 `current` 时返回非零退出码。

Manifest 中的 `collection_hash` 根据工作区/模板、题面媒体资源以及所有题目实际生成 Typst metadata 的输入计算。其他题的题面、题面图片、样例、展示配置或题序变化可能改变当前题的页码、总页数或单题 PDF，因此会使当前题变为 `stale`；其他题仅修改不参与排版的 secret 数据不会使当前题过期。

包含 `collection_hash` 的 Build Manifest 使用 schema v2。一次 build 的所有所选 Manifest 必须包含相同的非空 `batch_id`；缺失时显示 `stale_fields: ["batch_id", ...]`。旧 v1 Manifest 会以 `stale_fields: ["manifest_schema", ...]` 明确要求重建，不会被静默视为 `current`。

## 9. `judge`

```powershell
probhub judge [ID...] [--no-cache]
```

执行：

1. 编译并运行 Validator。
2. 编译 `solutions.accepted`、`solutions.brute`、`solutions.wrong`。
3. 对 `data/sample` 和 `data/secret` 逐点评测。
4. 按 `judge.type` 使用标准比较、Checker 或 Interactor。
5. 根据每个程序的结构化 `expected` 宿命验证状态、目标数据组与禁止状态；未配置时保持 accepted 全 AC、brute 不 WA 且至少 TLE/MLE、wrong 至少一个非 AC 的默认语义。
6. 对普通程序、Checker、Validator、编译器和 Interactor 应用完整进程树、时间、内存、输出与进程数控制。

支持的评测类型：

```yaml
# 普通题
judge:
  type: standard
  validator: code/validator.cpp

# standard 默认逐行比较；忽略整个输出首尾空白及每行末尾的空格/Tab。
# 行内空格数量、非首行的行首空白和换行结构仍然必须一致。

# 特判/浮点题
judge:
  type: custom
  validator: code/validator.cpp
  checker: code/checker.cpp

# 交互题
judge:
  type: interactive
  validator: code/validator.cpp
  interactor: code/interactor.cpp
  interactive:
    idle_limit: 1.0
    transcript_limit: 65536
```

交互题 JSONL 额外包含双向 `transcript` 事件和 `timeout_kind: idle|total`；修改交互选项会自动使逐点缓存失效。

Checker/Interactor 的参数协议、testlib 模板和状态映射见 `references/checker-interactor.md`。

数据逻辑分组、`solutions.*[].expected`、默认宿命和首个击杀用例字段见 `references/data-groups-expectations.md`。仅修改分组或宿命会复用逐点缓存，并重新计算断言。

资源配置：

```yaml
limits:
  time: 1
  memory: 256
  output: 64     # MiB，默认 64
  processes: 32  # 整棵进程树，默认 32
```

选手程序超过输出预算时状态为 `OLE`；超过进程数上限时为 `RE` 并带 `process limit exceeded`。官方 Checker、Validator、Interactor 或编译器自身超时、超限或无法建立完整进程树控制时属于基础设施 `FAIL`，而不是选手答案错误。输出超限后，保存的 stdout/stderr 会截断到预算以内。完整跨平台语义见 `references/process-control.md`。

成功最终事件：

```json
{
  "protocol": "probhub.local_judge",
  "protocol_version": 1,
  "type": "final",
  "ok": true,
  "status": "passed",
  "code": "all_expectations_met",
  "exit_code": 0
}
```

### 缓存

默认模式 `normal` 读取并更新：

```text
<problem>/.probhub/sandbox-cache-v1.json
```

缓存层级：

- 编译：源码、相关头文件、参数、编译器、平台、二进制摘要。
- Validator：验证器指纹和输入内容。
- Case：程序指纹、输入、答案、时限、内存、输出上限、进程数上限、平台和沙箱策略。

强制完整执行并用本次结果替换缓存：

```powershell
probhub judge L01 --no-cache
```

缓存事件包含 `mode`、`compile_hits/misses`、`validator_hits/misses`、`case_hits/misses`。进程树、OLE 或资源限制语义变化会提升缓存 Schema，防止旧结果绕过新策略。

## 10. `stress`

```powershell
probhub stress ID... [--rounds N] [--seed S]
probhub stress ID --replay latest
probhub stress ID --replay <反例目录或输入文件>
```

`stress` 使用题目 `probhub.yaml` 中的 `stress` 配置，逐轮执行 Generator → Validator → accepted → brute → 输出比较。首个失败会停止并保存到 `<problem>/.probhub/stress/`。

最小配置：

```yaml
stress:
  generator: code/stress_generator.cpp
```

常用完整配置：

```yaml
stress:
  generator: code/stress_generator.cpp
  args: ["{seed}", "{round}"]
  rounds: 1000
  time_limit: 5
  tool_timeout: 5
  accepted: code/std.cpp
  brute: code/brute.cpp
```

- `generator` 必需；每轮向 stdout 写一个完整测试点，stderr 仅写诊断。
- `args` 默认为 `["{seed}"]`，支持 `{seed}` 和从 1 开始的 `{round}`。
- 未传 `--seed` 时随机选择非负 master seed；本轮 seed 为 `master_seed + round - 1`。
- `--rounds` 覆盖配置中的 `rounds`；两者都必须为正整数。
- `accepted` / `brute` 可省略，默认取 `solutions.accepted` / `solutions.brute` 第一项。
- `time_limit` 控制 accepted/brute；`tool_timeout` 控制 Generator、Validator 和 Checker。

比较规则：

- `judge.type: standard`：accepted 是期望输出，brute 是实际输出；忽略整个输出首尾空白和每行末尾空格/Tab，其余严格。
- `judge.type: custom`：accepted 输出作为 jury answer，brute 输出作为 contestant output，使用 `judge.checker` 和现有 DOMjudge/testlib 协议。
- `judge.type: interactive`：暂不支持，lint 和命令都会失败。

示例：

```powershell
probhub stress L01 --rounds 10000 --seed 12345
probhub stress L01 L03 --rounds 2000
probhub --json stress L01 --rounds 10000 --seed 12345
```

Replay：

```powershell
probhub stress L01 --replay latest
probhub stress L01 --replay "L01/.probhub/stress/<artifact>"
probhub stress L01 --replay "L01/.probhub/stress/<artifact>/input.in"
```

`--replay` 要求恰好一个题目 ID，且路径必须位于该题的 `.probhub/stress/` 内。它使用保存的输入重新运行当前 Validator、accepted、brute 和 Checker，不重新运行或编译 Generator，也不覆盖原反例。

结果与退出：

- `status: passed`：全部轮次匹配或 replay 通过，退出码 `0`。
- `status: counterexample`：输出不匹配或 accepted/brute RE/TLE/MLE/OLE，保存反例并退出 `1`。
- `status: infrastructure`：Generator、Validator、Checker 或编译器 RE/TLE/MLE/OLE/进程限制失败，保存诊断并退出 `1`。
- Schema、路径、编译或参数错误：输出 `ok: false` 和 `error`，退出 `1`。
- Ctrl+C：退出 `130`。

多题执行只有全部题目通过才返回 `0`。完整字段、Generator 示例、Checker 调用、反例文件和失败 reason 见 `references/stress.md`。

## 10.1 `checkpoint`、`seal` 与 `assemble`

并行开发期间发布当前题目的不可变草稿：

```powershell
probhub checkpoint L10
```

题目完成后执行自动验证、冻结 revision 并生成一份完整试卷：

```powershell
probhub seal L10 --no-cache --seed 12345
probhub seal L10 --rounds 3000 --seed 12345
```

`seal` 执行：

1. 单题 lint；
2. judge，`--no-cache` 时完整重跑；
3. 若配置了 `stress`，按配置或 `--rounds` 执行固定 seed 差分测试；
4. 复核 live source/data hash 未在验证期间变化；
5. 写入 sealed checkpoint 和验证证据；
6. 使用所有题目的最新 checkpoint 组装完整试卷 generation。

单独组装和检查当前 generation：

```powershell
probhub assemble
probhub generation-status
```

generation 存放在 `.probhub/generations/<generation-id>/`，包含 `main.pdf`、逐题 PDF 和 `manifest.json`。它是内容寻址的隔离预览，不覆盖正式 Typst `main.pdf`、ZIP 或 Build Manifest。没有可用 checkpoint 的题目使用开发中占位页；`complete`、`all_sealed` 和逐题 `state` 会明确报告实际状态。

组装使用独立 `.probhub/generation.lock`。并发请求等待当前组装结束，随后按最新 revision 集合生成或复用版本，不占用正式 `build.lock`。详细存储和并行语义见 `references/generations.md`。

## 11. `typeset`

```powershell
probhub typeset [ID...]
```

无论选择几题，都会编译 `.probhub/workspace.yaml` 指定的完整 Typst 集合，以保证正式字母与物理页码正确；只把所选题目的页段提取到：

```text
<problem>/problem.pdf
```

它不会运行沙箱、构建 ZIP 或写 Manifest。

## 12. `package`

```powershell
probhub package [ID...]
probhub package L01 --allow-missing-pdf
```

执行：

1. 从 `probhub.yaml` 生成 DOMjudge `problem.yaml` 与 `domjudge-problem.ini`。
2. 在同卷临时路径构建 `<ID>.zip`。
3. 验证路径、配置、样例、隐藏数据、配对关系与 PDF；只有验证成功才替换根目录正式 ZIP。

默认要求已有 `problem.pdf`。`--allow-missing-pdf` 只用于尚未排版的中间状态，不用于正式交付。

`package` 不自动执行 lint、judge 或 typeset。正式流程优先使用 `build`。

## 13. `build`

```powershell
probhub build [ID...] [--skip-judge] [--no-cache]
```

顺序：

1. 取得 `.probhub/build.lock` 的跨平台 OS 排他锁；锁文件可以长期存在，只有 OS 锁状态表示占用。
2. lint 全部 collection 依赖，建立包含正式题序、全部排版输入和所选 source/data hash 的 BuildPlan。
3. 复制受控输入快照；后续 judge、排版和打包只读取该快照。
4. judge 所选题目。
5. 在快照中编译一次完整 Typst 集合并提取所选单题 PDF。
6. 在快照中生成 DOMjudge 配置、构建并验证全部所选 ZIP。
7. 为所有所选题目生成带同一 `batch_id` 的 Manifest。
8. 发布前重新计算 live 输入哈希；变化时以 `inputs_changed` 失败。
9. 全部准备成功后发布共享产物与所选产物，Manifest 最后替换。

即使执行 `build L01`，也会为了题序与页码编译全卷，但只评测、提取、打包和更新 L01。

执行 `build L01 L02 ...` 时，所选题目逐题评测，但完整 Typst 集合只编译一次。任一题在 judge、PDF 提取、配置、ZIP 构建或验证阶段失败时，正式 metadata、PDF、ZIP 与 Manifest 均保持不变。所有所选 Manifest 使用同一份 `workspace_hash`、`collection_hash` 和 `batch_id`。

并发执行 `build`、`typeset` 或 `package` 时，第二个 writer 失败并在 JSON 中返回 `code: "build_busy"`。快照创建或发布前发现 live 输入变化时返回 `code: "inputs_changed"`。快照 I/O 与发布 I/O 分别使用 `snapshot_failed`、`publish_failed`。当前版本尚未实现发布阶段的 journal/rollback；如果进程在多个正式文件替换之间被硬终止，仍需人工检查并重新 build。

选项：

```powershell
probhub build L01 --no-cache     # 完整沙箱并刷新缓存
probhub build L01 --skip-judge   # 跳过沙箱，仅用于已有可信评测的排版/打包迭代
```

不要把 `--skip-judge` 作为首次构建或正式正确性证明。

## 14. `verify-package`

```powershell
probhub verify-package L01.zip
probhub verify-package L01.zip --require-pdf
```

检查：

- ZIP 路径安全与重复路径。
- 根配置文件。
- `data/sample`、`data/secret`。
- `.in`/`.ans` 配对。
- 必需 PDF。

正式题目包使用 `--require-pdf`。

## 15. 推荐流程

### 单题开发

```powershell
probhub lint L01
probhub judge L01
probhub stress L01 --rounds 10000 --seed 12345  # 已配置 stress 时
probhub build L01
probhub status L01
```

### 只改题面

题面不影响沙箱指纹，默认缓存会复用代码与数据结果：

```powershell
probhub build L01
```

### 修改一个数据点

默认缓存只重跑受影响的 Validator 和程序测试点：

```powershell
probhub judge L01
probhub stress L01 --rounds 10000 --seed 12345  # 已配置 stress 时
probhub build L01
```

### 正式交付

最后一次影响代码、数据、答案或限制的修改后：

```powershell
probhub build L01 --no-cache
probhub status L01
probhub verify-package L01.zip --require-pdf
```

## 16. 常见问题

### `probhub` 未识别

先使用工作区回退入口：

```powershell
python scripts/probhub.py status
```

需要全局入口时，在 ProbHub-skill 包目录执行：

```powershell
npm install -g .
```

### `unknown problem id`

读取 `.probhub/workspace.yaml` 中 `problems[].id`，不要使用显示字母或未登记目录名。

### `stale`

读取 `stale_fields`，通常执行对应题目的 `build`；不要手工编辑 Manifest。

### `problem.pdf` 缺失

先执行：

```powershell
probhub typeset L01
```

或直接执行完整 `build L01`。

### Typst 字体警告

字体缺失警告不一定导致失败。以 Typst 退出码、PDF 是否生成和页码提取结果为准；正式发布前仍应在目标排版环境检查字体。
