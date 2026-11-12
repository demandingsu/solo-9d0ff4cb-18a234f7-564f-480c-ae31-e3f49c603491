"""数据模型：公告与项目归并结果。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResultStatus(str, Enum):
    """项目当前截止时间的处理结果。"""

    EFFECTIVE = "有效"          # 有明确截止时间且未被"延期但无新时间"的公告覆盖
    PENDING = "待核实"          # 较晚的延期/更正公告声明延期，却没有给出新截止时间
    UNKNOWN = "未公布"          # 所有公告均未给出截止时间


@dataclass
class AttachmentLink:
    """公告详情页中的一个附件链接及其标注摘要。"""

    url: str
    filename: str = ""
    declared_digests: list[str] = field(default_factory=list)  # 原样保留的标注值，可能为空


@dataclass
class Announcement:
    """单条招标公告（从详情页按字段标签解析得到）。"""

    url: str
    project_no: str
    title: str = ""
    notice_type: str = ""       # 公告类型，如 招标公告 / 更正公告 / 延期公告
    publish_time: str = ""      # 原始发布时间文本
    deadline: Optional[str] = None   # 投标截止时间原文；未给出时为 None
    declares_extension: bool = False  # 正文是否明确声明延期/顺延
    attachments: list[AttachmentLink] = field(default_factory=list)


@dataclass
class ProjectResult:
    """按项目编号归并后的当前结果。"""

    project_no: str
    project_name: str = ""
    status: ResultStatus = ResultStatus.UNKNOWN
    current_deadline: Optional[str] = None   # 当前有效的投标截止时间；待核实时为 None
    note: str = ""                            # 状态说明
    announcements: list[Announcement] = field(default_factory=list)


class AttachmentStatus(str, Enum):
    """单个附件 URL 的核验结论。"""

    MATCH = "一致"               # 所有标注摘要都与实际文件一致
    MISMATCH = "不一致"           # 存在标注摘要与实际文件不一致
    CONFLICT = "标注冲突"         # 同一 URL 在不同公告中标注了不同摘要
    UNVERIFIABLE = "无法核验"     # 下载失败、超过大小上限、无标注摘要或协议不支持


@dataclass
class AttachmentAnnotation:
    """某条公告对某个附件 URL 的标注（原样保留，不做去重取舍）。"""

    announcement_url: str
    project_no: str
    notice_type: str
    declared_digests: list[str] = field(default_factory=list)  # 该公告写出的摘要；未标注时为空


@dataclass
class AttachmentResult:
    """按 URL 归并后的附件核验结果，保留全部公告来源与各自标注值。"""

    url: str
    filename: str = ""
    status: AttachmentStatus = AttachmentStatus.UNVERIFIABLE
    actual_sha256: Optional[str] = None   # 实际下载文件计算出的 SHA-256；未下载成功为 None
    size_bytes: Optional[int] = None
    annotations: list[AttachmentAnnotation] = field(default_factory=list)
    note: str = ""


@dataclass
class AttachmentReport:
    """一次核验的附件总览：每个 URL 只下载一次，但保留各公告的标注。"""

    attachments: list[AttachmentResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    incomplete: bool = False              # 目录/详情采集不完整，不能宣称全部通过
    incomplete_reasons: list[str] = field(default_factory=list)

    @property
    def all_verified(self) -> bool:
        """仅当采集完整且每个附件都“一致”时，才可宣称全部通过。"""
        return (
            not self.incomplete
            and not self.errors
            and bool(self.attachments)
            and all(item.status == AttachmentStatus.MATCH for item in self.attachments)
        )

    def _count(self, status: AttachmentStatus) -> int:
        return sum(1 for item in self.attachments if item.status == status)

    @property
    def mismatch_count(self) -> int:
        return self._count(AttachmentStatus.MISMATCH)

    @property
    def conflict_count(self) -> int:
        return self._count(AttachmentStatus.CONFLICT)

    @property
    def unverifiable_count(self) -> int:
        return self._count(AttachmentStatus.UNVERIFIABLE)

    @property
    def matched_count(self) -> int:
        return self._count(AttachmentStatus.MATCH)
