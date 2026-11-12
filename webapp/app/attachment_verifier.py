"""附件核验：按 URL 归并各公告标注，每个 URL 只下载一次，计算实际 SHA-256。

核心口径：
- 同一 URL 在不同公告中标注了不同摘要 -> “标注冲突”，逐条保留标注，绝不任选一条认定通过；
- 下载失败、超过 5 MiB 上限、无标注摘要 -> “无法核验”；
- 实际摘要与全部标注一致 -> “一致”，否则“不一致”；
- 目录/详情采集不完整时，整体不得宣称“全部附件通过”。
"""
from __future__ import annotations

import hashlib
from urllib.parse import urldefrag

import httpx

from .attachments import normalize_sha256
from .crawler import REQUEST_TIMEOUT, USER_AGENT, CrawlReport
from .models import (
    Announcement,
    AttachmentAnnotation,
    AttachmentReport,
    AttachmentResult,
    AttachmentStatus,
)

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 每个文件最多读取 5 MiB


class _DownloadResult:
    def __init__(self, sha256: str | None = None, size: int | None = None,
                 too_large: bool = False, error: str = "") -> None:
        self.sha256 = sha256
        self.size = size
        self.too_large = too_large
        self.error = error


def _normalize_url(url: str) -> str:
    return urldefrag(url.strip())[0]


def _download(client: httpx.Client, url: str) -> _DownloadResult:
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()

            declared_length: int | None = None
            raw_length = response.headers.get("content-length")
            if raw_length:
                try:
                    declared_length = int(raw_length)
                except ValueError:
                    declared_length = None

            if declared_length is not None and declared_length > MAX_ATTACHMENT_BYTES:
                return _DownloadResult(
                    size=declared_length,
                    too_large=True,
                    error=f"文件大小 {declared_length} 字节，超过 5 MiB 上限，未下载。",
                )

            hasher = hashlib.sha256()
            total = 0
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_ATTACHMENT_BYTES:
                    return _DownloadResult(
                        size=total,
                        too_large=True,
                        error="读取过程中超过 5 MiB 上限，已中止下载。",
                    )
                hasher.update(chunk)
            return _DownloadResult(sha256=hasher.hexdigest(), size=total)
    except httpx.HTTPError as exc:
        return _DownloadResult(error=f"下载失败：{exc}")


def _verify_one(client: httpx.Client, url: str,
                annotations: list[AttachmentAnnotation], filename: str,
                cache: dict[str, _DownloadResult]) -> AttachmentResult:
    distinct: list[str] = []  # 各公告声明的摘要（去重保序）
    for annotation in annotations:
        for digest in annotation.declared_digests:
            normalized = normalize_sha256(digest)
            if normalized and normalized not in distinct:
                distinct.append(normalized)

    if url not in cache:
        cache[url] = _download(client, url)
    download = cache[url]

    result = AttachmentResult(
        url=url,
        filename=filename,
        actual_sha256=download.sha256,
        size_bytes=download.size,
        annotations=annotations,
    )

    no_digest_sources = [a for a in annotations if not a.declared_digests]

    if len(distinct) > 1:
        result.status = AttachmentStatus.CONFLICT
        result.note = (
            "同一附件 URL 在不同公告中标注了不同 SHA-256（"
            + "、".join(distinct)
            + "），不能任选一条认定通过，请人工核实。"
        )
        if download.error:
            result.note += f" 另：{download.error}"
        elif download.sha256:
            result.note += f" 实际文件摘要为 {download.sha256}。"
        return result

    if not distinct:
        result.status = AttachmentStatus.UNVERIFIABLE
        result.note = "公告未为该附件标注 SHA-256，无法比对。"
        if download.error:
            result.note += f" 另：{download.error}"
        return result

    if download.error or download.sha256 is None:
        result.status = AttachmentStatus.UNVERIFIABLE
        reason = download.error or "未能读取文件内容。"
        result.note = f"无法核验：{reason}"
        return result

    declared = distinct[0]
    if declared == download.sha256:
        result.status = AttachmentStatus.MATCH
        result.note = "实际文件 SHA-256 与公告标注一致。"
    else:
        result.status = AttachmentStatus.MISMATCH
        result.note = f"实际文件 SHA-256 为 {download.sha256}，与公告标注的 {declared} 不一致。"

    if no_digest_sources:
        result.note += f" 有 {len(no_digest_sources)} 条公告未标注摘要，仅按已标注公告比对。"
    return result


def verify_attachments(announcements: list[Announcement],
                       incomplete_reasons: list[str] | None = None,
                       client: httpx.Client | None = None) -> AttachmentReport:
    """核验公告附件；相同 URL 在一次核验中只下载一次。"""
    grouped: dict[str, list[AttachmentAnnotation]] = {}
    filenames: dict[str, str] = {}

    for announcement in announcements:
        for link in announcement.attachments:
            url = _normalize_url(link.url)
            grouped.setdefault(url, []).append(
                AttachmentAnnotation(
                    announcement_url=announcement.url,
                    project_no=announcement.project_no,
                    notice_type=announcement.notice_type,
                    declared_digests=list(link.declared_digests),
                )
            )
            if link.filename and url not in filenames:
                filenames[url] = link.filename

    report = AttachmentReport(incomplete_reasons=list(incomplete_reasons or []))
    report.incomplete = bool(report.incomplete_reasons)

    cache: dict[str, _DownloadResult] = {}
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    try:
        for url, annotations in grouped.items():
            result = _verify_one(client, url, annotations, filenames.get(url, ""), cache)
            report.attachments.append(result)
    finally:
        if owns_client and client is not None:
            client.close()

    return report


def verify_crawl_report(crawl_report: CrawlReport,
                        client: httpx.Client | None = None) -> AttachmentReport:
    """从抓取结果核验附件，并把采集完整性问题带到附件报告。"""
    report = verify_attachments(
        crawl_report.announcements,
        incomplete_reasons=crawl_report.incomplete_reasons,
        client=client,
    )
    report.errors.extend(crawl_report.errors)
    return report
