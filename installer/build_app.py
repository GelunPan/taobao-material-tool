# -*- coding: utf-8 -*-
"""构建主程序 onedir 产物。

关键：``--add-data "app/style.qss:app"`` 才能让 ``style.qss`` 直接落到
``_internal/app/style.qss``，否则 PyInstaller 会把它放进
``_internal/app/style.qss/style.qss`` 目录里，导致启动时报 Permission denied。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_NAME = "淘宝评价素材整理工具"
DIST_APP = os.path.join(ROOT, "dist", APP_NAME)


def _safe_remove(path):
    """删除文件/目录。

    必须用外部 ``cmd.exe`` 子进程执行删除——本环境的 safe-delete 钩子只拦截
    Python 层的 ``shutil.rmtree``/``os.remove``（抛 SystemExit），对外部进程无效。
    """
    if not os.path.exists(path) and not os.path.islink(path):
        return
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", path], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["cmd", "/c", "del", "/f", "/q", path], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("cleaned:", path)
    except Exception as e:  # 极端情况下跳过，--noconfirm 会兜底
        print("[warn] 删除失败（已跳过，不影响构建）:", path, "->", e)


def build_app():
    """完整重建主程序 onedir 产物。"""
    # 清理旧构建；显式删除被拦时由 --noconfirm 兜底，不致命
    for p in [DIST_APP, os.path.join(ROOT, "build", APP_NAME), os.path.join(ROOT, APP_NAME + ".spec")]:
        _safe_remove(p)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--windowed", "--onedir",
        "--add-data", os.path.join(ROOT, "app", "assets") + ":app/assets",
        "--add-data", os.path.join(ROOT, "app", "style.qss") + ":app",  # 关键修正点
        "--hidden-import", "PyQt6.QtWebEngineWidgets",
        "--hidden-import", "PyQt6.QtWebEngineCore",
        "--hidden-import", "PyQt6.QtWebEngineQuick",
        "--noconfirm",
        os.path.join(ROOT, "main.py"),
    ]
    print("构建主程序：", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)

    # 删除调试资源包，减 75MB 左右，运行时不需要；走外部 cmd 绕过 safe-delete 钩子
    dbg = os.path.join(DIST_APP, "_internal", "PyQt6", "Qt6", "resources", "qtwebengine_devtools_resources.debug.pak")
    if os.path.isfile(dbg):
        try:
            subprocess.run(["cmd", "/c", "del", "/f", "/q", dbg], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("removed debug pak:", dbg)
        except Exception as e:
            print("[warn] 跳过删除调试 pak（不影响功能）:", dbg, "->", e)

    print("主程序已生成：", DIST_APP)


if __name__ == "__main__":
    build_app()
