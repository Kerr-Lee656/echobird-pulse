# EchoBird AI Pulse — 独立模块
#
# 从 EchoBird (https://github.com/edison7009/EchoBird) 抽取的 AI 新闻/明星项目
# 数据管道，独立成 Python 包。MIT License 兼容（原仓库 MIT）。
#
# 功能:
#   1. 多源采集   — HN Algolia API + 18 个 RSS (AI lab/媒体/arXiv/Reddit)
#                    + GitHub Trending + aihot 人工精选
#   2. 清洗增强   — 主机/标题黑名单 + 未来时间戳修复 + aihot 标题覆盖
#   3. 置顶       — 按排序位置注入自有置顶条目 (published_at 时间戳控制)
#   4. 磁盘归档   — ~/.echobird-pulse/YYYY/MM/DD_{lang}.json 按日分桶、
#                    原子写入、URL 去重、跨日合并 (Rust pulse_archive 的 Python 移植)
#   5. 标准输出   — docs/pulse/latest-7d-en.json (schema 与上游 SuYxh 兼容)

__version__ = "1.0.0"
