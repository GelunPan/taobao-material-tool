"""新增素材记录弹窗"""
import os

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)


class AddRecordDialog(QDialog):
    """新增素材记录弹窗：录入文本字段并选择三张图片"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新增素材记录")
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)

        # ---------- 文本字段 ----------
        form_layout = QFormLayout()
        self.product_id_input = QLineEdit()
        self.spec_input = QLineEdit()
        self.title_input = QLineEdit()
        self.helper_input = QLineEdit()
        self.review_input = QTextEdit()
        self.review_input.setMaximumHeight(100)

        form_layout.addRow("商品ID:", self.product_id_input)
        form_layout.addRow("规格:", self.spec_input)
        form_layout.addRow("标题:", self.title_input)
        form_layout.addRow("补手:", self.helper_input)
        form_layout.addRow("买家秀评价:", self.review_input)
        layout.addLayout(form_layout)

        # ---------- 图片选择 ----------
        img_layout = QFormLayout()
        self.selected_images = {"spec": None, "link": None, "review": None}

        self.spec_img_label = QLabel("未选择图片")
        self.spec_img_label.setStyleSheet("color: #888;")
        btn_spec_img = QPushButton("选择")
        btn_spec_img.clicked.connect(lambda: self.select_image("spec"))
        spec_img_layout = QHBoxLayout()
        spec_img_layout.addWidget(self.spec_img_label)
        spec_img_layout.addWidget(btn_spec_img)
        img_layout.addRow("规格图:", spec_img_layout)

        self.link_img_label = QLabel("未选择图片")
        self.link_img_label.setStyleSheet("color: #888;")
        btn_link_img = QPushButton("选择")
        btn_link_img.clicked.connect(lambda: self.select_image("link"))
        link_img_layout = QHBoxLayout()
        link_img_layout.addWidget(self.link_img_label)
        link_img_layout.addWidget(btn_link_img)
        img_layout.addRow("链接主图:", link_img_layout)

        self.review_img_label = QLabel("未选择图片")
        self.review_img_label.setStyleSheet("color: #888;")
        btn_review_img = QPushButton("选择")
        btn_review_img.clicked.connect(lambda: self.select_image("review"))
        review_img_layout = QHBoxLayout()
        review_img_layout.addWidget(self.review_img_label)
        review_img_layout.addWidget(btn_review_img)
        img_layout.addRow("评价图片:", review_img_layout)

        layout.addLayout(img_layout)

        # ---------- 按钮 ----------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def select_image(self, img_type: str) -> None:
        """选择图片文件并回显文件名"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if not file_path:
            return
        self.selected_images[img_type] = file_path
        labels = {"spec": self.spec_img_label, "link": self.link_img_label, "review": self.review_img_label}
        labels[img_type].setText(os.path.basename(file_path))

    def get_data(self) -> dict:
        """获取输入的记录数据（字段名与 RECORD_FIELDS 对齐）"""
        return {
            "product_id": self.product_id_input.text().strip(),
            "spec_image": self.selected_images.get("spec", "") or "",
            "spec": self.spec_input.text().strip(),
            "title": self.title_input.text().strip(),
            "link_image": self.selected_images.get("link", "") or "",
            "helper": self.helper_input.text().strip(),
            "review": self.review_input.toPlainText().strip(),
            "image_path": self.selected_images.get("review", "") or "",
        }
