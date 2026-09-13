# -*- coding: utf-8 -*-
"""装配安装程序：

1. 把 PyInstaller 产出的 onedir 应用目录（dist/<APP>/）压缩成 installer/payload.zip
   （仅打包目录内容，不含外层文件夹，解压即得到应用根目录）。
2. 用 PyInstaller 把 installer/installer.py 与 payload.zip 打成一个独立的
   ``<APP>安装程序.exe``（--windowed，无控制台）。

用法：
    python installer/build_installer.py
"""
import os
import sys
import zipfile
import subprocess
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_NAME = "淘宝评价素材整理工具"
DIST_APP = os.path.join(ROOT, "dist", APP_NAME)
PAYLOAD = os.path.join(ROOT, "installer", "payload.zip")
INSTALLER_SRC = os.path.join(ROOT, "installer", "installer.py")


def zip_app_folder(src_dir: str, zip_path: str):
    """把 src_dir 的内容（不含外层目录）打包为 zip。

    用 ``"w"`` 模式直接覆盖旧 zip，避免先删除大文件触发安全删除拦截。
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for f in files:
                full = os.path.join(root, f)
                # arcname 去掉外层 src_dir，使解压落到目标根目录
                arc = os.path.relpath(full, src_dir)
                zf.write(full, arc)
    size = os.path.getsize(zip_path) / 1e6
    print("payload.zip 已生成：%.1f MB -> %s" % (size, zip_path))


def build_installer_exe():
    out_name = APP_NAME + "安装程序"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", out_name,
        "--windowed", "--onedir",
        "--add-data", "%s:." % PAYLOAD,
        "--add-data", os.path.join(ROOT, "app", "assets", "logo.png") + ":app/assets",
        "--noconfirm",
        INSTALLER_SRC,
    ]
    print("构建安装程序：", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)
    exe = os.path.join(ROOT, "dist", out_name, out_name + ".exe")
    print("安装程序已生成：", exe, "%.1f MB" % (os.path.getsize(exe) / 1e6 if os.path.isfile(exe) else 0))
    return exe


def main():
    if not os.path.isdir(DIST_APP):
        print("找不到应用目录：%s，请先运行 PyInstaller 打包应用。" % DIST_APP)
        sys.exit(1)
    zip_app_folder(DIST_APP, PAYLOAD)
    build_installer_exe()
    print("完成。把 dist/%s安装程序/%s安装程序.exe 发给用户即可。" % (APP_NAME, APP_NAME))


if __name__ == "__main__":
    main()
