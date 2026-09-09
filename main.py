"""淘宝评价素材整理工具 - 程序入口"""
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from app.config import ASSETS_DIR
from app.ui.main_window import MainWindow

STYLE_FILE = Path(__file__).parent / "app" / "style.qss"


def load_stylesheet() -> str:
    """读取全局 QSS，并把素材占位符替换为绝对路径（url() 不随工作目录变化）"""
    qss = STYLE_FILE.read_text(encoding="utf-8")
    return qss.replace("@ASSETS@", ASSETS_DIR.as_posix())


def main():
    app = QApplication(sys.argv)
    if STYLE_FILE.exists():
        app.setStyleSheet(load_stylesheet())
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
