"""Exercise literal venv redirectors from inside a real Windows virtualenv."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from probhub.process_control import ProcessCancelled, process_alive, run_managed_to_files


def main():
    assert Path(sys.prefix).resolve() == Path(os.environ["PROBHUB_TEST_VENV"]).resolve()
    root = Path(sys.argv[2])
    results = []
    for mode in ("timeout", "cancel", "completed"):
        marker = root / (mode + ".pid")
        child = (
            "import os,time; from pathlib import Path; "
            f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
        )
        # Embed the original venv executable, rather than reading the base
        # executable installed by the containment helper inside the parent.
        parent = (
            "import subprocess,time; from pathlib import Path\n"
            f"subprocess.Popen([{sys.executable!r},'-I','-S','-c',{child!r}])\n"
            f"while not Path({str(marker)!r}).exists(): time.sleep(0.01)\n"
        )
        if mode != "completed":
            parent += "time.sleep(60)\n"
        stdout, stderr = root / (mode + ".out"), root / (mode + ".err")
        try:
            result = run_managed_to_files(
                [sys.executable, "-I", "-S", "-c", parent],
                stdout_path=stdout,
                stderr_path=stderr,
                timeout=0.8 if mode == "timeout" else 5,
                memory_limit_mb=256,
                process_limit=8,
                output_limit_bytes=65536,
                cancel_check=lambda: mode == "cancel" and marker.exists(),
            )
            assert result["reason"] == ("time_limit" if mode == "timeout" else "completed"), result
            assert result["cleanup"]["ok"], result
        except ProcessCancelled:
            assert mode == "cancel"
        assert marker.is_file(), mode
        assert not process_alive(int(marker.read_text())), mode
        stdout.unlink()
        stderr.unlink()
        results.append(mode)
    print(json.dumps(results))


if __name__ == "__main__":
    main()
