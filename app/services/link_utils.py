# -*- coding: utf-8 -*-
"""商品链接识别：从用户粘贴的各种形态文本中提取纯链接。

用户经常直接复制手机淘宝的整段分享口令，例如：

    【淘宝】https://e.tb.cn/h.8qP8G0N3s7kCmIo?tk=ZxplTi8oakt CZ007 「牙科专用诊桌…」点击链接直接打开 或者 淘宝搜索直接打开

这里只负责把其中的 URL 抽出来；e.tb.cn 短链的跳转由抓取流程自动跟随，
item id 的解析由 taobao_playwright._extract_item_id 负责。

凡是有"商品链接"输入的地方（新增/修改对话框、抓取对话框、表格就地编辑、
抓取/填充/一键导入的取值处）都应经过 extract_product_url，保证落库的
product_url 始终是可直接访问的纯链接。
"""
import re

# URL 主体：到空白、中文括号/引号或全角标点为止
# （分享口令的标题用「」包着可能紧跟在链接后；中文标点也可能直接粘在链接尾部）
_URL_STOP = r"\s「」『』《》【】（），。、；：！？…—“”‘’"
_URL_RE = re.compile(r"https?://[^" + _URL_STOP + r"]+", re.IGNORECASE)

# 链接尾部可能粘连的中文标点/引号（URL 本身不会以这些字符结尾）
_TRAILING_JUNK = "。，、；：！？））》＞>\"'”’】,，"


def extract_product_url(text: str) -> str:
    """从任意粘贴文本中提取第一个 http(s) 链接。

    - 文本本身就是纯链接时原样返回（等价行为）
    - 提取不到链接时返回去除首尾空白后的原文——裸商品 ID 等合法输入不受影响
    - 结果去掉尾部粘连的标点
    """
    if not text:
        return ""
    text = str(text).strip()
    m = _URL_RE.search(text)
    if not m:
        return text
    return m.group(0).rstrip(_TRAILING_JUNK)


def extract_all_product_urls(text: str) -> list:
    if not text:
        return []
    seen=set(); out=[]
    for m in _URL_RE.finditer(str(text)):
        u=m.group(0).rstrip(_TRAILING_JUNK)
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out
