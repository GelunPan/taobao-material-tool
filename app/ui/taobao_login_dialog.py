"""淘宝登录对话框：调用本机真实 Chrome（Playwright）完成扫码登录。

不再内嵌 QWebEngine（嵌入式 Chromium 指纹会被淘宝风控）。
打开本对话框后自动启动一个真实 Chrome 窗口，请在其中扫码；
登录成功并验证通过后自动发出 cookies_received 并关闭。
"""
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)

from .. import config
from ..services import taobao_playwright
from ..utils.logger import get_logger

logger = get_logger("taobao.login")


class _LoginWorker(QObject):
    """在工作线程里跑 Playwright 登录流程"""
    status = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def run(self):
        try:
            ok, msg = taobao_playwright.run_login(
                config.TAOBAO_COOKIE_FILE, on_status=self.status.emit
            )
            self.done.emit(ok, msg)
        except Exception as e:
            logger.exception("登录工作线程异常")
            self.done.emit(False, f"登录异常：{e}")


class TaobaoLoginDialog(QDialog):
    """淘宝登录对话框：启动真实 Chrome 扫码，成功后发出 cookie。

    信号：cookies_received(list[dict])：登录验证成功后发出。
    """

    cookies_received = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登录淘宝")
        self.setMinimumWidth(480)
        self._thread = None
        self._worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("淘宝登录")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        self._tip = QLabel("点击下方按钮，将打开一个真实的 Chrome 浏览器窗口，请用淘宝 APP 扫码登录。")
        self._tip.setWordWrap(True)
        self._tip.setStyleSheet("color: #606266; font-size: 13px;")
        layout.addWidget(self._tip)

        self._status = QLabel("就绪")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #409EFF; font-size: 13px; padding: 8px; background: #ECF5FF; border-radius: 4px;")
        layout.addWidget(self._status)

        row = QHBoxLayout()
        self._btn_start = QPushButton("打开 Chrome 登录")
        self._btn_start.setStyleSheet(
            "QPushButton { background: #4CAF50; color: white; border: none; "
            "padding: 8px 24px; border-radius: 4px; font-size: 14px; }"
            "QPushButton:hover { background: #43A047; }"
            "QPushButton:disabled { background: #A5D6A7; }"
        )
        self._btn_start.clicked.connect(self._start_login)
        row.addStretch()
        row.addWidget(self._btn_start)
        row.addStretch()
        layout.addLayout(row)

    def _start_login(self):
        self._btn_start.setEnabled(False)
        self._btn_start.setText("登录中...")
        self._status.setText("正在启动浏览器...")
        self._thread = QThread()
        self._worker = _LoginWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status.connect(self._status.setText)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_done(self, ok: bool, msg: str):
        if ok:
            self._status.setText(f"✓ {msg}")
            logger.info("登录成功：%s，读取 cookie 文件并发信号", msg)
            try:
                cookies = __import__("json").loads(
                    Path(config.TAOBAO_COOKIE_FILE).read_text(encoding="utf-8")
                )
            except Exception as e:
                cookies = []
                logger.error("读取 cookie 文件失败: %s", e)
            self.cookies_received.emit(cookies)
            self.accept()
        else:
            self._status.setText(f"✗ {msg}")
            self._btn_start.setEnabled(True)
            self._btn_start.setText("重试")
            QMessageBox.warning(self, "登录未完成", msg)

    def reject(self):
        if self._thread and self._thread.isRunning():
            self._thread.quit()
        super().reject()
