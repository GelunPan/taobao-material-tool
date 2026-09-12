"""批量导入工作线程：驱动服务层完成「抓取 + 下载 + 组装」，信号向主线程汇报。

线程边界约定（与 _LinkFetchWorker 同一套模式）：
- 本 Worker 只做网络 I/O 与纯数据组装，不触碰 ShopRepository 与任何控件
- 记录字段的写入由主线程在 item_done 槽中完成，避免跨线程写数据
- 单条失败只跳过该条（携带 error 下发），不中断整批；cookie 失效除外
"""
from PyQt6.QtCore import QObject, pyqtSignal

from ..services import taobao_import, taobao_playwright
from ..utils.logger import get_logger

logger = get_logger("taobao.import.worker")


class BatchImportWorker(QObject):
    """一键导入 Worker。

    tasks = [(记录索引, 商品链接), ...]，由主窗口按「有链接且待导入」筛选后传入。

    信号（均跨线程排队投递到主线程）:
        progress(cur, total, url)      每条开始抓取时发出
        item_done(record_index, payload) 单条结束；payload = {updates, error, title}
        cookie_invalid(msg)            cookie 失效导致批次提前终止
        finished(summary)              全部结束；summary = {total, ok, fail, aborted}
    """

    progress = pyqtSignal(int, int, str)
    item_done = pyqtSignal(int, dict)
    cookie_invalid = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def __init__(self, tasks: list):
        super().__init__()
        self.tasks = tasks

    def run(self):
        urls = [url for _, url in self.tasks]
        summary = {"total": len(urls), "ok": 0, "fail": 0, "aborted": 0, "cookie_msg": ""}
        logger.info("一键导入开始，共 %d 条", len(urls))

        def on_item_start(i: int, url: str):
            self.progress.emit(i + 1, len(urls), url)

        def on_item_done(i: int, res: dict):
            record_index = self.tasks[i][0]
            err = res.get("error")
            if err:
                summary["fail"] += 1
                if taobao_playwright._is_cookie_invalid_error(err):
                    summary["cookie_msg"] = err
                self.item_done.emit(record_index, {"error": err, "title": res.get("title") or ""})
                return
            # 图片下载与字段组装都在本线程完成，主线程只管写入
            updates = taobao_import.build_field_updates(res)
            summary["ok"] += 1
            self.item_done.emit(record_index, {"updates": updates, "error": None,
                                               "title": res.get("title") or ""})

        try:
            results = taobao_playwright.fetch_items(
                urls, on_item_done=on_item_done, on_item_start=on_item_start,
            )
        except Exception as e:
            # fetch_items 内部已兜底，这里再兜一层，保证 finished 一定发出
            logger.exception("一键导入异常")
            summary["fail"] += len(urls) - summary["ok"] - summary["fail"]
            self.finished.emit(summary)
            return

        # 结果列表中 None 表示因 cookie 失效未抓取
        aborted = sum(1 for r in results if r is None)
        if aborted:
            summary["aborted"] = aborted
            self.cookie_invalid.emit(
                summary["cookie_msg"] or "cookie 已失效，请重新登录淘宝"
            )

        logger.info("一键导入结束: 成功 %d, 失败 %d, 中止 %d",
                    summary["ok"], summary["fail"], summary["aborted"])
        self.finished.emit(summary)
