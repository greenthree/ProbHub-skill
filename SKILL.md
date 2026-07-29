---
name: probhub
description: 当用户需要创作或维护算法竞赛题目、生成测试数据、运行 ProbHub CLI 受控沙箱或 stress 差分测试、配置进程与输出限制、使用 Workspace Schema v1、配置 DOMjudge 题目包或使用 Typst 组卷时调用。覆盖从题面和代码矩阵到 PDF、ZIP、Manifest 与状态验证的完整流程。
---

# 角色

作为严谨的 ACM/ICPC 出题人执行任务。熟悉 testlib.h、C++、Python/CYaRon、DOMjudge、Typst 和 ProbHub Core。主动读写文件、编译、运行和修复；不要只给用户命令让其代为执行。

# 1. 判断工作区模式

1. 从当前目录向上查找 `.probhub/workspace.yaml`。
2. 找到时，必须使用 **Workspace Schema v1** 和 `probhub` CLI。
3. 创建、迁移、审查或修改 Schema v1 工作区时，必须先读取 `references/workspace-schema-v1.md`，再修改 `.probhub/workspace.yaml`、`probhub.yaml` 或目录结构。
4. 未找到时，才读取并执行 `references/legacy-workflow.md`。
5. 不得混用两种模式：Schema v1 禁止手工维护 Legacy 元数据或构建产物。

# 2. Schema v1 的事实来源

| 内容 | 唯一事实来源 |
|---|---|
| 赛事信息、Typst 集合、稳定题序 | `.probhub/workspace.yaml` |
| 题目 ID、题名、限制、代码矩阵、数据路径 | `<题目>/probhub.yaml` |
| 描述、输入、输出、提示 | `<题目>/problem.md` |
| 题面图片等媒体资源 | `<题目>/assets/` 或题目目录内的常见图片文件 |
| 样例 | `<题目>/data/sample/*.in` 与 `.ans` |
| 隐藏数据 | `<题目>/data/secret/*.in` 与 `.ans` |
| C++ 源码和本地可执行文件 | `<题目>/code/` |

代码路径在 `probhub.yaml` 中必须写成相对题目目录的 `code/...`。选择题目时使用稳定 ID（如 `L01`），不要使用由题序推导的显示字母（如 `A`）。

以下均为 Core 生成物，禁止手工修改：

- `meta.json`
- Typst `problems.json`
- `problem.yaml`、`domjudge-problem.ini`
- `problem.pdf`、全卷 PDF、`<ID>.zip`
- `.probhub/build-manifest.json`

`.probhub/build.lock` 与 `.probhub/generation.lock` 是可保留的 OS 文件锁载体，不能用“文件是否存在”判断是否占用；`.probhub/sandbox-cache-v1.json` 是本地缓存，`.probhub/stress/` 保存可重放差分反例，`.probhub/checkpoints/` 与 `.probhub/generations/` 保存并行出题期间的不可变本地版本。这些路径都应被 Git 忽略，禁止提交或手工维护。

`<problem>/.probhub/judge-evidence-v2.json` 是最近一次完整成功 Judge 的本地校准证据，`judge-evidence.lock` 是其 OS 发布锁。lint/status 先按 schema、source/data hash 与平台判断 evidence 是否过期，再验证测量策略、结构和每个解法的运行域；失败 Judge 不覆盖上一份成功证据，较旧的 build 快照也不能覆盖较新的本地测量。它们不进入 Manifest 或 ZIP，也不得提交；旧 `judge-evidence-v1.json` 继续被 Git 忽略，但不会作为当前证据读取。

# 3. CLI 操作规则

## 3.1 入口和工作区定位

在工作区根目录或其任意子目录中运行：

```powershell
probhub <command>
```

若 CLI 不在 PATH，按顺序回退：

```powershell
python scripts/probhub.py <command>
python <Skill目录>/scripts/probhub.py --workspace <工作区> <command>
```

在工作区外运行时，全局选项必须放在子命令前：

```powershell
probhub --workspace <工作区路径> build L01
```

## 3.2 单题、多题和全工作区

```powershell
# 单题
probhub lint L01
probhub judge L01
probhub build L01
probhub status L01

# 多题
probhub build L01 L03

# 不写 ID 表示全部题目
probhub build
```

常用命令职责：

| 命令 | 作用 |
|---|---|
| `doctor` | 检查 Python、Node、Typst、g++ 和依赖 |
| `new <ID>` | 创建可编译、judge 开箱即过的题目骨架（`--judge` 可选 standard/custom/interactive），含带独立性声明的双 accepted、示例错解与定向数据组 |
| `gen <ID>` | 按 `data.recipes` 配方生成/校验 secret 数据；默认 plan 只报告，`--apply` 才写入，失败零写入 |
| `lint [ID...]` | 检查规范源文件、代码路径和数据配对 |
| `status [ID...]` | 报告 `current`、`stale`、`never-built` |
| `report [ID...]` | 只读汇总难度、数据画像、recipe、TL 余量和错解击杀矩阵；`--format markdown` 输出 Markdown |
| `sample-check [ID...]` | 只运行样例与首个 accepted，严格核对 `.ans`；不发布 Judge 校准 evidence |
| `judge [ID...]` | 编译并运行 Validator、accepted、brute、wrong |
| `stress ID...` | 反复生成小数据，对拍 accepted 与 brute，保存首个可重放反例；`--against <解法>` 反向找刀，`--fixate <case>` 把命中一步固化为 secret 数据 + 配方 + 定向数据组 |
| `checkpoint ID` | 发布当前题目的不可变 draft checkpoint，供并行组卷使用 |
| `seal ID` | lint、judge、stress 后冻结 revision，并自动生成一版完整试卷 |
| `assemble` | 使用各题最新 checkpoint 生成隔离的完整试卷 generation |
| `generation-status` | 校验并报告当前预览 generation |
| `typeset [ID...]` | 编译全卷并提取指定单题 PDF |
| `package [ID...]` | …55960 tokens truncated… root / problem_id
            paths.extend((
                problem_dir / "meta.json",
                problem_dir / "problem.pdf",
                problem_dir / "problem.yaml",
                problem_dir / "domjudge-problem.ini",
                problem_dir / ".probhub/build-manifest.json",
                root / f"{problem_id}.zip",
            ))
        before = {}
        for index, path in enumerate(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"old-{index}-{path.name}".encode("utf-8")
            path.write_bytes(payload)
            before[path] = payload
        return before

    def fake_compile(self, root, workspace, loaded):
        for problem_dir, config in loaded:
            (problem_dir / "meta.json").write_text(
                f'{{"id":"{config["id"]}"}}\n',
                encoding="utf-8",
            )
        typst_dir = root / "typst/contest"
        (typst_dir / "problems.json").write_text("[]\n", encoding="utf-8")
        main_pdf = typst_dir / "main.pdf"
        main_pdf.write_bytes(b"new main pdf")
        return typst_dir, main_pdf, []

    def fake_extract(self, main_pdf, loaded, only_ids=None):
        outputs = {}
        for problem_dir, config in loaded:
            problem_id = config["id"]
            if only_ids and problem_id not in only_ids:
                continue
            output = problem_dir / "problem.pdf"
            output.write_bytes(b"new problem pdf-" + problem_id.encode("ascii"))
            outputs[problem_id] = {"path": str(output), "pages": 1}
        return outputs

    def assert_artifacts_equal(self, before):
        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload, path)

    def test_compile_failure_leaves_live_artifacts_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)

            def failing_compile(snapshot_root, snapshot_workspace, loaded):
                self.fake_compile(snapshot_root, snapshot_workspace, loaded)
                raise ProbHubError("injected Typst failure", code="typeset_failed")

            with patch("probhub.building.compile_collection", side_effect=failing_compile):
                with self.assertRaises(ProbHubError) as raised:
                    typeset_workspace(root, workspace, problem_entries(workspace))

            self.assertEqual(raised.exception.code, "typeset_failed")
            self.assert_artifacts_equal(before)
            self.assertEqual(list((root / ".probhub").glob("build-publish-*")), [])
            self.assertEqual(list(root.parent.glob(f".{root.name}-probhub-*")), [])

    def test_extraction_failure_does_not_publish_earlier_problem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)

            def failing_extract(main_pdf, loaded, only_ids=None):
                first_dir, _ = loaded[0]
                (first_dir / "problem.pdf").write_bytes(b"staged first PDF")
                raise ProbHubError("injected second problem extraction failure")

            with (
                patch("probhub.building.compile_collection", side_effect=self.fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=failing_extract),
            ):
                with self.assertRaisesRegex(ProbHubError, "second problem"):
                    typeset_workspace(root, workspace, problem_entries(workspace))

            self.assert_artifacts_equal(before)

    def test_publish_failure_rolls_back_all_typeset_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)
            replacements = 0

            def failing_replace(source, target):
                nonlocal replacements
                replacements += 1
                if replacements == 4:
                    raise OSError("injected typeset publish failure")
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)

            with (
                patch("probhub.building.compile_collection", side_effect=self.fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=self.fake_extract),
                patch("probhub.building._replace_publish_path", side_effect=failing_replace),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    typeset_workspace(root, workspace, problem_entries(workspace))

            self.assertEqual(raised.exception.code, "publish_failed")
            self.assert_artifacts_equal(before)
            self.assertEqual(list((root / ".probhub").glob("build-publish-*")), [])

    def test_input_change_during_publish_staging_aborts_before_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)
            statement = root / "A/problem.md"
            real_copyfile = __import__("shutil").copyfile
            changed = False

            def copy_and_change_live_input(source, target, *args, **kwargs):
                nonlocal changed
                result = real_copyfile(source, target, *args, **kwargs)
                target_path = Path(target)
                if not changed and any(
                    part.name.startswith("build-publish-")
                    for part in target_path.parents
                ):
                    statement.write_text(
                        statement.read_text(encoding="utf-8")
                        + "\nChanged while staging the publish transaction.\n",
                        encoding="utf-8",
                    )
                    changed = True
                return result

            with (
                patch("probhub.building.compile_collection", side_effect=self.fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=self.fake_extract),
                patch("probhub.building.shutil.copyfile", side_effect=copy_and_change_live_input),
                patch(
                    "probhub.building._replace_publish_path",
                    side_effect=AssertionError("commit must not begin"),
                ),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    typeset_workspace(root, workspace, problem_entries(workspace))

            self.assertTrue(changed)
            self.assertEqual(raised.exception.code, "inputs_changed")
            self.assertIn("A.source_hash", str(raised.exception))
            self.assert_artifacts_equal(before)
            self.assertEqual(list((root / ".probhub").glob("build-publish-*")), [])

    def test_success_publishes_only_typeset_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            before = self.seed_artifacts(root)
            entries = [problem_entries(workspace)[0]]

            with (
                patch("probhub.building.compile_collection", side_effect=self.fake_compile),
                patch("probhub.building.extract_problem_pdfs", side_effect=self.fake_extract),
            ):
                result = typeset_workspace(root, workspace, entries)

            self.assertTrue(result["ok"])
            self.assertEqual(set(result["pdfs"]), {"A"})
            self.assertEqual(Path(result["main_pdf"]), root / "typst/contest/main.pdf")
            self.assertEqual((root / "typst/contest/main.pdf").read_bytes(), b"new main pdf")
            self.assertEqual((root / "A/problem.pdf").read_bytes(), b"new problem pdf-A")
            self.assertEqual((root / "B/problem.pdf").read_bytes(), before[root / "B/problem.pdf"])
            for problem_id in ("A", "B"):
                for relative in (
                    "problem.yaml",
                    "domjudge-problem.ini",
                    ".probhub/build-manifest.json",
                ):
                    path = root / problem_id / relative
                    self.assertEqual(path.read_bytes(), before[path], path)
                package = root / f"{problem_id}.zip"
                self.assertEqual(package.read_bytes(), before[package], package)


if __name__ == "__main__":
    unittest.main()
