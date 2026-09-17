# -*- coding: utf-8 -*-
"""数据统计对话框：带动画的环形扇形图 + 概览卡片 + 数据明细，整体滚动"""
import math
from collections import defaultdict
from datetime import datetime

from PyQt6.QtCore import (Qt, QPropertyAnimation, QEasingCurve, QRectF,
                          pyqtProperty, QTimer, pyqtSignal)
from PyQt6.QtGui import QPainter, QColor, QPen, QFont, QPainterPath
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                             QWidget, QGridLayout, QScrollArea, QFrame, QSizePolicy)


# 配色方案（现代、柔和有区分度）
PIE_COLORS = [
    QColor("#5B8FF9"), QColor("#5AD8A6"), QColor("#F6BD16"), QColor("#E8684A"),
    QColor("#6DC8EC"), QColor("#9270CA"), QColor("#FF9D4D"), QColor("#FF99C3"),
    QColor("#269A99"), QColor("#A0A0FF"), QColor("#7DAE2F"), QColor("#FF6B6B"),
]


class AnimatedPieChart(QWidget):
    """带动画的环形扇形图：依次展开每个扇形，鼠标悬停高亮。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._title = title
        self._data = []
        self._progress = 0.0
        self._hover_index = -1
        self.setMinimumSize(280, 280)
        self.setMouseTracking(True)

        self._anim = QPropertyAnimation(self, b"progress", self)
        self._anim.setDuration(900)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    @pyqtProperty(float)
    def progress(self):
        return self._progress

    @progress.setter
    def progress(self, value):
        self._progress = value
        self.update()

    def set_data(self, data: list):
        total = sum(v for _, v in data) or 1
        self._data = [(label, value, PIE_COLORS[i % len(PIE_COLORS)])
                      for i, (label, value) in enumerate(data)]
        self._total = total
        self._progress = 0.0
        self.update()
        QTimer.singleShot(100, self._anim.start)

    def _pie_rect(self):
        size = min(self.width(), self.height()) - 30
        x = (self.width() - size) / 2
        y = (self.height() - size) / 2 + 8
        return QRectF(x, y, size, size)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 标题
        painter.setPen(QColor("#1f1f1f"))
        painter.setFont(QFont("Microsoft YaHei", 13, QFont.Weight.Bold))
        painter.drawText(QRectF(0, 0, self.width(), 28),
                         Qt.AlignmentFlag.AlignCenter, self._title)

        if not self._data:
            painter.setPen(QColor("#bbb"))
            painter.setFont(QFont("Microsoft YaHei", 11))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无数据")
            return

        rect = self._pie_rect()
        center = rect.center()
        radius = rect.width() / 2

        start_angle = 90
        total_draw = self._total * self._progress
        drawn = 0

        for i, (label, value, color) in enumerate(self._data):
            if drawn >= total_draw:
                break
            slice_value = min(value, total_draw - drawn)
            span_angle = (slice_value / self._total) * 360

            ox = oy = 0
            if i == self._hover_index:
                mid_angle = math.radians(start_angle - span_angle / 2)
                ox = math.cos(mid_angle) * 8
                oy = -math.sin(mid_angle) * 8

            path = QPainterPath()
            path.moveTo(center.x() + ox, center.y() + oy)
            path.arcTo(QRectF(rect.x() + ox, rect.y() + oy,
                              rect.width(), rect.height()),
                       start_angle, -span_angle)
            path.closeSubpath()
            painter.fillPath(path, color)

            if span_angle > 25 and self._progress > 0.8:
                pct = value / self._total * 100
                if pct >= 3:
                    mid_angle = math.radians(start_angle - span_angle / 2)
                    text_r = radius * 0.62
                    tx = center.x() + math.cos(mid_angle) * text_r + ox
                    ty = center.y() - math.sin(mid_angle) * text_r + oy
                    painter.setPen(QColor("#ffffff"))
                    painter.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
                    painter.drawText(QRectF(tx - 28, ty - 12, 56, 24),
                                     Qt.AlignmentFlag.AlignCenter, f"{pct:.0f}%")

            start_angle -= span_angle
            drawn += slice_value

        # 中心圆
        inner_r = radius * 0.48
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, inner_r, inner_r)

        # 中心文字
        painter.setPen(QColor("#1f1f1f"))
        painter.setFont(QFont("Microsoft YaHei", 20, QFont.Weight.Bold))
        painter.drawText(QRectF(center.x() - 60, center.y() - 28, 120, 32),
                         Qt.AlignmentFlag.AlignCenter, str(self._total))
        painter.setFont(QFont("Microsoft YaHei", 10))
        painter.setPen(QColor("#999"))
        painter.drawText(QRectF(center.x() - 60, center.y() + 6, 120, 20),
                         Qt.AlignmentFlag.AlignCenter, "总计")
        painter.end()

    def mouseMoveEvent(self, event):
        rect = self._pie_rect()
        center = rect.center()
        dx = event.position().x() - center.x()
        dy = event.position().y() - center.y()
        dist = math.sqrt(dx * dx + dy * dy)
        radius = rect.width() / 2

        if radius * 0.48 < dist < radius:
            angle = math.degrees(math.atan2(-dy, dx))
            if angle < 0:
                angle += 360
            start_angle = 90
            found = -1
            for i, (label, value, color) in enumerate(self._data):
                span_angle = (value / self._total) * 360
                a = (start_angle - angle) % 360
                if a <= span_angle:
                    found = i
                    break
                start_angle -= span_angle
            self._hover_index = found
        else:
            self._hover_index = -1
        self.update()

    def leaveEvent(self, event):
        self._hover_index = -1
        self.update()


class StatCard(QFrame):
    """现代风格统计卡片：左侧彩色竖条 + 标题 + 大数值"""

    def __init__(self, title: str, color: str, parent=None):
        super().__init__(parent)
        self._color = color
        self.setStyleSheet(f"""
            QFrame {{
                background: #ffffff;
                border: 1px solid #f0f0f0;
                border-radius: 12px;
            }}
            QFrame:hover {{
                border: 1px solid {color}60;
            }}
        """)
        self.setFixedHeight(88)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 左侧彩色竖条
        bar = QFrame()
        bar.setFixedWidth(4)
        bar.setStyleSheet(f"background: {color}; border-top-left-radius: 12px; border-bottom-left-radius: 12px;")
        layout.addWidget(bar)

        # 内容区
        content = QVBoxLayout()
        content.setContentsMargins(16, 14, 16, 14)
        content.setSpacing(4)

        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet("color: #8c8c8c; font-size: 13px;")
        content.addWidget(self.title_lbl)

        self.value_lbl = QLabel("0")
        self.value_lbl.setStyleSheet(f"color: {color}; font-size: 30px; font-weight: bold;")
        content.addWidget(self.value_lbl)

        layout.addLayout(content, 1)

    def set_value(self, value: str):
        self.value_lbl.setText(value)


class StatsDialog(QDialog):
    """数据统计对话框：整体滚动，现代风格"""

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("📊 数据统计")
        self.resize(880, 720)
        self.setMinimumSize(760, 600)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ===== 顶部固定栏：标题 + 选择器 =====
        top_bar = QFrame()
        top_bar.setStyleSheet("background: #ffffff; border-bottom: 1px solid #f0f0f0;")
        top_bar.setFixedHeight(64)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(24, 0, 24, 0)
        top_layout.setSpacing(16)

        title = QLabel("📊 数据统计")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #1f1f1f;")
        top_layout.addWidget(title)
        top_layout.addStretch()

        top_layout.addWidget(QLabel("店铺："))
        self.shop_combo = QComboBox()
        self.shop_combo.setStyleSheet(
            "QComboBox { padding: 7px 14px; border: 1px solid #d9d9d9; border-radius: 8px; "
            "min-width: 140px; background: #fff; }"
            "QComboBox:hover { border-color: #409eff; }"
        )
        self.shop_combo.currentIndexChanged.connect(self._refresh)
        top_layout.addWidget(self.shop_combo)

        top_layout.addWidget(QLabel("月份："))
        self.month_combo = QComboBox()
        self.month_combo.setStyleSheet(
            "QComboBox { padding: 7px 14px; border: 1px solid #d9d9d9; border-radius: 8px; "
            "min-width: 120px; background: #fff; }"
            "QComboBox:hover { border-color: #409eff; }"
        )
        self.month_combo.currentIndexChanged.connect(self._refresh)
        top_layout.addWidget(self.month_combo)

        root.addWidget(top_bar)

        # ===== 整体滚动区域 =====
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: #f5f7fa; }")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content_widget = QWidget()
        content_widget.setStyleSheet("background: #f5f7fa;")
        self.content_layout = QVBoxLayout(content_widget)
        self.content_layout.setContentsMargins(24, 20, 24, 24)
        self.content_layout.setSpacing(16)

        # 概览卡片
        cards_row = QHBoxLayout()
        cards_row.setSpacing(16)
        self.card_avg_images = StatCard("📷 平均图片数（每条记录）", "#5B8FF9")
        self.card_avg_words = StatCard("📝 平均字数（每条评价）", "#5AD8A6")
        cards_row.addWidget(self.card_avg_images, 1)
        cards_row.addWidget(self.card_avg_words, 1)
        self.content_layout.addLayout(cards_row)

        # 扇形图卡片
        charts_card = QFrame()
        charts_card.setStyleSheet("background: #ffffff; border: 1px solid #f0f0f0; border-radius: 12px;")
        charts_layout = QVBoxLayout(charts_card)
        charts_layout.setContentsMargins(20, 16, 20, 20)
        charts_layout.setSpacing(8)

        charts_title = QLabel("📈 分类分布")
        charts_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #1f1f1f;")
        charts_layout.addWidget(charts_title)

        charts_row = QHBoxLayout()
        charts_row.setSpacing(20)
        self.pie_records = AnimatedPieChart("每月各分类记录数")
        self.pie_images = AnimatedPieChart("各分类评价图片数")
        charts_row.addWidget(self.pie_records, 1)
        charts_row.addWidget(self.pie_images, 1)
        charts_layout.addLayout(charts_row, 1)
        self.content_layout.addWidget(charts_card)

        # 数据明细卡片
        detail_card = QFrame()
        detail_card.setStyleSheet("background: #ffffff; border: 1px solid #f0f0f0; border-radius: 12px;")
        detail_layout = QVBoxLayout(detail_card)
        detail_layout.setContentsMargins(20, 16, 20, 20)
        detail_layout.setSpacing(12)

        detail_title = QLabel("📋 数据明细")
        detail_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #1f1f1f;")
        detail_layout.addWidget(detail_title)

        self.detail_widget = QWidget()
        self.detail_layout = QGridLayout(self.detail_widget)
        self.detail_layout.setSpacing(0)
        self.detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.detail_widget)

        self.content_layout.addWidget(detail_card)
        self.content_layout.addStretch()

        scroll.setWidget(content_widget)
        root.addWidget(scroll, 1)

        self._load_shops()

    def _load_shops(self):
        self.shop_combo.clear()
        shops = list(self.repo.shops.keys())
        self.shop_combo.addItems(shops)
        if shops:
            self._load_months(shops[0])

    def _load_months(self, shop):
        months = set()
        for cat, records in self.repo.shops.get(shop, {}).items():
            for r in records:
                m = r.get("created_at", "")
                if m and m != "未知":
                    months.add(m)
        months = sorted(months, reverse=True)
        if not months:
            months = [datetime.now().strftime("%Y-%m")]
        self.month_combo.blockSignals(True)
        self.month_combo.clear()
        self.month_combo.addItems(months)
        self.month_combo.blockSignals(False)
        self._refresh()

    def _refresh(self):
        shop = self.shop_combo.currentText()
        month = self.month_combo.currentText()
        if not shop:
            return

        # 统计1：该月份各分类的记录数
        records_by_cat = defaultdict(int)
        for cat, records in self.repo.shops.get(shop, {}).items():
            for r in records:
                m = r.get("created_at", "")
                if m == month:
                    records_by_cat[cat] += 1
        records_data = [(cat, cnt) for cat, cnt in sorted(records_by_cat.items(),
                                                           key=lambda x: -x[1]) if cnt > 0]
        self.pie_records.set_data(records_data)

        # 统计2：各分类的评价图片数
        images_by_cat = defaultdict(int)
        for cat, records in self.repo.shops.get(shop, {}).items():
            for r in records:
                imgs = r.get("image_paths") or []
                images_by_cat[cat] += len(imgs)
        images_data = [(cat, cnt) for cat, cnt in sorted(images_by_cat.items(),
                                                          key=lambda x: -x[1]) if cnt > 0]
        self.pie_images.set_data(images_data)

        # 计算平均值
        all_records = []
        for cat, records in self.repo.shops.get(shop, {}).items():
            all_records.extend(records)
        total_count = len(all_records) or 1
        total_images = sum(len(r.get("image_paths") or []) for r in all_records)
        total_words = sum(len(r.get("review") or "") for r in all_records)
        self.card_avg_images.set_value(f"{total_images / total_count:.1f}")
        self.card_avg_words.set_value(f"{total_words / total_count:.0f}")

        # 数据明细
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        headers = ["分类", f"{month} 记录数", "评价图片数", "记录占比", "图片占比"]
        for col, h in enumerate(headers):
            lbl = QLabel(h)
            lbl.setStyleSheet("color: #595959; font-size: 13px; font-weight: bold; "
                               "padding: 10px 12px; background: #fafafa; "
                               "border-bottom: 1px solid #f0f0f0;")
            self.detail_layout.addWidget(lbl, 0, col)

        total_records = sum(cnt for _, cnt in records_data) or 1
        total_images_sum = sum(cnt for _, cnt in images_data) or 1
        all_cats = sorted(set(list(records_by_cat.keys()) + list(images_by_cat.keys())))

        for row, cat in enumerate(all_cats, 1):
            rc = records_by_cat.get(cat, 0)
            ic = images_by_cat.get(cat, 0)
            color = PIE_COLORS[all_cats.index(cat) % len(PIE_COLORS)]
            bg = "#ffffff" if row % 2 == 1 else "#fafbfc"

            cat_lbl = QLabel(f"  ● {cat}")
            cat_lbl.setStyleSheet(f"color: {color.name()}; font-size: 13px; font-weight: bold; "
                                  f"padding: 10px 12px; background: {bg}; border-bottom: 1px solid #f5f5f5;")
            self.detail_layout.addWidget(cat_lbl, row, 0)

            for col, val in enumerate([rc, ic, f"{rc/total_records*100:.1f}%",
                                        f"{ic/total_images_sum*100:.1f}%"], 1):
                lbl = QLabel(str(val))
                lbl.setStyleSheet(f"color: #262626; font-size: 13px; padding: 10px 12px; "
                                  f"background: {bg}; border-bottom: 1px solid #f5f5f5;")
                self.detail_layout.addWidget(lbl, row, col)

        # 合计行
        total_lbl = QLabel("  合计")
        total_lbl.setStyleSheet("color: #1677ff; font-size: 13px; font-weight: bold; "
                                "padding: 12px; background: #f0f7ff; border-top: 1px solid #d6e8ff;")
        self.detail_layout.addWidget(total_lbl, len(all_cats) + 1, 0)
        for col, val in enumerate([total_records, total_images_sum, "100%", "100%"], 1):
            lbl = QLabel(str(val))
            lbl.setStyleSheet("color: #1677ff; font-size: 13px; font-weight: bold; "
                              "padding: 12px; background: #f0f7ff; border-top: 1px solid #d6e8ff;")
            self.detail_layout.addWidget(lbl, len(all_cats) + 1, col)
