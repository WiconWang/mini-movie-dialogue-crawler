#!/usr/bin/env python3
"""biligame wiki 原神剧情台词采集器

从 wiki.biligame.com/ys 的 MediaWiki API 获取剧情页面 wikitext，
解析台词并输出符合物料规范（docs/2026/0817-物料规范.md §3）的 JSONL 文件。

用法:
  # 索引页模式：自动发现子任务页面，合并输出
  python3 biligame_dialogue_crawler.py index <URL或页面名> -o downloads/任务名.jsonl

  # 单页模式：直接解析一个详情页
  python3 biligame_dialogue_crawler.py single <URL或页面名> -o downloads/任务名.jsonl

  # 列出索引页涉及的子任务页面（不抓取台词）
  python3 biligame_dialogue_crawler.py list <URL或页面名>
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

API_BASE = "https://wiki.biligame.com/ys/api.php"

# 旅行者相关名称 -- 无配音
UNVOICED_SPEAKERS = {"旅行者", "空", "荧"}


# ============================================================
# API 层
# ============================================================

def fetch_wikitext(title_or_url):
    """通过 MediaWiki API 获取页面 wikitext，返回 (页面标题, wikitext)"""
    if title_or_url.startswith("http"):
        parsed = urllib.parse.urlparse(title_or_url)
        path = urllib.parse.unquote(parsed.path)
        parts = path.split("/", 2)
        if len(parts) < 3:
            raise ValueError(f"无法从 URL 提取页面名: {title_or_url}")
        title = parts[2]
    else:
        title = title_or_url

    title = urllib.parse.unquote(title)

    params = urllib.parse.urlencode({
        "action": "query",
        "format": "json",
        "prop": "revisions",
        "titles": title,
        "rvprop": "content",
        "rvslots": "main",
    })
    url = f"{API_BASE}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    pages = data.get("query", {}).get("pages", {})
    for pid, page in pages.items():
        if str(pid) == "-1":
            raise ValueError(f"页面不存在: {title}")
        revs = page.get("revisions", [])
        if revs:
            content = revs[0].get("slots", {}).get("main", {}).get("*", "")
            return page.get("title", title), content

    raise ValueError(f"无法获取页面内容: {title}")


def discover_subpages(wikitext):
    """从索引页 wikitext 中发现子任务详情页名称，保持出现顺序"""
    subpages = []
    seen = set()

    # 优先匹配 {{提示|蓝色|详细对话内容，请查阅词条[[页面名]]}}
    pattern = r"详细对话内容，请查阅词条\[\[([^\]|]+)(?:\|[^\]]+)?\]\]"
    for m in re.finditer(pattern, wikitext):
        title = m.group(1).strip()
        if title not in seen:
            subpages.append(title)
            seen.add(title)

    # 回退：从 {{传说任务|任务名称=页面名}} 提取
    if not subpages:
        pattern = r"任务名称\s*=\s*(.+?)(?:\n|\||}})"
        for m in re.finditer(pattern, wikitext):
            title = m.group(1).strip()
            title = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", title)
            if title not in seen:
                subpages.append(title)
                seen.add(title)

    return subpages


# ============================================================
# 解析层
# ============================================================

def clean_text(text):
    """清除 wikitext 标记，保留纯文本"""
    text = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"'{2,}", "", text)
    return text.strip()


def parse_speaker_line(content):
    """
    解析 *说话人 台词行，返回 (speaker, text, voiced) 或 None。
    规则:
      - "说话人 : 台词"（ASCII 冒号 + 空格）-> NPC 有配音
      - "说话人：台词"（全角冒号）-> 旅行者无配音
      - 说话人在 UNVOICED_SPEAKERS 中 -> 无配音
    """
    content = content.strip()
    if not content:
        return None

    # 先尝试 ASCII 冒号格式 "说话人 : 台词"
    m = re.match(r"^(.+?)\s*:\s*(.+)$", content)
    if m:
        speaker = m.group(1).strip()
        text = m.group(2).strip()
        voiced = speaker not in UNVOICED_SPEAKERS
        return (speaker, text, voiced)

    # 再尝试全角冒号格式 "说话人：台词"
    m = re.match(r"^(.+?)：(.+)$", content)
    if m:
        speaker = m.group(1).strip()
        text = m.group(2).strip()
        voiced = speaker not in UNVOICED_SPEAKERS
        return (speaker, text, voiced)

    return None


def parse_options_block(block_text):
    """
    解析 {{剧情选项|选项1=...|剧情1=...}} 模板。
    返回 [(speaker, text, voiced), ...]，按 选项1->剧情1->选项2->剧情2 顺序。
    """
    results = []
    options = {}
    plots = {}
    for m in re.finditer(r"\|选项(\d+)=(.*?)(?=\||\}\}|\n)", block_text):
        options[int(m.group(1))] = m.group(2).strip()
    for m in re.finditer(r"\|剧情(\d+)=(.*?)(?=\||\}\}|\n)", block_text):
        plots[int(m.group(1))] = m.group(2).strip()

    max_n = max(list(options.keys()) + list(plots.keys()) + [0])
    for n in range(1, max_n + 1):
        if n in options:
            parsed = parse_speaker_line(options[n])
            if parsed:
                results.append(parsed)
        if n in plots:
            parsed = parse_speaker_line(plots[n])
            if parsed:
                results.append(parsed)

    return results


def split_sentences(text):
    """
    按句末标点（。！？；…）拆分，保留标点。
    处理省略号 …… 和闭合引号/括号。
    """
    text = text.strip()
    if not text:
        return []

    terminators = set("。！？；…")
    close_brackets = set("」』）」』）")

    sentences = []
    current = []
    i = 0
    while i < len(text):
        char = text[i]
        current.append(char)

        if char in terminators:
            should_split = True
            if char == "…":
                while i + 1 < len(text) and text[i + 1] == "…":
                    i += 1
                    current.append(text[i])
                # …… 后面紧跟其他句末标点时不拆分（如 ……？），让后续标点触发
                if i + 1 < len(text) and text[i + 1] in "。！？；":
                    should_split = False
            if should_split:
                while i + 1 < len(text) and text[i + 1] in close_brackets:
                    i += 1
                    current.append(text[i])
                sentence = "".join(current).strip()
                if sentence:
                    sentences.append(sentence)
                current = []

        i += 1

    remaining = "".join(current).strip()
    if remaining:
        sentences.append(remaining)

    return sentences


def parse_wikitext_dialogues(wikitext):
    """
    解析 wikitext，提取所有台词。
    返回 (entries, sections):
      entries: [{"speaker": ..., "text": ..., "voiced": ...}, ...]
      sections: [{"title": ..., "start_line": ...}, ...]
    """
    entries = []
    sections = []
    line_num = 0

    lines = wikitext.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # 节点标题 =====◆节点名===== 或 ==节点名==
        m = re.match(r"^(=+)\s*(.+?)\s*\1$", line)
        if m and len(m.group(1)) >= 2:
            title = clean_text(m.group(2))
            if title:
                sections.append({"title": title, "start_line": line_num})
            i += 1
            continue

        # 剧情选项块（可能跨多行）
        if line.startswith("{{剧情选项"):
            block_lines = [line]
            depth = line.count("{{") - line.count("}}")
            while depth > 0 and i + 1 < len(lines):
                i += 1
                block_lines.append(lines[i].strip())
                depth += lines[i].count("{{") - lines[i].count("}}")

            block_text = "\n".join(block_lines)
            for speaker, text, voiced in parse_options_block(block_text):
                text = clean_text(text)
                for sentence in split_sentences(text):
                    entry = {"speaker": speaker, "text": sentence}
                    if not voiced:
                        entry["voiced"] = False
                    entries.append(entry)
                    line_num += 1
            i += 1
            continue

        # 普通台词行 *说话人 : 台词
        if line.startswith("*"):
            parsed = parse_speaker_line(line[1:])
            if parsed:
                speaker, text, voiced = parsed
                text = clean_text(text)
                for sentence in split_sentences(text):
                    entry = {"speaker": speaker, "text": sentence}
                    if not voiced:
                        entry["voiced"] = False
                    entries.append(entry)
                    line_num += 1

        i += 1

    return entries, sections


# ============================================================
# 输出层
# ============================================================

def write_jsonl(entries, path):
    """写入 JSONL 文件（UTF-8 无 BOM）"""
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_meta(entries, path, source_urls, sections):
    """写入 .meta.json 元数据文件"""
    characters = []
    seen = set()
    for entry in entries:
        speaker = entry.get("speaker")
        if speaker and speaker not in seen:
            characters.append(speaker)
            seen.add(speaker)

    meta = {
        "sections": sections,
        "characters": characters,
        "source_url": source_urls[0] if len(source_urls) == 1 else source_urls,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


# ============================================================
# CLI 命令
# ============================================================

def cmd_list(args):
    """列出索引页涉及的子任务页面"""
    print(f"获取索引页: {args.page}")
    title, wikitext = fetch_wikitext(args.page)
    print(f"页面标题: {title}")
    print()

    subpages = discover_subpages(wikitext)
    if not subpages:
        print("未发现子任务页面。此页面可能本身即为详情页。")
        return

    print(f"共发现 {len(subpages)} 个子任务页面:")
    for idx, name in enumerate(subpages, 1):
        url = f"https://wiki.biligame.com/ys/{urllib.parse.quote(name)}"
        print(f"  {idx}. {name}")
        print(f"     {url}")


def cmd_index(args):
    """索引页模式：自动发现子页面，合并输出 JSONL"""
    print(f"获取索引页: {args.page}")
    title, wikitext = fetch_wikitext(args.page)
    print(f"页面标题: {title}")

    subpages = discover_subpages(wikitext)
    if not subpages:
        print("未发现子任务页面，将当前页面作为详情页解析。")
        subpages = [title]

    print(f"共发现 {len(subpages)} 个子任务页面:\n")
    all_entries = []
    all_sections = []
    source_urls = []
    section_offset = 0

    for idx, name in enumerate(subpages, 1):
        print(f"  [{idx}/{len(subpages)}] {name}")
        page_title, page_wikitext = fetch_wikitext(name)
        entries, sections = parse_wikitext_dialogues(page_wikitext)

        for s in sections:
            s["start_line"] += section_offset

        all_entries.extend(entries)
        all_sections.extend(sections)
        section_offset += len(entries)
        source_urls.append(f"https://wiki.biligame.com/ys/{urllib.parse.quote(name)}")
        print(f"         -> {len(entries)} 行台词")
        time.sleep(0.5)

    print(f"\n共 {len(all_entries)} 行台词")

    write_jsonl(all_entries, args.output)
    print(f"已写入: {args.output}")

    meta_path = args.output.rsplit(".", 1)[0] + ".meta.json"
    write_meta(all_entries, meta_path, source_urls, all_sections)
    print(f"元数据: {meta_path}")


def cmd_single(args):
    """单页模式：直接解析一个详情页"""
    print(f"获取详情页: {args.page}")
    title, wikitext = fetch_wikitext(args.page)
    print(f"页面标题: {title}")

    entries, sections = parse_wikitext_dialogues(wikitext)
    print(f"共 {len(entries)} 行台词\n")

    write_jsonl(entries, args.output)
    print(f"已写入: {args.output}")

    meta_path = args.output.rsplit(".", 1)[0] + ".meta.json"
    source_url = f"https://wiki.biligame.com/ys/{urllib.parse.quote(title)}"
    write_meta(entries, meta_path, [source_url], sections)
    print(f"元数据: {meta_path}")


def main():
    parser = argparse.ArgumentParser(
        description="biligame wiki 原神剧情台词采集器"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_list = sub.add_parser("list", help="列出索引页涉及的子任务页面")
    p_list.add_argument("page", help="索引页 URL 或页面名")

    p_index = sub.add_parser("index", help="索引页模式：自动发现子页面并合并输出")
    p_index.add_argument("page", help="索引页 URL 或页面名")
    p_index.add_argument("-o", "--output", required=True, help="输出 JSONL 路径")

    p_single = sub.add_parser("single", help="单页模式：直接解析一个详情页")
    p_single.add_argument("page", help="详情页 URL 或页面名")
    p_single.add_argument("-o", "--output", required=True, help="输出 JSONL 路径")

    args = parser.parse_args()

    try:
        if args.mode == "list":
            cmd_list(args)
        elif args.mode == "index":
            cmd_index(args)
        elif args.mode == "single":
            cmd_single(args)
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
