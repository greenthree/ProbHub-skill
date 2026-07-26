# Changelog

本文件整理自 README 历史快照、GitHub PR 与 npm 发布记录（首次建立于 2026-07-26）。此前版本的条目为事后补记，粒度以 PR 为准。

## [0.3.6] - 2026-07-12

npm：`probhub@0.3.6`、`probhub-skill@0.3.6`；Git tag `v0.3.6`（合并提交 `b1630d1`）。

- 不可变试卷 generation（PR #18）：`checkpoint` 发布不可变题目 revision；`seal` 自动运行 lint/judge/已配置 stress 并立即用全部题目的最新 checkpoint 组装隔离完整试卷；`assemble` 只消费 checkpoint，不读取其他 Agent 正在编辑的 live 文件，不覆盖正式产物。九题隔离回归首次组卷约 5.5 秒，缓存命中约 1 秒。
- WebUI 能力文案澄清与收紧（PR #20、#21）。
- 版本升级至 0.3.6（PR #22）。

## [0.3.5] - 2026-07

npm：`probhub@0.3.5`、`probhub-skill@0.3.5`；Git tag `v0.3.5`。

- 响应式 Light/Dark 双主题 WebUI（PR #10）。
- BuildPlan、输入变更检测、构建锁与批量 staging（PR #11）。
- 版本升级至 0.3.5（PR #12）。
- Schema v1 WebUI 写入边界重构（PR #13）：导航与 PDF 查看只读；规范源保存具备 revision、OS 锁和原子回滚；预览隔离于系统临时目录；正式分发统一调用 Core build。
- 题面媒体预览与 WebUI writer 队列修复（PR #14）：题面图片走受控只读路由；自动保存、封面保存、编译和分发前端串行协调，消除自身争锁的连续 `build_busy`。
- 分发构建编译缓存修复（PR #15）：正式 build 把摘要匹配的编译产物与沙箱缓存一起发布；九题回归冷构建 106.198s → 后续 10.959s，48 个编译项全部命中。
- Light 主题编辑器表面对比度修复（PR #17）。

## [0.3.4] 及更早

- Manifest v2、`collection_hash`、多题 build Fixture 与题面媒体哈希（PR #9）。
- npm 双包策略（PR #6）：`probhub` 完整主包 + `probhub-skill` 同版本轻量转发包。
- 精简安装文档（PR #7）；Typst 使用 KaiTi（PR #8）。
- 沙箱进程控制与 WebUI 临时提交评测（PR #5）。
- 更早历史见 GitHub PR 记录。
