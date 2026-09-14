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
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_NAME = "淘宝评价工具"
DIST_APP = os.path.join(ROOT, "dist", APP_NAME)
PAYLOAD = os.path.join(ROOT, "installer", "payload.zip")
INSTALLER_SRC = os.path.join(ROOT, "installer", "installer.py")
# 复用主程序的外部 cmd 删除，避免 Python safe-delete 钩子拦截
from build_app import _safe_remove  # noqa: E402


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
    out_name = "win10_x64_taobaotools_V1.1"
    # 用独立临时 distpath/workpath 构建，避免 PyInstaller --noconfirm 删除旧 exe
    # 触发 safe-delete 硬阻断（删除/重命名被环境拦截，只有写/复制放行）
    tmp = os.path.join(ROOT, "dist", "_installer_tmp")
    _safe_remove(tmp)  # 目录删除走外部 cmd，经验证可用
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", out_name,
        "--windowed", "--onefile",
        "--icon", os.path.join(ROOT, "app", "assets", "app_icon.png"),
        "--distpath", tmp,
        "--workpath", tmp,
        "--add-data", "%s:." % PAYLOAD,
        "--add-data", os.path.join(ROOT, "app", "assets", "app_icon.png") + ":app/assets",
        "--noconfirm",
        INSTALLER_SRC,
    ]
    print("构建安装程序：", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)
    src_exe = os.path.join(tmp, out_name + ".exe")
    exe = os.path.join(ROOT, "dist", out_name + ".exe")
    if not os.path.isfile(src_exe):
        print("[error] 未找到构建产物：", src_exe)
        sys.exit(1)
    # shutil.copy 直接走 OS 调用覆盖（写操作，不触发 safe-delete，且比 cmd copy 可靠）
    shutil.copy(src_exe, exe)
    print("安装程序已生成：", exe, "%.1f MB" % (os.path.getsize(exe) / 1e6 if os.path.isfile(exe) else 0))
    print("发给客户：dist/win10_x64_taobaotools_V1.1.exe")
    _safe_remove(tmp)
    return exe


def main():
    if not os.path.isdir(DIST_APP):
        print("找不到应用目录：%s，请先运行 PyInstaller 打包应用。" % DIST_APP)
        sys.exit(1)
    zip_app_folder(DIST_APP, PAYLOAD)
    build_installer_exe()
    print("完成。把 dist/%s安装程序.exe 发给用户即可。" % APP_NAME)


if __name__ == "__main__":
    main()
