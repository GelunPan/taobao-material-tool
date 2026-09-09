"""流式布局：子控件从左到右排列，宽度不够时自动换行，每行整体水平居中。

QTableWidget 的多图单元格用它承载缩略图：两张图并排、更多图自动换行，
并通过 heightForWidth 让表格行高随列宽与图片数量自适应。

改编自 Qt 官方 FlowLayout 示例（PyQt6），增加了“每行水平居中”。
"""
from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, hspace=6, vspace=6):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self._hspace = hspace
        self._vspace = vspace
        self._items: list = []

    def __del__(self):
        while self.count():
            self.takeAt(0)

    # ---------- QLayout 必须实现的接口 ----------
    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        # 只占用需要的空间，不主动拉伸
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    @staticmethod
    def _item_w(item) -> int:
        widget = item.widget()
        min_w = widget.minimumSize().width() if widget is not None else item.minimumSize().width()
        return max(item.sizeHint().width(), min_w)

    def _item_hint(self, item) -> QSize:
        widget = item.widget()
        if widget is not None:
            return item.sizeHint().expandedTo(widget.minimumSize())
        return item.sizeHint().expandedTo(item.minimumSize())

    def natural_width(self) -> int:
        """所有子控件排成一行时需要的宽度（含间距与边距）"""
        if not self._items:
            m = self.contentsMargins()
            return m.left() + m.right()
        widths = sum(self._item_w(it) for it in self._items)
        gaps = self._hspace * (len(self._items) - 1)
        m = self.contentsMargins()
        return widths + gaps + m.left() + m.right()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(self._item_hint(item))
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    # ---------- 排版核心 ----------
    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        """排列全部子控件，返回排版所需总高度（含上下边距）"""
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())

        x = effective.x()
        y = effective.y()
        line_items: list = []   # 当前行 [(item, width), ...]
        line_width = 0          # 当前行累计宽度（含控件间距）
        line_height = 0

        def place_line(items, top, height, total_w):
            # 整行水平居中：剩余空间均分到左侧
            left = effective.x() + max(0, (effective.width() - total_w + self._hspace) // 2)
            for item, w in items:
                if not test_only:
                    item.setGeometry(QRect(left, top, w, height))
                left += w + self._hspace

        for item in self._items:
            hint = self._item_hint(item)
            if x + hint.width() > effective.right() + 1 and line_items:
                # 当前行放不下，先落位上一行再换行
                place_line(line_items, y, line_height, line_width)
                x = effective.x()
                y += line_height + self._vspace
                line_items = []
                line_width = 0
                line_height = 0
            line_items.append((item, hint.width()))
            line_width += hint.width() + self._hspace
            line_height = max(line_height, hint.height())
            x += hint.width() + self._hspace

        if line_items:
            place_line(line_items, y, line_height, line_width)
            y += line_height

        return y - rect.y() + m.bottom()
