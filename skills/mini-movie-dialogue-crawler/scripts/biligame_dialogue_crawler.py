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
WIKI_ORIGIN = "https://wiki.biligame.com"

# biligame 的 WAF 对 API 请求强制校验 Referer：缺失即无条件返回 HTTP 567
# （与「页面不存在」同码，极易误判）。UA 也需足够完整，否则同样被拦。
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# 旅行者相关名称 -- 无配音
UNVOICED_SPEAKERS = {"旅行者", "空", "荧"}
SENTENCE_PUNCT = "。！？；…"


def wiki_headers(title=""):
    """构造能通过 biligame WAF 的请求头；Referer 缺失会被判 567。"""
    referer = (f"{WIKI_ORIGIN}/ys/{urllib.parse.quote(title)}"
               if title else f"{WIKI_ORIGIN}/ys/")
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
    }


# ============================================================
# API 层
# ============================================================

def _fetch_json_with_curl(url, fallback_error, title=""):
    """WSL 直连 HTTPS 被重置时，用 Windows 侧 curl.exe 走宿主机网络。"""
    import shutil
    import subprocess

    exe = shutil.which("curl.exe")
    if not exe:
        raise fallback_error
    h = wiki_headers(title)
    r = subprocess.run(
        [exe, "-sS", "--max-time", "30",
         "-A", h["User-Agent"], "-e", h["Referer"],
         "-H", f"Accept: {h['Accept']}", url],
        capture_output=True,
    )
    if r.returncode != 0:
        detail = r.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"curl.exe 获取失败: {detail}")
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"curl.exe 响应解析失败: {e}") from e


FETCH_ATTEMPTS = 3           # wiki 反爬限流是偶发的，退避重试
FETCH_BACKOFF = 1.5


def describe_fetch_error(err, title):
    """把 urllib 的裸异常翻译成可操作的提示（区分「页面不存在」与「被反爬拦截」）"""
    code = getattr(err, "code", None)
    reason = getattr(err, "reason", "")
    page_url = f"https://wiki.biligame.com/ys/{urllib.parse.quote(title)}"
    if code == 404:
        return f"页面不存在: {title}（{page_url}）"
    if code in (403, 429, 567):          # 567 = biligame WAF 拦截
        return (f"wiki 拒绝访问（HTTP {code} {reason}）——这通常是反爬限流，"
                f"**不代表页面不存在**。已重试 {FETCH_ATTEMPTS} 次仍失败；"
                f"请稍后再试，或在浏览器打开确认页面名：{page_url}")
    if code is not None:
        return f"wiki 请求失败（HTTP {code} {reason}）: {page_url}"
    return f"wiki 请求失败（{type(err).__name__}: {err}）: {page_url}"


def _api_get(params, title=""):
    """带 WAF 头 + 退避重试的 MediaWiki API GET，返回解析后的 JSON dict。"""
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"

    data = None
    last_err = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers=wiki_headers(title))
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except json.JSONDecodeError as e:
            last_err = e
        except Exception as urllib_err:
            last_err = urllib_err
            code = getattr(urllib_err, "code", None)
            # 4xx 里只有限流/被拦值得重试；404 之类重试无意义
            if code is not None and 400 <= code < 500 and code not in (403, 429):
                break
            try:
                data = _fetch_json_with_curl(url, urllib_err, title)   # WSL 直连被重置时走宿主机 curl.exe
                break
            except Exception:
                pass
        if attempt < FETCH_ATTEMPTS - 1:
            time.sleep(FETCH_BACKOFF * (attempt + 1))

    if data is None:
        raise ValueError(describe_fetch_error(last_err, title))
    return data


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

    data = _api_get({
        "action": "query",
        "format": "json",
        "prop": "revisions",
        "titles": title,
        "rvprop": "content",
        "rvslots": "main",
    }, title)

    pages = data.get("query", {}).get("pages", {})
    for pid, page in pages.items():
        if str(pid) == "-1":
            raise ValueError(f"页面不存在: {title}")
        revs = page.get("revisions", [])
        if revs:
            content = revs[0].get("slots", {}).get("main", {}).get("*", "")
            return page.get("title", title), content

    raise ValueError(f"无法获取页面内容: {title}")


# {{系列任务}} / {{多重系列任务}} 索引页的 |系列任务= 字段，用于判定从属关系
SERIES_FIELD_RE = re.compile(r"\|\s*系列任务\s*=\s*([^\n|]*)")
# 详情页用 {{任务}}；章节/总系列页用 {{系列任务}} 或 {{多重系列任务}}
QUEST_TEMPLATE_RE = re.compile(r"\{\{\s*(多重系列任务|系列任务|任务)\s*[\n|]")


def series_field(wikitext):
    """取 |系列任务= 的值（已剔除 HTML 注释），无则返回空串"""
    m = SERIES_FIELD_RE.search(wikitext or "")
    if not m:
        return ""
    return re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S).strip()


def discover_subpages_from_links(page_title):
    """系列任务索引页（{{系列任务}}/{{多重系列任务}}）的原始 wikitext 不列子页面。

    子页面只出现在**渲染后**的导航框里，所以改走 action=parse 的链接表拿候选，
    再批量抓候选页 wikitext，用「{{任务}} 模板 + |系列任务= 含本页名」双重过滤，
    避免把 NPC/道具/角色等噪音链接当成任务页。返回值保持导航框顺序。
    """
    if not page_title:
        return []

    data = _api_get({"action": "parse", "format": "json",
                     "page": page_title, "prop": "links"}, page_title)
    if "error" in data:
        return []

    cands = []
    for link in data.get("parse", {}).get("links", []):
        t = (link.get("*") or "").strip()
        # 注意：API 的 "exists" 是**空字符串标记**而非布尔值，别用 link.get("exists") 判真假。
        # 命名空间 0（主命名空间）+ 排除自身即可，噪音交给下面的 wikitext 二次过滤。
        if not t or t == page_title or link.get("ns") != 0:
            continue
        cands.append(t)
    if not cands:
        return []

    # 批量校验：一次请求最多 50 个 titles
    ok = set()
    for i in range(0, len(cands), 50):
        chunk = cands[i:i + 50]
        d = _api_get({"action": "query", "format": "json", "prop": "revisions",
                      "titles": "|".join(chunk), "rvprop": "content",
                      "rvslots": "main"}, page_title)
        for page in (d.get("query", {}).get("pages") or {}).values():
            if page.get("missing") is not None:
                continue
            revs = page.get("revisions") or []
            if not revs:
                continue
            c = (revs[0].get("slots", {}).get("main", {}) or {}).get("*", "")
            if QUEST_TEMPLATE_RE.search(c) and page_title in series_field(c):
                ok.add(page.get("title"))
        time.sleep(0.5)

    return [t for t in cands if t in ok]


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
    返回 [(speaker, text, voiced), ...]，保留 wikitext 原始行顺序。

    逐行解析，避免 {{剧情选项}} 多行值与裸续行丢失：
    - `|选项N=` / `|剧情N=` 值按台词行解析
    - 块内 <br> 分隔的片段与裸续行按独立台词行解析
    - 无说话人的完整句子保留为 (None, text, False)
    - 无句末标点的 UI 标签（如“甜甜花”“罗莎莉亚”）丢弃
    """
    results = []
    for raw_line in block_text.split("\n"):
        line = raw_line.strip()
        if not line or line == "{{剧情选项" or line == "}}":
            continue
        if re.match(r"^\|(选项|剧情)\d+=", line):
            value = re.sub(r"^\|(选项|剧情)\d+=", "", line).strip()
        else:
            value = line
        # 模板值里常自带列表标记（|剧情1=*派蒙：…），不剥掉会污染 speaker 字段
        value = re.sub(r"^\*+\s*", "", value)
        for seg in re.split(r"<br\s*/?>", value):
            seg = seg.strip()
            if not seg:
                continue
            parsed = parse_speaker_line(seg)
            if parsed:
                results.append(parsed)
            elif any(c in seg for c in SENTENCE_PUNCT):
                results.append((None, seg, False))

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

        # 普通台词行 *说话人 : 台词（** 等多级列表标记一并剥掉）
        if line.startswith("*"):
            parsed = parse_speaker_line(line.lstrip("*"))
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

    subpages = discover_subpages(wikitext) or discover_subpages_from_links(title)
    if not subpages:
        print("未发现子任务页面。此页面可能本身即为详情页。")
        return

    print(f"共发现 {len(subpages)} 个子任务页面:")
    for idx, name in enumerate(subpages, 1):
        url = f"https://wiki.biligame.com/ys/{urllib.parse.quote(name)}"
        print(f"  {idx}. {name}")
        print(f"     {url}")


def cmd_index(args):
    """索引页模式：自动发现子页面，合并输出 JSONL

    递归下钻层级：系列任务总页 → 章节页 →（详情页），最多 2 层。
    """
    print(f"获取索引页: {args.page}")
    title, wikitext = fetch_wikitext(args.page)
    print(f"页面标题: {title}")

    all_entries, all_sections, source_urls = [], [], []
    state = {"offset": 0, "n": 0}

    def collect(page_title, page_wikitext, depth):
        entries, sections = parse_wikitext_dialogues(page_wikitext)
        if entries:
            for s in sections:
                s["start_line"] += state["offset"]
            all_entries.extend(entries)
            all_sections.extend(sections)
            state["offset"] += len(entries)
            source_urls.append(f"{WIKI_ORIGIN}/ys/{urllib.parse.quote(page_title)}")
            return len(entries)

        if depth >= 2:          # 到底了仍无台词，多半是页面结构变了
            return 0

        subs = discover_subpages(page_wikitext) or discover_subpages_from_links(page_title)
        if not subs:
            return 0
        total = 0
        for sub in subs:
            state["n"] += 1
            print(f"  [{state['n']}] {sub}  （来自 {page_title}）")
            sub_title, sub_wikitext = fetch_wikitext(sub)
            got = collect(sub_title, sub_wikitext, depth + 1)
            print(f"         -> {got} 行台词")
            time.sleep(0.5)
            total += got
        return total

    n = collect(title, wikitext, 0)
    if n == 0:
        print("未发现子任务页面，将当前页面作为详情页解析。")
        state["n"] = 1
        print(f"  [1] {title}")
        n = collect(title, wikitext, 2)

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
