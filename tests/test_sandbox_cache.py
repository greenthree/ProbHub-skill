import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_judge_cache", ROOT / "scripts" / "local_judge.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SandboxCacheTests(unittest.TestCase):
    def test_cache_persists_and_tracks_hits(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            problem.mkdir()
            cache = MODULE.SandboxCache(problem)
            self.assertIsNone(cache.get("case", "missing"))
            cache.set("case", "key", {"status": "AC"})
            cache.save()

            loaded = MODULE.SandboxCache(problem)
            self.assertEqual(loaded.get("case", "key"), {"status": "AC"})
            self.assertEqual(loaded.stats["case_hits"], 1)
            self.assertTrue((problem / ".probhub" / MODULE.CACHE_FILENAME).is_file())

    def test_fingerprints_invalidate_changed_sources_and_answers(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            code = problem / "code"
            code.mkdir(parents=True)
            source = code / "std.cpp"
            source.write_text("int main(){return 0;}\n", encoding="utf-8")
            input_file = problem / "1.in"
            answer_file = problem / "1.ans"
            input_file.write_text("1\n", encoding="utf-8")
            answer_file.write_text("1\n", encoding="utf-8")

            first = MODULE.source_fingerprint(source, problem, "std")
            same = MODULE.source_fingerprint(source, problem, "std")
            self.assertEqual(first, same)
            first_case = MODULE.testcase_cache_key(first, input_file, answer_file, 1, 256)

            source.write_text("int main(){return 1;}\n", encoding="utf-8")
            changed = MODULE.source_fingerprint(source, problem, "std")
            self.assertNotEqual(first, changed)
            answer_file.write_text("2\n", encoding="utf-8")
            changed_case = MODULE.testcase_cache_key(first, input_file, answer_file, 1, 256)
            self.assertNotEqual(first_case, changed_case)

            checker_case = MODULE.testcase_cache_key(
                first,
                input_file,
                answer_file,
                1,
                256,
                judge_type="custom",
                judge_fingerprint="checker-v1",
            )
            changed_checker_case = MODULE.testcase_cache_key(
                first,
                input_file,
                answer_file,
                1,
                256,
                judge_type="custom",
                judge_fingerprint="checker-v2",
            )
            self.assertNotEqual(checker_case, changed_checker_case)
            self.assertNotEqual(first_case, checker_case)

            interactive_short_idle = MODULE.testcase_cache_key(
                first,
                input_file,
                answer_file,
                1,
                256,
                judge_type="interactive",
                judge_fingerprint="interactor-v1",
                judge_options_fingerprint="idle=0.2;transcript=4096",
            )
            interactive_long_idle = MODULE.testcase_cache_key(
                first,
                input_file,
                answer_file,
                1,
                256,
                judge_type="interactive",
                judge_fingerprint="interactor-v1",
                judge_options_fingerprint="idle=1.0;transcript=4096",
            )
            self.assertNotEqual(interactive_short_idle, interactive_long_idle)

    def test_refresh_mode_bypasses_reads_and_replaces_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            problem.mkdir()
            initial = MODULE.SandboxCache(problem)
            initial.set("case", "old", {"status": "WA"})
            initial.save()

            refresh = MODULE.SandboxCache(problem, enabled=True, read_enabled=False)
            self.assertIsNone(refresh.get("case", "old"))
            self.assertEqual(refresh.stats["mode"], "refresh")
            refresh.set("case", "new", {"status": "AC"})
            refresh.save()

            loaded = MODULE.SandboxCache(problem)
            self.assertIsNone(loaded.get("case", "old"))
            self.assertEqual(loaded.get("case", "new"), {"status": "AC"})

    def test_disabled_cache_does_not_write(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            problem.mkdir()
            cache = MODULE.SandboxCache(problem, enabled=False)
            cache.set("case", "key", {"status": "AC"})
            cache.save()
            self.assertFalse((problem / ".probhub" / MODULE.CACHE_FILENAME).exists())

    def test_previous_cache_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "A"
            cache_path = problem / ".probhub" / MODULE.CACHE_FILENAME
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps({"schema_version": 2, "case": {"old": {"status": "OLE"}}}),
                encoding="utf-8",
            )
            loaded = MODULE.SandboxCache(problem)
            self.assertIsNone(loaded.get("case", "old"))


if __name__ == "__main__":
    unittest.main()
