"""淘宝登录对话框：内置 QWebEngineView 浏览器，用户扫码登录后真实验证 cookie。

风控应对：
- 设置真实桌面 Chrome UA + 完整 viewport，模拟真实浏览器
- 未登录访客也会下发 _tb_token_/cookie2/sgcookie 等匿名 cookie，不能据此判登录成功；
  真正登录后才有的硬标志是 unb（用户ID，非空数字）和 tracknick（昵称）
- 检测到登录 cookie 后不自动关闭，显示确认栏让用户手动确认
- 用户确认后自动跳转"我的淘宝"做真实验证：未登录会被踢回登录页，
  只有真正加载出登录态页面才发出 cookie 信号、提示登录成功

设计要点：
- 用独立 QWebEngineProfile（"taobao-login"），避免污染默认浏览器配置
- 监听 cookieStore.cookieAdded 收集所有 cookie
- 登录判定：cookie 中出现非空的 unb（或 tracknick），才认为登录成功
- 二次验证：确认后访问 i.taobao.com，未被重定向到登录页才算真成功
"""
from PyQt6.QtCore import QTimer, QUrl, pyqtSignal
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import config
from ..utils.logger import get_logger

logger = get_logger("taobao.login")

# 登录页 URL
LOGIN_URL = "https://login.taobao.com/member/login.jhtml"

# 登录态验证页：未登录会被重定向到登录页，登录后才是"我的淘宝"
VERIFY_URL = "https://i.taobao.com/my_taobao.htm"

# 首页暖身 URL：抓商品前先访问，模拟真实用户浏览路径
HOME_URL = "https://www.taobao.com/"

# 真正登录后才会下发的硬标志 cookie（未登录访客绝不会有）
# - unb: 淘宝用户ID，纯数字
# - tracknick: 用户昵称
LOGGED_IN_COOKIE_KEYS = ("unb", "tracknick")

# 真实桌面 Chrome UA（降低风控识别概率）
DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class TaobaoLoginDialog(QDialog):
    """淘宝登录对话框：内置浏览器扫码登录，真实验证后发出 cookie 列表。

    信号：
        cookies_received(list[dict]): 验证登录成功后发出，
            每个元素是 {"name","value","domain","path"}
    """

    cookies_received = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登录淘宝")
        self.resize(720, 860)
        self.setMinimumSize(640, 780)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部提示
        self.tip = QLabel("请使用淘宝 APP 扫码登录，登录成功后点击下方「确认登录」")
        self.tip.setStyleSheet("padding: 5px 12px; color: #606266; font-size: 12px; background: #F5F7FA;")
        layout.addWidget(self.tip)

        # 浏览器
        self.browser = QWebEngineView()
        self.browser.setZoomFactor(0.8)
        layout.addWidget(self.browser)

        # 底部确认栏（默认隐藏，检测到登录后显示）
        self._confirm_bar = QWidget()
        self._confirm_bar.setStyleSheet("background: #E8F5E9; border-top: 1px solid #A5D6A7;")
        confirm_layout = QHBoxLayout(self._confirm_bar)
        confirm_layout.setContentsMargins(12, 8, 12, 8)
        self._confirm_label = QLabel("✓ 检测到登录，请确认页面已登录后点击按钮完成验证")
        self._confirm_label.setStyleSheet("color: #2E7D32; font-size: 13px;")
        self._confirm_btn = QPushButton("确认登录")
        self._confirm_btn.setStyleSheet(
            "QPushButton { background: #4CAF50; color: white; border: none; "
            "padding: 6px 20px; border-radius: 4px; font-size: 13px; }"
            "QPushButton:hover { background: #43A047; }"
        )
        self._confirm_btn.clicked.connect(self._on_confirm_login)
        confirm_layout.addWidget(self._confirm_label)
        confirm_layout.addStretch()
        confirm_layout.addWidget(self._confirm_btn)
        self._confirm_bar.hide()
        layout.addWidget(self._confirm_bar)

        # 独立 profile，cookie 隔离，设置真实桌面 UA。
        # 关键：把 profile 持久化到磁盘目录，保留浏览器指纹/cache/cookie，
        # 后续抓商品数据复用同一个"真实浏览器"，避免全新 profile 被风控识别
        self._profile = QWebEngineProfile("taobao-login", self)
        self._profile.setHttpUserAgent(DESKTOP_CHROME_UA)
        profile_dir = str(config.DATA_DIR / "taobao_browser_profile")
        self._profile.setPersistentStoragePath(profile_dir)
        self._profile.setCachePath(profile_dir + "/cache")
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
        )
        # 注入反指纹脚本：隐藏自动化标志（navigator.webdriver 等），降低风控识别
        self._inject_stealth_script(self._profile)
        self._cookie_store = self._profile.cookieStore()
        self._cookie_store.cookieAdded.connect(self._on_cookie_added)

        # 用自定义 page 绑定 profile
        self._page = QWebEnginePage(self._profile, self.browser)
        self.browser.setPage(self._page)

        # cookie 收集
        self._cookies: dict[str, dict] = {}
        self._login_detected = False  # 是否已检测到登录（显示确认栏）
        self._verifying = False       # 是否正在验证阶段
        self._finished = False

        # 加载登录页（先清除 profile 里的旧 cookie，强制重新登录）
        self._cookie_store.deleteAllCookies()
        logger.info("登录对话框已打开，清除旧 cookie，加载登录页: %s", LOGIN_URL)
        self.browser.setUrl(QUrl(LOGIN_URL))
        self.browser.urlChanged.connect(self._on_url_changed)

    # ---------- cookie 收集 ----------
    @staticmethod
    def _inject_stealth_script(profile: QWebEngineProfile) -> None:
        """注入反指纹脚本：抹掉 QWebEngine/自动化浏览器的明显特征。

        淘宝风控最基础的检测就是 navigator.webdriver 是否为 true，
        以及 navigator.plugins/languages 是否像真实浏览器。
        脚本在每个页面（含子框架）创建时最先执行。
        """
        js = r"""
        // 1. webdriver 必须为 undefined（关键）
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        // 2. 语言与插件伪装成真实 Chrome
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                {name: 'PDF Viewer'}, {name: 'Chrome PDF Viewer'},
                {name: 'Chromium PDF Viewer'}, {name: 'Microsoft Edge PDF Viewer'}
            ]
        });
        // 3. chrome 对象
        if (!window.chrome) { window.chrome = { runtime: {} }; }
        // 4. WebGL 供应商伪装（降低 canvas/WebGL 指纹异常）
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Google Inc. (Intel)';
            if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)';
            return getParameter.apply(this, [p]);
        };
        """
        script = QWebEngineScript()
        script.setName("stealth")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(js)
        profile.scripts().insert(script)
        logger.debug("已注入反指纹脚本")

    def _on_cookie_added(self, cookie) -> None:
        """收集浏览器中的 cookie，严格检测登录硬标志"""
        try:
            name = bytes(cookie.name()).decode("utf-8", errors="ignore")
            value = bytes(cookie.value()).decode("utf-8", errors="ignore")
        except Exception:
            return
        if not name:
            return
        self._cookies[name] = {
            "name": name,
            "value": value,
            "domain": cookie.domain(),
            "path": cookie.path() if hasattr(cookie, "path") else "/",
        }
        # 严格判定：只有出现非空的 unb（或 tracknick）才算真登录。
        # _tb_token_/cookie2/sgcookie 等未登录访客也有，不能作为判据
        if not self._login_detected and self._has_real_login():
            logger.info("检测到登录硬标志: unb=%s, tracknick=%s，显示确认栏",
                        self._cookies.get("unb", {}).get("value", "")[:8],
                        self._cookies.get("tracknick", {}).get("value", ""))
            self._show_confirm_bar()

    def _has_real_login(self) -> bool:
        """是否真正登录：unb 存在且非空（非 0），或 tracknick 存在"""
        unb = self._cookies.get("unb", {}).get("value", "").strip()
        if unb and unb != "0":
            return True
        tracknick = self._cookies.get("tracknick", {}).get("value", "").strip()
        return bool(tracknick)

    # ---------- 登录成功检测 ----------
    def _on_url_changed(self, url: QUrl) -> None:
        """验证阶段：检测是否被踢回登录页"""
        url_str = url.toString()
        logger.debug("URL 变化: %s", url_str[:120])
        if self._verifying:
            # 验证阶段若被重定向到登录页，说明 cookie 无效
            if "login.taobao.com" in url_str or ("login" in url_str.lower() and "taobao" in url_str):
                logger.warning("验证失败：被重定向到登录页")
                QTimer.singleShot(500, self._verify_failed)
            return
        if self._login_detected:
            return
        # 登录阶段：扫码后淘宝会跳 www.taobao.com 等，辅助判登录
        if "www.taobao.com" in url_str and self._has_real_login():
            logger.info("检测到登录成功跳转: %s", url_str[:120])
            self._show_confirm_bar()

    def _show_confirm_bar(self) -> None:
        """显示底部确认栏，提示用户手动确认登录"""
        if self._login_detected:
            return
        self._login_detected = True
        self._confirm_bar.show()
        logger.info("检测到登录成功，显示确认栏等待用户确认（已收集 %d 条 cookie）", len(self._cookies))

    # ---------- 确认后真实验证 ----------
    def _on_confirm_login(self) -> None:
        """用户点击确认登录：先真实验证，通过后才发出 cookie"""
        if self._finished or self._verifying:
            return
        # 基本验证：必须有真登录 cookie
        if not self._has_real_login():
            logger.warning("用户确认登录，但未检测到有效登录 cookie（unb/tracknick）")
            self._confirm_label.setText("⚠ 未检测到有效登录态，请确认页面已登录后再试")
            self._confirm_label.setStyleSheet("color: #E65100; font-size: 13px;")
            return
        # 进入验证阶段：禁用按钮，访问"我的淘宝"真实验证
        self._verifying = True
        self._confirm_btn.setEnabled(False)
        self._confirm_btn.setText("验证中...")
        self._confirm_label.setText("正在访问「我的淘宝」验证登录状态，请稍候...")
        self.tip.setText("正在验证登录态，请勿关闭窗口...")
        logger.info("开始真实验证：访问 %s", VERIFY_URL)
        # 监听验证页加载完成
        try:
            self._page.loadFinished.disconnect()
        except (TypeError, RuntimeError):
            pass
        self._page.loadFinished.connect(self._on_verify_loaded)
        # 12 秒超时兜底
        QTimer.singleShot(12000, self._verify_timeout)
        self.browser.setUrl(QUrl(VERIFY_URL))

    def _on_verify_loaded(self, ok: bool) -> None:
        """验证页加载完成：等渲染后判定是否被踢回登录页"""
        if not self._verifying or self._finished:
            return
        logger.info("验证页加载完成: ok=%s，等待渲染后判定", ok)
        # 重定向链里会多次触发 loadFinished，每次重置 timer；
        # _on_url_changed 若检测到跳登录页会提前处理
        QTimer.singleShot(2500, self._finish_verify)

    def _verify_timeout(self) -> None:
        """验证超时：按成功处理（能打开且没跳登录就说明 cookie 有效）"""
        if not self._verifying or self._finished:
            return
        logger.info("验证等待超时，按当前 URL 判定")
        self._finish_verify()

    def _finish_verify(self) -> None:
        """验证页加载完成后的判定：没被踢到登录页即通过"""
        if not self._verifying or self._finished:
            return
        url_str = self.browser.url().toString()
        logger.info("验证完成，最终 URL: %s", url_str[:120])
        if "login.taobao.com" in url_str or ("login" in url_str.lower() and "taobao" in url_str):
            self._verify_failed("验证失败：被重定向到登录页，cookie 无效")
            return
        # 真的加载出登录态页面
        cookies = list(self._cookies.values())
        self._finished = True
        self._verifying = False
        unb = self._cookies.get("unb", {}).get("value", "")
        logger.info("真实验证通过，共 %d 条 cookie（unb=%s），发出信号", len(cookies), unb)
        self.tip.setText("✓ 登录验证成功！")
        self.cookies_received.emit(cookies)
        QTimer.singleShot(400, self.accept)

    def _verify_failed(self, reason: str = "验证失败：未真正登录") -> None:
        if self._finished:
            return
        logger.warning(reason)
        self._verifying = False
        self._confirm_btn.setEnabled(True)
        self._confirm_btn.setText("确认登录")
        self._confirm_label.setText(f"⚠ {reason}，请重新扫码登录后再确认")
        self._confirm_label.setStyleSheet("color: #E65100; font-size: 13px;")
        self.tip.setText("请使用淘宝 APP 扫码登录，登录成功后点击下方「确认登录」")
        # 回到登录页
        self.browser.setUrl(QUrl(LOGIN_URL))

    def reject(self) -> None:
        """用户取消：标记完成，避免延迟回调再触发"""
        self._finished = True
        logger.info("用户取消登录（已收集 %d 条 cookie）", len(self._cookies))
        super().reject()
