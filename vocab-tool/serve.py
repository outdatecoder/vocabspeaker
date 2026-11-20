# -*- coding: utf-8 -*-
"""带 HTTP Range 支持的极简静态服务器 + 生成器 API（纯标准库）。

为什么必须用它，不能再用 `python -m http.server`：
    自带的 SimpleHTTPRequestHandler **不支持 Range 请求**，也不发
    `Accept-Ranges: bytes`。Chromium 因此把音频整体判为「不可 seek」——
    `audio.seekable.length` 会是 1 但 `end(0) == 0`，
    于是播放器里「点某条重听 / 上一词下一词 / 暂停后恢复」全部失效，
    表现就是「点词条没反应、最后总是从第一条开始、连读 first word」。
    `file://` 打开不受影响（本地文件可随机访问），所以这个问题
    只在通过 HTTP 预览时才暴露 —— 极易误判成"播放器逻辑有 bug"。

本文件只做 HTTP 这一层：路由、token 分级、JSON、Range 静态。
业务编排全部在 `webgen.py`（它不 import HTTP，可被探针直接调用）。

用法：
    python serve.py                          # 127.0.0.1:8899，docroot = 本脚本的上上级
    python serve.py 8901                     # 换端口（老脚本仍这么调）
    python serve.py 8901 D:\\some\\dir --host 0.0.0.0

接口（★ = 需要 X-Token，只接受生成器页面发起的写操作）：
    GET  /api/packs                 词库列表（免 token，静态播放器靠它探测入口）
    GET  /api/job/<id>              任务状态、阶段、进度、日志尾
    GET  /api/preview?job=<id>      解析结果预览（分组词表）
    POST /api/upload        ★       原始字节流上传（文件名放 X-Filename）
    POST /api/parse         ★       {doc_id, with_pages}
    POST /api/build         ★       {doc_id, job, rows, mode, pack_id, name}
    POST /api/cancel        ★       {job_id}
    POST /api/pack/rename   ★       {id, name}   → 改目录名 + 重渲染 index.html
    POST /api/pack/delete   ★       {id}         → 移入 work/_trash（不 rmtree）
    GET  /                          词库工作台（注入本次 token：它现在有写操作）
    GET  /generator                 生成器页面（注入本次 token）
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# 便携包里的解释器跑在「隔离模式」：带上 runtime\pythonXY._pth 后，sys.path
# **只**来自那个文件，脚本自身所在的目录不会被自动加进去（实测
# `python serve.py` 的 sys.path 里没有 vocab-tool）。不补这一行，
# dist 包一启动就 `ModuleNotFoundError: No module named 'webgen'`。
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import webgen  # noqa: E402  (must come after the sys.path fix above)

TOKEN_PLACEHOLDER = "__PET_TOKEN__"
MAX_JSON_BODY = 8 * 1024 * 1024      # /api/build 会带整份校对后的词表

WRITE_ROUTES = ("/api/upload", "/api/parse", "/api/build", "/api/cancel",
                "/api/pack/rename", "/api/pack/delete")


class RangeHandler(SimpleHTTPRequestHandler):
    server_version = "RangeHTTP/1.0"

    # ───────────────────────── HTTP Range 静态 ─────────────────────────
    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return None
        f = open(path, "rb")
        try:
            fs = os.fstat(f.fileno())
            size = fs.st_size
            ctype = self.guess_type(path)

            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng)
                if m:
                    g1, g2 = m.group(1), m.group(2)
                    if g1:
                        start = int(g1)
                        end = int(g2) if g2 else size - 1
                    elif g2:                       # bytes=-N → 末尾 N 字节
                        start = max(0, size - int(g2))
                    else:
                        start = size                     # 无意义 → 416
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        f.close()
                        return None
                    end = min(end, size - 1)
                    code = 206

            self._left = end - start + 1
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")     # ← 关键头
            self.send_header("Content-Length", str(self._left))
            if code == 206:
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
            self.end_headers()
            if start:
                f.seek(start)
            return f
        except Exception:
            f.close()
            raise

    def copyfile(self, source, outputfile):
        left = getattr(self, "_left", None)
        if left is None:
            return super().copyfile(source, outputfile)
        while left > 0:
            chunk = source.read(min(65536, left))
            if not chunk:
                break
            outputfile.write(chunk)
            left -= len(chunk)

    # ────────────────────────────── 工具 ──────────────────────────────
    def _route(self):
        return urllib.parse.urlsplit(self.path).path

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json_body(self):
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise webgen.WebError(411, "缺少 Content-Length")
        try:
            n = int(raw_len)
        except ValueError:
            raise webgen.WebError(400, "Content-Length 不是整数")
        if n <= 0:
            raise webgen.WebError(400, "请求体是空的")
        if n > MAX_JSON_BODY:
            raise webgen.WebError(413, "请求体过大")
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise webgen.WebError(400, "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise webgen.WebError(400, "请求体必须是 JSON 对象")
        return data

    def _need_token(self):
        if not webgen.token_ok(self.headers.get("X-Token")):
            raise webgen.WebError(
                403, "缺少或错误的 X-Token —— 写接口只接受生成器页面（/generator）发起的请求")

    # ────────────────────────────── GET ──────────────────────────────
    def do_GET(self):
        p = self._route()
        if p == "/api/packs":
            return self._json(200, {"packs": webgen.list_packs()})
        if p.startswith("/api/job/"):
            jid = urllib.parse.unquote(p[len("/api/job/"):]).strip("/")
            try:
                return self._json(200, webgen.get_job(jid))
            except webgen.WebError as e:
                return self._json(e.code, {"error": e.message})
        if p == "/api/preview":
            try:
                jid = (self._query().get("job") or [""])[0]
                return self._json(200, webgen.preview(jid))
            except webgen.WebError as e:
                return self._json(e.code, {"error": e.message})
        if p.startswith("/api/"):
            return self._json(404, {"error": "没有这个接口：%s" % p})
        # ── 工具层三个页面（永不写进词库包）──
        # 预留（本期不实现）：GET /api/export?pack=<id> → application/zip，
        #   免 token、复用 webgen.resolve_pack_id() 做越界检查、
        #   Content-Disposition: attachment; filename*=UTF-8''<id>.zip。
        #   接入点见《词库工作台_实施方案》§8.4，别在这里先写半套。
        if p in ("/", "/home", "/home.html", "/vocab-tool/home.html"):
            # 工作台有「✎ 编辑」这类写操作（改名 / 删除），必须注入 token
            return self._serve_page("home.html", inject_token=True)
        if p in ("/generator", "/vocab-tool/generator.html"):
            return self._serve_page("generator.html", inject_token=True)
        return super().do_GET()

    def _serve_page(self, filename, inject_token=False):
        """两个工具页共用：读 HERE/<filename>，可选注入 token，no-store 输出。"""
        path = os.path.join(HERE, filename)
        if not os.path.isfile(path):
            return self._json(404, {"error": "%s 还不存在" % filename})
        with open(path, encoding="utf-8") as f:
            html = f.read()
        if inject_token:
            html = html.replace(TOKEN_PLACEHOLDER, webgen.token())
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")     # token 每次启动都变
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── 关掉目录索引：packs/ 里几百个 mp3 不该变成可浏览目录 ──
    def list_directory(self, path):
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    # ────────────────────────────── POST ──────────────────────────────
    def do_POST(self):
        p = self._route()
        if p not in WRITE_ROUTES:
            return self._json(404, {"error": "没有这个写接口：%s" % p})
        try:
            self._need_token()                      # 先验 token，再碰任何业务逻辑
            if p == "/api/upload":
                name = urllib.parse.unquote(self.headers.get("X-Filename") or "")
                try:
                    self.connection.settimeout(60.0)   # 客户端半路死掉别让线程永远挂着
                except Exception:
                    pass
                meta = webgen.save_upload(self.rfile,
                                          self.headers.get("Content-Length"),
                                          name)
                return self._json(200, meta)
            body = self._json_body()
            if p == "/api/parse":
                jid = webgen.start_parse_job((body.get("doc_id") or "").strip(),
                                             bool(body.get("with_pages")))
                return self._json(200, {"job_id": jid})
            if p == "/api/build":
                jid = webgen.start_build_job(body.get("doc_id"), body.get("rows"),
                                             body.get("mode"), body.get("pack_id"),
                                             body.get("name"))
                return self._json(200, {"job_id": jid})
            if p == "/api/pack/rename":
                return self._json(200, {"pack": webgen.rename_pack(body.get("id"),
                                                                   body.get("name"))})
            if p == "/api/pack/delete":
                return self._json(200, webgen.delete_pack(body.get("id")))
            jid = webgen.cancel_job(body.get("job_id"))
            return self._json(200, {"canceled": bool(jid), "job_id": body.get("job_id")})
        except webgen.WebError as e:
            return self._json(e.code, {"error": e.message})
        except Exception as e:
            return self._json(500, {"error": "服务端异常：%s: %s" % (type(e).__name__, e)})

    def log_message(self, fmt, *args):
        try:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(prog="serve.py")
    ap.add_argument("portpos", nargs="?", type=int, default=None,
                    help="端口（兼容旧的 `serve.py 8901` 写法）")
    ap.add_argument("rootpos", nargs="?", default=None,
                    help="静态根目录（默认本脚本的上上级）")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--root", default=None)
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认只绑 127.0.0.1；要局域网访问才用 0.0.0.0）")
    args = ap.parse_args()

    port = args.port or args.portpos or 8899
    root = args.root or args.rootpos or os.path.abspath(os.path.join(HERE, ".."))
    if not os.path.isdir(root):
        sys.exit("静态根目录不存在：%s" % root)
    os.chdir(root)

    webgen.init()
    srv = ThreadingHTTPServer((args.host, port), RangeHandler)
    sys.stderr.write("serving %s at http://%s:%d/  (Range: on)\n"
                     % (root, args.host, port))
    sys.stderr.write("generator: http://%s:%d/generator  (token: on)\n"
                     % ("127.0.0.1" if args.host in ("0.0.0.0", "") else args.host, port))
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
