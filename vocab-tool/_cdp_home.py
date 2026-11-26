# -*- coding: utf-8 -*-
"""工作台（/）门禁：CDP 驱动真实 Edge，断言 30 条左右。

独立真值来源（不用实现里的数字）：
  · 卡片数量 / 顺序 ← 另起一条 HTTP 请求读 /api/packs 自己算
  · 页数词数时长 ← 正则匹配卡片渲染文本，再与 API 字段比
  · 「点了真进包」← location.pathname + 播放器里 .card 的数量
  · 「打包下载没渲染」← 卡片操作区 a[href] 恰好 2 个（接入那天这条会主动失败）
  · 「改名真的改了」← 去磁盘上的 packs/<新名>/index.html 里找 <title>
  · 「删除真的删了」← 目录不在 packs 下 + work/_trash 里多一条
  · 改名/删除都拿**临时副本**做，跑完把临时包与 _trash 条目一起清掉

用法：
    python -X utf8 _cdp_home.py            # 起一个临时 serve.py，跑完即清
"""
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
from _gateutil import edge_path

EDGE = edge_path()        # $EDGE 或两个常见安装位置，见 _gateutil.py
PY = sys.executable
RESULTS = []


def T(name, ok, extra=None):
    RESULTS.append(("PASS" if ok else "FAIL", name, extra))
    return bool(ok)


def S(name, why):
    RESULTS.append(("SKIP", name, why))


class _NoTemplate(Exception):
    """packs 里一个词库都没有 → 改名/删除那组没法测，如实 SKIP 而不是崩掉。"""


def fmt_bytes(b):
    b = float(b or 0)
    if b >= 1048576:
        return "%.1f MB" % (b / 1048576)
    if b >= 1024:
        return "%d KB" % round(b / 1024)
    return "%d B" % b


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_http(port, path="/api/packs", timeout=25):
    import urllib.request
    t0 = __import__("time").time()
    while __import__("time").time() - t0 < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        __import__("time").sleep(0.3)
    return False


async def main():
    import aiohttp

    port = free_port()
    cdp_port = port + 1
    prof = tempfile.mkdtemp(prefix="edge_home_")
    srv = subprocess.Popen([PY, os.path.join(HERE, "serve.py"), str(port)],
                           cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = None
    try:
        if not wait_http(port):
            print("!! serve.py 没起来")
            return 2
        async with aiohttp.ClientSession() as s:
            # 独立真值：接口自己报的列表
            async with s.get("http://127.0.0.1:%d/api/packs" % port) as r:
                packs = (await r.json()).get("packs") or []
            async with s.get("http://127.0.0.1:%d/" % port) as r:
                home = await r.text()
            T("GET / 返回 200 且是工作台", r.status == 200 and "VocabSpeaker" in home,
              r.status)

        br = subprocess.Popen(
            [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--mute-audio",
             "--remote-debugging-port=%d" % cdp_port,
             "--user-data-dir=" + prof, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        async with aiohttp.ClientSession() as s:
            ws_url = None
            for _ in range(60):
                try:
                    async with s.get("http://127.0.0.1:%d/json/list" % cdp_port) as r:
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
                return 2

            async with s.ws_connect(ws_url, max_msg_size=64 * 1024 * 1024) as ws:
                mid = 0
                exceptions = []

                async def send(method, params=None):
                    nonlocal mid
                    mid += 1
                    my = mid
                    await ws.send_json({"id": my, "method": method, "params": params or {}})
                    while True:
                        m = await ws.receive_json()
                        if m.get("method") == "Runtime.exceptionThrown":
                            exceptions.append(str(m)[:300])
                        if m.get("id") == my:
                            return m

                async def js(expr):
                    r = await send("Runtime.evaluate",
                                   {"expression": expr, "returnByValue": True,
                                    "awaitPromise": True})
                    return (r.get("result", {}).get("result") or {}).get("value")

                async def nav(url, probe, tries=80):
                    await send("Page.navigate", {"url": url})
                    for _ in range(tries):
                        if await js(probe):
                            return True
                        await asyncio.sleep(0.25)
                    return False

                await send("Page.enable")
                await send("Runtime.enable")
                await send("Emulation.setDeviceMetricsOverride",
                           {"width": 1440, "height": 900, "deviceScaleFactor": 1,
                            "mobile": False})
                base = "http://127.0.0.1:%d" % port
                ok = await nav(base + "/", "document.querySelectorAll('.pack').length > 0 && "
                                            "document.title.indexOf('VocabSpeaker') >= 0")
                T("打开 / 并渲染工作台", ok, await js("document.title"))

                # ── 1) 卡片数量与顺序 ──
                n = await js("document.querySelectorAll('.pack').length")
                T("卡片数 = /api/packs 长度", n == len(packs), "%s vs %s" % (n, len(packs)))
                first = await js("(document.querySelector('.pack-name')||{}).textContent")
                T("首卡 = 最近更新的那个（mtime 倒序）",
                  bool(packs) and first == packs[0]["name"], "%s vs %s" % (first,
                                                                           packs[0]["name"] if packs else None))

                # ── 2) 规模行 ──
                txt = await js("(document.querySelector('.pack-meta')||{}).textContent") or ""
                p0 = packs[0] if packs else {"pages": 0, "words": 0, "sec": 0, "bytes": 0}
                # 秒数必须**四舍五入**再拆 m:ss —— 与 home.html 的 fmtSec() 同一条规则。
                # 用 int() 截断时，211.7s 会算成 3:31 而界面写 3:32，卡在小数位上误报
                # （第 1 页包 211.7s 就被这样判过一次）。
                # ⚠️ 不能用 Python 的 round()：它是「四舍六入五成双」，156.5 → 156；
                #    而 JS 的 Math.round 是 157。正好卡在 .5 的包会平白报一条 FAIL。
                sec = int(float(p0["sec"] or 0) + 0.5)
                want = "%d 页 · %d 词 · %d:%02d · %s" % (
                    p0["pages"], p0["words"], sec // 60, sec % 60,
                    fmt_bytes(p0.get("bytes")))
                T("卡片规模行 = 接口下发的页数/词数/时长/体积", txt.strip() == want,
                  "%r vs %r" % (txt.strip(), want))
                T("卡片规模行格式匹配「N 页 · M 词 · m:ss」",
                  bool(re.search(r"\d+ 页 · \d+ 词 · \d+:\d\d", txt)), txt)

                # ── 3) 卡片操作区：2 个链接（打包下载仍是注释槽位）+ 1 个「✎ 编辑」按钮 ──
                cnt = await js("Array.from(document.querySelectorAll('.pack-acts a[href]'))"
                               ".filter(a => a.closest('.pack') === document.querySelector('.pack'))"
                               ".length")
                T("卡片操作区恰好 2 个链接（打包下载仍是注释槽位）", cnt == 2, cnt)
                btns = await js("Array.from(document.querySelectorAll('.pack-acts button'))"
                                ".filter(b => b.closest('.pack') === document.querySelector('.pack'))"
                                ".map(b => b.textContent)")
                T("卡片操作区有 1 个「✎ 编辑」按钮", btns == ["✎ 编辑"], btns)

                # ── 4) 打开播放器：真进包 ──
                await js("document.querySelector('.pack-acts a[href]').click()")
                for _ in range(40):
                    if await js("location.pathname.indexOf('/vocab-tool/packs/') === 0"):
                        break
                    await asyncio.sleep(0.2)
                # 等播放器把页卡片渲染出来（不是只看路径变了）
                for _ in range(80):
                    if await js("document.querySelectorAll('.card').length >= 1"):
                        break
                    await asyncio.sleep(0.2)
                ok = await js("document.querySelectorAll('.card').length >= 1")
                T("点「打开播放器」真的进了词库包", bool(ok),
                  await js("location.pathname"))
                # 两个入口靠 fetch('/api/packs') 探测后才显示，给它一点时间
                for _ in range(30):
                    if await js("getComputedStyle(document.getElementById('home-link')).display !== 'none'"):
                        break
                    await asyncio.sleep(0.2)
                T("包内顶栏出现「← 词库列表」",
                  await js("getComputedStyle(document.getElementById('home-link')).display !== 'none'"),
                  await js("getComputedStyle(document.getElementById('home-link')).display"))
                T("包内顶栏出现「＋ 添加词表」",
                  await js("getComputedStyle(document.getElementById('gen-link')).display !== 'none'"),
                  await js("getComputedStyle(document.getElementById('gen-link')).display"))

                # ── 5) 回工作台 ──
                await js("document.getElementById('home-link').click()")
                for _ in range(40):
                    if await js("location.pathname === '/' && document.querySelectorAll('.pack').length > 0"):
                        break
                    await asyncio.sleep(0.2)
                T("点「← 词库列表」回到 /", await js("location.pathname") == "/",
                  await js("location.pathname"))

                # ── 6) 继续加词表：带 ?pack= 且已预选 ──
                if packs:
                    await nav(base + "/", "document.querySelectorAll('.pack').length > 0")
                    await js("document.querySelectorAll('.pack-acts a[href]')[1].click()")
                    for _ in range(40):
                        if await js("location.pathname === '/generator'"):
                            break
                        await asyncio.sleep(0.2)
                    # location.search 是百分号编码的，比之前先解码（中文包名）
                    T("点「＋ 继续加词表」进入 /generator 并带 ?pack=",
                      (await js("decodeURIComponent(location.search)")) == "?pack=" + packs[0]["id"],
                      await js("decodeURIComponent(location.search)"))
                    await asyncio.sleep(0.8)
                    T("并入目标已预选为该词库",
                      await js("(document.getElementById('pack-select')||{}).value") == packs[0]["id"],
                      await js("(document.getElementById('pack-select')||{}).value"))
                    T("已切到「并入现有词库」模式",
                      await js("!!(document.getElementById('r-merge')||{}).classList && "
                               "document.getElementById('r-merge').classList.contains('on')"))
                    hint = await js("(document.getElementById('merge-hint')||{}).textContent") or ""
                    T("提示写的是「第 N+1 页起」且不含「本次 0 页」",
                      ("排到第" in hint) and ("本次 0 页" not in hint), hint)
                    T("词库列表带 max_page（并入起点用它算，不是用页数）",
                      all("max_page" in p for p in packs),
                      [(p["id"], p.get("pages"), p.get("max_page")) for p in packs])
                    # 起点必须按「最大页号 + 1」：单页包 pages=1 而页号是 2，用页数会差一。
                    # 找不到这种包就如实 SKIP —— 不拿"碰巧相等"的包假装验过。
                    odd = [p for p in packs
                           if int(p.get("max_page") or 0) != int(p.get("pages") or 0)]
                    if odd:
                        tgt = odd[0]
                        # 等提示**有字**再读：`/api/packs` 是异步拉的，元素存在但文本还是空，
                        # 直接取值会读到空串（本轮就是这么误报了一次）
                        await nav(base + "/generator?pack=" + urllib.parse.quote(tgt["id"]),
                                  "!!document.getElementById('merge-hint') && "
                                  "document.getElementById('merge-hint').textContent.length > 0")
                        h2 = await js("(document.getElementById('merge-hint')||{}).textContent") or ""
                        want = "第 %d 页起" % (int(tgt["max_page"]) + 1)
                        T("页号起点 = 最大页号 + 1（pages≠max_page 时按页号算）",
                          want in h2, "%s vs %s" % (want, h2))
                    else:
                        S("页号起点 = 最大页号 + 1", "本机没有 pages≠max_page 的词库可对照")
                    T("左上角返回链接固定为 /",
                      await js("document.getElementById('back-link').getAttribute('href')") == "/",
                      await js("document.getElementById('back-link').getAttribute('href')"))

                # ── 7) 目录索引已关 ──
                await nav(base + "/vocab-tool/packs/", "true")
                await asyncio.sleep(0.5)
                T("直接访问 packs 目录被 302 回工作台",
                  await js("location.pathname") == "/", await js("location.pathname"))

                # ── 7.5) 改名 / 删除（临时包；跑完磁盘上一点不剩）──
                # 模板包**动态取**：写死某个词库名，用户一删门禁就 FileNotFoundError 崩掉
                # （本轮就是这么崩的）。随便哪个能渲染的包都能当模板。
                _packs_dir = os.path.join(HERE, "packs")
                _cands = [d for d in sorted(os.listdir(_packs_dir))
                          if os.path.isdir(os.path.join(_packs_dir, d)) and not d.startswith("_")]
                tpl = os.path.join(_packs_dir, _cands[0]) if _cands else None
                tag = uuid.uuid4().hex[:6]
                old = "probe_ui_" + tag
                new = "probe_ui_" + tag + "_rn"
                old_dir = os.path.join(HERE, "packs", old)
                new_dir = os.path.join(HERE, "packs", new)
                trash_root = os.path.join(HERE, "work", "_trash")
                made_trash = []

                def api_ids():
                    with urllib.request.urlopen(base + "/api/packs", timeout=5) as rr:
                        return [p["id"] for p in (json.loads(rr.read().decode("utf-8"))
                                                  .get("packs") or [])]

                try:
                    if not tpl:
                        S("改名 / 删除（需要一个词库当模板）", "packs 里一个词库都没有")
                        raise _NoTemplate()
                    shutil.copytree(tpl, old_dir)
                    await nav(base + "/", "document.querySelectorAll('.pack').length > 0")
                    T("临时包出现在工作台上", old in api_ids(), api_ids())

                    await js("document.querySelector('[data-edit=\"%s\"]').click()" % old)
                    await asyncio.sleep(0.4)
                    T("点「✎ 编辑」打开弹窗且预填当前名",
                      await js("!document.getElementById('ed-mask').classList.contains('hidden')")
                      and (await js("document.getElementById('ed-name').value")) == old,
                      await js("document.getElementById('ed-name').value"))

                    await js("document.getElementById('ed-name').value = '%s';"
                             "document.getElementById('ed-save').click();" % new)
                    for _ in range(80):
                        if await js("document.getElementById('ed-mask')"
                                    ".classList.contains('hidden')"):
                            break
                        await asyncio.sleep(0.2)
                    T("改名成功后弹窗自动关闭",
                      await js("document.getElementById('ed-mask')"
                               ".classList.contains('hidden')"))
                    for _ in range(80):
                        if await js("!!document.querySelector('[data-edit=\"%s\"]')" % new):
                            break
                        await asyncio.sleep(0.2)
                    T("改名后卡片换成新名",
                      await js("!!document.querySelector('[data-edit=\"%s\"]')" % new))
                    ids = api_ids()
                    T("改名后 /api/packs：旧 id 消失、新 id 出现",
                      (old not in ids) and (new in ids), ids)
                    html = ""
                    pj = os.path.join(new_dir, "index.html")
                    if os.path.isfile(pj):
                        with open(pj, encoding="utf-8") as f:
                            html = f.read()
                    T("改名后包内 <title> 也换成新名（重渲染过）",
                      ("<title>%s · 外语词汇听读播放器</title>" % new) in html)

                    # 删除：先点「删除此词库」，再点「取消」
                    await js("document.querySelector('[data-edit=\"%s\"]').click()" % new)
                    await asyncio.sleep(0.4)
                    await js("document.getElementById('ed-del').click()")
                    await asyncio.sleep(0.3)
                    warn = await js("document.getElementById('ed-warn').textContent") or ""
                    T("确认视图写明「无法恢复」并给出确定 / 取消",
                      ("无法恢复" in warn) and
                      await js("!!document.getElementById('ed-c-ok') && "
                               "!!document.getElementById('ed-c-cancel')"), warn)
                    await js("document.getElementById('ed-c-cancel').click()")
                    await asyncio.sleep(0.3)
                    T("点「取消」回到编辑视图、词库仍在",
                      await js("!document.getElementById('ed-edit').classList.contains('hidden')")
                      and os.path.isdir(new_dir))

                    # 删除：再来一次，这次点「确定删除」
                    await js("document.getElementById('ed-del').click()")
                    await asyncio.sleep(0.25)
                    await js("document.getElementById('ed-c-ok').click()")
                    for _ in range(80):
                        if not await js("!!document.querySelector('[data-edit=\"%s\"]')" % new):
                            break
                        await asyncio.sleep(0.2)
                    T("确定删除后卡片消失",
                      (await js("!!document.querySelector('[data-edit=\"%s\"]')" % new)) is False)
                    T("删除后 packs 下目录已不在", not os.path.isdir(new_dir))
                    hit = [d for d in (os.listdir(trash_root) if os.path.isdir(trash_root) else [])
                           if d.startswith(new + "__")]
                    T("删除后 work/_trash 下留了一条（可手工找回）", len(hit) == 1, hit)
                    made_trash = [os.path.join(trash_root, d) for d in hit]
                except _NoTemplate:
                    pass
                finally:
                    shutil.rmtree(old_dir, ignore_errors=True)
                    shutil.rmtree(new_dir, ignore_errors=True)
                    for d in made_trash:
                        shutil.rmtree(d, ignore_errors=True)
                    await nav(base + "/", "document.querySelectorAll('.pack').length > 0")

                # ── 8) 移动端：只读 ──
                await send("Emulation.setDeviceMetricsOverride",
                           {"width": 390, "height": 844, "deviceScaleFactor": 1,
                            "mobile": True})
                await nav(base + "/", "document.querySelectorAll('.pack').length > 0")
                await asyncio.sleep(0.4)
                T("移动端卡片仍可见", (await js("document.querySelectorAll('.pack').length")) == len(packs))
                T("移动端不渲染「＋ 新建词表」",
                  (await js("!!document.getElementById('new-btn')")) is False)
                T("移动端显示「请在 PC 端」提示",
                  await js("!document.getElementById('pc-note').classList.contains('hidden')"))
                T("移动端没有文件输入框",
                  (await js("document.querySelectorAll('input[type=file]').length")) == 0)
                T("移动端卡片仍只有「打开播放器」1 个链接",
                  (await js("document.querySelectorAll('.pack-acts a[href]').length")) == len(packs),
                  await js("document.querySelectorAll('.pack-acts a[href]').length"))
                T("移动端不渲染「✎ 编辑」按钮",
                  (await js("document.querySelectorAll('.pack-acts button').length")) == 0,
                  await js("document.querySelectorAll('.pack-acts button').length"))

                T("全程没有未捕获异常", not exceptions, exceptions[:2])
    finally:
        for p in (br, srv):
            if p:
                try:
                    p.terminate()
                    p.wait(timeout=8)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        shutil.rmtree(prof, ignore_errors=True)

    print()
    for st, name, extra in RESULTS:
        print("%s  %s%s" % (st, name, "" if extra is None else "   [%s]" % (extra,)))
    bad = [r for r in RESULTS if r[0] == "FAIL"]
    print("\n结果: %d PASS / %d FAIL / %d SKIP" % (
        len([r for r in RESULTS if r[0] == "PASS"]), len(bad),
        len([r for r in RESULTS if r[0] == "SKIP"])))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
