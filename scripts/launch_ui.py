# -*- coding: utf-8 -*-
"""ProbHub 后台启动器 — 在独立进程中启动 ui.py 并立即返回。

用法：
    python launch_ui.py

该脚本会以完全分离的后台进程启动 ui.py（Flask 服务器），
启动后浏览器将自动打开 http://127.0.0.1:33933。
本脚本会立即退出，不会阻塞调用方。
"""

import subprocess
import sys
import os
import time


def main():
    workspace = os.path.dirname(os.path.abspath(__file__))
    ui_script = os.path.join(workspace, "ui.py")

    if not os.path.exists(ui_script):
        print(f"\n❌ 未找到 {ui_script}")
        print("   请先将 ui.py 拷贝到工作区根目录。\n")
        sys.exit(1)

    # 用完全分离的子进程启动 Flask
    kwargs: dict = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        cwd=workspace,
    )

    if sys.platform == "win32":
        # Windows: 彻底脱离当前进程组
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        # macOS / Linux: start_new_session 实现 daemonize
        kwargs["start_new_session"] = True

    subprocess.Popen([sys.executable, ui_script], **kwargs)

    # 给 Flask 一点启动时间
    time.sleep(1.5)

    print()
    print("=" * 55)
    print("  🚀  ProbHub 可视化控制台已在后台启动！")
    print(f"  📂  工作区: {workspace}")
    print("  🌐  浏览器将自动打开 http://127.0.0.1:33933")
    print("  📌  关闭本终端不影响控制台运行。")
    print("=" * 55)
    print()


if __name__ == "__main__":
    main()
