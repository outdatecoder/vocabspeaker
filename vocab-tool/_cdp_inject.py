# -*- coding: utf-8 -*-
"""把一个真实 .js 文件内联进词库包页面，用 CDP 跑完并读回结果。

比 `_cdp_probe.py` 里那些 Runtime.evaluate 巨型字符串更可靠：
JS 放在真实文件里由浏览器自己解析，不会再被 Python 的转义/编码搞出假语法错误。

用法：python _cdp_inject.py <包目录> <js文件> [等待秒数]
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
from _gateutil import PROJECT_ROOT, edge_path

EDGE = edge_path()        # $EDGE 或两个常见安装位置，见 _gateutil.py
PORT = 9555


async def main(pack, js_path, wait_sec):
    import aiohttp
    src = os.path.join(pack, "index.html")
    if not os.path.isfile(src):
        raise SystemExit("找不到 %s" % src)
    js = open(js_path, encoding="utf-8").read()
    inj = os.path.join(pack, "_inj.html")
    html = open(src, encoding="utf-8").read()
    # 用外链而不是内联：外链脚本是「经典脚本」，能正常解析；
    # 同时也避免 js 里出现 </script> 之类字符截断 HTML。
    with open(os.path.join(pack, "_trace.js"), "w", encoding="utf-8", newline="") as f:
        f.write(js)
    open(inj, "w", encoding="utf-8", newline="").write(
        html.replace("</body>", '<script src="_trace.js"></script></body>', 1))

    if len(sys.argv) > 5 and sys.argv[5] == "http":
        # 走本地 HTTP 预览，排除 file:// 与 http:// 的差异
        rel = os.path.relpath(inj, PROJECT_ROOT).replace("\\", "/")
        import urllib.parse
        port = sys.argv[6] if len(sys.argv) > 6 else "8901"
        url = "http://127.0.0.1:%s/" % port + urllib.parse.quote(rel)
    else:
        url = "file:///" + os.path.abspath(inj).replace("\\", "/")
    if len(sys.argv) > 4 and sys.argv[4] == "debug":
        url += "?debug=1"
    prof = tempfile.mkdtemp(prefix="edge_inj_")
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--mute-audio", "--allow-file-access-from-files",
         "--autoplay-policy=no-user-gesture-required",
         "--remote-debugging-port=%d" % PORT, "--user-data-dir=" + prof, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as s:
            ws_url = None
            for _ in range(60):
                try:
                    async with s.get("http://127.0.0.1:%d/json/list" % PORT) as r:
                        tg = await r.json()
                    pg = [t for t in tg if t.get("type") == "page"]
                    if pg:
                        ws_url = pg[0]["webSocketDebuggerUrl"]
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            if not ws_url:
                print("!! 连不上 CDP")
                return 3
            async with s.ws_connect(ws_url, max_msg_size=64 * 1024 * 1024) as ws:
                mid = 0

                async def send(method, params=None):
                    nonlocal mid
                    mid += 1
                    my = mid
                    await ws.send_json({"id": my, "method": method, "params": params or {}})
                    while True:
                        m = await ws.receive_json()
                        if m.get("id") == my:
                            return m

                await send("Page.enable")
                await send("Runtime.enable")
                await send("Page.navigate", {"url": url})
                for _ in range(int(wait_sec * 5)):
                    r = await send("Runtime.evaluate",
                                   {"expression": "!!window.__traceDone", "returnByValue": True})
                    if r.get("result", {}).get("result", {}).get("value") is True:
                        break
                    await asyncio.sleep(0.2)
                r = await send("Runtime.evaluate",
                               {"expression": "(document.getElementById('__trace')||{}).textContent || '(无结果)'",
                                "returnByValue": True})
                txt = r.get("result", {}).get("result", {}).get("value", "")
                print("追踪：%s + %s" % (os.path.basename(pack), os.path.basename(js_path)))
                print(txt)
                return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(prof, ignore_errors=True)
        for f in (inj, os.path.join(pack, "_trace.js")):
            try:
                os.remove(f)
            except OSError:
                pass


if __name__ == "__main__":
    pk = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "packs/初中英语词汇表_第1页")
    jsf = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else "_trace.js")
    wt = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    sys.exit(asyncio.run(main(pk, jsf, wt)))
