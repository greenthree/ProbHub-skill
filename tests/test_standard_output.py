import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "standard_output_local_judge", ROOT / "scripts" / "local_judge.py"
)
JUDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JUDGE)


class StandardOutputComparisonTests(unittest.TestCase):
    def assertAccepted(self, answer, actual):
        matched, message = JUDGE.compare_standard_output(answer, actual)
        self.assertTrue(matched, message)
        self.assertEqual(message, "")

    def assertRejected(self, answer, actual, message_fragment=None):
        matched, message = JUDGE.compare_standard_output(answer, actual)
        self.assertFalse(matched)
        if message_fragment:
            self.assertIn(message_fragment, message)

    def test_accepts_trailing_spaces_and_tabs_on_every_line(self):
        self.assertAccepted("1 2\nhello\n", "1 2   \nhello\t \n")

    def test_accepts_crlf_and_outer_blank_lines(self):
        self.assertAccepted("1\r\n2\r\n", "\n1\n2\n\n")

    def test_keeps_internal_spacing_significant(self):
        self.assertRejected("1 2\n", "1  2\n", "line 1")

    def test_keeps_internal_line_breaks_significant(self):
        self.assertRejected("1 2\n", "1\n2\n", "line 1")

    def test_keeps_leading_whitespace_on_nonfirst_lines_significant(self):
        self.assertRejected("a\n b\n", "a\nb\n", "line 2")

    def test_empty_and_whitespace_only_outputs_match(self):
        self.assertAccepted("", " \t\r\n")


if __name__ == "__main__":
    unittest.main()
