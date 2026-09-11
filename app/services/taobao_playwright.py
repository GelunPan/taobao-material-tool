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
               wait_ms: int = 8000, headless: bool = True) -> dict:
    """复用持久化 Chrome，打开商品页提取标题、价格、主图、规格。

    url: 商品完整链接（支持 e.tb.cn 短链接，自动跟随跳转）。阻塞调用，应放工作线程。
    """
    result = {"item_id": None, "title": None, "price": None, "url": None,
              "main_image": None, "images": [], "skus": [], "sku_images": [], "saved_files": [], "error": None}
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

            # 直接用原始 URL（支持短链接自动跳转），不强行转 item.taobao.com
            logger.info("进入商品页: %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # 等待页面渲染：轮询直到标题不再是默认值，或超时
            final_url = page.url
            for i in range(20):  # 最多等 10 秒
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
                ctx.close()
                return result

            # 再次检查页面是否跳转（淘宝可能在加载过程中跳登录页）
            cur_url = page.url
            if "login.taobao.com" in cur_url or "login.tmall.com" in cur_url or "login_jump" in cur_url:
                result["error"] = "页面跳转到登录页：cookie 已失效或被风控，请重新登录"
                ctx.close()
                return result

            # 用更精确的 JS 提取信息（try/except 包裹，防止页面跳转导致执行上下文销毁）
            try:
                info = page.evaluate(r"""() => {
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
            }""")
            except Exception as e:
                logger.warning("evaluate执行失败（页面可能跳转了）: %s", e)
                # 再次检查URL
                cur_url2 = page.url
                if "login" in cur_url2 or "login_jump" in cur_url2:
                    result["error"] = "页面跳转到登录页：cookie 已失效，请重新登录"
                else:
                    result["error"] = f"页面解析失败：{e}"
                ctx.close()
                return result

            if info.get("blocked"):
                result["error"] = "页面返回风控拦截（访问被拒绝）"
                ctx.close()
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

            # 可选下载图片
            if save_dir and result["images"]:
                save_dir = Path(save_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                for i, img_url in enumerate(result["images"][:5]):
                    try:
                        ext = "png" if ".png" in img_url else ("webp" if ".webp" in img_url else "jpg")
                        fp = save_dir / f"{result['item_id'] or 'item'}_{i+1}.{ext}"
                        resp = ctx.request.get(img_url, headers={"Referer": final_url})
                        fp.write_bytes(resp.body())
                        result["saved_files"].append(str(fp))
                    except Exception as e:
                        logger.warning("下载图失败 %s: %s", img_url[:60], e)
            ctx.close()
    except Exception as e:
        logger.exception("抓商品异常")
        result["error"] = f"抓商品异常：{e}"
    return result

