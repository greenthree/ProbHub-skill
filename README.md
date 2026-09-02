<div align="center">

<a href="https://github.com/greenthree/ProbHub-skill">
  <img src="logo.svg" alt="ProbHub" width="230">
</a>

<p><strong>把算法竞赛题，从想法带到可验证交付。</strong></p>
<p><a href="README.md">中文</a> / <a href="README_EN.md">English</a></p>
<p>
  <a href="https://github.com/greenthree/ProbHub-skill">GitHub</a> ·
  <a href="https://github.com/greenthree/ProbHub-skill/releases">版本与下载</a> ·
  <a href="https://www.npmjs.com/package/probhub">npm</a> ·
  <a href="https://github.com/greenthree/ProbHub-skill/issues">问题反馈</a> ·
  <a href="CHANGELOG.md">变更记录</a>
</p>

[![Release](https://img.shields.io/github/v/release/greenthree/ProbHub-skill?style=flat-square&label=release)](https://github.com/greenthree/ProbHub-skill/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/greenthree/ProbHub-skill/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/greenthree/ProbHub-skill/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/probhub?style=flat-square&label=npm)](https://www.npmjs.com/package/probhub)
[![Node.js](https://img.shields.io/badge/Node.js-18%2B-339933?style=flat-square&logo=node.js&logoColor=white)](https://nodejs.org/)
[![License](https://img.shields.io/badge/license-MIT-2f80ed?style=flat-square)](LICENSE)

</div>

## ProbHub 是什么

ProbHub 是一套面向 ACM/ICPC 出题人的本地工作流。它把题面、代码、数据、评测、排版和 DOMjudge 打包连接起来；你可以自己使用，也可以让 Codex、Claude Code 等 Agent 按同一套规则协作。

它适合从零设题、批量维护题面与数据，或希望让 Agent 参与出题而仍保留可审查、可复现交付结果的队伍。ProbHub 面向本地单用户、可信环境，会控制时间、内存、输出和进程树，但不是运行敌意代码的安全容器。

## 一眼看懂

| 你想完成的事 | ProbHub 提供的帮助 |
| --- | --- |
| 整理一道题 | Schema v1 工作区、题面模板、配置和源码目录 |
| 确认题目真的能判 | Validator、标程、暴力、错解、样例和无缓存 Judge |
| 找到隐藏反例 | stress 差分测试、反例重放、数据组职责和 std 变异测试 |
| 检查特殊 Judge | Checker/Interactor fixture、鲁棒性探针和隔离 Judge QA |
| 组卷与发题 | Typst 全卷与单题 PDF、DOMjudge ZIP、深度验包和 Manifest |
| 让 Agent 参与 | 全局 Skill、快速/普通/完整验证模式和交接标准 |

~~~mermaid
flowchart LR
    Idea["题目想法"] --> Source["题面 + 配置 + 代码 + 数据"]
    Source --> Verify["lint / Judge / stress / Judge QA"]
    Verify --> Freeze["seal：冻结可复现版本"]
    Freeze --> Preview["完整试卷预览"]
    Freeze --> Build["正式 build"]
    Build --> Delivery["PDF + DOMjudge ZIP + Manifest"]
~~~

## 快速开始

### 1. 安装 Skill

先确认 Node.js 18+（含 npm），以及 Windows/Linux x86_64 上的 CPython 3.10、3.11 或 3.12。Ubuntu 的系统 Python 还需要 python3-pip。ProbHub 不要求创建虚拟环境，也不推荐把依赖写入系统 Python 的全局目录。

Windows PowerShell：

~~~powershell
npm install -g probhub
$env:PROBHUB_ALLOW_SYSTEM_PYTHON = "1"
probhub-skill
probhub doctor
~~~

Ubuntu/Linux：

~~~bash
npm install -g probhub
PROBHUB_ALLOW_SYSTEM_PYTHON=1 probhub-skill
probhub doctor
~~~

PROBHUB_ALLOW_SYSTEM_PYTHON=1 只授权本次安装把固定版本 Python 依赖写入当前解释器的用户依赖目录；它不会覆盖 Ubuntu 的全局包，也不会关闭资源限制。PowerShell 设置只对当前终端会话生效。

Skill 安装目录：

~~~text
~/.claude/skills/probhub
~/.agents/skills/probhub
~~~

再次运行安装器时，这两个 Skill 目录会作为完整目录整体替换，而不是增量合并；目录中的本地手工修改不会保留。

### 2. 调用 Agent

在比赛目录中打开 Codex、Claude Code 或其他兼容 Agent，直接描述目标：

~~~text
使用 probhub 技能，帮我创建一场算法竞赛，并先完成第一道题。
题目想法是：……
请完成题面、程序、数据、验证、组卷和 DOMjudge 题目包。
~~~

维护已有题目：

~~~text
使用 probhub 技能，继续完善 L01。
请检查题面、标准程序、Validator、暴力程序、典型错解和测试数据，
完成 judge、stress、seal 和最终构建，不要修改其他题目。
~~~

Agent 会读取 Schema v1 规范源并调用同一套 Core；你主要需要审查题意、算法、数据强度和最终 PDF。

## 选择验证模式

未指定时使用普通模式。所有模式都会完成基础 lint、样例核验、无缓存 Judge 和交付门禁；遇到高风险信号时只能升级。

| 模式 | 适用情况 | 会做什么 |
| --- | --- | --- |
| **快速** | 简单、确定性强、证明完整、Judge 风险低 | 固定 seed 做 100 轮 stress，不调用独立 Agent |
| **普通（默认）** | 大多数题目 | 正式 stress，并由 1 个只看到冻结题面的盲审 Agent 独立给出证明和 std |
| **完整** | 难题、随机化/启发式、浮点、复杂 Checker/Interactor、资源紧张或有分歧 | 增加独立证明/参考实现和对抗审查；适用的 standard+C++ 题目建议一次有界 mutation |

验证模式是 Agent 工作契约，不是 CLI 参数；它不替代算法证明，也不把本机测试包装成目标 Linux/DOMjudge 的性能承诺。详见[验证模式说明](references/verification-modes.md)。

## 从题意到交付

1. **整理工作区**：使用 Workspace Schema v1，固定赛事信息和题目 ID 顺序。
2. **维护规范源**：题面写在 problem.md，限制和 Judge 写在 probhub.yaml，代码与数据放在题目目录内。
3. **验证题目**：运行 lint、样例核验、Judge；有配置时运行 stress、Judge QA 和 mutation。
4. **冻结版本**：seal 固定当前 revision，并生成隔离的完整试卷预览；并行出题不必等待其他题。
5. **正式发布**：所有题目 sealed 后，只运行一次多题 build，得到正式 PDF、ZIP 和 Manifest。

并行任务只修改自己的题目目录，使用 checkpoint 发布不可变草稿；其他题的 live 文件不会被预览读取。详见[Checkpoint、Seal 与 Generation](references/generations.md)。

## WebUI

进入包含 .probhub/workspace.yaml 的比赛目录后运行：

~~~bash
probhub ui
~~~

不自动打开浏览器：

~~~bash
probhub ui --no-browser
~~~

默认地址：<http://127.0.0.1:33933/>。安装检查：

~~~bash
probhub --json ui --check
~~~

WebUI 支持题面、样例、限制、封面和题序编辑，以及实时预览、隔离编译、临时上传代码评测和任务取消。编译用于隔离预览；分发才正式生成 PDF、ZIP 和构建记录。请求和任务队列有明确上限，繁忙时会返回可重试提示；服务只监听本机回环地址。

### 可选：集成 DeepSeek Harness

如果你使用 DeepSeek Harness，可以通过独立的下游扩展把 ProbHub 的题目工作台以及验证、交付工具挂载到兼容的 DSH Web profile：

~~~bash
dsh plugin --profile web add @greenthree/dsh-probhub@0.1.1-rc.2
dsh --profile web
~~~

这是下游扩展，不是官方 DSH 内置功能。完整题目工作台需要匹配的 DSH Web 客户端；仅使用官方上游 DSH 时，Host 和后台工具可以加载，但前端工作台可能不可用。详见 [`@greenthree/dsh-probhub`](https://www.npmjs.com/package/@greenthree/dsh-probhub) 的安装说明和兼容性信息。

## CLI：需要手动控制时使用

Agent 和 WebUI 都调用同一套 Core。常用命令：

命令默认输出结构化 JSON；需要快速人工查看时，在子命令前使用 `--format text`。脚本集成继续使用 `--json`，两者不会改变命令的退出码或验证深度。

| 命令 | 用途 |
| --- | --- |
| <code>probhub doctor</code> | 检查 Python、Node.js、npm、g++、Typst、字体和依赖 |
| <code>probhub init</code> | 初始化 Schema v1 工作区 |
| <code>probhub new L01</code> | 创建可编译、可评测的题目骨架 |
| <code>probhub lint L01</code> | 检查目录、配置、题面、数据和约束 |
| <code>probhub judge L01 --no-cache</code> | 编译并运行 Validator、标程、暴力和错解 |
| <code>probhub stress L01 --rounds 1000 --seed 12345</code> | 用随机小数据做差分测试 |
| <code>probhub judge-qa L01 --no-cache</code> | 主动测试 Checker/Interactor fixture |
| <code>probhub seal L01 --no-cache</code> | 验证并冻结当前题目版本 |
| <code>probhub build L01 --no-cache</code> | 正式生成 PDF、ZIP 和 Manifest |
| <code>probhub status L01</code> | 检查规范源和正式产物是否一致 |

命令只接受 workspace.yaml 中的稳定题目 ID（如 L01），不要使用显示字母。完整参数见[CLI 手册](references/cli.md)。

## 工作区里有哪些文件

~~~text
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
~~~

| 路径 | 内容 |
| --- | --- |
| .probhub/workspace.yaml | 比赛名称、题目顺序和组卷设置 |
| L01/problem.md | 题目描述、输入、输出和提示 |
| L01/probhub.yaml | 时间限制、评测方式、代码和数据配置 |
| L01/code/ | 标程、暴力、错解、Validator、Checker 等源码 |
| L01/data/sample/ | 题面展示的样例 |
| L01/data/secret/ | 选手不可见的正式测试数据 |

Core 生成物不要手工修改：meta.json、Typst problems.json、problem.yaml、domjudge-problem.ini、problem.pdf、整场 PDF、<ID>.zip 和 .probhub/build-manifest.json。过期时重新运行 seal / build。

## 题目验证重点

- validator.cpp 严格检查格式、范围和结构；多组数据题使用足够宽的类型累计总量并实际拒绝超过题面上限的输入。
- 标程、暴力和错解按数据组职责登记；killed 只表示当前已知错解被击杀，不表示不存在未知错解。
- custom 或 interactive 题目登记 judge.qa fixture，并在交付前让 judge-qa 返回 passed、evidence 为 current。
- mutation 是标准题 C++ 的补充探测；survived、人工排除和 mutation 分数都不是正确性证明。
- 固定 seed 只用于复现；本机资源测量不能替代目标 Linux/DOMjudge 校准。

详见[数据组与解法期望](references/data-groups-expectations.md)、[错解分类](references/mistake-taxonomy.md)、[Checker 与交互题](references/checker-interactor.md)和[std 变异测试](references/mutation-testing.md)。

## 正式交付标准

交付时应能明确给出：lint、Judge 和必要的 stress/Judge QA 已通过；Judge 最终结果为 all_expectations_met；特殊 Judge 状态为 passed 且 evidence 为 current；seal 成功；正式 build 后 status 为 current；ZIP 深度验证无错误；单题 PDF 和整场 PDF 已人工检查。

正式交付通常包括整场 main.pdf、每题 problem.pdf 和工作区根目录下的 <ID>.zip。题面、数据、题序、模板或工具链变化后必须重新验证和构建。

## 安装与平台说明

| 工具 | 要求 | 用途 |
| --- | --- | --- |
| [Python](https://www.python.org/downloads/) | CPython 3.10–3.12，Windows/Linux x86_64 | 运行 ProbHub Core |
| [Node.js](https://nodejs.org/) | 18+ | 安装 npm 包和 Agent Skill |
| g++ | C++17 | 编译标程、Validator 和 Checker |
| [Typst](https://github.com/typst/typst/releases/tag/v0.14.2) | 0.14.2 | 生成 PDF |
| Noto Sans CJK SC | 主包内置 | 保证中文题面稳定显示 |

Windows 可用 [MSYS2](https://www.msys2.org/) 安装 g++；Typst 使用固定 0.14.2。Ubuntu：

~~~bash
sudo apt update
sudo apt install -y g++ python3-pip
~~~

固定字体随 probhub 主包提供，正式编译会校验字体字节。macOS 通常可以运行，但不是发布 CI 的强制平台。

不全局安装 npm 包时：

~~~powershell
$env:PROBHUB_ALLOW_SYSTEM_PYTHON = "1"
npx probhub-skill
~~~

~~~bash
PROBHUB_ALLOW_SYSTEM_PYTHON=1 npx probhub-skill
~~~

只安装当前项目的 Skill 时在命令末尾加 --local。

## 常见问题

### 找不到 probhub 命令

~~~bash
node --version
npm --version
npm install -g probhub
~~~

仍找不到时检查 npm 全局可执行目录是否在 PATH 中，也可以临时使用 npx probhub --version。

### doctor 报错

按输出逐项处理，常见原因是 Python 解释器不对、依赖未安装、g++ 或 Typst 不在 PATH、Typst 版本不是 0.14.2，或包内固定字体缺失。重新运行带 PROBHUB_ALLOW_SYSTEM_PYTHON=1 的安装命令即可补装依赖。

### WebUI 打不开

确认当前目录或上级目录存在 .probhub/workspace.yaml，再运行：

~~~bash
probhub --json ui --check
probhub ui --no-browser
~~~

然后访问 <http://127.0.0.1:33933/>。

### build 提示需要 sealed revision

~~~bash
probhub seal L01 --no-cache --seed 12345
~~~

所有题目完成后，再执行一次多题 build。

### seal 提示 seal_judge_qa_failed

运行 probhub judge-qa L01 --no-cache，根据结构化结果修复 Checker、Interactor、模拟选手或 fixture，再重新 seal。FAIL 表示题目基础设施错误，不是成功击杀错解。

### status 显示 stale

读取 stale_fields，重新 seal 并 build，不要手工修改 Manifest。

### 出现 recovery_required

重新运行原来的 build、gen --apply 或 stress --fixate，让 ProbHub 使用事务记录恢复；不要手工删除 .probhub 中的恢复材料。

## 安全边界

ProbHub 适用于本地、单用户、可信的出题环境，WebUI 也只用于这一边界。它限制时间、内存、输出和进程树，但不是强安全沙箱；同机其他进程仍可能访问本机回环服务，CSRF token 也不是多用户主机的身份认证。不要用 ProbHub 执行来源不明或有意攻击主机的代码，这类任务应放在专门的虚拟机或容器中。

## 进一步阅读

- [CLI 完整手册](references/cli.md)
- [安装与发布说明](references/installation.md)
- [Workspace Schema v1](references/workspace-schema-v1.md)
- [数据组与解法期望](references/data-groups-expectations.md)
- [错解分类与数据强度](references/mistake-taxonomy.md)
- [Stress 差分测试](references/stress.md)
- [std 变异测试](references/mutation-testing.md)
- [Agent 验证模式](references/verification-modes.md)
- [Checker 与交互题](references/checker-interactor.md)
- [进程和资源控制](references/process-control.md)
- [Checkpoint、Seal 与 Generation](references/generations.md)

## 参与开发

~~~bash
npm ci
npm run check:fast
npm run check
npm run pack:check
~~~

`check:fast` 使用显式的快速测试清单，适合修改后的即时反馈；`check` 才是提交、发布和 CI 的完整门禁。测试分片只在源码仓库中使用，发布的 npm 包不包含测试文件。`pack:check` 会验证两个 npm 包的内容。可复用的 standard、custom、float、interactive 和 stress 工作区位于 tests/fixtures/。

贡献代码或报告问题，请使用 [GitHub Issues](https://github.com/greenthree/ProbHub-skill/issues)。版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## 鸣谢

- [CYaRon](https://github.com/luogu-dev/cyaron)：测试数据生成工具。
- [olymp-in-typst](https://github.com/lihaoze123/olymp-in-typst)：算法竞赛 Typst 排版模板。
- [testlib](https://github.com/MikeMirzayanov/testlib)：算法竞赛评测辅助库。

## License

[MIT](LICENSE)
