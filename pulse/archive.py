"""archive — AI Pulse 磁盘归档 (Rust pulse_archive.rs 的 Python 移植)。

布局: <root>/pulse/YYYY/MM/DD_{lang}.json
每个文件:
    { "schema": 1, "date": "2026-05-15", "lang": "zh",
      "item_count": 538, "items": [ ...NewsItem... ] }

上游 feed 是滑动 7 天窗口: 错过即永久丢失, 因此每次抓取按本地日期
(published_at) 把条目扇出到每日桶, 与磁盘已有数据原子合并。只要用户
每 7 天内打开一次应用, 没有一天会丢。

原子写入 = tmp 文件 → rename。部分写入永远不会留下半解析的日档。

移植要点 (与 Rust 行为一一对应):
  - effective_ts: published_at > now+5min (上游时区标记错误) → 回退
    first_seen_at/last_seen_at; 写路径与读路径都过它, 未来日期文件被
    写入端杜绝、读取端 (date > today+1) 过滤。
  - bucket_date: 本地时区的 YYYY-MM-DD — 裸 slice(0,10) 会把 CST
    00:00–08:00 的条目错分进 UTC 日期桶。
  - save_fanout 幂等: 同输入重跑是 no-op; URL 去重新者胜 (上游回填
    更好翻译/标题时取最新)。
  - load_all 跨日 URL 去重, 防上游时间戳变动把同 URL 分进两天。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .common import effective_ts, parse_iso

FUTURE_SLACK_SECS = 5 * 60

DEFAULT_ROOT = Path.home() / ".echobird-pulse"


def _pulse_root(root: Path) -> Path:
    return root / "pulse"


def day_path(root: Path, date: str, lang: str) -> Optional[Path]:
    """构造 (date, lang) 的磁盘路径。日期/语言不合法返回 None (防御路径注入)。"""
    if len(date) < 10:
        return None
    year, month, day = date[0:4], date[5:7], date[8:10]
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return None
    if date[4:5] != "-" or date[7:8] != "-":
        return None
    if not lang.isalpha():
        return None
    return _pulse_root(root) / year / month / f"{day}_{lang}.json"


def bucket_date(item: dict, today_str: str) -> str:
    """条目归属的本地日期桶。回退链: effective_ts → first_seen_at →
    last_seen_at → today。未来时间戳经 effective_ts 落到 first_seen_at 日期。"""
    primary = effective_ts(item)
    pick: Optional[str] = None
    if primary:
        pick = primary
    else:
        for key in ("first_seen_at", "last_seen_at"):
            v = item.get(key)
            if v:
                pick = v
                break
    if not pick:
        return today_str
    dt = parse_iso(pick)
    if dt is not None:
        local = dt.astimezone()
        return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
    # 兜底: 字符串以 YYYY-MM-DD 开头则信任前缀 (与 legacy localStorage 一致)
    if len(pick) >= 10:
        head = pick[:10]
        if all(c.isdigit() or c == "-" for c in head) and head.count("-") == 2:
            return head
    return today_str


def _load_day_file(path: Path) -> list[dict]:
    """读一个日档, 解析失败返回 [] (宽容, 与 Rust 一致)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("items") or [])
    except Exception:  # noqa: BLE001
        return []


def save_fanout(root: Path, lang: str, items: list[dict]) -> int:
    """把 items 扇出到每日桶, 与磁盘合并 (URL 去重新者胜), 原子写回。

    返回写入的总条数 (信息性)。幂等: 同输入重跑无副作用。
    """
    if not items:
        return 0
    now = datetime.now().astimezone()
    today_str = f"{now.year:04d}-{now.month:02d}-{now.day:02d}"

    buckets: dict[str, list[dict]] = {}
    for it in items:
        date = bucket_date(it, today_str)
        buckets.setdefault(date, []).append(it)

    total_written = 0
    for date, new_items in buckets.items():
        path = day_path(root, date, lang)
        if path is None:
            continue

        existing = _load_day_file(path) if path.exists() else []

        # URL 去重: 新条目替换旧条目 (上游回填更好标题时取最新)
        by_url: dict[str, dict] = {}
        for it in existing:
            by_url[it.get("url") or ""] = it
        for it in new_items:
            by_url[it.get("url") or ""] = it
        merged = list(by_url.values())

        # 新→旧排序; 空时间戳沉底
        merged.sort(key=lambda it: effective_ts(it), reverse=True)

        path.parent.mkdir(parents=True, exist_ok=True)
        day_file = {
            "schema": 1,
            "date": date,
            "lang": lang,
            "item_count": len(merged),
            "items": merged,
        }
        json_str = json.dumps(day_file, ensure_ascii=False, separators=(",", ":"))

        # 原子写入: 同目录 tmp → rename (Windows 上 rename 覆盖存在文件可行)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json_str)
            shutil.move(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        total_written += len(merged)
    return total_written


def load_all(root: Path, lang: str) -> list[dict]:
    """读磁盘上 lang 的全部条目, 新→旧排序, 跨日 URL 去重。"""
    now_local = datetime.now().astimezone()
    tomorrow = now_local + timedelta(days=1)
    max_date_str = f"{tomorrow.year:04d}-{tomorrow.month:02d}-{tomorrow.day:02d}"

    dates = [d for d in list_all_dates(root, lang) if d <= max_date_str]
    out: list[dict] = []
    seen_urls: set[str] = set()
    for date in dates:
        path = day_path(root, date, lang)
        if path is None or not path.exists():
            continue
        for it in _load_day_file(path):
            # 跨日去重: 上游时间戳变动可能把同 URL 落进两个日档
            url = it.get("url") or ""
            if url in seen_urls:
                continue
            seen_urls.add(url)
            out.append(it)
    out.sort(key=lambda it: effective_ts(it), reverse=True)
    return out


def list_all_dates(root: Path, lang: str) -> list[str]:
    """枚举 lang 的所有 YYYY-MM-DD, 降序。"""
    pulse_root = _pulse_root(root)
    dates: list[str] = []
    if not pulse_root.is_dir():
        return dates
    lang_suffix = f"_{lang}.json"
    for year_dir in sorted(pulse_root.iterdir(), reverse=True):
        if not (year_dir.is_dir() and year_dir.name.isdigit() and len(year_dir.name) == 4):
            continue
        for month_dir in sorted(year_dir.iterdir(), reverse=True):
            if not (month_dir.is_dir() and month_dir.name.isdigit() and len(month_dir.name) == 2):
                continue
            for f in month_dir.iterdir():
                if not f.is_file():
                    continue
                day_part = f.name.removesuffix(lang_suffix)
                if day_part != f.name and day_part.isdigit() and len(day_part) == 2:
                    dates.append(f"{year_dir.name}-{month_dir.name}-{day_part}")
    dates.sort(reverse=True)
    return dates


def date_counts(root: Path, lang: str) -> list[tuple[str, int]]:
    """每个归档日期及其条数, 降序。直接读 item_count 头, 不反序列化 items —
    比 load_all 快 ~10 倍, 供侧边栏使用。"""
    out: list[tuple[str, int]] = []
    for date in list_all_dates(root, lang):
        path = day_path(root, date, lang)
        if path is None or not path.exists():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            out.append((date, int(meta.get("item_count") or 0)))
        except Exception:  # noqa: BLE001
            continue
    return out
