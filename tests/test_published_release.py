import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_clean_install, check_published_release, check_release


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.6.7"
REGISTRY = check_published_release.DEFAULT_REGISTRY


def _package(name, *, latest=VERSION, dependencies=None, integrity=True):
    payload = {
        "_id": f"{name}@{VERSION}",
        "name": name,
        "version": VERSION,
        "dist-tags": {"latest": latest},
        "dist": {
            "integrity": (
                "sha512-YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo="
                if integrity
                else "sha1-invalid"
            ),
            "shasum": "a" * 40,
            "tarball": f"{REGISTRY}{name}/-/{name}-{VERSION}.tgz",
        },
    }
    if dependencies is not None:
        payload["dependencies"] = dependencies
    return payload


class PublishedReleaseTests(unittest.TestCase):
    def test_npm_view_json_accepts_object_and_single_object_array(self):
        expected = _package("probhub")
        for payload in (expected, [expected]):
            with self.subTest(payload_type=type(payload).__name__):
                parsed = check_published_release._parse_object(
                    json.dumps(payload),
                    code="npm_view_invalid_json",
                    source="npm view probhub@0.6.7",
                )
                self.assertEqual(parsed, expected)

    def test_npm_view_json_rejects_non_object_shapes(self):
        for payload in ([], [_package("probhub"), _package("probhub")], "text", 7):
            with self.subTest(payload=payload):
                with self.assertRaises(check_published_release.PublishedReleaseError) as caught:
                    check_published_release._parse_object(
                        json.dumps(payload),
                        code="npm_view_invalid_json",
                        source="npm view probhub@0.6.7",
                    )
                self.assertEqual(caught.exception.code, "npm_view_invalid_json")

    def test_bounded_json_command_has_stable_transport_and_json_failures(self):
        scenarios = (
            (
                {"reason": "timeout", "returncode": None, "stdout": "", "stderr": "timed out"},
                "npm_view_failed",
            ),
            (
                {"reason": "completed", "returncode": 0, "stdout": "not-json", "stderr": ""},
                "npm_view_failed_invalid_json",
            ),
        )
        for result, expected_code in scenarios:
            with (
                self.subTest(expected_code=expected_code),
                patch("scripts.check_published_release.run_bounded", return_value=result),
                self.assertRaises(check_published_release.PublishedReleaseError) as caught,
            ):
                check_published_release._run_json(
                    ["npm", "view", "probhub@0.6.7", "--json"],
                    code="npm_view_failed",
                    source="npm view probhub@0.6.7",
                )
            self.assertEqual(caught.exception.code, expected_code)

    def test_first_party_actions_use_node24_major_and_release_flow_is_manual(self):
        workflow_root = ROOT / ".github/workflows"
        for path in workflow_root.glob("*.yml"):
            content = path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if "uses: actions/" in line:
                    with self.subTest(workflow=path.name, action=line.strip()):
                        self.assertTrue(
                            line.strip().endswith("@v7"),
                            f"first-party action must use the Node.js 24 v7 major: {line}",
                        )

        published = (workflow_root / "published-release.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", published)
        self.assertNotIn("\n  release:", published)
        self.assertNotIn("\n  push:", published)
        self.assertIn("os: [ubuntu-latest, windows-latest]", published)
        self.assertIn("ref: refs/tags/v${{ inputs.version }}", published)
        self.assertIn('--registry-version "${{ inputs.version }}"', published)
        self.assertIn("scripts/check_published_release.py", published)
        self.assertIn("scripts/check_clean_install.py", published)
        self.assertIn("if: always()", published)

    def test_ci_only_release_scripts_are_not_published(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertIn("!scripts/check_clean_install.py", package["files"])
        self.assertIn("!scripts/check_published_release.py", package["files"])
        self.assertIn("!scripts/check_release.py", package["files"])
        self.assertIn("release-results/", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_registry_verification_accepts_exact_dual_package_release(self):
        manifests = [
            _package("probhub"),
            _package("probhub-skill", dependencies={"probhub": VERSION}),
        ]
        inventories = {
            "main": {"filename": "probhub-0.6.7.tgz", "files": 225},
            "compat": {"filename": "probhub-skill-0.6.7.tgz", "files": 5},
        }
        with (
            patch(
                "scripts.check_published_release._npm_metadata",
                side_effect=manifests,
            ),
            patch(
                "scripts.check_published_release.validate_pack_inventories",
                return_value=inventories,
            ) as pack,
        ):
            result = check_published_release.validate_npm_registry(VERSION)
        self.assertEqual(result["inventories"], inventories)
        self.assertEqual(
            result["packages"]["probhub-skill"]["dependencies"],
            {"probhub": VERSION},
        )
        pack.assert_called_once_with(
            main_target=f"probhub@{VERSION}",
            compat_target=f"probhub-skill@{VERSION}",
            registry_url=REGISTRY,
        )

    def test_registry_verification_has_stable_identity_failures(self):
        scenarios = {
            "latest": (
                [
                    _package("probhub", latest="0.6.6"),
                    _package("probhub-skill", dependencies={"probhub": VERSION}),
                ],
                "npm_latest_mismatch",
            ),
            "compat dependency": (
                [
                    _package("probhub"),
                    _package("probhub-skill", dependencies={"probhub": "^0.6.7"}),
                ],
                "npm_compat_dependency_mismatch",
            ),
            "dist identity": (
                [
                    _package("probhub", integrity=False),
                    _package("probhub-skill", dependencies={"probhub": VERSION}),
                ],
                "npm_dist_identity_invalid",
            ),
            "tarball identity": (
                [
                    {
                        **_package("probhub"),
                        "dist": {
                            **_package("probhub")["dist"],
                            "tarball": "https://registry.npmjs.org.invalid/probhub.tgz",
                        },
                    },
                    _package("probhub-skill", dependencies={"probhub": VERSION}),
                ],
                "npm_dist_identity_invalid",
            ),
        }
        for label, (manifests, code) in scenarios.items():
            with self.subTest(label=label), patch(
                "scripts.check_published_release._npm_metadata",
                side_effect=manifests,
            ):
                with self.assertRaises(check_published_release.PublishedReleaseError) as caught:
                    check_published_release.validate_npm_registry(VERSION)
                self.assertEqual(caught.exception.code, code)

    def test_registry_verification_rejects_non_exact_input_and_alternate_registry(self):
        for version in ("latest", "v0.6.7", "0.6", "0.6.7 || echo bad"):
            with self.subTest(version=version):
                with self.assertRaises(check_published_release.PublishedReleaseError) as caught:
                    check_published_release.validate_npm_registry(version)
                self.assertEqual(caught.exception.code, "invalid_version")
        with self.assertRaises(check_published_release.PublishedReleaseError) as caught:
            check_published_release.validate_npm_registry(
                VERSION,
                registry_url="https://registry.example.invalid/",
            )
        self.assertEqual(caught.exception.code, "registry_url_invalid")

    def test_github_release_requires_published_stable_exact_tag(self):
        release = {
            "name": "ProbHub 0.6.7",
            "tagName": "v0.6.7",
            "isDraft": False,
            "isPrerelease": False,
            "publishedAt": "2026-08-16T09:46:26Z",
            "targetCommitish": "main",
            "url": "https://github.com/greenthree/ProbHub-skill/releases/tag/v0.6.7",
        }
        with (
            patch("scripts.check_published_release.shutil.which", return_value="gh"),
            patch("scripts.check_published_release._run_json", return_value=release),
        ):
            self.assertEqual(
                check_published_release.validate_github_release(
                    VERSION,
                    "greenthree/ProbHub-skill",
                )["tagName"],
                "v0.6.7",
            )

        for field, value, code in (
            ("tagName", "v0.6.6", "github_release_tag_mismatch"),
            ("isDraft", True, "github_release_state_mismatch"),
            ("isPrerelease", True, "github_release_state_mismatch"),
            ("publishedAt", None, "github_release_state_mismatch"),
        ):
            payload = dict(release)
            payload[field] = value
            with (
                self.subTest(field=field),
                patch("scripts.check_published_release.shutil.which", return_value="gh"),
                patch("scripts.check_published_release._run_json", return_value=payload),
                self.assertRaises(check_published_release.PublishedReleaseError) as caught,
            ):
                check_published_release.validate_github_release(
                    VERSION,
                    "greenthree/ProbHub-skill",
                )
            self.assertEqual(caught.exception.code, code)

    def test_cli_atomically_writes_failure_result(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "nested/result.json"
            code = check_published_release.main(
                [
                    "--version",
                    "latest",
                    "--repo",
                    "greenthree/ProbHub-skill",
                    "--output",
                    str(output),
                    "--json",
                ]
            )
            self.assertEqual(code, 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "invalid_version")
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

    def test_atomic_output_cleans_temporary_file_when_publish_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result.json"
            with (
                patch(
                    "scripts.check_published_release.os.replace",
                    side_effect=OSError("publish failed"),
                ),
                self.assertRaises(OSError),
            ):
                check_published_release.atomic_write_json(output, {"ok": True})
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

    def test_clean_install_registry_plan_never_uses_local_tarballs(self):
        published = {
            "inventories": {
                "main": {"filename": "probhub-0.6.7.tgz", "files": 225},
                "compat": {"filename": "probhub-skill-0.6.7.tgz", "files": 5},
            }
        }
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "scripts.check_clean_install.validate_npm_registry",
                return_value=published,
            ) as registry,
            patch(
                "scripts.check_clean_install.validate_pack_inventories"
            ) as local_pack,
        ):
            plan = check_clean_install._package_install_plan(
                registry_version=VERSION,
                registry_url=REGISTRY,
                dist=Path(temp),
            )
        self.assertEqual(plan["source"], "npm-registry")
        self.assertEqual(
            plan["install_targets"],
            [f"probhub@{VERSION}", f"probhub-skill@{VERSION}"],
        )
        registry.assert_called_once_with(VERSION, registry_url=REGISTRY)
        local_pack.assert_not_called()

    def test_pack_manifest_passes_registry_as_an_argv_token(self):
        completed = {
            "reason": "completed",
            "returncode": 0,
            "stdout": json.dumps(
                [{"name": "probhub", "files": [{"path": "package.json"}]}]
            ),
            "stderr": "",
        }
        with (
            patch("scripts.check_release.shutil.which", return_value="npm"),
            patch("scripts.check_release.run_bounded", return_value=completed) as run,
        ):
            check_release.npm_pack_manifest(
                f"probhub@{VERSION}",
                registry_url=REGISTRY,
            )
        command = run.call_args.args[0]
        self.assertIn(f"probhub@{VERSION}", command)
        self.assertIn(f"--registry={REGISTRY}", command)
        self.assertNotIn("latest", command)


if __name__ == "__main__":
    unittest.main()
