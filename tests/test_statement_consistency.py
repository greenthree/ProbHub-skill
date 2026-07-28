import os
import tempfile
import unittest
from pathlib import Path

from probhub.io import read_yaml, write_yaml
from probhub.linting import lint_workspace
from probhub.workspace import load_workspace


VALID_STATEMENT = """# Test

## 题目描述

Description.

## 输入格式

Input.

## 输出格式

Output.
"""


class StatementConsistencyTests(unittest.TestCase):
    def create_workspace(self, root, *, statement=VALID_STATEMENT, validator="int main() {}\n", name="Test"):
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "problems": [{"id": "A", "directory": "A"}],
        })
        problem = root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(statement, encoding="utf-8")
        (problem / "code/validator.cpp").write_text(validator, encoding="utf-8")
        (problem / "code/std.cpp").write_text("int main() {}\n", encoding="utf-8")
        for directory in (problem / "data/sample", problem / "data/secret"):
            (directory / "1.in").write_text("1\n", encoding="utf-8")
            (directory / "1.ans").write_text("1\n", encoding="utf-8")
        write_yaml(problem / "probhub.yaml", {
            "schema_version": 1,
            "id": "A",
            "name": name,
            "display_name": name,
            "limits": {"time": 1, "memory": 256, "output": 64, "processes": 32},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {"accepted": ["code/std.cpp"], "brute": [], "wrong": []},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        })
        return problem

    def lint(self, root):
        root, workspace = load_workspace(root)
        return lint_workspace(root, workspace)["problems"][0]

    def update_config(self, problem, update):
        path = problem / "probhub.yaml"
        config = read_yaml(path)
        update(config)
        write_yaml(path, config)

    def test_deterministic_statement_structure_errors_are_blocking(self):
        cases = {
            "missing_h1": (
                "## 题目描述\nD\n\n## 输入格式\nI\n\n## 输出格式\nO\n",
                "statement_missing_h1",
            ),
            "multiple_h1": (
                "# Test\n\n# Another title\n\n## 题目描述\nD\n\n"
                "## 输入格式\nI\n\n## 输出格式\nO\n",
                "statement_multiple_h1",
            ),
            "missing_input": (
                "# Test\n\n## 题目描述\nD\n\n## 输出格式\nO\n",
                "statement_missing_section",
            ),
            "duplicate_output": (
                "# Test\n\n## 题目描述\nD\n\n## 输入格式\nI\n\n"
                "## 输出格式\nO1\n\n## 输出描述\nO2\n",
                "statement_duplicate_section",
            ),
            "out_of_order": (
                "# Test\n\n## 输入格式\nI\n\n## 题目描述\nD\n\n## 输出格式\nO\n",
                "statement_section_order",
            ),
            "notes_before_output": (
                "# Test\n\n## 题目描述\nD\n\n## 输入格式\nI\n\n"
                "## 提示\nN\n\n## 输出格式\nO\n",
                "statement_notes_before_output",
            ),
            "embedded_sample": (
                VALID_STATEMENT + "\n## 样例输入 1\n\n1\n\n## Sample Output\n\n1\n",
                "statement_embedded_sample_section",
            ),
            "embedded_sample_h3": (
                VALID_STATEMENT + "\n### 样例输入\n\n1\n",
                "statement_embedded_sample_section",
            ),
            "embedded_sample_middle_number": (
                VALID_STATEMENT + "\n## 样例 1 输出\n\n1\n",
                "statement_embedded_sample_section",
            ),
        }
        for name, (statement, expected_code) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.create_workspace(root, statement=statement)
                result = self.lint(root)
                codes = {
                    issue["code"] for issue in result["statement"]["structure"]["errors"]
                }
                self.assertFalse(result["ok"], result)
                self.assertIn(expected_code, codes)

    def test_heading_scan_ignores_fenced_code_and_keeps_soft_warnings(self):
        statement = """# Different title

## 题目描述

The following Markdown is literal:

```markdown
## 输入格式
## 样例输入
## Not a real heading
```

## 输入格式

Input.

## 输出格式

Output.

## 额外说明

Extra.
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement)
            result = self.lint(root)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["statement"]["structure"]["ok"])
            self.assertIn(
                "statement title 'Different title' differs from name 'Test'",
                result["warnings"],
            )
            self.assertIn("unknown statement headings: 额外说明", result["warnings"])
            self.assertFalse(any("样例输入" in error for error in result["errors"]))

    def test_statement_and_validator_paths_must_be_problem_local_regular_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            (root / "outside.md").write_text(VALID_STATEMENT, encoding="utf-8")
            (root / "outside-validator.cpp").write_text("int main() {}\n", encoding="utf-8")

            self.update_config(
                problem,
                lambda config: config["statement"].update({"source": "../outside.md"}),
            )
            result = self.lint(root)
            self.assertFalse(result["ok"])
            self.assertIn(
                "statement.source must stay inside the problem directory: ../outside.md",
                result["errors"],
            )

            self.update_config(problem, lambda config: config.update({
                "statement": {"source": "problem.md"},
                "judge": {"type": "standard", "validator": "../outside-validator.cpp"},
            }))
            result = self.lint(root)
            self.assertFalse(result["ok"])
            self.assertIn(
                "judge.validator must stay inside the problem directory: ../outside-validator.cpp",
                result["errors"],
            )

    def test_statement_and_validator_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            statement_link = problem / "linked-problem.md"
            validator_link = problem / "code/linked-validator.cpp"
            try:
                os.symlink("problem.md", statement_link)
                os.symlink("validator.cpp", validator_link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            self.update_config(
                problem,
                lambda config: config["statement"].update({"source": "linked-problem.md"}),
            )
            result = self.lint(root)
            self.assertIn(
                "statement.source must be a regular non-symlink file: linked-problem.md",
                result["errors"],
            )

            self.update_config(problem, lambda config: config.update({
                "statement": {"source": "problem.md"},
                "judge": {"type": "standard", "validator": "code/linked-validator.cpp"},
            }))
            result = self.lint(root)
            self.assertIn(
                "judge.validator must be a regular non-symlink file: code/linked-validator.cpp",
                result["errors"],
            )

    def test_l02_dynamic_bounds_match_by_subject_without_warning(self):
        statement = r"""# L02

## 题目描述

Description.

## 输入格式

第一行包含两个整数 $H_L,W_L$（$1\le H_L,W_L\le 10^9$）。

第二行包含两个整数 $x_L,y_L$（$1\le x_L\le H_L$，$1\le y_L\le W_L$）。

第三行包含两个整数 $H_R,W_R$（$1\le H_R,W_R\le 10^9$）。

第四行包含两个整数 $x_R,y_R$（$1\le x_R\le H_R$，$1\le y_R\le W_R$）。

第五行包含两个整数 $t_x,t_y$（$1\le t_x\le H_R$，$1\le t_y\le W_R$）。

## 输出格式

Output.
"""
        validator = r'''#include "testlib.h"
int main(int argc, char **argv) {
    registerValidation(argc, argv);
    long long hl = inf.readLong(1, 1000000000LL, "H_L");
    long long wl = inf.readLong(1, 1000000000LL, "W_L");
    inf.readLong(1, hl, "x_L");
    inf.readLong(1, wl, "y_L");
    long long hr = inf.readLong(1, 1000000000LL, "H_R");
    long long wr = inf.readLong(1, 1000000000LL, "W_R");
    inf.readLong(1, hr, "x_R");
    inf.readLong(1, wr, "y_R");
    inf.readLong(1, hr, "t_x");
    inf.readLong(1, wr, "t_y");
}
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator, name="L02")
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            self.assertTrue(result["ok"], result)
            self.assertEqual(report["analysis_state"], "partial")
            self.assertTrue(report["partial"])
            self.assertTrue(report["requires_review"])
            dynamic_match = next(
                item for item in report["matched"]
                if item["subject_normalized"] == "x_l" and item["bound"] == "upper"
            )
            self.assertTrue(dynamic_match["dynamic"])
            self.assertEqual(dynamic_match["normalized"], "x_l <= h_l")
            self.assertFalse(any("constraint_mismatch" in warning for warning in result["warnings"]))
            for side in (dynamic_match["statement"], dynamic_match["validator"]):
                self.assertIn("path", side)
                self.assertIn("line", side)
                self.assertIn("raw", side)
                self.assertIn("normalized", side)

    def test_scientific_notation_negative_numbers_and_strict_double_match(self):
        statement = r"""# L05

## 题目描述

Description.

## 输入格式

输入整数 $n$（$1\le n\le 10^6$）。

输入实数 $x$（$-2\times 10^3\le x\le 3$）。

## 输出格式

Output.
"""
        validator = r'''#include "testlib.h"
int main() {
    int n = inf.readInt(1, 1000000, "N");
    double x = inf.readStrictDouble(-2000, 3, "x");
}
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator, name="L05")
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            self.assertTrue(result["ok"], result)
            matches = {
                (item["subject_normalized"], item["bound"]): item
                for item in report["matched"]
            }
            self.assertEqual(matches[("n", "upper")]["statement"]["value"], 1000000)
            self.assertEqual(matches[("x", "lower")]["statement"]["value"], -2000)
            self.assertEqual(matches[("x", "lower")]["validator"]["origin"], "readStrictDouble")
            self.assertEqual(report["confidence"], "high")
            self.assertFalse(report["requires_review"])

    def test_l09_ensuref_size_comparisons_are_reconciled(self):
        statement = r"""# L09

## 题目描述

Description.

## 输入格式

第一行输入字符串 $s$（$1\le |s|\le 2\times 10^5$）。

第二行输入字符串 $t$（$1\le |t|\le 2\times 10^5$）。

## 输出格式

Output.
"""
        validator = r'''#include "testlib.h"
int main() {
    std::string s = inf.readToken("[a-z]+", "s");
    ensuref(s.size() <= 200000, "s is too long");
    std::string t = inf.readToken("[a-z]+", "t");
    ensuref(t.size() <= 200000, "t is too long");
}
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator, name="L09")
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            self.assertTrue(result["ok"], result)
            upper_matches = {
                item["subject_normalized"]: item
                for item in report["matched"]
                if item["bound"] == "upper"
            }
            self.assertEqual(set(upper_matches), {"len:s", "len:t"})
            self.assertEqual(upper_matches["len:s"]["validator"]["origin"], "ensuref")
            self.assertEqual(upper_matches["len:s"]["statement"]["value"], 200000)
            self.assertTrue(any(
                item["subject_normalized"] == "len:s" and item["bound"] == "lower"
                for item in report["statement_only"]
            ))
            self.assertFalse(any("constraint_mismatch" in warning for warning in result["warnings"]))

    def test_strict_integer_boundary_is_equivalent_to_shifted_inclusive_bound(self):
        statement = r"""# Strict

## 题目描述

Description.

## 输入格式

输入整数 $n$（$1\le n<10^9$）。

## 输出格式

Output.
"""
        validator = 'int n = inf.readInt(1, 999999999, "n");\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator, name="Strict")
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            upper = next(
                item for item in report["matched"]
                if item["subject_normalized"] == "n" and item["bound"] == "upper"
            )
            self.assertEqual(upper["normalized"], "n <= 999999999")
            self.assertFalse(any("constraint_mismatch" in warning for warning in result["warnings"]))

    def test_high_confidence_numeric_mismatch_is_nonblocking_warning_only(self):
        statement = r"""# Mismatch

## 题目描述

Description.

## 输入格式

输入整数 $n$（$1\le n\le 100$）。

## 输出格式

Output.
"""
        validator = 'int n = inf.readInt(1, 99, "n");\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator, name="Mismatch")
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["errors"], [])
            self.assertEqual(report["summary"]["high_confidence_mismatches"], 1)
            self.assertTrue(any("[constraint_mismatch]" in warning for warning in result["warnings"]))
            mismatch = next(
                diagnostic for diagnostic in result["diagnostics"]
                if diagnostic.get("code") == "constraint_mismatch"
            )
            self.assertEqual(mismatch["severity"], "warning")
            self.assertTrue(any(
                item["bound"] == "upper" and item["reconciliation"] == "numeric_mismatch"
                for item in report["statement_only"]
            ))

    def test_ensuref_disjunction_is_not_treated_as_a_hard_constraint(self):
        statement = VALID_STATEMENT.replace(
            "Input.", "输入整数 $n$（$1\\le n\\le 100$）。"
        )
        validator = r'''int main() {
            int n = inf.readInt(1, 100, "n");
            bool bypass = false;
            ensuref(n <= 99 || bypass, "conditional bound");
        }
        '''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator)
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            self.assertTrue(result["ok"], result)
            self.assertEqual(report["summary"]["high_confidence_mismatches"], 0)
            self.assertTrue(report["requires_review"])
            self.assertTrue(any(
                item.get("code") == "constraint_validator_expression_unsupported"
                for item in report["diagnostics"]
            ))

    def test_unknown_numeric_domain_does_not_choose_integer_equivalence(self):
        statement = VALID_STATEMENT.replace(
            "Input.", "输入实数 $x$（$x\\le 0$）。"
        )
        validator = 'double x; ensuref(x < 1, "bound");\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator)
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            self.assertTrue(result["ok"], result)
            self.assertEqual(report["summary"]["matched"], 0)
            self.assertEqual(report["summary"]["high_confidence_mismatches"], 0)
            self.assertTrue(report["requires_review"])
            self.assertFalse(any(
                diagnostic.get("code") == "constraint_mismatch"
                for diagnostic in report["diagnostics"]
            ))

    def test_cpp_strings_do_not_create_validator_constraints(self):
        statement = VALID_STATEMENT.replace(
            "Input.", "输入整数 $n$（$1\\le n\\le 100$）。"
        )
        validator = r'''int main() {
            int n = inf.readInt(1, 100, "n");
            const char *ordinary = "readInt(1, 98, \\"n\\")";
            const char *raw = R"tag(readInt(1, 99, "n"))tag";
        }
        '''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator)
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            upper = [
                item for item in report["validator_constraints"]
                if item["subject_normalized"] == "n" and item["bound"] == "upper"
            ]
            self.assertEqual([item["value"] for item in upper], [100])
            self.assertEqual(report["summary"]["high_confidence_mismatches"], 0)

    def test_only_testlib_inf_read_calls_are_treated_as_constraints(self):
        statement = VALID_STATEMENT.replace(
            "Input.", "输入整数 $n$（$1\\le n\\le 100$）。"
        )
        validator = r'''struct Fake { int readInt(int, int, const char *); };
        int readInt(int, int, const char *);
        int main() {
            Fake foo;
            foo.readInt(1, 99, "n");
            readInt(1, 98, "n");
        }
        '''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator)
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            self.assertTrue(result["ok"], result)
            self.assertEqual(report["validator_constraints"], [])
            self.assertEqual(report["summary"]["high_confidence_mismatches"], 0)
            self.assertTrue(report["requires_review"])

    def test_raw_string_comment_tokens_do_not_hide_later_read_calls(self):
        statement = VALID_STATEMENT.replace(
            "Input.",
            "输入整数 $n$（$1\\le n\\le 100$）和 $m$（$1\\le m\\le 5$）。",
        )
        validator = r'''int main() {
            int n = inf.readInt(1, 100, "n");
            const char *text = R"tag(a " /* raw text)tag";
            int m = inf.readInt(1, 5, "m");
        }
        '''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root, statement=statement, validator=validator)
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            self.assertTrue(result["ok"], result)
            validator_subjects = {
                item["subject_normalized"]
                for item in report["validator_constraints"]
            }
            self.assertEqual(validator_subjects, {"n", "m"})
            self.assertEqual(report["summary"]["matched"], 4)
            self.assertEqual(report["summary"]["high_confidence_mismatches"], 0)

    def test_zero_constraint_evidence_requires_review_with_stable_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            result = self.lint(root)
            report = result["constraint_reconciliation"]
            self.assertTrue(report["requires_review"])
            self.assertEqual(report["statement_constraints"], [])
            self.assertEqual(report["validator_constraints"], [])
            self.assertTrue(any(
                item.get("code") == "constraint_no_evidence"
                for item in report["diagnostics"]
            ))

    def test_falsey_non_mapping_statement_config_is_rejected(self):
        for value in ([], "", 0, False):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                problem = self.create_workspace(root)
                self.update_config(
                    problem,
                    lambda config, value=value: config.update({"statement": value}),
                )
                result = self.lint(root)
                self.assertFalse(result["ok"], result)
                self.assertIn("statement must be a mapping", result["errors"])


if __name__ == "__main__":
    unittest.main()
