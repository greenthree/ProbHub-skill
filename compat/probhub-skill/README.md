# probhub-skill compatibility package

`probhub-skill` 是 ProbHub 的兼容 npm 包，用于保留既有安装命令：

```bash
npx probhub-skill
npx probhub-skill --local
```

完整实现由同版本的 [`probhub`](https://www.npmjs.com/package/probhub) 主包提供。本包只保留 `probhub-skill` 与 `probhub` 两个命令转发入口，不复制 Python Core、WebUI、Skill 或 references。

新用户若需要持久的 CLI，推荐直接安装主包：

```bash
npm install -g probhub
probhub-skill
probhub --version
```

## 维护规则

- 本包版本必须与 `probhub` 主包版本完全一致。
- 必须先发布 `probhub`，确认 npm registry 可安装后，再发布本包。
- 本包的 `dependencies.probhub` 必须锁定精确版本，不能使用 `^` 或 `~`。
- 功能代码只在 `probhub` 主包中维护，本包不得复制实现。
