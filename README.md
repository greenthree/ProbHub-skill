# ProbHub Skill

![ProbHub Logo](logo.svg)

ProbHub Skill 是一个面向 ACM/ICPC、XCPC 和 DOMjudge 的自动化出题工作流。它把题面整理、数据生成、标准程序与错解验证、Typst 组卷、单题 PDF 裁剪、WebUI 微调和 DOMjudge 打包串成一条可由 Agent 执行的流程，尽量减少出题过程中重复、易错、但又必须严谨完成的杂活。

> 适合场景：从一个题目 idea 或现有题面出发，快速生成一份可自检、可排版、可上传 DOMjudge 的题目目录。

---

## 核心能力

- **Agent 驱动出题**：内置 [SKILL.md](SKILL.md)，让 Claude Code、Codex 或兼容 Agent 按固定流程完成出题任务。
- **严谨数据闭环**：自动组织 `std.cpp`、`validator.cpp`、`brute.cpp`、`wrong.cpp`，支持数据逻辑分组、结构化宿命和首个击杀用例，避免用偶然 RE/TLE 误判错解已被正确卡掉。
- **可复现差分测试**：`probhub stress` 按 seed 反复生成小数据，对拍 accepted 与 brute，并保存首个可 replay 的反例。
- **跨平台资源控制**：共享 `probhub/process_control.py` 为普通程序、Checker、Validator、编译器、Interactor 和 stress 提供完整进程树清理、时间/内存/输出/进程数限制，并报告 TLE/MLE/OLE/RE/WA/AC。
- **Typst 高速排版**：使用 Typst 模板生成全卷 PDF，并能按题目自动裁剪出独立 `problem.pdf`。
- **WebUI 微调题面**：提供 Flask 控制台，支持题目排序、Markdown 预览、样例编辑、引言 Quote、时空限制编辑、全卷编译和 PDF 分发。
- **可重复构建 Core**：Workspace Schema v1、源文件哈希、构建 Manifest、过期检测和统一 `probhub` CLI。
- **DOMjudge 兼容**：从规范源文件生成 `problem.yaml`、`domjudge-problem.ini` 和确定性 `.zip`。

示例 PDF：
[真实赛事 Typst 题面排版示例](https://github.com/greenthree/ProbHub-skill/blob/main/typst-template/%E6%AD%A3%E5%BC%8F%E8%B5%9B/main.pdf)

---

## 快速安装

只需注入 Skill 时可使用 npm 脚手架：

```bash
npx probhub-skill
```

若需要持久的 `probhub` CLI，推荐全局安装后再执行 Skill 注入：

```bash
npm install -g probhub-skill
probhub-skill
probhub --version
```

安装脚本会把 skill 注入到两个常见 Agent 目录：

```text
~/.claude/skills/probhub
~/.agents/skills/probhub
```

如果希望只安装到当前项目目录，可以使用：

```bash
npx probhub-skill --local
```

这会写入：

```text
./.claude/skills/probhub
./.agents/skills/probhub
```

也可以通过社区 Skills 框架安装，但目前更推荐本项目脚手架，因为它会复制完整脚本和参考文件：

```bash
npx skills add greenthree/ProbHub-skill
```

---

## 环境依赖

ProbHub 会尽量自动安装 Python 依赖，但底层编译和排版仍依赖本机环境。

### 基础工具

- Node.js / npm
- Python 3.8+
- GCC/G++，用于编译 C++ 标程、验证器、checker 和 interactor
- Typst 编译器

### Python 包

```bash
pip install -r requirements.txt
```

需要使用 CYaRon 数据生成器时再额外安装：

```bash
pip install cyaron
```

### Typst

macOS：

```bash
brew install typst
```

Windows：

```powershell
winget install typst
```

也可以从 [Typst Releases](https://github.com/typst/typst/releases) 下载可执行文件。

### 字体

模板默认使用下列字体。缺少字体时，Typst 可能仍能编译，但排版效果会偏离示例。

- `New Computer Modern Math`
- `New Computer Modern Mono`
- `FZKai-Z03`
- `STZhongSong`
- `Microsoft YaHei`
- `SimSun` / `simsun`

---

## ProbHub CLI 与 Workspace Schema v1

`0.3.0` 开始，推荐让 Skill 编排统一 CLI，而不是手工串联脚本：

```powershell
probhub doctor
probhub new L05 --name "新题"
probhub lint L01
probhub status
probhub judge L01
probhub judge L01 --no-cache  # 强制完整重跑并刷新缓存
probhub stress L01 --rounds 10000 --seed 12345
probhub typeset L01
probhub package L01
probhub build L01
probhub build             # 构建全工作区
probhub verify-package L01.zip --require-pdf
```

如果 npm bin 不在 PATH，可使用 Skill/工作区中的兼容入口：

```powershell
python scripts/probhub.py build L01
```

Schema v1 使用以下事实来源：

```text
.probhub/workspace.yaml    # 赛事配置、题目顺序、Typst 集合
L01/probhub.yaml           # 稳定 ID、限制、代码矩阵和数据目录
L01/problem.md             # 题面描述、输入、输出和提示
L01/code/                   # 全部 C++ 源码与本地编译产物
L01/data/sample/           # 样例唯一来源
L01/data/secret/           # 隐藏数据唯一来源
```

以下均为构建产物，不应手工维护：

- `meta.json`
- Typst `problems.json`
- `problem.yaml` 与 `domjudge-problem.ini`
- `problem.pdf`、全卷 PDF 和 DOMjudge ZIP
- `.probhub/build-manifest.json`

本地诊断目录 `.probhub/stress/` 保存差分反例，不属于正式构建产物，也不应提交。

`probhub build` 会依次执行 lint、沙箱、元数据生成、Typst 编译、单题 PDF 提取、DOMjudge 打包、包验证和 Manifest 写入。`probhub status` 会比较源文件、数据、PDF 和 ZIP 哈希，报告 `current`、`stale` 或 `never-built`。

完整命令语法、单题/多题操作、缓存语义与故障处理见 [`references/cli.md`](references/cli.md)。无 `.probhub/workspace.yaml` 的旧工作区流程已独立整理到 [`references/legacy-workflow.md`](references/legacy-workflow.md)，Schema v1 工作区不要混用。

---

## 基本用法

在一个题目工作区中启动你的 Agent 工具，例如 Claude Code：

```bash
claude
```

然后告诉它：

```text
使用 probhub 技能，我要出一道新题。
```

你可以提供：

- 一个题目 idea
- 现有 Markdown 题面
- PDF 题面
- 网页 URL
- 已有代码或数据约束

Agent 会按 [SKILL.md](SKILL.md) 中的流程推进：

1. 确立题面、中文题名和英文目录名。
2. 判断是否需要 checker、interactor 或特殊评测。
3. 在 `code/` 中编写 `std.cpp`、`validator.cpp`、`brute.cpp`、`wrong.cpp` 和数据生成器。
4. 生成 `data/sample` 与 `data/secret`。
5. 运行本地沙箱自检。
6. 加入 Typst 组卷并裁剪 `problem.pdf`。
7. 按需生成 DOMjudge 题目包。

---

## 本地沙箱自检

对题目目录执行：

```bash
python scripts/local_judge.py <problem_dir> --jsonl
```

自动化流程以退出码和最后一个 JSONL 事件为准。成功事件固定为：

```json
{"protocol":"probhub.local_judge","protocol_version":1,"type":"final","ok":true,"status":"passed","code":"all_expectations_met","exit_code":0,"message":"..."}
```

不带 `--jsonl` 时仍会输出适合人工阅读的文本，但调用方不应匹配自然语言成功提示。

`local_judge.py` 会检查：

- `probhub.yaml` 中 `judge.validator` 指向的验证器是否接受所有输入数据。
- `solutions.accepted` 指向的程序是否全部 AC。
- `solutions.brute` 指向的程序是否不 WA，并且至少出现 TLE 或 MLE，用于证明强数据足够强。
- `solutions.wrong` 指向的程序是否不能全 AC。
- `judge.type: standard` 的普通题按行比较，允许每行末尾多余的空格或 Tab；行内空格和内部换行仍严格检查。
- `judge.type: custom` 时，使用 `code/checker.cpp` 按 DOMjudge/testlib 协议判定输出。
- `judge.type: interactive` 时，将选手程序与 `code/interactor.cpp` 双向连接，执行总时间/空闲超时，并记录有大小上限的双向 Transcript。
- 每个测试点的时间、内存、输出和进程数状态，以及 Checker/Interactor 判定信息；选手输出超过限制时报告 `OLE`。
- 普通程序、Checker、Validator、编译器、Interactor 与 stress 统一使用完整进程树控制；即使父进程正常退出，残留后代也会被清理。
- `data.groups` 与 `solutions.*[].expected` 定义的数据组击杀矩阵、目标/禁止状态和首个相关用例；详见 `references/data-groups-expectations.md`。

### 沙箱增量缓存

沙箱会把本地缓存写入 `<problem>/.probhub/sandbox-cache-v1.json`：

- C++ 编译缓存按源码、相关头文件、编译参数、编译器和平台指纹失效。
- Validator 结果按验证器指纹和输入文件内容失效。
- 程序逐点结果按程序指纹、输入、答案、时限、内存、输出上限、进程数上限、平台和沙箱策略失效。
- 修改单个数据点时，只重新验证和运行受影响的测试点；没有相关改动时，重复沙箱通常只需读取缓存。

缓存文件是本地产物，不应提交。需要排查非确定性程序或强制完整重跑并刷新缓存时使用：

```powershell
probhub judge L01 --no-cache
probhub build L01 --no-cache
```

JSONL 的 `compile`、`validator`、`case` 事件包含 `cached` 字段，结束前会输出 `type=cache` 的命中统计。WebUI 使用同一协议展示缓存状态。资源控制语义变化时缓存 Schema 会升级，旧结果不会绕过新的 OLE、进程树或进程数规则。

### 进程与资源控制

题目级资源限制写在 `probhub.yaml`：

```yaml
limits:
  time: 1       # 秒
  memory: 256   # MiB
  output: 64    # MiB，默认 64
  processes: 32 # 整棵进程树，默认 32
```

- `limits.output` 限制一次受控运行写出的 stdout 与 stderr 总量；超过后立即终止完整进程树、截断已保存的诊断文件，并把选手程序判为 `OLE`。Checker、Validator、Generator、编译器或 Interactor 自身超限属于题目基础设施失败。
- `limits.processes` 限制整棵进程树。普通父进程即使先正常退出，ProbHub 仍会清理遗留后代。
- Windows 使用 Job Object 约束进程树、总内存和活动进程数；无法建立 Job 时 fail closed，不会降级为无保护执行。
- Linux/Unix 使用独立进程组、`RLIMIT_AS` 和低频 `/proc` 进程树监控；超时、超限、异常和正常退出路径都会清理后代。
- 编译器、Validator、Checker 和 stress 工具使用独立的内部诊断上限，但共享相同的进程树控制。

完整状态语义、平台差异、截断行为和缓存规则见 [`references/process-control.md`](references/process-control.md)。

### 差分测试（stress）

在题目的 `probhub.yaml` 中配置小数据生成器：

```yaml
stress:
  generator: code/stress_generator.cpp
  args: ["{seed}", "{round}"]
  rounds: 1000
  time_limit: 5
  tool_timeout: 5
  # accepted/brute 可省略，默认取 solutions 中的第一项
  accepted: code/std.cpp
  brute: code/brute.cpp
```

生成器每轮把一个完整测试点写到 stdout；stderr 只用于日志。第 `round` 轮 seed 为 `master_seed + round - 1`，其中 round 从 1 开始。常用命令：

```powershell
probhub stress L01 --rounds 10000 --seed 12345
probhub stress L01 --replay latest
```

- `standard` 题沿用普通沙箱的逐行比较规则，允许整个输出首尾空白和每行行尾空格/Tab。
- `custom` 题把 accepted 输出作为 jury answer、brute 输出作为 contestant output，交给 `judge.checker` 判定。
- 首个不匹配、程序异常或工具失败会停止测试，并写入 `<problem>/.probhub/stress/`；结果包含可直接执行的 `replay_command`。Generator、Validator、accepted、brute、Checker 和编译器都使用同一进程树控制，输出超限会被截断并记录为 `OLE` 或基础设施失败。
- `interactive` 暂不支持 stress。
- 全部轮次或 replay 通过时退出码为 `0`；反例、基础设施失败、配置或编译错误为 `1`；Ctrl+C 为 `130`。

完整 Schema、Generator 协议、反例目录、失败分类与 replay 语义见 [`references/stress.md`](references/stress.md)。

资源限制读取优先级：

1. `<problem_dir>/probhub.yaml` 中的 `limits.time`、`limits.memory`、`limits.output` 和 `limits.processes`
2. Legacy 工作区的 `meta.json`、`domjudge-problem.ini` 与 `problem.yaml` 提供时间和内存；输出与进程数仍使用默认值
3. 默认 `1s / 256MiB / 64MiB output / 32 processes`

---

## Typst 组卷

第一次组卷时，ProbHub 会创建类似结构：

```text
typst-statement/
├── lib.typ
├── problems-sample.json
├── usts.png
└── <subtitle>/
    ├── main.typ
    ├── problems.typ
    └── problems.json
```

单题元数据写入 `<problem_dir>/meta.json`，再通过脚本合并到对应卷的 `problems.json`：

```bash
python scripts/add_problem.py typst-statement/<subtitle>/problems.json <problem_dir>/meta.json
```

编译并裁剪单题 PDF：

```bash
python scripts/extract_new_problem.py typst-statement/<subtitle> <problem_dir>
```

裁剪策略：

- 新增最后一题：使用页数差值提取新增页面。
- 修改旧题：扫描 PDF 中的 `题目 X. 题名` 标题定位页面范围。

---

## WebUI 控制台

Agent 完成底层排版后，会把 `ui.py` 和 `launch_ui.py` 放到工作区根目录。推荐用户自己前台启动，方便随时退出：

```powershell
python ui.py
```

浏览器会打开：

```text
http://127.0.0.1:33933
```

需要后台运行时，可以手动执行：

```powershell
python launch_ui.py
```

控制台支持：

- 拖拽排序题目。
- 编辑题面、输入输出格式、提示和样例。
- 编辑难度、标签、时间限制、内存限制。
- 添加或移除题面引言 Quote。
- 编译全卷 PDF。
- 将每题 `problem.pdf` 分发回题目目录，并同步注入已有 zip。
- 运行本地沙箱并展示逐点结果。

当前 WebUI **尚未实现**上传代码并评测。共享进程控制层和隔离的临时输出机制为后续功能提供底层基础；未来提交代码应只在临时 submission workspace 中编译和评测，不得覆盖题目原有 `code/` 文件。

---

## DOMjudge 打包约定

每道题建议保持如下结构：

```text
<problem_dir>/
├── probhub.yaml             # 规范配置与代码调用路径
├── problem.md               # 规范题面源文件
├── code/
│   ├── std.cpp
│   ├── validator.cpp
│   ├── brute.cpp
│   ├── wrong.cpp
│   ├── inmaker.cpp
│   ├── checker.cpp          # 可选，非唯一答案时需要
│   ├── interactor.cpp       # 可选，交互题需要
│   └── *.exe                # 本地编译产物，不跟踪
├── meta.json                # Core 生成
├── problem.pdf              # Core 生成
├── domjudge-problem.ini     # Core 生成
├── problem.yaml             # Core 生成
├── data/
│   ├── sample/
│   │   ├── 1.in
│   │   └── 1.ans
│   └── secret/
│       ├── 2.in
│       └── 2.ans
└── output_validators/        # 自定义 checker/interactor 时使用
```

打包时应包含：

- `data/`
- `domjudge-problem.ini`
- `problem.yaml`
- `problem.pdf`，如果已生成
- `output_validators/`，如果存在自定义评测；Schema v1 下由 Core 从 `code/checker.cpp` 或 `code/interactor.cpp` 自动生成

推荐使用确定性打包和验证脚本：

```bash
python scripts/package_problem.py <problem_dir> <problem_id>.zip --require-pdf
python scripts/verify_package.py <problem_id>.zip --require-pdf
```

打包脚本会按稳定顺序和固定时间戳写入 ZIP；验证脚本会检查路径安全、根配置文件、样例/隐藏数据及 `.in`/`.ans` 配对关系。

---

## 仓库结构

```text
ProbHub-skill/
├── SKILL.md                  # Agent 工作流指令
├── README.md                 # 项目说明
├── bin/
│   └── init.js               # npm 脚手架入口
├── references/
│   ├── cli.md                # Workspace Schema v1 完整 CLI 手册
│   ├── checker-interactor.md # Checker 与交互题协议和模板
│   ├── stress.md             # 差分测试 Schema、协议、反例与 replay
│   ├── process-control.md    # 进程树、资源限制、OLE 与平台语义
│   ├── legacy-workflow.md    # 无 Schema v1 时按需加载的旧工作流
│   ├── cyaron.md             # CYaRon 快速参考
│   ├── fast.md               # 简单 C++ 数据生成模板
│   ├── testlib.h             # testlib 头文件
│   ├── lib.typ               # Typst 宏与样式
│   ├── main.typ              # Typst 主入口模板
│   ├── problems.typ          # Typst 题目列表入口
│   └── problems-sample.json  # 单题元数据样例
├── probhub/
│   └── process_control.py     # 跨平台共享进程与资源控制
├── scripts/
│   ├── add_problem.py
│   ├── extract_new_problem.py
│   ├── launch_ui.py
│   ├── local_judge.py
│   └── ui.py
└── typst-template/           # 示例组卷工程与 PDF
```

---

## 常见问题

### `npx probhub-skill` 后没有找到技能

安装脚本会写入以下目录：

```text
~/.claude/skills/probhub
~/.agents/skills/probhub
```

如果你的 Agent 工具使用了不同的技能目录，可以手动把 `SKILL.md`、`references/` 和 `scripts/` 复制到对应位置。

### Typst 编译失败

常见原因：

- Typst 未安装或不在 PATH 中。
- 缺少模板使用的字体。
- `problems.json` 中 Markdown、LaTeX 或图片路径格式错误。
- 图片路径、题名或 `meta.json` 中的 `display_name` 与实际文件不一致。

可以先确认 Typst 是否可用：

```bash
typst --version
```

### WebUI 打不开

确认 Flask 已安装：

```bash
pip install -r requirements.txt
```

然后在包含 `typst-statement/` 的工作区根目录运行：

```bash
python ui.py
```

浏览器打开 `http://127.0.0.1:33933` 后即可使用控制台。

---
## 鸣谢

ProbHub Skill 基于以下优秀项目构建：

- [CYaRon](https://github.com/luogu-dev/cyaron)：洛谷团队开源的 Python 测试数据生成库。
- [olymp-in-typst](https://github.com/lihaoze123/olymp-in-typst)：基于 Typst 的算法竞赛题面排版模板。
- [testlib](https://github.com/MikeMirzayanov/testlib)：算法竞赛评测辅助库。

---

## License

MIT
