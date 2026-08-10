# Changelog

本文件整理自 README 历史快照、GitHub PR 与 npm 发布记录（首次建立于 2026-07-26）。此前版本的条目为事后补记，粒度以 PR 为准。

## [Unreleased]

- 新增 `mutation.schema_version: 1` 题目级人工排除：按稳定 mutation ID 记录有界非空理由，lint 拒绝未知字段、错误版本、无效/重复 ID、超量记录和非 standard Judge 配置；排除在 `--max-mutants` 前应用，失效旧 ID 保留并报告 warning。
- mutation evidence 升至 v2，并在 JSON、终端和 Markdown report 中并列 raw、excluded、effective、selected、out-of-scope 与 unmatched exclusion 计数；部分算子运行不会把其他算子的有效排除误报为失效。排除理由进入 source/计划哈希并逐项展示，Schema 类型、当前 Core/编译器身份、配置变化和输入变化均参与 current 判定；发布故障保留旧 evidence。人工排除仍不构成正确性证明或 build 硬门禁。
- mutation 候选定位改为固定 Tree-sitter C++ 语法树，只接受函数/lambda 复合语句体中的真实表达式，排除模板尖括号、运算符声明、`<=>`、宏、concept/requires、`case` 标签、`static_assert` 与未求值上下文；保留仍有效的 `cpp-token-v1` ID，旧 evidence 变 stale，解析失败不回退 Token 扫描且不覆盖最后成功 evidence。新增 UTF-8、CRLF/LF 和复杂 C++ canonical plan Fixture，并把解析器版本纳入 builder fingerprint、Doctor smoke 与依赖审计。

## [0.6.6] - 2026-08-09

- 统一 Schema v1 特殊 Judge 路径栅栏：lint、正式打包和 local Judge 共同拒绝绝对路径、`..`、符号链接、junction/reparse point、非普通文件和题目目录外目标，并保留 `judge.type: checker` 兼容别名。
- 新增共享 Checker/Interactor Core 运行层，供 local Judge、stress 与数据生成复用；结构化结果分离 verdict、execution status、failure kind、责任方、终止原因、双方资源/流量证据与清理结果，沙箱缓存 Schema 升至 7。Checker feedback 保留有界诊断与正式失败原因，Interactor 的资源、启动、取消和清理异常使用一致的 fail-closed 语义。
- 新增 `judge.qa.schema_version: 1`：Checker/Interactor fixture 使用题目内路径栅栏、Windows 大小写不敏感 ID 去重、数量与字节上限，以及原始字节 `fixture_hash`；`judge-fixtures/` 和 `code/judge-qa/` 纳入 source/checkpoint 跟踪，并提供独立 Checker/Interactor Fixture 工作区。
- 新增隔离 `judge-qa` 执行与 CLI：在一致快照中复用正式 Validator、Checker/Interactor 和资源控制，运行声明 fixture、模拟选手及内建鲁棒性探针；支持 OS 锁、总 deadline、取消、编译复用、最终输入哈希围栏和清理失败优先级。
- Judge QA evidence v1 只在完整成功后原子发布；失败、取消、超时、锁竞争、输入变化与发布故障保留上一份成功证据。lint/status/report 会报告 `current`、`missing`、`stale`、`invalid`，配置 QA 的特殊 Judge 必须在 `seal` 前取得 current passed evidence；WebUI 与 Legacy 不接入该能力。
- 新增首版 `mutation`/`mutate` CLI：对标准题首个 C++ accepted 进行比较边界、布尔条件和十进制整数边界的保守 Token 变异，在临时快照中复用正式 Validator/Judge，输出 `killed`、`survived`、`compile-invalid` 和 `infrastructure-failed` 分类。
- 新增 mutation evidence v1 的 source/data/计划哈希、编译器指纹、稳定 mutation ID、计数与有界文本/命中/诊断校验；成功才原子发布，失败、取消、超时、证据超限和输入变化保留上一份成功证据。变异测试作为数据覆盖补充证据，不替代证明、独立 std、期望矩阵或 stress，也不作为 build 硬门禁。
- 安全升级 `pypdf` 至 `6.15.0`，修复审计发现的 `CVE-2026-71852` 与 `CVE-2026-71870`。

## [0.6.5] - 2026-08-04

- Schema v1 题面—Validator 约束对账新增多组数据累计约束检查：保守识别 LaTeX 求和、中文“所有测试用例之和”表述，以及 Validator 中直接 `+=` / `acc = acc + term` 累加和后续 `ensuref` 上限；统一规范化为 `sum:n`、`sum:len:s`、`sum:n+m` 等主体。
- lint 对累计约束的题面缺失、Validator 缺失和确定数值不一致给出非阻断 warning；动态边界与“检测到多测和单组规模但未发现累计上限”保留结构化人工复核信息，不自动推导正确上限，也不把启发式结果当作证明。
- `probhub report` 的 JSON、终端和 Markdown 输出新增累计约束状态与 matched / statement-only / Validator-only / dynamic 计数；新能力仅进入 Workspace Schema v1，Legacy 保持冻结兼容。
- Agent Skill 新增多组数据上限推导契约：`T_max` 在 5 至 100000 间按同文件测试需求、逐组固定成本和 I/O 选择；高测试需求且满足复杂度条件时以 `sum(n_i) <= 10N` 为候选，再用联合最坏数据和三倍 TL 余量校准。题面累计上限未在 Validator 匹配、数值冲突或动态不可核对时阻断封题；即使自动匹配，也要求人工确认累加器宽度、初始化、逐组一次累计和最终拒绝逻辑。

## [0.6.3] - 2026-08-02

- Unix 内存限制 helper 改为隔离 Python 的内联 `-c` 启动，保留 `RLIMIT_AS`、状态管道、信号复位、session/process group 与 fail-closed 语义，同时避免 WSL 从 Windows 挂载目录逐次读取 helper 文件的冷启动开销。
- 发布包清单检查同时兼容 npm 10 的单元素数组与 npm 12 的单包名对象 JSON 响应，畸形或多包响应仍 fail closed。
- 主包 README、兼容包 README、Agent Skill 与 Release 安装说明统一为 Node.js 18+ / Python 3.10+ 的系统 Python 显式授权流程；非虚拟环境依赖只写入用户目录并兼容 Ubuntu PEP 668，pip 子进程清除 Python 环境污染；临时 `npx`、Doctor 修复和 WebUI 检查不再遗漏 `PROBHUB_ALLOW_SYSTEM_PYTHON=1`，安装器报错不再引导用户创建虚拟环境。
- 空工作区锁文件只在取得 OS 文件锁后初始化，消除 Windows 并发 generation 首次启动时的写入、刷新与关闭竞态。
- Build Manifest 升至 schema v4、试卷 generation 升至 schema v3，统一记录 ProbHub/Core、Typst、pypdf、模板与固定字体的 `builder_fingerprint`；`status` 提供字段级 stale 原因，旧 schema 和不可探测工具链不再误报 `current`，build/seal/generation 在发布前以 `builder_changed` 阻断身份漂移。Noto Sans CJK SC 与许可证改为随 npm 包发布，Typst 正式编译只使用校验后的包内字体。
- Python 运行时升级并锁定 Flask 3.1.3 与 pypdf 6.14.2；Windows/Ubuntu CI 使用固定 pip-audit 审计完整依赖闭包，临时例外必须绑定包名、原因、到期日和追踪链接。PDF 页数读取、文本边界扫描与切页迁入受 timeout、内存、输出和进程数限制的独立 worker，损坏或异常 PDF 不再无界占用构建进程或 WebUI 请求线程。
- 非交互 stdout/stderr 及 Checker feedback 改为原子共享单一输出预算，完成、超时、取消和资源超限均在进程树终止后执行确定性公平前缀截断；无法测量或截断时 fail closed，沙箱缓存 Schema 升至 6。stress schema 2 反例以 `E + min(E, 8 MiB)` 限制单次持久化，消除 `generator.out` 输入副本，记录逐文件预算与截断证据，并拒绝重放不完整的 Generator OLE 输入。
- WebUI 完整沙箱与上传评测增加最多 8 个请求线程的有界 HTTP 接入、前置 admission gate、共享固定 worker pool、有界队列、同题进程内互斥与跨进程 Judge 锁，新增结构化 `429 queue_full`、单调排队/执行/整任务 deadline、完整沙箱取消，以及日志、协议输出、结构化结果和完成记录上限；取消与协议错误状态保持单调，上传准备与清理纳入任务生命周期，超时、清理失败与服务关闭不再虚报成功并继续回收完整进程树。
- WebUI 沙箱改为检查已安装 Core 内的 `local_judge.py`，不再要求赛事仓库复制 Judge 运行时；点击运行会先通过当前写入队列完成题面保存，修复沙箱按钮被错误禁用以及旧 `_doSave` 调用导致任务无法入队的问题。
- Windows 受控执行会绕过虚拟环境 `python.exe` 重定向器并保留其依赖路径，修复 Microsoft Store Python 下真正解释器及后代未进入 Job Object、可能绕过资源限制与清理的问题。

## [0.6.2] - 2026-07-31

- WebUI 的关键 JavaScript、样式、数学排版资源与字体改为随 npm 包本地发布，移除运行时 CDN 依赖和 `unsafe-eval`，在断网环境中仍可保持原有界面与交互。
- PDF 切页改用稳定题目 ID marker，严格拒绝 marker 缺失、重复、乱序以及 Legacy 题名冲突，覆盖重复题名和超过 26 题的组卷边界。
- Unix 受限进程启动改用 fail-closed exec helper 设置资源限制，移除多线程环境中的 `preexec_fn`，降低并发评测和 WebUI 启动时的死锁风险。
- WebUI 文本资源使用规范化 LF 哈希、二进制资源保留原始 SHA-256，使资源完整性检查在 Windows 与 Linux clean install 中保持一致。

## [0.6.1] - 2026-07-31

- 新增已安装 Core 的 `probhub ui` 入口与依赖诊断；WebUI 的 HTML、CSS 和 JavaScript 拆分为包内静态资源，保留现有界面、主题、交互和 Schema v1 写入边界。
- 新增 standard、custom、float、interactive、stress 五个独立 Schema v1 Fixture，以及可复用的临时复制与故障注入辅助层；端到端覆盖 Validator 拒绝、Checker FAIL、OLE、进程上限和 stale-artifact 状态。
- README 重构为面向普通出题人与技术新手的使用指南：快速开始聚焦 Skill 安装、Agent 调用和验证模式选择，环境、WebUI、最小 CLI、交付标准与常见故障分别组织。
- Agent Skill 新增快速、普通、完整三种验证模式：快速模式固定完成 100 轮 stress；普通模式增加隔离上下文的盲审独立证明与 std；完整模式再增加独立证明/参考实现和对抗审查，并统一模式升级、证据记录与 `SEALED` 并行交接规则。

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
