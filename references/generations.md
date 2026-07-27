# Immutable checkpoints and exam generations

本功能用于并行出题期间的试卷预览，不替代正式 `build` 和 DOMjudge 发布。

## 1. 两类不可变对象

题目 checkpoint 保存某一时刻的题目规范源副本：

```text
.probhub/checkpoints/<problem-key>/<revision-id>/
├── problem/
└── revision.json
```

checkpoint 只复制 Schema v1 规范源，排除 PDF、ZIP、Manifest、metadata、编译产物、沙箱缓存和 stress 反例。`revision-id` 绑定题目 source/data hash、状态和验证证据；发布后不得手工修改。

试卷 generation 保存一组 checkpoint 组成的完整试卷：

```text
.probhub/generations/<generation-id>/
├── main.pdf
├── manifest.json
└── problems/
    └── <ID>.pdf
```

generation 使用内容寻址 ID，不覆盖正式 Typst `main.pdf`、单题 `problem.pdf`、ZIP 或 Build Manifest。相同工作区配置和 checkpoint 集合直接复用已有 generation。

## 2. 命令

开发过程中发布一个可供其他任务组卷的草稿：

```powershell
probhub checkpoint L10
```

完成题目后验证并冻结当前 revision：

```powershell
probhub seal L10 --no-cache --seed 12345
```

`seal` 执行所选题目的 lint、judge，以及配置存在时的 stress。成功后写入带结构化证据的 sealed checkpoint，并自动组装一份完整试卷 generation。

单独组装或查看当前 generation：

```powershell
probhub assemble
probhub generation-status
```

## 3. 并行语义

- 组装只读取各题最后发布的 checkpoint，不读取其他 Agent 正在编辑的 live 文件。
- 没有 checkpoint 的题目会先尝试生成稳定 draft checkpoint；题目源缺失或损坏时使用明确的“开发中”占位页，并把该题连同原因列入结果与 manifest 的 `missing` 字段。
- 若某题没有任何 checkpoint 且其 checkpoint 锁被并发 `seal`/`checkpoint` 持有，组装会在有限预算内重试；仍然锁忙时以 `checkpoint_busy` 显式失败，不会把存在的题目静默替换为占位页。等待并发操作结束后重新运行 `assemble` 即可。
- generation 使用独立 `.probhub/generation.lock`，不会占用正式 `build.lock`，也不会修改正式发布产物。
- 同时到达的组装请求会等待当前 generation 完成，然后根据最新 checkpoint 集合生成或复用对应版本。
- 修改 live 题目不会改变旧 checkpoint 或旧 generation；必须再次运行 `checkpoint` 或 `seal` 才会进入新试卷版本。

## 4. 状态边界

- `draft`：所有槽位都有 checkpoint，但至少一题尚未 sealed。
- `placeholder`：该槽位没有可用 checkpoint，试卷仍可生成但 `complete=false`，且 `missing` 列出每个占位题目与原因。
- `sealed-preview`：所有题目 checkpoint 均为 sealed，但仍属于预览 generation，不是正式发布物。

消费 `assemble`/`seal` 结果时不得只看 `ok`：交付前必须确认 `complete=true` 或逐项核对 `missing`。

只有正式 `build` 才会生成或替换 DOMjudge ZIP、正式单题 PDF、共享 metadata 和 Build Manifest。不要把 `.probhub/generations/` 中的预览直接作为正式包发布。

正式 `build` 会要求工作区 collection 中每道题的最新 checkpoint 都是与当前 live source/data 一致的 sealed revision，并在 Manifest v3 中记录所选题目的 `sealed_revision_id`。任一题仍是 draft、缺少 checkpoint 或 seal 后又被修改时，整批构建会在创建快照前以 `sealed_revision_required` 拒绝；构建期间 revision 变化则以 `sealed_revision_changed` 拒绝发布。

## 5. 当前性能模型

当前第一阶段仍会为每个新 generation 编译一次完整 Typst 集合，但不会重复 judge 或 stress。下一阶段可把题目正文渲染为独立内容寻址 PDF 分片，再由轻量 assembler 合并并覆盖全局页码，从而只重渲染发生变化的题目。
