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
typst-statement/lib.typ
typst-statement/usts.png
typst-statement/<subtitle>/main.typ
typst-statement/<subtitle>/problems.typ
```

模板直接来自当前已安装 npm 包，不依赖源码仓库；workspace 固定记录 `creation_timestamp: 0`，避免构建时间进入 PDF 身份。已有 Typst 文件会保留，不被 `init --force` 静默覆盖。`subtitle` 必须是跨平台安全的单级目录名。

已初始化的工作区不要再次执行 `init`。只有明确需要覆盖 workspace 配置时才使用 `--force`。

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
│   ├── std2.cpp            # 按位进位加法的独立第二实现（带 independence 声明）
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
- `std2.cpp` 不只是更换 I/O：它用异或与进位迭代实现加法，并在第二 accepted 上声明 `independence.from/basis/note`，示范真正的交叉实现证据。
- standard/custom 的 `random01` 使用固定参数的真实生成 recipe，`overflow01` 以 `manual: true` 登记；脚手架因此可以直接演示 `probhub gen --apply` 的 Generator → Validator → accepted 闭环，同时保持 recipe 覆盖完整、lint 无 warning。interactive 的答案由 Interactor 协议定义，不支持 `gen`，两个测试点均显式登记为 manual。
- `solutions.brute` 初始为空：judge 对已登记的 brute 默认要求至少一个 TLE/MLE/OLE，脚手架数据尚无 brute-killer，实现真实暴力并准备击杀数据后再登记。
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

执行流程：编译生成器与首个 accepted → 按配方运行生成器 → Validator 全量过检 → accepted 产 `.ans` → Custom Judge 运行 Checker 复核 → 与磁盘现状比较。plan 输出逐测试点 `new` / `changed` / `unchanged` / `manual` 状态与新旧 SHA-256；`changed` 意味着配方结果与磁盘不一致（数据被手改、生成器或 accepted 变化），`--apply` 前先核对差异，不会静默覆盖。

要点：

- **失败即零写入**：任一配方出现生成器崩溃、Validator 拒绝、accepted 非正常退出或 Checker 不接受正式答案，整次 `gen` 以 `gen_failed` 失败（exit 1），不写任何数据文件。
- plan 严格只读；`--apply` 全程持有工作区写锁（与 `build`/`new` 相同的 `build.lock`），并发写入返回 `build_busy`。
- `--apply` 在取得锁后重新加载 live workspace 与 `probhub.yaml`，发布前再次核对配置身份；`.in`、`.ans` 与 gen manifest 全部先 staging，任一 replace 失败会恢复原文件并以 `gen_write_failed` 退出。
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

用于确认 Python >=3.10、Node >=18、npm、固定 Typst 0.14.2、`Noto Sans CJK SC`、g++ 和 Flask/PyYAML/pypdf，并报告 Python 包实际版本。`python -m probhub doctor` 与 npm `probhub doctor` 在这些业务模块尚未安装时也会输出结构化缺失报告，而不是在参数解析前 traceback。首次安装、换机器或 CI 失败时优先运行。

## 6.1 `ui`

从 Workspace Schema v1 根目录启动当前安装包中的 WebUI：

```powershell
probhub ui
probhub ui --no-browser
probhub ui --port 33934
```

服务只监听 `127.0.0.1`。`--no-browser` 不自动打开浏览器，`--port` 只改变本机监听端口。CI 或安装诊断使用：

```powershell
probhub --json ui --check
```

`--check` 会定位 workspace、从当前安装包绝对路径导入 WebUI 和 Core，然后立即退出；不会启动服务器或写入赛事规范源与正式产物。缺少打包文件、Python 依赖或入口契约不兼容时，分别返回稳定的 `webui_runtime_missing`、`webui_dependency_missing` 或 `webui_runtime_incompatible`。赛事仓库可以保留调用该命令的薄入口，但不得复制整套 Core/WebUI 后静默回退。

WebUI 的本地 WSGI 服务最多同时使用 8 个请求线程，使同步编译或分发进行时仍可处理任务轮询和取消，并避免默认每请求创建无界线程。完整沙箱和上传评测在解析任务前经过有界 admission gate，再进入共享固定 worker pool 与有界等待队列。任务被接受后先返回 `queued`，随后进入 `running`；入口或队列饱和时 HTTP 返回 `429`，JSON 包含 `code: "queue_full"`、`retryable: true` 和 `retry_after`，不会创建上传临时工作区。`local_judge.py` 在每个题目目录取得 OS 文件锁，同题的 CLI 与任意 WebUI 进程不会同时改写编译产物或缓存；不同题及隔离上传仍可并行。排队和运行中的任务都支持取消，运行任务还受单次执行与整任务 deadline 约束。可见日志、结构化事件、完成记录数量和 TTL 均有上限，旧结果不能作为长期持久记录使用。

## 7. `lint`

```powershell
probhub lint [ID...]
```

检查：

- 工作区和题目 Schema。
- 题名、唯一 H1、必需章节完整/非空/不重复/按序、提示位置、Markdown 样例章节和禁止占位符；fenced code 内的伪标题不参与扫描。
- 时间与内存限制。
- `statement.source` 与 Validator 必须是题目目录内的普通非符号链接文件。
- 样例/隐藏数据目录。
- `.in` 与 `.ans` 配对。
- 源文件与数据哈希。
- 最近一次完整 Judge 校准证据及 accepted/TLE 余量；缺失、过期或不足只产生结构化 warning，不使 lint 失败。
- `constraint_reconciliation` 以 `analysis_state: partial` 列出题面输入范围和 Validator 的直接字面约束。只有唯一同名变量、双方直接数值边界的高置信差异才产生 warning；动态、歧义和不支持的表达式只进入 info/report，任何约束对账结果都不改变 lint 退出码。

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

每题结果还包含 `calibration.state`、`diagnostics` 与兼容字符串 `warnings`。校准证据缺失/过期、accepted 余量不足或 expected-TLE 探针不足不会改变正式构建状态；它们是交付体检信息，不是 Manifest stale 字段。

Manifest 中的 `collection_hash` 根据工作区/模板、题面媒体资源以及所有题目实际生成 Typst metadata 的输入计算。其他题的题面、题面图片、样例、展示配置或题序变化可能改变当前题的页码、总页数或单题 PDF，因此会使当前题变为 `stale`；其他题仅修改不参与排版的 secret 数据不会使当前题过期。

正式 Build Manifest 使用 schema v4。一次 build 的所有所选 Manifest 必须包含相同的非空 `batch_id`，并分别记录其 `sealed_revision_id`；缺失时显示对应的 `stale_fields`。每份 Manifest 还记录同一规范化 `builder_fingerprint`：ProbHub/Core 版本、Manifest 与 generation schema、Typst、pypdf、LF 规范化模板 hash 和包内固定字体字节 hash。Node、g++、Flask、操作系统等不影响 PDF/ZIP 字节的诊断环境不进入该指纹。

旧 v1/v2/v3 Manifest 会以 `stale_fields: ["manifest_schema"]` 明确要求重建，不会被静默视为 `current`。指纹变化按 `builder_fingerprint.<field>` 精确报告；当前机器无法确定 Typst、pypdf 或固定字体身份时报告 `builder_fingerprint.unavailable` 与 `builder_fingerprint_error`，已有产物不会被误报为 `current`。

## 8.1 `report`

```powershell
probhub report [ID...]
probhub report [ID...] --format markdown
probhub --json report [ID...]
```

生成只读工作区体检，默认输出适合终端阅读的文本；`--format markdown` 输出 Markdown；全局 `--json` 输出 `report schema v1` 结构化文档并优先于文本格式。报告按正式题序汇总：

- 题号、稳定 ID、题名、难度与标签；
- sample/secret 数量、输入/答案字节规模和最大输入；
- `data.groups` 的角色、targets、命中用例数和 secret 占比；分组可重叠，因此占比之和不要求为 100%；
- `data.recipes` 覆盖率，以及无 recipe、随机型比例过高、缺少定向 recipe、缺少近上界信号等结构化 warning；
- 最近一次完整成功 Judge evidence 中 accepted 的 TL headroom；本机测量固定 `target_guarantee: false`；
- 按错解 × 数据组展示声明目标和当前 evidence 的击杀矩阵：`killed`、`missed`、`unknown`、`not-targeted`。

recipe 的随机/定向/近上界分类是显式标注的启发式分析：读取 recipe case/args、数据组 role/targets，以及与题面直接上界相等的参数。它用于提醒人工复核，不证明生成器真的覆盖了算法边界。

`report` 不运行编译器、Judge、Generator 或 Typst，也不创建 `.gitignore`、evidence、PDF、ZIP、metadata、Manifest 或报告文件。存在待恢复事务时返回 `recovery_required`；lint 错误使退出码非零，warning 不改变退出码。

## 8.2 `sample-check`

```powershell
probhub sample-check [ID...] [--no-cache]
```

只编译并运行 `data/sample` 与配置顺序中的首个 accepted，不运行 Validator、brute、wrong 或 Custom Checker。accepted stdout 与 `.ans` 只把 CRLF/裸 CR 归一为 LF，之后执行严格字节比较；尾空格、缺少尾换行和其他字节差异均返回 `sample_answer_mismatch`。Custom Checker 即使允许非唯一输出，也不能替代正式样例答案必须由首个 accepted 精确复现的不变量。

交互题返回成功但 `applicable: false` / `sample_check_not_applicable`。命令可复用并更新忽略的编译/样例 case cache；`--no-cache` 强制重跑当前样例但保留无关缓存项。它不运行完整 Judge、不发布或覆盖 `judge-evidence-v2.json`，也不写规范源、PDF、ZIP、metadata 或 Manifest。完整 `judge`、`seal` 与 `build` 同样执行该样例不变量，因此错误 `.ans` 会在正式交付前被确定性拦截。

## 9. `judge`

```powershell
probhub judge [ID...] [--no-cache]
```

执行：

1. 编译并运行 Validator。
2. 编译 `solutions.accepted`、`solutions.brute`、`solutions.wrong`。
3. 按每个 solution 的运行域逐点评测；未配置 `run_on` 时覆盖全部数据，配置后取所列数据组并集，sample 始终隐式执行。
4. 对非交互题，严格核对首个 accepted 的 sample stdout 与 `.ans`；仅归一换行，Custom Checker AC 不能掩盖字节不一致。
5. 按 `judge.type` 使用标准比较、Checker 或 Interactor。
6. 根据每个程序的结构化 `expected` 宿命验证状态、目标数据组与禁止状态；只基于实际执行域计算，未配置时保持 accepted 全 AC、brute 不 WA 且至少 TLE/MLE/OLE、wrong 至少一个非 AC 的默认语义。
7. 对普通程序、Checker、Validator、编译器和 Interactor 应用完整进程树、时间、内存、输出与进程数控制。
8. 为每个解法汇总 `run_on`、实际执行/跳过用例、`max_time`、最大用例、`time_limit_ratio` 和 headroom；对期望 TLE 的已命中用例执行延长时限校准探针，MLE/OLE 报告可证明的阈值下界。

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

数据逻辑分组、`solutions.*[].expected`、`run_on`、`independence`、默认宿命和首个击杀用例字段见 `references/data-groups-expectations.md`。

运行域规则：首个 accepted 禁止 `run_on`；第二及后续 accepted 缩域时必须显式声明 `expected.groups`，且期望组和隐式目标组都必须位于执行域。未知/空组会被 lint 拒绝。运行域仅控制本地 Judge，不改变 `stress` 或 DOMjudge 包。结构化结果公开执行与跳过用例，`forbid`、expectation、calibration 都不会越过实际执行域。仅修改分组、宿命或运行域可复用仍匹配的逐点缓存，并重新计算断言与 evidence。

`independence` 是第二 accepted 对另一 accepted 的作者声明：`from` 指明被互证实现，`basis` 为 `algorithm` 或 `key_implementation`，`note` 解释具体差异。Core 只阻断能确定的反证（同路径、同字节、直接 include 复用），不把声明冒充自动算法证明。`difficulty >= 4` 的题目若没有额外覆盖全域的 AC 参考实现会给结构化 warning。

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

每个 `summary` 事件新增 `calibration`：

```json
{
  "schema_version": 1,
  "max_time": 0.214321,
  "max_time_case": "secret/max01",
  "time_limit": 1.0,
  "time_limit_ratio": 0.214321,
  "headroom_factor": 4.6659,
  "fresh_cases": 30,
  "cached_cases": 0,
  "accepted_check": {
    "applicable": true,
    "required_multiplier": 3.0,
    "ok": true
  },
  "resource_kills": []
}
```

期望 TLE 的 `resource_kills` 会记录用例、正式 TL、延长探针上限、观测比值、`exact|lower_bound|inferred|unavailable` 证据类型和进程结果。MLE/OLE 的 `lower_bound` 分别来自明确触限的受控进程树峰值内存与截断前 stdout+stderr 总字节数；接近 ML 后异常退出但未观测到明确 `memory_limit` 的结果只标为 `inferred`。`expected.status` 列表是备选结果，只有实际发生的资源状态才产生余量诊断。完整成功的 Judge 会在单题证据锁内原子写入 `<problem>/.probhub/judge-evidence-v2.json`；失败、取消或外层超时不覆盖上一份成功证据。旧 v1 文件继续被忽略，但不会被当前校准状态读取。

所有校准 JSON 都包含 `target_guarantee: false`。本机测量不是正式评测承诺：Windows 与 Linux/DOMjudge 的进程启动、链接、调度、计时和内存口径不同，正式限制必须在目标 Linux 评测机重新校准。

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

缓存事件包含 `mode`、`compile_hits/misses`、`validator_hits/misses`、`case_hits/misses` 和 `probe_hits/misses`。进程树、OLE、校准探针或资源限制语义变化会提升缓存 Schema，防止旧结果绕过新策略。

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

`typeset` 与正式 `build` 共用工作区写锁、全量 collection lint、隔离输入快照、提交前输入栅栏和 journal/rollback 发布。Typst 编译、任一题切页或正式替换失败时，已有 `meta.json`、`problems.json`、全卷 PDF 和单题 PDF 均保持原字节；只发布全题 metadata、全卷产物和所选单题 PDF，不触碰 ZIP、DOMjudge 配置或 Manifest。

当前模板会在每题首页写入透明但可提取的稳定 ID 标记，Core 要求标记与正式题序一一对应、各出现一次且首页严格递增；题号按 `A` 到 `Z`、`AA`、`AB` 继续。单题页段从自己的标记页开始，到下一题标记页之前结束：封面、目录及首题前空白页不属于任何单题，题目内部空白页和最后一题后的尾页归入其所在题目。任一标记缺失、重复、未知或乱序都会以 `pdf_boundary_invalid` 失败。

未升级的旧 Typst 模板继续按可见题名回退切页，但归一化后大小写、全角字符或空白相同的题名会被 lint 拒绝，不能再静默取得另一题的 PDF。长标题和 27 题以上的可靠切页应升级到当前模板标记协议。

## 12. `package`

```powershell
probhub package [ID...]
probhub package L01 --allow-missing-pdf
```

执行：

1. 取得工作区写锁，只 lint 所选题目，并把所选规范源及现有 `problem.pdf` 复制到隔离快照；不要求整场 seal。
2. 在快照中从 `probhub.yaml` 生成 DOMjudge `problem.yaml` 与 `domjudge-problem.ini`。
3. 将配置的 sample/secret 目录映射到标准 `data/sample`、`data/secret`，并把精确小写扩展名的 `.in`/`.ans` 以 LF-only 字节流写入同卷临时 `<ID>.zip`；规范源文件保持不变。
4. 打开 ZIP 前先按文件大小和中央目录逐项计数执行上限检查，再流式安全解压；限制单条目、总解压体积与压缩方式，拒绝路径逃逸、非小写数据扩展名、大小写冲突、符号链接、可执行文件、缓存和未知路径。
5. 严格解析并对账题名、TL、ML 与 Judge 类型；`domjudge-problem.ini` 拒绝重复、未知或畸形键；逐字节核对规范源数据，编译包内 output validator，并用题目输入 Validator 检查全部包内 `.in`。
6. 全部所选题目验证成功且 live 输入未变化后，通过同一 journal/rollback 事务一次发布配置、output validator 与 ZIP；任一题准备或提交失败时整批保持原字节。

默认要求已有 `problem.pdf`。`--allow-missing-pdf` 只用于尚未排版的中间状态，不用于正式交付。

`package` 不执行 judge 或 typeset，也不写 meta、PDF、Manifest；它只 lint/打包所选题目。正式流程优先使用 `build`。

## 13. `build`

```powershell
probhub build [ID...] [--skip-judge] [--no-cache]
```

顺序：

1. 取得 `.probhub/build.lock` 的跨平台 OS 排他锁；锁文件可以长期存在，只有 OS 锁状态表示占用。
2. lint 全部 collection 依赖，建立包含正式题序、全部排版输入和所选 source/data hash 的 BuildPlan。
3. 要求 collection 中每道题的最新 checkpoint 均为与 live source/data 匹配的 sealed revision；否则以 `sealed_revision_required` 失败且不创建构建快照。
4. 复制受控输入快照；后续 judge、排版和打包只读取该快照。
5. judge 所选题目。
6. 在快照中编译一次完整 Typst 集合并提取所选单题 PDF。
7. 在快照中生成 DOMjudge 配置、构建并验证全部所选 ZIP。
8. 为所有所选题目生成带同一 `batch_id` 与各自 `sealed_revision_id` 的 Manifest。
9. 发布前重新计算 live 输入哈希，并复核所有 sealed revision；变化时以 `inputs_changed` 或 `sealed_revision_changed` 失败。
10. 全部准备成功后发布共享产物与所选产物，Manifest 最后替换。

即使执行 `build L01`，也会为了题序与页码编译全卷，并要求整场所有题目都已 seal，但只评测、提取、打包和更新 L01。正式发布推荐在全部题目 seal 后一次传入全部 ID。

执行 `build L01 L02 ...` 时，所选题目逐题评测，但完整 Typst 集合只编译一次。任一题在 judge、PDF 提取、配置、ZIP 构建或验证阶段失败时，正式 metadata、PDF、ZIP 与 Manifest 均保持不变。所有所选 Manifest 使用同一份 `workspace_hash`、`collection_hash` 和 `batch_id`。

并发执行 `build`、`typeset` 或 `package` 时，第二个 writer 失败并在 JSON 中返回 `code: "build_busy"`。快照创建或发布前发现 live 输入变化时返回 `code: "inputs_changed"`。快照 I/O 与发布 I/O 分别使用 `snapshot_failed`、`publish_failed`。正式发布先把全部文件和目录复制到工作区同卷事务目录，写入 journal，再备份并替换目标；中途失败会回滚。build、gen 和 fixate 的 writer 在读取题目配置前统一恢复三类硬中断 journal；只读 lint/status/judge 在恢复前返回 `recovery_required`。journal 的 committed 标记保证“已经发布但清理失败”的事务只会重试清理，不会被错误回滚。回滚或恢复本身失败时保留事务目录并返回对应的 rollback/recovery 错误，不得手工删除恢复材料。

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
probhub --workspace <工作区> verify-package L01.zip --require-pdf --problem L01
```

检查：

- 所有成员的路径、大小写/Unicode 规范冲突、普通文件类型、可执行/缓存条目和压缩方式。
- 在构造 `ZipFile` 前限制归档字节数并流式核对中央目录条目数；随后复核单条目、总解压体积和实际流式解压结果，不使用无界 `archive.read()` 或 `extractall()`。
- 根配置、题名、正数 TL/ML、standard/custom/interactive 映射、严格且无重复键的 `domjudge-problem.ini`，以及包内 output validator 可编译性。
- `data/sample`、`data/secret`、精确小写 `.in`/`.ans` 配对及 LF-only 文本。
- 必需 PDF。

不提供 `--problem` 时，结果的 `verification_scope` 为 `structural`，只证明 ZIP 自身的结构与安全边界。提供工作区题目上下文后为 `deep`：额外把题名、限制、Judge 类型和测试数据与 `probhub.yaml`/规范源对账，并编译运行该题输入 Validator 检查所有包内输入。结构化结果通过 `diagnostics[].code`、`entry` 和大小/换行证据报告失败原因。

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
probhub seal L01 --no-cache --seed 12345
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

### `invalid_yaml`

工作区或题目 YAML 无法解析，或文档根不是 mapping。修复结构后重试；`--json` 模式会返回结构化错误，不输出 traceback。

### `sealed_revision_required`

正式 build 要求整场每道题都已 `seal`，并且 seal 后未再修改 live source/data。对提示中的题目重新运行 `seal <ID>`，待全部题目 sealed 后执行一次多 ID build。

### `problem.pdf` 缺失

先执行：

```powershell
probhub typeset L01
```

或直接执行完整 `build L01`。

### `builder_fingerprint_failed` / `builder_changed`

正式 build、typeset、assemble 和 seal 需要确定的构建器身份。Typst 或 pypdf 缺失、包内固定字体缺失/损坏时以 `builder_fingerprint_failed` 阻断；运行期间工具版本、模板或字体身份变化时以 `builder_changed` 阻断发布。先运行 `probhub doctor` 修复环境，再重新执行原命令。只读 `status` / `generation-status` 会把无法探测身份报告为 stale，而不会把文件误判为损坏。
