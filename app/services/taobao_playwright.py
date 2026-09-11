"""淘宝登录与商品抓取：基于 Playwright 驱动本机真实 Chrome。

为什么不用 QWebEngine：
- QWebEngine 是嵌入式 Chromium，指纹和真 Chrome 有可检测差异，淘宝登录页一打开就风控；
- 这里直接启动系统安装的真实 Chrome（channel="chrome"），有窗口、用户自己扫码，
  浏览器本体指纹淘宝信任度最高，配合持久化用户目录，登录态长期保留。

流程：
- 登录：打开真 Chrome -> 用户扫码 -> 轮询到 unb cookie -> 访问"我的淘宝"验证 -> 保存 cookie
- 抓取：复用同一个持久化 Chrome，先开首页暖身再进商品页，提取标题和图片
"""
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .. import config
from ..utils.logger import get_logger

logger = get_logger("taobao.pw")

# 真正登录后才会下发的硬标志 cookie
LOGGED_IN_COOKIE_KEYS = ("unb", "tracknick")

LOGIN_URL = "https://login.taobao.com/member/login.jhtml"
VERIFY_URL = "https://i.taobao.com/my_taobao.htm"
HOME_URL = "https://www.taobao.com/"

PROFILE_DIR = str(config.DATA_DIR / "taobao_chrome_profile")


def _has_real_login(storage_cookies: list[dict]) -> bool:
    names = {c.get("name"): c.get("value", "") for c in storage_cookies}
    unb = str(names.get("unb", "")).strip()
    if unb and unb != "0":
        return True
    return bool(str(names.get("tracknick", "")).strip())


def run_login(cookie_file: Path, on_status=None) -> tuple[bool, str]:
    """打开真实 Chrome 完成登录，把 cookie 写入 cookie_file。

    on_status(str): 进度回调（可选）。阻塞调用，应放在工作线程里跑。
    返回 (是否成功, 说明)。
    """
    def _emit(msg):
        logger.info(msg)
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    cookie_file = Path(cookie_file)
    cookie_file.parent.mkdir(parents=True, exist_ok=True)

    _emit("正在启动本机 Chrome 浏览器...")
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                channel="chrome",
                headless=False,
                viewport={"width": 1280, "height": 860},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _emit("已打开淘宝登录页，请用淘宝 APP 扫码登录...")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            # 轮询等待 unb cookie 出现（最多 3 分钟）
            deadline = time.time() + 180
            logged = False
            while time.time() < deadline:
                cookies = ctx.cookies()
                if _has_real_login(cookies):
                    logged = True
                    break
                time.sleep(1.5)
                if page.is_closed():
                    _emit("浏览器被手动关闭")
                    ctx.close()
                    return False, "浏览器被关闭"

            if not logged:
                _emit("等待登录超时（3 分钟）")
                ctx.close()
                return False, "等待登录超时，请重新尝试"

            unb = next((c["value"] for c in ctx.cookies() if c["name"] == "unb"), "")
            _emit(f"检测到登录成功（unb={unb}），正在验证...")

            # 验证：访问"我的淘宝"，未登录会被踢回登录页
            time.sleep(1.5)
            try:
                page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=40000)
                time.sleep(3)
            except PWTimeout:
                _emit("验证页加载超时，按当前状态判定")
            cur = page.url
            if "login.taobao.com" in cur or ("login" in cur and "taobao" in cur):
                ctx.close()
                return False, "验证失败：被重定向到登录页，cookie 无效"

            cookies = ctx.cookies()
            # 转成程序内部格式
            out = [
                {"name": c["name"], "value": c["value"],
                 "domain": c.get("domain", ".taobao.com"), "path": c.get("path", "/")}
                for c in cookies
            ]
            cookie_file.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            _emit(f"登录验证通过，已保存 {len(out)} 条 cookie")
            ctx.close()
            return True, f"登录成功（{len(out)} 条 cookie）"
    except Exception as e:
        logger.exception("登录流程异常")
        return False, f"登录流程异常：{e}"


def fetch_item(item_id: str, cookie_file: Path, save_dir: Path = None,
               wait_ms: int = 6000) -> dict:
    """复用持久化 Chrome，打开商品页提取标题和图片。

    阻塞调用，应放工作线程。
    """
    result = {"item_id": item_id, "title": None, "main_image": None,
              "images": [], "saved_files": [], "error": None}
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                channel="chrome",
                headless=True,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # 先首页暖身
            logger.info("抓商品：先访问首页暖身")
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=40000)
            time.sleep(2.5)
            url = f"https://item.taobao.com/item.htm?id={item_id}"
            logger.info("进入商品页: %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(wait_ms)

            cur = page.url
            if "login.taobao.com" in cur:
                result["error"] = "被重定向到登录页：cookie 已失效，请重新登录"
                ctx.close()
                return result

            title = page.evaluate("""() => {
                const cands = [
                    document.querySelector('h1')?.textContent,
                    document.querySelector('[class*="mainTitle"]')?.textContent,
                    document.title
                ];
                const t = cands.find(x => x && x.trim().length > 5);
                return t ? t.trim() : document.title;
            }""")
            result["title"] = title
            if title and any(k in title for k in ["访问被拒绝", "拒绝访问", "亲，访问", "风控"]):
                result["error"] = f"触发风控：{title}"
                result["title"] = None
                ctx.close()
                return result

            images = page.evaluate("""() => {
                const urls = [];
                document.querySelectorAll('img').forEach(img => {
                    let u = img.src || img.getAttribute('data-src') || '';
                    if (u && (u.indexOf('alicdn') > -1 || u.indexOf('taobaocdn') > -1)) {
                        u = u.replace(/_\\d+x\\d+q\\d+\\.(jpg|png|webp)/, '.$1');
                        if (urls.indexOf(u) === -1) urls.push(u);
                    }
                });
                return urls;
            }""") or []
            result["images"] = [u for u in images if "tps-48-48" not in u and "tps-24-" not in u]
            if result["images"]:
                result["main_image"] = result["images"][0]
            logger.info("抓到标题=%s, 图片=%d", title, len(result["images"]))
            ctx.close()
    except Exception as e:
        logger.exception("抓商品异常")
        result["error"] = f"抓商品异常：{e}"
    return result
