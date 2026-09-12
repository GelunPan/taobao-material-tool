"""淘宝登录态判定规则（全项目唯一来源）。

只认「登录成功后才会下发」的硬标志：

- ``unb``   用户数字 ID（登录后由淘宝下发）
- ``_nk_``  用户昵称（编码）

**为什么不能认 tracknick 等 cookie**（曾导致「没登录也提示已获取 cookie」的严重误判）：
``tracknick``、``cookie2``、``sgcookie``、``_tb_token_``、``lgc``、``uc1/uc3/uc4``、
``wk_unb`` 等 cookie 在**未登录访客**浏览淘宝时同样会下发，且多为长期 cookie，
即使浏览器关闭、登录早已失效，它们依然残留在 profile 里。
实测：一个未登录的 profile 中 unb/_nk_ 均不存在，但 tracknick、cookie2、uc3、uc4 全在，
若把 tracknick 当登录依据，就会永远判定为「已登录」。

两处调用方（requests 客户端 / Playwright 浏览器）共用本模块，避免规则再次分叉。
"""

# 登录成功后才会下发的硬标志 cookie 名
LOGIN_COOKIE_NAMES = ("unb", "_nk_")

# 视为「无效」的取值（占位/被删除/空串）
_INVALID_VALUES = {"", "0", "deleted", "%00", "null", "undefined"}


def has_login_cookie(pairs) -> bool:
    """判断 cookie 集合中是否包含登录硬标志。

    pairs: 可迭代的 (name, value) 二元组（requests 的 Cookie 对象、
           Playwright 的 cookie dict 都可在调用处转成该形式）。
    命中任一硬标志且取值有效即视为已登录。
    """
    for name, value in pairs:
        if name in LOGIN_COOKIE_NAMES:
            value = str(value or "").strip()
            if value.lower() not in _INVALID_VALUES:
                return True
    return False


def describe_missing(pairs) -> str:
    """返回缺失的硬标志说明，用于日志/提示文案"""
    present = {name for name, value in pairs
               if str(value or "").strip().lower() not in _INVALID_VALUES}
    missing = [n for n in LOGIN_COOKIE_NAMES if n not in present]
    return "缺少登录标志 " + "/".join(missing) if missing else "登录标志齐全"
