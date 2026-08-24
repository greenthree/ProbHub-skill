import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import probhub.test_shards as test_shards
from probhub.test_shards import (
    TestShardError,
    _is_regular_test_file,
    _suite_for,
    load_fast_tests,
    load_shards,
)


class TestShardManifestTests(unittest.TestCase):
    def test_repository_manifests_cover_every_test_once(self):
        shards = load_shards()
        assigned = [name for names in shards.values() for name in names]
        self.assertEqual(len(assigned), len(set(assigned)))
        actual = [
            path
            for path in Path(__file__).parent.glob("test_*.py")
            if _is_regular_test_file(path)
        ]
        self.assertEqual(len(assigned), len(actual))
        self.assertTrue(load_fast_tests())

    def test_duplicate_assignment_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test-shards.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "shards": [
                    {"id": "a", "tests": ["test_core.py"]},
                    {"id": "b", "tests": ["test_core.py"]},
                ],
            }), encoding="utf-8")
            with self.assertRaisesRegex(TestShardError, "multiple shards"):
                load_shards(path)

    def test_nested_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test-shards.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "shards": [{"id": "a", "tests": ["nested/test_core.py"]}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(TestShardError, "invalid test filename"):
                load_shards(path)

    def test_directory_named_like_test_file_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            tests_root = Path(temp) / "tests"
            tests_root.mkdir()
            (tests_root / "test_fake.py").mkdir()
            manifest = tests_root / "test-shards.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "shards": [{"id": "fake", "tests": ["test_fake.py"]}],
            }), encoding="utf-8")
            with patch.object(test_shards, "TESTS_ROOT", tests_root):
                with self.assertRaisesRegex(TestShardError, "unknown=.*test_fake.py"):
                    load_shards(manifest)

    def test_symbolic_link_predicate_is_fail_closed(self):
        candidate = Mock()
        candidate.is_file.return_value = True
        candidate.is_symlink.return_value = True
        self.assertFalse(_is_regular_test_file(candidate))

    def test_symbolic_link_named_like_test_file_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            tests_root = Path(temp) / "tests"
            tests_root.mkdir()
            target = Path(temp) / "target.py"
            target.write_text(
                "import unittest\n"
                "class FakeTests(unittest.TestCase):\n"
                "    def test_fake(self): pass\n",
                encoding="utf-8",
            )
            link = tests_root / "test_link.py"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")
            manifest = tests_root / "test-shards.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "shards": [{"id": "link", "tests": ["test_link.py"]}],
            }), encoding="utf-8")
            with patch.object(test_shards, "TESTS_ROOT", tests_root):
                with self.assertRaisesRegex(TestShardError, "unknown=.*test_link.py"):
                    load_shards(manifest)

    def test_empty_test_file_is_rejected_before_success(self):
        with tempfile.TemporaryDirectory() as temp:
            tests_root = Path(temp) / "tests"
            tests_root.mkdir()
            (tests_root / "test_empty.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            with patch.object(test_shards, "TESTS_ROOT", tests_root):
                with self.assertRaisesRegex(TestShardError, "contains no test cases"):
                    _suite_for(["test_empty.py"])
                with patch.object(
                    test_shards,
                    "load_shards",
                    return_value={"empty": ["test_empty.py"]},
                ):
                    self.assertEqual(test_shards.main(["--shard", "empty"]), 2)


if __name__ == "__main__":
    unittest.main()
