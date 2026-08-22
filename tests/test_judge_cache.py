import tempfile
import unittest
from pathlib import Path

from probhub.judge_cache import (
    CACHE_FILENAME,
    CACHE_SCHEMA_VERSION,
    SandboxCache,
    file_digest,
    hash_parts,
    testcase_cache_key,
)


class JudgeCacheCoreTests(unittest.TestCase):
    def test_hash_parts_keeps_field_boundaries(self):
        self.assertNotEqual(hash_parts("ab", "c"), hash_parts("a", "bc"))
        self.assertEqual(hash_parts("same", b"bytes"), hash_parts("same", b"bytes"))

    def test_file_digest_tracks_bytes_and_is_pathlike_friendly(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.txt"
            path.write_bytes(b"first")
            first = file_digest(path)
            self.assertEqual(first, file_digest(str(path)))
            path.write_bytes(b"second")
            self.assertNotEqual(first, file_digest(path))

    def test_cache_round_trip_and_schema_is_owned_by_core(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "problem"
            cache = SandboxCache(problem)
            cache.set("case", "one", {"status": "AC"})
            cache.save()

            self.assertEqual(cache.path, str(problem / ".probhub" / CACHE_FILENAME))
            loaded = SandboxCache(problem)
            self.assertEqual(loaded.get("case", "one"), {"status": "AC"})
            self.assertEqual(loaded.data["schema_version"], CACHE_SCHEMA_VERSION)

    def test_case_key_includes_judge_identity_and_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            input_file = Path(temp) / "case.in"
            answer_file = Path(temp) / "case.ans"
            input_file.write_bytes(b"1\n")
            answer_file.write_bytes(b"1\n")
            standard = testcase_cache_key("program", input_file, answer_file, 1, 256)
            checker = testcase_cache_key(
                "program",
                input_file,
                answer_file,
                1,
                256,
                judge_type="custom",
                judge_fingerprint="checker-v1",
            )
            self.assertNotEqual(standard, checker)


if __name__ == "__main__":
    unittest.main()
