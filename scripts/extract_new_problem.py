import sys
import os
import subprocess
from pypdf import PdfReader, PdfWriter

def get_page_count(pdf_path):
    """读取 PDF 页数，如果文件不存在则返回 0"""
    if not os.path.exists(pdf_path):
        return 0
    try:
        reader = PdfReader(pdf_path)
        return len(reader.pages)
    except Exception as e:
        print(f"[!] 读取 {pdf_path} 失败: {e}")
        return 0

def main():
    if len(sys.argv) != 3:
        print("用法: python extract_new_problem.py <Typst目录路径> <题目输出目录路径>")
        sys.exit(1)

    typst_dir = sys.argv[1]
    problem_dir = sys.argv[2]
    
    main_typ_path = os.path.join(typst_dir, "main.typ")
    main_pdf_path = os.path.join(typst_dir, "main.pdf")
    output_pdf_path = os.path.join(problem_dir, "problem.pdf")

    # 1. 编译前：获取原始卷子的页数
    initial_pages = get_page_count(main_pdf_path)
    print(f"[*] 编译前主卷页数: {initial_pages}")

    # 2. 编译 Typst
    print(f"[*] 正在编译 {main_typ_path} ...")
    try:
        subprocess.run(["typst", "compile", "--root", "..", "main.typ"], cwd=typst_dir, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[!] Typst 编译失败，请检查 main.typ 或 JSON 语法。错误信息: {e}")
        sys.exit(1)

    # 3. 编译后：获取新卷子的页数
    new_pages = get_page_count(main_pdf_path)
    print(f"[*] 编译后主卷页数: {new_pages}")

    # 4. 计算新增题目的页数 x
    if initial_pages == 0:
        # 【核心修正】：说明是第一次编译（模板刚拷贝过来，没有历史 pdf）
        # 减去封面、注意事项等 Boilerplate 页数（默认为 2 页）
        boilerplate_pages = 2
        x = new_pages - boilerplate_pages
        print(f"[*] 检测为首次编译，去除前 {boilerplate_pages} 页模板页。")
    else:
        # 差值法：说明之前已经编译过题目了
        x = new_pages - initial_pages
    
    # 容错处理
    if x <= 0:
        print("[!] 警告: 题目页数计算 <= 0 (可能题面极短未分页，或重复编译)。")
        if new_pages > 2: # 如果总页数大于模板页，提取最后一页保底
            print("[*] 触发容错机制：默认提取最后一页。")
            x = 1
        else:
            print("[!] 错误: 编译后的 PDF 只有模板页，缺少题目内容。")
            sys.exit(1)

    print(f"[*] 最终确定要提取的题目页数 x = {x}")

    # 5. 提取并保存最后 x 页
    reader = PdfReader(main_pdf_path)
    writer = PdfWriter()

    start_index = new_pages - x
    for i in range(start_index, new_pages):
        writer.add_page(reader.pages[i])

    os.makedirs(problem_dir, exist_ok=True)
    
    with open(output_pdf_path, "wb") as f:
        writer.write(f)

    print(f"[+] 成功提取最后 {x} 页并保存至: {output_pdf_path}")

if __name__ == "__main__":
    main()