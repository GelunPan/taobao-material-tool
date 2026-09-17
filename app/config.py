"""全局配置：路径、表格列定义与界面常量。

后续新增字段时，只需同步修改 RECORD_FIELDS 与 TABLE_HEADERS，
表格渲染、搜索、导出会自动跟随，无需改动 UI 代码。
"""
from pathlib import Path
import sys

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent.parent
# 冻结（PyInstaller onedir）时，BASE_DIR 指向 _internal。只读资源（图标 / qss）仍从
# 此处取；但用户数据必须放到 _internal 之外的安装根目录 data/，原因有二：
#   1. _internal 是“冻结运行时”，重装 / 修复时会被整体覆盖，放在里面会丢数据；
#   2. 保持 _internal 纯净，便于校验与排错。
# 未冻结（源码直跑）时仍用项目内的 data/，开发调试不受影响。
if getattr(sys, "frozen", False):
    INSTALL_DIR = Path(sys.executable).resolve().parent
    DATA_DIR = INSTALL_DIR / "data"
else:
    DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
DATA_FILE = DATA_DIR / "data.json"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"   # 表单截图历史目录（一键截图 + 历史查看共用）
ASSETS_DIR = BASE_DIR / "app" / "assets"   # 界面图标素材（复选框三态等）

# ---------- 应用 ----------
APP_TITLE = "淘宝评价工具"
APP_VERSION = "1.3.0"
APP_VERSION_NAME = "v1.3 正式版"

# ---------- 素材记录字段（顺序即表格列顺序） ----------
RECORD_FIELDS = [
    "product_id",   # 商品ID
    "link_image",   # 链接主图（多张图片）
    "title",        # 标题
    "spec_image",   # 规格图（多张图片，SKU图）
    "spec",         # 规格
    "review",       # 买家秀评价
    "image_paths",  # 评价图片（多张，列表）
    "helper",       # 补手
    "product_url",  # 商品链接（文本，放在图片列右侧）
    "created_time", # 创建时间（自动填充）
]
TABLE_HEADERS = ["商品ID", "链接主图", "标题", "规格图", "规格", "买家秀评价", "评价图片", "补手", "商品链接", "创建时间"]

# 单张图片字段 / 多张图片字段（均渲染为图片单元格）
SINGLE_IMAGE_FIELDS = set()
MULTI_IMAGE_FIELDS = {"spec_image", "link_image", "image_paths"}
IMAGE_FIELDS = SINGLE_IMAGE_FIELDS | MULTI_IMAGE_FIELDS

# ---------- 表格列宽模式（0=按内容自适应的普通列, 1=弹性列瓜分剩余宽度） ----------
# 顺序对应 RECORD_FIELDS；表格最前面还有一列固定宽度的“选择”列（见 SELECT_COLUMN_WIDTH）
# 所有数据列均可手动拖拽宽度、拖动表头换位；普通列按内容收缩并夹在上下限之间，
# 弹性列（标题/评价）自动瓜分剩余空间，窗口缩小时同步自适应缩小
TABLE_COLUMN_MODES = [0, 0, 1, 0, 0, 1, 0, 0, 0, 0]  # 标题/买家秀评价为弹性列
TABLE_LINK_COLUMN_WIDTH = 120  # 商品链接列固定宽度
TABLE_DEFAULT_COL_WIDTH = 90     # 普通文本列的较小默认宽度（内容更短时收缩到内容宽度）
TABLE_SHORT_COL_MAX_WIDTH = 200  # 普通文本列按内容自适应的宽度上限，防止超长内容把列撑爆
TABLE_STRETCH_MIN_WIDTH = 120    # 弹性列最小宽度：总空间不够时不再压缩，改为出横向滚动条
SELECT_COLUMN_WIDTH = 48       # 最左侧勾选列宽（表头为全选复选框）
SELECT_COL_HEADER = ""         # 勾选列表头留空（放置全选复选框）
TABLE_COL_INIT_SCALE = 0.6     # 各数据列初始宽度按设计值的 60% 呈现，更紧凑
ROW_HEADER_WIDTH = 28          # 左侧行号（垂直表头）宽度：数字清晰可读，两位数不挤压

# ---------- 表格行高（下限保底，高度完全随内容自适应，长文本自动换行全部展示） ----------
TABLE_ROW_MIN_HEIGHT = 105     # 行高下限：单张缩略图(90)+单元格内边距，再补 item padding

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
# 评价图片折叠态最多展示几张（超出出现“展开/收起”小符号按钮）
TABLE_MULTI_VISIBLE = 5
# 多图单元格底部小按钮行（展开/添加）的固定高度
TABLE_MULTI_BTN_H = 24
# 文本单元格计算换行高度时的留白：横向需扣除 item padding，纵向加少量呼吸空间
TABLE_TEXT_HPAD = 15
TABLE_TEXT_VPAD = 12

# ---------- 图片 ----------
DIALOG_THUMB_SIZE = 84        # 弹窗单图缩略图边长
DIALOG_MULTI_SIZE = 72        # 弹窗多图缩略图边长
FULL_IMAGE_MAX_SIZE = 800     # 查看大图最大边长
IMAGE_EXT = "PNG"             # 粘贴/导入图片保存格式

# 需要**居中**显示的文本列（其余文本列统一左对齐）
# 约束：只放字段名，表格渲染、Excel 导出共用同一份定义，避免两处各写一遍而走样
TABLE_CENTER_FIELDS = {"product_id"}

# ---------- Excel 导出 ----------
# 业务字段 -> 导出列宽（字符数，约 7px/字符）与对齐，全部由字段类型推导，不写死索引
EXPORT_TEXT_COL_WIDTH = 26      # 普通文本列宽度
EXPORT_TEXT_WIDTH_OVERRIDES = {  # 个别文本列自定义宽度（内容短，不用给足）
    "product_id": 18,
    "helper": 12,
    "spec": 22,
}
# 商品链接列：单元格里写**完整链接**（方便直接复制粘贴去用），只是列宽收窄——
# 看的人不需要一眼看全链接长什么样，点一下在编辑栏里照样是完整值
EXPORT_LINK_COL_WIDTH = 16
EXPORT_TEXT_WRAP = True         # 文本列自动换行，长标题/长评价完整展示
EXPORT_TITLE_HEIGHT = 30        # 顶部店铺名标题行高（磅）
EXPORT_HEADER_HEIGHT = 22       # 表头行高（磅）
EXPORT_FREEZE_ROW = 3           # 冻结前两行（店铺名 + 表头），滚动时表头常驻

# ---------- Excel 内嵌图片 ----------
# 嵌入的是**原图文件本身**（不重编码、不生成缩略图），只约束它的显示尺寸
EXPORT_IMAGE_MAX_COUNT = 6      # 每格最多并排几张：再多整列会被撑爆
EXPORT_PX_PER_CHAR = 7          # Excel 列宽单位换算：1 字符 ≈ 7px
EXPORT_PX_TO_PT = 0.75          # Excel 行高单位换算：1px = 0.75pt
EXPORT_IMAGE_DISPLAY_SCALE = 0.125  # 显示尺寸 = 原图 × 1/8（此前按原始尺寸显示太大）
EXPORT_IMAGE_MAX_HEIGHT_PX = 533   # 单张显示高度硬上限（1/8 后仍超高的巨图兜底，行高上限 409.5pt ≈ 546px）
EXPORT_IMAGE_MAX_WIDTH_PX = 1785   # 图片列总宽硬上限（Excel 列宽上限 255 字符 ≈ 1785px）
EXPORT_IMAGE_EMPTY_COL_WIDTH = 10  # 该列一条图都没有时的窄列宽

# ---------- 淘宝导入 ----------
TAOBAO_COOKIE_FILE = DATA_DIR / "taobao_cookie.json"   # 淘宝登录 cookie 持久化文件
