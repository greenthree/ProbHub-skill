import hashlib
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import yaml
from yaml.constructor import ConstructorError

from .errors import ProbHubError
from .io import write_yaml
from .problem_paths import ProblemPathError, resolve_problem_regular_file
from .process_control import DEFAULT_PROCESS_LIMIT, ProcessCancelled, run_managed_to_files

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TESTLIB_PATH = PACKAGE_ROOT / "references" / "testlib.h"

FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ROOT_FILES = ("domjudge-problem.ini", "problem.yaml", "problem.pdf")
ROOT_DIRS = ("data", "output_validators")

MAX_PACKAGE_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_PACKAGE_ENTRY_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 10000
PACKAGE_READ_CHUNK = 1024 * 1024

ALLOWED_COMPRESSION = {ZIP_STORED, ZIP_DEFLATED}
BANNED_PATH_PARTS = {
    ".git",
    ".hg",
    ".probhub",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    "__pycache__",
    "node_modules",
}
EXECUTABLE_SUFFIXES = {
    ".a",
    ".bat",
    ".class",
    ".cmd",
    ".com",
    ".dll",
    ".dylib",
    ".exe",
    ".jar",
    ".lib",
    ".o",
    ".obj",
    ".ps1",
    ".pyc",
    ".pyd",
    ".pyo",
    ".so",
}
TEXT_DATA_SUFFIXES = {".in", ".ans"}
DOMJUDGE_INI_KEYS = {"timelimit"}
ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
ZIP_CENTRAL_HEADER_SIGNATURE = b"PK\x01\x02"
ZIP_CENTRAL_HEADER_SIZE = 46
ZIP_EOCD_SIZE = 22
ZIP_MAX_COMMENT_BYTES = 65535
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _judge_type(config):
    value = str((config.get("judge") or {}).get("type", "standard")).strip().lower()
    return "custom" if value == "checker" else value


def _normalise_package_text(payload):
    """Publish DOMjudge testcase text with one cross-platform LF convention."""
    return payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _write_package_member(archive, info, source, arcname):
    normalise = (
        Path(arcname).suffix.lower() in TEXT_DATA_SUFFIXES
        and arcname.startswith("data/")
    )
    pending_cr = False
    with Path(source).open("rb") as input_stream, archive.open(info, "w") as output_stream:
        while True:
            chunk = input_stream.read(PACKAGE_READ_CHUNK)
            if not chunk:
                break
            if not normalise:
                output_stream.write(chunk)
                continue
            if pending_cr:
                chunk = b"\r" + chunk
                pending_cr = False
            if chunk.endswith(b"\r"):
                chunk = chunk[:-1]
                pending_cr = True
            output_stream.write(_normalise_package_text(chunk))
        if normalise and pending_cr:
            output_stream.write(b"\n")


def _add_diagnostic(diagnostics, errors, warnings, code, message, *, severity="error", **extra):
    item = {"code": code, "severity": severity, "message": message, **extra}
    diagnostics.append(item)
    if severity == "warning":
        warnings.append(message)
    else:
        errors.append(message)


def _safe_path(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        return False
    if any(ord(character) < 32 for character in name):
        return False
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False
    stripped = name[:-1] if name.endswith("/") else name
    if not stripped:
        return False
    parts = stripped.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    for part in parts:
        if (
            any(character in '<>"|?*:' for character in part)
            or part.endswith((" ", "."))
            or len(part) > 255
        ):
            return False
        if part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES:
            return False
    path = PurePosixPath(stripped)
    return not path.is_absolute() and ".." not in path.parts


def _canonical_archive_name(name):
    return unicodedata.normalize("NFC", name.rstrip("/")).casefold()


def _allowed_package_path(name, is_dir=False):
    stripped = name.rstrip("/")
    parts = PurePosixPath(stripped).parts
    if not parts:
        return False
    if len(parts) == 1:
        return is_dir and parts[0] in ROOT_DIRS or (not is_dir and parts[0] in ROOT_FILES)
    if parts[0] == "data":
        if is_dir:
            return len(parts) <= 2 and (len(parts) == 1 or parts[1] in {"sample", "secret"})
        return (
            len(parts) == 3
            and parts[1] in {"sample", "secret"}
            and Path(parts[2]).suffix in TEXT_DATA_SUFFIXES
        )
    if parts[0] == "output_validators":
        if len(parts) < 2 or parts[1] != "validate":
            return False
        return True
    return False


def _parse_decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _parse_domjudge_ini(text, diagnostics, errors, warnings):
    values = {}
    invalid_keys = set()
    assignment = re.compile(
        r"^([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([^\s]+))$"
    )
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        match = assignment.fullmatch(stripped)
        if match is None:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_invalid_domjudge_ini",
                f"domjudge-problem.ini has an invalid assignment on line {line_number}",
                entry="domjudge-problem.ini",
                line=line_number,
            )
            continue
        key = match.group(1)
        value = next(group for group in match.groups()[1:] if group is not None)
        if key not in DOMJUDGE_INI_KEYS:
            invalid_keys.add(key)
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_unknown_domjudge_ini_key",
                f"domjudge-problem.ini contains an unsupported key: {key}",
                entry="domjudge-problem.ini",
                key=key,
                line=line_number,
            )
        if key in values:
            invalid_keys.add(key)
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_duplicate_domjudge_ini_key",
                f"domjudge-problem.ini contains a duplicate key: {key}",
                entry="domjudge-problem.ini",
                key=key,
                line=line_number,
            )
            continue
        values[key] = value
    return values, invalid_keys


def _expected_validation(config):
    judge_type = _judge_type(config)
    if judge_type == "custom":
        return "custom"
    if judge_type == "interactive":
        return "custom interactive"
    return None


def _problem_regular_file(problem_dir, relative, label):
    try:
        return resolve_problem_regular_file(problem_dir, relative)
    except ProblemPathError as exc:
        if exc.reason == "invalid":
            message = f"{label} is required"
        elif exc.reason == "outside":
            message = f"{label} must stay inside the problem directory: {relative}"
        else:
            message = f"{label} must be a regular file: {relative}"
        raise ProbHubError(message, code="unsafe_package_source") from exc


def _problem_directory(problem_dir, relative, label):
    if not isinstance(relative, str) or not relative:
        raise ProbHubError(f"{label} is required", code="unsafe_package_source")
    problem_dir = Path(problem_dir).resolve()
    relative_path = Path(relative)
    if relative_path.is_absolute() or relative_path.drive or ".." in relative_path.parts:
        raise ProbHubError(
            f"{label} must stay inside the problem directory: {relative}",
            code="unsafe_package_source",
        )
    current = problem_dir
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise ProbHubError(
                f"{label} must not traverse a symlink: {relative}",
                code="unsafe_package_source",
            )
    resolved = current.resolve()
    try:
        resolved.relative_to(problem_dir)
    except ValueError as exc:
        raise ProbHubError(
            f"{label} must stay inside the problem directory: {relative}",
            code="unsafe_package_source",
        ) from exc
    return resolved


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
    source = _problem_regular_file(problem_dir, source_value, f"judge.{source_key}")
    if not TESTLIB_PATH.is_file():
        raise ProbHubError(f"testlib.h not found: {TESTLIB_PATH}")

    if validate_dir.exists():
        shutil.rmtree(validate_dir)
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
    if judge_type not in {"standard", "custom", "interactive"}:
        raise ProbHubError(
            f"unsupported judge.type for DOMjudge packaging: {judge_type}",
            code="package_invalid_judge_type",
        )
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
        stdout_path = Path(temp) / "compiler.out"
        stderr_path = Path(temp) / "compiler.stderr"
        try:
            result = run_managed_to_files(
                command,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=60.0,
                memory_limit_mb=2048,
                output_limit_bytes=8 * 1024 * 1024,
                process_limit=DEFAULT_PROCESS_LIMIT,
            )
        except OSError as exc:
            raise ProbHubError(f"failed to run g++ for output validator: {exc}") from exc
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        if result["reason"] != "completed" or result["returncode"] != 0:
            detail = stderr.strip() or result["message"] or result["reason"]
            raise ProbHubError(f"output validator failed to compile: {detail}")
    return validate_dir


def _collect_regular_files(root, *, relative_to, prefix=None):
    root = Path(root)
    if root.is_symlink():
        raise ProbHubError(
            f"package source must not be a symlink: {root}",
            code="unsafe_package_source",
        )
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ProbHubError(
                f"package source must not be a symlink: {path}",
                code="unsafe_package_source",
            )
        if not path.is_file():
            continue
        if prefix is None:
            arcname = path.relative_to(relative_to).as_posix()
        else:
            relative = path.relative_to(root)
            if len(relative.parts) != 1:
                raise ProbHubError(
                    f"testcase directories must be flat: {path}",
                    code="unsafe_package_source",
                )
            arcname = f"{prefix}/{relative.as_posix()}"
        files.append((path, arcname))
    return files


def collect_package_files(problem_dir, config=None):
    problem_dir = Path(problem_dir)
    files = []
    for name in ROOT_FILES:
        path = problem_dir / name
        if path.is_symlink():
            raise ProbHubError(
                f"package source must not be a symlink: {path}",
                code="unsafe_package_source",
            )
        if path.is_file():
            files.append((path, name))
    if config is None:
        files.extend(
            _collect_regular_files(
                problem_dir / "data",
                relative_to=problem_dir,
            )
        )
    else:
        data = config.get("data") or {}
        for key, default, prefix in (
            ("sample_dir", "data/sample", "data/sample"),
            ("secret_dir", "data/secret", "data/secret"),
        ):
            configured_value = data.get(key, default)
            configured = _problem_directory(
                problem_dir,
                configured_value,
                f"data.{key}",
            )
            files.extend(
                _collect_regular_files(
                    configured,
                    relative_to=problem_dir,
                    prefix=prefix,
                )
            )
    files.extend(
        _collect_regular_files(
            problem_dir / "output_validators",
            relative_to=problem_dir,
        )
    )
    arcnames = [arcname for _, arcname in files]
    duplicates = sorted(name for name, count in Counter(arcnames).items() if count > 1)
    if duplicates:
        raise ProbHubError(
            "duplicate package source paths: " + ", ".join(duplicates),
            code="unsafe_package_source",
        )
    return sorted(files, key=lambda item: item[1])


def build_package(problem_dir, output_path, config=None):
    problem_dir = Path(problem_dir)
    output_path = Path(output_path)
    files = collect_package_files(problem_dir, config=config)
    if len(files) > MAX_PACKAGE_ENTRIES:
        raise ProbHubError(
            f"package source has too many files: {len(files)} > {MAX_PACKAGE_ENTRIES}",
            code="package_too_many_entries",
        )
    total_bytes = 0
    for source, arcname in files:
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise ProbHubError(
                f"cannot stat package source {source}: {exc}",
                code="package_source_unreadable",
            ) from exc
        if size > MAX_PACKAGE_ENTRY_BYTES:
            raise ProbHubError(
                f"package source exceeds entry size limit: {arcname}: {size} > {MAX_PACKAGE_ENTRY_BYTES}",
                code="package_entry_too_large",
            )
        total_bytes += size
    if total_bytes > MAX_PACKAGE_TOTAL_BYTES:
        raise ProbHubError(
            f"package sources exceed total size limit: {total_bytes} > {MAX_PACKAGE_TOTAL_BYTES}",
            code="package_total_too_large",
        )
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
                _write_package_member(archive, info, source, arcname)
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return files


def build_verified_package(
    problem_dir,
    output_path,
    require_pdf=False,
    *,
    expected_config=None,
    source_problem_dir=None,
    run_validator=None,
):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_path.stem}-package-",
        dir=output_path.parent,
    ) as temporary:
        staged = Path(temporary) / output_path.name
        files = build_package(problem_dir, staged, config=expected_config)
        verification = verify_package(
            staged,
            require_pdf=require_pdf,
            expected_config=expected_config,
            source_problem_dir=source_problem_dir,
            run_validator=run_validator,
        )
        if verification["ok"]:
            os.replace(staged, output_path)
        return files, verification


def _preflight_archive_file(
    zip_path,
    diagnostics,
    errors,
    warnings,
    stats,
    *,
    max_archive_bytes,
    max_entries,
):
    try:
        archive_size = Path(zip_path).stat().st_size
    except OSError as exc:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_unreadable",
            f"cannot stat ZIP: {exc}",
        )
        return False
    stats["archive_bytes"] = archive_size
    if archive_size > max_archive_bytes:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_archive_too_large",
            f"ZIP size exceeds limit: {archive_size} > {max_archive_bytes}",
            actual=archive_size,
            limit=max_archive_bytes,
        )
        return False

    try:
        with Path(zip_path).open("rb") as stream:
            tail_size = min(archive_size, ZIP_EOCD_SIZE + ZIP_MAX_COMMENT_BYTES)
            stream.seek(archive_size - tail_size)
            tail = stream.read(tail_size)
    except OSError as exc:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_unreadable",
            f"cannot read ZIP directory footer: {exc}",
        )
        return False
    offset = tail.rfind(ZIP_EOCD_SIGNATURE)
    while offset >= 0:
        if len(tail) - offset >= ZIP_EOCD_SIZE:
            candidate_comment_size = struct.unpack_from("<H", tail, offset + 20)[0]
            if offset + ZIP_EOCD_SIZE + candidate_comment_size == len(tail):
                break
        offset = tail.rfind(ZIP_EOCD_SIGNATURE, 0, offset)
    if offset < 0:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_unreadable",
            "cannot locate ZIP end-of-central-directory record",
        )
        return False
    (
        disk_number,
        directory_disk,
        entries_on_disk,
        total_entries,
        directory_size,
        directory_offset,
        _comment_size,
    ) = struct.unpack_from("<4H2LH", tail, offset + 4)
    if (
        disk_number != 0
        or directory_disk != 0
        or entries_on_disk != total_entries
    ):
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_multidisk_unsupported",
            "multi-disk ZIP archives are not supported",
        )
        return False
    if (
        total_entries == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
    ):
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_zip64_unsupported",
            "ZIP64 archives are outside the package verification limits",
        )
        return False
    if total_entries > max_entries:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_too_many_entries",
            f"ZIP entry count exceeds limit: {total_entries} > {max_entries}",
            actual=total_entries,
            limit=max_entries,
        )
        return False
    eocd_offset = archive_size - tail_size + offset
    if directory_offset + directory_size > eocd_offset:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_unreadable",
            "ZIP central directory extends beyond its declared boundary",
        )
        return False
    actual_directory_offset = eocd_offset - directory_size
    actual_entries = 0
    try:
        with Path(zip_path).open("rb") as stream:
            stream.seek(actual_directory_offset)
            while stream.tell() < eocd_offset:
                header = stream.read(ZIP_CENTRAL_HEADER_SIZE)
                if (
                    len(header) != ZIP_CENTRAL_HEADER_SIZE
                    or header[:4] != ZIP_CENTRAL_HEADER_SIGNATURE
                ):
                    raise ValueError("invalid central directory header")
                name_size, extra_size, entry_comment_size = struct.unpack_from(
                    "<3H", header, 28
                )
                variable_size = name_size + extra_size + entry_comment_size
                if stream.tell() + variable_size > eocd_offset:
                    raise ValueError("central directory entry exceeds declared size")
                stream.seek(variable_size, os.SEEK_CUR)
                actual_entries += 1
                if actual_entries > max_entries:
                    _add_diagnostic(
                        diagnostics,
                        errors,
                        warnings,
                        "package_too_many_entries",
                        f"ZIP entry count exceeds limit: more than {max_entries}",
                        actual=actual_entries,
                        limit=max_entries,
                    )
                    return False
    except (OSError, ValueError) as exc:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_unreadable",
            f"cannot validate ZIP central directory: {exc}",
        )
        return False
    if actual_entries != total_entries:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_unreadable",
            "ZIP central directory entry count does not match its footer",
            declared=total_entries,
            actual=actual_entries,
        )
        return False
    stats["entries"] = actual_entries
    return True


def _preflight_archive(
    archive,
    diagnostics,
    errors,
    warnings,
    stats,
    *,
    max_entry_bytes,
    max_total_bytes,
    max_entries,
):
    infos = archive.infolist()
    stats["entries"] = len(infos)
    file_infos = [info for info in infos if not info.is_dir()]
    stats["files"] = len(file_infos)
    if len(infos) > max_entries:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_too_many_entries",
            f"ZIP entry count exceeds limit: {len(infos)} > {max_entries}",
            actual=len(infos),
            limit=max_entries,
        )

    counts = Counter(info.filename for info in infos)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_duplicate_entry",
            "duplicate entries: " + ", ".join(duplicates),
            entries=duplicates,
        )

    canonical = {}
    for info in infos:
        key = _canonical_archive_name(info.filename)
        canonical.setdefault(key, set()).add(info.filename.rstrip("/"))
    collisions = sorted(
        sorted(values)
        for values in canonical.values()
        if len(values) > 1
    )
    if collisions:
        rendered = "; ".join(" / ".join(group) for group in collisions)
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_case_collision",
            "case-insensitive path collisions: " + rendered,
            collisions=collisions,
        )

    declared_total = 0
    for info in infos:
        name = info.filename
        if not _safe_path(name):
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_unsafe_path",
                f"unsafe ZIP path: {name}",
                entry=name,
            )
            continue
        if not _allowed_package_path(name, is_dir=info.is_dir()):
            suffix = Path(name).suffix
            if (
                not info.is_dir()
                and name.startswith("data/")
                and suffix.lower() in TEXT_DATA_SUFFIXES
                and suffix not in TEXT_DATA_SUFFIXES
            ):
                _add_diagnostic(
                    diagnostics,
                    errors,
                    warnings,
                    "package_noncanonical_data_extension",
                    f"testcase extensions must be lowercase .in or .ans: {name}",
                    entry=name,
                )
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_unexpected_path",
                f"unexpected package path: {name}",
                entry=name,
            )
        parts = [part.casefold() for part in PurePosixPath(name.rstrip("/")).parts]
        lowered_name = name.casefold()
        if any(part in BANNED_PATH_PARTS for part in parts) or lowered_name.endswith(("~", ".tmp", ".cache")):
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_cache_entry",
                f"cache or temporary entry is not allowed: {name}",
                entry=name,
            )
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_symlink_entry",
                f"symbolic links are not allowed in packages: {name}",
                entry=name,
            )
        file_type = stat.S_IFMT(mode)
        allowed_types = {0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG}
        if file_type not in allowed_types:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_special_entry",
                f"special filesystem entries are not allowed: {name}",
                entry=name,
                mode=oct(mode),
            )
        if not info.is_dir() and (mode & 0o111 or Path(name).suffix.lower() in EXECUTABLE_SUFFIXES):
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_executable_entry",
                f"executable or compiled entry is not allowed: {name}",
                entry=name,
            )
        if info.flag_bits & 0x1:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_encrypted_entry",
                f"encrypted ZIP entry is not allowed: {name}",
                entry=name,
            )
        if info.compress_type not in ALLOWED_COMPRESSION:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_unsupported_compression",
                f"unsupported ZIP compression for {name}: {info.compress_type}",
                entry=name,
                compression=info.compress_type,
            )
        if not info.is_dir():
            declared_total += info.file_size
            if info.file_size > max_entry_bytes:
                _add_diagnostic(
                    diagnostics,
                    errors,
                    warnings,
                    "package_entry_too_large",
                    f"ZIP entry exceeds uncompressed size limit: {name}: {info.file_size} > {max_entry_bytes}",
                    entry=name,
                    actual=info.file_size,
                    limit=max_entry_bytes,
                )
    stats["declared_uncompressed_bytes"] = declared_total
    if declared_total > max_total_bytes:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_total_too_large",
            f"ZIP uncompressed size exceeds limit: {declared_total} > {max_total_bytes}",
            actual=declared_total,
            limit=max_total_bytes,
        )
    return file_infos


def _extract_archive_streaming(
    archive,
    file_infos,
    destination,
    diagnostics,
    errors,
    warnings,
    stats,
    *,
    max_entry_bytes,
    max_total_bytes,
):
    destination = Path(destination)
    actual_total = 0
    extracted = {}
    try:
        for info in file_infos:
            target = destination.joinpath(*PurePosixPath(info.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            entry_total = 0
            with archive.open(info, "r") as source, target.open("xb") as output:
                while True:
                    chunk = source.read(PACKAGE_READ_CHUNK)
                    if not chunk:
                        break
                    entry_total += len(chunk)
                    actual_total += len(chunk)
                    if entry_total > max_entry_bytes:
                        _add_diagnostic(
                            diagnostics,
                            errors,
                            warnings,
                            "package_entry_too_large",
                            f"ZIP entry exceeded uncompressed size limit while reading: {info.filename}",
                            entry=info.filename,
                            actual=entry_total,
                            limit=max_entry_bytes,
                        )
                        return False
                    if actual_total > max_total_bytes:
                        _add_diagnostic(
                            diagnostics,
                            errors,
                            warnings,
                            "package_total_too_large",
                            "ZIP exceeded total uncompressed size limit while reading",
                            actual=actual_total,
                            limit=max_total_bytes,
                        )
                        return False
                    output.write(chunk)
            if entry_total != info.file_size:
                _add_diagnostic(
                    diagnostics,
                    errors,
                    warnings,
                    "package_size_mismatch",
                    f"ZIP entry size changed while extracting: {info.filename}",
                    entry=info.filename,
                    declared=info.file_size,
                    actual=entry_total,
                )
                return False
            extracted[info.filename] = target
    except (BadZipFile, OSError, RuntimeError, EOFError) as exc:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_extract_failed",
            f"failed to extract ZIP safely: {exc}",
        )
        return False

    actual_names = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    expected_names = set(extracted)
    if actual_names != expected_names:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_extraction_mismatch",
            "independent extraction file set does not match ZIP entries",
            missing=sorted(expected_names - actual_names),
            unexpected=sorted(actual_names - expected_names),
        )
        return False
    stats["extracted_bytes"] = actual_total
    return True


def _read_utf8(path, label, diagnostics, errors, warnings):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_invalid_text",
            f"{label} is not valid UTF-8 text: {exc}",
            entry=label,
        )
        return None


def _newline_style(path):
    has_crlf = False
    has_bare_cr = False
    pending_cr = False
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(PACKAGE_READ_CHUNK)
            if not chunk:
                break
            if pending_cr:
                chunk = b"\r" + chunk
                pending_cr = False
            if chunk.endswith(b"\r"):
                chunk = chunk[:-1]
                pending_cr = True
            if b"\r\n" in chunk:
                has_crlf = True
            if b"\r" in chunk.replace(b"\r\n", b""):
                has_bare_cr = True
    if pending_cr:
        has_bare_cr = True
    if has_crlf and has_bare_cr:
        return "mixed"
    if has_crlf:
        return "crlf"
    if has_bare_cr:
        return "bare_cr"
    return "lf"


def _validate_extracted_structure(
    extracted_root,
    names,
    require_pdf,
    diagnostics,
    errors,
    warnings,
    stats,
):
    extracted_root = Path(extracted_root)
    for required in ("problem.yaml", "domjudge-problem.ini"):
        if required not in names:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_missing_root_file",
                f"missing root file: {required}",
                entry=required,
            )
    if require_pdf and "problem.pdf" not in names:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_missing_root_file",
            "missing root file: problem.pdf",
            entry="problem.pdf",
        )
    if "problem.pdf" in names:
        try:
            with (extracted_root / "problem.pdf").open("rb") as stream:
                valid_pdf = stream.read(5) == b"%PDF-"
        except OSError:
            valid_pdf = False
        if not valid_pdf:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_invalid_pdf",
                "problem.pdf is not a valid PDF file",
                entry="problem.pdf",
            )

    inputs = {name[:-3] for name in names if name.startswith("data/") and name.endswith(".in")}
    answers = {name[:-4] for name in names if name.startswith("data/") and name.endswith(".ans")}
    if inputs - answers:
        missing = sorted(inputs - answers)
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_input_without_answer",
            "inputs without answers: " + ", ".join(missing),
            cases=missing,
        )
    if answers - inputs:
        missing = sorted(answers - inputs)
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_answer_without_input",
            "answers without inputs: " + ", ".join(missing),
            cases=missing,
        )
    samples = sorted(name for name in names if name.startswith("data/sample/") and name.endswith(".in"))
    secrets = sorted(name for name in names if name.startswith("data/secret/") and name.endswith(".in"))
    stats.update(sample_cases=len(samples), secret_cases=len(secrets))
    if not samples:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_no_samples",
            "no sample input cases",
        )
    if not secrets:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_no_secrets",
            "no secret input cases",
        )
    for name in sorted(
        item for item in names
        if item.startswith("data/") and Path(item).suffix.lower() in TEXT_DATA_SUFFIXES
    ):
        try:
            newline_style = _newline_style(extracted_root.joinpath(*PurePosixPath(name).parts))
        except OSError as exc:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_data_unreadable",
                f"cannot read testcase text {name}: {exc}",
                entry=name,
            )
            continue
        if newline_style != "lf":
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_data_not_lf",
                f"testcase text must be LF-only: {name}",
                entry=name,
                newline_style=newline_style,
            )

    parsed = {"name": None, "memory": None, "validation": None, "timelimit": None}
    if "problem.yaml" in names:
        yaml_text = _read_utf8(
            extracted_root / "problem.yaml",
            "problem.yaml",
            diagnostics,
            errors,
            warnings,
        )
        if yaml_text is not None:
            try:
                problem_yaml = yaml.load(yaml_text, Loader=_UniqueKeyLoader)
            except yaml.YAMLError as exc:
                problem_yaml = None
                _add_diagnostic(
                    diagnostics,
                    errors,
                    warnings,
                    "package_invalid_problem_yaml",
                    f"problem.yaml is invalid YAML: {exc}",
                    entry="problem.yaml",
                )
            if not isinstance(problem_yaml, dict):
                if problem_yaml is not None:
                    _add_diagnostic(
                        diagnostics,
                        errors,
                        warnings,
                        "package_invalid_problem_yaml",
                        "problem.yaml root must be a mapping",
                        entry="problem.yaml",
                    )
            else:
                parsed["name"] = problem_yaml.get("name")
                if not isinstance(parsed["name"], str) or not parsed["name"].strip():
                    _add_diagnostic(
                        diagnostics,
                        errors,
                        warnings,
                        "package_missing_name",
                        "problem.yaml has no non-empty root name field",
                        entry="problem.yaml",
                    )
                limits = problem_yaml.get("limits")
                memory = limits.get("memory") if isinstance(limits, dict) else None
                parsed["memory"] = memory
                if isinstance(memory, bool) or not isinstance(memory, int) or memory <= 0:
                    _add_diagnostic(
                        diagnostics,
                        errors,
                        warnings,
                        "package_invalid_memory_limit",
                        "problem.yaml has no positive integer memory limit",
                        entry="problem.yaml",
                    )
                validation = problem_yaml.get("validation")
                parsed["validation"] = validation
                if validation not in {None, "custom", "custom interactive"}:
                    _add_diagnostic(
                        diagnostics,
                        errors,
                        warnings,
                        "package_invalid_judge_type",
                        f"problem.yaml has unsupported validation mode: {validation}",
                        entry="problem.yaml",
                    )

    if "domjudge-problem.ini" in names:
        ini_text = _read_utf8(
            extracted_root / "domjudge-problem.ini",
            "domjudge-problem.ini",
            diagnostics,
            errors,
            warnings,
        )
        if ini_text is not None:
            ini_values, invalid_ini_keys = _parse_domjudge_ini(
                ini_text,
                diagnostics,
                errors,
                warnings,
            )
            if "timelimit" not in invalid_ini_keys:
                parsed["timelimit"] = _parse_decimal(ini_values.get("timelimit"))
            if parsed["timelimit"] is None and "timelimit" not in invalid_ini_keys:
                _add_diagnostic(
                    diagnostics,
                    errors,
                    warnings,
                    "package_invalid_time_limit",
                    "domjudge-problem.ini has no positive numeric timelimit",
                    entry="domjudge-problem.ini",
                )

    validator_files = {
        name for name in names if name.startswith("output_validators/")
    }
    validation = parsed["validation"]
    if validation in {"custom", "custom interactive"}:
        for required in (
            "output_validators/validate/validate.cpp",
            "output_validators/validate/testlib.h",
        ):
            if required not in names:
                _add_diagnostic(
                    diagnostics,
                    errors,
                    warnings,
                    "package_missing_output_validator",
                    f"missing custom validator file: {required}",
                    entry=required,
                )
    elif validator_files:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_judge_type_mismatch",
            "standard package must not contain output_validators",
            entries=sorted(validator_files),
        )
    return parsed


def _validate_packaged_output_validator(
    extracted_root,
    parsed,
    diagnostics,
    errors,
    warnings,
    stats,
):
    if parsed.get("validation") not in {"custom", "custom interactive"}:
        return
    validate_dir = Path(extracted_root) / "output_validators" / "validate"
    source = validate_dir / "validate.cpp"
    testlib = validate_dir / "testlib.h"
    if not source.is_file() or not testlib.is_file():
        return
    with tempfile.TemporaryDirectory(prefix="probhub-package-output-validator-") as temporary:
        output = Path(temporary) / ("validate.exe" if os.name == "nt" else "validate")
        command = [
            "g++",
            str(source),
            "-o",
            str(output),
            "-O2",
            "-std=c++17",
            "-I",
            str(validate_dir),
        ]
        if os.name == "nt":
            command.append("-static")
        stdout_path = Path(temporary) / "compiler.out"
        stderr_path = Path(temporary) / "compiler.stderr"
        try:
            result = run_managed_to_files(
                command,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=60.0,
                memory_limit_mb=2048,
                output_limit_bytes=8 * 1024 * 1024,
                process_limit=DEFAULT_PROCESS_LIMIT,
            )
        except OSError as exc:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_output_validator_compile_failed",
                f"failed to run g++ for packaged output validator: {exc}",
            )
            return
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        if result["reason"] != "completed" or result["returncode"] != 0:
            detail = stderr.strip() or result["message"] or result["reason"]
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_output_validator_compile_failed",
                f"packaged output validator failed to compile: {detail}",
            )
            return
        stats["output_validator_compiled"] = True


def _normalised_file_digest(path):
    digest = hashlib.sha256()
    pending_cr = False
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(PACKAGE_READ_CHUNK)
            if not chunk:
                break
            if pending_cr:
                chunk = b"\r" + chunk
                pending_cr = False
            if chunk.endswith(b"\r"):
                chunk = chunk[:-1]
                pending_cr = True
            digest.update(_normalise_package_text(chunk))
    if pending_cr:
        digest.update(b"\n")
    return digest.hexdigest()


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(PACKAGE_READ_CHUNK)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _validate_expected_source(
    extracted_root,
    names,
    expected_config,
    source_problem_dir,
    parsed,
    diagnostics,
    errors,
    warnings,
):
    expected_name = expected_config.get("name") or expected_config.get("display_name")
    if parsed["name"] != expected_name:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_name_mismatch",
            f"problem name mismatch: package={parsed['name']!r}, expected={expected_name!r}",
            actual=parsed["name"],
            expected=expected_name,
        )
    limits = expected_config.get("limits") or {}
    expected_memory = limits.get("memory")
    if parsed["memory"] != expected_memory:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_memory_mismatch",
            f"memory limit mismatch: package={parsed['memory']!r}, expected={expected_memory!r}",
            actual=parsed["memory"],
            expected=expected_memory,
        )
    expected_time = _parse_decimal(limits.get("time"))
    if parsed["timelimit"] != expected_time:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_time_mismatch",
            f"time limit mismatch: package={parsed['timelimit']!r}, expected={expected_time!r}",
            actual=str(parsed["timelimit"]) if parsed["timelimit"] is not None else None,
            expected=str(expected_time) if expected_time is not None else None,
        )
    expected_validation = _expected_validation(expected_config)
    expected_judge_type = _judge_type(expected_config)
    if expected_judge_type not in {"standard", "custom", "interactive"}:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_source_config_invalid",
            f"source config has unsupported judge.type: {expected_judge_type}",
            expected=expected_judge_type,
        )
    if parsed["validation"] != expected_validation:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_judge_type_mismatch",
            f"judge type mismatch: package={parsed['validation']!r}, expected={expected_validation!r}",
            actual=parsed["validation"],
            expected=expected_validation,
        )

    source_problem_dir = Path(source_problem_dir)
    data = expected_config.get("data") or {}
    expected_files = {}
    for key, default, prefix in (
        ("sample_dir", "data/sample", "data/sample"),
        ("secret_dir", "data/secret", "data/secret"),
    ):
        try:
            directory = _problem_directory(
                source_problem_dir,
                data.get(key, default),
                f"data.{key}",
            )
        except ProbHubError as exc:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                exc.code or "unsafe_package_source",
                str(exc),
                field=f"data.{key}",
            )
            continue
        if directory.is_dir():
            for path in sorted(directory.glob("*")):
                if path.is_symlink():
                    _add_diagnostic(
                        diagnostics,
                        errors,
                        warnings,
                        "unsafe_package_source",
                        f"data.{key} must not contain symlinks: {path.name}",
                        field=f"data.{key}",
                        path=path.name,
                    )
                    continue
                if path.is_file() and path.suffix in TEXT_DATA_SUFFIXES:
                    expected_files[f"{prefix}/{path.name}"] = path
    packaged_data = {
        name for name in names
        if name.startswith("data/") and Path(name).suffix in TEXT_DATA_SUFFIXES
    }
    expected_names = set(expected_files)
    if packaged_data != expected_names:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_data_set_mismatch",
            "package testcase set does not match the source workspace",
            missing=sorted(expected_names - packaged_data),
            unexpected=sorted(packaged_data - expected_names),
        )
    for name in sorted(packaged_data & expected_names):
        packaged = Path(extracted_root).joinpath(*PurePosixPath(name).parts)
        try:
            source_digest = _normalised_file_digest(expected_files[name])
            package_digest = _file_digest(packaged)
        except OSError as exc:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_data_compare_failed",
                f"failed to compare testcase {name}: {exc}",
                entry=name,
            )
            continue
        if package_digest != source_digest:
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_data_content_mismatch",
                f"packaged testcase differs from LF-normalized source: {name}",
                entry=name,
            )


def _validator_source(problem_dir, config):
    relative = (config.get("judge") or {}).get("validator")
    try:
        return _problem_regular_file(problem_dir, relative, "judge.validator")
    except ProbHubError as exc:
        raise ProbHubError(
            str(exc),
            code="package_validator_missing",
        ) from exc


def _compile_input_validator(source, build_dir, diagnostics, errors, warnings):
    source = Path(source)
    if source.suffix.lower() == ".py":
        return [sys.executable, str(source)]
    if source.suffix.lower() != ".cpp":
        if os.name != "nt" and not os.access(source, os.X_OK):
            _add_diagnostic(
                diagnostics,
                errors,
                warnings,
                "package_validator_not_executable",
                f"input validator is not executable: {source}",
            )
            return None
        return [str(source)]

    output = Path(build_dir) / ("validator.exe" if os.name == "nt" else "validator")
    command = [
        "g++",
        str(source),
        "-o",
        str(output),
        "-O2",
        "-std=c++17",
        "-I",
        str(PACKAGE_ROOT / "references"),
        "-I",
        str(source.parent),
    ]
    if os.name == "nt":
        command.extend(("-static", "-DFOR_LINUX"))
    stdout_path = Path(build_dir) / "validator-compiler.out"
    stderr_path = Path(build_dir) / "validator-compiler.stderr"
    try:
        result = run_managed_to_files(
            command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout=60.0,
            memory_limit_mb=2048,
            output_limit_bytes=8 * 1024 * 1024,
            process_limit=DEFAULT_PROCESS_LIMIT,
        )
    except OSError as exc:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_validator_compile_failed",
            f"failed to run g++ for input validator: {exc}",
        )
        return None
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    if result["reason"] != "completed" or result["returncode"] != 0:
        detail = stderr.strip() or result["message"] or result["reason"]
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_validator_compile_failed",
            f"input validator failed to compile: {detail}",
        )
        return None
    return [str(output)]


def _run_input_validator(
    extracted_root,
    names,
    source_problem_dir,
    expected_config,
    diagnostics,
    errors,
    warnings,
    stats,
):
    try:
        source = _validator_source(source_problem_dir, expected_config)
    except ProbHubError as exc:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            exc.code or "package_validator_missing",
            str(exc),
        )
        return

    limits = expected_config.get("limits") or {}
    process_limit = limits.get("processes", DEFAULT_PROCESS_LIMIT)
    if isinstance(process_limit, bool) or not isinstance(process_limit, int) or process_limit <= 0:
        process_limit = DEFAULT_PROCESS_LIMIT
    tool_timeout = (expected_config.get("data") or {}).get("gen_tool_timeout", 60)
    if isinstance(tool_timeout, bool) or not isinstance(tool_timeout, (int, float)) or tool_timeout <= 0:
        tool_timeout = 60
    tool_timeout = min(float(tool_timeout), 3600.0)

    input_names = sorted(name for name in names if name.startswith("data/") and name.endswith(".in"))
    checked = 0
    with tempfile.TemporaryDirectory(prefix="probhub-package-validator-") as temporary:
        build_dir = Path(temporary)
        command = _compile_input_validator(source, build_dir, diagnostics, errors, warnings)
        if command is None:
            return
        for index, name in enumerate(input_names):
            stdout_path = build_dir / f"case-{index}.out"
            stderr_path = build_dir / f"case-{index}.stderr"
            input_path = Path(extracted_root).joinpath(*PurePosixPath(name).parts)
            try:
                result = run_managed_to_files(
                    command,
                    input_path=input_path,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    timeout=tool_timeout,
                    memory_limit_mb=2048,
                    output_limit_bytes=1024 * 1024,
                    process_limit=process_limit,
                    cwd=source_problem_dir,
                )
            except ProcessCancelled:
                raise
            except OSError as exc:
                _add_diagnostic(
                    diagnostics,
                    errors,
                    warnings,
                    "package_validator_run_failed",
                    f"failed to run input validator for {name}: {exc}",
                    entry=name,
                )
                continue
            checked += 1
            if result["reason"] != "completed" or result["returncode"] != 0:
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                detail = stderr[:1000] or result["message"] or result["reason"]
                _add_diagnostic(
                    diagnostics,
                    errors,
                    warnings,
                    "package_validator_rejected_input",
                    f"input validator rejected {name}: {detail}",
                    entry=name,
                    reason=result["reason"],
                    returncode=result["returncode"],
                )
    stats["validator_cases"] = checked


def verify_package(
    zip_path,
    require_pdf=False,
    *,
    expected_config=None,
    source_problem_dir=None,
    run_validator=None,
    max_archive_bytes=MAX_PACKAGE_ARCHIVE_BYTES,
    max_entry_bytes=MAX_PACKAGE_ENTRY_BYTES,
    max_total_bytes=MAX_PACKAGE_TOTAL_BYTES,
    max_entries=MAX_PACKAGE_ENTRIES,
):
    errors, warnings, diagnostics = [], [], []
    stats = {
        "sample_cases": 0,
        "secret_cases": 0,
        "files": 0,
        "entries": 0,
        "archive_bytes": 0,
        "declared_uncompressed_bytes": 0,
        "extracted_bytes": 0,
        "validator_cases": 0,
        "output_validator_compiled": False,
    }
    zip_path = Path(zip_path)
    streaming_extraction = False
    source_consistency = False
    input_validator = False
    if run_validator is None:
        run_validator = expected_config is not None and source_problem_dir is not None
    if (expected_config is None) != (source_problem_dir is None):
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_source_context_incomplete",
            "expected_config and source_problem_dir must be provided together",
        )
    try:
        archive_ready = _preflight_archive_file(
            zip_path,
            diagnostics,
            errors,
            warnings,
            stats,
            max_archive_bytes=max_archive_bytes,
            max_entries=max_entries,
        )
        if archive_ready and not errors:
            with ZipFile(zip_path) as archive:
                file_infos = _preflight_archive(
                    archive,
                    diagnostics,
                    errors,
                    warnings,
                    stats,
                    max_entry_bytes=max_entry_bytes,
                    max_total_bytes=max_total_bytes,
                    max_entries=max_entries,
                )
                if file_infos is not None and not errors:
                    with tempfile.TemporaryDirectory(prefix="probhub-package-verify-") as temporary:
                        extracted_root = Path(temporary) / "package"
                        extracted_root.mkdir()
                        extracted = _extract_archive_streaming(
                            archive,
                            file_infos,
                            extracted_root,
                            diagnostics,
                            errors,
                            warnings,
                            stats,
                            max_entry_bytes=max_entry_bytes,
                            max_total_bytes=max_total_bytes,
                        )
                        if extracted:
                            streaming_extraction = True
                            names = {info.filename for info in file_infos}
                            parsed = _validate_extracted_structure(
                                extracted_root,
                                names,
                                require_pdf,
                                diagnostics,
                                errors,
                                warnings,
                                stats,
                            )
                            _validate_packaged_output_validator(
                                extracted_root,
                                parsed,
                                diagnostics,
                                errors,
                                warnings,
                                stats,
                            )
                            if expected_config is not None and source_problem_dir is not None:
                                source_consistency = True
                                _validate_expected_source(
                                    extracted_root,
                                    names,
                                    expected_config,
                                    source_problem_dir,
                                    parsed,
                                    diagnostics,
                                    errors,
                                    warnings,
                                )
                                if run_validator:
                                    input_validator = True
                                    _run_input_validator(
                                        extracted_root,
                                        names,
                                        source_problem_dir,
                                        expected_config,
                                        diagnostics,
                                        errors,
                                        warnings,
                                        stats,
                                    )
    except (OSError, BadZipFile) as exc:
        _add_diagnostic(
            diagnostics,
            errors,
            warnings,
            "package_unreadable",
            f"cannot read ZIP: {exc}",
        )
    return {
        "ok": not errors,
        "verification_scope": (
            "deep"
            if source_consistency and input_validator
            else "source"
            if source_consistency
            else "structural"
        ),
        "errors": errors,
        "warnings": warnings,
        "diagnostics": diagnostics,
        "stats": stats,
        "checks": {
            "streaming_extraction": streaming_extraction,
            "source_consistency": source_consistency,
            "input_validator": input_validator,
        },
    }
