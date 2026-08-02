import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from probhub.builder_fingerprint import (
    BUILD_MANIFEST_SCHEMA_VERSION,
    FIXED_FONT_SHA256,
    GENERATION_SCHEMA_VERSION,
    builder_fingerprint_stale_fields,
    compute_builder_fingerprint,
    compute_typst_template_hash,
    fixed_font_identity,
    fixed_font_path,
)
from probhub.errors import ProbHubError
from probhub.io import write_yaml
from probhub.linting import compute_workspace_hash


class BuilderFingerprintTests(unittest.TestCase):
    def create_workspace(self, root):
        typst_root = root / "typst"
        typst_dir = typst_root / "contest"
        typst_dir.mkdir(parents=True)
        (typst_dir / "main.typ").write_bytes(b"#include \"../lib.typ\"\n")
        (typst_root / "lib.typ").write_bytes(b"#let value = 1\n")
        (typst_root / "shape.svg").write_bytes(b"<svg>\n</svg>\n")
        (typst_root / "data.json").write_bytes(b'{"value": 1}\n')
        (typst_root / "logo.png").write_bytes(b"png-one")
        workspace = {"typst": {"directory": "typst/contest"}}
        (root / ".probhub").mkdir()
        write_yaml(root / ".probhub/workspace.yaml", workspace)
        return workspace

    @staticmethod
    def toolchain(*, typst="0.14.2", pypdf="6.14.2"):
        return {
            "typst": {
                "ok": typst is not None,
                "path": "/fixture/typst" if typst is not None else None,
                "version": typst,
                "diagnostic": None if typst is not None else "typst was not found",
            },
            "font": {
                "ok": True,
                "family": "Noto Sans CJK SC",
                "policy": "bundled-only-v1",
                "sha256": FIXED_FONT_SHA256,
                "diagnostic": None,
            },
            "pypdf_version": pypdf,
        }

    def test_template_text_hash_is_lf_normalized_but_images_are_byte_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            first = compute_typst_template_hash(root, workspace)
            first_workspace = compute_workspace_hash(root, workspace)

            paths = [root / ".probhub/workspace.yaml", *(root / "typst").rglob("*")]
            for path in paths:
                if path.suffix in {".yaml", ".typ", ".svg"}:
                    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
            second = compute_typst_template_hash(root, workspace)
            self.assertEqual(first, second)
            self.assertEqual(first_workspace, compute_workspace_hash(root, workspace))

            (root / "typst/contest/main.pdf").write_bytes(b"generated-pdf")
            (root / "typst/contest/problems.json").write_bytes(b"generated-json")
            (root / "typst/.preview").mkdir()
            (root / "typst/.preview/cache.bin").write_bytes(b"preview")
            self.assertEqual(second, compute_typst_template_hash(root, workspace))

            (root / "typst/custom-input.bin").write_bytes(b"custom-one")
            custom = compute_typst_template_hash(root, workspace)
            self.assertNotEqual(second, custom)
            (root / "typst/custom-input.bin").write_bytes(b"custom-two")
            self.assertNotEqual(custom, compute_typst_template_hash(root, workspace))

            (root / "typst/logo.png").write_bytes(b"png-two")
            self.assertNotEqual(second, compute_typst_template_hash(root, workspace))

    def test_workspace_hash_normalizes_equivalent_root_spellings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            aliased_root = root / ".." / root.name

            self.assertEqual(
                compute_workspace_hash(root, workspace),
                compute_workspace_hash(aliased_root, workspace),
            )

    def test_template_links_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            target = root / "outside.json"
            target.write_text("{}\n", encoding="utf-8")
            link = root / "typst/linked.json"
            try:
                os.symlink(target, link)
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            with self.assertRaises(ProbHubError) as raised:
                compute_typst_template_hash(root, workspace)
            self.assertEqual(raised.exception.code, "builder_fingerprint_failed")
            self.assertIn("links or reparse points", str(raised.exception))

    def test_fingerprint_contains_only_artifact_builder_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            with (
                patch(
                    "probhub.builder_fingerprint.builder_toolchain_status",
                    return_value=self.toolchain(),
                ),
                patch("probhub.builder_fingerprint.__version__", "9.8.7"),
            ):
                fingerprint = compute_builder_fingerprint(root, workspace)

        self.assertEqual(fingerprint["probhub_version"], "9.8.7")
        self.assertEqual(
            fingerprint["build_manifest_schema_version"],
            BUILD_MANIFEST_SCHEMA_VERSION,
        )
        self.assertEqual(
            fingerprint["generation_schema_version"],
            GENERATION_SCHEMA_VERSION,
        )
        self.assertEqual(fingerprint["typst_version"], "0.14.2")
        self.assertEqual(fingerprint["pypdf_version"], "6.14.2")
        self.assertEqual(fingerprint["font"]["sha256"], FIXED_FONT_SHA256)
        self.assertEqual(len(fingerprint["digest"]), 64)
        for excluded in ("node", "g++", "flask", "os", "platform"):
            self.assertNotIn(excluded, fingerprint)

    def test_stale_reasons_are_field_level(self):
        baseline = {
            "schema_version": 1,
            "probhub_version": "0.6.2",
            "build_manifest_schema_version": 4,
            "generation_schema_version": 3,
            "typst_version": "0.14.2",
            "pypdf_version": "6.14.2",
            "template_hash": "template",
            "font": {
                "family": "Noto Sans CJK SC",
                "policy": "bundled-only-v1",
                "sha256": "font",
            },
            "digest": "baseline",
        }
        cases = (
            (("schema_version",), "builder_fingerprint.schema_version"),
            (("probhub_version",), "builder_fingerprint.probhub_version"),
            (("build_manifest_schema_version",), "builder_fingerprint.build_manifest_schema_version"),
            (("generation_schema_version",), "builder_fingerprint.generation_schema_version"),
            (("typst_version",), "builder_fingerprint.typst_version"),
            (("pypdf_version",), "builder_fingerprint.pypdf_version"),
            (("template_hash",), "builder_fingerprint.template_hash"),
            (("font", "family"), "builder_fingerprint.font.family"),
            (("font", "policy"), "builder_fingerprint.font.policy"),
            (("font", "sha256"), "builder_fingerprint.font.sha256"),
        )
        for path, expected in cases:
            with self.subTest(field=".".join(path)):
                changed = copy.deepcopy(baseline)
                target = changed
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "changed"
                changed["digest"] = "changed"
                self.assertEqual(
                    builder_fingerprint_stale_fields(baseline, changed),
                    [expected],
                )

        digest_only = {**baseline, "digest": "changed"}
        self.assertEqual(
            builder_fingerprint_stale_fields(baseline, digest_only),
            ["builder_fingerprint.digest"],
        )

    def test_missing_tool_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self.create_workspace(root)
            for toolchain, message in (
                (self.toolchain(typst=None), "Typst"),
                (self.toolchain(pypdf=None), "pypdf"),
            ):
                with self.subTest(message=message), patch(
                    "probhub.builder_fingerprint.builder_toolchain_status",
                    return_value=toolchain,
                ):
                    with self.assertRaises(ProbHubError) as raised:
                        compute_builder_fingerprint(root, workspace)
                    self.assertEqual(
                        raised.exception.code,
                        "builder_fingerprint_failed",
                    )
                    self.assertIn(message, str(raised.exception))

    def test_bundled_font_and_license_are_present(self):
        identity = fixed_font_identity()
        self.assertEqual(identity["sha256"], FIXED_FONT_SHA256)
        self.assertTrue(fixed_font_path().with_name("OFL.txt").is_file())


if __name__ == "__main__":
    unittest.main()
