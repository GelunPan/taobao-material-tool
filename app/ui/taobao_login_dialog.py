"""淘宝登录对话框：内置 QWebEngineView 浏览器，用户扫码登录后手动确认提取 cookie。

风控应对：
- 设置真实桌面 Chrome UA，避免 QWebEngine 默认 UA 被识别
- 检测到登录后不自动关闭，显示确认栏让用户手动确认，避免过快操作触发风控
- 用户确认后才发出 cookie 信号，给足时间让页面完全加载、cookie 稳定

设计要点：
- 用独立 QWebEngineProfile（"taobao-login"），避免污染默认浏览器配置
- 监听 cookieStore.cookieAdded 收集所有 cookie
- 登录成功双检测：①URL 跳转到淘宝首页/个人中心；②cookie 中出现登录关键字段
- 检测到登录后显示底部确认栏，用户点击"确认登录"后才关闭
"""
from PyQt6.QtCore import QTimer, QUrl, pyqtSignal
from PyQt6.QtWebEngineCore import QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..utils.logger import get_logger

logger = get_logger("taobao.login")


# 登录成功的标志：URL 跳转到这些域名
LOGIN_SUCCESS_HOSTS = (
    "www.taobao.com",
    "h5.m.taobao.com",
    "my.taobao.com",
    "main.m.taobao.com",
)

# 登录成功的关键 cookie 名（出现这些基本说明已登录）
LOGIN_COOKIE_KEYS = ("_tb_token_", "cookie2", "sgcookie", "unb")

# 登录页 URL
LOGIN_URL = "https://login.taobao.com/"

# 真实桌面 Chrome UA（降低风控识别概率）
DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class TaobaoLoginDialog(QDialog):
    """淘宝登录对话框：内置浏览器扫码登录，用户确认后发出 cookie 列表。

    信号：
        cookies_received(list[dict]): 用户确认登录后发出，每个元素是 {"name","value","domain","path"}
    """

    cookies_received = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登录淘宝")
        # 淘宝登录页是桌面端左右布局（二维码+密码登录），默认给足尺寸确保二维码完整显示
        self.resize(720, 860)
        self.setMinimumSize(640, 780)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部提示
        tip = QLabel("请使用淘宝 APP 扫码登录，登录成功后点击下方「确认登录」按钮完成")
        tip.setStyleSheet("padding: 5px 12px; color: #606266; font-size: 12px; background: #F5F7FA;")
        layout.addWidget(tip)

        # 浏览器
        self.browser = QWebEngineView()
        # 页面缩小到 80%，确保二维码和登录表单完整显示在对话框内
        self.browser.setZoomFactor(0.8)
        layout.addWidget(self.browser)

        # 底部确认栏（默认隐藏，检测到登录后显示）
        self._confirm_bar = QWidget()
        self._confirm_bar.setStyleSheet("background: #E8F5E9; border-top: 1px solid #A5D6A7;")
        confirm_layout = QHBoxLayout(self._confirm_bar)
        confirm_layout.setContentsMargins(12, 8, 12, 8)
        self._confirm_label = QLabel("✓ 检测到登录成功，请确认页面已登录后点击按钮完成")
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

        # 独立 profile，cookie 隔离，设置真实 UA
        self._profile = QWebEngineProfile("taobao-login", self)
        self._profile.setHttpUserAgent(DESKTOP_CHROME_UA)
        self._cookie_store = self._profile.cookieStore()
        self._cookie_store.cookieAdded.connect(self._on_cookie_added)

        # 用自定义 page 绑定 profile
        from PyQt6.QtWebEngineCore import QWebEnginePage
        self._page = QWebEnginePage(self._profile, self.browser)
        self.browser.setPage(self._page)

        # cookie 收集
        self._cookies: dict[str, dict] = {}
        self._login_detected = False  # 是否已检测到登录（显示确认栏）
        self._finished = False

        # 加载登录页（先清除 profile 里的旧 cookie，强制重新登录）
        self._cookie_store.deleteAllCookies()
        logger.info("登录对话框已打开，清除旧 cookie，加载登录页: %s", LOGIN_URL)
        self.browser.setUrl(QUrl(LOGIN_URL))
        self.browser.urlChanged.connect(self._on_url_changed)

    # ---------- cookie 收集 ----------
    def _on_cookie_added(self, cookie) -> None:
        """收集浏览器中的 cookie，同时检测登录关键字段"""
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
        # 检测到登录关键字段，显示确认栏（不自动关闭，让用户手动确认）
        if not self._login_detected and name in LOGIN_COOKIE_KEYS:
            logger.info("检测到登录关键字段: %s，显示确认栏", name)
            self._show_confirm_bar()

    # ---------- 登录成功检测 ----------
    def _on_url_changed(self, url: QUrl) -> None:
        """URL 变化时检测是否登录成功（辅助检测）"""
        if self._login_detected:
            return
        url_str = url.toString()
        logger.debug("URL 变化: %s", url_str[:120])
        if any(host in url_str for host in LOGIN_SUCCESS_HOSTS):
            # 跳到了淘宝首页/个人中心，显示确认栏
            logger.info("检测到登录成功跳转: %s", url_str[:120])
            self._show_confirm_bar()

    def _show_confirm_bar(self) -> None:
        """显示底部确认栏，提示用户手动确认登录"""
        if self._login_detected:
            return
        self._login_detected = True
        self._confirm_bar.show()
        # 调整对话框高度给确认栏留空间
        self.resize(self.width(), self.height() + 40)
        logger.info("检测到登录成功，显示确认栏等待用户确认（已收集 %d 条 cookie）", len(self._cookies))

    def _on_confirm_login(self) -> None:
        """用户点击确认登录：发出 cookie 信号并关闭"""
        if self._finished:
            return
        cookies = list(self._cookies.values())
        # 基本验证：至少有一条 cookie，且包含登录关键字段
        if not cookies or not any(c["name"] in LOGIN_COOKIE_KEYS for c in cookies):
            logger.warning("用户确认登录，但未检测到有效登录 cookie（共 %d 条）", len(cookies))
            self._confirm_label.setText("⚠ 未检测到有效登录 cookie，请确认页面已登录后再试")
            self._confirm_label.setStyleSheet("color: #E65100; font-size: 13px;")
            return
        self._finished = True
        logger.info("用户确认登录成功，共 %d 条 cookie，发出信号", len(cookies))
        self.cookies_received.emit(cookies)
        self.accept()

    def reject(self) -> None:
        """用户取消：标记完成，避免延迟回调再触发"""
        self._finished = True
        logger.info("用户取消登录（已收集 %d 条 cookie）", len(self._cookies))
        super().reject()
