"""公共工具 — HTTP 抓取、AI 关键词过滤、主机黑名单、ID/时间格式化。"""
from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

HTTP_TIMEOUT = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; EchoBird-PulseBuilder/1.0)",
    "Accept": "application/json, application/rss+xml, text/html;q=0.9, */*;q=0.5",
}

# AI 关键词集合 — 用于 HN 故事与 GitHub Trending 仓库过滤。
# 保持宽松: 前端有全量新闻视图, 过度收录代价低, 漏收录会丢好内容。
AI_KEYWORDS = [
    "AI", "A.I.", "AGI", "LLM", "GPT", "ChatGPT", "Claude", "Gemini",
    "OpenAI", "Anthropic", "DeepMind", "HuggingFace", "Hugging Face",
    "Mistral", "Llama", "Grok", "transformer", "neural network",
    "machine learning", "deep learning", "diffusion", "stable diffusion",
    "midjourney", "RAG", "fine-tuning", "embedding", "agent", "agentic",
    "MCP", "vibe coding", "copilot", "cursor", "codex",
]
AI_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in AI_KEYWORDS) + r")\b", re.IGNORECASE
)

# 主机黑名单: x.com / twitter.com 是 KOL 帖子, v2ex.com 是论坛闲聊。
# 与 filter_pulse 保持一致 (两者都应用于 ZH feed)。
BLOCKED_HOST_RE = re.compile(
    r"^https?://([^/]+\.)?(x|twitter|v2ex)\.com/", re.IGNORECASE
)

# 标题黑名单: 社区公告 + 付费/推广标记。
# 刻意收窄, 避免误杀正文讨论广告/赞助的正当文章。
BLOCKED_TITLE_RE = re.compile(
    r"(?:"
    r"社区公告"
    r"|[【\[](?:广告|推广|赞助|AD|PR|Sponsored)[】\]]"
    r"|^(?:广告|推广|赞助)[:：\s|]"
    r")",
    re.IGNORECASE,
)


def fetch(url: str, *, timeout: int = HTTP_TIMEOUT, headers: Optional[dict] = None) -> Optional[requests.Response]:
    """GET 一个 URL, 仅返回 200 响应; 失败打印警告并返回 None。"""
    try:
        r = requests.get(url, headers=headers or HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r
        print(f"[warn] {url} → HTTP {r.status_code}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {url} → {e}", file=sys.stderr)
    return None


def stable_id(*parts: str) -> str:
    """确定性 SHA1(以 | 连接 parts)。匹配上游 schema。"""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def iso(dt: datetime) -> str:
    """UTC ISO-8601, 毫秒精度, Z 后缀 — 与上游/前端解析器兼容。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_seconds(dt: datetime) -> str:
    """UTC ISO-8601, 秒精度 .000Z — 用于归档/发布时间的常规格式。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def is_ai_title(title: str) -> bool:
    """标题是否命中 AI 关键词 (用于通用资讯源过滤)。"""
    return bool(AI_RE.search(title))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """宽容 ISO 解析: 支持 Z 后缀, 无时区视为 UTC。失败返回 None。"""
    if not value:
        return None
    s = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def effective_ts(item: dict, now_plus_slack_ts: Optional[int] = None) -> str:
    """前端 itemTs()/Rust effective_ts 的 Python 镜像:
    当 published_at 解析为 > now+5min (上游时区标记错误) 时回退到
    first_seen_at → last_seen_at → 原文。"""
    pub = item.get("published_at") or ""
    if pub:
        dt = parse_iso(pub)
        if dt is not None:
            if now_plus_slack_ts is None:
                now_plus_slack_ts = int(datetime.now(timezone.utc).timestamp()) + 5 * 60
            if dt.timestamp() > now_plus_slack_ts:
                for key in ("first_seen_at", "last_seen_at"):
                    v = item.get(key)
                    if v:
                        return v
                return pub
            return pub
    for key in ("first_seen_at", "last_seen_at"):
        v = item.get(key)
        if v:
            return v
    return ""
