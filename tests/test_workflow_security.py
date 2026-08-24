import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"


def _load_workflow(name):
    """Parse GitHub Actions YAML without YAML 1.1 turning `on` into True."""
    path = WORKFLOW_ROOT / name
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _step_runs(job):
    return [step["run"] for step in job["steps"] if isinstance(step, dict) and "run" in step]


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

    def test_runtime_installers_share_the_hash_locked_identity(self):
        command = (
            "python -m pip install --require-hashes "
            "--only-binary=:all: -r requirements.lock"
        )
        ci = _load_workflow("ci.yml")
        mutation = _load_workflow("mutation-benchmark.yml")
        published = _load_workflow("published-release.yml")

        self.assertEqual(
            ci["jobs"]["quality"]["strategy"]["matrix"]["python"],
            ["3.10", "3.11", "3.12"],
        )
        self.assertEqual(
            ci["jobs"]["python-dependency-audit"]["strategy"]["matrix"]["python"],
            ["3.10", "3.11", "3.12"],
        )
        self.assertIn(command, _step_runs(ci["jobs"]["quality"]))
        self.assertIn(command, _step_runs(mutation["jobs"]["shadow-matrix"]))
        self.assertIn(command, _step_runs(published["jobs"]["release-metadata"]))

        workflow_text = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(WORKFLOW_ROOT.glob("*.yml"))
        )
        self.assertNotIn("pip install -r requirements.txt", workflow_text)
        self.assertNotIn("pip install --require-hashes -r requirements.txt", workflow_text)
        self.assertNotIn("pip install -r requirements-audit.txt", workflow_text)
        self.assertNotIn("pip install --upgrade pip", workflow_text)
        audit_command = (
            "python -m pip install --require-hashes "
            "--only-binary=:all: -r requirements-audit.lock"
        )
        self.assertIn(
            audit_command,
            _step_runs(ci["jobs"]["python-dependency-audit"]),
        )

    def test_ci_shards_keep_full_platform_matrix_and_parallel_static_gate(self):
        ci = _load_workflow("ci.yml")
        self.assertIn("quality-static", ci["jobs"])
        quality = ci["jobs"]["quality"]
        matrix = quality["strategy"]["matrix"]
        self.assertEqual(matrix["os"], ["ubuntu-latest", "windows-latest"])
        self.assertEqual(matrix["python"], ["3.10", "3.11", "3.12"])
        self.assertEqual(
            matrix["shard"],
            ["core", "execution", "mutation-and-qa", "webui-and-delivery"],
        )
        self.assertNotIn("needs", quality)
        node_steps = [
            step for step in quality["steps"]
            if step.get("uses") == "actions/setup-node@v7"
        ]
        self.assertEqual(len(node_steps), 1)
        self.assertEqual(
            node_steps[0].get("if"),
            "matrix.shard == 'webui-and-delivery'",
        )
        npm_steps = [
            step for step in quality["steps"]
            if step.get("run") == "npm ci"
        ]
        self.assertEqual(len(npm_steps), 1)
        self.assertEqual(
            npm_steps[0].get("if"),
            "matrix.shard == 'webui-and-delivery'",
        )
        self.assertIn(
            'python -m probhub.test_shards --shard "${{ matrix.shard }}"',
            _step_runs(quality),
        )
        benchmark_steps = [
            step for step in quality["steps"]
            if step.get("run")
            == "python scripts/benchmark_process_startup.py --rounds 40 --warmup 5"
        ]
        self.assertEqual(len(benchmark_steps), 1)
        self.assertEqual(
            benchmark_steps[0].get("if"),
            "matrix.shard == 'execution'",
        )


if __name__ == "__main__":
    unittest.main()
