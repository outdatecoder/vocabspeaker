# -*- coding: utf-8 -*-
"""CDP 驱动「真实浏览器」跑断言 —— 补 headless 的媒体能力短板。

## 三个验证工具的分工（别重复造）

| 工具 | 环境 | 擅长 |
|------|------|------|
| `_probe_pack.py` | headless + 虚拟时钟 | **结构类**断言，很快；但媒体验不了（`readyState` 恒 0） |
| `_cdp_probe.py`（本文件） | 真实 Edge + **真实时间** | **交互 / 布局类**断言（自包含 JS，`Runtime.evaluate`） |
| `_cdp_inject.py` + 真实 `.js` | 真实 Edge + 外链**真实 .js 文件** | **播放流程类**断言；可直接读页面顶层 `let`（`idx`/`tracks`/`playing`），比巨型字符串注入可靠得多 |

## ⚠️ 2026-10-14 清理说明

本文件原先有 6 个场景，删掉了 5 个：`playing` / `fresh` / `paused` / `safari` / `trace`。
它们都是为「**整页 MP3 + 按时间轴 seek 到某词**」那套旧设计写的，
依赖 `tracks[k].start` 与 `currentTime` 语义 shim；而那套设计已被
「**一词一个 MP3**」取代（原因见 `AGENTS.md` 规则 R3）。
旧场景在现在的包上**永远不会通过**，留着只会误导后来人 —— 所以整体删除。

逐词播放的验证现在看这里（探针不再重复覆盖）：

    python _cdp_inject.py "packs/<词库名>" "_trace.js"    40 x http 8901
    python _cdp_inject.py "packs/<词库名>" "_trace_dl.js" 30 x http 8901

用法：
    python _cdp_probe.py "packs/xxx" [场景]
    场景：minibar —— 滚到底还能暂停 / 不遮挡末条 / 返回列表自动隐藏
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
from _gateutil import edge_path

EDGE = edge_path()        # $EDGE 或两个常见安装位置，见 _gateutil.py
PORT = 9333

PRELUDE = r"""
  const out = [];
  const log = (n, v) => out.push(n.padEnd(22) + '= ' + v);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const rowsEl = () => document.querySelectorAll('.row');
  /* 挂钩媒体原型以捕获 audio 实例：本工具在页面脚本之后注入，
     直接引用顶层 let 更脆，挂钩更稳（也顺带记录调用序列） */
  let au = null;
  const P = HTMLMediaElement.prototype.play, Q = HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.play  = function(){ au = this; return P.apply(this, arguments); };
  HTMLMediaElement.prototype.pause = function(){ au = this; return Q.apply(this, arguments); };
"""

SCEN = {}

# 底部迷你控制条：滚到列表底部还能不能暂停、会不会遮挡内容、返回列表是否自动隐藏
SCEN["minibar"] = r"""
(async () => {
""" + PRELUDE + r"""
  document.querySelectorAll('.card')[0].click();
  await sleep(300);
  log('进入播放页 底部条可见', document.getElementById('minibar').classList.contains('show'));
  document.getElementById('play').click();
  const t0 = Date.now();
  while (Date.now() - t0 < 5000 && (!au || au.readyState < 1)) await sleep(100);
  await sleep(1200);
  log('主按钮/底部条按钮', document.getElementById('play').textContent.trim() + ' | '
      + document.getElementById('play2').textContent.trim());

  window.scrollTo(0, document.body.scrollHeight);
  await sleep(500);
  const mb = document.getElementById('minibar').getBoundingClientRect();
  log('滚动到底 底部条仍在视口', (mb.top >= -1 && mb.bottom <= innerHeight + 1)
      ? 'YES' : 'NO(top=' + mb.top.toFixed(0) + ',bottom=' + mb.bottom.toFixed(0)
      + ',vh=' + innerHeight + ')');
  const pb = document.getElementById('play').getBoundingClientRect();
  log('主按钮已滚出视口', (pb.bottom < 0 || pb.top > innerHeight) ? 'YES ← 底部条就是为了它' : 'no');
  log('底部条当前词条', document.getElementById('m-en').textContent + ' / '
      + document.getElementById('m-idx').textContent);
  const rows = rowsEl(), last = rows[rows.length - 1].getBoundingClientRect();
  log('底部条是否遮挡末条', last.bottom <= mb.top + 1
      ? 'no' : 'YES 遮 ' + (last.bottom - mb.top).toFixed(0) + 'px');

  document.getElementById('play2').click();          // 用底部条暂停
  await sleep(500);
  log('点底部条后 主按钮', document.getElementById('play').textContent.trim());
  log('点底部条后 paused', au ? au.paused : '(拿不到实例)');
  log('点底部条后 底部条按钮', document.getElementById('play2').textContent.trim());

  document.getElementById('back').click();           // 返回列表
  await sleep(400);
  log('返回列表后 底部条可见', document.getElementById('minibar').classList.contains('show'));
  log('返回列表后 底部条已隐藏',
      getComputedStyle(document.getElementById('minibar')).display === 'none');
  return out.join('\n');
})()
"""


async def run(pack, scenario):
    import aiohttp
    url = "file:///" + os.path.join(pack, "index.html").replace("\\", "/")
    prof = tempfile.mkdtemp(prefix="edge_cdp_")
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
                    page = [t for t in tg if t.get("type") == "page"]
                    if page:
                        ws_url = page[0]["webSocketDebuggerUrl"]
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
                for _ in range(80):
                    r = await send("Runtime.evaluate",
                                   {"expression": "document.readyState", "returnByValue": True})
                    if r.get("result", {}).get("result", {}).get("value") == "complete":
                        break
                    await asyncio.sleep(0.2)

                r = await send("Runtime.evaluate",
                               {"expression": SCEN[scenario],
                                "awaitPromise": True, "returnByValue": True})
                res = r.get("result", {})
                print("场景 %s | 包 %s" % (scenario, os.path.relpath(pack, HERE)))
                if "exceptionDetails" in res:
                    print("!! 页面内异常：")
                    print(str(res["exceptionDetails"])[:1500])
                    return 2
                print(res.get("result", {}).get("value", "(无返回值)"))
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


if __name__ == "__main__":
    pk = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "packs/初中英语词汇表_第1页")
    sc = sys.argv[2] if len(sys.argv) > 2 else "minibar"
    if sc not in SCEN:
        print("!! 未知场景 %r；可用场景：%s" % (sc, ", ".join(sorted(SCEN))))
        sys.exit(4)
    sys.exit(asyncio.run(run(pk, sc)))
