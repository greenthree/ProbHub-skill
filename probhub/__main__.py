import json
import sys

from . import __version__


def _bootstrap_doctor(argv):
    remaining = list(argv)
    json_output = False
    index = 0
    while index < len(remaining):
        value = remaining[index]
        if value == "--json":
            json_output = True
            remaining.pop(index)
            continue
        if value == "--workspace" and index + 1 < len(remaining):
            del remaining[index:index + 2]
            continue
        index += 1
    if remaining != ["doctor"]:
        return None

    # Keep doctor usable before optional Python dependencies are installed.
    from .doctor import run_doctor

    result = run_doctor()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", False) else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--version"]:
        print(__version__)
        return 0
    doctor_result = _bootstrap_doctor(argv)
    if doctor_result is not None:
        return doctor_result

    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
