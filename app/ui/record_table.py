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

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QIcon, QKeySequence

def smart_split_spec(text: str) -> list:
    """智能把黏在一坨的规格文本拆成多条。

    支持分隔符：, ， | / 、 ; ； \n
    会去掉前缀如「颜色分类:」「商品规格:」「颜色:」。
    """
    if not text:
        return []
    # 先按 / 切（用户主要分隔符），其他分隔符也拆
    parts = re.split(r"[\n/、,，|;；]+", str(text))
    out = []
    for p in parts:
        p = p.strip().strip("/").strip()
        # 去掉「xxx:」「xxx：」前缀
        p = re.sub(r"^[^:：]{1,12}[:：]\s*", "", p)
        p = p.strip().strip(",").strip()
        if p and p not in out:
            out.append(p)
    return out


import re
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
    QTableWidgetSelectionRange,
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
    TABLE_CENTER_FIELDS,
    TABLE_COLUMN_MODES,
    TABLE_DEFAULT_COL_WIDTH,
    TABLE_GRID,
    TABLE_HEADERS,
    TABLE_ITEM_PAD_H,
    TABLE_ITEM_PAD_V,
    TABLE_LINK_COLUMN_WIDTH,
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
from ..services.link_utils import extract_product_url

# 可见区上下各预加载的行数（提前解码，滚动更顺滑）
_PRELOAD_ROWS = 8
# 每个定时器周期最多解码的单元格数（分片，防止单帧卡顿）
_BATCH_CELLS = 6
# 列宽变化后重算行高的防抖间隔（毫秒），拖拽列宽时不会频繁卡顿
_ROW_RESIZE_DEBOUNCE = 30
# 表头全选复选框边长（与 QSS 中 indicator 尺寸一致）
_HEADER_CHECK_SIZE = 16

# 「➕」号添加按钮与最后一行的默认间距（像素）
_ADD_BTN_GAP = 8
# 内容末尾之外额外放开的滚动余量（像素）：按钮悬浮在最后一行下方、不属于表格内容，
# 没有这截余量时滚动条滑到最大位置，最后一行底边正好贴住可用底边，按钮无处安放被隐藏
_ADD_BTN_BOTTOM_SPACE = 56

# 追加记录后滚到底部的缓动时长（毫秒）：够短不拖沓，又能看出是"滑过去"而非瞬移
_SCROLL_ANIM_MS = 260

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
            all_si = [si for si, _ in self._indexed_paths]
            self._table.bind_image_menu(
                thumb, self._row, self._col, self._field,
                view_paths, view_index, stored_index, multi=True,
                stored_indices=all_si,
            )
            self.flow.addWidget(thumb)
            self._table.enqueue_image_job(self._row, self._col, thumb, self._thumb_size, path)

        total = len(self._indexed_paths)
        hidden_count = total - TABLE_MULTI_VISIBLE
        # 截图模式下按钮一律不显示：这里也必须判断，否则任何一次 rebuild（懒加载、
        # 展开态同步等）都可能把已经隐藏的 ▼/＋ 又显示回来
        if total > TABLE_MULTI_VISIBLE and not self._table.is_capture_mode():
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

    def mousePressEvent(self, event) -> None:
        """点击单元格任意位置时，手动选中所在单元格，保证随后的 Ctrl+V/C/Del 能正确定位"""
        super().mousePressEvent(event)
        self._table.setCurrentCell(self._row, self._col)

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

    def is_expanded(self) -> bool:
        """当前是否处于展开态（导出截图时用来对齐主表格的展开/折叠状态）"""
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        """外部指定展开/折叠；状态没变则不做无谓重建"""
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self.rebuild()

    def set_capture_mode(self, on: bool) -> None:
        """进入/退出「截图模式」：隐藏缩略图下方的小按钮行（▼/▲ 展开收起、+ 添加）。

        这两个符号是交互入口、不属于表单内容，留档截图里不该出现。隐藏后布局会
        自然收拢，不会留下空白条；行高由 heightForWidth 决定，不受影响。
        """
        self._btn_row.setVisible(not on)

    def _toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)


class _SelectAllCheck(QCheckBox):
    """勾选列表头的「全选」复选框。

    Qt 三态框的默认点击行为是 未选 → 半选 → 全选 → 未选 循环，用户点一下会先停在
    半选态，很反直觉。这里覆写 nextCheckState，让点击**只在 全选 / 全不选 之间切换**；
    半选态只作为行勾选情况（选了一部分）的被动反映，用户点不出来。
    """

    def nextCheckState(self) -> None:
        self.setCheckState(
            Qt.CheckState.Unchecked
            if self.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )


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
    # 选区操作（Ctrl+A 全选 / 拖选一片区域 → 右键）：按**完整记录行**批量删除/复制，
    # 参数是原始记录索引列表，与单选的 delete_requested(row) 分开，互不干扰
    records_delete_requested = pyqtSignal(list)
    records_copy_requested = pyqtSignal(list)
    # 撤销：请求回退上一步（Ctrl+Z，焦点在表格内时由表格发出）
    undo_requested = pyqtSignal()
    # 勾选数量变化（当前勾选行数）
    selection_changed = pyqtSignal(int)
    # 文本格就地编辑完成（渲染行、字段名、新文本）
    cell_edited = pyqtSignal(int, str, str)
    # 链接栏右键：抓取此链接 / 填充抓取结果（行号、链接URL）
    link_fetch_requested = pyqtSignal(int, str)
    link_fill_requested = pyqtSignal(int, str)
    batch_import_requested = pyqtSignal()  # 表头右键：一键获取当前店铺所有商品信息
    # 点击底部➕号按钮：请求添加一行空白记录
    add_row_requested = pyqtSignal()

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
        # 已叠加到滚动范围上的底部余量（Qt 重算滚动范围后会被清除，需重新叠加）
        self._scroll_extra_applied = 0
        # 当前是否为搜索过滤视图（过滤态下新增记录不在结果集内，需退回整表渲染）
        self._is_filtered = False
        # 当前是否为「截图模式」（导出表单图片时置真：隐藏 ➕ / ▼ / 粘贴占位等交互控件）
        self._capture_mode = False
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
        # ➕号按钮：紧跟最后一条记录后面，点击添加一行空白记录
        # 作为表格(self)子部件，避免viewport重建导致野指针
        # 图标使用素材库的 add_row.svg（蓝色加号），浅蓝圆底衬托
        self._add_btn = QPushButton("", self)
        self._add_btn.setObjectName("addRowBtn")
        self._add_btn.setFixedSize(34, 34)
        self._add_btn.setIcon(QIcon(str(ASSETS_DIR / "add_row.svg")))
        self._add_btn.setIconSize(QSize(18, 18))
        self._add_btn.setToolTip("添加一行空白记录")
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setStyleSheet("""
            QPushButton#addRowBtn {
                border-radius: 17px;
                background: #EAF4FF;
                border: 1px solid transparent;
            }
            QPushButton#addRowBtn:hover {
                background: #D9ECFF;
                border-color: #7CC0F0;
            }
            QPushButton#addRowBtn:pressed {
                background: #C6E2FF;
                border-color: #1296db;
            }
        """)
        self._add_btn.clicked.connect(self._on_add_btn_clicked)
        self._add_btn.hide()
        # 平滑滚动动画：追加记录后把视图缓动到底部。
        # 复用同一个动画对象（父对象为表格），避免每次滚动都新建 QPropertyAnimation
        self._scroll_anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._scroll_anim.setDuration(_SCROLL_ANIM_MS)
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.finished.connect(self._update_add_btn_position)
        # 滚动时增量加载新进入可见区的图片 + 更新➕号位置
        self.verticalScrollBar().valueChanged.connect(self._schedule_load_visible)
        self.verticalScrollBar().valueChanged.connect(self._update_add_btn_position)

    def _init_table(self):
        # 第 0 列：勾选列；其后为数据列
        # 🔴 表头文字不要加空格前缀：加了会把「商品ID」顶得偏右（居中后看着不居中）。
        # 左上角「选择」按钮是悬浮子控件、贴在角格上，宽约一个表头高，
        # 不会压到居中的表头文字（见 _position_header_check）
        self.setColumnCount(len(TABLE_HEADERS) + 1)
        self.setHorizontalHeaderLabels([SELECT_COL_HEADER, *TABLE_HEADERS])
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
        # 平滑滚动：按像素而非按行滚动，避免"一段一段"的顿挫感
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        # 滚轮速度调慢：每次滚动 15px（默认约 20px+），配合 ScrollPerPixel 实现无极微调
        self.verticalScrollBar().setSingleStep(15)
        self.horizontalScrollBar().setSingleStep(15)
        header = self.horizontalHeader()
        # 表头文字一律居中：表头是"字段名的标题"，居中比左对齐更好读、更像一张表
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
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
        # 表头右键：商品链接列弹出「获取所有商品信息」
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_context_menu)
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

        # 勾选列表头的「全选」复选框：进入批量选择后出现在勾选列表头正中，
        # 一键全选 / 全不选；选了一部分时呈半选态。与 header_check 一样是
        # 悬浮在表格上的子控件（表头不支持塞控件），位置由 _position_select_all_check 维护
        self.select_all_check = _SelectAllCheck(self)
        self.select_all_check.setTristate(True)
        self.select_all_check.setToolTip("全选 / 全不选本店铺所有记录")
        self.select_all_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_check.setFixedSize(_HEADER_CHECK_SIZE, _HEADER_CHECK_SIZE)
        self.select_all_check.clicked.connect(self._on_select_all_clicked)
        self.select_all_check.hide()

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

    def _on_header_context_menu(self, pos):
        """表头右键：右键落在「商品链接」列上时，弹「获取所有商品信息」。"""
        col = self.horizontalHeader().logicalIndexAt(pos)
        if col <= 0:
            return
        field = RECORD_FIELDS[col - 1]
        if field != "product_url":
            return
        menu = QMenu(self)
        act = menu.addAction("获取所有商品信息（一键导入）")
        chosen = menu.exec(self.horizontalHeader().viewport().mapToGlobal(pos))
        if chosen == act:
            self.batch_import_requested.emit()

    # ---------- 列顺序 / 列宽 ----------
    def _on_section_moved(self, logical_index: int, old_visual: int, new_visual: int) -> None:
        """勾选列（逻辑第 0 列）必须固定在最左：拖走它或把别的列拖到它前面都还原"""
        if logical_index == 0 or new_visual == 0:
            header = self.horizontalHeader()
            header.blockSignals(True)
            header.moveSection(new_visual, old_visual)
            header.blockSignals(False)
        self._position_select_all_check()

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
        self._update_add_btn_position()
        self._position_select_all_check()
        self._schedule_scroll_extension()
        # 窗口变大后会有更多行进入可视区，补一次懒加载（原代码只在滚动时触发，
        # 最大化窗口时新露出的行会一直空着不加载缩略图）
        self._schedule_load_visible()

    def updateGeometries(self) -> None:
        """Qt 每次按内容重算滚动范围，都会把叠加的底部余量清掉，这里同步复位标记，
        使 _extend_scroll_for_add_btn 的叠加保持幂等（不会越加越多）"""
        super().updateGeometries()
        self._scroll_extra_applied = 0

    def _schedule_scroll_extension(self) -> None:
        """延迟到下一轮事件循环再施加滚动余量：避开 Qt 布局过程中对滚动范围的重算"""
        QTimer.singleShot(0, self._extend_scroll_for_add_btn)

    def _extend_scroll_for_add_btn(self) -> None:
        """在内容之外放开一截滚动余量，让最后一行下方始终能滑出放置「➕」号按钮的空白。

        幂等实现：始终以「当前上限 - 已叠加余量」为基准再加余量，
        因此 Qt 重算滚动范围（复位标记）或重复调用都不会造成范围累加漂移。
        """
        bar = self.verticalScrollBar()
        base_max = bar.maximum() - self._scroll_extra_applied
        if base_max <= bar.minimum():
            return
        bar.setRange(bar.minimum(), base_max + _ADD_BTN_BOTTOM_SPACE)
        self._scroll_extra_applied = _ADD_BTN_BOTTOM_SPACE

    def _update_add_btn_position(self):
        """更新「➕」号按钮位置：优先紧跟最后一行下方居中。

        按钮是悬浮在表格上的子控件、不属于表格内容，因此不能靠扩大滚动范围
        来保证可见。这里改为「一行完整进入可视区就贴上去」：
        - 最后一行底边尚未进入可视区（下面还有内容）→ 先隐藏，继续下滑即出现
        - 下方剩余空间不足以放下按钮 → 把间距压缩到 0，紧贴最后一行显示
        于是无论多少条记录，滑到最后一行就一定能看到并点到按钮。
        """
        if not hasattr(self, '_add_btn'):
            return
        if self._capture_mode:
            # 截图模式下底部悬浮 ➕ 不入图：本方法会被多个异步回调触发，
            # 这里直接拦掉，避免它悄悄把已隐藏的按钮又显示回来
            self._add_btn.hide()
            return
        row_count = self.rowCount()
        if row_count == 0:
            self._add_btn.move((self.width() - self._add_btn.width()) // 2,
                               max(40, (self.height() - self._add_btn.height()) // 2))
            self._add_btn.show()
            self._add_btn.raise_()
            return
        last_row = row_count - 1
        # rowViewportPosition 返回相对于 viewport 的坐标，加表头高度即表格中的坐标
        header_h = self.horizontalHeader().height()
        row_bottom = self.rowViewportPosition(last_row) + header_h + self.rowHeight(last_row)
        btn_h = self._add_btn.height()
        # 可用底部边界：表格底部扣除横向滚动条与少量留白
        usable_bottom = self.height() - self.horizontalScrollBar().height() - 4
        if row_bottom > usable_bottom:
            self._add_btn.hide()
            return
        y = row_bottom + _ADD_BTN_GAP
        if y + btn_h > usable_bottom:   # 下方空间不够，改为紧贴最后一行
            y = row_bottom
        self._add_btn.move((self.width() - self._add_btn.width()) // 2, y)
        self._add_btn.show()
        self._add_btn.raise_()

    def _on_add_btn_clicked(self):
        """点击➕号：异步发出信号，避免在点击事件中刷新表格导致崩溃。
        追加完成后的「滚到底部」由 append_record 的调用方通过 scroll_to_bottom 触发，
        这里不再硬编码延迟，避免时序不稳导致偶尔没滚到位。"""
        QTimer.singleShot(0, self.add_row_requested.emit)

    def scroll_to_bottom(self, animated: bool = True) -> None:
        """把视图滚动到底部（追加记录后调用，确保新行与「➕」按钮可见）。

        滚动范围要等 Qt 走完本轮布局（新行行高、底部余量重算）才稳定，
        因此延到下一轮事件循环再执行，避免滚到"上一版的底部"又停在半路。
        """
        QTimer.singleShot(0, lambda: self._do_scroll_to_bottom(animated))

    def _do_scroll_to_bottom(self, animated: bool) -> None:
        # 余量重算是幂等的，这里再补一次，保证目标位置就是真正的最底部
        self._extend_scroll_for_add_btn()
        bar = self.verticalScrollBar()
        target = bar.maximum()
        if bar.value() >= target:
            self._update_add_btn_position()
            return
        if not animated:
            bar.setValue(target)
            self._update_add_btn_position()
            return
        self._scroll_anim.stop()
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.start()

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
        if field == "spec":
            return max(text_w, 180)  # 规格列加宽：要显示完整 SKU 选项
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
                elif field == "product_url":
                    # 商品链接列固定宽度，无论 URL 多长都不变宽（跳过紧凑化）
                    self.setColumnWidth(col, TABLE_LINK_COLUMN_WIDTH)
                    continue
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
        self._is_filtered = indices is not None
        self._image_jobs = []
        self._loaded_thumbs.clear()
        self._row_checks.clear()
        self._load_timer.stop()
        self._row_resize_timer.stop()
        self._scroll_anim.stop()   # 整表重建会复位滚动范围，先停掉进行中的滚动动画

        # 规格列下拉选项：当前店铺所有记录的 spec 去重保序
        spec_opts = []
        seen = set()
        for r in records:
            s = (r.get("spec") or "").strip()
            if s and s not in seen:
                seen.add(s)
                spec_opts.append(s)
        self._spec_options = spec_opts

        self.setUpdatesEnabled(False)
        self.blockSignals(True)  # 批量 setItem 不触发 cellChanged，避免误写库
        self.setRowCount(0)
        self.setRowCount(len(records))
        for row, record in enumerate(records):
            self._render_row(row, record)
        self.blockSignals(False)
        self.setUpdatesEnabled(True)

        self._reset_header_check()
        self._position_header_check()
        QTimer.singleShot(0, self._update_add_btn_position)
        # 列宽在本轮事件处理后才稳定：先自适应列宽，再按最终列宽重算行高
        QTimer.singleShot(0, self._auto_fit_columns)
        QTimer.singleShot(0, self._adjust_row_heights)
        QTimer.singleShot(0, self._position_header_check)
        # 记录数变化后重算底部滚动余量（保证最后一行下方能滑出「➕」号按钮的位置）
        self._schedule_scroll_extension()
        # 首屏图片立即开始分片加载
        self._schedule_load_visible()

    def _render_row(self, row: int, record: dict) -> None:
        """渲染单行内容（勾选列 + 各数据列）。

        抽出来供整表 render 与增量 append_record 共用，避免两处渲染逻辑各写一遍
        而慢慢走样（改了一处忘了另一处）。
        """
        self._render_select_cell(row)
        for field_col, field in enumerate(RECORD_FIELDS):
            col = field_col + 1  # 数据列整体右移一位（第 0 列为勾选列）
            value = record.get(field, "")
            if field in MULTI_IMAGE_FIELDS:
                paths = value if isinstance(value, list) else ([value] if value else [])
                self._render_image_cell(row, col, paths, field, multi=True)
            elif field in SINGLE_IMAGE_FIELDS:
                self._render_image_cell(row, col, [value] if value else [], field, multi=False)
            elif field == "spec":
                text = "" if value is None else str(value)
                opts = record.get("spec_options") or []
                self._render_spec_cell(row, col, text, opts)
            else:
                text = "" if value is None else str(value)
                # 文本列保留可编辑标志：双击直接进入就地编辑，编辑结束由 cell_edited 落库
                item = QTableWidgetItem(text)
                if text:
                    # 完整内容已靠换行展示，tooltip 仅作悬停速览
                    item.setToolTip(text)
                item.setTextAlignment(self._text_alignment(field))
                self.setItem(row, col, item)

    # ---------- 增量追加 ----------
    def is_filtered(self) -> bool:
        """当前是否处于搜索过滤视图（过滤态下新增记录不在结果集内）"""
        return self._is_filtered

    def append_record(self, record: dict, index: int) -> int:
        """在表格末尾增量追加一行，返回新行号。

        与整表 render 的区别（正是「点添加闪一下 / 跳回 1 号记录」的根因所在）：
        - 只重建新行，老行的 cellWidget（图片/勾选框）原样保留，没有整表重绘的瞬间闪动
        - 不调用 setRowCount(0)，垂直滚动条不会被 Qt 复位到顶部
        - 已勾选的行不会被清空（整表 render 会重建所有勾选框、勾选态全丢）
        index 为该记录在原始记录列表中的位置，供 rendered_index 反查。
        """
        row = self.rowCount()
        self._rendered_indices.append(index)
        self.blockSignals(True)   # 批量 setItem 不触发 cellChanged，避免误写库
        try:
            self.setRowCount(row + 1)
            self._render_row(row, record)
        finally:
            self.blockSignals(False)
        # 列数未变，无需重新自适应列宽；只校正新行行高
        QTimer.singleShot(0, lambda r=row: self._adjust_row_height(r))
        self._schedule_scroll_extension()
        self._schedule_load_visible()
        # 新行为未勾选：全选框的三态（全选 / 半选 / 未选）要跟着变
        self._sync_header_check()
        return row

    def rendered_index(self, row: int) -> int:
        """渲染行号 -> 原始记录列表索引（供修改/删除定位真实记录）"""
        return self._rendered_indices[row]

    # ---------- 选区：一律按「完整记录行」判定 ----------
    @staticmethod
    def _text_alignment(field: str) -> int:
        """文本列对齐：短字段（如商品ID）居中更好读，其余左对齐。

        对齐规则只在 config.TABLE_CENTER_FIELDS 里定义一份，表格与 Excel 导出共用，
        避免两处各写一遍慢慢走样。
        """
        horizontal = (Qt.AlignmentFlag.AlignCenter if field in TABLE_CENTER_FIELDS
                      else Qt.AlignmentFlag.AlignLeft)
        return int(horizontal | Qt.AlignmentFlag.AlignVCenter)

    def _selected_rendered_rows(self) -> list:
        """选区涉及的所有渲染行号（升序、去重）"""
        rows = set()
        for rng in self.selectedRanges():
            for r in range(rng.topRow(), rng.bottomRow() + 1):
                if 0 <= r < self.rowCount():
                    rows.add(r)
        return sorted(rows)

    def selected_record_indices(self) -> list:
        """当前选区对应的**完整记录**索引（原始记录列表下标，升序、去重）。

        判定规则：选区只要碰到某一行，整条记录就算选中 —— 拖选一片区域时用户想的是
        「这几条记录」，而不是「这几个格子」。没有任何选区时退回当前行。
        """
        rows = self._selected_rendered_rows()
        if not rows and self.currentRow() >= 0:
            rows = [self.currentRow()]
        return [self._rendered_indices[r]
                for r in rows if 0 <= r < len(self._rendered_indices)]

    def expand_selection_to_rows(self) -> None:
        """把选区涉及的每一行**整行选中**，给出「这几条记录已选中」的视觉反馈"""
        rows = self._selected_rendered_rows()
        if not rows:
            return
        last_col = self.columnCount() - 1
        self.clearSelection()
        for row in rows:
            self.setRangeSelected(QTableWidgetSelectionRange(row, 0, row, last_col), True)

    def select_all_rows(self) -> None:
        """Ctrl+A：选中全部记录行（整行，非单元格）"""
        if self.rowCount() == 0:
            return
        last_col = self.columnCount() - 1
        self.clearSelection()
        self.setRangeSelected(
            QTableWidgetSelectionRange(0, 0, self.rowCount() - 1, last_col), True
        )

    # ---------- 截图模式 / 展开状态同步（供导出表单图片使用） ----------
    def set_capture_mode(self, on: bool) -> None:
        """进入/退出「截图模式」：把所有**属于交互、不属于表单内容**的控件隐藏掉。

        隐藏范围：各行的 ➕（添加图片）、▼/▲（展开收起）小按钮，单图列空位上的
        「（粘贴图片）」虚线占位框，以及底部悬浮的 ➕（见 `_update_add_btn_position`）。
        左上角「选择」按钮与勾选列表头全选框由 table_capture 另行处理。
        """
        self._capture_mode = on
        for row in range(self.rowCount()):
            for col in range(1, self.columnCount()):
                widget = self.cellWidget(row, col)
                if widget is None:
                    continue
                if isinstance(widget, MultiImageCell):
                    widget.set_capture_mode(on)
                    continue
                for label in widget.findChildren(QLabel):
                    if label.property("pastePlaceholder"):
                        label.setVisible(not on)
        self._update_add_btn_position()

    def is_capture_mode(self) -> bool:
        """当前是否为截图模式（单元格 rebuild 时据此决定要不要显示 ▼/＋ 按钮）"""
        return self._capture_mode

    def expanded_state(self) -> dict:
        """当前各多图单元格的展开状态：{原始记录索引: {已展开的字段名}}。

        用原始记录索引作键（而非渲染行号），这样调用方即使先筛掉空记录、
        再按新行号重新对齐，也能把展开状态准确套回去。
        """
        state: dict = {}
        for row in range(self.rowCount()):
            if row >= len(self._rendered_indices):
                break
            for col, field in enumerate(RECORD_FIELDS, start=1):
                if field not in MULTI_IMAGE_FIELDS:
                    continue
                widget = self.cellWidget(row, col)
                if isinstance(widget, MultiImageCell) and widget.is_expanded():
                    state.setdefault(self._rendered_indices[row], set()).add(field)
        return state

    def apply_expanded_state(self, state: dict) -> None:
        """把展开状态套到本表格（截图离屏副本用）：命中的多图格重建为展开态，
        其余保持默认折叠。这样「表单里展开了，导出图也就是展开的」。"""
        for row in range(self.rowCount()):
            if row >= len(self._rendered_indices):
                break
            fields = state.get(self._rendered_indices[row]) or ()
            for col, field in enumerate(RECORD_FIELDS, start=1):
                if field not in MULTI_IMAGE_FIELDS:
                    continue
                widget = self.cellWidget(row, col)
                if isinstance(widget, MultiImageCell):
                    widget.set_expanded(field in fields)

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
        # 表头文字保持居中（与其它列一致）：按钮贴在角格（x≈0..表头高），
        # 商品ID 表头居中后的文字离它还有一段距离，不会互相遮挡
        self._position_select_all_check()

    def _position_select_all_check(self) -> None:
        """把「全选」复选框摆到勾选列表头正中。

        表头不支持往 section 里塞控件，所以和 header_check 一样用「悬浮子控件 +
        手工定位」：x 取该 section 在视口中的位置，被横向滚动推出可视区时隐藏。
        """
        if not hasattr(self, "select_all_check"):
            return
        header = self.horizontalHeader()
        if self.isColumnHidden(0) or self.rowCount() == 0:
            self.select_all_check.hide()
            return
        size = self.select_all_check.width()
        left = header.x() + header.sectionViewportPosition(0)
        right = left + header.sectionSize(0)
        # 被横向滚动挤出表头可视区（或剩余空间塞不下）时隐藏，避免遮住行号列
        if left < header.x() or right > header.x() + header.width() or right - left < size:
            self.select_all_check.hide()
            return
        self.select_all_check.move(
            left + (right - left - size) // 2,
            (header.height() - size) // 2,
        )
        self.select_all_check.show()
        self.select_all_check.raise_()

    def _reset_header_check(self) -> None:
        """render 后行勾选全部清零，按钮回到默认“选择”图标，全选框回到未选"""
        self.header_check.setIcon(QIcon(str(ASSETS_DIR / "select.svg")))
        self._sync_header_check()

    def _on_row_check_changed(self, *_args) -> None:
        self._sync_header_check()
        self.selection_changed.emit(self.selected_count())

    def _on_select_all_clicked(self, _checked: bool = False) -> None:
        """表头全选框被点击：还没全选就全选，已全选则全不选（半选态点击 = 全选）"""
        total = len(self._row_checks)
        self._set_all_rows(self.selected_count() < total)

    def _sync_header_check(self) -> None:
        """按当前行勾选情况同步表头全选框的三态：
        全没选→未选，全选→选中，选了一部分→半选。"""
        if not hasattr(self, "select_all_check"):
            return
        total = len(self._row_checks)
        checked = self.selected_count()
        if total == 0 or checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked >= total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self.select_all_check.blockSignals(True)
        self.select_all_check.setCheckState(state)
        self.select_all_check.blockSignals(False)
        self._position_select_all_check()

    def total_min_width(self) -> int:
        """右侧表格的最小展示宽度：行号列 + 商品ID 到“图片”列（含）的最小宽 + 面板边距。
        左侧栏收起时右侧至少完整显示到“图片”列；最右的“商品链接”列可被挤出视口，
        以横向滚动条查看，不再把前面的列压到字段都看不见"""
        # 表格列：0=勾选,1..8=商品ID..图片,9=商品链接（可牺牲）
        keep_cols = [c for c in self._col_mins if c <= 8]
        body = sum(self._col_mins[c] for c in keep_cols) if keep_cols else 0
        return int(body + self.verticalHeader().width() + 24)

    def selected_count(self) -> int:
        """当前勾选的行数"""
        return sum(1 for check in self._row_checks.values() if check.isChecked())

    def selected_rendered_indices(self) -> list[int]:
        """勾选行对应的原始记录索引列表（供后续批量操作使用）"""
        return [self._rendered_indices[row] for row, check in self._row_checks.items() if check.isChecked()]

    # ---------- 键盘粘贴（Ctrl+V） ----------
    def keyPressEvent(self, event) -> None:
        # Ctrl+A：全选（整行）。放在最前面，避免被后面的复制/删除逻辑抢先吃掉
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all_rows()
            event.accept()
            return
        # Ctrl+Z 撤销：这里必须 `event.accept()`，否则事件继续冒泡到主窗口会被
        # 再处理一次，一次按键撤销两步。就地编辑单元格时焦点在编辑器上，
        # 由编辑器自带的撤销接管，不会走到这里。
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo_requested.emit()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste) and self._paste_to_current_cell():
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            # 图片列：复制图片本身；非图片列（含点左侧行号选中整行）：复制整条记录到下方
            if not self._copy_current_cell_image():
                row = self.currentRow()
                if row >= 0:
                    self.record_copy_requested.emit(row)
                    return
            return
        if event.matches(QKeySequence.StandardKey.Delete) and self._delete_current_cell_image():
            return
        super().keyPressEvent(event)

    def _delete_current_cell_image(self) -> bool:
        """当前焦点单元格是图片列时，删除最后点击的那张图（没点过则删第一张）"""
        row, col = self.currentRow(), self.currentColumn()
        if row < 0 or col <= 0:
            return False
        field = RECORD_FIELDS[col - 1]
        if field not in IMAGE_FIELDS:
            return False
        # 优先删除用户最后点击的那张图（需属于当前行）
        last = getattr(self, '_last_clicked_image', None)
        if last and last[0] == row and len(last) >= 3:
            self.image_delete_requested.emit(row, field, last[2])
            return True
        # 否则从单元格 widget 里找第一张带 stored_index 的缩略图
        widget = self.cellWidget(row, col)
        if widget is not None:
            for label in widget.findChildren(QLabel):
                si = label.property("stored_index")
                if si is not None:
                    self.image_delete_requested.emit(row, field, si)
                    return True
        return False

    def _copy_current_cell_image(self) -> bool:
        """当前焦点单元格是图片列时，复制最后点击的那张图（没点过则取第一张）到剪贴板"""
        row, col = self.currentRow(), self.currentColumn()
        if row < 0 or col <= 0:
            return False
        field = RECORD_FIELDS[col - 1]
        if field not in IMAGE_FIELDS:
            return False
        # 优先复制用户最后点击的那张图（需属于当前行）
        last = getattr(self, '_last_clicked_image', None)
        if last and last[0] == row and last[1]:
            self.image_copy_requested.emit(last[1])
            return True
        # 否则从单元格 widget 里找第一张带 image_path 的缩略图
        widget = self.cellWidget(row, col)
        if widget is not None:
            for label in widget.findChildren(QLabel):
                p = label.property("image_path")
                if p:
                    self.image_copy_requested.emit(p)
                    return True
        return False

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
        # 点击缩略图时选中所在单元格，保证 Ctrl+V/C/Del 能正确定位
        thumb.mousePressEvent = lambda event, r=row, c=col: self.setCurrentCell(r, c)
        lay.addWidget(thumb, alignment=Qt.AlignmentFlag.AlignCenter)
        self.enqueue_image_job(row, col, thumb, thumb_size, valid[0])
        self.setCellWidget(row, col, container)

    def make_thumb(self, thumb_size: int, view_paths: list, index: int) -> QLabel:
        """创建一张缩略图占位 QLabel（图片稍后懒加载填入）。

        双击=查看大图（复制已由 Ctrl+C / 右键承担）；删除等操作收进右键菜单。
        """
        thumb = QLabel()
        thumb.setFixedSize(thumb_size, thumb_size)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(_THUMB_STYLE)
        thumb.setToolTip("双击查看大图，Ctrl+C 复制，右键可删除")
        path = view_paths[index]
        thumb.setProperty("image_path", path)
        thumb.mouseDoubleClickEvent = lambda event, ps=view_paths, i=index: self.show_image_gallery(ps, i)
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
                        view_paths: list, view_index: int, stored_index: int, multi: bool,
                        stored_indices: list = None) -> None:
        """给缩略图绑定右键菜单：查看大图 / 粘贴 / 删除此图，并在点击时选中所在格"""
        anchor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        anchor.customContextMenuRequested.connect(
            lambda pos, a=anchor, r=row, c=col, f=field_name, ps=view_paths,
                   vi=view_index, si=stored_index, m=multi, sis=stored_indices:
            self._show_image_menu(a, pos, r, c, f, ps, vi, si, m, sis)
        )
        # cellWidget 会拦截鼠标事件，表格不会自动更新 currentCell，这里手动补齐；
        # 同时记录最后点击的图片（含 stored_index），供 Ctrl+C 复制 / Del 删除
        anchor.setProperty("stored_index", stored_index)
        def _on_thumb_press(event, r=row, c=col, p=view_paths[view_index], si=stored_index):
            self.select_image_cell(r, c)
            self._last_clicked_image = (r, p, si)
        anchor.mousePressEvent = _on_thumb_press

    def _show_image_menu(self, anchor: QWidget, pos, row: int, col: int,
                         field_name: str, view_paths: list, view_index: int,
                         stored_index: int, multi: bool, stored_indices: list = None) -> None:
        self.setCurrentCell(row, col)
        menu = QMenu(self)
        act_view = menu.addAction("查看大图")
        act_copy_img = menu.addAction("复制图片（Ctrl+C）")
        act_paste = menu.addAction("粘贴图片（Ctrl+V）")
        menu.addSeparator()
        act_delete = menu.addAction("删除此图片" if multi else "移除图片")
        menu.addSeparator()
        act_edit = menu.addAction("修改记录")
        act_copy = menu.addAction("复制记录")
        chosen = menu.exec(anchor.mapToGlobal(pos))
        if chosen == act_view:
            sis = stored_indices if stored_indices is not None else [stored_index] * len(view_paths)
            self.show_image_gallery(view_paths, view_index, row, field_name, sis)
        elif chosen == act_copy_img:
            self.image_copy_requested.emit(view_paths[view_index])
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

    def _render_spec_cell(self, row: int, col: int, current: str, options: list) -> None:
        """规格列：Excel 风格下拉框，右侧小箭头弹列表，选哪款显示哪款。"""
        from PyQt6.QtWidgets import QComboBox
        cb = QComboBox()
        cb.setEditable(True)
        cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

        # 合并选项：已抓取 SKU + 当前 spec 智能拆出，过滤 <4 字无意义项
        opts = []
        seen = set()
        for o in (options or []):
            o = (o or "").strip()
            if len(o) >= 4 and o not in seen:
                seen.add(o); opts.append(o)
        for o in smart_split_spec(current):
            if len(o) >= 4 and o not in seen:
                seen.add(o); opts.append(o)

        # 当前值智能清理：如果是长拼接，取第一款；去掉"商品规格:"前缀
        cur_pieces = smart_split_spec(current)
        cur = cur_pieces[0] if cur_pieces else ""
        if cur and cur not in opts:
            opts.insert(0, cur)

        cb.addItems(opts)
        cb.setCurrentText(cur)

        # 用默认样式，下拉箭头自然显示
        cb.setStyleSheet("QComboBox QAbstractItemView { border: 1px solid #dcdfe6; background: white; selection-background-color: #ecf5ff; selection-color: #409EFF; outline: 0px; }")

        def _on_pick(text):
            text = (text or "").strip()
            self.cell_edited.emit(row, "spec", text)
        cb.blockSignals(True)
        cb.currentTextChanged.connect(_on_pick)
        cb.blockSignals(False)
        cb.lineEdit().editingFinished.connect(lambda: _on_pick(cb.currentText()))

        # 把 QLineEdit 默认英文右键菜单换成中文
        from PyQt6.QtWidgets import QMenu
        def _lineedit_menu(pos):
            le = cb.lineEdit()
            menu = le.createStandardContextMenu()
            for a in menu.actions():
                txt = a.text()
                map_ = {
                    "Undo": "撤销", "Redo": "重做",
                    "Cut": "剪切", "Copy": "复制", "Paste": "粘贴",
                    "Delete": "删除", "Select All": "全选",
                }
                for k, v in map_.items():
                    if k in txt:
                        a.setText(txt.replace(k, v))
            menu.exec(le.viewport().mapToGlobal(pos) if hasattr(le, "viewport") else le.mapToGlobal(pos))
        cb.lineEdit().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        cb.lineEdit().customContextMenuRequested.connect(_lineedit_menu)

        self.setCellWidget(row, col, cb)


    def _set_spec(self, row: int, lbl, value: str):
        """统一设置规格格显示并发信号落库"""
        lbl.setText(value if value else "点击右侧 ▼ 选择规格")
        lbl.setStyleSheet("color: #303133; font-size: 12px; background: transparent;" if value
                          else "color: #c0c4cc; font-size: 11px; background: transparent; font-style: italic;")
        self.cell_edited.emit(row, "spec", value)

    def _on_cell_changed(self, row: int, col: int) -> None:
        """文本格就地编辑完成：发出 (渲染行, 字段, 新文本) 由主窗口落库"""
        if col <= 0:
            return
        field = RECORD_FIELDS[col - 1]
        if field in IMAGE_FIELDS:
            return  # 图片列底层无文本，不会走到
        item = self.item(row, col)
        text = "" if item is None else item.text()
        if field == "product_url" and text:
            cleaned = extract_product_url(text)
            if cleaned != text:
                if item is not None:
                    # 把识别出的纯链接回填到单元格，显示与落库一致；
                    # _url_cleaning 防重入：setText 会再次触发本槽，第二遍直接放行
                    self._url_cleaning = True
                    try:
                        item.setText(cleaned)
                    finally:
                        self._url_cleaning = False
                text = cleaned
        if getattr(self, "_url_cleaning", False):
            return  # 回填引起的那次信号不再重复落库
        self.cell_edited.emit(row, field, text)

    # ---------- 行高自适应 ----------
    def _row_needed_height(self, row: int) -> int:
        """该行按各列实际内容需要的高度：文本换行高度与图片排布高度取最大值。
        不设高度上限，长文本完整换行展示，行高仅有下限保底。"""
        needed = TABLE_ROW_MIN_HEIGHT
        font_metrics = self.fontMetrics()
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
        return max(TABLE_ROW_MIN_HEIGHT, needed)

    def _adjust_row_height(self, row: int) -> None:
        """只校正单行行高（增量追加后调用，避免为新行重排整张表）"""
        if not 0 <= row < self.rowCount():
            return
        height = self._row_needed_height(row)
        if self.rowHeight(row) != height:
            self.setRowHeight(row, height)
        self._update_add_btn_position()
        # 行高变化会改变内容总高，重算底部滚动余量
        self._schedule_scroll_extension()

    def _adjust_row_heights(self) -> None:
        """逐行按各列实际需要的高度设置行高（整表 / 窗口宽度变化时使用）"""
        count = self.rowCount()
        if count == 0:
            return
        for row in range(count):
            height = self._row_needed_height(row)
            if self.rowHeight(row) != height:
                self.setRowHeight(row, height)
        self._update_add_btn_position()
        # 行高变化会改变内容总高，重算底部滚动余量
        self._schedule_scroll_extension()

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
        # 打标记：截图模式下据此隐藏（占位框是交互提示，不属于表单内容）
        placeholder.setProperty("pastePlaceholder", True)
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

    # ---------- 右键菜单（整行：修改/复制/删除，支持多选） ----------
    def _show_context_menu(self, pos) -> None:
        """右键：右键落在已选区域内时作用于**整个选区**，否则只作用于右键所在的这一行。

        批量删除/复制的判定统一走 selected_record_indices()（选区即整条记录），
        菜单标题会写明条数，避免"以为删一条结果删了一片"。
        """
        row = self.rowAt(pos.y())
        if row < 0:
            return
        col = self.columnAt(pos.x())
        if col <= 0:
            col = 1
        if row in self._selected_rendered_rows():
            # 落在已选区域内：保持选区，并把这些行整行高亮
            self.expand_selection_to_rows()
        else:
            # 落在选区外：只选右键这一条
            self.clearSelection()
            self.setCurrentCell(row, col)
        indices = self.selected_record_indices()
        multi = len(indices) > 1
        menu = QMenu(self)

        # 链接列（product_url）额外增加抓取和填充选项
        field_name = RECORD_FIELDS[col - 1] if 0 < col <= len(RECORD_FIELDS) else ""
        if field_name == "product_url":
            # 直接从表格单元格获取链接文本
            link_item = self.item(row, col)
            link_url = link_item.text() if link_item else ""
            act_fetch = menu.addAction("🔍 抓取此链接")
            act_fill = menu.addAction("📥 抓取并填充到此行")
            menu.addSeparator()

        suffix = f"选中的 {len(indices)} 条" if multi else ""
        act_edit = menu.addAction("修改记录")
        act_copy = menu.addAction(f"复制{suffix}记录")
        menu.addSeparator()
        act_delete = menu.addAction(f"删除{suffix}记录")
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if multi:
            if chosen == act_copy:
                self.records_copy_requested.emit(indices)
            elif chosen == act_delete:
                self.records_delete_requested.emit(indices)
            elif chosen == act_edit:
                self.edit_requested.emit(row)   # 多选时"修改"只作用于右键那一条
            elif field_name == "product_url" and chosen == act_fetch:
                self.link_fetch_requested.emit(row, link_url)
            elif field_name == "product_url" and chosen == act_fill:
                self.link_fill_requested.emit(row, link_url)
            return
        if chosen == act_edit:
            self.edit_requested.emit(row)
        elif chosen == act_copy:
            self.record_copy_requested.emit(row)
        elif chosen == act_delete:
            self.delete_requested.emit(row)
        elif field_name == "product_url" and chosen == act_fetch:
            self.link_fetch_requested.emit(row, link_url)
        elif field_name == "product_url" and chosen == act_fill:
            self.link_fill_requested.emit(row, link_url)

    # ---------- 查看大图（支持多张翻页、右键复制/删除） ----------
    def show_image_gallery(self, paths: list, index: int = 0,
                           row: int = None, field_name: str = None,
                           stored_indices: list = None) -> None:
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

        def _copy_current():
            self.image_copy_requested.emit(paths[state["index"]])
            QMessageBox.information(dialog, "已复制", "图片已复制到剪贴板")

        def _delete_current():
            if row is None or field_name is None or stored_indices is None:
                return
            si = stored_indices[state["index"]] if state["index"] < len(stored_indices) else state["index"]
            ret = QMessageBox.question(
                dialog, "删除图片", "确定删除当前这张图片吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret == QMessageBox.StandardButton.Yes:
                self.image_delete_requested.emit(row, field_name, si)
                dialog.accept()

        # 右键菜单：复制图片 / 删除此图
        img_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        def _on_gallery_menu(pos):
            menu = QMenu(dialog)
            act_copy = menu.addAction("复制图片")
            can_delete = row is not None and field_name is not None and stored_indices is not None
            act_delete = menu.addAction("删除此图") if can_delete else None
            chosen = menu.exec(img_label.mapToGlobal(pos))
            if chosen == act_copy:
                _copy_current()
            elif chosen is not None and chosen == act_delete:
                _delete_current()
        img_label.customContextMenuRequested.connect(_on_gallery_menu)

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
