---
name: mini-movie-dialogue-crawler
description: >
  从游戏 wiki 采集剧情台词，解析 wikitext 并输出符合 mini-movie-maker 物料规范的 JSONL 台词文件。
  支持多源站路由：根据用户提供的 URL 域名自动判断源站，调用对应适配器脚本。
  当前已适配源站：biligame wiki（wiki.biligame.com，原神/崩坏等）。
  当用户需要：从 wiki 抓取剧情对话/台词、采集原神剧情文案、
  将 wiki 台词转为 JSONL、批量获取多个任务页面的对话内容时使用。
  触发词：biligame、wiki台词、剧情文案、对话采集、台词JSONL、原神台词、
  采集剧情、抓取对话、任务页面、子任务页面、mini-movie-dialogue-crawler。
---

# 台词采集器

从游戏 wiki 采集剧情台词，解析 wikitext 并输出符合物料规范的 JSONL 文件。

## 源站路由

根据用户提供的 URL 域名判断源站，调用对应适配器脚本。未适配的源站直接告知用户暂不支持。

| 域名匹配 | 适配器脚本 | 状态 |
|----------|-----------|------|
| `wiki.biligame.com` | `scripts/biligame_dialogue_crawler.py` | ✅ 已适配 |

> 后续新增源站时，在 `scripts/` 下新增 `{source}_dialogue_crawler.py`，并在上表添加一行即可。

## biligame 适配器用法

`scripts/biligame_dialogue_crawler.py` —— 纯 Python 3 标准库实现，无第三方依赖。

### 三种模式

#### 1. list —— 探查索引页涉及多少子任务页面

```bash
python3 scripts/biligame_dialogue_crawler.py list "<索引页URL>"
```

#### 2. index —— 索引页模式（主用法）

自动发现索引页下所有子任务页面，逐页抓取台词并合并为一个 JSONL 文件。

```bash
# 独立使用：-o downloads/任务名.jsonl
# 集成进 game-storyline-pipeline 管线时，输出到暂存再登记（每 quest 一份 dialog）：
python3 scripts/biligame_dialogue_crawler.py index "<索引页URL>" \
    -o "/tmp/{quest_slug}.jsonl"
# 登记进统一台账（落 {game}/{version}/{quest_slug}/dialog/，sections/characters/source_url 进 ledger meta_json）：
mmm add-asset --game <code> --version <no> --slug <quest_slug> \
    --kind dialog --src "/tmp/{quest_slug}.jsonl" \
    --source-url "<索引页URL>"
```

同时生成同名 `.meta.json`（含 sections、characters、source_url）——登记时其内容并入 ledger `meta_json`，伴生文件不入库。

#### 3. single —— 单页模式

```bash
python3 scripts/biligame_dialogue_crawler.py single "<详情页URL>" -o downloads/任务名.jsonl
```

## 典型工作流

1. 用户提供一个 wiki 链接和任务描述
2. **判断源站**：根据 URL 域名查路由表，未适配则告知用户暂不支持
3. 先运行 `list` 模式，确认涉及多少个子任务页面
4. 向用户报告页面数量和页面名，确认无误
5. 运行 `index` 模式（或 `single` 若只有一页），输出 JSONL + meta.json（独立使用输出 `downloads/`，集成管线输出 `/tmp/` 暂存后 `mmm add-asset --kind dialog` 登记）
6. 检查输出质量：行数、`voiced` 分布、超长行（登记时 `add-asset` 自动预检）

## 源站结构要点（biligame wiki）

页面分三层，`index` 模式会自动逐层下钻（最多 2 层，不用手工指定详情页）：

- **总系列页**：`{{多重系列任务}}` 模板（如 `天下人之章`），下挂多个章节页
- **章节页**：`{{系列任务}}` 模板（如 `影照浮世风流`），下挂多个任务详情页
- **详情页**：`{{任务}}` 模板，实际台词所在，格式为：
  - `*说话人 : 台词`（ASCII 冒号）= NPC 有配音
  - `*旅行者：台词`（全角冒号）= 旅行者无配音
  - `{{剧情选项|选项1=旅行者：…|剧情1=NPC : …}}` = 分支选项（模板值内可能自带 `*` 列表标记）
  - `=====◆节点名=====` = 任务流程节点（非台词，记入 sections）

> 索引页的原始 wikitext **不含**子页面链接——子页面只出现在渲染后的导航框里。
> `discover_subpages_from_links()` 因此走 `action=parse&prop=links` 取候选，
> 再用「`{{任务}}`/`{{系列任务}}` 模板 + `|系列任务=` 含本页名」双重过滤，剔除 NPC/道具等噪音链接。

### 站点限制（重要）

biligame 的 WAF **强制校验 `Referer`**：缺失即无条件返回 `HTTP 567`（与「页面不存在」同码，
极易误判成页面不存在）。脚本已内置 `wiki_headers()` 统一带 Referer/UA/Accept，
并对 403/429/567 做 3 次退避重试。

## 输出规范

详见 [references/output-spec.md](references/output-spec.md)。核心：JSONL 格式，字段为 `text`/`speaker`/`voiced`，按句末标点拆分，旅行者台词标 `voiced: false`。

## 注意事项

- biligame 适配器使用 MediaWiki API（`/api.php`），获取的是原始 wikitext，无需解析 HTML
- **但站点有 WAF**：必须带 `Referer`，否则一律 567（见上「站点限制」）
- 脚本需要网络访问权限（访问 wiki API）
- 多页面抓取时脚本内置 0.5s 礼貌延迟
- `？` 单独成行或 `……？` 组合属于原文如此，保留不合并
