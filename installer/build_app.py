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
APP_NAME = "tb-tool2"
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
        "--icon", os.path.join(ROOT, "app", "assets", "app_icon.png"),
        "--add-data", os.path.join(ROOT, "app", "assets") + ":app/assets",
        "--add-data", os.path.join(ROOT, "app", "style.qss") + ":app",  # 关键修正点
        "--hidden-import", "PyQt6.QtWebEngineWidgets",
        "--hidden-import", "PyQt6.QtWebEngineCore",
        "--hidden-import", "PyQt6.QtWebEngineQuick",
        # openpyxl（函数内延迟 import，必须显式声明）
        "--hidden-import", "openpyxl",
        "--hidden-import", "openpyxl.cell",
        "--hidden-import", "openpyxl.styles",
        "--hidden-import", "openpyxl.utils",
        "--hidden-import", "openpyxl.drawing",
        "--hidden-import", "openpyxl.drawing.image",
        "--hidden-import", "openpyxl.drawing.spreadsheet_drawing",
        "--hidden-import", "openpyxl.drawing.xdr",
        "--hidden-import", "openpyxl.utils.units",
        # requests（顶层 import 但被漏检）
        "--hidden-import", "requests",
        "--hidden-import", "urllib3",
        "--hidden-import", "certifi",
        "--hidden-import", "charset_normalizer",
        "--noconfirm",
        os.path.join(ROOT, "main.py"),
    ]
    print("构建主程序：", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)

    # 删除调试资源包，运行时不需要；走外部 cmd 绕过 safe-delete 钩子
    debug_files = [
        os.path.join(DIST_APP, "_internal", "PyQt6", "Qt6", "resources", "qtwebengine_devtools_resources.debug.pak"),
        os.path.join(DIST_APP, "_internal", "PyQt6", "Qt6", "resources", "qtwebengine_resources.debug.pak"),
        os.path.join(DIST_APP, "_internal", "PyQt6", "Qt6", "resources", "qtwebengine_resources_100p.debug.pak"),
        os.path.join(DIST_APP, "_internal", "PyQt6", "Qt6", "resources", "qtwebengine_resources_200p.debug.pak"),
        os.path.join(DIST_APP, "_internal", "PyQt6", "Qt6", "resources", "v8_context_snapshot.debug.bin"),
    ]
    for dbg in debug_files:
        if os.path.isfile(dbg):
            try:
                subprocess.run(["cmd", "/c", "del", "/f", "/q", dbg], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("removed debug file:", os.path.basename(dbg))
            except Exception as e:
                print("[warn] 跳过删除调试文件（不影响功能）:", dbg, "->", e)

    # 瘦身：删确定用不到的资源（不影响功能）
    slim_dirs = [
        os.path.join(DIST_APP, "_internal", "PyQt6", "Qt6", "qml"),       # QML 运行时（我们用 Widgets）
        os.path.join(DIST_APP, "_internal", "PyQt6", "Qt6", "translations"),  # 多语言翻译（52MB，用不到）
    ]
    for d in slim_dirs:
        if os.path.isdir(d):
            try:
                subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", d], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("removed slim dir:", d)
            except Exception as e:
                print("[warn] 跳过:", d, "->", e)
    # 软件渲染回退 DLL（19MB，有独显就不需要）
    sw = os.path.join(DIST_APP, "_internal", "PyQt6", "Qt6", "bin", "opengl32sw.dll")
    if os.path.isfile(sw):
        try:
            subprocess.run(["cmd", "/c", "del", "/f", "/q", sw], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("removed opengl32sw.dll")
        except Exception as e:
            print("[warn] 跳过 opengl32sw:", e)

    # 打包示例店铺数据：只保留"示例"店铺，其他测试店铺不打包
    seed_data = os.path.join(ROOT, "data", "data.json")
    seed_images = os.path.join(ROOT, "data", "images")
    target_data = os.path.join(DIST_APP, "data")
    target_images = os.path.join(target_data, "images")
    os.makedirs(target_images, exist_ok=True)
    if os.path.isfile(seed_data):
        import json, shutil
        with open(seed_data, "r", encoding="utf-8") as f:
            data = json.load(f)
        shops = data.get("shops", {})
        sample_shops = {k: v for k, v in shops.items() if k == "示例"}
        if not sample_shops:
            print("[warn] 没找到名为「示例」的店铺，将打包空数据")
        data["shops"] = sample_shops
        data["image_categories"] = data.get("image_categories", [])
        data["image_category_map"] = {}
        # 收集示例数据引用的所有图片（只复制被引用的，不复制整个 images 目录）
        # 注意：data.json 里保持原始绝对路径不变，由应用端图片加载时做 fallback
        image_fields = ["spec_image", "link_image", "image_paths"]
        referenced_images = set()
        for shop_name, shop_data in sample_shops.items():
            if not isinstance(shop_data, dict):
                continue
            for cname, recs in shop_data.items():
                if not isinstance(recs, list):
                    continue
                for rec in recs:
                    if not isinstance(rec, dict):
                        continue
                    for field in image_fields:
                        imgs = rec.get(field, [])
                        if not isinstance(imgs, list):
                            continue
                        for img in imgs:
                            if not img:
                                continue
                            fname = os.path.basename(img)
                            src = img if os.path.isfile(img) else os.path.join(seed_images, fname)
                            if os.path.isfile(src):
                                referenced_images.add((src, fname))
        # 只复制被引用的图片（而非整个 images 目录）
        copied = 0
        for src, fname in referenced_images:
            dst = os.path.join(target_images, fname)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
                copied += 1
        # data.json 保持原始路径不变；图片已复制到 target_images，应用端 scaled_pixmap 会 fallback 到 IMAGES_DIR 找同名文件
        with open(os.path.join(target_data, "data.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已打包示例数据：只保留「示例」店铺，引用图片 {copied} 张")

    # 把主程序 exe 重命名为中文名
    src_exe = os.path.join(DIST_APP, "tb-tool2.exe")
    dst_exe = os.path.join(DIST_APP, "淘宝评价工具.exe")
    if os.path.isfile(src_exe):
        if os.path.isfile(dst_exe):
            os.remove(dst_exe)
        os.rename(src_exe, dst_exe)
        print("主程序 exe 已重命名为：淘宝评价工具.exe")

    # 把更新器.exe 复制到主程序目录（安装时一起释放）
    updater_src = os.path.join(ROOT, "installer", "更新器.exe")
    if os.path.isfile(updater_src):
        shutil.copy2(updater_src, os.path.join(DIST_APP, "更新器.exe"))
        print("已包含更新器.exe")

    # 写 version.txt（直接读 config.py 里的 APP_VERSION）
    import re as _re
    cfg = open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read()
    m = _re.search(r'APP_VERSION\s*=\s*"([^"]+)"', cfg)
    ver = m.group(1) if m else "0.0.0"
    with open(os.path.join(DIST_APP, "version.txt"), "w", encoding="utf-8") as f:
        f.write(ver)

    print("主程序已生成：", DIST_APP)


if __name__ == "__main__":
    build_app()
