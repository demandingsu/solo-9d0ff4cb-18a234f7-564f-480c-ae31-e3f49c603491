"""附件解析：从公告详情页读取附件链接及其标注的 SHA-256。

规则：
- 附件链接取“下载/附件/招标文件”等锚文本，或 URL 指向常见文档后缀；
- 标注摘要从附件链接所在行/段落/表格行中查找，也支持与链接相邻的独立段落；
- 摘要只认 64 位十六进制（可带 sha256/摘要等前缀），原样保留，不在解析阶段取舍；
- 相对链接按详情页地址解析，非 http(s) 链接忽略。
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urldefrag, urlparse

from bs4 import BeautifulSoup, Tag

from .models import AttachmentLink

MAX_FILENAME = 120

_DOC_EXT_RE = re.compile(
    r"\.(?:pdf|zip|rar|7z|docx?|xlsx?|pptx?|txt|csv|ofd|wps|et|dwg|jpe?g|png|tiff?)(?:$|[?#])",
    re.IGNORECASE,
)
_ATTACH_TEXT_RE = re.compile(r"附件|下载|招标文件|采购文件|澄清文件|答疑|补遗|工程量清单|图纸", re.IGNORECASE)

# 64 位十六进制摘要，允许前缀：sha256 / sha-256 / SHA256 校验值 / 摘要 / 哈希
_DIGEST_RE = re.compile(
    r"(?:sha[-_ ]?256|摘要|哈希|校验值|checksum)?\s*[:：=]?\s*([0-9a-fA-F]{64})(?![0-9a-fA-F])",
    re.IGNORECASE,
)



def normalize_sha256(value: str) -> str:
    """统一为小写十六进制，便于比较；非 64 位十六进制返回空串。"""
    candidate = (value or "").strip().lower()
    return candidate if re.fullmatch(r"[0-9a-f]{64}", candidate) else ""


def extract_sha256(text: str) -> list[str]:
    """从一段文本中提取全部 SHA-256 标注值（去重保序）。"""
    if not text:
        return []
    found: list[str] = []
    for match in _DIGEST_RE.finditer(text):
        digest = normalize_sha256(match.group(1))
        if digest and digest not in found:
            found.append(digest)
    return found


def _filename_from_url(url: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name[:MAX_FILENAME]


def _is_attachment_anchor(anchor: Tag, url: str) -> bool:
    text = anchor.get_text(" ", strip=True)
    if _ATTACH_TEXT_RE.search(text):
        return True
    if _DOC_EXT_RE.search(urlparse(url).path) or _DOC_EXT_RE.search(url):
        return True
    # download 属性也视为附件（哪怕没有可读文件名）
    if anchor.has_attr("download"):
        return True
    return False


def _normalize_attachment_url(base: str, href: str) -> str | None:
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    absolute = urldefrag(urljoin(base, href))[0].strip()
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return absolute


def _row_text_for(anchor: Tag) -> str:
    """收集与链接同一行的文本，用于查找该附件的标注摘要。

    覆盖：<li>、表格行 <tr>、筛选同级的表格单元格、dt/dd 定义列表，
    以及最近的块级容器（段落/div）。只取该行/容器的文本，避免把整页无关文本收进来。
    """
    dt = anchor.find_parent("dt")
    if dt is not None:
        dl = dt.find_parent("dl")
        if dl is not None:
            dts, dds = dl.find_all("dt"), dl.find_all("dd")
            if dt in dts and len(dts) == len(dds):
                index = dts.index(dt)
                return " ".join(
                    [dt.get_text(" ", strip=True), dds[index].get_text(" ", strip=True)]
                )

    container = anchor.find_parent(["li", "tr"])
    if container is None:
        # 表格布局中常见“链接单独在一个 td、摘要写在旁边的 td”
        cell = anchor.find_parent("td")
        if cell is not None:
            siblings = cell.find_next_siblings("td", limit=1)
            if siblings:
                return " ".join(
                    [cell.get_text(" ", strip=True), siblings[0].get_text(" ", strip=True)]
                )
        container = anchor.find_parent(["p", "dd", "div"])
    if container is None:
        container = anchor.parent

    chunks: list[str] = []

    def walk(node: Tag, depth: int = 0) -> None:
        if depth > 2:
            return
        for child in node.children:
            if isinstance(child, Tag):
                if child.name in {"table", "ul", "ol"} and child is not container:
                    continue
                walk(child, depth + 1)
            elif child.string:
                chunks.append(child.string)

    if isinstance(container, Tag):
        walk(container)
    else:
        chunks.append(anchor.get_text(" ", strip=True))
    return " ".join(chunk.strip() for chunk in chunks if chunk.strip())


def _following_text(anchor: Tag) -> str:
    """链接之后紧邻的兄弟文本/段落，常见于“附件：xxx.pdf”下方另起一行写摘要。"""
    chunks: list[str] = []
    for sibling in anchor.find_next_siblings(limit=2):
        if isinstance(sibling, Tag) and sibling.name in {"table", "ul", "ol"}:
            continue
        chunks.append(sibling.get_text(" ", strip=True) if isinstance(sibling, Tag) else str(sibling))
    parent = anchor.find_parent(["li", "p", "dd", "td"])
    if parent is not None:
        for sibling in parent.find_next_siblings(limit=2):
            if isinstance(sibling, Tag) and sibling.name in {"table", "ul", "ol"}:
                continue
            chunks.append(sibling.get_text(" ", strip=True) if isinstance(sibling, Tag) else str(sibling))
    return " ".join(chunk.strip() for chunk in chunks if chunk and chunk.strip())


def extract_attachments(html: str, page_url: str) -> list[AttachmentLink]:
    """解析详情页中的附件链接与标注摘要，同一 URL 在单页内合并标注值。"""
    soup = BeautifulSoup(html, "html.parser")
    by_url: dict[str, AttachmentLink] = {}
    order: list[str] = []

    for anchor in soup.find_all("a", href=True):
        url = _normalize_attachment_url(page_url, anchor.get("href", ""))
        if not url or not _is_attachment_anchor(anchor, url):
            continue
        text = anchor.get_text(" ", strip=True)
        filename = text if text and not _DOC_EXT_RE.search(text) else ""
        if not filename:
            filename = anchor.get("download") or _filename_from_url(url)

        declared = extract_sha256(_row_text_for(anchor))
        if not declared:
            declared = extract_sha256(_following_text(anchor))

        if url not in by_url:
            by_url[url] = AttachmentLink(url=url, filename=filename[:MAX_FILENAME])
            order.append(url)
        link = by_url[url]
        for digest in declared:
            if digest not in link.declared_digests:
                link.declared_digests.append(digest)

    # 没有锚文本文件名时退化为 URL 文件名
    for link in by_url.values():
        if not link.filename:
            link.filename = _filename_from_url(link.url)
    return [by_url[url] for url in order]

