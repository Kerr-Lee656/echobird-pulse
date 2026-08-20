"""pulse — EchoBird AI Pulse 独立模块 CLI。

用法:
    python -m pulse build [--output PATH] [--days 7] [--no-aihot] [--pins FILE]
    python -m pulse fetch-zh [--output docs/pulse]
    python -m pulse merge-zh <feed.json>            # aihot 精选并入中文 feed
    python -m pulse filter <file...>                # 黑名单清洗
    python -m pulse pin <feed.json> [--lang zh|en] [--config pulse_pinned.json]
    python -m pulse archive <feed.json> [--root DIR] [--lang zh|en]
    python -m pulse load [--root DIR] [--lang zh|en] [--limit N]
    python -m pulse dates [--root DIR] [--lang zh|en]
    python -m pulse serve [--root DIR] [--port 8765]   # 本地静态预览(实验性)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .archive import date_counts, load_all, save_fanout
from .common import fetch, now_utc
from .filters import filter_file, filter_items
from .inject_pinned import inject
from .pipeline import build_en_feed, write_feed
from .sources import dedupe_by_title, merge_aihot

SUYXH_BASE = "https://raw.githubusercontent.com/SuYxh/ai-news-aggregator/main/data"


def cmd_build(args: argparse.Namespace) -> int:
    from .inject_pinned import make_injector

    pre_filter = None
    if args.pins:
        pre_filter = make_injector(Path(args.pins))
    payload = build_en_feed(
        window_days=args.days,
        merge_aihot_enabled=not args.no_aihot,
        pre_filter=pre_filter,
    )
    write_feed(payload, Path(args.output))
    print(f"total_items={payload.total_items} site_count={payload.site_count} source_count={payload.source_count}")
    return 0


def cmd_fetch_zh(args: argparse.Namespace) -> int:
    """镜像上游 SuYxh 中文聚合 feed (纯下载, 不做清洗 — 清洗用 `filter` 子命令)。"""
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    for f in ("latest-24h.json", "latest-7d.json"):
        url = f"{SUYXH_BASE}/{f}"
        r = fetch(url)
        if not r:
            print(f"[warn] {f} fetch failed", file=sys.stderr)
            ok = False
            continue
        if not r.text.lstrip().startswith("{"):
            print(f"[warn] {f} did not look like JSON", file=sys.stderr)
            ok = False
            continue
        dest = out_dir / f
        dest.write_text(r.text, encoding="utf-8")
        print(f"[ok] {f} → {dest} ({len(r.content)} bytes)")
    return 0 if ok else 1


def cmd_merge_zh(args: argparse.Namespace) -> int:
    """把 aihot 精选合并进中文 feed (原版 merge_aihot.py 等价)。

    按 URL 合并: 同 URL 时 aihot 的 LLM 规范化标题胜出并保留原条目发现时间。
    同步更新 total_items / site_stats, 保持文件缩进风格。失败非致命 (aihot
    不可用时原样继续)。
    """
    path = Path(args.file)
    if not path.exists():
        print(f"::warning::{path} does not exist, skipping", file=sys.stderr)
        return 0
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    existing: list[dict] = list(payload.get("items") or [])
    before_count = len(existing)

    merged, added, overrode = merge_aihot(existing, now_utc())

    # 2026-08-20: 标题归一化去重——IT之家原文与 aihot 镜像（URL 不同但标题相同）跨源重复
    merged = dedupe_by_title(merged)

    payload["items"] = merged
    payload["total_items"] = len(merged)

    # site_stats: SuYxh 已是 {site_id,site_name,count,raw_count} 列表形态
    site_stats = payload.get("site_stats") or []
    if isinstance(site_stats, list):
        aihot_count = sum(1 for it in merged if it.get("site_id") == "aihot")
        bumped = False
        for s in site_stats:
            if s.get("site_id") == "aihot":
                s["count"] = aihot_count
                s["raw_count"] = aihot_count
                s["site_name"] = "AI HOT 精选"
                bumped = True
                break
        if not bumped and aihot_count > 0:
            site_stats.append({"site_id": "aihot", "site_name": "AI HOT 精选",
                               "count": aihot_count, "raw_count": aihot_count})
            payload["site_count"] = len(site_stats)
        payload["site_stats"] = site_stats

    # 保持上游缩进风格, 让提交只含内容增量
    indent = 2 if len(text) > 2 and text[1] == "\n" and text[2] == " " else 0
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    print(f"{path}: {before_count} → {len(merged)} (+{added} new from aihot, {overrode} title-overrode)")
    return 0


def cmd_filter(args: argparse.Namespace) -> int:
    """对已下载的 feed 文件应用黑名单清洗 + 未来时间戳修复 (原版 filter_pulse.py 等价)。"""
    for arg in args.files:
        path = Path(arg)
        if not path.exists():
            print(f"::warning::{path} does not exist, skipping", file=sys.stderr)
            continue
        before, after, fixed = filter_file(path)
        print(f"{path}: {before} → {after} ({before - after} dropped, {fixed} ts-normalized)")
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    """向 feed 文件注入置顶条目 (原版 inject_pinned.py 等价)。"""
    path = Path(args.file)
    config = Path(args.config)
    if not path.exists():
        print(f"::warning::{path} does not exist, skipping", file=sys.stderr)
        return 0
    if not config.exists():
        print(f"::warning::{config} missing, skipping pin step", file=sys.stderr)
        return 0
    try:
        cfg = json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"::warning::pinned config parse failed: {e}", file=sys.stderr)
        return 0
    pins = list(cfg.get(args.lang) or []) + list(cfg.get("_all") or [])
    if not pins:
        print(f"{path}: no pinned items for lang={args.lang}")
        return 0
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    items = list(payload.get("items") or [])
    before = len(items)
    try:
        injected = inject(items, pins)
    except Exception as e:  # noqa: BLE001
        print(f"::warning::inject failed for {path}: {e}", file=sys.stderr)
        return 0
    payload["items"] = injected
    payload["total_items"] = len(injected)
    # 更新 site_stats 中对应 site_id 计数 (兼容 list / dict 两种形态)
    seen_sids = {p.get("site_id", "echobird") for p in pins}
    site_stats = payload.get("site_stats")
    if isinstance(site_stats, list):
        for sid in seen_sids:
            count = sum(1 for it in injected if it.get("site_id") == sid)
            sname = next((p["site_name"] for p in pins if p.get("site_id") == sid), sid)
            existing = next((s for s in site_stats if s.get("site_id") == sid), None)
            if existing:
                existing["count"] = count
                existing["raw_count"] = count
                existing["site_name"] = sname
            else:
                site_stats.append({"site_id": sid, "site_name": sname, "count": count, "raw_count": count})
        if "site_count" in payload:
            payload["site_count"] = len(site_stats)
    elif isinstance(site_stats, dict):
        for sid in seen_sids:
            site_stats[sid] = sum(1 for it in injected if it.get("site_id") == sid)
        if "site_count" in payload:
            payload["site_count"] = len(site_stats)
    indent = 2 if len(text) > 2 and text[1] == "\n" and text[2] == " " else 0
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    print(f"{path}: {before} → {len(injected)} (+{len(pins)} pinned)")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    """把 feed JSON 的 items 扇出归档到按日分桶文件。"""
    payload = json.loads(Path(args.feed).read_text(encoding="utf-8"))
    items = payload.get("items") or []
    total = save_fanout(Path(args.root), args.lang, items)
    print(f"archived {len(items)} items → {total} resident (root={Path(args.root) / 'pulse'})")
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    items = load_all(Path(args.root), args.lang)
    print(f"loaded {len(items)} items (lang={args.lang}, root={Path(args.root) / 'pulse'})")
    for it in items[: args.limit]:
        ts = it.get("published_at") or it.get("first_seen_at") or "?"
        print(f"  {ts[:10]}  {it.get('title', '')[:90]}")
    return 0


def cmd_dates(args: argparse.Namespace) -> int:
    for date, count in date_counts(Path(args.root), args.lang):
        print(f"{date}  {count}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """实验性: 本地静态预览归档 (纯标准库, 无依赖)。"""
    import http.server
    import socketserver

    root = Path(args.root)
    pulse_root = root / "pulse"
    print(f"Serving {pulse_root} at http://localhost:{args.port} — press Ctrl+C to stop")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(pulse_root), **kw)

        def log_message(self, *a):  # 静默
            pass

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pulse", description=f"EchoBird AI Pulse module v{__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build EN AI news/projects feed")
    b.add_argument("--output", default="docs/pulse/latest-7d-en.json")
    b.add_argument("--days", type=int, default=7)
    b.add_argument("--no-aihot", action="store_true", help="skip aihot curated merge")
    b.add_argument("--pins", default=None, help="pinned items JSON config")
    b.set_defaults(fn=cmd_build)

    f = sub.add_parser("fetch-zh", help="mirror upstream SuYxh ZH feed (download only)")
    f.add_argument("--output", default="docs/pulse")
    f.set_defaults(fn=cmd_fetch_zh)

    m = sub.add_parser("merge-zh", help="merge aihot curated picks into a ZH feed file")
    m.add_argument("file", type=str)
    m.set_defaults(fn=cmd_merge_zh)

    fl = sub.add_parser("filter", help="apply blocklist filter to downloaded feed files")
    fl.add_argument("files", nargs="+", type=str)
    fl.set_defaults(fn=cmd_filter)

    pn = sub.add_parser("pin", help="inject pinned items into a feed file")
    pn.add_argument("file", type=str)
    pn.add_argument("--lang", default="zh", choices=["zh", "en"])
    pn.add_argument("--config", default="pulse_pinned.json")
    pn.set_defaults(fn=cmd_pin)

    a = sub.add_parser("archive", help="fan out feed items into per-day archive")
    a.add_argument("feed", type=str)
    a.add_argument("--root", default=str(Path.home() / ".echobird-pulse"))
    a.add_argument("--lang", default="en", choices=["zh", "en"])
    a.set_defaults(fn=cmd_archive)

    l = sub.add_parser("load", help="load archived items")
    l.add_argument("--root", default=str(Path.home() / ".echobird-pulse"))
    l.add_argument("--lang", default="en", choices=["zh", "en"])
    l.add_argument("--limit", type=int, default=10)
    l.set_defaults(fn=cmd_load)

    d = sub.add_parser("dates", help="list archived dates with counts")
    d.add_argument("--root", default=str(Path.home() / ".echobird-pulse"))
    d.add_argument("--lang", default="en", choices=["zh", "en"])
    d.set_defaults(fn=cmd_dates)

    s = sub.add_parser("serve", help="experimental static preview of archive")
    s.add_argument("--root", default=str(Path.home() / ".echobird-pulse"))
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
