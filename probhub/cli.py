import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .build_lock import workspace_build_lock
from .building import build_workspace, package_problem
from .doctor import run_doctor
from .errors import ProbHubError
from .generations import (
    assemble_exam_generation,
    create_problem_checkpoint,
    generation_status,
)
from .io import write_yaml
from .judging import judge_problem
from .linting import (
    compute_collection_hash,
    compute_data_hash,
    compute_source_hash,
    compute_workspace_hash,
    lint_workspace,
    problem_status,
)
from .package_tools import verify_package
from .stressing import stress_problem
from .typesetting import compile_collection, extract_problem_pdfs
from .workspace import WORKSPACE_FILE, find_workspace, load_problem, load_workspace, problem_entries, select_entries


def configure_stdout():
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def emit(data, json_output=False):
    if json_output:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, str):
            print(data)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))


def workspace_context(args, allow_empty=False):
    root = find_workspace(getattr(args, "workspace", None))
    return load_workspace(root, allow_empty=allow_empty)


def _ensure_local_gitignore(root):
    path = Path(root) / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    required = [
        "**/.probhub/build.lock",
        "**/.probhub/checkpoints/",
        "**/.probhub/checkpoint-tmp/",
        "**/.probhub/generation.lock",
        "**/.probhub/generations/",
        "**/.probhub/generation-tmp/",
        "**/.probhub/sandbox-cache-v1.json",
        "**/.probhub/sandbox-cache-v1.json.tmp",
        "**/.probhub/stress/",
        "**/.probhub-*.typ",
    ]
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in required if line not in present]
    if not missing:
        return
    content = existing.rstrip()
    if content:
        content += "\n\n"
    content += "# Local ProbHub artifacts\n" + "\n".join(missing) + "\n"
    path.write_text(content, encoding="utf-8")


def command_init(args):
    root = Path(args.directory or Path.cwd()).resolve()
    path = root / WORKSPACE_FILE
    if path.exists() and not args.force:
        raise ProbHubError(f"workspace already exists: {path}")
    data = {
        "schema_version": 1,
        "contest": {"title": args.title or root.name, "subtitle": args.subtitle or "正式赛", "author": args.author or ""},
        "typst": {"directory": f"typst-statement/{args.subtitle or '正式赛'}"},
        "problems": [],
        "lint": {"forbidden_patterns": ["TODO", "FIXME", "114514", "待补充"]},
    }
    write_yaml(path, data)
    _ensure_local_gitignore(root)
    return {"ok": True, "workspace": str(path)}


def command_new(args):
    root, workspace = workspace_context(args, allow_empty=True)
    problem_id = args.problem_id.strip()
    if any(entry["id"] == problem_id for entry in problem_entries(workspace)):
        raise ProbHubError(f"problem id already exists: {problem_id}")
    directory = args.directory or problem_id
    problem_dir = root / directory
    if problem_dir.exists() and any(problem_dir.iterdir()):
        raise ProbHubError(f"problem directory is not empty: {problem_dir}")
    (problem_dir / "code").mkdir(parents=True, exist_ok=True)
    (problem_dir / "data/sample").mkdir(parents=True, exist_ok=True)
    (problem_dir / "data/secret").mkdir(parents=True, exist_ok=True)
    name = args.name or problem_id
    write_yaml(problem_dir / "probhub.yaml", {
        "schema_version": 1,
        "id": problem_id,
        "name": name,
        "display_name": name,
        "difficulty": None,
        "tags": [],
        "limits": {"time": 1, "memory": 256, "output": 64, "processes": 32},
        "statement": {"source": "problem.md"},
        "judge": {"type": "standard", "validator": "code/validator.cpp"},
        "solutions": {
            "accepted": ["code/std.cpp"],
            "brute": ["code/brute.cpp"],
            "wrong": ["code/wrong.cpp"],
        },
        "generators": ["code/inmaker.cpp"],
        "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
        "domjudge": {"include_pdf": True},
    })
    (problem_dir / "problem.md").write_text(
        f"# {name}\n\n## 题目描述\n\n待补充。\n\n## 输入格式\n\n待补充。\n\n## 输出格式\n\n待补充。\n",
        encoding="utf-8",
    )
    workspace.setdefault("problems", []).append({"id": problem_id, "directory": directory})
    write_yaml(root / WORKSPACE_FILE, workspace)
    return {"ok": True, "id": problem_id, "directory": str(problem_dir)}


def command_doctor(args):
    return run_doctor()


def command_lint(args):
    root, workspace = workspace_context(args)
    entries = select_entries(workspace, args.problem)
    return lint_workspace(root, workspace, entries)


def command_status(args):
    root, workspace = workspace_context(args)
    workspace_hash = compute_workspace_hash(root, workspace)
    collection_hash = compute_collection_hash(root, workspace)
    result = {}
    for entry in select_entries(workspace, args.problem):
        problem_dir, config = load_problem(root, entry)
        result[entry["id"]] = problem_status(
            problem_dir,
            config,
            root,
            workspace,
            workspace_hash=workspace_hash,
            collection_hash=collection_hash,
        )
    return {"ok": all(item["state"] == "current" for item in result.values()), "problems": result}


def command_judge(args):
    root, workspace = workspace_context(args)
    results = {}
    for entry in select_entries(workspace, args.problem):
        problem_dir, _ = load_problem(root, entry)
        results[entry["id"]] = judge_problem(root, problem_dir, use_cache=not args.no_cache)
    return {"ok": all(item["ok"] for item in results.values()), "problems": results}


def command_stress(args):
    root, workspace = workspace_context(args)
    entries = select_entries(workspace, args.problem)
    if args.replay is not None and len(entries) != 1:
        raise ProbHubError("stress --replay requires exactly one problem id")
    results = {}
    for entry in entries:
        problem_dir, config = load_problem(root, entry)
        results[entry["id"]] = stress_problem(
            root,
            problem_dir,
            config,
            rounds=args.rounds,
            master_seed=args.seed,
            replay=args.replay,
        )
    return {"ok": all(item["ok"] for item in results.values()), "problems": results}


def _single_problem_context(args):
    root, workspace = workspace_context(args)
    entries = select_entries(workspace, [args.problem_id])
    return root, workspace, entries[0]


def command_checkpoint(args):
    root, workspace, entry = _single_problem_context(args)
    _ensure_local_gitignore(root)
    checkpoint = create_problem_checkpoint(
        root,
        workspace,
        entry,
        state="draft",
    )
    return {"ok": True, "checkpoint": checkpoint}


def command_seal(args):
    root, workspace, entry = _single_problem_context(args)
    _ensure_local_gitignore(root)
    lint = lint_workspace(root, workspace, [entry])
    if not lint["ok"]:
        messages = list(lint.get("errors", []))
        for result in lint["problems"]:
            messages.extend(result["errors"])
        raise ProbHubError(
            f"cannot seal {entry['id']}: " + "; ".join(messages),
            code="seal_lint_failed",
        )

    problem_dir, config = load_problem(root, entry)
    source_hash = compute_source_hash(problem_dir, config)
    data_hash = compute_data_hash(problem_dir, config)
    judge = judge_problem(root, problem_dir, use_cache=not args.no_cache)
    if not judge["ok"]:
        raise ProbHubError(
            f"cannot seal {entry['id']}: sandbox failed: {judge.get('final')}",
            code="seal_judge_failed",
        )

    stress = None
    if config.get("stress"):
        stress = stress_problem(
            root,
            problem_dir,
            config,
            rounds=args.rounds,
            master_seed=args.seed,
        )
        if not stress["ok"]:
            raise ProbHubError(
                f"cannot seal {entry['id']}: stress failed: {stress.get('reason')}",
                code="seal_stress_failed",
            )

    _, current_config = load_problem(root, entry)
    if (
        compute_source_hash(problem_dir, current_config) != source_hash
        or compute_data_hash(problem_dir, current_config) != data_hash
    ):
        raise ProbHubError(
            f"cannot seal {entry['id']}: inputs changed during verification",
            code="inputs_changed",
        )

    evidence = {
        "lint": {
            "ok": True,
            "warnings": lint["problems"][0].get("warnings", []),
        },
        "judge": {
            "ok": judge["ok"],
            "returncode": judge["returncode"],
            "final": judge["final"],
            "cache": judge.get("cache", {}),
        },
        "stress": stress,
    }
    checkpoint = create_problem_checkpoint(
        root,
        workspace,
        entry,
        state="sealed",
        evidence=evidence,
    )
    generation = assemble_exam_generation(root)
    return {
        "ok": True,
        "checkpoint": checkpoint,
        "generation": generation,
    }


def command_assemble(args):
    root, _ = workspace_context(args)
    _ensure_local_gitignore(root)
    return assemble_exam_generation(root)


def command_generation_status(args):
    root, _ = workspace_context(args)
    return generation_status(root)


def command_typeset(args):
    root, workspace = workspace_context(args)
    with workspace_build_lock(root):
        _, workspace = load_workspace(root)
        all_loaded = [load_problem(root, entry) for entry in problem_entries(workspace)]
        _, main_pdf, _ = compile_collection(root, workspace, all_loaded)
        ids = {entry["id"] for entry in select_entries(workspace, args.problem)} if args.problem else None
        pdfs = extract_problem_pdfs(main_pdf, all_loaded, only_ids=ids)
        return {"ok": True, "main_pdf": str(main_pdf), "pdfs": pdfs}


def command_package(args):
    root, workspace = workspace_context(args)
    with workspace_build_lock(root):
        _, workspace = load_workspace(root)
        results = {}
        for entry in select_entries(workspace, args.problem):
            problem_dir, config = load_problem(root, entry)
            output, verification = package_problem(root, problem_dir, config, require_pdf=not args.allow_missing_pdf)
            results[entry["id"]] = {"path": str(output), "verification": verification}
        return {"ok": True, "packages": results}


def command_verify(args):
    result = verify_package(args.zip_path, require_pdf=args.require_pdf)
    result["path"] = str(Path(args.zip_path).resolve())
    return result


def command_build(args):
    root, workspace = workspace_context(args)
    entries = select_entries(workspace, args.problem)
    return build_workspace(
        root,
        workspace,
        entries,
        run_judge=not args.skip_judge,
        use_judge_cache=not args.no_cache,
    )


def build_parser():
    parser = argparse.ArgumentParser(prog="probhub", description="ProbHub deterministic problem build system")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit JSON")
    parser.add_argument("--workspace", help="workspace path or a path inside it")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("directory", nargs="?")
    init.add_argument("--title")
    init.add_argument("--subtitle")
    init.add_argument("--author")
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=command_init)

    new = sub.add_parser("new")
    new.add_argument("problem_id")
    new.add_argument("--name")
    new.add_argument("--directory")
    new.set_defaults(handler=command_new)

    doctor = sub.add_parser("doctor")
    doctor.set_defaults(handler=command_doctor)

    for name, handler in (("lint", command_lint), ("status", command_status), ("judge", command_judge), ("typeset", command_typeset), ("package", command_package), ("build", command_build)):
        item = sub.add_parser(name)
        item.add_argument("problem", nargs="*")
        if name == "package":
            item.add_argument("--allow-missing-pdf", action="store_true")
        if name in {"judge", "build"}:
            item.add_argument("--no-cache", action="store_true", help="ignore existing sandbox caches and refresh them")
        if name == "build":
            item.add_argument("--skip-judge", action="store_true")
        item.set_defaults(handler=handler)

    stress = sub.add_parser("stress")
    stress.add_argument("problem", nargs="+")
    stress.add_argument("--rounds", type=int, help="override stress.rounds")
    stress.add_argument("--seed", type=int, help="master seed; round seeds increase from this value")
    stress.add_argument("--replay", help="counterexample directory/input path, or 'latest'")
    stress.set_defaults(handler=command_stress)

    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("problem_id")
    checkpoint.set_defaults(handler=command_checkpoint)

    seal = sub.add_parser("seal")
    seal.add_argument("problem_id")
    seal.add_argument("--no-cache", action="store_true")
    seal.add_argument("--rounds", type=int, help="override stress.rounds")
    seal.add_argument("--seed", type=int, default=12345, help="stress master seed")
    seal.set_defaults(handler=command_seal)

    assemble = sub.add_parser("assemble")
    assemble.set_defaults(handler=command_assemble)

    generation = sub.add_parser("generation-status")
    generation.set_defaults(handler=command_generation_status)

    verify = sub.add_parser("verify-package")
    verify.add_argument("zip_path")
    verify.add_argument("--require-pdf", action="store_true")
    verify.set_defaults(handler=command_verify)
    return parser


def main(argv=None):
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        emit(result, args.json_output)
        return 0 if result.get("ok", True) else 1
    except ProbHubError as exc:
        result = {"ok": False, "error": str(exc)}
        if exc.code:
            result["code"] = exc.code
        emit(result, args.json_output)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
