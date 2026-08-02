import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pypdf import PdfReader, PdfWriter

from probhub.errors import ProbHubError
from probhub.pdf_processing import inspect_pdf, split_pdf
from probhub.process_control import process_alive
from probhub import pdf_worker


class PdfProcessingTests(unittest.TestCase):
    def make_pdf(self, path, pages=3):
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=200, height=200)
        with Path(path).open("wb") as stream:
            writer.write(stream)

    def test_inspect_and_split_run_in_bounded_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.pdf"
            first = root / "first.pdf"
            second = root / "second.pdf"
            self.make_pdf(source)

            inspected = inspect_pdf(source)
            self.assertEqual(inspected["pages"], 3)
            split = split_pdf(source, [
                {"path": first, "start": 0, "end": 1},
                {"path": second, "start": 1, "end": 3},
            ])

            self.assertTrue(split["ok"])
            self.assertEqual(len(PdfReader(first).pages), 1)
            self.assertEqual(len(PdfReader(second).pages), 2)

    def test_malformed_pdf_fails_with_stable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "malformed.pdf"
            path.write_bytes(b"%PDF-1.7\nmalformed\n%%EOF\n")
            with self.assertRaises(ProbHubError) as raised:
                inspect_pdf(path, scan_text=True)
            self.assertEqual(raised.exception.code, "pdf_processing_failed")

    def test_worker_timeout_terminates_the_process_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "input.pdf"
            child_pid_path = root / "child.pid"
            path.write_bytes(b"not parsed by injected worker")
            child_code = (
                "import os,pathlib,time;"
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid()),encoding='utf-8');"
                "time.sleep(30)"
            )
            parent_code = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                "time.sleep(30)"
            )
            command = [sys.executable, "-c", parent_code]
            with mock.patch(
                "probhub.pdf_processing._worker_command",
                return_value=command,
            ):
                with self.assertRaises(ProbHubError) as raised:
                    inspect_pdf(path, timeout=1.0)
            self.assertEqual(raised.exception.code, "pdf_processing_failed")
            self.assertIn("timed out", str(raised.exception))
            self.assertTrue(child_pid_path.is_file(), "worker child did not start")
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            deadline = time.time() + 5
            while process_alive(child_pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(process_alive(child_pid), f"PDF worker child {child_pid} survived")

    def test_split_rejects_invalid_ranges_without_publishing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.pdf"
            output = root / "output.pdf"
            self.make_pdf(source, pages=1)
            with self.assertRaises(ProbHubError):
                split_pdf(source, [{"path": output, "start": 0, "end": 2}])
            self.assertFalse(output.exists())

    def test_split_rejects_invalid_container_and_output_path(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            self.make_pdf(source, pages=1)
            for outputs in (None, [{"path": None, "start": 0, "end": 1}]):
                with self.subTest(outputs=outputs):
                    with self.assertRaises(ProbHubError) as raised:
                        split_pdf(source, outputs)
                    self.assertEqual(raised.exception.code, "pdf_processing_failed")

    def test_invalid_input_path_uses_stable_error(self):
        for call in (
            lambda: inspect_pdf(None),
            lambda: split_pdf(None, []),
        ):
            with self.subTest(call=call):
                with self.assertRaises(ProbHubError) as raised:
                    call()
                self.assertEqual(raised.exception.code, "pdf_processing_failed")

    def test_worker_write_failure_cleans_partial_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.pdf"
            output = root / "output.pdf"
            self.make_pdf(source, pages=1)
            payload = {
                "input": str(source),
                "outputs": [{"path": str(output), "start": 0, "end": 1}],
            }
            with mock.patch.object(pdf_worker.PdfWriter, "write", side_effect=RuntimeError("write failed")):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    pdf_worker._split(payload)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".output.pdf.probhub-*.tmp")), [])

    def test_worker_resource_failures_map_to_stable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            source.write_bytes(b"handled by mocked worker")
            for reason, text in (
                ("memory_limit", "memory limit"),
                ("output_limit", "diagnostic output limit"),
                ("process_limit", "process limit"),
            ):
                with self.subTest(reason=reason):
                    result = {"reason": reason, "returncode": None, "message": reason}
                    with mock.patch(
                        "probhub.pdf_processing.run_managed_to_files",
                        return_value=result,
                    ):
                        with self.assertRaises(ProbHubError) as raised:
                            inspect_pdf(source)
                    self.assertEqual(raised.exception.code, "pdf_processing_failed")
                    self.assertIn(text, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
