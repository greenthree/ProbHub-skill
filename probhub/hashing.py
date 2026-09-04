import hashlib
from pathlib import Path

from .errors import ProbHubError
from .problem_paths import is_link_like


def hash_file(path):
    path = Path(path)
    if is_link_like(path) or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_paths(root, paths, *, normalize_lf_suffixes=(), check=None):
    root = Path(root)
    normalize_lf_suffixes = {
        str(suffix).lower() for suffix in normalize_lf_suffixes
    }
    digest = hashlib.sha256()
    existing = []
    for path in sorted(paths, key=lambda item: Path(item).as_posix()):
        if check is not None:
            check()
        full = root / path
        if is_link_like(full):
            raise ProbHubError(
                f"hashed file must not be a symlink or reparse point: {full}",
                code="unsafe_data_source",
            )
        if not full.is_file():
            continue
        rel = Path(path).as_posix().encode("utf-8")
        if full.suffix.lower() not in normalize_lf_suffixes:
            expected_size = full.stat().st_size
            digest.update(len(rel).to_bytes(4, "big"))
            digest.update(rel)
            digest.update(expected_size.to_bytes(8, "big"))
            observed_size = 0
            with full.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    if check is not None:
                        check()
                    observed_size += len(chunk)
                    digest.update(chunk)
            if observed_size != expected_size:
                raise OSError(f"file changed while hashing: {full}")
            existing.append(full)
            continue
        content = full.read_bytes()
        content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        existing.append(full)
    return digest.hexdigest(), existing


def files_under(path, suffixes=None, *, check=None):
    path = Path(path)
    if is_link_like(path):
        raise ProbHubError(
            f"data directory must not be a symlink or reparse point: {path}",
            code="unsafe_data_source",
        )
    if not path.exists():
        return []
    result = []
    for item in path.rglob("*"):
        if check is not None:
            check()
        if is_link_like(item):
            raise ProbHubError(
                f"data file or directory must not be a symlink or reparse point: {item}",
                code="unsafe_data_source",
            )
        if item.is_file() and (suffixes is None or item.suffix in suffixes):
            result.append(item)
    return result
