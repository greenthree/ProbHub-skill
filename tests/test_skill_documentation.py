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
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, skill)
                self.assertIn(marker, index)

        self.assertIn("references/index.md", skill)

        self.assertIn("judge.qa.schema_version: 1", skill)
        self.assertIn("all_expectations_met", skill)
        self.assertIn("基础设施", skill)
        self.assertNotIn("旧的 Legacy workflow", skill)


if __name__ == "__main__":
    unittest.main()
