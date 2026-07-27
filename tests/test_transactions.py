import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from probhub.building import (
    _recover_build_publish_transactions,
    assert_collection_seals_unchanged,
    create_build_plan,
    require_collection_sealed,
)
from probhub.cli import main as cli_main
from probhub.datagen import _publish_generated_files, _recover_generated_transactions
from probhub.errors import ProbHubError
from probhub.generations import create_problem_checkpoint, latest_checkpoint
from probhub.io import read_yaml, write_json, write_yaml
from probhub.linting import compute_data_hash, compute_source_hash
from probhub.stressing import _recover_fixate_transactions
from probhub.transactions import recover_workspace_transactions
from probhub.workspace import load_problem, load_workspace, problem_entries, resolve_problem_dir


def run_cli(arguments):
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli_main(arguments)
    text = output.getvalue()
    return code, json.loads(text) if text.strip() else None


class TransactionSafetyTests(unittest.TestCase):
    def create_workspace(self, root):
        (root / ".probhub").mkdir(parents=True)
        write_yaml(root / ".probhub/workspace.yaml", {
            "schema_version": 1,
            "problems": [{"id": "A", "directory": "A"}],
        })
        problem = root / "A"
        (problem / "code").mkdir(parents=True)
        (problem / "data/sample").mkdir(parents=True)
        (problem / "data/secret").mkdir(parents=True)
        (problem / "problem.md").write_text(
            "# Alpha\n\n"
            "## 题目描述\n\nDescription.\n\n"
            "## 输入格式\n\nInput.\n\n"
            "## 输出格式\n\nOutput.\n",
            encoding="utf-8",
        )
        (problem / "code/validator.cpp").write_text("int main(){}\n", encoding="utf-8")
        (problem / "code/std.cpp").write_text("int main(){}\n", encoding="utf-8")
        (problem / "data/sample/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/sample/1.ans").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.in").write_text("1\n", encoding="utf-8")
        (problem / "data/secret/1.ans").write_text("1\n", encoding="utf-8")
        write_yaml(problem / "probhub.yaml", {
            "schema_version": 1,
            "id": "A",
            "name": "Alpha",
            "display_name": "Alpha",
            "limits": {"time": 1, "memory": 256, "output": 64},
            "statement": {"source": "problem.md"},
            "judge": {"type": "standard", "validator": "code/validator.cpp"},
            "solutions": {"accepted": ["code/std.cpp"], "brute": [], "wrong": []},
            "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        })
        return problem

    def create_build_transaction(self, root, journal):
        transaction = root / ".probhub/build-publish-fixture"
        transaction.mkdir(parents=True)
        write_json(transaction / "journal.json", journal)
        return transaction

    def assert_recovery_failure_preserves(self, root, journal):
        target = root / "artifact.txt"
        target.write_bytes(b"published")
        transaction = self.create_build_transaction(root, journal)
        with self.assertRaises(ProbHubError) as raised:
            _recover_build_publish_transactions(root)
        self.assertEqual(raised.exception.code, "publish_recovery_failed")
        self.assertEqual(target.read_bytes(), b"published")
        self.assertTrue(transaction.is_dir())
        self.assertTrue((transaction / "journal.json").is_file())
        return transaction

    def test_committed_journals_only_cleanup_without_rollback(self):
        recovery_cases = (
            ("build", ".probhub/build-publish-fixture", "target", _recover_build_publish_transactions),
            ("generated", "A/.probhub-gen-publish-fixture", "final", _recover_generated_transactions),
            ("fixate", "A/.probhub-fixate-fixture", "target", _recover_fixate_transactions),
        )
        for label, transaction_name, target_key, recover in recovery_cases:
            with self.subTest(kind=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                problem = root / "A"
                problem.mkdir()
                recovery_root = root if label == "build" else problem
                target = recovery_root / "artifact.txt"
                target.write_bytes(b"committed")
                transaction = root / transaction_name
                transaction.mkdir(parents=True)
                (transaction / "backup-0").write_bytes(b"old")
                write_json(transaction / "journal.json", {
                    "schema_version": 1,
                    "phase": "committed",
                    "entries": [{
                        target_key: "artifact.txt",
                        "backup": "backup-0",
                        "existed": True,
                    }],
                })

                recover(recovery_root)

                self.assertEqual(target.read_bytes(), b"committed")
                self.assertFalse(transaction.exists())

    def test_committed_cleanup_failure_is_recoverable_without_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            problem.mkdir()
            target = problem / "artifact.txt"
            target.write_bytes(b"old")
            real_rmtree = shutil.rmtree
            calls = 0

            def fail_first_cleanup(path, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("injected cleanup failure")
                return real_rmtree(path, *args, **kwargs)

            with patch("probhub.datagen.shutil.rmtree", side_effect=fail_first_cleanup):
                with self.assertRaises(ProbHubError) as raised:
                    _publish_generated_files(problem, [(target, b"new")])

            self.assertEqual(raised.exception.code, "gen_cleanup_failed")
            self.assertEqual(target.read_bytes(), b"new")
            transactions = list(problem.glob(".probhub-gen-publish-*"))
            self.assertEqual(len(transactions), 1)
            journal = json.loads(
                (transactions[0] / "journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(journal["phase"], "committed")

            _recover_generated_transactions(problem)

            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(transactions[0].exists())

    def test_second_interrupt_during_rollback_keeps_recovery_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            problem.mkdir()
            target = problem / "artifact.txt"
            target.write_bytes(b"old")
            real_replace = os.replace

            def interrupted_replace(source, destination):
                source = Path(source)
                destination = Path(destination)
                if source.name.startswith("payload-") and destination == target:
                    raise OSError("injected publish failure")
                if source.name.startswith("backup-") and destination == target:
                    raise KeyboardInterrupt()
                return real_replace(source, destination)

            with patch("probhub.datagen.os.replace", side_effect=interrupted_replace):
                with self.assertRaises(ProbHubError) as raised:
                    _publish_generated_files(problem, [(target, b"new")])

            self.assertEqual(raised.exception.code, "gen_rollback_failed")
            self.assertFalse(target.exists())
            transactions = list(problem.glob(".probhub-gen-publish-*"))
            self.assertEqual(len(transactions), 1)

            _recover_generated_transactions(problem)

            self.assertEqual(target.read_bytes(), b"old")
            self.assertFalse(transactions[0].exists())

    def test_invalid_duplicate_and_wrong_existed_journals_are_preserved(self):
        invalid_journals = (
            {
                "schema_version": 999,
                "phase": "prepared",
                "entries": [],
            },
            {
                "schema_version": 1,
                "phase": "prepared",
                "entries": [
                    {"target": "artifact.txt", "backup": None, "existed": False},
                    {"target": "ARTIFACT.TXT", "backup": None, "existed": False},
                ],
            },
            {
                "schema_version": 1,
                "phase": "prepared",
                "entries": [{
                    "target": "artifact.txt",
                    "backup": "backup-0",
                    "existed": "yes",
                }],
            },
            {
                "schema_version": 1,
                "phase": "prepared",
                "entries": [{
                    "target": "artifact.txt",
                    "backup": None,
                    "existed": True,
                }],
            },
        )
        for journal in invalid_journals:
            with self.subTest(journal=journal), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / ".probhub").mkdir()
                self.assert_recovery_failure_preserves(root, journal)

    def test_journal_target_and_backup_path_traversal_are_rejected(self):
        journals = (
            {
                "schema_version": 1,
                "phase": "prepared",
                "entries": [{
                    "target": "../outside.txt",
                    "backup": "backup-0",
                    "existed": True,
                }],
            },
            {
                "schema_version": 1,
                "phase": "prepared",
                "entries": [{
                    "target": "artifact.txt",
                    "backup": "../outside-backup",
                    "existed": True,
                }],
            },
        )
        for journal in journals:
            with self.subTest(journal=journal), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / ".probhub").mkdir()
                outside = root.parent / f"{root.name}-outside.txt"
                outside.write_bytes(b"outside")
                try:
                    self.assert_recovery_failure_preserves(root, journal)
                    self.assertEqual(outside.read_bytes(), b"outside")
                finally:
                    outside.unlink(missing_ok=True)

    def test_journal_target_through_external_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external:
            root = Path(temp)
            outside = Path(external)
            (root / ".probhub").mkdir()
            link = root / "linked"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            transaction = self.assert_recovery_failure_preserves(root, {
                "schema_version": 1,
                "phase": "prepared",
                "entries": [{
                    "target": "linked/artifact.txt",
                    "backup": None,
                    "existed": False,
                }],
            })
            self.assertFalse((outside / "artifact.txt").exists())
            self.assertTrue(transaction.is_dir())

    def test_journal_backup_external_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external:
            root = Path(temp)
            outside = Path(external) / "backup.txt"
            outside.write_bytes(b"outside")
            (root / ".probhub").mkdir()
            target = root / "artifact.txt"
            target.write_bytes(b"published")
            transaction = self.create_build_transaction(root, {
                "schema_version": 1,
                "phase": "prepared",
                "entries": [{
                    "target": "artifact.txt",
                    "backup": "backup-0",
                    "existed": True,
                }],
            })
            try:
                os.symlink(outside, transaction / "backup-0")
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

            with self.assertRaises(ProbHubError) as raised:
                _recover_build_publish_transactions(root)

            self.assertEqual(raised.exception.code, "publish_recovery_failed")
            self.assertEqual(target.read_bytes(), b"published")
            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertTrue(transaction.is_dir())

    def test_pending_transactions_block_lint_status_and_judge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            (root / ".probhub/build-publish-interrupted").mkdir()

            for command in ("lint", "status", "judge"):
                with self.subTest(command=command):
                    code, result = run_cli([
                        "--workspace", str(root), "--json", command, "A",
                    ])
                    self.assertEqual(code, 1)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["code"], "recovery_required")

    def test_writer_recovery_dispatches_every_transaction_type(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            _, workspace = load_workspace(root)
            with (
                patch("probhub.building._recover_build_publish_transactions") as build,
                patch("probhub.datagen._recover_generated_transactions") as generated,
                patch("probhub.stressing._recover_fixate_transactions") as fixate,
            ):
                recover_workspace_transactions(root, workspace)

            build.assert_called_once_with(root.resolve())
            generated.assert_called_once_with(root.resolve() / "A")
            fixate.assert_called_once_with(root.resolve() / "A")

    def test_workspace_problem_directories_must_stay_inside_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root.parent / f"{root.name}-outside"
            for directory in ("../outside", str(outside.resolve()), "."):
                with self.subTest(directory=directory), self.assertRaises(ProbHubError):
                    resolve_problem_dir(root, {"id": "A", "directory": directory})

    def test_checkpoint_rejects_display_name_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.create_workspace(root)
            _, workspace = load_workspace(root)
            entry = problem_entries(workspace)[0]
            checkpoint = create_problem_checkpoint(root, workspace, entry)
            copied_config = Path(checkpoint["problem_dir"]) / "probhub.yaml"
            config = read_yaml(copied_config)
            config["display_name"] = "Forged Display Name"
            config["name"] = "Forged Display Name"
            write_yaml(copied_config, config)

            with self.assertRaises(ProbHubError) as raised:
                latest_checkpoint(root, "A")

            self.assertEqual(raised.exception.code, "checkpoint_invalid")

    def test_build_rejects_latest_seal_pointer_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            _, workspace = load_workspace(root)
            entry = problem_entries(workspace)[0]
            _, config = load_problem(root, entry)
            create_problem_checkpoint(
                root,
                workspace,
                entry,
                state="sealed",
                evidence={"run": 1},
                expected_source_hash=compute_source_hash(problem, config),
                expected_data_hash=compute_data_hash(problem, config),
            )
            plan = create_build_plan(root, workspace, [entry])
            bound = require_collection_sealed(plan)

            create_problem_checkpoint(
                root,
                workspace,
                entry,
                state="sealed",
                evidence={"run": 2},
                expected_source_hash=compute_source_hash(problem, config),
                expected_data_hash=compute_data_hash(problem, config),
            )

            with self.assertRaises(ProbHubError) as raised:
                assert_collection_seals_unchanged(plan, bound)

            self.assertEqual(raised.exception.code, "sealed_revision_changed")

    def test_source_hash_tracks_python_and_inc_helpers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = self.create_workspace(root)
            _, workspace = load_workspace(root)
            _, config = load_problem(root, problem_entries(workspace)[0])
            initial = compute_source_hash(problem, config)

            helper_py = problem / "code/helper.py"
            helper_py.write_text("VALUE = 1\n", encoding="utf-8")
            with_python = compute_source_hash(problem, config)
            self.assertNotEqual(initial, with_python)

            helper_inc = problem / "code/helper.inc"
            helper_inc.write_text("#define VALUE 1\n", encoding="utf-8")
            with_include = compute_source_hash(problem, config)
            self.assertNotEqual(with_python, with_include)

            helper_py.write_text("VALUE = 2\n", encoding="utf-8")
            self.assertNotEqual(with_include, compute_source_hash(problem, config))


if __name__ == "__main__":
    unittest.main()
