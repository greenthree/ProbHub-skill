import tempfile
import unittest
from pathlib import Path

from probhub.source_diagnostics import diagnose_source, format_diagnostic


class SourceDiagnosticTests(unittest.TestCase):
    def write(self, directory, name, source):
        path = Path(directory) / name
        path.write_text(source, encoding="utf-8")
        return path

    def test_testlib_generator_requires_register_gen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write(temp, "inmaker.cpp", '#include "testlib.h"\nint main() { return 0; }\n')
            diagnostics = diagnose_source(path, "generator")
            self.assertEqual([item["code"] for item in diagnostics], ["generator_missing_registerGen"])
            self.assertIn("registerGen(argc, argv, 1)", format_diagnostic(diagnostics[0]))

    def test_registered_or_non_testlib_generator_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            registered = self.write(
                temp,
                "registered.cpp",
                '#include "testlib.h"\nint main(int argc, char** argv) { registerGen(argc, argv, 1); }\n',
            )
            plain = self.write(temp, "plain.cpp", "#include <iostream>\nint main() { std::cout << 1; }\n")
            self.assertEqual(diagnose_source(registered, "generator"), [])
            self.assertEqual(diagnose_source(plain, "generator"), [])

    def test_read_token_identifier_is_reported_but_real_pattern_is_not(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = self.write(
                temp,
                "validator.cpp",
                'int main() { std::string s = inf.readToken("s"); }\n',
            )
            good = self.write(
                temp,
                "validator-good.cpp",
                'int main() { std::string s = inf.readToken("[a-z]+", "s"); }\n',
            )
            diagnostics = diagnose_source(bad, "validator")
            self.assertEqual([item["code"] for item in diagnostics], ["validator_readToken_label_as_pattern"])
            self.assertEqual(diagnose_source(good, "validator"), [])

    def test_read_word_uses_the_actual_method_in_its_hint(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write(
                temp,
                "validator.cpp",
                'int main() { std::string word = inf.readWord("word"); }\n',
            )
            diagnostics = diagnose_source(path, "validator")
            self.assertEqual(len(diagnostics), 1)
            self.assertIn('readWord("[a-z]+", "word")', diagnostics[0]["hint"])

    def test_commented_examples_do_not_satisfy_or_trigger_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            generator = self.write(
                temp,
                "generator.cpp",
                '#include "testlib.h"\n// registerGen(argc, argv, 1);\nint main() {}\n',
            )
            validator = self.write(
                temp,
                "validator.cpp",
                '// std::string s = inf.readToken("s");\nint main() {}\n',
            )
            self.assertEqual(
                [item["code"] for item in diagnose_source(generator, "generator")],
                ["generator_missing_registerGen"],
            )
            self.assertEqual(diagnose_source(validator, "validator"), [])
