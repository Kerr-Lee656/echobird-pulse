"""数据源采集器 — 每个源独立函数, 失败静默降级 (warn + 0 条), 互不拖累。"""
from __future__ import annotations

import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import feedparser  # type: ignore[import-untyped]
import requests
from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from .common import AI_RE, fetch, iso, iso_seconds, is_ai_title, now_utc, stable_id


# ─── Source 1: Hacker News Algolia API ────────────────────────────────────────

# 每个查询发给 search_by_date, 限定 story + 7 天窗口。Algolia 每查询最多 1000
# 条; 多个窄查询比一个宽查询覆盖面大。
HN_QUERIES = [
    "AI", "LLM", "GPT", "ChatGPT", "Claude", "Gemini", "OpenAI", "Anthropic",
    "DeepMind", "Mistral", "Llama", "HuggingFace", "AGI", "RAG", "agent",
    "transformer", "diffusion", "embedding", "fine-tuning", "MCP",
]


def fetch_hn_stories(window_start: datetime, now: datetime) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    window_start_epoch = int(window_start.timestamp())
    for q in HN_QUERIES:
        url = (
            "https://hn.algolia.com/api/v1/search_by_date"
            f"?tags=story&query={requests.utils.quote(q)}"
            f"&numericFilters=created_at_i>{window_start_epoch}"
            "&hitsPerPage=200"
        )
        r = fetch(url)
        if not r:
            continue
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            continue
        for hit in data.get("hits", []):
            obj_id = hit.get("objectID")
            if not obj_id or obj_id in seen:
                continue
            url_field = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
            title = hit.get("title") or hit.get("story_title") or ""
            if not title:
                continue
            # 标题必须命中 AI 关键词 — HN 全文搜索有大量误报
            if not AI_RE.search(title):
                continue
            created_iso = hit.get("created_at")
            if not created_iso:
                continue
            seen.add(obj_id)
            points = hit.get("points") or 0
            comments = hit.get("num_comments") or 0
            out.append({
                "id": stable_id("hn", obj_id),
                "site_id": "hackernews",
                "site_name": "Hacker News",
                "source": f"Hacker News ({points}pts, {comments}c)",
                "title": title,
                "url": url_field,
                "published_at": created_iso,
                "first_seen_at": created_iso,
                "last_seen_at": iso(now),
                "title_original": title,
                "title_en": title,
                "title_zh": None,
                "title_bilingual": title,
            })
    print(f"[hn] {len(out)} stories", file=sys.stderr)
    return out


# ─── Source 1b: Hacker News 官方 Firebase API (热榜视角, 补充 Algolia 关键词视角) ──

# Algolia 搜索 API 覆盖"关键词命中"的帖子; Firebase topstories 提供当前热榜
# (500 个高分 story)。两者互补: 热榜能看到关键词搜不到的爆款, 重复条目由
# pipeline 的 dedupe_by_url 处理。Firebase 无速率限制, 免费, 国内直连可用。
HN_FIREBASE_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_FIREBASE_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"


def fetch_hn_firebase(window_start: datetime, now: datetime) -> list[dict]:
    r = fetch(HN_FIREBASE_TOP)
    if not r:
        return []
    try:
        story_ids: list[int] = r.json()
    except Exception:  # noqa: BLE001
        return []
    if not story_ids:
        return []

    out: list[dict] = []
    seen: set[int] = set()
    window_start_epoch = int(window_start.timestamp())

    # 并发拉取 story 详情 (topstories 返回的 id 已按分数排序, 保持顺序)
    def fetch_item(story_id: int) -> Optional[dict]:
        item = fetch(HN_FIREBASE_ITEM.format(story_id), timeout=10)
        if not item:
            return None
        try:
            return item.json()
        except Exception:  # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_item, sid): sid for sid in story_ids[:500]}
        for f in futures:
            hit = f.result()
            if not hit:
                continue
            obj_id = hit.get("id")
            if not obj_id or obj_id in seen:
                continue
            # 仅保留 story 类型 + 7 天窗口内
            if hit.get("type") != "story":
                continue
            created = hit.get("time")
            if not created or created < window_start_epoch:
                continue
            title = hit.get("title") or ""
            if not title or not AI_RE.search(title):
                continue
            seen.add(obj_id)
            url_field = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
            created_iso = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            points = hit.get("score") or 0
            comments = hit.get("descendants") or 0
            out.append({
                "id": stable_id("hnfb", str(obj_id)),
                "site_id": "hackernews",
                "site_name": "Hacker News",
                "source": f"Hacker News ({points}pts, {comments}c)",
                "title": title,
                "url": url_field,
                "published_at": created_iso,
                "first_seen_at": created_iso,
                "last_seen_at": iso(now),
                "title_original": title,
                "title_en": title,
                "title_zh": None,
                "title_bilingual": title,
            })
    print(f"[hnfb] {len(out)} stories", file=sys.stderr)
    return out


# ─── Source 2: RSS feeds (AI labs, tech media, arXiv, Reddit) ─────────────────

# slug -> (显示名, RSS URL, filter_ai, max_items)
#   filter_ai=False — feed 已全 AI (lab blog / AI-tagged / arXiv 分类)
#   filter_ai=True  — 通用资讯流 (Ars/MIT TR/The Verge), 需 AI_RE 标题过滤
#   max_items       — 每源保留上限; None = 窗口内全取
# 每个 feed 独立解析, 失败是警告不是致命错误。
RSS_FEEDS: dict[str, tuple[str, str, bool, Optional[int]]] = {
    # ── AI labs ─────────────────────────────────────────────
    "openai":         ("OpenAI",          "https://openai.com/news/rss.xml",                                                False, None),
    "anthropic":      ("Anthropic",       "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml", False, None),
    "deepmind":       ("Google DeepMind", "https://deepmind.google/blog/rss.xml",                                           False, None),
    "googleai":       ("Google Research", "https://research.google/blog/rss/",                                              False, None),
    "googleaiblog":   ("Google AI Blog",  "https://blog.google/technology/ai/rss/",                                         False, None),
    "appleml":        ("Apple ML",        "https://machinelearning.apple.com/rss.xml",                                      False, None),
    "huggingface":    ("Hugging Face",    "https://huggingface.co/blog/feed.xml",                                           False, None),
    # ── Tech media (AI-tagged) ──────────────────────────────
    "techcrunch_ai":  ("TechCrunch AI",   "https://techcrunch.com/category/artificial-intelligence/feed/",                  False, None),
    "wired_ai":       ("Wired AI",        "https://www.wired.com/feed/tag/ai/latest/rss",                                   False, None),
    "venturebeat":    ("VentureBeat",     "https://venturebeat.com/feed/",                                                   True,  None),
    # ── Tech media (general, 需关键词过滤) ──────────────────
    "theverge":       ("The Verge",       "https://www.theverge.com/rss/index.xml",                                         True,  None),
    "arstechnica":    ("Ars Technica",    "https://feeds.arstechnica.com/arstechnica/index",                                True,  None),
    "mit_tr":         ("MIT Tech Review", "https://www.technologyreview.com/feed/",                                         True,  None),
    # ── arXiv (每分类限 60, 否则论文淹没其他源) ─────────────
    "arxiv_ai":       ("arXiv cs.AI",     "https://export.arxiv.org/rss/cs.AI",                                             False, 60),
    "arxiv_lg":       ("arXiv cs.LG",     "https://export.arxiv.org/rss/cs.LG",                                             False, 60),
    "arxiv_cl":       ("arXiv cs.CL",     "https://export.arxiv.org/rss/cs.CL",                                             False, 60),
    "arxiv_cv":       ("arXiv cs.CV",     "https://export.arxiv.org/rss/cs.CV",                                             False, 60),
    "arxiv_sm":       ("arXiv stat.ML",   "https://export.arxiv.org/rss/stat.ML",                                           False, 60),
    "arxiv_ne":       ("arXiv cs.NE",     "https://export.arxiv.org/rss/cs.NE",                                             False, 60),
    # ── Reddit (RSS, 会拒绝通用 UA — 静默降级为 0 条) ────────
    "r_mlearning":    ("r/MachineLearning", "https://www.reddit.com/r/MachineLearning/.rss",                                False, 40),
    "r_localllama":   ("r/LocalLLaMA",      "https://www.reddit.com/r/LocalLLaMA/.rss",                                     False, 40),
    # ── Newsletters / analyst blogs ─────────────────────────
    "import_ai":      ("Import AI",        "https://jack-clark.net/feed/",                                                  False, None),
    "chip_huyen":     ("Chip Huyen",       "https://huyenchip.com/feed.xml",                                                False, None),
    "bensbites":      ("Ben's Bites",      "https://www.bensbites.com/feed",                                                False, None),
    "latentspace":    ("Latent Space",     "https://www.latent.space/feed",                                                 False, None),
}


def parse_rss_dt(entry) -> Optional[datetime]:
    for field in ("published_parsed", "updated_parsed"):
        v = getattr(entry, field, None) or (entry.get(field) if hasattr(entry, "get") else None)
        if v:
            try:
                return datetime(*v[:6], tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                pass
    return None


def fetch_rss_feed(slug: str, name: str, url: str, filter_ai: bool,
                   max_items: Optional[int], window_start: datetime, now: datetime) -> list[dict]:
    r = fetch(url, timeout=15)
    if not r:
        return []
    try:
        feed = feedparser.parse(r.content)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {slug} parse error: {e}", file=sys.stderr)
        return []
    out: list[dict] = []
    skipped_off_topic = 0
    for entry in feed.entries:
        if max_items is not None and len(out) >= max_items:
            break
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        # 清理 RSS 标题中的 HTML 实体与标签
        title = html.unescape(re.sub(r"<[^>]+>", "", title))
        if filter_ai and not is_ai_title(title):
            skipped_off_topic += 1
            continue
        dt = parse_rss_dt(entry)
        if not dt:
            continue
        # 只强制下界, 从不信任上游的未来时间戳
        if dt < window_start:
            continue
        out.append({
            "id": stable_id(slug, link),
            "site_id": slug,
            "site_name": name,
            "source": name,
            "title": title,
            "url": link,
            "published_at": iso_seconds(dt),
            "first_seen_at": iso_seconds(dt),
            "last_seen_at": iso(now),
            "title_original": title,
            "title_en": title,
            "title_zh": None,
            "title_bilingual": title,
        })
    extras = []
    if skipped_off_topic:
        extras.append(f"{skipped_off_topic} off-topic")
    if max_items is not None and len(out) >= max_items:
        extras.append(f"capped at {max_items}")
    suffix = f" ({', '.join(extras)})" if extras else ""
    print(f"[rss:{slug}] {len(out)} items{suffix}", file=sys.stderr)
    return out


def fetch_all_rss_feeds(window_start: datetime, now: datetime) -> list[dict]:
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [
            ex.submit(fetch_rss_feed, slug, name, url, filter_ai, max_items, window_start, now)
            for slug, (name, url, filter_ai, max_items) in RSS_FEEDS.items()
        ]
        for f in futures:
            out.extend(f.result())
    return out


# ─── Source 3: GitHub Trending (daily snapshot) ───────────────────────────────

TRENDING_LANGS = ["", "python", "typescript", "javascript", "rust", "go"]


def parse_trending(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows = soup.select("article.Box-row")
    out: list[dict] = []
    for row in rows:
        a = row.select_one("h2 a")
        if not a:
            continue
        href = a.get("href", "").strip()
        if not href.startswith("/"):
            continue
        repo = href.lstrip("/")
        url = f"https://github.com/{repo}"
        desc_el = row.select_one("p")
        desc = desc_el.get_text(strip=True) if desc_el else ""
        title = f"{repo} — {desc}" if desc else repo
        out.append({"repo": repo, "url": url, "title": title, "desc": desc})
    return out


def fetch_github_trending(now: datetime) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for lang in TRENDING_LANGS:
        url = f"https://github.com/trending/{lang}?since=daily" if lang else "https://github.com/trending?since=daily"
        r = fetch(url, timeout=15)
        if not r:
            continue
        for repo in parse_trending(r.text):
            if repo["repo"] in seen:
                continue
            haystack = repo["title"] + " " + repo.get("desc", "")
            if not AI_RE.search(haystack):
                continue
            seen.add(repo["repo"])
            now_iso = iso(now)
            out.append({
                "id": stable_id("ghtrend", repo["repo"]),
                "site_id": "github-trending",
                "site_name": "GitHub Trending",
                "source": "GitHub Trending",
                "title": repo["title"],
                "url": repo["url"],
                # Trending 无稳定发布时间戳 → 用当前运行时间; 前端 itemTs()
                # 在 published_at 为空时回退 first_seen_at, 归入今天。
                "published_at": None,
                "first_seen_at": now_iso,
                "last_seen_at": now_iso,
                "title_original": repo["title"],
                "title_en": repo["title"],
                "title_zh": None,
                "title_bilingual": repo["title"],
            })
        time.sleep(0.3)  # GitHub 无官方 trending API, 做礼貌爬虫
    print(f"[ghtrend] {len(out)} repos", file=sys.stderr)
    return out


# ─── Source 4: aihot.virxact.com curated picks (LLM-normalized titles) ────────

AIHOT_FEED_URL = "https://aihot.virxact.com/feed.xml"
AIHOT_USER_AGENT = "EchoBird-PulseBuilder/1.0 (+https://github.com/edison7009/EchoBird)"


def fetch_aihot_entries() -> list:
    try:
        r = requests.get(
            AIHOT_FEED_URL,
            headers={"User-Agent": AIHOT_USER_AGENT, "Accept": "application/rss+xml"},
            timeout=20,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"::warning::aihot fetch failed: {e}", file=sys.stderr)
        return []
    feed = feedparser.parse(r.content)
    if feed.bozo:
        print(f"::warning::aihot feed parse warning: {feed.bozo_exception}", file=sys.stderr)
    return list(feed.entries)


def aihot_to_news_item(entry, now_iso: str) -> Optional[dict]:
    url = (entry.get("link") or "").strip()
    title = (entry.get("title") or "").strip()
    if not url or not title:
        return None
    pub_at: Optional[str] = None
    pp = entry.get("published_parsed")
    if pp:
        try:
            pub_at = iso_seconds(datetime(*pp[:6], tzinfo=timezone.utc))
        except (TypeError, ValueError):
            pub_at = entry.get("published") or None
    guid = entry.get("id") or entry.get("guid") or url
    item_id = stable_id(guid)
    # <author> 形如 "noreply@aihot.virxact.com (IT之家（RSS）)" — 括号内是原始来源名
    author = (entry.get("author") or "").strip()
    if "(" in author and author.endswith(")"):
        source = author.rsplit("(", 1)[1].rstrip(")").strip() or "AI HOT"
    else:
        source = author or "AI HOT"
    return {
        "id": item_id,
        "site_id": "aihot",
        "site_name": "AI HOT 精选",
        "source": source,
        "title": title,
        "url": url,
        "published_at": pub_at,
        "first_seen_at": now_iso,
        "last_seen_at": now_iso,
        "title_original": title,
        "title_zh": title,
        "title_en": None,
        "title_bilingual": title,
    }


def merge_aihot(items: list[dict], now: datetime) -> tuple[list[dict], int, int]:
    """把 aihot 精选按 URL 合并进现有 items。

    同 URL 出现时 aihot 胜出 (LLM 规范化中文标题更干净), 并保留原条目的
    发现时间戳。返回 (新列表, added, overrode)。失败非致命: 返回原列表。
    """
    existing = list(items)
    suyxh_first_idx: dict[str, int] = {}
    for idx, it in enumerate(existing):
        u = it.get("url") or ""
        if u and u not in suyxh_first_idx:
            suyxh_first_idx[u] = idx

    now_iso = iso(now)
    entries = fetch_aihot_entries()
    added = 0
    overrode = 0
    for entry in entries:
        item = aihot_to_news_item(entry, now_iso)
        if not item:
            continue
        url = item["url"]
        if url in suyxh_first_idx:
            existing_item = existing[suyxh_first_idx[url]]
            existing_item["title"] = item["title"]
            existing_item["title_zh"] = item["title"]
            existing_item["title_original"] = item["title"]
            existing_item["title_bilingual"] = item["title"]
            existing_item["site_id"] = "aihot"
            existing_item["site_name"] = "AI HOT 精选"
            overrode += 1
        else:
            existing.append(item)
            suyxh_first_idx[url] = len(existing) - 1
            added += 1
    return existing, added, overrode


def dedupe_by_url(items: Iterable[dict]) -> list[dict]:
    by_url: dict[str, dict] = {}
    for it in items:
        u = it["url"]
        if u not in by_url:
            by_url[u] = it
    return list(by_url.values())
