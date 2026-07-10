import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("extract_new_problem", ROOT / "scripts" / "extract_new_problem.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DisplayNameTests(unittest.TestCase):
    def test_pdf_inserted_spaces_do_not_break_matching(self):
        self.assertEqual(MODULE.normalize_display_name("同步棋路 1"), MODULE.normalize_display_name("同步棋路1"))


if __name__ == "__main__":
    unittest.main()
