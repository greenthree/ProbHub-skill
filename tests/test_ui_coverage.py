import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from probhub.io import read_yaml, write_yaml


ROOT = Path(__file__).resolve().parents[1]


def load_ui():
    spec = importlib.util.spec_from_file_location(
        "coverage_ui", ROOT / "scripts" / "ui.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UiCoverageTests(unittest.TestCase):
    """API and frontend contract tests for the read-only coverage cockpit."""

    @classmethod
    def setUpClass(cls):
        cls.ui = load_ui()
        cls.client = cls.ui.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.ui.shutdown_webui_tasks()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_cwd = Path.cwd()
        os.chdir(self.root)
        (self.root / ".probhub").mkdir()
        (self.root / "typst-statement" / "Contest").mkdir(parents=True)
        (self.root / "typst-statement" / "Contest" / "main.typ").write_text(
            "// fixture\n", encoding="utf-8"
        )
        write_yaml(
            self.root / ".probhub" / "workspace.yaml",
            {
                "schema_version": 1,
                "contest": {"title": "Fixture", "subtitle": "Contest"},
                "typst": {"directory": "typst-statement/Contest"},
                "problems": [{"id": "A", "directory": "A"}],
            },
        )
        (self.root / "A").mkdir()

    def tearDown(self):
        os.chdir(self.original_cwd)
        self.temp.cleanup()

    @staticmethod
    def coverage_payload():
        return {
            "success": True,
            "schema_version": 1,
            "workspace": {
                "title": "Fixture",
                "subtitle": "Contest",
                "problem_count": 1,
                "selected_count": 1,
            },
            "problems": [
                {
                    "id": "A",
                    "coverage": {
                        "groups": [
                            {
                                "name": "secret",
                                "role": "secret",
                                "patterns": ["secret/*"],
                                "targets": ["accepted"],
                                "sample_cases": 0,
                                "secret_cases": 2,
                                "total_cases": 2,
                                "secret_ratio": 1.0,
                            }
                        ],
                        "ungrouped_secret_cases": 0,
                        "recipes": [
                            {
                                "recipe_hash": "abc123",
                                "manual": False,
                                "generator": "gen.py",
                                "present": True,
                                "groups": ["secret"],
                            }
                        ],
                        "secret_summary": {
                            "cases": 2,
                            "input_bytes": 12,
                            "answer_bytes": 16,
                        },
                    },
                    "wrong": [
                        {
                            "program": "wrong.cpp",
                            "expected_statuses": ["WA"],
                            "expected_groups": ["secret"],
                            "actual_status": "WA",
                            "matched_case_count": 2,
                            "state": "matched",
                        }
                    ],
                    "judge_qa": {
                            "state": "not-configured",
                            "configured": False,
                            "declared_cases": 0,
                            "evidence_cases": 0,
                            "matched_case_count": 0,
                            "probes": 0,
                            "manual_review_required": False,
                    },
                    "impact": {
                        "state": "current",
                        "source_revision": "rev-1",
                        "stale_fields": [],
                        "requires": [],
                    },
                    "delivery": {
                        "sealed": True,
                        "qa": "not-configured",
                        "evidence": "current",
                        "pdf": "current",
                        "zip": "current",
                        "manifest": "current",
                        "requires": [],
                    },
                }
            ],
            "delivery": {"state": "none", "items": [], "remediations": []},
        }

    def test_empty_workspace_returns_explicit_empty_schema(self):
        no_selection = self.client.get("/api/coverage")
        self.assertEqual(no_selection.status_code, 200)
        self.assertEqual(no_selection.get_json()["problems"], [])

        workspace_path = self.root / ".probhub" / "workspace.yaml"
        workspace = read_yaml(workspace_path)
        workspace["problems"] = []
        write_yaml(workspace_path, workspace)
        response = self.client.get("/api/coverage?collection=Contest")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["problems"], [])
        self.assertIn("delivery", payload)

    def test_collection_selector_rejects_legacy_name_and_unknown_problem(self):
        legacy = self.client.get("/api/coverage?subtitle=Contest")
        self.assertEqual(legacy.status_code, 400)
        self.assertEqual(legacy.get_json()["code"], "invalid_collection_selector")
        unknown = self.client.get("/api/coverage?collection=Contest&problem=NOPE")
        self.assertEqual(unknown.status_code, 400)
        self.assertFalse(unknown.get_json()["success"])

    def test_normal_request_returns_bounded_coverage_projection(self):
        expected = self.coverage_payload()
        with mock.patch.object(self.ui, "build_coverage_summary", return_value=expected) as builder:
            response = self.client.get("/api/coverage?collection=Contest&problem=A")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload, expected)
        builder.assert_called_once()
        args, kwargs = builder.call_args
        root_arg = kwargs.get("root", args[0] if args else None)
        self.assertEqual(Path(root_arg).resolve(), self.root.resolve())
        selected_ids = kwargs.get("selected_ids")
        if selected_ids is None and len(args) >= 3:
            selected_ids = args[2]
        self.assertEqual(selected_ids, ["A"])

    def test_stale_coverage_preserves_impact_and_remediation_fields(self):
        import probhub.webui_coverage as coverage_core

        report = {
            "workspace": {},
            "problems": [{
                "id": "A",
                "name": "Alpha",
                "groups": [],
                "ungrouped_secret_cases": 0,
                "recipes": {"profiles": []},
                "tests": {"secret": {"cases": 1, "input_bytes": 1, "answer_bytes": 1}},
                "kill_matrix": {"rows": []},
                "diagnostics": [],
            }],
            "diagnostics": [],
        }
        health = {
            "problems": [{
                "id": "A",
                "checkpoint": {"state": "stale", "revision_id": "rev-1"},
                "artifacts": {
                    "pdf": "stale",
                    "zip": "stale",
                    "manifest": "stale",
                    "stale_fields": ["source_hash", "judge"],
                },
                "checks": {"judge_qa": {"state": "stale", "configured": True, "applicable": True}},
                "evidence": {"calibration": {"state": "stale"}},
                "remediations": [{
                    "action_code": "refresh_judge",
                    "command": ["probhub", "judge", "A", "--no-cache"],
                    "manual_review_required": False,
                }],
            }],
            "generation": {"complete": False},
            "diagnostics": [],
        }
        with (
            mock.patch.object(coverage_core, "build_workspace_report", return_value=report),
            mock.patch.object(coverage_core, "build_health_summary", return_value=health),
        ):
            response = self.client.get("/api/coverage?collection=Contest")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            payload["problems"][0]["impact"]["stale_fields"],
            ["source_hash", "judge"],
        )
        self.assertEqual(payload["delivery"]["state"], "needs-action")
        self.assertEqual(
            payload["delivery"]["remediations"][0]["action_code"],
            "refresh_judge",
        )

    def test_corrupt_core_result_fails_closed_with_structured_error(self):
        with mock.patch.object(
            self.ui,
            "build_coverage_summary",
            side_effect=ValueError("malformed coverage evidence"),
        ):
            response = self.client.get("/api/coverage?collection=Contest")
        self.assertEqual(response.status_code, 500)
        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["code"], "coverage_failed")
        self.assertNotIn("malformed coverage evidence", response.get_data(as_text=True))

    def test_secret_details_are_not_exposed_by_endpoint_projection(self):
        # Feed raw secret/evidence/path fields through the Core report seam and
        # verify that the real projection strips them before jsonify().
        import probhub.webui_coverage as coverage_core

        report = {
            "workspace": {},
            "problems": [{
                "id": "A",
                "name": "Alpha",
                "groups": [],
                "ungrouped_secret_cases": 0,
                "recipes": {"profiles": []},
                "tests": {"secret": {"cases": 1, "input_bytes": 1, "answer_bytes": 1}},
                "kill_matrix": {"rows": []},
                "secret_content": "PRIVATE INPUT CONTENT",
                "evidence": {"transcript": "PRIVATE TRANSCRIPT"},
                "path": "data/secret/case-1.in",
            }],
            "diagnostics": [],
        }
        health = {
            "problems": [{
                "id": "A",
                "checkpoint": {"state": "sealed", "revision_id": "rev-1"},
                "artifacts": {"pdf": "current", "zip": "current", "manifest": "current", "stale_fields": []},
                "checks": {"judge_qa": {"state": "not-configured", "configured": False}},
                "evidence": {"calibration": {"state": "current"}},
                "remediations": [],
            }],
            "generation": {"complete": True},
            "diagnostics": [],
        }
        with (
            mock.patch.object(coverage_core, "build_workspace_report", return_value=report),
            mock.patch.object(coverage_core, "build_health_summary", return_value=health),
        ):
            response = self.client.get("/api/coverage?collection=Contest")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("PRIVATE INPUT CONTENT", body)
        self.assertNotIn("PRIVATE TRANSCRIPT", body)
        self.assertNotIn("data/secret/case-1.in", body)
        # ``delivery.evidence`` is an allowed bounded state token; only raw
        # evidence documents and filesystem paths must remain absent.
        self.assertNotIn('"transcript"', body)
        self.assertNotIn('"path"', body)
        self.assertNotIn("case-1", body)
        self.assertNotIn("case-2", body)

    def test_recipe_projection_uses_hash_without_case_or_args(self):
        import probhub.webui_coverage as coverage_core

        projected = coverage_core._recipe_projection([{
            "case": "secret-private-case",
            "manual": False,
            "generator": "code/generator.cpp",
            "args": ["secret-private-case", "999999"],
            "present": True,
            "groups": ["boundary"],
        }])[0]
        self.assertIn("recipe_hash", projected)
        self.assertNotIn("case", projected)
        self.assertNotIn("args", projected)
        self.assertNotIn("secret-private-case", str(projected))

    def test_coverage_get_is_byte_for_byte_read_only(self):
        generated = self.root / "A" / "meta.json"
        generated.write_bytes(b"legacy generated artifact")
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        with mock.patch.object(self.ui, "build_coverage_summary", return_value=self.coverage_payload()):
            response = self.client.get("/api/coverage?collection=Contest")
        self.assertEqual(response.status_code, 200)
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_frontend_fetches_coverage_with_encoded_subtitle(self):
        html = self.ui.HTML_TEMPLATE
        javascript = (self.ui.WEBUI_ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/coverage", html + javascript)
        self.assertRegex(
            javascript,
            r"fetch\(`?/api/coverage\?collection=\$\{encodeURIComponent\(",
        )

    def test_frontend_coverage_assets_remain_under_strict_csp(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("https://", csp)
        self.assertNotIn("http://", csp)


if __name__ == "__main__":
    unittest.main()
