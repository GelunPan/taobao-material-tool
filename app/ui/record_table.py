"""素材表格组件：负责记录渲染、图片单元格、查看大图与粘贴图片请求。

表格只负责“显示”与“发出请求”，粘贴后如何保存数据由 MainWindow 编排。

性能设计（记录量大时不卡）：
- 文本行一次性批量渲染（渲染期间关闭重绘，结束后统一刷新）
- 图片缩略图懒加载：只解码可见区域（含上下预加载行），滚动时增量加载，
  并用定时器分片处理，避免一次性解码成百上千张图片卡死界面
- 缩略图解码与缓存统一走 image_utils.scaled_pixmap（按目标尺寸解码，不加载原图）
"""
import os

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    FULL_IMAGE_MAX_SIZE,
    MULTI_IMAGE_FIELDS,
    RECORD_FIELDS,
    SINGLE_IMAGE_FIELDS,
    TABLE_COLUMN_MODES,
    TABLE_HEADERS,
    TABLE_ROW_HEIGHT,
    THUMBNAIL_SIZE,
)
from .image_utils import scaled_pixmap

# 可见区上下各预加载的行数（提前解码，滚动更顺滑）
_PRELOAD_ROWS = 8
# 每个定时器周期最多解码的单元格数（分片，防止单帧卡顿）
_BATCH_CELLS = 6


class RecordTable(QTableWidget):
    """素材记录表格，按 RECORD_FIELDS 顺序渲染记录字典"""

    # 用户双击图片占位区，请求粘贴图片（行、列、字段名）
    image_paste_requested = pyqtSignal(int, int, str)
    # 右键菜单：请求修改 / 删除某行（行号）
    edit_requested = pyqtSignal(int)
    delete_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rendered_indices: list[int] = []  # 渲染行号 -> 原始记录列表索引
        self._image_jobs: list[tuple] = []      # 待懒加载的 (行, 列, 图片路径)
        self._loaded_cells: set[tuple] = set()  # 已处理完成的 (行, 列)
        self._load_timer = QTimer(self)
        self._load_timer.timeout.connect(self._process_image_batch)

        self._init_table()
        # 滚动时增量加载新进入可见区的图片
        self.verticalScrollBar().valueChanged.connect(self._schedule_load_visible)

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

        # 右键菜单
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # ---------- 渲染 ----------
    def render(self, records: list, indices: list | None = None) -> None:
        """渲染记录列表（完整列表或搜索过滤后的列表均可）。

        indices: 每个渲染行对应的原始记录列表索引；
                 为 None 时视为渲染的就是原始列表。
        """
        self._rendered_indices = list(indices) if indices is not None else list(range(len(records)))
        self._image_jobs = []
        self._loaded_cells.clear()
        self._load_timer.stop()

        self.setUpdatesEnabled(False)
        self.setRowCount(0)
        self.setRowCount(len(records))
        for row, record in enumerate(records):
            for col, field in enumerate(RECORD_FIELDS):
                value = record.get(field, "")
                if field in MULTI_IMAGE_FIELDS:
                    paths = value if isinstance(value, list) else ([value] if value else [])
                    self._render_image_cell(row, col, paths, field, multi=True)
                elif field in SINGLE_IMAGE_FIELDS:
                    self._render_image_cell(row, col, [value] if value else [], field, multi=False)
                else:
                    self.setItem(row, col, QTableWidgetItem("" if value is None else str(value)))
            self.setRowHeight(row, TABLE_ROW_HEIGHT)
        self.setUpdatesEnabled(True)

        # 首屏图片立即开始分片加载
        self._schedule_load_visible()

    def rendered_index(self, row: int) -> int:
        """渲染行号 -> 原始记录列表索引（供修改/删除定位真实记录）"""
        return self._rendered_indices[row]

    def _render_image_cell(self, row: int, col: int, paths: list, field_name: str, multi: bool) -> None:
        """渲染图片单元格：有图片走懒加载队列，无图片显示粘贴占位。

        底层 item 不存路径文本且不可编辑，避免图片背后露出/编辑出本地链接。
        """
        self.setItem(row, col, self._make_readonly_item(""))
        valid = [p for p in paths if p and os.path.exists(p)]
        if not valid:
            self._set_paste_placeholder(row, col, field_name)
            return

        # 容器：缩略图 + 多图数量角标；图片稍后懒加载填入 thumb
        container = QWidget()
        lay = QGridLayout(container)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)
        thumb = QLabel()
        thumb.setObjectName("thumb")
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setToolTip("双击查看大图")
        thumb.mouseDoubleClickEvent = lambda event, ps=valid: self.show_image_gallery(ps)
        lay.addWidget(thumb, 0, 0)
        if multi and len(valid) > 1:
            badge = QLabel(str(len(valid)))
            badge.setFixedSize(22, 22)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                "background:rgba(64,158,255,230); color:white; border-radius:11px; font-weight:bold;"
            )
            lay.addWidget(badge, 0, 0, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        self.setCellWidget(row, col, container)
        self._image_jobs.append((row, col, valid[0]))

    @staticmethod
    def _make_readonly_item(text: str = "") -> QTableWidgetItem:
        """创建不可编辑的单元格 item（图片列专用，防止露出链接文本）"""
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    # ---------- 图片懒加载 ----------
    def _schedule_load_visible(self, *args) -> None:
        """有未加载的图片且定时器空闲时，启动分片加载"""
        if self._image_jobs and not self._load_timer.isActive():
            self._load_timer.start(0)

    def _process_image_batch(self) -> None:
        """每个 tick 解码可见区内最多 _BATCH_CELLS 个单元格的图片"""
        if not self._image_jobs:
            self._load_timer.stop()
            return
        first, last = self._visible_row_range()
        pending = [
            (r, c, p) for (r, c, p) in self._image_jobs
            if first <= r <= last and (r, c) not in self._loaded_cells
        ]
        for r, c, p in pending[:_BATCH_CELLS]:
            self._load_cell_image(r, c, p)
        visible_done = all(
            (r, c) in self._loaded_cells
            for (r, c, p) in self._image_jobs if first <= r <= last
        )
        if visible_done:
            self._load_timer.stop()

    def _visible_row_range(self) -> tuple:
        """当前可见行范围（含上下预加载行）"""
        count = self.rowCount()
        if count == 0:
            return 0, -1
        row_height = self.rowHeight(0)
        if row_height <= 0:
            row_height = TABLE_ROW_HEIGHT
        top = self.verticalScrollBar().value()
        view_h = max(self.viewport().height(), 1)
        first = max(0, top // row_height - _PRELOAD_ROWS)
        last = min(count - 1, (top + view_h) // row_height + _PRELOAD_ROWS)
        return first, last

    def _load_cell_image(self, row: int, col: int, image_path: str) -> None:
        """解码并把缩略图填入单元格容器内的 thumb 子控件"""
        pixmap = scaled_pixmap(image_path, THUMBNAIL_SIZE)
        self._loaded_cells.add((row, col))
        container = self.cellWidget(row, col)
        thumb = container.findChild(QLabel, "thumb") if container is not None else None
        if pixmap is None:
            if thumb is not None:
                thumb.setText("图片无效")
            else:
                self.removeCellWidget(row, col)
                self.setItem(row, col, self._make_readonly_item("图片无效"))
            return
        if thumb is not None:
            thumb.setPixmap(pixmap)

    # ---------- 粘贴占位 ----------
    def _set_paste_placeholder(self, row: int, col: int, field_name: str) -> None:
        placeholder = QLabel("（粘贴图片）")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(
            "color:#A8ABB2; border:1px dashed #C0C4CC; border-radius:6px; background:#FAFBFC;"
        )
        placeholder.mouseDoubleClickEvent = (
            lambda event, r=row, c=col, f=field_name: self.image_paste_requested.emit(r, c, f)
        )
        self.setCellWidget(row, col, placeholder)

    # ---------- 右键菜单 ----------
    def _show_context_menu(self, pos) -> None:
        row = self.rowAt(pos.y())
        if row < 0:
            return
        self.setCurrentCell(row, 0)
        menu = QMenu(self)
        act_edit = menu.addAction("修改记录")
        act_delete = menu.addAction("删除选中行")
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen == act_edit:
            self.edit_requested.emit(row)
        elif chosen == act_delete:
            self.delete_requested.emit(row)

    # ---------- 查看大图（支持多张翻页） ----------
    def show_image_gallery(self, paths: list, index: int = 0) -> None:
        paths = [p for p in paths if p and os.path.exists(p)]
        if not paths:
            QMessageBox.information(self, "提示", "图片文件不存在")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("查看图片")
        dialog.setMinimumSize(520, 560)
        layout = QVBoxLayout(dialog)

        img_label = QLabel()
        img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(img_label, 1)

        state = {"index": index}

        def render_current():
            pm = scaled_pixmap(paths[state["index"]], FULL_IMAGE_MAX_SIZE)
            if pm is not None:
                img_label.setPixmap(pm)
            else:
                img_label.setText("图片无法读取")
            counter.setText(f"{state['index'] + 1} / {len(paths)}")
            btn_prev.setEnabled(state["index"] > 0)
            btn_next.setEnabled(state["index"] < len(paths) - 1)

        bar = QHBoxLayout()
        btn_prev = QPushButton("上一张")
        btn_next = QPushButton("下一张")
        counter = QLabel()
        counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_close = QPushButton("关闭")
        btn_prev.clicked.connect(lambda: self._gallery_step(state, -1, render_current))
        btn_next.clicked.connect(lambda: self._gallery_step(state, 1, render_current, len(paths)))
        btn_close.clicked.connect(dialog.accept)
        bar.addWidget(btn_prev)
        bar.addStretch()
        bar.addWidget(counter)
        bar.addStretch()
        bar.addWidget(btn_next)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        if len(paths) == 1:
            btn_prev.hide()
            btn_next.hide()
            counter.hide()
        render_current()
        dialog.exec()

    @staticmethod
    def _gallery_step(state: dict, delta: int, render, total: int | None = None) -> None:
        new_index = state["index"] + delta
        if total is not None and not (0 <= new_index < total):
            return
        state["index"] = new_index
        render()
