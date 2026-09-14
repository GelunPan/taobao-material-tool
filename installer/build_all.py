# -*- coding: utf-8 -*-
"""一键构建完整安装程序：先打主程序 onedir，再打单文件安装器。"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_app import build_app, APP_NAME
from build_installer import build_installer_exe, zip_app_folder

DIST_APP = os.path.join(ROOT, "dist", APP_NAME)
PAYLOAD = os.path.join(ROOT, "installer", "payload.zip")


def main():
    build_app()
    # 先把 onedir 主程序打成 payload.zip，安装器才能内嵌
    zip_app_folder(DIST_APP, PAYLOAD)
    build_installer_exe()
    print("=" * 60)
    print("全部完成。最终产物：dist/win10_x64_taobaotools_V1.1.exe")


if __name__ == "__main__":
    main()
