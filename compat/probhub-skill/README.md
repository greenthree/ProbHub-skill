# probhub-skill

`probhub-skill` 是 ProbHub 的轻量 npm 入口包。完整实现由同版本的 [`probhub`](https://www.npmjs.com/package/probhub) 主包提供；本包只保留命令转发，不复制 Python Core、WebUI、Skill 或 references。

安装前需要 Node.js 18 或更高版本（包含 npm），以及 Python 3.10 或更高版本；Ubuntu 的系统 Python 还需安装 `python3-pip`。

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

`PROBHUB_ALLOW_SYSTEM_PYTHON=1` 明确授权安装器把固定版本的 Python 依赖安装到当前 Python 的用户依赖目录，不会覆盖 Ubuntu 由系统包管理器维护的 Python 包。PowerShell 中的设置只在当前终端会话生效。需要指定另一套 Python 3.10+ 时，先设置 `PYTHON` 指向该解释器。

临时运行时使用：

```powershell
$env:PROBHUB_ALLOW_SYSTEM_PYTHON = "1"
npx probhub-skill
```

```bash
PROBHUB_ALLOW_SYSTEM_PYTHON=1 npx probhub-skill
```

只安装到当前项目的 Agent Skill 目录时增加 `--local`。安装后在包含 `.probhub/workspace.yaml` 的目录运行 `probhub --json ui --check` 检查 WebUI，运行 `probhub ui` 启动它。

## 维护规则

- 本包版本必须与 `probhub` 主包版本完全一致。
- 必须先发布 `probhub`，确认 npm registry 可安装后，再发布本包。
- 两个包的同版本均可从 npm 安装后，才能创建对应 GitHub Release。
- 本包的 `dependencies.probhub` 必须锁定精确版本，不能使用 `^` 或 `~`。
- 功能代码只在 `probhub` 主包中维护，本包不得复制实现。
