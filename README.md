# ProbHub

![ProbHub Logo](logo.svg)

**把一道算法竞赛题，从想法整理成可测试的题目、排版好的 PDF 和可上传 DOMjudge 的 ZIP。**

ProbHub 是一套在本机运行的 ACM/ICPC 出题工具。你可以使用 WebUI 编辑题面，也可以让 Codex、Claude Code 等 Agent 按固定流程协助出题。ProbHub 会把题面、程序、数据、测试、组卷和打包连接起来，减少重复操作和漏检。

[查看排版示例](https://github.com/greenthree/ProbHub-skill/blob/main/typst-template/%E6%AD%A3%E5%BC%8F%E8%B5%9B/main.pdf) · [npm](https://www.npmjs.com/package/probhub) · [GitHub Releases](https://github.com/greenthree/ProbHub-skill/releases)

## ProbHub 能做什么

一次典型的出题流程是：

1. 写下题目想法、已有题面或约束。
2. 完成题面、标准程序、暴力程序、错解、数据生成器和 Validator。
3. 在本机检查样例，运行所有测试数据，并用 stress 随机对拍。
4. 生成整场试卷和单题 PDF。
5. 生成并验证 DOMjudge 题目包。

ProbHub 会在这条流程中提供：

- 面向出题人的 WebUI，支持题面、样例、限制、封面和题序编辑；
- 面向 Agent 的 Skill，让 Agent 了解规范文件、验证顺序和交付标准；
- standard、custom checker、浮点比较和 interactive 四类常见评测场景；
- AC、WA、TLE、MLE、OLE、RE、FAIL 等结果和完整进程树清理；
- 可复现的数据生成、差分测试、反例重放和错解击杀矩阵；
- Typst 全卷排版、单题 PDF、DOMjudge ZIP 和交付前验包；
- Windows 与 Ubuntu 双平台 CI 验证。

## 快速开始

### 1. 安装 Skill

安装前请确认已安装 Node.js 18 或更高版本（包含 npm），以及 Python 3.10 或更高版本。安装 Skill 时会把 Flask、PyYAML 和 pypdf 等依赖安装到当前 `python` 指向的环境；下面的命令显式允许这次安装。

Windows PowerShell：

```powershell
npm install -g probhub
$env:PROBHUB_ALLOW_SYSTEM_PYTHON = "1"
probhub-skill
probhub doctor
```

Ubuntu/Linux：

```bash
npm install -g probhub
PROBHUB_ALLOW_SYSTEM_PYTHON=1 probhub-skill
probhub doctor
```

`probhub doctor` 会列出 Python、Node.js、npm、`g++`、Typst、字体和 Python 依赖的实际状态。先修复其中的错误，再继续创建题目。

`probhub-skill` 还会把 Agent Skill 安装到：

```text
~/.claude/skills/probhub
~/.agents/skills/probhub
```

只想临时安装 Skill 时可以运行 `npx probhub-skill`；只想安装到当前项目时使用 `npx probhub-skill --local`。

### 2. 调用 Agent

在准备存放比赛文件的目录中打开 Codex、Claude Code 或其他兼容 Agent，然后直接描述需求。

从零创建比赛：

```text
使用 probhub 技能，帮我创建一场算法竞赛，并先完成第一道题。
题目想法是：……
请完成题面、程序、数据、验证、组卷和 DOMjudge 题目包。
```

维护已有题目：

```text
使用 probhub 技能，继续完善 L01。
请检查题面、标准程序、Validator、暴力程序、典型错解和测试数据，
完成 judge、stress、seal 和最终构建，不要修改其他题目。
```

你可以提供题目想法、已有 Markdown、PDF、网页、代码或数据。Agent 会根据 [SKILL.md](SKILL.md) 调用同一套 ProbHub Core；你只需要审查题意、算法、数据强度和最终 PDF。

### 3. 选择验证模式

调用 Agent 时可以直接指定模式；未指定时使用普通模式。三种模式都会完成 lint、样例核验、无缓存 Judge 和交付门禁，发现高风险问题时会自动升级。

| 模式 | 适用情况 | 主要差异 |
|---|---|---|
| 快速 | 简单、确定性强、证明完整的题目 | 固定 seed 完成 100 轮 stress，不调用独立 Agent |
| 普通（默认） | 大多数题目 | 正式 stress，并由 1 个盲审 Agent 独立给出证明和 std |
| 完整 | 难题、特殊 Judge、精度或随机化问题，以及存在分歧的题目 | 普通模式基础上增加独立证明/参考实现审查和对抗审查 |

详细选择、升级和交接规则见 [Agent 验证模式](references/verification-modes.md)。

## 环境要求

ProbHub 已在 Windows 和 Ubuntu 上持续测试。macOS 通常可以运行，但目前不是发布 CI 的强制验证平台。

| 工具 | 要求 | 用途 |
|---|---:|---|
| [Python](https://www.python.org/downloads/) | 3.10 或更高 | 运行 ProbHub Core |
| [Node.js](https://nodejs.org/) | 18 或更高 | 安装 `probhub` 命令和 Agent Skill |
| `g++` | 支持 C++17 | 编译标程、Validator 和 Checker |
| [Typst](https://github.com/typst/typst/releases/tag/v0.14.2) | 0.14.2 | 生成 PDF |
| Noto Sans CJK SC | 固定中文字体 | 保证题面中文正常显示 |

<details>
<summary>g++、Typst 或中文字体安装提示</summary>

Windows 推荐使用 [MSYS2](https://www.msys2.org/) 安装 `g++`，并把其 UCRT64 `bin` 目录加入 `PATH`。Typst 请下载 `typst-x86_64-pc-windows-msvc.zip`，解压后把 `typst.exe` 所在目录加入 `PATH`。

Ubuntu 可以安装编译器：

```bash
sudo apt update
sudo apt install -y g++
```

Typst 请使用上方链接中的 0.14.2 固定版本。Noto Sans CJK SC 固定字体可从下列地址下载：

```text
https://raw.githubusercontent.com/notofonts/noto-cjk/165c01b46ea533872e002e0785ff17e44f6d97d8/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf
```

Windows 可以双击字体文件安装；Linux 可以把它放入 `~/.local/share/fonts/` 后执行 `fc-cache -f`。如果不想安装到系统，也可以把字体所在目录设置为 `TYPST_FONT_PATHS`。

</details>

## WebUI 与 CLI

### WebUI：适合第一次使用

进入包含 `.probhub/workspace.yaml` 的比赛目录后运行：

```bash
probhub ui
```

WebUI 的“编译”用于隔离预览；“分发”才会正式生成 PDF、ZIP 和构建记录。导航、翻页和普通预览不会替代正式验证。服务只监听本机 `127.0.0.1`。

不希望自动打开浏览器时使用：

```bash
probhub ui --no-browser
```

### CLI：需要手动控制流程时使用

Agent 和 WebUI 都会调用同一套 Core。只有需要手动排查或编排流程时，才需要直接使用 CLI：

| 命令 | 用途 |
|---|---|
| `probhub doctor` | 检查安装环境 |
| `probhub ui` | 启动 WebUI |
| `probhub lint L01` | 检查目录、配置和题面结构 |
| `probhub judge L01` | 编译并运行 Validator、标程、暴力和错解 |
| `probhub stress L01 --rounds 1000 --seed 12345` | 用随机小数据对拍 |
| `probhub seal L01 --no-cache` | 验证并冻结当前题目版本 |
| `probhub build L01 --no-cache` | 正式生成 PDF、ZIP 和 Manifest |
| `probhub status L01` | 检查产物是否过期 |

完整参数见 [CLI 手册](references/cli.md)。

## 一道题由哪些文件组成

创建 `L01` 后，主要目录如下：

```text
L01/
├── probhub.yaml
├── problem.md
├── code/
│   ├── std.cpp
│   ├── validator.cpp
│   ├── brute.cpp
│   └── wrong.cpp
└── data/
    ├── sample/
    └── secret/
```

初学时只需要知道下面这些事实来源：

| 位置 | 内容 |
|---|---|
| `.probhub/workspace.yaml` | 比赛名称、题目顺序和组卷设置 |
| `L01/problem.md` | 题目描述、输入、输出和提示 |
| `L01/probhub.yaml` | 时间限制、评测方式、代码和数据配置 |
| `L01/code/` | 标程、暴力、错解、Validator、Checker 等源码 |
| `L01/data/sample/` | 题面展示的样例 |
| `L01/data/secret/` | 选手不可见的正式测试数据 |

以下文件由 Core 自动生成，不要手工修改：

- `meta.json` 和 Typst `problems.json`；
- `problem.yaml` 和 `domjudge-problem.ini`；
- `problem.pdf`、整场 PDF 和 `<ID>.zip`；
- `.probhub/build-manifest.json`。

如果生成物过期，应重新运行 `seal` / `build`，而不是直接改 PDF、ZIP 或 Manifest。

## 完成标准

Agent 完成题目后，应明确报告下列结果：

- 命令退出码为 0；
- Judge 最终结果为 `all_expectations_met`；
- `status` 为 `current`；
- ZIP 深度验证没有错误；
- 人工检查过单题 PDF 和整场 PDF。

最终交付文件通常是整场 `main.pdf`、每题的 `problem.pdf` 和工作区根目录下的 `<ID>.zip`。如果题目、数据或模板发生变化，应由 Agent 重新验证和构建，不要手工修改生成物。

本机通过不等于目标 DOMjudge 机器一定具有相同速度。时间限制和内存限制仍应在目标 Linux/DOMjudge 环境校准。

## 并行出题时怎么做

多名出题人或多个 Agent 可以各自只修改自己的题目目录：

1. 开始前一次性登记所有题目 ID 和正式顺序。
2. 开发中使用 `probhub checkpoint <ID>` 发布不可变草稿。
3. 单题完成后使用 `probhub seal <ID> --no-cache`。
4. `seal` 会立即生成一份完整试卷预览；其他未完成题目使用最近的 checkpoint 或占位页。
5. 全部题目 seal 后，只执行一次多题正式构建：

```bash
probhub build L10 L11 L12 --no-cache
```

每个题目任务不需要等待其他题目完成才能获得自己的完整试卷预览。正式构建仍会一次生成整场产物，避免多个任务互相覆盖。详细机制见 [generation 与并行组卷说明](references/generations.md)。

## 常见评测结果

| 结果 | 含义 | 通常先检查什么 |
|---|---|---|
| `AC` | 答案正确 | 无需处理 |
| `WA` | 答案错误 | 算法、答案文件或 Checker |
| `TLE` | 运行超时 | 算法复杂度和时间限制 |
| `MLE` | 内存超限 | 内存使用和内存限制 |
| `OLE` | 输出过多 | 无限输出、调试日志或输出限制 |
| `RE` | 运行时错误 | 越界、崩溃或进程数超限 |
| `FAIL` | 题目基础设施错误 | Validator、Checker、Interactor 或编译环境 |

`FAIL` 不能当成“错解被成功卡掉”。它表示题目自身的评测设施需要修复。

## 常见问题

### 找不到 `probhub` 命令

先确认：

```bash
node --version
npm --version
npm install -g probhub
```

如果仍找不到，检查 npm 的全局可执行目录是否在 `PATH` 中。也可以临时使用：

```bash
npx probhub --version
```

### `probhub doctor` 报错

按输出逐项处理。最常见的是：

- 当前终端调用了另一套 Python，或当前 Python 没有完成依赖安装；
- `g++` 或 Typst 不在 `PATH`；
- Typst 不是 0.14.2；
- Typst 找不到 `Noto Sans CJK SC`；
- Python 依赖没有安装，可重新运行 `probhub-skill`。

### WebUI 打不开

请确认当前目录或上级目录中存在 `.probhub/workspace.yaml`，然后运行：

```bash
probhub --json ui --check
probhub ui --no-browser
```

第二条命令不会自动打开浏览器。请手动访问 <http://127.0.0.1:33933/>。

### `build` 提示需要 sealed revision

正式构建要求整场所有题目都有与当前文件一致的 seal。先逐题执行：

```bash
probhub seal L01 --no-cache --seed 12345
```

所有题目完成后再运行多题 `build`。

### `status` 显示 `stale`

这表示题目、数据、题序、模板或正式产物在上次构建后发生了变化。重新 `seal` 并 `build`，不要手工修改 Manifest。

### 出现 `recovery_required`

上一次正式写入可能被断电或强制结束。重新运行原来的 `build`、`gen --apply` 或 `stress --fixate`，让 ProbHub 使用事务记录恢复。不要手工删除 `.probhub` 中的恢复材料。

## 安全边界

ProbHub 面向本地、单用户、可信的出题环境。它会限制时间、内存、输出和进程树，但这不是强安全容器。不要用它运行来源不明或有意攻击主机的代码；这类任务应放在专门的虚拟机或容器隔离环境中。

## 进一步阅读

- [CLI 完整手册](references/cli.md)
- [Workspace Schema v1](references/workspace-schema-v1.md)
- [数据组与解法期望](references/data-groups-expectations.md)
- [错解分类与数据强度](references/mistake-taxonomy.md)
- [Stress 差分测试](references/stress.md)
- [Agent 快速、普通与完整验证模式](references/verification-modes.md)
- [Checker 与交互题](references/checker-interactor.md)
- [进程和资源控制](references/process-control.md)
- [Checkpoint、Seal 与 Generation](references/generations.md)
- [C++ 数据生成模板](references/fast.md)
- [CYaRon 数据生成](references/cyaron.md)
- [旧工作区兼容说明](references/legacy-workflow.md)

## 参与开发

从源码运行检查：

```bash
npm ci
npm run check
npm run pack:check
```

`npm run check` 会检查 Python、Node.js、WebUI 静态资源并运行测试；`npm run pack:check` 会验证两个 npm 包的内容。可复用的 standard、custom、float、interactive 和 stress 测试工作区位于 `tests/fixtures/`。

提交问题或建议请使用 [GitHub Issues](https://github.com/greenthree/ProbHub-skill/issues)。版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## 鸣谢

- [CYaRon](https://github.com/luogu-dev/cyaron)：测试数据生成工具。
- [olymp-in-typst](https://github.com/lihaoze123/olymp-in-typst)：算法竞赛 Typst 排版模板。
- [testlib](https://github.com/MikeMirzayanov/testlib)：算法竞赛评测辅助库。

## License

MIT
