"""淘宝评价素材整理工具 - 程序入口"""
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from app.ui.main_window import MainWindow

STYLE_FILE = Path(__file__).parent / "app" / "style.qss"


def main():
    app = QApplication(sys.argv)
    if STYLE_FILE.exists():
        app.setStyleSheet(STYLE_FILE.read_text(encoding="utf-8"))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
