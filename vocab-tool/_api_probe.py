# -*- coding: utf-8 -*-
"""接口门禁（不需要浏览器）：起一个真实 serve.py，用真实 HTTP 打全部接口。

为什么要有它：
    `_probe_pack.py` 只验**成品包**；网页生成器的接口层（token 分级、上传拒绝分支、
    分组规则、任务状态机）以前没有任何断言。这些分支里只要有一个漏判，
    就是「静默接受了不该接受的东西」—— 比报错更难发现。

用法：
    python -X utf8 _api_probe.py           # 跑完清理本次产生的上传/任务文件
    python -X utf8 _api_probe.py --keep    # 保留样本与任务快照，方便排查

断言不是「实现调自己」：
    · 上传后**独立重算** sha256 与本地字节比对，不用服务端返回值证明自己；
    · 分组的期望值来自**测试样本本身的词数**（120 词 → 50/50/20），不是读 group_rows 的公式；
    · 拒绝分支断言的是**状态码 + 磁盘上没留下文件**两件事。
"""
import glob
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SAMPLES = os.path.join(HERE, "work", "_probe")
TOK = "probe-" + uuid.uuid4().hex
BADTOK = "probe-" + uuid.uuid4().hex

RESULTS = []
CREATED_DOCS = []
CREATED_JOBS = []


def T(name, ok, extra=None):
    RESULTS.append(("PASS" if ok else "FAIL", name, extra))
    return bool(ok)


def S(name, why):
    RESULTS.append(("SKIP", name, why))


# ────────────────────────────── 样本 ──────────────────────────────
PAIRS20 = [("apple", "苹果"), ("banana", "香蕉"), ("orange", "橙子"),
           ("grape", "葡萄"), ("peach", "桃子"), ("lemon", "柠檬"),
           ("mango", "芒果"), ("cherry", "樱桃"), ("strawberry", "草莓"),
           ("watermelon", "西瓜"), ("potato", "土豆"), ("tomato", "西红柿"),
           ("carrot", "胡萝卜"), ("onion", "洋葱"), ("cabbage", "卷心菜"),
           ("pumpkin", "南瓜"), ("cucumber", "黄瓜"), ("pepper", "辣椒"),
           ("mushroom", "蘑菇"), ("garlic", "大蒜")]


def pairs_n(n):
    return [("word%03d" % i, "第%d个词" % i) for i in range(1, n + 1)]


def make_txt(pairs):
    return "\n".join("%s\t%s" % (en, cn) for en, cn in pairs).encode("utf-8") + b"\n"


def make_docx(path, pairs):
    import docx
    d = docx.Document()
    d.add_paragraph("中学英语词汇表（接口门禁样本）")     # 非词条行，应被跳过
    for i, (en, cn) in enumerate(pairs, 1):
        d.add_paragraph("%d %s [test] n. %s" % (i, en, cn))
    d.save(path)


def make_pdf(path, pairs):
    """两列版式（左英文 x=60 / 右中文 x=300）。

    行距特意给到 56pt：`pairs_from_coords()` 的 `cluster_by_y()` 是按
    「相邻 token 的 y 差 ≤ 自适应 gap（上限 40）」并行的，行距过密（如 26pt）
    会把整列并成一条 —— 那是**既有解析器的已知边界**，不是本门禁要验的东西。
    这里只验「PDF 能走通 upload→parse→preview」，版式识别准确率不在 D1 范围。
    """
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72.0
    for en, cn in pairs:
        page.insert_text((60, y), en, fontsize=12)
        page.insert_text((300, y), cn, fontname="china-s", fontsize=12)
        y += 56
    doc.save(path)
    doc.close()


# ────────────────────────────── HTTP ──────────────────────────────
def req(port, method, path, body=None, headers=None, timeout=60):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def get(port, path, timeout=60):
    return req(port, "GET", path, timeout=timeout)


def post_json(port, path, obj, tok=TOK):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json; charset=utf-8",
         "Content-Length": str(len(body))}
    if tok is not None:
        h["X-Token"] = tok
    return req(port, "POST", path, body, h)


def upload(port, name, data, tok=TOK):
    h = {"X-Filename": urllib.parse.quote(name),
         "Content-Length": str(len(data))}
    if tok is not None:
        h["X-Token"] = tok
    return req(port, "POST", "/api/upload", data, h,
               timeout=90 if len(data) > 1048576 else 30)


def raw(port, data, half_close=True, timeout=15):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(data)
        if half_close:
            s.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            try:
                c = s.recv(65536)
            except socket.timeout:
                break
            if not c:
                break
            buf += c
    finally:
        try:
            s.close()
        except OSError:
            pass
    text = buf.decode("utf-8", "replace")
    head, _, body = text.partition("\r\n\r\n")
    try:
        code = int(head.split()[1])
    except Exception:
        code = 0
    return code, body


def jload(body):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, proc, seconds=25):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if proc.poll() is not None:
            return False
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def poll_job(port, jid, timeout=180):
    """轮询到终态；顺带检查 progress 单调不减。返回 (job, 秒数)。"""
    t0 = time.time()
    last = -1.0
    mono = True
    while time.time() - t0 < timeout:
        st, body = get(port, "/api/job/" + jid, timeout=20)
        if st != 200:
            return None, time.time() - t0
        j = jload(body)
        p = float(j.get("progress") or 0)
        if p < last:
            mono = False
        last = p
        if j.get("state") in ("done", "error", "canceled", "interrupted"):
            j["_monotonic"] = mono
            return j, time.time() - t0
        time.sleep(0.35)
    return None, time.time() - t0


# ────────────────────────────── 主流程 ──────────────────────────────
def main():
    keep = "--keep" in sys.argv
    os.makedirs(SAMPLES, exist_ok=True)
    port = free_port()
    logpath = os.path.join(SAMPLES, "serve.log")
    env = dict(os.environ, PET_API_TOKEN=TOK, PYTHONIOENCODING="utf-8")
    logf = open(logpath, "w", encoding="utf-8")
    proc = subprocess.Popen([PY, os.path.join(HERE, "serve.py"), str(port),
                             "--host", "127.0.0.1"],
                            cwd=HERE, env=env, stdout=logf, stderr=subprocess.STDOUT)
    try:
        if not wait_port(port, proc):
            logf.flush()
            print("服务没起来，日志尾：")
            print(open(logpath, encoding="utf-8", errors="replace").read()[-2000:])
            return 2
        run(port)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        logf.close()
        if not keep:
            cleanup()
    return report()


def run(port):
    # ── A. 只读接口免 token ────────────────────────────────────────
    st, body = get(port, "/api/packs")
    data = jload(body)
    packs = data.get("packs") or []
    T("GET /api/packs 无 token 可访问", st == 200, st)
    names = [p.get("id") for p in packs]
    T("词库列表非空且字段齐全",
      bool(packs) and all(k in packs[0] for k in ("id", "name", "pages", "words", "url")),
      names[:5])
    real = next((p for p in packs if p.get("id") == "初中英语词汇表"), None)
    if real:
        # 真值源是磁盘上的 pack_data.json，不是写死的「4 页」——
        # 这个词库会随使用不断并新页，硬编码的期望值必然过期（踩过一次）。
        pj = os.path.join(HERE, "packs", "初中英语词汇表", "pack_data.json")
        with open(pj, encoding="utf-8") as f:
            truth = json.load(f)
        want_pages = len(truth.get("pages") or [])
        want_words = sum(int(pg.get("words") or 0) for pg in truth.get("pages") or [])
        T("已存在词库的页数/词数与 pack_data.json 一致",
          real["pages"] == want_pages and real["words"] == want_words,
          "%s 页 / %s 词 vs %s / %s" % (real["pages"], real["words"], want_pages, want_words))
    else:
        S("已存在词库摘要", "packs/初中英语词汇表 不存在")

    st, _ = get(port, "/api/job/" + "0" * 12)
    T("GET /api/job/<不存在> 无 token 也返回 404（不是 403）", st == 404, st)
    st, _ = get(port, "/api/nope")
    T("未知 GET /api/* → 404 JSON", st == 404, st)

    # ── B. 写接口必须带 token ─────────────────────────────────────
    for path, payload in (("/api/upload", b"MZ\x90\x00"),
                          ("/api/parse", b"{}"),
                          ("/api/build", b"{}"),
                          ("/api/cancel", b"{}")):
        st, body = req(port, "POST", path,
                       payload, {"Content-Length": str(len(payload))})
        T("%s 缺 token → 403" % path, st == 403, st)
    st, body = post_json(port, "/api/parse", {"doc_id": "x" * 32}, tok=BADTOK)
    T("/api/parse 错 token → 403", st == 403, st)
    T("403 响应体是 JSON（带 error 字段）",
      "error" in jload(body), body[:80])
    st, _ = post_json(port, "/api/build", {}, tok=None)
    T("/api/build 缺 token 先被 403（不是 501，说明 token 先于业务判定）", st == 403, st)

    # ── C. 上传拒绝分支 ──────────────────────────────────────────
    st, body = upload(port, "evil.exe", b"MZ\x90\x00")
    T("扩展名 .exe → 400", st == 400, "%s %s" % (st, jload(body).get("error")))
    st, body = upload(port, "fake.docx", b"this is not a zip")
    T("docx 魔数不符 → 400", st == 400, "%s %s" % (st, jload(body).get("error")))
    st, body = upload(port, "fake.pdf", b"PK\x03\x04 not a pdf")
    T("pdf 魔数不符 → 400", st == 400, "%s %s" % (st, jload(body).get("error")))
    st, body = upload(port, "empty.txt", b"")
    T("空文件 → 400", st == 400, "%s %s" % (st, jload(body).get("error")))
    st, body = upload(port, "bin.txt", b"abc\x00def")
    T("txt 里带 NUL → 400（不是文本）", st == 400, "%s %s" % (st, jload(body).get("error")))

    # 缺 Content-Length
    code, body = raw(port, (
        "POST /api/upload HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "X-Token: %s\r\nX-Filename: a.txt\r\n\r\n" % TOK).encode("ascii"))
    T("缺 Content-Length → 411", code == 411, "%s %s" % (code, body[:80]))

    # >20MB（只发头，不发 20MB 正文）
    code, body = raw(port, (
        "POST /api/upload HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "X-Token: %s\r\nX-Filename: big.txt\r\n"
        "Content-Length: %d\r\n\r\n" % (TOK, 20 * 1024 * 1024 + 1)).encode("ascii"))
    T("Content-Length > 20MB → 413", code == 413, "%s %s" % (code, body[:80]))

    # 声明 100 实际 10（半路断流）
    code, body = raw(port, (
        "POST /api/upload HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "X-Token: %s\r\nX-Filename: cut.txt\r\n"
        "Content-Length: 100\r\n\r\nabcde12345" % TOK).encode("ascii"))
    T("上传长度不符 → 400", code == 400, "%s %s" % (code, body[:80]))

    upl = os.path.join(HERE, "work", "_uploads")
    before = set(os.listdir(upl)) if os.path.isdir(upl) else set()

    # ── D. 20 词 txt 全链路 ──────────────────────────────────────
    txt20 = make_txt(PAIRS20)
    st, body = upload(port, "测试20词.txt", txt20)
    meta = jload(body)
    T("上传 txt → 200 且返回 doc_id", st == 200 and len(meta.get("doc_id", "")) == 32,
      st)
    CREATED_DOCS.append(meta.get("doc_id"))
    T("服务端 sha256 = 本地重算值（独立真值）",
      meta.get("sha256") == hashlib.sha256(txt20).hexdigest(),
      meta.get("sha256"))
    T("上传后文件名/大小如实回报",
      meta.get("name") == "测试20词.txt" and meta.get("size") == len(txt20)
      and meta.get("ext") == ".txt",
      "%s %s %s" % (meta.get("name"), meta.get("size"), meta.get("ext")))

    st, body = post_json(port, "/api/parse",
                         {"doc_id": meta["doc_id"], "with_pages": False})
    jid = jload(body).get("job_id")
    T("POST /api/parse 返回 job_id", st == 200 and bool(jid), st)
    job, sec = poll_job(port, jid or "0" * 12)
    CREATED_JOBS.append(jid)
    T("20 词 txt 解析到 done", job is not None and job.get("state") == "done",
      (job or {}).get("state") or "timeout after %.1fs" % sec)
    T("解析任务 progress 单调不减", bool((job or {}).get("_monotonic", True)))
    res = (job or {}).get("result") or {}
    T("未勾选真实页码时按固定条数分组：20 词 → 1 组",
      res.get("groups") == 1 and res.get("words") == 20 and res.get("paged") is False,
      res)
    T("未分页时措辞用「组」不用「页」", res.get("unit") == "组", res.get("unit"))

    st, body = get(port, "/api/preview?job=" + (jid or ""))
    pv = jload(body)
    T("GET /api/preview 免 token 可访问", st == 200, st)
    g0 = (pv.get("groups") or [{}])[0]
    T("预览 total = 20 且 paged=false",
      pv.get("total") == 20 and pv.get("paged") is False, pv.get("total"))
    T("预览首尾词与样本一致（apple / garlic，期望值来自样本）",
      g0.get("first") == "apple" and g0.get("last") == "garlic",
      "%s / %s" % (g0.get("first"), g0.get("last")))
    T("预览词条内容与样本逐条一致",
      g0.get("pairs") == [[en, cn] for en, cn in PAIRS20],
      (g0.get("pairs") or [])[:2])

    st, _ = get(port, "/api/preview")
    T("预览缺 job 参数 → 4xx", 400 <= st < 500, st)

    # ── E. 固定条数分组：120 词 → 50/50/20 ───────────────────────
    txt120 = make_txt(pairs_n(120))
    st, body = upload(port, "120词.txt", txt120)
    d120 = jload(body).get("doc_id")
    CREATED_DOCS.append(d120)
    st, body = post_json(port, "/api/parse", {"doc_id": d120})
    jid120 = jload(body).get("job_id")
    CREATED_JOBS.append(jid120)
    job120, _ = poll_job(port, jid120 or "0" * 12)
    st, body = get(port, "/api/preview?job=" + (jid120 or ""))
    pv120 = jload(body)
    sizes = [g.get("words") for g in (pv120.get("groups") or [])]
    T("120 词按 50 条切组：组数=3 且每组 50/50/20",
      sizes == [50, 50, 20] and (job120 or {}).get("state") == "done", sizes)
    T("组号连续从 1 开始",
      [g.get("page") for g in (pv120.get("groups") or [])] == [1, 2, 3],
      [g.get("page") for g in (pv120.get("groups") or [])])

    # ── F. docx / pdf 各一份 ────────────────────────────────────
    dpath = os.path.join(SAMPLES, "s.docx")
    make_docx(dpath, PAIRS20[:10])
    with open(dpath, "rb") as f:
        dbytes = f.read()
    st, body = upload(port, "s.docx", dbytes)
    dmeta = jload(body)
    CREATED_DOCS.append(dmeta.get("doc_id"))
    T("docx 上传通过（魔数校验）", st == 200, "%s %s" % (st, jload(body).get("error")))
    if st == 200:
        st, body = post_json(port, "/api/parse", {"doc_id": dmeta["doc_id"]})
        jd = jload(body).get("job_id")
        CREATED_JOBS.append(jd)
        jdobj, _ = poll_job(port, jd or "0" * 12)
        rd = (jdobj or {}).get("result") or {}
        T("docx 解析到 done 且抽出 10 词",
          (jdobj or {}).get("state") == "done" and rd.get("words") == 10,
          "%s %s" % ((jdobj or {}).get("state"), rd.get("words")))

    ppath = os.path.join(SAMPLES, "s.pdf")
    make_pdf(ppath, PAIRS20[:8])
    with open(ppath, "rb") as f:
        pbytes = f.read()
    st, body = upload(port, "s.pdf", pbytes)
    pmeta = jload(body)
    CREATED_DOCS.append(pmeta.get("doc_id"))
    T("pdf 上传通过（魔数校验）", st == 200, "%s %s" % (st, jload(body).get("error")))
    if st == 200:
        st, body = post_json(port, "/api/parse", {"doc_id": pmeta["doc_id"]})
        jp = jload(body).get("job_id")
        CREATED_JOBS.append(jp)
        jpobj, _ = poll_job(port, jp or "0" * 12)
        rp = (jpobj or {}).get("result") or {}
        T("pdf 解析到 done 且抽出 8 词",
          (jpobj or {}).get("state") == "done" and rp.get("words") == 8,
          "%s %s" % ((jpobj or {}).get("state"), rp.get("words")))
        T("pdf 按物理页分（paged=true，措辞为「页」）",
          rp.get("paged") is True and rp.get("unit") == "页", rp)

    # ── G. 并发上传不串号 ──────────────────────────────────────
    after = set(os.listdir(upl)) if os.path.isdir(upl) else set()
    new_files = after - before
    bucket = {}
    lock = threading.Lock()

    def one(i):
        body = make_txt([("conc%d" % i, "并发%d" % i)])
        st, resp = upload(port, "并发%d.txt" % i, body)
        with lock:
            bucket[i] = (st, jload(resp), hashlib.sha256(body).hexdigest())

    ths = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    ok = True
    ids = set()
    for i, (st, meta_i, sha) in bucket.items():
        ids.add(meta_i.get("doc_id"))
        if not (st == 200 and meta_i.get("name") == "并发%d.txt" % i
                and meta_i.get("sha256") == sha):
            ok = False
    T("3 路并发上传：各自名字/内容/哈希都不串",
      ok and len(ids) == 3 and len(bucket) == 3,
      sorted(m.get("name") for _, m, _ in bucket.values()))
    for _, meta_i, _ in bucket.values():
        if meta_i.get("doc_id"):
            CREATED_DOCS.append(meta_i["doc_id"])
    T("每次上传在 work/_uploads 各留一个文件（数量对得上）",
      len(new_files) >= 3, len(new_files))

    # ── H. 词库 id 越界（函数级，D3 的 /api/build 复用同一判定）────
    import webgen
    bad = ["", "  ", "../packs", "a/b", "a\\b", "..", ".", "C:\\Windows",
           "a:b", "x*y", "a" * 90]
    rejected = []
    for b in bad:
        try:
            webgen.resolve_pack_id(b)
            rejected.append("漏判:" + repr(b))
        except webgen.WebError:
            pass
    T("非法词库 id 全部拒绝（路径穿越 / 非法字符 / 超长）",
      not rejected, rejected)
    try:
        p = webgen.resolve_pack_id("初中英语词汇表")
        inside = os.path.realpath(p) == os.path.join(os.path.realpath(webgen.PACKS),
                                                     "初中英语词汇表")
    except webgen.WebError as e:
        inside = False
    T("合法词库 id 解析到 packs/<id> 内", inside)

    # ── I. 参数校验 / 未实现的功能要如实报错 ────────────────────
    st, _ = post_json(port, "/api/parse", {"doc_id": "f" * 32})
    T("/api/parse 未知 doc_id → 404", st == 404, st)
    code, body = raw(port, (
        "POST /api/parse HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "X-Token: %s\r\n\r\n" % TOK).encode("ascii"))
    T("/api/parse 缺 Content-Length → 411", code == 411, code)

    st, body = post_json(port, "/api/build", {"doc_id": meta["doc_id"]})
    T("/api/build 缺 rows → 400", st == 400, "%s %s" % (st, jload(body).get("error")))
    st, body = post_json(port, "/api/build", {"doc_id": meta["doc_id"],
                                              "rows": [{"page": 1, "en": "a"}],
                                              "mode": "merge", "pack_id": "初中英语词汇表"})
    T("/api/build 某条缺中文 → 400", st == 400, "%s %s" % (st, jload(body).get("error")))
    st, body = post_json(port, "/api/build", {"doc_id": meta["doc_id"],
                                              "rows": [{"page": 0, "en": "a", "cn": "甲"}],
                                              "mode": "merge", "pack_id": "初中英语词汇表"})
    T("/api/build 组号 0 → 400", st == 400, "%s %s" % (st, jload(body).get("error")))
    st, body = post_json(port, "/api/build", {"doc_id": meta["doc_id"],
                                              "rows": [{"page": 1, "en": "a", "cn": "甲"}],
                                              "mode": "merge", "pack_id": "../../etc"})
    T("/api/build 非法词库 id（路径穿越）→ 400", st == 400, st)
    st, body = post_json(port, "/api/build", {"doc_id": meta["doc_id"],
                                              "rows": [{"page": 1, "en": "a", "cn": "甲"}],
                                              "mode": "merge", "pack_id": "不存在的词库xyz"})
    T("/api/build 不存在的词库 → 404", st == 404, "%s %s" % (st, jload(body).get("error")))

    # ── J. 新建独立词库（真实合成 5 个词）────────────────────────
    test_build_new(port)

    # ── K0. 并入分级判据（不跑 TTS，直接喂 _dup_report）──────────
    test_dup_grading()

    # ── K. 并入现有词库：旧内容一个字节都不许变 ──────────────────
    test_build_merge(port)

    # ── L. 原子替换：换不过去时旧包必须完好 ──────────────────────
    test_atomic_rollback()

    # ── M. 取消：进程真死、旧包不变、锁不卡死 ────────────────────
    test_cancel_build(port)

    # ── N. 重启后残留任务标 interrupted（不假装能续跑）───────────
    test_interrupted_on_restart()

    # ── O. Word 真实分页（勾选才跑；含缓存命中）─────────────────
    test_word_pages(port)

    # ── O2. 扫描件 PDF：必须给「去走 OCR」的可行动提示，不许空词库 ──
    test_scanned_pdf(port)

    # ── P. 工作台编辑：改名 / 删除（全部用副本）──────────────────
    test_pack_manage(port)


def test_scanned_pdf(port):
    """扫描件 PDF（有页面、无文字层）：任务要失败，且失败原因要指路，不是"0 词"。

    现场造一份只有色块的 PDF，不在仓库里放二进制样本。
    """
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(60, 60, 420, 420),
                   color=(0, 0, 0), fill=(0.85, 0.85, 0.85))
    data = doc.tobytes()
    doc.close()

    st, resp = upload(port, "扫描件.pdf", data)
    meta = jload(resp)
    CREATED_DOCS.append(meta.get("doc_id"))
    T("扫描件 PDF 能上传（魔数 %PDF- 仍通过）",
      st == 200 and bool(meta.get("doc_id")), "%s %s" % (st, meta.get("error")))
    if st != 200 or not meta.get("doc_id"):
        return

    st, resp = post_json(port, "/api/parse", {"doc_id": meta["doc_id"]})
    jid = jload(resp).get("job_id")
    CREATED_JOBS.append(jid)
    job, _ = poll_job(port, jid or "0" * 12)
    job = job or {}
    err = job.get("error") or ""
    T("扫描件 → 报「扫描件」并指路（不是笼统退出码）",
      job.get("state") == "error" and "看起来是扫描件" in err,
      "%s | %s" % (job.get("state"), err[:70]))
    parse_dir = os.path.join(HERE, "work", "_parse", meta["doc_id"])
    T("扫描件 → 不产出 words.csv（不生成空词库）",
      not os.path.isfile(os.path.join(parse_dir, "words.csv")), parse_dir)


def snapshot_dir(root):
    out = {}
    for dp, _, fns in os.walk(root):
        for f in fns:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, root).replace("\\", "/")
            with open(p, "rb") as fh:
                out[rel] = (os.path.getsize(p), hashlib.sha256(fh.read()).hexdigest())
    return out


def _build_and_wait(port, doc_id, rows, mode, pack_id, name=None):
    body = {"doc_id": doc_id, "rows": rows, "mode": mode, "pack_id": pack_id}
    if name:
        body["name"] = name
    st, resp = post_json(port, "/api/build", body)
    jid = jload(resp).get("job_id")
    CREATED_JOBS.append(jid)
    if st != 200 or not jid:
        return None, "%s %s" % (st, jload(resp).get("error"))
    job, _ = poll_job(port, jid, timeout=300)
    return job, jid


def test_build_new(port):
    # 不能用 "_" 前缀：list_packs() 会把 "_" 开头的目录当成门禁产物跳过（工作台要干净）
    name = "probe_new_%s" % uuid.uuid4().hex[:6]
    pairs = [("alpha", "阿尔法"), ("beta", "贝塔"), ("gamma", "伽马"),
             ("delta", "德尔塔"), ("epsilon", "艾普西龙")]
    st, resp = upload(port, "新建5词.txt", make_txt(pairs))
    meta = jload(resp)
    CREATED_DOCS.append(meta.get("doc_id"))
    st, resp = post_json(port, "/api/parse", {"doc_id": meta["doc_id"]})
    jid = jload(resp).get("job_id")
    CREATED_JOBS.append(jid)
    job, _ = poll_job(port, jid or "0" * 12)
    st, resp = get(port, "/api/preview?job=" + (jid or ""))
    pv = jload(resp)
    rows = [{"page": g["page"], "en": p[0], "cn": p[1]}
            for g in (pv.get("groups") or []) for p in g["pairs"]]
    T("新建：解析出 5 条可提交的词", len(rows) == 5, len(rows))

    job, info = _build_and_wait(port, meta["doc_id"], rows, "new", name, name)
    T("新建：生成任务到 done", (job or {}).get("state") == "done",
      (job or {}).get("state") or info)
    res = (job or {}).get("result") or {}
    T("新建：返回 added_pages=1 / words=5 / pages=1",
      res.get("added_pages") == 1 and res.get("words") == 5 and res.get("pages") == 1,
      res)
    pack = os.path.join(HERE, "packs", name)
    ok_files = (os.path.isfile(os.path.join(pack, "index.html"))
                and os.path.isfile(os.path.join(pack, "pack_data.json")))
    T("新建：包目录含 index.html + pack_data.json", ok_files, pack)
    audio = os.path.join(pack, "audio")
    mp3s = sorted(os.listdir(audio)) if os.path.isdir(audio) else []
    T("新建：音频 = 5 个词 + 1 个整页（都非空）",
      len(mp3s) == 6 and all(os.path.getsize(os.path.join(audio, m)) > 0 for m in mp3s),
      mp3s)
    st, resp = get(port, "/api/packs")
    pk = jload(resp).get("packs") or []
    T("新建：词库列表里出现了它", any(p["id"] == name for p in pk), name)
    # 工作台过滤规则："_" 开头的是门禁/调试产物，不许出现在列表里
    hidden = "_probe_hidden_%s" % uuid.uuid4().hex[:6]
    hd = os.path.join(HERE, "packs", hidden)
    os.makedirs(hd, exist_ok=True)
    with open(os.path.join(hd, "pack_data.json"), "w", encoding="utf-8") as f:
        f.write('{"audio": {}, "pages": []}')
    with open(os.path.join(hd, "index.html"), "w", encoding="utf-8") as f:
        f.write("<title>x</title>")
    st, resp = get(port, "/api/packs")
    ids = [p["id"] for p in (jload(resp).get("packs") or [])]
    T("「_」开头的目录不出现在词库列表里", hidden not in ids, ids[:5])
    shutil.rmtree(hd, ignore_errors=True)
    # 字段完整性：工作台卡片要用的三个新字段
    mine = [p for p in (jload(get(port, "/api/packs")[1]).get("packs") or [])
            if p["id"] == name]
    got = mine[0] if mine else {}
    T("词库列表带 sec / bytes / mtime 三个字段",
      all(k in got for k in ("sec", "bytes", "mtime")), sorted(got.keys()))
    # max_page：并入时新页从 max_page+1 起排。前端用它算"排到第几页"，
    # 用页数会差一页（单页包 pages=1 而页号是 2）。
    T("词库列表带 max_page，且不小于页数",
      ("max_page" in got) and int(got.get("max_page") or 0) >= int(got.get("pages") or 0),
      {k: got.get(k) for k in ("pages", "max_page")})
    # 重名必须拒绝
    st, resp = post_json(port, "/api/build", {"doc_id": meta["doc_id"], "rows": rows,
                                              "mode": "new", "pack_id": name, "name": name})
    T("新建：重名 → 409", st == 409, "%s %s" % (st, jload(resp).get("error")))
    if not keep_flag():
        shutil.rmtree(pack, ignore_errors=True)


def test_build_merge(port):
    src = os.path.join(HERE, "packs", "初中英语词汇表_第2页")
    if not os.path.isdir(src):
        S("并入现有词库", "packs/初中英语词汇表_第2页 不存在")
        return
    dst_name = "_probe_merge_%s" % uuid.uuid4().hex[:6]
    dst = os.path.join(HERE, "packs", dst_name)
    shutil.copytree(src, dst)
    try:
        with open(os.path.join(dst, "pack_data.json"), encoding="utf-8") as f:
            old = json.load(f)
        old_pages = old.get("pages") or []
        old_max = max([int(p.get("page") or 0) for p in old_pages] or [0])
        before = snapshot_dir(dst)
        n_audio_before = len([k for k in before if k.startswith("audio/")])

        pairs = [("zebra", "斑马"), ("yak", "牦牛")]
        st, resp = upload(port, "并入2词.txt", make_txt(pairs))
        meta = jload(resp)
        CREATED_DOCS.append(meta.get("doc_id"))
        st, resp = post_json(port, "/api/parse", {"doc_id": meta["doc_id"]})
        jid = jload(resp).get("job_id")
        CREATED_JOBS.append(jid)
        poll_job(port, jid or "0" * 12)
        st, resp = get(port, "/api/preview?job=" + (jid or ""))
        pv = jload(resp)
        rows = [{"page": g["page"], "en": p[0], "cn": p[1]}
                for g in (pv.get("groups") or []) for p in g["pairs"]]
        T("并入：待并入 2 条（源文件只有 1 组）",
          len(rows) == 2 and (pv.get("groups") or [{}])[0].get("page") == 1, len(rows))

        # 并入是"追加"：同一个词出现第二次，用户就分不清哪页有它。
        # 实测踩法：文件按 50 词一组，第 2 组从第 51 个词开始，而包的第 1 页到第 57 个词
        # → 7 个词两页都有。这里拿包里已有的第一条原样提交，必须在开跑前就被拦下。
        dup = list((old_pages[0].get("pairs") or [["(空)", "-"]])[0])
        st, resp = post_json(port, "/api/build",
                             {"doc_id": meta["doc_id"],
                              "rows": [{"page": 1, "en": dup[0], "cn": dup[1]}],
                              "mode": "merge", "pack_id": dst_name, "name": dst_name})
        err = jload(resp).get("error") or ""
        T("并入：与包内已有词重复 → 409，且点名是哪个词",
          st == 409 and dup[0] in err and "重复" in err,
          "%s %s" % (st, err[:90]))
        T("并入被拦下时目标包一个字节都没动", snapshot_dir(dst) == before,
          "快照不一致")

        # 判据放宽后，「同一页里两条一模一样的词」仍然必须拦 —— 那是真错误，
        # 不是「教材不同单元收了同一个词」（后者跨页，落在包的中间页）。
        st, resp = post_json(port, "/api/build",
                             {"doc_id": meta["doc_id"],
                              "rows": [{"page": 1, "en": "zebra", "cn": "斑马"},
                                       {"page": 1, "en": "zebra", "cn": "斑马"}],
                              "mode": "merge", "pack_id": dst_name, "name": dst_name})
        err = jload(resp).get("error") or ""
        T("并入：同一页内两条一样的词 → 409（判据放宽后仍要拦）",
          st == 409 and "同一页" in err, "%s %s" % (st, err[:90]))
        T("同一页重复被拦时目标包也没动", snapshot_dir(dst) == before, "快照不一致")

        job, info = _build_and_wait(port, meta["doc_id"], rows, "merge", dst_name, dst_name)
        T("并入：生成任务到 done", (job or {}).get("state") == "done",
          (job or {}).get("state") or info)
        res = (job or {}).get("result") or {}
        T("并入：offset = 现有最大页（不覆盖已有页）",
          res.get("offset") == old_max, "%s vs %s" % (res.get("offset"), old_max))
        T("并入：新页 = offset+1", (res.get("new_pages") or []) == [old_max + 1],
          res.get("new_pages"))

        with open(os.path.join(dst, "pack_data.json"), encoding="utf-8") as f:
            new = json.load(f)
        new_pages = {int(p.get("page")): p for p in (new.get("pages") or [])}
        same = all(new_pages.get(int(p.get("page"))) == p for p in old_pages)
        T("并入：旧页的 pack_data 记录逐字段相等", same,
          [p.get("page") for p in old_pages])
        T("并入：页数 = 旧页数 + 1",
          len(new.get("pages") or []) == len(old_pages) + 1,
          "%d → %d" % (len(old_pages), len(new.get("pages") or [])))
        T("并入：新页词数 = 2 且内容对得上",
          (new_pages.get(old_max + 1) or {}).get("words") == 2
          and [list(x) for x in ((new_pages.get(old_max + 1) or {}).get("pairs") or [])]
              == [["zebra", "斑马"], ["yak", "牦牛"]],
          (new_pages.get(old_max + 1) or {}).get("pairs"))

        after = snapshot_dir(dst)
        changed = [k for k, v in before.items()
                   if k not in ("pack_data.json", "index.html") and after.get(k) != v]
        T("并入：旧音频/旧文件的 SHA-256 与大小都不变", not changed, changed[:3])
        n_audio_after = len([k for k in after if k.startswith("audio/")])
        T("并入：音频文件数只多了「2 词 + 1 整页」3 个",
          n_audio_after - n_audio_before == 3,
          "%d → %d" % (n_audio_before, n_audio_after))
        T("并入：pack_data.json 里旧音频条目仍在",
          all(k in (new.get("audio") or {}) for k in (old.get("audio") or {})),
          sorted((old.get("audio") or {}).keys()))
        T("并入：新页每词音频非空",
          all(os.path.getsize(os.path.join(dst, "audio", "p%03d_%03d.mp3"
                                          % (old_max + 1, i))) > 0 for i in (1, 2)))

        # ── 判据分级：书里本来就重复（零星、落在包中间的页）→ 放行，但必须提示 ──
        # 拿第 2 页（包里**非**最后一页）的一条词 + 5 条新词：重复率 1/6 < 30%，
        # 且不在「最后一页 ∩ 本批首页」边界上 → 应当放行并生成，同时在任务里带提示。
        mid = list((old_pages[0].get("pairs") or [["(空)", "-"]])[0])
        soft_rows = [{"page": 1, "en": mid[0], "cn": mid[1]}]
        soft_rows += [{"page": 1, "en": "dupsoft%02d" % i, "cn": "分级测试%d" % i}
                      for i in range(1, 6)]
        job2, info2 = _build_and_wait(port, meta["doc_id"], soft_rows,
                                      "merge", dst_name, dst_name)
        T("分级：零星跨页重复（1/6，重复词在中间页）→ 放行并生成",
          (job2 or {}).get("state") == "done",
          (job2 or {}).get("state") or info2)
        T("分级：放行时任务带 dup_warnings（不许静默）",
          bool((job2 or {}).get("dup_warnings"))
          and mid[0] in json.dumps((job2 or {}).get("dup_warnings") or [],
                                   ensure_ascii=False),
          (job2 or {}).get("dup_warnings"))
        T("分级：result.dup_warnings 也带着（完成页要显示）",
          bool(((job2 or {}).get("result") or {}).get("dup_warnings")),
          ((job2 or {}).get("result") or {}).get("dup_warnings"))
        with open(os.path.join(dst, "pack_data.json"), encoding="utf-8") as f:
            fin = json.load(f)
        pg_new = [p for p in (fin.get("pages") or [])
                  if int(p.get("page") or 0) == old_max + 2]
        T("分级：重复的那条与 5 个新词一起写进新页（共 6 条）",
          bool(pg_new) and int(pg_new[0].get("words") or 0) == 6,
          (pg_new[0].get("words") if pg_new else None))
    finally:
        if not keep_flag():
            shutil.rmtree(dst, ignore_errors=True)


def test_pack_manage(port):
    """工作台「✎ 编辑」的两个接口：改名 / 删除。

    独立真值：改名后去**磁盘上的 index.html** 里找 `<title>`，
    不读接口返回的字段（那个字段就是实现自己填的）。
    """
    src = os.path.join(HERE, "packs", "初中英语词汇表_第2页")
    if not os.path.isdir(src):
        S("词库管理（改名 / 删除）", "packs/初中英语词汇表_第2页 不存在")
        return
    tag = uuid.uuid4().hex[:6]
    old = "probe_mg_%s" % tag
    new = "probe_mg_%s_rn" % tag
    old_dir = os.path.join(HERE, "packs", old)
    new_dir = os.path.join(HERE, "packs", new)
    shutil.copytree(src, old_dir)
    trashed = []
    try:
        # ── token 分级：两个写接口都不能裸调 ──
        st, _ = post_json(port, "/api/pack/rename", {"id": old, "name": new}, tok=None)
        T("改名：无 token → 403", st == 403, st)
        st, _ = post_json(port, "/api/pack/rename", {"id": old, "name": new}, tok=BADTOK)
        T("改名：错 token → 403", st == 403, st)
        st, _ = post_json(port, "/api/pack/delete", {"id": old}, tok=None)
        T("删除：无 token → 403", st == 403, st)

        # ── 五类拒绝 ──
        for payload, name, want in (
                ({"id": old, "name": ""}, "空名", 400),
                ({"id": old, "name": "a/b"}, "含斜杠", 400),
                ({"id": old, "name": "_hidden"}, "下划线开头", 400),
                ({"id": old, "name": "abc."}, "以点结尾", 400),
                ({"id": old, "name": "初中英语词汇表"}, "与现有词库重名", 409),
                ({"id": "no_such_pack_zzz", "name": "x"}, "词库不存在", 404),
                ({"id": "..", "name": "x"}, "id=..（越界）", 400)):
            st, resp = post_json(port, "/api/pack/rename", payload)
            T("改名：%s → %d" % (name, want), st == want,
              "%s %s" % (st, jload(resp).get("error")))
        T("被拒绝的改名一个字节都没动磁盘",
          os.path.isdir(old_dir) and not os.path.isdir(new_dir))

        # ── 成功改名 ──
        # 期望值不能写死「1 页 57 词」：这个探针包在前面已经并过一页（2 页 118 词）。
        # 拿「改名前从接口读到的规模」当真值 —— 改名只该换名字，不该动页数/词数。
        before = next((p for p in (jload(get(port, "/api/packs")[1]).get("packs") or [])
                       if p.get("id") == old), {})
        st, resp = post_json(port, "/api/pack/rename", {"id": old, "name": new})
        pk = jload(resp).get("pack") or {}
        T("改名：返回新 id，页数/词数不变",
          st == 200 and pk.get("id") == new
          and pk.get("pages") == before.get("pages")
          and pk.get("words") == before.get("words"),
          "%s vs 改名前的 %s" % (pk, before))
        ids = [p["id"] for p in (jload(get(port, "/api/packs")[1]).get("packs") or [])]
        T("改名后：列表里旧 id 消失、新 id 出现",
          (old not in ids) and (new in ids), ids)
        html = ""
        pj = os.path.join(new_dir, "index.html")
        if os.path.isfile(pj):
            with open(pj, encoding="utf-8") as f:
                html = f.read()
        T("改名后：包内 index.html 的 <title> 已是新名（重渲染过）",
          ("<title>%s · 外语词汇听读播放器</title>" % new) in html)
        T("改名后：旧名不再留在 index.html 里",
          ("<title>%s · 外语词汇听读播放器</title>" % old) not in html)
        st, resp = post_json(port, "/api/pack/rename", {"id": new, "name": new})
        T("改名：改成同名是幂等（200，目录不动）",
          st == 200 and os.path.isdir(new_dir), st)

        # ── 删除 ──
        st, resp = post_json(port, "/api/pack/delete", {"id": "no_such_pack_zzz"})
        T("删除：词库不存在 → 404", st == 404, st)
        st, resp = post_json(port, "/api/pack/delete", {"id": new})
        d = jload(resp)
        T("删除：返回 trashed=true 与 trash 路径",
          st == 200 and d.get("trashed") is True and bool(d.get("trash")), d)
        T("删除后：packs 下目录已不在", not os.path.isdir(new_dir))
        ids = [p["id"] for p in (jload(get(port, "/api/packs")[1]).get("packs") or [])]
        T("删除后：列表里也消失", new not in ids, ids)
        tp = os.path.join(HERE, d.get("trash") or "")
        T("删除后：work/_trash 里有完整副本（可手工找回）",
          os.path.isdir(tp) and os.path.isfile(os.path.join(tp, "pack_data.json")),
          d.get("trash"))
        trashed.append(tp)
        st, resp = post_json(port, "/api/pack/delete", {"id": new})
        T("重复删除 → 404", st == 404, st)
    finally:
        shutil.rmtree(old_dir, ignore_errors=True)
        shutil.rmtree(new_dir, ignore_errors=True)
        for p in trashed:
            shutil.rmtree(p, ignore_errors=True)


def test_dup_grading():
    """并入分级判据：错位 / 整批重复 → 拦；书里本来就重复 → 放行 + 提示。

    直接喂 `webgen._dup_report`（不跑 TTS、不碰真包、不起服务）。
    每条的期望值来自业务规则本身，不是从实现反推：
      · 同一页两条一模一样的词 = 数据错误（一页不会自己收两次同一个词）
      · 本批几乎所有词都已在包里 = 把同一批词又并了一次
      · 重复词全部卡在「包最后一页」∩「本批首页」= 分页方式对不上
      · 零星两条落在包的中间页 = 教材不同单元收了同一个词，应当放行
    """
    import webgen
    base = os.path.join(HERE, "work", "_probe_dup")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)

    def mkpack(pages):
        d = os.path.join(base, "p_%s" % uuid.uuid4().hex[:6])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "pack_data.json"), "w", encoding="utf-8") as f:
            json.dump({"pages": [{"page": pg, "words": len(ps),
                                  "pairs": [[en, cn] for en, cn in ps]}
                                 for pg, ps in pages]}, f, ensure_ascii=False)
        return d

    def rows_of(items, page=1):
        return [{"page": page, "en": en, "cn": cn} for en, cn in items]

    try:
        # ① 同一页里两条一模一样 → 拦
        pk = mkpack([(1, [("apple", "苹果")])])
        hard, soft, _ = webgen._dup_report(
            rows_of([("zebra", "斑马"), ("zebra", "斑马")]), pk)
        T("分级①：同一页内重复 → 拦，且说的是「同一页」",
          bool(hard) and "同一页" in hard, (hard or "")[:80])

        # ② 整批重复（100% 已在包里）→ 拦
        pk = mkpack([(1, [("apple", "苹果"), ("banana", "香蕉"), ("cherry", "樱桃")])])
        hard, soft, _ = webgen._dup_report(
            rows_of([("apple", "苹果"), ("banana", "香蕉"), ("cherry", "樱桃")]), pk)
        T("分级②：整批重复（100%）→ 拦，且点名到词",
          bool(hard) and "apple" in hard and "重复" in hard, (hard or "")[:80])

        # ③ 边界滑移：5 条重复词全是「包末页的词」且都在「本批首页」→ 拦
        pk = mkpack([(1, [("a1", "甲1"), ("a2", "甲2")]),
                     (2, [("b1", "乙1"), ("b2", "乙2"), ("b3", "乙3"),
                          ("b4", "乙4"), ("b5", "乙5")])])
        items = [("n%02d" % i, "新%d" % i) for i in range(1, 16)]
        items += [("b1", "乙1"), ("b2", "乙2"), ("b3", "乙3"),
                  ("b4", "乙4"), ("b5", "乙5")]
        hard, soft, _ = webgen._dup_report(rows_of(items), pk)
        T("分级③：重复词全卡在页边界（5/20）→ 拦，理由是边界",
          bool(hard) and "边界" in hard, (hard or "")[:80])

        # ④ 书里本来就重复：零星两条落在包的中间页 → 放行 + 必须给提示
        pk = mkpack([(1, [("look", "瞧，看"), ("who", "谁")]),
                     (2, [("work", "工作"), ("at work", "在工作")])])
        items = [("n%02d" % i, "新%d" % i) for i in range(1, 19)]
        items += [("look", "瞧，看"), ("work", "工作")]
        hard, soft, detail = webgen._dup_report(rows_of(items, page=3), pk)
        T("分级④：书内跨单元重复（2/20，落在中间页）→ 放行", hard is None, hard)
        T("分级④：放行时必须给出提示与清单（不许静默）",
          bool(soft) and len(detail) == 2
          and {d["en"] for d in detail} == {"look", "work"}, detail)

        # ⑤ 没有重复 → 既不拦也不提示
        hard, soft, detail = webgen._dup_report(rows_of([("zzz", "无")]), pk)
        T("分级⑤：没有重复 → 既不拦也不提示",
          hard is None and not soft and not detail, (hard, soft))

        # ⑥ 目标包还没有 pack_data.json（新建）→ 一律放行
        empty = os.path.join(base, "p_empty")
        os.makedirs(empty, exist_ok=True)
        hard, soft, detail = webgen._dup_report(rows_of([("apple", "苹果")]), empty)
        T("分级⑥：目标包为空/不存在 → 放行", hard is None and not soft, (hard, soft))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_atomic_rollback():
    """stage 不存在时，_install_stage 必须回滚，旧包一个文件都不能少。"""
    import webgen
    base = os.path.join(HERE, "work", "_probe_rollback")
    target = os.path.join(base, "pack")
    backup = os.path.join(base, "backup")
    stage = os.path.join(base, "stage_missing")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(target)
    with open(os.path.join(target, "pack_data.json"), "w", encoding="utf-8") as f:
        f.write('{"pages": []}')
    try:
        webgen._install_stage(stage, target, backup)
        raised = False
    except Exception:
        raised = True
    ok = (raised and os.path.isfile(os.path.join(target, "pack_data.json"))
          and not os.path.isdir(backup))
    T("原子替换：stage 缺失时回滚，目标包完好", ok,
      "raised=%s target_intact=%s" % (raised, os.path.isdir(target)))
    shutil.rmtree(base, ignore_errors=True)


def test_cancel_build(port):
    src = os.path.join(HERE, "packs", "初中英语词汇表_第2页")
    if not os.path.isdir(src):
        S("取消构建", "缺少基线包")
        return
    dst_name = "_probe_cancel_%s" % uuid.uuid4().hex[:6]
    dst = os.path.join(HERE, "packs", dst_name)
    shutil.copytree(src, dst)
    try:
        before = snapshot_dir(dst)
        # 故意用缓存里没有的怪词，让 TTS 真跑起来，才有时间取消
        pairs = [("cancelprobe%02d" % i, "取消测试%d" % i) for i in range(1, 31)]
        st, resp = upload(port, "取消30词.txt", make_txt(pairs))
        meta = jload(resp)
        CREATED_DOCS.append(meta.get("doc_id"))
        st, resp = post_json(port, "/api/parse", {"doc_id": meta["doc_id"]})
        pj = jload(resp).get("job_id")
        CREATED_JOBS.append(pj)
        poll_job(port, pj or "0" * 12)
        st, resp = get(port, "/api/preview?job=" + (pj or ""))
        rows = [{"page": g["page"], "en": p[0], "cn": p[1]}
                for g in (jload(resp).get("groups") or []) for p in g["pairs"]]
        st, resp = post_json(port, "/api/build", {"doc_id": meta["doc_id"], "rows": rows,
                                                  "mode": "merge", "pack_id": dst_name,
                                                  "name": dst_name})
        bj = jload(resp).get("job_id")
        CREATED_JOBS.append(bj)
        if not bj:
            T("取消：构建任务能创建", False, jload(resp).get("error"))
            return
        # 等到「子进程已经在合成」再取消：progress 是按页跳的（一页 30 词要几十秒
        # 才跳一次），所以不能拿 progress 当判据 —— 要看日志里出现缓存统计那一行。
        started = False
        j = {}
        t0 = time.time()
        while time.time() - t0 < 120:
            st, resp = get(port, "/api/job/" + bj)
            j = jload(resp)
            logs = j.get("log_tail") or []
            if j.get("state") == "running" and any("缓存命中" in l for l in logs):
                started = True
                break
            if j.get("state") in ("done", "error"):
                break
            time.sleep(0.4)
        T("取消：取消前子进程已在合成（日志已到 TTS 阶段）", started,
          "%s / %s" % (j.get("state"), (j.get("log_tail") or [])[-1:]))
        st, resp = post_json(port, "/api/cancel", {"job_id": bj})
        T("取消：接口返回 200", st == 200, st)
        job, sec = poll_job(port, bj, timeout=90)
        T("取消：状态变为 canceled", (job or {}).get("state") == "canceled",
          "%s（%.1fs）" % ((job or {}).get("state"), sec))

        # 进程真死了：log_tail 不再增长
        n1 = len((job or {}).get("log_tail") or [])
        time.sleep(3)
        st, resp = get(port, "/api/job/" + bj)
        n2 = len(jload(resp).get("log_tail") or [])
        T("取消：日志不再增长（子进程真的停了）", n1 == n2, "%d → %d" % (n1, n2))

        after = snapshot_dir(dst)
        T("取消：目标包与取消前逐字节一致（没有半成品）", before == after,
          sorted(set(after) ^ set(before))[:3])
        T("取消：暂存区没留下 stage 目录",
          not os.path.isdir(os.path.join(HERE, "work", "_stage", bj)))
        T("取消：没留下备份目录",
          not os.path.isdir(os.path.join(HERE, "work", "_backup", bj)))
    finally:
        if not keep_flag():
            shutil.rmtree(dst, ignore_errors=True)


def test_interrupted_on_restart():
    """服务重启后，残留的 running 必须变 interrupted —— 不假装能续跑。"""
    import webgen
    jid = "deadbeef0001"
    snap = {"id": jid, "kind": "build", "state": "running", "phase": "build",
            "progress": 42, "message": "合成音频", "log_tail": ["x"],
            "result": None, "error": None, "doc_id": "0" * 32,
            "created": time.time(), "updated": time.time()}
    webgen._save_snapshot(snap)
    webgen.init()
    try:
        with open(os.path.join(HERE, "work", "_jobs", jid + ".json"),
                  encoding="utf-8") as f:
            back = json.load(f)
        T("重启：残留 running → interrupted",
          back.get("state") == "interrupted", back.get("state"))
        T("重启：interrupted 的文案提示可重新开始",
          "重新开始" in (back.get("message") or ""), back.get("message"))
    finally:
        try:
            os.remove(os.path.join(HERE, "work", "_jobs", jid + ".json"))
        except OSError:
            pass


def make_docx_2pages(path):
    """跨页样本：3 条 + 分页符 + 3 条 → Word 报 2 页。"""
    import docx
    d = docx.Document()
    for i, (en, cn) in enumerate([("apple", "苹果"), ("banana", "香蕉"),
                                  ("cherry", "樱桃")], 1):
        d.add_paragraph("%d %s [test] n. %s" % (i, en, cn))
    d.add_page_break()
    for i, (en, cn) in enumerate([("grape", "葡萄"), ("lemon", "柠檬"),
                                  ("mango", "芒果")], 4):
        d.add_paragraph("%d %s [test] n. %s" % (i, en, cn))
    d.save(path)


def test_word_pages(port):
    p = os.path.join(SAMPLES, "pages.docx")
    make_docx_2pages(p)
    with open(p, "rb") as f:
        data = f.read()
    st, resp = upload(port, "跨页样本.docx", data)
    meta = jload(resp)
    CREATED_DOCS.append(meta.get("doc_id"))
    if st != 200:
        T("Word 分页：上传跨页 docx", False, jload(resp).get("error"))
        return
    st, resp = post_json(port, "/api/parse",
                         {"doc_id": meta["doc_id"], "with_pages": True})
    jid = jload(resp).get("job_id")
    CREATED_JOBS.append(jid)
    job, sec = poll_job(port, jid or "0" * 12, timeout=300)
    state = (job or {}).get("state")
    if state != "done":
        # 没装 Word / Word 被占用时，必须是**明确报错**，不能静默降级
        err = (job or {}).get("error") or ""
        T("Word 分页：失败时给出明确原因（而不是静默降级）",
          bool(err) and ("Word" in err), err[:120])
        S("Word 分页：真实分页结果", "本机 Word 不可用")
        return
    res = (job or {}).get("result") or {}
    T("Word 分页：勾选后 paged=true 且措辞为「页」",
      res.get("paged") is True and res.get("unit") == "页", res)
    T("Word 分页：跨页样本切成 2 页",
      res.get("groups") == 2 and res.get("words") == 6, res)
    st, resp = get(port, "/api/preview?job=" + jid)
    pv = jload(resp)
    sizes = [g.get("words") for g in (pv.get("groups") or [])]
    T("Word 分页：每页 3 条（期望值来自样本构造）", sizes == [3, 3], sizes)

    # 产出契约：每条都要同时带 page（词条**开始**的页）与 page_end（段落结尾的页）。
    # 只按 page 分组 —— 这一条专防"生产端被改回只写段尾页"：
    # 一旦回退，末行溢到下一页的词条会被归到下一页、从自己那页消失
    # （实例：232 grandpa「（口语）爷爷，外公」整条被记成第 5 页，第 4 页只剩 56 词）。
    cand = []
    for f in glob.glob(os.path.join(HERE, "work", "_pagescan", "*.pages.json")):
        try:
            with open(f, encoding="utf-8-sig") as fh:
                rows = json.load(fh)
        except Exception:
            continue
        if isinstance(rows, list) and rows and any(
                "apple" in (r.get("text") or "") for r in rows):
            cand.append((os.path.getmtime(f), rows))
    if cand:
        cand.sort(key=lambda t: t[0], reverse=True)
        rows = cand[0][1]
        ok = all(("page" in r and "page_end" in r) for r in rows) and \
            all(int(r["page"]) <= int(r["page_end"]) for r in rows)
        T("Word 分页：产物同时带 page 与 page_end，且 page ≤ page_end",
          ok, "%d 条 / 例 %s" % (len(rows), {k: rows[0].get(k)
                                            for k in ("entry", "page", "page_end")}))
    else:
        S("Word 分页：产物带 page_end", "找不到 pagescan 产物")
    T("Word 分页：日志里能看到 Word 版本",
      any("word version" in l.lower() for l in ((job or {}).get("log_tail") or [])),
      ((job or {}).get("log_tail") or [])[:2])

    # 第二次：命中缓存（同源文件 + 同脚本 + 同 Word 版本）
    st, resp = post_json(port, "/api/parse",
                         {"doc_id": meta["doc_id"], "with_pages": True})
    jid2 = jload(resp).get("job_id")
    CREATED_JOBS.append(jid2)
    job2, sec2 = poll_job(port, jid2 or "0" * 12, timeout=300)
    hit = any("命中缓存" in l for l in ((job2 or {}).get("log_tail") or []))
    T("Word 分页：同文件第二次命中缓存", hit,
      "%s / %.1fs（首次 %.1fs）" % ((job2 or {}).get("state"), sec2, sec))
    T("Word 分页：缓存结果与首次一致",
      ((job2 or {}).get("result") or {}).get("groups") == 2,
      ((job2 or {}).get("result") or {}).get("groups"))


def keep_flag():
    return "--keep" in sys.argv


def cleanup():
    for did in [d for d in CREATED_DOCS if d]:
        p = os.path.join(HERE, "work", "_docs", did + ".json")
        try:
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
            up = meta.get("path")
            if up and os.path.isfile(up):
                os.remove(up)
        except Exception:
            pass
        for path in (p, os.path.join(HERE, "work", "_parse", did)):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
    for jid in [j for j in CREATED_JOBS if j]:
        try:
            os.remove(os.path.join(HERE, "work", "_jobs", jid + ".json"))
        except Exception:
            pass
        shutil.rmtree(os.path.join(HERE, "work", "_build", jid), ignore_errors=True)
        shutil.rmtree(os.path.join(HERE, "work", "_stage", jid), ignore_errors=True)
    shutil.rmtree(SAMPLES, ignore_errors=True)


def report():
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    print("")
    for st, name, extra in RESULTS:
        line = "%-4s %s" % (st, name)
        if extra is not None:
            line += "   [%s]" % (extra,)
        print(line)
    print("")
    print("合计 %d 项：PASS %d / FAIL %d / SKIP %d"
          % (len(RESULTS),
             sum(1 for r in RESULTS if r[0] == "PASS"),
             len(fails),
             sum(1 for r in RESULTS if r[0] == "SKIP")))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
