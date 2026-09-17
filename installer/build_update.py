# -*- coding: utf-8 -*-
"""构建增量更新包。

输出：
  dist/tb_tools_update_Vx.x.zip  （发给客户的更新包，用户双击桌面「更新器」快捷方式自动找它）

以后更新：改完代码 → python build_update.py → 发 tb_tools_update_Vx.x.zip
"""
import os
import sys
import zipfile
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_app import build_app, APP_NAME
import re as _re
_cfg = open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read()
_m = _re.search(r'APP_VERSION\s*=\s*"([^"]+)"', _cfg)
APP_VERSION = _m.group(1) if _m else "0.0.0"

DIST_APP = os.path.join(ROOT, "dist", APP_NAME)
PREV_APP = os.path.join(ROOT, "dist", APP_NAME + "_prev")
UPDATER_EXE = os.path.join(ROOT, "installer", "更新器.exe")
SKIP_TOP = {"data"}
VERSION = APP_VERSION  # 从 config.py 读


def collect_changed(new_dir, old_dir):
    old_set = {}
    if os.path.isdir(old_dir):
        for root, _, files in os.walk(old_dir):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, old_dir).replace("\\", "/")
                old_set[rel] = os.path.getsize(full)
    changed = []
    for root, _, files in os.walk(new_dir):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, new_dir).replace("\\", "/")
            if rel.split("/")[0] in SKIP_TOP:
                continue
            size = os.path.getsize(full)
            if rel not in old_set or old_set[rel] != size:
                changed.append(rel)
    return changed


def build_updater_exe():
    """打更新器.exe（图标用 updater_icon.ico，只打一次）。"""
    if os.path.isfile(UPDATER_EXE):
        print("更新器.exe 已存在，跳过重打")
        return
    icon = os.path.join(ROOT, "app", "assets", "updater_icon.ico")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "更新器",
        "--windowed", "--onefile",
        "--icon", icon,
        "--distpath", os.path.join(ROOT, "dist", "_upd_build"),
        "--workpath", os.path.join(ROOT, "dist", "_upd_build"),
        "--noconfirm",
        os.path.join(ROOT, "installer", "updater.py"),
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    src = os.path.join(ROOT, "dist", "_upd_build", "更新器.exe")
    shutil.copy2(src, UPDATER_EXE)
    shutil.rmtree(os.path.join(ROOT, "dist", "_upd_build"), ignore_errors=True)
    spec = os.path.join(ROOT, "更新器.spec")
    if os.path.isfile(spec):
        os.remove(spec)


def main():
    print(f"当前版本：V{VERSION}")
    print("1/4 构建新主程序…")
    build_app()

    # 写 version.txt 到新主程序目录
    with open(os.path.join(DIST_APP, "version.txt"), "w", encoding="utf-8") as f:
        f.write(VERSION)

    print("2/4 对比改动文件…")
    changed = collect_changed(DIST_APP, PREV_APP)
    print(f"   共 {len(changed)} 个文件需要更新")

    print("3/4 打更新包…")
    out_zip = os.path.join(ROOT, "dist", f"tb_tools_update_V{VERSION}.zip")
    if os.path.isfile(out_zip):
        os.remove(out_zip)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        # 版本号
        zf.writestr("version.txt", VERSION)
        # 改动文件
        for rel in changed:
            src = os.path.join(DIST_APP, rel.replace("/", os.sep))
            zf.write(src, rel)

    print("4/4 准备更新器…")
    build_updater_exe()

    # 更新 prev 快照
    if os.path.isdir(PREV_APP):
        shutil.rmtree(PREV_APP, ignore_errors=True)
    shutil.copytree(DIST_APP, PREV_APP, dirs_exist_ok=True)

    size_mb = os.path.getsize(out_zip) / 1024 / 1024
    print("=" * 60)
    print(f"更新包：{out_zip}  ({size_mb:.1f} MB)")
    print(f"发给客户：把 {os.path.basename(out_zip)} 放到客户桌面即可")


if __name__ == "__main__":
    main()
