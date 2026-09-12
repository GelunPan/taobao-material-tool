"""撤销栈：数据快照式的「上一步」回退。

为什么用**整份数据快照**而不是记录反向操作：
素材表的数据量很小（几十条记录、图片只存路径），deepcopy 一份 shops 的成本可以忽略；
而给每一种操作（增删记录、改字段、加删图片、批量导入、拖拽排序…）各写一个反向操作，
既容易漏、又很容易两边写歪。快照式只要一处入口，任何操作都自动可撤销。

只支持**撤销**（Ctrl+Z），不做重做：办公场景里误删误改的回退是刚需，
而重做会增加一层状态机，收益不大、风险不小。

与界面解耦：这里只认 dict，不认控件；MainWindow 负责在变更前后调用并入栈。
"""
from copy import deepcopy


class UndoStack:
    """固定容量的撤销栈，存放 (标签, 数据快照)。"""

    def __init__(self, limit: int = 30):
        self._limit = max(1, int(limit))
        self._items: list[tuple[str, dict]] = []

    def push(self, label: str, snapshot: dict) -> None:
        """入栈一份变更前的快照；超出容量时丢弃最早的一份"""
        self._items.append((label, snapshot))
        if len(self._items) > self._limit:
            self._items.pop(0)

    def pop(self) -> tuple[str, dict] | None:
        """弹出最近一份快照；栈空返回 None"""
        if not self._items:
            return None
        return self._items.pop()

    def peek(self) -> str:
        """最近一次可撤销操作的标签（用于提示「撤销：删除 3 条记录」）；栈空返回空串"""
        return self._items[-1][0] if self._items else ""

    def can_undo(self) -> bool:
        return bool(self._items)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


def snapshot(data: dict) -> dict:
    """拷贝一份可安全长期持有的数据快照"""
    return deepcopy(data)
