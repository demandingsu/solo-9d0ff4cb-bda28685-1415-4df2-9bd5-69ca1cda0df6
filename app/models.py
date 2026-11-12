"""Pydantic 请求/响应模型。"""
from typing import Optional

from pydantic import BaseModel, Field


class SyncRequest(BaseModel):
    list_url: str = Field(..., description="公共采购网站公告列表页 URL")


class AnnouncementItem(BaseModel):
    notice_no: str
    project_no: Optional[str] = None
    notice_type: Optional[str] = None
    publish_time: Optional[str] = None
    deadline: Optional[str] = None
    ref_notice_no: Optional[str] = None
    source_url: Optional[str] = None
    conflict_reasons: list[str] = []
    effective_deadline: Optional[str] = None


class Chain(BaseModel):
    chain_id: str = Field(..., description="链标识，即首次公告编号")
    project_no: Optional[str] = None
    status: str = Field(..., description="ok / conflict / cancelled")
    effective_deadline: Optional[str] = Field(
        None, description="有效截止时间；冲突或已撤销时为 null"
    )
    announcements: list[AnnouncementItem] = []


class OrphanAnnouncement(BaseModel):
    notice_no: str
    project_no: Optional[str] = None
    notice_type: Optional[str] = None
    status: str
    conflict_reasons: list[str] = []
    effective_deadline: Optional[str] = None
    source_url: Optional[str] = None
    publish_time: Optional[str] = None
    deadline: Optional[str] = None
    ref_notice_no: Optional[str] = None


class QueryResponse(BaseModel):
    chains: list[Chain] = []
    orphans: list[OrphanAnnouncement] = []


class SyncResponse(BaseModel):
    run_id: str
    status: str
    total_listed: int
    fetched_ok: int
    failed_count: int
    kept_last: int
    not_modified: int = Field(
        0, description="本次同步中收到 304 并复用本地缓存的页面数"
    )
    message: Optional[str] = None


class PageCacheItem(BaseModel):
    url: str
    page_kind: str = Field(..., description="list / detail")
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    fetched_at: str
    last_used_at: Optional[str] = None
    content_length: int = Field(..., description="缓存原文字节长度")
