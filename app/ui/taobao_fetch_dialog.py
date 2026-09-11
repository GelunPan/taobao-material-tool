"""淘宝商品抓取对话框：粘贴商品链接，用已登录的真 Chrome 抓取标题/价格/图片。"""
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, QObject, Qt, QPropertyAnimation, QEasingCurve, QTimer, pyqtProperty
from PyQt6.QtGui import QPixmap, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QScrollArea, QWidget, QMessageBox,
)

from .. import config
from ..services import taobao_playwright
from ..utils.logger import get_logger

logger = get_logger("taobao.fetch")

# 加载动画 SVG 路径
LOADING_SVG = str(config.ASSETS_DIR / "loading.svg")


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


class _RotatingSvgIcon(QLabel):
    """旋转的 SVG 加载图标：QSvgRenderer 手动渲染 + QPropertyAnimation 驱动旋转。
    SVG 只解析一次，每帧只是旋转变换+重绘，性能开销极小。"""
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
    """中央加载动画覆盖层：旋转 SVG + 状态文字"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("background: rgba(255,255,255,0.85);")
        self.hide()

        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(12)

        # SVG 旋转加载图标
        self.icon = _RotatingSvgIcon(LOADING_SVG, 64)
        lay.addWidget(self.icon, alignment=Qt.AlignmentFlag.AlignCenter)

        # 状态文字
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
    """粘贴淘宝商品链接，抓取并展示标题/价格/图片"""
    fill_requested = pyqtSignal(dict)  # 填充到当前店铺：传递抓取结果

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("抓取淘宝商品")
        self.resize(760, 660)
        # 加最小化按钮
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self._thread = None
        self._last_result = None

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

        # 底部按钮行
        btn_row = QHBoxLayout()
        self.btn_fill = QPushButton("填充到当前店铺")
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

        # 中央加载动画覆盖层（放在最后，覆盖所有内容）
        self.loading_overlay = _LoadingOverlay(self)
        self.loading_overlay.setGeometry(self.rect())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 覆盖层始终铺满整个对话框
        self.loading_overlay.setGeometry(self.rect())

    def _start(self):
        url = self.input.text().strip()
        if not url:
            QMessageBox.information(self, "提示", "请先粘贴商品链接")
            return
        self.btn.setEnabled(False)
        self.btn_fill.setEnabled(False)
        self.status.setText("正在抓取（会先打开 Chrome 访问淘宝，请稍候）...")
        self.result.setText("")
        self._clear_images()
        self.loading_overlay.show_with_text("正在打开 Chrome 抓取商品...\n（首次启动较慢，请稍候）")

        self._thread = QThread()
        self._worker = _FetchWorker(url)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _on_done(self, res: dict):
        self.btn.setEnabled(True)
        self.loading_overlay.hide_overlay()
        if res.get("error"):
            # cookie失效时自动清空本地cookie文件
            err = res["error"]
            if "cookie" in err and ("失效" in err or "重新登录" in err):
                try:
                    if config.TAOBAO_COOKIE_FILE.exists():
                        config.TAOBAO_COOKIE_FILE.unlink()
                        logger.info("检测到cookie失效，已自动清空本地cookie文件")
                except Exception as e:
                    logger.warning("清空cookie文件失败: %s", e)
            self.status.setText(f"✗ {err}")
            self.btn_fill.setEnabled(False)
            self._last_result = None
            logger.error("抓取失败: %s", err)
            return
        self._last_result = res
        self.btn_fill.setEnabled(True)
        self.status.setText("✓ 抓取成功")
        sku_text = ""
        if res.get("skus"):
            parts = [f"{s['name']}: {', '.join(s['options'][:3])}" for s in res["skus"][:2]]
            sku_text = f"<br><b>规格：</b>{' | '.join(parts)}"
        self.result.setText(
            f"<b>标题：</b>{res.get('title','')}<br>"
            f"<b>价格：</b>{res.get('price','')}<br>"
            f"<b>商品ID：</b>{res.get('item_id','')}<br>"
            f"<b>图片：</b>{len(res.get('images',[]))} 张"
            f"{sku_text}"
        )
        self._load_images(res.get("images", []))

    def _on_fill(self):
        """点击填充按钮：把抓取结果通过信号传给主窗口"""
        if not self._last_result:
            return
        logger.info("用户点击填充到当前店铺，商品ID=%s", self._last_result.get("item_id"))
        self.btn_fill.setEnabled(False)
        self.btn_fill.setText("正在填充...")
        self.loading_overlay.show_with_text("正在下载图片并填充到店铺...")
        # 让界面先刷新
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        self.fill_requested.emit(self._last_result)
        self.loading_overlay.hide_overlay()
        self.btn_fill.setText("填充到当前店铺")
        self.accept()

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
