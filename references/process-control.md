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

所有超时、超限、异常和正常退出路径都会回收直接进程并清理后代。若父进程创建后台子进程后先正常退出，ProbHub 仍会终止遗留后代，避免污染下一测试点、占用文件或持续消耗 CPU。

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

## 4. 输出限制与截断

非交互运行将 stdout 和 stderr 直接写入临时文件，不使用无界 `capture_output`。监控器定期检查文件总大小：

1. 大于 `limits.output` 后立即判定输出超限；
2. 终止完整进程树；
3. 把捕获文件截断到配置预算以内；
4. 选手程序返回 `OLE`，官方工具返回基础设施失败。

共享进程控制会在截断前记录 stdout 与 stderr 的总字节数为 `output_bytes`。该值用于 OLE 校准下界；正式保存文件仍被截断到预算内，不能用截断后的文件大小代替实际观测值。

进程可能在一次轮询间隔内多写少量内容，因此判定依据是“观测到超限”，而最终保存文件会被截断。快速大量输出并立即退出也会在结束后的最终检查中被识别，不能绕过 OLE。

交互题同时限制选手到 Interactor 的通信流量以及 stderr；选手超限为 `OLE`，Interactor 自身输出洪泛为 `FAIL`。Transcript 仍受 `judge.interactive.transcript_limit` 单独限制，两个通信方向原子共享这份原始字节预算，并分别使用 UTF-8 增量解码保留跨读取块字符；Transcript 截断不等于 OLE。

编译器、Validator、Checker 和其他官方工具使用内部诊断输出上限，避免错误工具耗尽内存或磁盘；内部上限不改变 DOMjudge 正式题目限制。

## 5. Windows

Windows 使用 Job Object：

- `KILL_ON_JOB_CLOSE` 保证关闭 Job 时终止整棵进程树；
- Job 级内存限制约束进程树总内存；
- Active Process Limit 实施 `limits.processes`；
- 可查询 Job 峰值内存用于诊断和 MLE 判断。

若 Job Object 创建、配置或分配失败，ProbHub 会 **fail closed**：立即结束刚启动的进程并返回基础设施错误，不会退化成只能杀直接父进程的无保护执行。

## 6. Linux 与其他 Unix

Linux/Unix 使用：

- 独立 session/process group；
- 由独立 exec helper 在子进程内设置 `RLIMIT_AS`，随后以 `exec` 原位替换为目标程序；
- Linux `/proc` 低频采样整棵进程树的 RSS 和进程数；
- `killpg` 在结束时清理进程组。

Flask 多线程请求和 CLI 都不会在父进程中使用 Python `preexec_fn`。helper 会通过仅在成功 `exec` 时关闭的状态管道报告参数、`setrlimit` 或 `exec` 失败；启动阶段超时同样 fail closed，不会退化成无内存限制执行。

资源采样约每 `50 ms` 进行一次，时间和输出检查使用更短轮询，以降低大量短进程和 stress 场景的监控开销。Linux 的 `RLIMIT_AS` 是每进程地址空间限制，和 Windows Job 的整树共享内存配额并不完全等价；ProbHub 同时使用 `/proc` 聚合 RSS 做补充监控。无法使用 `/proc` 时，进程组清理与 `RLIMIT_AS` 仍然有效，但进程数和聚合内存遥测能力会受限。

## 7. Checker、Validator、编译器和 Interactor

官方工具不能把自己的资源故障伪装成选手 WA：

- Checker 的正常协议退出码继续映射为 AC/WA；超时、OLE、MLE、进程数超限或异常退出为 `FAIL`。
- Validator 被拒绝或运行失败会中止题目评测。
- 编译器超时、输出超限或异常会产生结构化编译失败。
- Interactor 的协议结果与选手结果分离；Interactor 自身超限为 `FAIL`。

这一区分对出题自检很重要：官方工具失败必须修复题目基础设施，不能作为错解“被击杀”的证据。

## 8. Stress

`probhub stress` 的每个阶段都使用相同底层控制：

```text
Generator → Validator → accepted → brute → Checker/standard compare
```

- accepted/brute 的 OLE、MLE、TLE 或 RE 作为 `counterexample` 保存；
- Generator、Validator、Checker 或编译器的资源错误作为 `infrastructure` 保存；
- 反例中的 stdout/stderr 已受上限保护并可能被截断；
- Replay 重新运行当前程序时继续应用当前资源限制。

Stress 不读取或写入普通沙箱缓存。完整反例和 replay 协议见 `references/stress.md`。

## 9. 缓存

逐点缓存键包含时间、内存、输出、进程数、平台和沙箱策略。进程控制或资源状态语义变化时会提升缓存 Schema，因此旧版本缓存不会继续返回缺少 OLE 或旧进程树行为的结果。

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
2. 每次提交生成唯一 task ID，源码写入 `.probhub/submissions/<task-id>/problem/code/submission.cpp`；
3. 临时配置只保留上传解法，并以绝对只读路径引用题目现有测试数据；
4. Custom Checker 或 Interactor 源码会复制到临时工作区后编译，原题 `code/` 不产生新的 `.exe`；
5. 评测复用 `local_judge.py` 与共享进程控制，返回 CE/AC/WA/TLE/MLE/OLE/RE/FAIL 和逐测试点信息；
6. 任务由后台线程监督独立子进程，HTTP 请求不会同步阻塞到评测结束；
7. 排队中和运行中的任务均可取消：先创建取消标记并通知监督进程，3 秒后仍未退出则强制终止已知进程组与后代进程树；
8. `local_judge.py --cancellable` 会在编译、普通运行和交互循环中检查取消标记，交互题取消时同时清理选手与 Interactor；
9. 任务结束或取消后删除源码、临时配置、可执行文件、输出和缓存；启动服务及接受新任务时清理超过 24 小时的 UUID 遗留目录。

遗留清理只处理 `.probhub/submissions/` 内名称严格为 32 位小写十六进制的非符号链接目录，并跳过活动任务、陌生名称和符号链接。上传流程不得覆盖、重命名或修改题目原有 `code/std.cpp`、`code/brute.cpp`、`code/wrong*.cpp`、Checker、Interactor、数据、答案或生成物。任务字典只保存文件名、结构化结果和日志，不保存源码正文。
