import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

from probhub import mutation_syntax
from probhub.errors import ProbHubError
from probhub.mutation import MUTATION_OPERATOR_VERSION, plan_mutations
from probhub.mutation_syntax import (
    MUTATION_LOCATOR_VERSION,
    MUTATION_PARSER_MEMORY_LIMIT_MB,
    MUTATION_PARSER_OUTPUT_LIMIT_BYTES,
    MUTATION_PARSER_PROCESS_LIMIT,
    MUTATION_PARSER_PROTOCOL_VERSION,
    TREE_SITTER_CPP_VERSION,
    TREE_SITTER_VERSION,
    locate_cpp_mutation_syntax,
    mutation_parser_identity,
)
from probhub.process_control import ProcessCancelled, process_alive


FIXTURE_ROOT = Path(__file__).parent / "fixtures/mutation-plans"
SOURCE_FIXTURE = FIXTURE_ROOT / "cpp-syntax-v1.cpp"
PLAN_FIXTURE = FIXTURE_ROOT / "cpp-syntax-v1.json"


def mutation_records(plan):
    return [item.as_dict() for item in plan["mutations"]]


def simple_worker_response(source, *, protocol_version=MUTATION_PARSER_PROTOCOL_VERSION):
    operator = source.index("<")
    left = source.rfind("x", 0, operator)
    right = source.index("3", operator)
    span = lambda start, end, kind: {
        "start": start,
        "end": end,
        "text": source[start:end],
        "kind": kind,
    }
    return {
        "protocol_version": protocol_version,
        "ok": True,
        "operation": "locate-cpp-v1",
        "locator_version": MUTATION_LOCATOR_VERSION,
        "tree_sitter_version": TREE_SITTER_VERSION,
        "tree_sitter_cpp_version": TREE_SITTER_CPP_VERSION,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "locations": {
            "comparisons": [{
                "operator": span(operator, operator + 1, "<"),
                "left": span(left, left + 1, "identifier"),
                "right": span(right, right + 1, "number_literal"),
            }],
            "unary_not": [],
            "conditions": [],
        },
    }


def fake_worker_result(payload, *, reason="completed", returncode=0, stderr=b""):
    def run(_command, **kwargs):
        stdout = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        Path(kwargs["stdout_path"]).write_bytes(stdout)
        Path(kwargs["stderr_path"]).write_bytes(stderr)
        return {"reason": reason, "returncode": returncode, "message": reason}
    return run


class MutationSyntaxTests(unittest.TestCase):
    def test_pinned_parser_identity_is_explicit(self):
        identity = mutation_parser_identity()
        self.assertEqual(identity["locator_version"], MUTATION_LOCATOR_VERSION)
        self.assertEqual(identity["tree_sitter_version"], TREE_SITTER_VERSION)
        self.assertEqual(identity["tree_sitter_cpp_version"], TREE_SITTER_CPP_VERSION)

    def test_parent_invokes_bounded_worker_with_clean_environment(self):
        source = "int f(int x) { return x < 3; }\n"
        real_run = mutation_syntax.run_managed_to_files
        with (
            patch.dict(
                os.environ,
                {"PYTHONHOME": "bad", "PYTHONPATH": "shadow", "PYTHONSTARTUP": "startup.py"},
            ),
            patch.object(
                mutation_syntax,
                "run_managed_to_files",
                wraps=real_run,
            ) as run_managed,
        ):
            locations = locate_cpp_mutation_syntax(source)

        self.assertEqual(len(locations.comparisons), 1)
        command = run_managed.call_args.args[0]
        kwargs = run_managed.call_args.kwargs
        self.assertEqual(command[:2], [sys.executable, "-c"])
        self.assertIn("probhub.mutation_parser_worker", command[2])
        self.assertEqual(kwargs["memory_limit_mb"], MUTATION_PARSER_MEMORY_LIMIT_MB)
        self.assertEqual(kwargs["output_limit_bytes"], MUTATION_PARSER_OUTPUT_LIMIT_BYTES)
        self.assertEqual(kwargs["process_limit"], MUTATION_PARSER_PROCESS_LIMIT)
        self.assertNotEqual(Path(kwargs["cwd"]).resolve(), mutation_syntax._PACKAGE_ROOT.resolve())
        self.assertTrue(Path(kwargs["cwd"]).name.startswith("probhub-mutation-parser-"))
        self.assertNotIn("PYTHONHOME", kwargs["env"])
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertNotIn("PYTHONSTARTUP", kwargs["env"])
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_worker_resource_failures_and_crash_are_distinct(self):
        source = "int f(int x) { return x < 3; }\n"
        misleading = {
            "protocol_version": MUTATION_PARSER_PROTOCOL_VERSION,
            "ok": False,
            "error": {"code": "mutation_syntax_invalid", "message": "ignore me"},
        }
        for reason, code in (
            ("time_limit", "mutation_parser_timeout"),
            ("memory_limit", "mutation_parser_memory_limit"),
            ("output_limit", "mutation_parser_output_limit"),
            ("process_limit", "mutation_parser_process_limit"),
        ):
            with self.subTest(reason=reason), patch.object(
                mutation_syntax,
                "run_managed_to_files",
                side_effect=fake_worker_result(misleading, reason=reason, returncode=None),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    locate_cpp_mutation_syntax(source)
                self.assertEqual(raised.exception.code, code)

        with patch.object(
            mutation_syntax,
            "run_managed_to_files",
            side_effect=fake_worker_result(b"", returncode=7, stderr=b"native crash"),
        ):
            with self.assertRaises(ProbHubError) as crashed:
                locate_cpp_mutation_syntax(source)
        self.assertEqual(crashed.exception.code, "mutation_parser_crashed")
        self.assertIn("native crash", str(crashed.exception))

    def test_worker_cancellation_is_structured(self):
        with patch.object(
            mutation_syntax,
            "run_managed_to_files",
            side_effect=ProcessCancelled("cancelled"),
        ):
            with self.assertRaises(ProbHubError) as raised:
                locate_cpp_mutation_syntax("int f(int x) { return x < 3; }\n")
        self.assertEqual(raised.exception.code, "mutation_parser_cancelled")

    def test_worker_temporary_request_failure_is_structured(self):
        with patch(
            "probhub.mutation_syntax.tempfile.TemporaryDirectory",
            side_effect=OSError("temp unavailable"),
        ):
            with self.assertRaises(ProbHubError) as raised:
                locate_cpp_mutation_syntax("int f(int x) { return x < 3; }\n")
        self.assertEqual(raised.exception.code, "mutation_parser_start_failed")

    def test_invalid_json_protocol_version_and_spans_fail_closed(self):
        source = "int f(int x) { return x < 3; }\n"
        invalid_span = simple_worker_response(source)
        invalid_span["locations"]["comparisons"][0]["operator"]["start"] = len(source) + 1
        invalid_unary = simple_worker_response(source)
        invalid_unary["locations"]["unary_not"].append(
            invalid_unary["locations"]["comparisons"][0]["operator"]
        )
        invalid_operator = simple_worker_response(source)
        invalid_operator["locations"]["comparisons"][0]["operator"]["kind"] = "identifier"
        invalid_condition = simple_worker_response(source)
        invalid_condition["locations"]["conditions"].append({
            "keyword": "if",
            "condition": {
                "start": 0,
                "end": 1,
                "text": "i",
                "kind": "identifier",
            },
        })
        cases = [
            b"{truncated",
            simple_worker_response(source, protocol_version=2),
            simple_worker_response(source, protocol_version=True),
            invalid_span,
            invalid_unary,
            invalid_operator,
            invalid_condition,
            {
                "protocol_version": MUTATION_PARSER_PROTOCOL_VERSION,
                "ok": False,
                "error": {"code": "arbitrary_worker_code", "message": "not allowed"},
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload), patch.object(
                mutation_syntax,
                "run_managed_to_files",
                side_effect=fake_worker_result(payload, returncode=0),
            ):
                with self.assertRaises(ProbHubError) as raised:
                    locate_cpp_mutation_syntax(source)
                self.assertEqual(raised.exception.code, "mutation_parser_protocol_error")

    def test_worker_timeout_cleans_descendant_process(self):
        with tempfile.TemporaryDirectory() as temp:
            child_pid = Path(temp) / "child.pid"
            child_code = (
                "import os,pathlib,time;"
                f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()),encoding='utf-8');"
                "time.sleep(30)"
            )
            parent_code = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                "time.sleep(30)"
            )
            with patch.object(
                mutation_syntax,
                "_worker_command",
                return_value=[sys.executable, "-c", parent_code],
            ):
                with self.assertRaises(ProbHubError) as raised:
                    locate_cpp_mutation_syntax(
                        "int f(int x) { return x < 3; }\n",
                        timeout=1.0,
                    )
            self.assertEqual(raised.exception.code, "mutation_parser_timeout")
            self.assertTrue(child_pid.is_file(), "worker descendant did not start")
            pid = int(child_pid.read_text(encoding="utf-8"))
            deadline = time.time() + 10
            while process_alive(pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(process_alive(pid), f"worker descendant {pid} survived")

    def test_real_worker_output_flood_is_ole(self):
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 65536); sys.stdout.flush()",
        ]
        with (
            patch.object(mutation_syntax, "_worker_command", return_value=command),
            patch.object(mutation_syntax, "MUTATION_PARSER_OUTPUT_LIMIT_BYTES", 1024),
        ):
            with self.assertRaises(ProbHubError) as raised:
                locate_cpp_mutation_syntax("int f(int x) { return x < 3; }\n")
        self.assertEqual(raised.exception.code, "mutation_parser_output_limit")

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

    def test_typeid_type_forms_are_recovered_without_mutating_unevaluated_content(self):
        type_ids = (
            "bool",
            "const bool",
            "bool*",
            "bool[3]",
            "unsigned long long",
            "struct S",
            "struct 类型",
            "decltype(x < 3)",
            "void(*)(int)",
        )
        for type_id in type_ids:
            with self.subTest(type_id=type_id):
                source = (
                    "#include <typeinfo>\n"
                    "struct S {};\n"
                    "int f(int x) {\n"
                    f"  (void)typeid({type_id});\n"
                    "  return x < 4;\n"
                    "}\n"
                )
                records = mutation_records(plan_mutations(source))
                self.assertEqual(
                    [(item["line"], item["column"], item["operator"], item["original"]) for item in records],
                    [
                        (5, 12, "comparison-boundary", "<"),
                        (5, 14, "integer-boundary", "4"),
                        (5, 14, "integer-boundary", "4"),
                    ],
                )

    def test_typeid_type_recovery_preserves_existing_runtime_mutation_ids(self):
        expression_source = (
            "#include <typeinfo>\n"
            "int f(int x) {\n"
            "  (void)typeid(x);\n"
            "  return x <= 3;\n"
            "}\n"
        )
        type_source = expression_source.replace("typeid(x)", "typeid(int)")
        expression_ids = [item.id for item in plan_mutations(expression_source)["mutations"]]
        type_ids = [item.id for item in plan_mutations(type_source)["mutations"]]
        self.assertEqual(type_ids, expression_ids)

    def test_typeid_type_recovery_keeps_only_the_outer_runtime_comparison(self):
        records = mutation_records(plan_mutations(
            "#include <typeinfo>\n"
            "int f(int x) { return typeid(x < 3) == typeid(bool); }\n"
        ))
        self.assertEqual(
            [(item["line"], item["column"], item["operator"], item["original"]) for item in records],
            [(2, 37, "comparison-boundary", "==")],
        )

    def test_typeid_type_recovery_rejects_malformed_or_unrelated_errors(self):
        sources = (
            "int f(int x) { (void)typeid(bool +); return x < 4; }\n",
            "int f(int x) { (void)typeid(bool; int y); return x < 4; }\n",
            "int f(int x) { (void)typeid(bool); return x < ; }\n",
            "int f(int x) { (void)typeid(bool; return x < 4; }\n",
        )
        for source in sources:
            with self.subTest(source=source), self.assertRaises(ProbHubError) as raised:
                plan_mutations(source)
            self.assertEqual(raised.exception.code, "mutation_syntax_invalid")

    def test_missing_or_mismatched_parser_is_structured(self):
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


if __name__ == "__main__":
    unittest.main()
