"""deepdive — 重点资讯深度报告（2026-08-18 用户拍板：标题筛选 → 正文采集 → 详细报告）

三步流水线（跑在 echobird-pulse 采集端，feed 构建后/独立 CLI 均可触发）：
  1. title_filter: LLM 批量筛标题 → 返回值得深挖的候选 (top 每天 ~10-15 条)
  2. fetch_article: trafilatura 抓正文（本地免费，失败自动跳过）
  3. gen_report: LLM 生成结构化深度报告（背景/核心要点/影响分析/相关工具建议）
     → 写 docs/pulse/deepdive/<sha>.json + 汇总索引 docs/pulse/deepdive/index.json

输出 schema（与 opentools 前端对齐）：
  {
    "id": sha256(url)[:12],
    "url": 原文链接, "title": 标题, "source": 来源,
    "published_at": ..., "fetched_at": ...,
    "content": "正文纯文本（截断 8000 字）",
    "report": {
      "summary": "一句话摘要",
      "background": "背景",
      "key_points": ["要点1", ...],
      "impact": "影响分析（对行业/开发者/普通用户）",
      "tools": "相关工具/资源建议（结合 OpenTools 场景）",
    }
  }
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── 配置 ──────────────────────────────────────────────
PULSE_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = PULSE_DIR / "docs" / "pulse"
DEEP_DIR = DOCS_DIR / "deepdive"
INDEX_FILE = DEEP_DIR / "index.json"

# DeepSeek（与 opentools ai-search 同源：C:\Users\25417\.codex\auth.json）
AUTH_FILE = Path(r"C:\Users\25417\.codex\auth.json")
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"

MAX_CONTENT_CHARS = 8000  # 正文截断
TITLE_BATCH = 60          # 每次筛选的标题数（2026-08-18 30→60：前 30 条可能全是同小时噪音）
MAX_DAILY = 15            # 每天最多深挖条数（成本控制）

PROXY = "http://127.0.0.1:10808"


def _api_key() -> str:
    # Actions 环境变量优先（本地无 auth.json 时）
    import os
    env_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if env_key:
        return env_key
    try:
        d = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        return d.get("OPENAI_API_KEY", "")
    except Exception:
        return ""


def _llm(messages: list[dict], max_tokens: int = 2000, timeout: int = 120) -> str:
    """调用 DeepSeek，带重试。代理优先、失败直连（2026-08-18：10808 代理未开时直连可达）。"""
    key = _api_key()
    if not key:
        raise RuntimeError("DeepSeek API key 未找到")
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }).encode()
    ctx = urllib.request.ssl.create_default_context()
    # 预构造两个 opener（代理 / 直连），轮换使用
    opener_proxy = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}),
        urllib.request.HTTPSHandler(context=ctx),
    )
    opener_direct = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    last_err: Optional[Exception] = None
    for attempt in range(4):
        opener = opener_proxy if attempt % 2 == 0 else opener_direct
        try:
            req = urllib.request.Request(API_URL, data=body, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            })
            with opener.open(req, timeout=timeout) as r:
                data = json.loads(r.read())
            content = data["choices"][0]["message"]["content"] or ""
            if content:
                return content
            raise RuntimeError("empty content")
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"LLM 调用失败: {last_err}")


# ── 步骤 1: 标题筛选 ─────────────────────────────────
# 抓不到正文的域名（微信公众号需验证码等）——筛选前直接过滤，让 LLM 只从可抓源里选
UNFETCHABLE_HOSTS = {
    "mp.weixin.qq.com", "weixin.qq.com",
}

def _host_of(url: str) -> str:
    if "//" in url:
        return url.split("/")[2].lower()
    return ""


def _fetchable(items: list[dict]) -> list[dict]:
    """过滤掉已知不可抓来源。"""
    return [it for it in items if _host_of(it.get("url", "")) not in UNFETCHABLE_HOSTS]


SCREEN_SYSTEM = """你是 AI 资讯主编。下面是一批 24 小时内出现的 AI 相关新闻标题。
请判断每条是否「值得深挖写深度报告」——标准：
- 值得：重大模型/产品发布、开源项目里程碑、政策监管、融资、行业格局变化、有长期影响的技术进展
- 不值得：日常更新、教程推广、软文、八卦、重复标题、纯产品促销

只输出 JSON：{"deep": ["值得深挖的标题原文，最多选 10 条", ...], "reason": "筛选依据一句话"}"""


def screen_titles(items: list[dict]) -> list[dict]:
    """LLM 筛标题 → 返回值得深挖且来源可抓的 items。
    2026-08-18 增强：分 2 批各 TITLE_BATCH 条覆盖更多条目；LLM 标题输出有随机性，两批都跑提高命中。"""
    items = _fetchable(items)  # 2026-08-18：先过滤微信等不可抓源
    if not items:
        return []
    picked_items: list[dict] = []
    seen_titles: set[str] = set()
    # 批次：按时间戳最新优先（feed 已排序），分 2 批
    batches = [items[:TITLE_BATCH], items[TITLE_BATCH : TITLE_BATCH * 2]]
    for batch in batches:
        if not batch:
            continue
        titles = [it.get("title", "")[:120] for it in batch if it.get("title")]
        if not titles:
            continue
        try:
            out = _llm([
                {"role": "system", "content": SCREEN_SYSTEM},
                {"role": "user", "content": "标题列表（每行一条）：\n" + "\n".join(f"- {t}" for t in titles)},
            ], max_tokens=4000)  # ⚠️ 推理模型：1200 被思维链占满 content 为空（记忆已知坑）
            m = re.search(r"\{[\s\S]*\}", out)
            picked = json.loads(m.group(0)) if m else {"deep": []}
            keep = set(picked.get("deep", []))
            for it in batch:
                t = it.get("title", "")[:120]
                if t in keep and t not in seen_titles:
                    seen_titles.add(t)
                    picked_items.append(it)
        except Exception:
            continue  # 单批失败不影响其他批
    return picked_items[:MAX_DAILY]


# ── 步骤 2: 正文采集 ─────────────────────────────────
def fetch_article(url: str) -> str:
    """trafilatura 抓正文纯文本；失败返回空串。"""
    import trafilatura
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return (text or "").strip()[:MAX_CONTENT_CHARS]
    except Exception:
        return ""


# ── 步骤 3: 深度报告生成 ─────────────────────────────
REPORT_SYSTEM = """你是 AI 领域的资深记者 + 技术分析师。基于一篇新闻的标题和正文，撰写结构化深度报告。

要求：
- summary: 一句话摘要（30字内）
- background: 背景（这个事件的前因后果、行业上下文，100-200字）
- key_points: 3-5 个核心要点（每条 40-80 字，用通俗语言）
- impact: 影响分析（对 AI 行业、开发者、普通用户分别意味着什么，100-150字）
- tools: 相关工具/开源项目建议（如果读者想跟进/上手，可以关注哪些工具，50-100字；没有可不写）

只输出 JSON：
{"summary": "...", "background": "...", "key_points": ["...", "..."], "impact": "...", "tools": "..."}"""


def gen_report(title: str, url: str, content: str) -> dict:
    out = _llm([
        {"role": "system", "content": REPORT_SYSTEM},
        {"role": "user", "content": f"标题：{title}\n原文链接：{url}\n\n正文：\n{content[:MAX_CONTENT_CHARS]}"},
    ], max_tokens=2500, timeout=180)
    try:
        m = re.search(r"\{[\s\S]*\}", out)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


# ── 主流程 ────────────────────────────────────────────
def item_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def run_deepdive(feed_file: str = "latest-24h.json", dry_run: bool = False) -> int:
    """入口：读 feed → 筛标题 → 抓正文 → 写报告。返回深挖条数。"""
    DEEP_DIR.mkdir(parents=True, exist_ok=True)
    feed_path = DOCS_DIR / feed_file
    if not feed_path.exists():
        print(f"[deepdive] feed 不存在: {feed_path}", file=__import__("sys").stderr)
        return 0

    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    items = feed.get("items", [])
    print(f"[deepdive] feed {len(items)} 条，开始标题筛选…")

    picked = screen_titles(items)
    print(f"[deepdive] 标题筛选命中 {len(picked)} 条值得深挖")
    if dry_run:
        for it in picked[:10]:
            print(f"  📄 {it.get('title', '')[:60]}")
        print(f"[deepdive] dry-run 结束（未抓正文未生成报告）")
        return 0

    done = 0
    index = {}
    if INDEX_FILE.exists():
        try:
            index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            index = {}

    for it in picked:
        url = it.get("url", "")
        if not url:
            continue
        iid = item_id(url)
        # 已有报告跳过（幂等）
        if iid in index:
            continue
        print(f"[deepdive] 抓正文: {it.get('title', '')[:40]}…")
        content = fetch_article(url)
        if len(content) < 200:
            print(f"  ⚠️ 正文过短/抓取失败 ({len(content)} 字)，跳过")
            continue
        print(f"  正文 {len(content)} 字，生成报告…")
        report = gen_report(it.get("title", ""), url, content)
        if not report:
            print("  ⚠️ 报告生成失败，跳过")
            continue

        entry = {
            "id": iid,
            "url": url,
            "title": it.get("title", ""),
            "source": it.get("source", ""),
            "site_name": it.get("site_name", ""),
            "published_at": it.get("published_at"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "content": content,
            "report": report,
        }
        (DEEP_DIR / f"{iid}.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        index[iid] = {
            "id": iid, "url": url, "title": it.get("title", ""),
            "source": it.get("source", ""), "site_name": it.get("site_name", ""),
            "published_at": it.get("published_at"), "fetched_at": entry["fetched_at"],
        }
        done += 1
        print(f"  ✅ 已生成: {iid}")

    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[deepdive] 完成：新增 {done} 篇深度报告（累计 {len(index)} 篇）")
    return done


if __name__ == "__main__":
    import argparse
    import sys
    p = argparse.ArgumentParser(description="重点资讯深度报告")
    p.add_argument("--feed", default="latest-24h.json", help="feed 文件名（docs/pulse 下）")
    p.add_argument("--dry-run", action="store_true", help="只筛选标题，不抓正文不生成")
    args = p.parse_args()
    sys.exit(0 if run_deepdive(args.feed, args.dry_run) >= 0 else 1)
