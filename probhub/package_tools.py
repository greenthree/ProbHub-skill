import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

from .errors import ProbHubError
from .io import write_yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TESTLIB_PATH = PACKAGE_ROOT / "references" / "testlib.h"

FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ROOT_FILES = ("domjudge-problem.ini", "problem.yaml", "problem.pdf")
ROOT_DIRS = ("data", "output_validators")


def _judge_type(config):
    value = str((config.get("judge") or {}).get("type", "standard")).strip().lower()
    return "custom" if value == "checker" else value


def prepare_output_validator(problem_dir, config):
    problem_dir = Path(problem_dir)
    validate_dir = problem_dir / "output_validators" / "validate"
    judge = config.get("judge") or {}
    judge_type = _judge_type(config)
    source_key = "checker" if judge_type == "custom" else "interactor" if judge_type == "interactive" else None

    if source_key is None:
        if validate_dir.exists():
            shutil.rmtree(validate_dir)
        output_root = validate_dir.parent
        if output_root.is_dir() and not any(output_root.iterdir()):
            output_root.rmdir()
        return None

    source_value = judge.get(source_key)
    if not source_value:
        raise ProbHubError(f"judge.{source_key} is required for {judge_type} judging")
    source = problem_dir / source_value
    if not source.is_file():
        raise ProbHubError(f"{source_key} not found: {source_value}")
    if not TESTLIB_PATH.is_file():
        raise ProbHubError(f"testlib.h not found: {TESTLIB_PATH}")

    validate_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, validate_dir / "validate.cpp")
    shutil.copyfile(TESTLIB_PATH, validate_dir / "testlib.h")
    return validate_dir


def generate_domjudge_config(problem_dir, config):
    problem_dir = Path(problem_dir)
    limits = config.get("limits") or {}
    time_limit = limits.get("time", 1)
    memory_limit = limits.get("memory", 256)
    (problem_dir / "domjudge-problem.ini").write_text(
        f"timelimit='{time_limit}'\n", encoding="utf-8"
    )
    domjudge = {"name": config.get("name") or config.get("display_name"), "limits": {"memory": memory_limit}}
    judge_type = _judge_type(config)
    if judge_type == "interactive":
        domjudge["validation"] = "custom interactive"
    elif judge_type == "custom":
        domjudge["validation"] = "custom"
    prepare_output_validator(problem_dir, config)
    write_yaml(problem_dir / "problem.yaml", domjudge)


def validate_output_validator_source(problem_dir, config):
    validate_dir = prepare_output_validator(problem_dir, config)
    if validate_dir is None:
        return None
    source = validate_dir / "validate.cpp"
    with tempfile.TemporaryDirectory(prefix="probhub-validator-build-") as temp:
        output = Path(temp) / ("validate.exe" if os.name == "nt" else "validate")
        command = ["g++", str(source), "-o", str(output), "-O2", "-std=c++17", "-I", str(validate_dir)]
        if os.name == "nt":
            command.insert(4, "-static")
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise ProbHubError(f"failed to run g++ for output validator: {exc}") from exc
        if result.returncode != 0:
            raise ProbHubError(f"output validator failed to compile: {result.stderr.strip()}")
    return validate_dir


def collect_package_files(problem_dir):
    problem_dir = Path(problem_dir)
    files = []
    for name in ROOT_FILES:
        path = problem_dir / name
        if path.is_file():
            files.append((path, name))
    for dirname in ROOT_DIRS:
        root = problem_dir / dirname
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    files.append((path, path.relative_to(problem_dir).as_posix()))
    return sorted(files, key=lambda item: item[1])


def build_package(problem_dir, output_path):
    problem_dir = Path(problem_dir)
    output_path = Path(output_path)
    files = collect_package_files(problem_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()
    try:
        with ZipFile(temp_path, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for source, arcname in files:
                info = ZipInfo(arcname, FIXED_TIMESTAMP)
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, source.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return files


def _safe_path(name):
    path = PurePosixPath(name)
    return bool(name) and "\\" not in name and not path.is_absolute() and ".." not in path.parts and not re.match(r"^[A-Za-z]:", name)


def verify_package(zip_path, require_pdf=False):
    errors, warnings = [], []
    stats = {"sample_cases": 0, "secret_cases": 0, "files": 0}
    try:
        with ZipFile(zip_path) as archive:
            names_list = [item.filename for item in archive.infolist() if not item.is_dir()]
            names = set(names_list)
            stats["files"] = len(names_list)
            duplicates = sorted(name for name in names if names_list.count(name) > 1)
            if duplicates:
                errors.append("duplicate entries: " + ", ".join(duplicates))
            unsafe = sorted(name for name in names if not _safe_path(name))
            if unsafe:
                errors.append("unsafe paths: " + ", ".join(unsafe))
            for required in ("problem.yaml", "domjudge-problem.ini"):
                if required not in names:
                    errors.append(f"missing root file: {required}")
            if require_pdf and "problem.pdf" not in names:
                errors.append("missing root file: problem.pdf")
            if "problem.pdf" in names and not archive.read("problem.pdf").startswith(b"%PDF-"):
                errors.append("problem.pdf is not a valid PDF file")
            inputs = {name[:-3] for name in names if name.startswith("data/") and name.endswith(".in")}
            answers = {name[:-4] for name in names if name.startswith("data/") and name.endswith(".ans")}
            if inputs - answers:
                errors.append("inputs without answers: " + ", ".join(sorted(inputs - answers)))
            if answers - inputs:
                errors.append("answers without inputs: " + ", ".join(sorted(answers - inputs)))
            samples = [name for name in names if name.startswith("data/sample/") and name.endswith(".in")]
            secrets = [name for name in names if name.startswith("data/secret/") and name.endswith(".in")]
            stats.update(sample_cases=len(samples), secret_cases=len(secrets))
            if not samples:
                errors.append("no sample input cases")
            if not secrets:
                errors.append("no secret input cases")
            if "problem.yaml" in names:
                text = archive.read("problem.yaml").decode("utf-8", errors="replace")
                if not re.search(r"(?m)^name\s*:", text):
                    errors.append("problem.yaml has no root name field")
                if not re.search(r"(?m)^\s*memory\s*:\s*\d+", text):
                    warnings.append("problem.yaml has no numeric memory limit")
                validation = None
                match = re.search(r"(?m)^validation\s*:\s*['\"]?([^'\"\r\n]+)", text)
                if match:
                    validation = match.group(1).strip()
                if validation in {"custom", "custom interactive"}:
                    for required in (
                        "output_validators/validate/validate.cpp",
                        "output_validators/validate/testlib.h",
                    ):
                        if required not in names:
                            errors.append(f"missing custom validator file: {required}")
    except (OSError, BadZipFile) as exc:
        errors.append(f"cannot read ZIP: {exc}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}
