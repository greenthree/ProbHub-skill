import re
from pathlib import Path

from .io import write_json
from .statement import parse_statement


def natural_key(path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(path))]


def read_samples(problem_dir, config):
    sample_dir = problem_dir / ((config.get("data") or {}).get("sample_dir", "data/sample"))
    samples = []
    if not sample_dir.is_dir():
        return samples
    for input_path in sorted(sample_dir.glob("*.in"), key=natural_key):
        answer_path = input_path.with_suffix(".ans")
        if answer_path.is_file():
            samples.append({
                "input": input_path.read_text(encoding="utf-8").rstrip("\r\n"),
                "output": answer_path.read_text(encoding="utf-8").rstrip("\r\n"),
            })
    return samples


def build_meta(problem_dir, config):
    source = (config.get("statement") or {}).get("source", "problem.md")
    parsed = parse_statement(problem_dir / source)
    limits = config.get("limits") or {}
    problem = {
        "display_name": config.get("display_name") or config.get("name"),
        "format": "markdown",
        "samples": read_samples(problem_dir, config),
        "time_limit": limits.get("time", 1),
        "memory_limit": limits.get("memory", 256),
    }
    if config.get("difficulty") is not None:
        problem["difficulty"] = config["difficulty"]
    if config.get("tags"):
        problem["tags"] = config["tags"]
    statement = dict(parsed["sections"])
    quote = (config.get("statement") or {}).get("quote")
    if isinstance(quote, dict):
        statement["quote"] = {
            "text": str(quote.get("text", "")),
            "source": str(quote.get("source", "")),
        }
    return {
        "name": config.get("name") or config.get("display_name"),
        "problem": problem,
        "statement": statement,
    }


def write_problem_meta(problem_dir, config):
    meta = build_meta(problem_dir, config)
    write_json(problem_dir / "meta.json", meta)
    return meta


def write_typst_collection(root, workspace, loaded_problems):
    typst = workspace.get("typst") or {}
    typst_dir = root / typst.get("directory", "typst-statement/正式赛")
    problems = []
    for problem_dir, config in loaded_problems:
        meta = write_problem_meta(problem_dir, config)
        problems.append(meta)
    write_json(typst_dir / "problems.json", problems)
    return typst_dir, problems
