# -*- coding: utf-8 -*-
"""用 CDP 给页面截图（可模拟手机视口、可先滚动到指定位置）。
   比 --screenshot 强的地方：能指定视口尺寸 + 能先跑 JS 把页面弄到要看的那个状态。

   用法：python _cdp_shot.py <html路径> <输出png> <宽> <高> <模式>
        模式：top（顶部） / bottom（滚到底） / mid（滚到中间）
"""
import asyncio
import base64
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
from _gateutil import edge_path

EDGE = edge_path()        # $EDGE 或两个常见安装位置，见 _gateutil.py
PORT = 9444

JS_TMPL = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 关掉动画，截图才稳定
  const st = document.createElement('style');
  st.textContent = '*{animation:none!important;transition:none!important}';
  document.head.appendChild(st);
  const mode = 'MODE';
  if (mode.startsWith('home') || mode.startsWith('list')) {
    // 工作台 / 包的列表页：不点卡片、不点播放。
    // 工作台要等 fetch 回来；包的列表页是同步渲染的，等一会儿就够
    if (mode.startsWith('home')) {
      for (let i = 0; i < 60 && !document.querySelector('.pack'); i++) await sleep(100);
      await sleep(300);
      if (mode.indexOf('-dlg') >= 0) {          // 打开第一张卡的编辑弹窗
        const b = document.querySelector('[data-edit]');
        if (b) b.click();
        await sleep(400);
      }
      if (mode.indexOf('-dcd') >= 0) {          // 再点「删除此词库」，看二次确认视图
        const d = document.getElementById('ed-del');
        if (d) d.click();
        await sleep(300);
      }
    } else {
      await sleep(400);
    }
  } else {
    document.querySelectorAll('.card')[0].click();      // 进播放页
    await sleep(200);
    document.getElementById('play').click();            // 开始播放
    await sleep(900);
  }
  if (mode.indexOf('bottom') >= 0) window.scrollTo(0, document.body.scrollHeight);
  else if (mode === 'mid' || mode === 'home-mid') window.scrollTo(0, document.body.scrollHeight * 0.45);
  else if (mode === 'dlg') document.getElementById('dl').click();
  await sleep(500);
  return 'ok';
})()
"""


async def main(html, out, w, h, mode):
    import aiohttp
    # 也接受 http(s) URL：工作台（/）在 file:// 下只有一句说明，截不到卡片
    if html.startswith("http://") or html.startswith("https://"):
        url = html
    else:
        url = "file:///" + os.path.abspath(html).replace("\\", "/")
    prof = tempfile.mkdtemp(prefix="edge_shot_")
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--mute-audio", "--allow-file-access-from-files",
         "--autoplay-policy=no-user-gesture-required", "--hide-scrollbars",
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
                await send("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2,
                            "mobile": False})
                await send("Page.navigate", {"url": url})
                for _ in range(80):
                    r = await send("Runtime.evaluate",
                                   {"expression": "document.readyState", "returnByValue": True})
                    if r.get("result", {}).get("result", {}).get("value") == "complete":
                        break
                    await asyncio.sleep(0.2)
                await send("Runtime.evaluate",
                           {"expression": JS_TMPL.replace("MODE", mode),
                            "awaitPromise": True, "returnByValue": True})
                r = await send("Page.captureScreenshot", {"format": "png"})
                data = r.get("result", {}).get("data")
                if not data:
                    print("!! 截图失败")
                    return 2
                with open(out, "wb") as f:
                    f.write(base64.b64decode(data))
                print("已截图 %s  (%dx%d, %s)" % (out, w, h, mode))
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
    a = sys.argv
    sys.exit(asyncio.run(main(
        a[1] if len(a) > 1 else "packs/初中英语词汇表_第1页/index.html",
        a[2] if len(a) > 2 else "_shot.png",
        int(a[3]) if len(a) > 3 else 430,
        int(a[4]) if len(a) > 4 else 900,
        a[5] if len(a) > 5 else "bottom")))
