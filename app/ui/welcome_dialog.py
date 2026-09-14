"""启动欢迎弹窗：横版左右分栏，美观的版本介绍 + 操作指南。

继承 AnimatedDialog，自带可拖动 + 弹出/关闭动画。
"""
from PyQt6.QtCore import Qt, QSettings, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QFont, QPainter, QLinearGradient, QColor, QBrush
from PyQt6.QtWidgets import (
    QLabel, QPushButton, QCheckBox, QWidget, QScrollArea,
    QFrame, QSizePolicy, QHBoxLayout, QVBoxLayout,
)

from .animated_dialog import AnimatedDialog
from .. import config

ORG_NAME = "TaobaoMaterialTool"
APP_NAME = "TaobaoMaterialTool"
SETTING_KEY = "welcome/show_on_startup"


def should_show_welcome() -> bool:
    """检查是否需要显示启动弹窗（用户勾选不再提醒后返回 False）"""
    settings = QSettings(ORG_NAME, APP_NAME)
    return settings.value(SETTING_KEY, True, type=bool)


def set_show_welcome(show: bool) -> None:
    """设置是否显示启动弹窗"""
    settings = QSettings(ORG_NAME, APP_NAME)
    settings.setValue(SETTING_KEY, show)


class _GradientSidebar(QWidget):
    """左侧渐变侧边栏：蓝色渐变 + 大标题 + 版本号"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(260)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 蓝色渐变（左上到右下，更有层次感）
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor("#1E6FFF"))
        gradient.setColorAt(0.5, QColor("#389BFF"))
        gradient.setColorAt(1, QColor("#6BB5FF"))
        painter.fillRect(self.rect(), QBrush(gradient))

        # 装饰：右下角半透明圆形（增加设计感）
        painter.setBrush(QBrush(QColor(255, 255, 255, 25)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(self.width() - 80, self.height() - 80, 160, 160)
        painter.drawEllipse(self.width() - 120, self.height() - 120, 100, 100)
        painter.end()


class WelcomeDialog(AnimatedDialog):
    """启动欢迎弹窗（横版左右分栏）"""

    # 覆盖动画参数（更细腻）
    POP_DURATION = 350
    CLOSE_DURATION = 220
    POP_SCALE_START = 0.90
    CLOSE_SCALE_END = 0.94

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(820, 760)
        self.setWindowTitle(f"欢迎使用 {config.APP_TITLE}")

    def _build_content(self):
        """构建内容：左右分栏"""
        # 主容器：左右分栏
        main = QHBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # ===== 左侧渐变侧边栏 =====
        sidebar = _GradientSidebar()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(28, 36, 28, 28)
        sidebar_layout.setSpacing(0)

        # 顶部装饰图标（大圆点 + 白色符号）
        icon_widget = QWidget()
        icon_widget.setFixedSize(72, 72)
        icon_widget.setStyleSheet("""
            QWidget {
                background: rgba(255,255,255,0.2);
                border-radius: 36px;
                border: 2px solid rgba(255,255,255,0.4);
            }
        """)
        icon_layout = QVBoxLayout(icon_widget)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        icon_label = QLabel("🛒")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("font-size: 32px; background: transparent;")
        icon_layout.addWidget(icon_label)
        sidebar_layout.addWidget(icon_widget)
        sidebar_layout.addSpacing(20)

        # 大标题
        title = QLabel("淘宝评价\n素材整理工具")
        title.setStyleSheet("""
            color: white;
            font-size: 26px;
            font-weight: bold;
            line-height: 1.3;
            background: transparent;
        """)
        title.setAlignment(Qt.AlignmentFlag.AlignLeft)
        sidebar_layout.addWidget(title)
        sidebar_layout.addSpacing(12)

        # 副标题
        subtitle = QLabel("高效管理你的评价素材")
        subtitle.setStyleSheet("""
            color: rgba(255,255,255,0.8);
            font-size: 14px;
            background: transparent;
        """)
        sidebar_layout.addWidget(subtitle)

        sidebar_layout.addStretch()

        # 底部版本号
        version_box = QFrame()
        version_box.setStyleSheet("background: transparent;")
        version_layout = QVBoxLayout(version_box)
        version_layout.setContentsMargins(0, 0, 0, 0)
        version_layout.setSpacing(2)

        ver_name = QLabel(config.APP_VERSION_NAME)
        ver_name.setStyleSheet("color: white; font-size: 16px; font-weight: bold; background: transparent;")
        version_layout.addWidget(ver_name)

        ver_code = QLabel(f"Version {config.APP_VERSION}")
        ver_code.setStyleSheet("color: rgba(255,255,255,0.7); font-size: 12px; background: transparent;")
        version_layout.addWidget(ver_code)

        sidebar_layout.addWidget(version_box)

        main.addWidget(sidebar)

        # ===== 右侧内容区 =====
        right = QWidget()
        right.setStyleSheet("background: #FFFFFF;")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(28, 24, 28, 0)
        right_layout.setSpacing(0)

        # 标题
        self._right_title = QLabel("v1.1 更新计划 🎉")
        self._right_title.setStyleSheet("color: #303133; font-size: 20px; font-weight: bold;")
        right_layout.addWidget(self._right_title)
        right_layout.addSpacing(4)

        self._desc = QLabel("本次版本新增与调整内容")
        self._desc.setStyleSheet("color: #909399; font-size: 13px;")
        right_layout.addWidget(self._desc)
        right_layout.addSpacing(16)

        from PyQt6.QtWidgets import QStackedWidget
        self._stack = QStackedWidget()

        # --- 第 1 页：更新计划 ---
        page1 = QWidget()
        p1_lay = QVBoxLayout(page1)
        p1_lay.setContentsMargins(0, 0, 8, 0)
        p1_lay.setSpacing(14)
        p1_lay.addWidget(self._section_title("🆕 v1.1 新功能"))
        updates = [
            ("批量抓取", "一次粘贴多段分享口令/多个链接，自动识别逐个抓取，一键填充全部"),
            ("编辑菜单", "左上角「编辑 ▾」：选择批量模式 / 新增 / 修改 / 复制 / 删除商品"),
            ("规格下拉", "规格列改为格子内下拉选择，选项自动汇总当前店铺已有规格"),
            ("新建店铺", "店铺列表底部新增蓝色「+ 新建店铺」按钮，一键创建"),
            ("导入迁移", "一键导入移到「商品链接」表头右键：获取所有商品信息"),
            ("大图右键", "查看大图时右键可复制图片或删除当前图（翻页也能删）"),
            ("Edge 支持", "登录可选 Chrome 或 Edge，两浏览器独立 profile 互不干扰"),
            ("空表加号", "空店铺/空分类时加号按钮居中显示在表格内，不再消失"),
        ]
        for name, desc_text in updates:
            p1_lay.addWidget(self._feature_item(name, desc_text))
        p1_lay.addStretch()
        self._stack.addWidget(page1)

        # --- 第 2 页：使用介绍 ---
        page2 = QWidget()
        p2_lay = QVBoxLayout(page2)
        p2_lay.setContentsMargins(0, 0, 8, 0)
        p2_lay.setSpacing(14)
        p2_lay.addWidget(self._section_title("✨ 主要功能"))
        features = [
            ("店铺管理", "左侧店铺列表，底部蓝色按钮新建，支持删除/重命名/拖拽排序/自定义颜色"),
            ("素材记录", "表格化管理商品素材，双击文本直接编辑，规格列下拉选择"),
            ("图片操作", "规格图/链接主图/评价图片分离，支持粘贴/复制/删除/多图展示"),
            ("淘宝登录", "Playwright 驱动本机 Chrome 或 Edge，登录对话框可下拉切换"),
            ("商品抓取", "粘贴多段文本自动识别多个链接，批量抓取后一键填充"),
            ("一键导入", "右键「商品链接」表头 → 获取所有商品信息，批量填入"),
        ]
        for name, desc_text in features:
            p2_lay.addWidget(self._feature_item(name, desc_text))
        p2_lay.addSpacing(4)
        p2_lay.addWidget(self._section_title("⌨️ 快捷操作"))
        shortcuts = [
            "双击表格文本 → 直接编辑；规格列下拉选择",
            "图片列 Ctrl+V 粘贴 / Ctrl+C 复制 / Del 删除",
            "双击缩略图看大图，大图右键复制/删除当前图",
            "右键商品链接 → 抓取此链接 / 抓取并填充到此行",
            "右键「商品链接」表头 → 获取所有商品信息",
            "底部「编辑 ▾」→ 选择（进入批量勾选）/ 新增 / 修改 / 复制 / 删除",
            "空表单点表格中间加号即可新增记录",
        ]
        for sc in shortcuts:
            p2_lay.addWidget(self._shortcut_item(sc))
        p2_lay.addStretch()
        self._stack.addWidget(page2)

        right_layout.addWidget(self._stack, 1)

        # ===== 底部操作栏 =====
        footer = QFrame()
        footer.setStyleSheet("background: #FAFBFC; border-top: 1px solid #EBEEF5;")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(28, 14, 28, 16)
        footer_layout.setSpacing(12)

        self.dont_show_again = QCheckBox("不再提醒")
        self.dont_show_again.setStyleSheet("""
            QCheckBox { color: #606266; font-size: 13px; spacing: 6px; }
            QCheckBox::indicator {
                width: 16px; height: 16px; border-radius: 4px;
                border: 1.5px solid #C0C4CC; background: white;
            }
            QCheckBox::indicator:checked {
                background: #389BFF; border: 1.5px solid #389BFF;
            }
        """)
        footer_layout.addWidget(self.dont_show_again)
        footer_layout.addStretch()

        self._next_btn = QPushButton("下一步")
        self._next_btn.setFixedSize(120, 40)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #5BA8FF, stop:1 #389BFF);
                color: white;
                font-size: 14px;
                font-weight: bold;
                border-radius: 8px;
                border: none;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #6BB5FF, stop:1 #48A8FF);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2E8AE6, stop:1 #1E7AD6);
            }
        """)
        self._next_btn.clicked.connect(self._on_next_page)
        footer_layout.addWidget(self._next_btn)

        right_layout.addWidget(footer)
        main.addWidget(right, 1)

        self.content_layout.addLayout(main)

    def _section_title(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("color: #303133; font-size: 15px; font-weight: bold;")
        return label

    def _feature_item(self, name: str, desc: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # 蓝色小圆点
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("""
            background: #389BFF;
            border-radius: 4px;
            margin-top: 5px;
        """)
        lay.addWidget(dot)

        text = QLabel(
            f"<b style='color:#303133; font-size:13px;'>{name}</b>"
            f"<span style='color:#909399; font-size:12px;'>　{desc}</span>"
        )
        text.setWordWrap(True)
        text.setStyleSheet("line-height: 1.6;")
        lay.addWidget(text, 1)

        return w

    def _shortcut_item(self, text: str) -> QLabel:
        label = QLabel(f"  {text}")
        label.setStyleSheet("""
            color: #606266;
            font-size: 12.5px;
            background: #F5F7FA;
            border-radius: 6px;
            padding: 7px 12px;
        """)
        return label

    def _animate_pop(self):
        """只做透明度淡入，不做 geometry 缩放（避免和 fixed size 冲突卡死）"""
        self._opacity_anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._opacity_anim.setDuration(280)
        self._opacity_anim.setStartValue(0.0)
        self._opacity_anim.setEndValue(1.0)
        self._opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._opacity_anim.start()

    def close_with_animation(self):
        """只做透明度淡出"""
        if getattr(self, "_is_closing", False):
            return
        self._is_closing = True
        self._close_opacity_anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._close_opacity_anim.setDuration(180)
        self._close_opacity_anim.setStartValue(1.0)
        self._close_opacity_anim.setEndValue(0.0)
        self._close_opacity_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._close_opacity_anim.finished.connect(self.done)
        self._close_opacity_anim.start()

    def _on_next_page(self):
        """下一步：第1页→第2页，第2页→关闭"""
        idx = self._stack.currentIndex()
        if idx == 0:
            self._stack.setCurrentIndex(1)
            self._right_title.setText("使用介绍 📖")
            self._desc.setText("主要功能与快捷操作，助你快速上手")
            self._next_btn.setText("开始使用")
        else:
            self._on_close()

    def _on_close(self):
        """关闭按钮：保存不再提醒设置 + 播放关闭动画"""
        if self.dont_show_again.isChecked():
            set_show_welcome(False)
        self._on_close_animated()

    def _on_close_animated(self):
        """触发关闭动画（基类方法）"""
        self.close_with_animation()
