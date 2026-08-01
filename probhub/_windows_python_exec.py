import os
import runpy
import sys
from pathlib import Path


def _prepare_descendants(base_executable):
    prefix = Path(sys.prefix).resolve()
    venv_paths = []
    for entry in sys.path:
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
            resolved.relative_to(prefix)
        except (OSError, ValueError):
            continue
        value = str(resolved)
        if value not in venv_paths:
            venv_paths.append(value)
    existing = os.environ.get("PYTHONPATH")
    if existing:
        venv_paths.append(existing)
    if venv_paths:
        os.environ["PYTHONPATH"] = os.pathsep.join(venv_paths)

    # Descendants must execute the real interpreter directly. The root keeps
    # the venv prefix established before this helper was loaded.
    os.environ["__PYVENV_LAUNCHER__"] = base_executable
    sys.executable = base_executable
    sys._base_executable = base_executable


def _run(arguments):
    if not arguments:
        raise SystemExit("managed Python command is missing a script, -c, or -m target")
    target, *rest = arguments
    if target == "-c":
        if not rest:
            raise SystemExit("argument expected for -c")
        source, *script_args = rest
        sys.argv = ["-c", *script_args]
        if not sys.flags.safe_path:
            sys.path[0] = ""
        namespace = {
            "__name__": "__main__",
            "__package__": None,
            "__spec__": None,
            "__builtins__": __builtins__,
        }
        exec(compile(source, "<string>", "exec"), namespace, namespace)
        return
    if target == "-m":
        if not rest:
            raise SystemExit("argument expected for -m")
        module, *module_args = rest
        sys.argv = [module, *module_args]
        if not sys.flags.safe_path:
            sys.path[0] = ""
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return
    if target == "-":
        sys.argv = ["-", *rest]
        if not sys.flags.safe_path:
            sys.path[0] = ""
        source = sys.stdin.buffer.read()
        namespace = {
            "__name__": "__main__",
            "__package__": None,
            "__spec__": None,
            "__builtins__": __builtins__,
        }
        exec(compile(source, "<stdin>", "exec"), namespace, namespace)
        return
    if target.startswith("-"):
        raise SystemExit(f"unsupported managed Python option: {target}")

    script = Path(target)
    sys.argv = [str(script), *rest]
    sys.path[0] = str(script.resolve().parent)
    runpy.run_path(str(script), run_name="__main__")


def main():
    if len(sys.argv) < 3:
        raise SystemExit("Windows Python execution helper arguments are incomplete")
    base_executable = str(Path(sys.argv[1]).resolve())
    arguments = sys.argv[2:]
    _prepare_descendants(base_executable)
    _run(arguments)


if __name__ == "__main__":
    main()
