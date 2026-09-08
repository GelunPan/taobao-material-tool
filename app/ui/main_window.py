"""主窗口：组装界面面板并编排业务流程。

只负责界面组织与事件响应，数据读写走 ShopRepository，图片/导出走服务层。
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..services import ExcelExporter, ImageService
from ..storage import DataLoadError, ShopRepository
from .add_dialog import AddRecordDialog
from .record_table import RecordTable


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(config.APP_TITLE)
        self.setGeometry(100, 100, 1400, 700)

        # 数据层
        self.repo = ShopRepository(config.DATA_FILE, config.IMAGES_DIR)
        self.current_shop = None

        self.init_ui()
        self.load_data()

    # ==================== 界面搭建 ====================
    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        splitter.addWidget(self._build_shop_panel())
        splitter.addWidget(self._build_record_panel())
        splitter.setSizes([250, 1150])

    def _build_shop_panel(self):
        """左侧：店铺列表面板"""
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(5, 5, 5, 5)

        left_label = QLabel("店铺列表")
        left_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        left_layout.addWidget(left_label)

        self.shop_tree = QTreeWidget()
        self.shop_tree.setHeaderLabels(["店铺名称"])
        self.shop_tree.setAlternatingRowColors(True)
        self.shop_tree.itemClicked.connect(self.on_shop_selected)
        self.shop_tree.itemDoubleClicked.connect(self.on_shop_double_clicked)
        left_layout.addWidget(self.shop_tree)

        shop_btn_layout = QHBoxLayout()
        self.btn_add_shop = QPushButton("+ 新店铺")
        self.btn_rename_shop = QPushButton("重命名")
        self.btn_delete_shop = QPushButton("删除店铺")
        shop_btn_layout.addWidget(self.btn_add_shop)
        shop_btn_layout.addWidget(self.btn_rename_shop)
        shop_btn_layout.addWidget(self.btn_delete_shop)
        left_layout.addLayout(shop_btn_layout)

        self.btn_add_shop.clicked.connect(self.on_add_shop)
        self.btn_rename_shop.clicked.connect(self.on_rename_shop)
        self.btn_delete_shop.clicked.connect(self.on_delete_shop)

        return left_widget

    def _build_record_panel(self):
        """右侧：素材表格面板"""
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(5, 5, 5, 5)

        self.current_shop_label = QLabel("请选择左侧店铺")
        self.current_shop_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #333;")
        right_layout.addWidget(self.current_shop_label)

        self.table = RecordTable()
        self.table.image_paste_requested.connect(self.on_paste_image_requested)
        right_layout.addWidget(self.table)

        tip_label = QLabel("提示：在图片单元格中按 Ctrl+V 可直接粘贴截图/图片")
        tip_label.setStyleSheet("color: #888; font-size: 12px;")
        right_layout.addWidget(tip_label)

        # 底部操作栏
        bottom_layout = QHBoxLayout()
        self.btn_add = QPushButton("新增记录")
        self.btn_delete = QPushButton("删除选中行")
        self.btn_export = QPushButton("导出Excel")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("按标题搜索...")
        self.btn_search = QPushButton("搜索")
        self.btn_clear_search = QPushButton("清除搜索")

        bottom_layout.addWidget(self.btn_add)
        bottom_layout.addWidget(self.btn_delete)
        bottom_layout.addStretch()
        bottom_layout.addWidget(QLabel("搜索:"))
        bottom_layout.addWidget(self.search_input)
        bottom_layout.addWidget(self.btn_search)
        bottom_layout.addWidget(self.btn_clear_search)
        bottom_layout.addWidget(self.btn_export)
        right_layout.addLayout(bottom_layout)

        self.btn_add.clicked.connect(self.on_add_record)
        self.btn_delete.clicked.connect(self.on_delete_record)
        self.btn_export.clicked.connect(self.on_export)
        self.btn_search.clicked.connect(self.on_search)
        self.btn_clear_search.clicked.connect(self.on_clear_search)
        self.search_input.returnPressed.connect(self.on_search)

        return right_widget

    # ==================== 数据加载与保存 ====================
    def load_data(self):
        try:
            self.repo.load()
        except DataLoadError as e:
            QMessageBox.warning(self, "警告", f"{e}\n将使用空数据。")
            self.repo.shops = {}
        self.refresh_shop_tree()

    def save_data(self):
        try:
            self.repo.save()
        except Exception as e:
            QMessageBox.warning(self, "警告", f"数据保存失败：{str(e)}")

    # ==================== 店铺管理 ====================
    def on_add_shop(self):
        name, ok = QInputDialog.getText(self, "添加店铺", "请输入店铺名称:")
        if not (ok and name.strip()):
            return
        name = name.strip()
        if self.repo.shop_exists(name):
            QMessageBox.warning(self, "提示", "该店铺已存在！")
            return
        self.repo.add_shop(name)
        self.refresh_shop_tree()
        self.save_data()
        self.select_shop_in_tree(name)

    def on_rename_shop(self):
        item = self.shop_tree.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择要重命名的店铺")
            return
        old_name = item.text(0)
        new_name, ok = QInputDialog.getText(self, "重命名店铺", "请输入新名称:", text=old_name)
        if not (ok and new_name.strip() and new_name.strip() != old_name):
            return
        new_name = new_name.strip()
        if self.repo.shop_exists(new_name):
            QMessageBox.warning(self, "提示", "该店铺名已存在！")
            return
        self.repo.rename_shop(old_name, new_name)
        if self.current_shop == old_name:
            self.current_shop = new_name
            self.current_shop_label.setText(f"当前店铺：{new_name}")
        self.refresh_shop_tree()
        self.save_data()

    def on_delete_shop(self):
        item = self.shop_tree.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择要删除的店铺")
            return
        name = item.text(0)
        reply = QMessageBox.question(
            self, "确认删除", f"确定要删除店铺 '{name}' 及其所有素材吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.repo.delete_shop(name)
        if self.current_shop == name:
            self.current_shop = None
            self.current_shop_label.setText("请选择左侧店铺")
            self.table.render([])
        self.refresh_shop_tree()
        self.save_data()

    def on_shop_selected(self, item, column):
        self.current_shop = item.text(0) if item.parent() is None else item.parent().text(0)
        self.current_shop_label.setText(f"当前店铺：{self.current_shop}")
        self.refresh_table()

    def on_shop_double_clicked(self, item, column):
        if item.childCount() > 0:
            item.setExpanded(not item.isExpanded())

    def refresh_shop_tree(self):
        self.shop_tree.clear()
        for shop_name, records in self.repo.shops.items():
            shop_item = QTreeWidgetItem([shop_name])
            shop_item.setFlags(shop_item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.shop_tree.addTopLevelItem(shop_item)
            count_item = QTreeWidgetItem([f"素材数量：{len(records)}"])
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            shop_item.addChild(count_item)
        self.shop_tree.expandAll()

    def select_shop_in_tree(self, name):
        for i in range(self.shop_tree.topLevelItemCount()):
            item = self.shop_tree.topLevelItem(i)
            if item.text(0) == name:
                self.shop_tree.setCurrentItem(item)
                self.on_shop_selected(item, 0)
                break

    # ==================== 素材记录管理 ====================
    def refresh_table(self):
        """按当前店铺重新渲染表格"""
        records = self.repo.get_records(self.current_shop) if self.current_shop else []
        self.table.render(records)

    def on_add_record(self):
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先在左侧选择或创建一个店铺")
            return
        dialog = AddRecordDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        record = dialog.get_data()
        if not record:
            return
        self.repo.add_record(self.current_shop, record)
        self.refresh_table()
        self.refresh_shop_tree()
        self.save_data()

    def on_delete_record(self):
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        current_row = self.table.currentRow()
        if current_row < 0:
            QMessageBox.information(self, "提示", "请先选中要删除的行")
            return
        reply = QMessageBox.question(
            self, "确认删除", "确定要删除选中的记录吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.repo.remove_record(self.current_shop, current_row)
        self.refresh_table()
        self.refresh_shop_tree()
        self.save_data()

    def on_paste_image_requested(self, row, col, field_name):
        """处理表格中的粘贴图片请求：保存图片并更新记录"""
        filepath, new_counter = ImageService.save_clipboard_image(
            QApplication.clipboard(), self.repo.images_dir, self.repo.image_counter
        )
        if not filepath:
            QMessageBox.information(self, "提示", "剪贴板中没有图片，请先截图或复制图片后按Ctrl+V")
            return
        self.repo.image_counter = new_counter
        self.repo.set_record_image(self.current_shop, row, field_name, filepath)
        self.refresh_table()
        self.save_data()
        QMessageBox.information(self, "成功", "图片已粘贴！")

    # ==================== 搜索 ====================
    def on_search(self):
        keyword = self.search_input.text().strip().lower()
        if not keyword or not self.current_shop:
            if self.current_shop:
                self.refresh_table()
            return
        records = self.repo.get_records(self.current_shop)
        matched = [r for r in records if keyword in r.get("title", "").lower()]
        self.table.render(matched)

    def on_clear_search(self):
        self.search_input.clear()
        if self.current_shop:
            self.refresh_table()

    # ==================== 导出 ====================
    def on_export(self):
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出Excel", f"{self.current_shop}_素材导出.xlsx", "Excel文件 (*.xlsx)"
        )
        if not file_path:
            return
        try:
            records = self.repo.get_records(self.current_shop)
            ExcelExporter.export(records, file_path, sheet_name=self.current_shop)
            QMessageBox.information(self, "成功", f"导出成功！\n文件保存在：{file_path}")
        except ImportError:
            QMessageBox.warning(self, "错误", "openpyxl库未安装，请运行: pip install openpyxl")
        except Exception as e:
            QMessageBox.warning(self, "错误", f"导出失败：{str(e)}")
