"""数据层：店铺素材数据的加载、保存与增删改查。

仅依赖标准库，不依赖任何界面组件，方便后续替换存储方式（如 SQLite）。
"""
import json
from pathlib import Path

# 每个店铺的默认产品分类：新建店铺 / 旧数据迁移都落到这里
DEFAULT_CATEGORY = "分类一"


class DataLoadError(Exception):
    """数据加载失败异常"""


class ShopRepository:
    """店铺素材仓库：管理内存数据与 data.json 文件持久化

    数据结构（三层）：店铺 -> 产品分类 -> 记录列表
        shops = {"大帅": {"分类一": [记录, ...], "分类二": [...]}}
    分类是**有序**的（dict 插入顺序），界面上按这个顺序展示。
    """

    def __init__(self, data_file: Path, images_dir: Path):
        self.data_file = Path(data_file)
        self.images_dir = Path(images_dir)
        # {店铺名: {分类名: [素材记录]}}
        self.shops: dict[str, dict[str, list[dict]]] = {}
        self.image_categories: list = []
        self.image_category_map: dict = {}
        self.image_counter = 0                   # 图片文件名计数器
        # {店铺名: {"bg": "#RRGGBB"|None, "text": "#RRGGBB"|None}}：
        # 店铺名的填充色/字体色。导出表单图片的标题带会用它，必须持久化，
        # 否则重启后颜色丢失、导出图与界面就不再一致
        self.shop_colors: dict[str, dict] = {}

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
            self.image_categories = data.get("image_categories", [])
            self.image_category_map = data.get("image_category_map", {})
            self.image_counter = int(data.get("image_counter", 0))
            self.shop_colors = data.get("shop_colors", {}) or {}
            self._migrate()
        except Exception as e:
            raise DataLoadError(f"数据加载失败：{e}") from e

    def _migrate(self) -> None:
        """旧数据迁移（幂等，重复执行无副作用）：

        1. 店铺值是 list（更早的格式）-> 归入默认分类「分类一」
        2. 店铺里某个分类的值为 list 且元素不是 dict（脏数据）-> 兜底清成空列表
        3. 单图字段 image_path(str) -> image_paths(list)
        """
        migrated: dict[str, dict[str, list]] = {}
        for shop, value in self.shops.items():
            if isinstance(value, list):
                # 旧格式：店铺直接就是记录列表 -> 全部归入默认分类
                migrated[shop] = {DEFAULT_CATEGORY: value}
            elif isinstance(value, dict):
                migrated[shop] = {
                    cat: (records if isinstance(records, list) else [])
                    for cat, records in value.items()
                }
            else:
                migrated[shop] = {DEFAULT_CATEGORY: []}
            # 空店铺也补一个默认分类：树上有东西可点，取记录列表时拿到的
            # 也永远是数据层里真实存在的那份（否则新增记录会 append 到临时列表里丢掉）
            if not migrated[shop]:
                migrated[shop] = {DEFAULT_CATEGORY: []}
        self.shops = migrated

        for records in (r for cats in self.shops.values() for r in cats.values()):
            for record in records:
                if isinstance(record, dict) and "image_paths" not in record:
                    old_path = record.pop("image_path", "") or ""
                    record["image_paths"] = [old_path] if old_path else []

    def save(self) -> None:
        """将当前数据写入 data.json"""
        data = {
            "shops": self.shops,
            "image_counter": self.image_counter,
            "shop_colors": self.shop_colors,
            "image_categories": getattr(self, "image_categories", []),
            "image_category_map": getattr(self, "image_category_map", {}),
        }
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- 店铺 ----------
    def shop_exists(self, name: str) -> bool:
        return name in self.shops

    def add_shop(self, name: str, category: str | None = None) -> None:
        """新增店铺，自带一个默认产品分类"""
        self.shops[name] = {category or DEFAULT_CATEGORY: []}

    def rename_shop(self, old_name: str, new_name: str) -> None:
        self.shops[new_name] = self.shops.pop(old_name)
        # 颜色是跟着店铺走的，改名后要一起迁移，否则新名字的导出图会丢掉配色
        if old_name in self.shop_colors:
            self.shop_colors[new_name] = self.shop_colors.pop(old_name)

    def delete_shop(self, name: str) -> None:
        del self.shops[name]
        self.shop_colors.pop(name, None)

    def reorder_shops(self, new_order: list[str]) -> None:
        """按新顺序重排店铺（拖拽排序用），保持 dict 插入顺序"""
        new_shops = {}
        for name in new_order:
            if name in self.shops:
                new_shops[name] = self.shops[name]
        for name, cats in self.shops.items():
            if name not in new_shops:
                new_shops[name] = cats
        self.shops = new_shops

    # ---------- 产品分类 ----------
    def ensure_shop(self, name: str, category: str | None = None) -> None:
        """确保店铺（及其默认分类）存在；记录所属店铺不存在时兜底创建"""
        if name not in self.shops:
            self.add_shop(name, category)
        elif not self.shops[name]:
            self.shops[name][category or DEFAULT_CATEGORY] = []

    def categories(self, shop_name: str) -> list[str]:
        """店铺下的产品分类（按展示顺序）"""
        return list(self.shops.get(shop_name, {}).keys())

    def first_category(self, shop_name: str) -> str:
        """店铺的第一个分类；没有店铺时返回默认分类名"""
        cats = self.categories(shop_name)
        return cats[0] if cats else DEFAULT_CATEGORY

    def category_exists(self, shop_name: str, category: str) -> bool:
        return category in self.shops.get(shop_name, {})

    def add_category(self, shop_name: str, category: str) -> None:
        self.ensure_shop(shop_name)
        self.shops[shop_name].setdefault(category, [])

    def rename_category(self, shop_name: str, old: str, new: str) -> None:
        cats = self.shops.get(shop_name)
        if not cats or old not in cats:
            return
        # 新名字已存在时把记录并过去，避免数据被顶掉
        if new in cats:
            cats[new].extend(cats.pop(old))
            self._rebuild_order(shop_name)
        else:
            # 原位改名：直接 cats[new] = cats.pop(old) 会把分类挪到末尾，顺序跳变
            self.shops[shop_name] = {
                new if key == old else key: value for key, value in cats.items()
            }

    def delete_category(self, shop_name: str, category: str) -> bool:
        """删除分类（连同里面的记录）。最后一个分类不允许删，返回是否删除成功"""
        cats = self.shops.get(shop_name)
        if not cats or category not in cats or len(cats) <= 1:
            return False
        cats.pop(category)
        self._rebuild_order(shop_name)
        return True

    def _rebuild_order(self, shop_name: str) -> None:
        """分类改名/删除后保持 dict 顺序稳定（避免某个分类被挪到末尾）"""
        cats = self.shops.get(shop_name)
        if cats:
            self.shops[shop_name] = dict(cats)

    # ---------- 素材记录 ----------
    def get_records(self, shop_name: str, category: str | None = None) -> list:
        """获取指定店铺+分类的记录列表；不存在时返回空列表（不主动建）"""
        category = category or self.first_category(shop_name)
        records = self.shops.get(shop_name, {}).get(category, [])
        return records if isinstance(records, list) else []

    def add_record(self, shop_name: str, category: str | None, record: dict) -> None:
        self.ensure_shop(shop_name, category)
        self.shops[shop_name].setdefault(category or self.first_category(shop_name), [])
        self.shops[shop_name][category or self.first_category(shop_name)].append(record)

    def insert_record(self, shop_name: str, category: str | None,
                      after_index: int, record: dict) -> int:
        """把记录插入到 after_index 之后（复制记录用），返回插入位置；越界时追加到末尾"""
        self.ensure_shop(shop_name, category)
        key = category or self.first_category(shop_name)
        records = self.shops[shop_name].setdefault(key, [])
        pos = max(0, min(after_index + 1, len(records)))
        records.insert(pos, record)
        return pos

    def remove_record(self, shop_name: str, category: str | None, index: int) -> None:
        records = self.get_records(shop_name, category)
        if 0 <= index < len(records):
            records.pop(index)

    def set_record_field(self, shop_name: str, category: str | None,
                         index: int, field_name: str, value) -> None:
        """更新某条记录的单个字段"""
        records = self.get_records(shop_name, category)
        if 0 <= index < len(records):
            records[index][field_name] = value

    def append_record_image(self, shop_name: str, category: str | None, index: int,
                            filepath: str, field_name: str = "image_paths") -> None:
        """向某条记录的指定多图字段追加一张图片（默认评价图片 image_paths）"""
        record = self.get_records(shop_name, category)[index]
        record.setdefault(field_name, [])
        # 兼容旧数据：如果该字段是字符串，转成列表
        if not isinstance(record[field_name], list):
            record[field_name] = [record[field_name]] if record[field_name] else []
        record[field_name].append(filepath)

    def remove_record_image(self, shop_name: str, category: str | None, index: int,
                            img_index: int, field_name: str = "image_paths") -> None:
        """移除某条记录指定多图字段中指定序号的一张（仅移除引用，不删除磁盘文件）"""
        record = self.get_records(shop_name, category)[index]
        images = record.setdefault(field_name, [])
        if not isinstance(images, list):
            images = [images] if images else []
            record[field_name] = images
        if 0 <= img_index < len(images):
            images.pop(img_index)


def new_blank_record() -> dict:
    """创建一条空白记录（所有字段为空，多图字段为空列表）"""
    from . import config
    record = {}
    for field in config.RECORD_FIELDS:
        if field in config.MULTI_IMAGE_FIELDS:
            record[field] = []
        elif field in config.SINGLE_IMAGE_FIELDS:
            record[field] = ""
        else:
            record[field] = ""
    return record
