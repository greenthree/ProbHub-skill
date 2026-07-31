import tempfile
import unittest
from pathlib import Path

from probhub.errors import ProbHubError
from probhub.io import write_yaml
from probhub.linting import lint_workspace
from probhub.metadata import problem_boundary_marker
from probhub.scaffold import scaffold_config, scaffold_files
from probhub.typesetting import (
    _expected_boundaries,
    _validate_legacy_boundaries,
    _validate_marker_boundaries,
)
from probhub.workspace import load_workspace


class PdfBoundaryTests(unittest.TestCase):
    def loaded(self, *items):
        return [
            (Path(problem_id), {
                "id": problem_id,
                "display_name": display_name,
            })
            for problem_id, display_name in items
        ]

    def test_boundary_markers_are_stable_and_id_specific(self):
        marker = problem_boundary_marker("L01")
        self.assertEqual(marker, problem_boundary_marker("L01"))
        self.assertNotEqual(marker, problem_boundary_marker("L02"))
        self.assertRegex(marker, r"^PROBHUB_BOUNDARY_V1_[0-9a-f]{64}$")

    def test_marker_boundaries_require_exact_order_and_unique_pages(self):
        expected = _expected_boundaries(self.loaded(("L01", "Alpha"), ("L02", "Beta")))
        valid = [
            {"marker": expected[0]["marker"], "page": 3},
            {"marker": expected[1]["marker"], "page": 5},
        ]
        self.assertEqual(
            _validate_marker_boundaries(valid, expected),
            [
                {"id": "L01", "marker": expected[0]["marker"], "page": 3},
                {"id": "L02", "marker": expected[1]["marker"], "page": 5},
            ],
        )

        invalid_sets = [
            valid[:1],
            [valid[1], valid[0]],
            [valid[0], valid[0]],
            [valid[0], {"marker": problem_boundary_marker("UNKNOWN"), "page": 5}],
            [valid[0], {**valid[1], "page": 3}],
        ]
        for boundaries in invalid_sets:
            with self.subTest(boundaries=boundaries):
                with self.assertRaises(ProbHubError) as raised:
                    _validate_marker_boundaries(boundaries, expected)
                self.assertEqual(raised.exception.code, "pdf_boundary_invalid")

    def test_legacy_boundaries_reject_unicode_and_casefold_name_collisions(self):
        expected = _expected_boundaries(
            self.loaded(("L01", "Ａ lpha"), ("L02", "alpha"))
        )
        boundaries = [
            {"display_name": "Ａ lpha", "page": 1},
            {"display_name": "alpha", "page": 2},
        ]
        with self.assertRaises(ProbHubError) as raised:
            _validate_legacy_boundaries(boundaries, expected)
        self.assertEqual(raised.exception.code, "pdf_boundary_invalid")
        self.assertIn("normalized duplicate", str(raised.exception))

    def test_lint_rejects_normalized_duplicate_names_only_for_legacy_templates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".probhub").mkdir()
            typst_dir = root / "typst/contest"
            typst_dir.mkdir(parents=True)
            main_typ = typst_dir / "main.typ"
            main_typ.write_text("#let legacy = true\n", encoding="utf-8")
            write_yaml(root / ".probhub/workspace.yaml", {
                "schema_version": 1,
                "typst": {"directory": "typst/contest"},
                "problems": [
                    {"id": "L01", "directory": "L01"},
                    {"id": "L02", "directory": "L02"},
                ],
            })
            for problem_id, name in (("L01", "Ａ lpha"), ("L02", "alpha")):
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
            legacy = lint_workspace(root, workspace)
            self.assertTrue(
                any("unique normalized problem display names" in item for item in legacy["errors"]),
                legacy,
            )
            selected = lint_workspace(root, workspace, [workspace["problems"][0]])
            self.assertTrue(
                any(
                    "unique normalized problem display names" in item
                    for item in selected["errors"]
                ),
                selected,
            )

            main_typ.write_text(
                "#let protocol(entry) = entry.boundary_marker\n",
                encoding="utf-8",
            )
            marker_aware = lint_workspace(root, workspace)
            self.assertFalse(
                any(
                    "unique normalized problem display names" in item
                    for item in marker_aware["errors"]
                ),
                marker_aware,
            )

    def test_unused_typst_helper_does_not_enable_marker_protocol(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".probhub").mkdir()
            typst_dir = root / "typst/contest"
            typst_dir.mkdir(parents=True)
            (typst_dir / "main.typ").write_text("#let legacy = true\n", encoding="utf-8")
            (typst_dir / "unused.typ").write_text(
                "#let protocol(entry) = entry.boundary_marker\n", encoding="utf-8"
            )
            write_yaml(root / ".probhub/workspace.yaml", {
                "schema_version": 1,
                "typst": {"directory": "typst/contest"},
                "problems": [
                    {"id": "L01", "directory": "L01"},
                    {"id": "L02", "directory": "L02"},
                ],
            })
            for problem_id, name in (("L01", "Ａ lpha"), ("L02", "alpha")):
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
            result = lint_workspace(root, workspace)
            self.assertTrue(
                any(
                    "unique normalized problem display names" in item
                    for item in result["errors"]
                ),
                result,
            )


if __name__ == "__main__":
    unittest.main()
