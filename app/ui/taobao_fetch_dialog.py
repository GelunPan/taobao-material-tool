"""淘宝商品抓取对话框：粘贴商品链接，用已登录的真 Chrome 抓取标题/价格/图片。"""
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, QObject, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QScrollArea, QWidget, QMessageBox,
)

from .. import config
from ..services import taobao_playwright
from ..utils.logger import get_logger

logger = get_logger("taobao.fetch")


class _FetchWorker(QObject):
    done = pyqtSignal(dict)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            res = taobao_playwright.fetch_item(self.url)
        except Exception as e:
            res = {"error": f"抓取异常：{e}"}
        self.done.emit(res)


class TaobaoFetchDialog(QDialog):
    """粘贴淘宝商品链接，抓取并展示标题/价格/图片"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("抓取淘宝商品")
        self.resize(760, 620)
        self._thread = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("粘贴淘宝商品链接，例如 https://item.taobao.com/item.htm?id=...")
        self.btn = QPushButton("抓取")
        self.btn.setStyleSheet(
            "QPushButton { background: #409EFF; color: white; border: none; "
            "padding: 6px 18px; border-radius: 4px; }"
            "QPushButton:hover { background: #66b1ff; }"
            "QPushButton:disabled { background: #a0cfff; }"
        )
        self.btn.clicked.connect(self._start)
        row.addWidget(self.input)
        row.addWidget(self.btn)
        layout.addLayout(row)

        self.status = QLabel("请先完成淘宝登录，再粘贴链接抓取")
        self.status.setStyleSheet("color: #909399; font-size: 12px;")
        layout.addWidget(self.status)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.RichText)
        self.result.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.result)

        # 图片区
        self.img_area = QScrollArea()
        self.img_area.setWidgetResizable(True)
        self.img_container = QWidget()
        self.img_layout = QHBoxLayout(self.img_container)
        self.img_layout.setSpacing(8)
        self.img_area.setWidget(self.img_container)
        self.img_area.setMinimumHeight(160)
        layout.addWidget(self.img_area)

    def _start(self):
        url = self.input.text().strip()
        if not url:
            QMessageBox.information(self, "提示", "请先粘贴商品链接")
            return
        self.btn.setEnabled(False)
        self.status.setText("正在抓取（会先打开 Chrome 访问淘宝，请稍候）...")
        self.result.setText("")
        self._clear_images()

        self._thread = QThread()
        self._worker = _FetchWorker(url)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _on_done(self, res: dict):
        self.btn.setEnabled(True)
        if res.get("error"):
            self.status.setText(f"✗ {res['error']}")
            return
        self.status.setText("✓ 抓取成功")
        self.result.setText(
            f"<b>标题：</b>{res.get('title','')}<br>"
            f"<b>价格：</b>{res.get('price','')}<br>"
            f"<b>商品ID：</b>{res.get('item_id','')}<br>"
            f"<b>图片：</b>{len(res.get('images',[]))} 张"
        )
        self._load_images(res.get("images", []))

    def _clear_images(self):
        while self.img_layout.count():
            it = self.img_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _load_images(self, urls: list):
        self._clear_images()
        for u in urls[:8]:
            lbl = QLabel()
            lbl.setFixedSize(140, 140)
            lbl.setStyleSheet("border: 1px solid #ddd;")
            lbl.setScaledContents(True)
            # 用 requests 下载缩略图（已登录 cookie 在 playwright 里，这里直接下 alicdn 公开图）
            try:
                import requests
                r = requests.get(u, timeout=10, headers={"Referer": "https://item.taobao.com/"})
                if r.status_code == 200:
                    pm = QPixmap()
                    pm.loadFromData(r.content)
                    lbl.setPixmap(pm)
            except Exception as e:
                logger.debug("加载缩略图失败 %s: %s", u[:60], e)
            self.img_layout.addWidget(lbl)
