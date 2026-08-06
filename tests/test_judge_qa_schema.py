import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from unittest.mock import Mock

from probhub.building import write_manifest
from probhub.generations import create_problem_checkpoint
from probhub.io import read_yaml, write_yaml
from probhub.judge_qa import (
    MAX_JUDGE_QA_CASES,
    MAX_JUDGE_QA_DIAGNOSTICS,
    MAX_JUDGE_QA_FILES,
    MAX_JUDGE_QA_FILE_BYTES,
    MAX_JUDGE_QA_ROBUSTNESS_PROBES,
    MAX_JUDGE_QA_TOTAL_BYTES,
    inspect_judge_qa,
)
from probhub.linting import (
    compute_collection_hash,
    compute_data_hash,
    compute_source_hash,
    compute_workspace_hash,
    lint_workspace,
)
from probhub.metadata import build_meta
from probhub.package_tools import (
    build_package,
    generate_domjudge_config,
    prepare_output_validator,
)
from probhub.workspace import load_problem, load_workspace, problem_entries
from tests.fixture_support import copy_workspace_fixture


class JudgeQASchemaTests(unittest.TestCase):
    def copy_fixture(self, name):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = copy_workspace_fixture(name, temporary.name)
        root, workspace = load_workspace(fixture.root)
        entry = problem_entries(workspace)[0]
        problem, config = load_problem(root, entry)
        return fixture, root, workspace, entry, problem, config

    @staticmethod
    def diagnostic_codes(report):
        return {diagnostic["code"] for diagnostic in report["diagnostics"]}

    def inspect_config(self, name, mutate):
        _, _, _, _, problem, config = self.copy_fixture(name)
        mutate(config)
        return inspect_judge_qa(problem, config)

    def test_checker_schema_resolves_formal_and_explicit_files(self):
        _, root, workspace, _, _, _ = self.copy_fixture("checker-qa")
        result = lint_workspace(root, workspace)
        self.assertTrue(result["ok"], result)
        report = result["problems"][0]["judge_qa"]
        self.assertTrue(report["configured"])
        self.assertTrue(report["applicable"])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["judge_type"], "custom")
        self.assertRegex(report["fixture_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["stats"]["cases"], 2)
        self.assertEqual(report["stats"]["files"], 6)
        self.assertEqual(
            {item["path"] for item in report["files"]},
            {
                "data/sample/basic.in",
                "data/sample/basic.ans",
                "judge-fixtures/checker/alternative.out",
                "judge-fixtures/checker/extra.in",
                "judge-fixtures/checker/extra.ans",
                "judge-fixtures/checker/extra.out",
            },
        )

    def test_schema_limits_and_fixture_hash_vectors_are_stable(self):
        self.assertEqual(MAX_JUDGE_QA_CASES, 128)
        self.assertEqual(MAX_JUDGE_QA_FILES, 256)
        self.assertEqual(MAX_JUDGE_QA_FILE_BYTES, 16 * 1024 * 1024)
        self.assertEqual(MAX_JUDGE_QA_TOTAL_BYTES, 64 * 1024 * 1024)
        self.assertEqual(MAX_JUDGE_QA_ROBUSTNESS_PROBES, 16)
        self.assertEqual(MAX_JUDGE_QA_DIAGNOSTICS, 128)
        expected = {
            "checker-qa": "7b5c879af93ad156a8d248ed1ee08be1db43fa119b6612f59d4f7bee5e646d41",
            "interactor-qa": "54957c98a1a4b9584489402a54c133430f746633a040124ee8970b2578e4c3c2",
        }
        for name, digest in expected.items():
            with self.subTest(name=name):
                _, _, _, _, problem, config = self.copy_fixture(name)
                self.assertEqual(inspect_judge_qa(problem, config)["fixture_hash"], digest)

    def test_diagnostic_and_robustness_probe_limits_are_bounded(self):
        _, _, _, _, problem, config = self.copy_fixture("checker-qa")
        config["judge"]["qa"]["robustness"]["probes"] = ["empty"] * 17
        report = inspect_judge_qa(problem, config)
        self.assertIn(
            "judge_qa_robustness_probe_limit_exceeded",
            self.diagnostic_codes(report),
        )

        _, _, _, _, problem, config = self.copy_fixture("checker-qa")
        for index in range(200):
            config["judge"]["qa"][f"unknown_{index}"] = True
        report = inspect_judge_qa(problem, config)
        self.assertEqual(len(report["diagnostics"]), MAX_JUDGE_QA_DIAGNOSTICS)
        self.assertEqual(report["diagnostics"][-1]["code"], "judge_qa_diagnostics_truncated")

    def test_interactor_schema_resolves_source_and_builtin_behaviors(self):
        _, root, workspace, _, _, _ = self.copy_fixture("interactor-qa")
        result = lint_workspace(root, workspace)
        self.assertTrue(result["ok"], result)
        report = result["problems"][0]["judge_qa"]
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["judge_type"], "interactive")
        self.assertEqual(report["stats"]["cases"], 4)
        self.assertEqual(report["stats"]["files"], 5)
        contestants = [case.get("contestant") for case in report["cases"]]
        self.assertIn({"source": "code/judge-qa/normal.cpp"}, contestants)
        self.assertIn({"behavior": "early-eof"}, contestants)
        self.assertIn({"behavior": "idle"}, contestants)
        self.assertIn({"behavior": "output-flood"}, contestants)

    def test_official_qa_programs_require_cpp_and_contestants_allow_python(self):
        _, _, _, _, problem, config = self.copy_fixture("interactor-qa")
        config["judge"]["interactor"] = "code/interactor.py"
        config["judge"]["qa"]["cases"][0]["contestant"]["source"] = (
            "code/judge-qa/player.exe"
        )
        (problem / "code" / "interactor.py").write_text("pass\n", encoding="utf-8")
        (problem / "code" / "judge-qa" / "player.exe").write_bytes(b"prebuilt")
        report = inspect_judge_qa(problem, config)
        diagnostics = [
            item for item in report["diagnostics"]
            if item["code"] == "judge_qa_program_type_unsupported"
        ]
        self.assertEqual(
            {item["field"] for item in diagnostics},
            {"judge.interactor", "judge.qa.cases[0].contestant.source"},
            diagnostics,
        )

    def test_missing_qa_is_compatible_and_not_reported_as_passed(self):
        _, root, workspace, _, _, _ = self.copy_fixture("custom")
        result = lint_workspace(root, workspace)
        self.assertTrue(result["ok"], result)
        report = result["problems"][0]["judge_qa"]
        self.assertFalse(report["configured"])
        self.assertTrue(report["applicable"])
        self.assertTrue(report["ok"])
        self.assertIsNone(report["fixture_hash"])
        self.assertEqual(report["diagnostics"], [])

    def test_lint_exposes_stable_judge_qa_diagnostic_codes(self):
        fixture, root, workspace, _, _, config = self.copy_fixture("checker-qa")
        config["judge"]["qa"]["schema_version"] = 2
        write_yaml(fixture.config_path, config)
        result = lint_workspace(root, workspace)
        self.assertFalse(result["ok"], result)
        problem_result = result["problems"][0]
        codes = {item["code"] for item in problem_result["diagnostics"]}
        self.assertIn("judge_qa_schema_version_unsupported", codes)
        self.assertTrue(any(
            error.startswith("[judge_qa_schema_version_unsupported]")
            for error in problem_result["errors"]
        ))

    def test_schema_and_mutual_exclusion_diagnostics_are_stable(self):
        mutations = {
            "judge_qa_schema_version_unsupported": (
                "checker-qa",
                lambda config: config["judge"]["qa"].update(schema_version=True),
            ),
            "judge_qa_unknown_field": (
                "checker-qa",
                lambda config: config["judge"]["qa"].update(typo=True),
            ),
            "judge_qa_case_id_duplicate": (
                "checker-qa",
                lambda config: config["judge"]["qa"]["cases"][1].update(
                    id="ACCEPTS-ALTERNATIVE"
                ),
            ),
            "judge_qa_case_reference_conflict": (
                "checker-qa",
                lambda config: config["judge"]["qa"]["cases"][1].update(
                    case="sample/basic"
                ),
            ),
            "judge_qa_case_reference_incomplete": (
                "checker-qa",
                lambda config: config["judge"]["qa"]["cases"][1].pop("jury_answer"),
            ),
            "judge_qa_expected_status_invalid_type": (
                "checker-qa",
                lambda config: config["judge"]["qa"]["cases"][0]["expected"].update(
                    status="FAIL"
                ),
            ),
            "judge_qa_expected_status_invalid": (
                "checker-qa",
                lambda config: config["judge"]["qa"]["cases"][0]["expected"].update(
                    status=[]
                ),
            ),
            "judge_qa_purpose_invalid": (
                "checker-qa",
                lambda config: config["judge"]["qa"]["cases"][0].pop("purpose"),
            ),
            "judge_qa_judge_type_unsupported": (
                "checker-qa",
                lambda config: config["judge"].update(type="standard"),
            ),
            "judge_qa_contestant_mode_conflict": (
                "interactor-qa",
                lambda config: config["judge"]["qa"]["cases"][0]["contestant"].update(
                    behavior="idle"
                ),
            ),
            "judge_qa_timeout_kind_without_tle": (
                "interactor-qa",
                lambda config: config["judge"]["qa"]["cases"][0]["expected"].update(
                    timeout_kind="idle"
                ),
            ),
            "judge_qa_timeout_kind_invalid_type": (
                "interactor-qa",
                lambda config: config["judge"]["qa"]["cases"][0]["expected"].update(
                    timeout_kind=[]
                ),
            ),
            "judge_qa_contestant_behavior_invalid_type": (
                "interactor-qa",
                lambda config: config["judge"]["qa"]["cases"][1]["contestant"].update(
                    behavior=[]
                ),
            ),
        }
        for code, (name, mutate) in mutations.items():
            with self.subTest(code=code):
                report = self.inspect_config(name, mutate)
                self.assertFalse(report["ok"], report)
                expected_code = {
                    "judge_qa_expected_status_invalid_type": "judge_qa_expected_status_invalid",
                    "judge_qa_timeout_kind_invalid_type": "judge_qa_timeout_kind_invalid",
                    "judge_qa_contestant_behavior_invalid_type": "judge_qa_contestant_behavior_invalid",
                }.get(code, code)
                self.assertIn(expected_code, self.diagnostic_codes(report), report)

    def test_resolver_race_returns_stable_diagnostic(self):
        _, _, _, _, problem, config = self.copy_fixture("checker-qa")
        broken_path = Mock()
        broken_path.resolve.side_effect = FileNotFoundError("injected race")
        with (
            patch("probhub.judge_qa._judge_qa_tree_paths", return_value=[]),
            patch("probhub.judge_qa.resolve_problem_regular_file", return_value=broken_path),
        ):
            report = inspect_judge_qa(problem, config)
        self.assertIn("judge_qa_fixture_changed", self.diagnostic_codes(report), report)

    def test_fixture_paths_reject_missing_outside_directory_and_wrong_scope(self):
        cases = {
            "judge_qa_fixture_path_invalid": "",
            "judge_qa_fixture_path_missing": "judge-fixtures/checker/missing.out",
            "judge_qa_fixture_path_outside": "../outside.out",
            "judge_qa_fixture_path_non_regular": "judge-fixtures/checker",
            "judge_qa_fixture_path_scope": "data/sample/basic.in",
        }
        for code, value in cases.items():
            with self.subTest(code=code):
                report = self.inspect_config(
                    "checker-qa",
                    lambda config, value=value: config["judge"]["qa"]["cases"][0].update(
                        contestant_output=value
                    ),
                )
                self.assertIn(code, self.diagnostic_codes(report), report)

    def test_fixture_paths_reject_links(self):
        _, _, _, _, problem, config = self.copy_fixture("checker-qa")
        target = problem / "judge-fixtures/checker/alternative.out"
        link = problem / "judge-fixtures/checker/linked.out"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")
        config["judge"]["qa"]["cases"][0]["contestant_output"] = (
            "judge-fixtures/checker/linked.out"
        )
        report = inspect_judge_qa(problem, config)
        self.assertIn("judge_qa_fixture_path_link", self.diagnostic_codes(report), report)

    @unittest.skipIf(os.name == "nt", "Windows cannot create case-colliding files")
    def test_fixture_paths_reject_windows_case_collisions(self):
        _, _, _, _, problem, config = self.copy_fixture("checker-qa")
        upper = problem / "judge-fixtures/checker/Output.out"
        lower = problem / "judge-fixtures/checker/output.out"
        upper.write_bytes(b"2\n")
        lower.write_bytes(b"2\n")
        cases = config["judge"]["qa"]["cases"]
        cases[0]["contestant_output"] = "judge-fixtures/checker/Output.out"
        cases[1]["contestant_output"] = "judge-fixtures/checker/output.out"
        report = inspect_judge_qa(problem, config)
        self.assertIn("judge_qa_fixture_path_collision", self.diagnostic_codes(report), report)

    def test_fixed_case_file_and_byte_limits_are_enforced(self):
        _, _, _, _, problem, config = self.copy_fixture("checker-qa")
        with patch("probhub.judge_qa.MAX_JUDGE_QA_CASES", 1):
            report = inspect_judge_qa(problem, config)
            self.assertIn("judge_qa_case_limit_exceeded", self.diagnostic_codes(report), report)
        with patch("probhub.judge_qa.MAX_JUDGE_QA_FILES", 5):
            report = inspect_judge_qa(problem, config)
            self.assertIn(
                "judge_qa_fixture_file_limit_exceeded",
                self.diagnostic_codes(report),
                report,
            )

        baseline = inspect_judge_qa(problem, config)
        largest = max(item["size"] for item in baseline["files"])
        total = baseline["stats"]["total_bytes"]
        with patch("probhub.judge_qa.MAX_JUDGE_QA_FILE_BYTES", largest):
            self.assertNotIn(
                "judge_qa_fixture_file_too_large",
                self.diagnostic_codes(inspect_judge_qa(problem, config)),
            )
        with patch("probhub.judge_qa.MAX_JUDGE_QA_FILE_BYTES", largest - 1):
            self.assertIn(
                "judge_qa_fixture_file_too_large",
                self.diagnostic_codes(inspect_judge_qa(problem, config)),
            )
        with patch("probhub.judge_qa.MAX_JUDGE_QA_TOTAL_BYTES", total):
            self.assertNotIn(
                "judge_qa_fixture_total_bytes_exceeded",
                self.diagnostic_codes(inspect_judge_qa(problem, config)),
            )
        with patch("probhub.judge_qa.MAX_JUDGE_QA_TOTAL_BYTES", total - 1):
            self.assertIn(
                "judge_qa_fixture_total_bytes_exceeded",
                self.diagnostic_codes(inspect_judge_qa(problem, config)),
            )

    def test_fixture_hash_and_checkpoint_track_exact_fixture_bytes(self):
        _, root, workspace, entry, problem, config = self.copy_fixture("checker-qa")
        fixture_path = problem / "judge-fixtures/checker/alternative.out"
        first_fixture_hash = inspect_judge_qa(problem, config)["fixture_hash"]
        first_source_hash = compute_source_hash(problem, config)
        first_checkpoint = create_problem_checkpoint(root, workspace, entry)
        copied = Path(first_checkpoint["problem_dir"]) / "judge-fixtures/checker/alternative.out"
        self.assertEqual(copied.read_bytes(), fixture_path.read_bytes())

        fixture_path.write_bytes(fixture_path.read_bytes().replace(b"\n", b"\r\n"))
        second_fixture_hash = inspect_judge_qa(problem, config)["fixture_hash"]
        second_source_hash = compute_source_hash(problem, config)
        second_checkpoint = create_problem_checkpoint(root, workspace, entry)

        self.assertNotEqual(first_fixture_hash, second_fixture_hash)
        self.assertNotEqual(first_source_hash, second_source_hash)
        self.assertNotEqual(first_checkpoint["revision_id"], second_checkpoint["revision_id"])
        copied = Path(second_checkpoint["problem_dir"]) / "judge-fixtures/checker/alternative.out"
        self.assertEqual(copied.read_bytes(), b"-2\r\n")

    def test_checkpoint_tracks_interactor_qa_source(self):
        _, root, workspace, entry, problem, config = self.copy_fixture("interactor-qa")
        source = problem / "code/judge-qa/normal.cpp"
        first_hash = compute_source_hash(problem, config)
        first = create_problem_checkpoint(root, workspace, entry)
        source.write_bytes(source.read_bytes() + b"\n// changed\n")
        second_hash = compute_source_hash(problem, config)
        second = create_problem_checkpoint(root, workspace, entry)
        self.assertNotEqual(first_hash, second_hash)
        self.assertNotEqual(first["revision_id"], second["revision_id"])
        copied = Path(second["problem_dir"]) / "code/judge-qa/normal.cpp"
        self.assertTrue(copied.read_bytes().endswith(b"// changed\n"))

    def test_unreferenced_fixture_is_tracked_but_not_part_of_pdf_identity(self):
        _, root, workspace, entry, problem, config = self.copy_fixture("checker-qa")
        first_source_hash = compute_source_hash(problem, config)
        first_fixture_hash = inspect_judge_qa(problem, config)["fixture_hash"]
        first_collection_hash = compute_collection_hash(root, workspace)
        extra = problem / "judge-fixtures/checker/unreferenced.bin"
        extra.write_bytes(b"unreferenced fixture\x00")
        second_source_hash = compute_source_hash(problem, config)
        second_fixture_hash = inspect_judge_qa(problem, config)["fixture_hash"]
        second_collection_hash = compute_collection_hash(root, workspace)
        checkpoint = create_problem_checkpoint(root, workspace, entry)
        copied = Path(checkpoint["problem_dir"]) / "judge-fixtures/checker/unreferenced.bin"
        self.assertNotEqual(first_source_hash, second_source_hash)
        self.assertNotEqual(first_fixture_hash, second_fixture_hash)
        self.assertEqual(first_collection_hash, second_collection_hash)
        self.assertEqual(copied.read_bytes(), b"unreferenced fixture\x00")

    def test_qa_files_do_not_enter_collection_package_or_manifest(self):
        _, root, workspace, _, problem, config = self.copy_fixture("checker-qa")
        first_meta = build_meta(problem, config)
        first_collection_hash = compute_collection_hash(root, workspace)
        first_source_hash = compute_source_hash(problem, config)
        marker = b"fixture-only-sentinel"
        fixture_path = problem / "judge-fixtures/checker/alternative.out"
        fixture_path.write_bytes(marker)
        second_meta = build_meta(problem, config)
        second_collection_hash = compute_collection_hash(root, workspace)
        second_source_hash = compute_source_hash(problem, config)
        self.assertEqual(first_meta, second_meta)
        self.assertEqual(first_collection_hash, second_collection_hash)
        self.assertNotEqual(first_source_hash, second_source_hash)

        (problem / "problem.pdf").write_bytes(b"%PDF-1.4\nfixture\n")
        generate_domjudge_config(problem, config)
        prepare_output_validator(problem, config)
        package = root / "F06.zip"
        build_package(problem, package, config=config)
        with ZipFile(package) as archive:
            names = archive.namelist()
            self.assertFalse(any(name.startswith("judge-fixtures/") for name in names), names)
            self.assertFalse(any(name.startswith("code/judge-qa/") for name in names), names)
            self.assertNotIn(marker, b"".join(archive.read(name) for name in names))

        manifest = write_manifest(
            problem,
            config,
            package,
            second_source_hash,
            compute_data_hash(problem, config),
            compute_workspace_hash(root, workspace),
            second_collection_hash,
            {"digest": "fixture-builder"},
            "fixture-batch",
            "fixture-revision",
        )
        self.assertNotIn("fixture_hash", manifest)
        self.assertNotIn("judge_qa", manifest)
        self.assertNotIn("judge-fixtures", str(manifest))
        self.assertNotIn("fixture-only-sentinel", str(manifest))

    def test_interactor_qa_sources_do_not_enter_package(self):
        _, root, _, _, problem, config = self.copy_fixture("interactor-qa")
        marker = b"judge-qa-source-only-sentinel"
        (problem / "code/judge-qa/extra.cpp").write_bytes(marker)
        (problem / "problem.pdf").write_bytes(b"%PDF-1.4\nfixture\n")
        generate_domjudge_config(problem, config)
        prepare_output_validator(problem, config)
        package = root / "F07.zip"
        build_package(problem, package, config=config)
        with ZipFile(package) as archive:
            names = archive.namelist()
            self.assertFalse(any(name.startswith("judge-fixtures/") for name in names), names)
            self.assertFalse(any(name.startswith("code/judge-qa/") for name in names), names)
            self.assertNotIn(marker, b"".join(archive.read(name) for name in names))


if __name__ == "__main__":
    unittest.main()
