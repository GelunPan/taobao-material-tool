"""业务服务层：图片处理与 Excel 导出等可复用能力。

与界面解耦：服务只接收数据与路径，不直接操作窗口控件。
"""
import os
import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtGui import QImage

from ..config import (
    EXPORT_FREEZE_ROW,
    EXPORT_HEADER_HEIGHT,
    EXPORT_IMAGE_DISPLAY_SCALE,
    EXPORT_IMAGE_MAX_COUNT,
    EXPORT_IMAGE_MAX_HEIGHT_PX,
    EXPORT_IMAGE_MAX_WIDTH_PX,
    EXPORT_IMAGE_EMPTY_COL_WIDTH,
    EXPORT_LINK_COL_WIDTH,
    EXPORT_PX_PER_CHAR,
    EXPORT_PX_TO_PT,
    EXPORT_TEXT_COL_WIDTH,
    EXPORT_TEXT_WIDTH_OVERRIDES,
    EXPORT_TEXT_WRAP,
    EXPORT_TITLE_HEIGHT,
    IMAGE_EXT,
    IMAGE_FIELDS,
    RECORD_FIELDS,
    SCREENSHOTS_DIR,
    TABLE_CENTER_FIELDS,
    TABLE_HEADERS,
)

# 复制图片文件粘贴时识别的扩展名
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".ico"}


class ImageService:
    """图片相关服务：剪贴板图片保存、文件名生成"""

    @staticmethod
    def save_clipboard_image(clipboard, images_dir: Path, counter: int):
        """保存剪贴板中的图片，支持两种来源：

        1. 剪贴板二进制图片（截图 / 复制图片内容）→ 保存到 images_dir；
        2. 复制的图片文件（文件管理器 Ctrl+C）→ 直接引用原文件路径。

        返回 (文件路径, 新计数器)；剪贴板无有效图片时返回 (None, counter)。
        """
        mime = clipboard.mimeData()
        if mime is None:
            return None, counter

        # 来源一：剪贴板二进制图片（截图等）
        image = ImageService._image_from_mime(clipboard, mime)
        if not image.isNull():
            import hashlib, tempfile
            images_dir.mkdir(parents=True, exist_ok=True)
            # 先存临时文件算MD5
            tmp = images_dir / "_tmp_hash.png"
            image.save(str(tmp), "PNG")
            md5 = hashlib.md5(open(tmp, "rb").read()).hexdigest()
            # 查 images_dir 里有没有同 md5 的图
            reused = None
            for existing in images_dir.glob("image_*.png"):
                try:
                    if hashlib.md5(open(existing, "rb").read()).hexdigest() == md5:
                        reused = str(existing)
                        break
                except Exception:
                    continue
            tmp.unlink(missing_ok=True)
            if reused:
                return reused, counter
            counter += 1
            filepath = images_dir / f"image_{counter:04d}.{IMAGE_EXT.lower()}"
            if image.save(str(filepath), IMAGE_EXT):
                return str(filepath), counter
            return None, counter - 1

        # 来源二：复制的图片文件（text/uri-list，文件管理器复制文件场景）
        path = ImageService._first_image_file(mime)
        if path:
            return path, counter

        return None, counter

    @staticmethod
    def import_image_files(src_paths, images_dir: Path, counter: int):
        """把用户从文件对话框选中的外部图片统一读入并保存到 images_dir。

        不直接引用原文件路径（原文件可能被移动/删除），而是用 QImage 解码后
        按统一编号保存为 IMAGE_EXT，和剪贴板保存的图片口径一致。
        返回 (已保存路径列表, 新计数器)；无法读取的文件自动跳过。
        """
        import hashlib
        saved = []
        images_dir.mkdir(parents=True, exist_ok=True)
        for src in src_paths:
            image = QImage(str(src))
            if image.isNull():
                continue
            # 算哈希查重复
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buf, IMAGE_EXT)
            md5 = hashlib.md5(bytes(ba)).hexdigest()
            reused = None
            for existing in images_dir.glob("image_*"):
                try:
                    with open(existing, "rb") as f:
                        if hashlib.md5(f.read()).hexdigest() == md5:
                            reused = str(existing)
                            break
                except Exception:
                    continue
            if reused:
                saved.append(reused)
                continue
            counter += 1
            filepath = images_dir / f"image_{counter:04d}.{IMAGE_EXT.lower()}"
            if image.save(str(filepath), IMAGE_EXT):
                saved.append(str(filepath))
            else:
                counter -= 1
        return saved, counter

    @staticmethod
    def _image_from_mime(clipboard, mime):
        """从剪贴板提取图片 QImage：优先 clipboard.image()，失败再从原始字节兜底解码。

        hasImage() 只认 Qt 内部格式，部分程序复制图片时仅写入 image/png 等字节，
        此时 hasImage() 为 False，因此这里不依赖 hasImage() 而直接尝试字节解码。
        """
        if mime.hasImage():
            image = clipboard.image()
            if not image.isNull():
                return image
        return ImageService._decode_from_mime(mime)

    @staticmethod
    def _first_image_file(mime) -> str | None:
        """从剪贴板的文件 URL 里取第一个本地图片文件路径，无则返回 None"""
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            local = url.toLocalFile()
            if local and os.path.exists(local) and Path(local).suffix.lower() in _IMAGE_SUFFIXES:
                return local
        return None

    @staticmethod
    def _decode_from_mime(mime):
        """从剪贴板 MIME 数据中按常见图片格式兜底解码出 QImage，失败返回 null"""
        image = QImage()
        for fmt in ("image/png", "image/bmp", "image/jpeg", "image/webp",
                    "image/gif", "image/tiff"):
            if mime.hasFormat(fmt) and image.loadFromData(mime.data(fmt)):
                return image
        return QImage()


class ScreenshotStore:
    """表单截图的历史存档：命名规则、落盘与历史列表。

    命名规则 `{店铺名}_{序号}.png`，序号按店铺各自从 1 递增；
    生成默认名时会自动跳到第一个没被占用的序号，绝不覆盖已有截图。
    """

    # Windows 文件名非法字符 + 换行/制表符
    _ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')

    @classmethod
    def safe_name(cls, shop: str) -> str:
        """店铺名 -> 可用作文件名的形式（替换非法字符、去掉首尾空白与点）"""
        cleaned = cls._ILLEGAL.sub("_", str(shop or "")).strip().strip(".")
        return cleaned or "未命名店铺"

    @staticmethod
    def ensure_dir(directory=None) -> Path:
        """确保目录存在并返回 Path（传 None 时用默认历史目录）"""
        directory = Path(directory or SCREENSHOTS_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @classmethod
    def next_index(cls, shop: str, directory) -> int:
        """该店铺下一个可用序号：扫描历史目录里已用的序号，从 1 起找第一个空位"""
        directory = Path(directory)
        prefix = cls.safe_name(shop) + "_"
        used = set()
        if directory.exists():
            for path in directory.glob(prefix + "*.png"):
                tail = path.stem[len(prefix):]
                if tail.isdigit():
                    used.add(int(tail))
        index = 1
        while index in used:
            index += 1
        return index

    @classmethod
    def default_path(cls, shop: str, directory=None) -> Path:
        """该店铺下一个截图的历史路径（保存对话框的默认位置与文件名）"""
        directory = cls.ensure_dir(directory or SCREENSHOTS_DIR)
        return directory / f"{cls.safe_name(shop)}_{cls.next_index(shop, directory)}.png"

    @classmethod
    def save(cls, pixmap, target, shop: str, directory=None) -> tuple:
        """保存截图，返回 (选定路径, 历史路径)。

        写入选定路径后：若该路径就在历史目录内则无需重复存档；
        若用户另存到了别处，再往历史目录补存一份**按「店铺名_序号」规范命名**的副本，
        这样历史截图里的文件名始终统一、序号也连续递增，不会因为改了保存位置而丢档或串号。
        """
        directory = cls.ensure_dir(directory or SCREENSHOTS_DIR)
        target = Path(target)
        if target.parent != Path():
            target.parent.mkdir(parents=True, exist_ok=True)
        if not pixmap.save(str(target), "PNG"):
            raise OSError(f"无法写入图片文件：{target}")

        if target.parent.resolve() == directory.resolve():
            return target, target

        # 目标在历史目录之外：目标文件不在历史目录里，故 next_index 不会与它撞号
        history = directory / f"{cls.safe_name(shop)}_{cls.next_index(shop, directory)}.png"
        pixmap.save(str(history), "PNG")
        return target, history

    @classmethod
    def list_all(cls, directory=None) -> list:
        """历史截图列表（新的在前）；每项为 dict：name / path / shop / index / mtime / size"""
        directory = Path(directory or SCREENSHOTS_DIR)
        if not directory.exists():
            return []
        items = []
        for path in directory.glob("*.png"):
            try:
                stat = path.stat()
            except OSError:
                continue    # 文件刚好被外部删掉，跳过而不是崩
            shop, index = cls.parse_name(path.stem)
            items.append({
                "name": path.name,
                "path": str(path),
                "shop": shop,
                "index": index,
                "mtime": stat.st_mtime,
                "time_text": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size_text": cls._human_size(stat.st_size),
            })
        items.sort(key=lambda it: it["mtime"], reverse=True)
        return items

    @classmethod
    def remove(cls, path, directory=None) -> tuple:
        """删除单张历史截图，返回 (是否成功, 错误信息)。

        只认历史目录里的 `*.png`，避免被传进来的任意路径误删别处的文件。
        """
        try:
            target = Path(path).resolve()
        except OSError as exc:
            return False, str(exc)
        directory = Path(directory or SCREENSHOTS_DIR).resolve()
        if target.parent != directory or target.suffix.lower() != ".png":
            return False, "只允许删除截图历史目录里的 PNG 文件"
        try:
            target.unlink()
        except OSError as exc:
            return False, str(exc)
        return True, ""

    @classmethod
    def parse_name(cls, stem: str) -> tuple:
        """从 `店铺名_序号` 反解出 (店铺名, 序号)；不像本工具产出的名字则序号为 0"""
        shop, sep, tail = str(stem).rpartition("_")
        if sep and tail.isdigit():
            return shop, int(tail)
        return str(stem), 0

    @classmethod
    def clear_all(cls, directory=None) -> tuple:
        """清空历史目录里的全部截图，返回 (删除成功数, 失败文件名列表)。

        只删 `*.png`（本工具归档的截图），不动目录里的其它文件，目录本身也保留。
        这是**真删除**、不进回收站，所以调用方必须先做二次确认。
        """
        directory = Path(directory or SCREENSHOTS_DIR)
        removed, failed = 0, []
        if not directory.exists():
            return removed, failed
        for path in sorted(directory.glob("*.png")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                failed.append(path.name)   # 被占用/无权限：记录下来，不中断整批
        return removed, failed

    @staticmethod
    def _human_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} GB"


class ExcelExporter:
    """将素材记录导出为 Excel 文件"""

class ExcelExporter:
    """导出 Excel：店铺名大标题 + 居中表头 + 图片列**嵌入缩略图** + 链接列只标「有链接」。

    布局（列顺序与 TABLE_HEADERS 一一对应，全部由字段类型推导，不写死列索引）：
      第 1 行  店铺名（合并整行、居中、加粗加大）
      第 2 行  表头（居中、加粗、浅灰底），冻结前两行
      第 3 行起 数据。图片字段嵌缩略图（横向并排，最多 EXPORT_IMAGE_MAX_COUNT 张）；
               商品链接只写「有链接」并限宽 —— 不用看链接长什么样，只要知道有链接。

    缩略图用 Pillow 生成到临时目录再交给 openpyxl；**没装 Pillow 时自动降级为写图片路径**，
    不会让导出直接失败（Pillow 已列入 requirements.txt）。
    """

    # 表头/数据起始行（第 1 行被店铺名标题占用）
    _HEADER_ROW = 2
    _DATA_ROW = 3

    @staticmethod
    def export(records: list, file_path: str, sheet_name: str = "素材",
               shop_name: str = "") -> None:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name or "素材"
        ncol = len(RECORD_FIELDS)

        # ---- 第 1 行：店铺名标题 ----
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
        title_cell = ws.cell(row=1, column=1, value=shop_name or sheet_name or "素材导出")
        title_cell.font = Font(bold=True, size=14)
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = EXPORT_TITLE_HEIGHT

        # ---- 第 2 行：表头（居中 + 浅灰底）----
        header_fill = PatternFill("solid", fgColor="F2F6FC")
        for col, header in enumerate(TABLE_HEADERS, 1):
            cell = ws.cell(row=ExcelExporter._HEADER_ROW, column=col, value=header)
            cell.font = Font(bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[ExcelExporter._HEADER_ROW].height = EXPORT_HEADER_HEIGHT

        # ---- 列宽：图片列按嵌入方案的实际显示宽度换算，其余按字段类型取固定宽 ----
        plan = ExcelExporter._image_plan(records)
        for col, field in enumerate(RECORD_FIELDS, 1):
            ws.column_dimensions[get_column_letter(col)].width = ExcelExporter._column_width(
                field, plan
            )

        # ---- 数据行 ----
        for r, record in enumerate(records):
            row = ExcelExporter._DATA_ROW + r
            embedded = False
            for col, field in enumerate(RECORD_FIELDS, 1):
                value = record.get(field, "")
                if field in IMAGE_FIELDS:
                    embedded = ExcelExporter._write_images(
                        ws, row, col, ExcelExporter._image_paths(value), plan.get(field)
                    ) or embedded
                elif field == "product_url":
                    # 写**完整链接**：这一列就是拿来复制粘贴用的；
                    # 列宽故意收窄，看不全没关系，点开单元格在编辑栏里是完整值
                    text = "" if value is None else str(value).strip()
                    cell = ws.cell(row=row, column=col, value=text)
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                else:
                    text = "" if value is None else str(value)
                    cell = ws.cell(row=row, column=col, value=text)
                    cell.alignment = Alignment(
                        horizontal="center" if field in TABLE_CENTER_FIELDS else "left",
                        vertical="center",
                        wrap_text=EXPORT_TEXT_WRAP,
                    )
            if embedded:
                # 有图才需要加高：空行保持默认行高，整表不会被拉得老长
                ws.row_dimensions[row].height = plan.get("_row_height", 0) or 0

        ws.freeze_panes = f"A{EXPORT_FREEZE_ROW}"
        wb.save(file_path)

    # ---------- 内部：列宽 / 图片 ----------
    @staticmethod
    def _column_width(field: str, plan: dict) -> float:
        """图片列宽 = 嵌入方案的显示总宽（px）换算成字符；链接列收窄；其余按字段取"""
        if field in IMAGE_FIELDS:
            entry = plan.get(field) or {}
            return round(entry.get("col_width", EXPORT_IMAGE_EMPTY_COL_WIDTH), 1)
        if field == "product_url":
            return EXPORT_LINK_COL_WIDTH
        return EXPORT_TEXT_WIDTH_OVERRIDES.get(field, EXPORT_TEXT_COL_WIDTH)

    @staticmethod
    def _image_paths(value) -> list:
        """字段值 -> 实际存在的图片路径列表（兼容字符串与列表两种存储形态）"""
        raw = value if isinstance(value, list) else ([value] if value else [])
        return [str(p) for p in raw if p and os.path.exists(str(p))]

    @staticmethod
    def _image_plan(records: list) -> dict:
        """一次性算好每个图片列的嵌入方案，供列宽与逐行嵌入共用（避免两处各算各的）。

        返回 {字段: {"scale": 缩放系数, "col_width": 列宽字符, "sizes": {路径: (w,h)}}}
        以及 "_row_height"（有图行的行高，磅）。

        规则（都受 Excel 自身的硬上限约束）：
        - 嵌入原图文件本身，不重编码；显示尺寸保持**原始宽高比**（之前 pad 成正方形是错的）
        - 显示尺寸 = 原图 × EXPORT_IMAGE_DISPLAY_SCALE（1/8，v0.6 起默认，此前按原尺寸显示太大）
        - 单张显示高度硬上限 EXPORT_IMAGE_MAX_HEIGHT_PX（1/8 后仍超高的巨图兜底，
          Excel 行高上限 409.5pt ≈ 546px 放不下）
        - 整列并排总宽硬上限 EXPORT_IMAGE_MAX_WIDTH_PX（Excel 列宽上限 255 字符），
          超了就整列等比再缩小，保证同一列的图大小一致
        """
        sizes: dict[str, tuple[int, int]] = {}      # 路径 -> 原始像素尺寸
        try:
            from PIL import Image as PILImage
        except ImportError:
            return {}       # 没装 Pillow：走"写路径"的降级路径，也就无所谓列宽了

        def original_size(path: str) -> tuple[int, int]:
            if path not in sizes:
                try:
                    with PILImage.open(path) as im:
                        sizes[path] = (im.width, im.height)
                except Exception:
                    sizes[path] = (0, 0)
            return sizes[path]

        plan: dict = {}
        max_row_height_px = 0        # 所有图片列里最高的一张（决定有图行的行高）
        for field in IMAGE_FIELDS:
            max_w = 0
            need_scale = 1.0
            for record in records:
                paths = ExcelExporter._image_paths(record.get(field, ""))[:EXPORT_IMAGE_MAX_COUNT]
                if not paths:
                    continue
                total = 0
                row_h = 0
                for path in paths:
                    w0, h0 = original_size(path)
                    if not w0 or not h0:
                        continue
                    # 显示尺寸 = 原图 × 1/8；超高巨图再受高度硬上限兜底
                    # （注意不能"先封顶再乘 1/8"，那会把巨图压得过小）
                    w, h = round(w0 * EXPORT_IMAGE_DISPLAY_SCALE), round(h0 * EXPORT_IMAGE_DISPLAY_SCALE)
                    if h > EXPORT_IMAGE_MAX_HEIGHT_PX:
                        k = EXPORT_IMAGE_MAX_HEIGHT_PX / h
                        w, h = round(w * k), round(h * k)
                    sizes[path] = (w, h)      # 记下最终显示尺寸，供嵌入时直接用
                    total += w + 4            # 4px 为图与图之间的留白
                    row_h = max(row_h, h)
                max_w = max(max_w, total)
                max_row_height_px = max(max_row_height_px, row_h)
                if total > EXPORT_IMAGE_MAX_WIDTH_PX:
                    need_scale = min(need_scale, EXPORT_IMAGE_MAX_WIDTH_PX / total)

            if not max_w:
                continue
            scale = round(need_scale, 4)
            plan[field] = {
                "scale": scale,
                "col_width": min(255, max_w * scale / EXPORT_PX_PER_CHAR),
                "sizes": sizes,
            }
        plan["_row_height"] = round(max_row_height_px * EXPORT_PX_TO_PT, 1)
        return plan

    @staticmethod
    def _write_images(ws, row: int, col: int, paths: list, entry: dict | None) -> bool:
        """把**原图**按方案里的显示尺寸横向并排嵌入单元格，返回是否嵌入了至少一张。

        多张图靠 `colOff` 横向偏移排在同一格里；没装 Pillow（算不出尺寸）时降级为
        把路径写进单元格，保证导出不中断。
        """
        if not paths:
            return False
        try:
            from openpyxl.drawing.image import Image as XLImage
        except ImportError:
            ws.cell(row=row, column=col, value="\n".join(paths))
            return False
        if not entry:
            ws.cell(row=row, column=col, value="\n".join(paths))
            return False

        from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
        from openpyxl.drawing.xdr import XDRPositiveSize2D
        from openpyxl.utils.units import pixels_to_EMU

        scale = entry.get("scale", 1.0)
        known = entry.get("sizes", {})
        x = 0
        embedded = False
        for path in paths[:EXPORT_IMAGE_MAX_COUNT]:
            w, h = known.get(path, (0, 0))
            if not w or not h:
                continue
            image = XLImage(path)          # 直接引用原图文件，不重编码
            image.width, image.height = w, h
            image.anchor = OneCellAnchor(
                _from=AnchorMarker(col=col - 1, colOff=pixels_to_EMU(x),
                                   row=row - 1, rowOff=pixels_to_EMU(2)),
                ext=XDRPositiveSize2D(pixels_to_EMU(w), pixels_to_EMU(h)),
            )
            ws.add_image(image)
            x += w + 4
            embedded = True
        return embedded
