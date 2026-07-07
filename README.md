# ProbHub Skill

![ProbHub Logo](logo.svg)

ProbHub Skill 是一个面向 ACM/ICPC、XCPC 和 DOMjudge 的自动化出题工作流。它把题面整理、数据生成、标准程序与错解验证、Typst 组卷、单题 PDF 裁剪、WebUI 微调和 DOMjudge 打包串成一条可由 Agent 执行的流程，尽量减少出题过程中重复、易错、但又必须严谨完成的杂活。

> 适合场景：从一个题目 idea 或现有题面出发，快速生成一份可自检、可排版、可上传 DOMjudge 的题目目录。

---

## 核心能力

- **Agent 驱动出题**：内置 [SKILL.md](SKILL.md)，让 Claude Code、Codex 或兼容 Agent 按固定流程完成出题任务。
- **严谨数据闭环**：自动组织 `std.cpp`、`validator.cpp`、`brute.cpp`、`wrong.cpp`，并通过本地沙箱检查标程 AC、暴力不 WA、错解被卡掉。
- **时空限制自检**：`local_judge.py` 支持读取 `meta.json`、`domjudge-problem.ini`、`problem.yaml` 中的时间和内存限制，并报告 TLE/MLE/RE/WA/AC。
- **Typst 高速排版**：使用 Typst 模板生成全卷 PDF，并能按题目自动裁剪出独立 `problem.pdf`。
- **WebUI 微调题面**：提供 Flask 控制台，支持题目排序、Markdown 预览、样例编辑、引言 Quote、时空限制编辑、全卷编译和 PDF 分发。
- **DOMjudge 兼容**：生成 `problem.yaml`、`domjudge-problem.ini`、数据目录和自定义 checker/interactor 所需结构，可打包为 `.zip`。

示例 PDF：
[真实赛事 Typst 题面排版示例](https://github.com/greenthree/ProbHub-skill/blob/main/typst-template/%E6%AD%A3%E5%BC%8F%E8%B5%9B/main.pdf)

---

## 快速安装

推荐使用 npm 脚手架安装：

```bash
npx probhub-skill
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
pip install cyaron pypdf flask
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
3. 编写 `std.cpp`、`validator.cpp`、`brute.cpp`、`wrong.cpp` 和数据生成器。
4. 生成 `data/sample` 与 `data/secret`。
5. 运行本地沙箱自检。
6. 加入 Typst 组卷并裁剪 `problem.pdf`。
7. 按需生成 DOMjudge 题目包。

---

## 本地沙箱自检

对题目目录执行：

```bash
python scripts/local_judge.py <problem_dir>
```

成功时应看到：

```text
[+] 恭喜！所有代码均符合预期宿命
```

`local_judge.py` 会检查：

- `validator.cpp` 是否接受所有输入数据。
- `std*.cpp` 是否全部 AC。
- `brute*.cpp` 是否不 WA，并且至少出现 TLE 或 MLE，用于证明强数据足够强。
- `wrong*.cpp` 是否不能全 AC。
- 每个测试点的时间和内存状态。

WebUI 会使用 JSONL 模式读取沙箱事件：

```bash
python scripts/local_judge.py <problem_dir> --jsonl
```

时空限制读取优先级：

1. `<problem_dir>/meta.json` 中的 `problem.time_limit` 和 `problem.memory_limit`
2. `domjudge-problem.ini` 中的 `timelimit`
3. `problem.yaml` 中的 `limits.memory`
4. 默认 `1s / 256MB`

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

---

## DOMjudge 打包约定

每道题建议保持如下结构：

```text
<problem_dir>/
├── std.cpp
├── validator.cpp
├── brute.cpp
├── wrong.cpp
├── checker.cpp              # 可选，非唯一答案时需要
├── interactor.cpp           # 可选，交互题需要
├── meta.json
├── problem.pdf
├── domjudge-problem.ini
├── problem.yaml
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
- `output_validators/`，如果存在自定义评测

---

## 仓库结构

```text
ProbHub-skill/
├── SKILL.md                  # Agent 工作流指令
├── README.md                 # 项目说明
├── bin/
│   └── init.js               # npm 脚手架入口
├── references/
│   ├── cyaron.md             # CYaRon 快速参考
│   ├── fast.md               # 简单 C++ 数据生成模板
│   ├── testlib.h             # testlib 头文件
│   ├── lib.typ               # Typst 宏与样式
│   ├── main.typ              # Typst 主入口模板
│   ├── problems.typ          # Typst 题目列表入口
│   └── problems-sample.json  # 单题元数据样例
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
pip install flask
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
