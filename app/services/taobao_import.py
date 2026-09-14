"""淘宝商品信息导入服务：抓取结果 → 本地图片 → 记录字段更新。

纯服务层，不依赖任何 UI 组件；网络 I/O（图片下载）也在本模块完成，
便于工作线程统一调用，主线程只负责把字段更新写入数据仓库。

职责边界：
- download_image: 下载单张商品图到 data/images（带 Referer，规避 403）
- build_field_updates: 把 fetch_item/fetch_items 的抓取结果组装成
  可直接写入素材记录的字段更新 dict（含图片下载与规格文本生成）
- build_spec_text: SKU 分组 → 规格文本
"""
import uuid

import requests

from .. import config
from ..utils.logger import get_logger

logger = get_logger("taobao.import")

# 图片下载超时（秒）
DOWNLOAD_TIMEOUT = 20
# 单条记录最多导入的 SKU 规格图张数（防止规格爆炸把图片目录塞满）
MAX_SPEC_IMAGES = 20


def download_image(img_url: str, prefix: str, item_id: str) -> str:
    """下载单张图片到 data/images，返回本地路径；失败返回空串。

    prefix 用于文件名前缀区分来源（tb_main=商品主图 / tb_sku=SKU规格图）。
    """
    if not img_url:
        return ""
    try:
        config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        r = requests.get(
            img_url, timeout=DOWNLOAD_TIMEOUT,
            headers={"Referer": "https://item.taobao.com/"},
        )
        if r.status_code == 200:
            ext = "jpg"
            low = img_url.lower()
            if ".png" in low:
                ext = "png"
            elif ".webp" in low:
                ext = "webp"
            fname = f"{prefix}_{item_id}_{uuid.uuid4().hex[:8]}.{ext}"
            fpath = config.IMAGES_DIR / fname
            fpath.write_bytes(r.content)
            return str(fpath)
    except Exception as e:
        logger.warning("下载图片失败 %s: %s", img_url[:60], e)
    return ""


def build_spec_text(skus: list) -> str:
    """把 SKU 分组列表组装成规格文本，如「颜色: 红, 蓝 | 尺码: S, M」"""
    if not skus:
        return ""
    parts = []
    for s in skus[:3]:
        opts = ", ".join(s.get("options", [])[:5])
        parts.append(f"{s.get('name', '')}: {opts}")
    return " | ".join(parts)


def build_field_updates(res: dict) -> dict:
    """把抓取结果组装成素材记录的字段更新（含图片下载）。

    返回 dict 只包含「有内容」的字段，调用方按需合并到记录中，
    不覆盖原有内容（补手/评价/评价图片等人工填写项不在其中）。
    """
    item_id = str(res.get("item_id") or "")
    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 商品主图 → 链接主图
    main_img = res.get("main_image") or (res.get("images") or [None])[0]
    link_image = download_image(main_img, "tb_main", item_id) if main_img else ""

    # 2. SKU 规格图 → 规格图（多张）；没有 SKU 图时退回用主图占位
    spec_images = []
    for img_url in (res.get("sku_images") or [])[:MAX_SPEC_IMAGES]:
        path = download_image(img_url, "tb_sku", item_id)
        if path:
            spec_images.append(path)
    if not spec_images and link_image:
        spec_images = [link_image]

    # 展平所有 SKU 选项，供规格列下拉选择
    spec_opts = []
    seen = set()
    for s in (res.get("skus") or []):
        for opt in s.get("options", []):
            opt = opt.strip()
            if opt and opt not in seen:
                seen.add(opt)
                spec_opts.append(opt)
    updates = {
        "product_id": item_id,
        "title": res.get("title", ""),
        "spec": build_spec_text(res.get("skus") or []),
        "spec_options": spec_opts,
        "link_image": [link_image] if link_image else [],
        "spec_image": spec_images,
    }
    logger.info("商品 %s 字段组装完成: 规格图 %d 张, 主图 %s",
                item_id, len(spec_images), "已下载" if link_image else "未获取")
    return updates
