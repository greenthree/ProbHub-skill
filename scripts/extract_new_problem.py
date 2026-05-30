# -*- coding: utf-8 -*-
import sys
import os
import json
import subprocess
from pypdf import PdfReader, PdfWriter

def main():
    if len(sys.argv) != 3:
        print("Usage: python scripts/extract_new_problem.py <typst_dir> <problem_dir>")
        sys.exit(1)

    typst_dir = sys.argv[1]    # 例如: typst-statement/热身赛
    problem_dir = sys.argv[2]  # 例如: balance

    main_typ_path = os.path.join(typst_dir, "main.typ")
    main_pdf_path = os.path.join(typst_dir, "main.pdf")
    output_pdf_path = os.path.join(problem_dir, "problem.pdf")

    # 1. 编译生成完整的 PDF
    print(f"[*] 正在编译全卷 PDF: {main_typ_path} ...")
    try:
        subprocess.run(
            ["typst", "compile", "--root", ".", main_typ_path, main_pdf_path], 
            check=True, encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        print("[-] 致命错误：Typst 编译失败！请检查 Typst 语法。")
        sys.exit(1)

    # 2. 查询所有题目的页码边界
    print("[*] 正在向 Typst 探针查询题目物理页码...")
    try:
        result = subprocess.run(
            ["typst", "query", "--root", ".", main_typ_path, "<prob-boundary>"],
            check=True, capture_output=True, text=True, encoding="utf-8"
        )
        boundaries = json.loads(result.stdout)
    except Exception as e:
        print(f"[-] 页码查询失败: {e}")
        sys.exit(1)

    if not boundaries:
        print("[-] 未检测到题目信标！请确保 lib.typ 中已埋入 metadata。")
        sys.exit(1)

    # 3. 读取本题的 meta.json，获取真实的题目名称 (对暗号)
    meta_path = os.path.join(problem_dir, "meta.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            target_name = json.load(f)["problem"]["display_name"]
    except Exception as e:
        print(f"[-] 无法读取当前题目的 meta.json: {e}")
        sys.exit(1)

    # 4. 精准匹配目标题目的 Start 和 End 页码
    boundaries.sort(key=lambda x: x["location"]["page"])
    start_page = -1
    end_page = -1

    for i, b in enumerate(boundaries):
        if b.get("value") == target_name:  # 暗号匹配成功！
            start_page = b["location"]["page"]
            # 如果后面还有题，下一题的起始页就是本题的终止页
            if i + 1 < len(boundaries):
                end_page = boundaries[i+1]["location"]["page"]
            break

    if start_page == -1:
        print(f"[-] 在 PDF 信标中未找到题目: '{target_name}'。可能未加入 problems.json")
        sys.exit(1)

    # 5. 物理裁剪 PDF
    reader = PdfReader(main_pdf_path)
    total_pages = len(reader.pages)
    
    # 如果是最后一题，切到卷尾
    if end_page == -1:
        end_page = total_pages + 1

    writer = PdfWriter()
    
    # pypdf 索引从 0 开始，Typst 页码从 1 开始
    for page_num in range(start_page - 1, end_page - 1):
        writer.add_page(reader.pages[page_num])

    with open(output_pdf_path, "wb") as f:
        writer.write(f)

    page_count = (end_page - 1) - (start_page - 1)
    print(f"[+] 🎯 成功提取独立题面！(共 {page_count} 页, 截取自原卷 P{start_page} - P{end_page - 1})")

if __name__ == "__main__":
    main()