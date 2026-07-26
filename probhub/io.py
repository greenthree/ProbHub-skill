import json
import os
import uuid
from pathlib import Path

import yaml


def read_yaml(path):
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    return data or {}


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_yaml(path, data):
    _atomic_write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000))


def write_json(path, data):
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def atomic_write_json(path, data):
    write_json(path, data)
