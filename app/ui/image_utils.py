"""图片缩略图工具：按目标尺寸直接解码（不加载原图全尺寸）+ QPixmapCache 全局缓存。

表格与弹窗共用，避免重复实现；同一路径同一尺寸只解码一次。
"""
import os

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QImageReader, QPixmap, QPixmapCache

# 放大缓存上限，记录多时也能命中
QPixmapCache.setCacheLimit(64 * 1024)


def scaled_pixmap(image_path: str, max_size: int):
    """按最长边 max_size 等比缩放解码图片，返回 QPixmap；失败返回 None"""
    if not image_path or not os.path.exists(image_path):
        return None
    cache_key = f"{image_path}@{max_size}"
    cached = QPixmapCache.find(cache_key)
    if cached is not None:
        return cached

    reader = QImageReader(image_path)
    reader.setAutoTransform(True)  # 正确处理手机截图/照片的 EXIF 旋转
    src = reader.size()
    if not src.isValid() or src.width() <= 0 or src.height() <= 0:
        return None
    if src.width() >= src.height():
        target = QSize(max_size, max(1, src.height() * max_size // src.width()))
    else:
        target = QSize(max(1, src.width() * max_size // src.height()), max_size)
    reader.setScaledSize(target)
    image = reader.read()
    if image.isNull():
        return None
    pixmap = QPixmap.fromImage(image)
    QPixmapCache.insert(cache_key, pixmap)
    return pixmap
