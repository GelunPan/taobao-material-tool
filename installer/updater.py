# -*- coding: utf-8 -*-
"""淘宝评价工具 —— 增量更新器。

用户双击桌面「更新器」快捷方式，自动：
1. 找 tb_tools_update_V*.zip（先桌面，再下载目录）
2. 读 zip 里的版本号，和当前版本比
3. 版本低则拒绝，版本高则解压覆盖
4. 重启主程序
"""
import os
import sys
import re
import time
import shutil
import zipfile
import subprocess

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QProgressBar,
    QVBoxLayout, QHBoxLayout, QMessageBox, QTextEdit,
)
from PyQt6.QtCore import QThread, pyqtSignal

APP_NAME = "淘宝评价工具"
EXE_NAME = APP_NAME + ".exe"
SKIP_PATHS = {"data"}
UPDATE_PATTERN = re.compile(r"tb_tools_update_V([\d.]+)\.zip", re.IGNORECASE)


def get_current_version(install_dir: str) -> str:
    """读安装目录下的 version.txt。"""
    p = os.path.join(install_dir, "version.txt")
    if os.path.isfile(p):
        try:
            return open(p, encoding="utf-8").read().strip()
        except Exception:
            pass
    return "0.0.0"


def find_update_zip() -> str:
    """找 tb_tools_update_V*.zip：先桌面，再下载目录。"""
    import ctypes
    # 桌面
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x0000, None, 0, buf)
    search_dirs = [buf.value] if buf.value else []
    # 下载目录
    dl = os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")
    if os.path.isdir(dl):
        search_dirs.append(dl)
    # 临时目录
    search_dirs.append(os.environ.get("TEMP", ""))

    best, best_ver = None, None
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            m = UPDATE_PATTERN.match(f)
            if m:
                ver = m.group(1)
                if best_ver is None or ver > best_ver:
                    best_ver = ver
                    best = os.path.join(d, f)
    return best


def find_install_dir() -> str:
    """找主程序安装位置。"""
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

    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0000, None, 0, buf)
        lnk = os.path.join(buf.value, APP_NAME + ".lnk")
        if os.path.isfile(lnk):
            ps = (
                '$ws=New-Object -ComObject WScript.Shell;'
                '$s=$ws.CreateShortcut("%s");'
                'Write-Output $s.TargetPath' % lnk
            )
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=5)
            target = r.stdout.strip()
            if target and os.path.isfile(target):
                return os.path.dirname(target)
    except Exception:
        pass

    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), APP_NAME)
    if os.path.isfile(os.path.join(default, EXE_NAME)):
        return default
    return ""


def kill_main_process():
    try:
        subprocess.run(["taskkill", "/IM", EXE_NAME, "/F"],
                       capture_output=True, timeout=10)
        time.sleep(1)
    except Exception:
        pass


def do_update(install_dir, log_fn, progress_cb) -> tuple:
    """返回 (成功?, 消息)。"""
    # 1. 找更新包
    zip_path = find_update_zip()
    if not zip_path:
        return False, "未找到更新包（tb_tools_update_V*.zip）\n请把更新包放到桌面或下载目录。"

    log_fn(f"找到更新包：{zip_path}")

    # 2. 读更新包版本
    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            new_ver = zf.read("version.txt").decode("utf-8").strip()
        except KeyError:
            new_ver = "999.0.0"  # 没版本号就当最新
        log_fn(f"新版本：{new_ver}")

        cur_ver = get_current_version(install_dir)
        log_fn(f"当前版本：{cur_ver}")

        if new_ver <= cur_ver:
            return False, f"当前已是最新版本（v{cur_ver}），无需更新。"

        # 3. 关主程序
        log_fn("正在关闭主程序…")
        kill_main_process()

        # 4. 解压
        log_fn("正在解压更新包…")
        tmp = os.path.join(os.environ.get("TEMP", "."), "tb_update_tmp")
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        names = zf.namelist()
        total = len(names)
        for i, name in enumerate(names):
            parts = name.replace("\\", "/").split("/")
            if parts and parts[0] in SKIP_PATHS:
                progress_cb(i + 1, total)
                continue
            zf.extract(name, tmp)
            if i % 20 == 0 or i == total - 1:
                progress_cb(i + 1, total)

    # 5. 覆盖
    log_fn("正在覆盖文件…")
    copied = 0
    for root, dirs, files in os.walk(tmp):
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.relpath(src, tmp)
            if rel.split(os.sep)[0] in SKIP_PATHS:
                continue
            dst = os.path.join(install_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
                continue
            shutil.copy2(src, dst)
            copied += 1
    shutil.rmtree(tmp, ignore_errors=True)

    # 6. 写新版本号
    with open(os.path.join(install_dir, "version.txt"), "w", encoding="utf-8") as f:
        f.write(new_ver)

    log_fn(f"更新完成，覆盖了 {copied} 个文件")

    # 7. 重启主程序
    exe = os.path.join(install_dir, EXE_NAME)
    if os.path.isfile(exe):
        log_fn("正在启动主程序…")
        subprocess.Popen([exe], cwd=install_dir)
    return True, f"已更新到 v{new_ver}"


class UpdateWorker(QThread):
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def run(self):
        install_dir = find_install_dir()
        if not install_dir:
            self.done.emit(False, "未找到主程序安装位置")
            return
        try:
            ok, msg = do_update(install_dir,
                                log_fn=lambda m: self.log.emit(m),
                                progress_cb=lambda d, t: self.progress.emit(d, t))
            self.done.emit(ok, msg)
        except Exception as e:
            self.done.emit(False, str(e))


class UpdaterWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("淘宝评价工具 - 更新")
        self.setFixedSize(520, 360)
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
            QMessageBox.information(self, "更新完成", msg)
            self.close()
        else:
            QMessageBox.information(self, "提示", msg)


def main():
    app = QApplication(sys.argv)
    win = UpdaterWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
