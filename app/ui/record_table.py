"""素材表格组件：负责记录渲染、勾选、图片单元格、查看大图与粘贴/删图请求。

表格只负责“显示”与“发出请求”，粘贴/删除后如何保存数据由 MainWindow 编排。

显示规则（行高随内容自适应）：
- 文本列开启自动换行，行高随文本长度与当前列宽变化（限定上下限，全文可悬停查看）
- 单图列（规格图/链接主图）居中显示一张缩略图
- 多图列（评价图片）所有图片在单元格内流式并排显示，宽度不够自动换行，
  两张即左右并排、更多张按每行最多 TABLE_MULTI_PER_ROW 张整齐排列
- 列宽被拖拽或窗口拉伸变化时，行高自动重算，排版始终整齐

勾选与粘贴：
- 最左侧为勾选列，每行一个复选框，表头复选框支持全选 / 取消全选 / 半选三态
- 选中图片单元格后可直接 Ctrl+V 粘贴剪贴板图片；图片右键菜单支持
  查看大图、粘贴图片、删除此图片（多图时精确删除右键的那一张）

性能设计（记录量大时不卡）：
- 文本行一次性批量渲染（渲染期间关闭重绘，结束后统一刷新）
- 图片缩略图懒加载：只解码可见区域（含上下预加载行），滚动时增量加载，
  并用定时器分片处理，避免一次性解码成百上千张图片卡死界面
- 缩略图解码与缓存统一走 image_utils.scaled_pixmap（按目标尺寸解码，不加载原图）
"""
import os

from PyQt6.QtCore import QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
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
    IMAGE_FIELDS,
    MULTI_IMAGE_FIELDS,
    RECORD_FIELDS,
    SELECT_COLUMN_WIDTH,
    SELECT_COL_HEADER,
    SINGLE_IMAGE_FIELDS,
    TABLE_CELL_PAD,
    TABLE_COLUMN_MODES,
    TABLE_GRID,
    TABLE_HEADERS,
    TABLE_ITEM_PAD_H,
    TABLE_ITEM_PAD_V,
    TABLE_MULTI_PER_ROW,
    TABLE_MULTI_THUMB,
    TABLE_ROW_MAX_HEIGHT,
    TABLE_ROW_MIN_HEIGHT,
    TABLE_SINGLE_THUMB,
    TABLE_TEXT_HPAD,
    TABLE_TEXT_VPAD,
    TABLE_THUMB_GAP,
)
from .flow_layout import FlowLayout
from .image_utils import scaled_pixmap

# 可见区上下各预加载的行数（提前解码，滚动更顺滑）
_PRELOAD_ROWS = 8
# 每个定时器周期最多解码的单元格数（分片，防止单帧卡顿）
_BATCH_CELLS = 6
# 列宽变化后重算行高的防抖间隔（毫秒），拖拽列宽时不会频繁卡顿
_ROW_RESIZE_DEBOUNCE = 30
# 表头全选复选框边长（与 QSS 中 indicator 尺寸一致）
_HEADER_CHECK_SIZE = 16

_THUMB_STYLE = "border:1px solid #DCDFE6; border-radius:6px; background:#FFFFFF;"
_PLACEHOLDER_STYLE = (
    "color:#A8ABB2; border:1px dashed #C0C4CC; border-radius:6px; background:#FAFBFC;"
)


class TableCell(QWidget):
    """单元格容器基类：统一补偿 QTableWidget::item 的 padding。

    全局 QSS 给 item 设置了 padding（上下 4、左右 6），Qt 给 cellWidget
    的物理几何会扣除该 padding 与 1px 网格线，而 ResizeToContents 定列宽时
    又直接取 sizeHint，两者口径不一致。这里让 sizeHint 主动补上差值，
    保证内部内容按真实物理尺寸排布、缩略图不被裁切。
    """

    def content_size(self) -> tuple[int, int]:
        """子类返回内部内容期望的（宽, 高）"""
        layout = self.layout()
        size = layout.minimumSize() if layout is not None else QSize()
        return size.width(), size.height()

    def sizeHint(self) -> QSize:
        content_w, content_h = self.content_size()
        return QSize(content_w + TABLE_ITEM_PAD_H, content_h + TABLE_ITEM_PAD_V + TABLE_GRID)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class FlowImageCell(TableCell):
    """多图单元格容器：内部用 FlowLayout 让缩略图自动换行。

    外层被表格拉伸到整行高度，内层 host 按内容实际高度固定并垂直居中，
    保证“只有 1 张图但行被长文本撑高”时图片仍在行的中线上。
    sizeHint 内容宽度限制为“一行最多 N 张”，配合表头的 ResizeToContents，
    避免某条记录图片特别多时把整列撑得超宽；高度通过 heightForWidth
    随实际列宽精确计算，供表格做行高自适应。
    """

    def __init__(self, max_content_width: int, parent=None):
        super().__init__(parent)
        self._max_content_width = max_content_width
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._host = QWidget()
        self.flow = FlowLayout(
            self._host,
            margin=TABLE_CELL_PAD,
            hspace=TABLE_THUMB_GAP,
            vspace=TABLE_THUMB_GAP,
        )
        # 上下弹性空间使图片组垂直居中；host 水平方向仍填满整列宽度
        outer.addStretch(1)
        outer.addWidget(self._host)
        outer.addStretch(1)

    def add_thumb(self, widget: QWidget) -> None:
        self.flow.addWidget(widget)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 容器宽度变化后，按实际物理宽度重新换行并固定内层高度，实现垂直居中
        self._host.setFixedHeight(self.flow.heightForWidth(max(1, self.width())))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self.flow.heightForWidth(width)

    def content_size(self) -> tuple[int, int]:
        # 所有图排一行的自然宽度与“一行最多 N 张”的上限取小，限制列宽
        width = min(self.flow.natural_width(), self._max_content_width)
        return width, self.flow.heightForWidth(width)


class RecordTable(QTableWidget):
    """素材记录表格：第 0 列为勾选列，其后按 RECORD_FIELDS 顺序渲染记录"""

    # 用户双击图片占位区或按 Ctrl+V / 右键粘贴，请求粘贴图片（行、列、字段名）
    image_paste_requested = pyqtSignal(int, int, str)
    # 右键删除某张图片（渲染行、字段名、该图在图片列表中的序号；单图字段序号为 0）
    image_delete_requested = pyqtSignal(int, str, int)
    # 右键菜单：请求修改 / 删除某行（行号）
    edit_requested = pyqtSignal(int)
    delete_requested = pyqtSignal(int)
    # 勾选数量变化（当前勾选行数）
    selection_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rendered_indices: list[int] = []   # 渲染行号 -> 原始记录列表索引
        # 待懒加载任务 (渲染行, 列, 缩略图QLabel, 缩略图边长, 图片路径)
        self._image_jobs: list[tuple] = []
        self._loaded_thumbs: set[int] = set()    # 已解码完成的缩略图（按 id 去重）
        self._row_checks: dict[int, QCheckBox] = {}  # 渲染行 -> 勾选框
        self._load_timer = QTimer(self)
        self._load_timer.timeout.connect(self._process_image_batch)
        # 列宽变化时防抖重算行高
        self._row_resize_timer = QTimer(self)
        self._row_resize_timer.setSingleShot(True)
        self._row_resize_timer.timeout.connect(self._adjust_row_heights)

        self._init_table()
        # 滚动时增量加载新进入可见区的图片
        self.verticalScrollBar().valueChanged.connect(self._schedule_load_visible)

    def _init_table(self):
        # 第 0 列：勾选列；其后为数据列
        self.setColumnCount(len(TABLE_HEADERS) + 1)
        self.setHorizontalHeaderLabels([SELECT_COL_HEADER, *TABLE_HEADERS])
        self.setAlternatingRowColors(True)
        self.setWordWrap(True)  # 长文本（标题/评价）按列宽自动换行，配合行高自适应
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        header = self.horizontalHeader()
        # 勾选列固定窄宽，不参与拉伸、不允许拖宽
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, SELECT_COLUMN_WIDTH)
        for col, mode in enumerate(TABLE_COLUMN_MODES, start=1):
            resize = QHeaderView.ResizeMode.Stretch if mode else QHeaderView.ResizeMode.ResizeToContents
            header.setSectionResizeMode(col, resize)
        # 拖拽列宽 / 窗口拉伸导致列宽变化后，重新自适应行高并校正表头勾选框位置
        header.sectionResized.connect(self._on_section_resized)
        self.horizontalScrollBar().valueChanged.connect(self._position_header_check)

        # 表头浮动的全选复选框（三态）
        self.header_check = QCheckBox(header)
        self.header_check.setToolTip("全选 / 取消全选")
        self.header_check.clicked.connect(self._toggle_all)

        # 右键菜单（非图片区域：整行 修改/删除）
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _on_section_resized(self, *_args) -> None:
        self._position_header_check()
        self._row_resize_timer.start(_ROW_RESIZE_DEBOUNCE)

    # ---------- 渲染 ----------
    def render(self, records: list, indices: list | None = None) -> None:
        """渲染记录列表（完整列表或搜索过滤后的列表均可）。

        indices: 每个渲染行对应的原始记录列表索引；
                 为 None 时视为渲染的就是原始列表。
        """
        self._rendered_indices = list(indices) if indices is not None else list(range(len(records)))
        self._image_jobs = []
        self._loaded_thumbs.clear()
        self._row_checks.clear()
        self._load_timer.stop()
        self._row_resize_timer.stop()

        self.setUpdatesEnabled(False)
        self.setRowCount(0)
        self.setRowCount(len(records))
        for row, record in enumerate(records):
            self._render_select_cell(row)
            for field_col, field in enumerate(RECORD_FIELDS):
                col = field_col + 1  # 数据列整体右移一位（第 0 列为勾选列）
                value = record.get(field, "")
                if field in MULTI_IMAGE_FIELDS:
                    paths = value if isinstance(value, list) else ([value] if value else [])
                    self._render_image_cell(row, col, paths, field, multi=True)
                elif field in SINGLE_IMAGE_FIELDS:
                    self._render_image_cell(row, col, [value] if value else [], field, multi=False)
                else:
                    text = "" if value is None else str(value)
                    item = QTableWidgetItem(text)
                    if text:
                        # 行高有上限，超长文本悬停可看全文
                        item.setToolTip(text)
                    self.setItem(row, col, item)
        self.setUpdatesEnabled(True)

        self._reset_header_check()
        self._position_header_check()
        # 列宽在本轮事件处理后才稳定，延迟一帧再按实际内容自适应行高
        QTimer.singleShot(0, self._adjust_row_heights)
        QTimer.singleShot(0, self._position_header_check)
        # 首屏图片立即开始分片加载
        self._schedule_load_visible()

    def rendered_index(self, row: int) -> int:
        """渲染行号 -> 原始记录列表索引（供修改/删除定位真实记录）"""
        return self._rendered_indices[row]

    # ---------- 勾选列 ----------
    def _render_select_cell(self, row: int) -> None:
        self.setItem(row, 0, self._make_readonly_item(""))
        host = QWidget()
        lay = QHBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        check = QCheckBox()
        check.setToolTip("勾选该商品")
        lay.addWidget(check, alignment=Qt.AlignmentFlag.AlignCenter)
        self.setCellWidget(row, 0, host)
        self._row_checks[row] = check
        check.stateChanged.connect(self._on_row_check_changed)

    def _position_header_check(self, *_args) -> None:
        """把全选复选框放到第 0 列表头的正中央"""
        header = self.horizontalHeader()
        x = header.sectionViewportPosition(0) + (self.columnWidth(0) - _HEADER_CHECK_SIZE) // 2
        y = max(0, (header.height() - _HEADER_CHECK_SIZE) // 2)
        self.header_check.setGeometry(x, y, _HEADER_CHECK_SIZE, _HEADER_CHECK_SIZE)
        self.header_check.raise_()

    def _reset_header_check(self) -> None:
        """render 后勾选状态全部清零"""
        check = self.header_check
        check.blockSignals(True)
        check.setTristate(False)
        check.setCheckState(Qt.CheckState.Unchecked)
        check.blockSignals(False)

    def _on_row_check_changed(self, *_args) -> None:
        self._sync_header_check()
        self.selection_changed.emit(self.selected_count())

    def _sync_header_check(self) -> None:
        """根据各行勾选状态同步表头复选框：全选 / 半选 / 未选"""
        total = self.rowCount()
        checked = self.selected_count()
        check = self.header_check
        check.blockSignals(True)
        if checked == 0 or total == 0:
            check.setTristate(False)
            check.setCheckState(Qt.CheckState.Unchecked)
        elif checked == total:
            check.setTristate(False)
            check.setCheckState(Qt.CheckState.Checked)
        else:
            check.setTristate(True)
            check.setCheckState(Qt.CheckState.PartiallyChecked)
        check.blockSignals(False)

    def _toggle_all(self) -> None:
        """用户点击表头复选框：当前非全选（含半选）则全选，已全选则全部取消"""
        select_all = self.header_check.checkState() == Qt.CheckState.Checked
        state = Qt.CheckState.Checked if select_all else Qt.CheckState.Unchecked
        for row_check in self._row_checks.values():
            row_check.blockSignals(True)
            row_check.setCheckState(state)
            row_check.blockSignals(False)
        self._sync_header_check()
        self.selection_changed.emit(self.selected_count())

    def selected_count(self) -> int:
        """当前勾选的行数"""
        return sum(1 for check in self._row_checks.values() if check.isChecked())

    def selected_rendered_indices(self) -> list[int]:
        """勾选行对应的原始记录索引列表（供后续批量操作使用）"""
        return [self._rendered_indices[row] for row, check in self._row_checks.items() if check.isChecked()]

    # ---------- 键盘粘贴（Ctrl+V） ----------
    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Paste) and self._paste_to_current_cell():
            return
        super().keyPressEvent(event)

    def _paste_to_current_cell(self) -> bool:
        """当前焦点单元格是图片列时，发出粘贴请求"""
        row, col = self.currentRow(), self.currentColumn()
        if row < 0 or col <= 0:
            return False
        field = RECORD_FIELDS[col - 1]
        if field not in IMAGE_FIELDS:
            return False
        self.image_paste_requested.emit(row, col, field)
        return True

    # ---------- 图片单元格 ----------
    def _render_image_cell(self, row: int, col: int, paths: list, field_name: str, multi: bool) -> None:
        """渲染图片单元格：有图片走懒加载队列，无图片显示粘贴占位。

        底层 item 不存路径文本且不可编辑，避免图片背后露出/编辑出本地链接。
        多图时所有有效图片都创建缩略图并排在单元格内（不再只显示第一张）。
        """
        self.setItem(row, col, self._make_readonly_item(""))
        valid = [p for p in paths if p and os.path.exists(p)]
        if not valid:
            self._set_paste_placeholder(row, col, field_name, multi)
            return

        if multi:
            thumb_size = TABLE_MULTI_THUMB
            max_content_width = (
                TABLE_MULTI_PER_ROW * thumb_size
                + (TABLE_MULTI_PER_ROW - 1) * TABLE_THUMB_GAP
                + 2 * TABLE_CELL_PAD
            )
            container = FlowImageCell(max_content_width)
            for index, path in enumerate(valid):
                thumb = self._make_thumb(thumb_size, valid, index)
                self._bind_image_menu(thumb, row, col, field_name, valid, index, multi)
                container.add_thumb(thumb)
                self._image_jobs.append((row, col, thumb, thumb_size, path))
        else:
            thumb_size = TABLE_SINGLE_THUMB
            container = TableCell()
            lay = QHBoxLayout(container)
            lay.setContentsMargins(TABLE_CELL_PAD, TABLE_CELL_PAD, TABLE_CELL_PAD, TABLE_CELL_PAD)
            thumb = self._make_thumb(thumb_size, valid, 0)
            self._bind_image_menu(thumb, row, col, field_name, valid, 0, multi)
            lay.addWidget(thumb, alignment=Qt.AlignmentFlag.AlignCenter)
            self._image_jobs.append((row, col, thumb, thumb_size, valid[0]))

        self.setCellWidget(row, col, container)

    def _make_thumb(self, thumb_size: int, all_paths: list, index: int) -> QLabel:
        """创建一张缩略图占位 QLabel（图片稍后懒加载填入），双击打开大图并定位到该张"""
        thumb = QLabel()
        thumb.setFixedSize(thumb_size, thumb_size)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(_THUMB_STYLE)
        thumb.setToolTip("双击查看大图，右键可删除")
        thumb.mouseDoubleClickEvent = (
            lambda event, ps=all_paths, i=index: self.show_image_gallery(ps, i)
        )
        return thumb

    def _bind_image_menu(self, anchor: QWidget, row: int, col: int,
                         field_name: str, paths: list, img_index: int, multi: bool) -> None:
        """给缩略图绑定右键菜单：查看大图 / 粘贴 / 删除此图"""
        anchor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        anchor.customContextMenuRequested.connect(
            lambda pos, a=anchor, r=row, c=col, f=field_name, ps=paths, i=img_index, m=multi:
            self._show_image_menu(a, pos, r, c, f, ps, i, m)
        )

    def _show_image_menu(self, anchor: QWidget, pos, row: int, col: int,
                         field_name: str, paths: list, img_index: int, multi: bool) -> None:
        self.setCurrentCell(row, col)
        menu = QMenu(self)
        act_view = menu.addAction("查看大图")
        act_paste = menu.addAction("粘贴图片（Ctrl+V）")
        menu.addSeparator()
        act_delete = menu.addAction("删除此图片" if multi else "移除图片")
        chosen = menu.exec(anchor.mapToGlobal(pos))
        if chosen == act_view:
            self.show_image_gallery(paths, img_index)
        elif chosen == act_paste:
            self.image_paste_requested.emit(row, col, field_name)
        elif chosen == act_delete:
            self.image_delete_requested.emit(row, field_name, img_index)

    @staticmethod
    def _make_readonly_item(text: str = "") -> QTableWidgetItem:
        """创建不可编辑的单元格 item（勾选/图片列专用，防止误编辑）"""
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    # ---------- 行高自适应 ----------
    def _adjust_row_heights(self) -> None:
        """逐行按各列实际需要的高度设置行高：文本换行高度与图片排布高度取最大值"""
        count = self.rowCount()
        if count == 0:
            return
        font_metrics = self.fontMetrics()
        for row in range(count):
            needed = TABLE_ROW_MIN_HEIGHT
            for col in range(self.columnCount()):
                col_width = self.columnWidth(col)
                if col_width <= 0:
                    continue
                widget = self.cellWidget(row, col)
                if widget is not None:
                    if widget.hasHeightForWidth():
                        # hfw 按控件物理宽度（列宽扣除 item 横向 padding 与网格线）计算内容高，
                        # 再补回纵向被扣掉的部分得到所需行高
                        physical_w = max(1, col_width - TABLE_ITEM_PAD_H - TABLE_GRID)
                        content_h = widget.heightForWidth(physical_w)
                        needed = max(needed, content_h + TABLE_ITEM_PAD_V + TABLE_GRID)
                    else:
                        # TableCell.sizeHint 已统一补偿 item padding
                        needed = max(needed, widget.sizeHint().height())
                    continue
                item = self.item(row, col)
                text = item.text() if item is not None else ""
                if text:
                    text_width = max(20, col_width - TABLE_TEXT_HPAD)
                    text_rect = font_metrics.boundingRect(
                        QRect(0, 0, text_width, 0),
                        Qt.TextFlag.TextWordWrap,
                        text,
                    )
                    needed = max(needed, text_rect.height() + TABLE_TEXT_VPAD)
            height = max(TABLE_ROW_MIN_HEIGHT, min(needed, TABLE_ROW_MAX_HEIGHT))
            if self.rowHeight(row) != height:
                self.setRowHeight(row, height)

    # ---------- 图片懒加载 ----------
    def _schedule_load_visible(self, *args) -> None:
        """有未加载的图片且定时器空闲时，启动分片加载"""
        if self._image_jobs and not self._load_timer.isActive():
            self._load_timer.start(0)

    def _process_image_batch(self) -> None:
        """每个 tick 解码可见区内最多 _BATCH_CELLS 个缩略图"""
        if not self._image_jobs:
            self._load_timer.stop()
            return
        first, last = self._visible_row_range()
        pending = [
            job for job in self._image_jobs
            if first <= job[0] <= last and id(job[2]) not in self._loaded_thumbs
        ]
        for _row, _col, thumb, size, path in pending[:_BATCH_CELLS]:
            self._load_thumb(thumb, size, path)
        visible_done = all(
            id(job[2]) in self._loaded_thumbs
            for job in self._image_jobs if first <= job[0] <= last
        )
        if visible_done:
            self._load_timer.stop()

    def _visible_row_range(self) -> tuple:
        """当前可见行范围（含上下预加载行）；行高不一，使用 rowAt 按实际坐标映射"""
        count = self.rowCount()
        if count == 0:
            return 0, -1
        top = self.verticalScrollBar().value()
        view_h = max(self.viewport().height(), 1)
        first = self.rowAt(top)
        if first < 0:
            first = 0 if top <= 0 else count - 1
        last = self.rowAt(top + view_h - 1)
        if last < 0:
            last = count - 1
        return max(0, first - _PRELOAD_ROWS), min(count - 1, last + _PRELOAD_ROWS)

    def _load_thumb(self, thumb: QLabel, thumb_size: int, image_path: str) -> None:
        """解码缩略图并填入对应的 QLabel；失败显示“无效”占位文字"""
        pixmap = scaled_pixmap(image_path, thumb_size)
        self._loaded_thumbs.add(id(thumb))
        if pixmap is None:
            thumb.setText("无效")
            return
        thumb.setPixmap(pixmap)

    # ---------- 粘贴占位 ----------
    def _set_paste_placeholder(self, row: int, col: int, field_name: str, multi: bool = False) -> None:
        placeholder = QLabel("（粘贴图片）")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(_PLACEHOLDER_STYLE)
        if multi:
            # 多图列占位宽度按两张图预留，直观提示此处可放多张
            place_w = TABLE_MULTI_THUMB * 2 + TABLE_THUMB_GAP + 2 * TABLE_CELL_PAD
            place_h = TABLE_MULTI_THUMB + 2 * TABLE_CELL_PAD
        else:
            place_w = place_h = TABLE_SINGLE_THUMB + 2 * TABLE_CELL_PAD
        # 固定占位框自身尺寸，再用容器居中：行被其他内容撑高时虚线框不会被拉长
        placeholder.setFixedSize(place_w, place_h)
        placeholder.setToolTip("双击或按 Ctrl+V 粘贴剪贴板图片，右键更多操作")
        placeholder.mouseDoubleClickEvent = (
            lambda event, r=row, c=col, f=field_name: self.image_paste_requested.emit(r, c, f)
        )
        placeholder.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        placeholder.customContextMenuRequested.connect(
            lambda pos, a=placeholder, r=row, c=col, f=field_name: self._show_placeholder_menu(a, pos, r, c, f)
        )
        host = TableCell()
        host_lay = QHBoxLayout(host)
        host_lay.setContentsMargins(0, 0, 0, 0)
        host_lay.addWidget(placeholder, alignment=Qt.AlignmentFlag.AlignCenter)
        self.setCellWidget(row, col, host)

    def _show_placeholder_menu(self, anchor: QWidget, pos, row: int, col: int, field_name: str) -> None:
        self.setCurrentCell(row, col)
        menu = QMenu(self)
        act_paste = menu.addAction("粘贴图片（Ctrl+V）")
        if menu.exec(anchor.mapToGlobal(pos)) == act_paste:
            self.image_paste_requested.emit(row, col, field_name)

    # ---------- 右键菜单（整行：修改/删除） ----------
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

        state = {"index": max(0, min(index, len(paths) - 1))}

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
