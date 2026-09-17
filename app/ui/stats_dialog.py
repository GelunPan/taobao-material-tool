# -*- coding: utf-8 -*-
"""数据统计对话框：带动画的扇形图，统计每月各分类记录数和各分类评价图片数"""
import math
from collections import defaultdict
from datetime import datetime

from PyQt6.QtCore import (Qt, QPropertyAnimation, QEasingCurve, QRectF, QPointF,
                          pyqtProperty, QTimer)
from PyQt6.QtGui import QPainter, QColor, QPen, QFont, QPainterPath
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                             QWidget, QGridLayout, QScrollArea, QFrame)


# 好看的配色方案（柔和但有区分度）
PIE_COLORS = [
    QColor("#5B8FF9"), QColor("#5AD8A6"), QColor("#5D7092"), QColor("#F6BD16"),
    QColor("#E8684A"), QColor("#6DC8EC"), QColor("#9270CA"), QColor("#FF9D4D"),
    QColor("#FF99C3"), QColor("#269A99"), QColor("#A0A0FF"), QColor("#7DAE2F"),
]


class AnimatedPieChart(QWidget):
    """带动画的扇形图：依次展开每个扇形，鼠标悬停高亮。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._title = title
        self._data = []  # [(label, value, color), ...]
        self._progress = 0.0  # 动画进度 0-1
        self._hover_index = -1
        self.setMinimumSize(320, 320)
        self.setMouseTracking(True)

        # 动画
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
        """data: [(label, value), ...]"""
        total = sum(v for _, v in data) or 1
        self._data = [(label, value, PIE_COLORS[i % len(PIE_COLORS)])
                      for i, (label, value) in enumerate(data)]
        self._total = total
        self._progress = 0.0
        self.update()
        QTimer.singleShot(100, self._anim.start)

    def _pie_rect(self):
        size = min(self.width(), self.height()) - 40
        x = (self.width() - size) / 2
        y = (self.height() - size) / 2 + 10
        return QRectF(x, y, size, size)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 标题
        painter.setPen(QColor("#333333"))
        painter.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        painter.drawText(QRectF(0, 0, self.width(), 30),
                         Qt.AlignmentFlag.AlignCenter, self._title)

        if not self._data:
            painter.setPen(QColor("#999999"))
            painter.setFont(QFont("Microsoft YaHei", 10))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无数据")
            return

        rect = self._pie_rect()
        center = rect.center()
        radius = rect.width() / 2

        # 计算每个扇形的角度
        start_angle = 90  # 从顶部开始
        total_draw = self._total * self._progress

        drawn = 0
        for i, (label, value, color) in enumerate(self._data):
            if drawn >= total_draw:
                break
            slice_value = min(value, total_draw - drawn)
            span_angle = (slice_value / self._total) * 360

            # 悬停的扇形向外偏移
            offset = 0
            if i == self._hover_index:
                mid_angle = math.radians(start_angle - span_angle / 2)
                offset = 8
                ox = math.cos(mid_angle) * offset
                oy = -math.sin(mid_angle) * offset
            else:
                ox = oy = 0

            # 绘制扇形
            path = QPainterPath()
            path.moveTo(center.x() + ox, center.y() + oy)
            path.arcTo(QRectF(rect.x() + ox, rect.y() + oy,
                              rect.width(), rect.height()),
                       start_angle, -span_angle)
            path.closeSubpath()

            painter.fillPath(path, color)

            # 百分比文字（只在扇形足够大时显示）
            if span_angle > 25 and self._progress > 0.8:
                pct = value / self._total * 100
                if pct >= 3:
                    mid_angle = math.radians(start_angle - span_angle / 2)
                    text_r = radius * 0.6
                    tx = center.x() + math.cos(mid_angle) * text_r + ox
                    ty = center.y() - math.sin(mid_angle) * text_r + oy
                    painter.setPen(QColor("#ffffff"))
                    painter.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
                    painter.drawText(QRectF(tx - 25, ty - 10, 50, 20),
                                     Qt.AlignmentFlag.AlignCenter, f"{pct:.0f}%")

            start_angle -= span_angle
            drawn += slice_value

        # 中心圆（做成环形图更好看）
        inner_r = radius * 0.45
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, inner_r, inner_r)

        # 中心文字：总数
        painter.setPen(QColor("#333333"))
        painter.setFont(QFont("Microsoft YaHei", 16, QFont.Weight.Bold))
        painter.drawText(QRectF(center.x() - 50, center.y() - 25, 100, 30),
                         Qt.AlignmentFlag.AlignCenter, str(self._total))
        painter.setFont(QFont("Microsoft YaHei", 9))
        painter.setPen(QColor("#999999"))
        painter.drawText(QRectF(center.x() - 50, center.y() + 5, 100, 20),
                         Qt.AlignmentFlag.AlignCenter, "总计")

        painter.end()

    def mouseMoveEvent(self, event):
        # 检测鼠标悬停在哪个扇形上
        rect = self._pie_rect()
        center = rect.center()
        dx = event.position().x() - center.x()
        dy = event.position().y() - center.y()
        dist = math.sqrt(dx * dx + dy * dy)
        radius = rect.width() / 2

        if radius * 0.45 < dist < radius:
            angle = math.degrees(math.atan2(-dy, dx))
            if angle < 0:
                angle += 360
            # 从顶部(90度)开始，顺时针
            start_angle = 90
            found = -1
            for i, (label, value, color) in enumerate(self._data):
                span_angle = (value / self._total) * 360
                end_angle = start_angle - span_angle
                # 归一化角度比较
                a = (start_angle - angle) % 360
                if a <= span_angle:
                    found = i
                    break
                start_angle = end_angle
            self._hover_index = found
        else:
            self._hover_index = -1
        self.update()

    def leaveEvent(self, event):
        self._hover_index = -1
        self.update()


class StatsDialog(QDialog):
    """数据统计对话框：两个扇形图 + 图例 + 数据明细"""

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("📊 数据统计")
        self.resize(900, 680)
        self.setMinimumSize(760, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # 顶部：标题 + 店铺选择
        top = QHBoxLayout()
        title = QLabel("📊 数据统计")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #1677ff;")
        top.addWidget(title)
        top.addStretch()

        top.addWidget(QLabel("店铺："))
        self.shop_combo = QComboBox()
        self.shop_combo.setStyleSheet("QComboBox { padding: 6px 12px; border: 1px solid #d9d9d9; border-radius: 6px; min-width: 140px; }")
        self.shop_combo.currentIndexChanged.connect(self._refresh)
        top.addWidget(self.shop_combo)

        top.addWidget(QLabel("月份："))
        self.month_combo = QComboBox()
        self.month_combo.setStyleSheet("QComboBox { padding: 6px 12px; border: 1px solid #d9d9d9; border-radius: 6px; min-width: 120px; }")
        self.month_combo.currentIndexChanged.connect(self._refresh)
        top.addWidget(self.month_combo)

        root.addLayout(top)

        # 分割线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #e8e8e8;")
        root.addWidget(line)

        # 两个扇形图并排
        charts_row = QHBoxLayout()
        charts_row.setSpacing(20)

        self.pie_records = AnimatedPieChart("每月各分类记录数")
        self.pie_images = AnimatedPieChart("各分类评价图片数")
        charts_row.addWidget(self.pie_records, 1)
        charts_row.addWidget(self.pie_images, 1)
        root.addLayout(charts_row, 1)

        # 底部：数据明细（可滚动）
        detail_label = QLabel("📋 数据明细")
        detail_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #333;")
        root.addWidget(detail_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: 1px solid #e8e8e8; border-radius: 6px; background: #fafafa; }")
        self.detail_widget = QWidget()
        self.detail_layout = QGridLayout(self.detail_widget)
        self.detail_layout.setSpacing(8)
        self.detail_layout.setContentsMargins(12, 12, 12, 12)
        scroll.setWidget(self.detail_widget)
        root.addWidget(scroll, 1)

        self._load_shops()

    def _load_shops(self):
        self.shop_combo.clear()
        shops = list(self.repo.shops.keys())
        self.shop_combo.addItems(shops)
        if shops:
            self._load_months(shops[0])

    def _load_months(self, shop):
        """收集该店铺所有记录的月份"""
        months = set()
        for cat, records in self.repo.shops.get(shop, {}).items():
            for r in records:
                m = r.get("created_at", "未知")
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
                m = r.get("created_at", "未知")
                if m == month:
                    records_by_cat[cat] += 1
        records_data = [(cat, cnt) for cat, cnt in sorted(records_by_cat.items(),
                                                           key=lambda x: -x[1]) if cnt > 0]
        self.pie_records.set_data(records_data)

        # 统计2：各分类的评价图片数（所有月份）
        images_by_cat = defaultdict(int)
        for cat, records in self.repo.shops.get(shop, {}).items():
            for r in records:
                imgs = r.get("image_paths") or []
                images_by_cat[cat] += len(imgs)
        images_data = [(cat, cnt) for cat, cnt in sorted(images_by_cat.items(),
                                                          key=lambda x: -x[1]) if cnt > 0]
        self.pie_images.set_data(images_data)

        # 数据明细
        # 清空旧内容
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        # 表头
        headers = ["分类", f"{month} 记录数", "评价图片数", "记录占比", "图片占比"]
        for col, h in enumerate(headers):
            lbl = QLabel(h)
            lbl.setStyleSheet("font-weight: bold; color: #555; padding: 4px 8px;")
            self.detail_layout.addWidget(lbl, 0, col)

        total_records = sum(cnt for _, cnt in records_data) or 1
        total_images = sum(cnt for _, cnt in images_data) or 1
        all_cats = sorted(set(list(records_by_cat.keys()) + list(images_by_cat.keys())))

        for row, cat in enumerate(all_cats, 1):
            rc = records_by_cat.get(cat, 0)
            ic = images_by_cat.get(cat, 0)
            color = PIE_COLORS[all_cats.index(cat) % len(PIE_COLORS)]

            # 分类名（带颜色点）
            cat_lbl = QLabel(f"  ● {cat}")
            cat_lbl.setStyleSheet(f"color: {color.name()}; font-weight: bold; padding: 4px 8px;")
            self.detail_layout.addWidget(cat_lbl, row, 0)

            for col, val in enumerate([rc, ic, f"{rc/total_records*100:.1f}%",
                                        f"{ic/total_images*100:.1f}%"], 1):
                lbl = QLabel(str(val))
                lbl.setStyleSheet("color: #333; padding: 4px 8px;")
                self.detail_layout.addWidget(lbl, row, col)

        # 合计行
        total_lbl = QLabel("  合计")
        total_lbl.setStyleSheet("font-weight: bold; color: #1677ff; padding: 4px 8px; border-top: 1px solid #ddd;")
        self.detail_layout.addWidget(total_lbl, len(all_cats) + 1, 0)
        for col, val in enumerate([total_records, total_images, "100%", "100%"], 1):
            lbl = QLabel(str(val))
            lbl.setStyleSheet("font-weight: bold; color: #1677ff; padding: 4px 8px; border-top: 1px solid #ddd;")
            self.detail_layout.addWidget(lbl, len(all_cats) + 1, col)
