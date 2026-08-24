import json
import tempfile
import unittest
from pathlib import Path

from probhub.test_shards import TestShardError, load_fast_tests, load_shards


class TestShardManifestTests(unittest.TestCase):
    def test_repository_manifests_cover_every_test_once(self):
        shards = load_shards()
        assigned = [name for names in shards.values() for name in names]
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(len(assigned), len(list(Path(__file__).parent.glob("test_*.py"))))
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


if __name__ == "__main__":
    unittest.main()
