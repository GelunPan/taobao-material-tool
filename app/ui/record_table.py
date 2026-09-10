"""素材表格组件：负责记录渲染、勾选、图片单元格、查看大图与粘贴/删图/复制/添加请求。

表格只负责“显示”与“发出请求”，数据如何保存由 MainWindow 编排。

显示规则（行高随内容自适应，不设上限）：
- 文本列开启自动换行，行高随文本长度与当前列宽增长，长标题/长评价完整展示
- 单图列（规格图/链接主图）居中显示一张缩略图
- 多图列（评价图片）缩略图流式排列：折叠态最多显示 TABLE_MULTI_VISIBLE 张
  （两行），超出出现低调的“展开/收起”小按钮；末尾始终有“+”块可继续添加
- 列宽被拖拽或窗口拉伸变化时，行高自动重算，排版始终整齐

交互：
- 双击缩略图：复制该图片到剪贴板；查看大图保留在右键菜单
- 图片格右键：查看大图 / 粘贴 / 删除 / 修改或复制该条记录；“+”块左键添加文件、双击或 Ctrl+V 粘贴
- 表头只有一个复选框（模式开关与全选合一）：未开启时位于商品ID表头，勾选即显示行勾选列并全选；
  批量模式下全选态再点一次退出、勾选列隐藏，半选态点击则补齐全选

列宽与列顺序：数据列均可拖拽宽度、拖动表头换位，勾选逻辑列锁定最左。

性能：文本批量渲染（关闭重绘后统一刷新）；缩略图按可见区分片懒加载，
解码走 image_utils.scaled_pixmap 并带缓存，记录量大也不卡。
"""
import os

from PyQt6.QtCore import QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence
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
    ASSETS_DIR,
    FULL_IMAGE_MAX_SIZE,
    IMAGE_FIELDS,
    MULTI_IMAGE_FIELDS,
    RECORD_FIELDS,
    SELECT_COLUMN_WIDTH,
    SELECT_COL_HEADER,
    SINGLE_IMAGE_FIELDS,
    TABLE_CELL_PAD,
    TABLE_COLUMN_MODES,
    TABLE_DEFAULT_COL_WIDTH,
    TABLE_GRID,
    TABLE_HEADERS,
    TABLE_ITEM_PAD_H,
    TABLE_ITEM_PAD_V,
    TABLE_MULTI_PER_ROW,
    TABLE_MULTI_THUMB,
    TABLE_MULTI_BTN_H,
    TABLE_MULTI_VISIBLE,
    TABLE_ROW_MIN_HEIGHT,
    TABLE_SHORT_COL_MAX_WIDTH,
    TABLE_SINGLE_THUMB,
    TABLE_STRETCH_MIN_WIDTH,
    TABLE_TEXT_HPAD,
    TABLE_TEXT_VPAD,
    TABLE_THUMB_GAP,
    TABLE_COL_INIT_SCALE,
    ROW_HEADER_WIDTH,
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

# 缩略图不画白底/边框：直接透出单元格（含选中行）背景，图片即“实际尺寸”贴片
_THUMB_STYLE = "background: transparent; border: none;"
_PLACEHOLDER_STYLE = (
    "color:#A8ABB2; border:1px dashed #C0C4CC; border-radius:6px; background:transparent;"
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


class MultiImageCell(TableCell):
    """评价图片单元格：缩略图流式排列 + 底部独立的小按钮行。

    - 缩略图只进流式布局，自动换行整齐排列；折叠态最多 TABLE_MULTI_VISIBLE 张
    - 按钮另起一行（▼/▲ 展开收起、+ 添加），永远不与缩略图挤在同一行，避免错乱
    - “+”点击请求文件选择并追加；没有图片时也只显示这一个小按钮
    indexed_paths 为 [(该图在记录列表中的真实序号, 路径), ...]（已过滤缺失文件），
    删除时用真实序号，避免缺文件时删错图。
    """

    def __init__(self, table: "RecordTable", row: int, col: int, field_name: str,
                 indexed_paths: list, thumb_size: int, max_content_width: int, parent=None):
        super().__init__(parent)
        self._table = table
        self._row, self._col, self._field = row, col, field_name
        self._indexed_paths = indexed_paths
        self._thumb_size = thumb_size
        self._max_content_width = max_content_width
        self._expanded = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        self._host = QWidget()
        host_lay = QVBoxLayout(self._host)
        host_lay.setContentsMargins(0, 0, 0, 0)
        host_lay.setSpacing(4)

        # 缩略图流式区
        self._flow_host = QWidget()
        self.flow = FlowLayout(
            self._flow_host,
            margin=TABLE_CELL_PAD,
            hspace=TABLE_THUMB_GAP,
            vspace=TABLE_THUMB_GAP,
        )
        host_lay.addWidget(self._flow_host)

        # 小按钮行：展开/收起符号 + 添加符号，左对齐
        self._btn_row = QWidget()
        btn_lay = QHBoxLayout(self._btn_row)
        btn_lay.setContentsMargins(TABLE_CELL_PAD, 0, TABLE_CELL_PAD, 0)
        btn_lay.setSpacing(6)
        self._more_btn = QPushButton("▼")
        self._more_btn.setObjectName("imgMoreBtn")
        self._more_btn.setFixedSize(30, TABLE_MULTI_BTN_H)
        self._more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_btn.clicked.connect(self._toggle_expanded)
        self._add_btn = QPushButton("+")
        self._add_btn.setObjectName("imgAddBtn")
        self._add_btn.setFixedSize(30, TABLE_MULTI_BTN_H)
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setToolTip("添加图片（从文件选择并追加）")
        self._add_btn.clicked.connect(
            lambda: self._table.image_add_requested.emit(self._row, self._field)
        )
        btn_lay.addWidget(self._more_btn)
        btn_lay.addWidget(self._add_btn)
        btn_lay.addStretch()
        host_lay.addWidget(self._btn_row)

        outer.addWidget(self._host)
        outer.addStretch(1)
        self.rebuild()
        # 单元格空白处（缩略图间隙、按钮行、空格）右键：粘贴图片 + 记录操作；
        # 缩略图自身右键另有“查看大图/删除此图”专属菜单，互不干扰
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda p, r=self._row, c=self._col, f=self._field:
            self._table._show_multi_cell_menu(p, r, c, f)
        )

    def clear_thumbs(self) -> None:
        """只清空流式区里的缩略图（按钮行保留）"""
        while self.flow.count():
            item = self.flow.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def rebuild(self) -> None:
        """按展开/折叠状态重建缩略图，并刷新展开按钮的符号与提示"""
        self.clear_thumbs()
        view_paths = [p for _, p in self._indexed_paths]
        visible = self._indexed_paths if self._expanded else self._indexed_paths[:TABLE_MULTI_VISIBLE]
        for view_index, (stored_index, path) in enumerate(visible):
            thumb = self._table.make_thumb(self._thumb_size, view_paths, view_index)
            self._table.bind_image_menu(
                thumb, self._row, self._col, self._field,
                view_paths, view_index, stored_index, multi=True,
            )
            self.flow.addWidget(thumb)
            self._table.enqueue_image_job(self._row, self._col, thumb, self._thumb_size, path)

        total = len(self._indexed_paths)
        hidden_count = total - TABLE_MULTI_VISIBLE
        if total > TABLE_MULTI_VISIBLE:
            self._more_btn.show()
            if self._expanded:
                self._more_btn.setText("▲")
                self._more_btn.setToolTip("收起图片")
            else:
                self._more_btn.setText("▼")
                self._more_btn.setToolTip(f"展开剩余 {hidden_count} 张图片")
        else:
            self._more_btn.hide()
        # 子控件数量变化后重算行高，并对新出现的缩略图继续懒加载
        self._table.after_cell_rebuilt()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 按单元格实际物理宽度重新换行并固定流式区高度
        self._flow_host.setFixedHeight(self.flow.heightForWidth(max(1, self.width())))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self.flow.heightForWidth(width) + 4 + TABLE_MULTI_BTN_H

    def content_size(self) -> tuple[int, int]:
        # 所有图排一行的自然宽度与“一行最多 N 张”的上限取小，限制列宽
        width = min(self.flow.natural_width(), self._max_content_width)
        height = self.flow.heightForWidth(width) + 4 + TABLE_MULTI_BTN_H
        return width, height

    def _toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self.rebuild()


class RecordTable(QTableWidget):
    """素材记录表格：第 0 列为勾选列（默认隐藏），其后按 RECORD_FIELDS 顺序渲染记录"""

    # 用户双击图片占位区或按 Ctrl+V / 右键粘贴，请求粘贴图片（行、列、字段名）
    image_paste_requested = pyqtSignal(int, int, str)
    # 右键删除某张图片（渲染行、字段名、该图在记录图片列表中的真实序号）
    image_delete_requested = pyqtSignal(int, str, int)
    # 双击缩略图：请求把该图片复制到剪贴板（图片路径）
    image_copy_requested = pyqtSignal(str)
    # 多图单元格“+”块：请求从文件选择并追加图片（渲染行、字段名）
    image_add_requested = pyqtSignal(int, str)
    # 右键菜单：请求修改 / 复制 / 删除某行（行号）
    edit_requested = pyqtSignal(int)
    record_copy_requested = pyqtSignal(int)
    delete_requested = pyqtSignal(int)
    # 勾选数量变化（当前勾选行数）
    selection_changed = pyqtSignal(int)
    # 文本格就地编辑完成（渲染行、字段名、新文本）
    cell_edited = pyqtSignal(int, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rendered_indices: list[int] = []   # 渲染行号 -> 原始记录列表索引
        # 待懒加载任务 (渲染行, 列, 缩略图QLabel, 缩略图边长, 图片路径)
        self._image_jobs: list[tuple] = []
        self._loaded_thumbs: set[int] = set()    # 已解码完成的缩略图（按 id 去重）
        self._row_checks: dict[int, QCheckBox] = {}  # 渲染行 -> 勾选框
        # 用户手动拖过宽度的逻辑列（自动布局不再覆盖，尊重用户调整）
        self._user_resized: set[int] = set()
        # 程序内部批量设置列宽时置 True，避免 sectionResized 误判为用户拖拽
        self._layout_guard = False
        self._load_timer = QTimer(self)
        self._load_timer.timeout.connect(self._process_image_batch)
        # 列宽变化时防抖重算行高
        self._row_resize_timer = QTimer(self)
        self._row_resize_timer.setSingleShot(True)
        self._row_resize_timer.timeout.connect(self._adjust_row_heights)
        # 各逻辑列最小宽度（表头文字/图片内容宽），拖拽不能再缩小至此以下
        self._col_mins: dict[int, int] = {}
        # 拖拽列宽结束后再统一找平弹性列：拖拽过程中同步重排会抢走鼠标手柄，
        # 视觉上表现为“拖完又弹回去”，故先记录、延迟 120ms 再分配
        self._col_distribute_timer = QTimer(self)
        self._col_distribute_timer.setSingleShot(True)
        self._col_distribute_timer.timeout.connect(self._distribute_stretch_columns)

        self._init_table()
        # 滚动时增量加载新进入可见区的图片
        self.verticalScrollBar().valueChanged.connect(self._schedule_load_visible)

    def _init_table(self):
        # 第 0 列：勾选列；其后为数据列（商品ID 标题前留空，放置模式开关复选框）
        self.setColumnCount(len(TABLE_HEADERS) + 1)
        header_labels = [SELECT_COL_HEADER, *TABLE_HEADERS]
        header_labels[1] = "    " + header_labels[1]
        self.setHorizontalHeaderLabels(header_labels)
        self.setAlternatingRowColors(True)
        self.setWordWrap(True)  # 长文本（标题/评价）按列宽自动换行，配合行高自适应
        # 选择规则：
        # - 点内容单元格：只选中该格（SelectItems）
        # - 点顶部字段表头：选中该列全部数据（QTableView 水平表头默认行为）
        # - 点最左侧行号（1/2/3…）：选中整行（垂直表头默认行为）
        # ExtendedSelection 下普通点击仍是单选，同时承载整行/整列选择，
        # 并保留 Ctrl/Shift 扩展选择（SingleSelection 无法选整行/整列）
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        # 仅双击文本格进入就地编辑（图片格是 cellWidget、不参与 item 编辑；
        # 单击不进编辑）。编辑结束由 cell_edited 信号落库，图片/勾选列仍只读
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.cellChanged.connect(self._on_cell_changed)
        # 最左侧行号（垂直表头）：固定窄宽、数字居中，点击行号即选中整行
        vheader = self.verticalHeader()
        vheader.setFixedWidth(ROW_HEADER_WIDTH)
        # 双保险：min/max 同值，多次拖拽列宽/窗口缩放后行号列也不会被压窄
        vheader.setMinimumWidth(ROW_HEADER_WIDTH)
        vheader.setMaximumWidth(ROW_HEADER_WIDTH)
        vheader.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        vheader.setToolTip("点击行号选中整行")
        header = self.horizontalHeader()
        header.setMinimumSectionSize(SELECT_COLUMN_WIDTH)  # 列宽下限（勾选列即此宽度），再窄出横向滚动条
        # 勾选列固定窄宽，不参与拉伸、不允许拖宽
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, SELECT_COLUMN_WIDTH)
        # 数据列全部 Interactive：宽度可拖拽、可由程序分配，初始宽度在渲染后计算
        for col in range(1, self.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        # 按住表头标签可拖动交换列位置
        header.setSectionsMovable(True)
        header.sectionMoved.connect(self._on_section_moved)
        # 拖拽列宽 / 窗口拉伸导致列宽变化后，重新自适应行高并校正表头勾选框位置
        header.sectionResized.connect(self._on_section_resized)
        self.horizontalScrollBar().valueChanged.connect(self._position_header_check)

        # 表头开关：一个和文字差不多大的小按钮。未进入时显示“选择”，
        # 点一下进入批量选择（行勾选列出现、全不选）并变为“取消选择”，再点退出
        self.header_check = QPushButton("", self)
        self.header_check.setObjectName("selectBtn")
        self.header_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header_check.setIcon(QIcon(str(ASSETS_DIR / "select.svg")))
        self.header_check.setIconSize(QSize(20, 20))
        self.header_check.setStyleSheet(
            "QPushButton#selectBtn { padding: 0px; border: 1px solid transparent; border-radius: 3px;"
            "  background: transparent; }"
            "QPushButton#selectBtn:hover { border-color: #c0c4cc; background: #f5f7fa; }"
        )
        self.header_check.setToolTip("点击进入批量选择")
        self.header_check.clicked.connect(self._on_header_btn_clicked)
        self.setColumnHidden(0, True)

        # 右键菜单（非图片区域：整行 修改/删除）
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # ---------- 批量选择模式（勾选列显隐） ----------
    def set_selection_mode(self, on: bool) -> None:
        """对外的模式切换入口：开启批量选择 / 退出批量选择"""
        hidden = self.isColumnHidden(0)
        if on and hidden:
            self._enter_selection_mode()
        elif not on and not hidden:
            self._exit_selection_mode()

    def _enter_selection_mode(self) -> None:
        """显示行勾选列，全部未勾选，由用户自行勾选要操作的行；左上角按钮变为“取消选择”"""
        self.setColumnHidden(0, False)
        self._set_all_rows(False)
        self.header_check.setIcon(QIcon(str(ASSETS_DIR / "cancel_select.svg")))
        self.header_check.setToolTip("点击退出批量选择")
        self._after_mode_toggle()

    def _exit_selection_mode(self) -> None:
        """清空行勾选并隐藏勾选列，表头复选框移回商品ID列、恢复未勾选"""
        self._set_all_rows(False, sync=False)
        self.setColumnHidden(0, True)
        self.header_check.setToolTip("点击进入批量选择")
        self.selection_changed.emit(0)
        self.header_check.setIcon(QIcon(str(ASSETS_DIR / "select.svg")))
        self._after_mode_toggle()

    def _set_all_rows(self, checked: bool, sync: bool = True) -> None:
        """统一设置全部行勾选；sync 为真时同步表头三态与已选计数"""
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row_check in self._row_checks.values():
            row_check.blockSignals(True)
            row_check.setCheckState(state)
            row_check.blockSignals(False)
        if sync:
            self._sync_header_check()
            self.selection_changed.emit(self.selected_count())

    def _after_mode_toggle(self) -> None:
        self._distribute_stretch_columns()
        self._adjust_row_heights()
        self._position_header_check()

    def _on_header_btn_clicked(self) -> None:
        """表头开关按钮：未进入→进入批量选择；已进入→退出（取消选择）"""
        if self.isColumnHidden(0):
            self._enter_selection_mode()
        else:
            self._exit_selection_mode()

    # ---------- 列顺序 / 列宽 ----------
    def _on_section_moved(self, logical_index: int, old_visual: int, new_visual: int) -> None:
        """勾选列（逻辑第 0 列）必须固定在最左：拖走它或把别的列拖到它前面都还原"""
        if logical_index == 0 or new_visual == 0:
            header = self.horizontalHeader()
            header.blockSignals(True)
            header.moveSection(new_visual, old_visual)
            header.blockSignals(False)

    def _on_section_resized(self, logical_index: int, _old: int, _new: int) -> None:
        self._position_header_check()
        if self._layout_guard:
            return
        # 拖拽过程中：只记住用户调整并夹到最小宽，延迟到拖拽停止后再统一找平，
        # 避免拖拽中重排其他列把手柄抢走、造成“拖完弹回去”
        self._user_resized.add(logical_index)
        self._clamp_column_width(logical_index)
        self._col_distribute_timer.start(120)
        self._row_resize_timer.start(_ROW_RESIZE_DEBOUNCE)

    def _clamp_column_width(self, logical_index: int) -> None:
        """用户把列拖得过窄时，夹回到该列最小宽度（表头文字/图片内容不能被挤没）"""
        if self._layout_guard:
            return
        min_w = self._col_mins.get(logical_index)
        if not min_w or self.columnWidth(logical_index) >= min_w:
            return
        self._layout_guard = True
        self.setColumnWidth(logical_index, min_w)
        self._layout_guard = False

    def resizeEvent(self, event) -> None:
        """窗口宽度变化时，弹性列同步缩小/放大（普通列宽度保持不变）"""
        super().resizeEvent(event)
        self._distribute_stretch_columns()

    def reset_column_layout(self) -> None:
        """清除用户手动列宽记忆并重新自适应（供外部“恢复默认列宽”使用）"""
        self._user_resized.clear()
        self._auto_fit_columns()

    def _field_min_width(self, field: str, header_text: str) -> int:
        """该列不能再小的宽度：表头文字要能完整显示，图片列不小于其内容宽"""
        text_w = self.fontMetrics().horizontalAdvance(header_text) + TABLE_ITEM_PAD_H + 22
        if field in SINGLE_IMAGE_FIELDS:
            img_w = TABLE_SINGLE_THUMB + 2 * TABLE_CELL_PAD + TABLE_ITEM_PAD_H + TABLE_GRID
            return max(text_w, img_w)
        if field in MULTI_IMAGE_FIELDS:
            content_w = (
                TABLE_MULTI_PER_ROW * TABLE_MULTI_THUMB
                + (TABLE_MULTI_PER_ROW - 1) * TABLE_THUMB_GAP
                + 2 * TABLE_CELL_PAD
            )
            return max(text_w, content_w + TABLE_ITEM_PAD_H + TABLE_GRID)
        return max(text_w, 88)  # 普通文本列不低于 88px：短ID/短规格一行不省略

    def _auto_fit_columns(self) -> None:
        """渲染后初始化列宽：图片列贴合缩略图尺寸，文本普通列按内容收缩，
        弹性列最后平分剩余空间；初始宽统一按设计值的 60% 紧凑呈现。
        用户手动拖过的列保持不动。"""
        self._layout_guard = True
        try:
            for field_col, field in enumerate(RECORD_FIELDS):
                col = field_col + 1
                self._col_mins[col] = self._field_min_width(field, TABLE_HEADERS[field_col])
                if col in self._user_resized or TABLE_COLUMN_MODES[field_col] == 1:
                    continue
                if field in SINGLE_IMAGE_FIELDS:
                    width = TABLE_SINGLE_THUMB + 2 * TABLE_CELL_PAD + TABLE_ITEM_PAD_H + TABLE_GRID
                elif field in MULTI_IMAGE_FIELDS:
                    content_w = (
                        TABLE_MULTI_PER_ROW * TABLE_MULTI_THUMB
                        + (TABLE_MULTI_PER_ROW - 1) * TABLE_THUMB_GAP
                        + 2 * TABLE_CELL_PAD
                    )
                    width = content_w + TABLE_ITEM_PAD_H + TABLE_GRID
                else:
                    # 按当前内容自适应，再夹在“较小默认值”与上限之间
                    self.resizeColumnToContents(col)
                    width = self.columnWidth(col)
                    width = max(TABLE_DEFAULT_COL_WIDTH, min(width, TABLE_SHORT_COL_MAX_WIDTH))
                # 初始宽紧凑化，并保证不小于该列最小宽
                width = int(width * TABLE_COL_INIT_SCALE)
                width = max(width, self._col_mins[col])
                self.setColumnWidth(col, width)
        finally:
            self._layout_guard = False
        self._distribute_stretch_columns()

    @staticmethod
    def _stretch_logical_columns() -> list[int]:
        """弹性列的逻辑列号（TABLE_COLUMN_MODES=1，加 1 是因为第 0 列为勾选列）"""
        return [idx + 1 for idx, mode in enumerate(TABLE_COLUMN_MODES) if mode == 1]

    def _distribute_stretch_columns(self) -> None:
        """弹性列平分剩余视口宽度；用户手动拖过的弹性列保留其宽度不参与分配。
        剩余空间不足时压到最小宽度并由横向滚动条兜底；隐藏列（如未开启的勾选列）
        不占用宽度。"""
        free_cols = [c for c in self._stretch_logical_columns() if c not in self._user_resized]
        if not free_cols or self.columnCount() == 0:
            return
        self._layout_guard = True
        try:
            fixed_used = sum(
                self.columnWidth(c)
                for c in range(self.columnCount())
                if c not in free_cols and not self.isColumnHidden(c)
            )
            avail = max(
                self.viewport().width() - fixed_used,
                TABLE_STRETCH_MIN_WIDTH * len(free_cols),
            )
            per = avail // len(free_cols)
            for col in free_cols:
                self.setColumnWidth(col, per)
        finally:
            self._layout_guard = False

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
        self.blockSignals(True)  # 批量 setItem 不触发 cellChanged，避免误写库
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
                    item = self._make_readonly_item(text)
                    if text:
                        # 完整内容已靠换行展示，tooltip 仅作悬停速览
                        item.setToolTip(text)
                    self.setItem(row, col, item)
        self.blockSignals(False)
        self.setUpdatesEnabled(True)

        self._reset_header_check()
        self._position_header_check()
        # 列宽在本轮事件处理后才稳定：先自适应列宽，再按最终列宽重算行高
        QTimer.singleShot(0, self._auto_fit_columns)
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
        # 勾选时同步当前行：修复“只点勾选框时 currentRow 不更新，
        # 导致底部修改/删除按钮总是作用在第一条记录”的问题
        check.clicked.connect(lambda _checked=False, r=row: self.setCurrentCell(r, 0))

    def _position_header_check(self, *_args) -> None:
        """把“选择/取消选择”按钮放到最左上角角格（行号列正上方）。
        按钮父对象是表格本身，(0,0) 即角格左上角，不随列滚动、不挡商品ID表头"""
        header = self.horizontalHeader()
        box_w = header.height()  # 图标按钮贴合角格高度
        self.header_check.setGeometry(0, 0, box_w, header.height())
        self.header_check.show()
        self.header_check.raise_()
        # 商品ID表头文字右对齐，给左上角按钮让出位置（数据格对齐不受影响）
        item = self.horizontalHeaderItem(1)
        if item is not None:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _reset_header_check(self) -> None:
        """render 后行勾选全部清零，按钮回到默认“选择”图标"""
        self.header_check.setIcon(QIcon(str(ASSETS_DIR / "select.svg")))

    def _on_row_check_changed(self, *_args) -> None:
        self._sync_header_check()
        self.selection_changed.emit(self.selected_count())

    def _sync_header_check(self) -> None:
        """勾选变化：表头不再有三态框，仅由底部“已选 N 条”反馈进度"""
        pass

    def total_min_width(self) -> int:
        """所有列最小宽 + 行号列 + 面板边距：左侧栏收起时右侧表格按此下限保持完整列宽，
        超出部分出横向滚动条，不再把列压到字段都看不见"""
        body = sum(self._col_mins.values()) if self._col_mins else 0
        return int(body + self.verticalHeader().width() + 24)

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
        """渲染图片单元格。

        底层 item 不存路径文本且不可编辑，避免图片背后露出/编辑出本地链接。
        多图：过滤掉磁盘缺失的文件但保留其在记录列表中的真实序号，
        交给 MultiImageCell（缩略图 + “+”块 + 折叠展开）；
        单图：有图显示一张缩略图，无图显示粘贴占位框。
        """
        self.setItem(row, col, self._make_readonly_item(""))

        if multi:
            thumb_size = TABLE_MULTI_THUMB
            indexed = [
                (stored_index, p)
                for stored_index, p in enumerate(paths)
                if p and os.path.exists(p)
            ]
            max_content_width = (
                TABLE_MULTI_PER_ROW * thumb_size
                + (TABLE_MULTI_PER_ROW - 1) * TABLE_THUMB_GAP
                + 2 * TABLE_CELL_PAD
            )
            container = MultiImageCell(self, row, col, field_name, indexed, thumb_size, max_content_width)
            self.setCellWidget(row, col, container)
            return

        valid = [p for p in paths if p and os.path.exists(p)]
        if not valid:
            self._set_paste_placeholder(row, col, field_name)
            return

        thumb_size = TABLE_SINGLE_THUMB
        container = TableCell()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(TABLE_CELL_PAD, TABLE_CELL_PAD, TABLE_CELL_PAD, TABLE_CELL_PAD)
        view_paths = [valid[0]]
        thumb = self.make_thumb(thumb_size, view_paths, 0)
        self.bind_image_menu(thumb, row, col, field_name, view_paths, 0, 0, multi=False)
        lay.addWidget(thumb, alignment=Qt.AlignmentFlag.AlignCenter)
        self.enqueue_image_job(row, col, thumb, thumb_size, valid[0])
        self.setCellWidget(row, col, container)

    def make_thumb(self, thumb_size: int, view_paths: list, index: int) -> QLabel:
        """创建一张缩略图占位 QLabel（图片稍后懒加载填入）。

        双击=复制该图片到剪贴板；查看大图/删除等操作统一收进右键菜单。
        """
        thumb = QLabel()
        thumb.setFixedSize(thumb_size, thumb_size)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(_THUMB_STYLE)
        thumb.setToolTip("双击复制图片，右键可查看大图或删除")
        path = view_paths[index]
        thumb.mouseDoubleClickEvent = lambda event, p=path: self.image_copy_requested.emit(p)
        return thumb

    def enqueue_image_job(self, row: int, col: int, thumb: QLabel, size: int, path: str) -> None:
        """把一张缩略图加入懒加载队列（单元格展开重建时也走这里）"""
        self._image_jobs.append((row, col, thumb, size, path))

    def after_cell_rebuilt(self) -> None:
        """单元格内部增删缩略图后：重算行高并继续懒加载可见区"""
        self._adjust_row_heights()
        self._schedule_load_visible()

    def select_image_cell(self, row: int, col: int) -> None:
        self.setCurrentCell(row, col)
        self.setFocus()

    def bind_image_menu(self, anchor: QWidget, row: int, col: int, field_name: str,
                        view_paths: list, view_index: int, stored_index: int, multi: bool) -> None:
        """给缩略图绑定右键菜单：查看大图 / 粘贴 / 删除此图，并在点击时选中所在格"""
        anchor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        anchor.customContextMenuRequested.connect(
            lambda pos, a=anchor, r=row, c=col, f=field_name, ps=view_paths,
                   vi=view_index, si=stored_index, m=multi:
            self._show_image_menu(a, pos, r, c, f, ps, vi, si, m)
        )
        # cellWidget 会拦截鼠标事件，表格不会自动更新 currentCell，这里手动补齐
        anchor.mousePressEvent = lambda event, r=row, c=col: self.select_image_cell(r, c)

    def _show_image_menu(self, anchor: QWidget, pos, row: int, col: int,
                         field_name: str, view_paths: list, view_index: int,
                         stored_index: int, multi: bool) -> None:
        self.setCurrentCell(row, col)
        menu = QMenu(self)
        act_view = menu.addAction("查看大图")
        act_paste = menu.addAction("粘贴图片（Ctrl+V）")
        menu.addSeparator()
        act_delete = menu.addAction("删除此图片" if multi else "移除图片")
        menu.addSeparator()
        act_edit = menu.addAction("修改记录")
        act_copy = menu.addAction("复制记录")
        chosen = menu.exec(anchor.mapToGlobal(pos))
        if chosen == act_view:
            self.show_image_gallery(view_paths, view_index)
        elif chosen == act_paste:
            self.image_paste_requested.emit(row, col, field_name)
        elif chosen == act_delete:
            self.image_delete_requested.emit(row, field_name, stored_index)
        elif chosen == act_edit:
            self.edit_requested.emit(row)
        elif chosen == act_copy:
            self.record_copy_requested.emit(row)

    @staticmethod
    def _make_readonly_item(text: str = "") -> QTableWidgetItem:
        """创建不可编辑的单元格 item（勾选/图片列专用，防止误编辑）"""
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _on_cell_changed(self, row: int, col: int) -> None:
        """文本格就地编辑完成：发出 (渲染行, 字段, 新文本) 由主窗口落库"""
        if col <= 0:
            return
        field = RECORD_FIELDS[col - 1]
        if field in IMAGE_FIELDS:
            return  # 图片列底层无文本，不会走到
        item = self.item(row, col)
        self.cell_edited.emit(row, field, "" if item is None else item.text())

    # ---------- 行高自适应 ----------
    def _adjust_row_heights(self) -> None:
        """逐行按各列实际需要的高度设置行高：文本换行高度与图片排布高度取最大值。
        不设高度上限，长文本完整换行展示，行高仅有下限保底。"""
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
            height = max(TABLE_ROW_MIN_HEIGHT, needed)
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

    # ---------- 粘贴占位（单图列无图时） ----------
    def _set_paste_placeholder(self, row: int, col: int, field_name: str) -> None:
        placeholder = QLabel("（粘贴图片）")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(_PLACEHOLDER_STYLE)
        place_w = place_h = TABLE_SINGLE_THUMB + 2 * TABLE_CELL_PAD
        # 固定占位框自身尺寸，再用容器居中：行被其他内容撑高时虚线框不会被拉长
        placeholder.setFixedSize(place_w, place_h)
        placeholder.setToolTip("双击或按 Ctrl+V 粘贴剪贴板图片，右键更多操作")
        placeholder.mouseDoubleClickEvent = (
            lambda event, r=row, c=col, f=field_name: self.image_paste_requested.emit(r, c, f)
        )
        # 点击占位框时手动选中所在单元格，保证随后的 Ctrl+V 能正确定位
        placeholder.mousePressEvent = lambda event, r=row, c=col: self.select_image_cell(r, c)
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
        menu.addSeparator()
        act_edit = menu.addAction("修改记录")
        act_copy = menu.addAction("复制记录")
        chosen = menu.exec(anchor.mapToGlobal(pos))
        if chosen == act_paste:
            self.image_paste_requested.emit(row, col, field_name)
        elif chosen == act_edit:
            self.edit_requested.emit(row)
        elif chosen == act_copy:
            self.record_copy_requested.emit(row)

    def _show_multi_cell_menu(self, pos, row: int, col: int, field_name: str) -> None:
        """多图单元格空白处右键：粘贴图片（Ctrl+V）/ 修改记录 / 复制记录。
        直接走表格级菜单定位（pos 相对 cellWidget，先映射为视口坐标）"""
        self.setCurrentCell(row, col)
        menu = QMenu(self)
        act_paste = menu.addAction("粘贴图片（Ctrl+V）")
        menu.addSeparator()
        act_edit = menu.addAction("修改记录")
        act_copy = menu.addAction("复制记录")
        anchor = self.cellWidget(row, col)
        chosen = menu.exec(anchor.mapToGlobal(pos))
        if chosen == act_paste:
            self.image_paste_requested.emit(row, col, field_name)
        elif chosen == act_edit:
            self.edit_requested.emit(row)
        elif chosen == act_copy:
            self.record_copy_requested.emit(row)

    # ---------- 右键菜单（整行：修改/删除） ----------
    def _show_context_menu(self, pos) -> None:
        row = self.rowAt(pos.y())
        if row < 0:
            return
        # 右键落在哪个格就选中哪个格（隐藏的勾选列回退到首个数据列），
        # 保证“修改记录/删除”始终作用于右键所在的这条记录
        col = self.columnAt(pos.x())
        if col <= 0:
            col = 1
        self.clearSelection()
        self.setCurrentCell(row, col)
        menu = QMenu(self)
        act_edit = menu.addAction("修改记录")
        act_copy = menu.addAction("复制记录")
        menu.addSeparator()
        act_delete = menu.addAction("删除记录")
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen == act_edit:
            self.edit_requested.emit(row)
        elif chosen == act_copy:
            self.record_copy_requested.emit(row)
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
