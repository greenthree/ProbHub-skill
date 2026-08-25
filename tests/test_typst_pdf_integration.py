import os
import shutil
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

from probhub.building import typeset_workspace
from probhub.errors import ProbHubError
from probhub.hashing import hash_file
from probhub.io import write_yaml
from probhub.metadata import problem_boundary_marker
from probhub.scaffold import scaffold_config, scaffold_files
from probhub.typesetting import problem_boundaries
from probhub.workspace import load_problem, load_workspace, problem_entries


ROOT = Path(__file__).resolve().parents[1]
RUN_TYPST_E2E = (
    os.environ.get("PROBHUB_RUN_TYPST_E2E") == "1"
    and shutil.which("typst") is not None
)


@unittest.skipUnless(
    RUN_TYPST_E2E,
    "set PROBHUB_RUN_TYPST_E2E=1 with Typst on PATH to run the PDF integration test",
)
class TypstPdfIntegrationTests(unittest.TestCase):
    def create_production_template_workspace(self, root, items=None):
        items = items or (("A", "Alpha"), ("B", "Beta"))
        (root / ".probhub").mkdir(parents=True)
        typst_root = root / "typst-statement"
        typst_dir = typst_root / "正式赛"
        typst_dir.mkdir(parents=True)
        references = ROOT / "references"
        shutil.copyfile(references / "lib.typ", typst_root / "lib.typ")
        shutil.copyfile(ROOT / "logo.svg", typst_root / "probhub.svg")
        shutil.copyfile(references / "main.typ", typst_dir / "main.typ")
        shutil.copyfile(references / "problems.typ", typst_dir / "problems.typ")

        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "contest": {
                "title": "ProbHub 生产模板集成测试",
                "subtitle": "正式赛",
                "author": "ProbHub CI",
                "date": "2026年7月29日",
            },
            "typst": {
                "directory": "typst-statement/正式赛",
                "creation_timestamp": 0,
                "cover": {
                    "logo": "probhub.svg",
                    "logo_width": "7cm",
                    "logo_space_above": "0em",
                    "logo_space_below": "0em",
                },
            },
            "problems": [
                {"id": problem_id, "directory": problem_id}
                for problem_id, _ in items
            ],
        })
        statements = {
            "A": (
                "# Alpha\n\n"
                "## 题目描述\n\n"
                "这是 PRODUCTION-MARKDOWN-SMOKE。**加粗中文**用于字体回退检查，"
                "行内公式 $x^2+y^2=z^2$ 用于验证 mitex。\n\n"
                "- cmarker 列表项\n\n"
                "## 输入格式\n\n"
                "输入两个整数 $a,b$。\n\n"
                "## 输出格式\n\n"
                "输出 $a+b$。\n"
            ),
            "B": (
                "# Beta\n\n"
                "## 题目描述\n\n"
                "第二道题用于验证生产模板的跨题分页。\n\n"
                "## 输入格式\n\n"
                "输入两个整数。\n\n"
                "## 输出格式\n\n"
                "输出它们的和。\n"
            ),
        }
        for problem_id, name in items:
            problem_dir = root / problem_id
            for relative, content in scaffold_files(name, "standard").items():
                path = problem_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
            (problem_dir / "problem.md").write_text(
                statements.get(problem_id) or (
                    f"# {name}\n\n"
                    "## 题目描述\n\n"
                    f"Boundary fixture for {problem_id}.\n\n"
                    "## 输入格式\n\n输入一个整数。\n\n"
                    "## 输出格式\n\n输出这个整数。\n"
                ),
                encoding="utf-8",
                newline="\n",
            )
            write_yaml(
                problem_dir / "probhub.yaml",
                scaffold_config(problem_id, name, "standard"),
            )
        _, workspace = load_workspace(root)
        return workspace

    def create_workspace(self, root):
        (root / ".probhub").mkdir(parents=True)
        typst_dir = root / "typst/contest"
        typst_dir.mkdir(parents=True)
        (typst_dir / "main.typ").write_text(
            '#let problems = json("problems.json")\n'
            '#set page(width: 210mm, height: 297mm, margin: 20mm)\n'
            '#set text(font: "Noto Sans CJK SC", size: 12pt)\n'
            '#for (index, entry) in problems.enumerate() {\n'
            '  if index > 0 { pagebreak() }\n'
            '  text(size: 1pt, fill: rgb("#00000000"), entry.boundary_marker)\n'
            '  heading(level: 1)[题目 #str.from-unicode(65 + index). #entry.problem.display_name]\n'
            '  [#entry.statement.description]\n'
            '  if index == 0 {\n'
            '    pagebreak()\n'
            '    [Alpha continuation page.]\n'
            '  }\n'
            '}\n',
            encoding="utf-8",
            newline="\n",
        )
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "contest": {"title": "PDF Integration Fixture"},
            "typst": {
                "directory": "typst/contest",
                "creation_timestamp": 0,
            },
            "problems": [
                {"id": "A", "directory": "A"},
                {"id": "B", "directory": "B"},
            ],
        })
        for problem_id, name in (("A", "Alpha"), ("B", "Beta")):
            problem_dir = root / problem_id
            for relative, content in scaffold_files(name, "standard").items():
                path = problem_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
            write_yaml(
                problem_dir / "probhub.yaml",
                scaffold_config(problem_id, name, "standard"),
            )
        _, workspace = load_workspace(root)
        return workspace

    def test_canonical_production_template_compiles_cover_markdown_math_and_footers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_production_template_workspace(root)

            result = typeset_workspace(root, workspace, problem_entries(workspace))

            main_pdf = Path(result["main_pdf"])
            reader = PdfReader(main_pdf)
            loaded = [
                load_problem(root, entry)
                for entry in problem_entries(workspace)
            ]
            self.assertEqual(len(reader.pages), 4)
            self.assertEqual(
                problem_boundaries(main_pdf, loaded),
                [
                    {"id": "A", "marker": problem_boundary_marker("A"), "page": 3},
                    {"id": "B", "marker": problem_boundary_marker("B"), "page": 4},
                ],
            )
            self.assertEqual(result["pdfs"]["A"]["pages"], 1)
            self.assertEqual(result["pdfs"]["B"]["pages"], 1)

            cover = "".join((reader.pages[0].extract_text() or "").split())
            alpha = "".join((reader.pages[2].extract_text() or "").split())
            beta = "".join((reader.pages[3].extract_text() or "").split())
            self.assertIn("ProbHub生产模板集成测试", cover)
            self.assertIn("试题列表", cover)
            self.assertIn("Alpha", cover)
            self.assertIn("Beta", cover)
            self.assertIn("本试题册共2题，2页", cover)
            self.assertEqual((reader.pages[1].extract_text() or "").strip(), "")

            self.assertIn("ProbHub生产模板集成测试", alpha)
            self.assertIn("2026年7月29日", alpha)
            self.assertIn("题目A.Alpha", alpha)
            self.assertIn("PRODUCTION-MARKDOWN-SMOKE", alpha)
            self.assertIn("加粗中文", alpha)
            self.assertIn("cmarker列表项", alpha)
            self.assertIn("第1页，共2页", alpha)
            self.assertIn("题目B.Beta", beta)
            self.assertIn("第2页，共2页", beta)

            self.assertEqual(len(PdfReader(root / "A/problem.pdf").pages), 1)
            self.assertEqual(len(PdfReader(root / "B/problem.pdf").pages), 1)
            self.assertEqual(
                (root / "typst-statement/lib.typ").read_bytes(),
                (ROOT / "references/lib.typ").read_bytes(),
            )
            self.assertEqual(list(root.rglob(".probhub-*.typ")), [])

    def test_canonical_production_template_compiles_custom_workspace_logo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_production_template_workspace(root)
            custom_logo = root / "typst-statement/custom-logo.svg"
            custom_logo.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
                '<rect width="80" height="40" fill="#336699"/></svg>\n',
                encoding="utf-8",
            )
            workspace_path = root / ".probhub/workspace.yaml"
            workspace["typst"]["cover"]["logo"] = custom_logo.name
            write_yaml(workspace_path, workspace)
            _, workspace = load_workspace(root)

            result = typeset_workspace(root, workspace, problem_entries(workspace))

            self.assertTrue(Path(result["main_pdf"]).is_file())
            self.assertEqual(workspace["typst"]["cover"]["logo"], "custom-logo.svg")
            self.assertEqual(custom_logo.read_text(encoding="utf-8").count("#336699"), 1)
            self.assertEqual(list(root.rglob(".probhub-*.typ")), [])

    def test_canonical_markers_support_duplicate_long_names_and_aa_label(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            long_name = "跨页边界稳定性验证" * 12
            items = [
                (f"P{index:02d}", "重复题名" if index <= 2 else f"Problem {index}")
                for index in range(1, 27)
            ]
            items.append(("P27", long_name))
            workspace = self.create_production_template_workspace(root, items)
            main_typ = root / "typst-statement/正式赛/main.typ"
            main_text = main_typ.read_text(encoding="utf-8")
            main_text = main_text.replace("enable-titlepage: true", "enable-titlepage: false")
            main_text = main_text.replace("enable-problem-list: true", "enable-problem-list: false")
            main_typ.write_text(main_text, encoding="utf-8", newline="\n")

            entries = problem_entries(workspace)
            result = typeset_workspace(root, workspace, entries)
            loaded = [load_problem(root, entry) for entry in entries]
            boundaries = problem_boundaries(result["main_pdf"], loaded)

            self.assertEqual([item["id"] for item in boundaries], [item[0] for item in items])
            self.assertEqual([item["page"] for item in boundaries], list(range(1, 28)))
            self.assertTrue(all(result["pdfs"][problem_id]["pages"] == 1 for problem_id, _ in items))
            for problem_id, _ in items:
                extracted = "".join(
                    page.extract_text() or ""
                    for page in PdfReader(root / problem_id / "problem.pdf").pages
                )
                self.assertEqual(extracted.count(problem_boundary_marker(problem_id)), 1)
            last_page = PdfReader(result["main_pdf"]).pages[-1].extract_text() or ""
            self.assertIn("题目 AA.", last_page)
            self.assertIn(long_name[:12], last_page)

    def test_real_multi_problem_typeset_splits_pages_and_failure_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            entries = problem_entries(workspace)

            result = typeset_workspace(root, workspace, entries)

            main_pdf = Path(result["main_pdf"])
            loaded = [load_problem(root, entry) for entry in entries]
            self.assertEqual(len(PdfReader(main_pdf).pages), 3)
            self.assertEqual(
                problem_boundaries(main_pdf, loaded),
                [
                    {"id": "A", "marker": problem_boundary_marker("A"), "page": 1},
                    {"id": "B", "marker": problem_boundary_marker("B"), "page": 3},
                ],
            )
            self.assertEqual(result["pdfs"]["A"]["pages"], 2)
            self.assertEqual(result["pdfs"]["B"]["pages"], 1)
            alpha_reader = PdfReader(root / "A/problem.pdf")
            beta_reader = PdfReader(root / "B/problem.pdf")
            self.assertEqual(len(alpha_reader.pages), 2)
            self.assertEqual(len(beta_reader.pages), 1)
            self.assertIn("Alpha continuation page.", alpha_reader.pages[1].extract_text())
            self.assertIn("题目 B. Beta", beta_reader.pages[0].extract_text())
            self.assertEqual(list(root.rglob(".probhub-*.typ")), [])

            generated = [
                root / "A/meta.json",
                root / "B/meta.json",
                root / "A/problem.pdf",
                root / "B/problem.pdf",
                root / "typst/contest/problems.json",
                main_pdf,
            ]
            before = {path: hash_file(path) for path in generated}
            duplicate_marker_main = (root / "typst/contest/main.typ").read_text(
                encoding="utf-8"
            )
            duplicate_marker_main = duplicate_marker_main.replace(
                "entry.boundary_marker", f'"{problem_boundary_marker("A")}"'
            )
            (root / "typst/contest/main.typ").write_text(
                duplicate_marker_main,
                encoding="utf-8",
                newline="\n",
            )

            with self.assertRaises(ProbHubError) as raised:
                typeset_workspace(root, workspace, entries)

            self.assertEqual(raised.exception.code, "pdf_boundary_invalid")
            self.assertEqual({path: hash_file(path) for path in generated}, before)
            self.assertEqual(list((root / ".probhub").glob("build-publish-*")), [])
            self.assertEqual(list(root.parent.glob(f".{root.name}-probhub-*")), [])

            (root / "typst/contest/main.typ").write_text(
                "#let broken =\n",
                encoding="utf-8",
                newline="\n",
            )

            with self.assertRaises(ProbHubError) as raised:
                typeset_workspace(root, workspace, entries)

            self.assertEqual(raised.exception.code, "typeset_failed")
            self.assertEqual({path: hash_file(path) for path in generated}, before)
            self.assertEqual(list((root / ".probhub").glob("build-publish-*")), [])
            self.assertEqual(list(root.parent.glob(f".{root.name}-probhub-*")), [])


if __name__ == "__main__":
    unittest.main()
