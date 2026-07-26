# Changelog

本文件整理自 README 历史快照、GitHub PR 与 npm 发布记录（首次建立于 2026-07-26）。此前版本的条目为事后补记，粒度以 PR 为准。

## [0.4.0] - 2026-07-26

数据工坊版本（ROADMAP 0.4.0：secret 数据从"裸字节"变成"可执行配方"，错解从"手工找刀"变成"一条命令找刀"，新题从"手写样板"变成"开箱即编译"）。

- **`new` 可编译脚手架**（PR #29）：生成完整可工作的 A+B 示例题——testlib validator 样板、双 accepted（std + 独立 std2 槽位）、未登记 brute 槽位、思路层/实现层双示例错解（后者由 overflow 定向数据组击杀，示范期望矩阵写法）、testlib 生成器骨架与样例/隐藏数据；三种 judge 模式（standard/custom/interactive）lint 零错误、judge 开箱 `all_expectations_met`；工作区注册纳入写锁与原子写。
- **`probhub gen` 测试点配方**（PR #30）：`data.recipes` 为每个 secret 测试点声明来源（`manual` 或生成器 + 精确 args）；gen 编译生成器与首个 accepted，按配方生成 → Validator 过检 → 产 `.ans`，plan 模式报告 new/changed/unchanged（即字节一致性验证），`--apply` 才写入且任一失败零写入；输出 LF 归一保证跨平台字节一致；lint 报告无配方测试点（存量兼容 warning）。
- **`stress --against` 错解找刀**（PR #32）：对拍 accepted 与任意目标解法，语义翻转——不一致即 `killer_found`（反例可 replay），全程一致报 `not_separated`；`--fixate` 一步固化命中为归一化 secret 数据 + gen 配方（stress 生成器与命中轮精确 argv）+ 定向 `wrong-solution-killer` 数据组；stress 生成器输出统一 LF 归一（修复 Windows CRLF 被 Linux 语义 validator 拒绝）。
- 修复 `process_alive` 存活判定（PR #31）：Windows 用退出码判定而非"可打开即存活"（句柄挂住的僵尸/PID 复用不再误判），Unix EPERM 判存活；容器化测试改为持句柄确定性断言。
- CI 加固（PR #33）：push 只跑 main + concurrency 取消过期运行（消除同 commit 双跑），集成测试对脚手架题放宽 TL 余量。

## [0.3.7] - 2026-07-26

npm：`probhub@0.3.7`、`probhub-skill@0.3.7`；Git tag `v0.3.7`（发布提交 `e0842ef`）。

审计修复与出题方法论版本（原计划的 0.3.8 文档里程碑并入本次发布）。

- P0 正确性修复（PR #23）：组卷不再静默缺题——checkpoint 锁忙有界重试后以 `checkpoint_busy` 显式失败，占位题目连同原因经顶层与 manifest `missing` 字段报告；Windows 子进程改为挂起创建 → 入 Job → 恢复，消除 Job Object 逃逸窗口；CLI judge 与 output validator g++ 编译纳入受控超时与输出限额；`write_yaml`/`write_json` 全部原子化；损坏 build manifest 报告为结构化 `never-built` 而非 traceback；临时 `.probhub-*.typ` 从 workspace hash、快照与 gitignore 三处统一排除。
- P1 评测与交付正确性（PR #26/#27）：MLE 判定统一为峰值内存证据（普通段错误报 RE）；`stress.args` 位置模板报结构化错误；lint 拒绝布尔 `limits.time`；非 UTF-8 样例报 `sample_not_utf8`；WebUI 提交清理容忍 Windows 句柄延迟释放；doctor 捕获工具挂起；损坏 generation 目录先验证再复用、stage 失败后清理；output validators 改为 stage 后原子切换发布。
- CI 与仓库卫生（PR #24）：CI 显式断言 g++、安装固定 Typst v0.14.2 并做编译冒烟、pip 缓存、改用双包 `pack:check`；`requirements.txt` 锁定兼容版本；删除热身赛构建产物 PDF 并保留文档化的正式赛排版样例。
- 出题方法论文档（PR #25）：新增 `references/mistake-taxonomy.md`（思路/复杂度/实现三层错解枚举、按算法族常见误实现、死亡模式数据构造、几何精度错法族、交互三轴、数据强度纪律），SKILL.md 第 5 节并入题面写法守则与错解枚举要求，`references/checker-interactor.md` 增加 Validator/Checker/Interactor 工艺守则。改写自 ICPC-Problem-Creator.skill（MIT，署名见文件头）。

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
