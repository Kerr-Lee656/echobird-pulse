"""filters — 黑名单清洗 + 未来时间戳修复 (filter_pulse.py 的模块化移植)。

同时提供置顶注入 (inject_pinned.py 逻辑, 见 inject_pinned.py 模块)。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .common import BLOCKED_HOST_RE, BLOCKED_TITLE_RE, AI_RE, CN_AI_RE, CJK_RE, parse_iso

FUTURE_SLACK = timedelta(minutes=5)


def normalize_future_timestamps(items: list[dict], reference: Optional[datetime] = None) -> int:
    """当 published_at 超前于参考时间 (> +5min) 时改写为 first_seen_at/last_seen_at。

    上游中文聚合器 (newsnow/juejin/个别微信抓取) 常把北京时间当 UTC 打标,
    造成 published_at 超前 8h。改写使其落回真实时间位置。
    返回修复条数 (可观测性)。
    """
    reference = reference or datetime.now(timezone.utc)
    cutoff = reference + FUTURE_SLACK
    fixed = 0
    for it in items:
        pub = parse_iso(it.get("published_at"))
        if pub is None or pub <= cutoff:
            continue
        fallback = it.get("first_seen_at") or it.get("last_seen_at")
        if not fallback:
            continue
        it["published_at"] = fallback
        fixed += 1
    return fixed


def filter_items(items: list[dict], *, reference: Optional[datetime] = None) -> list[dict]:
    """主机黑名单 (x/twitter/v2ex) + 标题黑名单 (社区公告/广告/推广)
    + 中文标题 AI 相关性过滤 (2026-08: 综合频道混入非 AI 内容) + 时间戳修复。"""
    kept = [
        it
        for it in items
        if not BLOCKED_HOST_RE.match(it.get("url") or "")
        and not BLOCKED_TITLE_RE.search(it.get("title") or "")
        and _is_ai_relevant(it.get("title") or "")
    ]
    normalize_future_timestamps(kept, reference)
    return kept


def _is_ai_relevant(title: str) -> bool:
    """中文标题必须命中 AI 关键词才保留; 英文标题不设限 (en feed 构建时已过滤)。

    中文 feed 镜像自 SuYxh 综合聚合器 (36氪/IT之家/虎嗅/Bloomberg 等频道),
    会混入娱乐/财经/事故等非 AI 内容 — 标题不含任何 AI 关键词即丢弃。
    """
    if not CJK_RE.search(title):
        return True
    return bool(AI_RE.search(title) or CN_AI_RE.search(title))


def filter_file(path: Path) -> tuple[int, int, int]:
    """原地清洗一个 pulse JSON 文件。返回 (before, kept, ts_fixed)。"""
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    items = payload.get("items") or []
    before = len(items)
    kept = filter_items(items, reference=parse_iso(payload.get("generated_at")))
    payload["items"] = kept
    payload["total_items"] = len(kept)
    fixed = normalize_future_timestamps(kept, reference=parse_iso(payload.get("generated_at")))
    # 保留原文件缩进风格, 使 refresh 提交只含真实内容增量
    indent = 2 if len(text) > 2 and text[1] == "\n" and text[2] == " " else 0
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    return before, len(kept), fixed


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: filter_pulse.py FILE [FILE ...]", file=sys.stderr)
        return 2
    for arg in argv[1:]:
        path = Path(arg)
        before, after, fixed = filter_file(path)
        dropped = before - after
        print(f"{path}: {before} → {after} ({dropped} dropped, {fixed} ts-normalized)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
