import tempfile
import unittest
from pathlib import Path

from probhub.judge_runtime import (
    BinaryChangedError,
    binary_identity,
    publish_compiled_binary,
    verify_binary_identities,
)


class JudgeRuntimeTests(unittest.TestCase):
    def test_binary_identity_and_fence_are_core_owned(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "program.exe"
            binary.write_bytes(b"expected")
            expected = binary_identity(binary)
            self.assertIsNone(verify_binary_identities([{"role": "std", "identity": expected}]))
            binary.write_bytes(b"changed")
            failure = verify_binary_identities([{"role": "std", "identity": expected}])
            self.assertEqual(failure["code"], "binary_changed")
            self.assertEqual(failure["failure_kind"], "infrastructure")

    def test_publish_identity_failure_restores_previous_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staged = root / "staged.exe"
            output = root / "program.exe"
            staged.write_bytes(b"new")
            output.write_bytes(b"old")
            staged_identity = binary_identity(staged)

            def tampered_identity(path):
                actual = binary_identity(path)
                actual["digest"] = "0" * 64
                return actual

            with self.assertRaises(BinaryChangedError):
                publish_compiled_binary(
                    str(staged),
                    str(output),
                    staged_identity,
                    identity_fn=tampered_identity,
                )
            self.assertEqual(output.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
