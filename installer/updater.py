# -*- coding: utf-8 -*-
"""淘宝评价工具 —— 增量更新器。

用法：
  1. 开发者跑 build_update.py 生成 update.zip（只含改动文件）
  2. 把 updater.exe（内嵌 update.zip）发给用户
  3. 用户双击 updater.exe，自动：
     - 找主程序安装位置（注册表/快捷方式/默认路径）
     - 关闭正在运行的主程序
     - 对比文件差异，覆盖新文件
     - 跳过 data/ 用户数据
     - 重启主程序
"""
import os
import sys
import time
import shutil
import zipfile
import subprocess
import ctypes
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QProgressBar,
    QVBoxLayout, QHBoxLayout, QMessageBox, QTextEdit,
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt

APP_NAME = "淘宝评价工具"
EXE_NAME = APP_NAME + ".exe"
# 这些目录/文件不覆盖（用户数据）
SKIP_PATHS = {"data"}


def get_update_zip() -> str:
    """冻结后从 sys._MEIPASS 取 update.zip；源码直跑则取同级。"""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "update.zip")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.zip")


def find_install_dir() -> str:
    """查找主程序安装位置，按优先级：
    1. 注册表卸载项（HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall）
    2. 桌面快捷方式 .lnk 的 TargetPath
    3. 默认路径 %LOCALAPPDATA%\\淘宝评价工具
    """
    # 1. 注册表
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
        )
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
                i += 1
            except OSError:
                break
            try:
                sk = winreg.OpenKey(key, sub)
                disp = winreg.QueryValueEx(sk, "DisplayName")[0]
                if APP_NAME in disp:
                    loc = winreg.QueryValueEx(sk, "InstallLocation")[0]
                    if os.path.isfile(os.path.join(loc, EXE_NAME)):
                        return loc
            except OSError:
                continue
    except Exception:
        pass

    # 2. 桌面快捷方式
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0000, None, 0, buf)
        lnk = os.path.join(buf.value, APP_NAME + ".lnk")
        if os.path.isfile(lnk):
            # 用 PowerShell 读快捷方式目标
            ps = (
                '$ws=New-Object -ComObject WScript.Shell;'
                '$s=$ws.CreateShortcut("%s");'
                'Write-Output $s.TargetPath' % lnk
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=5,
            )
            target = r.stdout.strip()
            if target and os.path.isfile(target):
                return os.path.dirname(target)
    except Exception:
        pass

    # 3. 默认路径
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), APP_NAME)
    if os.path.isfile(os.path.join(default, EXE_NAME)):
        return default

    return ""


def kill_main_process():
    """关闭正在运行的主程序。"""
    try:
        subprocess.run(
            ["taskkill", "/IM", EXE_NAME, "/F"],
            capture_output=True, timeout=10,
        )
        time.sleep(1)
    except Exception:
        pass


def do_update(install_dir: str, log_fn, progress_cb) -> bool:
    """执行更新：解压 update.zip 覆盖到 install_dir，跳过 data/。"""
    update_zip = get_update_zip()
    if not os.path.isfile(update_zip):
        log_fn("未找到内置更新包 update.zip")
        return False

    log_fn(f"安装目录：{install_dir}")
    log_fn("正在关闭主程序…")
    kill_main_process()

    log_fn("正在解压更新包…")
    tmp = os.path.join(os.environ.get("TEMP", "."), "tb_update_tmp")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    with zipfile.ZipFile(update_zip, "r") as zf:
        names = zf.namelist()
        total = len(names)
        for i, name in enumerate(names):
            # 跳过 data/ 用户数据
            parts = name.replace("\\", "/").split("/")
            if parts and parts[0] in SKIP_PATHS:
                progress_cb(i + 1, total)
                continue
            zf.extract(name, tmp)
            if i % 20 == 0 or i == total - 1:
                progress_cb(i + 1, total)

    log_fn("正在覆盖文件…")
    copied = 0
    for root, dirs, files in os.walk(tmp):
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.relpath(src, tmp)
            dst = os.path.join(install_dir, rel)
            # 跳过 data/
            if rel.split(os.sep)[0] in SKIP_PATHS:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            # 对比大小，相同就不覆盖
            if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
                continue
            shutil.copy2(src, dst)
            copied += 1

    shutil.rmtree(tmp, ignore_errors=True)
    log_fn(f"更新完成，覆盖了 {copied} 个文件")

    # 重启主程序
    exe = os.path.join(install_dir, EXE_NAME)
    if os.path.isfile(exe):
        log_fn("正在启动主程序…")
        subprocess.Popen([exe], cwd=install_dir)
    return True


class UpdateWorker(QThread):
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def run(self):
        install_dir = find_install_dir()
        if not install_dir:
            self.done.emit(False, "未找到主程序安装位置，请手动选择安装目录")
            return
        try:
            ok = do_update(
                install_dir,
                log_fn=lambda m: self.log.emit(m),
                progress_cb=lambda d, t: self.progress.emit(d, t),
            )
            self.done.emit(ok, install_dir)
        except Exception as e:
            self.done.emit(False, str(e))


class UpdaterWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("淘宝评价工具 - 更新")
        self.setFixedSize(520, 380)
        self._build_ui()
        self._run()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        title = QLabel("正在更新淘宝评价工具…")
        title.setStyleSheet("font-size:16px; font-weight:bold;")
        root.addWidget(title)
        self.bar = QProgressBar()
        root.addWidget(self.bar)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("background:#f7f7f7; font-family:Consolas; font-size:9pt;")
        root.addWidget(self.log, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setEnabled(False)
        btns.addWidget(self.close_btn)
        root.addLayout(btns)

    def _log(self, m):
        self.log.append(m)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _run(self):
        self.worker = UpdateWorker()
        self.worker.log.connect(self._log)
        self.worker.progress.connect(lambda d, t: self.bar.setValue(int(d / t * 100) if t else 0))
        self.worker.done.connect(self._finish)
        self.worker.start()

    def _finish(self, ok, msg):
        self.bar.setValue(100)
        self.close_btn.setEnabled(True)
        if ok:
            QMessageBox.information(self, "更新完成", f"已更新到最新版本！\n{msg}")
            self.close()
        else:
            QMessageBox.critical(self, "更新失败", msg)


def main():
    app = QApplication(sys.argv)
    win = UpdaterWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
