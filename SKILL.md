---
name: probhub
description: 当用户需要创作或维护算法竞赛题目、生成测试数据、运行 ProbHub CLI 受控沙箱或 stress 差分测试、配置进程与输出限制、使用 Workspace Schema v1、配置 DOMjudge 题目包或使用 Typst 组卷时调用。覆盖从题面和代码矩阵到 PDF、ZIP、Manifest 与状态验证的完整流程。
---

# 角色

作为严谨的 ACM/ICPC 出题人执行任务。熟悉 testlib.h、C++、Python/CYaRon、DOMjudge、Typst 和 ProbHub Core。主动读写文件、编译、运行和修复；不要只给用户命令让其代为执行。

# 1. 判断工作区模式

1. 从当前目录向上查找 `.probhub/workspace.yaml`。
2. 找到时，必须使用 **Workspace Schema v1** 和 `probhub` CLI。
3. 未找到时，才读取并执行 `references/legacy-workflow.md`。
4. 不得混用两种模式：Schema v1 禁止手工维护 Legacy 元数据或构建产物。

# 2. Schema v1 的事实来源

| 内容 | 唯一事实来源 |
|---|---|
| 赛事信息、Typst 集合、稳定题序 | `.probhub/workspace.yaml` |
| 题目 ID、题名、限制、代码矩阵、数据路径 | `<题目>/probhub.yaml` |
| 描述、输入、输出、提示 | `<题目>/problem.md` |
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

`.probhub/sandbox-cache-v1.json` 是被 Git 忽略的本地缓存；`.probhub/stress/` 保存可重放差分反例。两者都禁止提交或手工维护。

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
| `doctor` | 检查 Python、Node、Typst、g++ 和依赖 |
| `new <ID>` | 创建 Schema v1 题目骨架与 `code/` 目录 |
| `lint [ID...]` | 检查规范源文件、代码路径和数据配对 |
| `status [ID...]` | 报告 `current`、`stale`、`never-built` |
| `judge [ID...]` | 编译并运行 Validator、accepted、brute、wrong |
| `stress ID...` | 反复生成小数据，对拍 accepted 与 brute，保存首个可重放反例 |
| `typeset [ID...]` | 编译全卷并提取指定单题 PDF |
| `package [ID...]` | 从当前产物构建并验证指定 ZIP |
| `build [ID...]` | lint → judge → 全卷排版 → 单题 PDF → ZIP → Manifest |
| `verify-package <zip>` | 独立验证 DOMjudge ZIP |

`typeset <ID>` 和 `build <ID>` 为保证正式题号与页码正确，仍会编译整个 Typst 集合，但只提取、打包和更新所选题目。

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

# 4. Schema v1 标准执行闭环

1. 读取 `.probhub/workspace.yaml`，确认稳定 ID、目录和正式题序。
2. 读取所选题目的 `probhub.yaml`、`problem.md`、`code/` 与 `data/`。
3. 只修改规范源文件；不要修改生成物来“修复”结果。
4. 修改后执行：

   ```powershell
   probhub lint <ID>
   ```

5. 开发代码或数据时执行：

   ```powershell
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

8. 只有命令退出码为 `0`、沙箱最终事件为 `all_expectations_met`、ZIP 验证成功且 `status` 为 `current` 时才可交付。

# 5. 出题内容要求

- 现有题面来源不得擅自改意，只修正格式；Idea 题应自行完成约束、算法与简洁题面。
- 输入格式中的数据范围使用中文括号，紧跟变量第一次出现处，例如：`输入一个整数 $T$（$1\le T\le 100$）。`
- 在 `code/` 中维护：
  - `std.cpp`：最优正确解。
  - `validator.cpp`：基于 testlib.h，严格验证范围与格式。
  - `brute.cpp`：朴素但绝对正确，允许 TLE/MLE，不允许 WA。
  - `wrong*.cpp`：针对典型错误，必须被数据击杀。
  - `inmaker.cpp` 或生成脚本：覆盖样例、随机、边界、极限和定向卡错解数据。
- 普通唯一答案题使用 `judge.type: standard`：忽略整个输出首尾空白和每行末尾空格/Tab，但行内空格与内部换行仍需一致。需要 Token 级宽松比较时改用 Checker。
- 非唯一答案和浮点题使用 `judge.type: custom` 与 `code/checker.cpp`；交互题使用 `judge.type: interactive` 与 `code/interactor.cpp`。实现前读取 `references/checker-interactor.md`。
- Checker/Interactor 必须使用附带的 DOMjudge/testlib 协议；交互题按需设置 `judge.interactive.idle_limit` 和 `transcript_limit`。Core 负责本地编译以及生成 `output_validators/validate/`，不得手工维护该生成目录。
- 数据严格放在 `data/sample` 和 `data/secret`，每个 `.in` 必须有同名 `.ans`。
- 为定向卡错解和复杂度数据配置 `data.groups` 与结构化 `solutions.*[].expected`；实现或审查时读取 `references/data-groups-expectations.md`。要求错解必须 WA 时显式写 `status: WA`，不得用偶然 RE/TLE 代替。
- `limits.time` 使用正数秒；`limits.memory` 至少为 `256MB` 且为 2 的幂；`limits.output` 使用正整数 MiB，默认 `64`；`limits.processes` 使用正整数，默认 `32`。
- 复杂生成器读取 `references/cyaron.md`；简单 C++ 生成器读取 `references/fast.md`。用于差分测试的 Generator 必须把单个测试点写到 stdout，并只由 `{seed}` / `{round}` 参数控制随机性；读取 `references/stress.md`。

# 6. 沙箱宿命与修复

- Validator 失败：修复生成器或数据格式并重新生成。
- accepted 非全 AC：修复标程、答案或 Checker/Interactor。
- Checker/Interactor 返回 `FAIL`：这是题目基础设施错误，不得当作错解被击杀；检查官方答案、协议和评测程序。
- brute 出现 WA：修复 brute、标程或答案；不得忽略。
- brute 没有任何 TLE/MLE：检查复杂度与数据强度。
- wrong 全 AC：补充针对性数据或修正错解模型。
- 出现 `OLE`：先检查程序是否无限输出，再判断 `limits.output` 是否确实过小；不得用放宽上限掩盖错误程序。
- 出现 `process limit exceeded`：检查递归创建进程或未回收子进程；官方 Validator、Checker、Interactor、Generator 或编译器触发限制时按基础设施错误处理。
- 评测结束后仍有后代进程、Windows Job 建立失败或资源控制异常：视为沙箱基础设施错误，不得继续无保护运行；读取 `references/process-control.md`。
- stress 发现 `counterexample`：保留 `.probhub/stress/` 中的输入，用 `--replay latest` 或输出的重放命令复现；修复后把有价值的输入固化为隐藏数据。
- stress 报 `infrastructure`：先修复 Generator、Validator 或 Checker；不得把基础设施失败当作算法反例。交互题当前不支持 stress。
- 怀疑随机性、环境波动或缓存异常：普通沙箱使用 `--no-cache` 完整重跑并刷新缓存；stress 不使用沙箱缓存，应固定 `--seed` 或 replay。

不得根据自然语言提示判断成功：普通沙箱同时检查退出码和最后一个 JSONL `final` 事件；stress 同时检查退出码和单个结果中的 `ok`、`status`、`reason`。

# 7. WebUI 与交付限制

- 当前 Schema v1 WebUI 只用于只读预览和现有沙箱展示，不得通过 WebUI 保存题面或排序。上传代码并仅评测临时提交、且不覆盖题目 `code/` 的功能尚未实现；共享进程控制层只是该功能的基础。
- 不得手工增量修改旧 ZIP；必须由 Core 完整重建并验证。
- 不得提交 `.exe`、沙箱缓存、临时输出或 Typst/WebUI 预览缓存。
- 遇到错误必须自行定位、修复并重跑相应验证。

# 8. Legacy 工作区

仅当 `.probhub/workspace.yaml` 不存在时，读取 `references/legacy-workflow.md` 并按其中阶段执行。Legacy 工作区允许使用 `meta.json`、Typst `problems.json` 和手工 DOMjudge 配置；Schema v1 不允许。
