"""淘宝登录与商品抓取：基于 Playwright 驱动本机真实 Chrome。

为什么不用 QWebEngine：
- QWebEngine 是嵌入式 Chromium，指纹和真 Chrome 有可检测差异，淘宝登录页一打开就风控；
- 这里直接启动系统安装的真实 Chrome（channel="chrome"），有窗口、用户自己扫码，
  浏览器本体指纹淘宝信任度最高，配合持久化用户目录，登录态长期保留。

流程：
- 登录：打开真 Chrome -> 用户扫码 -> 轮询到 unb cookie -> 访问"我的淘宝"验证 -> 保存 cookie
- 单条抓取 fetch_item：复用同一个持久化 Chrome，先开首页暖身再进商品页，提取标题和图片
- 批量抓取 fetch_items：一个批次只启动一次 Chrome、暖身一次，逐条进商品页提取，
  每条结果通过回调流式返回；单条失败不影响后续，cookie 失效时提前终止
"""
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .. import config
from ..utils.logger import get_logger
from . import taobao_session
from .link_utils import extract_product_url

logger = get_logger("taobao.pw")


CHROME_PATHS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]
EDGE_PATHS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
]


def list_available_browsers() -> list:
    """返回本机可用的浏览器名列表（按探测顺序），供 UI 下拉选择。"""
    import os
    avail = []
    for label, paths in (("Chrome", CHROME_PATHS), ("Edge", EDGE_PATHS)):
        if any(os.path.exists(os.path.expandvars(p)) for p in paths):
            avail.append(label)
    if not avail:
        avail.append("系统自带 Chromium")
    return avail


def _find_chrome(browser: str = "auto"):
    """探测本机浏览器可执行文件路径。
    browser: "auto"=Chrome→Edge→自带Chromium; "Chrome"=只用 Chrome; "Edge"=只用 Edge。
    """
    import os
    if browser == "Chrome":
        groups = [("Chrome", CHROME_PATHS)]
    elif browser == "Edge":
        groups = [("Edge", EDGE_PATHS)]
    else:
        groups = [("Chrome", CHROME_PATHS), ("Edge", EDGE_PATHS)]
    for label, paths in groups:
        for p in paths:
            c = os.path.expandvars(p)
            if c and os.path.exists(c):
                logger.info("探测到 %s: %s", label, c)
                return c
    logger.warning("未找到 %s，将回退到 Playwright 自带 Chromium", browser)
    return None


def _launch_kwargs(headless: bool, browser: str = "auto"):
    """构造 launch 参数：优先用选定/自动探测的真浏览器，找不到回退自带 Chromium"""
    exe = _find_chrome(browser)
    kw = {
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if exe:
        kw["executable_path"] = exe
    else:
        logger.warning("回退到 Playwright 自带 Chromium（淘宝风控可能更严）")
    return kw


LOGIN_URL = "https://login.taobao.com/member/login.jhtml"
VERIFY_URL = "https://i.taobao.com/my_taobao.htm"
HOME_URL = "https://www.taobao.com/"

def _profile_dir(browser: str = "auto") -> str:
    """按浏览器返回不同的 profile 目录。Chrome/Edge 内核相近但版本有差异，
    共用同一目录会导致 cookie 数据库互相读不出，登录后抓数据报 cookie 失效。"""
    name = "taobao_edge_profile" if browser == "Edge" else "taobao_chrome_profile"
    return str(config.DATA_DIR / name)


def _browser_marker_file(cookie_file: Path) -> Path:
    return Path(cookie_file).parent / ".taobao_browser"


def _save_browser_marker(cookie_file: Path, browser: str) -> None:
    try:
        _browser_marker_file(cookie_file).write_text(browser or "auto", encoding="utf-8")
    except Exception:
        pass


def _read_browser_marker(cookie_file: Path) -> str:
    try:
        return _browser_marker_file(cookie_file).read_text(encoding="utf-8").strip()
    except Exception:
        return "auto"

# 批量抓取时两条商品之间的默认间隔（秒），降低连续请求触发风控的概率
BATCH_ITEM_DELAY_S = 2.0

# 等待用户扫码登录的超时（秒）
LOGIN_TIMEOUT_S = 180


def _has_real_login(storage_cookies: list[dict]) -> bool:
    """cookie 列表是否包含登录硬标志（规则见 taobao_session，唯一来源）。

    只认 unb / _nk_：tracknick 等 cookie 未登录访客也会有且长期残留，
    曾据此把「未登录」误判成「已登录」，导致一点登录就提示已获取 cookie。
    """
    return taobao_session.has_login_cookie(
        (c.get("name"), c.get("value")) for c in storage_cookies
    )


def clear_profile_login() -> int:
    """清除持久化 Chrome profile 中的登录信息（退出登录时调用）。

    直接删除 profile 的 cookie 数据库文件：下次启动浏览器即为全新会话，
    不会残留历史登录标志。返回成功删除的文件数。

    注意：需在浏览器未使用该 profile 时调用（Windows 下文件被占用会删除失败，
    此时仅记录日志、不影响主流程）。
    """
    removed = 0
    for base in (_profile_dir("Chrome"), _profile_dir("Edge")):
        for rel in ("Default/Network/Cookies", "Default/Network/Cookies-journal",
                    "Default/Cookies", "Default/Cookies-journal"):
            fp = Path(base) / rel
            try:
                if fp.exists():
                    fp.unlink()
                    removed += 1
                    logger.info("已清除浏览器登录信息文件: %s", fp)
            except OSError as e:
                logger.warning("清除登录信息文件失败 %s: %s", fp, e)
    if removed == 0:
        logger.info("浏览器 profile 无需清理（无 cookie 文件）")
    return removed


def _wait_for_login_marker(ctx, page, timeout_s: float) -> tuple:
    """轮询等待登录硬标志出现。

    返回 (是否检测到标志, 浏览器是否被手动关闭)。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _has_real_login(ctx.cookies()):
            return True, False
        time.sleep(1.5)
        if page.is_closed():
            return False, True
    return False, False


def _verify_login_on_site(page) -> bool:
    """访问「我的淘宝」确认登录态真实有效（未登录会被踢回登录页）"""
    try:
        page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=40000)
        time.sleep(3)
    except PWTimeout:
        logger.warning("验证页加载超时，按当前状态判定")
    cur = page.url
    ok = not ("login.taobao.com" in cur or ("login" in cur and "taobao" in cur))
    logger.info("登录态验证: url=%s -> %s", cur, "有效" if ok else "无效")
    return ok


def run_login(cookie_file: Path, on_status=None, browser: str = "auto") -> tuple[bool, str]:
    """打开真实 Chrome 完成登录，把 cookie 写入 cookie_file。

    on_status(str): 进度回调（可选）。阻塞调用，应放在工作线程里跑。
    返回 (是否成功, 说明)。

    流程：打开登录页 → 等待登录硬标志（unb/_nk_）→ 访问「我的淘宝」真实验证
    → 验证通过才保存并返回成功。
    若检测到的标志在验证中被判定无效（多为浏览器里残留的历史 cookie），
    会清除后请用户重新扫码，最多尝试两轮，避免"假成功"。
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
            kw = _launch_kwargs(headless=False, browser=browser)
            kw["args"].append("--start-maximized")
            ctx = p.chromium.launch_persistent_context(
                _profile_dir(browser),
                **kw,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _emit("已打开淘宝登录页，请用淘宝 APP 扫码登录...")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            for attempt in range(2):
                found, closed = _wait_for_login_marker(ctx, page, LOGIN_TIMEOUT_S)
                if closed:
                    _emit("浏览器被手动关闭")
                    ctx.close()
                    return False, "浏览器被关闭"
                if not found:
                    _emit(f"等待登录超时（{LOGIN_TIMEOUT_S // 60} 分钟）")
                    ctx.close()
                    return False, "等待登录超时，请重新尝试"

                unb = next((c["value"] for c in ctx.cookies() if c["name"] == "unb"), "")
                _emit(f"检测到登录标志（unb={unb or '无'}），正在验证登录态...")
                time.sleep(1.5)

                if _verify_login_on_site(page):
                    cookies = ctx.cookies()
                    out = [
                        {"name": c["name"], "value": c["value"],
                         "domain": c.get("domain", ".taobao.com"), "path": c.get("path", "/")}
                        for c in cookies
                    ]
                    cookie_file.write_text(
                        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    _save_browser_marker(cookie_file, browser)
                    _emit(f"登录验证通过，已保存 {len(out)} 条 cookie")
                    ctx.close()
                    return True, f"登录成功（{len(out)} 条 cookie）"

                # 验证失败：浏览器里残留的历史登录标志，清除后请用户重新扫码
                logger.warning("第 %d 次验证失败：登录标志无效或已过期，清除后重试", attempt + 1)
                ctx.clear_cookies()
                _emit("检测到浏览器中残留的旧登录信息已失效，已自动清除。\n请重新扫码登录。")
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            ctx.close()
            return False, "验证失败：登录态无效（已清除残留信息，请重新扫码）"
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


# ---------- 抓取公共流程（单条/批量共用） ----------

def _new_result(url: str = None) -> dict:
    """构造统一的抓取结果骨架（error 为 None 表示成功）"""
    return {"item_id": None, "title": None, "price": None, "url": url,
            "main_image": None, "images": [], "skus": [], "sku_images": [],
            "saved_files": [], "error": None}


def _launch_context(p, headless: bool = True, cookie_file: Path = None):
    """启动持久化浏览器上下文（复用登录 profile，反自动化参数）。
    按上次登录用的浏览器选择 profile 目录，避免 Chrome/Edge 目录冲突。"""
    browser = "auto"
    if cookie_file is not None:
        browser = _read_browser_marker(cookie_file)
    return p.chromium.launch_persistent_context(
        _profile_dir(browser),
        **_launch_kwargs(headless, browser),
    )


def _warmup(page) -> None:
    """首页暖身：模拟真实用户从首页进入商品页，降低风控概率"""
    logger.info("抓商品：先访问首页暖身")
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=40000)
    time.sleep(2.5)


# 商品页信息提取脚本（在页面上下文执行，返回 title/price/images/skus/skuImages/blocked）
_EXTRACT_JS = r"""() => {
    const bodyText = document.body ? document.body.innerText : '';
    const blocked = bodyText.indexOf('访问被拒绝') > -1
        || bodyText.indexOf('拒绝访问') > -1
        || bodyText.indexOf('亲，访问') > -1;

    // 非标题关键词（评价、销量、详情等区域可能也用 h1）
    const BAD_KEYWORDS = ['评价', '销量', '已售', '详情', '参数', '问答', '推荐', '相似', '猜你', '店铺'];

    // ===== 标题：精确提取，排除非标题区域 =====
    let title = '';

    // 1. 优先找商品标题专用 class
    const titleSelectors = [
        '[class*="mainTitle"]', '[class*="itemTitle"]',
        '[class*="ItemTitle"]', '[class*="title-text"]',
        '[class*="ItemHeader--title"]', '[class*="itemHeader--title"]',
        '.tb-main-title', '.tb-detail-hd h1'
    ];
    for (const sel of titleSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            const t = el.textContent.trim();
            if (t.length > 3 && !BAD_KEYWORDS.some(k => t.indexOf(k) > -1)) {
                title = t;
                break;
            }
        }
    }

    // 2. 找所有 h1，排除包含坏关键词的，选最长的
    if (!title) {
        const h1s = document.querySelectorAll('h1');
        let best = '';
        h1s.forEach(h => {
            const t = h.textContent.trim();
            if (t.length > 5 && t.length < 200
                && !BAD_KEYWORDS.some(k => t.indexOf(k) > -1)
                && t.length > best.length) {
                best = t;
            }
        });
        title = best;
    }

    // 3. 找所有包含商品标题特征的元素（较长、在页面上半部分）
    if (!title) {
        const all = document.querySelectorAll('div, span, p');
        let best = '';
        let bestY = 9999;
        all.forEach(el => {
            const t = el.textContent.trim();
            const rect = el.getBoundingClientRect();
            // 条件：长度适中、在页面上半部分(y<600)、不包含坏关键词、子元素少（避免抓到大容器）
            if (t.length > 8 && t.length < 150
                && rect.top > 0 && rect.top < 600
                && !BAD_KEYWORDS.some(k => t.indexOf(k) > -1)
                && el.children.length < 5) {
                // 优先选更靠上的
                if (rect.top < bestY) {
                    best = t;
                    bestY = rect.top;
                }
            }
        });
        title = best;
    }

    // 4. 最后 fallback：document.title 去掉后缀
    if (!title) {
        title = (document.title || '').replace(/-.*$/, '').replace(/_.*$/, '').trim();
    }

    // ===== 价格 =====
    let price = '';
    const priceEls = document.querySelectorAll(
        '[class*="price"] [class*="text"], [class*="Price"] [class*="text"], [class*="price--"], [class*="Price--"]'
    );
    for (const el of priceEls) {
        const t = el.textContent.trim();
        const m = t.match(/[¥￥]\s*([0-9]+(?:\.[0-9]+)?)/);
        if (m) { price = '¥' + m[1]; break; }
    }
    if (!price) {
        const m = bodyText.match(/[¥￥]\s*([0-9]+(?:\.[0-9]+)?)/);
        if (m) price = '¥' + m[1];
    }

    // ===== 商品主图 =====
    const images = [];
    const mainPicAreas = document.querySelectorAll(
        '[class*="mainPic"], [class*="MainPic"], [class*="gallery"], [class*="Gallery"], [class*="pic-wrap"], [class*="PicWrap"]'
    );
    let mainImgs = [];
    mainPicAreas.forEach(area => {
        area.querySelectorAll('img').forEach(img => {
            let u = img.src || img.getAttribute('data-src') || '';
            if (u && (u.indexOf('alicdn') > -1 || u.indexOf('taobaocdn') > -1)) {
                u = u.replace(/_\d+x\d+q\d+\.(jpg|png|webp|jpeg)/, '.$1');
                u = u.replace(/_\d+x\d+\.(jpg|png|webp|jpeg)/, '.$1');
                if (mainImgs.indexOf(u) === -1) mainImgs.push(u);
            }
        });
    });
    if (mainImgs.length === 0) {
        document.querySelectorAll('img').forEach(img => {
            let u = img.src || img.getAttribute('data-src') || '';
            if (!u || (u.indexOf('alicdn') === -1 && u.indexOf('taobaocdn') === -1)) return;
            const w = img.naturalWidth || 0;
            const h = img.naturalHeight || 0;
            if (w > 0 && w < 200) return;
            if (h > 0 && h < 200) return;
            if (u.indexOf('tps-') > -1 || u.indexOf('logo') > -1 || u.indexOf('icon') > -1) return;
            u = u.replace(/_\d+x\d+q\d+\.(jpg|png|webp|jpeg)/, '.$1');
            u = u.replace(/_\d+x\d+\.(jpg|png|webp|jpeg)/, '.$1');
            if (images.indexOf(u) === -1) images.push(u);
        });
    } else {
        images.push(...mainImgs);
    }

    // ===== SKU 规格信息 + SKU 图 =====
    const skus = [];
    const skuImages = [];
    const skuGroups = document.querySelectorAll(
        '[class*="skuItem"], [class*="SkuItem"], [class*="sku-item"], [class*="propItem"], [class*="PropItem"], [class*="Sku--item"], [class*="sku--item"]'
    );
    skuGroups.forEach(group => {
        const titleEl = group.querySelector('[class*="title"], [class*="Title"], dt, label, [class*="name"]');
        const groupTitle = titleEl ? titleEl.textContent.trim() : '';
        const options = [];
        // 找规格选项
        const optEls = group.querySelectorAll(
            '[class*="text"], [class*="Text"], [class*="value"], span, a, li, [class*="option"]'
        );
        optEls.forEach(opt => {
            const t = opt.textContent.trim();
            if (t && t.length < 30 && t !== groupTitle && options.indexOf(t) === -1) {
                options.push(t);
            }
            // 找 SKU 图（规格选项里的图片）
            const img = opt.querySelector('img') || (opt.tagName === 'IMG' ? opt : null);
            if (img) {
                let u = img.src || img.getAttribute('data-src') || '';
                if (u && (u.indexOf('alicdn') > -1 || u.indexOf('taobaocdn') > -1)) {
                    u = u.replace(/_\d+x\d+q\d+\.(jpg|png|webp|jpeg)/, '.$1');
                    u = u.replace(/_\d+x\d+\.(jpg|png|webp|jpeg)/, '.$1');
                    if (skuImages.indexOf(u) === -1) skuImages.push(u);
                }
            }
        });
        // 也找 group 直接包含的 img
        group.querySelectorAll('img').forEach(img => {
            let u = img.src || img.getAttribute('data-src') || '';
            if (u && (u.indexOf('alicdn') > -1 || u.indexOf('taobaocdn') > -1)) {
                u = u.replace(/_\d+x\d+q\d+\.(jpg|png|webp|jpeg)/, '.$1');
                u = u.replace(/_\d+x\d+\.(jpg|png|webp|jpeg)/, '.$1');
                if (skuImages.indexOf(u) === -1) skuImages.push(u);
            }
        });
        if (groupTitle && options.length > 0) {
            skus.push({name: groupTitle, options: options.slice(0, 15)});
        }
    });

    return {title, price, images, skus, skuImages, blocked};
}"""


def _is_cookie_invalid_error(err: str) -> bool:
    """错误文案是否表示 cookie 已失效（据此可提前终止批量抓取）"""
    return "cookie" in err and ("失效" in err or "重新登录" in err)


def _visit_and_extract(page, url: str, wait_ms: int = 8000) -> dict:
    """在已打开的页面上访问商品链接并提取信息（不管理浏览器上下文）。

    单条失败只写 result["error"]，不抛异常；调用方负责继续下一条。
    """
    result = _new_result(url)
    logger.info("进入商品页: %s", url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        result["error"] = "商品页加载超时，跳过该商品"
        return result

    # 等待页面渲染：轮询直到标题不再是默认值，或超时
    for _ in range(20):  # 最多等 10 秒
        page.wait_for_timeout(500)
        t = page.title()
        if t and t not in ("商品详情", "淘宝", "天猫", "加载中...", ""):
            break
    page.wait_for_timeout(wait_ms)

    final_url = page.url
    logger.info("最终 URL: %s", final_url)

    # 从最终 URL 提取商品 ID
    import re as _re
    m = _re.search(r"[?&]id=(\d+)", final_url)
    if m:
        result["item_id"] = m.group(1)
    else:
        m2 = _re.search(r"/(\d{6,})\.htm", final_url)
        if m2:
            result["item_id"] = m2.group(1)
    result["url"] = final_url

    if "login.taobao.com" in final_url or "login.tmall.com" in final_url:
        result["error"] = "被重定向到登录页：cookie 已失效，请重新登录"
        return result

    # 再次检查页面是否跳转（淘宝可能在加载过程中跳登录页）
    cur_url = page.url
    if "login.taobao.com" in cur_url or "login.tmall.com" in cur_url or "login_jump" in cur_url:
        result["error"] = "页面跳转到登录页：cookie 已失效或被风控，请重新登录"
        return result

    # 用更精确的 JS 提取信息（try/except 包裹，防止页面跳转导致执行上下文销毁）
    try:
        info = page.evaluate(_EXTRACT_JS)
    except Exception as e:
        logger.warning("evaluate执行失败（页面可能跳转了）: %s", e)
        # 再次检查URL
        cur_url2 = page.url
        if "login" in cur_url2 or "login_jump" in cur_url2:
            result["error"] = "页面跳转到登录页：cookie 已失效，请重新登录"
        else:
            result["error"] = f"页面解析失败：{e}"
        return result

    if info.get("blocked"):
        result["error"] = "页面返回风控拦截（访问被拒绝）"
        return result

    result["title"] = info.get("title") or ""
    result["price"] = info.get("price") or ""
    result["images"] = info.get("images") or []
    result["skus"] = info.get("skus") or []
    result["sku_images"] = info.get("skuImages") or []

    # 规格组去重（同名且选项相同的合并）
    seen_sku = set()
    unique_skus = []
    for sku in result["skus"]:
        key = sku.get("name", "") + "|" + ",".join(sku.get("options", []))
        if key not in seen_sku:
            seen_sku.add(key)
            unique_skus.append(sku)
    result["skus"] = unique_skus

    # SKU 图去重
    seen_sku_img = set()
    unique_sku_imgs = []
    for u in result["sku_images"]:
        if u not in seen_sku_img:
            seen_sku_img.add(u)
            unique_sku_imgs.append(u)
    result["sku_images"] = unique_sku_imgs[:20]

    # 去重 + 限制最多 20 张
    seen = set()
    unique_imgs = []
    for u in result["images"]:
        if u not in seen:
            seen.add(u)
            unique_imgs.append(u)
    result["images"] = unique_imgs[:20]

    if result["images"]:
        result["main_image"] = result["images"][0]

    logger.info("抓到: id=%s, 标题=%s, 价格=%s, 图片=%d张, 规格=%d组, SKU图=%d张",
                result["item_id"], result["title"], result["price"],
                len(result["images"]), len(result["skus"]), len(result["sku_images"]))
    return result


def fetch_item(url: str, cookie_file: Path = None, save_dir: Path = None,
               wait_ms: int = 8000, headless: bool = True) -> dict:
    """单条抓取：启动一次 Chrome，暖身后进商品页提取标题、价格、主图、规格。

    url: 商品完整链接（支持 e.tb.cn 短链接，自动跟随跳转；也兼容整段【淘宝】
         分享口令，入口处先识别出纯链接）。阻塞调用，应放工作线程。
    """
    url = extract_product_url(url)
    result = _new_result(url)
    try:
        with sync_playwright() as p:
            ctx = _launch_context(p, headless=headless, cookie_file=cookie_file)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _warmup(page)
            result = _visit_and_extract(page, url, wait_ms=wait_ms)

            # 可选下载图片（用浏览器上下文请求，自动带登录态）
            if save_dir and not result.get("error") and result["images"]:
                save_dir = Path(save_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                for i, img_url in enumerate(result["images"][:5]):
                    try:
                        ext = "png" if ".png" in img_url else ("webp" if ".webp" in img_url else "jpg")
                        fp = save_dir / f"{result['item_id'] or 'item'}_{i+1}.{ext}"
                        resp = ctx.request.get(img_url, headers={"Referer": result["url"]})
                        fp.write_bytes(resp.body())
                        result["saved_files"].append(str(fp))
                    except Exception as e:
                        logger.warning("下载图失败 %s: %s", img_url[:60], e)
            ctx.close()
    except Exception as e:
        logger.exception("抓商品异常")
        result["error"] = f"抓商品异常：{e}"
    return result


def fetch_items(urls: list, on_item_done=None, on_item_start=None,
                headless: bool = True, item_delay_s: float = BATCH_ITEM_DELAY_S) -> list:
    """批量抓取：整个批次只启动一次 Chrome、暖身一次，逐条进商品页提取。

    相比逐条调用 fetch_item，省去反复启动浏览器与重复暖身，速度更快、
    自动化痕迹更少；条与条之间默认间隔 BATCH_ITEM_DELAY_S 秒防风控。

    参数:
        urls: 商品链接列表（支持短链自动跳转）
        on_item_done(i, result): 每条抓完立即在当前线程回调（i 与 urls 下标对应）
        on_item_start(i, url): 每条开始抓取前回调（可用于进度提示）
        两个回调的异常都会被吞掉并记日志，不影响抓取流程
        item_delay_s: 两条商品之间的间隔秒数

    返回:
        与 urls 等长的结果列表；cookie 失效时提前终止，未抓取的条目为 None
        （整体异常时未抓条目为带 error 的结果，保证每条都有交代）。
        阻塞调用，应放工作线程。
    """
    results: list = [None] * len(urls)

    def _safe_call(callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("fetch_items 回调异常（已忽略）")

    try:
        with sync_playwright() as p:
            ctx = _launch_context(p, headless=headless, cookie_file=cookie_file)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _warmup(page)
            for i, url in enumerate(urls):
                url = extract_product_url(url)  # 兼容整段【淘宝】分享口令
                _safe_call(on_item_start, i, url)
                res = _visit_and_extract(page, url)
                results[i] = res
                _safe_call(on_item_done, i, res)
                # cookie 失效时后续必然全部失败，提前终止
                err = res.get("error") or ""
                if _is_cookie_invalid_error(err):
                    logger.warning("cookie 失效，批量抓取提前终止（剩余 %d 条未抓）",
                                   len(urls) - i - 1)
                    break
                if i < len(urls) - 1:
                    time.sleep(item_delay_s)
            ctx.close()
    except Exception as e:
        logger.exception("批量抓取异常")
        for i in range(len(urls)):
            if results[i] is None:
                results[i] = _new_result(urls[i])
                results[i]["error"] = f"批量抓取异常：{e}"
                _safe_call(on_item_done, i, results[i])
    return results
