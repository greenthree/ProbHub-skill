import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NpmPackageMetadataTests(unittest.TestCase):
    def load_json(self, relative_path: str) -> dict:
        return json.loads((ROOT / relative_path).read_text(encoding="utf-8-sig"))

    def test_main_and_compatibility_packages_share_exact_version(self):
        main = self.load_json("package.json")
        compat = self.load_json("compat/probhub-skill/package.json")
        python_version = {}
        exec((ROOT / "probhub/__init__.py").read_text(encoding="utf-8"), python_version)

        self.assertEqual("probhub", main["name"])
        self.assertEqual("probhub-skill", compat["name"])
        self.assertEqual(main["version"], python_version["__version__"])
        self.assertEqual(main["version"], compat["version"])
        self.assertEqual({"probhub": main["version"]}, compat["dependencies"])

    def test_both_packages_expose_cli_and_skill_installer(self):
        expected_bins = {
            "probhub-skill": "bin/init.js",
            "probhub": "bin/probhub.js",
        }
        main = self.load_json("package.json")
        compat = self.load_json("compat/probhub-skill/package.json")

        self.assertEqual(expected_bins, main["bin"])
        self.assertEqual(expected_bins, compat["bin"])
        self.assertIn("require('probhub/bin/init.js')", (ROOT / "compat/probhub-skill/bin/init.js").read_text(encoding="utf-8"))
        self.assertIn("require('probhub/bin/probhub.js')", (ROOT / "compat/probhub-skill/bin/probhub.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
