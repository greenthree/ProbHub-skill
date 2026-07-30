# Changelog

本文件整理自 README 历史快照、GitHub PR 与 npm 发布记录（首次建立于 2026-07-26）。此前版本的条目为事后补记，粒度以 PR 为准。

## [Unreleased]

## [0.6.0] - 2026-07-29

- DOMjudge ZIP 改为流式确定性写入，包内精确小写 `.in`/`.ans` 统一为 LF-only；`verify-package` 在打开 ZIP 前核对归档大小与中央目录条目数，并增加受限流式解压、路径/大小写/文件类型/体积栅栏、无重复/未知键的严格配置解析、包内 output validator 编译，以及带 `--problem` 的题名/限制/Judge/源数据对账和输入 Validator 全点复核；多题 `package` 改为 selected-only 快照和整批 journal/rollback 发布。
- 交互题 Transcript 改为双向原子共享原始字节预算，并按方向使用 UTF-8 增量解码，避免并发超额记录和字符跨读取块时的伪乱码；交互 activity/traffic 快照同步收口，沙箱缓存 Schema 升级以隔离旧结果。
- `typeset` 改为全卷输入快照、切页完成后输入栅栏和 journal/rollback 整批发布，Typst、切页或替换失败不再污染已有 metadata/PDF；Windows/Ubuntu CI 固定校验 Typst 0.14.2 压缩包与 Noto Sans CJK SC 字体哈希，在忽略系统字体的条件下真实编译三页双题试卷、切出 2+1 页单题 PDF，并验证语法错误零发布。
- `init` 从 npm 包内资源创建可直接组卷的固定 Typst 模板，脚手架包含真实确定性 recipe；npm 入口统一探测 Python >=3.10，Skill 注入使用 staging/回滚且依赖或复制失败返回非零。新增版本/CHANGELOG/tag/双包内容门禁，以及 Windows/Ubuntu 从本地 tarball、全新 npm prefix 与 Python venv 执行 `doctor -> init -> new -> gen -> judge -> seal -> build -> status -> verify-package` 的干净安装闭环；同时修复 MinGW 在中文与空格工作区路径下的编译参数乱码。

## [0.5.0] - 2026-07-28

校准与题面体检版本。

- Judge 生成本机资源校准 evidence，汇总 accepted 的 TL 余量和期望 TLE/MLE/OLE 的击杀证据；lint/status 以 `current/missing/stale/invalid` 报告，并固定声明本地测量不等于目标 Linux/DOMjudge 承诺。
- 新增只读 `sample-check`：只运行样例与首个 accepted，按换行归一后的严格字节比较核对 `.ans`；Custom Checker 不能掩盖样例答案不一致，完整 Judge/build/seal 复用同一门禁。
- lint 确定性拒绝题面 H1/章节/样例来源和 statement/Validator 路径错误，并输出始终非阻断的题面—Validator 约束对账报告；动态、析取和不支持表达式明确要求人工复核。
- 新增 `constraints` 单一事实源设计评估；当前 Schema 尚未启用该字段，后续实现必须同时覆盖 Judge/stress/gen、缓存与 hash、WebUI round-trip、Typst 和 build snapshot。
- solution 结构化条目支持本地 Judge `run_on` 运行域：多组并集、sample 隐式执行、首 accepted 全量栅栏、期望覆盖校验，并在结果/evidence 中公开执行与跳过用例；stress 与 DOMjudge 包语义不变。
- accepted 可声明 `independence.from/basis/note` 供人工复核，Core 阻断同路径、同字节和直接 include 等确定反证；高难度单 accepted 且无额外全域 AC 参考时给结构化 warning。`new` 的 std2 改为真正不同的按位进位加法实现。
- 本地校准证据升级为 `judge-evidence-v2.json`：记录并验证每个解法的实际运行域，状态判断按 schema、source/data、平台、策略与结构分层；旧 v1 文件继续忽略但不再读取。
- 新增只读 `probhub report`：按正式题序汇总难度、标签、测试规模、数据组配比、recipe 覆盖、TL headroom 与错解击杀矩阵；支持终端、Markdown 和 JSON。recipe 的随机/定向/近上界判断明确标为启发式，报告不运行外部工具或写入工作区。

## [0.4.0] - 2026-07-27

数据工坊与正式发布证据版本。

- `probhub gen` 以结构化 recipe 复现 secret 数据，运行 Generator、Validator、首个 accepted 与 Custom Checker；plan 保持只读，apply 在锁内重载配置，并以带回滚的事务同时发布 `.in`、`.ans` 与 gen manifest。
- `stress --against` 可面向已声明错解寻找 killer，生成可直接复制的完整 replay 命令；`--fixate` 在锁内复现命中输入、重产答案，并事务化写入数据、recipe、group 与 target。
- `seal` 将验证阶段的 source/data hash 绑定到不可变 sealed checkpoint，关闭验证结束至复制 revision 之间的竞态。
- 正式 `build` 要求整场所有题目存在与 live 输入匹配的 sealed revision，并在发布前再次复核；全部正式产物通过带 journal 的同卷事务发布，失败回滚且下次 build 可恢复硬中断；Build Manifest 升级到 v3，记录 `sealed_revision_id`，旧 v1/v2 明确显示 stale。
- build/gen/fixate 事务统一校验 journal 路径与结构，并以 committed 标记区分“待回滚”和“已发布待清理”；任意 writer 会先恢复三类事务，只读 lint/status/judge 遇到 pending journal 返回 `recovery_required`，不再读取半发布状态。
- source hash 覆盖 `code/` 下全部普通辅助源码（含 Python 与 `.inc/.ipp/.tcc`），并拒绝工作区外的题目目录、journal 路径穿越和符号链接逃逸。
- `new` 提供 standard/custom/interactive 三种可编译 A+B 脚手架，包含双 accepted、典型错解、定向数据组与完整 manual recipes。

## [0.3.8] - 2026-07-27

安全与交互评测围栏维护版本，从 `v0.3.7` 维护线发布，不包含尚未发布的 0.4 数据工坊功能。

- WebUI 对 Host、来源和写请求 token 做统一校验，限制上传请求体大小，阻止远端页面跨站触发本机编译评测。
- Markdown 题面预览在写入 DOM 前进行净化，阻止题面中的脚本、事件属性和危险 URL 执行。
- 交互题复用共享进程控制，Windows 在恢复进程前完成 Job Object 收容；补齐快速退出、输出超限、峰值内存与并发 stderr 清理语义。

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
