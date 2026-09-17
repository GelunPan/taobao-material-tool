# -*- coding: utf-8 -*-
"""构建增量更新包：只打包改动的文件 + 打 updater.exe。

用法：
  python build_update.py
输出：
  dist/淘宝评价工具_更新器_V1.2.exe  （内嵌 update.zip，双击即更新）

工作原理：
  1. 先 build_app() 打新主程序到 dist/淘宝评价工具/
  2. 和上一版 dist/淘宝评价工具_prev/ 对比（首次无 prev 则全量）
  3. 把改动文件打成 update.zip
  4. 打 updater.exe 内嵌 update.zip
  5. 更新完成后把 dist/淘宝评价工具/ 复制为 _prev 供下次对比
"""
import os
import sys
import zipfile
import shutil
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_app import build_app, APP_NAME
from build_installer import build_installer_exe  # 复用 PyInstaller 调用方式

DIST_APP = os.path.join(ROOT, "dist", APP_NAME)
PREV_APP = os.path.join(ROOT, "dist", APP_NAME + "_prev")
UPDATE_ZIP = os.path.join(ROOT, "installer", "update.zip")
UPDATE_EXE = os.path.join(ROOT, "dist", "淘宝评价工具_更新器.exe")
SKIP_TOP = {"data"}


def collect_changed_files(new_dir: str, old_dir: str) -> list:
    """对比新旧目录，返回需要更新的相对路径列表。
    新文件、大小不同的文件都算改动。"""
    changed = []
    old_set = {}
    if os.path.isdir(old_dir):
        for root, _, files in os.walk(old_dir):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, old_dir)
                old_set[rel.replace("\\", "/")] = os.path.getsize(full)

    for root, _, files in os.walk(new_dir):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, new_dir).replace("\\", "/")
            top = rel.split("/")[0]
            if top in SKIP_TOP:
                continue
            size = os.path.getsize(full)
            if rel not in old_set or old_set[rel] != size:
                changed.append(rel)
    return changed


def make_update_zip(changed: list, new_dir: str, out_zip: str):
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in changed:
            src = os.path.join(new_dir, rel.replace("/", os.sep))
            zf.write(src, rel)


def build_updater():
    """用 PyInstaller 打 updater.exe，内嵌 update.zip。"""
    import subprocess
    spec_name = "tb_updater"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", spec_name,
        "--windowed", "--onefile",
        "--icon", os.path.join(ROOT, "app", "assets", "app_icon.png"),
        "--add-data", f"{UPDATE_ZIP}{os.pathsep}.",
        "--add-data", os.path.join(ROOT, "app", "assets", "app_icon.png") + os.pathsep + "app/assets",
        "--distpath", os.path.join(ROOT, "dist", "_upd_tmp"),
        "--workpath", os.path.join(ROOT, "dist", "_upd_tmp"),
        "--noconfirm",
        os.path.join(ROOT, "installer", "updater.py"),
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    src = os.path.join(ROOT, "dist", "_upd_tmp", spec_name + ".exe")
    shutil.copy2(src, UPDATE_EXE)
    shutil.rmtree(os.path.join(ROOT, "dist", "_upd_tmp"), ignore_errors=True)
    spec_file = os.path.join(ROOT, spec_name + ".spec")
    if os.path.isfile(spec_file):
        os.remove(spec_file)


def main():
    print("1/4 构建新主程序…")
    build_app()

    print("2/4 对比改动文件…")
    changed = collect_changed_files(DIST_APP, PREV_APP)
    print(f"   共 {len(changed)} 个文件需要更新")

    print("3/4 打包 update.zip…")
    if os.path.isfile(UPDATE_ZIP):
        os.remove(UPDATE_ZIP)
    make_update_zip(changed, DIST_APP, UPDATE_ZIP)

    print("4/4 打更新器 exe…")
    build_updater()

    # 更新 prev 快照
    if os.path.isdir(PREV_APP):
        shutil.rmtree(PREV_APP, ignore_errors=True)
    shutil.copytree(DIST_APP, PREV_APP, dirs_exist_ok=True)

    size_mb = os.path.getsize(UPDATE_EXE) / 1024 / 1024
    print("=" * 60)
    print(f"更新包：{UPDATE_EXE}  ({size_mb:.1f} MB)")
    print(f"包含 {len(changed)} 个改动文件")


if __name__ == "__main__":
    main()
