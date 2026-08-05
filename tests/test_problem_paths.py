import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from probhub.problem_paths import ProblemPathError, resolve_problem_regular_file


class ProblemPathTests(unittest.TestCase):
    def assert_reason(self, problem_dir, value, reason):
        with self.assertRaises(ProblemPathError) as raised:
            resolve_problem_regular_file(problem_dir, value)
        self.assertEqual(raised.exception.reason, reason)

    def test_resolves_nested_file_with_either_separator(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "problem"
            source = problem / "code" / "checker.cpp"
            source.parent.mkdir(parents=True)
            source.write_text("int main() {}\n", encoding="utf-8")

            expected = source.absolute()
            self.assertEqual(
                resolve_problem_regular_file(problem, "code/checker.cpp"),
                expected,
            )
            self.assertEqual(
                resolve_problem_regular_file(problem, r"code\checker.cpp"),
                expected,
            )

    def test_preserves_access_spelling_after_canonical_containment_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            access_problem = root / "access" / "problem"
            canonical_problem = root / "canonical" / "problem"
            access_source = access_problem / "code" / "checker.cpp"
            canonical_source = canonical_problem / "code" / "checker.cpp"
            for source in (access_source, canonical_source):
                source.parent.mkdir(parents=True)
                source.write_text("int main() {}\n", encoding="utf-8")

            path_type = type(access_problem)
            real_resolve = path_type.resolve

            def canonicalized(path, strict=False):
                if path == access_problem:
                    return canonical_problem
                if path == access_source:
                    return canonical_source
                return real_resolve(path, strict=strict)

            with mock.patch.object(
                path_type,
                "resolve",
                autospec=True,
                side_effect=canonicalized,
            ):
                self.assertEqual(
                    resolve_problem_regular_file(
                        access_problem, "code/checker.cpp"
                    ),
                    access_source,
                )

    def test_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp)
            for value in (None, "", "   ", Path("code/checker.cpp")):
                with self.subTest(value=value):
                    self.assert_reason(problem, value, "invalid")

    def test_rejects_host_and_cross_platform_absolute_or_driven_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "problem"
            problem.mkdir()
            outside = str((problem.parent / "outside.cpp").resolve())
            for value in (
                outside,
                "/absolute/checker.cpp",
                r"C:\absolute\checker.cpp",
                r"\\server\share\checker.cpp",
                r"C:relative.cpp",
            ):
                with self.subTest(value=value):
                    self.assert_reason(problem, value, "outside")

    def test_rejects_parent_components_with_either_separator(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "problem"
            problem.mkdir()
            for value in ("../outside.cpp", r"..\outside.cpp", "code/../checker.cpp"):
                with self.subTest(value=value):
                    self.assert_reason(problem, value, "outside")

    def test_distinguishes_missing_and_non_regular_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "problem"
            code = problem / "code"
            code.mkdir(parents=True)

            self.assert_reason(problem, "code/missing.cpp", "missing")
            self.assert_reason(problem, "code", "non_regular")
            blocker = problem / "blocker"
            blocker.write_text("not a directory\n", encoding="utf-8")
            self.assert_reason(problem, "blocker/checker.cpp", "non_regular")

    def test_rejects_symlinks_to_inside_and_outside(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem = root / "problem"
            code = problem / "code"
            code.mkdir(parents=True)
            inside = code / "checker.cpp"
            inside.write_text("int main() {}\n", encoding="utf-8")
            outside = root / "outside.cpp"
            outside.write_text("int main() {}\n", encoding="utf-8")
            inside_link = code / "inside-link.cpp"
            outside_link = code / "outside-link.cpp"
            try:
                inside_link.symlink_to(inside)
                outside_link.symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            self.assert_reason(problem, "code/inside-link.cpp", "link")
            self.assert_reason(problem, "code/outside-link.cpp", "link")

    def test_rejects_mocked_reparse_point_component(self):
        with tempfile.TemporaryDirectory() as temp:
            problem = Path(temp) / "problem"
            code = problem / "code"
            code.mkdir(parents=True)
            (code / "checker.cpp").write_text("int main() {}\n", encoding="utf-8")
            real_lstat = os.lstat
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

            def mocked_lstat(path):
                info = real_lstat(path)
                if Path(path) == code:
                    return SimpleNamespace(
                        st_mode=info.st_mode,
                        st_file_attributes=reparse_flag,
                    )
                return info

            with mock.patch("probhub.problem_paths.os.lstat", side_effect=mocked_lstat):
                self.assert_reason(problem, "code/checker.cpp", "link")


if __name__ == "__main__":
    unittest.main()
