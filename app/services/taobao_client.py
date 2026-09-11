"""淘宝客户端：cookie 管理、登录状态检测、商品详情抓取。

当前阶段：登录获取 cookie + 商品详情抓取（标题、主图）；后续商品详情抓取、图片下载等功能在此扩展。
cookie 以 JSON 持久化到本地，避免每次启动重新登录。
所有操作记录日志到 data/logs/taobao.log（不上传 git）。
"""
import json
from pathlib import Path

import requests

# PyQt6 导入（用于 QWebEngineView 渲染动态页面抓取数据）
from PyQt6.QtCore import QEventLoop, QTimer, QUrl
from PyQt6.QtNetwork import QNetworkCookie
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView

from .. import config
from ..utils.logger import get_logger

logger = get_logger("taobao")


# 登录成功的关键 cookie 名（有这些基本说明已登录）
LOGIN_COOKIE_KEYS = ("_tb_token_", "cookie2", "sgcookie", "unb")

# 请求时统一带的 UA（移动端 H5，后续抓商品详情用）
DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

# 桌面端 Chrome UA（用于 QWebEngineView 渲染 PC 商品详情页）
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class TaobaoClient:
    """淘宝客户端：管理 cookie 与会话，检测登录状态。

    用法：
        client = TaobaoClient(cookie_file)
        if not client.is_logged_in():
            # 弹登录框，登录成功后调用 client.save_cookies(cookies)
            ...
        # 后续用 client.session 发请求
    """

    def __init__(self, cookie_file: Path):
        self.cookie_file = Path(cookie_file)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_UA,
            "Referer": "https://h5.m.taobao.com/",
        })
        logger.debug("TaobaoClient 初始化，cookie 文件: %s", self.cookie_file)
        self._load_cookies()

    # ---------- cookie 持久化 ----------
    def _load_cookies(self) -> None:
        """从本地 JSON 加载 cookie 到 session"""
        if not self.cookie_file.exists():
            logger.debug("cookie 文件不存在，跳过加载")
            return
        try:
            cookies = json.loads(self.cookie_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("cookie 文件解析失败: %s", e)
            return
        count = 0
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue
            self.session.cookies.set(
                name, value,
                domain=c.get("domain", ".taobao.com"),
                path=c.get("path", "/"),
            )
            count += 1
        logger.info("从本地加载 %d 条 cookie", count)

    def save_cookies(self, cookies: list[dict]) -> None:
        """保存 cookie 列表到本地，并重新加载到 session

        cookies 格式：[{"name": "...", "value": "...", "domain": "...", "path": "/"}, ...]
        """
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_file.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.session.cookies.clear()
        self._load_cookies()
        logger.info("保存 %d 条 cookie 到 %s", len(cookies), self.cookie_file)

    def clear_cookies(self) -> None:
        """清除本地 cookie 与 session"""
        if self.cookie_file.exists():
            self.cookie_file.unlink()
        self.session.cookies.clear()
        logger.info("已清除所有 cookie")

    # ---------- 登录状态 ----------
    def is_logged_in(self) -> bool:
        """检测当前是否已登录（只检测本地 cookie 关键字段，不发网络请求）。

        注意：用 requests 直接请求淘宝页面会触发风控（"访问被拒绝"），
        所以这里只检测 cookie 字段。需要严格验证时调用 verify_cookie()。
        """
        logged = self._has_login_cookie()
        logger.debug("登录状态检测: %s", "已登录" if logged else "未登录")
        return logged

    def verify_cookie(self) -> tuple[bool, str]:
        """实际请求淘宝页面验证 cookie 是否真的有效（严格验证，可能触发风控）。

        返回 (是否有效, 原因说明)。
        注意：用 requests 直接请求淘宝页面有概率触发风控"访问被拒绝"，
        建议只在抓取前等必要场景调用，日常登录状态检测用 is_logged_in()。
        """
        logger.info("开始严格验证 cookie 有效性")
        if not self._has_login_cookie():
            logger.warning("验证失败：缺少登录关键字段")
            return False, "缺少登录关键字段（_tb_token_/cookie2 等）"
        try:
            r = self.session.get(
                "https://h5.m.taobao.com/mlapp/orderlist.htm",
                timeout=8,
                allow_redirects=False,
            )
            logger.debug("验证请求状态码: %d, URL: %s", r.status_code, r.url)
            if r.status_code in (301, 302):
                location = r.headers.get("Location", "")
                if "login" in location.lower():
                    logger.warning("验证失败：cookie 已失效（跳转到登录页）")
                    return False, "cookie 已失效（跳转到登录页）"
                logger.warning("验证失败：页面跳转 %d，可能触发风控", r.status_code)
                return False, f"页面跳转（{r.status_code}），可能触发风控"
            if r.status_code == 200 and "login" not in r.url.lower():
                content = r.text
                if any(kw in content for kw in ["访问被拒绝", "拒绝访问", "亲，访问", "风控", "security"]):
                    logger.warning("验证失败：触发淘宝风控（访问被拒绝）")
                    return False, "触发淘宝风控（访问被拒绝），建议用 QWebEngineView 渲染页面或稍后重试"
                logger.info("验证通过：cookie 有效")
                return True, "cookie 有效"
            logger.warning("验证失败：HTTP %d，可能触发风控或 cookie 已过期", r.status_code)
            return False, f"验证失败（HTTP {r.status_code}），可能触发风控或 cookie 已过期"
        except requests.Timeout:
            logger.warning("验证超时（网络慢或被风控限流）")
            return False, "验证超时（网络慢或被风控限流）"
        except requests.RequestException as e:
            logger.error("验证网络异常: %s", e)
            return False, f"网络异常：{e}"

    def _has_login_cookie(self) -> bool:
        """本地 cookie 是否包含登录关键字段"""
        names = {c.name for c in self.session.cookies}
        unb = self.session.cookies.get("unb", "")
        if unb and str(unb).strip() not in ("", "0"):
            return True
        tracknick = self.session.cookies.get("tracknick", "")
        return bool(str(tracknick).strip())

    # ---------- 信息展示 ----------
    def cookie_summary(self) -> str:
        """返回 cookie 摘要字符串，用于 UI 显示登录状态"""
        if not self._has_login_cookie():
            return "未登录"
        count = len(self.session.cookies)
        unb = self.session.cookies.get("unb", "")
        if unb:
            return f"已登录（{count} 条 cookie）"
        return f"已登录（{count} 条 cookie）"

    # ---------- 商品详情抓取 ----------
    def fetch_item_detail(self, item_id: str, save_dir: Path = None,
                          wait_ms: int = 6000, timeout_ms: int = 30000) -> dict:
        """抓取淘宝商品详情：标题、主图 URL，可选下载主图到 save_dir。

        用 QWebEngineView 渲染 PC 商品详情页（数据由 JS 动态加载，requests 拿不到），
        等页面渲染完成后用 JS 提取标题和图片。

        参数:
            item_id: 商品 ID（从链接 id= 参数提取）
            save_dir: 图片保存目录（None 则不下载，只返回 URL）
            wait_ms: 页面加载完成后额外等待渲染的毫秒数
            timeout_ms: 整体超时时间

        返回:
            {
                "item_id": str,
                "title": str,
                "main_image": str,
                "images": list[str],
                "saved_files": list[str],
                "error": str | None,
            }
        """
        logger.info("===== 开始抓取商品 %s =====", item_id)
        logger.debug("参数: save_dir=%s, wait_ms=%d, timeout_ms=%d", save_dir, wait_ms, timeout_ms)

        result = {
            "item_id": item_id,
            "title": None,
            "main_image": None,
            "images": [],
            "saved_files": [],
            "error": None,
        }

        # 检查登录状态
        if not self._has_login_cookie():
            logger.error("抓取失败：未登录，缺少淘宝 cookie")
            result["error"] = "未登录：缺少淘宝 cookie，请先登录淘宝"
            return result

        url = f"https://item.taobao.com/item.htm?id={item_id}"
        logger.info("目标 URL: %s", url)

        # 复用登录时的持久化浏览器 profile（同目录），cookie/指纹/cache 都在，
        # 不再新建空 profile、不再手动注入 cookie——全新空 profile 会被淘宝风控秒识别
        profile = QWebEngineProfile("taobao-login", None)
        profile.setHttpUserAgent(DESKTOP_UA)
        profile_dir = str(config.DATA_DIR / "taobao_browser_profile")
        profile.setPersistentStoragePath(profile_dir)
        profile.setCachePath(profile_dir + "/cache")
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
        )
        self._inject_stealth(profile)
        logger.info("使用持久化浏览器 profile: %s", profile_dir)

        # 创建浏览器（不显示，后台渲染）
        page = QWebEnginePage(profile, None)
        view = QWebEngineView()
        view.setPage(page)
        view.resize(1280, 900)

        loop = QEventLoop()
        state = {"stage": "home", "finished": False, "ok": False}

        def after_home():
            # 首页暖身完成，进入商品页（模拟真实用户从首页点进商品）
            logger.info("首页暖身完成，进入商品页")
            state["stage"] = "item"
            view.setUrl(QUrl(url))

        def on_load_finished(ok):
            state["ok"] = ok
            if state["stage"] == "home":
                # 首页加载完成，停留 2.5 秒模拟浏览，再进商品页
                logger.info("淘宝首页加载完成: ok=%s，停留 2500ms 后进入商品页", ok)
                QTimer.singleShot(2500, after_home)
                return
            state["finished"] = True
            logger.info("商品页加载完成: ok=%s，等待 %dms 渲染", ok, wait_ms)
            QTimer.singleShot(wait_ms, loop.quit)

        page.loadFinished.connect(on_load_finished)
        QTimer.singleShot(timeout_ms, loop.quit)

        # 先访问淘宝首页暖身，而不是直接打商品页（真实用户路径）
        logger.info("先访问淘宝首页暖身...")
        view.setUrl(QUrl("https://www.taobao.com/"))
        loop.exec()

        if not state["finished"]:
            logger.error("页面加载超时（%dms）", timeout_ms)
            result["error"] = f"页面加载超时（{timeout_ms}ms）"
            return result

        # 检查是否被重定向到登录页
        current_url = page.url().toString()
        logger.debug("最终 URL: %s", current_url)
        if "login.taobao.com" in current_url or ("login" in current_url.lower() and "taobao" in current_url):
            logger.error("被重定向到登录页：cookie 已失效或触发风控")
            result["error"] = "被重定向到登录页：cookie 已失效或触发风控，请重新登录"
            return result

        # 用 JS 提取标题
        logger.debug("执行 JS 提取标题...")
        title_js = """
        (function() {
            var candidates = [
                document.querySelector('h1')?.textContent,
                document.querySelector('[class*="mainTitle"]')?.textContent,
                document.querySelector('[class*="Title"]')?.textContent,
                document.querySelector('.tb-main-title')?.textContent,
                document.title
            ];
            var t = candidates.filter(function(x){ return x && x.trim().length > 5; })[0];
            return t ? t.trim() : document.title;
        })()
        """
        result["title"] = self._run_js_sync(page, title_js)
        logger.info("提取到标题: %s", result["title"])

        # 检测风控拦截页面
        if result["title"] and any(kw in result["title"] for kw in ["访问被拒绝", "拒绝访问", "亲，访问", "风控"]):
            logger.error("触发淘宝风控: %s", result["title"])
            result["error"] = f"触发淘宝风控（{result['title']}），建议稍后重试或降低请求频率"
            result["title"] = None
            return result

        # 用 JS 提取所有图片 URL
        logger.debug("执行 JS 提取图片...")
        imgs_js = r"""
        (function() {
            var imgs = Array.from(document.querySelectorAll('img'));
            var urls = [];
            imgs.forEach(function(img) {
                var u = img.src || img.getAttribute('data-src') || '';
                if (u && (u.indexOf('alicdn') > -1 || u.indexOf('taobaocdn') > -1)) {
                    u = u.replace(/_\d+x\d+q\d+\.(jpg|png|webp)/, '.$1');
                    if (urls.indexOf(u) === -1) urls.push(u);
                }
            });
            return urls;
        })()
        """
        images = self._run_js_sync(page, imgs_js) or []
        # 过滤掉小图标
        result["images"] = [u for u in images if "tps-48-48" not in u and "tps-24-" not in u]
        logger.info("提取到 %d 张图片（过滤小图标后 %d 张）", len(images), len(result["images"]))
        if result["images"]:
            result["main_image"] = result["images"][0]
            logger.debug("主图: %s", result["main_image"])

        # 基本验证
        if not result["title"] or result["title"] == "登录":
            logger.error("未能提取到商品标题，可能页面未完全渲染或触发风控")
            result["error"] = "未能提取到商品标题，可能页面未完全渲染或触发风控"
            return result
        if not result["images"]:
            logger.error("未能提取到商品图片，可能页面未完全渲染或触发风控")
            result["error"] = "未能提取到商品图片，可能页面未完全渲染或触发风控"
            return result

        # 下载主图到指定目录
        if save_dir and result["images"]:
            save_dir = Path(save_dir)
            save_dir.mkdir(parents=True, exist_ok=True)
            logger.info("开始下载前 5 张主图到 %s", save_dir)
            for i, img_url in enumerate(result["images"][:5]):
                ext = "jpg"
                if ".png" in img_url:
                    ext = "png"
                elif ".webp" in img_url:
                    ext = "webp"
                filename = f"{item_id}_{i+1}.{ext}"
                filepath = save_dir / filename
                if self._download_image(img_url, filepath):
                    result["saved_files"].append(str(filepath))
                    logger.info("下载成功 [%d/%d]: %s", i+1, min(5, len(result["images"])), filename)
                else:
                    logger.warning("下载失败 [%d/%d]: %s", i+1, min(5, len(result["images"])), img_url[:80])

        logger.info("===== 抓取完成: %s，标题=%s，图片=%d张，下载=%d个 =====",
                    item_id, result["title"], len(result["images"]), len(result["saved_files"]))
        return result

    @staticmethod
    def _inject_stealth(profile) -> None:
        """注入反指纹脚本：抹掉自动化浏览器特征，降低淘宝风控识别"""
        js = r"""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                {name: 'PDF Viewer'}, {name: 'Chrome PDF Viewer'},
                {name: 'Chromium PDF Viewer'}, {name: 'Microsoft Edge PDF Viewer'}
            ]
        });
        if (!window.chrome) { window.chrome = { runtime: {} }; }
        const gp = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Google Inc. (Intel)';
            if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)';
            return gp.apply(this, [p]);
        };
        """
        s = QWebEngineScript()
        s.setName("stealth")
        s.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        s.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        s.setRunsOnSubFrames(True)
        s.setSourceCode(js)
        profile.scripts().insert(s)

    def _run_js_sync(self, page, js_code: str):
        """同步执行 JavaScript 并返回结果（用 QEventLoop 等待异步回调）"""
        loop = QEventLoop()
        res = {}

        def callback(val):
            res["val"] = val
            loop.quit()

        page.runJavaScript(js_code, callback)
        QTimer.singleShot(10000, loop.quit)
        loop.exec()
        return res.get("val")

    def _download_image(self, url: str, save_path: Path) -> bool:
        """下载图片到本地，带 Referer 头避免淘宝 403"""
        try:
            headers = {
                "User-Agent": DESKTOP_UA,
                "Referer": "https://item.taobao.com/",
            }
            r = self.session.get(url, headers=headers, timeout=15, stream=True)
            if r.status_code == 200:
                with open(save_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                return True
            logger.debug("图片下载 HTTP %d: %s", r.status_code, url[:80])
        except Exception as e:
            logger.debug("图片下载异常: %s", e)
        return False
