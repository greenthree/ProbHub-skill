import hashlib
from pathlib import Path


def hash_file(path):
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_paths(root, paths):
    root = Path(root)
    digest = hashlib.sha256()
    existing = []
    for path in sorted({Path(p) for p in paths}, key=lambda p: p.as_posix()):
        full = path if path.is_absolute() else root / path
        if not full.is_file():
            continue
        rel = full.relative_to(root).as_posix().encode("utf-8")
        content = full.read_bytes()
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        existing.append(full)
    return digest.hexdigest(), existing


def files_under(path, suffixes=None):
    path = Path(path)
    if not path.exists():
        return []
    result = []
    for item in path.rglob("*"):
        if item.is_file() and (suffixes is None or item.suffix in suffixes):
            result.append(item)
    return result
