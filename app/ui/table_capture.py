"""整表截图：把记录表格的**全部内容**（含滚出视口的行）离屏完整渲染成一张图片。

为什么不能直接对主界面表格 `grab()`：`QWidget.grab()` 只能截到控件当前可见的那一块，
滚出视口的行永远截不到。这里用一个**离屏的 RecordTable 副本**按内容总尺寸渲染，
既拿到完整内容，又不会在主界面上出现任何闪烁或尺寸跳动。

截图顶部会另画一条标题带（店铺名 + 记录数/时间），方便存档后一眼看出是哪家店的表。
"""
import time
from datetime import datetime

from PyQt6.QtCore import QEventLoop, QModelIndex, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPixmap

from .record_table import RecordTable

# 离屏表格的初始渲染宽度：列宽自适应以此为基准，之后按内容总宽收敛
_CAPTURE_WIDTH = 1680
# 每轮布局后等待列宽 / 行高 / 缩略图稳定的时间
_SETTLE_MS = 260
# 等缩略图解码完成的上限（超时兜底，绝不卡死界面）
_IMAGE_WAIT_MS = 6000
# 尺寸收敛的最大轮数（列宽分配是自洽的不动点，通常 1~2 轮即稳定）
_MAX_RESIZE_ROUNDS = 4
# 导出表单图片整体缩放系数：0.75（每边），面积约为原图的 56%。
# v0.4 用 0.5（面积 1/4）——小主反馈「有点糊」，提到 0.75 换回清晰度；想再调改这里即可
_CAPTURE_EXPORT_SCALE = 0.75

# 顶部标题带（逻辑像素）
_BAND_H = 58
_BAND_PAD_X = 22
_TITLE_FAMILY = "Microsoft YaHei"
_TITLE_SIZE = 20
_SUBTITLE_SIZE = 12
# 与 style.qss 里 `#title { color: #303133 }` 保持一致：没设过字体色时用它
_BAND_DEFAULT_TEXT = "#303133"
_SUBTITLE_DEFAULT_TEXT = "#909399"


def record_is_empty(record: dict) -> bool:
    """记录是否「全是空字段」：所有字段都没内容（空串 / None / 空图片列表）。

    导出表单图片时会跳过这类行——它们只是用户还填写的空壳，留在留档图里是噪音。
    """
    for value in record.values():
        if isinstance(value, (list, tuple)):
            if any(str(item).strip() for item in value):
                return False
        elif value is not None and str(value).strip():
            return False
    return True


def _pump(ms: int) -> None:
    """跑一段时间事件循环：让 Qt 完成布局、定时器回调与图片解码"""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _downscale(shot: QPixmap, factor: float) -> QPixmap:
    """把截图整体缩小到 factor 倍（factor<1 才生效）。

    在设备像素层面缩：先 toImage() 拿到真实像素，再 scaled，最后把 dpr 归 1，
    这样落盘分辨率确定、不受缩放屏 dpr 影响。factor=0.5 即面积变为原来的 1/4。
    """
    if factor >= 1.0 or shot.isNull():
        return shot
    img = shot.toImage()
    scaled = img.scaled(
        int(img.width() * factor), int(img.height() * factor),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    out = QPixmap.fromImage(scaled)
    out.setDevicePixelRatio(1.0)
    return out


def _full_size(table: RecordTable) -> tuple:
    """表格要完整显示全部内容所需的总尺寸（含表头、行号列与外框）"""
    frame = table.frameWidth() * 2
    width = table.verticalHeader().width() + sum(
        table.columnWidth(col)
        for col in range(table.columnCount())
        if not table.isColumnHidden(col)
    )
    height = table.horizontalHeader().height() + sum(
        table.rowHeight(row) for row in range(table.rowCount())
    )
    # +1 避免边界处最后一列/最后一行被判定为越界而不绘制
    return width + frame + 1, height + frame + 1


def _paint_title_band(shot: QPixmap, title: str, subtitle: str,
                      bg_color: str | None = None,
                      text_color: str | None = None) -> QPixmap:
    """在截图顶部加一条标题带，**样式与表单里看到的店铺名保持一致**：

    - 填充色：用户给店铺设的填充色（没设过则白底）
    - 字体色：用户给店铺设的字体色（没设过则用 style.qss 的 #303133）
    - 居中：店铺名在标题带里居中显示
    右侧仍保留一行小字副标题（记录数 / 时间），字体色取店铺字体色的淡化版，
    没设过颜色时用浅灰，保证不与主体抢眼。

    统一按**设备像素**作图、最后再把 devicePixelRatio 还原：`grab()` 在缩放屏上
    返回的 pixmap 可能带 dpr≠1，直接按逻辑坐标画会让字号和位置全算歪。
    """
    dpr = shot.devicePixelRatio() or 1.0
    source = shot.toImage()
    source.setDevicePixelRatio(1.0)
    band_h = max(1, int(round(_BAND_H * dpr)))
    pad = int(round(_BAND_PAD_X * dpr))

    canvas = QImage(source.width(), source.height() + band_h,
                    QImage.Format.Format_ARGB32_Premultiplied)
    band_bg = QColor(bg_color) if bg_color else QColor("#FFFFFF")
    if not band_bg.isValid():
        band_bg = QColor("#FFFFFF")
    canvas.fill(band_bg)

    painter = QPainter(canvas)
    try:
        title_font = QFont(_TITLE_FAMILY)
        title_font.setPixelSize(max(1, int(round(_TITLE_SIZE * dpr))))
        title_font.setBold(True)
        sub_font = QFont(_TITLE_FAMILY)
        sub_font.setPixelSize(max(1, int(round(_SUBTITLE_SIZE * dpr))))

        band_text = QColor(text_color) if text_color else QColor(_BAND_DEFAULT_TEXT)
        if not band_text.isValid():
            band_text = QColor(_BAND_DEFAULT_TEXT)
        sub_color = QColor(band_text)
        if text_color:
            # 有自定义字体色时，副标题用同色淡化，整条带子色调统一
            sub_color.setAlpha(115)
        else:
            sub_color = QColor(_SUBTITLE_DEFAULT_TEXT)

        align_center = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)
        align_right = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        usable = max(0, canvas.width() - 2 * pad)

        # 右侧副标题：先量出它占多宽，标题据此限宽，长店名不会压到它
        painter.setFont(sub_font)
        painter.setPen(sub_color)
        painter.drawText(QRect(pad, 0, usable, band_h), align_right, subtitle)
        sub_w = painter.fontMetrics().horizontalAdvance(subtitle)

        # 店铺名：居中绘制（与表单里的店铺名一样），过宽时右侧省略号收尾
        painter.setFont(title_font)
        painter.setPen(band_text)
        # 左右各留出副标题的宽度再判限宽，居中后也不会与右侧小字重叠
        title_limit = max(int(round(80 * dpr)), canvas.width() - 2 * pad - 2 * (sub_w + pad))
        painter.drawText(
            QRect(pad, 0, usable, band_h), align_center,
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, title_limit),
        )

        # 与表格之间的分隔线（没设填充色时更要它来收盘）
        if not bg_color:
            painter.setPen(QColor("#EBEDF0"))
            painter.drawLine(0, band_h - 1, canvas.width(), band_h - 1)
        painter.drawImage(0, band_h, source)
    finally:
        painter.end()

    result = QPixmap.fromImage(canvas)
    result.setDevicePixelRatio(dpr)
    return result


def capture_records_table(records: list, title: str = "", subtitle: str = "",
                          title_bg: str | None = None,
                          title_color: str | None = None,
                          expanded_state: dict | None = None) -> QPixmap | None:
    """把 records 渲染成一张完整表格截图；无记录或渲染失败时返回 None。

    title 不为空时在顶部加一条标题带（通常传店铺名），title_bg / title_color 为
    该店铺在表单里设置的填充色与字体色（店铺名居中显示，与表单所见一致）。

    expanded_state: {记录索引: {已展开的多图字段名}}，由主表格 RecordTable.expanded_state()
    给出，用于让截图「展开的就是展开的、折叠的就是折叠的」。

    records 应为**已剔除全空行**的列表（见 record_is_empty），调用方负责异常兜底
    （本函数内部已用 try/finally 保证离屏表格被释放）。
    """
    if not records:
        return None

    table = RecordTable()
    try:
        table.resize(_CAPTURE_WIDTH, 900)
        # 关掉滚动条：按内容全尺寸渲染后本就不需要滚动，留着只会占掉可用空间
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 🔴 离屏副本绝不能真的显示出来：没有父窗口的 QWidget 一旦 show() 就变成**真实顶层
        # 窗口**，会在屏幕上闪一下再消失（用户看到的就是「弹出一个图片页面」）。
        # WA_DontShowOnScreen 让 Qt 照常做布局、解码与绘制，但不把它投到屏幕上。
        table.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        table.show()
        table.render(records)
        _pump(_SETTLE_MS)

        # 先对齐主表格的展开态，再走尺寸收敛：展开的行更高，必须参与后续列宽/行高计算
        if expanded_state:
            table.apply_expanded_state(expanded_state)
            _pump(_SETTLE_MS)

        # 列宽是自洽不动点：按内容总尺寸 resize 后列宽分配会让总宽基本不变，
        # 但存在 1px 级别的小抖动，故迭代到尺寸稳定为止
        for _ in range(_MAX_RESIZE_ROUNDS):
            width, height = _full_size(table)
            if (table.width(), table.height()) == (width, height):
                break
            table.resize(width, height)
            _pump(_SETTLE_MS)

        # 缩略图是分片懒加载的：resize 后所有行才真正进入可视区，
        # 这里反复催一次并等到解码完（超时兜底）
        deadline = time.monotonic() + _IMAGE_WAIT_MS / 1000.0
        while time.monotonic() < deadline:
            table._schedule_load_visible()
            _pump(60)
            if not table._image_jobs:
                break
        _pump(120)

        # 截图是给内容留档，一切交互控件都不该出现：「选择/取消选择」按钮、勾选列表头
        # 全选框、底部悬浮「➕」；以及单元格内部的「➕ / ▼▲」小按钮与「（粘贴图片）」
        # 虚线占位框（见 RecordTable.set_capture_mode）。
        # 同时清掉默认选中的第 1 行，否则截图里会有一格带着选中高亮。
        table.set_capture_mode(True)
        table.header_check.hide()
        table.select_all_check.hide()
        table._add_btn.hide()
        table.clearSelection()
        table.setCurrentIndex(QModelIndex())
        _pump(160)

        shot = table.grab()
        # 整体缩小到 1/4（每边 ×0.5）：在设备像素层面缩放，落盘分辨率确定、不依赖 dpr
        shot = _downscale(shot, _CAPTURE_EXPORT_SCALE)
        if not title:
            return shot
        return _paint_title_band(
            shot, title,
            subtitle or f"共 {len(records)} 条记录 · {datetime.now():%Y-%m-%d %H:%M}",
            bg_color=title_bg, text_color=title_color,
        )
    finally:
        table.close()
        table.deleteLater()
