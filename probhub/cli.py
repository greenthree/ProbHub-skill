import argparse
import json
import re
import stat
import sys
import time
from pathlib import Path

from . import __version__
from .build_lock import workspace_build_lock, workspace_file_lock
from .builder_fingerprint import compute_builder_fingerprint
from .building import build_workspace, package_workspace, typeset_workspace
from .cli_output import render_human_result
from .doctor import run_doctor
from .errors import ProbHubError
from .generations import (
    assemble_exam_generation,
    create_problem_checkpoint,
    generation_status,
)
from .io import atomic_write_bytes, atomic_write_text, write_yaml
from .judge_qa import judge_qa_problem
from .judging import check_sample_answers, judge_problem
from .mutation import MUTATION_OPERATORS, mutation_test_problem
from .linting import (
    compute_collection_hash,
    compute_data_hash,
    compute_source_hash,
    compute_workspace_hash,
    lint_workspace,
    problem_status,
)
from .datagen import generate_problem_data, problem_config_identity
from .package_tools import verify_package
from .reporting import build_workspace_report, render_workspace_report
from .scaffold import JUDGE_TYPES, scaffold_config, scaffold_files
from .stressing import stress_problem
from .transactions import ensure_no_pending_transactions, recover_workspace_transactions
from .workspace import (
    WORKSPACE_FILE,
    find_workspace,
    load_problem,
    load_workspace,
    problem_entries,
    resolve_problem_dir,
    select_entries,
)


def configure_stdout():
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def webui_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def emit(data, json_output=False):
    if json_output:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, str):
            print(data)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))


def emit_result(data, args, renderer=None):
    """Select presentation without changing the structured result."""
    output_format = getattr(args, "output_format", None)
    if renderer is not None and not args.json_output and output_format != "json":
        print(renderer(data, args), end="")
    elif output_format == "text" and not args.json_output:
        print(render_human_result(data, getattr(args, "command", None)), end="")
    else:
        emit(data, args.json_output or output_format == "json")


def workspace_context(args, allow_empty=False):
    root = find_workspace(getattr(args, "workspace", None))
    return load_workspace(root, allow_empty=allow_empty)


def _ensure_local_gitignore(root):
    path = Path(root) / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    required = [
        "**/.probhub/build.lock",
        "**/.probhub/build-publish-*/",
        "**/.probhub/checkpoints/",
        "**/.probhub/checkpoint-tmp/",
        "**/.probhub/generation.lock",
        "**/.probhub/generations/",
        "**/.probhub/generation-tmp/",
        "**/.probhub/judge-evidence.lock",
        "**/.probhub/judge.lock",
        "**/.probhub/judge-evidence-v1.json",
        "**/.probhub/judge-evidence-v1.json.*.tmp",
        "**/.probhub/judge-evidence-v2.json",
        "**/.probhub/judge-evidence-v2.json.*.tmp",
        "**/.probhub/judge-qa-evidence.lock",
        "**/.probhub/judge-qa-evidence-v1.json",
        "**/.probhub/judge-qa-evidence-v1.json.*.tmp",
        "**/.probhub/judge-qa-tmp/",
        "**/.probhub/mutation.lock",
        "**/.probhub/mutation-evidence-v1.json",
        "**/.probhub/mutation-evidence-v1.json.*.tmp",
        "**/.probhub/mutation-evidence-v2.json",
        "**/.probhub/mutation-evidence-v2.json.*.tmp",
        "**/.probhub/sandbox-cache-v1.json",
        "**/.probhub/sandbox-cache-v1.json.tmp",
        "**/.probhub/stress/",
        "**/.probhub/gen-manifest.json",
        "**/.probhub/compile/",
        "**/.probhub-fixate-*/",
        "**/.probhub-gen-publish-*/",
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
    # Atomic: .gitignore is a user source file; a crash mid-write must not
    # truncate hand-maintained entries.
    atomic_write_text(path, content)


def _path_is_link_like(path):
    path = Path(path)
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(reparse and attributes & reparse)
    except OSError:
        return False


def _validate_init_target(root, target, label):
    root = Path(root).resolve()
    target = Path(target).absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ProbHubError(
            f"{label} must stay inside the workspace: {target}",
            code="init_path_escape",
        ) from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if _path_is_link_like(cursor):
            raise ProbHubError(
                f"{label} must not traverse a symlink or junction: {cursor}",
                code="init_path_link",
            )
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ProbHubError(
            f"{label} resolves outside the workspace: {target}",
            code="init_path_escape",
        ) from exc
    return target


def _validate_init_subtitle(subtitle):
    subtitle = str(subtitle or "正式赛").strip()
    windows_stem = subtitle.split(".", 1)[0].upper()
    if (
        not subtitle
        or subtitle in {".", ".."}
        or "/" in subtitle
        or "\\" in subtitle
        or "\x00" in subtitle
        or any(ord(character) < 32 or ord(character) == 127 for character in subtitle)
        or re.search(r'[<>:"|?*]', subtitle)
        or subtitle.endswith((" ", "."))
        or windows_stem in {"CON", "PRN", "AUX", "NUL"}
        or re.fullmatch(r"(?:COM|LPT)[1-9]", windows_stem)
    ):
        raise ProbHubError(
            "subtitle must be a cross-platform safe single directory name",
            code="invalid_subtitle",
        )
    return subtitle


def _remove_created_init_files(root, paths):
    root = Path(root).resolve()
    parents = set()
    for value in reversed(list(paths)):
        path = Path(value)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != root:
            parents.add(parent)
            parent = parent.parent
    for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass


def _restore_init_file(path, previous):
    path = Path(path)
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_bytes(path, previous)


def _init_typst_templates(root, subtitle):
    root = Path(root).resolve()

    package_root = Path(__file__).resolve().parents[1]
    references = package_root / "references"
    typst_root = root / "typst-statement"
    typst_dir = typst_root / subtitle
    sources = {
        typst_root / "lib.typ": references / "lib.typ",
        typst_root / "school-badge.png": package_root / "school-badge.png",
        typst_dir / "main.typ": references / "main.typ",
        typst_dir / "problems.typ": references / "problems.typ",
    }
    payloads = {}
    for target, source in sources.items():
        _validate_init_target(root, target, "Typst template target")
        if not source.is_file():
            raise ProbHubError(
                f"installed ProbHub template is missing: {source}",
                code="init_template_missing",
            )
        payloads[target] = source.read_bytes()

    to_create = []
    preserved = []
    for target, payload in payloads.items():
        if target.exists():
            if not target.is_file():
                raise ProbHubError(
                    f"Typst template target is not a file: {target}",
                    code="init_template_conflict",
                )
            preserved.append(str(target))
            continue
        to_create.append((target, payload))

    created = []
    try:
        for target, payload in to_create:
            atomic_write_bytes(target, payload)
            created.append(str(target))
    except BaseException as exc:
        _remove_created_init_files(root, created)
        if isinstance(exc, (OSError, UnicodeError)):
            raise ProbHubError(
                f"failed to install Typst templates: {exc}",
                code="init_template_write_failed",
            ) from exc
        raise
    return {
        "directory": str(typst_dir),
        "created": created,
        "preserved": preserved,
    }


def command_init(args):
    root = Path(args.directory or Path.cwd()).resolve()
    subtitle = _validate_init_subtitle(args.subtitle)
    path = root / WORKSPACE_FILE
    gitignore = root / ".gitignore"
    _validate_init_target(root, root / ".probhub/build.lock", "workspace init lock")
    with workspace_file_lock(
        root,
        ".probhub/build.lock",
        busy_code="build_busy",
        busy_message="another ProbHub writer is already running",
    ):
        _validate_init_target(root, path, "workspace configuration")
        _validate_init_target(root, gitignore, "workspace gitignore")
        if path.exists() and not args.force:
            raise ProbHubError(f"workspace already exists: {path}")
        if path.exists() and not path.is_file():
            raise ProbHubError(
                f"workspace configuration is not a regular file: {path}",
                code="init_path_conflict",
            )
        if gitignore.exists() and not gitignore.is_file():
            raise ProbHubError(
                f"workspace gitignore is not a regular file: {gitignore}",
                code="init_path_conflict",
            )

        workspace_before = path.read_bytes() if path.is_file() else None
        gitignore_before = gitignore.read_bytes() if gitignore.is_file() else None
        templates = _init_typst_templates(root, subtitle)
        data = {
            "schema_version": 1,
            "contest": {"title": args.title or root.name, "subtitle": subtitle, "author": args.author or ""},
            "typst": {
                "directory": f"typst-statement/{subtitle}",
                "creation_timestamp": 0,
                "cover": {
                    "logo": "school-badge.png",
                    "logo_width": "9cm",
                    "logo_space_above": "0em",
                    "logo_space_below": "0em",
                },
            },
            "problems": [],
            "lint": {"forbidden_patterns": ["TODO", "FIXME", "114514", "待补充"]},
        }
        try:
            write_yaml(path, data)
            _ensure_local_gitignore(root)
        except BaseException as exc:
            rollback_errors = []
            try:
                _remove_created_init_files(root, templates["created"])
            except OSError as rollback_exc:
                rollback_errors.append(f"templates: {rollback_exc}")
            for label, target, previous in (
                ("workspace", path, workspace_before),
                ("gitignore", gitignore, gitignore_before),
            ):
                try:
                    _restore_init_file(target, previous)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{label}: {rollback_exc}")
            if rollback_errors:
                raise ProbHubError(
                    "init failed and rollback was incomplete: " + "; ".join(rollback_errors),
                    code="init_rollback_failed",
                ) from exc
            if isinstance(exc, (OSError, UnicodeError)):
                raise ProbHubError(
                    f"failed to initialize workspace: {exc}",
                    code="init_write_failed",
                ) from exc
            raise
        return {"ok": True, "workspace": str(path), "typst": templates}


_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def command_new(args):
    root, _ = workspace_context(args, allow_empty=True)
    problem_id = args.problem_id.strip()
    if not _SAFE_PATH_COMPONENT.match(problem_id):
        raise ProbHubError(
            f"invalid problem id: {problem_id!r} (use letters, digits, '_', '.', '-')"
        )
    judge_type = getattr(args, "judge", None) or "standard"
    name = args.name or problem_id
    directory = args.directory or problem_id
    root_resolved = Path(root).resolve()
    problem_dir = (root_resolved / directory).resolve()
    if problem_dir == root_resolved:
        raise ProbHubError("problem directory must not be the workspace root")
    try:
        relative_dir = problem_dir.relative_to(root_resolved)
    except ValueError as exc:
        raise ProbHubError(
            f"problem directory must stay inside the workspace: {directory}"
        ) from exc
    for part in relative_dir.parts:
        if not _SAFE_PATH_COMPONENT.match(part):
            raise ProbHubError(
                f"invalid problem directory component: {part!r} "
                "(use letters, digits, '_', '.', '-')"
            )
    files = scaffold_files(name, judge_type)
    config = scaffold_config(problem_id, name, judge_type)
    with workspace_build_lock(root):
        _, workspace = load_workspace(root, allow_empty=True)
        recover_workspace_transactions(root, workspace)
        if any(entry["id"] == problem_id for entry in problem_entries(workspace)):
            raise ProbHubError(f"problem id already exists: {problem_id}")
        if problem_dir.exists() and any(problem_dir.iterdir()):
            raise ProbHubError(f"problem directory is not empty: {problem_dir}")
        for relative, content in files.items():
            path = problem_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
        write_yaml(problem_dir / "probhub.yaml", config)
        workspace.setdefault("problems", []).append(
            {"id": problem_id, "directory": relative_dir.as_posix()}
        )
        write_yaml(root / WORKSPACE_FILE, workspace)
    return {
        "ok": True,
        "id": problem_id,
        "directory": str(problem_dir),
        "judge": judge_type,
        "files": sorted([*files, "probhub.yaml"]),
    }


def command_gen(args):
    root, workspace, entry = _single_problem_context(args)
    if not args.apply:
        # Plan mode is strictly read-only: no gitignore, no lock, no writes.
        ensure_no_pending_transactions(root, workspace)
        problem_dir, config = load_problem(root, entry)
        return generate_problem_data(problem_dir, config, apply_changes=False, only=args.case)
    with workspace_build_lock(root):
        _, live_workspace = load_workspace(root)
        recover_workspace_transactions(root, live_workspace)
        live_entry = select_entries(live_workspace, [entry["id"]])[0]
        problem_dir, config = load_problem(root, live_entry)
        config_identity = problem_config_identity(problem_dir)
        source_hash = compute_source_hash(problem_dir, config)
        _ensure_local_gitignore(root)
        return generate_problem_data(
            problem_dir,
            config,
            apply_changes=True,
            only=args.case,
            expected_config_identity=config_identity,
            expected_source_hash=source_hash,
        )


def command_doctor(args):
    return run_doctor()


def command_ui(args):
    root = find_workspace(getattr(args, "workspace", None))
    load_workspace(root, allow_empty=True)
    from .webui_runtime import check_webui, run_webui

    if args.check:
        return check_webui(root)
    return run_webui(
        root,
        port=args.port,
        open_browser=not args.no_browser,
    )


def command_lint(args):
    root, workspace = workspace_context(args)
    entries = select_entries(workspace, args.problem)
    return lint_workspace(root, workspace, entries)


def command_status(args):
    root, workspace = workspace_context(args)
    ensure_no_pending_transactions(root, workspace)
    workspace_hash = compute_workspace_hash(root, workspace)
    collection_hash = compute_collection_hash(root, workspace)
    builder_fingerprint = None
    builder_fingerprint_error = None
    try:
        builder_fingerprint = compute_builder_fingerprint(root, workspace)
    except ProbHubError as exc:
        builder_fingerprint_error = {
            "code": exc.code or "builder_fingerprint_failed",
            "error": str(exc),
        }
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
            builder_fingerprint=builder_fingerprint,
            builder_fingerprint_error=builder_fingerprint_error,
        )
    return {"ok": all(item["state"] == "current" for item in result.values()), "problems": result}


def command_report(args):
    root, workspace = workspace_context(args)
    ensure_no_pending_transactions(root, workspace)
    entries = select_entries(workspace, args.problem)
    return build_workspace_report(root, workspace, entries)


def render_report_result(result, args):
    return render_workspace_report(result, args.format)


def command_judge(args):
    root, workspace = workspace_context(args)
    ensure_no_pending_transactions(root, workspace)
    _ensure_local_gitignore(root)
    results = {}
    for entry in select_entries(workspace, args.problem):
        problem_dir, _ = load_problem(root, entry)
        results[entry["id"]] = judge_problem(root, problem_dir, use_cache=not args.no_cache)
    return {"ok": all(item["ok"] for item in results.values()), "problems": results}


def command_judge_qa(args):
    root, workspace = workspace_context(args)
    ensure_no_pending_transactions(root, workspace)
    _ensure_local_gitignore(root)
    results = {}
    for entry in select_entries(workspace, args.problem):
        problem_dir, _ = load_problem(root, entry)
        results[entry["id"]] = judge_qa_problem(
            root,
            problem_dir,
            use_cache=not args.no_cache,
        )
    return {
        "ok": all(item["ok"] for item in results.values()),
        "problems": results,
    }


def command_sample_check(args):
    root, workspace = workspace_context(args)
    ensure_no_pending_transactions(root, workspace)
    results = {}
    for entry in select_entries(workspace, args.problem):
        problem_dir, _ = load_problem(root, entry)
        results[entry["id"]] = check_sample_answers(
            root,
            problem_dir,
            use_cache=not args.no_cache,
        )
    return {
        "ok": all(item["ok"] for item in results.values()),
        "problems": results,
    }


def command_stress(args):
    root, workspace = workspace_context(args)
    if args.fixate is None:
        ensure_no_pending_transactions(root, workspace)
    else:
        with workspace_build_lock(root):
            _, workspace = load_workspace(root)
            recover_workspace_transactions(root, workspace)
    entries = select_entries(workspace, args.problem)
    if args.replay is not None and len(entries) != 1:
        raise ProbHubError("stress --replay requires exactly one problem id")
    if args.against is not None and len(entries) != 1:
        raise ProbHubError("stress --against requires exactly one problem id")
    results = {}
    for entry in entries:
        if args.fixate is None:
            problem_dir, config = load_problem(root, entry)
        else:
            problem_dir = resolve_problem_dir(root, entry)
            config = {"id": entry["id"]}
        results[entry["id"]] = stress_problem(
            root,
            problem_dir,
            config,
            rounds=args.rounds,
            master_seed=args.seed,
            replay=args.replay,
            against=args.against,
            fixate=args.fixate,
            fixate_group=args.group,
        )
    return {"ok": all(item["ok"] for item in results.values()), "problems": results}


def command_mutate(args):
    root, workspace = workspace_context(args)
    ensure_no_pending_transactions(root, workspace)
    _ensure_local_gitignore(root)
    if args.timeout is not None and args.timeout <= 0:
        raise ProbHubError("mutation --timeout must be positive", code="mutation_timeout_invalid")
    entries = select_entries(workspace, args.problem)
    results = {}
    for entry in entries:
        problem_dir, _ = load_problem(root, entry)
        results[entry["id"]] = mutation_test_problem(
            root,
            problem_dir,
            use_cache=not args.no_cache,
            operators=args.operator,
            max_mutants=args.max_mutants,
            jobs=args.jobs,
            deadline=(time.monotonic() + args.timeout) if args.timeout else None,
        )
    return {"ok": all(item["ok"] for item in results.values()), "problems": results}


def _single_problem_context(args):
    root, workspace = workspace_context(args)
    entries = select_entries(workspace, [args.problem_id])
    return root, workspace, entries[0]


def command_checkpoint(args):
    root, workspace, entry = _single_problem_context(args)
    ensure_no_pending_transactions(root, workspace)
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
    ensure_no_pending_transactions(root, workspace)
    _ensure_local_gitignore(root)
    builder_fingerprint = compute_builder_fingerprint(root, workspace)
    lint = lint_workspace(root, workspace, [entry])
    if not lint["ok"]:
        messages = list(lint.get("errors", []))
        for result in lint["problems"]:
            messages.extend(result["errors"])
        raise ProbHubError(
            f"cannot seal {entry['id']}: " + "; ".join(messages),
            code="seal_lint_failed",
        )
    qa_configured = bool(
        (lint["problems"][0].get("judge_qa") or {}).get("configured")
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
    judge_qa = judge_qa_problem(
        root,
        problem_dir,
        use_cache=not args.no_cache,
    )
    qa_status = judge_qa.get("status")
    qa_passed = (
        judge_qa.get("ok") is True
        and (
            qa_status == "passed"
            if qa_configured
            else qa_status == "not-configured"
        )
    )
    if not qa_passed:
        raise ProbHubError(
            f"cannot seal {entry['id']}: Judge QA failed: "
            f"{qa_status or 'unknown'} ({judge_qa.get('code') or 'no-code'})",
            code="seal_judge_qa_failed",
        )
    # Judge and Judge QA publish local evidence. Refresh the read-only lint
    # view so the checkpoint records the post-verification states.
    lint = lint_workspace(root, workspace, [entry])
    evidence_state = (
        ((lint["problems"][0].get("judge_qa") or {}).get("evidence") or {}).get(
            "state"
        )
    )
    expected_evidence_state = "current" if qa_configured else "not-configured"
    if evidence_state != expected_evidence_state:
        raise ProbHubError(
            f"cannot seal {entry['id']}: Judge QA evidence is "
            f"{evidence_state or 'missing'}",
            code="seal_judge_qa_failed",
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
            "summaries": judge.get("summaries", []),
            "calibration": judge.get("calibration"),
        },
        "judge_qa": (
            {
                "status": "not-configured",
                "applicable": False,
                "judge_type": judge_qa.get("judge_type"),
            }
            if qa_status == "not-configured"
            else {
                "status": "passed",
                "applicable": True,
                "code": judge_qa.get("code"),
                **dict(judge_qa.get("evidence") or {}),
            }
        ),
        "stress": stress,
    }
    checkpoint = create_problem_checkpoint(
        root,
        workspace,
        entry,
        state="sealed",
        evidence=evidence,
        expected_source_hash=source_hash,
        expected_data_hash=data_hash,
    )
    generation = assemble_exam_generation(
        root,
        expected_builder_fingerprint=builder_fingerprint,
    )
    return {
        "ok": True,
        "checkpoint": checkpoint,
        "generation": generation,
    }


def command_assemble(args):
    root, workspace = workspace_context(args)
    ensure_no_pending_transactions(root, workspace)
    _ensure_local_gitignore(root)
    return assemble_exam_generation(root)


def command_generation_status(args):
    root, workspace = workspace_context(args)
    ensure_no_pending_transactions(root, workspace)
    return generation_status(root)


def command_typeset(args):
    root, workspace = workspace_context(args)
    entries = select_entries(workspace, args.problem)
    return typeset_workspace(root, workspace, entries)


def command_package(args):
    root, workspace = workspace_context(args)
    entries = select_entries(workspace, args.problem)
    return package_workspace(
        root,
        workspace,
        entries,
        require_pdf=not args.allow_missing_pdf,
    )


def command_verify(args):
    verification_args = {}
    if args.problem:
        root, workspace = workspace_context(args)
        entry = select_entries(workspace, [args.problem])[0]
        problem_dir, config = load_problem(root, entry)
        verification_args.update(
            expected_config=config,
            source_problem_dir=problem_dir,
            run_validator=True,
        )
    result = verify_package(
        args.zip_path,
        require_pdf=args.require_pdf,
        **verification_args,
    )
    result["path"] = str(Path(args.zip_path).resolve())
    return result


def command_build(args):
    root, workspace = workspace_context(args)
    _ensure_local_gitignore(root)
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
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        dest="output_format",
        help="output format; text is a bounded human-readable summary",
    )
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
    new.add_argument("--judge", choices=list(JUDGE_TYPES), default="standard")
    new.set_defaults(handler=command_new)

    gen = sub.add_parser("gen")
    gen.add_argument("problem_id")
    gen.add_argument("--apply", action="store_true")
    gen.add_argument("--case", action="append")
    gen.set_defaults(handler=command_gen)

    doctor = sub.add_parser("doctor")
    doctor.set_defaults(handler=command_doctor)

    ui = sub.add_parser("ui")
    ui.add_argument("--port", type=webui_port, default=33933)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--check", action="store_true", help="load the packaged WebUI and exit")
    ui.set_defaults(handler=command_ui)

    report = sub.add_parser("report")
    report.add_argument("problem", nargs="*")
    report.add_argument("--format", choices=("text", "markdown"), default="text")
    report.set_defaults(handler=command_report, renderer=render_report_result)

    for name, handler in (("lint", command_lint), ("status", command_status), ("judge", command_judge), ("judge-qa", command_judge_qa), ("sample-check", command_sample_check), ("typeset", command_typeset), ("package", command_package), ("build", command_build)):
        item = sub.add_parser(name)
        item.add_argument("problem", nargs="*")
        if name == "package":
            item.add_argument("--allow-missing-pdf", action="store_true")
        if name in {"judge", "judge-qa", "sample-check", "build"}:
            item.add_argument("--no-cache", action="store_true", help="ignore existing sandbox caches and refresh them")
        if name == "build":
            item.add_argument("--skip-judge", action="store_true")
        item.set_defaults(handler=handler)

    stress = sub.add_parser("stress")
    stress.add_argument("problem", nargs="+")
    stress.add_argument("--rounds", type=int, help="override stress.rounds")
    stress.add_argument("--seed", type=int, help="master seed; round seeds increase from this value")
    stress.add_argument("--replay", help="counterexample directory/input path, or 'latest'")
    stress.add_argument("--against", help="hunt a killer for this solution instead of comparing with brute")
    stress.add_argument("--fixate", help="on a killer hit, persist it as this secret case with recipe and data group")
    stress.add_argument("--group", help="data group name for --fixate (defaults to the case name)")
    stress.set_defaults(handler=command_stress)

    mutate = sub.add_parser("mutation", aliases=["mutate"])
    mutate.add_argument("problem", nargs="+")
    mutate.add_argument(
        "--operator",
        action="append",
        choices=list(MUTATION_OPERATORS),
        help="restrict mutation operators; repeat for more than one",
    )
    mutate.add_argument("--max-mutants", type=int, default=256)
    mutate.add_argument(
        "--jobs",
        type=int,
        choices=(1, 2),
        default=1,
        help="bounded mutation workers; parallel execution is explicit opt-in",
    )
    mutate.add_argument("--timeout", type=float, help="overall seconds per problem")
    mutate.add_argument("--no-cache", action="store_true", help="ignore temporary Judge caches")
    mutate.set_defaults(handler=command_mutate)

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
    verify.add_argument(
        "--problem",
        help="deep-verify against this workspace problem and run its input Validator",
    )
    verify.set_defaults(handler=command_verify)
    return parser


def main(argv=None):
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        renderer = getattr(args, "renderer", None)
        emit_result(result, args, renderer)
        return 0 if result.get("ok", True) else 1
    except ProbHubError as exc:
        result = {"ok": False, "error": str(exc)}
        if exc.code:
            result["code"] = exc.code
        result.update({
            key: value
            for key, value in exc.details.items()
            if key not in {"ok", "error", "code"}
        })
        emit_result(result, args)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        emit_result(
            {"ok": False, "code": "internal_error", "error": str(exc)},
            args,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
