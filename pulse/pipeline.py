"""pipeline — 构建英文 AI 新闻/项目 feed (build_en_pulse.py 的模块化移植)。

输出与上游 SuYxh/ai-news-aggregator latest-7d.json 同 schema, 前端可仅凭
文件名切换语言而无需改代码。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .common import BLOCKED_HOST_RE, iso, now_utc
from .filters import filter_items
from .models import RawFeed
from .sources import (
    dedupe_by_title,
    dedupe_by_url,
    fetch_all_rss_feeds,
    fetch_github_trending,
    fetch_hn_firebase,
    fetch_hn_stories,
    merge_aihot,
)


def build_en_feed(
    window_days: int = 7,
    *,
    now: Optional[datetime] = None,
    merge_aihot_enabled: bool = True,
    pre_filter: Optional[Callable[[list[dict]], list[dict]]] = None,
) -> RawFeed:
    """构建英文 feed。

    pre_filter: 可选钩子, 在写盘前对 items 做最后的自定义处理
    (例如注入置顶条目)。返回 None 表示不用。
    """
    now = now or now_utc()
    window_start = now - timedelta(days=window_days)

    items: list[dict] = []
    items.extend(fetch_hn_stories(window_start, now))
    items.extend(fetch_hn_firebase(window_start, now))  # 热榜视角补充
    items.extend(fetch_all_rss_feeds(window_start, now))
    items.extend(fetch_github_trending(now))

    items = dedupe_by_url(items)

    # aihot 精选合并 (默认开启; 失败非致命, 自动降级)
    if merge_aihot_enabled:
        items, added, overrode = merge_aihot(items, now)
        print(f"[aihot] merged: +{added} new, {overrode} title-overrode", file=sys.stderr)

    # 2026-08-20: 标题归一化去重——同一新闻从不同源进来（IT之家原文 vs aihot 镜像）URL 不同但标题相同
    # URL 去重（上面）拦不住跨源重复，这里按归一化标题去重，优先保留主域名源
    items = dedupe_by_title(items)

    # 黑名单过滤 + 未来时间戳修复
    items = filter_items(items, reference=now)

    # 自定义钩子 (置顶注入等)
    if pre_filter:
        items = pre_filter(items) or items

    # 新→旧排序: published_at → first_seen_at → last_seen_at
    def ts_key(it: dict) -> str:
        return it.get("published_at") or it.get("first_seen_at") or it.get("last_seen_at") or ""

    items.sort(key=ts_key, reverse=True)

    site_stats: dict[str, int] = {}
    sources: set[str] = set()
    for it in items:
        site_stats[it.get("site_id") or "unknown"] = site_stats.get(it.get("site_id") or "unknown", 0) + 1
        sources.add(it.get("source") or "")

    payload = RawFeed(
        generated_at=iso(now),
        window_hours=window_days * 24,
        total_items=len(items),
        total_items_ai_raw=len(items),
        total_items_raw=len(items),
        total_items_all_mode=len(items),
        topic_filter="ai+ml (en-only)",
        archive_total=len(items),
        site_count=len(site_stats),
        source_count=len(sources),
        site_stats=site_stats,
        items=[_dict_to_model(it) for it in items],
    )
    return payload


def _dict_to_model(it: dict):
    from .models import NewsItem
    return NewsItem.from_dict(it)


def write_feed(payload: RawFeed, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload.to_dict(), ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    print(f"[done] wrote {payload.total_items} items → {output_path}", file=sys.stderr)


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Build English AI news/projects pulse feed")
    p.add_argument("--output", type=Path, default=Path("docs/pulse/latest-7d-en.json"))
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--no-aihot", action="store_true", help="skip aihot curated merge")
    p.add_argument("--pins", type=Path, default=None, help="pinned items JSON (see inject_pinned)")
    args = p.parse_args()

    pre_filter = None
    if args.pins:
        from .inject_pinned import make_injector
        pre_filter = make_injector(args.pins)

    payload = build_en_feed(
        window_days=args.days,
        merge_aihot_enabled=not args.no_aihot,
        pre_filter=pre_filter,
    )
    write_feed(payload, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
