"""主窗口：组装界面面板并编排业务流程。

只负责界面组织与事件响应，数据读写走 ShopRepository，图片/导出走服务层。
"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
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
        self._all_expanded = True  # 店铺树整体展开状态

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
        left_widget.setObjectName("panel")
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(8)

        # 标题行：标题 + 展开/折叠切换按钮
        title_row = QHBoxLayout()
        left_label = QLabel("店铺列表")
        left_label.setObjectName("title")
        self.btn_toggle_tree = QPushButton("全部折叠")
        self.btn_toggle_tree.setObjectName("treeToggle")
        self.btn_toggle_tree.clicked.connect(self.on_toggle_tree)
        title_row.addWidget(left_label)
        title_row.addStretch()
        title_row.addWidget(self.btn_toggle_tree)
        left_layout.addLayout(title_row)

        self.shop_tree = QTreeWidget()
        self.shop_tree.setHeaderLabels(["店铺名称"])
        # 关闭 Qt 默认双击展开，由 on_shop_double_clicked 统一控制，避免双重切换抵消
        self.shop_tree.setExpandsOnDoubleClick(False)
        self.shop_tree.itemClicked.connect(self.on_shop_selected)
        self.shop_tree.itemDoubleClicked.connect(self.on_shop_double_clicked)
        # 右键菜单：店铺仅支持重命名
        self.shop_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.shop_tree.customContextMenuRequested.connect(self._on_shop_tree_menu)
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
        right_widget.setObjectName("panel")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(8)

        self.current_shop_label = QLabel("请选择左侧店铺")
        self.current_shop_label.setObjectName("title")
        right_layout.addWidget(self.current_shop_label)

        self.table = RecordTable()
        self.table.image_paste_requested.connect(self.on_paste_image_requested)
        self.table.edit_requested.connect(self.on_edit_record)
        self.table.delete_requested.connect(self.on_delete_record)
        right_layout.addWidget(self.table)

        tip_label = QLabel("提示：双击图片占位单元格可直接粘贴截图/图片")
        tip_label.setObjectName("tip")
        right_layout.addWidget(tip_label)

        # 底部操作栏
        bottom_layout = QHBoxLayout()
        self.btn_add = QPushButton("新增记录")
        self.btn_edit = QPushButton("修改记录")
        self.btn_delete = QPushButton("删除选中行")
        self.btn_export = QPushButton("导出Excel")
        # 主操作按钮样式
        self.btn_add.setProperty("primary", True)
        self.btn_export.setProperty("primary", True)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("按标题搜索...")
        self.btn_search = QPushButton("搜索")
        self.btn_clear_search = QPushButton("清除搜索")

        bottom_layout.addWidget(self.btn_add)
        bottom_layout.addWidget(self.btn_edit)
        bottom_layout.addWidget(self.btn_delete)
        bottom_layout.addStretch()
        bottom_layout.addWidget(QLabel("搜索:"))
        bottom_layout.addWidget(self.search_input)
        bottom_layout.addWidget(self.btn_search)
        bottom_layout.addWidget(self.btn_clear_search)
        bottom_layout.addWidget(self.btn_export)
        right_layout.addLayout(bottom_layout)

        self.btn_add.clicked.connect(self.on_add_record)
        self.btn_edit.clicked.connect(self.on_edit_record)
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

    def _on_shop_tree_menu(self, pos):
        """店铺树右键菜单：仅支持修改名字"""
        item = self.shop_tree.itemAt(pos)
        if item is None:
            return
        self.shop_tree.setCurrentItem(item)
        menu = QMenu(self)
        act_rename = menu.addAction("重命名")
        chosen = menu.exec(self.shop_tree.viewport().mapToGlobal(pos))
        if chosen == act_rename:
            self.on_rename_shop()

    def refresh_shop_tree(self):
        self.shop_tree.clear()
        for shop_name, records in self.repo.shops.items():
            shop_item = QTreeWidgetItem([shop_name])
            # 店名不可在树上直接编辑（改名统一走重命名，保证能保存）
            shop_item.setFlags(shop_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.shop_tree.addTopLevelItem(shop_item)
            count_item = QTreeWidgetItem([f"素材数量：{len(records)}"])
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            count_item.setForeground(0, QBrush(QColor("#909399")))
            shop_item.addChild(count_item)
        if self._all_expanded:
            self.shop_tree.expandAll()
        else:
            self.shop_tree.collapseAll()

    def on_toggle_tree(self):
        """一键展开/折叠全部店铺"""
        self._all_expanded = not self._all_expanded
        if self._all_expanded:
            self.shop_tree.expandAll()
            self.btn_toggle_tree.setText("全部折叠")
        else:
            self.shop_tree.collapseAll()
            self.btn_toggle_tree.setText("全部展开")

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

    def on_delete_record(self, row=None):
        """删除记录：row 为 None 时取当前选中行（按钮），否则为右键菜单指定的行"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        if row is None:
            row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中要删除的行")
            return
        reply = QMessageBox.question(
            self, "确认删除", "确定要删除选中的记录吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        record_index = self.table.rendered_index(row)
        self.repo.remove_record(self.current_shop, record_index)
        self.refresh_table()
        self.refresh_shop_tree()
        self.save_data()

    def on_edit_record(self, row=None):
        """修改记录：row 为 None 时取当前选中行（按钮），否则为右键菜单指定的行"""
        if not self.current_shop:
            QMessageBox.information(self, "提示", "请先选择店铺")
            return
        if row is None:
            row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中要修改的行")
            return
        record_index = self.table.rendered_index(row)
        old_record = self.repo.get_records(self.current_shop)[record_index]

        dialog = AddRecordDialog(self, record=old_record)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_record = dialog.get_data()
        if not new_record:
            return
        self.repo.shops[self.current_shop][record_index] = new_record
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
        record_index = self.table.rendered_index(row)
        # 评价图片（多图）追加，规格图/链接主图（单图）替换
        if field_name in config.MULTI_IMAGE_FIELDS:
            self.repo.append_record_image(self.current_shop, record_index, filepath)
        else:
            self.repo.set_record_field(self.current_shop, record_index, field_name, filepath)
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
        matched_indices = [i for i, r in enumerate(records) if keyword in r.get("title", "").lower()]
        matched = [records[i] for i in matched_indices]
        self.table.render(matched, matched_indices)

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
