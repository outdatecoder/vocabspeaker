# -*- coding: utf-8 -*-
"""PC / 移动双态断言：**包内不再有「添加词库 / 新增页」命令说明**，也没有任何上传控件。

背景（v1.3）：v1.3 之前列表页底部有一张折叠卡，PC 端贴两段 python 命令 + 复制按钮 +
「支持的格式」一行。现在新增/加页都走顶栏「＋ 添加词表」进生成器，那坨静态命令已删除。
本门禁随之改成**反向断言**：确认它真的没了，同时守住这个门禁原本要防的东西 ——
包里不许长出 `input[type=file]` / 「上传」按钮（离线包没有服务端，放控件就是假承诺）。

独立于页面实现 —— 这里只读 DOM，不调用页面里的 isMobileView() 来做判定，
也不读页面自己的变量来判断"有没有渲染"，避免「用实现验证实现」（AGENTS.md R1）。
真值来源：删掉的那些字面量本身（`build_vocab` / `merge-into` / `支持的格式`）。

v1.4 追加：列表页最底部那行草图话术（「生产版要点…不需要一次装载 41.8 MB」）已删除，
断言它不再出现（`GONE_NOTE`，与上面那组 `GONE_MARKS` **刻意分开**：两组失败时
才分得清是哪一次没删干净）。同时正向断言播放页底部那行 `#kernel-note` **仍在** ——
用户要删的只有列表页那行，不能把整块脚注一起铲掉。

用法：
    python _cdp_ui.py [包目录]
默认包目录：packs/初中英语词汇表
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
from _gateutil import edge_path

EDGE = edge_path()        # $EDGE 或两个常见安装位置，见 _gateutil.py
PORT = 9556

PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

PROBE = r"""
(() => {
  const out = {};
  const box = document.getElementById('addbox');
  out.ver = ((document.getElementById('ver') || {}).textContent || '').trim();
  out.hasBox = !!box;
  out.boxTxt = box ? (box.innerText || '').replace(/\s+/g, ' ').trim() : '';
  out.boxDisplay = box ? getComputedStyle(box).display : 'missing';
  out.hasHead = !!document.getElementById('addbox-head');
  out.cmdCount = document.querySelectorAll('.cmd').length;
  out.copyBtns = document.querySelectorAll('.cmd-copy').length;
  out.fileInputs = document.querySelectorAll('input[type=file]').length;
  out.uploadBtns = Array.from(document.querySelectorAll('button'))
      .filter(b => /上传/.test(b.textContent || '')).length;
  out.cardCount = document.querySelectorAll('#list-body .card').length;
  // 播放页底部那行说明（用户要求保留）是否还在 DOM 里
  out.hasKernelNote = !!document.getElementById('kernel-note');
  // 不截断：断言必须落在完整文本上（R2）
  out.txt = (document.body.innerText || '').replace(/\s+/g, ' ');
  out.pageOverflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  return JSON.stringify(out);
})()
"""

# 删掉的那些字面量：只要它们出现在渲染出来的文本里，就说明这块没删干净
GONE_MARKS = ("build_vocab", "merge-into", "支持的格式", "添加词库")

# v1.4 删掉的列表页脚注话术（与上面那组分开，失败时可归因到具体某一轮）
GONE_NOTE = ("生产版要点", "41.8")


async def run_case(pack, name, ua, platform, width, height, mobile, touch):
    import aiohttp
    url = "file:///" + os.path.join(pack, "index.html").replace("\\", "/")
    prof = tempfile.mkdtemp(prefix="edge_ui_")
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
                return None

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
                await send("Emulation.setUserAgentOverride",
                           {"userAgent": ua, "platform": platform})
                await send("Emulation.setDeviceMetricsOverride",
                           {"width": width, "height": height,
                            "deviceScaleFactor": 1, "mobile": mobile})
                await send("Emulation.setTouchEmulationEnabled",
                           {"enabled": bool(touch), "maxTouchPoints": 5 if touch else 0})
                await send("Page.navigate", {"url": url})
                for _ in range(80):
                    r = await send("Runtime.evaluate",
                                   {"expression": "document.readyState", "returnByValue": True})
                    if r.get("result", {}).get("result", {}).get("value") == "complete":
                        break
                    await asyncio.sleep(0.2)
                await asyncio.sleep(0.3)
                r = await send("Runtime.evaluate",
                               {"expression": PROBE, "returnByValue": True})
                val = r.get("result", {}).get("result", {}).get("value")
                if not val:
                    print("!! 页面内异常：", str(r)[:800])
                    return None
                return json.loads(val)
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


def check(cond, label, detail=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", label,
                          ("   [%s]" % detail) if detail != "" else ""))
    return bool(cond)


def verify_pc(o):
    print("\n[PC 态] 1440x900  Windows UA")
    res = []
    txt = o["txt"]
    left = [k for k in GONE_MARKS if k in txt]
    res.append(check(o["ver"] == "v2026-11-18.1", "顶栏版本号已是本次版本", o["ver"]))
    res.append(check(not o["hasHead"], "「添加词库 / 新增页」折叠标题已不存在"))
    res.append(check(o["cmdCount"] == 0, "命令卡数量为 0", o["cmdCount"]))
    res.append(check(o["copyBtns"] == 0, "「复制」按钮数量为 0", o["copyBtns"]))
    res.append(check(o["boxTxt"] == "" and o["boxDisplay"] == "none",
                    "PC 端说明容器为空且已收起（不留空条）",
                    "%r / %s" % (o["boxTxt"], o["boxDisplay"])))
    res.append(check(not left, "渲染文本里没有命令 / 格式 / 添加词库 字样", left))
    res.append(check(o["fileInputs"] == 0, "不存在 input[type=file]", o["fileInputs"]))
    res.append(check(o["uploadBtns"] == 0, "不存在「上传」按钮", o["uploadBtns"]))
    res.append(check(o["cardCount"] >= 1, "列表页仍渲染出页卡片", o["cardCount"]))
    left_note = [k for k in GONE_NOTE if k in txt]
    res.append(check(not left_note, "列表页底部草图话术（生产版要点/41.8）已消失", left_note))
    res.append(check(o["hasKernelNote"] is True,
                    "播放页底部 #kernel-note 仍在（只删了列表页那行）"))
    res.append(check(not o["pageOverflow"], "无横向溢出"))
    return res


def verify_mobile(o):
    print("\n[移动态] 390x844  iPhone UA + touch")
    res = []
    txt = o["txt"]
    left = [k for k in GONE_MARKS if k in txt]
    res.append(check(o["boxTxt"] == "请在 PC 端上传与生成",
                    "移动端只剩一句 PC 端提示", o["boxTxt"]))
    res.append(check(not o["hasHead"], "没有可点击的折叠标题"))
    res.append(check(o["cmdCount"] == 0 and o["copyBtns"] == 0,
                    "不渲染命令卡与复制按钮", "%s / %s" % (o["cmdCount"], o["copyBtns"])))
    res.append(check(not left, "渲染文本里没有命令 / 格式 / 添加词库 字样", left))
    res.append(check(o["fileInputs"] == 0, "不存在 input[type=file]", o["fileInputs"]))
    res.append(check(o["uploadBtns"] == 0, "不存在「上传」按钮", o["uploadBtns"]))
    res.append(check(o["cardCount"] >= 1, "列表页仍渲染出页卡片", o["cardCount"]))
    left_note = [k for k in GONE_NOTE if k in txt]
    res.append(check(not left_note, "列表页底部草图话术（生产版要点/41.8）已消失", left_note))
    res.append(check(o["hasKernelNote"] is True,
                    "播放页底部 #kernel-note 仍在（只删了列表页那行）"))
    res.append(check(not o["pageOverflow"], "无横向溢出"))
    return res


async def main():
    pack = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                           os.path.join(HERE, "packs", "初中英语词汇表"))
    print("UI 双态验证：%s" % os.path.relpath(pack, HERE))

    pc = await run_case(pack, "pc", PC_UA, "Win32", 1440, 900, False, False)
    mob = await run_case(pack, "mobile", MOBILE_UA, "iPhone", 390, 844, True, True)

    results = []
    if pc is None:
        print("!! PC 态取不到结果")
        results.append(False)
    else:
        results += verify_pc(pc)
    if mob is None:
        print("!! 移动态取不到结果")
        results.append(False)
    else:
        results += verify_mobile(mob)

    ok = sum(1 for r in results if r)
    print("\n结果: %d PASS / %d FAIL" % (ok, len(results) - ok))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
