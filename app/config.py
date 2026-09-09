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
ASSETS_DIR = BASE_DIR / "app" / "assets"   # 界面图标素材（复选框三态等）

# ---------- 应用 ----------
APP_TITLE = "淘宝评价素材整理工具"

# ---------- 素材记录字段（顺序即表格列顺序） ----------
RECORD_FIELDS = [
    "product_id",   # 商品ID
    "spec_image",   # 规格图（单张图片）
    "spec",         # 规格
    "title",        # 标题
    "link_image",   # 链接主图（单张图片）
    "helper",       # 补手
    "review",       # 买家秀评价
    "image_paths",  # 评价图片（多张，列表）
]
TABLE_HEADERS = ["商品ID", "规格图", "规格", "标题", "链接主图", "补手", "买家秀评价", "图片"]

# 单张图片字段 / 多张图片字段（均渲染为图片单元格）
SINGLE_IMAGE_FIELDS = {"spec_image", "link_image"}
MULTI_IMAGE_FIELDS = {"image_paths"}
IMAGE_FIELDS = SINGLE_IMAGE_FIELDS | MULTI_IMAGE_FIELDS

# ---------- 表格列宽模式（0=自适应内容, 1=拉伸填满） ----------
# 顺序对应 RECORD_FIELDS；表格最前面还有一列固定宽度的“选择”列（见 SELECT_COLUMN_WIDTH）
TABLE_COLUMN_MODES = [0, 0, 0, 1, 0, 0, 1, 0]
SELECT_COLUMN_WIDTH = 48       # 最左侧勾选列宽（表头为全选复选框）
SELECT_COL_HEADER = ""         # 勾选列表头留空（放置全选复选框）

# ---------- 表格行高（随内容自适应，仅限定上下限） ----------
TABLE_ROW_MIN_HEIGHT = 105     # 行高下限：单张缩略图(90)+单元格内边距，再补 item padding
TABLE_ROW_MAX_HEIGHT = 360     # 行高上限：避免超长评价把单行撑满整屏（全文可悬停查看）

# QSS 中 QTableWidget::item 的 padding（左右各 6、上下各 4）：cellWidget 的物理
# 几何会被 Qt 扣除这部分内边距与 1px 网格线，尺寸换算时必须补偿，否则缩略图被裁切
TABLE_ITEM_PAD_H = 12         # 横向被扣掉的 6*2
TABLE_ITEM_PAD_V = 8          # 纵向被扣掉的 4*2
TABLE_GRID = 1                # 网格线宽度

# 图片单元格内边距 / 多图缩略图之间的间距
TABLE_CELL_PAD = 3
TABLE_THUMB_GAP = 6
# 单图列（规格图/链接主图）与多图列（评价图片）每张缩略图边长
TABLE_SINGLE_THUMB = 90
TABLE_MULTI_THUMB = 74
# 评价图片一行最多并排几张（超出自动换行，同时限制该列最大内容宽度，避免列被撑得过宽）
TABLE_MULTI_PER_ROW = 3
# 文本单元格计算换行高度时的留白：横向需扣除 item padding，纵向加少量呼吸空间
TABLE_TEXT_HPAD = 15
TABLE_TEXT_VPAD = 12

# ---------- 图片 ----------
DIALOG_THUMB_SIZE = 84        # 弹窗单图缩略图边长
DIALOG_MULTI_SIZE = 72        # 弹窗多图缩略图边长
FULL_IMAGE_MAX_SIZE = 800     # 查看大图最大边长
IMAGE_EXT = "PNG"             # 粘贴图片保存格式

# ---------- Excel 导出 ----------
EXPORT_COLUMN_WIDTHS = [15, 30, 20, 40, 30, 15, 50, 30]
