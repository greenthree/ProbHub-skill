import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from probhub.build_lock import workspace_file_lock
from probhub.errors import ProbHubError
from probhub.install_skill import (
    CLEANUP_PREFIX,
    TARGETS,
    TRANSACTION_PREFIX,
    SkillInstallError,
    _skill_lock_location,
    install_skill,
    recover_skill_install,
)


class SkillInstallTests(unittest.TestCase):
    def create_package(self, root, *, label="new"):
        package = root / f"package-{label}"
        for directory in ("references", "scripts", "probhub"):
            (package / directory).mkdir(parents=True)
            (package / directory / f"{directory}.txt").write_text(
                label + "\n", encoding="utf-8"
            )
        (package / "SKILL.md").write_text(f"# {label}\n", encoding="utf-8")
        (package / "package.json").write_text(
            json.dumps({"version": f"1.0.{len(label)}"}), encoding="utf-8"
        )
        return package

    def targets(self, base):
        base = Path(base).resolve()
        return tuple(base.joinpath(*target.split("/")) for target in TARGETS)

    def seed_old_installs(self, base):
        targets = self.targets(base)
        for index, target in enumerate(targets):
            target.mkdir(parents=True)
            (target / "old.txt").write_text(f"old-{index}\n", encoding="utf-8")
        return targets

    def assert_no_transactions(self, base):
        self.assertEqual(list(base.glob(f"{TRANSACTION_PREFIX}*")), [])
        self.assertEqual(list(base.glob(f"{CLEANUP_PREFIX}*")), [])

    def test_success_replaces_both_targets_and_writes_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            targets = self.seed_old_installs(base)

            installed = install_skill(base, source_root=package, version="9.8.7")

            self.assertEqual(installed, targets)
            for target in targets:
                self.assertFalse((target / "old.txt").exists())
                self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "# new\n")
                marker = json.loads(
                    (target / ".probhub-version.json").read_text(encoding="utf-8")
                )
                self.assertEqual(marker["version"], "9.8.7")
                self.assertTrue((target / "probhub/probhub.txt").is_file())
            self.assert_no_transactions(base)

    def test_second_target_publish_failure_rolls_back_both_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            targets = self.seed_old_installs(base)
            real_replace = os.replace

            def fail_second_payload(source, destination):
                source = Path(source)
                destination = Path(destination)
                if source.name == "payload-1" and destination == targets[1]:
                    raise OSError("injected second target failure")
                return real_replace(source, destination)

            with patch("probhub.install_skill.os.replace", side_effect=fail_second_payload):
                with self.assertRaises(SkillInstallError):
                    install_skill(base, source_root=package)

            for index, target in enumerate(targets):
                self.assertEqual(
                    (target / "old.txt").read_text(encoding="utf-8"),
                    f"old-{index}\n",
                )
                self.assertFalse((target / "SKILL.md").exists())
            self.assert_no_transactions(base)

    def test_rollback_failure_is_recovered_on_next_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            targets = self.seed_old_installs(base)
            real_replace = os.replace

            def interrupt_publish_and_rollback(source, destination):
                source = Path(source)
                destination = Path(destination)
                if source.name == "payload-1" and destination == targets[1]:
                    raise OSError("injected publish failure")
                if source.name == "backup-0" and destination == targets[0]:
                    raise OSError("injected rollback failure")
                return real_replace(source, destination)

            with patch(
                "probhub.install_skill.os.replace",
                side_effect=interrupt_publish_and_rollback,
            ):
                with self.assertRaisesRegex(SkillInstallError, "rollback failed"):
                    install_skill(base, source_root=package)

            transactions = list(base.glob(f"{TRANSACTION_PREFIX}*"))
            self.assertEqual(len(transactions), 1)
            self.assertTrue((transactions[0] / "journal.json").is_file())
            self.assertFalse(targets[0].exists())

            recover_skill_install(base)

            for index, target in enumerate(targets):
                self.assertEqual(
                    (target / "old.txt").read_text(encoding="utf-8"),
                    f"old-{index}\n",
                )
            self.assert_no_transactions(base)

    def test_concurrent_installer_is_rejected_by_os_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "home"
            base.mkdir()
            package = self.create_package(root)

            lock_root, lock_file = _skill_lock_location(base)
            with workspace_file_lock(lock_root, lock_file, no_follow=True):
                with self.assertRaises(ProbHubError) as raised:
                    install_skill(base, source_root=package)

            self.assertEqual(raised.exception.code, "skill_install_busy")
            self.assertEqual(self.targets(base)[0].exists(), False)
            self.assertEqual(self.targets(base)[1].exists(), False)

    def test_corrupt_journal_fails_without_touching_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            targets = self.seed_old_installs(base)
            transaction = base / f"{TRANSACTION_PREFIX}{'a' * 32}"
            transaction.mkdir()
            (transaction / "journal.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "phase": "prepared",
                    "entries": [
                        {
                            "target": "../outside",
                            "payload": "payload-0",
                            "backup": None,
                            "existed": False,
                        },
                        {
                            "target": TARGETS[1],
                            "payload": "payload-1",
                            "backup": "backup-1",
                            "existed": True,
                        },
                    ],
                }),
                encoding="utf-8",
            )

            with self.assertRaises(SkillInstallError):
                install_skill(base, source_root=package)

            for index, target in enumerate(targets):
                self.assertEqual(
                    (target / "old.txt").read_text(encoding="utf-8"),
                    f"old-{index}\n",
                )
            self.assertTrue(transaction.is_dir())

    def test_committed_cleanup_failure_is_retried_without_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            targets = self.seed_old_installs(base)
            real_rmtree = shutil.rmtree

            def fail_cleanup(path, *args, **kwargs):
                if Path(path).name.startswith(CLEANUP_PREFIX):
                    raise OSError("injected cleanup failure")
                return real_rmtree(path, *args, **kwargs)

            with patch("probhub.install_skill.shutil.rmtree", side_effect=fail_cleanup):
                install_skill(base, source_root=package)

            cleanup = list(base.glob(f"{CLEANUP_PREFIX}*"))
            self.assertEqual(len(cleanup), 1)
            for target in targets:
                self.assertTrue((target / "SKILL.md").is_file())
                self.assertFalse((target / "old.txt").exists())

            recover_skill_install(base)

            self.assert_no_transactions(base)
            for target in targets:
                self.assertTrue((target / "SKILL.md").is_file())
                self.assertFalse((target / "old.txt").exists())

    def test_symlinked_target_is_rejected_without_following_it(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            outside = Path(external)
            outside_marker = outside / "marker.txt"
            outside_marker.write_text("outside\n", encoding="utf-8")
            target = self.targets(base)[0]
            target.parent.mkdir(parents=True)
            try:
                os.symlink(outside, target, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            with self.assertRaises(SkillInstallError):
                install_skill(base, source_root=package)

            self.assertEqual(outside_marker.read_text(encoding="utf-8"), "outside\n")
            self.assertTrue(target.is_symlink())
            self.assertFalse(self.targets(base)[1].exists())

    def test_install_base_inside_packaged_source_is_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = self.create_package(root)
            base = package / "probhub/local-project"

            with self.assertRaisesRegex(SkillInstallError, "descendants"):
                install_skill(base, source_root=package)

            self.assertFalse(base.exists())
            self.assertFalse((base / ".agents").exists())
            self.assertFalse((base / ".claude").exists())
            self.assert_no_transactions(base)

    def test_internal_package_link_is_rejected_without_changing_old_installs(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            targets = self.seed_old_installs(base)
            outside = Path(external) / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            link = package / "probhub/linked.txt"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

            with self.assertRaises(SkillInstallError):
                install_skill(base, source_root=package)

            for index, target in enumerate(targets):
                self.assertEqual(
                    (target / "old.txt").read_text(encoding="utf-8"),
                    f"old-{index}\n",
                )
            self.assert_no_transactions(base)

    def test_missing_backup_cannot_be_mistaken_for_a_successful_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            targets = self.seed_old_installs(base)
            real_replace = os.replace

            def lose_backup_then_fail(source, destination):
                source = Path(source)
                destination = Path(destination)
                if source.name == "payload-0" and destination == targets[0]:
                    result = real_replace(source, destination)
                    (source.parent / "backup-0").rename(source.parent / "lost-backup-0")
                    return result
                if source.name == "payload-1" and destination == targets[1]:
                    raise OSError("injected second target failure")
                return real_replace(source, destination)

            with patch("probhub.install_skill.os.replace", side_effect=lose_backup_then_fail):
                with self.assertRaisesRegex(SkillInstallError, "rollback failed"):
                    install_skill(base, source_root=package)

            self.assertTrue((targets[0] / "SKILL.md").is_file())
            transactions = list(base.glob(f"{TRANSACTION_PREFIX}*"))
            self.assertEqual(len(transactions), 1)
            with self.assertRaises(SkillInstallError):
                recover_skill_install(base)
            self.assertTrue(transactions[0].is_dir())

    def test_rollback_does_not_delete_an_external_target_created_mid_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            targets = self.targets(base)
            real_replace = os.replace

            def create_external_target(source, destination):
                source = Path(source)
                destination = Path(destination)
                if source.name == "payload-1" and destination == targets[1]:
                    destination.mkdir(parents=True)
                    (destination / "external.txt").write_text("external\n", encoding="utf-8")
                    raise OSError("injected target race")
                return real_replace(source, destination)

            with patch("probhub.install_skill.os.replace", side_effect=create_external_target):
                with self.assertRaisesRegex(SkillInstallError, "rollback failed"):
                    install_skill(base, source_root=package)

            self.assertEqual(
                (targets[1] / "external.txt").read_text(encoding="utf-8"),
                "external\n",
            )
            self.assertFalse(targets[0].exists())
            self.assertEqual(len(list(base.glob(f"{TRANSACTION_PREFIX}*"))), 1)

    def test_symlinked_lock_is_rejected_without_touching_external_file(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as external:
            root = Path(temp)
            base = root / "home"
            package = self.create_package(root)
            outside = Path(external) / "lock-target"
            outside.write_bytes(b"")
            lock_root, lock_file = _skill_lock_location(base)
            lock_path = lock_root / lock_file
            lock_path.unlink(missing_ok=True)
            try:
                os.symlink(outside, lock_path)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

            with self.assertRaises(ProbHubError):
                install_skill(base, source_root=package)

            self.assertEqual(outside.read_bytes(), b"")
            self.assertFalse(self.targets(base)[0].exists())
            self.assertFalse(self.targets(base)[1].exists())


if __name__ == "__main__":
    unittest.main()
