# ProbHub Process and Resource Control

本文说明 ProbHub 本地沙箱的共享进程控制、资源限制、状态映射和跨平台行为。实现入口是 `probhub/process_control.py`，供普通题、Checker、Validator、编译器、Interactor 与 `probhub stress` 共同使用。

## 1. 题目配置

在 `<problem>/probhub.yaml` 中配置：

```yaml
limits:
  time: 1       # 秒
  memory: 256   # MiB
  output: 64    # MiB，默认 64
  processes: 32 # 整棵进程树，默认 32
```

- `time`：选手程序的 wall-clock 时间限制。
- `memory`：选手程序的内存限制。
- `output`：一次选手程序运行捕获的 stdout 与 stderr 总预算；必须为正整数 MiB。
- `processes`：受控进程树允许的最大进程数；必须为正整数，主进程计入数量。

未写 `output` 或 `processes` 时分别使用 `64 MiB` 和 `32`。`probhub lint` 会拒绝布尔值、零、负数和不符合类型的配置。

## 2. 覆盖范围

以下角色都使用完整进程树控制：

- 普通 `standard` 题的选手程序；
- `custom` 题的选手程序和 Checker；
- `interactive` 题的选手程序和 Interactor；
- Validator；
- C++ 编译器及其后代；
- stress 的 Generator、Validator、accepted、brute、Checker 和编译器。
- Core 与 WebUI 的 pypdf 页数读取、边界扫描和切页 worker。
- mutation 的 Tree-sitter C++ native parser worker。

所有超时、超限、异常和正常退出路径都会回收直接进程并进入同一后代清理闭环。Windows 由 Job Object 覆盖关联进程；受支持 Linux 上，原进程组由 `killpg` 清理，资源采样期间已经观察到的脱离后代还会按 PID 与内核启动时间复核后单独清理。若父进程创建后台子进程后先正常退出，只要后代仍在原进程组或已经被采样观察，ProbHub 仍会终止它，避免污染下一测试点、占用文件或持续消耗 CPU。

官方工具与选手程序使用同一底层控制，但结果语义不同：选手超限属于提交结果；Checker、Validator、Interactor、Generator 或编译器超限属于题目基础设施错误。

## 3. 状态语义

| 状态 | 含义 | 常见原因 |
|---|---|---|
| `AC` | 正常完成且答案被接受 | 输出匹配或 Checker/Interactor 接受 |
| `WA` | 答案错误 | standard 不匹配或 Checker 拒绝 |
| `TLE` | 超过 wall-clock 总时限或交互空闲时限 | 死循环、复杂度过高、协议停滞 |
| `MLE` | 超过内存限制 | 申请过大内存或进程树内存过高 |
| `OLE` | 选手 stdout/stderr 或交互输出超过预算 | 无限打印、调试日志过多、输出规模错误 |
| `RE` | 运行时错误 | 非零退出、信号、进程数超限或启动错误 |
| `FAIL` | 官方评测基础设施失败 | Checker/Validator/Interactor/Generator/编译器超限、异常或协议错误 |

进程数超限当前映射为选手 `RE`，并在消息中报告 `process limit exceeded`。官方工具发生同类错误时映射为 `FAIL` 或 stress 的 `infrastructure`。

若进程树清理无法确认完成，底层返回稳定原因 `process_cleanup_failed`，并保留清理前原因用于诊断。它始终属于 supervisor/题目基础设施失败，会覆盖原先候选的 AC、WA、TLE、MLE、OLE 或 RE；不得把无法确认回收的运行写入普通 verdict 或缓存为可信结果。诊断只保留有界数量的 PID 与错误。

## 4. 输出限制与截断

非交互运行将 stdout 和 stderr 直接写入临时文件，不使用无界 `capture_output`。监控器定期检查文件总大小：

1. 大于 `limits.output` 后立即判定输出超限；
2. 终止完整进程树；
3. 再次测量终止期间写入的字节，并让全部受控输出路径原子共享同一配置预算；
4. 按稳定路径顺序公平保留各文件前缀，短流未用配额转移给长流，奇数字节由前一条路径优先；
5. 选手程序返回 `OLE`，官方工具返回基础设施失败。

默认路径顺序为 stdout、stderr；Checker feedback 等显式受控诊断路径排在其后。`output_bytes` 记录进程树终止后、截断前的总观测字节数，`retained_output_bytes` 记录实际保留总量，`stdout_retained_bytes`、`stderr_retained_bytes` 与 `output_truncated` 公开前两条流的保留量和是否发生截断。`output_bytes` 继续用于 OLE 校准下界，不能用截断后的文件大小代替。

完成、超时、取消、资源超限和快速退出都在完整进程树终止后执行相同的共享截断；原有终止原因不会被后到的输出覆盖。进程可能在一次轮询间隔内多写少量内容，因此判定依据是“观测到超限”，而最终保存文件仍不超过预算。捕获路径不是普通文件、无法测量或无法截断时 fail closed，作为沙箱基础设施失败，不会把失去约束的执行误报为选手 `RE` 或正常结果。

交互题使用流式泵送而不是非交互捕获文件。选手到 Interactor 的协议字节与选手 stderr 共享题目执行输出预算，超限为 `OLE`；Interactor 协议输出使用同额官方工具预算，Interactor stderr 与 feedback 使用独立诊断预算 `min(limits.output, 8 MiB)`，超限为 `FAIL`。进入事件和缓存的诊断文本只读取有界 UTF-8 前缀。Transcript 仍受 `judge.interactive.transcript_limit` 单独限制，两个通信方向原子共享这份原始字节预算，并分别使用 UTF-8 增量解码保留跨读取块字符；Transcript 截断不等于 OLE。

编译器、Validator、Checker 和其他官方工具使用内部诊断输出上限，Checker 的 stdout、stderr、`judgemessage.txt` 与 `teammessage.txt` 纳入同一受控执行预算，避免 feedback 文件绕过限制。内部上限不改变 DOMjudge 正式题目限制。

## 5. Windows

Windows 使用 Job Object：

- `KILL_ON_JOB_CLOSE` 保证关闭 Job 时终止整棵进程树；
- Job 级内存限制约束进程树总内存；
- Active Process Limit 实施 `limits.processes`；
- 可查询 Job 峰值内存用于诊断和 MLE 判断。

Windows 虚拟环境的 `python.exe` 可能是一个重定向启动器，尤其是 Microsoft Store Python。ProbHub 会在受控执行时直接启动对应的 base 解释器，并显式保留虚拟环境依赖路径，避免 Job 只约束启动器而真正的 Python 进程和后代逃逸。

若 Job Object 创建、配置或分配失败，ProbHub 会 **fail closed**：立即结束刚启动的进程并返回基础设施错误，不会退化成只能杀直接父进程的无保护执行。

## 6. Linux 与其他 Unix

Linux/Unix 使用：

- 独立 session/process group；
- 由隔离 Python 启动的内联 exec helper 在子进程内设置 `RLIMIT_AS`，随后以 `exec` 原位替换为目标程序；
- Linux `/proc` 低频采样整棵进程树的 RSS 和进程数；
- `killpg` 在结束时清理进程组。
- 采样时有界记录最多 4096 个后代的 PID 与 `/proc/<pid>/stat` 启动时间；清理原进程组后，只对启动时间仍匹配的已观察脱离后代发信号，支持时使用 pidfd 缩小 PID 复用竞态；
- 清理前会从仍匹配的已观察后代再扩展一次当前子树，随后复核它们已经退出。PID 已复用时不会向新进程发信号，`/proc` 信息不可读取、跟踪上限溢出或匹配后代仍存活时 fail closed 为 `process_cleanup_failed`。

Flask 多线程请求和 CLI 都不会在父进程中使用 Python `preexec_fn`。helper 代码通过 `python -I -S -c` 传入，避免 WSL 在 Windows 挂载目录中为每个短进程重新打开 helper 脚本；它仍通过仅在成功 `exec` 时关闭的状态管道报告参数、`setrlimit` 或 `exec` 失败。启动阶段超时同样 fail closed，不会退化成无内存限制执行。

资源采样约每 `50 ms` 进行一次，时间和输出检查使用更短轮询，以降低大量短进程和 stress 场景的监控开销。Linux 的 `RLIMIT_AS` 是每进程地址空间限制，和 Windows Job 的整树共享内存配额并不完全等价；ProbHub 同时使用 `/proc` 聚合 RSS 做补充监控。无法使用 `/proc` 时，进程组清理与 `RLIMIT_AS` 仍然有效，但进程数、聚合内存遥测和脱离后代身份跟踪能力会受限；若一个先前已记录的身份在清理时变得不可核对，该次运行按基础设施失败处理。

这项保证的准确边界是“受支持 Ubuntu/Linux 上，在父子关系仍可见时被采样观察到的后代”。一个敌意程序若在约 `50 ms` 采样间隔内快速双 fork、重新建立 session，并在被观察前让中间父进程退出，可能不再能从 `/proc` 父子关系追溯。ProbHub 不使用 cgroup、PID namespace 或外部 watchdog，因此这不是可安全执行任意敌意代码的强容器；需要该安全等级时应在独立容器或专用评测机运行。

## 7. Checker、Validator、编译器和 Interactor

官方工具不能把自己的资源故障伪装成选手 WA：

- Checker 的正常协议退出码继续映射为 AC/WA；超时、OLE、MLE、进程数超限或异常退出为 `FAIL`。
- Validator 被拒绝或运行失败会中止题目评测。
- 编译器超时、输出超限或异常会产生结构化编译失败。
- Interactor 的协议结果与选手结果分离；Interactor 自身超限为 `FAIL`。

这一区分对出题自检很重要：官方工具失败必须修复题目基础设施，不能作为错解“被击杀”的证据。

PDF 解析不会在 CLI 主构建进程或 Flask 请求线程内直接运行。Core 通过独立 Python worker 调用固定版本 pypdf，默认限制为 30 秒、512 MiB、1 MiB stdout/stderr 共享预算和 4 个进程；WebUI 页数检查使用 10 秒 deadline，页面渲染继续由受控 Poppler 进程完成。worker 超时、内存/输出/进程超限、损坏 JSON、链接输入、畸形 PDF 或缺失切页输出统一返回 `pdf_processing_failed`；正式 build/typeset 仍在 staging 中处理，失败不会覆盖最后正确产物。

mutation 的 Tree-sitter native binding 同样不进入 CLI 主进程。Core 把 accepted 源码复制到临时请求目录，通过独立 Python worker 返回版本化位置 JSON，默认限制为 30 秒、512 MiB、4 MiB stdout/stderr 共享预算和 8 个进程。父进程严格校验协议版本、解析器版本、源码哈希、位置和数量；超时、取消、native 崩溃、资源超限和畸形响应均清理完整进程树并结构化失败，不覆盖最后成功的 mutation evidence。

这层隔离用于避免损坏 PDF 无界占用本地出题流程，不是面向任意敌意文件的强安全容器。正式运行时依赖闭包由 `requirements.txt` 逐项锁定，仓库 CI 在 Windows 与 Ubuntu 上按精确版本审计，并每周自动复查一次。

## 8. Stress

`probhub stress` 的每个阶段都使用相同底层控制：

```text
Generator → Validator → accepted → brute → Checker/standard compare
```

- accepted/brute 的 OLE、MLE、TLE 或 RE 作为 `counterexample` 保存；
- Generator、Validator、Checker 或编译器的资源错误作为 `infrastructure` 保存；
- 反例中的 stdout/stderr 已受共享上限保护并可能被截断，单个反例另有明确总持久化预算；
- Replay 重新运行当前程序时继续应用当前资源限制。

Stress 不读取或写入普通沙箱缓存。完整反例和 replay 协议见 `references/stress.md`。

## 9. 缓存

逐点缓存键包含时间、内存、输出、进程数、平台和沙箱策略。可观测脱离后代清理与 `process_cleanup_failed` 语义加入后缓存 Schema 为 8；进程控制或资源状态语义变化时会继续提升 Schema，因此旧版本缓存不会返回缺少 OLE、旧 feedback 或旧进程树行为的结果。

要强制完整执行并刷新缓存：

```powershell
probhub judge L01 --no-cache
probhub build L01 --no-cache
```

缓存文件仍是 `<problem>/.probhub/sandbox-cache-v1.json`；文件名中的 `v1` 是本地存储名称，不代表内部缓存 Schema 永远不变。

期望 TLE 的校准探针仍使用同一进程树控制：正常判定先在正式 TL 处结束；随后只对一个已命中的目标用例以更长监督上限重新运行，记录精确完成时间或超时下界。探针不改变原 verdict，不运行 Checker；因此正常退出只表示进程完成，不表示答案 AC。交互题当前不做这种脱离 Interactor 的探针。

## 10. WebUI 临时提交评测

WebUI“沙箱评测”页支持上传单个 UTF-8 `.cpp` 并只评测该提交：

1. 请求限制源码扩展名、UTF-8 编码和 `1 MiB` 大小；
2. 本地 WSGI 服务最多同时使用 8 个请求线程；完整沙箱与上传评测在读取题目或上传正文前先经过非阻塞 admission gate，随后共用固定 worker pool；默认 worker 数为 `min(4, CPU)`，等待队列最多 16 项，不会按请求无限创建 HTTP 或评测线程，也不会无界保留源码；
3. admission 或队列饱和返回 HTTP `429`、`code: queue_full`、`retryable: true` 与 `retry_after`，被拒绝的上传不创建任务目录；
4. WebUI 进程内先按题目目录互斥，`local_judge.py` 再取得 `<problem>/.probhub/judge.lock` 的 OS 排他锁；因此同题的 CLI、多个 WebUI 进程与完整沙箱不会并发改写编译产物和缓存，不同题目仍可占用不同 worker 并行运行，上传提交始终使用各自的隔离目录；
5. 每次提交生成唯一 task ID，真正开始执行后才把源码写入 `.probhub/submissions/<task-id>/problem/code/submission.cpp`；
6. 临时配置只保留上传解法，并以绝对只读路径引用题目现有测试数据；
7. Custom Checker 或 Interactor 源码会复制到临时工作区后编译，原题 `code/` 不产生新的 `.exe`；
8. 评测复用 `local_judge.py` 与共享进程控制，返回 CE/AC/WA/TLE/MLE/OLE/RE/FAIL 和逐测试点信息；
9. 默认排队 deadline 为 5 分钟、单次执行 deadline 为 30 分钟、从接收到结束的整任务 deadline 为 35 分钟；源码准备、等待同题互斥锁、Judge 执行和清理都属于任务生命周期，超时返回稳定 `queue_timeout` 或 `task_deadline_exceeded`；
10. 排队中和运行中的完整沙箱、上传任务均可取消；取消状态单调且优先于同时到达的成功终态，运行任务先通过独立取消标记协作停止，3 秒后仍未退出则强制终止已知进程组与后代进程树；
11. `local_judge.py --cancellable` 会在编译、普通运行和交互循环中检查取消标记，交互题取消时同时清理选手与 Interactor；
12. 每项任务的可见日志最多保留 `256 KiB`，结构化明细事件原始预算为 `2 MiB`，最终事件另限 `64 KiB`；WebUI 使用固定 drainer 线程从 PIPE 持续读取 Judge JSONL，最多保留 `16 MiB` 前缀，超额数据继续排空并终止完整进程树，不会先写入无界临时文件；截断会明确标记，不改变题目 `limits.output` 的 OLE 语义；
13. 完整沙箱和上传评测分别最多保留 32 条完成记录，TTL 为 1 小时；新任务、状态读取和任务结束时清理过期记录，任务结果不是持久审计日志；
14. 上传任务结束、取消或超时后删除源码、临时配置、可执行文件、输出和缓存；删除失败返回 `submission_cleanup_failed` 与 `workspace_cleaned: false`，不得宣称已经清理；启动服务及接受新任务时清理超过 24 小时的 UUID 遗留目录。

遗留清理只处理 `.probhub/submissions/` 内名称严格为 32 位小写十六进制的非符号链接目录，并跳过活动任务、陌生名称和符号链接。上传流程不得覆盖、重命名或修改题目原有 `code/std.cpp`、`code/brute.cpp`、`code/wrong*.cpp`、Checker、Interactor、数据、答案或生成物。任务字典只保存文件名、结构化结果和日志，不保存源码正文。
