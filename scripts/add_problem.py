import json
import sys
import os

def main():
    if len(sys.argv) != 3:
        print("用法: python add_problem.py <主干problem.json路径> <新题目meta.json路径>")
        sys.exit(1)

    target_json_path = sys.argv[1]
    new_meta_path = sys.argv[2]

    # 1. 验证并读取新题目数据
    if not os.path.exists(new_meta_path):
        print(f"[!] 错误: 找不到新题目数据文件 {new_meta_path}")
        sys.exit(1)

    try:
        with open(new_meta_path, 'r', encoding='utf-8') as f:
            new_problem = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[!] 错误: {new_meta_path} 不是合法的 JSON 格式 - {e}")
        sys.exit(1)

    # 2. 读取主干 JSON 数据 (如果不存在则初始化为空列表)
    if os.path.exists(target_json_path):
        try:
            with open(target_json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError:
            print(f"[*] 警告: {target_json_path} 已损坏或为空，将其重置为空数组。")
            data = []
    else:
        print(f"[*] 提示: {target_json_path} 不存在，将自动创建。")
        data = []

    # 3. 结构兼容与追加逻辑
    # Typst 模板的 problem.json 通常是一个数组 (List)，但也有些模板包在一个字典里 {"problems": []}
    target_list = None
    if isinstance(data, list):
        target_list = data
    elif isinstance(data, dict) and "problems" in data:
        target_list = data["problems"]
    else:
        print("[!] 错误: 无法识别的 JSON 结构，期望是数组或包含 'problems' 键的字典。")
        sys.exit(1)

    # 简易查重：如果题目名已存在，先将其移除再追加（实现更新操作）
    problem_name = new_problem.get("name", "")
    if problem_name:
        original_length = len(target_list)
        target_list[:] = [p for p in target_list if p.get("name") != problem_name]
        if len(target_list) < original_length:
            print(f"[*] 提示: 检测到同名题目 '{problem_name}'，将执行覆盖更新。")

    target_list.append(new_problem)

    # 4. 安全写回
    try:
        # 使用临时文件写入，成功后再替换，防止写入中途崩溃导致主文件损坏
        temp_path = target_json_path + ".tmp"
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        
        os.replace(temp_path, target_json_path)
        print(f"[+] 成功将新题目追加到: {target_json_path}")
    except Exception as e:
        print(f"[!] 错误: 写入主文件失败 - {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()