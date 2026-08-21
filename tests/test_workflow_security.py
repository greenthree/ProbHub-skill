import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"


def _load_workflow(name):
    """Parse GitHub Actions YAML without YAML 1.1 turning `on` into True."""
    path = WORKFLOW_ROOT / name
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class WorkflowSecurityTests(unittest.TestCase):
    def test_all_first_party_workflows_parse_and_are_read_only(self):
        workflows = sorted(WORKFLOW_ROOT.glob("*.yml"))
        self.assertEqual(
            {path.name for path in workflows},
            {"ci.yml", "mutation-benchmark.yml", "published-release.yml"},
        )
        for path in workflows:
            with self.subTest(workflow=path.name):
                document = _load_workflow(path.name)
                self.assertIsInstance(document, dict)
                self.assertEqual(document.get("permissions"), {"contents": "read"})
                self.assertIsInstance(document.get("jobs"), dict)
                for job_name, job in document["jobs"].items():
                    with self.subTest(workflow=path.name, job=job_name):
                        self.assertNotIn("permissions", job)

    def test_ci_covers_pr_main_tag_and_dependency_audit_schedule_without_writes(self):
        workflow = _load_workflow("ci.yml")
        triggers = workflow["on"]
        self.assertIn("pull_request", triggers)
        self.assertEqual(triggers["push"]["branches"], ["main"])
        self.assertEqual(triggers["push"]["tags"], ["v*"])
        self.assertEqual(triggers["schedule"][0]["cron"], "17 2 * * 1")
        self.assertIn("python-dependency-audit", workflow["jobs"])
        self.assertNotIn("write", repr(workflow["permissions"]).lower())
        self.assertNotIn("pull_request_target", triggers)
        self.assertNotIn("workflow_run", triggers)
        for name, job in workflow["jobs"].items():
            if name == "python-dependency-audit":
                self.assertNotIn("if", job)
            else:
                self.assertEqual(job["if"], "github.event_name != 'schedule'")

    def test_mutation_and_published_release_keep_their_read_only_boundaries(self):
        mutation = _load_workflow("mutation-benchmark.yml")
        self.assertIn("pull_request", mutation["on"])
        self.assertIn("workflow_dispatch", mutation["on"])
        self.assertIn("schedule", mutation["on"])

        published = _load_workflow("published-release.yml")
        self.assertEqual(set(published["on"]), {"workflow_dispatch"})
        self.assertEqual(published["permissions"], {"contents": "read"})


if __name__ == "__main__":
    unittest.main()
