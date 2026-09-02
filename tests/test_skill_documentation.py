import re
import unittest
from pathlib import Path

import yaml

from probhub.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]


class SkillDocumentationTests(unittest.TestCase):
    def test_skill_and_index_have_valid_local_markdown_links(self):
        documents = [
            ROOT / "SKILL.md",
            ROOT / "references" / "index.md",
            ROOT / "references" / "problem-design-principles.md",
        ]
        links = re.compile(r"\]\(([^)#]+)(?:#[^)]+)?\)")
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for target in links.findall(text):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (document.parent / target).resolve()
                with self.subTest(document=document.name, target=target):
                    self.assertTrue(resolved.is_relative_to(ROOT), f"link escapes package root: {target}")
                    self.assertTrue(resolved.is_file(), f"missing local link: {target}")

    def test_skill_routes_low_frequency_details_to_references(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        index = (ROOT / "references" / "index.md").read_text(encoding="utf-8")
        for marker in (
            "cli.md",
            "checker-interactor.md",
            "verification-modes.md",
            "generations.md",
            "aggregate-limit-derivation.md",
            "problem-design-principles.md",
            "problem-creation-walkthrough.md",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, skill)
                self.assertIn(marker, index)

        self.assertIn("references/index.md", skill)

        for phase in ("### 1. 设计", "### 2. 骨架与规范源", "### 3. 执行", "### 4. 验证"):
            with self.subTest(phase=phase):
                self.assertIn(phase, index)
        self.assertIn("按需追加", index)

        self.assertIn("judge.qa.schema_version: 1", skill)
        self.assertIn("all_expectations_met", skill)
        self.assertIn("基础设施", skill)
        self.assertNotIn("旧的 Legacy workflow", skill)

    def test_skill_distinguishes_new_workspace_initialization(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("明确要求新建赛事或空工作区", skill)
        self.assertIn("直接以当前安装包的 `probhub init --help` 为准执行 `probhub init`", skill)
        self.assertIn("修改、验证或交付已有工作区时", skill)
        self.assertIn("`migration_required`", skill)
        self.assertIn("不要读取旧 `meta.json`", skill)

    def test_skill_mainline_states_structured_success_criteria(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("每一步都有结构化成功判据", skill)
        self.assertIn("结构化结果 `ok=true`", skill)
        self.assertIn("两条命令都必须退出码为 `0`", skill)
        self.assertIn("没有 `counterexample`、`infrastructure` 或取消未完成项", skill)

    def test_problem_design_guidance_preserves_evidence_boundaries(self):
        design = (ROOT / "references" / "problem-design-principles.md").read_text(
            encoding="utf-8"
        )
        for marker in (
            "design_intent",
            "difficulty_anchor",
            "validation_risk",
            "needs_review",
            "数组/字符串",
            "图论",
            "多组数据",
            "浮点/构造",
            "交互题",
            "设计建议",
            "人工证明/审查",
            "Core 证据",
            "目标平台校准",
            "不能证明不存在未知错解",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, design)
        for reference in (
            "verification-modes.md",
            "aggregate-limit-derivation.md",
            "mistake-taxonomy.md",
            "data-groups-expectations.md",
            "checker-interactor.md",
            "workspace-schema-v1.md",
            "problem-creation-walkthrough.md",
        ):
            with self.subTest(reference=reference):
                self.assertIn(f"]({reference})", design)
        for difficulty in range(6):
            with self.subTest(difficulty=difficulty):
                self.assertIn(f"| {difficulty} |", design)
        self.assertIn("problem-design-principles.md", (ROOT / "SKILL.md").read_text(encoding="utf-8"))

    def test_checker_interactor_guidance_covers_design_boundaries(self):
        document = (ROOT / "references" / "checker-interactor.md").read_text(
            encoding="utf-8"
        )
        for marker in (
            "合法性层",
            "目标层",
            "官方答案独立",
            "NaN",
            "+inf",
            "状态机",
            "START",
            "WAIT_QUERY",
            "query_limit",
            "不是查询次数上限",
            "固定回答",
            "自适应回答",
            "隐藏 strategy",
            "query-over-limit",
            "infrastructure FAIL",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, document)
        self.assertIn("mistake-taxonomy.md", document)
        self.assertIn("verification-modes.md", document)

    def test_problem_creation_walkthrough_covers_supported_judge_branches(self):
        walkthrough = (ROOT / "references" / "problem-creation-walkthrough.md").read_text(
            encoding="utf-8"
        )
        for marker in (
            "probhub init",
            "probhub new A01",
            "probhub sample-check A01",
            "probhub gen A01 --apply",
            "probhub judge A01 --no-cache",
            "probhub checkpoint A01",
            "probhub seal A01 --no-cache --seed 12345",
            "probhub build A01 B02 C03 --no-cache",
            "--judge custom",
            "--judge interactive",
            "judge.qa.schema_version: 1",
            "judge-fixtures/checker/alternative.out",
            "code/judge-qa/normal.cpp",
            "behavior: output-flood",
            "不能在 `expected` 中声明为 WA",
            "Interactor 题不走普通 `gen` 或 `stress`",
            "不需要等待其他题",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, walkthrough)

    def test_walkthrough_yaml_and_commands_match_executable_fixture(self):
        walkthrough = (ROOT / "references" / "problem-creation-walkthrough.md").read_text(
            encoding="utf-8"
        )
        blocks = re.findall(r"```yaml\n(.*?)\n```", walkthrough, flags=re.DOTALL)
        self.assertGreaterEqual(len(blocks), 3)
        parsed = [yaml.safe_load(block) for block in blocks]
        self.assertTrue(all(isinstance(value, dict) for value in parsed))
        snippet = next(value for value in parsed if value.get("id") == "W01")
        self.assertEqual(snippet["schema_version"], 1)
        self.assertEqual(snippet["judge"]["type"], "standard")
        self.assertEqual(snippet["judge"]["validator"], "code/validator.cpp")
        self.assertEqual(len(snippet["solutions"]["accepted"]), 1)
        self.assertEqual(len(snippet["solutions"]["brute"]), 1)
        self.assertEqual(len(snippet["solutions"]["wrong"]), 2)
        self.assertEqual(len(snippet["data"]["recipes"]), 3)

        fixture = ROOT / "tests" / "fixtures" / "workspaces" / "walkthrough" / "W01"
        config = yaml.safe_load((fixture / "probhub.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["id"], snippet["id"])
        self.assertEqual(config["judge"], {**config["judge"], **snippet["judge"]})
        for entry in config["solutions"]["accepted"] + config["solutions"]["brute"] + config["solutions"]["wrong"]:
            self.assertTrue((fixture / entry["file"]).is_file(), entry["file"])
        for recipe in config["data"]["recipes"]:
            if not recipe.get("manual"):
                self.assertTrue((fixture / recipe["generator"]).is_file(), recipe["generator"])
        self.assertIn(r"\sum_{i=1}^{T} n_i\le 64", (fixture / "problem.md").read_text(encoding="utf-8"))
        self.assertIn("ensuref(total_n <= 64", (fixture / "code" / "validator.cpp").read_text(encoding="utf-8"))

        parser = build_parser()
        commands = (
            ["--json", "lint", "W01"],
            ["sample-check", "W01", "--no-cache"],
            ["gen", "W01", "--apply"],
            ["judge", "W01", "--no-cache"],
            ["stress", "W01", "--rounds", "100", "--seed", "12345"],
        )
        for command in commands:
            with self.subTest(command=command):
                args = parser.parse_args(command)
                self.assertIn(args.command, {"lint", "sample-check", "gen", "judge", "stress"})


if __name__ == "__main__":
    unittest.main()
