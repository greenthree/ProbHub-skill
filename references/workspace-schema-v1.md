# ProbHub Workspace Schema v1

## Workspace file

Path: `.probhub/workspace.yaml`

```yaml
schema_version: 1
contest:
  title: Contest title
  subtitle: 正式赛
  author: Team
  date: 2026年6月26日
typst:
  directory: typst-statement/正式赛
  creation_timestamp: 1782403200
problems:
  - id: L01
    directory: L01
lint:
  forbidden_patterns: [TODO, FIXME, 114514, 待补充]
```

The order of `problems` is the official contest order. `id` is stable and does not change when the displayed letter changes.

## Problem file

Path: `<problem>/probhub.yaml`

```yaml
schema_version: 1
id: L01
name: 数字重构
display_name: 数字重构
difficulty: 3
tags: [优先队列, 模拟, 贪心]
limits:
  time: 1
  memory: 256
  output: 64
statement:
  source: problem.md
judge:
  type: standard
  validator: code/validator.cpp
solutions:
  accepted: [code/std.cpp]
  brute: [code/brute.cpp]
  wrong: [code/wrong_greedy.cpp]
generators: [code/inmaker.cpp]
data:
  sample_dir: data/sample
  secret_dir: data/secret
domjudge:
  include_pdf: true
```

## Problem directory layout

All problem-local C++ sources and locally compiled executables live under `code/`. Paths in `probhub.yaml` are relative to the problem directory.

```text
<problem>/
├── probhub.yaml
├── problem.md
├── code/
│   ├── std.cpp
│   ├── brute.cpp
│   ├── wrong*.cpp
│   ├── validator.cpp
│   ├── inmaker.cpp
│   └── *.exe              # local build output, ignored by Git
├── data/
│   ├── sample/
│   └── secret/
└── .probhub/
    └── build-manifest.json
```

`checker.cpp`, `interactor.cpp`, auxiliary solutions, and diagnostic C++ programs also belong in `code/`. Generated DOMjudge validator files remain under `output_validators/` because that directory is part of the package format rather than the source-code workspace.

## Statement file

`problem.md` must contain:

```markdown
# Problem title

## 题目描述
...

## 输入格式
...

## 输出格式
...

## 提示
... optional ...
```

Samples are not duplicated in Markdown. `data/sample/*.in` and matching `.ans` files are their only source.

## Generated artifacts

The Core generates and may overwrite:

- `<problem>/meta.json`
- Typst `problems.json`
- `<problem>/problem.yaml`
- `<problem>/domjudge-problem.ini`
- `<problem>/problem.pdf`
- Full contest PDF
- `<id>.zip`
- `<problem>/.probhub/build-manifest.json`

Do not edit generated artifacts to make source changes.
