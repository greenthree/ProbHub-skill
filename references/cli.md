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

创建新题骨架并加入稳定题序：

```powershell
probhub new L05 --name "新题"
probhub new L05 --name "新题" --directory problems/L05
```

生成：

```text
<directory>/
├── probhub.yaml
├── problem.md
├── code/
└── data/
    ├── sample/
    └── secret/
```

`probhub.yaml` 默认声明 `code/validator.cpp`、`code/std.cpp`、`code/brute.cpp`、`code/wrong.cpp` 和 `code/inmaker.cpp`，但仍需实际编写这些文件。

## 5. `doctor`

检查环境：

```powershell
probhub doctor
```

用于确认 Python、Node/npm、Typst、g++ 和 Python 依赖。首次安装、换机器或 CI 失败时优先运行。

## 6. `lint`

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

## 7. `status`

```powershell
probhub status [ID...]
```

状态：

- `current`：规范源、数据、工作区、PDF、ZIP 与 Manifest 一致。
- `stale`：至少一个哈希与 Manifest 不一致；读取 `stale_fields` 定位。
- `never-built`：缺少 Manifest 或正式产物。

`status` 非 `current` 时返回非零退出码。

## 8. `judge`

```powershell
probhub judge [ID...] [--no-cache]
```

执行：

1. 编译并运行 Validator。
2. 编译 `solutions.accepted`、`solutions.brute`、`solutions.wrong`。
3. 对 `data/sample` 和 `data/secret` 逐点评测。
4. 按 `judge.type` 使用标准比较、Checker 或 Interactor。
5. 根据每个程序的结构化 `expected` 宿命验证状态、目标数据组与禁止状态；未配置时保持 accepted 全 AC、brute 不 WA 且至少 TLE/MLE、wrong 至少一个非 AC 的默认语义。

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
- Case：程序指纹、输入、答案、时限、内存和平台。

强制完整执行并用本次结果替换缓存：

```powershell
probhub judge L01 --no-cache
```

缓存事件包含 `mode`、`compile_hits/misses`、`validator_hits/misses`、`case_hits/misses`。

## 9. `typeset`

```powershell
probhub typeset [ID...]
```

无论选择几题，都会编译 `.probhub/workspace.yaml` 指定的完整 Typst 集合，以保证正式字母与物理页码正确；只把所选题目的页段提取到：

```text
<problem>/problem.pdf
```

它不会运行沙箱、构建 ZIP 或写 Manifest。

## 10. `package`

```powershell
probhub package [ID...]
probhub package L01 --allow-missing-pdf
```

执行：

1. 从 `probhub.yaml` 生成 DOMjudge `problem.yaml` 与 `domjudge-problem.ini`。
2. 构建根目录 `<ID>.zip`。
3. 立即验证路径、配置、样例、隐藏数据、配对关系与 PDF。

默认要求已有 `problem.pdf`。`--allow-missing-pdf` 只用于尚未排版的中间状态，不用于正式交付。

`package` 不自动执行 lint、judge 或 typeset。正式流程优先使用 `build`。

## 11. `build`

```powershell
probhub build [ID...] [--skip-judge] [--no-cache]
```

顺序：

1. lint 所选题目。
2. judge 所选题目。
3. 编译完整 Typst 集合。
4. 提取所选单题 PDF。
5. 构建并验证所选 ZIP。
6. 写入所选 `.probhub/build-manifest.json`。

即使执行 `build L01`，也会为了题序与页码编译全卷，但只评测、提取、打包和更新 L01。

选项：

```powershell
probhub build L01 --no-cache     # 完整沙箱并刷新缓存
probhub build L01 --skip-judge   # 跳过沙箱，仅用于已有可信评测的排版/打包迭代
```

不要把 `--skip-judge` 作为首次构建或正式正确性证明。

## 12. `verify-package`

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

## 13. 推荐流程

### 单题开发

```powershell
probhub lint L01
probhub judge L01
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
probhub build L01
```

### 正式交付

最后一次影响代码、数据、答案或限制的修改后：

```powershell
probhub build L01 --no-cache
probhub status L01
probhub verify-package L01.zip --require-pdf
```

## 14. 常见问题

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
