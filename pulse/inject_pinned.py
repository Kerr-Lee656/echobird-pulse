"""inject_pinned — 置顶条目注入 (inject_pinned.py 的模块化移植)。

原理: 前端/Rust 归档按 effective_ts DESC 重排序。单纯数组插入 index 不够 —
排序会把置顶条目按时间戳重排回去。因此本模块计算一个 published_at, 使置顶
条目在重排后精确落在目标位置 (1-indexed): 继承其上方邻居的 effective_ts,
配合稳定排序 + 数组插入, 令其恰好停在目标槽位。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .common import effective_ts, iso, now_utc


def _iso_z_now(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _pre_sort_desc(items: list[dict]) -> None:
    """原地按 Rust effective_ts DESC 排序 — 上游数组顺序不保证, 位置插入前必须对齐。"""
    items.sort(key=lambda it: effective_ts(it) or "", reverse=True)


def compute_pin_ts_string(items_sorted: list[dict], position: int) -> str:
    """返回让置顶条目在 Rust 重排后落到 position (1-indexed) 的 published_at 字符串。

    策略: 继承直接位于其上方的条目 (items_sorted[position-2]) 的 effective_ts,
    配合在 index position-1 处数组插入, 稳定排序将其固定在该条目之下。
    position=1 时继承 items[0] 的 ts 并插到 index 0 (低索引赢得 tiebreak)。
    """
    if not items_sorted:
        return _iso_z_now(datetime.now(timezone.utc))
    above_idx = max(0, position - 2)
    above_idx = min(above_idx, len(items_sorted) - 1)
    ts = effective_ts(items_sorted[above_idx])
    if ts:
        return ts
    return _iso_z_now(datetime.now(timezone.utc) - timedelta(seconds=5))


def _build_item(pin: dict[str, Any], ts_iso: str, now_iso: str) -> dict[str, Any]:
    url = pin["url"]
    title = pin["title"]
    return {
        "id": hashlib.sha1(("echobird-pin:" + url).encode("utf-8")).hexdigest(),
        "site_id": pin.get("site_id", "echobird"),
        "site_name": pin.get("site_name", "EchoBird"),
        "source": pin.get("source", "EchoBird"),
        "title": title,
        "url": url,
        "published_at": ts_iso,
        "first_seen_at": now_iso,
        "last_seen_at": now_iso,
        "title_original": title,
        "title_en": pin.get("title_en"),
        "title_zh": pin.get("title_zh", title),
        "title_bilingual": pin.get("title_bilingual", title),
    }


def inject(items: list[dict], pins: list[dict[str, Any]], now: Optional[datetime] = None) -> list[dict]:
    """把置顶条目注入 items, 返回新列表。pins 形如 {"position": 3, "url": ..., "title": ...}。"""
    if not pins:
        return list(items)
    now = now or now_utc()
    now_iso = _iso_z_now(now)

    # 按 URL 去重, 置顶槽位是 URL 唯一出现处
    pinned_urls = {p["url"] for p in pins}
    items = [it for it in items if it.get("url") not in pinned_urls]

    # 对齐 Rust effective_ts DESC 排序 — 位置插入数学的前提
    _pre_sort_desc(items)

    # 按位置升序插入, 使后续 pin 的 "上方邻居" 已包含先落的 pin
    for pin in sorted(pins, key=lambda x: int(x.get("position", 1))):
        pos = max(1, int(pin.get("position", 1)))
        ts_str = compute_pin_ts_string(items, pos)
        item = _build_item(pin, ts_str, now_iso)
        items.insert(min(pos - 1, len(items)), item)
    return items


def make_injector(config_path: Path, lang: str = "en") -> Callable[[list[dict]], list[dict]]:
    """从 pinned 配置文件构造 pipeline 钩子函数。

    config JSON 形如 {"zh": [...], "en": [...]} — 按 lang 取对应数组,
    "_all" 数组始终并入 (跨语言通用置顶)。
    """
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    pins = list(cfg.get(lang) or []) + list(cfg.get("_all") or [])

    def _hook(items: list[dict]) -> list[dict]:
        return inject(items, pins)

    return _hook
