import copy
import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from probhub.io import write_yaml
from probhub.linting import lint_workspace
from probhub.solutions import (
    analyze_solution_verification,
    configured_solution_entries,
    normalize_run_on,
    normalize_solution_entries,
    select_run_cases,
)
from probhub.workspace import load_workspace


ROOT = Path(__file__).resolve().parents[1]
LOCAL_JUDGE = ROOT / "scripts/local_judge.py"


def diagnostic_codes(result, severity=None):
    return {
        item["code"]
        for item in result["diagnostics"]
        if severity is None or item["severity"] == severity
    }


class SolutionVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.problem = self.root / "A"
        (self.problem / "code").mkdir(parents=True)
        (self.problem / "data/sample").mkdir(parents=True)
        (self.problem / "data/secret").mkdir(parents=True)
        self.write_source("std.cpp", "// primary\nint main(){return 0;}\n")
        self.write_source("std2.cpp", "// secondary\nint main(){return 0;}\n")
        self.write_source("brute.cpp", "// brute\nint main(){return 0;}\n")
        self.write_source("validator.cpp", "int main(){return 0;}\n")
        self.add_case("sample", "basic")
        self.config = {
            "schema_version": 1,
            "id": "A",
            "name": "Verification",
            "difficulty": None,
            "limits": {"time": 1, "memory": 256, "output": 64, "processes": 32},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {"accepted": ["code/std.cpp"], "brute": [], "wrong": []},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret", "groups": []},
        }

    def write_source(self, name, text):
        (self.problem / "code" / name).write_text(text, encoding="utf-8")

    def add_case(self, suite, name, value="1\n"):
        directory = self.problem / "data" / suite
        (directory / f"{name}.in").write_text(value, encoding="utf-8")
        (directory / f"{name}.ans").write_text(value, encoding="utf-8")

    def analyze(self, changes=None):
        config = copy.deepcopy(self.config)
        if changes:
            changes(config)
        return analyze_solution_verification(self.problem, config)

    @staticmethod
    def independent(file="code/std2.cpp", reference="code/std.cpp"):
        return {
            "file": file,
            "expected": {"status": "AC", "all": True},
            "independence": {
                "from": reference,
                "basis": "algorithm",
                "note": "Uses an independently reviewed dynamic program.",
            },
        }

    def configure_groups(self, config, *groups):
        config["data"]["groups"] = list(groups)

    def test_analysis_shape_is_stable_and_never_claims_verification(self):
        result = self.analyze()
        self.assertEqual(result["analysis_state"], "declaration_and_static_checks")
        self.assertFalse(result["verified"])
        self.assertEqual(result["difficulty"]["value"], None)
        self.assertEqual(result["accepted"]["primary"], "code/std.cpp")
        self.assertEqual(result["brute_coverage"]["state"], "missing")
        self.assertFalse(result["requires_review"])
        for field in ("diagnostics", "errors", "warnings"):
            self.assertIsInstance(result[field], list)

    def test_difficulty_accepts_null_and_integer_boundaries(self):
        for value, high in ((None, False), (0, False), (4, True), (5, True)):
            with self.subTest(value=value):
                result = self.analyze(lambda config: config.update({
                    "difficulty": value,
                    "solutions": {
                        **config["solutions"],
                        "brute": [{
                            "file": "code/brute.cpp",
                            "expected": {"status": "AC", "all": True},
                        }],
                    },
                }))
                self.assertNotIn("invalid_difficulty", diagnostic_codes(result))
                self.assertEqual(result["difficulty"]["is_high"], high)

    def test_difficulty_rejects_bool_string_float_and_out_of_range(self):
        for value in (True, False, "4", 4.0, -1, 6):
            with self.subTest(value=value):
                result = self.analyze(lambda config, value=value: config.update({"difficulty": value}))
                self.assertIn("invalid_difficulty", diagnostic_codes(result, "error"))
                self.assertTrue(any("[invalid_difficulty]" in item for item in result["errors"]))
                self.assertFalse(result["difficulty"]["is_high"])

    def test_non_json_yaml_difficulty_is_rendered_json_safely(self):
        result = self.analyze(lambda config: config.update({
            "difficulty": datetime.date(2026, 7, 28),
        }))
        json.dumps(result)
        self.assertEqual(result["difficulty"]["value"], "2026-07-28")
        diagnostic = next(
            item for item in result["diagnostics"]
            if item["code"] == "invalid_difficulty"
        )
        self.assertEqual(diagnostic["value"], "2026-07-28")

    def test_expected_all_must_be_boolean_and_is_not_coerced(self):
        result = self.analyze(lambda config: config["solutions"].update({
            "brute": [{
                "file": "code/brute.cpp",
                "expected": {"status": "AC", "all": "false"},
            }],
        }))
        self.assertIn("invalid_expected_all", diagnostic_codes(result, "error"))
        entry = configured_solution_entries({
            "solutions": {
                "brute": [{
                    "file": "code/brute.cpp",
                    "expected": {"status": "AC", "all": "false"},
                }],
            },
        })["brute"][0]
        self.assertEqual(entry["expected"]["all"], "false")
        self.assertEqual(entry["expected_all_error"], "expected.all must be boolean")

    def test_expected_schema_errors_match_the_lint_contract(self):
        cases = (
            ("AC", "expected", "solutions.brute.expected must be a mapping"),
            ({}, "status", "expected.status is required for code/brute.cpp"),
            ({"status": []}, "status", "expected.status must not be empty for code/brute.cpp"),
            ({"status": ""}, "status", "expected.status must not be empty for code/brute.cpp"),
            ({"status": 42}, "status", "expected.status must be a status or list for code/brute.cpp"),
            ({"status": "BOOM"}, "status", "invalid expected status for code/brute.cpp: BOOM"),
            (
                {"status": "AC", "forbid": 42},
                "forbid",
                "expected.forbid must be a status or list for code/brute.cpp",
            ),
            (
                {"status": "AC", "forbid": "BOOM"},
                "forbid",
                "invalid expected forbid for code/brute.cpp: BOOM",
            ),
            (
                {"status": "AC", "groups": 42},
                "groups",
                "expected.groups must be a name or list for code/brute.cpp",
            ),
            (
                {"status": "AC", "groups": ""},
                "groups",
                "expected.groups must be a name or list for code/brute.cpp",
            ),
            (
                {"status": "AC", "groups": ["small", 1]},
                "groups",
                "expected.groups must be a name or list for code/brute.cpp",
            ),
            (
                {"status": "AC", "groups": ["missing"]},
                "groups",
                "unknown expected groups: missing",
            ),
        )
        for expected, field, message in cases:
            with self.subTest(expected=expected):
                result = self.analyze(lambda config, expected=expected: (
                    self.configure_groups(config, {
                        "name": "small",
                        "patterns": ["sample/*"],
                    }),
                    config["solutions"].update({"brute": [{
                        "file": "code/brute.cpp",
                        "expected": expected,
                    }]}),
                ))
                diagnostic = next(
                    item for item in result["diagnostics"]
                    if item["code"] == "invalid_solution_expected"
                    and item.get("field") == field
                )
                self.assertEqual(diagnostic["message"], message)

    def test_valid_expected_schema_keeps_legacy_defaults_and_casefolding(self):
        result = self.analyze(lambda config: (
            self.configure_groups(config, {
                "name": "small",
                "patterns": ["sample/*"],
            }),
            config["solutions"].update({"brute": [
                {"file": "code/brute.cpp"},
                {
                    "file": "code/std2.cpp",
                    "expected": {
                        "status": ["ac", "TLE"],
                        "forbid": "fail",
                        "groups": "small",
                        "all": False,
                    },
                },
            ]}),
        ))
        self.assertNotIn("invalid_solution_expected", diagnostic_codes(result, "error"))
        self.assertNotIn("invalid_expected_all", diagnostic_codes(result, "error"))

    def test_direct_judge_rejects_malformed_expected_before_compile(self):
        config = copy.deepcopy(self.config)
        config["solutions"]["brute"] = [{
            "file": "code/brute.cpp",
            "expected": "AC",
        }]
        write_yaml(self.problem / "probhub.yaml", config)
        completed = subprocess.run(
            [sys.executable, str(LOCAL_JUDGE), str(self.problem), "--jsonl", "--no-cache"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        events = [
            json.loads(line)
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(events[-1]["code"], "invalid_solution_expected")
        self.assertEqual(events[-1]["diagnostic"]["field"], "expected")
        self.assertFalse(any(event.get("type") == "compile" for event in events))

    def test_high_difficulty_warning_and_full_range_reference_matrix(self):
        def high(config):
            config["difficulty"] = 4

        warning = self.analyze(high)
        self.assertIn("single_accepted_high_difficulty", diagnostic_codes(warning, "warning"))
        self.assertTrue(warning["requires_review"])

        low = self.analyze(lambda config: config.update({"difficulty": 3}))
        self.assertNotIn("single_accepted_high_difficulty", diagnostic_codes(low))

        two_accepted = self.analyze(lambda config: (
            config.update({"difficulty": 4}),
            config["solutions"].update({"accepted": ["code/std.cpp", self.independent()]}),
        ))
        self.assertNotIn("single_accepted_high_difficulty", diagnostic_codes(two_accepted))
        self.assertEqual(two_accepted["accepted"]["full_range_count"], 2)

        full_brute = self.analyze(lambda config: (
            config.update({"difficulty": 4}),
            config["solutions"].update({"brute": [{
                "file": "code/brute.cpp",
                "expected": {"status": "AC", "all": True},
            }]}),
        ))
        self.assertNotIn("single_accepted_high_difficulty", diagnostic_codes(full_brute))
        self.assertEqual(full_brute["brute_coverage"]["state"], "full")

        alternative_ac = self.analyze(lambda config: (
            config.update({"difficulty": 5}),
            config["solutions"].update({"brute": [{
                "file": "code/brute.cpp",
                "expected": {"status": ["AC", "TLE"], "all": True},
            }]}),
        ))
        self.assertIn("single_accepted_high_difficulty", diagnostic_codes(alternative_ac))

        partial = self.independent()
        partial["run_on"] = ["small"]
        partial["expected"] = {"status": "AC", "groups": ["small"], "all": True}
        partial_accepted = self.analyze(lambda config: (
            config.update({"difficulty": 4}),
            self.configure_groups(config, {"name": "small", "patterns": ["sample/*"]}),
            config["solutions"].update({"accepted": ["code/std.cpp", partial]}),
        ))
        self.assertIn("single_accepted_high_difficulty", diagnostic_codes(partial_accepted))
        self.assertEqual(partial_accepted["accepted"]["full_range_count"], 1)

        restricted_brute = self.analyze(lambda config: (
            config.update({"difficulty": 4}),
            self.configure_groups(config, {"name": "small", "patterns": ["sample/*"]}),
            config["solutions"].update({"brute": [{
                "file": "code/brute.cpp",
                "run_on": ["small"],
                "expected": {"status": "AC", "all": True},
            }]}),
        ))
        self.assertIn("single_accepted_high_difficulty", diagnostic_codes(restricted_brute))

    def test_group_scoped_ac_brute_is_not_a_full_range_reference(self):
        self.add_case("secret", "large1")

        explicit = self.analyze(lambda config: (
            config.update({"difficulty": 4}),
            self.configure_groups(config, {
                "name": "small",
                "patterns": ["sample/*"],
            }),
            config["solutions"].update({"brute": [{
                "file": "code/brute.cpp",
                "expected": {
                    "status": "AC",
                    "all": True,
                    "groups": ["small"],
                },
            }]}),
        ))
        self.assertIn("single_accepted_high_difficulty", diagnostic_codes(explicit))
        self.assertEqual(explicit["brute_coverage"]["state"], "partial")

        implicit = self.analyze(lambda config: (
            config.update({"difficulty": 4}),
            self.configure_groups(config, {
                "name": "small",
                "patterns": ["sample/*"],
                "targets": ["code/brute.cpp"],
            }),
            config["solutions"].update({"brute": [{
                "file": "code/brute.cpp",
                "expected": {"status": "AC", "all": True},
            }]}),
        ))
        self.assertIn("single_accepted_high_difficulty", diagnostic_codes(implicit))
        self.assertEqual(implicit["brute_coverage"]["state"], "partial")

    def test_unavailable_ac_brute_is_not_a_full_range_reference(self):
        for program, path_code in (
            ("code/missing.cpp", "solution_missing"),
            ("../outside.cpp", "solution_source_invalid"),
        ):
            with self.subTest(program=program):
                result = self.analyze(lambda config, program=program: (
                    config.update({"difficulty": 4}),
                    config["solutions"].update({"brute": [{
                        "file": program,
                        "expected": {"status": "AC", "all": True},
                    }]}),
                ))
                self.assertIn(path_code, diagnostic_codes(result, "error"))
                self.assertIn(
                    "single_accepted_high_difficulty",
                    diagnostic_codes(result, "warning"),
                )
                self.assertEqual(result["brute_coverage"]["state"], "partial")

    def test_run_on_normalization_and_sample_implicit_group_union(self):
        self.assertIsNone(normalize_run_on("code/std.cpp"))
        self.assertEqual(normalize_run_on({"run_on": "small"}), ["small"])
        self.assertEqual(normalize_run_on({"run_on": ["small", "edge"]}), ["small", "edge"])
        entries = normalize_solution_entries({
            "solutions": {"brute": [{"file": "code/brute.cpp", "run_on": ["small", "edge"]}]}
        })
        testcases = [
            {"suite": "sample", "case": "sample/basic", "groups": []},
            {"suite": "secret", "case": "secret/small", "groups": ["small"]},
            {"suite": "secret", "case": "secret/edge", "groups": ["edge"]},
            {"suite": "secret", "case": "secret/both", "groups": ["small", "edge"]},
            {"suite": "secret", "case": "secret/other", "groups": ["other"]},
        ]
        executed, skipped = select_run_cases(entries["brute"][0], testcases)
        self.assertEqual(
            [case["case"] for case in executed],
            ["sample/basic", "secret/small", "secret/edge", "secret/both"],
        )
        self.assertEqual([case["case"] for case in skipped], ["secret/other"])

    def test_configured_solution_entries_is_the_stable_normalized_api(self):
        configured = configured_solution_entries(self.config)
        normalized = normalize_solution_entries(self.config)
        self.assertEqual(configured, normalized)
        self.assertEqual(configured["std"][0]["file"], "code/std.cpp")

    def test_run_on_rejects_invalid_empty_duplicate_unknown_and_no_case_groups(self):
        def base(config):
            self.configure_groups(
                config,
                {"name": "small", "patterns": ["sample/*"]},
                {"name": "empty", "patterns": ["secret/never*"]},
            )

        cases = (
            (42, "invalid_solution_run_on"),
            ([], "invalid_solution_run_on"),
            (["small", "small"], "invalid_solution_run_on"),
            (["unknown"], "solution_run_on_unknown_group"),
            (["empty"], "solution_run_on_has_no_cases"),
        )
        for run_on, code in cases:
            with self.subTest(run_on=run_on):
                result = self.analyze(lambda config, run_on=run_on: (
                    base(config),
                    config["solutions"].update({"brute": [{
                        "file": "code/brute.cpp",
                        "run_on": run_on,
                    }]}),
                ))
                self.assertIn(code, diagnostic_codes(result, "error"))

    def test_primary_run_on_is_forbidden(self):
        result = self.analyze(lambda config: (
            self.configure_groups(config, {"name": "small", "patterns": ["sample/*"]}),
            config["solutions"].update({"accepted": [{
                "file": "code/std.cpp",
                "run_on": "small",
                "expected": {"status": "AC", "all": True},
            }]}),
        ))
        self.assertIn("primary_accepted_run_on_forbidden", diagnostic_codes(result, "error"))

    def test_secondary_run_on_requires_expected_groups(self):
        secondary = self.independent()
        secondary["run_on"] = ["small"]
        secondary["expected"] = {"status": "AC", "all": True}
        result = self.analyze(lambda config: (
            self.configure_groups(config, {"name": "small", "patterns": ["sample/*"]}),
            config["solutions"].update({"accepted": ["code/std.cpp", secondary]}),
        ))
        self.assertIn("partial_accepted_expected_groups_required", diagnostic_codes(result, "error"))

    def test_run_on_must_cover_every_expectation_case(self):
        self.add_case("secret", "small1")
        self.add_case("secret", "large1")
        secondary = self.independent()
        secondary["run_on"] = ["small"]
        secondary["expected"] = {"status": "AC", "groups": ["large"], "all": True}
        result = self.analyze(lambda config: (
            self.configure_groups(
                config,
                {"name": "small", "patterns": ["secret/small*"]},
                {"name": "large", "patterns": ["secret/large*"]},
            ),
            config["solutions"].update({"accepted": ["code/std.cpp", secondary]}),
        ))
        self.assertIn("solution_run_on_expectation_gap", diagnostic_codes(result, "error"))
        gap = next(item for item in result["diagnostics"] if item["code"] == "solution_run_on_expectation_gap")
        self.assertEqual(gap["missing_cases"], ["secret/large1"])

    def test_run_on_must_cover_target_cases_even_with_explicit_expected_groups(self):
        self.add_case("secret", "small1")
        self.add_case("secret", "ordinary1")
        secondary = self.independent()
        secondary["run_on"] = ["small"]
        secondary["expected"] = {"status": "AC", "groups": ["small"], "all": True}
        result = self.analyze(lambda config: (
            self.configure_groups(
                config,
                {"name": "small", "patterns": ["secret/small*"]},
                {
                    "name": "ordinary",
                    "patterns": ["secret/ordinary*"],
                    "targets": ["code/std2.cpp"],
                },
            ),
            config["solutions"].update({"accepted": ["code/std.cpp", secondary]}),
        ))
        self.assertIn("solution_run_on_target_gap", diagnostic_codes(result, "error"))
        gap = next(
            item for item in result["diagnostics"]
            if item["code"] == "solution_run_on_target_gap"
        )
        self.assertEqual(gap["target_groups"], ["ordinary"])
        self.assertEqual(gap["missing_cases"], ["secret/ordinary1"])

    def test_valid_independence_is_recorded_as_unverified_review_evidence(self):
        for basis in ("algorithm", "key_implementation"):
            with self.subTest(basis=basis):
                secondary = self.independent()
                secondary["independence"]["basis"] = basis
                result = self.analyze(lambda config, secondary=secondary: config["solutions"].update({
                    "accepted": ["code/std.cpp", secondary]
                }))
                self.assertNotIn("invalid_independence_declaration", diagnostic_codes(result))
                self.assertEqual(len(result["accepted"]["independence_claims"]), 1)
                self.assertFalse(result["accepted"]["independence_claims"][0]["verified"])
                self.assertFalse(result["verified"])
                self.assertTrue(result["requires_review"])

    def test_missing_independence_is_a_non_blocking_warning(self):
        result = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", "code/std2.cpp"]
        }))
        self.assertIn("independent_accepted_unverified", diagnostic_codes(result, "warning"))
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["requires_review"])

    def test_invalid_independence_fields_are_errors_and_require_review(self):
        invalid_claims = (
            {"from": "code/std2.cpp", "basis": "algorithm", "note": "self"},
            {"from": "code/missing.cpp", "basis": "algorithm", "note": "unknown"},
            {"from": "code/std.cpp", "basis": "renaming", "note": "bad basis"},
            {"from": "code/std.cpp", "basis": "algorithm", "note": ""},
        )
        for claim in invalid_claims:
            with self.subTest(claim=claim):
                secondary = self.independent()
                secondary["independence"] = claim
                result = self.analyze(lambda config, secondary=secondary: config["solutions"].update({
                    "accepted": ["code/std.cpp", secondary]
                }))
                self.assertIn("invalid_independence_declaration", diagnostic_codes(result, "error"))
                self.assertTrue(result["requires_review"])

        valid = self.independent()["independence"]
        for field in ("from", "basis", "note"):
            for value in (True, ["value"], {"value": "text"}):
                with self.subTest(field=field, value=value):
                    secondary = self.independent()
                    secondary["independence"] = {**valid, field: value}
                    result = self.analyze(
                        lambda config, secondary=secondary: config["solutions"].update({
                            "accepted": ["code/std.cpp", secondary]
                        })
                    )
                    self.assertIn(
                        "invalid_independence_declaration",
                        diagnostic_codes(result, "error"),
                    )

    def test_independence_from_is_casefolded_to_the_configured_path(self):
        self.write_source(
            "std2.cpp",
            (self.problem / "code/std.cpp").read_text(encoding="utf-8"),
        )
        secondary = self.independent(reference="CODE/STD.CPP")
        result = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", secondary]
        }))
        claim = result["accepted"]["independence_claims"][0]
        self.assertEqual(claim["from"], "code/std.cpp")
        self.assertIn("independent_accepted_identical", diagnostic_codes(result, "error"))

    def test_duplicate_accepted_paths_are_casefolded(self):
        result = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", "CODE/STD.CPP"]
        }))
        self.assertIn("duplicate_accepted_solution", diagnostic_codes(result, "error"))
        self.assertEqual(result["accepted"]["declared_count"], 2)
        self.assertEqual(result["accepted"]["unique_count"], 1)

    def test_solution_entry_identity_is_casefold_unique_within_each_kind(self):
        cases = (
            (
                "accepted",
                "std",
                ["code/std.cpp", "CODE/STD.CPP"],
                "duplicate_accepted_solution",
            ),
            (
                "brute",
                "brute",
                ["code/brute.cpp", "CODE/BRUTE.CPP"],
                "duplicate_solution_entry",
            ),
            (
                "wrong",
                "wrong",
                ["code/std2.cpp", "CODE/STD2.CPP"],
                "duplicate_solution_entry",
            ),
        )
        for config_kind, kind, programs, code in cases:
            with self.subTest(config_kind=config_kind):
                result = self.analyze(
                    lambda config, config_kind=config_kind, programs=programs: config["solutions"].update({
                        config_kind: programs
                    })
                )
                diagnostic = next(
                    item for item in result["diagnostics"]
                    if item["code"] == code and item.get("kind") == kind
                )
                self.assertEqual(diagnostic["reference"], programs[0])
                self.assertEqual(diagnostic["program"], programs[1])

    def test_same_solution_path_remains_valid_across_different_kinds(self):
        result = self.analyze(lambda config: config.update({
            "solutions": {
                "accepted": ["code/std.cpp"],
                "brute": ["code/std.cpp"],
                "wrong": ["code/std.cpp"],
            },
        }))
        self.assertNotIn("duplicate_accepted_solution", diagnostic_codes(result, "error"))
        self.assertNotIn("duplicate_solution_entry", diagnostic_codes(result, "error"))

    def test_all_solution_source_paths_cannot_escape_or_be_absolute(self):
        outside = self.root / "outside.cpp"
        outside.write_text("int main(){return 0;}\n", encoding="utf-8")
        paths = ("../outside.cpp", outside.as_posix(), "C:/outside/std.cpp")
        kinds = (("accepted", "std"), ("brute", "brute"), ("wrong", "wrong"))
        for (config_kind, normalized_kind), program in (
            (kind, program) for kind in kinds for program in paths
        ):
            with self.subTest(config_kind=config_kind, program=program):
                result = self.analyze(
                    lambda config, config_kind=config_kind, program=program: config["solutions"].update({
                        config_kind: [program]
                    })
                )
                diagnostic = next(
                    item for item in result["diagnostics"]
                    if item["code"] == "solution_source_invalid"
                    and item.get("kind") == normalized_kind
                )
                self.assertEqual(diagnostic["program"], program.replace("\\", "/"))

    def test_solution_source_must_not_be_a_symlink(self):
        link = self.problem / "code/link.cpp"
        try:
            link.symlink_to(self.problem / "code/std.cpp")
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        for config_kind in ("accepted", "brute", "wrong"):
            with self.subTest(config_kind=config_kind):
                result = self.analyze(
                    lambda config, config_kind=config_kind: config["solutions"].update({
                        config_kind: ["code/link.cpp"]
                    })
                )
                self.assertIn("solution_source_invalid", diagnostic_codes(result, "error"))

    def test_identical_accepted_bytes_are_a_deterministic_error(self):
        self.write_source("std2.cpp", (self.problem / "code/std.cpp").read_text(encoding="utf-8"))
        result = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", self.independent()]
        }))
        self.assertIn("independent_accepted_identical", diagnostic_codes(result, "error"))

    def test_identical_bytes_are_checked_without_independence_declarations(self):
        self.write_source(
            "std2.cpp",
            (self.problem / "code/std.cpp").read_text(encoding="utf-8"),
        )
        result = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", "code/std2.cpp"]
        }))
        self.assertIn("independent_accepted_identical", diagnostic_codes(result, "error"))

    def test_every_pair_of_three_accepted_is_checked(self):
        self.write_source(
            "std3.cpp",
            (self.problem / "code/std2.cpp").read_text(encoding="utf-8"),
        )
        identical = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", "code/std2.cpp", "code/std3.cpp"]
        }))
        evidence = next(
            item for item in identical["diagnostics"]
            if item["code"] == "independent_accepted_identical"
        )
        self.assertEqual(
            (evidence["program"], evidence["reference"]),
            ("code/std3.cpp", "code/std2.cpp"),
        )

        self.write_source("std3.cpp", '#include "std2.cpp"\nint third(){return 0;}\n')
        included = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", "code/std2.cpp", "code/std3.cpp"]
        }))
        evidence = next(
            item for item in included["diagnostics"]
            if item["code"] == "independent_accepted_includes_reference"
        )
        self.assertEqual(
            (evidence["program"], evidence["reference"]),
            ("code/std3.cpp", "code/std2.cpp"),
        )

    def test_direct_include_of_reference_is_a_deterministic_error(self):
        self.write_source("std2.cpp", '#include "std.cpp"\nint second(){return 0;}\n')
        result = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", self.independent()]
        }))
        self.assertIn("independent_accepted_includes_reference", diagnostic_codes(result, "error"))

    def test_include_scanner_ignores_comments_raw_strings_and_disabled_code(self):
        self.write_source("std2.cpp", r'''
// #include "std.cpp"
/*
#include "std.cpp"
*/
const char* text = R"tag(
#include "std.cpp"
)tag";
#if 0
#include "std.cpp"
#endif
#if 1
int enabled = 1;
#else
#include "std.cpp"
#endif
int second(){return enabled;}
''')
        ignored = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", "code/std2.cpp"]
        }))
        self.assertNotIn(
            "independent_accepted_includes_reference",
            diagnostic_codes(ignored, "error"),
        )

        with_real_include = (
            (self.problem / "code/std2.cpp").read_text(encoding="utf-8")
            + '\n#include "std.cpp"\n'
        )
        self.write_source("std2.cpp", with_real_include)
        detected = self.analyze(lambda config: config["solutions"].update({
            "accepted": ["code/std.cpp", "code/std2.cpp"]
        }))
        self.assertIn(
            "independent_accepted_includes_reference",
            diagnostic_codes(detected, "error"),
        )

    def test_lint_workspace_exposes_diagnostics_and_fails_on_static_errors(self):
        (self.root / ".probhub").mkdir()
        write_yaml(
            self.root / ".probhub/workspace.yaml",
            {"schema_version": 1, "problems": [{"id": "A", "directory": "A"}]},
        )
        (self.problem / "problem.md").write_text(
            "# Verification\n\n## 题目描述\n\nD.\n\n"
            "## 输入格式\n\nI.\n\n## 输出格式\n\nO.\n",
            encoding="utf-8",
        )
        config = copy.deepcopy(self.config)
        config["difficulty"] = True
        write_yaml(self.problem / "probhub.yaml", config)
        root, workspace = load_workspace(self.root)
        result = lint_workspace(root, workspace)
        problem = result["problems"][0]
        self.assertFalse(result["ok"])
        self.assertFalse(problem["ok"])
        self.assertIn("invalid_difficulty", {item["code"] for item in problem["diagnostics"]})
        self.assertIn("[invalid_difficulty] difficulty must be an integer from 0 to 5", problem["errors"])
        self.assertEqual(problem["solution_verification"]["analysis_state"], "declaration_and_static_checks")
        self.assertFalse(problem["solution_verification"]["verified"])


if __name__ == "__main__":
    unittest.main()
