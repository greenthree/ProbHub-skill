import importlib.util
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("extract_new_problem", ROOT / "scripts" / "extract_new_problem.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DisplayNameTests(unittest.TestCase):
    def test_pdf_inserted_spaces_do_not_break_matching(self):
        self.assertEqual(MODULE.normalize_display_name("同步棋路 1"), MODULE.normalize_display_name("同步棋路1"))

    def test_page_count_extraction_uses_pdf_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "main.pdf"
            output = root / "problem.pdf"
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=200, height=200)
            with source.open("wb") as stream:
                writer.write(stream)

            self.assertTrue(MODULE.extract_by_page_count(source, output, 1))
            self.assertEqual(len(PdfReader(output).pages), 2)


if __name__ == "__main__":
    unittest.main()
