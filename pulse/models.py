"""数据模型 — 与上游 SuYxh/ai-news-aggregator schema 及 EchoBird 前端 NewsItem 对齐。

字段全部可选容错 (serde(default) 的 Python 对应物)，上游 schema 增删字段
不影响归档读取。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class NewsItem:
    id: str
    source: str
    title: str
    url: str
    site_id: Optional[str] = None
    site_name: Optional[str] = None
    published_at: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    title_original: Optional[str] = None
    title_en: Optional[str] = None
    title_zh: Optional[str] = None
    title_bilingual: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NewsItem":
        """宽容构造: 未知字段忽略, 缺失字段取默认值。"""
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        return {k: v for k, v in out.items() if v is not None}


@dataclass
class RawFeed:
    """顶层 payload。除 items 外均为元数据, 供前端 header 计数。"""
    generated_at: str
    window_hours: int
    total_items: int
    items: list[NewsItem] = field(default_factory=list)
    # 可选元数据, 保持与上游/自有构建器兼容
    total_items_ai_raw: Optional[int] = None
    total_items_raw: Optional[int] = None
    total_items_all_mode: Optional[int] = None
    topic_filter: Optional[str] = None
    archive_total: Optional[int] = None
    site_count: Optional[int] = None
    source_count: Optional[int] = None
    site_stats: Any = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in self.__dataclass_fields__:  # type: ignore[attr-defined]
            v = getattr(self, f)
            if v is None:
                continue
            out[f] = [it.to_dict() for it in v] if f == "items" else v
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RawFeed":
        items = [NewsItem.from_dict(it) for it in (d.get("items") or [])]
        kw = {k: v for k, v in d.items() if k != "items"}
        return cls(**kw, items=items)
