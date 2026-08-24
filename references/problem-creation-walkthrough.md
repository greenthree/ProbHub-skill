# 从想法到交付：出题 Walkthrough

这份教程把当前 CLI 串成一条可执行主线。命令示例使用临时的 Workspace Schema v1 工作区；正式出题时把示例题面、代码和数据替换成自己的规范源。`probhub` 的精确参数以当前安装包的 `--help` 为准。

## 0. 开始前

先在一个空目录初始化工作区。Windows PowerShell 和 Bash 都可以使用同样的 CLI 命令；变量写法按当前 shell 调整。

```text
probhub init demo-contest --title "Demo Contest" --subtitle "正式赛"
cd demo-contest
probhub doctor
```

初始化只创建 `.probhub/workspace.yaml`、Typst 模板和必要的忽略规则。题面、代码、数据和配置是之后允许编辑的规范源；PDF、ZIP、Manifest、metadata、checkpoint 和 generation 都由 Core 生成。

## 1. Standard：先做一题并封存

### 1.1 设计和骨架

先写一份设计记录，再创建骨架。设计记录至少回答：目标算法和证明、复杂度、边界、已知错误思路、数据组职责，以及单组/多组累计限制。设计建议不是 Core 证据，不能替代后面的命令。

```text
probhub new A01 --name "Sum of Two" --judge standard
probhub lint A01
```

`new` 会创建 `A01/probhub.yaml`、`problem.md`、`code/`、`data/sample/` 和 `data/secret/`。把脚手架中的 A+B 示例替换为正式题目，并在配置中登记 accepted、brute、wrong、Validator、数据组和 recipes。所有代码路径都相对于 `A01/` 写成 `code/...`。

### 1.2 样例和可复现数据

样例答案先由首个 accepted 复现，再检查题面样例与配置一致。secret 数据优先用 `data.recipes` 生成；手工数据必须显式 `manual: true`。

```text
probhub sample-check A01 --no-cache
probhub gen A01
probhub gen A01 --apply
probhub lint A01
```

不带 `--apply` 的 `gen` 是只读计划；只有 `--apply` 会写入 `data/secret/`。生成后重跑 lint，确认 Validator 真正执行题面约束，特别是多组数据的累计上限。

### 1.3 Judge、stress 和独立审查

```text
probhub judge A01 --no-cache
probhub stress A01 --rounds 100 --seed 12345
```

Judge 成功必须同时满足退出码 `0` 和最终事件 `all_expectations_met`。stress 的 `counterexample` 要先 replay；有价值的反例修复后固化为正式数据，再从 lint 开始重跑。普通模式还要让隔离上下文的独立解题者给出参考实现，主 Agent 编译并交叉运行；完整模式再增加证明/错解审查和适用的 mutation。交互题不使用普通 stress。

### 1.4 Checkpoint、seal 和单题交接

完成当前题的门禁后发布不可变 draft，再生成 sealed revision：

```text
probhub checkpoint A01
probhub seal A01 --no-cache --seed 12345
probhub generation-status
```

`checkpoint` 记录当前规范源的 draft；`seal` 会重新执行 lint、Judge、已配置的 Judge QA 和 stress，记录 evidence，并在隔离快照中组装当前工作区 generation。若其他题尚未 checkpoint，结果会明确包含 `placeholder`、`missing` 或 `complete=false`，不能把它当作完整试卷交付；这不阻塞当前题任务结束，也不需要等待其他题。修改任何规范源都必须重新 checkpoint 和 seal。

### 1.5 全部题封存后的正式组卷

多个 Agent 可以各自完成并 seal 自己的题。等目标题目都具备有效 sealed revision 后，由一个任务执行一次多 ID 正式 build：

```text
probhub build A01 B02 C03 --no-cache
probhub --format text status A01 B02 C03
probhub verify-package A01.zip --require-pdf --problem A01
probhub verify-package B02.zip --require-pdf --problem B02
probhub verify-package C03.zip --require-pdf --problem C03
```

题序以 `workspace.yaml` 为准，命令行参数不会重排。正式 build 会一次生成全卷 PDF 和共享元数据，并为选中的题目生成 ZIP/Manifest；任一题未 sealed 或准备阶段失败时不得发布混合版本。最后逐页渲染 PDF，并确认 `status` 为 `current`。

## 2. Custom：增加 Checker 和 Judge QA

Custom 题的主线与 Standard 相同，但必须把“合法输出”和“正确性目标”写进 Checker，并为异常路径登记主动 fixture：

```text
probhub new B02 --name "Any Valid Answer" --judge custom
probhub lint B02
probhub judge B02 --no-cache
probhub judge-qa B02 --no-cache
probhub seal B02 --no-cache --seed 12345
```

编辑 `B02/code/checker.cpp` 后，在 `B02/judge-fixtures/` 放入候选输出，并在 `B02/probhub.yaml` 的现有 `judge` 下登记最小 QA 配置；其完整键名是 `judge.qa.schema_version: 1`：

```yaml
judge:
  type: custom
  validator: code/validator.cpp
  checker: code/checker.cpp
  qa:
    schema_version: 1
    robustness:
      baseline: accepts-alternative
      probes: [empty, truncated, extra-token, oversized]
    cases:
      - id: accepts-alternative
        purpose: valid-alternative
        case: sample/1
        contestant_output: judge-fixtures/checker/alternative.out
        expected: {status: AC}
      - id: rejects-wrong-answer
        purpose: wrong-answer
        case: sample/1
        contestant_output: judge-fixtures/checker/wrong.out
        expected: {status: WA}
```

`case` 引用已有 `data/sample/1.in` 与 `1.ans`；需要专用输入时，改为互斥的 `input` 和 `jury_answer` 路径。Checker fixture 至少覆盖合法替代输出和错误输出。Checker 自身 `_fail`、崩溃或清理故障是基础设施失败，不能在 `expected` 中声明为 WA。QA 通过后 evidence 必须是 `current`；自动鲁棒性探针仍需人工确认没有误放行。Judge QA fixture 不会进入正式 DOMjudge 包。

## 3. Interactive：协议先于代码

交互题先定义命令、参数范围、查询次数、刷新要求和终止规则，再实现 Interactor 与选手程序：

```text
probhub new C03 --name "Interactive Sum" --judge interactive
probhub lint C03
probhub judge C03 --no-cache
probhub judge-qa C03 --no-cache
probhub seal C03 --no-cache --seed 12345
```

编辑 `C03/code/interactor.cpp` 和 `C03/code/std.cpp`，在 `C03/code/judge-qa/normal.cpp` 实现一个题目特定的正常协议模拟选手，并在现有 `judge` 下登记：

```yaml
judge:
  type: interactive
  validator: code/validator.cpp
  interactor: code/interactor.cpp
  interactive:
    idle_limit: 0.5
    transcript_limit: 4096
  qa:
    schema_version: 1
    cases:
      - id: normal-protocol
        purpose: normal
        case: secret/edge
        contestant: {source: code/judge-qa/normal.cpp}
        expected: {status: AC}
      - id: early-eof-player
        purpose: early-eof
        case: sample/1
        contestant: {behavior: early-eof}
        expected: {status: WA}
      - id: idle-player
        purpose: idle
        case: sample/1
        contestant: {behavior: idle}
        expected: {status: TLE, timeout_kind: idle}
      - id: output-flood-player
        purpose: output-flood
        case: sample/1
        contestant: {behavior: output-flood}
        expected: {status: OLE}
```

Interactor 题不走普通 `gen` 或 `stress`；输入数据、交互 transcript 和 fixture 的职责要分别说明。内建行为只用于通用故障路径，题目特定协议必须由 `code/judge-qa/` 中的模拟选手覆盖。`judge-qa` 必须区分选手错误与 Interactor/进程控制基础设施错误。

## 4. 失败后从哪里重跑

| 现象 | 修复后最早重跑的门禁 |
|---|---|
| 题面、配置、代码或数据变更 | `lint`，然后回到 `sample-check`/`gen` |
| Validator 拒绝 | 修复 Validator/生成器/数据，重新生成后 `lint` |
| accepted、brute 或 Checker 结果不符 | 修复对应程序，`judge --no-cache` |
| stress 有反例 | `stress --replay latest`，修复后重新跑完整 stress |
| Judge QA 失败或 evidence 过期 | 修复 fixture/协议，`judge-qa --no-cache` |
| `sealed_revision_required` 或输入变化 | 重新 `checkpoint`、`seal`，不能手改 Manifest |
| 正式 build 失败 | 保留上一份正确产物，修复后等待锁释放并重新执行一次多题 build |

任何取消、超时、清理失败或基础设施异常都保持“未完成”，不能记录为通过。完整交付需要：所有目标题有效 sealed、正式 build 成功、`status=current`、ZIP 深度验证通过和 PDF 实际渲染 QA 完成。

## 5. 并行出题的结束条件

单题 Agent 的结束条件是自己的 sealed revision 有效，并记录 seal 返回的 generation 状态，不是其他题也完成。协调者只在全局静止窗口执行一次多题正式 build；正式 build 期间禁止修改任何已 sealed 的规范源。若任一题需要修改，撤销该题及全局 build 准备状态，修复后重新 seal，再重新进入多题 build。
