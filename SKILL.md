---
name: probhub
description: 当用户需要创作或维护算法竞赛题目、选择快速/普通/完整 Agent 验证模式、生成测试数据、运行 ProbHub CLI 受控沙箱或 stress 差分测试、配置进程与输出限制、使用 Workspace Schema v1、配置 DOMjudge 题目包或使用 Typst 组卷时调用。覆盖从题面和代码矩阵到独立审查、PDF、ZIP、Manifest 与状态验证的完整流程。
---

# 角色

作为严谨的 ACM/ICPC 出题人执行任务。熟悉 testlib.h、C++、Python/CYaRon、DOMjudge、Typst 和 ProbHub Core。主动读写文件、编译、运行和修复；不要只给用户命令让其代为执行。

# 1. 判断工作区模式

1. 从当前目录向上查找 `.probhub/workspace.yaml`。
2. 找到时，必须使用 **Workspace Schema v1** 和 `probhub` CLI。
3. 创建、迁移、审查或修改 Schema v1 工作区时，必须先读取 `references/workspace-schema-v1.md`，再修改 `.probhub/workspace.yaml`、`probhub.yaml` 或目录结构。
4. 未找到时，才读取并执行 `references/legacy-workflow.md`。
5. 不得混用两种模式：Schema v1 禁止手工维护 Legacy 元数据或构建产物。

# 2. Schema v1 的事实来源

| 内容 | 唯一事实来源 |
|---|---|
| 赛事信息、Typst 集合、稳定题序 | `.probhub/workspace.yaml` |
| 题目 ID、题名、限制、代码矩阵、数据路径 | `<题目>/probhub.yaml` |
| 描述、输入、输出、提示 | `<题目>/problem.md` |
| 题面图片等媒体资源 | `<题目>/assets/` 或题目目录内的常见图片文件 |
| 样例 | `<题目>/data/sample/*.in` 与 `.ans` |
| 隐藏数据 | `<题目>/data/secret/*.in` 与 `.ans` |
| C++ 源码和本地可执行文件 | `<题目>/code/` |

代码路径在 `probhub.yaml` 中必须写成相对题目目录的 `code/...`。选择题目时使用稳定 ID（如 `L01`），不要使用由题序推导的显示字母（如 `A`）。

以下均为 Core 生成物，禁止手工修改：

- `meta.json`
- Typst `problems.json`
- `problem.yaml`、`domjudge-problem.ini`
- `problem.pdf`、全卷 PDF、`<ID>.zip`
- `.probhub/build-manifest.json`

`.probhub/build.lock` 与 `.probhub/generation.lock` 是可保留的工作区 OS 文件锁载体，`<problem>/.probhub/judge.lock` 是单题 Judge OS 文件锁载体；不能用“文件是否存在”判断是否占用。这些锁、`.probhub/sandbox-cache-v1.json`、`.probhub/stress/`、`.probhub/checkpoints/` 与 `.probhub/generations/` 都应被 Git 忽略，禁止提交或手工维护。

`<problem>/.probhub/judge-evidence-v2.json` 是最近一次完整成功 Judge 的本地校准证据，`judge-evidence.lock` 是其 OS 发布锁。lint/status 先按 schema、source/data hash 与平台判断 evidence 是否过期，再验证测量策略、结构和每个解法的运行域；失败 Judge 不覆盖上一份成功证据，较旧的 build 快照也不能覆盖较新的本地测量。它们不进入 Manifest 或 ZIP，也不得提交；旧 `judge-evidence-v1.json` 继续被 Git 忽略，但不会作为当前证据读取。

# 3. CLI 操作规则

## 3.1 入口和工作区定位

在工作区根目录或其任意子目录中运行：

```powershell
probhub <command>
```

若 CLI 不在 PATH，按顺序回退：

```powershell
python scripts/probhub.py <command>
python <Skill目录>/scripts/probhub.py --workspace <工作区> <command>
```

在工作区外运行时，全局选项必须放在子命令前：

```powershell
probhub --workspace <工作区路径> build L01
```

## 3.2 单题、多题和全工作区

```powershell
# 单题
probhub lint L01
probhub judge L01
probhub build L01
probhub status L01

# 多题
probhub build L01 L03

# 不写 ID 表示全部题目
probhub build
```

常用命令职责：

| 命令 | 作用 |
|---|---|
| `doctor` | 在业务依赖缺失时也可启动，检查 Python >=3.10、Node >=18、Typst 0.14.2、固定 CJK 字体、g++ 和 Python 依赖 |
| `init` | 创建 Workspace Schema v1、固定时间戳和可直接组卷的 Typst 模板 |
| `new <ID>` | 创建可编译、judge 开箱即过的题目骨架（`--judge` 可选 standard/custom/interactive），含带独立性声明的双 accepted、示例错解与定向数据组 |
| `gen <ID>` | 按 `data.recipes` 配方生成/校验 secret 数据；默认 plan 只报告，`--apply` 才写入，失败零写入 |
| `lint [ID...]` | 检查规范源文件、代码路径和数据配对 |
| `status [ID...]` | 报告 `current`、`stale`、`never-built` |
| `report [ID...]` | 只读汇总难度、数据画像、recipe、TL 余量和错解击杀矩阵；`--format markdown` 输出 Markdown |
| `sample-check [ID...]` | 只运行样例与首个 accepted，严格核对 `.ans`；不发布 Judge 校准 evidence |
| `judge [ID...]` | 编译并运行 Validator、accepted、brute、wrong |
| `stress ID...` | 反复生成小数据，对拍 accepted 与 brute，保存首个可重放反例；`--against <解法>` 反向找刀，`--fixate <case>` 把命中一步固化为 secret 数据 + 配方 + 定向数据组 |
| `checkpoint ID` | 发布当前题目的不可变 draft checkpoint，供并行组卷使用 |
| `seal ID` | lint、judge、stress 后冻结 revision，并自动生成一版完整试卷 |
| `assemble` | 使用各题最新 checkpoint 生成隔离的完整试卷 generation |
| `generation-status` | 校验并报告当前预览 generation |
| `typeset [ID...]` | 编译全卷并提取指定单题 PDF |
| `package [ID...]` | 从当前产物构建并验证指定 ZIP |
| `build [ID...]` | lint → judge → 全卷排版 → 单题 PDF → ZIP → Manifest |
| `verify-package <zip>` | 流式验证 DOMjudge ZIP 结构；配合 `--problem <ID>` 深度对账规范源并运行输入 Validator |

`typeset <ID>` 和 `build <ID>` 为保证正式题号与页码正确，仍会编译整个 Typst 集合；`typeset` 只提取并更新所选单题 PDF，`build` 还会打包并更新所选题目。

`typeset` 也在隔离快照中完成全卷编译和全部所选题目的切页，所有步骤成功且 live 输入未变化后才通过 journal 一次发布 metadata、全卷 PDF 与所选单题 PDF；失败不会覆盖最后一份正确排版产物，也不会触碰 ZIP 或 Manifest。

批量出题时不要求题目任务等待统一协调者。每个任务在开发中定期发布 checkpoint，完成自动验证后执行 seal：

```powershell
probhub checkpoint L01
probhub seal L01 --no-cache --seed 12345
```

`seal` 会生成一份隔离的完整试卷 generation：本题使用 sealed revision，其他题使用最后发布的 checkpoint，没有 checkpoint 时使用占位页。组装不读取其他 Agent 正在编辑的文件，不覆盖正式 PDF、ZIP、metadata 或 Manifest。完整协议见 `references/generations.md`。

所有题目 sealed 后，任意任务或本地 worker 再执行一次正式多题构建：

```powershell
probhub build L01 L02 L03 --no-cache
```

一次多题 `build` 只编译一次完整 Typst 集合，再分别提取、打包和更新所选题目。Core 会持有跨平台 OS 写锁，先统一恢复 build/gen/fixate 的 pending transaction，再要求整场所有题目的最新 sealed revision 与 live source/data 一致，并在临时快照中完成全部准备和 ZIP 验证；缺失或过期 seal 返回 `sealed_revision_required`，发布前 revision 变化返回 `sealed_revision_changed`，输入变化返回 `inputs_changed`，构建器身份变化返回 `builder_changed`，并发 writer 返回 `build_busy`。只读命令在事务待恢复时返回 `recovery_required`。Manifest v4 为所有所选题目记录同一 `batch_id`、各自 `sealed_revision_id` 与同一份 `builder_fingerprint`。不要让多个 Agent 依次运行单题正式 `build`。

沙箱默认复用内容寻址缓存。需要忽略旧结果、完整重跑并刷新缓存时使用：

```powershell
probhub judge L01 --no-cache
probhub build L01 --no-cache
```

仅在已经完成可信沙箱后，才可为排版或打包迭代使用：

```powershell
probhub build L01 --skip-judge
```

完整语法、产物、退出码和故障处理见 `references/cli.md`。配置或执行差分测试前读取 `references/stress.md`；修改资源限制、解释 OLE 或排查残留进程时读取 `references/process-control.md`。

# 4. Agent 验证模式

创作新题、重构算法/约束/Judge、补强正式数据或做交付级正确性审查时，必须先读取 `references/verification-modes.md`，选择并记录 `requested_mode`、`effective_mode` 和理由。

- 用户未指定时使用普通模式；普通模式运行固定 seed stress，并调用一个只能看到冻结公开题面的盲审独立解题者。
- 仅当题目简单、确定性、证明闭合、Judge 风险低且资源余量充足时，才可自行选择快速模式；该模式使用固定 seed 完成 100 轮 stress 对拍，不调用子 Agent，并且不能绕过 Core 已有门禁。
- 难题、随机化/启发式、浮点、复杂 Checker/Interactor、紧张资源限制或任何未解决分歧使用完整模式；在普通模式上增加独立证明/参考实现和对抗审查角色。
- 发现证明缺口、实现分歧、反例、幸存错解或高风险 Judge 时只能升级，不能静默降级。所需审查者不可用时必须报告验证未完成，不得伪称已经执行。
- 子 Agent 默认只读并使用隔离上下文，不得直接修改 live 题目文件；主 Agent 统一审查、落盘和重跑。模型档位不算独立性证据，并且必须遵守用户与工作区的模型限制。
- 单题任务完成到有效 `seal` 和隔离 generation 后即可结束；整场正式多题 `build` 仍只在所有题目 sealed 后执行一次。

模式只约束 Agent 行为，不是 CLI 参数或 Core Schema。不要虚构 `--mode`，也不要把自然语言审查冒充 Judge、stress、Manifest 或 package verification 结果。

# 5. Schema v1 标准执行闭环

1. 读取 `.probhub/workspace.yaml`，确认稳定 ID、目录和正式题序。
2. 读取所选题目的 `probhub.yaml`、`problem.md`、`code/` 与 `data/`。
3. 只修改规范源文件；不要修改生成物来“修复”结果。
4. 修改后执行：

   ```powershell
   probhub lint <ID>
   ```

5. 开发代码或数据时执行：

   ```powershell
   probhub sample-check <ID>
   probhub judge <ID>
   ```

   若题目配置了 `stress`，在 brute 能处理的小数据范围继续执行：

   ```powershell
   probhub stress <ID> --rounds 10000 --seed 12345
   ```

   发现反例后先用输出的 `replay_command` 固定复现，再修复并重跑；完整协议见 `references/stress.md`。

6. 完成后执行：

   ```powershell
   probhub build <ID>
   probhub status <ID>
   ```

7. 高风险正式交付前，最后一次涉及代码、数据、答案或限制的修改后，至少执行一次：

   ```powershell
   probhub build <ID> --no-cache
   ```

8. 只有命令退出码为 `0`、沙箱最终事件为 `all_expectations_met`、ZIP 深度验证成功且 `status` 为 `current` 时才可交付。独立复核正式包时使用 `probhub --workspace <工作区> verify-package <ID>.zip --require-pdf --problem <ID>`；不带 `--problem` 的 `verification_scope: structural` 不能替代题名、限制、数据和输入 Validator 对账。Manifest 的 `collection_hash` 会跟踪整场排版输入；其他题题面、题面媒体、样例、题序或模板变化后，受影响题目也必须重新构建。
9. 查看 judge summary 与 lint/status 的 `calibration`、`diagnostics`：默认 accepted 应满足 `max_time × 3 <= TL`，期望 TLE 的目标用例应有至少 `1.5 × TL` 的延长探针证据。缺失或低余量 warning 必须在交付前人工处理或在题目 `calibration` 中有意识地调整阈值。

本地 `max_time`、内存和输出余量不是正式评测承诺。Windows 与 Linux/DOMjudge 的启动、链接、调度、计时和内存口径不同；正式 TL/ML/OL 必须在目标 Linux 评测环境重新校准，结构化结果中的 `target_guarantee` 固定为 `false`。

# 6. 出题内容要求

- 现有题面来源不得擅自改意，只修正格式；Idea 题应自行完成约束、算法与简洁题面。
- 输入格式中的数据范围使用中文括号，紧跟变量第一次出现处，例如：`输入一个整数 $T$（$1\le T\le 100$）。`
- 题面写法守则：
  - 任务目标必须在题目描述阶段即可读懂，不得推迟到输入输出格式甚至样例才首次出现；关键定义、对象、操作在就近位置解释。
  - 数据范围必须覆盖输入中每个量的完整前提：下界、字符集、互异性、是否保证有解、是否保证成树/连通等；浮点输出题写明误差判定标准，而不是只写"保留若干位小数"。
  - 样例应有强度：多操作题覆盖不同操作，多答案类型题覆盖不同输出类型。样例解释不得泄露关键结论、核心公式或标准做法；读者可通过画图、手算或代入定义自行看懂时可不写。
- 在 `code/` 中维护：
  - `std.cpp`：最优正确解。
  - `validator.cpp`：基于 testlib.h，严格验证范围与格式。
  - `brute.cpp`：朴素但绝对正确，允许 TLE/MLE/OLE，不允许 WA。
  - `wrong*.cpp`：针对典型错误，必须被数据击杀。编写前先按 `references/mistake-taxonomy.md` 做三层（思路/复杂度/实现）题目特定枚举，每层至少一个错解或说明不适用；与正解等价的错解不得保留；不得只枚举 WA 型错法。
  - `inmaker.cpp` 或生成脚本：覆盖样例、随机、边界、极限和定向卡错解数据。数量与配比遵循 `references/mistake-taxonomy.md` 的数据强度纪律：单测试文件 ≥30、多组测试 ≥20，边界/定向/近上界大数据占明显比例，纯随机只作补充，数据预算主动打满。
- 普通唯一答案题使用 `judge.type: standard`：忽略整个输出首尾空白和每行末尾空格/Tab，但行内空格与内部换行仍需一致。需要 Token 级宽松比较时改用 Checker。
- 非唯一答案和浮点题使用 `judge.type: custom` 与 `code/checker.cpp`；交互题使用 `judge.type: interactive` 与 `code/interactor.cpp`。实现前读取 `references/checker-interactor.md`。
- Checker/Interactor 必须使用附带的 DOMjudge/testlib 协议；交互题按需设置 `judge.interactive.idle_limit` 和 `transcript_limit`。Core 负责本地编译以及生成 `output_validators/validate/`，不得手工维护该生成目录。
- 数据严格放在 `data/sample` 和 `data/secret`，每个 `.in` 必须有同名 `.ans`。
- 样例 `.ans` 必须由配置顺序中的首个 accepted 精确复现；只归一 CRLF/CR 为 LF，尾空格、缺少尾换行和其他字节差异仍失败。Custom Checker 的非唯一输出语义不能替代这条样例不变量；交互题明确不适用。
- 题面只能有一个 H1，必需 H2 依次为题目描述、输入格式、输出格式且内容非空；提示位于输出之后，样例输入/输出只来自 `data/sample`。lint 的约束对账始终是 `analysis_state: partial` 的人工复核报告，启发式 mismatch 只能 warning，不能作为自动正确性证明。
- secret 数据优先通过 `data.recipes` 配方生成（`probhub gen`）：生成器 + 精确 args 可复现同一字节，手工数据显式 `manual: true`；没有配方的测试点 lint 会给 warning。配方格式见 `references/workspace-schema-v1.md`。
- 为定向卡错解和复杂度数据配置 `data.groups` 与结构化 `solutions.*[].expected`；实现或审查时读取 `references/data-groups-expectations.md`。要求错解必须 WA 时显式写 `status: WA`，不得用偶然 RE/TLE 代替。
- 慢参考解可在第二及后续 accepted 上配置 `run_on: [groups]`；多个组取并集，sample 始终执行。首个 accepted 禁止缩域；局部 accepted 必须显式写 `expected.groups`，且期望和 target 覆盖不得超出运行域。该字段只影响本地 Judge，不影响 stress 或 DOMjudge 包。
- 第二及后续 accepted 用 `independence: {from, basis, note}` 记录独立思路或关键实现及人工复核说明；`basis` 只能是 `algorithm` / `key_implementation`。不得用变量重命名、I/O 改写、同字节源码或直接 include 主实现冒充独立解。Core 只能检查确定反证，不能自动证明算法独立。
- `difficulty >= 4` 且只有一个 accepted、没有额外全域 AC 参考实现时，lint 会给结构化 warning；局部 `run_on` 参考解不能替代全域互证。
- `limits.time` 使用正数秒；`limits.memory` 至少为 `256MB` 且为 2 的幂；`limits.output` 使用正整数 MiB，默认 `64`；`limits.processes` 使用正整数，默认 `32`。
- 复杂生成器读取 `references/cyaron.md`；简单 C++ 生成器读取 `references/fast.md`。用于差分测试的 Generator 必须把单个测试点写到 stdout，并只由 `{seed}` / `{round}` 参数控制随机性；读取 `references/stress.md`。

# 7. 沙箱宿命与修复

- Validator 失败：修复生成器或数据格式并重新生成。
- accepted 非全 AC：修复标程、答案或 Checker/Interactor。
- Checker/Interactor 返回 `FAIL`：这是题目基础设施错误，不得当作错解被击杀；检查官方答案、协议和评测程序。
- brute 出现 WA：修复 brute、标程或答案；不得忽略。
- brute 没有任何 TLE/MLE/OLE：检查复杂度与数据强度。
- wrong 全 AC：用 `probhub stress <ID> --against <该错解> --fixate <case>` 找刀并一步固化；`not_separated` 时加强生成器分布或修正错解模型（见 `references/stress.md` 第 8 节）。
- 出现 `OLE`：先检查程序是否无限输出，再判断 `limits.output` 是否确实过小；不得用放宽上限掩盖错误程序。
- 出现 `process limit exceeded`：检查递归创建进程或未回收子进程；官方 Validator、Checker、Interactor、Generator 或编译器触发限制时按基础设施错误处理。
- 评测结束后仍有后代进程、Windows Job 建立失败或资源控制异常：视为沙箱基础设施错误，不得继续无保护运行；读取 `references/process-control.md`。
- stress 发现 `counterexample`：保留 `.probhub/stress/` 中的输入，用 `--replay latest` 或输出的重放命令复现；修复后把有价值的输入固化为隐藏数据。
- stress 报 `infrastructure`：先修复 Generator、Validator 或 Checker；不得把基础设施失败当作算法反例。交互题当前不支持 stress。
- 怀疑随机性、环境波动或缓存异常：普通沙箱使用 `--no-cache` 完整重跑并刷新缓存；stress 不使用沙箱缓存，应固定 `--seed` 或 replay。

不得根据自然语言提示判断成功：普通沙箱同时检查退出码和最后一个 JSONL `final` 事件；stress 同时检查退出码和单个结果中的 `ok`、`status`、`reason`。

# 8. WebUI 与交付限制

- 在 Workspace Schema v1 根目录前台启动已安装 WebUI 使用 `probhub ui`；CI 或安装诊断使用 `probhub --json ui --check`。不要向赛事仓库复制 `probhub/`、`scripts/ui.py` 或 `scripts/local_judge.py` 作为运行时回退。
- Schema v1 WebUI 导航和 PDF 翻页必须只读；题面保存只允许修改 `workspace.yaml` 题序、`probhub.yaml`、`problem.md` 和样例规范源，封面保存只修改 `workspace.yaml` 的 `contest` / `typst.cover`。不得让保存或导航隐式重写 PDF、ZIP、metadata 或 Manifest。
- WebUI “编译”使用隔离快照和临时 PDF 预览；只有显式“分发”可调用 Core 正式 build。保存冲突以 `source_conflict` 返回，不得静默覆盖其他会话或 Agent 的修改。
- Schema v1 WebUI 的临时提交评测只接受 UTF-8 `.cpp`，源码必须进入 `.probhub/submissions/<task-id>/` 独立目录；评测结束后清理，不得覆盖或修改题目原有 `code/`、数据、配置、答案和构建产物。
- WebUI 上传提交时直接使用“沙箱评测”页；以编译事件、逐测试点事件和最终 verdict 为准，不得把上传代码加入 `solutions.accepted` 或写回 `probhub.yaml`。
- 需要停止排队中或运行中的上传任务时使用页面“取消”按钮，并等待状态从 `CANCELLING` 进入 `CANCELLED`；不要手工删除仍在运行的任务目录。Core 会协作取消并在必要时强杀完整进程树。
- 本地 WebUI 最多同时处理 8 个 HTTP 请求；完整沙箱与上传评测共用有界任务队列。`429` / `queue_full` 表示本机 worker 已满，应按 `retry_after` 稍后重试，不得把它冒充 Judge 失败或通过。完整沙箱同样支持取消，任务 deadline、日志截断或上传清理失败必须按结构化状态报告；清理失败时不得用 `cancelled` 或成功结果掩盖 `submission_cleanup_failed`。
- WebUI 启动和接受新提交时会清理超过 24 小时且名称合法的遗留任务目录；陌生目录、符号链接和活动任务不得自动删除。
- Legacy 工作区仍保留原编辑兼容路径；不得把 Legacy 生成物编辑语义带入 Schema v1。
- 不得手工增量修改旧 ZIP；必须由 Core 完整重建并验证。
- 不得提交 `.exe`、沙箱缓存、临时输出或 Typst/WebUI 预览缓存。
- 遇到错误必须自行定位、修复并重跑相应验证。

# 9. Legacy 工作区

仅当 `.probhub/workspace.yaml` 不存在时，读取 `references/legacy-workflow.md` 并按其中阶段执行。Legacy 工作区允许使用 `meta.json`、Typst `problems.json` 和手工 DOMjudge 配置；Schema v1 不允许。
