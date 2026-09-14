"""淘宝商品抓取对话框：粘贴一段文本（可含多个链接/分享口令），批量抓取标题/价格/图片，一键填充。"""
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, QObject, Qt, QPropertyAnimation, QEasingCurve, QTimer, pyqtProperty
from PyQt6.QtGui import QPixmap, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QScrollArea, QWidget, QMessageBox, QTextEdit, QListWidget, QListWidgetItem,
)

from .. import config
from ..services import taobao_playwright
from ..services.link_utils import extract_all_product_urls
from ..utils.logger import get_logger

logger = get_logger("taobao.fetch")

LOADING_SVG = str(config.ASSETS_DIR / "loading.svg")


class _FetchBatchWorker(QObject):
    """逐个抓取多个商品，进度/结果实时回吐"""
    progress = pyqtSignal(int, int, str)       # done, total, current_url
    item_done = pyqtSignal(int, dict)          # index, result
    all_done = pyqtSignal(list)                # results in order

    def __init__(self, urls: list):
        super().__init__()
        self.urls = urls

    def run(self):
        results = [None] * len(self.urls)
        for i, url in enumerate(self.urls):
            self.progress.emit(i, len(self.urls), url)
            try:
                res = taobao_playwright.fetch_item(url)
            except Exception as e:
                res = {"error": f"抓取异常：{e}", "url": url}
            results[i] = res
            self.item_done.emit(i, res)
        self.all_done.emit(results)


class _RotatingSvgIcon(QLabel):
    def __init__(self, svg_path: str, size: int = 64, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._renderer = QSvgRenderer(svg_path)
        self._angle = 0
        if self._renderer.isValid():
            self._anim = QPropertyAnimation(self, b"angle", self)
            self._anim.setDuration(1200)
            self._anim.setStartValue(0)
            self._anim.setEndValue(360)
            self._anim.setLoopCount(-1)
            self._anim.setEasingCurve(QEasingCurve.Type.Linear)
            self._anim.start()
        else:
            self.setText("⏳")
            self.setStyleSheet("font-size: 48px;")

    @pyqtProperty(int)
    def angle(self):
        return self._angle

    @angle.setter
    def angle(self, value):
        self._angle = value
        self.update()

    def paintEvent(self, event):
        if not getattr(self, '_renderer', None) or not self._renderer.isValid():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(self._angle)
        painter.translate(-self.width() / 2, -self.height() / 2)
        self._renderer.render(painter, QRectF(0, 0, self.width(), self.height()))
        painter.end()


class _LoadingOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("background: rgba(255,255,255,0.85);")
        self.hide()
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(12)
        self.icon = _RotatingSvgIcon(LOADING_SVG, 64)
        lay.addWidget(self.icon, alignment=Qt.AlignmentFlag.AlignCenter)
        self.text = QLabel("加载中...")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setStyleSheet("color: #606266; font-size: 14px;")
        lay.addWidget(self.text)

    def show_with_text(self, text: str):
        self.text.setText(text)
        self.show()
        self.raise_()

    def hide_overlay(self):
        self.hide()


class TaobaoFetchDialog(QDialog):
    """粘贴一段文本（可含多个链接/分享口令），批量抓取并一键填充。"""
    fill_requested = pyqtSignal(list)  # 成功抓取结果列表

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("抓取淘宝商品")
        self.resize(780, 700)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinimizeButtonHint)
        self._thread = None
        self._results = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(QLabel("粘贴文本（支持一次粘贴多段分享口令/多个链接，自动识别）："))
        self.input = QTextEdit()
        self.input.setPlaceholderText(
            "支持一次粘贴多个商品链接或多段【淘宝】分享口令，自动识别并逐个抓取：\n"
            "https://item.taobao.com/item.htm?id=...\n"
            "https://item.taobao.com/item.htm?id=..."
        )
        self.input.setMaximumHeight(110)
        layout.addWidget(self.input)

        row = QHBoxLayout()
        self.btn = QPushButton("开始抓取")
        self.btn.setStyleSheet(
            "QPushButton { background: #409EFF; color: white; border: none; "
            "padding: 8px 22px; border-radius: 4px; font-size: 14px; }"
            "QPushButton:hover { background: #66b1ff; }"
            "QPushButton:disabled { background: #a0cfff; }"
        )
        self.btn.clicked.connect(self._start)
        row.addStretch()
        row.addWidget(self.btn)
        layout.addLayout(row)

        self.status = QLabel("请先完成淘宝登录，再粘贴链接抓取")
        self.status.setStyleSheet("color: #909399; font-size: 12px;")
        layout.addWidget(self.status)

        # 结果列表
        self.list = QListWidget()
        self.list.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.list, 1)

        # 底部按钮
        btn_row = QHBoxLayout()
        self.btn_fill = QPushButton("一键填充全部成功商品")
        self.btn_fill.setStyleSheet(
            "QPushButton { background: #67C23A; color: white; border: none; "
            "padding: 8px 20px; border-radius: 4px; font-size: 13px; }"
            "QPushButton:hover { background: #85ce61; }"
            "QPushButton:disabled { background: #b3e19d; }"
        )
        self.btn_fill.setEnabled(False)
        self.btn_fill.clicked.connect(self._on_fill)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_fill)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setStyleSheet(
            "QPushButton { background: #f5f7fa; color: #606266; border: 1px solid #dcdfe6; "
            "padding: 8px 20px; border-radius: 4px; font-size: 13px; }"
            "QPushButton:hover { background: #ecf5ff; color: #409EFF; }"
        )
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        layout.addLayout(btn_row)

        self.loading_overlay = _LoadingOverlay(self)
        self.loading_overlay.setGeometry(self.rect())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.loading_overlay.setGeometry(self.rect())

    def _start(self):
        urls = extract_all_product_urls(self.input.toPlainText())
        if not urls:
            QMessageBox.information(self, "提示", "未识别到商品链接，请粘贴后再试")
            return
        self.btn.setEnabled(False)
        self.btn_fill.setEnabled(False)
        self.list.clear()
        self._results = [None] * len(urls)
        for i, u in enumerate(urls):
            self.list.addItem(QListWidgetItem(f"⏳ 待抓取：{u[:80]}"))
        self.status.setText(f"共识别到 {len(urls)} 个商品，开始逐个抓取...")
        self.loading_overlay.show_with_text(f"正在打开 Chrome 抓取（0/{len(urls)}）...")

        self._thread = QThread()
        self._worker = _FetchBatchWorker(urls)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.item_done.connect(self._on_item_done)
        self._worker.all_done.connect(self._on_all_done)
        self._worker.all_done.connect(self._thread.quit)
        self._thread.start()

    def _on_progress(self, done, total, url):
        self.loading_overlay.show_with_text(f"正在抓取（{done}/{total}）...\n{url[:60]}")

    def _on_item_done(self, idx, res):
        self._results[idx] = res
        item = self.list.item(idx)
        if not item:
            return
        if res.get("error"):
            item.setText(f"✗ 失败：{res.get('error','')[:60]}")
            item.setForeground(Qt.GlobalColor.red)
        else:
            item.setText(f"✓ {res.get('title','')[:60]}  ¥{res.get('price','')}  ({len(res.get('images',[]))}图)")
            item.setForeground(Qt.GlobalColor.darkGreen)

    def _on_all_done(self, results):
        self.btn.setEnabled(True)
        self.loading_overlay.hide_overlay()
        ok = [r for r in results if r and not r.get("error")]
        fail = [r for r in results if r and r.get("error")]
        self.status.setText(f"完成：成功 {len(ok)} 个，失败 {len(fail)} 个")
        if ok:
            self.btn_fill.setEnabled(True)
            self._results = ok
        else:
            # cookie 失效自动清
            for r in fail:
                err = r.get("error", "")
                if "cookie" in err and ("失效" in err or "重新登录" in err):
                    try:
                        if config.TAOBAO_COOKIE_FILE.exists():
                            config.TAOBAO_COOKIE_FILE.unlink()
                    except Exception:
                        pass
                    break

    def _on_fill(self):
        if not self._results:
            return
        self.btn_fill.setEnabled(False)
        self.btn_fill.setText("正在填充...")
        self.loading_overlay.show_with_text(f"正在下载图片并填充 {len(self._results)} 个商品...")
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        self.fill_requested.emit(self._results)
        self.loading_overlay.hide_overlay()
        self.accept()
