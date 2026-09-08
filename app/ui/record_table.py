"""素材表格组件：负责记录渲染、图片单元格、查看大图与粘贴图片请求。

表格只负责“显示”与“发出请求”，粘贴后如何保存数据由 MainWindow 编排。
"""
import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..config import (
    FULL_IMAGE_MAX_SIZE,
    IMAGE_FIELDS,
    RECORD_FIELDS,
    TABLE_COLUMN_MODES,
    TABLE_HEADERS,
    TABLE_ROW_HEIGHT,
    THUMBNAIL_SIZE,
)


class RecordTable(QTableWidget):
    """素材记录表格，按 RECORD_FIELDS 顺序渲染记录字典"""

    # 用户双击图片占位区，请求粘贴图片（行、列、字段名）
    image_paste_requested = pyqtSignal(int, int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_table()

    def _init_table(self):
        self.setColumnCount(len(TABLE_HEADERS))
        self.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        header = self.horizontalHeader()
        for col, mode in enumerate(TABLE_COLUMN_MODES):
            resize = QHeaderView.ResizeMode.Stretch if mode else QHeaderView.ResizeMode.ResizeToContents
            header.setSectionResizeMode(col, resize)

    # ---------- 渲染 ----------
    def render(self, records: list) -> None:
        """渲染记录列表（完整列表或搜索过滤后的列表均可）"""
        self.setRowCount(0)
        self.setRowCount(len(records))
        for row, record in enumerate(records):
            for col, field in enumerate(RECORD_FIELDS):
                value = record.get(field, "")
                if field in IMAGE_FIELDS:
                    self.set_image_cell(row, col, value, field)
                else:
                    self.setItem(row, col, QTableWidgetItem(value))
            self.setRowHeight(row, TABLE_ROW_HEIGHT)

    # ---------- 图片单元格 ----------
    def set_image_cell(self, row: int, col: int, image_path: str, field_name: str) -> None:
        if image_path and os.path.exists(image_path):
            pixmap = QPixmap(image_path)
            if pixmap.isNull():
                self.setItem(row, col, QTableWidgetItem("图片无效"))
                return
            scaled = pixmap.scaled(
                THUMBNAIL_SIZE, THUMBNAIL_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            img_label = QLabel()
            img_label.setPixmap(scaled)
            img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img_label.setToolTip("双击查看大图\n按Ctrl+V粘贴新图片")
            img_label.mouseDoubleClickEvent = lambda event, p=image_path: self.show_full_image(p)
            self.setCellWidget(row, col, img_label)
            self.setItem(row, col, QTableWidgetItem(image_path))
        else:
            placeholder = QLabel("（粘贴图片）")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color: #aaa; border: 1px dashed #ccc; padding: 10px;")
            placeholder.mouseDoubleClickEvent = (
                lambda event, r=row, c=col, f=field_name: self.image_paste_requested.emit(r, c, f)
            )
            self.setCellWidget(row, col, placeholder)
            self.setItem(row, col, QTableWidgetItem(""))

    # ---------- 查看大图 ----------
    def show_full_image(self, image_path: str) -> None:
        if not os.path.exists(image_path):
            QMessageBox.information(self, "提示", "图片文件不存在")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("查看图片")
        dialog.setMinimumSize(500, 500)
        layout = QVBoxLayout(dialog)
        label = QLabel()
        pixmap = QPixmap(image_path)
        if pixmap.width() > FULL_IMAGE_MAX_SIZE or pixmap.height() > FULL_IMAGE_MAX_SIZE:
            pixmap = pixmap.scaled(
                FULL_IMAGE_MAX_SIZE, FULL_IMAGE_MAX_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        dialog.exec()
