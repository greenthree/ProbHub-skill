"""Strict, dependency-free parsing for the packaged Python runtime lock."""

from __future__ import annotations

import hashlib
import platform
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


LOCK_SCHEMA_VERSION = 1
REQUIRE_HASHES_OPTION = "--require-hashes"
BINARY_ONLY_OPTION = "--only-binary=:all:"
SUPPORTED_PYTHON_VERSIONS = ("3.10", "3.11", "3.12")
SUPPORTED_PLATFORM_SYSTEMS = ("Windows", "Linux")
SUPPORTED_RUNTIME_DESCRIPTION = "CPython 3.10-3.12 on Windows/Linux x86_64"

_NAME_RE = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_VERSION_RE = r"[A-Za-z0-9][A-Za-z0-9.!+_-]*"
_PIN_RE = re.compile(
    rf"^(?P<name>{_NAME_RE})==(?P<version>{_VERSION_RE})"
    r"(?:\s*;\s*(?P<marker>.+))?$"
)
_MARKER_RE = re.compile(
    r"^(?P<variable>python_version|platform_system)\s*"
    r"(?P<operator>==|!=|<=|>=|<|>)\s*"
    r"(?P<quote>['\"])(?P<value>[^'\"]+)(?P=quote)$"
)
_ARTIFACT_RE = re.compile(
    r"^# artifact: (?P<filename>\S+) sha256:(?P<sha256>[0-9a-f]{64}) "
    r"size:(?P<size>[1-9][0-9]*)$"
)
_HASH_LINE_RE = re.compile(
    r"^\s{4}--hash=sha256:(?P<sha256>[0-9a-f]{64})(?: (?P<continued>\\))?$"
)
_SOURCE_HASH_RE = re.compile(r"^# source-sha256: (?P<sha256>[0-9a-f]{64})$")
_SCHEMA_RE = re.compile(r"^# probhub-lock-schema: (?P<version>[0-9]+)$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")


class DependencyLockError(RuntimeError):
    """A stable fail-closed dependency lock diagnostic."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RequirementPin:
    name: str
    normalized_name: str
    version: str
    marker: str | None
    text: str


@dataclass(frozen=True)
class LockedArtifact:
    filename: str
    sha256: str
    size: int


@dataclass(frozen=True)
class LockedRequirement:
    pin: RequirementPin
    artifacts: tuple[LockedArtifact, ...]


@dataclass(frozen=True)
class DependencyLock:
    schema_version: int
    source_sha256: str
    requirements: tuple[LockedRequirement, ...]
    lock_sha256: str
    text: str


@dataclass(frozen=True)
class DependencyTarget:
    identifier: str
    platform_system: str
    python_version: str
    platform_tag: str


SUPPORTED_DEPENDENCY_TARGETS = tuple(
    DependencyTarget(
        identifier=f"{system.lower()}-x86_64-cp{version.replace('.', '')}",
        platform_system=system,
        python_version=version,
        platform_tag="win_amd64" if system == "Windows" else "manylinux_x86_64",
    )
    for system in SUPPORTED_PLATFORM_SYSTEMS
    for version in SUPPORTED_PYTHON_VERSIONS
)


def _normalized_text(text: str) -> str:
    if not isinstance(text, str):
        raise DependencyLockError("dependency_lock_invalid", "dependency lock content must be text")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _text_sha256(text: str) -> str:
    return hashlib.sha256(_normalized_text(text).encode("utf-8")).hexdigest()


def normalize_project_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def current_dependency_target() -> DependencyTarget:
    """Return the lock target for the running interpreter or fail closed."""

    implementation = getattr(sys, "implementation", None)
    implementation_name = getattr(implementation, "name", "")
    system = platform.system()
    machine = platform.machine().lower()
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if implementation_name != "cpython":
        raise DependencyLockError(
            "dependency_runtime_unsupported",
            f"Python implementation {implementation_name or 'unknown'} is unsupported; "
            f"use {SUPPORTED_RUNTIME_DESCRIPTION}",
        )
    if system not in SUPPORTED_PLATFORM_SYSTEMS:
        raise DependencyLockError(
            "dependency_runtime_unsupported",
            f"platform {system or 'unknown'} is unsupported; "
            f"use {SUPPORTED_RUNTIME_DESCRIPTION}",
        )
    if machine not in {"x86_64", "amd64"}:
        raise DependencyLockError(
            "dependency_runtime_unsupported",
            f"CPU architecture {machine or 'unknown'} is unsupported; "
            f"use {SUPPORTED_RUNTIME_DESCRIPTION}",
        )
    if python_version not in SUPPORTED_PYTHON_VERSIONS:
        raise DependencyLockError(
            "dependency_runtime_unsupported",
            f"Python {python_version} is unsupported; use {SUPPORTED_RUNTIME_DESCRIPTION}",
        )
    return DependencyTarget(
        identifier=f"{system.lower()}-x86_64-cp{python_version.replace('.', '')}",
        platform_system=system,
        python_version=python_version,
        platform_tag="win_amd64" if system == "Windows" else "manylinux_x86_64",
    )


def _validate_marker(marker: str | None, *, line_number: int) -> None:
    if marker is None:
        return
    match = _MARKER_RE.fullmatch(marker)
    if not match:
        raise DependencyLockError(
            "dependency_source_invalid",
            f"unsupported environment marker on line {line_number}: {marker}",
        )
    if match.group("variable") == "python_version":
        if not re.fullmatch(r"[0-9]+\.[0-9]+", match.group("value")):
            raise DependencyLockError(
                "dependency_source_invalid",
                f"python_version marker must use major.minor on line {line_number}",
            )
    elif match.group("operator") not in {"==", "!="}:
        raise DependencyLockError(
            "dependency_source_invalid",
            f"platform_system marker only supports == or != on line {line_number}",
        )


def parse_source_requirements(text: str) -> tuple[RequirementPin, ...]:
    """Parse the small, exact-pin runtime source format used by this project."""

    normalized = _normalized_text(text)
    pins = []
    names = set()
    for line_number, raw_line in enumerate(normalized.split("\n"), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN_RE.fullmatch(stripped)
        if not match:
            raise DependencyLockError(
                "dependency_source_invalid",
                f"runtime requirement on line {line_number} must be an exact name==version pin",
            )
        name = match.group("name")
        normalized_name = normalize_project_name(name)
        if normalized_name in names:
            raise DependencyLockError(
                "dependency_source_invalid",
                f"duplicate runtime requirement on line {line_number}: {name}",
            )
        marker = match.group("marker")
        _validate_marker(marker, line_number=line_number)
        names.add(normalized_name)
        pins.append(
            RequirementPin(
                name=name,
                normalized_name=normalized_name,
                version=match.group("version"),
                marker=marker,
                text=stripped,
            )
        )
    if not pins:
        raise DependencyLockError("dependency_source_invalid", "runtime requirements are empty")
    return tuple(pins)


def marker_applies(pin: RequirementPin, environment: dict | None = None) -> bool:
    """Evaluate the deliberately restricted marker grammar without third-party code."""

    if pin.marker is None:
        return True
    environment = environment or {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform_system": platform.system(),
    }
    match = _MARKER_RE.fullmatch(pin.marker)
    if not match:  # parse_source_requirements normally makes this unreachable.
        raise DependencyLockError("dependency_source_invalid", f"unsupported marker: {pin.marker}")
    variable = match.group("variable")
    left = environment.get(variable)
    if not isinstance(left, str) or not left:
        raise DependencyLockError(
            "dependency_marker_environment_invalid",
            f"marker environment is missing {variable}",
        )
    right = match.group("value")
    if variable == "python_version":
        try:
            left_value = tuple(int(part) for part in left.split("."))
            right_value = tuple(int(part) for part in right.split("."))
        except ValueError as exc:
            raise DependencyLockError(
                "dependency_marker_environment_invalid",
                "python_version marker environment must use numeric components",
            ) from exc
    else:
        left_value = left
        right_value = right
    operator = match.group("operator")
    comparisons = {
        "==": left_value == right_value,
        "!=": left_value != right_value,
        "<": left_value < right_value,
        "<=": left_value <= right_value,
        ">": left_value > right_value,
        ">=": left_value >= right_value,
    }
    return comparisons[operator]


def _python_tag_supports(python_tag: str, abi_tag: str, python_version: str) -> bool:
    target_major, target_minor = (int(part) for part in python_version.split("."))
    target_cp = f"cp{target_major}{target_minor}"
    tags = set(python_tag.split("."))
    if "py3" in tags or f"py{target_major}" in tags:
        return abi_tag == "none"
    if target_cp in tags:
        return abi_tag in {target_cp, "abi3", "none"}
    if abi_tag == "abi3":
        for tag in tags:
            match = re.fullmatch(r"cp([0-9])([0-9]+)", tag)
            if match and (int(match.group(1)), int(match.group(2))) <= (target_major, target_minor):
                return True
    return False


def wheel_supports_target(filename: str, target: DependencyTarget) -> bool:
    if not isinstance(filename, str) or not filename.endswith(".whl"):
        return False
    try:
        _, python_tag, abi_tag, platform_tag = filename[:-4].rsplit("-", 3)
    except ValueError:
        return False
    if not _python_tag_supports(python_tag, abi_tag, target.python_version):
        return False
    platforms = platform_tag.lower().split(".")
    if "any" in platforms:
        return True
    if target.platform_system == "Windows":
        return "win_amd64" in platforms
    return any(tag.startswith("manylinux") and tag.endswith("x86_64") for tag in platforms)


def validate_target_coverage(lock: DependencyLock) -> dict[str, tuple[str, ...]]:
    """Prove every active runtime pin has a wheel for each supported target."""

    coverage = {}
    for target in SUPPORTED_DEPENDENCY_TARGETS:
        covered = []
        for requirement in lock.requirements:
            if not marker_applies(
                requirement.pin,
                {
                    "platform_system": target.platform_system,
                    "python_version": target.python_version,
                },
            ):
                continue
            if not any(
                wheel_supports_target(artifact.filename, target)
                for artifact in requirement.artifacts
            ):
                raise DependencyLockError(
                    "dependency_lock_target_incomplete",
                    f"{requirement.pin.name} has no wheel for {target.identifier}",
                )
            covered.append(requirement.pin.normalized_name)
        coverage[target.identifier] = tuple(covered)
    return coverage


def validate_locked_artifact(artifact: LockedArtifact) -> None:
    if not isinstance(artifact.filename, str) or not _FILENAME_RE.fullmatch(artifact.filename):
        raise DependencyLockError(
            "dependency_lock_invalid",
            f"invalid locked artifact filename: {artifact.filename!r}",
        )
    if not isinstance(artifact.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact.sha256):
        raise DependencyLockError(
            "dependency_lock_invalid",
            f"invalid SHA-256 for locked artifact: {artifact.filename}",
        )
    if not isinstance(artifact.size, int) or isinstance(artifact.size, bool) or artifact.size <= 0:
        raise DependencyLockError(
            "dependency_lock_invalid",
            f"invalid size for locked artifact: {artifact.filename}",
        )


def _pins_identity(pins) -> tuple[tuple[str, str, str | None], ...]:
    return tuple((pin.normalized_name, pin.version, pin.marker) for pin in pins)


def parse_dependency_lock(text: str, source_text: str | None = None) -> DependencyLock:
    """Parse the generated lock and verify every artifact/hash association."""

    normalized = _normalized_text(text)
    lines = normalized.split("\n")
    schema_version = None
    source_sha256 = None
    options = []
    requirements = []
    requirement_names = set()
    pending_artifacts = []
    pending_filenames = set()
    index = 0
    allowed_comments = {
        "# Generated by python scripts/update_python_dependency_lock.py --apply.",
        "# Do not edit manually; update requirements.txt, then rerun that command.",
        "# Do not edit manually; update the matching source requirements file, then rerun that command.",
    }
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        line_number = index + 1
        if not stripped:
            index += 1
            continue
        if stripped in allowed_comments:
            if requirements or pending_artifacts:
                raise DependencyLockError(
                    "dependency_lock_invalid",
                    f"lock header comment appears after requirement blocks on line {line_number}",
                )
            index += 1
            continue
        match = _SCHEMA_RE.fullmatch(stripped)
        if match:
            if requirements or pending_artifacts:
                raise DependencyLockError(
                    "dependency_lock_invalid",
                    f"lock schema appears after requirement blocks on line {line_number}",
                )
            if schema_version is not None:
                raise DependencyLockError("dependency_lock_invalid", "duplicate lock schema header")
            schema_version = int(match.group("version"))
            index += 1
            continue
        match = _SOURCE_HASH_RE.fullmatch(stripped)
        if match:
            if requirements or pending_artifacts:
                raise DependencyLockError(
                    "dependency_lock_invalid",
                    f"source hash appears after requirement blocks on line {line_number}",
                )
            if source_sha256 is not None:
                raise DependencyLockError("dependency_lock_invalid", "duplicate source hash header")
            source_sha256 = match.group("sha256")
            index += 1
            continue
        if stripped.startswith("--"):
            if requirements or pending_artifacts:
                raise DependencyLockError(
                    "dependency_lock_invalid",
                    f"global option appears after requirement blocks on line {line_number}",
                )
            options.append(stripped)
            index += 1
            continue
        match = _ARTIFACT_RE.fullmatch(stripped)
        if match:
            artifact = LockedArtifact(
                filename=match.group("filename"),
                sha256=match.group("sha256"),
                size=int(match.group("size")),
            )
            validate_locked_artifact(artifact)
            filename_key = artifact.filename.casefold()
            if filename_key in pending_filenames:
                raise DependencyLockError(
                    "dependency_lock_invalid",
                    f"duplicate locked artifact filename on line {line_number}: {artifact.filename}",
                )
            pending_filenames.add(filename_key)
            pending_artifacts.append(artifact)
            index += 1
            continue
        if stripped.startswith("#"):
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"unknown lock comment on line {line_number}",
            )
        if line != line.lstrip():
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"orphaned hash or continuation on line {line_number}",
            )
        if not line.rstrip().endswith("\\"):
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"locked requirement must be followed by SHA-256 hashes on line {line_number}",
            )
        requirement_text = line.rstrip()[:-1].rstrip()
        parsed_pin = parse_source_requirements(requirement_text)
        if len(parsed_pin) != 1:
            raise DependencyLockError("dependency_lock_invalid", f"invalid requirement on line {line_number}")
        pin = parsed_pin[0]
        if pin.normalized_name in requirement_names:
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"duplicate locked requirement on line {line_number}: {pin.name}",
            )
        if not pending_artifacts:
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"locked requirement has no artifact inventory on line {line_number}: {pin.name}",
            )
        hashes = []
        expects_more_hashes = True
        index += 1
        while index < len(lines):
            hash_match = _HASH_LINE_RE.fullmatch(lines[index])
            if not hash_match:
                break
            hashes.append(hash_match.group("sha256"))
            continued = hash_match.group("continued") is not None
            expects_more_hashes = continued
            index += 1
            if not continued:
                break
        if not hashes:
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"locked requirement has no SHA-256 hash: {pin.name}",
            )
        if index < len(lines) and _HASH_LINE_RE.fullmatch(lines[index]):
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"hash continuation ended early for requirement: {pin.name}",
            )
        if expects_more_hashes:
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"hash continuation is incomplete for requirement: {pin.name}",
            )
        if Counter(hashes) != Counter(artifact.sha256 for artifact in pending_artifacts):
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"artifact inventory and pip hashes differ for requirement: {pin.name}",
            )
        requirement_names.add(pin.normalized_name)
        requirements.append(LockedRequirement(pin=pin, artifacts=tuple(pending_artifacts)))
        pending_artifacts = []
        pending_filenames = set()
    if pending_artifacts:
        raise DependencyLockError("dependency_lock_invalid", "orphaned artifact inventory at end of lock")
    if schema_version != LOCK_SCHEMA_VERSION:
        raise DependencyLockError(
            "dependency_lock_schema_unsupported",
            f"dependency lock must use schema {LOCK_SCHEMA_VERSION}",
        )
    if source_sha256 is None:
        raise DependencyLockError("dependency_lock_invalid", "dependency lock is missing source SHA-256")
    if options != [REQUIRE_HASHES_OPTION, BINARY_ONLY_OPTION]:
        raise DependencyLockError(
            "dependency_lock_invalid",
            "dependency lock must contain only the required hash and binary policy options",
        )
    if not requirements:
        raise DependencyLockError("dependency_lock_invalid", "dependency lock has no requirements")
    if source_text is not None:
        source_pins = parse_source_requirements(source_text)
        if source_sha256 != _text_sha256(source_text):
            raise DependencyLockError(
                "dependency_lock_stale",
                "dependency lock source SHA-256 does not match requirements.txt",
            )
        if _pins_identity(source_pins) != _pins_identity(item.pin for item in requirements):
            raise DependencyLockError(
                "dependency_lock_stale",
                "dependency lock requirements do not match requirements.txt",
            )
    return DependencyLock(
        schema_version=schema_version,
        source_sha256=source_sha256,
        requirements=tuple(requirements),
        lock_sha256=_text_sha256(normalized),
        text=normalized,
    )


def render_dependency_lock(source_text: str, artifacts_by_name) -> str:
    """Render a deterministic pip lock from exact pins and verified artifacts."""

    pins = parse_source_requirements(source_text)
    normalized_artifacts = {
        normalize_project_name(str(name)): tuple(artifacts)
        for name, artifacts in artifacts_by_name.items()
    }
    expected_names = {pin.normalized_name for pin in pins}
    if set(normalized_artifacts) != expected_names:
        missing = sorted(expected_names - set(normalized_artifacts))
        extra = sorted(set(normalized_artifacts) - expected_names)
        raise DependencyLockError(
            "dependency_lock_invalid",
            f"artifact mapping does not match runtime pins (missing={missing}, extra={extra})",
        )
    lines = [
        "# Generated by python scripts/update_python_dependency_lock.py --apply.",
        "# Do not edit manually; update the matching source requirements file, then rerun that command.",
        f"# probhub-lock-schema: {LOCK_SCHEMA_VERSION}",
        f"# source-sha256: {_text_sha256(source_text)}",
        REQUIRE_HASHES_OPTION,
        BINARY_ONLY_OPTION,
        "",
    ]
    for pin in pins:
        artifacts = sorted(
            normalized_artifacts[pin.normalized_name],
            key=lambda artifact: (artifact.filename.casefold(), artifact.filename),
        )
        if not artifacts:
            raise DependencyLockError(
                "dependency_lock_invalid",
                f"runtime requirement has no locked artifacts: {pin.name}",
            )
        filenames = set()
        for artifact in artifacts:
            validate_locked_artifact(artifact)
            filename_key = artifact.filename.casefold()
            if filename_key in filenames:
                raise DependencyLockError(
                    "dependency_lock_invalid",
                    f"duplicate artifact filename for {pin.name}: {artifact.filename}",
                )
            filenames.add(filename_key)
            lines.append(
                f"# artifact: {artifact.filename} sha256:{artifact.sha256} size:{artifact.size}"
            )
        lines.append(f"{pin.text} \\")
        for artifact_index, artifact in enumerate(artifacts):
            suffix = " \\" if artifact_index + 1 < len(artifacts) else ""
            lines.append(f"    --hash=sha256:{artifact.sha256}{suffix}")
        lines.append("")
    rendered = "\n".join(lines).rstrip("\n") + "\n"
    parse_dependency_lock(rendered, source_text=source_text)
    return rendered


def load_dependency_lock(lock_path, source_path=None) -> DependencyLock:
    lock_path = Path(lock_path)
    try:
        lock_text = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DependencyLockError(
            "dependency_lock_missing",
            f"cannot read dependency lock {lock_path}: {exc}",
        ) from exc
    source_text = None
    if source_path is not None:
        source_path = Path(source_path)
        try:
            source_text = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DependencyLockError(
                "dependency_source_missing",
                f"cannot read dependency source {source_path}: {exc}",
            ) from exc
    return parse_dependency_lock(lock_text, source_text=source_text)


def active_locked_requirements(lock: DependencyLock, environment=None) -> tuple[LockedRequirement, ...]:
    return tuple(
        requirement
        for requirement in lock.requirements
        if marker_applies(requirement.pin, environment)
    )


def verify_wheelhouse(lock: DependencyLock, wheelhouse, environment=None) -> dict:
    """Verify staged wheel bytes before the installer touches site-packages."""

    wheelhouse = Path(wheelhouse)
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise DependencyLockError(
            "dependency_wheelhouse_invalid",
            f"wheelhouse is not a regular directory: {wheelhouse}",
        )
    active = active_locked_requirements(lock, environment)
    expected = {
        artifact.filename.casefold(): artifact
        for requirement in active
        for artifact in requirement.artifacts
    }
    seen = set()
    for entry in wheelhouse.iterdir():
        if entry.is_symlink() or not entry.is_file() or entry.suffix.lower() != ".whl":
            raise DependencyLockError(
                "dependency_wheelhouse_invalid",
                f"wheelhouse contains a non-wheel entry: {entry.name}",
            )
        key = entry.name.casefold()
        artifact = expected.get(key)
        if artifact is None:
            raise DependencyLockError(
                "dependency_wheelhouse_invalid",
                f"wheelhouse contains an unlocked wheel: {entry.name}",
            )
        if key in seen:
            raise DependencyLockError(
                "dependency_wheelhouse_invalid",
                f"wheelhouse contains duplicate wheel identity: {entry.name}",
            )
        try:
            size = entry.stat().st_size
        except OSError as exc:
            raise DependencyLockError(
                "dependency_wheelhouse_invalid",
                f"cannot inspect wheel {entry.name}: {exc}",
            ) from exc
        if size != artifact.size:
            raise DependencyLockError(
                "dependency_wheel_hash_mismatch",
                f"wheel size mismatch for {entry.name}: expected {artifact.size}, got {size}",
            )
        digest = hashlib.sha256()
        try:
            with entry.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise DependencyLockError(
                "dependency_wheelhouse_invalid",
                f"cannot read wheel {entry.name}: {exc}",
            ) from exc
        actual = digest.hexdigest()
        if actual != artifact.sha256:
            raise DependencyLockError(
                "dependency_wheel_hash_mismatch",
                f"wheel SHA-256 mismatch for {entry.name}: expected {artifact.sha256}, got {actual}",
            )
        seen.add(key)

    missing = []
    selected = {}
    for requirement in active:
        matches = [
            artifact.filename
            for artifact in requirement.artifacts
            if artifact.filename.casefold() in seen
        ]
        if len(matches) != 1:
            missing.append(requirement.pin.normalized_name)
        else:
            selected[requirement.pin.normalized_name] = matches[0]
    if missing:
        raise DependencyLockError(
            "dependency_wheelhouse_incomplete",
            "wheelhouse must contain exactly one locked wheel for each active requirement; "
            f"missing or ambiguous: {', '.join(sorted(missing))}",
        )
    return {
        "ok": True,
        "files": len(seen),
        "requirements": len(active),
        "selected": selected,
    }
