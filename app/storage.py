"""数据层：店铺素材数据的加载、保存与增删改查。

仅依赖标准库，不依赖任何界面组件，方便后续替换存储方式（如 SQLite）。
"""
import json
from pathlib import Path


class DataLoadError(Exception):
    """数据加载失败异常"""


class ShopRepository:
    """店铺素材仓库：管理内存数据与 data.json 文件持久化"""

    def __init__(self, data_file: Path, images_dir: Path):
        self.data_file = Path(data_file)
        self.images_dir = Path(images_dir)
        self.shops: dict[str, list[dict]] = {}   # {店铺名: [素材记录]}
        self.image_counter = 0                   # 图片文件名计数器

    # ---------- 持久化 ----------
    def load(self) -> None:
        """从 data.json 加载数据；文件不存在时使用空数据，损坏时抛 DataLoadError"""
        self.images_dir.mkdir(parents=True, exist_ok=True)
        if not self.data_file.exists():
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.shops = data.get("shops", {})
            self.image_counter = int(data.get("image_counter", 0))
            self._migrate()
        except Exception as e:
            raise DataLoadError(f"数据加载失败：{e}") from e

    def _migrate(self) -> None:
        """旧数据迁移：单图字段 image_path(str) -> image_paths(list)"""
        for records in self.shops.values():
            for record in records:
                if "image_paths" in record:
                    continue
                old_path = record.pop("image_path", "") or ""
                record["image_paths"] = [old_path] if old_path else []

    def save(self) -> None:
        """将当前数据写入 data.json"""
        data = {"shops": self.shops, "image_counter": self.image_counter}
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- 店铺 ----------
    def shop_exists(self, name: str) -> bool:
        return name in self.shops

    def add_shop(self, name: str) -> None:
        self.shops[name] = []

    def rename_shop(self, old_name: str, new_name: str) -> None:
        self.shops[new_name] = self.shops.pop(old_name)

    def delete_shop(self, name: str) -> None:
        del self.shops[name]

    # ---------- 素材记录 ----------
    def ensure_shop(self, name: str) -> None:
        """确保店铺存在（记录所属店铺不存在时兜底创建）"""
        if name not in self.shops:
            self.shops[name] = []

    def get_records(self, shop_name: str) -> list:
        """获取指定店铺的记录列表；店铺不存在时返回空列表"""
        return self.shops.get(shop_name, [])

    def add_record(self, shop_name: str, record: dict) -> None:
        self.ensure_shop(shop_name)
        self.shops[shop_name].append(record)

    def insert_record(self, shop_name: str, after_index: int, record: dict) -> int:
        """把记录插入到 after_index 之后（复制记录用），返回插入位置；越界时追加到末尾"""
        self.ensure_shop(shop_name)
        records = self.shops[shop_name]
        pos = max(0, min(after_index + 1, len(records)))
        records.insert(pos, record)
        return pos

    def remove_record(self, shop_name: str, index: int) -> None:
        self.shops[shop_name].pop(index)

    def set_record_field(self, shop_name: str, index: int, field_name: str, value) -> None:
        """更新某条记录的单个字段"""
        self.shops[shop_name][index][field_name] = value

    def append_record_image(self, shop_name: str, index: int, filepath: str) -> None:
        """向某条记录的评价图片列表追加一张图片"""
        record = self.shops[shop_name][index]
        record.setdefault("image_paths", [])
        record["image_paths"].append(filepath)

    def remove_record_image(self, shop_name: str, index: int, img_index: int) -> None:
        """移除某条记录评价图片列表中指定序号的一张（仅移除引用，不删除磁盘文件）"""
        record = self.shops[shop_name][index]
        images = record.setdefault("image_paths", [])
        if 0 <= img_index < len(images):
            images.pop(img_index)
