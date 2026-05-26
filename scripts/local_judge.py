import sys
import subprocess
import os
import time

def compile_cpp(src_file, out_bin):
    if not os.path.exists(src_file): return False
    print(f"[*] 正在编译 {os.path.basename(src_file)}...")
    try:
        subprocess.run(["g++", src_file, "-o", out_bin, "-O2", "-std=c++17", "-I", "../../references"], 
                       check=True, stderr=subprocess.PIPE, text=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[-] 编译失败:\n{e.stderr}")
        return False

def run_testcase(bin_path, in_file, ans_file, time_limit=2.0):
    """运行测试并返回 (状态, 耗时)"""
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
    
    # 极简 diff (忽略行末空格)
    with open(ans_file, "r") as fa, open(temp_out, "r") as ft:
        if fa.read().strip() == ft.read().strip():
            return "AC", run_time
        else:
            return "WA", run_time

def run_validator(val_bin, in_file):
    """使用 testlib 验证输入格式"""
    try:
        with open(in_file, "r") as fin:
            subprocess.run([val_bin], stdin=fin, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/local_judge.py <问题目录>")
        sys.exit(1)

    prob_dir = sys.argv[1]
    data_dir = os.path.join(prob_dir, "data", "secret")
    in_files = sorted([f for f in os.listdir(data_dir) if f.endswith(".in")])
    
    print("\n" + "="*50)
    print("ProbHub 验题沙箱启动")
    print("="*50)

    # 1. 编译并执行 Validator (严格拦截非法数据)
    val_cpp = os.path.join(prob_dir, "validator.cpp")
    val_bin = os.path.join(prob_dir, "validator.exe")
    if os.path.exists(val_cpp):
        if compile_cpp(val_cpp, val_bin):
            print("\n开始执行 Validator 校验...")
            for inf in in_files:
                if not run_validator(val_bin, os.path.join(data_dir, inf)):
                    print(f"[-] 致命错误：数据 {inf} 违反了 Validator 约束！请修改数据生成器！")
                    sys.exit(1)
            print("[+] 数据格式全量校验通过！(All Valid)")

    # 2. 搜集所有代码并编译
    solutions = {"std": [], "brute": [], "wrong": []}
    for file in os.listdir(prob_dir):
        if file.endswith(".cpp") and file != "validator.cpp":
            for kind in solutions.keys():
                if file.startswith(kind):
                    bin_name = os.path.join(prob_dir, file.replace(".cpp", ".exe"))
                    if compile_cpp(os.path.join(prob_dir, file), bin_name):
                        solutions[kind].append((file, bin_name))

    if not solutions["std"]:
        print("[-] 找不到标程 std.cpp，沙箱终止。")
        sys.exit(1)

    # 3. 评测主逻辑
    for kind, progs in solutions.items():
        for prog_name, bin_path in progs:
            print(f"\n评测程序: {prog_name}")
            stats = {"AC": 0, "WA": 0, "TLE": 0, "RE": 0}
            
            for in_name in in_files:
                base_name = in_name[:-3]
                in_path = os.path.join(data_dir, in_name)
                ans_path = os.path.join(data_dir, f"{base_name}.ans")
                
                status, t = run_testcase(bin_path, in_path, ans_path)
                stats[status] += 1
                if status != "AC" or kind == "std": # 只有标程或者出错时才详细打印
                    print(f"  - {base_name.ljust(5)} : {status} ({t:.3f}s)")
            
            print(f"  汇总: {stats}")

            # 宿命断言校验
            if kind == "std" and stats["AC"] != len(in_files):
                print("[-] 标程未 100% AC！请立即修复。")
                sys.exit(1)
            elif kind == "brute":
                if stats["WA"] > 0:
                    print("[-] 暴力解法出现 WA！暴力算法写错，或标程/数据有逻辑错误！")
                    sys.exit(1)
                if stats["TLE"] == 0:
                    print("[-] 警告：暴力解法全部通过！数据强度过弱，未能卡住暴力，请增大极限数据！")
                    sys.exit(1)
            elif kind == "wrong":
                if stats["AC"] == len(in_files):
                    print("[-] 警告：错解程序全部通过！请构造特殊的 Corner Case 把它卡掉！")
                    sys.exit(1)

    print("\n" + "="*50)
    print("恭喜！所有代码均符合预期宿命")
    print("="*50)