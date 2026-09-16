"""主窗口：组装界面面板并编排业务流程。

只负责界面组织与事件响应，数据读写走 ShopRepository，图片/导出走服务层。
数据变更统一走 `_undo_step` 包一层，Ctrl+Z 即可回退上一步。
"""
import os
from contextlib import contextmanager
from copy import deepcopy

from PyQt6.QtCore import (
    QRect, Qt, QEasingCurve, QObject, QPropertyAnimation, QSize, QTimer, pyqtProperty,
    pyqtSignal, QVariantAnimation, QThread,
)
from PyQt6.QtWidgets import QAbstractItemView
from PyQt6.QtGui import QBrush, QColor, QCursor, QIcon, QImage, QKeySequence, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
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
    QStackedWidget,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..services import (
    ExcelExporter,
    ImageService,
    ScreenshotStore,
    taobao_import,
    taobao_playwright,
    taobao_session,
)
from ..services.taobao_client import TaobaoClient
from ..services.link_utils import extract_product_url
from ..services.history import UndoStack, snapshot
from ..storage import DataLoadError, ShopRepository
from ..utils.logger import get_logger
from .add_dialog import AddRecordDialog
from .import_worker import BatchImportWorker
from .record_table import RecordTable
from .screenshot_history_dialog import ScreenshotHistoryDialog
from .table_capture import capture_records_table, record_is_empty
from .taobao_login_dialog import TaobaoLoginDialog
from .taobao_fetch_dialog import TaobaoFetchDialog

logger = get_logger("taobao.ui")

# 左侧店铺栏宽度与折叠动画参数
SHOP_PANEL_MAX_WIDTH = 330       # 店铺列表能拉到的最大宽度（再宽也没必要）
SHOP_PANEL_DEFAULT_WIDTH = 200   # 展开时的默认宽度（也是首次展开的目标宽度）
SHOP_PANEL_MIN_WIDTH = 73        # 拖动下限 ≈ 原上限 220 的 1/3，避免被拉没
SHOP_RAIL_WIDTH = 38             # 收起后保留的窄轨宽度（放置展开按钮）
PANEL_ANIM_DURATION = 220        # 折叠/展开过渡时长（毫秒），InOutCubic 缓动
PANEL_EXPAND_SWITCH_THRESHOLD = 120  # 折叠态拖缝时，窄轨宽超过该值才切出完整面板（先宽后显）


class _LinkFetchWorker(QObject):
    """链接栏抓取/填充的后台线程 Worker"""
    done = pyqtSignal(dict)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            from ..services import taobao_playwright
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
    """全局加载动画覆盖层：旋转 SVG + 状态文字"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("background: rgba(255,255,255,0.88);")
        self.hide()

        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(12)

        self.icon = _RotatingSvgIcon(str(config.ASSETS_DIR / "loading.svg"), 64)
        lay.addWidget(self.icon, alignment=Qt.AlignmentFlag.AlignCenter)

        self.text = QLabel("加载中...")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setStyleSheet("color: #606266; font-size: 14px;")
        lay.addWidget(self.text)

    def show_with_text(self, text: str):
        self.text.setText(text)
        self.show()
        self.raise_()

    def update_text(self, text: str):
        """展示期间更新状态文字（如批量导入进度），不改变可见状态"""
        self.text.setText(text)

    def hide_overlay(self):
        self.hide()


class SortableShopTree(QTreeWidget):
    """可拖拽排序的店铺树：只允许顶层店铺项拖拽，松手后发出顺序变更信号并高亮被移动项。

    展开/收起箭头用 drawBranch 代码绘制，支持顺时针丝滑旋转动效：
    - 收起=0 度（向右），展开=90 度（向下），点击时 QVariantAnimation 插值，InOutCubic 缓动 250ms
    - 顶层 item 设 ItemIsDragEnabled 且去掉 ItemIsDropEnabled，避免被拖成另一个店铺的子项
    - 子项（素材数量）同时去掉 Drag 和 Drop，完全不可参与拖拽
    """
    shop_order_changed = pyqtSignal(list)  # 新的店铺名顺序（list[str]）
    BRANCH_ARROW_SIZE = 13  # 箭头绘制尺寸（px），与下方 setIndentation 的缩进宽度搭配不拥挤

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setIndentation(24)  # branch 区域宽度，给箭头留空间
        self._dragged_name = None
        # 旋转动效状态
        self._arrow_pm = QPixmap(str(config.ASSETS_DIR / "tree_arrow.svg"))
        self._branch_angles = {}   # id(item) -> 当前角度（QTreeWidgetItem 不可哈希，用 id）（0=收起向右，90=展开向下）
        self._branch_anims = {}    # id(item) -> QVariantAnimation（防止被 GC）
        self.itemExpanded.connect(self._on_item_expanded)
        self.itemCollapsed.connect(self._on_item_collapsed)

    # ---------- 展开/收起旋转动效 ----------
    def _on_item_expanded(self, item):
        if item.parent() is None:
            # 首次展开（refresh 重建后）直接设终值，不播动画；用户手动展开才走动画
            if id(item) not in self._branch_angles:
                self._branch_angles[id(item)] = 90.0
                self.viewport().update()
            else:
                self._animate_branch(item, 90.0)

    def _on_item_collapsed(self, item):
        if item.parent() is None:
            if id(item) not in self._branch_angles:
                self._branch_angles[id(item)] = 0.0
                self.viewport().update()
            else:
                self._animate_branch(item, 0.0)

    def _animate_branch(self, item, target):
        """从当前角度丝滑过渡到目标角度（顺时针），InOutCubic 缓动 250ms"""
        start = self._branch_angles.get(id(item), 90.0 if target == 0.0 else 0.0)
        if id(item) in self._branch_anims:
            self._branch_anims[id(item)].stop()
        anim = QVariantAnimation(self)
        anim.setDuration(250)
        anim.setStartValue(start)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.valueChanged.connect(lambda v, it=item: self._update_branch_angle(it, v))
        anim.finished.connect(lambda: self._branch_anims.pop(id(item), None))
        self._branch_anims[id(item)] = anim
        anim.start()

    def _update_branch_angle(self, item, angle):
        self._branch_angles[id(item)] = float(angle)
        self.viewport().update()

    def drawBranch(self, painter, rect, index):
        """Qt6 里 drawBranch 可能不被调用，实际箭头绘制走 paintEvent（保留此方法备用）"""
        super().drawBranch(painter, rect, index)

    def paintEvent(self, event):
        """重写绘制：默认绘制完成后，在每个有子项的顶层店铺的 branch 区域画旋转箭头"""
        super().paintEvent(event)
        if self._arrow_pm.isNull():
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        view_h = self.viewport().height()
        ind = self.indentation()
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item.childCount() == 0:
                continue
            rect = self.visualItemRect(item)
            # 只画可见区域内的
            if rect.y() + rect.height() < 0 or rect.y() > view_h:
                continue
            # branch 区域：viewport 左侧 indentation 宽度（visualItemRect 的 x 是文字起始位，不含 branch）
            branch_rect = QRect(0, rect.y(), ind, rect.height())
            angle = self._branch_angles.get(id(item), 90.0 if item.isExpanded() else 0.0)
            self._draw_rotated_arrow(painter, branch_rect, angle)
        painter.end()

    def _draw_rotated_arrow(self, painter, rect, angle):
        """在 rect 中心绘制顺时针旋转 angle 度的箭头"""
        if self._arrow_pm.isNull() or rect.width() <= 0 or rect.height() <= 0:
            return
        size = min(self.BRANCH_ARROW_SIZE, rect.width() - 2, rect.height() - 2)
        if size <= 0:
            return
        scaled = self._arrow_pm.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.translate(rect.center())
        painter.rotate(angle)
        painter.translate(-scaled.width() / 2, -scaled.height() / 2)
        painter.drawPixmap(0, 0, scaled)
        painter.restore()

    # ---------- 拖拽排序 ----------
    def startDrag(self, supportedActions):
        """只允许顶层店铺项开始拖拽；子项（素材数量）直接忽略"""
        item = self.currentItem()
        if item is None or item.parent() is not None:
            return
        self._dragged_name = item.text(0)
        super().startDrag(supportedActions)

    def dropEvent(self, event):
        """拖拽结束：父类完成 item 移动 -> 读新顺序发信号 -> 高亮被移动项"""
        if self._dragged_name is None:
            super().dropEvent(event)
            return
        name = self._dragged_name
        super().dropEvent(event)
        new_order = [self.topLevelItem(i).text(0) for i in range(self.topLevelItemCount())]
        self.shop_order_changed.emit(new_order)
        self._highlight_moved(name)
        self._dragged_name = None

    def _highlight_moved(self, name):
        """被移动项短暂高亮：背景色从淡蓝经两帧过渡到透明，模拟自然回落"""
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item.text(0) != name:
                continue
            colors = ["#D6EAF8", "#EBF5FB", "#F4F9FD", ""]
            def step(idx=0):
                if idx >= len(colors):
                    return
                c = colors[idx]
                if c:
                    item.setBackground(0, QBrush(QColor(c)))
                else:
                    item.setBackground(0, QBrush())
                QTimer.singleShot(55, lambda: step(idx + 1))
            step()
            break


class CollapsibleStack(QStackedWidget):
    """分栏折叠容器：最小尺寸提示归零。

    左栏宽度由 setFixedWidth 直接钉死（拖缝/折叠动画是唯一入口），布局不会
    按内容 minimumSizeHint 反推宽度；这里归零只是双保险，保证页面在
    「完整面板 ⇄ 窄轨」切换瞬间也不会被内容撑开。
    """

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)


class _SplitBar(QWidget):
    """左栏与表单区之间的拖动缝：拖动只改左栏宽度，右栏被动跟随。

    - 往右拖 = 左栏变宽，右侧表单整体被向右推开（宽度 = 窗口剩余空间）
    - 往左拖 = 左栏收窄
    - 不用 QSplitter：命中区就是这 6px 本身，悬停/按住变蓝给出可拖提示
    """

    drag_started = pyqtSignal()      # 鼠标按下（开始一次拖拽会话）
    drag_moved = pyqtSignal(int)     # 相对按下点的总水平位移（px，右拖为正）
    drag_finished = pyqtSignal()     # 鼠标松开

    BAR_WIDTH = 6  # 缝宽：与旧 QSplitter handleWidth 一致，视觉不变

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.BAR_WIDTH)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._hover = False
        self._pressed = False
        self._press_x = 0.0

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self.update()

    def paintEvent(self, event):
        """画中间 1px 分隔线：默认灰，悬停/按住时变蓝（与旧把手视觉一致）"""
        painter = QPainter(self)
        color = QColor("#A0CFFF") if (self._hover or self._pressed) else QColor("#DCDFE6")
        painter.fillRect((self.width() - 1) // 2, 0, 1, self.height(), color)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self._press_x = event.globalPosition().x()
            self.update()
            self.drag_started.emit()
            event.accept()

    def mouseMoveEvent(self, event):
        if not self._pressed:
            return
        # 发送相对按下点的总位移（非增量）：float 只在最后取整一次，无累积误差
        self.drag_moved.emit(int(round(event.globalPosition().x() - self._press_x)))
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._pressed:
            self._pressed = False
            self.update()
            self.drag_finished.emit()
            event.accept()


class MainWindow(QMainWindow):
    image_library_changed = pyqtSignal()  # 评价图片增删后通知图片管理刷新
    def __init__(self):
        super().__init__()
        self.setWindowTitle(config.APP_TITLE)
        self.setGeometry(100, 100, 1400, 700)

        # 数据层
        self.repo = ShopRepository(config.DATA_FILE, config.IMAGES_DIR)
        self.taobao = TaobaoClient(config.TAOBAO_COOKIE_FILE)
        self._undo = UndoStack()      # Ctrl+Z：数据快照式撤销栈
        self.current_shop = None
        self.current_category = None   # 当前选中的产品分类（表格/导出/新增都只针对它）
        self._col_widths_memory = {}  # 每个店铺独立记忆列宽
        self._all_expanded = True  # 店铺树整体展开状态
        # 左侧栏折叠状态与展开宽度记忆
        self._shop_collapsed = False
        self._shop_expanded_width = SHOP_PANEL_DEFAULT_WIDTH
        self._panel_animating = False  # 折叠/展开动画进行中（期间忽略拖缝）
        self._drag_base_width = None   # 本次拖缝按下时刻的左栏宽度（位移基准）
        # 一键导入后台线程（运行中禁止重复触发）
        self._batch_import_thread = None
        self._batch_import_worker = None

        self.init_ui()
        self.load_data()

    @property
    def _shop_label_colors(self) -> dict:
        """店铺名 -> {"bg": hex|None, "text": hex|None}。

        直接指向仓库里的 dict（随 data.json 持久化），不另存一份内存副本——
        店铺名的填充色/字体色要跨重启保留，导出图才能和界面上看到的一致。
        """
        return self.repo.shop_colors

    def _color_key(self, shop: str, category: str) -> str:
        """颜色存储 key：店铺名|分类名（每个分类独立配色）。"""
        return f"{shop}|{category}"

    def _get_color(self, shop: str, category: str) -> dict:
        """读取某分类的配色：优先用 店铺|分类 的独立配色，不存在则回退到店铺级旧配色。"""
        key = self._color_key(shop, category)
        if key in self._shop_label_colors:
            return self._shop_label_colors[key]
        return self._shop_label_colors.get(shop, {"bg": None, "text": None})

    # ==================== 界面搭建 ====================
    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        # 应用图标（标题栏/任务栏左上角）：素材库 app/assets/logo.png，不存在则忽略
        logo = config.ASSETS_DIR / "logo.png"
        if logo.exists():
            self.setWindowIcon(QIcon(str(logo)))
        main_layout = QHBoxLayout(central_widget)
        main_layout.setSpacing(0)  # 左中右之间不留间隙，缝宽 = 拖动条本身的 6px

        # 左侧用 QStackedWidget 承载“完整面板 / 收起窄轨”两个页面。
        # 不再用 QSplitter：左栏宽度固定，只有拖中间缝（或折叠/展开动画）会改它，
        # 窗口缩放的全部增量都归右栏——天然满足「拉动边框不影响隔壁板块宽度」
        self.shop_stack = CollapsibleStack()
        self.shop_stack.addWidget(self._build_shop_panel())      # index 0：完整面板
        self.shop_stack.addWidget(self._build_collapsed_rail())  # index 1：收起窄轨
        self.shop_stack.setFixedWidth(SHOP_PANEL_DEFAULT_WIDTH)
        main_layout.addWidget(self.shop_stack)

        # 中间拖动缝：往右拖 = 左栏变宽（右侧表单整体被向右推开），往左拖 = 左栏收窄
        self._split_bar = _SplitBar()
        self._split_bar.drag_started.connect(self._on_split_drag_started)
        self._split_bar.drag_moved.connect(self._on_split_drag_moved)
        self._split_bar.drag_finished.connect(self._on_split_drag_finished)
        main_layout.addWidget(self._split_bar)

        # 右侧表单区：stretch=1，吸收窗口缩放与拖缝让出的全部剩余空间
        main_layout.addWidget(self._build_record_panel(), 1)

        # 折叠/展开过渡动画（InOutCubic 起止柔和、中间流畅）：逐帧改左栏固定宽度
        self.panel_animation = QPropertyAnimation(self, b"shopPanelWidth", self)
        self.panel_animation.setDuration(PANEL_ANIM_DURATION)
        self.panel_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)

        # 全局加载动画覆盖层（链接栏抓取/填充时显示）
        self._global_loading = _LoadingOverlay(self.centralWidget())
        self._global_loading.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_global_loading'):
            self._global_loading.setGeometry(self.centralWidget().rect())

    def showEvent(self, event):
        super().showEvent(event)
        # 左栏宽度开局就由 setFixedWidth 钉死，无需首帧后再补偿初始栏宽
        # 首次启动显示欢迎弹窗（用户勾选不再提醒后跳过）
        if not getattr(self, '_welcome_shown', False):
            self._welcome_shown = True
            from .welcome_dialog import should_show_welcome, WelcomeDialog
            if should_show_welcome():
                QTimer.singleShot(100, self._show_welcome_dialog)

    def _show_welcome_dialog(self):
        """显示启动欢迎弹窗"""
        from .welcome_dialog import WelcomeDialog
        dialog = WelcomeDialog(self)
        dialog.exec()

    def _build_shop_panel(self):
        """左侧：店铺列表面板（店铺 -> 产品分类 -> 素材）"""
        left_widget = QWidget()
        left_widget.setObjectName("panel")
        # 宽度约束不在这里设：左栏宽度统一由 shop_stack.setFixedWidth 控制（拖缝/动画唯一入口）
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(8)

        # 标题行：标题 + 树展开折叠按钮
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

        self.shop_tree = SortableShopTree()
        self.shop_tree.setMinimumWidth(0)
        self.shop_tree.setHeaderLabels(["店铺名称"])
        # 关闭 Qt 默认双击展开，由 on_shop_double_clicked 统一控制，避免双重切换抵消
        self.shop_tree.setExpandsOnDoubleClick(False)
        self.shop_tree.itemClicked.connect(self.on_shop_selected)
        self.shop_tree.itemDoubleClicked.connect(self.on_shop_double_clicked)
        # 右键菜单：店铺仅支持重命名
        self.shop_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.shop_tree.customContextMenuRequested.connect(self._on_shop_tree_menu)
        # 拖拽排序：松手后更新数据层顺序并持久化
        self.shop_tree.shop_order_changed.connect(self.on_shop_order_changed)
        left_layout.addWidget(self.shop_tree, 1)

        self.btn_shop_mgmt = QPushButton("店铺管理")
        self.btn_shop_mgmt.setStyleSheet(
            "QPushButton { background: #409EFF; color: white; border: none; padding: 8px; "
            "border-radius: 4px; font-size: 13px; font-weight: bold; }"
            "QPushButton:hover { background: #66b1ff; }"
        )
        self.shop_mgmt_menu = QMenu(self)
        self.act_add_shop = self.shop_mgmt_menu.addAction("➕ 新增店铺")
        self.act_img_mgmt = self.shop_mgmt_menu.addAction("🖼 图片管理")
        self.btn_shop_mgmt.setMenu(self.shop_mgmt_menu)
        self.act_add_shop.triggered.connect(lambda: self.on_add_shop())
        self.act_img_mgmt.triggered.connect(self._open_image_manager)
        left_layout.addWidget(self.btn_shop_mgmt)
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

    # ---------- 左侧栏宽度：拖缝 / 折叠展开动画 ----------
    # 左栏宽度属性桥：QPropertyAnimation 逐帧写 shopPanelWidth → setFixedWidth，
    # 右栏在布局里 stretch=1，被动吸收剩余空间变化（推开的语义天然成立）
    @pyqtProperty(int)
    def shopPanelWidth(self) -> int:
        return self.shop_stack.width()

    @shopPanelWidth.setter
    def shopPanelWidth(self, value: int) -> None:
        self.shop_stack.setFixedWidth(int(value))

    def _animate_panel_width(self, target_width: int, on_finished=None) -> None:
        """把左侧栏从当前宽度平滑过渡到 target_width（右栏被动跟随）"""
        anim = self.panel_animation
        anim.stop()
        try:
            anim.finished.disconnect()
        except (TypeError, RuntimeError):
            pass  # 没有已连接的槽时忽略
        if on_finished is not None:
            anim.finished.connect(on_finished)
        # 动画期间禁用拖缝，避免手动拖拽与动画互相打架
        self._panel_animating = True
        self._split_bar.setEnabled(False)

        def _reenable():
            self._split_bar.setEnabled(True)
            self._panel_animating = False
        anim.finished.connect(_reenable)

        anim.setStartValue(self.shop_stack.width())
        anim.setEndValue(int(target_width))
        anim.start()

    # ---------- 中间拖动缝 ----------
    def _on_split_drag_started(self) -> None:
        """记住按下时刻的左栏宽度，作为本次拖拽的位移基准"""
        if self._panel_animating:
            return
        self._drag_base_width = self.shop_stack.width()

    def _on_split_drag_moved(self, dx: int) -> None:
        """拖缝实时改宽：右拖为正 → 左栏变宽、右栏整体被推开；左拖为负 → 左栏收窄。

        展开态宽度夹在 [SHOP_PANEL_MIN_WIDTH, SHOP_PANEL_MAX_WIDTH]；
        折叠态窄轨先跟随增长，拖过阈值才切出完整面板（先宽后显，避免店铺列表突然跳出）。
        """
        if self._panel_animating or self._drag_base_width is None:
            return
        target = self._drag_base_width + dx
        if self._shop_collapsed:
            if target >= PANEL_EXPAND_SWITCH_THRESHOLD:
                self._shop_collapsed = False
                self.shop_stack.setCurrentIndex(0)
                self.shop_stack.setFixedWidth(
                    max(SHOP_PANEL_MIN_WIDTH, min(target, SHOP_PANEL_MAX_WIDTH))
                )
            else:
                self.shop_stack.setFixedWidth(max(SHOP_RAIL_WIDTH, target))
        else:
            self.shop_stack.setFixedWidth(
                max(SHOP_PANEL_MIN_WIDTH, min(target, SHOP_PANEL_MAX_WIDTH))
            )

    def _on_split_drag_finished(self) -> None:
        """松手：展开态记住当前宽度；折叠态没拖出阈值就回弹窄轨"""
        self._drag_base_width = None
        if self._panel_animating:
            return
        if self._shop_collapsed:
            if self.shop_stack.width() != SHOP_RAIL_WIDTH:
                self._animate_panel_width(SHOP_RAIL_WIDTH)  # 回弹窄轨，rail 不停在中间宽
        else:
            self._shop_expanded_width = self.shop_stack.width()

    def collapse_shop_panel(self) -> None:
        """收起左侧店铺栏：动画收窄到窄轨，结束后切换为窄轨页面"""
        if self._shop_collapsed:
            return
        current = self.shop_stack.width()
        if current > SHOP_RAIL_WIDTH + 20:
            self._shop_expanded_width = current  # 记住用户当前宽度，展开时还原

        def on_finished():
            self.shop_stack.setCurrentIndex(1)
            self._shop_collapsed = True
            self.shop_stack.setFixedWidth(SHOP_RAIL_WIDTH)  # 精确钉在窄轨宽度
        self._animate_panel_width(SHOP_RAIL_WIDTH, on_finished)

    def expand_shop_panel(self) -> None:
        """展开左侧店铺栏：先切回完整面板，再从窄轨宽度动画还原"""
        if not self._shop_collapsed:
            return
        self.shop_stack.setCurrentIndex(0)
        self._shop_collapsed = False
        target = max(SHOP_PANEL_MIN_WIDTH, min(self._shop_expanded_width, SHOP_PANEL_MAX_WIDTH))
        self.shop_stack.setFixedWidth(SHOP_RAIL_WIDTH)  # 从窄轨起点开始动画
        self._animate_panel_width(target)

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
        # 店铺名居中、略加大加粗；右键可设置填充色（按店铺记忆）
        self._shop_label_base_style = "font-size: 18px; font-weight: 600;"
        self.current_shop_label.setStyleSheet(self._shop_label_base_style)
        self.current_shop_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.current_shop_label.customContextMenuRequested.connect(self._on_shop_label_context_menu)
        right_layout.addWidget(self.current_shop_label)
        self.record_panel = right_widget

        self.table = RecordTable()
        self.table.add_row_requested.connect(self.on_add_blank_record)
        self.table.image_paste_requested.connect(self.on_paste_image_requested)
        self.table.image_delete_requested.connect(self.on_delete_image_requested)
        self.table.image_copy_requested.connect(self.on_copy_image)
        self.table.image_add_requested.connect(self.on_add_images_requested)
        self.table.edit_requested.connect(self.on_edit_record)
        self.table.record_copy_requested.connect(self.on_copy_record)
        self.table.delete_requested.connect(self.on_delete_record)
        # 选区批量（Ctrl+A 全选 / 拖选一片区域 → 右键）：参数是原始记录索引列表
        self.table.records_delete_requested.connect(self.on_delete_records)
        self.table.records_copy_requested.connect(self.on_copy_records)
        # Ctrl+Z：表格内由表格发出；焦点在按钮等控件上时由主窗口 keyPressEvent 兜底
        self.table.undo_requested.connect(self.on_undo)
        self.table.cell_edited.connect(self.on_cell_edited)
        self.table.selection_changed.connect(self.on_selection_changed)
        self.table.link_fetch_requested.connect(self.on_link_fetch)
        self.table.link_fill_requested.connect(self.on_link_fill)
        self.table.batch_import_requested.connect(self.on_batch_import)
        self.table.spec_option_added.connect(self.on_spec_option_added)
        right_layout.addWidget(self.table)

        tip_label = QLabel("提示：点单元格只选该格，点顶部字段选中整列、点左侧行号选中整行；Ctrl+A 全选记录；右键删除/复制选中的记录；Ctrl+Z 撤销上一步；图片格双击查看大图、Ctrl+C 复制、Del 删除、Ctrl+V 粘贴；右键更多操作")
        tip_label.setObjectName("tip")
        # 关键修复：这行提示很长，QLabel 不换行时 minimumSizeHint = 整行文字宽度（≈1356px），
        # 会把右栏最小宽度撑爆 → 布局为满足右栏最小宽度会把左栏挤没（缝拖不动的帮凶）。
        # 开启自动换行后最小宽度提示降到 ≈72px，左栏在任何窗口宽度下都稳在设定值。
        tip_label.setWordWrap(True)
        right_layout.addWidget(tip_label)

        # 底部操作栏
        # 删除不再放按钮：一律走右键（单行 / 多选区都支持），避免"手滑点一下就删"。
        # 新增/修改/复制收敛成一个「商品」下拉菜单，底部栏不再排一排同质按钮
        bottom_layout = QHBoxLayout()
        self.btn_product = QPushButton("编辑 ▾")
        self.btn_product.setToolTip("选择批量操作 / 新增 / 修改 / 复制 / 删除商品")
        self.product_menu = QMenu(self)
        self.act_select = self.product_menu.addAction("选择")
        self.act_select.setCheckable(True)
        self.product_menu.addSeparator()
        self.act_product_add = self.product_menu.addAction("新增商品")
        self.act_product_edit = self.product_menu.addAction("修改商品")
        self.act_product_copy = self.product_menu.addAction("复制商品")
        self.act_product_delete = self.product_menu.addAction("删除商品")
        self.btn_product.setMenu(self.product_menu)
        def _show_edit_menu_from_header():
            from PyQt6.QtGui import QCursor
            self.product_menu.exec(QCursor.pos())
        self.table.header_button_clicked.connect(_show_edit_menu_from_header)
        # 「导出」：一个入口，三种出口（Excel 表格 / 表单整表图片 / 截图历史）
        self.btn_export = QPushButton("导出 ▾")
        self.btn_export.setToolTip("导出当前店铺：Excel 表格 / 表单图片 / 查看截图历史")
        self.export_menu = QMenu(self)
        self.act_export_excel = self.export_menu.addAction("导出 Excel 表格")
        self.act_export_excel.setToolTip("把当前店铺全部记录导出为 .xlsx")
        self.act_export_image = self.export_menu.addAction("导出表单图片")
        self.act_export_image.setToolTip(
            "把当前店铺的整张表单（含滚动区外的所有记录）渲染成图片\n"
            "顶部带店铺名，弹出保存位置，文件名默认为「店铺名_序号」，并归档到截图历史"
        )
        self.act_screenshot_history = self.export_menu.addAction("截图历史")
        self.act_screenshot_history.setToolTip("查看所有历史截图：大图预览、滚轮缩放、另存为")
        self.btn_export.setMenu(self.export_menu)
        # 主操作按钮样式
        self.btn_product.setProperty("primary", True)
        self.btn_export.setProperty("primary", True)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("按标题搜索...")
        self.btn_search = QPushButton("搜索")
        self.btn_clear_search = QPushButton("清除搜索")

        self.btn_taobao_login = QPushButton("淘宝登录")
        self.btn_taobao_login.setToolTip("登录淘宝获取 cookie，用于后续导入商品素材")
        bottom_layout.addWidget(self.btn_taobao_login)
        self.btn_taobao_fetch = QPushButton("抓取商品")
        self.btn_taobao_fetch.setToolTip("粘贴淘宝商品链接，自动抓取标题/价格/主图")
        bottom_layout.addWidget(self.btn_taobao_fetch)
        bottom_layout.addWidget(self.btn_product)
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

        self.btn_taobao_login.clicked.connect(self.on_taobao_login)
        self.btn_taobao_fetch.clicked.connect(self.on_open_fetch)
        # 「商品」菜单三项：新增 / 修改 / 复制（action 用 triggered，不带 checked 布尔）
        self.act_select.triggered.connect(self._on_toggle_select_menu)
        self.act_product_add.triggered.connect(lambda _checked=False: self.on_add_record())
        self.act_product_edit.triggered.connect(lambda _checked=False: self.on_edit_record())
        self.act_product_copy.triggered.connect(lambda _checked=False: self.on_copy_record())
        self.act_product_delete.triggered.connect(lambda _checked=False: self.on_delete_record())
        self.act_export_excel.triggered.connect(lambda _checked=False: self.on_export_excel())
        self.act_export_image.triggered.connect(lambda _checked=False: self.on_export_image())
        self.act_screenshot_history.triggered.connect(lambda _checked=False: self.on_screenshot_history())
        self.btn_search.clicked.connect(self.on_search)
        self.btn_clear_search.clicked.connect(self.on_clear_search)
        self.search_input.returnPressed.connect(self.on_search)

        return right_widget

    def keyPressEvent(self, event) -> None:
        """Ctrl+Z 兜底：焦点不在表格上（例如停在某个按钮）时也能撤销。

        事件只有在焦点控件**没有消费**它时才会冒泡到这里，所以：
        - 焦点在表格 → 表格自己处理并 accept，这里不会再触发（不会一次撤销两步）
        - 焦点在搜索框 → QLineEdit 的文本撤销先接管，也不会误回退整表
        """
        if event.matches(QKeySequence.StandardKey.Undo):
            self.on_undo()
            event.accept()
            return
        super().keyPressEvent(event)

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
    def _open_image_manager(self):
        """图片管理：分类+网格预览"""
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QScrollArea, QGridLayout, QLabel,
            QSplitter, QListWidget, QListWidgetItem, QPushButton, QHBoxLayout,
            QInputDialog, QMenu, QCheckBox, QWidget as _W
        )
        from .image_utils import scaled_pixmap
        dlg = QDialog(self)
        dlg.setWindowTitle("图片管理")
        dlg.resize(1200, 750)
        root = QVBoxLayout(dlg)

        # 数据：分类列表 + 图片->分类映射
        cats = self.repo.image_categories
        cat_map = self.repo.image_category_map
        current_cat = {"name": "全部图片"}  # 当前选中分类

        # ===== 左右布局 =====
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # 左：分类列表
        cat_list = QListWidget()
        cat_list.setMaximumWidth(200)
        cat_list.setStyleSheet("QListWidget { border-right: 1px solid #e0e0e0; }")
        splitter.addWidget(cat_list)

        def refresh_cat_list():
            cat_list.clear()
            item_all = QListWidgetItem(f"全部图片")
            cat_list.addItem(item_all)
            for c in cats:
                cat_list.addItem(QListWidgetItem(c))
            # 默认选中全部
            for i in range(cat_list.count()):
                if cat_list.item(i).text() == current_cat["name"]:
                    cat_list.setCurrentRow(i)
                    break

        def on_cat_changed():
            current_cat["name"] = cat_list.currentItem().text() if cat_list.currentItem() else "全部图片"
            render_grid()

        cat_list.currentRowChanged.connect(on_cat_changed)

        # 右键空白处：新建分类；分类右键：删除
        def cat_context_menu(pos):
            item = cat_list.itemAt(pos)
            m = QMenu(cat_list)
            if item is None:
                a_new = m.addAction("新建分类")
                act = m.exec(cat_list.viewport().mapToGlobal(pos))
                if act == a_new:
                    name, ok = QInputDialog.getText(dlg, "新建分类", "分类名称:")
                    if ok and name.strip():
                        cats.append(name.strip())
                        self.repo.save()
                        refresh_cat_list()
                return
            name = item.text()
            if name == "全部图片":
                return
            a_del = m.addAction("删除分类")
            act = m.exec(cat_list.viewport().mapToGlobal(pos))
            if act == a_del:
                ret = QMessageBox.question(dlg, "删除分类", f"删除分类「{name}」？\n（图片不会删除，仅移出分类）")
                if ret == QMessageBox.StandardButton.Yes:
                    cats.remove(name)
                    for k in list(cat_map.keys()):
                        if cat_map.get(k) == name:
                            del cat_map[k]
                    self.repo.save()
                    refresh_cat_list()
                    render_grid()
        cat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        cat_list.customContextMenuRequested.connect(cat_context_menu)

        # 右：工具栏 + 图片网格
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(right)

        # 工具栏
        toolbar = QHBoxLayout()
        select_btn = QPushButton("选择")
        select_btn.setCheckable(True)
        toolbar.addWidget(select_btn)
        toolbar.addStretch(1)
        refresh_btn = QPushButton("刷新")
        from PyQt6.QtWidgets import QStyle as _QStyle
        refresh_btn.setIcon(dlg.style().standardIcon(_QStyle.StandardPixmap.SP_BrowserReload))
        toolbar.addWidget(refresh_btn)
        rlay.addLayout(toolbar)

        selecting = {"on": False}
        checked_paths = set()

        def toggle_select(on):
            selecting["on"] = on
            checked_paths.clear()
            select_btn.setText("取消选择" if on else "选择")
            render_grid()
        select_btn.toggled.connect(toggle_select)

        # 收集图片
        import hashlib as _hl
        def _file_md5(path):
            try:
                return _hl.md5(open(path, "rb").read()).hexdigest()
            except Exception:
                return None
        def collect_items():
            its = []
            seen_md5 = set()  # 按内容MD5去重
            for shop, cat, records in [(s, c, rs) for s, sd in self.repo.shops.items() for c, rs in sd.items()]:
                for r in records:
                    item_id = r.get("product_id") or r.get("item_id") or ""
                    for pp in (r.get("image_paths") or []):
                        if not pp:
                            continue
                        b2 = os.path.basename(pp).lower()
                        if b2.startswith("tb_sku_") or b2.startswith("tb_main_"):
                            continue
                        if not os.path.isfile(pp):
                            continue
                        h = _file_md5(pp)
                        if h and h in seen_md5:
                            continue
                        if h:
                            seen_md5.add(h)
                        its.append((pp, f"{shop}_{cat}_{item_id}"))
            # 按当前分类过滤
            if current_cat["name"] != "全部图片":
                its = [(p, n) for p, n in its if cat_map.get(p) == current_cat["name"]]
            return its

        def usage_map():
            # 按 MD5 合并：相同内容的图路径不同，但算同一张
            md5_to_primary = {}  # md5 -> 代表路径
            path_to_md5 = {}
            for shop, catsd in self.repo.shops.items():
                for cat, records in catsd.items():
                    for idx, r in enumerate(records):
                        for pp in (r.get("image_paths") or []):
                            if pp and os.path.isfile(pp):
                                h = _file_md5(pp)
                                if h:
                                    path_to_md5[pp] = h
                                    if h not in md5_to_primary:
                                        md5_to_primary[h] = pp
            m = {}
            for shop, catsd in self.repo.shops.items():
                for cat, records in catsd.items():
                    for idx, r in enumerate(records):
                        for pp in (r.get("image_paths") or []):
                            if pp:
                                h = path_to_md5.get(pp)
                                key = md5_to_primary.get(h, pp)
                                m.setdefault(key, []).append((shop, cat, idx))
            return m

        def goto_record(shop, cat, rec_idx):
            dlg.accept()
            self.select_shop_in_tree(shop)
            if hasattr(self, "_set_category"):
                self._set_category(cat)
            QTimer.singleShot(200, lambda: self.table.selectRow(rec_idx))

        def delete_image(path):
            ret = QMessageBox.question(dlg, "删除图片",
                f"确认删除？\n将从所有商品评价图片中移除。\n{os.path.basename(path)}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
            with self._undo_step("删除评价图片"):
                for shop, catsd in self.repo.shops.items():
                    for cat, records in catsd.items():
                        for r in records:
                            r["image_paths"] = [p for p in (r.get("image_paths") or []) if p != path]
            cat_map.pop(path, None)
            self.repo.save()
            self.refresh_table()
            self.image_library_changed.emit()
            render_grid()

        def view_big(path):
            pm = scaled_pixmap(path, 1200)
            if not pm:
                return
            v = QDialog(dlg)
            v.setWindowTitle(os.path.basename(path))
            v.resize(min(1200, pm.width()+40), min(900, pm.height()+80))
            vl = QVBoxLayout(v)
            lb = QLabel()
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lb.setPixmap(pm)
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setWidget(lb)
            vl.addWidget(sc)
            v.exec()

        # 移动图片到分类
        def move_to_cat(paths_list, cat_name):
            for p in paths_list:
                if cat_name == "未分类":
                    cat_map.pop(p, None)
                else:
                    cat_map[p] = cat_name
            self.repo.save()
            render_grid()

        # 网格
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setSpacing(12)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll.setWidget(host)
        rlay.addWidget(scroll)

        def dedupe_external_images():
            """刷新时：把外部路径的图和 images_dir 里视觉相同的合并。"""
            from PyQt6.QtGui import QImage as _QImg
            import os as _os
            images_dir = str(self.repo.images_dir)
            def _phash(path):
                img = _QImg(path)
                if img.isNull():
                    return None
                img = img.scaled(8, 8)
                g = img.convertToFormat(_QImg.Format.Format_Grayscale8)
                px = [g.pixelColor(x, y).value() for y in range(8) for x in range(8)]
                avg = sum(px) / 64
                b = 0
                for i, v in enumerate(px):
                    if v >= avg:
                        b |= (1 << i)
                return b
            def _ham(a, b):
                return bin(a ^ b).count("1")
            # 索引 images_dir
            idh = {}
            from pathlib import Path as _P
            for f in _P(images_dir).glob("image_*"):
                h = _phash(str(f))
                if h is not None:
                    idh[str(f)] = h
            changed = False
            for shop, cats in self.repo.shops.items():
                for cat, records in cats.items():
                    for r in records:
                        ips = r.get("image_paths", [])
                        if not isinstance(ips, list):
                            continue
                        new_ips = []
                        seen = set()
                        for pp in ips:
                            if pp and _os.path.isfile(pp) and not pp.startswith(images_dir):
                                h = _phash(pp)
                                if h is not None:
                                    best, bd = None, 999
                                    for ip, ih in idh.items():
                                        d = _ham(h, ih)
                                        if d < bd:
                                            bd, best = d, ip
                                    if best is not None and bd <= 5:
                                        pp = best
                            if pp not in seen:
                                seen.add(pp)
                                new_ips.append(pp)
                        if new_ips != ips:
                            r["image_paths"] = new_ips
                            changed = True
            if changed:
                self.repo.save()

        def render_grid():
            dedupe_external_images()
            # 彻底清空旧内容
            while grid.count():
                it = grid.takeAt(0)
                w = it.widget()
                if w:
                    w.setParent(None)
                    w.deleteLater()
            # 同步子项
            for w in host.findChildren(QWidget):
                if w is not host and w.parent() is None:
                    w.deleteLater()
            its = collect_items()
            if not its:
                grid.addWidget(QLabel("暂无图片"), 0, 0)
                return
            umap = usage_map()
            cols = 5
            for i, (path, name) in enumerate(its):
                uses = umap.get(path, [])
                cell = QWidget()
                cl = QVBoxLayout(cell)
                cl.setContentsMargins(0, 0, 0, 0)
                cl.setSpacing(1)
                img_wrap = QWidget()
                img_wrap.setFixedSize(148, 148)
                lbl = QLabel(img_wrap)
                lbl.setGeometry(0, 0, 148, 148)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                pm = scaled_pixmap(path, 140)
                if pm:
                    lbl.setPixmap(pm)
                lbl.setStyleSheet("border: 1px solid #1677ff; background: white; border-radius: 2px;")
                lbl.setToolTip(f"{name}\n使用次数：{len(uses)}")

                # 选择模式：左上角复选框
                cb = None
                if selecting["on"]:
                    cb = QCheckBox(img_wrap)
                    cb.setGeometry(4, 4, 18, 18)
                    cb.setStyleSheet("background: white;")
                    cb.setChecked(path in checked_paths)
                    def _toggled(checked, p=path):
                        if checked:
                            checked_paths.add(p)
                        else:
                            checked_paths.discard(p)
                    cb.toggled.connect(_toggled)

                # 右下角蓝点角标
                badge = QPushButton(str(len(uses)), img_wrap)
                badge.setGeometry(118, 120, 28, 20)
                badge.setStyleSheet("background: #1677ff; color: white; border-radius: 9px; font: bold 9pt; padding: 0;")
                badge.setCursor(Qt.CursorShape.PointingHandCursor)
                def _show_uses(uses_list, p):
                    m = QMenu(badge)
                    m.setStyleSheet("QMenu { min-width: 280px; background: white; border: 1px solid #1677ff; padding: 4px; } QMenu::item { padding: 4px 20px; } QMenu::item:selected { background: #e6f4ff; color: #1677ff; }")
                    for j, (shop, cat, idx) in enumerate(uses_list, 1):
                        a = m.addAction(f"{j}. {shop} / {cat} / 第{idx+1}条")
                        a.setData((shop, cat, idx))
                    m.addSeparator()
                    a_del = m.addAction("删除这张图片")
                    act = m.exec(QCursor.pos())
                    if act is None:
                        return
                    if act == a_del:
                        delete_image(p)
                    else:
                        data = act.data()
                        if data:
                            goto_record(*data)
                badge.clicked.connect(lambda checked=False, ul=uses, pp=path: _show_uses(ul, pp))

                # 右键菜单
                def _menu(p=path):
                    m = QMenu(lbl)
                    a1 = m.addAction("查看大图")
                    # 移动到分类子菜单
                    sub = m.addMenu("移动到分类")
                    a_none = sub.addAction("未分类")
                    cat_acts = []
                    for c in cats:
                        a = sub.addAction(c)
                        cat_acts.append((a, c))
                    m.addSeparator()
                    a2 = m.addAction("删除图片")
                    act = m.exec(QCursor.pos())
                    if act == a1:
                        view_big(p)
                    elif act == a2:
                        delete_image(p)
                    elif act == a_none:
                        move_to_cat([p], "未分类")
                    else:
                        for a, c in cat_acts:
                            if act == a:
                                move_to_cat([p], c)
                                break
                lbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                lbl.customContextMenuRequested.connect(lambda pos, pp=path: _menu(pp))
                lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                lbl.mouseDoubleClickEvent = lambda e, p=path: view_big(p)

                name_lbl = QLabel(name)
                name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                name_lbl.setStyleSheet("font-size: 11px; color: #606266; padding: 0; margin: 0;")
                name_lbl.setWordWrap(True)
                cl.addWidget(img_wrap)
                cl.addWidget(name_lbl)
                grid.addWidget(cell, i // cols, i % cols,
                               Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

            # 选择模式下：右键网格空白处批量移动
            def grid_context_menu(pos):
                if not selecting["on"] or not checked_paths:
                    return
                m = QMenu(host)
                sub = m.addMenu(f"移动 {len(checked_paths)} 张到分类")
                a_none = sub.addAction("未分类")
                cat_acts = [(sub.addAction(c), c) for c in cats]
                act = m.exec(QCursor.pos())
                if act == a_none:
                    move_to_cat(list(checked_paths), "未分类")
                else:
                    for a, c in cat_acts:
                        if act == a:
                            move_to_cat(list(checked_paths), c)
                            break
            host.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            host.customContextMenuRequested.connect(grid_context_menu)

        refresh_cat_list()
        render_grid()
        refresh_btn.clicked.connect(render_grid)
        self.image_library_changed.connect(render_grid)
        try:
            dlg.exec()
        finally:
            try:
                self.image_library_changed.disconnect(render_grid)
            except Exception:
                pass

    def on_add_shop(self):
        name, ok = QInputDialog.getText(self, "添加店铺", "请输入店铺名称:")
        if not (ok and name.strip()):
            return
        name = name.strip()
        if self.repo.shop_exists(name):
            QMessageBox.warning(self, "提示", "该店铺已存在！")
            return
        with self._undo_step("添加店铺"):
            self.repo.add_shop(name)
        self.refresh_shop_tree()
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
        with self._undo_step("重命名店铺"):
            self.repo.rename_shop(old_name, new_name)
            if self.current_shop == old_name:
                self.current_shop = new_name
                self.current_shop_label.setText(new_name)
        self.refresh_shop_tree()

    # ==================== 产品分类 ====================
    def _next_category_name(self, shop: str) -> str:
        """给新分类起名：分类二 / 分类三 …（跳过已被占用的名字）"""
        existing = set(self.repo.categories(shop))
        index = 2
        while f"分类{index}" in existing:
            index += 1
        return f"分类{index}"

    def on_add_category(self, shop: str | None = None) -> None:
        """在指定店铺下新增一个产品分类（默认起名 分类N，可改）"""
        shop = shop or self.current_shop
        if not shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        suggestion = self._next_category_name(shop)
        name, ok = QInputDialog.getText(self, "新增产品分类", f"「{shop}」的分类名称:",
                                        text=suggestion)
        if not (ok and name.strip()):
            return
        name = name.strip()
        if self.repo.category_exists(shop, name):
            QMessageBox.warning(self, "提示", "该分类名已存在！")
            return
        with self._undo_step(f"新增分类「{name}」"):
            self.repo.add_category(shop, name)
        self.refresh_shop_tree()
        self.select_shop_in_tree(shop, name)

    def on_rename_category(self, shop: str, category: str) -> None:
        new_name, ok = QInputDialog.getText(self, "重命名分类", "请输入新名称:", text=category)
        if not (ok and new_name.strip() and new_name.strip() != category):
            return
        new_name = new_name.strip()
        if self.repo.category_exists(shop, new_name):
            QMessageBox.warning(self, "提示", "该分类名已存在！")
            return
        with self._undo_step(f"重命名分类「{category}」"):
            self.repo.rename_category(shop, category, new_name)
            if self.current_shop == shop and self.current_category == category:
                self.current_category = new_name
        self.refresh_shop_tree()

    def on_delete_category(self, shop: str, category: str) -> None:
        if not self._confirm(
            "确认删除",
            f"确定要删除分类「{category}」吗？\n该分类下的 {len(self.repo.get_records(shop, category))} 条素材会一起删除。",
        ):
            return
        with self._undo_step(f"删除分类「{category}」"):
            removed = self.repo.delete_category(shop, category)
        if not removed:
            QMessageBox.information(self, "提示", "每个店铺至少要保留一个分类")
            return
        if self.current_shop == shop and self.current_category == category:
            self._set_current(shop, self.repo.first_category(shop))
        self.refresh_shop_tree()
        self.refresh_table()

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
        with self._undo_step("删除店铺"):
            self.repo.delete_shop(name)
            if self.current_shop == name:
                self.current_shop = None
                self.current_category = None
                self.current_shop_label.setText("请选择左侧店铺")
                self.table.render([])
        self.refresh_shop_tree()

    def _on_shop_label_context_menu(self, pos) -> None:
        """店铺名右键：填充色（加深预设 + 自选）+ 字体颜色（预设 + 自选）+ 清除，按店铺记忆"""
        menu = QMenu(self)
        # —— 填充色（背景）：稍深一点，方便区分不同店铺 ——
        bg_menu = menu.addMenu("填充色")
        bg_presets = [
            ("#B3D9FF", "蓝"), ("#B3E6B3", "绿"), ("#FFD9A0", "橙"),
            ("#FFB3B3", "红"), ("#D9B3FF", "紫"), ("#FFEC99", "黄"),
            ("#D0D0D0", "灰"),
        ]
        for hex_color, name in bg_presets:
            act = bg_menu.addAction(f"  {name}")
            act.setData(("bg", hex_color))
        bg_menu.addSeparator()
        act_bg_more = bg_menu.addAction("更多颜色...")
        act_bg_more.setData(("bg", "more"))
        act_bg_clear = bg_menu.addAction("清除填充色")
        act_bg_clear.setData(("bg", None))
        # —— 字体颜色 ——
        text_menu = menu.addMenu("字体颜色")
        text_presets = [
            ("#000000", "黑色"), ("#1F3A93", "深蓝"), ("#C0392B", "深红"),
            ("#1E8449", "深绿"), ("#6C3483", "深紫"), ("#D35400", "橙色"),
            ("#2C3E50", "深灰"),
        ]
        for hex_color, name in text_presets:
            act = text_menu.addAction(f"  {name}")
            act.setData(("text", hex_color))
        text_menu.addSeparator()
        act_text_more = text_menu.addAction("更多颜色...")
        act_text_more.setData(("text", "more"))
        act_text_clear = text_menu.addAction("恢复默认字体色")
        act_text_clear.setData(("text", None))
        # —— 执行 ——
        chosen = menu.exec(self.current_shop_label.mapToGlobal(pos))
        if chosen is None:
            return
        data = chosen.data()
        if not isinstance(data, tuple):
            return
        kind, value = data
        if value == "more":
            title = "选择填充色" if kind == "bg" else "选择字体颜色"
            color = QColorDialog.getColor(parent=self, title=title)
            if color.isValid():
                value = color.name()
            else:
                return
        self._apply_shop_label_style(
            bg_color=value if kind == "bg" else None,
            text_color=value if kind == "text" else None,
            replace_bg=(kind == "bg"),
            replace_text=(kind == "text"),
        )

    def _apply_shop_label_style(self, bg_color=None, text_color=None,
                                  replace_bg=True, replace_text=True,
                                  persist=True) -> None:
        """设置当前店铺名样式（背景色 + 字体色），按店铺名记忆。
        replace_bg/replace_text 为 True 时才覆盖对应维度（用于菜单只改其中一项）。
        颜色为 None 表示清除该维度。
        persist=True 时写入 data.json（菜单改色）；切换到已有店铺时传 False，
        只是把存下来的颜色套回标签，不必因此写一次盘。"""
        cur = {"bg": None, "text": None}
        if self.current_shop and self.current_category:
            key = self._color_key(self.current_shop, self.current_category)
            cur = self._get_color(self.current_shop, self.current_category).copy()
            if replace_bg:
                cur["bg"] = bg_color
            if replace_text:
                cur["text"] = text_color
            if cur["bg"] is None and cur["text"] is None:
                self._shop_label_colors.pop(key, None)
            else:
                self._shop_label_colors[key] = cur
        # 实时应用到标签
        parts = [self._shop_label_base_style]
        if cur.get("bg"):
            parts.append(f"background-color: {cur['bg']};")
        if cur.get("text"):
            parts.append(f"color: {cur['text']};")
        if cur.get("bg"):
            parts.append("border-radius: 6px;")
        self.current_shop_label.setStyleSheet(" ".join(parts))
        if persist and self.current_shop:
            # 颜色要跟着 data.json 走：导出表单图片的标题带读的就是它，
            # 不落盘的话重启后颜色全丢，导出图和界面就对不上了
            self.save_data()

    def on_shop_selected(self, item, column):
        """点击树节点：顶层=店铺（自动落到它的第一个分类），子级=产品分类。

        界面上**永远有一个明确的「当前分类」**，表格/导出/新增都只针对它，
        这样"这条记录该归到哪个分类"不会出现歧义。
        """
        if item.parent() is None:
            shop = item.text(0)
            category = self.repo.first_category(shop)
        else:
            shop = item.parent().text(0)
            # 子节点文字带「（数量）」后缀，分类名从 UserRole 取，别拿后缀当分类名
            category = item.data(0, Qt.ItemDataRole.UserRole) or item.text(0)
        self._set_current(shop, category)
        self.refresh_table()

    def _set_current(self, shop: str, category: str) -> None:
        """记录当前店铺+分类，并把右侧标题、店铺名配色一起带上（一个入口，不散落）"""
        if self.current_shop and hasattr(self, "table"):
            self._col_widths_memory[self.current_shop] = self.table.save_column_widths()
        self.current_shop = shop
        self.current_category = category
        self.current_shop_label.setText(shop)
        saved = self._get_color(shop, category)
        self._apply_shop_label_style(bg_color=saved.get("bg"), text_color=saved.get("text"),
                                     persist=False)

    def on_shop_double_clicked(self, item, column):
        if item.childCount() > 0:
            item.setExpanded(not item.isExpanded())

    def _current_item_kind(self) -> str:
        """右键选中的节点类型：shop=店铺 / category=产品分类 / none=空白处"""
        item = self.shop_tree.currentItem()
        if item is None:
            return "none"
        return "shop" if item.parent() is None else "category"

    def _on_shop_tree_menu(self, pos):
        """店铺树右键菜单：店铺 / 分类 / 空白处 各有各的操作，全部在这里收口"""
        item = self.shop_tree.itemAt(pos)
        if item is not None:
            self.shop_tree.setCurrentItem(item)
        kind = self._current_item_kind()
        menu = QMenu(self)
        if kind == "shop":
            act_add_shop = menu.addAction("新增店铺")
        else:
            act_add_shop = None
        if kind == "shop":
            shop = self.shop_tree.currentItem().text(0)
            menu.addSeparator()
            act_add_cat = menu.addAction("新增产品分类")
            act_rename = menu.addAction("重命名店铺")
            menu.addSeparator()
            act_del_shop = menu.addAction("删除店铺")
        elif kind == "category":
            menu.addSeparator()
            act_new_record = menu.addAction("新建商品")
            menu.addSeparator()
            act_add_cat = menu.addAction("新增产品分类")
            act_rename_cat = menu.addAction("重命名分类")
            act_del_cat = menu.addAction("删除分类")
        chosen = menu.exec(self.shop_tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if act_add_shop is not None and chosen is act_add_shop:
            self.on_add_shop()
        elif kind == "shop" and chosen is act_add_cat:
            self.on_add_category(shop)
        elif kind == "shop" and chosen is act_rename:
            self.on_rename_shop()
        elif kind == "shop" and chosen is act_del_shop:
            self.on_delete_shop()
        elif kind == "category":
            node = self.shop_tree.currentItem()
            shop = node.parent().text(0)
            # 🔴 分类名从 UserRole 取：节点文字带「（数量）」后缀（如 分类一（2）），
            # 拿后缀名去数据层当 old 名会找不到 → 重命名/删除静默失败
            category = node.data(0, Qt.ItemDataRole.UserRole) or node.text(0)
            if chosen is act_new_record:
                # 切到该分类并打开新增商品对话框
                self.shop_tree.setCurrentItem(node)
                self.current_category = category
                self.current_shop = shop
                self.refresh_table()
                self.on_add_record()
            elif chosen is act_add_cat:
                self.on_add_category(shop)
            elif chosen is act_rename_cat:
                self.on_rename_category(shop, category)
            elif chosen is act_del_cat:
                self.on_delete_category(shop, category)

    def refresh_shop_tree(self):
        """重建店铺树：店铺（顶层）-> 产品分类（子级，带素材数量）"""
        self.shop_tree.clear()
        for shop_name, categories in self.repo.shops.items():
            shop_item = QTreeWidgetItem([shop_name])
            # 店名不可在树上直接编辑（改名统一走重命名，保证能保存）
            shop_item.setFlags(shop_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            # 拖拽排序：顶层店铺可拖，但不可作为 drop 目标（避免被拖成另一个店铺的子项）
            shop_item.setFlags(shop_item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
            shop_item.setFlags(shop_item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self.shop_tree.addTopLevelItem(shop_item)
            for category, records in categories.items():
                cat_item = QTreeWidgetItem([f"{category}（{len(records)}）"])
                # 真实分类名存到 UserRole：树节点文字带「（数量）」后缀，
                # 选中/恢复时用它取干净的分类名，避免把后缀当分类名用
                cat_item.setData(0, Qt.ItemDataRole.UserRole, category)
                cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                # 分类不可拖（拖拽排序只针对店铺），也不可作为 drop 目标
                cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
                cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
                shop_item.addChild(cat_item)
        if self._all_expanded:
            self.shop_tree.expandAll()
        else:
            self.shop_tree.collapseAll()
        self._restore_tree_selection()

    def _restore_tree_selection(self) -> None:
        """重建树之后把之前选中的店铺/分类选回去（否则每次刷新都跳回第一项）"""
        if not self.current_shop:
            return
        for i in range(self.shop_tree.topLevelItemCount()):
            shop_item = self.shop_tree.topLevelItem(i)
            if shop_item.text(0) != self.current_shop:
                continue
            if not self.current_category:
                self.shop_tree.setCurrentItem(shop_item)
                return
            for j in range(shop_item.childCount()):
                child = shop_item.child(j)
                real = child.data(0, Qt.ItemDataRole.UserRole) or child.text(0)
                if real == self.current_category or child.text(0).startswith(self.current_category):
                    self.shop_tree.setCurrentItem(child)
                    return
            self.shop_tree.setCurrentItem(shop_item)
            return

    def on_toggle_tree(self):
        """一键展开/折叠全部店铺"""
        self._all_expanded = not self._all_expanded
        if self._all_expanded:
            self.shop_tree.expandAll()
            self.btn_toggle_tree.setText("全部折叠")
        else:
            self.shop_tree.collapseAll()
            self.btn_toggle_tree.setText("全部展开")

    def select_shop_in_tree(self, name, category: str | None = None):
        """选中某店铺（默认落到它的第一个分类）并刷新右侧表格"""
        for i in range(self.shop_tree.topLevelItemCount()):
            item = self.shop_tree.topLevelItem(i)
            if item.text(0) != name:
                continue
            category = category or self.repo.first_category(name)
            for j in range(item.childCount()):
                child = item.child(j)
                real = child.data(0, Qt.ItemDataRole.UserRole) or child.text(0)
                if real == category or child.text(0).startswith(category):
                    item = child
                    break
            self.shop_tree.setCurrentItem(item)
            self.on_shop_selected(item, 0)
            break

    def on_shop_order_changed(self, new_order):
        """拖拽排序结束：按新顺序重排数据层店铺并持久化（不触发 tree 重建，保留动效）"""
        with self._undo_step("调整店铺顺序"):
            self.repo.reorder_shops(new_order)

    # ==================== 素材记录管理 ====================
    def refresh_table(self):
        # 恢复当前店铺记忆的列宽（如果有）
        saved_w = self._col_widths_memory.get(self.current_shop)
        if saved_w:
            QTimer.singleShot(50, lambda: self.table.load_column_widths(saved_w))
        """按当前店铺+产品分类重新渲染表格"""
        records = self._records() if self.current_shop else []
        self.table.render(records)
        # 左侧栏收起时右侧表格保持完整列宽（不被压到字段看不见）。
        # _auto_fit_columns 是 singleShot 异步算列宽的，这里延迟到列宽算完再设最小宽，
        # 否则此刻 _col_mins 还是空的，右侧没有最小宽支撑、折叠时列被压成一团
        if hasattr(self, 'record_panel'):
            def _apply_min_width():
                self.record_panel.setMinimumWidth(self.table.total_min_width())
            QTimer.singleShot(80, _apply_min_width)
        self.selection_label.setText("已选 0 条")

    # ---------- 当前店铺+分类 的统一入口（表格/导出/搜索/记录操作都走这里） ----------
    def _records(self) -> list:
        """当前选中的产品分类下的记录列表"""
        return self.repo.get_records(self.current_shop, self.current_category)

    def on_add_blank_record(self):
        """底部➕号按钮：在当前店铺末尾添加一行空白记录。

        走「增量追加」而非整表 refresh_table：
        - 不再 setRowCount(0) 重建所有行，画面不会闪，滚动条也不会被复位到顶部
        - 已有行的勾选状态保留
        - 追加后平滑缓动到底部，让新行与➕按钮自然进入视野
        搜索过滤态下新记录不在结果集内，退回整表渲染（此时本就该看到过滤结果）。
        """
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择左侧店铺")
            return
        from ..storage import new_blank_record
        record = new_blank_record()
        records = self._records()
        with self._undo_step("添加空白记录"):
            records.append(record)
        if self.table.is_filtered():
            self.refresh_table()
        else:
            self.table.append_record(record, len(records) - 1)
        # 等 Qt 走完布局（新行行高 / 底部余量）再缓动到底部
        self.table.scroll_to_bottom()
        logger.info("添加空白记录到店铺: %s", self.current_shop)

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
        with self._undo_step("新增记录"):
            self.repo.add_record(self.current_shop, self.current_category, record)
        self._after_records_changed()

    def _current_table_row(self) -> int:
        """取当前要操作的行：优先 currentRow；若用户只点了勾选框导致
        currentRow 停在旧位置，则回退到选择模型中选中的行"""
        row = self.table.currentRow()
        if row >= 0:
            return row
        # 单元格选择模式下 selectedRows 常为空，回退到任意被选中的格所在行
        selected = self.table.selectionModel().selectedIndexes()
        return selected[0].row() if selected else -1

    # ==================== 撤销（Ctrl+Z） ====================
    @contextmanager
    def _undo_step(self, label: str):
        """把一段数据变更包成「可撤销的一步」。

        用法：`with self._undo_step("删除 3 条记录"): <改数据的代码>`
        - 变更前存一份快照，变更后**只有数据真的变了**才入栈（避免空操作占满撤销栈）
        - 顺带统一落盘，调用方不用再记得调 save_data()
        快照里带上 shop_colors：改名/删店铺时配色也跟着回退，不会出现"店铺回来了、
        颜色还挂在旧名字上"的错位。
        """
        before = {"shops": snapshot(self.repo.shops),
                  "colors": snapshot(self.repo.shop_colors)}
        yield
        if self.repo.shops != before["shops"] or self.repo.shop_colors != before["colors"]:
            self._undo.push(label, before)
            self.save_data()

    def on_undo(self) -> None:
        """Ctrl+Z：回退上一步数据变更（撤销本身不再入栈，避免变成"来回横跳"）"""
        if not self._undo.can_undo():
            QToolTip.showText(QCursor.pos(), "没有可撤销的操作", self, msecShowTime=1200)
            return
        label, data = self._undo.pop()
        self.repo.shops = data["shops"]
        self.repo.shop_colors = data["colors"]
        self._after_records_changed()
        self.save_data()
        logger.info("撤销：%s", label)
        QToolTip.showText(QCursor.pos(), f"已撤销：{label}", self, msecShowTime=1800)

    # ==================== 记录增删：统一入口 ====================
    def _after_records_changed(self) -> None:
        """数据改完的统一收尾：刷新表格与店铺树（顺序固定，避免各处漏掉一个）"""
        self.refresh_table()
        self.refresh_shop_tree()

    def _confirm(self, title: str, text: str) -> bool:
        """统一的确认框（默认停在「否」，回车/点「是」才会继续）"""
        return QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _remove_records(self, indices: list) -> None:
        """按原始记录索引批量删除；**倒序**删，前面的删除不会挪动后面记录的位置"""
        for index in sorted(set(indices), reverse=True):
            self.repo.remove_record(self.current_shop, self.current_category, index)

    def _duplicate_records(self, indices: list) -> None:
        """把选中的记录各复制一份，按原顺序追加到当前分类末尾（不弹表单、直接复制）"""
        records = self._records()
        for index in sorted(set(indices)):
            if 0 <= index < len(records):
                self.repo.add_record(self.current_shop, self.current_category,
                                     deepcopy(records[index]))

    def _delete_records(self, indices: list, exit_check_mode: bool = False) -> None:
        """删除记录的唯一入口：确认 → 快照 → 删 → 刷新"""
        if not indices:
            return
        count = len(indices)
        tip = f"确定要删除选中的 {count} 条记录吗？" if count > 1 else "确定要删除选中的记录吗？"
        if not self._confirm("确认删除", tip):
            return
        with self._undo_step(f"删除 {count} 条记录"):
            self._remove_records(indices)
        self._after_records_changed()
        if exit_check_mode:
            self.table.set_selection_mode(False)     # 勾选模式操作完自动收回复选框
        QToolTip.showText(QCursor.pos(), f"已删除 {count} 条记录", self, msecShowTime=1500)

    def _copy_records(self, indices: list, exit_check_mode: bool = False) -> None:
        """批量复制记录的唯一入口：快照 → 复制 → 刷新（不弹表单）"""
        if not indices:
            return
        with self._undo_step(f"复制 {len(indices)} 条记录"):
            self._duplicate_records(indices)
        self._after_records_changed()
        if exit_check_mode:
            self.table.set_selection_mode(False)
        QToolTip.showText(QCursor.pos(), f"已复制 {len(indices)} 条记录", self, msecShowTime=1500)

    def on_delete_records(self, indices: list) -> None:
        """右键「删除选中的 N 条记录」（选区 / Ctrl+A 全选后）"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        self._delete_records(list(indices))

    def on_copy_records(self, indices: list) -> None:
        """右键「复制选中的 N 条记录」（选区 / Ctrl+A 全选后）"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        self._copy_records(list(indices))

    def on_delete_record(self, row=None):
        """删除单条记录：右键指定行，或按钮/快捷键取当前行；勾选模式下删勾选的那些"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        # bool 是 int 子类：误传入信号 bool 时统一按“未指定行”处理
        if not isinstance(row, int) or isinstance(row, bool):
            row = self._current_table_row()
        checked = self.table.selected_rendered_indices()
        if not self.table.isColumnHidden(0) and checked:
            self._delete_records(checked, exit_check_mode=True)
            return
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中要删除的行（或右键该行 → 删除记录）")
            return
        self._delete_records([self.table.rendered_index(row)])

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
        old_record = self._records()[record_index]

        dialog = AddRecordDialog(self, record=old_record)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_record = dialog.get_data()
        if not new_record:
            return
        with self._undo_step("修改记录"):
            self._records()[record_index] = new_record
        self._after_records_changed()

    def on_copy_record(self, row=None):
        """复制记录：与修改一致弹出预填表单，确认后在原记录之后插入一条相同记录"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        # bool 是 int 子类：误传入信号 bool 时统一按“未指定行”处理
        if not isinstance(row, int) or isinstance(row, bool):
            row = self._current_table_row()
        # 批量选择模式下勾选了至少一条：直接整批复制，不再逐条弹表单
        checked = self.table.selected_rendered_indices()
        if not self.table.isColumnHidden(0) and checked:
            self._copy_records(checked, exit_check_mode=True)
            return
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中要复制的记录")
            return
        record_index = self.table.rendered_index(row)
        old_record = self._records()[record_index]

        dialog = AddRecordDialog(self, record=old_record)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_record = dialog.get_data()
        if not new_record:
            return
        # 插到原记录正后方，并在刷新后选中新复制出的这一行
        with self._undo_step("复制记录"):
            new_pos = self.repo.insert_record(
                self.current_shop, self.current_category, record_index, new_record)
        self._after_records_changed()
        self.table.setCurrentCell(new_pos, 1)

    def on_cell_edited(self, row, field, text):
        """文本格双击就地编辑完成：直接写回该字段并保存，行高随之重排（不重渲表格）"""
        if not self.current_shop:
            return
        record_index = self.table.rendered_index(row)
        with self._undo_step("修改单元格"):
            self.repo.set_record_field(self.current_shop, self.current_category,
                                       record_index, field, text)
            # 规格选择/编辑时：把用过的款都存进 spec_options，下次下拉还能选
            if field == "spec":
                recs = self.repo.get_records(self.current_shop, self.current_category)
                rec = recs[record_index] if record_index < len(recs) else {}
                opts = list(rec.get("spec_options") or [])
                from .record_table import smart_split_spec
                for piece in smart_split_spec(text):
                    if piece and piece not in opts:
                        opts.append(piece)
                self.repo.set_record_field(self.current_shop, self.current_category,
                                           record_index, "spec_options", opts)
        self.table._adjust_row_heights()

    def on_spec_option_added(self, row, option_text):
        """规格列右键新增规格：追加到该记录的 spec_options，并刷新下拉列表。"""
        if not self.current_shop or not self.current_category:
            return
        record_index = self.table.rendered_index(row)
        with self._undo_step("新增规格"):
            recs = self.repo.get_records(self.current_shop, self.current_category)
            if record_index >= len(recs):
                return
            rec = recs[record_index]
            opts = list(rec.get("spec_options") or [])
            if option_text not in opts:
                opts.append(option_text)
            self.repo.set_record_field(self.current_shop, self.current_category,
                                       record_index, "spec_options", opts)
        # 刷新该行规格列的下拉选项（只重渲该行，不整表刷新，避免卡顿）
        self.refresh_table()

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
        # 粘贴不替换：多图列追加到末尾，单图列直接替换（只有一张）
        with self._undo_step("粘贴图片"):
            if field_name in config.MULTI_IMAGE_FIELDS:
                self.repo.append_record_image(
                    self.current_shop, self.current_category,
                    record_index, filepath, field_name)
            else:
                self.repo.set_record_field(
                    self.current_shop, self.current_category,
                    record_index, field_name, filepath)
        self.refresh_table()
        if field_name == "image_paths":
            self.image_library_changed.emit()

    def on_delete_image_requested(self, row, field_name, img_index):
        """删除单元格内的某张图片：多图移除指定序号，单图直接清空（仅移除引用）"""
        if not self.current_shop:
            return
        record_index = self.table.rendered_index(row)
        with self._undo_step("删除图片"):
            if field_name in config.MULTI_IMAGE_FIELDS:
                self.repo.remove_record_image(
                    self.current_shop, self.current_category,
                    record_index, img_index, field_name)
            else:
                self.repo.set_record_field(
                    self.current_shop, self.current_category,
                    record_index, field_name, "")
        self.refresh_table()
        if field_name == "image_paths":
            self.image_library_changed.emit()

    def _on_toggle_select_menu(self, checked: bool):
        """菜单里点「选择」：切换批量选择模式，同步表头按钮与勾选列"""
        self.table.set_selection_mode(checked)
        if checked:
            self.act_select.setText("✅ 选择")
        else:
            self.act_select.setText("选择")

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
        with self._undo_step("添加图片"):
            for filepath in saved:
                self.repo.append_record_image(
                    self.current_shop, self.current_category, record_index, filepath)
        self.refresh_table()
        if field_name == "image_paths":
            self.image_library_changed.emit()

    # ==================== 搜索 ====================
    def on_search(self):
        keyword = self.search_input.text().strip().lower()
        if not keyword or not self.current_shop:
            if self.current_shop:
                self.refresh_table()
            return
        records = self._records()
        matched_indices = [i for i, r in enumerate(records) if keyword in r.get("title", "").lower()]
        matched = [records[i] for i in matched_indices]
        self.table.render(matched, matched_indices)

    def on_clear_search(self):
        self.search_input.clear()
        if self.current_shop:
            self.refresh_table()

    # ==================== 淘宝登录 ====================
    def _clear_taobao_login(self):
        """清空淘宝登录状态：本地 cookie + 浏览器 profile 里的登录信息一并清除。

        浏览器 profile 里长期残留的访客 cookie（tracknick 等）会让人误以为还登录着，
        退出登录时一并清掉，下次必须重新扫码，状态才可信。
        """
        try:
            self.taobao.clear_cookies()
            taobao_playwright.clear_profile_login()
            logger.info("已清空淘宝登录状态（本地 cookie + 浏览器 profile）")
        except Exception as e:
            logger.warning("清空淘宝登录状态失败: %s", e)
        self.btn_taobao_login.setText("淘宝登录")
        self.btn_taobao_login.setToolTip("")

    def _refresh_login_button(self):
        """根据当前登录状态刷新按钮显示"""
        if self.taobao.is_logged_in():
            count = len(self.taobao.session.cookies)
            self.btn_taobao_login.setText("淘宝已登录 ✓")
            self.btn_taobao_login.setToolTip(f"已登录淘宝（{count} 条 cookie）")
        else:
            self.btn_taobao_login.setText("淘宝登录")
            self.btn_taobao_login.setToolTip("")

    def on_taobao_login(self):
        """淘宝登录按钮：已登录则显示状态并提供重新登录/退出登录，未登录则弹登录框"""
        logger.info("点击淘宝登录按钮，当前登录状态: %s", "已登录" if self.taobao.is_logged_in() else "未登录")
        if self.taobao.is_logged_in():
            # 已登录：提供重新登录和退出登录选项
            msg = QMessageBox(self)
            msg.setWindowTitle("淘宝登录")
            msg.setText(f"当前已登录淘宝\n{self.taobao.cookie_summary()}")
            btn_relogin = msg.addButton("重新登录", QMessageBox.ButtonRole.AcceptRole)
            btn_logout = msg.addButton("退出登录", QMessageBox.ButtonRole.DestructiveRole)
            msg.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == btn_relogin:
                # 重新登录：清除本地 cookie 与浏览器登录信息后弹登录框（保证重新扫码）
                logger.info("用户选择重新登录，清除旧登录状态")
                self._clear_taobao_login()
                self._show_taobao_login_dialog()
            elif clicked == btn_logout:
                # 退出登录：清除本地 cookie 与浏览器登录信息，按钮恢复
                logger.info("用户选择退出登录，清除登录状态")
                self._clear_taobao_login()
                self.btn_taobao_login.setToolTip("登录淘宝获取 cookie，用于后续导入商品素材")
            else:
                logger.info("用户取消登录操作")
            return
        self._show_taobao_login_dialog()

    def _show_taobao_login_dialog(self):
        """弹出淘宝登录对话框"""
        logger.info("弹出淘宝登录对话框")
        dialog = TaobaoLoginDialog(self)
        dialog.cookies_received.connect(self._on_taobao_cookies_received)
        dialog.exec()
        logger.info("淘宝登录对话框已关闭")

    # ==================== 链接栏右键：抓取/填充 ====================
    def on_link_fetch(self, row: int, url: str):
        """链接栏右键：抓取此链接，成功后提示抓取到多少张图"""
        url = extract_product_url(url or "")  # 兼容历史脏数据：可能存的是整段分享口令
        if not url:
            QMessageBox.information(self, "提示", "该记录没有商品链接")
            return
        if not self.taobao.is_logged_in():
            QMessageBox.information(self, "提示", "请先点击「淘宝登录」完成登录")
            return
        logger.info("链接栏抓取: row=%d, url=%s", row, url[:60])
        self._global_loading.setGeometry(self.centralWidget().rect())
        self._global_loading.show_with_text("正在打开 Chrome 抓取商品...\n（首次启动较慢，请稍候）")
        self._global_loading.raise_()

        self._link_fetch_thread = QThread()
        self._link_fetch_worker = _LinkFetchWorker(url)
        self._link_fetch_worker.moveToThread(self._link_fetch_thread)
        self._link_fetch_thread.started.connect(self._link_fetch_worker.run)
        self._link_fetch_worker.done.connect(lambda res, r=row: self._on_link_fetch_done(res, r))
        self._link_fetch_worker.done.connect(self._link_fetch_thread.quit)
        self._link_fetch_thread.start()

    def _on_link_fetch_done(self, res: dict, row: int):
        """链接栏抓取完成"""
        self._global_loading.hide_overlay()
        if res.get("error"):
            err = res["error"]
            # cookie失效时自动清空登录状态
            if "cookie" in err and ("失效" in err or "重新登录" in err):
                self._clear_taobao_login()
            logger.error("链接栏抓取失败: %s", err)
            QMessageBox.warning(self, "抓取失败", err)
            return
        img_count = len(res.get("images", []))
        sku_count = len(res.get("sku_images", []))
        logger.info("链接栏抓取成功: row=%d, 标题=%s, 图片=%d张, SKU图=%d张",
                    row, res.get("title", "")[:30], img_count, sku_count)
        QMessageBox.information(self, "抓取成功",
            f"商品：{res.get('title', '')[:40]}\n"
            f"价格：{res.get('price', '')}\n"
            f"商品主图：{img_count} 张\n"
            f"规格图（SKU）：{sku_count} 张\n\n"
            f"可再次右键选择「抓取并填充到此行」将数据填入此记录")

    def on_link_fill(self, row: int, url: str):
        """链接栏右键：抓取并填充到此行"""
        url = extract_product_url(url or "")  # 兼容历史脏数据：可能存的是整段分享口令
        if not url:
            QMessageBox.information(self, "提示", "该记录没有商品链接")
            return
        if not self.taobao.is_logged_in():
            QMessageBox.information(self, "提示", "请先点击「淘宝登录」完成登录")
            return
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        logger.info("链接栏填充: row=%d, url=%s", row, url[:60])
        self._global_loading.setGeometry(self.centralWidget().rect())
        self._global_loading.show_with_text("正在抓取并填充商品数据...\n（下载图片可能需要几秒）")
        self._global_loading.raise_()

        self._link_fill_thread = QThread()
        self._link_fill_worker = _LinkFetchWorker(url)
        self._link_fill_worker.moveToThread(self._link_fill_thread)
        self._link_fill_thread.started.connect(self._link_fill_worker.run)
        self._link_fill_worker.done.connect(lambda res, r=row: self._on_link_fill_done(res, r))
        self._link_fill_worker.done.connect(self._link_fill_thread.quit)
        self._link_fill_thread.start()

    def _on_link_fill_done(self, res: dict, row: int):
        """链接栏填充完成：下载图片并更新当前行记录"""
        try:
            if res.get("error"):
                err = res["error"]
                # cookie失效时自动清空登录状态
                if "cookie" in err and ("失效" in err or "重新登录" in err):
                    self._clear_taobao_login()
                self._global_loading.hide_overlay()
                QMessageBox.warning(self, "抓取失败", err)
                return

            record_idx = self.table.rendered_index(row)
            shop_records = self._records()
            if record_idx < 0 or record_idx >= len(shop_records):
                self._global_loading.hide_overlay()
                QMessageBox.warning(self, "错误", "行号无效")
                return

            record = shop_records[record_idx]
            item_id = str(res.get("item_id", ""))

            # 下载图片走服务层（失败返回空串，不中断流程）
            main_img = res.get("main_image") or (res.get("images") or [None])[0]
            link_image = taobao_import.download_image(main_img, "tb_main", item_id)
            QApplication.processEvents()

            spec_images = []
            for img_url in (res.get("sku_images") or []):
                path = taobao_import.download_image(img_url, "tb_sku", item_id)
                if path:
                    spec_images.append(path)
                QApplication.processEvents()
            if not spec_images and main_img:
                spec_images = [link_image] if link_image else []

            spec_text = taobao_import.build_spec_text(res.get("skus") or [])

            # 更新记录（只更新抓到的字段，保留原有的补手/评价/评价图片）
            record["product_id"] = item_id or record.get("product_id", "")
            record["title"] = res.get("title", "") or record.get("title", "")
            record["spec"] = spec_text or record.get("spec", "")
            record["link_image"] = [link_image] if link_image else record.get("link_image", [])
            record["spec_image"] = spec_images if spec_images else record.get("spec_image", [])

            self.repo.save()
            self.refresh_table()
            self._global_loading.hide_overlay()
            logger.info("链接栏填充成功: row=%d, 商品ID=%s, 规格图=%d张", row, item_id, len(spec_images))
            QMessageBox.information(self, "填充成功",
                f"已更新此记录：\n"
                f"标题：{res.get('title', '')[:30]}\n"
                f"规格图：{len(spec_images)} 张\n"
                f"链接主图：{'已更新' if link_image else '未获取'}")
        except Exception as e:
            self._global_loading.hide_overlay()
            logger.exception("链接栏填充异常")
            QMessageBox.critical(self, "错误", f"填充失败：{e}")

    # ==================== 一键导入（批量抓取填充） ====================
    def on_batch_import(self):
        """一键导入：批量抓取当前店铺所有已填链接的商品信息并填入记录。

        筛选规则：商品链接非空、且尚未导入（标题与链接主图未同时齐备）的记录；
        已导入过的跳过（单条刷新请用商品链接列右键「抓取并填充到此行」）。
        """
        # 0. 正在导入中：忽略重复点击
        running = getattr(self, "_batch_import_thread", None)
        if running is not None and running.isRunning():
            return
        # 1. 前置检查：店铺
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先在左侧选择一个店铺")
            return
        # 2. 前置检查：cookie 登录状态（本地检测，不发网络请求，避免触发风控）
        if not self.taobao.is_logged_in():
            QMessageBox.information(self, "提示", "请先点击「淘宝登录」完成登录，再一键导入")
            return
        # 3. 筛选待导入任务
        records = self._records()
        tasks = []
        skipped = 0
        for idx, record in enumerate(records):
            # 兼容历史脏数据：单元格里可能存着整段【淘宝】分享口令，识别出纯链接再入队
            url = extract_product_url(record.get("product_url") or "")
            if not url:
                continue
            if record.get("title") and record.get("link_image"):
                skipped += 1
                continue
            tasks.append((idx, url))
        if not tasks:
            tip = f"（其中 {skipped} 条已导入过，自动跳过）" if skipped else ""
            QMessageBox.information(
                self, "提示",
                f"当前店铺没有待导入的记录{tip}\n请先在「商品链接」列填写淘宝商品链接",
            )
            return

        total = len(tasks)
        logger.info("一键导入开始: 店铺=%s, 待导入=%d, 已跳过=%d",
                    self.current_shop, total, skipped)
        self._global_loading.setGeometry(self.centralWidget().rect())
        self._global_loading.show_with_text(
            f"正在启动 Chrome 准备批量导入...\n共 {total} 条商品，每条约需 10~20 秒，请耐心等待"
        )
        self._global_loading.raise_()

        self._batch_import_thread = QThread()
        self._batch_import_worker = BatchImportWorker(tasks)
        self._batch_import_worker.moveToThread(self._batch_import_thread)
        self._batch_import_thread.started.connect(self._batch_import_worker.run)
        self._batch_import_worker.progress.connect(self._on_batch_import_progress)
        self._batch_import_worker.item_done.connect(self._on_batch_import_item_done)
        self._batch_import_worker.cookie_invalid.connect(self._on_batch_import_cookie_invalid)
        self._batch_import_worker.finished.connect(self._on_batch_import_finished)
        self._batch_import_worker.finished.connect(self._batch_import_thread.quit)
        self._batch_import_thread.start()

    def _on_batch_import_progress(self, cur: int, total: int, url: str):
        """每条开始抓取：更新中央加载动画的进度文字"""
        short = url if len(url) <= 46 else url[:43] + "..."
        self._global_loading.update_text(
            f"正在导入 {cur}/{total}：{short}\n获取失败的商品会自动跳过"
        )

    def _on_batch_import_item_done(self, record_index: int, payload: dict):
        """单条结束：失败的跳过不动记录，成功的把字段填入并落库。

        界面统一在 finished 后刷新（动画期间逐条重建表格只会闪烁并重置滚动位置），
        数据则逐条保存，中途关闭程序也不丢已导入的部分。
        """
        if payload.get("error"):
            logger.warning("一键导入记录 #%d 失败（跳过）: %s", record_index, payload["error"])
            return
        records = self._records()
        if not (0 <= record_index < len(records)):
            logger.warning("一键导入记录 #%d 越界，忽略", record_index)
            return
        record = records[record_index]
        for field, value in (payload.get("updates") or {}).items():
            if value:  # 空值不覆盖原有内容
                record[field] = value
        self.repo.save()
        logger.info("一键导入记录 #%d 填充成功: %s", record_index, payload.get("title", "")[:30])

    def _on_batch_import_cookie_invalid(self, msg: str):
        """cookie 失效：清空登录状态并提示（动画由 finished 统一收尾）"""
        logger.warning("一键导入因 cookie 失效提前终止: %s", msg)
        self._clear_taobao_login()

    def _on_batch_import_finished(self, summary: dict):
        """全部结束：隐藏中央动画、刷新界面并弹出汇总"""
        self._global_loading.hide_overlay()
        self.refresh_table()
        self.refresh_shop_tree()
        lines = [f"成功导入：{summary['ok']} 条"]
        if summary["fail"]:
            lines.append(f"获取失败（已跳过）：{summary['fail']} 条")
        if summary["aborted"]:
            lines.append(f"因 cookie 失效未导入：{summary['aborted']} 条（请重新登录后再试）")
        QMessageBox.information(self, "一键导入完成", "\n".join(lines))
        logger.info("一键导入完成: 成功=%d 失败=%d 中止=%d",
                    summary["ok"], summary["fail"], summary["aborted"])

    def on_open_fetch(self):
        """打开抓取商品对话框（粘贴链接抓取标题/价格/主图）"""
        if not self.taobao.is_logged_in():
            QMessageBox.information(self, "提示", "请先点击「淘宝登录」完成登录")
            return
        logger.info("打开抓取商品对话框")
        dialog = TaobaoFetchDialog(self)
        dialog.fill_requested.connect(self._on_fill_fetch_result)
        dialog.exec()
        # 对话框关闭后刷新登录按钮状态（可能因cookie失效被清空）
        self._refresh_login_button()

    def _on_fill_fetch_result(self, results):
        """把淘宝抓取结果（单条 dict 或多条 list）填充为新记录到当前店铺"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先在左侧选择一个店铺")
            return
        if isinstance(results, dict):
            results = [results]
        ok = 0
        for res in results:
            if res.get("error"):
                continue
            try:
                self._fill_one_fetch(res)
                ok += 1
            except Exception:
                logger.exception("填充抓取结果失败: %s", res.get("title", ""))
        QMessageBox.information(self, "完成", f"已填充 {ok}/{len(results)} 个商品到店铺「{self.current_shop}」")

    def _fill_one_fetch(self, res: dict):
        """把一条抓取结果填充为新记录到当前店铺"""
        try:
            item_id = str(res.get("item_id", ""))

            # 1. 下载链接主图（第一张商品主图）
            main_img = res.get("main_image") or (res.get("images") or [None])[0]
            link_image = taobao_import.download_image(main_img, "tb_main", item_id)
            QApplication.processEvents()

            # 2. 下载 SKU 图作为规格图（多张，全部下载）
            sku_imgs = res.get("sku_images") or []
            logger.info("开始下载 %d 张 SKU 图作为规格图", len(sku_imgs))
            spec_images = []
            for img_url in sku_imgs:
                path = taobao_import.download_image(img_url, "tb_sku", item_id)
                if path:
                    spec_images.append(path)
                # 每下载一张就让界面响应一次，避免卡死
                QApplication.processEvents()

            # 如果没有 SKU 图，用商品主图作为规格图
            if not spec_images and main_img:
                spec_images = [link_image] if link_image else []

            logger.info("SKU 图下载完成，共 %d 张", len(spec_images))

            # 3. 展平 SKU 选项，默认选第一款
            spec_opts = []
            seen = set()
            for s in (res.get("skus") or []):
                for opt in s.get("options", []):
                    opt = opt.strip()
                    if opt and opt not in seen:
                        seen.add(opt)
                        spec_opts.append(opt)
            spec_text = spec_opts[0] if spec_opts else ""
            record = {
                "product_id": item_id,
                "spec_image": spec_images,       # 规格图 = SKU图（多张）
                "spec": spec_text,
                "spec_options": spec_opts,
                "title": res.get("title", ""),
                "link_image": [link_image] if link_image else [],  # 链接主图 = 商品主图
                "helper": "",
                "review": "",
                "image_paths": [],                 # 评价图片留空（用户自己贴好评晒图）
                "product_url": res.get("url", ""),
            }

            with self._undo_step("抓取商品填充"):
                self.repo.add_record(self.current_shop, self.current_category, record)
            self.refresh_table()
            self.refresh_shop_tree()
            logger.info("已填充抓取结果到店铺[%s]分类[%s]，商品ID=%s，规格图%d张",
                        self.current_shop, self.current_category, item_id, len(spec_images))
            return
        except Exception:
            raise

    def _on_taobao_cookies_received(self, cookies: list):
        """登录成功回调：保存 cookie 并更新按钮状态。

        落库前再校验一次登录硬标志（unb/_nk_）：这是最后一道防线，
        任何情况下（cookie 文件读取失败、页面状态异常）都不会再出现"假成功"。
        """
        has_marker = taobao_session.has_login_cookie(
            (c.get("name"), c.get("value")) for c in cookies
        )
        if not has_marker:
            logger.error("登录回调缺少登录硬标志（unb/_nk_），共 %d 条 cookie，拒绝标记为已登录",
                         len(cookies))
            self._clear_taobao_login()
            QMessageBox.warning(
                self, "登录未完成",
                "未检测到有效的登录态（缺少 unb/_nk_ 登录标志）。\n\n"
                "请重新点击「淘宝登录」，在弹出的 Chrome 窗口中用淘宝 APP 扫码完成登录。",
            )
            return

        logger.info("收到登录成功回调，共 %d 条 cookie，开始保存", len(cookies))
        self.taobao.save_cookies(cookies)
        count = len(cookies)
        self.btn_taobao_login.setText("淘宝已登录 ✓")
        self.btn_taobao_login.setToolTip(f"已登录淘宝（{count} 条 cookie）")
        logger.info("淘宝登录完成，按钮状态已更新（%d 条 cookie）", count)
        QMessageBox.information(
            self,
            "登录成功",
            f"淘宝登录成功，已获取 {count} 条 cookie。\n\n"
            f"提示：抓取商品时用内置浏览器渲染页面，避免直接请求触发风控。",
        )

    # ==================== 导出（Excel / 图片 / 截图历史） ====================
    def on_export_excel(self):
        """导出当前店铺全部记录为 Excel"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出Excel", f"{self.current_shop}_素材导出.xlsx", "Excel文件 (*.xlsx)"
        )
        if not file_path:
            return
        try:
            records = self._records()
            ExcelExporter.export(
                records, file_path,
                sheet_name=self.current_shop,
                shop_name=self.current_shop,      # 首行合并的大标题
            )
            QMessageBox.information(self, "成功", f"导出成功！\n文件保存在：{file_path}")
        except ImportError:
            QMessageBox.warning(self, "错误", "openpyxl库未安装，请运行: pip install openpyxl")
        except PermissionError:
            # Windows 上目标文件正在 Excel/WPS 里打开时会被锁定，覆盖写入就是 Errno 13
            QMessageBox.warning(
                self, "文件被占用",
                "导出失败：目标文件正被占用，大概率已在 Excel / WPS 中打开：\n"
                f"{file_path}\n\n请关闭该文件后重新导出。",
            )
        except Exception as e:
            QMessageBox.warning(self, "错误", f"导出失败：{str(e)}")

    def _export_records(self, records: list) -> tuple:
        """导出图片前把记录规整好：**剔掉全是空字段的空白行**，并把表格当前的
        展开状态按新行号重新对齐（展开态以原始记录索引为键，过滤后索引会位移）。

        返回 (有效记录列表, 展开状态, 跳过的空行数)。
        """
        live_state = self.table.expanded_state() if self.table is not None else {}
        kept, remap = [], {}
        for old_index, record in enumerate(records):
            if record_is_empty(record):
                continue
            remap[len(kept)] = old_index
            kept.append(record)
        expanded = {
            new_index: live_state[old_index]
            for new_index, old_index in remap.items()
            if old_index in live_state
        }
        return kept, expanded, len(records) - len(kept)

    def on_export_image(self):
        """导出表单图片：把当前店铺整张表单（含滚出视口的行）渲染成图片并保存。

        先弹保存对话框（默认落到截图历史目录、文件名「店铺名_序号」），再进行渲染，
        避免用户取消时白等一场。渲染走离屏副本，主界面不会闪。

        截图与表单所见保持一致：
        - 顶部标题带的填充色 / 字体色 / 居中与表单里的店铺名一致
        - 展开的多图格在截图里也是展开的（反之亦然），取自表格当前状态
        - 单元格里的「➕ / ▼▲」与「（粘贴图片）」占位框不入图
        - 全是空字段的空白行跳过，不计入记录数
        """
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        all_records = self._records()
        records, expanded_state, skipped = self._export_records(all_records)
        if not records:
            tip = ("当前分类的记录都还是空白行，没有可导出的内容"
                   if all_records else "当前分类还没有记录，无法导出图片")
            QMessageBox.information(self, "提示", tip)
            return

        default_path = ScreenshotStore.default_path(self.current_shop)
        save_path, _ = QFileDialog.getSaveFileName(
            self, "导出表单图片", str(default_path), "PNG 图片 (*.png)"
        )
        if not save_path:
            return

        self._global_loading.setGeometry(self.centralWidget().rect())
        self._global_loading.show_with_text("正在生成表单图片…\n（记录较多时要等图片加载完）")
        self._global_loading.raise_()
        QApplication.processEvents()

        # 顶部标题带的颜色 = 表单里这个店铺名设置的填充色 / 字体色
        colors = self._get_color(self.current_shop, self.current_category) or {}
        error = ""
        result = None
        try:
            pixmap = capture_records_table(
                records,
                title=self.current_shop,
                title_bg=colors.get("bg"),
                title_color=colors.get("text"),
                expanded_state=expanded_state,
            )
            if pixmap is None:
                error = "图片内容为空"
            else:
                result = ScreenshotStore.save(pixmap, save_path, self.current_shop)
        except Exception as exc:   # 渲染/落盘任何一步失败都不该让程序崩
            error = str(exc)
        finally:
            self._global_loading.hide_overlay()

        if error:
            logger.warning("导出表单图片失败: %s", error)
            QMessageBox.warning(self, "失败", f"导出失败：{error}")
            return

        target, history = result
        # 导出即进剪贴板：拿到图就能直接 Ctrl+V，不用再去文件里翻
        QApplication.clipboard().setPixmap(pixmap)
        logger.info("表单图片已保存并复制到剪贴板: %s -> %s", self.current_shop, target)
        skipped_tip = f"\n\n（已跳过 {skipped} 条全空记录）" if skipped else ""
        if target == history:
            QMessageBox.information(
                self, "成功", f"图片已保存：\n{target}\n\n图片已复制到剪贴板，可直接粘贴使用。{skipped_tip}")
        else:
            QMessageBox.information(
                self, "成功",
                f"图片已保存：\n{target}\n\n已同步归档到截图历史：\n{history}"
                f"\n\n图片已复制到剪贴板，可直接粘贴使用。{skipped_tip}",
            )

    def on_screenshot_history(self):
        """打开截图历史：浏览所有归档截图，可看大图（滚轮缩放）、右键另存为"""
        ScreenshotStore.ensure_dir()   # 首次使用也先把目录建出来，对话框不至于空目录报错
        ScreenshotHistoryDialog(self, directory=ScreenshotStore.ensure_dir()).exec()
