"""离线自检 — 不依赖网络, 验证 archive/filter/inject 核心逻辑。"""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pulse import archive as A  # noqa: E402
from pulse.common import effective_ts, iso, parse_iso  # noqa: E402
from pulse.filters import filter_items, normalize_future_timestamps  # noqa: E402
from pulse.inject_pinned import inject  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


# 1) future timestamp fix
now = datetime.now(timezone.utc)
items_future = [
    {"url": "https://a.com/1", "title": "t1", "published_at": iso(now).replace("Z", "Z"),
     "first_seen_at": iso(now).replace("Z", "Z")},
]
fixed = normalize_future_timestamps(
    [{"url": "u", "title": "x", "published_at": "2026-08-05T16:24:59Z",
      "first_seen_at": "2026-08-05T08:24:59Z"}],
    reference=datetime(2026, 8, 5, 8, 30, tzinfo=timezone.utc),
)
check("future-ts fallback", fixed == 1, str(fixed))

# 2) blocklist hosts
blocked = filter_items([
    {"url": "https://x.com/elon/status/1", "title": "KOL post"},
    {"url": "https://twitter.com/a/status/2", "title": "KOL post 2"},
    {"url": "https://v2ex.com/t/3", "title": "forum"},
    {"url": "https://openai.com/blog/x", "title": "real news"},
])
check("host blocklist", len(blocked) == 1 and blocked[0]["url"].startswith("https://openai.com"), f"{len(blocked)} kept")

# 3) title blocklist
blocked2 = filter_items([
    {"url": "https://a.com/1", "title": "【广告】买课"},
    {"url": "https://a.com/2", "title": "广告：xxx"},
    {"url": "https://a.com/3", "title": "社区公告"},
    {"url": "https://a.com/4", "title": "Anthropic 拒绝 X 公司赞助"},
])
check("title blocklist (keep legit article)", len(blocked2) == 1 and "拒绝" in blocked2[0]["title"], f"{len(blocked2)} kept")

# 4) archive fanout + atomic write + dedupe
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    items = [
        {"url": "https://x.com/1", "title": "same-url-new", "published_at": "2026-08-05T08:00:00Z", "first_seen_at": "2026-08-05T08:00:00Z"},
        {"url": "https://x.com/2", "title": "new2", "published_at": "2026-08-04T09:00:00Z", "first_seen_at": "2026-08-04T09:00:00Z"},
    ]
    n1 = A.save_fanout(root, "en", items)
    # 重跑同输入 → 幂等
    n2 = A.save_fanout(root, "en", items)
    # 同 URL 新内容 → 覆盖
    n3 = A.save_fanout(root, "en", [{"url": "https://x.com/1", "title": "same-url-newer", "published_at": "2026-08-05T10:00:00Z", "first_seen_at": "2026-08-05T10:00:00Z"}])
    check("fanout idempotent", n1 == 2 and n2 == 2, f"{n1}/{n2}")
    dates = A.list_all_dates(root, "en")
    check("dates bucketed", len(dates) == 2, str(dates))
    loaded = A.load_all(root, "en")
    check("load_all dedupe+newest-wins", len(loaded) == 2, f"{len(loaded)} items")
    newest = loaded[0]
    check("newest-wins by url", newest["title"] == "same-url-newer", newest["title"])
    # date_counts 快速路径
    counts = dict(A.date_counts(root, "en"))
    check("date_counts", sum(counts.values()) == 2, str(counts))

# 5) future-stamped item → not bucketed into future file
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    A.save_fanout(root, "en", [
        {"url": "https://future.com/1", "title": "fut", "published_at": "2030-01-01T00:00:00Z",
         "first_seen_at": "2026-08-05T08:00:00Z", "last_seen_at": "2026-08-05T08:00:00Z"},
    ])
    loaded = A.load_all(root, "en")
    check("future-ts bucketed by first_seen", len(loaded) == 1 and loaded[0]["url"] == "https://future.com/1", str(A.list_all_dates(root, "en")))

# 6) pin injection keeps position
items = []
for i in range(5):
    items.append({"url": f"https://p.com/{i}", "title": f"item{i}",
                  "published_at": f"2026-08-05T0{i}:00:00Z", "first_seen_at": f"2026-08-05T0{i}:00:00Z"})
pinned = inject(items, [{"position": 3, "url": "https://pin.example.com/guide", "title": "PINNED GUIDE"}])
check("pin injected", len(pinned) == 6, f"{len(pinned)} items")
# 排序后置顶应位于第 3 位 (effective_ts DESC)
pinned_sorted = sorted(pinned, key=lambda it: effective_ts(it), reverse=True)
check("pin at position 3", pinned_sorted[2]["title"] == "PINNED GUIDE", pinned_sorted[2]["title"])

print()
if fails:
    print(f"❌ {len(fails)} FAILED: {fails}")
    sys.exit(1)
print("✅ all offline checks passed")
