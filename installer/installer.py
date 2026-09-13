# -*- coding: utf-8 -*-
"""淘宝评价工具 —— 极简安装向导（PyQt6 版）。

流程：选择安装位置 -> 点击安装 -> 在安装目录释放程序 + 在桌面创建快捷方式。
内置无界面自测模式：``安装程序.exe --test <目标目录>`` 直接跑完整安装流程并退出，
用于离线验证「选位置 / 安装 / 桌面快捷方式」三步是否都正常。
"""
import os
import sys
import time
import zipfile
import subprocess

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QProgressBar,
    QTextEdit, QFileDialog, QMessageBox, QHBoxLayout, QVBoxLayout,
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QIcon

APP_NAME = "淘宝评价工具"
EXE_NAME = APP_NAME + ".exe"


def get_payload_path() -> str:
    """返回内嵌的应用程序 zip 路径。

    冻结后由 PyInstaller 通过 --add-data 把 payload.zip 放到 sys._MEIPASS；
    源码直跑时则取与本脚本同级的 payload.zip（便于开发期自测）。
    """
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "payload.zip")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload.zip")


def get_logo_path() -> str:
    if getattr(sys, "frozen", False):
        p = os.path.join(sys._MEIPASS, "app", "assets", "logo.png")
    else:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "assets", "logo.png")
    return p if os.path.isfile(p) else ""


def default_install_dir() -> str:
    """默认装到用户可写目录，避开 C:\\Program Files 的写权限坑。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def desktop_path() -> str:
    return os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop")


def extract_payload(payload: str, dest: str, progress_cb=None) -> None:
    """把 payload.zip 解压到 dest，progress_cb(done, total) 上报进度。"""
    with zipfile.ZipFile(payload, "r") as zf:
        members = zf.namelist()
        total = len(members)
        os.makedirs(dest, exist_ok=True)
        for i, name in enumerate(members):
            zf.extract(name, dest)
            if progress_cb is not None and (i % 40 == 0 or i == total - 1):
                progress_cb(i + 1, total)


def create_desktop_shortcut(exe_path: str, work_dir: str) -> str:
    """在桌面创建指向 exe 的 .lnk 快捷方式，返回快捷方式路径。

    用系统自带的 PowerShell（Windows 必装）写入，无需任何第三方依赖。
    """
    import tempfile

    lnk = os.path.join(desktop_path(), APP_NAME + ".lnk")
    ps_path = os.path.join(tempfile.gettempdir(), "wb_make_shortcut_%s.ps1" % os.getpid())
    # utf-8-sig 写 BOM，确保 PowerShell 正确识别中文路径
    icon_path = exe_path  # 快捷方式图标直接用主程序 exe 的图标
    script = (
        '$ws = New-Object -ComObject WScript.Shell\n'
        '$s = $ws.CreateShortcut("%s")\n'
        '$s.TargetPath = "%s"\n'
        '$s.WorkingDirectory = "%s"\n'
        '$s.Description = "%s"\n'
        '$s.IconLocation = "%s,0"\n'
        '$s.Save()\n'
    ) % (lnk, exe_path, work_dir, APP_NAME, icon_path)
    with open(ps_path, "w", encoding="utf-8-sig") as f:
        f.write(script)
    try:
        # 不捕获输出（DEVNULL）：避免 PowerShell 的 GBK 输出触发 Python 读取线程
        # 解码异常而导致 subprocess.run 永久挂起（会让安装卡在创建快捷方式）
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        try:
            os.remove(ps_path)
        except OSError:
            pass
    return lnk


def run_install(install_dir: str, log_fn=print, progress_fn=None) -> dict:
    """执行完整安装流程，返回结果信息字典。供 GUI 与自测模式共用。"""
    result = {"install_dir": install_dir, "exe": None, "shortcut": None, "ok": False, "error": None}
    payload = get_payload_path()
    if not os.path.isfile(payload):
        result["error"] = "未找到内置程序包 payload.zip（路径：%s）" % payload
        return result

    log_fn("正在释放程序文件到：%s" % install_dir)
    try:
        extract_payload(payload, install_dir, progress_fn)
    except Exception as e:  # noqa: BLE001
        result["error"] = "解压失败：%s" % e
        return result

    exe_path = os.path.join(install_dir, EXE_NAME)
    if not os.path.isfile(exe_path):
        result["error"] = "释放完成但未找到主程序：%s" % exe_path
        return result
    result["exe"] = exe_path

    log_fn("正在创建桌面快捷方式…")
    try:
        result["shortcut"] = create_desktop_shortcut(exe_path, install_dir)
        log_fn("桌面快捷方式已创建：%s" % result["shortcut"])
    except Exception as e:  # noqa: BLE001
        result["shortcut_error"] = "快捷方式创建失败（不影响使用）：%s" % e
        log_fn(result["shortcut_error"])

    result["ok"] = True
    log_fn("安装完成 ✅")
    return result


class InstallWorker(QThread):
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def __init__(self, target: str):
        super().__init__()
        self.target = target

    def run(self):
        res = run_install(
            self.target,
            log_fn=lambda m: self.log.emit(m),
            progress_fn=lambda d, t: self.progress.emit(d, t),
        )
        self.finished.emit(res)


class InstallerWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("%s 安装向导" % APP_NAME)
        self.setFixedSize(560, 400)
        logo = get_logo_path()
        if logo:
            self.setWindowIcon(QIcon(logo))

        self.install_dir = default_install_dir()
        self.worker = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(8)

        title = QLabel("%s 安装向导" % APP_NAME)
        title.setStyleSheet("font-size:16px; font-weight:bold;")
        root.addWidget(title)
        sub = QLabel("版本 v1.0 正式版　|　仅此一步：选好位置，点安装即可。")
        sub.setStyleSheet("color:#666666; font-size:9pt;")
        root.addWidget(sub)

        row = QHBoxLayout()
        row.addWidget(QLabel("安装位置："))
        self.entry = QLineEdit(self.install_dir)
        self.entry.setMinimumWidth(300)
        row.addWidget(self.entry, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        root.addLayout(row)

        self.status = QLabel("")
        root.addWidget(self.status)

        self.bar = QProgressBar()
        self.bar.setValue(0)
        root.addWidget(self.bar)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("background:#f7f7f7; font-family:Consolas; font-size:9pt;")
        root.addWidget(self.log, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_install = QPushButton("安装")
        self.btn_install.setStyleSheet("background:#2e7d32; color:white; font-weight:bold; padding:6px 18px;")
        self.btn_install.clicked.connect(self._start_install)
        btns.addWidget(self.btn_install)
        self.btn_close = QPushButton("取消")
        self.btn_close.clicked.connect(self.close)
        btns.addWidget(self.btn_close)
        root.addLayout(btns)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择安装位置", self.entry.text())
        if d:
            self.entry.setText(d)

    def _log(self, msg):
        self.log.append(msg)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _set_status(self, msg):
        self.status.setText(msg)

    def _start_install(self):
        target = self.entry.text().strip()
        if not target:
            QMessageBox.critical(self, "错误", "请先选择安装位置。")
            return
        parent = os.path.dirname(target) or target
        if not os.path.isdir(parent):
            QMessageBox.critical(self, "错误", "上级目录不存在，无法在此安装：\n%s" % target)
            return
        try:
            os.makedirs(target, exist_ok=True)
            test = os.path.join(target, ".wtest")
            with open(test, "w") as f:
                f.write("1")
            os.remove(test)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "错误", "该位置不可写，请换一个目录：\n%s" % e)
            return

        self.btn_install.setEnabled(False)
        self.btn_close.setEnabled(False)
        self._set_status("正在安装，请稍候…")
        self._log("开始安装到：%s" % target)

        self.worker = InstallWorker(target)
        self.worker.log.connect(self._log)
        self.worker.progress.connect(lambda d, t: self.bar.setValue(int(d / t * 100)))
        self.worker.finished.connect(self._finish)
        self.worker.start()

    def _finish(self, res):
        self.bar.setValue(100)
        self.btn_close.setEnabled(True)
        self.btn_close.setText("关闭")
        if res.get("ok"):
            self._set_status("安装完成 ✅")
            extra = ("\n\n注意：" + res["shortcut_error"]) if res.get("shortcut_error") else ""
            msg = "程序已安装到：\n%s\n\n桌面快捷方式：%s%s" % (
                res["install_dir"], res.get("shortcut") or "（未创建）", extra)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("安装完成")
            box.setText(msg)
            open_btn = box.addButton("打开安装目录", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() == open_btn:
                os.startfile(res["install_dir"])
            # 安装完成后自动关闭安装向导窗口
            self.close()
        else:
            self._set_status("安装失败 ❌")
            QMessageBox.critical(self, "安装失败", res.get("error") or "未知错误")


def main():
    # 自测模式：安装程序.exe --test <目标目录>
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        target = sys.argv[2]

        # GBK 控制台无法编码 ✅ 等 emoji，自测打印做编码兜底，避免验证过程本身崩
        def _safe_log(msg):
            s = str(msg)
            try:
                print(s)
            except UnicodeEncodeError:
                # 不可编码字符（✅ 等）直接替换为 ? 后输出，绝不二次抛错
                print(s.encode("gbk", "replace").decode("gbk", "ignore"))

        _safe_log("[自测] 安装到：%s" % target)
        t0 = time.time()
        res = run_install(target, log_fn=_safe_log,
                          progress_fn=lambda d, t: _safe_log("  进度 %.0f%%" % (d / t * 100)))
        _safe_log("结果：%s" % res)
        _safe_log("耗时 %.1fs" % (time.time() - t0))
        return

    app = QApplication(sys.argv)
    win = InstallerWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
