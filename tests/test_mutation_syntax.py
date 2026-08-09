import json
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

from probhub.errors import ProbHubError
from probhub.mutation import MUTATION_OPERATOR_VERSION, plan_mutations
from probhub.mutation_syntax import (
    MUTATION_LOCATOR_VERSION,
    TREE_SITTER_CPP_VERSION,
    TREE_SITTER_VERSION,
    _cpp_language,
    mutation_parser_identity,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures/mutation-plans"
SOURCE_FIXTURE = FIXTURE_ROOT / "cpp-syntax-v1.cpp"
PLAN_FIXTURE = FIXTURE_ROOT / "cpp-syntax-v1.json"


def mutation_records(plan):
    return [item.as_dict() for item in plan["mutations"]]


class MutationSyntaxTests(unittest.TestCase):
    def test_pinned_parser_identity_is_explicit(self):
        identity = mutation_parser_identity()
        self.assertEqual(identity["locator_version"], MUTATION_LOCATOR_VERSION)
        self.assertEqual(identity["tree_sitter_version"], TREE_SITTER_VERSION)
        self.assertEqual(identity["tree_sitter_cpp_version"], TREE_SITTER_CPP_VERSION)

    def test_canonical_plan_is_stable_across_lf_crlf_and_utf8_path(self):
        source = SOURCE_FIXTURE.read_text(encoding="utf-8")
        expected = json.loads(PLAN_FIXTURE.read_text(encoding="utf-8"))
        lf = plan_mutations(source, max_mutants=256)
        crlf = plan_mutations(source.replace("\n", "\r\n"), max_mutants=256)

        self.assertEqual(lf["locator_version"], MUTATION_LOCATOR_VERSION)
        self.assertEqual(expected["locator_version"], MUTATION_LOCATOR_VERSION)
        self.assertEqual(
            [item.id for item in lf["mutations"]],
            expected["mutation_ids"],
        )
        self.assertEqual(lf["plan_hash"], expected["plan_hash"])
        self.assertEqual(
            [item.id for item in crlf["mutations"]],
            [item.id for item in lf["mutations"]],
        )
        self.assertEqual(
            [
                {
                    **item,
                    "original": item["original"].replace("\r\n", "\n"),
                    "replacement": item["replacement"].replace("\r\n", "\n"),
                }
                for item in mutation_records(crlf)
            ],
            mutation_records(lf),
        )
        self.assertTrue(all(
            item.id.startswith(MUTATION_OPERATOR_VERSION + ":")
            for item in lf["mutations"]
        ))
        self.assertNotEqual(lf["source_sha256"], crlf["source_sha256"])
        for mutation in lf["mutations"]:
            self.assertEqual(source[mutation.start:mutation.end], mutation.original)
            line_start = source.rfind("\n", 0, mutation.start)
            self.assertEqual(mutation.line, source.count("\n", 0, mutation.start) + 1)
            self.assertEqual(mutation.column, mutation.start - line_start)

        with tempfile.TemporaryDirectory() as temp:
            utf8_dir = Path(temp) / "题目"
            utf8_dir.mkdir()
            copied = utf8_dir / SOURCE_FIXTURE.name
            copied.write_text(source, encoding="utf-8", newline="")
            from_utf8_path = plan_mutations(copied.read_text(encoding="utf-8"))
        self.assertEqual(mutation_records(from_utf8_path), mutation_records(lf))

    def test_ast_plan_excludes_non_runtime_syntax(self):
        source = SOURCE_FIXTURE.read_text(encoding="utf-8")
        records = mutation_records(plan_mutations(source))
        lines = {item["line"] for item in records}

        self.assertNotIn(5, lines)   # macro body
        self.assertNotIn(7, lines)   # template parameter default
        self.assertNotIn(9, lines)   # requires expression
        self.assertNotIn(17, lines)  # spaceship expression
        self.assertNotIn(20, lines)  # static_assert
        self.assertNotIn(26, lines)  # template argument expression
        self.assertNotIn(27, lines)  # if constexpr condition
        self.assertIn(15, lines)     # operator< function body
        self.assertIn(16, lines)     # operator! function body comparison
        self.assertIn(28, lines)     # executable if constexpr body
        self.assertNotIn(30, lines)  # noexcept operand
        self.assertFalse(any(
            item["line"] == 32 and item["column"] < 29
            for item in records
        ))  # case label
        self.assertIn(32, lines)     # executable case body
        self.assertIn(36, lines)     # ordinary multi-line if condition

    def test_noexcept_and_case_labels_do_not_produce_candidates(self):
        source = (
            "int f(int x) {\n"
            "  static_assert(noexcept(x < 3));\n"
            "  switch (x) { case (1 < 2): return x < 4; default: return 0; }\n"
            "}\n"
        )
        records = mutation_records(plan_mutations(source))
        self.assertEqual(
            [(item["line"], item["column"], item["operator"], item["original"]) for item in records],
            [
                (3, 39, "comparison-boundary", "<"),
                (3, 41, "integer-boundary", "4"),
                (3, 41, "integer-boundary", "4"),
            ],
        )

    def test_old_id_is_preserved_for_unchanged_runtime_expression(self):
        source = "int f(int x) { return x <= 3 ? 1 : 0; }\n"
        ids = [item.id for item in plan_mutations(source)["mutations"]]
        self.assertEqual(ids, [
            "cpp-token-v1:comparison-boundary:1:25:e42035632288c6a1",
            "cpp-token-v1:integer-boundary:1:28:0e315b4334f3ff07",
            "cpp-token-v1:integer-boundary:1:28:5612be40c1831dac",
        ])

    def test_invalid_syntax_fails_closed_without_token_fallback(self):
        with self.assertRaises(ProbHubError) as raised:
            plan_mutations("int f( { return x < 3; }\n")
        self.assertEqual(raised.exception.code, "mutation_syntax_invalid")
        self.assertIn("line", str(raised.exception))

    def test_unsupported_typeid_type_form_fails_closed(self):
        with self.assertRaises(ProbHubError) as raised:
            plan_mutations(
                "#include <typeinfo>\n"
                "int f(int x) { return typeid(x < 3) == typeid(bool); }\n"
            )
        self.assertEqual(raised.exception.code, "mutation_syntax_invalid")

    def test_missing_or_mismatched_parser_is_structured(self):
        _cpp_language.cache_clear()
        try:
            with patch(
                "probhub.mutation_syntax.metadata.version",
                side_effect=metadata.PackageNotFoundError("tree-sitter"),
            ):
                with self.assertRaises(ProbHubError) as missing:
                    plan_mutations("int f() { return 1 < 2; }\n")
            self.assertEqual(missing.exception.code, "mutation_parser_unavailable")

            with patch(
                "probhub.mutation_syntax.metadata.version",
                side_effect=["0.0.0", TREE_SITTER_CPP_VERSION],
            ):
                with self.assertRaises(ProbHubError) as mismatched:
                    plan_mutations("int f() { return 1 < 2; }\n")
            self.assertEqual(mismatched.exception.code, "mutation_parser_unavailable")
        finally:
            _cpp_language.cache_clear()


if __name__ == "__main__":
    unittest.main()
