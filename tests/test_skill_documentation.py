import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SkillDocumentationTests(unittest.TestCase):
    def test_skill_and_index_have_valid_local_markdown_links(self):
        documents = [ROOT / "SKILL.md", ROOT / "references" / "index.md"]
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
            "problem-creation-walkthrough.md",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, skill)
                self.assertIn(marker, index)

        self.assertIn("references/index.md", skill)

        self.assertIn("judge.qa.schema_version: 1", skill)
        self.assertIn("all_expectations_met", skill)
        self.assertIn("基础设施", skill)
        self.assertNotIn("旧的 Legacy workflow", skill)

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


if __name__ == "__main__":
    unittest.main()
