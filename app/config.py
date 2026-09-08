"""全局配置：路径、表格列定义与界面常量。

后续新增字段时，只需同步修改 RECORD_FIELDS 与 TABLE_HEADERS，
表格渲染、搜索、导出会自动跟随，无需改动 UI 代码。
"""
from pathlib import Path

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
DATA_FILE = DATA_DIR / "data.json"

# ---------- 应用 ----------
APP_TITLE = "淘宝评价素材整理工具"

# ---------- 素材记录字段（顺序即表格列顺序） ----------
RECORD_FIELDS = [
    "product_id",   # 商品ID
    "spec_image",   # 规格图（图片）
    "spec",         # 规格
    "title",        # 标题
    "link_image",   # 链接主图（图片）
    "helper",       # 补手
    "review",       # 买家秀评价
    "image_path",   # 评价图片（图片）
]
TABLE_HEADERS = ["商品ID", "规格图", "规格", "标题", "链接主图", "补手", "买家秀评价", "图片"]

# 图片类型字段（渲染为图片单元格）
IMAGE_FIELDS = {"spec_image", "link_image", "image_path"}

# ---------- 表格列宽模式（0=自适应内容, 1=拉伸填满） ----------
TABLE_COLUMN_MODES = [0, 0, 0, 1, 0, 0, 1, 0]
TABLE_ROW_HEIGHT = 110

# ---------- 图片 ----------
THUMBNAIL_SIZE = 100          # 表格缩略图边长
FULL_IMAGE_MAX_SIZE = 800     # 查看大图最大边长
IMAGE_EXT = "PNG"             # 粘贴图片保存格式

# ---------- Excel 导出 ----------
EXPORT_COLUMN_WIDTHS = [15, 30, 20, 40, 30, 15, 50, 30]
