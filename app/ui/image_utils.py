"""图片缩略图工具：按目标尺寸直接解码（不加载原图全尺寸）+ QPixmapCache 全局缓存。

表格与弹窗共用，避免重复实现；同一路径同一尺寸只解码一次。
"""
import os

from ..config import IMAGES_DIR
from PyQt6.QtCore import QSize
from PyQt6.QtGui import QImageReader, QPixmap, QPixmapCache

# 放大缓存上限，记录多时也能命中
QPixmapCache.setCacheLimit(64 * 1024)


def scaled_pixmap(image_path: str, max_size: int):
    """按最长边 max_size 等比缩放解码图片，返回 QPixmap；失败返回 None

    路径不存在时自动 fallback 到 IMAGES_DIR 下的同名文件（打包环境下
    data.json 里的绝对路径会失效，通过此机制在用户端仍能正常加载图片）。
    """
    if not image_path:
        return None
    if not os.path.exists(image_path):
        fname = os.path.basename(image_path)
        fallback = os.path.join(str(IMAGES_DIR), fname)
        if os.path.exists(fallback):
            image_path = fallback
        else:
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


def fit_width_pixmap(image_path: str, width: int, max_height: int = 20000):
    """按目标**宽度**等比缩放（长截图专用：宽度铺满、竖向滚动查看）。

    `scaled_pixmap` 是按「最长边」缩放的——整表截图动辄几千像素高，
    最长边就是高度，于是宽度被压到几十像素，糊成一团认不出来。
    长截图要的是「宽度优先」：宽度对齐视口、高度按比例溢出后靠滚动查看。
    max_height 是安全上限，防止极端长图解码出超大位图撑爆内存。
    """
    if not image_path or not os.path.exists(image_path):
        return None
    width = max(1, int(width))
    cache_key = f"{image_path}@w{width}"
    cached = QPixmapCache.find(cache_key)
    if cached is not None:
        return cached

    reader = QImageReader(image_path)
    reader.setAutoTransform(True)
    src = reader.size()
    if not src.isValid() or src.width() <= 0 or src.height() <= 0:
        return None
    height = max(1, src.height() * width // src.width())
    if height > max_height:                      # 太长则退而求其次，连宽度一起缩小
        width = max(1, width * max_height // height)
        height = max_height
    reader.setScaledSize(QSize(width, height))
    image = reader.read()
    if image.isNull():
        return None
    pixmap = QPixmap.fromImage(image)
    QPixmapCache.insert(cache_key, pixmap)
    return pixmap


def cover_pixmap(image_path: str, width: int, height: int):
    """做一张 width×height 的「封面」缩略图：先按宽度缩放，再取顶部区域。

    列表缩略图如果直接按最长边缩放，长截图会变成一条几十像素宽的细缝，
    既糊又认不出是什么。先铺满宽度、再裁掉下半部分，能一眼看到表头与前几行。
    """
    if not image_path or not os.path.exists(image_path):
        return None
    width, height = max(1, int(width)), max(1, int(height))
    cache_key = f"{image_path}@cover{width}x{height}"
    cached = QPixmapCache.find(cache_key)
    if cached is not None:
        return cached

    reader = QImageReader(image_path)
    reader.setAutoTransform(True)
    src = reader.size()
    if not src.isValid() or src.width() <= 0 or src.height() <= 0:
        return None
    scaled_h = max(1, src.height() * width // src.width())
    reader.setScaledSize(QSize(width, scaled_h))
    image = reader.read()
    if image.isNull():
        return None
    if scaled_h > height:
        image = image.copy(0, 0, width, height)
    pixmap = QPixmap.fromImage(image)
    QPixmapCache.insert(cache_key, pixmap)
    return pixmap
