import sys
import subprocess
import os
import time
import platform

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REFERENCES_DIR = os.path.join(SCRIPT_DIR, "..", "references")


def compile_cpp(src_file, out_bin):
    if not os.path.exists(src_file):
        return False
    print(f"[*] Compiling {os.path.basename(src_file)}...")
    flags = ["g++", src_file, "-o", out_bin, "-O2", "-std=c++17",
             "-I", REFERENCES_DIR]
    if platform.system() == "Windows":
        flags.insert(4, "-static")
    try:
        ret = subprocess.run(flags, capture_output=True, text=True)
        if ret.returncode != 0:
            print(f"[-] Compile failed:\n{ret.stderr}")
            return False
        return True
    except Exception as e:
        print(f"[-] Compile error: {e}")
        return False


def run_testcase(bin_path, in_file, ans_file, time_limit=2.0):
    """Run a test case and return (status, elapsed_time)."""
    temp_out = "temp.out"
    start = time.time()
    try:
        with open(in_file, "r") as fin, open(temp_out, "w") as fout:
            subprocess.run([bin_path], stdin=fin, stdout=fout, stderr=subprocess.PIPE,
                           timeout=time_limit, check=True)
    except subprocess.TimeoutExpired:
        return "TLE", time_limit
    except subprocess.CalledProcessError:
        return "RE", time.time() - start

    run_time = time.time() - start

    # Simple diff (ignoring trailing whitespace)
    with open(ans_file, "r") as fa, open(temp_out, "r") as ft:
        if fa.read().strip() == ft.read().strip():
            return "AC", run_time
        return "WA", run_time


def run_validator(val_bin, in_file):
    """Validate input format using testlib-style exit codes."""
    try:
        with open(in_file, "r") as fin:
            subprocess.run([val_bin], stdin=fin, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def collect_testcases(prob_dir):
    """Collect sample and secret test cases as (group, name, in_path, ans_path)."""
    cases = []
    for group in ("sample", "secret"):
        data_dir = os.path.join(prob_dir, "data", group)
        if not os.path.isdir(data_dir):
            print(f"[-] FATAL: missing data/{group} directory.")
            sys.exit(1)

        in_files = sorted(f for f in os.listdir(data_dir) if f.endswith(".in"))
        if not in_files:
            print(f"[-] FATAL: no .in files found in data/{group}.")
            sys.exit(1)

        for in_name in in_files:
            base_name = in_name[:-3]
            in_path = os.path.join(data_dir, in_name)
            ans_path = os.path.join(data_dir, f"{base_name}.ans")
            if not os.path.exists(ans_path):
                print(f"[-] FATAL: missing answer file for data/{group}/{in_name}.")
                sys.exit(1)
            cases.append((group, base_name, in_path, ans_path))
    return cases


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/local_judge.py <problem_dir>")
        sys.exit(1)

    prob_dir = sys.argv[1]
    testcases = collect_testcases(prob_dir)

    print("\n" + "="*50)
    print("ProbHub Validation Sandbox")
    print("="*50)

    # 1. Compile and run Validator
    val_cpp = os.path.join(prob_dir, "validator.cpp")
    val_bin = os.path.join(prob_dir, "validator.exe")
    if os.path.exists(val_cpp):
        if compile_cpp(val_cpp, val_bin):
            print("\nRunning validator...")
            for group, base_name, in_path, _ in testcases:
                if not run_validator(val_bin, in_path):
                    print(f"[-] FATAL: data/{group}/{base_name}.in violates validator constraints! Fix the data generator.")
                    sys.exit(1)
            print("[+] Validator: ALL PASS")

    # 2. Discover and compile all solutions
    solutions = {"std": [], "brute": [], "wrong": []}
    for file in os.listdir(prob_dir):
        if file.endswith(".cpp") and file != "validator.cpp":
            for kind in solutions.keys():
                if file.startswith(kind):
                    bin_name = os.path.join(prob_dir, file.replace(".cpp", ".exe"))
                    if compile_cpp(os.path.join(prob_dir, file), bin_name):
                        solutions[kind].append((file, bin_name))

    if not solutions["std"]:
        print("[-] std.cpp not found. Aborting.")
        sys.exit(1)

    # 3. Evaluate all solutions
    for kind, progs in solutions.items():
        for prog_name, bin_path in progs:
            print(f"\nEvaluating: {prog_name}")
            stats = {"AC": 0, "WA": 0, "TLE": 0, "RE": 0}

            for group, base_name, in_path, ans_path in testcases:
                status, t = run_testcase(bin_path, in_path, ans_path)
                stats[status] += 1
                if status != "AC" or kind == "std":
                    case_name = f"{group}/{base_name}"
                    print(f"  - {case_name.ljust(12)} : {status} ({t:.3f}s)")

            print(f"  Summary: {stats}")

            # Fate assertions
            if kind == "std" and stats["AC"] != len(testcases):
                print("[-] std not 100% AC! Fix the standard solution immediately.")
                sys.exit(1)
            elif kind == "brute":
                if stats["WA"] > 0:
                    print("[-] Brute force has WA! Fix the brute or std logic.")
                    sys.exit(1)
                if stats["TLE"] == 0:
                    print("[-] WARNING: Brute force passes all tests without TLE. Data is too weak!")
                    sys.exit(1)
            elif kind == "wrong":
                if stats["AC"] == len(testcases):
                    print("[-] WARNING: Wrong solution passes all tests! Add corner cases to break it.")
                    sys.exit(1)

    print("\n" + "="*50)
    print("[+] 恭喜！所有代码均符合预期宿命")
    print("="*50)


if __name__ == "__main__":
    main()
