"""业务服务层：图片处理与 Excel 导出等可复用能力。

与界面解耦：服务只接收数据与路径，不直接操作窗口控件。
"""
from pathlib import Path

from PyQt6.QtGui import QImage

from .config import EXPORT_COLUMN_WIDTHS, RECORD_FIELDS, TABLE_HEADERS, IMAGE_EXT


class ImageService:
    """图片相关服务：剪贴板图片保存、文件名生成"""

    @staticmethod
    def save_clipboard_image(clipboard, images_dir: Path, counter: int):
        """保存剪贴板中的图片。

        返回 (文件路径, 新计数器)；剪贴板无图片或图片无效时返回 (None, counter)。
        """
        mime = clipboard.mimeData()
        if not mime.hasImage():
            return None, counter
        image = QImage(clipboard.image())
        if image.isNull():
            return None, counter

        counter += 1
        filename = f"image_{counter:04d}.{IMAGE_EXT.lower()}"
        filepath = images_dir / filename
        image.save(str(filepath), IMAGE_EXT)
        return str(filepath), counter


class ExcelExporter:
    """将素材记录导出为 Excel 文件"""

    @staticmethod
    def export(records: list, file_path: str, sheet_name: str = "素材") -> None:
        from openpyxl import Workbook
        from openpyxl.styles import Font

        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name

        # 表头
        for col, header in enumerate(TABLE_HEADERS, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)

        # 数据行（字段顺序与列顺序一一对应）
        for row, record in enumerate(records, 2):
            for col, field in enumerate(RECORD_FIELDS, 1):
                ws.cell(row=row, column=col, value=record.get(field, ""))

        # 列宽
        for col, width in enumerate(EXPORT_COLUMN_WIDTHS, 1):
            ws.column_dimensions[chr(64 + col)].width = width

        wb.save(file_path)
