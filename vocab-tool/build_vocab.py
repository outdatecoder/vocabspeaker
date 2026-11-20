#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_vocab.py — 把用户给的文件转成一个可双击打开的播放器词库包

两步式设计（第一步不需要联网、不需要 TTS）：

  第一步  解析 + 生成待校对表
      python build_vocab.py parse --in "我的词表.pdf" --out work/我的词表
      → work/我的词表/words.csv     ← 用 Excel/记事本改错词
      → work/我的词表/report.txt    ← 每页用了什么策略、置信度、警告

  第二步  合成音频 + 打包
      python build_vocab.py build --work work/我的词表 --name 我的词表 --out packs
      → packs/我的词表/index.html + audio/p001.mp3 …

  自检  python build_vocab.py doctor

设计原则
  1. 抽取路线依次尝试：表格识别 → 坐标聚类（自适应找两列）→ 报低置信度交给人工校对
     —— 绝不静默返回空表，绝不写死某一版 PDF 的坐标
  2. 词级缓存：audio 片段按 sha256(文本+音色) 存，换文件时大量词条直接命中、零 TTS 成本
  3. 时间轴由成品 MP3 的静音边界反推（已验证 110 页 / 2078 条 / 0 异常）
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
SKETCH_TEMPLATE = os.path.join(HERE, "..", "pet-player-sketch", "index.html")
CACHE_DIR = os.path.join(HERE, "cache")

EN_VOICE = "en-GB-SoniaNeural"
CN_VOICE = "zh-CN-XiaoxiaoNeural"
GAP_EC = 0.5      # 英文与中文之间
GAP_WORD = 0.5    # 词与词之间
# TTS 并发度。串行 3.60s/条 → 并发 12 时 0.44s/条（_tmp_tts_perf.py 实测）；
# 再往上只会更早撞 edge-tts 的限流，没有额外收益。
TTS_CONCURRENCY = 12
CJK = re.compile(r"[\u3400-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")
# 表头过滤：只在「两侧都是表头」时才丢。
# ⚠️ 不能按单侧匹配 —— 实测 PET 词表里有 'Chinese / 中国人；汉语'、'English / 英语'
#    这种**正经词条**（国家/语言主题），单侧匹配会把它们误删（踩过一次）。
SINGLE_HEADERS = {"英文", "中文"}
HEADER_PAIRS = {("english", "chinese"), ("英语", "汉语"), ("单词", "释义"),
                ("word", "meaning"), ("word", "translation")}


# ────────────────────────────── 基础设施 ──────────────────────────────
def log(msg):
    print(msg, flush=True)


_FFMPEG = None


def ffmpeg_exe():
    """定位 ffmpeg。优先环境变量 `FFMPEG`，其次 imageio-ffmpeg 自带的二进制，结果缓存。

    ⚠️ 已知无害噪音：`imageio_ffmpeg.get_ffmpeg_exe()` 内部会跑一次 `ffmpeg -version`
    做探测，在**中文 Windows（默认 GBK）**下它的读取线程会抛
    `UnicodeDecodeError: 'gbk' codec can't decode byte 0xac`
    （ffmpeg 版本横幅里有 `©` 之类的字节）。异常发生在子线程里、不影响返回值，
    但会污染日志 —— 所以这里缓存结果只探测一次；设了环境变量 `FFMPEG` 就完全跳过探测。
    """
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    p = os.environ.get("FFMPEG")
    if p and os.path.isfile(p):
        _FFMPEG = p
        return p
    try:
        import imageio_ffmpeg
        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _FFMPEG = shutil.which("ffmpeg")
    return _FFMPEG


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def sha16(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def norm_text(s):
    s = (s or "").replace("\ufb01", "fi").replace("\ufb02", "fl").replace("\ufb00", "ff")
    return re.sub(r"\s+", " ", s).strip()


# ────────────────────────────── 抽取层 ──────────────────────────────
def kmeans1d(vals, k=2, iters=60):
    """一维 k-means（不依赖 numpy）—— 用来自适应地找出「左列 / 右列」的中心。"""
    if len(vals) < k:
        return sorted(vals)
    lo, hi = min(vals), max(vals)
    cs = [lo + (hi - lo) * (i + 0.5) / k for i in range(k)]
    for _ in range(iters):
        groups = [[] for _ in range(k)]
        for v in vals:
            j = min(range(k), key=lambda i: abs(v - cs[i]))
            groups[j].append(v)
        new = [sum(g) / len(g) if g else cs[i] for i, g in enumerate(groups)]
        if all(abs(new[i] - cs[i]) < 1e-9 for i in range(k)):
            cs = new
            break
        cs = new
    return sorted(cs)


def cluster_by_y(tokens, y_gap):
    """把同一行的 token 合并成一个条目。

    ⚠️ 比较基准必须是**这一组的锚点行**，不能滚动更新成「上一个 token 的 y」。
    滚动更新会让相邻行不断串接：26pt 行距在 y_gap=40 时会把整列并成一条，
    然后整页被当成「抽取失败」丢掉（词就永久少了，用户还看不见）。
    """
    if not tokens:
        return []
    tokens = sorted(tokens, key=lambda t: (t[1], t[0]))
    groups = []
    for x, y, txt in tokens:
        if groups and abs(y - groups[-1]["y0"]) <= y_gap:
            groups[-1]["items"].append((y, x, txt))
        else:
            groups.append({"y0": y, "items": [(y, x, txt)]})
    for g in groups:
        g["items"].sort(key=lambda it: (it[0], it[1]))
    return [(g["y0"], " ".join(t for _, _, t in g["items"])) for g in groups]


def guess_y_gap(ys):
    """行距自适应：用**众数行距的一半**当阈值。

    旧实现 `median(diff) * 1.6` 再 clamp 到 40 是自相矛盾的：
    行距 d ≤ 25 时 1.6d ≤ 40，d ≤ 1.6d 恒成立 → 一定合并；
    d > 25 时阈值被压到 40，d ≤ 40 仍然一定合并。
    结果：**任何 40pt 以内的等距行都会被并成一整条**。
    众数取「重复出现最多的行距」，再取一半 → 既不串行，也不把同一行切开。
    """
    uniq = sorted(set(round(y, 1) for y in ys))
    diffs = [b - a for a, b in zip(uniq, uniq[1:]) if 0.5 < b - a < 60]
    if not diffs:
        return 24.0
    buckets = {}
    for d in diffs:
        k = int(round(d))
        buckets[k] = buckets.get(k, 0) + 1
    pitch = max(buckets.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return max(6.0, min(60.0, pitch * 0.5))


def score_pair(en, cn):
    """给一条「英文 / 中文」配对打质量分（0~1）"""
    if not en or not cn:
        return 0.0
    s = 0.0
    s += 0.45 * min(1.0, len(LATIN.findall(en)) / max(1, len(en)))
    s += 0.35 * min(1.0, len(CJK.findall(cn)) * 2 / max(1, len(cn)))
    if len(en) <= 40:
        s += 0.10
    if len(cn) <= 24:
        s += 0.10
    return round(min(1.0, s), 2)


def keep_pair(en, cn):
    en_s, cn_s = en.strip(), cn.strip()
    if en_s in SINGLE_HEADERS or cn_s in SINGLE_HEADERS:
        return False
    if (en_s.lower(), cn_s.lower()) in HEADER_PAIRS:
        return False
    return True


def drop_headers(pairs):
    return [(e, c) for e, c in pairs if keep_pair(e, c)]


def pairs_from_table(rows):
    """从表格行里找「左英文 / 右中文」两列。
    返回 (pairs, 说明)。行里可能有多个空列，所以按内容特征挑列而不是按固定下标。"""
    if not rows or len(rows) < 2:
        return [], "表格行数不足"
    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]

    col_en, col_cn = -1, -1
    best = 0.0
    for i in range(width):
        cells = [norm_text(r[i]) for r in rows]
        latin = sum(1 for c in cells if c and len(LATIN.findall(c)) >= len(c) * 0.6)
        if latin / max(1, len(rows)) > best:
            best = latin / max(1, len(rows))
            col_en = i
    best = 0.0
    for i in range(width):
        if i == col_en:
            continue
        cells = [norm_text(r[i]) for r in rows]
        cjk = sum(1 for c in cells if c and CJK.search(c))
        if cjk / max(1, len(rows)) > best:
            best = cjk / max(1, len(rows))
            col_cn = i
    if col_en < 0 or col_cn < 0:
        return [], "没找到「英文列 + 中文列」的组合"
    pairs = []
    for r in rows:
        en, cn = norm_text(r[col_en]), norm_text(r[col_cn])
        if en and cn:
            pairs.append((en, cn))
    return drop_headers(pairs), "英文列#%d / 中文列#%d" % (col_en, col_cn)


def pairs_from_coords(page, words, tag=""):
    """坐标法：一维聚类自适应找两列 → 按 y 聚类成条目 → 顺序配对。"""
    words = [w for w in words if norm_text(w[4])]
    if len(words) < 4:
        return [], "本页文字太少", []
    W = page.rect.width
    centers = kmeans1d([w[0] for w in words], 2)
    if len(centers) < 2 or (centers[1] - centers[0]) < W * 0.22:
        return [], "看不出明显的两列结构（列间距 < 22% 页宽）", []
    split = (centers[0] + centers[1]) / 2
    left = [(w[0], w[1], norm_text(w[4])) for w in words if w[0] < split]
    right = [(w[0], w[1], norm_text(w[4])) for w in words if w[0] >= split]
    if not left or not right:
        return [], "只有一列有内容", []

    y_gap = guess_y_gap([w[1] for w in words])
    en_entries = cluster_by_y(left, y_gap)
    cn_entries = cluster_by_y(right, y_gap)

    warnings = []
    if len(en_entries) == len(cn_entries):
        pairs = [(e[1], c[1]) for e, c in zip(en_entries, cn_entries)]
        how = "顺序 1:1 配对（两侧各 %d 条）" % len(en_entries)
    else:
        pairs, used = [], set()
        for y, e in en_entries:
            best = None
            for j, (yc, c) in enumerate(cn_entries):
                if j in used:
                    continue
                d = abs(yc - y)
                if best is None or d < best[0]:
                    best = (d, j, c)
            if best and best[0] <= y_gap * 1.5:
                pairs.append((e, best[2]))
                used.add(best[1])
        how = "最近邻配对（左 %d / 右 %d，取到 %d 条）" % (len(en_entries), len(cn_entries), len(pairs))
        warnings.append("左右数量不等（%d vs %d），已按最近邻兜底，请重点校对本页"
                        % (len(en_entries), len(cn_entries)))
    return drop_headers(pairs), how, warnings


def extract_pdf_page(page, page_no):
    # ① 表格识别优先
    try:
        tabs = page.find_tables()
        for t in tabs.tables:
            pairs, how = pairs_from_table(t.extract())
            if len(pairs) >= 3:
                return dict(page=page_no, pairs=pairs, strategy="table",
                            detail=how, warnings=[])
    except Exception:
        pass
    # ② 坐标聚类兜底
    pairs, how, warns = pairs_from_coords(page, page.get_text("words"))
    return dict(page=page_no, pairs=pairs, strategy="coords", detail=how, warnings=warns)


def parse_pdf(path):
    """逐页抽取，**不在这里丢页**。

    旧实现 `if len(pairs) >= 2` 会把「抽不出来」的页静默丢掉 —— 用户在校对表里
    根本看不到那一页，也就无从发现漏词（P1）。现在每页都返回（pairs 可能为空），
    由 cmd_parse 汇总成 pages_report.json，网页端再把「空页 / 未抽取页」显示出来。
    """
    import pymupdf
    doc = pymupdf.open(path)
    out = []
    try:
        for i in range(doc.page_count):
            out.append(extract_pdf_page(doc[i], i + 1))
    finally:
        doc.close()
    return out


def parse_txt(path):
    """按字节试解码：UTF-8(BOM) → GB18030。

    旧实现固定 `utf-8-sig` + `errors="replace"`：上传层已经识别出 GB18030 并记进
    meta，解析层却把中文替换成 �，任务还照样显示 done（P2）。宁可明确报错，
    也不要输出一份"看起来成功"的乱码词表。
    """
    with open(path, "rb") as f:
        raw_bytes = f.read()
    raw, used = None, None
    for enc in ("utf-8-sig", "gb18030"):
        try:
            raw = raw_bytes.decode(enc)
            used = enc
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raise SystemExit("文本既不是 UTF-8 也不是 GB18030，无法解码（请另存为 UTF-8）")
    lines = [l.rstrip("\n") for l in raw.splitlines()]
    pairs = []
    warn = []
    for line in lines:
        if not line.strip():
            continue
        parts = re.split(r"\t+|\s{3,}|\s*\|\s*|\s*,\s*", line.strip(), maxsplit=1)
        if len(parts) == 2 and norm_text(parts[0]) and norm_text(parts[1]):
            pairs.append((norm_text(parts[0]), norm_text(parts[1])))
        else:
            warn.append("跳过无法切分的行：%s" % line[:50])
    return [dict(page=1, pairs=drop_headers(pairs), strategy="txt",
                 detail="按 Tab/多空格/|/, 切分（%s）" % used, warnings=warn[:10])]


# 词性标记（用于从"音标后的尾巴"里剥掉词性，剩下的就是释义）
POS = {"n", "v", "adj", "adv", "prep", "conj", "pron", "num", "phr", "art",
       "int", "aux", "vt", "vi", "abbr", "pl", "det"}
PHONETIC = re.compile(r"[\[［]([^\]］]{0,80})[\]］]")
NUMBERED = re.compile(r"^(\d{1,5})\s+(.+)$")


def _strip_pos(tail):
    """已并入 clean_cn —— 保留此函数仅为兼容旧调用点（等价于 clean_cn 的前半段）。
    clean_cn 能处理 `n & v  旅游，旅行`（& 连接的多词性）这种 _strip_pos 处理不了的写法。"""
    return clean_cn(tail)


CJK_ANY = re.compile(r"[\u4e00-\u9fff]")

# 词性前缀。要点：
#   · 必须"后面紧跟中文/全角括号"才算词性，否则会误伤 `what is 的缩写形式` 这类英文释义
#   · 支持 `v.aux`（多段）、`v& n`（& 连接）、`pro`（pron 简写）这些实测写法
_POS = r"(?:n|v|adj|adv|prep|conj|pron|pro|num|phr|art|int|aux|vt|vi|abbr|pl|det)"
POS_LEAD = re.compile(
    r"^\s*" + _POS + r"(?:\s*\.\s*(?:aux|pl|ing|ed|inf))?"
    r"(?:\s*[&＆、,，/]\s*" + _POS + r")*\.?\s*(?=[\u4e00-\u9fff（【])",
    re.I)
# 中文中间的 `；n  中国人` 这类残留词性
POS_MID = re.compile(
    r"[；;]\s*(?:n|v|adj|adv|prep|conj|pron|num|phr|art|int|aux|vt|vi)\.?\s*",
    re.I)


def clean_cn(cn):
    """剥掉释义侧残留的词性标记。

    实测这份词表里的三种写法（`cn` 以字母开头共 108/2182 条，其中 49 条是残词性）：
        minus [..] prep减，减去        → 词性与中文**紧贴**
        can   [..] v.aux 能，可以，会   → 词性是**多段**写法
        Chinese[..] adj中国的…；n 中国人 → 词性出现在**中段**
    而 `what is 的缩写形式` / `child的复数形式` / `go的单数第三人称现在时`
    是**合法英文释义**，不能动 —— 靠"词性标记后必须紧跟中文"这个前瞻来区分。
    """
    s = (cn or "").strip()
    # 反复剥，直到不再变化。一轮里依次处理三种写法：
    #   ① 紧贴中文 / & 连接 / 多段：`prep减`、`n & v  旅游`、`v.aux 能`
    #   ② 独立词性 token：`n pl.potatoes 马铃薯` 里的那个 `n`
    #   ③ 词性 + 英文变形 + 空格 + 中文：`pl.potatoes 马铃薯`
    for _ in range(5):
        before = s
        m = POS_LEAD.match(s)
        if m:
            s = s[m.end():].strip()
        parts = s.split(None, 1)
        if len(parts) == 2 and parts[0].lower().rstrip(".") in POS:
            s = parts[1].strip()
        s = re.sub(r"^\s*(?:pl|n|v|adj|adv|aux|vt|vi)\.\s*[A-Za-z][A-Za-z\-'’.]*\s+"
                   r"(?=[\u4e00-\u9fff])", "", s)
        s = POS_MID.sub("；", s)
        if s == before:
            break
    return re.sub(r"[；;]\s*[；;]", "；", s).strip("；; ")


def merge_back_english(en, tail):
    """原文可能把**音标插在词中间**：实测 `892  trip [trip] over   phr. （被...）绊倒`
    → 正确词条是 `trip over`。此时音标后的英文词要并回词条。

    只在「这段英文之后**紧跟词性标记**」时才并 —— 否则会把
    `what's [hwɔts] what is 的缩写形式` 误并成 `what's what is`。
    """
    toks = (tail or "").split()
    k = 0
    while (k < len(toks)
           and re.fullmatch(r"[A-Za-z][A-Za-z\-'’.]*", toks[k])
           and toks[k].lower().rstrip(".") not in POS):
        k += 1
    if k and k < len(toks) and toks[k].lower().rstrip(".") in POS:
        return (en + " " + " ".join(toks[:k])).strip(), " ".join(toks[k:])
    return en, tail


def split_plain_entry(rest):
    """没有 [音标] 时的兜底切分。实测这份词表里 **所有短语/部分缩写都没有音标**
    （340 条，约 15%），丢掉它们是不可接受的，所以必须能切。

    样本：
        at school   phr. 在学校               → ('at school', '在学校')
        How are you？   你（身体）好吗?       → ('How are you？', '你（身体）好吗?')
        What about...?  phr. （询问消息，征求意 见）…怎么样?  → ('What about...?', '（询问消息，征求意见）…怎么样?')
        regard… as   phr. 把…当作，当作        → ('regard… as', '把…当作，当作')
        This way，please.   请走这边           → ('This way，please.', '请走这边')

    规则：找**独立的词性 token**（phr. / n / adv …）作为分界；找不到就找**第一个含中文的 token**。
    中文部分用 '' 直接拼接 —— 源文件里 `征求意 见` 那种空格是排版换行残留，应当去掉。
    """
    toks = (rest or "").split()
    if not toks:
        return "", ""
    pos_i = next((i for i, t in enumerate(toks) if t.lower().rstrip(".") in POS), -1)
    if pos_i > 0:
        return " ".join(toks[:pos_i]).strip(), "".join(toks[pos_i + 1:]).strip()
    cjk_i = next((i for i, t in enumerate(toks) if CJK_ANY.search(t)), -1)
    if cjk_i > 0:
        return " ".join(toks[:cjk_i]).strip(), "".join(toks[cjk_i:]).strip()
    return rest.strip(), ""


def parse_docx_paragraphs(d):
    """段落式词表：`序号 + 空格 + 单词 + [音标] + 词性(可选) + 释义`
    实测样本（E:\\01+初中英语单词表2182个(带音标).docx，2191 段，**无表格**）：
        1    what [hwɔt] pron 什么
        3    what's [hwɔts] what is 的缩写形式
        9    I'm  [aim]   I am 的缩写形式
        2178  alive [ə'laiv]  adj 活着的
    """
    lines = [p.text for p in d.paragraphs]
    pairs, warns, skipped = [], [], 0
    no_phonetic = 0
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        m = NUMBERED.match(s)
        if not m:
            skipped += 1                     # 标题 / “注：n 名词…” 之类的说明行
            continue
        rest = m.group(2).strip()
        mb = PHONETIC.search(rest)
        if mb:
            en = rest[:mb.start()].strip()
            tail = rest[mb.end():].strip()
            en, tail = merge_back_english(en, tail)     # 音标被插在词中间的情况
            cn = clean_cn(tail)                          # 统一走 clean_cn（含 & 连接词性）
        else:
            # ⚠️ 不能丢！这份词表里所有短语都没有音标（实测 340 条）→ 走兜底切分
            no_phonetic += 1
            en, cn = split_plain_entry(rest)
            cn = clean_cn(cn)
            if not (en and cn):
                warns.append("无法切分，已跳过：%s" % s[:60])
                continue
        if en and cn:
            pairs.append((en, cn))
    if no_phonetic:
        warns.append("其中 %d 行没有 [音标]，已用「词性/中文边界」兜底切分" % no_phonetic)
    if skipped:
        warns.append("已跳过 %d 行非词条行（标题/注释）" % skipped)
    return pairs, warns


def load_word_pages(path_json):
    """读 Word COM 导出的真实页码映射（`_word_pages.ps1` 产出）。
    返回 [页号, ...]，长度 = 词条数，顺序与解析顺序一致；不可用则返回 None。

    为什么要这个：docx 的 XML **不存分页信息**（实测 lastRenderedPageBreak 0 处、
    显式分页符 0 处），"第一页有多少词"只能靠 Word 自己排版才知道。
    """
    if not path_json:
        return None
    try:
        with open(path_json, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        log("⚠️ 读取 %s 失败（%s），将把全文当一页处理" % (path_json, e))
        return None
    if isinstance(data, dict):
        data = [data]
    try:
        rows = sorted(data, key=lambda r: int(r["entry"]))
        # `page` = 词条**开始**的那一页。旧版脚本只记了段尾页（wdActiveEndPageNumber），
        # 于是"末行溢到下一页"的词条会被归到下一页、从自己那一页里消失
        # （实例：232 grandpa 的中文「外公」在第 5 页，整条被记成第 5 页）。
        # 旧文件没有 page_end 字段 —— 只说一句，不猜、不改它的语义。
        if rows and not all("page_end" in r for r in rows):
            log("⚠️ %s 是旧版页码映射（没有 page_end 字段）：跨页词条会归到下一页，"
                "建议重跑 _word_pages.ps1" % os.path.basename(path_json))
        return [int(r["page"]) for r in rows]
    except Exception as e:
        log("⚠️ %s 格式不符（%s），忽略" % (path_json, e))
        return None


def parse_docx(path, word_pages=None):
    try:
        import docx
    except ImportError:
        raise SystemExit("缺少 python-docx，请先运行：pip install python-docx")
    d = docx.Document(path)

    # ① 先试表格式（两列表格）
    out = []
    for ti, table in enumerate(d.tables, 1):
        rows = [[norm_text(c.text) for c in r.cells] for r in table.rows]
        pairs, how = pairs_from_table(rows)
        if len(pairs) >= 2:
            out.append(dict(page=ti, pairs=pairs, strategy="table",
                            detail="表格%d：%s" % (ti, how), warnings=[]))
    if out:
        return out

    # ② 表格里没东西 → 走段落式（实测这份"初中英语单词表"就是这种）
    pairs, warns = parse_docx_paragraphs(d)
    if len(pairs) < 2:
        raise SystemExit("docx 里既没找到两列表格，也没找到「序号+单词+[音标]+释义」的段落式词条")

    # ③ 有真实页码映射 → 按真实分页切段；否则全文当一页
    if word_pages:
        if len(word_pages) != len(pairs):
            log("⚠️ 真实页码映射有 %d 条，但抽到 %d 条词条 —— 数量不符，"
                "不敢按它切页（请重跑 _word_pages.ps1），本次把全文当一页。"
                % (len(word_pages), len(pairs)))
        else:
            out, cur, cur_pg = [], [], None
            for (en, cn), pg in zip(pairs, word_pages):
                if cur_pg is not None and pg != cur_pg:
                    out.append(dict(page=cur_pg, pairs=cur, strategy="lines",
                                    detail="段落式（真实分页：Word 第 %d 页）" % cur_pg,
                                    warnings=[]))
                    cur = []
                cur_pg = pg
                cur.append((en, cn))
            if cur:
                out.append(dict(page=cur_pg, pairs=cur, strategy="lines",
                                detail="段落式（真实分页：Word 第 %d 页）" % cur_pg,
                                warnings=[]))
            if out:
                out[0]["warnings"] = list(out[0]["warnings"]) + warns
            log("✅ 已按 Word 真实分页切分：%d 页（第 1 页 %d 条）"
                % (len(out), len(out[0]["pairs"])))
            return out

    return [dict(page=1, pairs=pairs, strategy="lines",
                 detail="段落式：序号+单词+[音标]+词性+释义（全文当一页）",
                 warnings=warns)]


def parse_any(path, word_pages=None):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return parse_pdf(path)
    if ext == ".txt":
        return parse_txt(path)
    if ext == ".docx":
        return parse_docx(path, word_pages)
    if ext in (".jpg", ".jpeg", ".png"):
        raise SystemExit("图片/扫描件要先走 OCR，这一支线尚未实现（见实施计划 §17）")
    raise SystemExit("暂不支持的格式：%s" % ext)


# ─────────────────────── 解析结果 → 报告 / CSV ───────────────────────
def quality_of(pairs):
    if not pairs:
        return 0.0
    return round(sum(score_pair(e, c) for e, c in pairs) / len(pairs), 2)


def cmd_parse(args):
    src = os.path.abspath(args.input)
    if not os.path.isfile(src):
        raise SystemExit("找不到输入文件：%s" % src)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    log("解析：%s  (%.1f MB)" % (os.path.basename(src), os.path.getsize(src) / 1048576))
    t0 = time.time()
    all_pages = parse_any(src, load_word_pages(getattr(args, "word_pages", None)))
    if not all_pages:
        raise SystemExit("一个词条都没抽到 —— 这个文件可能不是「英中配对词汇表」")
    pages = all_pages
    # --page N：只处理第 N 页（配合 --word-pages 时 N 即 Word 的真实页码）
    if getattr(args, "page", 0):
        sel = [p for p in pages if p["page"] == args.page]
        if not sel:
            raise SystemExit("没有第 %d 页（共 %d 页，页码：%s）"
                             % (args.page, len(pages),
                                ", ".join(str(p["page"]) for p in pages[:15])))
        pages = sel
    if args.max_pages:
        pages = pages[:args.max_pages]

    # 空页（一条都没抽到）单独拎出来：不写进 words.csv，但必须让用户看见 ——
    # 用户看不见的页 = 永久丢词（旧实现是在 parse_pdf 里静默丢掉的）。
    empty_pages = [p for p in pages if not p["pairs"]]
    pages = [p for p in pages if p["pairs"]]
    if empty_pages:
        log("⚠️ %d 页一条词条都没抽到（会写进 pages_report.json 并显示在网页端）：%s"
            % (len(empty_pages),
               "、".join("第%d页" % p["page"] for p in empty_pages[:10])))
    if not pages:
        raise SystemExit("所有页都没抽出词条 —— 每页原因：%s"
                         % "；".join("第%d页：%s" % (p["page"], p.get("detail") or "未抽取")
                                    for p in empty_pages[:5]))

    # --max-words：只取前 N 条（用于"先拿一页试生成"，避免一次跑 2000+ 词）
    if getattr(args, "max_words", 0):
        left, cut = args.max_words, []
        for p in pages:
            if left <= 0:
                break
            if len(p["pairs"]) > left:
                q = dict(p)
                q["pairs"] = p["pairs"][:left]
                q["warnings"] = list(p["warnings"]) + ["已按 --max-words 截断到前 %d 条" % args.max_words]
                cut.append(q)
            else:
                cut.append(p)
            left -= len(p["pairs"])
        pages = cut

    # 非词表页闸门：说明页/封面页也会被抽出几条垃圾（实测 PET 第 2 页是「学习方法说明页」，
    # 抽到 3 条均分 0.46 的垃圾），必须按均分剔掉，否则会污染词库。
    skipped = []                              # [(page, score)]，写进 pages_report.json
    if not args.keep_low:
        kept = []
        for p in pages:
            q = quality_of(p["pairs"])
            if q < args.min_page_score:
                skipped.append((p["page"], q))
            else:
                kept.append(p)
        if skipped:
            log("已跳过 %d 页（均分 < %.2f，疑似说明页 / 非词表页）：%s"
                % (len(skipped), args.min_page_score,
                   "、".join("第%d页(%.2f)" % s for s in skipped)))
        pages = kept
        if not pages:
            raise SystemExit("所有页都被判定为非词表页 —— 这个文件可能不是「英中配对词表」。"
                             "确认无误可加 --keep-low 强制保留")

    csv_path = os.path.join(out_dir, "words.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["page", "en", "cn", "score"])
        for p in pages:
            for en, cn in p["pairs"]:
                w.writerow([p["page"], en, cn, "%.2f" % score_pair(en, cn)])

    # 逐页诊断：words.csv 只能表达「成功的页」，丢了哪些页必须另有出口。
    # 网页端读它 → 校对界面把「空页 / 低分页 / 只有 1 条」标出来。
    thin = [{"page": p["page"], "pairs": len(p["pairs"]),
             "detail": p.get("detail") or ""}
            for p in pages if len(p["pairs"]) == 1]
    pages_report = {
        "source": os.path.basename(src),
        "total_pages": len(all_pages),
        "used_pages": [p["page"] for p in pages],
        "empty_pages": [{"page": p["page"],
                         "strategy": p.get("strategy") or "",
                         "reason": p.get("detail") or "本页没有抽出任何词条"}
                        for p in empty_pages],
        "skipped_pages": [{"page": pg, "score": q,
                           "reason": "页均分 < %.2f（疑似说明页/非词表页）"
                                     % args.min_page_score}
                          for pg, q in skipped],
        "thin_pages": thin,
    }
    with open(os.path.join(out_dir, "pages_report.json"), "w",
              encoding="utf-8") as f:
        json.dump(pages_report, f, ensure_ascii=False, indent=1)

    lines = ["来源：%s" % src, "耗时：%.1fs" % (time.time() - t0), ""]
    lines.append("%-6s %-8s %-6s %-6s %s" % ("页", "策略", "条数", "均分", "说明"))
    lines.append("-" * 96)
    total, low, warns = 0, 0, 0
    for p in pages:
        q = quality_of(p["pairs"])
        total += len(p["pairs"])
        if q < 0.75:
            low += 1
        warns += len(p["warnings"])
        lines.append("%-6d %-8s %-6d %-6.2f %s" % (p["page"], p["strategy"], len(p["pairs"]), q, p["detail"]))
        for wmsg in p["warnings"]:
            lines.append("        ⚠️  " + wmsg)
    lines += ["", "合计 %d 页 / %d 条；均分 < 0.75 的页：%d 页；警告：%d 条" % (len(pages), total, low, warns)]
    if empty_pages:
        lines += ["", "⚠️ 以下 %d 页一条词条都没抽到（校对表里不会出现，请确认是否需要补录）："
                      % len(empty_pages)]
        for p in empty_pages[:20]:
            lines.append("    第%d页  %s  %s" % (p["page"], p.get("strategy") or "-",
                                                p.get("detail") or "未抽取"))
    if skipped:
        lines += ["", "已按 min-page-score 跳过 %d 页：%s"
                      % (len(skipped), "、".join("第%d页(%.2f)" % s for s in skipped))]
    if thin:
        lines += ["", "⚠️ %d 页只抽出 1 条（可能漏词）：%s"
                      % (len(thin), "、".join("第%d页" % t["page"] for t in thin[:20]))]

    # 把「释义仍以英文字母开头」的条目标出来 —— 要么是合法英文释义（如 what is 的缩写形式），
    # 要么是漏剥的词性。这是校对时最该先看的一批。
    susp = [(p["page"], en, cn) for p in pages for en, cn in p["pairs"]
            if re.match(r"^[A-Za-z]", cn or "")]
    if susp:
        lines += ["", "⚠️ 以下 %d 条「释义以英文字母开头」—— 多为合法英文释义，"
                      "也可能是漏剥的词性，建议过一眼（完整清单见 words.csv）：" % len(susp)]
        for pg, en, cn in susp[:40]:
            lines.append("    p%-4s %-20s | %s" % (pg, en, cn[:52]))
        if len(susp) > 40:
            lines.append("    …（其余 %d 条见 words.csv）" % (len(susp) - 40))

    lines += ["", "👉 下一步：用 Excel 打开同目录的 words.csv，把错的改对（列不能改名），"
                  "然后运行 build 子命令。"]
    with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log("\n".join(lines[:3]))
    log("…（完整报告见 %s）" % os.path.join(out_dir, "report.txt"))
    log("\n✅ 已生成待校对表：%s   （%d 页 / %d 条）" % (csv_path, len(pages), total))
    if low:
        log("⚠️  有 %d 页均分偏低，建议优先校对" % low)
    return 0


# ────────────────────────────── 合成层 ──────────────────────────────
class TTS:
    """词级缓存的 TTS 封装。缓存键 = sha256(文本 + 音色)，命中即 0 成本。"""

    def __init__(self, provider="edge", proxy=None):
        self.provider = provider
        self.proxy = proxy
        self.hit = 0
        self.miss = 0
        os.makedirs(CACHE_DIR, exist_ok=True)

    def cached(self, text, voice):
        return os.path.join(CACHE_DIR, "%s_%s.mp3" % (sha16(text + "|" + voice), voice[:2]))

    def preflight(self):
        """不联网先探一次，把问题尽早暴露出来（edge-tts 目前已知会 403）"""
        if self.provider != "edge":
            return True
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            log("❌ 没装 edge-tts：pip install edge-tts")
            return False
        probe = self.cached("__probe__", EN_VOICE)
        if os.path.exists(probe):
            return True
        try:
            asyncio.run(edge_tts.Communicate("ok", EN_VOICE,
                                            proxy=self.proxy).save(probe))
            os.remove(probe)
            return True
        except Exception as e:
            if os.path.exists(probe):          # 失败会留下 0 字节文件，别污染缓存
                try:
                    os.remove(probe)
                except OSError:
                    pass
            log("❌ TTS 通道不可用：%s" % str(e)[:120])
            try:
                ver = getattr(edge_tts, "__version__", "0")
                nums = tuple(int(x) for x in re.findall(r"\d+", ver)[:2])
                if nums < (7, 3):
                    log("   ⚠️ 当前 edge-tts 版本 %s 偏旧：国内出口需要 >= 7.3.1（v2 令牌）才能过，"
                        "请 pip install -U edge-tts" % ver)
            except Exception:
                pass
            log("   已诊断（vocab-tool/_probe_edge_tts*.py 可复跑）：")
            log("     · HTTP voices/list 同域名同 token → 200（域名/网络/出口 IP 均正常）")
            log("     · WS 握手 7 种参数组合全 403（含完全不带 Sec-MS-GEC 的裸请求）")
            log("     · 本机时钟偏差仅 0.9 秒 → 与常见文章说的「时钟漂移」无关")
            log("     · 补全 Sec-CH-UA 系列头、换端点域名、换压缩/子协议 → 仍然 403")
            log("     · 真实 Edge 155 浏览器的 WS 也被拒（closed code=1006）")
            log("     ⇒ 端点在当前网络出口被统一拒绝，改代码/改参数修不了。")
            log("   🔎 最快定位（按此顺序）：")
            log("        ① 看 edge-tts 版本 —— 需 >= 7.3.1；国内出口要 v2 令牌（Sec-MS-GEC-Version: 2-…）")
            log("        ② 再看出口 IP —— 海外出口对旧版宽松，会掩盖 v1 令牌已被弃用的问题")
            log("   可选：① pip install -U edge-tts（>=7.3.1）—— 本机实测即可恢复，**无需代理**")
            log("        ② 挂代理：--proxy http://127.0.0.1:7890")
            log("        ③ 换 Azure（确定可用，约 ¥3/全书）")
            log("        ④ 先靠词级缓存复用已有音频（0 成本）")
            log("        ⑤ 只跑 parse 先完成校对（完全不需要 TTS）")
            return False

    async def synth(self, text, voice, out_path):
        import edge_tts
        last = None
        for attempt in range(4):
            try:
                await asyncio.wait_for(
                    edge_tts.Communicate(text, voice, proxy=self.proxy).save(out_path),
                    timeout=40)
                return
            except Exception as e:
                last = e
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError("TTS 失败 %r: %s" % (text, last))

def cache_ok(tts, text, voice):
    """缓存文件存在**且有内容**才算命中。

    为什么不能只看 `os.path.exists`：edge-tts 失败时会留下 0 字节文件，
    只看存在性会把"残骸"当成命中 → 跳过合成 → 拼接时得到空音轨（静默出错）。
    """
    p = tts.cached(text, voice)
    return p, os.path.exists(p) and os.path.getsize(p) > 0


async def _synth_all(tts, jobs, concurrency):
    """把全部未命中项并发跑完。jobs = [(text, voice, out_path)]。

    串行的代价是每个词都要等两次完整的网络往返；并发后这些往返互相重叠。
    拼接（ffmpeg）是本地 CPU，留在调用方逐页做，输出顺序与内容完全不变。
    任何一条最终失败都会让异常冒出去 —— 半个词库比没有词库更难被发现。
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    total, done = len(jobs), 0

    async def one(text, voice, out_path):
        nonlocal done
        async with sem:
            await tts.synth(text, voice, out_path)
        done += 1                      # 事件循环单线程，这里不需要锁
        if done % 20 == 0 or done == total:
            log("    … 已合成 %d/%d" % (done, total))

    await asyncio.gather(*(one(t, v, p) for t, v, p in jobs))


_MPEG_BR = {
    3: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],   # MPEG1 L3
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],       # MPEG2/2.5 L3
}
_MPEG_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def mp3_frames(path):
    """逐帧遍历 MP3，返回 (帧数, 精确时长秒)。

    为什么必须自己数帧、不能用 ffmpeg 报的 Duration / silencedetect：
    时间轴要为**浏览器**服务，而浏览器对 CBR MP3 的 currentTime 就是
    「帧序号 × 每帧时长」。ffmpeg 解码时会应用 LAME 的 gapless 元数据（裁掉编码器
    延迟/填充），得到的是**另一条时间轴** —— 两者差几十到几百毫秒，且会在拼接处累积。
    更关键的是：靠"数静音"推断词边界本质上不可靠（见 tracks_analytic 的注释）。
    """
    with open(path, "rb") as f:
        buf = f.read()
    i, n, total = 0, 0, 0.0
    if buf[:3] == b"ID3" and len(buf) > 10:
        sz = ((buf[6] & 0x7F) << 21) | ((buf[7] & 0x7F) << 14) \
             | ((buf[8] & 0x7F) << 7) | (buf[9] & 0x7F)
        i = 10 + sz
    L = len(buf)
    while i + 4 <= L:
        if buf[i] != 0xFF or (buf[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        ver = (buf[i + 1] >> 3) & 0x03
        layer = (buf[i + 1] >> 1) & 0x03
        bri = (buf[i + 2] >> 4) & 0x0F
        sri = (buf[i + 2] >> 2) & 0x03
        pad = (buf[i + 2] >> 1) & 0x01
        if layer != 1 or bri in (0, 15) or sri == 3:
            i += 1
            continue
        br = _MPEG_BR[3 if ver == 3 else 2][bri]
        sr = _MPEG_SR[ver][sri]
        spf = 1152 if ver == 3 else 576
        flen = int(spf / 8.0 * br * 1000 / sr) + pad
        if flen <= 4 or i + flen > L:
            break
        total += spf / float(sr)
        n += 1
        i += flen
    return n, total


def tracks_analytic(pairs, segs, gap_word_dur):
    """解析式时间轴：按拼接顺序与各段精确帧时长累加。

    结构： [en1][gap_ec][cn1][gap_word][en2][gap_ec][cn2] ...
    start_i = 该词英文片段的起点；end_i = 该词中文片段的终点。

    ⚠️ 为什么不再用 silencedetect 的结果：实测同一页会多检出 13 段静音
    （片段内部停顿，如 0.31s），而 GAP_EC 与 GAP_WORD 都是 0.5s、**无法区分**，
    原实现只能"赌奇偶"（取奇数下标当词边界）。一旦多/少一段，奇偶性翻转，
    之后所有词边界都取到「英↔中」那一段 → start 指向该词中文起点
    （即上一个词的中文），播放时就表现为「先读了上一次的中文」。
    改为按帧累加后，与静音检测彻底解耦。
    """
    tracks, t = [], 0.0
    for i, (en, cn) in enumerate(pairs):
        d_en, d_ec, d_cn = segs[i]
        tracks.append({"i": i + 1, "en": en, "cn": cn,
                       "start": round(t, 2),
                       "end": round(t + d_en + d_ec + d_cn, 2)})
        t += d_en + d_ec + d_cn + gap_word_dur
    return tracks


def silence_gaps(mp3):
    ff = ffmpeg_exe()
    out = run([ff, "-hide_banner", "-i", mp3, "-af", "silencedetect=noise=-40dB:d=0.35",
               "-f", "null", "-"]).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    return dur, starts, ends


def tracks_from_gaps(pairs, dur, starts, ends):
    inter = [(starts[i], ends[i]) for i in range(1, len(starts), 2)]
    tracks = []
    for i, (en, cn) in enumerate(pairs):
        st = 0.0 if i == 0 else inter[i - 1][1]
        ed = dur if i >= len(inter) else inter[i][0]
        tracks.append({"i": i + 1, "en": en, "cn": cn,
                       "start": round(st, 2), "end": round(ed, 2)})
    return tracks


def measure(mp3):
    ff = ffmpeg_exe()
    out = run([ff, "-hide_banner", "-i", mp3, "-f", "null", "-"]).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def make_silence(seconds, out_path):
    ff = ffmpeg_exe()
    run([ff, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(seconds),
         "-c:a", "libmp3lame", "-ar", "24000", out_path], check=False)


def concat(paths, out_path, tmp, reencode=False):
    """把若干 MP3 顺序拼成一个。

    ⚠️ reencode=False（`-c copy`）在**多处拼接**时会出问题：每段自带 LAME 的
    gapless 元数据（编码器延迟/填充），逐段 copy 拼起来后，各 junction 会被裁掉
    一点 —— 实测一页 57 词会少 3.27s。于是「按输入帧数累加」的解析式时间轴
    与输出文件（也就是浏览器看到）的时间轴对不上，词边界会系统性偏移。

    reencode=True 时重新解码再编码成一条连续流：输出时间轴 = 各输入**解码**时长
    的累加，解析式时间轴因此成立。代价是多一次有损编码（只在整页那一次做，质量可接受）。
    """
    ff = ffmpeg_exe()
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", lst]
    if reencode:
        cmd += ["-c:a", "libmp3lame", "-ar", "24000", "-b:a", "48k"]
    else:
        cmd += ["-c", "copy"]
    r = run(cmd + [out_path])
    if not os.path.exists(out_path):
        raise RuntimeError("ffmpeg 拼接失败：%s" % r.stderr[-300:])


def cmd_build(args):
    work = os.path.abspath(args.work)
    csv_path = os.path.join(work, "words.csv")
    if not os.path.isfile(csv_path):
        raise SystemExit("找不到 %s —— 请先跑 parse 子命令并完成校对" % csv_path)

    rows = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            en, cn = norm_text(r.get("en")), norm_text(r.get("cn"))
            if en and cn:
                rows.append((int(r["page"]), en, cn))
    if not rows:
        raise SystemExit("words.csv 里没有有效的词条")

    by_page = {}
    for pg, en, cn in rows:
        by_page.setdefault(pg, []).append((en, cn))
    pages = sorted(by_page)
    log("待合成：%d 页 / %d 条" % (len(pages), len(rows)))

    tts = TTS(args.provider, getattr(args, "proxy", None))
    # 先统计缓存命中，再决定要不要联网（这是"你的文件能省多少 TTS 调用"的答案）
    hit_pairs = sum(1 for _, en, cn in rows
                    if cache_ok(tts, en, EN_VOICE)[1]
                    and cache_ok(tts, cn, CN_VOICE)[1])
    log("词级缓存命中：%d / %d 条（%.0f%%）；需要新建 %d 条"
        % (hit_pairs, len(rows), 100.0 * hit_pairs / len(rows), len(rows) - hit_pairs))

    if len(rows) - hit_pairs > 0:
        if not tts.preflight():
            raise SystemExit("TTS 不可用，且仍有 %d 条不在缓存里 —— 见上面的可选方案"
                             % (len(rows) - hit_pairs))

    tmp = os.path.join(work, "_tmp")
    os.makedirs(tmp, exist_ok=True)
    merge_into = getattr(args, "merge_into", None)
    out_dir = getattr(args, "out_dir", None)
    if merge_into:
        pack_dir = os.path.abspath(merge_into)
        pack_name = args.name or os.path.basename(pack_dir)
    elif out_dir:
        # 目录名由调用方指定（网页生成器用它把暂存包放在 _stage/<job>），
        # --name 只决定播放器标题 —— 两者混用会把随机 job id 写进标题。
        pack_name = args.name or os.path.basename(os.path.abspath(out_dir))
        pack_dir = os.path.abspath(out_dir)
    else:
        pack_name = args.name or os.path.basename(work)
        pack_dir = os.path.join(os.path.abspath(args.out), pack_name)
    audio_dir = os.path.join(pack_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)

    gap_ec = os.path.join(tmp, "gap_ec.mp3")
    gap_word = os.path.join(tmp, "gap_word.mp3")
    make_silence(GAP_EC, gap_ec)
    make_silence(GAP_WORD, gap_word)
    d_gap_ec = mp3_frames(gap_ec)[1]
    d_gap_word = mp3_frames(gap_word)[1]
    log("间隔精确时长（按帧）：英↔中 %.3fs，词↔词 %.3fs" % (d_gap_ec, d_gap_word))

    # ── 阶段一：把**所有**缓存未命中的文本一次并发合成 ──
    # 原来是「一个词两次 asyncio.run(tts.synth(...))」嵌套在拼接循环里：网络往返全串行，
    # 实测 3.60s/条 → 并发 12 后 0.44s/条。拼接是本地 CPU，留在阶段二逐页做，
    # 因此产物的文件顺序、时长、JSON 结构与串行版本完全一致。
    # 按 (文本, 音色) 去重：同一个词在书里出现两次只需合成一次（否则两个协程会抢写同一路径）。
    conc = getattr(args, "tts_concurrency", 0) or TTS_CONCURRENCY
    jobs, seen = [], set()
    for _, en, cn in rows:
        for text, voice in ((en, EN_VOICE), (cn, CN_VOICE)):
            key = (text, voice)
            if key in seen:
                tts.hit += 1
                continue
            seen.add(key)
            p, ok = cache_ok(tts, text, voice)
            if ok:
                tts.hit += 1
            else:
                tts.miss += 1
                jobs.append((text, voice, p))
    if jobs:
        log("并发合成 %d 条（去重后，并发度 %d）…" % (len(jobs), conc))
        t0 = time.time()
        asyncio.run(_synth_all(tts, jobs, conc))
        dt = time.time() - t0
        log("  合成完成 %.1fs（%.2fs/条），全部落进词级缓存" % (dt, dt / len(jobs)))

    audio, page_meta = {}, []
    for n, pg in enumerate(pages, 1):
        pairs = by_page[pg]
        word_files, durs = [], []
        for i, (en, cn) in enumerate(pairs, 1):
            # 阶段一已保证两条缓存都在（命中或刚合成），这里只取路径，不再计数/联网。
            en_p = tts.cached(en, EN_VOICE)
            cn_p = tts.cached(cn, CN_VOICE)
            # 一词一个 MP3：播放某词 = 播这个文件，**根本不需要时间轴**。
            rel = "audio/p%03d_%03d.mp3" % (pg, i)
            wf = os.path.join(pack_dir, rel.replace("/", os.sep))
            concat([en_p, gap_ec, cn_p], wf, tmp, reencode=True)
            word_files.append(rel)
            durs.append(mp3_frames(wf)[1])

        # 整页 MP3：仅供「下载本页 MP3」用，播放不再依赖它 → 用 -c copy 无损直拼
        timeline = []
        for i, wf in enumerate(word_files):
            timeline.append(os.path.join(pack_dir, wf.replace("/", os.sep)))
            if i < len(word_files) - 1:
                timeline.append(gap_word)
        page_mp3 = os.path.join(audio_dir, "p%03d.mp3" % pg)
        concat(timeline, page_mp3, tmp)

        total = sum(durs) + d_gap_word * max(0, len(durs) - 1)
        tracks = [{"i": i + 1, "en": en, "cn": cn, "url": word_files[i]}
                  for i, (en, cn) in enumerate(pairs)]
        audio[str(pg)] = {"duration": round(total, 2),
                          "mp3": "audio/p%03d.mp3" % pg, "tracks": tracks}
        page_meta.append({"page": pg, "words": len(pairs),
                          "first": pairs[0][0], "last": pairs[-1][0],
                          "bytes": os.path.getsize(page_mp3),
                          "sec": round(total, 1),
                          "pairs": [[e, c] for e, c in pairs]})
        log("  [%2d/%d] 第 %d 页  %2d 词  %5.1fs  → %d 个单词音频 + %s"
            % (n, len(pages), pg, len(pairs), total, len(word_files),
               os.path.basename(page_mp3)))

    old_path = os.path.join(pack_dir, "pack_data.json")
    if merge_into and os.path.isfile(old_path):
        # 增量合并：按页号覆盖，其余页与其音频文件原样保留。
        # 真值仍在各页的 words.csv —— 某页内容改了，就对那一页重跑一次 merge。
        with open(old_path, encoding="utf-8") as f:
            old = json.load(f)
        merged_pages = {int(p["page"]): p for p in old.get("pages", [])}
        merged_audio = dict(old.get("audio", {}))
        for p in page_meta:
            merged_pages[int(p["page"])] = p
        merged_audio.update(audio)
        before = len(old.get("pages", []))
        pack_data = {"audio": merged_audio,
                     "pages": [merged_pages[k] for k in sorted(merged_pages)]}
        log("  合并：原有 %d 页 + 本次 %d 页 → 共 %d 页"
            % (before, len(page_meta), len(pack_data["pages"])))
    else:
        pack_data = {"audio": audio, "pages": page_meta}
    with open(os.path.join(pack_dir, "pack_data.json"), "w", encoding="utf-8") as f:
        json.dump(pack_data, f, ensure_ascii=False, indent=1)
    shutil.rmtree(tmp, ignore_errors=True)

    render_pack(pack_dir, pack_data, pack_name)
    log("\n✅ 词库包已生成：%s" % pack_dir)
    log("   双击 %s 即可打开；整个文件夹拷给别人也能用（无需联网）。" % os.path.join(pack_dir, "index.html"))
    log("   TTS 调用：命中缓存 %d 次 / 实际新建 %d 次" % (tts.hit, tts.miss))
    return 0


def render_pack(pack_dir, data, title):
    """把数据内联进播放器模板，产出一个可双击的独立包"""
    tpl = os.path.abspath(SKETCH_TEMPLATE)
    if not os.path.isfile(tpl):
        raise SystemExit("找不到播放器模板：%s" % tpl)
    html = open(tpl, encoding="utf-8").read()
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    # replacement 必须传**函数**：字符串 replacement 会把 `\n`、`\1`、`\\`
    # 再解释一次 —— 于是原文 `C:\new` 进到播放器里就变成真的换行（P2）。
    html, n = re.subn(r"^const DATA = .*;$",
                      lambda m: "const DATA = " + compact + ";",
                      html, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("模板里没找到 DATA 注入点")
    html = html.replace("<title>外语词汇听读播放器 · 界面草图</title>",
                        "<title>%s · 外语词汇听读播放器</title>" % title)
    # ⚠️ brand 必须**定点**替换：全局 replace("外语词汇听读播放器", title) 会把上一行刚
    # 写进 <title> 的那半个名字也一起吃掉，结果变成「X · X」。
    html = html.replace("▶</span>外语词汇听读播放器</div>",
                        "▶</span>%s</div>" % title)
    html = html.replace("界面草图 · 形态③（纯 HTML 播放器）", "外语词汇听读播放器")
    out = os.path.join(pack_dir, "index.html")
    open(out, "w", encoding="utf-8", newline="").write(html)
    return out


# ────────────────────────────── render ──────────────────────────────
def cmd_render(args):
    """只按模板重渲染 index.html：读现有 pack_data.json，不重跑 TTS / concat。

    用途：改了播放器模板（文案、样式）后，让已生成的包立即跟上，
    而不是为了改一行字把几百个词的音频重新拼一遍。
    """
    pack_dir = os.path.abspath(args.pack)
    data_path = os.path.join(pack_dir, "pack_data.json")
    if not os.path.isfile(data_path):
        raise SystemExit("找不到 %s —— 这里应该是一个已生成的词库包" % data_path)
    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "pages" not in data:
        raise SystemExit("%s 格式不符（缺 pages）" % data_path)
    name = args.name or os.path.basename(pack_dir)
    out = render_pack(pack_dir, data, name)
    log("✅ 已重渲染：%s" % out)
    log("   音频未改动；双击 index.html 即可查看新版界面。")
    return 0


# ────────────────────────────── doctor ──────────────────────────────
def cmd_doctor(args):
    log("== 环境自检 ==")
    log("Python      : %s" % sys.version.split()[0])
    # ⚠️ 模块名 ≠ 安装名：`docx` 模块由包 `python-docx` 提供（`pip install docx` 装的是**另一个**老包）
    for mod, why, pkg in (("pymupdf", "PDF 抽取", "pymupdf"),
                          ("edge_tts", "TTS（edge）", "edge-tts>=7.3.1"),
                          ("imageio_ffmpeg", "音频拼接/静音检测", "imageio-ffmpeg"),
                          ("docx", "Word 解析（可选）", "python-docx")):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "") or ""
            if mod == "edge_tts":
                nums = tuple(int(x) for x in re.findall(r"\d+", ver)[:2])
                mark = "✅" if nums >= (7, 3) else "⚠️"
                extra = "" if nums >= (7, 3) else "   ← 国内出口需 >= 7.3.1（v2 令牌）"
                log("%s %-16s %-18s %s%s" % (mark, mod, why, ver, extra))
            else:
                log("✅ %-16s %-18s %s" % (mod, why, ver))
        except ImportError:
            log("❌ %-16s %-18s → pip install %s" % (mod, why, pkg))
    log("ffmpeg      : %s" % (ffmpeg_exe() or "❌ 未找到"))
    log("模板        : %s" % ("✅ " + os.path.abspath(SKETCH_TEMPLATE)
                              if os.path.isfile(SKETCH_TEMPLATE) else "❌ 找不到"))
    cache_n = len(os.listdir(CACHE_DIR)) if os.path.isdir(CACHE_DIR) else 0
    log("词级缓存    : %d 个片段" % cache_n)
    log("")
    TTS(getattr(args, "provider", "edge")).preflight()
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="build_vocab.py",
        description="把文件转成可双击的外语词汇听读播放器包（两步式：parse → 校对 → build）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("parse", help="解析文件并生成待校对的 words.csv（不需要联网/TTS）")
    p1.add_argument("--in", dest="input", required=True, help="输入文件：pdf / docx / txt")
    p1.add_argument("--out", required=True, help="工作目录（存放 words.csv 与 report.txt）")
    p1.add_argument("--max-pages", type=int, default=0, help="只处理前 N 页（调试用）")
    p1.add_argument("--page", type=int, default=0,
                    help="只处理第 N 页（按需逐页生成就用这个；配 --word-pages 时 N = Word 真实页码）")
    p1.add_argument("--min-page-score", type=float, default=0.60,
                    help="页均分低于此值就判为非词表页并跳过（默认 0.60）")
    p1.add_argument("--keep-low", action="store_true", help="不跳过低分页（强制保留）")
    p1.add_argument("--max-words", type=int, default=0,
                    help="只取前 N 条（按条数截断，与真实分页无关）")
    p1.add_argument("--word-pages",
                    help="Word COM 导出的真实页码 JSON（_word_pages.ps1 产出）。"
                         "给了它 --max-pages 1 才等于「真的第一页」")
    p1.set_defaults(func=cmd_parse)

    p2 = sub.add_parser("build", help="合成音频并打包成词库包（需要 TTS 可用）")
    p2.add_argument("--work", required=True, help="parse 生成的工作目录（读其中的 words.csv）")
    p2.add_argument("--name", help="词库名（默认取工作目录名）")
    p2.add_argument("--out", default="packs", help="输出目录（默认 ./packs）")
    p2.add_argument("--out-dir",
                    help="直接指定包目录（优先级高于 --out；--name 只作展示名）")
    p2.add_argument("--provider", default="edge", choices=["edge", "azure"])
    p2.add_argument("--proxy", help="HTTP 代理，如 http://127.0.0.1:7890 —— edge-tts 被地区限制时优先试这个")
    p2.add_argument("--merge-into",
                    help="把本次的页增量并入已有包目录（同页号覆盖，其余页原样保留）。"
                         "逐页扩充同一个 index 就用它：先正常 build 一次，之后每页都加这个参数")
    p2.add_argument("--tts-concurrency", type=int, default=0,
                    help="TTS 并发度（默认 %d）。网络差时调小，别超过 16 —— edge-tts 会限流"
                         % TTS_CONCURRENCY)
    p2.set_defaults(func=cmd_build)

    p3 = sub.add_parser("doctor", help="环境自检")
    p3.add_argument("--provider", default="edge")
    p3.set_defaults(func=cmd_doctor)

    p4 = sub.add_parser("render", help="只按模板重渲染 index.html（读 pack_data.json，不重合成）")
    p4.add_argument("--pack", required=True, help="已有包目录（含 pack_data.json 与 audio/）")
    p4.add_argument("--name", help="标题（默认取目录名）")
    p4.set_defaults(func=cmd_render)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
