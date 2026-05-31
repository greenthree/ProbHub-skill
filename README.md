# ProbHub-skill: ACM/ICPC 自动化出题工作流
![ProbHub Logo](logo.svg)

ProbHub 是一个基于大语言模型 (LLM Agent) 和现代排版/测试框架构建的算法竞赛出题环境。本项目旨在通过标准化的自动化工作流，减少造数据、调格式和配置评测包等重复性劳动。

---

## 核心特性

* **Agent 驱动**: 内置 `SKILL.md`，兼容 Claude Code 等 Agent 框架，通过自然语言指令完成出题流程。
* **数据生成**: 集成 CYaRon，支持通过 Python 脚本快速生成树、图等复杂数据结构。
* **数据检验**: 使用沙箱（可自定义 validator）对小数据暴力对拍，全局保证卡掉暴力、错解。
* **极速排版**: 采用 Typst 模板，替代传统 LaTeX，大幅提升题面 PDF 编译速度，支持 WebUI 手动修改题面，后续会更新修改 lib.typ，自定义排版的功能。
* **DOMjudge 兼容**: 自动生成 `problem.yaml`、`domjudge-problem.ini` 及基于 `testlib.h` 的验证器，一键打包 `.zip` 题库。

**[点击查看：真实赛事 Typst 题面排版示例](https://github.com/greenthree/ProbHub-skill/blob/main/typst-template/%E6%AD%A3%E5%BC%8F%E8%B5%9B/main.pdf)**

---

## 快速安装

在一个干净的空文件夹中打开终端，选择以下任意一种方式即可一键初始化（自动安装依赖并注入 Claude 技能库）：

**方式一：使用社区通用 Skills 框架 (推荐)**
```bash
npx skills add greenthree/ProbHub-skill
```

**方式二：使用本项目的独立脚手架**

```bash
npx probhub-skill
```

---

## 环境依赖与前置安装

除了通过上述命令一键安装的工具外，底层编译仍需要依赖本地的运行环境。请确保你的系统已配置好以下基础依赖：

**1. 基础编译环境**

* Python 3.8+
* GCC/G++ (用于编译 `outmaker.cpp` 和 `checker.cpp`)

**2. Python 依赖包**

```bash
pip install cyaron pypdf flask
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
本项目的 Typst 模板指定了高标准的竞赛级中英文字体栈。为保证 PDF 成功编译且排版符合预期，请务必在本地系统中安装以下字体：

* **开源英文字体**: `New Computer Modern Math` (用于公式与标准西文)
* **开源等宽字体**: `New Computer Modern Mono` (用于代码块)
* **方正楷体**: `FZKai-Z03` (用于题面特殊说明)
* **华文中宋**: `STZhongSong` (用于标题与加粗)
* **微软雅黑**: `Microsoft YaHei` (用于无衬线文本)
* **宋体**: `SimSun` / `simsun` (用于基础中文正文)

---

## 使用说明

本项目设计为由拥有本地文件读写权限的 AI Agent 工具（如 Claude Code）驱动。

1. **唤醒助手**: 在项目根目录启动 Agent (执行 `claude`)，输入指令：“**使用 probhub 技能，我要出一道新题。**”
2. **提供题面**: 随意提供你构思的 Markdown、PDF、网页链接或纯文字想法。
3. **全自动执行**: 助手将根据 `SKILL.md` 的设定，自动建立题目目录、编写数据生成器和标准程序、构造 `data/` 测试集、合并排版 Typst 题面，并最终打包成可直接上传至 DOMjudge 的 `.zip` 压缩包。

> **提示**: `typst-template/热身赛` 目录中保留了手动添加插图、副标题的进阶方法，如有需要可参考该模板进行手动扩展。

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
│   ├── lib.typ              # Typst 宏包依赖
│   ├── main.typ             # Typst 组卷入口
│   ├── problems.typ          # Typst 单题模板
│   └── problems-sample.json  # 题目元数据配置样例
├── scripts/                 # 自动化脚本 
│   ├── add_problem.py       # 向 problems.json 追加新题
│   ├── extract_new_problem.py # 自动裁剪单题 PDF
│   └── local_judge.py         # 轻量化本地评测与宿命自检
├── typst-template/          # Typst 题面示例
│
└── [英文目录名]/               # 示例：由 AI 顺次生成的独立题目文件夹（如 A/）
    ├── std.cpp                # 标程：最优解代码（宿命：100% AC）
    ├── validator.cpp          # 格式校验：基于 testlib.h 检查输入合规性
    ├── brute.cpp              # 强力对拍：无脑暴力正确解（宿命：AC 或 TLE，绝不能 WA）
    ├── wrong.cpp              # 错解验证：故意写错的贪心/溢出代码（宿命：必须吃 WA/RE）
    ├── meta.json              # 单题元数据（供 Typst 渲染题面使用）
    ├── problem.pdf            # 由脚本自动裁剪出的单题独立题面
    ├── domjudge-problem.ini   # DOMjudge 运行参数配置文件（如 timelimit）
    ├── problem.yaml           # DOMjudge 题目元数据（如 name, memory limit 和特殊评测）
    ├── data/                  # 全量测试数据集
    │   ├── sample/            # 样例数据：存放 1.in, 1.ans（严格对应题面样例）
    │   └── secret/            # 盲测强数据：存放 2.in, 2.ans ~ 30.ans（含极限与卡错解数据）
    └── output_validators/     # （仅自定义题目适用）存放编译后的 checker/interactor

```

---



## 鸣谢

本项目的基础设施依赖于以下优秀的开源生态：

* **[CYaRon](https://github.com/luogu-dev/cyaron)**: 洛谷团队开源的 Python 测试数据生成库。
* **[olymp-in-typst](https://github.com/lihaoze123/olymp-in-typst)**: 基于 Typst 的算法竞赛题面极简排版模板。
* **[testlib](https://github.com/MikeMirzayanov/testlib)**: 业界标准的 C++ 评测辅助库。

