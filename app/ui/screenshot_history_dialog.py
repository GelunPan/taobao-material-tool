"""历史截图查看：左侧缩略图列表 + 右侧按宽度铺满的预览，双击看大图（支持滚轮缩放）。

截图统一存档在 `data/screenshots/`（配置见 config.SCREENSHOTS_DIR）。
这里的定位是**方便回放与取用**，所以：
- Ctrl+C / 右键「复制」：把**图片本身**复制到剪贴板（列表项按 Ctrl+C 默认只会复制
  条目文字，这里改成复制图片），可直接粘到聊天/千牛
- 右键「删除」：删单张（二次确认后真删除）；「清空历史」清空整个目录（二次确认 + 真删除）

关于「糊」：整表截图是**又宽又长**的图（几千像素高），
- 列表缩略图若按「最长边」缩放会变成几十像素宽的细缝 → 改用「按宽度缩放后裁顶部」的封面图
- 预览/大图若按「适应窗口」缩放，长图会被压到十几分之一，字全糊 → 改为**按宽度铺满、竖向滚动**，
  大图窗口再提供滚轮缩放，想看细节直接滚到 100%
"""
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QCursor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolTip,
    QVBoxLayout,
)

from ..services import ScreenshotStore
from ..utils.logger import get_logger
from .image_utils import cover_pixmap, fit_width_pixmap

logger = get_logger(__name__)

# 列表缩略图（封面图）尺寸：按宽度铺满再裁顶部，一眼能看出表头和前几行
_THUMB_W = 168
_THUMB_H = 104
# 预览区按宽度解码时的宽度取整步长：拖动窗口时不至于每 1px 就重新解码一次
_WIDTH_STEP = 20


def _is_copy_key(event) -> bool:
    """是否按下的是「复制」快捷键：Ctrl+C（含 Ctrl+Insert 这个经典变体）"""
    if event.type() != QEvent.Type.KeyPress:
        return False
    if event.matches(QKeySequence.StandardKey.Copy):
        return True
    return (event.key() == Qt.Key.Key_Insert
            and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))


def _copy_image_to_clipboard(path: str) -> bool:
    """把图片**本身**放进剪贴板（可直接粘到聊天/千牛）；失败返回 False"""
    if not path or not os.path.exists(path):
        return False
    pixmap = QPixmap(path)
    if pixmap.isNull():
        return False
    QGuiApplication.clipboard().setPixmap(pixmap)
    return True


class ImageViewerDialog(QDialog):
    """大图查看：默认「适应宽度」，滚轮缩放、按住左键拖动平移。

    长截图（整表截图）用「适应窗口」会糊成一团，所以默认按宽度铺满；
    想看细节滚轮放到 100% 即可，缩放会以鼠标位置为锚点，不会跳来跳去。
    """

    _MIN_ZOOM = 0.05
    _MAX_ZOOM = 4.0

    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"查看大图 - {Path(image_path).name}")
        self.resize(1040, 760)
        self._path = image_path
        self._original = QPixmap(image_path)
        self._zoom = 1.0
        self._fitted = "width"        # width / window / manual
        self._drag_origin = None

        layout = QVBoxLayout(self)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setCursor(Qt.CursorShape.OpenHandCursor)
        self._label.setPixmap(self._original)
        # 只读查看：不让图/文字被框选，也不给右键菜单（否则会出现「复制」入口）
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._scroll.setWidget(self._label)
        layout.addWidget(self._scroll, 1)
        # 滚轮/拖拽要拦在滚动区上，否则事件会被 QScrollArea 自己吃掉（变成普通滚动）
        for target in (self._scroll.viewport(), self._label):
            target.installEventFilter(self)

        bar = QHBoxLayout()
        btn_width = QPushButton("适应宽度")
        btn_width.clicked.connect(self.fit_width)
        btn_window = QPushButton("适应窗口")
        btn_window.clicked.connect(self.fit_window)
        btn_actual = QPushButton("实际大小")
        btn_actual.clicked.connect(self.actual_size)
        btn_out = QPushButton("－")
        btn_out.setFixedWidth(36)
        btn_out.clicked.connect(lambda: self.zoom_by(1 / 1.25))
        btn_in = QPushButton("＋")
        btn_in.setFixedWidth(36)
        btn_in.clicked.connect(lambda: self.zoom_by(1.25))
        self._zoom_label = QLabel()
        self._zoom_label.setObjectName("tip")
        self._zoom_label.setFixedWidth(52)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        btn_save = QPushButton("另存为…")
        btn_save.clicked.connect(self._save_as)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        for widget in (btn_width, btn_window, btn_actual, btn_out, self._zoom_label,
                       btn_in):
            bar.addWidget(widget)
        bar.addStretch()
        hint = QLabel("滚轮缩放 · 按住左键拖动平移")
        hint.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        bar.addWidget(hint)
        bar.addStretch()
        bar.addWidget(btn_save)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

        self.fit_width()

    # ---------- 缩放 ----------
    def fit_width(self) -> None:
        """适应宽度：宽度铺满视口，是长截图最实用的默认视图"""
        self._fitted = "width"
        vw = self._scroll.viewport().width()
        self._set_zoom(min(1.0, vw / max(1, self._original.width())))

    def fit_window(self) -> None:
        """适应窗口：整张图都塞进视口（图很长时字会很小）"""
        self._fitted = "window"
        area = self._scroll.viewport().size()
        zoom = min(
            1.0,
            area.width() / max(1, self._original.width()),
            area.height() / max(1, self._original.height()),
        )
        self._set_zoom(zoom)

    def actual_size(self) -> None:
        self._fitted = "manual"
        self._set_zoom(1.0)

    def zoom_by(self, factor: float) -> None:
        self._fitted = "manual"
        self._set_zoom(self._zoom * factor)

    def _set_zoom(self, zoom: float, anchor: QPoint | None = None) -> None:
        """设置缩放并尽量保持锚点处的图像位置不动（滚轮缩放不跳）"""
        zoom = max(self._MIN_ZOOM, min(self._MAX_ZOOM, zoom))
        if abs(zoom - self._zoom) < 1e-4:
            return
        old_w = max(1, self._label.width())
        old_h = max(1, self._label.height())
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        if anchor is None:
            anchor = QPoint(self._scroll.viewport().width() // 2,
                            self._scroll.viewport().height() // 2)
        # 锚点在图像中的相对位置（0~1），缩放后让它仍落在同一个像素上
        rel_x = (hbar.value() + anchor.x()) / old_w
        rel_y = (vbar.value() + anchor.y()) / old_h

        self._zoom = zoom
        self._apply_zoom()

        new_w = max(1, self._label.width())
        new_h = max(1, self._label.height())
        hbar.setValue(round(rel_x * new_w - anchor.x()))
        vbar.setValue(round(rel_y * new_h - anchor.y()))

    def _apply_zoom(self) -> None:
        if self._original.isNull():
            self._label.setText("图片无法读取")
            self._label.setFixedSize(self._scroll.viewport().size())
            self._zoom_label.setText("—")
            return
        width = max(1, round(self._original.width() * self._zoom))
        height = max(1, round(self._original.height() * self._zoom))
        pixmap = self._original.scaled(
            width, height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # 用实际得到的尺寸设置控件，避免 KeepAspectRatio 的 1px 取整误差让图与控件对不齐
        self._label.setPixmap(pixmap)
        self._label.setFixedSize(pixmap.size())
        self._zoom_label.setText(f"{round(self._zoom * 100)}%")

    # ---------- 事件 ----------
    def resizeEvent(self, event) -> None:
        """窗口尺寸变化后重算缩放。

        必须延到下一轮事件循环：resizeEvent 触发时子控件布局还没更新，
        此刻读 viewport().width() 拿到的是**旧尺寸**，算出来的缩放会差一大截。
        """
        super().resizeEvent(event)
        if self._fitted in ("width", "window"):
            QTimer.singleShot(0, self._refit)
        else:
            QTimer.singleShot(0, self._apply_zoom)

    def _refit(self) -> None:
        if self._fitted == "width":
            self.fit_width()
        elif self._fitted == "window":
            self.fit_window()

    def eventFilter(self, obj, event) -> bool:
        """滚轮缩放 + 左键拖动平移（拦在滚动区之前，避免被当成普通滚动）"""
        kind = event.type()
        if _is_copy_key(event):
            # 复制这张截图本身（不是文件名），可直接粘到聊天/千牛
            if _copy_image_to_clipboard(self._path):
                QToolTip.showText(QCursor.pos(), "图片已复制，可直接粘贴")
            return True
        if kind == QEvent.Type.Wheel and not self._original.isNull():
            delta = event.angleDelta().y()
            if delta:
                pos = self._scroll.viewport().mapFrom(obj, event.position().toPoint())
                self._fitted = "manual"
                self._set_zoom(self._zoom * (1.25 ** (delta / 120.0)), anchor=pos)
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = (
                event.position().toPoint(),
                self._scroll.horizontalScrollBar().value(),
                self._scroll.verticalScrollBar().value(),
            )
            self._label.setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        if kind == QEvent.Type.MouseMove and self._drag_origin is not None:
            start, h0, v0 = self._drag_origin
            now = event.position().toPoint()
            self._scroll.horizontalScrollBar().setValue(h0 - (now.x() - start.x()))
            self._scroll.verticalScrollBar().setValue(v0 - (now.y() - start.y()))
            return True
        if kind == QEvent.Type.MouseButtonRelease and self._drag_origin is not None:
            self._drag_origin = None
            self._label.setCursor(Qt.CursorShape.OpenHandCursor)
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        """焦点落在对话框自身时也要响应复制键"""
        if _is_copy_key(event):
            if _copy_image_to_clipboard(self._path):
                QToolTip.showText(QCursor.pos(), "图片已复制，可直接粘贴")
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        """大图上右键：复制 / 另存为（与列表右键同一套取用方式）"""
        menu = QMenu(self)
        act_copy = menu.addAction("复制")
        act_save = menu.addAction("另存为…")
        chosen = menu.exec(event.globalPos())
        if chosen == act_copy:
            if _copy_image_to_clipboard(self._path):
                QToolTip.showText(QCursor.pos(), "图片已复制，可直接粘贴")
        elif chosen == act_save:
            self._save_as()

    def _save_as(self, *_args) -> None:
        save_path, _ = QFileDialog.getSaveFileName(
            self, "另存为", Path(self._path).name, "PNG 图片 (*.png);;所有文件 (*.*)"
        )
        if not save_path:
            return
        if _copy_file(self._path, save_path):
            QMessageBox.information(self, "成功", f"已保存到：\n{save_path}")
        else:
            QMessageBox.warning(self, "失败", "保存失败，请检查目标位置是否可写")


class ScreenshotHistoryDialog(QDialog):
    """历史截图：左边列表（封面缩略图 + 店铺 + 时间），右边大图预览。"""

    def __init__(self, parent=None, directory=None):
        super().__init__(parent)
        self.setWindowTitle("截图历史")
        self.resize(1120, 720)
        self._dir = Path(directory) if directory else None
        self._items: list = []

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self._hint = QLabel()
        self._hint.setObjectName("tip")
        # QLabel 只要「有文字」就自带一个标准右键菜单（含「复制」），
        # 截图历史是只读归档，凡是会显示文字/图片的标签一律禁掉右键菜单
        self._hint.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        top.addWidget(self._hint)
        top.addStretch()
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.refresh)
        btn_folder = QPushButton("打开文件夹")
        btn_folder.clicked.connect(self._open_folder)
        btn_clear = QPushButton("清空历史")
        btn_clear.setObjectName("dangerBtn")
        btn_clear.setToolTip("删除全部历史截图（会二次确认，确认后不可恢复）")
        btn_clear.clicked.connect(self._clear_all)
        top.addWidget(btn_refresh)
        top.addWidget(btn_folder)
        top.addWidget(btn_clear)
        layout.addLayout(top)

        body = QHBoxLayout()
        self._list = QListWidget()
        self._list.setIconSize(QSize(_THUMB_W, _THUMB_H))
        self._list.setUniformItemSizes(True)
        self._list.setSpacing(2)
        self._list.setMinimumWidth(360)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_menu)
        self._list.currentItemChanged.connect(lambda *_: self._update_preview())
        self._list.itemDoubleClicked.connect(lambda *_: self._view_large())
        # 只读归档：条目不可改写、不可拖拽（拖出去会等于复制文件），
        # 并拦住 Ctrl+C —— 实测 QListWidget 会把条目文字写进剪贴板
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.setDragEnabled(False)
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self._list.setDefaultDropAction(Qt.DropAction.IgnoreAction)
        self._list.installEventFilter(self)
        body.addWidget(self._list, 2)

        right = QVBoxLayout()
        # 预览按宽度铺满、竖向滚动：长截图这样才看得清，而不是被压成一条小图
        self._preview_scroll = QScrollArea()
        self._preview_scroll.setWidgetResizable(False)
        self._preview_scroll.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )
        self._preview = QLabel("选择左侧截图查看")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet("background:#F7F8FA; color:#909399;")
        self._preview.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._preview.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        # 预览区右键交给对话框统一处理（复制 / 删除 / 另存为），
        # 滚动区视口才是真正承接事件的地方，事件要装在它上面
        self._preview_scroll.viewport().setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._preview_scroll.viewport().customContextMenuRequested.connect(
            self._show_preview_menu)
        self._preview_scroll.setWidget(self._preview)
        self._preview_scroll.setMinimumWidth(380)
        right.addWidget(self._preview_scroll, 1)
        self._meta = QLabel()
        self._meta.setObjectName("tip")
        self._meta.setWordWrap(True)
        self._meta.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._meta.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        right.addWidget(self._meta)
        row = QHBoxLayout()
        btn_view = QPushButton("查看大图")
        btn_view.setToolTip("双击图片也可放大；大图窗口支持滚轮缩放")
        btn_view.clicked.connect(self._view_large)
        btn_save = QPushButton("另存为…")
        btn_save.clicked.connect(self._save_as)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_view)
        row.addWidget(btn_save)
        row.addStretch()
        row.addWidget(btn_close)
        right.addLayout(row)
        body.addLayout(right, 3)
        layout.addLayout(body, 1)

        self.refresh()

    # ---------- 数据 ----------
    def refresh(self) -> None:
        """重新扫描历史目录（用户在外部新增/删除截图后点“刷新”即可同步）"""
        self._items = ScreenshotStore.list_all(self._dir)
        keep = self._current_path()
        self._list.clear()
        for item in self._items:
            row = QListWidgetItem()
            # 封面图：按宽度缩放后取顶部，长截图不会缩成一条细缝
            thumb = cover_pixmap(item["path"], _THUMB_W, _THUMB_H)
            if thumb is not None:
                row.setIcon(QIcon(thumb))
            else:
                row.setText("（无法预览）")
            row.setText(f"{item['name']}\n{item['shop']} · {item['time_text']} · {item['size_text']}")
            row.setData(Qt.ItemDataRole.UserRole, item["path"])
            row.setToolTip(item["path"])
            self._list.addItem(row)

        self._hint.setText(f"共 {len(self._items)} 张截图（{self._dir or ''}）")
        if not self._items:
            self._show_placeholder("还没有历史截图\n点「导出 → 导出表单图片」生成一张")
            self._meta.setText("")
            return
        # 尽量保持原来选中的那张，避免刷新后跳回第一张
        target = 0
        for i, item in enumerate(self._items):
            if item["path"] == keep:
                target = i
                break
        self._list.setCurrentRow(target)

    def _show_placeholder(self, text: str) -> None:
        """空状态：预览区铺满视口并居中提示文字"""
        self._preview.setPixmap(QPixmap())
        self._preview.setText(text)
        self._preview.setFixedSize(self._preview_scroll.viewport().size())

    def _current_path(self) -> str:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def _update_preview(self) -> None:
        path = self._current_path()
        if not path:
            return
        # 取整到步长（就近取整，不会比视口宽出滚动条），拖动窗口时不会每 1px 就重新解码一次
        viewport_w = self._preview_scroll.viewport().width()
        target_w = max(200, round(viewport_w / _WIDTH_STEP) * _WIDTH_STEP)
        pixmap = fit_width_pixmap(path, target_w)
        if pixmap is None:
            self._show_placeholder("图片无法读取（文件可能已被移动）")
            return
        self._preview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._preview.setPixmap(pixmap)
        self._preview.setFixedSize(pixmap.size())
        for item in self._items:
            if item["path"] == path:
                self._meta.setText(
                    f"{item['name']}　{item['shop']} · {item['time_text']} · {item['size_text']}"
                    f"　（点「查看大图」可滚轮缩放看细节）"
                )
                break

    def resizeEvent(self, event) -> None:
        """同样要延后：此刻滚动区视口还没拿到新尺寸，立刻重算会用到旧宽度"""
        super().resizeEvent(event)
        QTimer.singleShot(0, self._rescale_preview)

    def _rescale_preview(self) -> None:
        if self._items:
            self._update_preview()
        else:
            self._show_placeholder(self._preview.text())

    # ---------- 操作 ----------
    def _shot_menu(self, menu: QMenu) -> dict:
        """给菜单挂上「复制 / 删除」两个取用入口，返回 {动作: 标识}（列表与预览区共用）"""
        act_copy = menu.addAction("复制")
        act_delete = menu.addAction("删除")
        return {"copy": act_copy, "delete": act_delete}

    def _run_menu_action(self, chosen, actions: dict) -> bool:
        """执行「复制 / 删除」；chosen 不是这两项时返回 False 交给调用方继续判断"""
        if chosen is actions.get("copy"):
            if _copy_image_to_clipboard(self._current_path()):
                QToolTip.showText(QCursor.pos(), "图片已复制，可直接粘贴")
            return True
        if chosen is actions.get("delete"):
            self._delete_current()
            return True
        return False

    def _show_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        self._list.setCurrentItem(item)
        menu = QMenu(self)
        act_view = menu.addAction("查看大图")
        actions = self._shot_menu(menu)
        menu.addSeparator()
        act_save = menu.addAction("另存为…")
        act_folder = menu.addAction("在文件夹中显示")
        menu.addSeparator()
        act_refresh = menu.addAction("刷新列表")
        chosen = menu.exec(self._list.mapToGlobal(pos))
        if self._run_menu_action(chosen, actions):
            return
        if chosen is act_view:
            self._view_large()
        elif chosen is act_save:
            self._save_as()
        elif chosen is act_folder:
            _reveal_in_folder(self._current_path())
        elif chosen is act_refresh:
            self.refresh()

    def _show_preview_menu(self, pos) -> None:
        """预览区右键（用户多半就在这儿看大图，右键取用最顺手）"""
        if not self._current_path():
            return
        menu = QMenu(self)
        act_view = menu.addAction("查看大图")
        actions = self._shot_menu(menu)
        menu.addSeparator()
        act_save = menu.addAction("另存为…")
        chosen = menu.exec(self._preview_scroll.viewport().mapToGlobal(pos))
        if self._run_menu_action(chosen, actions):
            return
        if chosen is act_view:
            self._view_large()
        elif chosen is act_save:
            self._save_as()

    def _delete_current(self) -> None:
        """删除当前选中的那一张（确认后**真删除**，不进回收站）"""
        path = self._current_path()
        if not path:
            QMessageBox.information(self, "提示", "请先选择一张截图")
            return
        name = Path(path).name
        answer = QMessageBox.question(
            self, "删除截图",
            f"确定要删除这张截图吗？删除后无法恢复。\n\n{name}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok, error = ScreenshotStore.remove(path, self._dir)
        if not ok:
            logger.warning("删除截图失败 %s: %s", path, error)
            QMessageBox.warning(self, "删除失败", f"删除失败：{error}")
            return
        self.refresh()
        QToolTip.showText(QCursor.pos(), f"已删除 {name}", self, msecShowTime=1500)

    def _view_large(self, *_args) -> None:
        path = self._current_path()
        if not path:
            QMessageBox.information(self, "提示", "请先选择一张截图")
            return
        ImageViewerDialog(path, self).exec()

    def _save_as(self, *_args) -> None:
        path = self._current_path()
        if not path:
            QMessageBox.information(self, "提示", "请先选择一张截图")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self, "另存为", Path(path).name, "PNG 图片 (*.png);;所有文件 (*.*)"
        )
        if not save_path:
            return
        if _copy_file(path, save_path):
            QMessageBox.information(self, "成功", f"已保存到：\n{save_path}")
        else:
            QMessageBox.warning(self, "失败", "保存失败，请检查目标位置是否可写")

    def _open_folder(self) -> None:
        folder = Path(self._dir) if self._dir else Path(ScreenshotStore.ensure_dir())
        ScreenshotStore.ensure_dir(folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---------- 复制：Ctrl+C 取的是图片本身 ----------
    def eventFilter(self, obj, event) -> bool:
        """列表上的 Ctrl+C 改成复制**图片**（Qt 默认行为只是把条目文字写进剪贴板）"""
        if obj is self._list and _is_copy_key(event):
            if _copy_image_to_clipboard(self._current_path()):
                QToolTip.showText(QCursor.pos(), "图片已复制，可直接粘贴")
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        """焦点不在列表上（例如落在按钮上）时同样响应复制键"""
        if _is_copy_key(event):
            if _copy_image_to_clipboard(self._current_path()):
                QToolTip.showText(QCursor.pos(), "图片已复制，可直接粘贴")
            return
        super().keyPressEvent(event)

    # ---------- 清空历史（唯一的破坏性操作） ----------
    def _clear_all(self) -> None:
        """清空历史截图：二次确认后**真删除**归档文件（不进回收站，不可恢复）。"""
        total = len(self._items)
        if total == 0:
            QMessageBox.information(self, "提示", "当前没有历史截图可清空")
            return
        folder = Path(self._dir) if self._dir else Path(ScreenshotStore.ensure_dir())
        answer = QMessageBox.warning(
            self,
            "清空历史截图",
            f"将永久删除 {total} 张历史截图，删除后无法恢复。\n\n"
            f"存档目录：\n{folder}\n\n确定要清空吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        removed, failed = ScreenshotStore.clear_all(self._dir)
        self.refresh()
        if failed:
            logger.warning("清空截图历史：%d 张删除失败 %s", len(failed), failed)
            QMessageBox.warning(
                self, "部分失败",
                f"已删除 {removed} 张，以下 {len(failed)} 张删除失败"
                f"（可能正被其它程序占用）：\n" + "\n".join(failed[:10]),
            )
        elif removed:
            QMessageBox.information(self, "已清空", f"已删除 {removed} 张历史截图")


def _copy_file(src: str, dst: str) -> bool:
    """复制文件（不移动、不删除原档）；目标已存在时覆盖"""
    try:
        data = Path(src).read_bytes()
        Path(dst).write_bytes(data)
        return True
    except OSError:
        return False


def _reveal_in_folder(path: str) -> None:
    """在系统文件管理器中定位该文件（Windows 用 explorer /select）"""
    if not path or not os.path.exists(path):
        QMessageBox.information(None, "提示", "文件不存在")
        return
    if sys.platform.startswith("win"):
        try:
            # explorer 的 /select 参数要把路径单独作为一个参数传入
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return
        except OSError:
            pass
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
