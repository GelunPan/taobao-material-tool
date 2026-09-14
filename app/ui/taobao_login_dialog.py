"""淘宝登录对话框：调用本机真实浏览器（Playwright）完成扫码登录。

用户可选择用 Chrome 还是 Edge 登录（自动探测本机已装的浏览器）。
"""
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)

from .. import config
from ..services import taobao_playwright
from ..utils.logger import get_logger

logger = get_logger("taobao.login")


class _LoginWorker(QObject):
    """在工作线程里跑 Playwright 登录流程"""
    status = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, browser: str):
        super().__init__()
        self.browser = browser

    def run(self):
        try:
            ok, msg = taobao_playwright.run_login(
                config.TAOBAO_COOKIE_FILE, on_status=self.status.emit,
                browser=self.browser,
            )
            self.done.emit(ok, msg)
        except Exception as e:
            logger.exception("登录工作线程异常")
            self.done.emit(False, f"登录异常：{e}")


class TaobaoLoginDialog(QDialog):
    """淘宝登录对话框：选择浏览器并扫码，成功后发出 cookie。

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

        # 浏览器选择
        brow_row = QHBoxLayout()
        brow_row.addWidget(QLabel("登录浏览器："))
        self._brow_combo = QComboBox()
        available = taobao_playwright.list_available_browsers()
        self._brow_combo.addItems(available)
        # 默认选第一个可用（通常是 Chrome）
        brow_row.addWidget(self._brow_combo, 1)
        layout.addLayout(brow_row)

        self._tip = QLabel("点击下方按钮，将打开所选浏览器窗口，请用淘宝 APP 扫码登录。")
        self._tip.setWordWrap(True)
        self._tip.setStyleSheet("color: #606266; font-size: 13px;")
        layout.addWidget(self._tip)

        self._status = QLabel("就绪")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #409EFF; font-size: 13px; padding: 8px; background: #ECF5FF; border-radius: 4px;")
        layout.addWidget(self._status)

        row = QHBoxLayout()
        self._btn_start = QPushButton("打开浏览器登录")
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
        browser = self._brow_combo.currentText()
        self._brow_combo.setEnabled(False)
        self._btn_start.setEnabled(False)
        self._btn_start.setText("登录中...")
        self._status.setText(f"正在启动 {browser} ...")
        self._thread = QThread()
        self._worker = _LoginWorker(browser)
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
            self._brow_combo.setEnabled(True)
            self._btn_start.setEnabled(True)
            self._btn_start.setText("重试")
            QMessageBox.warning(self, "登录未完成", msg)

    def reject(self):
        if self._thread and self._thread.isRunning():
            self._thread.quit()
        super().reject()
