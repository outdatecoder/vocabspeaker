# -*- coding: utf-8 -*-
"""webgen.py — 网页生成器的编排层（不 import 任何 HTTP 模块）。

分工（别混）：
    serve.py   只做 HTTP：路由、token 分级、JSON 序列化、Range 静态
    webgen.py  只做编排：上传落盘、解析任务、分组、构建、原子替换、取消

这样 `_api_probe.py` 可以直接 import 本模块做函数级断言（不必先起服务器），
而真正的抽取/合成只有一处实现 —— 子进程调用 `build_vocab.py`，
本模块**不复制**任何解析或拼接逻辑。

目录约定（都在 vocab-tool/ 下）：
    work/_uploads/<doc_id>.<ext>     原始上传文件
    work/_docs/<doc_id>.json         上传元数据（名字、sha256、路径）
    work/_parse/<doc_id>/            parse 子命令的工作目录（words.csv / report.txt）
    work/_jobs/<job_id>.json         任务快照（服务重启后可读回，不假装能续跑）
    work/_stage/<job_id>/            构建暂存包（D3）
    work/_backup/<job_id>/           原子替换前的旧包（D3）
    work/_trash/<id>__<时间戳>/       工作台上「删除词库」的去处（不 rmtree，留一次后悔的机会）
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
UPLOADS = os.path.join(WORK, "_uploads")
DOCS = os.path.join(WORK, "_docs")
PARSED = os.path.join(WORK, "_parse")
PAGESCAN = os.path.join(WORK, "_pagescan")
JOBS = os.path.join(WORK, "_jobs")
STAGE = os.path.join(WORK, "_stage")
BACKUP = os.path.join(WORK, "_backup")
TXN = os.path.join(WORK, "_txn")          # 目录交换的事务记录（崩溃后靠它自愈）
TRASH = os.path.join(WORK, "_trash")      # 删除的词库放这里：界面说"不可恢复"，但能手工捞回
PACKS = os.path.join(HERE, "packs")

BUILD_VOCAB = os.path.join(HERE, "build_vocab.py")
PY = sys.executable or "python"

MAX_UPLOAD = 20 * 1024 * 1024          # 20MB
UPLOAD_CHUNK = 8192
GROUP_SIZE = 50                        # 无真实页码时，每「组」的词条数
LOG_TAIL = 60                          # log_tail 只留最后 60 行

# 扩展名 → 魔数（.txt 走文本编码校验，没有魔数）
MAGIC = {".docx": b"PK\x03\x04", ".pdf": b"%PDF-", ".txt": None}
ALLOWED_EXT = tuple(MAGIC)


# ────────────────────────────── 错误 ──────────────────────────────
class WebError(Exception):
    """带 HTTP 状态码的业务错误。serve.py 只负责把它转成 JSON。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)


# ────────────────────────────── token ──────────────────────────────
# 只绑 127.0.0.1 挡不住「浏览器里另一个页面 POST 到本机」——
# 写接口必须带 token；只读接口免 token（静态播放器要能探测 /api/packs）。
# PET_API_TOKEN 只给门禁探针用（探针在另一个进程里，拿不到随机 token）；
# 正常启动不要设它，每次启动都会生成新 token。
_TOKEN = os.environ.get("PET_API_TOKEN") or secrets.token_urlsafe(24)


def token():
    return _TOKEN


def token_ok(given):
    if not given or not isinstance(given, str):
        return False
    return secrets.compare_digest(given, _TOKEN)


# ────────────────────────────── 任务表 ──────────────────────────────
_JOBS = {}
_JOBS_LOCK = threading.RLock()


def _ensure_dirs():
    for d in (UPLOADS, DOCS, PARSED, PAGESCAN, JOBS, STAGE, BACKUP, TXN, TRASH):
        os.makedirs(d, exist_ok=True)


# ────────────────────── 提交事务（目录交换的崩溃自愈） ──────────────────────
def _txn_path(jid):
    return os.path.join(TXN, jid + ".json")


def _txn_write(jid, rec):
    """原子写事务记录。写失败必须抛 —— 没记录的交换等于不可恢复的交换。"""
    _ensure_dirs()
    p = _txn_path(jid)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _txn_clear(jid):
    try:
        os.remove(_txn_path(jid))
    except OSError:
        pass


def recover_transactions():
    """启动时按事务记录补完/回滚目录交换。

    规则只看**磁盘现状**（记录里的 phase 只用于说明）：
      · target 在、backup 不在 → 上次已装好，只清记录
      · target 不在、backup 在 → 回滚旧包（宁可丢新包，也不让词库从正常位置消失）
      · target 在、backup 在   → 装好了但没清备份 → 删备份
      · target 不在、stage 在  → 保留现场、报 conflict（一个字节都不删）
    返回 [(jid, action)]，由 init() 打到 stderr，别静默。
    """
    out = []
    if not os.path.isdir(TXN):
        return out
    for fn in sorted(os.listdir(TXN)):
        if not fn.endswith(".json"):
            continue
        jid = fn[:-5]
        p = os.path.join(TXN, fn)
        try:
            with open(p, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception as e:
            out.append((jid, "conflict: 事务记录读不出来（%s），已保留现场" % e))
            continue
        target = rec.get("target") or ""
        backup = rec.get("backup") or ""
        stage = rec.get("stage") or ""
        try:
            has_t = bool(target) and os.path.isdir(target)
            has_b = bool(backup) and os.path.isdir(backup)
            has_s = bool(stage) and os.path.isdir(stage)
            if has_t and has_b:
                shutil.rmtree(backup, ignore_errors=True)
                _txn_clear(jid)
                out.append((jid, "已装好，清理遗留备份"))
            elif has_t and not has_b:
                _txn_clear(jid)
                out.append((jid, "已完成，记录清理"))
            elif (not has_t) and has_b:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.rename(backup, target)
                if has_s:
                    shutil.rmtree(stage, ignore_errors=True)
                _txn_clear(jid)
                out.append((jid, "旧包已回滚到正常位置（新包丢弃）"))
            elif (not has_t) and (not has_b) and has_s:
                out.append((jid, "conflict: 旧包已移走但新包未就位，现场保留在 %s"
                            % stage))
            else:
                _txn_clear(jid)
                out.append((jid, "空记录清理"))
        except Exception as e:
            out.append((jid, "conflict: 恢复失败（%s: %s），现场已保留"
                        % (type(e).__name__, e)))
    return out


def init():
    """服务启动时调用：建目录 + 自愈中断的目录交换 + 把残留 running 标成 interrupted。"""
    _ensure_dirs()
    for jid, action in recover_transactions():
        print("[webgen] 事务恢复 %s：%s" % (jid, action), file=sys.stderr)
    for jid in _list_snapshots():
        try:
            snap = _load_snapshot(jid)
        except Exception:
            continue
        if snap.get("state") in ("queued", "running", "cancel_requested"):
            snap["state"] = "interrupted"
            snap["message"] = "服务重启导致中断，请重新开始"
            _save_snapshot(snap)
    return token()


def _new_job(kind, **extra):
    _ensure_dirs()
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "kind": kind,
        "state": "queued", "phase": "", "progress": 0,
        "message": "", "log_tail": [], "result": None, "error": None,
        "created": time.time(), "updated": time.time(),
    }
    job.update(extra)
    with _JOBS_LOCK:
        _JOBS[jid] = job
    _save_snapshot(snapshot(job))
    return job


def _set(job, **kw):
    """progress 单调不减，且未 done 前封顶 99 —— 日志断流也不许显示 100%。"""
    with _JOBS_LOCK:
        if "progress" in kw:
            try:
                p = float(kw["progress"])
            except (TypeError, ValueError):
                p = job.get("progress", 0)
            p = max(float(job.get("progress") or 0), p)
            kw["progress"] = int(round(min(99 if job.get("state") != "done" else 100, p)))
        job.update(kw)
        job["updated"] = time.time()
        job["log_tail"] = list(job.get("log_tail") or [])


def _log(job, line):
    line = (line or "").rstrip()
    if not line:
        return
    with _JOBS_LOCK:
        tail = job.setdefault("log_tail", [])
        tail.append(line)
        if len(tail) > LOG_TAIL:
            del tail[:len(tail) - LOG_TAIL]
        job["updated"] = time.time()


def _scanned_hint(job):
    """从子进程日志里认出「这份 PDF 是扫描件」那句（build_vocab.SCANNED_PDF_HINT）。

    为什么要透传：用户看到"解析失败（退出码 1）"只能再问一遍；而这句话直接告诉他
    该怎么做。**只认一个固定片段**，其他失败原因一律不替他解释（R6：宁可少承诺）。
    """
    for line in reversed(list(job.get("log_tail") or [])):
        if "看起来是扫描件" in line:
            return line.strip()
    return ""


def _finish(job, state, message="", result=None, error=None, progress=None):
    with _JOBS_LOCK:
        job["state"] = state
        job["message"] = message or job.get("message", "")
        job["result"] = result
        job["error"] = error
        if progress is not None:
            job["progress"] = max(float(job.get("progress") or 0), min(100.0, float(progress)))
        if state == "done":
            job["progress"] = 100
        job["updated"] = time.time()
        job["log_tail"] = list(job.get("log_tail") or [])
    _save_snapshot(snapshot(job))


def snapshot(job):
    """对外快照：只含可 JSON 化的字段（不含 Popen、groups 等内部引用）。"""
    with _JOBS_LOCK:
        return {
            "id": job.get("id"),
            "kind": job.get("kind"),
            "state": job.get("state"),
            "phase": job.get("phase"),
            "progress": job.get("progress", 0),
            "message": job.get("message", ""),
            "log_tail": list(job.get("log_tail") or []),
            "result": job.get("result"),
            "error": job.get("error"),
            "dup_warnings": list(job.get("dup_warnings") or []),
            "doc_id": job.get("doc_id"),
            "created": job.get("created"),
            "updated": job.get("updated"),
        }


def _snapshot_path(jid):
    return os.path.join(JOBS, jid + ".json")


def _save_snapshot(snap):
    try:
        _ensure_dirs()
        tmp = _snapshot_path(snap["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _snapshot_path(snap["id"]))
    except Exception:
        pass          # 快照只是「可恢复线索」，写失败不该让任务失败


def _load_snapshot(jid):
    with open(_snapshot_path(jid), encoding="utf-8") as f:
        return json.load(f)


def _list_snapshots():
    if not os.path.isdir(JOBS):
        return []
    out = []
    for n in os.listdir(JOBS):
        if n.endswith(".json") and not n.endswith(".tmp"):
            out.append(n[:-5])
    return out


def get_job(jid):
    if not jid or not isinstance(jid, str):
        raise WebError(400, "缺少 job_id")
    if not re.fullmatch(r"[0-9a-f]{6,32}", jid):
        raise WebError(404, "任务不存在")
    with _JOBS_LOCK:
        job = _JOBS.get(jid)
    if job:
        return snapshot(job)
    try:
        return _load_snapshot(jid)
    except Exception:
        raise WebError(404, "任务不存在")


def _get_job_obj(jid):
    with _JOBS_LOCK:
        return _JOBS.get(jid)


# ────────────────────────────── 上传 ──────────────────────────────
def _sanitize_name(orig):
    name = os.path.basename((orig or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f]", "", name).strip()
    return name[:120]


def _check_txt(path):
    with open(path, "rb") as f:
        data = f.read()
    if b"\x00" in data:
        raise WebError(400, "这个 .txt 看着像二进制文件，不是文本词表")
    for enc in ("utf-8-sig", "gb18030"):
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    raise WebError(400, "文本无法按 UTF-8 / GB18030 解码（请另存为 UTF-8）")


def save_upload(stream, content_length, orig_name):
    """把请求体按 Content-Length 分块落盘。不用 multipart（Python 3.13 没有 cgi）。

    stream        : rfile（带 read 的类文件对象）
    content_length: 请求头里的 Content-Length（原样，未解析）
    orig_name     : 原始文件名（前端放在 X-Filename 里，URL 编码）
    """
    if content_length is None or str(content_length).strip() == "":
        raise WebError(411, "缺少 Content-Length —— 拒绝收一个长度未知的请求体")
    try:
        length = int(content_length)
    except (TypeError, ValueError):
        raise WebError(400, "Content-Length 不是整数")
    if length <= 0:
        raise WebError(400, "文件是空的")
    if length > MAX_UPLOAD:
        raise WebError(413, "文件超过 20MB（%.1f MB）" % (length / 1048576.0))

    name = _sanitize_name(orig_name)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise WebError(400, "只支持 %s（收到 %s）"
                       % (" / ".join(ALLOWED_EXT), ext or "无扩展名"))
    if not name:
        name = "upload" + ext

    _ensure_dirs()
    doc_id = uuid.uuid4().hex
    path = os.path.join(UPLOADS, doc_id + ext)
    h = hashlib.sha256()
    size = 0
    try:
        with open(path, "wb") as f:
            while size < length:
                try:
                    chunk = stream.read(min(UPLOAD_CHUNK, length - size))
                except OSError as e:            # 含 socket.timeout：客户端断了
                    raise WebError(400, "上传中断：%s" % e)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
                size += len(chunk)
        if size != length:
            raise WebError(400, "上传不完整：收到 %d / 声明 %d 字节" % (size, length))
        # 魔数 / 编码校验放在最后：先把字节收全，再判真伪
        if ext == ".txt":
            enc = _check_txt(path)
        else:
            enc = None
            magic = MAGIC[ext]
            with open(path, "rb") as f:
                head = f.read(len(magic))
            if head != magic:
                raise WebError(400, "文件内容不像 %s（魔数不符，可能只是改了扩展名）"
                               % ext.lstrip(".").upper())
        meta = {
            "doc_id": doc_id, "name": name, "ext": ext, "size": size,
            "sha256": h.hexdigest(), "path": path, "encoding": enc,
            "created": time.time(),
        }
        with open(os.path.join(DOCS, doc_id + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return {k: meta[k] for k in ("doc_id", "name", "ext", "size", "sha256")}


def load_doc(doc_id):
    if not doc_id or not isinstance(doc_id, str) or not re.fullmatch(r"[0-9a-f]{32}", doc_id):
        raise WebError(400, "doc_id 不合法")
    p = os.path.join(DOCS, doc_id + ".json")
    if not os.path.isfile(p):
        raise WebError(404, "找不到这次上传（doc_id=%s），请重新选择文件" % doc_id)
    with open(p, encoding="utf-8") as f:
        meta = json.load(f)
    if not os.path.isfile(meta.get("path", "")):
        raise WebError(404, "上传的文件已被清理，请重新选择文件")
    return meta


# ────────────────────────────── 子进程 ──────────────────────────────
def _popen(cmd):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW：别弹黑框
    return subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, **kw)


def _kill_tree(proc):
    """Windows 下 terminate() 只杀直接子进程，ffmpeg/ffprobe 会残留 → 必须杀树。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _exec(job, cmd, on_line=None, progress_cb=None):
    """跑子进程并把输出逐行写进 log_tail。返回 (returncode, canceled)。"""
    _log(job, "→ " + " ".join(cmd))
    proc = _popen(cmd)
    with _JOBS_LOCK:
        job["proc"] = proc
    try:
        for line in proc.stdout:
            _log(job, line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass
            if job.get("cancel"):
                _kill_tree(proc)
                break
            if progress_cb:
                try:
                    progress_cb(line)
                except Exception:
                    pass
        rc = proc.wait()
    finally:
        with _JOBS_LOCK:
            job["proc"] = None
    canceled = bool(job.get("cancel"))
    if canceled and rc == 0:
        rc = -1                                   # 被取消的子进程绝不算成功
    return rc, canceled


def _build_progress(line):
    """把 `build_vocab.py build` 的输出行翻译成**百分比**；无进度含义的行返回 None。

    两个阶段各占一段区间 —— 合成 5→50，逐页拼接 50→85（85 之后由调用方收尾）：
      · 合成行：`    … 已合成 20/22`（并发阶段，几十秒到几分钟）
      · 拼接行：`  [ 1/2] 第 1 页  6 词 …`
    为什么必须分两段：并发合成期间**没有任何「第 n 页」输出**，只按页算的话
    进度条会在 5% 停好几分钟，用户看到的就是「点了没反应」。
    """
    m = re.search(r"已合成 (\d+)/(\d+)", line)
    if m:
        return 5 + 45.0 * int(m.group(1)) / max(1, int(m.group(2)))
    m = re.search(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]", line)
    if m:
        return 50 + 35.0 * int(m.group(1)) / max(1, int(m.group(2)))
    return None


# ────────────────────────────── 解析 ──────────────────────────────
def _read_words_csv(path):
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            en = (r.get("en") or "").strip()
            cn = (r.get("cn") or "").strip()
            if not en or not cn:
                continue
            try:
                pg = int(r.get("page") or 1)
            except ValueError:
                pg = 1
            try:
                sc = float(r.get("score") or 0)
            except ValueError:
                sc = 0.0
            rows.append({"page": pg, "en": en, "cn": cn, "score": sc})
    return rows


def build_groups(rows, ext, with_pages, size=GROUP_SIZE):
    """把 words.csv 的扁平行变成「组」。

    - PDF：按物理页（解析器给的就是真实页号）
    - docx + with_pages：按 Word 真实页码（_word_pages.ps1 给的）
    - 其余（docx/txt 无真实页码）：按固定条数切，页号 1..N，界面称「组」
    返回 (paged, groups)，groups 里 page 一律是**源文件**页/组号，
    写进包的页号由 D3 的 offset 决定 —— 两者不要混。
    """
    if not rows:
        return False, []
    paged = ext == ".pdf" or (ext == ".docx" and bool(with_pages))
    chunks = []
    if paged:
        buckets = {}
        order = []
        for r in rows:
            if r["page"] not in buckets:
                buckets[r["page"]] = []
                order.append(r["page"])
            buckets[r["page"]].append(r)
        chunks = [(pg, buckets[pg]) for pg in order]
    else:
        for i in range(0, len(rows), max(1, size)):
            chunks.append((i // max(1, size) + 1, rows[i:i + size]))
    groups = []
    for pg, items in chunks:
        groups.append({
            "page": pg,
            "words": len(items),
            "first": items[0]["en"],
            "last": items[-1]["en"],
            "pairs": [[r["en"], r["cn"]] for r in items],
            "scores": [r["score"] for r in items],
        })
    return paged, groups


def start_parse_job(doc_id, with_pages=False):
    meta = load_doc(doc_id)
    job = _new_job("parse", doc_id=doc_id, phase="parse")
    t = threading.Thread(target=_run_parse, args=(job, meta, bool(with_pages)),
                         daemon=True)
    t.start()
    return job["id"]


def _run_parse(job, meta, with_pages):
    out_dir = os.path.join(PARSED, meta["doc_id"])
    try:
        _set(job, state="running", phase="parse", progress=5,
             message="正在解析词条…")
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)
        # --keep-low：网页流程把「低分页」也交给用户看（校对表里可整组删掉），
        # 不做静默丢弃 —— 命令行那套 min-page-score 闸门只适合批量跑。
        cmd = [PY, BUILD_VOCAB, "parse", "--in", meta["path"],
               "--out", out_dir, "--keep-low"]
        wp_pages = 0
        if with_pages:
            wp, _wp_entries, wp_pages = pagescan(meta, job)
            if job.get("cancel"):
                _finish(job, "canceled", "已取消", progress=0)
                return
            if wp:
                cmd += ["--word-pages", wp]
        _set(job, phase="parse", message="正在解析词条…")
        rc, canceled = _exec(job, cmd,
                             progress_cb=lambda line: _set(job, progress=50))
        if canceled:
            _finish(job, "canceled", "已取消", progress=0)
            return
        csv_path = os.path.join(out_dir, "words.csv")
        if rc != 0 or not os.path.isfile(csv_path):
            hint = _scanned_hint(job)
            _finish(job, "error",
                    error=hint or ("解析失败（退出码 %s）—— 详见上方日志" % rc),
                    message="这份 PDF 是扫描件" if hint else "解析失败")
            return
        rows = _read_words_csv(csv_path)
        if not rows:
            _finish(job, "error",
                    error="没抽到一个词条 —— 这个文件可能不是「英中配对词表」，"
                          "或英文/中文之间缺少分隔（Tab、3 个以上空格、| 或 ,）",
                    message="没有抽到词条")
            return
        paged, groups = build_groups(rows, meta["ext"], with_pages)
        # 逐页诊断（空页 / 低分页 / 只有 1 条的页）：words.csv 表达不了这些，
        # 不读它，用户就永远不知道「有一页根本没进来」（P1-4）。
        diag = {}
        rp = os.path.join(out_dir, "pages_report.json")
        if os.path.isfile(rp):
            try:
                with open(rp, encoding="utf-8") as f:
                    diag = json.load(f)
            except Exception as e:
                _log(job, "⚠️ pages_report.json 读不出来：%s" % e)
        job["diagnostics"] = diag
        empty_n = len(diag.get("empty_pages") or [])
        if empty_n:
            _log(job, "⚠️ %d 页一条词条都没抽到：%s"
                 % (empty_n, "、".join("第%d页" % p.get("page")
                                       for p in (diag.get("empty_pages") or [])[:10])))
        if with_pages and wp_pages and len(groups) != wp_pages:
            # Word 报的页数与解析出的段数对不上 —— 不能假装分页成功
            _log(job, "⚠️ Word 报 %d 页，解析出 %d 段：数量不符，改按固定条数分组"
                 % (wp_pages, len(groups)))
            paged, groups = build_groups(rows, meta["ext"], False)
        total = sum(g["words"] for g in groups)
        job["groups"] = groups
        job["paged"] = paged
        _finish(job, "done",
                message="解析完成：%d %s / %d 词" % (
                    len(groups), "页" if paged else "组", total),
                progress=100,
                result={"doc_id": meta["doc_id"], "name": meta["name"],
                        "paged": paged, "groups": len(groups), "words": total,
                        "unit": "页" if paged else "组",
                        "empty_pages": empty_n})
    except WebError as e:
        _finish(job, "error", error=e.message, message="解析失败")
    except Exception as e:                      # 编排层自己的异常也要如实转成 error
        _finish(job, "error", error="解析异常：%s: %s" % (type(e).__name__, e),
                message="解析异常")


def preview(job_id):
    job = _get_job_obj(job_id)
    if not job:
        snap = get_job(job_id)
        raise WebError(409, "任务详情已不在内存里（服务重启过），请重新解析")
    if job.get("kind") != "parse":
        raise WebError(400, "这个任务不是解析任务")
    if job.get("state") != "done":
        raise WebError(409, "任务尚未完成（当前 %s）" % job.get("state"))
    groups = job.get("groups") or []
    return {
        "job_id": job_id,
        "paged": bool(job.get("paged")),
        "unit": "页" if job.get("paged") else "组",
        "total": sum(g["words"] for g in groups),
        "groups": groups,
        # 空页 / 低分页 / 只有 1 条的页 —— 前端必须显示，否则用户以为"全都进来了"
        "diagnostics": job.get("diagnostics") or {},
    }


def pagescan(meta, job=None):
    """跑 `_word_pages.ps1` 拿 Word 真实页码，返回 (json 路径, 页数)。

    缓存键 = sha256(源文件) + sha256(脚本) + Word 版本：
    换了一份脚本（逻辑变了）或换了 Office 版本，缓存就不能复用 ——
    docx 的 XML 里根本没有分页信息，这个 JSON 是唯一真值来源。
    """
    src = meta["path"]
    script = os.path.join(HERE, "_word_pages.ps1")
    if not os.path.isfile(script):
        raise WebError(400, "找不到 _word_pages.ps1，无法按 Word 真实页码分页")
    if os.name != "nt":
        raise WebError(400, "Word 分页只能在本机 Windows + 已安装 Word 的环境里跑")

    h_src = _sha256(src)
    h_scr = _sha256(script)
    wver = _word_version()
    os.makedirs(PAGESCAN, exist_ok=True)
    cache_path = os.path.join(PAGESCAN, "%s_%s.json" % (h_src[:20], h_scr[:8]))
    try:
        with open(cache_path, encoding="utf-8") as f:
            c = json.load(f)
        if (c.get("src_sha") == h_src and c.get("script_sha") == h_scr
                and c.get("word_version") == wver
                and os.path.isfile(c.get("json_path") or "")):
            _log(job, "Word 分页命中缓存：%d 条 / %d 页（Word %s）"
                 % (c.get("count") or 0, c.get("pages") or 0,
                    c.get("word_build") or wver))
            return c["json_path"], int(c.get("count") or 0), int(c.get("pages") or 0)
    except Exception:
        pass

    out_json = os.path.join(PAGESCAN, "%s.pages.json" % h_src[:20])
    _set(job, phase="pagescan", progress=5,
         message="调用 Word 扫描真实页码（39 页约 20 秒，页数越多越久；可取消）…")
    seen = {"ver": wver}

    def prog(line):
        # 快路径按「页」报进度（... pages 10 / 39），慢路径按「段」报（... para 250 / 2191）。
        m = (re.search(r"\.\.\.\s*pages\s+(\d+)\s*/\s*(\d+)", line)
             or re.search(r"\.\.\.\s*para\s+(\d+)\s*/\s*(\d+)", line))
        if m:
            _set(job, progress=5 + 40.0 * int(m.group(1)) / max(1, int(m.group(2))),
                 message="Word 分页：%s / %s" % (m.group(1), m.group(2)))
        mv = re.search(r"word version = (\S+)", line)
        if mv:
            seen["ver"] = mv.group(1)

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", script, src, out_json]
    rc, canceled = _exec(job, cmd, progress_cb=prog)
    if canceled:
        return None, 0, 0
    if rc != 0:
        raise WebError(400, "Word 分页扫描失败（退出码 %s）—— 本机可能没装 Word，"
                            "或 Word 被占用；详见日志。取消勾选后可继续按固定条数分组。"
                       % rc)
    if not os.path.isfile(out_json):
        raise WebError(400, "Word 分页没有产出结果 —— 本机可能没装 Word")
    try:
        with open(out_json, encoding="utf-8-sig") as f:
            rows = json.load(f)
    except Exception as e:
        raise WebError(400, "Word 分页结果无法解析：%s" % e)
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        raise WebError(400, "Word 分页结果是空的 —— 文档里没找到编号词条")
    try:
        pages_n = len({int(r["page"]) for r in rows})
    except Exception:
        raise WebError(400, "Word 分页结果格式不对（缺 page 字段）")
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            # 注意：判缓存用的是注册表里的 wver（Word 每次启动前就能读到），
            # seen["ver"] 只是 Word 自己报的 16.0，两者不能混着比，否则永远不命中。
            json.dump({"src_sha": h_src, "script_sha": h_scr,
                       "word_version": wver, "word_build": seen["ver"],
                       "count": len(rows), "pages": pages_n,
                       "json_path": out_json},
                      f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    _log(job, "Word 分页完成：%d 条 / %d 页（Word %s）" % (len(rows), pages_n, seen["ver"]))
    return out_json, len(rows), pages_n


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _word_version():
    """不启动 Word，只从注册表读版本号；读不到就记 unknown（缓存仍可用）。"""
    if os.name != "nt":
        return "unknown"
    try:
        import winreg
        for hive, key, name in (
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Microsoft\Office\ClickToRun\Configuration",
                 "VersionToReport"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\WOW6432Node\Microsoft\Office\ClickToRun\Configuration",
                 "VersionToReport")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    v, _ = winreg.QueryValueEx(k, name)
                    if v:
                        return str(v)
            except OSError:
                continue
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Office") as k:
            best = ""
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if re.fullmatch(r"\d+\.\d+", sub) and sub > best:
                    best = sub
            return best or "unknown"
    except Exception:
        return "unknown"


# ────────────────────────────── 词库 ──────────────────────────────
def list_packs():
    """只读：packs/*/pack_data.json → 工作台要的全部字段。免 token。

    返回 [{id, name, pages, words, sec, bytes, mtime, max_page, url}]，按 (-mtime, id) 排序。
    · 跳过 `_` 开头的目录：门禁/调试产物不该出现在工作台上
    · mtime 取 pack_data.json 的修改时间，不是目录时间（目录时间会被无关文件刷新）
    · bytes 是 audio/*.mp3 的字节和（"拷走有多大"），单个坏文件跳过，不拖垮整个列表
    · **max_page**：现有最大页号。并入时新页从 `max_page + 1` 起排（见 _build_body），
      而「页数」是 len(pages)。两者只在页号连续且从 1 开始时相等 ——
      单页包 `初中英语词汇表_第2页` 就是 pages=1 / max_page=2。
      前端算"新页排到第几页"必须用 max_page，用页数会差一页。
    """
    out = []
    if not os.path.isdir(PACKS):
        return out
    for name in os.listdir(PACKS):
        if name.startswith("_"):
            continue
        d = os.path.join(PACKS, name)
        pj = os.path.join(d, "pack_data.json")
        if not os.path.isdir(d) or not os.path.isfile(pj):
            continue
        try:
            with open(pj, encoding="utf-8") as f:
                data = json.load(f)
            pages = data.get("pages") or []
            words = sum(int(p.get("words") or len(p.get("pairs") or []))
                        for p in pages)
            sec = round(sum(float(p.get("sec") or 0) for p in pages), 1)
            mtime = os.path.getmtime(pj)
        except Exception:
            continue
        size = 0
        ad = os.path.join(d, "audio")
        if os.path.isdir(ad):
            for fn in os.listdir(ad):
                if not fn.lower().endswith(".mp3"):
                    continue
                try:
                    size += os.path.getsize(os.path.join(ad, fn))
                except OSError:
                    continue
        out.append({"id": name, "name": name, "pages": len(pages),
                    "words": words, "sec": sec, "bytes": size,
                    "mtime": mtime,
                    "max_page": max([int(p.get("page") or 0) for p in pages] or [0]),
                    "url": "/vocab-tool/packs/%s/index.html" % name})
    out.sort(key=lambda x: (-x["mtime"], x["id"]))
    return out


_BAD_PACK_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def resolve_pack_id(pack_id):
    """客户端只送「词库 id」，服务端自己映射到 packs/<id>。

    拒绝：空值 / 带路径分隔符 / 带 Windows 非法字符 / . 与 .. / 绝对路径，
    最后再 realpath 一次，确认没被符号链接带到 packs 外面。
    """
    if not isinstance(pack_id, str) or not pack_id.strip():
        raise WebError(400, "缺少词库 id")
    pid = pack_id.strip()
    if len(pid) > 80:
        raise WebError(400, "词库 id 过长（≤80 字）")
    if pid in (".", "..") or _BAD_PACK_CHARS.search(pid):
        raise WebError(400, "词库 id 不能包含路径分隔符或 %s 等字符" % '\\/:*?"<>|')
    root = os.path.realpath(PACKS)
    target = os.path.realpath(os.path.join(root, pid))
    if os.path.commonpath([root, target]) != root or target == root:
        raise WebError(400, "词库路径越界，已拒绝")
    return target


def pack_info(pack_id):
    """已存在词库的摘要；不存在返回 None。"""
    target = resolve_pack_id(pack_id)
    pj = os.path.join(target, "pack_data.json")
    if not os.path.isfile(pj):
        return None
    with open(pj, encoding="utf-8") as f:
        data = json.load(f)
    pages = data.get("pages") or []
    return {"id": pack_id, "pages": len(pages),
            "words": sum(int(p.get("words") or len(p.get("pairs") or []))
                         for p in pages),
            "max_page": max([int(p.get("page") or 0) for p in pages] or [0])}


# ──────────────────────── 词库管理（改名 / 删除） ────────────────────────
def _validate_new_name(name):
    """重命名目标名：规则同 resolve_pack_id，额外禁 `_` 前缀与结尾的点。

    为什么额外禁 `_`：list_packs() 把 `_` 开头的目录当成门禁/调试产物跳过 ——
    允许这种名字，用户改完名会看到"词库凭空消失了"。
    结尾的点在 Windows 上建不出目录（会被系统吃掉），提前拒绝比 os.rename 抛 OSError 清楚。
    """
    if not isinstance(name, str) or not name.strip():
        raise WebError(400, "词库名不能为空")
    nm = name.strip()
    if len(nm) > 80:
        raise WebError(400, "词库名过长（≤80 字），现在 %d 字" % len(nm))
    if nm in (".", "..") or _BAD_PACK_CHARS.search(nm):
        raise WebError(400, '词库名不能包含 \\ / : * ? " < > | 等字符')
    if nm.startswith("_"):
        raise WebError(400, '词库名不能以下划线 "_" 开头（会被当成调试产物隐藏）')
    if nm.endswith("."):
        raise WebError(400, "词库名不能以「.」结尾（Windows 上建不出这种目录）")
    return nm


def _acquire_target_locks(paths):
    """按 realpath 排序依次非阻塞拿锁 —— 同序避免两个操作互锁死。

    锁必须与 `_run_build` 用同一族（`_target_lock`）：构建整段持锁，
    改名/删除撞上时就 409 让用户等一下，而不是阻塞一个 HTTP 请求好几分钟。
    """
    keys = sorted({os.path.realpath(p) for p in paths})
    got = []
    try:
        for k in keys:
            lk = _target_lock(k)
            if not lk.acquire(blocking=False):
                raise WebError(409, "这个词库正在生成，等它完成后再试")
            got.append(lk)
    except Exception:
        for lk in reversed(got):
            lk.release()
        raise
    return got


def _pack_summary(target, pid):
    """改名/删除后用：从磁盘现读一份摘要，不复用改名前的内存值。"""
    with open(os.path.join(target, "pack_data.json"), encoding="utf-8") as f:
        data = json.load(f)
    pages = data.get("pages") or []
    return {"id": pid, "name": pid, "pages": len(pages),
            "words": sum(int(p.get("words") or len(p.get("pairs") or []))
                         for p in pages),
            "url": "/vocab-tool/packs/%s/index.html" % pid}


def rename_pack(old_id, new_name):
    """改目录名 + 重渲染 index.html，返回新词库摘要。

    ⚠️ 必须重渲染：`pack_data.json` 只有 audio + pages，**不存名字**；
    播放器标题是渲染时从 `--name` 写进 index.html 的。
    少了这一步就是"文件夹叫新名、页面标题还是旧名"，而且旧名会一直留在包里。
    """
    src = resolve_pack_id(old_id)
    if not os.path.isfile(os.path.join(src, "pack_data.json")):
        raise WebError(404, "没有这个词库：%s" % old_id)
    nm = _validate_new_name(new_name)
    dst = resolve_pack_id(nm)
    if os.path.realpath(src) == os.path.realpath(dst):
        return _pack_summary(dst, nm)            # 幂等：没改名就别动文件
    if os.path.exists(dst):
        raise WebError(409, "已存在同名词库：%s" % nm)

    locks = _acquire_target_locks([src, dst])
    try:
        os.rename(src, dst)
        out = b""
        try:
            r = subprocess.run([PY, BUILD_VOCAB, "render", "--pack", dst, "--name", nm],
                               cwd=HERE, capture_output=True, timeout=180)
            ok = (r.returncode == 0)
            out = (r.stderr or b"") + (r.stdout or b"")
        except Exception as e:
            ok = False
            out = ("%s: %s" % (type(e).__name__, e)).encode("utf-8", "replace")
        if not ok:
            # 名字已经改了、页面还没跟上 —— 这种半成品比"改名失败"更难排查，直接回滚
            try:
                os.rename(dst, src)
            except Exception as e2:
                raise WebError(500, "重渲染失败，且目录名回滚也失败（%s）。"
                                    "词库现在叫「%s」，请手工把目录改回「%s」"
                                    % (e2, nm, old_id))
            tail = out.decode("utf-8", "replace").strip()[-400:]
            raise WebError(500, "改名后重渲染失败，已把目录名改回：%s" % (tail or "无输出"))
        return _pack_summary(dst, nm)
    finally:
        for lk in reversed(locks):
            lk.release()


def delete_pack(pack_id):
    """移入 `work/_trash/<id>__<时间戳>`，返回 {id, trashed, trash}。

    刻意**不用** shutil.rmtree：一个词库是几百个词加音频，误点一次就真没了。
    界面上照旧警告"无法恢复"（用户确实不该指望它能恢复），
    但实现上留一条手工捞回的路 —— 见 AGENTS.md「刻意的设计决定」。
    """
    src = resolve_pack_id(pack_id)
    if not os.path.isfile(os.path.join(src, "pack_data.json")):
        raise WebError(404, "没有这个词库：%s" % pack_id)
    pid = os.path.basename(src)
    locks = _acquire_target_locks([src])
    try:
        _ensure_dirs()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(TRASH, "%s__%s" % (pid, stamp))
        n = 2
        while os.path.exists(dst):
            dst = os.path.join(TRASH, "%s__%s-%d" % (pid, stamp, n))
            n += 1
        os.rename(src, dst)
    finally:
        for lk in reversed(locks):
            lk.release()
    try:
        print("[webgen] 已移入回收站：%s → %s" % (pid, dst), file=sys.stderr)
    except Exception:
        pass
    return {"id": pid, "trashed": True,
            "trash": os.path.relpath(dst, HERE).replace("\\", "/")}


# ────────────────────────────── 构建 ──────────────────────────────
# 为什么必须原子替换：`build_vocab.py build` 直接往目标包写 —— TTS 断网或
# ffmpeg 中途失败，目标包就剩半套（页数据已写、音频没齐），播放器会静默坏掉。
# 所以：先在 work/_stage/<job> 完整产出一个包 → 校验 → 再整体换过去。
_BUILD_GLOBAL = threading.Lock()          # 初版兜底：同一时刻只跑一个构建
_TARGET_LOCKS = {}
_TARGET_LOCKS_LOCK = threading.Lock()


def _target_lock(target):
    key = os.path.realpath(target)
    with _TARGET_LOCKS_LOCK:
        lk = _TARGET_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _TARGET_LOCKS[key] = lk
    return lk


def _clean_rows(rows):
    if not isinstance(rows, list) or not rows:
        raise WebError(400, "没有可生成的词条")
    if len(rows) > 20000:
        raise WebError(400, "一次最多 20000 条（收到 %d）" % len(rows))
    out = []
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            raise WebError(400, "第 %d 条格式不对" % i)
        en = r.get("en")
        cn = r.get("cn")
        en = en.strip() if isinstance(en, str) else ""
        cn = cn.strip() if isinstance(cn, str) else ""
        if not en or not cn:
            raise WebError(400, "第 %d 条缺英文或中文" % i)
        try:
            pg = int(r.get("page"))
        except (TypeError, ValueError):
            raise WebError(400, "第 %d 条的组/页号不合法" % i)
        if pg < 1:
            raise WebError(400, "第 %d 条的组/页号必须 ≥ 1" % i)
        if len(en) > 120 or len(cn) > 240:
            raise WebError(400, "第 %d 条文本过长（英文 ≤120 / 中文 ≤240）" % i)
        out.append({"page": pg, "en": en, "cn": cn})
    return out


def _pack_pairs_by_page(pack_dir):
    """目标包 {页号: [(英文小写, 释义), ...]}。

    **保持包里原有顺序** —— 判「最后一页尾部」必须靠顺序，不能靠集合。
    读一次文件，`_existing_pairs` 与 `_dup_report` 都从它派生，避免两次读到
    不一致的状态（两次读之间包可能正被替换）。
    """
    out = {}
    try:
        with open(os.path.join(pack_dir, "pack_data.json"), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return out
    for p in (data.get("pages") or []):
        try:
            pg = int(p.get("page") or 0)
        except (TypeError, ValueError):
            pg = 0
        bucket = out.setdefault(pg, [])
        for pair in (p.get("pairs") or []):
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            bucket.append((str(pair[0]).strip().casefold(), str(pair[1]).strip()))
    return {k: v for k, v in out.items() if v}


def _existing_pairs(pack_dir):
    """目标包里已有的 (英文, 释义) → 它出现在哪些页。"""
    out = {}
    for pg, pairs in _pack_pairs_by_page(pack_dir).items():
        for k in pairs:
            out.setdefault(k, set()).add(pg)
    return out


DUP_BLOCK_RATIO = 0.3    # 本批里「包里已有」的比例 ≥ 这个值 → 判为把同一批词又并了一次


def _dup_key(r):
    return (r["en"].strip().casefold(), r["cn"].strip())


def _dup_report(rows, pack_dir):
    """按**重复的形态**分级，而不是「有没有重复」。

    (英文, 释义) 这个键分不开两件完全不同的事：

    ① **分页错位**：文件没勾「按 Word 真实页码分页」→ 每 50 词一组，第 2 组从第
       51 个词开始，而包的第 1 页到第 57 个词 → in / English / in English / a /
       clock / and / pencil-box 这 7 条**同时**出现在「目标包最后一页」和「本批
       首页」。特征是**成批 + 卡在页边界**，必须拦。
    ② **教材本身**：同一本书在不同单元收了同一个词，释义一字不差 —— 实测 look
       「瞧，看」在书第 2 页与第 8 页、work「工作」在第 4 页与第 8 页（一个动词、
       一个名词，学生朗读时读音一样）。特征是**零星 + 落在包中间的页**；拦住就
       没法分批并入，而一本书里 look / work / have / like 这类高频词必然跨页重复。

    返回 (hard, soft, detail)：
      hard   —— 非空 = 必须 409，内容是给用户看的完整句子
      soft   —— 可以放行、但必须让用户看见的提示（写进日志与完成页）
      detail —— 结构化清单（日志/门禁用），不进快照
    """
    pages = _pack_pairs_by_page(pack_dir)
    if not pages or not rows:      # 空批次：下面的 min() 会抛 ValueError
        return None, [], []

    # ① 同一页里出现两条一模一样的词条：一页几十个词不会自己重复，
    #    这只能是「同一页勾了两次」或表格里重复粘贴了一行。
    first_page, same_page = {}, []
    for r in rows:
        k = _dup_key(r)
        if k in first_page and first_page[k] == r["page"]:
            same_page.append(r)
        else:
            first_page.setdefault(k, r["page"])
    if same_page:
        eg = "、".join("%s（第 %s 页）" % (r["en"], r["page"]) for r in same_page[:6])
        return ("本批词条里有 %d 条在**同一页**重复出现：%s%s。"
                "同一页不会自己收两次同一个词 —— 请检查是不是把这一页勾了两次，"
                "或在表格里重复粘贴了一行。"
                % (len(same_page), eg, "…" if len(same_page) > 6 else ""), [], [])

    have = {}
    for pg, pairs in pages.items():
        for k in pairs:
            have.setdefault(k, set()).add(pg)

    dup = [(r, _dup_key(r)) for r in rows if _dup_key(r) in have]
    if not dup:
        return None, [], []

    def _name(items, limit=6):
        parts = []
        for r, _k in items[:limit]:
            pgs = "、".join(str(x) for x in sorted(have[_dup_key(r)]))
            parts.append("%s（%s，第 %s 页已有）" % (r["en"], r["cn"], pgs))
        return "、".join(parts) + ("…" if len(items) > limit else "")

    # ② 整批重复：本批已有这么高比例的词在包里 → 多半是把同一批词又并了一遍
    ratio = len(dup) / float(len(rows))
    if ratio >= DUP_BLOCK_RATIO:
        return ("本批 %d 条词里有 %d 条（%.0f%%）与包里已有词重复：%s。"
                "这更像是把已经进包的那几页又并了一次，而不是新页。"
                "请只勾书里还没进包的那几页；确实想让两页都保留同一批词，"
                "请改用「新建词库」。"
                % (len(rows), len(dup), ratio * 100, _name(dup)), [], [])

    # ③ 边界滑移：所有重复词同时是「目标包最后一页的词」和「本批首页的词」。
    #    只有分页边界对不上才会长这样（真实页与 50 词一组错位）。
    last_pg = max(pages)
    tail = set(pages[last_pg])
    first_src = min(r["page"] for r in rows)
    head = set(_dup_key(r) for r in rows if r["page"] == first_src)
    edge = [(r, k) for r, k in dup if k in tail and k in head]
    if len(edge) == len(dup) and len(edge) >= 2:
        return ("本批 %d 条重复词全部卡在页边界上：既在第 %d 页（包里的最后一页），"
                "又在本批第 %s 页的开头 —— 这是分页方式对不上的特征（例如文件其实"
                "是「每 50 词一组」，而目标词库是按书的真实页建的）：%s。"
                "请勾选「按 Word 真实页码分页」重新扫描，只把书里还没进包的那几页加进来；"
                "确实想让两页都保留同一批词，请改用「新建词库」。"
                % (len(edge), last_pg, first_src, _name(dup)), [], [])

    # ④ 其余：书里本来就在不同单元收了同一个词 → 放行，但必须让用户看见
    detail = [{"en": r["en"], "cn": r["cn"], "src_page": r["page"],
               "pack_pages": sorted(have[_dup_key(r)])} for r, _k in dup]
    soft = ["提示：本批有 %d 条词在包里已有（同一本书不同单元收录，按原文保留）：%s"
            % (len(dup), _name(dup, limit=10))]
    return None, soft, detail


def start_build_job(doc_id, rows, mode, pack_id, name):
    rows = _clean_rows(rows)
    mode = (mode or "").strip().lower()
    if mode not in ("merge", "new"):
        raise WebError(400, "mode 只能是 merge（并入）或 new（新建）")
    meta = load_doc(doc_id)                   # 记录 source_file_sha256 用
    # 只有 merge 会判重；new 也必须先定义，否则下面 _new_job 引用 soft 直接 UnboundLocalError
    hard, soft, dup_detail = None, [], []

    if mode == "merge":
        if not pack_id:
            raise WebError(400, "并入模式必须指定目标词库 id")
        info = pack_info(pack_id)
        if not info:
            raise WebError(404, "词库 %s 不存在（没有 pack_data.json）" % pack_id)
        target = resolve_pack_id(pack_id)
        # 并入前分级：错位/整批重复 → 409；书里本来就重复 → 放行但提示。
        # （这里是 HTTP 层的「尽早判死」，锁内 _build_body 还会再复核一次）
        hard, soft, dup_detail = _dup_report(rows, target)
        if hard:
            raise WebError(409, hard)
        pack_name = pack_id.strip()
    else:
        nm = (name or "").strip()
        if not nm:
            raise WebError(400, "新建模式必须填词库名称")
        target = resolve_pack_id(nm)
        if os.path.isfile(os.path.join(target, "pack_data.json")):
            raise WebError(409, "已存在同名词库：%s（可改用「并入现有词库」）" % nm)
        if os.path.exists(target):
            raise WebError(409, "目标目录已存在：packs/%s" % nm)
        info = {"pages": 0, "words": 0, "max_page": 0}
        pack_name = nm.strip()

    job = _new_job("build", doc_id=doc_id, phase="build",
                   pack_id=pack_name, mode=mode,
                   dup_warnings=(soft or []), dup_detail=(dup_detail or []))
    # ⚠️ 这里的 info 只用于「立刻判死」的早期校验；真正决定页号的是 _run_build 里
    #    拿到锁之后重读的那一份 —— 排队期间别的任务可能已经改了词库。
    t = threading.Thread(target=_run_build,
                         args=(job, meta, rows, mode, target, pack_name),
                         daemon=True)
    t.start()
    return job["id"]


def _run_build(job, meta, rows, mode, target, pack_name):
    """拿齐两把锁之后**重新读取**目标状态，再决定 offset —— 这是覆盖 bug 的根治点。

    排队期间目标可能已经变了：
      · merge：别的任务追加过页 → offset 必须按当前最大页算，否则覆盖它的新页
      · new  ：同名任务已经建好包 → 必须 409 拒绝，而不是把人家替换掉
    """
    lk = _target_lock(target)
    if not lk.acquire(blocking=False):
        _set(job, message="这个词库还有一个构建在跑，排队中…")
        lk.acquire()
    got_global = False
    try:
        if not _BUILD_GLOBAL.acquire(blocking=False):
            _set(job, message="前面还有构建任务在跑，排队中…")
            _BUILD_GLOBAL.acquire()          # 注意：只有没拿到时才阻塞等待
        got_global = True
        if _cancel_if_requested(job):
            return
        if mode == "merge":
            info = pack_info(pack_name)
            if not info:
                raise WebError(404, "词库 %s 在排队期间不见了（pack_data.json 被删？）"
                                    % pack_name)
            _log(job, "锁内重读：现有 %d 页 / %d 词 / 最大页 %d"
                 % (info["pages"], info["words"], info["max_page"]))
        else:
            if os.path.lexists(target):
                raise WebError(409, "已存在同名词库：%s（排队期间被别人创建了，"
                                    "请换一个名字，或改用「并入现有词库」）" % pack_name)
            info = {"pages": 0, "words": 0, "max_page": 0}
        _build_body(job, meta, rows, mode, target, pack_name, info)
    except WebError as e:
        _finish(job, "error", error=e.message, message="生成失败")
    except Exception as e:
        _finish(job, "error", error="生成异常：%s: %s" % (type(e).__name__, e),
                message="生成异常")
    finally:
        if got_global:
            _BUILD_GLOBAL.release()
        lk.release()


def _install_stage(stage, target, backup, jid=None):
    """把 stage 换成 target。Windows 不能 os.replace 覆盖非空目录 → 先改名旧包。

    每一步都写进事务记录（work/_txn/<jid>.json）：进程在两次 rename 之间被杀掉，
    下次启动 init() 才能按磁盘现状回滚 —— 否则词库只是「从正常位置消失」，
    磁盘上还在 backup 里，用户却看不到（R1：不许靠人肉翻目录恢复）。
    """
    had = os.path.isdir(target)
    if jid:
        _txn_write(jid, {"jid": jid, "target": target, "stage": stage,
                         "backup": backup, "phase": "begin", "ts": time.time()})
    if had:
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        if os.path.isdir(backup):
            # jid 唯一 → 只可能是同一 job 的历史残留，正常流程不该出现
            print("[webgen] 警告：备份目录已存在，将覆盖：%s" % backup, file=sys.stderr)
            shutil.rmtree(backup, ignore_errors=True)
        if jid:
            _txn_write(jid, {"jid": jid, "target": target, "stage": stage,
                             "backup": backup, "phase": "renamed",
                             "ts": time.time()})
        os.rename(target, backup)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.rename(stage, target)
    except Exception:
        if had:
            try:
                os.rename(backup, target)
            except Exception:
                pass
        if jid:
            _txn_clear(jid)
        raise
    if jid:
        _txn_write(jid, {"jid": jid, "target": target, "stage": stage,
                         "backup": backup, "phase": "installed",
                         "ts": time.time()})
    if had:
        shutil.rmtree(backup, ignore_errors=True)
    if jid:
        _txn_clear(jid)


def _cancel_if_requested(job, stage=None):
    """统一的取消检查点：返回 True 表示「已按取消处理，调用方立刻 return」。

    ⚠️ 绝不能像旧代码那样在开始处 `job["cancel"] = False` —— 那会把**排队期间**
    用户点的取消直接抹掉（接口已返回"正在取消"，任务却照常提交）。
    """
    if not job.get("cancel"):
        return False
    if stage and os.path.isdir(stage):
        shutil.rmtree(stage, ignore_errors=True)
    _finish(job, "canceled", "已取消", progress=0)
    return True


def _mp3_ok(path):
    """用 build_vocab.mp3_frames 校验「能解出帧」—— 只读工具，不复制实现。"""
    try:
        from build_vocab import mp3_frames
        frames, sec = mp3_frames(path)
        return frames > 0 and sec > 0.05
    except Exception:
        return False


def _build_body(job, meta, rows, mode, target, pack_name, info):
    jid = job["id"]
    work_dir = os.path.join(WORK, "_build", jid)
    stage = os.path.join(STAGE, jid)
    backup = os.path.join(BACKUP, jid)
    offset = int(info.get("max_page") or 0)
    new_pages = sorted({offset + int(r["page"]) for r in rows})

    try:
        # 锁内复核：排队期间目标可能被别的任务追加过页 —— HTTP 层那次判定不能当结论。
        # 只有「错位 / 整批重复」才拦；书里本来就重复的照旧放行（逐条写进日志）。
        if mode == "merge":
            hard2, _soft2, _d2 = _dup_report(rows, target)
            if hard2:
                _finish(job, "error", error=hard2, message="并入被拦下")
                return
        for d in (job.get("dup_detail") or []):
            _log(job, "  重复但按原文保留：%s（%s）→ 包第 %s 页已有，本条写入第 %s 页"
                 % (d.get("en"), d.get("cn"),
                    "、".join(str(x) for x in (d.get("pack_pages") or [])),
                    offset + int(d.get("src_page") or 0)))
        for w in (job.get("dup_warnings") or []):
            _log(job, w)

        _set(job, state="running", phase="build", progress=3,
             message="写出待合成词表（%d 条）…" % len(rows))
        shutil.rmtree(work_dir, ignore_errors=True)
        os.makedirs(work_dir, exist_ok=True)

        # 页号偏移：并入时新页从「现有最大页 + 1」起排，绝不覆盖已有页
        with open(os.path.join(work_dir, "words.csv"), "w",
                  encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["page", "en", "cn", "score"])
            for r in rows:
                w.writerow([offset + int(r["page"]), r["en"], r["cn"], "1.00"])
        # 来源痕迹只留在工作目录，不写进包（不改 pack_data.json 结构）
        with open(os.path.join(work_dir, "source.json"), "w", encoding="utf-8") as f:
            json.dump({"source_file": meta.get("name"),
                       "source_file_sha256": meta.get("sha256"),
                       "mode": mode, "offset": offset, "pack": pack_name,
                       "rows": [{"source_page": r["page"],
                                 "pack_page": offset + int(r["page"]),
                                 "en": r["en"], "cn": r["cn"]} for r in rows]},
                      f, ensure_ascii=False, indent=1)

        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(STAGE, exist_ok=True)
        if mode == "merge":
            _log(job, "把现有 %d 页复制到暂存区…" % info.get("pages", 0))
            shutil.copytree(target, stage)
            cmd = [PY, BUILD_VOCAB, "build", "--work", work_dir,
                   "--name", pack_name, "--merge-into", stage]
        else:
            # 展示名与目录名必须分开：--name 只进 <title>，--out-dir 才是暂存目录。
            # 旧写法 `--name jid --out STAGE` 会把随机 job id 写进播放器标题。
            cmd = [PY, BUILD_VOCAB, "build", "--work", work_dir,
                   "--name", pack_name, "--out-dir", stage]

        if _cancel_if_requested(job, stage):
            return

        def prog(line):
            p = _build_progress(line)
            if p is not None:
                _set(job, progress=p)

        rc, canceled = _exec(job, cmd, progress_cb=prog)
        if canceled:
            shutil.rmtree(stage, ignore_errors=True)
            _finish(job, "canceled", "已取消", progress=0)
            return
        if rc != 0:
            shutil.rmtree(stage, ignore_errors=True)
            _finish(job, "error",
                    error="合成/打包失败（退出码 %s）—— 详见上方日志" % rc,
                    message="生成失败")
            return

        # ── 校验暂存包：页数、词数、新页音频都必须在且非空 ──
        _set(job, phase="stage", progress=87, message="校验暂存包…")
        pkg = os.path.join(stage, "pack_data.json")
        if not os.path.isfile(pkg) or not os.path.isfile(os.path.join(stage, "index.html")):
            shutil.rmtree(stage, ignore_errors=True)
            _finish(job, "error", error="暂存包不完整（缺 pack_data.json 或 index.html）",
                    message="校验失败")
            return
        with open(pkg, encoding="utf-8") as f:
            data = json.load(f)
        pages = data.get("pages") or []
        by_page = {int(p.get("page")): p for p in pages}
        want_pages = int(info.get("pages") or 0) + len(new_pages)
        problems = []
        if len(pages) != want_pages:
            problems.append("页数 %d ≠ 期望 %d" % (len(pages), want_pages))
        per_page = {}
        for r in rows:
            pg = offset + int(r["page"])
            per_page[pg] = per_page.get(pg, 0) + 1
        for pg in new_pages:
            p = by_page.get(pg)
            if not p:
                problems.append("缺第 %d 页" % pg)
                continue
            want_w = per_page[pg]
            if int(p.get("words") or 0) != want_w:
                problems.append("第 %d 页词数 %s ≠ %d"
                                % (pg, p.get("words"), want_w))
            tracks = ((data.get("audio", {}).get(str(pg)) or {}).get("tracks") or [])
            # 数量必须等于词数。旧代码只遍历「已有 tracks」，列表为空时循环 0 次 →
            # 一个没有逐词音轨的包也能通过校验、装进正式目录。
            if len(tracks) != want_w:
                problems.append("第 %d 页逐词音轨 %d 条 ≠ 词数 %d"
                                % (pg, len(tracks), want_w))
            for i, rel in enumerate(tracks, 1):
                url = (rel.get("url") or "")
                exp = "audio/p%03d_%03d.mp3" % (pg, i)
                if url.replace("\\", "/") != exp:
                    problems.append("第 %d 页第 %d 条音轨路径异常：%s（应为 %s）"
                                    % (pg, i, url, exp))
                    break
                fp = os.path.join(stage, url.replace("/", os.sep))
                if not os.path.isfile(fp) or os.path.getsize(fp) <= 0:
                    problems.append("第 %d 页音频缺失/为空：%s" % (pg, url))
                    break
                if not _mp3_ok(fp):
                    problems.append("第 %d 页音频解不出帧（坏文件）：%s" % (pg, url))
                    break
            mp3 = os.path.join(stage, "audio", "p%03d.mp3" % pg)
            if not os.path.isfile(mp3) or os.path.getsize(mp3) <= 0:
                problems.append("第 %d 页整页 MP3 缺失/为空" % pg)
            elif not _mp3_ok(mp3):
                problems.append("第 %d 页整页 MP3 解不出帧（坏文件）" % pg)

        # 播放器里的内联 DATA 必须与 pack_data.json 一致：
        # 注入写错（如反斜杠被 re 再解释一次）时，包照样能播，但词条已不是原文。
        try:
            with open(os.path.join(stage, "index.html"), encoding="utf-8") as f:
                html = f.read()
            m = re.search(r"^const DATA = (.*);$", html, re.M)
            inlined = json.loads(m.group(1)) if m else None
            if not inlined:
                problems.append("index.html 里找不到内联 DATA")
            else:
                a = [(p.get("page"), p.get("words")) for p in inlined.get("pages") or []]
                b = [(p.get("page"), p.get("words")) for p in pages]
                if a != b:
                    problems.append("index.html 内联数据与 pack_data.json 不一致")
        except Exception as e:
            problems.append("index.html 内联数据读不出来：%s: %s"
                            % (type(e).__name__, e))

        if problems:
            shutil.rmtree(stage, ignore_errors=True)
            _finish(job, "error",
                    error="暂存包校验失败（原词库未被改动）：" + "；".join(problems[:5]),
                    message="校验失败")
            return

        # ── 原子替换 ──
        if _cancel_if_requested(job, stage):
            return
        _set(job, phase="swap", progress=93, message="替换词库目录…")
        _log(job, "替换目标目录：%s" % target)
        # 进入提交区间后不再接受取消：宁可明确拒绝，也不要「接受了再忽略」
        job["committed"] = True
        _install_stage(stage, target, backup, jid=jid)

        _finish(job, "done", progress=100,
                message="已生成 %d %s / %d 词" % (len(new_pages),
                                                 "页", len(rows)),
                result={"pack": "packs/" + pack_name, "pack_id": pack_name,
                        "url": "/vocab-tool/packs/%s/index.html" % pack_name,
                        "mode": mode, "added_pages": len(new_pages),
                        "pages": len(pages), "words": len(rows),
                        "offset": offset, "new_pages": new_pages,
                        "dup_warnings": list(job.get("dup_warnings") or [])})
    finally:
        # stage 在成功时已被 rename 走；失败/取消时这里兜底清理
        if os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)


# ────────────────────────────── 取消 ──────────────────────────────
_TERMINAL = ("done", "error", "canceled", "interrupted")


def cancel_job(job_id):
    """取消 = 置标记 + 杀进程树。

    只置标记不够：子进程若卡在 TTS/ffmpeg 上不会自己退出；
    只杀进程也不够：_exec 会把「被杀的进程」当成正常结束。
    两件都做，再由 _exec 把 canceled 传回调用方，保证不落半成品。
    """
    job = _get_job_obj(job_id)
    if not job:
        raise WebError(404, "任务不存在（服务重启过，进程已经没了，无法取消）")
    if job.get("state") in _TERMINAL:
        return False
    if job.get("committed"):
        # 已经进入「改名换目录」区间：这时候接受取消就是撒谎
        raise WebError(409, "任务已进入提交阶段，词库正在替换，无法取消")
    job["cancel"] = True
    _set(job, state="cancel_requested", message="正在取消…")
    _kill_tree(job.get("proc"))
    return True
