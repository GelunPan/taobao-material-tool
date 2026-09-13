"""可复用的动画弹窗基类：无边框 + 可拖动 + 弹出/关闭动画。

用法：
    class MyDialog(AnimatedDialog):
        def _build_content(self):
            # 子类实现内容布局，添加到 self.content_layout
            ...

    dialog = MyDialog(parent)
    dialog.exec()  # 自动播放弹出动画，点关闭自动播放消失动画后关闭

动画参数可通过类属性覆盖：
    POP_DURATION = 300      # 弹出动画时长(ms)
    CLOSE_DURATION = 200    # 关闭动画时长(ms)
    POP_SCALE_START = 0.92  # 弹出起始缩放比例
    CLOSE_SCALE_END = 0.95  # 关闭结束缩放比例
"""
from PyQt6.QtCore import (
    Qt, QPoint, QRect, QEasingCurve, QPropertyAnimation, QTimer, pyqtProperty,
)
from PyQt6.QtGui import QPainter, QColor, QBrush, QPainterPath
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QWidget, QGraphicsOpacityEffect


class AnimatedDialog(QDialog):
    """可复用的动画弹窗基类。

    特性：
    - 无边框圆角卡片，自带阴影
    - 整个窗口可拖动（鼠标按住任意位置拖动）
    - show/exec 时自动播放弹出动画（透明度+缩放，OutCubic）
    - 调用 close_with_animation() 或子类调用 _on_close 时播放消失动画后关闭
    - 子类只需实现 _build_content()，把内容加到 self.content_layout
    """

    # 动画参数（子类可覆盖）
    POP_DURATION = 300
    CLOSE_DURATION = 200
    POP_SCALE_START = 0.92
    CLOSE_SCALE_END = 0.95
    CARD_RADIUS = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)

        # 拖动状态
        self._drag_pos = QPoint()
        self._is_dragging = False

        # 动画状态
        self._is_closing = False
        self._base_geometry = QRect()

        # 透明度效果（用于弹出/关闭动画）
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_effect)

        # 外层布局（容纳圆角卡片）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)  # 外边距留阴影空间
        outer.setSpacing(0)

        # 圆角卡片容器
        self._card = QWidget()
        self._card.setObjectName("animatedCard")
        self._card.setStyleSheet(f"""
            QWidget#animatedCard {{
                background: #FFFFFF;
                border-radius: {self.CARD_RADIUS}px;
            }}
        """)
        self._card_layout = QVBoxLayout(self._card)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(0)

        # 内容区（子类填充）
        self.content_layout = QVBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        self._card_layout.addLayout(self.content_layout)

        outer.addWidget(self._card)

        # 子类构建内容
        self._build_content()

    # ==================== 子类实现 ====================
    def _build_content(self):
        """子类实现：把内容加到 self.content_layout"""
        pass

    # ==================== 拖动 ====================
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._is_dragging = True
            event.accept()

    def mouseMoveEvent(self, event):
        if self._is_dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._is_dragging = False

    # ==================== 弹出动画 ====================
    def showEvent(self, event):
        super().showEvent(event)
        if not self._base_geometry.isValid():
            self._base_geometry = self.geometry()
        # 弹出动画：透明度 0→1 + 缩放 从 POP_SCALE_START→1.0
        self._animate_pop()

    def _animate_pop(self):
        """播放弹出动画"""
        # 透明度动画
        self._opacity_anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._opacity_anim.setDuration(self.POP_DURATION)
        self._opacity_anim.setStartValue(0.0)
        self._opacity_anim.setEndValue(1.0)
        self._opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # 缩放动画（通过 geometry 实现中心缩放）
        self._scale_anim = QPropertyAnimation(self, b"geometry", self)
        self._scale_anim.setDuration(self.POP_DURATION)
        self._scale_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        start_rect = self._scaled_rect(self.POP_SCALE_START)
        self._scale_anim.setStartValue(start_rect)
        self._scale_anim.setEndValue(self._base_geometry)

        self._opacity_anim.start()
        self._scale_anim.start()

    def _scaled_rect(self, scale: float) -> QRect:
        """以窗口中心为原点，计算缩放后的 geometry"""
        center = self._base_geometry.center()
        w = int(self._base_geometry.width() * scale)
        h = int(self._base_geometry.height() * scale)
        return QRect(center.x() - w // 2, center.y() - h // 2, w, h)

    # ==================== 关闭动画 ====================
    def close_with_animation(self):
        """播放关闭动画，动画结束后真正关闭"""
        if self._is_closing:
            return
        self._is_closing = True

        # 透明度动画 1→0
        self._close_opacity_anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._close_opacity_anim.setDuration(self.CLOSE_DURATION)
        self._close_opacity_anim.setStartValue(1.0)
        self._close_opacity_anim.setEndValue(0.0)
        self._close_opacity_anim.setEasingCurve(QEasingCurve.Type.InCubic)

        # 缩放动画 1.0→CLOSE_SCALE_END
        self._close_scale_anim = QPropertyAnimation(self, b"geometry", self)
        self._close_scale_anim.setDuration(self.CLOSE_DURATION)
        self._close_scale_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._close_scale_anim.setStartValue(self.geometry())
        self._close_scale_anim.setEndValue(self._scaled_rect(self.CLOSE_SCALE_END))

        self._close_opacity_anim.finished.connect(self._finish_close)
        self._close_opacity_anim.start()
        self._close_scale_anim.start()

    def _finish_close(self):
        """关闭动画结束，真正关闭"""
        self.done(0)

    def _on_close(self):
        """子类关闭按钮调用此方法，触发关闭动画"""
        self.close_with_animation()

    def reject(self):
        """ESC 键关闭时也走动画"""
        if not self._is_closing:
            self.close_with_animation()
        else:
            super().reject()
