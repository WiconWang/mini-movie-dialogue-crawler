# 台词输出规范

## JSONL 格式（每行一个 JSON 对象，UTF-8 无 BOM）

{"speaker": "派蒙", "text": "看！天上那个就是「群玉阁」了。"}
{"speaker": "？？？", "text": "住手！何事喧哗？"}
{"speaker": null, "text": "旁白或无说话人的台词。"}
{"speaker": "旅行者", "text": "我觉得不是这样……", "voiced": false}

## 字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `text` | 是 | 台词原文，只清除 wikitext 标记，不做改写 |
| `speaker` | 否 | 说话人，无则 null。`？？？` 等未揭晓角色原样保留 |
| `voiced` | 否 | 默认 true。false = 此行无配音（旅行者选项行） |

## 硬性规则

1. 只收有配音的台词行——无配音的选项文本按 voiced: false 保留
2. 顺序即剧情顺序——行的先后与源页面顺序一致
3. 分支台词全收——互斥分支的台词按文稿顺序全部收录
4. 一行一句，按句拆分——以 。！？；… 为句末标点拆分
5. 禁止混入场景描述、动作提示、章节标题（这些放入 .meta.json 的 sections）

## .meta.json 元数据

{
  "sections": [{"title": "前往骑士团", "start_line": 1}],
  "characters": ["派蒙", "刻晴"],
  "source_url": "https://wiki.biligame.com/ys/页面名"
}

## voiced 判定依据

biligame wiki 的 wikitext 中，冒号格式天然区分配音状态：

- *派蒙 : 台词（ASCII 冒号 + 空格）-> NPC 有配音
- *旅行者：台词（全角冒号）-> 旅行者无配音

脚本据此自动标注 voiced 字段。
