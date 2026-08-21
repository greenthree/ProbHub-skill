# 安装与发布说明

安装、升级、修复依赖或准备 GitHub Release 时使用本说明。面向用户的主 README、兼容包 README 和 Release 必须保留相同的支持版本与系统 Python 授权语义。

## 1. 支持环境

- Node.js 18 或更高版本，包含 npm。
- CPython 3.10、3.11 或 3.12，运行在 Windows/Linux x86_64；Ubuntu 的系统 Python 需要 `python3-pip`。Python 3.13+、PyPy、ARM 和 macOS 当前不在锁文件保证范围内。
- 完整出题还需要支持 C++17 的 `g++` 与 Typst 0.14.2；固定中文字体随主包提供。

不要向用户推荐创建虚拟环境。`probhub-skill` 会选择 `PYTHON` 指定的受支持解释器；未设置时从 PATH 中选择 CPython 3.10–3.12 x86_64。当该解释器不是虚拟环境时，安装 Python 依赖必须显式设置 `PROBHUB_ALLOW_SYSTEM_PYTHON=1`，依赖只写入该解释器的用户依赖目录。Ubuntu 仍由系统包管理器维护全局 Python 包；缺少 pip 时先运行 `sudo apt install python3-pip`。

该变量仅表示用户同意本次安装向所选 Python 的用户依赖目录写入固定版本依赖。安装器使用随 npm 包发布的 `requirements.lock`，强制 wheel-only 与 SHA-256 hash；缺少适用 wheel、lock 不完整或下载字节不匹配都会直接失败，不会回退到未锁定版本或源码构建。锁文件本身不包含 wheel，因此断网时仍需提前准备与同一 hash 匹配的 pip 缓存。安装器只会在非虚拟环境执行 `python -m pip install --user ...` 时，为该 pip 子进程设置 `PIP_BREAK_SYSTEM_PACKAGES=1`；它不会移除 `--user`、使用 `sudo` 或写入发行版维护的全局 `site-packages`。该变量不关闭沙箱限制，不改变构建身份，也不应被描述为永久系统设置。

再次运行安装器时，`.claude/skills/probhub` 与 `.agents/skills/probhub` 会作为完整目录整体替换，而不是增量合并；目录中的本地手工修改不会保留。安装仍以两个目标共用的事务发布，任一目标失败时恢复原目录。

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

## 5. 发布后验证

Git tag、稳定 GitHub Release 和两个 npm 包全部发布后，在仓库的 GitHub Actions 页面手动运行 **Published release verification**。输入不带 `v` 的精确 semver，例如 `0.6.8`。不要在 tag、Release 或第一个 npm 包刚创建时提前运行；该流程有意不自动触发，也不会回退到本地 tarball、全局 link、`latest` 或其他 registry。

流程先核对：

- tag、本地版本和 GitHub Release 是同一精确版本，Release 不是 Draft 或 prerelease；
- `probhub` 与 `probhub-skill` 的正式 registry 身份、`latest`、integrity、shasum 和包清单；
- 兼容包只精确依赖同版本的 `probhub`。

身份核对通过后，Windows 与 Ubuntu 会从 `https://registry.npmjs.org/` 安装两个精确版本，在隔离 npm prefix、Python 环境和临时工作区中执行 `doctor -> init -> new -> gen -> judge -> judge-qa -> seal -> build -> status -> verify-package`。三个 job 都会上传结构化 JSON evidence，保留 90 天。

发布后验证证明该版本在当次 GitHub runner 和官方 registry 上可完成交付闭环；它不证明所有镜像已同步，也不替代目标 Linux/DOMjudge 的时间、内存和真实导入校准。registry 短暂未同步时应等待后重跑同一精确版本，不能改用 `latest` 规避失败。
