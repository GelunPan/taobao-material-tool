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


def _extract_item_id(url: str) -> str:
    """从商品链接里提取 item id，支持 https://item.taobao.com/item.htm?id=xxx 或裸 id"""
    import re
    m = re.search(r"[?&]id=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"(\d{6,})", url)
    return m.group(1) if m else url.strip()


def fetch_item(url: str, cookie_file: Path = None, save_dir: Path = None,
               wait_ms: int = 6000, headless: bool = True) -> dict:
    """复用持久化 Chrome，打开商品页提取标题、价格、图片。

    url: 商品完整链接或纯 item id。阻塞调用，应放工作线程。
    """
    item_id = _extract_item_id(url)
    result = {"item_id": item_id, "title": None, "price": None,
              "main_image": None, "images": [], "saved_files": [], "error": None}
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                channel="chrome",
                headless=headless,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # 先首页暖身
            logger.info("抓商品：先访问首页暖身")
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=40000)
            time.sleep(2.5)
            item_url = f"https://item.taobao.com/item.htm?id={item_id}"
            logger.info("进入商品页: %s", item_url)
            page.goto(item_url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(wait_ms)

            cur = page.url
            if "login.taobao.com" in cur:
                result["error"] = "被重定向到登录页：cookie 已失效，请重新登录"
                ctx.close()
                return result

            info = page.evaluate("""() => {
                const text = document.body ? document.body.innerText : '';
                const block = text.indexOf('访问被拒绝') > -1 || text.indexOf('拒绝访问') > -1 || text.indexOf('亲，访问') > -1;
                const cands = [
                    document.querySelector('h1')?.textContent,
                    document.querySelector('[class*="mainTitle"]')?.textContent,
                    document.querySelector('[class*="ItemTitle"]')?.textContent,
                    document.title
                ];
                const title = (cands.find(x => x && x.trim().length > 5) || document.title || '').trim();
                // 价格
                let price = '';
                const pEl = document.querySelector('[class*="price"] [class*="text"], [class*="Price"]');
                if (pEl) price = pEl.textContent.trim();
                if (!price) {
                    const m = text.match(/¥\\s*([0-9]+(?:\\.[0-9]+)?)/);
                    if (m) price = '¥' + m[1];
                }
                // 图片
                const urls = [];
                document.querySelectorAll('img').forEach(img => {
                    let u = img.src || img.getAttribute('data-src') || '';
                    if (u && (u.indexOf('alicdn') > -1 || u.indexOf('taobaocdn') > -1)) {
                        u = u.replace(/_\\d+x\\d+q\\d+\\.(jpg|png|webp)/, '.$1');
                        if (urls.indexOf(u) === -1) urls.push(u);
                    }
                });
                return {title, price, block, urls};
            }""")
            if info.get("block"):
                result["error"] = "页面返回风控拦截（访问被拒绝）"
                ctx.close()
                return result
            result["title"] = info.get("title")
            result["price"] = info.get("price")
            images = info.get("urls") or []
            result["images"] = [u for u in images if "tps-48-48" not in u and "tps-24-" not in u]
            if result["images"]:
                result["main_image"] = result["images"][0]
            logger.info("抓到标题=%s, 价格=%s, 图片=%d", result["title"], result["price"], len(result["images"]))

            # 可选下载图片
            if save_dir and result["images"]:
                save_dir = Path(save_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                for i, img_url in enumerate(result["images"][:5]):
                    try:
                        ext = "png" if ".png" in img_url else ("webp" if ".webp" in img_url else "jpg")
                        fp = save_dir / f"{item_id}_{i+1}.{ext}"
                        resp = ctx.request.get(img_url, headers={"Referer": "https://item.taobao.com/"})
                        fp.write_bytes(resp.body())
                        result["saved_files"].append(str(fp))
                    except Exception as e:
                        logger.warning("下载图失败 %s: %s", img_url[:60], e)
            ctx.close()
    except Exception as e:
        logger.exception("抓商品异常")
        result["error"] = f"抓商品异常：{e}"
    return result
