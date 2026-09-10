"""主窗口：组装界面面板并编排业务流程。

只负责界面组织与事件响应，数据读写走 ShopRepository，图片/导出走服务层。
"""
from copy import deepcopy

from PyQt6.QtCore import (
    Qt, QEasingCurve, QEvent, QObject, QPropertyAnimation, QSize, pyqtProperty,
)
from PyQt6.QtGui import QBrush, QColor, QCursor, QIcon, QImage
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..services import ExcelExporter, ImageService
from ..storage import DataLoadError, ShopRepository
from .add_dialog import AddRecordDialog
from .record_table import RecordTable

# 左侧店铺栏宽度与折叠动画参数
SHOP_PANEL_DEFAULT_WIDTH = 250   # 展开时的默认宽度（也是首次展开的目标宽度）
SHOP_RAIL_WIDTH = 38             # 收起后保留的窄轨宽度（放置展开按钮）
PANEL_ANIM_DURATION = 220        # 折叠/展开过渡时长（毫秒），InOutCubic 缓动
PANEL_COLLAPSE_THRESHOLD = 80   # 拖动分割条使左栏窄于该宽度，松手即自动收起


class SplitterWidthAnimator(QObject):
    """动画属性桥：把 QSplitter 某个子部件的宽度包装成可插值的 Qt 属性。

    QPropertyAnimation 每帧设置 panelWidth，setter 内同步 setSizes，
    剩余宽度全部分给另一侧，实现分栏宽度的平滑过渡。
    """

    def __init__(self, splitter: QSplitter, index: int, parent=None):
        super().__init__(parent)
        self._splitter = splitter
        self._index = index
        self._width = 0

    def _get_width(self) -> int:
        return self._width

    def _set_width(self, value) -> None:
        self._width = int(value)
        sizes = self._splitter.sizes()
        total = sum(sizes)
        sizes[self._index] = self._width
        rest = max(0, total - self._width)
        for i in range(len(sizes)):
            if i != self._index:
                sizes[i] = rest
        self._splitter.setSizes(sizes)

    panelWidth = pyqtProperty(int, fget=_get_width, fset=_set_width)


class CollapsibleStack(QStackedWidget):
    """分栏折叠容器：最小尺寸提示归零。

    QSplitter 默认按子部件 minimumSizeHint 限制可收窄的下限，
    QStackedWidget 会取所有页面（含完整店铺面板）的最小提示，导致无法
    收到窄轨宽度；这里统一返回 0，宽度完全交给 QSplitter/动画控制。
    """

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(config.APP_TITLE)
        self.setGeometry(100, 100, 1400, 700)

        # 数据层
        self.repo = ShopRepository(config.DATA_FILE, config.IMAGES_DIR)
        self.current_shop = None
        self._all_expanded = True  # 店铺树整体展开状态
        # 左侧栏折叠状态与展开宽度记忆
        self._shop_collapsed = False
        self._shop_expanded_width = SHOP_PANEL_DEFAULT_WIDTH
        self._panel_sized = False  # 首帧显示后再精确设定初始栏宽
        self._user_dragging = False  # 用户是否正在拖动分割条

        self.init_ui()
        self.load_data()

    # ==================== 界面搭建 ====================
    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        # 应用图标（标题栏/任务栏左上角）：素材库 app/assets/logo.png，不存在则忽略
        logo = config.ASSETS_DIR / "logo.png"
        if logo.exists():
            self.setWindowIcon(QIcon(str(logo)))
        main_layout = QHBoxLayout(central_widget)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(self.splitter)

        # 左侧用 QStackedWidget 承载“完整面板 / 收起窄轨”两个页面，
        # 宽度由 QSplitter 动画驱动，页面在动画起止时切换
        self.shop_stack = CollapsibleStack()
        self.shop_stack.setMinimumWidth(0)
        self.shop_stack.addWidget(self._build_shop_panel())      # index 0：完整面板
        self.shop_stack.addWidget(self._build_collapsed_rail())  # index 1：收起窄轨
        self.splitter.addWidget(self.shop_stack)
        self.splitter.addWidget(self._build_record_panel())
        self.splitter.setSizes([SHOP_PANEL_DEFAULT_WIDTH, 1150])

        # 折叠/展开过渡动画（InOutCubic 起止柔和、中间流畅）
        self._panel_animator = SplitterWidthAnimator(self.splitter, 0, self)
        self.panel_animation = QPropertyAnimation(self._panel_animator, b"panelWidth", self)
        self.panel_animation.setDuration(PANEL_ANIM_DURATION)
        self.panel_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        # 拖动左栏右边缘：窄于阈值松手自动收起；收起态向外拖出自动展开
        # QSplitterHandle 没有按压信号，用事件过滤器捕获鼠标按下/松开
        self._split_handle = self.splitter.handle(1)
        self._split_handle.installEventFilter(self)

    def showEvent(self, event):
        # 首帧显示时按分割器实际可用宽度精确设定左栏宽度（避免按比例缩放产生偏差）
        super().showEvent(event)
        if not self._panel_sized:
            self._panel_sized = True
            available = self.splitter.width() - self.splitter.handleWidth()
            self.splitter.setSizes(
                [SHOP_PANEL_DEFAULT_WIDTH, max(0, available - SHOP_PANEL_DEFAULT_WIDTH)]
            )

    def _build_shop_panel(self):
        """左侧：店铺列表面板"""
        left_widget = QWidget()
        left_widget.setObjectName("panel")
        # 显式放开最小宽度，QSplitter 动画才能把它收到窄轨宽度
        left_widget.setMinimumWidth(0)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(8)

        # 标题行：标题 + 树展开折叠 + 侧栏收起按钮
        title_row = QHBoxLayout()
        left_label = QLabel("店铺列表")
        left_label.setObjectName("title")
        self.btn_toggle_tree = QPushButton("全部折叠")
        self.btn_toggle_tree.setObjectName("treeToggle")
        self.btn_toggle_tree.clicked.connect(self.on_toggle_tree)
        title_row.addWidget(left_label)
        title_row.addStretch()
        title_row.addWidget(self.btn_toggle_tree)
        left_layout.addLayout(title_row)

        self.shop_tree = QTreeWidget()
        self.shop_tree.setMinimumWidth(0)
        self.shop_tree.setHeaderLabels(["店铺名称"])
        # 关闭 Qt 默认双击展开，由 on_shop_double_clicked 统一控制，避免双重切换抵消
        self.shop_tree.setExpandsOnDoubleClick(False)
        self.shop_tree.itemClicked.connect(self.on_shop_selected)
        self.shop_tree.itemDoubleClicked.connect(self.on_shop_double_clicked)
        # 右键菜单：店铺仅支持重命名
        self.shop_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.shop_tree.customContextMenuRequested.connect(self._on_shop_tree_menu)
        left_layout.addWidget(self.shop_tree)

        shop_btn_layout = QHBoxLayout()
        self.btn_add_shop = QPushButton("+ 新店铺")
        self.btn_rename_shop = QPushButton("重命名")
        self.btn_delete_shop = QPushButton("删除店铺")
        shop_btn_layout.addWidget(self.btn_add_shop)
        shop_btn_layout.addWidget(self.btn_rename_shop)
        shop_btn_layout.addWidget(self.btn_delete_shop)
        left_layout.addLayout(shop_btn_layout)

        self.btn_add_shop.clicked.connect(self.on_add_shop)
        self.btn_rename_shop.clicked.connect(self.on_rename_shop)
        self.btn_delete_shop.clicked.connect(self.on_delete_shop)

        return left_widget

    def _build_collapsed_rail(self):
        """左侧收起后保留的窄轨：仅放一个展开按钮"""
        rail = QWidget()
        rail.setObjectName("panel")
        rail.setMinimumWidth(0)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(4, 10, 4, 10)
        self.btn_expand_panel = QPushButton("»")
        self.btn_expand_panel.setObjectName("treeToggle")
        self.btn_expand_panel.setToolTip("展开店铺栏（也可向右拖动边缘展开）")
        self.btn_expand_panel.setFixedWidth(28)
        self.btn_expand_panel.clicked.connect(self.expand_shop_panel)
        rail_layout.addWidget(
            self.btn_expand_panel,
            alignment=Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
        )
        rail_layout.addStretch()
        return rail

    # ---------- 左侧栏折叠/展开动画 ----------
    def _animate_panel_width(self, target_width: int, on_finished=None) -> None:
        """把左侧栏从当前宽度平滑过渡到 target_width"""
        anim = self.panel_animation
        anim.stop()
        try:
            anim.finished.disconnect()
        except (TypeError, RuntimeError):
            pass  # 没有已连接的槽时忽略
        if on_finished is not None:
            anim.finished.connect(on_finished)
        # 动画期间禁用分割条拖拽，避免手动拖拽与动画互相打架
        handle = self.splitter.handle(1)
        handle.setEnabled(False)

        def _reenable(*_):
            handle.setEnabled(True)
        anim.finished.connect(_reenable)

        anim.setStartValue(self.splitter.sizes()[0])
        anim.setEndValue(target_width)
        anim.start()

    def eventFilter(self, obj, event):
        """监听分割条手柄的鼠标按下/松开，驱动拖拽收起/展开"""
        if obj is self._split_handle:
            if event.type() == QEvent.Type.MouseButtonPress:
                self._on_split_handle_pressed()
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._on_split_handle_released()
        return super().eventFilter(obj, event)

    def _on_split_handle_pressed(self) -> None:
        """开始拖动分割条：若当前是收起态，立即换回完整面板跟随拖拽宽度"""
        self._user_dragging = True
        if self._shop_collapsed:
            self.panel_animation.stop()
            self.shop_stack.setCurrentIndex(0)
            self._shop_collapsed = False

    def _on_split_handle_released(self) -> None:
        """松手判定：拖到阈值以下自动吸附收起；否则记住展开宽度"""
        self._user_dragging = False
        width = self.splitter.sizes()[0]
        if width < PANEL_COLLAPSE_THRESHOLD:
            self.collapse_shop_panel()
        elif width > SHOP_RAIL_WIDTH + 20:
            self._shop_expanded_width = width

    def collapse_shop_panel(self) -> None:
        """收起左侧店铺栏：动画收窄到窄轨，结束后切换为窄轨页面"""
        if self._shop_collapsed:
            return
        current = self.splitter.sizes()[0]
        if current > SHOP_RAIL_WIDTH + 20:
            self._shop_expanded_width = current  # 记住用户当前宽度，展开时还原

        def on_finished():
            self.shop_stack.setCurrentIndex(1)
            self._shop_collapsed = True
        self._animate_panel_width(SHOP_RAIL_WIDTH, on_finished)

    def expand_shop_panel(self) -> None:
        """展开左侧店铺栏：先切回完整面板，再动画还原宽度"""
        if not self._shop_collapsed:
            return
        self.shop_stack.setCurrentIndex(0)
        self._shop_collapsed = False
        self._animate_panel_width(self._shop_expanded_width)

    def _build_record_panel(self):
        """右侧：素材表格面板"""
        right_widget = QWidget()
        right_widget.setObjectName("panel")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(8)

        self.current_shop_label = QLabel("请选择左侧店铺")
        self.current_shop_label.setObjectName("title")
        self.current_shop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 店铺名居中、略加大加粗
        self.current_shop_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        right_layout.addWidget(self.current_shop_label)
        self.record_panel = right_widget

        self.table = RecordTable()
        self.table.image_paste_requested.connect(self.on_paste_image_requested)
        self.table.image_delete_requested.connect(self.on_delete_image_requested)
        self.table.image_copy_requested.connect(self.on_copy_image)
        self.table.image_add_requested.connect(self.on_add_images_requested)
        self.table.edit_requested.connect(self.on_edit_record)
        self.table.record_copy_requested.connect(self.on_copy_record)
        self.table.delete_requested.connect(self.on_delete_record)
        self.table.cell_edited.connect(self.on_cell_edited)
        self.table.selection_changed.connect(self.on_selection_changed)
        right_layout.addWidget(self.table)

        tip_label = QLabel("提示：点单元格只选该格，点顶部字段选中整列、点左侧行号选中整行；任意图片格（含多图格空白处）右键或按 Ctrl+V 粘贴图片；双击图片复制")
        tip_label.setObjectName("tip")
        right_layout.addWidget(tip_label)

        # 底部操作栏
        bottom_layout = QHBoxLayout()
        self.btn_add = QPushButton("新增记录")
        self.btn_edit = QPushButton("修改记录")
        self.btn_copy = QPushButton("复制记录")
        self.btn_delete = QPushButton("删除记录")
        self.btn_export = QPushButton("导出Excel")
        # 主操作按钮样式
        self.btn_add.setProperty("primary", True)
        self.btn_export.setProperty("primary", True)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("按标题搜索...")
        self.btn_search = QPushButton("搜索")
        self.btn_clear_search = QPushButton("清除搜索")

        bottom_layout.addWidget(self.btn_add)
        bottom_layout.addWidget(self.btn_edit)
        bottom_layout.addWidget(self.btn_copy)
        bottom_layout.addWidget(self.btn_delete)
        self.selection_label = QLabel("已选 0 条")
        self.selection_label.setObjectName("tip")
        self.selection_label.hide()  # 未进入批量选择模式时不显示，界面更干净
        bottom_layout.addWidget(self.selection_label)
        bottom_layout.addStretch()
        bottom_layout.addWidget(QLabel("搜索:"))
        bottom_layout.addWidget(self.search_input)
        bottom_layout.addWidget(self.btn_search)
        bottom_layout.addWidget(self.btn_clear_search)
        bottom_layout.addWidget(self.btn_export)
        right_layout.addLayout(bottom_layout)

        self.btn_add.clicked.connect(self.on_add_record)
        # clicked 信号自带 bool(checked)，用 lambda 隔离，避免 False 被当作行号传入
        self.btn_edit.clicked.connect(lambda _checked=False: self.on_edit_record())
        self.btn_copy.clicked.connect(lambda _checked=False: self.on_copy_record())
        self.btn_delete.clicked.connect(lambda _checked=False: self.on_delete_record())
        self.btn_export.clicked.connect(self.on_export)
        self.btn_search.clicked.connect(self.on_search)
        self.btn_clear_search.clicked.connect(self.on_clear_search)
        self.search_input.returnPressed.connect(self.on_search)

        return right_widget

    # ==================== 数据加载与保存 ====================
    def load_data(self):
        try:
            self.repo.load()
        except DataLoadError as e:
            QMessageBox.warning(self, "警告", f"{e}\n将使用空数据。")
            self.repo.shops = {}
        self.refresh_shop_tree()

    def save_data(self):
        try:
            self.repo.save()
        except Exception as e:
            QMessageBox.warning(self, "警告", f"数据保存失败：{str(e)}")

    # ==================== 店铺管理 ====================
    def on_add_shop(self):
        name, ok = QInputDialog.getText(self, "添加店铺", "请输入店铺名称:")
        if not (ok and name.strip()):
            return
        name = name.strip()
        if self.repo.shop_exists(name):
            QMessageBox.warning(self, "提示", "该店铺已存在！")
            return
        self.repo.add_shop(name)
        self.refresh_shop_tree()
        self.save_data()
        self.select_shop_in_tree(name)

    def on_rename_shop(self):
        item = self.shop_tree.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择要重命名的店铺")
            return
        old_name = item.text(0)
        new_name, ok = QInputDialog.getText(self, "重命名店铺", "请输入新名称:", text=old_name)
        if not (ok and new_name.strip() and new_name.strip() != old_name):
            return
        new_name = new_name.strip()
        if self.repo.shop_exists(new_name):
            QMessageBox.warning(self, "提示", "该店铺名已存在！")
            return
        self.repo.rename_shop(old_name, new_name)
        if self.current_shop == old_name:
            self.current_shop = new_name
            self.current_shop_label.setText(new_name)
        self.refresh_shop_tree()
        self.save_data()

    def on_delete_shop(self):
        item = self.shop_tree.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择要删除的店铺")
            return
        name = item.text(0)
        reply = QMessageBox.question(
            self, "确认删除", f"确定要删除店铺 '{name}' 及其所有素材吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.repo.delete_shop(name)
        if self.current_shop == name:
            self.current_shop = None
            self.current_shop_label.setText("请选择左侧店铺")
            self.table.render([])
        self.refresh_shop_tree()
        self.save_data()

    def on_shop_selected(self, item, column):
        self.current_shop = item.text(0) if item.parent() is None else item.parent().text(0)
        self.current_shop_label.setText(self.current_shop)
        self.refresh_table()

    def on_shop_double_clicked(self, item, column):
        if item.childCount() > 0:
            item.setExpanded(not item.isExpanded())

    def _on_shop_tree_menu(self, pos):
        """店铺树右键菜单：仅支持修改名字"""
        item = self.shop_tree.itemAt(pos)
        if item is None:
            return
        self.shop_tree.setCurrentItem(item)
        menu = QMenu(self)
        act_rename = menu.addAction("重命名")
        chosen = menu.exec(self.shop_tree.viewport().mapToGlobal(pos))
        if chosen == act_rename:
            self.on_rename_shop()

    def refresh_shop_tree(self):
        self.shop_tree.clear()
        for shop_name, records in self.repo.shops.items():
            shop_item = QTreeWidgetItem([shop_name])
            # 店名不可在树上直接编辑（改名统一走重命名，保证能保存）
            shop_item.setFlags(shop_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.shop_tree.addTopLevelItem(shop_item)
            count_item = QTreeWidgetItem([f"素材数量：{len(records)}"])
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            count_item.setForeground(0, QBrush(QColor("#909399")))
            shop_item.addChild(count_item)
        if self._all_expanded:
            self.shop_tree.expandAll()
        else:
            self.shop_tree.collapseAll()

    def on_toggle_tree(self):
        """一键展开/折叠全部店铺"""
        self._all_expanded = not self._all_expanded
        if self._all_expanded:
            self.shop_tree.expandAll()
            self.btn_toggle_tree.setText("全部折叠")
        else:
            self.shop_tree.collapseAll()
            self.btn_toggle_tree.setText("全部展开")

    def select_shop_in_tree(self, name):
        for i in range(self.shop_tree.topLevelItemCount()):
            item = self.shop_tree.topLevelItem(i)
            if item.text(0) == name:
                self.shop_tree.setCurrentItem(item)
                self.on_shop_selected(item, 0)
                break

    # ==================== 素材记录管理 ====================
    def refresh_table(self):
        """按当前店铺重新渲染表格"""
        records = self.repo.get_records(self.current_shop) if self.current_shop else []
        self.table.render(records)
        # 左侧栏收起时右侧表格保持完整列宽（不被压到字段看不见）
        if hasattr(self, 'record_panel'):
            self.record_panel.setMinimumWidth(self.table.total_min_width())
        self.selection_label.setText("已选 0 条")

    def on_add_record(self):
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先在左侧选择或创建一个店铺")
            return
        dialog = AddRecordDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        record = dialog.get_data()
        if not record:
            return
        self.repo.add_record(self.current_shop, record)
        self.refresh_table()
        self.refresh_shop_tree()
        self.save_data()

    def _current_table_row(self) -> int:
        """取当前要操作的行：优先 currentRow；若用户只点了勾选框导致
        currentRow 停在旧位置，则回退到选择模型中选中的行"""
        row = self.table.currentRow()
        if row >= 0:
            return row
        # 单元格选择模式下 selectedRows 常为空，回退到任意被选中的格所在行
        selected = self.table.selectionModel().selectedIndexes()
        return selected[0].row() if selected else -1

    def on_delete_record(self, row=None):
        """删除记录：row 为 None 时取当前选中行（按钮），否则为右键菜单指定的行"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        # bool 是 int 子类：误传入信号 bool 时统一按“未指定行”处理
        if not isinstance(row, int) or isinstance(row, bool):
            row = self._current_table_row()
        # 批量选择模式下勾选了至少一条：批量删除
        checked = self.table.selected_rendered_indices()
        if not self.table.isColumnHidden(0) and checked:
            reply = QMessageBox.question(
                self, "确认删除", f"确定要删除选中的 {len(checked)} 条记录吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            for ridx in sorted(checked, reverse=True):
                self.repo.remove_record(self.current_shop, ridx)
            self.refresh_table()
            self.refresh_shop_tree()
            self.save_data()
            self.table.set_selection_mode(False)  # 操作完成自动收回复选框
            QToolTip.showText(
                QCursor.pos(), f"已删除 {len(checked)} 条记录", self, msecShowTime=1500,
            )
            return
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中要删除的行")
            return
        reply = QMessageBox.question(
            self, "确认删除", "确定要删除选中的记录吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        record_index = self.table.rendered_index(row)
        self.repo.remove_record(self.current_shop, record_index)
        self.refresh_table()
        self.refresh_shop_tree()
        self.save_data()

    def on_edit_record(self, row=None):
        """修改记录：row 为 None 时取当前选中行（按钮），否则为右键菜单指定的行"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        # bool 是 int 子类：误传入信号 bool 时统一按“未指定行”处理
        if not isinstance(row, int) or isinstance(row, bool):
            row = self._current_table_row()
        # 批量选择模式下勾了多条：不支持同时修改
        checked = self.table.selected_rendered_indices()
        if not self.table.isColumnHidden(0) and len(checked) >= 2:
            QMessageBox.information(self, "提示", "多条记录不能同时修改！")
            return
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中要修改的行")
            return
        record_index = self.table.rendered_index(row)
        old_record = self.repo.get_records(self.current_shop)[record_index]

        dialog = AddRecordDialog(self, record=old_record)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_record = dialog.get_data()
        if not new_record:
            return
        self.repo.shops[self.current_shop][record_index] = new_record
        self.refresh_table()
        self.refresh_shop_tree()
        self.save_data()

    def on_copy_record(self, row=None):
        """复制记录：与修改一致弹出预填表单，确认后在原记录之后插入一条相同记录"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        # bool 是 int 子类：误传入信号 bool 时统一按“未指定行”处理
        if not isinstance(row, int) or isinstance(row, bool):
            row = self._current_table_row()
        # 批量选择模式下勾选了至少一条：直接逐条复制，不再弹表单
        checked = self.table.selected_rendered_indices()
        if not self.table.isColumnHidden(0) and checked:
            records = self.repo.get_records(self.current_shop)
            # 从后往前插入，前面的复制不会顶乱后面记录的位置
            for ridx in sorted(checked, reverse=True):
                self.repo.insert_record(self.current_shop, ridx, deepcopy(records[ridx]))
            self.refresh_table()
            self.refresh_shop_tree()
            self.save_data()
            self.table.set_selection_mode(False)  # 复制完成自动收回复选框
            QToolTip.showText(
                QCursor.pos(), f"已复制 {len(checked)} 条记录", self, msecShowTime=1500,
            )
            return
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中要复制的记录")
            return
        record_index = self.table.rendered_index(row)
        records = self.repo.get_records(self.current_shop)
        old_record = records[record_index]

        dialog = AddRecordDialog(self, record=old_record)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_record = dialog.get_data()
        if not new_record:
            return
        # 插到原记录正后方，并在刷新后选中新复制出的这一行
        new_pos = self.repo.insert_record(self.current_shop, record_index, new_record)
        self.refresh_table()
        self.refresh_shop_tree()
        self.save_data()
        self.table.setCurrentCell(new_pos, 1)

    def on_cell_edited(self, row, field, text):
        """文本格双击就地编辑完成：直接写回该字段并保存，行高随之重排（不重渲表格）"""
        if not self.current_shop:
            return
        record_index = self.table.rendered_index(row)
        self.repo.set_record_field(self.current_shop, record_index, field, text)
        self.save_data()
        self.table._adjust_row_heights()

    def on_paste_image_requested(self, row, col, field_name):
        """处理表格中的粘贴图片请求：保存图片并覆盖更新记录"""
        filepath, new_counter = ImageService.save_clipboard_image(
            QApplication.clipboard(), self.repo.images_dir, self.repo.image_counter
        )
        if not filepath:
            # 轻提示：无需确认、2.6 秒自动消失，不打断操作
            QToolTip.showText(
                QCursor.pos(),
                "剪贴板中不是图片，请先复制图片（截图或图片文件）后再粘贴",
                self, msecShowTime=2600,
            )
            return
        self.repo.image_counter = new_counter
        record_index = self.table.rendered_index(row)
        # 粘贴覆盖原有图片：单图列直接替换，多图列清空后只保留新粘贴的一张
        if field_name in config.MULTI_IMAGE_FIELDS:
            self.repo.set_record_field(self.current_shop, record_index, field_name, [filepath])
        else:
            self.repo.set_record_field(self.current_shop, record_index, field_name, filepath)
        self.refresh_table()
        self.save_data()

    def on_delete_image_requested(self, row, field_name, img_index):
        """删除单元格内的某张图片：多图移除指定序号，单图直接清空（仅移除引用）"""
        if not self.current_shop:
            return
        record_index = self.table.rendered_index(row)
        if field_name in config.MULTI_IMAGE_FIELDS:
            self.repo.remove_record_image(self.current_shop, record_index, img_index)
        else:
            self.repo.set_record_field(self.current_shop, record_index, field_name, "")
        self.refresh_table()
        self.save_data()

    def on_selection_changed(self, count):
        """表格勾选数量变化时更新底部计数（仅批量选择模式下显示）"""
        self.selection_label.setText(f"已选 {count} 条")
        self.selection_label.setVisible(not self.table.isColumnHidden(0))

    def on_copy_image(self, path: str) -> None:
        """双击缩略图：把图片本身复制到剪贴板（可直接粘贴到聊天/千牛等）"""
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(self, "提示", "图片读取失败，无法复制")
            return
        QApplication.clipboard().setImage(image)
        QToolTip.showText(QCursor.pos(), "图片已复制，可直接粘贴")

    def on_add_images_requested(self, row: int, field_name: str) -> None:
        """多图单元格“+”块：从文件选择图片，导入素材目录后追加到记录"""
        if not self.current_shop:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "添加图片（可按住 Ctrl 多选）", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)",
        )
        if not paths:
            return
        saved, new_counter = ImageService.import_image_files(
            paths, self.repo.images_dir, self.repo.image_counter
        )
        if not saved:
            QMessageBox.warning(self, "提示", "所选文件都不是有效图片")
            return
        self.repo.image_counter = new_counter
        record_index = self.table.rendered_index(row)
        for filepath in saved:
            self.repo.append_record_image(self.current_shop, record_index, filepath)
        self.refresh_table()
        self.save_data()

    # ==================== 搜索 ====================
    def on_search(self):
        keyword = self.search_input.text().strip().lower()
        if not keyword or not self.current_shop:
            if self.current_shop:
                self.refresh_table()
            return
        records = self.repo.get_records(self.current_shop)
        matched_indices = [i for i, r in enumerate(records) if keyword in r.get("title", "").lower()]
        matched = [records[i] for i in matched_indices]
        self.table.render(matched, matched_indices)

    def on_clear_search(self):
        self.search_input.clear()
        if self.current_shop:
            self.refresh_table()

    # ==================== 导出 ====================
    def on_export(self):
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出Excel", f"{self.current_shop}_素材导出.xlsx", "Excel文件 (*.xlsx)"
        )
        if not file_path:
            return
        try:
            records = self.repo.get_records(self.current_shop)
            ExcelExporter.export(records, file_path, sheet_name=self.current_shop)
            QMessageBox.information(self, "成功", f"导出成功！\n文件保存在：{file_path}")
        except ImportError:
            QMessageBox.warning(self, "错误", "openpyxl库未安装，请运行: pip install openpyxl")
        except Exception as e:
            QMessageBox.warning(self, "错误", f"导出失败：{str(e)}")
