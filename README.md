# EchoBird AI Pulse — 独立 AI 新闻/明星项目数据模块

从 [EchoBird](https://github.com/edison7009/EchoBird) (Tauri + Rust 的 AI 部署/管理桌面应用, MIT License) 抽取的 **AI News (AiPulse)** 功能, 独立成纯 Python 模块。前端展示层是 React (Tauri), 本模块专注**数据管道 + 磁盘归档**, 两者通过标准 JSON schema 解耦 —— 你可以把产物接入任何前端 (React/Vue/静态站) 或 API 服务。

## 功能

| 能力 | 说明 |
|---|---|
| 多源采集 | HN Algolia API (20 关键词) + 18 个 RSS (OpenAI/Anthropic/DeepMind/HF/TechCrunch/Wired/arXiv×4/Reddit×2/Import AI/Chip Huyen…) + GitHub Trending (6 语言) |
| aihot 精选 | 合并 [aihot.virxact.com](https://aihot.virxact.com/) 人工精选 (~80/天, LLM 规范化中文标题), 按 URL 去重、精选标题优先 |
| 清洗 | x.com/twitter/v2ex 主机黑名单 + 社区公告/广告/推广标题黑名单 + 未来时间戳修复 (北京时区错标 UTC 问题) |
| 置顶 | 通过计算 `published_at` 在排序中精确落位 (原理见 `pulse/inject_pinned.py`) |
| 磁盘归档 | Rust `pulse_archive` 的 Python 移植: `~/.echobird-pulse/pulse/YYYY/MM/DD_{lang}.json` 按日分桶、原子写入、URL 去重、跨日合并 |
| 标准输出 | schema 与上游 [SuYxh/ai-news-aggregator](https://github.com/SuYxh/ai-news-aggregator) 兼容, 前端仅凭文件名切换中/英 |

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows; macOS/Linux: .venv/bin/pip
# 或直接: pip install -r requirements.txt (用你的全局 Python)

# ⚠️ 国内网络必须走代理 (实测 16 个数据源直连全部超时):
#     V2rayN/Clash 等开启后, 先设置环境变量 (requests 自动读取):
export HTTPS_PROXY=http://127.0.0.1:10808 HTTP_PROXY=http://127.0.0.1:10808
#     GitHub Actions (海外 runner) 不需要 — 直接跑即可。

# 1) 构建英文 feed (HN + RSS + GitHub Trending + aihot)
python -m pulse build --output docs/pulse/latest-7d-en.json

# 2) 镜像上游中文 feed → 并入 aihot 精选 → 黑名单清洗
python -m pulse fetch-zh --output docs/pulse
python -m pulse merge-zh docs/pulse/latest-7d.json
python -m pulse filter docs/pulse/latest-24h.json docs/pulse/latest-7d.json

# 3) 置顶注入 (zh 24h / zh 7d / en 7d)
python -m pulse pin docs/pulse/latest-24h.json --lang zh
python -m pulse pin docs/pulse/latest-7d.json  --lang zh
python -m pulse pin docs/pulse/latest-7d-en.json --lang en

# 4) 归档 feed 到按日分桶
python -m pulse archive docs/pulse/latest-7d-en.json --lang en

# 5) 读取归档 / 查看日期 / 本地静态预览
python -m pulse load --lang en --limit 5
python -m pulse dates --lang en
python -m pulse serve --port 8765
```

## CLI 一览

```
python -m pulse build     # 构建 EN feed (--days 7 --no-aihot --pins pulse_pinned.json)
python -m pulse fetch-zh  # 镜像 SuYxh 中文 feed (纯下载)
python -m pulse merge-zh  # aihot 精选并入中文 feed (按 URL 合并, 精选标题优先)
python -m pulse filter    # 黑名单清洗 + 未来时间戳修复 (24h/7d 中文 feed)
python -m pulse pin       # 置顶注入 (--lang zh|en, 排序位置精确控制)
python -m pulse archive   # feed JSON → 磁盘按日归档
python -m pulse load      # 读取归档条目
python -m pulse dates     # 归档日期 + 条数
python -m pulse serve     # 实验性静态预览
```

## 目录结构

```
pulse/
├── __init__.py       # 包声明
├── models.py         # NewsItem / RawFeed dataclass (schema 兼容)
├── common.py         # HTTP、AI 关键词、黑名单、时间/ID 工具
├── sources.py        # 4 类采集器 (HN / RSS / Trending / aihot)
├── filters.py        # 黑名单清洗 + 未来时间戳修复
├── inject_pinned.py  # 置顶注入 (排序位置控制)
├── pipeline.py       # 构建主流程
├── archive.py        # 磁盘归档 (Rust pulse_archive 移植)
└── cli.py            # CLI 入口
pulse_pinned.json     # 置顶条目配置
.github/workflows/refresh-pulse-data.yml  # 每 2h 定时刷新 (仅变化时提交)
```

## 输出 schema

```json
{
  "generated_at": "2026-08-05T03:31:34.000Z",
  "window_hours": 168,
  "total_items": 1057,
  "site_count": 18,
  "source_count": 158,
  "site_stats": { "hackernews": 705, "arxiv_lg": 60, "github-trending": 36, ... },
  "items": [
    {
      "id": "sha1...", "site_id": "hackernews", "site_name": "Hacker News",
      "source": "Hacker News (342pts, 156c)", "title": "...", "url": "...",
      "published_at": "2026-08-05T03:31:34.000Z",
      "first_seen_at": "...", "last_seen_at": "...",
      "title_original": "...", "title_en": "...", "title_zh": null, "title_bilingual": "..."
    }
  ]
}
```

## 前端消费建议 (来自 EchoBird AiPulse.tsx 的设计)

- **镜像链**: 生产环境用 Cloudflare Worker 透传 + 边缘缓存 + CORS open (`infra/pulse-worker` 是原版参考), 前端多镜像 fallback (echobird.ai → jsDelivr → raw.githubusercontent)
- **刷新节流**: 30 分钟最小刷新间隔, localStorage 存 `lastFetched`
- **新闻/项目分流**: URL 在 github.com 或 HF spaces/models/datasets → 明星项目, 否则新闻
- **语言检测**: 标题含 CJK → zh, 否则 en
- **相对时间**: `published_at` 未来 5 分钟内视为错标, 回退 `first_seen_at`

## 部署 (照抄 EchoBird 三层架构)

```
┌─ 数据层 ──┐   ┌─ 分发层 ──┐   ┌─ 消费层 ──┐
│ GitHub 仓库 │→│ jsDelivr / │→│ 网页 / App │
│ Actions 每2h│ │ Pages / CF │ │ 多镜像 fallback│
│ 海外采集+提交│ │ 静态文件 CDN │ │ 零代理读取 │
└───────────┘   └───────────┘   └───────────┘
```

### 第 1 步：建 GitHub 仓库并推送

```bash
# 本地已 git init (main 分支)。在 GitHub 新建空仓库后:
git remote add origin https://github.com/<你的用户名>/echobird-pulse.git
git add -A
git commit -m "feat: EchoBird AI Pulse 独立模块"
git push -u origin main
```

> ⚠️ 推送前需要有效认证。当前 `scripts/.env` 的 GH_TOKEN 已失效 (401)，
> 重新生成后可用 `gh auth login` 或 HTTPS PAT 方式推送。

### 第 2 步：workflow 自动采集 (已就绪, 推送即生效)

`.github/workflows/refresh-pulse-data.yml` 每 2 小时在 GitHub 海外 runner 上:
1. 下载 SuYxh 中文 feed → 2. 合并 aihot 精选 → 3. 黑名单清洗
4. 构建英文 feed → 5. 置顶注入 → 6. 内容变化才提交
- 首次推送后手动触发一次: Actions 页 → Refresh Pulse Data → Run workflow
- 零代理 (海外 runner 直连), 零成本 (GitHub Actions 免费额度)

### 第 3 步：分发 (推荐 jsDelivr, 零部署)

产物提交到仓库后, jsDelivr CDN 自动代理 (实测国内直连 200):

```
https://cdn.jsdelivr.net/gh/<用户名>/echobird-pulse@main/docs/pulse/latest-7d-en.json
https://cdn.jsdelivr.net/gh/<用户名>/echobird-pulse@main/docs/pulse/latest-7d.json
https://cdn.jsdelivr.net/gh/<用户名>/echobird-pulse@main/docs/pulse/latest-24h.json
```

验证: `curl -s https://cdn.jsdelivr.net/gh/<用户名>/echobird-pulse@main/docs/pulse/latest-7d-en.json | head -c 200`

可选进阶 (原版用了 echobird.ai Cloudflare Worker):
- **GitHub Pages**: 仓库 Settings → Pages → 部署分支 main → `https://<用户名>.github.io/echobird-pulse/docs/pulse/latest-7d-en.json`
- **Cloudflare Pages/Worker**: 连接仓库自动部署, 自定义域名 + 边缘缓存 (参考原版 `infra/pulse-worker/`)

### 第 4 步：前端消费 (多镜像 fallback, 原版 AiPulse 同款逻辑)

```js
const MIRRORS = [
  `https://cdn.jsdelivr.net/gh/<用户名>/echobird-pulse@main/docs/pulse`,
  `https://raw.githubusercontent.com/<用户名>/echobird-pulse/main/docs/pulse`,
  // 国内用户首选 jsDelivr; 海外可加 raw.githubusercontent
];

async function fetchFeed(lang) {
  const file = lang === 'zh' ? 'latest-7d.json' : 'latest-7d-en.json';
  for (const base of MIRRORS) {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 10000);
      const res = await fetch(`${base}/${file}`, { signal: ctrl.signal });
      const text = await res.text();
      if (!text.trim().startsWith('{')) continue;      // HTML 错误页
      const data = JSON.parse(text);
      if (data.items?.length) return data;             // 首个可用的镜像
    } catch { /* 换下一个镜像 */ }
  }
  return null;
}
```

- 30 分钟最小刷新间隔 (localStorage 存 `lastFetched`)
- 新闻/项目分流: URL 是 github.com 或 HF spaces/models/datasets → 明星项目, 否则新闻
- 标题含 CJK → zh, 否则 en

## 与原版差异

- Rust `pulse_archive` → Python `archive.py` (行为一一对应)
- `build_en_pulse.py` / `filter_pulse.py` / `merge_aihot.py` / `inject_pinned.py` 四个脚本 → `pulse/` 包内模块化，CLI 子命令对齐原版 workflow 的 6 步处理链 (`fetch-zh` → `merge-zh` → `filter` → `build` → `pin` → commit)
- GitHub Actions workflow 改用 `python -m pulse` 命令，步骤与原版一一对应
- 归档根目录从 `~/.echobird/pulse` 改为 `~/.echobird-pulse/pulse` (可用 `--root` 覆盖)
- en feed 默认也合并 aihot 精选 (原版仅 zh feed 合并；可用 `--no-aihot` 关闭)

## License

MIT (与 EchoBird 相同)。数据源版权归各上游所有。
