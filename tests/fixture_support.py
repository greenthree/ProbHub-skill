from dataclasses import dataclass
from pathlib import Path
import shutil

from probhub.hashing import hash_file
from probhub.io import read_yaml, write_json, write_yaml
from probhub.linting import (
    BUILD_MANIFEST_SCHEMA_VERSION,
    compute_data_hash,
    compute_source_hash,
)
from probhub.workspace import load_problem, load_workspace, problem_entries


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
JUDGE_QA_FIXTURES = ("checker-qa", "interactor-qa")
WORKSPACE_FIXTURES = (
    "standard",
    "walkthrough",
    "custom",
    "float",
    "interactive",
    "stress",
    "mutation",
    "mutation-many-light",
    "mutation-few-heavy",
    "mutation-mixed-runtime",
    *JUDGE_QA_FIXTURES,
)


@dataclass(frozen=True)
class WorkspaceFixture:
    name: str
    root: Path
    problem: Path
    problem_id: str

    @property
    def config_path(self):
        return self.problem / "probhub.yaml"

    def config(self):
        return read_yaml(self.config_path)


def copy_workspace_fixture(name, destination):
    if name not in WORKSPACE_FIXTURES:
        raise ValueError(f"unknown workspace fixture: {name}")
    source = FIXTURE_ROOT / "workspaces" / name
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(f"fixture destination must be empty: {destination}")
    shutil.copytree(source, destination, dirs_exist_ok=True)
    root, workspace = load_workspace(destination)
    entries = problem_entries(workspace)
    if len(entries) != 1:
        raise ValueError(f"fixture {name} must contain exactly one problem")
    problem, _ = load_problem(root, entries[0])
    return WorkspaceFixture(name, root, problem, str(entries[0]["id"]))


def inject_fault(fixture, name):
    base_by_fault = {
        "validator": "standard",
        "checker": "custom",
        "ole": "standard",
        "process-limit": "standard",
    }
    expected_base = base_by_fault.get(name)
    if expected_base is None:
        raise ValueError(f"unknown fixture fault: {name}")
    if fixture.name != expected_base:
        raise ValueError(f"fault {name} requires the {expected_base} fixture")

    source_name = {
        "validator": "validator-reject.cpp",
        "checker": "checker-fail.cpp",
        "ole": "ole.cpp",
        "process-limit": "process-limit.cpp",
    }[name]
    target_name = f"fixture-{source_name}"
    shutil.copy2(
        FIXTURE_ROOT / "faults" / source_name,
        fixture.problem / "code" / target_name,
    )

    config = fixture.config()
    relative = f"code/{target_name}"
    if name == "validator":
        config["judge"]["validator"] = relative
    elif name == "checker":
        config["judge"]["checker"] = relative
    elif name == "ole":
        config["solutions"]["wrong"].append({
            "file": relative,
            "expected": {"status": "OLE", "all": True},
        })
    else:
        config["limits"]["processes"] = 1
        config["solutions"]["wrong"].append({
            "file": relative,
            "expected": {"status": "RE", "all": True},
        })
    write_yaml(fixture.config_path, config)
    return relative


def publish_current_status_artifacts(fixture):
    config = fixture.config()
    pdf = fixture.problem / "problem.pdf"
    package = fixture.root / f"{fixture.problem_id}.zip"
    pdf.write_bytes(b"%PDF-1.4\nfixture\n")
    package.write_bytes(b"fixture package")
    manifest = {
        "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
        "batch_id": "fixture-batch",
        "sealed_revision_id": "fixture-revision",
        "builder_fingerprint": {"fixture": True},
        "source_hash": compute_source_hash(fixture.problem, config),
        "data_hash": compute_data_hash(fixture.problem, config),
        "pdf_hash": hash_file(pdf),
        "package_hash": hash_file(package),
    }
    write_json(fixture.problem / ".probhub/build-manifest.json", manifest)
    return pdf, package
