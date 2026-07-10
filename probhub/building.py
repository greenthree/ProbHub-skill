from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .errors import ProbHubError
from .hashing import hash_file
from .io import write_json
from .judging import judge_problem
from .linting import compute_data_hash, compute_source_hash, compute_workspace_hash, lint_workspace
from .package_tools import build_package, generate_domjudge_config, validate_output_validator_source, verify_package
from .typesetting import compile_collection, extract_problem_pdfs
from .workspace import load_problem


def load_selected(root, entries):
    return [load_problem(root, entry) for entry in entries]


def write_manifest(root, workspace, problem_dir, config, package_path):
    manifest = {
        "schema_version": 1,
        "problem_id": config["id"],
        "source_hash": compute_source_hash(problem_dir, config),
        "data_hash": compute_data_hash(problem_dir, config),
        "workspace_hash": compute_workspace_hash(root, workspace),
        "pdf_hash": hash_file(problem_dir / "problem.pdf"),
        "package_hash": hash_file(package_path),
        "built_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "probhub_version": __version__,
    }
    write_json(problem_dir / ".probhub/build-manifest.json", manifest)
    return manifest


def package_problem(root, problem_dir, config, require_pdf=True):
    generate_domjudge_config(problem_dir, config)
    validate_output_validator_source(problem_dir, config)
    output = root / f"{config['id']}.zip"
    build_package(problem_dir, output)
    verification = verify_package(output, require_pdf=require_pdf)
    if not verification["ok"]:
        raise ProbHubError(f"package verification failed for {config['id']}: {'; '.join(verification['errors'])}")
    return output, verification


def build_workspace(root, workspace, entries, run_judge=True, use_judge_cache=True):
    lint = lint_workspace(root, workspace, entries)
    if not lint["ok"]:
        messages = []
        for result in lint["problems"]:
            messages.extend(f"{result['id']}: {error}" for error in result["errors"])
        raise ProbHubError("lint failed: " + "; ".join(messages + lint.get("errors", [])))
    selected = load_selected(root, entries)
    normalized = []
    from .workspace import problem_entries
    for entry in problem_entries(workspace):
        normalized.append(load_problem(root, entry))
    judge_results = {}
    if run_judge:
        for problem_dir, config in selected:
            result = judge_problem(root, problem_dir, use_cache=use_judge_cache)
            judge_results[config["id"]] = {
                "ok": result["ok"],
                "returncode": result["returncode"],
                "final": result["final"],
                "cache": result.get("cache", {}),
            }
            if not result["ok"]:
                raise ProbHubError(f"sandbox failed for {config['id']}: {result.get('final')}")
    typst_dir, main_pdf, _ = compile_collection(root, workspace, normalized)
    target_ids = {config["id"] for _, config in selected}
    pdfs = extract_problem_pdfs(main_pdf, normalized, only_ids=target_ids)
    packages, manifests = {}, {}
    for problem_dir, config in selected:
        package_path, verification = package_problem(root, problem_dir, config, require_pdf=True)
        packages[config["id"]] = {"path": str(package_path), "verification": verification}
        manifests[config["id"]] = write_manifest(root, workspace, problem_dir, config, package_path)
    return {
        "ok": True,
        "typst_dir": str(typst_dir),
        "main_pdf": str(main_pdf),
        "pdfs": pdfs,
        "packages": packages,
        "manifests": manifests,
        "judge": judge_results,
    }
