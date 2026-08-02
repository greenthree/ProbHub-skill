# 安装与发布说明

安装、升级、修复依赖或准备 GitHub Release 时使用本说明。面向用户的主 README、兼容包 README 和 Release 必须保留相同的支持版本与系统 Python 授权语义。

## 1. 支持环境

- Node.js 18 或更高版本，包含 npm。
- Python 3.10 或更高版本；Ubuntu 的系统 Python 需要 `python3-pip`。
- 完整出题还需要支持 C++17 的 `g++` 与 Typst 0.14.2；固定中文字体随主包提供。

不要向用户推荐创建虚拟环境。`probhub-skill` 会选择 `PYTHON` 指定的解释器；未设置时选择 PATH 中的 Python 3.10+。当该解释器不是虚拟环境时，安装 Python 依赖必须显式设置 `PROBHUB_ALLOW_SYSTEM_PYTHON=1`，依赖只写入该解释器的用户依赖目录。Ubuntu 仍由系统包管理器维护全局 Python 包；缺少 pip 时先运行 `sudo apt install python3-pip`。

该变量仅表示用户同意本次安装向所选 Python 的用户依赖目录写入固定版本依赖。它不关闭沙箱限制，不改变构建身份，也不应被描述为永久系统设置。

## 2. 持久安装

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

`doctor` 必须能够报告 Python、Node.js、npm、`g++`、Typst、固定字体和 Python 依赖。进入 Schema v1 工作区后，再运行 `probhub --json ui --check` 检查已安装 WebUI。

## 3. 临时安装

Windows PowerShell：

```powershell
$env:PROBHUB_ALLOW_SYSTEM_PYTHON = "1"
npx probhub-skill
```

Ubuntu/Linux：

```bash
PROBHUB_ALLOW_SYSTEM_PYTHON=1 npx probhub-skill
```

只写入当前项目的 `.claude/skills/probhub` 与 `.agents/skills/probhub` 时增加 `--local`。不要给 `npx probhub-skill` 写成缺少允许开关的裸命令。

## 4. Release 安装段落

GitHub Release 使用第 2 节的双平台命令，不另创安装路径。先发布 `probhub` 主包并确认目标版本可安装，再发布精确依赖同版本主包的 `probhub-skill`；两个包均可解析到该版本后再创建 GitHub Release。
