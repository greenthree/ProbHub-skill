import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import __version__
from .build_lock import workspace_file_lock, workspace_generation_lock
from .builder_fingerprint import (
    GENERATION_SCHEMA_VERSION,
    builder_fingerprint_stale_fields,
    compute_builder_fingerprint,
)
from .errors import ProbHubError
from .hashing import hash_file
from .io import atomic_write_json, read_yaml, write_yaml
from .linting import compute_data_hash, compute_source_hash, compute_workspace_hash
from .typesetting import compile_collection, extract_problem_pdfs, is_temporary_typst_source
from .workspace import WORKSPACE_FILE, load_problem, load_workspace, problem_entries


CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINTS_DIR = Path(".probhub/checkpoints")
CHECKPOINT_TMP_DIR = Path(".probhub/checkpoint-tmp")
GENERATIONS_DIR = Path(".probhub/generations")
GENERATION_TMP_DIR = Path(".probhub/generation-tmp")
CHECKPOINT_BUSY_WAIT_SECONDS = 10.0
CHECKPOINT_BUSY_POLL_SECONDS = 0.25


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_safe(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _digest_json(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_json_object(path, *, code, label):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbHubError(f"invalid {label}: {path}", code=code) from exc
    if not isinstance(value, dict):
        raise ProbHubError(f"invalid {label}: {path}", code=code)
    return value


def _problem_storage_key(problem_id):
    return hashlib.sha256(str(problem_id).encode("utf-8")).hexdigest()[:24]


def _checkpoint_root(root, problem_id):
    return Path(root) / CHECKPOINTS_DIR / _problem_storage_key(problem_id)


def _problem_path(root, entry):
    root = Path(root).resolve()
    problem_dir = (root / entry.get("directory", entry["id"])).resolve()
    try:
        problem_dir.relative_to(root)
    except ValueError as exc:
        raise ProbHubError(
            f"problem directory must stay inside the workspace: {problem_dir}"
        ) from exc
    return problem_dir


def _problem_copy_ignore(source_root):
    source_root = Path(source_root).resolve()
    ignored_directories = {
        ".git",
        ".hg",
        ".mypy_cache",
        ".preview",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        "__pycache__",
        "node_modules",
        "output_validators",
    }
    ignored_root_files = {
        "domjudge-problem.ini",
        "meta.json",
        "problem.pdf",
        "problem.yaml",
    }

    def ignore(directory, names):
        directory = Path(directory).resolve()
        result = []
        for name in names:
            candidate = directory / name
            if name in ignored_directories or name == ".probhub":
                result.append(name)
            elif directory == source_root and name in ignored_root_files:
                result.append(name)
            elif candidate.is_file() and candidate.suffix.lower() in {
                ".exe",
                ".o",
                ".obj",
                ".pyc",
            }:
                result.append(name)
        return result

    return ignore


def _copy_problem_consistently(
    root,
    entry,
    destination,
    *,
    expected_source_hash=None,
    expected_data_hash=None,
):
    expected_problem_dir = _problem_path(root, entry)
    problem_dir, config = load_problem(root, entry)
    problem_dir = problem_dir.resolve()
    if problem_dir != expected_problem_dir:
        raise ProbHubError(f"unexpected problem directory: {problem_dir}")
    source_hash = compute_source_hash(problem_dir, config)
    data_hash = compute_data_hash(problem_dir, config)
    if (
        expected_source_hash is not None
        and source_hash != expected_source_hash
    ) or (
        expected_data_hash is not None
        and data_hash != expected_data_hash
    ):
        raise ProbHubError(
            f"verified inputs changed before creating checkpoint for {entry['id']}",
            code="inputs_changed",
        )
    shutil.copytree(
        problem_dir,
        destination,
        ignore=_problem_copy_ignore(problem_dir),
    )

    copied_config = read_yaml(destination / "probhub.yaml")
    copied_source_hash = compute_source_hash(destination, copied_config)
    copied_data_hash = compute_data_hash(destination, copied_config)
    try:
        _, current_config = load_problem(root, entry)
        current_source_hash = compute_source_hash(problem_dir, current_config)
        current_data_hash = compute_data_hash(problem_dir, current_config)
    except Exception as exc:
        raise ProbHubError(
            f"problem changed while creating checkpoint for {entry['id']}: {exc}",
            code="inputs_changed",
        ) from exc

    if (
        copied_source_hash != source_hash
        or copied_data_hash != data_hash
        or current_source_hash != source_hash
        or current_data_hash != data_hash
        or (
            expected_source_hash is not None
            and copied_source_hash != expected_source_hash
        )
        or (
            expected_data_hash is not None
            and copied_data_hash != expected_data_hash
        )
    ):
        raise ProbHubError(
            f"problem changed while creating checkpoint for {entry['id']}",
            code="inputs_changed",
        )
    return copied_config, source_hash, data_hash


def _checkpoint_manifest_path(root, problem_id, revision_id):
    return _checkpoint_root(root, problem_id) / revision_id / "revision.json"


def _checkpoint_display_name(config, problem_id):
    return config.get("display_name") or config.get("name") or problem_id


def _read_checkpoint(root, problem_id, revision_id):
    manifest_path = _checkpoint_manifest_path(root, problem_id, revision_id)
    if not manifest_path.is_file():
        raise ProbHubError(
            f"checkpoint manifest not found for {problem_id}: {revision_id}",
            code="checkpoint_invalid",
        )
    manifest = _read_json_object(
        manifest_path,
        code="checkpoint_invalid",
        label="checkpoint manifest",
    )
    problem_dir = manifest_path.parent / "problem"
    if (
        manifest.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or manifest.get("problem_id") != problem_id
        or manifest.get("revision_id") != revision_id
        or manifest.get("state") not in {"draft", "sealed"}
        or not problem_dir.is_dir()
    ):
        raise ProbHubError(
            f"invalid checkpoint for {problem_id}: {revision_id}",
            code="checkpoint_invalid",
        )
    expected_revision_id = _digest_json({
        "problem_id": manifest.get("problem_id"),
        "state": manifest.get("state"),
        "source_hash": manifest.get("source_hash"),
        "data_hash": manifest.get("data_hash"),
        "evidence": manifest.get("evidence"),
    })
    if expected_revision_id != revision_id:
        raise ProbHubError(
            f"checkpoint identity mismatch for {problem_id}: {revision_id}",
            code="checkpoint_invalid",
        )
    config = read_yaml(problem_dir / "probhub.yaml")
    if (
        compute_source_hash(problem_dir, config) != manifest.get("source_hash")
        or compute_data_hash(problem_dir, config) != manifest.get("data_hash")
        or config.get("id") != problem_id
        or _checkpoint_display_name(config, problem_id) != manifest.get("display_name")
    ):
        raise ProbHubError(
            f"checkpoint content hash mismatch for {problem_id}: {revision_id}",
            code="checkpoint_invalid",
        )
    return {**manifest, "path": str(manifest_path.parent), "problem_dir": str(problem_dir)}


def latest_checkpoint(root, problem_id):
    latest_path = _checkpoint_root(root, problem_id) / "latest.json"
    if not latest_path.is_file():
        return None
    pointer = _read_json_object(
        latest_path,
        code="checkpoint_invalid",
        label="checkpoint pointer",
    )
    revision_id = pointer.get("revision_id")
    if not isinstance(revision_id, str) or not revision_id:
        raise ProbHubError(
            f"invalid latest checkpoint pointer for {problem_id}",
            code="checkpoint_invalid",
        )
    return _read_checkpoint(root, problem_id, revision_id)


def checkpoint_revision(root, problem_id, revision_id):
    """Read and verify one immutable checkpoint revision by identity."""
    return _read_checkpoint(root, problem_id, revision_id)


def create_problem_checkpoint(
    root,
    workspace,
    entry,
    state="draft",
    evidence=None,
    *,
    expected_source_hash=None,
    expected_data_hash=None,
):
    root = Path(root).resolve()
    if state not in {"draft", "sealed"}:
        raise ProbHubError(f"unsupported checkpoint state: {state}")
    if state == "sealed" and (
        not isinstance(expected_source_hash, str)
        or not expected_source_hash
        or not isinstance(expected_data_hash, str)
        or not expected_data_hash
    ):
        raise ProbHubError(
            "sealed checkpoints require the source/data hashes verified by seal",
            code="seal_revision_required",
        )
    problem_id = entry["id"]
    lock_path = CHECKPOINTS_DIR / f"{_problem_storage_key(problem_id)}.lock"
    with workspace_file_lock(
        root,
        lock_path,
        busy_code="checkpoint_busy",
        busy_message=f"another checkpoint operation is running for {problem_id}",
    ):
        current = latest_checkpoint(root, problem_id)
        problem_dir, config = load_problem(root, entry)
        source_hash = compute_source_hash(problem_dir, config)
        data_hash = compute_data_hash(problem_dir, config)
        if (
            state == "draft"
            and current
            and current.get("state") == "sealed"
            and current.get("source_hash") == source_hash
            and current.get("data_hash") == data_hash
        ):
            return {**current, "reused": True}

        temporary_root = root / CHECKPOINT_TMP_DIR
        temporary_root.mkdir(parents=True, exist_ok=True)
        stage = temporary_root / uuid.uuid4().hex
        stage_problem = stage / "problem"
        try:
            copied_config, source_hash, data_hash = _copy_problem_consistently(
                root,
                entry,
                stage_problem,
                expected_source_hash=expected_source_hash,
                expected_data_hash=expected_data_hash,
            )
            evidence = _json_safe(evidence or {})
            revision_id = _digest_json({
                "problem_id": problem_id,
                "state": state,
                "source_hash": source_hash,
                "data_hash": data_hash,
                "evidence": evidence,
            })
            manifest = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "problem_id": problem_id,
                "revision_id": revision_id,
                "state": state,
                "source_hash": source_hash,
                "data_hash": data_hash,
                "display_name": _checkpoint_display_name(copied_config, problem_id),
                "created_at": _now(),
                "probhub_version": __version__,
                "evidence": evidence,
            }
            atomic_write_json(stage / "revision.json", manifest)
            final = _checkpoint_root(root, problem_id) / revision_id
            final.parent.mkdir(parents=True, exist_ok=True)
            reused = final.exists()
            if reused:
                _read_checkpoint(root, problem_id, revision_id)
                shutil.rmtree(stage)
            else:
                os.replace(stage, final)
            atomic_write_json(
                _checkpoint_root(root, problem_id) / "latest.json",
                {
                    "problem_id": problem_id,
                    "revision_id": revision_id,
                    "state": state,
                    "updated_at": _now(),
                },
            )
            return {
                **manifest,
                "path": str(final),
                "problem_dir": str(final / "problem"),
                "reused": reused,
            }
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)


def _workspace_copy_ignore(root, entries):
    root = Path(root).resolve()
    excluded_problem_roots = {
        (root / entry.get("directory", entry["id"])).resolve()
        for entry in entries
    }
    ignored_directories = {
        ".git",
        ".hg",
        ".mypy_cache",
        ".preview",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        "__pycache__",
        "node_modules",
    }

    def ignore(directory, names):
        directory = Path(directory).resolve()
        result = []
        for name in names:
            candidate = (directory / name).resolve()
            if candidate in excluded_problem_roots or name in ignored_directories:
                result.append(name)
                continue
            if is_temporary_typst_source(candidate):
                result.append(name)
                continue
            if directory == root / ".probhub" and name != "workspace.yaml":
                result.append(name)
                continue
            if name in {"main.pdf", "problems.json"}:
                result.append(name)
                continue
            if directory == root and name.lower().endswith(".zip"):
                result.append(name)
        return result

    return ignore


def _placeholder_record(entry):
    problem_id = entry["id"]
    display_name = f"{problem_id}（开发中）"
    return {
        "problem_id": problem_id,
        "revision_id": f"placeholder-{_problem_storage_key(problem_id)}",
        "state": "placeholder",
        "source_hash": None,
        "data_hash": None,
        "display_name": display_name,
    }


def _write_placeholder(problem_dir, entry):
    record = _placeholder_record(entry)
    problem_id = record["problem_id"]
    display_name = record["display_name"]
    (problem_dir / "data/sample").mkdir(parents=True, exist_ok=True)
    (problem_dir / "data/secret").mkdir(parents=True, exist_ok=True)
    write_yaml(problem_dir / "probhub.yaml", {
        "schema_version": 1,
        "id": problem_id,
        "name": display_name,
        "display_name": display_name,
        "limits": {"time": 1, "memory": 256, "output": 64, "processes": 32},
        "statement": {"source": "problem.md"},
        "judge": {"type": "standard", "validator": "code/validator.cpp"},
        "solutions": {"accepted": [], "brute": [], "wrong": []},
        "data": {"sample_dir": "data/sample", "secret_dir": "data/secret"},
    })
    (problem_dir / "problem.md").write_text(
        f"# {display_name}\n\n"
        "## 题目描述\n\n本题仍在开发中。\n\n"
        "## 输入格式\n\n将在后续版本中补充。\n\n"
        "## 输出格式\n\n将在后续版本中补充。\n",
        encoding="utf-8",
    )
    return record


def _ensure_generation_checkpoint(root, workspace, entry):
    """Return (checkpoint, placeholder_reason) for one workspace entry.

    A missing or broken problem source keeps the documented placeholder-page
    behaviour, but the reason is reported instead of being swallowed. A busy
    checkpoint lock (concurrent seal/checkpoint) is transient: retry within a
    bounded budget, then fail explicitly so an existing problem can never be
    silently replaced by a placeholder. Infrastructure errors (OSError)
    propagate to the caller.
    """
    problem_id = entry["id"]
    checkpoint = latest_checkpoint(root, problem_id)
    if checkpoint:
        return checkpoint, None
    deadline = time.monotonic() + CHECKPOINT_BUSY_WAIT_SECONDS
    while True:
        try:
            return create_problem_checkpoint(root, workspace, entry, state="draft"), None
        except ProbHubError as exc:
            if exc.code != "checkpoint_busy":
                return None, str(exc)
            checkpoint = latest_checkpoint(root, problem_id)
            if checkpoint:
                return checkpoint, None
            if time.monotonic() >= deadline:
                raise ProbHubError(
                    f"cannot assemble exam generation: problem {problem_id} has no "
                    "checkpoint and its checkpoint lock stayed busy; retry after the "
                    "concurrent seal or checkpoint operation finishes",
                    code="checkpoint_busy",
                ) from exc
            time.sleep(CHECKPOINT_BUSY_POLL_SECONDS)
        except (ValueError, yaml.YAMLError) as exc:
            return None, str(exc)


def _generation_missing(manifest):
    missing = manifest.get("missing")
    if isinstance(missing, list):
        return missing
    return [
        {"problem_id": problem.get("problem_id"), "reason": "problem has no checkpoint"}
        for problem in manifest.get("problems", [])
        if isinstance(problem, dict) and problem.get("state") == "placeholder"
    ]


def _generation_result(root, manifest, cached):
    generation_dir = Path(root) / GENERATIONS_DIR / manifest["generation_id"]
    return {
        "ok": True,
        "generation_id": manifest["generation_id"],
        "state": manifest["state"],
        "complete": manifest["complete"],
        "missing": _generation_missing(manifest),
        "all_sealed": manifest["all_sealed"],
        "cached": cached,
        "path": str(generation_dir),
        "main_pdf": str(generation_dir / "main.pdf"),
        "manifest": manifest,
    }


def _read_generation_manifest(generation_dir, *, require_current_schema=True):
    generation_dir = Path(generation_dir)
    manifest = _read_json_object(
        generation_dir / "manifest.json",
        code="generation_invalid",
        label="generation manifest",
    )
    main_pdf = generation_dir / "main.pdf"
    valid = (
        manifest.get("generation_id") == generation_dir.name
        and manifest.get("main_pdf_hash") == hash_file(main_pdf)
    )
    problems = manifest.get("problems")
    if not isinstance(problems, list) or not problems:
        valid = False
        problems = []
    for problem in problems:
        if not isinstance(problem, dict):
            valid = False
            break
        relative = problem.get("pdf")
        if not isinstance(relative, str) or Path(relative).is_absolute():
            valid = False
            break
        pdf_path = generation_dir / relative
        try:
            pdf_path.resolve().relative_to(generation_dir.resolve())
        except ValueError:
            valid = False
            break
        if problem.get("pdf_hash") != hash_file(pdf_path):
            valid = False
            break
    if not valid:
        raise ProbHubError(
            f"generation artifact hash mismatch: {generation_dir}",
            code="generation_invalid",
        )
    if require_current_schema and (
        manifest.get("schema_version") != GENERATION_SCHEMA_VERSION
        or not isinstance(manifest.get("builder_fingerprint"), dict)
    ):
        raise ProbHubError(
            f"generation identity is stale: {generation_dir}",
            code="generation_stale",
        )
    return manifest


def _assert_builder_fingerprint(expected, current):
    changed = builder_fingerprint_stale_fields(expected, current)
    if changed:
        raise ProbHubError(
            "builder identity changed during generation: " + ", ".join(changed),
            code="builder_changed",
        )


def _recheck_builder_fingerprint(root, workspace, expected):
    try:
        current = compute_builder_fingerprint(root, workspace)
    except Exception as exc:
        raise ProbHubError(
            f"builder identity changed during generation: {exc}",
            code="builder_changed",
        ) from exc
    _assert_builder_fingerprint(expected, current)
    return current


def assemble_exam_generation(
    root,
    workspace=None,
    *,
    expected_builder_fingerprint=None,
):
    root = Path(root).resolve()
    with workspace_generation_lock(root):
        _, workspace = load_workspace(root) if workspace is None else (root, workspace)
        if expected_builder_fingerprint is None:
            builder_fingerprint = compute_builder_fingerprint(root, workspace)
        else:
            builder_fingerprint = _recheck_builder_fingerprint(
                root,
                workspace,
                expected_builder_fingerprint,
            )
        entries = problem_entries(workspace)
        checkpoints = {}
        placeholder_reasons = {}
        for entry in entries:
            checkpoint, reason = _ensure_generation_checkpoint(root, workspace, entry)
            checkpoints[entry["id"]] = checkpoint
            if checkpoint is None:
                placeholder_reasons[entry["id"]] = reason or "problem has no checkpoint"
        live_workspace_hash = compute_workspace_hash(root, workspace)
        revision_records = []
        for entry in entries:
            checkpoint = checkpoints[entry["id"]]
            if checkpoint is None:
                record = _placeholder_record(entry)
            else:
                record = {
                    key: checkpoint.get(key)
                    for key in (
                        "problem_id",
                        "revision_id",
                        "state",
                        "source_hash",
                        "data_hash",
                        "display_name",
                    )
                }
            revision_records.append(record)
        generation_id = _digest_json({
            "schema_version": GENERATION_SCHEMA_VERSION,
            "workspace_hash": live_workspace_hash,
            "builder_fingerprint": builder_fingerprint,
            "revisions": [
                {
                    "problem_id": item["problem_id"],
                    "revision_id": item["revision_id"],
                }
                for item in revision_records
            ],
        })[:32]
        generation_dir = root / GENERATIONS_DIR / generation_id
        manifest_path = generation_dir / "manifest.json"
        if manifest_path.is_file() and (generation_dir / "main.pdf").is_file():
            try:
                manifest = _read_generation_manifest(generation_dir)
            except ProbHubError:
                manifest = None
            if manifest is not None:
                _recheck_builder_fingerprint(
                    root,
                    load_workspace(root)[1],
                    builder_fingerprint,
                )
                atomic_write_json(
                    root / GENERATIONS_DIR / "current.json",
                    {"generation_id": generation_id, "updated_at": _now()},
                )
                return _generation_result(root, manifest, cached=True)

        temporary_root = None
        stage = None
        try:
            temporary_root = Path(tempfile.mkdtemp(
                prefix=f".{root.name}-probhub-generation-",
                dir=root.parent,
            ))
            snapshot_root = temporary_root / "workspace"
            shutil.copytree(
                root,
                snapshot_root,
                ignore=_workspace_copy_ignore(root, entries),
            )
            _, snapshot_workspace = load_workspace(snapshot_root)
            for entry, record in zip(entries, revision_records):
                problem_dir = _problem_path(snapshot_root, entry)
                checkpoint = checkpoints[entry["id"]]
                if checkpoint is None:
                    _write_placeholder(problem_dir, entry)
                else:
                    shutil.copytree(
                        checkpoint["problem_dir"],
                        problem_dir,
                        dirs_exist_ok=True,
                    )

            snapshot_workspace_hash = compute_workspace_hash(
                snapshot_root,
                snapshot_workspace,
            )
            current_workspace_hash = compute_workspace_hash(root, load_workspace(root)[1])
            if (
                snapshot_workspace_hash != live_workspace_hash
                or current_workspace_hash != live_workspace_hash
            ):
                raise ProbHubError(
                    "workspace or Typst inputs changed while creating exam generation",
                    code="inputs_changed",
                )
            _recheck_builder_fingerprint(
                snapshot_root,
                snapshot_workspace,
                builder_fingerprint,
            )

            loaded = [load_problem(snapshot_root, entry) for entry in entries]
            _, main_pdf, _ = compile_collection(
                snapshot_root,
                snapshot_workspace,
                loaded,
            )
            pdfs = extract_problem_pdfs(main_pdf, loaded)

            stage_root = root / GENERATION_TMP_DIR
            stage_root.mkdir(parents=True, exist_ok=True)
            stage = stage_root / uuid.uuid4().hex
            (stage / "problems").mkdir(parents=True)
            shutil.copyfile(main_pdf, stage / "main.pdf")
            for problem_id, result in pdfs.items():
                shutil.copyfile(result["path"], stage / "problems" / f"{problem_id}.pdf")

            states = [item["state"] for item in revision_records]
            all_sealed = bool(states) and all(state == "sealed" for state in states)
            complete = all(state != "placeholder" for state in states)
            manifest = {
                "schema_version": GENERATION_SCHEMA_VERSION,
                "generation_id": generation_id,
                "state": "sealed-preview" if all_sealed else "draft",
                "complete": complete,
                "missing": [
                    {
                        "problem_id": record["problem_id"],
                        "reason": placeholder_reasons.get(
                            record["problem_id"], "problem has no checkpoint"
                        ),
                    }
                    for record in revision_records
                    if record["state"] == "placeholder"
                ],
                "all_sealed": all_sealed,
                "workspace_hash": snapshot_workspace_hash,
                "builder_fingerprint": builder_fingerprint,
                "main_pdf_hash": hash_file(stage / "main.pdf"),
                "created_at": _now(),
                "probhub_version": __version__,
                "problems": [
                    {
                        **record,
                        "pages": pdfs[record["problem_id"]]["pages"],
                        "pdf": f"problems/{record['problem_id']}.pdf",
                        "pdf_hash": hash_file(
                            stage / "problems" / f"{record['problem_id']}.pdf"
                        ),
                    }
                    for record in revision_records
                ],
            }
            atomic_write_json(stage / "manifest.json", manifest)
            _recheck_builder_fingerprint(
                root,
                load_workspace(root)[1],
                builder_fingerprint,
            )
            generation_dir.parent.mkdir(parents=True, exist_ok=True)
            if generation_dir.exists():
                # Reuse an existing directory only after validating it; a
                # corrupt leftover must never win over the freshly built stage.
                try:
                    manifest = _read_generation_manifest(generation_dir)
                    shutil.rmtree(stage, ignore_errors=True)
                except ProbHubError:
                    shutil.rmtree(generation_dir)
                    os.replace(stage, generation_dir)
            else:
                os.replace(stage, generation_dir)
            atomic_write_json(
                root / GENERATIONS_DIR / "current.json",
                {"generation_id": generation_id, "updated_at": _now()},
            )
            return _generation_result(root, manifest, cached=False)
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)


def generation_status(root):
    root = Path(root).resolve()
    pointer_path = root / GENERATIONS_DIR / "current.json"
    if not pointer_path.is_file():
        return {"ok": False, "state": "none"}
    try:
        pointer = _read_json_object(
            pointer_path,
            code="generation_invalid",
            label="generation pointer",
        )
    except ProbHubError:
        return {"ok": False, "state": "invalid"}
    generation_id = pointer.get("generation_id")
    generation_dir = root / GENERATIONS_DIR / str(generation_id)
    manifest_path = generation_dir / "manifest.json"
    main_pdf = generation_dir / "main.pdf"
    if not manifest_path.is_file() or not main_pdf.is_file():
        return {
            "ok": False,
            "state": "invalid",
            "generation_id": generation_id,
        }
    try:
        manifest = _read_generation_manifest(
            generation_dir,
            require_current_schema=False,
        )
    except ProbHubError:
        manifest = None
    stale = []
    builder_fingerprint_error = None
    if manifest is not None:
        if manifest.get("schema_version") != GENERATION_SCHEMA_VERSION:
            stale.append("generation_schema")
        elif not isinstance(manifest.get("builder_fingerprint"), dict):
            stale.append("builder_fingerprint")
        else:
            try:
                _, workspace = load_workspace(root)
                current_builder = compute_builder_fingerprint(root, workspace)
            except ProbHubError as exc:
                stale.append("builder_fingerprint.unavailable")
                builder_fingerprint_error = {
                    "code": exc.code or "builder_fingerprint_failed",
                    "error": str(exc),
                }
            else:
                stale.extend(builder_fingerprint_stale_fields(
                    manifest["builder_fingerprint"],
                    current_builder,
                ))
    valid = manifest is not None and not stale
    return {
        "ok": valid,
        "state": (
            manifest.get("state")
            if valid
            else ("stale" if manifest is not None and stale else "invalid")
        ),
        **({"stale_fields": stale} if manifest is not None and stale else {}),
        **(
            {"builder_fingerprint_error": builder_fingerprint_error}
            if builder_fingerprint_error is not None
            else {}
        ),
        "generation_id": generation_id,
        "path": str(generation_dir),
        "main_pdf": str(main_pdf),
        "manifest": manifest,
    }
