# ProbHub-skill: ACM/ICPC 自动化出题工作流

ProbHub 是一个基于大语言模型 (LLM Agent) 和现代排版/测试框架构建的算法竞赛出题环境。本项目旨在通过标准化的自动化工作流，减少造数据、调格式和配置评测包等重复性劳动。

---

## 核心特性

* **Agent 驱动**: 内置 `SKILL.md`，兼容 Claude Code 等 Agent 框架，通过自然语言指令完成出题流程。
* **数据生成**: 集成 CYaRon，支持通过 Python 脚本快速生成树、图等复杂数据结构。
* **极速排版**: 采用 Typst 模板，替代传统 LaTeX，大幅提升题面 PDF 编译速度。
* **DOMjudge 兼容**: 自动生成 `problem.yaml`、`domjudge-problem.ini` 及基于 `testlib.h` 的验证器，一键打包 `.zip` 题库。

---

## 目录结构

```text
ProbHub/
├── README.md                # 项目说明文档
├── SKILL.md                 # Agent 工作流指令与约束
├── references/              # 外部工具与模板
│   ├── cyaron.md            # CYaRon API 快速参考
│   ├── fast.md              # 基础数据生成规范
│   ├── testlib.h            # SPJ/交互题评测头文件
│   └── typst-template/      # Typst 初始模板库
│       ├── main.typ
│       └── problem-sample.json
├── scripts/                 # 自动化脚本 
│   ├── add_problem.py       # 向 problem.json 追加新题
│   └── extract_new_problem.py # 自动裁剪单题 PDF
└── workspace/               # 工作区 (由 AI 自动生成)
    ├── balance/             # 例：具体题目目录
    └── typst-statement/     # 例：比赛统一组卷目录

```

---

## 环境依赖与安装

**1. 基础环境**

* Python 3.8+
* GCC/G++ (用于编译 `std.cpp` 和 `checker.cpp`)

**2. Python 依赖包**

```bash
pip install cyaron pypdf
```

**3. Typst 编译器**
请前往 [Typst 官方 Github](https://github.com/typst/typst/releases) 下载对应系统的可执行文件，或使用包管理器安装：

```bash
# macOS
brew install typst

# Windows
winget install typst
```

**4. 字体依赖 (核心排版要求)**
本项目的 Typst 模板指定了特定的中英文字体栈。为保证 PDF 成功编译且排版符合预期，请务必在本地系统或 Typst 字体路径中安装以下字体：

* **开源英数与代码字体:**
* `New Computer Modern Math` (用于公式与标准西文)
* `New Computer Modern Mono` (用于代码块与等宽字符)


* **中文字体系列:**
* `FZShuSong-Z01` (方正书宋 - 正文衬线)
* `FZKai-Z03` (方正楷体 - 题面特殊说明)
* `STZhongSong` (华文中宋 - 标题与加粗)
* `Microsoft YaHei` (微软雅黑 - 无衬线)
* `SimSun` / `simsun` (宋体 - 基础中文)
* `SimSun-ExtG` (宋体扩展 - 特殊字形支持)



> **字体部署提示**:
> 如果你在 Linux 服务器或纯命令行环境（无桌面系统）下运行本项目，可以将上述字体文件 (`.ttf` / `.otf`) 统一放入一个字体文件夹（例如项目根目录下的 `fonts/`），然后在 Agent 脚本或环境中指定 Typst 的字体搜索路径：
> `export TYPST_FONT_PATHS=./fonts`


---

## 使用说明

本项目设计为由拥有本地文件读写权限的 AI Agent 工具（如 Claude Code）驱动。

1. **唤醒助手**: 在项目根目录启动 Agent，输入指令：“使用 probhub 技能，我要出一道新题。”
2. **提供题面**: 提供题目的 Markdown、PDF、网页链接或初始构思。
3. **自动化执行**: 助手将根据 `SKILL.md` 的设定，自动建立目录、编写生成器和标程、构造 `data/` 测试集、合并 Typst 题面并最终打包为可上传到 DomJudge 的 `.zip`。

**[Typst 题面示例](https://uploadfiles.nowcoder.com/files/20260503/468072_1777778993097/第十二届苏州科技大学程序设计竞赛正式赛.pdf)**

---

## 鸣谢

本项目的基础设施依赖于以下开源项目：

* **[CYaRon](https://github.com/luogu-dev/cyaron)**: 洛谷团队开源的 Python 测试数据生成库。
* **[olymp-in-typst](https://github.com/lihaoze123/olymp-in-typst)**: 基于 Typst 的算法竞赛题面排版模板。
* **[testlib](https://github.com/MikeMirzayanov/testlib)**: Codeforces 官方维护的 C++ 评测辅助头文件。


