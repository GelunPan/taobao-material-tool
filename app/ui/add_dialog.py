"""新增/修改素材记录弹窗：文本字段 + 图片选择（单图缩略图选择器、多图添加器）。

record 为 None 时是新增模式；传入已有记录时预填字段，作为修改模式。
图片区域一律显示缩略图，不显示本地文件名。
布局：文本字段与图片分组上下排列，两列标签固定同宽，整体对齐规整。
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..config import DIALOG_MULTI_SIZE, DIALOG_THUMB_SIZE
from .image_utils import scaled_pixmap

IMAGE_FILTER = "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)"

_DASHED = "color:#A8ABB2; border:1px dashed #C0C4CC; border-radius:6px; background:#FAFBFC;"
_SOLID = "border:1px solid #DCDFE6; border-radius:6px;"

# 两列布局中标签列的固定宽度（文本区与图片区标签严格对齐）
_LABEL_WIDTH = 76


def _form_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setFixedWidth(_LABEL_WIDTH)
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return label


class SingleImagePicker(QWidget):
    """单张图片选择器：缩略图 + 选择/移除按钮，不显示文件名"""

    def __init__(self, size: int = DIALOG_THUMB_SIZE, parent=None):
        super().__init__(parent)
        self._size = size
        self._path = ""

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.thumb = QLabel("未选择")
        self.thumb.setFixedSize(size, size)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(_DASHED)
        lay.addWidget(self.thumb)

        col = QVBoxLayout()
        col.setSpacing(6)
        btn_select = QPushButton("选择图片")
        btn_select.setFixedWidth(84)
        btn_clear = QPushButton("移除")
        btn_clear.setFixedWidth(84)
        btn_select.clicked.connect(self.choose)
        btn_clear.clicked.connect(self.clear)
        col.addWidget(btn_select)
        col.addWidget(btn_clear)
        col.addStretch()
        lay.addLayout(col)
        lay.addStretch()

    def choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择图片", "", IMAGE_FILTER)
        if path:
            self.set_path(path)

    def set_path(self, path: str) -> None:
        self._path = path or ""
        if self._path:
            pixmap = scaled_pixmap(self._path, self._size)
            if pixmap is not None:
                self.thumb.setPixmap(pixmap)
                self.thumb.setStyleSheet(_SOLID)
                return
        self.clear()

    def clear(self) -> None:
        self._path = ""
        self.thumb.clear()
        self.thumb.setText("未选择")
        self.thumb.setStyleSheet(_DASHED)

    @property
    def path(self) -> str:
        return self._path


class MultiImagePicker(QWidget):
    """多张图片添加器：缩略图网格，每张可单独删除，支持一次多选添加"""

    def __init__(self, size: int = DIALOG_MULTI_SIZE, columns: int = 5, parent=None):
        super().__init__(parent)
        self._size = size
        self._columns = columns
        self._paths: list[str] = []

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        outer.addWidget(self.grid_host, 1)

        col = QVBoxLayout()
        col.setSpacing(6)
        self.btn_add = QPushButton("+ 添加图片")
        self.btn_add.setFixedWidth(84)
        self.btn_add.clicked.connect(self.choose_more)
        col.addWidget(self.btn_add)
        col.addStretch()
        outer.addLayout(col)

        self._rebuild()

    def choose_more(self) -> None:
        """打开多选对话框追加图片（自动去重）"""
        paths, _ = QFileDialog.getOpenFileNames(self, "添加图片（可按住 Ctrl 多选）", "", IMAGE_FILTER)
        if not paths:
            return
        existed = set(self._paths)
        for p in paths:
            if p not in existed:
                self._paths.append(p)
                existed.add(p)
        self._rebuild()

    def set_paths(self, paths: list) -> None:
        self._paths = [p for p in (paths or []) if p]
        self._rebuild()

    def _rebuild(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not self._paths:
            empty = QLabel("点击右侧「+ 添加图片」选择")
            empty.setStyleSheet("color:#A8ABB2;")
            self.grid.addWidget(empty, 0, 0)
            return
        for i, path in enumerate(self._paths):
            self.grid.addWidget(self._make_thumb(i, path), i // self._columns, i % self._columns)

    def _make_thumb(self, index: int, path: str) -> QWidget:
        box = QWidget()
        g = QGridLayout(box)
        g.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel()
        lbl.setFixedSize(self._size, self._size)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = scaled_pixmap(path, self._size)
        if pixmap is not None:
            lbl.setPixmap(pixmap)
            lbl.setStyleSheet(_SOLID)
        else:
            lbl.setText("无效")
            lbl.setStyleSheet(_DASHED)
        g.addWidget(lbl, 0, 0)

        btn_del = QPushButton("×")
        btn_del.setFixedSize(20, 20)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setStyleSheet(
            "QPushButton{background:#F56C6C;color:white;border:none;border-radius:10px;font-weight:bold;}"
            "QPushButton:hover{background:#f78989;}"
        )
        btn_del.clicked.connect(lambda _, i=index: self._remove(i))
        g.addWidget(btn_del, 0, 0, alignment=Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        return box

    def _remove(self, index: int) -> None:
        self._paths.pop(index)
        self._rebuild()

    @property
    def paths(self) -> list:
        return list(self._paths)


class AddRecordDialog(QDialog):
    """新增/修改素材记录弹窗"""

    def __init__(self, parent=None, record: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("修改素材记录" if record else "新增素材记录")
        self.setMinimumWidth(680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)

        # ---------- 文本字段 ----------
        form_layout = QFormLayout()
        form_layout.setHorizontalSpacing(12)
        form_layout.setVerticalSpacing(10)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.product_id_input = QLineEdit()
        self.spec_input = QLineEdit()
        self.title_input = QLineEdit()
        self.helper_input = QLineEdit()
        self.product_url_input = QLineEdit()
        self.product_url_input.setPlaceholderText("https://item.taobao.com/...")
        self.review_input = QTextEdit()
        self.review_input.setMaximumHeight(96)

        form_layout.addRow(_form_label("商品ID:"), self.product_id_input)
        form_layout.addRow(_form_label("规格:"), self.spec_input)
        form_layout.addRow(_form_label("标题:"), self.title_input)
        form_layout.addRow(_form_label("补手:"), self.helper_input)
        form_layout.addRow(_form_label("买家秀评价:"), self.review_input)
        form_layout.addRow(_form_label("商品链接:"), self.product_url_input)
        layout.addLayout(form_layout)

        # ---------- 图片选择（浅色分组容器，标签列与上方文本区同宽对齐） ----------
        img_group = QWidget()
        img_group.setObjectName("imgGroup")
        img_layout = QFormLayout(img_group)
        img_layout.setContentsMargins(10, 8, 10, 10)
        img_layout.setHorizontalSpacing(12)
        img_layout.setVerticalSpacing(10)
        img_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        group_title = QLabel("商品图片")
        group_title.setObjectName("imgGroupTitle")
        img_layout.addRow(group_title)

        self.spec_picker = SingleImagePicker()
        self.link_picker = SingleImagePicker()
        self.review_picker = MultiImagePicker()
        img_layout.addRow(_form_label("规格图:"), self.spec_picker)
        img_layout.addRow(_form_label("链接主图:"), self.link_picker)
        img_layout.addRow(_form_label("评价图片:"), self.review_picker)
        layout.addWidget(img_group)

        layout.addStretch()

        # ---------- 底部按钮（右对齐，保存为主按钮） ----------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if record:
            self._load_record(record)

    def _load_record(self, record: dict) -> None:
        """预填已有记录的内容（修改模式）"""
        self.product_id_input.setText(record.get("product_id", ""))
        self.spec_input.setText(record.get("spec", ""))
        self.title_input.setText(record.get("title", ""))
        self.helper_input.setText(record.get("helper", ""))
        self.review_input.setPlainText(record.get("review", ""))
        self.product_url_input.setText(record.get("product_url", ""))

        self.spec_picker.set_path(record.get("spec_image", ""))
        self.link_picker.set_path(record.get("link_image", ""))
        # 兼容旧版单图字段 image_path
        paths = record.get("image_paths")
        if paths is None:
            old_path = record.get("image_path", "")
            paths = [old_path] if old_path else []
        self.review_picker.set_paths(paths)

    def get_data(self) -> dict:
        """获取输入的记录数据（字段名与 RECORD_FIELDS 对齐）"""
        return {
            "product_id": self.product_id_input.text().strip(),
            "spec_image": self.spec_picker.path,
            "spec": self.spec_input.text().strip(),
            "title": self.title_input.text().strip(),
            "link_image": self.link_picker.path,
            "helper": self.helper_input.text().strip(),
            "review": self.review_input.toPlainText().strip(),
            "image_paths": self.review_picker.paths,
            "product_url": self.product_url_input.text().strip(),
        }
