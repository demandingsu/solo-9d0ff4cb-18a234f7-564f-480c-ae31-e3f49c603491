"""端到端测试：启动本地静态站点 -> 爬虫抓取 -> 归并核对 -> 附件 SHA-256 核验。

运行方式：
    PYTHONPATH=/tmp/pylibs python3 -I tests/test_end_to_end.py
"""
from __future__ import annotations

import functools
import hashlib
import http.server
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

# -I 隔离模式下 PYTHONPATH 不生效，显式注入依赖目录
for candidate in ("/tmp/pylibs", "/tmp/pyroot/lib/python3.11/site-packages"):
    if Path(candidate).exists() and candidate not in sys.path:
        sys.path.insert(0, candidate)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

from app.attachment_verifier import verify_attachments, verify_crawl_report  # noqa: E402
from app.attachments import extract_sha256  # noqa: E402
from app.crawler import CrawlReport, crawl  # noqa: E402
from app.merger import merge_announcements  # noqa: E402
from app.models import AttachmentStatus, ResultStatus  # noqa: E402
from app.parser import parse_detail  # noqa: E402

SITE_DIR = Path(__file__).resolve().parent.parent / "example-site" / "site"
EXPECTED = {
    "CG-2026-001": (ResultStatus.EFFECTIVE, "2026-11-06 10:00:00", 2),
    "CG-2026-002": (ResultStatus.EFFECTIVE, "2026-10-28 15:00:00", 2),
    "CG-2026-003": (ResultStatus.PENDING, None, 2),
    "CG-2026-004": (ResultStatus.PENDING, None, 3),
    "CG-2026-005": (ResultStatus.EFFECTIVE, "2026-11-02 09:30:00", 1),
    "CG-2026-006": (ResultStatus.UNKNOWN, None, 1),
}


def start_server() -> str:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}/list/page_1.html"


class CountingHandler(http.server.SimpleHTTPRequestHandler):
    """记录附件文件被请求的次数，验证“一次核验中相同 URL 只下载一次”。"""

    hits: dict[str, int] = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server 协议方法名
        path = urlparse(self.path).path
        if path.startswith("/files/"):
            self.hits[path] = self.hits.get(path, 0) + 1
        super().do_GET()

    def log_message(self, format: str, *args) -> None:  # 静默测试日志
        return


def start_counting_server() -> tuple[str, dict[str, int]]:
    handler = functools.partial(CountingHandler, directory=str(SITE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    CountingHandler.hits = {}
    return f"http://127.0.0.1:{port}/list/page_1.html", CountingHandler.hits


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}：期望 {expected!r}，实际 {actual!r}")
    print(f"  ✓ {message}")


def main() -> int:
    entry = start_server()
    print(f"[1] 本地示例站点：{entry}")

    print("[2] 从列表页沿下一页抓取并解析详情 ...")
    report = crawl(entry)
    assert_equal(len(report.visited_list_pages), 2, "遍历了 2 个列表页（下一页跟随成功）")
    assert_equal(len(report.announcements), 11, "解析出 11 条公告")
    assert_equal(report.errors, [], "抓取过程无错误")

    # 抽查一条详情页的字段标签解析
    first = next(a for a in report.announcements if a.project_no == "CG-2026-001" and a.notice_type == "更正公告")
    assert_equal(first.publish_time, "2026-10-15 16:20:00", "更正公告发布时间按标签正确解析")
    assert_equal(first.deadline, "2026-11-06 10:00:00", "更正公告新截止时间按标签正确解析")

    pending_notice = next(a for a in report.announcements if a.project_no == "CG-2026-003" and a.notice_type == "延期公告")
    assert_equal(pending_notice.deadline, None, "无新时间的延期页截止时间为 None")
    assert_equal(pending_notice.declares_extension, True, "延期页被识别为声明延期")

    print("[3] 按项目编号归并并按发布时间处理 ...")
    results = merge_announcements(report.announcements)
    assert_equal(len(results), 6, "归并出 6 个项目")
    by_no = {r.project_no: r for r in results}

    for project_no, (status, deadline, count) in EXPECTED.items():
        result = by_no[project_no]
        assert_equal(result.status, status, f"{project_no} 状态为 {status.value}")
        assert_equal(result.current_deadline, deadline,
                     f"{project_no} 当前截止时间为 {deadline or '空（不显示旧值）'}")
        assert_equal(len(result.announcements), count, f"{project_no} 保留 {count} 条公告链接")
        # 公告按发布时间升序
        times = [a.publish_time for a in result.announcements]
        assert_equal(times, sorted(times), f"{project_no} 公告按发布时间升序排列")

    print("[4] 解析器兜底能力（dl/dt/dd 与纯文本标签）...")
    alt_html = """
    <html><body><h1>某设备采购更正公告</h1>
      <dl><dt>项目编号：</dt><dd>ALT-999</dd>
          <dt>公告类型</dt><dd>更正公告</dd>
          <dt>发布时间：</dt><dd>2026年11月01日 10:00</dd></dl>
      <p>投标截止时间：2026年11月20日 09:30，特此通知。</p>
    </body></html>
    """
    ann = parse_detail(alt_html, "http://test/alt.html")
    assert_equal(ann.project_no, "ALT-999", "dl/dt/dd 结构解析项目编号")
    assert_equal(ann.notice_type, "更正公告", "dl/dt/dd 结构解析公告类型")
    assert_equal(ann.publish_time, "2026年11月01日 10:00", "dl/dt/dd 结构解析发布时间")
    assert_equal(ann.deadline, "2026年11月20日 09:30", "纯文本标签兜底解析截止时间")

    print("[5] 无项目编号的页面应被拒绝 ...")
    try:
        parse_detail("<html><body><p>没有字段标签的页面</p></body></html>", "http://test/x.html")
    except ValueError:
        print("  ✓ 无项目编号页面抛出 ValueError")
    else:
        raise AssertionError("无项目编号页面未被拒绝")

    print("[6] 附件链接与标注摘要解析 ...")
    attachments = [link for ann in report.announcements for link in ann.attachments]
    assert_equal(len(attachments), 9, "解析出 9 个附件链接")
    assert_equal(
        len(extract_sha256("SHA-256：" + "a" * 64 + " ），另见校验值：" + "b" * 64)),
        2,
        "同一文本中的多个摘要均可提取",
    )
    assert_equal(extract_sha256("摘要：not-a-digest"), [], "非 64 位十六进制不算摘要")

    no_digest = next(link for link in attachments if "drawings" in link.url)
    assert_equal(no_digest.declared_digests, [], "未标注摘要的附件保留空标注")

    print("[7] 附件核验：每个 URL 只下载一次，逐条保留公告标注 ...")
    entry, hits = start_counting_server()
    counted = crawl(entry)
    att_report = verify_crawl_report(counted)

    assert_equal(att_report.incomplete, False, "采集完整")
    assert_equal(att_report.all_verified, False, "存在不一致/冲突/无法核验，不能宣称全部通过")
    assert_equal(len(att_report.attachments), 7, "同一 URL 合并后共 7 个附件")

    by_path = {urlparse(att.url).path.rsplit("/", 1)[-1]: att for att in att_report.attachments}
    for path, expected_status in (
        ("CG-2026-003-tender.pdf", AttachmentStatus.CONFLICT),
        ("CG-2026-004-missing.pdf", AttachmentStatus.UNVERIFIABLE),
        ("CG-2026-006-archive.zip", AttachmentStatus.UNVERIFIABLE),
        ("CG-2026-005-drawings.zip", AttachmentStatus.UNVERIFIABLE),
        ("CG-2026-004-clarification.pdf", AttachmentStatus.MATCH),
        ("CG-2026-002-tender.zip", AttachmentStatus.MISMATCH),
    ):
        assert_equal(by_path[path].status, expected_status, f"{path} 核验结果为 {expected_status.value}")

    matched = next(att for att in att_report.attachments if "/CG-2026-001-tender.pdf" in att.url)
    assert_equal(matched.status, AttachmentStatus.MATCH, "两条公告对同一附件标注相同摘要且文件相符 -> 一致")
    assert_equal(len(matched.annotations), 2, "同 URL 的两条公告来源均保留")
    digest = hashlib.sha256((SITE_DIR / "files" / "CG-2026-001-tender.pdf").read_bytes()).hexdigest()
    assert_equal(matched.actual_sha256, digest, "实际摘要按文件内容计算")

    conflict = next(att for att in att_report.attachments if "/CG-2026-003-tender.pdf" in att.url)
    assert_equal(conflict.status, AttachmentStatus.CONFLICT, "同 URL 标注不同摘要 -> 标注冲突")
    assert_equal(len(conflict.annotations), 2, "冲突时两条公告来源均保留")
    distinct = {d for ann in conflict.annotations for d in ann.declared_digests}
    assert_equal(len(distinct), 2, "冲突时两个标注值分别保留，未任选一条")
    assert_equal(conflict.actual_sha256,
                 hashlib.sha256((SITE_DIR / "files" / "CG-2026-003-tender.pdf").read_bytes()).hexdigest(),
                 "冲突不影响实际摘要的计算")

    missing = next(att for att in att_report.attachments if "CG-2026-004-missing" in att.url)
    assert_equal(missing.actual_sha256, None, "下载失败不产生实际摘要")
    assert_equal("下载失败" in missing.note, True, "下载失败注明原因")

    large = next(att for att in att_report.attachments if "CG-2026-006-archive" in att.url)
    assert_equal(large.size_bytes, 5 * 1024 * 1024 + 512 * 1024, "超限附件仅记录声明大小，不读取")
    assert_equal("5 MiB" in large.note, True, "超限附件注明上限")

    print("[8] 相同 URL 在一次核验中只下载一次 ...")
    dedup_url = "/files/CG-2026-001-tender.pdf"
    assert_equal(hits.get(dedup_url), 1, "被两条公告引用的附件只下载一次")
    assert_equal(hits.get("/files/CG-2026-003-tender.pdf"), 1, "标注冲突的同一 URL 只下载一次")
    for path in ("/files/CG-2026-004-missing.pdf", "/files/CG-2026-005-drawings.zip"):
        assert_equal(hits.get(path), 1, f"{path} 只请求一次")

    print("[9] 采集不完整时不得宣称全部附件通过 ...")
    # 只保留 001 与 004 更正公告，它们的附件都与实际文件一致
    clean = [ann for ann in report.announcements
             if ann.project_no in {"CG-2026-001", "CG-2026-004"}
             and not any("missing" in link.url for link in ann.attachments)]
    incomplete_report = verify_attachments(clean, incomplete_reasons=["详情页抓取失败：某公告"])
    assert_equal(incomplete_report.all_verified, False, "采集不完整时即使附件均一致也不得宣称全部通过")
    assert_equal(incomplete_report.incomplete, True, "采集不完整被标记")

    complete_report = verify_attachments(clean)
    assert_equal(complete_report.all_verified, True, "采集完整且附件均一致时才可宣称全部通过")
    assert_equal(complete_report.mismatch_count, 0, "一致集合无不一致附件")

    print("[10] 采集不完整的原因来自抓取器（详情页抓取失败 / 达到上限）...")
    partial = CrawlReport(incomplete_reasons=["详情页抓取失败：某公告"])
    partial.errors.append("详情页抓取失败：某公告")
    partial_attachments = verify_crawl_report(partial)
    assert_equal(partial_attachments.all_verified, False, "无附件时也不得宣称全部通过")
    assert_equal(partial_attachments.errors, ["详情页抓取失败：某公告"], "抓取错误传导到附件核验")

    print("\n全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
